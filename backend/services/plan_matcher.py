"""
Reads plan pricing from two hand-editable CSVs instead of one wide,
error-prone sheet:

  data/plans.csv             - the plan catalog. One row per plan:
                                id, name, price, period, features.
                                Edit THIS to change a price or feature.
  data/plan_availability.csv - one row per category/tonnage/location
                                combo, listing which plan_ids (from
                                plans.csv) apply and which actions are
                                allowed. Edit THIS to change which plans
                                are offered for a given setup.

Splitting these means the team edits a price ONCE in plans.csv instead
of hunting down and re-typing it on every combo row that offers that
plan - the old single-CSV format repeated the full price/feature text
on every row, which is exactly what made it easy to typo or update only
half the rows.
"""
import csv
import os

_PLANS_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "plans.csv")
_AVAIL_CSV = os.path.join(os.path.dirname(__file__), "..", "data", "plan_availability.csv")
_cache = None
_cache_mtimes = None


def _current_mtimes():
    try:
        return (os.path.getmtime(_PLANS_CSV), os.path.getmtime(_AVAIL_CSV))
    except OSError:
        return None

_CATEGORY_MAP = {
    "Heating": "heating",
    "Cooling with Heat Pump": "cooling_heat_pump",
    "Cooling with Electric Heat": "cooling_electric_heat",
}
_TONNAGE_MAP = {"2": "2_ton", "2.5": "2.5_ton", "3": "3_ton", "3.5": "3.5_ton", "4": "4_ton"}
_LOCATION_MAP = {
    "Attic Horizontal": "attic_horizontal",
    "Closet Vertical": "closet_vertical",
    "Garage Vertical": "garage_vertical",
}


def _price_display(price: str, period: str) -> str:
    price = str(int(float(price)))
    period = period.strip()
    head, _, unit = period.partition(" ")
    if head.isdigit() and int(head) != 1 and not unit.endswith("s"):
        period = f"{head} {unit}s"
    return f"${price} / {period}"


def _load():
    global _cache, _cache_mtimes
    mtimes = _current_mtimes()
    if _cache is not None and mtimes == _cache_mtimes:
        return _cache

    catalog = {}
    with open(_PLANS_CSV, newline="") as f:
        for row in csv.DictReader(f):
            pid = row["plan_id"].strip()
            if not pid:
                continue
            catalog[pid] = {
                "id": pid,
                "name": row["name"].strip(),
                "monthly_price": float(row["monthly_price"]),
                "price_display": _price_display(row["monthly_price"], row["price_period"]),
                "features": [x for x in row.get("features", "").split(";") if x],
            }

    table = {}
    with open(_AVAIL_CSV, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("status") != "has_plans":
                continue
            cat = _CATEGORY_MAP.get(row["category"].strip())
            ton = _TONNAGE_MAP.get(row["tonnage"].strip())
            loc = _LOCATION_MAP.get(row["unit_location"].strip())
            if not (cat and ton and loc):
                continue
            actions = [x.strip() for x in row.get("actions_allowed", "").split(",") if x.strip()]
            plans = []
            for pid in row.get("plan_ids", "").split(","):
                pid = pid.strip()
                plan = catalog.get(pid)
                if not plan:
                    continue  # plan_id referenced here but missing from plans.csv - skip rather than crash
                plans.append({**plan, "actions_allowed": actions})
            table[(cat, ton, loc)] = plans
    _cache = table
    _cache_mtimes = mtimes
    return table


def reload():
    """No longer required after editing a CSV - _load() now checks file
    mtimes on every call and rebuilds automatically. Kept as a no-op for
    any code that still calls it explicitly (e.g. an admin 'reload' button)."""
    global _cache
    _cache = None


def get_available_plans(category: str, tonnage: str, location: str):
    return list(_load().get((category, tonnage, location), []))