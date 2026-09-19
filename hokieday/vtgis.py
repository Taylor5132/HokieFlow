"""Permission-safe VT Enterprise GIS client + multimodal-routing foundation.

WHAT THIS MODULE IS
-------------------
A stdlib-only adapter over Virginia Tech's public ArcGIS REST services that the
rest of HokieFlow can consume without touching the network except through the
cache layer:

  * Building search / detail and the official building<->entrance join.
  * Walking and ADA Walking route geometry, distance and turn directions.
  * Campus construction closures with effective intervals.
  * Provenance + attribution on every result, and typed `unavailable` /
    `no_route` results instead of exceptions or fabricated numbers.

WHAT THIS MODULE IS NOT (deliberate scope boundary)
---------------------------------------------------
`Fastest` multimodal (GIS walking edges ranked against GTFS / live-bus edges) is
the PARENT's later job. This module only wires the two GIS walking modes. The UI
labels "Fastest" and "Least Walking" both map to the GIS *Walking* travel mode;
`resolve_ui_mode()` records that they are walking-only in this build so the
frontend cannot imply a multimodal comparison that was not computed.

HONESTY / PERMISSION RULES BAKED IN
-----------------------------------
1. Attribution is mandatory on public reads. Every result carries a
   `Provenance` with source + disclaimer.
2. Persistence / export / snapshot is REFUSED unless the caller supplies a
   capability tied to a written permission reference; public readability is not
   a redistribution grant, so nothing derived is exported or committed.
   Runtime reads are allowed; nothing in this module ships a real GIS fixture.
3. `totalTime` from the NAServer Route solve is 0, so GIS travel time is never
   claimed. Estimated minutes are computed ONLY from a declared walking speed
   and are labelled an estimate.
4. Caller-supplied coordinates are never labelled authoritative.
5. Closures are parsed and exposed, but this build does NOT feed them to the
   route solve, so results never claim closure avoidance.
6. No credentials. All requests are anonymous.
"""
from __future__ import annotations

import json
import math
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from . import cache, config

__all__ = [
    # constants
    "GIS_BASE", "BUILDINGS_LAYER", "ACCESSIBILITY_ENTRANCES_LAYER",
    "ROUTE_SERVICE", "CONSTRUCTION_CLOSURES_CURRENT",
    "CONSTRUCTION_CLOSURES_SCHEDULED",
    "CAMPUS_CLOSURE_ROADS", "CAMPUS_CLOSURE_AREAS",
    "ATTRIBUTION", "DISCLAIMER",
    "TRAVEL_MODE_WALKING", "TRAVEL_MODE_ADA", "TRAVEL_MODES",
    "MODE_WALKING", "MODE_ADA_WALKING", "ROUTE_MODES",
    "UI_MODE_FASTEST", "UI_MODE_LEAST_WALKING", "UI_MODE_ADA",
    "UI_MODE_OPTIONS", "FALLBACK_CONTRACT",
    "STATUS_OK", "STATUS_UNAVAILABLE", "STATUS_NO_ROUTE",
    "RedistributionNotPermitted", "VTGISError",
    # models
    "RedistributionPermit", "Provenance", "Building", "Entrance",
    "BuildingDetail", "RouteEndpoint",
    "DirectionsStep", "Route", "NoRoute", "Unavailable", "Closure",
    # query builders
    "escape_sql_literal", "build_where_id", "build_building_search_where",
    "build_where_bldg_ids", "build_query_params", "build_query_url",
    "build_solve_form",
    # geometry / distance
    "projected_2284_to_wgs84", "wgs84_to_projected_2284",
    "normalize_geometry_to_wgs84", "normalize_polygon_rings",
    "feet_to_meters", "meters_to_feet", "haversine_m", "polyline_length_m",
    "estimate_walk_minutes",
    # normalizers
    "normalize_buildings", "normalize_entrances", "normalize_route",
    "normalize_directions", "normalize_closures",
    # API
    "search_buildings", "building_by_id", "building_detail",
    "entrances_for_building", "solve_route", "route_between_buildings",
    "closures", "effective_closures", "closures_for_day",
    "walk_fallback", "unavailable_fallback",
    # permission gate
    "authorize_redistribution", "assert_redistribution_allowed", "route_to_geojson",
    "buildings_to_geojson", "snapshot_payload",
]

# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
# Verified anonymous + CORS-readable 2026-09. Base is VT Enterprise GIS.
GIS_BASE = "https://arcgis-central.gis.vt.edu/arcgis/rest/services"
BUILDINGS_LAYER = f"{GIS_BASE}/vtcampusmap/Buildings/MapServer/0"
ACCESSIBILITY_ENTRANCES_LAYER = f"{GIS_BASE}/vtcampusmap/Accessibility/MapServer/3"
ROUTE_SERVICE = f"{GIS_BASE}/facilities/MultimodalRouting_2024_12_13/NAServer/Route"
CONSTRUCTION_CLOSURES_CURRENT = f"{GIS_BASE}/facilities/Construction_Closures/MapServer/0"
CONSTRUCTION_CLOSURES_SCHEDULED = f"{GIS_BASE}/facilities/Construction_Closures/MapServer/1"
CAMPUS_CLOSURE_ROADS = f"{GIS_BASE}/facilities/CampusClosureLayers/MapServer/0"
CAMPUS_CLOSURE_AREAS = f"{GIS_BASE}/facilities/CampusClosureLayers/MapServer/1"

ATTRIBUTION = (
    "Virginia Tech Campus Planning, Infrastructure, and Facilities (CPIF) GIS "
    "(arcgis-central.gis.vt.edu)"
)
DISCLAIMER = (
    "Official VT GIS campus data, used for wayfinding. Not a survey product; "
    "distances are network-path lengths, not travel time. Verify closures and "
    "accessible routes on site."
)

# Native source CRS is NAD83 / Virginia South (ftUS). We always request
# outSR=4326, but a normalizer must still recognize a native 2284 response.
WGS84_WKID = 4326
NAD83_GEOGRAPHIC_WKID = 4269
VT_SOUTH_WKID = 2284
VT_SOUTH_WKID_LEGACY = 102747

# --------------------------------------------------------------------------- #
# Canonical travel modes (verbatim from the Route service metadata)
# --------------------------------------------------------------------------- #
# The ADA mode needs the FULL travelMode object: the restriction is carried by
# `restrictionAttributeNames=["ADA_only"]` plus the attribute parameter value.
# A bare name would silently solve the non-accessible network.
TRAVEL_MODE_WALKING = {
    "name": "Walking",
    "itemId": "1",
    "type": "WALK",
    "description": ("Finds the shortest walking path between the starting point "
                    "and destination.  NOTE: routes may include stairs or slopes "
                    "greater than 5%.  For accessible routes, please use the "
                    "\"ADA Routes Only\" travel mode. "),
    "timeAttributeName": "",
    "distanceAttributeName": "Length",
    "impedanceAttributeName": "Length",
    "restrictionAttributeNames": [],
    "attributeParameterValues": [
        {"attributeName": "ADA_only", "parameterName": "Restriction Usage",
         "value": "Prohibited"},
    ],
    "useHierarchy": False,
    "uturnAtJunctions": "esriNFSBAllowBacktrack",
    "simplificationTolerance": None,
    "simplificationToleranceUnits": "esriMeters",
}
TRAVEL_MODE_ADA = {
    "name": "ADA Routes Only",
    "itemId": "2",
    "type": "OTHER",
    "description": ("Finds the shortest distance between start and end points "
                    "using ONLY fully accessible pathways."),
    "timeAttributeName": "",
    "distanceAttributeName": "Length",
    "impedanceAttributeName": "Length",
    "restrictionAttributeNames": ["ADA_only"],
    "attributeParameterValues": [
        {"attributeName": "ADA_only", "parameterName": "Restriction Usage",
         "value": "Prohibited"},
    ],
    "useHierarchy": False,
    "uturnAtJunctions": "esriNFSBAllowBacktrack",
    "simplificationTolerance": None,
    "simplificationToleranceUnits": "esriMeters",
}

MODE_WALKING = "walking"
MODE_ADA_WALKING = "ada_walking"
ROUTE_MODES = (MODE_WALKING, MODE_ADA_WALKING)
TRAVEL_MODES = {
    MODE_WALKING: TRAVEL_MODE_WALKING,
    MODE_ADA_WALKING: TRAVEL_MODE_ADA,
}

