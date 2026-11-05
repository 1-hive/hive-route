# SPDX-License-Identifier: GPL-3.0-or-later
"""Routes served by long-lived agents (rev 21): `actor`, `review_distinct`, `unavailable`, and
accepting such a route without a canary."""
from __future__ import annotations

import json

import pytest
from conftest import qualified

from hiveroute.cli import main
from hiveroute.decide import decide
from hiveroute.errors import RouteError
from hiveroute.log import read
from hiveroute.table import Table

RAW = {
    "format": "hive-route.table/1", "version": 1,
    "fact_defaults": {"author_tier": "light"},
    "pools": {
        "openai": {"kind": "metered", "limits": []},
        "claude": {"kind": "subscription", "limits": []},
    },
    "routes": {
        "astra": {"tier": "strong", "pool": "openai", "family": "gpt", "model": "gpt-6-astra",
                  "tools": True, "kinds": ["review"], "actor": "reviewer.astra",
                  "price": {"in": 0, "out": 0}},
        "fable": {"tier": "strong", "pool": "claude", "family": "claude", "model": "claude-fable-5-1",
                  "tools": True, "kinds": ["review"], "actor": "reviewer.claude"},
        "bot": {"tier": "strong", "pool": "claude", "family": "claude", "model": "claude-opus-5-5",
                "tools": True, "actor": "worker.bot"},
    },
    "tiers": {"strong": ["astra", "fable"]},
}


def table(**extra) -> Table:
    raw = json.loads(json.dumps(RAW))
    raw.update(extra)
    return Table.from_data(raw)


def state(t: Table) -> dict:
    return {"as_of": "2026-11-05T00:00:00Z", "routes": qualified(t)}


def review(**extra) -> dict:
    return {"task": "t", "attempt": "t.review.0", "max_attempt_usd": 1,
            "facts": {"kind": "review", "consequence": "costly", "verification": "independent",
                      "scope": "few", "specification": "explicit", "leverage": 0},
            "author": {"family": "claude", "model": "claude-opus-5-5"},
            "hint": {"tier": "strong", "reason": "the order asks for this review tier"}, **extra}


def test_a_route_with_an_actor_names_it_in_the_decision():
    t = table()
    d = decide(review(author={"family": "other"}), t, state(t))
    assert (d["decision"], d["route_id"], d["actor"]) == ("route", "astra", "reviewer.astra")


def test_without_an_actor_a_decision_has_no_actor_key():
    raw = json.loads(json.dumps(RAW))
    del raw["routes"]["astra"]["actor"]
    t = Table.from_data(raw)
    assert "actor" not in decide(review(author={"family": "other"}), t, state(t))


def test_rt4_by_family_rules_out_the_same_maker():
    t = table()
    d = decide(review(unavailable={"astra": "out of credits"}), t, state(t))
    assert d["decision"] == "wait"
    assert "RT4: same family" in d["rejected"]["fable"][0]


def test_rt4_by_model_allows_another_model_of_the_same_family():
    t = table(review_distinct="model")
    d = decide(review(unavailable={"astra": "out of credits"}), t, state(t))
    assert (d["decision"], d["actor"]) == ("route", "reviewer.claude")
    assert any("can't run now: out of credits" in r for r in d["rejected"]["astra"])


def test_rt4_by_model_still_refuses_the_authors_own_model():
    t = table(review_distinct="model")
    req = review(author={"family": "claude", "model": "claude-fable-5-1"},
                 unavailable={"astra": "offline"})
    d = decide(req, t, state(t))
    assert d["decision"] == "wait" and "RT4: same model" in d["rejected"]["fable"][0]


def test_rt4_by_model_without_the_authors_model_falls_back_to_family():
    t = table(review_distinct="model")
    d = decide(review(author={"family": "claude"}, unavailable={"astra": "x"}), t, state(t))
    assert d["decision"] == "wait" and "RT4: same family" in d["rejected"]["fable"][0]


def test_an_unavailable_route_waits_rather_than_no_route():
    t = table(review_distinct="model")
    d = decide(review(unavailable={"astra": "a", "fable": "b"}), t, state(t))
    assert d["decision"] == "wait" and d["wait_until"] is None


def test_absent_options_keep_the_table_pin():
    raw = json.loads(json.dumps(RAW))
    assert Table.from_data(raw).pin == table().pin
    assert table(review_distinct="model").pin != table().pin


def test_accept_without_a_canary_is_only_for_actor_routes(tmp_path, capsys):
    raw = json.loads(json.dumps(RAW))
    raw["routes"]["plain"] = {"tier": "strong", "pool": "claude", "family": "x", "model": "m",
                              "tools": True}
    tp, sp, log = tmp_path / "t.json", tmp_path / "s.json", tmp_path / "log.jsonl"
    tp.write_text(json.dumps(raw))
    sp.write_text(json.dumps({"format": "hive-route.sources/1",
                              "qualifications": str(tmp_path / "q.json")}))
    assert main(["canary", "accept", str(tp), "fable", "--by", "andy", "--reason", "in service",
                 "--sources", str(sp), "--log", str(log), "--no-canary"]) == 0
    q = json.loads((tmp_path / "q.json").read_text())["fable"]
    t = Table.from_data(raw)
    assert q["status"] == "qualified" and q["route_pin"] == t.route_pin("fable")
    assert (q["passed"], q["cases"], q["accepted"]["by"]) == (0, 0, "andy")
    ev = [e for e in read(str(log)) if e["type"] == "route.canary_recorded"][-1]["data"]
    assert ev["suite"] == "in-service" and ev["suite_pin"].startswith("sha256:")
    with pytest.raises(RouteError, match="has no actor"):
        from hiveroute.canary import accept
        accept(t, "plain", "andy", "x", json.loads(sp.read_text()), str(log), no_canary=True)


def test_review_tiers_order_reviews_without_changing_work():
    raw = json.loads(json.dumps(RAW))
    raw["routes"]["opus-std"] = {"tier": "standard", "pool": "claude", "family": "claude",
                                 "model": "claude-opus-5-5", "tools": True}
    raw["routes"]["sol"] = {"tier": "standard", "pool": "openai", "family": "gpt", "model": "gpt-5.6-sol",
                            "tools": True, "price": {"in": 0, "out": 0}}
    raw["tiers"]["standard"] = ["opus-std", "sol"]
    raw["kind_floor"] = {"review": "standard"}
    base = Table.from_data(raw)
    raw["review_tiers"] = {"standard": ["sol", "opus-std"]}
    t = Table.from_data(raw)
    assert all(t.route_pin(r) == base.route_pin(r) for r in t.routes)
    rev = review(author={"family": "qwen"}, hint=None)
    del rev["hint"]
    rev["facts"]["consequence"] = "reversible"
    assert decide(rev, t, state(t))["route_id"] == "sol"
    assert decide(rev, base, state(base))["route_id"] == "opus-std"
    work = {"task": "t", "attempt": "t.w.0", "max_attempt_usd": 1,
            "facts": {**rev["facts"], "kind": "work", "scope": "many"}}
    assert decide(work, t, state(t))["route_id"] == "opus-std"
    assert decide({**rev, "unavailable": {"sol": "out"}}, t, state(t))["route_id"] == "opus-std"


def test_review_tiers_are_checked():
    raw = json.loads(json.dumps(RAW))
    raw["review_tiers"] = {"standard": ["astra"]}
    with pytest.raises(RouteError, match="has tier"):
        Table.from_data(raw)
