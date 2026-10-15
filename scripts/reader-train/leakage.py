# SPDX-License-Identifier: GPL-3.0-or-later
"""Check candidate texts against the evaluation sets: instance ids, repositories, upstream
PRs, and text similarity (word-trigram Jaccard and TF-IDF cosine).

    python3 scripts/reader-train/leakage.py --evalsets fixtures/scorer \
        --workspace ~/repos/hive-workspace.git --cases a.jsonl b.jsonl --report <work>/leakage.json

Writes a report with every flagged candidate and its nearest evaluation case; candidates
at or above either threshold are listed under "drop".
"""

import argparse
import json
import math
import re
import subprocess
from collections import Counter
from pathlib import Path

EVAL = ("portable.jsonl", "portable-disputed.jsonl", "suite.jsonl", "suite-disputed.jsonl",
        "cases.jsonl")
SUITE_COMMIT = "90ca75fd99ed30475c45999bb99c9ef0e1af43e7"


def words(t):
    return re.findall(r"[a-z0-9_]+", t.lower())


def trigrams(t):
    w = words(t)
    return {tuple(w[i:i + 3]) for i in range(len(w) - 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evalsets", required=True)
    ap.add_argument("--workspace")
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--jaccard", type=float, default=0.15)
    ap.add_argument("--cosine", type=float, default=0.4)
    a = ap.parse_args()

    ev, ids, repos, prs = [], set(), set(), set()
    for f in EVAL:
        for line in (Path(a.evalsets) / f).read_text().splitlines():
            d = json.loads(line)
            text = d.get("text")
            if text is None and a.workspace:
                r = subprocess.run(["git", "-C", a.workspace, "show",
                                    f"{d.get('commit') or SUITE_COMMIT}:{d['path']}"],
                                   capture_output=True, text=True)
                text = r.stdout if r.returncode == 0 else None
            if text:
                ev.append((f"{f}:{d['id']}", text))
            p = d.get("provenance") or {}
            if p.get("instance_id"):
                ids.add(p["instance_id"])
                repos.add(p["repo"].lower())
                if p.get("upstream_pr"):
                    prs.add(p["upstream_pr"])
    cands = [json.loads(x) for f in a.cases for x in Path(f).read_text().splitlines() if x.strip()]

    docs = [t for _, t in ev] + [c["text"] for c in cands]
    df = Counter(w for t in docs for w in set(words(t)))
    n = len(docs)

    def vec(t):
        tf = Counter(words(t))
        v = {w: (1 + math.log(c)) * math.log(n / df[w]) for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / norm for w, x in v.items()}

    evv = [(k, vec(t), trigrams(t)) for k, t in ev]
    flagged, drop, id_hits = [], [], []
    for c in cands:
        p = c.get("provenance") or {}
        if p.get("instance_id") in ids or (p.get("repo") or "").lower() in repos \
                or (p.get("upstream_pr") and p["upstream_pr"] in prs):
            id_hits.append(c["id"])
        cv, ct = vec(c["text"]), trigrams(c["text"])
        best = (0.0, 0.0, None)
        for k, v, tg in evv:
            cos = sum(x * v.get(w, 0.0) for w, x in cv.items())
            jac = len(ct & tg) / len(ct | tg) if ct and tg else 0.0
            if max(cos, jac) > max(best[0], best[1]):
                best = (cos, jac, k)
        rec = {"id": c["id"], "cosine": round(best[0], 3), "jaccard": round(best[1], 3), "nearest": best[2]}
        if best[0] >= 0.3 or best[1] >= 0.1:
            flagged.append(rec)
        if best[0] >= a.cosine or best[1] >= a.jaccard:
            drop.append(c["id"])
    report = {"eval_texts": len(ev), "candidates": len(cands), "excluded_ids": len(ids),
              "excluded_repos": len(repos), "excluded_prs": len(prs),
              "id_repo_pr_hits": id_hits, "thresholds": {"cosine": a.cosine, "jaccard": a.jaccard},
              "drop": sorted(set(drop) | set(id_hits)),
              "flagged": sorted(flagged, key=lambda r: -max(r["cosine"], r["jaccard"]))}
    Path(a.report).write_text(json.dumps(report, indent=1))
    print(f"eval texts {len(ev)}; candidates {len(cands)}; id/repo/PR hits {len(id_hits)}; "
          f"flagged {len(flagged)}; drop {len(report['drop'])}")


if __name__ == "__main__":
    main()
