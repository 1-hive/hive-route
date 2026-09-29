# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ROOT
from jsonschema import Draft202012Validator

from hiveroute.cli import main
from hiveroute.log import read
from hiveroute.record import line_digest, summary, sync

TABLE_PATH = str(ROOT / "examples" / "1-hive.yaml")
RECORD_SCHEMAS = ROOT.parent / "hive-record" / "policy" / "core" / "schemas" / "events"


def build_log(tmp_path) -> Path:
    log = tmp_path / "log.jsonl"
    assert main(["mode", "fixed", TABLE_PATH, "--log", str(log)]) == 0
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"task": "t1", "attempt": "t1.worker.0",
                               "facts": {"kind": "work", "scope": "few"}}))
    assert main(["decide", TABLE_PATH, str(req), "--log", str(log)]) == 0
    st = tmp_path / "limited.json"
    st.write_text(json.dumps({"as_of": "2026-09-29T12:00:00Z", "pools": {
        "claude-plan": {"limited_until": "2026-09-29T15:00:00Z"}}}))
    assert main(["decide", TABLE_PATH, str(req), "--state", str(st), "--log", str(log)]) == 3
    return log


def test_summaries_are_compact_and_bound_to_the_log(tmp_path, capsys):
    log = build_log(tmp_path)
    capsys.readouterr()
    events = list(read(log))
    out = [summary(e, {}) for e in events]
    assert [o[0] for o in out] == ["route.table_pinned", "route.mode_set", "route.decided",
                                   "route.waiting"]
    for ev, (_, data) in zip(events, out, strict=True):
        assert data["log"] == {"seq": ev["seq"], "digest": line_digest(ev)}
        assert len(json.dumps(data)) < 8192
    decided = out[2][1]
    assert decided["route_id"] == "opus-plan" and decided["facts"] == {
        "specification": "goal_only", "verification": "none", "scope": "few",
        "consequence": "costly"}
    assert "fixed" in decided["rules"] and "F5" in decided["rules"]
    waiting = out[3][1]
    assert (waiting["decision"], waiting["wait_until"]) == ("wait", "2026-09-29T15:00:00Z")


@pytest.mark.skipif(not RECORD_SCHEMAS.is_dir(), reason="no hive-record checkout alongside")
def test_summaries_match_the_record_schemas(tmp_path, capsys):
    log = build_log(tmp_path)
    capsys.readouterr()
    for ev in read(log):
        type_, data = summary(ev, {})
        schema = json.loads((RECORD_SCHEMAS / f"{type_}.schema.json").read_text())
        errors = list(Draft202012Validator(schema).iter_errors(data))
        assert not errors, (type_, [e.message for e in errors])


def test_sync_posts_once_and_stops_at_an_error(tmp_path, capsys):
    log = build_log(tmp_path)
    capsys.readouterr()
    posted: list = []

    def ok(type_, data, key):
        posted.append((type_, key))
        return True, "ok"

    assert sync(str(log), ok) == (4, [])
    assert sync(str(log), ok) == (0, [])  # already recorded
    assert len({k for _, k in posted}) == 4

    (tmp_path / "b").mkdir()
    log2 = build_log(tmp_path / "b")
    calls = []

    def fail_second(type_, data, key):
        calls.append(type_)
        return (len(calls) < 2), "refused: UNKNOWN_EVENT_TYPE"

    n, errors = sync(str(log2), fail_second)
    assert n == 1 and errors and "UNKNOWN_EVENT_TYPE" in errors[0]
    assert Path(str(log2) + ".recorded").read_text().strip() == "1"
