#!/usr/bin/env python3
"""OPT-IN, READ-ONLY smoke test for the VT GIS client (hokieday.vtgis).

This is not part of any test suite and never runs by itself. It must be invoked
explicitly:

    python3 scripts/vtgis_smoke.py --live
    # or
    VTGIS_LIVE=1 python3 scripts/vtgis_smoke.py

GUARANTEES
  * Anonymous requests only; no credentials, no API key.
  * READ-ONLY against VT: it only issues ArcGIS query/solve reads.
  * It NEVER calls the permission-gated export/snapshot helpers or writes under
    the repository. Runtime cache files live in an OS temporary directory and
    are deleted before exit.
  * It prints a short human summary; it does not persist or redistribute a GIS
    response.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokieday import config, vtgis  # noqa: E402


def _enabled(args) -> bool:
    return bool(args.live) or os.environ.get("VTGIS_LIVE") == "1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="explicitly allow the anonymous live reads")
    ap.add_argument("--building", default="Burruss",
                    help="building name/number to search (default: Burruss)")
    args = ap.parse_args(argv)

    if not _enabled(args):
        print("VT GIS smoke test is opt-in and read-only.\n"
              "Re-run with:  python3 scripts/vtgis_smoke.py --live\n"
              "or set VTGIS_LIVE=1. Nothing was fetched.")
        return 2

    # Even successful GET/POST calls use the common cache layer. Point that
    # operational cache outside the repository and remove it on exit so this
    # smoke command is genuinely read-only with respect to the worktree.
    with tempfile.TemporaryDirectory(prefix="hokieday-vtgis-smoke-") as tmp:
        original_cache_dir = config.CACHE_DIR
        original_cache_only = config.CACHE_ONLY
        original_demo_mode = config.DEMO_MODE
        config.CACHE_DIR = Path(tmp)
        config.CACHE_ONLY = False
        config.DEMO_MODE = "live"
        try:
            return _run_live(args)
        finally:
            config.CACHE_DIR = original_cache_dir
            config.CACHE_ONLY = original_cache_only
            config.DEMO_MODE = original_demo_mode


def _run_live(args) -> int:
    print(f"[vtgis] attribution: {vtgis.ATTRIBUTION}")
    print(f"[vtgis] disclaimer : {vtgis.DISCLAIMER}")

    found = vtgis.search_buildings(args.building, limit=5)
    if isinstance(found, vtgis.Unavailable):
        print(f"[vtgis] buildings UNAVAILABLE: {found.reason} ({found.detail})")
        return 1
    print(f"[vtgis] buildings matching {args.building!r}: {len(found)}")
    for b in found:
        print(f"         {b.building_id:>8}  {b.name}  "
              f"({b.lat}, {b.lon}) authoritative={b.coords_authoritative}")

    if not found:
        return 1
    target = found[0]
    if target.lat is None or target.lon is None:
        print("[vtgis] first result has no coordinates; stopping")
        return 1

    # Route from the official building point to a caller coordinate (an
    # anonymous point; explicitly NOT authoritative).
    for mode in (vtgis.MODE_WALKING, vtgis.MODE_ADA_WALKING):
        result = vtgis.solve_route(target, (37.22903, -80.41905), mode=mode)
        if isinstance(result, vtgis.Unavailable):
            print(f"[vtgis] {mode}: UNAVAILABLE: {result.reason}")
        elif isinstance(result, vtgis.NoRoute):
            print(f"[vtgis] {mode}: NO ROUTE ({result.reason})")
        else:
            print(f"[vtgis] {mode}: {result.distance_m} m, "
                  f"{len(result.geometry)} geometry points, "
                  f"{len(result.directions)} directions; "
                  f"est={result.estimated_minutes} min "
                  f"(gis_time={result.gis_travel_time_available})")

    active = vtgis.effective_closures()
    if isinstance(active, vtgis.Unavailable):
        print(f"[vtgis] closures UNAVAILABLE: {active.reason}")
    else:
        print(f"[vtgis] effective closures now: {len(active)}")

    print("[vtgis] note: routes are walking-only here; closures are NOT applied "
          "to the solve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())