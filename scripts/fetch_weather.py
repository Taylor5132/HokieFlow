#!/usr/bin/env python3
"""Edge fetch for NWS weather -> frozen fixtures/ (for DEMO_MODE=cache).

This is the ONLY place in the project that talks to api.weather.gov directly.
`hokieday/weather.py` routes every request through `hokieday.cache`, so the
library and its tests never open a socket.

What it captures (all keyless, official NWS):
  * /points/{lat},{lon}              point metadata -> grid + forecast URLs + zone
  * forecastHourly                    hourly periods (trimmed to --hours)
  * /alerts/active?point=...          active alerts for the point
  * /alerts/active?zone=...           active alerts for the forecast zone
  * /gridpoints/{g}/{x},{y}/stations  observation stations (trimmed)
  * /stations/{id}/observations/latest latest observation for the nearest station

The script is defensive: a missing/empty upstream does not abort the run; it
writes whatever it could and prints a summary. Alerts are frequently EMPTY,
which is a real state worth committing.

    python3 scripts/fetch_weather.py                 # write fixtures/
    python3 scripts/fetch_weather.py --hours 24 --stations 6
    python3 scripts/fetch_weather.py --dry-run       # fetch + report, no writes

Fixture freshness: the envelope `fetched_at` is set to the snapshot stamp of
the existing `bt_buses` fixture (or now, if absent) so the replay clock and the
weather ages stay self-consistent in DEMO_MODE=cache.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Write through the cache layer into the FROZEN store, exactly like
# scripts/seed_cache.py. Forcing replay mode before config import makes
# config.CACHE_DIR resolve to fixtures/.
os.environ.setdefault("DEMO_MODE", "cache")

from hokieday import cache, config  # noqa: E402
from hokieday import weather as weather_mod  # noqa: E402

USER_AGENT = config.USER_AGENT
ACCEPT = "application/geo+json"


def _get_json(url: str, timeout: int = 30):
    """Raw GET returning (payload, effective_url) or (None, url) on failure."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), r.geturl()
    except Exception as exc:                                    # noqa: BLE001
        print(f"  WARN fetch failed: {url}\n       {type(exc).__name__}: {exc}")
        return None, url


def _snapshot_stamp() -> str:
    """Reuse the bt_buses snapshot time when present so replay stays coherent."""
    p = config.CACHE_DIR / "bt_buses.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))["fetched_at"]
        except Exception:                                       # noqa: BLE001
            pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(name: str, params: dict, url: str, payload, stamp: str,
           dry_run: bool) -> bool:
    if payload is None:
        return False
    path = cache._json_path(name, params)
    if dry_run:
        print(f"  DRY   {path.name} ({len(json.dumps(payload)):,} bytes)")
        return True
    cache._write_envelope(path, url, payload)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["fetched_at"] = stamp
    envelope["mode"] = "weather-spike"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    print(f"  OK    {path.name} ({path.stat().st_size:,} bytes)")
    return True


def _trim_hourly(payload: dict, hours: int) -> dict:
    if not isinstance(payload, dict):
        return payload
    props = payload.get("properties") or {}
    periods = props.get("periods") or []
    props = dict(props)
    props["periods"] = periods[:hours]
    return {**payload, "properties": props}


def _trim_stations(payload: dict, limit: int) -> dict:
    if not isinstance(payload, dict):
        return payload
    feats = payload.get("features") or []
    return {**payload, "features": feats[:limit]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=weather_mod.DEFAULT_LAT)
    ap.add_argument("--lon", type=float, default=weather_mod.DEFAULT_LON)
    ap.add_argument("--hours", type=int, default=48,
                    help="hourly periods to keep (default 48)")
    ap.add_argument("--stations", type=int, default=12,
                    help="observation stations to keep (default 12)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stamp = _snapshot_stamp()
    lat_s, lon_s = f"{args.lat:.4f}", f"{args.lon:.4f}"
    coords = f"{lat_s},{lon_s}"
    print(f"NWS weather fetch for {coords} (target={config.CACHE_DIR}, "
          f"mode={config.DEMO_MODE}, stamp={stamp})")

    point_url = config.ENDPOINTS["weather_points"].format(lat=lat_s, lon=lon_s)
    point, _ = _get_json(point_url)
    if point is None:
        print("point metadata failed; nothing else can be resolved. aborting.")
        return 1
    pprops = point.get("properties") or {}
    grid_id, grid_x, grid_y = pprops.get("gridId"), pprops.get("gridX"), pprops.get("gridY")
    zone = str(pprops.get("forecastZone") or "").rsplit("/", 1)[-1] or None
    hourly_url = pprops.get("forecastHourly")
    stations_url = pprops.get("observationStations")
    print(f"  resolved grid={grid_id}/{grid_x},{grid_y} zone={zone} "
          f"timezone={pprops.get('timeZone')}")

    written = 0
    written += _write("weather_points", {"lat": lat_s, "lon": lon_s},
                      point_url, point, stamp, args.dry_run)

    if hourly_url:
        hourly, _ = _get_json(hourly_url)
        hourly = _trim_hourly(hourly, args.hours)
        written += _write(
            "weather_hourly", {"grid": str(grid_id), "x": str(grid_x), "y": str(grid_y)},
            hourly_url, hourly, stamp, args.dry_run)

    alerts_url = f"{weather_mod.NWS_API_BASE}/alerts/active?point={coords}"
    alerts_point, _ = _get_json(alerts_url)
    written += _write("weather_alerts_point", {"lat": lat_s, "lon": lon_s},
                      alerts_url, alerts_point, stamp, args.dry_run)
    if zone:
        zone_url = f"{weather_mod.NWS_API_BASE}/alerts/active?zone={zone}"
        alerts_zone, _ = _get_json(zone_url)
        written += _write("weather_alerts_zone", {"zone": zone},
                          zone_url, alerts_zone, stamp, args.dry_run)

    nearest_station = None
    if stations_url:
        stations, _ = _get_json(stations_url)
        stations = _trim_stations(stations, args.stations)
        written += _write(
            "weather_stations", {"grid": str(grid_id), "x": str(grid_x), "y": str(grid_y)},
            stations_url, stations, stamp, args.dry_run)
        feats = (stations or {}).get("features") or []
        if feats:
            nearest_station = (feats[0].get("properties") or {}).get("stationIdentifier")

    if nearest_station:
        obs_url = f"{weather_mod.NWS_API_BASE}/stations/{nearest_station}/observations/latest"
        obs, _ = _get_json(obs_url)
        written += _write("weather_observation", {"station": nearest_station},
                          obs_url, obs, stamp, args.dry_run)
        print(f"  nearest station={nearest_station}")
    else:
        print("  WARN no nearest station resolved; skipped latest observation")

    print(f"\n{'would write' if args.dry_run else 'wrote'} {written} weather fixtures "
          f"into {config.CACHE_DIR}")
    return 0 if written >= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())