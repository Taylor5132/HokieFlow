"""VT dining: locations, menus, allergens, nutrition, hours.

Owner: worker dining. Contract: INTERFACES.md §3 (frozen) plus the additive
post-push basic/location layer documented there. Facts: SDD.md §5.4–5.5.

THE DATE-FORMAT TRAP (verified 2026-09-19, do not "fix"):
  * Menu API  `dtdate` = MM/DD/YYYY. An ISO date (2026-09-19) returns HTTP 200
    with "meals": [] — a SILENT empty result, no error.
  * Hours API `date`   = YYYY-MM-DD (a different format on purpose).

Those two empties are DIFFERENT and must never be conflated:
  * a malformed `dtdate` is a caller bug -> `_menu_dtdate` raises MenuError
    BEFORE any request (`menu()` and `menu_result()` propagate it);
  * a valid date with zero recipes is a REAL state -> `menu_result()` returns
    STATUS_EMPTY, while the FROZEN `menu()` still raises MenuError on it.

EVIDENCE MUST BE FROM THE PAST: an envelope captured after `config.now()` (the
replay/request clock) is rejected (menu -> STATUS_UNAVAILABLE
"not_yet_available", hours -> NotYetAvailableError) and returns no rows. A menu
payload whose own `locationNum`/`date` disagree with the request is rejected as
"source_mismatch" rather than attributed to the wrong hall/day.

String handling: a `str` menu date must already be MM/DD/YYYY. `date` objects
are formatted per-API (the safe path). `eat_options` needs BOTH formats, so it
normalizes any input to a `date` first (via `_as_date`).

All HTTP goes through hokieday.cache.get_json — never urllib/requests here.

THE HOURS <-> MENU JOIN (SDD.md §5.5): the hours API unit carries
extra_data[key="foodpro_id"].value which equals the menu API's locationNum
(verified for D2 = "15"). See `unit_foodpro_id()`. A single foodpro_id can map
to several physical units (Perry Place `06` has 8); each becomes its own window.
OVERNIGHT windows (close < open, e.g. DX 22:00:01 -> 02:00:00) are rolled to
the next day via `window_span()`.

HOURS ARE ANCHORED TO THEIR SOURCE DATE. A window returned for date D is only
ever placed on D (its overnight close rolling into D+1). It is NEVER shifted
backward to D-1: doing so invents a schedule the source did not publish for
D-1. For an early-morning probe on date D, `open_windows()` loads D-1's ACTUAL
hours and includes only its true overnight windows; when D-1 cannot be read the
result is UNKNOWN (`unavailable`), never a fabricated open/closed verdict.

NUTRITION IS SUBJECT TO THE SAME CLOCK. A nutrition chunk or derived all-menu
envelope captured after `config.now()` is not evidence about the replay instant,
so `nutrition_for_location()`/`nutrition_bulk()` skip it and leave kcal unknown.
A requested `max_kcal` stays a HARD ceiling: with no proven calories the item is
dropped, and callers get a typed `nutrition_unavailable` reason rather than
unproven rows. Only the obviously-synthetic demo profiles may drop their hidden
ceiling when calories are unknown; an explicit caller ceiling never is.

PUBLIC BASIC/STATUS SURFACE (frontend-ready; no nutrition fetch):
  * location_directory()                 -> all 12 configured locations, sorted
  * location_status(num, date)           -> ok | closed | empty | unavailable
  * menu_result(num, date)               -> items + status + provenance
  * list_foods()/search_foods()/filter_foods()
                                         -> basic rows + sources_ok/skipped/stale
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import cache, config


class MenuError(RuntimeError):
    """Raised for a malformed menu date, an unreachable menu source, or a
    genuine zero-recipe menu -- the frozen `menu()` contract.

    `menu_result()` is the typed alternative that returns STATUS_EMPTY instead.
    """


class NotYetAvailableError(RuntimeError):
    """Raised when a cached envelope was captured AFTER the replay/request clock.

    A fixture from the future is not evidence about "now": serving it would
    invent data the clock says has not happened yet. Both menu and hours reject
    it (menu as STATUS_UNAVAILABLE, hours by raising this) and return no rows.
    """


# ------------------------------------------------------------------ models
@dataclass(frozen=True)
class Location:
    location_num: str
    name: str


@dataclass(frozen=True)
class MenuItem:
    location_num: str
    date: date
    meal: str
    section: str
    recipe_id: str
    name: str
    description: str
    portion_size: str
    portion_unit: str
    allergens: tuple[str, ...]      # split on ",", stripped, empties dropped
    diet_tags: tuple[str, ...]      # from legendImages, lowercased


@dataclass(frozen=True)
class HoursWindow:
    foodpro_id: str
    name: str
    date: date
    open_time: str                  # "HH:MM:SS" as returned
    close_time: str                 # "HH:MM:SS" as returned


@dataclass(frozen=True)
class Nutrients:
    cals: float
    protein_g: float
    fat_g: float
    carb_g: float
    sodium_mg: float


# A location's resolution is a SEPARATE value from its food rows. A closed or
# data-poor hall is not the same as one whose upstream call failed, and an open
# hall with no published menu is not a bug -- all four are legitimate states a
# student UI must render differently.
STATUS_OK = "ok"
STATUS_CLOSED = "closed"
STATUS_EMPTY = "empty"
STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class LocationStatus:
    """Per-location dining resolution, kept separate from the food rows.

    `menu_status` and `hours_status` are INDEPENDENT; `status` is their
    composite. This matters because "menu ok but hours unknown" is not `ok`,
    and "hours closed but menu unreachable" is `unavailable`, not `closed`.
    """
    location_num: str
    name: str
    date: date
    status: str                     # ok | closed | empty | unavailable
    open_now: bool | None           # None when the hours source is unavailable
    menu_count: int | None          # None when the menu source is unavailable
    menu_status: str | None = None  # ok | empty | unavailable
    hours_status: str | None = None # ok | closed | unavailable
    windows: tuple[HoursWindow, ...] = ()
    stale: bool | None = None       # None when provenance/threshold unavailable
    source: str | None = None       # cache key of the menu envelope
    fetched_at: str | None = None   # envelope capture time
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "location_num": self.location_num,
            "name": self.name,
            "date": self.date.isoformat(),
            "status": self.status,
            "menu_status": self.menu_status,
            "hours_status": self.hours_status,
            "open_now": self.open_now,
            "menu_count": self.menu_count,
            "windows": [
                {"name": w.name, "open_time": w.open_time,
                 "close_time": w.close_time,
                 "foodpro_id": w.foodpro_id}
                for w in self.windows
            ],
            "stale": self.stale,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MenuResult:
    """A menu fetch and its resolution -- never conflates empty with invalid."""
    location_num: str
    name: str
    date: date
    status: str                     # ok | empty | unavailable
    items: tuple[MenuItem, ...] = ()
    reason: str | None = None
    source: str | None = None
    fetched_at: str | None = None
    stale: bool | None = None


@dataclass(frozen=True)
class FoodRow:
    """Backend-ready BASIC food row (no nutrition fetch involved)."""
    location_num: str
    location_name: str
    date: date
    meal: str
    section: str
    name: str
    description: str
    portion: str
    diet_tags: tuple[str, ...]
    allergens: tuple[str, ...]
    allergens_known: bool
    venue_allergen_free: bool
    recipe_id: str
    source: str | None = None
    fetched_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "location_num": self.location_num,
            "location_name": self.location_name,
            "date": self.date.isoformat(),
            "meal": self.meal,
            "section": self.section,
            "name": self.name,
            "description": self.description,
            "portion": self.portion,
            "diet_tags": list(self.diet_tags),
            "allergens": list(self.allergens),
            "allergens_known": self.allergens_known,
            "venue_allergen_free": self.venue_allergen_free,
            "recipe_id": self.recipe_id,
            "source": self.source,
            "fetched_at": self.fetched_at,
        }


# A source row can contradict itself. The 2026-09-19 D2 snapshot labels Whole
# Wheat Penne Pasta "vegan" while also declaring Eggs in its allergen list. VT's
# nutrition page presents BOTH fields as decision aids, so a requested diet must
# not trust one while hiding a direct contradiction in the other. These are only
# contradictions we can prove from the available fields; this is not an attempt
# to infer every ingredient from allergen data.
_DIET_CONFLICT_ALLERGENS = {
    "vegan": frozenset({"milk", "eggs", "fish", "crustacean shellfish"}),
    "vegetarian": frozenset({"fish", "crustacean shellfish"}),
}


def diet_allergen_conflicts(item: MenuItem, diet: str | None) -> tuple[str, ...]:
    """Declared allergens that directly contradict a requested diet tag.

    An empty tuple means no contradiction is visible in the source fields; it
    does NOT independently certify that the item satisfies the diet.
    """
    blocked = _DIET_CONFLICT_ALLERGENS.get(str(diet or "").strip().lower(), ())
    return tuple(a for a in item.allergens if a.strip().lower() in blocked)


# ------------------------------------------------------------------ helpers
def _as_date(d: date | str) -> date:
    """Normalize a date-or-string (either verified format) to a `date`."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    s = d.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date {d!r}: use YYYY-MM-DD or MM/DD/YYYY")


