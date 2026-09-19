#!/usr/bin/env python3
"""Minimal Databricks REST client — stdlib only, no CLI, no SDK, no `requests`.

This machine has no `databricks` CLI and no `databricks-sdk`, so this exists to
talk to the workspace over plain HTTPS. It is the tool the integrator uses to
automate the platform side of Tier 1.

CREDENTIALS (never printed, never committed)
    Reads, in order:
      1. env: DATABRICKS_HOST + DATABRICKS_TOKEN
      2. ~/.databrickscfg  (standard file; must be chmod 600)

Usage
    python3 scripts/dbapi.py whoami
    python3 scripts/dbapi.py warehouses
    python3 scripts/dbapi.py catalogs
    python3 scripts/dbapi.py schemas hokieday
    python3 scripts/dbapi.py sql "SELECT 1 AS ok"          [--warehouse <id>]
    python3 scripts/dbapi.py upload local.json /Volumes/hokieday/gold/landing/gold_bundle.json
    python3 scripts/dbapi.py probe          # the T1.0 capability gate, automated

Security notes
    * The token is never echoed. `whoami` prints only your user name.
    * `upload` streams the file; nothing is logged but the path and size.
    * Revoke the token in Settings > Developer > Access tokens when finished.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CFG = Path.home() / ".databrickscfg"
TIMEOUT = 60


# ------------------------------------------------------------------ credentials
def load_credentials() -> tuple[str, str]:
    host = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
    token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    if host and token:
        return host, token

    if not CFG.exists():
        die(
            f"No credentials found.\n"
            f"  Set DATABRICKS_HOST + DATABRICKS_TOKEN, or create {CFG}:\n\n"
            f"    [DEFAULT]\n"
            f"    host  = https://<your-workspace>.cloud.databricks.com\n"
            f"    token = dapi...\n\n"
            f"  Then: chmod 600 {CFG}"
        )

    mode = oct(CFG.stat().st_mode)[-3:]
    if mode != "600":
        print(f"[warn] {CFG} permissions are {mode}; run: chmod 600 {CFG}", file=sys.stderr)

    parser = configparser.ConfigParser()
    parser.read(CFG)
    if "DEFAULT" not in parser:
        die(f"{CFG} has no [DEFAULT] section")
    section = parser["DEFAULT"]
    host = (section.get("host") or "").strip().rstrip("/")
    token = (section.get("token") or "").strip()
    if not host or not token:
        die(f"{CFG} [DEFAULT] needs both `host` and `token`")
    if not host.startswith("http"):
        host = "https://" + host
    return host, token


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(2)


# ------------------------------------------------------------------ HTTP
def request(host: str, token: str, method: str, path: str,
            body: dict | None = None, raw: bytes | None = None,
            content_type: str = "application/json") -> dict:
    url = f"{host}{path}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = r.read()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        if e.code == 401:
            die(f"401 Unauthorized — token invalid/expired for {host}\n{detail}")
        if e.code == 403:
            print(f"403 Forbidden on {path}: {detail}", file=sys.stderr)
            return {"_status": 403, "_error": detail}
        print(f"HTTP {e.code} on {method} {path}: {detail}", file=sys.stderr)
        return {"_status": e.code, "_error": detail}
    except urllib.error.URLError as e:
        die(f"cannot reach {host}: {e.reason}\n"
            f"(Databricks workspaces are normally reachable over HTTPS; "
            f"check the host and any VPN/ACL configuration)")


# ------------------------------------------------------------------ commands
def cmd_whoami(host, token) -> int:
    me = request(host, token, "GET", "/api/2.0/preview/scim/v2/Me")
    if "userName" not in me:
        print(json.dumps(me, indent=2)[:400])
        return 1
    print(f"host      : {host}")
    print(f"user      : {me.get('userName')}")
    print(f"display   : {me.get('displayName')}")
    print(f"active    : {me.get('active')}")
    print("token     : valid (value never printed)")
    return 0


def cmd_warehouses(host, token) -> int:
    data = request(host, token, "GET", "/api/2.0/sql/warehouses")
    whs = data.get("warehouses", [])
    if not whs:
        print("no SQL warehouses found — a serverless warehouse is required to run statements")
        return 1
    for w in whs:
        print(f"{w.get('id')}  {w.get('name'):30} state={w.get('state')} "
              f"size={w.get('cluster_size')} serverless={w.get('enable_serverless_compute')}")
    return 0


def cmd_catalogs(host, token) -> int:
    data = request(host, token, "GET", "/api/2.1/unity-catalog/catalogs")
    names = [c.get("name") for c in data.get("catalogs", [])]
    print("\n".join(names) if names else "(none)")
    return 0


def cmd_schemas(host, token, catalog) -> int:
    data = request(host, token, "GET", f"/api/2.1/unity-catalog/schemas?catalog_name={catalog}")
    names = [s.get("full_name") for s in data.get("schemas", [])]
    print("\n".join(names) if names else "(none)")
    return 0


def run_sql(host, token, statement: str, warehouse_id: str | None = None,
            poll_s: float = 1.0, max_wait_s: float = 180) -> dict:
    if warehouse_id is None:
        whs = request(host, token, "GET", "/api/2.0/sql/warehouses").get("warehouses", [])
        if not whs:
            die("no SQL warehouse available to run statements")
        warehouse_id = whs[0]["id"]

    body = {
        "warehouse_id": warehouse_id,
        "statement": statement,
        "wait_timeout": "30s",
        "on_wait_timeout": "CONTINUE",
        "format": "JSON_ARRAY",
        "disposition": "INLINE",
    }
    res = request(host, token, "POST", "/api/2.0/sql/statements", body)
    sid = res.get("statement_id")
    waited = 0.0
    while sid and res.get("status", {}).get("state") in ("PENDING", "RUNNING") and waited < max_wait_s:
        time.sleep(poll_s)
        waited += poll_s
        res = request(host, token, "GET", f"/api/2.0/sql/statements/{sid}")

    state = res.get("status", {}).get("state")
    if state != "SUCCEEDED":
        err = res.get("status", {}).get("error", {})
        print(f"statement {state}: {err.get('message', res)[:400]}", file=sys.stderr)
    return res


def cmd_sql(host, token, statement, warehouse) -> int:
    res = run_sql(host, token, statement, warehouse)
    state = res.get("status", {}).get("state")
    print(f"state: {state}  ({res.get('status', {}).get('row_count', '?')} rows)")
    result = res.get("result", {})
    cols = [c.get("name") for c in (res.get("manifest", {}).get("schema", {}).get("columns") or [])]
    rows = result.get("data_array") or []
    if cols:
        print(" | ".join(cols))
        print("-" * max(20, len(" | ".join(cols))))
    for r in rows[:50]:
        print(" | ".join("" if v is None else str(v) for v in r))
    return 0 if state == "SUCCEEDED" else 1


def cmd_upload(host, token, local, remote) -> int:
    p = Path(local)
    if not p.exists():
        die(f"{local} does not exist")
    data = p.read_bytes()
    # Unity Catalog Files API over a volume. Overwrite so reruns are idempotent.
    path = remote if remote.startswith("/api/") else f"/api/2.0/fs/files{remote}?overwrite=true"
    request(host, token, "PUT", path, raw=data, content_type="application/octet-stream")
    print(f"uploaded {p.name} ({len(data):,} bytes) -> {remote}")
    return 0


def cmd_probe(host, token) -> int:
    """The T1.0 gate, automated: can this workspace run the things we need?"""
    print("=" * 68)
    print("FREE EDITION CAPABILITY PROBE")
    print("=" * 68)

    results: dict[str, str] = {}

    print("\n[1/5] identity")
    me = request(host, token, "GET", "/api/2.0/preview/scim/v2/Me")
    ok = "userName" in me
    results["auth"] = "PASS" if ok else "FAIL"
    print(f"      {results['auth']}  host={host} user={me.get('userName', me)}")

    print("\n[2/5] serverless SQL warehouse")
    whs = request(host, token, "GET", "/api/2.0/sql/warehouses").get("warehouses", [])
    results["warehouse"] = "PASS" if whs else "FAIL"
    print(f"      {results['warehouse']}  {len(whs)} warehouse(s)")
    if whs:
        wh = whs[0]
        print(f"      using id={wh.get('id')} name={wh.get('name')} "
              f"serverless={wh.get('enable_serverless_compute')}")
        wid = wh["id"]
    else:
        wid = None

    if wid:
        print("\n[3/5] create catalog + schema")
        r = run_sql(host, token, "CREATE CATALOG IF NOT EXISTS hokieday", wid)
        s1 = r.get("status", {}).get("state") == "SUCCEEDED"
        r = run_sql(host, token, "CREATE SCHEMA IF NOT EXISTS hokieday.gold", wid)
        s2 = r.get("status", {}).get("state") == "SUCCEEDED"
        results["catalog"] = "PASS" if (s1 and s2) else "FAIL"
        print(f"      {results['catalog']}")

        print("\n[4/5] CREATE FUNCTION (the UC-function tool gate)")
        ddl = ("CREATE OR REPLACE FUNCTION hokieday.gold.add_numbers(a DOUBLE, b DOUBLE) "
               "RETURNS DOUBLE COMMENT 'Capability probe only.' RETURN a + b")
        r = run_sql(host, token, ddl, wid)
        results["create_function"] = "PASS" if r.get("status", {}).get("state") == "SUCCEEDED" else "FAIL"
        print(f"      {results['create_function']}")

        print("\n[5/5] call the function")
        r = run_sql(host, token, "SELECT hokieday.gold.add_numbers(2, 3) AS ok", wid)
        val = None
        rows = r.get("result", {}).get("data_array") or []
        if rows and rows[0]:
            val = rows[0][0]
        results["call_function"] = "PASS" if str(val) == "5.0" else "FAIL"
        print(f"      {results['call_function']}  returned {val!r}")

    print("\n" + "=" * 68)
    for k, v in results.items():
        print(f"  {k:18} {v}")
    print("=" * 68)
    if results.get("create_function") == "PASS" and results.get("call_function") == "PASS":
        print("VERDICT: UC functions work -> tools can ship as UC functions.")
    else:
        print("VERDICT: UC functions NOT usable -> run tools in-process in the App "
              "(documented fallback, not a redesign).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Minimal Databricks REST client (stdlib only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    sub.add_parser("warehouses")
    sub.add_parser("catalogs")
    p = sub.add_parser("schemas"); p.add_argument("catalog")
    p = sub.add_parser("sql"); p.add_argument("statement"); p.add_argument("--warehouse")
    p = sub.add_parser("upload"); p.add_argument("local"); p.add_argument("remote")
    sub.add_parser("probe")

    args = ap.parse_args()
    host, token = load_credentials()

    if args.cmd == "whoami":
        return cmd_whoami(host, token)
    if args.cmd == "warehouses":
        return cmd_warehouses(host, token)
    if args.cmd == "catalogs":
        return cmd_catalogs(host, token)
    if args.cmd == "schemas":
        return cmd_schemas(host, token, args.catalog)
    if args.cmd == "sql":
        return cmd_sql(host, token, args.statement, args.warehouse)
    if args.cmd == "upload":
        return cmd_upload(host, token, args.local, args.remote)
    if args.cmd == "probe":
        return cmd_probe(host, token)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())