#!/usr/bin/env python3
"""Create the agent's tools as governed Unity Catalog functions.

WHY SQL-BODY FUNCTIONS
----------------------
Databricks recommends Unity Catalog functions as agent tools for *structured
data retrieval when the query is known ahead of time and the agent supplies the
parameters* -- which is exactly our case. SQL-body functions can read the gold
tables; serverless Python UDFs cannot reach the network but CAN be slow to start
and cannot open a Spark session, so table reading belongs in SQL.

PARAMETER NAMING (deliberate)
-----------------------------
Parameters are prefixed `p_`. Inside a SQL-body function a parameter named
`stop_id` collides with the column `stop_id` and resolves ambiguously -- a
silent wrong-answers bug. Each parameter carries a COMMENT so the agent still
knows exactly what to pass.

    python3 scripts/create_uc_functions.py            # create + verify
    python3 scripts/create_uc_functions.py --drop     # drop them all first
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from dbapi import load_credentials, request, run_sql  # noqa: E402

CATALOG, SCHEMA = "hokieday", "gold"
F = f"{CATALOG}.{SCHEMA}"          # function/table namespace

# (name, signature, comment, sql body)
FUNCTIONS: list[tuple[str, str, str, str]] = [
    (
        "get_next_departures",
        "p_stop_id STRING COMMENT 'BT stop id, e.g. 1600. See the stops table.', "
        "p_horizon_min INT COMMENT 'How many minutes ahead to look. 180 is a good default.', "
        "p_route_id STRING COMMENT 'Optional route filter such as SME. NULL means all routes.'",
        "Returns the upcoming SCHEDULED departures at a BT stop as a JSON array, "
        "soonest first, each with route_id, head_sign, dep_time, in_min and "
        "is_realtime. Results are service-filtered to the loaded service date, so "
        "they do NOT include other days' trips. Empty array means no service in "
        "the requested horizon.",
        f"""
        SELECT COALESCE(to_json(collect_list(s)), '[]') FROM (
          SELECT named_struct(
                   'route_id', route_id,
                   'head_sign', head_sign,
                   'dep_time', CAST(dep_time AS STRING),
                   'in_min', in_min,
                   'is_realtime', false) AS s
          FROM {F}.next_departures
          WHERE stop_id = p_stop_id
            AND in_min >= 0
            AND in_min <= COALESCE(p_horizon_min, 180)
            AND (p_route_id IS NULL OR route_id = p_route_id)
          ORDER BY in_min
          LIMIT 10)""",
    ),
    (
        "get_live_bus",
        "p_route_id STRING COMMENT 'Optional route such as SME. NULL returns every live vehicle.'",
        "Returns LIVE BT vehicle positions as a JSON array. load_pct is crowding "
        "(from percentOfCapacity only). sched_delta_min is minutes LATE (+) or "
        "EARLY (-) versus the static schedule; a null means the vehicle could not "
        "be joined to a scheduled trip. is_stale means the position is old and "
        "should be treated as schedule-only.",
        f"""
        SELECT COALESCE(to_json(collect_list(s)), '[]') FROM (
          SELECT named_struct(
                   'bus_id', bus_id,
                   'route_id', route_id,
                   'stop_id', stop_id,
                   'load_pct', load_pct,
                   'is_at_stop', at_stop,
                   'sched_delta_min', sched_delta_min,
                   'is_stale', is_stale) AS s
          FROM {F}.live_buses
          WHERE p_route_id IS NULL OR route_id = p_route_id)""",
    ),
    (
        "find_food",
        "p_location_num STRING COMMENT 'FoodPro location, e.g. 15 for D2 at Dietrick Hall. NULL searches all.', "
        "p_diet STRING COMMENT 'Diet tag to require, e.g. vegetarian or vegan. NULL means any.', "
        "p_avoid STRING COMMENT 'Comma-separated allergens to EXCLUDE, e.g. ''Peanuts,Tree Nuts''.', "
        "p_max_kcal DOUBLE COMMENT 'Maximum calories per item. Items with unknown calories are excluded.', "
        "p_include_unknown BOOLEAN DEFAULT false COMMENT 'Set true to ALSO return items whose allergens are unknown. They are unsafe by default and must be labelled unverified.'",
        "Returns dining menu items matching diet, allergen and caloried limits as "
        "a JSON array, each with name, kcal, protein_g, allergens, diet_tags and "
        "allergens_known and venue_allergen_free. SAFETY: `avoid` is a HARD "
        "filter with a THREE-WAY policy -- (1) an item stating an avoided "
        "allergen is excluded; (2) an item with a BLANK allergen field is "
        "excluded as UNKNOWN unless venue_allergen_free is true; (3) a blank "
        "field in a documented allergen-free kitchen (Viridian, which VT states "
        "is free from the top nine allergens with separate preparation space) "
        "is treated as safe and IS returned. Never describe an item with "
        "allergens_known=false as safe.",
        f"""
        SELECT COALESCE(to_json(collect_list(s)), '[]') FROM (
          SELECT named_struct(
                   'name', name,
                   'location_num', location_num,
                   'meal', meal,
                   'section', section,
                   'kcal', kcal,
                   'protein_g', protein_g,
                   'allergens', allergens,
                   'diet_tags', diet_tags,
                   'allergens_known', allergens_known,
                   'venue_allergen_free', venue_allergen_free) AS s
          FROM {F}.eat_options e
          WHERE (p_location_num IS NULL OR p_location_num = ''
                 OR e.location_num = p_location_num)
            AND (p_diet IS NULL OR p_diet = ''
                 OR array_contains(e.diet_tags, lower(trim(p_diet))))
            AND (p_avoid IS NULL OR p_avoid = ''
                 OR (
                   size(array_intersect(
                        transform(e.allergens, x -> lower(trim(x))),
                        transform(split(p_avoid, ','), x -> lower(trim(x)))
                   )) = 0
                   AND (size(e.allergens) > 0
                        OR e.venue_allergen_free
                        OR COALESCE(p_include_unknown, false))
                 ))
            AND (p_max_kcal IS NULL
                 OR (e.kcal IS NOT NULL AND e.kcal <= p_max_kcal))
          ORDER BY e.kcal ASC NULLS LAST
          LIMIT 40)""",
    ),
    (
        "get_hours",
        "p_foodpro_id STRING COMMENT 'FoodPro location id, e.g. 15 for D2 at Dietrick Hall.'",
        "Returns the opening windows for a dining location on the loaded service "
        "date as a JSON array of {foodpro_id, name, open_time, close_time}. Use "
        "close_time to decide whether a student can finish eating in time.",
        f"""
        SELECT COALESCE(to_json(collect_list(s)), '[]') FROM (
          SELECT named_struct(
                   'foodpro_id', foodpro_id,
                   'name', name,
                   'open_time', open_time,
                   'close_time', close_time) AS s
          FROM {F}.dining_hours
          WHERE p_foodpro_id IS NULL OR foodpro_id = p_foodpro_id)""",
    ),
]

# Verification: (function, args sql, a substring the result MUST contain, a substring it must NOT)
CHECKS: list[tuple[str, str, str | None, str | None]] = [
    ("get_next_departures", "'1600', 180, NULL", "11:48:29", "11:23:29"),
    ("get_live_bus", "NULL", "sched_delta_min", None),
    ("get_live_bus", "'SME'", "SME", None),
    ("find_food", "'15', 'vegetarian', 'Peanuts,Tree Nuts', 800, false", "allergens_known", "Peanuts"),
    # The three-way safety policy, checked in BOTH directions:
    #  - a documented allergen-free kitchen must SURVIVE an avoid filter
    #    (all 48 Viridian items have a blank allergen field)
    #  - and no avoided allergen may appear in the output
    ("find_food", "'15', NULL, 'Peanuts,Tree Nuts', NULL, false", "Viridian", "Peanuts"),
    ("get_hours", "'15'", "15:00:00", None),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true", help="drop the functions first")
    args = ap.parse_args()

    host, token = load_credentials()
    whs = request(host, token, "GET", "/api/2.0/sql/warehouses").get("warehouses", [])
    if not whs:
        raise SystemExit("no SQL warehouse")
    wh = whs[0]["id"]
    print(f"host {host}\nwarehouse {wh}\n")

    def sql(stmt: str, label: str, quiet: bool = False) -> tuple[bool, dict]:
        res = run_sql(host, token, stmt, wh)
        ok = res.get("status", {}).get("state") == "SUCCEEDED"
        if not quiet or not ok:
            msg = ""
            if not ok:
                msg = "  <- " + str(res.get("status", {}).get("error", {}).get("message", ""))[:200]
            print(f"  {'ok  ' if ok else 'FAIL'} {label}{msg}")
        return ok, res

    if args.drop:
        print("[0] drop existing")
        for name, *_ in FUNCTIONS:
            sql(f"DROP FUNCTION IF EXISTS {F}.{name}", f"drop {name}")

    print("[1] create functions")
    failures = 0
    for name, sig, comment, body in FUNCTIONS:
        ddl = (f"CREATE OR REPLACE FUNCTION {F}.{name}({sig})\n"
               f"RETURNS STRING\nCOMMENT {json.dumps(comment)}\n"
               f"RETURN {body}")
        ok, res = sql(ddl, f"create {name}")
        if not ok:
            failures += 1

    if failures:
        print(f"\nVERDICT: FAIL - {failures} function(s) failed to create")
        return 1

    print("\n[2] verify by calling each function")
    problems = 0
    for fn, args_sql, must, must_not in CHECKS:
        ok, res = sql(f"SELECT {F}.{fn}({args_sql}) AS r", f"{fn}({args_sql})")
        rows = res.get("result", {}).get("data_array") or []
        val = rows[0][0] if rows and rows[0] else None
        if val is None:
            problems += 1
            continue
        n = 0
        try:
            n = len(json.loads(val))
        except Exception:                                    # noqa: BLE001
            pass
        note = ""
        if must and must in val:
            note = f"  contains {must!r} OK"
        elif must:
            note = f"  MISSING {must!r}"
            problems += 1
        if must_not and must_not in val:
            note += f"  LEAKED {must_not!r}"
            problems += 1
        print(f"      -> {n} rows{note}")

    print("\n[3] the safety property, checked directly in SQL")
    sql(f"""SELECT COUNT(*) AS items_with_peanuts
            FROM {F}.eat_options
            WHERE array_contains(allergens, 'Peanuts')""", "peanut items in table")
    sql(f"""SELECT COUNT(*) AS leaked FROM (
              SELECT * FROM {F}.eat_options e
              WHERE array_contains(e.diet_tags, 'vegetarian')
                AND size(array_intersect(
                      transform(e.allergens, x -> lower(trim(x))),
                      array('peanuts', 'tree nuts'))) > 0
                AND e.kcal <= 800)""", "would leak through the filter")

    print("\nVERDICT: " + ("PASS - UC functions created and verified"
                           if problems == 0 else f"FAIL - {problems} check(s) failed"))
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())