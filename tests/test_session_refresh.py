"""A signed-in session must not turn into "please sign in again" after an hour.

The session cookie is valid for a week, but the Supabase access token inside it
expires in about an hour. Identity survived that (the cookie carries the user),
so the UI kept showing a signed-in student while every account read and write
handed PostgREST a dead token -- which answers with "JWT expired", surfacing as
"Your account could not access saved data. Please sign in again."

`app/supabase_session.token_for` refreshes the token before it is used. These
tests pin that behaviour without touching the network.

Run:  DEMO_MODE=cache python3 -m unittest tests.test_session_refresh -v
"""
from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

from app import server, supabase_session  # noqa: E402

_AUTH_NEEDS_DEPS = "auth needs supabase + python-dotenv (pip install -r requirements.txt)"


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _session(*, access_token="old-token", refresh_token="refresh-abc",
             expires_in_s=3600, token_expires_at=None) -> dict:
    session = {"access_token": access_token, "refresh_token": refresh_token,
               "expires_at": (datetime.now(timezone.utc)
                              + timedelta(days=7)).isoformat()}
    if token_expires_at is not None:
        session["token_expires_at"] = token_expires_at
    return session


class TokenRefreshTests(unittest.TestCase):
    def setUp(self):
        supabase_session._CACHE.clear()
        os.environ["SUPABASE_URL"] = "https://project.supabase.co"
        os.environ["SUPABASE_KEY"] = "anon-key-for-tests"
        self.addCleanup(os.environ.pop, "SUPABASE_URL", None)
        self.addCleanup(os.environ.pop, "SUPABASE_KEY", None)

    def _patch_transport(self, payload, *, record):
        def opener(request, timeout=None):
            record.append(request)
            return _FakeResponse(payload)
        self._original = supabase_session.urllib.request.urlopen
        supabase_session.urllib.request.urlopen = opener
        self.addCleanup(setattr, supabase_session.urllib.request, "urlopen",
                        self._original)

    def test_a_live_token_is_used_without_any_network_call(self):
        calls = []
        self._patch_transport({}, record=calls)
        session = _session(token_expires_at=(datetime.now(timezone.utc)
                                             + timedelta(minutes=30)).isoformat())
        self.assertEqual(supabase_session.token_for(session), "old-token")
        self.assertEqual(calls, [], "a healthy token must not be refreshed")

    def test_an_expired_token_is_refreshed_once_and_then_reused(self):
        calls = []
        self._patch_transport({"access_token": "fresh-token", "expires_in": 3600},
                              record=calls)
        session = _session(token_expires_at=(datetime.now(timezone.utc)
                                             - timedelta(minutes=5)).isoformat())
        self.assertEqual(supabase_session.token_for(session), "fresh-token")
        self.assertEqual(len(calls), 1)
        # A second request from the same stale cookie reuses the fresh token.
        self.assertEqual(supabase_session.token_for(session), "fresh-token")
        self.assertEqual(len(calls), 2 - 1, "the refreshed token is cached")

        sent = calls[0].full_url
        self.assertIn("grant_type=refresh_token", sent)
        self.assertIn("/auth/v1/token", sent)
        self.assertEqual(json.loads(calls[0].data.decode())["refresh_token"],
                         "refresh-abc")

    def test_a_session_without_a_recorded_expiry_is_refreshed(self):
        """Cookies minted before the expiry was stored must heal themselves."""
        calls = []
        self._patch_transport({"access_token": "healed", "expires_in": 3600},
                              record=calls)
        self.assertEqual(supabase_session.token_for(_session()), "healed")
        self.assertEqual(len(calls), 1)

    def test_a_failed_refresh_does_not_raise_or_sign_anyone_out(self):
        calls = []
        self._patch_transport({}, record=calls)   # no access_token in the reply
        session = _session(token_expires_at=(datetime.now(timezone.utc)
                                             - timedelta(minutes=5)).isoformat())
        self.assertEqual(supabase_session.token_for(session), "old-token",
                         "a provider hiccup must not drop the session")

    def test_no_refresh_token_means_no_call_but_no_sign_out_either(self):
        calls = []
        self._patch_transport({"access_token": "x"}, record=calls)
        # Without a refresh token there is nothing to refresh with, so the stored
        # token is offered as-is (the provider, not this module, judges it) and
        # no network call is made.
        self.assertEqual(supabase_session.token_for({"access_token": "only"}),
                         "only")
        self.assertEqual(calls, [])
        os.environ.pop("SUPABASE_URL", None)
        self.assertEqual(supabase_session.token_for(_session()), "old-token")
        self.assertEqual(calls, [])

    def test_tokens_are_never_written_to_logs(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        calls = []
        self._patch_transport({"access_token": "super-secret-token",
                               "expires_in": 3600}, record=calls)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            supabase_session.token_for(_session())
        self.assertNotIn("super-secret-token", out.getvalue() + err.getvalue())
        self.assertNotIn("refresh-abc", out.getvalue() + err.getvalue())

    def test_expiry_is_read_from_seconds_or_iso(self):
        epoch = time.time() + 1800
        self.assertAlmostEqual(
            supabase_session._expiry_epoch({"token_expires_at": epoch}), epoch)
        iso = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        self.assertGreater(supabase_session._expiry_epoch(
            {"token_expires_at": iso}), time.time())
        self.assertEqual(supabase_session._expiry_epoch({}), 0.0)
        self.assertEqual(supabase_session._expiry_epoch({"token_expires_at": "nonsense"}), 0.0)


class SessionPayloadTests(unittest.TestCase):
    """The cookie must record Supabase's own token expiry, not just its own."""

    @unittest.skipIf(not server.AUTH_AVAILABLE, _AUTH_NEEDS_DEPS)
    def test_the_google_callback_records_the_token_expiry(self):
        from auth import _token_expiry
        stamp = time.time() + 3600
        self.assertGreater(
            datetime.fromisoformat(_token_expiry({"expires_at": stamp})).timestamp(),
            time.time())
        self.assertGreater(
            datetime.fromisoformat(_token_expiry({"expires_in": 3600})).timestamp(),
            time.time())
        self.assertEqual(_token_expiry({}), "")

    @unittest.skipIf(not server.AUTH_AVAILABLE, _AUTH_NEEDS_DEPS)
    def test_the_email_path_records_it_too(self):
        from app import server
        # Supabase's own shape: {"session": {...}, "user": {...}}
        payload = server._session_payload({
            "session": {"access_token": "t", "refresh_token": "r",
                        "expires_in": 3600},
            "user": {"id": "u1", "email": "a@vt.edu"}})
        self.assertIn("token_expires_at", payload)
        self.assertGreater(
            datetime.fromisoformat(payload["token_expires_at"]).timestamp(),
            time.time())
        # The cookie itself still lives a week: an hour-long token must not sign
        # anyone out on its own.
        self.assertGreater(
            datetime.fromisoformat(payload["expires_at"]).timestamp(),
            time.time() + 6 * 24 * 3600)


if __name__ == "__main__":
    unittest.main()