"""Post-push FoodPro BASIC dining/location integration tests.

Covers the multi-location menu/status layer added on top of the original D2
slice: the 12-location directory, all four per-location status states, overnight
and multi-unit hours, basic food list/search/filter, safety invariants,
partial-failure reporting, provenance, staleness and deterministic replay.

Offline only. Run:
    DEMO_MODE=cache python3 -m unittest tests.test_dining_basic -v
"""
import json
import os

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from hokieday import cache, config, dining, tools


def setUpModule():
    if not config.CACHE_ONLY:
        raise RuntimeError(
            "tests.test_dining_basic requires DEMO_MODE=cache (network must stay off)"
        )


D = date(2026, 9, 19)                     # the frozen Saturday snapshot
AT = datetime(2026, 9, 19, 15, 22)        # inside the pinned replay clock


class TestLocationDirectory(unittest.TestCase):
    def test_exactly_twelve_sorted_configured_entries(self):
        directory = dining.location_directory()
        self.assertEqual(len(directory), 12)
        nums = [loc.location_num for loc in directory]
        self.assertEqual(nums, sorted(nums))
        self.assertEqual(sorted(nums), sorted(config.DINING_LOCATIONS))

    def test_directory_matches_the_live_api_names(self):
        api = {loc.location_num: loc.name for loc in dining.locations()}
        for loc in dining.location_directory():
            self.assertEqual(api[loc.location_num], loc.name)

    def test_all_twelve_complete_names(self):
        expected = {
            "01": "Ducky's at GLC",
            "06": "Perry Place at HITT Hall",
            "07": "Xpress Lane Market",
            "09": "Hokie Grill at Owens",
            "14": "Turner Place at Lavery Hall",
            "15": "D2 at Dietrick Hall",
            "16": "West End at Cochrane Hall",
            "18": "Squires Food Court",
            "19": "Viva Market - Johnston Student Center & Viva Too  - Goodwin Hall",
            "39": "Owens Food Court",
            "71": "DX",
            "72": "Deet's Place",
        }
        self.assertEqual(config.DINING_LOCATIONS, expected)
        self.assertEqual(
            {l.location_num: l.name for l in dining.location_directory()}, expected)


class TestStatusStates(unittest.TestCase):
    """The four required states, each resolved separately from food rows."""

    def test_ok_open_with_menu(self):
        st = dining.location_status("39", D, at=AT)
        self.assertEqual(st.status, dining.STATUS_OK)
        self.assertIs(st.open_now, True)
        self.assertEqual(st.menu_count, 79)
        self.assertEqual(len(st.windows), 2)
        self.assertIsNone(st.reason)

    def test_empty_open_but_no_published_menu(self):
        st = dining.location_status("09", D, at=AT)
        self.assertEqual(st.status, dining.STATUS_EMPTY)
        self.assertIs(st.open_now, True)
        self.assertEqual(st.menu_count, 0)
        self.assertIn("no published menu", st.reason)

    def test_closed_day_even_when_a_menu_exists(self):
        st = dining.location_status("14", D, at=AT)
        self.assertEqual(st.status, dining.STATUS_CLOSED)
        self.assertIs(st.open_now, False)
        # the menu for that date exists (220 rows) -- closed is about HOURS,
        # so food rows must remain queryable separately from the status
        self.assertEqual(st.menu_count, 220)
        self.assertIn("no published hours", st.reason)

    def test_unavailable_when_the_source_cannot_be_read(self):
        with patch.object(dining.cache, "get_json",
                          side_effect=cache.CacheMiss("upstream down")):
            st = dining.location_status("39", D, at=AT)
        self.assertEqual(st.status, dining.STATUS_UNAVAILABLE)
        self.assertIsNone(st.menu_count)
        self.assertIsNone(st.open_now)
        self.assertIn("upstream down", st.reason or "")

    def test_every_configured_location_resolves_to_a_known_state(self):
        valid = {dining.STATUS_OK, dining.STATUS_CLOSED, dining.STATUS_EMPTY,
                 dining.STATUS_UNAVAILABLE}
        for loc in dining.location_directory():
            with self.subTest(location=loc.location_num):
                st = dining.location_status(loc.location_num, D, at=AT)
                self.assertIn(st.status, valid)
                self.assertEqual(st.name, loc.name)

    def test_status_as_dict_is_json_ready(self):
        row = dining.location_status("39", D, at=AT).as_dict()
        self.assertEqual(row["date"], "2026-09-19")
        self.assertIn(row["status"], {"ok", "closed", "empty", "unavailable"})
        json.dumps(row)                       # must not raise


