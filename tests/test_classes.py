"""Offline tests for hokieday.classes (public VT timetable integration).

Run:  DEMO_MODE=cache python3 -m unittest tests.test_classes -v

Everything is offline. The only captive data is:
  * fixtures/classes_timetable_fall2026_as.html -- a REDACTED fragment of a
    harmless public AS-subject query captured 2026-09-19 (instructor cells are
    all "N/A"; no student data exists on the public page at all),
  * fixtures/classes_buildings.html -- the public building-abbreviation page,
  * fixtures/classes_snapshot_fall2026_as.json -- a generated snapshot envelope,
  * fixtures/classes_schedule_sample.ics -- a SYNTHETIC calendar.
"""
import ast
import importlib.util
import os
import tempfile

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hokieday import classes, config

FIX = Path(__file__).resolve().parent.parent / "fixtures"
TIMETABLE_HTML = FIX / "classes_timetable_fall2026_as.html"
BUILDINGS_HTML = FIX / "classes_buildings.html"
SNAPSHOT_JSON = FIX / "classes_snapshot_fall2026_as.json"
ICS_SAMPLE = FIX / "classes_schedule_sample.ics"

TZ = ZoneInfo("America/New_York")


def setUpModule():
    if not config.CACHE_ONLY:
        raise RuntimeError(
            "tests.test_classes requires DEMO_MODE=cache (network must stay off)")
    for p in (TIMETABLE_HTML, BUILDINGS_HTML, SNAPSHOT_JSON, ICS_SAMPLE):
        if not p.exists():
            raise RuntimeError(f"missing fixture {p}")


def _table(rows: str) -> str:
    return (
        '<table class="dataentrytable" '
        'SUMMARY="This table displays the Timetable of Classes">'
        "<tr><td>CRN</td><td>Course</td><td>Title</td><td>Schedule Type</td>"
        "<td>Modality</td><td>Cr Hrs</td><td>Capacity</td><td>Instructor</td>"
        "<td>Days</td><td>Begin</td><td>End</td><td>Location</td><td>Exam</td></tr>"
        f"{rows}</table>"
    )


def _row(crn, course="CS-2114", title="Software Design", stype="L",
         modality="Face-to-Face Instruction", cr="3", cap="100",
         instructor="N/A", days="M", begin="10:00AM", end="11:15AM",
         loc="MCB 100", exam="00X", extra_cells=""):
    return (
        "<tr>"
        f"<td>{crn}</td><td>{course}</td><td>{title}</td><td>{stype}</td>"
        f"<td>{modality}</td><td>{cr}</td><td>{cap}</td><td>{instructor}</td>"
        f"<td>{days}</td><td>{begin}</td><td>{end}</td><td>{loc}</td>"
        f"<td>{exam}</td>{extra_cells}</tr>"
    )


def _section(crn, days=("M",), begin="10:00", end="11:15",
             building="MCB", room="100", term="202609", subject="CS",
             course="2114", title="Software Design", is_tba=False,
             is_online=False):
    m = classes.Meeting(
        days=tuple(days), begin=begin, end=end,
        location_raw=f"{building} {room}" if building else "",
        building=building, room=room, is_tba=is_tba, is_online=is_online)
    return classes.ClassSection(
        term=term, crn=crn, subject=subject, course_number=course,
        title=title, meetings=(m,))


