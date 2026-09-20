"""Offline tests for hokieday.weather (stdlib unittest, DEMO_MODE=cache).

No network. Pure parsing/risk functions are tested directly. Cache-backed
fetchers are tested against a TEMPORARY synthetic cache (config.CACHE_ONLY=True
+ a temp config.CACHE_DIR), so no committed weather fixture is required. The
replay store deliberately has NO weather fixtures (see docs/WEATHER.md): the
real capture was incoherent with the bus replay clock, so replay returns a typed
"unavailable" until a full coherent bundle can be captured.
"""
from __future__ import annotations

import shutil
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hokieday import cache, config, weather

NY = ZoneInfo("America/New_York")
STAMP = "2026-09-19T15:22:29+00:00"
STAMP_DT = datetime(2026, 9, 19, 15, 22, 29, tzinfo=timezone.utc)

T0 = "2026-09-19T10:00:00-04:00"
T1 = "2026-09-19T11:00:00-04:00"
T2 = "2026-09-19T12:00:00-04:00"
T3 = "2026-09-19T13:00:00-04:00"

POINT_PARAMS = {"lat": "37.2292", "lon": "-80.4240"}
HOURLY_PARAMS = {"grid": "RNK", "x": "57", "y": "65"}
STATIONS_PARAMS = {"grid": "RNK", "x": "57", "y": "65"}
ZONE = "VAZ014"


# ------------------------------------------------------------------ builders
def window(start: str, end: str, **overrides) -> dict:
    """A minimal normalized forecast window for pure risk tests.

    Defaults carry the three required hazard families so a default window can
    legitimately score "none". Tests that omit fields exercise the missing-data
    path explicitly.
    """
    base = {
        "kind": "forecast", "start": start, "end": end,
        "timezone": config.CAMPUS_TZ, "temperature_c": 20.0,
        "temperature_f": 68.0, "temperature_unit": "F",
        "precip_probability_pct": 5.0, "relative_humidity_pct": None,
        "wind_speed_kph": 5.0, "wind_speed_mph": 3.1, "wind_speed_text": "5 mph",
        "wind_direction": None, "short_forecast": "Sunny",
        "detailed_forecast": "", "is_daytime": True,
        "source": "test", "fetched_at": STAMP, "stale": False,
    }
    base.update(overrides)
    return base


def alert(**overrides) -> dict:
    base = {
        "kind": "alert", "id": "urn:test:1", "event": "Severe Thunderstorm Warning",
        "severity": "Severe", "certainty": "Likely", "urgency": "Immediate",
        "status": "Actual", "message_type": "Alert", "category": "Met",
        "response": "Shelter", "headline": "Severe Thunderstorm Warning",
        "description": "A line of storms is moving through.",
        "instruction": "Move indoors.", "area_desc": "Montgomery",
        "sender_name": "NWS Blacksburg VA",
        "effective": "2026-09-19T10:00:00-04:00",
        "onset": "2026-09-19T10:10:00-04:00",
        "expires": "2026-09-19T11:30:00-04:00",
        "ends": None, "source": "test alerts", "fetched_at": STAMP, "stale": False,
    }
    base.update(overrides)
    return base


def period(start, end, temp_f=68, precip=10, wind="5 mph", short="Sunny"):
    return {
        "number": 1, "startTime": start, "endTime": end, "isDaytime": True,
        "temperature": temp_f, "temperatureUnit": "F",
        "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": precip},
        "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 55},
        "windSpeed": wind, "windDirection": "S", "shortForecast": short,
    }


def point_payload(zone=ZONE) -> dict:
    return {"properties": {
        "gridId": "RNK", "gridX": 57, "gridY": 65,
        "forecastZone": f"https://api.weather.gov/zones/forecast/{zone}",
        "county": "https://api.weather.gov/zones/county/VAC121",
        "timeZone": "America/New_York", "forecastHourly": "https://e.test/hourly",
        "observationStations": "https://e.test/stations", "radarStation": "KFCX",
    }}


def stations_payload(entries) -> dict:
    return {"features": [{"properties": {
        "stationIdentifier": sid, "name": name,
        "distance": {"unitCode": "wmoUnit:m", "value": dist},
        "timeZone": "America/New_York",
    }} for sid, name, dist in entries]}


def alert_feature(alert_id="urn:test:zone", event="Flood Warning", severity="Moderate"):
    return {"properties": {
        "id": alert_id, "event": event, "severity": severity,
        "certainty": "Likely", "urgency": "Expected", "status": "Actual",
        "messageType": "Alert", "category": "Met", "response": "Avoid",
        "headline": event, "description": "d", "instruction": "i",
        "areaDesc": "Montgomery", "senderName": "NWS Blacksburg VA",
        "effective": "2026-09-19T10:00:00-04:00", "onset": "2026-09-19T10:00:00-04:00",
        "expires": "2026-09-19T12:00:00-04:00", "ends": None,
    }}


