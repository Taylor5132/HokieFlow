# Public class/timetable integration (`hokieday/classes.py`)

Owner: `feature/classes-public` worker. Status: implemented, offline-tested,
**not yet wired into `tools.py` / the app** (parent integration steps below).

This module turns HokieFlow's deadline from a string parsed out of a sentence
("by 1:25") into a **deterministic** next-class deadline: the next timed class
meeting's start time minus a buffer, plus the building it is in. It is a data
module only — no UI, no LLM.

---

## 1. Sources (public only)

| Source | URL | Method | Notes |
|---|---|---|---|
| Timetable form | `https://selfservice.banner.vt.edu/ssb/HZSKVTSC.P_DispRequest` | GET | form + subject list |
| Timetable search | `https://selfservice.banner.vt.edu/ssb/HZSKVTSC.P_ProcRequest` | **POST** | HTML results table |
| Building abbreviations | `https://selfservice.banner.vt.edu/ssb/hzskvtsc.P_DispBldgList` | GET | code → description |
| Final-exam schedule | `https://selfservice.banner.vt.edu/ssb/hzskexam.P_DispExamInfo` | GET | **raw capture only** — the unreliable parser was removed (see §8) |

**Never accessed:** HokieSPA, My VT, any authenticated page, any login. No
credentials, cookies, grades, GPA, rosters, or PIDs are requested or stored.
There is no `catalog.vt.edu` API call anywhere.

### Verified POST form fields (Fall 2026 term `202609`)

`CAMPUS=0`, `TERMYEAR=202609`, `CORE_CODE=AR%`, `subj_code=<SUBJ>`,
`SCHDTYPE=%`, `CRSE_NUMBER=`, `crn=`, `open_only=`, `disp_comments_in=Y`,
`sess_code=%`, `BTN_PRESSED=FIND class sections`, `inst_name=` (empty).

Two field traps, verified against the live endpoint on 2026-09-19:

1. **`CORE_CODE` is required.** Omitting it (or sending it empty) makes Banner
   re-render the search form instead of returning results — HTTP 200 with an
   87 KB page that looks like a result but has no rows. The value must be sent
   verbatim including the literal `%`: `CORE_CODE=AR%` (i.e. URL-encoded
   `AR%25`).
2. `TERMYEAR` is required; `subj_code` is the term's subject code (e.g. `AS`).

Politeness: the edge script sends the project `config.USER_AGENT`, a `Referer`
of the form page, one request at a time, and sleeps `--delay` seconds (default
2 s) between crawl requests. It does **not** crawl unless explicitly told to.

### Fixture provenance

| Fixture | Contents |
|---|---|
| `fixtures/classes_timetable_fall2026_as.html` | redacted `<table …>` fragment of a harmless `subj_code=AS` query, captured 2026-09-19. Every instructor cell was `N/A`; the public page has no student data. This is the ONE canonical timetable fixture. |
| `fixtures/classes_buildings.html` | the public building-abbreviation table (295 codes). |
| `fixtures/classes_schedule_sample.ics` | **synthetic** calendar (3 VEVENTs: weekly MWF with TZID, one-off UTC, floating time). |

Snapshot JSON is **generated deterministically** from the canonical HTML by
`classes.make_snapshot(...)` (the id/content hash include term, query,
fetched_at and a SHA-1 of the HTML). No snapshot JSON is committed, so there is
exactly one copy of the timetable and it cannot drift from the snapshot.

No all-term crawl is committed (deliberate — see §8).

---

## 2. Models (all frozen dataclasses; every one has `to_dict()`)

```
Meeting        days: tuple[str,...]  begin: "HH:MM"|None  end: "HH:MM"|None
               location_raw, building, room, is_tba, is_online
ClassSection   term, crn, subject, course_number, title, schedule_type,
               modality, credit_hours, capacity, instructor, campus,
               meetings: tuple[Meeting,...], comments, exam_code,
               building_names, warnings, snapshot_id, source
ClassOccurrence term, crn, subject, course_number, title, date, start, end, meeting
Conflict       a, b, overlap_min
NextClass      occurrence, minutes_until, leave_by, buffer_min
Building       code, name, lat=None, lon=None, source, gis_verified=False
IcsEvent       uid, summary, location, dtstart, dtend, all_day, tzid, rrule,
               days, recurring, raw_start, raw_end, warnings
TimetableSnapshot term, html, query, fetched_at, source_url, snapshot_id, campus,
               content_sha1
```

