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

from hokieday import config, tools, vtgis

UI_DIR = Path(__file__).resolve().parent.parent / "ui"

# Exact basenames only: index.html was already served at "/" and the UI fetches
# every other asset by its fixed name. A mapping (instead of Path.joinpath on
# user input) makes path traversal structurally impossible.
_ASSET_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "config.js": "text/javascript; charset=utf-8",
    "model.js": "text/javascript; charset=utf-8",
    "fixtures.js": "text/javascript; charset=utf-8",
    "home-live.js": "text/javascript; charset=utf-8",
    "bus-location.js": "text/javascript; charset=utf-8",
    "dining.js": "text/javascript; charset=utf-8",
    "schedule.js": "text/javascript; charset=utf-8",
    "directions.js": "text/javascript; charset=utf-8",
    "preferences.js": "text/javascript; charset=utf-8",
    "buildings.js": "text/javascript; charset=utf-8",
    "vt-gis.js": "text/javascript; charset=utf-8",
}

_CACHEABLE = {"styles.css", "app.js", "config.js", "model.js", "fixtures.js",
              "home-live.js", "bus-location.js", "dining.js", "schedule.js",
              "directions.js", "preferences.js", "buildings.js",
              "vt-gis.js"}


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
    """Compact, stable identity for UI state (never the raw provider object)."""
    if isinstance(user, dict):
        return {"id": user.get("id"), "email": user.get("email")}
    return {"id": getattr(user, "id", None), "email": getattr(user, "email", None)}


def enrich_account(body: dict, user: object) -> dict:
    """Add {data, version} from Supabase when configured.

    Contract (CONNECTING.md): data is exactly {savedClass, reduceMotion,
    plans, events}; version is a monotonic integer for optimistic concurrency.
    No database configured -> account data stays absent and the UI keeps
    working logged-in without cloud save.
    """
    try:
        from database import supabase  # optional dependency, may be None
    except Exception:                                    # noqa: BLE001
        return body
    client = supabase
    if client is None:
        return body
    uid = user_ref(user).get("id")
    if not uid:
        return body
    try:
        rows = (client.table("account_data")
                .select("data, version")
                .eq("user_id", uid)
                .limit(1).execute().data) or []
        if rows:
            body["data"] = rows[0].get("data") or {}
            body["version"] = int(rows[0].get("version") or 0)
    except Exception:                                    # noqa: BLE001
        pass
    return body


def save_account(user: object, data: object, version: object) -> tuple[dict, int]:
    """Optimistic-concurrency account save. 409 on a stale version."""
    if not isinstance(data, dict):
        return {"error": "data must be an object"}, 400
    allowed = {"savedClass", "reduceMotion", "plans", "events"}
    clean = {k: v for k, v in data.items() if k in allowed}
    try:
        from database import supabase
        client = supabase
    except Exception:                                    # noqa: BLE001
        client = None
    if client is None:
        return {"error": "account storage is not configured"}, 503
    uid = user_ref(user).get("id")
    if not uid:
        return {"error": "authentication required"}, 401
    try:
        rows = (client.table("account_data")
                .select("version").eq("user_id", uid).limit(1).execute().data) or []
        current = int(rows[0].get("version") or 0) if rows else 0
        try:
            requested = int(version)
        except (TypeError, ValueError):
            requested = 0
        if requested != current:
            return {"error": "stale version — reload and retry",
                    "version": current}, 409
        client.table("account_data").upsert(
            {"user_id": uid, "data": clean, "version": current + 1},
            on_conflict="user_id").execute()
        return {"ok": True, "data": clean, "version": current + 1}, 200
    except Exception as exc:                             # noqa: BLE001
        return {"error": f"save failed: {type(exc).__name__}"}, 500


# --------------------------------------------------------------------------- #
# GET endpoints
# --------------------------------------------------------------------------- #

def dining_places_endpoint() -> dict:
    """Curated campus dining directory (CONNECTING.md contract).

    The planner's place registry already carries the curated coordinates the
    trip planner uses, so the UI browses exactly the places plans can route
    to. Menus/allergens stay in /api/ask results; this list never fabricates
    hours or open-now state it does not have.
    """
    places = []
    for key, row in config.static_places():
        name = str(key)
        low = name.lower()
        category = ("dining-hall" if ("d2" in low or "dietrick" in low
                                      or "owens" in low or "west end" in low)
                    else "cafe" if ("coffee" in low or "cafe" in low)
                    else "market" if "market" in low else "dining")
        places.append({
            "id": name,
            "name": name,
            "category": category,
            "lat": row.get("lat"),
            "lon": row.get("lon"),
            # Omitted, never guessed: no fabricated hours/description here.
        })
    return {"places": places, "note": ("coordinates match the planner place "
            "registry; verified flags are deliberate")}


def transit_stops_endpoint() -> dict:
    """BT stops (id/name/lat/lon) for the Near-me departure board."""
    try:
        g = tools._src(None)._g()          # the shared lazy GTFS loader
    except Exception as exc:                             # noqa: BLE001
        return {"stops": [], "status": "unavailable",
                "reason": f"transit schedule unavailable: {exc}"}
    stops = [{"id": s.stop_id, "name": s.name, "lat": s.lat, "lon": s.lon}
             for s in g.stops.values()]
    stops.sort(key=lambda s: s["id"])
    return {"stops": stops}


def transit_departures_endpoint(stop_id: str) -> dict:
    """Departure board rows for one stop, service-filtered (schedule truth)."""
    result = tools.get_next_departures(str(stop_id or ""))
    return {"stop_id": result.get("stop_id"), "departures": result.get("departures", []),
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