# UI labels. "Fastest" and "Least Walking" are BOTH walking-only here; true
# multimodal ranking is future work (see module docstring). Exposing them as a
# data structure keeps the frontend from inventing a false promise.
UI_MODE_FASTEST = "fastest"
UI_MODE_LEAST_WALKING = "least_walking"
UI_MODE_ADA = "ada_walking"
UI_MODE_OPTIONS = (
    {
        "ui_mode": UI_MODE_FASTEST,
        "label": "Fastest",
        "route_mode": MODE_WALKING,
        "multimodal": False,
        "note": ("Walking geometry only in this build; multimodal ranking "
                 "(GIS walk + GTFS/live bus) is not implemented here."),
    },
    {
        "ui_mode": UI_MODE_LEAST_WALKING,
        "label": "Least Walking",
        "route_mode": MODE_WALKING,
        "multimodal": False,
        "note": ("Shortest walking path; bus legs are not combined in this "
                 "build."),
    },
    {
        "ui_mode": UI_MODE_ADA,
        "label": "ADA Walking",
        "route_mode": MODE_ADA_WALKING,
        "multimodal": False,
        "note": "Fully accessible pathways only (ADA_only restriction).",
    },
)

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NO_ROUTE = "no_route"

# Offline / degraded contract. When GIS is unavailable, these are the only
# honest fallbacks this repo already owns.
FALLBACK_CONTRACT = {
    "status": STATUS_UNAVAILABLE,
    "walk": ("hokieday.tools.walk_time: haversine x config.WALK_PATH_FACTOR at "
             "config.WALK_SPEED_MPS; STRAIGHT-LINE estimate, NOT a routed path"),
    "map": ("app.mapview.build_map_svg: straight dashed walk legs; GTFS bus legs "
            "are real shape geometry"),
    "routed_geometry": None,
    "route_directions": None,
    "closure_awareness": None,
}

FT_TO_M = 0.3048  # exact international foot; US survey foot differs ~2 ppm and
# the network attribute reports feet for a short campus path, so the practical
# difference is sub-centimetre and irrelevant to wayfinding.

_LAYER_MAX_AGE_S = 6 * 3600.0
_ROUTE_MAX_AGE_S = 900.0
_CLOSURE_MAX_AGE_S = 1800.0
_ARCGIS_MAX_RECORD_COUNT = 2000
_BUILDING_SEARCH_MAX = 200

_CAMPUS_TZ = ZoneInfo(config.CAMPUS_TZ)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class VTGISError(RuntimeError):
    """Malformed or unusable VT GIS payload."""


class RedistributionNotPermitted(RuntimeError):
    """Raised when persistence/export/snapshot is attempted without permission.

    The VT GIS terms do not explicitly grant redistribution, so the safe default
    is to refuse. Runtime public reads (with attribution) are unaffected.
    """


@dataclass(frozen=True)
class RedistributionPermit:
    """Capability proving a caller recorded written redistribution permission.

    This token deliberately cannot be replaced by a casual boolean. The
    reference should identify the written grant (for example a ticket or signed
    agreement), not contain the GIS data or any credential.
    """

    written_permission_reference: str


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Provenance:
    """Where a result came from, and the constraints that travel with it."""

    source: str
    service: str
    url: str
    attribution: str = ATTRIBUTION
    disclaimer: str = DISCLAIMER
    redistribution_allowed: bool = False
    fetched_at: str | None = None
    cache_hit: bool | None = None

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "service": self.service,
            "url": self.url,
            "attribution": self.attribution,
            "disclaimer": self.disclaimer,
            "redistribution_allowed": self.redistribution_allowed,
            "fetched_at": self.fetched_at,
            "cache_hit": self.cache_hit,
        }


def _provenance(service: str, url: str, *, cache_hit: bool | None = None) -> Provenance:
    return Provenance(
        source="Virginia Tech Enterprise GIS",
        service=service,
        url=url,
        cache_hit=cache_hit,
        redistribution_allowed=False,
    )


# --------------------------------------------------------------------------- #
# Typed results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Unavailable:
    """Typed failure: the service could not be read (offline, HTTP, malformed).

    Callers must treat this as "no answer", never as zero distance / no route.
    """

    status: str
    service: str
    reason: str
    detail: str | None = None
    fallback: dict | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "service": self.service,
            "reason": self.reason,
            "detail": self.detail,
            "fallback": self.fallback,
        }


@dataclass(frozen=True)
class NoRoute:
    """Typed result: the solve ran and the network has no path for this mode.

    For ADA this is a legitimate, expected answer (no fully accessible path),
    not an error.
    """

    status: str
    mode: str
    reason: str
    detail: str | None
    provenance: Provenance
    from_endpoint: "RouteEndpoint | None" = None
    to_endpoint: "RouteEndpoint | None" = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "mode": self.mode,
            "reason": self.reason,
            "detail": self.detail,
            "from": self.from_endpoint.as_dict() if self.from_endpoint else None,
            "to": self.to_endpoint.as_dict() if self.to_endpoint else None,
            "provenance": self.provenance.as_dict(),
        }


@dataclass(frozen=True)
class RouteEndpoint:
    lat: float
    lon: float
    label: str | None = None
    authoritative: bool = False  # True ONLY for an official VT GIS feature point
    building_id: str | None = None
    entrance_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "lat": self.lat, "lon": self.lon, "label": self.label,
            "authoritative": self.authoritative,
            "building_id": self.building_id, "entrance_id": self.entrance_id,
            "coordinate_basis": ("official VT GIS feature" if self.authoritative
                                 else "caller-supplied (NOT authoritative)"),
        }


@dataclass(frozen=True)
class Building:
    building_id: str
    name: str
    bldg_num: str | None
    vtes_bldg_num: str | None
    bldg_use: str | None
    status: str | None
    lat: float | None
    lon: float | None
    coords_authoritative: bool
    object_id: int | None
    url: str | None
    provenance: Provenance
    geometry: tuple[tuple[float, float], ...] = ()   # WGS84 outer ring (lat, lon)

    def as_dict(self) -> dict:
        return {
            "building_id": self.building_id,
            "name": self.name,
            "bldg_num": self.bldg_num,
            "vtes_bldg_num": self.vtes_bldg_num,
            "bldg_use": self.bldg_use,
            "status": self.status,
            "lat": self.lat,
            "lon": self.lon,
            "coords_authoritative": self.coords_authoritative,
            "object_id": self.object_id,
            "url": self.url,
            "geometry": [list(p) for p in self.geometry],
            "provenance": self.provenance.as_dict(),
        }


@dataclass(frozen=True)
class Entrance:
    entrance_id: str
    building_id: str
    lat: float
    lon: float
    accessible: bool
    automatic_door: bool
    entrance_type: str | None
    ada: str | None
    floor: int | None
    object_id: int | None
    provenance: Provenance

    def as_dict(self) -> dict:
        return {
            "entrance_id": self.entrance_id,
            "building_id": self.building_id,
            "lat": self.lat, "lon": self.lon,
            "accessible": self.accessible,
            "automatic_door": self.automatic_door,
            "entrance_type": self.entrance_type,
            "ada": self.ada,
            "floor": self.floor,
            "object_id": self.object_id,
            "provenance": self.provenance.as_dict(),
        }


@dataclass(frozen=True)
class BuildingDetail:
    building: Building
    entrances: tuple[Entrance, ...]
    accessible_entrance_count: int

    def as_dict(self) -> dict:
        return {
            "building": self.building.as_dict(),
            "entrances": [e.as_dict() for e in self.entrances],
            "accessible_entrance_count": self.accessible_entrance_count,
        }


@dataclass(frozen=True)
class DirectionsStep:
    index: int
    text: str
    distance_ft: float | None
    distance_m: float | None
    time_min: float | None
    time_source: str  # "gis" or "unavailable"

    def as_dict(self) -> dict:
        return {
            "index": self.index, "text": self.text,
            "distance_ft": self.distance_ft, "distance_m": self.distance_m,
            "time_min": self.time_min, "time_source": self.time_source,
        }


