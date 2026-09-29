# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ROOT

from hiveroute.canonical import format_time
from hiveroute.cli import main
from hiveroute.decide import decide
from hiveroute.errors import RouteError
from hiveroute.table import load_table
from hiveroute.usage import collect, load_sources, window_name

TABLE = load_table(ROOT / "examples" / "1-hive.yaml")
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def ts(dt: datetime) -> int:
    return int(dt.timestamp())


def claude_event(five: float, week: float, status: str = "allowed", base: datetime = NOW) -> dict:
    info = {"status": status, "resetsAt": ts(base + timedelta(hours=2)),
            "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": five,
                                             "resetsAt": ts(base + timedelta(hours=2))},
                               "seven_day": {"utilization": week,
                                             "resetsAt": ts(base + timedelta(days=3))}}}
    return {"type": "rate_limit_event", "rate_limit_info": info, "session_id": "s"}


def codex_event(at: datetime, primary: float, secondary: float) -> dict:
    return {"timestamp": format_time(at), "type": "event_msg",
            "payload": {"type": "token_count", "info": {}, "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": primary, "window_minutes": 300,
                            "resets_at": ts(NOW + timedelta(hours=1))},
                "secondary": {"used_percent": secondary, "window_minutes": 10080,
                              "resets_at": ts(NOW + timedelta(days=5))}}}}


def write_jsonl(path, events, mtime: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events) + '{"type": "partial')
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))


def sources(tmp_path, **extra) -> dict:
    s = {"format": "hive-route.sources/1", "fresh_minutes": 30, "pools": {
        "claude-plan": [{"reader": "claude-stream", "paths": [str(tmp_path / "w" / "*.jsonl")]}],
        "chatgpt-plan": [{"reader": "codex-sessions", "paths": [str(tmp_path / "codex")]}]}}
    s.update(extra)
    return s


def test_window_names():
    assert (window_name(300), window_name(10080), window_name(90)) == ("5h", "7d", "90m")


def test_claude_stream_latest_file_and_event(tmp_path):
    write_jsonl(tmp_path / "w" / "old.jsonl", [claude_event(0.9, 0.5)], NOW - timedelta(hours=1))
    write_jsonl(tmp_path / "w" / "new.jsonl", [claude_event(0.2, 0.4), claude_event(0.3, 0.41)],
                NOW - timedelta(minutes=5))
    state, notes = collect(TABLE, sources(tmp_path), NOW)
    u = state["pools"]["claude-plan"]["usage"]
    assert u["window/5h"] == {"used": 0.3, "basis": "measured",
                              "resets_at": format_time(NOW + timedelta(hours=2))}
    assert u["window/7d"]["used"] == 0.41
    assert notes["claude-plan"]["source"].endswith("new.jsonl")
    assert "limited_until" not in state["pools"]["claude-plan"]


def test_stale_reading_is_estimated_and_reset_window_unknown(tmp_path):
    write_jsonl(tmp_path / "w" / "a.jsonl", [claude_event(0.5, 0.2)], NOW - timedelta(hours=1))
    later = NOW + timedelta(hours=3)  # past the 5h window's reset
    state, _ = collect(TABLE, sources(tmp_path), later)
    u = state["pools"]["claude-plan"]["usage"]
    assert u["window/5h"] == {"used": None, "basis": "unknown"}
    assert u["window/7d"]["basis"] == "estimated"


def test_rejected_sets_limited_until_and_fixed_mode_waits(tmp_path):
    write_jsonl(tmp_path / "w" / "a.jsonl", [claude_event(1, 0.34, "rejected")], NOW)
    state, _ = collect(TABLE, sources(tmp_path), NOW)
    until = format_time(NOW + timedelta(hours=2))
    assert state["pools"]["claude-plan"]["limited_until"] == until
    d = decide({"task": "t", "attempt": "t.w0", "facts": {"kind": "work"}}, TABLE, state, "fixed")
    assert (d["decision"], d["wait_until"]) == ("wait", until)


def test_codex_sessions_latest_timestamp_wins(tmp_path):
    write_jsonl(tmp_path / "codex" / "2026" / "a.jsonl",
                [codex_event(NOW - timedelta(minutes=50), 10, 3),
                 codex_event(NOW - timedelta(minutes=10), 100, 5)], NOW - timedelta(minutes=9))
    write_jsonl(tmp_path / "codex" / "2026" / "b.jsonl",
                [codex_event(NOW - timedelta(minutes=40), 20, 4)], NOW - timedelta(minutes=8))
    state, notes = collect(TABLE, sources(tmp_path), NOW)
    p = state["pools"]["chatgpt-plan"]
    assert p["usage"]["window/5h"]["used"] == 1.0 and p["usage"]["window/7d"]["used"] == 0.05
    assert p["limited_until"] == format_time(NOW + timedelta(hours=1))
    assert notes["chatgpt-plan"]["source"].endswith("a.jsonl")


def test_no_readings_means_unknown_not_zero(tmp_path):
    state, notes = collect(TABLE, sources(tmp_path), NOW)
    assert state["pools"]["claude-plan"]["usage"]["window/5h"] == {"used": None,
                                                                   "basis": "unknown"}
    assert notes["claude-plan"] == {"source": None}


def test_qualifications_only_for_table_routes(tmp_path):
    q = tmp_path / "q.json"
    q.write_text(json.dumps({
        "opus-plan": {"status": "qualified", "route_pin": TABLE.route_pin("opus-plan"),
                      "lesson": "fine"},
        "gone": {"status": "qualified", "route_pin": "sha256:" + "0" * 64}}))
    state, _ = collect(TABLE, sources(tmp_path, qualifications=str(q)), NOW)
    assert state["routes"] == {"opus-plan": {"status": "qualified",
                                             "route_pin": TABLE.route_pin("opus-plan")}}


def test_sources_validation(tmp_path):
    bad = tmp_path / "s.yaml"
    bad.write_text("format: hive-route.sources/1\npools: {claude-plan: [{reader: nope, paths: [x]}]}")
    with pytest.raises(RouteError, match="SOURCES_INVALID"):
        load_sources(bad)
    unknown = tmp_path / "u.yaml"
    unknown.write_text("format: hive-route.sources/1\n"
                       "pools: {other: [{reader: claude-stream, paths: [x]}]}")
    with pytest.raises(RouteError, match="not a pool of the table"):
        collect(TABLE, load_sources(unknown), NOW)


def test_cli_state_and_decide_with_sources(tmp_path, capsys):
    now = datetime.now(UTC)
    write_jsonl(tmp_path / "w" / "a.jsonl", [claude_event(0.25, 0.1, base=now)], now)
    src = tmp_path / "sources.yaml"
    src.write_text(json.dumps(sources(tmp_path)))
    table = str(ROOT / "examples" / "1-hive.yaml")
    assert main(["state", table, str(src)]) == 0
    assert json.loads(capsys.readouterr().out)["pools"]["claude-plan"]["usage"][
        "window/5h"]["used"] == 0.25
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"task": "t", "attempt": "t.w0", "facts": {"kind": "work"}}))
    log = tmp_path / "log.jsonl"
    assert main(["decide", table, str(req), "--mode", "fixed", "--sources", str(src),
                 "--log", str(log)]) == 0
    capsys.readouterr()
    assert main(["replay", str(log)]) == 0