class TestEmptyVsBadDateFormat(unittest.TestCase):
    def test_valid_date_with_zero_recipes_is_empty_not_an_error(self):
        res = dining.menu_result("09", D)
        self.assertEqual(res.status, dining.STATUS_EMPTY)
        self.assertEqual(res.items, ())
        self.assertEqual(dining.menu("09", D), [])
        self.assertEqual(dining.menu("09", "09/19/2026"), [])

    def test_iso_date_string_raises_loudly(self):
        with self.assertRaises(dining.MenuError) as cm:
            dining.menu("09", "2026-09-19")
        self.assertIn("MM/DD/YYYY", str(cm.exception))

    def test_wrong_separator_raises_loudly(self):
        with self.assertRaises(dining.MenuError):
            dining.menu("15", "09-19-2026")

    def test_unreachable_source_raises_not_returns_empty(self):
        with patch.object(dining.cache, "get_json",
                          side_effect=cache.CacheMiss("gone")):
            with self.assertRaises(dining.MenuError):
                dining.menu("15", D)


class TestOvernightHours(unittest.TestCase):
    def test_dx_window_crosses_midnight(self):
        windows = dining.hours("71", D)
        self.assertEqual(len(windows), 1)
        self.assertEqual((windows[0].open_time, windows[0].close_time),
                         ("22:00:01", "02:00:00"))
        open_dt, close_dt = dining.window_span(windows[0])
        self.assertEqual(open_dt, datetime(2026, 9, 19, 22, 0, 1))
        self.assertEqual(close_dt, datetime(2026, 9, 20, 2, 0))

    def test_open_before_and_after_midnight(self):
        windows = dining.hours("71", D)
        self.assertEqual(dining.is_open(windows, datetime(2026, 9, 19, 23, 0)),
                         (True, 180.0))
        is_open, mins = dining.is_open(windows, datetime(2026, 9, 20, 1, 0))
        self.assertTrue(is_open)
        self.assertAlmostEqual(mins, 60.0, delta=0.1)

    def test_before_opening_is_positive_and_after_close_is_negative(self):
        windows = dining.hours("71", D)
        before = dining.is_open(windows, datetime(2026, 9, 19, 21, 0))
        self.assertFalse(before[0])
        self.assertGreater(before[1], 0)
        after = dining.is_open(windows, datetime(2026, 9, 20, 3, 0))
        self.assertFalse(after[0])
        self.assertLess(after[1], 0)

    def test_xpress_lane_long_overnight_window(self):
        windows = dining.hours("07", D)
        self.assertEqual((windows[0].open_time, windows[0].close_time),
                         ("08:00:01", "02:00:00"))
        is_open, _ = dining.is_open(windows, datetime(2026, 9, 20, 1, 30))
        self.assertTrue(is_open)


class TestMultiUnitHall(unittest.TestCase):
    def test_perry_place_returns_one_window_per_unit(self):
        # Verified: foodpro_id 06 (Perry Place) has 8 physical units sharing the
        # same extra_data foodpro_id, each with its own hours.
        windows = dining.hours("06", date(2026, 9, 17))
        self.assertEqual(len(windows), 8)
        self.assertEqual(len({w.name for w in windows}), 8)
        self.assertEqual({w.foodpro_id for w in windows}, {"06"})

    def test_multi_unit_open_when_any_unit_is_open(self):
        windows = dining.hours("06", date(2026, 9, 17))
        is_open, _ = dining.is_open(windows, datetime(2026, 9, 17, 12, 0))
        self.assertTrue(is_open)
        # every unit is shut by 20:00, so the hall reads closed after that
        closed, mins = dining.is_open(windows, datetime(2026, 9, 17, 21, 0))
        self.assertFalse(closed)
        self.assertLess(mins, 0)


