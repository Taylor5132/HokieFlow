"""Virginia Tech campus events (events.vt.edu) — normalized model + query contract.

Source reality (verified 2026-09-19 against the live site):
  * ``events.vt.edu`` is **AEM / Ensemble CMS**, NOT Localist. There is **no
    JSON, RSS or ICS feed**. The public surface is server-rendered HTML.
  * ``https://events.vt.edu/sitemap.xml`` lists every canonical page with an
    optional ``<lastmod>``. Event detail pages live under
    ``/events/<year>/<month>/<slug>.html`` (note the ``/2026/09/`` segment).
  * A month page ``/events/2026/09.html`` is advertised by the sitemap but can
    legitimately 404 (observed 2026-09-19). The crawler must degrade to the
    year listing and the sitemap instead of failing the whole run.
  * Detail pages carry schema.org microdata: ``itemprop="startDate"`` /
    ``endDate`` (quirky ``2026-09-22T10:00Z-0400`` form), ``location`` with a
    building + ``Blacksburg, VA 24061`` address, ``offers/price`` and a
    ``meta name="keywords"`` tag list.

Ownership / rules
-----------------
* This module is pure stdlib and **never touches the network**; the edge
  crawler (``scripts/crawl_events.py``) owns fetching and calls the parsers
  here. That keeps every parser testable offline with ``DEMO_MODE=cache``.
* Copyright/PII minimization: an ``Event`` deliberately has **no**
  description, image, email or phone field. ``parse_detail`` drops the
  contact/accessibility blocks entirely and only keeps a boolean
  ``accessibility`` indicator. ``summary`` is opt-in and truncated + scrubbed.

The user flow this contract serves:
  browse upcoming September events -> text search -> filter by
  date/category/free-food/in-person -> open detail/source -> add to schedule.
  HokieFlow may recommend an event only when it fits **fully inside** a free
  gap (``fits_gap`` / ``recommendable``).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import config

# ---------------------------------------------------------------- constants
CAMPUS_TZ = config.CAMPUS_TZ
_EVT_TZ = ZoneInfo(CAMPUS_TZ)

# The snapshot is restricted to this window by contract.
MONTH_KEY = "2026-09"
MONTH_YEAR = 2026
MONTH_NUMBER = 9
MONTH_START = date(MONTH_YEAR, MONTH_NUMBER, 1)
MONTH_END = date(MONTH_YEAR, MONTH_NUMBER, 30)

# How old a snapshot may be before the UI must label it stale.
EVENTS_STALE_AFTER_H = 24
# Consecutive crawls an event may be missing from the source before we call it
# cancelled rather than "cancelled-uncertain". One miss is not enough: the
# listing rotates and a detail page can be temporarily unlinked.
MISS_THRESHOLD = 2

# status values (a plain closed set, not an Enum, so JSON stays readable)
STATUS_SCHEDULED = "scheduled"
STATUS_CANCELLED_UNCERTAIN = "cancelled-uncertain"
STATUS_CANCELLED = "cancelled"
STATUS_PARSER_FAILED = "parser-failed"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"

# snapshot / browse state values
STATE_OK = "ok"
STATE_EMPTY = "empty"
STATE_STALE = "stale"
STATE_PARTIAL = "partial"
STATE_NO_MATCH = "no-match"

ADMISSION_FREE = "free"
ADMISSION_PAID = "paid"
ADMISSION_UNKNOWN = "unknown"

# Substrings that place an event in the Blacksburg area. The address string on
# a VT detail page is the most reliable signal; venue names are a fallback for
# rows whose address is blank.
_BLACKSBURG_ADDRESS_HINTS = ("blacksburg", "24061", "24060")
_BLACKSBURG_VENUE_HINTS = (
    "drillfield", "moss arts center", "squires", "burruss", "hahn", "mcbryde",
    "newman library", "goodwin", "lane stadium", "cassell", "dietrick",
    "owens", "torgersen", "kelly hall", "duck pond", "alumni mall",
    "war memorial", "pamplin", "ferguson", "hillcrest", "inn at virginia tech",
    "veterinary", "steger", "price hall", "holden hall", "derring",
)

# PII scrubber: summary is the only free-text field, and even it must not leak
# an email address or phone number from the source page.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")


class EventParseError(ValueError):
    """Raised when a detail page cannot be normalized (no title or no start)."""


# ---------------------------------------------------------------- time helpers
_LEADING_Z_OFFSET_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)Z([+-])(\d{2}):?(\d{2})$"
)


def parse_event_time(raw: str | None) -> datetime | None:
    """Parse an events.vt.edu microdata timestamp into an aware datetime.

    The site emits ``2026-09-22T10:00Z-0400`` — a literal ``Z`` immediately
    followed by a numeric offset, which is NOT valid ISO-8601 and that
    ``datetime.fromisoformat`` rejects. We normalize it to
    ``2026-09-22T10:00-04:00``. A bare ``...Z`` is treated as UTC. A date-only
    value becomes midnight campus-local.

    Returns ``None`` for ``None``/empty; raises ``ValueError`` for garbage so
    the caller can record a parser failure instead of guessing.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    m = _LEADING_Z_OFFSET_RE.match(s)
    if m:
        s = f"{m.group(1)}{m.group(2)}{m.group(3)}:{m.group(4)}"
    elif s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # date-only ("2026-09-22")
        dt = datetime.fromisoformat(s + "T00:00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_EVT_TZ)
    return dt


def to_campus(dt: datetime) -> datetime:
    """Convert an aware datetime to America/New_York."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_EVT_TZ)
    return dt.astimezone(_EVT_TZ)


def in_month(dt: datetime | None, year: int = MONTH_YEAR, month: int = MONTH_NUMBER) -> bool:
    """True when ``dt`` falls in the given campus-local month."""
    if dt is None:
        return False
    local = to_campus(dt)
    return local.year == year and local.month == month


def in_september_2026(dt: datetime | None) -> bool:
    """True when ``dt`` is inside 2026-09-01..2026-09-30 (campus time)."""
    return in_month(dt, MONTH_YEAR, MONTH_NUMBER)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_EVT_TZ)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=_EVT_TZ)
    return parse_event_time(str(value))


# ---------------------------------------------------------------- ids
def event_id(canonical_url: str) -> str:
    """Deterministic canonical-URL id (stable across crawls)."""
    digest = hashlib.sha1(canonical_url.strip().encode("utf-8")).hexdigest()[:16]
    return f"evt_{digest}"


# ---------------------------------------------------------------- models
@dataclass(frozen=True)
class SitemapEntry:
    url: str
    lastmod: str | None = None


@dataclass(frozen=True)
class EventCard:
    """Metadata harvested from a listing page card (no detail fetch needed).

    Listing cards encode filters in their CSS classes, e.g.
    ``categories arts ... types in-person admission paid ... locations
    blacksburg-va-24061``. This is the cheap signal the crawler uses to order
    and prefilter work before spending a polite request on a detail page.
    """

    url: str
    title: str
    category: str | None = None
    admission: str = ADMISSION_UNKNOWN
    in_person: bool | None = None
    free_food: bool = False
    location_hint: str | None = None
    month: str | None = None


@dataclass(frozen=True)
class Event:
    """Normalized event. Deliberately contains no description/image/contact PII."""

    id: str
    source_url: str
    canonical_url: str
    title: str
    start: str                       # ISO-8601 with offset, campus-local preferred
    end: str | None
    timezone: str
    location_name: str
    address: str
    tags: tuple[str, ...]
    category: str | None
    admission: str                   # free | paid | unknown
    registration_url: str | None
    accessibility: bool
    source_lastmod: str | None
    fetched_at: str
    status: str = STATUS_SCHEDULED
    summary: str | None = None
    in_person: bool | None = None
    free_food: bool = False
    miss_streak: int = 0
    all_day: bool = False

    # ---- derived helpers -------------------------------------------------
    @property
    def start_dt(self) -> datetime | None:
        return parse_event_time(self.start)

    @property
    def end_dt(self) -> datetime | None:
        return parse_event_time(self.end)

    @property
    def location_known(self) -> bool:
        return bool((self.location_name or "").strip() or (self.address or "").strip())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_url": self.source_url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "timezone": self.timezone,
            "location_name": self.location_name,
            "address": self.address,
            "tags": list(self.tags),
            "category": self.category,
            "admission": self.admission,
            "registration_url": self.registration_url,
            "accessibility": self.accessibility,
            "source_lastmod": self.source_lastmod,
            "fetched_at": self.fetched_at,
            "status": self.status,
            "summary": self.summary,
            "in_person": self.in_person,
            "free_food": self.free_food,
            "miss_streak": self.miss_streak,
            "all_day": self.all_day,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "Event":
        return cls(
            id=str(row["id"]),
            source_url=str(row["source_url"]),
            canonical_url=str(row.get("canonical_url") or row["source_url"]),
            title=str(row.get("title") or ""),
            start=str(row.get("start") or ""),
            end=(str(row["end"]) if row.get("end") else None),
            timezone=str(row.get("timezone") or CAMPUS_TZ),
            location_name=str(row.get("location_name") or ""),
            address=str(row.get("address") or ""),
            tags=tuple(str(t) for t in (row.get("tags") or ())),
            category=(str(row["category"]) if row.get("category") else None),
            admission=str(row.get("admission") or ADMISSION_UNKNOWN),
            registration_url=(str(row["registration_url"]) if row.get("registration_url") else None),
            accessibility=bool(row.get("accessibility")),
            source_lastmod=(str(row["source_lastmod"]) if row.get("source_lastmod") else None),
            fetched_at=str(row.get("fetched_at") or ""),
            status=str(row.get("status") or STATUS_SCHEDULED),
            summary=(str(row["summary"]) if row.get("summary") else None),
            in_person=row.get("in_person"),
            free_food=bool(row.get("free_food")),
            miss_streak=int(row.get("miss_streak") or 0),
            all_day=bool(row.get("all_day")),
        )


@dataclass(frozen=True)
class Gap:
    """A free window in a student's schedule (campus-local aware datetimes)."""

    start: datetime
    end: datetime
    label: str = ""


@dataclass(frozen=True)
class BrowseResult:
    events: tuple[Event, ...]
    total: int
    state: str
    notices: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "events": [e.to_dict() for e in self.events],
            "total": self.total,
            "state": self.state,
            "notices": list(self.notices),
        }


