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
  abbreviations). NEVER HokieSPA / My VT,
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
import math
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

# ICS safety bounds: a hostile or accidentally huge calendar must never exhaust
# memory or spin forever. Exceeding a bound is an explicit parse error/flag.
MAX_ICS_BYTES = 512 * 1024        # input size
MAX_ICS_LINES = 20_000            # unfolded lines
MAX_ICS_EVENTS = 500              # VEVENTs accepted
MAX_ICS_OCCURRENCES = 2_000       # occurrences per event expansion
MAX_ICS_WINDOW_DAYS = 730         # default recurrence cap (~2 years)

# A planning buffer larger than this is almost certainly a bug, not a plan.
MAX_BUFFER_MIN = 240.0

# GLOBAL bounds across a combined (Banner + ICS) schedule. Beyond these the
# result is refused with a typed ``BoundsExceeded`` error instead of building an
# unbounded O(n^2) conflict list.
MAX_TOTAL_OCCURRENCES = 5_000
MAX_CONFLICTS = 1_000


class BoundsExceeded(RuntimeError):
    """Typed refusal when a bounded schedule computation would blow up.

    ``kind`` is "occurrences" or "conflicts"; ``limit`` is the configured cap
    and ``actual`` the count that tripped it. Callers surface it as a typed
    state (e.g. ``schedule_json.state == "bounds_exceeded"``).
    """

    def __init__(self, kind: str, limit: int, actual: int):
        super().__init__(
            f"{kind} exceeded the {limit} cap (got {actual}); refusing to build "
            "an unbounded result")
        self.kind = kind
        self.limit = int(limit)
        self.actual = int(actual)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "limit": self.limit, "actual": self.actual,
                "message": str(self)}

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
    """A concrete dated instance of a Meeting (recurrence expanded in code).

    ``source``/``provenance``/``term_assumed`` carry WHERE the date came from.
    Banner only exposes a weekly meeting pattern, so its recurrence is a
    WHOLE-TERM INFERENCE (``term_assumed=True``). ICS dates are explicit
    (``term_assumed=False``). Callers must opt in to term-assumed occurrences;
    see ``combined_occurrences(..., allow_term_assumption=...)``.
    """

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
    source: str = "banner"           # "banner" | "ics"
    term_assumed: bool = False
    provenance: str = "banner_weekly_assumed"

    @property
    def course(self) -> str:
        if self.subject and self.course_number:
            return f"{self.subject}-{self.course_number}"
        return self.subject or self.course_number

    @property
    def identity(self) -> tuple[str, str, str]:
        """SOURCE-AWARE composite identity, never bare CRN equality.

        Banner: ("banner", term, crn). ICS: ("ics", uid, ""). This prevents a
        numeric Banner CRN and a same-string ICS UID from being treated as the
        same class (e.g. in conflict self-exclusion).
        """
        if self.source == "ics":
            return ("ics", str(self.crn), "")
        return ("banner", str(self.term), str(self.crn))

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
            "source": self.source,
            "term_assumed": self.term_assumed,
            "provenance": self.provenance,
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
            "source": o.source,
            "term_assumed": o.term_assumed,
            "provenance": o.provenance,
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
    ambiguous: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "found": [s.to_dict() for s in self.found],
            "missing": list(self.missing),
            "invalid": list(self.invalid),
            "duplicates": list(self.duplicates),
            "ambiguous": list(self.ambiguous),
        }


def select_crns(sections: Iterable[ClassSection], crns: str | Iterable[str],
                *, term: str | None = None) -> SelectionResult:
    """Resolve pasted CRNs against parsed sections -> found/missing/invalid.

    CRN identity is (term, crn). When a term is given, a CRN from another term
    is reported missing rather than silently matched. When the SAME CRN exists
    under more than one term and no ``term`` is supplied, it is reported as
    ``ambiguous`` and NOT selected -- the caller must disambiguate by term.
    """
    valid, invalid = parse_crns(crns)
    by_crn: dict[str, ClassSection] = {}
    terms_by_crn: dict[str, set[str]] = {}
    for s in sections:
        if term and str(s.term) != str(term):
            continue
        terms_by_crn.setdefault(s.crn, set()).add(str(s.term))
        by_crn.setdefault(s.crn, s)
    found: list[ClassSection] = []
    missing: list[str] = []
    dupes: list[str] = []
    ambiguous: list[str] = []
    seen: set[str] = set()
    for c in valid:
        if c in seen:
            dupes.append(c)
            continue
        seen.add(c)
        if term is None and len(terms_by_crn.get(c, set())) > 1:
            ambiguous.append(c)
            continue
        if c in by_crn:
            found.append(by_crn[c])
        else:
            missing.append(c)
    return SelectionResult(tuple(found), tuple(missing), tuple(invalid),
                           tuple(dupes), tuple(ambiguous))


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
    start: date | None = None,
    end: date | None = None,
    tz: ZoneInfo | None = None,
    skip_holidays: bool = True,
) -> list[ClassOccurrence]:
    """Expand a section's weekly meetings across its term window.

    The range is CLAMPED to the section term's VERIFIED ``TERM_WINDOWS`` entry:
    a caller can request a sub-window, but never beyond the term, and an
    unknown/unverified term yields NO occurrences (never a synthesized
    +120-day window). TBA meetings produce no occurrences and are surfaced by
    ``ClassSection.has_tba``. University holidays for the term are skipped.

    PARTIAL-TERM LIMITATION: a meeting pattern is expanded for the WHOLE term
    window. Banner does not expose part-of-term session start/end dates in the
    results table, so first-half/second-half courses are expanded over the full
    term (documented as a known limitation).
    """
    win = _section_window(section, start, end)
    if win is None:
        return []
    lo, hi = win
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
            for d in _weekday_dates(lo, hi, wd):
                if d in holidays:
                    continue
                out.append(ClassOccurrence(
                    term=section.term, crn=section.crn, subject=section.subject,
                    course_number=section.course_number, title=section.title,
                    date=d, start=_make_dt(d, m.begin, tz),
                    end=_make_dt(d, m.end, tz), meeting=m,
                    source="banner", term_assumed=True,
                    provenance="banner_weekly_assumed",
                ))
    out.sort(key=lambda o: (o.start, o.crn))
    return out


