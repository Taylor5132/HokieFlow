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

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


class CacheMiss(RuntimeError):
    """Raised in cache-only mode when a fixture is not present."""


# ------------------------------------------------------------------ keys
def key(name: str, params: dict | None = None) -> str:
    """Deterministic, filename-safe cache key.

    key("dining_menu", {"location_num": "15", "dtdate": "09/19/2026"})
      -> "dining_menu__dtdate=09-19-2026__location_num=15"
    """
    params = params or {}
    raw = name if not params else name + "__" + "__".join(
        f"{k}={params[k]}" for k in sorted(params)
    )
    return re.sub(r"[^A-Za-z0-9._=+-]", "-", raw)


def _json_path(name: str, params: dict | None = None) -> Path:
    return config.CACHE_DIR / f"{key(name, params)}.json"


def _bin_path(name: str, params: dict | None = None) -> Path:
    return config.CACHE_DIR / f"{key(name, params)}.bin"


# ------------------------------------------------------------------ internals
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_envelope(path: Path, url: str, payload: Any) -> None:
    envelope = {
        "key": path.stem,
        "url": url,
        "fetched_at": _now_iso(),
        "mode": config.DEMO_MODE,
        "payload": payload,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope), encoding="utf-8")
    tmp.replace(path)              # atomic: never leave a half-written fixture


def _read_envelope(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _http(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
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
    have = p.exists()
    fresh = have and (max_age_s is None or (age_seconds(name, params) or 1e18) <= max_age_s)

    if config.CACHE_ONLY:
        if have:
            return _read_envelope(p)["payload"]
        raise CacheMiss(f"DEMO_MODE=cache and no fixture for {key(name, params)}")

    if fresh and not force:
        return _read_envelope(p)["payload"]

    try:
        payload = json.loads(_http(url, timeout).decode("utf-8", "replace"))
    except Exception as exc:                     # noqa: BLE001 - deliberate breadth
        if have:
            print(f"[cache] WARN {key(name, params)}: fetch failed ({exc}); using cached copy")
            return _read_envelope(p)["payload"]
        raise
    _write_envelope(p, url, payload)
    return payload


def get_bytes(name: str, url: str, *, params: dict | None = None,
              max_age_s: float | None = None, force: bool = False,
              timeout: int = 120) -> bytes:
    """Binary variant, used for the GTFS zip."""
    p = _bin_path(name, params)
    have = p.exists()
    fresh = have and (max_age_s is None or (age_seconds(name, params) or 1e18) <= max_age_s)

    if config.CACHE_ONLY:
        if have:
            return p.read_bytes()
        raise CacheMiss(f"DEMO_MODE=cache and no binary fixture for {key(name, params)}")

    if fresh and not force:
        return p.read_bytes()

    try:
        data = _http(url, timeout)
    except Exception as exc:                     # noqa: BLE001
        if have:
            print(f"[cache] WARN {key(name, params)}: fetch failed ({exc}); using cached copy")
            return p.read_bytes()
        raise
    p.write_bytes(data)
    _write_envelope(_json_path(name, params), url, {"bytes": len(data), "file": p.name})
    return data


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