"""Post-push FoodPro BASIC dining/location integration tests.

Covers the multi-location menu/status layer added on top of the original D2
slice: the 12-location directory, all four per-location composite states with
independent menu/hours sub-states, overnight and multi-unit hours, basic food
list/search/filter, safety invariants, partial-failure reporting, provenance,
staleness, future-capture rejection, payload source-matching and deterministic
replay.

REPLAY REALITY: the committed fixtures are D2-only (plus the live-buses
snapshot). Multi-location menus/hours are exercised with a SYNTHETIC temp cache
whose envelopes are captured at a time <= the pinned replay clock; the real
replay honestly reports every configured location, with non-D2 menus unavailable.
Live mode can fetch all 12 halls.

Offline only. Run:
    DEMO_MODE=cache python3 -m unittest tests.test_dining_basic -v
"""
import json
import os
import shutil
import tempfile

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hokieday import cache, config, dining, tools


def setUpModule():
    if not config.CACHE_ONLY:
        raise RuntimeError(
            "tests.test_dining_basic requires DEMO_MODE=cache (network must stay off)"
        )
    config.now()          # resolve the replay pin against the REAL fixtures


D = date(2026, 9, 19)                     # the frozen Saturday snapshot
D2 = date(2026, 9, 20)
# The pinned replay clock is 2026-09-19T15:22:29Z == 11:22 ET. `at` values are
# CAMPUS-LOCAL (naive), so the pinned campus instant is 11:22, not 15:22.
AT = datetime(2026, 9, 19, 11, 22)

SYN_FETCHED = "2026-09-19T15:00:00+00:00"     # <= replay pin 15:22:29
FUTURE_FETCHED = "2026-09-19T23:00:00+00:00"  # > replay pin: rejected

# --------------------------------------------------------------- synthetic data
def _recipe(rid, name, section="Deli", allergens="Milk",
            diet=("vegetarian",), desc="a dish"):
    return {"recipeId": rid, "name": name, "description": desc,
            "portionSize": "1", "portionUnit": "each",
            "allergens": allergens, "legendImages": list(diet)}


def _menu(num, sections, dtdate="09/19/2026"):
    return {"locationNum": num, "date": dtdate, "meals": [{
        "mealName": "Lunch",
        "sections": [{"sectionName": sec, "recipes": recs}
                     for sec, recs in sections],
    }]}


def _hours(fid, units, start="2026-09-19"):
    """units: list of (unit_name, [(open, close), ...])."""
    return [{
        "name": name,
        "extra_data": [{"key": "foodpro_id", "value": fid}],
        "hours": [{"open_time": o, "close_time": c, "start": start}
                  for o, c in wins],
    } for name, wins in units]


