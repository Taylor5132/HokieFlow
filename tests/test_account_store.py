"""Exercise the real Supabase query builder against an offline Data API double."""
import copy
import json
import unittest
from unittest.mock import patch

from app import account_store as store, ui_files
try:
    import httpx
    from supabase import ClientOptions, create_client
    from auth import set_session_cookie
    HAS_AUTH = True
except ImportError:
    HAS_AUTH = False

UID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class DataAPI:
    def __init__(self):
        self.rows = {}
        self.requests = []
        self.error = None
        self.race = False
        self.users = {"user-one-token": UID, "user-two-token": OTHER}

    def handle(self, request):
        self.requests.append(request)
        assert request.url.path == "/rest/v1/saved_plans", request.url
        if self.error:
            return httpx.Response(400, json={"code": self.error, "message": "secret provider detail", "details": None, "hint": None})
        uid = self.users.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if not uid:
            return httpx.Response(401, json={"code": "PGRST301", "message": "invalid token", "details": None, "hint": None})
        p = request.url.params
        if request.method == "GET":
            row = self.rows.get(p["id"].removeprefix("eq."))
            visible = row and row["user_id"] == uid and p["user_id"] == "eq." + uid
            return httpx.Response(200, json=[{"payload": row["payload"]}] if visible else [])
        body = json.loads(request.content)
        if request.method == "POST":
            if body["user_id"] != uid:
                return httpx.Response(403, json={"code": "42501", "message": "RLS denied", "details": None, "hint": None})
            if body["id"] in self.rows or self.race:
                return httpx.Response(409, json={"code": "23505", "message": "duplicate", "details": None, "hint": None})
            self.rows[body["id"]] = body
            return httpx.Response(201, json=[body])
        if request.method == "PATCH":
            row = self.rows.get(p["id"].removeprefix("eq."))
            if self.race:
                row["payload"]["version"] += 1
            matches = row and row["user_id"] == uid and p["user_id"] == "eq." + uid
            matches = matches and p["payload->>version"] == "eq." + str(row["payload"]["version"])
            if not matches:
                return httpx.Response(200, json=[])
            row.update(body)
            return httpx.Response(200, json=[row])
        raise AssertionError(request.method)