`ClassSection.course` = `"AS-1115"`. Identity is **(term, crn)** — CRNs are
reused across terms, so every selection/resolution path carries the term.

### `ClassSection.to_dict()` (API shape)

```json
{
  "term": "202609", "term_name": "Fall 2026", "crn": "81476",
  "subject": "AS", "course_number": "1115", "course": "AS-1115",
  "title": "Introduction to the Air Force",
  "schedule_type": "L", "modality": "Face-to-Face Instruction",
  "credit_hours": "1", "capacity": "60", "instructor": "N/A",
  "campus": "0",
  "meetings": [
    {"days": ["W"], "begin": "10:10", "end": "11:00",
     "location_raw": "CLMS 270", "building": "CLMS", "room": "270",
     "is_tba": false, "is_online": false}
  ],
  "comments": ["MUST SCHEDULE LAB WITH LECTURE"],
  "exam_code": "00X",
  "building_names": {"CLMS": "Corps Ldrship & Military Sci"},
  "flags": {"has_tba": false, "has_unknown_building": false,
            "multiple_meetings": false, "is_online": false},
  "warnings": [], "snapshot_id": "classes_snapshot__…",
  "source": "banner_public"
}
```

---

## 3. Functions

**Parse / search**
- `parse_timetable_html(html, *, term, campus, crosswalk, snapshot_id) -> TimetableParse`
  — one result row = one meeting; rows sharing a CRN merge into one section
  with multiple meetings; `Comments for CRN …` rows attach to their section.
  Tolerates Banner's unclosed `<td>`/`<tr>` and honors `COLSPAN` (an online/TBA
  row merges Begin+End into one cell — without colspan the exam code shifts into
  the Location column).
- `search(sections, *, term, subject, course_number, crn, crns, title_contains,
  days, modality_contains, instructor_contains, schedule_type)` — pure,
  order-preserving AND filter.
- `select_crns(sections, crns, *, term) -> SelectionResult(found, missing,
  invalid, duplicates, ambiguous)` — a CRN present under more than one term
  without an explicit `term` is `ambiguous` and is NOT selected.
- `parse_crns(text)` — splits comma/space/newline runs; a CRN must be 3–5 digits.
- `parse_days`, `parse_clock`, `parse_location`, `parse_course_label`.

**Recurrence (owned by code, never the LLM)**
- `expand_section(section, *, start, end, tz, skip_holidays)` /
  `expand_sections(...)` — weekly expansion of `Meeting.days`, **clamped to the
  section term's VERIFIED window**. A caller can request a sub-window but never
  beyond the term; an unknown/unverified term yields **no occurrences** (typed
  via `term_expandability(term) == (False, reason)`), never a synthesized
  window. TBA/ARR meetings expand to nothing. Holidays are skipped.
- `term_expandability(term) -> (ok, reason)` — the typed availability check.
- `TERM_WINDOWS["202609"]` = classes 2026-08-24 → 2026-12-09, source: VT
  Registrar 2026-2027 calendar (verified). `HOLIDAYS["202609"]` = Labor Day,
  Fall Break, Thanksgiving break.

**Conflicts**
- `find_conflicts(occurrences, *, include_same_crn=False)` — half-open
  intervals; `end == start` is not a conflict; meetings of the same CRN are
  never a conflict by default.
- `schedule_conflicts(sections, *, start, end, tz)`.

**Next class / deadline**
- `next_class(sections, at, *, start, end, buffer_min=10, skip_online, tz,
  ics_events) -> NextClass | None`.
- `combined_occurrences(sections, ics_events, *, start, end, tz)` — Banner
  occurrences (term-clamped) plus ICS occurrences (dated recurrence).
- `next_class_json(...)` — the payload HokieFlow consumes:

```json
{
  "schema": "hokieday.classes.next_class/1",
  "status": "scheduled",
  "at": "2026-09-21T12:00:00-04:00",
  "class_start": "2026-09-21T13:25:00-04:00",
  "class_end": "2026-09-21T14:15:00-04:00",
  "deadline": "2026-09-21T13:15:00-04:00",
  "buffer_min": 10.0, "minutes_until": 85.0,
  "crn": "81478", "course": "AS-1115",
  "building": "CLMS", "room": "270",
  "location_raw": "CLMS 270", "is_online": false
}
```

