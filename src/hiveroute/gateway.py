# SPDX-License-Identifier: GPL-3.0-or-later
"""Gateway configuration generated from the route table (ROUTING.md §8, build step 2).

The route table is the single source of truth; the gateway's config is generated from it
and never edited by hand or held in the gateway's database. ``litellm_config`` produces a
LiteLLM proxy config:

- one ``model_list`` entry per route, named by its ``route_id`` (the alias a harness
  asks for), calling the route's ``gateway_model`` (Ollama routes derive
  ``ollama_chat/<model>``), with the pool's key from ``gateway_key_env``, the route's
  endpoint, effort and output limit, and tags ``pool:<id>``, ``tier:<t>``, ``route:<id>``
  so spend can be read and budgeted per pool and per route;
- no retries and no fallbacks (the router is their one owner), and unsupported
  parameters fail instead of being dropped;
- ``store_model_in_db: false``, so the file stays the only source of model config.

Budgets are not generated: the gateway's budget features must be tested on the pinned
version before they are relied on (§8). ``budget_notes`` lists what each metered pool's
limits need.

Subscription routes are left out unless asked for: passthrough of a subscription login
is used only where the provider's terms allow it.
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


def litellm_config(table: Table, include_subscription: bool = False) -> dict:
    models = []
    for rid, route in table.routes.items():
        pool = table.pools[route["pool"]]
        if pool["kind"] == "subscription" and not include_subscription:
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
        params["tags"] = [f"pool:{route['pool']}", f"tier:{route['tier']}", f"route:{rid}"]
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
                             "database_url": "os.environ/LITELLM_DATABASE_URL",
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
                         f"pool:{pid}" if limit["per"] not in ("attempt", "task")
                         else f"{pid}: {amount} {unit} per {limit['per']} -> enforced by the "
                         "router (RT2), with max_tokens as the hard stop")
    return notes
