"""Focused tests for live request time, /api/time, and live bus freshness.

All offline (DEMO_MODE=cache). "Live" behavior is exercised by passing an
explicit captured `now` and `live=True` into the HTTP-free pipeline, so no
network or wall-clock timing is involved.

These guard the behavior the demo depends on:
  * one captured `evaluated_at` drives planning in every mode
  * replay stays pinned/deterministic and never ticks
  * live free text with no deadline clarifies instead of borrowing the demo window
  * a passed deadline is corrected, never silently rolled to tomorrow
  * preset windows are byte-for-byte unchanged in cache mode
  * /api/time is lightweight (no GTFS/dining/live-bus load)
  * live bus snapshots honor config.LIVE_BUS_POLL_SECONDS and never force GTFS
"""
from __future__ import annotations

import http.client
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo
from unittest import mock

from app import server
from hokieday import config, gtfs, livebus, tools

TZ = ZoneInfo(config.CAMPUS_TZ)
# The frozen fixture's own campus-local capture time (see test_integration.py).
PINNED = datetime(2026, 9, 19, 11, 22, 29, tzinfo=TZ)


class TestTimeEndpoint(unittest.TestCase):
    def test_returns_required_fields(self):
        t = server.time_endpoint(now=PINNED, live=False)
        for key in ("mode", "is_replay", "timezone", "iso", "human", "clock",
                    "evaluated_at", "pinned", "ticking"):
            self.assertIn(key, t)
        self.assertEqual(t["timezone"], config.CAMPUS_TZ)
        self.assertEqual(t["human"], "11:22 AM")
        self.assertEqual(t["clock"], "11:22:29")
        self.assertEqual(t["iso"], "2026-09-19T11:22:29-04:00")
        self.assertTrue(t["is_replay"])
        self.assertTrue(t["pinned"])
        self.assertFalse(t["ticking"])

    def test_live_mode_ticked_and_not_pinned(self):
        t = server.time_endpoint(now=PINNED, live=True)
        self.assertFalse(t["is_replay"])
        self.assertFalse(t["pinned"])
        self.assertTrue(t["ticking"])
        self.assertEqual(t["time_source"], "wall_clock")

    def test_is_lightweight_never_touches_heavy_sources(self):
        """The whole point of /api/time: it must not load GTFS, dining or buses."""
        with mock.patch.object(server.tools, "get_live_bus",
                               side_effect=AssertionError("heavy live bus load")), \
             mock.patch.object(server.cache, "stats",
                               side_effect=AssertionError("heavy status load")):
            t = server.time_endpoint(now=PINNED, live=False)
        self.assertEqual(t["iso"], PINNED.isoformat(timespec="seconds"))


class TestRequestMetadata(unittest.TestCase):
    def test_meta_shape(self):
        m = server.time_meta(PINNED, live=False)
        self.assertEqual(set(m), {"mode", "evaluated_at", "time_source", "is_replay"})
        self.assertEqual(m["evaluated_at"], "2026-09-19T11:22:29-04:00")
        self.assertEqual(m["time_source"], "snapshot")
        self.assertTrue(m["is_replay"])

    def test_every_ask_carries_time_even_on_error(self):
        result, code = server.handle_ask({"scenario_id": "nope"}, now=PINNED)
        self.assertEqual(code, 400)
        self.assertEqual(result["_time"]["evaluated_at"],
                         "2026-09-19T11:22:29-04:00")

    def test_captured_now_is_reused_for_planning(self):
        result, code = server.handle_ask({"scenario_id": "eat"}, now=PINNED)
        self.assertEqual(code, 200)
        self.assertEqual(result["_time"], server.time_meta(PINNED, live=False))
        self.assertEqual(result["_request"]["start"], "11:22")
        self.assertEqual(result["_request"]["end"], "13:25")


