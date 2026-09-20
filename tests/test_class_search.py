"""Course search: the typed-query parser, the snapshot path, and the route.

The feature exists because searching the catalog was the one campus question the
app could not answer: `hokieday/classes.py` could parse a Banner snapshot and
filter it, but nothing served it, so a student could only add classes by hand.
These tests pin the three pieces that make it reachable:

* `classes.query_filters` - what a student types ("CS 3114", "83568",
  "data structures") turned into filters, never silently guessed.
* `app.class_search.search` - answers from the committed captures offline, and
  must not touch the network while replaying.
* `GET /api/classes/search` - the contract the design UI reads.

Run:  DEMO_MODE=cache python3 -m unittest tests.test_class_search -v
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

from app import server  # noqa: E402
from hokieday import classes  # noqa: E402


class QueryFilterTests(unittest.TestCase):
    """What people type, mapped without guessing."""

    def test_course_numbers_and_crns(self):
        self.assertEqual(classes.query_filters("CS 3114"),
                         {"subject": "CS", "course_number": "3114"})
        self.assertEqual(classes.query_filters("cs3114"),
                         {"subject": "CS", "course_number": "3114"})
        self.assertEqual(classes.query_filters("CS-3114"),
                         {"subject": "CS", "course_number": "3114"})
        self.assertEqual(classes.query_filters("MATH 1225"),
                         {"subject": "MATH", "course_number": "1225"})
        # VT CRNs are five digits; a four-digit number is a course number.
        self.assertEqual(classes.query_filters("83568"), {"crn": "83568"})
        self.assertEqual(classes.query_filters("3114"), {"course_number": "3114"})

    def test_subjects_titles_and_extra_words(self):
        self.assertEqual(classes.query_filters("CS"), {"subject": "CS"})
        self.assertEqual(classes.query_filters("MATH"), {"subject": "MATH"})
        self.assertEqual(classes.query_filters("AFROTC"), {"subject": "AFROTC"})
        self.assertEqual(classes.query_filters("data structures"),
                         {"title_contains": "data structures"})
        self.assertEqual(classes.query_filters("CS 3114 data structures"),
                         {"subject": "CS", "course_number": "3114",
                          "title_contains": "data structures"})

    def test_unrecognised_input_becomes_a_title_search(self):
        self.assertEqual(classes.query_filters("intro to cs"),
                         {"title_contains": "intro to cs"})
        self.assertEqual(classes.query_filters("   "), {})
        self.assertEqual(classes.query_filters(None), {})


class SnapshotSearchTests(unittest.TestCase):
    """Offline answers come from the committed captures, with provenance."""

    def _run(self, text):
        from app import class_search
        return class_search.search(text)

    def test_captures_answer_common_queries(self):
        from app import class_search
        self.assertTrue(class_search.snapshot_paths(),
                        "a capture must ship or search cannot work offline")
        result = self._run("CS 3114")
        self.assertEqual(result["source"], "snapshot",
                         "replay must never claim a live answer")
        self.assertFalse(result["live"])
        self.assertEqual(result["state"], "results")
        self.assertTrue(result["sections"])
        section = result["sections"][0]
        self.assertEqual(section["subject"], "CS")
        self.assertEqual(section["course_number"], "3114")
        self.assertTrue(section["meetings"], "a section the UI can place")
        self.assertTrue(section["meetings"][0]["begin"])
        self.assertTrue(section["meetings"][0]["building"])

    def test_searches_the_whole_captured_term_not_just_the_newest_file(self):
        """"MATH 1225" must not be filtered against a CS-only capture."""
        result = self._run("MATH 1225")
        self.assertEqual(result["state"], "results", "MATH is captured separately")
        self.assertTrue(all(s["subject"] == "MATH" for s in result["sections"]))
        self.assertGreater(len(result["snapshot_ids"]), 1,
                           "every capture of the term is searched")

    def test_title_search_and_a_crn_lookup(self):
        titled = self._run("data structures")
        self.assertEqual(titled["state"], "results")
        self.assertTrue(any("data structures" in s["title"].lower()
                            for s in titled["sections"]))
        by_crn = self._run("83568")
        self.assertEqual([s["crn"] for s in by_crn["sections"]], ["83568"])

    def test_no_match_is_no_results_not_an_error(self):
        result = self._run("CS 9999")
        self.assertEqual(result["state"], "no_results")
        self.assertEqual(result["sections"], [])

    def test_the_snapshot_is_labelled_with_its_capture_time(self):
        result = self._run("CS 3114")
        snapshot = result["snapshot"]
        self.assertTrue(snapshot["fetched_at"], "never present a capture as live")
        self.assertIn("is_stale", snapshot)
        self.assertIn("age_seconds", snapshot)
        self.assertEqual(result["term"], snapshot["term"])

    def test_replay_never_reaches_the_network(self):
        """The whole point of the snapshot fallback."""
        from app import class_import, class_search
        calls = []

        def explode(*_args, **_kwargs):
            calls.append(1)
            raise AssertionError("replay must not call Banner")

        original = class_search.fetch_timetable_html
        class_search.fetch_timetable_html = explode
        try:
            class_search.search("CS 3114")
            class_search.search("data structures")
            # The Schedule tab's own endpoint must honour replay too: it asks the
            # network boundary directly, and it once leaked a live call here.
            class_import.search_endpoint({"term": "202609", "subject": "ECE",
                                          "course_number": "2564"})
        finally:
            class_search.fetch_timetable_html = original
        self.assertEqual(calls, [], "no live fetch while replaying")

    def test_a_live_capture_never_lands_in_the_replay_store(self):
        """In replay CACHE_DIR IS fixtures/, so an unguarded live write would
        overwrite the frozen snapshot and stamp it with the replay clock."""
        from app import class_search
        before = {p.name: p.stat().st_mtime for p in class_search.snapshot_paths()}
        self.assertTrue(class_search._is_inside_fixtures(
            class_search._cache_path("anything")))
        written = class_search._snapshot_from_cache(
            "202609", {"subject": "CS"}, "<html></html>",
            "2026-09-20T12:00:00+00:00")
        self.assertIsNone(written, "the write must be refused")
        after = {p.name: p.stat().st_mtime for p in class_search.snapshot_paths()}
        self.assertEqual(before, after, "the replay store is byte-unchanged")

    def test_a_term_we_never_captured_reports_unavailable(self):
        from app import class_search
        result = class_search.search("CS 3114", term="203001")
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["source"], "unavailable")
        self.assertTrue(result["reason"], "say WHY there is no answer")


class ClassSearchRouteTests(unittest.TestCase):
    """GET /api/classes/search is what the Schedule tab calls."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        import json as _json
        cls.json = _json
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
        conn.request("GET", path, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        body = self.json.loads(response.read())
        conn.close()
        return response.status, body

    def test_the_route_answers_with_the_documented_envelope(self):
        status, body = self._get("/api/classes/search?q=CS%203114")
        self.assertEqual(status, 200)
        for key in ("schema", "term", "term_name", "query", "snapshot", "state",
                    "count", "sections", "source", "live"):
            self.assertIn(key, body)
        self.assertEqual(body["state"], "results")
        self.assertTrue(body["sections"])

    def test_the_response_never_contains_student_identity(self):
        status, body = self._get("/api/classes/search?q=CS%203114")
        text = self.json.dumps(body).lower()
        for forbidden in ("pid", "student_ref", "cookie", "token", "grade"):
            self.assertNotIn(forbidden, text)

    def test_limit_is_clamped_and_truncation_is_reported(self):
        status, body = self._get("/api/classes/search?subject=CS&limit=3")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["sections"]), 3)
        self.assertTrue(body["truncated"], "the UI must know it is seeing a page")

    def test_unknown_input_is_an_empty_result_not_a_crash(self):
        status, body = self._get("/api/classes/search?q=ZZZZ%209999")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "no_results")
        self.assertEqual(body["sections"], [])