# ===========================================================================
class TestFixtureParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.term = "202609"
        cls.parse = classes.parse_timetable_html(
            TIMETABLE_HTML.read_text(encoding="utf-8"), term=cls.term)
        cls.by_crn = {s.crn: s for s in cls.parse.sections}

    def test_fifteen_sections_no_error(self):
        self.assertFalse(self.parse.empty)
        self.assertEqual(len(self.parse.sections), 15)
        self.assertEqual(self.parse.errors, ())

    def test_crn_81476_fields(self):
        s = self.by_crn["81476"]
        self.assertEqual(s.course, "AS-1115")
        self.assertEqual(s.title, "Introduction to the Air Force")
        self.assertEqual(s.schedule_type, "L")
        self.assertEqual(s.credit_hours, "1")
        self.assertEqual(s.capacity, "60")
        self.assertEqual(s.exam_code, "00X")
        self.assertEqual(s.term, "202609")
        m = s.meetings[0]
        self.assertEqual(m.days, ("W",))
        self.assertEqual((m.begin, m.end), ("10:10", "11:00"))
        self.assertEqual((m.building, m.room), ("CLMS", "270"))
        self.assertFalse(m.is_tba)

    def test_comments_attached(self):
        self.assertIn("MUST SCHEDULE LAB WITH LECTURE", self.by_crn["81476"].comments)

    def test_tba_and_online_row_colspan_alignment(self):
        # Banner merges Begin+End (colspan=2) and the modality cell says
        # ONLINE COURSE. The parser must honor COLSPAN or the exam code shifts
        # into the Location column (a real bug caught by this fixture).
        s = self.by_crn["92874"]
        m = s.meetings[0]
        self.assertTrue(m.is_online)
        self.assertEqual(m.building, "ONLINE")
        self.assertIsNone(m.begin)
        self.assertTrue(s.has_tba)
        self.assertEqual(s.exam_code, "00X")
        self.assertIn("tba_arr", classes.section_state(s))

    def test_tr_meeting_parsed(self):
        s = self.by_crn["81481"]
        self.assertEqual(s.meetings[0].days, ("T", "R"))
        self.assertEqual((s.meetings[0].begin, s.meetings[0].end), ("08:00", "09:15"))

    def test_instructor_cell_is_na_no_pii(self):
        self.assertEqual({s.instructor for s in self.parse.sections}, {"N/A"})

    def test_section_dict_has_no_pii_fields(self):
        d = self.by_crn["81476"].to_dict()
        forbidden = {"pid", "gpa", "grades", "roster", "email", "student_id",
                     "banner_id", "u_no", "instructor_email", "password"}
        self.assertFalse(forbidden & set(d))
        self.assertNotIn("@", json.dumps(d))

    def test_flags_and_states(self):
        s = self.by_crn["81476"]
        self.assertFalse(s.flags()["has_tba"])
        self.assertIn("tba_arr", classes.section_state(self.by_crn["92874"]))

    def test_full_page_and_fragment_parse_same(self):
        # Feed the bare fragment and a wrapped page; both must find the table.
        frag = TIMETABLE_HTML.read_text(encoding="utf-8")
        wrapped = f"<html><body><div>{frag}</div></body></html>"
        p2 = classes.parse_timetable_html(wrapped, term="202609")
        self.assertEqual(len(p2.sections), 15)

    def test_empty_html_is_no_results_not_crash(self):
        p = classes.parse_timetable_html("<html><body>nothing</body></html>",
                                         term="202609")
        self.assertTrue(p.empty)
        self.assertEqual(p.sections, ())
        self.assertTrue(p.warnings)


class TestMultiMeeting(unittest.TestCase):
    def test_same_crn_two_rows_becomes_one_section_two_meetings(self):
        rows = (
            _row("12345", days="M W", begin="09:00AM", end="09:50AM",
                 loc="MCB 100")
            + _row("12345", days="T", begin="01:00PM", end="02:50PM",
                   loc="MCB 204")
            + '<tr><td COLSPAN="2">Comments for CRN 12345:</td>'
              '<td colspan="11">Extra lab required</td></tr>'
        )
        p = classes.parse_timetable_html(_table(rows), term="202609")
        self.assertEqual(len(p.sections), 1)
        s = p.sections[0]
        self.assertEqual(len(s.meetings), 2)
        self.assertEqual(s.meetings[0].days, ("M", "W"))
        self.assertEqual(s.meetings[1].days, ("T",))
        self.assertTrue(s.flags()["multiple_meetings"])
        self.assertIn("multiple_meetings", classes.section_state(s))
        self.assertIn("Extra lab required", s.comments)

    def test_conflicting_title_across_rows_warns(self):
        rows = _row("11111", title="First Title") + _row("11111", title="Other Title")
        p = classes.parse_timetable_html(_table(rows), term="202609")
        self.assertTrue(any("title differs" in w for w in p.warnings))
        self.assertEqual(p.sections[0].title, "First Title")


