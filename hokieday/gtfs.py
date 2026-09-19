"""Static GTFS -> next departures (BT, Blacksburg Transit).

Owner: worker **gtfs**. Implements INTERFACES.md section 1 verbatim.

Key facts about THIS feed (verified 2026-09-19, see SDD.md section 5.2):
  * There is NO `calendar.txt`. All service is defined by `calendar_dates.txt`
    exceptions only (exception_type 1 = added, 2 = removed). The failure mode
    of ignoring this is NOT zero trips - it is a plausible-looking WRONG day's
    trips (weekday/Friday service showing on a Saturday). Only 2 of the feed's
    8 services are active on a given Saturday.
  * GTFS times may exceed 24:00:00 (after-midnight service, e.g. 25:48:29;
    the latest departure in this feed is 27:00:00). Parsed as
    hours*3600 + minutes*60 + seconds, never via datetime.strptime("%H").
  * All HTTP goes through hokieday.cache.get_bytes - never urllib/requests.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import cache, config

_TZ = ZoneInfo(config.CAMPUS_TZ)

_REQUIRED_FILES = ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt",
                   "calendar_dates.txt")


# ------------------------------------------------------------------ helpers
def _parse_time(s: str) -> int:
    """GTFS HH:MM:SS -> seconds since service-day midnight. May exceed 86400."""
    h, m, sec = (int(p) for p in s.strip().split(":"))
    return h * 3600 + m * 60 + sec


def _to_date(s: str) -> date:
    """GTFS YYYYMMDD -> date."""
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def _rows(name: str) -> list[dict[str, str]]:
    path = config.GTFS_DIR / name
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between (lat, lon) pairs, degrees."""
    import math
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(h))


# ------------------------------------------------------------------ model
@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    lat: float
    lon: float
    wheelchair: int


@dataclass(frozen=True)
class Departure:
    stop_id: str
    route_id: str
    trip_id: str
    head_sign: str
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


# ------------------------------------------------------------------ extraction
def ensure_extracted(force: bool = False) -> Path:
    """Extract the cached GTFS zip into config.GTFS_DIR (idempotent).

    The normal path is that cache/gtfs/ already exists and is populated -
    in that case this is a cheap existence check, no network, no unzip.
    """
    if not force and all((config.GTFS_DIR / f).exists() for f in _REQUIRED_FILES):
        return config.GTFS_DIR

    data = cache.get_bytes(
        "bt_gtfs", config.ENDPOINTS["bt_gtfs"],
        max_age_s=config.DEFAULT_GTFS_CACHE_MAX_AGE_S,
    )
    config.GTFS_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            # never let an archive path escape the target directory
            target = (config.GTFS_DIR / member).resolve()
            if not str(target).startswith(str(config.GTFS_DIR.resolve())):
                raise ValueError(f"unsafe zip member: {member}")
        zf.extractall(config.GTFS_DIR)
    return config.GTFS_DIR


# ------------------------------------------------------------------ loading
def load_gtfs(force: bool = False) -> Gtfs:
    ensure_extracted(force=force)

    stops: dict[str, Stop] = {}
    for r in _rows("stops.txt"):
        stops[r["stop_id"]] = Stop(
            stop_id=r["stop_id"],
            name=r["stop_name"],
            lat=float(r["stop_lat"]),
            lon=float(r["stop_lon"]),
            wheelchair=int(r.get("wheelchair_boarding") or 0),
        )

    routes = {r["route_id"]: r for r in _rows("routes.txt")}
    trips = {r["trip_id"]: r for r in _rows("trips.txt")}

    stop_times: dict[str, list[tuple]] = {}
    for r in _rows("stop_times.txt"):
        stop_times.setdefault(r["trip_id"], []).append((
            int(r["stop_sequence"]),
            r["stop_id"],
            _parse_time(r["arrival_time"]),
            _parse_time(r["departure_time"]),
        ))
    for lst in stop_times.values():
        lst.sort(key=lambda t: t[0])

    calendar_dates: dict[str, dict] = {}
    for r in _rows("calendar_dates.txt"):
        calendar_dates.setdefault(r["service_id"], {})[_to_date(r["date"])] = \
            int(r["exception_type"])

    return Gtfs(stops=stops, routes=routes, trips=trips,
                stop_times=stop_times, calendar_dates=calendar_dates)


