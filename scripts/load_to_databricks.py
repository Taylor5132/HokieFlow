#!/usr/bin/env python3
"""Load the exported gold bundle into Unity Catalog as Delta tables.

This is the edge-ingestion handoff: the laptop fetched the data (Free Edition
restricts outbound internet and serverless UDFs can never reach the internet),
and the platform now owns storage, governance and serving.

    python3 scripts/export_gold.py           # writes gold/tables/*.jsonl
    python3 scripts/load_to_databricks.py    # creates the Delta tables

Steps, all verified by row counts rather than assumed:
  1. CREATE CATALOG / SCHEMA / VOLUME
  2. upload each gold/tables/<name>.jsonl to the volume (Files API)
  3. CREATE OR REPLACE TABLE ... AS SELECT * FROM read_files(...)
  4. compare every table's row count against the bundle's own _stats

Row counts are the point: a stale upload is the most likely way to lose an hour,
and it is invisible without this check.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from dbapi import load_credentials, request, run_sql  # noqa: E402

TABLES = ["stops", "next_departures", "live_buses", "eat_options", "dining_hours"]
CATALOG = "hokieday"
SCHEMA = "gold"
VOLUME = "landing"


def first_warehouse(host: str, token: str) -> str:
    whs = request(host, token, "GET", "/api/2.0/sql/warehouses").get("warehouses", [])
    if not whs:
        raise SystemExit("no SQL warehouse available")
    return whs[0]["id"]


def table_comment(spark_sql_name: str, bundle: dict) -> str:
    """Comments are load-bearing: the agent and Genie read them."""
    meta = bundle.get("_meta", {})
    return json.dumps({
        "stops": "BT bus stops (static GTFS).",
        "next_departures": (
            "Upcoming scheduled departures per stop. SERVICE-FILTERED to "
            f"{meta.get('service_date')}: unfiltered lookups return other days' trips."
        ),
        "live_buses": (
            "Live BT vehicle positions replayed from a snapshot. load_pct comes from "
            "percentOfCapacity ONLY ('capacity' is internally inconsistent). "
            "sched_delta_min = minutes late (+) / early (-) vs the static schedule."
        ),
        "eat_options": (
            "Dining menu items with allergens, diet tags and nutrition. SAFETY: a blank "
            "allergens array means UNKNOWN, never allergen-free. Filter on allergens_known."
        ),
        "dining_hours": "Per-location opening windows for the service date.",
    }[spark_sql_name])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=CATALOG)
    ap.add_argument("--schema", default=SCHEMA)
    ap.add_argument("--volume", default=VOLUME)
    args = ap.parse_args()

    host, token = load_credentials()
    wh = first_warehouse(host, token)
    print(f"host     : {host}")
    print(f"warehouse: {wh}")

    tables_dir = REPO / "gold" / "tables"
    bundle_path = REPO / "gold" / "gold_bundle.json"
    if not tables_dir.exists() or not bundle_path.exists():
        raise SystemExit("run `python3 scripts/export_gold.py` first")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    stats = bundle.get("_stats", {})

    def sql(stmt: str, label: str) -> bool:
        res = run_sql(host, token, stmt, wh)
        state = res.get("status", {}).get("state")
        ok = state == "SUCCEEDED"
        print(f"  {'ok ' if ok else 'FAIL'} {label}"
              + ("" if ok else f"  <- {res.get('status', {}).get('error', {})}"))
        return ok

    print("\n[1] catalog / schema / volume")
    sql(f"CREATE CATALOG IF NOT EXISTS {args.catalog}", "create catalog")
    sql(f"CREATE SCHEMA IF NOT EXISTS {args.catalog}.{args.schema}", "create schema")
    sql(f"CREATE VOLUME IF NOT EXISTS {args.catalog}.{args.schema}.{args.volume}",
        "create volume")

    vol_path = f"/Volumes/{args.catalog}/{args.schema}/{args.volume}"

    print("\n[2] upload JSONL")
    for name in TABLES:
        local = tables_dir / f"{name}.jsonl"
        if not local.exists():
            print(f"  skip {name} (no local file)")
            continue
        data = local.read_bytes()
        request(host, token, "PUT",
                f"/api/2.0/fs/files{vol_path}/{name}.jsonl?overwrite=true",
                raw=data, content_type="application/octet-stream")
        print(f"  up   {name:18} {len(data):>9,} bytes")

    print("\n[3] create Delta tables")
    for name in TABLES:
        ok = sql(
            f"CREATE OR REPLACE TABLE {args.catalog}.{args.schema}.{name} AS "
            f"SELECT * FROM read_files('{vol_path}/{name}.jsonl', format => 'json')",
            f"create {name}")
        if ok:
            sql(f"COMMENT ON TABLE {args.catalog}.{args.schema}.{name} IS "
                f"{json.dumps(table_comment(name, bundle))}", f"comment {name}")

    print("\n[4] verify row counts against the exporter's own _stats")
    # _stats keys differ slightly from table names; map them explicitly.
    expected = {
        "stops": stats.get("stops"),
        "next_departures": stats.get("next_departures"),
        "live_buses": stats.get("live_buses"),
        "eat_options": stats.get("eat_options"),
        "dining_hours": stats.get("dining_hours"),
    }
    mismatches = []
    for name in TABLES:
        res = run_sql(host, token,
                      f"SELECT COUNT(*) AS n FROM {args.catalog}.{args.schema}.{name}", wh)
        rows = res.get("result", {}).get("data_array") or []
        got = int(rows[0][0]) if rows and rows[0] else -1
        want = expected.get(name)
        flag = "ok " if (want is None or got == want) else "MISMATCH"
        if flag != "ok ":
            mismatches.append((name, want, got))
        print(f"  {flag:8} {name:18} got={got:<7} exported={want}")

    print("\n[5] the checks that encode bugs we already hit")
    checks = {
        "every live bus joined to the schedule": f"""
            SELECT COUNT(*) AS buses,
                   SUM(CASE WHEN sched_delta_min IS NULL THEN 1 ELSE 0 END) AS unmatched
            FROM {args.catalog}.{args.schema}.live_buses""",
        "blank allergens visible as UNKNOWN": f"""
            SELECT COUNT(*) AS items,
                   SUM(CASE WHEN allergens_known THEN 1 ELSE 0 END) AS stated,
                   SUM(CASE WHEN NOT allergens_known THEN 1 ELSE 0 END) AS unknown
            FROM {args.catalog}.{args.schema}.eat_options""",
        "service filter removed other days' trips": f"""
            SELECT route_id, MIN(dep_time) AS first_dep
            FROM {args.catalog}.{args.schema}.next_departures
            WHERE stop_id = '1600' GROUP BY route_id ORDER BY first_dep""",
    }
    for label, stmt in checks.items():
        res = run_sql(host, token, stmt, wh)
        cols = [c.get("name") for c in
                (res.get("manifest", {}).get("schema", {}).get("columns") or [])]
        rows = res.get("result", {}).get("data_array") or []
        print(f"\n  {label}")
        print(f"    {' | '.join(cols)}")
        for r in rows[:6]:
            print(f"    {' | '.join('' if v is None else str(v) for v in r)}")

    if mismatches:
        print(f"\nVERDICT: FAIL - {len(mismatches)} table(s) disagree with the export: "
              f"{mismatches}\n(the upload is stale; re-run the exporter)")
        return 1
    print("\nVERDICT: PASS - all tables loaded and counts match the export")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())