# SPDX-License-Identifier: GPL-3.0-or-later
"""The route service: ``POST /route`` for agents with their own loop (ROUTING.md §9.2).

An agent such as Iter asks for a model at the start of each episode and reports how the
episode ended. The service builds the request the way a launcher would: the agent's fixed
facts (what's at stake in its role) from the agents file, which the agent can't edit; the
facts it may state per episode (by default ``specification`` and ``scope``) from its call;
and the task's history from the service's own record of earlier episodes. Every decision
is logged like the CLI's, so it replays. At each episode's end the agent reports the models
the provider said answered; one that isn't the route's is drift (§3), logged and demoting
the route, as for launched attempts.

    POST /route            {"task", "episode", "facts"?, "kind"?, "hint"?, "context_tokens"?,
                            "estimate"?, "tools_needed"?, "text"?}   -> the decision
    POST /episodes/end     {"episode", "models", "class"?, "limited_until"?}
                                                            -> {"ok": true, "drift": [...]}
    GET  /health                                                     -> table, mode

Agents authenticate with a bearer token whose SHA-256 is in the agents file. Standard
library only; bind it to loopback or a private network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from . import log as routelog
from .canonical import UTC, format_time
from .decide import decide
from .errors import RouteError
from .observe import demote, same_model
from .scorer import apply, score
from .table import Table, schema_error
from .usage import collect

ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
CLASSES = ("outage", "capacity", "truncated", "missing_info", "failed_check", "stalled",
           "indeterminate", "interrupted", "checkpoint")
MAX_BODY = 256 * 1024
DEFAULT_EPISODE_FACTS = ("specification", "scope")


def load_agents(path: str | Path) -> dict:
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise RouteError("AGENTS_INVALID", f"{path}: {e}") from e
    err = schema_error("agents-v1.schema.json", raw)
    if err:
        raise RouteError("AGENTS_INVALID", err)
    for name, a in raw["agents"].items():
        both = set(a.get("facts", {})) & set(a.get("episode_facts", DEFAULT_EPISODE_FACTS))
        if both:
            raise RouteError("AGENTS_INVALID", f"{name}: {', '.join(sorted(both))} both fixed "
                             "and stated per episode")
    return raw


def run_decision(table: Table, request: dict, state: dict, mode: str, log: str | None,
                 text: str | None = None) -> dict:
    """Score unknown facts from the task's text (if any), decide, and log: what the CLI's
    ``decide`` and the service share."""
    if text:
        scored = score(table, request, text)
        if scored:
            # What the estimates would do to the tier, logged in either scorer mode.
            with_est = decide(apply(request, {**scored, "mode": "live"}), table, state, "live")
            scored["tier_with_estimates"] = with_est["computed_tier"]
            scored["tier_without"] = decide(request, table, state, "live")["computed_tier"]
            request = apply(request, scored)
            if log:
                routelog.record_scored(log, table, scored)
    decision = decide(request, table, state, mode)
    if log:
        routelog.record_decision(log, table, request, state, mode, decision)
    return decision


class History:
    """Each agent's episodes, as an append-only JSONL file per agent in the state folder:
    ``decided`` lines (the route an episode got) and ``ended`` lines (its class)."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def _path(self, agent: str) -> Path:
        return self.folder / f"{agent}.episodes.jsonl"

    def _lines(self, agent: str) -> list[dict]:
        p = self._path(agent)
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []

    def _append(self, agent: str, line: dict) -> None:
        with open(self._path(agent), "a") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")

    def episodes(self, agent: str) -> dict:
        """attempt id -> {task, route_id, tier, pool, class?, limited_until?}, in order."""
        out: dict[str, dict] = {}
        for x in self._lines(agent):
            if x["t"] == "decided":
                out[x["attempt"]] = {k: x[k] for k in ("task", "route_id", "tier", "pool",
                                                       "route_pin", "model") if k in x}
            elif x["t"] == "ended" and x["attempt"] in out:
                out[x["attempt"]].update({k: x[k] for k in ("class", "limited_until") if k in x},
                                         ended=True)
        return out

    def decided(self, agent: str, attempt: str, d: dict) -> None:
        self._append(agent, {"t": "decided", "attempt": attempt, "task": d["task"],
                             "route_id": d["route_id"], "route_pin": d["route_pin"],
                             "model": d["model"], "tier": d["tier"], "pool": d["pool"]})

    def ended(self, agent: str, attempt: str, cls: str | None, limited_until: str | None,
              models: list[str]) -> None:
        line: dict = {"t": "ended", "attempt": attempt, "models": models}
        if cls:
            line["class"] = cls
        if limited_until:
            line["limited_until"] = limited_until
        self._append(agent, line)