def _num(x: Any) -> float:
    """FoodPro sends '----' for missing nutrient values; treat those as 0."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def unit_foodpro_id(unit: dict) -> str | None:
    """Hours-API unit -> FoodPro location number (the D6 join key).

    Verified (SDD.md §5.5): the hours unit's
    extra_data[key="foodpro_id"].value equals the menu API's locationNum
    (D2: "15"). Returns None when the unit carries no such key.
    """
    for kv in unit.get("extra_data") or []:
        if kv.get("key") == "foodpro_id":
            return str(kv.get("value"))
    return None


def _now_campus() -> datetime:
    """Now in config.CAMPUS_TZ, naive (matches hours-api times).

    Sourced from config.now(), NOT the raw wall clock. In DEMO_MODE=cache the
    replay clock is pinned to the snapshot; calling datetime.now() here would
    evaluate dining hours at demo time while transit is evaluated at snapshot
    time, so a single answer would mix two different clocks (e.g. a bus board
    reading 11:48 alongside a dining "closed" verdict computed at 08:00).
    """
    tz = ZoneInfo(config.CAMPUS_TZ)
    return config.now(tz).replace(tzinfo=None)


def _nutrition_chunks(items: list[tuple[str, str, int]], chunk: int) -> list[str]:
    """Split (recipeId, portionSize, quantity) items into comma-joined
    'recipeId*portionSize*quantity' chunk strings (NutritiveReport.aspx)."""
    if chunk < 1:
        raise ValueError("chunk must be >= 1")
    return [
        ",".join(f"{rid}*{ps}*{q}" for rid, ps, q in items[i:i + chunk])
        for i in range(0, len(items), chunk)
    ]


# ------------------------------------------------------------------ provenance
_MENU_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def _menu_dtdate(d: date | str) -> tuple[str, date]:
    """Return (MM/DD/YYYY string, date) for the menu API's dtdate.

    THE DATE-FORMAT TRAP: the API needs MM/DD/YYYY and fails SILENTLY on any
    other format (HTTP 200 with meals: []). We validate the format HERE, before
    any request, so a bad-format string raises a loud MenuError while a valid
    date with a genuinely empty menu is allowed through as STATUS_EMPTY. This is
    what stops the two from being conflated.
    """
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, date):
        return d.strftime("%m/%d/%Y"), d
    s = str(d).strip()
    if not _MENU_DATE_RE.match(s):
        raise MenuError(
            f"menu needs MM/DD/YYYY, got {d!r}. An ISO date (YYYY-MM-DD) makes "
            f"the API return HTTP 200 with meals: [] -- a silent empty result."
        )
    try:
        parsed = datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError as exc:
        raise MenuError(
            f"menu needs a real MM/DD/YYYY date, got {d!r}: {exc}") from exc
    return s, parsed


def _source_key(name: str, params: dict | None = None) -> str:
    return cache.key(name, params)


def _envelope_meta(name: str, params: dict | None = None) -> dict:
    """Public provenance for a cache key ({} when absent).

    Uses cache.metadata() rather than the private envelope helpers, so this
    module never reaches into cache internals and is cheap to merge with parent
    cache changes. A missing/malformed envelope is not an error: provenance is
    disclosed when available and omitted otherwise.
    """
    try:
        return cache.metadata(name, params) or {}
    except Exception:                                        # noqa: BLE001
        return {}


def _captured_in_future(fetched_at: str | None) -> bool:
    """True when an envelope was captured AFTER the replay/request clock.

    A fixture from the future is not evidence about "now". Serving it as fresh
    would invent data the clock says has not happened, so both menu and hours
    reject it (unavailable / NotYetAvailableError) and return no rows.
    """
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(str(fetched_at))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts > config.now(timezone.utc)


def _stale(fetched_at: str | None, max_age_s: float | None) -> bool | None:
    """Is the envelope older than max_age_s on the replay clock?

    None when there is no provenance or no threshold to compare against: an
    unknown staleness is reported as unknown, never guessed as fresh.
    """
    if fetched_at is None or max_age_s is None:
        return None
    try:
        ts = datetime.fromisoformat(str(fetched_at))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (config.now(timezone.utc) - ts).total_seconds()
    return age > float(max_age_s)


def _payload_mismatch(payload: dict, location_num: str, day: date) -> str | None:
    """Validate a menu payload's own location/date against what we requested.

    The menu API can answer with a different location or day (a wrong id, a
    cached neighbor), or omit the identity fields entirely. Accepting any of
    those silently would attribute another hall's or day's food to this request,
    so a mismatch OR a missing/unparseable identity is a typed
    `source_mismatch`, never rows.
    """
    if "locationNum" not in payload or str(payload.get("locationNum") or "").strip() == "":
        return "missing locationNum"
    got_loc = str(payload.get("locationNum")).strip()
    if got_loc != str(location_num):
        return f"locationNum {got_loc!r} != requested {location_num!r}"
    if "date" not in payload or str(payload.get("date") or "").strip() == "":
        return "missing date"
    raw_date = str(payload.get("date")).strip()
    try:
        got_day = datetime.strptime(raw_date, "%m/%d/%Y").date()
    except ValueError:
        return f"unparseable payload date {raw_date!r}"
    if got_day != day:
        return f"date {got_day.isoformat()} != requested {day.isoformat()}"
    return None


# ------------------------------------------------------------------ locations
def locations(force: bool = False) -> list[Location]:
    """All VT dining locations (Locations.aspx). Verified: 12 entries."""
    payload = cache.get_json(
        "dining_locations", config.ENDPOINTS["dining_locations"], force=force,
    )
    return [
        Location(
            location_num=str(row.get("locationNum", row.get("id", ""))),
            name=str(row.get("name", "")).strip(),
        )
        for row in payload
    ]


def location_directory() -> list[Location]:
    """Deterministic directory of ALL configured official locations.

    Sorted by location_num and built from config.DINING_LOCATIONS, so it cannot
    silently lose an entry when Locations.aspx is unreachable (the API-order
    `locations()` stays available for the live name view). This is the directory
    a frontend should bind to for the location picker.
    """
    return [Location(location_num=n, name=config.DINING_LOCATIONS[n])
            for n in sorted(config.DINING_LOCATIONS)]


# ------------------------------------------------------------------ menu
def menu_result(location_num: str, d: date | str, force: bool = False,
                max_age_s: float | None = None) -> MenuResult:
    """Menu fetch resolved into a status, never conflating the failure modes.

    * invalid `d` (wrong dtdate format) -> raises MenuError loudly
    * upstream/cache missing           -> STATUS_UNAVAILABLE + reason
    * valid date but zero recipes      -> STATUS_EMPTY (a real state)
    * otherwise                        -> STATUS_OK
    """
    dtdate, day = _menu_dtdate(d)
    num = str(location_num)
    name = config.DINING_LOCATIONS.get(num, "")
    params = {"location_num": num, "dtdate": dtdate}
    source = _source_key("dining_menu", params)
    try:
        payload, meta = cache.get_json_with_metadata(
            "dining_menu",
            config.ENDPOINTS["dining_menu"].format(
                location_num=num, dtdate=dtdate),
            params=params, force=force, max_age_s=max_age_s,
        )
    except Exception as exc:                                 # noqa: BLE001
        return MenuResult(num, name, day, STATUS_UNAVAILABLE,
                          reason=f"menu unavailable: {exc}", source=source)

    fetched_at = meta.get("fetched_at")
    if _captured_in_future(fetched_at):
        return MenuResult(
            num, name, day, STATUS_UNAVAILABLE,
            reason=(f"not_yet_available: menu envelope captured {fetched_at} "
                    f"after the replay/request clock"),
            source=source, fetched_at=fetched_at,
            stale=_stale(fetched_at, max_age_s))
    mismatch = _payload_mismatch(payload, num, day)
    if mismatch:
        return MenuResult(
            num, name, day, STATUS_UNAVAILABLE,
            reason=f"source_mismatch: {mismatch}",
            source=source, fetched_at=fetched_at,
            stale=_stale(fetched_at, max_age_s))
    items = _parse_menu_items(payload, num, day)
    if not items:
        return MenuResult(
            num, name, day, STATUS_EMPTY, reason=(
                f"no published menu for {num} on {day.isoformat()}"),
            source=source, fetched_at=fetched_at,
            stale=_stale(fetched_at, max_age_s))
    return MenuResult(num, name, day, STATUS_OK, tuple(items),
                      source=source, fetched_at=fetched_at,
                      stale=_stale(fetched_at, max_age_s))


def _parse_menu_items(payload: dict, location_num: str,
                      fallback_day: date) -> list[MenuItem]:
    items: list[MenuItem] = []
    for meal in payload.get("meals") or []:
        meal_name = str(meal.get("mealName", ""))
        for section in meal.get("sections") or []:
            section_name = str(section.get("sectionName", ""))
            for r in section.get("recipes") or []:
                # legendImages holds e.g. ["vegetarian", "vegan"]; lowercased tags
                tags: list[str] = []
                for img in r.get("legendImages") or []:
                    tag = str(img).strip().lower()
                    if tag and tag not in tags:
                        tags.append(tag)
                items.append(MenuItem(
                    location_num=str(payload.get("locationNum", location_num)),
                    date=_menu_date(payload, fallback_day),
                    meal=meal_name,
                    section=section_name,
                    recipe_id=str(r.get("recipeId", "")),
                    name=str(r.get("name", "")).strip(),
                    description=str(r.get("description", "")).strip(),
                    portion_size=str(r.get("portionSize", "")),
                    portion_unit=str(r.get("portionUnit", "")),
                    allergens=tuple(
                        a.strip() for a in str(r.get("allergens", "")).split(",")
                        if a.strip()
                    ),
                    diet_tags=tuple(tags),
                ))
    return items


def menu(location_num: str, d: date | str, force: bool = False,
         max_age_s: float | None = config.DEFAULT_MENU_CACHE_MAX_AGE_S
         ) -> list[MenuItem]:
    """Menu for one location on one day (FROZEN contract).

    `d` as a `date` is formatted MM/DD/YYYY (the only format the API accepts).
    A string MUST already be MM/DD/YYYY: an ISO string raises a loud MenuError
    before a request is made. A GENUINE zero-recipe menu and an unreachable
    source BOTH raise MenuError here -- `menu_result()` is the typed alternative
    that returns STATUS_EMPTY / STATUS_UNAVAILABLE so callers can distinguish
    them.

    `max_age_s` defaults to the menu TTL so LIVE callers refresh a stale menu
    instead of reusing a cached copy indefinitely (replay ignores it: the frozen
    store has no network to refresh from).
    """
    res = menu_result(location_num, d, force=force, max_age_s=max_age_s)
    if not res.items:
        raise MenuError(res.reason or f"menu unavailable for {location_num}")
    return list(res.items)


def _menu_date(payload: dict, d: date | str) -> date:
    """Best-effort menu date: the payload's own 'date' (MM/DD/YYYY) first."""
    raw = str(payload.get("date", "")).strip()
    if raw:
        try:
            return datetime.strptime(raw, "%m/%d/%Y").date()
        except ValueError:
            pass
    if isinstance(d, date):
        return d
    return _as_date(d)


