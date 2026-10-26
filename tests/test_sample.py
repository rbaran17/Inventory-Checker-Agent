from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from inventory_checker.sample import (
    TEMPLATE_CSV,
    assess,
    compare,
    demo_sample,
    normalise,
    parse_sample,
    reconcile,
)


def test_parse_csv_maps_core_columns_and_keeps_everything_else_as_attributes():
    s = parse_sample(b"Item No;Item Description;Book Qty;Bin;Serial Number;Model No.\n"
                     b"A1;Laptop;1;Office;C02XK1ZZ;A2337\nA2;Monitor;2;Office;;P2422H\n", "listing.csv")
    assert s.attribute_names == ["Serial Number", "Model No."]
    a1, a2 = s.items
    assert (a1.item_id, a1.description, a1.expected_qty, a1.location) == ("A1", "Laptop", 1, "Office")
    assert a1.attributes == {"Serial Number": "C02XK1ZZ", "Model No.": "A2337"}
    assert a2.attributes["Serial Number"] == ""  # blank = record only


def test_parse_xlsx_and_template():
    wb = Workbook()
    ws = wb.active
    ws.append(["SKU", "Name", "Quantity", "serial_number"])
    ws.append(["X-1", "Drill", 3.0, 12345])
    buf = io.BytesIO()
    wb.save(buf)
    s = parse_sample(buf.getvalue(), "items.xlsx")
    assert s.items[0].item_id == "X-1" and s.items[0].expected_qty == 3
    assert s.items[0].attributes == {"Serial number": "12345"}
    t = parse_sample(TEMPLATE_CSV.encode(), "template.csv")
    assert t.attribute_names == ["Serial number", "Model number", "Manufacturer"] and len(t.items) == 2


def test_parse_errors_are_actionable():
    with pytest.raises(ValueError, match="No description column"):
        parse_sample(b"id,serial\n1,2\n")
    with pytest.raises(ValueError, match="appears twice"):
        parse_sample(b"id,description\nA,x\nA,y\n")
    with pytest.raises(ValueError, match="not a number"):
        parse_sample(b"id,description,qty\nA,x,lots\n")
    assert parse_sample(b"description\nMug\n").items[0].expected_qty == 1  # qty defaults to 1, id generated


def test_compare_values_read_from_labels():
    assert normalise("S/N: C02X-K1ZZ") == "C02XK1ZZ"
    assert compare("C02XK1ZZJG5H", ["c02x k1zz jg5h"]) == "match"
    assert compare("C02XK1ZZJG5H", ["C02XK1ZZJG5B"]) == "near_match"  # one misread character on a long code
    assert compare("A2337", ["A2338"]) == "mismatch"  # short codes must match exactly
    assert compare("Dell", []) == "not_read"
    assert compare("", ["XYZ"]) == "recorded" and compare("", []) == "n/a"


def _scan(sid, *objs):
    return {"id": sid, "objects": [dict(o) for o in objs]}


def test_reconcile_prefers_serial_over_description():
    s = parse_sample(b"id,description,serial\nL1,Laptop,SN-111111\nL2,Laptop,SN-222222\n")
    obj = {"name": "laptop", "count": 1, "matched_item_id": "L1",
           "attributes": [{"name": "Serial", "value": "222222"}]}
    assert reconcile(s, obj) == ("L2", "Serial")
    assert reconcile(s, {**obj, "attributes": []}) == ("L1", "description")
    assert reconcile(s, {**obj, "matched_item_id": "NOPE", "attributes": []}) == (None, "none")


def test_assess_statuses_and_reviews():
    s = parse_sample(b"id,description,qty,serial,model\n"
                     b"A,Laptop,1,C02XK1ZZJG5H,A2337\nB,Mug,2,,\nC,Monitor,1,,P2422H\nD,Pen,4,,\n")
    scans = [
        _scan("S001", {"name": "laptop", "count": 1, "item_id": "A",
                       "attributes": [{"name": "serial", "value": "C02XK1ZZJG5H"}, {"name": "Model", "value": "A2337"}]},
              {"name": "mug", "count": 1, "item_id": "B", "attributes": []},
              {"name": "duck", "count": 1, "item_id": None, "attributes": []}),
        _scan("S002", {"name": "mug", "count": 1, "item_id": "B", "attributes": []},
              {"name": "monitor", "count": 1, "item_id": "C", "attributes": []}),
    ]
    a = assess(s, scans, {"C": {"decision": "accepted", "note": "label behind desk"}})
    rows = {r["item_id"]: r for r in a["items"]}
    assert rows["A"]["status"] == "verified"
    assert rows["B"]["status"] == "exception" and rows["B"]["counted_qty"] == 1  # max per scan, not summed
    assert rows["C"]["status"] == "check"  # model number not read
    assert rows["D"]["status"] == "not_scanned"
    assert a["summary"] == {"verified": 1, "check": 1, "exception": 1, "not_scanned": 1, "total": 4,
                            "reviewed": 1, "to_review": 2}
    assert a["not_in_sample"][0]["name"] == "duck"


def test_demo_sample_is_ready_to_use():
    d = demo_sample()
    assert len(d.items) == 12 and d.attribute_names == ["Brand", "Model number", "Serial number"]
    assert all(v == "" for i in d.items for v in i.attributes.values())
