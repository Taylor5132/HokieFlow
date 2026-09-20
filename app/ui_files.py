"""UI serving + the read/plan endpoints the ui/ single-page app expects.

WHY THIS MODULE EXISTS
The ui/ folder is the shipped application design (mobile-first chat shell with
Home, Bus, Dining, and account screens). The demo server used to serve only
app/index.html; these screens call a set of same-origin JSON contracts that did
not exist yet. This module wires those contracts to the existing hokieday
library so the UI renders REAL data and degrades with honest typed failures.

HONESTY RULES CARRIED OVER
  * A failed source is "unavailable", never fabricated data.
  * VT GIS geometry is served only through the recorded written-permission
    capability (see vtgis.RedistributionPermit). No permit, no geometry --
    the UI falls back to its labeled schematic or a plain building list.
  * Replay clock: everything time-based goes through config.now(), never
    datetime.now().
"""
from __future__ import annotations

import os
from pathlib import Path

from hokieday import config, tools, vtgis, transit

UI_DIR = Path(__file__).resolve().parent.parent / "ui"

# Exact basenames only: index.html was already served at "/" and the UI fetches
# every other asset by its fixed name. A mapping (instead of Path.joinpath on
# user input) makes path traversal structurally impossible.
_ASSET_TYPES = {
    "motion.js": "text/javascript; charset=utf-8",
    "gsap.min.js": "text/javascript; charset=utf-8",
    "ScrollTrigger.min.js": "text/javascript; charset=utf-8",
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "config.js": "text/javascript; charset=utf-8",
    "model.js": "text/javascript; charset=utf-8",
    "fixtures.js": "text/javascript; charset=utf-8",
    "home-live.js": "text/javascript; charset=utf-8",
    "bus-location.js": "text/javascript; charset=utf-8",
    "dining.js": "text/javascript; charset=utf-8",
    "class-import.js": "text/javascript; charset=utf-8",
    "schedule.js": "text/javascript; charset=utf-8",
    "directions.js": "text/javascript; charset=utf-8",
    "preferences.js": "text/javascript; charset=utf-8",
    "buildings.js": "text/javascript; charset=utf-8",
}

_CACHEABLE = {"styles.css", "app.js", "config.js", "model.js", "fixtures.js",
              "home-live.js", "bus-location.js", "dining.js", "schedule.js",
              "directions.js", "preferences.js", "buildings.js"}


def _make_permit() -> vtgis.RedistributionPermit | None:
    """Capability for serving already-fetched GIS geometry to our own UI.

    The token records WHERE written permission is documented; it is not itself
    the grant. Set HOKIEDAY_GIS_PERMISSION_REF once the team records the
    written VT GIS redistribution permission; without it this module serves
    building POINTS with attribution only, never polygons/paths.
    """
    ref = (os.environ.get("HOKIEDAY_GIS_PERMISSION_REF") or "").strip()
    return vtgis.authorize_redistribution(ref) if ref else None


_PERMIT = _make_permit()


def serve(path: str) -> tuple[int, bytes, str, list[tuple[str, str]]] | None:
    """UI asset lookup. Returns (code, body, ctype, extra_headers) or None."""
    name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
    ctype = _ASSET_TYPES.get(name)
    if ctype is None:
        return None
    try:
        body = (UI_DIR / name).read_bytes()
    except OSError:
        return None
    headers = ([("Cache-Control", "public, max-age=300")]
               if name in _CACHEABLE else [])
    return 200, body, ctype, headers


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def user_ref(user: object) -> dict:
    """Compact identity for UI state (never the raw provider object).

    The client renders `Hi, ${user.name}` and takes `user.name[0]` for the
    avatar, so the display name has to survive here. Accepts either the
    normalized dict auth.py builds or a raw Supabase user object.
    """
    if isinstance(user, dict):
        metadata = user.get("user_metadata") or {}
        app_metadata = user.get("app_metadata") or {}
        email = user.get("email")
        return {
            "id": user.get("id"),
            "email": email,
            "name": (user.get("name") or metadata.get("full_name")
                     or metadata.get("name")
                     or (email.split("@", 1)[0] if email else None)),
            "picture": user.get("picture") or metadata.get("avatar_url") \
                or metadata.get("picture"),
            "provider": user.get("provider") or app_metadata.get("provider"),
        }
    metadata = getattr(user, "user_metadata", None) or {}
    email = getattr(user, "email", None)
    return {
        "id": getattr(user, "id", None),
        "email": email,
        "name": metadata.get("full_name") or metadata.get("name") \
            or (email.split("@", 1)[0] if email else None),
        "picture": metadata.get("avatar_url") or metadata.get("picture"),
        "provider": (getattr(user, "app_metadata", None) or {}).get("provider"),
    }


