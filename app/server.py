#!/usr/bin/env python3
"""HokieFlow demo server — stdlib only, no dependencies, works offline.

WHY STDLIB, NOT STREAMLIT
Streamlit/pandas are not installed on this machine, and more importantly the
expo demo must survive unknown Wi-Fi and a possible Databricks quota shutdown.
`http.server` needs nothing installed and reads the frozen fixtures, so the demo
depends on nothing but Python.

Run:
    DEMO_MODE=cache python3 app/server.py            # offline, deterministic
    DEMO_MODE=cache python3 app/server.py --port 8080

Endpoints:
    GET  /                  the UI
    GET  /api/time          lightweight campus clock (no GTFS/dining/live buses)
    GET  /api/status        clock, mode, data freshness, provenance
    GET  /api/scenarios     the preset demo buttons
    POST /api/ask           {"text": "..."} -> plan_day(...) result
    GET  /api/raw?n=1       the raw JSON for scenario n (debugging / slides)
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo


# The repo root must be on sys.path BEFORE the imports below: run as a script
# (`python3 app/server.py`) sys.path[0] is app/, so `import auth` (repo root) and
# the .env loader would otherwise fail. This is the only reason the path setup
# sits above the imports.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.local_env import load_local_env  # noqa: E402

# Local credentials remain in the gitignored .env file. Load them before
# hokieday.config reads DEMO_MODE; existing process environment always wins.
load_local_env(REPO / ".env")

# Auth is OPTIONAL at import time: it needs the third-party `supabase` and
# `python-dotenv` packages, while the offline demo and the whole test suite must
# run on the stdlib alone (NFR-1). When the packages are absent the auth
# endpoints answer 503 with a clear reason instead of taking down the server.
try:                                                          # noqa: SIM105
    from auth import (
        clear_oauth_state_cookie,
        clear_session_cookie,
        get_oauth_state_cookie,
        get_session_cookie,
        handle_google_oauth_callback,
        login_user,
        logout_user,
        require_auth,
        register_user,
        set_oauth_state_cookie,
        set_session_cookie,
        start_google_oauth,
    )

    AUTH_AVAILABLE = True
    AUTH_UNAVAILABLE_REASON = None
except Exception as _auth_exc:                                 # noqa: BLE001
    AUTH_AVAILABLE = False
    AUTH_UNAVAILABLE_REASON = f"{type(_auth_exc).__name__}: {_auth_exc}"

    class AuthUnavailable(RuntimeError):
        """Raised when an auth endpoint is used without its optional deps."""

    def _auth_unavailable(*_args, **_kwargs):
        raise AuthUnavailable(
            "Accounts need the optional dependencies: "
            "pip install -r requirements.txt  (supabase, python-dotenv). "
            f"Import failed with {AUTH_UNAVAILABLE_REASON}")

    clear_oauth_state_cookie = _auth_unavailable
    clear_session_cookie = _auth_unavailable
    get_oauth_state_cookie = _auth_unavailable
    get_session_cookie = _auth_unavailable
    handle_google_oauth_callback = _auth_unavailable
    login_user = _auth_unavailable
    logout_user = _auth_unavailable
    require_auth = _auth_unavailable
    register_user = _auth_unavailable
    set_oauth_state_cookie = _auth_unavailable
    set_session_cookie = _auth_unavailable
    start_google_oauth = _auth_unavailable

from hokieday import agent as hokie_agent  # noqa: E402
from hokieday import cache, config, tools  # noqa: E402
from hokieday.agent_tools import AgentContext  # noqa: E402

# Absolute, not relative: this file is run as a SCRIPT (`python3 app/server.py`),
# where `from . import x` raises "attempted relative import with no known parent
# package". REPO is on sys.path just above, so `app` resolves either way -- as a
# namespace package when run as a script, and as app.server when tests import it.
from app import mapview  # noqa: E402
from app import ui_files  # noqa: E402
from app.gemini_provider import GeminiProvider  # noqa: E402

TZ = ZoneInfo(config.CAMPUS_TZ)

# Preset scenarios. Each is a deterministic call into the SAME plan_day the agent
# uses -- the demo is not a re-enactment.
SCENARIOS: list[dict] = [
    {
        "id": "eat",
        "label": "Can I eat and still make my 1:25?",
        "text": "I've got from 11:22 to 13:25, I'm hungry, and I need to get from Burruss to McBryde",
        "student_ref": "demo-student-1", "start": "11:22", "end": "13:25", "prefs": {},
    },
    {
        "id": "bus_replan",
        "label": "Same trip, but I want the bus",
        "text": "Same trip but I'd rather take the bus than walk",
        "student_ref": "demo-student-1", "start": "11:22", "end": "13:25",
        "prefs": {"prefer": "bus"},
    },
    {
        "id": "vegan",
        "label": "Vegan, and no sesame",
        "text": "I'm vegan and I can't have sesame. Same window.",
        "student_ref": "demo-student-2", "start": "11:22", "end": "13:25", "prefs": {},
    },
    {
        "id": "tight",
        "label": "Tighter window (11:22 to 12:05)",
        "text": "Only have until 12:05",
        "student_ref": "demo-student-1", "start": "11:22", "end": "12:05", "prefs": {},
    },
]

TIME_RE = re.compile(
    r"\b(\d{1,2}):(\d{2})(?:\s*([ap])\.?m\.?)?\b", re.IGNORECASE
)

# Common student phrasing mapped to the source's official allergen names.
# "nuts" deliberately expands to both categories; an avoid filter should err
# toward exclusion, never silently narrow what the student asked for.
_ALLERGEN_ALIASES: dict[str, tuple[str, ...]] = {
    "Milk": ("milk", "dairy"),
    "Eggs": ("egg", "eggs"),
    "Fish": ("fish",),
    "Crustacean Shellfish": ("shellfish", "crustacean shellfish"),
    "Tree Nuts": ("tree nut", "tree nuts", "nuts"),
    "Peanuts": ("peanut", "peanuts", "nuts"),
    "Wheat": ("wheat",),
    "Soybeans": ("soy", "soybean", "soybeans"),
    "Gluten": ("gluten",),
    "Sesame": ("sesame",),
}

# alias -> every official name it stands for. "nuts" maps to BOTH Tree Nuts and
# Peanuts so an avoid filter always errs toward exclusion, never toward serving
# something the student named.
_ALIAS_TO_OFFICIALS: dict[str, tuple[str, ...]] = {}
for _official, _aliases in _ALLERGEN_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_TO_OFFICIALS[_alias] = _ALIAS_TO_OFFICIALS.get(_alias, ()) + (_official,)

# Longest alias first so "tree nuts" wins over "nuts", "soybeans" over "soy".
_ALIAS_ALT = "|".join(
    sorted((re.escape(a) for a in _ALIAS_TO_OFFICIALS), key=len, reverse=True))
_ALLERGEN_WORD_RE = re.compile(rf"\b(?:{_ALIAS_ALT})\b", re.IGNORECASE)

# A list separator: comma/slash/ampersand/plus (optionally followed by and/or)
# or a bare "and"/"or".
_ALLERGEN_SEP_RE = re.compile(
    r"(?:\s*(?:,|/|&|\+)\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+)",
    re.IGNORECASE,
)

# Negation/avoidance cues are split into two TRIGGER CLASSES, because they
# carry different evidence:
#
#   EXPLICIT -- "allergic to", "can't have": the sentence itself asserts a
#   dietary restriction, so an unmappable list member is always worth a
#   clarification, even when no known allergen was captured ("allergic to
#   poultry").
#
#   BARE -- "no", "avoid", "without": these are overwhelmingly transport or
#   environment language ("no parking", "avoid traffic", "no rain route"), so
#   they only denote an allergen list when a RECOGNISED allergen anchors the
#   same joined list ("no milk and poultry"). This is a trigger-class rule, not
#   a growing keyword stoplist.
_EXPLICIT_AVOID_TRIGGER_RE = re.compile(
    r"\b(?:allergic\s+to|allergy\s+to|allergies\s+to|"
    r"can(?:no|'|\u2019)t\s+have|cannot\s+have)\b",
    re.IGNORECASE,
)
_BARE_AVOID_TRIGGER_RE = re.compile(
    r"\b(?:avoid(?:ing)?|without|no)\b",
    re.IGNORECASE,
)

# Only an explicit allergy/restriction phrase warrants a clarification when no
# known allergen can be parsed: a bare "no" is far too common ("no bus") to
# fire a dietary question.
_ALLERGY_MENTION_RE = re.compile(
    r"\ballerg(?:y|ies|ic)\b|can(?:no|'|\u2019)t\s+have|cannot\s+have",
    re.IGNORECASE,
)

# Reverse phrasing: "a milk and eggs allergy". Requires at least one allergen.
_REVERSE_ALLERGY_RE = re.compile(
    rf"(?P<list>(?:\b(?:{_ALIAS_ALT})\b)"
    rf"(?:(?:\s*(?:,|/|&|\+)\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+)"
    rf"(?:\b(?:{_ALIAS_ALT})\b))*)"
    rf"\s+allerg(?:y|ies|ic)\b",
    re.IGNORECASE,
)


# Clause furniture that can follow a list separator. An unrecognised token
# only counts as a dropped list member when it is NOT one of these, so normal
# prose ("allergic to milk and I need lunch", "no bus, but I can have eggs")
# is never mistaken for an ingredient. Without a food ontology a stoplist is
# the honest way to distinguish "milk and poultry" from "milk and want".
_LIST_STOPWORDS = frozenset({
    "i", "im", "we", "we're", "you", "you're", "he", "she", "it", "they",
    "them", "my", "me", "our", "us", "your", "a", "an", "the", "this",
    "that", "these", "those", "and", "or", "but", "so", "then", "also",
    "to", "for", "by", "at", "on", "in", "of", "with", "from", "as",
    "is", "are", "was", "were", "be", "been", "being", "can", "cant",
    "cannot", "could", "would", "should", "will", "shall", "may", "might",
    "must", "have", "has", "had", "need", "needs", "want", "wants",
    "like", "please", "only", "just", "really", "very", "too", "not", "no",
    "yes", "if", "when", "while", "because", "before", "after", "around",
    "about", "between", "get", "getting", "going", "go", "know", "think",
    "say", "said", "tell", "give", "take", "make", "find", "show", "help",
    "eat", "eating", "lunch", "dinner", "breakfast", "food", "menu", "plan",
    "walk", "bus", "transit", "ride", "tickets", "ticket", "time", "day",
    "today", "tomorrow", "please", "thanks", "thank", "problem", "homework",
    "money", "idea", "way", "doubt", "rush", "hurry", "location", "place",
    "grab", "meet", "meeting", "head", "attend", "class", "study", "work",
    "leave", "arrive", "come", "came", "send", "put", "keep", "let", "look",
    "feel", "become", "turn", "start", "begin", "stop", "finish", "end", "use",
    "used", "using", "try", "trying", "call", "ask", "answer", "follow", "move",
    "run", "sit", "stand", "wait", "waiting", "stay", "drive", "catch", "bring",
    "buy", "order", "pick", "choose", "decide", "schedule", "check", "confirm",
    "book", "reserve", "pay", "cost", "free", "busy", "open", "closed", "available",
    "right", "now", "later", "soon", "first", "next", "last", "again", "still",
    "actually", "maybe", "probably", "sure", "ok", "okay", "alright", "great",
    "good", "fine", "better", "best", "quick", "quickly", "fast", "early", "late",
    "exam", "test", "project", "job", "lecture", "library", "dorm", "room",
    "building", "hall", "campus", "calendar", "reminder", "note", "email",
    "message", "phone", "laptop", "charger", "wifi", "internet", "signal", "service",
})


def _scan_constraint_list(text: str, pos: int) -> tuple[list[str], bool]:
    """Walk one joined constraint list starting at `pos`.

    Grammar: [any|the] MEMBER (SEP MEMBER)*, where a member is either a known
    allergen or one plausible but unmappable word. The scan stops at the first
    NON-member token -- clause furniture such as "I", "need", "bus" -- so
    ordinary prose after the list is never mistaken for an ingredient.

    Returns (known officials, saw_unmappable_member). The flag is True when a
    separator-joined member could not be mapped, e.g. "milk and poultry" or
    "poultry and milk". It does NOT fire for a separator followed by ordinary
    prose ("milk and I need lunch") or for a leading non-member ("no bus").
    """
    lead = re.match(r"\s+(?:any\s+|the\s+)?", text[pos:])
    if not lead:
        return [], False
    i = pos + lead.end()
    officials: list[str] = []
    unmappable = False
    while True:
        word = _ALLERGEN_WORD_RE.match(text, i)
        if word:
            officials.extend(_ALIAS_TO_OFFICIALS[word.group(0).lower()])
            i = word.end()
        else:
            m = re.match(r"([A-Za-z][A-Za-z'\-]*)", text[i:])
            if not m:
                return officials, unmappable
            token = m.group(1).lower().replace("'", "")
            if token in _LIST_STOPWORDS:
                return officials, unmappable
            unmappable = True
            i += m.end()
        sep = _ALLERGEN_SEP_RE.match(text, i)
        if not sep:
            # A known alias followed immediately by another content word is a
            # multiword member we cannot map safely ("milk powder", "dairy
            # products", "tree nut products"). Do not silently keep only the
            # known prefix: force clarification. Clause furniture such as
            # "today", "before class", or "I need" remains a clean stop.
            residue = re.match(r"\s+([A-Za-z][A-Za-z'\-]*)", text[i:])
            if residue:
                token = residue.group(1).lower().replace("'", "")
                if token not in _LIST_STOPWORDS:
                    unmappable = True
            return officials, unmappable
        i = sep.end()


def _extract_avoids(text: str) -> tuple[list[str], bool]:
    """Known allergens named by avoidance phrases, plus an unparsed flag.

    Forward triggers ("allergic to a, b and c") and reverse phrasing
    ("a, b allergy") both feed the same set. Results are deduplicated in the
    source's official allergen order, and aliases like "nuts" expand to every
    category they cover.

    The boolean is True whenever a restriction clause contained a list member
    that could not be mapped. Explicit allergy/cannot-have phrases fire on any
    unmappable member; bare no/avoid/without phrases fire only once a known
    allergen anchors the list, so transport/weather language is not mistaken
    for a dietary constraint.
    """
    found: list[str] = []
    unparsed = False
    for m in _EXPLICIT_AVOID_TRIGGER_RE.finditer(text):
        officials, unmappable = _scan_constraint_list(text, m.end())
        found.extend(officials)
        unparsed = unparsed or unmappable
    for m in _BARE_AVOID_TRIGGER_RE.finditer(text):
        officials, unmappable = _scan_constraint_list(text, m.end())
        found.extend(officials)
        if officials:
            unparsed = unparsed or unmappable
    for m in _REVERSE_ALLERGY_RE.finditer(text):
        for am in _ALLERGEN_WORD_RE.finditer(m.group("list")):
            found.extend(_ALIAS_TO_OFFICIALS[am.group(0).lower()])
    avoids = [official for official in _ALLERGEN_ALIASES if official in found]
    return avoids, unparsed


def _has_unparsed_allergy(text: str) -> bool:
    """An explicit allergy/restriction phrase that named no known allergen."""
    return bool(_ALLERGY_MENTION_RE.search(text))


def _clock_match(match: re.Match) -> tuple[int, bool] | None:
    """Return minute-of-day and whether AM/PM was explicit."""
    hour, minute = int(match.group(1)), int(match.group(2))
    suffix = (match.group(3) or "").lower()
    if minute > 59 or hour > (12 if suffix else 23):
        return None
    if suffix:
        hour = hour % 12 + (12 if suffix == "p" else 0)
    return hour * 60 + minute, bool(suffix)


def _clock_text(total_min: int) -> str:
    total_min %= 24 * 60
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def _clock_label(total_min: int) -> str:
    total_min %= 24 * 60
    hour, minute = divmod(total_min, 60)
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"



# Served inline so the app has no asset files to lose.
MANIFEST = {
    "name": "HokieFlow",
    "short_name": "HokieFlow",
    "description": "Campus-life agent: one question across dining, transit and hours.",
    "start_url": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0f1115",
    "theme_color": "#0f1115",
    "icons": [{"src": "/icon.svg", "sizes": "any",
               "type": "image/svg+xml", "purpose": "any maskable"}],
}

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="112" fill="#861f41"/>
  <text x="256" y="330" font-family="-apple-system,Helvetica,Arial,sans-serif"
        font-size="210" font-weight="700" text-anchor="middle" fill="#ffffff">HF</text>
  <circle cx="388" cy="124" r="36" fill="#e87722"/>
</svg>
"""