def term_expandability(term: str) -> tuple[bool, str | None]:
    """Whether recurrence may be expanded for a Banner term.

    Only VERIFIED windows expand. An unknown or unverified term is a typed
    unavailable -> no occurrences, never an invented default window.
    """
    tw = term_window(term)
    if tw is None:
        return False, f"term {str(term)!r} has no known term window"
    if not tw.verified:
        return False, f"term {str(term)!r} window is unverified"
    return True, None


def _section_window(
    section: ClassSection,
    start: date | None,
    end: date | None,
) -> tuple[date, date] | None:
    """Clamp a requested range to the section term's verified window."""
    tw = term_window(section.term)
    if tw is None or not tw.verified:
        return None
    lo = tw.classes_begin if start is None else max(start, tw.classes_begin)
    hi = tw.classes_end if end is None else min(end, tw.classes_end)
    if lo > hi:
        return None
    return lo, hi


def expand_sections(
    sections: Iterable[ClassSection],
    *,
    start: date | None = None,
    end: date | None = None,
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
    max_conflicts: int = MAX_CONFLICTS,
) -> list[Conflict]:
    """Overlapping occurrences (half-open: end == start is NOT a conflict).

    Same-identity exclusions use the SOURCE-AWARE composite identity (Banner
    term+CRN vs ICS UID), never bare CRN equality. The scan stops as soon as
    the conflict cap is exceeded and raises ``BoundsExceeded`` instead of
    materializing an unbounded O(n^2) list.
    """
    items = sorted(occurrences, key=lambda o: (o.start, o.end, o.crn))
    out: list[Conflict] = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if b.start >= a.end:
                break
            if b.start < a.end and a.start < b.end:
                if not include_same_crn and a.identity == b.identity:
                    continue
                overlap = (min(a.end, b.end) - max(a.start, b.start)).total_seconds() / 60
                if overlap > 0:
                    out.append(Conflict(a, b, overlap))
                    if len(out) > max_conflicts:
                        raise BoundsExceeded("conflicts", max_conflicts, len(out))
    return out


def schedule_conflicts(
    sections: Iterable[ClassSection],
    *,
    start: date | None = None,
    end: date | None = None,
    tz: ZoneInfo | None = None,
    ics_events: Iterable[IcsEvent] | None = None,
    allow_term_assumption: bool = False,
) -> list[Conflict]:
    occ = combined_occurrences(
        sections, ics_events or (), start=start, end=end, tz=tz,
        allow_term_assumption=allow_term_assumption)
    return find_conflicts(occ)


def combined_occurrences(
    sections: Iterable[ClassSection],
    ics_events: Iterable[IcsEvent],
    *,
    start: date | None = None,
    end: date | None = None,
    tz: ZoneInfo | None = None,
    allow_term_assumption: bool = False,
    max_occurrences: int = MAX_TOTAL_OCCURRENCES,
) -> list[ClassOccurrence]:
    """All dated occurrences, GLOBALLY bounded.

    Banner sections expand to term-assumed recurrences (flagged
    ``term_assumed=True``) and are EXCLUDED unless ``allow_term_assumption=True``
    because Banner gives no section-specific start/end dates. ICS events expand
    from their own dated recurrence and are always usable. ``BoundsExceeded``
    is raised past ``max_occurrences`` so a huge schedule is refused, not
    materialized.
    """
    out: list[ClassOccurrence] = []
    for s in sections:
        for o in expand_section(s, start=start, end=end, tz=tz):
            if o.term_assumed and not allow_term_assumption:
                continue
            out.append(o)
            if len(out) > max_occurrences:
                raise BoundsExceeded("occurrences", max_occurrences, len(out))
    for e in ics_events:
        for o in expand_ics_event(e, start=start, end=end, tz=tz):
            out.append(o)
            if len(out) > max_occurrences:
                raise BoundsExceeded("occurrences", max_occurrences, len(out))
    out.sort(key=lambda o: (o.start, o.crn))
    return out