def _seed():
    """(name, params, payload, fetched_at) rows for a synthetic replay cache."""
    ok_menu = _menu("39", [
        ("Deli", [_recipe("R1", "Synthetic Wrap"),
                  _recipe("R2", "Plain Salad", allergens="", diet=("vegan",))]),
        ("Viridian", [_recipe("R3", "Impostor Viridian",
                              allergens="", diet=("vegan",))]),
    ])
    return [
        ("dining_menu", {"location_num": "39", "dtdate": "09/19/2026"},
         ok_menu, SYN_FETCHED),
        ("dining_hours", {"foodpro_id": "39", "date": "2026-09-19"},
         _hours("39", [("Synthetic Owens",
                        [("10:00:01", "15:00:00"), ("15:00:01", "20:00:00")])]),
         SYN_FETCHED),
        ("dining_menu", {"location_num": "09", "dtdate": "09/19/2026"},
         _menu("09", []), SYN_FETCHED),
        ("dining_hours", {"foodpro_id": "09", "date": "2026-09-19"},
         _hours("09", [("Synthetic Hokie", [("08:00:01", "20:00:00")])]),
         SYN_FETCHED),
        ("dining_menu", {"location_num": "14", "dtdate": "09/19/2026"},
         _menu("14", [("Deli", [_recipe("R1", "Synthetic Wrap")])]),
         SYN_FETCHED),
        ("dining_hours", {"foodpro_id": "14", "date": "2026-09-19"}, [],
         SYN_FETCHED),
        ("dining_menu", {"location_num": "07", "dtdate": "09/19/2026"},
         _menu("07", []), SYN_FETCHED),
        ("dining_hours", {"foodpro_id": "07", "date": "2026-09-19"},
         _hours("07", [("Synthetic Xpress", [("08:00:01", "02:00:00")])]),
         SYN_FETCHED),
        ("dining_menu", {"location_num": "71", "dtdate": "09/19/2026"},
         _menu("71", [("Grill", [_recipe("R1", "Synthetic Burger")])]),
         SYN_FETCHED),
        ("dining_hours", {"foodpro_id": "71", "date": "2026-09-19"},
         _hours("71", [("Synthetic DX", [("22:00:01", "02:00:00")])]),
         SYN_FETCHED),
        # next-day overnight window, used to prove the previous-day anchor
        ("dining_hours", {"foodpro_id": "71", "date": "2026-09-20"},
         _hours("71", [("Synthetic DX", [("22:00:01", "02:00:00")])],
                start="2026-09-20"),
         SYN_FETCHED),
        # multi-unit hall with staggered closes on the weekday menu
        ("dining_menu", {"location_num": "06", "dtdate": "09/17/2026"},
         _menu("06", [("Deli", [_recipe("R1", "Synthetic Perry Wrap")])],
               dtdate="09/17/2026"), SYN_FETCHED),
        ("dining_hours", {"foodpro_id": "06", "date": "2026-09-17"},
         _hours("06", [
             ("Perry - Breakfast", [("09:00:01", "13:00:00")]),
             ("Perry - Lunch", [("11:00:01", "18:00:00")]),
             ("Perry - Dinner", [("16:00:01", "21:00:00")]),
         ], start="2026-09-17"), SYN_FETCHED),
        # hours-only failure: menu ok, hours captured in the future
        ("dining_menu", {"location_num": "19", "dtdate": "09/19/2026"},
         _menu("19", [("Deli", [_recipe("R1", "Synthetic Viva Wrap")])]),
         SYN_FETCHED),
        ("dining_hours", {"foodpro_id": "19", "date": "2026-09-19"},
         _hours("19", [("Synthetic Viva", [("08:00:01", "16:00:00")])]),
         FUTURE_FETCHED),
        # menu-only failure: hours ok, no menu fixture (14 is handled separately)
        ("dining_hours", {"foodpro_id": "18", "date": "2026-09-19"},
         _hours("18", [("Synthetic Squires", [("09:00:01", "19:00:00")])]),
         SYN_FETCHED),
        # future-captured menu: rejected as not_yet_available
        ("dining_menu", {"location_num": "16", "dtdate": "09/19/2026"},
         _menu("16", [("Deli", [_recipe("R1", "Future West End")])]),
         FUTURE_FETCHED),
        ("dining_hours", {"foodpro_id": "16", "date": "2026-09-19"},
         _hours("16", [("Synthetic West End", [("10:30:01", "20:00:00")])]),
         SYN_FETCHED),
        # source mismatch: payload locationNum wrong
        ("dining_menu", {"location_num": "17", "dtdate": "09/19/2026"},
         _menu("99", [("Deli", [_recipe("R1", "Wrong Hall")])]), SYN_FETCHED),
        # source mismatch: payload date wrong
        ("dining_menu", {"location_num": "18", "dtdate": "09/19/2026"},
         _menu("18", [("Deli", [_recipe("R1", "Wrong Day")])],
               dtdate="09/18/2026"), SYN_FETCHED),
        # source mismatch: payload missing BOTH identity fields
        ("dining_menu", {"location_num": "72", "dtdate": "09/19/2026"},
         {"meals": [{"mealName": "Lunch", "sections": [{
             "sectionName": "Deli",
             "recipes": [_recipe("R1", "No Identity")]}]}]},
         SYN_FETCHED),
        # source mismatch: payload missing just the date
        ("dining_menu", {"location_num": "01", "dtdate": "09/19/2026"},
         {"locationNum": "01", "meals": [{"mealName": "Lunch", "sections": [{
             "sectionName": "Deli",
             "recipes": [_recipe("R1", "No Date")]}]}]},
         SYN_FETCHED),
        # contemporaneous all-menu nutrition for 39 (captured BEFORE the pin), so
        # a live-like kcal path can be exercised without future data.
        ("nutrition_location", {"location_num": "39", "date": "2026-09-19"},
         {"recipes": [
             {"id": "R1", "nutrients": [{"name": "Cals", "value": "500"}]},
             {"id": "R2", "nutrients": [{"name": "Cals", "value": "120"}]},
             {"id": "R3", "nutrients": [{"name": "Cals", "value": "650"}]},
         ]}, SYN_FETCHED),
        # future-captured nutrition for 71: must be refused as non-contemporaneous
        ("nutrition_location", {"location_num": "71", "date": "2026-09-19"},
         {"recipes": [{"id": "R1",
                       "nutrients": [{"name": "Cals", "value": "9999"}]}]},
         FUTURE_FETCHED),
        # a CONTEMPORANEOUS raw chunk for 71's one-item menu order; the future
        # derived envelope above must be skipped and this chunk used instead.
        ("dining_nutrition", {"items": "R1*1*1"},
         {"recipes": [{"id": "R1",
                       "nutrients": [{"name": "Cals", "value": "420"}]}]},
         SYN_FETCHED),
        # a Sunday-only overnight window with NO Saturday fixture: probing early
        # Sunday must be UNKNOWN, not open (that would need Saturday's schedule).
        ("dining_hours", {"foodpro_id": "72", "date": "2026-09-20"},
         _hours("72", [("Synthetic Deet's", [("22:00:01", "02:00:00")])],
                start="2026-09-20"), SYN_FETCHED),
        # closed day with an unreachable menu -> composite unavailable, not closed
        ("dining_hours", {"foodpro_id": "01", "date": "2026-09-19"}, [],
         SYN_FETCHED),
    ]


