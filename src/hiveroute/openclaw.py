# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenClaw: routing for long-running agents (ROUTING.md §9.3).

A launcher-driven hive asks the router before each attempt (§4). An OpenClaw agent runs
continuously and takes its model from its config: a primary model and an ordered list of
fallbacks, which OpenClaw hot-applies when the file changes. For such agents the router
**binds** instead of launching:

- **Bindings** (``schemas/bindings-v1.schema.json``) name each agent's place in an OpenClaw
  config file (``agents.defaults`` or ``agents.entries.<id>``), its kind and facts, and
  where its session database is.
- ``render()`` decides each binding as if it were an attempt (the same ``decide()``, so the
  tier rules, qualifications, pool limits and fixed mode all apply), then orders the other
  usable routes as fallbacks: the rest of the tier, the tiers above, and, only when the
  binding allows it (``fallback_floor``), the tiers below. The result is a JSON merge patch
  per config file. The router never writes an agent's config: each hive applies the patch
  its own way.
- ``read_sessions()`` reads a pool's usage from OpenClaw's per-agent session databases:
  spend and tokens per period for metered pools, and rate-limit errors, which put the pool
  at a limit for ``down_minutes``. OpenClaw doesn't record subscription window shares, so
  those stay unknown.
- ``observe()`` checks the turns since the latest binding for **drift**: a provider that
  answered with another model than the one requested, or a model the binding didn't name.

Routes for OpenClaw have ``harness: openclaw`` and an OpenClaw model reference,
``provider/model``, as their ``model``.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import quals
from .canonical import UTC, digest, parse_time
from .decide import check_route, decide, order_routes, pool_view
from .errors import RouteError
from .log import open_log, read
from .table import TIERS, Table, rank, schema_error
from .usage import Reading, period_bounds

HARNESS = "openclaw"
RATE_LIMIT = re.compile(r"rate_limit_error|rate.?limit|\b429\b|usage limit|quota", re.I)


# --------------------------------------------------------------------------- #
# bindings
# --------------------------------------------------------------------------- #
def load_bindings(path: str | Path) -> dict:
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise RouteError("BINDINGS_INVALID", f"{path}: {e}") from e
    err = schema_error("bindings-v1.schema.json", raw)
    if err:
        raise RouteError("BINDINGS_INVALID", err)
    return raw


def _model_ref(table: Table, rid: str) -> str:
    route = table.routes[rid]
    if route.get("harness") != HARNESS:
        raise RouteError("BINDINGS_INVALID", f"route {rid} is not an openclaw route")
    if "/" not in route["model"]:
        raise RouteError("TABLE_INVALID", f"routes/{rid}: an openclaw model is provider/model")
    return route["model"]


def _request(name: str, b: dict, stamp: str) -> dict:
    req = {"task": f"binding:{name}", "attempt": f"{name}@{stamp}",
           "facts": {"kind": b["kind"], **b.get("facts", {})},
           "tools_needed": b.get("tools_needed", True)}
    if b["kind"] == "review":
        req["author"] = b["author"]
    return req


def _chain(name: str, b: dict, table: Table, state: dict, mode: str,
           decision: dict) -> tuple[list[str], list[str]]:
    """The binding's routes, primary first, and notes on how they were chosen."""
    notes: list[str] = []
    if mode == "fixed":
        primary = b.get("route") or table.data["fixed"].get(b["kind"])
        if primary is None:
            raise RouteError("BINDINGS_INVALID", f"{name}: no fixed route for kind {b['kind']}")
        chain = [primary, *[r for r in b.get("fallbacks", []) if r != primary]]
        for rid in chain:
            if rid not in table.routes:
                raise RouteError("BINDINGS_INVALID", f"{name}: unknown route {rid!r}")
        return chain, ["fixed: the binding's route and listed fallbacks"]

    request = _request(name, b, "x")
    now = parse_time(state["as_of"])
    views = {pid: pool_view(pid, table, state, request, now) for pid in table.pools}
    floor = b.get("fallback_floor", decision["tier"])
    tiers = [t for t in TIERS if rank(t) >= rank(decision["tier"])]
    tiers += [t for t in reversed(TIERS) if rank(floor) <= rank(t) < rank(decision["tier"])]
    chain = [decision["route_id"]] if decision["decision"] == "route" else []
    for t in tiers:
        usable = [r for r in table.tier_routes(t) if r not in chain
                  and table.routes[r].get("harness") == HARNESS
                  and check_route(r, table, state, request, views).ok]
        ordered, _ = order_routes(usable, table, request, views) if usable else ([], [])
        chain += ordered
    if decision["decision"] != "route":
        notes.append(f"{decision['decision']}: no {decision['tier']} route can serve now"
                     + (f"; fallbacks down to {floor}" if chain else ""))
    if not chain:
        fixed = b.get("route") or table.data["fixed"].get(b["kind"])
        if fixed is None:
            raise RouteError("BINDINGS_INVALID", f"{name}: nothing can serve it and no fixed "
                             "route to keep it running")
        chain = [fixed]
        notes.append(f"degraded: kept on the fixed route {fixed}")
    return chain, notes