def lan_ips() -> list[str]:
    """Best-effort LAN addresses, so a phone on the same network can connect.

    The UDP connect sends nothing; it just asks the routing table which local
    address would be used to reach the outside world.
    """
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:                                      # noqa: BLE001
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:                                      # noqa: BLE001
        pass
    return sorted(ips)


def _iso_campus(dt: datetime) -> str:
    """Campus-local ISO text for a request-bound window bound."""
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def _clock_minutes(value) -> int | None:
    """Parse a bare "HH:MM" to minute-of-day, or None."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def time_meta(now: datetime | None = None, live: bool | None = None) -> dict:
    """Structured clock metadata attached to every /api/ask response.

    `evaluated_at` is the ONE campus-local timestamp captured at the request
    boundary; planning and every displayed time are derived from it, never from
    the browser clock. `time_source` says whether it came from the pinned
    replay snapshot or the live wall clock.
    """
    if live is None:
        live = not config.CACHE_ONLY
    if now is None:
        now = config.now(TZ)
    return {
        "mode": config.DEMO_MODE,
        "evaluated_at": now.astimezone(TZ).isoformat(timespec="seconds"),
        "time_source": "wall_clock" if live else "snapshot",
        "is_replay": not live,
    }


def time_endpoint(now: datetime | None = None, live: bool | None = None) -> dict:
    """Lightweight campus clock for the UI chip (GET /api/time).

    Deliberately touches nothing but the clock abstraction: no GTFS load, no
    dining read, no live-bus fetch. The chip re-syncs against this periodically
    while the heavier /api/status is fetched once at boot.
    """
    if live is None:
        live = not config.CACHE_ONLY
    if now is None:
        now = config.now(TZ)
    now = now.astimezone(TZ)
    return {
        "mode": config.DEMO_MODE,
        "is_replay": not live,
        "time_source": "wall_clock" if live else "snapshot",
        "timezone": config.CAMPUS_TZ,
        "iso": now.isoformat(timespec="seconds"),
        "evaluated_at": now.isoformat(timespec="seconds"),
        "clock": now.strftime("%H:%M:%S"),
        "human": now.strftime("%I:%M %p").lstrip("0"),
        "weekday": now.strftime("%a %d %b %Y"),
        "pinned": not live,
        "ticking": live,
    }


def _preset_deadline_guard(call: dict, now: datetime, live: bool) -> dict | None:
    """In live mode, refuse to replay a preset whose deadline already passed.

    The preset keeps its explicit window, but a passed deadline is a correction
    the student must see -- silently rolling it to tomorrow would fabricate a
    plan for a time they never asked about.
    """
    if not live:
        return None
    end_min = _clock_minutes(call.get("end"))
    if end_min is None:
        return None
    now_min = now.hour * 60 + now.minute
    if end_min <= now_min:
        return {
            "kind": "deadline_passed",
            "question": (f"This scenario's {_clock_label(end_min)} deadline has "
                         f"already passed today. What time should I plan for?"),
            "detail": (f"Now is {_clock_label(now_min)}; the preset deadline "
                       f"{_clock_label(end_min)} is in the past. I will not "
                       f"silently move it to tomorrow \u2014 give me a later "
                       f"time today."),
            "now": _clock_label(now_min),
            "deadline": _clock_label(end_min),
        }
    return None


def _materialize_live_preset(call: dict, now: datetime) -> None:
    """In live mode, replace a preset's frozen `start` with the captured request
    time, keeping the preset's explicit deadline on the same campus date.

    A preset's window (11:22-13:25) is a replay artifact. If the request arrives
    at 12:00, reusing 11:22 as the start hands the student a plan that left an
    hour ago. The deadline is the student's real constraint, so it is preserved;
    only the start moves to the captured `now`.
    """
    end_min = _clock_minutes(call.get("end"))
    if end_min is None:
        return
    now_min = now.hour * 60 + now.minute
    call["start"] = _iso_campus(now)
    call["end"] = _iso_campus(now.replace(hour=end_min // 60,
                                          minute=end_min % 60,
                                          second=0, microsecond=0))
    call.setdefault("_interpretation_notes", []).append(
        f"Planning from now ({_clock_label(now_min)}) to your "
        f"{_clock_label(end_min)} deadline.")


def _resolve_live_window(call: dict, parsed: list, now: datetime) -> None:
    """Resolve a free-text window in DEMO_MODE=live.

    Live planning starts at the captured request time, never at the frozen
    11:22-13:25 demo window. No deadline -> a structured clarification, because
    borrowing the demo window would silently invent a deadline the student never
    gave. One explicit deadline becomes now -> deadline; a deadline already in
    the past is corrected, never rolled to tomorrow.
    """
    now_min = now.hour * 60 + now.minute
    if not parsed:
        call["end"] = None
        call["_clarification"] = {
            "kind": "need_deadline",
            "question": "What time do you need to arrive by?",
            "detail": ("I can plan from right now, but I need a deadline. "
                       "Tell me a time such as \u201c1:25 PM\u201d and I'll work "
                       "backwards from it."),
            "now": _clock_label(now_min),
        }
        return
    if len(parsed) == 1:
        match, (end_min, explicit) = parsed[0]
        if not explicit and end_min <= now_min and end_min < 12 * 60:
            end_min += 12 * 60
            call["_interpretation_notes"].append(
                f"Interpreted {match.group(0)} as {_clock_label(end_min)}.")
        if end_min <= now_min:
            call["end"] = _clock_text(end_min)
            call["_clarification"] = {
                "kind": "deadline_passed",
                "question": (f"{_clock_label(end_min)} has already passed today. "
                             f"Do you mean a later time, or tomorrow?"),
                "detail": (f"Now is {_clock_label(now_min)} and your deadline "
                           f"{_clock_label(end_min)} is in the past. I will not "
                           f"silently move it to tomorrow \u2014 give me a later "
                           f"time today."),
                "now": _clock_label(now_min),
                "deadline": _clock_label(end_min),
            }
            return
        call["start"] = _iso_campus(now)
        call["end"] = _iso_campus(now.replace(hour=end_min // 60,
                                               minute=end_min % 60,
                                               second=0, microsecond=0))
        call["_interpretation_notes"].append(
            f"Planning from now ({_clock_label(now_min)}) to your "
            f"{_clock_label(end_min)} deadline.")
        return
    # Two or more explicit times: honor them, anchored to the request date.
    (m1, (start_min, start_explicit)), (m2, (end_min, end_explicit)) = parsed[:2]
    if not end_explicit and end_min <= start_min and end_min < 12 * 60:
        end_min += 12 * 60
        call["_interpretation_notes"].append(
            f"Interpreted {m2.group(0)} as {_clock_label(end_min)}.")
    if (not start_explicit and not end_explicit
            and start_min < 8 * 60 and end_min < 8 * 60):
        start_min += 12 * 60
        end_min += 12 * 60
        call["_interpretation_notes"].append(
            "Interpreted both unsuffixed times as PM.")
    call["start"] = _iso_campus(now.replace(hour=start_min // 60,
                                            minute=start_min % 60,
                                            second=0, microsecond=0))
    call["end"] = _iso_campus(now.replace(hour=end_min // 60,
                                          minute=end_min % 60,
                                          second=0, microsecond=0))


# Words that cannot belong to a place name and therefore TERMINATE a bounded
# `from`/`to` phrase. Without this, "I want to know" invents a place named
# "know" and "from 11:22" invents "11".
_PLACE_BOUNDARY_WORDS = frozenset({
    "a", "an", "the", "my", "your", "his", "her", "their", "our",
    "and", "or", "then", "but", "so", "please",
    "by", "at", "before", "after", "around", "until", "till", "on", "in",
    "for", "with", "without", "via", "near", "from", "to",
    "get", "go", "going", "come", "eat", "eating", "take", "taking",
    "walk", "walking", "ride", "riding", "bus", "transit", "drive",
    "know", "see", "ask", "check", "find", "make", "plan", "leave",
    "arrive", "meet", "need", "want", "class", "meeting", "lunch",
    "dinner", "breakfast", "food",
})

# An unknown place phrase is accepted only when it looks like a proper name
# (every word capitalised) or names a kind of campus venue. This keeps ordinary
# grammar -- "to know", "to eat" -- from being mistaken for a destination.
_PLACE_SUFFIX_WORDS = frozenset({
    "hall", "center", "centre", "library", "market", "court", "building",
    "gym", "stadium", "theatre", "theater", "auditorium", "commons",
    "cafeteria", "dining", "arena", "field", "park", "lot", "garage",
    "institute", "lab", "laboratory", "complex", "pavilion", "chapel",
    "church", "school", "inn", "house",
})


# Deictic / filler words that must never become a place name when a lowercase
# from/to route phrase is accepted. Unlike _PLACE_BOUNDARY_WORDS these are only
# consulted on the lowercase fallback path, so a capitalised unknown name
# ("New Building") or a venue suffix ("duck pond" for the pond word) still
# works through the proper-noun / venue checks above.
_ROUTE_PLACE_STOPWORDS = frozenset({
    "here", "there", "home", "now", "then", "somewhere", "anywhere",
    "everywhere", "everyplace",
})


def _bounded_place_phrase(raw: str, start: int,
                          *, allow_lowercase: bool = False) -> str | None:
    """Read at most four words from `start`, stopping at a boundary word.

    Returns the phrase only when it looks like a place: proper-noun casing or a
    campus-venue suffix. When `allow_lowercase` is set (a strong bounded
    from/to route phrase), an all-lowercase unknown name is also accepted
    provided no word is a deictic stopword, so "from burruss to narnia" keeps
    narnia while "from here to there" does not become place names.
    """
    words: list[str] = []
    pos = start
    while len(words) < 4:
        m = re.match(r"\s*([A-Za-z][A-Za-z'.\-]*)", raw[pos:])
        if not m:
            break
        word = m.group(1)
        if word.lower() in _PLACE_BOUNDARY_WORDS:
            break
        words.append(word)
        pos += m.end()
        tail = raw[pos:]
        space = re.match(r"\s*", tail)
        nxt = tail[space.end():space.end() + 1]
        if nxt in ",.;:!?()":
            break
    if not words:
        return None
    proper = all(w[:1].isupper() for w in words)
    venue = any(w.lower() in _PLACE_SUFFIX_WORDS for w in words)
    if proper or venue:
        return " ".join(words)
    if allow_lowercase and not any(
            w.lower() in _ROUTE_PLACE_STOPWORDS for w in words):
        return " ".join(words)
    return None


def _extract_explicit_places(raw: str, text: str) -> tuple[str | None, str | None]:
    """Return (from_place, to_place) for EXPLICIT from/to phrases.

    Known configured places win and keep their canonical key. An unknown but
    clearly place-like phrase is preserved verbatim so the planner reports
    `unknown_place` instead of silently defaulting to a different building.
    Lowercase unknown names are only accepted when the request is a strong
    bounded from/to route phrase (both cues present), so ordinary grammar --
    "to know", "to eat", "from 11:22" -- is never promoted to a place.
    """
    from_place: str | None = None
    to_place: str | None = None
    # A from/to PAIR bounds the unknown-name risk: the phrase sits between two
    # routing cues instead of floating in prose. A lone lowercase cue does not.
    route_phrase = bool(re.search(r"\bfrom\b", text)
                        and re.search(r"\bto\b", text))
    for place, row in config.static_places():
        aliases = {place.lower(), place.lower().removesuffix(" hall")}
        for alias in aliases:
            a = re.escape(alias)
            if re.search(rf"\bfrom\s+(?:the\s+)?{a}\b", text):
                from_place = place
            if re.search(rf"\bto\s+(?:the\s+)?{a}\b", text):
                to_place = place
    for cue, current in (("from", from_place), ("to", to_place)):
        if current is not None:
            continue
        for m in re.finditer(rf"\b{cue}\s+(?:the\s+)?", raw, re.IGNORECASE):
            phrase = _bounded_place_phrase(
                raw, m.end(), allow_lowercase=route_phrase)
            if phrase is not None:
                if cue == "from":
                    from_place = phrase
                else:
                    to_place = phrase
                break
    return from_place, to_place


def parse_free_text(text: str, *, now: datetime | None = None,
                    live: bool | None = None) -> dict:
    """A deliberately SMALL, honest offline parser.

    It is NOT presented as the language model. With a Databricks workspace the
    text->parameters step is the agent's job; this fallback only handles the
    demo's bounded vocabulary and carries every time inference back to the UI.

    In DEMO_MODE=cache (the default for tests) the frozen demo window is kept
    exactly as before. In live mode the window is anchored to `now` and a
    missing/passed deadline yields a structured clarification instead of a
    silently borrowed window.
    """
    raw = text or ""
    t = raw.lower()
    if live is None:
        live = not config.CACHE_ONLY
    if now is None:
        now = config.now(TZ)
    call = {
        "id": "free_text",
        "label": "Free-text request",
        "text": raw,
        "student_ref": "demo-free-text",
        "start": (SCENARIOS[0]["start"] if not live
                  else _clock_text(now.hour * 60 + now.minute)),
        "end": SCENARIOS[0]["end"] if not live else None,
        "prefs": {},
        "_interpretation_notes": [],
    }

    if any(p in t for p in ("no bus", "without the bus", "walk only",
                            "only walk", "skip the bus", "don't take the bus",
                            "do not take the bus", "prefer walking")):
        call["prefs"]["prefer"] = "walk"
    elif re.search(r"\b(?:bus|transit|ride)\b", t):
        call["prefs"]["prefer"] = "bus"

    if "vegan" in t:
        call["prefs"]["diet"] = "vegan"
    elif "vegetarian" in t:
        call["prefs"]["diet"] = "vegetarian"
    avoids, avoid_unparsed = _extract_avoids(t)
    if avoids:
        call["prefs"]["avoid"] = avoids
    if avoid_unparsed or (not avoids and _has_unparsed_allergy(t)):
        # SAFETY: an explicit restriction listed an ingredient we could not
        # map, or named no known allergen at all. Never plan as if the dropped
        # constraint did not exist -- ask instead.
        call["_clarification"] = {
            "kind": "allergen_unparsed",
            "question": "Which ingredient should I avoid?",
            "detail": ("You mentioned an allergy, but I couldn't match it to a "
                       "known allergen. Tell me the ingredient (for example "
                       "milk, eggs, peanuts, tree nuts, sesame, soy, wheat, "
                       "fish, shellfish, or gluten) and I'll filter the menu."),
        }

    # Recognise explicit from/to phrases. Known configured places keep their
    # canonical key; a clear but unknown phrase is preserved so plan_day returns
    # `unknown_place` rather than silently substituting the default building.
    from_place, to_place = _extract_explicit_places(raw, t)
    if from_place is not None:
        call["prefs"]["from_place"] = from_place
    if to_place is not None:
        call["prefs"]["to_place"] = to_place

    matches = list(TIME_RE.finditer(raw))
    parsed = [(m, _clock_match(m)) for m in matches]
    parsed = [(m, value) for m, value in parsed if value is not None]
    if live and not call.get("_clarification"):
        _resolve_live_window(call, parsed, now)
    elif len(parsed) >= 2:
        (m1, (start_min, start_explicit)), (m2, (end_min, end_explicit)) = parsed[:2]
        # In a daytime campus-planning question, "11:22 to 1:25" means the
        # next 1:25, not thirteen hours backwards. Preserve explicit AM/PM.
        if not end_explicit and end_min <= start_min and end_min < 12 * 60:
            end_min += 12 * 60
            call["_interpretation_notes"].append(
                f"Interpreted {m2.group(0)} as {_clock_label(end_min)}.")
        # "1:00 to 2:00" during the daytime gets the same next-occurrence rule.
        if (not start_explicit and not end_explicit
                and start_min < 8 * 60 and end_min < 8 * 60):
            start_min += 12 * 60
            end_min += 12 * 60
            call["_interpretation_notes"].append(
                "Interpreted both unsuffixed times as PM.")
        call["start"], call["end"] = _clock_text(start_min), _clock_text(end_min)
    elif len(parsed) == 1:
        match, (end_min, explicit) = parsed[0]
        start_h, start_m = (int(p) for p in call["start"].split(":"))
        start_min = start_h * 60 + start_m
        if not explicit and end_min <= start_min and end_min < 12 * 60:
            end_min += 12 * 60
            call["_interpretation_notes"].append(
                f"Interpreted {match.group(0)} as {_clock_label(end_min)}.")
        call["end"] = _clock_text(end_min)
    return call


def _static_or_none(payload: dict, info: dict) -> tuple[str | None, dict]:
    """Fall back to a selected campus place (or the planner default).

    Used whenever a device position is absent or rejected, so the caller always
    gets a usable origin and the reason travels with it.
    """
    sel = payload.get("from_place")
    if sel and str(sel) not in ("auto", "default"):
        row = config.lookup_place(str(sel))
        if row and not row.get("dynamic"):
            if info.get("note"):
                info["note"] += f" \u2014 using {sel} instead"
            info["source"] = "selected"
            info["label"] = str(sel)
            return str(sel), info
    return None, info


def resolve_origin(payload: dict) -> tuple[str | None, dict]:
    """Turn an optional device position into a place key.

    NEVER raises and never silently accepts a bad origin: a wrong origin yields a
    confidently wrong plan, so every rejection carries a stated reason.

    The rejection that matters in practice: a LAPTOP's Wi-Fi geolocation often
    resolves to the ISP, hundreds of km from campus. Accepting that would produce
    a plan with a multi-day walk, so anything beyond MAX_ORIGIN_KM falls back.
    """
    info: dict = {"source": "default", "label": None, "accuracy_m": None,
                  "note": None}
    if payload.get("lat") is None or payload.get("lon") is None:
        return _static_or_none(payload, info)
    try:
        lat, lon = float(payload["lat"]), float(payload["lon"])
    except (TypeError, ValueError):
        info["note"] = "device position was not numeric; using the default origin"
        return _static_or_none(payload, info)
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        info["note"] = (f"device position {lat}, {lon} is out of range; "
                        f"using the default origin")
        return _static_or_none(payload, info)

    km = tools._haversine_m((lat, lon), config.CAMPUS_REFERENCE) / 1000.0
    if km > config.MAX_ORIGIN_KM:
        info["note"] = (f"your device reports a position {km:,.0f} km from campus — "
                        f"that is Wi-Fi/network geolocation rather than GPS, so the "
                        f"plan uses the default origin instead")
        info["rejected_km_from_campus"] = round(km, 1)
        return _static_or_none(payload, info)

    try:
        acc = float(payload["accuracy"]) if payload.get("accuracy") is not None else None
    except (TypeError, ValueError):
        acc = None

    key = config.register_dynamic_place(lat, lon, "your location", acc)
    info.update({"source": "device", "label": "your location",
                 "lat": round(lat, 6), "lon": round(lon, 6),
                 "accuracy_m": acc, "km_from_campus": round(km, 3)})
    if acc is not None and acc > 100:
        info["note"] = (f"device accuracy is only ±{acc:,.0f} m, so walk times "
                        f"are approximate")
    return key, info


def deep_links(result: dict) -> list[dict]:
    """Keyless navigation handoff, one movement leg at a time.

    A single origin→destination link skipped the dining waypoint entirely. Each
    walk/bus leg now gets its own Apple and Google link, so following the links
    follows the itinerary the student was actually shown. `dirflg`: w=foot,
    r=public transit. `travelmode`: walking|transit.
    """
    legs = ((result.get("itinerary") or {}).get("legs")) or []
    out: list[dict] = []
    for leg in legs:
        if leg.get("type") not in ("walk", "bus"):
            continue
        origin, dest = leg.get("from_coords"), leg.get("to_coords")
        if not (origin and dest):
            continue
        # Commas must be percent-encoded in Maps URLs.
        o = f"{origin[0]:.6f}%2C{origin[1]:.6f}"
        d = f"{dest[0]:.6f}%2C{dest[1]:.6f}"
        is_bus = leg.get("type") == "bus"
        mode = "transit" if is_bus else "walking"
        flag = "r" if is_bus else "w"
        destination = str(leg.get("to") or leg.get("to_stop_name") or "next stop")
        action = (f"Ride {leg.get('route_id')} to {destination}" if is_bus
                  else f"Walk to {destination}")
        common = {"label": action, "mode": mode, "leg_seq": leg.get("seq")}
        out.extend([
            {**common, "app": "Apple Maps",
             "url": f"https://maps.apple.com/?saddr={o}&daddr={d}&dirflg={flag}"},
            {**common, "app": "Google Maps",
             "url": ("https://www.google.com/maps/dir/?api=1"
                     f"&origin={o}&destination={d}&travelmode={mode}")},
        ])
    return out


def run_plan(call: dict, origin: dict | None = None,
             now: datetime | None = None) -> dict:
    prefs = call.get("prefs") or {}
    if call.get("_clarification"):
        # A structured clarification is a first-class result: no itinerary, no
        # map, no invented deadline. `now` is the captured request clock.
        cl = dict(call["_clarification"])
        result = {
            "student_ref": call.get("student_ref"),
            "itinerary": None,
            "rationale": cl.get("detail") or cl.get("question") or "",
            "feasible": False,
            "infeasible_reason": {"code": "clarification_needed",
                                   "kind": cl.get("kind")},
            "alternatives": [],
            "replan_trigger": None,
            "clarification": cl,
        }
    else:
        result = tools.plan_day(call["student_ref"], call["start"], call["end"],
                                prefs, now=now)
    result["_request"] = {"student_ref": call["student_ref"],
                          "start": call.get("start"), "end": call.get("end"),
                          "prefs": prefs}
    default_label = str(prefs.get("from_place") or "Burruss Hall")
    result["_origin"] = origin or {"source": "default", "label": default_label,
                                    "accuracy_m": None, "note": None}
    result["_interpretation_notes"] = list(call.get("_interpretation_notes") or [])
    if call.get("_clarification"):
        result["_map_svg"] = ""
        result["_links"] = []
        return result
    # The map is drawn from OUR data (GTFS shapes) and inlined, so it renders with
    # networking off. Deep links are the keyless handoff for real navigation.
    try:
        plan_a = next((a.get("itinerary") for a in (result.get("alternatives") or [])
                       if a.get("type") == "previous_itinerary_a"), None)
        result["_map_svg"] = mapview.build_map_svg(result.get("itinerary"),
                                                   overlay=plan_a)
    except Exception as exc:                               # noqa: BLE001
        result["_map_svg"] = ""
        result["_map_error"] = f"{type(exc).__name__}: {exc}"
    try:
        result["_links"] = deep_links(result)
    except Exception:                                      # noqa: BLE001
        result["_links"] = []
    return result


_MAX_KCAL_RE = re.compile(
    r"\b(?:under|below|less\s+than|at\s+most|max(?:imum)?|no\s+more\s+than)\s*"
    r"(\d{1,4}(?:\.\d+)?)\s*(?:kcal|calories?)\b",
    re.IGNORECASE,
)


def _agent_hard_constraints(text: str) -> dict:
    """Deterministically preserve supported numeric safety constraints."""
    ceilings = []
    for match in _MAX_KCAL_RE.finditer(str(text or "")):
        value = float(match.group(1))
        if 0 <= value <= 5000:
            ceilings.append(value)
    return {"max_kcal": min(ceilings)} if ceilings else {}


_CLIENT_BUDGET_LOCK = threading.Lock()
_CLIENT_QUESTION_TIMES: dict[str, deque[float]] = defaultdict(deque)


def _client_agent_budget_available(client_ip: str) -> bool:
    """Per-process/IP guard in front of the shared Gemini call budget."""
    try:
        limit = int(os.environ.get("HOKIEFLOW_AI_QUESTIONS_PER_IP_HOUR", "10"))
    except ValueError:
        limit = 10
    limit = min(max(limit, 1), 1000)
    now_mono = time.monotonic()
    key = str(client_ip or "unknown")
    with _CLIENT_BUDGET_LOCK:
        queue = _CLIENT_QUESTION_TIMES[key]
        while queue and now_mono - queue[0] >= 3600:
            queue.popleft()
        if len(queue) >= limit:
            return False
        queue.append(now_mono)
        return True


def configured_agent_provider():
    """Build the opt-in live provider from environment configuration.

    No default model is hardcoded: both the secret and model id must be set at
    runtime. In replay mode callers never invoke this function.
    """
    provider_name = os.environ.get("HOKIEFLOW_AI_PROVIDER", "").strip().lower()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "").strip()
    if not provider_name and (key or model):
        provider_name = "gemini"
    if provider_name != "gemini" or not key or not model:
        return None
    try:
        return GeminiProvider(key, model)
    except ValueError:
        return None


def _decorate_agent_plan(result: dict, origin: dict, now: datetime,
                         live: bool) -> dict:
    """Keep agent answers compatible with the existing structured UI."""
    result["_origin"] = origin
    result["_time"] = time_meta(now, live=live)
    nested = result.get("result")
    if isinstance(nested, dict):
        nested.setdefault("_origin", origin)
        nested.setdefault("_time", result["_time"])
    if isinstance(result.get("itinerary"), dict):
        try:
            plan_a = next((a.get("itinerary") for a in
                           (result.get("alternatives") or [])
                           if a.get("type") == "previous_itinerary_a"), None)
            result["_map_svg"] = mapview.build_map_svg(
                result.get("itinerary"), overlay=plan_a)
        except Exception as exc:                              # noqa: BLE001
            result["_map_svg"] = ""
            result["_map_error"] = f"{type(exc).__name__}: {exc}"
        try:
            result["_links"] = deep_links(result)
        except Exception:                                    # noqa: BLE001
            result["_links"] = []
    return result


def handle_ask(payload: dict, now: datetime | None = None,
               live: bool | None = None, provider=None) -> tuple[dict, int]:
    """The /api/ask pipeline, HTTP-free so it is directly testable.

    Captures ONE campus-local request timestamp (or accepts the one the HTTP
    handler already captured) and threads it through parsing and planning, so
    every calculated time agrees with `_time.evaluated_at`. Returns
    (result, http_status).
    """
    if live is None:
        live = not config.CACHE_ONLY
    if now is None:
        now = config.now(TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    else:
        now = now.astimezone(TZ)
    meta = time_meta(now, live=live)

    try:
        origin_key, origin = resolve_origin(payload)
    except Exception as exc:                                   # noqa: BLE001
        return {"error": f"origin resolution failed: {exc}", "_time": meta}, 500

    agent_failure = None
    if payload.get("scenario_id"):
        match = next((s for s in SCENARIOS
                      if s["id"] == payload["scenario_id"]), None)
        if match is None:
            return {"error": "unknown scenario_id", "_time": meta}, 400
        call = dict(match)
        guard = _preset_deadline_guard(call, now, live)
        if guard is not None:
            call["_clarification"] = guard
        elif live:
            _materialize_live_preset(call, now)
    else:
        text = str(payload.get("text", ""))
        # One deterministic pre-pass supplies BOTH the hard constraints the model
        # may never weaken and the planner window (request start -> the deadline
        # the student actually stated). The model is never asked to invent either.
        safety_call = parse_free_text(text, now=now, live=live)
        # The global replay switch is stronger than a per-call clock override:
        # DEMO_MODE=cache must never contact a language provider.
        active_provider = provider if live and not config.CACHE_ONLY else None
        if active_provider is None and live and not config.CACHE_ONLY:
            active_provider = configured_agent_provider()
        safety_clarification = safety_call.get("_clarification") or {}
        # Two clarifications are exact factual claims about the clock or about
        # safety, and deterministic code already answers them correctly: an
        # unparsed allergy must never be weakened, and a passed deadline must be
        # corrected rather than re-planned into an invalid window.
        if (active_provider is not None
                and safety_clarification.get("kind")
                not in ("allergen_unparsed", "deadline_passed")):
            required_prefs = dict(safety_call.get("prefs") or {})
            required_prefs.update(_agent_hard_constraints(text))
            explicit_from = str(payload.get("from_place") or "").strip()
            explicit_to = str(payload.get("to_place") or "").strip()
            if explicit_from and explicit_from.lower() not in ("auto", "default"):
                required_prefs["from_place"] = explicit_from
            if explicit_to and explicit_to.lower() not in ("auto", "default"):
                required_prefs["to_place"] = explicit_to
            schedule = payload.get("schedule")
            if not isinstance(schedule, list):
                schedule = []
            context = AgentContext(
                now=now, schedule=schedule[:200], origin_place=origin_key,
                required_prefs=required_prefs, is_replay=False,
                plan_start=safety_call.get("start"),
                plan_end=safety_call.get("end"))
            try:
                result = hokie_agent.run_agent(
                    text, provider=active_provider, context=context)
                return _decorate_agent_plan(result, origin, now, live), 200
            except hokie_agent.AgentUnavailable as exc:
                # Fall through to the existing deterministic parser/planner.
                # The typed provider failure is safe metadata, not raw internals.
                agent_failure = exc.to_dict()
        call = parse_free_text(text, now=now, live=live)

    # Copy prefs rather than mutating the shared SCENARIOS entry.
    call["prefs"] = dict(call.get("prefs") or {})
    if origin_key:
        call["prefs"]["from_place"] = origin_key
    else:
        # An explicit origin `resolve_origin` could not turn into a place is
        # preserved instead of silently defaulting. Device precedence is intact:
        # a valid lat/lon yields origin_key, so this branch only runs otherwise.
        explicit_from = str(payload.get("from_place") or "").strip()
        if explicit_from and explicit_from.lower() not in ("auto", "default"):
            call["prefs"]["from_place"] = explicit_from
    destination = str(payload.get("to_place") or "").strip()
    if destination and destination.lower() not in ("auto", "default"):
        destination_row = config.lookup_place(destination)
        if destination_row and not destination_row.get("dynamic"):
            call["prefs"]["to_place"] = destination
        elif destination_row is None:
            # An explicit unknown destination must not default to McBryde; the
            # planner reports `unknown_place` and the UI can ask for a real one.
            call["prefs"]["to_place"] = destination

    try:
        result = run_plan(call, origin, now=now)
    except Exception as exc:                                   # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "_time": meta}, 500
    result["_time"] = meta
    if agent_failure is not None:
        result["agent"] = agent_failure
        result["provenance"] = {
            "provider": "unavailable", "model": None, "tool_names": [],
            "replay": not live, "fallback": "bounded_parser",
        }
    return result, 200


def status() -> dict:
    fixtures = cache.stats()
    ages = {}
    for name, params in (("bt_buses", {}), ("dining_menu",
                                            {"location_num": "15",
                                             "dtdate": "09/19/2026"})):
        a = cache.age_seconds(name, params)
        ages[name] = None if a is None else round(a / 3600.0, 1)
    live = tools.get_live_bus(source=None)
    ai_provider = os.environ.get("HOKIEFLOW_AI_PROVIDER", "").strip().lower()
    ai_model = os.environ.get("GEMINI_MODEL", "").strip()
    ai_enabled = bool(ai_provider == "gemini"
                      and os.environ.get("GEMINI_API_KEY", "").strip()
                      and ai_model and not config.CACHE_ONLY)
    return {
        "mode": config.DEMO_MODE,
        "offline": config.CACHE_ONLY,
        "clock": config.now().isoformat(timespec="seconds"),
        "clock_pinned_to_snapshot": config.CACHE_ONLY,
        "wall_clock": datetime.now(TZ).isoformat(timespec="seconds"),
        "campus_now": config.now(TZ).strftime("%a %d %b %Y %H:%M"),
        "fixtures": fixtures["count"],
        "fixture_age_hours": ages,
        "live_vehicles": len(live.get("buses", live) or []),
        "live_stale": live.get("stale"),
        "agent": {
            "name": "HokieFlow AI", "enabled": ai_enabled,
            "provider": "gemini" if ai_enabled else None,
            "model": ai_model if ai_enabled else None,
            "fallback": "bounded_parser",
        },
        "assumptions": {
            "eat_minutes": tools.EAT_MINUTES,
            "meal_ranking": tools.MEAL_RANKING_RULE,
            "meal_ranking_note": "a demo convenience, not dietary advice",
            "min_board_buffer_min": tools.MIN_BOARD_BUFFER_MIN,
            "walk_speed_mps": config.WALK_SPEED_MPS,
            "walk_path_factor": config.WALK_PATH_FACTOR,
            "bus_full_pct": config.BUS_FULL_PCT,
        },
        "caveats": [
            "blank allergen field means UNKNOWN, except in a documented "
            "allergen-free kitchen (Viridian) -- see venue_allergen_free",
            ("live bus positions are a replayed snapshot; sched_delta_min is "
             "computed against the pinned snapshot clock"
             if config.CACHE_ONLY else
             "live bus positions come from the upstream poll; sched_delta_min "
             "is computed against the live wall clock"),
            "walk times use a straight-line path factor, not a routed path; "
            "most configured place coordinates still need field verification",
            "diet tags are cross-checked against declared allergens; "
            "self-contradictory rows are not recommended",
        ],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # quieter console
        if "/api/" not in (args[0] if args else ""):
            return

    def _send(self, code: int, body: bytes, ctype: str, extra_headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers:
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200, extra_headers: list[tuple[str, str]] | None = None) -> None:
        self._send(code, json.dumps(obj, default=str, indent=1).encode("utf-8"),
                   "application/json; charset=utf-8", extra_headers)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            try:
                qs = parse_qs(raw.decode("utf-8"))
                return {k: v[0] if len(v) == 1 else v for k, v in qs.items()}
            except Exception:
                return {}

    def _assets(self, path: str) -> bool:
        """Serve the shipped ui/ design (and legacy app/index.html assets)."""
        asset = ui_files.serve(path)
        if asset is not None:
            code, body, ctype, extra = asset
            self._send(code, body, ctype, extra)
            return True
        if path == "/manifest.webmanifest":   # legacy demo app constants
            self._send(200, json.dumps(MANIFEST).encode("utf-8"),
                       "application/manifest+json; charset=utf-8")
            return True
        if path == "/icon.svg":
            self._send(200, ICON_SVG.encode("utf-8"), "image/svg+xml")
            return True
        if path == "/favicon.ico":           # keep the console clean
            self._send(204, b"", "image/x-icon")
            return True
        if path in ("/styles.css", "/app.js"):
            legacy = Path(__file__).parent / path.lstrip("/")
            try:
                ctype = ("text/css; charset=utf-8" if path.endswith(".css")
                         else "text/javascript; charset=utf-8")
                self._send(200, legacy.read_bytes(), ctype)
            except OSError:
                self._send(404, b"{}", "application/json; charset=utf-8")
            return True
        return False

    def do_GET(self) -> None:                 # noqa: N802
        path = self.path.split("?")[0]
        # One guard for every account route: with the optional auth packages
        # missing, answer 503 with the install hint rather than raising from an
        # unwrapped call site and killing the connection.
        if (path.startswith("/api/auth/") or path == "/api/account/data") \
                and not AUTH_AVAILABLE:
            self._json({"error": "accounts are unavailable; install the optional "
                                 "dependencies with 'pip install -r requirements.txt'",
                        "status": "unavailable",
                        "detail": AUTH_UNAVAILABLE_REASON}, 503)
            return
        query = parse_qs(urlparse(self.path).query)
        if self._assets(path):
            return
        if path == "/api/auth/me":
            try:
                user = require_auth({"Authorization": self.headers.get("Authorization"), "Cookie": self.headers.get("Cookie")})
                self._json(ui_files.enrich_account({"user": ui_files.user_ref(user)}, user), 200)
            except PermissionError:
                self._json({"user": None}, 200)
            return
        if path == "/api/auth/google":
            state = secrets.token_urlsafe(32)
            redirect = start_google_oauth({"state": state})
            self._send(302, b"", "text/plain; charset=utf-8",
                       [("Location", redirect),
                        ("Set-Cookie", set_oauth_state_cookie(state))])
            return
        if path == "/api/auth/google/callback":
            params = {key: values[0] for key, values in query.items()}
            state = get_oauth_state_cookie(self.headers.get("Cookie"))
            try:
                result = handle_google_oauth_callback(params, {"state": state})
                self._send(302, b"", "text/plain; charset=utf-8",
                           [("Location", "/"),
                            ("Set-Cookie", set_session_cookie(result["session"])),
                            ("Set-Cookie", clear_oauth_state_cookie())])
            except ValueError as exc:
                message = quote(str(exc), safe="")
                self._send(302, b"", "text/plain; charset=utf-8",
                           [("Location", f"/?auth_error={message}"),
                            ("Set-Cookie", clear_oauth_state_cookie())])
            return
        if path == "/api/time":
            # Lightweight: the clock only -- no GTFS, dining, or live-bus load.
            self._json(time_endpoint())
            return
        if path == "/api/status":
            self._json(status())
            return
        if path == "/api/scenarios":
            self._json([{k: s[k] for k in ("id", "label", "text")} for s in SCENARIOS])
            return
        if path == "/api/origins":
            # Static campus places, for when the device will not give a position
            # (geolocation needs a SECURE CONTEXT: localhost or HTTPS). Read
            # through the locked snapshot so a concurrent device registration
            # cannot leak another session's coordinates or race the iteration.
            self._json([{"key": k, "verified": bool(v.get("verified"))}
                        for k, v in config.static_places()])
            return
        if path == "/api/dining/places":
            self._json(ui_files.dining_places_endpoint())
            return
        if path == "/api/transit/stops":
            self._json(ui_files.transit_stops_endpoint())
            return
        if path == "/api/transit/departures":
            self._json(ui_files.transit_departures_endpoint(
                query.get("stop", [""])[0]))
            return
        if path == "/api/map/buildings":
            self._json(ui_files.buildings_endpoint(query.get("q", [])))
            return
        if path == "/api/map/state":
            self._json(ui_files.map_state_endpoint(config.now(TZ)))
            return
        if path == "/api/raw":
            n = int((parse_qs(urlparse(self.path).query).get("n", ["1"])[0]))
            idx = max(0, min(len(SCENARIOS) - 1, n - 1))
            try:
                result, code = handle_ask({"scenario_id": SCENARIOS[idx]["id"]})
                self._json(result, code)
            except Exception as exc:                      # noqa: BLE001
                self._json({"error": f"{type(exc).__name__}: {exc}",
                            "_time": time_meta()}, 500)
            return
        self._json({"error": "not found"}, 404)

    def _account_save(self) -> None:
        """Save account data. Shared by POST (kept) and PUT (what the UI sends).

        ui/app.js calls `PUT /api/account/data`, while this server originally
        implemented the route only inside do_POST and defined no do_PUT at all.
        BaseHTTPRequestHandler then answered the UI's request with a bare 501 and
        closed the connection, so saving a class, a plan or a schedule event
        failed in the deployed app while working against serve.py (which does
        implement do_PUT). One implementation now serves both verbs.
        """
        if not AUTH_AVAILABLE:
            self._json({"error": "accounts are unavailable; install the optional "
                                 "dependencies with 'pip install -r requirements.txt'",
                        "status": "unavailable",
                        "detail": AUTH_UNAVAILABLE_REASON}, 503)
            return
        try:
            user = require_auth({"Authorization": self.headers.get("Authorization"),
                                 "Cookie": self.headers.get("Cookie")})
        except PermissionError:
            self._json({"error": "authentication required"}, 401)
            return
        payload = self._read_json()
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        same_origin = origin in (None, f"https://{host}", f"http://{host}")
        if not same_origin or self.headers.get("X-HokieFlow-Request") != "1":
            self._json({"error": "forbidden"}, 403)
            return
        result, code = ui_files.save_account(user, payload.get("data"),
                                             payload.get("version"))
        self._json(result, code)

    def do_PUT(self) -> None:                 # noqa: N802
        """Only the one PUT route the client uses; 404 for anything else."""
        path = self.path.split("?")[0]
        if path == "/api/account/data":
            self._account_save()
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:                # noqa: N802
        path = self.path.split("?")[0]
        # One guard for every account route: with the optional auth packages
        # missing, answer 503 with the install hint rather than raising from an
        # unwrapped call site and killing the connection.
        if (path.startswith("/api/auth/") or path == "/api/account/data") \
                and not AUTH_AVAILABLE:
            self._json({"error": "accounts are unavailable; install the optional "
                                 "dependencies with 'pip install -r requirements.txt'",
                        "status": "unavailable",
                        "detail": AUTH_UNAVAILABLE_REASON}, 503)
            return
        if path == "/api/auth/register":
            payload = self._read_json()
            try:
                user = register_user(payload.get("email"), payload.get("password"))
                session_cookie = set_session_cookie({"access_token": user.get("session", {}).get("access_token"), "refresh_token": user.get("session", {}).get("refresh_token"), "expires_at": user.get("session", {}).get("expires_at") or ""})
                self._json({"user": user.get("user")}, 201, [("Set-Cookie", session_cookie)])
            except Exception as exc:                           # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return
        if path == "/api/auth/login":
            payload = self._read_json()
            try:
                user = login_user(payload.get("email"), payload.get("password"))
                session_cookie = set_session_cookie({"access_token": user.get("session", {}).get("access_token"), "refresh_token": user.get("session", {}).get("refresh_token"), "expires_at": user.get("session", {}).get("expires_at") or ""})
                self._json({"user": user.get("user")}, 200, [("Set-Cookie", session_cookie)])
            except Exception as exc:                           # noqa: BLE001
                self._json({"error": str(exc)}, 401)
            return
        if path == "/api/auth/logout":
            self._json({"ok": True}, 200, [("Set-Cookie", clear_session_cookie())])
            return
        if path == "/api/auth/me":
            try:
                user = require_auth({"Authorization": self.headers.get("Authorization"), "Cookie": self.headers.get("Cookie")})
                self._json(ui_files.enrich_account({"user": ui_files.user_ref(user)}, user), 200)
            except PermissionError:
                self._json({"user": None}, 200)
            return
        if path == "/api/account/data":
            self._account_save()
            return
        if path == "/api/auth/google":
            payload = self._read_json() if self.headers.get("Content-Type", "").lower().startswith("application/json") else {}
            state = payload.get("state") or secrets.token_urlsafe(32)
            redirect = start_google_oauth({"state": state})
            self._json({"redirect": redirect}, 200, [("Set-Cookie", set_oauth_state_cookie(state))])
            return
        if path == "/api/auth/google/callback":
            params = parse_qs(urlparse(self.path).query)
            data = {k: v[0] for k, v in params.items()}
            state = get_oauth_state_cookie(self.headers.get("Cookie"))
            try:
                result = handle_google_oauth_callback(data, {"state": state})
                self._json({"user": result.get("user")}, 200,
                           [("Set-Cookie", set_session_cookie(result["session"])),
                            ("Set-Cookie", clear_oauth_state_cookie())])
            except ValueError as exc:
                self._json({"error": str(exc)}, 400, [("Set-Cookie", clear_oauth_state_cookie())])
            return
        if path == "/api/route":
            payload = self._read_json()
            try:
                result, code = ui_files.route_endpoint(payload)
                self._json(result, code)
            except Exception as exc:                      # noqa: BLE001
                self._json({"error": f"{type(exc).__name__}: {exc}", "status": "unavailable"}, 502)
            return
        if self.path.split("?")[0] != "/api/ask":
            self._json({"error": "not found"}, 404)
            return
        # Capture ONE campus-local timestamp at the request boundary, before the
        # body is parsed or any planning runs. This is the authority for every
        # time in the response; the browser clock is never consulted.
        now = config.now(TZ)
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 1_000_000:
            self._json({"error": "request body too large",
                        "_time": time_meta(now)}, 413)
            return
        payload = self._read_json()
        if (isinstance(payload, dict) and not payload.get("scenario_id")
                and str(payload.get("text") or "").strip()
                and not config.CACHE_ONLY
                and configured_agent_provider() is not None
                and not _client_agent_budget_available(self.client_address[0])):
            self._json({
                "error": "AI request limit reached for this client; try later",
                "agent": {"status": "unavailable", "code": "client_rate_limit"},
                "_time": time_meta(now),
            }, 429)
            return
        try:
            result, code = handle_ask(payload, now=now)
            self._json(result, code)
        except Exception as exc:                           # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}",
                        "_time": time_meta(now)}, 500)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8321)
    ap.add_argument("--host", default=None,
                    help="bind address; defaults to 127.0.0.1, or 0.0.0.0 with --lan")
    ap.add_argument("--lan", action="store_true",
                    help="bind 0.0.0.0 so a phone on the same Wi-Fi can open it")
    args = ap.parse_args()
    if args.host is None:
        args.host = "0.0.0.0" if args.lan else "127.0.0.1"

    st = status()
    print("=" * 68)
    print("HokieFlow demo server")
    print("=" * 68)
    print(f"  mode        : {st['mode']}   offline={st['offline']}")
    clock_note = ("pinned to the snapshot"
                  if st["clock_pinned_to_snapshot"] else "live wall clock")
    print(f"  campus now  : {st['campus_now']}  ({clock_note})")
    print(f"  fixtures    : {st['fixtures']}")
    print(f"  live buses  : {st['live_vehicles']}   stale={st['live_stale']}")

    # Bind BEFORE announcing a URL, so we never print an address we did not get.
    # Binding can fail because a previous instance is still running -- easy on
    # demo day, and a raw traceback is a bad look. Step to the next free port and
    # say plainly which one we actually bound.
    httpd = None
    for port in range(args.port, args.port + 6):
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            print(f"  port {port} is busy (an older HokieFlow server still "
                  f"running?), trying {port + 1}")
    if httpd is None:
        print(f"\n  Could not bind any port in {args.port}-{args.port + 5}.\n"
              f"  Stop the other instance, or run: python3 app/server.py --port 9000")
        return 1

    bound = httpd.server_address[1]
    print(f"\n  open on this machine : http://127.0.0.1:{bound}/")
    if args.lan or args.host == "0.0.0.0":
        ips = lan_ips()
        if ips:
            print("  open on a phone     : " +
                  "  ".join(f"http://{ip}:{bound}/" for ip in ips))
            print("                        (same Wi-Fi; if it will not load, the")
            print("                         network is blocking device-to-device traffic)")
        else:
            print("  --lan: could not determine a LAN address")
    else:
        print("  (add --lan to reach it from a phone on the same Wi-Fi)")
    print("  Ctrl-C to stop\n")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())