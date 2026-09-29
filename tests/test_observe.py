# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import ROOT

from hiveroute.canonical import format_time
from hiveroute.cli import main
from hiveroute.log import read
from hiveroute.table import load_table

TABLE_PATH = str(ROOT / "examples" / "1-hive.yaml")
TABLE = load_table(TABLE_PATH)


def manifest(tmp_path, attempt: str, route_id: str, output, harness="claude-code",
             started=None) -> str:
    route = TABLE.routes[route_id]
    m = {"format": "hive-route.attempt/1", "attempt": attempt, "task": attempt.split(".")[0],
         "route_id": route_id, "route_pin": TABLE.route_pin(route_id), "model": route["model"],
         "effort": route.get("effort"), "harness": harness, "cwd": str(tmp_path / "task"),
         "output": str(output),
         "started_at": format_time(started or datetime.now(UTC) - timedelta(minutes=5))}
    p = tmp_path / f"{attempt}.attempt.json"
    p.write_text(json.dumps(m))
    return str(p)


def claude_stream(path, *models) -> None:
    lines = [{"type": "system", "subtype": "init", "model": models[0] if models else "x"}]
    lines += [{"type": "assistant", "message": {"model": m, "content": []}} for m in models]
    lines.append({"type": "assistant", "message": {"model": "<synthetic>", "content": []}})
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


def quals(tmp_path) -> tuple[str, str]:
    q = tmp_path / "q.json"
    q.write_text(json.dumps({rid: {"status": "qualified", "route_pin": TABLE.route_pin(rid)}
                             for rid in TABLE.routes}))
    s = tmp_path / "sources.yaml"
    s.write_text(json.dumps({"format": "hive-route.sources/1", "qualifications": str(q),
                             "pools": {}}))
    return str(q), str(s)


def test_no_drift(tmp_path, capsys):
    out = tmp_path / "w.jsonl"
    claude_stream(out, "claude-opus-5-5", "claude-opus-5-5")
    mf = manifest(tmp_path, "t.worker.0", "opus-plan", out)
    log = tmp_path / "log.jsonl"
    assert main(["observe", str(log), mf]) == 0
    assert list(read(log)) == []


def test_drift_logged_once_and_route_demoted(tmp_path, capsys):
    out = tmp_path / "w.jsonl"
    claude_stream(out, "claude-opus-5-5", "claude-opus-5-6")
    mf = manifest(tmp_path, "t.worker.0", "opus-plan", out)
    q, s = quals(tmp_path)
    log = tmp_path / "log.jsonl"
    assert main(["observe", str(log), str(tmp_path / "*.attempt.json"), "--sources", s]) == 4
    assert "pinned claude-opus-5-5, reported claude-opus-5-5, claude-opus-5-6" in \
        capsys.readouterr().out
    assert main(["observe", str(log), mf, "--sources", s]) == 0  # already recorded
    events = list(read(log))
    assert [e["type"] for e in events] == ["route.drift_detected"]
    status = json.loads(Path(q).read_text())
    assert status["opus-plan"]["status"] == "candidate"
    assert status["sonnet-plan"]["status"] == "qualified"


def test_codex_models_from_sessions_in_the_attempt_folder(tmp_path):
    root = tmp_path / "codex"
    root.mkdir()
    cwd = str(tmp_path / "task")

    def session(name, session_cwd, model):
        p = root / name
        p.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": session_cwd}}) + "\n"
                     + json.dumps({"type": "turn_context", "payload": {"model": model}}) + "\n")
        return p

    session("mine.jsonl", cwd, "gpt-5.7-sol")
    session("other.jsonl", "/elsewhere", "gpt-5.6-sol")
    old = session("old.jsonl", cwd, "gpt-5.5")
    t = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(old, (t, t))
    mf = manifest(tmp_path, "t.review.0", "gpt-sol-plan", tmp_path / "codex-0.log", "codex")
    log = tmp_path / "log.jsonl"
    assert main(["observe", str(log), mf, "--codex-root", str(root)]) == 4
    [ev] = list(read(log))
    assert ev["data"]["observed_models"] == ["gpt-5.7-sol"]


def test_manifest_from_decision(tmp_path, capsys):
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"task": "t", "attempt": "t.worker.0", "facts": {"kind": "work"}}))
    assert main(["decide", TABLE_PATH, str(req), "--mode", "fixed"]) == 0
    dec = tmp_path / "dec.json"
    dec.write_text(capsys.readouterr().out)
    assert main(["manifest", str(dec), "--cwd", "/w", "--output", "/w/worker-0.jsonl"]) == 0
    m = json.loads(capsys.readouterr().out)
    assert (m["route_id"], m["model"], m["harness"]) == ("opus-plan", "claude-opus-5-5",
                                                          "claude-code")
