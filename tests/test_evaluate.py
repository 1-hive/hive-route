# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json

from conftest import FIXTURES, ROOT

from hiveroute.cli import main
from hiveroute.evaluate import agentsview_usage, task_outcomes
from hiveroute.log import read
from hiveroute.table import load_table

TABLE_PATH = str(ROOT / "examples" / "1-hive.yaml")
V1 = str(FIXTURES / "tables" / "1-hive-v1.yaml")
EXPLICIT = {"specification": "explicit", "verification": "independent", "scope": "few",
            "consequence": "reversible", "leverage": 0}


def quals_state(tmp_path) -> str:
    t = load_table(TABLE_PATH)
    st = tmp_path / "state.json"
    st.write_text(json.dumps({"as_of": "2026-09-29T12:00:00Z", "routes": {
        rid: {"status": "qualified", "route_pin": t.route_pin(rid)} for rid in t.routes}}))
    return str(st)


def decide_into(tmp_path, log, task, facts, *extra) -> None:
    req = tmp_path / f"{task}.json"
    req.write_text(json.dumps({"task": task, "attempt": f"{task}.worker.0",
                               "facts": {"kind": "work", **facts}}))
    assert main(["decide", TABLE_PATH, str(req), "--mode", "fixed", "--state",
                 quals_state(tmp_path), "--log", str(log), *extra]) == 0


def test_shadow_table_logged_and_replayed(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    decide_into(tmp_path, log, "t1", EXPLICIT, "--shadow-table", TABLE_PATH)
    capsys.readouterr()
    [sh] = [e["data"] for e in read(log) if e["type"] == "route.shadow_decided"]
    assert sh["mode"] == "live" and sh["decision"]["tier"] == "light"
    assert main(["replay", str(log)]) == 0
    assert "2 decisions replayed, 0 problems" in capsys.readouterr().out


def test_whatif_under_another_table(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    decide_into(tmp_path, log, "t1", EXPLICIT)
    decide_into(tmp_path, log, "t2", {})
    capsys.readouterr()
    assert main(["whatif", str(log), TABLE_PATH, "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    # t1 goes light in live mode; t2 stays strong, and on opus-plan (no usage known: listed order)
    assert r["decisions"] == 2 and r["changed"] == 1
    assert r["tiers"] == {"light -> light": 1, "strong -> strong": 1}
    assert main(["whatif", str(log), V1]) == 0  # the v1 table: a readable summary
    assert "2 decisions" in capsys.readouterr().out


def test_report_joins_outcomes_and_usage(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    for t in ("a", "b", "c"):
        decide_into(tmp_path, log, t, {})
    events = tmp_path / "events.jsonl"
    evs = []
    for t in ("a", "b", "c"):
        evs += [{"type": "task.result_posted", "task": t},
                {"type": "review.recorded", "task": t, "data": {"verdict": "passed"}},
                {"type": "task.closed", "task": t}]
    events.write_text("".join(json.dumps(e) + "\n" for e in evs))
    capsys.readouterr()
    assert main(["report", str(log), "--events", str(events), "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    [g] = r["groups"]
    assert (g["route"], g["attempts"], g["tasks"], g["accepted"]) == ("opus-plan", 3, 3, 3)
    assert len(r["proposals"]) == 1 and "one tier lower" in r["proposals"][0]


def test_outcomes_and_usage_helpers():
    o = task_outcomes([{"type": "review.recorded", "task": "x", "data": {"verdict": "failed"}},
                       {"type": "review.recorded", "task": "x", "data": {"verdict": "passed"}},
                       {"type": "task.closed", "task": "x"}, {"type": "hive.initialized"}])
    assert o["x"] == {"results": 0, "passed": 1, "failed": 1, "closed": True, "cancelled": False}
    u = agentsview_usage([{"cwd": "/w/1hive/x", "total_output_tokens": 10},
                          {"cwd": "/w/1hive/x-review/sub", "total_output_tokens": 5},
                          {"cwd": "/elsewhere", "total_output_tokens": 99}], "/w/1hive")
    assert u == {"x": 15}


def test_report_keeps_runtimes_apart(tmp_path, capsys):
    log = tmp_path / "log.jsonl"
    for rt in ("host", "container"):
        req = tmp_path / f"{rt}.json"
        req.write_text(json.dumps({"task": f"t-{rt}", "attempt": f"t-{rt}.worker.0",
                                   "runtime": rt, "facts": {"kind": "work"}}))
        assert main(["decide", TABLE_PATH, str(req), "--mode", "fixed", "--state",
                     quals_state(tmp_path), "--log", str(log)]) == 0
    capsys.readouterr()
    assert main(["report", str(log), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["groups"]
    assert sorted(r["runtime"] for r in rows) == ["container", "host"]
