# SPDX-License-Identifier: GPL-3.0-or-later
"""Shadow tables, what-if replays and the outcomes report (ROUTING.md §4.6, §6).

- **Shadow table:** ``decide --shadow-table`` also decides with a second table (in
  ``live`` mode, same request and state) and logs ``route.shadow_decided``. Nothing acts
  on it; replay checks it like any decision.
- **What-if:** ``whatif`` re-decides every logged request under another table, so a
  proposed change can be judged on the hive's real history before it is pinned.
- **Report:** ``report`` joins the log's attempts with task outcomes from the record
  (review verdicts, closes) and, optionally, usage per task folder from AgentsView, and
  groups them by kind, facts, tier, route, mode and runtime (the request's ``runtime``
  label, so evidence from different runtimes stays apart): the evidence for tuning the table. A
  person approves every table change (§4.6); the report only proposes.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from .decide import ORDER, costlier_than, decide, tail_value
from .errors import RouteError
from .log import read
from .table import TAIL_DEFAULTS, Table, rank


def shadow(request: dict, table: Table, state: dict) -> dict:
    return decide(request, table, state, "live")


def whatif(log: str, table: Table) -> dict:
    """Re-decide every logged request under ``table``. Returns counts and the changes."""
    changes, total = [], 0
    tiers: Counter = Counter()
    routes: Counter = Counter()
    for ev in read(log):
        if ev["type"] not in ("route.decided", "route.waiting"):
            continue
        d = ev["data"]
        total += 1
        try:
            again = decide(d["request"], table, d["state"], "live")
        except RouteError as e:
            changes.append({"attempt": d["request"]["attempt"], "error": str(e)})
            continue
        before, after = d["decision"], again
        tiers[(before["computed_tier"], after["computed_tier"])] += 1
        routes[(before.get("route_id"), after.get("route_id"))] += 1
        if (before.get("route_id"), before["decision"]) != (after.get("route_id"),
                                                            after["decision"]):
            changes.append({"attempt": d["request"]["attempt"],
                            "before": f"{before['decision']} {before.get('route_id')} "
                                      f"({before['tier']})",
                            "after": f"{after['decision']} {after.get('route_id')} "
                                     f"({after['tier']})"})
    return {"decisions": total, "changed": len(changes), "changes": changes,
            "tiers": {f"{a} -> {b}": n for (a, b), n in sorted(tiers.items())},
            "routes": {f"{a} -> {b}": n for (a, b), n in sorted(routes.items(),
                                                                 key=lambda x: str(x))}}


def task_outcomes(events: list[dict]) -> dict:
    """Per task, from record events: reviews passed and failed, closed or not."""
    out: dict = defaultdict(lambda: {"results": 0, "passed": 0, "failed": 0, "closed": False,
                                     "cancelled": False})
    for e in events:
        t = e.get("task")
        if not t:
            continue
        if e["type"] == "task.result_posted":
            out[t]["results"] += 1
        elif e["type"] == "review.recorded":
            v = (e.get("data") or {}).get("verdict")
            if v in ("passed", "failed"):
                out[t][v] += 1
        elif e["type"] == "task.closed":
            out[t]["closed"] = True
        elif e["type"] == "task.cancelled":
            out[t]["cancelled"] = True
    return dict(out)


def load_events(path: str | None) -> list[dict]:
    if not path:
        return []
    text = Path(path).read_text() if path != "-" else __import__("sys").stdin.read()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def profile(facts: dict) -> str:
    v = {k: f["value"] for k, f in facts.items()}
    return (f"{v['kind']}: spec={v['specification']} ver={v['verification']} "
            f"scope={v['scope']} cons={v['consequence']}")


def report(log: str, events: list[dict], usage: dict | None = None) -> dict:
    """Group routed attempts by (profile, tier, route) with their tasks' outcomes."""
    outcomes = task_outcomes(events)
    groups: dict = defaultdict(lambda: {"attempts": 0, "tasks": set(), "accepted": set(),
                                        "failed_reviews": 0, "output_tokens": 0})
    scored: dict = defaultdict(list)  # attempt -> one entry per scorer
    for e in read(log):
        if e["type"] == "route.scored":
            scored[e["data"]["attempt"]].append(e["data"])
    agreement: Counter = Counter()
    for ev in read(log):
        if ev["type"] != "route.decided":
            continue
        d = ev["data"]["decision"]
        task = d["task"]
        runtime = ev["data"]["request"].get("runtime", "unlabelled")
        g = groups[(profile(d["facts"]), d["tier"], d["route_id"], ev["data"]["mode"], runtime)]
        g["attempts"] += 1
        g["tasks"].add(task)
        o = outcomes.get(task)
        if o and o["closed"] and o["passed"]:
            g["accepted"].add(task)
        if o:
            g["failed_reviews"] = max(g["failed_reviews"], o["failed"])
        if usage and task in usage:
            g["output_tokens"] += usage.pop(task)
        for s in scored.get(d["attempt"], []) if o and o["closed"] else []:
            outcome = "first-pass" if o["failed"] == 0 else "rework"
            agreement[(s["route_id"], s.get("tier_with_estimates"), outcome)] += 1
    rows = []
    for (prof, tier, route, mode, runtime), g in sorted(groups.items()):
        rows.append({"profile": prof, "tier": tier, "route": route, "mode": mode,
                     "runtime": runtime,
                     "attempts": g["attempts"], "tasks": len(g["tasks"]),
                     "accepted": len(g["accepted"]), "failed_reviews": g["failed_reviews"],
                     "output_tokens": g["output_tokens"] or None})
    proposals = [
        f"{r['profile']} on {r['route']} ({r['tier']}): {r['accepted']}/{r['tasks']} accepted "
        "with no failed reviews; try one tier lower in a shadow table"
        for r in rows if r["tier"] in ("strong", "standard") and r["tasks"] >= 3
        and r["accepted"] == r["tasks"] and r["failed_reviews"] == 0]
    out = {"groups": rows,
           "scorer": {f"{r}: {t} / {k}": n for (r, t, k), n in sorted(agreement.items(), key=str)},
           "proposals": proposals}
    readings = reader_report(log)
    if readings:
        out["reader"] = readings
    return out


