# SPDX-License-Identifier: GPL-3.0-or-later
"""Properties over every combination of facts (ROUTING.md §4)."""

from __future__ import annotations

import copy
import itertools

import pytest
from conftest import NOW, qualified

from hiveroute import RouteError, decide
from hiveroute.table import TIERS, rank

VALUES = {
    "kind": ["work", "review", "plan", "consult", "triage", "digest"],
    "specification": ["explicit", "partial", "goal_only", None],
    "verification": ["independent", "weak", "none", None],
    "scope": ["single", "few", "many", None],
    "consequence": ["reversible", "costly", None],
    "leverage": [0, 5, None],
}
USAGE = {"pools": {p: {"usage": {"usd/month": {"used": 1, "basis": "measured"}}}
                   for p in ("anthropic-api", "openai-api")}}


def all_facts():
    names = list(VALUES)
    for combo in itertools.product(*(VALUES[n] for n in names)):
        yield {n: v for n, v in zip(names, combo, strict=True) if v is not None}


def request(facts: dict, **extra) -> dict:
    r = {"task": "t", "attempt": "t.1", "facts": facts, **extra}
    if facts["kind"] == "review":
        r.setdefault("author", {"family": "claude", "tier": "light"})
    return r


@pytest.fixture
def state(table):
    return {"as_of": NOW, **USAGE, "routes": qualified(table)}


def test_every_combination(table, state):
    for facts in all_facts():
        req = request(facts)
        d = decide(req, table, state)
        # deterministic
        assert d == decide(copy.deepcopy(req), table, copy.deepcopy(state))
        # never below the kind's floor
        assert rank(d["tier"]) >= rank(table.data["kind_floor"][facts["kind"]])
        # unknown facts never make it cheaper than knowing the costliest value
        worst = {**facts, **{k: v for k, v in {"specification": "goal_only",
                 "verification": "none", "scope": "many", "consequence": "costly",
                 "leverage": 5}.items() if k not in facts}}
        assert d["tier"] == decide(request(worst), table, state)["tier"]
        # a chosen route is in the decided tier, and reviews change family
        if d["decision"] == "route":
            assert table.routes[d["route_id"]]["tier"] == d["tier"]
            if facts["kind"] == "review":
                assert table.routes[d["route_id"]]["family"] != "claude"
        # a hint only ever raises
        for hint in TIERS:
            h = decide(request(facts, hint={"tier": hint, "reason": "r"}), table, state)
            assert rank(h["computed_tier"]) == max(rank(d["computed_tier"]), rank(hint))


def test_decide_does_not_mutate_inputs(table, state):
    req = request({"kind": "work"}, history=[
        {"attempt": "t.0", "route_id": "opus-plan", "tier": "strong", "class": "interrupted"}])
    before = copy.deepcopy((req, state, table.data))
    decide(req, table, state)
    assert (req, state, table.data) == before


@pytest.mark.parametrize("bad, code", [
    ({"task": "t", "attempt": "a", "facts": {}}, "REQUEST_INVALID"),
    ({"task": "t", "attempt": "a", "facts": {"kind": "review"}}, "REQUEST_INVALID"),
    ({"task": "t", "attempt": "a", "facts": {"kind": "work"},
      "override": {"route_id": "ghost", "reason": "r", "by": "op"}}, "REQUEST_INVALID"),
    ({"task": "t", "attempt": "a", "facts": {"kind": "work"}, "surprise": 1}, "REQUEST_INVALID"),
])
def test_bad_requests(table, state, bad, code):
    with pytest.raises(RouteError) as e:
        decide(bad, table, state)
    assert e.value.code == code


def test_bad_state(table):
    with pytest.raises(RouteError) as e:
        decide(request({"kind": "work"}), table, {"as_of": "yesterday"})
    assert e.value.code == "REQUEST_INVALID"
    with pytest.raises(RouteError) as e:
        decide(request({"kind": "work"}), table, {"as_of": "2026-09-29T12:00:00"})
    assert "offset" in e.value.reason
    with pytest.raises(RouteError):
        decide(request({"kind": "work"}), table, {"as_of": NOW}, mode="shadow")


def test_unknown_usage_is_never_zero(table):
    st = {"as_of": NOW, "routes": qualified(table),
          "pools": {"claude-plan": {"limited_until": "2026-09-30T00:00:00Z"},
                    "anthropic-api": {"usage": {"usd/month": {"used": 1, "basis": "unknown"}}},
                    "openai-api": {"usage": {"usd/month": {"used": None, "basis": "measured"}}}}}
    d = decide(request({"kind": "work"}), table, st)
    assert d["decision"] == "wait"
    assert any("usage unknown" in n for n in d["rejected"]["opus-api"])
