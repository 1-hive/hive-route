# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hiveroute.table import Table, load_table

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
NOW = "2026-09-29T12:00:00Z"


def fixture_table(name: str = "mixed.yaml") -> Table:
    return load_table(FIXTURES / "tables" / name)


def qualified(table: Table, ids=None, stale=()) -> dict:
    """State entries marking routes qualified at their current pins (stale ones at another)."""
    ids = table.routes if ids is None else ids
    return {rid: {"status": "qualified",
                  "route_pin": "sha256:" + "0" * 64 if rid in stale else table.route_pin(rid)}
            for rid in ids}


def build_case(case: dict) -> tuple[dict, Table, dict, str]:
    table = fixture_table(case.get("table", "mixed.yaml"))
    qualify = case.get("qualify", "all")
    state = {"as_of": NOW, **case.get("state", {}),
             "routes": qualified(table, None if qualify == "all" else qualify,
                                 case.get("stale", ()))}
    request = {"task": "t", "attempt": "t.9", **case["request"]}
    return request, table, state, case.get("mode", "live")


def cases() -> list:
    out = []
    for f in sorted((FIXTURES / "cases").glob("*.json")):
        for c in json.loads(f.read_text()):
            out.append(pytest.param(c, id=f"{f.stem}: {c['name']}"))
    return out


@pytest.fixture
def table() -> Table:
    return fixture_table()
