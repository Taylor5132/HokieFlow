#!/usr/bin/env python3
"""Bus tap — start this IMMEDIATELY and leave it running.

Rationale (SDD §9): the ML label for `sched_delta_min` comes from live-vs-schedule
joins. Every hour we are not sampling is training data we can never recover.

This taps the LIVE cache (`cache/`) and appends to `data/bus_bronze.jsonl` using
`livebus.append_bronze`, so records share ONE format with the production poller:
one JSON line per vehicle per poll, each carrying `fetched_at`.

The tap deliberately never writes the frozen replay store (`fixtures/`) — that
store has its own uniquely-shaped files, and keeping them separate is what makes
offline tests and the offline demo deterministic.

Usage:
    python3 scripts/tap_buses.py                # run forever, poll every 60s
    python3 scripts/tap_buses.py --minutes 30   # bounded run
    python3 scripts/tap_buses.py --once         # single sample

Leave it running for the rest of the hackathon:
    nohup python3 scripts/tap_buses.py > data/tap.log 2>&1 &
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hokieday import config, livebus  # noqa: E402

OUT = config.DATA_DIR / "bus_bronze.jsonl"


def sample(verbose: bool = True) -> int:
    """Take one sample and append it. Returns the number of vehicles captured."""
    vehicles = livebus.fetch_vehicles(force=True)
    rows = livebus.append_bronze(vehicles, OUT)
    if verbose:
        routes = sorted({v.get("routeId", "?") for v in vehicles})
        print(f"[{config.now().isoformat(timespec='seconds')}] "
              f"{len(vehicles)} vehicles | routes={','.join(routes)}", flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="take a single sample and exit")
    ap.add_argument("--minutes", type=float, default=None, help="run for N minutes")
    ap.add_argument("--interval", type=int, default=config.LIVE_BUS_POLL_SECONDS)
    args = ap.parse_args()

    if args.interval < config.LIVE_BUS_POLL_SECONDS:
        print(f"refusing: interval {args.interval}s is faster than the "
              f"{config.LIVE_BUS_POLL_SECONDS}s politeness floor for this undocumented endpoint")
        return 2

    if args.once:
        n = sample()
        print(f"appended {n} rows to {OUT} ({OUT.stat().st_size:,} bytes)")
        return 0 if n else 1

    deadline = None if args.minutes is None else time.monotonic() + args.minutes * 60
    print(f"tapping every {args.interval}s -> {OUT}  (Ctrl-C to stop)", flush=True)
    polls = 0
    try:
        while deadline is None or time.monotonic() < deadline:
            try:
                sample()
                polls += 1
            except Exception as exc:                       # noqa: BLE001
                print(f"  WARN sample failed: {exc}", flush=True)   # a gap beats a dead run
            if deadline is None or time.monotonic() < deadline:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    print(f"done: {polls} polls appended to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())