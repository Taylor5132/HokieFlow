# INTERFACES — frozen contract for Tier 1

**Owned by the integrator. Workers implement against this; workers do not change it.**
If a signature here is wrong, stop and report it — do not silently redesign.

Everything below is in `hokieday/` (import as `from hokieday import gtfs`).

---

## 0. Non-negotiable rules for every worker

1. **Do not edit `hokieday/config.py` or `hokieday/cache.py`.** Parent owns them.
2. **All HTTP goes through `cache.get_json(...)` or `cache.get_bytes(...)`.**
   Never import `urllib`/`requests` directly — doing so bypasses the offline demo
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

**Hard requirements**

- **There is no `calendar.txt` in this feed.** Service MUST be resolved from
  `calendar_dates.txt` only (`exception_type=1` added, `2` removed). Code that
  expects `calendar.txt` will silently return zero trips.
- GTFS times are `HH:MM:SS` and **may exceed 24:00:00** for after-midnight
  service (e.g. `25:10:00`). Parse as `hours*3600+...`, do not use `%H`.
- `next_departures` must filter to trips whose service IS active on `at.date()`.
- Unzip `cache/bt_gtfs.bin` into `cache/gtfs/` using stdlib `zipfile`.
  Handle the case where `cache/gtfs/` already exists (that is the normal path).
- Verified expectations to assert in tests:
  - 297 stops, 24 routes, 3658 trips, 74301 stop_times, 181 calendar_dates rows
  - stop `1600` "Main/Roanoke Sbnd" is ~37 m from (37.22957, -80.41394)
  - on 2026-09-19 with `at=11:22`, stop `1600` yields departures starting ~11:23:29 (route SME)

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
  **Allergy filtering is a hard filter, never a preference.**
- Join note: the hours API's `extra_data[key="foodpro_id"].value` equals the menu
  API's `locationNum` (verified for D2 = `15`). Keep this as a documented helper.
- Verified expectations to assert:
  - D2 (`15`) on `09/19/2026` -> **470** recipes
  - 174 vegetarian, 231 vegan, 240 with no Peanuts/Tree Nuts
  - `avoid=("Peanuts",)` returns **zero** items whose allergen string contains Peanuts
  - allergen list has 10 entries; diet categories have 4
  - hours fixture -> 2 windows, `09:30:01–15:00:00` and `15:00:01–20:00:00`
  - nutrition fixture for `214022*1*1,141002*2*1` -> cals ~529.6

---

## 4. NOT assigned yet (parent owns)

`hokieday/tools.py`, `hokieday/agent.py`, `notebooks/`, `app/`, `scripts/poll_buses.py`.
Workers must not create these files.