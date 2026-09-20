"""Request-time course search: live Banner first, committed snapshot offline.

`hokieday/classes.py` parses and filters Banner HTML but never touches the
network (the package is stdlib-only and AST-checked). This module is the
request-time boundary -- the counterpart to `app/gemini_provider.py` -- and is
the only place that talks to Banner while serving a request;
`scripts/fetch_classes.py` remains the deliberate capture tool.

Rules this module keeps:

* PUBLIC DATA ONLY. The public timetable form, no credentials, no cookies, and
  never HokieSPA / My VT. No instructor name or student identity is stored.
* One request per uncached query, and at least ``MIN_INTERVAL_S`` between them,
  with the project User-Agent so an administrator can identify the client.
* Live captures are kept under ``cache/`` (gitignored) with their own
  ``fetched_at``; the committed snapshots in ``fixtures/`` are never written.
* Under ``DEMO_MODE=cache`` nothing goes to the network: the committed snapshot
  answers, labelled with its capture time, and no snapshot means a typed
  unavailable result rather than an invented timetable.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from hokieday import classes, config

TERM_DEFAULT = classes.TERM_FALL_2026
LIVE_TTL_S = 600.0          # one Banner call serves repeat searches for 10 min
MIN_INTERVAL_S = 2.0        # politeness between live requests
REQUEST_TIMEOUT_S = 30
MAX_RESULTS = 50
DEFAULT_MAX_AGE_S = 6 * 3600

_LOCK = threading.Lock()
_MEMO: dict[str, tuple[float, str, str]] = {}   # key -> (monotonic, html, fetched_at)
_LAST_CALL = [0.0]


# ---------------------------------------------------------------------------
# Snapshots (offline truth)
# ---------------------------------------------------------------------------

def snapshot_paths() -> list[Path]:
    """Committed Banner captures, newest filename first (ids are not ordered)."""
    return sorted(config.FIXTURES_DIR.glob("classes_snapshot_*.json"))


def latest_snapshot(term: str | None = None) -> classes.TimetableSnapshot | None:
    """Newest committed capture for `term` (or any term when it is None)."""
    best: classes.TimetableSnapshot | None = None
    for path in snapshot_paths():
        try:
            snapshot = classes.load_snapshot(path)
        except (ValueError, OSError):
            continue
        if term and snapshot.term != str(term):
            continue
        if best is None or snapshot.fetched_at > best.fetched_at:
            best = snapshot
    return best


def snapshot_terms() -> list[str]:
    terms = set()
    for path in snapshot_paths():
        try:
            terms.add(classes.load_snapshot(path).term)
        except (ValueError, OSError):
            continue
    return sorted(terms)


# ---------------------------------------------------------------------------
# Live Banner (public timetable POST)
# ---------------------------------------------------------------------------

def form_data(*, term: str, subject: str = "", course_number: str = "",
              crn: str = "", campus: str = classes.DEFAULT_CAMPUS,
              open_only: bool = False) -> dict:
    """The public form fields, byte-identical to the capture script's.

    CORE_CODE is required: omitting it makes Banner return the search form
    instead of results.
    """
    return {
        "CAMPUS": str(campus),
        "TERMYEAR": str(term),
        "CORE_CODE": classes.DEFAULT_CORE_CODE,
        "subj_code": str(subject or ""),
        "SCHDTYPE": classes.DEFAULT_SCHDTYPE,
        "CRSE_NUMBER": str(course_number or ""),
        "crn": str(crn or ""),
        "open_only": "on" if open_only else "",
        "disp_comments_in": "Y",
        "sess_code": classes.DEFAULT_SESS_CODE,
        "BTN_PRESSED": "FIND class sections",
        "inst_name": "",
    }


def fetch_timetable_html(form: dict) -> str:
    body = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        classes.BANNER_PROC_URL, data=body, method="POST",
        headers={
            "User-Agent": config.USER_AGENT,
            "Referer": classes.BANNER_FORM_URL,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml",
        })
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return response.read().decode("utf-8", "replace")


def _cache_path(key: str) -> Path:
    return config.CACHE_DIR / f"classes_live_{hashlib.sha1(key.encode()).hexdigest()[:12]}.json"


def _live_snapshot(term: str, filters: dict) -> classes.TimetableSnapshot | None:
    """Capture one Banner query, or None when it could not be read.

    A live query needs a subject, course number, or CRN: Banner will not list a
    whole term on a title search, and we will not ask it to.
    """
    subject = str(filters.get("subject") or "")
    course_number = str(filters.get("course_number") or "")
    crn = str(filters.get("crn") or "")
    if not (subject or course_number or crn):
        return None
    key = f"{term}|{subject}|{course_number}|{crn}"
    now_mono = time.monotonic()
    with _LOCK:
        hit = _MEMO.get(key)
        if hit and now_mono - hit[0] < LIVE_TTL_S:
            html, fetched_at = hit[1], hit[2]
            return _snapshot_from_cache(term, filters, html, fetched_at)
        wait = MIN_INTERVAL_S - (now_mono - _LAST_CALL[0])
        _LAST_CALL[0] = now_mono + max(wait, 0.0)
    if wait > 0:
        time.sleep(min(wait, MIN_INTERVAL_S))
    try:
        html = fetch_timetable_html(form_data(
            term=term, subject=subject, course_number=course_number, crn=crn))
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    fetched_at = config.now(timezone.utc).isoformat(timespec="seconds")
    with _LOCK:
        _MEMO[key] = (time.monotonic(), html, fetched_at)
    return _snapshot_from_cache(term, filters, html, fetched_at)


def _snapshot_from_cache(term: str, filters: dict, html: str,
                         fetched_at: str) -> classes.TimetableSnapshot | None:
    """Persist a live capture under cache/ and load it back through the
    snapshot validator, so live and offline results share one code path."""
    query = {k: v for k, v in filters.items()
             if k in ("subject", "course_number", "crn", "title_contains")}
    payload = classes.make_snapshot(
        html, term=term, query=query,
        fetched_at=datetime.fromisoformat(fetched_at))
    path = _cache_path(f"{term}|{sorted(query.items())}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return classes.load_snapshot(path)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The search itself
# ---------------------------------------------------------------------------

def _unavailable(reason: str, *, term: str, query: dict) -> dict:
    return {
        "schema": classes.SCHEMA_SEARCH,
        "term": term,
        "term_name": classes.term_name(term),
        "query": query,
        "state": "unavailable",
        "count": 0,
        "sections": [],
        "source": "unavailable",
        "live": False,
        "reason": reason,
    }


def _finalize(envelope: dict, *, source: str, limit: int) -> dict:
    envelope["source"] = source
    envelope["live"] = source == "banner_live"
    envelope.setdefault("snapshot_ids", [])
    envelope["truncated"] = envelope.get("truncated", False) or \
        len(envelope.get("sections", [])) > limit
    envelope["sections"] = envelope.get("sections", [])[:limit]
    return envelope


def search(text: str = "", *, term: str | None = None, subject: str | None = None,
           course_number: str | None = None, crn: str | None = None,
           days: list[str] | None = None, limit: int = MAX_RESULTS,
           max_age_s: float = DEFAULT_MAX_AGE_S,
           now: datetime | None = None) -> dict:
    """Search the course catalog, live when possible and from a snapshot when not.

    Returns the documented `hokieday.classes.search` envelope plus `source`
    ("banner_live" or "snapshot") and `truncated`, so a caller can tell a fresh
    catalog answer from a captured one instead of guessing.
    """
    term = str(term or TERM_DEFAULT)
    query: dict = {}
    raw_text = str(text or "").strip()
    if raw_text:
        query = classes.query_filters(raw_text)
    for key, value in (("subject", subject), ("course_number", course_number),
                       ("crn", crn)):
        if value:
            query[key] = str(value)
    if days:
        query["days"] = list(days)
    filters = dict(query)

    snapshot = None if config.CACHE_ONLY else _live_snapshot(term, filters)
    if snapshot is not None:
        parse = classes.snapshot_sections(snapshot)
        hits = classes.search(parse.sections, **filters)
        envelope = classes.search_result_json(
            replace(parse, sections=hits), snapshot,
            query={"q": raw_text, **query} if raw_text else query,
            max_age_s=max_age_s, now=now)
        envelope["snapshot_ids"] = [snapshot.snapshot_id]
        return _finalize(envelope, source="banner_live", limit=limit)

    # Offline, or the live call failed: answer from every committed capture of
    # this term. A live title-only search is impossible anyway (Banner needs a
    # subject), so this is also the path for "data structures".
    envelope = classes.search_from_snapshots(
        snapshot_paths(), filters=filters, term=term, query_text=raw_text,
        limit=limit, max_age_s=max_age_s, now=now)
    if envelope.get("state") == "unavailable":
        reason = envelope.get("reason", "no course data is available")
        return _unavailable(reason, term=term, query=query)
    return _finalize(envelope, source="snapshot", limit=limit)