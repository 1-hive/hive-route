# SPDX-License-Identifier: GPL-3.0-or-later
"""Recording routing on the hive record (ROUTING.md §9; hive-record amendment A1).

The router's own log keeps every event in full, so it replays on its own. The record
gets a compact summary of each: what the hive needs to see (which model ran each
attempt, why, what waited, what qualified, what drifted), bound to the full entry by
``log: {seq, digest}``, the SHA-256 of the entry's canonical line.

``sync`` posts the summaries of log entries not yet recorded, as the router's actor
(class ``instrument``), through the ``hive emit`` command with the identity in the
environment (``HIVE_URL``, ``HIVE_ID``, ``HIVE_KEY_FILE``). Each post carries an
idempotency key made from the entry's seq and digest, so a repeated sync never records
twice. Progress is kept in ``<log>.recorded``. ``route.scored`` and
``route.shadow_decided`` stay in the router's log: they are evaluation data, not
decisions the hive acted on.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from .canonical import dumps
from .log import read

RECORDED = ("route.table_pinned", "route.mode_set", "route.decided", "route.waiting",
            "route.canary_recorded", "route.drift_detected")
FACTS = ("specification", "verification", "scope", "consequence")


def line_digest(ev: dict) -> str:
    return "sha256:" + hashlib.sha256(dumps(ev).encode()).hexdigest()


def _rules(decision: dict) -> list[str]:
    out: list[str] = []
    for r in decision.get("reasons", []):
        if r["rule"] not in out:
            out.append(r["rule"])
    return out[:32]


def _facts(decision: dict) -> dict:
    f = {k: decision["facts"][k]["value"] for k in FACTS if k in decision.get("facts", {})}
    est = [k for k in FACTS if decision.get("facts", {}).get(k, {}).get("source") == "estimated"]
    if est:
        f["estimated"] = est
    return f


def summary(ev: dict, tables: dict) -> tuple[str, dict] | None:
    """The record event (type, data) for a log entry, or None if it isn't recorded."""
    t, d = ev["type"], ev["data"]
    if t not in RECORDED:
        return None
    log = {"seq": ev["seq"], "digest": line_digest(ev)}
    if t == "route.table_pinned":
        data = {"table": d["pin"], "log": log}
        if isinstance(d["table"].get("version"), int):
            data["version"] = d["table"]["version"]
        return t, data
    if t == "route.mode_set":
        return t, {"mode": d["mode"], "table": d["table"], "log": log}
    if t in ("route.decided", "route.waiting"):
        dec = d["decision"]
        if t == "route.decided":
            data = {"task": dec["task"], "attempt": dec["attempt"], "mode": d["mode"],
                    "table": d["table"], "tier": dec["tier"], "computed_tier": dec["computed_tier"],
                    "route_id": dec["route_id"], "route_pin": dec["route_pin"], "pool": dec["pool"],
                    "model": dec["model"], "facts": _facts(dec), "rules": _rules(dec), "log": log}
            for k in ("harness", "effort"):
                if dec.get(k):
                    data[k] = dec[k]
            return t, data
        data = {"task": dec["task"], "attempt": dec["attempt"], "mode": d["mode"],
                "table": d["table"], "decision": dec["decision"],
                "computed_tier": dec["computed_tier"], "rules": _rules(dec), "log": log}
        if dec.get("wait_until"):
            data["wait_until"] = dec["wait_until"]
        return t, data
    if t == "route.canary_recorded":
        return t, {"route_id": d["route_id"], "route_pin": d["route_pin"], "status": d["status"],
                   "suite": d["suite"], "suite_pin": d["suite_pin"], "passed": d["passed"],
                   "cases": d["cases"], "lesson": (d["lesson"] or "-")[:2000], "log": log}
    data = {"route_id": d["route_id"], "route_pin": d["route_pin"], "attempt": d["attempt"],
            "pinned_model": d["pinned_model"], "observed_models": d["observed_models"][:8],
            "log": log}
    if d.get("task"):
        data["task"] = d["task"]
    return t, data


def emit_with_hive(type_: str, data: dict, key: str, hive_cmd: str = "hive") -> tuple[bool, str]:
    r = subprocess.run([hive_cmd, "emit", type_, "--data", json.dumps(data),
                        "--idempotency-key", key], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()[-500:]
    # The key names the entry's content, so a conflict on it means an earlier sync
    # recorded this entry and stopped before saving its progress (the retry's basis
    # differs, hence the conflict rather than the original response).
    return r.returncode == 0 or "IDEMPOTENCY_CONFLICT" in out, out


def sync(log: str, emit=None, dry_run: bool = False) -> tuple[int, list[str]]:
    """Post every unrecorded entry. Returns (posted, errors); stops at the first error,
    so the record's order follows the log's."""
    emit = emit or emit_with_hive
    progress = Path(str(log) + ".recorded")
    done = int(progress.read_text().strip() or 0) if progress.exists() else 0
    posted, errors = 0, []
    for ev in read(log):
        if ev["seq"] <= done:
            continue
        s = summary(ev, {})
        if s is not None:
            type_, data = s
            key = f"route-{ev['seq']}-{data['log']['digest'][7:23]}"
            if dry_run:
                print(json.dumps({"type": type_, "idempotency_key": key, "data": data}))
            else:
                ok, out = emit(type_, data, key)
                if not ok:
                    errors.append(f"seq {ev['seq']} {type_}: {out}")
                    break
            posted += 1
        done = ev["seq"]
        if not dry_run:
            progress.write_text(f"{done}\n")
    return posted, errors