class TempCacheCase(unittest.TestCase):
    """Swap the cache to a temp dir in CACHE_ONLY mode (no network possible)."""

    def setUp(self):
        self._orig_only = config.CACHE_ONLY
        self._orig_dir = config.CACHE_DIR
        self._tmp = tempfile.mkdtemp(prefix="hokie-weather-test-")
        config.CACHE_ONLY = True
        config.CACHE_DIR = Path(self._tmp)

    def tearDown(self):
        config.CACHE_ONLY = self._orig_only
        config.CACHE_DIR = self._orig_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def seed(self, name, payload, params=None, url="https://e.test/x",
             fetched_at=STAMP):
        return cache.write_envelope_atomic(name, url, payload, params=params,
                                           fetched_at=fetched_at)

    def seed_full(self, *, point_age=None, hourly_periods=None, alerts_point=None,
                  alerts_zone=None, stations=None, observation=None):
        point_stamp = STAMP if point_age is None else point_age
        self.seed("weather_points", point_payload(), POINT_PARAMS,
                  fetched_at=point_stamp)
        if hourly_periods is not None:
            self.seed("weather_hourly", {"properties": {"periods": hourly_periods}},
                      HOURLY_PARAMS)
        if alerts_point is not None:
            self.seed("weather_alerts_point", alerts_point, POINT_PARAMS)
        if alerts_zone is not None:
            self.seed("weather_alerts_zone", alerts_zone, {"zone": ZONE})
        if stations is not None:
            self.seed("weather_stations", stations, STATIONS_PARAMS)
        if observation is not None:
            self.seed("weather_observation", observation, {"station": "KBCB"})


# ------------------------------------------------------------- parsing / units
class TestNormalize(unittest.TestCase):
    def test_hourly_fahrenheit_to_celsius_both_kept(self):
        payload = {"properties": {"periods": [period(
            T0, T1, temp_f=68, precip=55, wind="10 mph", short="Chance Showers")]}}
        w = weather.normalize_hourly(payload)[0]
        self.assertAlmostEqual(w["temperature_c"], 20.0, places=1)
        self.assertEqual(w["temperature_f"], 68.0)
        self.assertEqual(w["temperature_unit"], "F")
        self.assertEqual(w["precip_probability_pct"], 55.0)
        self.assertAlmostEqual(w["wind_speed_kph"], 16.1, places=1)
        self.assertEqual(w["wind_speed_text"], "10 mph")
        self.assertEqual(w["kind"], "forecast")

    def test_null_precip_is_none_not_zero(self):
        p = period(T0, T1, precip=None)
        w = weather.normalize_hourly({"properties": {"periods": [p]}})[0]
        self.assertIsNone(w["precip_probability_pct"])

    def test_wind_range_uses_maximum(self):
        cases = [
            ("25 to 45 mph", 45 * 1.609344),
            ("25-45 mph", 45 * 1.609344),
            ("20 to 30 km/h", 30.0),
            ("15 to 20 kt", 20 * 1.852),
            ("3 mph", 3 * 1.609344),
        ]
        for text, expected_kph in cases:
            with self.subTest(text=text):
                kph, mph, raw = weather._parse_wind_text(text)
                self.assertAlmostEqual(kph, expected_kph, places=2)
                self.assertAlmostEqual(mph, expected_kph * 0.621371, places=2)
                self.assertEqual(raw, text)

    def test_wind_range_normalizes_through_hourly(self):
        p = period(T0, T1, wind="25 to 45 mph")
        w = weather.normalize_hourly({"properties": {"periods": [p]}})[0]
        self.assertAlmostEqual(w["wind_speed_kph"], 45 * 1.609344, places=1)

    def test_observation_celsius_to_fahrenheit_and_wind(self):
        payload = {"properties": {
            "station": "https://api.weather.gov/stations/KBCB",
            "timestamp": "2026-09-19T20:55:00+00:00",
            "temperature": {"unitCode": "wmoUnit:degC", "value": 27.5},
            "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 12.96},
            "windGust": {"unitCode": "wmoUnit:km_h-1", "value": None},
            "windDirection": {"unitCode": "wmoUnit:degree_(angle)", "value": 120},
            "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 62.1},
            "textDescription": "Clear",
        }}
        obs = weather.normalize_observation(payload, station_id="KBCB")
        self.assertEqual(obs["kind"], "observation")
        self.assertEqual(obs["station_name"], "Virginia Tech Airport")
        self.assertAlmostEqual(obs["temperature_c"], 27.5, places=1)
        self.assertAlmostEqual(obs["temperature_f"], 81.5, places=1)
        self.assertAlmostEqual(obs["wind_speed_kph"], 13.0, places=1)
        self.assertAlmostEqual(obs["wind_speed_mph"], 8.1, places=1)
        self.assertIsNone(obs["wind_gust_kph"])
        self.assertIn("airport observation", obs["station_note"])

    def test_alert_normalization_preserves_times(self):
        payload = {"features": [alert_feature("urn:a", "Flood Warning", "Moderate")]}
        a = weather.normalize_alerts(payload)[0]
        self.assertEqual(a["kind"], "alert")
        self.assertEqual(a["severity"], "Moderate")
        self.assertEqual(a["onset"], "2026-09-19T10:00:00-04:00")

    def test_point_normalization(self):
        meta = weather.normalize_point(point_payload(), weather.DEFAULT_LAT,
                                       weather.DEFAULT_LON)
        self.assertEqual(meta["grid_id"], "RNK")
        self.assertEqual(meta["forecast_zone"], ZONE)
        self.assertEqual(meta["county_zone"], "VAC121")