class TestBasicFoodRows(unittest.TestCase):
    def test_list_foods_row_shape_and_provenance(self):
        res = dining.list_foods(location_num="39", d=D)
        self.assertEqual(res["count"], 79)
        row = res["rows"][0]
        for key in ("location_num", "location_name", "date", "meal", "section",
                    "name", "description", "portion", "diet_tags", "allergens",
                    "allergens_known", "venue_allergen_free", "recipe_id",
                    "source", "fetched_at"):
            self.assertIn(key, row)
        self.assertEqual(row["location_num"], "39")
        self.assertEqual(row["location_name"], "Owens Food Court")
        self.assertEqual(row["date"], "2026-09-19")
        self.assertEqual(row["source"],
                         cache.key("dining_menu",
                                   {"location_num": "39",
                                    "dtdate": "09/19/2026"}))
        self.assertEqual(row["fetched_at"], "2026-09-19T19:38:07+00:00")

    def test_basic_rows_never_fetch_nutrition(self):
        with patch.object(dining, "nutrition_for_location",
                          side_effect=AssertionError("nutrition must not be fetched")), \
                patch.object(dining, "nutrition_bulk",
                             side_effect=AssertionError("nutrition must not be fetched")):
            res = dining.list_foods(d=D)
        self.assertGreater(res["count"], 0)

    def test_query_search_is_case_insensitive_substring(self):
        res = dining.search_foods("pizza", d=D)
        self.assertGreater(res["count"], 0)
        for row in res["rows"]:
            blob = (f"{row['name']} {row['description']} {row['section']} "
                    f"{row['meal']}").lower()
            self.assertIn("pizza", blob)

    def test_structured_filters(self):
        lunch = dining.filter_foods(d=D, meal="lunch")
        self.assertGreater(lunch["count"], 0)
        for row in lunch["rows"]:
            self.assertEqual(row["meal"].lower(), "lunch")

        section = dining.filter_foods(location_num="39", section="deli", d=D)
        for row in section["rows"]:
            self.assertIn("deli", row["section"].lower())

        veg = dining.filter_foods(location_num="39", diet="vegetarian", d=D)
        self.assertGreater(veg["count"], 0)
        for row in veg["rows"]:
            self.assertIn("vegetarian", row["diet_tags"])

    def test_all_locations_report_every_configured_location(self):
        res = dining.list_foods(d=D)
        reported = set(res["sources_ok"]) | {
            s["location_num"] for s in res["sources_skipped"]}
        self.assertEqual(reported, set(config.DINING_LOCATIONS))
        self.assertGreater(len(res["sources_ok"]), 1)
        self.assertTrue(res["sources_skipped"])

    def test_multi_location_rows_span_several_locations(self):
        rows = dining.list_foods(d=D)["rows"]
        locs = {r["location_num"] for r in rows}
        self.assertGreaterEqual(len(locs), 2)
        self.assertTrue(locs.issubset(set(config.DINING_LOCATIONS)))


class TestSafetyInvariants(unittest.TestCase):
    def test_blank_allergen_never_treated_as_safe(self):
        rows = dining.list_foods(location_num="15", d=D,
                                 avoid=("Peanuts", "Tree Nuts"))["rows"]
        self.assertTrue(rows)
        for row in rows:
            self.assertFalse(
                any("peanut" in a.lower() or "tree nut" in a.lower()
                    for a in row["allergens"]),
                f"nut allergen leaked into {row['name']}")
            if not row["allergens"]:
                self.assertTrue(row["venue_allergen_free"],
                                f"blank-allergen item {row['name']} presented as safe")
            self.assertIn("allergens_known", row)
            self.assertIn("venue_allergen_free", row)

    def test_documented_viridian_exception_is_preserved(self):
        rows = dining.list_foods(location_num="15", d=D,
                                 avoid=("Peanuts", "Tree Nuts"))["rows"]
        venue = [r for r in rows if r["venue_allergen_free"]]
        self.assertEqual(len(venue), 48)
        for row in venue:
            self.assertTrue(row["section"].lower().startswith("viridian"))
            self.assertIs(row["allergens_known"], False)

    def test_source_contradiction_is_not_recommended(self):
        # Whole Wheat Penne Pasta is tagged vegan but declares Eggs upstream.
        rows = dining.list_foods(location_num="15", d=D, diet="vegan",
                                 avoid=("Sesame",))["rows"]
        self.assertNotIn("Whole Wheat Penne Pasta", {r["name"] for r in rows})
        for row in rows:
            self.assertIn("vegan", row["diet_tags"])

    def test_avoid_is_a_hard_substring_filter(self):
        rows = dining.list_foods(location_num="15", d=D, avoid=("Milk",))["rows"]
        for row in rows:
            self.assertFalse(any("milk" in a.lower() for a in row["allergens"]))