def reader_report(log: str) -> dict | None:
    """The reader's readings (§4.8): failures, latency, how often a reading would change the
    tier, and per writer and fact how often the reader is confident the stated value is too
    cheap (it would raise it) or too costly (evidence for loosening it). Thresholds are the
    ones in the table each reading was taken under."""
    events = list(read(log))
    tables = {e["data"]["pin"]: e["data"]["table"] for e in events
              if e["type"] == "route.table_pinned"}
    reads = [e["data"] for e in events if e["type"] == "route.read"]
    if not reads:
        return None
    tier: Counter = Counter()
    facts: dict = defaultdict(Counter)
    for r in reads:
        if not r["probabilities"]:
            continue
        if "tier_with_reading" in r:
            a, b = rank(r["tier_with_reading"]), rank(r["tier_without"])
            tier["higher" if a > b else "lower" if a < b else "same"] += 1
        reader = (tables.get(r["table"]) or {}).get("reader") or {}
        raise_at = reader.get("raise_at", 0.9)
        tails = {**TAIL_DEFAULTS, **reader.get("tail", {})}
        for f, probs in r["probabilities"].items():
            c = facts[f"{r.get('writer') or 'unknown'} / {f}"]
            stated = r["stated"].get(f)
            if stated is None:
                c["unknown, filled"] += 1
                continue
            c["stated"] += 1
            if costlier_than(f, stated, probs) >= raise_at:
                c["too cheap (would raise)"] += 1
            elif ORDER[f].index(tail_value(f, probs, tails[f])) < ORDER[f].index(stated):
                c["too costly (confidently cheaper)"] += 1
    ok = [r for r in reads if r["probabilities"]]
    return {"readings": len(reads), "failed": len(reads) - len(ok),
            "seconds_mean": round(sum(r["seconds"] for r in reads) / len(reads), 2),
            "tier_with_reading": dict(tier),
            "facts": {k: dict(v) for k, v in sorted(facts.items())}}


def agentsview_usage(sessions: list[dict], root: str) -> dict:
    """Output tokens per task, from ``agentsview session list --json`` rows whose working
    folder is a task folder under ``root`` (a ``-review`` folder counts for its task)."""
    out: Counter = Counter()
    root = root.rstrip("/") + "/"
    for s in sessions:
        cwd = s.get("cwd") or ""
        if not cwd.startswith(root):
            continue
        task = cwd[len(root):].split("/")[0]
        task = task.removesuffix("-review")
        out[task] += s.get("total_output_tokens") or 0
    return dict(out)
