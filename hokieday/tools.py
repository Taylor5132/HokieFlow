"""The 8-tool contract between the agent and reality (SDD.md section 7).

Owner: worker tools. Every public tool:
  * returns a plain dict/list -- it NEVER raises into the agent loop
    (SDD 7.2 rule 1: tolerate bad input, always give the agent something to say);
  * carries provenance in flags (is_realtime / stale / basis / method -- rule 3);
  * carries units in field names (in_min, kcal, minutes, meters -- rule 2);
  * never invents a number the code did not compute (README rule 5).

The `Source` protocol decouples the tools from where the data lives: today the
local modules (hokieday.gtfs / livebus / dining), tomorrow Unity Catalog Delta
tables read inside Databricks. `LocalSource` is the default implementation.

Two honest absences, deliberately NOT faked:
  * predict_bus_delay -- no model exists yet: basis="no_model", delta None;
  * get_events        -- the events.vt.edu scrape was never built (SDD 5.6):
                          [] + reason, never invented rows.

CLOCK DISCIPLINE: every time comes from config.now() (the replay clock, pinned
to the snapshot in DEMO_MODE=cache). This module contains no datetime.now()
call -- tests/test_integration.py's AST tripwire fails the suite if one appears.
"""
from __future__ import annotations

import math
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from . import config, dining, gtfs, livebus

_CAMPUS_TZ = ZoneInfo(config.CAMPUS_TZ)

# Per-request "now" override. The server captures ONE campus-local timestamp at
# the /api/ask boundary and plan_day() pins it here, so every calculation inside
# the request (window dates, dining hours date, live-bus deltas, re-plan margins)
# agrees with the captured request context instead of re-reading the wall clock
# a few milliseconds later. ContextVar, not a module global: the demo server is
# threaded, and one request must never see another request's clock. When unset
# (direct library calls, tests) the code falls back to config.now() exactly as
# before, which keeps the AST clock tripwire and replay pinning intact.
_REQUEST_NOW: ContextVar[datetime | None] = ContextVar(
    "hokieday_request_now", default=None)

# ---------------------------------------------------------------------------
# Stated assumptions -- inputs to the arithmetic, not measurements. Each is
# echoed back in the itinerary so a judge can question it.
# ---------------------------------------------------------------------------
EAT_MINUTES = 20.0          # assumed time to sit down and eat one item
MIN_BOARD_BUFFER_MIN = 2.0   # buffer needed at a stop to actually board the bus

# When a student says they are hungry, prefer a MEAL-sized item over the smallest
# available. Ordering by calories ascending made the agent offer "Cinnamon Apples
# (82 kcal)" as lunch. This is a DECLARED assumption, surfaced in the demo UI
# alongside EAT_MINUTES -- not a hidden magic number.
MEAL_TARGET_KCAL = 450.0        # kept only to document a failed approach; see below

# How the "I'm hungry" pick is chosen. Four approaches were tried and each
# failed visibly in the demo:
#   1. kcal ascending        -> "Cinnamon Apples (82 kcal)" as lunch
#   2. closeness to 450 kcal -> "Oreo Cobbler Cake"
#   3. + dessert penalty     -> "Bleu Cheese Dressing" (a salad-bar condiment)
#   4. + item-name penalty   -> "1000 Island" (a dressing with no keyword in its name)
# Keyword-hunting does not converge, because "is this a meal?" is not recoverable
# from names. The shipped rule is deliberately simple and explainable:
#     among items that satisfy the constraints, offer the MOST FILLING one
#     (highest calories), with obviously non-meal sections demoted.
# A student who says they are hungry wants the biggest meal that fits, and that
# rule is one sentence a judge can check. It is a DEMO CONVENIENCE, not dietary
# advice -- stated in the UI rather than presented as nutrition.
MEAL_RANKING_RULE = "most filling option that satisfies the constraints"

# Ranking the "I'm hungry" pick. Two naive attempts failed in visible ways:
#   kcal ascending            -> "Cinnamon Apples (82 kcal)" as lunch
#   kcal-distance only        -> "Oreo Cobbler Cake"
#   + dessert penalty         -> "Bleu Cheese Dressing" (a condiment)
# So classify the SECTION into meal / neutral / non-meal and rank meals first,
# then closeness to the target. A whitelist is used rather than a blacklist
# because section names vary; the blacklist only demotes the obvious cases.
#
# NOTE: duplicated in the SQL UC function find_food (it cannot import Python);
# tests/test_regressions.py asserts the Python path picks a MEAL section.
MEAL_SECTION_PATTERN = ("entree|entr\u00e9e|deli|salad|pizza|pasta|grill|pan asia|"
                        "salsa|soup|chili|bowl|sub|wrap|sandwich|burrito|taco")
NON_MEAL_SECTION_PATTERN = (
    "condiment|topping|dressing|sauce|syrup|butter|spread|beverage|drink|"
    "coffee|tea|juice|soda|milk|patisserie|ice cream|dessert|cookie|cake|"
    "brownie|cobbler|yogurt|mousse|smoothie|cereal|bagel|bread|oatmeal|"
    # Build-your-own stations ("Edens Salad Bar", "East Side Deli Bar",
    # "Yogurt Bar") serve COMPONENTS, not composed dishes -- which is why a
    # salad bar offered "1000 Island" as lunch. Written as " bar" so it does not
    # also catch "barbecue".
    " bar")
MEAL_SECTION_SQL = ("entree|entr\u00e9e|deli|salad|pizza|pasta|grill|pan asia|"
                    "salsa|soup|chili|bowl|sub|wrap|sandwich|burrito|taco")
NON_MEAL_SECTION_SQL = (
    "condiment|topping|dressing|sauce|syrup|butter|spread|beverage|drink|"
    "coffee|tea|juice|soda|milk|patisserie|ice cream|dessert|cookie|cake|"
    "brownie|cobbler|yogurt|mousse|smoothie|cereal|bagel|bread|oatmeal|"
    " bar")

# Section alone is not enough: "Bleu Cheese Dressing" is served at the SALAD BAR,
# so the section matched the meal whitelist and the agent offered dressing as
# lunch. The ITEM name is the signal that actually distinguishes a dish from a
# condiment, so it is checked first and can only ever DEMOTE.
NON_MEAL_ITEM_PATTERN = (
    "dressing|vinaigrette|sauce|syrup|butter|spread|jam|jelly|condiment|dip|"
    "seasoning|vinegar|ketchup|mustard|mayonnaise|soda|juice|coffee|tea|water|"
    "milk|cream cheese|whipped cream|relish|salsa verde|hot sauce")
NON_MEAL_ITEM_SQL = (
    "dressing|vinaigrette|sauce|syrup|butter|spread|jam|jelly|condiment|dip|"
    "seasoning|vinegar|ketchup|mustard|mayonnaise|soda|juice|coffee|tea|water|"
    "milk|cream cheese|whipped cream|relish")


def section_rank(section: str | None, name: str | None = None) -> int:
    """0 = looks like a meal, 1 = unclassified, 2 = dessert/condiment/drink.

    The ITEM name is checked first and can only demote, so a dressing served at a
    salad bar is not mistaken for a meal.
    """
    import re as _re
    if name and _re.search(NON_MEAL_ITEM_PATTERN, name.lower()):
        return 2
    s = (section or "").lower()
    # NON-MEAL IS CHECKED BEFORE MEAL: a "Salad Bar" contains the word "salad"
    # but is a component station, so demotion must win.
    if _re.search(NON_MEAL_SECTION_PATTERN, s):
        return 2
    if _re.search(MEAL_SECTION_PATTERN, s):
        return 0
    return 1
_DEPARTURE_LIMIT = 10       # rows per departures board

