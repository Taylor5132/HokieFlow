# INTERFACES — frozen contract for Tier 1

**Owned by the integrator. Workers implement against this; workers do not change it.**
If a signature here is wrong, stop and report it — do not silently redesign.

Everything below is in `hokieday/` (import as `from hokieday import gtfs`).

---

## 0. Non-negotiable rules for every worker

1. **Do not edit `hokieday/config.py` or `hokieday/cache.py`.** Parent owns them.
2. **All library HTTP goes through `cache.get_json(...)`, `cache.get_bytes(...)`,
   or `cache.post_form_json(...)`.** `post_form_json` is the sanctioned form-POST
   path for cache-identified computational APIs such as the VT GIS route solve;
   its identity includes the exact URL and encoded body digest. Never import
   `urllib`/`requests` in `hokieday/*.py` — doing so bypasses the offline demo
   path (`DEMO_MODE=cache`), which is a Tier 1 acceptance criterion.
3. **Do not edit another worker's file.** Three workers run in parallel in this
   checkout on disjoint files.
4. **Tests must pass with no network.** Use stdlib `unittest` — **pytest is NOT
   installed** on this machine (Python 3.14.7, no pandas either). Run only your
   own module, because other workers' files may be mid-write:
   ```bash
   DEMO_MODE=cache python3 -m unittest tests.test_gtfs -v
   ```
5. **Stdlib only** in `hokieday/*.py`. No pandas, no requests, no pyspark.
   (Reason: the same code must run in a notebook, in an App, and locally — and
   none of those are installed here anyway.)
6. **Do not `git commit`.** Parent reviews and commits.
7. Report at the end: files written, exact test command, exact observed output.

## 0.1 Cache fixtures available (real payloads, captured 2026-09-19)

| cache name | params | contents |
|---|---|---|
| `bt_gtfs` | — | GTFS zip as `.bin`; also extracted into `cache/gtfs/` |
| `bt_buses` | — | 13 live vehicles |
| `dining_locations` | — | 12 locations |
| `dining_menu` | `location_num=15`, `dtdate=09/19/2026` | 470 recipes |
| `dining_menu` | `location_num=15`, `dtdate=09/17/2026` | weekday menu |
| `dining_hours` | `foodpro_id=15`, `date=2026-09-19` | 2 windows |
| `dining_allergens` | `location_num=15` | 10 allergens, 4 diets |
| `dining_nutrition` | `items=214022*1*1,141002*2*1` | nutrient totals |

---

## 1. `hokieday/gtfs.py` — owner: worker **gtfs**

```python
@dataclass(frozen=True)
class Stop:        stop_id: str; name: str; lat: float; lon: float; wheelchair: int

@dataclass(frozen=True)
class Departure:
    stop_id: str; route_id: str; trip_id: str; head_sign: str
    dep_time: datetime          # campus-local (config.CAMPUS_TZ)
    in_min: float               # minutes from 'at' to dep_time
    service_date: date

@dataclass
class Gtfs:
    stops: dict[str, Stop]
    routes: dict[str, dict]                 # route_id -> row
    trips: dict[str, dict]                  # trip_id -> row
    stop_times: dict[str, list[tuple]]      # trip_id -> [(seq, stop_id, arr, dep), ...] sorted
    calendar_dates: dict[str, dict]         # service_id -> {date: exception_type}

def ensure_extracted(force: bool = False) -> Path
def load_gtfs(force: bool = False) -> Gtfs
def service_ids_for_date(g: Gtfs, d: date) -> set[str]
def active_trip_ids(g: Gtfs, d: date) -> set[str]
def nearest_stops(g: Gtfs, lat: float, lon: float, k: int = 5) -> list[tuple[float, Stop]]
def next_departures(g: Gtfs, stop_id: str, at: datetime, horizon_min: int = 180,
                    route_id: str | None = None, limit: int = 10) -> list[Departure]
def walk_minutes(a: tuple[float, float], b: tuple[float, float]) -> float
```

### TYPE PIN — added after an integration failure; do not re-loosen

`arr` / `dep` are **`int` seconds after the service-day midnight** (hours may
exceed 24, so `27:00:00` becomes `97200`). The original contract left their type
unstated; both workers made a defensible choice; the mismatch **silently broke
the 13/13 keystone join** — every vehicle came back unmatched, with no error.
Consumers MUST tolerate both `int` seconds and `"HH:MM:SS"` strings.