# ------------------------------------------------------------------ allergens
def allergens(location_num: str = "15") -> tuple[list[str], list[dict]]:
    """Allergen taxonomy and diet categories for a location.

    Verified (SDD.md §5.4): 10 allergens
    (Milk, Eggs, Fish, Crustacean Shellfish, Tree Nuts, Peanuts, Wheat,
    Soybeans, Gluten, Sesame) and 4 diet categories
    (wcveg Vegan, wcvtn Vegetarian, wcha Halal Certified Meat, wcal Alcohol).
    """
    payload = cache.get_json(
        "dining_allergens",
        config.ENDPOINTS["dining_allergens"].format(location_num=location_num),
        params={"location_num": location_num},
    )
    return list(payload.get("allergens") or []), list(payload.get("categories") or [])


# ------------------------------------------------------------------ nutrition
# NutritiveReport.aspx nutrient names -> Nutrients fields (verified values
# in SDD.md §5.4).
_NUTRIENT_FIELDS = {
    "Cals": "cals",
    "Prot": "protein_g",
    "Fat-T": "fat_g",
    "Carb": "carb_g",
    "Sod": "sodium_mg",
}


def _nutrients_from(payload: dict) -> dict[str, Nutrients]:
    """Parse a NutritiveReport payload into {recipeId: Nutrients}."""
    out: dict[str, Nutrients] = {}
    for recipe in payload.get("recipes") or []:
        vals = {
            n.get("name"): _num(n.get("value"))
            for n in recipe.get("nutrients") or []
        }
        out[str(recipe.get("id", ""))] = Nutrients(
            cals=vals.get("Cals", 0.0),
            protein_g=vals.get("Prot", 0.0),
            fat_g=vals.get("Fat-T", 0.0),
            carb_g=vals.get("Carb", 0.0),
            sodium_mg=vals.get("Sod", 0.0),
        )
    return out