# ------------------------------------------------------------- interval overlap
class TestOverlap(unittest.TestCase):
    def test_half_open_overlap(self):
        windows = [window(T0, T1), window(T1, T2), window(T2, T3)]
        got = weather.overlapping_windows(
            "2026-09-19T10:30:00-04:00", "2026-09-19T12:00:00-04:00", windows)
        self.assertEqual([w["start"] for w in got], [T0, T1])
        touching = weather.overlapping_windows(
            "2026-09-19T12:00:00-04:00", "2026-09-19T12:30:00-04:00", windows)
        self.assertEqual([w["start"] for w in touching], [T2])

    def test_naive_window_bounds_are_assumed_campus_local(self):
        got = weather.overlapping_windows(
            datetime(2026, 9, 19, 10, 30), datetime(2026, 9, 19, 10, 45),
            [window(T0, T1)])
        self.assertEqual(len(got), 1)


# ------------------------------------------------------------- risk / thresholds
class TestAssessLeg(unittest.TestCase):
    def test_precip_boundaries(self):
        cases = [(10, "none"), (20, "low"), (39, "low"),
                 (40, "moderate"), (59, "moderate"), (60, "high"), (95, "high")]
        for pct, expected in cases:
            with self.subTest(pct=pct):
                result = weather.assess_leg(
                    "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
                    windows=[window(T0, T1, precip_probability_pct=pct)])
                self.assertEqual(result["level"], expected)

    def test_thunderstorm_token_floors_at_high(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[window(T0, T1, precip_probability_pct=5,
                            short_forecast="Showers And Thunderstorms Likely")])
        self.assertEqual(result["level"], "high")
        self.assertTrue(any(e["factor"] == "thunderstorm" for e in result["evidence"]))

    def test_heat_cold_and_wind_bands(self):
        heat = weather.assess_leg(
            "2026-09-19T13:00:00-04:00", "2026-09-19T13:30:00-04:00",
            windows=[window(T3, "2026-09-19T14:00:00-04:00", temperature_c=40.0)])
        self.assertEqual(heat["level"], "severe")
        cold = weather.assess_leg(
            "2026-09-19T06:00:00-04:00", "2026-09-19T06:30:00-04:00",
            windows=[window("2026-09-19T06:00:00-04:00",
                            "2026-09-19T07:00:00-04:00", temperature_c=-12.0)])
        self.assertEqual(cold["level"], "high")
        wind = weather.assess_leg(
            "2026-09-19T10:00:00-04:00", "2026-09-19T10:30:00-04:00",
            windows=[window(T0, T1, wind_speed_kph=70.0)])
        self.assertEqual(wind["level"], "severe")

    def test_generated_wording_attributes_weather_to_the_forecast(self):
        result = weather.assess_leg(
            "2026-09-19T10:00:00-04:00", "2026-09-19T10:30:00-04:00",
            windows=[window(T0, T1, precip_probability_pct=70, wind_speed_kph=50)])
        text = " ".join(result["reasons"]).lower()
        self.assertIn("forecast", text)

    def test_thresholds_are_configurable_per_call(self):
        w = [window(T0, T1, precip_probability_pct=50)]
        default = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00", windows=w)
        self.assertEqual(default["level"], "moderate")
        stricter = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00", windows=w,
            thresholds={"precip_probability_pct": {"high": 45.0}})
        self.assertEqual(stricter["level"], "high")

    def test_mild_window_with_all_fields_is_none(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[window(T0, T1, precip_probability_pct=5,
                            temperature_c=20, wind_speed_kph=5)])
        self.assertEqual(result["level"], "none")
        self.assertEqual(result["status"], "ok")

    # -- missing data must never read as reassuring -------------------------
    def test_all_hazard_fields_missing_is_unknown(self):
        bare = {"kind": "forecast", "start": T0, "end": T1,
                "temperature_c": None, "precip_probability_pct": None,
                "wind_speed_kph": None, "short_forecast": None}
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[bare])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["level"], "unknown")

    def test_partial_missing_fields_with_mild_signal_is_unknown(self):
        partial = window(T0, T1, precip_probability_pct=5,
                         temperature_c=None, wind_speed_kph=None)
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[partial])
        self.assertEqual(result["level"], "unknown")
        self.assertIn("missing", result["reasons"][0].lower())

    def test_precip_token_with_null_pop_is_conservative(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[window(T0, T1, precip_probability_pct=None,
                            temperature_c=20, wind_speed_kph=5,
                            short_forecast="Chance Rain Showers")])
        self.assertEqual(result["level"], "low")
        self.assertTrue(any(e["factor"] == "precip_token" for e in result["evidence"]))

    def test_snow_token_with_null_pop_is_conservative(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[window(T0, T1, precip_probability_pct=None,
                            temperature_c=1, wind_speed_kph=5,
                            short_forecast="Snow Likely")])
        self.assertIn(result["level"], ("low", "moderate", "high"))
        self.assertTrue(any(e["factor"] == "precip_token" for e in result["evidence"]))

    def test_missing_precipitation_probability_with_other_fields_is_unknown(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[window(T0, T1, precip_probability_pct=None,
                            temperature_c=20, wind_speed_kph=5,
                            short_forecast="Sunny")])
        self.assertEqual(result["level"], "unknown")
        self.assertIn("precipitation", result["reasons"][0].lower())

    def test_no_overlapping_window_is_unknown_not_none(self):
        result = weather.assess_leg(
            "2026-09-20T10:00:00-04:00", "2026-09-20T11:00:00-04:00",
            windows=[window(T0, T1)])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["level"], "unknown")


class TestBasis(unittest.TestCase):
    def test_alert_only_basis_is_alert(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[window(T0, T1)], alerts=[alert()])
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["basis"], "alert")

    def test_forecast_only_basis_is_forecast(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[window(T0, T1, precip_probability_pct=80)], alerts=[])
        self.assertEqual(result["basis"], "forecast")

    def test_alert_and_forecast_is_mixed(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[window(T0, T1, precip_probability_pct=80)], alerts=[alert()])
        self.assertEqual(result["basis"], "mixed")


