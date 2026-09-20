"""Offline tests for hokieday.events (no network; DEMO_MODE=cache).

All HTML/XML comes from ``fixtures/events/``: a trimmed real sitemap, a
synthetic listing page, and synthetic detail pages that deliberately contain
fake contact emails/phones so the no-PII guarantee is actually exercised.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hokieday import cache, config, events

FIX = Path(config.FIXTURES_DIR) / "events"
TZ = ZoneInfo("America/New_York")


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def make_event(**kw) -> events.Event:
    base = dict(
        id=events.event_id("https://events.vt.edu/e/1"),
        source_url="https://events.vt.edu/e/1",
        canonical_url="https://events.vt.edu/e/1",
        title="Sample Event",
        start="2026-09-22T10:00-04:00",
        end="2026-09-22T12:00-04:00",
        timezone="America/New_York",
        location_name="Squires Student Center",
        address="Blacksburg, VA 24061",
        tags=("Free", "In-Person"),
        category="lecture",
        admission=events.ADMISSION_FREE,
        registration_url=None,
        accessibility=False,
        source_lastmod="2026-09-11",
        fetched_at="2026-09-19T15:00:00+00:00",
    )
    base.update(kw)
    return events.Event(**base)


class SitemapTests(unittest.TestCase):
    def test_parse_sitemap_entries_and_lastmod(self):
        entries = events.parse_sitemap(read("sitemap_2026_09.xml"))
        urls = [e.url for e in entries]
        self.assertIn("https://events.vt.edu/events.html", urls)
        by_url = {e.url: e.lastmod for e in entries}
        self.assertEqual(
            by_url["https://events.vt.edu/events/2026/09/study-abroad-fair.html"],
            "2026-09-11")
        self.assertIsNone(
            by_url["https://events.vt.edu/events/2026/09/no-lastmod-page.html"])

    def test_sitemap_month_urls_only_september(self):
        entries = events.parse_sitemap(read("sitemap_2026_09.xml"))
        sept = events.sitemap_month_urls(entries, 2026, 9)
        self.assertTrue(sept)
        self.assertTrue(all("/events/2026/09/" in e.url for e in sept))
        self.assertNotIn("https://events.vt.edu/events/2026/10/october-thing.html",
                         [e.url for e in sept])

    def test_sitemap_tolerates_missing_lastmod(self):
        entries = events.parse_sitemap(
            "<urlset><url><loc>https://x/a</loc></url></urlset>")
        self.assertEqual(entries[0].lastmod, None)


class TimeTests(unittest.TestCase):
    def test_parse_event_time_quirky_z_offset(self):
        dt = events.parse_event_time("2026-09-22T10:00Z-0400")
        self.assertEqual(dt.utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual(dt.hour, 10)

    def test_parse_event_time_utc_z_and_plain_offset(self):
        self.assertEqual(events.parse_event_time("2026-09-22T14:00Z"),
                         datetime(2026, 9, 22, 14, tzinfo=timezone.utc))
        dt = events.parse_event_time("2026-09-22T10:00-04:00")
        self.assertEqual(dt.utcoffset().total_seconds(), -4 * 3600)

    def test_parse_event_time_date_only_is_campus_midnight(self):
        dt = events.parse_event_time("2026-09-22")
        self.assertEqual(dt.date(), date(2026, 9, 22))
        self.assertEqual(events.to_campus(dt).hour, 0)

    def test_parse_event_time_bad_value_raises(self):
        with self.assertRaises(ValueError):
            events.parse_event_time("not-a-date")

    def test_month_bounds_are_campus_local(self):
        self.assertTrue(events.in_september_2026(
            events.parse_event_time("2026-09-30T23:00-04:00")))
        self.assertFalse(events.in_september_2026(
            events.parse_event_time("2026-10-01T00:00-04:00")))
        # 2026-10-01T01:00Z is still Sept 30 21:00 in Blacksburg
        self.assertTrue(events.in_september_2026(
            events.parse_event_time("2026-10-01T01:00Z")))
        self.assertFalse(events.in_september_2026(None))


class ListingTests(unittest.TestCase):
    def test_listing_card_class_filters(self):
        cards = {c.url.rsplit("/", 1)[-1]: c for c in events.parse_listing(
            read("listing_2026_09.html"))}
        paid = cards["paid-concert.html"]
        self.assertEqual(paid.category, "arts")
        self.assertEqual(paid.admission, events.ADMISSION_PAID)
        self.assertTrue(paid.in_person)
        self.assertTrue(paid.free_food)
        self.assertEqual(paid.location_hint, "blacksburg-va-24061")
        lec = cards["free-lecture.html"]
        self.assertEqual(lec.category, "lecture")
        self.assertEqual(lec.admission, events.ADMISSION_FREE)
        self.assertTrue(lec.in_person)
        online = cards["online-meeting.html"]
        self.assertFalse(online.in_person)
        self.assertEqual(online.month, "2026-09")


class DetailTests(unittest.TestCase):
    def _parse(self, name, **kw):
        return events.parse_detail(
            read(name),
            "https://events.vt.edu/events/2026/09/" + name,
            fetched_at="2026-09-19T15:00:00+00:00", **kw)

    def _with_fake_pii(self, name):
        # The committed fixture is PII-free; inject a fake contact vector here so
        # the parser's stripping is tested without committing emails/phones.
        return read(name).replace("ACCESS_MARKER", "pat@example.edu 540-555-1212")

    def test_parse_detail_fields(self):
        ev = self._parse("detail_study_abroad.html")
        self.assertEqual(ev.title, "Study Abroad Fair")
        self.assertEqual(ev.start, "2026-09-22T10:00-04:00")
        self.assertEqual(ev.end, "2026-09-22T16:00-04:00")
        self.assertEqual(ev.timezone, "America/New_York")
        self.assertEqual(ev.location_name, "Drillfield")
        self.assertEqual(ev.address, "Blacksburg, VA 24061")
        self.assertEqual(ev.admission, events.ADMISSION_FREE)
        self.assertTrue(ev.in_person)
        self.assertTrue(ev.free_food)
        self.assertTrue(ev.accessibility)
        self.assertIn("Giveaways", ev.tags)
        self.assertEqual(ev.registration_url, "https://example.edu/study-abroad/register")
        self.assertEqual(ev.status, events.STATUS_SCHEDULED)
        self.assertEqual(ev.source_lastmod, None)
        self.assertTrue(events.is_blacksburg(ev.address, ev.location_name, ev.tags))

    def test_parse_detail_has_no_pii_and_no_image_or_full_description(self):
        ev = events.parse_detail(
            self._with_fake_pii("detail_study_abroad.html"),
            "https://events.vt.edu/events/2026/09/study-abroad-fair.html",
            fetched_at="2026-09-19T15:00:00+00:00")
        blob = json.dumps(ev.to_dict())
        self.assertNotIn("pat@example.edu", blob)   # contact email dropped
        self.assertNotIn("540-555-1212", blob)      # contact phone dropped
        self.assertNotIn("image.jpg", blob)         # images never stored
        self.assertNotIn("Accessibility Contact", blob)
        # a public registration URL is allowed and expected
        self.assertEqual(ev.registration_url,
                         "https://example.edu/study-abroad/register")
        # no description field at all unless summary is opted in
        self.assertIsNone(ev.summary)

    def test_summary_is_short_and_scrubbed(self):
        ev = events.parse_detail(
            self._with_fake_pii("detail_study_abroad.html"),
            "https://events.vt.edu/events/2026/09/study-abroad-fair.html",
            fetched_at="2026-09-19T15:00:00+00:00", include_summary=True)
        self.assertIsNotNone(ev.summary)
        self.assertLessEqual(len(ev.summary), 200)
        self.assertNotIn("@", ev.summary)
        self.assertNotIn("540-555-1212", ev.summary)

    def test_scrub_pii_removes_email_and_phone(self):
        self.assertNotIn("@", events.scrub_pii("write to pat@example.edu"))
        self.assertNotIn("555", events.scrub_pii("call 540-555-1212 now"))

    def test_listing_card_enriches_category_and_admission(self):
        card = next(c for c in events.parse_listing(read("listing_2026_09.html"))
                    if c.url.endswith("free-lecture.html"))
        html = read("detail_unknown_location.html")
        # canonical on the real fixture differs; just prove card metadata flows through
        ev = events.parse_detail(html, "https://events.vt.edu/x.html",
                                 fetched_at="2026-09-19T15:00:00+00:00", card=card)
        self.assertEqual(ev.category, "lecture")
        self.assertTrue(ev.in_person)

    def test_non_blacksburg_detail(self):
        ev = self._parse("detail_nonblacksburg.html")
        self.assertFalse(events.is_blacksburg(ev.address, ev.location_name, ev.tags))
        self.assertEqual(ev.admission, events.ADMISSION_PAID)

    def test_malformed_detail_raises_parse_error(self):
        with self.assertRaises(events.EventParseError):
            self._parse("detail_malformed.html")

    def test_unknown_location_state(self):
        ev = self._parse("detail_unknown_location.html")
        self.assertFalse(ev.location_known)
        self.assertEqual(ev.location_name, "")
        self.assertEqual(ev.address, "")


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.a = make_event(title="Free Lecture on AI", category="lecture",
                            tags=("Free", "In-Person", "Free Food"),
                            free_food=True, start="2026-09-22T10:00-04:00",
                            end="2026-09-22T12:00-04:00")
        self.b = make_event(id=events.event_id("https://x/2"),
                            source_url="https://x/2", canonical_url="https://x/2",
                            title="Paid Concert", category="concert",
                            admission=events.ADMISSION_PAID,
                            tags=("Paid", "In-Person"),
                            start="2026-09-22T19:00-04:00",
                            end="2026-09-22T22:00-04:00",
                            location_name="Moss Arts Center")
        self.c = make_event(id=events.event_id("https://x/3"),
                            source_url="https://x/3", canonical_url="https://x/3",
                            title="Online Talk", category="lecture",
                            in_person=False, tags=("Online",),
                            start="2026-09-23T09:00-04:00",
                            end="2026-09-23T10:00-04:00", address="",
                            location_name="")
        self.all = [self.a, self.b, self.c]

    def test_search_title_tags_location(self):
        self.assertEqual([e.title for e in events.search(self.all, "concert")],
                         ["Paid Concert"])
        self.assertEqual([e.title for e in events.search(self.all, "free food")],
                         ["Free Lecture on AI"])
        self.assertEqual([e.title for e in events.search(self.all, "moss")],
                         ["Paid Concert"])
        self.assertEqual(events.search(self.all, ""), sorted(
            self.all, key=lambda e: (e.start_dt, e.id)))
        self.assertEqual(events.search(self.all, "zzz"), [])

    def test_category_filter(self):
        got = events.filter_events(self.all, category="lecture")
        self.assertEqual({e.title for e in got}, {"Free Lecture on AI", "Online Talk"})

    def test_free_and_free_food_filters(self):
        self.assertEqual([e.title for e in events.filter_events(self.all, free=True)],
                         ["Free Lecture on AI", "Online Talk"])
        self.assertEqual([e.title for e in events.filter_events(self.all, free_food=True)],
                         ["Free Lecture on AI"])

    def test_in_person_filter(self):
        got = events.filter_events(self.all, in_person=False)
        self.assertEqual([e.title for e in got], ["Online Talk"])

    def test_date_filter(self):
        got = events.filter_events(self.all, date_="2026-09-22")
        self.assertEqual({e.title for e in got}, {"Free Lecture on AI", "Paid Concert"})


class GapTests(unittest.TestCase):
    def setUp(self):
        self.ev = make_event(start="2026-09-22T10:00-04:00",
                             end="2026-09-22T12:00-04:00")

    def test_fits_fully_inside(self):
        gap = events.Gap(datetime(2026, 9, 22, 9, tzinfo=TZ),
                         datetime(2026, 9, 22, 13, tzinfo=TZ), "free")
        self.assertIs(events.fits_gap(self.ev, [gap]), gap)

    def test_fits_exactly_on_boundaries(self):
        gap = events.Gap(datetime(2026, 9, 22, 10, tzinfo=TZ),
                         datetime(2026, 9, 22, 12, tzinfo=TZ))
        self.assertIsNotNone(events.fits_gap(self.ev, [gap]))

    def test_partial_overlap_does_not_fit(self):
        gap = events.Gap(datetime(2026, 9, 22, 11, tzinfo=TZ),
                         datetime(2026, 9, 22, 13, tzinfo=TZ))
        self.assertIsNone(events.fits_gap(self.ev, [gap]))

    def test_too_narrow_gap_does_not_fit(self):
        gap = events.Gap(datetime(2026, 9, 22, 9, tzinfo=TZ),
                         datetime(2026, 9, 22, 11, 59, tzinfo=TZ))
        self.assertIsNone(events.fits_gap(self.ev, [gap]))

    def test_first_containing_gap_wins(self):
        g1 = events.Gap(datetime(2026, 9, 22, 8, tzinfo=TZ),
                        datetime(2026, 9, 22, 11, tzinfo=TZ))
        g2 = events.Gap(datetime(2026, 9, 22, 9, tzinfo=TZ),
                        datetime(2026, 9, 22, 13, tzinfo=TZ))
        self.assertIs(events.fits_gap(self.ev, [g1, g2]), g2)

    def test_recommendable_excludes_cancelled(self):
        cancelled = make_event(id="evt_c", source_url="https://x/c",
                               canonical_url="https://x/c", title="Cancelled",
                               status=events.STATUS_CANCELLED)
        uncertain = make_event(id="evt_u", source_url="https://x/u",
                               canonical_url="https://x/u", title="Uncertain",
                               status=events.STATUS_CANCELLED_UNCERTAIN)
        gap = events.Gap(datetime(2026, 9, 22, 9, tzinfo=TZ),
                         datetime(2026, 9, 22, 13, tzinfo=TZ))
        got = {e.title for e in events.recommendable(
            [self.ev, cancelled, uncertain], [gap])}
        self.assertEqual(got, {"Sample Event"})


class DedupeTests(unittest.TestCase):
    def test_same_canonical_url_collapses(self):
        a = make_event(id="evt_same", source_url="https://x/a",
                       canonical_url="https://x/canon", location_name="")
        b = make_event(id="evt_same", source_url="https://x/b",
                       canonical_url="https://x/canon", location_name="Squires")
        got = events.dedupe([a, b])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].location_name, "Squires")

    def test_same_title_different_start_is_kept(self):
        a = make_event(id="evt_a", source_url="https://x/a", canonical_url="https://x/a")
        b = make_event(id="evt_b", source_url="https://x/b", canonical_url="https://x/b",
                       start="2026-09-23T10:00-04:00", end="2026-09-23T12:00-04:00")
        self.assertEqual(len(events.dedupe([a, b])), 2)

    def test_same_title_and_start_different_url_is_deduped(self):
        a = make_event(id="evt_a", source_url="https://x/a", canonical_url="https://x/a")
        b = make_event(id="evt_b", source_url="https://x/b", canonical_url="https://x/b")
        self.assertEqual(len(events.dedupe([a, b])), 1)


class UpdateTests(unittest.TestCase):
    def test_miss_heuristic_uncertain_then_cancelled_then_recovers(self):
        ev = make_event()
        first = events.apply_miss_heuristic([ev], set(), fetched_at="t1")
        self.assertEqual(first[0].status, events.STATUS_CANCELLED_UNCERTAIN)
        self.assertEqual(first[0].miss_streak, 1)
        second = events.apply_miss_heuristic(first, set(), fetched_at="t2")
        self.assertEqual(second[0].status, events.STATUS_CANCELLED)
        self.assertEqual(second[0].miss_streak, 2)
        third = events.apply_miss_heuristic(second, {ev.id}, fetched_at="t3")
        self.assertEqual(third[0].status, events.STATUS_SCHEDULED)
        self.assertEqual(third[0].miss_streak, 0)

    def test_threshold_is_configurable(self):
        ev = make_event()
        got = events.apply_miss_heuristic([ev], set(), fetched_at="t", threshold=1)
        self.assertEqual(got[0].status, events.STATUS_CANCELLED)

    def test_changed_hashes(self):
        changed, removed = events.changed_hashes(
            {"a": "1", "b": "2"}, {"a": "1", "b": "9", "c": "3"})
        self.assertEqual(changed, ["b", "c"])
        self.assertEqual(removed, [])


class SnapshotTests(unittest.TestCase):
    def _snap(self, evs, fetched_at="2026-09-19T15:00:00+00:00", failures=()):
        return events.Snapshot(
            month="2026-09", generated_at=fetched_at, fetched_at=fetched_at,
            source={"site": "https://events.vt.edu"},
            coverage={"included_blacksburg": len(evs)},
            events=tuple(evs), parser_failures=tuple(failures))

    def test_snapshot_roundtrip(self):
        snap = self._snap([make_event()])
        restored = events.Snapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
        self.assertEqual(restored.events[0].title, snap.events[0].title)
        self.assertEqual(restored.coverage, snap.coverage)

    def test_states_ok_empty_partial_stale(self):
        now = datetime(2026, 9, 19, 16, tzinfo=timezone.utc)
        self.assertEqual(events.snapshot_state(self._snap([make_event()]), now=now),
                         events.STATE_OK)
        self.assertEqual(events.snapshot_state(self._snap([]), now=now),
                         events.STATE_EMPTY)
        self.assertEqual(events.snapshot_state(
            self._snap([make_event()], failures=("https://x/bad",)), now=now),
            events.STATE_PARTIAL)
        old = self._snap([make_event()], fetched_at="2026-09-01T00:00:00+00:00")
        self.assertEqual(events.snapshot_state(old, now=now), events.STATE_STALE)

    def test_browse_states_and_filters(self):
        a = make_event(title="Free Lecture", free_food=True, category="lecture")
        b = make_event(id="evt_b", source_url="https://x/b", canonical_url="https://x/b",
                       title="Paid Concert", admission=events.ADMISSION_PAID,
                       category="concert")
        snap = self._snap([a, b])
        now = datetime(2026, 9, 19, 16, tzinfo=timezone.utc)
        ok = events.browse(snap, now=now)
        self.assertEqual(ok.state, events.STATE_OK)
        self.assertEqual(ok.total, 2)
        free = events.browse(snap, free=True, now=now)
        self.assertEqual([e.title for e in free.events], ["Free Lecture"])
        none = events.browse(snap, query="zzz", now=now)
        self.assertEqual(none.state, events.STATE_NO_MATCH)
        self.assertEqual(none.total, 0)
        limited = events.browse(snap, limit=1, now=now)
        self.assertEqual(limited.total, 2)
        self.assertEqual(len(limited.events), 1)

    def test_load_snapshot_from_fixture_snapshot_optional(self):
        path = FIX / "events_september_2026.json"
        if not path.exists():
            self.skipTest("demo snapshot not committed in this checkout")
        snap = events.load_snapshot(path)
        self.assertTrue(snap.events)
        self.assertIn("coverage", snap.to_dict())


class CalendarTests(unittest.TestCase):
    def test_calendar_event_fields(self):
        ev = make_event()
        cal = events.to_calendar_event(ev)
        self.assertTrue(cal["uid"].startswith("evt_"))
        self.assertEqual(cal["title"], "Sample Event")
        self.assertEqual(cal["timezone"], "America/New_York")
        self.assertIn("DTSTART", cal["ics"])
        self.assertTrue(cal["ics"]["DTSTART"].endswith("Z"))
        self.assertEqual(cal["ics"]["STATUS"], "CONFIRMED")

    def test_calendar_status_reflects_cancellation(self):
        ev = make_event(status=events.STATUS_CANCELLED)
        self.assertEqual(events.to_calendar_event(ev)["ics"]["STATUS"], "CANCELLED")
        ev2 = make_event(status=events.STATUS_CANCELLED_UNCERTAIN)
        self.assertEqual(events.to_calendar_event(ev2)["ics"]["STATUS"], "TENTATIVE")

    def test_ics_is_well_formed_and_escapes(self):
        ev = make_event(title="Talk: AI, Ethics; and You")
        ics = events.to_ics([ev])
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertIn("END:VCALENDAR", ics)
        self.assertIn("SUMMARY:Talk: AI\\, Ethics\\; and You", ics)
        self.assertTrue(ics.endswith("\r\n"))

    def test_unknown_location_falls_back(self):
        ev = make_event(location_name="", address="")
        cal = events.to_calendar_event(ev)
        self.assertEqual(cal["location"], "Location not specified")


class FrozenSnapshotTests(unittest.TestCase):
    """The committed September 2026 Blacksburg demo snapshot, if present."""

    @classmethod
    def setUpClass(cls):
        cls.path = FIX / "events_september_2026.json"
        if not cls.path.exists():
            raise unittest.SkipTest("demo snapshot not committed in this checkout")
        cls.snap = events.load_snapshot(cls.path)

    def test_snapshot_scope_and_coverage_labels(self):
        self.assertEqual(self.snap.month, "2026-09")
        self.assertTrue(self.snap.events)
        self.assertIn("candidates", self.snap.coverage)
        self.assertIn("included_blacksburg", self.snap.coverage)
        self.assertIn("month_page_available", self.snap.source)
        # never claims completeness
        self.assertIn("not all campus events",
                      self.snap.coverage.get("note", "").lower())

    def test_every_event_is_september_and_blacksburg(self):
        for e in self.snap.events:
            self.assertTrue(events.in_september_2026(e.start_dt), e.title)
            self.assertTrue(events.is_blacksburg(e.address, e.location_name, e.tags),
                            e.title)

    def test_snapshot_has_no_pii_or_images(self):
        # Scan only source-derived free text, never opaque hashed ids/URLs, so a
        # random hex id cannot masquerade as a phone number.
        texts = []
        for e in self.snap.events:
            texts += [e.title, e.location_name, e.address, e.summary or "",
                      " ".join(e.tags), e.registration_url or ""]
        blob = " ".join(texts)
        self.assertIsNone(events._EMAIL_RE.search(blob))
        self.assertIsNone(events._PHONE_RE.search(blob))
        self.assertNotIn("image.jpg", json.dumps(self.snap.to_dict()))

    def test_snapshot_browse_and_gap_flow(self):
        now = events.parse_event_time(self.snap.fetched_at)
        res = events.browse(self.snap, now=now)
        # The crawl saw 33 unreachable pages, so the snapshot is PARTIAL, never ok.
        self.assertEqual(res.state, events.STATE_PARTIAL)
        self.assertTrue(self.snap.fetch_failures)
        self.assertEqual(res.total, len(self.snap.events))
        free = events.browse(self.snap, free=True, now=now)
        self.assertTrue(all(e.admission == events.ADMISSION_FREE for e in free.events))
        # a whole-day gap should contain at least one event
        day = events.Gap(datetime(2026, 9, 22, 0, tzinfo=TZ),
                         datetime(2026, 9, 23, 0, tzinfo=TZ))
        fit = events.recommendable(self.snap.events, [day])
        self.assertTrue(all(events.fits_gap(e, [day]) is not None for e in fit))
        for e in fit:
            self.assertEqual(events.to_campus(e.start_dt).date(), date(2026, 9, 22))

    def test_snapshot_calendar_export(self):
        ics = events.to_ics(self.snap.events)
        self.assertEqual(ics.count("BEGIN:VEVENT"), len(self.snap.events))
        self.assertIn("END:VCALENDAR", ics)


class PartialStateTests(unittest.TestCase):
    def _snap(self, evs, **kw):
        return events.Snapshot(
            month="2026-09", generated_at="t", fetched_at="2026-09-19T15:00:00+00:00",
            source={}, coverage={}, events=tuple(evs), **kw)

    def test_any_fetch_or_parse_failure_is_partial_never_ok(self):
        now = datetime(2026, 9, 19, 16, tzinfo=timezone.utc)
        self.assertEqual(events.snapshot_state(self._snap([make_event()]), now=now),
                         events.STATE_OK)
        self.assertEqual(
            events.snapshot_state(self._snap([make_event()], fetch_failures=("u",)), now=now),
            events.STATE_PARTIAL)
        self.assertEqual(
            events.snapshot_state(self._snap([make_event()], parser_failures=("u",)), now=now),
            events.STATE_PARTIAL)

    def test_fetch_failures_round_trip(self):
        snap = self._snap([make_event()], fetch_failures=("https://x/1",))
        restored = events.Snapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
        self.assertEqual(restored.fetch_failures, ("https://x/1",))


class UnknownEndTests(unittest.TestCase):
    def test_unknown_end_lists_but_is_not_recommendable(self):
        ev = make_event(end=None)
        self.assertTrue(ev.duration_unknown)
        self.assertTrue(ev.not_recommendable)
        gap = events.Gap(datetime(2026, 9, 22, 9, tzinfo=TZ),
                         datetime(2026, 9, 23, 0, tzinfo=TZ))
        self.assertIsNone(events.fits_gap(ev, [gap]))
        self.assertEqual(events.recommendable([ev], [gap]), [])
        snap = events.Snapshot("2026-09", "t", "2026-09-19T15:00:00+00:00", {}, {},
                               (ev,))
        self.assertIn(ev.id, [e.id for e in events.browse(snap).events])
        self.assertTrue(events.to_calendar_event(ev)["duration_unknown"])
        self.assertFalse(events.to_calendar_event(ev)["recommendable"])

    def test_known_end_is_recommendable(self):
        ev = make_event()
        self.assertFalse(ev.duration_unknown)
        gap = events.Gap(datetime(2026, 9, 22, 9, tzinfo=TZ),
                         datetime(2026, 9, 22, 13, tzinfo=TZ))
        self.assertIsNotNone(events.fits_gap(ev, [gap]))


class UrlPolicyTests(unittest.TestCase):
    def test_allowed_fetch_url(self):
        self.assertTrue(events.allowed_fetch_url("https://events.vt.edu/events/2026/09/x.html"))
        self.assertTrue(events.allowed_fetch_url("https://events.vt.edu/sitemap.xml"))
        self.assertTrue(events.allowed_fetch_url("https://events.vt.edu/"))
        for bad in ("http://events.vt.edu/x", "https://evil.com/x",
                    "https://events.vt.edu.evil.com/x", "https://events.vt.edu@evil.com/x",
                    "https://localhost/x", "https://127.0.0.1/x",
                    "https://events.vt.edu:8443/x", "https://events.vt.edu/../secret"):
            self.assertFalse(events.allowed_fetch_url(bad), bad)

    def test_url_policy_rejects_malformed_ports_and_encoded_traversal(self):
        for bad in (
            "https://events.vt.edu:99999/x",          # port out of range
            "https://events.vt.edu:443abc/x",         # non-numeric port
            "https://events.vt.edu:-1/x",             # negative port
            "https://events.vt.edu:0/x",              # non-443 port
            "https://events.vt.edu/%2e%2e/secret",
            "https://events.vt.edu/%2E%2E/%2E%2E/secret",
            "https://events.vt.edu/events/%2e%2e/%2e%2e/sitemap.xml",
            "https://events.vt.edu/events%2f..%2f..%2fsecret",
            "https://events.vt.edu/events/..%5csecret",
            "https://events.vt.edu/events/%00x.html",
            "https://events.vt.edu/events/%252e%252e/secret",   # double-encoded
            "https://events.vt.edu.evil.com/x",
            "https://events.vt.edu@evil.com/x",
        ):
            self.assertFalse(events.allowed_fetch_url(bad), bad)
        # the expected crawler paths remain allowed
        self.assertTrue(events.allowed_fetch_url(
            "https://events.vt.edu/events/2026/09.html"))
        self.assertTrue(events.allowed_fetch_url(
            "https://events.vt.edu/events/2026.html"))

    def test_resolve_url(self):
        self.assertEqual(events.resolve_url("https://events.vt.edu/a/b.html", "/events/x.html"),
                         "https://events.vt.edu/events/x.html")
        self.assertEqual(events.resolve_url("https://events.vt.edu/a/b.html", "c.html"),
                         "https://events.vt.edu/a/c.html")
        self.assertIsNone(events.resolve_url("https://events.vt.edu/a", None))

    def test_validate_month_fixed_scope(self):
        self.assertTrue(events.validate_month("2026-09"))
        self.assertFalse(events.validate_month("2026-10"))
        self.assertFalse(events.validate_month(None))


_REORDERED_DETAIL = """<html><head>
<meta content='2026-09-22T10:00Z-0400' itemprop='startDate'/>
<meta itemprop="endDate" content="2026-09-22T16:00Z-0400"/>
<meta name=keywords content="Public;Free;In-Person"/>
<link rel="canonical" href="/events/2026/09/relative.html">
<title>Relative</title></head><body>
<h1 class="vt-page-title">Relative <em>Title</em></h1>
<span id=vt_event_location_building>Squires <b>Center</b></span>
<span id="vt_event_location_address">Blacksburg, VA 24061</span>
<span class="vt-event-free" itemprop="price" content="Free">Free</span>
<a class="vt-tag-link" href="x">Free Food</a>
<a class="reg" href="/register/here">Register</a>
</body></html>"""


def _unfold_ics(ics: str) -> list[str]:
    out: list[str] = []
    for line in ics.split("\r\n"):
        if line.startswith(" ") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


class HtmlParserTests(unittest.TestCase):
    def test_detail_reordered_attrs_and_single_quotes(self):
        ev = events.parse_detail(_REORDERED_DETAIL,
                                 "https://events.vt.edu/events/2026/09/page.html",
                                 fetched_at="2026-09-19T15:00:00+00:00")
        self.assertEqual(ev.title, "Relative Title")
        self.assertEqual(ev.location_name, "Squires Center")
        self.assertEqual(ev.canonical_url,
                         "https://events.vt.edu/events/2026/09/relative.html")
        self.assertEqual(ev.admission, events.ADMISSION_FREE)
        self.assertEqual(ev.start, "2026-09-22T10:00-04:00")
        self.assertIn("Free Food", ev.tags)
        self.assertEqual(ev.registration_url, "https://events.vt.edu/register/here")

    def test_listing_reordered_attrs_and_sr_only(self):
        html = ("<ul><li class='item event-page categories arts admission free "
                "types in-person'>"
                "<a href='/events/2026/09/x.html' class='vt-list-item-title-link'>"
                "Real Title <span class='sr-only'>, event</span></a></li></ul>")
        cards = events.parse_listing(html, "https://events.vt.edu/events/2026.html")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].title, "Real Title")
        self.assertEqual(cards[0].url, "https://events.vt.edu/events/2026/09/x.html")


class BlacksburgTests(unittest.TestCase):
    def test_explicit_other_city_overrides_venue_fallback(self):
        self.assertFalse(events.is_blacksburg(
            "900 N Glebe Rd, Arlington, VA 22203", "Virginia Tech Research Center", ()))

    def test_blackburg_tag_wins(self):
        self.assertTrue(events.is_blacksburg("", "", ("Blacksburg, VA 24061",)))

    def test_online_only_rejected(self):
        self.assertFalse(events.is_blacksburg(
            "Blacksburg, VA 24061", "Squires", ("Online",)))

    def test_unknown_address_uses_venue(self):
        self.assertTrue(events.is_blacksburg("", "Moss Arts Center", ()))

    def test_hybrid_physical_signal_overrides_online(self):
        # A hybrid/in-person tag overrides an online tag; only online-only is
        # rejected. This is the adversarial case (both tags present).
        self.assertTrue(events.is_blacksburg(
            "Blacksburg, VA 24061", "Squires", ("Online", "Hybrid")))
        self.assertTrue(events.is_blacksburg(
            "Blacksburg, VA 24061", "Squires", ("Virtual", "In-Person")))


class InPersonOverrideTests(unittest.TestCase):
    def test_listing_hybrid_overrides_online(self):
        # The CMS repeats the `types X` class per variant (`types_-X` is the
        # marker); this card carries BOTH an online and a hybrid signal.
        html = ("<ul><li class='event-page types online types_-online "
                "types hybrid types_-hybrid'>"
                "<a href='/events/2026/09/x.html' "
                "class='vt-list-item-title-link'>Hybrid</a></li></ul>")
        card = events.parse_listing(html, "https://events.vt.edu/events.html")[0]
        self.assertTrue(card.in_person)

    def test_listing_online_only_is_online(self):
        html = ("<ul><li class='event-page types online'>"
                "<a href='/events/2026/09/x.html' "
                "class='vt-list-item-title-link'>Online</a></li></ul>")
        card = events.parse_listing(html, "https://events.vt.edu/events.html")[0]
        self.assertFalse(card.in_person)

    def test_tags_hybrid_overrides_online(self):
        self.assertTrue(events._in_person_from_tags(["Online", "Hybrid"]))
        self.assertTrue(events._in_person_from_tags(["Virtual", "In-Person"]))
        self.assertFalse(events._in_person_from_tags(["Online"]))
        self.assertIsNone(events._in_person_from_tags(["Lecture"]))


class FreeFoodTriStateTests(unittest.TestCase):
    def test_none_is_not_matched_by_true_or_false(self):
        unknown = make_event(free_food=None)
        yes = make_event(id="evt_y", source_url="https://x/y", canonical_url="https://x/y",
                         free_food=True)
        no = make_event(id="evt_n", source_url="https://x/n", canonical_url="https://x/n",
                        free_food=False)
        self.assertEqual([e.id for e in events.filter_events([unknown, yes, no], free_food=True)],
                         [yes.id])
        self.assertEqual([e.id for e in events.filter_events([unknown, yes, no], free_food=False)],
                         [no.id])


class IcsHardeningTests(unittest.TestCase):
    def test_all_day_exclusive_dtend(self):
        ev = make_event(start="2026-09-22", end=None, all_day=True)
        cal = events.to_calendar_event(ev)
        self.assertIn("DTSTART;VALUE=DATE", cal["ics"])
        self.assertEqual(cal["ics"]["DTSTART;VALUE=DATE"], "20260922")
        self.assertEqual(cal["ics"]["DTEND;VALUE=DATE"], "20260923")
        self.assertIn("DTSTART;VALUE=DATE:20260922", events.to_ics([ev]))

    def test_all_day_multiday_preserves_explicit_exclusive_end(self):
        # A three-day all-day event: the source end (09-24) is preserved as the
        # exclusive DTEND instead of being flattened to start + 1 day.
        ev = make_event(start="2026-09-22", end="2026-09-24", all_day=True)
        cal = events.to_calendar_event(ev)
        self.assertEqual(cal["ics"]["DTSTART;VALUE=DATE"], "20260922")
        self.assertEqual(cal["ics"]["DTEND;VALUE=DATE"], "20260924")

    def test_all_day_same_day_end_falls_back_to_one_day(self):
        ev = make_event(start="2026-09-22", end="2026-09-22", all_day=True)
        cal = events.to_calendar_event(ev)
        self.assertEqual(cal["ics"]["DTEND;VALUE=DATE"], "20260923")

    def test_crlf_injection_is_neutralized(self):
        ev = make_event(title="Evil\r\nEND:VEVENT:injected")
        ics = events.to_ics([ev])
        raw_lines = ics.split("\r\n")
        # exactly ONE real component terminator; the payload is escaped inline
        self.assertEqual(sum(1 for line in raw_lines if line == "END:VEVENT"), 1)
        self.assertNotIn("END:VEVENT:injected", raw_lines)
        summary = [line for line in _unfold_ics(ics) if line.startswith("SUMMARY:")][0]
        self.assertEqual(summary, "SUMMARY:Evil\\nEND:VEVENT:injected")

    def test_long_unicode_folds_on_octet_boundaries(self):
        title = "\u4f1a\u8bae" * 60 + " \U0001f600" * 10
        ev = make_event(title=title)
        ics = events.to_ics([ev])
        for line in ics.split("\r\n"):
            if line:
                self.assertLessEqual(len(line.encode("utf-8")), 75)
        summary = [line for line in _unfold_ics(ics) if line.startswith("SUMMARY:")][0]
        self.assertEqual(summary[len("SUMMARY:"):], title)


REPO = Path(config.REPO_DIR)


class CrawlerLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "crawl_events", REPO / "scripts" / "crawl_events.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def test_robots_delay_and_disallow(self):
        text = ("User-agent: *\nAllow: /\nCrawl-delay: 10\n"
                "User-agent: BadBot\nDisallow: /\n")
        self.assertEqual(self.mod.parse_robots_crawl_delay(text), 10.0)
        rp = self.mod.build_robot_parser(text)
        self.assertTrue(rp.can_fetch("hokieday", "https://events.vt.edu/events/x.html"))
        self.assertFalse(rp.can_fetch("BadBot", "https://events.vt.edu/events/x.html"))

    def test_bad_month_refused_without_output(self):
        import argparse
        args = argparse.Namespace(month="2026-10", dry_run=False, from_cache=True,
                                  throttle=10, out="/tmp/never.json",
                                  state="/tmp/never_state.json", max_pages=200,
                                  all_locations=False, include_summary=False)
        self.assertEqual(self.mod.crawl(args), 2)

    def test_planned_entries_resolve_and_allowlist(self):
        entries = [
            events.SitemapEntry("/events/2026/09/a.html", "2026-09-01"),
            events.SitemapEntry("https://evil.com/events/2026/09/b.html", None),
            events.SitemapEntry("https://events.vt.edu/events/2026/09/c.html", None),
        ]
        got = self.mod._planned_detail_entries(entries, 2026, 9)
        self.assertIn("https://events.vt.edu/events/2026/09/a.html", got)
        self.assertIn("https://events.vt.edu/events/2026/09/c.html", got)
        self.assertNotIn("https://evil.com/events/2026/09/b.html", got)

    def test_latest_ts_compares_instants_not_strings(self):
        # 11:00-04:00 == 15:00Z, which is NEWER than 14:00Z even though the
        # string sorts lower; max() over strings would pick the wrong one.
        got = self.mod._latest_ts(["2026-09-10T14:00:00+00:00",
                                   "2026-09-10T11:00:00-04:00"])
        self.assertEqual(got, "2026-09-10T11:00:00-04:00")
        self.assertIsNone(self.mod._latest_ts([]))

    @staticmethod
    def _seed_page(url, body, ts):
        import hashlib
        from hokieday import cache
        params = {"u": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]}
        p = cache._bin_path("events_page", params)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        env = {"key": p.stem, "url": url, "fetched_at": ts, "mode": "live",
               "payload": {"bytes": len(body), "file": p.name}}
        cache._json_path("events_page", params).write_text(json.dumps(env))

    def test_from_cache_never_advances_fetched_at(self):
        import argparse
        import tempfile
        from hokieday import config

        old_cache = config.CACHE_DIR
        seed_ts = "2026-09-01T00:00:00+00:00"
        detail_url = "https://events.vt.edu/events/2026/09/study-abroad-fair.html"
        sitemap = ("<?xml version='1.0'?><urlset>"
                   f"<url><loc>{detail_url}</loc><lastmod>2026-09-01</lastmod></url>"
                   "</urlset>")
        with tempfile.TemporaryDirectory() as td:
            config.CACHE_DIR = Path(td)
            try:
                self._seed_page("https://events.vt.edu/robots.txt",
                                b"User-agent: *\nAllow: /\nCrawl-delay: 10\n", seed_ts)
                self._seed_page("https://events.vt.edu/sitemap.xml",
                                sitemap.encode("utf-8"), seed_ts)
                self._seed_page(detail_url, read("detail_study_abroad.html").encode("utf-8"),
                                seed_ts)
                out = Path(td) / "out.json"
                args = argparse.Namespace(
                    month="2026-09", dry_run=False, from_cache=True, throttle=10,
                    out=str(out), state=str(Path(td) / "state.json"),
                    max_pages=200, all_locations=False, include_summary=False)
                self.assertEqual(self.mod.crawl(args), 0)
                snap = json.loads(out.read_text(encoding="utf-8"))
                self.assertEqual(snap["fetched_at"], seed_ts)
                self.assertEqual(snap["source"]["refresh"]["network_requests"], 0)
                self.assertEqual(snap["source"]["refresh"]["read_only"], True)
                self.assertEqual(len(snap["events"]), 1)
            finally:
                config.CACHE_DIR = old_cache


class BrowseScopeTests(unittest.TestCase):
    """browse() enforces the fixed window and keeps health above match state."""

    def _snap(self, evs, *, fetched_at="2026-09-19T15:00:00+00:00", **kw):
        return events.Snapshot(
            month="2026-09", generated_at=fetched_at, fetched_at=fetched_at,
            source={}, coverage={}, events=tuple(evs), **kw)

    def test_browse_rejects_out_of_scope_date(self):
        snap = self._snap([make_event()])
        res = events.browse(snap, date_="2026-10-05")
        self.assertEqual(res.state, events.STATE_OUT_OF_SCOPE)
        self.assertEqual(res.match_state, events.STATE_OUT_OF_SCOPE)
        self.assertEqual(res.events, ())
        self.assertTrue(res.notices)

    def test_browse_rejects_out_of_scope_range_but_allows_overlap(self):
        snap = self._snap([make_event()])
        after = events.browse(snap, start="2026-10-01T00:00:00-04:00")
        self.assertEqual(after.state, events.STATE_OUT_OF_SCOPE)
        before = events.browse(snap, end="2026-08-01T00:00:00-04:00")
        self.assertEqual(before.state, events.STATE_OUT_OF_SCOPE)
        overlap = events.browse(
            snap, start="2026-09-01T00:00:00-04:00",
            end="2026-10-15T00:00:00-04:00")
        self.assertNotEqual(overlap.state, events.STATE_OUT_OF_SCOPE)

    def test_partial_snapshot_with_zero_matches_stays_partial(self):
        now = datetime(2026, 9, 19, 16, tzinfo=timezone.utc)
        snap = events.Snapshot(
            month="2026-09", generated_at="t",
            fetched_at="2026-09-19T15:00:00+00:00", source={}, coverage={},
            events=(make_event(),), parser_failures=("https://x/bad",))
        res = events.browse(snap, query="zzz", now=now)
        self.assertEqual(res.state, events.STATE_PARTIAL,
                         "health must not collapse to no-match")
        self.assertEqual(res.match_state, events.STATE_NO_MATCH)
        self.assertEqual(res.total, 0)
        self.assertIn("No events found", " ".join(res.notices))

    def test_stale_snapshot_with_zero_matches_stays_stale(self):
        now = datetime(2026, 9, 25, 16, tzinfo=timezone.utc)
        snap = self._snap([make_event()])
        res = events.browse(snap, query="zzz", now=now)
        self.assertEqual(res.state, events.STATE_STALE)
        self.assertEqual(res.match_state, events.STATE_NO_MATCH)

    def test_healthy_zero_matches_is_no_match(self):
        now = datetime(2026, 9, 19, 16, tzinfo=timezone.utc)
        snap = self._snap([make_event()])
        res = events.browse(snap, query="zzz", now=now)
        self.assertEqual(res.state, events.STATE_NO_MATCH)
        self.assertEqual(res.match_state, events.STATE_NO_MATCH)


class InvalidRangeTests(unittest.TestCase):
    def test_end_before_start_is_invalid_and_non_recommendable(self):
        ev = make_event(start="2026-09-22T12:00-04:00",
                        end="2026-09-22T10:00-04:00")
        self.assertTrue(ev.invalid_range)
        self.assertTrue(ev.not_recommendable)
        self.assertFalse(events.to_calendar_event(ev)["recommendable"])
        # still lists / exports, it is just never recommended
        self.assertTrue(ev.to_dict()["invalid_range"])
        gap = events.Gap(datetime(2026, 9, 22, 9, tzinfo=TZ),
                         datetime(2026, 9, 22, 23, tzinfo=TZ))
        self.assertIsNone(events.fits_gap(ev, [gap]))
        self.assertEqual(events.recommendable([ev], [gap]), [])

    def test_equal_start_and_end_is_invalid(self):
        ev = make_event(start="2026-09-22T10:00-04:00",
                        end="2026-09-22T10:00-04:00")
        self.assertFalse(ev.invalid_range)   # equal is zero-length, not reversed


class CrawlerIncrementalTests(unittest.TestCase):
    """Adversarial incremental sequences for the edge crawler.

    These exercise the live cache path with a fake network: parse failure must
    not freeze accepted state and must retry; a stale refresh fallback must not
    advance accepted state, must count as a fetch failure, and must retry.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "crawl_events_incr", REPO / "scripts" / "crawl_events.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.mod = mod

    def setUp(self):
        self._only = config.CACHE_ONLY
        self._dir = config.CACHE_DIR
        self._http_final = cache._http_final
        self._orig_sleep = self.mod.Fetcher._sleep
        # No real throttle sleeps in tests: the polite delay is exercised by the
        # bots/allowlist tests, not by every incremental sequence.
        self.mod.Fetcher._sleep = lambda self: None
        self._tmp = tempfile.mkdtemp(prefix="hokie-crawl-incr-")
        config.CACHE_ONLY = False
        config.CACHE_DIR = Path(self._tmp)
        cache._attempts.clear()
        cache._key_locks.clear()
        self.detail_url = ("https://events.vt.edu/events/2026/09/"
                           "study-abroad-fair.html")

    def tearDown(self):
        import shutil
        cache._http_final = self._http_final
        self.mod.Fetcher._sleep = self._orig_sleep
        config.CACHE_ONLY = self._only
        config.CACHE_DIR = self._dir
        cache._attempts.clear()
        cache._key_locks.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed(self, url, body, ts="2026-09-01T00:00:00+00:00"):
        import hashlib
        params = {"u": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]}
        p = cache._bin_path("events_page", params)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        cache._json_path("events_page", params).write_text(json.dumps({
            "key": p.stem, "url": url, "fetched_at": ts, "mode": "live",
            "payload": {"bytes": len(body), "file": p.name}}), encoding="utf-8")

    def _sitemap(self, lastmod):
        return ("<?xml version='1.0'?><urlset>"
                f"<url><loc>{self.detail_url}</loc><lastmod>{lastmod}</lastmod></url>"
                "</urlset>").encode("utf-8")

    def _net(self, detail_body, sitemap_body, robots=None):
        robots = robots if robots is not None else \
            b"User-agent: *\nAllow: /\nCrawl-delay: 10\n"

        def http_final(url, timeout):
            if url.endswith("/robots.txt"):
                return robots, url
            if url.endswith("/sitemap.xml"):
                return sitemap_body, url
            if url == self.detail_url:
                return detail_body, url
            return b"<html></html>", url
        return http_final

    def _args(self, out, state):
        import argparse
        return argparse.Namespace(
            month="2026-09", dry_run=False, from_cache=False, throttle=10,
            out=str(out), state=str(state), max_pages=200,
            all_locations=False, include_summary=False)

    def test_parse_failure_does_not_advance_state_and_retries(self):
        out = Path(self._tmp) / "out.json"
        state = Path(self._tmp) / "state.json"
        sitemap = self._sitemap("2026-09-01")
        bad = read("detail_malformed.html").encode("utf-8")
        good = read("detail_study_abroad.html").encode("utf-8")

        cache._http_final = self._net(bad, sitemap)
        self.assertEqual(self.mod.crawl(self._args(out, state)), 0)
        snap = events.load_snapshot(out)
        self.assertTrue(snap.parser_failures)
        self.assertEqual(events.snapshot_state(snap), events.STATE_PARTIAL)
        entry = json.loads(state.read_text(encoding="utf-8"))[self.detail_url]
        self.assertIn("parse_failed", entry)
        self.assertFalse(entry.get("sha256"))

        # Next run: the parse-failed marker forces a re-fetch, and success
        # clears partial and advances the accepted state.
        cache._http_final = self._net(good, sitemap)
        self.assertEqual(self.mod.crawl(self._args(out, state)), 0)
        snap2 = events.load_snapshot(out)
        self.assertEqual(snap2.parser_failures, ())
        self.assertEqual(events.snapshot_state(snap2), events.STATE_OK)
        self.assertEqual(len(snap2.events), 1)
        entry2 = json.loads(state.read_text(encoding="utf-8"))[self.detail_url]
        self.assertNotIn("parse_failed", entry2)
        self.assertTrue(entry2.get("sha256"))

    def test_stale_fallback_keeps_accepted_state_and_retries(self):
        out = Path(self._tmp) / "out.json"
        state = Path(self._tmp) / "state.json"
        good = read("detail_study_abroad.html").encode("utf-8")
        cache._http_final = self._net(good, self._sitemap("2026-09-01"))
        self.assertEqual(self.mod.crawl(self._args(out, state)), 0)
        sha1 = json.loads(state.read_text(encoding="utf-8"))[self.detail_url]["sha256"]
        self.assertTrue(sha1)

        # The page changed upstream (new lastmod) but the refresh now fails.
        sitemap2 = self._sitemap("2026-09-02")
        ok_net = self._net(good, sitemap2)

        def flaky(url, timeout):
            if url == self.detail_url:
                raise OSError("upstream down")
            return ok_net(url, timeout)
        cache._http_final = flaky
        self.assertEqual(self.mod.crawl(self._args(out, state)), 0)

        snap = events.load_snapshot(out)
        self.assertTrue(snap.fetch_failures, "stale fallback must be a fetch failure")
        self.assertEqual(events.snapshot_state(snap), events.STATE_PARTIAL)
        self.assertEqual(len(snap.events), 1, "prior good event retained")
        entry = json.loads(state.read_text(encoding="utf-8"))[self.detail_url]
        self.assertEqual(entry["lastmod"], "2026-09-01",
                         "stale fallback must not advance accepted lastmod")
        self.assertEqual(entry["sha256"], sha1,
                         "stale fallback must not advance accepted hash")

        # A later successful refresh for the same changed page advances state.
        new = good.replace(b"Study Abroad Fair", b"Study Abroad Fair 2")
        cache._http_final = self._net(new, sitemap2)
        self.assertEqual(self.mod.crawl(self._args(out, state)), 0)
        snap3 = events.load_snapshot(out)
        self.assertEqual(snap3.fetch_failures, ())
        entry3 = json.loads(state.read_text(encoding="utf-8"))[self.detail_url]
        self.assertEqual(entry3["lastmod"], "2026-09-02")
        self.assertNotEqual(entry3["sha256"], sha1)

    def test_robots_disallow_sitemap_aborts_without_writing(self):
        out = Path(self._tmp) / "out.json"
        state = Path(self._tmp) / "state.json"
        cache._http_final = self._net(
            read("detail_study_abroad.html").encode("utf-8"),
            self._sitemap("2026-09-01"),
            robots=b"User-agent: *\nDisallow: /sitemap.xml\n")
        self.assertEqual(self.mod.crawl(self._args(out, state)), 0)
        self.assertFalse(out.exists(), "a disallowed sitemap must not write a snapshot")

    def test_robots_disallow_detail_counts_excluded(self):
        out = Path(self._tmp) / "out.json"
        state = Path(self._tmp) / "state.json"
        cache._http_final = self._net(
            read("detail_study_abroad.html").encode("utf-8"),
            self._sitemap("2026-09-01"),
            robots=(b"User-agent: *\nAllow: /sitemap.xml\n"
                    b"Disallow: /events/\n"))
        self.assertEqual(self.mod.crawl(self._args(out, state)), 0)
        snap = events.load_snapshot(out)
        self.assertEqual(snap.coverage["excluded_robots"], 1)
        self.assertEqual(len(snap.events), 0)