def _nutrition_evidence_ok(name: str, params: dict) -> bool:
    """True when a nutrition envelope is evidence for the replay/request clock.

    A chunk or derived all-menu envelope captured AFTER config.now() describes a
    moment that has not happened yet at replay time. Serving it would stamp a
    later snapshot's calories onto replay rows and gold as if contemporaneous,
    so we refuse it and leave kcal unknown. The timestamp is never rewritten.
    """
    return not _captured_in_future(
        _envelope_meta(name, params).get("fetched_at"))


def nutrition_for_location(location_num: str, d: date | str,
                           force: bool = False,
                           max_age_s: float | None
                           = config.DEFAULT_MENU_CACHE_MAX_AGE_S
                           ) -> dict[str, Nutrients]:
    """Nutrition for EVERY item on a location's menu, keyed by recipeId.

    WHY THIS EXISTS (a real bug, not a nicety)
    -----------------------------------------
    Caching nutrition per CHUNK is fragile: the chunk string depends on which
    items are in the request. `eat_options(max_kcal=...)` used to request
    nutrition for the already-filtered subset (470 -> 142 after diet+allergen
    filtering), which produced chunk keys that did not match the seeded
    menu-order chunks. Every lookup missed, a miss means "kcal unknown", and the
    calorie filter then dropped all 142 candidates -- so the meal silently
    disappeared from every plan, and "can I eat and still make class?" answered
    "no".

    Fetching the WHOLE MENU in menu order and caching it under one stable
    {location_num, date} key makes any later subset hit, whatever survives
    filtering.

    TEMPORAL PROVENANCE: a derived all-menu envelope or any chunk captured after
    the replay/request clock is skipped (see `_nutrition_evidence_ok`). In replay
    this leaves kcal UNKNOWN for the future-captured D2 nutrition fixtures rather
    than presenting later data as contemporaneous. A requested `max_kcal` stays
    hard; callers get typed `nutrition_unavailable` rather than unproven rows.
    """
    day = _as_date(d)
    params = {"location_num": str(location_num), "date": day.isoformat()}
    origin = "derived: all-menu nutrition merged from NutritiveReport chunks"

    if cache.has("nutrition_location", params) and not force:
        if _nutrition_evidence_ok("nutrition_location", params):
            return _nutrients_from(
                cache.get_json("nutrition_location", origin, params=params))
        # else: the derived envelope is future-captured; fall through to chunks.

    try:
        menu_items = menu(location_num, day, force=force, max_age_s=max_age_s)
    except Exception:                                        # noqa: BLE001
        return {}

    merged: dict = {"recipes": []}
    for chunk_str in _nutrition_chunks(
            [(it.recipe_id, it.portion_size or "1", 1) for it in menu_items], 40):
        cparams = {"items": chunk_str}
        if not _nutrition_evidence_ok("dining_nutrition", cparams):
            continue            # future-captured chunk: not contemporaneous evidence
        try:
            payload = cache.get_json(
                "dining_nutrition",
                config.ENDPOINTS["dining_nutrition"].format(
                    items=quote(chunk_str, safe="*,")),
                params=cparams, force=force, max_age_s=max_age_s)
        except cache.CacheMiss:
            continue            # partial nutrition beats none; kcal stays unknown
        merged["recipes"].extend(payload.get("recipes") or [])

    if merged["recipes"]:
        cache.put_json("nutrition_location", origin, merged, params=params)
    return _nutrients_from(merged)