class TestParsingBasics(unittest.TestCase):
    def test_parse_days(self):
        self.assertEqual(classes.parse_days("T R"), ("T", "R"))
        self.assertEqual(classes.parse_days("MWF"), ("M", "W", "F"))
        self.assertEqual(classes.parse_days("R"), ("R",))
        self.assertEqual(classes.parse_days("(ARR)"), ())
        self.assertEqual(classes.parse_days(""), ())
        self.assertEqual(classes.days_to_text(("W", "M")), "MW")

    def test_parse_clock(self):
        self.assertEqual(classes.parse_clock("10:10AM"), "10:10")
        self.assertEqual(classes.parse_clock("12:00PM"), "12:00")
        self.assertEqual(classes.parse_clock("12:30AM"), "00:30")
        self.assertEqual(classes.parse_clock("2:30PM"), "14:30")
        self.assertEqual(classes.parse_clock("14:30"), "14:30")
        self.assertIsNone(classes.parse_clock("(ARR)"))
        self.assertIsNone(classes.parse_clock("-----"))
        self.assertIsNone(classes.parse_clock("nonsense"))

    def test_parse_course_label(self):
        self.assertEqual(classes.parse_course_label("AS-1115"), ("AS", "1115"))
        self.assertEqual(classes.parse_course_label("CS 2114"), ("CS", "2114"))
        self.assertEqual(classes.parse_course_label("MATH-1225H"), ("MATH", "1225H"))

    def test_parse_location_tba_and_online(self):
        self.assertTrue(classes.parse_location("(ARR) ----- (ARR)").is_tba)
        self.assertTrue(classes.parse_location("").is_tba)
        on = classes.parse_location("ONLINE")
        self.assertTrue(on.is_online)
        self.assertEqual(on.building, "ONLINE")

    def test_parse_location_uses_longest_crosswalk_code(self):
        crosswalk = {"ART": object(), "ART C": object()}
        m = classes.parse_location("ART C 101", crosswalk)
        self.assertEqual(m.building, "ART C")
        self.assertEqual(m.room, "101")

    def test_parse_crns(self):
        valid, invalid = classes.parse_crns("81476, 81477 12 abc 999999")
        self.assertEqual(valid, ["81476", "81477"])
        self.assertEqual(invalid, ["12", "abc", "999999"])
        self.assertFalse(classes.is_valid_crn("12"))
        self.assertTrue(classes.is_valid_crn("81476"))


# ===========================================================================
class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sections = classes.parse_timetable_html(
            TIMETABLE_HTML.read_text(encoding="utf-8"), term="202609").sections

    def test_subject_and_course(self):
        rows = classes.search(self.sections, subject="AS", course_number="1115")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(s.subject == "AS" for s in rows))

    def test_crn_and_days(self):
        rows = classes.search(self.sections, crn="81476")
        self.assertEqual([s.crn for s in rows], ["81476"])
        tr = classes.search(self.sections, days=("T", "R"), subject="AS")
        self.assertIn("81481", {s.crn for s in tr})

    def test_title_contains_case_insensitive(self):
        rows = classes.search(self.sections, title_contains="air force")
        self.assertEqual({s.crn for s in rows},
                         {"81476", "81477", "81478", "81481", "81482"})

    def test_no_match_is_empty(self):
        self.assertEqual(classes.search(self.sections, subject="ZZZZ"), [])

    def test_term_filter_isolates_identity(self):
        other = _section("81476", term="202612")
        rows = classes.search(list(self.sections) + [other], crn="81476",
                              term="202609")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].term, "202609")


class TestCRNSelectionAndSchedule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sections = list(classes.parse_timetable_html(
            TIMETABLE_HTML.read_text(encoding="utf-8"), term="202609").sections)

    def test_select_crns_reports_found_missing_invalid(self):
        res = classes.select_crns(self.sections, "81476, 00000, nope, 81476",
                                  term="202609")
        self.assertEqual([s.crn for s in res.found], ["81476"])
        self.assertEqual(res.missing, ("00000",))
        self.assertEqual(res.invalid, ("nope",))
        self.assertEqual(res.duplicates, ("81476",))

    def test_add_remove_schedule(self):
        first = classes.add_to_schedule([], self.sections, "81476, 81481",
                                        term="202609")
        self.assertEqual(len(first["schedule"]), 2)
        self.assertEqual({r["crn"] for r in first["added"]}, {"81476", "81481"})
        # Adding again is a no-op and reported, never duplicated.
        second = classes.add_to_schedule(first["schedule"], self.sections,
                                         "81476", term="202609")
        self.assertEqual(len(second["schedule"]), 2)
        self.assertEqual(second["added"], [])
        self.assertEqual(second["already_in_schedule"], ["81476"])
        removed = classes.remove_from_schedule(second["schedule"], "81476, 99999")
        self.assertEqual([r["crn"] for r in removed["schedule"]], ["81481"])
        self.assertEqual(removed["removed"], ["81476"])
        self.assertEqual(removed["not_in_schedule"], ["99999"])

    def test_schedule_record_is_json_ready_and_minimal(self):
        out = classes.add_to_schedule([], self.sections, "81476", term="202609")
        rec = out["schedule"][0]
        json.dumps(rec)
        self.assertEqual(set(rec) & {"pid", "gpa", "grades", "roster"}, set())
        self.assertEqual(rec["term"], "202609")
        self.assertEqual(rec["crn"], "81476")

    def test_resolve_schedule(self):
        out = classes.add_to_schedule([], self.sections, "81476, 81481")
        resolved = classes.resolve_schedule(out["schedule"], self.sections)
        self.assertEqual({s.crn for s in resolved}, {"81476", "81481"})


