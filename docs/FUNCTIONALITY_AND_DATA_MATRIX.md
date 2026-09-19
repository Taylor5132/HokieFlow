# HokieFlow — Functionality & Data Availability Matrix

**Purpose:** a Figma-facing specification of what HokieFlow can *actually* show today, what it must
never imply, and what is only a roadmap source. This document describes the **repository as it is**,
not the pitch. If a capability is not backed by code or a fixture in this checkout, it is labelled
as such.

**Brand:** **HokieFlow** is the user-visible product name. Internal identifiers stay `hokieday`
(package `hokieday/`, imports, file names, config keys).

**As verified:** 2026-09-19 (working tree after commit `d813630` "transit map drawn from our own
GTFS geometry + keyless navigation handoff", with the live request-time and feasibility work
present but uncommitted). Offline suite: **249 tests, 0 failures** under `DEMO_MODE=cache`. Live bus
fixture captured **2026-09-19T15:22:29Z**. See §12 for the exact verification commands.

---

## 0. Status legend (use these labels in Figma annotations)

| Status | Meaning | Design consequence |
|---|---|---|
| ✅ **IMPLEMENTED** | In code and exercised by the offline test suite; usable in the demo | Safe to mock with real values |
| 🟡 **PARTIAL** | Works, but bounded: one dining location, one frozen snapshot, unverified coordinates, or backend-only (not surfaced in the UI yet) | Show it, but design the degraded variant |
| 🔵 **IDENTIFIED** | A real public source exists and was researched; **no integration exists in this repo** | Roadmap / "coming later" only. Never render as live |
| ⛔ **UNAVAILABLE / UNSAFE** | No data exists, or presenting it as fact would be dishonest or unsafe | Do not draw it; or draw it explicitly as unknown |

---

## 1. Product truth

### 1.1 The decision HokieFlow helps a student make

> **"In the time I have, starting from where I am, what can I actually do — and is that still true
> given the latest bus snapshot?"**

Concretely, one answer that combines four constraints a student currently checks across separate apps:

1. **Time** — a start and a deadline ("11:22 to 1:25").
2. **Place** — where they are (device GPS or a named building) and where they must end up.
3. **Food** — a meal that fits the window, with diet and allergen constraints applied as *hard* filters.
4. **Transit** — whether a bus beats walking, and whether the live vehicle snapshot invalidates the plan.

The output is a **plan with provenance** (what is live, what is scheduled, what is an estimate) and,
when reality contradicts Plan A, a **re-plan that states exactly what changed**.

### 1.2 What the UI must never imply

| Must never imply | Because |
|---|---|
| An **exact ETA** ("arrives in 4 min") | BT provides positions only; there is **no public GTFS-RT TripUpdates/ETA feed**. Any ETA would be our inference, not observed. |
| **Turn-by-turn / routed walking** | Walk time is haversine straight-line × 1.30 at 1.35 m/s. It is **not** a routed path. |
| **Continuous / smooth bus tracking** | We hold **one frozen snapshot** of 13 vehicles (plus 2 bronze polls). No interpolation, no trajectory. |
| **Broad dining coverage** | Menu/nutrition/hours fixtures exist for **D2 at Dietrick Hall (location 15) only**. |
| **A trained delay model** | No model is trained. `predict_bus_delay` returns `basis: "no_model"`. |
| **A real LLM agent** | The offline text box is a **bounded rule parser**, not a language model. |
| That a plan "fits" when it does not | Now enforced: `feasible: false` + `infeasible_reason` drive a "No plan fits" card, and the rationale never says "fits" or "to spare" when the deadline is missed (§5.9). |
| That a deadline the student gave was silently moved to tomorrow | Live mode returns a `deadline_passed` clarification instead of rolling the time forward (§5.19). |
| That the browser clock is authoritative | Every answer carries a server-captured `_time.evaluated_at`; the client only ticks locally in live mode and stays pinned in replay (§4.8, §6.1). |
| That blank allergens mean safe | Blank means **UNKNOWN**, except in the one documented allergen-free kitchen (Viridian). |

---

## 2. User-job inventory (grouped into coherent flows)

| Flow | User job | Answerable today? | Notes |
|---|---|---|---|
| **A. Plan a gap** | "I have a gap — can I eat and still make class?" | ✅ Yes (D2 + walking) | `plan_day` builds walk → eat → walk, ranks the most filling valid item, returns slack vs deadline |
| **A. Plan a gap** | "What time do I need to leave?" | ✅ Yes | `leave_time`, `arrive_time`, `total_min`, `slack_min` |
| **A. Plan a gap** | "Same trip, but I want the bus" | ✅ Yes, but the bus is usually invalidated | Plan A bus → live re-check → Plan B walk, with cause |
| **A. Plan a gap** | "I need to be there by 1:25" (live, one deadline) | ✅ Yes | Plans from the captured request time to the given deadline; `leave_time = now` |
| **A. Plan a gap** | "I'm hungry" (live, no deadline given) | ✅ Clarify | Returns `need_deadline`; never borrows the frozen demo window |
| **A. Plan a gap** | "Can I make my 1:25?" asked after 1:25 | ✅ Corrected | Returns `deadline_passed`; never silently rolls to tomorrow |
| **B. Monitor / recover** | "Did anything change? Should I switch plans?" | ✅ Yes (bus early/late/full, dining closing) | `replan_trigger` with `cause` + `detail`; Plan A retained |
| **B. Monitor / recover** | "Will the bus actually be there?" | 🟡 Partial | Schedule adherence (`sched_delta_min`) + crowding; no ETA, no future prediction |
| **C. Choose food safely** | "What can I eat given my diet and allergies?" | 🟡 Partial (D2 only) | Hard allergen filter, three-way blank policy, contradiction rejection |
| **C. Choose food safely** | "How many calories / protein?" | ✅ Yes (D2) | Real macros attached to the chosen item |
| **C. Choose food safely** | "Is it open right now / when does it close?" | ✅ Yes (D2) | Two windows; `is_open_now`, `closes_in_min` |
| **D. Navigate** | "Get me from A to B." | ✅ Yes (schematic) | Own GTFS-geometry map + keyless Apple/Google deep links per leg |
| **D. Navigate** | "Turn-by-turn directions" | 🔵 Handed off | We open the phone's maps app; we do not build turn-by-turn |
| **E. Discover later** | "What's happening on campus?" | ⛔ Not built | Events source identified, no scraper |
| **E. Discover later** | Weather / library / gym / campus status / SafeRide / classes | 🔵 Identified | See §7 |

---

## 3. Capability matrix

> **Fields** are the exact keys a UI can bind to today. **Freshness** is the intended refresh, not a
> promise the source will honour. **UI may show** is the honesty boundary.

### 3.1 Transit — static schedule

