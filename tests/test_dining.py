"""Offline tests for hokieday.dining (INTERFACES.md §3).

Run:  DEMO_MODE=cache python3 -m unittest tests.test_dining -v

All numbers asserted here were verified against the captured fixtures AND,
where noted, re-checked against the live APIs on 2026-09-19. No network is
touched (DEMO_MODE=cache).

NOTE on two numbers that circulate in SDD.md / the task brief but do NOT
reproduce against the captured payload (verified twice: cache fixture AND a
fresh live API call on 2026-09-19):
  * nutrition items=214022*1*1,141002*2*1 -> totals Cals = 479.616
    (SDD says "529.6" — a transcription slip; 262.512 + 217.104 = 479.616).
  * items with no Peanuts and no Tree Nuts = 428 of 470
    (the brief's "240" reproduces under no natural definition tried:
    item-level 428, distinct-recipe 201, both-days union 223).
We assert the reproducible fixture numbers; see the final report.
"""
import os

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

import unittest
from datetime import date, datetime

from hokieday import cache, config, dining


def setUpModule():
    if not config.CACHE_ONLY:
        raise RuntimeError(
            "tests.test_dining requires DEMO_MODE=cache (network must stay off)"
        )


D = date(2026, 9, 19)          # the captured Saturday at D2
LOC = "15"                     # D2 at Dietrick Hall (verified; NOT 09)


class TestLocations(unittest.TestCase):
    def test_twelve_locations_and_d2_is_15(self):
        locs = dining.locations()
        self.assertEqual(len(locs), 12)
        by_num = {l.location_num: l for l in locs}
        self.assertIn("D2", by_num["15"].name)

    def test_config_agrees_with_api(self):
        # config.DINING_LOCATIONS was hand-verified; the API must agree.
        api = {l.location_num: l.name for l in dining.locations()}
        for num, name in config.DINING_LOCATIONS.items():
            self.assertEqual(api[num], name)


class TestMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = dining.menu(LOC, D)

    def test_470_recipes_across_3_meals(self):
        self.assertEqual(len(self.items), 470)
        self.assertEqual(len({it.meal for it in self.items}), 3)

    def test_item_shape(self):
        it = self.items[0]
        self.assertEqual(it.location_num, "15")
        self.assertEqual(it.date, D)
        self.assertIsInstance(it.allergens, tuple)
        self.assertIsInstance(it.diet_tags, tuple)
        for a in it.allergens:                 # split, stripped, no empties
            self.assertTrue(a)
            self.assertEqual(a, a.strip())

    def test_diet_counts_174_vegetarian_231_vegan(self):
        veg = sum(1 for it in self.items if "vegetarian" in it.diet_tags)
        vegn = sum(1 for it in self.items if "vegan" in it.diet_tags)
        self.assertEqual(veg, 174)
        self.assertEqual(vegn, 231)

    def test_nut_free_count(self):
        """Items with no Peanuts and no Tree Nuts: 428 of 470 (fixture truth;
        see module docstring for the unreproducible '240' claim)."""
        nutfree = [
            it for it in self.items
            if "Peanuts" not in it.allergens and "Tree Nuts" not in it.allergens
        ]
        self.assertEqual(len(nutfree), 428)

    def test_iso_string_raises_not_silent(self):
        """THE TRAP: the menu API needs MM/DD/YYYY. An ISO string yields HTTP
        200 with meals: [] — menu() must turn that into a loud error, never
        return []. We simulate the API's real silent-empty response (in
        DEMO_MODE=cache there is no fixture for an ISO dtdate key)."""
        from unittest.mock import patch
        silent_empty = {"locationNum": "15", "date": "", "meals": []}
        with patch.object(cache, "get_json", return_value=silent_empty):
            with self.assertRaises(dining.MenuError) as cm:
                dining.menu(LOC, "2026-09-19")     # ISO: wrong format on purpose
        self.assertIn("MM/DD/YYYY", str(cm.exception))


class TestAllergens(unittest.TestCase):
    def test_10_allergens_4_diet_categories(self):
        allergy, categories = dining.allergens(LOC)
        self.assertEqual(len(allergy), 10)
        self.assertIn("Peanuts", allergy)
        self.assertIn("Tree Nuts", allergy)
        self.assertEqual(len(categories), 4)
        self.assertEqual({c["code"] for c in categories},
                         {"wcveg", "wcvtn", "wcha", "wcal"})