# Places that are BOTH in config.PLACES (walkable, have coordinates) AND dining
# locations. Locations without coordinates cannot anchor a walk leg.
DINING_PLACES: dict[str, str] = {
    "15": "D2 at Dietrick Hall",
    "39": "Owens Food Court",
}

EVENTS_REASON = (
    "events feed not built: no scraper exists for events.vt.edu "
    "(SDD.md section 5.6, known gap). Returning no data rather than "
    "inventing events."
)
NO_MODEL_BASIS = "no_model"


# ---------------------------------------------------------------------------
# Source protocol
# ---------------------------------------------------------------------------
class Source(Protocol):
    """Where the tools read from. One object, five methods -- nothing else.

    The same tools run against the local modules today (LocalSource) and
    against Unity Catalog Delta tables inside Databricks tomorrow; only this
    surface changes.
    """

    def next_departures(self, stop_id: str, route_id: str | None,
                        horizon_min: int) -> list[dict]: ...

    def live_buses(self, route_id: str | None) -> list[dict]: ...

    def eat_options(self, location_num: str | None, diet: str | None,
                    avoid: tuple[str, ...], max_kcal: float | None,
                    open_only: bool) -> list[dict]: ...

    def hours(self, foodpro_id: str) -> list[dict]: ...

    def events(self, date: str, tags: tuple[str, ...]) -> list[dict]: ...


class LocalSource:
    """Default Source: backed by hokieday.gtfs / livebus / dining.

    Never raises on bad ids or missing fixtures -- every method returns []
    (or None) and lets the tool layer attach a reason. The GTFS load is lazy
    and cached on the instance: load_gtfs() re-reads ~74k stop_times rows, so
    one LocalSource should be reused across calls (tools._LOCAL does exactly
    that).
    """

    def __init__(self) -> None:
        self._gtfs: Any = None

    def _g(self) -> Any:
        if self._gtfs is None:
            self._gtfs = gtfs.load_gtfs()
        return self._gtfs

    def _now_local(self) -> datetime:
        # The captured request clock, or the pinned replay clock in
        # DEMO_MODE=cache; never the raw wall clock.
        return _now_campus()

    # ------------------------------------------------------------- transit
    def next_departures(self, stop_id: str, route_id: str | None,
                        horizon_min: int) -> list[dict]:
        try:
            deps = gtfs.next_departures(
                self._g(), str(stop_id), self._now_local(),
                horizon_min=int(horizon_min), route_id=route_id,
                limit=_DEPARTURE_LIMIT,
            )
        except Exception:                                    # noqa: BLE001
            return []
        return [
            {
                "stop_id": d.stop_id,
                "route_id": d.route_id,
                "trip_id": d.trip_id,
                "head_sign": d.head_sign,
                "dep_time": d.dep_time,          # campus-local datetime
                "in_min": round(float(d.in_min), 1),
            }
            for d in deps
        ]

    def live_buses(self, route_id: str | None) -> list[dict]:
        try:
            # Pass the request clock explicitly so schedule deltas and staleness
            # labels are measured against the SAME instant as the plan window.
            rows = livebus.live(now=self._now_local())
        except Exception:                                    # noqa: BLE001
            return []
        out = []
        for r in rows:
            if route_id is not None and r.route_id != route_id:
                continue
            out.append({
                "bus_id": r.bus_id,
                "route_id": r.route_id,
                "stop_id": r.stop_id,
                "load_pct": r.load_pct,
                "is_at_stop": r.at_stop,
                "sched_delta_min": r.sched_delta_min,
                "gtfs_trip_id": r.gtfs_trip_id,
                "lat": r.lat,
                "lon": r.lon,
                "observed_at": r.observed_at.isoformat(),
                "is_stale": r.is_stale,
            })
        return out

    # -------------------------------------------------------------- dining
    def food_search(self, location_num: str | None, diet: str | None,
                    avoid: tuple[str, ...], max_kcal: float | None,
                    open_only: bool, max_age_s: float | None = None) -> dict:
        """Search one location, or ALL configured locations, without losing any.

        Returns {items, sources_ok, sources_skipped, statuses,
        nutrition_attached}. Every configured location lands in exactly one of
        sources_ok (reachable) or sources_skipped (unreachable, with status + a
        typed reason) -- a location is NEVER silently dropped.

        NUTRITION: for a single-location query real macros are attached (the
        planner can quote kcal). For an all-locations query nutrition is NOT
        fetched -- a campus-wide NutritiveReport sweep per search is exactly the
        cost blow-up this layer must avoid, so kcal is left unknown there.

        A requested `max_kcal` is a HARD ceiling. When the single location's
        nutrition is unavailable -- including nutrition captured after the
        replay clock, which is refused as non-contemporaneous -- the location is
        reported with a typed `nutrition_unavailable` skipped state instead of
        returning rows whose calories are unproven.
        """
        today = self._now_local().date()
        if location_num in (None, ""):
            nums = sorted(config.DINING_LOCATIONS)
            with_nutrition = False
        else:
            nums = [str(location_num)]
            with_nutrition = True

        # A kcal ceiling is a HARD constraint. Without per-location nutrition we
        # cannot prove ANY item satisfies it, so an all-locations kcal query
        # returns NO items -- never unproven ones -- with a typed skipped state.
        if max_kcal is not None and not with_nutrition:
            return {
                "items": [],
                "sources_ok": [],
                "sources_skipped": [{
                    "location_num": num,
                    "location_name": config.DINING_LOCATIONS.get(num, ""),
                    "status": "nutrition_unavailable",
                    "reason": ("kcal ceiling requires per-location nutrition; "
                               "a campus-wide search does not fetch it"),
                } for num in nums],
                "statuses": [],
                "nutrition_attached": False,
                "reason": ("max_kcal is a hard constraint but no campus-wide "
                           "nutrition was fetched; refusing to return items "
                           "whose calories are unverified. Query one location "
                           "or omit max_kcal."),
            }

        rows: list[dict] = []
        sources_ok: list[str] = []
        sources_skipped: list[dict] = []
        statuses: list[dict] = []
        for num in nums:
            name = config.DINING_LOCATIONS.get(num, "")
            statuses.append(dining.location_status(
                num, today, max_age_s=max_age_s).as_dict())
            # Prove calories BEFORE filtering when a hard ceiling is requested:
            # a future-captured/absent nutrition source must surface as a typed
            # skipped state, not as a silent "no items matched".
            nut: dict = {}
            if with_nutrition:
                try:
                    nut = dining.nutrition_for_location(
                        num, today, max_age_s=max_age_s)
                except Exception:                            # noqa: BLE001
                    nut = {}
                if max_kcal is not None and not nut:
                    sources_skipped.append({
                        "location_num": num, "location_name": name,
                        "status": "nutrition_unavailable",
                        "reason": ("max_kcal is a hard constraint but nutrition "
                                   "is unavailable (missing, or captured after "
                                   "the replay clock); refusing unproven rows"),
                    })
                    continue
            try:
                items = dining.eat_options(
                    num, today, diet=diet, avoid=tuple(avoid or ()),
                    max_kcal=max_kcal,
                    open_only=open_only,
                    max_age_s=max_age_s,
                )
            except Exception as exc:                         # noqa: BLE001
                # CacheMiss in replay mode (no fixture for this location), a
                # bad location number, or a MenuError: record it, never raise
                # and never drop it silently.
                sources_skipped.append({
                    "location_num": num, "location_name": name,
                    "status": dining.STATUS_UNAVAILABLE, "reason": str(exc),
                })
                continue
            sources_ok.append(num)
            # Nutrition is a cached per-location lookup; attach real macros so
            # the agent can quote a calorie count for a single-location pick.
            for it in items:
                n = nut.get(it.recipe_id)
                rows.append({
                    "name": it.name,
                    "location_num": it.location_num,
                    "section": it.section,
                    "meal": it.meal,
                    "kcal": round(n.cals, 1) if n is not None else None,
                    "protein_g": round(n.protein_g, 1) if n is not None else None,
                    "allergens": list(it.allergens),
                    "diet_tags": list(it.diet_tags),
                    # SAFETY (SDD risk R8): a BLANK allergen field means
                    # UNKNOWN, never allergen-free. The flag -- not the
                    # absence -- is what the agent must surface.
                    "allergens_known": bool(it.allergens),
                    # True only for the documented D2 Viridian kitchen (location
                    # + section bound together, never section text alone).
                    "venue_allergen_free": config.is_venue_allergen_free(
                        it.section, it.location_num),
                    "recipe_id": it.recipe_id,
                    "portion": f"{it.portion_size} {it.portion_unit}".strip(),
                })
        return {
            "items": rows,
            "sources_ok": sources_ok,
            "sources_skipped": sources_skipped,
            "statuses": statuses,
            "nutrition_attached": with_nutrition,
        }

    def eat_options(self, location_num: str | None, diet: str | None,
                    avoid: tuple[str, ...], max_kcal: float | None,
                    open_only: bool) -> list[dict]:
        """Frozen Source-protocol method: the item rows from food_search()."""
        return self.food_search(location_num, diet, avoid, max_kcal,
                                open_only)["items"]

    def hours(self, foodpro_id: str) -> list[dict]:
        now = self._now_local()
        try:
            # `open_windows` adds the PREVIOUS day's real overnight windows when
            # `now` is early enough to fall inside one, so an early-morning plan
            # does not miss a 22:00->02:00 window that began yesterday. A prior
            # day that cannot be read is treated as no windows here (the dining
            # status layer reports it as unknown); it is never invented by
            # shifting today's windows backward.
            wins, _prior = dining.open_windows(
                str(foodpro_id), now.date(), now.replace(tzinfo=None))
        except Exception:                                    # noqa: BLE001
            return []
        out = []
        for w in wins:
            # Overnight windows (close < open) must roll the close to the next
            # day, or a 22:00->02:00 window can never read as open. window_span
            # is the shared resolver so this path and dining.is_open agree.
            open_dt, close_dt = dining.window_span(w)
            out.append({
                "foodpro_id": w.foodpro_id,
                "name": w.name,
                "date": w.date.isoformat(),
                "open_time": w.open_time,
                "close_time": w.close_time,
                "open_dt": open_dt,
                "close_dt": close_dt,
            })
        return out

    # -------------------------------------------------------------- events
    def events(self, date: str, tags: tuple[str, ...]) -> list[dict]:
        # Deliberately empty: the events scrape does not exist yet (SDD 5.6).
        # We do not fabricate rows to make the demo look fuller.
        return []

    # -------------------------------------------------- LocalSource extras
    # NOT part of the frozen Source protocol. plan_day reaches them via
    # getattr and degrades to a walk-only itinerary when a Source lacks them.
    def nearest_stop(self, place: str) -> dict | None:
        """Nearest GTFS stop to a named config.PLACES place."""
        p = _place(place)
        if p is None:
            return None
        try:
            scored = gtfs.nearest_stops(self._g(), p["lat"], p["lon"], k=1)
        except Exception:                                    # noqa: BLE001
            return None
        if not scored:
            return None
        dist, stop = scored[0]
        return {
            "stop_id": stop.stop_id,
            "name": stop.name,
            "distance_m": round(dist, 1),
            "lat": stop.lat,
            "lon": stop.lon,
            "place_coords": (p["lat"], p["lon"]),
        }

    def ride_minutes(self, stop_a: str, stop_b: str,
                     after: datetime | None = None, route_id: str | None = None,
                     horizon_min: int = 180) -> dict | None:
        """Earliest scheduled ride stop_a -> stop_b departing at/after `after`.

        Ride time is READ off the same trip's stop_times (arrival at b minus
        departure at a) -- computed, never guessed. Returns None when no trip
        serves both stops in order within the horizon (never raises).
        """
        try:
            at = after if after is not None else self._now_local()
            deps = gtfs.next_departures(
                self._g(), str(stop_a), at,
                horizon_min=int(horizon_min), route_id=route_id, limit=25,
            )
            for d in deps:
                rows = self._g().stop_times.get(d.trip_id) or []
                ia = ib = None
                for i, (_seq, sid, _arr, _dep) in enumerate(rows):
                    if str(sid) == str(stop_a):
                        ia = i
                    elif str(sid) == str(stop_b):
                        ib = i
                if ia is None or ib is None or ib <= ia:
                    continue
                # int seconds after service-day midnight (INTERFACES 1 pin)
                ride = (rows[ib][2] - rows[ia][3]) / 60.0
                if 0 < ride <= horizon_min:
                    return {
                        "route_id": d.route_id,
                        "trip_id": d.trip_id,
                        "from_stop": str(stop_a),
                        "to_stop": str(stop_b),
                        "dep_time": d.dep_time,
                        "arrive_time": d.dep_time + timedelta(minutes=ride),
                        "ride_min": round(ride, 1),
                    }
        except Exception:                                    # noqa: BLE001
            return None
        return None