| User question | Capability / tool | Status | Fields available now | Source | Freshness / refresh | Provenance / confidence | Constraints & known gaps | UI may show |
|---|---|---|---|---|---|---|---|---|
| "When's the next bus from here?" | `get_next_departures(stop_id, route_id, horizon_min)` | ✅ | `stop_id, route_id, head_sign, trip_id, dep_time, in_min, is_realtime` | BT static GTFS (`bt4uclassic.org/gtfs/google_transit.zip`) | Daily (`DEFAULT_GTFS_CACHE_MAX_AGE_S = 24 h`) | `is_realtime` true only when a **non-stale live vehicle runs that exact trip**; else schedule-only | Service must be resolved from `calendar_dates.txt` (no `calendar.txt`); wrong service day yields plausible wrong trips | Scheduled times; "service-filtered schedule" badge |
| "Which stops are near me?" | `nearest_stops`, `gtfs.nearest_stops` | ✅ | `stop_id, name, lat, lon, wheelchair, distance_m` | GTFS `stops.txt` (297) | Daily | Distance is haversine metres | Only 1 of 8 named places (GTFS Stop 1600) has a verified coordinate; official VT GIS invalidated the legacy Burruss point | Stop name + distance; wheelchair flag |
| "What route does the bus drive?" | `gtfs.shape_for_trip` / mapview | ✅ | `shape_id → [(lat, lon)]` (67 shapes, 23–367 pts) | GTFS `shapes.txt` (7,351 pts) | Daily | Real ingested geometry | Route colour exists (`route_color`, 24/24) but the map uses one fixed orange | A schematic route line, labelled "GTFS shape geometry" |
| "How long is the ride?" | `LocalSource.ride_minutes` | ✅ | `route_id, trip_id, dep_time, arrive_time, ride_min` | GTFS `stop_times.txt` (74,301) | Daily | Read off the same trip's stop times | Only where one trip serves both stops in order | "N min ride", "N min wait" |

### 3.2 Transit — live vehicles

| User question | Capability / tool | Status | Fields available now | Source | Freshness / refresh | Provenance / confidence | Constraints & known gaps | UI may show |
|---|---|---|---|---|---|---|---|---|
| "Where are the buses?" | `get_live_bus(route_id)` / `livebus.live` | ✅ | `bus_id, route_id, stop_id, lat, lon, load_pct, is_at_stop, sched_delta_min, gtfs_trip_id, observed_at, is_stale` | BT live vehicles (undocumented Joomla AJAX endpoint) | Live cache window **60 s** (`max_age_s = LIVE_BUS_POLL_SECONDS`); stale > 10 min | `load_pct` from `percentOfCapacity`; `sched_delta_min` = signed minutes late (keystone join 13/13) | One frozen snapshot of 13 vehicles; no public GTFS-RT ETA; `capacity` field is internally inconsistent and must not be used; a bus poll **never forces** the static GTFS download (`load_gtfs(force=False)`) | Static bus dots; route, load %, on-time/early/late, "replayed snapshot" label |
| "Is my bus late?" | `sched_delta_min` | ✅ | signed float minutes (+late, −early, `None` unmatched) | Live + static join | 60 s (live) | Derived from `gtfsTripId → trips.trip_id → stop_times` | Buses in the capture ran 1–8 min **early**; delta is against the pinned clock offline | "1.4 min early", "3 min late" |
| "Is the bus crowded?" | `load_pct` | 🟡 | 0–100 integer | `percentOfCapacity` | 60 s | Direct source field | Fixture range 0–30 %, mean 10 %; full threshold 85 % | Load % / a crowding indicator |
| "When will it arrive?" | — | ⛔ | — | — | — | — | No TripUpdates/ETA feed; we only have positions | **Do not show an ETA** |

### 3.3 Dining

| User question | Capability / tool | Status | Fields available now | Source | Freshness / refresh | Provenance / confidence | Constraints & known gaps | UI may show |
|---|---|---|---|---|---|---|---|---|
| "What's on the menu?" | `dining.menu` | 🟡 (D2 only) | `location_num, date, meal, section, recipe_id, name, description, portion_size, portion_unit, allergens[], diet_tags[]` | VT FoodPro Menu API | Daily (6 h default menu cache) | Source payload, date-stamped | Fixtures only for D2 (`15`) on 09/17 and 09/19; 470 recipes on 09/19 | Dish name, section, meal, portion, diet tags, listed allergens |
| "What can I eat safely?" | `find_food` / `dining.eat_options` | 🟡 (D2 only) | `name, location_num, section, meal, kcal, protein_g, allergens, diet_tags, allergens_known, venue_allergen_free, recipe_id, portion` | Menu + Allergens APIs | Daily / weekly | `avoid` is a **hard** filter; blank = UNKNOWN unless `venue_allergen_free` | 188 of 470 items state no allergens; 42 list nut allergens; 428 do not contain nut allergens | The chosen dish + "hard filter: excludes …" + "allergens UNKNOWN" when applicable |
| "How many calories / protein?" | `nutrition_for_location`, `nutrition_bulk` | ✅ (D2) | `cals, protein_g, fat_g, carb_g, sodium_mg` | VT NutritiveReport API | On demand, cached | Real macros; fixture total for `214022*1*1,141002*2*1` = **479.616 kcal** | Values arrive as strings; missing values sent as `----` → treated as 0; `kcal` is `None` if nutrition missing | kcal + protein with "VT menu API" badge; "—" when unknown |
| "Is it open? When does it close?" | `get_hours` / `dining.hours` / `is_open` | 🟡 (D2) | `foodpro_id, name, date, open_time, close_time, is_open_now, closes_in_min` | VT hours API | Daily | Hours ↔ menu join via `foodpro_id == locationNum` (verified D2 = 15) | Two windows only in the fixture: 09:30:01–15:00:00, 15:00:01–20:00:00 | Open/closed, "closes in N min" |
| "Which dining places exist?" | `dining.locations` | ✅ | `location_num, name` | Locations API (12 entries) | Daily | Source list | `config.DINING_LOCATIONS` names 6; only 2 (`15`, `39`) have map coordinates | A picker of known dining places |

### 3.4 Location & walking

| User question | Capability / tool | Status | Fields available now | Source | Freshness / refresh | Provenance / confidence | Constraints & known gaps | UI may show |
|---|---|---|---|---|---|---|---|---|
| "Where am I starting?" | `resolve_origin` / `config.register_dynamic_place` | ✅ | `source (device/selected/default), label, lat, lon, accuracy_m, km_from_campus, note, rejected_km_from_campus` | Browser Geolocation / place picker | Per request | Device coords are **not** survey-grade; `verified` stays False | Geolocation needs a secure context (localhost/HTTPS); Wi-Fi geolocation beyond 5 km is rejected with a reason | "from your location ±N m" or "from Burruss Hall"; rejection note |
| "How long is the walk?" | `walk_time` / `_walk_result` | 🟡 | `minutes, meters, method, coords_verified` | `config.PLACES` + haversine | Static | Straight-line × 1.30 at 1.35 m/s | 7 of 8 named places are **UNVERIFIED**; not a routed path | "estimated · straight-line × factor"; never a path shape |
| "What's my place list?" | `config.PLACES` | 🟡 | 8 static places (Burruss, Stop 1600, McBryde, Hahn, D2, West End, Owens, Squires) | Hard-coded registry | Static | Only GTFS Stop 1600 is verified; the VT GIS audit disproved the legacy Burruss coordinate | Recalibrate every building/dining coordinate before presenting routed distances | A picker; no building receives a verified ✓ until the GIS integration lands |

