# SPDX-License-Identifier: GPL-3.0-or-later
"""``hive-route``: check tables, make decisions, keep and replay the decision log."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from . import log as routelog
from .canary import qualify, set_lesson
from .canonical import format_time, parse_time
from .decide import decide
from .errors import RouteError
from .evaluate import agentsview_usage, load_events, report, shadow, whatif
from .gateway import budget_notes, litellm_config
from .observe import CODEX_ROOT, find_manifests, observe
from .record import sync
from .scorer import apply, score
from .table import load_table
from .usage import collect, load_sources


def _read_json(arg: str) -> dict:
    try:
        text = sys.stdin.read() if arg == "-" else Path(arg).read_text(encoding="utf-8")
        obj = json.loads(text)
    except (OSError, json.JSONDecodeError) as e:
        raise RouteError("INPUT_INVALID", f"{arg}: {e}") from e
    if not isinstance(obj, dict):
        raise RouteError("INPUT_INVALID", f"{arg}: not a JSON object")
    return obj


def _as_of(arg: str | None) -> datetime:
    if not arg:
        return datetime.now(UTC)
    try:
        return parse_time(arg)
    except ValueError as e:
        raise RouteError("INPUT_INVALID", f"--as-of: {e}") from e


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hive-route", description=__doc__)
    p.add_argument("--version", action="version", version=f"hive-route {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="validate a route table and print its pins")
    c.add_argument("table")
    c.add_argument("--json", action="store_true")

    d = sub.add_parser("decide", help="choose the tier and route for one attempt")
    d.add_argument("table")
    d.add_argument("request", help="request JSON file, or - for stdin")
    src = d.add_mutually_exclusive_group()
    src.add_argument("--state", help="state JSON file (default: no usage known, nothing qualified)")
    src.add_argument("--sources", help="build the state now from this sources file (§7)")
    d.add_argument("--mode", choices=["live", "fixed"],
                   help="default: the log's latest route.mode_set, else live")
    d.add_argument("--log", help="append the decision to this JSONL log")
    d.add_argument("--shadow-table", help="also decide with this table (live mode) and log it "
                   "as route.shadow_decided; nothing acts on it")
    d.add_argument("--task-text", help="the task's text, for the scorer (§4.3) when the table "
                   "has one and an unknown fact could lower the tier")

    m = sub.add_parser("mode", help="record a mode change in the log")
    m.add_argument("mode", choices=["live", "fixed"])
    m.add_argument("table")
    m.add_argument("--log", required=True)

    s = sub.add_parser("state", help="build the usage and qualification state from a sources file")
    s.add_argument("table")
    s.add_argument("sources")
    s.add_argument("--as-of", help="the state's time (default: now)")
    s.add_argument("--notes", action="store_true", help="print where each pool's usage came from")

    mf = sub.add_parser("manifest", help="print the attempt manifest for a routed decision")
    mf.add_argument("decision", help="decision JSON file, or - for stdin")
    mf.add_argument("--cwd", required=True, help="the folder the harness runs in")
    mf.add_argument("--output", required=True, help="the file the harness's output goes to")

    o = sub.add_parser("observe", help="check attempts' reported models for drift (§3)")
    o.add_argument("log")
    o.add_argument("manifests", nargs="+", help="attempt manifest files or glob patterns")
    o.add_argument("--sources", help="sources file naming the qualifications file to update")
    o.add_argument("--codex-root", default=CODEX_ROOT)
    o.add_argument("--since-days", type=float, default=8,
                   help="only manifests written in this many days (default 8)")

    ca = sub.add_parser("canary", help="qualify routes on a canary suite (§3)")
    csub = ca.add_subparsers(dest="canary_cmd", required=True)
    cr = csub.add_parser("run", help="run a suite on one route and record the result")
    cr.add_argument("table")
    cr.add_argument("route")
    cr.add_argument("--suite", required=True)
    cr.add_argument("--sources", required=True, help="harness commands and qualifications file")
    cr.add_argument("--log", required=True)
    cr.add_argument("--case", action="append", help="run only this case (repeatable)")
    cr.add_argument("--lesson", help="the lesson to record (default: a summary of the results)")
    cr.add_argument("--codex-root", default=CODEX_ROOT)
    cr.add_argument("--max-usage", type=float,
                    help="stop before a case if the route's pool has used this share (0-1)")
    cl = csub.add_parser("lesson", help="record an operator's lesson for a route")
    cl.add_argument("route")
    cl.add_argument("lesson")
    cl.add_argument("--sources", required=True)
    cl.add_argument("--log", required=True)

    g = sub.add_parser("gateway-config", help="generate the LiteLLM config from the table (§8)")
    g.add_argument("table")
    g.add_argument("--include-subscription", action="store_true",
                   help="also subscription routes (only where the provider's terms allow it)")

    rc = sub.add_parser("record", help="post new log entries' summaries to the hive record (A1)")
    rc.add_argument("log")
    rc.add_argument("--hive-cmd", default="hive", help="the hive CLI (identity from HIVE_* env)")
    rc.add_argument("--dry-run", action="store_true", help="print the events instead")

    w = sub.add_parser("whatif", help="re-decide every logged request under another table")
    w.add_argument("log")
    w.add_argument("table")
    w.add_argument("--json", action="store_true")

    rp = sub.add_parser("report", help="attempts by kind, facts, tier and route, with outcomes")
    rp.add_argument("log")
    rp.add_argument("--events", help="the record's events as JSONL (`hive events`), or -")
    rp.add_argument("--agentsview", metavar="ROOT",
                    help="add output tokens per task from AgentsView, for task folders under ROOT")
    rp.add_argument("--json", action="store_true")

    r = sub.add_parser("replay", help="recompute every decision in a log")
    r.add_argument("log")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except RouteError as e:
        print(f"hive-route: {e}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    if args.cmd == "check":
        t = load_table(args.table)
        pins = {rid: t.route_pin(rid) for rid in sorted(t.routes)}
        if args.json:
            print(json.dumps({"table": t.pin, "routes": pins}, indent=2))
        else:
            print(f"table  {t.pin}")
            for rid, pin in pins.items():
                print(f"route  {rid:<24} {pin}")
        return 0

    if args.cmd == "decide":
        t = load_table(args.table)
        request = _read_json(args.request)
        if args.state:
            state = _read_json(args.state)
        elif args.sources:
            state, _ = collect(t, load_sources(args.sources), datetime.now(UTC))
        else:
            state = {"as_of": format_time(datetime.now(UTC))}
        mode = args.mode or (routelog.current_mode(args.log) if args.log else None) or "live"
        if args.task_text:
            try:
                text = Path(args.task_text).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise RouteError("INPUT_INVALID", f"--task-text: {e}") from e
            scored = score(t, request, text)
            if scored:
                # What the estimates would do to the tier, logged in either scorer mode.
                with_est = decide(apply(request, {**scored, "mode": "live"}), t, state, "live")
                scored["tier_with_estimates"] = with_est["computed_tier"]
                scored["tier_without"] = decide(request, t, state, "live")["computed_tier"]
                request = apply(request, scored)
                if args.log:
                    routelog.record_scored(args.log, t, scored)
        decision = decide(request, t, state, mode)
        if args.log:
            routelog.record_decision(args.log, t, request, state, mode, decision)
        if args.shadow_table:
            st = load_table(args.shadow_table)
            sd = shadow(request, st, state)
            if args.log:
                routelog.record_shadow(args.log, st, request, state, sd)
        print(json.dumps(decision, indent=2))
        return 0 if decision["decision"] == "route" else 3

    if args.cmd == "mode":
        ev = routelog.set_mode(args.log, args.mode, load_table(args.table))
        print(f"seq {ev['seq']}: mode {args.mode}, table {ev['data']['table']}")
        return 0

    if args.cmd == "state":
        t = load_table(args.table)
        now = _as_of(args.as_of)
        state, notes = collect(t, load_sources(args.sources), now)
        print(json.dumps(state, indent=2))
        if args.notes:
            print(json.dumps(notes, indent=2), file=sys.stderr)
        return 0

    if args.cmd == "manifest":
        d = _read_json(args.decision)
        if d.get("decision") != "route":
            raise RouteError("INPUT_INVALID", "a manifest is only for a routed decision")
        print(json.dumps({
            "format": "hive-route.attempt/1", "attempt": d["attempt"], "task": d["task"],
            "route_id": d["route_id"], "route_pin": d["route_pin"], "model": d["model"],
            "effort": d.get("effort"), "harness": d["harness"], "cwd": args.cwd,
            "output": args.output, "started_at": format_time(datetime.now(UTC))}, indent=2))
        return 0

    if args.cmd == "observe":
        quals = load_sources(args.sources).get("qualifications") if args.sources else None
        since = datetime.now(UTC) - timedelta(days=args.since_days)
        events = observe(find_manifests(args.manifests, since), args.log, quals, args.codex_root)
        for ev in events:
            d = ev["data"]
            print(f"drift: {d['attempt']} on {d['route_id']}: pinned {d['pinned_model']}, "
                  f"reported {', '.join(d['observed_models'])}")
        return 4 if events else 0

    if args.cmd == "canary":
        sources = load_sources(args.sources)
        if args.canary_cmd == "lesson":
            e = set_lesson(args.route, args.lesson, sources, args.log)
            print(f"{args.route}: {e['status']}; lesson recorded")
            return 0
        e = qualify(load_table(args.table), args.route, args.suite, sources, args.log,
                    args.lesson, args.case, args.codex_root, args.max_usage)
        for r in e["results"]:
            print(f"  {r['case']:<20} {'pass' if r['passed'] else 'FAIL'}  {r['seconds']:>6}s  "
                  f"{', '.join(r['observed_models']) or '-'}")
        print(f"{args.route}: {e['status']} ({e['passed']}/{e['cases']}); {e['lesson']}")
        if e["stopped"]:
            return 6
        return 0 if e["status"] == "qualified" else 5

    if args.cmd == "gateway-config":
        import yaml
        t = load_table(args.table)
        print(f"# Generated by hive-route gateway-config from table {t.pin}; don't edit.")
        print(yaml.safe_dump(litellm_config(t, args.include_subscription), sort_keys=False),
              end="")
        for n in budget_notes(t):
            print(f"budget: {n}", file=sys.stderr)
        return 0

    if args.cmd == "record":
        from .record import emit_with_hive
        posted, errors = sync(args.log, lambda t, d, k: emit_with_hive(t, d, k, args.hive_cmd),
                              args.dry_run)
        for e in errors:
            print(f"hive-route: record: {e}", file=sys.stderr)
        if not args.dry_run:
            print(f"{posted} events recorded")
        return 7 if errors else 0

    if args.cmd == "whatif":
        r = whatif(args.log, load_table(args.table))
        if args.json:
            print(json.dumps(r, indent=2))
            return 0
        print(f"{r['decisions']} decisions, {r['changed']} would change")
        for k, n in r["tiers"].items():
            print(f"  tier  {k}: {n}")
        for c in r["changes"]:
            print(f"  {c['attempt']}: " + (c.get("error") or f"{c['before']}  ->  {c['after']}"))
        return 0

    if args.cmd == "report":
        usage = None
        if args.agentsview:
            import subprocess
            out = subprocess.run(["agentsview", "session", "list", "--json", "--include-one-shot",
                                  "--include-automated", "--include-children", "--limit", "500"],
                                 capture_output=True, text=True)
            if out.returncode != 0:
                raise RouteError("INPUT_INVALID", f"agentsview: {out.stderr.strip()[:300]}")
            usage = agentsview_usage(json.loads(out.stdout).get("sessions", []), args.agentsview)
        r = report(args.log, load_events(args.events), usage)
        if args.json:
            print(json.dumps(r, indent=2))
            return 0
        for g in r["groups"]:
            tok = f"  {g['output_tokens']:,} out-tokens" if g["output_tokens"] else ""
            print(f"{g['profile']}\n    {g['route']} ({g['tier']}, {g['mode']}): "
                  f"{g['attempts']} attempts, {g['accepted']}/{g['tasks']} tasks accepted, "
                  f"{g['failed_reviews']} failed reviews{tok}")
        for k, n in r["scorer"].items():
            print(f"scorer estimate {k}: {n}")
        for p in r["proposals"]:
            print(f"proposal: {p}")
        return 0

    if args.cmd == "replay":
        checked, problems = routelog.replay(args.log)
        for p in problems:
            print(p)
        print(f"{checked} decisions replayed, {len(problems)} problems")
        return 1 if problems else 0
    return 2
