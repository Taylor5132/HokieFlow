import base64
import json
import os
import secrets
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


def _response_dict(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return {}


def _auth_call(method, payload):
    try:
        return _response_dict(method(payload))
    except Exception as exc:
        if any(word in str(exc).lower() for word in ("streamreset", "remote_reset", "connection", "timed out")):
            raise ValueError("The account service could not be reached. Please try again shortly.") from exc
        raise ValueError(str(exc)) from exc


def register_user(email: str, password: str):
    client = get_supabase_client()
    if not email or not password:
        raise ValueError("Email and password are required.")
    response = _auth_call(client.auth.sign_up, {"email": email, "password": password})
    if not response or not response.get("user"):
        raise ValueError("Registration failed.")
    return response


def login_user(email: str, password: str):
    client = get_supabase_client()
    if not email or not password:
        raise ValueError("Email and password are required.")
    try:
        response = _auth_call(client.auth.sign_in_with_password, {"email": email, "password": password})
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

    client = get_supabase_client()
    try:
        # Validate the supplied token, never a client's unrelated cached session.
        response = _response_dict(client.auth.get_user(token))
        user = response.get("user")
        if user:
            return user
    except Exception:
        pass

    raise PermissionError("Authentication required.")


def start_google_oauth(request_state: Optional[Mapping[str, Any]] = None):
    client = get_supabase_client()
    state_value = (request_state or {}).get("state") or secrets.token_urlsafe(32)
    response = client.auth.sign_in_with_oauth({
        "provider": "google",
        "options": {"redirect_to": os.getenv("APP_URL", "http://127.0.0.1:8321/api/auth/google/callback")},
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
