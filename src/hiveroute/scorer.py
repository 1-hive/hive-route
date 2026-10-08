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

The table's ``shadow_scorers`` run the same way on other routes, always in shadow mode,
each logged as its own ``route.scored``: a second scorer is compared on real tasks before
it could replace the first. ``evaluate`` (``hive-route scorer-eval``) runs scorers on a
labelled suite.

The scorer's route is called directly, never routed. Supported harnesses: ``ollama``
(the native chat API with a JSON schema) and ``systemone`` (a decision model with
TypeSafe's System One API, such as Jev or a self-hosted Kev: one ``choice`` question per
fact, two for ``verification``, answered with probabilities; ``min_probability`` drops a
less likely answer, so the fact stays unknown).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .canonical import digest
from .decide import FACTS, ORDER, compute_tier, resolve_facts
from .errors import RouteError
from .table import Table

# The facts' values from cheapest to costliest (§4.1).
VALUES = ORDER
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

# The same definitions as System One questions: one choice per fact, cheapest option first.
# Each wording is the best of those tried on Kev-4B against fixtures/scorer/suite.jsonl
# (59 cases): short labels, as in Jev's and Kev's examples, for specification (48 against
# 39 with whole clauses) and scope (38 against 35); whole clauses for consequence (52
# against 51, and 68 against 66 on cases.jsonl). A `score` question, averaging both option
# orders, or a two-step scope question did no better. Held-out sets gain less (about +1).
QUESTIONS = {
    "specification": ("How fully does the task pin what to do and how it's checked?",
                      {"explicit": "Plan and acceptance check given",
                       "partial": "What to do given, approach or acceptance partly open",
                       "goal_only": "Only a goal; plan must be found"}),
    "scope": ("How much does the task touch?",
              {"single": "One file or component",
               "few": "Two to four files or components",
               "many": "Five or more, or several subsystems"}),
    "consequence": ("What does a mistake cost?",
                    {"reversible": "the work is easy to undo",
                     "costly": "it spends external compute, changes shared or production state, "
                               "or is hard to undo"}),
}
# Verification as two questions, which a decision model answers far better than one three-way
# choice (Kev-4B on fixtures/scorer/suite.jsonl: 42/59 against 17/59; "none" was almost
# never chosen): is there any check at all, and if so, who controls it.
CHECKED = ("Does the task text name any way the result will be checked: tests to pass or "
           "write, a benchmark or reference result, acceptance criteria, or a review?")
CONTROLLED = ("What mainly decides whether the task succeeded?",
              {"independent": "a check the worker can't change: existing or protected tests, a "
                              "reference result or benchmark, or a reviewer with a stated bar",
               "weak": "tests or checks the worker writes, or a matter of judgment"})
MAX_CHARS = 12000


def systemone_questions(facts: list[str]) -> dict:
    qs = {}
    for f in facts:
        if f == "verification":
            qs["verification.checked"] = {"type": "noul", "instructions": CHECKED}
            qs["verification.controlled"] = {"type": "choice", "instructions": CONTROLLED[0],
                                             "criteria": CONTROLLED[1]}
        else:
            qs[f] = {"type": "choice", "instructions": QUESTIONS[f][0], "criteria": QUESTIONS[f][1]}
    return qs


def _verification(answers: dict) -> dict | None:
    """``none`` when no check is named (p < 0.5); otherwise who controls it. The
    probabilities combine both answers."""
    checked, ctl = answers.get("verification.checked"), answers.get("verification.controlled")
    if not (isinstance(checked, dict) and isinstance(ctl, dict)):
        return None
    n, probs = checked.get("noul"), ctl.get("probabilities") or {}
    if not isinstance(n, (int, float)) or ctl.get("choice") not in ("independent", "weak"):
        return None
    combined = {"independent": n * float(probs.get("independent", 0)),
                "weak": n * float(probs.get("weak", 0)), "none": 1 - n}
    return {"choice": "none" if n < 0.5 else ctl["choice"], "probabilities": combined}

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


def call_systemone(route: dict, scorer: dict, body: dict) -> str:
    """POST to a System One API (Jev, Kev); returns the response body. A hosted endpoint
    takes a bearer key from the environment variable ``api_key_env`` names."""
    url = route["endpoint"].rstrip("/") + "/v1/systemone"
    headers = {"Content-Type": "application/json"}
    if route.get("api_key_env"):
        key = os.environ.get(route["api_key_env"])
        if not key:
            raise ValueError(f"{route['api_key_env']} is not set")
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=scorer.get("timeout_s", 60)) as r:
        return r.read().decode()


CALLERS: dict[str, Caller] = {
    "ollama": lambda route, scorer, body: call_ollama(route, scorer, body),
    "systemone": lambda route, scorer, body: call_systemone(route, scorer, body),
}


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


def parse_systemone(content: str, facts: list[str], scorer: dict) -> dict:
    """Each fact's chosen value with its probability; below ``min_probability`` it's dropped."""
    try:
        answers = json.loads(content).get("answers") or {}
    except (json.JSONDecodeError, AttributeError):
        return {}
    out = {}
    floor = scorer.get("min_probability", 0)
    for f in facts:
        a = _verification(answers) if f == "verification" else answers.get(f)
        if not isinstance(a, dict) or a.get("choice") not in VALUES[f]:
            continue
        probs = {k: round(float(v), 4) for k, v in (a.get("probabilities") or {}).items()
                 if k in VALUES[f] and isinstance(v, (int, float))}
        p = probs.get(a["choice"])
        if p is None or p < floor:
            continue
        out[f] = {"value": a["choice"], "reason": f"p={p:.2f}", "p": p, "probabilities": probs}
    return out


