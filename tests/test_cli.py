# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json

from conftest import FIXTURES, NOW, fixture_table, qualified

from hiveroute.cli import main

TABLE = str(FIXTURES / "tables" / "mixed.yaml")


def test_check(capsys):
    assert main(["check", TABLE, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["table"] == fixture_table().pin and "opus-plan" in out["routes"]


def test_check_invalid(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("format: hive-route.table/1\n")
    assert main(["check", str(bad)]) == 2
    assert "TABLE_INVALID" in capsys.readouterr().err


def test_decide_log_mode_and_replay(tmp_path, capsys):
    req, state, log = tmp_path / "req.json", tmp_path / "state.json", tmp_path / "log.jsonl"
    req.write_text(json.dumps({"task": "t", "attempt": "t.1", "facts": {"kind": "work"}}))
    state.write_text(json.dumps({"as_of": NOW, "routes": qualified(fixture_table())}))

    assert main(["mode", "fixed", TABLE, "--log", str(log)]) == 0
    capsys.readouterr()
    assert main(["decide", TABLE, str(req), "--state", str(state), "--log", str(log)]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["mode"] == "fixed" and d["route_id"] == "opus-plan"

    # no usage known: every metered pool is blocked, so live mode waits (exit 3)
    assert main(["decide", TABLE, str(req), "--mode", "live", "--log", str(log)]) == 3
    assert json.loads(capsys.readouterr().out)["decision"] in ("wait", "no_route")

    assert main(["replay", str(log)]) == 0
    assert "2 decisions replayed, 0 problems" in capsys.readouterr().out


def test_decide_bad_input(tmp_path, capsys):
    req = tmp_path / "req.json"
    req.write_text("[1]")
    assert main(["decide", TABLE, str(req)]) == 2
    assert "INPUT_INVALID" in capsys.readouterr().err
