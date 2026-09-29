# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import threading

from conftest import NOW, build_case, cases, fixture_table, qualified

from hiveroute import decide
from hiveroute import log as routelog


def test_every_fixture_replays_from_the_log(tmp_path):
    path = tmp_path / "route.jsonl"
    for p in cases():
        req, table, state, mode = build_case(p.values[0])
        routelog.record_decision(path, table, req, state, mode, decide(req, table, state, mode))
    events = list(routelog.read(path))
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert sum(e["type"] == "route.table_pinned" for e in events) == 2  # mixed and 1-hive
    checked, problems = routelog.replay(path)
    assert checked == len(cases()) and problems == []


def test_replay_catches_a_changed_decision(tmp_path):
    path = tmp_path / "route.jsonl"
    table = fixture_table()
    req = {"task": "t", "attempt": "t.1", "facts": {"kind": "work"}}
    state = {"as_of": NOW, "routes": qualified(table)}
    routelog.record_decision(path, table, req, state, "live", decide(req, table, state))
    lines = path.read_text().splitlines()
    ev = json.loads(lines[-1])
    ev["data"]["decision"]["route_id"] = "qwen-local"
    lines[-1] = json.dumps(ev)
    path.write_text("\n".join(lines) + "\n")
    checked, problems = routelog.replay(path)
    assert checked == 1 and len(problems) == 1


def test_replay_catches_a_tampered_table(tmp_path):
    path = tmp_path / "route.jsonl"
    table = fixture_table()
    routelog.set_mode(path, "fixed", table)
    ev = json.loads(path.read_text().splitlines()[0])
    ev["data"]["table"]["soft_threshold"] = 0.5
    path.write_text(json.dumps(ev) + "\n" + path.read_text().splitlines()[1] + "\n")
    _, problems = routelog.replay(path)
    assert problems and "table content" in problems[0]


def test_mode_and_waiting_events(tmp_path):
    path = tmp_path / "route.jsonl"
    table = fixture_table()
    assert routelog.current_mode(path) is None
    routelog.set_mode(path, "fixed", table)
    routelog.set_mode(path, "live", table)
    assert routelog.current_mode(path) == "live"
    req = {"task": "t", "attempt": "t.1", "facts": {"kind": "work"}}
    state = {"as_of": NOW}
    ev = routelog.record_decision(path, table, req, state, "live", decide(req, table, state))
    assert ev["type"] == "route.waiting"
    assert [e["type"] for e in routelog.read(path)].count("route.table_pinned") == 1


def test_concurrent_appends_are_gapless(tmp_path):
    path = tmp_path / "route.jsonl"
    table = fixture_table()
    req = {"task": "t", "attempt": "t.1", "facts": {"kind": "work"}}
    state = {"as_of": NOW, "routes": qualified(table)}
    d = decide(req, table, state)

    def work():
        for _ in range(10):
            routelog.record_decision(path, table, req, state, "live", d)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [e["seq"] for e in routelog.read(path)]
    assert seqs == list(range(1, 42))