def _target(b: dict) -> list[str]:
    t = b["target"]
    return ["agents", "defaults"] if t == "defaults" else ["agents", "entries", t]


def render(table: Table, bindings: dict, state: dict, mode: str) -> dict:
    """Decide every binding and build the patches. Deterministic in its inputs."""
    stamp = state["as_of"]
    results, patches = {}, {}
    for name in sorted(bindings["bindings"]):
        b = bindings["bindings"][name]
        decision = decide(_request(name, b, stamp), table, state, mode)
        chain, notes = _chain(name, b, table, state, mode, decision)
        refs = [_model_ref(table, r) for r in chain]
        efforts = sorted({table.routes[r]["effort"] for r in chain if table.routes[r].get("effort")})
        if efforts:
            notes.append("effort is not set through the config (the agent's thinking level "
                         "applies): " + ", ".join(efforts))
        results[name] = {"config": b["config"], "target": b["target"], "tier": decision["tier"],
                         "computed_tier": decision["computed_tier"], "decision": decision["decision"],
                         "routes": chain, "route_pins": [table.route_pin(r) for r in chain],
                         "primary": refs[0], "fallbacks": refs[1:], "notes": notes}
        node = patches.setdefault(b["config"], {})
        for key in _target(b):
            node = node.setdefault(key, {})
        node["model"] = {"primary": refs[0], "fallbacks": refs[1:]}
        node.setdefault("models", {}).update({r: {} for r in refs})
    return {"table": table.pin, "mode": mode, "as_of": stamp, "results": results,
            "patches": patches, "patch_files": {c: patch_name(c) for c in patches},
            "digest": digest({"results": results, "patches": patches})}


def check_config(config: dict, result: dict) -> list[str]:
    """Problems applying one binding's models to an OpenClaw config: a model its
    ``modelPolicy.allow`` list refuses, or a provider it doesn't configure."""
    problems = []
    agents = config.get("agents") or {}
    allow = ((agents.get("defaults") or {}).get("modelPolicy") or {}).get("allow")
    providers = set((config.get("models") or {}).get("providers") or {})
    for ref in [result["primary"], *result["fallbacks"]]:
        if allow is not None and ref not in allow:
            problems.append(f"{ref}: not in agents.defaults.modelPolicy.allow")
        if providers and ref.split("/", 1)[0] not in providers:
            problems.append(f"{ref}: provider {ref.split('/', 1)[0]} is not configured")
    return problems


def compare_config(config: dict, result: dict) -> list[str]:
    """How applying the binding would change the config's model settings for its target;
    empty when it matches what the agent runs now."""
    node: object = config
    for key in _target({"target": result["target"]}):
        node = node.get(key) if isinstance(node, dict) else None
    current = (node or {}).get("model") if isinstance(node, dict) else None
    if isinstance(current, str):
        current = {"primary": current}
    current = current or {}
    out = []
    if current.get("primary") != result["primary"]:
        out.append(f"primary {current.get('primary')} -> {result['primary']}")
    if (current.get("fallbacks") or []) != result["fallbacks"]:
        out.append(f"fallbacks {current.get('fallbacks') or []} -> {result['fallbacks']}")
    return out


