# SPDX-License-Identifier: GPL-3.0-or-later
"""A request's `harnesses`: routes on harnesses the runner can't start are ruled out (RT2, rev 15)."""
from __future__ import annotations

from conftest import ROOT, qualified

from hiveroute.decide import decide
from hiveroute.table import load_table

T = load_table(ROOT / "examples" / "1-hive.yaml")
LIGHT = {"task": "t", "attempt": "t.worker.1", "reason": "restart", "facts": {
    "kind": "work", "leverage": 0, "specification": "explicit", "verification": "independent",
    "scope": "single", "consequence": "reversible"}}


def state(claude_used: float) -> dict:
    return {"as_of": "2026-10-14T19:00:00Z", "routes": qualified(T), "pools": {"claude-plan": {"usage": {
        "window/5h": {"used": claude_used, "basis": "measured", "resets_at": "2026-10-14T20:50:00Z"}}}}}


def test_above_the_soft_threshold_the_router_moves_to_another_pools_harness():
    d = decide(LIGHT, T, state(0.87))
    assert d["route_id"] == "gpt-sol-low" and d["harness"] == "codex"


def test_a_runner_without_that_harness_stays_on_one_it_can_start():
    d = decide({**LIGHT, "harnesses": ["claude-code", "claude-code-allowlist"]}, T, state(0.87))
    assert d["decision"] == "route" and d["route_id"] == "sonnet-low"
    assert d["rejected"]["gpt-sol-low"] == ["RT2: harness codex not available to this attempt's runner"]


def test_no_startable_harness_in_the_tier_is_no_route_not_a_wait():
    d = decide({**LIGHT, "harnesses": ["openclaw"]}, T, state(0.1))
    assert d["decision"] == "no_route"


def test_without_harnesses_nothing_changes():
    assert decide(LIGHT, T, state(0.1))["route_id"] == "sonnet-low"
