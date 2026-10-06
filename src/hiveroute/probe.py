# SPDX-License-Identifier: GPL-3.0-or-later
"""Usage probes (ROUTING.md §7): a tiny harness call that refreshes a pool's window shares.

Subscription providers report how much of each usage window is used only in their
harnesses' output (``claude-stream``, ``codex-sessions``). A hive whose agents don't run
through those harnesses (OpenClaw agents, say) can still read the shares by running the
harness once in a while on a trivial prompt, e.g.
``claude -p "Reply with OK" --model claude-haiku-4-5 --output-format stream-json --verbose``,
whose output carries a ``rate_limit_event`` with every window's utilization.

A probe draws on the pool it measures, so it runs only when it's useful: no sooner than
``every_minutes`` after the previous one, and, when ``active_paths`` is set, only if one of
those files changed since then (the agents used something). Each run writes a new file
``<output_dir>/probe-<time>.jsonl``, which the pool's reader picks up (list ``output_dir`` in
its ``paths``), and only the newest ``keep`` files are kept.

The probe must draw on the same subscription as the agents. Where the agents use a token
(a Claude Code setup-token, say), ``env_from`` names the variables to set from
``env_file`` (KEY=value lines), e.g. ``CLAUDE_CODE_OAUTH_TOKEN``; they're read at each run
and never written anywhere. With ``env_file``, they come from that file only, never from the
router's own environment (where a personal token would silently stand in). ``env`` sets fixed,
non-secret variables.
"""

from __future__ import annotations

import glob
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from .canonical import UTC
from .usage import env_value, file_value


def _latest_probe(out: Path) -> datetime | None:
    files = sorted(out.glob("probe-*.jsonl"))
    if not files:
        return None
    return datetime.fromtimestamp(files[-1].stat().st_mtime, UTC)


def _active_since(patterns: list[str], since: datetime) -> bool:
    for pat in patterns:
        for f in glob.glob(os.path.expanduser(pat), recursive=True):
            try:
                if datetime.fromtimestamp(os.path.getmtime(f), UTC) > since:
                    return True
            except OSError:
                continue
    return False


def due(spec: dict, now: datetime) -> str | None:
    """Why the probe should run now, or None."""
    out = Path(os.path.expanduser(spec["output_dir"]))
    last = _latest_probe(out)
    if last is None:
        return "no earlier probe"
    if now - last < timedelta(minutes=spec.get("every_minutes", 60)):
        return None
    if spec.get("active_paths") and not _active_since(spec["active_paths"], last):
        return None
    return f"last probe {(now - last).total_seconds() / 60:.0f} min ago"


def run(spec: dict, now: datetime) -> dict:
    """Run one probe; returns {output, exit}."""
    out = Path(os.path.expanduser(spec["output_dir"]))
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"probe-{now.strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    env = {**os.environ, **spec.get("env", {})}
    for var in spec.get("env_from", []):   # secrets: read now, never logged or stored
        # From env_file only, when one is named: a variable of the same name in the router's
        # own environment (someone's personal token, in a manual run) must not stand in for it.
        value = (env_value(var, None) if not spec.get("env_file")
                 else file_value(var, spec["env_file"]))
        if value is None:
            return {"output": str(path), "exit": f"{var} not found in env_file"}
        env[var] = value
    try:
        with open(path, "w") as f:
            r = subprocess.run(spec["argv"], stdout=f, stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL, cwd=out, env=env,
                               timeout=spec.get("timeout_seconds", 120))
        code: int | str = r.returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    except OSError as e:
        code = f"could not start: {e}"
    for old in sorted(out.glob("probe-*.jsonl"))[:-spec.get("keep", 20)]:
        old.unlink(missing_ok=True)
    return {"output": str(path), "exit": code}


def probe_all(sources: dict, now: datetime, force: bool = False,
              pools: list[str] | None = None) -> dict:
    """Run every due probe in the sources file. Returns {pool: result or skip reason}."""
    results = {}
    for pid, spec in sources.get("probes", {}).items():
        if pools and pid not in pools:
            continue
        why = "forced" if force else due(spec, now)
        if why is None:
            results[pid] = {"skipped": "not due"}
            continue
        results[pid] = {**run(spec, now), "why": why}
    return results
