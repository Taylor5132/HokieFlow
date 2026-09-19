"""Configuration: endpoints, paths, and tuning constants.

OWNER: parent (do not edit from a worker subagent).
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- paths
PKG_DIR = Path(__file__).resolve().parent          # .../hokieday/hokieday
REPO_DIR = PKG_DIR.parent                          # .../hokieday
CACHE_DIR = Path(os.environ.get("HOKIEDAY_CACHE", REPO_DIR / "cache"))
DATA_DIR = Path(os.environ.get("HOKIEDAY_DATA", REPO_DIR / "data"))
GTFS_DIR = CACHE_DIR / "gtfs"                      # extracted static feed

for _d in (CACHE_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- demo mode
# "live"  -> hit the network, write through to cache, fall back to stale cache
# "cache" -> never touch the network; read cache only (demo safety, tests)
DEMO_MODE = os.environ.get("DEMO_MODE", "live").strip().lower()
CACHE_ONLY = DEMO_MODE == "cache"

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
# Coordinates for walk_time(). 'verified' means confirmed against a live source
# during the data spike; everything else MUST be calibrated before the demo.
PLACES: dict[str, dict] = {
    "Burruss Hall":        {"lat": 37.22957, "lon": -80.41394, "verified": True},
    "Stop 1600":           {"lat": 37.22924, "lon": -80.41366, "verified": True},
    # --- UNVERIFIED: calibrate from vt.edu/maps before presenting -----------
    "McBryde Hall":        {"lat": 37.22903, "lon": -80.41905, "verified": False},
    "Hahn Hall":           {"lat": 37.23230, "lon": -80.41838, "verified": False},
    "D2 at Dietrick Hall": {"lat": 37.22543, "lon": -80.41633, "verified": False},
    "West End Market":     {"lat": 37.23364, "lon": -80.41932, "verified": False},
    "Owens Food Court":    {"lat": 37.23270, "lon": -80.41289, "verified": False},
    "Squires Student Center": {"lat": 37.22936, "lon": -80.41785, "verified": False},
}

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