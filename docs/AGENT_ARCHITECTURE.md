# HokieFlow AI grounded-agent architecture

Status: implemented and live-verified. The Gemini REST adapter was exercised
against the real API with `gemini-3.6-flash` on 2026-09-20 through both the smoke
script and an end-to-end `POST /api/ask`. Offline suites remain the canonical
gate and run against a fake provider.

## Decision

HokieFlow uses this boundary:

```text
student text
  → provider interprets and selects allowlisted tools
  → deterministic Python reads campus/user-supplied data and computes decisions
  → provider narrates the bounded tool evidence
  → server verifies exact numbers and preserves structured results
```

The model never fetches arbitrary URLs, reads files, executes code, computes
feasibility, or weakens safety constraints.

## Components

- `hokieday/agent.py`: bounded provider-neutral loop, exact-number grounding
  check, deterministic narration fallback, response envelope.
- `hokieday/agent_tools.py`: strict schemas, allowlisted dispatch, output bounds,
  coordinate scrubbing, hard-constraint merging, request-scoped schedule gaps.
- `hokieday/agent_prompts.py`: model policy.
- `hokieday/providers/base.py`: normalized provider protocol and typed failures.
- `app/gemini_provider.py`: stdlib Gemini GenerateContent HTTP adapter. The API
  key is sent only in `x-goog-api-key`; provider storage is disabled.
- `app/server.py`: live-mode integration and bounded-parser fallback.

## Tool allowlist

1. `get_campus_schedule`: user-controlled schedule only; computes breakfast,
   lunch, or dinner free windows in code.
2. `plan_day`: deterministic dining/transit/walking feasibility and replanning.
3. `get_events`: bounded September 2026 VT events snapshot.
4. `get_weather`: NWS forecast and alerts; unavailable in incoherent replay.
5. `get_live_bus`: observed BT snapshot, never an exact ETA.
6. `get_next_departures`: service-filtered static schedule.
7. `find_food`: FoodPro search with immutable hard diet/allergen/kcal filters.
8. `get_hours`: FoodPro opening windows.

Model arguments cannot contain URLs, shell commands, file paths, arbitrary
properties, student IDs, raw schedules, or coordinates. Request-scoped schedule
and origin data are injected by code. Coordinates and surrogate student keys are
removed before a tool result is sent to the provider.

## Bounds and failure behavior

- Input: 8 KiB.
- Tool arguments: 8 KiB.
- Tool output: 24 KiB.
- Turns/provider calls: 3 (normally 2).
- Tool calls: 4.
- Identical calls: at most 1.
- Provider timeout: 20 seconds.
- Schedule entries: 200 per request.
- HTTP request body: 1 MiB.
- Live AI questions per client IP: 10/hour by default.

Unknown tools and invalid arguments become typed tool errors. Loop/provider
failures become typed `agent` metadata and `/api/ask` falls back to the bounded
parser. `DEMO_MODE=cache` never constructs or calls a provider. The provider has
process-wide hourly/daily call budgets and the HTTP layer adds a per-client-IP
question throttle; Google account quotas remain the final account-level guard.

## Response envelope

```json
{
  "answer": "Grounded narration",
  "result": {"feasible": true, "itinerary": []},
  "sources": [{"file_id": "plan_day", "title": "..."}],
  "provenance": {
    "provider": "gemini",
    "model": "configured-at-runtime",
    "tool_names": ["plan_day"],
    "replay": false,
    "grounding_replaced": false
  },
  "agent": {"name": "HokieFlow AI", "status": "ok"},
  "_time": {"evaluated_at": "..."}
}
```

When `result` is a plan, its legacy fields are also retained at the top level so
existing clients remain compatible. No chain-of-thought or raw provider request
is returned or logged.

## Schedule privacy

The browser may attach its account-owned schedule entries to that one request.
The tool accepts only bounded title/type/location/start/end/weekly-recurrence
fields needed for the answer. It never receives credentials, PID, CRN, grades,
rosters, transcripts, or account tokens. Malformed and unsupported recurrence is
ignored with a warning; an absent schedule is `unavailable`, never interpreted
as free time.

