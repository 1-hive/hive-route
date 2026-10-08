# SPDX-License-Identifier: GPL-3.0-or-later
"""The second reader of the facts (ROUTING.md §4.8)."""

from __future__ import annotations

import json

import pytest
from conftest import ROOT, qualified

from hiveroute import scorer
from hiveroute.cli import main
from hiveroute.decide import costlier_than, decide, tail_value
from hiveroute.errors import RouteError
from hiveroute.evaluate import reader_report
from hiveroute.log import read
from hiveroute.scoreeval import qualify_reader
from hiveroute.serve import run_decision
from hiveroute.table import Table, load_table
from hiveroute.usage import collect

BASE = load_table(ROOT / "examples" / "1-hive.yaml")


def table(mode: str = "live", **reader) -> Table:
    data = json.loads(json.dumps(BASE.data))
    data["reader"] = {"route": "kev-4b", "mode": mode, **reader}
    return Table.from_data(data)


def state(t: Table, reader_status: str | None = "qualified", pin: str | None = None) -> dict:
    s = {"as_of": "2026-10-08T00:00:00Z", "routes": qualified(t)}
    if reader_status:
        s["reader"] = {"status": reader_status, "reader_pin": pin or scorer.reader_pin(t)}
    return s


def answers(**probs) -> str:
    """A System One reply giving each fact's probabilities (verification as two questions)."""
    out = {}
    for f, p in probs.items():
        if f == "verification":
            checked = 1 - p.get("none", 0)
            ind = p.get("independent", 0) / checked if checked else 0.5
            out["verification.checked"] = {"type": "noul", "noul": checked}
            out["verification.controlled"] = {
                "type": "choice", "choice": "independent" if ind >= 0.5 else "weak",
                "probabilities": {"independent": ind, "weak": 1 - ind}}
        else:
            out[f] = {"type": "choice", "choice": max(p, key=p.get), "probabilities": p}
    return json.dumps({"answers": out})


SURE_CHEAP = dict(specification={"explicit": 0.97, "partial": 0.02, "goal_only": 0.01},
                  verification={"independent": 0.95, "weak": 0.04, "none": 0.01},
                  scope={"single": 0.96, "few": 0.03, "many": 0.01},
                  consequence={"reversible": 0.97, "costly": 0.03})


def request(**facts) -> dict:
    return {"task": "t", "attempt": "t.worker.0", "facts": {"kind": "work", "leverage": 0,
                                                            **facts}}


def with_reading(t: Table, req: dict, **probs) -> dict:
    return {**req, "read_facts": probs, "reader_pin": scorer.reader_pin(t)}


def note(decision: dict) -> str:
    [r] = [r for r in decision["reasons"] if r["rule"] == "reader"]
    return r["note"]


# --------------------------------------------------------------------------- resolution
def test_tail_value_and_costlier_than():
    p = {"reversible": 0.92, "costly": 0.08}
    assert costlier_than("consequence", "reversible", p) == pytest.approx(0.08)
    assert tail_value("consequence", p, 0.1) == "reversible"
    assert tail_value("consequence", p, 0.05) == "costly"
    assert tail_value("scope", {"single": 0.5, "few": 0.45, "many": 0.05}, 0.1) == "few"


def test_unknown_facts_take_the_tail_value_when_live_and_qualified():
    t = table()
    d = decide(with_reading(t, request(), **SURE_CHEAP), t, state(t))
    assert {k: d["facts"][k]["source"] for k in SURE_CHEAP} == dict.fromkeys(SURE_CHEAP, "read")
    assert d["computed_tier"] == "light"
    assert note(d) == "reading used"


def test_a_confident_reading_raises_a_stated_fact_and_never_lowers_one():
    t = table()
    probs = {**SURE_CHEAP, "consequence": {"reversible": 0.05, "costly": 0.95}}
    req = request(specification="partial", verification="independent", scope="few",
                  consequence="reversible")
    d = decide(with_reading(t, req, **probs), t, state(t))
    assert d["facts"]["consequence"] == {"value": "costly", "source": "raised",
                                         "stated": "reversible"}
    # specification: the reader is sure it's explicit, cheaper than stated; it stays partial
    assert d["facts"]["specification"] == {"value": "partial", "source": "supplied"}
    assert "raised consequence reversible -> costly" in note(d)


@pytest.mark.parametrize("mode, status, pin, why", [
    ("shadow", "qualified", None, "the reader isn't live"),
    ("live", "candidate", None, "the reader isn't qualified"),
    ("live", None, None, "the reader isn't qualified"),
    ("live", "qualified", "sha256:" + "0" * 64, "the reader is qualified for another version"),
])
def test_an_unusable_reading_changes_nothing(mode, status, pin, why):
    t = table(mode)
    req = request()
    plain = decide(req, t, state(t, status, pin))
    d = decide(with_reading(t, req, **SURE_CHEAP), t, state(t, status, pin))
    assert d["facts"] == plain["facts"] and d["computed_tier"] == plain["computed_tier"]
    assert note(d) == f"reading not used: {why}"


def test_wording_or_text_cap_changes_void_the_qualification():
    assert scorer.reader_pin(table()) != scorer.reader_pin(table(max_chars=5000))


