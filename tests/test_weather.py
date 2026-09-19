"""Offline tests for hokieday.weather (stdlib unittest, DEMO_MODE=cache).

No network: parsing and risk functions are pure, and the cache-backed fetchers
read the frozen `fixtures/` weather envelopes captured from api.weather.gov on
2026-09-19. The stale/error tests monkeypatch `weather._load`, they never open a
socket.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from hokieday import cache, config, weather

NY = ZoneInfo("America/New_York")
STAMP = "2026-09-19T15:22:29+00:00"
STAMP_DT = datetime(2026, 9, 19, 15, 22, 29, tzinfo=timezone.utc)


def window(start: str, end: str, **overrides) -> dict:
    """A minimal normalized forecast window for pure risk tests."""
    base = {
        "kind": "forecast", "start": start, "end": end,
        "timezone": config.CAMPUS_TZ, "temperature_c": 20.0,
        "temperature_f": 68.0, "temperature_unit": "F",
        "precip_probability_pct": None, "relative_humidity_pct": None,
        "wind_speed_kph": None, "wind_speed_mph": None, "wind_speed_text": None,
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


# ------------------------------------------------------------- parsing / units
class TestNormalize(unittest.TestCase):
    def test_hourly_fahrenheit_to_celsius_both_kept(self):
        payload = {"properties": {"periods": [{
            "number": 1, "startTime": "2026-09-19T17:00:00-04:00",
            "endTime": "2026-09-19T18:00:00-04:00", "isDaytime": True,
            "temperature": 68, "temperatureUnit": "F",
            "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 55},
            "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 60},
            "windSpeed": "10 mph", "windDirection": "SW",
            "shortForecast": "Chance Showers",
        }]}}
        windows = weather.normalize_hourly(payload)
        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertAlmostEqual(w["temperature_c"], 20.0, places=1)
        self.assertEqual(w["temperature_f"], 68.0)
        self.assertEqual(w["temperature_unit"], "F")
        self.assertEqual(w["precip_probability_pct"], 55.0)
        self.assertAlmostEqual(w["wind_speed_kph"], 16.1, places=1)
        self.assertAlmostEqual(w["wind_speed_mph"], 10.0, places=1)
        self.assertEqual(w["wind_speed_text"], "10 mph")
        self.assertEqual(w["kind"], "forecast")

    def test_null_precip_is_none_not_zero(self):
        """A null probability must stay None; 0 would read as 'no chance'."""
        payload = {"properties": {"periods": [{
            "number": 1, "startTime": "2026-09-19T17:00:00-04:00",
            "endTime": "2026-09-19T18:00:00-04:00", "temperature": 70,
            "temperatureUnit": "F",
            "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": None},
        }]}}
        w = weather.normalize_hourly(payload)[0]
        self.assertIsNone(w["precip_probability_pct"])

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
        self.assertEqual(obs["station_id"], "KBCB")
        self.assertEqual(obs["station_name"], "Virginia Tech Airport")
        self.assertAlmostEqual(obs["temperature_c"], 27.5, places=1)
        self.assertAlmostEqual(obs["temperature_f"], 81.5, places=1)
        self.assertAlmostEqual(obs["wind_speed_kph"], 13.0, places=1)
        self.assertAlmostEqual(obs["wind_speed_mph"], 8.1, places=1)
        self.assertIsNone(obs["wind_gust_kph"])
        self.assertIn("airport observation", obs["station_note"])

    def test_alert_normalization_preserves_times(self):
        payload = {"features": [{"properties": {
            "id": "urn:test:1", "event": "Severe Thunderstorm Warning",
            "severity": "Severe", "certainty": "Likely", "urgency": "Immediate",
            "status": "Actual", "messageType": "Alert", "category": "Met",
            "response": "Shelter", "headline": "h", "description": "d",
            "instruction": "i", "areaDesc": "Montgomery",
            "senderName": "NWS Blacksburg VA",
            "effective": "2026-09-19T16:00:00-04:00",
            "onset": "2026-09-19T16:10:00-04:00",
            "expires": "2026-09-19T18:00:00-04:00", "ends": None,
        }}]}
        alerts = weather.normalize_alerts(payload)
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a["kind"], "alert")
        self.assertEqual(a["severity"], "Severe")
        self.assertEqual(a["onset"], "2026-09-19T16:10:00-04:00")
        self.assertEqual(a["expires"], "2026-09-19T18:00:00-04:00")

    def test_point_normalization(self):
        payload = {"properties": {
            "gridId": "RNK", "gridX": 57, "gridY": 65,
            "forecastZone": "https://api.weather.gov/zones/forecast/VAZ014",
            "county": "https://api.weather.gov/zones/county/VAC121",
            "timeZone": "America/New_York", "forecastHourly": "https://x/hourly",
            "observationStations": "https://x/stations", "radarStation": "KFCX",
        }}
        meta = weather.normalize_point(payload, weather.DEFAULT_LAT, weather.DEFAULT_LON)
        self.assertEqual(meta["grid_id"], "RNK")
        self.assertEqual(meta["forecast_zone"], "VAZ014")
        self.assertEqual(meta["county_zone"], "VAC121")
        self.assertEqual(meta["timezone"], "America/New_York")


# ------------------------------------------------------------- interval overlap
class TestOverlap(unittest.TestCase):
    def test_half_open_overlap(self):
        windows = [
            window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00"),
            window("2026-09-19T11:00:00-04:00", "2026-09-19T12:00:00-04:00"),
            window("2026-09-19T12:00:00-04:00", "2026-09-19T13:00:00-04:00"),
        ]
        got = weather.overlapping_windows(
            "2026-09-19T10:30:00-04:00", "2026-09-19T12:00:00-04:00", windows)
        self.assertEqual([w["start"] for w in got], [
            "2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00"])
        # touching at the boundary is NOT an overlap
        touching = weather.overlapping_windows(
            "2026-09-19T12:00:00-04:00", "2026-09-19T12:30:00-04:00", windows)
        self.assertEqual([w["start"] for w in touching], [
            "2026-09-19T12:00:00-04:00"])

    def test_naive_window_bounds_are_assumed_campus_local(self):
        windows = [window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00")]
        got = weather.overlapping_windows(
            datetime(2026, 9, 19, 10, 30), datetime(2026, 9, 19, 10, 45), windows)
        self.assertEqual(len(got), 1)


# ------------------------------------------------------------- risk / thresholds
class TestAssessLeg(unittest.TestCase):
    def test_precip_boundaries(self):
        bands = weather.DEFAULT_THRESHOLDS["precip_probability_pct"]
        self.assertEqual(bands, {"low": 20.0, "moderate": 40.0, "high": 60.0})
        cases = [(10, "none"), (20, "low"), (39, "low"),
                 (40, "moderate"), (59, "moderate"), (60, "high"), (95, "high")]
        for pct, expected in cases:
            with self.subTest(pct=pct):
                w = [window("2026-09-19T10:00:00-04:00",
                            "2026-09-19T11:00:00-04:00",
                            precip_probability_pct=pct)]
                result = weather.assess_leg(
                    "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
                    windows=w)
                self.assertEqual(result["level"], expected)

    def test_thunderstorm_token_floors_at_high(self):
        w = [window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00",
                    precip_probability_pct=5,
                    short_forecast="Showers And Thunderstorms Likely")]
        result = weather.assess_leg("2026-09-19T10:15:00-04:00",
                                    "2026-09-19T10:45:00-04:00", windows=w)
        self.assertEqual(result["level"], "high")
        self.assertTrue(any(e["factor"] == "thunderstorm" for e in result["evidence"]))

    def test_heat_cold_and_wind_bands(self):
        w_heat = [window("2026-09-19T13:00:00-04:00", "2026-09-19T14:00:00-04:00",
                         temperature_c=40.0)]
        self.assertEqual(weather.assess_leg(
            "2026-09-19T13:00:00-04:00", "2026-09-19T13:30:00-04:00",
            windows=w_heat)["level"], "severe")
        w_cold = [window("2026-09-19T06:00:00-04:00", "2026-09-19T07:00:00-04:00",
                         temperature_c=-12.0)]
        result = weather.assess_leg("2026-09-19T06:00:00-04:00",
                                    "2026-09-19T06:30:00-04:00", windows=w_cold)
        self.assertEqual(result["level"], "high")
        self.assertTrue(any(e["factor"] == "cold" for e in result["evidence"]))
        w_wind = [window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00",
                         wind_speed_kph=70.0)]
        self.assertEqual(weather.assess_leg(
            "2026-09-19T10:00:00-04:00", "2026-09-19T10:30:00-04:00",
            windows=w_wind)["level"], "severe")

    def test_thresholds_are_configurable_per_call(self):
        w = [window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00",
                    precip_probability_pct=50)]
        default = weather.assess_leg("2026-09-19T10:15:00-04:00",
                                     "2026-09-19T10:45:00-04:00", windows=w)
        self.assertEqual(default["level"], "moderate")
        stricter = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00", windows=w,
            thresholds={"precip_probability_pct": {"high": 45.0}})
        self.assertEqual(stricter["level"], "high")

    def test_no_overlapping_window_is_unknown_not_none(self):
        result = weather.assess_leg("2026-09-20T10:00:00-04:00",
                                    "2026-09-20T11:00:00-04:00",
                                    windows=[window("2026-09-19T10:00:00-04:00",
                                                    "2026-09-19T11:00:00-04:00")])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["level"], "unknown")

    def test_evidence_leaves_the_decision_to_the_student(self):
        w = [window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00",
                    precip_probability_pct=80)]
        result = weather.assess_leg("2026-09-19T10:15:00-04:00",
                                    "2026-09-19T10:45:00-04:00", windows=w)
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["basis"], "forecast")
        self.assertIn("precip_probability_pct", result["thresholds"])
        self.assertEqual(result["thresholds"]["precip_probability_pct"]["high"], 60.0)


class TestAlerts(unittest.TestCase):
    def test_severe_thunderstorm_warning_is_high(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[window("2026-09-19T10:00:00-04:00",
                            "2026-09-19T11:00:00-04:00")],
            alerts=[alert()])
        self.assertEqual(result["level"], "high")
        self.assertTrue(any(e["factor"] == "alert" for e in result["evidence"]))

    def test_extreme_alert_is_severe(self):
        result = weather.assess_leg(
            "2026-09-19T10:15:00-04:00", "2026-09-19T10:45:00-04:00",
            windows=[], alerts=[alert(severity="Extreme", event="Tornado Warning")])
        self.assertEqual(result["level"], "severe")

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
            windows=[window("2026-09-19T10:00:00-04:00",
                            "2026-09-19T11:00:00-04:00")],
            alerts=[expired])
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
        windows = [
            window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00",
                   precip_probability_pct=70),
            window("2026-09-19T11:00:00-04:00", "2026-09-19T12:00:00-04:00",
                   precip_probability_pct=5),
        ]
        result = weather.plan_risk(self.LEGS, windows=windows, alerts=[])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["legs"][0]["level"], "high")
        self.assertEqual(result["legs"][0]["status"], "ok")
        self.assertEqual(result["legs"][1]["status"], "not_applicable")
        self.assertEqual(result["legs"][2]["level"], "none")
        self.assertEqual(result["level"], "high")

    def test_minutes_produce_the_leg_end(self):
        windows = [window("2026-09-19T10:00:00-04:00",
                          "2026-09-19T10:30:00-04:00", precip_probability_pct=65)]
        result = weather.plan_risk(self.LEGS[:1], windows=windows, alerts=[])
        self.assertEqual(result["legs"][0]["level"], "high")

    def test_missing_window_marks_the_leg_unknown(self):
        result = weather.plan_risk(self.LEGS[:1], windows=[], alerts=[])
        self.assertEqual(result["legs"][0]["level"], "unknown")
        self.assertEqual(result["level"], "unknown")


# ------------------------------------------------------------- cache / failure
class TestCacheBackedFetchers(unittest.TestCase):
    """Reads the committed real NWS fixtures; no network."""

    def test_resolved_grid_zone_station_from_fixture(self):
        point = weather.resolve_point()
        self.assertEqual(point["status"], "ok")
        meta = point["point"]
        self.assertEqual((meta["grid_id"], meta["grid_x"], meta["grid_y"]),
                         ("RNK", 57, 65))
        self.assertEqual(meta["forecast_zone"], "VAZ014")
        self.assertEqual(meta["timezone"], "America/New_York")

    def test_hourly_windows_from_fixture(self):
        fetched = weather.hourly_windows()
        self.assertEqual(fetched["status"], "ok")
        self.assertGreater(len(fetched["windows"]), 0)
        self.assertEqual(fetched["windows"][0]["kind"], "forecast")
        self.assertEqual(fetched["point"]["grid_id"], "RNK")

    def test_observation_is_labelled_as_airport(self):
        obs = weather.latest_observation()
        self.assertEqual(obs["status"], "ok")
        self.assertEqual(obs["observation"]["station_id"], "KBCB")
        self.assertEqual(obs["observation"]["station_name"], "Virginia Tech Airport")
        self.assertIn("not an on-campus sensor", obs["observation"]["station_note"])

    def test_empty_alerts_is_a_normal_ok_state(self):
        alerts = weather.active_alerts()
        self.assertEqual(alerts["status"], "ok")
        self.assertEqual(alerts["alerts"], [])
        self.assertEqual(alerts["count"], 0)

    def test_forecast_strip_is_limited_and_fresh(self):
        strip = weather.forecast_strip(hours=6, at=STAMP_DT.astimezone(NY))
        self.assertEqual(strip["status"], "ok")
        self.assertEqual(strip["hours"], 6)
        self.assertEqual(len(strip["windows"]), 6)
        self.assertFalse(strip["stale"])
        self.assertIn("National Weather Service", strip["attribution"])


class TestFreshnessAndFailure(unittest.TestCase):
    def test_stale_age_uses_config_clock_not_wall_clock(self):
        """A future replay 'now' must flip the label; the fixture is 15:22 UTC."""
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
            point = weather.resolve_point()
            self.assertEqual(point["status"], "unavailable")
            self.assertIn("no fixture", point["reason"])
            hourly = weather.hourly_windows()
            self.assertEqual(hourly["status"], "unavailable")
            self.assertEqual(hourly["windows"], [])
            obs = weather.latest_observation(station="KBCB")
            self.assertEqual(obs["status"], "unavailable")
        finally:
            weather._load = original

    def test_cache_miss_type_is_available(self):
        self.assertTrue(issubclass(weather.WeatherUnavailable, weather.WeatherError))

    def test_as_failure_converts_only_non_ok_results(self):
        ok = weather.hourly_windows()
        self.assertIsNone(weather.as_failure(ok))
        original = weather._load

        def boom(*args, **kwargs):
            raise weather.WeatherUnavailable("down", source="test")

        weather._load = boom
        try:
            failed = weather.resolve_point()
        finally:
            weather._load = original
        failure = weather.as_failure(failed)
        self.assertIsInstance(failure, weather.Failure)
        self.assertEqual(failure.status, "unavailable")
        self.assertIn("down", failure.reason)

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


class TestCacheKeys(unittest.TestCase):
    def test_point_and_hourly_keys_are_stable(self):
        self.assertEqual(
            cache.key("weather_points", {"lat": "37.2292", "lon": "-80.4240"}),
            "weather_points__lat=37.2292__lon=-80.4240")
        self.assertEqual(
            cache.key("weather_hourly", {"grid": "RNK", "x": "57", "y": "65"}),
            "weather_hourly__grid=RNK__x=57__y=65")
        self.assertEqual(
            cache.key("weather_alerts_zone", {"zone": "VAZ014"}),
            "weather_alerts_zone__zone=VAZ014")

    def test_fixtures_are_present_and_named_by_those_keys(self):
        for name, params in [
            ("weather_points", {"lat": "37.2292", "lon": "-80.4240"}),
            ("weather_hourly", {"grid": "RNK", "x": "57", "y": "65"}),
            ("weather_alerts_point", {"lat": "37.2292", "lon": "-80.4240"}),
            ("weather_alerts_zone", {"zone": "VAZ014"}),
            ("weather_stations", {"grid": "RNK", "x": "57", "y": "65"}),
            ("weather_observation", {"station": "KBCB"}),
        ]:
            with self.subTest(name=name, params=params):
                self.assertTrue(cache.has(name, params),
                                f"missing fixture {cache.key(name, params)}")


class TestTimezones(unittest.TestCase):
    def test_window_offset_is_preserved_verbatim(self):
        payload = {"properties": {"periods": [{
            "number": 1, "startTime": "2026-09-19T17:00:00-04:00",
            "endTime": "2026-09-19T18:00:00-04:00", "temperature": 70,
            "temperatureUnit": "F",
        }]}}
        w = weather.normalize_hourly(payload)[0]
        self.assertEqual(w["start"], "2026-09-19T17:00:00-04:00")
        self.assertEqual(w["end"], "2026-09-19T18:00:00-04:00")
        self.assertEqual(w["timezone"], "America/New_York")

    def test_aware_utc_and_naive_local_describe_the_same_window(self):
        windows = [window("2026-09-19T10:00:00-04:00",
                          "2026-09-19T11:00:00-04:00",
                          precip_probability_pct=75)]
        naive = weather.assess_leg(datetime(2026, 9, 19, 10, 15),
                                   datetime(2026, 9, 19, 10, 45), windows=windows)
        aware = weather.assess_leg(
            datetime(2026, 9, 19, 14, 15, tzinfo=timezone.utc),
            datetime(2026, 9, 19, 14, 45, tzinfo=timezone.utc), windows=windows)
        self.assertEqual(naive["level"], aware["level"])
        self.assertEqual(weather._parse_ts(naive["start"]),
                         weather._parse_ts(aware["start"]))


# ------------------------------------------------------------- claim wording
CERTAINTY_WORDS = (
    "will ", "will,", "will.", "definitely", "guaranteed", "certainty",
    "certainly", "no doubt", "unconditional", "always", "never", "100%",
)


class TestProhibitedCertaintyWording(unittest.TestCase):
    """The module must present evidence, never promise an outcome."""

    SCENARIOS = [
        ([window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00",
                 precip_probability_pct=90,
                 short_forecast="Thunderstorms")], [alert()]),
        ([window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00",
                 temperature_c=41.0)], []),
        ([window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00",
                 temperature_c=-25.0)], []),
        ([window("2026-09-19T10:00:00-04:00", "2026-09-19T11:00:00-04:00",
                 wind_speed_kph=80.0)], []),
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
        for level, summary in weather.SUMMARY_BY_LEVEL.items():
            lowered = summary.lower()
            self.assertNotIn("will", lowered)
            self.assertNotIn("certain", lowered)


if __name__ == "__main__":
    unittest.main()