### 3.5 Weather, events, models (honest absences)

| User question | Capability / tool | Status | Fields available now | Source | Freshness | Constraints & known gaps | UI may show |
|---|---|---|---|---|---|---|---|
| "Will it rain on my walk?" | — (trigger commented "NOT IMPLEMENTED") | 🔵 | Endpoint constant only (`api.weather.gov/points/{lat},{lon}`) | NWS | — | No fixture, no tool, no plan input | Nothing live; roadmap only |
| "What's happening on campus?" | `get_events` | 🔵 | Always `[]` + reason | `events.vt.edu/events` (HTML, no API) | — | Scraper never built | "No events data" empty state only |
| "Predict bus lateness" | `predict_bus_delay` | ⛔ | `expected_delta_min: None, confidence: 0.0, basis: "no_model"` | — | — | No model; gate is ~2,000 labelled rows; no bus time series is committed (`data/` is runtime-only) | "No prediction available" only |
| "Predict dining wait" | — (SDD listed `predict_dining_wait`) | ⛔ | — | — | — | No ground truth exists; deliberately dropped | Nothing |

### 3.6 Orchestration

| User question | Capability / tool | Status | Fields available now | Constraints & known gaps | UI may show |
|---|---|---|---|---|---|
| "Give me one plan" | `plan_day` | ✅ | `itinerary{legs, leave_time, arrive_time, window_end, total_min, slack_min, arrives_in_window, used_bus, eat_start, eat_end, eat_close_in_min, notes}, rationale, feasible, infeasible_reason, constraints, alternatives, replan_trigger` | Only D2 food; walking estimate; bus chosen only if it beats walking unless `prefer=bus`, but **feasibility beats preference** — a bus that misses the deadline falls back to a walk that fits | The full plan card |
| "Is this plan possible?" | `feasible` + `infeasible_reason` | ✅ | `feasible: bool`; `infeasible_reason.code ∈ {invalid_window, unknown_place, no_legs, deadline_missed, clarification_needed}` plus code-specific fields (`late_by_min`, `field`, `value`, `known_places`, `detail`, `kind`) | Top-level feasibility describes the **chosen** plan (a re-planned Plan B can itself miss); the least-late useful itinerary is still returned for inspection | A "No plan fits" card with the corrective action; never an empty 0-minute itinerary |
| "What changed?" | `_replan_trigger` + Plan A retention | ✅ | `cause (bus_early/bus_late/bus_full/dining_closing), detail`; `alternatives[previous_itinerary_a]` | Weather trigger still absent | Re-plan banner + Plan A/B diff |
| "Ask in free text" | `server.parse_free_text(text, now, live)` | 🟡 | `start, end, prefs{diet, avoid, from_place, to_place, prefer}, _interpretation_notes, _clarification` | **Bounded rule parser, not an LLM**; recognises configured places and a small allergen/negation vocabulary. In live mode it anchors to the captured request `now`, plans from now for a single deadline, and returns `need_deadline` / `deadline_passed` clarifications. In cache mode the frozen demo window is kept byte-for-byte | The text box + "↳ interpreted …" notes; an "I need one detail" card for clarifications |

### 3.7 Request time & clock

| User question | Capability / tool | Status | Fields available now | Source | Freshness / refresh | Provenance / confidence | Constraints & known gaps | UI may show |
|---|---|---|---|---|---|---|---|---|
| "What time is it on campus?" | `GET /api/time` → `server.time_endpoint` | ✅ | `mode, is_replay, time_source, timezone, iso, evaluated_at, clock, human, weekday, pinned, ticking` | Server clock (`config.now`) | Live: 1 s local tick, 30 s re-sync; replay: pinned, no tick | Server is authoritative; the browser never invents a time | Lightweight by design: never loads GTFS, dining, or live buses | A clock chip: "● 10:00:03 AM" (live) or "⏸ pinned 11:22 AM · replay" |
| "Which clock did my plan use?" | `_time` on every `/api/ask` response | ✅ | `mode, evaluated_at, time_source (wall_clock \| snapshot), is_replay` | Captured at the request boundary in `handle_ask` | One capture per request | Every calculated time in the answer derives from this instant; present even on error responses | — | "evaluated at 10:00 AM" provenance; pinned vs live label |
| "Plan from now to my deadline" | `_resolve_live_window` | ✅ | Derives `start = now`, `end = deadline`; adds an interpretation note | Captured request clock | Per request | Live only; a single deadline becomes now → deadline | Two explicit times are honoured as-is; no deadline or a passed deadline becomes a clarification. Applies to presets too: a live preset keeps its explicit deadline but starts at the captured now, not the frozen 11:22 | "Planning from now (10:00 AM) to your 1:25 PM deadline." |

---

## 4. Figma-facing data dictionary

Legend for the Availability column: **A** = available from API/code now · **D** = derived/computed ·
**M** = missing today.

### 4.1 Plan summary card

| Field | Avail. | Type / units | Notes |
|---|---|---|---|
| `leave_time` | A | ISO datetime, campus-local | Render as clock ("11:22 AM") |
| `arrive_time` | A | ISO datetime | Render as clock |
| `window_end` | A | ISO datetime | The deadline |
| `total_min` | D | minutes (1 dp) | Sum of rounded leg minutes |
| `slack_min` | D | minutes (1 dp) | Positive = spare; negative = after deadline |
| `arrives_in_window` | D | boolean | Must drive the headline wording |
| `used_bus` | D | boolean | "bus" / "walk" badge |
| `eat_start`, `eat_end` | A/D | ISO datetime | `eat_end = eat_start + 20 min` (assumed) |
| `eat_close_in_min` | D | minutes | From planned eat start to window close |
| `rationale` | D | string | Human sentence; now states the miss ("35 min after the deadline") instead of "fits" when infeasible |
| `feasible` | D | boolean | Top-level; describes the **chosen** plan |
| `infeasible_reason` | D | object or null | `{code, ...}`; codes in §4.9 |
| `clarification` | D | object or null | Present when the request needs one detail; see §4.10 |
| `constraints.diet`, `.avoid`, `.max_kcal`, `.prefer`, `.from_place`, `.to_place` | A | strings/list | Surface applied constraints |
| `notes[]` | A | list of strings | E.g. "eat leg dropped" |
| `origin` (`_origin`) | A | `{source, label, accuracy_m, note}` | `source ∈ device/selected/default` |
| `_interpretation_notes[]` | D | list of strings | Ambiguity disclosure |
| `_time` | A | `{mode, evaluated_at, time_source, is_replay}` | The request clock every displayed time derives from; see §4.8 |

### 4.2 Itinerary leg

