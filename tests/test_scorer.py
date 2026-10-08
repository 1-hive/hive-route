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
    monkeypatch.setitem(scorer.CALLERS, "systemone", lambda *a: kev_reply(scope="few"))
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"task": "t", "attempt": "t.worker.0", "facts": {"kind": "work"}}))
    text = tmp_path / "task.md"
    text.write_text("Fix the off-by-one in stats.py; tests in test_stats.py must pass.")
    log = tmp_path / "log.jsonl"
    assert main(["decide", TABLE_PATH, str(req), "--mode", "fixed", "--task-text", str(text),
                 "--log", str(log)]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["facts"]["scope"]["source"] == "default"  # shadow: not used
    [scored] = [e["data"] for e in read(log) if e["type"] == "route.scored"
                and e["data"]["route_id"] == "qwen-local"]
    assert (scored["tier_without"], scored["tier_with_estimates"]) == ("strong", "light")
    assert main(["replay", str(log)]) == 0


def kev_reply(**facts) -> str:
    """A System One answer: each fact's choice with probability 0.8."""
    answers = {}
    for f, v in facts.items():
        if f == "verification":  # asked as two questions
            answers["verification.checked"] = {"type": "noul", "noul": 0.1 if v == "none" else 0.9}
            ctl = "weak" if v == "none" else v
            answers["verification.controlled"] = {
                "type": "choice", "choice": ctl,
                "probabilities": {ctl: 0.8, ("independent" if ctl == "weak" else "weak"): 0.2}}
            continue
        rest = [x for x in scorer.VALUES[f] if x != v]
        probs = {v: 0.8, **{x: round(0.2 / len(rest), 4) for x in rest}}
        answers[f] = {"type": "choice", "choice": v, "confidence": 0.6, "probabilities": probs}
    return json.dumps({"model": "kev-latest", "answers": answers})


def test_systemone_asks_one_choice_per_fact_and_keeps_probabilities():
    seen = {}

    def call(route, cfg, body):
        seen.update(body)
        return kev_reply(specification="explicit", scope="few")

    est = scorer.estimate(TABLE, {"route": "kev-4b", "mode": "shadow"}, "work",
                          ["specification", "scope"], "text", caller=call)
    assert set(seen["questions"]) == {"specification", "scope"}
    assert set(seen["questions"]["scope"]["criteria"]) == {"single", "few", "many"}
    assert est["estimates"]["scope"]["value"] == "few"
    assert est["estimates"]["scope"]["p"] == 0.8
    assert est["error"] is None


def test_systemone_min_probability_leaves_fact_unknown():
    est = scorer.estimate(TABLE, {"route": "kev-4b", "mode": "shadow", "min_probability": 0.9},
                          "work", ["scope"], "text", caller=lambda *a: kev_reply(scope="few"))
    assert est["estimates"] == {}
    assert est["error"] == "no valid estimates in the output"


@pytest.mark.parametrize("content", ["", "{}", '{"answers": {"scope": {"choice": "huge"}}}'])
def test_systemone_invalid_output_is_discarded(content):
    est = scorer.estimate(TABLE, {"route": "kev-4b", "mode": "shadow"}, "work", ["scope"], "x",
                          caller=lambda *a: content)
    assert est["estimates"] == {}


def test_shadow_scorers_are_logged_and_never_used(tmp_path, capsys, monkeypatch):
    data = json.loads(json.dumps(TABLE.data))
    data["scorer"]["mode"] = "live"
    path = tmp_path / "t.json"
    path.write_text(json.dumps(data))
    monkeypatch.setitem(scorer.CALLERS, "ollama", lambda *a: reply(specification="partial"))
    monkeypatch.setitem(scorer.CALLERS, "systemone", lambda *a: kev_reply(
        specification="explicit", verification="independent", scope="few",
        consequence="reversible"))
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"task": "t", "attempt": "t.worker.0", "facts": {
        "kind": "work", "verification": "independent", "scope": "few",
        "consequence": "reversible", "leverage": 0}}))
    text = tmp_path / "task.md"
    text.write_text("Make the pager configurable.")
    log = tmp_path / "log.jsonl"
    main(["decide", str(path), str(req), "--mode", "live", "--task-text", str(text),
          "--log", str(log)])  # no route qualified: exit 3, but the facts are what matter
    d = json.loads(capsys.readouterr().out)
    assert d["facts"]["specification"] == {"value": "partial", "source": "estimated"}
    scored = {e["data"]["route_id"]: e["data"] for e in read(log) if e["type"] == "route.scored"}
    assert scored["kev-4b"]["mode"] == "shadow"
    assert scored["kev-4b"]["estimates"]["specification"]["value"] == "explicit"
    assert main(["replay", str(log)]) == 0


def test_scorer_eval_reports_accuracy_and_cheaper_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(scorer.CALLERS, "systemone", lambda *a: kev_reply(
        specification="explicit", verification="independent", scope="single",
        consequence="reversible"))
    suite = tmp_path / "suite.jsonl"
    labels = {"specification": "explicit", "verification": "independent", "scope": "few",
              "consequence": "reversible"}
    suite.write_text(json.dumps({"id": "a", "source": "authored", "kind": "work",
                                 "text": "Route facts: scope=single\nDo it.", "facts": labels})
                     + "\n")
    out = tmp_path / "rows.jsonl"
    assert main(["scorer-eval", TABLE_PATH, str(suite), "--scorer", "kev-4b",
                 "--out", str(out)]) == 0
    s = json.loads(capsys.readouterr().out)["kev-4b"]
    assert s["accuracy"]["scope (all)"] == "0/1"
    assert s["accuracy"]["specification (authored)"] == "1/1"
    assert s["cheaper_than_label"] == {"scope": 1}
    assert s["by_probability"]["p>=0.7"] == {"coverage": 1.0, "accuracy": 0.75}
    assert json.loads(out.read_text().splitlines()[0])["case"] == "a"


def test_scorer_eval_strips_route_facts_lines():
    from hiveroute.scoreeval import case_text
    assert case_text({"id": "a", "text": "x\nRoute facts: scope=few\nReview tier: light\ny"},
                     None, None) == "x\n\n\ny"


@pytest.mark.parametrize("given, value, p", [("none", "none", 0.9), ("weak", "weak", 0.72),
                                             ("independent", "independent", 0.72)])
def test_systemone_verification_is_two_questions(given, value, p):
    seen = {}

    def call(route, cfg, body):
        seen.update(body["questions"])
        return kev_reply(verification=given)

    est = scorer.estimate(TABLE, {"route": "kev-4b", "mode": "shadow"}, "work", ["verification"],
                          "text", caller=call)
    assert set(seen) == {"verification.checked", "verification.controlled"}
    assert est["estimates"]["verification"]["value"] == value
    assert est["estimates"]["verification"]["p"] == p


def test_systemone_sends_bearer_key_from_env(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return kev_reply(scope="few").encode()

    def urlopen(req, timeout):
        seen.update(url=req.full_url, auth=req.get_header("Authorization"))
        return Resp()

    monkeypatch.setattr(scorer.urllib.request, "urlopen", urlopen)
    route = {"endpoint": "https://api.typesafe.ai", "api_key_env": "JEV_KEY"}
    monkeypatch.delenv("JEV_KEY", raising=False)
    with pytest.raises(ValueError, match="JEV_KEY is not set"):
        scorer.call_systemone(route, {}, {})
    monkeypatch.setenv("JEV_KEY", "k")
    scorer.call_systemone(route, {}, {})
    assert seen == {"url": "https://api.typesafe.ai/v1/systemone", "auth": "Bearer k"}
