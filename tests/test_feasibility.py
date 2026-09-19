"""Feasibility-contract guards: a plan that misses must say so, and an unknown
place must never masquerade as a valid 0-minute itinerary.

These close the two planner correctness gaps recorded in
docs/FUNCTIONALITY_AND_DATA_MATRIX.md section 5.9 / 8.1 (B1/P1):

  1. a tight window still opened with "Yes — ... fits your time window" while
     the card read "35 min late";
  2. an unknown origin/destination produced an empty 0-minute itinerary whose
     rationale said "A route fits your time window".

Offline (DEMO_MODE=cache), pinned replay clock, no network.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app import server
from hokieday import tools

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "app" / "index.html"


class TestDeadlineFeasibility(unittest.TestCase):
    """Gap 1: a missed deadline must be top-level infeasible, with the miss."""

    def test_tight_window_is_infeasible_and_states_the_lateness(self):
        r = tools.plan_day("demo-student-1", "11:22", "11:23")
        self.assertIs(r["feasible"], False)
        reason = r["infeasible_reason"]
        self.assertEqual(reason["code"], "deadline_missed")
        self.assertGreater(reason["late_by_min"], 0)

        # late_by_min is DERIVED from the chosen plan's slack, not invented.
        it = r["itinerary"]
        self.assertIsNotNone(it, "a late best effort may stay for inspection")
        self.assertFalse(it["arrives_in_window"])
        self.assertEqual(reason["late_by_min"], round(-it["slack_min"], 1))

        # The rationale must state the miss and its size, never "fits"/"to spare".
        text = r["rationale"].lower()
        self.assertNotIn("fits", text)
        self.assertNotIn("to spare", text)
        self.assertIn("after the deadline", text)
        self.assertIn(str(int(round(reason["late_by_min"]))), r["rationale"])

    def test_normal_window_is_feasible_with_no_reason(self):
        r = tools.plan_day("demo-student-1", "11:22", "13:25")
        self.assertIs(r["feasible"], True)
        self.assertIsNone(r["infeasible_reason"])
        self.assertTrue(r["itinerary"]["arrives_in_window"])
        self.assertIn("fits your time window", r["rationale"])

    def test_feasibility_is_described_by_the_chosen_plan(self):
        """A re-plan can itself miss; top-level feasibility follows Plan B."""
        r = tools.plan_day("demo-student-1", "11:22", "12:05", {"prefer": "bus"})
        self.assertEqual(bool(r["feasible"]), bool(r["itinerary"]["arrives_in_window"]))


class TestUnknownPlaces(unittest.TestCase):
    """Gap 2: unknown places must not become valid empty itineraries."""

    def _assert_unknown(self, r, field, value):
        self.assertIs(r["feasible"], False)
        self.assertIsNone(r["itinerary"],
                          "an unknown place must not yield an empty itinerary")
        reason = r["infeasible_reason"]
        self.assertEqual(reason["code"], "unknown_place")
        self.assertEqual(reason["field"], field)
        self.assertEqual(reason["value"], value)
        self.assertTrue(reason["known_places"])
        self.assertNotIn("fits", r["rationale"].lower())
        self.assertIn(value, r["rationale"])

    def test_unknown_origin(self):
        r = tools.plan_day("demo-student-1", "11:22", "13:25",
                           {"from_place": "Narnia"})
        self._assert_unknown(r, "from_place", "Narnia")

    def test_unknown_destination(self):
        r = tools.plan_day("demo-student-1", "11:22", "13:25",
                           {"to_place": "Narnia"})
        self._assert_unknown(r, "to_place", "Narnia")

    def test_unknown_place_never_reports_a_zero_minute_plan(self):
        """The literal old failure: 0 legs, 0.0 min total, arrives_in_window True."""
        for prefs in ({"from_place": "Atlantis"}, {"to_place": "Atlantis"}):
            r = tools.plan_day("demo-student-1", "11:22", "13:25", prefs)
            it = r["itinerary"]
            self.assertFalse(
                bool(it) and it["total_min"] == 0 and it["arrives_in_window"],
                f"empty plan leaked for {prefs}")

    def test_both_unknown_places_are_reported(self):
        r = tools.plan_day("demo-student-1", "11:22", "13:25",
                           {"from_place": "Narnia", "to_place": "Atlantis"})
        reason = r["infeasible_reason"]
        self.assertEqual(reason["field"], "from_place")
        self.assertEqual({u["field"] for u in reason["unknown"]},
                         {"from_place", "to_place"})

    def test_unusable_pairs_return_no_itinerary(self):
        """Same place, no meal: zero legs must be infeasible, not a valid plan."""
        r = tools.plan_day("demo-student-1", "11:22", "13:25",
                           {"from_place": "McBryde Hall",
                            "to_place": "McBryde Hall", "eat": False})
        self.assertIs(r["feasible"], False)
        self.assertIsNone(r["itinerary"])
        self.assertEqual(r["infeasible_reason"]["code"], "no_legs")


class SlowBusSource(tools.LocalSource):
    """Deterministic Source whose bus ride is far slower than the walk.

    Lets us prove preference never overrides feasibility without depending on
    the exact minute the frozen fixtures happen to schedule.
    """

    def ride_minutes(self, stop_a, stop_b, after=None, route_id=None,
                     horizon_min=180):
        ride = super().ride_minutes(stop_a, stop_b, after=after,
                                    route_id=route_id, horizon_min=horizon_min)
        if ride is None:
            return None
        ride = dict(ride)
        ride["ride_min"] = round(ride["ride_min"] + 60.0, 1)
        ride["arrive_time"] = ride["dep_time"] + timedelta(minutes=ride["ride_min"])
        return ride


class TestCandidateSelection(unittest.TestCase):
    """Feasibility beats preference; when nothing fits, keep the least late."""

    def test_bus_preference_falls_back_to_a_walk_that_fits(self):
        # 43-minute window: the ~35.6-min walk fits; the +60-min bus does not.
        src = SlowBusSource()
        r = tools.plan_day("demo-student-1", "11:22", "12:05",
                           {"prefer": "bus"}, source=src)
        self.assertIs(r["feasible"], True)
        self.assertTrue(r["itinerary"]["arrives_in_window"])
        self.assertFalse(r["itinerary"]["used_bus"],
                         "a bus preference must not force an itinerary that misses")

    def test_least_late_useful_candidate_is_kept_when_none_fit(self):
        src = SlowBusSource()
        r = tools.plan_day("demo-student-1", "11:22", "11:23",
                           {"prefer": "bus"}, source=src)
        self.assertIs(r["feasible"], False)
        self.assertEqual(r["infeasible_reason"]["code"], "deadline_missed")
        it = r["itinerary"]
        self.assertIsNotNone(it)
        self.assertFalse(it["used_bus"],
                         "the least-late useful candidate here is the walk")


class TestServerPassThrough(unittest.TestCase):
    """The feasibility fields must survive run_plan and handle_ask unchanged."""

    def test_run_plan_carries_feasibility(self):
        ok = server.run_plan({"student_ref": "demo-student-1", "start": "11:22",
                              "end": "13:25", "prefs": {}})
        self.assertIs(ok["feasible"], True)
        self.assertIsNone(ok["infeasible_reason"])

        late = server.run_plan({"student_ref": "demo-student-1", "start": "11:22",
                                "end": "11:23", "prefs": {}})
        self.assertIs(late["feasible"], False)
        self.assertEqual(late["infeasible_reason"]["code"], "deadline_missed")
        self.assertGreater(late["infeasible_reason"]["late_by_min"], 0)

        unknown = server.run_plan({"student_ref": "demo-student-1",
                                   "start": "11:22", "end": "13:25",
                                   "prefs": {"to_place": "Narnia"}})
        self.assertIs(unknown["feasible"], False)
        self.assertIsNone(unknown["itinerary"])
        self.assertEqual(unknown["infeasible_reason"]["code"], "unknown_place")
        # A None itinerary must not break the map/link pass.
        self.assertEqual(unknown["_links"], [])
        self.assertEqual(unknown["_map_svg"], "")

    def test_handle_ask_carries_feasibility(self):
        res, code = server.handle_ask({"text": "from 11:22 to 11:23"})
        self.assertEqual(code, 200)
        self.assertIs(res["feasible"], False)
        self.assertEqual(res["infeasible_reason"]["code"], "deadline_missed")

        res, code = server.handle_ask({"scenario_id": "eat"})
        self.assertEqual(code, 200)
        self.assertIs(res["feasible"], True)


class TestFrontendScript(unittest.TestCase):
    """The inline UI must stay parseable and must handle the infeasible shape."""

    def test_inline_script_is_syntactically_valid(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        html = INDEX.read_text(encoding="utf-8")
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(scripts, "index.html must contain an inline script")
        for index, js in enumerate(scripts):
            with tempfile.NamedTemporaryFile("w", suffix=".js",
                                            delete=False) as fh:
                fh.write(js)
                path = fh.name
            try:
                proc = subprocess.run([node, "--check", path],
                                      capture_output=True, text=True)
            finally:
                Path(path).unlink(missing_ok=True)
            self.assertEqual(proc.returncode, 0,
                             f"inline script {index} failed node --check:\n"
                             f"{proc.stderr}")

    def test_render_handles_the_infeasible_shape(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("infeasibleCopy", html)
        self.assertIn("r.feasible === false", html)

    def test_render_runs_in_a_dom_without_throwing(self):
        """Drive the real inline render() with infeasible payloads under a stub
        DOM. A field the UI forgot to guard would throw here, not in the demo."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        html = INDEX.read_text(encoding="utf-8")
        js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
        payloads = [
            {
                "name": "unknown_place",
                "payload": {
                    "feasible": False,
                    "infeasible_reason": {
                        "code": "unknown_place", "field": "from_place",
                        "value": "Narnia",
                        "known_places": ["Burruss Hall", "McBryde Hall"],
                    },
                    "rationale": "I can't plan that: I don't know the "
                                 "starting place 'Narnia'.",
                    "itinerary": None, "alternatives": [],
                    "replan_trigger": None,
                },
                "contains": ["No plan fits", "Narnia", "Known places"],
                "notContains": ["fits your time window"],
            },
            {
                "name": "deadline_missed",
                "payload": {
                    "feasible": False,
                    "infeasible_reason": {"code": "deadline_missed",
                                          "late_by_min": 34.6},
                    "rationale": "No plan makes that deadline. 35 min after "
                                 "the deadline.",
                    "itinerary": {
                        "legs": [],
                        "leave_time": "2026-09-19T11:22:00-04:00",
                        "arrive_time": "2026-09-19T11:57:00-04:00",
                        "window_end": "2026-09-19T11:23:00-04:00",
                        "total_min": 35.6, "slack_min": -34.6,
                        "arrives_in_window": False, "used_bus": False,
                    },
                    "alternatives": [], "replan_trigger": None,
                    "_origin": {"source": "default", "label": "Burruss Hall"},
                    "_request": {"end": "11:23"},
                    "constraints": {"to_place": "McBryde Hall"},
                },
                "contains": ["No plan makes that deadline", "35 min late"],
                "notContains": ["fits your time window"],
            },
        ]
        harness = (
            "const fs=require('fs'),vm=require('vm');"
            "const els={};"
            "function makeEl(){return {innerHTML:'',className:'',textContent:'',"
            "title:'',value:'',options:[],dataset:{},style:{},disabled:false,"
            "classList:{toggle(){},add(){},remove(){}},setAttribute(){},"
            "addEventListener(){},focus(){},scrollIntoView(){}};}"
            "global.document={querySelector(s){return els[s]||(els[s]=makeEl());},"
            "querySelectorAll(){return [];},"
            "getElementById(id){return els['#'+id]||(els['#'+id]=makeEl());}};"
            "global.window={isSecureContext:false};global.navigator={};"
            "global.location={search:''};"
            "global.fetch=()=>Promise.reject(new Error('offline'));"
            "global.setInterval=()=>0;global.clearInterval=()=>{};"
            "global.setTimeout=()=>0;global.clearTimeout=()=>{};"
            "vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8')"
            "+'\\n;globalThis.__render=render;');"
            "const cases=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));"
            "for(const c of cases){globalThis.__render(c.payload);"
            "const h=els['#out'].innerHTML;"
            "for(const n of c.contains)if(!h.includes(n)){console.error("
            "'missing '+n+' in '+h);process.exit(2);}"
            "for(const b of c.notContains||[])if(h.includes(b)){console.error("
            "'unexpected '+b+' in '+h);process.exit(3);}}"
            "console.log('ok');process.exit(0);"
        )
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "inline.js"
            harness_path = Path(tmp) / "harness.js"
            cases_path = Path(tmp) / "cases.json"
            script.write_text(js, encoding="utf-8")
            harness_path.write_text(harness, encoding="utf-8")
            cases_path.write_text(json.dumps(payloads), encoding="utf-8")
            proc = subprocess.run(
                [node, str(harness_path), str(script), str(cases_path)],
                capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         f"infeasible render failed:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
