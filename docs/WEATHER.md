# Weather integration (NWS) — backend contract

**Owner: worker weather. Source: `api.weather.gov` (keyless, official NWS).**
Implementation: `hokieday/weather.py`. Edge fetch/seed: `scripts/fetch_weather.py`.
Tests: `tests/test_weather.py` (offline, no fixtures required).

This doc is the contract the app/Figma consumes. It deliberately does **not**
re-design the visual UI and does **not** use an LLM: risk is deterministic and
the UI shows evidence, not a verdict.

---

## 1. Resolved point (verified live 2026-09-19)

Default point = official VT GIS **Burruss Hall centroid** `37.22924778,-80.42396247`.
The grid is resolved at runtime from `/points/{lat},{lon}` — never assumed.

| field | value from the live response |
|---|---|
| grid | `RNK` `gridX=57` `gridY=65` |
| forecast zone | `VAZ014` (Montgomery) |
| county zone | `VAC121` |
| timezone | `America/New_York` |
| radar | `KFCX` (Blacksburg) |
| nearest observation station | `KBCB` — **"Virginia Tech Airport"**, ~1.8 km away |

**Honest label:** KBCB is the Virginia Tech Airport ASOS, an *airport* sensor,
not an on-campus weather station. Every observation carries `station_note`
saying so. The nearest station is chosen by **normalized `distance_m`**, not by
response order.

NWS API notes: the un-rounded `/points` URL returns a `301`; `urllib` follows
it. A descriptive `User-Agent` is required — the shared `config.USER_AGENT` is
used by the cache layer. NWS data is US-Government public domain; `ATTRIBUTION`
("Weather data from the National Weather Service (weather.gov)") is attached to
every result anyway.

---

