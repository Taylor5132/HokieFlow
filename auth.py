import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from database import get_supabase_client

SESSION_COOKIE_NAME = "hokieflow_session"
OAUTH_STATE_COOKIE_NAME = "hokieflow_oauth_state"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7
GOOGLE_CALLBACK_PATH = "/api/auth/google/callback"

# ---------------------------------------------------------------------------
# OAuth funnel
#
# Sign-in failures happen inside a redirect, so the browser lands on the home
# screen looking signed out whatever went wrong: the state cookie was never
# sent, Supabase rejected the code, or /api/auth/me never saw a session. These
# counters record which stage each attempt reached. They are exposed through
# /api/status: counters only, never codes, tokens, or error text.
# ---------------------------------------------------------------------------

_FUNNEL_LOCK = threading.Lock()
_FUNNEL: dict[str, int] = {
    "oauth_started": 0,
    "callback_reached": 0,
    "state_ok": 0,
    "exchange_ok": 0,
    "session_accepted": 0,
}


def _bump(stage: str) -> None:
    with _FUNNEL_LOCK:
        _FUNNEL[stage] = _FUNNEL.get(stage, 0) + 1


def funnel() -> dict[str, int]:
    """A copy of the sign-in stage counters (safe to publish)."""
    with _FUNNEL_LOCK:
        return dict(_FUNNEL)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dict or an object (Supabase returns objects)."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _session_secret() -> bytes:
    """Secret used to sign the session cookie and derive the PKCE verifier."""
    secret = (os.getenv("SESSION_SECRET") or "").strip()
    if secret:
        return secret.encode("utf-8")
    if os.getenv("APP_ENV", "development") == "production":
        raise RuntimeError("SESSION_SECRET must be set in production.")
    return b"dev-only-insecure-secret-change-me"


def _sign(value: str) -> str:
    return _b64url(hmac.new(_session_secret(), value.encode("utf-8"), hashlib.sha256).digest())


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

def _get_cookie_value(cookie_name: str, cookie_header: Optional[str]) -> Optional[str]:
    if not cookie_header:
        return None
    for part in str(cookie_header).split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == cookie_name:
            return value
    return None


def _encode_cookie_value(payload: Mapping[str, Any]) -> str:
    """Encode the payload and attach an HMAC signature so it can't be forged."""
    serialized = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)
    body = _b64url(serialized.encode("utf-8"))
    return f"{body}.{_sign(body)}"


def _cookie_value_to_dict(value: Optional[str]) -> dict[str, Any]:
    """Decode a signed cookie. Returns {} if missing, malformed, or tampered with."""
    if not value or "." not in value:
        return {}
    body, _, signature = value.partition(".")
    expected = _sign(body)
    if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")):
        return {}
    try:
        padded = body + "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_session_cookie(cookie_header: Optional[str]) -> dict[str, Any]:
    return _cookie_value_to_dict(_get_cookie_value(SESSION_COOKIE_NAME, cookie_header))


def get_oauth_state_cookie(cookie_header: Optional[str]) -> Optional[str]:
    return _get_cookie_value(OAUTH_STATE_COOKIE_NAME, cookie_header)