class TestAlertsPure(unittest.TestCase):
    def test_extreme_alert_is_severe_without_forecast_coverage(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[], alerts=[alert(severity="Extreme", event="Tornado Warning")])
        self.assertEqual(result["level"], "severe")
        self.assertEqual(result["basis"], "alert")

    def test_alert_outside_the_window_is_ignored(self):
        result = weather.assess_leg(
            "2026-09-19T08:00:00-04:00", "2026-09-19T09:00:00-04:00",
            windows=[window("2026-09-19T08:00:00-04:00",
                            "2026-09-19T09:00:00-04:00")],
            alerts=[alert()])
        self.assertEqual(result["level"], "none")

    def test_expired_alert_is_ignored(self):
        expired = alert(onset="2026-09-19T06:00:00-04:00",
                        effective="2026-09-19T06:00:00-04:00",
                        ends="2026-09-19T07:00:00-04:00", expires=None)
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[window(T0, T1)], alerts=[expired])
        self.assertEqual(result["level"], "none")

    def test_summarize_alerts_counts(self):
        counts = weather.summarize_alerts([alert(), alert(severity="Extreme")])
        self.assertEqual(counts["Severe"], 1)
        self.assertEqual(counts["Extreme"], 1)


class TestPlanRisk(unittest.TestCase):
    LEGS = [
        {"seq": 1, "type": "walk", "from": "A", "to": "B",
         "start_time": "2026-09-19T10:15:00-04:00", "minutes": 12},
        {"seq": 2, "type": "eat", "from": "B", "to": "B",
         "start_time": "2026-09-19T10:27:00-04:00", "minutes": 20},
        {"seq": 3, "type": "walk", "from": "B", "to": "C",
         "start_time": "2026-09-19T11:00:00-04:00", "minutes": 8},
    ]

    def test_outdoor_legs_scored_and_indoor_not_applicable(self):
        windows = [window(T0, T1, precip_probability_pct=70),
                   window(T1, T2, precip_probability_pct=5)]
        result = weather.plan_risk(self.LEGS, windows=windows, alerts=[])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["legs"][0]["level"], "high")
        self.assertEqual(result["legs"][1]["status"], "not_applicable")
        self.assertEqual(result["legs"][2]["level"], "none")
        self.assertEqual(result["level"], "high")
        self.assertFalse(result["stale"])

    def test_minutes_produce_the_leg_end(self):
        windows = [window(T0, "2026-09-19T10:30:00-04:00",
                          precip_probability_pct=65)]
        result = weather.plan_risk(self.LEGS[:1], windows=windows, alerts=[])
        self.assertEqual(result["legs"][0]["level"], "high")

    def test_missing_window_marks_the_leg_unknown(self):
        result = weather.plan_risk(self.LEGS[:1], windows=[], alerts=[])
        self.assertEqual(result["legs"][0]["level"], "unknown")
        self.assertEqual(result["level"], "unknown")