# ===========================================================================
class TestRecurrence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sections = classes.parse_timetable_html(
            TIMETABLE_HTML.read_text(encoding="utf-8"), term="202609").sections
        cls.by_crn = {s.crn: s for s in cls.sections}

    def test_weekly_mondays(self):
        s = self.by_crn["81478"]      # M 1:25-2:15
        occ = classes.expand_section(s, start=date(2026, 9, 21),
                                     end=date(2026, 10, 5), tz=TZ)
        self.assertEqual([o.date.isoformat() for o in occ],
                         ["2026-09-21", "2026-09-28", "2026-10-05"])
        self.assertTrue(all(o.start.tzinfo is not None for o in occ))
        self.assertEqual(occ[0].start.strftime("%H:%M"), "13:25")

    def test_holiday_skipped(self):
        s = self.by_crn["81478"]
        occ = classes.expand_section(s, start=date(2026, 11, 9),
                                     end=date(2026, 12, 7), tz=TZ)
        dates = [o.date.isoformat() for o in occ]
        self.assertIn("2026-11-16", dates)
        self.assertNotIn("2026-11-23", dates, "Thanksgiving Monday must be skipped")
        self.assertIn("2026-11-30", dates)

    def test_tba_meeting_expands_to_nothing(self):
        s = self.by_crn["92874"]
        occ = classes.expand_section(s, start=date(2026, 9, 1),
                                     end=date(2026, 12, 1), tz=TZ)
        self.assertEqual(occ, [])

    def test_two_days_expand_to_two_per_week(self):
        s = self.by_crn["81481"]      # T R 8:00
        occ = classes.expand_section(s, start=date(2026, 9, 15),
                                     end=date(2026, 9, 22), tz=TZ)
        self.assertEqual([o.date.isoformat() for o in occ],
                         ["2026-09-15", "2026-09-17", "2026-09-22"])


class TestConflicts(unittest.TestCase):
    def test_overlap_detected(self):
        a = _section("10001", days=("M",), begin="10:00", end="11:00", building="A")
        b = _section("10002", days=("M",), begin="10:30", end="11:30", building="B")
        conflicts = classes.schedule_conflicts(
            [a, b], start=date(2026, 9, 14), end=date(2026, 9, 14))
        self.assertEqual(len(conflicts), 1)
        self.assertAlmostEqual(conflicts[0].overlap_min, 30.0)
        self.assertEqual(
            {conflicts[0].a.crn, conflicts[0].b.crn}, {"10001", "10002"})

    def test_adjacent_classes_do_not_conflict(self):
        a = _section("10001", days=("M",), begin="10:00", end="11:00")
        b = _section("10002", days=("M",), begin="11:00", end="12:00")
        self.assertEqual(
            classes.schedule_conflicts([a, b], start=date(2026, 9, 14),
                                       end=date(2026, 9, 14)), [])

    def test_same_crn_meetings_not_conflicts_by_default(self):
        m1 = classes.Meeting(days=("M",), begin="10:00", end="11:00",
                             building="A", room="1")
        m2 = classes.Meeting(days=("M",), begin="10:30", end="11:30",
                             building="A", room="2")
        s = classes.ClassSection(term="202609", crn="10001", subject="CS",
                                 course_number="2114", title="x",
                                 meetings=(m1, m2))
        self.assertEqual(classes.schedule_conflicts([s], start=date(2026, 9, 14),
                                                    end=date(2026, 9, 14)), [])
        self.assertEqual(len(classes.schedule_conflicts(
            [s], start=date(2026, 9, 14), end=date(2026, 9, 14))), 0)