@unittest.skipUnless(HAS_AUTH, "requires optional Supabase dependencies")
class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.api = DataAPI()
        self.clients = []

        def client():
            transport = httpx.Client(transport=httpx.MockTransport(self.api.handle))
            self.clients.append(transport)
            return create_client("https://example.supabase.co", "public-test-key",
                                 options=ClientOptions(httpx_client=transport,
                                                       auto_refresh_token=False,
                                                       persist_session=False))
        self.factory = patch("database.get_supabase_client", side_effect=client)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.env = patch.dict("os.environ", {"SESSION_SECRET": "test-account-store-secret", "APP_ENV": "development"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.user = {"id": UID, "email": "test@example.test", "name": "Student"}
        self.headers = {"Cookie": set_session_cookie({"user": self.user, "access_token": "user-one-token"}).split(";", 1)[0]}
        self.data = {**copy.deepcopy(store.EMPTY_DATA), "events": [{"id": "event-1", "title": "Study Abroad Fair"}]}

    def test_plus_persists_and_reload_preserves_login_schedule_and_other_plans(self):
        self.api.rows["ordinary-plan"] = {"user_id": UID, "payload": {"keep": True}}
        initial = ui_files.enrich_account({"user": self.user}, self.user, headers=self.headers)
        self.assertEqual(initial["version"], 0)
        saved, status = ui_files.save_account(self.user, self.data, 0, headers=self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(saved["user"]["id"], UID)
        again = ui_files.enrich_account({"user": self.user}, self.user, headers=self.headers)
        self.assertEqual(again["data"], self.data)
        self.assertEqual(again["version"], 1)
        self.assertEqual(self.api.rows["ordinary-plan"]["payload"], {"keep": True})
        changed = {**self.data, "events": []}
        self.assertEqual(ui_files.save_account(self.user, changed, 1, headers=self.headers)[1], 200)
        self.assertEqual(store.load(UID, "user-one-token")["data"]["events"], [])
        self.assertTrue(all(c.is_closed for c in self.clients))

    def test_users_are_isolated_and_token_is_never_in_response(self):
        store.save(UID, "user-one-token", self.data, 0)
        self.assertEqual(store.load(OTHER, "user-two-token")["version"], 0)
        with self.assertRaises(store.StorageError) as error:
            store.save(UID, "user-two-token", self.data, 0)
        self.assertEqual(error.exception.status, 401)
        result = ui_files.enrich_account({"user": self.user}, self.user, headers=self.headers)
        self.assertNotIn("user-one-token", json.dumps(result))
        self.assertNotIn("public-test-key", json.dumps(result))

    def test_bearer_and_new_login_tokens_scope_database_reads(self):
        for kwargs in ({"headers": {"Authorization": "Bearer user-one-token"}},
                       {"access_token": "user-one-token"}):
            self.assertNotIn("storageError", ui_files.enrich_account({}, self.user, **kwargs))
        self.assertTrue(all(r.headers["authorization"] == "Bearer user-one-token" for r in self.api.requests))

    def test_missing_token_never_falls_back_to_anonymous_database_access(self):
        result = ui_files.enrich_account({"user": self.user}, self.user)
        self.assertIn("storageError", result)
        self.assertNotIn("data", result)
        self.assertEqual(ui_files.save_account(self.user, self.data, 0)[1], 401)
        self.assertFalse(self.api.requests)

    def test_stale_and_racing_updates_cannot_overwrite_saved_schedule(self):
        store.save(UID, "user-one-token", self.data, 0)
        for version, race in ((0, False), (1, True)):
            self.api.race = race
            result, status = ui_files.save_account(self.user, {**self.data, "events": []}, version, headers=self.headers)
            self.assertEqual(status, 409)
            self.assertEqual(self.api.rows[store.row_id(UID)]["payload"]["data"], self.data)

    def test_racing_first_insert_reports_conflict(self):
        self.api.race = True
        self.assertEqual(ui_files.save_account(self.user, self.data, 0, headers=self.headers)[1], 409)

    def test_storage_errors_are_safe_and_not_reported_as_empty_accounts(self):
        for code, status in (("PGRST205", 503), ("42501", 401), ("PGRST301", 401)):
            self.api.error = code
            result = ui_files.enrich_account({"user": self.user}, self.user, headers=self.headers)
            self.assertIn("storageError", result)
            self.assertNotIn("data", result)
            saved, actual = ui_files.save_account(self.user, self.data, 0, headers=self.headers)
            self.assertEqual(actual, status)
            self.assertNotIn("secret provider detail", json.dumps(saved))

    def test_invalid_data_rejected_before_database_write(self):
        for data, version in (({}, True), ({"events": "bad"}, 0), ({"plans": [None] * 21}, 0)):
            self.assertEqual(ui_files.save_account(self.user, data, version, headers=self.headers)[1], 400)
        self.assertFalse(self.api.requests)

    def test_browser_http_save_then_reload_uses_signed_session_and_keeps_identity(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from app import server
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            def request(method, path, payload=None):
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                headers = {**self.headers, "Content-Type": "application/json", "X-HokieFlow-Request": "1"}
                conn.request(method, path, json.dumps(payload) if payload is not None else None, headers)
                response = conn.getresponse()
                result = response.status, json.loads(response.read())
                conn.close()
                return result
            code, saved = request("PUT", "/api/account/data", {"data": self.data, "version": 0})
            self.assertEqual(code, 200, saved)
            self.assertEqual(saved["user"]["id"], UID)
            code, loaded = request("GET", "/api/auth/me")
            self.assertEqual(code, 200)
            self.assertEqual(loaded["data"], self.data)
            self.assertEqual(loaded["version"], 1)
            self.assertEqual(request("PUT", "/api/account/data", {"data": self.data, "version": 0})[0], 409)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
