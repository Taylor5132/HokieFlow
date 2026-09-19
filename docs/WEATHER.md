# Weather integration (NWS) — backend contract

**Owner: worker weather. Source: `api.weather.gov` (keyless, official NWS).**
Implementation: `hokieday/weather.py`. Fixtures: `fixtures/weather_*`. Edge fetch:
`scripts/fetch_weather.py`. Tests: `tests/test_weather.py` (offline).

This doc is the contract the app/Figma consumes. It deliberately does **not**
re-design the visual UI and does **not** use an LLM: risk is deterministic and
the UI shows evidence, not a verdict.

---

## 1. Resolved point (verified live 2026-09-19)

Default point = official VT GIS **Burruss Hall centroid** `37.22924778,-80.42396247`.
The grid is resolved at runtime from `/points/{lat},{lon}` — never assumed.

| field | value from the captured response |
|---|---|
| grid | `RNK` `gridX=57` `gridY=65` |
| forecast zone | `VAZ014` (Montgomery) |
| county zone | `VAC121` |
| timezone | `America/New_York` |
| radar | `KFCX` (Blacksburg) |
| nearest observation station | `KBCB` — **"Virginia Tech Airport"**, ~1.8 km away |

**Honest label:** KBCB is the Virginia Tech Airport ASOS, an *airport* sensor,
not an on-campus weather station. Every observation carries `station_note`
saying so. Prior-research values (RNK grid, VAZ014, KBCB) are confirmed by the
live response and stored in the fixtures.

