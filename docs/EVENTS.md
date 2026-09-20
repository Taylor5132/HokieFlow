# Events integration (September 2026, Blacksburg)

Backend contracts and states for the campus-events user flow. Implemented in
`hokieday/events.py` (pure stdlib, offline) with the network edge in
`scripts/crawl_events.py`.

## 1. Source and scope

| | |
|---|---|
| Source | `https://events.vt.edu` — **AEM / Ensemble CMS**, not Localist |
| Discovery | `sitemap.xml` (URL + `lastmod`), month page, year/index listings |
| Detail | public detail HTML with schema.org `Event` microdata |
| Feed? | **none** — no JSON, RSS or ICS exists |
| Window | 2026-09-01 .. 2026-09-30 inclusive (campus time, America/New_York) |
| Geography | Blacksburg-area only (address contains `Blacksburg`/`24061`/`24060`, or a known campus venue) |
| Out of scope | GobblerConnect, HokieSports, authenticated/submission forms |

The month page `/events/2026/09.html` is advertised by the sitemap but returned
**404** on 2026-09-19. The crawler treats that as a normal degradation and falls
back to `/events/2026.html`, `/events.html` and the sitemap. This is why the
snapshot carries `source.month_page_available`.

Coverage is always labelled. The snapshot never claims "all campus events": it
says how many candidates were seen, how many were included, and how many were
excluded as non-Blacksburg / out-of-month.

## 2. Normalized model

`hokieday.events.Event` (no description, image, email or phone field):

`id`, `source_url`, `canonical_url`, `title`, `start`, `end`, `timezone`,
`location_name`, `address`, `tags`, `category`, `admission`,
`registration_url`, `accessibility` (bool), `source_lastmod`, `fetched_at`,
`status`, plus `summary` (opt-in, ≤160 chars, PII-scrubbed) and derived
`in_person` / `free_food` / `duration_unknown` / `not_recommendable` /
`miss_streak` / `all_day`.

* `id` = `evt_<sha1(canonical_url)[:16]>` — stable across crawls.
* `start`/`end` are ISO-8601 **with offset**. The site's quirky
  `2026-09-22T10:00Z-0400` is parsed by `parse_event_time` into `-04:00`.
* `admission` ∈ `free` / `paid` / `unknown`. `admission == free` is
  independent of the `free_food` feature tag.
* `in_person` and `free_food` are **tri-state** `True`/`False`/`None`
  (unknown). `None` means the source did not state it; it is never treated as
  `False`, and a `free_food=True` filter will not match it.
* `duration_unknown` is True when the source states no end time; such an event
  lists in browse but is `not_recommendable` and never "fits" a gap.
* `accessibility` is a **boolean only** — the accessibility contact name,
  email and phone are deliberately dropped.
* `status` ∈ `scheduled`, `cancelled-uncertain`, `cancelled`,
  `parser-failed`, `stale`, `unknown`.

## 3. Snapshot format

`fixtures/events/events_september_2026.json` (schema
`hokieday.events/snapshot/1`):

```json
{
  "schema": "hokieday.events/snapshot/1",
  "month": "2026-09",
  "generated_at": "...", "fetched_at": "...",
  "source": {"sitemap": "...", "month_page_available": false, "crawl_delay_s": 10,
             "refresh": {"fresh_fetches": F, "stale_fallbacks": S,
                          "reused_unchanged": U, "read_only": false}},
  "coverage": {"scope": "...", "note": "Partial coverage: ...",
               "candidates": 67, "detail_parsed": N, "detail_failed": Z,
               "included_blacksburg": M, "excluded_not_blacksburg": X,
               "excluded_out_of_month": Y, "month_page_available": false},
  "parser_failures": ["https://..."],
  "fetch_failures": ["https://..."],
  "events": [ ... ]
}
```

`fetched_at` is the **actual** time of the newest page content, not the time the
snapshot file was written. `source.refresh` distinguishes a real network
refresh from a stale-cache fallback (`cache.age_seconds`, public API), so a
failed refresh never masquerades as fresh.

`load_snapshot(path)` reads it offline; `snapshot_state(snap, now=...)`
returns the worst of `empty` / `partial` / `stale` / `ok`. **Any fetch OR parse
failure makes the snapshot `partial` (never `ok`)** — the crawl saw pages it
could not turn into events.

## 4. User-flow contract

One entry point for the UI:

```python
events.browse(snapshot, query=..., date_=..., start=..., end=...,
              category=..., free=..., free_food=..., in_person=...,
              include_cancelled=True, only_known_location=False,
              limit=..., now=...) -> BrowseResult
```

`BrowseResult = {events, total, state, notices}` where `state` is:

