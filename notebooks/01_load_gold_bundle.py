# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Load the gold bundle into Unity Catalog Delta tables
# MAGIC
# MAGIC **Why this notebook exists.** Databricks Free Edition restricts outbound internet to a
# MAGIC limited, unpublished set of trusted domains, and serverless UDFs can never reach the
# MAGIC internet. So ingestion happens at the edge (`scripts/export_gold.py` on the laptop) and
# MAGIC the platform owns **storage, governance, ML, and serving**.
# MAGIC
# MAGIC This notebook takes the uploaded bundle and materialises it as Delta tables in
# MAGIC `hokieday.gold`, which is what the tools/agent read.
# MAGIC
# MAGIC **Serverless notes (Free Edition):**
# MAGIC * Only Spark Connect APIs are supported — all code below is DataFrame/SQL only.
# MAGIC * `df.cache()` / `persist()` / `CACHE TABLE` **throw** on serverless. Do not use them.
# MAGIC * Keep runs short: overrunning the quota shuts compute down for the rest of the day.
# MAGIC
# MAGIC **Upload the bundle first** to a Unity Catalog Volume, e.g.
# MAGIC `/Volumes/hokieday/gold/landing/gold_bundle.json` (Catalog Explorer → create volume →
# MAGIC upload). Regenerate it any time with `python3 scripts/export_gold.py`.

# COMMAND ----------

dbutils.widgets.text("catalog", "hokieday", "Catalog")
dbutils.widgets.text("schema", "gold", "Schema")
dbutils.widgets.text(
    "bundle_path",
    "/Volumes/hokieday/gold/landing/gold_bundle.json",
    "Path to gold_bundle.json",
)

CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
BUNDLE_PATH = dbutils.widgets.get("bundle_path")

