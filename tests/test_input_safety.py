"""Adversarial guards for request-parsing safety (P1).

Two failures are pinned here:

  A. Conjoined allergen constraints were dropped: "allergic to milk and eggs"
     captured only Milk. Every known allergen in a conjoined clause must now be
     collected, a bare unrelated "no bus" must not sweep in a later food word,
     and an allergy that matches no known allergen must produce a structured
     clarification instead of silently planning as if it did not exist.

  B. An explicit unknown destination/origin silently defaulted to McBryde/
     Burruss. `from Burruss to Narnia` and `{to_place: 'Narnia'}` must preserve
     the unknown place so `plan_day` reports `unknown_place`, while a valid
     device position keeps precedence and ordinary grammar is never made a
     place name.

Offline (DEMO_MODE=cache), pinned replay clock, no network.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

from app import server                                    # noqa: E402
from hokieday import tools                                # noqa: E402

ALL_ALLERGENS = ("Milk", "Eggs", "Fish", "Crustacean Shellfish",
                 "Tree Nuts", "Peanuts", "Wheat", "Soybeans", "Gluten",
                 "Sesame")


def _none_stated(items, stated) -> list[str]:
    """Return any item allergen that matches a stated avoid term."""
    offenders: list[str] = []
    for item in items:
        declared_all = (item.allergens if hasattr(item, "allergens")
                        else item.get("allergens"))
        name = getattr(item, "name", None) or item.get("name")
        for declared in declared_all or ():
            for target in stated:
                if target.lower() in declared.lower():
                    offenders.append(f"{declared!r} in {name!r}")
    return offenders


class TestAllergenLists(unittest.TestCase):
    def test_forward_conjoined_milk_and_eggs(self):
        call = server.parse_free_text(
            "I'm vegetarian and allergic to milk and eggs")
        self.assertEqual(call["prefs"]["diet"], "vegetarian")
        self.assertEqual(call["prefs"]["avoid"], ["Milk", "Eggs"])

    def test_oxford_list_of_three(self):
        call = server.parse_free_text("no peanuts, tree nuts, or sesame")
        self.assertEqual(call["prefs"]["avoid"],
                         ["Tree Nuts", "Peanuts", "Sesame"])

    def test_ampersand_and_slash_separators(self):
        call = server.parse_free_text("can't have milk, eggs & soy")
        self.assertEqual(call["prefs"]["avoid"], ["Milk", "Eggs", "Soybeans"])
        slash = server.parse_free_text("I avoid dairy/gluten")
        self.assertEqual(slash["prefs"]["avoid"], ["Milk", "Gluten"])

    def test_nuts_alias_expands_to_both_categories(self):
        call = server.parse_free_text("I'm allergic to nuts")
        self.assertEqual(call["prefs"]["avoid"], ["Tree Nuts", "Peanuts"])

    def test_deduplicated_in_official_taxonomy_order(self):
        call = server.parse_free_text(
            "allergic to sesame and eggs and milk and nuts")
        # Taxonomy order: Milk, Eggs, Tree Nuts, Peanuts, Sesame.
        self.assertEqual(call["prefs"]["avoid"],
                         ["Milk", "Eggs", "Tree Nuts", "Peanuts", "Sesame"])

    def test_reverse_phrasing_is_supported(self):
        call = server.parse_free_text("I have a milk and eggs allergy")
        self.assertEqual(call["prefs"]["avoid"], ["Milk", "Eggs"])

    def test_no_bus_does_not_capture_a_later_food_word(self):
        # The generic "no" cue must not reach across the clause into a positive
        # statement about eggs.
        call = server.parse_free_text("No bus, but I can have eggs")
        self.assertNotIn("avoid", call["prefs"])
        self.assertEqual(call["prefs"]["prefer"], "walk")

    def test_no_bus_alone_produces_no_allergy_clarification(self):
        call = server.parse_free_text("No bus please; I only want to walk")
        self.assertNotIn("avoid", call["prefs"])
        self.assertNotIn("_clarification", call)

    def test_unmatched_allergy_returns_structured_clarification(self):
        call = server.parse_free_text("I'm allergic to poultry")
        self.assertEqual(call["_clarification"]["kind"], "allergen_unparsed")
        self.assertTrue(call["_clarification"]["question"])

    def test_unmatched_allergy_clarification_survives_the_pipeline(self):
        result, code = server.handle_ask({"text": "I'm allergic to poultry"})
        self.assertEqual(code, 200)
        self.assertIs(result["feasible"], False)
        self.assertEqual(result["infeasible_reason"]["code"],
                         "clarification_needed")
        self.assertEqual(result["clarification"]["kind"],
                         "allergen_unparsed")
        self.assertIsNone(result["itinerary"])
        self.assertEqual(result["_map_svg"], "")
        self.assertEqual(result["_links"], [])

    def test_known_then_unknown_member_clarifies(self):
        # Milk parses but poultry does not: the whole clause must still be
        # treated as unmappable rather than planning around Milk alone.
        for text in ("I'm allergic to milk and poultry",
                     "I'm allergic to milk, eggs, and kiwi",
                     "can't have milk and mustard"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertEqual(call["_clarification"]["kind"],
                                 "allergen_unparsed")

    def test_multiword_member_residue_clarifies(self):
        # A known allergen prefix must not hide an unmappable modifier/member.
        for text in ("I'm allergic to dairy products and poultry",
                     "I'm allergic to milk powder and poultry",
                     "I'm allergic to tree nut products and mustard"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertEqual(call["_clarification"]["kind"],
                                 "allergen_unparsed")
                result, code = server.handle_ask({"text": text})
                self.assertEqual(code, 200)
                self.assertIsNone(result["itinerary"])

    def test_unknown_then_known_member_clarifies(self):
        # The unknown member comes first, so no known allergen is captured;
        # the explicit allergy mention must still force a clarification.
        for text in ("I'm allergic to poultry and milk",
                     "no poultry and milk"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertEqual(call["_clarification"]["kind"],
                                 "allergen_unparsed")

    def test_unknown_clause_is_not_masked_by_a_known_clause(self):
        # A recognised allergen in a second clause must not hide the first
        # clause's unmappable member.
        call = server.parse_free_text(
            "I'm allergic to poultry, and I can't have milk")
        self.assertEqual(call["_clarification"]["kind"],
                         "allergen_unparsed")

    def test_equivalent_separators_before_an_unknown_member_clarify(self):
        for text in ("allergic to milk/poultry",
                     "allergic to milk & poultry",
                     "allergic to milk or poultry",
                     "allergic to milk, poultry"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertEqual(call["_clarification"]["kind"],
                                 "allergen_unparsed")

    def test_known_only_list_does_not_clarify(self):
        call = server.parse_free_text("I'm allergic to milk, eggs, and sesame")
        self.assertNotIn("_clarification", call)
        self.assertEqual(call["prefs"]["avoid"], ["Milk", "Eggs", "Sesame"])

    def test_later_prose_is_not_mistaken_for_an_unknown_member(self):
        # "need lunch" / "the bus" are clause furniture, not ingredients.
        for text in ("I'm allergic to milk and I need lunch by 1:25 PM",
                     "allergic to eggs and want a quick bite",
                     "I avoid dairy and take the bus"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertNotIn("_clarification", call)
                self.assertIn("avoid", call["prefs"])

    def test_mixed_known_unknown_never_yields_a_plan(self):
        for text in ("I'm allergic to milk and poultry and need lunch by 1:25 PM",
                     "I'm allergic to milk, eggs, and kiwi"):
            with self.subTest(text=text):
                result, code = server.handle_ask({"text": text})
                self.assertEqual(code, 200)
                self.assertFalse(result.get("itinerary"))
                self.assertEqual(result["clarification"]["kind"],
                                 "allergen_unparsed")

    def test_transport_and_environment_phrases_do_not_clarify(self):
        # Reviewer examples: bare no/avoid/without is overwhelmingly transport
        # or weather language, never a dietary restriction on its own.
        for text in ("no parking",
                     "avoid traffic",
                     "No buses, walking only",
                     "No rain route",
                     "without the bus",
                     "no problem, I'll walk"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertNotIn("_clarification", call)
                self.assertNotIn("avoid", call["prefs"])

    def test_bare_avoid_with_known_anchor_and_unknown_member_clarifies(self):
        # "no milk and poultry": Milk is a recognised allergen, so the joined
        # "poultry" member is a dropped constraint and must be surfaced.
        for text in ("no milk and poultry",
                     "no milk and poultry and need lunch by 1:25 PM",
                     "no milk, eggs, and kiwi",
                     "avoid dairy and mustard"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertEqual(call["_clarification"]["kind"],
                                 "allergen_unparsed")

    def test_bare_avoid_unknown_first_then_known_clarifies(self):
        # Unknown-first: no known allergen is captured at the head, but the
        # separator joins a later recognised allergen, so this IS an allergen
        # list and the leading member must not be dropped.
        for text in ("no poultry and milk",
                     "no kiwi, eggs, and milk"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertEqual(call["_clarification"]["kind"],
                                 "allergen_unparsed")
                self.assertIn("Milk", call["prefs"]["avoid"])

    def test_transport_word_before_a_real_allergy_is_not_allergen_context(self):
        # The bare cue must not reach across the clause into a later explicit
        # restriction; the explicit phrase still captures its allergen.
        call = server.parse_free_text(
            "No parking, but I'm allergic to peanuts")
        self.assertNotIn("_clarification", call)
        self.assertEqual(call["prefs"]["avoid"], ["Peanuts"])

    def test_explicit_unknown_ingredient_still_clarifies(self):
        for text in ("I'm allergic to poultry",
                     "I can't have kiwi",
                     "allergic to dragonfruit"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertEqual(call["_clarification"]["kind"],
                                 "allergen_unparsed")


class TestAllergenHardFilterOutput(unittest.TestCase):
    """A stated list must never appear in the chosen plan or its menu."""

    STATEMENTS = (
        ("I'm vegetarian and allergic to milk and eggs", ("Milk", "Eggs")),
        ("no peanuts, tree nuts, or sesame",
         ("Peanuts", "Tree Nuts", "Sesame")),
        ("can't have milk, eggs & soy", ("Milk", "Eggs", "Soybeans")),
    )

    def test_plan_meal_contains_none_of_the_stated_allergens(self):
        for text, stated in self.STATEMENTS:
            with self.subTest(text=text):
                result, code = server.handle_ask({"text": text})
                self.assertEqual(code, 200)
                meal = next((leg for leg in (result["itinerary"] or {}).get(
                    "legs", []) if leg["type"] == "eat"), None)
                self.assertIsNotNone(meal, "expected an eat leg to inspect")
                self.assertEqual(sorted(meal["avoid"]), sorted(stated))
                offenders = _none_stated(
                    [type("I", (), {"allergens": meal["allergens"],
                                    "name": meal["item"]})()],
                    stated)
                self.assertEqual(offenders, [],
                                 f"stated allergen leaked into {meal['item']}")

    def test_menu_filter_keeps_none_of_each_stated_allergen(self):
        avoid = ("Milk", "Eggs", "Tree Nuts", "Peanuts", "Sesame",
                 "Soybeans", "Wheat", "Gluten", "Fish", "Crustacean Shellfish")
        items = tools.find_food(location_num="15", avoid=avoid)["items"]
        self.assertTrue(items, "expected some allergen-free items")
        self.assertEqual(_none_stated(items, avoid), [])

    def test_unknown_allergen_never_yields_a_plan(self):
        result, _ = server.handle_ask(
            {"text": "I'm allergic to poultry and need lunch by 1:25 PM"})
        self.assertFalse(result.get("itinerary"))


class TestUnknownPlaces(unittest.TestCase):
    def _assert_unknown(self, result, field, value):
        self.assertIs(result["feasible"], False)
        self.assertIsNone(result["itinerary"])
        reason = result["infeasible_reason"]
        self.assertEqual(reason["code"], "unknown_place")
        self.assertEqual(reason["field"], field)
        self.assertEqual(reason["value"], value)
        self.assertIn(value, result["rationale"])
        self.assertEqual(result["_map_svg"], "")
        self.assertEqual(result["_links"], [])

    def test_free_text_unknown_destination_is_not_silently_defaulted(self):
        result, code = server.handle_ask(
            {"text": "from Burruss to Narnia by 1:25 PM"})
        self.assertEqual(code, 200)
        self._assert_unknown(result, "to_place", "Narnia")
        self.assertEqual(result["_request"]["prefs"]["from_place"],
                         "Burruss Hall")

    def test_free_text_unknown_origin_is_not_silently_defaulted(self):
        result, _ = server.handle_ask(
            {"text": "from Narnia to Burruss by 1:25 PM"})
        self._assert_unknown(result, "from_place", "Narnia")

    def test_payload_unknown_destination(self):
        result, _ = server.handle_ask({"to_place": "Narnia"})
        self._assert_unknown(result, "to_place", "Narnia")

    def test_payload_unknown_origin(self):
        result, _ = server.handle_ask({"from_place": "Narnia"})
        self._assert_unknown(result, "from_place", "Narnia")

    def test_known_places_are_unaffected(self):
        result, code = server.handle_ask(
            {"text": "from Burruss to McBryde by 1:25 PM"})
        self.assertEqual(code, 200)
        self.assertIs(result["feasible"], True)
        prefs = result["_request"]["prefs"]
        self.assertEqual(prefs["from_place"], "Burruss Hall")
        self.assertEqual(prefs["to_place"], "McBryde Hall")

    def test_known_place_payload_control(self):
        result, _ = server.handle_ask({"to_place": "Hahn Hall"})
        self.assertIs(result["feasible"], True)
        self.assertEqual(result["_request"]["prefs"]["to_place"], "Hahn Hall")

    def test_device_position_keeps_precedence_over_explicit_origin(self):
        result, _ = server.handle_ask({
            "from_place": "Narnia",
            "lat": 37.22957, "lon": -80.41394,
            "to_place": "McBryde Hall",
        })
        self.assertIs(result["feasible"], True)
        origin = result["_request"]["prefs"]["from_place"]
        self.assertTrue(origin.startswith("your location"),
                        f"device position lost precedence: {origin!r}")

    def test_ordinary_grammar_is_not_turned_into_a_place(self):
        for text in ("I want to know the bus schedule",
                     "I need to eat at 12",
                     "I have from 11:22 to 13:25"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertNotIn("to_place", call["prefs"])
                self.assertNotIn("from_place", call["prefs"])

    def test_payload_auto_and_default_are_not_unknown_places(self):
        for value in ("auto", "default", ""):
            with self.subTest(value=value):
                result, _ = server.handle_ask({"to_place": value})
                self.assertIs(result["feasible"], True)

    def test_lowercase_unknown_destination_in_route_phrase(self):
        result, code = server.handle_ask(
            {"text": "from burruss to narnia by 1:25 PM"})
        self.assertEqual(code, 200)
        self._assert_unknown(result, "to_place", "narnia")
        self.assertEqual(result["_request"]["prefs"]["from_place"],
                         "Burruss Hall")

    def test_lowercase_unknown_origin_in_route_phrase(self):
        result, _ = server.handle_ask(
            {"text": "from narnia to Burruss by 1:25 PM"})
        self._assert_unknown(result, "from_place", "narnia")

    def test_lowercase_multiword_unknown_in_route_phrase(self):
        result, _ = server.handle_ask(
            {"text": "from burruss to duck pond by 1:25 PM"})
        self._assert_unknown(result, "to_place", "duck pond")

    def test_lowercase_route_grammar_and_deictics_are_not_places(self):
        # Adversarial controls: a from/to pair alone must not promote ordinary
        # grammar or location deictics into place names.
        for text in ("I want to know the bus schedule",
                     "I need to eat at 12",
                     "I have from 11:22 to 13:25",
                     "from here to there by 1:25 PM",
                     "from somewhere to anywhere",
                     "from home to there by noon"):
            with self.subTest(text=text):
                call = server.parse_free_text(text)
                self.assertNotIn("from_place", call["prefs"])
                self.assertNotIn("to_place", call["prefs"])


if __name__ == "__main__":
    unittest.main()