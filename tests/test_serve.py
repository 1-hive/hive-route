# SPDX-License-Identifier: GPL-3.0-or-later
"""The route service (serve.py, ROUTING.md §9.2): per-episode routing over HTTP."""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request

import pytest
from conftest import fixture_table, qualified

from hiveroute.errors import RouteError
from hiveroute.log import read, replay
from hiveroute.observe import same_model
from hiveroute.serve import Service, load_agents, make_server

TABLE = fixture_table()
EXPLICIT = {"specification": "explicit", "scope": "few"}


def sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


@pytest.fixture
def svc(tmp_path):
    agents = tmp_path / "agents.yaml"
    agents.write_text(json.dumps({"format": "hive-route.agents/1", "agents": {
        "iter-a": {"token_sha256": sha("tok-a"), "kinds": ["work", "consult"],
                   "facts": {"verification": "independent", "consequence": "reversible",
                             "leverage": 0}},
        "iter-b": {"token_sha256": sha("tok-b"), "kinds": ["work"]}}}))
    quals = tmp_path / "quals.json"
    quals.write_text(json.dumps(qualified(TABLE)))
    sources = {"format": "hive-route.sources/1", "qualifications": str(quals)}
    s = Service(TABLE, load_agents(agents), sources, str(tmp_path / "log.jsonl"),
                tmp_path / "state", mode="live")
    srv = make_server(s, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield s
    srv.shutdown()


def call(svc, path: str, body: dict | None = None, token: str | None = "tok-a") -> tuple[int, dict]:
    req = urllib.request.Request(svc.url + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_episode_facts_lower_the_tier_and_the_log_replays(svc):
    code, d = call(svc, "/route", {"task": "q1", "episode": "e1", "facts": EXPLICIT})
    # light, run one tier up: the fixture's light route is metered with unknown spend (RT3)
    assert code == 200 and d["decision"] == "route" and d["computed_tier"] == "light"
    assert d["attempt"] == "iter-a.e1" and d["task"] == "iter-a/q1"
    code, d = call(svc, "/route", {"task": "q2", "episode": "e2"})   # no episode facts: costly defaults
    assert d["computed_tier"] == "standard"                         # F2, F3
    assert replay(svc.log) == (2, [])


def test_an_agent_cant_state_its_fixed_facts(svc):
    code, d = call(svc, "/route", {"task": "q", "episode": "e", "facts": {"consequence": "reversible"}},
                   token="tok-b")
    assert code == 400 and "scope, specification" in d["message"]
    code, d = call(svc, "/route", {"task": "q", "episode": "e", "kind": "consult"}, token="tok-b")
    assert code == 400


def test_tokens_and_ids(svc):
    assert call(svc, "/route", {"task": "q", "episode": "e"}, token=None)[0] == 401
    assert call(svc, "/route", {"task": "q", "episode": "e"}, token="wrong")[0] == 401
    assert call(svc, "/route", {"task": "../x", "episode": "e"})[0] == 400
    assert call(svc, "/route", {"task": "q", "episode": "e", "model": "x"})[0] == 400
    assert call(svc, "/health")[1]["mode"] == "live"


def test_ended_episodes_become_the_tasks_history(svc):
    call(svc, "/route", {"task": "q", "episode": "e1", "facts": EXPLICIT})
    assert call(svc, "/route", {"task": "q", "episode": "e1"})[0] == 409     # one decision per episode
    end = {"class": "failed_check", "models": []}
    assert call(svc, "/episodes/end", {"episode": "e1", **end})[1] == {"ok": True, "drift": []}
    assert call(svc, "/episodes/end", {"episode": "e1", **end})[0] == 409
    assert call(svc, "/episodes/end", {"episode": "nope", **end})[0] == 404
    assert call(svc, "/episodes/end", {"episode": "e1", "class": "fine", "models": []})[0] == 400
    call(svc, "/route", {"task": "q", "episode": "e2", "facts": EXPLICIT})
    call(svc, "/episodes/end", {"episode": "e2", **end})
    _, d = call(svc, "/route", {"task": "q", "episode": "e3", "facts": EXPLICIT})
    # two failed checks on standard (light ran one tier up): one tier above, F8
    assert d["computed_tier"] == "strong" and "F8" in {r["rule"] for r in d["reasons"]}
    req = [e for e in read(svc.log) if e["type"] == "route.decided"][-1]["data"]["request"]
    assert req["reason"] == "restart" and [h["class"] for h in req["history"]] == ["failed_check"] * 2
    # another agent's episodes with the same ids are its own
    assert call(svc, "/route", {"task": "q", "episode": "e1"}, token="tok-b")[0] == 200


def test_a_fact_both_fixed_and_per_episode_is_refused(tmp_path):
    f = tmp_path / "agents.yaml"
    f.write_text(json.dumps({"format": "hive-route.agents/1", "agents": {"a": {
        "token_sha256": sha("t"), "kinds": ["work"], "facts": {"scope": "few"}}}}))
    with pytest.raises(RouteError, match="both fixed"):
        load_agents(f)


def test_reported_models_are_checked_for_drift(svc, tmp_path):
    _, d = call(svc, "/route", {"task": "q", "episode": "e1", "facts": EXPLICIT})
    rid, model = d["route_id"], d["model"]
    # a success: no class, the models the provider reported; a dated snapshot is the same model
    assert call(svc, "/episodes/end", {"episode": "e1", "models": [model + "-20261001"]})[1] == \
        {"ok": True, "drift": []}
    _, d = call(svc, "/route", {"task": "q", "episode": "e2", "facts": EXPLICIT})
    assert d["route_id"] == rid
    assert call(svc, "/episodes/end", {"episode": "e2", "models": ["other-model"]})[1] == \
        {"ok": True, "drift": ["other-model"]}
    ev = [e for e in read(svc.log) if e["type"] == "route.drift_detected"]
    assert len(ev) == 1 and ev[0]["data"]["source"] == "agent report"
    quals = json.loads((tmp_path / "quals.json").read_text())
    assert quals[rid]["status"] == "candidate"                  # demoted, as for attempts
    _, d = call(svc, "/route", {"task": "q", "episode": "e3", "facts": EXPLICIT})
    assert d["route_id"] != rid                                  # the next decision sees it
    # a success isn't history: no failure class, no tier change
    req = [e for e in read(svc.log) if e["type"] == "route.decided"][-1]["data"]["request"]
    assert "history" not in req
    assert call(svc, "/episodes/end", {"episode": "e3"})[0] == 400   # models is required


def test_same_model():
    assert same_model("anthropic/claude-opus-5-5", "claude-opus-5-5")
    assert same_model("claude-opus-5-5", "claude-opus-5-5-20261001")
    assert same_model("gpt-6-astra", "gpt-6-astra-2026-09-30")
    assert not same_model("claude-opus-5-5", "claude-opus-4-6")
    assert not same_model("claude-sonnet-5", "claude-sonnet-5-mini")   # not a snapshot
    assert not same_model("anthropic/claude-opus-5-5", "openai/claude-opus-5-5x")