class CourseCatalogToolTests(unittest.TestCase):
    """search_classes is how the agent answers catalog questions."""

    def _call(self, query, **extra):
        from hokieday import agent_tools, config
        context = agent_tools.AgentContext(
            now=config.now(), schedule=[], origin_place=None,
            required_prefs={}, student_ref=None, is_replay=True)
        execution = agent_tools.ToolRegistry().dispatch(
            "search_classes", {"query": query, **extra}, context)
        return execution

    def test_it_is_registered_with_a_bounded_query_argument(self):
        from hokieday import agent_tools
        spec = {s.name: s for s in agent_tools.TOOL_SPECS}["search_classes"]
        self.assertEqual(spec.parameters["properties"]["query"]["maxLength"], 60)
        self.assertEqual(spec.parameters["required"], ["query"])
        self.assertFalse(spec.parameters["additionalProperties"])

    def test_a_course_query_returns_placeable_sections(self):
        execution = self._call("CS 3114")
        self.assertTrue(execution.public["ok"])
        data = execution.public["data"]
        self.assertEqual(data["state"], "results")
        self.assertTrue(data["sections"])
        for row in data["sections"]:
            self.assertTrue(row["course"].startswith("CS "))
            self.assertTrue(row["crn"])
            self.assertTrue(row["days"] and row["start"] and row["end"],
                            "a section with no time cannot be planned around")

    def test_results_are_capped_and_report_truncation(self):
        execution = self._call("CS")
        data = execution.public["data"]
        self.assertLessEqual(len(data["sections"]), 5, "never flood the prompt")
        self.assertTrue(data["truncated"])

    def test_it_always_says_the_data_is_a_capture(self):
        data = self._call("CS 3114").public["data"]
        self.assertEqual(data["source"], "banner_public_snapshot")
        self.assertTrue(data["catalog"]["captured_at"], "never imply live registration")
        self.assertIn("not a live registration system", data["catalog"]["note"])

    def test_no_match_is_honest_and_never_invents_sections(self):
        data = self._call("ZZZZ 9999").public["data"]
        self.assertEqual(data["state"], "no_results")
        self.assertEqual(data["sections"], [])

    def test_the_payload_carries_no_student_identity(self):
        import json as _json
        data = self._call("CS 3114").public["data"]
        text = _json.dumps(data).lower()
        for forbidden in ("student_ref", "pid", "cookie", "token"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()