# ------------------------------------------------------------- temp-cache fetch
class TestCacheBackedFetchers(TempCacheCase):
    def test_hourly_windows_from_temp_cache(self):
        self.seed_full(hourly_periods=[period(T0, T1, precip=55), period(T1, T2)])
        fetched = weather.hourly_windows(now=STAMP_DT)
        self.assertEqual(fetched["status"], "ok")
        self.assertEqual(len(fetched["windows"]), 2)
        self.assertEqual(fetched["point"]["grid_id"], "RNK")
        self.assertEqual(set(fetched["sources"]), {"points", "hourly"})
        self.assertEqual(fetched["sources"]["hourly"]["status"], "ok")

    def test_stale_point_dependency_propagates(self):
        old = (STAMP_DT - timedelta(days=2)).isoformat(timespec="seconds")
        self.seed_full(point_age=old, hourly_periods=[period(T0, T1)])
        fetched = weather.hourly_windows(now=STAMP_DT)
        self.assertEqual(fetched["sources"]["points"]["status"], "stale")
        self.assertEqual(fetched["sources"]["hourly"]["status"], "ok")
        self.assertEqual(fetched["status"], "stale")
        self.assertTrue(fetched["stale"])
        self.assertTrue(fetched["windows"][0]["stale"])

    def test_forecast_strip_is_limited_and_fresh(self):
        periods = [period(f"2026-09-19T{10 + i:02d}:00:00-04:00",
                          f"2026-09-19T{11 + i:02d}:00:00-04:00")
                   for i in range(8)]
        self.seed_full(hourly_periods=periods)
        strip = weather.forecast_strip(hours=6, at=STAMP_DT.astimezone(NY),
                                       now=STAMP_DT)
        self.assertEqual(strip["status"], "ok")
        self.assertEqual(strip["hours"], 6)
        self.assertEqual(len(strip["windows"]), 6)
        self.assertFalse(strip["stale"])
        self.assertIn("National Weather Service", strip["attribution"])

    def test_zone_only_alert_is_included(self):
        self.seed_full(alerts_point={"features": []},
                       alerts_zone={"features": [alert_feature("urn:zone:1")]})
        result = weather.active_alerts(now=STAMP_DT)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["alerts"][0]["id"], "urn:zone:1")
        self.assertEqual(result["status"], "ok")

    def test_duplicate_alert_is_deduped_across_point_and_zone(self):
        dup = alert_feature("urn:same")
        self.seed_full(alerts_point={"features": [dup]},
                       alerts_zone={"features": [dup]})
        result = weather.active_alerts(now=STAMP_DT)
        self.assertEqual(result["count"], 1)

    def test_one_alert_feed_unavailable_is_partial_not_ok(self):
        self.seed_full(alerts_point={"features": []})   # zone fixture absent
        result = weather.active_alerts(now=STAMP_DT)
        self.assertEqual(result["sources"]["point"]["status"], "ok")
        self.assertEqual(result["sources"]["zone"]["status"], "unavailable")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["stale"])

    def test_both_alert_feeds_unavailable_is_unavailable(self):
        self.seed_full()   # point and zone alert fixtures absent
        result = weather.active_alerts(now=STAMP_DT)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["count"], 0)

    def test_latest_observation_picks_nearest_and_labels_airport(self):
        stations = stations_payload([
            ("KFAR", "Far Airport", 50000),
            ("KBCB", "Virginia Tech Airport", 1766),
        ])
        obs_payload = {"properties": {
            "station": "https://api.weather.gov/stations/KBCB",
            "timestamp": "2026-09-19T20:55:00+00:00",
            "temperature": {"unitCode": "wmoUnit:degC", "value": 27.5},
            "textDescription": "Clear",
        }}
        self.seed_full(stations=stations, observation=obs_payload)
        result = weather.latest_observation(now=STAMP_DT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observation"]["station_id"], "KBCB")
        self.assertEqual(result["observation"]["station_name"], "Virginia Tech Airport")
        self.assertIn("not an on-campus sensor", result["observation"]["station_note"])

    def test_plan_risk_status_and_sources_from_temp_cache(self):
        self.seed_full(hourly_periods=[period(T0, T1, precip=80)],
                       alerts_point={"features": []},
                       alerts_zone={"features": []})
        legs = [{"type": "walk", "start_time": T0, "minutes": 20}]
        result = weather.plan_risk(legs, now=STAMP_DT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(set(result["sources"]), {"forecast", "alerts"})
        self.assertFalse(result["stale"])
        self.assertEqual(result["legs"][0]["level"], "high")


def stations_from(payload):
    return [{"station_id": f["properties"]["stationIdentifier"],
             "distance_m": f["properties"]["distance"]["value"],
             "name": f["properties"]["name"]} for f in payload["features"]]


class TestNearestStation(unittest.TestCase):
    def test_picks_smallest_distance_regardless_of_order(self):
        stations = stations_from(stations_payload([
            ("KFAR", "Far", 50000), ("KBCB", "Near", 1766), ("KMID", "Mid", 20000)]))
        self.assertEqual(weather.nearest_station(stations)["station_id"], "KBCB")

    def test_none_distances_sort_last(self):
        stations = [{"station_id": "A", "distance_m": None},
                    {"station_id": "B", "distance_m": 5000}]
        self.assertEqual(weather.nearest_station(stations)["station_id"], "B")

    def test_empty_is_none(self):
        self.assertIsNone(weather.nearest_station([]))


# ------------------------------------------------------------- real replay
class TestReplayStoreHasNoWeatherFixtures(unittest.TestCase):
    """Deliberate decision: the frozen store carries NO weather data.

    The 2026-09-19 NWS capture was acquired ~6 h after the bus replay clock and
    its forecast did not cover the replay "now", so committing it (with a
    rewritten fetched_at) would have been an incoherent, falsified snapshot.
    Until a complete bundle is captured coherently, replay returns a typed
    unavailable state instead of showing stale-relative-to-replay weather.
    """

    def test_resolve_point_is_typed_unavailable_in_replay(self):
        result = weather.resolve_point()
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNotNone(result["reason"])

    def test_hourly_windows_empty_and_unavailable_in_replay(self):
        result = weather.hourly_windows()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["windows"], [])

    def test_as_failure_reports_unavailable(self):
        failure = weather.as_failure(weather.resolve_point())
        self.assertIsInstance(failure, weather.Failure)
        self.assertEqual(failure.status, "unavailable")

    def test_no_weather_fixture_files_are_committed(self):
        offenders = [p.name for p in Path(config.FIXTURES_DIR).glob("weather_*")]
        self.assertEqual(offenders, [])


# ------------------------------------------------------------- failure/staleness
class TestFreshnessAndFailure(TempCacheCase):
    def test_stale_age_uses_config_clock(self):
        self.seed_full(hourly_periods=[period(T0, T1)])
        fresh = weather.hourly_windows(now=STAMP_DT)
        self.assertFalse(fresh["stale"])
        old = weather.hourly_windows(now=STAMP_DT + timedelta(hours=2))
        self.assertTrue(old["stale"])
        self.assertEqual(old["status"], "stale")

    def test_missing_cache_entry_is_a_typed_unavailable_state(self):
        original = weather._load

        def boom(*args, **kwargs):
            raise weather.WeatherUnavailable("no fixture", source="test")

        weather._load = boom
        try:
            self.assertEqual(weather.resolve_point()["status"], "unavailable")
            hourly = weather.hourly_windows()
            self.assertEqual(hourly["status"], "unavailable")
            self.assertEqual(hourly["windows"], [])
            self.assertEqual(weather.latest_observation(station="KBCB")["status"],
                             "unavailable")
        finally:
            weather._load = original

    def test_cache_miss_type_is_available(self):
        self.assertTrue(issubclass(weather.WeatherUnavailable, weather.WeatherError))

    def test_as_failure_converts_only_non_ok_results(self):
        self.assertIsNone(weather.as_failure({"status": "ok"}))
        failure = weather.as_failure(
            {"status": "partial", "source": "s", "reason": "r",
             "fetched_at": STAMP, "age_seconds": 1.0})
        self.assertEqual(failure.status, "partial")
        self.assertEqual(failure.reason, "r")

    def test_failed_load_never_leaves_a_partial_payload(self):
        original = weather._load

        def boom(*args, **kwargs):
            raise weather.WeatherUnavailable("down", source="test")

        weather._load = boom
        try:
            alerts = weather.active_alerts(include_zone=False)
            self.assertEqual(alerts["status"], "unavailable")
            self.assertEqual(alerts["alerts"], [])
            self.assertEqual(alerts["count"], 0)
        finally:
            weather._load = original


class TestPublicCacheMetadata(TempCacheCase):
    def test_read_and_envelope_meta_roundtrip(self):
        self.seed("weather_points", point_payload(), POINT_PARAMS)
        env = cache.read_envelope("weather_points", POINT_PARAMS)
        self.assertIsNotNone(env)
        self.assertEqual(env["payload"], point_payload())
        meta = cache.envelope_meta("weather_points", POINT_PARAMS)
        self.assertEqual(meta["fetched_at"], STAMP)
        self.assertEqual(meta["key"], "weather_points__lat=37.2292__lon=-80.4240")
        self.assertNotIn("payload", meta)

    def test_read_envelope_missing_is_none(self):
        self.assertIsNone(cache.read_envelope("weather_nope", None))
        self.assertIsNone(cache.envelope_meta("weather_nope", None))

    def test_write_envelope_atomic_preserves_real_stamp(self):
        path = cache.write_envelope_atomic(
            "weather_probe", "https://e.test/x", {"ok": True},
            params={"k": "v"}, fetched_at="2020-01-01T00:00:00+00:00")
        self.assertTrue(path.exists())
        env = cache.read_envelope("weather_probe", {"k": "v"})
        self.assertEqual(env["fetched_at"], "2020-01-01T00:00:00+00:00")

    def test_publish_envelopes_all_or_nothing(self):
        entries = [
            {"name": "weather_a", "url": "u", "payload": {"n": 1}, "params": {"i": "1"},
             "fetched_at": STAMP},
            {"name": "weather_b", "url": "u", "payload": {"n": 2}, "params": {"i": "2"},
             "fetched_at": STAMP},
        ]
        published = cache.publish_envelopes(entries)
        self.assertEqual(published["count"], 2)
        self.assertEqual(len(published["files"]), 2)
        self.assertRegex(published["version"], r"^[0-9a-f]{16}$")
        self.assertIn("published_at", published)
        self.assertEqual(cache.read_envelope("weather_a", {"i": "1"})["payload"],
                         {"n": 1})
        self.assertEqual(cache.read_envelope("weather_b", {"i": "2"})["payload"],
                         {"n": 2})

    def test_publish_envelopes_failure_rolls_back(self):
        good = {"name": "weather_good", "url": "u", "payload": {"n": 1},
                "params": {"i": "1"}, "fetched_at": STAMP}
        cache.publish_envelopes([good])
        original = cache.read_envelope("weather_good", {"i": "1"})["payload"]
        # A non-JSON-serializable payload fails during staging, before any swap.
        bad = {"name": "weather_good", "url": "u", "payload": {"bad": object()},
               "params": {"i": "1"}, "fetched_at": STAMP}
        with self.assertRaises(TypeError):
            cache.publish_envelopes([bad, good])
        self.assertEqual(cache.read_envelope("weather_good", {"i": "1"})["payload"],
                         original)

    def test_publish_envelopes_rolls_back_on_swap_failure(self):
        keep = {"name": "weather_keep", "url": "u", "payload": {"n": 1},
                "params": {"i": "1"}, "fetched_at": STAMP}
        cache.publish_envelopes([keep])
        original = cache.read_envelope("weather_keep", {"i": "1"})["payload"]

        new_keep = {"name": "weather_keep", "url": "u", "payload": {"n": 2},
                    "params": {"i": "1"}, "fetched_at": STAMP}
        new_other = {"name": "weather_other", "url": "u", "payload": {"n": 3},
                     "params": {"i": "2"}, "fetched_at": STAMP}

        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            if str(dst).endswith(".json"):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated swap failure")
            return real_replace(src, dst)

        os.replace = flaky
        try:
            with self.assertRaises(OSError):
                cache.publish_envelopes([new_keep, new_other])
        finally:
            os.replace = real_replace

        # The previously published bundle is restored and the new entry is absent.
        self.assertEqual(cache.read_envelope("weather_keep", {"i": "1"})["payload"],
                         original)
        self.assertIsNone(cache.read_envelope("weather_other", {"i": "2"}))


class TestCacheKeys(unittest.TestCase):
    def test_keys_are_stable(self):
        self.assertEqual(
            cache.key("weather_points", POINT_PARAMS),
            "weather_points__lat=37.2292__lon=-80.4240")
        self.assertEqual(
            cache.key("weather_hourly", HOURLY_PARAMS),
            "weather_hourly__grid=RNK__x=57__y=65")
        self.assertEqual(
            cache.key("weather_alerts_zone", {"zone": ZONE}),
            "weather_alerts_zone__zone=VAZ014")


class TestTimezones(unittest.TestCase):
    def test_window_offset_is_preserved_verbatim(self):
        w = weather.normalize_hourly({"properties": {"periods": [period(T0, T1)]}})[0]
        self.assertEqual(w["start"], T0)
        self.assertEqual(w["end"], T1)
        self.assertEqual(w["timezone"], "America/New_York")

    def test_aware_utc_and_naive_local_describe_the_same_window(self):
        windows = [window(T0, T1, precip_probability_pct=75)]
        naive = weather.assess_leg(datetime(2026, 9, 19, 10, 15),
                                   datetime(2026, 9, 19, 10, 45), windows=windows)
        aware = weather.assess_leg(
            datetime(2026, 9, 19, 14, 15, tzinfo=timezone.utc),
            datetime(2026, 9, 19, 14, 45, tzinfo=timezone.utc), windows=windows)
        self.assertEqual(naive["level"], aware["level"])
        self.assertEqual(weather._parse_ts(naive["start"]),
                         weather._parse_ts(aware["start"]))


# ------------------------------------------------------------- aggregate rules
class TestAggregateSemantics(unittest.TestCase):
    def test_nested_partial_beats_stale(self):
        sources = {
            "forecast": {"status": "stale", "stale": True, "reason": "old"},
            "alerts": {"status": "partial", "stale": True, "reason": "zone down"},
        }
        status, stale, reason, _metas = weather._aggregate(sources, primary="forecast")
        self.assertEqual(status, "partial")
        self.assertTrue(stale)
        self.assertEqual(reason, "zone down")

    def test_primary_unavailable_forces_unavailable(self):
        sources = {
            "points": {"status": "ok", "stale": False},
            "hourly": {"status": "unavailable", "stale": True, "reason": "down"},
        }
        status, stale, reason, _metas = weather._aggregate(sources, primary="hourly")
        self.assertEqual(status, "unavailable")
        self.assertTrue(stale)
        self.assertEqual(reason, "down")


class TestPlanRiskPartialAlerts(TempCacheCase):
    def test_partial_alert_feed_propagates_to_plan_risk(self):
        # Point + hourly fresh; point alerts ok; zone alerts missing -> partial.
        self.seed_full(hourly_periods=[period(T0, T1, precip=5)],
                       alerts_point={"features": []})
        legs = [{"type": "walk", "start_time": T0, "minutes": 20}]
        result = weather.plan_risk(legs, now=STAMP_DT)
        self.assertEqual(result["sources"]["alerts"]["status"], "partial")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["stale"])
        self.assertIsNotNone(result["reason"])
        self.assertIn("forecast", result["sources"])


