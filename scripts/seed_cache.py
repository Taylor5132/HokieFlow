#!/usr/bin/env python3
"""Seed the cache from the real payloads captured during the data spike.

This is what makes DEMO_MODE=cache possible and what the worker test suites run
against. Every fixture here is a REAL payload from 2026-09-19.

    python3 scripts/seed_cache.py
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hokieday import cache, config  # noqa: E402

SPIKE = Path("/Volumes/mySSD/workspace/vthacks/spike")
RAW = SPIKE / "raw"
GTFS_ZIP = SPIKE / "gtfs" / "bt_gtfs.zip"
GTFS_EXTRACTED = SPIKE / "gtfs" / "extracted"

STAMP = "2026-09-19T15:22:29+00:00"          # when the spike actually ran

# (cache name, params, source file, url used)
JSON_FIXTURES = [
    ("bt_buses", {}, RAW / "live_buses.json",
     config.ENDPOINTS["bt_buses"]),
    ("dining_locations", {}, RAW / "vt_locations.json",
     config.ENDPOINTS["dining_locations"]),
    ("dining_menu", {"location_num": "15", "dtdate": "09/19/2026"},
     RAW / "menu_d2_today.json",
     config.ENDPOINTS["dining_menu"].format(location_num="15", dtdate="09/19/2026")),
    ("dining_menu", {"location_num": "15", "dtdate": "09/17/2026"},
     RAW / "menu_d2_thu.json",
     config.ENDPOINTS["dining_menu"].format(location_num="15", dtdate="09/17/2026")),
    ("dining_hours", {"foodpro_id": "15", "date": "2026-09-19"},
     RAW / "hours_d2.json",
     config.ENDPOINTS["dining_hours"].format(foodpro_id="15", date="2026-09-19")),
    ("dining_allergens", {"location_num": "15"}, RAW / "allergens.json",
     config.ENDPOINTS["dining_allergens"].format(location_num="15")),
    ("dining_nutrition", {"items": "214022*1*1,141002*2*1"},
     RAW / "nutrition_test.json",
     config.ENDPOINTS["dining_nutrition"].format(items="214022*1*1,141002*2*1")),
]


def main() -> int:
    written = 0

    for name, params, src, url in JSON_FIXTURES:
        if not src.exists():
            print(f"  SKIP  {src.name} (missing)")
            continue
        payload = json.loads(src.read_text(encoding="utf-8"))
        path = cache._json_path(name, params)
        cache._write_envelope(path, url, payload)
        # keep the original capture timestamp so staleness logic behaves
        env = json.loads(path.read_text(encoding="utf-8"))
        env["fetched_at"] = STAMP
        env["mode"] = "spike"
        path.write_text(json.dumps(env), encoding="utf-8")
        print(f"  OK    {path.name}")
        written += 1

    # GTFS zip (binary)
    if GTFS_ZIP.exists():
        dest = cache._bin_path("bt_gtfs", {})
        shutil.copyfile(GTFS_ZIP, dest)
        cache._write_envelope(
            cache._json_path("bt_gtfs", {}), config.ENDPOINTS["bt_gtfs"],
            {"bytes": dest.stat().st_size, "file": dest.name},
        )
        print(f"  OK    {dest.name} ({dest.stat().st_size:,} bytes)")
        written += 1

    # extracted GTFS tree (so tests do not need to unzip)
    if GTFS_EXTRACTED.exists():
        dest = config.GTFS_DIR
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(GTFS_EXTRACTED, dest)
        print(f"  OK    {dest}/ ({len(list(dest.iterdir()))} files)")
        written += 1

    print(f"\nseeded {written} fixtures into {config.CACHE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())