class SyntheticCache:
    """Temp replay store whose envelopes precede the pinned replay clock."""

    def __init__(self, seed=None):
        self.seed = seed if seed is not None else _seed()

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hokieday-syn-"))
        self.patcher = patch.object(config, "CACHE_DIR", self.tmp)
        self.patcher.start()
        for name, params, payload, fetched in self.seed:
            key = cache.key(name, params)
            env = {"key": key, "url": "http://synthetic", "fetched_at": fetched,
                   "mode": "test", "payload": payload}
            (self.tmp / f"{key}.json").write_text(json.dumps(env),
                                                  encoding="utf-8")
        return self.tmp

    def __exit__(self, *exc):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


# =============================================================== directory
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


# ============================================================ status states
class TestCompositeStatus(unittest.TestCase):
    """menu and hours are independent; the composite follows fixed precedence."""

    def _status(self, num, day=D, at=AT):
        return dining.location_status(num, day, at=at)

    def test_ok_open_with_menu(self):
        with SyntheticCache():
            st = self._status("39")
        self.assertEqual(st.status, dining.STATUS_OK)
        self.assertEqual(st.menu_status, dining.STATUS_OK)
        self.assertEqual(st.hours_status, dining.STATUS_OK)
        self.assertIs(st.open_now, True)
        self.assertEqual(st.menu_count, 3)

    def test_empty_open_but_no_published_menu(self):
        with SyntheticCache():
            st = self._status("09")
        self.assertEqual(st.status, dining.STATUS_EMPTY)
        self.assertEqual(st.menu_status, dining.STATUS_EMPTY)
        self.assertEqual(st.hours_status, dining.STATUS_OK)
        self.assertIs(st.open_now, True)
        self.assertEqual(st.menu_count, 0)

    def test_closed_day_even_when_a_menu_exists(self):
        with SyntheticCache():
            st = self._status("14")
        self.assertEqual(st.status, dining.STATUS_CLOSED)
        self.assertEqual(st.hours_status, dining.STATUS_CLOSED)
        self.assertEqual(st.menu_status, dining.STATUS_OK)
        self.assertIs(st.open_now, False)
        self.assertEqual(st.menu_count, 1)
        self.assertIn("no published hours", st.reason)

    def test_menu_only_failure_is_unavailable_not_ok(self):
        # hours are fine, but there is no menu fixture for 18
        with SyntheticCache():
            st = self._status("18")
        self.assertEqual(st.status, dining.STATUS_UNAVAILABLE)
        self.assertEqual(st.menu_status, dining.STATUS_UNAVAILABLE)
        self.assertEqual(st.hours_status, dining.STATUS_OK)

    def test_hours_only_failure_is_unavailable_not_ok(self):
        # menu ok, but the hours envelope is captured after the replay clock
        with SyntheticCache():
            st = self._status("19")
        self.assertEqual(st.status, dining.STATUS_UNAVAILABLE)
        self.assertEqual(st.menu_status, dining.STATUS_OK)
        self.assertEqual(st.hours_status, dining.STATUS_UNAVAILABLE)
        self.assertIsNone(st.open_now)

    def test_closed_cannot_be_unavailable(self):
        # hours say closed, menu is unreachable -> source failure dominates
        with SyntheticCache():
            st = self._status("01")
        self.assertEqual(st.status, dining.STATUS_UNAVAILABLE)
        self.assertEqual(st.hours_status, dining.STATUS_CLOSED)

    def test_unavailable_when_the_source_raises(self):
        with SyntheticCache():
            with patch.object(dining.cache, "get_json",
                              side_effect=cache.CacheMiss("upstream down")):
                st = self._status("39")
        self.assertEqual(st.status, dining.STATUS_UNAVAILABLE)
        self.assertIsNone(st.menu_count)
        self.assertIsNone(st.open_now)

    def test_every_configured_location_resolves_to_a_known_state(self):
        valid = {dining.STATUS_OK, dining.STATUS_CLOSED, dining.STATUS_EMPTY,
                 dining.STATUS_UNAVAILABLE}
        with SyntheticCache():
            for loc in dining.location_directory():
                with self.subTest(location=loc.location_num):
                    st = self._status(loc.location_num)
                    self.assertIn(st.status, valid)
                    self.assertEqual(st.name, loc.name)

    def test_status_as_dict_is_json_ready(self):
        with SyntheticCache():
            row = self._status("39").as_dict()
        self.assertEqual(row["date"], "2026-09-19")
        self.assertIn("menu_status", row)
        self.assertIn("hours_status", row)
        json.dumps(row)


