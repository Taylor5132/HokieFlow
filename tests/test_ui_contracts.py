"""Contract tests for the endpoints the shipped ui/ design actually calls.

These exist because the design was developed against `serve.py` while the
deployed entrypoint is `app.server`, and the two disagreed in ways no existing
test caught: a verb the UI uses but the server never implemented, a dining
directory fed from the wrong registry, and departure fields the client could not
parse. Each test below pins one of those contracts to what the client reads.

Run:  DEMO_MODE=cache python3 -m unittest tests.test_ui_contracts -v
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

from app import server, ui_files  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class DiningPlacesContract(unittest.TestCase):
    """ui/app.js reads p.name, p.building; the list must BE dining places."""

    def test_places_are_dining_locations_not_planner_waypoints(self):
        payload = ui_files.dining_places_endpoint()
        places = payload["places"]
        self.assertGreaterEqual(len(places), 8)
        names = {p["name"] for p in places}
        # The planner registry (the wrong source) contains these; none is dining.
        for waypoint in ("Burruss Hall", "McBryde Hall", "Hahn Hall", "Stop 1600"):
            self.assertNotIn(waypoint, names,
                             f"{waypoint} is a planner place, not a dining location")

    def test_every_place_carries_what_the_card_renders(self):
        payload = ui_files.dining_places_endpoint()
        for place in payload["places"]:
            self.assertTrue(place["name"])
            self.assertTrue(place["building"], "cards show the building subtitle")
            self.assertTrue(-90 <= place["lat"] <= 90)
            self.assertTrue(-180 <= place["lon"] <= 180)
            self.assertTrue(place["source_url"], "curated entries cite a source")

    def test_hours_and_open_state_are_absent_rather_than_invented(self):
        """CONNECTING.md: never imply open-now from a stale file."""
        payload = ui_files.dining_places_endpoint()
        flat = json.dumps(payload)
        self.assertNotIn("open_now", flat)
        self.assertNotIn("hours_text", flat)


class TransitDeparturesContract(unittest.TestCase):
    """ui/home-live.js parses departure_at and groups on route + stop_id."""

    def test_rows_use_the_field_names_the_client_reads(self):
        result = ui_files.transit_departures_endpoint("1600")
        rows = result["departures"]
        self.assertTrue(rows, "stop 1600 has departures on the snapshot day")
        for row in rows:
            self.assertIn("departure_at", row)
            self.assertIn("route", row)
            self.assertTrue(row["stop_id"])
            self.assertTrue(row["stop_name"], "the row prints the stop name")
            # And the planner's own names must not leak through instead.
            self.assertNotIn("dep_time", row)
            self.assertNotIn("route_id", row)

    def test_departure_at_is_parseable_by_date_parse(self):
        import datetime
        rows = ui_files.transit_departures_endpoint("1600")["departures"]
        for row in rows:
            parsed = datetime.datetime.fromisoformat(row["departure_at"])
            self.assertIsNotNone(parsed.tzinfo,
                                 "a naive timestamp shifts silently in the browser")
            self.assertEqual(row["route"][:2], row["route"][:2])

    def test_the_client_parser_keeps_rows_against_the_pinned_clock(self):
        """The regression: every row was dropped, so the card said 'none'."""
        import datetime
        from hokieday import config
        pinned = config.now()          # the replay clock the UI must trust
        rows = ui_files.transit_departures_endpoint("1600")["departures"]
        kept = [r for r in rows
                if r["departure_at"] and r["route"] and r["stop_name"]]
        self.assertTrue(kept, "the parser must have rows to group")
        # Every kept row must be upcoming at the pinned instant, which is what
        # makes the card render instead of reporting "no departures".
        for row in kept:
            self.assertGreater(
                datetime.datetime.fromisoformat(row["departure_at"]), pinned,
                "a departure behind the pinned clock would be filtered out")


class AccountSaveVerbContract(unittest.TestCase):
    """ui/app.js saves with PUT; the deployed server must implement it."""

    def test_put_is_implemented(self):
        self.assertTrue(hasattr(server.Handler, "do_PUT"),
                        "PUT /api/account/data is how the UI saves")

    def test_the_route_is_shared_by_both_verbs(self):
        import inspect
        put = inspect.getsource(server.Handler._route_put)
        post = inspect.getsource(server.Handler._route_post)
        self.assertIn("/api/account/data", put)
        self.assertIn("/api/account/data", post)
        for source in (put, post):
            self.assertIn("_account_save", source,
                          "one implementation, so the verbs cannot drift")
        # And each verb dispatches through the crash guard.
        for verb in ("do_PUT", "do_POST", "do_GET"):
            self.assertIn("_dispatch",
                          inspect.getsource(getattr(server.Handler, verb)))

    def test_unknown_put_path_is_404_not_a_dead_socket(self):
        import inspect
        self.assertIn('"not found"',
                      inspect.getsource(server.Handler._route_put))


class AssetMapContract(unittest.TestCase):
    """Every whitelisted asset must exist; every module must be whitelisted."""

    def test_no_mapped_asset_is_missing(self):
        from pathlib import Path
        ui = Path(__file__).resolve().parent.parent / "ui"
        on_disk = {p.name for p in ui.iterdir() if p.is_file()}
        import re
        src = (Path(__file__).resolve().parent.parent / "app" / "ui_files.py").read_text()
        mapped = set(re.findall(r'"([\w.\-]+\.(?:js|css|html))":\s*"text', src))
        self.assertEqual(mapped - on_disk, set(),
                         "a whitelisted asset that does not exist 404s in the browser")

    def test_every_ui_module_is_reachable(self):
        from pathlib import Path
        import re
        root = Path(__file__).resolve().parent.parent
        ui = root / "ui"
        src = (root / "app" / "ui_files.py").read_text()
        mapped = set(re.findall(r'"([\w.\-]+\.(?:js|css|html))":\s*"text', src))
        for path in ui.iterdir():
            if path.is_file() and path.suffix in (".js", ".css", ".html"):
                self.assertIn(path.name, mapped,
                              f"{path.name} is not served, so the app cannot load it")

    def test_the_design_app_and_its_assets_are_served(self):
        code, body, ctype, _ = ui_files.serve("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"app.js", body)
        code, body, ctype, _ = ui_files.serve("/app.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", ctype)


if __name__ == "__main__":
    unittest.main()

class HttpEdgeContractTests(unittest.TestCase):
    """The deployed server must answer odd requests, not drop the connection.

    A dropped connection is invisible locally but becomes 502 Bad Gateway behind
    a proxy, which is how the deployed app behaved: BaseHTTPRequestHandler's
    send_error() -> log_error() -> log_message() received an HTTPStatus enum,
    `"/api/" in HTTPStatus.X` raised TypeError inside send_error, and no error
    response was ever written.
    """

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _conn(self):
        import http.client
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)

    def test_log_message_survives_an_httpstatus_argument(self):
        """The exact crash: send_error passes an enum, not a string."""
        from http import HTTPStatus
        import io
        from contextlib import redirect_stderr
        handler = server.Handler.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 1234)
        with redirect_stderr(io.StringIO()):
            handler.log_message("code %d, message %s",
                                HTTPStatus.NOT_IMPLEMENTED, "Unsupported method")

    def test_unsupported_method_gets_a_response_not_an_empty_reply(self):
        for method in ("DELETE", "PATCH"):
            conn = self._conn()
            conn.request(method, "/api/ask", body="{}",
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 405, method)
            body = json.loads(response.read())
            self.assertIn("GET", body["allow"])
            conn.close()

    def test_options_and_head_are_answered(self):
        conn = self._conn()
        conn.request("OPTIONS", "/api/ask")
        self.assertEqual(conn.getresponse().status, 204)
        conn.close()
        conn = self._conn()
        conn.request("HEAD", "/api/ask")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()

    def test_malformed_request_line_still_gets_a_status_line(self):
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        try:
            sock.sendall(b"BOGUS /x HTTP/1.1\r\nHost: test\r\n\r\n")
            data = sock.recv(200)
            self.assertTrue(data.startswith(b"HTTP/1."),
                            f"expected a status line, got {data!r}")
        finally:
            sock.close()

    def test_oversized_body_is_refused_without_desyncing_the_socket(self):
        """A refused body must not poison the connection.

        The guard refuses without parsing, so the bytes are either discarded or
        the socket is closed. Leaving them unread on a keep-alive connection made
        the NEXT request parse as garbage, which the deployed proxy surfaced as a
        502. So: the 413 must arrive, and the same connection must still work.
        """
        conn = self._conn()
        conn.request("POST", "/api/ask",
                     body=json.dumps({"text": "x" * 1_100_000}),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 413)
        self.assertIn("limit_bytes", json.loads(response.read()))
        if response.getheader("Connection") == "close":
            conn.close()
            return
        conn.request("GET", "/api/status")     # same socket, new request
        follow_up = conn.getresponse()
        self.assertEqual(follow_up.status, 200,
                         "the connection must be usable after a refused body")
        follow_up.read()
        conn.close()

    def test_an_escaping_exception_never_becomes_an_empty_reply(self):
        """A crash must answer, not drop the socket.

        Deployed with APP_ENV=production and no SESSION_SECRET, auth.py refused
        to sign with the development secret and the request died silently: the
        client saw an empty reply, the proxy reported 502, and nothing said why.
        """
        from unittest import mock
        conn = self._conn()
        with mock.patch.object(server, "start_google_oauth",
                               side_effect=RuntimeError("config missing")):
            conn.request("GET", "/api/auth/google")
            response = conn.getresponse()
            self.assertEqual(response.status, 503)
            body = json.loads(response.read())
            self.assertEqual(body["status"], "unavailable")
            self.assertNotIn("config missing", body["error"],
                             "internals stay in the log, not in the response")
        conn.close()

    def test_an_unexpected_exception_is_a_500_not_a_dead_socket(self):
        from unittest import mock
        conn = self._conn()
        with mock.patch.object(server.ui_files, "dining_places_endpoint",
                               side_effect=ValueError("boom")):
            conn.request("GET", "/api/dining/places")
            response = conn.getresponse()
            self.assertEqual(response.status, 500)
            self.assertEqual(json.loads(response.read())["type"], "ValueError")
        conn.close()

    def test_normal_routes_still_answer_after_the_guard(self):
        for path, expected in (("/api/time", 200), ("/api/status", 200),
                               ("/api/dining/places", 200),
                               ("/api/transit/stops", 200),
                               ("/api/nope", 404)):
            conn = self._conn()
            conn.request("GET", path)
            self.assertEqual(conn.getresponse().status, expected, path)
            conn.close()

    def test_bounded_body_is_still_accepted(self):
        conn = self._conn()
        conn.request("POST", "/api/ask",
                     body=json.dumps({"text": "hungry, Burruss to McBryde by 1:25"}),
                     headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()


class DeploymentDiagnosticsContract(unittest.TestCase):
    """An operator must be able to tell WHY the agent is off, remotely.

    'credentials missing' and 'the app is in replay mode' need completely
    different fixes, and both looked identical from outside: enabled=false with
    no explanation. No secret value is exposed -- only presence and the model id.
    """

    @staticmethod
    def _status_offline():
        """status() with the live-bus read stubbed: no network, no cache write."""
        from unittest import mock
        return mock.patch.object(server.tools, "get_live_bus",
                                 return_value={"count": 0, "buses": [],
                                               "stale": False})

    def test_replay_mode_reports_that_as_the_reason(self):
        import os
        from unittest import mock
        with self._status_offline(), \
                mock.patch.object(server.config, "CACHE_ONLY", True), \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k",
                                             "GEMINI_MODEL": "gemini-x"},
                                clear=False):
            block = server.status()["agent"]
        self.assertFalse(block["enabled"])
        self.assertTrue(block["configured"])
        self.assertEqual(block["model"], "gemini-x")
        self.assertIn("replay mode", block["reason"])

    def test_missing_credentials_are_named_as_the_reason(self):
        import os
        from unittest import mock
        with self._status_offline(), \
                mock.patch.object(server.config, "CACHE_ONLY", False), \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "",
                                             "GEMINI_MODEL": "",
                                             "HOKIEFLOW_AI_PROVIDER": ""},
                                clear=False):
            block = server.status()["agent"]
        self.assertFalse(block["enabled"])
        self.assertFalse(block["configured"])
        self.assertIn("GEMINI_API_KEY", block["reason"])

    def test_enabled_live_mode_has_no_reason_to_report(self):
        import os
        from unittest import mock
        with self._status_offline(), \
                mock.patch.object(server.config, "CACHE_ONLY", False), \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k",
                                             "GEMINI_MODEL": "gemini-x"},
                                clear=False):
            block = server.status()["agent"]
        self.assertTrue(block["enabled"])
        self.assertTrue(block["configured"])
        self.assertIsNone(block["reason"])

    def test_the_status_block_never_contains_a_secret(self):
        import os
        from unittest import mock
        with self._status_offline(), \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "super-secret-key",
                                             "GEMINI_MODEL": "gemini-x"},
                                clear=False):
            rendered = json.dumps(server.status())
        self.assertNotIn("super-secret-key", rendered)


class ReplayStoreIsReadOnlyContract(unittest.TestCase):
    """fixtures/ must never be overwritten by an ordinary run.

    CACHE_DIR is resolved at import while CACHE_ONLY is read per call, so a
    process that started in replay mode and later treated itself as live wrote a
    fresh bus snapshot straight over the fixture -- moving the replay clock and
    emptying every replayed departure. The guard belongs at the write boundary.
    """

    def _envelope(self):
        return json.dumps({"key": "bt_buses",
                           "fetched_at": "2026-09-19T15:22:29+00:00",
                           "payload": {"data": []}})

    def test_an_accidental_write_is_refused(self):
        import tempfile
        from pathlib import Path as _Path
        from unittest import mock
        from hokieday import cache
        with tempfile.TemporaryDirectory() as td:
            store = _Path(td)
            target = store / "bt_buses.json"
            target.write_text(self._envelope())
            with mock.patch.object(cache.config, "FIXTURES_DIR", store), \
                    mock.patch.object(cache, "_FIXTURE_WRITES_ALLOWED", False):
                cache._write_envelope(target, "https://example.test/buses",
                                      {"data": [{"bus_id": "live"}]})
            self.assertEqual(json.loads(target.read_text())["fetched_at"],
                             "2026-09-19T15:22:29+00:00",
                             "the frozen snapshot must be byte-identical")

    def test_a_deliberate_seeding_run_may_write(self):
        import tempfile
        from pathlib import Path as _Path
        from unittest import mock
        from hokieday import cache
        with tempfile.TemporaryDirectory() as td:
            store = _Path(td)
            target = store / "bt_buses.json"
            target.write_text(self._envelope())
            with mock.patch.object(cache.config, "FIXTURES_DIR", store), \
                    mock.patch.object(cache, "_FIXTURE_WRITES_ALLOWED", True):
                cache._write_envelope(target, "https://example.test/buses",
                                      {"data": [{"bus_id": "seeded"}]},
                                      fetched_at="2026-09-20T06:56:12+00:00")
            written = json.loads(target.read_text())
            self.assertEqual(written["fetched_at"], "2026-09-20T06:56:12+00:00")
            self.assertEqual(written["payload"]["data"][0]["bus_id"], "seeded")

    def test_the_live_cache_directory_is_unaffected(self):
        import tempfile
        from pathlib import Path as _Path
        from unittest import mock
        from hokieday import cache
        with tempfile.TemporaryDirectory() as td:
            live = _Path(td) / "cache"
            live.mkdir()
            target = live / "bt_buses.json"
            with mock.patch.object(cache.config, "FIXTURES_DIR",
                                   _Path(td) / "fixtures"), \
                    mock.patch.object(cache, "_FIXTURE_WRITES_ALLOWED", False):
                cache._write_envelope(target, "https://example.test/buses",
                                      {"data": [{"bus_id": "live"}]})
            self.assertTrue(target.exists(), "live caching still works")


class OAuthHostContract(unittest.TestCase):
    """Google sign-in has to start and finish on one host.

    The OAuth state is an HttpOnly cookie, so it is scoped to the host that set
    it, while Supabase returns the browser to APP_URL. Starting the flow on the
    Azure alias therefore dropped the cookie: the callback answered "Missing
    OAuth state", sent the browser to /?auth_error=..., and the home screen
    rendered signed out with no explanation because the login screen (the only
    place that showed the message) was never opened.
    """

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _google_start(self, host):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("GET", "/api/auth/google", headers={"Host": host})
        response = conn.getresponse()
        headers = dict(response.getheaders())
        response.read()
        conn.close()
        return response.status, headers

    @unittest.skipIf(not server.AUTH_AVAILABLE,
                     "auth needs supabase + python-dotenv (pip install -r requirements.txt)")
    def test_canonical_origin_is_read_from_app_url(self):
        from unittest import mock
        from auth import canonical_origin
        for value, expected in {
            "https://hokieflow.tech": "https://hokieflow.tech",
            "https://hokieflow.tech/": "https://hokieflow.tech",
            "https://hokieflow.tech/api/auth/google/callback": "https://hokieflow.tech",
            "http://127.0.0.1:8321": "http://127.0.0.1:8321",
        }.items():
            with mock.patch.dict(os.environ, {"APP_URL": value}):
                self.assertEqual(canonical_origin(), expected, value)
        with mock.patch.dict(os.environ, {"APP_URL": ""}):
            self.assertIsNone(canonical_origin(),
                              "an unset APP_URL leaves local dev alone")

    @unittest.skipIf(not server.AUTH_AVAILABLE,
                     "auth needs supabase + python-dotenv (pip install -r requirements.txt)")
    def test_the_callback_follows_the_host_the_browser_uses(self):
        """started on www -> comes back to www; unknown hosts fall back to APP_URL."""
        from auth import redirect_uri_for
        with mock.patch.dict(os.environ, {"APP_URL": "https://hokieflow.tech"}):
            self.assertEqual(redirect_uri_for("hokieflow.tech"),
                             "https://hokieflow.tech/api/auth/google/callback")
            self.assertEqual(redirect_uri_for("www.hokieflow.tech"),
                             "https://www.hokieflow.tech/api/auth/google/callback",
                             "the Site URL is www: forcing apex moves the state cookie")
            self.assertEqual(redirect_uri_for("WWW.HokieFlow.tech"),
                             "https://www.hokieflow.tech/api/auth/google/callback")
            for hostile in ("evil.example", "hokieflow.tech.evil.example", "", None):
                self.assertEqual(redirect_uri_for(hostile),
                                 "https://hokieflow.tech/api/auth/google/callback",
                                 "only hosts derived from APP_URL may be used")

    def test_the_request_host_reaches_the_sign_in_flow(self):
        """The handler must pass the browser's Host through, not assume one."""
        seen = {}

        def fake_start(request_state=None, request_host=None):
            seen["host"] = request_host
            seen["state"] = (request_state or {}).get("state")
            return "https://project.supabase.co/auth/v1/authorize?provider=google"

        with mock.patch.object(server, "AUTH_AVAILABLE", True), \
                mock.patch.object(server, "start_google_oauth", fake_start), \
                mock.patch.object(server, "set_oauth_state_cookie",
                                  lambda state: f"hokieflow_oauth_state={state}; Path=/"):
            status, headers = self._google_start("www.hokieflow.tech")
        self.assertEqual(status, 302)
        self.assertIn("supabase", headers["Location"])
        self.assertEqual(seen["host"], "www.hokieflow.tech")
        self.assertTrue(seen["state"], "state is still generated per browser")
        self.assertIn("hokieflow_oauth_state", headers.get("Set-Cookie", ""))

    def test_the_browser_says_why_sign_in_failed(self):
        source = (REPO / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("escape(googleAuthErrorMessage)", source,
                      "auth_error must be rendered, not captured and dropped")


class AuthSessionContract(unittest.TestCase):
    """What the browser is handed after a successful sign-in.

    ui/app.js renders `Hi, ${state.user.name}` and takes `name[0]` for the
    avatar, but /api/auth/me used to answer with the id and email only, so a
    signed-in account rendered as "Hi, undefined" and the account panel threw.
    Sign-in also finishes inside a redirect, which leaves no visible failure:
    /api/status therefore publishes stage counters for it.
    """

    SECRET = "contract-test-secret-0123456789abcdef"

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _call(self, method, path, payload=None, cookie=None):
        import http.client
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request(method, path,
                     body=json.dumps(payload) if payload is not None else None,
                     headers=headers)
        response = conn.getresponse()
        headers_out = dict(response.getheaders())
        raw = response.read()
        conn.close()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        return response.status, headers_out, body

    def _signed_cookie(self, payload):
        """The documented cookie format: base64url(json).hmac_sha256(secret)."""
        import base64
        import hashlib
        import hmac
        body = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"),
                       sort_keys=True).encode()).rstrip(b"=").decode()
        signature = base64.urlsafe_b64encode(
            hmac.new(self.SECRET.encode(), body.encode(),
                     hashlib.sha256).digest()).rstrip(b"=").decode()
        return f"hokieflow_session={body}.{signature}"

    @unittest.skipIf(not server.AUTH_AVAILABLE,
                     "auth needs supabase + python-dotenv (pip install -r requirements.txt)")
    def test_me_carries_the_display_name_the_client_renders(self):
        payload = {
            "access_token": "jwt",
            "expires_at": "2999-01-01T00:00:00+00:00",
            "user": {"id": "u1", "email": "student@vt.edu", "name": "A Student",
                     "picture": "https://example.test/a.png", "provider": "google"},
        }
        with mock.patch.dict(os.environ, {"SESSION_SECRET": self.SECRET}):
            status, _, body = self._call("GET", "/api/auth/me",
                                         cookie=self._signed_cookie(payload))
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["name"], "A Student",
                         "the greeting and avatar initial read user.name")
        self.assertEqual(body["user"]["email"], "student@vt.edu")

    @unittest.skipIf(not server.AUTH_AVAILABLE,
                     "auth needs supabase + python-dotenv (pip install -r requirements.txt)")
    def test_a_forged_session_cookie_is_still_refused(self):
        payload = {"access_token": "jwt", "user": {"id": "u1", "email": "x@vt.edu"}}
        forged = self._signed_cookie(payload).replace("hokieflow_session=",
                                                      "hokieflow_session=") + "x"
        with mock.patch.dict(os.environ, {"SESSION_SECRET": self.SECRET}):
            status, _, body = self._call("GET", "/api/auth/me", cookie=forged)
        self.assertEqual(status, 200)
        self.assertIsNone(body["user"])

    def test_user_ref_keeps_a_name_from_a_raw_provider_object(self):
        class RawUser:
            id = "u2"
            email = "raw@vt.edu"
            user_metadata = {"full_name": "Raw Student"}
            app_metadata = {"provider": "google"}

        self.assertEqual(ui_files.user_ref(RawUser())["name"], "Raw Student")
        self.assertEqual(ui_files.user_ref({"id": "u3", "email": "netid@vt.edu"})["name"],
                         "netid", "no display name still yields what the greeting needs")

    def test_registration_without_a_session_does_not_sign_anyone_in(self):
        """Supabase returns a user but no session until the email is confirmed."""
        fake = lambda email, password: {"user": {"id": "u4", "email": email},
                                        "session": None}  # noqa: E731
        with mock.patch.object(server, "AUTH_AVAILABLE", True), \
                mock.patch.object(server, "register_user", fake):
            status, headers, body = self._call("POST", "/api/auth/register",
                                               {"email": "new@vt.edu",
                                                "password": "a-long-enough-password"})
        self.assertEqual(status, 201)
        self.assertNotIn("Set-Cookie", headers,
                         "no session token means no signed-in cookie")
        self.assertIn("message", body)

    def test_status_publishes_the_sign_in_funnel_without_secrets(self):
        status, _, body = self._call("GET", "/api/status")
        self.assertEqual(status, 200)
        auth = body["auth"]
        self.assertIn("available", auth)
        self.assertIn("canonical_origin", auth)
        for stage in ("oauth_started", "callback_reached", "state_ok",
                      "exchange_ok", "session_accepted"):
            self.assertIn(stage, auth["funnel"], "sign-in must be observable")
        text = json.dumps(auth).lower()
        self.assertNotIn("token", text)
        self.assertNotIn("secret", text)