class TestNextClass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sections = classes.parse_timetable_html(
            TIMETABLE_HTML.read_text(encoding="utf-8"), term="202609",
            crosswalk=classes.building_crosswalk(classes.load_buildings())
        ).sections

    def test_deadline_is_start_minus_buffer(self):
        nc = classes.next_class(
            self.sections, datetime(2026, 9, 21, 12, 0, tzinfo=TZ),
            start=date(2026, 9, 21), end=date(2026, 12, 9), tz=TZ,
            buffer_min=10)
        self.assertIsNotNone(nc)
        self.assertEqual(nc.occurrence.crn, "81478")
        self.assertEqual(nc.leave_by.strftime("%H:%M"), "13:15")
        self.assertEqual(nc.occurrence.start.strftime("%H:%M"), "13:25")
        self.assertEqual(nc.occurrence.meeting.building, "CLMS")
        self.assertEqual(nc.minutes_until, 85.0)

    def test_json_contract(self):
        out = classes.next_class_json(
            self.sections, datetime(2026, 9, 21, 12, 0, tzinfo=TZ),
            start=date(2026, 9, 21), end=date(2026, 12, 9), tz=TZ)
        self.assertEqual(out["schema"], classes.SCHEMA_NEXT_CLASS)
        self.assertEqual(out["status"], "scheduled")
        self.assertEqual(out["building"], "CLMS")
        self.assertIn("deadline", out)

    def test_no_class_after_term(self):
        out = classes.next_class_json(
            self.sections, datetime(2027, 1, 1, 9, 0, tzinfo=TZ),
            start=date(2026, 9, 1), end=date(2026, 12, 9), tz=TZ)
        self.assertEqual(out["status"], "none")
        self.assertIsNone(out["deadline"])

    def test_skip_online(self):
        online = _section("90001", days=("M",), begin="09:00", end="10:00",
                          building="ONLINE", room=None, is_online=True)
        physical = _section("90002", days=("M",), begin="11:00", end="12:00",
                            building="MCB", room="100")
        at = datetime(2026, 9, 14, 8, 0, tzinfo=TZ)
        picked = classes.next_class([online, physical], at, start=date(2026, 9, 14),
                                    end=date(2026, 9, 14), tz=TZ)
        self.assertEqual(picked.occurrence.crn, "90001")
        skipped = classes.next_class([online, physical], at,
                                     start=date(2026, 9, 14),
                                     end=date(2026, 9, 14), tz=TZ,
                                     skip_online=True)
        self.assertEqual(skipped.occurrence.crn, "90002")