class TestEmptyVsBadDateFormat(unittest.TestCase):
    def test_menu_result_reports_empty_for_a_typed_call(self):
        with SyntheticCache():
            res = dining.menu_result("09", D)
        self.assertEqual(res.status, dining.STATUS_EMPTY)
        self.assertEqual(res.items, ())

    def test_frozen_menu_still_raises_on_a_genuine_empty(self):
        with SyntheticCache():
            with self.assertRaises(dining.MenuError):
                dining.menu("09", D)

    def test_iso_date_string_raises_loudly(self):
        with self.assertRaises(dining.MenuError) as cm:
            dining.menu("15", "2026-09-19")
        self.assertIn("MM/DD/YYYY", str(cm.exception))

    def test_wrong_separator_raises_loudly(self):
        with self.assertRaises(dining.MenuError):
            dining.menu("15", "09-19-2026")


class TestSourceValidation(unittest.TestCase):
    def test_future_captured_menu_is_unavailable(self):
        with SyntheticCache():
            res = dining.menu_result("16", D)
            st = dining.location_status("16", D, at=AT)
        self.assertEqual(res.status, dining.STATUS_UNAVAILABLE)
        self.assertIn("not_yet_available", res.reason)
        self.assertEqual(res.items, ())
        self.assertEqual(st.status, dining.STATUS_UNAVAILABLE)
        self.assertEqual(st.menu_status, dining.STATUS_UNAVAILABLE)

    def test_future_captured_hours_raise_and_yield_unavailable(self):
        with SyntheticCache():
            with self.assertRaises(dining.NotYetAvailableError):
                dining.hours("19", D)
            st = dining.location_status("19", D, at=AT)
        self.assertEqual(st.status, dining.STATUS_UNAVAILABLE)
        self.assertEqual(st.hours_status, dining.STATUS_UNAVAILABLE)

    def test_payload_location_mismatch_is_source_mismatch(self):
        with SyntheticCache():
            res = dining.menu_result("17", D)
        self.assertEqual(res.status, dining.STATUS_UNAVAILABLE)
        self.assertIn("source_mismatch", res.reason)
        self.assertIn("locationNum", res.reason)
        self.assertEqual(res.items, ())

    def test_payload_date_mismatch_is_source_mismatch(self):
        with SyntheticCache():
            res = dining.menu_result("18", D)
        self.assertEqual(res.status, dining.STATUS_UNAVAILABLE)
        self.assertIn("source_mismatch", res.reason)
        self.assertIn("date", res.reason)

    def test_payload_missing_identity_is_source_mismatch(self):
        # Neither locationNum nor date: identity is REQUIRED, not optional.
        with SyntheticCache():
            res = dining.menu_result("72", D)
        self.assertEqual(res.status, dining.STATUS_UNAVAILABLE)
        self.assertIn("source_mismatch", res.reason)
        self.assertEqual(res.items, ())

    def test_payload_missing_date_is_source_mismatch(self):
        with SyntheticCache():
            res = dining.menu_result("01", D)
        self.assertEqual(res.status, dining.STATUS_UNAVAILABLE)
        self.assertIn("source_mismatch", res.reason)
        self.assertIn("date", res.reason)
        self.assertEqual(res.items, ())


