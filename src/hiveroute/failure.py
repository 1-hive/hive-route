# SPDX-License-Identifier: GPL-3.0-or-later
"""What to do after a failed model call (ROUTING.md §5, §9.4): one owner for retries.

An agent with its own loop reports a failed call as it saw it (HTTP status, error type, a
message excerpt, how long the call ran, the provider's Retry-After, and the retries it has
already made for this request). ``classify`` gives the §5 class and one action:

- ``retry``: the same model again, after ``wait_s``; with ``hint`` ``continue_in_parts`` the
  agent first asks the model to continue in smaller parts (a long step, not an outage);
- ``reroute``: end the routed episode; the rest of the request runs on a new route or on the
  agent's configured chain;
- ``stop``: don't call a model again for this request; tell the person why (``reason``);
- ``agent``: not a provider failure; the agent handles it as its own error.

Pure: no I/O, the clock is passed in. The rules came from two Iter deployments: the laptop's
terminal-provider stop (400/401/402/403/413 are never retried) and VM2's LONG_STEP (a call that
runs long is work in progress, retried once in parts).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .canonical import format_time

DEFAULT_POLICY = {
    "transport_retries": 1,       # §8: at most one transport retry
    "long_step_retries": 1,       # a long step is retried once, in parts
    "long_step_seconds": 120,     # a 5xx after running this long is a long step, not an outage
    "capacity_wait_max_s": 120,   # a rate limit that frees up sooner is waited out once
    "backoff_s": 15,              # first transport retry; doubles per retry
    "down_minutes": 30,           # how long a pool counts at its limit when the provider doesn't say
}

CONTEXT = re.compile(r"context[_ ]length|maximum context|context window|prompt is too long|too many tokens"
                     r"|prompt tokens limit|request too large|input is too long", re.I)
QUOTA = re.compile(r"usage limit|next reset in|insufficient_quota|quota exceeded|exceeded your current quota"
                   r"|credit balance|out of credits", re.I)
RATE = re.compile(r"rate[ _]limit|RateLimitError|too many requests", re.I)
TIMEOUT = re.compile(r"APITimeoutError|timed out|timeout", re.I)
SERVER = re.compile(r"InternalServerError|internal error|agent run (aborted|failed)|ServiceUnavailable|Bad Gateway",
                    re.I)
CONNECTION = re.compile(r"APIConnectionError|ConnectionError|ClientDisconnect|connection (reset|refused|aborted)",
                        re.I)
STATUS_IN_TEXT = re.compile(r"\bError code:\s*(\d{3})\b|[\"']code[\"']\s*:\s*(\d{3})\b|\bstatus(?:_code)?\s*[=:]\s*(\d{3})\b",
                            re.I)


def status_of(report: dict) -> int | None:
    """The HTTP status: the reported one, else one named in the message (wrapped errors)."""
    s = report.get("status")
    if isinstance(s, int) and 100 <= s <= 599:
        return s
    m = STATUS_IN_TEXT.search(report.get("message") or "")
    return int(next(g for g in m.groups() if g)) if m else None


def classify(report: dict, policy: dict | None = None, routed: bool = False,
             now: datetime | None = None) -> dict:
    """The verdict for one failed call. ``routed``: the call ran on a routed episode, so a
    reroute is possible; otherwise a failure that retrying won't fix stops the request."""
    p = {**DEFAULT_POLICY, **(policy or {})}
    retries = report.get("retries") or {}
    status = status_of(report)
    text = f"{report.get('error') or ''}: {report.get('message') or ''}"
    elapsed = report.get("elapsed_s")
    after = report.get("retry_after_s")
    give_up = "reroute" if routed else "stop"

    def until(seconds: float | None) -> str | None:
        return format_time(now + timedelta(seconds=seconds)) if now else None

    def v(cls: str | None, action: str, reason: str, **kw: object) -> dict:
        out = {"class": cls, "action": action, "reason": reason,
               "terminal": action in ("stop", "reroute") and kw.pop("terminal", True)}
        out.update({k: x for k, x in kw.items() if x is not None})
        return out

    # Too long for the model: the same request fails the same way anywhere it can't fit.
    # (A 402 is about payment even when it names prompt tokens: OpenRouter's "Prompt tokens limit
    # exceeded" means the credit left can't pay for a prompt that size.)
    if status == 413 or (status in (400, None) and CONTEXT.search(text)):
        return v("truncated", "stop", "the request is too large for the model (context or size limit); "
                 "shorten or split it")
    if status in (401, 403):
        return v("outage", give_up, f"the provider rejected the credentials (HTTP {status})")
    if status == 402 or (status != 429 and QUOTA.search(text)):
        return v("capacity", give_up, "the provider's credit or quota is exhausted"
                 + (f" (HTTP {status})" if status else ""),
                 limited_until=until(after if isinstance(after, (int, float)) else p["down_minutes"] * 60))
    if status == 400:
        return v("outage", give_up, "the provider rejected the request as invalid (HTTP 400)")
    if status == 429 or RATE.search(text):
        if QUOTA.search(text):
            return v("capacity", give_up, "the provider's usage limit is reached",
                     limited_until=until(after if isinstance(after, (int, float)) else p["down_minutes"] * 60))
        if (isinstance(after, (int, float)) and after <= p["capacity_wait_max_s"]
                and retries.get("capacity", 0) < 1):
            return v("capacity", "retry", f"rate limited; retrying after {after:.0f}s",
                     wait_s=max(float(after), 1.0))
        return v("capacity", give_up, "rate limited", terminal=False,
                 limited_until=until(after if isinstance(after, (int, float)) else p["down_minutes"] * 60))

    server = (isinstance(status, int) and status >= 500) or bool(SERVER.search(text))
    timeout = bool(TIMEOUT.search(text)) or status in (408, 504)
    long_step = timeout or (server and isinstance(elapsed, (int, float)) and elapsed >= p["long_step_seconds"])
    if long_step and retries.get("long_step", 0) < p["long_step_retries"]:
        return v("truncated", "retry", "the step ran long without finishing; continue in smaller parts",
                 wait_s=0.0, hint="continue_in_parts")
    if server or timeout or CONNECTION.search(text) or status in (409, 529):
        n = retries.get("transport", 0)
        if n < p["transport_retries"]:
            return v("outage", "retry", "provider error; one transport retry",
                     wait_s=float(p["backoff_s"] * 2 ** n))
        return v("outage", give_up, "the provider keeps failing" + (f" (HTTP {status})" if status else ""),
                 terminal=False)
    return v(None, "agent", "not a provider failure")
