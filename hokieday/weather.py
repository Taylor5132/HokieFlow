"""Blacksburg-campus weather from the keyless National Weather Service API.

Contract
--------
This module turns NWS JSON into deterministic, unit-carrying, provenance-tagged
plain dicts and a risk assessment for a route leg's time window. It NEVER makes
a decision for the student and never implies certainty: the risk function
returns `evidence` + `reasons` + a `level`, and every generated sentence is
phrased as a risk, not a prediction of fact.

Authoritative point
-------------------
`DEFAULT_LAT`/`DEFAULT_LON` are the official VT GIS Burruss Hall centroid
(37.22924778, -80.42396247). The grid is NOT hard-coded: `resolve_point()` calls
`/points/{lat},{lon}` and reads back `gridId`/`gridX`/`gridY`, the forecast zone
and the station list. The live capture on 2026-09-19 resolved to:
    RNK grid 57,65  ·  forecast zone VAZ014  ·  timezone America/New_York
    nearest observation station KBCB ("Virginia Tech Airport", ~1.8 km away)

KBCB is an AIRPORT station, not an on-campus sensor. The normalized observation
says so explicitly (`station_note`) so the UI can label it honestly.

Data flow (all HTTP through hokieday.cache; this file imports no urllib)
-----------------------------------------------------------------------
    resolve_point()        /points/{lat},{lon}                 TTL 24 h   key weather_points
    hourly_windows()       forecastHourly                      TTL 25 m   key weather_hourly
    active_alerts()        /alerts/active?point=...&zone=...   TTL  3 m   keys weather_alerts_*
    observation_stations() /gridpoints/{g}/{x},{y}/stations    TTL 24 h   key weather_stations
    latest_observation()   /stations/{id}/observations/latest  TTL 10 m   key weather_observation

The cache layer already gives stale-on-failure fallback. This module adds the
*freshness label* (`status`, `stale`, `age_seconds`) measured against
`config.now()` -- the replay clock -- NOT `cache.age_seconds()` (real elapsed
time), so a replayed demo is self-consistent. There is no `datetime.now()` here
(tests/test_integration.py enforces that with an AST tripwire).

Units / timezones
-----------------
NWS sends mixed units (hourly in degF + mph, gridpoints in degC + km/h). Every
normalized value carries BOTH the source reading and a converted pair, plus the
raw source text/unit, so nothing is lost:
    temperature_c / temperature_f / temperature_unit
    wind_speed_kph / wind_speed_mph / wind_speed_text
Timestamps keep the NWS ISO-8601 offset verbatim, and a `timezone` field names
the campus IANA zone.

Attribution
-----------
The NWS asks for attribution; `ATTRIBUTION` is attached to every API-ready
result. Weather data from NWS is public domain (US Government work), but the
attribution is still surfaced per the NWS api.weather.gov "please credit"
guidance.

Thresholds
----------
`DEFAULT_THRESHOLDS` is declared and every function that scores accepts an
override dict, merged per section. Tests assert each boundary.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import cache, config

# ------------------------------------------------------------------ constants
NWS_API_BASE = "https://api.weather.gov"

# Official VT GIS Burruss Hall centroid (Blacksburg campus).
DEFAULT_LAT = 37.22924778
DEFAULT_LON = -80.42396247

ATTRIBUTION = "Weather data from the National Weather Service (weather.gov)"

_CAMPUS_TZ = ZoneInfo(config.CAMPUS_TZ)

# Cache lifetimes (seconds), chosen to the source's own update cadence.
POINTS_TTL_S = 24 * 3600        # grid metadata is stable; NWS caches ~24h
HOURLY_TTL_S = 25 * 60          # hourly forecast refreshes about hourly
ALERTS_TTL_S = 3 * 60           # alerts are safety-relevant: short window
OBSERVATION_TTL_S = 10 * 60     # ASOS/METAR reports ~every 20-60 min
STATIONS_TTL_S = 24 * 3600

# Risk levels, ordered. `none` < `low` < `moderate` < `high` < `severe`.
LEVELS = ("none", "low", "moderate", "high", "severe")
_LEVEL_RANK = {level: i for i, level in enumerate(LEVELS)}
LEVEL_LABELS = {
    "none": "None", "low": "Low", "moderate": "Moderate",
    "high": "High", "severe": "Severe", "unknown": "Unknown",
}

# Leg types that are exposed to the sky. Everything else (eating indoors, a bus
# ride) is deliberately NOT scored.
OUTDOOR_LEG_TYPES = ("walk", "bike", "scooter", "wait", "outdoor")

# Alert events that alone deserve at least a "high" badge, regardless of the
# severity field (NWS sometimes labels a Severe Thunderstorm Warning only
# "Severe", which is already high -- this keeps the mapping explicit).
SEVERE_ALERT_EVENTS = (
    "severe thunderstorm warning",
    "tornado warning",
    "flash flood warning",
    "winter storm warning",
    "extreme wind warning",
    "ice storm warning",
    "blizzard warning",
)

_THUNDER_RE = re.compile(r"thunder|tstm|t-storm|lightning|thunderstorm", re.I)
# Tokens that mean precipitation is in the forecast even when the probability
# field is null. They must not silently fall through to a reassuring "none".
_PRECIP_TOKEN_RE = re.compile(
    r"rain|shower|snow|sleet|drizzle|freezing|ice|wintry|precip", re.I)
_WIND_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mph|km/?h|kph|kt|knots)?", re.I)

DEFAULT_THRESHOLDS: dict[str, Any] = {
    # probability of precipitation, percent (hourly forecast)
    "precip_probability_pct": {"low": 20.0, "moderate": 40.0, "high": 60.0},
    # thunderstorm token in shortForecast -> at least "high"
    "thunderstorm_level": "high",
    # temperature, degrees Celsius
    "temperature_high_c": {"moderate": 32.0, "high": 35.0, "severe": 39.0},
    "temperature_low_c": {"moderate": 0.0, "high": -10.0, "severe": -18.0},
    # sustained wind, km/h
    "wind_kph": {"moderate": 30.0, "high": 45.0, "severe": 65.0},
    # NWS alert severity -> risk level
    "alert_severity": {
        "Extreme": "severe", "Severe": "high", "Moderate": "moderate",
        "Minor": "low", "Unknown": "low",
    },
    # a "warning"-class event floors at this level even if severity is lower
    "severe_alert_event_level": "high",
}

# What the NWS calls degree values / where we relabel a station. KBCB is the
# nearest station to campus but it is the Virginia Tech Airport ASOS -- an
# airport sensor, not an on-campus weather station.
STATION_LABELS = {
    "KBCB": "Virginia Tech Airport",
    "KFCX": "Blacksburg Radar (NWS)",
}
KBCB_NOTE = (
    "KBCB is the Virginia Tech Airport station, about 1.8 km from the Burruss "
    "centroid; it is an airport observation, not an on-campus sensor."
)

SUMMARY_BY_LEVEL = {
    "severe": ("Severe weather is possible in this window; consider an indoor "
               "alternative or a later time."),
    "high": "Weather risk is high for this window; have a backup plan.",
    "moderate": "Weather may affect this window; allow extra time.",
    "low": "A minor weather inconvenience is possible in this window.",
    "none": "No notable weather risk was found in this window.",
    "unknown": "No forecast covers this window, so weather risk is unknown.",
}


# ------------------------------------------------------------------ failures
class WeatherError(RuntimeError):
    """Base class for typed weather failures."""


class WeatherUnavailable(WeatherError):
    """Upstream (and cache) could not supply data; no stale copy exists.

    Carries `source` so the caller can say which feed failed.
    """

    def __init__(self, message: str, *, source: str = "NWS"):
        super().__init__(message)
        self.source = source


@dataclass(frozen=True)
class Failure:
    """Typed failure state returned by API-ready functions.

    `status` is a closed set: "ok", "stale", "partial", "unavailable".
    "unknown" is used by the pure risk function when a window cannot be scored.
    """

    status: str
    source: str
    reason: str
    fetched_at: str | None = None
    age_seconds: float | None = None


def as_failure(result: dict) -> Failure | None:
    """Convert an API-ready result to a typed Failure, or None when status is ok.

    This is the single place consumers turn the plain-dict contract into a typed
    state (for UI badges / retry logic), so the closed status set is defined
    once.
    """
    if not isinstance(result, dict):
        return None
    status = str(result.get("status") or "unknown")
    if status == "ok":
        return None
    return Failure(
        status=status,
        source=str(result.get("source") or ""),
        reason=str(result.get("reason") or ""),
        fetched_at=result.get("fetched_at"),
        age_seconds=result.get("age_seconds"),
    )


# ------------------------------------------------------------------ small utils
def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _c_to_f(c: float | None) -> float | None:
    return None if c is None else c * 9.0 / 5.0 + 32.0


def _f_to_c(f: float | None) -> float | None:
    return None if f is None else (f - 32.0) * 5.0 / 9.0


def _kmh_to_mph(kmh: float | None) -> float | None:
    return None if kmh is None else kmh * 0.621371


def _parse_ts(value) -> datetime | None:
    """Parse a NWS timestamp to an aware datetime. Naive is assumed campus-local."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_CAMPUS_TZ)
    return dt


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat(timespec="seconds")