# ============================================================ hours windows
class TestOvernightHours(unittest.TestCase):
    def test_dx_window_crosses_midnight(self):
        with SyntheticCache():
            windows = dining.hours("71", D)
        self.assertEqual(len(windows), 1)
        self.assertEqual((windows[0].open_time, windows[0].close_time),
                         ("22:00:01", "02:00:00"))
        open_dt, close_dt = dining.window_span(windows[0])
        self.assertEqual(open_dt, datetime(2026, 9, 19, 22, 0, 1))
        self.assertEqual(close_dt, datetime(2026, 9, 20, 2, 0))

    def test_open_before_and_after_midnight(self):
        with SyntheticCache():
            windows = dining.hours("71", D)
            self.assertEqual(dining.is_open(windows, datetime(2026, 9, 19, 23, 0)),
                             (True, 180.0))
            is_open, mins = dining.is_open(windows, datetime(2026, 9, 20, 1, 0))
        self.assertTrue(is_open)
        self.assertAlmostEqual(mins, 60.0, delta=0.1)

    def test_previous_day_window_covers_next_day_early_hours(self):
        # 01:00 on 09-20 is covered by SATURDAY's real 22:00->02:00 window, not
        # by Sunday's own window shifted backward. open_windows() loads the
        # actual D-1 fixture; is_open() alone must never invent it.
        with SyntheticCache():
            sunday_only = dining.hours("71", D2)
            self.assertFalse(
                dining.is_open(sunday_only, datetime(2026, 9, 20, 1, 0))[0],
                "Sunday's own window must not be shifted back onto Saturday")
            windows, prior_status = dining.open_windows(
                "71", D2, datetime(2026, 9, 20, 1, 0))
            is_open, mins = dining.is_open(windows, datetime(2026, 9, 20, 1, 0))
        self.assertIsNone(prior_status)
        self.assertTrue(is_open)
        self.assertAlmostEqual(mins, 60.0, delta=0.1)

    def test_missing_previous_day_is_unknown_not_open(self):
        # Location 72 has a Sunday-only 22:00->02:00 window and NO Saturday
        # fixture. At 01:00 Sunday the only window that could cover it began
        # Saturday, so the honest answer is UNKNOWN -- never "open" (invented)
        # and never "closed" (a claim we cannot support).
        with SyntheticCache():
            windows, prior_status = dining.open_windows(
                "72", D2, datetime(2026, 9, 20, 1, 0))
            st = dining.location_status("72", D2,
                                        at=datetime(2026, 9, 20, 1, 0))
        self.assertEqual(prior_status, dining.STATUS_UNAVAILABLE)
        self.assertIsNone(st.open_now)
        self.assertEqual(st.hours_status, dining.STATUS_UNAVAILABLE)
        self.assertEqual(st.status, dining.STATUS_UNAVAILABLE)

    def test_before_opening_is_positive_and_after_close_is_negative(self):
        with SyntheticCache():
            windows = dining.hours("71", D)
            before = dining.is_open(windows, datetime(2026, 9, 19, 21, 0))
            after = dining.is_open(windows, datetime(2026, 9, 20, 3, 0))
        self.assertFalse(before[0])
        self.assertGreater(before[1], 0)
        self.assertFalse(after[0])
        self.assertLess(after[1], 0)

    def test_xpress_lane_long_overnight_window(self):
        with SyntheticCache():
            windows = dining.hours("07", D)
            is_open, _ = dining.is_open(windows, datetime(2026, 9, 20, 1, 30))
        self.assertTrue(is_open)


class TestMultiUnitHall(unittest.TestCase):
    def test_one_window_per_unit(self):
        with SyntheticCache():
            windows = dining.hours("06", date(2026, 9, 17))
        self.assertEqual(len(windows), 3)
        self.assertEqual(len({w.name for w in windows}), 3)
        self.assertEqual({w.foodpro_id for w in windows}, {"06"})

    def test_close_reflects_the_last_open_unit(self):
        with SyntheticCache():
            windows = dining.hours("06", date(2026, 9, 17))
            # 12:00: Breakfast (to 13:00) and Lunch (to 18:00) are both open;
            # the hall closes at the LAST of those, 18:00 -> 360 min.
            is_open, mins = dining.is_open(windows, datetime(2026, 9, 17, 12, 0))
            self.assertTrue(is_open)
            self.assertAlmostEqual(mins, 360.0, delta=0.1)
            # 17:00: Lunch (to 18:00) and Dinner (to 21:00) -> 240 min.
            _, mins2 = dining.is_open(windows, datetime(2026, 9, 17, 17, 0))
            self.assertAlmostEqual(mins2, 240.0, delta=0.1)

    def test_multi_unit_closed_when_all_are_past(self):
        with SyntheticCache():
            windows = dining.hours("06", date(2026, 9, 17))
            closed, mins = dining.is_open(windows, datetime(2026, 9, 17, 22, 0))
        self.assertFalse(closed)
        self.assertLess(mins, 0)