class TestLiveFreeText(unittest.TestCase):
    def test_one_deadline_plans_from_now(self):
        now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ)
        res, code = server.handle_ask(
            {"text": "I need to be at McBryde by 1:25 PM"}, now=now, live=True)
        self.assertEqual(code, 200)
        self.assertIsNone(res.get("clarification"))
        self.assertEqual(res["_time"]["evaluated_at"],
                         "2026-09-19T10:00:00-04:00")
        self.assertFalse(res["_time"]["is_replay"])
        self.assertEqual(res["itinerary"]["leave_time"],
                         "2026-09-19T10:00:00-04:00")
        self.assertEqual(res["itinerary"]["window_end"],
                         "2026-09-19T13:25:00-04:00")

    def test_no_deadline_returns_clarification(self):
        now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ)
        res, code = server.handle_ask({"text": "I'm hungry, where can I eat?"},
                                      now=now, live=True)
        self.assertEqual(code, 200)
        self.assertIsNone(res["itinerary"])
        self.assertEqual(res["clarification"]["kind"], "need_deadline")
        # It must NOT borrow the frozen demo window.
        self.assertNotEqual(res["_request"]["start"], "11:22")

    def test_passed_deadline_is_corrected_not_rolled_forward(self):
        now = datetime(2026, 9, 19, 14, 0, tzinfo=TZ)
        res, code = server.handle_ask(
            {"text": "Can I make my 1:25 PM class?"}, now=now, live=True)
        self.assertEqual(code, 200)
        self.assertIsNone(res["itinerary"])
        self.assertEqual(res["clarification"]["kind"], "deadline_passed")
        self.assertIn("1:25 PM", res["clarification"]["detail"])

    def test_two_explicit_times_are_honored(self):
        now = datetime(2026, 9, 19, 9, 0, tzinfo=TZ)
        res, code = server.handle_ask(
            {"text": "I'm free from 11:22 to 13:25 and I'm hungry"},
            now=now, live=True)
        self.assertEqual(code, 200)
        self.assertIsNone(res.get("clarification"))
        self.assertEqual(res["itinerary"]["window_end"],
                         "2026-09-19T13:25:00-04:00")


class TestPresetGuard(unittest.TestCase):
    def test_passed_preset_deadline_is_corrected(self):
        now = datetime(2026, 9, 19, 14, 0, tzinfo=TZ)
        res, code = server.handle_ask({"scenario_id": "tight"}, now=now, live=True)
        self.assertEqual(code, 200)
        self.assertIsNone(res["itinerary"])
        self.assertEqual(res["clarification"]["kind"], "deadline_passed")

    def test_future_preset_still_plans(self):
        now = datetime(2026, 9, 19, 11, 0, tzinfo=TZ)
        res, code = server.handle_ask({"scenario_id": "tight"}, now=now, live=True)
        self.assertEqual(code, 200)
        self.assertIsNone(res.get("clarification"))
        self.assertIsNotNone(res["itinerary"])

    def test_future_preset_starts_at_captured_now(self):
        """Live preset window opens at the request time, not the frozen 11:22.

        Request at 12:00 with a 13:25 deadline: the frozen start (11:22) is a
        plan that already left, so the start must be the captured now while the
        explicit deadline is preserved on the same campus date.
        """
        now = datetime(2026, 9, 19, 12, 0, tzinfo=TZ)
        res, code = server.handle_ask({"scenario_id": "eat"}, now=now, live=True)
        self.assertEqual(code, 200)
        self.assertIsNone(res.get("clarification"))
        self.assertEqual(res["itinerary"]["leave_time"],
                         "2026-09-19T12:00:00-04:00")
        self.assertEqual(res["itinerary"]["window_end"],
                         "2026-09-19T13:25:00-04:00")

    def test_cache_mode_preset_unchanged(self):
        res, code = server.handle_ask({"scenario_id": "tight"}, now=PINNED)
        self.assertEqual(code, 200)
        self.assertIsNone(res.get("clarification"))
        self.assertIsNotNone(res["itinerary"])
        self.assertTrue(res["_time"]["is_replay"])
        # Cache mode keeps the frozen start exactly -- no live materialization.
        self.assertEqual(res["_request"]["start"], "11:22")
        self.assertEqual(res["itinerary"]["leave_time"],
                         "2026-09-19T11:22:00-04:00")


class TestCacheCompatibility(unittest.TestCase):
    """DEMO_MODE=cache keeps the fixed demo window and pinned snapshot exactly."""

    def test_default_free_text_window_is_unchanged(self):
        call = server.parse_free_text("Can I eat and make my 1:25 class?")
        self.assertEqual((call["start"], call["end"]), ("11:22", "13:25"))
        self.assertTrue(call["_interpretation_notes"])

    def test_no_deadline_in_cache_mode_still_uses_demo_window(self):
        call = server.parse_free_text("I'm hungry")
        self.assertEqual((call["start"], call["end"]), ("11:22", "13:25"))
        self.assertNotIn("_clarification", call)

    def test_pinned_clock_reaches_plan_day(self):
        r = server.run_plan({"student_ref": "demo-student-1", "start": "11:22",
                             "end": "13:25", "prefs": {}})
        self.assertEqual(r["itinerary"]["leave_time"],
                         "2026-09-19T11:22:00-04:00")


