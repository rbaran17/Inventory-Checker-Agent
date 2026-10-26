"""Client population, sampling, and the automatic assessment of what the camera saw.

Auditor workflow this POC follows:
1. the client provides the inventory population (book quantities by SKU and location);
2. the auditor draws a sample (all items above a value threshold, plus a random selection);
3. the auditor counts the sampled items at the location (here: with the phone camera);
4. counts are compared with the book quantities of the sampled items.
"""

from __future__ import annotations

import csv
import io
import random
from typing import Any

from pydantic import BaseModel, Field


class Item(BaseModel):
    sku: str
    description: str
    location: str = ""
    book_qty: int = Field(ge=0)
    unit_cost: float = Field(0, ge=0)

    @property
    def value(self) -> float:
        return self.book_qty * self.unit_cost


def _i(sku: str, desc: str, loc: str, qty: int, cost: float) -> Item:
    return Item(sku=sku, description=desc, location=loc, book_qty=qty, unit_cost=cost)


# Fake client population of everyday items, so the flow can be tested at a desk or in an office.
FAKE_POPULATION: list[Item] = [
    _i("OFF-1001", "Laptop computer", "Desk", 1, 1400),
    _i("OFF-1002", "Computer monitor", "Desk", 1, 320),
    _i("OFF-1003", "Computer mouse", "Desk", 1, 35),
    _i("OFF-1004", "Keyboard", "Desk", 1, 60),
    _i("OFF-1005", "Mobile phone", "Desk", 1, 900),
    _i("OFF-1006", "Tablet", "Desk", 1, 600),
    _i("OFF-1007", "Headphones or earbuds", "Drawer", 1, 180),
    _i("OFF-1008", "Charging cable", "Drawer", 3, 15),
    _i("OFF-1009", "Power adapter or charger", "Drawer", 2, 45),
    _i("OFF-1010", "Desk lamp", "Desk", 1, 55),
    _i("STA-2001", "Pen or pencil", "Desk", 6, 2),
    _i("STA-2002", "Book or notebook", "Shelf", 4, 12),
    _i("STA-2003", "Stapler", "Drawer", 1, 18),
    _i("STA-2004", "Scissors", "Drawer", 1, 9),
    _i("STA-2005", "Sticky notes pad", "Drawer", 2, 4),
    _i("STA-2006", "Paper clip box", "Drawer", 1, 3),
    _i("STA-2007", "Tape dispenser", "Desk", 1, 8),
    _i("STA-2008", "Folder or binder", "Shelf", 3, 6),
    _i("KIT-3001", "Coffee mug", "Kitchen", 2, 10),
    _i("KIT-3002", "Water bottle", "Kitchen", 2, 25),
    _i("KIT-3003", "Drinking glass", "Kitchen", 4, 5),
    _i("KIT-3004", "Plate", "Kitchen", 4, 8),
    _i("KIT-3005", "Bowl", "Kitchen", 3, 7),
    _i("KIT-3006", "Fork", "Kitchen", 4, 3),
    _i("KIT-3007", "Spoon", "Kitchen", 4, 3),
    _i("KIT-3008", "Knife", "Kitchen", 2, 6),
    _i("KIT-3009", "Banana", "Kitchen", 3, 0.3),
    _i("KIT-3010", "Apple or orange", "Kitchen", 4, 0.5),
    _i("HOM-4001", "Chair", "Room", 2, 120),
    _i("HOM-4002", "Potted plant", "Room", 1, 30),
    _i("HOM-4003", "Remote control", "Room", 1, 20),
    _i("HOM-4004", "Backpack or bag", "Room", 1, 70),
    _i("HOM-4005", "Wall or desk clock", "Room", 1, 25),
    _i("HOM-4006", "Shoe", "Room", 2, 60),
    _i("HOM-4007", "Umbrella", "Room", 1, 15),
]


def parse_population_csv(text: str) -> list[Item]:
    """CSV with columns sku, description, book_qty and optional location, unit_cost."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = {(f or "").strip().lower() for f in reader.fieldnames or []}
    missing = {"sku", "description", "book_qty"} - fields
    if missing:
        raise ValueError(f"Population CSV is missing column(s): {', '.join(sorted(missing))}")
    items, seen = [], set()
    for line, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not row.get("sku"):
            continue
        if row["sku"] in seen:
            raise ValueError(f"Row {line}: duplicate SKU {row['sku']}")
        try:
            qty = int(float(row["book_qty"] or 0))
            cost = float(row.get("unit_cost") or 0)
        except ValueError as exc:
            raise ValueError(f"Row {line}: book_qty and unit_cost must be numbers") from exc
        items.append(Item(sku=row["sku"], description=row["description"], location=row.get("location", ""),
                          book_qty=qty, unit_cost=cost))
        seen.add(row["sku"])
    if not items:
        raise ValueError("Population CSV has no rows")
    return items


def draw_sample(population: list[Item], *, size: int, value_threshold: float | None = None,
                seed: int | None = None) -> dict[str, str]:
    """Every item at or above ``value_threshold``, plus ``size`` random others. Returns {sku: reason}."""
    chosen: dict[str, str] = {}
    if value_threshold is not None:
        chosen = {i.sku: "value_threshold" for i in population if i.value >= value_threshold}
    rest = [i for i in population if i.sku not in chosen]
    rng = random.Random(seed)
    for i in rng.sample(rest, min(max(size, 0), len(rest))):
        chosen[i.sku] = "random"
    return {i.sku: chosen[i.sku] for i in population if i.sku in chosen}


def assess(population: list[Item], sample: dict[str, str], scans: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare camera counts with the book quantities of the sampled items.

    Each scan covers one view (a photo or a short video segment). The count for a SKU is the highest
    count in any single scan, so filming the same shelf twice does not double count.
    """
    by_sku = {i.sku: i for i in population}
    best: dict[str, dict[str, Any]] = {}
    not_sampled: dict[str, int] = {}
    not_in_population = []
    for scan in scans:
        per_scan: dict[str, int] = {}
        for obj in scan["objects"]:
            sku = obj.get("matched_sku")
            if sku in by_sku:
                per_scan[sku] = per_scan.get(sku, 0) + int(obj["count"])
            else:
                not_in_population.append({"scan_id": scan["id"], "name": obj["name"], "count": obj["count"]})
        for sku, qty in per_scan.items():
            if sku in sample:
                if sku not in best or qty > best[sku]["qty"]:
                    best[sku] = {"qty": qty, "scan_id": scan["id"]}
            else:
                not_sampled[sku] = max(not_sampled.get(sku, 0), qty)
    rows = []
    for sku, reason in sample.items():
        item = by_sku[sku]
        seen = best.get(sku)
        diff = None if seen is None else seen["qty"] - item.book_qty
        status = "not_counted" if seen is None else ("match" if diff == 0 else "difference")
        rows.append({**item.model_dump(), "sample_reason": reason, "counted_qty": seen["qty"] if seen else None,
                     "difference": diff, "status": status, "evidence_scan": seen["scan_id"] if seen else None})
    summary = {s: sum(1 for r in rows if r["status"] == s) for s in ("match", "difference", "not_counted")}
    return {
        "sample": rows, "summary": summary,
        "seen_not_sampled": [{"sku": s, "description": by_sku[s].description, "counted_qty": q}
                             for s, q in not_sampled.items()],
        "not_in_population": not_in_population,
    }