def patch_name(config: str) -> str:
    """A patch file's name, unique per config file: every OpenClaw config is openclaw.json."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", config.strip("/")) + ".patch.json"


def bind(table: Table, bindings: dict, state: dict, mode: str, log: str | None) -> tuple[dict, bool]:
    """Render, and log it as ``route.bound`` unless it equals the latest binding.
    Returns (rendered, changed)."""
    out = render(table, bindings, state, mode)
    if log is None:
        return out, True
    last = [e for e in read(log) if e["type"] == "route.bound"]
    if last and last[-1]["data"]["digest"] == out["digest"]:
        return out, False
    with open_log(log) as w:
        w.ensure_table(table)
        w.append("route.bound", {"table": table.pin, "mode": mode, "state": state,
                                 "bindings": bindings, "digest": out["digest"],
                                 "results": out["results"], "patches": out["patches"]})
    return out, True


def replay_bound(ev: dict, tables: dict) -> str | None:
    """A mismatch description if a logged binding doesn't render the same again."""
    d = ev["data"]
    table = tables.get(d["table"])
    if table is None:
        return f"table {d['table']} not pinned earlier in the log"
    again = render(table, d["bindings"], d["state"], d["mode"])
    return None if again["digest"] == d["digest"] else "the binding renders differently"