def nutrition_bulk(
    items: list[tuple[str, str, int]], chunk: int = 40, force: bool = False,
    max_age_s: float | None = config.DEFAULT_MENU_CACHE_MAX_AGE_S,
) -> dict[str, Nutrients]:
    """Nutrients for many items, keyed by recipeId.

    `items` are (recipeId, portionSize, quantity); the wire format is
    'recipeId*portionSize*quantity', comma-joined. Requests are CHUNKED
    (default 40) instead of building one enormous URL, and each chunk is
    cached under ("dining_nutrition", {"items": <chunk string>}) so repeated
    calls never re-fetch.

    A chunk captured after the replay/request clock is skipped, so it can never
    populate replay rows as contemporaneous; those recipes stay kcal-unknown.
    """
    out: dict[str, Nutrients] = {}
    for chunk_str in _nutrition_chunks(list(items), chunk):
        # The items string MUST be percent-encoded. Real portion sizes include
        # fractions with spaces and slashes (e.g. "1 1/2", "1/10"), so an
        # unencoded chunk produced `InvalidURL: URL can't contain control
        # characters ... (found at least ' ')` and the whole seed silently
        # returned nothing. '*' and ',' are safe to leave literal.
        cparams = {"items": chunk_str}
        if not _nutrition_evidence_ok("dining_nutrition", cparams):
            continue
        payload = cache.get_json(
            "dining_nutrition",
            config.ENDPOINTS["dining_nutrition"].format(
                items=quote(chunk_str, safe="*,")),
            params=cparams,
            force=force,
            max_age_s=max_age_s,
        )
        for recipe in payload.get("recipes") or []:
            vals = {
                n.get("name"): _num(n.get("value"))
                for n in recipe.get("nutrients") or []
            }
            out[str(recipe.get("id", ""))] = Nutrients(
                cals=vals.get("Cals", 0.0),
                protein_g=vals.get("Prot", 0.0),
                fat_g=vals.get("Fat-T", 0.0),
                carb_g=vals.get("Carb", 0.0),
                sodium_mg=vals.get("Sod", 0.0),
            )
    return out


# ------------------------------------------------------------------ hours
def hours(foodpro_id: str, d: date | str, force: bool = False,
          max_age_s: float | None = None) -> list[HoursWindow]:
    """Opening windows for a FoodPro center on one day.

    NOTE the different date format: the hours API takes YYYY-MM-DD (a `date`
    is formatted with .isoformat(); a str is forwarded verbatim).
    Verified for D2 on 2026-09-19: 09:30:01–15:00:00 and 15:00:01–20:00:00.

    MULTI-UNIT HALLS: a single foodpro_id can map to several physical units
    (verified: Perry Place `06` returns 8 units, each with its own name and its
    own hours, all sharing extra_data foodpro_id = "06"). This function returns
    ONE HoursWindow PER UNIT, so `is_open` is true when ANY unit is open and the
    caller can name the open unit. Splitting a hall into a single averaged
    window would hide which counter is actually serving.

    Raises NotYetAvailableError when the served envelope was captured after the
    replay/request clock: future evidence is unavailable, not empty/closed.
    """
    iso = d if isinstance(d, str) else d.isoformat()
    params = {"foodpro_id": foodpro_id, "date": iso}
    payload, meta = cache.get_json_with_metadata(
        "dining_hours",
        config.ENDPOINTS["dining_hours"].format(foodpro_id=foodpro_id, date=iso),
        params=params,
        force=force,
        max_age_s=max_age_s,
    )
    fetched_at = meta.get("fetched_at")
    if _captured_in_future(fetched_at):
        raise NotYetAvailableError(
            f"hours envelope captured {fetched_at} after the replay/request "
            f"clock for {foodpro_id} on {iso}")
    out: list[HoursWindow] = []
    for unit in payload or []:
        # Verified join: extra_data foodpro_id == menu locationNum (D2 = "15").
        fid = unit_foodpro_id(unit) or foodpro_id
        name = str(unit.get("name", ""))
        for w in unit.get("hours") or []:
            out.append(HoursWindow(
                foodpro_id=fid,
                name=name,
                date=_as_date(w.get("start") or iso),
                open_time=str(w.get("open_time", "")),
                close_time=str(w.get("close_time", "")),
            ))
    return out


def _parse_clock(s: str):
    """'HH:MM:SS' -> time (no %H parsing traps here; hours are plain clock times)."""
    hh, mm, ss = (int(p) for p in s.split(":"))
    return time(hh, mm, ss)