class Service:
    def __init__(self, table: Table, agents: dict, sources: dict | None, log: str,
                 state_dir: str | Path, state_ttl: float = 30.0, mode: str | None = None):
        self.table, self.agents, self.sources, self.log = table, agents["agents"], sources, log
        self.history = History(state_dir)
        self.state_ttl, self.fixed_mode = state_ttl, mode
        self.lock = threading.Lock()
        self._state: tuple[datetime, dict] | None = None

    # ------------------------------------------------------------------ helpers
    def agent_for(self, auth: str | None) -> str:
        token = auth[7:].strip() if auth and auth.startswith("Bearer ") else ""
        if token:
            digest = hashlib.sha256(token.encode()).hexdigest()
            for name, a in self.agents.items():
                if hmac.compare_digest(digest, a["token_sha256"]):
                    return name
        raise RouteError("UNAUTHORIZED", "a valid bearer token is required")

    def state(self, now: datetime) -> dict:
        if self.sources is None:
            return {"as_of": format_time(now)}
        if self._state is None or (now - self._state[0]).total_seconds() > self.state_ttl:
            self._state = (now, collect(self.table, self.sources, now)[0])
        return self._state[1]

    def mode(self) -> str:
        return self.fixed_mode or routelog.current_mode(self.log) or "live"

    # ------------------------------------------------------------------ endpoints
    def route(self, agent: str, body: dict) -> dict:
        a = self.agents[agent]
        task, episode = body.get("task"), body.get("episode")
        if not (isinstance(task, str) and ID.match(task) and isinstance(episode, str)
                and ID.match(episode)):
            raise RouteError("REQUEST_INVALID", "task and episode must match " + ID.pattern)
        unknown = set(body) - {"task", "episode", "facts", "kind", "hint", "context_tokens",
                               "estimate", "tools_needed", "text"}
        if unknown:
            raise RouteError("REQUEST_INVALID", f"unknown fields: {', '.join(sorted(unknown))}")
        kind = body.get("kind", a["kinds"][0])
        if kind not in a["kinds"]:
            raise RouteError("REQUEST_INVALID", f"kind {kind!r} is not one of this agent's")
        stated = body.get("facts") or {}
        allowed = set(a.get("episode_facts", DEFAULT_EPISODE_FACTS))
        if not isinstance(stated, dict) or set(stated) - allowed:
            raise RouteError("REQUEST_INVALID", "an episode may state only "
                             + ", ".join(sorted(allowed)))
        text = body.get("text")
        if text is not None and not isinstance(text, str):
            raise RouteError("REQUEST_INVALID", "text must be a string")
        attempt = f"{agent}.{episode}"
        with self.lock:
            episodes = self.history.episodes(agent)
            if attempt in episodes:
                raise RouteError("CONFLICT", f"episode {episode} was already routed")
            tid = f"{agent}/{task}"
            history = [{"attempt": k, **{f: v[f] for f in ("route_id", "tier", "pool", "class",
                                                            "limited_until") if f in v}}
                       for k, v in episodes.items() if v["task"] == tid and v.get("class")]
            request = {"task": tid, "attempt": attempt,
                       "reason": "restart" if any(v["task"] == tid for v in episodes.values())
                       else "new",
                       "facts": {"kind": kind, **a.get("facts", {}), **stated},
                       "tools_needed": body.get("tools_needed", True)}
            for k in ("hint", "context_tokens", "estimate"):
                if k in body:
                    request[k] = body[k]
            for k in ("project", "runtime", "author"):
                if k in a:
                    request[k] = a[k]
            if history:
                request["history"] = history
            now = datetime.now(UTC)
            decision = run_decision(self.table, request, self.state(now), self.mode(),
                                    self.log, text)
            if decision["decision"] == "route":
                self.history.decided(agent, attempt, decision)
        return decision

    def end(self, agent: str, body: dict) -> dict:
        episode, cls, models = body.get("episode"), body.get("class"), body.get("models")
        if not (isinstance(episode, str) and ID.match(episode)):
            raise RouteError("REQUEST_INVALID", "episode must match " + ID.pattern)
        if cls is not None and cls not in CLASSES:
            raise RouteError("REQUEST_INVALID", "class, when the episode didn't simply "
                             "succeed, is one of " + ", ".join(CLASSES))
        if not (isinstance(models, list) and len(models) <= 32
                and all(isinstance(m, str) and 0 < len(m) <= 200 for m in models)):
            raise RouteError("REQUEST_INVALID", "models: the model names the provider reported "
                             "for the episode's calls (a list, empty if none answered)")
        unknown = set(body) - {"episode", "class", "limited_until", "models"}
        if unknown:
            raise RouteError("REQUEST_INVALID", f"unknown fields: {', '.join(sorted(unknown))}")
        lu = body.get("limited_until")
        if lu is not None and cls != "capacity":
            raise RouteError("REQUEST_INVALID", "limited_until goes with class capacity")
        attempt = f"{agent}.{episode}"
        with self.lock:
            ep = self.history.episodes(agent).get(attempt)
            if ep is None:
                raise RouteError("NOT_FOUND", f"no routed episode {episode}")
            if ep.get("ended"):
                raise RouteError("CONFLICT", f"episode {episode} already ended")
            self.history.ended(agent, attempt, cls, lu, sorted(set(models)))
            drift = self._drift(attempt, ep, models)
        return {"ok": True, "drift": drift}

    def _drift(self, attempt: str, ep: dict, models: list[str]) -> list[str]:
        """Reported models that aren't the route's (§3): logged once, and the route goes back
        to candidate. A harness's own auxiliary models (the sources' ``aux_models``) and the
        route's gateway alias (its id, §8) aren't drift."""
        route = self.table.routes.get(ep["route_id"], {})
        harness = (self.sources or {}).get("harnesses", {}).get(route.get("harness"), {})
        pinned = ep.get("model") or route.get("model", "")
        pin = ep.get("route_pin") or self.table.route_pin(ep["route_id"])
        allowed = {ep["route_id"], *harness.get("aux_models", ())}
        bad = sorted({m for m in models if m not in allowed and not same_model(pinned, m)})
        if not bad:
            return []
        with routelog.open_log(self.log) as w:
            w.append("route.drift_detected", {
                "attempt": attempt, "task": ep["task"], "route_id": ep["route_id"],
                "route_pin": pin, "pinned_model": pinned,
                "observed_models": sorted(set(models)), "source": "agent report"})
        demote((self.sources or {}).get("qualifications"), ep["route_id"], pin,
               f"drift on {attempt}: {', '.join(bad)} (agent report)")
        self._state = None   # the next decision sees the demotion
        return bad

    def health(self) -> dict:
        return {"ok": True, "table": self.table.pin, "mode": self.mode()}


STATUS = {"UNAUTHORIZED": 401, "NOT_FOUND": 404, "CONFLICT": 409}


def handler(svc: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "hive-route"

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:  # no request logging to stderr
            pass

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, svc.health())
            else:
                self._send(404, {"error": "NOT_FOUND"})

        def do_POST(self) -> None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise RouteError("REQUEST_INVALID", "body too large")
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise RouteError("REQUEST_INVALID", "the body must be a JSON object")
                agent = svc.agent_for(self.headers.get("Authorization"))
                if self.path == "/route":
                    self._send(200, svc.route(agent, body))
                elif self.path == "/episodes/end":
                    self._send(200, svc.end(agent, body))
                else:
                    self._send(404, {"error": "NOT_FOUND"})
            except RouteError as e:
                self._send(STATUS.get(e.code, 400), {"error": e.code, "message": e.reason})
            except (ValueError, UnicodeDecodeError) as e:
                self._send(400, {"error": "REQUEST_INVALID", "message": f"bad JSON: {e}"})
            except Exception as e:  # one bad request must not take the service down
                self._send(500, {"error": "INTERNAL", "message": type(e).__name__})

    return Handler


def make_server(svc: Service, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), handler(svc))