# ===========================================================================
class TestICS(unittest.TestCase):
    def test_sample_calendar(self):
        text = ICS_SAMPLE.read_text(encoding="utf-8")
        p = classes.parse_ics(text)
        self.assertTrue(p.valid, p.errors)
        self.assertEqual(len(p.events), 3)
        by_uid = {e.uid: e for e in p.events}
        lec = by_uid["cs-2114-lecture-01@hokieday.example"]
        self.assertEqual(lec.days, ("M", "W", "F"))
        self.assertEqual(lec.tzid, "America/New_York")
        self.assertEqual(lec.dtstart.strftime("%H:%M"), "10:00")
        self.assertTrue(lec.recurring)

    def test_utc_event_converted_to_campus_tz(self):
        p = classes.parse_ics(ICS_SAMPLE.read_text(encoding="utf-8"))
        ev = next(e for e in p.events if e.uid.startswith("math-1225"))
        # 14:00Z on Sep 2 2026 == 10:00 America/New_York
        self.assertEqual(ev.dtstart.strftime("%H:%M"), "10:00")
        self.assertEqual(ev.dtstart.utcoffset().total_seconds(), -4 * 3600)
        self.assertFalse(ev.recurring)

    def test_floating_time_warns_and_assumes_campus(self):
        p = classes.parse_ics(ICS_SAMPLE.read_text(encoding="utf-8"))
        ev = next(e for e in p.events if e.uid.startswith("open-house"))
        self.assertTrue(any("floating" in w for w in ev.warnings))

    def test_recurrence_expansion(self):
        ev = next(e for e in classes.parse_ics(
            ICS_SAMPLE.read_text(encoding="utf-8")).events
            if e.uid.startswith("cs-2114"))
        occ = classes.expand_ics_event(ev, start=date(2026, 8, 24),
                                       end=date(2026, 9, 4))
        self.assertEqual([o.date.isoformat() for o in occ],
                         ["2026-08-24", "2026-08-26", "2026-08-28",
                          "2026-08-31", "2026-09-02", "2026-09-04"])

    def test_recurrence_count_semantics(self):
        text = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:c1\nSUMMARY:C\n"
                "DTSTART;TZID=America/New_York:20260824T100000\n"
                "DTEND;TZID=America/New_York:20260824T110000\n"
                "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=3\n"
                "END:VEVENT\nEND:VCALENDAR\n")
        ev = classes.parse_ics(text).events[0]
        all_occ = classes.expand_ics_event(ev, start=date(2026, 8, 24),
                                           end=date(2026, 12, 1))
        self.assertEqual([o.date.isoformat() for o in all_occ],
                         ["2026-08-24", "2026-08-31", "2026-09-07"])
        # COUNT is a property of the series, not the query: a later window
        # still leaves only the two remaining occurrences.
        later = classes.expand_ics_event(ev, start=date(2026, 8, 31),
                                         end=date(2026, 12, 1))
        self.assertEqual([o.date.isoformat() for o in later],
                         ["2026-08-31", "2026-09-07"])

    def test_recurrence_interval(self):
        text = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:i1\nSUMMARY:I\n"
                "DTSTART;TZID=America/New_York:20260824T090000\n"
                "DTEND;TZID=America/New_York:20260824T100000\n"
                "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;COUNT=3\n"
                "END:VEVENT\nEND:VCALENDAR\n")
        ev = classes.parse_ics(text).events[0]
        occ = classes.expand_ics_event(ev, start=date(2026, 8, 24),
                                       end=date(2026, 12, 1))
        self.assertEqual([o.date.isoformat() for o in occ],
                         ["2026-08-24", "2026-09-07", "2026-09-21"])

    def test_non_recurring_single_occurrence(self):
        ev = next(e for e in classes.parse_ics(
            ICS_SAMPLE.read_text(encoding="utf-8")).events
            if e.uid.startswith("math-1225"))
        occ = classes.expand_ics_event(ev, start=date(2026, 8, 24),
                                       end=date(2026, 12, 1))
        self.assertEqual(len(occ), 1)

    def test_line_unfolding(self):
        folded = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u1\r\n"
                  "SUMMARY:Long\r\n  Summary\r\n"
                  "DTSTART;TZID=America/New_York:20260824T100000\r\n"
                  "DTEND;TZID=America/New_York:20260824T110000\r\n"
                  "END:VEVENT\r\nEND:VCALENDAR\r\n")
        lines = classes.unfold_ics(folded)
        self.assertIn("SUMMARY:Long Summary", lines)

    def test_malformed_dtstart_flagged_not_guessed(self):
        bad = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\nDTSTART:garbage\n"
               "END:VEVENT\nEND:VCALENDAR\n")
        p = classes.parse_ics(bad)
        self.assertFalse(p.valid)
        self.assertEqual(p.events, ())
        self.assertTrue(any("malformed" in e.lower() for e in p.errors))

    def test_unsupported_freq_rejected(self):
        bad = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:y\n"
               "DTSTART;TZID=America/New_York:20260824T100000\n"
               "DTEND;TZID=America/New_York:20260824T110000\n"
               "RRULE:FREQ=MONTHLY\nEND:VEVENT\nEND:VCALENDAR\n")
        p = classes.parse_ics(bad)
        self.assertFalse(p.valid)
        self.assertEqual(p.events, ())
        self.assertTrue(any("unsupported" in e.lower() for e in p.errors))

    def test_missing_uid_rejected(self):
        bad = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
               "DTSTART;TZID=America/New_York:20260824T100000\n"
               "DTEND;TZID=America/New_York:20260824T110000\n"
               "END:VEVENT\nEND:VCALENDAR\n")
        p = classes.parse_ics(bad)
        self.assertEqual(p.events, ())
        self.assertTrue(any("UID" in e for e in p.errors))

    def test_not_a_calendar(self):
        p = classes.parse_ics("hello world")
        self.assertFalse(p.valid)
        self.assertTrue(p.errors)

    def test_ics_event_to_section_flags_non_crn_identity(self):
        ev = classes.parse_ics(ICS_SAMPLE.read_text(encoding="utf-8")).events[0]
        sec = ev.to_section()
        self.assertEqual(sec.source, "ics")
        self.assertEqual(sec.crn, ev.uid)
        self.assertTrue(any("not a VT CRN" in w for w in sec.warnings))


