#!/usr/bin/env python3
"""Edge fetch for NWS weather -> frozen fixtures/ (for DEMO_MODE=cache).

This is the ONLY place in the project that talks to api.weather.gov directly.
`hokieday/weather.py` routes every request through `hokieday.cache`, so the
library and its tests never open a socket.

What it captures (all keyless, official NWS):
  * /points/{lat},{lon}               point metadata -> grid + forecast URLs + zone
  * forecastHourly                     hourly periods (trimmed to --hours)
  * /alerts/active?point=...           active alerts for the point
  * /alerts/active?zone=...            active alerts for the forecast zone
  * /gridpoints/{g}/{x},{y}/stations   observation stations (trimmed)
  * /stations/{id}/observations/latest latest observation for the nearest station

HONESTY RULES (learned from a rejected bundle):
  * `fetched_at` is the REAL acquisition time of each payload. It is never
    rewritten to match another snapshot.
  * The whole bundle is fetched into memory first, validated for temporal
    coherence, then published ALL-OR-NONE through cache.publish_envelopes().
    There is no Path.write_text() envelope editing here.
  * A bundle whose hourly forecast does not cover the acquisition time, whose
    observation is stale/future, or whose resources were fetched minutes apart
    is REJECTED. An incoherent bundle is worse than no bundle.

Mode safety: this script writes the FROZEN fixtures/ store. It refuses to run if
DEMO_MODE is set to anything other than "cache" (no silent setdefault).

VISIBILITY: publication is an OFFLINE, STOPPED-APP operation. Each file is
swapped atomically, but the bundle as a whole is NOT reader-atomically visible
(there is no versioned pointer), so a running server could read a mixed
snapshot. Writing therefore requires the explicit `--publish` flag, and the
script prints the returned bundle manifest version.

    python3 scripts/fetch_weather.py                 # fetch + validate (no writes)
    python3 scripts/fetch_weather.py --publish       # write; app must be stopped
    python3 scripts/fetch_weather.py --hours 24 --stations 6
    python3 scripts/fetch_weather.py --dry-run       # fetch + validate, no writes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _resolve_mode() -> str:
    """Explicitly control DEMO_MODE; refuse a mode this script must not use."""
    requested = os.environ.get("DEMO_MODE")
    if requested is None:
        os.environ["DEMO_MODE"] = "cache"
        return "cache"
    if requested.strip().lower() != "cache":
        print(f"refusing: DEMO_MODE={requested!r}. This script writes the frozen "
              f"fixtures/ store and must run with DEMO_MODE=cache "
              f"(unset DEMO_MODE, or set it to 'cache').", file=sys.stderr)
        raise SystemExit(2)
    return "cache"


MODE = _resolve_mode()

from hokieday import cache, config  # noqa: E402
from hokieday import weather as weather_mod  # noqa: E402

USER_AGENT = config.USER_AGENT
ACCEPT = "application/geo+json"


def _get_json(url: str, timeout: int = 30):
    """Raw GET -> (payload, effective_url, acquired_at_iso) or (None, url, None)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
            acquired = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return payload, r.geturl(), acquired
    except Exception as exc:                                    # noqa: BLE001
        print(f"  WARN fetch failed: {url}\n       {type(exc).__name__}: {exc}")
        return None, url, None


def _trim_hourly(payload: dict, hours: int) -> dict:
    if not isinstance(payload, dict):
        return payload
    props = payload.get("properties") or {}
    props = dict(props)
    props["periods"] = (props.get("periods") or [])[:hours]
    return {**payload, "properties": props}


def _sort_stations(payload: dict) -> dict:
    """Sort the FULL station list by normalized distance (None last).

    Trimming before sorting could discard the nearest station just because NWS
    ordered the response differently, so sort first, then trim.
    """
    if not isinstance(payload, dict):
        return payload
    feats = list(payload.get("features") or [])

    def distance(feature):
        value = ((feature.get("properties") or {}).get("distance") or {}).get("value")
        return (value is None, value if value is not None else float("inf"))

    feats.sort(key=distance)
    return {**payload, "features": feats}


def _trim_stations(payload: dict, limit: int) -> dict:
    if not isinstance(payload, dict):
        return payload
    return {**payload, "features": (payload.get("features") or [])[:limit]}