## 2. Cache keys, TTLs, freshness, sources

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
status      : "ok" | "stale" | "partial" | "unavailable"
stale       : bool
age_seconds : seconds since fetch, measured against config.now() (the replay clock)
fetched_at  : provenance stamp from the cache envelope
source      : the URL / grid that produced it
attribution : NWS credit line
reason      : human explanation when not "ok" (null when "ok")
sources     : per-feed statuses, e.g. {"points": {...}, "hourly": {...}}
```

Aggregate status is **truthful and non-contradictory**:

- `ok` — every requested feed is fresh (so `stale` is always `false`).
- `stale` — all feeds are present but at least one is past its TTL. A stale
  **point** dependency propagates to the whole forecast result.
- `partial` — at least one feed is available and at least one is unavailable
  (e.g. the point alert feed works but the zone feed is down). A nested
  `partial` (plan_risk's alert feed) propagates ahead of a stale sibling.
- `unavailable` — no feed is available.

The aggregator is **primary-aware**: the feed a result cannot exist without
(hourly forecast, station list, observation) drives the status. Fresh metadata
plus an unavailable primary yields `unavailable`, not a reassuring `partial`.
When `include_zone=True` the zone alert feed DEPENDS on the point metadata, so
an unavailable metadata marks an explicit unavailable `zone` source rather than
silently omitting it.

`stale` is `true` whenever any source is stale *or* unavailable, so `status ==
"ok"` can never carry `stale: true`. `sources` exposes each feed so the UI can
name which one degraded. Freshness is measured against `config.now()` (the
replay clock), **not** real elapsed time; there is no `datetime.now()` in the
module (the integration AST tripwire enforces this).

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
| `resolve_point(lat, lon)` | point metadata result (grid/zone/timezone/URLs) + `sources` |
| `hourly_windows(lat, lon)` | `{status, windows:[WeatherWindow], point, sources}` |
| `forecast_strip(lat, lon, hours=12, at=None)` | `{status, windows, hours, strip_start, sources}` — next N whole hours |
| `active_alerts(lat, lon, include_zone=True)` | `{status, alerts, count, by_severity, forecast_zone, sources}` |
| `observation_stations(lat, lon)` | `{status, stations:[…sorted nearest first…], sources}` |
| `nearest_station(stations)` | pure nearest-by-`distance_m` pick (None-safe) |
| `latest_observation(lat, lon, station=None)` | `{status, observation, sources}` |
| `assess_leg(start, end, windows, alerts)` | one leg's risk (pure) |
| `plan_risk(legs, lat, lon, windows=None, alerts=None)` | per-leg badges + overall + `sources` |
| `overlapping_windows(start, end, windows)` | overlapping forecast hours (pure) |
| `summarize_alerts(alerts)` | counts by severity |
| `as_failure(result)` | typed `Failure` for a non-`ok` result, else `None` |

### Figma states (from `status` / `level`)

| state | trigger | UI treatment |
|---|---|---|
| `ok` | fresh cache | show strip + source + age |
| `stale` | age > TTL (stale copy served) or a stale dependency | show data + "cached · updated HH:MM" |
| `partial` | one feed down, another up | show available data + "some weather feeds unavailable" |
| `unavailable` | no data (incl. replay with no coherent bundle) | grey weather row + "forecast unavailable", never a guess |
| level `none` | thresholds not crossed and all fields present | no badge / neutral |
| level `low`/`moderate` | minor/inconvenient | small info badge |
| level `high` | precip ≥ 60 %, thunder token, Severe alert, heat/cold/wind high | warning badge on the **outdoor leg** |
| level `severe` | Extreme alert or severe-band heat/cold/wind | loud badge + reason, suggest indoor/later |
| `unknown` | no forecast covers the window, or required fields are missing | "no forecast for this window", not "clear" |
| `not_applicable` | indoor leg (eat, bus) | no badge |

Each badge carries `reasons` (human) and `evidence` (structured) so the student
can compare, e.g. "forecast shows a 57% chance of rain during this 12-minute
walk".

---

## 5. Deterministic risk function

`assess_leg` scores each overlapping forecast hour and any in-effect alert, then
takes the **maximum** level. It returns `evidence`, `reasons`, the thresholds
used, and a `basis` that names the winning evidence:

- `forecast` — the top level came from forecast factors;
- `alert` — the top level came only from an in-effect alert (never labelled
  `forecast`);
- `mixed` — alerts and forecast factors tie at the top level.

**Missing data never reads as reassuring — and completeness is evaluated PER
forecast period.** For every overlapping hour, the required hazard fields are
precipitation (probability or a rain/snow token), temperature, and wind. If any
hour has a gap and no stronger evidence exists, the result is `status:
"unknown"`, not `none`; a mild complete hour cannot mask an incomplete hour. A
token like "Rain Likely" with a null probability adds conservative `low`
evidence even when a different hour reported a probability, and when a strong
hour already raised the level the result keeps that level but records a
`data_gap` evidence entry.

**Wind ranges are read conservatively.** `25 to 45 mph` and `25-45 mph` are
scored at 45 mph; `20 to 30 km/h` at 30 km/h; knots are converted. The unit is
taken from the last token that carries one.

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
per-leg level plus an overall `max`, and its aggregate `status`/`stale` come
from the same rule as the fetchers, so they cannot contradict.

**Prohibited certainty wording.** Generated text is risk language only. Tests
reject `will`, `definitely`, `guaranteed`, `certain(ty)`, `no doubt`,
`unconditional`, `always`, `never`, `100%` in every reason/summary. There is no
"it will rain" — only a forecast-stated chance.

---

## 6. Replay decision: no committed weather fixtures

The frozen replay store (`fixtures/`) deliberately contains **NO** weather
envelopes. The first NWS capture was acquired around 21:29 UTC while the bus
replay clock is pinned at 15:22 UTC, and its hourly forecast did not cover the
replay "now". Committing it (after rewriting `fetched_at` to match the bus
stamp) would have been a falsified, incoherent snapshot. Therefore, in
`DEMO_MODE=cache`, weather returns the typed `unavailable` state until all
campus sources (including the bus snapshot) can be recaptured as one coherent
bundle. Tests for cache-backed fetchers use a **temporary synthetic cache**, not
committed fixtures.

### Regenerating a coherent bundle (`scripts/fetch_weather.py`)

```
python3 scripts/fetch_weather.py --dry-run     # fetch + validate, no writes
python3 scripts/fetch_weather.py               # fetch + validate (preview only)
python3 scripts/fetch_weather.py --publish     # write; APP MUST BE STOPPED
```

The script:

1. **Controls DEMO_MODE explicitly.** It refuses to run if `DEMO_MODE` is set to
   anything other than `cache` (no silent `setdefault`).
2. **Preserves real acquisition times.** Each envelope's `fetched_at` is the
   time that response was actually fetched; nothing is rewritten.
3. **Stages the complete bundle in memory**, then validates temporal invariants:
   required resources derived from the point metadata; all fetched within a
   10-minute span; hourly forecast has periods and covers the acquisition time;
   the observation has a parseable, non-future, ≤ 6 h timestamp.
4. **Sorts the FULL station list by distance before trimming**, so the nearest
   station is never discarded by response order.
5. **Refuses an incoherent replay.** If the target store has a `bt_buses` replay
   pin that the forecast does not cover, publishing is rejected (override only
   with an explicit `--force-incoherent`).
6. **Requires `--publish` and is explicitly NOT reader-atomic across the
   bundle.** Each file is swapped atomically, but a concurrent reader could see
   a mixed snapshot (there is no versioned pointer), so publication is an
   offline, stopped-app operation. `cache.publish_envelopes()` returns a
   manifest (`version`, `published_at`, `count`, `files`) and rolls back on
   writer failure. It never edits envelopes with `Path.write_text()`.

---

## 7. Integration steps (parent, after this branch lands)

`tools.py` and `config.py` are parent-owned; this module does not edit them.

1. In `tools.LocalSource`, add `forecast()` / `alerts()` thin wrappers around
   `weather.hourly_windows()` and `weather.active_alerts()`.
2. In `_build_itinerary`, after each `walk` leg is created, call
   `weather.assess_leg(leg["start_time"], start + minutes, windows, alerts)` and
   attach `weather_risk = {level, label, reasons, evidence, basis, status}`.
3. Add a plan-level `weather` block from `weather.plan_risk(itinerary["legs"])`,
   carrying `sources`, `stale`, and `reason`.
4. Replace the `# -- weather trigger: NOT IMPLEMENTED YET` comment in
   `_replan_trigger` with: fire cause `"weather"` only when a walk leg's level is
   `high`/`severe` and `status` is not `unavailable`, using `reasons[0]`; keep
   the existing walk-only alternative (Plan B already exists).
5. App: expose `GET /api/weather` → `forecast_strip()` + `active_alerts()` and
   attach `plan_risk` output to `/api/ask`. In replay with no bundle, render the
   `unavailable` state rather than weather.