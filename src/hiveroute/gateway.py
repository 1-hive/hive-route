# SPDX-License-Identifier: GPL-3.0-or-later
"""Gateway configuration generated from the route table (ROUTING.md §8, build step 2).

The route table is the single source of truth; the gateway's config is generated from it
and never edited by hand or held in the gateway's database. ``litellm_config`` produces a
LiteLLM proxy config:

- one ``model_list`` entry per route, named by its ``route_id`` (the alias a harness
  asks for), calling the route's ``gateway_model`` (Ollama routes derive
  ``ollama_chat/<model>``), with the pool's key from ``gateway_key_env``, the route's
  endpoint, effort and output limit, and tags ``pool:<id>``, ``tier:<t>``, ``route:<id>``
  (plus ``pool:<id>:<period>`` for each period limit of a metered pool) so spend can be
  read and budgeted per pool and per route;
- no retries and no fallbacks (the router is their one owner), and unsupported
  parameters fail instead of being dropped;
- ``store_model_in_db: false``, so the file stays the only source of model config.

Budgets come separately (``budgets``, ``attempt_budget``): one LiteLLM tag budget per
period limit of each metered pool, and one per attempt on a metered route, created by the
launcher. Tested on LiteLLM 1.103.0 (§8): enforced once logged spend passes the budget,
with calls in flight able to overshoot.

Without a database (``database=False``) the gateway has only its master key: no
per-actor keys and no spend logs. Subscription routes are left out unless asked for: passthrough of a subscription login
is used only where the provider's terms allow it. ``systemone`` routes (decision models
for the scorer) are left out too: they are called directly and aren't chat models.
"""

from __future__ import annotations

from .errors import RouteError
from .table import Table


def gateway_model(route: dict) -> str | None:
    if route.get("gateway_model"):
        return route["gateway_model"]
    if route.get("harness") == "ollama":
        return f"ollama_chat/{route['model']}"
    return None


def litellm_config(table: Table, include_subscription: bool = False,
                   database: bool = True) -> dict:
    models = []
    for rid, route in table.routes.items():
        pool = table.pools[route["pool"]]
        if pool["kind"] == "subscription" and not include_subscription:
            continue
        if route.get("harness") == "systemone":  # a decision model, not a chat model
            continue
        model = gateway_model(route)
        if model is None:
            raise RouteError("TABLE_INVALID",
                             f"routes/{rid}: no gateway_model for the gateway to call")
        params: dict = {"model": model}
        if pool.get("gateway_key_env"):
            params["api_key"] = f"os.environ/{pool['gateway_key_env']}"
        elif pool["kind"] == "metered":
            raise RouteError("TABLE_INVALID", f"pools/{route['pool']}: metered, but no "
                             "gateway_key_env")
        if route.get("endpoint"):
            params["api_base"] = route["endpoint"]
        if route.get("effort"):
            params["reasoning_effort"] = route["effort"]
        if route.get("max_output_tokens"):
            params["max_tokens"] = route["max_output_tokens"]
        if pool.get("gateway_drop_params"):
            params["additional_drop_params"] = list(pool["gateway_drop_params"])
        params["tags"] = [f"pool:{route['pool']}", f"tier:{route['tier']}", f"route:{rid}"]
        if pool["kind"] == "metered":  # a tag per period, so each period has its own budget
            params["tags"] += [f"pool:{route['pool']}:{lim['per']}" for lim in pool["limits"]
                               if "usd" in lim and lim["per"] in DURATIONS]
        # LiteLLM reserves model_info.tier ("free"/"paid"), hence the route_ prefix.
        info = {"id": table.route_pin(rid), "route_tier": route["tier"],
                "route_pool": route["pool"], "route_family": route["family"]}
        if "price" in route:
            info["input_cost_per_token"] = route["price"]["in"] / 1_000_000
            info["output_cost_per_token"] = route["price"]["out"] / 1_000_000
        if route.get("context_limit"):
            info["max_input_tokens"] = route["context_limit"]
        models.append({"model_name": rid, "litellm_params": params, "model_info": info})
    return {
        "model_list": models,
        "litellm_settings": {"num_retries": 0, "drop_params": False, "request_timeout": 900},
        "router_settings": {"num_retries": 0, "fallbacks": [], "context_window_fallbacks": [],
                            "content_policy_fallbacks": []},
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY",
                             **({"database_url": "os.environ/LITELLM_DATABASE_URL"}
                                if database else {}),
                             "store_model_in_db": False},
    }


def budget_notes(table: Table) -> list[str]:
    notes = []
    for pid, pool in table.pools.items():
        if pool["kind"] != "metered":
            continue
        for limit in pool["limits"]:
            amount = limit.get("usd", limit.get("tokens"))
            unit = "USD" if "usd" in limit else "tokens"
            notes.append(f"{pid}: {amount} {unit} per {limit['per']} -> a budget on tag "
                         f"pool:{pid}:{limit['per']} (--budgets)"
                         if limit["per"] not in ("attempt", "task")
                         else f"{pid}: {amount} {unit} per {limit['per']} -> the router's "
                         "check (RT2), and per attempt a tag budget the launcher creates "
                         "(attempt_budget)")
    return notes



# LiteLLM durations for the router's periods: calendar-aligned day, week and month.
DURATIONS = {"day": "1d", "week": "1w", "month": "1mo"}


def budgets(table: Table) -> list[dict]:
    """The gateway's hard stops for metered pools: a tag budget per period limit, on the tag
    ``pool:<id>:<period>`` that the pool's routes carry (tag budgets are keyed by name, so
    each period needs its own tag). Tested on LiteLLM 1.103.0: requests are refused once
    logged spend passes the budget, but spend is logged just after each request, so calls
    already in flight can pass it. The router reads the same periods (usage.py
    ``gateway_period``), so the two agree on what remains."""
    out = []
    for pid, pool in table.pools.items():
        if pool["kind"] != "metered":
            continue
        for limit in pool["limits"]:
            if "usd" in limit and limit["per"] in DURATIONS:
                out.append({"name": f"pool:{pid}:{limit['per']}", "max_budget": limit["usd"],
                            "budget_duration": DURATIONS[limit["per"]],
                            "description": f"hive-route: pool {pid}, {limit['usd']} USD per "
                                           f"{limit['per']} (generated from the route table)"})
    return out


def attempt_budget(table: Table, decision: dict) -> dict | None:
    """The tag budget that caps one attempt on a metered route: ``attempt:<id>`` with the
    pool's per-attempt limit (or the request's smaller ``max_attempt_usd``). A launcher
    creates it before the attempt and tags the attempt's requests ``attempt:<id>``; the
    router's own check only decides whether an attempt fits, it can't stop one running."""
    if decision.get("decision") != "route" or not decision.get("via_gateway"):
        return None
    pool = table.pools[decision["pool"]]
    if pool["kind"] != "metered":
        return None
    caps = [lim["usd"] for lim in pool["limits"] if "usd" in lim and lim["per"] == "attempt"]
    if not caps:
        return None
    return {"name": f"attempt:{decision['attempt']}", "max_budget": min(caps),
            "budget_duration": "1mo",
            "description": f"hive-route: attempt {decision['attempt']} on {decision['route_id']}"}
