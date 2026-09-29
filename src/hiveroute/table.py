# SPDX-License-Identifier: GPL-3.0-or-later
"""The route table (ROUTING.md §2, §10): loading, validation, defaults and pins."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .canonical import digest
from .errors import RouteError

TIERS = ("local", "light", "standard", "strong")
KINDS = ("work", "review", "plan", "consult", "triage", "digest")

# The costlier value of each fact (§4.1): what an unknown fact counts as.
CONSERVATIVE = {
    "specification": "goal_only",
    "verification": "none",
    "scope": "many",
    "consequence": "costly",
    "author_tier": "strong",
}

DEFAULTS = {
    "soft_threshold": 0.8,
    "prefer": "order",
    "kind_floor": {"digest": "local", "work": "light", "review": "light",
                   "plan": "standard", "triage": "standard", "consult": "strong"},
    "leverage_threshold": 3,
    "unknown_facts": "conservative",
    "fact_defaults": {},
    "context_margin": 0.8,
    "allow_upgrade_on_wait": [],
    "indeterminate_retry": False,
    "fixed": {},
    "tiers": {},
}

LIMIT_KINDS = {"usd": "metered", "tokens": "metered", "window": "subscription",
               "concurrent": "local"}


def rank(tier: str) -> int:
    return TIERS.index(tier)


def limit_key(limit: dict) -> str:
    """The key a pool's usage for this limit is reported under, e.g. ``usd/month``."""
    if "usd" in limit:
        return f"usd/{limit['per']}"
    if "tokens" in limit:
        return f"tokens/{limit['per']}"
    if "window" in limit:
        return f"window/{limit['window']}"
    return "concurrent"


@cache
def validator(name: str) -> Draft202012Validator:
    try:
        text = resources.files("hiveroute").joinpath("_schemas", name).read_text()
    except (FileNotFoundError, NotADirectoryError):
        text = (Path(__file__).resolve().parents[2] / "schemas" / name).read_text()
    return Draft202012Validator(json.loads(text))


def schema_error(name: str, obj: object) -> str | None:
    err = next(iter(sorted(validator(name).iter_errors(obj), key=lambda e: list(e.path))), None)
    if err is None:
        return None
    where = "/".join(str(p) for p in err.absolute_path) or "(root)"
    return f"{where}: {err.message}"


@dataclass(frozen=True)
class Table:
    """A validated route table with defaults applied. ``pin`` names its content."""

    data: dict
    pin: str

    @classmethod
    def from_data(cls, raw: dict) -> Table:
        err = schema_error("table-v1.schema.json", raw)
        if err:
            raise RouteError("TABLE_INVALID", err)
        data = copy.deepcopy(DEFAULTS)
        for k, v in copy.deepcopy(raw).items():
            if k == "kind_floor":
                data[k].update(v)
            else:
                data[k] = v
        _check(data)
        return cls(data, digest(data))

    @property
    def routes(self) -> dict:
        return self.data["routes"]

    @property
    def pools(self) -> dict:
        return self.data["pools"]

    def tier_routes(self, tier: str) -> list[str]:
        return self.data["tiers"].get(tier, [])

    def fact_default(self, name: str) -> object:
        if name in self.data["fact_defaults"]:
            return self.data["fact_defaults"][name]
        if name == "leverage":
            return self.data["leverage_threshold"]
        return CONSERVATIVE[name]

    def route_pin(self, route_id: str) -> str:
        return digest({"route_id": route_id, **self.routes[route_id]})


def _check(data: dict) -> None:
    routes, pools = data["routes"], data["pools"]
    for pid, pool in pools.items():
        for limit in pool["limits"]:
            kind = LIMIT_KINDS[next(k for k in limit if k in LIMIT_KINDS)]
            if kind != pool["kind"]:
                raise RouteError("TABLE_INVALID",
                                 f"pools/{pid}: a {limit_key(limit)} limit needs a {kind} pool")
        keys = [limit_key(lim) for lim in pool["limits"]]
        if len(keys) != len(set(keys)):
            raise RouteError("TABLE_INVALID", f"pools/{pid}: duplicate limits")
    for rid, route in routes.items():
        if route["pool"] not in pools:
            raise RouteError("TABLE_INVALID", f"routes/{rid}: unknown pool {route['pool']!r}")
    listed: set[str] = set()
    for tier, ids in data["tiers"].items():
        for rid in ids:
            if rid not in routes:
                raise RouteError("TABLE_INVALID", f"tiers/{tier}: unknown route {rid!r}")
            if routes[rid]["tier"] != tier:
                raise RouteError("TABLE_INVALID",
                                 f"tiers/{tier}: route {rid!r} has tier {routes[rid]['tier']!r}")
            if rid in listed:
                raise RouteError("TABLE_INVALID", f"tiers/{tier}: route {rid!r} listed twice")
            listed.add(rid)
    for kind, rid in data["fixed"].items():
        if rid not in routes:
            raise RouteError("TABLE_INVALID", f"fixed/{kind}: unknown route {rid!r}")
    scorer = data.get("scorer")
    if scorer and scorer["route"] not in routes:
        raise RouteError("TABLE_INVALID", f"scorer: unknown route {scorer['route']!r}")
    if "strong" in data["allow_upgrade_on_wait"]:
        raise RouteError("TABLE_INVALID", "allow_upgrade_on_wait: strong has no tier above it")


def load_table(path: str | Path) -> Table:
    """Load a table from YAML or JSON. Quote YAML 1.1 words such as ``on``, ``off``, ``no``."""
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise RouteError("TABLE_INVALID", f"{path}: {e}") from e
    if not isinstance(raw, dict):
        raise RouteError("TABLE_INVALID", f"{path}: not a mapping")
    return Table.from_data(raw)