| Leg type | Field | Avail. | Units | Notes |
|---|---|---|---|---|
| all | `seq`, `type` | A | int, `walk\|eat\|bus` | Ordering + icon |
| all | `start_time` | A | ISO datetime | |
| all | `minutes` | A | minutes | |
| walk | `from`, `to` | A | strings | Display names |
| walk | `from_coords`, `to_coords` | A | `(lat, lon)` | For the map |
| walk | `meters` | D | metres | May be absent on some walk legs (stop-to-stop legs omit it) |
| walk | `method` | D | string | "haversine straight-line × 1.30 …" |
| eat | `place`, `location_num`, `coords` | A | string / id / `(lat,lon)` | D2 = `15` |
| eat | `item`, `portion` | A | strings | Dish + portion |
| eat | `kcal`, `protein_g` | A | kcal, grams (1 dp) | `None` if nutrition missing → render "—" |
| eat | `allergens[]` | A | list | Declared allergens |
| eat | `allergens_known` | A | boolean | **False = UNKNOWN**, never safe |
| eat | `venue_allergen_free` | A | boolean | True only for Viridian sections |
| eat | `diet_tags[]`, `requested_diet`, `avoid[]` | A | lists | Show applied constraint |
| eat | `end_time` | D | ISO datetime | |
| eat | `method` | D | string | "assumed eating time (20 min)" |
| bus | `route_id`, `trip_id` | A | strings | |
| bus | `from_stop`, `to_stop`, `from_stop_name`, `to_stop_name` | A | ids + names | Show human names |
| bus | `from_coords`, `to_coords` | A | `(lat,lon)` | |
| bus | `dep_time`, `arrive_time` | A | ISO datetime | |
| bus | `wait_min`, `ride_min` | D | minutes | |
| bus | `is_realtime` | A | boolean | **Currently hard-coded `False`** in plans — the planner uses live data only for the re-plan trigger |
| bus | `method` | D | string | "GTFS schedule (service-filtered)" |

### 4.3 Bus marker + detail sheet

| Field | Avail. | Units | Notes |
|---|---|---|---|
| `bus_id` | A | string | e.g. `6413` |
| `route_id` | A | string | e.g. `SME`; joins `routes.route_color` (24/24) but **colour is unused today** |
| `lat`, `lon` | A | degrees | Single observed point |
| `load_pct` | A | 0–100 | From `percentOfCapacity` |
| `is_at_stop` | A | boolean | From `isBusAtStop == "Y"` |
| `sched_delta_min` | D | minutes (signed) | + late, − early |
| `observed_at` | A | UTC datetime | From `states[-1].version` (epoch ms) |
| `is_stale` | D | boolean | Age > 10 min against `config.now()` |
| `gtfs_trip_id`, `stop_id` | A | strings | Join keys |
| `speed`, `passengers` | A | m/s?, count | Present in raw payload; **not surfaced by the tool** |
| `isProjected`, `isGenerated` | M | booleans | Present in raw `states[]`, dropped in `normalize`; not shown |

### 4.4 Dining option card

| Field | Avail. | Units | Notes |
|---|---|---|---|
| `name` | A | string | |
| `location_num`, `location` | A | id / string | D2 = `15` |
| `meal`, `section` | A | strings | Meal period + station |
| `portion` | A | string | `portion_size portion_unit` |
| `kcal` | A | kcal (1 dp) | `None` → "—" |
| `protein_g` | A | grams | |
| `allergens[]` | A | list | Declared only |
| `diet_tags[]` | A | list | vegan/vegetarian/halal/alcohol |
| `allergens_known` | A | boolean | False = UNKNOWN |
| `venue_allergen_free` | A | boolean | Viridian |
| `description` | A | string | Available in menu payload |
| `fat_g`, `carb_g`, `sodium_mg` | A | g / mg | Available but not shown today |
| `price` | A | — | Present in raw menu; not surfaced |
| `open_now`, `closes_in_min` | A/D | boolean, minutes | Location-level, not per item |

### 4.5 Source / freshness badge

| Field | Avail. | Units | Notes |
|---|---|---|---|
| `mode` (`live`/`cache`) | A | string | From `/api/status` |
| `offline` | D | boolean | `DEMO_MODE=cache` |
| `evaluated_at` | A | ISO datetime | The one server instant per request (from `/api/time`, or `_time` on an answer) |
| `time_source` | A | `wall_clock` \| `snapshot` | Live vs pinned replay |
| `is_replay` / `pinned` / `ticking` | A | booleans | `/api/time` clock-chip state |
| `human`, `clock`, `iso`, `weekday`, `timezone` | A | strings | `/api/time` display fields |
| `campus_now` | A | datetime | `/api/status` campus time; pinned in replay mode |
| `clock_pinned_to_snapshot` | D | boolean | |
| `wall_clock` | A | datetime | Real time, for the "snapshot captured …" copy |
| `live_vehicles` | A | int | 13 in the frozen fixture |
| `live_stale` | A | boolean | Any row > 10 min |
| `fixture_age_hours` | D | hours | Uses real elapsed wall-clock age (3.6 h at verify) |
| `fetched_at` | A | ISO datetime | Cache envelope; real write time |
| `observed_at` | A | ISO datetime | Source observation time |
| `is_realtime` | A | boolean | Per departure/leg |
| `stale` / `reason` | A | bool / string | Tool-level degradation signal |
| `method`, `basis`, `coords_verified` | D | strings | Estimation provenance |

### 4.6 Location / origin

| Field | Avail. | Units | Notes |
|---|---|---|---|
| `source` | A | `device\|selected\|default` | |
| `label` | A | string | "your location" / place name |
| `lat`, `lon` | A | degrees | |
| `accuracy_m` | A | metres | From device only |
| `km_from_campus` | D | km | |
| `note` | D | string | Rejection/accuracy warning |
| `rejected_km_from_campus` | D | km | Set when Wi-Fi geolocation rejected |
| `verified` | A | boolean | False for all dynamic places |
| `dynamic` | A | boolean | True for device-registered places |

### 4.7 Re-plan diff

| Field | Avail. | Notes |
|---|---|---|
| `replan_trigger.cause` | A | `bus_early`, `bus_late`, `bus_full`, `dining_closing` |
| `replan_trigger.detail` | D | Human sentence with numbers, e.g. "Route CAS is 1.4 min early, leaving only 1.5 min to board at Tennis Courts; the plan requires a 2 min buffer." |
| `alternatives[type=previous_itinerary_a]` | A | Full Plan A itinerary, retained for the diff |
| `alternatives[type=walk_direct]` | A | `minutes`, `meters`, `method` |
| Map `overlay` | D | Plan A bus geometry drawn dashed grey underneath Plan B |
| `_map_error` | A | Set when the map fails; plan still renders |

### 4.8 Request time & clock objects

`GET /api/time` (clock chip) and `_time` (per-answer provenance). Also `_request.start` / `_request.end`
carry the resolved window.

| Field | Source | Avail. | Units / values | Notes |
|---|---|---|---|---|
| `evaluated_at` | `/api/time`, `_time` | A | ISO datetime | The single server instant the answer used |
| `iso` | `/api/time` | A | ISO datetime | Same instant, chip binding |
| `human` | `/api/time` | A | string | e.g. `11:22 AM` |
| `clock` | `/api/time` | A | string | e.g. `11:22:29` |
| `weekday` | `/api/time` | A | string | e.g. `Sat 19 Sep 2026` |
| `timezone` | `/api/time` | A | string | `America/New_York` |
| `time_source` | both | A | `wall_clock` \| `snapshot` | Live vs replay |
| `is_replay` | both | A | boolean | True in `DEMO_MODE=cache` |
| `pinned` / `ticking` | `/api/time` | A | booleans | Replay: `true`/`false`; live: `false`/`true` |
| `mode` | both | A | string | `live` \| `cache` |