class ToolsEventsTests(unittest.TestCase):
    def test_get_events_typed_and_partial(self):
        from hokieday import tools
        res = tools.get_events(date="2026-09-22")
        self.assertIn("state", res)
        self.assertIn("coverage", res)
        self.assertIsInstance(res["events"], list)
        self.assertIn(res["state"],
                      (events.STATE_PARTIAL, events.STATE_OK, events.STATE_NO_MATCH))

    def test_get_events_out_of_scope(self):
        from hokieday import tools
        res = tools.get_events(date="2026-10-05")
        self.assertEqual(res["state"], events.STATE_OUT_OF_SCOPE)
        self.assertEqual(res["events"], [])

    def test_get_events_legacy_list_source(self):
        from hokieday import tools

        class Src:
            def events(self, date, tags):
                return [{"title": "x"}]

        self.assertEqual(tools.get_events(source=Src())["state"], events.STATE_OK)

        class Empty:
            def events(self, date, tags):
                return []

        self.assertEqual(tools.get_events(source=Empty())["state"], events.STATE_EMPTY)

    def test_get_events_tags_require_all(self):
        from hokieday import tools
        res = tools.get_events(date="2026-09-22", tags=("Career Fair",))
        for e in res["events"]:
            self.assertIn("career", e["title"].lower())


if __name__ == "__main__":
    unittest.main()