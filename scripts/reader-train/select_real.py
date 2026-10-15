# SPDX-License-Identifier: GPL-3.0-or-later
"""Pick real issue texts for the reader's training data from SWE-rebench and SWE-Gym.

Run with an isolated interpreter that has pyarrow (the datasets are untrusted data):

    ~/opt/kev/.venv/bin/python -I scripts/reader-train/select_real.py \
        --rebench ~/opt/kev/data/swe-rebench-reader/filtered-0.parquet \
        --gym ~/opt/kev/data/swe-gym-reader/train-0.parquet \
        --evalsets fixtures/scorer --out <work>/real-candidates.jsonl

Every repository that has any case in an evaluation set (portable, portable-disputed) is
excluded whole, which also excludes those cases' instance ids and upstream PRs. Texts
are kept between 200 and 4,000 characters. Issues whose patch changes several files are
oversampled, for scope variety.
"""

import argparse
import json
import random
import re
from pathlib import Path

import pyarrow.parquet as pq

EVAL = ("portable.jsonl", "portable-disputed.jsonl", "suite.jsonl", "suite-disputed.jsonl")


def files_in_patch(patch: str) -> int:
    return len(set(re.findall(r"^diff --git a/(\S+)", patch or "", re.M)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebench", required=True)
    ap.add_argument("--gym", required=True)
    ap.add_argument("--evalsets", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n_rebench", type=int, default=1000)
    ap.add_argument("--n_gym", type=int, default=150)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    bad_repos, bad_ids = set(), set()
    for f in EVAL:
        for line in (Path(a.evalsets) / f).read_text().splitlines():
            d = json.loads(line)
            p = d.get("provenance") or {}
            if p.get("instance_id"):
                bad_ids.add(p["instance_id"])
                bad_repos.add(p["repo"].lower())
    rng = random.Random(a.seed)
    out, seen = [], set()

    def pool(path, source, license_, n):
        t = pq.read_table(path).to_pylist()
        cands = []
        for r in t:
            iid, repo, text = r["instance_id"], r["repo"], r["problem_statement"] or ""
            if iid in bad_ids or repo.lower() in bad_repos or iid in seen:
                continue
            if not 200 <= len(text) <= 4000:
                continue
            cands.append((r, files_in_patch(r.get("patch"))))
        many = [c for c in cands if c[1] >= 3]
        rest = [c for c in cands if c[1] < 3]
        rng.shuffle(many)
        rng.shuffle(rest)
        # at most 4 issues per repository, a third from multi-file patches
        picked, per_repo = [], {}
        for group, quota in ((many, n // 3), (rest, n)):
            k = 0
            for r, nf in group:
                if len(picked) >= n or k >= quota:
                    break
                if per_repo.get(r["repo"], 0) >= 4:
                    continue
                per_repo[r["repo"]] = per_repo.get(r["repo"], 0) + 1
                picked.append((r, nf))
                k += 1
        for r, nf in picked:
            seen.add(r["instance_id"])
            m = re.match(r"(.+)-(\d+)$", r["instance_id"])
            prov = {"dataset": source, "license": license_, "instance_id": r["instance_id"],
                    "url": f"https://huggingface.co/datasets/{source}", "repo": r["repo"],
                    "upstream_pr": f"https://github.com/{r['repo']}/pull/{m.group(2)}" if m else None,
                    "patch_files": nf}
            if r.get("license_name"):
                prov["repo_license"] = r["license_name"]
            out.append({"id": f"{source.split('/')[-1].lower()}:{r['instance_id']}",
                        "source": source.split("/")[-1], "provenance": prov, "kind": "work",
                        "text": r["problem_statement"]})

    pool(a.rebench, "nebius/SWE-rebench", "CC-BY-4.0", a.n_rebench)
    pool(a.gym, "SWE-Gym/SWE-Gym", "MIT", a.n_gym)
    with open(a.out, "w") as w:
        for d in out:
            w.write(json.dumps(d) + "\n")
    print(f"{len(out)} real candidates; excluded repos: {len(bad_repos)}, ids: {len(bad_ids)}")


if __name__ == "__main__":
    main()
