"""Cache-first fetch layer.

OWNER: parent (do not edit from a worker subagent).

Every external call goes through here so that:
  * responses are written to disk as evidence we can re-derive from (bronze idea)
  * DEMO_MODE=cache replays a full demo with networking disabled (NFR-1)
  * a dead source degrades to the last good copy instead of an exception (NFR-4)

On-disk envelope: {"key", "url", "fetched_at", "mode", "payload"}
Binary bodies (the GTFS zip) are stored as a sibling ".bin" file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


class CacheMiss(RuntimeError):
    """Raised in cache-only mode when a fixture is not present."""


class CacheRefreshError(RuntimeError):
    """Raised for a cold-cache refresh suppressed by the failure cooldown.

    Only used when a previous refresh attempt for the same key failed so
    recently that retrying is pointless (and would hammer a dead upstream).
    When the original failure is known its message is folded in so the caller
    still sees *why* nothing was fetched.
    """


# Filename components are limited to 255 bytes on common filesystems; keep well
# under it so the "__h<digest>" suffix always fits.
_MAX_KEY_LEN = 120

# ------------------------------------------------------------------ concurrency
# A ThreadingHTTPServer serves many requests at once. Without per-key
# synchronization two cold/stale callers both call upstream and race on the
# same ".tmp" envelope file (observed: 2 upstream calls + 1 FileNotFoundError).
#
# The lock table is REFERENCE-COUNTED instead of an unbounded dict: an entry is
# dropped as soon as no thread holds or waits on it, so a long-lived process
# cannot accumulate one lock per cache key forever. The global registry lock is
# held only to look up/create/drop an entry -- never across the fetch -- so two
# unrelated keys never serialize on each other.
_lock_registry = threading.Lock()
_key_locks: dict[str, list] = {}


@contextmanager
def _key_lock(k: str):
    """Return the per-cache-key lock as a context manager (ref-counted)."""
    with _lock_registry:
        entry = _key_locks.get(k)
        if entry is None:
            entry = [threading.Lock(), 0]
            _key_locks[k] = entry
        entry[1] += 1
    lock = entry[0]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _lock_registry:
            entry[1] -= 1
            if entry[1] <= 0 and _key_locks.get(k) is entry:
                del _key_locks[k]


# ------------------------------------------------------------------ cooldown
# A failed refresh with stale data on disk must not be retried on EVERY request
# during the politeness window: the live bus endpoint is undocumented and the
# whole point of LIVE_BUS_POLL_SECONDS is at most one upstream call per window.
# We remember the last failure per key and serve stale/flat until it expires.
# max_age_s is the natural cooldown (for the bus feed that is 60s); callers that
# don't declare a freshness window get a small default so a cold burst of
# concurrent requests still collapses to one upstream attempt.
_DEFAULT_FAILURE_COOLDOWN_S = 5.0
_ATTEMPT_TTL_S = 3600.0
_monotonic = time.monotonic
_attempt_registry = threading.Lock()
_attempts: dict[str, tuple[float, BaseException]] = {}


def _cooldown_window(max_age_s: float | None) -> float:
    if max_age_s is not None and max_age_s > 0:
        return float(max_age_s)
    return _DEFAULT_FAILURE_COOLDOWN_S


def _record_failure(k: str, exc: BaseException) -> None:
    now = _monotonic()
    with _attempt_registry:
        _attempts[k] = (now, exc)
        if len(_attempts) > 256:            # bound bookkeeping on many dead keys
            cutoff = now - _ATTEMPT_TTL_S
            for stale_k in [key for key, (ts, _) in _attempts.items() if ts < cutoff]:
                _attempts.pop(stale_k, None)


def _clear_failure(k: str) -> None:
    with _attempt_registry:
        _attempts.pop(k, None)


def _failure_record(k: str) -> tuple[float, BaseException] | None:
    with _attempt_registry:
        return _attempts.get(k)


def _suppress_retry(k: str, max_age_s: float | None) -> BaseException | None:
    """Return the recent failure if a retry must be skipped, else None."""
    record = _failure_record(k)
    if record is None:
        return None
    ts, exc = record
    return exc if (_monotonic() - ts) < _cooldown_window(max_age_s) else None


# ------------------------------------------------------------------ keys
def key(name: str, params: dict | None = None) -> str:
    """Deterministic, filename-safe cache key.

    key("dining_menu", {"location_num": "15", "dtdate": "09/19/2026"})
      -> "dining_menu__dtdate=09-19-2026__location_num=15"

    Keys longer than _MAX_KEY_LEN are hashed. This is not cosmetic: a 40-item
    nutrition query string produced a filename well past the 255-byte filesystem
    limit and every write failed with `OSError: [Errno 63] File name too long`,
    which silently emptied the whole nutrition seed. Short keys are returned
    unchanged so existing fixtures keep their names.
    """
    params = params or {}
    raw = name if not params else name + "__" + "__".join(
        f"{k}={params[k]}" for k in sorted(params)
    )
    safe = re.sub(r"[^A-Za-z0-9._=+-]", "-", raw)
    if len(safe) > _MAX_KEY_LEN:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return f"{safe[:_MAX_KEY_LEN // 2]}__h{digest}"
    return safe


def _json_path(name: str, params: dict | None = None) -> Path:
    return config.CACHE_DIR / f"{key(name, params)}.json"


def _bin_path(name: str, params: dict | None = None) -> Path:
    return config.CACHE_DIR / f"{key(name, params)}.bin"


# ------------------------------------------------------------------ internals
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_envelope(path: Path, url: str, payload: Any,
                    *, fetched_at: str | None = None,
                    mode: str | None = None) -> None:
    envelope = {
        "key": path.stem,
        "url": url,
        "fetched_at": fetched_at or _now_iso(),
        "mode": mode or config.DEMO_MODE,
        "payload": payload,
    }
    data = json.dumps(envelope)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A shared "<name>.tmp" is NOT collision-safe: two threads/processes
    # refreshing the same key overwrite each other's temp and the loser's
    # os.replace() raises FileNotFoundError. mkstemp gives each writer a unique
    # name in the target directory so os.replace stays the atomic commit point.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)      # atomic: never leave a half-written fixture
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _cleanup_stale_tmp(path)


def _cleanup_stale_tmp(path: Path) -> None:
    """Best-effort delete of orphaned temp files left by a crashed writer.

    Only files older than an hour are removed so a live writer in another
    process is never disturbed.
    """
    cutoff = time.time() - 3600
    try:
        for leftover in path.parent.glob(path.name + ".*.tmp"):
            try:
                if leftover.stat().st_mtime < cutoff:
                    leftover.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Publish a binary body through a unique temp file + atomic os.replace.

    Readers must never observe a partially written GTFS zip, and two writers
    (threads or processes) refreshing the same key must not collide on a shared
    temp name. mkstemp gives every writer its own file in the target directory
    and os.replace() is the single atomic commit point.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)      # atomic: never leave a half-written blob
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _cleanup_stale_tmp(path)


def _read_envelope(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _http(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _http_post(url: str, data: bytes, timeout: int) -> bytes:
    """POST an application/x-www-form-urlencoded body.

    Kept separate from ``_http`` so every existing GET caller (and the tests
    that monkeypatch ``cache._http`` with a ``(url, timeout)`` callable) stays
    untouched. The POST body is what makes it a *different* operation: an
    ArcGIS route solve cannot be represented as a cache key by URL alone.
    """
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": config.USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_failed(exc: Exception) -> bool:
    return isinstance(
        exc,
        (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError),
    )


# ------------------------------------------------------------------ public API
def age_seconds(name: str, params: dict | None = None) -> float | None:
    """Seconds since this key was fetched, or None if never fetched."""
    p = _json_path(name, params)
    if not p.exists():
        return None
    try:
        ts = datetime.fromisoformat(_read_envelope(p)["fetched_at"])
    except Exception:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds()


def is_stale(name: str, max_age_s: float, params: dict | None = None) -> bool:
    a = age_seconds(name, params)
    return a is None or a > max_age_s


def get_json(name: str, url: str, *, params: dict | None = None,
             max_age_s: float | None = None, force: bool = False,
             timeout: int = 30) -> Any:
    """Return parsed JSON for a resource, using the cache appropriately.

    live mode : fresh cache -> use it; else fetch; on failure -> stale copy.
    cache mode: read only; raise CacheMiss if nothing is on disk.
    """
    p = _json_path(name, params)
    k = key(name, params)

    if config.CACHE_ONLY:
        if p.exists():
            return _read_envelope(p)["payload"]
        raise CacheMiss(f"DEMO_MODE=cache and no fixture for {k}")

    # Fast path: a fresh on-disk entry needs no lock -- os.replace() publishes
    # complete files, so readers never observe a partial write.
    if (not force and p.exists()
            and (max_age_s is None
                 or (age_seconds(name, params) or 1e18) <= max_age_s)):
        return _read_envelope(p)["payload"]

    with _key_lock(k):
        # Re-check INSIDE the lock: the caller that was waiting must observe the
        # entry the first caller just wrote instead of fetching again.
        have = p.exists()
        fresh = have and (max_age_s is None
                          or (age_seconds(name, params) or 1e18) <= max_age_s)
        if fresh and not force:
            return _read_envelope(p)["payload"]

        suppressed = None if force else _suppress_retry(k, max_age_s)
        if suppressed is not None:
            if have:
                print(f"[cache] WARN {k}: refresh skipped after recent failure "
                      f"({suppressed}); using cached copy")
                return _read_envelope(p)["payload"]
            # Cold cache + recent failure: do NOT hit upstream a second time.
            raise CacheRefreshError(
                f"{k}: refresh suppressed after recent failure ({suppressed})") from suppressed

        try:
            payload = json.loads(_http(url, timeout).decode("utf-8", "replace"))
        except Exception as exc:                 # noqa: BLE001 - deliberate breadth
            _record_failure(k, exc)
            if have:
                print(f"[cache] WARN {k}: fetch failed ({exc}); using cached copy")
                return _read_envelope(p)["payload"]
            raise
        _clear_failure(k)
        _write_envelope(p, url, payload)
        return payload


def get_bytes(name: str, url: str, *, params: dict | None = None,
              max_age_s: float | None = None, force: bool = False,
              timeout: int = 120) -> bytes:
    """Binary variant, used for the GTFS zip.

    Mirrors get_json(): per-key lock, inside-lock freshness recheck, recent-
    failure cooldown, and deterministic cold-failure behavior. The binary body
    and its JSON metadata envelope share the same cache key, so the one lock
    serializes both publications without any nested acquisition (no deadlock).
    """
    p = _bin_path(name, params)
    k = key(name, params)

    if config.CACHE_ONLY:
        if p.exists():
            return p.read_bytes()
        raise CacheMiss(f"DEMO_MODE=cache and no binary fixture for {k}")

    # Fast path: a fresh on-disk entry needs no lock -- os.replace() publishes
    # complete files, so readers never observe a partial write.
    if (not force and p.exists()
            and (max_age_s is None
                 or (age_seconds(name, params) or 1e18) <= max_age_s)):
        return p.read_bytes()

    with _key_lock(k):
        # Re-check INSIDE the lock: the caller that was waiting must observe the
        # entry the first caller just wrote instead of fetching again.
        have = p.exists()
        fresh = have and (max_age_s is None
                          or (age_seconds(name, params) or 1e18) <= max_age_s)
        if fresh and not force:
            return p.read_bytes()

        suppressed = None if force else _suppress_retry(k, max_age_s)
        if suppressed is not None:
            if have:
                print(f"[cache] WARN {k}: refresh skipped after recent failure "
                      f"({suppressed}); using cached copy")
                return p.read_bytes()
            # Cold cache + recent failure: do NOT hit upstream a second time.
            raise CacheRefreshError(
                f"{k}: refresh suppressed after recent failure ({suppressed})") from suppressed

        try:
            data = _http(url, timeout)
        except Exception as exc:                 # noqa: BLE001 - deliberate breadth
            _record_failure(k, exc)
            if have:
                print(f"[cache] WARN {k}: fetch failed ({exc}); using cached copy")
                return p.read_bytes()
            raise
        _clear_failure(k)
        _write_bytes_atomic(p, data)
        _write_envelope(_json_path(name, params), url,
                        {"bytes": len(data), "file": p.name})
        return data


def _post_cache_params(url: str, encoded_form: bytes,
                       params: dict | None) -> dict:
    """Return caller parameters plus an opaque identity for this exact POST.

    The URL and body are hashed rather than copied into the filename: request
    bodies can be large and may contain private values. The reserved key is
    always overwritten, so a caller cannot accidentally (or deliberately)
    make two different POSTs share an entry by supplying a stale identity.
    """
    identity = hashlib.sha256(url.encode("utf-8") + b"\0" + encoded_form).hexdigest()
    out = dict(params or {})
    out["_post_sha256"] = identity
    return out


def _post_envelope_url(url: str) -> str:
    """Safe endpoint-only URL for cache metadata (no userinfo/query/fragment)."""
    parts = urllib.parse.urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def post_form_json(name: str, url: str, form: dict,
                   *, params: dict | None = None,
                   max_age_s: float | None = None, force: bool = False,
                   timeout: int = 30) -> Any:
    """Cached POST of an ``application/x-www-form-urlencoded`` body -> parsed JSON.

    Same contract as :func:`get_json` (per-key lock, inside-lock freshness
    recheck, recent-failure cooldown, stale fallback, ``DEMO_MODE=cache`` reads
    only) but the request is a form POST. Needed because an ArcGIS NAServer
    ``solve`` carries a JSON ``travelMode`` object and a ``stops`` object that
    are both too large and too operationally meaningful to fold into a URL.

    The helper itself folds a SHA-256 identity of the exact URL + encoded form
    body into the cache key. Callers may add readable ``params`` for diagnostics,
    but correctness never depends on them. Neither body values nor credentials
    are included in filenames or warning logs. Writes use the same lock,
    cooldown and atomic envelope publisher as GET.
    """
    data = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
    cache_params = _post_cache_params(url, data, params)
    p = _json_path(name, cache_params)
    k = key(name, cache_params)

    if config.CACHE_ONLY:
        if p.exists():
            return _read_envelope(p)["payload"]
        raise CacheMiss(f"DEMO_MODE=cache and no fixture for {k}")

    if (not force and p.exists()
            and (max_age_s is None
                 or (age_seconds(name, cache_params) or 1e18) <= max_age_s)):
        return _read_envelope(p)["payload"]

    with _key_lock(k):
        have = p.exists()
        fresh = have and (max_age_s is None
                          or (age_seconds(name, cache_params) or 1e18) <= max_age_s)
        if fresh and not force:
            return _read_envelope(p)["payload"]

        suppressed = None if force else _suppress_retry(k, max_age_s)
        if suppressed is not None:
            if have:
                print(f"[cache] WARN {k}: refresh skipped after recent failure "
                      f"({suppressed}); using cached copy")
                return _read_envelope(p)["payload"]
            raise CacheRefreshError(
                f"{k}: refresh suppressed after recent failure ({suppressed})") from suppressed

        try:
            payload = json.loads(_http_post(url, data, timeout).decode("utf-8", "replace"))
        except Exception as exc:                 # noqa: BLE001 - deliberate breadth
            _record_failure(k, exc)
            if have:
                print(f"[cache] WARN {k}: POST failed ({exc}); using cached copy")
                return _read_envelope(p)["payload"]
            raise
        _clear_failure(k)
        _write_envelope(p, _post_envelope_url(url), payload)
        return payload


def has(name: str, params: dict | None = None) -> bool:
    """True if a cache entry exists on disk (never touches the network).

    Needed because a DERIVED entry has no real source URL, so get_json() cannot
    be used to probe for it: live mode would try to fetch the pseudo-URL and fail
    with `unknown url type`.
    """
    return _json_path(name, params).exists()


def put_json(name: str, url: str, payload: Any, *, params: dict | None = None) -> None:
    """Persist a DERIVED payload (e.g. all-menu nutrition merged from chunks).

    Deliberately a no-op in DEMO_MODE=cache: a replay must never rewrite the
    frozen store it is reading. Live runs are what populate it.
    """
    if config.CACHE_ONLY:
        return
    _write_envelope(_json_path(name, params), url, payload)


# ------------------------------------------------------------------ public metadata
# These are the supported ways OUTSIDE cache.py to inspect or write provenance
# envelopes. They exist so cache consumers never reach for `_read_envelope` /
# `_json_path` / `_write_envelope` directly; those private names may change.
def read_envelope(name: str, params: dict | None = None) -> dict | None:
    """Public read of a cached envelope dict, or None when absent/unreadable.

    Never touches the network and never raises for a missing/corrupt entry.
    """
    p = _json_path(name, params)
    if not p.exists():
        return None
    try:
        return _read_envelope(p)
    except Exception:                                          # noqa: BLE001
        return None


def envelope_meta(name: str, params: dict | None = None) -> dict | None:
    """Public provenance metadata for a cache entry, or None when absent.

    Returns only the envelope's cheap metadata (never the payload), which is
    what freshness/provenance consumers actually need.
    """
    env = read_envelope(name, params)
    if env is None:
        return None
    return {k: env.get(k) for k in ("key", "url", "fetched_at", "mode")}


def write_envelope_atomic(name: str, url: str, payload: Any, *,
                          params: dict | None = None,
                          fetched_at: str | None = None) -> Path:
    """Publish one envelope atomically, keeping a caller-supplied real stamp.

    Unlike put_json() this works in DEMO_MODE=cache too and never invents a
    fresh timestamp when `fetched_at` is given -- important for fixtures that
    must preserve their true acquisition time.
    """
    path = _json_path(name, params)
    _write_envelope(path, url, payload, fetched_at=fetched_at)
    return path


def publish_envelopes(entries: list[dict], *,
                      cache_dir: Path | str | None = None) -> dict:
    """Publish a bundle of envelopes through staged temp files + swaps.

    `entries` is a list of dicts: {name, url, payload, params?, fetched_at?}.

    VISIBILITY — read this before claiming atomicity:
      * Each individual file is published with os.replace(), so no reader ever
        sees a half-written FILE.
      * The bundle as a whole is NOT reader-atomically visible: a concurrent
        reader can observe some new files and some old files between swaps.
        There is no versioned pointer. This is acceptable only because
        publication is an OFFLINE, STOPPED-APP operation (regenerate fixtures,
        restart the app). Do not publish while a demo/server is serving.
      * The returned manifest carries a `version` digest and `files`, so a
        caller can tell later which bundle is on disk.

    All-or-none applies to WRITER failure: if any stage or swap fails, the
    previously published files are restored and the new ones removed. Returns:
        {"version": str, "published_at": iso, "count": int, "files": [str]}
    """
    target = Path(cache_dir) if cache_dir is not None else config.CACHE_DIR
    target.mkdir(parents=True, exist_ok=True)

    digested = hashlib.sha1()
    staged: list[tuple[Path, Path]] = []
    try:
        for entry in entries:
            name = entry["name"]
            params = entry.get("params")
            final = target / f"{key(name, params)}.json"
            fetched_at = entry.get("fetched_at") or _now_iso()
            envelope = {
                "key": final.stem,
                "url": entry.get("url"),
                "fetched_at": fetched_at,
                "mode": entry.get("mode") or config.DEMO_MODE,
                "payload": entry["payload"],
            }
            digested.update(f"{final.stem}|{fetched_at}|".encode("utf-8"))
            digested.update(
                json.dumps(entry["payload"], sort_keys=True).encode("utf-8"))
            fd, tmp_name = tempfile.mkstemp(
                prefix=final.name + ".stage.", suffix=".tmp", dir=str(target))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(envelope))
            staged.append((Path(tmp_name), final))
    except BaseException:
        for tmp, _final in staged:
            try:
                tmp.unlink()
            except OSError:
                pass
        raise

    backups: dict[Path, Path | None] = {}
    published: list[Path] = []
    try:
        for tmp, final in staged:
            if final.exists():
                bfd, backup_name = tempfile.mkstemp(
                    prefix=final.name + ".bak.", suffix=".tmp", dir=str(target))
                os.close(bfd)
                os.replace(final, backup_name)
                backups[final] = Path(backup_name)
            else:
                backups[final] = None
            os.replace(tmp, final)
            published.append(final)
    except BaseException:
        for final, backup in backups.items():
            try:
                if final.exists():
                    final.unlink()
            except OSError:
                pass
            if backup is not None and backup.exists():
                try:
                    os.replace(backup, final)
                except OSError:
                    pass
        for tmp, _final in staged:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        raise

    for backup in backups.values():
        if backup is not None and backup.exists():
            try:
                backup.unlink()
            except OSError:
                pass
    return {
        "version": digested.hexdigest()[:16],
        "published_at": _now_iso(),
        "count": len(published),
        "files": [str(p) for p in published],
    }


def stats() -> dict:
    files = sorted(config.CACHE_DIR.glob("*.json")) + sorted(config.CACHE_DIR.glob("*.bin"))
    return {
        "mode": config.DEMO_MODE,
        "cache_dir": str(config.CACHE_DIR),
        "count": len(files),
        "files": [f.name for f in files],
    }


if __name__ == "__main__":       # python -m hokieday.cache
    print(json.dumps(stats(), indent=2))