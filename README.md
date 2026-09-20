# HokieFlow

Unified campus-life agent for Virginia Tech. Built for VTHacks 14, Deloitte × Databricks challenge.

**Read `docs/SDD.md` first — it is the source of truth.** This repo is the implementation of its Tier 1.

## Shape of the repo

```
hokieday/
  hokieday/            # pure-stdlib core library (no pyspark — stays portable)
    config.py          # endpoints, tuning constants, place registry
    cache.py           # cache-first fetch: live | cache modes
    gtfs.py            # static GTFS -> departures          [worker: gtfs]
    livebus.py         # live buses -> crowding + lateness   [worker: livebus]
    dining.py          # menu, nutrition, allergens, hours   [worker: dining]
    weather.py         # NWS forecast/alerts/observation + leg risk badges
    tools.py           # deterministic tools + plan_day/re-planning loop
    agent_tools.py     # strict allowlist, schemas, request-scoped schedule tool
    agent.py           # provider-neutral bounded HokieFlow AI tool loop
    providers/base.py  # provider protocol; no network dependency
  scripts/
    seed_cache.py      # build fixtures/ from known-good captured payloads
    fetch_weather.py   # edge fetch NWS -> frozen weather fixtures
    tap_buses.py       # 60s poller -> appends the ML training dataset
    export_gold.py     # local decision-ready data -> JSONL handoff
    load_to_databricks.py
  cache/               # LIVE cache. Refreshed whenever we hit the network.
  fixtures/            # FROZEN replay snapshot. ONLY scripts/seed_cache.py writes it.
  data/                # bronze JSONL (the ML dataset we generate ourselves)
  tests/               # offline tests; must pass with DEMO_MODE=cache
  notebooks/           # thin Databricks wrappers around the core library
  app/                 # Databricks App (chat UI)
```

## Why the core library is stdlib-only

The same code must run in three places: locally for tests, in a Databricks notebook, and inside a Databricks App. A `pyspark` import in the core would break the first. **Notebooks adapt to the library; the library never adapts to the notebook.**

## Two stores, deliberately separated

| dir | who writes | who reads |
|---|---|---|
| `cache/` | any live fetch | `DEMO_MODE=live` |
| `fixtures/` | **only** `scripts/seed_cache.py` / `scripts/fetch_weather.py` | `DEMO_MODE=cache` (tests + offline demo) |

They must never be the same directory. A live poll once overwrote the very
fixture the tests read, which silently changed expected values mid-session
(observed: fixture `fetched_at` jumped 15:22 → 15:51).

## The replay clock (`config.now()`)

A schedule delta is `now − scheduled_departure`, so **a cached snapshot's deltas
grow one minute per minute of real time.** Measured: a 6.7-minute gap inflated all
13 live deltas to a +7.73 min mean; a fixture replayed the next morning reads as
~20 hours late.

So in `DEMO_MODE=cache`, `config.now()` is **pinned to the snapshot's capture
time** instead of the wall clock. After pinning, the same 13 vehicles read
mean **+0.96 min** — physically sensible. Override with `DEMO_NOW=<iso8601>`.

**Rule:** library code calls `config.now()`, never `datetime.now()`.

## Offline demo (non-negotiable — NFR-1)

```bash
export DEMO_MODE=cache
python3 -m unittest discover -s tests -v     # 719 tests, no network
```

With `DEMO_MODE=cache` nothing touches the network and the clock is pinned. If a
demo-day idea needs a network call, it is wrong.

> No pytest, no pandas on this machine (Python 3.14.7) — tests use stdlib `unittest`, and the core library is stdlib-only.

## Live mode

```bash
unset DEMO_MODE          # or DEMO_MODE=live
python3 scripts/tap_buses.py --once       # one sample
python3 scripts/tap_buses.py              # 60s poller; leave it running all night
```

The tap writes to `cache/` (live) and appends one JSON line **per vehicle per
poll** to `data/bus_bronze.jsonl` — the ML training set. It never touches
`fixtures/`.

### HokieFlow AI with Gemini (opt-in live mode)

HokieFlow AI is a provider-neutral agent loop: Gemini interprets the question and
selects strict allowlisted tools; deterministic Python obtains campus facts,
computes schedule gaps and feasibility, and enforces safety constraints; Gemini
then narrates only the returned evidence. The API key stays server-side.

```bash
cp .env.example .env
chmod 600 .env
# Edit .env locally and fill GEMINI_API_KEY + GEMINI_MODEL.
python3 app/server.py
```