def _validate_buffer(buffer_min: float) -> float:
    """Finite, non-negative, bounded planning buffer. Raise on garbage."""
    try:
        b = float(buffer_min)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"buffer_min must be a number, got {buffer_min!r}") from exc
    if not math.isfinite(b):
        raise ValueError("buffer_min must be finite")
    if b < 0:
        raise ValueError("buffer_min must be >= 0")
    if b > MAX_BUFFER_MIN:
        raise ValueError(f"buffer_min {b:g} exceeds MAX_BUFFER_MIN {MAX_BUFFER_MIN:g}")
    return b


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
    ics_events: Iterable[IcsEvent] | None = None,
    allow_term_assumption: bool = False,
) -> NextClass | None:
    """The next timed class meeting at/after ``at`` -> deterministic deadline.

    ``deadline`` (leave_by) = class start - ``buffer_min``. Banner sections are
    clamped to their verified term window and are TERM-ASSUMED whole-term
    inferences, so they are excluded unless ``allow_term_assumption=True``. ICS
    events are dated and always usable. TBA meetings are ignored here (no time
    to schedule). Raises ``BoundsExceeded`` when the occurrence cap is hit.
    """
    b = _validate_buffer(buffer_min)
    tz = tz or campus_tz()
    if at.tzinfo is None:
        at = at.replace(tzinfo=tz)
    occ = combined_occurrences(
        list(sections), list(ics_events or ()), start=start, end=end, tz=tz,
        allow_term_assumption=allow_term_assumption)
    for o in occ:
        if o.start < at:
            continue
        if skip_online and o.meeting.is_online:
            continue
        return NextClass(
            occurrence=o,
            minutes_until=(o.start - at).total_seconds() / 60,
            leave_by=o.start - timedelta(minutes=b),
            buffer_min=b,
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
    ics_events: Iterable[IcsEvent] | None = None,
    allow_term_assumption: bool = False,
) -> dict:
    b = _validate_buffer(buffer_min)
    tz = tz or campus_tz()
    if at.tzinfo is None:
        at = at.replace(tzinfo=tz)
    section_list = list(sections)
    ics_list = list(ics_events or ())
    has_timed_banner = any(
        m.has_time for s in section_list for m in s.meetings)
    assumed_excluded = bool(section_list) and has_timed_banner and (
        not allow_term_assumption)
    try:
        nc = next_class(section_list, at, start=start, end=end, buffer_min=b,
                        skip_online=skip_online, tz=tz, ics_events=ics_list,
                        allow_term_assumption=allow_term_assumption)
    except BoundsExceeded as exc:
        return {
            "schema": SCHEMA_NEXT_CLASS,
            "status": "bounds_exceeded",
            "at": at.isoformat(timespec="seconds"),
            "deadline": None,
            "buffer_min": b,
            "reason": str(exc),
            "bounds": exc.to_dict(),
            "allow_term_assumption": allow_term_assumption,
        }
    if nc is None:
        unverified = _dedupe(
            r for s in section_list
            for ok, r in [term_expandability(s.term)] if not ok and r)
        if assumed_excluded and not ics_list:
            status = "recurrence_unavailable"
            reason = (
                "Banner meeting patterns are whole-term inferences and do "
                "not carry section-specific dates; pass "
                "allow_term_assumption=True to use them, or use ICS for "
                "dated occurrences.")
        elif section_list and not ics_list and unverified:
            status = "unavailable"
            reason = ("; ".join(unverified) + ". Only verified TERM_WINDOWS "
                      "expand; no fallback window is invented.")
        else:
            status = "none"
            reason = ("no timed class meeting found in the requested window; "
                      "check TBA meetings with section.flags().has_tba")
        return {
            "schema": SCHEMA_NEXT_CLASS,
            "status": status,
            "at": at.isoformat(timespec="seconds"),
            "deadline": None,
            "buffer_min": b,
            "reason": reason,
            "unverified_terms": unverified,
            "allow_term_assumption": allow_term_assumption,
            "term_assumption_required": assumed_excluded,
        }
    payload = {"schema": SCHEMA_NEXT_CLASS, "at": at.isoformat(timespec="seconds"),
               "allow_term_assumption": allow_term_assumption}
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
        "kind": "crn",
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


def _ics_schedule_record(event: IcsEvent, snapshot_id: str = "") -> dict:
    """A stored ICS entry keeps DATED/RECURRENCE semantics (uid identity).

    It is intentionally NOT a ClassSection: an ICS event has no (term, CRN) and
    must never be treated as a weekly Banner meeting.
    """
    rec = event.to_dict()
    rec.update({"kind": "ics", "term": "", "snapshot_id": snapshot_id})
    return rec


def add_to_schedule(
    schedule: Iterable[dict],
    sections: Iterable[ClassSection] | None = None,
    crns: str | Iterable[str] | None = None,
    *,
    term: str | None = None,
    snapshot_id: str = "",
    ics_events: Iterable[IcsEvent] | None = None,
) -> dict:
    """Add selected Banner CRNs and/or ICS events to a local schedule list.

    Two identity spaces, kept separate on purpose:
      * Banner records use ``kind="crn"`` and identity ``(term, crn)``.
      * ICS records use ``kind="ics"`` and identity ``uid``.
    Returns a JSON-ready dict; the caller persists it (no DB here).
    """
    current = list(schedule or [])
    have_crn = {(str(r.get("term", "")), str(r.get("crn", "")))
                for r in current if r.get("kind", "crn") == "crn"}
    have_uid = {str(r.get("uid", "")) for r in current if r.get("kind") == "ics"}
    added: list[dict] = []
    already: list[str] = []
    missing: list[str] = []
    invalid: list[str] = []
    ambiguous: list[str] = []

    if crns is not None:
        sel = select_crns(sections or (), crns, term=term)
        already.extend(sel.duplicates)
        missing.extend(sel.missing)
        invalid.extend(sel.invalid)
        ambiguous.extend(sel.ambiguous)
        for s in sel.found:
            key = (s.term, s.crn)
            if key in have_crn:
                already.append(s.crn)
                continue
            have_crn.add(key)
            rec = _schedule_record(s, snapshot_id)
            current.append(rec)
            added.append(rec)

    for event in (ics_events or ()):
        uid = str(event.uid)
        if uid in have_uid:
            already.append(uid)
            continue
        have_uid.add(uid)
        rec = _ics_schedule_record(event, snapshot_id)
        current.append(rec)
        added.append(rec)

    return {
        "schema": SCHEMA_SCHEDULE,
        "schedule": current,
        "added": added,
        "missing": list(missing),
        "invalid": list(invalid),
        "ambiguous": _dedupe(ambiguous),
        "already_in_schedule": _dedupe(already),
    }