class TestMultiHourCompleteness(unittest.TestCase):
    """A mild complete hour must not mask an incomplete or token hour."""

    def test_incomplete_second_hour_makes_the_leg_unknown(self):
        complete = window(T0, T1, precip_probability_pct=5, temperature_c=20,
                          wind_speed_kph=5, short_forecast="Sunny")
        incomplete = window(T1, T2, precip_probability_pct=5, temperature_c=20,
                            wind_speed_kph=None, short_forecast="Sunny")
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T11:30:00-04:00",
            windows=[complete, incomplete])
        self.assertEqual(result["level"], "unknown")
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(any(e["factor"] == "data_gap" for e in result["evidence"]))

    def test_rain_token_hour_is_not_hidden_by_a_mild_hour(self):
        complete = window(T0, T1, precip_probability_pct=5, temperature_c=20,
                          wind_speed_kph=5, short_forecast="Sunny")
        token_hour = window(T1, T2, precip_probability_pct=None, temperature_c=20,
                            wind_speed_kph=5, short_forecast="Rain Likely")
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T11:30:00-04:00",
            windows=[complete, token_hour])
        self.assertEqual(result["level"], "low")
        self.assertTrue(any(e["factor"] == "precip_token" for e in result["evidence"]))

    def test_stronger_hour_keeps_its_level_but_records_the_gap(self):
        strong = window(T0, T1, precip_probability_pct=70, temperature_c=20,
                        wind_speed_kph=5, short_forecast="Showers")
        incomplete = window(T1, T2, precip_probability_pct=5, temperature_c=20,
                            wind_speed_kph=None, short_forecast="Sunny")
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T11:30:00-04:00",
            windows=[strong, incomplete])
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(any(e["factor"] == "data_gap" for e in result["evidence"]))


