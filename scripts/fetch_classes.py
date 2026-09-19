#!/usr/bin/env python3
"""Edge fetcher: polite public Banner timetable POST -> offline snapshot JSON.

This is the ONLY place that talks to Banner. ``hokieday/classes.py`` never
imports urllib; it parses the HTML snapshot this script writes, which keeps the
whole library offline-testable and the demo reproducible (DEMO_MODE=cache).

PUBLIC DATA ONLY. This script:
  * POSTs the public timetable form (HZSKVTSC) and GETs the public building and
    final-exam pages;
  * sends no credentials, no cookies, and never touches HokieSPA / My VT;
  * never requests or stores grades, GPA, rosters, or student PIDs;
  * is rate-limited (default 2 s between requests) and uses a descriptive
    User-Agent so an administrator can identify the client;
  * does NOT crawl by default. Full-term subject-by-subject collection is
    available (``--all-subjects --yes-crawl``) for a deliberate, slow run.

Usage
-----
    # one harmless query -> fixtures/classes_snapshot_fall2026_as.json
    python3 scripts/fetch_classes.py --term 202609 --subject AS

    # a pasted CRN
    python3 scripts/fetch_classes.py --term 202609 --crn 81476 --name my_crn

    # public reference pages
    python3 scripts/fetch_classes.py --buildings
    python3 scripts/fetch_classes.py --exams --term 202609

    # list the subjects the form offers (no network beyond the form page)
    python3 scripts/fetch_classes.py --list-subjects

    # FULL-TERM collection (slow, explicit): one snapshot per subject
    python3 scripts/fetch_classes.py --all-subjects --yes-crawl --delay 3
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hokieday import classes, config  # noqa: E402

DEFAULT_OUT = REPO / "fixtures"
DEFAULT_DELAY = 2.0
REQUEST_TIMEOUT = 60


# ---------------------------------------------------------------------------
# HTTP (polite)
# ---------------------------------------------------------------------------
def _request(url: str, data: dict | None = None) -> str:
    """One HTTP request with the project User-Agent and a Referer on POSTs."""
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers = {
            "User-Agent": config.USER_AGENT,
            "Referer": classes.BANNER_FORM_URL,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml",
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    else:
        req = urllib.request.Request(
            url, headers={"User-Agent": config.USER_AGENT,
                          "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8", "replace")


def form_data(*, term: str, subject: str = "", course_number: str = "",
              crn: str = "", campus: str = classes.DEFAULT_CAMPUS,
              open_only: bool = False, schedule_type: str = classes.DEFAULT_SCHDTYPE,
              session: str = classes.DEFAULT_SESS_CODE,
              comments: bool = True) -> dict:
    """Exact public form fields. CORE_CODE is REQUIRED (omitting it returns the
    form, not results -- verified 2026-09-19)."""
    return {
        "CAMPUS": str(campus),
        "TERMYEAR": str(term),
        "CORE_CODE": classes.DEFAULT_CORE_CODE,
        "subj_code": str(subject or ""),
        "SCHDTYPE": str(schedule_type or "%"),
        "CRSE_NUMBER": str(course_number or ""),
        "crn": str(crn or ""),
        "open_only": "on" if open_only else "",
        "disp_comments_in": "Y" if comments else "",
        "sess_code": str(session or "%"),
        "BTN_PRESSED": "FIND class sections",
        "inst_name": "",
    }


def fetch_timetable(*, term: str, subject: str = "", course_number: str = "",
                    crn: str = "", campus: str = classes.DEFAULT_CAMPUS,
                    open_only: bool = False) -> str:
    return _request(classes.BANNER_PROC_URL, form_data(
        term=term, subject=subject, course_number=course_number, crn=crn,
        campus=campus, open_only=open_only))


def fetch_buildings() -> str:
    return _request(classes.BANNER_BUILDINGS_URL)


def fetch_exams() -> str:
    return _request(classes.BANNER_EXAMS_URL)


# ---------------------------------------------------------------------------
# Subject list (parsed from the form page's JS, no separate endpoint)
# ---------------------------------------------------------------------------
_OPTION_RE = re.compile(
    r'new Option\("([^"]*)","([^"]*)"', re.I)


def parse_subject_codes(form_html: str) -> list[tuple[str, str]]:
    """[(code, label)] from the form's ``new Option(...)`` subject list."""
    out: list[tuple[str, str]] = []
    for label, code in _OPTION_RE.findall(form_html or ""):
        code = code.strip()
        if not code or code == "%":
            continue
        out.append((code, label.strip()))
    return out


# ---------------------------------------------------------------------------
# Snapshot writing
# ---------------------------------------------------------------------------
def _snapshot_name(term: str, *, subject: str = "", course_number: str = "",
                   crn: str = "", name: str = "") -> str:
    if name:
        base = name
    elif subject:
        base = f"fall{term[:4]}_{subject.lower()}"
        if course_number:
            base += f"_{course_number.lower()}"
    elif crn:
        base = f"fall{term[:4]}_crn{crn}"
    else:
        base = f"fall{term[:4]}_all"
    return f"classes_snapshot_{base}.json"