def window_span(w: HoursWindow) -> tuple[datetime, datetime]:
    """Resolved (open_datetime, close_datetime) for one window.

    OVERNIGHT WINDOWS: FoodPro returns close_time < open_time for a window
    running past midnight (verified: DX `71` on 2026-09-19 is
    22:00:01 -> 02:00:00; Xpress Lane `07` is 08:00:01 -> 02:00:00). A naive
    same-day combine puts close BEFORE open, so such a window can never read as
    open. When close <= open the close is rolled to the NEXT day. This helper is
    shared by `is_open` and by tools.LocalSource.hours so both agree.
    """
    o = _parse_clock(w.open_time)
    c = _parse_clock(w.close_time)
    open_dt = datetime.combine(w.date, o)
    close_dt = datetime.combine(w.date, c)
    if c <= o:
        close_dt += timedelta(days=1)
    return open_dt, close_dt


def _is_overnight(w: HoursWindow) -> bool:
    return _parse_clock(w.close_time) <= _parse_clock(w.open_time)


def _candidate_spans(w: HoursWindow) -> list[tuple[datetime, datetime]]:
    """The ONE day-placement a window represents: its own source date.

    An overnight window (close <= open) still belongs to its source date; its
    close is rolled forward into the next day by `window_span`. It is NEVER
    shifted BACKWARD: a window published for date D says nothing about D-1, and
    shifting it would invent a schedule the source never published. Callers that
    need yesterday's early hours must load D-1's actual windows (see
    `open_windows`).
    """
    return [window_span(w)]


def is_open(windows: list[HoursWindow], at: datetime) -> tuple[bool, float | None]:
    """(is_open_now, minutes_until_close) for a set of windows.

    Open      -> (True,  minutes until the LAST still-open unit closes).
    Between   -> (False, minutes until the NEXT window's close)  [positive].
    All past  -> (False, minutes since the last close)           [NEGATIVE].

    Each window is anchored to its OWN source date; an overnight window
    (close <= open) rolls its close into the next day. To read an early-morning
    instant correctly, pass D-1's ACTUAL overnight windows as well (see
    `open_windows`) -- `is_open` will not manufacture them. With several units,
    "minutes until close" is the union's LAST close, not the first unit's.
    """
    if not windows:
        return False, None
    spans: list[tuple[datetime, datetime]] = []
    for w in windows:
        spans.extend(_candidate_spans(w))
    spans = sorted(set(spans))
    open_closes = [close_dt for open_dt, close_dt in spans
                   if open_dt <= at < close_dt]
    if open_closes:
        return True, (max(open_closes) - at).total_seconds() / 60
    upcoming = [(o, c) for o, c in spans if o > at]
    if upcoming:
        _, close_dt = upcoming[0]
        return False, (close_dt - at).total_seconds() / 60
    _, close_dt = spans[-1]
    return False, (close_dt - at).total_seconds() / 60


# A prior-day overnight window can only still be open in the early morning
# (the verified overnight windows close at 02:00). This is the latest clock time
# at which we will even look for one on a day with no published windows; it
# keeps a late-morning probe from failing just because the previous day's
# fixture is absent.
_MORNING_CUTOFF = time(6, 0)


def _needs_prior_overnight(day_windows: list[HoursWindow], at: datetime,
                           day: date) -> bool:
    """Would D-1's real overnight windows be needed to judge `at` on `day`?

    Only an early-morning instant can fall inside a window that opened the
    previous evening. On a day with windows, "early" means before the first
    one opens; on a day with no windows we use a conservative morning cutoff so
    a midday probe is not made to depend on yesterday's fixture.
    """
    if at.date() != day:
        return False
    opens = [_parse_clock(w.open_time) for w in day_windows if w.open_time]
    if opens:
        return at.time() < min(opens)
    return at.time() < _MORNING_CUTOFF


def _prior_overnight_windows(foodpro_id: str, day: date, force: bool,
                             max_age_s: float | None
                             ) -> list[HoursWindow] | None:
    """D-1's ACTUAL overnight windows, or None when D-1 cannot be read.

    None means UNKNOWN, never closed: a missing previous day must not be
    reported as a closed window, and its schedule must never be invented by
    shifting `day`'s windows backward.
    """
    try:
        prev = hours(foodpro_id, day - timedelta(days=1), force=force,
                     max_age_s=max_age_s)
    except Exception:                                        # noqa: BLE001
        return None
    return [w for w in prev if _is_overnight(w)]


def open_windows(foodpro_id: str, day: date, at: datetime,
                 force: bool = False,
                 max_age_s: float | None = config.DEFAULT_MENU_CACHE_MAX_AGE_S
                 ) -> tuple[list[HoursWindow], str | None]:
    """Windows to judge `at` on service date `day`, plus a prior-day status.

    Returns (windows, prior_status): `windows` are `day`'s own windows plus,
    when the probe is early enough that a previous-day overnight window could
    still be open, D-1's ACTUAL overnight windows. `prior_status` is
    STATUS_UNAVAILABLE when a prior-day lookup was needed but failed, else None.
    Never shifts `day`'s windows backward.
    """
    day_windows = hours(foodpro_id, day, force=force, max_age_s=max_age_s)
    if not _needs_prior_overnight(day_windows, at, day):
        return day_windows, None
    prior = _prior_overnight_windows(foodpro_id, day, force, max_age_s)
    if prior is None:
        return day_windows, STATUS_UNAVAILABLE
    return day_windows + prior, None


