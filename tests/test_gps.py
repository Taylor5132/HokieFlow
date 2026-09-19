"""Device-position (GPS) origin: acceptance, rejection, and fallback.

OWNER: integrator. The planner itself needed no new code path -- a device
position is registered as a dynamic place and passed as `from_place` -- so these
tests focus on the server-side guard, which is where the real risk lives:

  * a LAPTOP's Wi-Fi geolocation commonly resolves to the ISP, hundreds of km
    from campus. Accepting it silently would produce a plan containing a
    multi-day walk, presented with full confidence.
  * geolocation needs a SECURE CONTEXT, so a plain-HTTP LAN phone cannot use it;
    the picker fallback must always yield a usable origin.

Offline. DEMO_MODE=cache only.
"""
from __future__ import annotations

import unittest

from hokieday import config, tools

from app import server

BURRUSS = (37.22957, -80.41394)
NEAR_CAMPUS = (37.22990, -80.41420)          # ~40 m from Burruss
FAR_AWAY = (38.90, -77.03)                   # Washington DC, ~350 km


class TestDynamicPlaceRegistry(unittest.TestCase):
    def test_registers_and_returns_a_usable_key(self):
        key = config.register_dynamic_place(*NEAR_CAMPUS, "your location", 12.0)
        self.assertIn(key, config.PLACES)
        row = config.PLACES[key]
        self.assertAlmostEqual(row["lat"], NEAR_CAMPUS[0], places=5)
        self.assertAlmostEqual(row["lon"], NEAR_CAMPUS[1], places=5)
        self.assertTrue(config.is_dynamic(key))
        self.assertFalse(row["verified"], "device coords are not survey-grade")

    def test_same_position_yields_the_same_key(self):
        """Keys are derived from the coordinates, so concurrent requests from one
        spot share an entry instead of racing over a single mutable 'current'."""
        a = config.register_dynamic_place(*NEAR_CAMPUS, accuracy_m=9.0)
        b = config.register_dynamic_place(*NEAR_CAMPUS, accuracy_m=9.0)
        self.assertEqual(a, b)

    def test_rejects_coordinates_that_are_not_on_earth(self):
        for lat, lon in ((91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)):
            with self.assertRaises(ValueError):
                config.register_dynamic_place(lat, lon)

    def test_registry_is_bounded(self):
        """Otherwise a long-running demo leaks a place per distinct position."""
        for i in range(config.MAX_DYNAMIC_PLACES + 25):
            config.register_dynamic_place(37.0 + i * 0.0001, -80.4, "leak test")
        dynamic = [k for k, v in config.PLACES.items() if v.get("dynamic")]
        self.assertLessEqual(len(dynamic), config.MAX_DYNAMIC_PLACES)


class TestOriginResolution(unittest.TestCase):
    """The guard that stops a nonsense plan from being presented confidently."""

    def test_no_position_uses_the_default(self):
        key, info = server.resolve_origin({})
        self.assertIsNone(key)
        self.assertEqual(info["source"], "default")

    def test_nearby_position_is_accepted(self):
        key, info = server.resolve_origin(
            {"lat": NEAR_CAMPUS[0], "lon": NEAR_CAMPUS[1], "accuracy": 12})
        self.assertIsNotNone(key)
        self.assertEqual(info["source"], "device")
        self.assertEqual(info["accuracy_m"], 12.0)
        self.assertIsNone(info["note"])
        self.assertLess(info["km_from_campus"], 0.1)

    def test_far_position_is_rejected_with_a_reason(self):
        """A laptop on Wi-Fi reports the ISP, not the room. Accepting 350 km would
        yield a multi-day walk shown as fact."""
        key, info = server.resolve_origin({"lat": FAR_AWAY[0], "lon": FAR_AWAY[1],
                                           "accuracy": 5000})
        self.assertIsNone(key, "a 350 km origin must not be used")
        self.assertEqual(info["source"], "default")
        self.assertIn("350", info["note"])
        self.assertGreater(info["rejected_km_from_campus"], 300)
        self.assertIn("Wi-Fi", info["note"])

    def test_poor_accuracy_is_flagged_but_used(self):
        key, info = server.resolve_origin(
            {"lat": NEAR_CAMPUS[0], "lon": NEAR_CAMPUS[1], "accuracy": 400})
        self.assertIsNotNone(key)
        self.assertIn("accuracy", (info["note"] or "").lower())

    def test_garbage_input_falls_back_without_raising(self):
        for payload in ({"lat": "abc", "lon": "def"},
                        {"lat": None, "lon": None},
                        {"lat": 999, "lon": 999},
                        {"lat": [1], "lon": {}}):
            key, info = server.resolve_origin(payload)
            self.assertIsNone(key, f"{payload} should not yield an origin")
            self.assertEqual(info["source"], "default")

    def test_explicit_place_is_used_when_no_position(self):
        key, info = server.resolve_origin({"from_place": "McBryde Hall"})
        self.assertEqual(key, "McBryde Hall")
        self.assertEqual(info["source"], "selected")

    def test_rejected_position_falls_back_to_the_selected_place(self):
        key, info = server.resolve_origin({"lat": FAR_AWAY[0], "lon": FAR_AWAY[1],
                                           "from_place": "McBryde Hall"})
        self.assertEqual(key, "McBryde Hall")
        self.assertEqual(info["source"], "selected")
        self.assertIn("instead", info["note"])

    def test_unknown_place_does_not_become_an_origin(self):
        key, info = server.resolve_origin({"from_place": "Atlantis"})
        self.assertIsNone(key)


class TestPositionActuallyChangesThePlan(unittest.TestCase):
    """The point of the feature: distances are measured from where you ARE.

    Without this, a GPS origin could be accepted and then quietly ignored.
    """

    def test_walk_leg_starts_at_the_device_position(self):
        key = config.register_dynamic_place(*NEAR_CAMPUS, "your location", 10.0)
        from_burruss = tools.plan_day("demo-student-1", "11:22", "13:00",
                                      {"from_place": "Burruss Hall"})
        from_device = tools.plan_day("demo-student-1", "11:22", "13:00",
                                     {"from_place": key})

        first_b = from_burruss["itinerary"]["legs"][0]
        first_d = from_device["itinerary"]["legs"][0]
        self.assertEqual(first_d["from"], key)
        self.assertEqual(first_b["from"], "Burruss Hall")
        self.assertEqual(first_d["type"], "walk")

        # Same trip, different origin => the first walk must differ. Equal values
        # would mean the position was ignored.
        self.assertNotEqual(first_d["minutes"], first_b["minutes"],
                            "the device position did not change the first walk leg")

    def test_closer_origin_never_yields_a_longer_walk(self):
        """Pure sanity on the direction of the effect."""
        near = config.register_dynamic_place(37.22550, -80.41640, "right outside D2", 8.0)
        far = config.register_dynamic_place(37.22957, -80.41394, "Burruss", 8.0)
        a = tools.plan_day("demo-student-1", "11:22", "13:00", {"from_place": near})
        b = tools.plan_day("demo-student-1", "11:22", "13:00", {"from_place": far})
        wa = a["itinerary"]["legs"][0]["minutes"]
        wb = b["itinerary"]["legs"][0]["minutes"]
        self.assertLess(wa, wb, f"walking from beside D2 ({wa}) should be shorter "
                                f"than from Burruss ({wb})")


if __name__ == "__main__":
    unittest.main()