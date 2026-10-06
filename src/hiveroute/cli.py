# SPDX-License-Identifier: GPL-3.0-or-later
"""``hive-route``: check tables, make decisions, keep and replay the decision log."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from . import log as routelog
from .canary import accept, qualify, set_lesson
from .canonical import UTC, format_time, parse_time
from .errors import RouteError
from .evaluate import agentsview_usage, load_events, report, shadow, whatif
from .gateway import attempt_budget, budget_notes, budgets, litellm_config
from .observe import CODEX_ROOT, find_manifests, observe
from .record import sync
from .serve import run_decision
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
    cacc = csub.add_parser("accept", help="accept a candidate below the suite's bar (operator)")
    cacc.add_argument("table")
    cacc.add_argument("route")
    cacc.add_argument("--by", required=True, help="who accepts it")
    cacc.add_argument("--reason", required=True)
    cacc.add_argument("--sources", required=True)
    cacc.add_argument("--log", required=True)
    cl = csub.add_parser("lesson", help="record an operator's lesson for a route")
    cl.add_argument("route")
    cl.add_argument("lesson")
    cl.add_argument("--sources", required=True)
    cl.add_argument("--log", required=True)

    g = sub.add_parser("gateway-config", help="generate the LiteLLM config from the table (§8)")
    g.add_argument("table")
    g.add_argument("--include-subscription", action="store_true",
                   help="also subscription routes (only where the provider's terms allow it)")
    g.add_argument("--budgets", action="store_true",
                   help="print the metered pools' gateway budgets (JSON) instead of the config")
    g.add_argument("--no-database", action="store_true",
                   help="no database_url: master key only, no per-actor keys or spend logs")

    oc = sub.add_parser("openclaw-config", help="bind long-running OpenClaw agents: their "
                        "models as config patches (§9.3)")
    oc.add_argument("table")
    oc.add_argument("bindings", help="bindings file (schemas/bindings-v1.schema.json)")
    osrc = oc.add_mutually_exclusive_group()
    osrc.add_argument("--state", help="state JSON file")
    osrc.add_argument("--sources", help="build the state now from this sources file (§7)")
    oc.add_argument("--mode", choices=["live", "fixed"],
                    help="default: the log's latest route.mode_set, else live")
    oc.add_argument("--log", help="log the binding as route.bound when it changed")
    oc.add_argument("--write-dir", help="also write each config's patch to "
                    "DIR/<config name>.patch.json")
    oc.add_argument("--per-binding", action="store_true", help="with --write-dir, write one "
                    "patch per binding instead, DIR/<binding>.patch.json: for a controller that "
                    "applies changes per agent when agents share a config file")
    oc.add_argument("--check", action="store_true", help="check the patches against the "
                    "config files they're for: say whether each binding matches what the agent "
                    "runs now, and exit 8 on a model the config can't use (allow list, providers)")

    oo = sub.add_parser("openclaw-observe", help="check OpenClaw agents' turns since the latest "
                        "binding for drift (§9.3)")
    oo.add_argument("log")
    oo.add_argument("--sources", help="sources file naming the qualifications file to update")

    sv = sub.add_parser("serve", help="the route service: POST /route at each episode of an "
                        "agent with its own loop (§9.2)")
    sv.add_argument("table")
    sv.add_argument("agents", help="agents file (schemas/agents-v1.schema.json)")
    sv.add_argument("--log", required=True, help="the decision log (route.decided per episode)")
    sv.add_argument("--state-dir", required=True, help="where the service keeps each agent's "
                    "episodes; outside the agents' reach")
    sv.add_argument("--sources", help="sources file: pool usage and qualifications (§7)")
    sv.add_argument("--state-ttl", type=float, default=30.0,
                    help="seconds a collected state is reused (default 30)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8480)

    pr = sub.add_parser("probe", help="run the sources file's usage probes that are due (§7)")
    pr.add_argument("sources")
    pr.add_argument("--pool", action="append", help="only this pool's probe (repeatable)")
    pr.add_argument("--force", action="store_true", help="run even if not due")

    rc = sub.add_parser("record", help="post new log entries' summaries to the hive record (A1)")
    rc.add_argument("log")
    rc.add_argument("--hive-cmd", default="hive", help="the hive CLI (identity from HIVE_* env)")
    rc.add_argument("--dry-run", action="store_true", help="print the events instead")

    ab = sub.add_parser("attempt-budget", help="the gateway tag budget that caps one attempt on "
                        "a metered route (JSON), or nothing")
    ab.add_argument("table")
    ab.add_argument("decision", help="decision JSON file, or - for stdin")

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
        text = None
        if args.task_text:
            try:
                text = Path(args.task_text).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise RouteError("INPUT_INVALID", f"--task-text: {e}") from e
        decision = run_decision(t, request, state, mode, args.log, text)
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
        src = load_sources(args.sources) if args.sources else {}
        aux = {h: set(c.get("aux_models", ())) for h, c in src.get("harnesses", {}).items()}
        since = datetime.now(UTC) - timedelta(days=args.since_days)
        events = observe(find_manifests(args.manifests, since), args.log,
                         src.get("qualifications"), args.codex_root, aux)
        for ev in events:
            d = ev["data"]
            print(f"drift: {d['attempt']} on {d['route_id']}: pinned {d['pinned_model']}, "
                  f"reported {', '.join(d['observed_models'])}")
        return 4 if events else 0

    if args.cmd == "canary":
        sources = load_sources(args.sources)
        if args.canary_cmd == "accept":
            e = accept(load_table(args.table), args.route, args.by, args.reason, sources, args.log)
            print(f"{args.route}: qualified at {e['passed']}/{e['cases']}, accepted by {args.by}")
            return 0
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
        if args.budgets:
            print(json.dumps(budgets(t), indent=2))
            return 0
        print(f"# Generated by hive-route gateway-config from table {t.pin}; don't edit.")
        print(yaml.safe_dump(litellm_config(t, args.include_subscription, not args.no_database),
                             sort_keys=False),
              end="")
        for n in budget_notes(t):
            print(f"budget: {n}", file=sys.stderr)
        return 0

    if args.cmd == "openclaw-config":
        from . import openclaw
        t = load_table(args.table)
        b = openclaw.load_bindings(args.bindings)
        if args.state:
            state = _read_json(args.state)
        elif args.sources:
            state, _ = collect(t, load_sources(args.sources), datetime.now(UTC))
        else:
            state = {"as_of": format_time(datetime.now(UTC))}
        mode = args.mode or (routelog.current_mode(args.log) if args.log else None)
        if mode is None:
            mode = "live"
            print("hive-route: no --mode and none logged: rendering live mode (with nothing "
                  "qualified, no route); --mode fixed shows the baseline", file=sys.stderr)
        out, changed = openclaw.bind(t, b, state, mode, args.log)
        print(json.dumps({**out, "changed": changed}, indent=2))
        if args.log and not changed:
            print("hive-route: binding unchanged since the latest route.bound", file=sys.stderr)
        if args.write_dir:
            d = Path(args.write_dir)
            d.mkdir(parents=True, exist_ok=True)
            files = ({d / f"{n}.patch.json": p for n, p in out["binding_patches"].items()}
                     if args.per_binding else
                     {d / out["patch_files"][c]: p for c, p in out["patches"].items()})
            for f, patch in files.items():
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps(patch, indent=2) + "\n")
                tmp.replace(f)
        problems = []
        for name, res in out["results"].items():
            for n in res["notes"]:
                print(f"{name}: {n}", file=sys.stderr)
            if args.check:
                try:
                    cfg = json.loads(Path(res["config"]).read_text())
                except (OSError, json.JSONDecodeError) as e:
                    problems.append(f"{name}: {res['config']}: {e}")
                    continue
                problems += [f"{name}: {p}" for p in openclaw.check_config(cfg, res)]
                diff = openclaw.compare_config(cfg, res)
                print(f"check: {name}: " + ("matches the config" if not diff
                                            else "would change " + "; ".join(diff)),
                      file=sys.stderr)
        for p in problems:
            print(f"check: {p}", file=sys.stderr)
        return 8 if problems else 0

    if args.cmd == "serve":
        from .serve import Service, load_agents, make_server
        t = load_table(args.table)
        svc = Service(t, load_agents(args.agents),
                      load_sources(args.sources) if args.sources else None,
                      args.log, args.state_dir, args.state_ttl)
        srv = make_server(svc, args.host, args.port)
        print(f"hive-route: serving on {args.host}:{srv.server_address[1]}, table {t.pin}, "
              f"mode {svc.mode()}", file=sys.stderr, flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            srv.serve_forever()
        return 0

    if args.cmd == "openclaw-observe":
        from . import openclaw
        src = load_sources(args.sources) if args.sources else {}
        warnings: list[str] = []
        events = openclaw.observe(args.log, src.get("qualifications"), warnings)
        for w in warnings:
            print(f"hive-route: couldn't check: {w}", file=sys.stderr)
        for ev in events:
            d = ev["data"]
            print(f"drift: {d['binding']} on {d['route_id']}: bound {d['pinned_model']}, "
                  f"reported {', '.join(d['observed_models'])}")
        return 4 if events else 10 if warnings else 0

    if args.cmd == "probe":
        from .probe import probe_all
        res = probe_all(load_sources(args.sources), datetime.now(UTC), args.force, args.pool)
        for pid, r in res.items():
            print(f"{pid}: " + (r["skipped"] if "skipped" in r
                                else f"ran ({r['why']}): exit {r['exit']}, {r['output']}"))
        return 0 if all(r.get("exit", 0) == 0 for r in res.values()) else 9

    if args.cmd == "record":
        from .record import emit_with_hive
        posted, errors = sync(args.log, lambda t, d, k: emit_with_hive(t, d, k, args.hive_cmd),
                              args.dry_run)
        for e in errors:
            print(f"hive-route: record: {e}", file=sys.stderr)
        if not args.dry_run:
            print(f"{posted} events recorded")
        return 7 if any("skipped" not in e for e in errors) else 0

    if args.cmd == "attempt-budget":
        b = attempt_budget(load_table(args.table), _read_json(args.decision))
        if b:
            print(json.dumps(b))
        return 0

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
            print(f"{g['profile']}\n    {g['route']} ({g['tier']}, {g['mode']}, {g['runtime']}): "
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