### 4.9 Infeasible reason dictionary (`infeasible_reason`)

| `code` | Extra fields | Meaning | UI copy source |
|---|---|---|---|
| `deadline_missed` | `late_by_min` (number) | The chosen plan arrives after the deadline; a late best effort is retained for inspection | "No plan makes that deadline — the closest option arrives about N min late." |
| `unknown_place` | `field` (`from_place`/`to_place`), `value`, `known_places[]`, optional `unknown[]` | Origin or destination is not a configured place; **no itinerary** | "I don't know that starting place (Narnia). Known places: …" |
| `no_legs` | `detail` or `known_places[]` | A usable route could not be built (e.g. same origin/destination, no meal) | "I couldn't build a usable route between those places." |
| `invalid_window` | `detail` | Unparseable or reversed window (`end <= start`) | "That deadline is before the start time…" |
| `clarification_needed` | `kind` (`need_deadline`/`deadline_passed`/`allergen_unparsed`) | The request needs one detail before planning | The clarification question (§4.10) |

### 4.10 Clarification object (`clarification`)

| Field | Avail. | Notes |
|---|---|---|
| `kind` | A | `need_deadline` (live free text with no time), `deadline_passed` (free text or preset whose deadline is past), or `allergen_unparsed` (an allergy/restriction contains an ingredient outside the known taxonomy) |
| `question` | A | Short question, e.g. "What time do you need to arrive by?" |
| `detail` | A | Why it is asking, e.g. "I will not silently move it to tomorrow" |
| `now` | A | The captured campus time, e.g. `2:00 PM` |
| `deadline` | A | Present for `deadline_passed` |

---

## 5. State inventory (layout-independent)

| # | State | Trigger | Required user-facing message / data | Current implementation |
|---|---|---|---|---|
| 5.1 | **Initial / empty** | App load before any ask | Prompt + example scenario chips + input | 🟡 No true empty state: `boot()` auto-runs the "eat" scenario. The loading card appears immediately. An explicit first-run state is **missing** |
| 5.2 | **Permission request** | Tap "📍 Locate" | Explain why location is wanted; offer the place picker | 🟡 Browser prompt only; no pre-permission rationale. Place picker is the fallback |
| 5.3 | **Permission denied / unavailable** | Geolocation error or non-secure context | Plain reason + usable fallback | ✅ Button text: "📍 denied" / "📍 unavailable" / "📍 needs HTTPS" / "no location"; plan still runs from a named place |
| 5.4 | **Loading** | Any ask | "Checking dining, transit, and the latest vehicle snapshot…" | ✅ `aria-busy=true`, controls disabled, loading card |
| 5.5 | **Live** | `DEMO_MODE=live` | "LIVE MODE" chip; ticking campus clock; per-leg "live vehicle" when realtime | 🟡 Chip exists and the clock ticks from `/api/time` (1 s local, 30 s re-sync). Bus legs are still hard-coded `is_realtime: False`, so a chosen leg always reads "scheduled" (B3 open) |
| 5.6 | **Offline replay** | `DEMO_MODE=cache` | "OFFLINE REPLAY" chip + "snapshot was captured live and is replayed here"; clock visibly pinned, not ticking | ✅ Chip + `⏸ pinned … · replay` clock; `is_replay`/`pinned` on `/api/time` |
| 5.7 | **Fresh / stale / schedule-only** | `observed_at` age vs 10 min | Stale banner; bus badge flips to "scheduled · service-filtered" | ✅ `live_stale` chip + `is_stale`; plan badge currently always schedule-only |
| 5.8 | **Partial source failure** | Cache miss, fetch fail, bad location | Named degraded reason; plan still returns | 🟡 Backend returns `[] + reason` and cache falls back to the last copy; nutrition is partial-tolerant; map failure isolated. Reasons are only sometimes surfaced in the UI (via `notes` in the rationale) |
| 5.9 | **No feasible plan** | Deadline too tight; unknown place; same origin/destination with no meal; invalid window | "No plan fits" + closest option + what to relax | ✅ Implemented: top-level `feasible: false` + `infeasible_reason` (`deadline_missed`, `unknown_place`, `no_legs`, `invalid_window`); a late best effort is retained for inspection; a zero-leg itinerary is never rendered as valid |
| 5.10 | **Ambiguous input** | Free text with ambiguous times/places | "Interpreted 1:25 as 1:25 PM." | ✅ `_interpretation_notes` rendered as "↳ …"; unknown buildings redirect to the place picker via `unknown_place`; live missing/passed deadlines become a first-class `clarification` card |
| 5.11 | **Safety unknown** | Item has blank allergens, or a stated allergy cannot be mapped confidently | "allergens UNKNOWN" for source data; "Which ingredient should I avoid?" for unparsed input | ✅ Blank fields render from `allergens_known: false`; unknown or partially parsed allergy lists return `clarification.kind = allergen_unparsed` and no itinerary |
| 5.12 | **Contradictory source data** | Diet tag contradicts declared allergens | Exclude the row; show source fields for the chosen row | ✅ `diet_allergen_conflicts` rejects (e.g. vegan + Eggs); UI shows diet tag + listed allergens |
| 5.13 | **Re-planned** | Bus early/late/full, or dining closing | "RE-PLANNED — <cause>", the numeric detail, Plan A beside Plan B | ✅ Banner + both plans + map overlay |
| 5.14 | **Navigation handoff** | User taps a map link | Per-leg Apple/Google link; "your maps app supplies turn-by-turn" | ✅ Deep links per walk/bus leg; opens externally |
| 5.15 | **No live vehicles** | Empty snapshot | "No live vehicles" + schedule fallback | 🟡 `get_live_bus` returns `count: 0` + reason; not a dedicated visual state |
| 5.16 | **Events unavailable** | Any events ask | "Events feed not built" | 🔵 Always `[]` + reason; no UI surface today |
| 5.17 | **Live from-now planning** | Live request with a future deadline (free text or preset) | "Planning from now (10:00 AM) to your 1:25 PM deadline." | ✅ `start = evaluated_at`, `end = deadline`; interpretation note shown; a live preset uses the captured now instead of the frozen 11:22 start |
| 5.18 | **Missing deadline** | Live free text with no time | "What time do you need to arrive by?" | ✅ `clarification.kind = need_deadline`; no itinerary, no borrowed demo window |
| 5.19 | **Deadline already passed** | Live free text or preset whose deadline is in the past | "<time> has already passed today. Do you mean a later time, or tomorrow?" | ✅ `clarification.kind = deadline_passed` (free text and preset guard); never silently rolled to tomorrow |
| 5.20 | **Clarification needed** | Missing/passed deadline or an allergy clause that cannot be mapped completely | A one-detail question + detail | ✅ "I need one detail" card; `feasible: false` with `infeasible_reason.code = clarification_needed`; no itinerary is produced |

---

## 6. Refresh & time model

### 6.1 One request clock, one replay clock (the core correctness rule)

`handle_ask` captures **one** campus-local `now` at the request boundary and threads it through
parsing and planning. Every displayed time derives from it, exposed as `_time.evaluated_at`; the
browser clock is never consulted. `GET /api/time` exposes the same server clock cheaply for the UI
chip.

