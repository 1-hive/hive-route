# SPDX-License-Identifier: GPL-3.0-or-later
"""Canaries (ROUTING.md §3): qualify a route by running it on a small fixed suite.

A **suite** is a directory with ``suite.yaml`` (``schemas/suite-v1.schema.json``): cases,
each with a prompt, starting files, and a **check** the model can't edit. The check
runs from the suite directory against the case's work folder, so an attempt can't
pass by changing its tests. The suite is pinned by the digest of its files.

The **harnesses** section of a sources file says how to start each harness: an argv
template with ``{model}``, ``{prompt}``, ``{cwd}``, the arguments to add for an effort,
and whether the prompt goes on stdin.

A route is ``qualified`` when every case passes (or the suite's ``pass_fraction``) and
no reported model differs from the route's. The result is written to the
qualifications file, which ``decide()`` reads through the state (§7), with a lesson, and
logged as ``route.canary_recorded``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from . import quals
from .canonical import digest, format_time
from .errors import RouteError
from .log import open_log
from .observe import CODEX_ROOT, models_claude_stream, models_codex
from .table import Table, schema_error
from .usage import env_value


def load_suite(path: str | Path) -> tuple[Path, dict, str]:
    """The suite's directory, its definition, and its pin (a digest of every file)."""
    root = Path(path)
    if root.is_file():
        root = root.parent
    try:
        suite = yaml.safe_load((root / "suite.yaml").read_text())
    except (OSError, yaml.YAMLError) as e:
        raise RouteError("SUITE_INVALID", f"{root}: {e}") from e
    err = schema_error("suite-v1.schema.json", suite)
    if err:
        raise RouteError("SUITE_INVALID", err)
    files = {}
    for f in sorted(root.rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            files[str(f.relative_to(root))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return root, suite, digest(files)


def _fill(argv: list[str], values: dict) -> list[str]:
    return [a.format(**values) for a in argv]


def run_case(route_id: str, route: dict, case: dict, root: Path, harness: dict, base: Path,
             codex_root: str = CODEX_ROOT, gateway: dict | None = None) -> dict:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    work = base / route_id / f"{case['id']}-{stamp}"
    work.parent.mkdir(parents=True, exist_ok=True)
    if "files" in case:
        shutil.copytree(root / case["files"], work)
    else:
        work.mkdir()
    prompt = (root / case["prompt"]).read_text()
    via = bool(route.get("via_gateway"))
    # Through the gateway, the harness asks for the route's alias, its route_id (§8).
    values = {"model": route_id if via else route["model"], "prompt": prompt, "cwd": str(work),
              "effort": route.get("effort") or ""}
    env = dict(os.environ)
    if via:
        if not gateway:
            raise RouteError("SOURCES_INVALID", f"{route_id} is via_gateway; no gateway section")
        values.update(gateway_url=gateway["url"], key_env=gateway["key_env"],
                      gateway_key=env_value(gateway["key_env"], gateway.get("env_file")) or "")
    argv = _fill(harness["argv"], values)
    if via:
        argv += _fill(harness.get("gateway_args", []), values)
        env.update({k: v.format(**values) for k, v in harness.get("gateway_env", {}).items()})
        if gateway["key_env"] not in env and values["gateway_key"]:
            env[gateway["key_env"]] = values["gateway_key"]
    if route.get("effort"):
        if "effort_args" not in harness:
            raise RouteError("SOURCES_INVALID", f"harness for {route_id} can't set an effort")
        argv += _fill(harness["effort_args"], values)
    output = work.parent / f"{work.name}.out"
    timeout = case.get("timeout_minutes", 15) * 60
    started = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        with open(output, "w") as out:
            r = subprocess.run(argv, cwd=work, stdout=out, stderr=subprocess.STDOUT, env=env,
                               input=prompt if harness.get("stdin") else None,
                               text=True, timeout=timeout,
                               stdin=None if harness.get("stdin") else subprocess.DEVNULL)
        harness_exit = r.returncode
    except subprocess.TimeoutExpired:
        harness_exit = "timeout"
    except OSError as e:
        harness_exit = f"could not start: {e}"
    seconds = round(time.monotonic() - t0, 1)

    check = _fill(case["check"], {"work": str(work), "case": str(root / case["id"]),
                                  "suite": str(root)})
    try:
        c = subprocess.run(check, cwd=root, capture_output=True, text=True,
                           timeout=case.get("check_timeout_seconds", 120),
                           env={**os.environ, "CANARY_WORK": str(work)})
        passed, tail = c.returncode == 0, (c.stdout + c.stderr)[-600:]
    except (subprocess.TimeoutExpired, OSError) as e:
        passed, tail = False, f"check did not run: {e}"

    if route.get("harness") == "codex":
        observed = models_codex(str(work), started, codex_root)
    else:
        observed = models_claude_stream(output)
    return {"case": case["id"], "passed": passed, "harness_exit": harness_exit,
            "seconds": seconds, "observed_models": sorted(observed), "check_tail": tail,
            "work": str(work), "output": str(output)}


def pool_busy(table: Table, route_id: str, sources: dict, max_usage: float) -> str | None:
    """Why the route's pool is too busy for a canary now, or None. Canaries draw on the
    same pools as live work, so they stop before a window passes ``max_usage``."""
    from .usage import collect
    pid = table.routes[route_id]["pool"]
    state, _ = collect(table, sources, datetime.now(UTC))
    p = state.get("pools", {}).get(pid, {})
    if "limited_until" in p:
        return f"pool {pid} at a limit until {p['limited_until']}"
    for key, u in p.get("usage", {}).items():
        if u.get("used") is not None and u["used"] >= max_usage:
            return f"pool {pid} {key} at {u['used']:.0%} (canaries stop at {max_usage:.0%})"
    return None


def qualify(table: Table, route_id: str, suite_path: str, sources: dict, log: str,
            lesson: str | None = None, cases: list[str] | None = None,
            codex_root: str = CODEX_ROOT, max_usage: float | None = None) -> dict:
    """Run the suite on one route and record the result. Returns the qualification entry.

    With ``max_usage``, each case first checks the route's pool (``pool_busy``); a busy
    pool stops the run, which is recorded as incomplete and leaves the route a candidate.
    """
    if route_id not in table.routes:
        raise RouteError("INPUT_INVALID", f"unknown route {route_id!r}")
    route = table.routes[route_id]
    harnesses = sources.get("harnesses", {})
    if route.get("harness") not in harnesses:
        raise RouteError("SOURCES_INVALID", f"no harness command for {route.get('harness')!r}")
    if not sources.get("qualifications"):
        raise RouteError("SOURCES_INVALID", "the sources file names no qualifications file")
    root, suite, suite_pin = load_suite(suite_path)
    chosen = [c for c in suite["cases"] if not cases or c["id"] in cases]
    kinds = {c["kind"] for c in chosen}
    base = Path(os.path.expanduser(sources.get("canary_workdir", "~/.cache/hive-route/canary")))

    results, stopped = [], None
    for c in chosen:
        if max_usage is not None:
            stopped = pool_busy(table, route_id, sources, max_usage)
            if stopped:
                break
        results.append(run_case(route_id, route, c, root, harnesses[route["harness"]], base,
                                codex_root, sources.get("gateway")))
    npass = sum(r["passed"] for r in results)
    # A harness reports a gateway route by its alias, the route_id.
    allowed = {route["model"], route_id, *harnesses[route["harness"]].get("aux_models", ())}
    drift = sorted({m for r in results for m in r["observed_models"] if m not in allowed})
    needed = suite.get("pass_fraction", 1.0) * len(results)
    status = ("qualified" if results and not stopped and npass >= needed and not drift
              else "candidate")
    failed = [r["case"] for r in results if not r["passed"]]
    auto = ((f"stopped after {len(results)} of {len(chosen)} cases: {stopped}; " if stopped
             else "")
            + f"{npass}/{len(results)} cases passed"
            + (f"; failed: {', '.join(failed)}" if failed else "")
            + (f"; reported models differ from {route['model']}: {', '.join(drift)}"
               if drift else ""))
    entry = {"status": status, "route_pin": table.route_pin(route_id), "suite": suite["name"],
             "suite_pin": suite_pin, "kinds": sorted(kinds), "at": format_time(datetime.now(UTC)),
             "passed": npass, "cases": len(results), "stopped": stopped, "lesson": lesson or auto,
             "results": [{k: r[k] for k in ("case", "passed", "harness_exit", "seconds",
                                             "observed_models", "work")} for r in results]}

    quals.update(sources["qualifications"], lambda q: q.__setitem__(route_id, entry))

    with open_log(log) as w:
        w.ensure_table(table)
        w.append("route.canary_recorded", {"route_id": route_id, **{
            k: entry[k] for k in ("route_pin", "status", "suite", "suite_pin", "kinds", "passed",
                                  "cases", "lesson", "results")}})
    return entry


def set_lesson(route_id: str, lesson: str, sources: dict, log: str) -> dict:
    """Replace a route's lesson with an operator's, and log it."""
    if not sources.get("qualifications") or not quals.path_of(sources["qualifications"]).is_file():
        raise RouteError("INPUT_INVALID", "no qualifications file")

    def change(q: dict) -> dict:
        if route_id not in q:
            raise RouteError("INPUT_INVALID", f"{route_id!r} has no canary result")
        q[route_id]["lesson"] = lesson
        return q[route_id]

    e = quals.update(sources["qualifications"], change)
    with open_log(log) as w:
        w.append("route.canary_recorded", {"route_id": route_id, **{
            k: e[k] for k in ("route_pin", "status", "suite", "suite_pin", "kinds", "passed",
                              "cases", "lesson", "results")}})
    return e
