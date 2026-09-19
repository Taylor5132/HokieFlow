"""Offline tests for hokieday.livebus (stdlib unittest, DEMO_MODE=cache, no network).

Two layers:
  (A) unit tests against a hand-built fake Gtfs (never imports hokieday.gtfs)
  (B) an integration test of the keystone join, skipped until the gtfs worker
      lands hokieday/gtfs.py -- it asserts the VERIFIED result: 13 vehicles,
      13/13 non-None schedule deltas, routes SME/NMG/HDG/UCB/TCP/HWC.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hokieday import config, livebus

REPO = Path(__file__).resolve().parent.parent
NY = ZoneInfo("America/New_York")
GTFS_LANDED = os.path.exists(os.path.join(REPO, "hokieday", "gtfs.py"))


def campus_utc(y, mo, d, h, mi, s=0) -> datetime:
    """A UTC instant given as campus-local wall time (America/New_York)."""
    return datetime(y, mo, d, h, mi, s, tzinfo=NY).astimezone(timezone.utc)


# ---------------------------------------------------------------- fake Gtfs
class FakeGtfs:
    """Minimal duck-typed stand-in for the frozen Gtfs shape."""

    def __init__(self, trips=None, stop_times=None):
        self.trips = trips or {}
        self.stop_times = stop_times or {}


FAKE = FakeGtfs(
    trips={"T1": {"route_id": "SME"}, "T2": {"route_id": "NMG"}},
    stop_times={
        # (seq, stop_id, arr, dep) -- GTFS 'HH:MM:SS', may exceed 24:00:00
        "T1": [(1, "1635", "11:20:23", "11:20:23"), (2, "1700", "11:25:00", "11:25:00")],
        "T2": [(1, "1411", "25:10:00", "25:10:00")],      # after-midnight service
    },
)


def obs(trip="T1", stop="1635") -> livebus.BusObs:
    return livebus.BusObs(
        bus_id="6413", route_id="SME", stop_id=stop, lat=37.2, lon=-80.4,
        speed=1.0, passengers=24, load_pct=30, at_stop=True,
        gtfs_trip_id=trip, observed_at=config.now(),
    )


class TestScheduleDelta(unittest.TestCase):
    """(A) sign + magnitude of the keystone delta, against a fake Gtfs."""

    def test_on_time_is_zero(self):
        now = campus_utc(2026, 9, 19, 11, 20, 23)
        self.assertAlmostEqual(livebus.schedule_delta(obs(), FAKE, now=now), 0.0)

    def test_late_is_positive(self):
        # scheduled dep 11:20:23, bus observed at 11:25:23 -> 5 min LATE
        now = campus_utc(2026, 9, 19, 11, 25, 23)
        self.assertAlmostEqual(livebus.schedule_delta(obs(), FAKE, now=now), 5.0)

    def test_early_is_negative(self):
        # scheduled dep 11:20:23, bus observed at 11:15:23 -> 5 min EARLY
        now = campus_utc(2026, 9, 19, 11, 15, 23)
        self.assertAlmostEqual(livebus.schedule_delta(obs(), FAKE, now=now), -5.0)

    def test_second_stop_of_trip(self):
        now = campus_utc(2026, 9, 19, 11, 26, 30)
        self.assertAlmostEqual(
            livebus.schedule_delta(obs(stop="1700"), FAKE, now=now), 1.5)

    def test_unmatched_trip_returns_none(self):
        self.assertIsNone(livebus.schedule_delta(obs(trip="NOPE"), FAKE,
                                                now=campus_utc(2026, 9, 19, 11, 20)))

    def test_unmatched_stop_returns_none(self):
        self.assertIsNone(livebus.schedule_delta(obs(stop="9999"), FAKE,
                                                now=campus_utc(2026, 9, 19, 11, 20)))

    def test_after_midnight_time_over_24h(self):
        # dep '25:10:00' on service date D-1 == 01:10 on calendar day D.
        # Observed 00:20 on day D -> 50 min EARLY.
        now = campus_utc(2026, 9, 20, 0, 20)
        self.assertAlmostEqual(
            livebus.schedule_delta(obs(trip="T2", stop="1411"), FAKE, now=now), -50.0)

    def test_never_raises_on_broken_gtfs(self):
        self.assertIsNone(livebus.schedule_delta(obs(), FakeGtfs(), now=None))


class TestNormalize(unittest.TestCase):
    """(A) raw vehicle dict -> BusObs, including states[-1] and at_stop parsing."""

    def make_vehicle(self, version=1789833056000, at_stop="Y"):
        return {
            "id": "6413", "routeId": "SME", "stopId": "1635",
            "capacity": "24", "percentOfCapacity": "30",
            "gtfsTripId": "d8eb4606-890a-47f3-a2f0-faf883e22497",
            "states": [
                {"latitude": 1.0, "longitude": 2.0, "speed": "0",
                 "passengers": "10", "isBusAtStop": "N", "version": 111},
                {"latitude": 37.2246213333333, "longitude": -80.408931,
                 "realtimeLatitude": 37.2247, "realtimeLongitude": -80.4089,
                 "speed": "1", "passengers": "24", "isBusAtStop": at_stop,
                 "version": version},
            ],
        }

    def test_uses_last_state(self):
        o = livebus.normalize([self.make_vehicle()])[0]
        self.assertEqual(o.lat, 37.2247)          # realtime coords of states[-1]
        self.assertEqual(o.lon, -80.4089)
        self.assertEqual(o.passengers, 24)        # not the states[0] value 10

    def test_observed_at_from_epoch_ms(self):
        o = livebus.normalize([self.make_vehicle(version=1789833056000)])[0]
        self.assertEqual(o.observed_at,
                         datetime.fromtimestamp(1789833056000 / 1000, tz=timezone.utc))

    def test_observed_at_fallback(self):
        v = self.make_vehicle()
        v["states"][-1].pop("version")
        fb = campus_utc(2026, 9, 19, 11, 0, 0)
        self.assertEqual(livebus.normalize([v], observed_at=fb)[0].observed_at, fb)

    def test_at_stop_parsed_from_Y_N(self):
        self.assertTrue(livebus.normalize([self.make_vehicle(at_stop="Y")])[0].at_stop)
        self.assertFalse(livebus.normalize([self.make_vehicle(at_stop="N")])[0].at_stop)

    def test_load_pct_uses_percentOfCapacity_not_capacity(self):
        # capacity=24 + passengers=24 while percentOfCapacity=30 is the VERIFIED
        # inconsistency; load_pct must be 30, never a capacity-derived value.
        o = livebus.normalize([self.make_vehicle()])[0]
        self.assertEqual(o.load_pct, 30)
        self.assertEqual(o.passengers, 24)


class TestStaleness(unittest.TestCase):
    def test_stale_after_threshold(self):
        fresh = livebus.BusObs(bus_id="1", route_id="SME", stop_id="1635", lat=0, lon=0,
                               speed=0, passengers=0, load_pct=0, at_stop=False,
                               gtfs_trip_id="T1", observed_at=config.now())
        old = fresh.__class__(**{**fresh.__dict__,
                                 "observed_at": config.now()
                                 - timedelta(minutes=config.STALE_LIVE_MINUTES + 2)})
        self.assertFalse(livebus._is_stale(fresh.observed_at))
        self.assertTrue(livebus._is_stale(old.observed_at))


class TestAppendBronze(unittest.TestCase):
    def test_appends_rows_with_fetched_at(self):
        vehicles = [{"id": "6413", "routeId": "SME"}, {"id": "6412", "routeId": "NMG"}]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bus_bronze.jsonl"
            n = livebus.append_bronze(vehicles, path=path)
            self.assertEqual(n, 2)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            for line, veh in zip(lines, vehicles):
                rec = json.loads(line)
                self.assertIn("fetched_at", rec)
                self.assertEqual(rec["id"], veh["id"])

    def test_default_path_is_data_dir(self):
        vehicles = [{"id": "9999", "routeId": "TST"}]
        with tempfile.TemporaryDirectory() as td:
            old = config.DATA_DIR
            try:
                config.DATA_DIR = Path(td)
                n = livebus.append_bronze(vehicles)      # path=None -> default
                self.assertEqual(n, 1)
                self.assertTrue((Path(td) / "bus_bronze.jsonl").exists())
            finally:
                config.DATA_DIR = old


# ------------------------------------------------------- (B) integration
@unittest.skipUnless(GTFS_LANDED, "gtfs.py not landed yet")
class TestKeystoneJoin(unittest.TestCase):
    """Real fixture -> real static schedule. Verified 13/13 join."""

    @classmethod
    def setUpClass(cls):
        from hokieday import gtfs as gtfs_mod
        cls.gtfs_mod = gtfs_mod
        cls.vehicles = livebus.fetch_vehicles()
        cls.obs_list = livebus.normalize(cls.vehicles)
        cls.g = gtfs_mod.load_gtfs()

    def test_fixture_yields_13_vehicles(self):
        self.assertEqual(len(self.vehicles), 13)

    def test_normalize_returns_13_busobs(self):
        self.assertEqual(len(self.obs_list), 13)
        self.assertTrue(all(isinstance(o, livebus.BusObs) for o in self.obs_list))

    def test_routes_include_expected_set(self):
        routes = {o.route_id for o in self.obs_list}
        for r in ("SME", "NMG", "HDG", "UCB", "TCP", "HWC"):
            self.assertIn(r, routes)

    def test_at_least_one_crowded_bus(self):
        self.assertTrue(any(o.load_pct > 0 for o in self.obs_list))

    def test_at_stop_is_real_bool(self):
        self.assertTrue(all(isinstance(o.at_stop, bool) for o in self.obs_list))
        self.assertTrue(any(o.at_stop for o in self.obs_list))

    def test_keystone_join_all_vehicles(self):
        deltas = [livebus.schedule_delta(o, self.g) for o in self.obs_list]
        nones = [o.bus_id for o, d in zip(self.obs_list, deltas) if d is None]
        self.assertEqual(nones, [],
                         f"unmatched vehicles (join bug): {nones}")
        self.assertEqual(len(deltas), 13)
        # verified live: buses ran EARLY (3-8 min), i.e. small negative deltas
        self.assertTrue(all(-30.0 <= d <= 30.0 for d in deltas),
                         f"implausible deltas: {deltas}")

    def test_live_snapshot(self):
        rows = livebus.live()
        self.assertEqual(len(rows), 13)
        self.assertTrue(all(isinstance(r, livebus.BusLive) for r in rows))
        self.assertTrue(all(r.sched_delta_min is not None for r in rows))
        self.assertTrue(all(isinstance(r.is_stale, bool) for r in rows))


if __name__ == "__main__":
    unittest.main()
