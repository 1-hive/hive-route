# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import copy

import pytest
from conftest import FIXTURES, ROOT, fixture_table

from hiveroute import RouteError, Table, load_table


def raw() -> dict:
    import yaml
    return yaml.safe_load((FIXTURES / "tables" / "mixed.yaml").read_text())


def test_example_tables_load():
    assert fixture_table().pin.startswith("sha256:")
    load_table(ROOT / "examples" / "1-hive.yaml")


def test_pin_is_stable_and_content_addressed():
    a, b = Table.from_data(raw()), Table.from_data(raw())
    assert a.pin == b.pin
    changed = raw()
    changed["soft_threshold"] = 0.7
    assert Table.from_data(changed).pin != a.pin


def test_defaults_are_part_of_the_pin():
    r = raw()
    del r["prefer"]
    assert Table.from_data(r).data["prefer"] == "order"
    r2 = raw()
    r2["prefer"] = "order"
    assert Table.from_data(r).pin == Table.from_data(r2).pin


def test_changing_a_route_changes_only_its_pin():
    a = Table.from_data(raw())
    r = raw()
    r["routes"]["opus-plan"]["model"] = "claude-strong-y"
    b = Table.from_data(r)
    assert a.route_pin("opus-plan") != b.route_pin("opus-plan")
    assert a.route_pin("opus-api") == b.route_pin("opus-api")


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r["routes"]["opus-api"].update(pool="nope"), "unknown pool"),
    (lambda r: r["tiers"]["strong"].append("mini-api"), "has tier"),
    (lambda r: r["tiers"]["light"].append("ghost"), "unknown route"),
    (lambda r: r["fixed"].update(work="ghost"), "unknown route"),
    (lambda r: r["scorer"].update(route="ghost"), "unknown route"),
    (lambda r: r["pools"]["claude-plan"]["limits"].append({"usd": 5, "per": "day"}), "metered pool"),
    (lambda r: r["pools"]["claude-plan"]["limits"].append({"window": "5h"}), "duplicate"),
    (lambda r: r.update(allow_upgrade_on_wait=["strong"]), "no tier above"),
    (lambda r: r.update(prefer="cheapest"), "prefer"),
    (lambda r: r["routes"]["opus-api"].pop("family"), "family"),
    (lambda r: r.update(format="hive-route.table/2"), "format"),
])
def test_invalid_tables_are_refused(mutate, message):
    r = copy.deepcopy(raw())
    mutate(r)
    with pytest.raises(RouteError) as e:
        Table.from_data(r)
    assert e.value.code == "TABLE_INVALID"
    assert message in e.value.reason


def test_yaml_errors_are_table_errors(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("pools: [unclosed")
    with pytest.raises(RouteError):
        load_table(p)