def remove_from_schedule(
    schedule: Iterable[dict],
    crns: str | Iterable[str] | None = None,
    *,
    term: str | None = None,
    uids: str | Iterable[str] | None = None,
) -> dict:
    """Remove Banner CRNs and/or ICS UIDs, preserving composite identity.

    Banner CRNs are only unique WITHIN a term, so a CRN that appears under more
    than one term in the schedule is AMBIGUOUS unless ``term`` is supplied; it
    is reported in ``ambiguous`` and left in place rather than silently removed
    from every term.
    """
    valid, invalid = parse_crns(crns) if crns is not None else ([], [])
    want_crns = set(valid)
    want_term = str(term) if term is not None else None
    want_uids = set()
    if uids is not None:
        raw = ([uids] if isinstance(uids, str) else list(uids))
        want_uids = {str(u).strip() for u in raw if str(u).strip()}

    # (crn -> set of terms present) for Banner records.
    terms_by_crn: dict[str, set[str]] = {}
    for rec in schedule or []:
        if rec.get("kind", "crn") != "crn":
            continue
        terms_by_crn.setdefault(str(rec.get("crn", "")), set()).add(
            str(rec.get("term", "")))

    kept: list[dict] = []
    removed: list[str] = []
    ambiguous: list[str] = []
    for rec in schedule or []:
        kind = rec.get("kind", "crn")
        if kind == "ics":
            if str(rec.get("uid", "")) in want_uids:
                removed.append(str(rec.get("uid", "")))
            else:
                kept.append(rec)
            continue
        crn = str(rec.get("crn", ""))
        rec_term = str(rec.get("term", ""))
        if crn not in want_crns:
            kept.append(rec)
            continue
        if want_term is not None:
            if rec_term == want_term:
                removed.append(crn)
            else:
                kept.append(rec)
            continue
        if len(terms_by_crn.get(crn, set())) > 1:
            ambiguous.append(crn)          # do NOT remove an ambiguous CRN
            kept.append(rec)
            continue
        removed.append(crn)

    ambiguous_set = set(ambiguous)
    not_in = [c for c in valid if c not in removed and c not in ambiguous_set]
    for u in sorted(want_uids):
        if u not in removed:
            not_in.append(u)
    return {
        "schema": SCHEMA_SCHEDULE,
        "schedule": kept,
        "removed": _dedupe(removed),
        "not_in_schedule": not_in,
        "ambiguous": _dedupe(ambiguous),
        "invalid": invalid,
    }


def resolve_schedule(
    schedule: Iterable[dict],
    sections: Iterable[ClassSection],
) -> list[ClassSection]:
    """Join stored Banner records back to freshly parsed sections by (term, crn).

    ICS records are ignored here -- use ``schedule_occurrences`` so they are
    consumed as expanded dated occurrences, never as Banner sections.
    """
    by_key = {(s.term, s.crn): s for s in sections}
    out: list[ClassSection] = []
    for rec in schedule or []:
        if rec.get("kind", "crn") != "crn":
            continue
        s = by_key.get((str(rec.get("term", "")), str(rec.get("crn", ""))))
        if s is not None:
            out.append(s)
    return out


def resolve_ics_schedule(schedule: Iterable[dict]) -> list[IcsEvent]:
    """Rebuild stored ICS entries into IcsEvents (dated recurrence preserved).

    Reconstruction goes through ``parse_ics`` on a one-event calendar so the
    stored RRULE/DTSTART are re-validated instead of trusted blindly.
    """
    out: list[IcsEvent] = []
    for rec in schedule or []:
        if rec.get("kind") != "ics":
            continue
        text = _ics_record_to_text(rec)
        parsed = parse_ics(text)
        if parsed.events:
            out.append(parsed.events[0])
    return out


def _ics_record_to_text(rec: dict) -> str:
    """Serialize one stored ICS record back to a minimal VEVENT calendar.

    Uses the RAW DTSTART/DTEND values (not the ISO projection) plus the TZID so
    re-parsing re-validates the same recurrence rather than trusting the store.
    """
    tzid = rec.get("tzid")

    def dt_line(name: str, raw: str) -> str:
        raw = str(raw or "")
        if (tzid and len(raw) == 15 and "T" in raw and not raw.endswith("Z")):
            return f"{name};TZID={tzid}:{raw}"
        return f"{name}:{raw}"

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT",
             f"UID:{_escape_ics_text(str(rec.get('uid', '')))}",
             dt_line("DTSTART", rec.get("raw_start", "")),
             dt_line("DTEND", rec.get("raw_end", ""))]
    if rec.get("summary"):
        lines.append("SUMMARY:" + _escape_ics_text(str(rec["summary"])))
    if rec.get("location"):
        lines.append("LOCATION:" + _escape_ics_text(str(rec["location"])))
    rrule = rec.get("rrule") or {}
    if rrule and rec.get("recurring"):
        parts = [f"{k}={v}" for k, v in rrule.items() if not k.startswith("_")]
        if parts:
            lines.append("RRULE:" + ";".join(parts))
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\n".join(lines)


