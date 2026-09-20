"""Dining menus: every food in a hall, grouped by meal, with nutrition detail.

The Dining tab listed halls but never their food, and the nutrition join existed
only inside the planner. These tests pin the contract the sheet reads, and the
two honesty rules that matter more than the feature: a blank allergen field is
UNKNOWN (never "contains none"), and replay nutrition that does not match the
clock is shown as unknown rather than as a stale number.

Run:  DEMO_MODE=cache python3 -m unittest tests.test_dining_menu -v
"""
from __future__ import annotations

import json
import os
import unittest

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

from app import dining_menu  # noqa: E402
from hokieday import dining  # noqa: E402


class DiningMenuContract(unittest.TestCase):
    """What the sheet renders: schema, meals, counts, provenance."""

    @classmethod
    def setUpClass(cls):
        cls.payload = dining_menu.menu_payload("D2")

    def test_a_hall_reports_its_foods_grouped_by_meal(self):
        payload = self.payload
        self.assertEqual(payload["schema"], "hokieday.dining.menu/1")
        self.assertEqual(payload["state"], "ok")
        self.assertEqual(payload["location"]["num"], "15")
        self.assertIn("D2", payload["location"]["name"])
        self.assertTrue(payload["meals"], "a hall with a published menu has meals")
        for meal in payload["meals"]:
            self.assertTrue(meal["meal"])
            self.assertTrue(meal["sections"], "each meal groups its sections")
            self.assertEqual(meal["count"],
                             sum(len(s["items"]) for s in meal["sections"]))

    def test_meals_come_in_the_order_a_student_eats_them(self):
        order = [m["meal"] for m in self.payload["meals"]]
        self.assertEqual(order, ["Breakfast", "Lunch", "Dinner"])

    def test_counts_and_provenance_are_present(self):
        payload = self.payload
        self.assertEqual(payload["count"],
                         sum(m["count"] for m in payload["meals"]))
        self.assertTrue(payload["fetched_at"], "a menu always says when it was read")
        self.assertTrue(payload["source"])
        self.assertIn("stale", payload)
        self.assertEqual(payload["date"], "2026-09-19")

    def test_the_response_is_bounded(self):
        payload = self.payload
        self.assertLessEqual(payload["count"], dining_menu.MAX_ITEMS)
        self.assertIn("truncated", payload)

    def test_the_payload_never_carries_student_identity(self):
        """Assert the field contract, not substrings: a section really is called
        "la patisserie cookies and tarts", and a text search would flag it."""
        payload = self.payload
        allowed_item_keys = {"name", "description", "portion", "section", "meal",
                             "diet_tags", "allergens", "allergens_known",
                             "venue_allergen_free", "recipe_id", "nutrition_state",
                             "nutrition"}
        allowed_top_keys = {"schema", "state", "location", "date", "meal_filter",
                            "open_now", "hours", "meals", "meals_available", "count",
                            "truncated", "nutrition_state", "source", "fetched_at",
                            "stale", "reason", "assumptions", "now"}
        self.assertEqual(set(payload), allowed_top_keys)
        self.assertEqual(set(payload["location"]), {"num", "name"})
        for meal in payload["meals"]:
            self.assertEqual(set(meal), {"meal", "count", "sections"})
            for section in meal["sections"]:
                self.assertEqual(set(section), {"section", "items"})
                for item in section["items"]:
                    self.assertEqual(set(item), allowed_item_keys, item["name"])
        # A student identity would arrive as a new key, not inside a food name.
        for identity in ("student_ref", "pid", "personal", "authorization", "cookie"):
            for meal in payload["meals"]:
                for section in meal["sections"]:
                    for item in section["items"]:
                        self.assertNotIn(identity, {k.lower() for k in item})