`deadline` is `class_start − buffer_min`. `status` is `scheduled` | `online` |
`unavailable` | `none`. `buffer_min` must be finite, non-negative and ≤
`MAX_BUFFER_MIN` (240) or `ValueError` is raised. An unknown/unverified term
yields `status="unavailable"` with `unverified_terms` — no fallback window.
TBA meetings are surfaced via `ClassSection.flags().has_tba`.

**Schedule (no DB; JSON/Lakebase-ready records)**
- `add_to_schedule(schedule, sections=None, crns=None, *, term, snapshot_id,
  ics_events)` → schedule list + `added` / `missing` / `invalid` / `ambiguous` /
  `already_in_schedule`. Banner records are `kind="crn"`; ICS records are
  `kind="ics"` with `uid` identity and the dated `dtstart`/`raw_start`/`rrule`.
- `remove_from_schedule(schedule, crns=None, *, term=None, uids=None)` →
  `removed` / `not_in_schedule` / **`ambiguous`** / `invalid`. A CRN that
  appears under more than one term is NOT removed unless `term` is supplied; it
  is reported as ambiguous instead. Removal must never delete the same CRN in
  another term.
- `resolve_schedule(schedule, sections)` — rejoin Banner records by (term, crn).
- `resolve_ics_schedule(schedule)` — rebuild ICS events (re-validated through
  `parse_ics`).
- `schedule_occurrences(schedule, sections, *, start, end, tz)` — dated
  occurrences for both kinds.
- `schedule_json(schedule, sections, *, start, end, snapshot, now, ics_events)`
  → adds `conflicts`, `state`, `occurrence_count`, `unresolved`.

A stored schedule record is deliberately minimal:

```json
{"term": "202609", "crn": "81476", "subject": "AS", "course_number": "1115",
 "course": "AS-1115", "title": "Introduction to the Air Force",
 "meetings": [ … ], "flags": { … }, "snapshot_id": "classes_snapshot__…"}
```

**ICS import (bounded VEVENT subset)**
- `parse_ics(text, *, tz) -> IcsParse(events, warnings, errors, valid)`.
  Supported: `VCALENDAR`/`VEVENT`, line unfolding, `DTSTART`/`DTEND`
  (`VALUE=DATE`, UTC `Z`, `TZID`, or floating→campus-time **warning**),
  `SUMMARY`, `LOCATION`, `UID`, and `FREQ=WEEKLY` with `BYDAY`/`INTERVAL`/
  `COUNT`/`UNTIL`. TEXT escapes (`\n`, `\,`, `\;`, `\\`) are decoded.
  Anything else (`FREQ=MONTHLY`, `WKST`, `RDATE`, `EXDATE`, ordinal `BYDAY`,
  non-positive `INTERVAL`/`COUNT`, invalid `UNTIL`, unknown `TZID`, missing
  `UID`, malformed `DTSTART`, truncated `VEVENT`) is **flagged and skipped**,
  never guessed.
- **Safety bounds** (exceeding is an explicit error): `MAX_ICS_BYTES` (512 KiB),
  `MAX_ICS_LINES` (20 000), `MAX_ICS_EVENTS` (500), `MAX_ICS_OCCURRENCES`
  (2 000), `MAX_ICS_WINDOW_DAYS` (730).
- `expand_ics_event(event, *, start, end, tz)` — a non-recurring event yields a
  single occurrence; a weekly RRULE expands only within `COUNT`/`UNTIL`/window.
  It never raises (a malformed event returns `[]`).
- `expand_ics_events(events, ...)` — many events to one sorted list.
- **No `to_section`.** An ICS event is NOT converted into a Banner section, so
  it can never be expanded as a full-term weekly class. Conflicts/next-class
  consume expanded ICS occurrences directly (`combined_occurrences`,
  `schedule_occurrences`).

**Building crosswalk (ready for the later VT GIS join)**
- `parse_building_list_html(html) -> list[Building]` (295 codes from the fixture;
  multi-word codes like `AJ E` preserved).
- `building_crosswalk(buildings)` → `code.upper() -> Building`.
- `attach_gis_coords(crosswalk, {code: (lat, lon)})` — joins caller-supplied
  coordinates, raises on out-of-range values, never invents a coordinate.
- `crosswalk_contract()` — `{"coords_present": false, "gis_join": {...}}`.
  Unjoined codes keep `lat=None, lon=None, gis_verified=False`.

