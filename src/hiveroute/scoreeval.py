# SPDX-License-Identifier: GPL-3.0-or-later
"""``scorer-eval``: run scorers on a labelled suite (ROUTING.md §4.3).

Each case is a task's text with its facts labelled by hand (``fixtures/scorer``). A case
gives its text inline (``text``) or as a work order in a workspace repository (``path`` at
the suite's ``commit``, read with ``git show``, so the text is the one that was labelled).
Each scorer is asked for all four facts; ``Route facts:`` and ``Review tier:`` lines are
removed first, so the scorer can't read the answer.

The summary, per scorer: accuracy per fact (all cases and per source), how often an
estimate is *cheaper* than the label (the error that lowers a tier), the tier the
estimates give against the tier the labels give, and for a scorer with probabilities, the
accuracy and coverage above a few thresholds. Nothing here is logged or replayed: it
judges scorers, it doesn't route.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from .decide import compute_tier, resolve_facts
from .errors import RouteError
from .scorer import VALUES, estimate, scorers
from .table import Table, rank

STRIP = re.compile(r"^(Route facts|Review tier):.*$", re.IGNORECASE | re.MULTILINE)
THRESHOLDS = (0.5, 0.7, 0.9)


def load_suite(path: str | Path) -> list[dict]:
    try:
        lines = Path(path).read_text().splitlines()
        return [json.loads(x) for x in lines if x.strip()]
    except (OSError, json.JSONDecodeError) as e:
        raise RouteError("INPUT_INVALID", f"{path}: {e}") from e


def case_text(case: dict, workspace: str | None, commit: str | None) -> str:
    if "text" in case:
        text = case["text"]
    else:
        if not workspace:
            raise RouteError("INPUT_INVALID", f"{case['id']}: an order case needs --workspace")
        ref = f"{case.get('commit') or commit or 'HEAD'}:{case['path']}"
        r = subprocess.run(["git", "-C", workspace, "show", ref], capture_output=True, text=True)
        if r.returncode != 0:
            raise RouteError("INPUT_INVALID", f"{case['id']}: git show {ref}: {r.stderr.strip()}")
        text = r.stdout
    return STRIP.sub("", text)


def tier_of(table: Table, kind: str, facts: dict) -> str:
    request = {"facts": {"kind": kind, "leverage": 0, **facts}, "author": {"tier": "light"}}
    return compute_tier(request, resolve_facts(request, table), table)[0]


def run(table: Table, suite: list[dict], routes: list[str] | None, workspace: str | None,
        commit: str | None) -> list[dict]:
    """One result per (case, scorer)."""
    configured = {s["route"]: s for s in scorers(table)}
    chosen = routes or list(configured)
    if not chosen:
        raise RouteError("INPUT_INVALID", "no scorer: name one with --scorer or set it in the table")
    rows = []
    for case in suite:
        text = case_text(case, workspace, commit)
        for rid in chosen:
            if rid not in table.routes:
                raise RouteError("INPUT_INVALID", f"--scorer: unknown route {rid!r}")
            cfg = configured.get(rid, {"route": rid, "mode": "shadow"})
            est = estimate(table, cfg, case["kind"], list(VALUES), text)
            rows.append({"case": case["id"], "source": case.get("source", "?"),
                         "kind": case["kind"], "labels": case["facts"], **est})
    return rows


def summarize(table: Table, rows: list[dict]) -> dict:
    out = {}
    by_route: dict = defaultdict(list)
    for r in rows:
        by_route[r["route_id"]].append(r)
    for rid, rs in by_route.items():
        acc: Counter = Counter()
        n: Counter = Counter()
        cheaper: Counter = Counter()
        confusion: dict = defaultdict(Counter)
        tiers: Counter = Counter()
        conf: dict = {t: Counter() for t in THRESHOLDS}
        for r in rs:
            est = {f: e["value"] for f, e in r["estimates"].items()}
            for f, gold in r["labels"].items():
                for scope in ("all", r["source"]):
                    n[(f, scope)] += 1
                got = est.get(f)
                confusion[f][f"{gold} -> {got or 'missing'}"] += 1
                if got is None:
                    continue
                ok = got == gold
                for scope in ("all", r["source"]):
                    acc[(f, scope)] += ok
                if VALUES[f].index(got) < VALUES[f].index(gold):
                    cheaper[f] += 1
                p = r["estimates"][f].get("p")
                if p is not None:
                    for t in THRESHOLDS:
                        if p >= t:
                            conf[t]["answered"] += 1
                            conf[t]["right"] += ok
            gold_tier = tier_of(table, r["kind"], r["labels"])
            got_tier = tier_of(table, r["kind"], {**r["labels"], **est}) if len(est) == 4 else None
            tiers["missing" if got_tier is None else "same" if got_tier == gold_tier
                  else "lower" if rank(got_tier) < rank(gold_tier) else "higher"] += 1
        cells = len(rs) * len(VALUES)
        out[rid] = {
            "model": rs[0]["model"], "cases": len(rs),
            "errors": sum(1 for r in rs if r["error"]),
            "seconds_mean": round(sum(r["seconds"] for r in rs) / len(rs), 2),
            "accuracy": {f"{f} ({s})": f"{acc[(f, s)]}/{n[(f, s)]}"
                         for f, s in sorted(n, key=lambda k: (list(VALUES).index(k[0]), k[1]))},
            "accuracy_overall": round(sum(acc[(f, "all")] for f in VALUES) / cells, 3),
            "cheaper_than_label": dict(cheaper),
            "tier_vs_labels": dict(tiers),
            "confusion": {f: dict(sorted(c.items())) for f, c in confusion.items()},
        }
        if any(conf[t]["answered"] for t in THRESHOLDS):
            out[rid]["by_probability"] = {
                f"p>={t}": {"coverage": round(conf[t]["answered"] / cells, 3),
                            "accuracy": round(conf[t]["right"] / conf[t]["answered"], 3)
                            if conf[t]["answered"] else None} for t in THRESHOLDS}
    return out
