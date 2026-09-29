# SPDX-License-Identifier: GPL-3.0-or-later
"""The rule fixtures in fixtures/cases: one expected decision per case."""

from __future__ import annotations

import pytest
from conftest import build_case, cases

from hiveroute import decide


@pytest.mark.parametrize("case", cases())
def test_case(case):
    d = decide(*build_case(case))
    exp = case["expect"]
    for key in ("decision", "tier", "computed_tier", "route_id", "wait_until"):
        if key in exp:
            assert d[key] == exp[key], (key, d)
    rules = {r["rule"] for r in d["reasons"]}
    assert set(exp.get("rules", [])) <= rules, d["reasons"]
    assert not set(exp.get("not_rules", [])) & rules, d["reasons"]
    assert set(exp.get("rejected", [])) <= set(d["rejected"]), d["rejected"]
    assert not set(exp.get("not_rejected", [])) & set(d["rejected"]), d["rejected"]
    if "suggest" in exp:
        assert bool(d["suggestions"]) == exp["suggest"], d["suggestions"]