print(f"target  : {CATALOG}.{SCHEMA}")
print(f"bundle  : {BUNDLE_PATH}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read the bundle
# MAGIC
# MAGIC A single JSON document. `spark.read.json` on a file containing one object reads it as
# MAGIC a single row; we then pull the arrays out by name.

# COMMAND ----------

raw = spark.read.text(BUNDLE_PATH).collect()
text = "\n".join(r["value"] for r in raw)

import json  # noqa: E402  (notebook-scoped; safe on serverless)

bundle = json.loads(text)
meta = bundle.get("_meta", {})
print("bundle meta:", json.dumps(meta, indent=2)[:800])
print("\nsections:", [k for k in bundle if not k.startswith("_")])
print("stats   :", json.dumps(bundle.get("_stats", {}), indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create the catalog / schema
# MAGIC
# MAGIC Free Edition provides one metastore, so a plain `CREATE` is enough. Managed tables keep
# MAGIC this simple — no external locations or storage credentials required.

# COMMAND ----------

spark.sql(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Materialise each section as a Delta table
# MAGIC
# MAGIC `spark.createDataFrame` from local data is allowed on serverless (rows must stay under
# MAGIC 128 MB each — this bundle is a couple of hundred KB). Every table is written as Delta so
# MAGIC Unity Catalog records lineage and so Genie can query it.

# COMMAND ----------

TABLES = [
    "stops",
    "next_departures",
    "live_buses",
    "dining_locations",
    "dining_status",
    "food_basic",
    "eat_options",
    "dining_hours",
]

for name in TABLES:
    rows = bundle.get(name) or []
    if not rows:
        print(f"{name:16} skipped (0 rows)")
        continue
    df = spark.createDataFrame(rows)
    target = f"{CATALOG}.{SCHEMA}.{name}"
    (df.write
       .mode("overwrite")
       .option("overwriteSchema", "true")
       .saveAsTable(target))
    print(f"{name:16} wrote {df.count():>6} rows -> {target}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Table comments
# MAGIC
# MAGIC These are not decoration: Unity Catalog comments are how the **agent** and **Genie** learn
# MAGIC what a table means and how to use it. A vague comment produces a vague agent.

# COMMAND ----------

COMMENTS = {
    "stops": "BT bus stops with coordinates (from the static GTFS feed).",
    "next_departures": (
        "Upcoming scheduled departures per stop. SERVICE-FILTERED: only trips whose "
        "service is active on _meta.service_date. Unfiltered lookups return other days' trips."
    ),
    "live_buses": (
        "Live BT vehicle positions replayed from a snapshot. load_pct comes from "
        "percentOfCapacity ONLY (the 'capacity' field is internally inconsistent). "
        "sched_delta_min = minutes late (+) or early (-) vs the static schedule."
    ),
    "eat_options": (
        "Dining menu items with allergens and diet tags. SAFETY: a blank allergens array "
        "means UNKNOWN, NOT allergen-free (188 of 470 D2 rows). Filter on allergens_known."
    ),
    "dining_locations": (
        "Deterministic directory of all 12 official FoodPro locations, sorted by "
        "location_num; the picker source of truth even when Locations.aspx is down."
    ),
    "dining_status": (
        "Per-location resolution: composite status (ok/closed/empty/unavailable) with "
        "INDEPENDENT menu_status and hours_status plus provenance and stale."
    ),
    "food_basic": (
        "BASIC multi-location food rows (no nutrition). SAFETY: blank allergens means "
        "UNKNOWN; venue_allergen_free is bound to the D2 Viridian kitchen only."
    ),
    "dining_hours": "Per-location opening windows for the service date.",
}

for name, comment in COMMENTS.items():
    try:
        spark.sql(
            f"COMMENT ON TABLE {CATALOG}.{SCHEMA}.{name} IS {json.dumps(comment)}"
        )
        print(f"commented {name}")
    except Exception as exc:                        # table may have been skipped
        print(f"comment failed for {name}: {exc}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Verify
# MAGIC
# MAGIC These counts must match the exporter's `_stats`. If they do not, the upload is stale —
# MAGIC which is the single most likely way to waste an hour today.

# COMMAND ----------

STATS_KEYS = {
    "stops": "stops",
    "next_departures": "next_departures",
    "live_buses": "live_buses",
    "dining_locations": "dining_locations",
    "dining_status": "dining_status",
    "food_basic": "food_basic",
    "eat_options": "eat_options",
    "dining_hours": "dining_hours",
}

mismatches = []
for name in TABLES:
    try:
        n = spark.table(f"{CATALOG}.{SCHEMA}.{name}").count()
        want = bundle.get("_stats", {}).get(STATS_KEYS.get(name, name))
        flag = "ok" if (want is None or n == want) else "MISMATCH"
        if flag == "MISMATCH":
            mismatches.append((name, want, n))
        print(f"{flag:8} {name:16} got={n:<7} exported={want}")
    except Exception as exc:
        print(f"{name:16} MISSING ({exc})")
        mismatches.append((name, bundle.get("_stats", {}).get(name), None))

assert not mismatches, f"stale upload: {mismatches}; re-run scripts/export_gold.py"

# COMMAND ----------

# MAGIC %md
# MAGIC ### Sanity checks that encode real bugs we have already hit

# COMMAND ----------

# The keystone join must be complete: every live vehicle matched to the schedule.
spark.sql(f"""
SELECT
  COUNT(*)                                             AS buses,
  SUM(CASE WHEN sched_delta_min IS NULL THEN 1 ELSE 0 END) AS unmatched,
  ROUND(AVG(sched_delta_min), 2)                       AS mean_delta_min
FROM {CATALOG}.{SCHEMA}.live_buses
""").show()

# Blank allergens must be visible as unknown, never silently treated as safe.
spark.sql(f"""
SELECT
  COUNT(*)                                              AS items,
  SUM(CASE WHEN allergens_known THEN 1 ELSE 0 END)       AS allergen_stated,
  SUM(CASE WHEN NOT allergens_known THEN 1 ELSE 0 END)   AS allergen_UNKNOWN,
  SUM(CASE WHEN array_contains(allergens, 'Peanuts') THEN 1 ELSE 0 END) AS contains_peanuts
FROM {CATALOG}.{SCHEMA}.eat_options
""").show()

# The service filter must have removed other days' trips.
spark.sql(f"""
SELECT route_id, MIN(dep_time) AS first_dep
FROM {CATALOG}.{SCHEMA}.next_departures
WHERE stop_id = '1600'
GROUP BY route_id ORDER BY first_dep
""").show(truncate=False)

# Multi-location basic rows must carry provenance and never lose the directory.
spark.sql(f"""
SELECT
  COUNT(*)                                            AS locations,
  COUNT(DISTINCT location_num)                        AS distinct_nums
FROM {CATALOG}.{SCHEMA}.dining_locations
""").show()

spark.sql(f"""
SELECT status, COUNT(*) AS n
FROM {CATALOG}.{SCHEMA}.dining_status
GROUP BY status ORDER BY status
""").show()

spark.sql(f"""
SELECT
  COUNT(*)                                                  AS basic_rows,
  SUM(CASE WHEN fetched_at IS NULL THEN 1 ELSE 0 END)       AS missing_fetched,
  SUM(CASE WHEN venue_allergen_free THEN 1 ELSE 0 END)      AS venue_allergen_free
FROM {CATALOG}.{SCHEMA}.food_basic
""").show()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Next steps
# MAGIC
# MAGIC 1. `02_governance.py` — column masks and row filters (the FERPA slide).
# MAGIC 2. Wire the tools to read these tables (`hokieday/tools.py` exposes a `Source`
# MAGIC    protocol; a `TableSource` reads Delta instead of the local modules).
# MAGIC 3. Create the Genie space over `next_departures`, `live_buses`, `eat_options`,
# MAGIC    `dining_locations`, `dining_status`, `food_basic`.
# MAGIC
# MAGIC **Reminder to say out loud in the demo:** no academic record — no transcript, no grades,
# MAGIC no GPA — ever enters this lakehouse, and Unity Catalog enforces that rather than relying
# MAGIC on a policy promise.