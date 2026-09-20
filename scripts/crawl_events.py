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
* ``robots.txt`` is parsed with ``urllib.robotparser``; Disallow rules are
  enforced and ``Crawl-delay`` is honoured, with a hard floor of 10 s even if
  robots cannot be read.
* URL allowlist: HTTPS only, exact host ``events.vt.edu``, public content paths
  only. localhost, external hosts, non-HTTPS, userinfo and traversal are
  rejected before any socket is opened. Read-only; never authenticated/write.
* ``--max-pages`` guard: a sitemap that suddenly grows cannot make us hammer
  the origin (default 200, refuses to truncate silently).
* A 404 month page (observed for ``/events/2026/09.html``) is not a failure:
  the crawler degrades to the year listing + sitemap.

Incremental correctness
-----------------------
* A URL with a matching sitemap ``lastmod`` AND cached bytes is reused without a
  network call. A **missing lastmod never freezes**: those pages are always
  revalidated.
* Each page records the *actual* fetch time. A failed refresh that falls back to
  stale cache bytes is recorded as a stale fallback with the old timestamp, not
  stamped as a successful refresh.
* ``--from-cache`` never touches the network and never stamps ``now``.

Usage
-----
    python3 scripts/crawl_events.py --dry-run          # read-only plan (cache-only)
    python3 scripts/crawl_events.py                    # crawl into the live cache
    python3 scripts/crawl_events.py --from-cache       # rebuild offline from cache

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser

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
MIN_THROTTLE = 10.0
FRESH_WINDOW_S = 15.0                       # age below this == this run's fetch


# ---------------------------------------------------------------- polite HTTP
class Fetcher:
    """Cache-backed GET with robots/allowlist checks and content-freshness info."""

    def __init__(self, *, throttle: float = DEFAULT_THROTTLE,
                 offline: bool = False, verbose: bool = True):
        self.throttle = max(MIN_THROTTLE, throttle)
        self.offline = offline
        self.verbose = verbose
        self._last = 0.0
        self.network_calls = 0
        self.cache_hits = 0
        self.stale_fallbacks = 0

    def _params(self, url: str) -> dict:
        return {"u": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]}

    def cached_path(self, url: str) -> Path:
        key = cache.key("events_page", self._params(url))
        return config.CACHE_DIR / f"{key}.bin"

    def _stored_fetched_at(self, url: str) -> str | None:
        age = cache.age_seconds("events_page", self._params(url))
        if age is None:
            return None
        return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(
            timespec="seconds")

    def _sleep(self) -> None:
        wait = self.throttle - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str, *, refresh: bool = True) -> tuple[bytes, bool, str | None]:
        """Return ``(bytes, fresh, fetched_at_iso)``.

        ``fresh`` is True only when this call actually refreshed the bytes.
        Reading a cache hit (or a stale fallback) returns False with the stored
        timestamp, so callers never stamp ``now`` on old content.
        """
        p = self.cached_path(url)
        if self.offline:
            if not p.exists():
                raise FileNotFoundError(f"no cached copy for {url}")
            self.cache_hits += 1
            return p.read_bytes(), False, self._stored_fetched_at(url)
        if p.exists() and not refresh:
            self.cache_hits += 1
            return p.read_bytes(), False, self._stored_fetched_at(url)
        if not events.allowed_fetch_url(url):
            raise ValueError(f"URL not allowed by policy: {url}")

        self._sleep()
        self.network_calls += 1
        data = cache.get_bytes("events_page", url, params=self._params(url),
                               max_age_s=None, force=True)
        age = cache.age_seconds("events_page", self._params(url))
        now = datetime.now(timezone.utc)
        if age is not None and age <= FRESH_WINDOW_S:
            return data, True, now.isoformat(timespec="seconds")
        # cache.get_bytes fell back to a stale copy because the refresh failed
        self.stale_fallbacks += 1
        ts = now - timedelta(seconds=(age or 0))
        return data, False, ts.isoformat(timespec="seconds")


def build_robot_parser(text: str) -> RobotFileParser:
    parsed = RobotFileParser()
    parsed.parse(text.splitlines())
    return parsed


def parse_robots_crawl_delay(text: str) -> float | None:
    """Fallback crawl-delay reader (used if RobotFileParser returns None)."""
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


