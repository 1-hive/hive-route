# SPDX-License-Identifier: GPL-3.0-or-later
"""Routes reached through the hive's gateway (§8), and pools checked by health (§7)."""
from __future__ import annotations

import json
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import yaml
from conftest import ROOT

from hiveroute.cli import main
from hiveroute.decide import decide
from hiveroute.gateway import litellm_config
from hiveroute.table import Table, load_table
from hiveroute.usage import collect

BASE = load_table(ROOT / "examples" / "1-hive.yaml")


def with_gateway_route() -> Table:
    data = json.loads(json.dumps(BASE.data))
    data["pools"]["sc"] = {"kind": "local", "limits": [{"concurrent": 2}],
                           "gateway_key_env": "SC_KEY", "gateway_drop_params": ["safeguards"]}
    data["routes"]["sc-big"] = {"tier": "standard", "pool": "sc", "family": "deepseek",
                                "harness": "claude-code", "model": "deepseek/v4",
                                "gateway_model": "custom_openai/deepseek/v4",
                                "endpoint": "https://sc.example/v1", "via_gateway": True,
                                "tools": True}
    data["tiers"]["standard"] = ["sc-big", *data["tiers"]["standard"]]
    data["prefer"] = "order"
    return Table.from_data(data)


class Health(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(Health.status if self.headers.get("Authorization") == "Bearer k"
                           else 401)
        self.end_headers()

    def log_message(self, *a):
        pass


def serve() -> str:
    srv = HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}/v1/models"


def qualified(t: Table) -> dict:
    return {rid: {"status": "qualified", "route_pin": t.route_pin(rid)} for rid in t.routes}


def test_decision_marks_gateway_routes_only_when_set():
    t = with_gateway_route()
    req = {"task": "t", "attempt": "a", "facts": {"kind": "work", "specification": "partial",
                                                   "verification": "independent", "scope": "many",
                                                   "consequence": "reversible", "leverage": 0}}
    d = decide(req, t, {"as_of": "2026-09-29T12:00:00Z", "routes": qualified(t)}, "live")
    assert (d["route_id"], d["via_gateway"]) == ("sc-big", True)
    d2 = decide({**req, "attempt": "b"}, BASE, {"as_of": "2026-09-29T12:00:00Z"}, "fixed")
    assert "via_gateway" not in d2  # earlier decisions keep their shape


def test_health_down_pool_waits_and_falls_back(tmp_path):
    url = serve()
    t = with_gateway_route()
    env = tmp_path / "k.env"
    env.write_text("SC_KEY=k\n")
    src = {"format": "hive-route.sources/1", "pools": {"sc": [
        {"reader": "http-health", "paths": [url], "key_env": "SC_KEY", "env_file": str(env)}]}}
    now = datetime.now(UTC)
    Health.status = 200
    state, notes = collect(t, src, now)
    assert "sc" not in state.get("pools", {}) and notes["sc"]["source"] == url
    Health.status = 503
    state, _ = collect(t, src, now)
    assert "limited_until" in state["pools"]["sc"]
    state["routes"] = qualified(t)
    req = {"task": "t", "attempt": "a", "facts": {"kind": "work", "specification": "partial",
                                                   "verification": "independent", "scope": "many",
                                                   "consequence": "reversible", "leverage": 0}}
    d = decide(req, t, state, "live")
    # the next standard route in listed order: the table's own SingularityCompute route
    assert d["route_id"] == "sc-deepseek-v4" and "sc-big" in d["rejected"]


def test_gateway_config_drops_named_params_only():
    [m] = [m for m in litellm_config(with_gateway_route(), database=False)["model_list"]
           if m["model_name"] == "sc-big"]
    assert m["litellm_params"]["additional_drop_params"] == ["safeguards"]
    assert m["litellm_params"]["api_base"] == "https://sc.example/v1"


FAKE = """
import json, os, pathlib, sys
print(json.dumps({"type": "assistant", "message": {"model": sys.argv[1], "content": []}}))
ok = os.environ.get("GW_URL") == "http://gw" and os.environ.get("SC_GW_KEY") == "secret"
pathlib.Path("done.txt").write_text("done" if ok else "no gateway env")
"""


def test_canary_through_the_gateway(tmp_path, capsys):
    t = with_gateway_route()
    table = tmp_path / "t.yaml"
    table.write_text(yaml.safe_dump(t.data))
    suite = tmp_path / "suite"
    (suite / "one").mkdir(parents=True)
    (suite / "one" / "PROMPT.md").write_text("go")
    (suite / "one" / "check.py").write_text(
        "import os, pathlib, sys\n"
        "p = pathlib.Path(os.environ['CANARY_WORK']) / 'done.txt'\n"
        "sys.exit(0 if p.exists() and p.read_text() == 'done' else 1)\n")
    (suite / "suite.yaml").write_text(json.dumps({"format": "hive-route.suite/1", "name": "t",
        "cases": [{"id": "one", "kind": "work", "prompt": "one/PROMPT.md",
                   "check": [sys.executable, "{case}/check.py"]}]}))
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE)
    keyfile = tmp_path / "gw.env"
    keyfile.write_text("SC_GW_KEY=secret\n")
    sources = tmp_path / "sources.yaml"
    sources.write_text(json.dumps({
        "format": "hive-route.sources/1", "qualifications": str(tmp_path / "q.json"),
        "canary_workdir": str(tmp_path / "work"),
        "gateway": {"url": "http://gw", "key_env": "SC_GW_KEY", "env_file": str(keyfile)},
        "harnesses": {"claude-code": {"argv": [sys.executable, str(fake), "{model}"],
                                      "gateway_env": {"GW_URL": "{gateway_url}"}}}}))
    assert main(["canary", "run", str(table), "sc-big", "--suite", str(suite),
                 "--sources", str(sources), "--log", str(tmp_path / "log.jsonl")]) == 0
    q = json.loads((tmp_path / "q.json").read_text())["sc-big"]
    assert q["status"] == "qualified" and q["results"][0]["observed_models"] == ["sc-big"]