def set_session_cookie(session_data: Mapping[str, Any], *, secure: Optional[bool] = None) -> str:
    secure = secure if secure is not None else os.getenv("APP_ENV", "development") == "production"
    payload = dict(session_data)
    if "expires_at" not in payload:
        payload["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).isoformat()
    encoded = _encode_cookie_value(payload)
    cookie = (
        f"{SESSION_COOKIE_NAME}={encoded}; Path=/; HttpOnly; SameSite=Lax; "
        f"Max-Age={SESSION_MAX_AGE_SECONDS}"
    )
    if secure:
        cookie += "; Secure"
    return cookie


def set_oauth_state_cookie(state_value: str, *, secure: Optional[bool] = None) -> str:
    secure = secure if secure is not None else os.getenv("APP_ENV", "development") == "production"
    cookie = f"{OAUTH_STATE_COOKIE_NAME}={state_value}; Path=/; HttpOnly; SameSite=Lax; Max-Age=600"
    if secure:
        cookie += "; Secure"
    return cookie


def clear_session_cookie() -> str:
    return (
        f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; "
        "Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0"
    )


def clear_oauth_state_cookie() -> str:
    return (
        f"{OAUTH_STATE_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; "
        "Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0"
    )


# ---------------------------------------------------------------------------
# Email / password auth
# ---------------------------------------------------------------------------

def register_user(email: str, password: str):
    client = get_supabase_client()
    if not email or not password:
        raise ValueError("Email and password are required.")
    response = client.auth.sign_up({"email": email, "password": password})
    if not response or not _get(response, "user"):
        raise ValueError("Registration failed.")

    session = _get(response, "session")
    session_token = _get(session, "access_token") if isinstance(session, Mapping) else None
    if session_token:
        return response

    try:
        sign_in_response = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception:
        return response

    if sign_in_response and _get(sign_in_response, "user") and _get(sign_in_response, "session"):
        return sign_in_response
    return response


def login_user(email: str, password: str):
    client = get_supabase_client()
    if not email or not password:
        raise ValueError("Email and password are required.")
    try:
        response = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as exc:  # pragma: no cover - surfaced to callers for security messages
        raise ValueError(str(exc)) from exc
    if not response or not _get(response, "user"):
        raise ValueError("Invalid login credentials.")
    return response


def _session_client_for_headers(headers: Optional[Mapping[str, Any]]):
    client = get_supabase_client()
    if not headers:
        return client
    session = get_session_cookie(headers.get("Cookie") or headers.get("cookie"))
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if access_token and hasattr(client, "auth") and hasattr(client.auth, "set_session"):
        try:
            client.auth.set_session(access_token, refresh_token or "")
        except Exception:
            pass
    return client


def create_profile(first_name, last_name, major, graduation_year, headers: Optional[Mapping[str, Any]] = None):
    user = require_auth(headers)
    user_id = user.get("id")
    if not user_id:
        raise ValueError("User is not authenticated.")
    client = _session_client_for_headers(headers)
    return (
        client.table("profiles")
        .upsert(
            {
                "id": user_id,
                "first_name": first_name,
                "last_name": last_name,
                "major": major,
                "graduation_year": graduation_year,
            },
            on_conflict="id",
        )
        .execute()
    )


def get_profile(headers: Optional[Mapping[str, Any]] = None):
    user = require_auth(headers) if headers is not None else None
    if not user:
        return None
    client = _session_client_for_headers(headers)
    return (
        client.table("profiles")
        .select("*")
        .eq("id", user.get("id"))
        .single()
        .execute()
        .data
    )


def logout_user() -> dict[str, str]:
    try:
        client = get_supabase_client()
        client.auth.sign_out()
    except Exception:
        pass
    return {"Set-Cookie": clear_session_cookie()}


# ---------------------------------------------------------------------------
# Request authentication
# ---------------------------------------------------------------------------

def _user_to_dict(user: Any) -> dict[str, Any]:
    metadata = _get(user, "user_metadata") or {}
    app_metadata = _get(user, "app_metadata") or {}
    email = _get(user, "email")
    fallback_name = email.split("@", 1)[0] if email else None
    return {
        "id": str(_get(user, "id")),
        "email": email,
        "name": metadata.get("full_name") or metadata.get("name") or fallback_name,
        "picture": metadata.get("avatar_url") or metadata.get("picture"),
        "provider": app_metadata.get("provider"),
    }


def to_session_user(user: Any) -> dict[str, Any]:
    """The compact user shape the session cookie and the UI both carry."""
    return _user_to_dict(user)


def _user_from_token(token: str) -> Optional[dict[str, Any]]:
    """Ask Supabase whether an access token is valid and who it belongs to."""
    try:
        auth = get_supabase_client().auth
        try:
            response = auth.get_user(token)
        except TypeError:
            response = auth.get_user()
    except Exception:
        return None
    user = _get(response, "user")
    return _user_to_dict(user) if user else None


def require_auth(headers: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not headers:
        raise PermissionError("Authentication required.")

    auth_header = headers.get("Authorization") or headers.get("authorization")
    cookie_header = headers.get("Cookie") or headers.get("cookie")

    # 1) Explicit bearer token: Supabase must confirm it is valid.
    if auth_header and str(auth_header).lower().startswith("bearer "):
        token = str(auth_header).split(" ", 1)[1].strip()
        user = _user_from_token(token) if token else None
        if user:
            return user
        raise PermissionError("Authentication required.")

    # 2) Signed session cookie (tampered or unsigned cookies decode to {}).
    session = get_session_cookie(cookie_header)
    if not session:
        raise PermissionError("Authentication required.")

    expires_at = session.get("expires_at")
    if expires_at:
        try:
            expires_dt = datetime.fromisoformat(str(expires_at))
        except ValueError:
            raise PermissionError("Session expired.")
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires_dt:
            raise PermissionError("Session expired.")

    user = session.get("user")
    if isinstance(user, dict) and user.get("id"):
        _bump("session_accepted")
        return user

    token = session.get("access_token")
    user = _user_from_token(token) if token else None
    if user:
        _bump("session_accepted")
        return user

    raise PermissionError("Authentication required.")


# ---------------------------------------------------------------------------
# Google sign-in (through Supabase, PKCE flow)
#
# Route contract:
#
#   GET /api/auth/google
#       state = secrets.token_urlsafe(32)
#       url = start_google_oauth({"state": state})
#       -> 302 to `url`, with header  set_oauth_state_cookie(state)
#
#   GET /api/auth/google/callback
#       state = get_oauth_state_cookie(<Cookie header>)
#       result = handle_google_oauth_callback(<query params>, {"state": state})
#       -> 302 to "/", with headers  set_session_cookie(result["session"])
#                                    clear_oauth_state_cookie()
#
# Google itself is configured inside Supabase (Authentication > Providers >
# Google), so no Google client ID/secret is needed in this app.
# ---------------------------------------------------------------------------

def _redirect_uri() -> str:
    """Full URL Supabase sends the browser back to after Google sign-in.

    APP_URL may be either the site root (https://www.hokieflow.tech) or the
    full callback URL; both work.
    """
    raw = (os.getenv("APP_URL") or "").strip().rstrip("/")
    if not raw:
        return f"http://127.0.0.1:8321{GOOGLE_CALLBACK_PATH}"
    if raw.endswith(GOOGLE_CALLBACK_PATH):
        return raw
    return raw + GOOGLE_CALLBACK_PATH


def canonical_origin() -> Optional[str]:
    """The origin the OAuth callback is registered for, taken from APP_URL.

    The state cookie is host-scoped, so the browser has to start and finish the
    sign-in on this origin. Returns None when APP_URL is unset (local dev),
    which disables the hand-over redirect.
    """
    raw = (os.getenv("APP_URL") or "").strip()
    if not raw:
        return None
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.hostname:
        return None
    return f"{parsed.scheme or 'https'}://{parsed.netloc}"


def _supabase_url() -> str:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    if not url:
        url = str(getattr(get_supabase_client(), "supabase_url", "") or "")
    if not url:
        raise RuntimeError("SUPABASE_URL is not set.")
    return url.rstrip("/")


def _pkce_verifier(state_value: str) -> str:
    """Derive the PKCE code verifier from the per-browser state value.

    The state lives in an HttpOnly cookie, and the HMAC key is server-side, so
    the verifier can be recomputed on the callback without server-side storage.
    The result is 43 URL-safe characters, which is a valid PKCE verifier.
    """
    digest = hmac.new(_session_secret(), f"pkce:{state_value}".encode("utf-8"), hashlib.sha256).digest()
    return _b64url(digest)


def _pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def start_google_oauth(request_state: Optional[Mapping[str, Any]] = None) -> str:
    """Return the Supabase URL that starts the Google sign-in flow."""
    state_value = (request_state or {}).get("state") or secrets.token_urlsafe(32)
    query = {
        "provider": "google",
        "redirect_to": _redirect_uri(),
        "state": state_value,
        "code_challenge": _pkce_challenge(_pkce_verifier(state_value)),
        "code_challenge_method": "s256",
    }
    _bump("oauth_started")
    return f"{_supabase_url()}/auth/v1/authorize?{urllib.parse.urlencode(query)}"


def login_with_google(request_state: Optional[Mapping[str, Any]] = None) -> str:
    return start_google_oauth(request_state)


def handle_google_oauth_callback(
    callback_params: Optional[Mapping[str, Any]],
    request_state: Optional[Mapping[str, Any]] = None,
):
    params = dict(callback_params or {})
    _bump("callback_reached")
    if params.get("error"):
        raise ValueError(str(params.get("error_description") or params["error"]))
    code = params.get("code")
    if not code:
        raise ValueError("Missing OAuth code.")

    # The state cookie ties this callback to the browser that started the login.
    # Without it the code verifier can't be rebuilt, so the exchange must fail.
    state_value = (request_state or {}).get("state")
    if not state_value:
        raise ValueError("Missing OAuth state. Please start the sign-in again.")
    _bump("state_ok")

    client = get_supabase_client()
    try:
        response = client.auth.exchange_code_for_session({
            "auth_code": str(code),
            "code_verifier": _pkce_verifier(state_value),
        })
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    _bump("exchange_ok")

    supa_session = _get(response, "session")
    supa_user = _get(response, "user") or _get(supa_session, "user")
    if not supa_session or not supa_user:
        raise ValueError("OAuth session was not created.")

    user = _user_to_dict(supa_user)
    user["provider"] = "google"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)
    return {
        "user": user,
        "session": {
            "access_token": _get(supa_session, "access_token") or "",
            "refresh_token": _get(supa_session, "refresh_token") or "",
            "expires_at": expires_at.isoformat(),
            "provider": "google",
            "user": user,
        },
    }


def logout():
    return logout_user()


def login(email, password):
    return login_user(email, password)


def sign_up(email, password):
    return register_user(email, password)