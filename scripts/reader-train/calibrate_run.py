# SPDX-License-Identifier: GPL-3.0-or-later
"""Fit a fine-tuned Kev run's temperature on the held-out calibration records, write it into
the checkpoint, and score the development records against the untouched model.

    cd ~/opt/kev && uv run python <hive-route>/scripts/reader-train/calibrate_run.py \
        --run runs/reader-ft-v1 --base jaredpalmer/kev-4b@v1.0 --data <kev-data-dir> --out runs/reader-ft-v1-eval

The same recipe as Kev's kev-finetune skill (kev_modal.py ``score``): raw logits at T=1,
one temperature by minimum NLL (micro) on calibration.jsonl, development scored at it.
On a 16 GB GPU, run ``--only fine_tuned`` and ``--only baseline`` as two processes.
"""

import argparse
import gc
import json
from pathlib import Path

import torch
from kev.benchmark import evaluate_records
from kev.checkpoint import LoadOptions, read_meta, write_meta
from kev.data import load_records
from kev.metrics import fit_temperature, grouped_metrics
from kev.model import training_context
from kev.predictors import LocalPredictor


def score(run, cal, dev, out, device="cuda", dtype=None):
    # the context the run trained in (--max_state 2048): reader texts are longer than 384 tokens
    pred = LocalPredictor(run, device, LoadOptions(temperature=1.0, dtype=dtype),
                          context=training_context(2048))
    try:
        _, cal_rows = evaluate_records(cal, pred, Path(out) / "calibration")
        T = fit_temperature(cal_rows, aggregation="micro")
        report, rows = evaluate_records(dev, pred, Path(out) / "development", T)
    finally:
        del pred
        gc.collect()
        torch.cuda.empty_cache()
    clean = [r for r in rows if r["variant"] == "clean"]
    return {"temperature": T, "raw": report["clean"], "calibrated": report["calibrated_clean"],
            "per_question": grouped_metrics(clean, "question", T), "n_questions": len(clean)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--write", type=int, default=1, help="write the fitted temperature into --run")
    ap.add_argument("--only", choices=("fine_tuned", "baseline"),
                    help="score one model (the GPU may not hold both in one process); "
                         "results merge into <out>/result.json")
    a = ap.parse_args()
    cal = load_records(Path(a.data) / "calibration.jsonl")
    dev = load_records(Path(a.data) / "development.jsonl")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "result.json"
    result = json.loads(path.read_text()) if path.exists() else {}
    result.update({"calibration_records": len(cal), "development_records": len(dev)})
    if a.only != "baseline":
        result["fine_tuned"] = score(a.run, cal, dev, out / "fine-tuned", a.device)
    if a.write and a.only != "baseline":
        m = read_meta(a.run)
        m.temperature = result["fine_tuned"]["temperature"]
        m.extra["temperature_fit"] = {"rows": "calibration.jsonl", "n": len(cal),
                                      "method": "min NLL, micro, kev.metrics.fit_temperature",
                                      "value": m.temperature}
        write_meta(a.run, m)
    if a.only != "fine_tuned":
        # bf16, as kev.serve runs it: the base checkpoint loads in fp32 otherwise, which a
        # 16 GB GPU can't hold (the fine-tuned run already has a bf16 backbone)
        result["baseline"] = score(a.base, cal, dev, out / "baseline", a.device, torch.bfloat16)
    path.write_text(json.dumps(result, indent=1, default=float))
    for k in ("baseline", "fine_tuned"):
        if k not in result:
            continue
        r = result[k]
        print(k, "T", round(r["temperature"], 3), "acc", r["calibrated"].get("acc"),
              {q: round(m.get("acc", 0), 3) for q, m in r["per_question"].items()})


if __name__ == "__main__":
    main()