**Snapshots**
- `make_snapshot(html, *, term, query, source_url, fetched_at, campus)`,
  `save_snapshot`, `load_snapshot`, `snapshot_sections`,
  `snapshot_age_seconds`, `snapshot_is_stale(max_age_s=6h)`.
- The snapshot `id` and `content_sha1` cover term, sanitized query, `fetched_at`
  and a SHA-1 of the HTML, so two captures never collide and the content is
  verifiable.
- `load_snapshot` **rejects a missing/invalid/naive `fetched_at`**, rejects a
  `content_sha1` that does not match the HTML, and **re-sanitizes** the stored
  query.
- `sanitize_query(query)` keeps only whitelisted non-PII keys and drops
  `inst_name`, `pid`, `password`, `cookie`, etc.

---

## 4. Backend response contracts

- **search / list** — `search_result_json(parse, snapshot, *, query, max_age_s,
  now)`, schema `hokieday.classes.search/1`. `state` ∈ `results`,
  `no_results`, `stale_snapshot`.
- **add/list schedule** — `schedule_json(...)`, schema
  `hokieday.classes.schedule/1`. `state` ∈ `ready`, `conflict`,
  `stale_snapshot`; carries `conflicts[]`, `occurrence_count`, `unresolved`.
- **next class** — `next_class_json(...)`, schema
  `hokieday.classes.next_class/1`.
- **snapshot** — schema `hokieday.classes.snapshot/1`.
- **building crosswalk** — schema `hokieday.classes.building_crosswalk/1`.

---

## 5. Figma / UI states (`classes.UI_STATES`)

The backend tags these states so the frontend can render without re-deriving
them. `section_state(section)` returns the per-section ones.

| State | Trigger | Backend signal |
|---|---|---|
| `loading` | request in flight | frontend only; show skeletons, keep prior results dimmed |
| `no_results` | zero rows | `search_result_json.state == "no_results"` |
| `tba_arr` | ARR/TBA meeting | `flags.has_tba`; `section_state` includes `tba_arr` |
| `unknown_building` | code not in crosswalk | `flags.has_unknown_building` |
| `multiple_meetings` | >1 meeting pattern | `flags.multiple_meetings` |
| `conflict` | two selected occurrences overlap | `schedule_json.state == "conflict"` + `conflicts[]` |
| `stale_snapshot` | snapshot older than window | `snapshot.is_stale` and/or top-level `state` |
| `malformed_ics` | ICS warnings/errors | `IcsParse.valid == false`, `warnings`/`errors` |

---

## 6. Edge script (`scripts/fetch_classes.py`)

```
python3 scripts/fetch_classes.py --term 202609 --subject AS
python3 scripts/fetch_classes.py --term 202609 --crn 81476 --name my_crn
python3 scripts/fetch_classes.py --buildings
python3 scripts/fetch_classes.py --exams --term 202609
python3 scripts/fetch_classes.py --list-subjects
# full-term, one snapshot per subject (slow; explicit opt-in):
python3 scripts/fetch_classes.py --all-subjects --yes-crawl --delay 3
```

The library never imports `urllib` (enforced by a test); the POST lives only
here and writes an HTML snapshot envelope the library can parse offline and
`DEMO_MODE=cache` can replay. `--max-subjects N` is a safety valve, `--delay`
must be ≥ 1 s, subject codes are read only from the requested term's case block
and de-duplicated, and the crawl exits non-zero if it writes nothing.
`--exams` captures the raw exam page only (no parser).

---

## 7. Parent integration steps (exact)

1. **Wire `next_class_json` into the planner.** In `hokieday/tools.py` add a
   `LocalSource` method (e.g. `scheduled_classes`) that loads the newest class
   snapshot and caches parsed sections on the instance, then have `plan_day`
   replace the sentence-parsed "1:25" deadline with
   `classes.next_class_json(sections, at, ics_events=...) ["deadline"]`. Keep
   the existing text parser as a fallback when no snapshot exists — a missing
   snapshot returns `status="none"` and an unknown term returns
   `status="unavailable"`, so the demo never invents a window and never depends
   on a live Banner call.
2. **Add a `hokieday/classes.json` snapshot selection.** Prefer
   `config.DATA_DIR / "classes"` (live cache) then any
   `config.FIXTURES_DIR / "classes_snapshot_*.json"` (or generate one from the
   canonical HTML with `make_snapshot`); expose the chosen `snapshot_id` +
   `is_stale` + `content_sha1` in the tool payload so the UI can show the
   `stale_snapshot` state.