| Clock | Live mode | Replay mode (`DEMO_MODE=cache`) | Source |
|---|---|---|---|
| `config.now()` | Real wall clock (UTC) | **Pinned to the bus snapshot's `fetched_at`** (override with `DEMO_NOW`) | `config._resolve_pin()` |
| Request `now` | Captured once per request for parse + plan | Same, but the pinned value | `handle_ask` → `time_meta` |
| `cache` freshness age | Real elapsed time | **Still real elapsed time** | `cache.age_seconds` uses `datetime.now(utc)` |
| Source `observed_at` | Bus `states[-1].version` (epoch ms → UTC) | Same frozen value | `livebus.normalize` |
| `fetched_at` | When we wrote the cache envelope | Original capture stamp | `cache._write_envelope` |

Verified pin: `clock = 2026-09-19T15:22:29+00:00`, `campus_now = Sat 19 Sep 2026 11:22`. Without the
pin, a schedule delta grows one minute per real minute; a 6.7-minute gap was measured to inflate all
13 live deltas to a +7.73 min mean. In replay the clock chip is **pinned and does not tick**; in live
mode the client advances the last synced instant by locally elapsed time and re-syncs every 30 s
(never from the heavy `/api/status`).

### 6.2 Refresh expectations

| Source | Intended refresh | Enforced constant | Notes |
|---|---|---|---|
| Live buses (live mode) | 60 s cache window | `config.LIVE_BUS_POLL_SECONDS` passed as `max_age_s` | `livebus.fetch_vehicles` reuses a snapshot younger than 60 s; `force=True` (an explicit tap) bypasses it. A bus poll never forces the static GTFS (`load_gtfs(force=False)`) |
| Live buses (`DEMO_MODE=cache`) | fixture only | freshness argument is ignored | Replay always reads the frozen fixture; staleness is measured against the pinned clock |
| Live staleness | label schedule-only after 10 min | `STALE_LIVE_MINUTES = 10` | `livebus._is_stale` accepts the supplied request clock |
| Campus clock chip | 30 s re-sync; 1 s tick in live | — | `GET /api/time`; pinned/no-tick in replay |
| Static GTFS | daily | `DEFAULT_GTFS_CACHE_MAX_AGE_S = 24 h` | |
| Menus | daily | `DEFAULT_MENU_CACHE_MAX_AGE_S = 6 h` | Fixtures exist for 09/17 and 09/19 |
| Hours | daily | none enforced | Two windows in the D2 fixture |
| Allergens | weekly (per SDD) | none enforced | 10 allergens, 4 diet categories |
| Nutrition | on demand, cached | chunked (40) | Whole-menu cache per location/day |
| Weather | 15 min (roadmap) | — | Not integrated |
| Events | 6 h (roadmap) | — | Not integrated |

### 6.3 Cache vs live mode

| Mode | Reads | Writes | Network |
|---|---|---|---|
| `DEMO_MODE=cache` | `fixtures/` only (frozen) | Never (derived writes are a no-op) | **Never** |
| `DEMO_MODE=live` (default) | `cache/` when fresh | `cache/`; appends bronze | Yes; falls back to the last cached copy on failure |

`cache/` and `fixtures/` are deliberately separate: a live poll once overwrote the fixture the tests
read and silently changed expected values mid-session. The UI takes its clock from `/api/time`
(re-synced every 30 s in live mode, advanced locally between syncs) rather than from the browser
clock; replay is pinned; and the heavier `/api/status` is fetched once at boot.

### 6.4 Why smooth bus movement cannot be called "observed"

- The public repository holds **one frozen snapshot** of 13 vehicles
  (`fixtures/bt_buses.json`, `fetched_at 2026-09-19T15:22:29Z`). The runtime
  `data/` directory is intentionally ignored, so a local poller's sample count
  is not reproducible evidence for a fresh clone and is not a shipped dataset.
- Each marker is a **single observed point** with `lat/lon` and an `observed_at` timestamp.
- `normalize()` takes only `states[-1]`; it does **not** interpolate or project. The raw
  `isProjected`/`isGenerated` flags are dropped.
- Therefore the UI may draw **static dots with an observed-at time**, never a moving vehicle, a
  trail, or a heading inferred from two samples. Consecutive samples at a known cadence are required
  before motion is an observation rather than an inference. The new 60 s live-cache freshness window
  changes how often a snapshot may be *reused*, not how many samples we hold; this limit stands.

---

## 7. Data availability by domain

| Domain | What exists in this repo | Status | Official / public source | Refresh | Access risk | UI-allowed |
|---|---|---|---|---|---|---|
| **Transit (static)** | GTFS ingested, 297 stops / 24 routes / 3,658 trips / 74,301 stop_times / 181 calendar_dates / 67 shapes | ✅ | `http://www.bt4uclassic.org/gtfs/google_transit.zip` | Daily | Documented, stable; no SLA | Schedule, stops, route geometry |
| **Transit (live)** | 13 vehicles, load %, schedule delta, 13/13 join | ✅ | BT internal Joomla AJAX `ridebt.org/...method=getBuses` | 60 s | **Undocumented, no SLA, may change**; no public GTFS-RT ETA | Static dots, crowding, early/late, replay label |
| **Dining** | D2 only: 470 recipes, allergens, hours, whole-menu nutrition | 🟡 | `foodpro.students.vt.edu/menus/API/*`, `apps.students.vt.edu/hours/...` | Daily / weekly | No SLA; menu fails silently on wrong date format | D2 dish, macros, hours, allergen status |
| **Location / walking** | 8 named places (1 verified) + device origin | 🟡 | Hard-coded registry + browser geolocation | Per request / static | Building coordinates await VT GIS calibration; geolocation needs HTTPS | Origin label ± accuracy; walk estimate |
| **Weather** | Endpoint constant only | 🔵 | NWS `https://api.weather.gov/points/{lat},{lon}` | 15 min (proposed) | Documented, no key; not wired | Roadmap only — do not show a forecast |
| **Events** | None | 🔵 | `https://events.vt.edu/events` (server-rendered HTML, no API) | 6 h (proposed) | Brittle scrape; no API | Roadmap only — empty state today |
| **Classes / calendar** | None; "1:25" is parsed from text | 🔵 | Banner timetable `https://selfservice.banner.vt.edu/ssb/HZSKVTSC.P_DispRequest` | Per term | Public search UI; scraping/ToS risk | Roadmap only — never claim a real class schedule |
| **Library / study** | None | 🔵 | `https://lib.vt.edu/about-us/hours.html`; bookings `https://kiosk.lib.vt.edu/bookings/`; space search `https://calendar.lib.vt.edu/reserve/group-study` | Daily | HTML/booking systems; no documented API | Roadmap only |
| **Gym occupancy** | None | 🔵 | Rec Sports Connect2 live counts, e.g. `https://www.connect2mycloud.com/Widgets/Data/locationCount?type=bar&key=874fbaa7-d67f-48a0-8aa5-6d4ebbdc0b08` | ~minutes | Undocumented widget endpoint; per-facility keys | Roadmap only |
| **Campus status / closures** | None | 🔵 | `https://www.vt.edu/status.html`; impacts `https://www.facilities.vt.edu/campus-impacts.html`; closures viewer `https://experience.arcgis.com/experience/2f37a5b71b2e41c4b0f5522eddc4b262` | On change | Status page is not a clean API; ArcGIS viewer is JS-rendered | Roadmap only |
| **SafeRide** | None | 🔵 | `https://police.vt.edu/vtpd-services/safe-ride.html` (nighttime escort; phone 540-231-7233; TransLoc Rider app) | On request | No public availability API; phone/app is the interface | Roadmap only — link/phone, never a live ETA |

