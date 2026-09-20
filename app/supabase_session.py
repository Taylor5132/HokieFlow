"""Keep a signed-in session's Supabase access token usable.

A HokieFlow session cookie is valid for a week, but the Supabase access token
inside it expires after about an hour. Identity can survive that (the cookie
carries the user), yet every account read or write hands the token to PostgREST,
which answers a dead token with "JWT expired" -- surfacing as
"Your account could not access saved data. Please sign in again." while the UI
still shows the student as signed in. That is the confusing state this module
removes: before a token is used, refresh it with the stored refresh token.

No token is ever logged, printed, or written outside the cookie, and a failed
refresh returns None so the caller can ask the student to sign in again instead
of retrying forever.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping

REFRESH_SKEW_S = 120          # refresh slightly early rather than mid-request
REQUEST_TIMEOUT_S = 20
_CACHE_LIMIT = 128

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[str, float]] = {}   # digest -> (access_token, expires_epoch)


def _expiry_epoch(session: Mapping[str, Any]) -> float:
    """When the stored access token dies, as an epoch float (0 = unknown)."""
    raw = session.get("token_expires_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return 0.0


def _digest(session: Mapping[str, Any]) -> str:
    refresh = str(session.get("refresh_token") or "")
    return hashlib.sha256(refresh.encode("utf-8")).hexdigest()[:16]


def _remember(session: Mapping[str, Any], token: str, expires_epoch: float) -> None:
    key = _digest(session)
    with _LOCK:
        if len(_CACHE) >= _CACHE_LIMIT:
            # Drop whatever is closest to any expiry; the map is a convenience,
            # not a store, so eviction is safe.
            for gone in sorted(_CACHE, key=lambda k: _CACHE[k][1])[: _CACHE_LIMIT // 4]:
                _CACHE.pop(gone, None)
        _CACHE[key] = (token, expires_epoch)


def _cached(session: Mapping[str, Any]) -> str | None:
    key = _digest(session)
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and hit[1] - time.time() > REFRESH_SKEW_S:
        return hit[0]
    return None


def _endpoint() -> tuple[str, str] | None:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not url or not key:
        return None
    return url, key


def refresh(session: Mapping[str, Any]) -> tuple[str, float] | None:
    """Exchange the refresh token for a new access token.

    Returns ``(access_token, expires_epoch)`` or None when the session cannot be
    refreshed (no refresh token, configuration missing, or Supabase refused it).
    """
    refresh_token = str(session.get("refresh_token") or "").strip()
    endpoint = _endpoint()
    if not refresh_token or endpoint is None:
        return None
    url, key = endpoint
    body = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    request = urllib.request.Request(
        f"{url}/auth/v1/token?grant_type=refresh_token", data=body, method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    token = str(payload.get("access_token") or "")
    if not token:
        return None
    expires = payload.get("expires_at")
    if isinstance(expires, (int, float)):
        expires_epoch = float(expires)
    else:
        try:
            expires_epoch = time.time() + float(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_epoch = time.time() + 3600
    return token, expires_epoch


def token_for(session: Mapping[str, Any]) -> str | None:
    """A usable access token for this session, refreshing it when it has expired.

    A session with no recorded expiry is treated as expired: cookies issued
    before the token expiry was stored would otherwise keep presenting a dead
    token. The refresh result is cached in-process (keyed by a hash of the
    refresh token), so a stale cookie costs one Supabase call, not one per
    request.
    """
    if not session:
        return None
    token = str(session.get("access_token") or "")
    expires = _expiry_epoch(session)
    if token and expires and expires - time.time() > REFRESH_SKEW_S:
        return token
    cached = _cached(session)
    if cached:
        return cached
    refreshed = refresh(session)
    if refreshed is None:
        # Nothing to refresh with: fall back to whatever was stored, so a
        # provider-side hiccup does not sign anyone out on its own.
        return token or None
    new_token, expires_epoch = refreshed
    _remember(session, new_token, expires_epoch)
    return new_token