# ===========================================================================
class TestBuildingCrosswalk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.buildings = classes.parse_building_list_html(
            BUILDINGS_HTML.read_text(encoding="utf-8"))
        cls.crosswalk = classes.building_crosswalk(cls.buildings)

    def test_parsed_codes(self):
        self.assertEqual(len(self.buildings), 295)
        codes = {b.code for b in self.buildings}
        self.assertIn("CLMS", codes)
        self.assertIn("MCB", codes)
        self.assertIn("AJ E", codes)

    def test_no_invented_coordinates(self):
        self.assertTrue(all(b.lat is None and b.lon is None
                            for b in self.buildings))
        contract = classes.crosswalk_contract(self.crosswalk)
        self.assertFalse(contract["coords_present"])
        self.assertEqual(contract["schema"],
                         "hokieday.classes.building_crosswalk/1")

    def test_unknown_building_resolved_by_crosswalk(self):
        html = TIMETABLE_HTML.read_text(encoding="utf-8")
        no_cw = classes.parse_timetable_html(html, term="202609")
        with_cw = classes.parse_timetable_html(html, term="202609",
                                               crosswalk=self.crosswalk)
        self.assertTrue(any(s.has_unknown_building for s in no_cw.sections))
        self.assertFalse(any(s.has_unknown_building for s in with_cw.sections))
        sec = next(s for s in with_cw.sections if s.crn == "81476")
        self.assertEqual(sec.building_names["CLMS"],
                         "Corps Ldrship & Military Sci")

    def test_attach_gis_coords_validates(self):
        cw = classes.building_crosswalk([classes.Building(code="CLMS",
                                                          name="Corps")])
        joined = classes.attach_gis_coords(cw, {"CLMS": (37.2, -80.4)})
        self.assertTrue(joined["CLMS"].has_coords)
        self.assertTrue(joined["CLMS"].gis_verified)
        with self.assertRaises(ValueError):
            classes.attach_gis_coords(cw, {"CLMS": (999.0, -80.4)})

    def test_exam_grid_parser_conservative(self):
        html = ("<table class='plaintable'><tr><td>Friday, Dec 11</td>"
                "<td>7:45AM to 9:45AM</td></tr>"
                "<tr><td>12M</td><td>08M</td></tr></table>")
        slots = classes.parse_exam_schedule_html(html)
        self.assertTrue(any(s.code == "12M" for s in slots))