# ------------------------------------------------------------------ status
# Required user states, each resolvable separately from the food rows:
#   ok          open/known location with a published menu
#   closed      no published hours for the date (regardless of menu availability)
#   empty       operating, but the menu source published nothing
#   unavailable the source itself could not be read (upstream/cache failure)
# "stale" is orthogonal: it flags a served-but-old cached copy, not a state.
def location_status(location_num: str, d: date | str | None = None,
                    at: datetime | None = None, force: bool = False,
                    max_age_s: float | None = config.DEFAULT_MENU_CACHE_MAX_AGE_S
                    ) -> LocationStatus:
    """Resolve one location's date-level status, keeping hours and menu apart.

    Precedence: an unreachable source is `unavailable`; no published hours is
    `closed` (a real closed day, even if a stale menu exists); an operating
    location with zero recipes is `empty`; otherwise `ok`. `open_now` describes
    the instant `at` (default: campus now) and is None when hours are unknown.
    """
    num = str(location_num)
    day = _as_date(d) if d is not None else _now_campus().date()
    name = config.DINING_LOCATIONS.get(num, "")
    when = at or _now_campus()

    menu_res = menu_result(num, day, force=force, max_age_s=max_age_s)

    hours_status = STATUS_OK
    hours_reason: str | None = None
    try:
        # `open_windows` adds D-1's ACTUAL overnight windows when the probe is
        # early enough to need them, and reports UNKNOWN (not closed) when that
        # previous day cannot be read. It never shifts D's windows backward.
        wins, prior_status = open_windows(
            num, day, when, force=force, max_age_s=max_age_s)
    except Exception as exc:                                 # noqa: BLE001
        # CacheMiss, NotYetAvailableError, a future-captured envelope, or any
        # upstream failure: the source is unavailable, NOT a closed day.
        wins = []
        hours_status = STATUS_UNAVAILABLE
        hours_reason = str(exc)
    else:
        if prior_status == STATUS_UNAVAILABLE:
            hours_status = STATUS_UNAVAILABLE
            hours_reason = ("previous-day hours unavailable for an "
                            "early-morning probe")
        elif not wins:
            hours_status = STATUS_CLOSED

    if hours_status == STATUS_OK:
        open_now: bool | None = is_open(wins, when)[0]
    elif hours_status == STATUS_CLOSED:
        open_now = False
    else:
        open_now = None

    menu_status = menu_res.status
    # COMPOSITE PRECEDENCE: a source failure dominates (so a closed day with an
    # unreachable menu is `unavailable`, never `closed`); then an actually
    # closed day; then an operating location whose menu is empty; else ok.
    if menu_status == STATUS_UNAVAILABLE or hours_status == STATUS_UNAVAILABLE:
        status = STATUS_UNAVAILABLE
        parts = []
        if menu_status == STATUS_UNAVAILABLE:
            parts.append(menu_res.reason or "menu unavailable")
        if hours_status == STATUS_UNAVAILABLE:
            parts.append(f"hours unavailable: {hours_reason}")
        reason = "; ".join(parts)
    elif hours_status == STATUS_CLOSED:
        status = STATUS_CLOSED
        reason = f"no published hours for {num} on {day.isoformat()}"
    elif menu_status == STATUS_EMPTY:
        status = STATUS_EMPTY
        reason = menu_res.reason or "operating but no published menu"
    else:
        status = STATUS_OK
        reason = None

    menu_count = (len(menu_res.items)
                  if menu_status != STATUS_UNAVAILABLE else None)
    return LocationStatus(
        location_num=num, name=name, date=day, status=status,
        open_now=open_now, menu_count=menu_count,
        menu_status=menu_status, hours_status=hours_status,
        windows=tuple(wins), stale=menu_res.stale, source=menu_res.source,
        fetched_at=menu_res.fetched_at, reason=reason,
    )


# ------------------------------------------------------------------ basic rows
def _apply_diet(items: list[MenuItem], diet: str | None) -> list[MenuItem]:
    """Require the upstream diet tag AND reject a self-contradicting row."""
    if not diet:
        return items
    want = diet.strip().lower()
    return [
        it for it in items
        if want in it.diet_tags and not diet_allergen_conflicts(it, want)
    ]


def _apply_avoid(items: list[MenuItem], avoid: tuple[str, ...]) -> list[MenuItem]:
    """HARD SAFETY FILTER (never a preference): substring, case-insensitive.

    THREE-WAY POLICY (SDD risk R8): exclude on a stated match; keep a BLANK
    field ONLY when the venue documents an allergen-free kitchen (Viridian);
    exclude every other blank field as UNKNOWN.
    """
    bad = tuple(a.strip().lower() for a in avoid if a and a.strip())
    if not bad:
        return items
    kept: list[MenuItem] = []
    for it in items:
        stated = [alg.lower() for alg in it.allergens]
        if any(b in alg for alg in stated for b in bad):
            continue                      # definitely contains it
        if not stated and not config.is_venue_allergen_free(
                it.section, it.location_num):
            continue                      # UNKNOWN -> excluded by default
        kept.append(it)
    return kept


def _food_row(it: MenuItem, location_name: str, source: str | None,
              fetched_at: str | None) -> FoodRow:
    return FoodRow(
        location_num=it.location_num,
        location_name=location_name,
        date=it.date,
        meal=it.meal,
        section=it.section,
        name=it.name,
        description=it.description,
        portion=f"{it.portion_size} {it.portion_unit}".strip(),
        diet_tags=it.diet_tags,
        allergens=it.allergens,
        # SAFETY: blank means UNKNOWN, never allergen-free. venue_allergen_free
        # explains a blank; it does not turn allergens_known true.
        allergens_known=bool(it.allergens),
        venue_allergen_free=config.is_venue_allergen_free(
            it.section, it.location_num),
        recipe_id=it.recipe_id,
        source=source,
        fetched_at=fetched_at,
    )