**Hard requirements**

- **There is no `calendar.txt` in this feed.** Service MUST be resolved from
  `calendar_dates.txt` only (`exception_type=1` added, `2` removed).
  **The failure mode is NOT "zero trips" — it is a plausible-looking wrong day's
  trips.** Only 2 of the feed's 8 services are active on any given Saturday, so
  unfiltered queries return weekday/Friday trips that look perfectly normal.
  This exact bug was found in the integrator's own spike; do not reintroduce it.
- GTFS times are `HH:MM:SS` and **may exceed 24:00:00** for after-midnight
  service (e.g. `25:10:00`). Parse as `hours*3600+...`, do not use `%H`.
- `next_departures` must filter to trips whose service IS active on `at.date()`.
- Unzip `cache/bt_gtfs.bin` into `cache/gtfs/` using stdlib `zipfile`.
  Handle the case where `cache/gtfs/` already exists (that is the normal path).
- Verified expectations to assert in tests:
  - 297 stops, 24 routes, 3658 trips, 74301 stop_times, 181 calendar_dates rows
  - **the feed contains only 8 services, and only 2 are active on 2026-09-19**
  - stop `1600` "Main/Roanoke Sbnd" is ~37 m from (37.22957, -80.41394)
  - on **2026-09-19** with `at=11:22`, the service-filtered departures from stop `1600` are
    **`SME 11:48:29`, `HDG 11:50:21`, `SME 12:18:29`, `HDG 12:20:21`**
    *(an earlier draft of this file said 11:23:29 — that number was an artifact of
    not filtering by service date and is WRONG; see SDD §5.2 v1.1)*

---

## 2. `hokieday/livebus.py` — owner: worker **livebus**

```python
@dataclass(frozen=True)
class BusObs:
    bus_id: str; route_id: str; stop_id: str
    lat: float; lon: float; speed: float; passengers: int
    load_pct: int                # from percentOfCapacity
    at_stop: bool
    gtfs_trip_id: str
    observed_at: datetime        # UTC

@dataclass(frozen=True)
class BusLive(BusObs):
    sched_delta_min: float | None   # + late, - early, None if unmatched
    is_stale: bool

def fetch_vehicles(force: bool = False) -> list[dict]
def normalize(vehicles: list[dict], observed_at: datetime | None = None) -> list[BusObs]
def schedule_delta(obs: BusObs, g: "Gtfs") -> float | None
def live(force: bool = False) -> list[BusLive]
def append_bronze(vehicles: list[dict], path: Path | None = None) -> int
```

**Hard requirements**

- **Use `percentOfCapacity` for crowding. `capacity` is internally inconsistent**
  (observed `capacity=24, passengers=24, percentOfCapacity=30`) and must not be
  used as a denominator. Document this in a comment.
- The endpoint returns `states[]`; use the **last** element as current state.
- `observed_at` should come from `states[-1]["version"]` (epoch ms) when present,
  else now(UTC).
- `schedule_delta` = minutes between now and the **scheduled departure** at the
  bus's current `stop_id` for its `gtfs_trip_id`. Positive = running late.
  Return `None` when the trip or stop cannot be matched (never raise).
- Join key is `gtfsTripId` -> `trips.trip_id`. This join is verified working on
  13/13 vehicles, so a `None` delta in tests means a bug.
- `is_stale` = `observed_at` older than `config.STALE_LIVE_MINUTES`.
- `fetch_vehicles` must return `payload["data"]`.
- Verified expectation to assert: fixture yields **13** vehicles, routes include
  `SME`, `NMG`, `HDG`, `UCB`, `TCP`, `HWC`; all 13 get a non-`None` delta.

---

## 3. `hokieday/dining.py` — owner: worker **dining**

