#!/usr/bin/env python3
"""Edge crawler for official Virginia Tech events (events.vt.edu).

Why a crawler and not an API call: events.vt.edu is AEM / Ensemble CMS and
publishes **no JSON, RSS or ICS**. The only sources are ``sitemap.xml``, the
month/listing HTML and the public detail HTML. This script is the *edge*
component: it fetches politely, hashes what it got, caches raw pages
incrementally, and hands the bytes to the pure parsers in ``hokieday.events``
so those stay offline-testable.

Politeness / safety contract
----------------------------
* Descriptive User-Agent (``config.USER_AGENT``).
* Honours ``robots.txt``. The site publishes ``Crawl-delay: 10``; we sleep at
  least that long between *network* requests (never between cache hits).
* Read-only. We never touch authenticated or write paths; only GETs of public
  pages.
* ``--max-pages`` guard: a sitemap that suddenly grows cannot make us hammer
  the origin (default 200, refuses to truncate silently).
* A 404 month page (observed for ``/events/2026/09.html``) is not a failure:
  the crawler degrades to the year listing + sitemap.

Usage
-----
    python3 scripts/crawl_events.py --dry-run          # plan, no details, no writes
    python3 scripts/crawl_events.py                    # crawl into the live cache
    python3 scripts/crawl_events.py --out fixtures/events_september_2026.json

The output snapshot is a *normalized, PII-free* JSON file. Raw HTML lives under
the (gitignored) cache and is never committed, because detail pages contain
contact emails/phones and images.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hokieday import cache, config, events  # noqa: E402

SITEMAP_URL = "https://events.vt.edu/sitemap.xml"
ROBOTS_URL = "https://events.vt.edu/robots.txt"
MONTH_PAGE = "https://events.vt.edu/events/{year}/{month:02d}.html"
YEAR_PAGE = "https://events.vt.edu/events/{year}.html"
EVENTS_INDEX = "https://events.vt.edu/events.html"

DEFAULT_OUT = config.REPO_DIR / "fixtures" / "events" / "events_september_2026.json"
DEFAULT_STATE = config.CACHE_DIR / "events_crawl_state.json"
DEFAULT_THROTTLE = 10.0                     # events.vt.edu robots Crawl-delay


# ---------------------------------------------------------------- polite HTTP
class Fetcher:
    """Cache-backed GET with a robots crawl-delay floor and content hashing."""

    def __init__(self, *, throttle: float = DEFAULT_THROTTLE, verbose: bool = True):
        self.throttle = max(0.0, throttle)
        self.verbose = verbose
        self.offline = False
        self._last = 0.0
        self.network_calls = 0
        self.cache_hits = 0

    def _slug(self, url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]

    def cached_path(self, url: str) -> Path:
        key = cache.key("events_page", {"u": self._slug(url)})
        return config.CACHE_DIR / f"{key}.bin"

    def _sleep(self) -> None:
        if self.throttle <= 0:
            return
        wait = self.throttle - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str, *, refresh: bool = True) -> bytes:
        """Return page bytes. ``refresh=False`` reads cache if present (no sleep)."""
        p = self.cached_path(url)
        if self.offline:
            if not p.exists():
                raise FileNotFoundError(f"--from-cache: no cached copy for {url}")
            self.cache_hits += 1
            return p.read_bytes()
        if p.exists() and not refresh:
            self.cache_hits += 1
            return p.read_bytes()
        self._sleep()
        self.network_calls += 1
        return cache.get_bytes("events_page", url,
                               params={"u": self._slug(url)},
                               max_age_s=None, force=refresh)

    def get_bytes(self, url: str) -> bytes:
        """Fetch raw bytes (robots/sitemap) through the same polite path."""
        return self.get(url, refresh=True)


def parse_robots_crawl_delay(text: str) -> float | None:
    """Crawl-delay for ``User-agent: *`` (falls back to the first directive)."""
    star: float | None = None
    first: float | None = None
    current_is_star = False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            current_is_star = value == "*"
            continue
        if field == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                continue
            if first is None:
                first = delay
            if current_is_star:
                star = delay
    return star if star is not None else first


# ---------------------------------------------------------------- crawl state
def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------- planning
def _month_detail_urls(entries, year, month) -> list:
    return events.sitemap_month_urls(entries, year, month)


def plan_urls(entries, year: int, month: int) -> dict:
    return {
        "month_page": MONTH_PAGE.format(year=year, month=month),
        "year_page": YEAR_PAGE.format(year=year),
        "events_index": EVENTS_INDEX,
        "detail_urls": [e.url for e in _month_detail_urls(entries, year, month)],
        "sitemap_entries": len(entries),
    }


# ---------------------------------------------------------------- crawl core
def crawl(args) -> int:
    month = args.month
    year, mon = (int(x) for x in month.split("-"))
    throttle = args.throttle
    fetcher = Fetcher(throttle=throttle)
    fetcher.offline = args.from_cache

    # ---- robots (polite) ----
    robots_text = ""
    try:
        robots_text = fetcher.get_bytes(ROBOTS_URL).decode("utf-8", "replace")
        robots_delay = parse_robots_crawl_delay(robots_text)
        if robots_delay and robots_delay > fetcher.throttle:
            fetcher.throttle = robots_delay
        print(f"[robots] Crawl-delay={robots_delay} -> using {fetcher.throttle:g}s")
    except Exception as exc:                              # noqa: BLE001
        print(f"[robots] WARN could not read robots.txt ({exc}); using {throttle:g}s")

    # ---- sitemap ----
    sitemap_text = fetcher.get_bytes(SITEMAP_URL).decode("utf-8", "replace")
    entries = events.parse_sitemap(sitemap_text)
    plan = plan_urls(entries, year, mon)
    print(f"[sitemap] {plan['sitemap_entries']} entries; "
          f"{len(plan['detail_urls'])} under /events/{year}/{mon:02d}/")

    # ---- month page: expected, may 404 ----
    month_available = False
    month_cards: list = []
    month_url = plan["month_page"]
    try:
        body = fetcher.get_bytes(month_url).decode("utf-8", "replace")
        if "no content" not in body[:200].lower():
            month_cards = events.parse_listing(body)
            month_available = bool(month_cards)
    except Exception as exc:                              # noqa: BLE001
        print(f"[month] {month_url} unavailable ({type(exc).__name__}: {exc}); "
              "degrading to year listing + sitemap")

    # ---- year / index listings for card metadata (category, free-food, ...) ----
    cards_by_url: dict = {c.url: c for c in month_cards}
    for listing_url in (plan["year_page"], plan["events_index"]):
        try:
            body = fetcher.get_bytes(listing_url).decode("utf-8", "replace")
            for c in events.parse_listing(body):
                if (c.month or "") == month:
                    cards_by_url.setdefault(c.url, c)
            print(f"[listing] {listing_url}: {len(cards_by_url)} September cards known")
        except Exception as exc:                          # noqa: BLE001
            print(f"[listing] WARN {listing_url}: {exc}")

    # ---- candidate union: sitemap detail URLs + September cards ----
    candidates = sorted(set(plan["detail_urls"]) | {
        u for u, c in cards_by_url.items() if (c.month or "") == month})

    if len(candidates) > args.max_pages:
        print(f"[guard] {len(candidates)} candidate pages exceed --max-pages "
              f"{args.max_pages}; re-run with --max-pages {len(candidates)} "
              "(refusing to truncate silently)")
        return 3
    est = len(candidates) * fetcher.throttle
    print(f"[plan] {len(candidates)} candidate detail pages "
          f"(~{est/60:.1f} min at {fetcher.throttle:g}s)")

    if args.dry_run:
        for u in candidates[:10]:
            print(f"  would fetch {u}")
        if len(candidates) > 10:
            print(f"  ... and {len(candidates) - 10} more")
        print("[dry-run] no pages fetched, no snapshot written")
        return 0

    # ---- previous snapshot / incremental state ----
    out_path = Path(args.out)
    prev_snap = None
    if out_path.exists():
        try:
            prev_snap = events.load_snapshot(out_path)
        except (OSError, ValueError) as exc:
            print(f"[state] WARN previous snapshot unreadable: {exc}")
    prev_by_url: dict[str, events.Event] = {}
    if prev_snap:
        for e in prev_snap.events:
            prev_by_url[e.source_url] = e

    state_path = Path(args.state)
    state = load_state(state_path)
    sitemap_lastmod = {e.url: e.lastmod for e in entries}

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen_candidates: set[str] = set()
    parsed: list[events.Event] = []
    parser_failures: list[str] = []
    changed_urls: list[str] = []
    coverage = {
        "scope": "September 2026 Blacksburg-area events only",
        "note": "Partial coverage: not all campus events; only Blacksburg-area "
                "September 2026 events that were reachable in the public HTML.",
        "candidates": len(candidates),
        "detail_parsed": 0,
        "detail_failed": 0,
        "included_blacksburg": 0,
        "excluded_not_blacksburg": 0,
        "excluded_out_of_month": 0,
        "from_incremental_cache": 0,
        "month_page_available": month_available,
    }

    for i, url in enumerate(candidates, 1):
        seen_candidates.add(url)
        lastmod = sitemap_lastmod.get(url)
        prev_state = state.get(url, {})
        page_fetched_at = prev_state.get("fetched_at") or now_iso
        unchanged = (
            prev_state
            and prev_state.get("lastmod") == lastmod
            and prev_state.get("sha256")
            and fetcher.cached_path(url).exists()
        )
        card = cards_by_url.get(url)
        if unchanged and url in prev_by_url and not args.from_cache:
            parsed.append(prev_by_url[url])
            coverage["from_incremental_cache"] += 1
            continue
        try:
            raw = fetcher.get(url, refresh=not unchanged)
        except Exception as exc:                          # noqa: BLE001
            print(f"  [{i}/{len(candidates)}] FETCH FAIL {url} ({type(exc).__name__})")
            coverage["detail_failed"] += 1
            if url in prev_by_url:                         # carry last good copy
                parsed.append(prev_by_url[url])
            continue
        sha = hashlib.sha256(raw).hexdigest()
        if prev_state.get("sha256") not in (None, sha):
            changed_urls.append(url)
        state[url] = {"lastmod": lastmod, "sha256": sha, "fetched_at": page_fetched_at}
        try:
            ev = events.parse_detail(raw.decode("utf-8", "replace"), url,
                                     fetched_at=page_fetched_at, source_lastmod=lastmod,
                                     card=card, include_summary=args.include_summary)
        except events.EventParseError as exc:
            print(f"  [{i}/{len(candidates)}] PARSE FAIL {url}: {exc}")
            coverage["detail_failed"] += 1
            parser_failures.append(url)
            continue
        coverage["detail_parsed"] += 1
        if not events.in_september_2026(ev.start_dt):
            coverage["excluded_out_of_month"] += 1
            continue
        if not args.all_locations and not events.is_blacksburg(
                ev.address, ev.location_name, ev.tags):
            coverage["excluded_not_blacksburg"] += 1
            continue
        parsed.append(ev)

    # ---- cancellation heuristic for events no longer advertised ----
    advertised = {e.url for e in entries}
    carried: list[events.Event] = []
    for e in (prev_snap.events if prev_snap else ()):
        if e.source_url in seen_candidates or e.source_url in advertised:
            continue                                          # still current or ours
        carried.append(e)
    seen_ids = {e.id for e in parsed}
    updated_carried = events.apply_miss_heuristic(carried, seen_ids,
                                                  fetched_at=now_iso)

    all_events = events.dedupe(parsed + updated_carried)
    coverage["included_blacksburg"] = sum(
        1 for e in all_events if e.status == events.STATUS_SCHEDULED)

    snapshot = events.Snapshot(
        month=month,
        generated_at=now_iso,
        fetched_at=now_iso,
        source={
            "site": "https://events.vt.edu",
            "sitemap": SITEMAP_URL,
            "month_page": plan["month_page"],
            "month_page_available": month_available,
            "year_page": plan["year_page"],
            "event_pages": "public detail HTML (schema.org microdata)",
            "crawl_delay_s": fetcher.throttle,
            "user_agent": config.USER_AGENT,
        },
        coverage=coverage,
        events=tuple(all_events),
        parser_failures=tuple(parser_failures),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    save_state(state_path, state)

    print(f"\n[crawl] network={fetcher.network_calls} cache_hits={fetcher.cache_hits}")
    print(f"[crawl] parsed={coverage['detail_parsed']} failed={coverage['detail_failed']} "
          f"blacksburg={coverage['included_blacksburg']} "
          f"not_blacksburg={coverage['excluded_not_blacksburg']} "
          f"out_of_month={coverage['excluded_out_of_month']} "
          f"changed={len(changed_urls)}")
    print(f"[write] {out_path} ({len(all_events)} events)")
    if parser_failures:
        print(f"[warn] {len(parser_failures)} parser failure(s) recorded in snapshot")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month", default=events.MONTH_KEY, help="YYYY-MM (default 2026-09)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="snapshot JSON output path")
    ap.add_argument("--state", default=str(DEFAULT_STATE),
                    help="incremental crawl-state JSON (gitignored cache)")
    ap.add_argument("--max-pages", type=int, default=200,
                    help="refuse to crawl more than this many detail pages")
    ap.add_argument("--throttle", type=float, default=DEFAULT_THROTTLE,
                    help="minimum seconds between network requests (robots floor wins)")
    ap.add_argument("--all-locations", action="store_true",
                    help="keep non-Blacksburg events (default: Blacksburg only)")
    ap.add_argument("--include-summary", action="store_true",
                    help="store a <=160 char scrubbed summary (off: copyright-minimal)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the crawl plan; fetch no detail pages; write nothing")
    ap.add_argument("--from-cache", action="store_true",
                    help="re-parse already-cached pages with no network (rebuild snapshot)")
    args = ap.parse_args()

    if config.CACHE_ONLY:
        print("refusing: DEMO_MODE=cache cannot crawl. Run with DEMO_MODE=live.")
        return 2
    return crawl(args)


if __name__ == "__main__":
    raise SystemExit(main())