class TestActiveAlertsMetadataDependency(TempCacheCase):
    def test_metadata_unavailable_marks_zone_unavailable(self):
        # Point alerts ok, but no weather_points fixture -> zone cannot resolve.
        self.seed("weather_alerts_point", {"features": []}, POINT_PARAMS)
        result = weather.active_alerts(now=STAMP_DT)
        self.assertEqual(result["sources"]["point"]["status"], "ok")
        self.assertEqual(result["sources"]["zone"]["status"], "unavailable")
        self.assertEqual(result["status"], "partial")
        self.assertIn("point metadata", result["reason"])

    def test_metadata_and_point_alert_unavailable_is_unavailable(self):
        result = weather.active_alerts(now=STAMP_DT)
        self.assertEqual(result["status"], "unavailable")


class TestObservationAggregation(TempCacheCase):
    OBS = {"properties": {
        "station": "https://api.weather.gov/stations/KBCB",
        "timestamp": "2026-09-19T20:55:00+00:00",
        "temperature": {"unitCode": "wmoUnit:degC", "value": 27.5},
        "textDescription": "Clear"}}
    STATIONS = stations_payload([("KBCB", "Virginia Tech Airport", 1766)])

    def test_stale_point_dependency_propagates(self):
        old = (STAMP_DT - timedelta(days=2)).isoformat(timespec="seconds")
        self.seed("weather_points", point_payload(), POINT_PARAMS, fetched_at=old)
        self.seed("weather_stations", self.STATIONS, STATIONS_PARAMS)
        self.seed("weather_observation", self.OBS, {"station": "KBCB"})
        result = weather.latest_observation(now=STAMP_DT)
        self.assertEqual(set(result["sources"]),
                         {"points", "stations", "observation"})
        self.assertEqual(result["sources"]["points"]["status"], "stale")
        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["stale"])

    def test_missing_observation_is_unavailable_not_partial(self):
        self.seed_full(stations=self.STATIONS)   # point + stations fresh, no obs
        result = weather.latest_observation(now=STAMP_DT)
        self.assertEqual(result["sources"]["observation"]["status"], "unavailable")
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["stale"])

    def test_hourly_primary_unavailable_is_unavailable(self):
        self.seed("weather_points", point_payload(), POINT_PARAMS)  # fresh metadata
        result = weather.hourly_windows(now=STAMP_DT)
        self.assertEqual(result["sources"]["points"]["status"], "ok")
        self.assertEqual(result["sources"]["hourly"]["status"], "unavailable")
        self.assertEqual(result["status"], "unavailable")