class TestEatOptionsSafety(unittest.TestCase):
    """SAFETY PROPERTY — allergy avoidance is a hard filter, not a preference."""

    @classmethod
    def setUpClass(cls):
        cls.safe = dining.eat_options(LOC, D, avoid=("Peanuts",))
        cls.everything = dining.menu(LOC, D)

    def test_returns_results(self):
        self.assertTrue(self.safe, "eat_options(avoid=Peanuts) returned nothing")

    def test_zero_peanuts_survive(self):
        """Loud assertion: not one surviving item may involve peanuts,
        under any casing, in any allergen field."""
        offenders = [
            it for it in self.safe
            if any("peanut" in a.lower() for a in it.allergens)
        ]
        self.assertEqual(offenders, [])

    def test_filter_is_strict_subset(self):
        ids_all = {it.recipe_id for it in self.everything}
        ids_safe = {it.recipe_id for it in self.safe}
        self.assertTrue(ids_safe.issubset(ids_all))
        # and it actually removed something (25 items carry Peanuts)
        self.assertLess(len(ids_safe), len(ids_all))

    def test_diet_filter(self):
        vegan = dining.eat_options(LOC, D, diet="vegan", avoid=("Peanuts",))
        self.assertTrue(vegan)
        for it in vegan:
            self.assertIn("vegan", it.diet_tags)


class TestNutrition(unittest.TestCase):
    def test_seeded_fixture_totals(self):
        """Verified (cache fixture AND live API recheck on 2026-09-19):
        214022*1*1,141002*2*1 -> Cals 479.616, Prot 11.716, Fat-T 17.36,
        Carb 67.048, Sod 1757.322. (SDD's '529.6' is a transcription slip;
        see module docstring.)"""
        res = dining.nutrition_bulk([("214022", "1", 1), ("141002", "2", 1)])
        self.assertEqual(set(res), {"214022", "141002"})
        total_cals = sum(n.cals for n in res.values())
        self.assertAlmostEqual(total_cals, 479.616, delta=0.5)
        # every Nutrients field is populated from the totals' components
        self.assertAlmostEqual(sum(n.protein_g for n in res.values()), 11.716, delta=0.5)
        self.assertAlmostEqual(sum(n.carb_g for n in res.values()), 67.048, delta=0.5)
        self.assertAlmostEqual(sum(n.fat_g for n in res.values()), 17.36, delta=0.5)
        self.assertAlmostEqual(sum(n.sodium_mg for n in res.values()), 1757.322, delta=0.5)

    def test_chunking(self):
        """nutrition_bulk must chunk (default 40), one cache entry per chunk."""
        items = [(f"{100000 + i}", "1", 1) for i in range(95)]
        chunks = dining._nutrition_chunks(items, 40)
        self.assertEqual([len(c.split(",")) for c in chunks], [40, 40, 15])
        self.assertEqual(chunks[0].split(",")[0], "100000*1*1")
        self.assertEqual(dining._nutrition_chunks(items[:3], 2)[1], "100002*1*1")

    def test_chunk_cache_key_shape(self):
        # each chunk must be cached under ("dining_nutrition", {"items": ...})
        k = cache.key("dining_nutrition", {"items": "214022*1*1,141002*2*1"})
        self.assertEqual(k, "dining_nutrition__items=214022-1-1-141002-2-1")


class TestHoursAndIsOpen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.windows = dining.hours(LOC, D)

    def test_two_windows(self):
        self.assertEqual(len(self.windows), 2)
        self.assertEqual(
            [(w.open_time, w.close_time) for w in self.windows],
            [("09:30:01", "15:00:00"), ("15:00:01", "20:00:00")],
        )
        for w in self.windows:
            self.assertEqual(w.name, "Dietrick - D2")
            self.assertEqual(w.date, D)

    def test_foodpro_id_join(self):
        """The verified join: hours unit's foodpro_id == menu locationNum."""
        for w in self.windows:
            self.assertEqual(w.foodpro_id, "15")

    def test_is_open_inside_window(self):
        open_now, mins = dining.is_open(self.windows[:1], datetime(2026, 9, 19, 14, 0))
        self.assertTrue(open_now)
        self.assertAlmostEqual(mins, 60.0, delta=0.01)

    def test_is_open_after_close_negative(self):
        open_now, mins = dining.is_open(self.windows[:1], datetime(2026, 9, 19, 16, 0))
        self.assertFalse(open_now)
        self.assertIsNotNone(mins)
        self.assertLess(mins, 0)                       # negative when already closed
        self.assertAlmostEqual(mins, -60.0, delta=0.01)

    def test_is_open_before_opening(self):
        open_now, mins = dining.is_open(self.windows[:1], datetime(2026, 9, 19, 8, 0))
        self.assertFalse(open_now)
        self.assertGreater(mins, 0)


if __name__ == "__main__":
    unittest.main()
