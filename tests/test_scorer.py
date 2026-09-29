# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json

import pytest
from conftest import ROOT

from hiveroute import scorer
from hiveroute.cli import main
from hiveroute.decide import decide
from hiveroute.log import read
from hiveroute.table import Table, load_table

TABLE_PATH = str(ROOT / "examples" / "1-hive.yaml")
TABLE = load_table(TABLE_PATH)
EXPLICIT = {"specification": "explicit", "verification": "independent", "scope": "few",
            "consequence": "reversible", "leverage": 0}


def with_mode(mode: str) -> Table:
    data = json.loads(json.dumps(TABLE.data))
    data["scorer"]["mode"] = mode
    return Table.from_data(data)


def reply(**facts) -> str:
    return json.dumps({"facts": {k: {"value": v, "reason": "r"} for k, v in facts.items()},
                       "suggested_tier": "light"})


def test_not_called_when_facts_are_known_or_cannot_lower():
    req = {"task": "t", "attempt": "a", "facts": {"kind": "work", **EXPLICIT}}
    assert scorer.could_lower(req, TABLE) == []
    # a consult is strong whatever the facts
    assert scorer.could_lower({"task": "t", "attempt": "a", "facts": {"kind": "consult"}},
                              TABLE) == []
    assert scorer.score(TABLE, req, "x", caller=lambda *a: pytest.fail("called")) is None


def test_live_estimates_fill_unknown_facts_only():
    t = with_mode("live")
    req = {"task": "t", "attempt": "a",
           "facts": {"kind": "work", "consequence": "reversible", "leverage": 0}}
    assert scorer.could_lower(req, t) == ["specification", "verification", "scope"]
    s = scorer.score(t, req, "text", caller=lambda *a: reply(
        specification="explicit", verification="independent", scope="single",
        consequence="costly"))
    assert set(s["estimates"]) == {"specification", "verification", "scope"}  # not asked: dropped
    req2 = scorer.apply(req, s)
    d = decide(req2, t, {"as_of": "2026-09-29T12:00:00Z"}, "live")
    assert d["computed_tier"] == "light"
    assert d["facts"]["scope"] == {"value": "single", "source": "estimated"}
    assert d["facts"]["consequence"]["source"] == "supplied"


def test_shadow_estimates_are_not_used():
    s = scorer.score(TABLE, {"task": "t", "attempt": "a", "facts": {"kind": "work"}}, "x",
                     caller=lambda *a: reply(specification="explicit"))
    assert s["mode"] == "shadow"
    req = {"task": "t", "attempt": "a", "facts": {"kind": "work"}}
    assert scorer.apply(req, s) is req


@pytest.mark.parametrize("content", ["not json", "[]", '{"facts": {"scope": {"value": "huge"}}}'])
def test_invalid_output_is_discarded(content):
    s = scorer.score(with_mode("live"), {"task": "t", "attempt": "a", "facts": {"kind": "work"}},
                     "x", caller=lambda *a: content)
    assert s["estimates"] == {} and s["error"] == "no valid estimates in the output"


def test_unreachable_model_leaves_facts_unknown():
    def boom(*a):
        raise OSError("connection refused")
    s = scorer.score(with_mode("live"), {"task": "t", "attempt": "a", "facts": {"kind": "work"}},
                     "x", caller=boom)
    assert s["estimates"] == {} and "connection refused" in s["error"]


def test_cli_logs_scored_and_replays(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(scorer.CALLERS, "ollama", lambda *a: reply(
        specification="explicit", verification="independent", scope="few", consequence="reversible"))
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"task": "t", "attempt": "t.worker.0", "facts": {"kind": "work"}}))
    text = tmp_path / "task.md"
    text.write_text("Fix the off-by-one in stats.py; tests in test_stats.py must pass.")
    log = tmp_path / "log.jsonl"
    assert main(["decide", TABLE_PATH, str(req), "--mode", "fixed", "--task-text", str(text),
                 "--log", str(log)]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["facts"]["scope"]["source"] == "default"  # shadow: not used
    [scored] = [e["data"] for e in read(log) if e["type"] == "route.scored"]
    assert (scored["tier_without"], scored["tier_with_estimates"]) == ("strong", "light")
    assert main(["replay", str(log)]) == 0
