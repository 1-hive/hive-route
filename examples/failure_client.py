# SPDX-License-Identifier: GPL-3.0-or-later
"""Ask the route service what to do after a failed model call (ROUTING.md §9.4).

Standard library only: copy this file into an agent (e.g. an Iter folder) and replace the
agent's own error classification with it. Every agent then retries, waits, reroutes and stops
by the same rules, kept in one place (hive-route's failure.py).

    from failure_client import FailureClient
    fc = FailureClient("http://172.18.0.1:8480", token_file="/hive/me/secrets/hive-route.token")
    retries = {}                                        # per request: what this request has retried
    ...
    except Exception as error:
        v = fc.report(error, elapsed_s=time.time() - started, retries=retries, episode=episode_or_None)
        if v["action"] == "retry":
            fc.count(retries, v); time.sleep(v.get("wait_s", 0))
            if v.get("hint") == "continue_in_parts": messages.append(<a note asking for smaller parts>)
            continue
        if v["action"] == "reroute": <the episode is ended (v["ended"]); route anew, or use the configured chain>
        if v["action"] == "stop": <tell the person v["reason"]; don't call a model again for this request>
        if v["action"] == "agent": <not a provider failure: the agent's own error handling>

If the service can't be reached, the answer is local and cautious: one retry after
``fallback_wait_s``, then stop. It's marked ``"local": true``; count those, as for episodes that
weren't routed.
"""

from __future__ import annotations

import json
import urllib.request

COUNTED = {"truncated": "long_step", "capacity": "capacity", "outage": "transport"}


def error_report(error: BaseException, elapsed_s: float | None = None) -> dict:
    """The report for an exception from an OpenAI-style client: status, type, a short message,
    and the provider's Retry-After. Nothing from the request (no prompt text, no key)."""
    report: dict = {"error": type(error).__name__[:200], "message": str(error)[:500]}
    for candidate in (error, getattr(error, "response", None)):
        status = getattr(candidate, "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            report["status"] = status
            break
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    try:
        after = headers.get("retry-after") or headers.get("Retry-After")
        if after is not None and 0 <= float(after) <= 86400:
            report["retry_after_s"] = float(after)
    except (TypeError, ValueError, AttributeError):
        pass
    if elapsed_s is not None:
        report["elapsed_s"] = round(min(max(float(elapsed_s), 0.0), 86400.0), 1)
    return report


class FailureClient:
    def __init__(self, url: str, token: str | None = None, token_file: str | None = None,
                 timeout: float = 5.0, fallback_wait_s: float = 30.0):
        self.url, self.timeout, self.fallback_wait_s = url.rstrip("/"), timeout, fallback_wait_s
        self._token, self._token_file = token, token_file

    def _bearer(self) -> str | None:
        if self._token:
            return self._token
        if self._token_file:
            try:
                with open(self._token_file, encoding="ascii") as f:
                    return f.read().strip() or None
            except OSError:
                return None
        return None

    def report(self, error: BaseException, elapsed_s: float | None = None, retries: dict | None = None,
               episode: str | None = None, models: list[str] | None = None) -> dict:
        body = error_report(error, elapsed_s)
        if retries:
            body["retries"] = {k: v for k, v in retries.items() if k in ("transport", "long_step", "capacity")}
        if episode:
            body["episode"] = episode
            if models:
                body["models"] = sorted(set(models))[:32]
        token = self._bearer()
        if token:
            req = urllib.request.Request(self.url + "/episodes/failure", data=json.dumps(body).encode(),
                                         method="POST", headers={"Content-Type": "application/json",
                                                                 "Authorization": "Bearer " + token})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except Exception:
                pass
        return self.local(retries)

    def local(self, retries: dict | None) -> dict:
        """When the service can't answer: one retry, then stop."""
        if sum((retries or {}).values()) < 1:
            return {"class": None, "action": "retry", "wait_s": self.fallback_wait_s, "terminal": False,
                    "reason": "route service unreachable; one cautious retry", "ended": False, "local": True}
        return {"class": None, "action": "stop", "terminal": False, "ended": False, "local": True,
                "reason": "route service unreachable and the retry failed"}

    @staticmethod
    def count(retries: dict, verdict: dict) -> None:
        """Count a retry the agent is about to make, under the kind the service will look at."""
        key = "long_step" if verdict.get("hint") == "continue_in_parts" else COUNTED.get(verdict.get("class"),
                                                                                          "transport")
        retries[key] = retries.get(key, 0) + 1
