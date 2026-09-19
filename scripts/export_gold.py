#!/usr/bin/env python3
"""Export a small, upload-ready gold bundle from the local data layer.

WHY THIS EXISTS
---------------
Databricks Free Edition restricts outbound internet to a limited (unpublished)
set of trusted domains, and serverless UDFs can never reach the internet. So
ingestion happens at the edge (this laptop) and the platform owns storage,
governance, reasoning and serving.

This script runs the SAME gold logic the agent uses, and writes one compact JSON
bundle that can be dropped into a Unity Catalog Volume or workspace file and
loaded into Delta tables in a notebook.

    python3 scripts/export_gold.py            # uses the frozen fixtures (deterministic)
    python3 scripts/export_gold.py --live     # uses the live cache

Output: gold/gold_bundle.json (a few tens of KB) + a README stub.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Default to the deterministic frozen store; --live opts into the live cache.
if "--live" not in sys.argv:
    os.environ.setdefault("DEMO_MODE", "cache")

from hokieday import config, dining, gtfs, livebus  # noqa: E402

OUT_DIR = REPO / "gold"
BUNDLE = OUT_DIR / "gold_bundle.json"

# Stops worth pre-computing for the demo (Burruss-adjacent + a few others).
DEMO_STOPS = ["1600", "1628", "1500", "1521", "1601"]
# FoodPro locations to precompute menus for.
DEMO_LOCATIONS = ["15", "39", "09", "72"]


def build_bundle() -> dict:
    now_campus = config.now(ZoneInfo(config.CAMPUS_TZ))
    service_date = now_campus.date()
    g = gtfs.load_gtfs()

    # ---- transit -----------------------------------------------------------
    departures: list[dict] = []
    for stop_id in DEMO_STOPS:
        for d in gtfs.next_departures(g, stop_id, now_campus, limit=8):
            departures.append({
                "stop_id": stop_id,
                "route_id": d.route_id,
                "head_sign": d.head_sign,
                "dep_time": d.dep_time.isoformat(timespec="seconds"),
                "in_min": round(d.in_min, 2),
                "service_date": d.service_date.isoformat(),
            })

    stops = []
    for stop_id, s in g.stops.items():
        stops.append({"stop_id": s.stop_id, "name": s.name, "lat": s.lat, "lon": s.lon})

    # ---- live buses (replayed snapshot) ------------------------------------
    live = []
    for r in livebus.live():
        live.append({
            "bus_id": r.bus_id, "route_id": r.route_id, "stop_id": r.stop_id,
            "lat": r.lat, "lon": r.lon, "load_pct": r.load_pct,
            "at_stop": r.at_stop, "gtfs_trip_id": r.gtfs_trip_id,
            "observed_at": r.observed_at.isoformat(timespec="seconds"),
            "sched_delta_min": r.sched_delta_min, "is_stale": r.is_stale,
        })

    # ---- dining ------------------------------------------------------------
    eat_options: list[dict] = []
    hours_rows: list[dict] = []
    for loc in DEMO_LOCATIONS:
        try:
            for it in dining.menu(loc, service_date):
                eat_options.append({
                    "location_num": loc, "meal": it.meal, "section": it.section,
                    "recipe_id": it.recipe_id, "name": it.name,
                    "portion": f"{it.portion_size} {it.portion_unit}".strip(),
                    "description": it.description[:200],
                    "allergens": list(it.allergens), "diet_tags": list(it.diet_tags),
                    # SAFETY: blank allergens are UNKNOWN, never "safe" (SDD risk R8)
                    "allergens_known": bool(it.allergens),
                })
        except Exception as exc:                       # noqa: BLE001
            print(f"  WARN menu {loc}: {exc}")
        try:
            for w in dining.hours(loc, service_date):
                hours_rows.append({
                    "foodpro_id": loc, "name": w.name,
                    "open_time": w.open_time, "close_time": w.close_time,
                    "service_date": service_date.isoformat(),
                })
        except Exception as exc:                       # noqa: BLE001
            print(f"  WARN hours {loc}: {exc}")

    empty_allergen = sum(1 for r in eat_options if not r["allergens"])
    return {
        "_meta": {
            "generated_at": config.now().isoformat(timespec="seconds"),
            "clock_mode": config.DEMO_MODE,
            "service_date": service_date.isoformat(),
            "source": "scripts/export_gold.py",
            "caveats": [
                "blank allergen field means UNKNOWN, not allergen-free",
                "live bus positions are a replayed snapshot; sched_delta_min is "
                "computed against the snapshot clock",
            ],
        },
        "stops": stops,
        "next_departures": departures,
        "live_buses": live,
        "eat_options": eat_options,
        "dining_hours": hours_rows,
        "_stats": {
            "stops": len(stops),
            "next_departures": len(departures),
            "live_buses": len(live),
            "eat_options": len(eat_options),
            "eating_with_blank_allergens": empty_allergen,
            "dining_hours": len(hours_rows),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the live cache instead of fixtures")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bundle = build_bundle()
    BUNDLE.write_text(json.dumps(bundle, indent=1), encoding="utf-8")

    print(f"mode={config.DEMO_MODE}  clock={config.now().isoformat(timespec='seconds')}")
    for k, v in bundle["_stats"].items():
        print(f"  {k:32} {v}")
    print(f"\nwrote {BUNDLE}  ({BUNDLE.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())