_LOCAL = LocalSource()


def _src(source: Any) -> Any:
    return source if source is not None else _LOCAL


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _place(name: str) -> dict | None:
    """Case-insensitive lookup in config.PLACES (a COPY of the row, or None).

    Delegates to config.find_place so the registry is read under its lock and
    never iterated live: the demo server is threaded and a concurrent
    registration/eviction must not race this read.
    """
    return config.find_place(name)


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * 6_371_000.0 * math.asin(math.sqrt(h))


def _xy(p: dict | None) -> tuple[float, float] | None:
    """(lat, lon) from a place row, or None. Used to put leg geometry on a map."""
    if not p or "lat" not in p or "lon" not in p:
        return None
    return (float(p["lat"]), float(p["lon"]))


def _walk_result(from_place: str, to_place: str) -> dict:
    a, b = _place(from_place), _place(to_place)
    if a is None or b is None:
        unknown = from_place if a is None else to_place
        # STATIC only: a dynamic key embeds a device's rounded coordinates and
        # must never be echoed back to other sessions (see config.static_place_keys).
        known = ", ".join(config.static_place_keys())
        return {
            "error": f"unknown place {unknown!r}; known places: {known}",
            "from_place": str(from_place), "to_place": str(to_place),
            "minutes": None, "meters": None, "method": None,
        }
    metres = _haversine_m((a["lat"], a["lon"]), (b["lat"], b["lon"]))
    path_m = metres * config.WALK_PATH_FACTOR
    minutes = path_m / config.WALK_SPEED_MPS / 60.0
    verified = bool(a.get("verified")) and bool(b.get("verified"))
    return {
        "from_place": str(from_place),
        "to_place": str(to_place),
        "minutes": round(minutes, 1),
        "meters": round(path_m, 0),
        "method": ("haversine straight-line x 1.30 path factor at 1.35 m/s "
                   + ("(verified coords)" if verified
                      else "(coordinates UNVERIFIED for this pair)")),
        "coords_verified": verified,
    }


