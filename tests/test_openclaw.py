# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenClaw bindings (ROUTING.md §9.3): config patches, usage, drift, canaries."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta

import pytest
from conftest import FIXTURES, qualified

from hiveroute import openclaw
from hiveroute.canonical import UTC, format_time
from hiveroute.cli import main
from hiveroute.errors import RouteError
from hiveroute.log import read, replay
from hiveroute.table import load_table
from hiveroute.usage import collect

TABLE = load_table(FIXTURES / "tables" / "openclaw.yaml")
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def state(**extra) -> dict:
    return {"as_of": format_time(NOW), "routes": qualified(TABLE), **extra}


def bindings(**over) -> dict:
    b = {"config": "/hive/a/openclaw.json", "target": "defaults", "kind": "work",
         "facts": {"specification": "explicit", "verification": "independent",
                   "scope": "few", "consequence": "reversible"}}
    return {"format": "hive-route.bindings/1", "bindings": {
        "cosmo": {**b, "facts": {}, **over},
        "omega": {**b, "config": "/hive/b/openclaw.json", "target": "iter-omega",
                  "fallback_floor": "local"}}}


# --------------------------------------------------------------------------- render
def test_live_binding_orders_the_tier_then_up():
    out = openclaw.render(TABLE, bindings(), state(), "live")
    cosmo = out["results"]["cosmo"]          # no facts: strong
    assert cosmo["tier"] == "strong" and cosmo["routes"] == ["oc-opus", "oc-gpt"]
    assert out["patches"]["/hive/a/openclaw.json"] == {"agents": {"defaults": {
        "model": {"primary": "anthropic/claude-opus-5-5", "fallbacks": ["openai/gpt-5.6-sol"]},
        "models": {"anthropic/claude-opus-5-5": {}, "openai/gpt-5.6-sol": {}}}}}


SPEND = {"sc-api": {"usage": {"usd/day": {"used": 1.0, "basis": "measured",
                                         "resets_at": format_time(NOW + timedelta(hours=12))}}}}


def test_fallback_floor_reaches_lower_tiers_and_skips_other_harnesses():
    out = openclaw.render(TABLE, bindings(), state(pools=SPEND), "live")
    omega = out["results"]["omega"]          # facts: light
    assert omega["tier"] == "light"
    # the tier, the tiers above (cc-sonnet is a claude-code route: skipped), then local
    assert omega["routes"] == ["oc-qwen-api", "oc-sonnet", "oc-opus", "oc-gpt", "oc-qwen-local"]
    patch = out["patches"]["/hive/b/openclaw.json"]
    assert patch["agents"]["entries"]["iter-omega"]["model"]["primary"] == \
        "singularitycompute/qwen/qwen3.8-27b"


def test_unknown_spend_blocks_a_metered_route():
    omega = openclaw.render(TABLE, bindings(), state(), "live")["results"]["omega"]
    assert omega["decision"] == "wait" and "oc-qwen-api" not in omega["routes"]
    assert omega["primary"] == "anthropic/claude-sonnet-5-5"   # the next usable, upward


def test_a_limited_pool_moves_the_primary():
    st = state(pools={"claude-plan": {"limited_until": format_time(NOW + timedelta(hours=1))}})
    cosmo = openclaw.render(TABLE, bindings(), st, "live")["results"]["cosmo"]
    assert cosmo["routes"] == ["oc-gpt"] and cosmo["primary"] == "openai/gpt-5.6-sol"


def test_nothing_serves_keeps_the_fixed_route():
    st = {"as_of": format_time(NOW)}         # nothing qualified
    cosmo = openclaw.render(TABLE, bindings(), st, "live")["results"]["cosmo"]
    assert cosmo["routes"] == ["oc-opus"]
    assert any(n.startswith("degraded") for n in cosmo["notes"])


def test_fixed_mode_uses_the_binding_route_and_fallbacks():
    b = bindings(route="oc-gpt", fallbacks=["oc-opus", "oc-qwen-local"])
    cosmo = openclaw.render(TABLE, b, state(), "fixed")["results"]["cosmo"]
    assert cosmo["routes"] == ["oc-gpt", "oc-opus", "oc-qwen-local"]
    with pytest.raises(RouteError):
        openclaw.render(TABLE, bindings(route="nope"), state(), "fixed")
    with pytest.raises(RouteError, match="not an openclaw route"):
        openclaw.render(TABLE, bindings(route="cc-sonnet"), state(), "fixed")