def _account_token(headers=None, access_token=None):
    if access_token:
        return access_token
    if not headers:
        return None
    bearer = headers.get("Authorization") or headers.get("authorization") or ""
    if str(bearer).lower().startswith("bearer "):
        return str(bearer).split(" ", 1)[1].strip()
    from auth import get_session_cookie
    from app import supabase_session
    session = get_session_cookie(headers.get("Cookie") or headers.get("cookie"))
    # The cookie is valid for a week, but the Supabase access token inside it
    # lasts about an hour. Refreshing here -- one choke point for account reads
    # and writes -- is what stops a signed-in student from being told to sign in
    # again while the UI still shows them signed in.
    return supabase_session.token_for(session)


def enrich_account(body: dict, user: object, *, headers=None, access_token=None) -> dict:
    """Load UI state using the same user's session as the authenticated request."""
    from app import account_store
    uid = user_ref(user).get("id")
    if not uid:
        return body
    try:
        body.update(account_store.load(uid, _account_token(headers, access_token)))
    except account_store.StorageError as exc:
        # A failed read is not an empty account. The UI blocks saves until
        # a successful reload so it cannot overwrite unknown existing data.
        body["storageError"] = str(exc)
        body["storageErrorCode"] = exc.code
    return body


def save_account(user: object, data: object, version: object, *, headers=None) -> tuple[dict, int]:
    from app import account_store
    uid = user_ref(user).get("id")
    if not uid:
        return {"error": "authentication required"}, 401
    try:
        result = account_store.save(uid, _account_token(headers), data, version)
        return {**result, "user": user_ref(user)}, 200
    except account_store.StorageError as exc:
        return {"error": str(exc), "code": exc.code}, exc.status

# --------------------------------------------------------------------------- #
# GET endpoints
# --------------------------------------------------------------------------- #

def dining_places_endpoint() -> dict:
    """Curated campus dining directory (CONNECTING.md contract).

    Source is hokieday.dining_places -- the curated DINING list -- not
    config.static_places(), which is the PLANNER's registry of routable places
    (Burruss Hall, Stop 1600, ...). Serving the planner registry here put
    non-dining buildings on the dining screens, and it omits `building`, so each
    card's subtitle fell back to a generic string.

    Coordinates are published building reference points, not entrances; the
    browser computes straight-line distances. Hours/open-now and menus are
    deliberately absent rather than guessed.
    """
    from hokieday import dining_places as directory
    payload = directory.dining()
    places = []
    for row in payload.get("places") or []:
        places.append({
            "id": row.get("name"),
            "name": row.get("name"),
            "building": row.get("building"),
            "lat": row.get("lat"),
            "lon": row.get("lon"),
            "source_url": row.get("source_url"),
            "category": "dining",
        })
    return {"places": places, "source": payload.get("source"),
            "note": ("curated dining directory; coordinates are building "
                     "reference points, distances are straight-line estimates")}


def transit_stops_endpoint() -> dict:
    try:
        return transit.stops()
    except Exception:
        return {"stops": [], "status": "unavailable", "reason": "BT stops are temporarily unavailable."}