def list_foods(location_num: str | None = None, d: date | str | None = None,
               meal: str | None = None, section: str | None = None,
               diet: str | None = None, avoid: tuple[str, ...] = (),
               query: str | None = None, force: bool = False,
               max_age_s: float | None = config.DEFAULT_MENU_CACHE_MAX_AGE_S
               ) -> dict:
    """Backend-ready BASIC food list/search/filter over one or all locations.

    Returns rows WITHOUT any nutrition fetch (basic menu first; nutrition is a
    later layer), plus a typed resolution per location:

      sources_ok       locations with a reachable menu (ok or empty)
      sources_skipped  unreachable locations, each with status + reason
      sources_stale    reachable locations whose copy exceeds max_age_s
      statuses         the full LocationStatus dict (incl. closed/empty)

    A location is NEVER silently dropped: if it is configured it appears in
    exactly one of sources_ok / sources_skipped. `query` is a case-insensitive
    substring over name + description + section + meal. Rows are sorted by
    (location_num, meal, section, name, recipe_id) for deterministic replay.
    """
    day = _as_date(d) if d is not None else _now_campus().date()
    if location_num in (None, ""):
        nums = sorted(config.DINING_LOCATIONS)
    else:
        nums = [str(location_num)]

    q = str(query).strip().lower() if query else None
    meal_want = str(meal).strip().lower() if meal else None
    section_want = str(section).strip().lower() if section else None

    rows: list[FoodRow] = []
    sources_ok: list[str] = []
    sources_skipped: list[dict] = []
    sources_stale: list[str] = []
    statuses: list[dict] = []

    for num in nums:
        name = config.DINING_LOCATIONS.get(num, "")
        res = menu_result(num, day, force=force, max_age_s=max_age_s)
        statuses.append(location_status(
            num, day, force=force, max_age_s=max_age_s).as_dict())
        if res.status == STATUS_UNAVAILABLE:
            sources_skipped.append({
                "location_num": num, "location_name": name,
                "status": res.status, "reason": res.reason,
            })
            continue
        sources_ok.append(num)
        if res.stale:
            sources_stale.append(num)
        items = _apply_avoid(_apply_diet(list(res.items), diet), avoid)
        for it in items:
            if meal_want and it.meal.strip().lower() != meal_want:
                continue
            if section_want and section_want not in it.section.strip().lower():
                continue
            if q and q not in (
                    f"{it.name} {it.description} {it.section} {it.meal}"
            ).lower():
                continue
            rows.append(_food_row(it, name, res.source, res.fetched_at))

    rows.sort(key=lambda r: (r.location_num, r.meal, r.section, r.name,
                             r.recipe_id))
    reason = None
    if not rows:
        reason = ("no basic menu rows matched; see statuses / sources_skipped "
                  "for closed, empty, unavailable or filtered-out locations")
    return {
        "location_num": location_num,
        "date": day.isoformat(),
        "count": len(rows),
        "rows": [r.as_dict() for r in rows],
        "sources_ok": sources_ok,
        "sources_skipped": sources_skipped,
        "sources_stale": sources_stale,
        "statuses": statuses,
        "reason": reason,
    }


def search_foods(query: str, location_num: str | None = None,
                 d: date | str | None = None, diet: str | None = None,
                 avoid: tuple[str, ...] = (), force: bool = False,
                 max_age_s: float | None = config.DEFAULT_MENU_CACHE_MAX_AGE_S
                 ) -> dict:
    """Text search over basic food rows; thin wrapper over list_foods()."""
    return list_foods(location_num=location_num, d=d, diet=diet, avoid=avoid,
                      query=query, force=force, max_age_s=max_age_s)


def filter_foods(location_num: str | None = None, d: date | str | None = None,
                 meal: str | None = None, section: str | None = None,
                 diet: str | None = None, avoid: tuple[str, ...] = (),
                 force: bool = False,
                 max_age_s: float | None = config.DEFAULT_MENU_CACHE_MAX_AGE_S
                 ) -> dict:
    """Structured filter over basic food rows; thin wrapper over list_foods()."""
    return list_foods(location_num=location_num, d=d, meal=meal,
                      section=section, diet=diet, avoid=avoid, force=force,
                      max_age_s=max_age_s)


# ------------------------------------------------------------------ eat options
def eat_options(
    location_num: str,
    d: date | str,
    diet: str | None = None,
    avoid: tuple[str, ...] = (),
    max_kcal: float | None = None,
    at: datetime | None = None,
    open_only: bool = False,
    force: bool = False,
    max_age_s: float | None = config.DEFAULT_MENU_CACHE_MAX_AGE_S,
) -> list[MenuItem]:
    """Menu items matching diet / allergen-avoidance / kcal preferences.

    ALLERGY FILTERING IS A HARD FILTER, NEVER A PREFERENCE (SDD root rule):
    any item whose allergen list contains an `avoid` term (case-insensitive
    SUBSTRING match, so avoid="Nuts" also kills "Tree Nuts") is excluded
    before anything else, and cannot be re-admitted by other filters.

    diet: matched against the lowercased diet_tags (e.g. "vegan").
    max_kcal: requires a nutrition fetch (chunked, cached); items whose
        calories exceed the ceiling, or whose nutrients are unavailable,
        are dropped (conservative). This stays HARD even when nutrition is
        future-captured/unavailable: the result is then empty, not unproven.
    open_only: gates on the location's hours at `at` (default: campus now),
        using the verified foodpro_id == locationNum join. A closed location
        (or one whose early-morning previous-day hours are unknown) -> [].
    max_age_s: menu/nutrition TTL; forwarded so LIVE callers refresh rather
        than reuse a cached menu indefinitely.
    """
    day = _as_date(d)
    # Use the typed result, not the frozen menu(): a genuine empty menu is a
    # valid (empty) option list for filtering, while an unavailable source must
    # still surface as an error to callers.
    res = menu_result(location_num, day, force=force, max_age_s=max_age_s)
    if res.status == STATUS_UNAVAILABLE:
        raise MenuError(res.reason or f"menu unavailable for {location_num}")
    items = list(res.items)

    # Require the upstream diet designation AND reject any row whose own declared
    # allergens directly contradict that designation (source-quality guard).
    items = _apply_diet(items, diet)

    # HARD SAFETY FILTER -- never a preference. Substring, case-insensitive.
    # Three-way blank-allergen policy; see _apply_avoid and SDD risk R8.
    items = _apply_avoid(items, avoid)

    if max_kcal is not None:
        # Look nutrition up for the WHOLE MENU (menu order, cached per location),
        # never for the already-filtered subset: subsetting changed the chunk
        # boundaries, every chunk key missed the cache, and because a miss means
        # "kcal unknown" the ceiling then dropped all 142 candidates -- the meal
        # silently vanished from every plan. See nutrition_for_location().
        nut = nutrition_for_location(location_num, day, force=force,
                                     max_age_s=max_age_s)
        items = [
            it for it in items
            if it.recipe_id in nut and nut[it.recipe_id].cals <= max_kcal
        ]

    if open_only:
        # hours API is keyed by foodpro_id; verified equal to locationNum.
        # `open_windows` adds D-1's real overnight windows for an early probe.
        when = at or _now_campus()
        wins, prior_status = open_windows(
            location_num, day, when, force=force, max_age_s=max_age_s)
        if prior_status == STATUS_UNAVAILABLE:
            return []           # cannot prove open -> do not claim it
        open_now, _ = is_open(wins, when)
        if not open_now:
            return []

    return items
