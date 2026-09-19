"""Offline tests for hokieday.gtfs (DEMO_MODE=cache, no network).

Numbers asserted here are verified against the real feed fixture
(cache/gtfs, FY27 Blacksburg 1.6A, captured 2026-09-19) - see SDD.md 5.2 v1.1.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime

from hokieday import config, gtfs


class GtfsFixtureTests(unittest.TestCase):
    """The whole feed is loaded once; every test below is offline."""

    @classmethod
    def setUpClass(cls):
        cls.assertTrue(config.CACHE_ONLY, "tests must run with DEMO_MODE=cache")
        cls.g = gtfs.load_gtfs()

    # ------------------------------------------------------- feed shape
    def test_feed_counts(self):
        self.assertEqual(len(self.g.stops), 297)
        self.assertEqual(len(self.g.routes), 24)
        self.assertEqual(len(self.g.trips), 3658)
        self.assertEqual(
            sum(len(v) for v in self.g.stop_times.values()), 74301)
        self.assertEqual(
            sum(len(v) for v in self.g.calendar_dates.values()), 181)

    def test_no_calendar_txt_needed(self):
        # explicit: only calendar_dates.txt drives service resolution
        import pathlib
        self.assertFalse((config.GTFS_DIR / "calendar.txt").exists())

    # ------------------------------------------------------- service
    def test_service_ids_for_date_saturday_nonempty(self):
        # THE regression test for the missing-calendar.txt bug: service
        # resolution must come from calendar_dates.txt, and on Sat
        # 2026-09-19 exactly 2 of the feed's 8 services are active.
        sids = gtfs.service_ids_for_date(self.g, date(2026, 9, 19))
        self.assertIsInstance(sids, set)
        self.assertEqual(len(sids), 2)
        self.assertTrue(sids, "service_ids_for_date must not be empty on 2026-09-19")

    def test_active_trip_ids_are_subset_of_trips(self):
        trips = gtfs.active_trip_ids(self.g, date(2026, 9, 19))
        self.assertGreater(len(trips), 0)
        self.assertTrue(trips.issubset(self.g.trips.keys()))

    # ------------------------------------------------------- geometry
    def test_stop_1600_is_nearest_to_burruss_within_5m(self):
        # SDD: nearest stop to Burruss Hall (37.22957, -80.41394) is stop
        # 1600 "Main/Roanoke Sbnd" at ~37 m.
        scored = gtfs.nearest_stops(self.g, 37.22957, -80.41394, k=1)
        dist_m, stop = scored[0]
        self.assertEqual(stop.stop_id, "1600")
        self.assertEqual(stop.name, "Main/Roanoke Sbnd")
        self.assertLessEqual(abs(dist_m - 37.0), 5.0)

    def test_walk_minutes_matches_formula(self):
        m = gtfs.walk_minutes((37.22957, -80.41394), (37.22924, -80.41366))
        import math
        expected = (gtfs._haversine_m((37.22957, -80.41394),
                                      (37.22924, -80.41366))
                    * config.WALK_PATH_FACTOR / config.WALK_SPEED_MPS / 60.0)
        self.assertAlmostEqual(m, expected, places=9)
        # sanity: the 37 m hop should be well under 2 walking minutes
        self.assertLess(m, 2.0)
        self.assertGreater(m, 0.0)
        del math

    # ------------------------------------------------------- times > 24h
    def test_parse_time_over_24h(self):
        self.assertEqual(gtfs._parse_time("25:10:00"), 25 * 3600 + 10 * 60)
        self.assertEqual(gtfs._parse_time("27:00:00"), 27 * 3600)
        self.assertEqual(gtfs._parse_time("11:23:29"), 11 * 3600 + 23 * 60 + 29)

    def test_feed_really_has_times_over_24h(self):
        # guard the guard: the fixture must contain after-midnight times,
        # otherwise the >24h parsing path is untested against real data.
        max_dep = max(dep for rows in self.g.stop_times.values()
                      for _seq, _sid, _arr, dep in rows)
        self.assertGreater(max_dep, 24 * 3600)
        self.assertEqual(max_dep, 27 * 3600)

    # ------------------------------------------------------- departures
    def test_next_departures_on_saturday(self):
        # at = 2026-09-19 11:22 campus-local; stop 1600. With STRICT service
        # filtering the first departure is SME 11:48:29 (the earlier draft
        # expectation of 11:23:29 belonged to a service that runs only on
        # 2026-09-12 and must NOT appear on the 19th).
        at = datetime(2026, 9, 19, 11, 22)
        deps = gtfs.next_departures(self.g, "1600", at)
        self.assertTrue(deps)
        got = [(d.route_id, d.dep_time.strftime("%H:%M:%S")) for d in deps[:4]]
        self.assertEqual(
            got,
            [("SME", "11:48:29"), ("HDG", "11:50:21"),
             ("SME", "12:18:29"), ("HDG", "12:20:21")],
        )
        first = deps[0]
        self.assertEqual(first.route_id, "SME")
        self.assertEqual(first.dep_time.hour, 11)
        self.assertEqual(first.dep_time.minute, 48)
        self.assertEqual(first.dep_time.second, 29)
        self.assertEqual(first.service_date, date(2026, 9, 19))
        # 11:48:29 = 42509 s since service-day midnight; at = 11:22:00
        self.assertAlmostEqual(first.in_min, (42509.0 - (11 * 3600 + 22 * 60)) / 60.0,
                               places=6)
        self.assertAlmostEqual(first.in_min, 26.4833333333, places=6)
        self.assertTrue(first.dep_time.tzinfo is not None)

    def test_next_departures_filters_by_service_date(self):
        # Prove the filter actually changes the answer (regression guard):
        # an UNFILTERED lookup at stop 1600 after 11:22 on 2026-09-19 would
        # return SME 11:23:29 - a trip whose service (406882a3...) runs only
        # on 2026-09-12 per calendar_dates.txt. The filtered result must
        # differ, and the 11:23:29 trip must be absent.
        at = datetime(2026, 9, 19, 11, 22)
        cutoff = 11 * 3600 + 22 * 60

        # unfiltered: every trip in the feed serving 1600 after the cutoff
        unfiltered = sorted(
            (dep, tid) for tid, rows in self.g.stop_times.items()
            for _seq, sid, _arr, dep in rows
            if sid == "1600" and dep >= cutoff
        )
        first_dep_s, first_tid = unfiltered[0]
        first_trip = self.g.trips[first_tid]
        self.assertEqual(first_dep_s, 11 * 3600 + 23 * 60 + 29)   # 11:23:29
        self.assertEqual(first_trip["route_id"], "SME")
        self.assertEqual(first_trip["service_id"], next(
            sid for sid, ex in self.g.calendar_dates.items()
            if ex == {date(2026, 9, 12): 1}))
        self.assertNotIn(first_trip["service_id"],
                         gtfs.service_ids_for_date(self.g, date(2026, 9, 19)))

        # filtered: different (later) first departure, 11:23:29 absent
        deps = gtfs.next_departures(self.g, "1600", at, limit=100)
        self.assertNotEqual(deps[0].dep_time.strftime("%H:%M:%S"), "11:23:29")
        self.assertFalse(any(d.trip_id == first_tid for d in deps))

    def test_next_departures_route_and_horizon(self):
        at = datetime(2026, 9, 19, 11, 22)
        sme = gtfs.next_departures(self.g, "1600", at, route_id="SME", limit=3)
        self.assertEqual([d.route_id for d in sme], ["SME"] * 3)
        self.assertEqual(sme[0].dep_time.strftime("%H:%M:%S"), "11:48:29")

        # first departure is 26.48 min out, so a 30-min horizon catches it
        short = gtfs.next_departures(self.g, "1600", at, horizon_min=30)
        self.assertTrue(short)
        self.assertTrue(all(d.in_min <= 30 for d in short))
        # no departure between 11:22 and 11:32 -> empty horizon yields nothing
        self.assertEqual(gtfs.next_departures(self.g, "1600", at, horizon_min=1), [])

    def test_next_departures_unknown_stop(self):
        self.assertEqual(
            gtfs.next_departures(self.g, "no-such-stop", datetime(2026, 9, 19, 11, 22)),
            [])


if __name__ == "__main__":
    unittest.main()
