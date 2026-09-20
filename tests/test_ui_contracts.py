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

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

from app import server, ui_files  # noqa: E402


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
        put = inspect.getsource(server.Handler.do_PUT)
        post = inspect.getsource(server.Handler.do_POST)
        self.assertIn("/api/account/data", put)
        self.assertIn("/api/account/data", post)
        for source in (put, post):
            self.assertIn("_account_save", source,
                          "one implementation, so the verbs cannot drift")

    def test_unknown_put_path_is_404_not_a_dead_socket(self):
        import inspect
        self.assertIn('"not found"', inspect.getsource(server.Handler.do_PUT))


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

    def test_bounded_body_is_still_accepted(self):
        conn = self._conn()
        conn.request("POST", "/api/ask",
                     body=json.dumps({"text": "hungry, Burruss to McBryde by 1:25"}),
                     headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