`app/server.py` loads only an allowlisted set of keys from `.env`, never prints
values, and refuses a file readable by group/other users. Real process
environment variables override `.env`. The file is ignored by Git.

No model is hardcoded. If any setting is absent, or Gemini fails, `/api/ask`
falls back to the bounded parser. `DEMO_MODE=cache` always bypasses the provider,
even if credentials are present. The `ui/` client may attach only its
request-scoped schedule entries; no PID, grades, roster, token, or raw account
record is sent.

To protect free-tier quota, one question normally uses two Gemini calls (tool
selection and grounded narration), with a hard maximum of three turns and four
tool calls. Responses are capped at 512 tokens. The server also defaults to 30
Gemini calls/hour and 200/day per process, with no automatic retry. Override only
when intentional with `HOKIEFLOW_GEMINI_CALLS_PER_HOUR` and
`HOKIEFLOW_GEMINI_CALLS_PER_DAY` (default 120/day). Live HTTP requests also
default to 10 AI questions/hour per client IP
(`HOKIEFLOW_AI_QUESTIONS_PER_IP_HOUR`). The guard is stored in
`cache/ai_provider_calls.json`, so restarting the server cannot silently reset
the allowance. Google AI Studio quota controls remain the stronger
account-level guard: a real `429` is reported as a typed provider failure and
`/api/ask` falls back to the bounded parser.

This is separate from Virginia Tech's **HokieAI** platform. HokieAI is a
university-supported interface to vetted commercial models; it is not the name
of this application's agent and is not automatically grounded in HokieFlow's
campus data.

A real-API smoke test is deliberately opt-in:

```bash
DEMO_MODE=live HOKIEFLOW_LIVE_SMOKE=1 python3 scripts/gemini_agent_smoke.py

# One question per tool family (weather, food, hours, events, bus, departures,
# planning) through the same pre-pass a real request uses:
DEMO_MODE=live HOKIEFLOW_LIVE_SMOKE=1 python3 scripts/gemini_live_battery.py
```

**Before any demo, re-run `DEMO_MODE=cache python3 scripts/seed_cache.py`** to
restore a known-good frozen snapshot.

## Rules for contributors / subagents

1. `config.py` and `cache.py` are owned by the integrator. Do not edit them.
2. Campus-data HTTP goes through `cache.get_json` / `cache.get_bytes`. The sole
   direct HTTP boundary is `app/gemini_provider.py`, and the server can invoke
   it only outside replay mode. Never add provider networking to `hokieday/`.
3. Tests MUST pass with `DEMO_MODE=cache` and MUST NOT touch the network.
   Use stdlib `unittest`, not pytest.
4. Poll the live bus endpoint no faster than `config.LIVE_BUS_POLL_SECONDS`.
5. Never emit a number that the code did not compute. (Design root R2: a language
   model predicts text, it does not calculate.)
6. **Never call `datetime.now()` in library code — call `config.now()`.** Otherwise
   you reintroduce the replay-clock drift (see above).
7. **Never treat a blank allergen field as allergen-free.** 188 of 470 D2 items
   state no allergens; blank means UNKNOWN (SDD risk R8).
## Data sources, attribution, and use

This project is an unaffiliated student hackathon entry. Dining data comes from
Virginia Tech's public **FoodPro** endpoints (`foodpro.students.vt.edu/menus/API/*`
and `apps.students.vt.edu/hours/...`); transit data comes from **BT**'s public
GTFS feed and live-bus endpoint; weather comes from the **NWS** API. Those marks
and data belong to their respective owners.

- **No endorsement.** Virginia Tech, BT, NWS, Deloitte, and Databricks have not
  reviewed, approved, or endorsed HokieFlow or these fixtures.
- **No redistribution claim.** The committed `fixtures/` are a small, frozen
  cache captured for an offline demo and tests. They are not an official data
  distribution; anyone reusing this repo should re-fetch from the upstream
  sources under their terms rather than rely on the snapshot.
- **Data-use caveat.** Menus, nutrition, and hours change constantly; allergen
  and nutrition fields can be incomplete (`----` means missing, and a blank
  allergen field means UNKNOWN). Do not use HokieFlow for allergy or dietary
  decisions without verifying against the venue. Staff usernames and other
  internal metadata that the APIs returned were removed from the committed
  hours fixture; only unit names, FoodPro IDs, and hour windows are kept.
- **Provenance is enforced, not cosmetic.** A menu, hours, or nutrition envelope
  captured after `config.now()` is refused as non-contemporaneous, so replay
  never presents later data as fresh.