def _planned_detail_entries(entries, year: int, month: int):
    """Resolve + allowlist + de-dupe sitemap month detail URLs (with lastmod)."""
    out: dict[str, str | None] = {}
    for e in events.sitemap_month_urls(entries, year, month):
        url = events.resolve_url(SITEMAP_URL, e.url)
        if url and events.allowed_fetch_url(url):
            out.setdefault(url, e.lastmod)
    return out


def _latest_ts(times: list[str]) -> str | None:
    """Newest of a set of ISO timestamps, compared as instants (not strings).

    A plain ``max()`` over strings mis-orders offsets (``...-04:00`` sorts after
    ``...+00:00`` even when it is earlier), which would misreport the snapshot
    ``fetched_at``. We compare parsed datetimes and keep the original string.
    """
    best: tuple[datetime, str] | None = None
    for raw in times:
        if not raw:
            continue
        try:
            dt = events.parse_event_time(raw)
        except ValueError:
            continue
        if dt is None:
            continue
        if best is None or dt > best[0]:
            best = (dt, raw)
    return best[1] if best else None


def plan_urls(entries, year: int, month: int) -> dict:
    return {
        "month_page": MONTH_PAGE.format(year=year, month=month),
        "year_page": YEAR_PAGE.format(year=year),
        "events_index": EVENTS_INDEX,
        "detail_urls": list(_planned_detail_entries(entries, year, month)),
        "sitemap_entries": len(entries),
    }


