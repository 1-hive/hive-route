# SPDX-License-Identifier: GPL-3.0-or-later
"""``hive-route``: check tables, make decisions, keep and replay the decision log."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from . import log as routelog
from .canonical import format_time
from .decide import decide
from .errors import RouteError
from .table import load_table


def _read_json(arg: str) -> dict:
    try:
        text = sys.stdin.read() if arg == "-" else Path(arg).read_text(encoding="utf-8")
        obj = json.loads(text)
    except (OSError, json.JSONDecodeError) as e:
        raise RouteError("INPUT_INVALID", f"{arg}: {e}") from e
    if not isinstance(obj, dict):
        raise RouteError("INPUT_INVALID", f"{arg}: not a JSON object")
    return obj


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
    d.add_argument("--state", help="state JSON file (default: no usage known, nothing qualified)")
    d.add_argument("--mode", choices=["live", "fixed"],
                   help="default: the log's latest route.mode_set, else live")
    d.add_argument("--log", help="append the decision to this JSONL log")

    m = sub.add_parser("mode", help="record a mode change in the log")
    m.add_argument("mode", choices=["live", "fixed"])
    m.add_argument("table")
    m.add_argument("--log", required=True)

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
        now = {"as_of": format_time(datetime.now(UTC))}
        state = _read_json(args.state) if args.state else now
        mode = args.mode or (routelog.current_mode(args.log) if args.log else None) or "live"
        decision = decide(request, t, state, mode)
        if args.log:
            routelog.record_decision(args.log, t, request, state, mode, decision)
        print(json.dumps(decision, indent=2))
        return 0 if decision["decision"] == "route" else 3

    if args.cmd == "mode":
        ev = routelog.set_mode(args.log, args.mode, load_table(args.table))
        print(f"seq {ev['seq']}: mode {args.mode}, table {ev['data']['table']}")
        return 0

    if args.cmd == "replay":
        checked, problems = routelog.replay(args.log)
        for p in problems:
            print(p)
        print(f"{checked} decisions replayed, {len(problems)} problems")
        return 1 if problems else 0
    return 2