def transit_departures_endpoint(stop_id: str) -> dict:
    if config.CACHE_ONLY:
        payload = _scheduled_transit_departures_endpoint(stop_id)
        return {**payload, "is_replay": True, "fetched_at": config.now().isoformat()}
    try:
        return transit.departures(str(stop_id or ""))
    except Exception:
        return {"departures": [], "status": "unavailable", "reason": "BT departures are temporarily unavailable."}


def _scheduled_transit_departures_endpoint(stop_id: str) -> dict:
    """Departure board rows for one stop, service-filtered (schedule truth).

    The row field names are the UI's contract (`departure_at`, `route`,
    `stop_id`, `pattern`), not the planner tool's (`dep_time`, `route_id`,
    `head_sign`). ui/home-live.js parses `departure_at` and groups on `route`,
    so returning the tool's names meant every row parsed to NaN and the board
    rendered empty even when departures existed.

    Only real source values are mapped. `pattern` carries the head sign (the
    destination text the source publishes); `destination_loop` is left to the
    caller because the schedule feed does not state a loop colour.
    """
    result = tools.get_next_departures(str(stop_id or ""))
    stop_name = None
    try:
        stop = tools._src(None)._g().stops.get(str(stop_id))
        stop_name = getattr(stop, "name", None)
    except Exception:                                        # noqa: BLE001
        stop_name = None
    rows = []
    for row in result.get("departures") or []:
        rows.append({
            "stop_id": row.get("stop_id"),
            "stop_name": stop_name,
            "route": row.get("route_id"),
            "departure_at": row.get("dep_time"),
            "pattern": row.get("head_sign"),
            "trip_id": row.get("trip_id"),
            "in_min": row.get("in_min"),
            "is_realtime": row.get("is_realtime"),
        })
    return {"stop_id": result.get("stop_id"), "departures": rows,
            "reason": result.get("reason")}


def buildings_endpoint(query_values: list[str]) -> dict:
    """Building search -> GeoJSON FeatureCollection (points).

    Serve full geometry (footprint rings) only under the recorded written
    permission; otherwise points with attribution still give the UI a working
    search and selector.
    """
    q = (query_values[0] if query_values else "").strip()
    try:
        results = vtgis.search_buildings(q, limit=30)
    except Exception as exc:                             # noqa: BLE001
        return {"type": "FeatureCollection", "features": [],
                "status": "unavailable", "reason": f"buildings unavailable: {exc}"}
    if isinstance(results, vtgis.Unavailable):
        return {"type": "FeatureCollection", "features": [],
                "status": "unavailable", "reason": results.reason}
    features = []
    for b in results:
        if b.lat is None or b.lon is None:
            continue
        geometry = ({"type": "Point", "coordinates": [b.lon, b.lat]}
                    if not b.geometry else
                    {"type": "Polygon",
                     "coordinates": [[[lon, lat] for lat, lon in b.geometry]]})
        features.append({
            "type": "Feature",
            "id": str(b.building_id),
            "geometry": geometry,
            "properties": {"id": str(b.building_id), "name": b.name,
                           "bldg_num": b.bldg_num,
                           "provenance": b.provenance.as_dict()},
        })
    return {"type": "FeatureCollection", "features": features,
            "status": "ok" if features else "empty"}


