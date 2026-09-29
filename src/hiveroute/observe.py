# SPDX-License-Identifier: GPL-3.0-or-later
"""Drift detection (ROUTING.md §3): did the model that ran match the route's pinned model?

A launcher writes an **attempt manifest** next to each attempt's output when it starts
the harness: ``{attempt, task, route_id, route_pin, model, harness, cwd, output,
started_at}`` (``schemas/attempt-v1.schema.json``). ``observe()`` reads the models the
provider reported for that attempt:

- ``claude-code``: the ``model`` of every assistant message in the stream-json output;
- ``codex``: the ``model`` of every ``turn_context`` in the Codex session files whose
  ``session_meta.cwd`` is the attempt's folder and that were written after it started.

A reported model other than the route's is **drift**: the route's qualification no
longer describes what runs. It is logged once per attempt as ``route.drift_detected``,
and the route goes back to ``candidate`` in the qualifications file, so ``live`` mode
stops using it until a canary requalifies it.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from pathlib import Path

from . import quals
from .canonical import parse_time
from .errors import RouteError
from .log import open_log, read
from .table import schema_error
from .usage import _epoch, _files, _lines_with

CODEX_ROOT = "~/.codex/sessions"


def models_claude_stream(path: str | Path) -> set[str]:
    models = set()
    for e in _lines_with(Path(path), '"assistant"'):
        if e.get("type") == "assistant":
            m = (e.get("message") or {}).get("model")
            if m and not m.startswith("<"):  # "<synthetic>": the harness's own messages
                models.add(m)
    return models


def models_codex(cwd: str, since: datetime, root: str = CODEX_ROOT) -> set[str]:
    models = set()
    for path in _files([root], since):
        metas = _lines_with(path, '"session_meta"')
        if not any((m.get("payload") or {}).get("cwd") == cwd for m in metas):
            continue
        for e in _lines_with(path, '"turn_context"'):
            if e.get("type") == "turn_context" and (e.get("payload") or {}).get("model"):
                models.add(e["payload"]["model"])
    return models


def read_manifest(path: str | Path) -> dict:
    try:
        m = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RouteError("INPUT_INVALID", f"{path}: {e}") from e
    err = schema_error("attempt-v1.schema.json", m)
    if err:
        raise RouteError("INPUT_INVALID", f"{path}: {err}")
    return m


def observed_models(manifest: dict, codex_root: str = CODEX_ROOT) -> set[str]:
    if manifest["harness"] == "claude-code":
        return models_claude_stream(manifest["output"])
    if manifest["harness"] == "codex":
        return models_codex(manifest["cwd"], parse_time(manifest["started_at"]), codex_root)
    return set()


def _demote(qualifications: str | None, route_id: str, route_pin: str, note: str) -> bool:
    """Set a qualified route back to candidate. Returns whether anything changed."""
    if not qualifications:
        return False
    if not quals.path_of(qualifications).exists():
        return False

    def change(q: dict) -> bool:
        e = q.get(route_id)
        if not e or e.get("status") != "qualified" or e.get("route_pin") != route_pin:
            return False
        e.update(status="candidate", demoted=note)
        return True

    return quals.update(qualifications, change)


def observe(manifests: list[str], log: str, qualifications: str | None = None,
            codex_root: str = CODEX_ROOT, aux: dict | None = None) -> list[dict]:
    """Check each attempt's reported models against its route. Returns the drift events
    written; an attempt already checked for drift is not logged twice. ``aux`` maps a
    harness to models it uses for itself (the sources file's ``aux_models``)."""
    seen = {e["data"]["attempt"] for e in read(log) if e["type"] == "route.drift_detected"}
    written = []
    for path in manifests:
        m = read_manifest(path)
        if m["attempt"] in seen:
            continue
        observed = observed_models(m, codex_root)
        # A harness's own auxiliary models (e.g. Codex's approval reviewer) aren't drift.
        allowed = {m["model"], *(aux or {}).get(m["harness"], ())}
        drifted = sorted(x for x in observed if x not in allowed)
        if not drifted:
            continue
        with open_log(log) as w:
            ev = w.append("route.drift_detected", {
                "attempt": m["attempt"], "task": m["task"], "route_id": m["route_id"],
                "route_pin": m["route_pin"], "pinned_model": m["model"],
                "observed_models": sorted(observed), "manifest": str(path)})
        _demote(qualifications, m["route_id"], m["route_pin"],
                f"drift on {m['attempt']}: {', '.join(drifted)}")
        seen.add(m["attempt"])
        written.append(ev)
    return written


def find_manifests(patterns: list[str], since: datetime | None = None) -> list[str]:
    found: set[str] = set()
    for pat in patterns:
        found.update(glob.glob(os.path.expanduser(pat), recursive=True))
    if since is not None:
        found = {f for f in found if _epoch(os.path.getmtime(f)) >= since}
    return sorted(found)