# ============================================================ basic rows
class TestBasicFoodRows(unittest.TestCase):
    def test_list_foods_row_shape_and_provenance(self):
        with SyntheticCache():
            res = dining.list_foods(location_num="39", d=D)
        self.assertEqual(res["count"], 3)
        row = res["rows"][0]
        for key in ("location_num", "location_name", "date", "meal", "section",
                    "name", "description", "portion", "diet_tags", "allergens",
                    "allergens_known", "venue_allergen_free", "recipe_id",
                    "source", "fetched_at"):
            self.assertIn(key, row)
        self.assertEqual(row["location_num"], "39")
        self.assertEqual(row["date"], "2026-09-19")
        self.assertEqual(row["source"],
                         cache.key("dining_menu",
                                   {"location_num": "39",
                                    "dtdate": "09/19/2026"}))
        self.assertEqual(row["fetched_at"], SYN_FETCHED)

    def test_basic_rows_never_fetch_nutrition(self):
        with SyntheticCache():
            with patch.object(dining, "nutrition_for_location",
                              side_effect=AssertionError("no nutrition")), \
                    patch.object(dining, "nutrition_bulk",
                                 side_effect=AssertionError("no nutrition")):
                res = dining.list_foods(d=D)
        self.assertGreater(res["count"], 0)

    def test_query_search_is_case_insensitive_substring(self):
        with SyntheticCache():
            res = dining.search_foods("wrap", d=D)
        self.assertGreater(res["count"], 0)
        for row in res["rows"]:
            blob = (f"{row['name']} {row['description']} {row['section']} "
                    f"{row['meal']}").lower()
            self.assertIn("wrap", blob)

    def test_structured_filters(self):
        with SyntheticCache():
            veg = dining.filter_foods(location_num="39", diet="vegetarian", d=D)
            deli = dining.filter_foods(location_num="39", section="deli", d=D)
        self.assertGreater(veg["count"], 0)
        for row in veg["rows"]:
            self.assertIn("vegetarian", row["diet_tags"])
        for row in deli["rows"]:
            self.assertIn("deli", row["section"].lower())

    def test_all_locations_report_every_configured_location(self):
        with SyntheticCache():
            res = dining.list_foods(d=D)
        reported = set(res["sources_ok"]) | {
            s["location_num"] for s in res["sources_skipped"]}
        self.assertEqual(reported, set(config.DINING_LOCATIONS))
        self.assertGreater(len(res["sources_ok"]), 1)
        self.assertTrue(res["sources_skipped"])

    def test_multi_location_rows_span_several_locations(self):
        with SyntheticCache():
            rows = dining.list_foods(d=D)["rows"]
        locs = {r["location_num"] for r in rows}
        self.assertGreaterEqual(len(locs), 2)
        self.assertTrue(locs.issubset(set(config.DINING_LOCATIONS)))


# ============================================================ safety
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

    def test_documented_viridian_exception_is_preserved(self):
        rows = dining.list_foods(location_num="15", d=D,
                                 avoid=("Peanuts", "Tree Nuts"))["rows"]
        venue = [r for r in rows if r["venue_allergen_free"]]
        self.assertEqual(len(venue), 48)
        for row in venue:
            self.assertTrue(row["section"].lower().startswith("viridian"))
            self.assertIs(row["allergens_known"], False)

    def test_viridian_exception_is_bound_to_location_15(self):
        # A section literally named "Viridian" at another hall must NOT inherit
        # the D2 kitchen guarantee: its blank allergens stay UNKNOWN and are
        # therefore excluded under an avoid filter.
        with SyntheticCache():
            rows = dining.list_foods(location_num="39", d=D,
                                     avoid=("Peanuts",))["rows"]
        names = {r["name"] for r in rows}
        self.assertNotIn("Impostor Viridian", names)
        for row in rows:
            self.assertFalse(row["venue_allergen_free"])
        # and the config helper itself is location-bound
        self.assertFalse(config.is_venue_allergen_free("Viridian", "39"))
        self.assertTrue(config.is_venue_allergen_free("Viridian", "15"))
        self.assertFalse(config.is_venue_allergen_free("Viridian"))

    def test_source_contradiction_is_not_recommended(self):
        rows = dining.list_foods(location_num="15", d=D, diet="vegan",
                                 avoid=("Sesame",))["rows"]
        self.assertNotIn("Whole Wheat Penne Pasta", {r["name"] for r in rows})
        for row in rows:
            self.assertIn("vegan", row["diet_tags"])

    def test_avoid_is_a_hard_substring_filter(self):
        rows = dining.list_foods(location_num="15", d=D, avoid=("Milk",))["rows"]
        for row in rows:
            self.assertFalse(any("milk" in a.lower() for a in row["allergens"]))


