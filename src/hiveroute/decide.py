# SPDX-License-Identifier: GPL-3.0-or-later
"""``decide()``: the tier an attempt needs, and the route that serves it (ROUTING.md §4-§6).

A pure function. It reads no clock and no files: the time is ``state["as_of"]``. The
same request, table, state and mode always give the same decision, so every logged
decision can be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .canonical import format_time, parse_time
from .errors import RouteError
from .table import TIERS, Table, limit_key, rank, schema_error

FACTS = ("specification", "verification", "scope", "consequence", "leverage")
# Classes after which a restart keeps its route (RT7), unless something else says otherwise.
# An indeterminate attempt only gets here once reconciled, or when the table allows a retry.
STAY_CLASSES = frozenset({"interrupted", "truncated", "missing_info", "indeterminate"})


def _reason(rule: str, note: str, tier: str | None = None) -> dict:
    r = {"rule": rule, "note": note}
    if tier is not None:
        r["tier"] = tier
    return r


# --------------------------------------------------------------------------- #
# §4.1-4.2: facts and tier
# --------------------------------------------------------------------------- #
def resolve_facts(request: dict, table: Table) -> dict:
    given = request["facts"]
    facts = {"kind": {"value": given["kind"], "source": "supplied"}}
    for name in FACTS:
        if name in given:
            facts[name] = {"value": given[name], "source": "supplied"}
        else:
            facts[name] = {"value": table.fact_default(name), "source": "default"}
    return facts


def compute_tier(request: dict, facts: dict, table: Table) -> tuple[str, list, list]:
    """The highest tier any applicable rule requires, the rules that applied, and suggestions."""
    v = {k: f["value"] for k, f in facts.items()}
    kind, independent = v["kind"], v["verification"] == "independent"
    applied: list[dict] = []
    suggestions: list[str] = []

    applied.append(_reason("F1", f"kind {kind}", table.data["kind_floor"][kind]))
    if kind == "work" and v["specification"] == "goal_only":
        applied.append(_reason("F2", "work specified by its goal only", "standard"))
        suggestions.append("split: a plan task first, then explicit work tasks")
    if v["scope"] == "many":
        applied.append(_reason("F3", "scope many", "standard"))
    if v["verification"] == "none":
        applied.append(_reason("F4", "no verification", "standard"))
    if v["consequence"] == "costly" and not independent:
        applied.append(_reason("F5", "costly without independent verification", "strong"))
    if v["leverage"] >= table.data["leverage_threshold"] and not independent:
        applied.append(_reason("F6", f"leverage {v['leverage']} without independent verification",
                               "strong"))
    if kind == "review":
        author = request["author"]
        author_tier = author.get("tier") or table.fact_default("author_tier")
        source = "supplied" if "tier" in author else "default"
        applied.append(_reason("F7", f"author's tier ({source})", author_tier))
        if v["consequence"] == "costly":
            applied.append(_reason("F7", "review of costly work", "standard"))

    failed = [a for a in request.get("history", []) if a["class"] == "failed_check"]
    if len(failed) >= 2:
        up = TIERS[min(rank(failed[-1]["tier"]) + 1, len(TIERS) - 1)]
        applied.append(_reason("F8", f"{len(failed)} failed checks, last on {failed[-1]['tier']}",
                               up))
        if failed[-1]["tier"] == "strong":
            suggestions.append("consult on the approach: failed checks already on strong")

    hint = request.get("hint")
    if hint:
        applied.append(_reason("hint", hint["reason"], hint["tier"]))

    tier = max((a["tier"] for a in applied), key=rank)
    return tier, applied, suggestions


# --------------------------------------------------------------------------- #
# §4.4 RT2, §7: pools and fit
# --------------------------------------------------------------------------- #
@dataclass
class PoolView:
    """What the state says about one pool at ``as_of``."""

    blocked: list[str] = field(default_factory=list)   # at a limit: capacity reasons
    free_at: datetime | None = None                    # earliest known time it frees up
    fractions: list[float] = field(default_factory=list)  # known usage share per limit
    max_attempt_usd: float | None = None               # the pool's per-attempt limit
    max_cost: float | None = None                      # this attempt's maximum, if known

    @property
    def headroom(self) -> float | None:
        return 1 - max(self.fractions) if self.fractions else None

    def _free(self, t: datetime | None) -> None:
        if t is not None and (self.free_at is None or t < self.free_at):
            self.free_at = t


def pool_view(pid: str, table: Table, state: dict, request: dict, now: datetime) -> PoolView:
    pool = table.pools[pid]
    st = state.get("pools", {}).get(pid, {})
    view = PoolView()

    limited = [parse_time(st["limited_until"])] if "limited_until" in st else []
    for a in request.get("history", []):
        a_pool = a.get("pool") or table.routes.get(a["route_id"], {}).get("pool")
        if a["class"] == "capacity" and a_pool == pid and "limited_until" in a:
            limited.append(parse_time(a["limited_until"]))
    until = max(limited, default=None)
    if until is not None and until > now:
        view.blocked.append(f"at a limit until {format_time(until)}")
        view._free(until)

    usage = st.get("usage", {})
    budgets: list[tuple[str, float | None, datetime | None]] = []
    for limit in pool["limits"]:
        key = limit_key(limit)
        u = usage.get(key, {"used": None, "basis": "unknown"})
        used = u.get("used") if u["basis"] != "unknown" else None
        resets = parse_time(u["resets_at"]) if "resets_at" in u else None
        if pool["kind"] == "local":
            used, amount = st.get("in_flight"), limit["concurrent"]
        elif pool["kind"] == "subscription":
            amount = 1.0  # usage is reported as a share of the window
        elif limit["per"] == "attempt":
            if "usd" in limit:
                view.max_attempt_usd = limit["usd"]
            continue
        elif limit["per"] == "task":
            used = request.get("task_usage", {}).get(pid, {}).get(key.split("/")[0])
            amount = limit.get("usd", limit.get("tokens"))
        else:
            amount = limit.get("usd", limit.get("tokens"))
        if used is None:
            if "usd" in limit:
                budgets.append((key, None, None))
            continue
        view.fractions.append(used / amount)
        if used >= amount:
            view.blocked.append(f"{key} used up")
            view._free(resets)
        elif "usd" in limit:
            budgets.append((key, amount - used, resets))

    # A metered attempt fits only if its maximum cost fits what remains of every budget.
    caps = [x for x in (view.max_attempt_usd, request.get("max_attempt_usd")) if x is not None]
    view.max_cost = min(caps) if caps else None
    if view.max_cost is not None:
        for key, remaining, resets in budgets:
            if remaining is None:
                view.blocked.append(f"{key} usage unknown")
            elif view.max_cost > remaining:
                view.blocked.append(f"{key}: attempt maximum ${view.max_cost:g} exceeds "
                                    f"remaining ${remaining:g}")
                view._free(resets)
    return view


def _cost(route: dict, request: dict, fallback: float | None) -> float | None:
    est = request.get("estimate")
    if est and "price" in route:
        p = route["price"]
        return (est["input_tokens"] * p["in"] + est["output_tokens"] * p["out"]) / 1_000_000
    return fallback


@dataclass
class Check:
    unfit: list[str] = field(default_factory=list)    # won't clear by waiting
    blocked: list[str] = field(default_factory=list)  # may clear with time

    @property
    def ok(self) -> bool:
        return not self.unfit and not self.blocked


def check_route(rid: str, table: Table, state: dict, request: dict, views: dict) -> Check:
    route, c = table.routes[rid], Check()
    pool = table.pools[route["pool"]]
    view: PoolView = views[route["pool"]]
    kind = request["facts"]["kind"]
    history = request.get("history", [])
    last = history[-1] if history else None

    q = state.get("routes", {}).get(rid)
    if q is None or q["status"] != "qualified":
        c.unfit.append(f"RT2: {q['status'] if q else 'candidate'}, not qualified")
    elif q["route_pin"] != table.route_pin(rid):
        c.unfit.append("RT2: qualified for another version of this route")
    if "projects" in pool and request.get("project") not in pool["projects"]:
        c.unfit.append(f"RT2: pool not allowed for project {request.get('project')!r}")
    c.blocked += [f"RT2: pool {route['pool']} {b}" for b in view.blocked]

    if pool["kind"] == "metered":
        if "price" not in route:
            c.unfit.append("RT2: price unknown")
        if view.max_cost is None:
            c.unfit.append("RT2: the attempt's maximum cost is unknown")

    if "context_tokens" in request:
        if "context_limit" not in route:
            c.unfit.append("RT2: context limit unknown")
        elif request["context_tokens"] > route["context_limit"] * table.data["context_margin"]:
            c.unfit.append(f"RT2: context {request['context_tokens']} exceeds "
                           f"{route['context_limit']} with margin")
    if request.get("tools_needed", True) and route.get("tools") is not True:
        c.unfit.append("RT2: tool use " + ("unsupported" if "tools" in route else "unknown"))

    if kind == "review" and route["family"] == request["author"]["family"]:
        c.unfit.append(f"RT4: same family as the author ({route['family']})")
    if last and last["route_id"] == rid:
        if last["class"] == "outage":
            c.blocked.append("RT5: outage on the previous attempt")
        elif request.get("reason") == "reassign":
            c.blocked.append("RT5: reassigned away from the previous route")
    return c


# --------------------------------------------------------------------------- #
# §4.4 RT6: order within a tier
# --------------------------------------------------------------------------- #
def order_routes(ids: list[str], table: Table, request: dict, views: dict) -> tuple[list, list]:
    threshold, prefer = table.data["soft_threshold"], table.data["prefer"]
    notes: list[dict] = []

    def soft(rid: str) -> bool:
        return any(f >= threshold for f in views[table.routes[rid]["pool"]].fractions)

    below = [r for r in ids if not soft(r)]
    if below and len(below) < len(ids):
        notes.append(_reason("RT6", "skipped pools above the soft threshold: "
                             + ", ".join(sorted({table.routes[r]["pool"] for r in ids
                                                 if soft(r)}))))
        ids = below
    position = {r: i for i, r in enumerate(ids)}

    def cost_key(rid: str) -> tuple:
        route = table.routes[rid]
        view = views[route["pool"]]
        if table.pools[route["pool"]]["kind"] != "metered":
            return (0, 0.0, position[rid])
        return (1, _cost(route, request, view.max_cost), position[rid])

    def headroom_key(rid: str) -> tuple:
        h = views[table.routes[rid]["pool"]].headroom
        return (h is None, -(h or 0.0), position[rid])

    key = {"order": lambda r: position[r], "cost": cost_key, "headroom": headroom_key}[prefer]
    notes.append(_reason("RT6", f"prefer {prefer}"))
    return sorted(ids, key=key), notes


# --------------------------------------------------------------------------- #
# decide()
# --------------------------------------------------------------------------- #
def _validate(request: dict, state: dict, mode: str) -> datetime:
    err = schema_error("request-v1.schema.json", request)
    if err:
        raise RouteError("REQUEST_INVALID", err)
    err = schema_error("state-v1.schema.json", state)
    if err:
        raise RouteError("STATE_INVALID", err)
    if mode not in ("live", "fixed"):
        raise RouteError("MODE_INVALID", f"decide() runs in live or fixed mode, not {mode!r}")
    if request["facts"]["kind"] == "review" and "author" not in request:
        raise RouteError("REQUEST_INVALID", "a review needs the author's family (RT4)")
    try:
        for a in request.get("history", []):
            if "limited_until" in a:
                parse_time(a["limited_until"])
        for p in state.get("pools", {}).values():
            if "limited_until" in p:
                parse_time(p["limited_until"])
            for u in p.get("usage", {}).values():
                if "resets_at" in u:
                    parse_time(u["resets_at"])
        return parse_time(state["as_of"])
    except ValueError as e:
        raise RouteError("REQUEST_INVALID", f"bad timestamp: {e}") from e


def decide(request: dict, table: Table, state: dict, mode: str = "live") -> dict:
    """Choose the tier and route for one attempt. See ROUTING.md §4.

    ``tier`` is the tier the attempt runs at; ``computed_tier`` is what the rules
    required (they differ under an override, in fixed mode, or after an RT3 upgrade).
    ``decision`` is one of ``route`` (use ``route_id``), ``wait`` (ask again at
    ``wait_until``, or later if it is null), ``no_route`` (nothing in the table can
    serve this attempt; the operator decides) or ``reconcile`` (the previous attempt's
    outcome is unknown; settle it first).
    """
    now = _validate(request, state, mode)
    facts = resolve_facts(request, table)
    tier, reasons, suggestions = compute_tier(request, facts, table)
    views = {pid: pool_view(pid, table, state, request, now) for pid in table.pools}
    history = request.get("history", [])
    last = history[-1] if history else None
    out = {
        "decision": None, "task": request["task"], "attempt": request["attempt"],
        "mode": mode, "table": table.pin, "tier": tier, "computed_tier": tier, "facts": facts,
        "route_id": None, "route_pin": None, "pool": None, "model": None,
        "reasons": reasons, "suggestions": suggestions, "rejected": {}, "wait_until": None,
    }

    def chosen(rid: str) -> dict:
        route = table.routes[rid]
        out.update(decision="route", route_id=rid, route_pin=table.route_pin(rid),
                   pool=route["pool"], model=route["model"])
        return out

    # RT1: an operator override wins, in every mode.
    ov = request.get("override")
    if ov:
        if ov["route_id"] not in table.routes:
            raise RouteError("REQUEST_INVALID", f"override: unknown route {ov['route_id']!r}")
        reasons.append(_reason("RT1", f"override by {ov['by']}: {ov['reason']}"))
        c = check_route(ov["route_id"], table, state, request, views)
        if not c.ok:
            out["rejected"][ov["route_id"]] = c.unfit + c.blocked
        return chosen(ov["route_id"])

    # §5 indeterminate: never retried blindly.
    if (last and last["class"] == "indeterminate" and not last.get("reconciled")
            and not table.data["indeterminate_retry"]):
        reasons.append(_reason("RT5", f"attempt {last['attempt']} is indeterminate; reconcile "
                               "it with the provider before a new attempt"))
        out["decision"] = "reconcile"
        return out

    if mode == "fixed":
        kind = request["facts"]["kind"]
        rid = table.data["fixed"].get(kind)
        if rid is None:
            reasons.append(_reason("fixed", f"no fixed route for kind {kind}"))
            out["decision"] = "no_route"
            return out
        reasons.append(_reason("fixed", f"kind {kind} runs on {rid}"))
        out["tier"] = table.routes[rid]["tier"]
        view = views[table.routes[rid]["pool"]]
        if view.blocked:
            out["rejected"][rid] = view.blocked
            out.update(decision="wait",
                       wait_until=format_time(view.free_at) if view.free_at else None)
            return out
        return chosen(rid)

    # RT2-RT7, one tier at a time: the computed tier, then one up if the table allows it.
    tiers = [tier]
    if tier in table.data["allow_upgrade_on_wait"]:
        tiers.append(TIERS[rank(tier) + 1])
    waiting: list[str] = []
    for i, t in enumerate(tiers):
        eligible = []
        for rid in table.tier_routes(t):
            c = check_route(rid, table, state, request, views)
            if c.ok:
                eligible.append(rid)
            else:
                out["rejected"][rid] = c.unfit + c.blocked
                if not c.unfit:
                    waiting.append(rid)
        if eligible:
            if i > 0:
                reasons.append(_reason("RT3", f"no {tier} route has capacity; the table "
                                       f"allows {t} rather than wait"))
                out["tier"] = t
            if last and last["route_id"] in eligible and last["class"] in STAY_CLASSES:
                reasons.append(_reason("RT7", f"stays on {last['route_id']} after "
                                       f"{last['class']}"))
                return chosen(last["route_id"])
            ordered, notes = order_routes(eligible, table, request, views)
            reasons.extend(notes)
            return chosen(ordered[0])
        if not waiting:
            break  # nothing in this tier can clear with time; don't upgrade past a no_route

    if waiting:
        frees = [views[table.routes[r]["pool"]].free_at for r in waiting]
        known = [f for f in frees if f is not None]
        out["decision"] = "wait"
        out["wait_until"] = format_time(min(known)) if known else None
        reasons.append(_reason("RT3", f"no {tier} route has capacity; the tier is a floor"))
    else:
        out["decision"] = "no_route"
        reasons.append(_reason("RT2", f"no qualified, fitting {tier} route"))
    return out
