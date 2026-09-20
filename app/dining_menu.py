"""Dining menus for the UI: every food in a hall, with nutrition on request.

`hokieday/dining.py` owns the FoodPro parsing and the safety rules;
`list_foods()` deliberately returns rows WITHOUT nutrition ("basic menu first;
nutrition is a later layer") because fetching nutrition for every hall is a cost
the planner must not pay. A student browsing one dining hall does want those
numbers, so this module joins them for exactly one location:

* one `list_foods()` call for the rows and their typed per-location status,
* one `nutrition_for_location()` call for that location's whole menu, keyed by
  recipe id, so the chunk keys match the seeded ones (the bug documented in
  dining.py when nutrition was requested for a filtered subset).

NUTRITION HONESTY: replay/offline captures taken after the pinned clock are
refused upstream, which leaves `nutrition` unknown for those items. Unknown kcal
is reported as null with `nutrition_state="unknown"`, never as a stale number.

ALLERGEN HONESTY: a blank allergen field stays UNKNOWN (`allergens_known=false`)
unless the venue documents an allergen-free kitchen, exactly as
`_food_row`/`tools` already decide. This module never adds or removes a
constraint -- it only presents what the strict layer returned.
"""
from __future__ import annotations

from typing import Any

from hokieday import config, dining

# A hall's full menu (D2: 470 items) is larger than any other response this app
# serves, so the list is capped and the caller is told when it was cut. The UI
# asks for one meal at a time, which keeps a normal response near 150 items.
MAX_ITEMS = 600
MEAL_ORDER = ("Breakfast", "Brunch", "Lunch", "Dinner", "Late Night")


def _meal_rank(meal: str) -> tuple[int, str]:
    name = str(meal or "").strip()
    for index, known in enumerate(MEAL_ORDER):
        if name.lower() == known.lower():
            return index, name
    return len(MEAL_ORDER), name


def _item(row: dict, nutrients: dict[str, dining.Nutrients] | None) -> dict:
    """One menu row plus its nutrition, or an honest `unknown`.

    `list_foods()` returns JSON-ready dicts (FoodRow.as_dict), so this reads
    keys rather than attributes.
    """
    recipe_id = str(row.get("recipe_id") or "")
    facts = (nutrients or {}).get(recipe_id)
    item: dict[str, Any] = {
        "name": row.get("name"),
        "description": row.get("description") or None,
        "portion": row.get("portion") or None,
        "section": row.get("section"),
        "meal": row.get("meal"),
        "diet_tags": list(row.get("diet_tags") or ()),
        "allergens": list(row.get("allergens") or ()),
        # SAFETY: blank means UNKNOWN, never allergen-free.
        "allergens_known": bool(row.get("allergens_known")),
        "venue_allergen_free": bool(row.get("venue_allergen_free")),
        "recipe_id": recipe_id,
        "nutrition_state": "ok" if facts else "unknown",
        "nutrition": None,
    }
    if facts:
        item["nutrition"] = {
            "kcal": round(facts.cals, 1),
            "protein_g": round(facts.protein_g, 2),
            "fat_g": round(facts.fat_g, 2),
            "carb_g": round(facts.carb_g, 2),
            "sodium_mg": round(facts.sodium_mg, 1),
        }
    return item


def _group(items: list[dict]) -> list[dict]:
    """meals -> sections -> items, in a stable, readable order."""
    meals: dict[str, dict[str, list[dict]]] = {}
    for item in items:
        meals.setdefault(str(item["meal"] or "Menu"), {}).setdefault(
            str(item["section"] or "Other"), []).append(item)
    out: list[dict] = []
    for meal in sorted(meals, key=_meal_rank):
        sections = [
            {"section": name, "items": rows}
            for name, rows in sorted(meals[meal].items())
        ]
        out.append({
            "meal": meal,
            "count": sum(len(s["items"]) for s in sections),
            "sections": sections,
        })
    return out


def menu_payload(location: str = "", *, date: str | None = None,
                 meal: str | None = None) -> dict:
    """One dining hall's foods, grouped by meal, with nutrition when available.

    `location` accepts what a student would say ("D2", "Owens Food Court") or a
    FoodPro location number. An unknown or ambiguous name is a typed
    `unknown_location` rather than a silent fallback to another hall.
    """
    # Imported here so app/ stays importable without the optional auth stack.
    from hokieday.agent_tools import _known_dining_names, _resolve_dining_location

    wanted = str(location or "").strip()
    location_num = _resolve_dining_location(wanted) if wanted else None
    if wanted and location_num is None:
        return {
            "schema": "hokieday.dining.menu/1",
            "state": "unknown_location",
            "location": None,
            "value": wanted,
            "known_locations": _known_dining_names(),
            "meals": [],
            "count": 0,
            "reason": (f"I don't have a dining location called {wanted!r}. "
                       "Try a name from known_locations."),
        }
    if location_num is None:
        return {
            "schema": "hokieday.dining.menu/1",
            "state": "unknown_location",
            "location": None,
            "meals": [],
            "count": 0,
            "reason": "Which dining hall? Pass a location name or number.",
            "known_locations": _known_dining_names(),
        }

    listing = dining.list_foods(location_num=location_num, d=date, meal=meal)
    rows: list[dict] = list(listing.get("rows") or [])
    # `statuses` is a LIST of per-location status dicts, even for one location.
    statuses = next((s for s in (listing.get("statuses") or [])
                     if str(s.get("location_num")) == str(location_num)), {})
    name = str(statuses.get("name")
               or (rows[0].get("location_name") if rows else location_num))

    nutrients: dict[str, dining.Nutrients] = {}
    nutrition_state = "unknown"
    if rows:
        try:
            nutrients = dining.nutrition_for_location(
                location_num, date or (rows[0].get("date") if rows else None))
            nutrition_state = "ok" if nutrients else "unknown"
        except Exception:                                # noqa: BLE001
            # A nutrition outage must not hide the menu: the listing still
            # answers, with every item honestly marked nutrition unknown.
            nutrients = {}
            nutrition_state = "unavailable"

    items = [_item(row, nutrients) for row in rows[:MAX_ITEMS]]
    meals = _group(items)
    state = "ok" if items else str(statuses.get("menu_status") or "empty")
    if state == "ok" and str(statuses.get("status") or "") == "closed":
        state = "closed"

    return {
        "schema": "hokieday.dining.menu/1",
        "state": state,
        "location": {"num": location_num, "name": name},
        "date": (rows[0].get("date") if rows else None),
        "meal_filter": meal,
        "open_now": statuses.get("open_now"),
        "hours": statuses.get("windows") or [],
        "meals": meals,
        "meals_available": [entry["meal"] for entry in meals],
        "count": len(items),
        "truncated": len(rows) > MAX_ITEMS,
        "nutrition_state": nutrition_state,
        "source": statuses.get("source") or (rows[0].get("source") if rows else None),
        "fetched_at": statuses.get("fetched_at") or (rows[0].get("fetched_at") if rows else None),
        "stale": statuses.get("stale"),
        "reason": statuses.get("reason"),
        "assumptions": {
            "allergens": "a blank allergen field means UNKNOWN, not allergen-free",
            "nutrition": "values come from VT's published menu data for this date",
        },
        "now": config.now().isoformat(timespec="seconds"),
    }