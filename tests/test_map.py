"""Map rendering and the keyless navigation handoff.

OWNER: integrator. Two things worth guarding here:

  * The map is drawn from OUR GTFS shape geometry and inlined, so it renders with
    networking off. If it silently stopped drawing the bus route, the demo would
    lose its most convincing visual and nothing else would fail.
  * The walk lines are STRAIGHT and the caption must keep saying so. Our walk
    model is haversine x 1.30, not a routed path; drawing a path would be a lie
    the geometry cannot support.
  * The deep links carry the same origin/destination the plan used. If they used
    the wrong origin they would send a judge somewhere else entirely.

Offline. DEMO_MODE=cache only.
"""
from __future__ import annotations

import unittest
import xml.dom.minidom

from hokieday import config, tools

from app import mapview, server


def _plan(prefs=None):
    return tools.plan_day("demo-student-1", "11:22", "13:00", prefs or {})


def _plan_a(result):
    return next((a.get("itinerary") for a in (result.get("alternatives") or [])
                 if a.get("type") == "previous_itinerary_a"), None)


class TestMapSvg(unittest.TestCase):
    def test_empty_input_yields_empty_string(self):
        for bad in (None, {}, {"legs": []}):
            self.assertEqual(mapview.build_map_svg(bad), "")

    def test_walk_plan_is_wellformed_and_has_no_bus_route(self):
        itin = _plan()["itinerary"]
        svg = mapview.build_map_svg(itin)
        xml.dom.minidom.parseString(svg)              # must not raise
        self.assertNotIn("<polyline", svg, "a walk-only plan has no bus geometry")
        self.assertGreater(svg.count("stroke-dasharray"), 0, "walk legs must be drawn")

    def test_bus_plan_draws_the_gtfs_shape(self):
        """The orange line must be the REAL shape, not a straight chord."""
        plan_a = _plan_a(_plan({"prefer": "bus"}))
        self.assertIsNotNone(plan_a, "fixture should produce a bus plan")
        svg = mapview.build_map_svg(plan_a)
        xml.dom.minidom.parseString(svg)
        self.assertIn("<polyline", svg)

        verts = [len(p.split(" ")) for p in
                 __import__("re").findall(r'<polyline points="([^"]+)"', svg)]
        self.assertTrue(verts, "expected at least one polyline")
        self.assertGreater(max(verts), 10,
                           "a real road shape has many vertices; a chord would have 2")

    def test_overlay_draws_the_abandoned_route(self):
        """A re-plan must be legible in ONE image: the abandoned bus route sits
        under the chosen plan, and the bbox must still cover it."""
        result = _plan({"prefer": "bus"})
        plan_a = _plan_a(result)
        chosen = result["itinerary"]

        without = mapview.build_map_svg(chosen)
        with_overlay = mapview.build_map_svg(chosen, overlay=plan_a)
        xml.dom.minidom.parseString(with_overlay)

        self.assertNotIn("abandoned", without)
        self.assertIn("abandoned", with_overlay,
                      "the legend must say the grey route was abandoned")
        self.assertGreater(len(with_overlay), len(without),
                           "the overlay should add geometry")
        self.assertGreater(with_overlay.count("<polyline"),
                           without.count("<polyline"))

    def test_caption_states_the_walk_lines_are_estimates(self):
        """Honesty invariant: our walk model is straight-line, so the image must
        not present the dashed lines as routed paths."""
        svg = mapview.build_map_svg(_plan()["itinerary"])
        self.assertIn("straight-line estimate", svg)

    def test_scale_bar_is_drawn(self):
        svg = mapview.build_map_svg(_plan()["itinerary"])
        self.assertTrue((" km<" in svg) or (" m<" in svg),
                        "a scale bar makes the drawing interpretable")

    def test_dots_are_clipped_to_the_viewbox(self):
        """Cosmetic, but it keeps the element count honest: stops inside the padded
        lat/lon window can project outside the fitted area."""
        svg = mapview.build_map_svg(_plan()["itinerary"])
        for cx, cy in __import__("re").findall(
                r'<circle cx="([-\d.]+)" cy="([-\d.]+)" r="2"', svg):
            self.assertTrue(0 <= float(cx) <= mapview.W, f"x out of box: {cx}")
            self.assertTrue(0 <= float(cy) <= mapview.H, f"y out of box: {cy}")

    def test_map_contains_no_external_references(self):
        """It must render offline: no raster/base imagery, no script, no fetched
        assets. The xmlns declaration is a namespace URI, not a fetch, so it is
        stripped before checking."""
        svg = mapview.build_map_svg(_plan()["itinerary"])
        body = svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
        for forbidden in ("<image", "<script", "<use", "xlink:href",
                          "http://", "https://"):
            self.assertNotIn(forbidden, body, f"{forbidden!r} would need the network")