## Verification

```bash
DEMO_MODE=cache python3 -m unittest tests.test_agent -v
DEMO_MODE=cache python3 -m unittest discover -s tests
python3 -m pyflakes hokieday app scripts tests
git diff --check
```

The app agent is deliberately not named HokieAI. Virginia Tech's HokieAI is a
separate university-supported multi-model platform and is not inherently
trained on HokieFlow campus data.

Gemini usage is capped by default at 30 calls/hour and 200/day per server
process, with 512 output tokens per call and no automatic retry. Real Gemini
access is separate and explicit:

```bash
# GEMINI_API_KEY and GEMINI_MODEL are read from the protected .env file.
DEMO_MODE=live HOKIEFLOW_LIVE_SMOKE=1 python3 scripts/gemini_agent_smoke.py
# Add HOKIEFLOW_SMOKE_VERBOSE=1 to print the model's own prose before grounding.
```

Verified live result (`gemini-3.6-flash`, synthetic CS3114 12:30-13:45 schedule):

> Based on your schedule for Monday ... you have two available windows for lunch
> around your CS3114 class at McBryde Hall (12:30 PM to 1:45 PM): 11:00 AM -
> 12:30 PM (90 minutes); 1:45 PM - 3:00 PM (75 minutes).

`grounding_replaced: false` -- every number came from the tool result, so the
model's own phrasing was kept.

### Every tool exercised live

`scripts/gemini_live_battery.py` asks one question per tool family through the
same deterministic pre-pass a real request uses, reprinting the arguments the
model chose. Verified with `gemini-3.1-flash-lite`; every question selected its
expected tool and kept its own narration (`grounding_replaced: false`):

| Question | Tool | Observed answer |
|---|---|---|
| Walk in the rain this afternoon? | `get_weather` | NWS windows with 18-24% then 45-53% precipitation, discloses stale |
| What's under 500 calories? | `find_food` | D2 open; real items with real kcal and declared allergens |
| D2's hours today? | `get_hours` | 9:30 AM-3:00 PM, 3:00 PM-8:00 PM, "currently open" |
| Events on campus today? | `get_events` | Real Sept 2026 event with venue/time, discloses the partial snapshot |
| Are buses late right now? | `get_live_bus` | Per-route early/late deviations, "not exact arrival ETAs" |
| Next bus from Tennis Courts? | `get_next_departures` | Resolved the name to stop 1125, quoted the scheduled 11:23 AM |
| Eat and reach McBryde by 1:25? | `plan_day` | Feasible itinerary with meal, walk legs and slack |

### Live testing found and fixed real defects

- The model cannot know that D2 is `location_num` 15 or that "Tennis Courts" is
  `1125`, so tools now resolve student-facing names in code and return a typed
  `unknown_location` / `unknown_stop` with real options instead of guessing.
- Godish status disagreed with `get_hours` because dining evaluated "open now"
  against its own clock; tool calls now share the pinned request clock (aware
  values had to be converted, since dining compares naive campus wall-clock).
- A tool that returned nothing usable was still "narratable", which let the
  model answer "everything is under 500 calories" from an empty search. An empty
  result must now be narrated as empty.
- The clock-time grounding check rejected correct answers twice: a model saying
  "3:00 PM" for `15:00:00`, and "9:30 AM" for `09:30:01`. Times now compare at
  minute granularity with 12/24-hour equivalence.
- The final turn is narration-only, so a model that keeps reaching for tools
  still answers from the evidence it already has instead of failing.

### Quota behaviour

A real `HTTP 429` was observed during testing. It surfaces as a typed
`provider_unavailable`, `/api/ask` falls back to the bounded parser, and the
battery stops instead of retrying. The budget guard is persisted in
`cache/ai_provider_calls.json` so a server restart cannot hand out a fresh
allowance while the account quota keeps draining. Defaults: 30 calls/hour and
120/day per account, 10 AI questions/hour per client IP.