def schedule_occurrences(
    schedule: Iterable[dict],
    sections: Iterable[ClassSection] | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    tz: ZoneInfo | None = None,
    allow_term_assumption: bool = False,
) -> list[ClassOccurrence]:
    """Dated occurrences for a stored schedule: Banner sections + ICS events.

    Banner (term-assumed) recurrences are excluded unless
    ``allow_term_assumption=True``; ICS dated occurrences are always included.
    """
    resolved = resolve_schedule(schedule, sections or ())
    ics = resolve_ics_schedule(schedule)
    return combined_occurrences(resolved, ics, start=start, end=end, tz=tz,
                                allow_term_assumption=allow_term_assumption)


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
# Final exams: NOT SUPPORTED
# ---------------------------------------------------------------------------
# The public HZSKEXAM page is a date x time matrix with year-less column
# headers. A previous "best effort" parser guessed the year and dropped codes
# with no header context, which risked returning WRONG exam dates. There is no
# exam parser and no raw-capture option -- exam data is out of scope here.


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
        """Display-only projection. NOT a recurrence source: use
        ``expand_ics_event`` for dated occurrences."""
        b = None if self.all_day else self.dtstart.strftime("%H:%M")
        e = None if self.all_day else self.dtend.strftime("%H:%M")
        meeting = parse_location(self.location)
        return replace(meeting, days=self.days if not self.all_day else (),
                       begin=b, end=e)

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "summary": self.summary,
            "location": self.location,
            "dtstart": self.dtstart.isoformat(timespec="seconds"),
            "dtend": self.dtend.isoformat(timespec="seconds"),
            "raw_start": self.raw_start,
            "raw_end": self.raw_end,
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
    """Return (aware dt, all_day, tzid). Raises ValueError on a malformed value.

    ``VALUE=DATE`` requires EXACTLY 8 digits (YYYYMMDD); anything else is
    rejected rather than truncated into a plausible date.
    """
    value = value.strip()
    value_type = params.get("VALUE", "").upper()
    tzid = params.get("TZID")
    if value_type == "DATE":
        if not re.fullmatch(r"\d{8}", value):
            raise ValueError(f"VALUE=DATE requires exactly YYYYMMDD, got {value!r}")
        d = datetime.strptime(value, "%Y%m%d")
        return d.replace(tzinfo=default_tz), True, tzid
    if value_type not in ("", "DATE-TIME"):
        raise ValueError(f"unsupported DTSTART/DTEND VALUE={value_type!r}")
    m = re.match(r"^(\d{8})T(\d{6})(Z?)$", value)
    if m:
        base = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        if m.group(3) == "Z":
            return base.replace(tzinfo=timezone.utc).astimezone(default_tz), False, tzid
        if tzid:
            try:
                return base.replace(tzinfo=ZoneInfo(tzid)), False, tzid
            except ZoneInfoNotFoundError as exc:
                # Unknown TZID is REJECTED, never silently shifted to campus time.
                raise ValueError(f"unknown TZID {tzid!r}") from exc
        warnings.append("floating time with no TZID; assuming campus time")
        return base.replace(tzinfo=default_tz), False, tzid
    # A bare 8-digit value is a DATE only when no VALUE parameter was given.
    if value_type == "" and re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value, "%Y%m%d")
        return d.replace(tzinfo=default_tz), True, tzid
    raise ValueError(f"unsupported DTSTART/DTEND value {value!r}")


_RRULE_SUPPORTED = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY"}


