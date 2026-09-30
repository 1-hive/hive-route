# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import sys
from pathlib import Path

from conftest import ROOT

from hiveroute.canary import load_suite
from hiveroute.cli import main
from hiveroute.log import read
from hiveroute.table import load_table

TABLE_PATH = str(ROOT / "examples" / "1-hive.yaml")
TABLE = load_table(TABLE_PATH)

FAKE = """
import json, pathlib, sys
model, mode = sys.argv[1], sys.argv[2]
reported = "claude-opus-9" if mode == "drift" else model
print(json.dumps({"type": "assistant", "message": {"model": reported, "content": []}}))
if mode != "lazy":
    pathlib.Path("done.txt").write_text("done")
"""


def setup(tmp_path, mode: str) -> tuple[str, str, str]:
    suite = tmp_path / "suite"
    (suite / "one").mkdir(parents=True)
    (suite / "one" / "PROMPT.md").write_text("write done.txt")
    (suite / "one" / "check.py").write_text(
        "import os, pathlib, sys\n"
        "sys.exit(0 if (pathlib.Path(os.environ['CANARY_WORK']) / 'done.txt').exists() else 1)\n")
    (suite / "suite.yaml").write_text(json.dumps({
        "format": "hive-route.suite/1", "name": "t",
        "cases": [{"id": "one", "kind": "work", "prompt": "one/PROMPT.md",
                   "check": [sys.executable, "{case}/check.py"]}]}))
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE)
    sources = tmp_path / "sources.yaml"
    sources.write_text(json.dumps({
        "format": "hive-route.sources/1", "qualifications": str(tmp_path / "q.json"),
        "canary_workdir": str(tmp_path / "work"),
        "harnesses": {"claude-code": {"argv": [sys.executable, str(fake), "{model}", mode],
                                      "effort_args": ["--effort={effort}"]}}}))
    return str(suite), str(sources), str(tmp_path / "log.jsonl")


def test_qualified(tmp_path, capsys):
    suite, sources, log = setup(tmp_path, "ok")
    assert main(["canary", "run", TABLE_PATH, "opus-plan", "--suite", suite,
                 "--sources", sources, "--log", log]) == 0
    q = json.loads((tmp_path / "q.json").read_text())["opus-plan"]
    assert (q["status"], q["route_pin"], q["passed"]) == ("qualified", TABLE.route_pin("opus-plan"), 1)
    assert q["suite_pin"] == load_suite(suite)[2]
    ev = [e for e in read(log) if e["type"] == "route.canary_recorded"]
    assert ev[0]["data"]["lesson"] == "1/1 cases passed"

    assert main(["canary", "lesson", "opus-plan", "good at small edits",
                 "--sources", sources, "--log", log]) == 0
    assert json.loads((tmp_path / "q.json").read_text())["opus-plan"]["lesson"] == \
        "good at small edits"
    # the state now says the route is qualified at its pin
    assert main(["state", TABLE_PATH, sources]) == 0


def test_failed_check_leaves_candidate(tmp_path, capsys):
    suite, sources, log = setup(tmp_path, "lazy")
    assert main(["canary", "run", TABLE_PATH, "opus-plan", "--suite", suite,
                 "--sources", sources, "--log", log]) == 5
    q = json.loads((tmp_path / "q.json").read_text())["opus-plan"]
    assert q["status"] == "candidate" and q["lesson"] == "0/1 cases passed; failed: one"


def test_drift_fails_qualification(tmp_path, capsys):
    suite, sources, log = setup(tmp_path, "drift")
    assert main(["canary", "run", TABLE_PATH, "opus-plan", "--suite", suite,
                 "--sources", sources, "--log", log]) == 5
    q = json.loads((tmp_path / "q.json").read_text())["opus-plan"]
    assert q["status"] == "candidate" and "claude-opus-9" in q["lesson"]


def test_starter_suite_loads():
    root, suite, pin = load_suite(ROOT / "canaries" / "starter")
    assert [c["id"] for c in suite["cases"]] == [
        "fix-bug", "implement-spec", "multi-file-change", "review-buggy", "review-clean"]
    for c in suite["cases"]:
        assert (root / c["prompt"]).is_file() and (root / c["files"]).is_dir()
    assert pin.startswith("sha256:")
    assert Path(root / "fix-bug" / "check.py").is_file()


def test_busy_pool_stops_the_run(tmp_path, capsys):
    suite, sources, log = setup(tmp_path, "ok")
    s = json.loads(Path(sources).read_text())
    stream = tmp_path / "w.jsonl"
    stream.write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.7, "resetsAt": 4102444800},
            "seven_day": {"utilization": 0.2, "resetsAt": 4102444800}}}}) + "\n")
    s["pools"] = {"claude-plan": [{"reader": "claude-stream", "paths": [str(stream)]}]}
    Path(sources).write_text(json.dumps(s))
    assert main(["canary", "run", TABLE_PATH, "opus-plan", "--suite", suite, "--sources", sources,
                 "--log", log, "--max-usage", "0.6"]) == 6
    q = json.loads((tmp_path / "q.json").read_text())["opus-plan"]
    assert q["status"] == "candidate" and q["lesson"].startswith("stopped after 0 of 1 cases")


def test_operator_accepts_a_candidate(tmp_path, capsys):
    suite, sources, log = setup(tmp_path, "lazy")
    assert main(["canary", "run", TABLE_PATH, "opus-plan", "--suite", suite,
                 "--sources", sources, "--log", log]) == 5
    assert main(["canary", "accept", TABLE_PATH, "opus-plan", "--by", "operator",
                 "--reason", "cheap to retry", "--sources", sources, "--log", log]) == 0
    q = json.loads((tmp_path / "q.json").read_text())["opus-plan"]
    assert q["status"] == "qualified" and q["accepted"]["by"] == "operator"
    assert "Accepted at 0/1 by operator: cheap to retry" in q["lesson"]
    ev = [e for e in read(log) if e["type"] == "route.canary_recorded"][-1]["data"]
    assert ev["status"] == "qualified" and ev["accepted"]["reason"] == "cheap to retry"
    assert main(["canary", "accept", TABLE_PATH, "opus-plan", "--by", "operator",
                 "--reason", "again", "--sources", sources, "--log", log]) == 2  # already qualified


def test_busy_check_uses_a_share_for_metered_pools(monkeypatch):
    from hiveroute import canary
    from hiveroute.table import Table
    data = json.loads(json.dumps(TABLE.data))
    data["pools"]["api"] = {"kind": "metered", "limits": [{"usd": 10, "per": "month"}]}
    data["routes"]["api-x"] = {"tier": "standard", "pool": "api", "family": "claude",
                               "model": "m", "price": {"in": 1, "out": 1}}
    t = Table.from_data(data)
    used = {"v": 2.0}

    def fake_collect(table, sources, now):
        return {"as_of": "2026-09-30T00:00:00Z", "pools": {"api": {"usage": {
            "usd/month": {"used": used["v"], "basis": "measured"}}}}}, {}
    monkeypatch.setattr("hiveroute.usage.collect", fake_collect)
    assert canary.pool_busy(t, "api-x", {}, 0.8) is None  # $2 of $10 is 20%
    used["v"] = 9.0
    assert "90%" in canary.pool_busy(t, "api-x", {}, 0.8)  # $9 of $10