class TestPartialFailure(unittest.TestCase):
    def test_one_unreachable_location_does_not_drop_the_others(self):
        real = dining.menu_result

        def fake(num, day, **kwargs):
            if str(num) == "39":
                return dining.MenuResult(
                    "39", "Owens Food Court", day, dining.STATUS_UNAVAILABLE,
                    reason="simulated upstream failure")
            return real(num, day, **kwargs)

        with patch.object(dining, "menu_result", side_effect=fake):
            res = dining.list_foods(d=D)
        skipped = {s["location_num"]: s for s in res["sources_skipped"]}
        self.assertIn("39", skipped)
        self.assertEqual(skipped["39"]["status"], dining.STATUS_UNAVAILABLE)
        self.assertTrue(res["sources_ok"], "other locations must still be searched")
        self.assertTrue(all(r["location_num"] != "39" for r in res["rows"]))

    def test_find_food_exposes_typed_sources(self):
        res = tools.find_food()
        reported = set(res["sources_ok"]) | {
            s["location_num"] for s in res["sources_skipped"]}
        self.assertEqual(reported, set(config.DINING_LOCATIONS))
        self.assertGreater(len(res["sources_ok"]), 1)
        self.assertTrue(res["sources_skipped"])
        for entry in res["sources_skipped"]:
            self.assertIn("status", entry)
            self.assertTrue(entry["reason"])

    def test_single_location_find_food_only_reports_that_location(self):
        res = tools.find_food(location_num="39")
        self.assertEqual(res["sources_ok"], ["39"])
        self.assertTrue(all(r["location_num"] == "39" for r in res["items"]))


class TestStalenessAndProvenance(unittest.TestCase):
    def test_stale_flag_after_the_age_threshold(self):
        future = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
        with patch.object(dining.config, "now", return_value=future):
            st = dining.location_status("39", D, at=AT, max_age_s=6 * 3600)
        self.assertIs(st.stale, True)
        self.assertEqual(st.fetched_at, "2026-09-19T19:38:07+00:00")

    def test_fresh_copy_is_not_stale(self):
        soon = datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc)
        with patch.object(dining.config, "now", return_value=soon):
            st = dining.location_status("39", D, at=AT, max_age_s=6 * 3600)
        self.assertIs(st.stale, False)

    def test_unknown_threshold_reports_unknown_staleness(self):
        st = dining.location_status("39", D, at=AT, max_age_s=None)
        self.assertIsNone(st.stale)

    def test_menu_result_provenance(self):
        res = dining.menu_result("14", D)
        self.assertEqual(res.source,
                         cache.key("dining_menu",
                                   {"location_num": "14",
                                    "dtdate": "09/19/2026"}))
        self.assertEqual(res.fetched_at, "2026-09-19T19:38:08+00:00")


class TestDeterministicReplay(unittest.TestCase):
    def test_list_foods_replays_identically(self):
        first = json.dumps(dining.list_foods(d=D), sort_keys=True)
        second = json.dumps(dining.list_foods(d=D), sort_keys=True)
        self.assertEqual(first, second)

    def test_find_food_ranking_replays_identically(self):
        first = json.dumps(tools.find_food()["items"], sort_keys=True)
        second = json.dumps(tools.find_food()["items"], sort_keys=True)
        self.assertEqual(first, second)

    def test_status_replays_identically(self):
        first = dining.location_status("39", D, at=AT).as_dict()
        second = dining.location_status("39", D, at=AT).as_dict()
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))


if __name__ == "__main__":
    unittest.main()