# ---------------------------------------------------------------- parsing
def parse_sitemap(xml_text: str) -> list[SitemapEntry]:
    """Parse a sitemap.org ``<urlset>`` into ordered ``SitemapEntry`` rows.

    Tolerant by design: a missing ``<lastmod>`` yields ``None`` (the crawler
    then falls back to a content hash), and relative ``<loc>`` values are kept
    as-is for the caller to resolve.
    """
    entries: list[SitemapEntry] = []
    for block in re.findall(r"<url>(.*?)</url>", xml_text, re.S | re.I):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.S | re.I)
        if not loc:
            continue
        lastmod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block, re.S | re.I)
        entries.append(SitemapEntry(
            url=_html.unescape(loc.group(1).strip()),
            lastmod=(_html.unescape(lastmod.group(1).strip()) if lastmod else None),
        ))
    return entries


def sitemap_month_urls(entries: Sequence[SitemapEntry], year: int = MONTH_YEAR,
                       month: int = MONTH_NUMBER) -> list[SitemapEntry]:
    """Filter sitemap entries to detail pages under ``/events/<year>/<MM>/``."""
    needle = f"/events/{year}/{month:02d}/"
    return [e for e in entries if needle in e.url]


def _strip_tags(text: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _first(html_text: str, pattern: str, group: int = 1, flags: int = re.S) -> str | None:
    m = re.search(pattern, html_text, flags)
    if not m:
        return None
    return _collapse(_strip_tags(m.group(group)))


_LISTING_CARD_RE = re.compile(
    r'<li\s+class="([^"]*event-page[^"]*)"(.*?)</li>', re.S | re.I)


def parse_listing(html_text: str) -> list[EventCard]:
    """Extract listing cards (URL, title, class-encoded filters) from a listing page.

    The card CSS classes are the *only* cheap source of category / admission /
    in-person / free-food / location for events we choose not to detail-fetch.
    """
    cards: list[EventCard] = []
    for m in _LISTING_CARD_RE.finditer(html_text):
        classes, body = m.group(1), m.group(2)
        link = re.search(
            r'vt-list-item-title-link"\s+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S)
        if not link:
            continue
        url = _html.unescape(link.group(1).strip())
        title = _collapse(_strip_tags(
            re.sub(r'<span class="sr-only">.*?</span>', "", link.group(2), flags=re.S)))
        if not title:
            continue
        cat = _class_value(classes, "categories")
        admission = _admission_from_class(classes)
        types = re.findall(r"(?:^|\s)types ([\w-]+)", classes)
        in_person: bool | None
        if "online" in types:
            in_person = False
        elif "in-person" in types or "hybrid" in types:
            in_person = True
        else:
            in_person = None
        feats = re.findall(r"(?:^|\s)features ([\w-]+)", classes)
        loc = _class_value(classes, "locations")
        month_m = re.search(r"/events/(\d{4})/(\d{2})/", url)
        cards.append(EventCard(
            url=url,
            title=title,
            category=cat,
            admission=admission,
            in_person=in_person,
            free_food=("free-food" in feats),
            location_hint=loc,
            month=(f"{month_m.group(1)}-{month_m.group(2)}" if month_m else None),
        ))
    return cards


def _class_value(classes: str, kind: str) -> str | None:
    m = re.search(rf"(?:^|\s){kind} ([\w-]+)", classes)
    if not m:
        return None
    value = m.group(1)
    # ignore the variant marker class (e.g. "categories_-arts")
    return None if value.startswith("_") else value


def _admission_from_class(classes: str) -> str:
    # The listing emits BOTH the filter class and a variant marker, e.g.
    # "admission paid" and "admission_-paid". Either identifies the value.
    for value in re.findall(r"(?:^|\s)admission[_ ]([\w-]+)", classes):
        value = value.strip("-_")
        if value == "free":
            return ADMISSION_FREE
        if value == "paid":
            return ADMISSION_PAID
    return ADMISSION_UNKNOWN


def parse_tags(html_text: str) -> tuple[str, ...]:
    """Merged, order-preserving tags from ``meta keywords`` + tag links."""
    tags: list[str] = []
    meta = re.search(r'<meta\s+name="keywords"\s+content="([^"]*)"', html_text, re.I)
    if meta:
        for t in _html.unescape(meta.group(1)).split(";"):
            t = t.strip()
            if t:
                tags.append(t)
    for m in re.finditer(r'class="vt-tag-link"[^>]*>(.*?)</a>', html_text, re.S | re.I):
        t = _collapse(_strip_tags(m.group(1)))
        if t:
            tags.append(t)
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return tuple(out)


def _find_registration_url(html_text: str) -> str | None:
    """First plausible registration/RSVP/ticket link, excluding map/social/PII."""
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html_text, re.S | re.I):
        href = _html.unescape(m.group(1).strip())
        low = href.lower()
        if low.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        blob = (href + " " + _strip_tags(m.group(2))).lower()
        if re.search(r"map|share|facebook|twitter|instagram|linkedin|youtube|"
                     r"add-to-calendar|ical", blob):
            continue
        if re.search(r"register|registration|rsvp|sign.?up|ticket", blob):
            return href
    return None


def scrub_pii(text: str | None) -> str | None:
    """Remove email addresses and phone numbers from free text."""
    if not text:
        return text
    cleaned = _EMAIL_RE.sub("[redacted]", text)
    cleaned = _PHONE_RE.sub("[redacted]", cleaned)
    return cleaned


def extract_summary(html_text: str, limit: int = 160) -> str | None:
    """A SHORT, scrubbed first paragraph of the description (opt-in only).

    We never store the full description: it is copyrighted editorial content.
    """
    m = re.search(r'itemprop="description"[^>]*>(.*?)</div>', html_text, re.S | re.I)
    if not m:
        return None
    text = _collapse(_strip_tags(m.group(1)))
    if not text:
        return None
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return scrub_pii(text)


def parse_detail(html_text: str, source_url: str, *,
                 fetched_at: str,
                 source_lastmod: str | None = None,
                 card: EventCard | None = None,
                 include_summary: bool = False,
                 timezone_name: str = CAMPUS_TZ) -> Event:
    """Normalize one event detail page.

    Raises ``EventParseError`` when required fields (title, startDate) are
    missing so the crawler records an explicit parser failure rather than
    inventing an event.
    """
    canonical = _first(html_text, r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"') \
        or _first(html_text, r'<meta[^>]+property="og:url"[^>]+content="([^"]+)"') \
        or source_url

    title = _first(html_text, r"<h1[^>]*>(.*?)</h1>") \
        or _first(html_text, r'itemprop="name"[^>]*>(.*?)</')
    if not title:
        raise EventParseError(f"no title in {source_url}")

    start_raw = _first(html_text, r'itemprop="startDate"[^>]*content="([^"]+)"')
    if not start_raw:
        raise EventParseError(f"no startDate in {source_url}")
    try:
        start_dt = parse_event_time(start_raw)
        end_dt = parse_event_time(
            _first(html_text, r'itemprop="endDate"[^>]*content="([^"]+)"'))
    except ValueError as exc:                        # malformed timestamp
        raise EventParseError(f"bad timestamp in {source_url}: {exc}") from exc
    if start_dt is None:
        raise EventParseError(f"empty startDate in {source_url}")

    location_name = _first(
        html_text, r'id="vt_event_location_building"[^>]*>\s*(.*?)\s*</span>') or ""
    street = _first(
        html_text, r'id="vt_event_location_address"[^>]*>\s*(.*?)\s*</span>') or ""
    city = _first(
        html_text, r'id="vt_event_location_3"[^>]*>\s*(.*?)\s*</span>') or ""
    parts: list[str] = []
    for part in (street, city):
        if part and part.lower() not in ", ".join(parts).lower():
            parts.append(part)
    address = ", ".join(parts)
    if not location_name and not address:
        # fall back to the whole address block's text
        block = _first(html_text, r'itemprop="address"[^>]*>(.*?)</span>\s*</span>')
        if block:
            location_name = block

    tags = parse_tags(html_text)

    admission = _admission_from_detail(html_text)
    if admission == ADMISSION_UNKNOWN and card is not None:
        admission = card.admission

    category = card.category if card is not None else None
    if category is None:
        category = _category_from_tags(tags)

    in_person = _in_person_from_tags(tags)
    if in_person is None and card is not None:
        in_person = card.in_person
    free_food = any(_norm(t) in ("free food", "free-food", "food provided", "refreshments")
                    for t in tags) or (card.free_food if card else False)

    accessibility = bool(
        re.search(r"vt-event-access-contacts-text|itemprop=\"accessibility\"", html_text)
        or any(_norm(t).startswith("accessib") for t in tags)
    )

    summary = extract_summary(html_text) if include_summary else None

    return Event(
        id=event_id(canonical),
        source_url=source_url,
        canonical_url=canonical,
        title=title,
        start=to_campus(start_dt).isoformat(timespec="minutes"),
        end=(to_campus(end_dt).isoformat(timespec="minutes") if end_dt else None),
        timezone=timezone_name,
        location_name=location_name.strip(),
        address=address.strip(),
        tags=tags,
        category=category,
        admission=admission,
        registration_url=_find_registration_url(html_text),
        accessibility=accessibility,
        source_lastmod=source_lastmod,
        fetched_at=fetched_at,
        status=STATUS_SCHEDULED,
        summary=summary,
        in_person=in_person,
        free_food=free_food,
        miss_streak=0,
        all_day=False,
    )


def _admission_from_detail(html_text: str) -> str:
    if re.search(r'itemprop="price"[^>]*content="[Ff]ree"', html_text):
        return ADMISSION_FREE
    if re.search(r'class="vt-event-free"', html_text):
        return ADMISSION_FREE
    price = re.search(r'itemprop="price"[^>]*content="([^"]+)"', html_text)
    if price:
        value = price.group(1).strip().lower()
        if value and value not in ("free", "0", "0.00"):
            return ADMISSION_PAID
    if re.search(r'class="vt-event-paid"', html_text):
        return ADMISSION_PAID
    return ADMISSION_UNKNOWN


def _category_from_tags(tags: Sequence[str]) -> str | None:
    known = {
        "arts", "concert", "cinema", "theatre", "sport", "lecture", "workshop",
        "conference", "symposium", "exhibition", "meeting", "open house",
        "tour", "town hall", "performance", "show and tell", "how-to",
    }
    for t in tags:
        if _norm(t) in known:
            return _norm(t).replace(" ", "-")
    return None


def _in_person_from_tags(tags: Sequence[str]) -> bool | None:
    lowered = {_norm(t) for t in tags}
    if "online" in lowered or "virtual" in lowered:
        return False
    if "in-person" in lowered or "in person" in lowered or "hybrid" in lowered:
        return True
    return None


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", " ", (value or "").lower()).strip()


# ---------------------------------------------------------------- geo / scope
def is_blacksburg(address: str = "", location_name: str = "",
                  tags: Sequence[str] = ()) -> bool:
    """Blacksburg-area predicate.

    Primary signal is the postal address ("Blacksburg, VA 24061"). Venue names
    in the building field are a fallback for rows with a blank/near-blank
    address. Online-only events are never Blacksburg.
    """
    if any(_norm(t) in ("online", "virtual") for t in tags):
        return False
    hay_addr = (address or "").lower()
    hay_loc = (location_name or "").lower()
    if any(h in hay_addr for h in _BLACKSBURG_ADDRESS_HINTS):
        return True
    if any(h in hay_loc for h in _BLACKSBURG_VENUE_HINTS):
        return True
    # A VT-branded building with a 240xx zip anywhere in the combined string.
    combined = f"{hay_addr} {hay_loc}"
    return any(h in combined for h in ("24060", "24061"))


# ---------------------------------------------------------------- query
def _event_start(e: Event) -> datetime | None:
    return e.start_dt


def search(events: Iterable[Event], query: str | None) -> list[Event]:
    """Case-insensitive substring match across title, tags, category, location.

    An empty query is a no-op. Results are ordered by start time (then id) so
    browse/search are deterministic.
    """
    q = (query or "").strip().lower()
    if not q:
        return sorted(events, key=_sort_key)
    out = []
    for e in events:
        hay = " ".join((
            e.title, e.category or "", e.location_name, e.address,
            " ".join(e.tags),
        )).lower()
        if q in hay:
            out.append(e)
    return sorted(out, key=_sort_key)


def _sort_key(e: Event) -> tuple:
    dt = _event_start(e)
    return (dt or datetime.max.replace(tzinfo=timezone.utc), e.id)


def filter_events(events: Iterable[Event], *,
                  date_: date | str | None = None,
                  start: datetime | str | None = None,
                  end: datetime | str | None = None,
                  category: str | None = None,
                  free: bool | None = None,
                  free_food: bool | None = None,
                  in_person: bool | None = None,
                  include_cancelled: bool = True,
                  include_uncertain: bool = True,
                  only_known_location: bool = False) -> list[Event]:
    """Deterministic multi-filter. ``None`` means "do not filter on this"."""
    want_day = None
    if date_ is not None:
        want_day = (date.fromisoformat(date_) if isinstance(date_, str) else date_)
    lo = _as_datetime(start)
    hi = _as_datetime(end)
    cat = _norm(category) if category else None

    out = []
    for e in events:
        if not include_cancelled and e.status == STATUS_CANCELLED:
            continue
        if not include_uncertain and e.status == STATUS_CANCELLED_UNCERTAIN:
            continue
        if only_known_location and not e.location_known:
            continue
        dt = _event_start(e)
        if want_day is not None:
            if dt is None or to_campus(dt).date() != want_day:
                continue
        if lo is not None and (dt is None or dt < lo):
            continue
        if hi is not None and (dt is None or dt > hi):
            continue
        if cat is not None:
            e_cat = _norm(e.category or "")
            if e_cat != cat:
                continue
        if free is not None:
            is_free = e.admission == ADMISSION_FREE
            if is_free != free:
                continue
        if free_food is not None and bool(e.free_food) != free_food:
            continue
        if in_person is not None and e.in_person is not in_person:
            continue
        out.append(e)
    return sorted(out, key=_sort_key)


def browse(snapshot: "Snapshot", *,
           query: str | None = None,
           date_: date | str | None = None,
           start: datetime | str | None = None,
           end: datetime | str | None = None,
           category: str | None = None,
           free: bool | None = None,
           free_food: bool | None = None,
           in_person: bool | None = None,
           include_cancelled: bool = True,
           include_uncertain: bool = True,
           only_known_location: bool = False,
           limit: int | None = None,
           now: datetime | None = None) -> BrowseResult:
    """The single browse/list/search/filter entry point for the UI.

    Returns a ``BrowseResult`` whose ``state`` is one of ``ok``, ``empty``,
    ``no-match``, ``stale`` or ``partial`` so the frontend can render the right
    empty/failure state without re-deriving it.
    """
    notices: list[str] = []
    base_state = snapshot_state(snapshot, now=now)
    if base_state == STATE_STALE:
        notices.append(
            "This event snapshot is older than "
            f"{EVENTS_STALE_AFTER_H} h; times may have changed.")
    if base_state == STATE_PARTIAL:
        notices.append(
            f"{len(snapshot.parser_failures)} event page(s) could not be parsed "
            "and are missing from results.")
    if base_state == STATE_EMPTY:
        return BrowseResult(events=(), total=0, state=STATE_EMPTY,
                            notices=tuple(notices + [
                                "No events match September 2026 Blacksburg coverage."]))

    matched = filter_events(
        snapshot.events, date_=date_, start=start, end=end, category=category,
        free=free, free_food=free_food, in_person=in_person,
        include_cancelled=include_cancelled, include_uncertain=include_uncertain,
        only_known_location=only_known_location)
    matched = search(matched, query)
    total = len(matched)
    if total == 0:
        state = STATE_NO_MATCH if (query or date_ or category or free is not None
                                   or free_food is not None or in_person is not None
                                   or start or end) else base_state
        notices.append("No events found for these filters.")
    else:
        state = base_state
    if limit is not None and limit >= 0:
        matched = matched[:limit]
    return BrowseResult(events=tuple(matched), total=total, state=state,
                        notices=tuple(notices))


# ---------------------------------------------------------------- dedupe
def dedupe(events: Iterable[Event]) -> list[Event]:
    """Collapse true duplicates, preserving distinct recurring occurrences.

    Two rows are the same event when they share a canonical id, OR when the
    normalized (title, start, address) triple matches. Recurring instances have
    different start times, so they survive. The richer row wins.
    """
    by_id: dict[str, Event] = {}
    order: list[str] = []
    for e in events:
        prev = by_id.get(e.id)
        if prev is None:
            by_id[e.id] = e
            order.append(e.id)
        else:
            by_id[e.id] = _richer(prev, e)
    by_triple: dict[tuple, Event] = {}
    triple_order: list[tuple] = []
    for e in by_id.values():
        key = (_norm(e.title), e.start, _norm(e.address or e.location_name))
        if key in by_triple:
            by_triple[key] = _richer(by_triple[key], e)
        else:
            by_triple[key] = e
            triple_order.append(key)
    return [by_triple[k] for k in triple_order]


def _richer(a: Event, b: Event) -> Event:
    """Prefer the row with more non-empty signal, then the newer fetch."""
    def score(e: Event) -> int:
        return sum(bool(x) for x in (
            e.title, e.end, e.location_name, e.address, e.tags,
            e.category, e.admission != ADMISSION_UNKNOWN,
            e.registration_url, e.accessibility, e.summary))
    sa, sb = score(a), score(b)
    if sb > sa:
        return b
    if sa > sb:
        return a
    return b if (b.fetched_at or "") > (a.fetched_at or "") else a


# ---------------------------------------------------------------- schedule
def _gap_bounds(gap: Gap | tuple | list | Mapping) -> tuple[datetime | None, datetime | None]:
    if isinstance(gap, Gap):
        return gap.start, gap.end
    if isinstance(gap, Mapping):
        return _as_datetime(gap.get("start")), _as_datetime(gap.get("end"))
    if isinstance(gap, (tuple, list)) and len(gap) >= 2:
        return _as_datetime(gap[0]), _as_datetime(gap[1])
    return None, None


def fits_gap(event: Event, gaps: Iterable[Gap | tuple | list | Mapping]) -> Gap | tuple | None:
    """Return the first free gap that **fully contains** the event, else None.

    "Fully inside" is inclusive of both boundaries: an event ending exactly
    when a class begins still fits. A missing end time is treated as a
    zero-length event at its start. An event that only partially overlaps a
    gap does NOT fit.
    """
    st = _event_start(event)
    if st is None:
        return None
    en = event.end_dt or st
    for gap in gaps:
        gs, ge = _gap_bounds(gap)
        if gs is None or ge is None:
            continue
        if st >= gs and en <= ge:
            return gap
    return None


def recommendable(events: Iterable[Event], gaps: Iterable[Gap | tuple | list | Mapping],
                  *, include_cancelled: bool = False) -> list[Event]:
    """Events that fit fully inside a free gap and are safe to recommend.

    Cancelled and cancelled-uncertain events are excluded by default: we never
    recommend something we are not sure is still happening.
    """
    gaps = list(gaps)
    out = []
    for e in events:
        if not include_cancelled and e.status in (STATUS_CANCELLED, STATUS_CANCELLED_UNCERTAIN):
            continue
        if e.status == STATUS_PARSER_FAILED:
            continue
        if fits_gap(e, gaps) is not None:
            out.append(e)
    return sorted(out, key=_sort_key)


# ---------------------------------------------------------------- update / cancellation
def apply_miss_heuristic(prev_events: Iterable[Event], seen_ids: set[str], *,
                         fetched_at: str,
                         threshold: int = MISS_THRESHOLD) -> list[Event]:
    """Update cancellation status from this crawl's ``seen_ids``.

    * event still present -> ``scheduled``, streak reset
    * missing once        -> ``cancelled-uncertain`` (transient de-listing)
    * missing >= threshold-> ``cancelled``
    """
    out: list[Event] = []
    for e in prev_events:
        if e.id in seen_ids:
            out.append(replace(e, status=STATUS_SCHEDULED, miss_streak=0,
                               fetched_at=fetched_at or e.fetched_at))
        else:
            streak = e.miss_streak + 1
            status = STATUS_CANCELLED if streak >= threshold else STATUS_CANCELLED_UNCERTAIN
            out.append(replace(e, status=status, miss_streak=streak,
                               fetched_at=fetched_at or e.fetched_at))
    return out


def changed_hashes(prev_hashes: Mapping[str, str],
                   current_hashes: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Diff content hashes -> (changed_or_new_urls, removed_urls)."""
    changed = [u for u, h in current_hashes.items() if prev_hashes.get(u) != h]
    removed = [u for u in prev_hashes if u not in current_hashes]
    return sorted(changed), sorted(removed)


# ---------------------------------------------------------------- snapshot
@dataclass(frozen=True)
class Snapshot:
    month: str
    generated_at: str
    fetched_at: str
    source: dict
    coverage: dict
    events: tuple[Event, ...]
    parser_failures: tuple[str, ...] = ()
    schema: str = "hokieday.events/snapshot/1"

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "month": self.month,
            "generated_at": self.generated_at,
            "fetched_at": self.fetched_at,
            "source": self.source,
            "coverage": self.coverage,
            "parser_failures": list(self.parser_failures),
            "events": [e.to_dict() for e in self.events],
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "Snapshot":
        return cls(
            month=str(row.get("month") or MONTH_KEY),
            generated_at=str(row.get("generated_at") or row.get("fetched_at") or ""),
            fetched_at=str(row.get("fetched_at") or ""),
            source=dict(row.get("source") or {}),
            coverage=dict(row.get("coverage") or {}),
            events=tuple(Event.from_dict(e) for e in (row.get("events") or ())),
            parser_failures=tuple(str(u) for u in (row.get("parser_failures") or ())),
            schema=str(row.get("schema") or "hokieday.events/snapshot/1"),
        )


def load_snapshot(path: str | Path) -> Snapshot:
    """Load a normalized snapshot JSON from disk (offline; no network)."""
    return Snapshot.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def snapshot_state(snapshot: Snapshot, *, now: datetime | None = None,
                   stale_after_h: float = EVENTS_STALE_AFTER_H) -> str:
    """One of ``empty``/``partial``/``stale``/``ok`` (worst state wins).

    Order matters: an empty snapshot is empty even if also stale; a partial
    crawl is flagged partial so the UI can say "some results may be missing".
    """
    if not snapshot.events:
        return STATE_EMPTY
    if snapshot.parser_failures:
        return STATE_PARTIAL
    fetched = parse_event_time(snapshot.fetched_at)
    if fetched is None:
        return STATE_STALE
    reference = now or config.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age = reference - fetched.astimezone(timezone.utc)
    if age > timedelta(hours=stale_after_h):
        return STATE_STALE
    return STATE_OK


# ---------------------------------------------------------------- calendar export
def to_calendar_event(event: Event, *, dtstamp: datetime | None = None) -> dict:
    """A standard calendar-event dict + ICS-ready fields.

    This only EXPORTS a payload for the frontend's "add to schedule" action; it
    never writes a user's calendar.
    """
    st = _event_start(event) or config.now(_EVT_TZ)
    en = event.end_dt or (st + timedelta(hours=1))
    stamp = dtstamp or _as_datetime(event.fetched_at) or config.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    status = "CANCELLED" if event.status == STATUS_CANCELLED else "TENTATIVE" \
        if event.status == STATUS_CANCELLED_UNCERTAIN else "CONFIRMED"
    location = event.location_name or event.address or "Location not specified"
    description = event.summary or (
        f"{event.title} — {location}. Source: {event.source_url}")
    return {
        "uid": f"{event.id}@events.vt.edu",
        "title": event.title,
        "start": to_campus(st).isoformat(timespec="minutes"),
        "end": to_campus(en).isoformat(timespec="minutes"),
        "timezone": event.timezone,
        "all_day": event.all_day,
        "location": location,
        "address": event.address,
        "url": event.source_url,
        "description": description,
        "status": event.status,
        "ics": {
            "UID": f"{event.id}@events.vt.edu",
            "DTSTAMP": _ics_utc(stamp),
            "DTSTART": _ics_dt(st),
            "DTEND": _ics_dt(en),
            "SUMMARY": _ics_escape(event.title),
            "LOCATION": _ics_escape(location),
            "DESCRIPTION": _ics_escape(description),
            "URL": event.source_url,
            "STATUS": status,
        },
    }


def _ics_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_EVT_TZ)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;") \
        .replace(",", "\\,").replace("\n", "\\n")


def to_ics(events: Iterable[Event], *, cal_name: str = "HokieFlow Events",
           dtstamp: datetime | None = None) -> str:
    """Serialize events to an ICS document string (no file is written)."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//HokieFlow//events.vt.edu//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(cal_name)}",
    ]
    for e in events:
        payload = to_calendar_event(e, dtstamp=dtstamp)["ics"]
        lines.append("BEGIN:VEVENT")
        for key in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY",
                    "LOCATION", "DESCRIPTION", "URL", "STATUS"):
            if payload.get(key):
                lines.append(f"{key}:{payload[key]}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


__all__ = [
    "CAMPUS_TZ", "MONTH_KEY", "MONTH_START", "MONTH_END", "MISS_THRESHOLD",
    "EVENTS_STALE_AFTER_H", "EventParseError", "SitemapEntry", "EventCard",
    "Event", "Gap", "BrowseResult", "Snapshot", "STATUS_SCHEDULED",
    "STATUS_CANCELLED", "STATUS_CANCELLED_UNCERTAIN", "STATUS_PARSER_FAILED",
    "STATUS_STALE", "STATUS_UNKNOWN", "STATE_OK", "STATE_EMPTY", "STATE_STALE",
    "STATE_PARTIAL", "STATE_NO_MATCH", "ADMISSION_FREE", "ADMISSION_PAID",
    "ADMISSION_UNKNOWN", "parse_event_time", "to_campus", "in_month",
    "in_september_2026", "event_id", "parse_sitemap", "sitemap_month_urls",
    "parse_listing", "parse_tags", "scrub_pii", "extract_summary",
    "parse_detail", "is_blacksburg", "search", "filter_events", "browse",
    "dedupe", "fits_gap", "recommendable", "apply_miss_heuristic",
    "changed_hashes", "load_snapshot", "snapshot_state", "to_calendar_event",
    "to_ics",
]