def test_rendering_is_deterministic_and_bind_logs_only_changes(tmp_path):
    log = str(tmp_path / "log.jsonl")
    a, changed = openclaw.bind(TABLE, bindings(), state(), "live", log)
    b, again = openclaw.bind(TABLE, bindings(), state(), "live", log)
    assert changed and not again and a["digest"] == b["digest"]
    st = state(pools={"claude-plan": {"limited_until": format_time(NOW + timedelta(hours=1))}})
    _, moved = openclaw.bind(TABLE, bindings(), st, "live", log)
    assert moved
    assert [e["type"] for e in read(log)] == ["route.table_pinned", "route.bound", "route.bound"]
    assert replay(log) == (2, [])


def test_check_config():
    res = openclaw.render(TABLE, bindings(), state(), "live")["results"]["cosmo"]
    cfg = {"models": {"providers": {"anthropic": {}}},
           "agents": {"defaults": {"modelPolicy": {"allow": ["anthropic/claude-opus-5-5"]}}}}
    assert openclaw.check_config(cfg, res) == [
        "openai/gpt-5.6-sol: not in agents.defaults.modelPolicy.allow",
        "openai/gpt-5.6-sol: provider openai is not configured"]


def test_bindings_schema(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text(json.dumps({**bindings(), "extra": 1}))
    with pytest.raises(RouteError):
        openclaw.load_bindings(f)
    f.write_text(json.dumps(bindings()))
    assert openclaw.load_bindings(f)["bindings"]["cosmo"]["kind"] == "work"


# --------------------------------------------------------------------------- sessions
SCHEMA = ("create table transcript_events (session_id text, seq integer, event_json text, "
          "created_at integer, event_zstd blob, event_utf8_bytes integer, navigation_json text)")


def turn(at: datetime, model: str, provider: str = "anthropic", *, response: str | None = None,
         stop: str = "stop", error: str | None = None, cost: float = 0.0, tokens: int = 100) -> dict:
    m = {"role": "assistant", "content": [], "api": "x", "provider": provider, "model": model,
         "usage": {"input": tokens, "output": 0, "totalTokens": tokens,
                   "cost": {"total": cost}},
         "stopReason": stop, "timestamp": int(at.timestamp() * 1000)}
    if response or stop != "error":
        m["responseModel"] = response or model
    if error:
        m["errorMessage"] = error
    return {"type": "message", "id": "x", "message": m}


def make_db(path, events, compress=()) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    for i, e in enumerate(events):
        text = json.dumps(e)
        if i in compress:
            blob = subprocess.run(["zstd", "-q", "-c"], input=text.encode(), capture_output=True,
                                  check=True).stdout
            con.execute("insert into transcript_events values ('s', ?, null, 0, ?, ?, null)",
                        (i, blob, len(text)))
        else:
            con.execute("insert into transcript_events values ('s', ?, ?, 0, null, null, null)",
                        (i, text))
    con.commit()
    con.close()
    return str(path)


RATE = '{"type":"error","error":{"type":"rate_limit_error","message":"rate limit"}}'


def test_metered_spend_per_period(tmp_path):
    db = make_db(tmp_path / "a" / "openclaw-agent.sqlite", [
        turn(NOW - timedelta(hours=2), "qwen/qwen3.8-27b", "singularitycompute", cost=1.5),
        turn(NOW - timedelta(hours=1), "qwen/qwen3.8-27b", "singularitycompute", cost=2.0),
        turn(NOW - timedelta(days=2), "qwen/qwen3.8-27b", "singularitycompute", cost=9.0),
        turn(NOW - timedelta(hours=1), "claude-opus-5-5", cost=7.0)])
    r = openclaw.read_sessions({"paths": [db]}, "sc-api", TABLE, NOW)
    assert r.windows["usd/day"][0] == pytest.approx(3.5)


def test_rate_limit_errors_put_the_pool_at_a_limit(tmp_path):
    t0 = NOW - timedelta(minutes=10)
    db = make_db(tmp_path / "a.sqlite", [turn(t0, "claude-opus-5-5", stop="error", error=RATE)])
    r = openclaw.read_sessions({"paths": [db], "down_minutes": 30}, "claude-plan", TABLE, NOW)
    assert r.limited_until == t0 + timedelta(minutes=30)
    # a later successful turn on the pool clears it
    db2 = make_db(tmp_path / "b.sqlite", [turn(t0, "claude-opus-5-5", stop="error", error=RATE),
                                          turn(t0 + timedelta(minutes=5), "claude-sonnet-5-5")])
    assert openclaw.read_sessions({"paths": [db2]}, "claude-plan", TABLE, NOW).limited_until is None
    # other errors aren't limits
    db3 = make_db(tmp_path / "c.sqlite", [turn(t0, "claude-opus-5-5", stop="error",
                                               error="request timed out")])
    assert openclaw.read_sessions({"paths": [db3]}, "claude-plan", TABLE, NOW).limited_until is None


@pytest.mark.skipif(shutil.which("zstd") is None, reason="needs zstd")
def test_compressed_events_are_read(tmp_path):
    db = make_db(tmp_path / "a.sqlite", [
        turn(NOW - timedelta(hours=1), "qwen/qwen3.8-27b", "singularitycompute", cost=4.0)],
        compress={0})
    r = openclaw.read_sessions({"paths": [db]}, "sc-api", TABLE, NOW)
    assert r.windows["usd/day"][0] == pytest.approx(4.0)


def test_sources_reader_feeds_the_state(tmp_path):
    db = make_db(tmp_path / "agents" / "main" / "openclaw-agent.sqlite",
                 [turn(NOW - timedelta(minutes=1), "claude-opus-5-5", stop="error", error=RATE)])
    sources = {"format": "hive-route.sources/1", "pools": {"claude-plan": [
        {"reader": "openclaw-sessions", "paths": [str(tmp_path / "agents" / "*" / "*.sqlite")]}]}}
    st, notes = collect(TABLE, sources, NOW)
    assert "limited_until" in st["pools"]["claude-plan"]
    assert notes["claude-plan"]["source"].startswith("1 turns in 1 databases")
    assert db


# --------------------------------------------------------------------------- drift
def bound_log(tmp_path, db: str) -> str:
    log = str(tmp_path / "log.jsonl")
    b = bindings(sessions=[db])
    openclaw.bind(TABLE, b, state(), "live", log)
    return log


def test_drift_when_the_provider_answers_with_another_model(tmp_path):
    db = tmp_path / "s.sqlite"
    log = bound_log(tmp_path, str(db))
    quals = tmp_path / "q.json"
    quals.write_text(json.dumps({"oc-opus": {"status": "qualified",
                                             "route_pin": TABLE.route_pin("oc-opus")}}))
    later = datetime.now(UTC) + timedelta(seconds=5)
    make_db(db, [turn(later, "claude-opus-5-5", response="claude-opus-5-5-20261001"),  # snapshot
                 turn(later, "gpt-5.6-sol", "openai"),                                 # fallback
                 turn(later, "claude-opus-5-5", response="claude-haiku-4-5")])
    events = openclaw.observe(log, str(quals))
    assert len(events) == 1
    d = events[0]["data"]
    assert d["route_id"] == "oc-opus" and d["observed_models"] == ["anthropic/claude-haiku-4-5"]
    assert json.loads(quals.read_text())["oc-opus"]["status"] == "candidate"
    assert openclaw.observe(log, str(quals)) == []       # once per binding version


def test_an_unbound_model_is_drift_without_demotion(tmp_path):
    db = tmp_path / "s.sqlite"
    log = bound_log(tmp_path, str(db))
    make_db(db, [turn(datetime.now(UTC) + timedelta(seconds=5), "glm-5.2", "openrouter")])
    [ev] = openclaw.observe(log)
    assert ev["data"]["observed_models"] == ["openrouter/glm-5.2"]
    assert ev["data"]["route_id"] == "oc-opus"


# --------------------------------------------------------------------------- canaries
def test_models_from_an_exec_envelope(tmp_path):
    out = tmp_path / "o.out"
    out.write_text('starting\n{\n  "ok": true,\n  "status": "ok",\n  "model": "gpt-6-astra",\n'
                   '  "provider": "openai"\n}\n')
    assert openclaw.models_exec(out) == {"openai/gpt-6-astra"}
    out.write_text('{"ok": false, "status": "error", "model": null, "provider": null}\n')
    assert openclaw.models_exec(out) == set()


# --------------------------------------------------------------------------- CLI
def test_cli_writes_patches_and_checks(tmp_path, capsys):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({"models": {"providers": {"anthropic": {}, "openai": {}}},
                               "agents": {"defaults": {"modelPolicy": {"allow": [
                                   "anthropic/claude-opus-5-5", "openai/gpt-5.6-sol"]}}}}))
    b = {"format": "hive-route.bindings/1", "bindings": {"cosmo": {
        "config": str(cfg), "target": "defaults", "kind": "work"}}}
    bf, sf = tmp_path / "b.yaml", tmp_path / "state.json"
    bf.write_text(json.dumps(b))
    sf.write_text(json.dumps(state()))
    rc = main(["openclaw-config", str(FIXTURES / "tables" / "openclaw.yaml"), str(bf),
               "--state", str(sf), "--write-dir", str(tmp_path / "out"), "--check"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"]["cosmo"]["primary"] == "anthropic/claude-opus-5-5"
    written = json.loads((tmp_path / "out" / "openclaw.patch.json").read_text())
    assert written == out["patches"][str(cfg)]
