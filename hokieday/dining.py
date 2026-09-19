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
    STATUS_EMPTY and `menu()` returns [] (not an error).

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
    """Raised for a malformed menu date or an unreachable menu source.

    A valid date with zero recipes is NOT an error: see `menu_result()`.
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
    """Per-location dining resolution, kept separate from the food rows."""
    location_num: str
    name: str
    date: date
    status: str                     # ok | closed | empty | unavailable
    open_now: bool | None           # None when the hours source is unavailable
    menu_count: int | None          # None when the menu source is unavailable
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


def _envelope_meta(name: str, params: dict | None = None
                   ) -> tuple[str | None, str | None]:
    """(fetched_at, url) from a cache envelope, else (None, None).

    Reads the envelope's provenance fields directly so a caller can disclose
    WHEN a row was captured. A missing or malformed envelope is not an error:
    provenance is reported when available and omitted otherwise.
    """
    try:
        path = cache._json_path(name, params)
        if not path.exists():
            return None, None
        env = cache._read_envelope(path)
    except Exception:                                        # noqa: BLE001
        return None, None
    return env.get("fetched_at"), env.get("url")


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
        payload = cache.get_json(
            "dining_menu",
            config.ENDPOINTS["dining_menu"].format(
                location_num=num, dtdate=dtdate),
            params=params, force=force,
        )
    except Exception as exc:                                 # noqa: BLE001
        return MenuResult(num, name, day, STATUS_UNAVAILABLE,
                          reason=f"menu unavailable: {exc}", source=source)

    fetched_at, _ = _envelope_meta("dining_menu", params)
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


def menu(location_num: str, d: date | str, force: bool = False) -> list[MenuItem]:
    """Menu for one location on one day.

    `d` as a `date` is formatted MM/DD/YYYY (the only format the API accepts).
    A string MUST already be MM/DD/YYYY: an ISO string raises a loud MenuError
    before a request is made. A valid date with a genuinely empty menu returns
    [] (STATUS_EMPTY via menu_result) rather than being conflated with the
    bad-format trap; an unreachable source raises MenuError.
    """
    res = menu_result(location_num, d, force=force)
    if res.status == STATUS_UNAVAILABLE:
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


def nutrition_for_location(location_num: str, d: date | str,
                           force: bool = False) -> dict[str, Nutrients]:
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
    """
    day = _as_date(d)
    params = {"location_num": str(location_num), "date": day.isoformat()}
    origin = "derived: all-menu nutrition merged from NutritiveReport chunks"

    if cache.has("nutrition_location", params) and not force:
        return _nutrients_from(
            cache.get_json("nutrition_location", origin, params=params))

    try:
        menu_items = menu(location_num, day, force=force)
    except Exception:                                        # noqa: BLE001
        return {}

    merged: dict = {"recipes": []}
    for chunk_str in _nutrition_chunks(
            [(it.recipe_id, it.portion_size or "1", 1) for it in menu_items], 40):
        try:
            payload = cache.get_json(
                "dining_nutrition",
                config.ENDPOINTS["dining_nutrition"].format(
                    items=quote(chunk_str, safe="*,")),
                params={"items": chunk_str}, force=force)
        except cache.CacheMiss:
            continue            # partial nutrition beats none; kcal stays unknown
        merged["recipes"].extend(payload.get("recipes") or [])

    if merged["recipes"]:
        cache.put_json("nutrition_location", origin, merged, params=params)
    return _nutrients_from(merged)