class TestPublishManifest(TempCacheCase):
    def test_manifest_version_is_stable_for_same_bundle(self):
        entries = [{"name": "weather_m", "url": "u", "payload": {"n": 1},
                    "params": {"i": "1"}, "fetched_at": STAMP}]
        first = cache.publish_envelopes(entries)
        second = cache.publish_envelopes(entries)
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(first["count"], 1)


class TestFetchScriptHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = (Path(__file__).resolve().parent.parent
                / "scripts" / "fetch_weather.py")
        spec = importlib.util.spec_from_file_location("hokieday_fetch_weather", path)
        module = importlib.util.module_from_spec(spec)
        prior = os.environ.get("DEMO_MODE")
        os.environ["DEMO_MODE"] = "cache"
        try:
            spec.loader.exec_module(module)
        finally:
            if prior is None:
                os.environ.pop("DEMO_MODE", None)
            else:
                os.environ["DEMO_MODE"] = prior
        cls.mod = module

    def test_sort_stations_by_distance_before_trim(self):
        payload = stations_payload([
            ("KFAR", "Far", 50000), ("KBCB", "Near", 1766), ("KMID", "Mid", 20000)])
        sorted_payload = self.mod._sort_stations(payload)
        ids = [f["properties"]["stationIdentifier"]
               for f in sorted_payload["features"]]
        self.assertEqual(ids, ["KBCB", "KMID", "KFAR"])
        trimmed = self.mod._trim_stations(sorted_payload, 1)
        self.assertEqual(len(trimmed["features"]), 1)
        self.assertEqual(
            trimmed["features"][0]["properties"]["stationIdentifier"], "KBCB")

    def test_required_names_derived_from_point_metadata(self):
        entries = [{"name": "weather_points", "payload": point_payload()},
                   {"name": "weather_hourly"},
                   {"name": "weather_alerts_point"}]
        required = self.mod._required_names(entries)
        self.assertIn("weather_alerts_zone", required)
        self.assertIn("weather_stations", required)
        self.assertNotIn("weather_observation", required)
        entries.append({"name": "weather_stations"})
        self.assertIn("weather_observation", self.mod._required_names(entries))

    def test_validate_flags_missing_required_resources(self):
        point = {"name": "weather_points", "payload": point_payload(),
                 "fetched_at": STAMP}
        errors = self.mod._validate([point], datetime(2026, 9, 19, tzinfo=timezone.utc))
        self.assertTrue(any("weather_hourly" in e for e in errors))
        self.assertTrue(any("weather_alerts_zone" in e for e in errors))


# ------------------------------------------------------------- claim wording
CERTAINTY_WORDS = (
    "will ", "will,", "will.", "definitely", "guaranteed", "certainty",
    "certainly", "no doubt", "unconditional", "always", "never", "100%",
)


class TestProhibitedCertaintyWording(unittest.TestCase):
    SCENARIOS = [
        ([window(T0, T1, precip_probability_pct=90, short_forecast="Thunderstorms")],
         [alert()]),
        ([window(T0, T1, temperature_c=41.0)], []),
        ([window(T0, T1, temperature_c=-25.0)], []),
        ([window(T0, T1, wind_speed_kph=80.0)], []),
        ([], []),
    ]

    def test_generated_text_contains_no_certainty_words(self):
        for windows, alerts in self.SCENARIOS:
            result = weather.assess_leg(
                "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
                windows=windows, alerts=alerts)
            text = " ".join(result["reasons"]) + " " + result["summary"]
            for entry in result["evidence"]:
                text += " " + str(entry.get("detail", ""))
            lowered = text.lower()
            for word in CERTAINTY_WORDS:
                with self.subTest(word=word, text=text):
                    self.assertNotIn(word, lowered,
                                     f"prohibited certainty wording {word!r}: {text}")

    def test_summaries_use_risk_language(self):
        for summary in weather.SUMMARY_BY_LEVEL.values():
            lowered = summary.lower()
            self.assertNotIn("will", lowered)
            self.assertNotIn("certain", lowered)


if __name__ == "__main__":
    unittest.main()