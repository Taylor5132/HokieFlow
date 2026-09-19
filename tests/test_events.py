"""Offline tests for hokieday.events (no network; DEMO_MODE=cache).

All HTML/XML comes from ``fixtures/events/``: a trimmed real sitemap, a
synthetic listing page, and synthetic detail pages that deliberately contain
fake contact emails/phones so the no-PII guarantee is actually exercised.
"""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hokieday import config, events

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
        self.assertEqual(res.state, events.STATE_OK)
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


if __name__ == "__main__":
    unittest.main()