def _walk_minutes(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Walk minutes between two raw (lat, lon) pairs.

    Mirrors _walk_result's formula exactly (haversine x path factor at walking
    speed) so that whole-place walks and stop-to-stop walks stay consistent.
    Both call sites in _build_itinerary pass coordinates, not place names.
    """
    metres = _haversine_m(a, b) * config.WALK_PATH_FACTOR
    return metres / config.WALK_SPEED_MPS / 60.0


def _now_campus() -> datetime:
    """Campus-local 'now': the captured request clock when plan_day set one,
    else config.now() (the pinned replay clock in DEMO_MODE=cache)."""
    pinned = _REQUEST_NOW.get()
    if pinned is not None:
        return pinned.astimezone(_CAMPUS_TZ)
    return config.now(_CAMPUS_TZ)


def _now_naive() -> datetime:
    return _now_campus().replace(tzinfo=None)


def _naive(dt: datetime) -> datetime:
    """Strip tz to campus-local NAIVE time.

    Hours windows (open_dt/close_dt) are built NAIVE by LocalSource via
    datetime.combine, while itinerary times (`t`, `eat_start`) are AWARE. Mixing
    them raises "can't compare offset-naive and offset-aware datetimes". That bug
    stayed hidden because the eat leg was never reached (the meal always failed
    the calorie filter first), so it only surfaced once meals started working.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(_CAMPUS_TZ).replace(tzinfo=None)
    return dt


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _human_time(value: datetime | str) -> str:
    """Compact campus-local clock text for user-facing rationale."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(_CAMPUS_TZ)
    return dt.strftime("%I:%M %p").lstrip("0")


def _parse_campus(s: Any) -> datetime:
    """Parse a window bound: 'HH:MM' (today, campus time) or an ISO datetime."""
    txt = str(s).strip()
    parts = txt.split(":")
    if (len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit()
            and "-" not in txt and "T" not in txt and " " not in txt):
        base = _now_campus()
        return base.replace(hour=int(parts[0]), minute=int(parts[1]),
                            second=0, microsecond=0)
    dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_CAMPUS_TZ)
    return dt.astimezone(_CAMPUS_TZ)


# ===========================================================================
# public tools
# ===========================================================================
def get_next_departures(stop_id: str, route_id: str | None = None,
                        horizon_min: int = config.DEPARTURE_HORIZON_MIN,
                        source: Any = None) -> dict:
    """Next departures board for one stop, service-filtered (calendar_dates.txt
    only -- see INTERFACES section 1 for why that filter is the single most
    important correctness rule in this codebase).

    Each row: route_id, head_sign, dep_time (ISO, campus-local), in_min,
    is_realtime. is_realtime is True only when a NON-STALE live vehicle is
    running that exact trip right now; otherwise the row is schedule-only.
    """
    src = _src(source)
    try:
        rows = src.next_departures(str(stop_id), route_id, int(horizon_min))
    except Exception as exc:                                 # noqa: BLE001
        return {
            "stop_id": str(stop_id), "route_id": route_id,
            "horizon_min": int(horizon_min), "departures": [],
            "reason": f"departures unavailable: {exc}",
        }
    live_trips: set[str] = set()
    try:
        live_trips = {
            r.get("gtfs_trip_id") for r in src.live_buses(None)
            if r.get("gtfs_trip_id") and not r.get("is_stale")
        }
    except Exception:                                        # noqa: BLE001
        pass
    out = []
    for r in rows:
        dep_time = r.get("dep_time")
        out.append({
            "stop_id": r.get("stop_id"),
            "route_id": r.get("route_id"),
            "head_sign": r.get("head_sign"),
            "trip_id": r.get("trip_id"),
            "dep_time": _iso(dep_time) if isinstance(dep_time, datetime) else dep_time,
            "in_min": r.get("in_min"),
            "is_realtime": bool(r.get("trip_id") and r["trip_id"] in live_trips),
        })
    reason = None
    if not out:
        reason = (f"no scheduled departures for stop {stop_id!r} within "
                  f"{int(horizon_min)} min (check the stop id and the service day)")
    return {
        "stop_id": str(stop_id), "route_id": route_id,
        "horizon_min": int(horizon_min),
        "departures": out, "reason": reason,
    }


def get_live_bus(route_id: str | None = None, source: Any = None) -> dict:
    """One live-vehicle snapshot, optionally filtered to a route.

    Each row: bus_id, route_id, stop_id, load_pct (from percentOfCapacity --
    `capacity` is internally inconsistent and must not be used as a
    denominator), is_at_stop, sched_delta_min (+late / None if unmatched).
    Top-level `stale` is True if ANY row is older than
    config.STALE_LIVE_MINUTES.
    """
    src = _src(source)
    try:
        rows = src.live_buses(route_id)
    except Exception as exc:                                 # noqa: BLE001
        return {"route_id": route_id, "count": 0, "stale": True, "buses": [],
                "reason": f"live bus data unavailable: {exc}"}
    out = []
    for r in rows:
        out.append({
            "bus_id": r.get("bus_id"),
            "route_id": r.get("route_id"),
            "stop_id": r.get("stop_id"),
            "load_pct": r.get("load_pct"),
            "is_at_stop": r.get("is_at_stop"),
            "sched_delta_min": r.get("sched_delta_min"),
            "lat": r.get("lat"),
            "lon": r.get("lon"),
            "observed_at": r.get("observed_at"),
            "is_stale": bool(r.get("is_stale")),
        })
    stale = any(r["is_stale"] for r in out)
    reason = None
    if not out:
        reason = (f"no live vehicles on route {route_id!r}"
                  if route_id else "no live vehicles in the snapshot")
    elif stale:
        reason = ("some rows are older than STALE_LIVE_MINUTES; "
                  "treat as schedule-only")
    return {
        "route_id": route_id,
        "count": len(out),
        "stale": stale,
        "buses": out,
        "reason": reason,
    }


def walk_time(from_place: str, to_place: str) -> dict:
    """Walking estimate between two named config.PLACES places.

    Returns {minutes, meters, method}. Raise-free by contract: an unknown
    place yields {"error": ...} with minutes/meters None, never an exception.
    """
    try:
        return _walk_result(from_place, to_place)
    except Exception as exc:                                 # noqa: BLE001
        return {"error": f"walk_time failed: {exc}", "minutes": None,
                "meters": None, "method": None}


def find_food(location_num: str | None = None, diet: str | None = None,
              avoid: tuple[str, ...] = (), max_kcal: float | None = None,
              open_only: bool = False, source: Any = None,
              max_age_s: float | None = config.DEFAULT_MENU_CACHE_MAX_AGE_S
              ) -> dict:
    """Menu items matching diet / allergen / kcal constraints -- one dining
    location, or every configured location when location_num is None.

    THE ALLERGEN RULE (SDD risk R8): `avoid` is a HARD filter applied
    upstream (case-insensitive substring). A BLANK allergen field means
    UNKNOWN, never allergen-free: such rows carry allergens_known=False and
    are never presented as safe. Requested diet tags are also cross-checked
    against declared allergens so a self-contradictory source row is not
    recommended (for example, a row tagged vegan that declares Eggs).

    PARTIAL SUCCESS: an all-locations search reports `sources_ok` (reachable)
    and `sources_skipped` (unreachable, each with status + reason) so a location
    that failed upstream is visible, never silently dropped. Ranking stays
    deterministic: meal-ish section first, then most filling, then name.
    """
    src = _src(source)
    if isinstance(avoid, str):
        avoid = (avoid,)
    avoid = tuple(avoid or ())
    loc = None if location_num in (None, "") else str(location_num)
    sources_ok: list[str] | None = None
    sources_skipped: list[dict] = []
    statuses: list[dict] = []
    search_reason: str | None = None
    try:
        search = getattr(src, "food_search", None)
        if callable(search):
            res = search(loc, diet, avoid, max_kcal, bool(open_only),
                         max_age_s=max_age_s)
            rows = res.get("items") or []
            sources_ok = res.get("sources_ok")
            sources_skipped = res.get("sources_skipped") or []
            statuses = res.get("statuses") or []
            search_reason = res.get("reason")
        else:
            # A Source that only implements the frozen protocol: partial-success
            # bookkeeping is unavailable, but the rows still work.
            rows = src.eat_options(loc, diet, avoid, max_kcal, bool(open_only))
            sources_ok = None if loc is None else [loc]
    except Exception as exc:                                 # noqa: BLE001
        return {"location_num": location_num, "diet": diet, "avoid": list(avoid),
                "max_kcal": max_kcal, "open_only": bool(open_only),
                "count": 0, "items": [], "sources_ok": sources_ok,
                "sources_skipped": sources_skipped, "statuses": statuses,
                "reason": f"menu data unavailable: {exc}"}
    # Rank: meal-ish section first, then dessert/condiment/drink, then closeness
    # to a MEAL-sized target. Three naive rankings failed visibly: cheapest-first
    # gave "Cinnamon Apples (82 kcal)", calorie-distance gave "Oreo Cobbler Cake",
    # and a dessert penalty gave "Bleu Cheese Dressing". Unknown calories sort
    # last; ties break on name so the choice is reproducible.
    # Tier by section classification (meals first, desserts/condiments/drinks
    # last), then MOST FILLING first, then name for reproducibility. See
    # MEAL_RANKING_RULE for the four approaches this replaced.
    rows.sort(key=lambda r: (
        section_rank(str(r.get("section")), str(r.get("name"))),
        -((r["kcal"] if r.get("kcal") is not None else -1e9)),
        str(r.get("name", "")),
    ))

    reason = None
    if not rows:
        reason = search_reason or (
            "no menu items matched (allergen filters are hard filters; "
            "a kcal ceiling needs nutrition data, which may be "
            "unavailable offline; open_only=True drops closed locations)")
    return {
        "location_num": location_num, "diet": diet, "avoid": list(avoid),
        "max_kcal": max_kcal, "open_only": bool(open_only),
        "count": len(rows), "items": rows, "reason": reason,
        "sources_ok": sources_ok, "sources_skipped": sources_skipped,
        "statuses": statuses,
    }


def get_hours(foodpro_id: str, source: Any = None) -> dict:
    """Opening windows for a FoodPro center today, each with is_open_now and
    closes_in_min evaluated at the pinned campus clock."""
    src = _src(source)
    try:
        rows = src.hours(str(foodpro_id))
    except Exception as exc:                                 # noqa: BLE001
        return {"foodpro_id": str(foodpro_id), "windows": [],
                "reason": f"hours unavailable: {exc}"}
    now = _now_naive()
    out = []
    for w in rows:
        open_dt, close_dt = w.get("open_dt"), w.get("close_dt")
        closes_in = None
        if close_dt is not None:
            closes_in = round((close_dt - now).total_seconds() / 60, 1)
        out.append({
            "foodpro_id": w.get("foodpro_id"),
            "name": w.get("name"),
            "date": w.get("date"),
            "open_time": w.get("open_time"),
            "close_time": w.get("close_time"),
            "is_open_now": bool(
                open_dt is not None and close_dt is not None
                and open_dt <= now < close_dt),
            "closes_in_min": closes_in,
        })
    reason = None
    if not out:
        reason = (f"no hours on file for foodpro_id {foodpro_id!r} today "
                  "(unknown id, or no fixture for this date)")
    return {"foodpro_id": str(foodpro_id), "windows": out, "reason": reason}


def get_events(date: str = "today", tags: tuple[str, ...] = (),
               source: Any = None) -> dict:
    """Campus events for a date. ALWAYS [] + reason today: the events.vt.edu
    scrape has not been built (SDD 5.6 known gap) and we do not invent events."""
    src = _src(source)
    tags = tuple(tags or ())
    try:
        rows = src.events(str(date), tags)
    except Exception as exc:                                 # noqa: BLE001
        return {"date": str(date), "tags": list(tags), "events": [],
                "reason": f"events lookup failed: {exc}"}
    if not rows:
        return {"date": str(date), "tags": list(tags), "events": [],
                "reason": EVENTS_REASON}
    return {"date": str(date), "tags": list(tags), "events": rows,
            "reason": None}


def predict_bus_delay(route_id: str, hour: int, source: Any = None) -> dict:
    """Predicted lateness for a route at an hour of day.

    NO MODEL EXISTS YET, so this returns expected_delta_min=None,
    confidence=0.0, basis="no_model". We refuse to fabricate a prediction
    (README: never emit a number the code did not compute; SDD section 9).
    """
    return {
        "route_id": str(route_id),
        "hour": int(hour),
        "expected_delta_min": None,
        "confidence": 0.0,
        "basis": NO_MODEL_BASIS,
        "note": ("no lateness model is trained yet; the bronze dataset is "
                 "still being collected by the bus poller"),
    }


# ---------------------------------------------------------------------------
# plan_day -- the orchestrator (SDD 7.3)
# ---------------------------------------------------------------------------
STUDENT_PROFILES: dict[str, dict] = {
    # Obviously synthetic demo profiles: surrogate keys only -- no real names,
    # no PIDs, no academic data (SDD section 11).
    #
    # NO HIDDEN CALORIE CEILING. These profiles are demo aids, not user input,
    # and a hidden ceiling would silently drop the demo's meal whenever replay
    # nutrition is unavailable (it is: the committed D2 nutrition chunks were
    # captured after the pinned replay clock and are refused as
    # non-contemporaneous). An EXPLICIT max_kcal from the caller stays a hard
    # constraint in find_food/eat_options.
    "demo-student-1": {
        "label": "synthetic demo profile 1 (not a real person)",
        "diet": "vegetarian",
        "avoid": ["Peanuts", "Tree Nuts"],
        "max_kcal": None,
    },
    "demo-student-2": {
        "label": "synthetic demo profile 2 (not a real person)",
        "diet": "vegan",
        "avoid": ["Sesame"],
        "max_kcal": None,
    },
    "demo-student-3": {
        "label": "synthetic demo profile 3 (not a real person)",
        "diet": None,
        "avoid": ["Milk"],
        "max_kcal": None,
    },
}


def _plan_prefs(student_ref: str, prefs: dict | None) -> dict:
    """Explicit prefs override the (obviously synthetic) demo profile."""
    merged = dict(prefs or {})
    prof = STUDENT_PROFILES.get(str(student_ref).strip())
    if prof:
        for k, v in prof.items():
            if k != "label":
                merged.setdefault(k, v)
    return merged


def _build_itinerary(src: Any, *, start_dt: datetime, end_dt: datetime,
                     from_place: str, to_place: str, eat_loc: str | None,
                     diet: str | None, avoid: tuple[str, ...],
                     max_kcal: float | None, route_id: str | None,
                     skip_bus: bool = False) -> dict:
    """One itinerary. Deterministic; every minute comes from a tool result or
    from the declared EAT_MINUTES assumption. Cumulative times are built from
    the ROUNDED leg minutes so leave + sum(leg minutes) == arrive exactly."""
    legs: list[dict] = []
    notes: list[str] = []
    t = start_dt

    eat_place = DINING_PLACES.get(str(eat_loc)) if eat_loc else None
    item = None
    if eat_place:
        food = find_food(location_num=str(eat_loc), diet=diet, avoid=avoid,
                         max_kcal=max_kcal, source=src)
        if food["items"]:
            item = food["items"][0]
        else:
            notes.append(f"no menu item matched the constraints at {eat_place}; "
                         "eat leg dropped")

    eat_start = None
    waypoint = from_place
    # Coordinates per leg, so a map can be drawn FROM THE PLAN. The leg `from`/
    # `to` fields are display strings for humans ("stop 1125 (Tennis Courts)"),
    # and re-parsing them to draw would be both fragile and wrong-shaped.
    origin_p = _place(from_place) or {}
    dest_p = _place(to_place) or {}
    eat_p = _place(eat_place) or {}
    if eat_place:
        w1 = _walk_result(from_place, eat_place)
        if "error" in w1:
            notes.append(w1["error"])
        else:
            legs.append({"seq": len(legs) + 1, "type": "walk",
                         "from": from_place, "to": eat_place,
                         "from_coords": _xy(origin_p), "to_coords": _xy(eat_p),
                         "start_time": _iso(t),
                         "minutes": w1["minutes"], "meters": w1["meters"],
                         "method": w1["method"]})
            t = t + timedelta(minutes=w1["minutes"])
            waypoint = eat_place
            if item is not None:
                eat_start = t
                # openness at the planned eat time, from the hours fixture.
                # Do NOT filter on w["date"] == t.date(): an overnight window
                # that began on D-1 has its close_dt on D and must still count.
                open_now = any(
                    w["open_dt"] <= _naive(t) < w["close_dt"]
                    for w in src.hours(str(eat_loc))
                )
                if open_now:
                    eat_end = t + timedelta(minutes=EAT_MINUTES)
                    legs.append({
                        "seq": len(legs) + 1, "type": "eat",
                        "location_num": str(eat_loc), "place": eat_place,
                        "coords": _xy(eat_p),
                        "item": item["name"], "kcal": item.get("kcal"),
                        "protein_g": item.get("protein_g"),
                        "portion": item.get("portion"),
                        "allergens": item.get("allergens"),
                        "allergens_known": item.get("allergens_known"),
                        "venue_allergen_free": item.get("venue_allergen_free"),
                        "diet_tags": item.get("diet_tags") or [],
                        "requested_diet": diet,
                        "avoid": list(avoid),
                        "start_time": _iso(eat_start), "end_time": _iso(eat_end),
                        "minutes": EAT_MINUTES,
                        "method": (f"assumed eating time ({EAT_MINUTES:g} min); "
                                   f"item from location {eat_loc} menu"),
                    })
                    t = eat_end
                else:
                    notes.append(f"{eat_place} is closed at the planned eat "
                                 "time; eat leg dropped")
                    eat_start = None

    # ---- transit (optional): stop near waypoint -> stop near to_place ----
    bus = None
    if not skip_bus:
        ns_from = getattr(src, "nearest_stop", None)
        ride_fn = getattr(src, "ride_minutes", None)
        if ns_from is not None and ride_fn is not None:
            sa = ns_from(waypoint)
            sd = ns_from(to_place)
            if sa and sd and sa["stop_id"] != sd["stop_id"]:
                ride = ride_fn(sa["stop_id"], sd["stop_id"], after=t,
                               route_id=route_id)
                if ride:
                    w2min = round(_walk_minutes((sa["place_coords"][0],
                                                 sa["place_coords"][1]),
                                                (sa["lat"], sa["lon"])), 1)
                    if w2min > 0:
                        legs.append({"seq": len(legs) + 1, "type": "walk",
                                     "from": waypoint,
                                     "to": f"{sa['name']} bus stop",
                                     "from_coords": tuple(sa["place_coords"]),
                                     "to_coords": (sa["lat"], sa["lon"]),
                                     "start_time": _iso(t), "minutes": w2min,
                                     "method": "walk to boarding stop"})
                        t = t + timedelta(minutes=w2min)
                    wait = round(max((ride["dep_time"] - t).total_seconds() / 60,
                                     0.0), 1)
                    b_minutes = round(wait + ride["ride_min"], 1)
                    b_end = t + timedelta(minutes=b_minutes)
                    legs.append({
                        "seq": len(legs) + 1, "type": "bus",
                        "route_id": ride["route_id"],
                        "trip_id": ride["trip_id"],
                        "from": sa["name"], "to": sd["name"],
                        "from_stop": ride["from_stop"],
                        "to_stop": ride["to_stop"],
                        "from_stop_name": sa["name"],
                        "to_stop_name": sd["name"],
                        "from_coords": (sa["lat"], sa["lon"]),
                        "to_coords": (sd["lat"], sd["lon"]),
                        "dep_time": _iso(ride["dep_time"]),
                        "arrive_time": _iso(b_end),
                        "wait_min": wait, "ride_min": ride["ride_min"],
                        "minutes": b_minutes,
                        "is_realtime": False,
                        "method": "GTFS schedule (service-filtered)",
                    })
                    t = b_end
                    bus = legs[-1]
                    # walk from the alighting stop to the destination
                    w3min = round(_walk_minutes((sd["lat"], sd["lon"]),
                                                (sd["place_coords"][0],
                                                 sd["place_coords"][1])), 1)
                    legs.append({"seq": len(legs) + 1, "type": "walk",
                                 "from": f"{sd['name']} bus stop",
                                 "to": to_place, "start_time": _iso(t),
                                 "from_coords": (sd["lat"], sd["lon"]),
                                 "to_coords": _xy(dest_p),
                                 "minutes": w3min,
                                 "method": "walk from alighting stop"})
                    t = t + timedelta(minutes=w3min)

    if bus is None:
        # walk-only fallback (also the shape of itinerary B after a bus trigger)
        if waypoint != to_place:
            w4 = _walk_result(waypoint, to_place)
            if "error" in w4:
                notes.append(w4["error"])
            else:
                legs.append({"seq": len(legs) + 1, "type": "walk",
                             "from": waypoint, "to": to_place,
                             "from_coords": _xy(eat_p if waypoint == eat_place
                                                else origin_p),
                             "to_coords": _xy(dest_p),
                             "start_time": _iso(t), "minutes": w4["minutes"],
                             "meters": w4["meters"], "method": w4["method"]})
                t = t + timedelta(minutes=w4["minutes"])

    total = round((t - start_dt).total_seconds() / 60, 1)
    slack = round((end_dt - t).total_seconds() / 60, 1)

    # closing-time margin: minutes from the eat START to the window's close
    eat_close_in = None
    if eat_start is not None and eat_loc:
        eat_start_naive = _naive(eat_start)      # windows are naive campus-local
        closes = [w["close_dt"] for w in src.hours(str(eat_loc))
                  if w.get("close_dt") is not None
                  and w["close_dt"] >= eat_start_naive]
        if closes:
            eat_close_in = round(
                (min(closes) - eat_start_naive).total_seconds() / 60, 1)

    return {
        "legs": legs,
        "leave_time": _iso(start_dt),
        "arrive_time": _iso(t),
        "window_end": _iso(end_dt),
        "total_min": total,
        "slack_min": slack,
        "arrives_in_window": t <= end_dt,
        "used_bus": bus is not None,
        "eat_start": _iso(eat_start) if eat_start else None,
        "eat_end": (_iso(eat_start + timedelta(minutes=EAT_MINUTES))
                    if eat_start else None),
        "eat_close_in_min": eat_close_in,
        "notes": notes,
    }


def _replan_trigger(src: Any, itin: dict) -> dict | None:
    """Re-check LIVE state against itinerary A (SDD 7.3). Any one fires."""
    # -- bus triggers: only when the plan actually rides a bus
    if itin.get("used_bus"):
        bus_leg = next((l for l in itin["legs"] if l["type"] == "bus"), None)
        if bus_leg is not None:
            # TOLERANCE IS THE BOARDING BUFFER, NOT THE WINDOW SLACK.
            # The plan aims to reach the stop `wait_min` before departure and
            # needs MIN_BOARD_BUFFER_MIN to board, so the margin it can absorb is
            # the planned wait minus that buffer. The original code compared the
            # bus's deviation against `slack_min` -- the slack to the WINDOW END,
            # ~62 min in practice -- so a bus would have needed to be an hour late
            # to trigger anything and the re-plan never fired at all.
            tolerance = max(
                0.0,
                float(bus_leg.get("wait_min") or 0.0) - MIN_BOARD_BUFFER_MIN)
            window_end = datetime.fromisoformat(itin["window_end"])
            live = get_live_bus(bus_leg["route_id"], source=src)
            for row in live["buses"]:
                delta = row.get("sched_delta_min")
                if delta is not None:
                    # Buses here run EARLY (observed -7.5 .. +7.5 min), so the
                    # realistic failure is missing one, not waiting for it.
                    if -delta > tolerance:
                        wait = float(bus_leg.get("wait_min") or 0.0)
                        remaining = max(0.0, wait + float(delta))
                        stop_name = (bus_leg.get("from_stop_name")
                                     or f"stop {bus_leg.get('from_stop')}")
                        return {
                            "cause": "bus_early",
                            "detail": (f"Route {row['route_id']} is "
                                       f"{abs(delta):.1f} min early, leaving "
                                       f"only {remaining:.1f} min to board at "
                                       f"{stop_name}; the plan requires a "
                                       f"{MIN_BOARD_BUFFER_MIN:g} min buffer."),
                        }
                    arrives = (datetime.fromisoformat(bus_leg["arrive_time"])
                               + timedelta(minutes=delta))
                    if arrives > window_end:
                        return {
                            "cause": "bus_late",
                            "detail": (f"route {row['route_id']} is running "
                                       f"+{delta:g} min, pushing arrival to "
                                       f"{arrives.strftime('%H:%M')}, past the "
                                       f"{window_end.strftime('%H:%M')} "
                                       f"deadline"),
                        }
                if (row.get("load_pct") or 0) >= config.BUS_FULL_PCT:
                    return {
                        "cause": "bus_full",
                        "detail": (f"bus {row['bus_id']} on route "
                                   f"{row['route_id']} is {row['load_pct']}% full "
                                   f"(threshold {config.BUS_FULL_PCT}%)"),
                    }
    # -- dining trigger: closes before walk + eat time is spent (SDD 7.3)
    if itin.get("eat_start"):
        eat_leg = next(l for l in itin["legs"] if l["type"] == "eat")
        walk_before_eat = sum(
            l["minutes"] for l in itin["legs"]
            if l["type"] == "walk"
            and str(l.get("start_time", "")) < itin["eat_start"]
        )
        eat_close_in = itin.get("eat_close_in_min")
        if eat_close_in is not None:
            # eat_close_in_min is measured from the planned EAT START; adding the
            # time from now until the eat start converts it to "minutes from now
            # until closing", which is what the budget below must be compared to.
            # _now_naive(), not _now_campus(): hours windows are NAIVE campus-local,
            # so the clock compared against them must be naive too.
            now = _now_naive()
            eat_start = _naive(datetime.fromisoformat(itin["eat_start"]))
            closes_from_now = eat_close_in + (
                eat_start - now
            ).total_seconds() / 60
            if closes_from_now < walk_before_eat + EAT_MINUTES:
                return {
                    "cause": "dining_closing",
                    "detail": (f"{eat_leg.get('place')} closes "
                               f"{eat_close_in:g} min after the planned eat "
                               f"start ({closes_from_now:g} min from now); the "
                               f"plan needs "
                               f"{walk_before_eat + EAT_MINUTES:g} min"),
                }
    # -- weather trigger: NOT IMPLEMENTED YET (no forecast source wired in).
    return None


def _alternative_eat_location(src: Any, current: str, eat_after: datetime,
                              need_min: float) -> str | None:
    """Another dining place open long enough at eat_after, else None."""
    for num in DINING_PLACES:
        if num == str(current):
            continue
        eat_after_naive = _naive(eat_after)      # windows are naive campus-local
        closes = [w["close_dt"] for w in src.hours(num)
                  if w.get("close_dt") is not None
                  and w["close_dt"] >= eat_after_naive]
        if closes and (min(closes) - eat_after_naive).total_seconds() / 60 >= need_min:
            return num
    return None


def plan_day(student_ref: str, start: str, end: str, prefs: dict | None = None,
             source: Any = None, now: datetime | None = None) -> dict:
    """Public entry point. `now` pins ONE campus-local request timestamp for
    every calculation inside the plan (see _REQUEST_NOW); when omitted the
    library falls back to config.now() exactly as before."""
    token = None
    if now is not None:
        aware = now if now.tzinfo is not None else now.replace(tzinfo=_CAMPUS_TZ)
        token = _REQUEST_NOW.set(aware.astimezone(_CAMPUS_TZ))
    try:
        return _plan_day_impl(student_ref, start, end, prefs=prefs, source=source)
    finally:
        if token is not None:
            _REQUEST_NOW.reset(token)


def _plan_day_impl(student_ref: str, start: str, end: str, prefs: dict | None = None,
                   source: Any = None) -> dict:
    """The orchestrator (SDD 7.3): interpret constraints, build itinerary A
    from walk_time + get_next_departures + find_food + get_hours, RE-CHECK
    live state via get_live_bus, and if reality moved build itinerary B and
    set replan_trigger = {cause, detail}.

    Never raises: malformed input returns a structured error dict the agent
    can narrate. The returned itinerary carries only surrogate student_ref --
    no PII (SDD section 11).
    """
    src = _src(source)
    p = _plan_prefs(student_ref, prefs)
    avoid = tuple(p.get("avoid") or ())
    diet = p.get("diet")
    max_kcal = p.get("max_kcal")
    eat = p.get("eat", True)
    eat_loc = None if eat is False else p.get("location_num", "15")
    route_hint = p.get("route_id")

    # A bus is only the DEFAULT when it actually beats walking (see below), but a
    # student may still prefer it (rain, luggage, injury). Parsed before the
    # place check so every return carries the same `constraints` block.
    prefer = str(((prefs or {}).get("prefer") or "fastest")).lower()
    constraints = {
        "diet": diet,
        "avoid": list(avoid),
        "max_kcal": max_kcal,
        "prefer": prefer,
        "from_place": str(p.get("from_place", "Burruss Hall")),
        "to_place": str(p.get("to_place", "McBryde Hall")),
    }

    try:
        start_dt = _parse_campus(start)
        end_dt = _parse_campus(end)
    except Exception as exc:                                 # noqa: BLE001
        return {
            "student_ref": str(student_ref), "itinerary": None,
            "rationale": f"could not parse the time window: {exc}",
            "constraints": constraints,
            "alternatives": [], "replan_trigger": None, "error": str(exc),
            "feasible": False,
            "infeasible_reason": {"code": "invalid_window",
                                  "detail": str(exc)},
        }
    if end_dt <= start_dt:
        return {
            "student_ref": student_ref, "itinerary": None,
            "rationale": ("That deadline is before the start time. If you mean "
                          "the afternoon, write 1:25 PM or 13:25."),
            "constraints": constraints,
            "alternatives": [], "replan_trigger": None,
            "error": "end must be after start",
            "feasible": False,
            "infeasible_reason": {"code": "invalid_window",
                                  "detail": "the deadline is before the start"},
        }

    from_place = constraints["from_place"]
    to_place = constraints["to_place"]

    # UNKNOWN PLACES ARE A HARD FAILURE, NOT AN EMPTY PLAN. `_walk_result` used
    # to record an unknown place as a note and return no legs, which produced a
    # 0-minute itinerary whose rationale still said a route "fits". An unknown
    # origin or destination now yields no itinerary plus a structured reason a
    # UI can bind to, and the never-raise contract is preserved.
    unknown: list[tuple[str, str]] = []
    if _place(from_place) is None:
        unknown.append(("from_place", from_place))
    if _place(to_place) is None:
        unknown.append(("to_place", to_place))
    if unknown:
        field, value = unknown[0]
        known = config.static_place_keys()
        label = {"from_place": "starting place", "to_place": "destination"}
        label = label.get(field, "place")
        reason = {
            "code": "unknown_place",
            "field": field,
            "value": value,
            "known_places": known,
        }
        if len(unknown) > 1:
            reason["unknown"] = [{"field": f, "value": v} for f, v in unknown]
        return {
            "student_ref": str(student_ref), "itinerary": None,
            "rationale": (
                f"I can't plan that: I don't know the {label} {value!r}. "
                f"Known places: {', '.join(known)}. Pick one of those and "
                f"I'll plan the trip."),
            "constraints": constraints,
            "alternatives": [], "replan_trigger": None,
            "feasible": False,
            "infeasible_reason": reason,
        }

    errors: list[str] = []

    def _build(skip_bus: bool) -> dict | None:
        try:
            return _build_itinerary(
                src, start_dt=start_dt, end_dt=end_dt, from_place=from_place,
                to_place=to_place, eat_loc=eat_loc, diet=diet, avoid=avoid,
                max_kcal=max_kcal, route_id=route_hint, skip_bus=skip_bus)
        except Exception as exc:                             # noqa: BLE001
            errors.append(f"{'walk' if skip_bus else 'bus'} plan failed: {exc}")
            return None

    # Build BOTH a bus-based and a walk-based plan and keep the FASTER one.
    # Without this the planner proposed a 35.8-minute bus ride for a trip that is
    # a 7.3-minute walk: technically valid, and indefensible in a demo. A bus is
    # only a plan when it actually beats walking.
    candidates = [c for c in (_build(False), _build(True)) if c is not None]
    if not candidates:
        detail = "; ".join(errors) or "no candidate itinerary"
        return {
            "student_ref": student_ref, "itinerary": None,
            "rationale": f"could not build a plan: {detail}",
            "constraints": constraints,
            "alternatives": [], "replan_trigger": None, "error": detail,
            "feasible": False,
            "infeasible_reason": {"code": "no_legs", "detail": detail},
        }

    def _usable(c: dict) -> bool:
        return bool(c.get("legs"))

    def _meets_deadline(c: dict) -> bool:
        return bool(c.get("arrives_in_window")) and _usable(c)

    # FEASIBILITY BEATS SPEED. A fast bus that lands after the deadline is not a
    # plan; a slower walk that makes it is. Explicit bus/walk preference is
    # honoured only among candidates that actually fit, so `prefer: bus` can
    # still fall back to walking when the bus cannot make the deadline. When
    # nothing fits, the least-late USEFUL candidate is kept for inspection.
    meeting = [c for c in candidates if _meets_deadline(c)]
    if meeting:
        if prefer == "bus":
            pool = [c for c in meeting if c["used_bus"]] or meeting
        elif prefer in ("walk", "walking"):
            pool = [c for c in meeting if not c["used_bus"]] or meeting
        else:
            pool = meeting
        itin_a = min(pool, key=lambda c: c["total_min"])
    else:
        useful = [c for c in candidates if _usable(c)] or candidates
        itin_a = max(useful, key=lambda c: c["slack_min"])

    trigger = _replan_trigger(src, itin_a)
    itin_b = None
    if trigger is not None:
        if trigger["cause"] in ("bus_late", "bus_early", "bus_full"):
            # drop the compromised bus leg; walk the remaining distance
            itin_b = _build_itinerary(
                src, start_dt=start_dt, end_dt=end_dt, from_place=from_place,
                to_place=to_place, eat_loc=eat_loc, diet=diet, avoid=avoid,
                max_kcal=max_kcal, route_id=route_hint, skip_bus=True)
        elif trigger["cause"] == "dining_closing":
            # "D2 closes at 15:00, so West End is now the option": try another
            # location that is open long enough, else drop the eat leg.
            eat_start = (datetime.fromisoformat(itin_a["eat_start"])
                         if itin_a.get("eat_start") else start_dt)
            alt = _alternative_eat_location(src, str(eat_loc), eat_start,
                                            EAT_MINUTES)
            itin_b = _build_itinerary(
                src, start_dt=start_dt, end_dt=end_dt, from_place=from_place,
                to_place=to_place, eat_loc=alt, diet=diet, avoid=avoid,
                max_kcal=max_kcal, route_id=route_hint, skip_bus=False)

    chosen = itin_b if itin_b is not None else itin_a
    # A zero-leg "plan" is not something to render as a valid itinerary: hand the
    # UI no itinerary at all rather than a 0-minute card that reads as success.
    itinerary_out = chosen if chosen.get("legs") else None

    # TOP-LEVEL FEASIBILITY DESCRIBES THE CHOSEN PLAN. A re-planned Plan B can
    # itself miss the deadline, so this is computed after `chosen` is fixed.
    # `late_by_min` is derived from the rounded slack the card already shows --
    # never a fresh guess.
    feasible = True
    infeasible_reason: dict | None = None
    if not _usable(chosen):
        feasible = False
        infeasible_reason = {"code": "no_legs",
                             "known_places": config.static_place_keys()}
    elif not chosen.get("arrives_in_window"):
        feasible = False
        infeasible_reason = {
            "code": "deadline_missed",
            "late_by_min": round(-float(chosen["slack_min"]), 1),
        }

    spare = int(round(abs(float(chosen["slack_min"]))))
    timing = (f"with {spare} min to spare" if chosen["arrives_in_window"]
              else f"{spare} min after the deadline")
    if not feasible and infeasible_reason["code"] == "deadline_missed":
        # NEVER say "fits" or "to spare" for a plan that misses: state the miss
        # and the size of it, and name what would have to change.
        late = int(round(float(infeasible_reason["late_by_min"])))
        lead = (f"Use Plan B. {trigger['detail']} " if trigger is not None else "")
        rationale = (
            f"{lead}No plan makes that deadline. The closest option reaches "
            f"{to_place} at {_human_time(chosen['arrive_time'])}, {late} min "
            f"after the deadline. Move the deadline later or drop the meal "
            f"to fit."
        )
    elif not feasible:
        rationale = (
            "I couldn't build a usable route between those places, so there "
            "is nothing to recommend. Check the starting point and "
            "destination and try again."
        )
    elif trigger is not None:
        rationale = (
            f"Use Plan B. {trigger['detail']} The revised "
            f"{'bus' if chosen['used_bus'] else 'walking'} plan reaches "
            f"{to_place} at {_human_time(chosen['arrive_time'])}, {timing}."
        )
    else:
        eat_leg = next((l for l in chosen["legs"] if l["type"] == "eat"), None)
        if eat_leg is not None:
            opening = (f"Yes — {eat_leg['item']} at {eat_leg['place']} fits "
                       f"your time window.")
        else:
            opening = "A route fits your time window."
        check = (" ".join(chosen["notes"]) if chosen["notes"] else
                 "The latest vehicle snapshot does not invalidate it.")
        rationale = (
            f"{opening} Leave at {_human_time(chosen['leave_time'])} and reach "
            f"{to_place} at {_human_time(chosen['arrive_time'])}, {timing}. "
            f"{check}"
        )

    alternatives: list[dict] = []
    direct = _walk_result(from_place, to_place)
    if "error" not in direct:
        alternatives.append({"type": "walk_direct",
                             "minutes": direct["minutes"],
                             "meters": direct["meters"],
                             "method": direct["method"]})
    if itin_b is not None:
        alternatives.append({"type": "previous_itinerary_a",
                             "itinerary": itin_a})

    return {
        "student_ref": student_ref,           # surrogate only -- never PII
        "itinerary": itinerary_out,
        "rationale": rationale,
        "feasible": feasible,
        "infeasible_reason": infeasible_reason,
        "constraints": constraints,
        "alternatives": alternatives,
        "replan_trigger": trigger,
    }