# --------------------------------------------------------------------------- #
# session databases
# --------------------------------------------------------------------------- #
def _zstd(blob: bytes) -> bytes | None:
    try:
        from compression import zstd  # Python 3.14
        return zstd.decompress(blob)
    except ImportError:
        pass
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj().decompress(blob)
    except ImportError:
        pass
    try:
        return subprocess.run(["zstd", "-dcq"], input=blob, capture_output=True, check=True,
                              timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _paths(patterns: list[str]) -> list[str]:
    out: set[str] = set()
    for p in patterns:
        out.update(glob.glob(os.path.expanduser(p), recursive=True))
    return sorted(out)


def scan(patterns: list[str], since: datetime) -> tuple[list[dict], list[str]]:
    """Assistant turns at or after ``since`` across OpenClaw agent session databases,
    oldest first: ``{at, provider, model, response_model, stop, error, usage, db}``, and the
    problems reading them: a pattern no database matches, a database that can't be read,
    events that couldn't be decompressed. A problem is never silent: an unreadable database
    must not look like an idle agent. Databases are opened read-only."""
    since_ms = since.timestamp() * 1000
    out, problems = [], []
    for pat in patterns:
        if not _paths([pat]):
            problems.append(f"no database matches {pat}")
    for path in _paths(patterns):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            rows = con.execute("select event_json, event_zstd from transcript_events").fetchall()
            con.close()
        except sqlite3.Error as e:
            problems.append(f"{path}: {e}")
            continue
        undecoded = 0
        for ej, blob in rows:
            if ej is None:
                raw = _zstd(blob) if blob else None
                if raw is None:
                    undecoded += 1
                    continue
                ej = raw.decode("utf-8", "replace")
            if '"assistant"' not in ej:
                continue
            try:
                m = json.loads(ej).get("message") or {}
            except json.JSONDecodeError:
                continue
            if m.get("role") != "assistant" or not m.get("provider") or not m.get("model"):
                continue
            ts = m.get("timestamp")
            if not isinstance(ts, (int, float)) or ts < since_ms:
                continue
            out.append({"at": datetime.fromtimestamp(ts / 1000, UTC), "provider": m["provider"],
                        "model": m["model"], "response_model": m.get("responseModel"),
                        "stop": m.get("stopReason"), "error": str(m.get("errorMessage") or ""),
                        "usage": m.get("usage") or {}, "db": path})
        if undecoded:
            problems.append(f"{path}: {undecoded} compressed events not decoded (install zstd)")
    return sorted(out, key=lambda t: t["at"]), problems


def turns(patterns: list[str], since: datetime) -> list[dict]:
    return scan(patterns, since)[0]


def _ref(t: dict) -> str:
    return f"{t['provider']}/{t['model']}"


def read_sessions(spec: dict, pid: str, table: Table, now: datetime) -> Reading | None:
    """``openclaw-sessions``: one pool's usage from the turns on its routes' models."""
    models = {r["model"] for r in table.routes.values()
              if r["pool"] == pid and r.get("harness") == HARNESS}
    if not models:
        return None
    limits = table.pools[pid]["limits"]
    pers = [lim["per"] for lim in limits if lim.get("per") in ("day", "week", "month")]
    starts = [period_bounds(p, now)[0] for p in pers]
    down = timedelta(minutes=spec.get("down_minutes", 30))
    since = min([*starts, now - down])
    found, problems = scan(spec["paths"], since)
    ts = [t for t in found if _ref(t) in models]
    r = Reading(observed_at=now, source=f"{len(ts)} turns in {len(_paths(spec['paths']))} databases"
                + (f"; problems: {'; '.join(problems)}" if problems else ""))
    for lim in limits:
        if lim.get("per") not in ("day", "week", "month") or problems:
            continue  # with a database unread, spend stays unknown (and blocks the pool)
        start, reset = period_bounds(lim["per"], now)
        inside = [t for t in ts if t["at"] >= start]
        if "usd" in lim:
            used = sum(float(((t["usage"].get("cost") or {}).get("total")) or 0) for t in inside)
            r.windows[f"usd/{lim['per']}"] = (used, reset)
        elif "tokens" in lim:
            used = sum(int(t["usage"].get("totalTokens") or 0) for t in inside)
            r.windows[f"tokens/{lim['per']}"] = (used, reset)
    limited = [t for t in ts if t["stop"] == "error" and RATE_LIMIT.search(t["error"])]
    if limited:
        last = limited[-1]["at"]
        recovered = any(t["at"] > last and t["stop"] != "error" for t in ts)
        if not recovered and last + down > now:
            r.limited_until = last + down
    return r


# --------------------------------------------------------------------------- #
# drift
# --------------------------------------------------------------------------- #
def _same(requested: str, answered: str) -> bool:
    """A provider may answer with a dated snapshot of the requested model."""
    return answered == requested or answered.startswith(requested + "-")


def observe(log: str, qualifications: str | None = None,
            warnings: list[str] | None = None) -> list[dict]:
    """Check the turns since the latest ``route.bound`` against it. One drift event per
    binding per binding version; a provider answering with another model demotes the route
    (as for attempts, §3). Databases that couldn't be read are added to ``warnings``."""
    bound = [e for e in read(log) if e["type"] == "route.bound"]
    if not bound:
        return []
    ev = bound[-1]
    seen = {e["data"]["attempt"] for e in read(log) if e["type"] == "route.drift_detected"}
    since = parse_time(ev["at"])
    written = []
    for name, res in sorted(ev["data"]["results"].items()):
        attempt = f"{name}@{ev['seq']}"
        b = ev["data"]["bindings"]["bindings"][name]
        if attempt in seen or not b.get("sessions"):
            continue
        allowed = {res["primary"], *res["fallbacks"]}
        by_ref = dict(zip([res["primary"], *res["fallbacks"]],
                          zip(res["routes"], res["route_pins"], strict=True), strict=True))
        bad, demote = set(), None
        found, problems = scan(b["sessions"], since)
        if warnings is not None:
            warnings += [f"{name}: {p}" for p in problems]
        for t in found:
            if t["stop"] == "error":
                continue
            ref = _ref(t)
            if ref not in allowed:
                bad.add(ref)
            elif t["response_model"] and not _same(t["model"], t["response_model"]):
                bad.add(f"{t['provider']}/{t['response_model']}")
                demote = demote or (ref, *by_ref[ref])
        if not bad:
            continue
        rid, pin = (demote[1], demote[2]) if demote else (res["routes"][0], res["route_pins"][0])
        with open_log(log) as w:
            out = w.append("route.drift_detected", {
                "attempt": attempt, "task": f"binding:{name}", "route_id": rid, "route_pin": pin,
                "pinned_model": demote[0] if demote else res["primary"],
                "observed_models": sorted(bad), "binding": name})
        if demote and qualifications and quals.path_of(qualifications).exists():
            note = f"drift on {attempt}: {', '.join(sorted(bad))}"

            def change(q: dict, rid: str = rid, pin: str = pin, note: str = note) -> bool:
                e = q.get(rid)
                if not e or e.get("status") != "qualified" or e.get("route_pin") != pin:
                    return False
                e.update(status="candidate", demoted=note)
                return True
            quals.update(qualifications, change)
        written.append(out)
    return written


# --------------------------------------------------------------------------- #
# canaries
# --------------------------------------------------------------------------- #
def models_exec(path: str | Path) -> set[str]:
    """The model ``openclaw agent exec --json`` reports, as ``provider/model``. The output
    file may hold log lines around the JSON envelope, which may span several lines."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    candidates = [text]
    starts = [m.start() for m in re.finditer(r"(?m)^\{", text)]
    candidates += [text[i:] for i in starts] + text.splitlines()
    for c in candidates:
        try:
            o = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict) and o.get("provider") and o.get("model"):
            return {f"{o['provider']}/{o['model']}"}
    return set()
