# SPDX-License-Identifier: GPL-3.0-or-later
"""Usage probes (probe.py): run when due and useful, and feed the pool's reader."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta

from conftest import FIXTURES

from hiveroute.canonical import UTC
from hiveroute.cli import main
from hiveroute.probe import due, probe_all
from hiveroute.table import load_table
from hiveroute.usage import collect

TABLE = load_table(FIXTURES / "tables" / "openclaw.yaml")


def spec(tmp_path, **over) -> dict:
    event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
             "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": time.time() + 3600},
                                "seven_day": {"utilization": 0.1,
                                              "resetsAt": time.time() + 86400}}}}
    return {"argv": [sys.executable, "-c", f"print({json.dumps(json.dumps(event))})"],
            "output_dir": str(tmp_path / "probes"), **over}


def test_first_probe_runs_and_feeds_the_reader(tmp_path):
    s = spec(tmp_path)
    now = datetime.now(UTC)
    res = probe_all({"probes": {"claude-plan": s}}, now)
    assert res["claude-plan"]["exit"] == 0
    sources = {"format": "hive-route.sources/1", "pools": {"claude-plan": [
        {"reader": "claude-stream", "paths": [s["output_dir"]]}]}}
    st, _ = collect(TABLE, sources, datetime.now(UTC))
    assert st["pools"]["claude-plan"]["usage"]["window/5h"]["used"] == 0.4


def test_not_due_until_the_interval_and_activity(tmp_path):
    active = tmp_path / "agent.sqlite-wal"
    active.write_text("x")
    s = spec(tmp_path, every_minutes=30, active_paths=[str(active)])
    now = datetime.now(UTC)
    probe_all({"probes": {"p": s}}, now)
    assert due(s, now + timedelta(minutes=10)) is None           # too soon
    assert due(s, now + timedelta(minutes=40)) is None           # no activity since
    later = time.time() + 5
    os.utime(active, (later, later))
    assert due(s, now + timedelta(minutes=40))                   # used, and due


def test_keeps_the_newest_files(tmp_path):
    s = spec(tmp_path, keep=2)
    now = datetime.now(UTC)
    for i in range(4):
        probe_all({"probes": {"p": s}}, now + timedelta(minutes=i), force=True)
    assert len(list((tmp_path / "probes").glob("probe-*.jsonl"))) == 2


def test_cli(tmp_path, capsys):
    f = tmp_path / "sources.yaml"
    f.write_text(json.dumps({"format": "hive-route.sources/1", "probes": {"p": spec(tmp_path)}}))
    assert main(["probe", str(f)]) == 0
    assert main(["probe", str(f)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("p: ran (no earlier probe): exit 0") and out[1] == "p: not due"