def _required_names(entries: list[dict]) -> list[str]:
    """Required resources derived from the point metadata.

    Zone alerts are required when the point names a forecast zone; the station
    list is required when the point names an observation-stations URL; the
    observation is required once a station list is present.
    """
    required = ["weather_points", "weather_hourly", "weather_alerts_point"]
    point = next((e for e in entries if e["name"] == "weather_points"), None)
    props = ((point or {}).get("payload") or {}).get("properties") or {}
    if str(props.get("forecastZone") or "").strip():
        required.append("weather_alerts_zone")
    if str(props.get("observationStations") or "").strip():
        required.append("weather_stations")
    if any(e["name"] == "weather_stations" for e in entries):
        required.append("weather_observation")
    return required


def _validate(entries: list[dict], now: datetime) -> list[str]:
    """Temporal/coverage invariants. Any error means the bundle must NOT publish."""
    errors: list[str] = []
    names = {e["name"] for e in entries}
    for required in _required_names(entries):
        if required not in names:
            errors.append(f"bundle incomplete: missing {required}")

    stamps = []
    for entry in entries:
        ts = weather_mod._parse_ts(entry.get("fetched_at"))
        if ts is None:
            errors.append(f"{entry['name']}: unparseable fetched_at")
        else:
            stamps.append((entry["name"], ts))
    if stamps:
        span = max(ts for _, ts in stamps) - min(ts for _, ts in stamps)
        if span > timedelta(minutes=10):
            errors.append(
                f"resources were fetched over {span}; not one coherent snapshot")

    hourly = next((e for e in entries if e["name"] == "weather_hourly"), None)
    if hourly is not None:
        periods = ((hourly["payload"] or {}).get("properties") or {}).get("periods") or []
        if not periods:
            errors.append("weather_hourly has no periods")
        else:
            starts = [s for s in (weather_mod._parse_ts(p.get("startTime"))
                                  for p in periods) if s]
            ends = [e for e in (weather_mod._parse_ts(p.get("endTime"))
                                for p in periods) if e]
            if not starts or not ends:
                errors.append("weather_hourly periods lack start/end times")
            else:
                if min(starts) > now + timedelta(hours=2):
                    errors.append("hourly forecast starts >2 h after acquisition")
                if max(ends) < now:
                    errors.append("hourly forecast ends before acquisition")
                if not (min(starts) <= now <= max(ends)):
                    errors.append(
                        "hourly forecast does not cover the acquisition time")

    obs = next((e for e in entries if e["name"] == "weather_observation"), None)
    if obs is not None:
        props = (obs["payload"] or {}).get("properties") or {}
        ots = weather_mod._parse_ts(props.get("timestamp"))
        if ots is None:
            errors.append("observation has no parseable timestamp")
        else:
            if ots > now + timedelta(hours=1):
                errors.append("observation timestamp is in the future")
            if now - ots > timedelta(hours=6):
                errors.append("observation is older than 6 h; not 'latest'")
    return errors


def _replay_pin() -> datetime | None:
    """The frozen store's replay clock (bt_buses fetched_at), if present."""
    env = cache.read_envelope("bt_buses", None)
    if not env:
        return None
    return weather_mod._parse_ts(env.get("fetched_at"))


