"""Configuration: endpoints, paths, tuning constants, and the replay clock.

OWNER: parent (do not edit from a worker subagent).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- paths
PKG_DIR = Path(__file__).resolve().parent          # .../hokieday/hokieday
REPO_DIR = PKG_DIR.parent                          # .../hokieday

# Two SEPARATE stores, deliberately:
#   cache/     - the live cache. Refreshed whenever we hit the network.
#   fixtures/  - the FROZEN replay snapshot. Only scripts/seed_cache.py writes it.
# They must never be the same directory: a live poll used to overwrite the very
# fixture the tests and the offline demo read, which silently changed expected
# values mid-session (observed: fixture `fetched_at` jumped 15:22 -> 15:51).
CACHE_DIR = Path(os.environ.get("HOKIEDAY_CACHE", REPO_DIR / "cache"))
FIXTURES_DIR = Path(os.environ.get("HOKIEDAY_FIXTURES", REPO_DIR / "fixtures"))
DATA_DIR = Path(os.environ.get("HOKIEDAY_DATA", REPO_DIR / "data"))

# ---------------------------------------------------------------- demo mode
# "live"  -> hit the network, write through to cache/, fall back to stale cache
# "cache" -> never touch the network; read the FROZEN fixtures/ only
DEMO_MODE = os.environ.get("DEMO_MODE", "live").strip().lower()
CACHE_ONLY = DEMO_MODE == "cache"

if CACHE_ONLY:
    CACHE_DIR = FIXTURES_DIR        # a replay never reads the live cache

GTFS_DIR = CACHE_DIR / "gtfs"      # extracted static feed

for _d in (CACHE_DIR, FIXTURES_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- replay clock
# A schedule delta is `now - scheduled_departure`, so a cached snapshot's deltas
# grow by exactly one minute per minute of real time. Verified failure: a
# 6.7-minute gap between capture and replay inflated all 13 live deltas to a
# +7.73 min mean, and a fixture replayed the next morning reads as ~20 hours late.
# In replay mode the clock must therefore be pinned to the snapshot, not the wall.
_DEMO_NOW_ENV = os.environ.get("DEMO_NOW")
_pin: datetime | None = None
_pin_resolved = False


def _resolve_pin() -> datetime | None:
    """Pin the replay clock: explicit DEMO_NOW wins, else the live-bus snapshot's
    own capture time (so the bus positions and the clock agree)."""
    global _pin, _pin_resolved
    if _pin_resolved:
        return _pin
    _pin_resolved = True

    if _DEMO_NOW_ENV:
        try:
            _pin = datetime.fromisoformat(_DEMO_NOW_ENV).astimezone(timezone.utc)
            return _pin
        except ValueError:
            print(f"[config] WARN DEMO_NOW={_DEMO_NOW_ENV!r} is not ISO-8601; ignoring")

    p = CACHE_DIR / "bt_buses.json"
    if p.exists():
        try:
            _pin = datetime.fromisoformat(
                json.loads(p.read_text(encoding="utf-8"))["fetched_at"]
            ).astimezone(timezone.utc)
        except Exception as exc:                            # noqa: BLE001
            print(f"[config] WARN could not read snapshot time from {p.name}: {exc}")
    return _pin


def now(tz=None) -> datetime:
    """The system clock for all library code.

    live mode : the real wall clock (UTC).
    cache mode: pinned to the replay snapshot (see above), so replayed schedule
                deltas and staleness labels stay self-consistent.

    Pass `tz` (e.g. ZoneInfo(CAMPUS_TZ)) to get the time in that zone.
    """
    t = _resolve_pin() if CACHE_ONLY else None
    if t is None:
        t = datetime.now(timezone.utc)
    return t.astimezone(tz) if tz is not None else t

# ---------------------------------------------------------------- endpoints
ENDPOINTS = {
    # --- transit (all verified live 2026-09-19) ---
    "bt_gtfs": "http://www.bt4uclassic.org/gtfs/google_transit.zip",
    "bt_buses": (
        "https://ridebt.org/index.php?option=com_ajax&module=bt_map"
        "&method=getBuses&format=json&Itemid=101"
    ),
    "bt_routes": (
        "https://ridebt.org/index.php?option=com_ajax&module=bt_map"
        "&method=getRoutes&format=json&Itemid=101"
    ),
    "bt_patterns": (
        "https://ridebt.org/index.php?option=com_ajax&module=bt_map"
        "&method=getRoutePatterns&format=json&Itemid=101"
    ),
    # --- dining (FoodPro). NOTE: the two date formats differ. ---
    "dining_locations": "https://foodpro.students.vt.edu/menus/API/Locations.aspx",
    "dining_menu": (
        "https://foodpro.students.vt.edu/menus/API/MenuAtLocation.aspx"
        "?locationNum={location_num}&dtdate={dtdate}"
    ),
    "dining_nutrition": (
        "https://foodpro.students.vt.edu/menus/API/NutritiveReport.aspx?items={items}"
    ),
    "dining_allergens": (
        "https://foodpro.students.vt.edu/menus/API/Allergens.aspx?locationNum={location_num}"
    ),
    # hours API is keyed by foodpro_id and takes an ISO date
    "dining_hours": (
        "https://apps.students.vt.edu/hours/Api/NonRestricted/FoodProCentersOpen"
        "/readByFoodPro/{foodpro_id}/{date}"
    ),
    # --- misc ---
    "weather_points": "https://api.weather.gov/points/{lat},{lon}",
    "events": "https://events.vt.edu/events",
}

# Substrings that indicate a request is ours (be a polite client).
USER_AGENT = "hokieday-hackathon/0.1 (VTHacks 14; educational)"

# ---------------------------------------------------------------- tuning
LIVE_BUS_POLL_SECONDS = 60          # never poll faster; undocumented endpoint
STALE_LIVE_MINUTES = 10             # beyond this, label data schedule-only
DEFAULT_MENU_CACHE_MAX_AGE_S = 6 * 3600
DEFAULT_GTFS_CACHE_MAX_AGE_S = 24 * 3600

WALK_SPEED_MPS = 1.35               # average pedestrian
WALK_PATH_FACTOR = 1.30             # straight-line -> path distance. UNVERIFIED: calibrate.

BUS_FULL_PCT = 85                   # crowding that should trigger an alternative
DEPARTURE_HORIZON_MIN = 180

# ---------------------------------------------------------------- campus time
CAMPUS_TZ = "America/New_York"

# ---------------------------------------------------------------- places
# Coordinates for walk_time(). 'verified' means confirmed against an official
# source. The VT GIS audit disproved the legacy Burruss coordinate below, so it
# remains an explicitly UNVERIFIED placeholder until the post-push GIS route
# integration recalibrates every place together. Stop 1600 comes from BT GTFS.
PLACES: dict[str, dict] = {
    "Burruss Hall":        {"lat": 37.22957, "lon": -80.41394, "verified": False},
    "Stop 1600":           {"lat": 37.22924, "lon": -80.41366, "verified": True},
    # --- UNVERIFIED: replace from official VT GIS before presenting --------
    "McBryde Hall":        {"lat": 37.22903, "lon": -80.41905, "verified": False},
    "Hahn Hall":           {"lat": 37.23230, "lon": -80.41838, "verified": False},
    "D2 at Dietrick Hall": {"lat": 37.22543, "lon": -80.41633, "verified": False},
    "West End Market":     {"lat": 37.23364, "lon": -80.41932, "verified": False},
    "Owens Food Court":    {"lat": 37.23270, "lon": -80.41289, "verified": False},
    "Squires Student Center": {"lat": 37.22936, "lon": -80.41785, "verified": False},
}

# --------------------------------------------------------------- dynamic places
# A device position is just another place, so GPS needs no new planner code path:
# register the coordinate here and pass its key as `from_place`. Keys are derived
# from the rounded coordinates, so two requests from the same spot share one entry
# and concurrent requests never race on each other's data (a single mutable
# "current location" key WOULD race, since the demo server is threaded).
#
# Coordinates from a device are NOT survey-grade: 'verified' stays False and the
# reported accuracy is carried through so the UI can disclose it.
# Official VT GIS Burruss centroid; used only for the 5 km on-campus guard.
# PLACES coordinates are recalibrated separately in the GIS integration.
CAMPUS_REFERENCE = (37.22924778, -80.42396247)
MAX_ORIGIN_KM = 5.0                          # beyond this the position is not campus
MAX_DYNAMIC_PLACES = 64                      # bounded; evict oldest
_DYNAMIC_ORDER: list[str] = []

# The demo server is threaded, so the check/insert/evict sequence below is a
# critical section: without this lock two requests can both see `key not in
# PLACES`, both append to _DYNAMIC_ORDER, and the eviction loop can pop an
# entry that another thread is reading. The lock makes the "concurrent requests
# never race" claim in the comment above actually true.
_DYNAMIC_LOCK = threading.Lock()


def register_dynamic_place(lat: float, lon: float,
                           label: str = "your location",
                           accuracy_m: float | None = None) -> str:
    """Register a device position as a place and return its lookup key.

    Returns a stable key per (rounded) coordinate pair. Raises ValueError for
    coordinates that are not on Earth. Thread-safe: the registration and the
    bounded eviction run under a single lock, so concurrent requests share an
    entry (same coordinates) instead of racing on it.
    """
    lat, lon = float(lat), float(lon)
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise ValueError(f"coordinate out of range: lat={lat}, lon={lon}")
    key = f"{label} ({lat:.5f}, {lon:.5f})"
    with _DYNAMIC_LOCK:
        if key not in PLACES:
            PLACES[key] = {
                "lat": lat, "lon": lon, "verified": False,
                "dynamic": True, "accuracy_m": accuracy_m,
            }
            _DYNAMIC_ORDER.append(key)
            while len(_DYNAMIC_ORDER) > MAX_DYNAMIC_PLACES:
                PLACES.pop(_DYNAMIC_ORDER.pop(0), None)
        else:
            PLACES[key]["accuracy_m"] = accuracy_m
    return key


def _snapshot_row(row: dict) -> dict:
    """Copy a registry row so callers never share mutable state with the lock."""
    return dict(row)


def lookup_place(place_key: str) -> dict | None:
    """Thread-safe copy of one place row, or None. Never returns the live row."""
    with _DYNAMIC_LOCK:
        row = PLACES.get(place_key)
        return _snapshot_row(row) if row is not None else None


def find_place(name: str | None) -> dict | None:
    """Case-insensitive lookup in PLACES, returning a row COPY (or None).

    The runtime equivalent of ``tools._place``: callers must not iterate or
    index ``PLACES`` directly while another thread may be registering/evicting
    dynamic entries.
    """
    if name is None:
        return None
    want = str(name).strip().lower()
    with _DYNAMIC_LOCK:
        for k, v in PLACES.items():
            if k.lower() == want:
                return _snapshot_row(v)
    return None


def place_snapshot() -> dict[str, dict]:
    """Thread-safe copy of the whole registry with copied rows.

    Safe to iterate at any time: the caller owns the returned dict and rows,
    so a concurrent registration/eviction cannot raise "dictionary changed
    size during iteration" or mutate a row mid-read.
    """
    with _DYNAMIC_LOCK:
        return {k: _snapshot_row(v) for k, v in PLACES.items()}


def static_places() -> list[tuple[str, dict]]:
    """Ordered static (key, row-copy) pairs, taken under the dynamic lock."""
    with _DYNAMIC_LOCK:
        return [(k, _snapshot_row(v)) for k, v in PLACES.items()
                if not v.get("dynamic")]


def static_place_keys() -> list[str]:
    """Sorted keys of the STATIC campus places only.

    Dynamic device places are session-scoped and their keys embed the rounded
    coordinates of wherever a student actually is. Listing them in an error
    payload ("known places: ...") would leak one request's position to every
    other client, so user-facing place lists must use this helper, never
    `sorted(PLACES)`. Runs under the dynamic lock so a concurrent eviction
    cannot mutate the mapping mid-iteration.
    """
    with _DYNAMIC_LOCK:
        return sorted(k for k, v in PLACES.items() if not v.get("dynamic"))


def is_dynamic(place_key: str) -> bool:
    with _DYNAMIC_LOCK:
        return bool(PLACES.get(place_key, {}).get("dynamic"))

# ---------------------------------------------------------------- dining ids
# Verified from Locations.aspx and the hours API on 2026-09-19.
DINING_LOCATIONS: dict[str, str] = {
    "09": "Hokie Grill at Owens",
    "15": "D2 at Dietrick Hall",
    "72": "Deet's Place",
    "01": "Ducky's at GLC",
    "71": "DX",
    "39": "Owens Food Court",
}

KNOWN_ALLERGENS = [
    "Milk", "Eggs", "Fish", "Crustacean Shellfish", "Tree Nuts",
    "Peanuts", "Wheat", "Soybeans", "Gluten", "Sesame",
]
DIET_CODES = {"wcveg": "Vegan", "wcvtn": "Vegetarian", "wcha": "Halal Certified Meat", "wcal": "Alcohol"}

# ---------------------------------------------------------------- allergen policy
# Sections whose kitchen Virginia Tech documents as free from the TOP NINE
# allergens, with separate storage / preparation / cooking / serving space.
# Source: dining.vt.edu Dietrick Hall page (verified 2026-09-19): Viridian is
# "a dedicated kitchen serving menu items that are free from the top nine
# allergens (dairy, egg, fish, shellfish, peanut, tree nuts, soy, sesame, wheat
# and gluten)".
#
# WHY THIS IS LOAD-BEARING: all 48 Viridian items carry a BLANK allergen field.
# Blank normally means UNKNOWN, but here the blank is EXPLAINED by a documented
# venue-level guarantee -- which makes these the safest items on the menu. A
# blanket "blank means unknown, exclude it" rule would hide exactly the food a
# peanut-allergic student needs.
VENUE_ALLERGEN_FREE_SECTIONS = ("viridian",)


def is_venue_allergen_free(section: str | None) -> bool:
    """True when an item's section belongs to a documented allergen-free kitchen."""
    s = (section or "").strip().lower()
    return any(s.startswith(p) for p in VENUE_ALLERGEN_FREE_SECTIONS)