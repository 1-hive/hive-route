# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare scorer-eval results (``--out`` JSONL) of the untouched and the fine-tuned reader.

    uv run python scripts/reader-train/eval_summary.py <table> base.jsonl ft.jsonl [...]

Per file: right answers per fact, overall accuracy, tier lower/same/higher with the argmax
estimates (as scorer-eval's summary and PORTABLE.md's baseline table), tier lower with the
reader's tail values (as reader-qualify, tail 0.3), and p >= 0.7 accuracy and coverage.
"""

import json
import sys
from collections import Counter
from pathlib import Path

from hiveroute.decide import tail_value
from hiveroute.scoreeval import tier_of
from hiveroute.scorer import VALUES
from hiveroute.table import TAIL_DEFAULTS, load_table, rank


def summary(table, path):
    rows = [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
    right, tiers, tail_lower = Counter(), Counter(), 0
    conf = Counter()
    for r in rows:
        est = {f: e["value"] for f, e in r["estimates"].items()}
        for f in VALUES:
            right[f] += est.get(f) == r["labels"][f]
            e = r["estimates"].get(f)
            if e and e.get("p", 0) >= 0.7:
                conf["answered"] += 1
                conf["right"] += e["value"] == r["labels"][f]
        gold = tier_of(table, r["kind"], r["labels"])
        if len(est) == 4:
            got = tier_of(table, r["kind"], est)
            tiers["same" if got == gold else "lower" if rank(got) < rank(gold) else "higher"] += 1
            probs = {f: e["probabilities"] for f, e in r["estimates"].items()}
            reading = {f: tail_value(f, probs[f], TAIL_DEFAULTS[f]) for f in VALUES}
            tail_lower += rank(tier_of(table, r["kind"], reading)) < rank(gold)
        else:
            tiers["missing"] += 1
    n = len(rows)
    return {"cases": n, **{f: right[f] for f in VALUES},
            "overall": round(sum(right.values()) / (4 * n), 3),
            "tier": f"{tiers['lower']} / {tiers['same']} / {tiers['higher']}"
                    + (f" (+{tiers['missing']} missing)" if tiers["missing"] else ""),
            "tail_lower": tail_lower,
            "p>=0.7": {"accuracy": round(conf["right"] / conf["answered"], 3) if conf["answered"] else None,
                       "coverage": round(conf["answered"] / (4 * n), 3)}}


def main():
    table = load_table(sys.argv[1])
    for p in sys.argv[2:]:
        print(p, json.dumps(summary(table, p)))


if __name__ == "__main__":
    main()
