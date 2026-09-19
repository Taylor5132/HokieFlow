"""Offline tests for hokieday.vtgis (VT Enterprise GIS client).

The suite is offline: cache mode is exercised against a temp cache dir, and live
mode against a fake ``cache._http`` / ``cache._http_post``. No socket opens and
NO real VT GIS payload is committed -- every fixture below is synthetic and
shaped to match the verified ArcGIS service schemas.

Run:  DEMO_MODE=cache python3 -m unittest tests.test_vtgis -v
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hokieday import cache, config, vtgis

_CAMPUS = ZoneInfo(config.CAMPUS_TZ)


# --------------------------------------------------------------------------- #
# Synthetic payloads (shaped from the service metadata, not captured data)
# --------------------------------------------------------------------------- #
BURRUSS = (37.22924778, -80.42396247)
MCBRYDE = (37.23059151, -80.42177674)

BUILDINGS_PAYLOAD = {
    "objectIdFieldName": "objectid",
    "geometryType": "esriGeometryPolygon",
    "spatialReference": {"wkid": 4326, "latestWkid": 4326},
    "features": [
        {
            "attributes": {
                "objectid": 1, "name": "Burruss Hall", "bldg_num": "0001",
                "vtes_bldg_num": "000001", "bldg_use": "Academic",
                "status": "Existing Conditions",
                "latitude": BURRUSS[0], "longitude": BURRUSS[1],
                "x_coord": BURRUSS[1], "y_coord": BURRUSS[0],
                "url": "https://vt.edu/burruss",
            },
            "geometry": {
                "rings": [[
                    [-80.4241, 37.2290], [-80.4238, 37.2290],
                    [-80.4238, 37.2294], [-80.4241, 37.2294],
                    [-80.4241, 37.2290],
                ]],
                "spatialReference": {"wkid": 4326},
            },
        },
        {
            "attributes": {
                "objectid": 2, "name": "McBryde Hall", "bldg_num": "0011",
                "vtes_bldg_num": "000011", "bldg_use": "Academic",
                "status": "Existing Conditions",
                "latitude": MCBRYDE[0], "longitude": MCBRYDE[1],
            },
            "geometry": {
                "rings": [[
                    [-80.4219, 37.2304], [-80.4216, 37.2304],
                    [-80.4216, 37.2308], [-80.4219, 37.2308],
                    [-80.4219, 37.2304],
                ]],
                "spatialReference": {"wkid": 4326},
            },
        },
    ],
}

ENTRANCES_PAYLOAD = {
    "spatialReference": {"wkid": 4326},
    "features": [
        {
            "attributes": {"objectid": 10, "id": 1, "ada": "Y",
                           "handletype": "Lever", "floor": 1,
                           "bldg_id": "00001",
                           "type": "Accessible Entrance with Automatic Door"},
            "geometry": {"x": -80.4240, "y": 37.2291,
                         "spatialReference": {"wkid": 4326}},
        },
        {
            "attributes": {"objectid": 11, "id": 2, "ada": "N",
                           "handletype": "Push", "floor": 1,
                           "bldg_id": "00001",
                           "type": "Non-Accessible Entrance"},
            "geometry": {"x": -80.4242, "y": 37.2292,
                         "spatialReference": {"wkid": 4326}},
        },
        {   # belongs to McBryde; must NOT leak into Burruss detail
            "attributes": {"objectid": 12, "id": 3, "ada": "Y",
                           "handletype": "Lever", "floor": 1,
                           "bldg_id": "00011",
                           "type": "Accessible Entrance without Automatic Door"},
            "geometry": {"x": -80.4218, "y": 37.2305,
                         "spatialReference": {"wkid": 4326}},
        },
    ],
}

ROUTE_PAYLOAD = {
    "routes": {
        "features": [{
            "attributes": {"Total_Length": 1080.5, "Total_Time": 0,
                           "Total_Kilometers": 0.329},
            "geometry": {
                "paths": [[
                    [-80.4240, 37.2292], [-80.4225, 37.2300],
                    [-80.4218, 37.2305],
                ]],
                "spatialReference": {"wkid": 4326},
            },
        }],
    },
    "directions": {
        "features": [
            {"attributes": {"text": "Head north on Alumni Mall", "length": 300.0,
                            "time": 0}},
            {"attributes": {"text": "Turn right and continue", "length": 780.5,
                            "time": 0}},
        ],
    },
}

NO_ROUTE_PAYLOAD = {
    "routes": {"features": []},
    "messages": [{"type": 50, "description": "Unable to complete route."}],
}

NO_ACCESSIBLE_PAYLOAD = {
    "error": {"code": 400, "message": "Unable to complete operation.",
              "details": ["No accessible path"]},
}


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


CLOSURE_START = datetime(2026, 9, 19, 8, 0, tzinfo=_CAMPUS)
CLOSURE_END = datetime(2026, 9, 26, 17, 0, tzinfo=_CAMPUS)
CLOSURES_PAYLOAD = {
    "spatialReference": {"wkid": 4326},
    "features": [
        {
            "attributes": {
                "objectid": 7, "constructionsite": "Alumni Mall Phase 2",
                "closurestartdate": _epoch_ms(CLOSURE_START),
                "closureenddate": _epoch_ms(CLOSURE_END),
                "projecttype": "Capital",
                "comments": "Sidewalk closed",
                "url": "https://vt.edu/closure/7",
            },
            "geometry": {"rings": [[
                [-80.4245, 37.2285], [-80.4240, 37.2285],
                [-80.4240, 37.2290], [-80.4245, 37.2290],
                [-80.4245, 37.2285],
            ]], "spatialReference": {"wkid": 4326}},
        },
        {
            "attributes": {
                "objectid": 8, "constructionsite": "Old scheduled work",
                "closurestartdate": _epoch_ms(datetime(2021, 1, 1, tzinfo=_CAMPUS)),
                "closureenddate": _epoch_ms(datetime(2021, 2, 1, tzinfo=_CAMPUS)),
                "projecttype": "Other",
            },
            "geometry": {"rings": [[
                [-80.430, 37.225], [-80.429, 37.225],
                [-80.429, 37.226], [-80.430, 37.226],
                [-80.430, 37.225],
            ]], "spatialReference": {"wkid": 4326}},
        },
    ],
}


class FakeHTTP:
    """Records GETs; returns a payload chosen by URL fragment."""

    def __init__(self, routes: dict | None = None, delay: float = 0.0):
        self.routes = routes or {}
        self.delay = delay
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, timeout: int) -> bytes:
        with self._lock:
            self.calls.append(url)
        if self.delay:
            import time
            time.sleep(self.delay)
        for fragment, payload in self.routes.items():
            if fragment in url:
                return json.dumps(payload).encode("utf-8")
        return json.dumps({}).encode("utf-8")

    @property
    def n(self) -> int:
        with self._lock:
            return len(self.calls)


class FakePOST:
    def __init__(self, payload, delay: float = 0.0):
        self.payload = payload
        self.delay = delay
        self.bodies: list[str] = []
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, url: str, data: bytes, timeout: int) -> bytes:
        with self._lock:
            self.calls += 1
            self.bodies.append(data.decode("utf-8"))
        if self.delay:
            import time
            time.sleep(self.delay)
        return json.dumps(self.payload).encode("utf-8")


class VTGISTCase(unittest.TestCase):
    def setUp(self):
        self._orig_cache_only = config.CACHE_ONLY
        self._orig_cache_dir = config.CACHE_DIR
        self._orig_http = cache._http
        self._orig_http_post = cache._http_post
        self._tmp = tempfile.mkdtemp(prefix="hokie-vtgis-test-")
        config.CACHE_ONLY = False
        config.CACHE_DIR = Path(self._tmp)
        cache._attempts.clear()
        cache._key_locks.clear()
        cache._http = FakeHTTP({
            "Buildings": BUILDINGS_PAYLOAD,
            "Accessibility": ENTRANCES_PAYLOAD,
            "Construction_Closures": CLOSURES_PAYLOAD,
        })

    def tearDown(self):
        cache._http = self._orig_http
        cache._http_post = self._orig_http_post
        config.CACHE_ONLY = self._orig_cache_only
        config.CACHE_DIR = self._orig_cache_dir
        cache._attempts.clear()
        cache._key_locks.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Query / form encoding
# --------------------------------------------------------------------------- #
class QueryEncodingTest(unittest.TestCase):
    def test_sql_literal_doubles_single_quote(self):
        self.assertEqual(vtgis.escape_sql_literal("O'Brien"), "'O''Brien'")

    def test_search_where_strips_like_wildcards_and_quotes(self):
        where = vtgis.build_building_search_where("100%_ Hall's")
        # The LIKE clause must not contain the user's wildcards.
        self.assertIn("UPPER(name) LIKE UPPER('%100 Hall''s%')", where)

    def test_build_where_id_matches_both_number_columns(self):
        where = vtgis.build_where_id("0001")
        self.assertIn("bldg_num = '0001'", where)
        self.assertIn("vtes_bldg_num = '0001'", where)

    def test_build_where_bldg_ids_is_deterministic_and_escaped(self):
        where = vtgis.build_where_bldg_ids(["b", "a", "a"])
        self.assertEqual(where, "bldg_id IN ('a','b')")

    def test_query_params_encode_booleans_lowercase(self):
        params = vtgis.build_query_params(where="1=1", return_geometry=False)
        self.assertEqual(params["returnGeometry"], "false")
        self.assertEqual(params["outSR"], "4326")

    def test_record_count_is_validated_and_capped_at_service_max(self):
        self.assertEqual(
            vtgis.build_query_params(result_record_count=999999)["resultRecordCount"],
            "2000")
        with self.assertRaises(ValueError):
            vtgis.build_query_params(result_record_count=0)

    def test_numeric_building_id_query_includes_real_padding_variants(self):
        where = vtgis.build_where_id("1")
        self.assertIn("bldg_num = '0001'", where)
        self.assertIn("vtes_bldg_num = '000001'", where)

    def test_query_url_percent_encodes_special_characters(self):
        params = vtgis.build_query_params(
            where="name = 'A & B's'", out_fields="name")
        url = vtgis.build_query_url(vtgis.BUILDINGS_LAYER, params)
        self.assertIn("name+%3D+%27A+%26+B%27s%27", url)
        self.assertTrue(url.startswith(vtgis.BUILDINGS_LAYER + "/query?"))

    def test_solve_form_carries_full_travel_mode(self):
        a = vtgis.RouteEndpoint(*BURRUSS, label="A")
        b = vtgis.RouteEndpoint(*MCBRYDE, label="B")
        form = vtgis.build_solve_form(a, b, vtgis.MODE_ADA_WALKING)
        tm = json.loads(form["travelMode"])
        self.assertEqual(tm["name"], "ADA Routes Only")
        self.assertEqual(tm["restrictionAttributeNames"], ["ADA_only"])
        self.assertIn("simplificationTolerance", tm)
        self.assertEqual(tm["simplificationToleranceUnits"], "esriMeters")
        stops = json.loads(form["stops"])
        self.assertEqual(len(stops["features"]), 2)
        self.assertEqual(stops["features"][0]["geometry"]["spatialReference"]["wkid"],
                         4326)
        # x is longitude, y is latitude
        self.assertAlmostEqual(stops["features"][0]["geometry"]["x"], BURRUSS[1])
        self.assertEqual(form["directionsLengthUnits"], "esriNAUFeet")

    def test_output_projection_does_not_relabel_wgs84_input_stops(self):
        form = vtgis.build_solve_form(vtgis.RouteEndpoint(*BURRUSS),
                                      vtgis.RouteEndpoint(*MCBRYDE), out_sr=2284)
        stops = json.loads(form["stops"])
        self.assertEqual(form["outSR"], "2284")
        self.assertEqual(stops["features"][0]["geometry"]["spatialReference"],
                         {"wkid": 4326})

    def test_solve_form_walking_has_no_restriction(self):
        form = vtgis.build_solve_form(vtgis.RouteEndpoint(*BURRUSS),
                                      vtgis.RouteEndpoint(*MCBRYDE))
        tm = json.loads(form["travelMode"])
        self.assertEqual(tm["name"], "Walking")
        self.assertEqual(tm["restrictionAttributeNames"], [])


# --------------------------------------------------------------------------- #
# CRS / geometry
# --------------------------------------------------------------------------- #
class GeometryTest(unittest.TestCase):
    def test_roundtrip_2284_projection(self):
        for lat, lon in (BURRUSS, MCBRYDE):
            x, y = vtgis.wgs84_to_projected_2284(lat, lon)
            back_lat, back_lon = vtgis.projected_2284_to_wgs84(x, y)
            self.assertAlmostEqual(back_lat, lat, places=9)
            self.assertAlmostEqual(back_lon, lon, places=9)

    def test_2284_easting_falls_in_official_buildings_extent(self):
        x, y = vtgis.wgs84_to_projected_2284(*BURRUSS)
        self.assertTrue(10_874_000 < x < 10_935_000, x)
        self.assertTrue(3_594_000 < y < 3_622_000, y)

    def test_point_geometry_wgs84_swaps_to_lat_lon(self):
        geom = {"x": -80.42396247, "y": 37.22924778,
                "spatialReference": {"wkid": 4326}}
        pts = vtgis.normalize_geometry_to_wgs84(geom)
        self.assertAlmostEqual(pts[0][0], 37.22924778)
        self.assertAlmostEqual(pts[0][1], -80.42396247)

    def test_point_geometry_in_native_2284_is_projected(self):
        x, y = vtgis.wgs84_to_projected_2284(*BURRUSS)
        geom = {"x": x, "y": y, "spatialReference": {"wkid": 102747}}
        pts = vtgis.normalize_geometry_to_wgs84(geom)
        self.assertAlmostEqual(pts[0][0], BURRUSS[0], places=6)
        self.assertAlmostEqual(pts[0][1], BURRUSS[1], places=6)

    def test_geojson_point_is_supported(self):
        pts = vtgis.normalize_geometry_to_wgs84(
            {"type": "Point", "coordinates": [-80.42, 37.23]})
        self.assertEqual(pts, [(37.23, -80.42)])

    def test_all_geojson_geometry_nesting_is_normalized(self):
        expected = [(37.0, -80.0), (38.0, -81.0)]
        geometries = (
            {"type": "MultiPoint", "coordinates": [[-80, 37], [-81, 38]]},
            {"type": "LineString", "coordinates": [[-80, 37], [-81, 38]]},
            {"type": "MultiLineString",
             "coordinates": [[[-80, 37]], [[-81, 38]]]},
            {"type": "Polygon",
             "coordinates": [[[-80, 37], [-81, 38]]]},
            {"type": "MultiPolygon",
             "coordinates": [[[[-80, 37]]], [[[-81, 38]]]]},
        )
        for geometry in geometries:
            with self.subTest(kind=geometry["type"]):
                self.assertEqual(vtgis.normalize_geometry_to_wgs84(geometry),
                                 expected)

    def test_malformed_geometry_vertices_are_skipped(self):
        geometry = {"type": "MultiPolygon", "coordinates": [
            [None, "bad-ring", [[-80, 37], ["bad", 2], [-81, 38]]],
        ]}
        self.assertEqual(vtgis.normalize_geometry_to_wgs84(geometry),
                         [(37.0, -80.0), (38.0, -81.0)])
        self.assertEqual(vtgis.normalize_geometry_to_wgs84([1, 2]), [])

    def test_arcgis_multi_geometries_in_native_projection(self):
        a = vtgis.wgs84_to_projected_2284(*BURRUSS)
        b = vtgis.wgs84_to_projected_2284(*MCBRYDE)
        for key, coordinates in (
                ("points", [a, b]),
                ("paths", [[a], [b]]),
                ("rings", [[a, b]])):
            points = vtgis.normalize_geometry_to_wgs84(
                {key: coordinates, "spatialReference": {"wkid": 2284}})
            self.assertAlmostEqual(points[0][0], BURRUSS[0], places=6)
            self.assertAlmostEqual(points[-1][1], MCBRYDE[1], places=6)

    def test_geojson_multipolygon_rings_have_no_variable_shadowing(self):
        geometry = {"type": "MultiPolygon", "coordinates": [
            [[[-80, 37], [-81, 38]]], [[[-82, 39], [-83, 40]]],
        ]}
        rings = vtgis.normalize_polygon_rings(geometry)
        self.assertEqual(rings, [
            [(37.0, -80.0), (38.0, -81.0)],
            [(39.0, -82.0), (40.0, -83.0)],
        ])

    def test_paths_are_flattened_for_route_geometry(self):
        geom = {"paths": [[[-80.42, 37.22], [-80.41, 37.23]]],
                "spatialReference": {"wkid": 4326}}
        pts = vtgis.normalize_geometry_to_wgs84(geom)
        self.assertEqual(pts, [(37.22, -80.42), (37.23, -80.41)])

    def test_unknown_crs_raises(self):
        with self.assertRaises(vtgis.VTGISError):
            vtgis.normalize_geometry_to_wgs84(
                {"x": 1, "y": 2, "spatialReference": {"wkid": 99999}})


# --------------------------------------------------------------------------- #
# Distance
# --------------------------------------------------------------------------- #
class DistanceTest(unittest.TestCase):
    def test_feet_meters_conversion(self):
        self.assertAlmostEqual(vtgis.feet_to_meters(1000), 304.8)
        self.assertAlmostEqual(vtgis.meters_to_feet(304.8), 1000.0)

    def test_haversine_matches_known_distance(self):
        d = vtgis.haversine_m(BURRUSS, MCBRYDE)
        self.assertGreater(d, 150)
        self.assertLess(d, 250)

    def test_polyline_length_is_sum_of_segments(self):
        pts = [(37.0, -80.0), (37.0, -80.01), (37.01, -80.01)]
        self.assertAlmostEqual(vtgis.polyline_length_m(pts),
                               vtgis.haversine_m(pts[0], pts[1])
                               + vtgis.haversine_m(pts[1], pts[2]))

    def test_estimate_is_labelled_and_uses_declared_speed(self):
        minutes, label = vtgis.estimate_walk_minutes(1350.0, speed_mps=1.35)
        self.assertAlmostEqual(minutes, 16.7, places=1)
        self.assertIn("ESTIMATE", label)
        self.assertIn("1.35", label)


# --------------------------------------------------------------------------- #
# Buildings / entrances
# --------------------------------------------------------------------------- #
class BuildingTest(VTGISTCase):
    def test_normalize_building_prefers_official_lat_lon(self):
        rows = vtgis.normalize_buildings(BUILDINGS_PAYLOAD)
        b = rows[0]
        self.assertEqual(b.building_id, "1")
        self.assertEqual(b.name, "Burruss Hall")
        self.assertAlmostEqual(b.lat, BURRUSS[0])
        self.assertAlmostEqual(b.lon, BURRUSS[1])
        self.assertTrue(b.coords_authoritative)
        self.assertTrue(len(b.geometry) >= 4)

    def test_building_search_and_join_end_to_end(self):
        rows = vtgis.search_buildings("burruss")
        self.assertIsInstance(rows, list)
        self.assertIn("Burruss Hall", [r.name for r in rows])
        detail = vtgis.building_detail("0001")
        self.assertIsNotNone(detail)
        names = {e.entrance_type for e in detail.entrances}
        self.assertIn("Accessible Entrance with Automatic Door", names)
        self.assertIn("Non-Accessible Entrance", names)
        self.assertEqual(detail.accessible_entrance_count, 1)
        # McBryde's entrance must not leak into Burruss.
        self.assertNotIn("Accessible Entrance without Automatic Door", names)

    def test_entrance_join_key_is_zero_padding_insensitive(self):
        self.assertEqual(vtgis.canonical_building_id("00001"), "1")
        self.assertEqual(vtgis.canonical_building_id("1"), "1")
        self.assertEqual(vtgis.canonical_building_id("000011"), "11")

    def test_entrance_server_query_includes_five_digit_join_key(self):
        building = vtgis.normalize_buildings(BUILDINGS_PAYLOAD)[0]
        rows = vtgis.entrances_for_building(building)
        self.assertIsInstance(rows, list)
        query = __import__("urllib.parse").parse.unquote_plus(cache._http.calls[-1])
        self.assertIn("bldg_id IN", query)
        self.assertIn("'00001'", query)

    def test_search_deduplicates_multiple_polygons_for_one_building(self):
        payload = json.loads(json.dumps(BUILDINGS_PAYLOAD))
        payload["features"].append(json.loads(json.dumps(payload["features"][0])))
        payload["features"][-1]["attributes"]["objectid"] = 99
        cache._http = FakeHTTP({"Buildings": payload})
        rows = vtgis.search_buildings("Hall")
        self.assertEqual([row.building_id for row in rows].count("1"), 1)

    def test_building_detail_does_not_turn_entrance_failure_into_zero(self):
        def http(url, timeout):
            if "Buildings" in url:
                return json.dumps(BUILDINGS_PAYLOAD).encode()
            raise OSError("entrance layer down")

        cache._http = http
        detail = vtgis.building_detail("0001")
        self.assertIsInstance(detail, vtgis.Unavailable)
        self.assertIn("GIS request failed", detail.reason)

    def test_building_by_id_unknown_returns_none(self):
        cache._http = FakeHTTP({"Buildings": {"features": []}})
        self.assertIsNone(vtgis.building_by_id("does-not-exist"))


# --------------------------------------------------------------------------- #
# Routes: walking vs ADA, no-route, provenance, estimate
# --------------------------------------------------------------------------- #
class RouteTest(VTGISTCase):
    def _solve(self, mode, payload=ROUTE_PAYLOAD):
        cache._http_post = FakePOST(payload)
        return vtgis.solve_route(BURRUSS, MCBRYDE, mode=mode)

    def test_walking_route_distance_geometry_directions(self):
        route = self._solve(vtgis.MODE_WALKING)
        self.assertEqual(route.status, vtgis.STATUS_OK)
        self.assertAlmostEqual(route.distance_ft, 1080.5)
        self.assertAlmostEqual(route.distance_m, 329.34, places=1)
        self.assertEqual(len(route.geometry), 3)
        self.assertEqual(len(route.directions), 2)
        self.assertEqual(route.distance_source, "gis_total_length_ft")
        self.assertFalse(route.closures_considered)
        self.assertFalse(route.gis_travel_time_available)
        self.assertIn("CPIF", route.provenance.attribution)

    def test_estimated_minutes_labelled_not_gis_time(self):
        route = self._solve(vtgis.MODE_WALKING)
        self.assertIsNotNone(route.estimated_minutes)
        self.assertIn("ESTIMATE", route.estimated_minutes_label)
        self.assertNotIn(route.estimated_minutes, (0, None))

    def test_ada_route_sends_ada_travel_mode(self):
        cache._http_post = FakePOST(ROUTE_PAYLOAD)
        route = vtgis.solve_route(BURRUSS, MCBRYDE, mode=vtgis.MODE_ADA_WALKING)
        self.assertEqual(route.mode, vtgis.MODE_ADA_WALKING)
        body = cache._http_post.bodies[0]
        self.assertIn("ADA_only", body.replace("%22", '"'))
        self.assertIn("ADA", body)

    def test_caller_coordinates_are_not_authoritative(self):
        cache._http_post = FakePOST(ROUTE_PAYLOAD)
        route = vtgis.solve_route(BURRUSS, MCBRYDE, mode=vtgis.MODE_WALKING)
        self.assertFalse(route.from_endpoint.authoritative)
        self.assertFalse(route.to_endpoint.authoritative)
        self.assertIn("NOT authoritative",
                      route.from_endpoint.as_dict()["coordinate_basis"])
        cache_names = [p.name for p in Path(self._tmp).glob("vtgis_route*.json")]
        self.assertTrue(cache_names)
        self.assertTrue(all(str(BURRUSS[0]) not in name for name in cache_names))

    def test_caller_dict_cannot_self_declare_authoritative(self):
        cache._http_post = FakePOST(ROUTE_PAYLOAD)
        route = vtgis.solve_route(
            {"lat": BURRUSS[0], "lon": BURRUSS[1], "authoritative": True},
            {"lat": MCBRYDE[0], "lon": MCBRYDE[1]}, mode=vtgis.MODE_WALKING)
        self.assertFalse(route.from_endpoint.authoritative)

    def test_caller_route_endpoint_cannot_self_declare_authoritative(self):
        cache._http_post = FakePOST(ROUTE_PAYLOAD)
        claimed = vtgis.RouteEndpoint(*BURRUSS, authoritative=True,
                                      building_id="forged")
        route = vtgis.solve_route(claimed, MCBRYDE)
        self.assertFalse(route.from_endpoint.authoritative)
        self.assertIsNone(route.from_endpoint.building_id)

    def test_out_of_range_caller_coordinate_rejected(self):
        with self.assertRaises(ValueError):
            vtgis.solve_route((999.0, -80.0), MCBRYDE)

    def test_route_between_buildings_snaps_to_authoritative_entrance(self):
        cache._http_post = FakePOST(ROUTE_PAYLOAD)
        route = vtgis.route_between_buildings("0001", "0011",
                                              mode=vtgis.MODE_ADA_WALKING)
        self.assertEqual(route.status, vtgis.STATUS_OK)
        self.assertTrue(route.from_endpoint.authoritative)
        self.assertTrue(route.to_endpoint.authoritative)
        self.assertIsNotNone(route.from_endpoint.entrance_id)
        self.assertEqual(route.from_endpoint.building_id, "1")

    def test_ada_no_route_is_a_valid_typed_result(self):
        route = self._solve(vtgis.MODE_ADA_WALKING, NO_ACCESSIBLE_PAYLOAD)
        self.assertIsInstance(route, vtgis.NoRoute)
        self.assertEqual(route.status, vtgis.STATUS_NO_ROUTE)
        self.assertEqual(route.mode, vtgis.MODE_ADA_WALKING)

    def test_empty_features_is_no_route(self):
        route = self._solve(vtgis.MODE_WALKING, NO_ROUTE_PAYLOAD)
        self.assertIsInstance(route, vtgis.NoRoute)
        self.assertIn("no route", route.reason)

    def test_route_without_gis_length_falls_back_to_geometry(self):
        payload = {
            "routes": {"features": [{
                "attributes": {"Total_Time": 0},
                "geometry": {"paths": [[[-80.424, 37.2292], [-80.4218, 37.2305]]],
                             "spatialReference": {"wkid": 4326}},
            }]},
        }
        route = self._solve(vtgis.MODE_WALKING, payload)
        self.assertEqual(route.distance_source, "computed_from_wgs84_geometry")
        self.assertGreater(route.distance_ft, 0)

    def test_direction_zero_time_is_reported_unavailable_not_zero(self):
        route = self._solve(vtgis.MODE_WALKING)
        self.assertTrue(all(s.time_min is None for s in route.directions))
        self.assertTrue(all(s.time_source == "unavailable" for s in route.directions))

    def test_total_time_zero_wins_over_inconsistent_direction_time(self):
        payload = json.loads(json.dumps(ROUTE_PAYLOAD))
        payload["directions"] = [{"features": [{"attributes": {
            "text": "Walk", "length": 10, "time": 2}}]}]
        route = self._solve(vtgis.MODE_WALKING, payload)
        self.assertFalse(route.gis_travel_time_available)
        self.assertEqual(len(route.directions), 1)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            vtgis.solve_route(BURRUSS, MCBRYDE, mode="teleport")

    def test_ui_modes_present_and_walking_based(self):
        by_ui = {o["ui_mode"]: o for o in vtgis.UI_MODE_OPTIONS}
        self.assertEqual(set(by_ui), {"fastest", "least_walking", "ada_walking"})
        self.assertEqual(by_ui["fastest"]["route_mode"], vtgis.MODE_WALKING)
        self.assertEqual(by_ui["least_walking"]["route_mode"], vtgis.MODE_WALKING)
        self.assertEqual(by_ui["ada_walking"]["route_mode"], vtgis.MODE_ADA_WALKING)
        self.assertFalse(by_ui["fastest"]["multimodal"])


# --------------------------------------------------------------------------- #
# Closures
# --------------------------------------------------------------------------- #
class ClosureTest(VTGISTCase):
    def test_normalize_closures_parses_effective_interval(self):
        rows = vtgis.normalize_closures(
            CLOSURES_PAYLOAD, layer=vtgis.CONSTRUCTION_CLOSURES_CURRENT,
            source_label="Current construction closures")
        self.assertEqual(len(rows), 2)
        c = rows[0]
        self.assertEqual(c.name, "Alumni Mall Phase 2")
        self.assertEqual(c.start.astimezone(_CAMPUS), CLOSURE_START)
        self.assertEqual(c.end.astimezone(_CAMPUS), CLOSURE_END)
        self.assertGreater(len(c.geometry), 3)
        self.assertEqual(c.as_dict()["geometry"][0], [37.2285, -80.4245])

    def test_effective_at_inside_and_outside(self):
        rows = vtgis.normalize_closures(
            CLOSURES_PAYLOAD, layer="x", source_label="x")
        active = rows[0]
        self.assertTrue(active.is_effective_at(
            datetime(2026, 9, 21, 12, 0, tzinfo=_CAMPUS)))
        self.assertFalse(active.is_effective_at(
            datetime(2026, 10, 1, 12, 0, tzinfo=_CAMPUS)))
        self.assertFalse(active.is_effective_at(
            datetime(2026, 9, 1, 12, 0, tzinfo=_CAMPUS)))

    def test_open_ended_closure_is_effective(self):
        payload = {"features": [{"attributes": {
            "objectid": 1, "constructionsite": "Open",
            "closurestartdate": _epoch_ms(CLOSURE_START)}}]}
        row = vtgis.normalize_closures(payload, layer="x", source_label="x")[0]
        self.assertTrue(row.is_effective_at(
            datetime(2030, 1, 1, tzinfo=_CAMPUS)))

    def test_closures_for_day_overlaps(self):
        rows = vtgis.closures_for_day(
            datetime(2026, 9, 22, 9, 0, tzinfo=_CAMPUS))
        self.assertTrue(rows)
        self.assertTrue(all(r.name == "Alumni Mall Phase 2" for r in rows))

    def test_route_never_claims_closure_avoidance(self):
        payload = {
            "routes": {"features": [{
                "attributes": {"Total_Length": 10},
                "geometry": {"paths": [[[-80.42, 37.22], [-80.41, 37.23]]],
                             "spatialReference": {"wkid": 4326}}}]}}
        form = vtgis.build_solve_form(vtgis.RouteEndpoint(*BURRUSS),
                                      vtgis.RouteEndpoint(*MCBRYDE))
        self.assertNotIn("barriers", form)
        route = vtgis.normalize_route(
            payload, mode=vtgis.MODE_WALKING,
            from_endpoint=vtgis.RouteEndpoint(*BURRUSS),
            to_endpoint=vtgis.RouteEndpoint(*MCBRYDE))
        self.assertFalse(route.closures_considered)
        self.assertTrue(any("Closures are NOT applied" in n for n in route.notes))


# --------------------------------------------------------------------------- #
# Malformed responses
# --------------------------------------------------------------------------- #
class MalformedTest(VTGISTCase):
    def test_buildings_not_object_raises(self):
        with self.assertRaises(vtgis.VTGISError):
            vtgis.normalize_buildings("nope")

    def test_buildings_features_not_list_raises(self):
        with self.assertRaises(vtgis.VTGISError):
            vtgis.normalize_buildings({"features": "nope"})

    def test_bad_features_are_skipped(self):
        rows = vtgis.normalize_buildings({"features": [None, 42, {}]})
        self.assertEqual(rows, [])

    def test_route_non_object_raises(self):
        with self.assertRaises(vtgis.VTGISError):
            vtgis.normalize_route("nope", mode=vtgis.MODE_WALKING,
                                  from_endpoint=vtgis.RouteEndpoint(*BURRUSS),
                                  to_endpoint=vtgis.RouteEndpoint(*MCBRYDE))

    def test_query_error_payload_returns_unavailable(self):
        cache._http = FakeHTTP({"Buildings": {
            "error": {"code": 400, "message": "bad where"}}})
        result = vtgis.search_buildings("burruss")
        self.assertIsInstance(result, vtgis.Unavailable)
        self.assertIn("query returned an error", result.reason)

    def test_api_returns_unavailable_on_malformed_payload(self):
        # FakeHTTP json-encodes whatever it is given, so "nope" arrives as a
        # JSON string; the API must degrade, not raise.
        cache._http = FakeHTTP({"Buildings": "nope"})
        result = vtgis.search_buildings("burruss")
        self.assertIsInstance(result, vtgis.Unavailable)
        self.assertEqual(result.status, vtgis.STATUS_UNAVAILABLE)


# --------------------------------------------------------------------------- #
# Cache / live separation + offline behavior
# --------------------------------------------------------------------------- #
class CacheSeparationTest(VTGISTCase):
    def test_live_fetch_populates_cache_then_cache_only_reads_it(self):
        live = vtgis.search_buildings("burruss")
        self.assertIsInstance(live, list)
        self.assertIn("Burruss Hall", [b.name for b in live])
        calls_after_live = cache._http.n

        config.CACHE_ONLY = True
        cache._http = FakeHTTP({})  # would return {} if ever called

        def boom(url, timeout):
            raise AssertionError("cache mode must never open a socket")

        cache._http = boom
        replay = vtgis.search_buildings("burruss")
        self.assertIsInstance(replay, list)
        self.assertIn("Burruss Hall", [b.name for b in replay])
        self.assertGreater(calls_after_live, 0)

    def test_cache_only_miss_returns_typed_unavailable_with_fallback(self):
        config.CACHE_ONLY = True

        def boom(url, timeout):
            raise AssertionError("cache mode must never open a socket")

        cache._http = boom
        result = vtgis.search_buildings("burruss")
        self.assertIsInstance(result, vtgis.Unavailable)
        self.assertIsNotNone(result.fallback)
        self.assertIn("haversine", result.fallback["walk"])

    def test_closure_layers_do_not_collide_in_cache(self):
        # current and scheduled share where=1=1; the cache key must differ.
        current = vtgis._fetch_closure_layer(
            "Construction_Closures/0", vtgis.CONSTRUCTION_CLOSURES_CURRENT,
            "vtgis_closures_current", "Current construction closures", force=False)
        scheduled = vtgis._fetch_closure_layer(
            "Construction_Closures/1", vtgis.CONSTRUCTION_CLOSURES_SCHEDULED,
            "vtgis_closures_scheduled", "Scheduled construction closures", force=False)
        self.assertIsInstance(current, list)
        self.assertIsInstance(scheduled, list)
        files = {p.name for p in Path(self._tmp).glob("*.json")}
        self.assertEqual(len(files), 2, files)

    def test_walk_fallback_is_labelled(self):
        out = vtgis.walk_fallback(BURRUSS, MCBRYDE)
        self.assertFalse(out["routed"])
        self.assertFalse(out["gis"])
        self.assertIn("STRAIGHT-LINE", out["method"])
        self.assertIn("ESTIMATE", out["estimated_minutes_label"])


# --------------------------------------------------------------------------- #
# POST helper: form encoding + cache concurrency
# --------------------------------------------------------------------------- #
class PostHelperTest(unittest.TestCase):
    def setUp(self):
        self._orig_cache_only = config.CACHE_ONLY
        self._orig_cache_dir = config.CACHE_DIR
        self._orig_http_post = cache._http_post
        self._tmp = tempfile.mkdtemp(prefix="hokie-post-test-")
        config.CACHE_ONLY = False
        config.CACHE_DIR = Path(self._tmp)
        cache._attempts.clear()
        cache._key_locks.clear()

    def tearDown(self):
        cache._http_post = self._orig_http_post
        config.CACHE_ONLY = self._orig_cache_only
        config.CACHE_DIR = self._orig_cache_dir
        cache._attempts.clear()
        cache._key_locks.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_form_body_is_urlencoded(self):
        post = FakePOST({"ok": True})
        cache._http_post = post
        cache.post_form_json("t", "http://x", {"a": "b c", "n": 1},
                             params={"k": "1"})
        self.assertEqual(post.bodies[0], "a=b+c&n=1")

    def test_form_body_supports_repeated_keys(self):
        post = FakePOST({"ok": True})
        cache._http_post = post
        cache.post_form_json("t2", "http://x", {"f": ["json", "geojson"]},
                             params={"k": "2"})
        self.assertEqual(post.bodies[0], "f=json&f=geojson")

    def test_concurrent_cold_callers_make_one_post(self):
        post = FakePOST({"ok": True}, delay=0.05)
        cache._http_post = post
        barrier = threading.Barrier(4)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def call():
            barrier.wait()
            try:
                cache.post_form_json("concurrent", "http://x", {"a": "1"},
                                     params={"k": "3"}, max_age_s=60)
            except BaseException as exc:          # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(post.calls, 1)

    def test_cache_only_never_posts(self):
        post = FakePOST({"ok": True})
        cache._http_post = post
        cache.post_form_json("seed", "http://x", {"a": "1"}, params={"k": "4"})
        self.assertEqual(post.calls, 1)

        config.CACHE_ONLY = True

        def boom(url, data, timeout):
            raise AssertionError("cache mode must never POST")

        cache._http_post = boom
        out = cache.post_form_json("seed", "http://x", {"a": "1"},
                                   params={"k": "4"})
        self.assertEqual(out, {"ok": True})

    def test_cold_cache_only_miss_raises_cache_miss(self):
        config.CACHE_ONLY = True

        def boom(url, data, timeout):
            raise AssertionError("cache mode must never POST")

        cache._http_post = boom
        with self.assertRaises(cache.CacheMiss):
            cache.post_form_json("missing", "http://x", {"a": "1"},
                                 params={"k": "5"})

    def _age_out(self, name, params, age_s=3600):
        import json as _json
        matches = list(Path(self._tmp).glob(f"{name}*.json"))
        self.assertEqual(len(matches), 1, (params, matches))
        p = matches[0]
        env = _json.loads(p.read_text(encoding="utf-8"))
        env["fetched_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=age_s)
        ).isoformat(timespec="seconds")
        p.write_text(_json.dumps(env), encoding="utf-8")

    def test_post_cache_metadata_does_not_persist_url_or_form_secrets(self):
        cache._http_post = FakePOST({"ok": True})
        cache.post_form_json(
            "private", "https://user:pass@example.test/solve?token=abc",
            {"password": "body-secret", "f": "json"}, params={"mode": "walk"})
        text = next(Path(self._tmp).glob("private*.json")).read_text()
        self.assertNotIn("user", text)
        self.assertNotIn("pass", text)
        self.assertNotIn("token", text)
        self.assertNotIn("abc", text)
        self.assertNotIn("body-secret", text)
        self.assertIn("https://example.test/solve", text)

    def test_cache_identity_always_includes_url_and_form_body(self):
        calls = []

        def echo(url, data, timeout):
            calls.append((url, data))
            return json.dumps({"url": url, "body": data.decode()}).encode()

        cache._http_post = echo
        first = cache.post_form_json("identity", "http://x", {"stop": "A"},
                                     params={"same": "key"})
        second = cache.post_form_json("identity", "http://x", {"stop": "B"},
                                      params={"same": "key"})
        third = cache.post_form_json("identity", "http://y", {"stop": "A"},
                                     params={"same": "key"})
        self.assertEqual(len(calls), 3)
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)
        names = [p.name for p in Path(self._tmp).glob("identity*.json")]
        self.assertEqual(len(names), 3)
        self.assertTrue(all("stop" not in name for name in names))

    def test_stale_post_failure_falls_back_to_cached_copy(self):
        cache._http_post = FakePOST({"v": "good"})
        cache.post_form_json("fb", "http://x", {"a": "1"}, params={"k": "8"})
        self._age_out("fb", {"k": "8"})

        def fail(url, data, timeout):
            raise OSError("upstream down")

        cache._http_post = fail
        out = cache.post_form_json("fb", "http://x", {"a": "1"},
                                   params={"k": "8"}, max_age_s=60)
        self.assertEqual(out, {"v": "good"})

    def test_force_bypasses_post_cooldown(self):
        cache._http_post = FakePOST({"v": "old"})
        cache.post_form_json("cd", "http://x", {"a": "1"}, params={"k": "9"})
        self._age_out("cd", {"k": "9"})

        def fail(url, data, timeout):
            raise OSError("down")

        cache._http_post = fail
        self.assertEqual(
            cache.post_form_json("cd", "http://x", {"a": "1"},
                                 params={"k": "9"}, max_age_s=60),
            {"v": "old"})
        cache._http_post = FakePOST({"v": "new"})
        out = cache.post_form_json("cd", "http://x", {"a": "1"},
                                   params={"k": "9"}, max_age_s=60, force=True)
        self.assertEqual(out, {"v": "new"})


# --------------------------------------------------------------------------- #
# Permission gate
# --------------------------------------------------------------------------- #
class PermissionTest(unittest.TestCase):
    def _route(self):
        return vtgis.normalize_route(
            ROUTE_PAYLOAD, mode=vtgis.MODE_WALKING,
            from_endpoint=vtgis.RouteEndpoint(*BURRUSS),
            to_endpoint=vtgis.RouteEndpoint(*MCBRYDE))

    def test_route_export_refused_without_flag(self):
        with self.assertRaises(vtgis.RedistributionNotPermitted):
            vtgis.route_to_geojson(self._route())

    def test_buildings_export_refused_without_flag(self):
        with self.assertRaises(vtgis.RedistributionNotPermitted):
            vtgis.buildings_to_geojson(vtgis.normalize_buildings(BUILDINGS_PAYLOAD))

    def test_snapshot_refused_without_flag(self):
        with self.assertRaises(vtgis.RedistributionNotPermitted):
            vtgis.snapshot_payload({"raw": True})

    def test_boolean_cannot_bypass_written_permission_gate(self):
        with self.assertRaises(vtgis.RedistributionNotPermitted):
            vtgis.assert_redistribution_allowed(True)

    def test_short_or_blank_permission_reference_is_refused(self):
        for reference in ("", "yes", "   "):
            with self.subTest(reference=reference):
                with self.assertRaises(vtgis.RedistributionNotPermitted):
                    vtgis.authorize_redistribution(reference)

    def test_export_allowed_with_written_permission_reference(self):
        permit = vtgis.authorize_redistribution("VT-GIS-PERMISSION-2026-001")
        geojson = vtgis.route_to_geojson(self._route(), permission=permit)
        self.assertEqual(geojson["type"], "FeatureCollection")
        self.assertEqual(geojson["features"][0]["geometry"]["type"], "LineString")
        # GeoJSON coordinates are [lon, lat]
        first = geojson["features"][0]["geometry"]["coordinates"][0]
        self.assertLess(first[0], 0)
        self.assertGreater(first[1], 0)

    def test_provenance_states_no_redistribution(self):
        prov = self._route().provenance
        self.assertFalse(prov.redistribution_allowed)
        self.assertIn("Virginia Tech", prov.attribution)
        self.assertIn("survey", prov.disclaimer)


# --------------------------------------------------------------------------- #
# No credentials embedded
# --------------------------------------------------------------------------- #
class NoCredentialsTest(unittest.TestCase):
    def test_module_source_has_no_credentials(self):
        src = Path(vtgis.__file__).read_text(encoding="utf-8").lower()
        forbidden = ("api_key", "apikey", "password", "secret", "bearer ",
                     "authorization:", "private key", "client_secret")
        for token in forbidden:
            self.assertNotIn(token, src, f"credential-like token {token!r} found")

    def test_all_endpoints_are_public_anonymous(self):
        for url in (vtgis.BUILDINGS_LAYER, vtgis.ACCESSIBILITY_ENTRANCES_LAYER,
                    vtgis.ROUTE_SERVICE, vtgis.CONSTRUCTION_CLOSURES_CURRENT):
            self.assertTrue(url.startswith("https://"))
            self.assertNotIn("token", url.lower())


if __name__ == "__main__":
    unittest.main()