#!/usr/bin/env python3
"""Seed per-item nutrition into the FROZEN store, so calorie filters work offline.

WHY THIS EXISTS
---------------
`find_food(max_kcal=...)` was returning ZERO items: 0 of 470 D2 items carried a
kcal value, because nutrition is fetched from a separate per-item API
(NutritiveReport.aspx) and only a 2-item fixture had ever been seeded. Any
calorie constraint therefore silently deleted the meal -- and "can I eat and
still make class?" is the headline demo question.

Run this ONCE while online:

    cd hokieday
    DEMO_MODE=live HOKIEDAY_CACHE=$(pwd)/fixtures python3 scripts/seed_nutrition.py

That combination is deliberate: DEMO_MODE=live allows network fetches, while
HOKIEDAY_CACHE=fixtures makes the cache layer WRITE into the frozen store, so the
fetched nutrition is available offline afterwards. (With DEMO_MODE=cache the
cache layer refuses to touch the network, so it cannot seed.)
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hokieday import cache, config, dining  # noqa: E402

SERVICE_DATE = date(2026, 9, 19)      # the date the frozen menu fixtures cover
LOCATIONS = ["15"]                    # D2; add more once their menus are seeded
CHUNK = 40


def main() -> int:
    if config.CACHE_ONLY:
        print("refusing: DEMO_MODE=cache cannot fetch. Use:\n"
              "  DEMO_MODE=live HOKIEDAY_CACHE=$(pwd)/fixtures python3 scripts/seed_nutrition.py")
        return 2

    print(f"mode={config.DEMO_MODE}  cache_dir={config.CACHE_DIR}")
    if config.CACHE_DIR != config.FIXTURES_DIR:
        print(f"  WARNING: not writing into the frozen store ({config.FIXTURES_DIR}).\n"
              f"  Set HOKIEDAY_CACHE={config.FIXTURES_DIR} or the demo will not see this data.")

    total_items = total_kcal = 0
    for loc in LOCATIONS:
        items = dining.menu(loc, SERVICE_DATE)
        print(f"\nlocation {loc}: {len(items)} items")
        payload = [(it.recipe_id, it.portion_size or "1", 1) for it in items]

        for start in range(0, len(payload), CHUNK):
            chunk = payload[start:start + CHUNK]
            try:
                got = dining.nutrition_bulk(chunk)
            except Exception as exc:                       # noqa: BLE001
                print(f"  chunk {start // CHUNK + 1}: FAILED {type(exc).__name__}: {exc}")
                continue
            with_kcal = sum(1 for v in got.values()
                            if getattr(v, "cals", None) is not None)
            total_kcal += with_kcal
            total_items += len(chunk)
            print(f"  chunk {start // CHUNK + 1:>2}/{-(-len(payload) // CHUNK):<2} "
                  f"{len(chunk):>3} items -> {with_kcal:>3} with kcal")

    print(f"\nseeded nutrition for {total_kcal}/{total_items} items into {config.CACHE_DIR}")
    print(f"fixtures now: {cache.stats()['count']} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())