class DiningMenuFilterTests(unittest.TestCase):
    def test_a_meal_filter_narrows_the_response(self):
        payload = dining_menu.menu_payload("D2", meal="Lunch")
        self.assertEqual([m["meal"] for m in payload["meals"]], ["Lunch"])
        full = dining_menu.menu_payload("D2")
        lunch = next(m for m in full["meals"] if m["meal"] == "Lunch")
        self.assertEqual(payload["count"], lunch["count"])
        self.assertLess(payload["count"], full["count"])

    def test_a_student_facing_name_resolves_like_the_agent_tool(self):
        for name in ("D2", "d2", "D2 at Dietrick Hall"):
            self.assertEqual(dining_menu.menu_payload(name)["location"]["num"], "15",
                             name)

    def test_an_unknown_hall_is_typed_and_never_answered_by_another_hall(self):
        payload = dining_menu.menu_payload("Narnia Cafe")
        self.assertEqual(payload["state"], "unknown_location")
        self.assertIsNone(payload["location"])
        self.assertEqual(payload["meals"], [])
        self.assertTrue(payload["known_locations"])
        self.assertIn("Narnia Cafe", payload["reason"])

    def test_no_hall_asks_rather_than_guessing_one(self):
        payload = dining_menu.menu_payload("")
        self.assertEqual(payload["state"], "unknown_location")
        self.assertTrue(payload["reason"])
        self.assertEqual(payload["meals"], [])

    def test_a_hall_with_no_capture_reports_unavailable_offline(self):
        payload = dining_menu.menu_payload("Owens Food Court")
        self.assertIn(payload["state"], ("unavailable", "empty", "closed"))
        self.assertEqual(payload["meals"], [])
        self.assertEqual(payload["count"], 0)


class DiningMenuHonestyTests(unittest.TestCase):
    def test_a_blank_allergen_field_stays_unknown(self):
        items = [i for m in dining_menu.menu_payload("D2")["meals"]
                 for s in m["sections"] for i in s["items"]]
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item["allergens_known"], bool(item["allergens"]),
                             f"{item['name']}: blank must not read as allergen-free")
            if not item["allergens"] and not item["venue_allergen_free"]:
                self.assertFalse(item["allergens_known"])

    def test_replay_nutrition_is_unknown_rather_than_a_stale_number(self):
        """The D2 nutrition fixture postdates the pinned clock, so it is refused."""
        payload = dining_menu.menu_payload("D2")
        self.assertEqual(payload["nutrition_state"], "unknown")
        for meal in payload["meals"]:
            for section in meal["sections"]:
                for item in section["items"]:
                    self.assertIsNone(item["nutrition"])
                    self.assertEqual(item["nutrition_state"], "unknown")

    def test_live_nutrition_reaches_the_items(self):
        """With a nutrition response, each item carries its own numbers."""
        fake = {"212005": dining.Nutrients(cals=300.0, protein_g=10.0, fat_g=1.5,
                                           carb_g=57.0, sodium_mg=620.0)}
        original = dining_menu.dining.nutrition_for_location
        dining_menu.dining.nutrition_for_location = lambda *a, **k: fake
        try:
            payload = dining_menu.menu_payload("D2", meal="Breakfast")
        finally:
            dining_menu.dining.nutrition_for_location = original
        self.assertEqual(payload["nutrition_state"], "ok")
        bagels = [i for m in payload["meals"] for s in m["sections"]
                  for i in s["items"] if i["recipe_id"] == "212005"]
        self.assertTrue(bagels, "the fixture recipe is on D2's breakfast menu")
        self.assertEqual(bagels[0]["nutrition"]["kcal"], 300.0)
        self.assertEqual(bagels[0]["nutrition"]["protein_g"], 10.0)

    def test_a_nutrition_outage_still_shows_the_menu(self):
        def explode(*_args, **_kwargs):
            raise dining.MenuError("nutrition upstream down")

        original = dining_menu.dining.nutrition_for_location
        dining_menu.dining.nutrition_for_location = explode
        try:
            payload = dining_menu.menu_payload("D2", meal="Lunch")
        finally:
            dining_menu.dining.nutrition_for_location = original
        self.assertEqual(payload["state"], "ok", "a menu is not an outage")
        self.assertTrue(payload["count"])
        self.assertEqual(payload["nutrition_state"], "unavailable")

    def test_responses_state_their_allergen_and_nutrition_rules(self):
        payload = dining_menu.menu_payload("D2")
        self.assertIn("UNKNOWN", payload["assumptions"]["allergens"])
        self.assertTrue(payload["assumptions"]["nutrition"])


class DiningMenuRouteTests(unittest.TestCase):
    """GET /api/dining/menu is what the Dining tab calls."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        from app import server
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("GET", path)
        response = conn.getresponse()
        body = json.loads(response.read())
        conn.close()
        return response.status, body

    def test_the_route_answers_a_hall_by_name(self):
        status, body = self._get("/api/dining/menu?location=D2&meal=Lunch")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "ok")
        self.assertEqual(body["location"]["num"], "15")
        self.assertTrue(body["meals"])

    def test_the_route_answers_an_unknown_hall_without_guessing(self):
        status, body = self._get("/api/dining/menu?location=Nope")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "unknown_location")
        self.assertEqual(body["meals"], [])


if __name__ == "__main__":
    unittest.main()