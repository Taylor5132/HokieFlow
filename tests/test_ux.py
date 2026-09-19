"""Regression tests for the student-facing request and response contract.

These are not cosmetic snapshots. Each test guards a failure a judge or student
could hit through the primary UI: ambiguous class times, negated transport,
lost route context, internal timestamps, and self-contradictory dining data.
All tests run against the frozen replay store.
"""
from __future__ import annotations

import unittest

from app import server
from hokieday import tools


class TestFreeTextInterpretation(unittest.TestCase):
    def test_headline_125_means_the_next_daytime_occurrence(self):
        call = server.parse_free_text("Can I eat and make my 1:25 class?")
        self.assertEqual(call["start"], "11:22")
        self.assertEqual(call["end"], "13:25")
        self.assertTrue(call["_interpretation_notes"])
        self.assertIn("PM", call["_interpretation_notes"][0])

    def test_two_time_window_rolls_125_forward(self):
        call = server.parse_free_text("I am free from 11:22 to 1:25")
        self.assertEqual((call["start"], call["end"]), ("11:22", "13:25"))

    def test_explicit_pm_is_preserved(self):
        call = server.parse_free_text("I need to arrive by 1:25 PM")
        self.assertEqual(call["end"], "13:25")
        self.assertEqual(call["_interpretation_notes"], [])

    def test_explicit_am_before_start_gets_a_helpful_correction(self):
        result = server.run_plan(server.parse_free_text("Make my 1:25 AM class"))
        self.assertEqual(result["error"], "end must be after start")
        self.assertIn("1:25 PM", result["rationale"])

    def test_no_bus_does_not_turn_the_bus_on(self):
        call = server.parse_free_text("No bus please; I only want to walk")
        self.assertEqual(call["prefs"]["prefer"], "walk")

    def test_free_text_extracts_diet_allergen_and_places(self):
        call = server.parse_free_text(
            "I’m vegan, can’t have sesame, and need to go from Burruss to Hahn")
        self.assertEqual(call["prefs"]["diet"], "vegan")
        self.assertIn("Sesame", call["prefs"]["avoid"])
        self.assertEqual(call["prefs"]["from_place"], "Burruss Hall")
        self.assertEqual(call["prefs"]["to_place"], "Hahn Hall")


class TestStudentFacingPlan(unittest.TestCase):
    def test_rationale_uses_human_times_not_iso_timestamps(self):
        result = tools.plan_day("demo-student-1", "11:22", "13:25")
        self.assertNotIn("2026-", result["rationale"])
        self.assertNotIn("T11:", result["rationale"])
        self.assertIn("11:22 AM", result["rationale"])

    def test_bus_leg_has_human_stop_names(self):
        result = tools.plan_day(
            "demo-student-1", "11:22", "13:25", {"prefer": "bus"})
        plan_a = next(a["itinerary"] for a in result["alternatives"]
                      if a.get("type") == "previous_itinerary_a")
        bus = next(l for l in plan_a["legs"] if l["type"] == "bus")
        self.assertTrue(bus["from_stop_name"])
        self.assertTrue(bus["to_stop_name"])
        self.assertNotEqual(bus["from_stop_name"], bus["from_stop"])
        self.assertNotEqual(bus["to_stop_name"], bus["to_stop"])

    def test_selected_meal_exposes_applied_constraints(self):
        result = tools.plan_day("demo-student-2", "11:22", "13:25")
        meal = next(l for l in result["itinerary"]["legs"] if l["type"] == "eat")
        self.assertEqual(meal["requested_diet"], "vegan")
        self.assertIn("vegan", meal["diet_tags"])
        self.assertIn("Sesame", meal["avoid"])
        self.assertIn("protein_g", meal)
        self.assertEqual(result["constraints"]["diet"], "vegan")

    def test_default_origin_is_named_not_called_default(self):
        result = server.run_plan(dict(server.SCENARIOS[0]))
        self.assertEqual(result["_origin"]["label"], "Burruss Hall")


if __name__ == "__main__":
    unittest.main()
