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


def env_value(key_env: str | None, env_file: str | None) -> str | None:
    """A secret from the environment, or from a KEY=value file (never logged)."""
    if not key_env:
        return None
    if key_env in os.environ:
        return os.environ[key_env]
    if env_file:
        try:
            for line in Path(os.path.expanduser(env_file)).read_text().splitlines():
                k, _, v = line.partition("=")
                if k.strip() == key_env:
                    return v.strip()
        except OSError:
            return None
    return None


def check_health(spec: dict, now: datetime) -> Reading:
    """``http-health``: a pool is down while none of its URLs answers 2xx (each is tried
    twice). A down pool is
    at a limit for ``down_minutes`` (default 5), so the router picks another route in the
    tier (or waits) instead of starting an attempt that fails as an outage."""
    import urllib.error
    import urllib.request
    key = env_value(spec.get("key_env"), spec.get("env_file"))
    errors = []
    # Each URL is tried twice, a second apart: one dropped probe isn't an outage.
    for url in [u for u in spec["paths"] for _ in range(2)]:
        if errors:
            import time
            time.sleep(1)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
        try:
            with urllib.request.urlopen(req, timeout=spec.get("timeout_seconds", 5)) as r:
                if 200 <= r.status < 300:
                    return Reading(observed_at=now, source=url)
                errors.append(f"{url}: HTTP {r.status}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            errors.append(f"{url}: {e}")
    return Reading(observed_at=now, source="; ".join(errors)[:300],
                   limited_until=now + timedelta(minutes=spec.get("down_minutes", 5)))


def period_bounds(per: str, now: datetime) -> tuple[datetime, datetime] | None:
    """The current calendar period (UTC) for a metered limit: its start and its reset."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if per == "day":
        return day, day + timedelta(days=1)
    if per == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=7)
    if per == "month":
        start = day.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt
    return None


GATEWAY_DURATIONS = {"day": "1d", "week": "1w", "month": "1mo"}


def _gateway_get(base: str, path: str, key: str | None, timeout: float,
                 body: dict | None = None) -> object:
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def gateway_period(base: str, key: str | None, tag: str, per: str, now: datetime,
                   timeout: float) -> tuple[datetime, datetime] | None:
    """The period the gateway's own budget on ``tag`` enforces: (start, reset), from the
    budget's ``budget_reset_at`` and duration. None when there is no such budget."""
    import urllib.error
    try:
        info = _gateway_get(base, "/tag/info", key, timeout, {"names": [tag]})
    except (urllib.error.URLError, OSError, ValueError):
        return None
    bt = ((info or {}).get(tag) or {}).get("litellm_budget_table") or {}
    if not bt.get("budget_reset_at"):
        return None
    reset = parse_time(bt["budget_reset_at"])
    if per == "month":
        prev_month = (reset.replace(day=1) - timedelta(days=1))
        start = reset.replace(year=prev_month.year, month=prev_month.month,
                              day=min(reset.day, prev_month.day))
    else:
        start = reset - timedelta(days=1 if per == "day" else 7)
    return (start, reset) if start <= now < reset + timedelta(minutes=5) else None


def read_gateway_spend(spec: dict, pid: str, limits: list[dict], now: datetime) -> Reading:
    """``gateway-spend``: a metered pool's spend in each period its limits name (day, week,
    month), from the gateway's spend logs by the tag ``pool:<pool>`` (LiteLLM
    ``/global/spend/tags``; the gateway's price-map figure, not an invoice). The period is
    the one the gateway's own budget on ``pool:<pool>:<per>`` enforces (``gateway-config
    --budgets``), so router and gateway agree on what remains; without such a budget, the
    calendar period (UTC). A period the gateway can't report stays unknown, and an unknown
    budget blocks the pool."""
    import urllib.error
    import urllib.parse
    key = env_value(spec.get("key_env"), spec.get("env_file"))
    base = spec["paths"][0]
    timeout = spec.get("timeout_seconds", 10)
    r = Reading(observed_at=now, source=base)
    for limit in limits:
        if "usd" not in limit or limit["per"] not in GATEWAY_DURATIONS:
            continue
        per = limit["per"]
        b = (gateway_period(base, key, f"pool:{pid}:{per}", per, now, timeout)
             or period_bounds(per, now))
        start, reset = b
        q = urllib.parse.urlencode({"start_date": start.strftime("%Y-%m-%d"),
                                    "end_date": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
                                    "tags": f"pool:{pid}"})
        try:
            rows = _gateway_get(base, "/global/spend/tags?" + q, key, timeout)
            rows = rows.get("spend_per_tag", []) if isinstance(rows, dict) else []
        except (urllib.error.URLError, OSError, ValueError):
            continue
        spend = sum(float(t.get("spend") or 0) for t in rows if t.get("name") == f"pool:{pid}")
        r.windows[f"usd/{per}"] = (spend, reset)
    return r


def merge_readings(readings: list[Reading]) -> Reading | None:
    """One pool's readings from several sources, combined: every source's limits (the
    newest reading wins for a limit two report), the latest limited_until, the newest
    observation time."""
    if not readings:
        return None
    rs = sorted(readings, key=lambda r: r.observed_at)
    out = Reading(observed_at=rs[-1].observed_at, source="; ".join(r.source for r in rs)[:300])
    for r in rs:
        out.windows.update(r.windows)
        if r.limited_until and (out.limited_until is None or r.limited_until > out.limited_until):
            out.limited_until = r.limited_until
    return out


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
        if not (key.startswith("window/") or limit.get("per") in ("day", "week", "month")):
            continue  # per-attempt and per-task limits come from the request, not the state
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
        readings = [check_health(s, now) if s["reader"] == "http-health"
                    else read_gateway_spend(s, pid, table.pools[pid]["limits"], now)
                    if s["reader"] == "gateway-spend"
                    else READERS[s["reader"]](s["paths"], since) for s in specs]
        latest = merge_readings([r for r in readings if r is not None])
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
