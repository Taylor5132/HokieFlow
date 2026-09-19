"""Live BT buses -> crowding + schedule adherence.

OWNER: worker livebus. Implements INTERFACES.md section 2 verbatim.

Data flow:
    fetch_vehicles() -> raw payload["data"] (13 vehicles in the captured fixture)
    normalize()      -> BusObs (one per vehicle, current state = states[-1])
    schedule_delta() -> minutes vs the scheduled departure at the bus's stop
    live()           -> BusLive (obs + sched_delta_min + is_stale)
    append_bronze()  -> raw vehicles + fetched_at, one JSON line each, the ML dataset

The key caveat (verified live 2026-09-19, see SDD.md 5.3):
    `capacity` is INTERNALLY INCONSISTENT -- one bus reported
    capacity=24, passengers=24, percentOfCapacity=30, which is impossible.
    It must NOT be used as a crowding denominator. Crowding comes ONLY from
    `percentOfCapacity` (verified range across the 13-vehicle fixture:
    min 0%, max 30%, mean 10%).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from . import cache, config

if TYPE_CHECKING:                      # annotation-only, for the "Gtfs" forward ref
    from .gtfs import Gtfs

_CAMPUS_TZ = ZoneInfo(config.CAMPUS_TZ)


# ----------------------------------------------------------------- types
@dataclass(frozen=True)
class BusObs:
    bus_id: str
    route_id: str
    stop_id: str
    lat: float
    lon: float
    speed: float
    passengers: int
    load_pct: int                 # from percentOfCapacity (NOT capacity -- see module docstring)
    at_stop: bool
    gtfs_trip_id: str
    observed_at: datetime         # UTC


@dataclass(frozen=True)
class BusLive(BusObs):
    sched_delta_min: float | None   # + late, - early, None if unmatched
    is_stale: bool


# ----------------------------------------------------------------- fetch
def fetch_vehicles(force: bool = False) -> list[dict]:
    """Fetch the live-vehicle payload via the cache layer; return payload["data"].

    Freshness honors config.LIVE_BUS_POLL_SECONDS: a cached snapshot younger
    than that is reused, so every live-map/state consumer sees at most one new
    observed snapshot per politeness window. `force=True` still bypasses the
    cache for an explicit tap sample. In DEMO_MODE=cache the freshness argument
    is inert -- the frozen fixture is returned verbatim.
    """
    payload = cache.get_json(
        "bt_buses", config.ENDPOINTS["bt_buses"], force=force,
        max_age_s=config.LIVE_BUS_POLL_SECONDS,
    )
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    return data if isinstance(data, list) else []


# ----------------------------------------------------------------- normalize
def _epoch_ms_to_utc(ms) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)


def normalize(vehicles: list[dict], observed_at: datetime | None = None) -> list[BusObs]:
    """Map the raw vehicle dicts to BusObs, one per vehicle.

    Each vehicle carries `states[]`; the LAST element is the current state
    (earlier elements are history). `observed_at` comes from
    states[-1]["version"] (epoch milliseconds -> UTC) when present, else from
    the `observed_at` argument, else datetime.now(timezone.utc).
    """
    fallback = observed_at or config.now()
    out: list[BusObs] = []
    for v in vehicles:
        states = v.get("states") or []
        s = states[-1] if states else {}

        if s.get("version") is not None:
            ts = _epoch_ms_to_utc(s["version"])
        else:
            ts = fallback

        # prefer realtime coordinates, fall back to the plain position
        lat = s.get("realtimeLatitude", s.get("latitude"))
        lon = s.get("realtimeLongitude", s.get("longitude"))

        out.append(BusObs(
            bus_id=str(v.get("id", "")),
            route_id=str(v.get("routeId", "")),
            stop_id=str(v.get("stopId", "")),
            lat=float(lat) if lat is not None else 0.0,
            lon=float(lon) if lon is not None else 0.0,
            speed=float(s.get("speed") or 0.0),
            passengers=int(float(s.get("passengers") or 0)),
            load_pct=int(float(v.get("percentOfCapacity") or 0)),
            at_stop=str(s.get("isBusAtStop", "")).strip().upper() == "Y",
            gtfs_trip_id=str(v.get("gtfsTripId", "")),
            observed_at=ts,
        ))
    return out


# ----------------------------------------------------------------- schedule delta
def _parse_gtfs_seconds(s) -> int | None:
    """Normalise a GTFS time to seconds after the service-day midnight.

    INTERFACES.md 1 declares these tuple slots as
        (stop_sequence, stop_id, arrival_time, departure_time)
    but the ORIGINAL contract did not pin their TYPE, so both representations
    exist in the wild and we accept both:
      * int/float  -- what hokieday.gtfs stores (seconds after service-day
                      midnight; hours may exceed 24, e.g. 27h00m -> 97200)
      * "HH:MM:SS" -- the raw GTFS text, e.g. "25:10:00" (also >24h)
    Accepting both is deliberate: this seam broke the 13/13 keystone join once
    already (all vehicles unmatched) and it must not break again.
    Returns None if unparseable.
    """
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return int(s)
    if not isinstance(s, str):
        return None
    parts = s.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, sec = int(parts[0]), int(parts[1]), float(parts[2])
    except ValueError:
        return None
    return h * 3600 + m * 60 + sec


def schedule_delta(obs: BusObs, g: "Gtfs", now: datetime | None = None) -> float | None:
    """Minutes between now and the SCHEDULED DEPARTURE of `obs.gtfs_trip_id` at
    `obs.stop_id`. Positive = running late, negative = running early.

    `g` is duck-typed against the frozen Gtfs shape (g.trips, g.stop_times) --
    hokieday.gtfs is deliberately NOT imported at module top level, so this
    works with any object exposing those two dicts. Returns None (never raises)
    when the trip or the current stop cannot be matched.

    GTFS times are campus-local on a service date; the service date is not in
    the live feed, so we score the departure against today and yesterday as
    candidate service dates and keep the smallest absolute delta. This also
    handles after-midnight trips (times >= 24:00:00) correctly.

    Arrival/departure slots may be int seconds or "HH:MM:SS" strings -- see
    _parse_gtfs_seconds().
    """
    try:
        if now is None:
            now_local = config.now(_CAMPUS_TZ)
        elif now.tzinfo is None:
            now_local = now.replace(tzinfo=_CAMPUS_TZ)
        else:
            now_local = now.astimezone(_CAMPUS_TZ)

        stop_times = g.stop_times.get(obs.gtfs_trip_id)      # trip match
        if not stop_times:
            return None

        entries = [e for e in stop_times if str(e[1]) == str(obs.stop_id)]
        if not entries:                                       # current stop match
            return None

        best: float | None = None
        for d in {now_local.date(), now_local.date() - timedelta(days=1)}:
            day_midnight = datetime.combine(d, time(0), tzinfo=_CAMPUS_TZ)
            for e in entries:
                secs = _parse_gtfs_seconds(e[3])              # departure_time
                if secs is None:
                    secs = _parse_gtfs_seconds(e[2])          # fall back to arrival
                if secs is None:
                    continue
                sched = day_midnight + timedelta(seconds=secs)
                delta_min = (now_local - sched).total_seconds() / 60.0
                if best is None or abs(delta_min) < abs(best):
                    best = delta_min
        return None if best is None else round(best, 2)
    except Exception:
        # never raise on unmatched/odd data -- a None delta means "schedule-only"
        return None


# ----------------------------------------------------------------- live
def _is_stale(observed_at: datetime, now: datetime | None = None) -> bool:
    # `now` is the captured request clock when the caller supplies it, else
    # config.now() -- the PINNED replay clock in DEMO_MODE=cache. Using the raw
    # wall clock here would mark every replayed snapshot stale during a demo,
    # because a fixture is by definition older than the 10-minute threshold.
    ref = now if now is not None else config.now()
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    age_s = (ref - observed_at).total_seconds()
    return age_s > config.STALE_LIVE_MINUTES * 60


def live(force: bool = False, now: datetime | None = None) -> list[BusLive]:
    """One snapshot: fetch -> normalize -> join to the static schedule -> label staleness.

    `now` (optional) is the captured request clock; when given it is used for
    the schedule-delta and staleness arithmetic so a plan and its live evidence
    share one instant. The static GTFS feed is ALWAYS loaded with force=False:
    a bus poll must never re-download or re-extract the schedule. The bus
    payload itself honors config.LIVE_BUS_POLL_SECONDS via fetch_vehicles().

    hokieday.gtfs is imported lazily (not at module top level) so this module
    stays usable when the static-feed module is absent or mid-write.
    """
    obs_list = normalize(fetch_vehicles(force=force))
    try:
        from . import gtfs as gtfs_mod
        g = gtfs_mod.load_gtfs(force=False)
    except Exception:
        g = None
    out: list[BusLive] = []
    for obs in obs_list:
        delta = schedule_delta(obs, g, now=now) if g is not None else None
        out.append(BusLive(
            **{f: getattr(obs, f) for f in obs.__dataclass_fields__},
            sched_delta_min=delta,
            is_stale=_is_stale(obs.observed_at, now=now),
        ))
    return out


# ----------------------------------------------------------------- bronze
def append_bronze(vehicles: list[dict], path: Path | None = None) -> int:
    """Append the raw vehicle payload, one JSON line per vehicle per poll, to the
    bronze JSONL file (default config.DATA_DIR / "bus_bronze.jsonl").

    This file is the ML training dataset for bus lateness -- every record
    carries `fetched_at` (UTC) so arrival deltas can be derived offline.
    Returns the number of rows written.
    """
    if path is None:
        path = config.DATA_DIR / "bus_bronze.jsonl"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = 0
    with path.open("a", encoding="utf-8") as f:
        for v in vehicles:
            record = {"fetched_at": fetched_at, **v}
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
            rows += 1
    return rows