# --------------------------------------------------------------------------- the table
def test_a_reader_needs_a_systemone_route_and_tails_below_raise_at():
    with pytest.raises(RouteError, match="systemone"):
        Table.from_data({**json.loads(json.dumps(BASE.data)),
                         "reader": {"route": "qwen-local", "mode": "shadow"}})
    with pytest.raises(RouteError, match="below raise_at"):
        table(raise_at=0.5, tail={"scope": 0.6})


def test_text_leaving_the_host_needs_egress_allowed():
    data = json.loads(json.dumps(BASE.data))
    data["routes"]["jev"] = {"tier": "local", "pool": "kev-gpu", "family": "jev",
                             "harness": "systemone", "endpoint": "https://api.typesafe.ai",
                             "model": "jev-latest", "api_key_env": "JEV_API_KEY", "tools": False}
    data["reader"] = {"route": "jev", "mode": "shadow"}
    with pytest.raises(RouteError, match="egress: allowed"):
        Table.from_data(data)
    data["routes"]["jev"]["egress"] = "allowed"
    Table.from_data(data)


# --------------------------------------------------------------------------- reading and logging
def test_shadow_reading_is_logged_with_its_tier_and_never_used(tmp_path, monkeypatch):
    t = table("shadow")
    monkeypatch.setitem(scorer.CALLERS, "systemone", lambda *a: answers(**SURE_CHEAP))
    monkeypatch.setitem(scorer.CALLERS, "ollama", lambda *a: "{}")
    log = str(tmp_path / "log.jsonl")
    req = {**request(consequence="costly"), "writer": "cos"}
    d = run_decision(t, req, state(t), "live", log, "Fix the pager.")
    assert d["facts"]["specification"]["source"] == "default"
    [r] = [e["data"] for e in read(log) if e["type"] == "route.read"]
    assert (r["writer"], r["stated"]) == ("cos", {"consequence": "costly"})
    # costly stays (never lowered), but with an independent check it no longer needs strong
    assert (r["tier_without"], r["tier_with_reading"]) == ("strong", "light")
    rep = reader_report(log)
    assert rep["tier_with_reading"] == {"lower": 1}
    assert rep["facts"]["cos / consequence"] == {"stated": 1, "too costly (confidently cheaper)": 1}
    assert rep["facts"]["cos / scope"] == {"unknown, filled": 1}
    assert main(["replay", log]) == 0


def test_live_qualified_reading_enters_the_request_and_replays(tmp_path, monkeypatch):
    t = table()
    monkeypatch.setitem(scorer.CALLERS, "systemone", lambda *a: answers(**SURE_CHEAP))
    monkeypatch.setitem(scorer.CALLERS, "ollama", lambda *a: "{}")
    log = str(tmp_path / "log.jsonl")
    d = run_decision(t, request(), state(t), "live", log, "Fix the pager.")
    assert d["computed_tier"] == "light"
    [dec] = [e["data"] for e in read(log) if e["type"] == "route.decided"]
    assert "read_facts" in dec["request"]
    assert main(["replay", log]) == 0


def test_a_failed_reading_leaves_the_decision_as_without_it(tmp_path, monkeypatch):
    t = table()
    monkeypatch.setitem(scorer.CALLERS, "systemone", lambda *a: "not json")
    monkeypatch.setitem(scorer.CALLERS, "ollama", lambda *a: "{}")
    log = str(tmp_path / "log.jsonl")
    d = run_decision(t, request(), state(t), "live", log, "x")
    assert d["computed_tier"] == "strong"
    [r] = [e["data"] for e in read(log) if e["type"] == "route.read"]
    assert r["probabilities"] == {} and r["error"]
    assert reader_report(log)["failed"] == 1


# --------------------------------------------------------------------------- qualification
def write_suite(path, facts):
    path.write_text("".join(json.dumps({"id": f"c{i}", "source": "authored", "kind": "work",
                                        "text": "task", "facts": f}) + "\n"
                            for i, f in enumerate(facts)))


def test_reader_qualifies_into_the_state_and_fails_when_it_lowers_tiers(tmp_path):
    t = table()
    suite = tmp_path / "suite.jsonl"
    cheap = {"specification": "explicit", "verification": "independent", "scope": "single",
             "consequence": "reversible"}
    write_suite(suite, [cheap] * 10)
    q = tmp_path / "q.json"
    sources = {"format": "hive-route.sources/1", "qualifications": str(q)}
    log = str(tmp_path / "log.jsonl")
    e = qualify_reader(t, str(suite), sources, log, caller=lambda *a: answers(**SURE_CHEAP))
    assert (e["status"], e["tier_lower"], e["errors"]) == ("qualified", 0, 0)
    st, _ = collect(t, sources, __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc))
    assert st["reader"] == {"status": "qualified", "reader_pin": scorer.reader_pin(t)}
    costly = {**cheap, "consequence": "costly", "verification": "none"}
    write_suite(suite, [costly] * 10)
    e = qualify_reader(t, str(suite), sources, log, caller=lambda *a: answers(**SURE_CHEAP))
    assert (e["status"], e["tier_lower"]) == ("candidate", 10)
    assert json.loads(q.read_text())["reader:kev-4b"]["status"] == "candidate"