def reader_pin(table: Table) -> str | None:
    """The reader's pin (§4.8): its route's pin, its questions and how much text it reads.
    Any change to them voids its qualification."""
    reader = table.data.get("reader")
    if not reader:
        return None
    return digest({"route_id": reader["route"], "route_pin": table.route_pin(reader["route"]),
                   "questions": systemone_questions(list(VALUES)),
                   "max_chars": reader.get("max_chars", MAX_CHARS)})


def read(table: Table, request: dict, text: str, caller: Caller | None = None) -> dict | None:
    """The reader's reading of all four facts (§4.8): the ``route.read`` data, or None
    without a reader. ``probabilities`` holds each fact it read; a failure leaves it empty."""
    reader = table.data.get("reader")
    if not reader:
        return None
    cfg = {k: v for k, v in reader.items() if k in ("route", "mode", "max_chars", "timeout_s")}
    est = estimate(table, cfg, request["facts"]["kind"], list(VALUES), text,
                   request.get("history", []), caller)
    probs = {f: e["probabilities"] for f, e in est["estimates"].items() if e.get("probabilities")}
    return {"task": request["task"], "attempt": request["attempt"],
            "writer": request.get("writer"), "reader_pin": reader_pin(table),
            "stated": {f: request["facts"][f] for f in VALUES if f in request["facts"]},
            "route_id": est["route_id"], "model": est["model"], "mode": reader["mode"],
            "probabilities": probs, "error": est["error"], "seconds": est["seconds"]}


def scorers(table: Table) -> list[dict]:
    """The table's scorer (in its own mode), then its shadow scorers (always shadow)."""
    out = [table.data["scorer"]] if table.data.get("scorer") else []
    return out + [{**s, "mode": "shadow"} for s in table.data.get("shadow_scorers", [])]


def estimate(table: Table, scorer: dict, kind: str, facts: list[str], text: str,
             history: list[dict] | None = None, caller: Caller | None = None) -> dict:
    """Ask one scorer for ``facts`` from the task's text: estimates, suggested tier, error."""
    route = table.routes[scorer["route"]]
    harness = route.get("harness", "")
    call = caller or CALLERS.get(harness)
    if call is None:
        raise RouteError("TABLE_INVALID", f"scorer: can't call harness {harness!r}")
    past = [f"attempt {a['attempt']} on {a['tier']}: {a['class']}" for a in history or []]
    head = (f"Task kind: {kind}\n"
            + (f"Earlier attempts: {'; '.join(past)}\n" if past else ""))
    text = text[:scorer.get("max_chars", MAX_CHARS)]
    if harness == "systemone":
        body = {"model": route["model"], "state": f"{head}\nTask text:\n{text}",
                "questions": systemone_questions(facts)}
    else:
        user = head + f"Estimate these facts: {', '.join(facts)}.\n\nTask text:\n{text}"
        body = {"model": route["model"], "stream": False, "think": False,
                "format": schema_for(facts),
                "options": {"temperature": 0, "num_predict": scorer.get("max_output_tokens", 800)},
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": user}]}
    t0 = time.monotonic()
    error = None
    try:
        content = call(route, scorer, body)
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as e:
        content, error = "", f"{type(e).__name__}: {e}"
    if harness == "systemone":
        estimates, suggested = parse_systemone(content, facts, scorer), None
    else:
        estimates, suggested = parse(content, facts)
    if not estimates and error is None:
        error = "no valid estimates in the output"
    return {"route_id": scorer["route"], "model": route["model"], "mode": scorer["mode"],
            "asked": facts, "estimates": estimates, "suggested_tier": suggested, "error": error,
            "seconds": round(time.monotonic() - t0, 2)}


def score(table: Table, request: dict, text: str, caller: Caller | None = None,
          scorer: dict | None = None) -> dict | None:
    """Estimate the unknown facts that could lower the tier, with ``scorer`` (default: the
    table's). None when it isn't configured or isn't needed; otherwise the ``route.scored``
    data."""
    scorer = scorer or table.data.get("scorer")
    if not scorer:
        return None
    facts = could_lower(request, table)
    if not facts:
        return None
    est = estimate(table, scorer, request["facts"]["kind"], facts, text,
                   request.get("history", []), caller)
    return {"task": request["task"], "attempt": request["attempt"], **est}


def apply(request: dict, scored: dict | None) -> dict:
    """The request with the estimates as ``estimated_facts`` (live mode only)."""
    if not scored or scored["mode"] != "live" or not scored["estimates"]:
        return request
    est = {**request.get("estimated_facts", {}),
           **{f: e["value"] for f, e in scored["estimates"].items()}}
    return {**request, "estimated_facts": est}


__all__ = ["FACTS", "VALUES", "apply", "could_lower", "estimate", "parse", "read", "reader_pin",
           "score", "scorers"]
