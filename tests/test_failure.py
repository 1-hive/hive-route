# SPDX-License-Identifier: GPL-3.0-or-later
"""What to do after a failed model call (failure.py, ROUTING.md §9.4), and /episodes/failure."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

import pytest
from test_serve import EXPLICIT, call, svc  # noqa: F401  (the service fixture)

from hiveroute.canonical import UTC
from hiveroute.failure import classify, status_of

NOW = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)


def c(report, routed=False, **policy):
    return classify(report, policy or None, routed=routed, now=NOW)


# --- the laptop's terminal-provider stop: 400/401/402/403/413 are never retried --------------

@pytest.mark.parametrize("status", [400, 401, 402, 403, 413])
def test_terminal_statuses_are_never_retried(status):
    v = c({"status": status, "error": "APIStatusError"})
    assert v["action"] == "stop" and v["terminal"]
    assert c({"status": status}, routed=True)["action"] in ("reroute", "stop")


def test_a_wrapped_status_in_the_message_counts():
    # the laptop's real error, from a background branch
    msg = ("Background LLM call failed: APIStatusError: Error code: 402 - "
           "{'error': {'message': 'Prompt tokens limit exceeded'}}")
    assert status_of({"message": msg}) == 402
    v = c({"error": "Exception", "message": msg})
    assert v["class"] == "capacity" and v["action"] == "stop"
    assert v["limited_until"] == "2026-10-23T20:30:00Z"     # down_minutes, the provider said nothing


def test_context_overflow_stops_even_when_routed():
    v = c({"status": 400, "message": "This model's maximum context length is 200000 tokens"}, routed=True)
    assert v["class"] == "truncated" and v["action"] == "stop"
    assert c({"status": 413})["class"] == "truncated"


def test_credentials_are_an_outage_of_the_pool():
    v = c({"status": 401}, routed=True)
    assert v["class"] == "outage" and v["action"] == "reroute" and v["terminal"]


# --- VM2's LONG_STEP: a long call is work in progress, retried once in parts -----------------

def test_a_first_timeout_is_a_long_step():
    v = c({"error": "APITimeoutError", "message": "Request timed out.", "elapsed_s": 300})
    assert v == {"class": "truncated", "action": "retry", "reason": v["reason"], "terminal": False,
                 "wait_s": 0.0, "hint": "continue_in_parts"}


def test_a_server_error_after_running_long_is_a_long_step_but_a_quick_one_is_an_outage():
    late = {"status": 500, "message": "internal error", "elapsed_s": 130}
    assert c(late)["hint"] == "continue_in_parts"
    quick = {**late, "elapsed_s": 3}
    assert c(quick) == {"class": "outage", "action": "retry", "reason": "provider error; one transport retry",
                        "terminal": False, "wait_s": 15.0}
    assert "hint" not in c(late, long_step_seconds=200)


def test_a_second_long_step_counts_as_a_provider_failure():
    r = {"error": "APITimeoutError", "elapsed_s": 300, "retries": {"long_step": 1}}
    assert c(r)["action"] == "retry" and c(r)["class"] == "outage"            # the transport retry
    r["retries"]["transport"] = 1
    assert c(r)["action"] == "stop" and c(r, routed=True)["action"] == "reroute"


# --- rate limits and quotas -------------------------------------------------------------------

def test_a_short_rate_limit_is_waited_out_once():
    r = {"status": 429, "retry_after_s": 20}
    assert c(r)["action"] == "retry" and c(r)["wait_s"] == 20.0
    r["retries"] = {"capacity": 1}
    v = c(r, routed=True)
    assert v["action"] == "reroute" and not v["terminal"] and v["limited_until"] == "2026-10-23T20:00:20Z"


def test_a_long_rate_limit_or_a_usage_limit_moves_on():
    assert c({"status": 429, "retry_after_s": 3600}, routed=True)["action"] == "reroute"
    v = c({"status": 429, "message": "You've hit your usage limit; next reset in 3h"}, routed=True)
    assert v["class"] == "capacity" and v["action"] == "reroute"


def test_transport_retries_back_off_and_follow_the_policy():
    r = {"error": "APIConnectionError"}
    assert c(r)["wait_s"] == 15.0
    assert c({**r, "retries": {"transport": 1}}, transport_retries=3)["wait_s"] == 30.0
    assert c({**r, "retries": {"transport": 3}}, transport_retries=3)["action"] == "stop"


def test_anything_else_is_the_agents_own_error():
    assert c({"error": "KeyError", "message": "'tool_calls'"})["action"] == "agent"


# --- the endpoint -----------------------------------------------------------------------------

def test_without_an_episode_it_only_classifies(svc):  # noqa: F811
    code, d = call(svc, "/episodes/failure", {"status": 402, "message": "insufficient credits"})
    assert code == 200 and d["action"] == "stop" and d["ended"] is False


def test_a_routed_episode_is_ended_with_the_class_and_the_next_route_avoids_it(svc):  # noqa: F811
    code, d = call(svc, "/route", {"task": "q", "episode": "e1", "facts": EXPLICIT})
    assert d["decision"] == "route"
    code, v = call(svc, "/episodes/failure", {"episode": "e1", "status": 429, "retry_after_s": 30})
    assert v["action"] == "retry" and v["ended"] is False                 # waited out once
    code, v = call(svc, "/episodes/failure", {"episode": "e1", "status": 429, "retry_after_s": 30,
                                              "retries": {"capacity": 1}, "models": [d["model"]]})
    assert v["action"] == "reroute" and v["ended"] is True and v["drift"] == []
    ep = svc.history.episodes("iter-a")["iter-a.e1"]
    assert ep["class"] == "capacity" and "limited_until" in ep
    code, _ = call(svc, "/episodes/end", {"episode": "e1", "models": []})
    assert code == 409                                                    # ended once
    code, d2 = call(svc, "/route", {"task": "q", "episode": "e2", "facts": EXPLICIT})
    # the pool is at its limit: another pool, or (the fixture has no other) wait
    assert d2["decision"] == "wait" or d2["pool"] != d["pool"]


def test_the_agents_policy_applies(svc):  # noqa: F811
    svc.agents["iter-a"]["failure_policy"] = {"transport_retries": 0}
    _, v = call(svc, "/episodes/failure", {"error": "APIConnectionError"})
    assert v["action"] == "stop"


@pytest.mark.parametrize("body", [{"status": 99}, {"elapsed_s": -1}, {"retries": {"other": 1}},
                                  {"message": "x" * 2001}, {"surprise": 1}, {"episode": "Bad Id"}])
def test_bad_reports_are_refused(svc, body):  # noqa: F811
    code, d = call(svc, "/episodes/failure", body)
    assert code == 400 and d["error"] == "REQUEST_INVALID"


def test_an_unknown_episode_is_not_found(svc):  # noqa: F811
    code, _ = call(svc, "/episodes/failure", {"episode": "nope", "status": 500})
    assert code == 404


# --- the reference client (examples/failure_client.py) ----------------------------------------

def _client():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parent.parent / "examples" / "failure_client.py"
    spec = importlib.util.spec_from_file_location("failure_client", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    status_code = 429
    headers: ClassVar[dict] = {"retry-after": "12"}


class _RateLimitError(Exception):
    def __init__(self):
        super().__init__("Error code: 429 - rate limited")
        self.response = _Resp()


def test_the_client_reports_an_openai_style_error_and_counts_its_retries(svc):  # noqa: F811
    m = _client()
    assert m.error_report(_RateLimitError(), 3) == {"error": "_RateLimitError", "status": 429,
                                                    "message": "Error code: 429 - rate limited",
                                                    "retry_after_s": 12.0, "elapsed_s": 3.0}
    fc, retries = m.FailureClient(svc.url, token="tok-a"), {}
    v = fc.report(_RateLimitError(), 3, retries)
    assert v["action"] == "retry" and v["wait_s"] == 12.0
    fc.count(retries, v)
    assert retries == {"capacity": 1} and fc.report(_RateLimitError(), 3, retries)["action"] == "stop"


def test_the_client_falls_back_to_one_cautious_retry():
    m = _client()
    fc = m.FailureClient("http://127.0.0.1:9", token="x", timeout=0.5, fallback_wait_s=7)
    v = fc.report(TimeoutError("timed out"))
    assert v["local"] and v["action"] == "retry" and v["wait_s"] == 7
    assert fc.report(TimeoutError("timed out"), retries={"transport": 1})["action"] == "stop"
