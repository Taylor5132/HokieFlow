# Class and campus-event UI integration

Schedule → Find classes or import calendar connects the existing `hokieday/classes.py` contracts to the account calendar. More → Events connects `hokieday/events.py` to the same calendar.

## Implemented

- GET `/api/classes?term=202609&subject=CS&course_number=2506&crn=...` reads newest valid snapshots from `config.DATA_DIR/classes`, falling back to fixture snapshots. Newest term/CRN wins. Results include every meeting block, building names, TBA/unknown-building states, snapshot age, and coverage context.
- POST `/api/classes/preview` accepts `start`, `end`, and either `ics` text or `term`/`crn` plus explicit `allow_term_assumption: true`. Taylor's bounded parser and recurrence logic own all date calculations. Banner dates require opt-in and honor verified term bounds/holidays. Unsupported ICS errors block saving; warnings are visible. No uploaded ICS file is written to disk.
- Preview uses `add_to_schedule`, `schedule_json`, `combined_occurrences`, and `next_class_json`. It returns the source contracts plus normalized account events. Unknown term/TBA, missing snapshots, invalid calendars, and oversized ranges fail visibly.
- Dates are stored as individual `kind: class` meetings with Eastern UTC offsets, `repeat: none`, and stable source/occurrence IDs. This preserves holidays, DST and dated ICS recurrences. Imports are limited to 180 days and the account's existing 200-entry cap; no silent truncation. Existing IDs are skipped on reimport. Reimport is additive, not automatic sync; changed or removed classes must be reviewed manually.
- UI compares imports against saved classes/events, including weekly entries, and shows overlap warnings before a deliberate save. The final write uses the existing authenticated, versioned PUT `/api/account/data`; all other account data is preserved.
- Home's next-three list, calendar editing, directions, and the request-scoped schedule sent to `/api/ask` consume those same account meetings. No separate class database is introduced. Taylor's standalone CRN/ICS records are adapted to this project's existing account format rather than saved in a second store.
- Campus events support login gating, duplicate prevention, account saving, and unknown-end-time editing. Available September 2026 feed is partial and its source notices remain visible.

## Deployment and remaining source work

Run the integrated server with `python -m app.server --port 3002` and configured account dependencies. The older standalone `serve.py` is not the integration server for these endpoints.

Populate the runtime timetable directory with Taylor's existing script, for example:

```sh
python scripts/fetch_classes.py --term 202609 --subject CS --out data/classes
```

A CS snapshot was fetched successfully for local verification. Runtime data is ignored by Git and is not shipped in this change. The deployment needs its own snapshots and refresh job; only loaded subjects can be searched. Full-term ingestion is a separate deliberate operation supported by Taylor's importer. No HokieSPA access or automatic enrollment sync is claimed. Only Fall 2026 has verified Banner term dates; ICS can use other bounded date ranges.

Real Supabase persistence must be verified with the team's deployed auth/database configuration. Browser save verification used an isolated in-memory test account, never a real user's account. Existing planner account-schedule wiring is retained, not replaced by a new class-specific planner or Lakebase implementation. No GIS classroom join is added because this UI uses Apple Maps directions as requested.

## Validation

122 Python class/import tests and 6 JavaScript import/calendar tests passed. Browser check: search real CS 2506 → review 21 meetings → save to isolated test account → Home displays the next three meetings. Tests cover consent, malformed ICS, range bounds, duplicate imports and overlaps with weekly events.
