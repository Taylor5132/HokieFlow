import unittest
from unittest import mock

from auth import (
    register_user,
    login_user,
    logout_user,
    require_auth,
    start_google_oauth,
    handle_google_oauth_callback,
    get_session_cookie,
    set_session_cookie,
)


class FakeAuth:
    def __init__(self, user=None, *, error=None, oauth_url=None):
        self.user = user
        self.error = error
        self.oauth_url = oauth_url
        self.sign_out_calls = 0

    def sign_up(self, payload):
        if self.error:
            raise RuntimeError(self.error)
        return {"user": {"id": "user-1", "email": payload["email"]}, "session": {"access_token": "access-1", "refresh_token": "refresh-1"}}

    def sign_in_with_password(self, payload):
        if self.error:
            raise RuntimeError(self.error)
        if payload["password"] != "correctpass":
            raise ValueError("invalid login credentials")
        return {"user": {"id": "user-1", "email": payload["email"]}, "session": {"access_token": "access-1", "refresh_token": "refresh-1"}}

    def sign_out(self):
        self.sign_out_calls += 1
        return {"message": "logged out"}

    def get_user(self):
        if self.user is None:
            return None
        return {"user": self.user}

    def sign_in_with_oauth(self, payload):
        return {"url": self.oauth_url or "https://example.com/google/callback"}

    def exchange_code_for_session(self, code):
        if self.error:
            raise RuntimeError(self.error)
        self.user = {"id": "user-1", "email": "oauth@example.com"}
        return {"user": self.user, "session": {"access_token": "oauth-access", "refresh_token": "oauth-refresh"}}


class FakeClient:
    def __init__(self, *, user=None, error=None, oauth_url=None):
        self.auth = FakeAuth(user=user, error=error, oauth_url=oauth_url)


class TestAuth(unittest.TestCase):
    def test_register_user_success(self):
        fake = FakeClient()
        with mock.patch("auth.get_supabase_client", return_value=fake):
            result = register_user("alice@example.com", "secretpass")
        self.assertEqual(result["user"]["email"], "alice@example.com")
        self.assertIn("access_token", result["session"])

    def test_login_user_failure_and_success(self):
        bad = FakeClient(error="invalid login credentials")
        with mock.patch("auth.get_supabase_client", return_value=bad):
            with self.assertRaises(ValueError):
                login_user("alice@example.com", "wrongpass")

        good = FakeClient()
        with mock.patch("auth.get_supabase_client", return_value=good):
            result = login_user("alice@example.com", "correctpass")
        self.assertEqual(result["user"]["email"], "alice@example.com")

    def test_logout_user_clears_session_cookie(self):
        fake = FakeClient()
        with mock.patch("auth.get_supabase_client", return_value=fake):
            cookie = logout_user()
        self.assertIn("Set-Cookie", cookie)
        self.assertIn("expires=thu, 01 jan 1970", cookie["Set-Cookie"].lower())

    def test_require_auth_rejects_expired_session(self):
        expired = {"access_token": "expired-token", "expires_at": "2000-01-01T00:00:00+00:00"}
        self.assertRaises(PermissionError, require_auth, {"Cookie": f"hokieflow_session={expired['access_token']}"})

    def test_require_auth_rejects_missing_session(self):
        with self.assertRaises(PermissionError):
            require_auth({})

    def test_require_auth_accepts_valid_session(self):
        user = {"id": "user-1", "email": "alice@example.com"}
        fake = FakeClient(user=user)
        with mock.patch("auth.get_supabase_client", return_value=fake):
            result = require_auth({"Authorization": "Bearer access-1"})
        self.assertEqual(result["email"], "alice@example.com")

    def test_cross_user_isolation_uses_separate_session_cookies(self):
        alice = {"id": "user-a", "email": "a@example.com"}
        bob = {"id": "user-b", "email": "b@example.com"}
        first = {"Authorization": "Bearer access-a"}
        second = {"Authorization": "Bearer access-b"}
        with mock.patch("auth.get_supabase_client") as getter:
            getter.side_effect = [FakeClient(user=alice), FakeClient(user=bob)]
            self.assertEqual(require_auth(first)["email"], "a@example.com")
            self.assertEqual(require_auth(second)["email"], "b@example.com")

    def test_oauth_start_and_callback_validate_state(self):
        fake = FakeClient(oauth_url="https://example.com/start")
        with mock.patch("auth.get_supabase_client", return_value=fake), \
             mock.patch("auth.secrets.token_urlsafe", return_value="state-123"):
            redirect = start_google_oauth({})
        self.assertIn("state-123", redirect)

        with mock.patch("auth.get_supabase_client", return_value=FakeClient()):
            result = handle_google_oauth_callback({"state": "state-123", "code": "abc"}, {"state": "state-123"})
        self.assertEqual(result["user"]["email"], "oauth@example.com")

    def test_require_auth_rejects_expired_session(self):
        expired = {"access_token": "expired-token", "expires_at": "2000-01-01T00:00:00+00:00"}
        encoded = __import__("base64").urlsafe_b64encode(__import__("json").dumps(expired).encode("utf-8")).decode("utf-8").rstrip("=")
        with self.assertRaises(PermissionError):
            require_auth({"Cookie": f"hokieflow_session={encoded}"})

    def test_google_oauth_cancelled_or_failed(self):
        with self.assertRaises(ValueError):
            handle_google_oauth_callback({"error": "access_denied"}, {"state": "state-123"})

        with self.assertRaises(ValueError):
            handle_google_oauth_callback({"code": "abc"}, {"state": "diff"})

    def test_session_cookie_helpers_round_trip(self):
        cookie = set_session_cookie({"access_token": "t1", "refresh_token": "t2"})
        self.assertIn("HttpOnly", cookie)
        session = get_session_cookie(cookie)
        self.assertEqual(session["access_token"], "t1")
        self.assertEqual(session["refresh_token"], "t2")


if __name__ == "__main__":
    unittest.main()
