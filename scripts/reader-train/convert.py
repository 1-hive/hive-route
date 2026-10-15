# SPDX-License-Identifier: GPL-3.0-or-later
"""Merge both labellers' labels, keep agreement, and write Kev training records.

    uv run python scripts/reader-train/convert.py --cases real.jsonl authored.jsonl \
        --labels labels-claude.jsonl labels-codex.jsonl --drop leakage.json \
        --dataset fixtures/reader-train/cases.jsonl --kev <work>/kev [--heldout 0.15]

A case is kept when both labellers are sure and agree on all four facts. The Kev records
use exactly the reader's request (hiveroute.scorer): the state as ``estimate`` builds it
and the questions of ``systemone_questions``; verification is two questions, and for
``none`` the second is omitted. Split by case: 85% train, 15% held out (half for the
temperature fit, half for in-distribution evaluation), stratified by source.
"""

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from hiveroute.scorer import MAX_CHARS, VALUES, systemone_questions

FACTS = list(VALUES)


def state(kind, text):
    # scorer.estimate with no history: head + "\nTask text:\n" + text[:max_chars]
    return f"Task kind: {kind}\n\nTask text:\n{text[:MAX_CHARS]}"


def record(case):
    qs = systemone_questions(FACTS)
    f = case["facts"]
    qs["specification"]["label"] = f["specification"]
    qs["scope"]["label"] = f["scope"]
    qs["consequence"]["label"] = f["consequence"]
    qs["verification.checked"]["label"] = f["verification"] != "none"
    if f["verification"] == "none":
        del qs["verification.controlled"]
    else:
        qs["verification.controlled"]["label"] = f["verification"]
    return {"id": case["id"], "state": state(case["kind"], case["text"]), "questions": qs}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--labels", nargs=2, required=True, metavar=("A", "B"))
    ap.add_argument("--check", help="a third labeller's labels for some cases: a kept case it "
                    "labels differently is dropped (status check_disagree)")
    ap.add_argument("--drop", help="leakage.py report; its 'drop' ids are excluded")
    ap.add_argument("--dataset", required=True, help="all labelled cases with both labels")
    ap.add_argument("--kev", required=True, help="directory for train/calibration/development JSONL")
    ap.add_argument("--heldout", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()

    cases = {}
    for f in a.cases:
        for x in Path(f).read_text().splitlines():
            if x.strip():
                d = json.loads(x)
                cases[d["id"]] = d
    labs = []
    for f in a.labels:
        m = {}
        for x in Path(f).read_text().splitlines():
            if x.strip():
                d = json.loads(x)
                m.setdefault(d["id"], d)
        labs.append(m)
    check = {}
    if a.check:
        for x in Path(a.check).read_text().splitlines():
            if x.strip():
                d = json.loads(x)
                check.setdefault(d["id"], d)
    drop = set(json.loads(Path(a.drop).read_text())["drop"]) if a.drop else set()

    stats = Counter()
    agree_fact = Counter()
    out, kept = [], []
    seen_text = set()
    for cid, c in cases.items():
        la, lb = labs[0].get(cid), labs[1].get(cid)
        if not (la and lb):
            stats["unlabelled"] += 1
            continue
        stats["labelled"] += 1
        for f in FACTS:
            agree_fact[f] += la["facts"][f] == lb["facts"][f]
        agree = la["facts"] == lb["facts"]
        sure = la["sure"] and lb["sure"]
        key = " ".join(c["text"].split()).lower()
        lc = check.get(cid)
        status = ("leakage" if cid in drop else "duplicate" if key in seen_text
                  else "disagree" if not agree else "unsure" if not sure
                  else "check_disagree" if lc and lc["facts"] != la["facts"] else "kept")
        seen_text.add(key)
        stats[status] += 1
        row = {k: c[k] for k in ("id", "source", "provenance", "kind", "text") if k in c}
        if c.get("source") == "authored":
            row["generator"] = c.get("generator")
        row["facts"] = la["facts"] if status == "kept" else None
        row["labels"] = {la["labeller"]: {"facts": la["facts"], "sure": la["sure"], "note": la["note"]},
                         lb["labeller"]: {"facts": lb["facts"], "sure": lb["sure"], "note": lb["note"]}}
        if lc:
            row["labels"][lc["labeller"]] = {"facts": lc["facts"], "sure": lc["sure"], "note": lc["note"]}
        row["status"] = status
        if status == "leakage":  # too close to an evaluation case: its text isn't published
            row["text"] = None
        out.append(row)
        if status == "kept":
            kept.append(row)

    rng = random.Random(a.seed)
    split = {}
    for src in sorted({r["source"] for r in kept}):
        rs = sorted((r for r in kept if r["source"] == src), key=lambda r: r["id"])
        rng.shuffle(rs)
        k = round(len(rs) * a.heldout)
        for i, r in enumerate(rs):
            split[r["id"]] = "train" if i >= k else ("calibration" if i % 2 == 0 else "development")
    for r in out:
        if r["status"] == "kept":
            r["split"] = split[r["id"]]

    Path(a.dataset).parent.mkdir(parents=True, exist_ok=True)
    with open(a.dataset, "w") as w:
        for r in sorted(out, key=lambda r: r["id"]):
            w.write(json.dumps(r, ensure_ascii=False) + "\n")
    kev = Path(a.kev)
    kev.mkdir(parents=True, exist_ok=True)
    parts = Counter()
    for part in ("train", "calibration", "development"):
        rs = sorted((r for r in kept if split[r["id"]] == part), key=lambda r: r["id"])
        if part == "train":
            rng.shuffle(rs)
        with open(kev / f"{part}.jsonl", "w") as w:
            for r in rs:
                w.write(json.dumps(record(r), ensure_ascii=False) + "\n")
        parts[part] = len(rs)
    dist = {f: dict(Counter(r["facts"][f] for r in kept)) for f in FACTS}
    summary = {"stats": dict(stats), "agreement_per_fact": {f: f"{agree_fact[f]}/{stats['labelled']}" for f in FACTS},
               "kept_by_source": dict(Counter(r["source"] for r in kept)),
               "kept_kind": dict(Counter(r["kind"] for r in kept)),
               "splits": dict(parts), "kept_values": dist}
    (kev / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