def _temp_to_c(value, unit) -> float | None:
    value = _num(value)
    if value is None:
        return None
    u = str(unit or "").upper()
    if u.endswith("F") or "FAHRENHEIT" in u:
        return _f_to_c(value)
    if u.endswith("C") or "CELSIUS" in u:
        return value
    return None


def _speed_to_kph(value, unit) -> float | None:
    value = _num(value)
    if value is None:
        return None
    u = str(unit or "").lower()
    if "km_h" in u or "km/h" in u or "kph" in u:
        return value
    if "m_s" in u or "m/s" in u:
        return value * 3.6
    if "mi_h" in u or "mph" in u:
        return value * 1.609344
    if "kt" in u or "knot" in u:
        return value * 1.852
    return None


def _parse_wind_text(text) -> tuple[float | None, float | None, str | None]:
    """'3 mph' / '10 to 15 mph' / '25-45 mph' -> (kph, mph, original text).

    Ranges are read CONSERVATIVELY: the highest number in the text is used, so
    "25 to 45 mph" is scored as 45 mph. The unit is taken from the last token
    that carries one ("10 to 15 mph" -> mph).
    """
    if text is None:
        return None, None, None
    raw = str(text)
    matches = list(_WIND_RE.finditer(raw))
    if not matches:
        return None, None, raw
    values = [float(m.group(1)) for m in matches]
    unit = "mph"
    for m in matches:
        if m.group(2):
            unit = m.group(2).lower()
    val = max(values)                       # top of any range, never the bottom
    if unit in ("km/h", "kmh", "kph"):
        kph = val
    elif unit in ("kt", "knots"):
        kph = val * 1.852
    else:
        kph = val * 1.609344
    return kph, kph * 0.621371, raw