```python
@dataclass(frozen=True)
class Location:   location_num: str; name: str
@dataclass(frozen=True)
class MenuItem:
    location_num: str; date: date; meal: str; section: str
    recipe_id: str; name: str; description: str
    portion_size: str; portion_unit: str
    allergens: tuple[str, ...]      # split, stripped, no empties
    diet_tags: tuple[str, ...]      # from legendImages, lowercased
@dataclass(frozen=True)
class HoursWindow:
    foodpro_id: str; name: str; date: date
    open_time: str; close_time: str     # "HH:MM:SS" as returned
@dataclass(frozen=True)
class Nutrients: cals: float; protein_g: float; fat_g: float; carb_g: float; sodium_mg: float

def locations(force: bool = False) -> list[Location]
def menu(location_num: str, d: date | str, force: bool = False) -> list[MenuItem]
def allergens(location_num: str = "15") -> tuple[list[str], list[dict]]
def nutrition_bulk(items: list[tuple[str, str, int]], chunk: int = 40,
                   force: bool = False) -> dict[str, Nutrients]
def hours(foodpro_id: str, d: date | str, force: bool = False) -> list[HoursWindow]
def is_open(windows: list[HoursWindow], at: datetime) -> tuple[bool, float | None]
def eat_options(location_num: str, d: date | str, diet: str | None = None,
                avoid: tuple[str, ...] = (), max_kcal: float | None = None,
                at: datetime | None = None, open_only: bool = False,
                force: bool = False) -> list[MenuItem]
```

**Hard requirements**

- **The two date formats differ, and the menu API fails SILENTLY on the wrong one:**
  - menu / `dtdate` = `MM/DD/YYYY`  (ISO returns HTTP 200 with `"meals": []`)
  - hours `date`    = `YYYY-MM-DD`
  Raise a clear error if `menu()` returns zero recipes — never return `[]` quietly.
- Nutrition `items` format is `recipeId*portionSize*quantity`, comma-joined.
  **`nutrition_bulk` must chunk** (default 40) rather than build one giant URL,
  and must cache each chunk under `("dining_nutrition", {"items": <chunk string>})`
  so repeated calls are free.
- `is_open` returns `(is_open_now, minutes_until_close)`; `minutes_until_close`
  may be negative when already closed.
- `eat_options` applies filters **in code**: diet tag match, `avoid` allergens
  (substring match against the allergen string, case-insensitive), kcal ceiling.
  **Allergy filtering is a hard filter, never a preference.** A requested diet
  also rejects a row whose declared allergens directly contradict its diet tag
  (observed source conflict: `vegan` plus `Eggs`).
- Join note: the hours API's `extra_data[key="foodpro_id"].value` equals the menu
  API's `locationNum` (verified for D2 = `15`). Keep this as a documented helper.
- Verified expectations to assert:
  - D2 (`15`) on `09/19/2026` -> **470** recipes
  - 174 vegetarian, 231 vegan
  - **42** items list nut allergens, **188** have a BLANK allergen field, and
    **428** do not contain nut allergens. (An earlier draft said 240 — that came
    from a condition that excluded blank-allergen items. Do not assert 240.)
  - **SAFETY:** blank allergen field is NOT "allergen-free". Treat blank as
    UNKNOWN; never as safe.
  - `avoid=("Peanuts",)` returns **zero** items whose allergen string contains Peanuts
  - allergen list has 10 entries; diet categories have 4
  - hours fixture -> 2 windows, `09:30:01–15:00:00` and `15:00:01–20:00:00`
  - nutrition fixture for `214022*1*1,141002*2*1` (biscuit + pancakes, **2 items**)
    -> cals **479.616**
    *(an earlier draft said 529.6 — that is the **3-item** query WITH bacon:
    `081108*1*1,214022*1*1,141002*2*1`. Both are real; the pairing was wrong.)*
  - nutrient values come back as **strings**, not floats — cast before comparing

### 3b. ADDITIVE basic/location layer (post-push, frontend contract)

These functions are **new** — no §3 signature changed. They add multi-location
basic menus and a status separate from the food rows. **No function in this
layer fetches nutrition** (nutrition stays a later, separate join).