# ---------------------------------------------------------------- crawl core
def crawl(args) -> int:
    if not events.validate_month(args.month):
        print(f"refusing: --month {args.month!r} is outside the fixed integration "
              f"scope ({events.MONTH_KEY}); no output written")
        return 2
    year, mon = (int(x) for x in args.month.split("-"))
    read_only = args.dry_run or args.from_cache
    fetcher = Fetcher(throttle=args.throttle, offline=read_only)

    # ---- robots (polite) ----
    rp: RobotFileParser | None = None
    try:
        data, _fresh, _ts = fetcher.get(ROBOTS_URL)
        text = data.decode("utf-8", "replace")
        rp = build_robot_parser(text)
        delay = rp.crawl_delay("*") or parse_robots_crawl_delay(text)
        if delay and delay > fetcher.throttle:
            fetcher.throttle = delay
        print(f"[robots] Crawl-delay={delay} -> using {fetcher.throttle:g}s")
    except FileNotFoundError:
        print(f"[robots] read-only run has no cached robots.txt; using "
              f"{fetcher.throttle:g}s floor")
    except Exception as exc:                              # noqa: BLE001
        print(f"[robots] WARN could not read robots.txt ({exc}); using "
              f"{fetcher.throttle:g}s floor")

    # ---- sitemap ----
    try:
        sitemap_data, _fresh, _ts = fetcher.get(SITEMAP_URL)
    except FileNotFoundError:
        print("[sitemap] read-only run has no cached sitemap; run a live crawl "
              "first (dry-run never fetches)")
        return 0
    entries = events.parse_sitemap(sitemap_data.decode("utf-8", "replace"))
    plan = plan_urls(entries, year, mon)
    print(f"[sitemap] {plan['sitemap_entries']} entries; "
          f"{len(plan['detail_urls'])} under /events/{year}/{mon:02d}/")

    # ---- month page: expected, may 404 ----
    month_available = False
    month_cards: list = []
    month_url = plan["month_page"]
    try:
        body, _fresh, _ts = fetcher.get(month_url)
        decoded = body.decode("utf-8", "replace")
        if "no content" not in decoded[:200].lower():
            month_cards = events.parse_listing(decoded, month_url)
            month_available = bool(month_cards)
    except Exception as exc:                              # noqa: BLE001
        print(f"[month] {month_url} unavailable ({type(exc).__name__}: {exc}); "
              "degrading to year listing + sitemap")

    # ---- year / index listings for card metadata (category, free-food, ...) ----
    cards_by_url: dict = {c.url: c for c in month_cards}
    for listing_url in (plan["year_page"], plan["events_index"]):
        try:
            body, _fresh, _ts = fetcher.get(listing_url)
            for c in events.parse_listing(body.decode("utf-8", "replace"), listing_url):
                if (c.month or "") == args.month and events.allowed_fetch_url(c.url):
                    cards_by_url.setdefault(c.url, c)
            print(f"[listing] {listing_url}: {len(cards_by_url)} September cards known")
        except Exception as exc:                          # noqa: BLE001
            print(f"[listing] WARN {listing_url}: {exc}")

    # ---- candidate union: sitemap detail URLs + September cards ----
    sitemap_map = _planned_detail_entries(entries, year, mon)
    candidates = sorted(set(sitemap_map) | {
        u for u, c in cards_by_url.items() if (c.month or "") == args.month})

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
        print("[dry-run] read-only: no network, no cache writes, no snapshot written")
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

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen_candidates: set[str] = set()
    parsed: list[events.Event] = []
    parser_failures: list[str] = []
    fetch_failures: list[str] = []
    stale_fallback_urls: list[str] = []
    changed_urls: list[str] = []
    page_times: list[str] = []
    excluded_url_policy = 0
    excluded_robots = 0
    reused = 0
    fresh_fetches = 0
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
        "excluded_url_policy": 0,
        "excluded_robots": 0,
        "from_incremental_cache": 0,
        "fresh_fetch": 0,
        "stale_fallback": 0,
        "month_page_available": month_available,
    }

    for i, url in enumerate(candidates, 1):
        # URL policy + robots are enforced before any request.
        if not events.allowed_fetch_url(url):
            excluded_url_policy += 1
            continue
        if rp is not None and not rp.can_fetch(config.USER_AGENT, url):
            print(f"  [{i}/{len(candidates)}] ROBOTS DISALLOW {url}")
            excluded_robots += 1
            continue
        seen_candidates.add(url)
        lastmod = sitemap_map.get(url)
        prev_state = state.get(url, {})
        # A missing lastmod can never be treated as "unchanged" -- revalidate.
        unchanged = (
            lastmod is not None
            and prev_state.get("lastmod") == lastmod
            and prev_state.get("sha256")
            and fetcher.cached_path(url).exists()
        )
        card = cards_by_url.get(url)
        if unchanged and url in prev_by_url and not args.from_cache:
            parsed.append(prev_by_url[url])
            reused += 1
            page_times.append(prev_by_url[url].fetched_at)
            continue

        attempted_refresh = not unchanged
        try:
            raw, fresh, page_fetched_at = fetcher.get(url, refresh=attempted_refresh)
        except Exception as exc:                          # noqa: BLE001
            print(f"  [{i}/{len(candidates)}] FETCH FAIL {url} ({type(exc).__name__})")
            fetch_failures.append(url)
            coverage["detail_failed"] += 1
            if url in prev_by_url:                         # retain last good copy
                parsed.append(prev_by_url[url])
                page_times.append(prev_by_url[url].fetched_at)
            continue

        page_fetched_at = page_fetched_at or ("" if read_only else now_iso)
        page_times.append(page_fetched_at)
        if fresh:
            fresh_fetches += 1
        elif attempted_refresh and not read_only:
            # Only a *failed refresh* is a stale fallback; a deliberate cache
            # hit (unchanged lastmod) is not.
            stale_fallback_urls.append(url)
        sha = hashlib.sha256(raw).hexdigest()
        if prev_state.get("sha256") not in (None, sha):
            changed_urls.append(url)
        state[url] = {"lastmod": lastmod, "sha256": sha,
                      "fetched_at": page_fetched_at, "fresh": fresh}
        try:
            ev = events.parse_detail(raw.decode("utf-8", "replace"), url,
                                     fetched_at=page_fetched_at, source_lastmod=lastmod,
                                     card=card, include_summary=args.include_summary)
        except events.EventParseError as exc:
            print(f"  [{i}/{len(candidates)}] PARSE FAIL {url}: {exc}")
            parser_failures.append(url)
            coverage["detail_failed"] += 1
            if url in prev_by_url:                         # retain last good copy
                parsed.append(prev_by_url[url])
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
    advertised = set(sitemap_map)
    carried: list[events.Event] = []
    for e in (prev_snap.events if prev_snap else ()):
        if e.source_url in seen_candidates or e.source_url in advertised:
            continue
        carried.append(e)
    seen_ids = {e.id for e in parsed}
    # ``fetched_at`` is the newest *content* time, never the run time. A
    # read-only run (--dry-run/--from-cache) must not advance freshness, so it
    # keeps the previous snapshot's timestamp when it fetched no page.
    snap_fetched = _latest_ts(page_times)
    if snap_fetched is None:
        if read_only:
            snap_fetched = prev_snap.fetched_at if prev_snap else ""
        else:
            snap_fetched = now_iso
    updated_carried = events.apply_miss_heuristic(carried, seen_ids,
                                                  fetched_at=snap_fetched)

    all_events = events.dedupe(parsed + updated_carried)
    coverage["included_blacksburg"] = sum(
        1 for e in all_events if e.status == events.STATUS_SCHEDULED)
    coverage["from_incremental_cache"] = reused
    coverage["fresh_fetch"] = fresh_fetches
    coverage["stale_fallback"] = len(stale_fallback_urls)
    coverage["excluded_url_policy"] = excluded_url_policy
    coverage["excluded_robots"] = excluded_robots

    snapshot = events.Snapshot(
        month=args.month,
        generated_at=now_iso,
        fetched_at=snap_fetched,
        source={
            "site": "https://events.vt.edu",
            "sitemap": SITEMAP_URL,
            "month_page": plan["month_page"],
            "month_page_available": month_available,
            "year_page": plan["year_page"],
            "event_pages": "public detail HTML (schema.org microdata)",
            "crawl_delay_s": fetcher.throttle,
            "user_agent": config.USER_AGENT,
            "refresh": {
                "network_requests": fetcher.network_calls,
                "cache_hits": fetcher.cache_hits,
                "fresh_fetches": fresh_fetches,
                "stale_fallbacks": len(stale_fallback_urls),
                "reused_unchanged": reused,
                "read_only": read_only,
            },
        },
        coverage=coverage,
        events=tuple(all_events),
        parser_failures=tuple(parser_failures),
        fetch_failures=tuple(fetch_failures),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    save_state(state_path, state)

    print(f"\n[crawl] network={fetcher.network_calls} cache_hits={fetcher.cache_hits} "
          f"fresh={fresh_fetches} stale_fallbacks={len(stale_fallback_urls)} "
          f"reused={reused}")
    print(f"[crawl] parsed={coverage['detail_parsed']} failed={coverage['detail_failed']} "
          f"blacksburg={coverage['included_blacksburg']} "
          f"not_blacksburg={coverage['excluded_not_blacksburg']} "
          f"out_of_month={coverage['excluded_out_of_month']} "
          f"changed={len(changed_urls)}")
    print(f"[write] {out_path} ({len(all_events)} events, "
          f"state={events.snapshot_state(snapshot, now=datetime.now(timezone.utc))})")
    if parser_failures or fetch_failures:
        print(f"[warn] {len(parser_failures)} parser + {len(fetch_failures)} fetch "
              "failure(s) recorded; snapshot state is partial")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month", default=events.MONTH_KEY, help="YYYY-MM (fixed: 2026-09)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="snapshot JSON output path")
    ap.add_argument("--state", default=str(DEFAULT_STATE),
                    help="incremental crawl-state JSON (gitignored cache)")
    ap.add_argument("--max-pages", type=int, default=200,
                    help="refuse to crawl more than this many detail pages")
    ap.add_argument("--throttle", type=float, default=DEFAULT_THROTTLE,
                    help="minimum seconds between network requests (floor 10)")
    ap.add_argument("--all-locations", action="store_true",
                    help="keep non-Blacksburg events (default: Blacksburg only)")
    ap.add_argument("--include-summary", action="store_true",
                    help="store a <=160 char scrubbed summary (off: copyright-minimal)")
    ap.add_argument("--dry-run", action="store_true",
                    help="read-only plan from cache: no network, no writes")
    ap.add_argument("--from-cache", action="store_true",
                    help="re-parse already-cached pages with no network (rebuild snapshot)")
    args = ap.parse_args()

    if config.CACHE_ONLY:
        print("refusing: DEMO_MODE=cache cannot crawl. Run with DEMO_MODE=live.")
        return 2
    return crawl(args)


if __name__ == "__main__":
    raise SystemExit(main())