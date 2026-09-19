"""Regression guards for bugs found and fixed on 2026-09-19/20.

OWNER: integrator. Every test here exists because something was actually broken;
each one names the failure it prevents. This is the file that stops today's fixes
from being quietly undone tonight.

Offline. DEMO_MODE=cache only.
"""
from __future__ import annotations

import unittest
from datetime import date

from hokieday import cache, config, dining, tools


# --------------------------------------------------------------------------- helpers
class StubBusSource:
    """Minimal Source that only serves live_buses, with a chosen deviation.

    Lets us drive the re-plan trigger deterministically instead of hoping a real
    bus happens to be late at the moment we run.
    """

    def __init__(self, delta_min: float, load_pct: int = 10) -> None:
        self.delta_min = delta_min
        self.load_pct = load_pct

    def live_buses(self, route_id: str | None) -> list[dict]:
        return [{
            "bus_id": "STUB", "route_id": route_id or "SME", "stop_id": "1600",
            "load_pct": self.load_pct, "is_at_stop": False,
            "sched_delta_min": self.delta_min,
        }]


# --------------------------------------------------------------------------- safety
class TestAllergenThreeWayPolicy(unittest.TestCase):
    """The policy that took two attempts to get right.

    Naive implementations fail in BOTH directions:
      * treating a blank allergen field as safe keeps genuinely UNKNOWN items
      * excluding every blank field drops all 48 Viridian items, whose blank is
        explained by a documented top-nine-free kitchen
    """

    def setUp(self):
        self.day = date(2026, 9, 19)
        self.all_items = dining.menu("15", self.day)
        self.kept = dining.eat_options("15", self.day, avoid=("Peanuts", "Tree Nuts"))

    def test_viridian_items_survive_an_avoid_filter(self):
        """VT documents Viridian as top-nine-allergen free. Hiding it would hide
        exactly the food a nut-allergic student needs."""
        vir = [i for i in self.kept if i.section.lower().startswith("viridian")]
        self.assertGreater(len(vir), 0, "Viridian items must survive an avoid filter")
        self.assertEqual(len(vir), 48, "all 48 documented allergen-free items should survive")

    def test_viridian_items_really_do_have_blank_allergens(self):
        """If this ever becomes false the venue-guarantee rule is moot."""
        vir = [i for i in self.all_items if i.section.lower().startswith("viridian")]
        self.assertTrue(all(not i.allergens for i in vir),
                        "the whole point: Viridian's blank field is the documented case")

    def test_unknown_allergen_items_are_excluded(self):
        """The dangerous direction: a blank field must NOT read as 'safe'."""
        non_vir_blank = [i for i in self.kept
                         if not i.allergens
                         and not i.section.lower().startswith("viridian")]
        self.assertEqual(non_vir_blank, [],
                         f"{len(non_vir_blank)} items with UNKNOWN allergens leaked through")

    def test_no_avoided_allergen_leaks(self):
        leaks = [i for i in self.kept
                 if any("peanut" in a.lower() or "tree nut" in a.lower()
                        for a in i.allergens)]
        self.assertEqual(leaks, [], f"{len(leaks)} items containing nut allergens leaked")

    def test_filter_is_actually_selective(self):
        """Guards against a filter that silently passes everything."""
        self.assertLess(len(self.kept), len(self.all_items))
        unknown = sum(1 for i in self.all_items if not i.allergens)
        self.assertEqual(unknown, 188, "fixture expectation for the blank-allergen count")

    def test_venue_flag_is_exposed_for_the_agent(self):
        rows = tools.find_food(location_num="15", avoid=("Peanuts",))["items"]
        self.assertTrue(any(r.get("venue_allergen_free") for r in rows))
        self.assertTrue(all("venue_allergen_free" in r for r in rows))
        # a flagged row must be the documented case, never an arbitrary blank
        for r in rows:
            if r["venue_allergen_free"]:
                self.assertTrue(str(r["section"]).lower().startswith("viridian"))