```python
STATUS_OK = "ok"; STATUS_CLOSED = "closed"
STATUS_EMPTY = "empty"; STATUS_UNAVAILABLE = "unavailable"

@dataclass(frozen=True)
class LocationStatus:
    location_num: str; name: str; date: date; status: str
    open_now: bool | None; menu_count: int | None
    menu_status: str | None      # ok | empty | unavailable (INDEPENDENT)
    hours_status: str | None     # ok | closed | unavailable (INDEPENDENT)
    windows: tuple[HoursWindow, ...]; stale: bool | None
    source: str | None; fetched_at: str | None; reason: str | None
    def as_dict(self) -> dict

@dataclass(frozen=True)
class MenuResult:
    location_num: str; name: str; date: date; status: str
    items: tuple[MenuItem, ...]; reason: str | None
    source: str | None; fetched_at: str | None; stale: bool | None

@dataclass(frozen=True)
class FoodRow:  # as_dict() -> JSON-ready basic row
dict with location_num, location_name, date (ISO), meal, section, name,
description, portion, diet_tags[], allergens[], allergens_known,
venue_allergen_free, recipe_id, source, fetched_at

def location_directory() -> list[Location]          # all 12, sorted by num
def location_status(location_num, d=None, at=None, force=False,
                    max_age_s=None) -> LocationStatus
def menu_result(location_num, d, force=False,
                max_age_s=None) -> MenuResult
def list_foods(location_num=None, d=None, meal=None, section=None,
               diet=None, avoid=(), query=None, force=False,
               max_age_s=None) -> dict
def search_foods(query, location_num=None, d=None, diet=None, avoid=(),
                 force=False, max_age_s=None) -> dict
def filter_foods(location_num=None, d=None, meal=None, section=None,
                 diet=None, avoid=(), force=False, max_age_s=None) -> dict
def window_span(w: HoursWindow) -> tuple[datetime, datetime]   # overnight roll
```

`list_foods`/`search_foods`/`filter_foods` return a dict:

```
{date, count, rows[FoodRow.as_dict()],
 sources_ok: [location_num],        # reachable (ok or empty)
 sources_skipped: [{location_num, location_name, status, reason}],
 sources_stale: [location_num],     # age > max_age_s
 statuses: [LocationStatus.as_dict()],
 reason: str | None}
```

**Status semantics.** `menu_status` and `hours_status` are independent; the
composite `status` follows a fixed precedence: **any unavailable source ->
`unavailable`** (so a closed day with an unreachable menu is `unavailable`, and
hours failure is never plain `ok`); else `hours_status == closed` -> `closed`;
else `menu_status == empty` -> `empty`; else `ok`. `stale` is orthogonal (served
copy older than `max_age_s`). `window_span` rolls `close <= open` to the next
day. Each window is anchored to its OWN source date and is NEVER shifted
backward: an overnight window (e.g. DX 22:00:01 -> 02:00:00) reads correctly at
01:00 on the day AFTER it opened, and `dining.open_windows()` loads D-1's ACTUAL
hours for an early-morning probe on D (a missing D-1 is `unavailable`/UNKNOWN,
never a fabricated open/closed). With multiple units the close is the union's
LAST close.

**Future capture is not data.** A menu/hour/nutrition envelope whose
`fetched_at` is after `config.now()` (the replay/request clock) is rejected:
`menu_result` returns `unavailable` (`not_yet_available`) with no rows,
`hours()` raises `NotYetAvailableError`, and nutrition chunks/derived all-menu
envelopes are skipped so `kcal` stays unknown. This is why replay only serves D2
menus, and why replay kcal is `None`: any promoted later capture would otherwise
read as fresh.

**Payload source-matching.** `menu_result` REQUIRES the payload's own
`locationNum` and `date` to be present, parseable, and exactly equal to the
normalized request; a missing or mismatched identity returns `unavailable`
(`source_mismatch`), never the wrong hall's or day's rows.

**`tools.find_food(location_num=None)`** searches every configured location and
returns `sources_ok` / `sources_skipped` / `statuses`; no location is silently
dropped. Deterministic ranking is unchanged (meal-ish section, most filling,
name). A **kcal ceiling is a hard constraint**: an all-locations `max_kcal`
query does not fetch campus-wide nutrition, so it returns **no items** with
typed `nutrition_unavailable` skipped entries and an explicit reason. A single
location attaches real macros and honors the ceiling when contemporaneous
nutrition exists; when it does not (including future-captured replay data) the
location is reported `nutrition_unavailable` and no unproven rows are returned.

---

## 4. NOT assigned yet (parent owns)

`hokieday/tools.py`, `hokieday/agent.py`, `notebooks/`, `app/`, `scripts/poll_buses.py`.
Workers must not create these files.