NWS API notes: responses arrive with a `301` from the un-rounded URL; `urllib`
follows it. A descriptive `User-Agent` is required — the shared
`config.USER_AGENT` is used by the cache layer. NWS data is US-Government public
domain; `ATTRIBUTION` ("Weather data from the National Weather Service
(weather.gov)") is attached to every result anyway.

---

## 2. Cache keys, TTLs, freshness

All HTTP goes through `hokieday.cache.get_json`; `weather.py` imports no urllib.

| resource | cache key | TTL | fallback |
|---|---|---|---|
| point metadata | `weather_points__lat=…__lon=…` | 24 h | stale copy |
| hourly forecast | `weather_hourly__grid=…__x=…__y=…` | 25 min | stale copy |
| alerts by point | `weather_alerts_point__lat=…__lon=…` | 3 min | stale copy |
| alerts by zone | `weather_alerts_zone__zone=…` | 3 min | stale copy |
| station list | `weather_stations__grid=…__x=…__y=…` | 24 h | stale copy |
| latest observation | `weather_observation__station=KBCB` | 10 min | stale copy |

The cache layer already returns the last-good copy when upstream fails. On top
of that, each result carries:

```
status      : "ok" | "stale" | "unavailable"
stale       : bool
age_seconds : seconds since fetch, measured against config.now() (the replay clock)
fetched_at  : provenance stamp from the cache envelope
source      : the URL / grid that produced it
attribution : NWS credit line
reason      : human explanation when not "ok" (null when "ok")
```

Freshness is measured against `config.now()`, **not** real elapsed wall time, so
a replayed demo is self-consistent. There is no `datetime.now()` in the module
(the integration AST tripwire enforces this).

---

## 3. Normalized schemas (plain dicts)

### WeatherWindow (`kind: "forecast"`)

```
kind, number, start, end, timezone
temperature_c, temperature_f, temperature_unit          # source unit preserved
precip_probability_pct                                   # NONE stays None (not 0)
relative_humidity_pct
wind_speed_kph, wind_speed_mph, wind_speed_text, wind_direction
short_forecast, detailed_forecast, is_daytime
source, fetched_at, stale
```

### Alert (`kind: "alert"`)

```
id, event, severity, certainty, urgency, status, message_type, category, response,
headline, description, instruction, area_desc, sender_name,
effective, onset, expires, ends, source, fetched_at, stale
```

### Observation (`kind: "observation"`)

```
station_id, station_name, station_note            # KBCB -> airport note
timestamp
temperature_c, temperature_f, dewpoint_c, heat_index_c, wind_chill_c
relative_humidity_pct
wind_speed_kph, wind_speed_mph, wind_gust_kph, wind_direction_deg
text_description, source, fetched_at, age_seconds, stale
```

Forecast and observation are **never mixed**: `kind` is always `forecast` or
`observation`. Bulk surfaces (`hourly_windows`, `forecast_strip`) are forecast;
`latest_observation` is observation and is labelled `current conditions`, not a
prediction.

---

## 4. API-ready functions

| function | returns |
|---|---|
| `resolve_point(lat, lon)` | point metadata result (grid/zone/timezone/URLs) |
| `hourly_windows(lat, lon)` | `{status, windows:[WeatherWindow], point, freshness}` |
| `forecast_strip(lat, lon, hours=12, at=None)` | `{status, windows, hours, strip_start, freshness}` — next N whole hours |
| `active_alerts(lat, lon, include_zone=True)` | `{status, alerts, count, by_severity, forecast_zone, freshness}` |
| `observation_stations(lat, lon)` | `{status, stations:[…nearest first…]}` |
| `latest_observation(lat, lon, station=None)` | `{status, observation, freshness}` |
| `assess_leg(start, end, windows, alerts)` | one leg's risk (pure) |
| `plan_risk(legs, lat, lon, windows=None, alerts=None)` | per-leg badges + overall |
| `overlapping_windows(start, end, windows)` | overlapping forecast hours (pure) |
| `summarize_alerts(alerts)` | counts by severity |
| `as_failure(result)` | typed `Failure` for a non-`ok` result, else `None` |

### Figma states (from `status` / `level`)

| state | trigger | UI treatment |
|---|---|---|
| `ok` | fresh fixture/cache | show strip + source + age |
| `stale` | age > TTL (upstream down, stale copy served) | show data + "cached · updated HH:MM" |
| `unavailable` | no copy and fetch failed | grey weather row + "forecast unavailable", never a guess |
| level `none` | no threshold crossed | no badge / neutral |
| level `low`/`moderate` | minor/inconvenient | small info badge |
| level `high` | precip ≥ 60 %, thunder token, Severe alert, heat/cold/wind high | warning badge on the **outdoor leg** |
| level `severe` | Extreme alert or severe-band heat/cold/wind | loud badge + reason, suggest indoor/later |
| `unknown` | no forecast hour covers the window | "no forecast for this window", not "clear" |
| `not_applicable` | indoor leg (eat, bus) | no badge |

Each badge carries `reasons` (human) and `evidence` (structured) so the student
can compare, e.g. "57% chance of rain during this 12-minute walk".

---

## 5. Deterministic risk function

`assess_leg` scores each overlapping forecast hour and any in-effect alert, then
takes the **maximum** level. It returns `evidence`, `reasons`, the thresholds
used, and a `basis` of `forecast` or `alert`. It never asserts an outcome.

`DEFAULT_THRESHOLDS` (override per call):

| factor | low | moderate | high | severe |
|---|---|---|---|---|
| precip probability % | 20 | 40 | 60 | — |
| heat (°C) | — | 32 | 35 | 39 |
| cold (°C) | — | 0 | −10 | −18 |
| wind sustained (km/h) | — | 30 | 45 | 65 |
| thunderstorm token | — | — | **high** (floor) | — |
| alert severity | Minor | Moderate | Severe | Extreme |
| warning-class event | — | — | **high** (floor) | Extreme alert |

Levels: `none < low < moderate < high < severe`. `plan_risk` returns the
per-leg level plus an overall `max`.

**Prohibited certainty wording.** Generated text is risk language only. Tests
reject `will`, `definitely`, `guaranteed`, `certain(ty)`, `no doubt`,
`unconditional`, `always`, `never`, `100%` in every reason/summary. There is no
"it will rain" — only "57% chance of precipitation during this window".

---

## 6. Integration steps (parent, after this branch lands)

`tools.py` and `config.py` are parent-owned; this module does not edit them.

1. In `tools.LocalSource`, add `forecast()` / `alerts()` thin wrappers that call
   `weather.hourly_windows()` and `weather.active_alerts()`.
2. In `_build_itinerary`, after each `walk` leg is created, call
   `weather.assess_leg(leg["start_time"], start + minutes, windows, alerts)` and
   attach `weather_risk = {level, label, reasons, evidence}` to the leg.
3. Add a plan-level `weather` block from `weather.plan_risk(itinerary["legs"])`.
4. Replace the `# -- weather trigger: NOT IMPLEMENTED YET` comment in
   `_replan_trigger` with: fire cause `"weather"` when a walk leg's level is
   `high`/`severe`, detail from `reasons[0]`, and keep the existing walk-only
   alternative (Plan B already exists).
5. App: expose `GET /api/weather` → `forecast_strip()` + `active_alerts()` and
   attach `plan_risk` output to `/api/ask`. No network at request time beyond
   the cache layer.

## 7. Regenerating fixtures

```
python3 scripts/fetch_weather.py --hours 36 --stations 10
```

Writes into `fixtures/` (the frozen replay store) with the same snapshot stamp
as `bt_buses`, so tests and the offline demo stay deterministic. Alerts may be
empty — that is a real, committed state. Network stays out of tests.