class TestDeepLinks(unittest.TestCase):
    """Keyless handoff: verified that Google Maps URLs need no API key, and Apple
    map links need none either (dirflg w=foot, r=transit)."""

    def _links(self, prefs=None):
        return server.deep_links(_plan(prefs))

    def test_four_links_for_both_apps_and_modes(self):
        links = self._links()
        self.assertEqual(len(links), 4)
        urls = " ".join(l["url"] for l in links)
        self.assertIn("maps.apple.com", urls)
        self.assertIn("google.com/maps", urls)
        self.assertIn("dirflg=w", urls)          # Apple: by foot
        self.assertIn("dirflg=r", urls)          # Apple: public transit
        self.assertIn("travelmode=walking", urls)
        self.assertIn("travelmode=transit", urls)

    def test_google_urls_keep_the_required_api_parameter(self):
        for link in self._links():
            if "google.com" in link["url"]:
                self.assertIn("api=1", link["url"],
                              "Google ignores all parameters without api=1")

    def test_commas_are_percent_encoded(self):
        for link in self._links():
            self.assertIn("%2C", link["url"], "coordinate commas must be encoded")
            self.assertNotIn(",", link["url"].split("?")[1].split("&")[0])

    def test_links_use_the_plans_own_origin_and_destination(self):
        """The handoff must match the plan, or it sends the user elsewhere."""
        result = _plan()
        legs = result["itinerary"]["legs"]
        origin = next(l["from_coords"] for l in legs if l.get("from_coords"))
        dest = next(l["to_coords"] for l in reversed(legs) if l.get("to_coords"))
        url = server.deep_links(result)[0]["url"]
        self.assertIn(f"{origin[0]:.6f}", url)
        self.assertIn(f"{origin[1]:.6f}".replace("-", "-"), url)
        self.assertIn(f"{dest[0]:.6f}", url)

    def test_device_origin_flows_into_the_links(self):
        key = config.register_dynamic_place(37.22990, -80.41420, "your location", 9.0)
        result = tools.plan_day("demo-student-1", "11:22", "13:00",
                                {"from_place": key})
        url = server.deep_links(result)[0]["url"]
        self.assertIn("37.229900", url,
                      "the handoff must start where the student actually is")

    def test_no_links_when_there_is_nothing_to_route(self):
        self.assertEqual(server.deep_links({}), [])
        self.assertEqual(server.deep_links({"itinerary": None}), [])
        self.assertEqual(server.deep_links({"itinerary": {"legs": []}}), [])


class TestRunPlanIncludesMapAndLinks(unittest.TestCase):
    def test_response_carries_map_and_links(self):
        result = server.run_plan({"student_ref": "demo-student-1",
                                  "start": "11:22", "end": "13:00", "prefs": {}})
        self.assertGreater(len(result["_map_svg"]), 500)
        xml.dom.minidom.parseString(result["_map_svg"])
        self.assertEqual(len(result["_links"]), 4)

    def test_a_map_failure_does_not_break_the_plan(self):
        """The plan is the product; the map is a view. Never let one take out the
        other -- the demo would lose everything, not just the picture."""
        import unittest.mock as mock
        with mock.patch.object(mapview, "build_map_svg",
                               side_effect=RuntimeError("boom")):
            result = server.run_plan({"student_ref": "demo-student-1",
                                      "start": "11:22", "end": "13:00", "prefs": {}})
        self.assertEqual(result["_map_svg"], "")
        self.assertIn("boom", result["_map_error"])
        self.assertIsNotNone(result["itinerary"], "the plan must survive")


if __name__ == "__main__":
    unittest.main()