class TestCapturedNowDrivesTools(unittest.TestCase):
    def test_plan_day_anchors_bare_times_to_the_captured_date(self):
        other_day = datetime(2026, 9, 20, 11, 0, tzinfo=TZ)
        r = tools.plan_day("demo-student-1", "11:22", "13:25", now=other_day)
        self.assertIsNone(r.get("error"))
        self.assertEqual(r["itinerary"]["window_end"],
                         "2026-09-20T13:25:00-04:00")

    def test_context_is_restored_after_plan_day(self):
        """One request's clock must not leak into the next call."""
        other_day = datetime(2026, 9, 20, 11, 0, tzinfo=TZ)
        tools.plan_day("demo-student-1", "11:22", "13:25", now=other_day)
        self.assertEqual(tools._now_campus(), config.now(TZ))


class TestLiveBusFreshness(unittest.TestCase):
    def test_fetch_vehicles_honors_poll_window(self):
        with mock.patch.object(livebus.cache, "get_json",
                               return_value={"data": [{"id": "1"}]}) as m:
            rows = livebus.fetch_vehicles()
        self.assertEqual(rows, [{"id": "1"}])
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs.get("max_age_s"), config.LIVE_BUS_POLL_SECONDS)
        self.assertFalse(kwargs.get("force"))

    def test_force_still_bypasses_the_cache(self):
        with mock.patch.object(livebus.cache, "get_json",
                               return_value={"data": []}) as m:
            livebus.fetch_vehicles(force=True)
        self.assertTrue(m.call_args.kwargs.get("force"))
        self.assertEqual(m.call_args.kwargs.get("max_age_s"),
                         config.LIVE_BUS_POLL_SECONDS)

    def test_bus_poll_never_forces_gtfs(self):
        with mock.patch.object(livebus, "fetch_vehicles", return_value=[]), \
             mock.patch.object(gtfs, "load_gtfs") as lg:
            livebus.live(force=True)
        lg.assert_called_once_with(force=False)

    def test_staleness_uses_the_supplied_request_clock(self):
        observed = datetime(2026, 9, 19, 11, 0, tzinfo=timezone.utc)
        fresh_now = observed + timedelta(minutes=config.STALE_LIVE_MINUTES - 1)
        stale_now = observed + timedelta(minutes=config.STALE_LIVE_MINUTES + 1)
        self.assertFalse(livebus._is_stale(observed, now=fresh_now))
        self.assertTrue(livebus._is_stale(observed, now=stale_now))


class TestHttpSmoke(unittest.TestCase):
    """Real HTTP round-trips: /api/time is lightweight and replay stays pinned."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        except OSError as exc:                                # pragma: no cover
            raise unittest.SkipTest(f"could not bind a test port: {exc}")
        cls.host, cls.port = cls.httpd.server_address[:2]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def _post(self, path, body):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            conn.request("POST", path, body=json.dumps(body),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_api_time_is_lightweight_and_pinned(self):
        with mock.patch.object(server.tools, "get_live_bus",
                               side_effect=AssertionError("heavy live bus load")), \
             mock.patch.object(server.cache, "stats",
                               side_effect=AssertionError("heavy status load")):
            status, body = self._get("/api/time")
        self.assertEqual(status, 200)
        self.assertTrue(body["is_replay"])
        self.assertTrue(body["pinned"])
        self.assertFalse(body["ticking"])
        self.assertEqual(body["timezone"], config.CAMPUS_TZ)
        self.assertEqual(body["evaluated_at"], "2026-09-19T11:22:29-04:00")

    def test_api_ask_replay_is_pinned(self):
        status, body = self._post("/api/ask", {"scenario_id": "eat"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(body["itinerary"])
        self.assertTrue(body["_time"]["is_replay"])
        self.assertEqual(body["_time"]["time_source"], "snapshot")
        self.assertEqual(body["_time"]["evaluated_at"],
                         "2026-09-19T11:22:29-04:00")


if __name__ == "__main__":
    unittest.main()