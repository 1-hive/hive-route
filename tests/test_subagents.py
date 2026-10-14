# SPDX-License-Identifier: GPL-3.0-or-later
"""Subagents inside an attempt (ROUTING.md §4.9)."""
from __future__ import annotations

import json

import pytest
from conftest import ROOT, qualified

from hiveroute.cli import main
from hiveroute.decide import decide
from hiveroute.errors import RouteError
from hiveroute.log import read
from hiveroute.table import Table, load_table

BASE = load_table(ROOT / "examples" / "1-hive.yaml")


def table(subagents: dict | None) -> Table:
    data = json.loads(json.dumps(BASE.data))
    if subagents is not None:
        data["subagents"] = subagents
    return Table.from_data(data)


def state(t: Table, ids=None) -> dict:
    return {"as_of": "2026-10-14T00:00:00Z", "routes": qualified(t, ids)}


STRONG = {"task": "t", "attempt": "t.worker.0", "facts": {
    "kind": "work", "leverage": 0, "specification": "goal_only", "verification": "none",
    "scope": "many", "consequence": "costly"}}


def test_a_routed_decision_lists_its_eligible_subagents():
    t = table({"opus-plan": ["sonnet-plan"]})
    d = decide(STRONG, t, state(t))
    assert d["route_id"] == "opus-plan"
    assert d["subagents"] == [{"route_id": "sonnet-plan", "route_pin": t.route_pin("sonnet-plan"),
                               "model": "claude-sonnet-5"}]
    assert any(r["rule"] == "SA" and "sonnet-plan" in r["note"] for r in d["reasons"])


def test_an_unqualified_subagent_is_left_out():
    t = table({"opus-plan": ["sonnet-plan"]})
    d = decide(STRONG, t, state(t, [r for r in t.routes if r != "sonnet-plan"]))
    assert d["route_id"] == "opus-plan" and d["subagents"] == []
    assert "RT2" in d["rejected"]["sonnet-plan"][0]


def test_without_the_table_entry_a_decision_is_unchanged():
    assert "subagents" not in decide(STRONG, BASE, state(BASE))
    assert table(None).pin == BASE.pin  # an absent map doesn't change the table's pin


def test_adding_subagents_keeps_every_route_pin():
    t = table({"opus-plan": ["sonnet-plan"]})
    assert t.pin != BASE.pin
    assert all(t.route_pin(r) == BASE.route_pin(r) for r in t.routes)


@pytest.mark.parametrize("subs, message", [
    ({"ghost": ["sonnet-plan"]}, "unknown route"),
    ({"opus-plan": ["ghost"]}, "unknown route"),
    ({"gpt-astra-high": ["gpt-sol-plan"]}, "only claude-code"),
    ({"opus-plan": ["opus-plan"]}, "its own subagent"),
    ({"opus-plan": ["api-sonnet"]}, "same pool"),
    ({"opus-plan": ["sonnet-low"]}, "effort"),
    ({"sonnet-plan": ["opus-plan"]}, "effort"),
])
def test_invalid_subagents_are_refused(subs, message):
    with pytest.raises(RouteError) as e:
        table(subs)
    assert e.value.code == "TABLE_INVALID" and message in e.value.reason


def test_a_higher_tier_subagent_is_refused():
    data = json.loads(json.dumps(BASE.data))
    data["routes"]["opus-nofx"] = {**data["routes"]["opus-plan"], "tier": "strong"}
    del data["routes"]["opus-nofx"]["effort"]
    data["tiers"]["strong"].append("opus-nofx")
    data["subagents"] = {"sonnet-plan": ["opus-nofx"]}
    with pytest.raises(RouteError) as e:
        Table.from_data(data)
    assert "higher tier" in e.value.reason


def test_manifest_carries_subagents_and_observe_accepts_their_models(tmp_path, capsys):
    t = table({"opus-plan": ["sonnet-plan"]})
    d = decide(STRONG, t, state(t))
    dec = tmp_path / "dec.json"
    dec.write_text(json.dumps(d))
    out = tmp_path / "w.jsonl"
    assert main(["manifest", str(dec), "--cwd", str(tmp_path), "--output", str(out)]) == 0
    m = json.loads(capsys.readouterr().out)
    assert m["subagents"] == [{"route_id": "sonnet-plan", "model": "claude-sonnet-5"}]
    mf = tmp_path / "t.worker.0.attempt.json"
    mf.write_text(json.dumps(m))
    rows = [{"type": "assistant", "message": {"model": "claude-opus-5-5", "content": []}},
            {"type": "assistant", "parent_tool_use_id": "toolu_1",
             "message": {"model": "claude-sonnet-5", "content": []}}]
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    log = tmp_path / "log.jsonl"
    assert main(["observe", str(log), str(mf)]) == 0
    assert list(read(log)) == []
    # a model that is neither the route's nor a listed subagent's is still drift
    rows.append({"type": "assistant", "parent_tool_use_id": "toolu_2",
                 "message": {"model": "claude-haiku-4-5", "content": []}})
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert main(["observe", str(log), str(mf)]) == 4
