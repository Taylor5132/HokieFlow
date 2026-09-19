# HokieDay

Unified campus-life agent for Virginia Tech. Built for VTHacks 14, Deloitte × Databricks challenge.

**Read `../SDD.md` first — it is the source of truth.** This repo is the implementation of its Tier 1.

## Shape of the repo

```
hokieday/
  hokieday/            # pure-stdlib core library (no pyspark — stays portable)
    config.py          # endpoints, tuning constants, place registry
    cache.py           # cache-first fetch: live | cache modes
    gtfs.py            # static GTFS -> departures          [worker: gtfs]
    livebus.py         # live buses -> crowding + lateness   [worker: livebus]
    dining.py          # menu, nutrition, allergens, hours   [worker: dining]
    tools.py           # the 8-tool contract (UC-function ready)
    agent.py           # plan_day + the re-planning loop
  scripts/
    seed_cache.py      # build cache/ from real captured payloads
    poll_buses.py      # 60s poller -> appends the ML training dataset
    ingest_gtfs.py
  cache/               # REAL payloads (fixtures). DEMO_MODE=cache reads these.
  data/                # bronze JSONL (the ML dataset we generate ourselves)
  tests/               # offline tests; must pass with DEMO_MODE=cache
  notebooks/           # thin Databricks wrappers around the core library
  app/                 # Databricks App (chat UI)
```

## Why the core library is stdlib-only

The same code must run in three places: locally for tests, in a Databricks notebook, and inside a Databricks App. A `pyspark` import in the core would break the first. **Notebooks adapt to the library; the library never adapts to the notebook.**

## Offline demo (non-negotiable — NFR-1)

```bash
export DEMO_MODE=cache
python3 -m unittest discover -s tests -v
```

With `DEMO_MODE=cache` nothing touches the network. If a demo-day idea needs a
network call, it is wrong.

> No pytest, no pandas on this machine (Python 3.14.7) — tests use stdlib `unittest`, and the core library is stdlib-only.

## Live mode

```bash
unset DEMO_MODE          # or DEMO_MODE=live
python3 scripts/seed_cache.py     # only needed once
python3 scripts/poll_buses.py     # start the 60s poller EARLY - it builds the ML dataset
```

## Rules for contributors / subagents

1. `config.py` and `cache.py` are owned by the integrator. Do not edit them.
2. All HTTP goes through `cache.get_json` / `cache.get_bytes`. Never call
   `urllib`/`requests` directly — you would bypass the offline demo path.
3. Tests MUST pass with `DEMO_MODE=cache` and MUST NOT touch the network.
   Use stdlib `unittest`, not pytest.
4. Poll the live bus endpoint no faster than `config.LIVE_BUS_POLL_SECONDS`.
5. Never emit a number that the code did not compute. (Design root R2: a language
   model predicts text, it does not calculate.)