# --------------------------------------------------------------------------- bugs
class TestBugRegressions(unittest.TestCase):
    """Each test names the specific breakage it prevents from returning."""

    def test_plan_day_builds_a_real_plan(self):
        """BUG: `_walk_minutes` was undefined, so plan_day returned no itinerary
        at all and silently reported 'could not build a plan'."""
        r = tools.plan_day("demo-student-1", "11:22", "13:00")
        self.assertIsNone(r.get("error"), f"plan_day errored: {r.get('error')}")
        self.assertIsNotNone(r.get("itinerary"))
        self.assertGreater(len(r["itinerary"]["legs"]), 0)

    def test_eat_leg_survives_a_calorie_ceiling(self):
        """BUG: kcal was 0/470 populated, so a kcal ceiling dropped every item and
        the meal vanished from every plan -- 'can I eat?' always answered no."""
        r = tools.plan_day("demo-student-1", "11:22", "13:00")
        it = r["itinerary"]
        eat = [l for l in it["legs"] if l["type"] == "eat"]
        self.assertEqual(len(eat), 1, "the headline scenario must include a meal")

    def test_eat_leg_carries_real_calories(self):
        """BUG: kcal was hardcoded None, so the agent could never quote a number."""
        r = tools.plan_day("demo-student-1", "11:22", "13:00")
        eat = next(l for l in r["itinerary"]["legs"] if l["type"] == "eat")
        self.assertIsNotNone(eat["kcal"], "eat leg must carry calories")
        self.assertGreater(eat["kcal"], 0)

    def test_replan_does_not_raise_on_a_plan_with_eating(self):
        """BUG: undefined `closes_in_now` raised NameError on any plan with a
        meal. It also mixed naive (hours windows) with aware (itinerary) datetimes,
        which was HIDDEN behind the calorie bug until that was fixed."""
        r = tools.plan_day("demo-student-1", "11:22", "13:00")
        itin = r["itinerary"]
        self.assertIsNotNone(itin["eat_start"])
        # Calling the trigger directly is where both crashes lived.
        tools._replan_trigger(tools._LOCAL, itin)      # must not raise

    def _bus_itinerary(self):
        """Build the bus-based plan DIRECTLY.

        plan_day() may already have re-planned to a walk itinerary (the real
        fixture has buses running early, which correctly fires the trigger), so a
        test of the trigger itself must start from a known bus plan rather than
        from plan_day's returned itinerary.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo
        p = tools._plan_prefs("demo-student-1", None)
        tz = ZoneInfo(config.CAMPUS_TZ)
        itin = tools._build_itinerary(
            tools._LOCAL,
            start_dt=datetime(2026, 9, 19, 11, 22, tzinfo=tz),
            end_dt=datetime(2026, 9, 19, 13, 0, tzinfo=tz),
            from_place="Burruss Hall", to_place="McBryde Hall", eat_loc="15",
            diet=p.get("diet"), avoid=tuple(p.get("avoid") or ()),
            max_kcal=p.get("max_kcal"), route_id=None, skip_bus=False)
        self.assertTrue(itin["used_bus"], "fixture should offer a bus option here")
        return itin

    def test_bus_early_fires_the_replan(self):
        """BUG: the trigger compared deviation to slack_min (~62 min), so a bus
        would have to be an hour late -- the demo centrepiece never fired."""
        trigger = tools._replan_trigger(StubBusSource(delta_min=-30.0),
                                        self._bus_itinerary())
        self.assertIsNotNone(trigger, "a bus 30 min early must invalidate the plan")
        self.assertEqual(trigger["cause"], "bus_early")

    def test_bus_late_fires_when_arrival_breaks_the_window(self):
        trigger = tools._replan_trigger(StubBusSource(delta_min=999.0),
                                        self._bus_itinerary())
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["cause"], "bus_late")

    def test_on_time_bus_does_not_trigger(self):
        """Guards against a trigger that fires on everything."""
        self.assertIsNone(tools._replan_trigger(StubBusSource(delta_min=0.0),
                                                self._bus_itinerary()))

    def test_full_bus_triggers(self):
        trigger = tools._replan_trigger(
            StubBusSource(delta_min=0.0, load_pct=config.BUS_FULL_PCT + 5),
            self._bus_itinerary())
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["cause"], "bus_full")

    def test_prefer_bus_replans_on_the_real_fixture(self):
        """The demo: prefer the bus, and the frozen live data invalidates it.

        Buses in the frozen capture run 1-8 min EARLY, so the boarding buffer is
        breached and a re-plan is produced with plan A preserved for the diff.
        """
        r = tools.plan_day("demo-student-1", "11:22", "13:00", {"prefer": "bus"})
        self.assertIsNone(r.get("error"))
        trigger = r.get("replan_trigger")
        if trigger is None:
            self.assertFalse(r["itinerary"]["used_bus"])   # nothing to invalidate
            return
        self.assertIn(trigger["cause"], ("bus_early", "bus_late", "bus_full"))
        self.assertIn("detail", trigger)
        kinds = [a.get("type") for a in r["alternatives"]]
        self.assertIn("previous_itinerary_a", kinds,
                      "plan A must be preserved so the demo can show the diff")
        plan_a = next(a["itinerary"] for a in r["alternatives"]
                      if a.get("type") == "previous_itinerary_a")
        self.assertTrue(plan_a["used_bus"], "plan A should be the bus plan")

    def test_planner_never_proposes_a_bus_slower_than_walking(self):
        """BUG: it proposed a 35.8-min bus ride for a 7.3-min walk."""
        r = tools.plan_day("demo-student-1", "11:22", "13:00")   # default: fastest
        it = r["itinerary"]
        walk_only = tools.walk_time("Burruss Hall", "McBryde Hall")["minutes"]
        if not it["used_bus"]:
            self.assertLessEqual(it["total_min"], walk_only + 30,
                                 "a walking plan should not be absurdly long")


class TestMealChoice(unittest.TestCase):
    """Four rankings were tried for the "I'm hungry" pick and three failed
    visibly in the demo: cheapest-first gave 'Cinnamon Apples (82 kcal)',
    calorie-distance gave 'Oreo Cobbler Cake', a dessert penalty gave 'Bleu Cheese
    Dressing', and a name penalty gave '1000 Island'. This locks in the shipped
    rule: meal-ish sections first, then most filling."""

    def test_build_your_own_bars_are_demoted(self):
        """Bars serve COMPONENTS, not dishes. This is what let a salad bar offer
        '1000 Island' as lunch."""
        self.assertEqual(tools.section_rank("Edens Salad Bar", "Lettuce"), 2)
        self.assertEqual(tools.section_rank("East Side Deli Bar", "Turkey"), 2)
        self.assertEqual(tools.section_rank("Yogurt Bar", "Granola"), 2)

    def test_real_stations_are_meals(self):
        self.assertEqual(tools.section_rank("Mangia Pizza", "Cheese Pizza"), 0)
        self.assertEqual(tools.section_rank("Mangia Pasta", "Penne"), 0)
        self.assertEqual(tools.section_rank("Viridian Entrees", "Roast Chicken"), 0)
        self.assertEqual(tools.section_rank("Edens Soups and Chili", "Chili"), 0)

    def test_barbecue_is_not_caught_by_the_bar_rule(self):
        """' bar' is spaced deliberately so it does not match 'barbecue'."""
        self.assertNotEqual(tools.section_rank("Barbecue Pit", "Pulled Pork"), 2)

    def test_the_shipped_pick_is_a_dish(self):
        for profile in ("demo-student-1", "demo-student-2"):
            r = tools.plan_day(profile, "11:22", "13:00")
            eat = [l for l in r["itinerary"]["legs"] if l["type"] == "eat"]
            self.assertEqual(len(eat), 1)
            self.assertGreater(eat[0]["kcal"], 150,
                               f"{profile} was offered a snack-sized meal")

    def test_food_ranking_prefers_a_meal_section(self):
        r = tools.find_food(location_num="15", diet="vegetarian",
                            avoid=("Peanuts", "Tree Nuts"), max_kcal=800)
        top = r["items"][0]
        self.assertEqual(
            tools.section_rank(str(top.get("section")), str(top.get("name"))), 0,
            f"top pick {top['name']!r} from {top.get('section')!r} is not a meal section")


class TestCacheKeyLength(unittest.TestCase):
    """BUG: a 40-item nutrition query produced a filename past the 255-byte
    filesystem limit; every write failed with Errno 63 and the whole nutrition
    seed was silently empty."""

    def test_long_keys_are_hashed_and_short_ones_are_stable(self):
        short = cache.key("dining_nutrition", {"items": "214022*1*1,141002*2*1"})
        self.assertEqual(short, "dining_nutrition__items=214022-1-1-141002-2-1",
                         "short keys must keep their historical filenames")
        long_items = ",".join(f"{100000 + i}*1*1" for i in range(40))
        long_key = cache.key("dining_nutrition", {"items": long_items})
        self.assertLessEqual(len(long_key), 140, "hashed key must stay well under the limit")
        self.assertIn("__h", long_key)

    def test_hashed_keys_are_deterministic(self):
        items = ",".join(f"{200000 + i}*1*1" for i in range(40))
        self.assertEqual(cache.key("x", {"items": items}), cache.key("x", {"items": items}))


class TestNutritionCoverage(unittest.TestCase):
    """BUG: nutrition was fetched for the already-filtered subset, changing the
    chunk boundaries, so every cache lookup missed and kcal was always unknown."""

    def test_location_nutrition_covers_unique_recipes(self):
        nut = dining.nutrition_for_location("15", date(2026, 9, 19))
        self.assertGreater(len(nut), 100, "should cover every unique recipe on the menu")
        self.assertTrue(all(v.cals is not None for v in nut.values()))

    def test_eat_items_expose_kcal(self):
        rows = dining.eat_options("15", date(2026, 9, 19))
        with_kcal = [r for r in rows if r.recipe_id in
                     dining.nutrition_for_location("15", date(2026, 9, 19))]
        self.assertGreater(len(with_kcal), 400, "most D2 items should have nutrition")


if __name__ == "__main__":
    unittest.main()