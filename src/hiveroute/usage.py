# SPDX-License-Identifier: GPL-3.0-or-later
"""Usage state (ROUTING.md §7): what each pool has used, read from where it is reported.

Subscription providers report the share of each usage window that has been used, and
the harnesses write those reports into their session output:

- ``claude-stream``: Claude Code's ``--output-format stream-json`` output (what a
  launcher captures from ``claude -p``) carries ``rate_limit_event`` lines with the
  utilization of every window, their reset times, and ``status: rejected`` at a limit.
- ``codex-sessions``: Codex's session files (``~/.codex/sessions``) carry
  ``token_count`` events with ``rate_limits``: used percent, window length, reset time.

A reading is ``measured`` while it is fresh, ``estimated`` once it is older than the
sources file's ``fresh_minutes`` (usage can only have grown since), and ``unknown``
once its window has reset. An unknown figure is never replaced with zero.

Route qualifications come from the file the canary runner keeps (§3).
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from .canonical import format_time, parse_time
from .errors import RouteError
from .table import Table, limit_key, schema_error

CLAUDE_WINDOWS = {"five_hour": "5h", "seven_day": "7d"}


@dataclass
class Reading:
    """One provider report of a pool's usage windows."""

    observed_at: datetime
    source: str
    windows: dict[str, tuple[float, datetime]] = field(default_factory=dict)  # key: (used, resets)
    limited_until: datetime | None = None


def window_name(minutes: int) -> str:
    """``300`` -> ``5h``, ``10080`` -> ``7d``: the table's names for usage windows."""
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _epoch(t: float) -> datetime:
    return datetime.fromtimestamp(t, UTC)


def _files(patterns: list[str], since: datetime) -> list[Path]:
    """Files matching the patterns (a directory means every ``*.jsonl`` under it),
    modified since ``since``, newest first."""
    found: set[str] = set()
    for pat in patterns:
        p = os.path.expanduser(pat)
        if os.path.isdir(p):
            found.update(glob.glob(os.path.join(p, "**", "*.jsonl"), recursive=True))
        else:
            found.update(glob.glob(p, recursive=True))
    out = []
    for f in found:
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if _epoch(m) >= since:
            out.append((m, Path(f)))
    return [p for _, p in sorted(out, reverse=True)]


def _lines_with(path: Path, needle: str) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if needle in line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # a line still being written
    except OSError:
        pass
    return out


def read_claude_stream(patterns: list[str], since: datetime) -> Reading | None:
    """The latest ``rate_limit_event`` across Claude Code stream-json files.

    The events carry no timestamp, so a reading is dated by its file's modification
    time: the file's last event was written at or before it.
    """
    for path in _files(patterns, since):
        events = [e for e in _lines_with(path, '"rate_limit_event"')
                  if e.get("type") == "rate_limit_event"]
        if not events:
            continue
        info = events[-1].get("rate_limit_info") or {}
        r = Reading(observed_at=_epoch(path.stat().st_mtime), source=str(path))
        for name, w in (info.get("unifiedWindows") or {}).items():
            if name in CLAUDE_WINDOWS and "utilization" in w and "resetsAt" in w:
                r.windows[f"window/{CLAUDE_WINDOWS[name]}"] = (float(w["utilization"]),
                                                               _epoch(w["resetsAt"]))
        if info.get("status") == "rejected" and "resetsAt" in info:
            r.limited_until = _epoch(info["resetsAt"])
        return r
    return None


def read_codex_sessions(patterns: list[str], since: datetime, files: int = 20) -> Reading | None:
    """The latest ``rate_limits`` report across the most recent Codex session files."""
    best: Reading | None = None
    for path in _files(patterns, since)[:files]:
        events = [e for e in _lines_with(path, '"rate_limits"')
                  if (e.get("payload") or {}).get("type") == "token_count"
                  and (e["payload"].get("rate_limits") or {}).get("primary")]
        if not events:
            continue
        e = events[-1]
        t = parse_time(e["timestamp"])
        if best is not None and t <= best.observed_at:
            continue
        r = Reading(observed_at=t, source=str(path))
        for slot in ("primary", "secondary"):
            w = e["payload"]["rate_limits"].get(slot)
            if w and w.get("window_minutes") and w.get("resets_at") is not None:
                resets = _epoch(w["resets_at"])
                used = float(w["used_percent"]) / 100
                r.windows[f"window/{window_name(int(w['window_minutes']))}"] = (used, resets)
                if used >= 1 and (r.limited_until is None or resets > r.limited_until):
                    r.limited_until = resets
        best = r
    return best


READERS = {"claude-stream": read_claude_stream, "codex-sessions": read_codex_sessions}


def load_sources(path: str | Path) -> dict:
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise RouteError("SOURCES_INVALID", f"{path}: {e}") from e
    err = schema_error("sources-v1.schema.json", raw)
    if err:
        raise RouteError("SOURCES_INVALID", err)
    return raw


def load_qualifications(path: str | Path | None) -> dict:
    """``{route_id: {status, route_pin, ...}}`` as the canary runner writes it."""
    if not path:
        return {}
    p = Path(os.path.expanduser(str(path)))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RouteError("SOURCES_INVALID", f"{p}: {e}") from e


def pool_state(table: Table, pid: str, reading: Reading | None, now: datetime,
               fresh: timedelta) -> dict:
    """The state entry for one pool: every window limit the table declares, with its basis."""
    usage = {}
    for limit in table.pools[pid]["limits"]:
        key = limit_key(limit)
        if not key.startswith("window/"):
            continue
        w = reading.windows.get(key) if reading else None
        if w is None or w[1] <= now:
            usage[key] = {"used": None, "basis": "unknown"}
            continue
        basis = "measured" if now - reading.observed_at <= fresh else "estimated"
        usage[key] = {"used": round(w[0], 4), "basis": basis, "resets_at": format_time(w[1])}
    st: dict = {"usage": usage} if usage else {}
    if reading and reading.limited_until and reading.limited_until > now:
        st["limited_until"] = format_time(reading.limited_until)
    return st


def collect(table: Table, sources: dict, now: datetime) -> tuple[dict, dict]:
    """Build the state for ``decide()``, and a note per pool saying where it came from."""
    fresh = timedelta(minutes=sources.get("fresh_minutes", 30))
    since = now - timedelta(days=sources.get("max_age_days", 8))
    pools, notes = {}, {}
    for pid, specs in sources.get("pools", {}).items():
        if pid not in table.pools:
            raise RouteError("SOURCES_INVALID", f"pools/{pid}: not a pool of the table")
        readings = [READERS[s["reader"]](s["paths"], since) for s in specs]
        readings = [r for r in readings if r is not None]
        latest = max(readings, key=lambda r: r.observed_at, default=None)
        entry = pool_state(table, pid, latest, now, fresh)
        if entry:
            pools[pid] = entry
        notes[pid] = ({"source": latest.source, "observed_at": format_time(latest.observed_at)}
                      if latest else {"source": None})
    quals = load_qualifications(sources.get("qualifications"))
    routes = {rid: {"status": q["status"], "route_pin": q["route_pin"]}
              for rid, q in quals.items() if rid in table.routes}
    state: dict = {"as_of": format_time(now)}
    if pools:
        state["pools"] = pools
    if routes:
        state["routes"] = routes
    return state, notes