3. **Expose the search/list tools** (`find_classes`, `class_schedule`) through
   the same `Source` protocol so the Databricks/Unity Catalog swap stays open.
   Return `search_result_json` / `schedule_json` verbatim. For a selected
   schedule pass `ics_events` (and stored `kind="ics"` records) so conflicts
   and next-class use expanded dated occurrences, not Banner sections.
4. **Seed the frozen demo** by copying the committed fixtures into the replay
   store (mirror `scripts/seed_cache.py`); do not commit a full-term crawl.
5. **Move the four Banner URLs** from `classes.py` into
   `config.ENDPOINTS` if the parent wants one endpoint registry; the constants
   are already named `BANNER_*`.
6. **Optional:** feed `Building` rows and `attach_gis_coords` from the VT GIS
   work (sibling `feature/vtgis-routing` branch) to resolve
   `unknown_building` and give `walk_time` real classroom coordinates.

---

## 8. Limitations & migration fragility

- **Scraping fragility.** Banner is a legacy PL/SQL web app. Field names,
  `COLSPAN` layout, and the results table `SUMMARY` are stable today but are
  not a contract. A banner upgrade or term change can break parsing. Mitigation:
  the parser is defensive (warns instead of guessing), and the snapshot design
  lets a stale-but-parseable HTML copy keep the demo alive. Re-run
  `scripts/fetch_classes.py --subject AS` and re-check the fixture when Banner
  changes.
- **Terms / partial term.** Only `202609` (Fall 2026) and `202612`
  (Winter 26-27) are listed; add a `TERM_WINDOWS` row (with a verified
  academic-calendar source) before expanding a new term. An unknown/unverified
  term expands to NO occurrences (`term_expandability`), never a fallback
  window. A meeting pattern is expanded for the WHOLE term window: Banner does
  not expose part-of-term session dates in the results table, so first/second
  half-term courses are expanded over the full term (documented limitation).
- **No open/closed filtering in code.** The results page shows `Capacity`, not
  seats remaining, so the module does not claim a section is open.
  `--open-only` passes the filter to Banner; the code never re-labels it.
- **No coordinates in the crosswalk.** The VT GIS join is deferred; classroom
  walk times are not available from this data alone.
- **Final exams are not parsed.** The public page's year-less date matrix could
  not be parsed reliably, so the parser was REMOVED rather than shipped with
  wrong dates. `--exams` captures the raw HTML for a future verified parser;
  nothing exam-related is exported today.
- **ICS subset.** Monthly recurrence, exception dates (`EXDATE`/`RDATE`),
  `WKST`, ordinal `BYDAY`, and alarms are out of scope and are rejected/flagged.
  Input size and recurrence are bounded (`MAX_ICS_*`), and an ICS event is never
  turned into a weekly Banner section.
- **No persistence.** Schedule records are plain dicts for a later
  Lakebase/JSON store; nothing is written to a DB by this module.
- **All-term crawl not done.** Deliberately; the fetcher is capable
  (`--all-subjects --yes-crawl`) but a full crawl is large and is a separate,
  polite, off-hours job.
- **Privacy.** No instructor names are stored from the fixture (all `N/A`), and
  `sanitize_query` prevents a query field like `inst_name` from being
  persisted. Grades/GPA/rosters/PIDs never appear in any data path.

---

## 9. Test coverage

`tests/test_classes.py` (offline, `DEMO_MODE=cache`, stdlib `unittest`):

```
DEMO_MODE=cache python3 -m unittest tests.test_classes -v
```

Covers fixture parsing (sections, comments, colspan/TBA/online rows), multi-
meeting merge, `search`/`select_crns`, add/remove/resolve schedule (including
term-scoped removal and ICS UID identity), recurrence + holiday skipping + term
clamping and unknown-term unavailability, conflict detection (Banner×Banner and
Banner×ICS), next-class deadline + buffer bounds, timezone (TZID/UTC/floating)
and unknown-TZID rejection, ICS happy + malformed + unsupported/bounded RRULE,
ICS schedule semantics (no Banner `to_section`, dated recurrence, UID add/
remove), building crosswalk (no coordinates + GIS join validation), snapshot
deterministic generation/content hash/missing-fetched_at rejection/query
sanitization, the edge script's required form fields, per-term subject parsing,
delay floor and nonzero-on-nothing-written, and privacy exclusions (no network
imports in the library, no PII keys in payloads).