> **Rule:** everything marked 🔵 must be drawn as future/roadmap. It must not appear in a mockup with
> live-looking values, timestamps, or "now" language.

---

## 8. Ranked data / functionality backlog

### 8.1 Backend / data work (unlocks new UI states)

**Completed in this pass (was the top of this backlog):** explicit feasibility (B1), the live
request-time model (`handle_ask`/`_time`, `/api/time`), and the 60 s live-bus cache window without
forcing GTFS. B1 and P1 are therefore closed below.

| Rank | Item | Why it unlocks Figma states | Acceptance criteria | Depends on |
|---|---|---|---|---|
| B1 | ✅ **DONE — explicit "no feasible plan" state** | Removes the dishonest "fits" headline; adds a real failure design | Shipped: top-level `feasible` + `infeasible_reason` (`deadline_missed`/`unknown_place`/`no_legs`/`invalid_window`); a late best effort is retained; a zero-leg itinerary is never returned as valid; the rationale never says "fits"/"to spare" when late | — |
| B2 | **Multi-location dining fixtures** (at least Owens `39`, Hokie Grill `09`) | Broadens the food picker and "closing soon, try X" re-plan | Menus + hours + nutrition for 2+ non-D2 locations; `find_food(location_num=None)` returns rows for >1 location | Data capture + seed |
| B3 | **Attach live state to the chosen bus leg** | Enables a "live vehicle" badge on the plan, not just in the header | Chosen bus leg carries real `is_realtime`, `load_pct`, `sched_delta_min`, `observed_at` | — |
| B4 | **Consecutive bus samples + a stated staleness policy** | Justifies any future motion/heading; strengthens the replay label | ≥ N samples at 60 s cadence persisted; UI exposes "observed at" and refuses to animate from one sample | Poller running |
| B5 | **Weather integration** (NWS) | Unlocks the rain-on-walk re-plan trigger and a weather badge | Forecast fetched through `cache.get_json`; `_replan_trigger` fires `weather` with a numeric precip probability | NWS |
| B6 | **Events integration** (scrape) | Unlocks the "discover later" flow | ≥1 day of events parsed to `{title, start, place, tags, url}`; cached; degrades to `[]` | events.vt.edu |
| B7 | **Campus status / closures** | Unlocks a safety banner and route-avoidance copy | Status + active closures parsed; UI banner when active | vt.edu/status, ArcGIS |
| B8 | **Library hours** | Unlocks a study-spot job | Hours for Newman/others; `is_open_now`, `closes_in_min` | LibCal/HTML |
| B9 | **Gym occupancy** | Unlocks "how busy is the gym" | Live counts per facility with `observed_at` | Connect2 |
| B10 | **Class timetable** | Turns "1:25" from text into a real schedule | Read-only lookup with a clear ToS decision; never in the default demo | Banner |
| B11 | **Trained lateness model** | Replaces "no_model" with a defensible prediction | ≥2,000 labelled rows, MLflow run, batch scores; UI shows basis + confidence | B4 |
| B12 | **SafeRide availability** | Unlocks a nighttime safety option | A documented data path exists; otherwise ship phone/app link only | VTPD |

### 8.2 Presentation-only work (no new data required)

| Rank | Item | Why | Acceptance criteria |
|---|---|---|---|
| P1 | ✅ **DONE — fix the "fits" contradiction** | Headline must agree with the plan card | Shipped: `infeasibleCopy` renders "No plan makes that deadline … N min late" / "No plan fits"; the rationale never says "fits" when infeasible |
| P2 | **Dedicated empty / first-run state** | Today the app auto-runs a scenario | A designed empty state with prompt + chips |
| P3 | **Route colours from GTFS** | `route_color` exists for 24/24 routes but the map is one orange | Map/legend use the route's own colour; contrast checked |
| P4 | **Per-source freshness badges** | Central to the honesty story | Every number carries live / scheduled / estimated; a stale variant exists |
| P5 | **Partial-failure banner** | Make degradation visible | A banner names the failed source and the fallback |
| P6 | **"No live vehicles" state** | Avoids an empty header chip | A designed schedule-only state |
| P7 | **Safety-unknown styling** | "UNKNOWN" must not read as a warning only | A distinct, accessible treatment for UNKNOWN vs listed-allergen vs allergen-free |
| P8 | **Navigation-handoff disclaimers** | Prevent "we navigate" claims | Copy says the maps app navigates; per-leg links match the plan |
| P9 | **Accessibility text for every component** | Required for handoff (§10) | Each component names its a11y label and live-region behaviour |
| P10 | **Interpretation-notes treatment** | Ambiguity should be visible, not buried | "↳ interpreted …" rendered near the input, with the assumed value |
| P11 | ✅ **DONE — campus clock chip** | Users need to trust the time the plan used | Shipped: `/api/time` chip ticks in live mode and stays pinned in replay; clarifications render as "I need one detail" |

---

## 9. Allowed claims / prohibited claims

| Claim area | ✅ Allowed to say / show | ⛔ Prohibited |
|---|---|---|
| Bus timing | "Scheduled departure", "running ~1.4 min early", "schedule-filtered", "from a snapshot captured 11:22" | "Arrives in 4 min", "exact ETA", any countdown |
| Bus movement | Static markers with `observed_at`; "replayed snapshot" | Animated/smooth tracking, trails, headings, "live tracking" (still no consecutive samples; §6.4) |
| Request time | "evaluated at 10:00 AM"; "planning from now to your 1:25 PM deadline"; replay pinned to a captured snapshot | Browser-clock times; presenting a replay clock as live; silently moving a passed deadline to tomorrow |
| Feasibility | "No plan makes that deadline — the closest arrives about 35 min late"; "I don't know the starting place 'Narnia'" | "Yes, it fits" when `feasible` is false; rendering a zero-leg itinerary as a valid plan |
| Walking | "~8 min, straight-line × 1.30 estimate" | "Routed walk", "turn-by-turn", a path-shaped walk line |
| Dining | "D2 at Dietrick Hall", "470 items on 2026-09-19", "hard filter excludes Peanuts" | "All VT dining", "allergen-free" for unknown items, menu coverage beyond D2 |
| Allergens | "Declared allergens", "UNKNOWN", "documented allergen-free kitchen (Viridian)" | Blank = safe; "certified allergen-free" outside Viridian |
| ML | "No model yet; acting on observed deviation" | "Our model predicts lateness/wait" |
| Agent | "Bounded offline parser today; agent layer maps language to governed tools" | "A real LLM agent answers this" in the offline demo |
| Navigation | "We plan the trip; your maps app navigates it" | "Built-in turn-by-turn" |
| Weather / events / classes / gym / library / status / SafeRide | "Roadmap" | Any live-looking value or "now" state |
| Provenance | Per-number live / scheduled / estimated tags | A number with no source tag |