def _coord_params(lat: float, lon: float) -> dict:
    return {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"}


def _point_url(lat: float, lon: float) -> str:
    return config.ENDPOINTS["weather_points"].format(lat=f"{lat:.4f}", lon=f"{lon:.4f}")


def _envelope_fetched_at(name: str, params: dict | None = None) -> str | None:
    # Public cache metadata API only -- no private cache helpers here.
    meta = cache.envelope_meta(name, params)
    return meta.get("fetched_at") if meta else None


def _freshness(name: str, params: dict | None, max_age_s: float,
               now: datetime | None = None) -> dict:
    stamp = _envelope_fetched_at(name, params)
    ts = _parse_ts(stamp)
    age = None
    if ts is not None:
        ref = now if now is not None else config.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        age = (ref.astimezone(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
    return {
        "fetched_at": stamp,
        "age_seconds": _round(age, 1) if age is not None else None,
        "stale": age is None or age > max_age_s,
    }


def _base_result(name: str, params: dict | None, max_age_s: float, *,
                 source: str, now: datetime | None = None,
                 status: str | None = None, reason: str | None = None) -> dict:
    fresh = _freshness(name, params, max_age_s, now=now)
    resolved_status = status or ("stale" if fresh["stale"] else "ok")
    return {
        "status": resolved_status,
        "source": source,
        "attribution": ATTRIBUTION,
        "fetched_at": fresh["fetched_at"],
        "age_seconds": fresh["age_seconds"],
        "stale": bool(fresh["stale"]),
        "reason": reason,
    }


AGGREGATE_STATUSES = ("ok", "stale", "partial", "unavailable")


def _source_meta(result: dict) -> dict:
    """Compact per-source status for aggregate results (public provenance)."""
    if not isinstance(result, dict):
        return {"status": "unavailable", "stale": True, "fetched_at": None,
                "age_seconds": None, "reason": "no result"}
    return {
        "status": result.get("status") or "unavailable",
        "stale": bool(result.get("stale")),
        "fetched_at": result.get("fetched_at"),
        "age_seconds": result.get("age_seconds"),
        "reason": result.get("reason"),
    }


def _aggregate(sources: dict[str, dict], *, primary: str | None = None,
               default_reason: str | None = None) -> tuple[str, bool, str | None, dict]:
    """Aggregate per-source statuses truthfully, so they cannot contradict.

    Precedence: primary-unavailable > all-unavailable > any unavailable OR any
    nested partial > stale > ok. A nested `partial` (e.g. plan_risk's alert
    feed) therefore survives instead of being masked by a stale sibling.
    `stale` is True when any source is stale or unavailable, so a status of
    "ok" can never carry stale=True.

    `primary` names the feed the result cannot exist without (hourly forecast,
    station list, observation). If that feed is unavailable the whole result is
    unavailable, even when fresh metadata is available.
    """
    metas = {name: _source_meta(res) for name, res in sources.items()}
    statuses = [m["status"] for m in metas.values()]
    primary_status = metas.get(primary, {}).get("status") if primary else None
    if not statuses:
        status = "unavailable"
    elif primary_status == "unavailable":
        status = "unavailable"
    elif all(s == "unavailable" for s in statuses):
        status = "unavailable"
    elif any(s in ("unavailable", "partial") for s in statuses):
        status = "partial"
    elif any(s == "stale" for s in statuses):
        status = "stale"
    else:
        status = "ok"
    stale = any(m["stale"] or m["status"] == "unavailable" for m in metas.values())
    reason = None
    if status != "ok":
        if primary_status == "unavailable" and metas.get(primary, {}).get("reason"):
            reason = metas[primary]["reason"]
        else:
            if status == "partial":
                candidates = [m for m in metas.values()
                              if m["status"] in ("unavailable", "partial")
                              and m["reason"]]
            else:
                candidates = [m for m in metas.values() if m["reason"]]
            if candidates:
                reason = candidates[0]["reason"]
        if reason is None:
            reason = default_reason or {
                "stale": ("one or more weather sources are older than their "
                          "freshness window"),
                "partial": "one or more weather sources are unavailable",
                "unavailable": "no weather source is available",
            }.get(status)
    return status, stale, reason, metas


def _load(name: str, url: str, params: dict | None, max_age_s: float,
          force: bool) -> Any:
    """Fetch through the cache layer, translating failures into WeatherUnavailable."""
    try:
        return cache.get_json(name, url, params=params, max_age_s=max_age_s,
                              force=force)
    except cache.CacheMiss as exc:
        raise WeatherUnavailable(str(exc), source=url) from exc
    except cache.CacheRefreshError as exc:
        raise WeatherUnavailable(str(exc), source=url) from exc
    except Exception as exc:                                   # noqa: BLE001
        raise WeatherUnavailable(
            f"{type(exc).__name__}: {exc}", source=url) from exc


# ------------------------------------------------------------------ normalize
def normalize_point(payload: dict, lat: float, lon: float) -> dict:
    """`/points` response -> point metadata (grid + URLs + zones)."""
    props = (payload or {}).get("properties") or {}
    return {
        "kind": "point_metadata",
        "lat": lat,
        "lon": lon,
        "grid_id": props.get("gridId"),
        "grid_x": props.get("gridX"),
        "grid_y": props.get("gridY"),
        "forecast_zone": str(props.get("forecastZone") or "").rsplit("/", 1)[-1] or None,
        "county_zone": str(props.get("county") or "").rsplit("/", 1)[-1] or None,
        "timezone": props.get("timeZone") or config.CAMPUS_TZ,
        "forecast_url": props.get("forecast"),
        "forecast_hourly_url": props.get("forecastHourly"),
        "observation_stations_url": props.get("observationStations"),
        "radar_station": props.get("radarStation"),
        "relative_location": props.get("relativeLocation"),
        "source": f"NWS /points/{lat:.4f},{lon:.4f}",
    }


def normalize_hourly(payload: dict, *, fetched_at: str | None = None,
                     stale: bool = False,
                     source: str = "NWS hourly forecast") -> list[dict]:
    """`forecastHourly` response -> list of forecast WeatherWindow dicts.

    `probabilityOfPrecipitation.value` is frequently null; it is preserved as
    None (never coerced to 0, which would falsely read as "no rain chance").
    """
    props = (payload or {}).get("properties") or {}
    periods = props.get("periods") or []
    out: list[dict] = []
    for period in periods:
        if not isinstance(period, dict):
            continue
        unit = period.get("temperatureUnit") or "F"
        temp_c = _temp_to_c(period.get("temperature"), unit)
        precip = period.get("probabilityOfPrecipitation") or {}
        wind_kph, wind_mph, wind_text = _parse_wind_text(period.get("windSpeed"))
        humidity = (period.get("relativeHumidity") or {}).get("value")
        out.append({
            "kind": "forecast",
            "number": period.get("number"),
            "start": period.get("startTime"),
            "end": period.get("endTime"),
            "timezone": config.CAMPUS_TZ,
            "temperature_c": _round(temp_c, 1),
            "temperature_f": _round(_c_to_f(temp_c), 1),
            "temperature_unit": unit,
            "precip_probability_pct": _round(_num(precip.get("value")), 1),
            "relative_humidity_pct": _round(_num(humidity), 1),
            "wind_speed_kph": _round(wind_kph, 1),
            "wind_speed_mph": _round(wind_mph, 1),
            "wind_speed_text": wind_text,
            "wind_direction": period.get("windDirection"),
            "short_forecast": period.get("shortForecast"),
            "detailed_forecast": period.get("detailedForecast"),
            "is_daytime": period.get("isDaytime"),
            "source": source,
            "fetched_at": fetched_at,
            "stale": stale,
        })
    return out


def normalize_alerts(payload: dict, *, fetched_at: str | None = None,
                     stale: bool = False,
                     source: str = "NWS active alerts") -> list[dict]:
    """`/alerts/active` response -> list of Alert dicts (possibly empty)."""
    features = (payload or {}).get("features") or []
    out: list[dict] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        out.append({
            "kind": "alert",
            "id": props.get("id") or feature.get("id"),
            "event": props.get("event"),
            "severity": props.get("severity"),
            "certainty": props.get("certainty"),
            "urgency": props.get("urgency"),
            "status": props.get("status"),
            "message_type": props.get("messageType"),
            "category": props.get("category"),
            "response": props.get("response"),
            "headline": props.get("headline"),
            "description": props.get("description"),
            "instruction": props.get("instruction"),
            "area_desc": props.get("areaDesc"),
            "sender_name": props.get("senderName"),
            "effective": props.get("effective"),
            "onset": props.get("onset"),
            "expires": props.get("expires"),
            "ends": props.get("ends"),
            "source": source,
            "fetched_at": fetched_at,
            "stale": stale,
        })
    return out


def normalize_observation(payload: dict, *, station_id: str | None = None,
                          station_name: str | None = None,
                          fetched_at: str | None = None,
                          age_seconds: float | None = None,
                          stale: bool = False,
                          source: str = "NWS latest observation") -> dict:
    """`/stations/{id}/observations/latest` -> one Observation dict."""
    props = (payload or {}).get("properties") or {}
    station_url = props.get("station") or ""
    resolved_id = station_id or str(station_url).rsplit("/", 1)[-1] or None
    resolved_name = station_name or STATION_LABELS.get(resolved_id or "")
    temp_c = _temp_to_c(
        (props.get("temperature") or {}).get("value"),
        (props.get("temperature") or {}).get("unitCode"),
    )
    wind_kph = _speed_to_kph(
        (props.get("windSpeed") or {}).get("value"),
        (props.get("windSpeed") or {}).get("unitCode"),
    )
    gust_kph = _speed_to_kph(
        (props.get("windGust") or {}).get("value"),
        (props.get("windGust") or {}).get("unitCode"),
    )
    humidity = (props.get("relativeHumidity") or {}).get("value")
    note = KBCB_NOTE if resolved_id == "KBCB" else None
    return {
        "kind": "observation",
        "station_id": resolved_id,
        "station_name": resolved_name,
        "station_note": note,
        "timestamp": props.get("timestamp"),
        "temperature_c": _round(temp_c, 1),
        "temperature_f": _round(_c_to_f(temp_c), 1),
        "dewpoint_c": _round(_temp_to_c(
            (props.get("dewpoint") or {}).get("value"),
            (props.get("dewpoint") or {}).get("unitCode")), 1),
        "heat_index_c": _round(_temp_to_c(
            (props.get("heatIndex") or {}).get("value"),
            (props.get("heatIndex") or {}).get("unitCode")), 1),
        "wind_chill_c": _round(_temp_to_c(
            (props.get("windChill") or {}).get("value"),
            (props.get("windChill") or {}).get("unitCode")), 1),
        "relative_humidity_pct": _round(_num(humidity), 1),
        "wind_speed_kph": _round(wind_kph, 1),
        "wind_speed_mph": _round(_kmh_to_mph(wind_kph), 1),
        "wind_gust_kph": _round(gust_kph, 1),
        "wind_direction_deg": _round(_num(
            (props.get("windDirection") or {}).get("value")), 0),
        "text_description": props.get("textDescription"),
        "source": source,
        "fetched_at": fetched_at,
        "age_seconds": age_seconds,
        "stale": stale,
    }


# ------------------------------------------------------------------ fetch API
def resolve_point(lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON,
                  *, force: bool = False, now: datetime | None = None) -> dict:
    """Resolve `/points/{lat},{lon}` -> grid, forecast URLs, zone, timezone.

    Always returns a dict; a source failure yields status="unavailable".
    """
    params = _coord_params(lat, lon)
    try:
        payload = _load("weather_points", _point_url(lat, lon), params,
                        POINTS_TTL_S, force)
    except WeatherUnavailable as exc:
        return _base_result("weather_points", params, POINTS_TTL_S,
                            source=_point_url(lat, lon), now=now,
                            status="unavailable", reason=str(exc))
    meta = normalize_point(payload, lat, lon)
    result = _base_result("weather_points", params, POINTS_TTL_S,
                          source=meta["source"], now=now)
    result["point"] = meta
    return result


def hourly_windows(lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON,
                   *, force: bool = False, now: datetime | None = None) -> dict:
    """Hourly forecast -> {"status", "windows": [...], freshness, sources}.

    The resolved point is a dependency: if its cached copy is stale, the whole
    forecast result is marked stale too, and `sources` exposes each feed.
    """
    point_res = resolve_point(lat, lon, force=force, now=now)
    sources: dict[str, dict] = {"points": point_res}
    meta = point_res.get("point") or {}
    point_public = {
        "grid_id": meta.get("grid_id"), "grid_x": meta.get("grid_x"),
        "grid_y": meta.get("grid_y"), "forecast_zone": meta.get("forecast_zone"),
        "timezone": meta.get("timezone"),
    }

    def finish(result: dict) -> dict:
        status, stale, reason, metas = _aggregate(sources, primary="hourly")
        out = {**result, "status": status, "stale": stale, "reason": reason,
               "sources": metas, "point": point_public}
        for window in out.get("windows") or []:
            window["stale"] = stale
        return out

    if point_res["status"] == "unavailable":
        return finish({**point_res, "windows": []})

    url = meta.get("forecast_hourly_url")
    params = {"grid": str(meta.get("grid_id")), "x": str(meta.get("grid_x")),
              "y": str(meta.get("grid_y"))}
    if not url:
        hourly = _base_result("weather_hourly", params, HOURLY_TTL_S,
                              source="NWS hourly forecast", now=now,
                              status="unavailable",
                              reason="point metadata has no forecastHourly URL")
        sources["hourly"] = hourly
        return finish({**hourly, "windows": []})
    try:
        payload = _load("weather_hourly", url, params, HOURLY_TTL_S, force)
    except WeatherUnavailable as exc:
        hourly = _base_result("weather_hourly", params, HOURLY_TTL_S,
                              source="NWS hourly forecast", now=now,
                              status="unavailable", reason=str(exc))
        sources["hourly"] = hourly
        return finish({**hourly, "windows": []})
    hourly = _base_result("weather_hourly", params, HOURLY_TTL_S,
                          source="NWS hourly forecast", now=now)
    hourly["windows"] = normalize_hourly(
        payload,
        fetched_at=hourly["fetched_at"],
        stale=hourly["stale"],
        source=f"NWS grid {meta.get('grid_id')}/{meta.get('grid_x')},{meta.get('grid_y')} hourly",
    )
    sources["hourly"] = hourly
    return finish(hourly)


def forecast_strip(lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON, *,
                   hours: int = 12, at: datetime | None = None,
                   force: bool = False, now: datetime | None = None) -> dict:
    """API-ready forecast strip for the next `hours` from `at` (default now).

    Only whole forecast hours that start at/after `at` are included, capped at
    `hours`. `status` is "ok", "stale", or "unavailable"; the freshness fields
    are always present so the UI can show source + age.
    """
    fetched = hourly_windows(lat, lon, force=force, now=now)
    if fetched["status"] == "unavailable":
        return {**fetched, "windows": [], "hours": int(hours)}
    reference = at if at is not None else (now if now is not None else config.now())
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=_CAMPUS_TZ)
    selected: list[dict] = []
    for window in fetched["windows"]:
        start = _parse_ts(window.get("start"))
        if start is None or start < reference.astimezone(start.tzinfo):
            continue
        selected.append(window)
        if len(selected) >= max(0, int(hours)):
            break
    out = {k: v for k, v in fetched.items() if k != "windows"}
    out["windows"] = selected
    out["hours"] = len(selected)
    out["strip_start"] = _iso(reference)
    return out


def active_alerts(lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON, *,
                  include_zone: bool = True, force: bool = False,
                  now: datetime | None = None) -> dict:
    """Active alerts for the point and (optionally) its forecast zone.

    Point and zone results are BOTH merged (a zone can raise an alert that the
    point response does not list) and deduplicated by alert id. Empty is a
    normal state: `alerts` is [] and `status` is "ok". If one feed is down the
    aggregate is "partial", never "ok"; `sources` exposes each feed.
    """
    params = _coord_params(lat, lon)
    url = f"{NWS_API_BASE}/alerts/active?point={params['lat']},{params['lon']}"
    sources: dict[str, dict] = {}
    alerts: list[dict] = []

    try:
        payload = _load("weather_alerts_point", url, params, ALERTS_TTL_S, force)
        point_result = _base_result("weather_alerts_point", params, ALERTS_TTL_S,
                                    source="NWS active alerts (point)", now=now)
        point_result["alerts"] = normalize_alerts(
            payload, fetched_at=point_result["fetched_at"],
            stale=point_result["stale"], source="NWS active alerts (point)")
        alerts.extend(point_result["alerts"])
        sources["point"] = point_result
    except WeatherUnavailable as exc:
        sources["point"] = _base_result(
            "weather_alerts_point", params, ALERTS_TTL_S,
            source="NWS active alerts (point)", now=now,
            status="unavailable", reason=str(exc))

    zone_id = None
    if include_zone:
        point_res = resolve_point(lat, lon, force=False, now=now)
        if point_res["status"] == "unavailable":
            # The zone feed DEPENDS on the point metadata. If the metadata is
            # down, say so as an explicit unavailable zone source instead of
            # silently omitting it (which would make a point-only bundle look
            # "ok").
            sources["zone"] = _base_result(
                "weather_alerts_zone", None, ALERTS_TTL_S,
                source="NWS active alerts (zone)", now=now,
                status="unavailable",
                reason=("point metadata unavailable; forecast zone could not "
                        "be resolved"))
        else:
            zone_id = point_res["point"].get("forecast_zone")
            if not zone_id:
                sources["zone"] = _base_result(
                    "weather_alerts_zone", None, ALERTS_TTL_S,
                    source="NWS active alerts (zone)", now=now,
                    status="unavailable", reason="point metadata has no forecastZone")
    if zone_id:
        zparams = {"zone": zone_id}
        zurl = f"{NWS_API_BASE}/alerts/active?zone={zone_id}"
        try:
            payload = _load("weather_alerts_zone", zurl, zparams, ALERTS_TTL_S, force)
            zone_result = _base_result("weather_alerts_zone", zparams, ALERTS_TTL_S,
                                       source=f"NWS active alerts (zone {zone_id})", now=now)
            zone_result["alerts"] = normalize_alerts(
                payload, fetched_at=zone_result["fetched_at"],
                stale=zone_result["stale"],
                source=f"NWS active alerts (zone {zone_id})")
            alerts.extend(zone_result["alerts"])
            sources["zone"] = zone_result
        except WeatherUnavailable as exc:
            sources["zone"] = _base_result(
                "weather_alerts_zone", zparams, ALERTS_TTL_S,
                source=f"NWS active alerts (zone {zone_id})", now=now,
                status="unavailable", reason=str(exc))

    # Deduplicate by alert id; a critical alert is often listed by both point
    # and zone. Point copies are appended first, so they win on provenance.
    deduped: list[dict] = []
    seen: set[str] = set()
    for alert in alerts:
        dedupe_key = str(
            alert.get("id")
            or f"{alert.get('event')}|{alert.get('onset')}|{alert.get('expires')}")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append(alert)

    status, stale, reason, metas = _aggregate(sources)
    primary = next((m for m in metas.values() if m.get("fetched_at")), {})
    feeds = "+".join(sorted(metas)) or "none"
    return {
        "status": status,
        "alerts": deduped,
        "count": len(deduped),
        "by_severity": summarize_alerts(deduped),
        "source": f"NWS active alerts ({feeds})",
        "attribution": ATTRIBUTION,
        "fetched_at": primary.get("fetched_at"),
        "age_seconds": primary.get("age_seconds"),
        "stale": stale,
        "forecast_zone": zone_id,
        "sources": metas,
        "reason": reason,
    }


def summarize_alerts(alerts: list[dict]) -> dict:
    counts = {"Extreme": 0, "Severe": 0, "Moderate": 0, "Minor": 0, "Unknown": 0}
    for alert in alerts or []:
        sev = str(alert.get("severity") or "Unknown")
        counts[sev if sev in counts else "Unknown"] += 1
    return counts


def nearest_station(stations: list[dict]) -> dict | None:
    """Pick the station with the smallest normalized distance (None last).

    NWS returns the gridpoint stations in an order that happens to be nearest
    first, but that is not a contract. Sorting by the reported `distance_m` is
    deterministic and survives a reordering of the response.
    """
    usable = [s for s in stations or [] if isinstance(s, dict)]
    if not usable:
        return None
    return min(
        usable,
        key=lambda s: (s.get("distance_m") is None,
                       s.get("distance_m") if s.get("distance_m") is not None else 0.0),
    )


def observation_stations(lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON, *,
                         force: bool = False, now: datetime | None = None) -> dict:
    """Observation stations near the grid, nearest first (sorted by distance)."""
    point_res = resolve_point(lat, lon, force=force, now=now)
    sources: dict[str, dict] = {"points": point_res}
    if point_res["status"] == "unavailable":
        status, stale, reason, metas = _aggregate(sources, primary="stations")
        return {**point_res, "stations": [], "status": status, "stale": stale,
                "reason": reason, "sources": metas}
    meta = point_res["point"]
    url = meta.get("observation_stations_url")
    if not url:
        stations_res = _base_result("weather_stations", None, STATIONS_TTL_S,
                                    source="NWS observation stations", now=now,
                                    status="unavailable",
                                    reason="point metadata has no observationStations URL")
        sources["stations"] = stations_res
        status, stale, reason, metas = _aggregate(sources, primary="stations")
        return {**stations_res, "stations": [], "status": status, "stale": stale,
                "reason": reason, "sources": metas}
    params = {"grid": str(meta.get("grid_id")), "x": str(meta.get("grid_x")),
              "y": str(meta.get("grid_y"))}
    try:
        payload = _load("weather_stations", url, params, STATIONS_TTL_S, force)
    except WeatherUnavailable as exc:
        stations_res = _base_result("weather_stations", params, STATIONS_TTL_S,
                                    source="NWS observation stations", now=now,
                                    status="unavailable", reason=str(exc))
        sources["stations"] = stations_res
        status, stale, reason, metas = _aggregate(sources, primary="stations")
        return {**stations_res, "stations": [], "status": status, "stale": stale,
                "reason": reason, "sources": metas}
    stations_res = _base_result("weather_stations", params, STATIONS_TTL_S,
                                source="NWS observation stations", now=now)
    stations = []
    for feature in (payload or {}).get("features") or []:
        props = feature.get("properties") or {}
        sid = props.get("stationIdentifier")
        stations.append({
            "station_id": sid,
            "name": props.get("name"),
            "label": STATION_LABELS.get(sid, props.get("name")),
            "distance_m": _round(_num((props.get("distance") or {}).get("value")), 0),
            "timezone": props.get("timeZone"),
            "source": "NWS /gridpoints/stations",
        })
    stations_res["stations"] = stations
    stations.sort(key=lambda s: (s["distance_m"] is None,
                                 s["distance_m"] if s["distance_m"] is not None else 0.0))
    sources["stations"] = stations_res
    status, stale, reason, metas = _aggregate(sources, primary="stations")
    return {**stations_res, "status": status, "stale": stale,
            "reason": reason, "sources": metas}


def latest_observation(lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON, *,
                       station: str | None = None, force: bool = False,
                       now: datetime | None = None) -> dict:
    """Latest observation, defaulting to the nearest station from NWS.

    For the Burruss centroid the nearest station is KBCB (Virginia Tech
    Airport). The output carries `station_note` saying it is an airport station,
    not an on-campus sensor. Freshness aggregates the point metadata, the
    station list, and the observation itself, and exposes each as a `sources`
    entry; a stale dependency propagates to the top-level `stale`.
    """
    sources: dict[str, dict] = {}
    if station is None:
        stations = observation_stations(lat, lon, force=force, now=now)
        sources.update(stations.get("sources") or {})
        choice = nearest_station(stations.get("stations") or [])
        if stations["status"] == "unavailable" or choice is None:
            result = _base_result("weather_observation", None, OBSERVATION_TTL_S,
                                  source="NWS latest observation", now=now,
                                  status="unavailable",
                                  reason="no observation station could be resolved")
            sources["observation"] = result
            status, stale, reason, metas = _aggregate(sources, primary="observation")
            result.update(status=status, stale=stale, reason=reason, sources=metas)
            return result
        station = choice["station_id"]
    params = {"station": str(station)}
    url = f"{NWS_API_BASE}/stations/{station}/observations/latest"
    try:
        payload = _load("weather_observation", url, params, OBSERVATION_TTL_S, force)
    except WeatherUnavailable as exc:
        result = _base_result("weather_observation", params, OBSERVATION_TTL_S,
                              source="NWS latest observation", now=now,
                              status="unavailable", reason=str(exc))
        sources["observation"] = result
        status, stale, reason, metas = _aggregate(sources, primary="observation")
        result.update(status=status, stale=stale, reason=reason, sources=metas)
        return result
    result = _base_result("weather_observation", params, OBSERVATION_TTL_S,
                          source="NWS latest observation", now=now)
    obs = normalize_observation(
        payload, station_id=str(station),
        station_name=STATION_LABELS.get(str(station)),
        fetched_at=result["fetched_at"], age_seconds=result["age_seconds"],
        stale=result["stale"],
        source=f"NWS {station} latest observation")
    result["observation"] = obs
    sources["observation"] = result
    status, stale, reason, metas = _aggregate(sources, primary="observation")
    obs["stale"] = stale
    result.update(status=status, stale=stale, reason=reason, sources=metas)
    return result


# ------------------------------------------------------------------ thresholds
def _thresholds(overrides: dict | None) -> dict:
    th = copy.deepcopy(DEFAULT_THRESHOLDS)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(th.get(key), dict):
            th[key].update(value)
        else:
            th[key] = value
    return th


def _level_rank(level: str) -> int:
    return _LEVEL_RANK.get(str(level), -1)


def _max_level(*levels: str) -> str:
    best = "none"
    for level in levels:
        if _level_rank(level) > _level_rank(best):
            best = level
    return best


def _band(value: float | None, bands: dict, *, reverse: bool = False) -> str:
    """Map a value to a level using moderate/high/severe bands.

    `reverse` is used for cold: -18 is more dangerous than 0, so the comparison
    flips.
    """
    if value is None:
        return "none"
    if reverse:
        if value <= bands.get("severe", float("-inf")):
            return "severe"
        if value <= bands.get("high", float("-inf")):
            return "high"
        if value <= bands.get("moderate", float("-inf")):
            return "moderate"
        if value <= bands.get("low", float("-inf")):
            return "low"
        return "none"
    if value >= bands.get("severe", float("inf")):
        return "severe"
    if value >= bands.get("high", float("inf")):
        return "high"
    if value >= bands.get("moderate", float("inf")):
        return "moderate"
    if value >= bands.get("low", float("inf")):
        return "low"
    return "none"


# ------------------------------------------------------------------ risk
def overlapping_windows(start, end, windows: list[dict]) -> list[dict]:
    """Forecast windows that overlap [start, end). Half-open: touching is not overlap."""
    a = _parse_ts(start) if not isinstance(start, datetime) else start
    b = _parse_ts(end) if not isinstance(end, datetime) else end
    if a is None or b is None:
        return []
    if a.tzinfo is None:
        a = a.replace(tzinfo=_CAMPUS_TZ)
    if b.tzinfo is None:
        b = b.replace(tzinfo=_CAMPUS_TZ)
    out = []
    for window in windows or []:
        ws = _parse_ts(window.get("start"))
        we = _parse_ts(window.get("end"))
        if ws is None or we is None:
            continue
        if ws.tzinfo is None:
            ws = ws.replace(tzinfo=_CAMPUS_TZ)
        if we.tzinfo is None:
            we = we.replace(tzinfo=_CAMPUS_TZ)
        if ws < b and we > a:
            out.append(window)
    return out


def _alert_overlaps(alert: dict, start: datetime, end: datetime) -> bool:
    """True when a point/zone alert is in effect during [start, end).

    Effective interval preference: onset -> effective, and ends -> expires.
    An alert with no parseable interval is treated as active for the window
    (it was returned by /alerts/active for this point/zone).
    """
    a_start = _parse_ts(alert.get("onset")) or _parse_ts(alert.get("effective"))
    a_end = _parse_ts(alert.get("ends")) or _parse_ts(alert.get("expires"))
    if a_start is None and a_end is None:
        return True
    if a_start is not None and a_start >= end:
        return False
    if a_end is not None and a_end <= start:
        return False
    return True


def _has_thunder(window: dict) -> bool:
    return bool(_THUNDER_RE.search(str(window.get("short_forecast") or "")))


def _period_gaps(window: dict) -> list[str]:
    """Required hazard fields missing from ONE forecast period.

    Evaluated per period on purpose: a complete mild hour must not mask an
    incomplete or rain-token hour elsewhere in the same leg window.
    """
    gaps: list[str] = []
    short = str(window.get("short_forecast") or "")
    has_precip = window.get("precip_probability_pct") is not None
    has_token = bool(_PRECIP_TOKEN_RE.search(short))
    if not has_precip and not has_token:
        gaps.append("precipitation probability")
    if window.get("temperature_c") is None:
        gaps.append("temperature")
    if window.get("wind_speed_kph") is None:
        gaps.append("wind")
    return gaps


def _winning_basis(evidence: list[dict], level: str) -> str:
    """Which evidence produced the winning level: forecast, alert, or mixed.

    The label must not claim a forecast basis when only an alert fired.
    """
    if level in ("none", "unknown"):
        return "forecast"
    factors = {e.get("factor") for e in evidence if e.get("level") == level}
    if not factors:
        return "forecast"
    if factors == {"alert"}:
        return "alert"
    if "alert" in factors:
        return "mixed"
    return "forecast"


def assess_leg(start, end, *, windows: list[dict] | None = None,
               alerts: list[dict] | None = None,
               thresholds: dict | None = None) -> dict:
    """Deterministic weather risk for one time window.

    Pure: no I/O. `windows` and `alerts` are normalized dicts from this module.
    Returns a level, the evidence that produced it, human reasons, and the
    thresholds used. It NEVER asserts a fact -- the language is about risk.
    """
    th = _thresholds(thresholds)
    a = start if isinstance(start, datetime) else _parse_ts(start)
    b = end if isinstance(end, datetime) else _parse_ts(end)
    if a is None or b is None:
        return {
            "status": "unknown", "level": "unknown", "label": LEVEL_LABELS["unknown"],
            "reasons": ["this window has no start/end, so it cannot be checked"],
            "evidence": [], "summary": SUMMARY_BY_LEVEL["unknown"],
            "overlap_count": 0, "basis": "forecast",
        }
    if a.tzinfo is None:
        a = a.replace(tzinfo=_CAMPUS_TZ)
    if b.tzinfo is None:
        b = b.replace(tzinfo=_CAMPUS_TZ)

    overlapping = overlapping_windows(a, b, windows or [])
    evidence: list[dict] = []
    reasons: list[str] = []

    if not overlapping:
        evidence.append({
            "factor": "coverage", "level": "unknown",
            "detail": "no hourly forecast covers this window",
            "basis": "forecast",
        })
        reasons.append("no hourly forecast covers this window")

    # ---- hazard field presence: evaluated PER overlapping period -----------
    precip_values = [(w.get("precip_probability_pct"), w) for w in overlapping
                     if w.get("precip_probability_pct") is not None]
    temps = [w.get("temperature_c") for w in overlapping
             if w.get("temperature_c") is not None]
    winds = [w.get("wind_speed_kph") for w in overlapping
             if w.get("wind_speed_kph") is not None]
    precip_token_windows = [
        w for w in overlapping
        if _PRECIP_TOKEN_RE.search(str(w.get("short_forecast") or ""))]
    thunder = [w for w in overlapping if _has_thunder(w)]
    # Union of fields missing from ANY overlapping hour (not just all of them).
    gaps: list[str] = sorted({field for w in overlapping for field in _period_gaps(w)})
    gapped_hours = sum(1 for w in overlapping if _period_gaps(w))

    # precipitation probability: worst overlapping hour that actually reports one
    if precip_values:
        peak, peak_window = max(precip_values, key=lambda item: item[0])
        level = _band(peak, th["precip_probability_pct"])
        if level != "none":
            detail = (f"forecast shows a {peak:.0f}% chance of precipitation "
                      "during this window")
            evidence.append({
                "factor": "precip_probability", "level": level,
                "value": peak, "unit": "percent",
                "detail": detail, "basis": "forecast",
                "window_start": peak_window.get("start"),
                "window_end": peak_window.get("end"),
            })
            reasons.append(detail)
    # Precipitation tokens in hours that do NOT report a probability get their
    # own conservative evidence, even when another hour DID report a value.
    token_only = [w for w in precip_token_windows
                  if w.get("precip_probability_pct") is None]
    if token_only:
        sample = token_only[0].get("short_forecast")
        detail = (f"forecast mentions precipitation ({sample}) but no "
                  "probability is reported")
        evidence.append({
            "factor": "precip_token", "level": "low", "value": len(token_only),
            "detail": detail, "basis": "forecast",
        })
        reasons.append(detail)
    if not precip_values and not precip_token_windows:
        evidence.append({
            "factor": "precip_probability", "level": "unknown",
            "detail": "precipitation probability is not reported for this window",
            "basis": "forecast",
        })

    # thunderstorm token
    if thunder:
        sample = thunder[0].get("short_forecast")
        detail = f"thunderstorms are mentioned in the forecast ({sample})"
        evidence.append({
            "factor": "thunderstorm", "level": th["thunderstorm_level"],
            "value": len(thunder), "unit": "hours",
            "detail": detail, "basis": "forecast",
        })
        reasons.append(detail)

    # heat / cold
    heat_level = _band(max(temps) if temps else None, th["temperature_high_c"])
    if heat_level != "none":
        detail = (f"forecast temperature is around {max(temps):.0f} °C "
                  "during this window")
        evidence.append({
            "factor": "heat", "level": heat_level, "value": _round(max(temps), 1),
            "unit": "degC", "detail": detail, "basis": "forecast",
        })
        reasons.append(detail)
    cold_level = _band(min(temps) if temps else None, th["temperature_low_c"],
                       reverse=True)
    if cold_level != "none":
        detail = (f"forecast temperature drops to about {min(temps):.0f} °C "
                  "during this window")
        evidence.append({
            "factor": "cold", "level": cold_level, "value": _round(min(temps), 1),
            "unit": "degC", "detail": detail, "basis": "forecast",
        })
        reasons.append(detail)

    # wind
    wind_level = _band(max(winds) if winds else None, th["wind_kph"])
    if wind_level != "none":
        detail = (f"forecast wind is around {max(winds):.0f} km/h during this "
                  "window")
        evidence.append({
            "factor": "wind", "level": wind_level, "value": _round(max(winds), 1),
            "unit": "km/h", "detail": detail, "basis": "forecast",
        })
        reasons.append(detail)

    # alerts in effect during the window
    alert_levels: list[str] = []
    severity_map = th["alert_severity"]
    for alert in alerts or []:
        if not _alert_overlaps(alert, a, b):
            continue
        severity = str(alert.get("severity") or "Unknown")
        level = severity_map.get(severity, "low")
        event = str(alert.get("event") or "")
        if event.lower() in SEVERE_ALERT_EVENTS:
            level = _max_level(level, th["severe_alert_event_level"])
        alert_levels.append(level)
        detail = f"{event or 'weather alert'} ({severity}) in effect for this window"
        evidence.append({
            "factor": "alert", "level": level, "value": event,
            "detail": detail, "basis": "alert",
            "effective": alert.get("effective"), "expires": alert.get("expires"),
        })
        reasons.append(detail)

    level = _max_level(
        *([e["level"] for e in evidence if e["level"] != "unknown"] or ["none"]),
        *alert_levels,
    )
    missing_fields = ", ".join(gaps)
    if not overlapping and level == "none":
        # No forecast covers the window and no alert applies: the honest answer
        # is "unknown", not the reassuring "none".
        return {
            "status": "unknown", "level": "unknown",
            "label": LEVEL_LABELS["unknown"],
            "reasons": ["no hourly forecast covers this window"],
            "evidence": evidence, "summary": SUMMARY_BY_LEVEL["unknown"],
            "overlap_count": 0, "basis": "forecast",
            "start": _iso(a), "end": _iso(b), "thresholds": th,
        }
    if level == "none" and gaps:
        # A mild-but-present signal is not enough when required fields are
        # missing; do not present missing data as "no risk".
        gaps_detail = (f"forecast hours for this window are missing: "
                       f"{missing_fields}")
        evidence.append({"factor": "data_gap", "level": "unknown",
                         "detail": gaps_detail, "basis": "forecast"})
        return {
            "status": "unknown", "level": "unknown",
            "label": LEVEL_LABELS["unknown"],
            "reasons": [gaps_detail],
            "evidence": evidence, "summary": SUMMARY_BY_LEVEL["unknown"],
            "overlap_count": len(overlapping), "basis": "forecast",
            "start": _iso(a), "end": _iso(b), "thresholds": th,
        }
    if gaps:
        evidence.append({
            "factor": "data_gap", "level": "unknown",
            "detail": (f"{gapped_hours} forecast hour(s) are missing: "
                       f"{missing_fields}"),
            "basis": "forecast",
        })
    if not reasons:
        reasons = [SUMMARY_BY_LEVEL[level]]
    basis = _winning_basis(evidence, level)
    return {
        "status": "ok",
        "level": level,
        "label": LEVEL_LABELS.get(level, "Unknown"),
        "reasons": reasons,
        "evidence": evidence,
        "summary": SUMMARY_BY_LEVEL.get(level, SUMMARY_BY_LEVEL["none"]),
        "overlap_count": len(overlapping),
        "basis": basis,
        "start": _iso(a),
        "end": _iso(b),
        "thresholds": th,
    }


def plan_risk(legs: list[dict], lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON,
              *, windows: list[dict] | None = None, alerts: list[dict] | None = None,
              thresholds: dict | None = None,
              outdoor_types: tuple[str, ...] = OUTDOOR_LEG_TYPES,
              force: bool = False, now: datetime | None = None) -> dict:
    """Per-leg weather risk badges + an overall level.

    `legs` are itinerary leg dicts (from hokieday.tools): a `type`, a
    `start_time` (ISO) and either `end_time`/`end` or `minutes`. Indoor legs
    (eat, bus) are reported `not_applicable`. When `windows`/`alerts` are not
    supplied they are fetched through the cache layer; supplying them keeps the
    whole call pure and offline-testable.
    """
    sources: dict[str, dict] = {}
    if windows is None:
        fetched = hourly_windows(lat, lon, force=force, now=now)
        windows = fetched.get("windows") or []
        sources["forecast"] = fetched
    if alerts is None:
        fetched_alerts = active_alerts(lat, lon, force=force, now=now)
        alerts = fetched_alerts.get("alerts") or []
        sources["alerts"] = fetched_alerts
    if sources:
        agg_status, agg_stale, agg_reason, metas = _aggregate(sources, primary="forecast")
    else:
        agg_status, agg_stale, agg_reason, metas = "ok", False, None, {}

    results: list[dict] = []
    for index, leg in enumerate(legs or []):
        entry = _leg_badge(leg, index, windows, alerts, thresholds, outdoor_types)
        results.append(entry)

    overall = "none"
    for entry in results:
        overall = _max_level(overall, entry.get("level", "none"))
    if any(e["status"] == "unknown" for e in results) and overall == "none":
        overall = "unknown"
    source = ((sources.get("forecast") or {}).get("source")
              or (sources.get("alerts") or {}).get("source"))
    return {
        "status": agg_status,
        "level": overall,
        "label": LEVEL_LABELS.get(overall, "Unknown"),
        "legs": results,
        "source": source,
        "attribution": ATTRIBUTION,
        "fetched_at": ((sources.get("forecast") or {}).get("fetched_at")),
        "age_seconds": ((sources.get("forecast") or {}).get("age_seconds")),
        "stale": bool(agg_stale),
        "reason": agg_reason,
        "sources": metas,
        "thresholds": _thresholds(thresholds),
    }


def _leg_badge(leg: dict, index: int, windows: list[dict], alerts: list[dict],
               thresholds: dict | None, outdoor_types: tuple[str, ...]) -> dict:
    leg_type = str(leg.get("type") or "").lower()
    base = {"seq": leg.get("seq", index + 1), "type": leg_type,
            "from": leg.get("from"), "to": leg.get("to")}
    if leg_type and leg_type not in outdoor_types:
        return {**base, "status": "not_applicable", "level": "none",
                "label": LEVEL_LABELS["none"], "reasons": [],
                "summary": "This leg is not outdoors, so no weather badge applies.",
                "evidence": []}
    start = leg.get("start_time") or leg.get("start")
    end = leg.get("end_time") or leg.get("end")
    if end is None and leg.get("minutes") is not None and start is not None:
        sdt = _parse_ts(start)
        if sdt is not None:
            end = sdt + timedelta(minutes=float(leg["minutes"]))
    assessment = assess_leg(start, end, windows=windows, alerts=alerts,
                            thresholds=thresholds)
    return {**base, **assessment}