# ============================================================ failures
class TestPartialFailure(unittest.TestCase):
    def test_one_unreachable_location_does_not_drop_the_others(self):
        with SyntheticCache():
            real = dining.menu_result

            def fake(num, day, **kwargs):
                if str(num) == "39":
                    return dining.MenuResult(
                        "39", "Owens Food Court", day,
                        dining.STATUS_UNAVAILABLE, reason="simulated failure")
                return real(num, day, **kwargs)

            with patch.object(dining, "menu_result", side_effect=fake):
                res = dining.list_foods(d=D)
        skipped = {s["location_num"]: s for s in res["sources_skipped"]}
        self.assertIn("39", skipped)
        self.assertEqual(skipped["39"]["status"], dining.STATUS_UNAVAILABLE)
        self.assertTrue(res["sources_ok"])
        self.assertTrue(all(r["location_num"] != "39" for r in res["rows"]))

    def test_find_food_exposes_typed_sources(self):
        with SyntheticCache():
            res = tools.find_food()
        reported = set(res["sources_ok"]) | {
            s["location_num"] for s in res["sources_skipped"]}
        self.assertEqual(reported, set(config.DINING_LOCATIONS))
        self.assertGreater(len(res["sources_ok"]), 1)
        self.assertTrue(res["sources_skipped"])
        for entry in res["sources_skipped"]:
            self.assertIn("status", entry)
            self.assertTrue(entry["reason"])

    def test_all_locations_kcal_query_returns_no_unproven_items(self):
        with SyntheticCache():
            res = tools.find_food(max_kcal=800)
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["items"], [])
        self.assertEqual(res["sources_ok"], [])
        self.assertTrue(res["sources_skipped"])
        for entry in res["sources_skipped"]:
            self.assertEqual(entry["status"], "nutrition_unavailable")
        self.assertIn("max_kcal", res["reason"])

    def test_single_location_kcal_query_refuses_future_nutrition(self):
        # The committed D2 nutrition was captured AFTER the replay pin, so a
        # single-location hard ceiling must return NO unproven items with a
        # typed nutrition_unavailable state -- never rows with unknown kcal.
        res = tools.find_food(location_num="15", max_kcal=800,
                              avoid=("Peanuts",))
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["items"], [])
        self.assertEqual(res["sources_ok"], [])
        self.assertEqual(res["sources_skipped"][0]["location_num"], "15")
        self.assertEqual(res["sources_skipped"][0]["status"],
                         "nutrition_unavailable")

    def test_single_location_kcal_query_proves_calories_when_contemporaneous(
            self):
        # A synthetic cache whose nutrition was captured BEFORE the replay pin
        # (the live-like case) still proves and attaches calories.
        with SyntheticCache():
            res = tools.find_food(location_num="39", max_kcal=800)
        self.assertGreater(res["count"], 0)
        self.assertTrue(all(r["kcal"] is not None for r in res["items"]))
        self.assertTrue(all(r["kcal"] <= 800 for r in res["items"]))
        self.assertEqual(res["sources_ok"], ["39"])

    def test_future_captured_nutrition_is_not_used(self):
        # Real fixtures: D2's derived all-menu envelope (16:37Z) and every menu
        # chunk (16:29Z) were captured AFTER the 15:22:29Z pin, so replay must
        # leave kcal unknown rather than present later data as contemporaneous.
        self.assertEqual(dining.nutrition_for_location("15", D), {})
        # ...while the ONE contemporaneous chunk is still used normally.
        good = dining.nutrition_bulk([("214022", "1", 1),
                                      ("141002", "2", 1)])
        self.assertEqual(set(good), {"214022", "141002"})

    def test_future_derived_envelope_is_skipped_for_a_real_chunk(self):
        # 71's derived all-menu envelope is future-captured; its contemporaneous
        # raw chunk must win instead, proving the future envelope was skipped.
        with SyntheticCache():
            nut = dining.nutrition_for_location("71", D)
        self.assertEqual(set(nut), {"R1"})
        self.assertEqual(nut["R1"].cals, 420.0)

    def test_single_location_find_food_only_reports_that_location(self):
        res = tools.find_food(location_num="39")
        # D2-only replay: 39 is honestly unavailable, not silently dropped
        self.assertIn("39", res["sources_ok"] + [
            s["location_num"] for s in res["sources_skipped"]])
        self.assertTrue(all(r["location_num"] == "39" for r in res["items"]))


