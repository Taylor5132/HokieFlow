# Free Edition capability probe

Run this **in a Databricks notebook** (Free Edition) before writing any more
platform code. It answers the four questions that decide the architecture.

Paste each cell in order. Total runtime: about a minute.

---

## Cell 1 — can the workspace reach our sources?

Free Edition restricts outbound internet to a limited, **unpublished** set of
trusted domains, and Databricks docs say **serverless UDFs can never reach the
internet**. This decides whether ingestion can happen *in* Databricks or must
happen at the edge.

```python
import urllib.request

def probe(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return f"OK    {r.status} {len(r.read()):>9,}b"
    except Exception as e:
        return f"BLOCK {type(e).__name__}: {str(e)[:70]}"

SOURCES = {
    "NWS weather":   "https://api.weather.gov/points/37.2296,-80.4139",
    "BT live buses": "https://ridebt.org/index.php?option=com_ajax&module=bt_map&method=getBuses&format=json&Itemid=101",
    "BT GTFS zip":   "http://www.bt4uclassic.org/gtfs/google_transit.zip",
    "VT dining":     "https://foodpro.students.vt.edu/menus/API/Locations.aspx",
    "VT hours":      "https://apps.students.vt.edu/hours/Api/NonRestricted/FoodProCentersOpen/readByFoodPro/15/2026-09-19",
}

for name, url in SOURCES.items():
    print(f"{name:15} {probe(url)}")

# what does the environment think it is?
import os
print("\nDATABRICKS_RUNTIME_VERSION:", os.environ.get("DATABRICKS_RUNTIME_VERSION"))
```

**Interpretation**

| Result | Meaning |
|---|---|
| all `OK` | Ingestion can run in-platform. Original design stands. |
| `BLOCK` on some/all | Expected on Free Edition without LinkedIn verification. Use **edge ingestion**: run `scripts/export_gold.py` locally and upload. |

If blocked, do the **LinkedIn verification** (Free Edition docs say it unlocks
outbound internet) and re-run this cell. Even if it then succeeds, prefer edge
ingestion for the demo — it cannot be broken by a quota or network hiccup.

---

## Cell 2 — is there serverless *generic* compute?

Unity Catalog functions used as **agent tools** require serverless *generic*
compute (Spark Connect), **not** a serverless SQL warehouse. Free Edition is
serverless-only, and its limits page says "Only Spark Connect APIs are
supported", which suggests yes — confirm it.

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
me = w.current_user.me()
print("user:", me.user_name)
print("default catalog:", w.current_user.me().user_name and "see Catalogs below")
```

Then just run any Spark cell — if this works, generic serverless is present:

```python
spark.sql("SELECT 1 AS ok").show()
```

---

## Cell 3 — can we create a Unity Catalog function and call it?

This is the T1.0 gate. If it fails, tools must run **in-process** inside the App
instead (which is a documented fallback, not a redesign).

```python
spark.sql("CREATE CATALOG IF NOT EXISTS hokieday")
spark.sql("CREATE SCHEMA IF NOT EXISTS hokieday.gold")

spark.sql("""
CREATE OR REPLACE FUNCTION hokieday.gold.add_numbers(a DOUBLE, b DOUBLE)
RETURNS DOUBLE
COMMENT 'Adds two numbers. Used only as a capability probe.'
RETURN a + b
""")

spark.sql("SELECT hokieday.gold.add_numbers(2, 3) AS result").show()
```

**Interpretation**

| Result | Meaning |
|---|---|
| returns `5.0` | UC functions work → tools can be UC functions |
| error mentioning Spark Connect / serverless | tools must run in-process in the App |

---

## Cell 4 — Unity Catalog Python functions (the tool implementation path)

Docs require: type hints on every argument and the return, **no `*args`/`**kwargs`**,
**Google-style docstrings** (the toolkit parses them to teach the LLM when to call
the tool), and **imports inside the function body**.

```python
%pip install unitycatalog-ai[databricks]
dbutils.library.restartPython()
```

```python
from unitycatalog.ai.core.databricks import DatabricksFunctionClient
client = DatabricksFunctionClient()

def sum_two(number_1: float, number_2: float) -> float:
    """
    A capability probe: adds two numbers.

    Args:
      number_1 (float): The first number.
      number_2 (float): The second number.

    Returns:
      float: The sum of the two numbers.
    """
    return number_1 + number_2

info = client.create_python_function(
    func=sum_two, catalog="hokieday", schema="gold", replace=True)
print(info)
print(client.execute_function(
    function_name="hokieday.gold.sum_two",
    parameters={"number_1": 36939.0, "number_2": 8922.4}).value)
```

---

## What to report back

1. Which sources returned `OK` vs `BLOCK` (Cell 1).
2. Whether `SELECT 1` worked (Cell 2).
3. Whether Cell 3 returned `5.0`.
4. Whether Cell 4 printed `45861.4`.

Those four answers determine whether tools ship as UC functions, whether the
agent runs in-process in the App, and whether ingestion is in-platform or at the
edge. Everything else in the build is unaffected.