# ===========================================================================
class TestSnapshot(unittest.TestCase):
    def test_load_generated_snapshot(self):
        snap = classes.load_snapshot(SNAPSHOT_JSON)
        self.assertEqual(snap.term, "202609")
        self.assertEqual(snap.query.get("subject"), "AS")
        self.assertEqual(snap.fetched_at.tzinfo is not None, True)
        parse = classes.snapshot_sections(
            snap, classes.building_crosswalk(classes.load_buildings()))
        self.assertEqual(len(parse.sections), 15)
        self.assertEqual(parse.snapshot_id, snap.snapshot_id)

    def test_staleness(self):
        snap = classes.load_snapshot(SNAPSHOT_JSON)
        fresh = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
        old = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
        self.assertFalse(classes.snapshot_is_stale(snap, now=fresh))
        self.assertTrue(classes.snapshot_is_stale(snap, now=old))

    def test_search_result_json_states(self):
        snap = classes.load_snapshot(SNAPSHOT_JSON)
        parse = classes.snapshot_sections(snap)
        ok = classes.search_result_json(
            parse, snap, query={"subject": "AS"},
            now=datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(ok["state"], "results")
        self.assertEqual(ok["count"], 15)
        self.assertEqual(ok["schema"], classes.SCHEMA_SEARCH)
        stale = classes.search_result_json(
            parse, snap, now=datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(stale["state"], "stale_snapshot")
        self.assertTrue(stale["snapshot"]["is_stale"])

    def test_empty_parse_is_no_results(self):
        snap = classes.load_snapshot(SNAPSHOT_JSON)
        empty = classes.parse_timetable_html("<html></html>", term="202609")
        out = classes.search_result_json(empty, snap)
        self.assertEqual(out["state"], "no_results")
        self.assertEqual(out["count"], 0)

    def test_sanitize_query_drops_pii_and_unknown_keys(self):
        q = classes.sanitize_query({
            "subject": "AS", "term": "202609", "inst_name": "Smith",
            "password": "x", "pid": "123", "whatever": "y",
        })
        self.assertEqual(q, {"subject": "AS", "term": "202609"})
        for forbidden in ("inst_name", "password", "pid"):
            self.assertNotIn(forbidden, q)

    def test_schedule_json_conflict_state(self):
        a = _section("10001", days=("M",), begin="10:00", end="11:00")
        b = _section("10002", days=("M",), begin="10:30", end="11:30")
        sched = classes.add_to_schedule([], [a, b], "10001, 10002")["schedule"]
        out = classes.schedule_json(sched, [a, b], start=date(2026, 9, 14),
                                    end=date(2026, 9, 14))
        self.assertEqual(out["state"], "conflict")
        self.assertEqual(len(out["conflicts"]), 1)
        self.assertEqual(out["count"], 2)


# ===========================================================================
class TestPrivacyAndBoundary(unittest.TestCase):
    def test_module_never_imports_network_libraries(self):
        src = (Path(classes.__file__)).read_text(encoding="utf-8")
        tree = ast.parse(src)
        banned = {"urllib", "requests", "socket", "httpx", "aiohttp",
                  "http.client", "urllib3"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned,
                                     f"banned import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertNotIn(mod.split(".")[0], banned if "." not in mod
                                 else banned, f"banned import from {mod}")

    def test_snapshot_envelope_has_no_forbidden_keys(self):
        snap = json.loads(SNAPSHOT_JSON.read_text(encoding="utf-8"))
        serialized = json.dumps(snap).lower()
        for bad in ("password", "pid", '"gpa"', "roster", "bannerid"):
            self.assertNotIn(bad, serialized)

    def test_section_dict_roundtrips_json(self):
        parse = classes.parse_timetable_html(
            TIMETABLE_HTML.read_text(encoding="utf-8"), term="202609")
        blob = json.dumps([s.to_dict() for s in parse.sections])
        restored = json.loads(blob)
        self.assertEqual(restored[0]["term"], "202609")


class TestEdgeScript(unittest.TestCase):
    """The polite POST form is the one place urllib is allowed; verify its
    fields and offline snapshot writing without opening a socket."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent.parent / "scripts" / "fetch_classes.py"
        spec = importlib.util.spec_from_file_location("fetch_classes", path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_required_form_fields(self):
        data = self.mod.form_data(term="202609", subject="AS", crn="81476")
        self.assertEqual(data["TERMYEAR"], "202609")
        self.assertEqual(data["subj_code"], "AS")
        self.assertEqual(data["crn"], "81476")
        # CORE_CODE is REQUIRED: omitting it makes Banner re-render the form.
        self.assertEqual(data["CORE_CODE"], "AR%")
        self.assertEqual(data["BTN_PRESSED"], "FIND class sections")
        self.assertIn("inst_name", data)  # empty, never a real instructor

    def test_subject_code_parser(self):
        html = ('<script>subj_code.options[1]=new Option("AAD - Architecture","AAD");'
                'subj_code.options[2]=new Option("AS - Aerospace","AS");</script>')
        subs = self.mod.parse_subject_codes(html)
        self.assertEqual(subs, [("AAD", "AAD - Architecture"),
                                ("AS", "AS - Aerospace")])

    def test_snapshot_written_offline_and_reloadable(self):
        html = TIMETABLE_HTML.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            path = self.mod.write_timetable_snapshot(
                Path(tmp), html, term="202609", subject="AS",
                name="unit_test")
            self.assertTrue(path.exists())
            snap = classes.load_snapshot(path)
            self.assertEqual(snap.term, "202609")
            self.assertEqual(len(classes.snapshot_sections(snap).sections), 15)
            # The persisted query must not carry the instructor field.
            self.assertNotIn("inst_name", snap.query)


if __name__ == "__main__":
    unittest.main()