def _parse_rrule(value: str, dtstart_weekday: int,
                 warnings: list[str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse a WEEKLY RRULE, flagging anything unsupported instead of guessing.

    A flagged rule carries ``_UNSUPPORTED`` and the event is skipped by the
    caller, so a bad/unsupported rule can never produce invented recurrence.
    """
    rule: dict[str, str] = {}
    for part in value.split(";"):
        if not part:
            continue
        k, _, v = part.partition("=")
        k = k.strip().upper()
        v = v.strip().upper()
        if k not in _RRULE_SUPPORTED:
            warnings.append(f"unsupported RRULE part {k!r}; event flagged")
            rule["_UNSUPPORTED"] = k
            continue
        rule[k] = v

    freq = rule.get("FREQ", "")
    if freq != "WEEKLY":
        warnings.append(f"unsupported RRULE FREQ={freq or '?'} (only WEEKLY)")
        rule["_UNSUPPORTED"] = "FREQ"
        return rule, ()

    if "INTERVAL" in rule:
        raw = rule["INTERVAL"]
        if not raw.isdigit() or int(raw) < 1:
            warnings.append(f"invalid RRULE INTERVAL={raw!r} (positive integer)")
            rule["_UNSUPPORTED"] = "INTERVAL"
            return rule, ()

    if "COUNT" in rule:
        raw = rule["COUNT"]
        if not raw.isdigit() or int(raw) < 1 or int(raw) > MAX_ICS_OCCURRENCES:
            warnings.append(
                f"invalid RRULE COUNT={raw!r} (1..{MAX_ICS_OCCURRENCES})")
            rule["_UNSUPPORTED"] = "COUNT"
            return rule, ()

    if "UNTIL" in rule:
        try:
            _parse_ics_dt(rule["UNTIL"], {}, campus_tz(), [])
        except ValueError:
            warnings.append(f"invalid RRULE UNTIL={rule['UNTIL']!r}")
            rule["_UNSUPPORTED"] = "UNTIL"
            return rule, ()

    byday = rule.get("BYDAY", "")
    days: list[str] = []
    if byday:
        for tok in byday.split(","):
            tok = tok.strip().upper()
            if not tok or not tok.isalpha() or tok not in ICS_DAY_TO_BANNER:
                warnings.append(
                    f"unsupported RRULE BYDAY token {tok!r} (plain MO..SU)")
                rule["_UNSUPPORTED"] = "BYDAY"
                return rule, ()
            b = ICS_DAY_TO_BANNER[tok]
            if b not in days:
                days.append(b)
    if not days:
        days = [WEEKDAY_TO_DAY[dtstart_weekday]]
    return rule, tuple(sorted(days, key=lambda d: DAY_TO_WEEKDAY[d]))


def _decode_ics_text(value: str) -> str:
    """Decode RFC 5545 TEXT escapes (\\n, \\N, \\, \\; \\\\).

    An unknown escape drops the backslash and keeps the character; it never
    raises. This is applied to UID/SUMMARY/LOCATION only (the values a UI shows
    or matches on) -- not to DTSTART/RRULE, which are parsed structurally.
    """
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _escape_ics_text(value: str) -> str:
    """Encode a value for an RFC 5545 TEXT property (inverse of decode).

    Backslash FIRST, then CR/LF -> ``\\n``, then comma/semicolon. This is what
    makes a schedule record round-trip through storage without a comma or a
    newline corrupting the property structure (or injecting a fake line).
    """
    out = str(value).replace("\\", "\\\\")
    out = out.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    out = out.replace(",", "\\,").replace(";", "\\;")
    return out


def parse_ics(text: str, *, tz: ZoneInfo | None = None) -> IcsParse:
    """Parse an ICS calendar into a bounded, useful VEVENT subset.

    Supported: VCALENDAR/VEVENT, line unfolding, DTSTART/DTEND (DATE, UTC 'Z',
    TZID, or floating->campus warning), SUMMARY, LOCATION, UID, and weekly
    RRULE (FREQ=WEEKLY with BYDAY/INTERVAL/COUNT/UNTIL). Unsupported or
    malformed parts are reported in ``warnings``/``errors`` and the event is
    skipped -- never guessed. Input bytes/lines/events and recurrence counts are
    bounded (``MAX_ICS_*``); unknown TZIDs and RDATE/EXDATE/WKST are rejected.
    """
    raw = text or ""
    if len(raw.encode("utf-8", "replace")) > MAX_ICS_BYTES:
        return IcsParse((), (), (f"ICS exceeds {MAX_ICS_BYTES} bytes; rejected",),
                        valid=False)
    default_tz = tz or campus_tz()
    lines = unfold_ics(raw)
    if len(lines) > MAX_ICS_LINES:
        return IcsParse((), (), (f"ICS exceeds {MAX_ICS_LINES} lines; rejected",),
                        valid=False)

    warnings: list[str] = []
    errors: list[str] = []
    events: list[IcsEvent] = []

    if not any(line.strip().upper() == "BEGIN:VCALENDAR" for line in lines):
        return IcsParse((), (), ("not an ICS calendar: missing BEGIN:VCALENDAR",),
                        valid=False)
    if not any(line.strip().upper() == "END:VCALENDAR" for line in lines):
        errors.append("calendar is truncated: missing END:VCALENDAR")

    in_event = False
    sub_depth = 0
    sub_name = ""
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
            sub_depth = 0
            sub_name = ""
            props = {}
            event_warnings = []
            continue
        if upper == "END:VEVENT":
            if not in_event:
                errors.append("END:VEVENT with no BEGIN:VEVENT")
                continue
            if sub_depth > 0:
                errors.append(
                    f"VEVENT has unterminated sub-component {sub_name!r}; "
                    "event discarded")
                in_event = False
                sub_depth = 0
                sub_name = ""
                continue
            in_event = False
            if len(events) >= MAX_ICS_EVENTS:
                errors.append(
                    f"more than {MAX_ICS_EVENTS} VEVENTs; remaining skipped")
                continue
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
        # Nested components (VALARM, STANDARD, ...) are TRACKED. Their inner
        # properties are ignored so a malicious ``SUMMARY``/``DTSTART`` inside
        # a VALARM can never overwrite a VEVENT property.
        if name == "BEGIN":
            sub_depth += 1
            sub_name = value.strip().upper() or sub_name
            warnings.append(f"ignored sub-component {sub_name}")
            continue
        if name == "END":
            if sub_depth > 0:
                sub_depth -= 1
                if sub_depth == 0:
                    sub_name = ""
            else:
                warnings.append(f"END:{value.strip()} with no matching BEGIN")
            continue
        if sub_depth > 0:
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
    # RDATE/EXDATE change the date set in ways this bounded parser does not
    # model; reject rather than silently dropping the exceptions.
    if "RDATE" in props:
        return None, "RDATE recurrence is unsupported; event skipped"
    if "EXDATE" in props:
        return None, "EXDATE exceptions are unsupported; event skipped"
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

    uid = _decode_ics_text(props["UID"][1].strip())
    summary = _decode_ics_text(props.get("SUMMARY", ({}, ""))[1].strip())
    location = _decode_ics_text(props.get("LOCATION", ({}, ""))[1].strip())
    days: tuple[str, ...] = ()
    rrule: dict[str, str] = {}
    recurring = False
    if "RRULE" in props:
        rrule, days = _parse_rrule(props["RRULE"][1], dtstart.weekday(), warnings)
        recurring = True
        if "_UNSUPPORTED" in rrule:
            return None, f"unsupported RRULE for UID {uid!r}; event skipped"
    elif not all_day:
        # A single VEVENT with no RRULE is ONE class meeting, not a weekly one.
        days = (WEEKDAY_TO_DAY[dtstart.weekday()],)
    if all_day:
        warnings.append("all-day event: no class time to expand")
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
    """Expand a supported IcsEvent's DATED weekly recurrence into occurrences.

    Respects INTERVAL/COUNT/UNTIL and BYDAY. All-day or unsupported events
    yield nothing (they are flagged by the parser instead). The window and the
    occurrence count are bounded (``MAX_ICS_WINDOW_DAYS`` /
    ``MAX_ICS_OCCURRENCES``) and the function never raises: a malformed event
    returns [] rather than crashing the planner.
    """
    try:
        return _expand_ics_event_impl(event, start=start, end=end, tz=tz)
    except Exception:                                    # noqa: BLE001
        return []


def _expand_ics_event_impl(event: IcsEvent, *, start: date | None = None,
                           end: date | None = None,
                           tz: ZoneInfo | None = None) -> list[ClassOccurrence]:
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
            source="ics", term_assumed=False, provenance="ics_dated",
        )]

    interval = int(rrule.get("INTERVAL", "1") or 1)
    if interval < 1:
        return []
    count = int(rrule["COUNT"]) if rrule.get("COUNT", "").isdigit() else None
    # UNTIL is an INSTANT, not a date: keep the aware datetime and compare
    # candidate start instants, so a same-day-but-later UNTIL is respected.
    until_dt: datetime | None = None
    if rrule.get("UNTIL"):
        try:
            until_dt, _, _ = _parse_ics_dt(rrule["UNTIL"], {}, tz, [])
        except ValueError:
            return []                     # parser flags invalid UNTIL already
    win_start = max(start or first, first)
    default_end = first + timedelta(days=MAX_ICS_WINDOW_DAYS)
    win_end = end or (until_dt.date() if until_dt else default_end)
    if until_dt:
        win_end = min(win_end, until_dt.date())
    if (win_end - win_start).days > MAX_ICS_WINDOW_DAYS:
        win_end = win_start + timedelta(days=MAX_ICS_WINDOW_DAYS)

    # Iterate week-by-week so INTERVAL is honored exactly. COUNT counts every
    # recurrence from DTSTART (RFC 5545), so occurrences before the query
    # window still consume the count instead of being re-issued later.
    out: list[ClassOccurrence] = []
    n = 0
    week = first - timedelta(days=first.weekday())
    while week - timedelta(days=6) <= win_end:
        for day_letter in event.days:
            wd = DAY_TO_WEEKDAY.get(day_letter)
            if wd is None:
                continue
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
            if until_dt is not None and start_dt > until_dt:
                # UNTIL is inclusive: the first candidate AFTER the instant ends
                # the series (all later candidates are later too).
                out.sort(key=lambda o: o.start)
                return out
            out.append(ClassOccurrence(
                term="", crn=event.uid, subject="", course_number="",
                title=event.summary, date=d, start=start_dt,
                end=start_dt + duration, meeting=event.to_meeting(),
                title_extra="ics",
                source="ics", term_assumed=False, provenance="ics_dated",
            ))
            if len(out) >= MAX_ICS_OCCURRENCES:
                out.sort(key=lambda o: o.start)
                return out
            if count is not None and n >= count:
                out.sort(key=lambda o: o.start)
                return out
        week += timedelta(days=7 * interval)
    out.sort(key=lambda o: o.start)
    return out


def expand_ics_events(events: Iterable[IcsEvent], *, start: date | None = None,
                      end: date | None = None,
                      tz: ZoneInfo | None = None) -> list[ClassOccurrence]:
    """Expand many IcsEvents into a single sorted occurrence list."""
    out: list[ClassOccurrence] = []
    for e in events:
        out.extend(expand_ics_event(e, start=start, end=end, tz=tz))
    out.sort(key=lambda o: (o.start, o.crn))
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
    content_sha1: str = ""

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
            "content_sha1": self.content_sha1,
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
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    content_sha1 = hashlib.sha1((html or "").encode("utf-8")).hexdigest()
    digest_src = json.dumps({
        "term": str(term),
        "query": safe_query,
        "fetched_at": fetched.isoformat(timespec="seconds"),
        "content_sha1": content_sha1,
    }, sort_keys=True, separators=(",", ":"))
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
        "content_sha1": content_sha1,
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
    """Read and VALIDATE a snapshot JSON envelope (see scripts/fetch_classes.py).

    Rejects a missing/invalid ``fetched_at`` (a snapshot with no trustworthy
    capture time cannot be labelled fresh/stale) and re-sanitizes the stored
    query so a hand-edited file cannot smuggle a forbidden key back in. If a
    content hash is present it is verified against the HTML.
    """
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA_SNAPSHOT:
        raise ValueError(f"{p}: not a {SCHEMA_SNAPSHOT} snapshot")
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        raise ValueError(f"{p}: missing fetched_at")
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError as exc:
        raise ValueError(f"{p}: bad fetched_at {fetched_at!r}") from exc
    if fetched.tzinfo is None:
        raise ValueError(f"{p}: fetched_at must include a timezone offset")
    html = str(data.get("html", ""))
    content_sha1 = str(data.get("content_sha1", "") or "")
    actual_sha1 = hashlib.sha1(html.encode("utf-8")).hexdigest()
    if content_sha1 and content_sha1 != actual_sha1:
        raise ValueError(f"{p}: content_sha1 does not match html")
    return TimetableSnapshot(
        term=str(data.get("term", "")),
        html=html,
        query=sanitize_query(dict(data.get("query", {}) or {})),
        fetched_at=fetched,
        source_url=str(data.get("source_url", BANNER_PROC_URL)),
        snapshot_id=str(data.get("id", p.stem)),
        campus=str(data.get("campus", DEFAULT_CAMPUS)),
        schema=str(data.get("schema", SCHEMA_SNAPSHOT)),
        content_sha1=content_sha1 or actual_sha1,
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
    "recurrence_unavailable": "Banner recurrence is a whole-term inference; "
                            "pass allow_term_assumption or import ICS",
    "bounds_exceeded": "combined schedule exceeded the occurrence/conflict caps; "
                      "narrow the window or selection",
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
    sections: Iterable[ClassSection] | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    now: datetime | None = None,
    max_age_s: float = 6 * 3600,
    snapshot: TimetableSnapshot | None = None,
    ics_events: Iterable[IcsEvent] | None = None,
    allow_term_assumption: bool = False,
) -> dict:
    """Schedule response with conflict + stale + bounds + term-provenance info.

    Conflicts come from DATED occurrences. Banner recurrences are whole-term
    inferences and are excluded unless ``allow_term_assumption=True``; ICS
    dated occurrences are always used. A schedule that exceeds the global
    occurrence/conflict caps returns ``state="bounds_exceeded"`` instead of a
    partial or unbounded result.
    """
    schedule_list = list(schedule or [])
    section_list = list(sections or ())
    ics_list = list(ics_events or ())
    resolved = resolve_schedule(schedule_list, section_list)
    resolved_ics = resolve_ics_schedule(schedule_list)
    if ics_list:
        by_uid = {e.uid: e for e in ics_list}
        resolved_ics = [by_uid.get(e.uid, e) for e in resolved_ics]

    stale = snapshot_is_stale(snapshot, max_age_s=max_age_s,
                              now=now) if snapshot else False
    known_crn = {(s.term, s.crn) for s in section_list}
    known_uid = {e.uid for e in (ics_list or resolved_ics)}
    unresolved: list[str] = []
    for rec in schedule_list:
        if rec.get("kind", "crn") == "ics":
            uid = str(rec.get("uid", ""))
            if uid not in known_uid:
                unresolved.append(uid)
        else:
            key = (str(rec.get("term", "")), str(rec.get("crn", "")))
            if key not in known_crn:
                unresolved.append(str(rec.get("crn", "")))

    has_timed_banner = any(m.has_time for s in resolved for m in s.meetings)
    term_assumption_required = has_timed_banner and not allow_term_assumption
    base = {
        "schema": SCHEMA_SCHEDULE,
        "schedule": schedule_list,
        "count": len(schedule_list),
        "unresolved": unresolved,
        "snapshot": snapshot.meta_dict() if snapshot else None,
        "allow_term_assumption": allow_term_assumption,
        "term_assumption_required": term_assumption_required,
    }
    try:
        occ = combined_occurrences(
            resolved, resolved_ics, start=start, end=end,
            allow_term_assumption=allow_term_assumption)
        conflicts = find_conflicts(occ)
    except BoundsExceeded as exc:
        return {**base, "occurrence_count": None, "conflicts": [],
                "state": "bounds_exceeded", "bounds": exc.to_dict(),
                "reason": str(exc)}
    if conflicts:
        state = "conflict"
    elif term_assumption_required and not resolved_ics:
        state = "recurrence_unavailable"
    elif stale:
        state = "stale_snapshot"
    else:
        state = "ready"
    return {**base, "occurrence_count": len(occ),
            "conflicts": [c.to_dict() for c in conflicts], "state": state}


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
    "DEFAULT_CAMPUS", "DEFAULT_CORE_CODE",
    "SCHEMA_SNAPSHOT", "SCHEMA_SEARCH", "SCHEMA_NEXT_CLASS", "SCHEMA_SCHEDULE",
    "MAX_ICS_BYTES", "MAX_ICS_LINES", "MAX_ICS_EVENTS", "MAX_ICS_OCCURRENCES",
    "MAX_ICS_WINDOW_DAYS", "MAX_BUFFER_MIN", "MAX_TOTAL_OCCURRENCES",
    "MAX_CONFLICTS", "BoundsExceeded",
    "TERM_FALL_2026", "SUPPORTED_TERMS", "TERM_WINDOWS", "HOLIDAYS",
    "TermWindow", "term_name", "term_window", "term_expandability",
    "Meeting", "ClassSection",
    "ClassOccurrence", "TimetableParse", "Conflict", "NextClass", "Building",
    "IcsEvent", "IcsParse", "SelectionResult", "TimetableSnapshot",
    "DAY_TO_WEEKDAY", "WEEKDAY_TO_DAY", "ICS_DAY_TO_BANNER", "BANNER_DAY_TO_ICS",
    "parse_days", "days_to_text", "parse_clock", "parse_location",
    "parse_course_label", "is_valid_crn", "parse_crns", "extract_table_rows",
    "parse_timetable_html", "search", "select_crns", "expand_section",
    "expand_sections", "campus_tz", "find_conflicts", "schedule_conflicts",
    "combined_occurrences", "next_class", "next_class_json", "add_to_schedule",
    "remove_from_schedule", "resolve_schedule", "resolve_ics_schedule",
    "schedule_occurrences", "parse_building_list_html", "load_buildings",
    "building_crosswalk", "attach_gis_coords", "crosswalk_contract",
    "unfold_ics", "parse_ics", "expand_ics_event", "expand_ics_events",
    "make_snapshot", "sanitize_query", "QUERY_KEYS",
    "FORBIDDEN_QUERY_KEYS", "save_snapshot", "load_snapshot",
    "snapshot_age_seconds", "snapshot_is_stale", "snapshot_sections",
    "UI_STATES", "search_state", "search_result_json", "schedule_json",
    "section_state",
]