def _replay_coherence_error(entries: list[dict], pin: datetime) -> str | None:
    """Refuse a bundle whose forecast would not cover the frozen replay clock.

    Publishing weather whose forecast starts after the bus replay 'now' is
    exactly the incoherent snapshot this project removed; require an explicit
    override instead of silently recreating it.
    """
    hourly = next((e for e in entries if e["name"] == "weather_hourly"), None)
    if hourly is None:
        return None
    periods = ((hourly["payload"] or {}).get("properties") or {}).get("periods") or []
    starts = [s for s in (weather_mod._parse_ts(p.get("startTime"))
                          for p in periods) if s]
    ends = [e for e in (weather_mod._parse_ts(p.get("endTime"))
                        for p in periods) if e]
    if starts and ends and not (min(starts) <= pin <= max(ends)):
        return (f"hourly forecast does not cover the frozen replay clock "
                f"{pin.isoformat()}; publishing would recreate an incoherent "
                f"snapshot. Recapture the other campus sources first, or pass "
                f"--force-incoherent")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=weather_mod.DEFAULT_LAT)
    ap.add_argument("--lon", type=float, default=weather_mod.DEFAULT_LON)
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--stations", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--publish", action="store_true",
                    help="write the frozen store; only while the app is stopped")
    ap.add_argument("--force-incoherent", action="store_true",
                    help="publish even if the forecast does not cover the replay clock")
    args = ap.parse_args()

    lat_s, lon_s = f"{args.lat:.4f}", f"{args.lon:.4f}"
    coords = f"{lat_s},{lon_s}"
    target = config.FIXTURES_DIR
    print(f"NWS weather fetch for {coords} (target={target}, mode={MODE})")

    point_url = config.ENDPOINTS["weather_points"].format(lat=lat_s, lon=lon_s)
    point, point_url_eff, point_at = _get_json(point_url)
    if point is None:
        print("point metadata failed; aborting (nothing published).")
        return 1
    pprops = point.get("properties") or {}
    grid_id, grid_x, grid_y = pprops.get("gridId"), pprops.get("gridX"), pprops.get("gridY")
    zone = str(pprops.get("forecastZone") or "").rsplit("/", 1)[-1] or None
    hourly_url = pprops.get("forecastHourly")
    stations_url = pprops.get("observationStations")
    print(f"  resolved grid={grid_id}/{grid_x},{grid_y} zone={zone} "
          f"timezone={pprops.get('timeZone')}")

    entries: list[dict] = [{
        "name": "weather_points", "params": {"lat": lat_s, "lon": lon_s},
        "url": point_url_eff, "payload": point, "fetched_at": point_at,
    }]

    if hourly_url:
        payload, eff, at = _get_json(hourly_url)
        if payload is not None:
            entries.append({
                "name": "weather_hourly",
                "params": {"grid": str(grid_id), "x": str(grid_x), "y": str(grid_y)},
                "url": eff, "payload": _trim_hourly(payload, args.hours),
                "fetched_at": at,
            })

    alerts_url = f"{weather_mod.NWS_API_BASE}/alerts/active?point={coords}"
    payload, eff, at = _get_json(alerts_url)
    if payload is not None:
        entries.append({
            "name": "weather_alerts_point",
            "params": {"lat": lat_s, "lon": lon_s},
            "url": eff, "payload": payload, "fetched_at": at,
        })
    if zone:
        zone_url = f"{weather_mod.NWS_API_BASE}/alerts/active?zone={zone}"
        payload, eff, at = _get_json(zone_url)
        if payload is not None:
            entries.append({
                "name": "weather_alerts_zone", "params": {"zone": zone},
                "url": eff, "payload": payload, "fetched_at": at,
            })

    nearest_station = None
    if stations_url:
        payload, eff, at = _get_json(stations_url)
        if payload is not None:
            sorted_payload = _sort_stations(payload)
            trimmed = _trim_stations(sorted_payload, args.stations)
            entries.append({
                "name": "weather_stations",
                "params": {"grid": str(grid_id), "x": str(grid_x), "y": str(grid_y)},
                "url": eff, "payload": trimmed, "fetched_at": at,
            })
            feats = (trimmed or {}).get("features") or []
            if feats:
                nearest_station = (feats[0].get("properties") or {}).get(
                    "stationIdentifier")

    if nearest_station:
        obs_url = (f"{weather_mod.NWS_API_BASE}/stations/{nearest_station}"
                   "/observations/latest")
        payload, eff, at = _get_json(obs_url)
        if payload is not None:
            entries.append({
                "name": "weather_observation",
                "params": {"station": nearest_station},
                "url": eff, "payload": payload, "fetched_at": at,
            })
        print(f"  nearest station={nearest_station}")
    else:
        print("  WARN no nearest station resolved; bundle will be incomplete")

    now = datetime.now(timezone.utc)
    errors = _validate(entries, now)
    pin = _replay_pin()
    if pin is not None and not args.force_incoherent:
        coherence_error = _replay_coherence_error(entries, pin)
        if coherence_error:
            errors.append(coherence_error)
    if errors:
        print("\nREJECTED: bundle failed temporal validation; nothing published:")
        for err in errors:
            print(f"  - {err}")
        return 1

    if args.dry_run and args.publish:
        print("\nrefusing: --dry-run and --publish are mutually exclusive")
        return 2
    if args.dry_run:
        print(f"\nvalidated {len(entries)} resources; --dry-run, nothing published")
        for entry in entries:
            print(f"  {entry['name']:24s} {entry.get('fetched_at')}")
        return 0
    if not args.publish:
        print(f"\nvalidated {len(entries)} resources; preview only.\n"
              "Pass --publish WHILE THE APP IS STOPPED to write the frozen "
              "store (publication is not reader-atomic across the bundle).")
        for entry in entries:
            print(f"  {entry['name']:24s} {entry.get('fetched_at')}")
        return 0

    try:
        manifest = cache.publish_envelopes(entries, cache_dir=target)
    except Exception as exc:                                    # noqa: BLE001
        print(f"\nPublish failed (rolled back, all-or-none): {type(exc).__name__}: {exc}")
        return 1
    print(f"\npublished {manifest['count']} weather fixtures into {target}")
    print(f"  bundle version={manifest['version']} at {manifest['published_at']}")
    for path in manifest["files"]:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())