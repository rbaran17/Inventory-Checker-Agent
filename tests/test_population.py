from __future__ import annotations

import pytest

from inventory_checker.population import FAKE_POPULATION, assess, draw_sample, parse_population_csv


def _scan(sid, *objs):
    return {"id": sid, "objects": [{"name": n, "count": c, "matched_sku": s} for n, c, s in objs]}


def test_fake_population_is_unique_and_sample_is_reproducible():
    assert len({i.sku for i in FAKE_POPULATION}) == len(FAKE_POPULATION)
    a = draw_sample(FAKE_POPULATION, size=6, value_threshold=500, seed=42)
    assert a == draw_sample(FAKE_POPULATION, size=6, value_threshold=500, seed=42)
    high = {s for s, r in a.items() if r == "value_threshold"}
    assert high == {i.sku for i in FAKE_POPULATION if i.value >= 500}
    assert sum(1 for r in a.values() if r == "random") == 6


def test_parse_population_csv():
    items = parse_population_csv("sku,description,location,book_qty,unit_cost\nA1,Widget,Bin 3,12,2.5\n")
    assert items[0].sku == "A1" and items[0].book_qty == 12 and items[0].value == 30
    with pytest.raises(ValueError, match="missing"):
        parse_population_csv("sku,description\nA,b\n")
    with pytest.raises(ValueError, match="duplicate"):
        parse_population_csv("sku,description,book_qty\nA,b,1\nA,c,2\n")


def test_assess_match_difference_not_counted_and_out_of_sample():
    sample = {"KIT-3001": "random", "STA-2001": "random", "OFF-1001": "value_threshold"}
    scans = [
        _scan("scan_001", ("mug", 2, "KIT-3001"), ("pen", 4, "STA-2001"), ("plant", 1, "HOM-4002")),
        _scan("scan_002", ("pen", 5, "STA-2001"), ("rubber duck", 1, None)),  # second view: higher count wins
    ]
    a = assess(FAKE_POPULATION, sample, scans)
    rows = {r["sku"]: r for r in a["sample"]}
    assert rows["KIT-3001"]["status"] == "match" and rows["KIT-3001"]["counted_qty"] == 2
    assert rows["STA-2001"]["status"] == "difference" and rows["STA-2001"]["difference"] == -1
    assert rows["STA-2001"]["evidence_scan"] == "scan_002"
    assert rows["OFF-1001"]["status"] == "not_counted"
    assert a["summary"] == {"match": 1, "difference": 1, "not_counted": 1}
    assert a["seen_not_sampled"] == [{"sku": "HOM-4002", "description": "Potted plant", "counted_qty": 1}]
    assert a["not_in_population"][0]["name"] == "rubber duck"


def test_same_sku_twice_in_one_view_is_added():
    a = assess(FAKE_POPULATION, {"STA-2001": "random"},
               [_scan("s", ("pen", 3, "STA-2001"), ("pencil", 3, "STA-2001"))])
    assert a["sample"][0]["counted_qty"] == 6 and a["sample"][0]["status"] == "match"
