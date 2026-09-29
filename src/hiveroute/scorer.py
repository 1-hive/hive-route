# SPDX-License-Identifier: GPL-3.0-or-later
"""The scorer (ROUTING.md §4.3): a small fixed model that estimates unknown task facts.

It is called only when a fact that could change the tier is unknown: the tier with the
unknown facts at their conservative defaults differs from the tier with them at their
cheapest values. It estimates those facts from the task's text and history; it never
overrides a supplied fact and never sets the tier. Its output must match a fixed
schema; anything else is discarded and the facts stay unknown.

In ``shadow`` mode (the table's ``scorer.mode``) the estimates are logged as
``route.scored`` and not used. In ``live`` mode they go into the request as
``estimated_facts``, so the decision (and its replay) uses them with source
``estimated``.

The scorer's route is called directly, never routed. Supported harnesses: ``ollama``
(the native chat API with a JSON schema).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .decide import FACTS, compute_tier, resolve_facts
from .errors import RouteError
from .table import Table

# The facts' values from cheapest to costliest (§4.1).
VALUES = {
    "specification": ["explicit", "partial", "goal_only"],
    "verification": ["independent", "weak", "none"],
    "scope": ["single", "few", "many"],
    "consequence": ["reversible", "costly"],
}
DEFINITIONS = """\
- specification: "explicit" if the task pins a plan or interface and an acceptance check;
  "partial" if it describes what to do but leaves parts of the approach or the acceptance
  open; "goal_only" if it states only a goal and the plan must be discovered.
- verification: "independent" if a check the worker can't edit decides success (protected
  tests, a reference result, a reviewer with a stated check); "weak" if success rests on
  tests the worker writes or on judgment; "none" if there is no check at all.
- scope: "single" for one file or component; "few" for two to four; "many" for more, or
  several interacting subsystems.
- consequence: "costly" if the work spends external compute, changes shared or production
  state, or is hard to undo; otherwise "reversible"."""

SYSTEM = f"""You estimate facts about a software task, to decide how capable a model it needs.
You do not do the task. Answer only for the facts you are asked about, from the task's text.
Definitions:
{DEFINITIONS}
When the text doesn't settle a fact, choose the costlier value. Reply with JSON only."""

Caller = Callable[[dict, dict, dict], str]


def could_lower(request: dict, table: Table) -> list[str]:
    """The unknown facts, if setting them to their cheapest values would lower the tier."""
    unknown = [f for f in VALUES if f not in request["facts"]
               and f not in request.get("estimated_facts", {})]
    if not unknown:
        return []
    tier, _, _ = compute_tier(request, resolve_facts(request, table), table)
    cheap = {**request, "facts": {**request["facts"], **{f: VALUES[f][0] for f in unknown}}}
    low, _, _ = compute_tier(cheap, resolve_facts(cheap, table), table)
    return unknown if low != tier else []


def schema_for(facts: list[str]) -> dict:
    props = {f: {"type": "object", "properties": {"value": {"enum": VALUES[f]},
                                                  "reason": {"type": "string"}},
                 "required": ["value", "reason"]} for f in facts}
    return {"type": "object", "properties": {
        "facts": {"type": "object", "properties": props, "required": facts},
        "suggested_tier": {"enum": ["local", "light", "standard", "strong"]}},
        "required": ["facts", "suggested_tier"]}


def call_ollama(route: dict, scorer: dict, body: dict) -> str:
    """POST to Ollama's chat API; returns the message content."""
    url = route["endpoint"].rstrip("/") + "/api/chat"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=scorer.get("timeout_s", 60)) as r:
        return json.loads(r.read())["message"]["content"]


CALLERS: dict[str, Caller] = {"ollama": lambda route, scorer, body: call_ollama(route, scorer,
                                                                              body)}


def parse(content: str, facts: list[str]) -> tuple[dict, str | None]:
    """Keep only well-formed estimates for the asked facts; the rest stay unknown."""
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return {}, None
    if not isinstance(obj, dict):
        return {}, None
    out = {}
    for f in facts:
        e = (obj.get("facts") or {}).get(f)
        if isinstance(e, dict) and e.get("value") in VALUES[f]:
            out[f] = {"value": e["value"], "reason": str(e.get("reason", ""))[:300]}
    tier = obj.get("suggested_tier")
    return out, tier if tier in ("local", "light", "standard", "strong") else None


def score(table: Table, request: dict, text: str, caller: Caller | None = None) -> dict | None:
    """Estimate the unknown facts that could lower the tier. None when the scorer isn't
    configured or isn't needed; otherwise the ``route.scored`` data."""
    scorer = table.data.get("scorer")
    if not scorer:
        return None
    facts = could_lower(request, table)
    if not facts:
        return None
    route = table.routes[scorer["route"]]
    call = caller or CALLERS.get(route.get("harness", ""))
    if call is None:
        raise RouteError("TABLE_INVALID", f"scorer: can't call harness {route.get('harness')!r}")
    history = [f"attempt {a['attempt']} on {a['tier']}: {a['class']}"
               for a in request.get("history", [])]
    user = (f"Task kind: {request['facts']['kind']}\n"
            + (f"Earlier attempts: {'; '.join(history)}\n" if history else "")
            + f"Estimate these facts: {', '.join(facts)}.\n\nTask text:\n{text[:12000]}")
    body = {"model": route["model"], "stream": False, "think": False,
            "format": schema_for(facts),
            "options": {"temperature": 0, "num_predict": scorer.get("max_output_tokens", 800)},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    t0 = time.monotonic()
    error = None
    try:
        content = call(route, scorer, body)
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as e:
        content, error = "", f"{type(e).__name__}: {e}"
    estimates, suggested = parse(content, facts)
    if not estimates and error is None:
        error = "no valid estimates in the output"
    return {"task": request["task"], "attempt": request["attempt"], "route_id": scorer["route"],
            "model": route["model"], "mode": scorer["mode"], "asked": facts,
            "estimates": estimates, "suggested_tier": suggested, "error": error,
            "seconds": round(time.monotonic() - t0, 2)}


def apply(request: dict, scored: dict | None) -> dict:
    """The request with the estimates as ``estimated_facts`` (live mode only)."""
    if not scored or scored["mode"] != "live" or not scored["estimates"]:
        return request
    est = {**request.get("estimated_facts", {}),
           **{f: e["value"] for f, e in scored["estimates"].items()}}
    return {**request, "estimated_facts": est}


__all__ = ["FACTS", "VALUES", "apply", "could_lower", "parse", "score"]