---

## 10. Figma handoff checklist

Every component must name all five before it is marked ready.

| Component | Data dependency | Freshness state | Missing-data state | Action | Accessibility text |
|---|---|---|---|---|---|
| Answer headline | `rationale` / `feasible` / `infeasible_reason` / `arrives_in_window` / `replan_trigger` | pinned clock shown (`is_replay`) | `infeasible_reason` copy ("No plan fits") | — | Live region announcement |
| Campus clock chip | `/api/time` (`iso`, `human`, `is_replay`, `pinned`, `ticking`) | ticks live; frozen in replay | "clock unavailable" (keep last known) | — | "Campus clock, replay pinned to 11:22 AM" |
| Clarification card | `clarification` (`kind`, `question`, `detail`) | from the request clock | n/a | answer the question | "One detail needed: what time do you need to arrive by?" |
| "No plan fits" card | `feasible: false` + `infeasible_reason` | pinned clock shown | this **is** the missing-data state | pick a known place / later time | "No plan fits; closest arrives 35 minutes late" |
| Plan summary card | plan fields (§4.1) | live vs replay chip | empty-itinerary state | none | "Plan leaves at …, arrives at …" |
| Itinerary leg row | leg fields (§4.2) | per-leg provenance tag | "—" for missing kcal | none | "Walk 8 minutes to D2" |
| Bus marker | bus fields (§4.3) | observed_at + stale | no-vehicle state | tap → detail sheet | "Bus 6413 on route SME, 30% full" |
| Bus detail sheet | bus fields + route colour | observed_at + replay | no-vehicle state | close | Sheet title + close label |
| Dining option | dining fields (§4.4) | menu date | UNKNOWN vs listed vs allergen-free | none | "Cheesy Baked Potato Soup, 226 kcal, contains Milk, Wheat" |
| Source/freshness badge | badge fields (§4.5) | live / scheduled / estimated / stale | "source unavailable" | none | "Data from VT menu API, scheduled" |
| Location / origin | origin fields (§4.6) | per request | default / rejected note | "Locate" + picker | "Starting from your location, accurate to ±12 m" |
| Re-plan banner | trigger + Plan A | replay evidence line | no re-plan → hidden | view Plan A | "Re-planned because the bus is early" |
| Map | itinerary coords + GTFS shapes | schematic label | `_map_error` fallback | open in maps | "Schematic route map; walk lines are estimates" |
| Navigation links | `_links[]` | per leg | no-links state | opens external app | "Open Apple Maps to walk to D2" |
| Scenario chips | `/api/scenarios` | static | chips unavailable | runs request | Button role + label |
| Assumptions panel | `/api/status.assumptions` + caveats | static | status unavailable | expand/collapse | "Assumptions and data limits" |

---

## 11. Appendix — evidence map

| Claim | Where it lives |
|---|---|
| 249 offline tests pass | `python3 -m unittest discover -s tests` |
| 13 vehicles, one snapshot | `fixtures/bt_buses.json` (`fetched_at 2026-09-19T15:22:29Z`) |
| No committed bus time series | `.gitignore` excludes `data/`; only the single `fixtures/bt_buses.json` observation is shipped |
| 470 recipes, 188 blank allergens, 42 nut, 174 veg, 231 vegan | `fixtures/dining_menu__dtdate=09-19-2026__location_num=15.json` |
| D2 = location 15; hours windows | `fixtures/dining_hours__date=2026-09-19__foodpro_id=15.json`, `config.DINING_LOCATIONS` |
| Route colours 24/24, unused | Committed `fixtures/bt_gtfs.bin`, verified through `gtfs.load_gtfs().routes`; no `route_color` reference in `hokieday/` or `app/` |
| Bus leg `is_realtime` hard-coded False | `hokieday/tools.py` `_build_itinerary` |
| Weather trigger absent | `hokieday/tools.py` `_replan_trigger` comment |
| Events always empty | `hokieday/tools.py` `get_events`, `EVENTS_REASON` |
| No ML model | `hokieday/tools.py` `predict_bus_delay` |
| Bounded parser, not LLM | `app/server.py` `parse_free_text` |
| Feasibility guards (closed gap) | `tests/test_feasibility.py`: tight window → `feasible:false` + `deadline_missed`; unknown place → no itinerary; rationale never "fits" |
| Request-time + clock + bus-freshness guards | `tests/test_time.py`: `/api/time` lightweight; `_time` on every ask; live from-now/clarifications; replay pinned; 60 s bus cache; no forced GTFS |
| `/api/time` and `_time` exist | `app/server.py` `time_endpoint` / `time_meta` / `handle_ask`; `GET /api/time` |

---

## 12. Verification commands

Run from the repo root. All are read-only; none modify the repository.

```bash
# 1. Offline suite (no network, pinned clock)
DEMO_MODE=cache python3 -m unittest discover -s tests -v        # -> Ran 249 tests, OK
DEMO_MODE=cache python3 -m unittest tests.test_time tests.test_feasibility -v

# 2. Fixture and snapshot counts
ls fixtures/*.json fixtures/*.bin | wc -l                       # -> 22
python3 -c "import json;d=json.load(open('fixtures/bt_buses.json'))['payload']['data'];print(len(d))"   # -> 13
# data/ is runtime-only and ignored; no bus time series is shipped.

# 3. Dining coverage
python3 - <<'PY'
import json
m=json.load(open('fixtures/dining_menu__dtdate=09-19-2026__location_num=15.json'))['payload']['meals']
items=[r for x in m for s in (x.get('sections') or []) for r in (s.get('recipes') or [])]
print(len(items),
      sum(1 for r in items if not str(r.get('allergens','')).strip()),
      sum(1 for r in items if any(k in str(r.get('allergens','')).lower() for k in ('nut','peanut'))))
PY
# -> 470 188 42

# 4. Status / provenance surface
DEMO_MODE=cache python3 -c "from app import server; import json; print(json.dumps(server.status(), indent=1, default=str))"

# 5. Feasibility (the gap this doc used to record is closed)
DEMO_MODE=cache python3 -c "
from hokieday import tools
r=tools.plan_day('demo-student-1','11:22','11:23')
print(r['feasible'], r['infeasible_reason']); print(r['rationale'])
u=tools.plan_day('demo-student-1','11:22','13:25',{'from_place':'Narnia'})
print(u['feasible'], u['infeasible_reason']['code'], u['itinerary'])
"

# 6. Request clock and /api/time
DEMO_MODE=cache python3 -c "
from app import server
from datetime import datetime
from zoneinfo import ZoneInfo
from hokieday import config
TZ=ZoneInfo(config.CAMPUS_TZ)
print(server.time_endpoint(now=datetime(2026,9,19,11,22,29,tzinfo=TZ), live=False))
r,_=server.handle_ask({'text':'from 11:22 to 11:23'})
print(r['feasible'], r['infeasible_reason'], r['_time'])
"
```

---

*End of document. Brand: HokieFlow; internal package/identifiers remain `hokieday`.*