def map_state_endpoint(now_dt) -> dict:
    """Live/replay transit overlay + effective construction closures."""
    try:
        live = tools.get_live_bus(None)
    except Exception as exc:                             # noqa: BLE001
        live = {"buses": [], "reason": f"live buses unavailable: {exc}"}
    buses = live.get("buses", live) if isinstance(live, dict) else live
    features = []
    for bus in buses if isinstance(buses, list) else []:
        lat, lon = bus.get("lat"), bus.get("lon")
        if lat is None or lon is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"kind": "bus", "bus_id": bus.get("bus_id"),
                           "route_id": bus.get("route_id"),
                           "load_pct": bus.get("load_pct"),
                           "sched_delta_min": bus.get("sched_delta_min"),
                           "is_stale": bus.get("is_stale"),
                           "observed_at": bus.get("observed_at")},
        })
    closures = []
    try:
        for c in vtgis.effective_closures(now_dt):
            ring = [[lon, lat] for lat, lon in (c.geometry or [])]
            if len(ring) >= 4:
                closures.append({
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {"kind": "closure", "name": c.name,
                                   "start": c.start.isoformat() if c.start else None,
                                   "end": c.end.isoformat() if c.end else None,
                                   "provenance": c.provenance.as_dict()},
                })
    except Exception:                                    # noqa: BLE001
        pass
    meta = config.status() if hasattr(config, "status") else {}
    return {"type": "FeatureCollection", "features": features,
            "closures": closures,
            "is_replay": bool(getattr(config, "CACHE_ONLY", False)),
            "observed_at": (live.get("observed_at") if isinstance(live, dict)
                            else None),
            "mode": meta.get("mode") if isinstance(meta, dict) else None}


def route_endpoint(payload: dict) -> tuple[dict, int]:
    """POST /api/route — VT GIS routed path between two official buildings."""
    origin = payload.get("origin") or {}
    destination = payload.get("destination") or {}
    mode = payload.get("mode", "fastest")
    try:
        mode = vtgis.validate_mode(mode)
        if mode == vtgis.MODE_ADA_WALKING:
            mode = vtgis.UI_MODE_ADA if hasattr(vtgis, "UI_MODE_ADA") else mode
    except ValueError as exc:
        return {"error": str(exc)}, 400
    from_id, to_id = origin.get("building_id"), destination.get("building_id")
    if not from_id or not to_id:
        return {"error": "origin.building_id and destination.building_id are required"}, 400
    try:
        route = vtgis.route_between_buildings(str(from_id), str(to_id), mode=mode)
    except Exception as exc:                             # noqa: BLE001
        return {"error": f"route failed: {type(exc).__name__}: {exc}",
                "status": "unavailable"}, 502
    if isinstance(route, vtgis.Unavailable):
        return route.as_dict(), 503
    if isinstance(route, vtgis.NoRoute):
        return route.as_dict(), 200                      # a real, honest answer
    body = {"status": "ok", "mode": route.mode,
            "distance_m": route.distance_m, "distance_ft": route.distance_ft,
            "distance_source": route.distance_source,
            "estimated_minutes": route.estimated_minutes,
            "estimated_minutes_label": route.estimated_minutes_label,
            "directions": [d.as_dict() for d in route.directions],
            "notes": list(route.notes),
            "provenance": route.provenance.as_dict()}
    try:
        geojson = vtgis.route_to_geojson(route, permission=_PERMIT)
        body["geometry"] = geojson["features"][0]["geometry"]
    except vtgis.RedistributionNotPermitted:
        body["geometry"] = None
        body["fallback"] = True
        body["notes"] = list(route.notes) + [
            "Path geometry withheld pending the recorded VT GIS redistribution "
            "permission; distance and directions are still real."]
    return body, 200


def campus_events_endpoint():
    """Expose Taylor's browse/calendar contract without writing a calendar."""
    from hokieday import events
    candidates = [config.CACHE_DIR / "events" / "events_september_2026.json",
                  config.FIXTURES_DIR / "events" / "events_september_2026.json"]
    try:
        path = next(p for p in candidates if p.exists())
        snapshot = events.load_snapshot(path)
        now = config.now()
        result = events.browse(snapshot, start=now, now=now,
                               include_cancelled=False, include_uncertain=False, limit=100)
        rows = []
        for event in result.events:
            if event.invalid_range or event.status == "parser-failed":
                continue
            calendar = events.to_calendar_event(event)
            if event.duration_unknown and not event.all_day:
                calendar["end"] = None
            rows.append({"id": event.id, **calendar})
        return {"events": rows, "state": result.state, "notices": list(result.notices),
                "month": snapshot.month, "fetched_at": snapshot.fetched_at}
    except Exception:
        return {"events": [], "state": "unavailable", "notices": ["Campus events could not load. Please try again."]}