def nutrition_bulk(
    items: list[tuple[str, str, int]], chunk: int = 40, force: bool = False,
) -> dict[str, Nutrients]:
    """Nutrients for many items, keyed by recipeId.

    `items` are (recipeId, portionSize, quantity); the wire format is
    'recipeId*portionSize*quantity', comma-joined. Requests are CHUNKED
    (default 40) instead of building one enormous URL, and each chunk is
    cached under ("dining_nutrition", {"items": <chunk string>}) so repeated
    calls never re-fetch.
    """
    out: dict[str, Nutrients] = {}
    for chunk_str in _nutrition_chunks(list(items), chunk):
        # The items string MUST be percent-encoded. Real portion sizes include
        # fractions with spaces and slashes (e.g. "1 1/2", "1/10"), so an
        # unencoded chunk produced `InvalidURL: URL can't contain control
        # characters ... (found at least ' ')` and the whole seed silently
        # returned nothing. '*' and ',' are safe to leave literal.
        payload = cache.get_json(
            "dining_nutrition",
            config.ENDPOINTS["dining_nutrition"].format(
                items=quote(chunk_str, safe="*,")),
            params={"items": chunk_str},
            force=force,
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
def hours(foodpro_id: str, d: date | str, force: bool = False) -> list[HoursWindow]:
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
    """
    iso = d if isinstance(d, str) else d.isoformat()
    payload = cache.get_json(
        "dining_hours",
        config.ENDPOINTS["dining_hours"].format(foodpro_id=foodpro_id, date=iso),
        params={"foodpro_id": foodpro_id, "date": iso},
        force=force,
    )
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


def is_open(windows: list[HoursWindow], at: datetime) -> tuple[bool, float | None]:
    """(is_open_now, minutes_until_close) for a set of windows.

    Open      -> (True,  minutes until that window's close).
    Between   -> (False, minutes until the NEXT window's close)  [positive].
    All past  -> (False, minutes since the last close)           [NEGATIVE].

    Overnight windows (close < open) are rolled to the next day; see
    window_span(). With several units, this returns the first open span, which
    is the normal multi-unit case (any unit open == hall open).
    """
    if not windows:
        return False, None
    spans = sorted(window_span(w) for w in windows)
    for open_dt, close_dt in spans:
        if open_dt <= at < close_dt:
            return True, (close_dt - at).total_seconds() / 60
    upcoming = [(o, c) for o, c in spans if o > at]
    if upcoming:
        _, close_dt = upcoming[0]
        return False, (close_dt - at).total_seconds() / 60
    _, close_dt = spans[-1]
    return False, (close_dt - at).total_seconds() / 60


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

    menu_res = menu_result(num, day, force=force, max_age_s=max_age_s)

    hours_known = True
    hours_reason: str | None = None
    try:
        wins = hours(num, day, force=force)
    except Exception as exc:                                 # noqa: BLE001
        wins = []
        hours_known = False
        hours_reason = str(exc)

    when = at or _now_campus()
    if not hours_known:
        open_now: bool | None = None
    elif not wins:
        open_now = False
    else:
        open_now, _ = is_open(wins, when)

    if not hours_known and menu_res.status == STATUS_UNAVAILABLE:
        status = STATUS_UNAVAILABLE
        reason = hours_reason or menu_res.reason
    elif not hours_known:
        status = menu_res.status
        reason = (menu_res.reason
                  or f"hours unavailable: {hours_reason}")
    elif not wins:
        status = STATUS_CLOSED
        reason = f"no published hours for {num} on {day.isoformat()}"
    elif menu_res.status == STATUS_UNAVAILABLE:
        status = STATUS_UNAVAILABLE
        reason = menu_res.reason
    elif menu_res.status == STATUS_EMPTY:
        status = STATUS_EMPTY
        reason = menu_res.reason or "operating but no published menu"
    else:
        status = STATUS_OK
        reason = None

    menu_count = (len(menu_res.items)
                  if menu_res.status != STATUS_UNAVAILABLE else None)
    return LocationStatus(
        location_num=num, name=name, date=day, status=status,
        open_now=open_now, menu_count=menu_count, windows=tuple(wins),
        stale=menu_res.stale, source=menu_res.source,
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
        if not stated and not config.is_venue_allergen_free(it.section):
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
        venue_allergen_free=config.is_venue_allergen_free(it.section),
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
) -> list[MenuItem]:
    """Menu items matching diet / allergen-avoidance / kcal preferences.

    ALLERGY FILTERING IS A HARD FILTER, NEVER A PREFERENCE (SDD root rule):
    any item whose allergen list contains an `avoid` term (case-insensitive
    SUBSTRING match, so avoid="Nuts" also kills "Tree Nuts") is excluded
    before anything else, and cannot be re-admitted by other filters.

    diet: matched against the lowercased diet_tags (e.g. "vegan").
    max_kcal: requires a nutrition fetch (chunked, cached); items whose
        calories exceed the ceiling, or whose nutrients are unavailable,
        are dropped (conservative).
    open_only: gates on the location's hours at `at` (default: campus now),
        using the verified foodpro_id == locationNum join. Closed -> [].
    """
    day = _as_date(d)
    items = menu(location_num, day, force=force)

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
        nut = nutrition_for_location(location_num, day, force=force)
        items = [
            it for it in items
            if it.recipe_id in nut and nut[it.recipe_id].cals <= max_kcal
        ]

    if open_only:
        # hours API is keyed by foodpro_id; verified equal to locationNum.
        wins = hours(location_num, day, force=force)
        open_now, _ = is_open(wins, at or _now_campus())
        if not open_now:
            return []

    return items