@dataclass(frozen=True)
class Route:
    status: str
    mode: str
    ui_mode: str | None
    from_endpoint: RouteEndpoint
    to_endpoint: RouteEndpoint
    distance_ft: float | None
    distance_m: float | None
    geometry: tuple[tuple[float, float], ...]   # WGS84 (lat, lon)
    directions: tuple[DirectionsStep, ...]
    estimated_minutes: float | None
    estimated_minutes_label: str | None
    gis_travel_time_available: bool
    distance_source: str
    closures_considered: bool
    provenance: Provenance
    notes: tuple[str, ...] = ()

    @property
    def is_estimate(self) -> bool:
        return self.estimated_minutes is not None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "mode": self.mode,
            "ui_mode": self.ui_mode,
            "from": self.from_endpoint.as_dict(),
            "to": self.to_endpoint.as_dict(),
            "distance_ft": self.distance_ft,
            "distance_m": self.distance_m,
            "distance_source": self.distance_source,
            "geometry": [list(p) for p in self.geometry],
            "directions": [d.as_dict() for d in self.directions],
            "estimated_minutes": self.estimated_minutes,
            "estimated_minutes_label": self.estimated_minutes_label,
            "gis_travel_time_available": self.gis_travel_time_available,
            "closures_considered": self.closures_considered,
            "provenance": self.provenance.as_dict(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Closure:
    closure_id: str
    name: str
    start: datetime | None
    end: datetime | None
    project_type: str | None
    comments: str | None
    url: str | None
    layer: str
    source_label: str
    provenance: Provenance
    geometry: tuple[tuple[float, float], ...] = ()

    def interval(self) -> tuple[datetime | None, datetime | None]:
        return (self.start, self.end)

    def is_effective_at(self, at: datetime | None = None) -> bool:
        """True when `at` (default campus now) falls in the closure interval.

        Open-ended start/end are handled: a missing bound does not make the
        closure ineffective on that side.
        """
        ref = _as_campus_local(at if at is not None else config.now())
        if self.start is not None and ref < self.start:
            return False
        if self.end is not None and ref > self.end:
            return False
        return True

    def as_dict(self) -> dict:
        return {
            "closure_id": self.closure_id,
            "name": self.name,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "project_type": self.project_type,
            "comments": self.comments,
            "url": self.url,
            "layer": self.layer,
            "source_label": self.source_label,
            "geometry": [list(point) for point in self.geometry],
            "provenance": self.provenance.as_dict(),
        }


# --------------------------------------------------------------------------- #
# Permission gate
# --------------------------------------------------------------------------- #
def authorize_redistribution(
        written_permission_reference: str) -> RedistributionPermit:
    """Create an export capability after written permission has been recorded.

    Readable public endpoints are not themselves permission. The non-blank
    reference must point to a written grant retained by the caller; this module
    intentionally does not offer a boolean bypass.
    """
    reference = str(written_permission_reference or "").strip()
    if len(reference) < 8:
        raise RedistributionNotPermitted(
            "A non-blank reference to written VT GIS redistribution permission "
            "is required (for example, a permission ticket or agreement ID)."
        )
    return RedistributionPermit(reference)


def assert_redistribution_allowed(
        permission: RedistributionPermit | None = None) -> None:
    """Raise unless a written-permission capability was explicitly supplied."""
    if not isinstance(permission, RedistributionPermit):
        raise RedistributionNotPermitted(
            "Refusing to persist/export/snapshot VT GIS data: public read access "
            "does not grant redistribution. Obtain written permission and pass "
            "authorize_redistribution(<permission reference>)."
        )
    # Reject tokens manually constructed with an empty/placeholder reference.
    authorize_redistribution(permission.written_permission_reference)


# --------------------------------------------------------------------------- #
# Projection: NAD83 / Virginia South (ftUS) <-> geographic
# --------------------------------------------------------------------------- #
# EPSG:2284, Lambert Conformal Conic (2SP) on GRS80. outSR=4326 is requested on
# every read, but a native 2284 response must still be normalisable, so the
# inverse is implemented here in stdlib. Values match EPSG:2284 WKT.
_A = 6378137.0
_F = 1.0 / 298.257222101
_E2 = _F * (2.0 - _F)
_E = math.sqrt(_E2)
_LAT0 = math.radians(36.3333333333333)
_LON0 = math.radians(-78.5)
_LAT1 = math.radians(37.9666666666667)
_LAT2 = math.radians(36.7666666666667)
_X0_FT = 11482916.667
_Y0_FT = 3280833.333
_US_FT = 0.304800609601219


def _lcc_m(phi: float) -> float:
    return math.cos(phi) / math.sqrt(1.0 - _E2 * math.sin(phi) ** 2)


def _lcc_t(phi: float) -> float:
    return (math.tan(math.pi / 4.0 - phi / 2.0)
            / ((1.0 - _E * math.sin(phi)) / (1.0 + _E * math.sin(phi))) ** (_E / 2.0))


_LCC_N = (math.log(_lcc_m(_LAT1)) - math.log(_lcc_m(_LAT2))) / (
    math.log(_lcc_t(_LAT1)) - math.log(_lcc_t(_LAT2)))
_LCC_F = _lcc_m(_LAT1) / (_LCC_N * _lcc_t(_LAT1) ** _LCC_N)
_LCC_RHO0 = _A * _LCC_F * _lcc_t(_LAT0) ** _LCC_N


def projected_2284_to_wgs84(x_ft: float, y_ft: float) -> tuple[float, float]:
    """(easting_ft, northing_ft) in NAD83 / VA South -> (lat, lon) degrees."""
    x = float(x_ft) * _US_FT
    y = float(y_ft) * _US_FT
    rho = math.hypot(x - _X0_FT * _US_FT, _LCC_RHO0 - (y - _Y0_FT * _US_FT))
    if rho == 0:
        return math.degrees(_LAT0), math.degrees(_LON0)
    theta = math.atan2(x - _X0_FT * _US_FT, _LCC_RHO0 - (y - _Y0_FT * _US_FT))
    t = (rho / (_A * _LCC_F)) ** (1.0 / _LCC_N)
    phi = math.pi / 2.0 - 2.0 * math.atan(t)
    for _ in range(12):
        phi = math.pi / 2.0 - 2.0 * math.atan(
            t * ((1.0 - _E * math.sin(phi)) / (1.0 + _E * math.sin(phi))) ** (_E / 2.0))
    lon = _LON0 + theta / _LCC_N
    return math.degrees(phi), math.degrees(lon)


def wgs84_to_projected_2284(lat: float, lon: float) -> tuple[float, float]:
    """(lat, lon) degrees -> (easting_ft, northing_ft). Inverse of the above."""
    phi = math.radians(float(lat))
    lam = math.radians(float(lon))
    rho = _A * _LCC_F * _lcc_t(phi) ** _LCC_N
    theta = _LCC_N * (lam - _LON0)
    x = _X0_FT * _US_FT + rho * math.sin(theta)
    y = _Y0_FT * _US_FT + _LCC_RHO0 - rho * math.cos(theta)
    return x / _US_FT, y / _US_FT


# --------------------------------------------------------------------------- #
# Geometry normalisation
# --------------------------------------------------------------------------- #
def _finite(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _xy(value: Any) -> tuple[float, float] | None:
    """A finite coordinate pair, ignoring optional z/m ordinates."""
    if (not isinstance(value, (list, tuple)) or len(value) < 2
            or not _finite(value[0]) or not _finite(value[1])):
        return None
    return float(value[0]), float(value[1])


def _wkid(sr: dict | None) -> int | None:
    if not isinstance(sr, dict):
        return None
    for k in ("latestWkid", "latestwkid", "wkid", "id"):
        if _finite(sr.get(k)):
            return int(sr[k])
    return None


def _convert_xy(x: float, y: float, wkid: int | None) -> tuple[float, float]:
    """(x, y) in the declared CRS -> (lat, lon) WGS84 degrees."""
    if wkid in (VT_SOUTH_WKID, VT_SOUTH_WKID_LEGACY):
        return projected_2284_to_wgs84(x, y)
    if wkid in (WGS84_WKID, NAD83_GEOGRAPHIC_WKID, None):
        # ArcGIS x=lon, y=lat. NAD83 vs WGS84 differ ~1 m, below our concern.
        return float(y), float(x)
    raise VTGISError(f"unsupported geometry spatial reference wkid={wkid}")


def normalize_geometry_to_wgs84(geometry: Any,
                                spatial_reference: dict | None = None) -> list[tuple[float, float]]:
    """Any ArcGIS or GeoJSON geometry -> flat [(lat, lon), ...] WGS84.

    Point, MultiPoint, LineString/MultiLineString and Polygon/MultiPolygon
    nesting is handled explicitly. Malformed vertices are skipped rather than
    causing a shape/index/type error. An unsupported declared CRS still raises
    because silently treating projected numbers as degrees would be dangerous.
    """
    if not isinstance(geometry, dict) or not geometry:
        return []

    # GeoJSON (RFC 7946): coordinates are [lon, lat] in WGS84. Accept the short
    # Line/MultiLine aliases too because lightweight callers commonly use them.
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    geojson_types = {
        "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon",
        "MultiPolygon", "Line", "MultiLine",
    }
    raw: list[tuple[float, float]] = []
    if gtype in geojson_types:
        if gtype == "Point":
            pair = _xy(coords)
            if pair:
                raw.append(pair)
        elif gtype == "MultiPoint":
            for vertex in coords if isinstance(coords, (list, tuple)) else ():
                pair = _xy(vertex)
                if pair:
                    raw.append(pair)
        elif gtype in ("LineString", "Line"):
            for vertex in coords if isinstance(coords, (list, tuple)) else ():
                pair = _xy(vertex)
                if pair:
                    raw.append(pair)
        elif gtype in ("MultiLineString", "MultiLine"):
            for line in coords if isinstance(coords, (list, tuple)) else ():
                for vertex in line if isinstance(line, (list, tuple)) else ():
                    pair = _xy(vertex)
                    if pair:
                        raw.append(pair)
        elif gtype == "Polygon":
            for ring in coords if isinstance(coords, (list, tuple)) else ():
                for vertex in ring if isinstance(ring, (list, tuple)) else ():
                    pair = _xy(vertex)
                    if pair:
                        raw.append(pair)
        elif gtype == "MultiPolygon":
            for polygon in coords if isinstance(coords, (list, tuple)) else ():
                for ring in polygon if isinstance(polygon, (list, tuple)) else ():
                    for vertex in ring if isinstance(ring, (list, tuple)) else ():
                        pair = _xy(vertex)
                        if pair:
                            raw.append(pair)
        return [_convert_xy(x, y, WGS84_WKID) for x, y in raw]

    wkid = _wkid(geometry.get("spatialReference")) or _wkid(spatial_reference)
    pair = _xy((geometry.get("x"), geometry.get("y")))
    if pair:
        raw.append(pair)
    points = geometry.get("points")
    for vertex in points if isinstance(points, (list, tuple)) else ():
        pair = _xy(vertex)
        if pair:
            raw.append(pair)
    for key in ("paths", "rings"):
        parts = geometry.get(key)
        for part in parts if isinstance(parts, (list, tuple)) else ():
            for vertex in part if isinstance(part, (list, tuple)) else ():
                pair = _xy(vertex)
                if pair:
                    raw.append(pair)

    return [_convert_xy(x, y, wkid) for x, y in raw]


def normalize_polygon_rings(geometry: Any,
                            spatial_reference: dict | None = None
                            ) -> list[list[tuple[float, float]]]:
    """Polygon rings -> [[(lat, lon), ...], ...] WGS84.

    MultiPolygon boundaries are returned in source order as a flat ring list;
    malformed vertices/rings are omitted.
    """
    if not isinstance(geometry, dict):
        return []
    gtype = geometry.get("type")
    source_rings: list[Any] = []
    wkid = WGS84_WKID
    if gtype == "Polygon":
        coords = geometry.get("coordinates")
        source_rings = list(coords) if isinstance(coords, (list, tuple)) else []
    elif gtype == "MultiPolygon":
        coords = geometry.get("coordinates")
        if isinstance(coords, (list, tuple)):
            for polygon in coords:
                if isinstance(polygon, (list, tuple)):
                    source_rings.extend(polygon)
    else:
        arc_rings = geometry.get("rings")
        if not isinstance(arc_rings, (list, tuple)):
            return []
        source_rings = list(arc_rings)
        wkid = (_wkid(geometry.get("spatialReference"))
                or _wkid(spatial_reference))

    rings: list[list[tuple[float, float]]] = []
    for ring in source_rings:
        converted: list[tuple[float, float]] = []
        for vertex in ring if isinstance(ring, (list, tuple)) else ():
            pair = _xy(vertex)
            if pair:
                converted.append(_convert_xy(pair[0], pair[1], wkid))
        if converted:
            rings.append(converted)
    return rings


def _ring_center(ring: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    if not ring:
        return None
    lats = [p[0] for p in ring]
    lons = [p[1] for p in ring]
    return ((min(lats) + max(lats)) / 2.0, (min(lons) + max(lons)) / 2.0)


# --------------------------------------------------------------------------- #
# Distance
# --------------------------------------------------------------------------- #
def feet_to_meters(feet: float) -> float:
    return float(feet) * FT_TO_M


def meters_to_feet(meters: float) -> float:
    return float(meters) / FT_TO_M


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle metres between (lat, lon) pairs."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * 6_371_000.0 * math.asin(math.sqrt(h))


def polyline_length_m(points: Sequence[tuple[float, float]]) -> float:
    return sum(haversine_m(points[i], points[i + 1])
               for i in range(len(points) - 1))


def estimate_walk_minutes(distance_m: float,
                          speed_mps: float | None = None) -> tuple[float, str]:
    """(minutes, label) for a *declared* walking speed. Always an estimate."""
    speed = float(speed_mps if speed_mps is not None else config.WALK_SPEED_MPS)
    if speed <= 0:
        raise ValueError("walking speed must be > 0")
    minutes = float(distance_m) / speed / 60.0
    label = (f"ESTIMATE: {distance_m:.0f} m / {speed:.2f} m/s declared walking "
             f"speed. VT GIS returned no travel time.")
    return round(minutes, 1), label


def _as_campus_local(dt: datetime | None) -> datetime:
    if dt is None:
        return config.now(_CAMPUS_TZ)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_CAMPUS_TZ)
    return dt.astimezone(_CAMPUS_TZ)


# --------------------------------------------------------------------------- #
# Query builders
# --------------------------------------------------------------------------- #
def escape_sql_literal(value: Any) -> str:
    """Single-quoted ArcGIS SQL literal. A quote is doubled; nothing else is."""
    return "'" + str(value).replace("'", "''") + "'"


def _strip_like_wildcards(text: str) -> str:
    # We intentionally drop LIKE metacharacters rather than rely on an ESCAPE
    # clause (support varies across ArcGIS versions). Building names never need
    # a literal % or _ to be found.
    return text.replace("%", "").replace("_", "")


def _building_id_variants(building_id: Any) -> list[str]:
    raw = str(building_id).strip()
    variants = {raw}
    canonical = canonical_building_id(raw)
    if canonical and canonical.isdigit():
        # Real services use different fixed-width strings: building bldg_num is
        # commonly 4 digits, vtes_bldg_num 6, and entrance bldg_id 5.
        variants.update({canonical, canonical.zfill(4), canonical.zfill(6)})
    return sorted(v for v in variants if v)


def build_where_id(building_id: Any) -> str:
    variants = _building_id_variants(building_id)
    if not variants:
        return "1=2"
    clauses = []
    for value in variants:
        literal = escape_sql_literal(value)
        clauses.extend((f"bldg_num = {literal}", f"vtes_bldg_num = {literal}"))
    return " OR ".join(clauses)


def build_building_search_where(query: Any) -> str:
    q = _strip_like_wildcards(str(query).strip())
    if not q:
        return "1=2"
    like = escape_sql_literal(f"%{q}%")
    return f"UPPER(name) LIKE UPPER({like}) OR {build_where_id(q)}"


def build_where_bldg_ids(values: Sequence[Any]) -> str:
    lits = sorted({escape_sql_literal(v) for v in values if v not in (None, "")})
    if not lits:
        return "1=2"
    return "bldg_id IN (" + ",".join(lits) + ")"


def build_query_params(*, where: str = "1=1", out_fields: str = "*",
                       out_sr: int = WGS84_WKID, return_geometry: bool = True,
                       result_record_count: int | None = None,
                       order_by: str | None = None,
                       return_distinct: bool = False) -> dict[str, str]:
    """Deterministic ArcGIS FeatureServer/MapServer query parameters."""
    params: dict[str, str] = {
        "f": "json",
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
        "outSR": str(out_sr),
    }
    if result_record_count is not None:
        try:
            count = int(result_record_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("result_record_count must be an integer") from exc
        if count < 1:
            raise ValueError("result_record_count must be at least 1")
        params["resultRecordCount"] = str(min(count, _ARCGIS_MAX_RECORD_COUNT))
    if order_by:
        params["orderByFields"] = order_by
    if return_distinct:
        params["returnDistinctValues"] = "true"
    return params


def build_query_url(layer_url: str, params: dict[str, str]) -> str:
    return f"{layer_url}/query?{urllib.parse.urlencode(params)}"


def _point_geometry(lat: float, lon: float) -> dict:
    # Endpoint values accepted by this API are always WGS84. `outSR` controls
    # only the solve response and must never relabel these input coordinates.
    return {"x": float(lon), "y": float(lat),
            "spatialReference": {"wkid": WGS84_WKID}}


def build_solve_form(from_endpoint: "RouteEndpoint", to_endpoint: "RouteEndpoint",
                     mode: str = MODE_WALKING, *, out_sr: int = WGS84_WKID,
                     return_directions: bool = True,
                     directions_language: str = "en",
                     directions_length_units: str = "esriNAUFeet",
                     accumulate: Sequence[str] = ("Length",)) -> dict:
    """Full ArcGIS NAServer `solve` form body for one origin->destination.

    The full travelMode object is required: the ADA route only happens when
    `restrictionAttributeNames=["ADA_only"]` is present. Sending a name alone
    silently solves the unrestricted walking network.
    """
    tm = TRAVEL_MODES[validate_mode(mode)]
    stops = {"features": [
        {"attributes": {"Name": from_endpoint.label or "Origin"},
         "geometry": _point_geometry(from_endpoint.lat, from_endpoint.lon)},
        {"attributes": {"Name": to_endpoint.label or "Destination"},
         "geometry": _point_geometry(to_endpoint.lat, to_endpoint.lon)},
    ]}
    form = {
        "f": "json",
        "stops": json.dumps(stops, separators=(",", ":")),
        "travelMode": json.dumps(tm, separators=(",", ":")),
        "outSR": str(out_sr),
        "returnRoutes": "true",
        "returnDirections": "true" if return_directions else "false",
        "returnStops": "false",
        "returnBarriers": "false",
        "directionsLanguage": directions_language,
        "directionsLengthUnits": directions_length_units,
    }
    if accumulate:
        form["accumulateAttributeNames"] = ",".join(accumulate)
    return form


def validate_mode(mode: Any) -> str:
    """Return a canonical route mode or raise ValueError for an unknown one."""
    m = str(mode).strip().lower()
    aliases = {
        "walking": MODE_WALKING, "walk": MODE_WALKING,
        UI_MODE_FASTEST: MODE_WALKING, UI_MODE_LEAST_WALKING: MODE_WALKING,
        "ada": MODE_ADA_WALKING, "ada_walking": MODE_ADA_WALKING,
        UI_MODE_ADA: MODE_ADA_WALKING, "accessible": MODE_ADA_WALKING,
    }
    if m not in aliases:
        raise ValueError(f"unknown route mode {mode!r}; expected one of {ROUTE_MODES}")
    return aliases[m]


def resolve_ui_mode(ui_mode: Any) -> dict:
    """Map a UI mode label to its option descriptor (walking-only in this build)."""
    m = str(ui_mode).strip().lower()
    for opt in UI_MODE_OPTIONS:
        if opt["ui_mode"] == m:
            return opt
    raise ValueError(f"unknown UI mode {ui_mode!r}")


# --------------------------------------------------------------------------- #
# Generic cached read
# --------------------------------------------------------------------------- #
def _query_layer(service: str, layer_url: str, name: str, params: dict,
                 *, key_params: dict | None = None,
                 max_age_s: float = _LAYER_MAX_AGE_S,
                 force: bool = False) -> Any | Unavailable:
    # `params` becomes the request URL; `key_params` (when given) becomes the
    # cache key. They are separate because two different layers can share an
    # identical query string (e.g. current vs scheduled closures both use
    # where=1=1); without a layer discriminator they would collide on one
    # envelope and return the wrong layer's data.
    url = build_query_url(layer_url, params)
    cache_key = key_params if key_params is not None else params
    try:
        payload = cache.get_json(name, url, params=cache_key,
                                 max_age_s=max_age_s, force=force)
        error = _payload_error(payload)
        if error:
            return Unavailable(STATUS_UNAVAILABLE, service,
                               "GIS query returned an error", error,
                               FALLBACK_CONTRACT)
        return payload
    except cache.CacheMiss as exc:
        return Unavailable(STATUS_UNAVAILABLE, service,
                           "offline: no cached GIS response", str(exc),
                           FALLBACK_CONTRACT)
    except cache.CacheRefreshError as exc:
        return Unavailable(STATUS_UNAVAILABLE, service,
                           "offline: refresh suppressed after recent failure",
                           str(exc), FALLBACK_CONTRACT)
    except Exception as exc:  # noqa: BLE001 - a dead source must degrade
        return Unavailable(STATUS_UNAVAILABLE, service,
                           "GIS request failed", f"{type(exc).__name__}: {exc}",
                           FALLBACK_CONTRACT)


def _payload_error(payload: Any) -> str | None:
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        e = payload["error"]
        return f"{e.get('code')}: {e.get('message')}"
    return None


# --------------------------------------------------------------------------- #
# Normalizers
# --------------------------------------------------------------------------- #
def canonical_building_id(value: Any) -> str | None:
    """Stable join key: trimmed, upper-cased, leading zeros removed.

    The building layer's `bldg_num`/`vtes_bldg_num` and the entrance layer's
    `bldg_id` are differently zero-padded, so comparing raw strings misses the
    join. Canonicalising makes the join deterministic.
    """
    if value is None:
        return None
    s = str(value).strip().upper()
    if not s:
        return None
    s = s.lstrip("0")
    return s or "0"


def _attr(attrs: dict, *names: str) -> Any:
    if not isinstance(attrs, dict):
        return None
    lower = {str(k).lower(): v for k, v in attrs.items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not _finite(value):
        return None
    return float(value)


def _int_or_none(value: Any) -> int | None:
    v = _num(value)
    return int(v) if v is not None else None


def normalize_buildings(payload: Any, *,
                        provenance: Provenance | None = None) -> list[Building]:
    """ArcGIS `features[]` -> Building list. Malformed rows are skipped."""
    prov = provenance or _provenance("Buildings", BUILDINGS_LAYER)
    if not isinstance(payload, dict):
        raise VTGISError("buildings payload is not an object")
    features = payload.get("features") or []
    if not isinstance(features, list):
        raise VTGISError("buildings payload has no features list")
    top_sr = payload.get("spatialReference")
    out: list[Building] = []
    for f in features:
        if not isinstance(f, dict):
            continue
        attrs = f.get("attributes") or {}
        geom = f.get("geometry")
        name = _attr(attrs, "name")
        bldg_num = _attr(attrs, "bldg_num")
        vtes = _attr(attrs, "vtes_bldg_num")
        object_id = _int_or_none(_attr(attrs, "objectid"))
        canonical = canonical_building_id(bldg_num) or canonical_building_id(vtes) \
            or (f"oid:{object_id}" if object_id is not None else None)
        if canonical is None:
            continue

        # Official WGS84 attribute columns are preferred; geometry is a fallback.
        lat = _num(_attr(attrs, "latitude"))
        lon = _num(_attr(attrs, "longitude"))
        if lat is None or lon is None:
            lat = _num(_attr(attrs, "y_coord"))
            lon = _num(_attr(attrs, "x_coord"))
        rings = normalize_polygon_rings(geom, top_sr) if geom else []
        geometry: tuple[tuple[float, float], ...] = ()
        if rings:
            geometry = tuple(rings[0])
        if (lat is None or lon is None) and rings:
            center = _ring_center(rings[0])
            if center:
                lat, lon = center

        out.append(Building(
            building_id=canonical,
            name=str(name).strip() if name is not None else "",
            bldg_num=str(bldg_num) if bldg_num is not None else None,
            vtes_bldg_num=str(vtes) if vtes is not None else None,
            bldg_use=_attr(attrs, "bldg_use"),
            status=_attr(attrs, "status"),
            lat=lat, lon=lon,
            # This flag means "coordinates came from an official VT GIS feature".
            coords_authoritative=True,
            object_id=object_id,
            url=_attr(attrs, "url"),
            provenance=prov,
            geometry=geometry,
        ))
    return out


_ACCESSIBLE_TYPES = {
    "accessible entrance with automatic door",
    "accessible entrance without automatic door",
}


def normalize_entrances(payload: Any, *,
                        provenance: Provenance | None = None) -> list[Entrance]:
    """ArcGIS accessible-entrance features -> Entrance list (joined by bldg_id)."""
    prov = provenance or _provenance("Accessibility/Entrances",
                                     ACCESSIBILITY_ENTRANCES_LAYER)
    if not isinstance(payload, dict):
        raise VTGISError("entrances payload is not an object")
    features = payload.get("features") or []
    if not isinstance(features, list):
        raise VTGISError("entrances payload has no features list")
    top_sr = payload.get("spatialReference")
    out: list[Entrance] = []
    for f in features:
        if not isinstance(f, dict):
            continue
        attrs = f.get("attributes") or {}
        geom = f.get("geometry")
        coords = normalize_geometry_to_wgs84(geom, top_sr) if geom else []
        if not coords:
            continue
        lat, lon = coords[0]
        raw_id = _attr(attrs, "bldg_id")
        canonical = canonical_building_id(raw_id)
        if canonical is None:
            continue
        etype = _attr(attrs, "type")
        etype_s = str(etype).strip() if etype is not None else None
        accessible = (etype_s or "").lower() in _ACCESSIBLE_TYPES
        if not accessible and (str(_attr(attrs, "ada") or "").strip().upper() == "Y"):
            accessible = True
        automatic = "automatic door" in (etype_s or "").lower()
        entrance_id = (f"{canonical}:{_attr(attrs, 'objectid')}"
                       if _attr(attrs, "objectid") is not None
                       else f"{canonical}:{lat:.6f},{lon:.6f}")
        out.append(Entrance(
            entrance_id=str(entrance_id),
            building_id=canonical,
            lat=lat, lon=lon,
            accessible=accessible,
            automatic_door=automatic,
            entrance_type=etype_s,
            ada=(str(_attr(attrs, "ada")) if _attr(attrs, "ada") is not None else None),
            floor=_int_or_none(_attr(attrs, "floor")),
            object_id=_int_or_none(_attr(attrs, "objectid")),
            provenance=prov,
        ))
    return out


def _direction_attr_value(attrs: dict, *names: str) -> Any:
    return _attr(attrs, *names)


def normalize_directions(directions: Any) -> list[DirectionsStep]:
    """ArcGIS directions -> DirectionsStep list.

    ArcGIS may return one directions object or a list (one per route). `time` is
    often 0 from this service, so zero/absent time is unavailable rather than a
    real zero-minute leg. Length is feet because `build_solve_form` explicitly
    requests ``esriNAUFeet``.
    """
    groups = directions if isinstance(directions, list) else [directions]
    features: list = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("features"), list):
            features.extend(group["features"])
    out: list[DirectionsStep] = []
    for i, f in enumerate(features):
        if not isinstance(f, dict):
            continue
        attrs = f.get("attributes") or {}
        text = _direction_attr_value(attrs, "text", "directions", "instruction")
        if text is None:
            continue
        text = str(text).strip()
        if not text:
            continue
        length_ft = _num(_direction_attr_value(attrs, "length", "distance"))
        time_min = _num(_direction_attr_value(attrs, "time", "elapsedtime"))
        time_source = "gis"
        if time_min is None or time_min <= 0:
            time_min = None
            time_source = "unavailable"
        out.append(DirectionsStep(
            index=i,
            text=text,
            distance_ft=length_ft,
            distance_m=feet_to_meters(length_ft) if length_ft is not None else None,
            time_min=time_min,
            time_source=time_source,
        ))
    return out


def _route_error_reason(payload: dict) -> str | None:
    err = payload.get("error")
    if isinstance(err, dict):
        parts = [f"{err.get('code')}: {err.get('message')}"]
        details = err.get("details")
        if isinstance(details, list):
            parts.extend(str(item) for item in details if item)
        return "; ".join(parts)
    messages = payload.get("messages")
    if isinstance(messages, list):
        descs = [str(m.get("description")) for m in messages
                 if isinstance(m, dict) and m.get("description")]
        if descs:
            return "; ".join(descs)
    return None


def normalize_route(payload: Any, *, mode: str,
                    from_endpoint: RouteEndpoint, to_endpoint: RouteEndpoint,
                    provenance: Provenance | None = None,
                    ui_mode: str | None = None,
                    walk_speed_mps: float | None = None) -> Route | NoRoute:
    """ArcGIS NAServer solve response -> Route or typed NoRoute."""
    mode = validate_mode(mode)
    prov = provenance or _provenance("Route solve", ROUTE_SERVICE)
    if not isinstance(payload, dict):
        raise VTGISError("route payload is not an object")

    err = _route_error_reason(payload)
    if isinstance(payload.get("error"), dict):
        # A solve can legitimately report no path as an ArcGIS 400 response,
        # especially for ADA. Other ArcGIS errors are service/malformed failures
        # and must not be mislabeled as evidence that no route exists.
        no_route_markers = ("no route", "no accessible path", "unable to complete route")
        if err and any(marker in err.lower() for marker in no_route_markers):
            return NoRoute(
                status=STATUS_NO_ROUTE, mode=mode,
                reason="no route between these points for this mode",
                detail=err, provenance=prov,
                from_endpoint=from_endpoint, to_endpoint=to_endpoint,
            )
        raise VTGISError(f"route solve error: {err or 'unknown ArcGIS error'}")

    routes = payload.get("routes") or {}
    if not isinstance(routes, dict):
        raise VTGISError("route payload has no routes object")
    features = routes.get("features") or []
    if not isinstance(features, list):
        raise VTGISError("route payload has no route features list")
    if not features:
        return NoRoute(
            status=STATUS_NO_ROUTE, mode=mode,
            reason="no route between these points for this mode",
            detail=_route_error_reason(payload), provenance=prov,
            from_endpoint=from_endpoint, to_endpoint=to_endpoint,
        )

    feat = features[0]
    if not isinstance(feat, dict):
        raise VTGISError("route feature is not an object")
    attrs = feat.get("attributes") or {}
    geom = feat.get("geometry") or {}
    sr = geom.get("spatialReference") or payload.get("spatialReference")
    geometry = tuple(normalize_geometry_to_wgs84(geom, sr))

    total_ft = _num(_attr(attrs, "Total_Length", "total_length", "length"))
    distance_source = "gis_total_length_ft"
    if total_ft is None and geometry:
        total_ft = meters_to_feet(polyline_length_m(geometry))
        distance_source = "computed_from_wgs84_geometry"
    total_m = feet_to_meters(total_ft) if total_ft is not None else None

    steps = normalize_directions(payload.get("directions"))
    total_time = _num(_attr(attrs, "Total_Time", "total_time", "time"))
    # In particular, never interpret the service's sentinel Total_Time=0 as an
    # instantaneous route, even if an inconsistent direction row has a value.
    gis_time_available = total_time is not None and total_time > 0

    estimated: float | None = None
    estimate_label: str | None = None
    if total_m is not None:
        estimated, estimate_label = estimate_walk_minutes(total_m, walk_speed_mps)

    notes = [
        "VT GIS route solve has no time cost; Total_Time=0 is unavailable, "
        "not an instantaneous trip.",
        "Closures are NOT applied to this solve; do not claim avoidance.",
    ]
    return Route(
        status=STATUS_OK, mode=mode, ui_mode=ui_mode,
        from_endpoint=from_endpoint, to_endpoint=to_endpoint,
        distance_ft=round(total_ft, 2) if total_ft is not None else None,
        distance_m=round(total_m, 2) if total_m is not None else None,
        geometry=geometry,
        directions=tuple(steps),
        estimated_minutes=estimated,
        estimated_minutes_label=estimate_label,
        gis_travel_time_available=gis_time_available,
        distance_source=distance_source,
        closures_considered=False,
        provenance=prov,
        notes=tuple(notes),
    )


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    if not _finite(value):
        return None
    seconds = float(value) / 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(_CAMPUS_TZ)
    except (OverflowError, OSError, ValueError):
        return None


def normalize_closures(payload: Any, *, layer: str, source_label: str,
                       provenance: Provenance | None = None) -> list[Closure]:
    """Construction closure features -> Closure list with effective intervals."""
    prov = provenance or _provenance(source_label, layer)
    if not isinstance(payload, dict):
        raise VTGISError("closures payload is not an object")
    features = payload.get("features") or []
    if not isinstance(features, list):
        raise VTGISError("closures payload has no features list")
    top_sr = payload.get("spatialReference")
    out: list[Closure] = []
    for f in features:
        if not isinstance(f, dict):
            continue
        attrs = f.get("attributes") or {}
        geometry = normalize_geometry_to_wgs84(f.get("geometry"), top_sr)
        start = _epoch_ms_to_dt(_attr(attrs, "closurestartdate"))
        end = _epoch_ms_to_dt(_attr(attrs, "closureenddate"))
        oid = _attr(attrs, "objectid")
        out.append(Closure(
            closure_id=str(oid) if oid is not None else f"{source_label}:{len(out)}",
            name=str(_attr(attrs, "constructionsite") or "").strip(),
            start=start, end=end,
            project_type=_attr(attrs, "projecttype"),
            comments=_attr(attrs, "comments"),
            url=_attr(attrs, "url"),
            layer=layer,
            source_label=source_label,
            provenance=prov,
            geometry=tuple(geometry),
        ))
    return out


# --------------------------------------------------------------------------- #
# Public API: buildings
# --------------------------------------------------------------------------- #
_BUILDING_FIELDS = ("objectid,name,bldg_num,vtes_bldg_num,bldg_use,status,"
                    "latitude,longitude,x_coord,y_coord,url")
_ENTRANCE_FIELDS = "objectid,id,ada,handletype,floor,elevation,bldg_id,type"
_CLOSURE_FIELDS = ("objectid,constructionsite,closurestartdate,closureenddate,"
                   "projecttype,comments,url")


def search_buildings(query: str, *, limit: int = 20, force: bool = False
                     ) -> list[Building] | Unavailable:
    """Search official VT buildings by name or building number."""
    q = str(query or "").strip()
    if not q:
        return []
    try:
        requested = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if requested <= 0:
        return []
    requested = min(requested, _BUILDING_SEARCH_MAX)
    params = build_query_params(
        where=build_building_search_where(q),
        out_fields=_BUILDING_FIELDS,
        result_record_count=requested,
        order_by="name",
    )
    payload = _query_layer("vtgis_buildings_search", BUILDINGS_LAYER,
                           "vtgis_buildings", params,
                           key_params={**params, "_layer": "buildings"},
                           force=force)
    if isinstance(payload, Unavailable):
        return payload
    try:
        rows = normalize_buildings(
            payload, provenance=_provenance("Buildings", BUILDINGS_LAYER))
        # The layer can contain multiple polygon records for one building.
        # Search is a destination picker, so expose each canonical building once.
        unique: list[Building] = []
        seen: set[str] = set()
        for row in rows:
            if row.building_id not in seen:
                unique.append(row)
                seen.add(row.building_id)
        return unique[:requested]
    except VTGISError as exc:
        return Unavailable(STATUS_UNAVAILABLE, "Buildings",
                           "malformed building response", str(exc),
                           FALLBACK_CONTRACT)


def building_by_id(building_id: str, *, force: bool = False
                   ) -> Building | None | Unavailable:
    """One official building by number/id, or None if not found."""
    bid = str(building_id or "").strip()
    if not bid:
        return None
    params = build_query_params(where=build_where_id(bid),
                                out_fields=_BUILDING_FIELDS,
                                result_record_count=5)
    payload = _query_layer("vtgis_buildings_by_id", BUILDINGS_LAYER,
                           "vtgis_buildings", params,
                           key_params={**params, "_layer": "buildings"},
                           force=force)
    if isinstance(payload, Unavailable):
        return payload
    try:
        rows = normalize_buildings(
            payload, provenance=_provenance("Buildings", BUILDINGS_LAYER))
    except VTGISError as exc:
        return Unavailable(STATUS_UNAVAILABLE, "Buildings",
                           "malformed building response", str(exc),
                           FALLBACK_CONTRACT)
    want = canonical_building_id(bid)
    for row in rows:
        if want in (canonical_building_id(row.bldg_num),
                    canonical_building_id(row.vtes_bldg_num),
                    row.building_id):
            return row
    return rows[0] if rows else None


def entrances_for_building(building: Building | str, *, force: bool = False
                           ) -> list[Entrance] | Unavailable:
    """Accessible/non-accessible entrances joined to one building."""
    if isinstance(building, Building):
        candidates = [building.bldg_num, building.vtes_bldg_num, building.building_id]
        prov = _provenance("Accessibility/Entrances",
                           ACCESSIBILITY_ENTRANCES_LAYER)
    else:
        candidates = [str(building)]
        prov = _provenance("Accessibility/Entrances",
                           ACCESSIBILITY_ENTRANCES_LAYER)
    candidates = [str(c).strip() for c in candidates if c not in (None, "")]
    if not candidates:
        return []
    query_candidates = set(candidates)
    for candidate in candidates:
        canonical = canonical_building_id(candidate)
        if canonical and canonical.isdigit():
            query_candidates.add(canonical.zfill(5))
    params = build_query_params(where=build_where_bldg_ids(query_candidates),
                                out_fields=_ENTRANCE_FIELDS,
                                result_record_count=200)
    payload = _query_layer("vtgis_entrances", ACCESSIBILITY_ENTRANCES_LAYER,
                           "vtgis_entrances", params,
                           key_params={**params, "_layer": "entrances"},
                           force=force)
    if isinstance(payload, Unavailable):
        return payload
    try:
        rows = normalize_entrances(payload, provenance=prov)
    except VTGISError as exc:
        return Unavailable(STATUS_UNAVAILABLE, "Accessibility/Entrances",
                           "malformed entrance response", str(exc),
                           FALLBACK_CONTRACT)
    # Server-side `bldg_id IN (...)` already filters, but re-assert the canonical
    # join client-side so a differently zero-padded fixture cannot leak another
    # building's entrances into this building's detail.
    want = {canonical_building_id(c) for c in query_candidates}
    want.discard(None)
    return [e for e in rows if e.building_id in want] if want else rows


def building_detail(building_id: str, *, force: bool = False
                    ) -> BuildingDetail | Unavailable | None:
    """Building + its entrances (joined on the canonical building id)."""
    b = building_by_id(building_id, force=force)
    if isinstance(b, Unavailable):
        return b
    if b is None:
        return None
    entrances = entrances_for_building(b, force=force)
    if isinstance(entrances, Unavailable):
        # Empty would falsely claim that the official layer has no entrances.
        return entrances
    accessible = [e for e in entrances if e.accessible]
    return BuildingDetail(building=b, entrances=tuple(entrances),
                          accessible_entrance_count=len(accessible))


# --------------------------------------------------------------------------- #
# Public API: routes
# --------------------------------------------------------------------------- #
def _coerce_endpoint(value: Any, *, authoritative: bool = False,
                     label: str | None = None) -> RouteEndpoint:
    if isinstance(value, RouteEndpoint):
        if authoritative:
            return value
        # RouteEndpoint is public and constructible, so accepting its flag here
        # would let raw caller coordinates self-declare official authority.
        lat, lon = _validate_coords(float(value.lat), float(value.lon))
        return RouteEndpoint(lat=lat, lon=lon, label=value.label or label,
                             authoritative=False)
    if isinstance(value, Building):
        if value.lat is None or value.lon is None:
            raise ValueError(f"building {value.building_id!r} has no coordinates")
        return RouteEndpoint(lat=value.lat, lon=value.lon, label=value.name,
                             authoritative=value.coords_authoritative,
                             building_id=value.building_id)
    if isinstance(value, Entrance):
        return RouteEndpoint(lat=value.lat, lon=value.lon,
                             label=f"entrance {value.entrance_id}",
                             authoritative=True,
                             building_id=value.building_id,
                             entrance_id=value.entrance_id)
    if isinstance(value, (tuple, list)) and len(value) == 2:
        lat, lon = _validate_coords(float(value[0]), float(value[1]))
        return RouteEndpoint(lat=lat, lon=lon, label=label,
                             authoritative=False)
    if isinstance(value, dict) and "lat" in value and "lon" in value:
        # A raw dict is caller-supplied data. It is NEVER authoritative: only a
        # Building/Entrance fetched from VT GIS may set that flag.
        lat, lon = _validate_coords(float(value["lat"]), float(value["lon"]))
        return RouteEndpoint(lat=lat, lon=lon,
                             label=value.get("label", label),
                             authoritative=False)
    raise TypeError(f"cannot interpret endpoint {value!r}")


def _validate_coords(lat: float, lon: float) -> tuple[float, float]:
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise ValueError(f"coordinate out of range: lat={lat}, lon={lon}")
    return lat, lon


def _solve_key_params(mode: str) -> dict:
    # Exact endpoint/body identity is an opaque digest added by post_form_json.
    # Keeping raw coordinates out of filenames avoids leaking a user's origin.
    return {"mode": mode}


def _request_route(from_e: RouteEndpoint, to_e: RouteEndpoint, *, mode: str,
                   ui_mode: str | None, force: bool,
                   walk_speed_mps: float | None) -> Route | NoRoute | Unavailable:
    """Perform a solve for already classified (trusted or caller) endpoints."""
    form = build_solve_form(from_e, to_e, mode)
    key = _solve_key_params(mode)
    try:
        payload = cache.post_form_json(
            "vtgis_route_solve", f"{ROUTE_SERVICE}/solve", form,
            params=key, max_age_s=_ROUTE_MAX_AGE_S, force=force)
    except cache.CacheMiss as exc:
        return Unavailable(STATUS_UNAVAILABLE, "Route solve",
                           "offline: no cached route response", str(exc),
                           FALLBACK_CONTRACT)
    except cache.CacheRefreshError as exc:
        return Unavailable(STATUS_UNAVAILABLE, "Route solve",
                           "offline: refresh suppressed after recent failure",
                           str(exc), FALLBACK_CONTRACT)
    except Exception as exc:  # noqa: BLE001
        return Unavailable(STATUS_UNAVAILABLE, "Route solve",
                           "route solve request failed",
                           f"{type(exc).__name__}: {exc}", FALLBACK_CONTRACT)
    try:
        return normalize_route(
            payload, mode=mode, from_endpoint=from_e, to_endpoint=to_e,
            provenance=_provenance("Route solve", ROUTE_SERVICE),
            ui_mode=ui_mode, walk_speed_mps=walk_speed_mps)
    except VTGISError as exc:
        return Unavailable(STATUS_UNAVAILABLE, "Route solve",
                           "malformed route response", str(exc),
                           FALLBACK_CONTRACT)


def solve_route(from_endpoint: Any, to_endpoint: Any, *,
                mode: str = MODE_WALKING, ui_mode: str | None = None,
                force: bool = False,
                walk_speed_mps: float | None = None
                ) -> Route | NoRoute | Unavailable:
    """Solve one Walking or ADA Walking route between two points.

    Caller-supplied tuples, dictionaries and ``RouteEndpoint`` instances are
    always non-authoritative. Passing a ``Building`` or ``Entrance`` returned by
    this client preserves its official-coordinate classification.
    """
    mode = validate_mode(mode)
    if ui_mode is not None:
        option = resolve_ui_mode(ui_mode)
        if option["route_mode"] != mode:
            raise ValueError(f"ui mode {ui_mode!r} does not use route mode {mode!r}")
        ui_mode = option["ui_mode"]
    from_e = _coerce_endpoint(from_endpoint, authoritative=False, label="Origin")
    to_e = _coerce_endpoint(to_endpoint, authoritative=False, label="Destination")
    return _request_route(from_e, to_e, mode=mode, ui_mode=ui_mode,
                          force=force, walk_speed_mps=walk_speed_mps)


def route_between_buildings(from_building_id: str, to_building_id: str, *,
                            mode: str = MODE_WALKING,
                            prefer_entrances: bool = True,
                            force: bool = False,
                            walk_speed_mps: float | None = None
                            ) -> Route | NoRoute | Unavailable:
    """Convenience: route between two official buildings.

    For ADA mode, when `prefer_entrances`, snap each end to the nearest
    accessible entrance (falling back to the building point). For walking, use
    the official building point.
    """
    mode = validate_mode(mode)
    a = building_by_id(from_building_id, force=force)
    b = building_by_id(to_building_id, force=force)
    if isinstance(a, Unavailable):
        return a
    if isinstance(b, Unavailable):
        return b
    if a is None:
        return Unavailable(STATUS_UNAVAILABLE, "Buildings",
                           f"unknown origin building {from_building_id!r}",
                           None, FALLBACK_CONTRACT)
    if b is None:
        return Unavailable(STATUS_UNAVAILABLE, "Buildings",
                           f"unknown destination building {to_building_id!r}",
                           None, FALLBACK_CONTRACT)

    from_e = _endpoint_for_building(a, prefer_entrances=prefer_entrances,
                                    accessible=mode == MODE_ADA_WALKING,
                                    force=force)
    to_e = _endpoint_for_building(b, prefer_entrances=prefer_entrances,
                                  accessible=mode == MODE_ADA_WALKING,
                                  force=force)
    return _request_route(from_e, to_e, mode=mode, ui_mode=None, force=force,
                          walk_speed_mps=walk_speed_mps)


def _endpoint_for_building(building: Building, *, prefer_entrances: bool,
                           accessible: bool, force: bool) -> RouteEndpoint:
    if building.lat is None or building.lon is None:
        raise ValueError(f"building {building.building_id!r} has no coordinates")
    if prefer_entrances:
        entrances = entrances_for_building(building, force=force)
        if isinstance(entrances, list):
            pool = [e for e in entrances if e.accessible] if accessible else entrances
            if pool:
                # nearest to the building point for deterministic selection
                best = min(pool, key=lambda e: haversine_m(
                    (building.lat, building.lon), (e.lat, e.lon)))
                return RouteEndpoint(
                    lat=best.lat, lon=best.lon,
                    label=f"{building.name} entrance",
                    authoritative=True, building_id=building.building_id,
                    entrance_id=best.entrance_id)
    return RouteEndpoint(lat=building.lat, lon=building.lon, label=building.name,
                         authoritative=True, building_id=building.building_id)


# --------------------------------------------------------------------------- #
# Public API: closures
# --------------------------------------------------------------------------- #
def _fetch_closure_layer(service: str, layer_url: str, name: str,
                         layer_label: str, *, force: bool
                         ) -> list[Closure] | Unavailable:
    params = build_query_params(where="1=1", out_fields=_CLOSURE_FIELDS,
                                result_record_count=2000)
    payload = _query_layer(name, layer_url, "vtgis_closures", params,
                           key_params={**params, "_layer": name},
                           max_age_s=_CLOSURE_MAX_AGE_S, force=force)
    if isinstance(payload, Unavailable):
        return payload
    try:
        return normalize_closures(
            payload, layer=layer_url, source_label=layer_label,
            provenance=_provenance(layer_label, layer_url))
    except VTGISError as exc:
        return Unavailable(STATUS_UNAVAILABLE, layer_label,
                           "malformed closure response", str(exc),
                           FALLBACK_CONTRACT)


def closures(*, include_scheduled: bool = True, force: bool = False
             ) -> list[Closure] | Unavailable:
    """Current (and optionally scheduled) construction closures.

    Returns all intervals; call `effective_closures(at=...)` to filter. If one
    layer fails and the other succeeds, the successful layer is returned.
    """
    current = _fetch_closure_layer(
        "Construction_Closures/0", CONSTRUCTION_CLOSURES_CURRENT,
        "vtgis_closures_current", "Current construction closures", force=force)
    results: list[Closure] = []
    errors: list[str] = []
    if isinstance(current, Unavailable):
        errors.append(current.reason)
    else:
        results.extend(current)

    if include_scheduled:
        scheduled = _fetch_closure_layer(
            "Construction_Closures/1", CONSTRUCTION_CLOSURES_SCHEDULED,
            "vtgis_closures_scheduled", "Scheduled construction closures",
            force=force)
        if isinstance(scheduled, Unavailable):
            errors.append(scheduled.reason)
        else:
            results.extend(scheduled)

    if not results and errors:
        return Unavailable(STATUS_UNAVAILABLE, "Construction closures",
                           "; ".join(errors), None, FALLBACK_CONTRACT)
    results.sort(key=lambda c: (c.start is None, c.start or datetime.max.replace(
        tzinfo=timezone.utc), c.name))
    return results


def effective_closures(at: datetime | None = None, *,
                       include_scheduled: bool = True, force: bool = False
                       ) -> list[Closure] | Unavailable:
    """Only closures whose effective interval contains `at` (default now).

    This is informational only: the interval is exposed, but no route solve in
    this build consumes closures as barriers.
    """
    rows = closures(include_scheduled=include_scheduled, force=force)
    if isinstance(rows, Unavailable):
        return rows
    return [c for c in rows if c.is_effective_at(at)]


def closures_for_day(day: datetime | None = None, *, force: bool = False
                     ) -> list[Closure] | Unavailable:
    """Closures that overlap a calendar day (default today, campus time)."""
    ref = _as_campus_local(day)
    start_of_day = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day.replace(hour=23, minute=59, second=59,
                                      microsecond=999999)
    rows = closures(force=force)
    if isinstance(rows, Unavailable):
        return rows
    out = []
    for c in rows:
        if c.start is not None and c.start > end_of_day:
            continue
        if c.end is not None and c.end < start_of_day:
            continue
        out.append(c)
    return out


# --------------------------------------------------------------------------- #
# Offline fallback contract
# --------------------------------------------------------------------------- #
def walk_fallback(from_coords: tuple[float, float] | RouteEndpoint,
                  to_coords: tuple[float, float] | RouteEndpoint, *,
                  speed_mps: float | None = None) -> dict:
    """The existing straight-line walk model, clearly labelled as a fallback.

    Mirrors hokieday.tools._walk_result's formula (haversine x
    config.WALK_PATH_FACTOR) so numbers agree with the agent's walk_time tool.
    This is NOT routed geometry and must never be presented as a GIS route.
    """
    a = _coerce_endpoint(from_coords, label="Origin")
    b = _coerce_endpoint(to_coords, label="Destination")
    metres = haversine_m((a.lat, a.lon), (b.lat, b.lon))
    path_m = metres * config.WALK_PATH_FACTOR
    minutes, label = estimate_walk_minutes(path_m, speed_mps)
    return {
        "status": STATUS_OK,
        "fallback": "haversine_walk",
        "distance_m": round(path_m, 1),
        "distance_ft": round(meters_to_feet(path_m), 1),
        "estimated_minutes": minutes,
        "estimated_minutes_label": label,
        "geometry": ((a.lat, a.lon), (b.lat, b.lon)),
        "routed": False,
        "method": ("haversine x WALK_PATH_FACTOR at declared walking speed; "
                   "STRAIGHT-LINE fallback, not a routed path"),
        "gis": False,
    }


def unavailable_fallback() -> dict:
    """Copy of the offline/degraded contract for callers to surface."""
    return dict(FALLBACK_CONTRACT)


# --------------------------------------------------------------------------- #
# Permission-gated export / snapshot
# --------------------------------------------------------------------------- #
def _route_coordinates(line: Sequence[tuple[float, float]]) -> list[list[float]]:
    return [[round(p[1], 7), round(p[0], 7)] for p in line]


def route_to_geojson(route: Route, *,
                     permission: RedistributionPermit | None = None) -> dict:
    """Route -> GeoJSON. Requires a written-permission capability."""
    assert_redistribution_allowed(permission)
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": _route_coordinates(route.geometry)},
            "properties": {
                "mode": route.mode,
                "distance_ft": route.distance_ft,
                "distance_m": route.distance_m,
                "attribution": route.provenance.attribution,
                "disclaimer": route.provenance.disclaimer,
            },
        }],
    }


def buildings_to_geojson(
        buildings: Sequence[Building], *,
        permission: RedistributionPermit | None = None) -> dict:
    """Buildings -> GeoJSON. Requires a written-permission capability."""
    assert_redistribution_allowed(permission)
    features = []
    for b in buildings:
        if b.lat is None or b.lon is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [b.lon, b.lat]},
            "properties": {"building_id": b.building_id, "name": b.name,
                           "bldg_num": b.bldg_num,
                           "attribution": b.provenance.attribution},
        })
    return {"type": "FeatureCollection", "features": features}


def snapshot_payload(
        payload: Any, *,
        permission: RedistributionPermit | None = None) -> Any:
    """Return payload unchanged only with recorded written permission.

    Exists so a caller who is tempted to dump a raw GIS response to fixtures/
    must first provide a capability tied to a written permission record.
    """
    assert_redistribution_allowed(permission)
    return payload