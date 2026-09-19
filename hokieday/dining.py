"""VT dining: locations, menus, allergens, nutrition, hours.

Owner: worker dining. Contract: INTERFACES.md §3 (frozen). Facts: SDD.md §5.4–5.5.

THE DATE-FORMAT TRAP (verified 2026-09-19, do not "fix"):
  * Menu API  `dtdate` = MM/DD/YYYY. An ISO date (2026-09-19) returns HTTP 200
    with "meals": [] — a SILENT empty result, no error. `menu()` therefore
    raises MenuError when it parses zero recipes; it never returns [] quietly.
  * Hours API `date`   = YYYY-MM-DD (a different format on purpose).

String handling: a `str` date passed to menu()/hours() is forwarded to the
API VERBATIM (so a wrong-format string trips the zero-recipe guard — the
tests assert exactly this). `date` objects are formatted per-API, which is
the safe path. `eat_options` needs BOTH formats, so it normalizes any input
to a `date` first (via `_as_date`, which accepts either string format).

All HTTP goes through hokieday.cache.get_json — never urllib/requests here.

THE HOURS <-> MENU JOIN (SDD.md §5.5): the hours API unit carries
extra_data[key="foodpro_id"].value which equals the menu API's locationNum
(verified for D2 = "15"). See `unit_foodpro_id()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from . import cache, config


class MenuError(RuntimeError):
    """Raised when the menu API yields zero recipes (usually a date-format slip)."""


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
    """Wall-clock now in config.CAMPUS_TZ, naive (matches hours-api times)."""
    tz = ZoneInfo(config.CAMPUS_TZ)
    return datetime.now(tz).replace(tzinfo=None)


def _nutrition_chunks(items: list[tuple[str, str, int]], chunk: int) -> list[str]:
    """Split (recipeId, portionSize, quantity) items into comma-joined
    'recipeId*portionSize*quantity' chunk strings (NutritiveReport.aspx)."""
    if chunk < 1:
        raise ValueError("chunk must be >= 1")
    return [
        ",".join(f"{rid}*{ps}*{q}" for rid, ps, q in items[i:i + chunk])
        for i in range(0, len(items), chunk)
    ]


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


# ------------------------------------------------------------------ menu
def menu(location_num: str, d: date | str, force: bool = False) -> list[MenuItem]:
    """Menu for one location on one day.

    `d` as a `date` is formatted MM/DD/YYYY (the only format the API
    accepts). `d` as a str is forwarded VERBATIM — an ISO string will
    silently produce zero recipes upstream, which we convert into a loud
    MenuError. Never returns [] quietly.
    """
    dtdate = d if isinstance(d, str) else d.strftime("%m/%d/%Y")
    payload = cache.get_json(
        "dining_menu",
        config.ENDPOINTS["dining_menu"].format(location_num=location_num, dtdate=dtdate),
        params={"location_num": location_num, "dtdate": dtdate},
        force=force,
    )

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
                    date=_menu_date(payload, d),
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

    if not items:
        raise MenuError(
            f"menu {location_num} @ {dtdate}: 0 recipes. The menu API fails "
            f"SILENTLY on a wrong-format dtdate — it needs MM/DD/YYYY "
            f"(ISO YYYY-MM-DD returns HTTP 200 with meals: [])."
        )
    return items


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
        payload = cache.get_json(
            "dining_nutrition",
            config.ENDPOINTS["dining_nutrition"].format(items=chunk_str),
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


def is_open(windows: list[HoursWindow], at: datetime) -> tuple[bool, float | None]:
    """(is_open_now, minutes_until_close) for a set of windows.

    Open      -> (True,  minutes until that window's close).
    Between   -> (False, minutes until the NEXT window's close)  [positive].
    All past  -> (False, minutes since the last close)           [NEGATIVE].
    """
    if not windows:
        return False, None
    spans = sorted(
        (datetime.combine(w.date, _parse_clock(w.open_time)),
         datetime.combine(w.date, _parse_clock(w.close_time)))
        for w in windows
    )
    for open_dt, close_dt in spans:
        if open_dt <= at < close_dt:
            return True, (close_dt - at).total_seconds() / 60
    upcoming = [(o, c) for o, c in spans if o > at]
    if upcoming:
        _, close_dt = upcoming[0]
        return False, (close_dt - at).total_seconds() / 60
    _, close_dt = spans[-1]
    return False, (close_dt - at).total_seconds() / 60


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

    if diet:
        want = diet.strip().lower()
        items = [it for it in items if want in it.diet_tags]

    if avoid:
        # HARD SAFETY FILTER — never a preference. Substring, case-insensitive.
        bad = tuple(a.strip().lower() for a in avoid if a and a.strip())
        items = [
            it for it in items
            if not any(b in alg.lower() for alg in it.allergens for b in bad)
        ]

    if max_kcal is not None:
        nut = nutrition_bulk(
            [(it.recipe_id, it.portion_size, 1) for it in items], force=force,
        )
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