# ------------------------------------------------------------------ service
def service_ids_for_date(g: Gtfs, d: date) -> set[str]:
    """Services active on calendar date d, from calendar_dates.txt ONLY.

    There is no calendar.txt in this feed: a service runs on d iff it has an
    exception_type=1 (added) row for d that is not overridden by a later
    exception_type=2 (removed) row for the same date.
    """
    out: set[str] = set()
    for sid, exceptions in g.calendar_dates.items():
        exc = exceptions.get(d)
        if exc == 1:
            out.add(sid)
        elif exc == 2:
            out.discard(sid)
    return out


def active_trip_ids(g: Gtfs, d: date) -> set[str]:
    """Trip ids whose service is active on calendar date d."""
    services = service_ids_for_date(g, d)
    return {tid for tid, row in g.trips.items() if row["service_id"] in services}


# ------------------------------------------------------------------ geometry
def nearest_stops(g: Gtfs, lat: float, lon: float, k: int = 5) -> list[tuple[float, Stop]]:
    """The k stops closest to (lat, lon) as (haversine_metres, Stop), nearest first."""
    scored = [(_haversine_m((lat, lon), (s.lat, s.lon)), s) for s in g.stops.values()]
    scored.sort(key=lambda t: t[0])
    return scored[:k]


def walk_minutes(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Straight-line metres -> path metres (WALK_PATH_FACTOR) -> minutes."""
    metres = _haversine_m(a, b) * config.WALK_PATH_FACTOR
    return metres / config.WALK_SPEED_MPS / 60.0


# ------------------------------------------------------------------ departures
def _stop_departure_index(g: Gtfs) -> dict[str, list[tuple[int, str]]]:
    """stop_id -> [(departure_seconds, trip_id), ...] over the whole feed.

    Cached on the Gtfs instance so repeated next_departures() calls on the
    same object are cheap; load_gtfs() creates a fresh object per load.
    """
    idx = getattr(g, "_stop_departure_index", None)
    if idx is None:
        idx = {}
        for tid, rows in g.stop_times.items():
            for _seq, sid, _arr, dep in rows:
                idx.setdefault(sid, []).append((dep, tid))
        for lst in idx.values():
            lst.sort()
        g._stop_departure_index = idx        # noqa: SLF001 - our own dataclass
    return idx


def _as_campus_local(at: datetime) -> datetime:
    """Interpret naive datetimes as campus-local; convert aware ones into it."""
    if at.tzinfo is None:
        return at.replace(tzinfo=_TZ)
    return at.astimezone(_TZ)


def next_departures(g: Gtfs, stop_id: str, at: datetime, horizon_min: int = 180,
                    route_id: str | None = None, limit: int = 10) -> list[Departure]:
    """Next departures from stop_id at/after 'at', service-filtered.

    ONLY trips whose service_id is active on at.date() (per calendar_dates.txt)
    are considered - see the module docstring for why this filter is the
    single most important correctness rule in this module.
    """
    at_local = _as_campus_local(at)
    service_date = at_local.date()
    services = service_ids_for_date(g, service_date)
    if not services:
        return []

    # Campus-local midnight of the service day; GTFS times are offsets from it
    # and may exceed 24 h (after-midnight service belongs to the prior day).
    midnight = datetime(service_date.year, service_date.month, service_date.day,
                        tzinfo=_TZ)
    trips = g.trips
    routes = g.routes
    out: list[Departure] = []

    for dep_s, tid in _stop_departure_index(g).get(stop_id, ()):
        trip = trips.get(tid)
        if trip is None or trip["service_id"] not in services:
            continue
        if route_id is not None and trip["route_id"] != route_id:
            continue
        dep_time = midnight + timedelta(seconds=dep_s)
        in_min = (dep_time - at_local).total_seconds() / 60.0
        if in_min < 0 or in_min > horizon_min:
            continue
        head_sign = trip.get("trip_headsign") or ""
        out.append(Departure(
            stop_id=stop_id,
            route_id=trip["route_id"],
            trip_id=tid,
            head_sign=head_sign or (routes.get(trip["route_id"], {}) or {}).get(
                "route_long_name", ""),
            dep_time=dep_time,
            in_min=in_min,
            service_date=service_date,
        ))
        if len(out) >= limit * 4:            # pre-trim; exact sort+limit below
            break

    out.sort(key=lambda d: (d.dep_time, d.route_id))
    return out[:limit]
