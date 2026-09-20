import base64
import json
import os
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from database import get_supabase_client

SESSION_COOKIE_NAME = "hokieflow_session"
OAUTH_STATE_COOKIE_NAME = "hokieflow_oauth_state"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7


def _get_cookie_value(cookie_name: str, cookie_header: Optional[str]) -> Optional[str]:
    if not cookie_header:
        return None
    for part in str(cookie_header).split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == cookie_name:
            return value
    return None


def _cookie_value_to_dict(value: Optional[str]) -> dict[str, Any]:
    if not value:
        return {}
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("utf-8"))
        decoded = payload.decode("utf-8")
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _encode_cookie_value(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(serialized.encode("utf-8")).decode("utf-8").rstrip("=")


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


def register_user(email: str, password: str):
    client = get_supabase_client()
    if not email or not password:
        raise ValueError("Email and password are required.")
    response = client.auth.sign_up({"email": email, "password": password})
    if not response or not response.get("user"):
        raise ValueError("Registration failed.")
    return response


def login_user(email: str, password: str):
    client = get_supabase_client()
    if not email or not password:
        raise ValueError("Email and password are required.")
    try:
        response = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as exc:  # pragma: no cover - surfaced to callers for security messages
        raise ValueError(str(exc)) from exc
    if not response or not response.get("user"):
        raise ValueError("Invalid login credentials.")
    return response


def create_profile(first_name, last_name, major, graduation_year):
    client = get_supabase_client()
    user_response = client.auth.get_user()
    if not user_response or not getattr(user_response, "user", None):
        raise ValueError("User is not authenticated.")
    user_id = user_response.user.id
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


def login_with_google():
    client = get_supabase_client()
    return client.auth.sign_in_with_oauth({"provider": "google"})
    

def get_profile():
    client = get_supabase_client()
    user_response = client.auth.get_user()
    if not user_response or not getattr(user_response, "user", None):
        return None
    return (
        client.table("profiles")
        .select("*")
        .eq("id", user_response.user.id)
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


def require_auth(headers: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not headers:
        raise PermissionError("Authentication required.")

    auth_header = headers.get("Authorization") or headers.get("authorization")
    cookie_header = headers.get("Cookie") or headers.get("cookie")
    session = get_session_cookie(cookie_header)
    expires_at = session.get("expires_at")
    if expires_at:
        try:
            expires_dt = datetime.fromisoformat(str(expires_at))
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= expires_dt:
                raise PermissionError("Session expired.")
        except ValueError:
            pass

    token = None
    if auth_header and str(auth_header).lower().startswith("bearer "):
        token = str(auth_header).split(" ", 1)[1]
    elif session.get("access_token"):
        token = session["access_token"]

    if not token:
        raise PermissionError("Authentication required.")

    if isinstance(session.get("user"), dict):
        return session["user"]

    client = get_supabase_client()
    try:
        user_response = client.auth.get_user()
        if user_response and getattr(user_response, "user", None):
            return user_response.user
    except Exception:
        pass

    if isinstance(client.auth, object) and hasattr(client.auth, "user") and client.auth.user:
        return client.auth.user

    raise PermissionError("Authentication required.")


def _google_oauth_env() -> tuple[str, str, str]:
    client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()
    redirect_uri = (os.getenv("APP_URL") or "http://127.0.0.1:8321/api/auth/google/callback").strip()
    return client_id, client_secret, redirect_uri


def _decode_google_id_token(id_token: str) -> dict[str, Any]:
    if not id_token:
        return {}
    try:
        header_b64, payload_b64, _, = str(id_token).split(".")
        if not payload_b64:
            return {}
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def start_google_oauth(request_state: Optional[Mapping[str, Any]] = None):
    client_id, _, redirect_uri = _google_oauth_env()
    if client_id:
        state_value = (request_state or {}).get("state") or secrets.token_urlsafe(32)
        query = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state_value,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(query)}"

    client = get_supabase_client()
    state_value = (request_state or {}).get("state") or secrets.token_urlsafe(32)
    response = client.auth.sign_in_with_oauth({
        "provider": "google",
        "options": {"redirect_to": redirect_uri},
    })
    redirect_url = response.get("url") if isinstance(response, dict) else str(response)
    if "?" in redirect_url:
        redirect_url = f"{redirect_url}&state={state_value}"
    else:
        redirect_url = f"{redirect_url}?state={state_value}"
    return redirect_url


def handle_google_oauth_callback(callback_params: Optional[Mapping[str, Any]], request_state: Optional[Mapping[str, Any]] = None):
    params = dict(callback_params or {})
    if params.get("error"):
        raise ValueError(str(params.get("error")))
    if not params.get("code"):
        raise ValueError("Missing OAuth code.")
    expected_state = (request_state or {}).get("state")
    received_state = params.get("state")
    if expected_state and received_state != expected_state:
        raise ValueError("Invalid OAuth state.")

    client_id, client_secret, redirect_uri = _google_oauth_env()
    if client_id and client_secret:
        token_req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=urllib.parse.urlencode({
                "code": params["code"],
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        if "id_token" not in body and "access_token" not in body:
            raise ValueError("OAuth token exchange failed.")
        id_token = body.get("id_token")
        claims = _decode_google_id_token(id_token) if id_token else {}
        if not claims:
            raise ValueError("OAuth token response did not include a valid ID token.")
        now = datetime.now(timezone.utc).timestamp()
        if str(claims.get("aud")) != client_id:
            raise ValueError("OAuth ID token audience mismatch.")
        if claims.get("iss") not in {"https://accounts.google.com", "accounts.google.com"}:
            raise ValueError("OAuth ID token issuer mismatch.")
        exp = claims.get("exp")
        if exp is not None and float(exp) < now:
            raise ValueError("OAuth ID token has expired.")
        email = claims.get("email")
        if not email:
            raise ValueError("OAuth ID token missing email claim.")
        user = {
            "id": claims.get("sub") or email,
            "email": email,
            "name": claims.get("name") or email.split("@", 1)[0],
            "picture": claims.get("picture"),
            "provider": "google",
        }
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE_SECONDS)
        return {
            "user": user,
            "session": {
                "access_token": body.get("access_token") or "",
                "refresh_token": body.get("refresh_token") or "",
                "expires_at": expires_at.isoformat(),
                "provider": "google",
                "user": user,
            },
        }

    client = get_supabase_client()
    try:
        session = client.auth.exchange_code_for_session(params["code"])
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    if not session or not session.get("user"):
        raise ValueError("OAuth session was not created.")
    return session


def logout():
    return logout_user()


def login(email, password):
    return login_user(email, password)


def sign_up(email, password):
    return register_user(email, password)
