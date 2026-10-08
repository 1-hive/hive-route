# SPDX-License-Identifier: GPL-3.0-or-later
"""The local decision log (ROUTING.md §9): one ``route.*`` event per line.

Until the record admits the ``route.`` family, this file is the router's log. Each
line is ``{"v", "seq", "type", "at", "data"}`` in canonical JSON; ``at`` is wall time
and informational only. Every decision carries its full inputs, and every table it
names is logged in full by ``route.table_pinned``, so the log replays on its own.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .canonical import UTC, dumps, format_time
from .decide import decide
from .errors import RouteError
from .table import Table

VERSION = 1


def read(path: str | Path) -> Iterator[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    raise RouteError("LOG_INVALID", f"{path}:{n}: {e}") from e
    except FileNotFoundError:
        return


class _Writer:
    def __init__(self, f, events: list[dict]) -> None:
        self._f, self.events = f, events

    def append(self, type_: str, data: dict) -> dict:
        from . import __version__
        ev = {"v": VERSION, "seq": len(self.events) + 1, "type": type_,
              "at": format_time(datetime.now(UTC)), "router": __version__, "data": data}
        self._f.write(dumps(ev) + "\n")
        self.events.append(ev)
        return ev

    def ensure_table(self, table: Table) -> None:
        if not any(e["type"] == "route.table_pinned" and e["data"]["pin"] == table.pin
                   for e in self.events):
            self.append("route.table_pinned", {"pin": table.pin, "table": table.data})


@contextmanager
def open_log(path: str | Path) -> Iterator[_Writer]:
    """Append under an exclusive lock, so concurrent callers get gapless ``seq``."""
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            events = list(read(path))
            w = _Writer(f, events)
            yield w
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def current_mode(path: str | Path) -> str | None:
    modes = [e["data"]["mode"] for e in read(path) if e["type"] == "route.mode_set"]
    return modes[-1] if modes else None


def record_decision(path: str | Path, table: Table, request: dict, state: dict, mode: str,
                    decision: dict) -> dict:
    kind = "route.decided" if decision["decision"] == "route" else "route.waiting"
    with open_log(path) as w:
        w.ensure_table(table)
        return w.append(kind, {"table": table.pin, "mode": mode, "request": request,
                               "state": state, "decision": decision})


def record_shadow(path: str | Path, table: Table, request: dict, state: dict,
                  decision: dict) -> dict:
    """A shadow table's decision (§6): logged and replayed, never acted on."""
    with open_log(path) as w:
        w.ensure_table(table)
        return w.append("route.shadow_decided", {"table": table.pin, "mode": "live",
                                                 "request": request, "state": state,
                                                 "decision": decision})


def record_scored(path: str | Path, table: Table, scored: dict) -> dict:
    """The scorer's estimates for one attempt (§4.3). Not replayed: a model's output."""
    with open_log(path) as w:
        w.ensure_table(table)
        return w.append("route.scored", {"table": table.pin, **scored})


def record_read(path: str | Path, table: Table, read: dict) -> dict:
    """The reader's reading for one attempt (§4.8). Not replayed: a model's output. A live,
    qualified reading also enters the attempt's request, where it is replayed."""
    with open_log(path) as w:
        w.ensure_table(table)
        return w.append("route.read", {"table": table.pin, **read})


def set_mode(path: str | Path, mode: str, table: Table) -> dict:
    with open_log(path) as w:
        w.ensure_table(table)
        return w.append("route.mode_set", {"mode": mode, "table": table.pin})


def replay(path: str | Path) -> tuple[int, list[str]]:
    """Recompute every logged decision. Returns (decisions checked, mismatches)."""
    tables: dict[str, Table] = {}
    problems: list[str] = []
    checked = 0
    for ev in read(path):
        where = f"seq {ev.get('seq')}"
        if ev["type"] == "route.table_pinned":
            t = Table.from_data(ev["data"]["table"])
            if t.pin != ev["data"]["pin"]:
                problems.append(f"{where}: table content gives {t.pin}, logged {ev['data']['pin']}")
            tables[ev["data"]["pin"]] = t
        elif ev["type"] == "route.bound":
            from .openclaw import replay_bound
            checked += 1
            problem = replay_bound(ev, tables)
            if problem:
                problems.append(f"{where}: binding: {problem}")
        elif ev["type"] in ("route.decided", "route.waiting", "route.shadow_decided"):
            d = ev["data"]
            table = tables.get(d["table"])
            if table is None:
                problems.append(f"{where}: table {d['table']} not pinned earlier in the log")
                continue
            checked += 1
            again = decide(d["request"], table, d["state"], d["mode"])
            if dumps(again) != dumps(d["decision"]):
                problems.append(f"{where}: task {d['request']['task']} attempt "
                                f"{d['request']['attempt']}: replay differs")
    return checked, problems