| state | meaning | UI guidance |
|---|---|---|
| `ok` | results found, snapshot fresh | render list |
| `no-match` | snapshot fine, filters/query matched nothing | "No events match these filters" |
| `empty` | snapshot itself is empty | "No September Blacksburg coverage" |
| `stale` | snapshot older than `EVENTS_STALE_AFTER_H` (24 h) | render + age warning |
| `partial` | some pages failed to fetch OR parse | render + "some events may be missing" |
| `out-of-scope` | requested date outside 2026-09 | explain the fixed scope |

`include_cancelled` / `include_uncertain` let the UI hide cancelled rows;
cancelled rows are shown by default with their status so a student never sees a
dead event as live.

The product tools are wired to this contract: `hokieday.tools.get_events(date,
tags)` and `LocalSource.events` load the frozen snapshot and return the typed
envelope `{state, events, reason, coverage, fetched_at}` rather than a bare list.

Search is a case-insensitive substring over title, tags, category, location and
address (`events.search`). Filters compose deterministically and results are
ordered by start time, then id.

### Add to schedule

`to_calendar_event(event)` returns a standard dict (`uid`, `title`, `start`,
`end`, `timezone`, `all_day`, `duration_unknown`, `recommendable`, `location`,
`url`, `description`, `status`) plus an `ics` sub-dict, and `to_ics(events)`
emits a full `VCALENDAR` string. All-day events use `DTSTART;VALUE=DATE` with an
exclusive `DTEND;VALUE=DATE` (the next day). TEXT values are RFC 5545 escaped
(CR/LF neutralized, so a title cannot inject `END:VEVENT`), URI values are
control-stripped, and lines are folded on UTF-8 octet boundaries so long Unicode
titles round-trip. This **only exports** a payload for the frontend's "add to
schedule" action — the backend never writes a user's calendar.

### Free-gap recommendation rule

`fits_gap(event, gaps)` returns the first gap that **fully contains** the event
(inclusive boundaries); a partial overlap does not fit, and an event with an
unknown end time is never treated as zero-length. `recommendable(events, gaps)`
returns only fitting events and excludes cancelled / cancelled-uncertain /
parser-failed / duration-unknown rows. HokieFlow may recommend an event only
when it fits fully inside a free gap.

## 5. Crawl behavior

`scripts/crawl_events.py`:

* descriptive `User-Agent` (`config.USER_AGENT`);
* `robots.txt` parsed with `urllib.robotparser`: Disallow rules are enforced and
  `Crawl-delay` is honoured, with a hard floor of 10 s even if robots cannot be
  read;
* URL allowlist: HTTPS only, exact host `events.vt.edu`, public content paths
  only (localhost, external hosts, non-HTTPS, ports, userinfo and traversal are
  rejected before any socket); relative hrefs are resolved against the page;
* read-only GETs of public pages; never authenticated or write paths;
* content hashes (`sha256`) + sitemap `lastmod` per URL in an incremental state
  file (gitignored cache). Unchanged `lastmod` + existing cached bytes => no
  network call. A **missing lastmod is always revalidated** (never frozen);
* the actual page fetch time is preserved; a failed refresh that falls back to
  stale bytes is recorded as a `stale_fallback`, not stamped as fresh;
* canonical-URL ids so a re-crawl updates rather than duplicates;
* `--max-pages` guard (default 200) refuses to truncate silently;
* `--dry-run` is genuinely read-only (cache-only plan: no network, no cache
  writes, no snapshot) and says so;
* `--from-cache` re-parses cached pages offline and never stamps `now`;
* `--all-locations` / `--include-summary` are opt-in (summary off by default to
  stay copyright-minimal).

### Cancellation uncertainty

`apply_miss_heuristic(prev_events, seen_ids)`:

* event still advertised this crawl -> `scheduled`, miss streak resets;
* missing once -> `cancelled-uncertain` (transient de-listing);
* missing `MISS_THRESHOLD` (2) consecutive crawls -> `cancelled`.

A fetch/parse failure is **not** a miss: the last good copy is carried forward
with its status intact, so a flaky network never cancels an event.

## 6. Privacy / copyright / safety

* Raw detail HTML is cached under the gitignored `cache/`, never committed:
  it contains contact PII (emails, phones) and images.
* The normalized `Event` has no contact fields; `parse_detail` drops the
  contact and accessibility-contact blocks and keeps only a boolean.
* `scrub_pii` strips emails and phone numbers from any opt-in summary.
* Full descriptions are never stored; summaries are off by default and capped.

## 7. Caveats

* Not all campus events: the window is September 2026 and Blacksburg-area. The
  committed snapshot covers 27 of 67 advertised detail pages (33 were stale
  404s), so its state is `partial` by construction.
* `in_person`/`free_food`/`category` are best-effort from tags and listing
  classes; `None`/`unknown` means "not stated", not "false".
* `admission == unknown` is common; treat it as "check the source", not free.
* The month page can 404; coverage metadata records this.
* Times are normalized to campus time but the source's per-event offsets are
  trusted as published.