# ============================================================ provenance
class TestStalenessAndProvenance(unittest.TestCase):
    def test_stale_flag_after_the_age_threshold(self):
        future = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
        with SyntheticCache():
            with patch.object(dining.config, "now", return_value=future):
                st = dining.location_status("39", D, at=AT, max_age_s=6 * 3600)
        self.assertIs(st.stale, True)
        self.assertEqual(st.fetched_at, SYN_FETCHED)

    def test_fresh_copy_is_not_stale(self):
        soon = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
        with SyntheticCache():
            with patch.object(dining.config, "now", return_value=soon):
                st = dining.location_status("39", D, at=AT, max_age_s=6 * 3600)
        self.assertIs(st.stale, False)

    def test_unknown_threshold_reports_unknown_staleness(self):
        with SyntheticCache():
            st = dining.location_status("39", D, at=AT, max_age_s=None)
        self.assertIsNone(st.stale)

    def test_menu_result_provenance_uses_public_metadata(self):
        with SyntheticCache():
            res = dining.menu_result("39", D)
        self.assertEqual(res.source,
                         cache.key("dining_menu",
                                   {"location_num": "39",
                                    "dtdate": "09/19/2026"}))
        self.assertEqual(res.fetched_at, SYN_FETCHED)


# ============================================================ live TTL
class TestLiveTTLForwarding(unittest.TestCase):
    """LIVE paths must enforce the menu/nutrition TTL, not reuse cache forever."""

    def test_menu_defaults_to_the_menu_ttl(self):
        calls = []

        def spy(name, url, **kw):
            calls.append((name, kw.get("max_age_s")))
            return {"locationNum": "39", "date": "09/19/2026", "meals": [{
                "mealName": "Lunch", "sections": [{
                    "sectionName": "Deli",
                    "recipes": [_recipe("R1", "TTL Dish")]}]}]}, {}

        with patch.object(dining.cache, "get_json_with_metadata",
                          side_effect=spy):
            dining.menu("39", D)
        self.assertEqual(calls, [("dining_menu",
                                  config.DEFAULT_MENU_CACHE_MAX_AGE_S)])

    def test_eat_options_forwards_an_explicit_ttl(self):
        calls = []

        def spy(name, url, **kw):
            calls.append((name, kw.get("max_age_s")))
            return {"locationNum": "39", "date": "09/19/2026",
                    "meals": []}, {}

        with patch.object(dining.cache, "get_json_with_metadata",
                          side_effect=spy):
            dining.eat_options("39", D, max_age_s=42)
        self.assertTrue(calls)
        self.assertTrue(all(ttl == 42 for _, ttl in calls))

    def test_find_food_forwards_ttl_to_single_location_nutrition(self):
        seen = []
        real = dining.nutrition_for_location

        def spy(num, day, force=False, max_age_s="MISSING"):
            seen.append(max_age_s)
            return real(num, day, force=force, max_age_s=max_age_s)

        with SyntheticCache():
            with patch.object(dining, "nutrition_for_location",
                              side_effect=spy):
                tools.find_food(location_num="39", max_kcal=800,
                                max_age_s=77)
        self.assertTrue(seen)
        self.assertTrue(all(v == 77 for v in seen), seen)


# ============================================================ determinism
class TestDeterministicReplay(unittest.TestCase):
    def test_list_foods_replays_identically(self):
        with SyntheticCache():
            first = json.dumps(dining.list_foods(d=D), sort_keys=True)
            second = json.dumps(dining.list_foods(d=D), sort_keys=True)
        self.assertEqual(first, second)

    def test_find_food_ranking_replays_identically(self):
        with SyntheticCache():
            first = json.dumps(tools.find_food()["items"], sort_keys=True)
            second = json.dumps(tools.find_food()["items"], sort_keys=True)
        self.assertEqual(first, second)

    def test_status_replays_identically(self):
        with SyntheticCache():
            first = dining.location_status("39", D, at=AT).as_dict()
            second = dining.location_status("39", D, at=AT).as_dict()
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))


if __name__ == "__main__":
    unittest.main()