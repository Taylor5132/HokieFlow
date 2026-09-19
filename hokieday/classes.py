"""Public Virginia Tech class/timetable integration (Banner self-service).

OWNER: worker classes-public. Contract: docs/CLASSES.md (frozen by the parent
after this branch is reviewed). Facts from the public Banner timetable only.

WHY THIS MODULE EXISTS
----------------------
HokieFlow's planning deadline used to be a string ("1:25") parsed out of the
student's own sentence. This module turns that into a REAL, deterministic
deadline: the next class meeting's start time minus a boarding/walk buffer, and
the building it is in. It is intentionally a *data* module -- no LLM, no UI.

NON-NEGOTIABLE BOUNDARIES (see docs/CLASSES.md)
-----------------------------------------------
* PUBLIC pages only: HZSKVTSC (timetable), HZSKVTS P_DispBldgList (building
  abbreviations), HZSKEXAM (final-exam schedule). NEVER HokieSPA / My VT,
  never a login, never a CRN owner's grades/GPA/roster/PID.
* This module NEVER talks to the network. The polite form POST lives in the
  edge script ``scripts/fetch_classes.py``, which writes a JSON *snapshot* to
  disk. Parse functions here take an HTML string or a snapshot path, so the
  whole library is importable and testable offline. There is no ``urllib``
  import in this file (tests enforce it).
* Nothing is guessed. A TBA/ARR meeting, an unknown building code, an
  unsupported RRULE, or a malformed ICS line is FLAGGED, not filled in.

SAMPLE API-READY JSON (see docs/CLASSES.md for the full contract)
----------------------------------------------------------------
Search result (``search_result_json``)::

    {
      "schema": "hokieday.classes.search/1",
      "term": "202609", "term_name": "Fall 2026",
      "query": {"subject": "AS", "course_number": "1115"},
      "snapshot": {"id": "...", "fetched_at": "...", "is_stale": false},
      "count": 3,
      "state": "results",          # results | no_results | stale_snapshot
      "sections": [ {...ClassSection...} ]
    }

ClassSection::

    {
      "term": "202609", "crn": "81476",
      "subject": "AS", "course_number": "1115", "course": "AS-1115",
      "title": "Introduction to the Air Force",
      "schedule_type": "L", "modality": "Face-to-Face Instruction",
      "credit_hours": "1", "capacity": "60", "instructor": "N/A",
      "meetings": [
        {"days": ["W"], "begin": "10:10", "end": "11:00",
         "location_raw": "CLMS 270", "building": "CLMS", "room": "270",
         "is_tba": false, "is_online": false}
      ],
      "comments": ["..."], "exam_code": "00X",
      "building_names": {"CLMS": "..."},
      "flags": {"has_tba": false, "has_unknown_building": false,
                "multiple_meetings": false, "is_online": false},
      "warnings": [], "snapshot_id": "...", "source": "banner_public"
    }

Next-class deadline (``next_class_json``)::

    {
      "schema": "hokieday.classes.next_class/1",
      "status": "scheduled",       # scheduled | none
      "at": "2026-09-21T10:00:00-04:00",
      "deadline": "2026-09-21T10:00:00-04:00",  # class start - buffer
      "class_start": "2026-09-21T10:10:00-04:00",
      "buffer_min": 10.0, "minutes_until": 10.0,
      "crn": "81476", "course": "AS-1115",
      "building": "CLMS", "building_name": "Norris Hall? (crosswalk)",
      "room": "270", "is_online": false
    }
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config

# ---------------------------------------------------------------------------
# Source identity. Kept here (not in config.py) so this branch stays disjoint
# from the parent-owned config during parallel work; the parent may move these
# into config.ENDPOINTS during integration.
# ---------------------------------------------------------------------------
BANNER_BASE = "https://selfservice.banner.vt.edu/ssb/"
BANNER_FORM_URL = BANNER_BASE + "HZSKVTSC.P_DispRequest"
BANNER_PROC_URL = BANNER_BASE + "HZSKVTSC.P_ProcRequest"
BANNER_BUILDINGS_URL = BANNER_BASE + "hzskvtsc.P_DispBldgList"
BANNER_EXAMS_URL = BANNER_BASE + "hzskexam.P_DispExamInfo"

# Default campus = Blacksburg. Banner campus codes are strings.
DEFAULT_CAMPUS = "0"
# "Search Pathways Concept" is the default CORE_CODE value and is REQUIRED by
# the form: omitting it returns the HTML form back instead of results (verified
# 2026-09-19 against the live endpoint). Send it verbatim, including the "%".
DEFAULT_CORE_CODE = "AR%"
DEFAULT_SCHDTYPE = "%"
DEFAULT_SESS_CODE = "%"

SCHEMA_SNAPSHOT = "hokieday.classes.snapshot/1"
SCHEMA_SEARCH = "hokieday.classes.search/1"
SCHEMA_NEXT_CLASS = "hokieday.classes.next_class/1"
SCHEMA_SCHEDULE = "hokieday.classes.schedule/1"

TERM_FALL_2026 = "202609"
SUPPORTED_TERMS: dict[str, str] = {
    "202609": "Fall 2026",
    "202612": "Winter 26-27",
}


def term_name(term: str) -> str:
    return SUPPORTED_TERMS.get(str(term), str(term))


# ---------------------------------------------------------------------------
# Academic-calendar windows. VERIFIED against the VT Registrar 2026-2027
# calendar (https://www.registrar.vt.edu/dates-deadlines/academic-calendar/
# 2026-2027.html, read 2026-09-19). Recurrence uses these to bound expansion;
# it never invents dates. Non-instruction days are skipped (see HOLIDAYS).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TermWindow:
    term: str
    name: str
    classes_begin: date
    classes_end: date
    source: str
    verified: bool


TERM_WINDOWS: dict[str, TermWindow] = {
    "202609": TermWindow(
        term="202609", name="Fall 2026",
        classes_begin=date(2026, 8, 24),
        classes_end=date(2026, 12, 9),
        source=("VT Registrar academic calendar 2026-2027: Classes Begin "
                "Aug 24 2026, Classes End Dec 9 2026"),
        verified=True,
    ),
}

# Dates with no classes. Kept explicit so a weekly expansion cannot place a
# class on a university holiday. Thanksgiving break Nov 21-29 2026.
HOLIDAYS: dict[str, tuple[date, ...]] = {
    "202609": (
        date(2026, 9, 7),    # Labor Day
        date(2026, 10, 9),   # Fall Break
        *tuple(date(2026, 11, d) for d in range(21, 30)),  # Thanksgiving break
    ),
}


def term_window(term: str) -> TermWindow | None:
    return TERM_WINDOWS.get(str(term))


def _holiday_set(term: str) -> frozenset[date]:
    return frozenset(HOLIDAYS.get(str(term), ()))


# ---------------------------------------------------------------------------
# Days
# ---------------------------------------------------------------------------
# Banner uses the classic M/T/W/R/F/S/U day letters (R = Thursday).
DAY_TO_WEEKDAY: dict[str, int] = {
    "M": 0, "T": 1, "W": 2, "R": 3, "F": 4, "S": 5, "U": 6,
}
WEEKDAY_TO_DAY = {v: k for k, v in DAY_TO_WEEKDAY.items()}
ICS_DAY_TO_BANNER = {
    "MO": "M", "TU": "T", "WE": "W", "TH": "R", "FR": "F", "SA": "S", "SU": "U",
}
BANNER_DAY_TO_ICS = {v: k for k, v in ICS_DAY_TO_BANNER.items()}

_ARR_RE = re.compile(r"^\s*$|^\(?arr\)?$|^-+$|^tba$|^tba\s+tba$", re.I)


def parse_days(text: str | None) -> tuple[str, ...]:
    """'T R' / 'MWF' / 'W' / '(ARR)' -> ('T','R') / ('M','W','F') / () .

    Only known day letters are accepted; anything else is dropped (the caller
    can compare lengths / inspect the raw text to notice).
    """
    if not text:
        return ()
    raw = str(text).upper()
    if "ARR" in raw or "TBA" in raw:
        return ()
    out: list[str] = []
    for tok in re.split(r"[\s,;/]+", raw):
        if not tok:
            continue
        if len(tok) == 1:
            if tok in DAY_TO_WEEKDAY and tok not in out:
                out.append(tok)
        else:
            # A run like "MWF" or "TR".
            for ch in tok:
                if ch in DAY_TO_WEEKDAY and ch not in out:
                    out.append(ch)
    # Return in Monday-first order for reproducibility.
    return tuple(sorted(out, key=lambda d: DAY_TO_WEEKDAY[d]))


def days_to_text(days: Iterable[str]) -> str:
    return "".join(d for d in ("M", "T", "W", "R", "F", "S", "U") if d in set(days))


def parse_clock(text: str | None) -> str | None:
    """'10:10AM' -> '10:10' (24h, zero-padded). TBA/garbage -> None.

    Never guesses: a value that does not parse is None and the raw text stays
    on the meeting so a caller can show it.
    """
    if text is None:
        return None
    raw = str(text).strip().upper()
    if not raw or "ARR" in raw or raw.startswith("-") or raw == "TBA":
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})\s*([AP])\.?M\.?$", raw)
    if not m:
        m24 = re.match(r"^(\d{1,2}):(\d{2})$", raw)
        if m24:
            hh, mm = int(m24.group(1)), int(m24.group(2))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return f"{hh:02d}:{mm:02d}"
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if not (1 <= hh <= 12) or not (0 <= mm <= 59):
        return None
    if ap == "A":
        hh = 0 if hh == 12 else hh
    else:
        hh = 12 if hh == 12 else hh + 12
    return f"{hh:02d}:{mm:02d}"


def _to_minutes(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    try:
        hh, mm = (int(p) for p in hhmm.split(":"))
    except ValueError:
        return None
    return hh * 60 + mm


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Meeting:
    """One meeting pattern on a section (a Banner result row's Days/Begin/End/
    Location). A section with two rows becomes two Meetings."""

    days: tuple[str, ...] = ()
    begin: str | None = None          # "HH:MM" 24h campus-local, or None
    end: str | None = None
    location_raw: str = ""
    building: str | None = None
    room: str | None = None
    is_tba: bool = False
    is_online: bool = False

    @property
    def has_time(self) -> bool:
        return bool(self.days and self.begin and self.end)

    def to_dict(self) -> dict:
        return {
            "days": list(self.days),
            "begin": self.begin,
            "end": self.end,
            "location_raw": self.location_raw,
            "building": self.building,
            "room": self.room,
            "is_tba": self.is_tba,
            "is_online": self.is_online,
        }


@dataclass(frozen=True)
class ClassSection:
    term: str
    crn: str
    subject: str
    course_number: str
    title: str
    schedule_type: str = ""
    modality: str = ""
    credit_hours: str = ""
    capacity: str = ""
    instructor: str = ""
    campus: str = DEFAULT_CAMPUS
    meetings: tuple[Meeting, ...] = ()
    comments: tuple[str, ...] = ()
    exam_code: str = ""
    building_names: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    snapshot_id: str = ""
    source: str = "banner_public"

    @property
    def course(self) -> str:
        if self.subject and self.course_number:
            return f"{self.subject}-{self.course_number}"
        return self.subject or self.course_number

    @property
    def has_tba(self) -> bool:
        return any(m.is_tba or not m.has_time for m in self.meetings)

    @property
    def has_unknown_building(self) -> bool:
        for m in self.meetings:
            if m.is_tba or m.is_online or not m.building:
                continue
            if not self.building_names.get(m.building):
                return True
        return False

    @property
    def is_online(self) -> bool:
        return bool(self.meetings) and all(m.is_online for m in self.meetings)

    def flags(self) -> dict:
        return {
            "has_tba": self.has_tba,
            "has_unknown_building": self.has_unknown_building,
            "multiple_meetings": len(self.meetings) > 1,
            "is_online": self.is_online,
        }

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "term_name": term_name(self.term),
            "crn": self.crn,
            "subject": self.subject,
            "course_number": self.course_number,
            "course": self.course,
            "title": self.title,
            "schedule_type": self.schedule_type,
            "modality": self.modality,
            "credit_hours": self.credit_hours,
            "capacity": self.capacity,
            "instructor": self.instructor,
            "campus": self.campus,
            "meetings": [m.to_dict() for m in self.meetings],
            "comments": list(self.comments),
            "exam_code": self.exam_code,
            "building_names": dict(self.building_names),
            "flags": self.flags(),
            "warnings": list(self.warnings),
            "snapshot_id": self.snapshot_id,
            "source": self.source,
        }


@dataclass(frozen=True)
class ClassOccurrence:
    """A concrete dated instance of a Meeting (recurrence expanded in code)."""

    term: str
    crn: str
    subject: str
    course_number: str
    title: str
    date: date
    start: datetime                  # campus-local, tz-aware
    end: datetime                    # campus-local, tz-aware
    meeting: Meeting
    title_extra: str = ""

    @property
    def course(self) -> str:
        if self.subject and self.course_number:
            return f"{self.subject}-{self.course_number}"
        return self.subject or self.course_number

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "crn": self.crn,
            "course": self.course,
            "title": self.title,
            "date": self.date.isoformat(),
            "start": self.start.isoformat(timespec="seconds"),
            "end": self.end.isoformat(timespec="seconds"),
            "days": list(self.meeting.days),
            "building": self.meeting.building,
            "room": self.meeting.room,
            "location_raw": self.meeting.location_raw,
            "is_online": self.meeting.is_online,
        }


@dataclass(frozen=True)
class TimetableParse:
    term: str
    sections: tuple[ClassSection, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    empty: bool = False
    snapshot_id: str = ""


@dataclass(frozen=True)
class Conflict:
    a: ClassOccurrence
    b: ClassOccurrence
    overlap_min: float

    def to_dict(self) -> dict:
        return {
            "overlap_min": round(self.overlap_min, 1),
            "a": self.a.to_dict(),
            "b": self.b.to_dict(),
        }


@dataclass(frozen=True)
class NextClass:
    occurrence: ClassOccurrence
    minutes_until: float
    leave_by: datetime
    buffer_min: float

    @property
    def status(self) -> str:
        return "online" if self.occurrence.meeting.is_online else "scheduled"

    def to_dict(self) -> dict:
        o = self.occurrence
        return {
            "status": self.status,
            "crn": o.crn,
            "course": o.course,
            "title": o.title,
            "class_start": o.start.isoformat(timespec="seconds"),
            "class_end": o.end.isoformat(timespec="seconds"),
            "deadline": self.leave_by.isoformat(timespec="seconds"),
            "buffer_min": self.buffer_min,
            "minutes_until": round(self.minutes_until, 1),
            "building": o.meeting.building,
            "room": o.meeting.room,
            "location_raw": o.meeting.location_raw,
            "is_online": o.meeting.is_online,
        }


# ---------------------------------------------------------------------------
# HTML table extraction (stdlib only; tolerant of Banner's unclosed <td>/<tr>)
# ---------------------------------------------------------------------------
class _TableRowParser(HTMLParser):
    """Collect rows of cell text from the FIRST table matching a predicate.

    Banner emits invalid HTML (unclosed ``<td>`` and ``<tr>``, attributes with
    no separating space). A streaming parser is used instead of a DOM because
    stdlib has no DOM; ``HTMLParser`` already recovers from those mistakes.
    """

    def __init__(self, predicate: Callable[[dict], bool]):
        super().__init__(convert_charrefs=True)
        self._predicate = predicate
        self.rows: list[list[str]] = []
        self.found = False
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._colspan = 1
        self._buf: list[str] = []

    # -- helpers
    def _flush_cell(self) -> None:
        # No-op unless a <td>/<th> was actually opened: Banner leaves <td>
        # tags unclosed, so the next <td>/<tr> must not emit a phantom empty
        # cell for the one already flushed at </td>.
        if self._cell is None:
            self._buf = []
            return
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if self._row is not None:
            self._row.append(text)
            # Honor COLSPAN so column positions stay aligned. Banner merges
            # Begin+End into one colspan=2 cell on ONLINE/TBA rows; without
            # this the Location/Exam columns would shift left by one and the
            # exam code would be mistaken for a building.
            for _ in range(max(1, self._colspan) - 1):
                self._row.append("")
        self._cell = None
        self._colspan = 1
        self._buf = []

    def _flush_row(self) -> None:
        if self._row is not None and any(c for c in self._row):
            self.rows.append(self._row)
        self._row = None

    # -- HTMLParser hooks
    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "table":
            if not self.found:
                if self._predicate(a):
                    self.found = True
                    self._depth = 1
                return
            self._depth += 1
            return
        if not self.found:
            return
        if tag == "tr":
            self._flush_cell()
            self._flush_row()
            self._row = []
        elif tag in ("td", "th"):
            self._flush_cell()
            self._cell = []
            try:
                self._colspan = max(1, int(a.get("colspan", "1") or 1))
            except ValueError:
                self._colspan = 1
        elif tag in ("br", "p", "li", "div"):
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if not self.found:
            return
        if tag in ("td", "th"):
            self._flush_cell()
        elif tag == "tr":
            self._flush_cell()
            self._flush_row()
        elif tag == "table":
            self._depth -= 1
            if self._depth <= 0:
                self._flush_cell()
                self._flush_row()
                self.found = False

    def handle_data(self, data: str) -> None:
        if self.found and data:
            self._buf.append(data)

    def close(self) -> None:          # noqa: D102 - inherited
        super().close()
        self._flush_cell()
        self._flush_row()


def extract_table_rows(html: str, predicate: Callable[[dict], bool]) -> list[list[str]]:
    p = _TableRowParser(predicate)
    p.feed(html or "")
    p.close()
    return p.rows


# ---------------------------------------------------------------------------
# Location / course parsing
# ---------------------------------------------------------------------------
def _looks_tba(text: str) -> bool:
    raw = (text or "").strip()
    upper = raw.upper()
    if "ARR" in upper or upper in ("TBA", "TBA TBA"):
        return True
    stripped = re.sub(r"[()\-\s]", "", upper)
    return stripped == ""


def parse_location(text: str, crosswalk: dict[str, Any] | None = None) -> Meeting:
    """'CLMS 270' -> Meeting(building=CLMS, room=270).

    Multi-word building codes exist ('ART C', 'AJ E', 'FITZ 1'). When a
    crosswalk is supplied the LONGEST known code prefix wins; otherwise the
    first whitespace token is the code. Unknown is left as-is, never guessed.
    """
    raw = (text or "").strip()
    if _looks_tba(raw):
        return Meeting(location_raw=raw, is_tba=True)
    upper = raw.upper()
    if "ONLINE" in upper:
        return Meeting(location_raw=raw, building="ONLINE", is_online=True)

    code: str | None = None
    room: str | None = None
    if crosswalk:
        candidates = sorted(crosswalk, key=len, reverse=True)
        for cand in candidates:
            if upper == cand or upper.startswith(cand + " "):
                code = cand
                room = raw[len(cand):].strip() or None
                break
    if code is None:
        parts = raw.split(None, 1)
        code = parts[0] if parts else None
        room = parts[1].strip() if len(parts) > 1 else None
    return Meeting(location_raw=raw, building=code or None, room=room)


def parse_course_label(text: str) -> tuple[str, str]:
    """'AS-1115' / 'CS 2114' -> ('AS', '1115'). """
    raw = (text or "").strip()
    m = re.match(r"^([A-Za-z]{2,8})[\s-]*(\d{3,4}[A-Za-z]?)$", raw)
    if m:
        return m.group(1).upper(), m.group(2).upper()
    if "-" in raw:
        left, _, right = raw.partition("-")
        return left.strip().upper(), right.strip().upper()
    return raw.upper(), ""


def is_valid_crn(crn: str) -> bool:
    return bool(re.match(r"^\d{3,5}$", str(crn).strip()))


def parse_crns(text: str | Iterable[str]) -> tuple[list[str], list[str]]:
    """Parse pasted CRNs -> (valid, invalid). Accepts comma/space/newline runs."""
    if isinstance(text, str):
        raw_items = re.split(r"[\s,;]+", text)
    else:
        raw_items = []
        for item in text:
            raw_items.extend(re.split(r"[\s,;]+", str(item)))
    valid: list[str] = []
    invalid: list[str] = []
    for item in raw_items:
        item = item.strip()
        if not item:
            continue
        if is_valid_crn(item):
            valid.append(item)
        else:
            invalid.append(item)
    return valid, invalid


# ---------------------------------------------------------------------------
# Timetable result parsing
# ---------------------------------------------------------------------------
_TIMETABLE_SUMMARY = "timetable of classes"


def _is_timetable_table(attrs: dict) -> bool:
    summary = (attrs.get("summary") or "").lower()
    if _TIMETABLE_SUMMARY in summary:
        return True
    # Fallback for a saved fragment that lost the summary: Banner's results
    # table is class="dataentrytable".
    return attrs.get("class", "").strip().lower() == "dataentrytable"


_COMMENT_RE = re.compile(r"Comments for CRN\s+(\d+)", re.I)


def parse_timetable_html(
    html: str,
    *,
    term: str = TERM_FALL_2026,
    campus: str = DEFAULT_CAMPUS,
    crosswalk: dict[str, Any] | None = None,
    snapshot_id: str = "",
) -> TimetableParse:
    """Parse a Banner timetable RESULT page into normalized sections.

    One result row == one meeting. Rows sharing a CRN are merged into one
    ClassSection with multiple meetings (multi-meeting patterns). A
    "Comments for CRN ..." row is attached to its section. Anything that does
    not look like a section row is skipped with a warning -- never invented.
    """
    warnings: list[str] = []
    rows = extract_table_rows(html, _is_timetable_table)
    if not rows:
        return TimetableParse(
            term=str(term), sections=(), empty=True, snapshot_id=snapshot_id,
            warnings=("no timetable table found in the supplied HTML",))

    sections: dict[str, dict[str, Any]] = {}
    comments: dict[str, list[str]] = {}
    order: list[str] = []
    data_rows = 0

    for row in rows:
        joined = " ".join(row).strip()
        if not joined:
            continue
        m = _COMMENT_RE.search(joined)
        if m and not row[0].strip().isdigit():
            crn = m.group(1)
            after = joined[m.end():].lstrip(" :")
            # Drop a leading "Comments for CRN NNNNN:" label remnant.
            if after:
                comments.setdefault(crn, []).append(after)
            continue

        if not row[0].strip().isdigit():
            continue                      # header / info row
        if len(row) < 12:
            warnings.append(f"CRN {row[0]}: row has only {len(row)} cells; skipped")
            continue

        crn = row[0].strip()
        course_raw = row[1] if len(row) > 1 else ""
        title = row[2] if len(row) > 2 else ""
        sched_type = row[3] if len(row) > 3 else ""
        modality = row[4] if len(row) > 4 else ""
        credit = row[5] if len(row) > 5 else ""
        capacity = row[6] if len(row) > 6 else ""
        instructor = row[7] if len(row) > 7 else ""
        days_raw = row[8] if len(row) > 8 else ""
        begin_raw = row[9] if len(row) > 9 else ""
        end_raw = row[10] if len(row) > 10 else ""
        loc_raw = row[11] if len(row) > 11 else ""
        exam_code = row[12] if len(row) > 12 else ""

        subject, course_number = parse_course_label(course_raw)
        meeting = parse_location(loc_raw, crosswalk)
        meeting = replace(
            meeting,
            days=parse_days(days_raw),
            begin=parse_clock(begin_raw),
            end=parse_clock(end_raw),
        )

        if crn in sections:
            sections[crn]["meetings"].append(meeting)
            if sections[crn]["title"] and title and sections[crn]["title"] != title:
                warnings.append(
                    f"CRN {crn}: title differs across meeting rows "
                    f"({sections[crn]['title']!r} vs {title!r}); kept first")
        else:
            sections[crn] = {
                "crn": crn, "subject": subject, "course_number": course_number,
                "title": title, "schedule_type": sched_type, "modality": modality,
                "credit_hours": credit, "capacity": capacity,
                "instructor": instructor, "exam_code": exam_code,
                "meetings": [meeting],
            }
            order.append(crn)
        data_rows += 1

    out: list[ClassSection] = []
    for crn in order:
        data = sections[crn]
        meetings = tuple(data.pop("meetings"))
        rows_warn: list[str] = []
        if any(not m.has_time for m in meetings):
            rows_warn.append("meeting time/location is TBA/ARR")
        building_names: dict[str, str] = {}
        for m in meetings:
            if m.building and crosswalk and m.building in crosswalk:
                building_names[m.building] = getattr(
                    crosswalk[m.building], "name", str(crosswalk[m.building]))
            elif m.building and not m.is_online and not m.is_tba:
                rows_warn.append(f"unknown building code {m.building!r}")
        sec = ClassSection(
            term=str(term), campus=str(campus),
            comments=tuple(comments.get(crn, ())),
            building_names=building_names,
            meetings=meetings,
            warnings=tuple(_dedupe(rows_warn)),
            snapshot_id=snapshot_id,
            **data,
        )
        out.append(sec)

    empty = data_rows == 0
    if empty:
        warnings.append("timetable table found but contained no section rows")
    return TimetableParse(
        term=str(term), sections=tuple(out), warnings=tuple(warnings),
        empty=empty, snapshot_id=snapshot_id)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.append(it)
    return seen


# ---------------------------------------------------------------------------
# Search / list
# ---------------------------------------------------------------------------
def search(
    sections: Iterable[ClassSection],
    *,
    term: str | None = None,
    subject: str | None = None,
    course_number: str | None = None,
    crn: str | None = None,
    crns: Iterable[str] | None = None,
    title_contains: str | None = None,
    days: Iterable[str] | None = None,
    modality_contains: str | None = None,
    instructor_contains: str | None = None,
    schedule_type: str | None = None,
) -> list[ClassSection]:
    """Filter a parsed section list. Pure and order-preserving.

    ``days`` matches sections that meet on ANY of the requested days. All
    provided filters must match (AND). Case-insensitive where text.
    """
    want_days = set(parse_days(" ".join(days))) if days else set()
    want_crns = set(str(c).strip() for c in (crns or ()) if str(c).strip())
    if crn:
        want_crns.add(str(crn).strip())
    out: list[ClassSection] = []
    for s in sections:
        if term and str(s.term) != str(term):
            continue
        if subject and s.subject.upper() != str(subject).upper():
            continue
        if course_number and s.course_number.upper() != str(course_number).upper():
            continue
        if want_crns and s.crn not in want_crns:
            continue
        if title_contains and str(title_contains).lower() not in s.title.lower():
            continue
        if modality_contains and str(modality_contains).lower() not in s.modality.lower():
            continue
        if instructor_contains and str(instructor_contains).lower() not in s.instructor.lower():
            continue
        if schedule_type and s.schedule_type.upper() != str(schedule_type).upper():
            continue
        if want_days and not (want_days & set().union(*(m.days for m in s.meetings))):
            continue
        out.append(s)
    return out


@dataclass(frozen=True)
class SelectionResult:
    found: tuple[ClassSection, ...]
    missing: tuple[str, ...]
    invalid: tuple[str, ...]
    duplicates: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "found": [s.to_dict() for s in self.found],
            "missing": list(self.missing),
            "invalid": list(self.invalid),
            "duplicates": list(self.duplicates),
        }


def select_crns(sections: Iterable[ClassSection], crns: str | Iterable[str],
                *, term: str | None = None) -> SelectionResult:
    """Resolve pasted CRNs against parsed sections -> found/missing/invalid.

    CRN identity is (term, crn). When a term is given, a CRN from another term
    is reported missing rather than silently matched.
    """
    valid, invalid = parse_crns(crns)
    by_crn: dict[str, ClassSection] = {}
    for s in sections:
        if term and str(s.term) != str(term):
            continue
        by_crn.setdefault(s.crn, s)
    found: list[ClassSection] = []
    missing: list[str] = []
    dupes: list[str] = []
    seen: set[str] = set()
    for c in valid:
        if c in seen:
            dupes.append(c)
            continue
        seen.add(c)
        if c in by_crn:
            found.append(by_crn[c])
        else:
            missing.append(c)
    return SelectionResult(tuple(found), tuple(missing), tuple(invalid),
                           tuple(dupes))


# ---------------------------------------------------------------------------
# Recurrence / date-range expansion (owned by code, never by the LLM)
# ---------------------------------------------------------------------------
def _weekday_dates(start: date, end: date, weekday: int) -> list[date]:
    """Every `weekday` on/after `start` and on/before `end` (weekly step)."""
    first = start + timedelta(days=(weekday - start.weekday()) % 7)
    out: list[date] = []
    d = first
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out


def _make_dt(d: date, hhmm: str, tz: ZoneInfo) -> datetime:
    hh, mm = (int(p) for p in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)


def campus_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.CAMPUS_TZ)
    except ZoneInfoNotFoundError:            # pragma: no cover - tzdata missing
        return ZoneInfo("UTC")


def expand_section(
    section: ClassSection,
    *,
    start: date,
    end: date,
    tz: ZoneInfo | None = None,
    skip_holidays: bool = True,
) -> list[ClassOccurrence]:
    """Expand a section's weekly meetings across [start, end] inclusive.

    TBA meetings produce NO occurrences (they have no day/time to expand) and
    are reported by ``ClassSection.has_tba`` / flags instead. University
    holidays for the section's term are skipped when ``skip_holidays``.
    """
    tz = tz or campus_tz()
    holidays = _holiday_set(section.term) if skip_holidays else frozenset()
    out: list[ClassOccurrence] = []
    for m in section.meetings:
        if not m.has_time:
            continue
        for day in m.days:
            wd = DAY_TO_WEEKDAY.get(day)
            if wd is None:
                continue
            for d in _weekday_dates(start, end, wd):
                if d in holidays:
                    continue
                out.append(ClassOccurrence(
                    term=section.term, crn=section.crn, subject=section.subject,
                    course_number=section.course_number, title=section.title,
                    date=d, start=_make_dt(d, m.begin, tz),
                    end=_make_dt(d, m.end, tz), meeting=m,
                ))
    out.sort(key=lambda o: (o.start, o.crn))
    return out


def expand_sections(
    sections: Iterable[ClassSection],
    *,
    start: date,
    end: date,
    tz: ZoneInfo | None = None,
    skip_holidays: bool = True,
) -> list[ClassOccurrence]:
    out: list[ClassOccurrence] = []
    for s in sections:
        out.extend(expand_section(s, start=start, end=end, tz=tz,
                                  skip_holidays=skip_holidays))
    out.sort(key=lambda o: (o.start, o.crn))
    return out


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------
def find_conflicts(
    occurrences: Iterable[ClassOccurrence],
    *,
    include_same_crn: bool = False,
) -> list[Conflict]:
    """Overlapping occurrences (half-open intervals: end == start is NOT a
    conflict). Different meetings of the SAME CRN never conflict by default."""
    items = sorted(occurrences, key=lambda o: (o.start, o.end, o.crn))
    out: list[Conflict] = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if b.start >= a.end:
                break
            if b.start < a.end and a.start < b.end:
                if not include_same_crn and a.crn == b.crn:
                    continue
                overlap = (min(a.end, b.end) - max(a.start, b.start)).total_seconds() / 60
                if overlap > 0:
                    out.append(Conflict(a, b, overlap))
    return out


def schedule_conflicts(
    sections: Iterable[ClassSection],
    *,
    start: date,
    end: date,
    tz: ZoneInfo | None = None,
) -> list[Conflict]:
    occ = expand_sections(sections, start=start, end=end, tz=tz)
    return find_conflicts(occ)


# ---------------------------------------------------------------------------
# Next class / deterministic planning deadline
# ---------------------------------------------------------------------------
def next_class(
    sections: Iterable[ClassSection],
    at: datetime,
    *,
    start: date | None = None,
    end: date | None = None,
    buffer_min: float = 10.0,
    skip_online: bool = False,
    tz: ZoneInfo | None = None,
) -> NextClass | None:
    """The next timed class meeting at/after ``at`` -> deterministic deadline.

    ``deadline`` (leave_by) = class start - ``buffer_min``. This is the value
    HokieFlow uses as the planning horizon; it is computed, not narrated.
    TBA meetings are ignored here (they have no time) -- surface them with
    ``ClassSection.has_tba`` so the UI can show the TBA state.
    """
    tz = tz or campus_tz()
    if at.tzinfo is None:
        at = at.replace(tzinfo=tz)
    section_list = list(sections)
    if start is None or end is None:
        if start is None:
            starts = [term_window(s.term).classes_begin for s in section_list
                      if term_window(s.term)]
            start = min(starts) if starts else at.date()
        if end is None:
            ends = [term_window(s.term).classes_end for s in section_list
                    if term_window(s.term)]
            end = max(ends) if ends else at.date() + timedelta(days=120)
    occ = expand_sections(section_list, start=start, end=end, tz=tz)
    for o in occ:
        if o.start < at:
            continue
        if skip_online and o.meeting.is_online:
            continue
        return NextClass(
            occurrence=o,
            minutes_until=(o.start - at).total_seconds() / 60,
            leave_by=o.start - timedelta(minutes=buffer_min),
            buffer_min=buffer_min,
        )
    return None


def next_class_json(
    sections: Iterable[ClassSection],
    at: datetime,
    *,
    start: date | None = None,
    end: date | None = None,
    buffer_min: float = 10.0,
    skip_online: bool = False,
    tz: ZoneInfo | None = None,
) -> dict:
    tz = tz or campus_tz()
    if at.tzinfo is None:
        at = at.replace(tzinfo=tz)
    nc = next_class(sections, at, start=start, end=end, buffer_min=buffer_min,
                    skip_online=skip_online, tz=tz)
    if nc is None:
        return {
            "schema": SCHEMA_NEXT_CLASS,
            "status": "none",
            "at": at.isoformat(timespec="seconds"),
            "deadline": None,
            "buffer_min": buffer_min,
            "reason": "no timed class meeting found in the requested window; "
                      "check TBA meetings with section.flags().has_tba",
        }
    payload = {"schema": SCHEMA_NEXT_CLASS, "at": at.isoformat(timespec="seconds")}
    payload.update(nc.to_dict())
    return payload


# ---------------------------------------------------------------------------
# Local schedule (no DB yet -- plain JSON-ready records)
# ---------------------------------------------------------------------------
def _schedule_record(section: ClassSection, snapshot_id: str = "") -> dict:
    """A compact, JSON/Lakebase-ready selected-class record.

    Deliberately NOT the whole section payload: the schedule stores identity
    plus a display snapshot. Grades/roster/PID are never present by design.
    """
    return {
        "term": section.term,
        "crn": section.crn,
        "subject": section.subject,
        "course_number": section.course_number,
        "course": section.course,
        "title": section.title,
        "meetings": [m.to_dict() for m in section.meetings],
        "flags": section.flags(),
        "snapshot_id": snapshot_id or section.snapshot_id,
    }


def add_to_schedule(
    schedule: Iterable[dict],
    sections: Iterable[ClassSection],
    crns: str | Iterable[str],
    *,
    term: str | None = None,
    snapshot_id: str = "",
) -> dict:
    """Add selected CRNs to a local schedule list.

    Returns a JSON-ready dict; the caller persists it (no DB here). Duplicate
    (term, crn) entries are reported, not added twice.
    """
    current = list(schedule or [])
    have = {(str(r.get("term", "")), str(r.get("crn", ""))) for r in current}
    sel = select_crns(sections, crns, term=term)
    added: list[dict] = []
    already: list[str] = list(sel.duplicates)
    for s in sel.found:
        key = (s.term, s.crn)
        if key in have:
            already.append(s.crn)
            continue
        have.add(key)
        rec = _schedule_record(s, snapshot_id)
        current.append(rec)
        added.append(rec)
    return {
        "schema": SCHEMA_SCHEDULE,
        "schedule": current,
        "added": added,
        "missing": list(sel.missing),
        "invalid": list(sel.invalid),
        "already_in_schedule": _dedupe(already),
    }


def remove_from_schedule(
    schedule: Iterable[dict],
    crns: str | Iterable[str],
) -> dict:
    valid, invalid = parse_crns(crns)
    want = set(valid)
    kept: list[dict] = []
    removed: list[str] = []
    for rec in schedule or []:
        crn = str(rec.get("crn", ""))
        if crn in want:
            removed.append(crn)
        else:
            kept.append(rec)
    missing = [c for c in valid if c not in removed]
    return {
        "schema": SCHEMA_SCHEDULE,
        "schedule": kept,
        "removed": _dedupe(removed),
        "not_in_schedule": missing,
        "invalid": invalid,
    }


def resolve_schedule(
    schedule: Iterable[dict],
    sections: Iterable[ClassSection],
) -> list[ClassSection]:
    """Join a stored schedule back to freshly parsed sections by (term, crn)."""
    by_key = {(s.term, s.crn): s for s in sections}
    out: list[ClassSection] = []
    for rec in schedule or []:
        s = by_key.get((str(rec.get("term", "")), str(rec.get("crn", ""))))
        if s is not None:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Building crosswalk (ready for the later VT GIS join; NO invented coords)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Building:
    code: str
    name: str
    lat: float | None = None
    lon: float | None = None
    source: str = "banner_building_list"
    gis_verified: bool = False

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lon is not None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "source": self.source,
            "gis_verified": self.gis_verified,
        }


def _is_building_table(attrs: dict) -> bool:
    if "building list" in (attrs.get("caption") or "").lower():
        return True
    return attrs.get("class", "").strip().lower() == "dataentrytable"


def parse_building_list_html(html: str) -> list[Building]:
    """Parse HZSKVTS P_DispBldgList into code -> description rows.

    The Banner page is a two-column table. Codes contain spaces ('AJ E'), so
    the code is the whole first cell. Rows that are not (code, description)
    pairs are skipped.
    """
    rows = extract_table_rows(html, _is_building_table)
    out: list[Building] = []
    seen: set[str] = set()
    for row in rows:
        if len(row) != 2:
            continue
        code, name = row[0].strip(), row[1].strip()
        if not code or not name:
            continue
        low = code.lower()
        if low in ("abbreviation", "description"):
            continue
        if low.startswith(("click", "you may", "press", "close")):
            continue
        if len(code) > 12:
            continue
        if code in seen:
            continue
        seen.add(code)
        out.append(Building(code=code, name=name))
    return out


def load_buildings(path: str | Path | None = None) -> list[Building]:
    p = Path(path) if path else config.FIXTURES_DIR / "classes_buildings.html"
    if not p.exists():
        return []
    return parse_building_list_html(p.read_text(encoding="utf-8"))


def building_crosswalk(buildings: Iterable[Building] | None = None) -> dict[str, Building]:
    """code -> Building. Lookup is case-insensitive on the code."""
    rows = list(buildings) if buildings is not None else load_buildings()
    out: dict[str, Building] = {}
    for b in rows:
        out[b.code.upper()] = b
    return out


def attach_gis_coords(
    crosswalk: dict[str, Building],
    coords: dict[str, tuple[float, float]],
    *,
    source: str = "VT GIS (caller supplied)",
) -> dict[str, Building]:
    """Return a NEW crosswalk with coordinates joined from `coords`.

    Coordinates are never invented here. Codes not present in the mapping keep
    ``lat/lon = None`` and stay ``gis_verified=False``. Out-of-range values
    raise instead of being silently accepted.
    """
    out = dict(crosswalk)
    for code, (lat, lon) in coords.items():
        key = str(code).upper()
        if key not in out:
            continue
        lat_f, lon_f = float(lat), float(lon)
        if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lon_f <= 180.0):
            raise ValueError(f"coordinate out of range for {code!r}: {lat_f}, {lon_f}")
        out[key] = replace(out[key], lat=lat_f, lon=lon_f, source=source,
                           gis_verified=True)
    return out


def crosswalk_contract(crosswalk: dict[str, Building] | None = None) -> dict:
    """JSON contract for the later VT GIS join. Rows carry no coordinates here."""
    cw = crosswalk if crosswalk is not None else building_crosswalk()
    rows = [cw[k].to_dict() for k in sorted(cw)]
    return {
        "schema": "hokieday.classes.building_crosswalk/1",
        "count": len(rows),
        "coords_present": any(r["lat"] is not None for r in rows),
        "gis_join": {
            "join_key": "code",
            "expected": "VT GIS building-point layer keyed by building code",
            "apply": "attach_gis_coords(crosswalk, {code: (lat, lon)})",
            "policy": "never invent coordinates; unjoined codes stay null",
        },
        "buildings": rows,
    }


# ---------------------------------------------------------------------------
# Numeric exam-code schedule (public HZSKEXAM page) -- optional hook
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExamSlot:
    code: str
    date: date | None
    begin: str | None
    end: str | None
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "date": self.date.isoformat() if self.date else None,
            "begin": self.begin,
            "end": self.end,
            "label": self.label,
        }


def parse_exam_schedule_html(html: str, *, term: str = TERM_FALL_2026) -> list[ExamSlot]:
    """Best-effort parse of the public final-exam grid.

    The page is a matrix of exam codes under date/time headers. This parser
    reads the header cells to learn each column's date + window and returns
    one ExamSlot per (code, occurrence). It is deliberately conservative: a
    code with no resolvable header context is skipped rather than guessed.
    """
    rows = extract_table_rows(html, lambda a: "plaintable" in a.get("class", "")
                              or "dataentrytable" in a.get("class", ""))
    slots: list[ExamSlot] = []
    seen: set[tuple[str, str]] = set()
    current_date: date | None = None
    current_begin: str | None = None
    current_end: str | None = None
    for row in rows:
        # A date header row contains a long date like "Friday , Dec 11".
        for cell in row:
            m = re.search(r"([A-Z][a-z]+)\s*,?\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|"
                          r"Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})", cell)
            if m:
                try:
                    mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                           "Aug", "Sep", "Oct", "Nov", "Dec"].index(m.group(2)) + 1
                    current_date = date(2026, mon, int(m.group(3)))
                except (ValueError, IndexError):
                    current_date = None
            tm = re.search(r"(\d{1,2}:\d{2}(?:AM|PM)?)\s*(?:to|-)\s*"
                           r"(\d{1,2}:\d{2}(?:AM|PM)?)", cell, re.I)
            if tm:
                current_begin = parse_clock(tm.group(1).upper())
                current_end = parse_clock(tm.group(2).upper())
        for cell in row:
            for code in re.findall(r"\b(\d{2}[A-Z])\b", cell):
                key = (code, f"{current_date}-{current_begin}")
                if key in seen:
                    continue
                seen.add(key)
                slots.append(ExamSlot(code=code, date=current_date,
                                      begin=current_begin, end=current_end,
                                      label="final exam grid"))
    return slots


def exam_slot_for(section: ClassSection, slots: Iterable[ExamSlot]) -> ExamSlot | None:
    if not section.exam_code:
        return None
    for s in slots:
        if s.code.upper() == section.exam_code.upper():
            return s
    return None


# ---------------------------------------------------------------------------
# ICS import (bounded VEVENT subset; reject/flag, never guess)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IcsEvent:
    uid: str
    summary: str
    location: str
    dtstart: datetime                 # campus-local aware (or UTC-derived)
    dtend: datetime
    all_day: bool
    tzid: str | None
    rrule: dict[str, str] = field(default_factory=dict)
    days: tuple[str, ...] = ()        # Banner day letters for weekly recurrence
    recurring: bool = False           # True only when an RRULE was present
    raw_start: str = ""
    raw_end: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def duration_min(self) -> float:
        return (self.dtend - self.dtstart).total_seconds() / 60

    def to_meeting(self) -> Meeting:
        b = None if self.all_day else self.dtstart.strftime("%H:%M")
        e = None if self.all_day else self.dtend.strftime("%H:%M")
        meeting = parse_location(self.location)
        return replace(meeting, days=self.days if not self.all_day else (),
                       begin=b, end=e)

    def to_section(self, *, term: str = "") -> ClassSection:
        meeting = self.to_meeting()
        warn = list(self.warnings)
        warn.append("imported from ICS: identity is UID, not a VT CRN")
        return ClassSection(
            term=term, crn=self.uid, subject="", course_number="",
            title=self.summary or self.uid, modality="(ICS import)",
            meetings=(meeting,), warnings=tuple(_dedupe(warn)),
            source="ics",
        )

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "summary": self.summary,
            "location": self.location,
            "dtstart": self.dtstart.isoformat(timespec="seconds"),
            "dtend": self.dtend.isoformat(timespec="seconds"),
            "all_day": self.all_day,
            "tzid": self.tzid,
            "rrule": dict(self.rrule),
            "days": list(self.days),
            "recurring": self.recurring,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class IcsParse:
    events: tuple[IcsEvent, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    valid: bool = True

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "events": [e.to_dict() for e in self.events],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def unfold_ics(text: str) -> list[str]:
    """RFC 5545 line unfolding: a leading space/tab continues the prior line."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    """'DTSTART;TZID=America/New_York:20260824T100000' -> (name, params, value)."""
    if ":" not in line:
        return None
    head, _, value = line.partition(":")
    parts = head.split(";")
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, _, v = p.partition("=")
            params[k.strip().upper()] = v.strip().strip('"')
        elif p.strip():
            params[p.strip().upper()] = ""
    return name, params, value


def _parse_ics_dt(value: str, params: dict[str, str], default_tz: ZoneInfo,
                  warnings: list[str]) -> tuple[datetime, bool, str | None]:
    """Return (aware dt, all_day, tzid). Raises ValueError on a malformed value."""
    value = value.strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or re.match(r"^\d{8}$", value)
    tzid = params.get("TZID")
    if is_date:
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=default_tz), True, tzid
    m = re.match(r"^(\d{8})T(\d{6})(Z?)$", value)
    if not m:
        raise ValueError(f"unsupported DTSTART/DTEND value {value!r}")
    base = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if m.group(3) == "Z":
        return base.replace(tzinfo=timezone.utc).astimezone(default_tz), False, tzid
    if tzid:
        try:
            return base.replace(tzinfo=ZoneInfo(tzid)), False, tzid
        except ZoneInfoNotFoundError:
            warnings.append(f"unknown TZID {tzid!r}; assuming campus time")
            return base.replace(tzinfo=default_tz), False, tzid
    warnings.append("floating time with no TZID; assuming campus time")
    return base.replace(tzinfo=default_tz), False, tzid


_ALLOWED_RRULE = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}


def _parse_rrule(value: str, dtstart_weekday: int,
                 warnings: list[str]) -> tuple[dict[str, str], tuple[str, ...]]:
    rule: dict[str, str] = {}
    for part in value.split(";"):
        if not part:
            continue
        k, _, v = part.partition("=")
        k = k.strip().upper()
        if k not in _ALLOWED_RRULE:
            warnings.append(f"unsupported RRULE part {k!r}; event flagged")
            rule.setdefault("_UNSUPPORTED", "")
            continue
        rule[k] = v.strip().upper()
    freq = rule.get("FREQ", "")
    if freq != "WEEKLY":
        warnings.append(f"unsupported RRULE FREQ={freq or '?'} (only WEEKLY)")
        rule["_UNSUPPORTED"] = ""
        return rule, ()
    byday = rule.get("BYDAY", "")
    days: list[str] = []
    if byday:
        for tok in byday.split(","):
            tok = tok.strip().upper().lstrip("+-0123456789")
            b = ICS_DAY_TO_BANNER.get(tok)
            if b and b not in days:
                days.append(b)
    if not days:
        days = [WEEKDAY_TO_DAY[dtstart_weekday]]
    return rule, tuple(sorted(days, key=lambda d: DAY_TO_WEEKDAY[d]))


def parse_ics(text: str, *, tz: ZoneInfo | None = None) -> IcsParse:
    """Parse an ICS calendar into a bounded, useful VEVENT subset.

    Supported: VCALENDAR/VEVENT, line unfolding, DTSTART/DTEND (DATE, UTC 'Z',
    TZID, or floating->campus warning), SUMMARY, LOCATION, UID, and weekly
    RRULE (FREQ=WEEKLY with BYDAY/INTERVAL/COUNT/UNTIL). Unsupported or
    malformed parts are reported in ``warnings``/``errors`` and the event is
    skipped -- never guessed.
    """
    default_tz = tz or campus_tz()
    lines = unfold_ics(text)
    warnings: list[str] = []
    errors: list[str] = []
    events: list[IcsEvent] = []

    if not any(line.strip().upper() == "BEGIN:VCALENDAR" for line in lines):
        return IcsParse((), (), ("not an ICS calendar: missing BEGIN:VCALENDAR",),
                        valid=False)
    if not any(line.strip().upper() == "END:VCALENDAR" for line in lines):
        errors.append("calendar is truncated: missing END:VCALENDAR")

    in_event = False
    props: dict[str, tuple[dict[str, str], str]] = {}
    event_warnings: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            if in_event:
                errors.append("nested BEGIN:VEVENT; previous event discarded")
            in_event = True
            props = {}
            event_warnings = []
            continue
        if upper == "END:VEVENT":
            if not in_event:
                errors.append("END:VEVENT with no BEGIN:VEVENT")
                continue
            in_event = False
            ev, err = _build_event(props, event_warnings, default_tz)
            if ev is None:
                errors.append(err or "malformed VEVENT")
            else:
                events.append(ev)
            continue
        if not in_event:
            continue
        parsed = _split_prop(line)
        if parsed is None:
            warnings.append(f"unparseable ICS line skipped: {line[:60]!r}")
            continue
        name, params, value = parsed
        if name in ("BEGIN", "END"):
            # Sub-components (VALARM, STANDARD, ...) are ignored.
            if value.strip().upper() not in ("VALARM",):
                warnings.append(f"ignored sub-component {value.strip()}")
            continue
        props[name] = (params, value)

    if in_event:
        errors.append("truncated VEVENT (no END:VEVENT); event discarded")
    if not events and not errors:
        warnings.append("no VEVENT components found")
    return IcsParse(tuple(events), tuple(_dedupe(warnings)), tuple(_dedupe(errors)),
                    valid=not errors)


def _build_event(props: dict[str, tuple[dict[str, str], str]],
                 warnings: list[str], default_tz: ZoneInfo) -> tuple[IcsEvent | None, str]:
    if "DTSTART" not in props:
        return None, "VEVENT missing DTSTART"
    if "UID" not in props or not props["UID"][1].strip():
        return None, "VEVENT missing UID"
    try:
        dtstart, all_day, tzid = _parse_ics_dt(
            props["DTSTART"][1], props["DTSTART"][0], default_tz, warnings)
    except ValueError as exc:
        return None, f"DTSTART malformed: {exc}"

    if "DTEND" in props:
        try:
            dtend, end_all_day, _ = _parse_ics_dt(
                props["DTEND"][1], props["DTEND"][0], default_tz, warnings)
        except ValueError as exc:
            return None, f"DTEND malformed: {exc}"
        if end_all_day != all_day:
            warnings.append("DTSTART/DTEND disagree on DATE vs DATE-TIME")
    else:
        warnings.append("VEVENT missing DTEND; assuming zero-length")
        dtend = dtstart
    if dtend < dtstart:
        return None, "DTEND is before DTSTART"

    uid = props["UID"][1].strip()
    summary = props.get("SUMMARY", ({}, ""))[1].strip()
    location = props.get("LOCATION", ({}, ""))[1].strip()
    days: tuple[str, ...] = ()
    rrule: dict[str, str] = {}
    recurring = False
    if "RRULE" in props:
        rrule, days = _parse_rrule(props["RRULE"][1], dtstart.weekday(), warnings)
        recurring = True
    elif not all_day:
        # A single VEVENT with no RRULE is ONE class meeting, not a weekly one.
        # days is still populated for display/ICS->Meeting, but recurrence is
        # explicitly off so expansion cannot invent repeats.
        days = (WEEKDAY_TO_DAY[dtstart.weekday()],)
    if all_day:
        warnings.append("all-day event: no class time to expand")
    if "_UNSUPPORTED" in rrule:
        return None, f"unsupported RRULE for UID {uid!r}; event skipped"
    return IcsEvent(
        uid=uid, summary=summary, location=location, dtstart=dtstart,
        dtend=dtend, all_day=all_day, tzid=tzid, rrule=rrule, days=days,
        recurring=recurring,
        raw_start=props["DTSTART"][1], raw_end=props.get("DTEND", ({}, ""))[1],
        warnings=tuple(_dedupe(warnings)),
    ), ""


def expand_ics_event(event: IcsEvent, *, start: date | None = None,
                     end: date | None = None,
                     tz: ZoneInfo | None = None) -> list[ClassOccurrence]:
    """Expand a supported IcsEvent's weekly recurrence into occurrences.

    Respects INTERVAL/COUNT/UNTIL and BYDAY. All-day or unsupported events
    yield nothing (they are flagged by the parser instead).
    """
    if event.all_day or not event.days:
        return []
    tz = tz or campus_tz()
    rrule = event.rrule
    duration = event.dtend - event.dtstart
    first = event.dtstart.date()

    if not event.recurring:
        # A plain VEVENT is a single occurrence at DTSTART (honoring any window).
        if start and first < start:
            return []
        if end and first > end:
            return []
        meeting = event.to_meeting()
        return [ClassOccurrence(
            term="", crn=event.uid, subject="", course_number="",
            title=event.summary, date=first, start=event.dtstart,
            end=event.dtend, meeting=meeting, title_extra="ics",
        )]

    interval = int(rrule.get("INTERVAL", "1") or 1) or 1
    count = int(rrule["COUNT"]) if rrule.get("COUNT", "").isdigit() else None
    until: date | None = None
    if rrule.get("UNTIL"):
        raw = rrule["UNTIL"]
        try:
            until_dt, _, _ = _parse_ics_dt(raw, {}, tz, [])
            until = until_dt.date()
        except ValueError:
            until = None
    win_start = max(start or first, first)
    win_end = end or until or (first + timedelta(days=365))
    if until:
        win_end = min(win_end, until)

    # Iterate week-by-week so INTERVAL is honored exactly. COUNT counts every
    # recurrence from DTSTART (RFC 5545), so occurrences before the query
    # window still consume the count instead of being re-issued later.
    out: list[ClassOccurrence] = []
    n = 0
    week = first - timedelta(days=first.weekday())
    while week - timedelta(days=6) <= win_end:
        for day_letter in event.days:
            wd = DAY_TO_WEEKDAY[day_letter]
            d = week + timedelta(days=wd)
            if d < first:
                continue
            n += 1
            if count is not None and n > count:
                out.sort(key=lambda o: o.start)
                return out
            if d < win_start or d > win_end:
                continue
            start_dt = datetime(d.year, d.month, d.day, event.dtstart.hour,
                                event.dtstart.minute, tzinfo=event.dtstart.tzinfo)
            out.append(ClassOccurrence(
                term="", crn=event.uid, subject="", course_number="",
                title=event.summary, date=d, start=start_dt,
                end=start_dt + duration, meeting=event.to_meeting(),
                title_extra="ics",
            ))
            if count is not None and n >= count:
                out.sort(key=lambda o: o.start)
                return out
        week += timedelta(days=7 * interval)
    out.sort(key=lambda o: o.start)
    return out


# ---------------------------------------------------------------------------
# Snapshot (produced by the EDGE script; parsed offline here)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TimetableSnapshot:
    term: str
    html: str
    query: dict
    fetched_at: datetime
    source_url: str = BANNER_PROC_URL
    snapshot_id: str = ""
    campus: str = DEFAULT_CAMPUS
    schema: str = SCHEMA_SNAPSHOT

    def meta_dict(self) -> dict:
        return {
            "schema": self.schema,
            "id": self.snapshot_id,
            "term": self.term,
            "term_name": term_name(self.term),
            "campus": self.campus,
            "query": dict(self.query),
            "source_url": self.source_url,
            "fetched_at": self.fetched_at.isoformat(timespec="seconds"),
        }

    def to_dict(self) -> dict:
        payload = self.meta_dict()
        payload["html"] = self.html
        return payload


def make_snapshot(html: str, *, term: str, query: dict | None = None,
                  source_url: str = BANNER_PROC_URL,
                  fetched_at: datetime | None = None,
                  campus: str = DEFAULT_CAMPUS,
                  snapshot_id: str = "") -> dict:
    """Build the JSON envelope the edge script writes and this module reads.

    ``query`` is sanitized to a whitelist of non-PII keys: no instructor name,
    no student identity, no cookie, no PID is ever persisted.
    """
    safe_query = sanitize_query(query or {})
    fetched = fetched_at or config.now(timezone.utc)
    digest_src = json.dumps(
        {"term": str(term), "query": safe_query}, sort_keys=True,
        separators=(",", ":"))
    sid = snapshot_id or (
        "classes_snapshot__" + hashlib.sha1(digest_src.encode()).hexdigest()[:12])
    return {
        "schema": SCHEMA_SNAPSHOT,
        "id": sid,
        "term": str(term),
        "term_name": term_name(term),
        "campus": str(campus),
        "query": safe_query,
        "source_url": source_url,
        "fetched_at": fetched.isoformat(timespec="seconds"),
        "html": html,
    }


QUERY_KEYS = (
    "campus", "term", "subject", "course_number", "crn", "core_code",
    "schedule_type", "session", "open_only", "comments",
)
# Keys that must NEVER be persisted in a snapshot (PII-ish or credential-ish).
FORBIDDEN_QUERY_KEYS = frozenset({
    "inst_name", "instructor", "pin", "pid", "password", "passwd", "cookie",
    "session_id", "token", "email", "student", "bannerid", "u_no",
})


def sanitize_query(query: dict) -> dict:
    """Keep only whitelisted source-query fields; drop anything PII-shaped."""
    out: dict = {}
    for key, value in (query or {}).items():
        k = str(key).strip().lower()
        if k in FORBIDDEN_QUERY_KEYS:
            continue
        if k not in QUERY_KEYS:
            continue
        if value is None or value == "":
            continue
        out[k] = value
    return out


def save_snapshot(path: str | Path, snapshot: dict) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return p


def load_snapshot(path: str | Path) -> TimetableSnapshot:
    """Read a snapshot JSON envelope (see scripts/fetch_classes.py)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA_SNAPSHOT:
        raise ValueError(f"{path}: not a {SCHEMA_SNAPSHOT} snapshot")
    fetched_at = data.get("fetched_at")
    if isinstance(fetched_at, str):
        try:
            fetched_at = datetime.fromisoformat(fetched_at)
        except ValueError as exc:
            raise ValueError(f"{path}: bad fetched_at {fetched_at!r}") from exc
    if fetched_at is None:
        fetched_at = config.now(timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return TimetableSnapshot(
        term=str(data.get("term", "")),
        html=str(data.get("html", "")),
        query=dict(data.get("query", {}) or {}),
        fetched_at=fetched_at,
        source_url=str(data.get("source_url", BANNER_PROC_URL)),
        snapshot_id=str(data.get("id", Path(path).stem)),
        campus=str(data.get("campus", DEFAULT_CAMPUS)),
        schema=str(data.get("schema", SCHEMA_SNAPSHOT)),
    )


def snapshot_age_seconds(snapshot: TimetableSnapshot,
                         now: datetime | None = None) -> float:
    at = now or config.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (at - snapshot.fetched_at).total_seconds()


def snapshot_is_stale(snapshot: TimetableSnapshot, *,
                      max_age_s: float = 6 * 3600,
                      now: datetime | None = None) -> bool:
    return snapshot_age_seconds(snapshot, now) > max_age_s


def snapshot_sections(snapshot: TimetableSnapshot,
                      crosswalk: dict[str, Building] | None = None) -> TimetableParse:
    return parse_timetable_html(
        snapshot.html, term=snapshot.term, campus=snapshot.campus,
        crosswalk=crosswalk, snapshot_id=snapshot.snapshot_id)


# ---------------------------------------------------------------------------
# Composite API-ready contracts + Figma UI states
# ---------------------------------------------------------------------------
# The eight UI states the frontend must render. Kept as data so the contract
# can be asserted in tests and shared with the design without a UI dependency.
UI_STATES: dict[str, str] = {
    "loading": "request in flight; render skeleton rows, no stale results",
    "no_results": "query returned zero sections; offer subject/CRN reset",
    "tba_arr": "section has an ARR/TBA meeting; show TBA chip, no time",
    "unknown_building": "building code parsed but absent from crosswalk; "
                        "show code + 'location lookup unavailable'",
    "multiple_meetings": "section has >1 meeting pattern; render each block",
    "conflict": "two selected occurrences overlap; show both + overlap minutes",
    "stale_snapshot": "snapshot older than the freshness window; show as-of time",
    "malformed_ics": "ICS had unsupported/malformed parts; list warnings/errors",
}


def search_state(parse: TimetableParse, snapshot: TimetableSnapshot,
                 *, max_age_s: float = 6 * 3600,
                 now: datetime | None = None) -> str:
    if snapshot_is_stale(snapshot, max_age_s=max_age_s, now=now):
        return "stale_snapshot"
    if parse.empty or not parse.sections:
        return "no_results"
    return "results"


def search_result_json(
    parse: TimetableParse,
    snapshot: TimetableSnapshot,
    *,
    query: dict | None = None,
    max_age_s: float = 6 * 3600,
    now: datetime | None = None,
) -> dict:
    """Search/list response contract (see module docstring / docs/CLASSES.md)."""
    state = search_state(parse, snapshot, max_age_s=max_age_s, now=now)
    return {
        "schema": SCHEMA_SEARCH,
        "term": snapshot.term,
        "term_name": term_name(snapshot.term),
        "query": sanitize_query(query or snapshot.query),
        "snapshot": {
            **snapshot.meta_dict(),
            "is_stale": snapshot_is_stale(snapshot, max_age_s=max_age_s, now=now),
            "age_seconds": round(snapshot_age_seconds(snapshot, now), 1),
        },
        "state": state,
        "count": len(parse.sections),
        "sections": [s.to_dict() for s in parse.sections],
        "warnings": list(parse.warnings),
        "errors": list(parse.errors),
        "ui_states": list(UI_STATES),
    }


def schedule_json(
    schedule: Iterable[dict],
    sections: Iterable[ClassSection],
    *,
    start: date,
    end: date,
    now: datetime | None = None,
    max_age_s: float = 6 * 3600,
    snapshot: TimetableSnapshot | None = None,
) -> dict:
    """Add/remove/list schedule response with conflict + stale + state info."""
    schedule_list = list(schedule or [])
    section_list = list(sections)
    resolved = resolve_schedule(schedule_list, section_list)
    conflicts = schedule_conflicts(resolved, start=start, end=end)
    stale = snapshot_is_stale(snapshot, max_age_s=max_age_s, now=now) if snapshot else False
    known = {(s.term, s.crn) for s in section_list}
    return {
        "schema": SCHEMA_SCHEDULE,
        "schedule": schedule_list,
        "count": len(schedule_list),
        "conflicts": [c.to_dict() for c in conflicts],
        "state": "conflict" if conflicts else (
            "stale_snapshot" if stale else "ready"),
        "unresolved_crns": [
            r.get("crn") for r in schedule_list
            if (str(r.get("term", "")), str(r.get("crn", ""))) not in known
        ],
        "snapshot": snapshot.meta_dict() if snapshot else None,
    }


def section_state(section: ClassSection) -> list[str]:
    """Which UI states apply to one section (for render decisions)."""
    states: list[str] = []
    if section.has_tba:
        states.append("tba_arr")
    if section.has_unknown_building:
        states.append("unknown_building")
    if len(section.meetings) > 1:
        states.append("multiple_meetings")
    return states


__all__ = [
    "BANNER_BASE", "BANNER_FORM_URL", "BANNER_PROC_URL", "BANNER_BUILDINGS_URL",
    "BANNER_EXAMS_URL", "DEFAULT_CAMPUS", "DEFAULT_CORE_CODE",
    "SCHEMA_SNAPSHOT", "SCHEMA_SEARCH", "SCHEMA_NEXT_CLASS", "SCHEMA_SCHEDULE",
    "TERM_FALL_2026", "SUPPORTED_TERMS", "TERM_WINDOWS", "HOLIDAYS",
    "TermWindow", "term_name", "term_window", "Meeting", "ClassSection",
    "ClassOccurrence", "TimetableParse", "Conflict", "NextClass", "Building",
    "ExamSlot", "IcsEvent", "IcsParse", "SelectionResult", "TimetableSnapshot",
    "DAY_TO_WEEKDAY", "WEEKDAY_TO_DAY", "ICS_DAY_TO_BANNER", "BANNER_DAY_TO_ICS",
    "parse_days", "days_to_text", "parse_clock", "parse_location",
    "parse_course_label", "is_valid_crn", "parse_crns", "extract_table_rows",
    "parse_timetable_html", "search", "select_crns", "expand_section",
    "expand_sections", "campus_tz", "find_conflicts", "schedule_conflicts",
    "next_class", "next_class_json", "add_to_schedule", "remove_from_schedule",
    "resolve_schedule", "parse_building_list_html", "load_buildings",
    "building_crosswalk", "attach_gis_coords", "crosswalk_contract",
    "parse_exam_schedule_html", "exam_slot_for", "unfold_ics", "parse_ics",
    "expand_ics_event", "make_snapshot", "sanitize_query", "QUERY_KEYS",
    "FORBIDDEN_QUERY_KEYS", "save_snapshot", "load_snapshot",
    "snapshot_age_seconds", "snapshot_is_stale", "snapshot_sections",
    "UI_STATES", "search_state", "search_result_json", "schedule_json",
    "section_state",
]