def write_timetable_snapshot(out_dir: Path, html: str, *, term: str,
                             subject: str = "", course_number: str = "",
                             crn: str = "", campus: str = classes.DEFAULT_CAMPUS,
                             name: str = "") -> Path:
    query = {
        "campus": campus, "term": term, "subject": subject or None,
        "course_number": course_number or None, "crn": crn or None,
    }
    snapshot = classes.make_snapshot(
        html, term=term, query=query, campus=campus,
        source_url=classes.BANNER_PROC_URL,
        fetched_at=datetime.now(timezone.utc),
    )
    path = out_dir / _snapshot_name(term, subject=subject,
                                    course_number=course_number, crn=crn,
                                    name=name)
    classes.save_snapshot(path, snapshot)
    return path


def write_html_fixture(out_dir: Path, filename: str, html: str) -> Path:
    path = out_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _subject_mode(args) -> int:
    if args.all_subjects and not args.yes_crawl:
        print("--all-subjects is a slow full-term crawl. Re-run with "
              "--yes-crawl to confirm, and keep --delay polite.")
        return 2
    form_html = _request(classes.BANNER_FORM_URL)
    subjects = parse_subject_codes(form_html)
    if args.list_subjects:
        for code, label in subjects:
            print(f"{code}\t{label}")
        return 0
    if not subjects:
        print("could not parse any subject codes from the form page", file=sys.stderr)
        return 1
    if args.subject:
        wanted = [s for s in subjects if s[0].upper() == args.subject.upper()]
        if not wanted:
            print(f"subject {args.subject!r} is not in the form's subject list",
                  file=sys.stderr)
            return 1
        subjects = wanted
    if args.max_subjects:
        subjects = subjects[: args.max_subjects]
    print(f"crawling {len(subjects)} subject(s), {args.delay}s apart")
    written = 0
    for i, (code, _label) in enumerate(subjects, 1):
        try:
            html = fetch_timetable(term=args.term, subject=code,
                                   campus=args.campus)
        except urllib.error.URLError as exc:
            print(f"  FAIL {code}: {exc}", file=sys.stderr)
            continue
        # Per-subject filename so a crawl never overwrites its own evidence.
        path = write_timetable_snapshot(
            args.out, html, term=args.term, subject=code, campus=args.campus,
            name=f"{args.term}_{code}")
        print(f"  [{i}/{len(subjects)}] {code} -> {path.name} "
              f"({len(html):,} bytes)")
        written += 1
        if i < len(subjects):
            time.sleep(max(0.0, args.delay))
    print(f"wrote {written} snapshot(s) to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--term", default=classes.TERM_FALL_2026,
                    help="Banner term code (default: Fall 2026 = 202609)")
    ap.add_argument("--subject", default="", help="subject code, e.g. AS")
    ap.add_argument("--course-number", default="", help="course number, e.g. 1115")
    ap.add_argument("--crn", default="", help="a single CRN")
    ap.add_argument("--campus", default=classes.DEFAULT_CAMPUS)
    ap.add_argument("--open-only", action="store_true",
                    help="ask Banner for open sections only (filter is upstream)")
    ap.add_argument("--name", default="",
                    help="snapshot basename stem (default derived from query)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="output directory (default: fixtures/)")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help="seconds between crawl requests (default 2)")
    ap.add_argument("--buildings", action="store_true",
                    help="fetch the public building-abbreviation page")
    ap.add_argument("--exams", action="store_true",
                    help="fetch the public final-exam schedule page")
    ap.add_argument("--list-subjects", action="store_true",
                    help="print the form's subject codes and exit")
    ap.add_argument("--all-subjects", action="store_true",
                    help="full-term crawl: one snapshot per subject (needs --yes-crawl)")
    ap.add_argument("--yes-crawl", action="store_true",
                    help="confirm the full-term crawl")
    ap.add_argument("--max-subjects", type=int, default=0,
                    help="cap the crawl (safety valve); 0 = all")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    did_something = False

    if args.buildings:
        html = fetch_buildings()
        path = write_html_fixture(args.out, "classes_buildings.html", html)
        print(f"buildings -> {path} ({len(html):,} bytes)")
        did_something = True

    if args.exams:
        html = fetch_exams()
        path = args.out / f"classes_exams_{args.term}.html"
        path.write_text(html, encoding="utf-8")
        print(f"exams -> {path} ({len(html):,} bytes)")
        did_something = True

    if args.list_subjects or args.all_subjects or (
            args.subject and not (args.course_number or args.crn)):
        return _subject_mode(args)

    if args.crn or args.course_number:
        html = fetch_timetable(term=args.term, subject=args.subject,
                               course_number=args.course_number, crn=args.crn,
                               campus=args.campus, open_only=args.open_only)
        path = write_timetable_snapshot(
            args.out, html, term=args.term, subject=args.subject,
            course_number=args.course_number, crn=args.crn,
            campus=args.campus, name=args.name)
        print(f"timetable -> {path} ({len(html):,} bytes)")
        did_something = True

    if not did_something:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())