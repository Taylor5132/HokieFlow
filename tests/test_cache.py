"""Deterministic concurrency/cooldown tests for hokieday.cache.

OWNER: cache-safety worker. These tests exist because a ThreadingHTTPServer
serving the live map let two requests see a stale/missing ``bt_buses`` entry at
once: both called upstream and raced on the same ``<key>.tmp`` file (observed:
2 upstream calls + one FileNotFoundError). A failed refresh with stale data also
re-fetched on EVERY subsequent request, defeating the 60s politeness window.

The suite itself is offline. It runs under DEMO_MODE=cache (like the rest of
tests/) but flips config.CACHE_ONLY/CACHE_DIR to a temp dir so the live code
paths are exercised against a fake ``_http`` -- no socket is ever opened.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hokieday import cache, config


class FakeHTTP:
    """Records every upstream call; optionally delays or raises."""

    def __init__(self, result=None, error: BaseException | None = None,
                 delay: float = 0.0):
        self.result = result
        self.error = error
        self.delay = delay
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, timeout: int) -> bytes:
        with self._lock:
            self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return json.dumps(self.result).encode("utf-8")

    @property
    def n(self) -> int:
        with self._lock:
            return len(self.calls)


class CacheConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self._orig_cache_only = config.CACHE_ONLY
        self._orig_cache_dir = config.CACHE_DIR
        self._orig_http = cache._http
        self._tmp = tempfile.mkdtemp(prefix="hokie-cache-test-")
        config.CACHE_ONLY = False
        config.CACHE_DIR = Path(self._tmp)
        cache._attempts.clear()
        cache._key_locks.clear()

    def tearDown(self):
        cache._http = self._orig_http
        config.CACHE_ONLY = self._orig_cache_only
        config.CACHE_DIR = self._orig_cache_dir
        cache._attempts.clear()
        cache._key_locks.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -------------------------------------------------------------- helpers
    def _write_stale(self, name, payload, params=None, age_s: int = 3600):
        p = cache._json_path(name, params)
        cache._write_envelope(p, "http://x", payload)
        env = json.loads(p.read_text(encoding="utf-8"))
        env["fetched_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=age_s)
        ).isoformat(timespec="seconds")
        p.write_text(json.dumps(env), encoding="utf-8")

    def _write_stale_bin(self, name, data: bytes, params=None, age_s: int = 3600):
        bp = cache._bin_path(name, params)
        cache._write_bytes_atomic(bp, data)
        env_p = cache._json_path(name, params)
        cache._write_envelope(env_p, "http://x", {"bytes": len(data), "file": bp.name})
        env = json.loads(env_p.read_text(encoding="utf-8"))
        env["fetched_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=age_s)
        ).isoformat(timespec="seconds")
        env_p.write_text(json.dumps(env), encoding="utf-8")

    def _run_threads(self, n: int, fn):
        barrier = threading.Barrier(n)
        results: list = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def wrap():
            barrier.wait()
            try:
                r = fn()
                with lock:
                    results.append(r)
            except BaseException as exc:          # noqa: BLE001 - test capture
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=wrap) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertTrue(all(not t.is_alive() for t in threads),
                        "a caller thread hung (deadlock?)")
        return results, errors

    # ------------------------------------------------- cold cache dedupe
    def test_cold_cache_concurrent_callers_make_one_upstream_fetch(self):
        http = FakeHTTP(result={"data": [1]}, delay=0.05)
        cache._http = http
        results, errors = self._run_threads(
            4, lambda: cache.get_json("bt_buses", "http://x", max_age_s=60))
        self.assertEqual(errors, [])
        self.assertEqual(http.n, 1,
                         "concurrent cold-cache callers must collapse to one fetch")
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r == {"data": [1]} for r in results))

    def test_cold_failure_concurrent_callers_make_one_upstream_fetch(self):
        http = FakeHTTP(error=OSError("upstream down"), delay=0.05)
        cache._http = http
        results, errors = self._run_threads(
            2, lambda: cache.get_json("bt_buses", "http://x", max_age_s=60))
        self.assertEqual(results, [])
        self.assertEqual(http.n, 1,
                         "a cold-cache failure must not be retried immediately")
        self.assertEqual(len(errors), 2)
        self.assertEqual({type(e) for e in errors},
                         {OSError, cache.CacheRefreshError})

    # ------------------------------------------- stale cache + failed refresh
    def test_stale_failed_refresh_two_callers_one_fetch_both_stale(self):
        self._write_stale("bt_buses", {"data": ["old"]})
        http = FakeHTTP(error=OSError("upstream down"), delay=0.05)
        cache._http = http
        results, errors = self._run_threads(
            2, lambda: cache.get_json("bt_buses", "http://x", max_age_s=60))
        self.assertEqual(errors, [])
        self.assertEqual(http.n, 1)
        self.assertEqual(results, [{"data": ["old"]}, {"data": ["old"]}])

    def test_immediate_retry_inside_cooldown_makes_no_upstream_call(self):
        self._write_stale("bt_buses", {"data": ["old"]})
        http = FakeHTTP(error=OSError("down"))
        cache._http = http
        first = cache.get_json("bt_buses", "http://x", max_age_s=60)
        self.assertEqual(first, {"data": ["old"]})
        self.assertEqual(http.n, 1)
        again = cache.get_json("bt_buses", "http://x", max_age_s=60)
        self.assertEqual(again, {"data": ["old"]})
        self.assertEqual(http.n, 1, "cooldown must suppress the immediate retry")

    def test_after_simulated_cooldown_exactly_one_retry(self):
        self._write_stale("bt_buses", {"data": ["old"]})
        http = FakeHTTP(error=OSError("down"))
        cache._http = http
        cache.get_json("bt_buses", "http://x", max_age_s=60)
        self.assertEqual(http.n, 1)
        # Simulate the politeness window elapsing without sleeping in the test.
        k = cache.key("bt_buses")
        ts, exc = cache._attempts[k]
        cache._attempts[k] = (ts - 120.0, exc)
        http.error = None
        http.result = {"data": ["new"]}
        res = cache.get_json("bt_buses", "http://x", max_age_s=60)
        self.assertEqual(http.n, 2, "after cooldown exactly one retry is allowed")
        self.assertEqual(res, {"data": ["new"]})

    def test_force_bypasses_cooldown_for_explicit_taps(self):
        self._write_stale("bt_buses", {"data": ["old"]})
        http = FakeHTTP(error=OSError("down"))
        cache._http = http
        cache.get_json("bt_buses", "http://x", max_age_s=60)
        self.assertEqual(http.n, 1)
        http.error = None
        http.result = {"data": ["new"]}
        res = cache.get_json("bt_buses", "http://x", max_age_s=60, force=True)
        self.assertEqual(http.n, 2, "force=True is an explicit sample; never suppressed")
        self.assertEqual(res, {"data": ["new"]})

    # ------------------------------------------------- temp-file safety
    def test_envelope_writes_are_temp_collision_safe(self):
        target = Path(self._tmp) / "same.json"
        errors: list[BaseException] = []
        lock = threading.Lock()

        def writer(i: int):
            try:
                cache._write_envelope(target, "http://x", {"i": i})
            except BaseException as exc:          # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [], "concurrent writers collided on a temp file")
        env = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn(env["payload"]["i"], range(16))
        leftovers = list(Path(self._tmp).glob("same.json.*.tmp"))
        self.assertEqual(leftovers, [], "no temp files may survive a successful write")

    def test_stale_temp_leftovers_are_cleaned_best_effort(self):
        target = Path(self._tmp) / "left.json"
        orphan = Path(self._tmp) / "left.json.dead.tmp"
        orphan.write_text("{}", encoding="utf-8")
        old = time.time() - 7200
        import os
        os.utime(orphan, (old, old))
        cache._write_envelope(target, "http://x", {"ok": True})
        self.assertFalse(orphan.exists(), "orphaned temp older than an hour should be removed")

    # ------------------------------------------------- isolation / per-key
    def test_distinct_keys_do_not_serialize_on_a_global_lock(self):
        a_entered = threading.Event()
        b_done = threading.Event()

        def http(url: str, timeout: int) -> bytes:
            if url == "http://a":
                a_entered.set()
                if not b_done.wait(timeout=5):
                    raise AssertionError("key A blocked key B (a global lock?)")
                return b'{"v": "a"}'
            b_done.set()
            return b'{"v": "b"}'

        cache._http = http
        out: dict = {}

        ta = threading.Thread(
            target=lambda: out.__setitem__(
                "a", cache.get_json("key_a", "http://a", max_age_s=60)))
        ta.start()
        self.assertTrue(a_entered.wait(timeout=5))
        tb = threading.Thread(
            target=lambda: out.__setitem__(
                "b", cache.get_json("key_b", "http://b", max_age_s=60)))
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)
        self.assertFalse(ta.is_alive())
        self.assertFalse(tb.is_alive())
        self.assertEqual(out, {"a": {"v": "a"}, "b": {"v": "b"}})

    def test_lock_bookkeeping_is_bounded(self):
        http = FakeHTTP(result={"ok": True})
        cache._http = http
        for i in range(50):
            cache.get_json(f"key_{i}", "http://x", max_age_s=60)
        self.assertEqual(cache._key_locks, {},
                         "per-key lock entries must be released when idle")

    # ------------------------------------------------- preserved behavior
    def test_short_and_hashed_keys_are_unchanged(self):
        self.assertEqual(
            cache.key("dining_nutrition", {"items": "214022*1*1,141002*2*1"}),
            "dining_nutrition__items=214022-1-1-141002-2-1")
        long_items = ",".join(f"{100000 + i}*1*1" for i in range(40))
        long_key = cache.key("dining_nutrition", {"items": long_items})
        self.assertIn("__h", long_key)
        self.assertLessEqual(len(long_key), 140)

    def test_cache_only_reads_only_and_never_calls_http(self):
        self._write_stale("bt_buses", {"data": ["frozen"]})
        config.CACHE_ONLY = True

        def boom(url, timeout):
            raise AssertionError("cache mode must never touch the network")

        cache._http = boom
        self.assertEqual(cache.get_json("bt_buses", "http://x"),
                         {"data": ["frozen"]})
        with self.assertRaises(cache.CacheMiss):
            cache.get_json("absent_key", "http://x")

    def test_put_json_and_has_still_work(self):
        cache.put_json("derived", "derived: x", {"v": 1})
        self.assertTrue(cache.has("derived"))
        self.assertEqual(cache.get_json("derived", "derived: x",
                                         max_age_s=60), {"v": 1})
        config.CACHE_ONLY = True
        cache.put_json("derived2", "derived: y", {"v": 2})
        self.assertFalse(cache.has("derived2"))

    def test_read_envelope_is_a_single_payload_plus_provenance_snapshot(self):
        self._write_stale("atomic", {"v": 7}, age_s=10)
        env = cache.read_envelope("atomic")
        self.assertEqual(env["payload"], {"v": 7})
        self.assertIn("fetched_at", env)
        self.assertEqual(cache.read_envelope("absent"), None)

    def test_get_json_with_metadata_returns_a_matched_pair(self):
        self._write_stale("atomic", {"v": 7}, age_s=10)
        payload, meta = cache.get_json_with_metadata(
            "atomic", "http://x", max_age_s=60)
        self.assertEqual(payload, {"v": 7})
        self.assertEqual(meta["key"], "atomic")
        self.assertIn("fetched_at", meta)

    def test_binary_cache_read_path_is_preserved(self):
        p = cache._bin_path("bt_gtfs")
        p.write_bytes(b"ZIPDATA")
        cache._write_envelope(cache._json_path("bt_gtfs"), "http://x",
                              {"bytes": 7})
        self.assertEqual(
            cache.get_bytes("bt_gtfs", "http://x", max_age_s=60), b"ZIPDATA")

    # ----------------------------------------- explicit fetch metadata
    def test_fetch_result_exposes_explicit_status_fresh_reused_stale(self):
        http = FakeHTTP(result={"v": 1})
        cache._http = http
        fresh = cache.get_json_result("meta", "http://x", max_age_s=60)
        self.assertEqual(fresh.status, cache.FETCH_FRESH)
        self.assertTrue(fresh.is_fresh)
        self.assertFalse(fresh.fetch_failed)
        self.assertIsNotNone(fresh.fetched_at)
        self.assertEqual(fresh.value, {"v": 1})
        # A second call inside the window is a deliberate reuse, not a fetch.
        reused = cache.get_json_result("meta", "http://x", max_age_s=60)
        self.assertEqual(reused.status, cache.FETCH_REUSED)
        self.assertFalse(reused.fetch_failed)
        self.assertEqual(http.n, 1)

    def test_recent_cache_plus_failed_refresh_is_stale_never_fresh(self):
        """Adversarial: a 1s-old cached copy must NOT read as fresh when the
        upstream refresh failed. Age-inference got this wrong."""
        self._write_stale("recent", {"old": True}, age_s=1)
        http = FakeHTTP(error=OSError("down"))
        cache._http = http
        res = cache.get_json_result("recent", "http://x", max_age_s=60, force=True)
        self.assertEqual(res.status, cache.FETCH_STALE)
        self.assertTrue(res.fetch_failed)
        self.assertFalse(res.is_fresh)
        self.assertEqual(res.value, {"old": True})
        self.assertIn("down", res.error)

    def test_binary_fetch_result_reports_stale_after_failed_refresh(self):
        self._write_stale_bin("meta_zip", b"OLD", age_s=1)
        http = FakeHTTP(error=OSError("down"))
        cache._http = http
        res = cache.get_bytes_result("meta_zip", "http://x", max_age_s=60, force=True)
        self.assertEqual(res.status, cache.FETCH_STALE)
        self.assertTrue(res.fetch_failed)
        self.assertEqual(res.value, b"OLD")
        self.assertIsNotNone(res.fetched_at,
                             "binary provenance comes from the sibling envelope")

    def test_binary_reuse_keeps_envelope_timestamp(self):
        cache._http = FakeHTTP(result=None)
        cache._http.result = None
        # FakeHTTP JSON-encodes results, so seed binary + metadata directly.
        p = cache._bin_path("reuse_zip")
        cache._write_bytes_atomic(p, b"ZIP")
        stamp = "2026-09-19T15:22:29+00:00"
        cache._write_envelope(cache._json_path("reuse_zip"), "http://x",
                              {"bytes": 3, "file": p.name}, fetched_at=stamp)
        res = cache.get_bytes_result("reuse_zip", "http://x", max_age_s=None)
        self.assertEqual(res.status, cache.FETCH_REUSED)
        self.assertEqual(res.value, b"ZIP")
        self.assertEqual(res.fetched_at, stamp)

    def test_redirect_final_url_is_revalidated(self):
        def good(u):
            return u.startswith("https://events.vt.edu/")

        orig = cache._http_final
        try:
            # Cold cache + disallowed redirect -> hard failure, no invented data.
            cache._http_final = lambda url, timeout: (b'{"v": 1}', "https://evil.com/x")
            with self.assertRaises(ValueError):
                cache.get_json_result("redir", "https://events.vt.edu/a",
                                      max_age_s=60, force=True,
                                      require_final_url=good)
            # With a cached copy, a disallowed redirect is a stale fallback.
            self._write_stale("redir2", {"old": True}, age_s=3600)
            res = cache.get_json_result("redir2", "https://events.vt.edu/a",
                                        max_age_s=60, force=True,
                                        require_final_url=good)
            self.assertEqual(res.status, cache.FETCH_STALE)
            self.assertIn("disallowed", res.error)
            # An allowlisted final URL is accepted as fresh.
            cache._http_final = lambda url, timeout: (b'{"v": 2}',
                                                      "https://events.vt.edu/final")
            ok = cache.get_json_result("redir3", "https://events.vt.edu/a",
                                       max_age_s=60, force=True,
                                       require_final_url=good)
            self.assertEqual(ok.status, cache.FETCH_FRESH)
            self.assertEqual(ok.final_url, "https://events.vt.edu/final")
        finally:
            cache._http_final = orig

    # ------------------------------------------------- binary concurrency
    def test_binary_cold_cache_concurrent_callers_make_one_upstream_fetch(self):
        http = FakeHTTP(result={"zip": [1]}, delay=0.05)
        cache._http = http
        expected = json.dumps({"zip": [1]}).encode("utf-8")
        results, errors = self._run_threads(
            4, lambda: cache.get_bytes("bt_gtfs", "http://x", max_age_s=60))
        self.assertEqual(errors, [])
        self.assertEqual(http.n, 1,
                         "concurrent cold-cache binary callers must collapse to one fetch")
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r == expected for r in results))

    def test_binary_cold_failure_concurrent_callers_make_one_upstream_fetch(self):
        http = FakeHTTP(error=OSError("upstream down"), delay=0.05)
        cache._http = http
        results, errors = self._run_threads(
            2, lambda: cache.get_bytes("bt_gtfs", "http://x", max_age_s=60))
        self.assertEqual(results, [])
        self.assertEqual(http.n, 1,
                         "a cold-cache binary failure must not be retried immediately")
        self.assertEqual(len(errors), 2)
        self.assertEqual({type(e) for e in errors},
                         {OSError, cache.CacheRefreshError})

    def test_binary_stale_failed_refresh_two_callers_one_fetch_both_stale(self):
        self._write_stale_bin("bt_gtfs", b"OLDZIP")
        http = FakeHTTP(error=OSError("upstream down"), delay=0.05)
        cache._http = http
        results, errors = self._run_threads(
            2, lambda: cache.get_bytes("bt_gtfs", "http://x", max_age_s=60))
        self.assertEqual(errors, [])
        self.assertEqual(http.n, 1)
        self.assertEqual(results, [b"OLDZIP", b"OLDZIP"])

    def test_binary_immediate_retry_inside_cooldown_makes_no_upstream_call(self):
        self._write_stale_bin("bt_gtfs", b"OLDZIP")
        http = FakeHTTP(error=OSError("down"))
        cache._http = http
        first = cache.get_bytes("bt_gtfs", "http://x", max_age_s=60)
        self.assertEqual(first, b"OLDZIP")
        self.assertEqual(http.n, 1)
        again = cache.get_bytes("bt_gtfs", "http://x", max_age_s=60)
        self.assertEqual(again, b"OLDZIP")
        self.assertEqual(http.n, 1, "cooldown must suppress the immediate binary retry")

    def test_binary_after_simulated_cooldown_exactly_one_retry(self):
        self._write_stale_bin("bt_gtfs", b"OLDZIP")
        http = FakeHTTP(error=OSError("down"))
        cache._http = http
        cache.get_bytes("bt_gtfs", "http://x", max_age_s=60)
        self.assertEqual(http.n, 1)
        # Simulate the politeness window elapsing without sleeping in the test.
        k = cache.key("bt_gtfs")
        ts, exc = cache._attempts[k]
        cache._attempts[k] = (ts - 120.0, exc)
        http.error = None
        http.result = {"zip": [2]}
        res = cache.get_bytes("bt_gtfs", "http://x", max_age_s=60)
        self.assertEqual(http.n, 2, "after cooldown exactly one retry is allowed")
        self.assertEqual(res, json.dumps({"zip": [2]}).encode("utf-8"))

    def test_binary_force_bypasses_cooldown_for_explicit_taps(self):
        self._write_stale_bin("bt_gtfs", b"OLDZIP")
        http = FakeHTTP(error=OSError("down"))
        cache._http = http
        cache.get_bytes("bt_gtfs", "http://x", max_age_s=60)
        self.assertEqual(http.n, 1)
        http.error = None
        http.result = {"zip": [3]}
        res = cache.get_bytes("bt_gtfs", "http://x", max_age_s=60, force=True)
        self.assertEqual(http.n, 2, "force=True is an explicit sample; never suppressed")
        self.assertEqual(res, json.dumps({"zip": [3]}).encode("utf-8"))

    def test_binary_writes_are_atomic_and_temp_collision_safe(self):
        target = Path(self._tmp) / "same.bin"
        errors: list[BaseException] = []
        lock = threading.Lock()

        def writer(i: int):
            try:
                cache._write_bytes_atomic(target, bytes([i]) * 4096)
            except BaseException as exc:          # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [], "concurrent binary writers collided on a temp file")
        data = target.read_bytes()
        self.assertEqual(len(data), 4096)
        self.assertEqual(len(set(data)), 1, "readers must never see partial bytes")
        self.assertIn(data[0], range(16))
        leftovers = list(Path(self._tmp).glob("same.bin.*.tmp"))
        self.assertEqual(leftovers, [], "no temp files may survive a successful write")

    def test_binary_reader_never_sees_partial_bytes(self):
        target = Path(self._tmp) / "atomic.bin"
        cache._write_bytes_atomic(target, b"A" * 8192)
        stop = threading.Event()
        bad: list[bytes] = []

        def wobble():
            i = 0
            while not stop.is_set():
                cache._write_bytes_atomic(
                    target, (b"A" if i % 2 == 0 else b"B") * 8192)
                i += 1

        def reader():
            while not stop.is_set():
                blob = target.read_bytes()
                if len(blob) != 8192 or len(set(blob)) != 1 or blob[0] not in b"AB":
                    bad.append(blob)
                    return

        w = threading.Thread(target=wobble)
        r = threading.Thread(target=reader)
        w.start()
        r.start()
        time.sleep(0.2)
        stop.set()
        w.join(timeout=10)
        r.join(timeout=10)
        self.assertFalse(w.is_alive())
        self.assertFalse(r.is_alive())
        self.assertEqual(bad, [], "a reader observed a partially published binary")

    def test_binary_cache_only_read_path_is_preserved(self):
        self._write_stale_bin("bt_gtfs", b"FROZENZIP")
        config.CACHE_ONLY = True

        def boom(url, timeout):
            raise AssertionError("cache mode must never touch the network")

        cache._http = boom
        self.assertEqual(cache.get_bytes("bt_gtfs", "http://x"), b"FROZENZIP")
        with self.assertRaises(cache.CacheMiss):
            cache.get_bytes("absent_gtfs", "http://x")

    # ------------------------------------------------- livebus seam
    def test_livebus_fetch_vehicles_collapses_concurrent_polls(self):
        """The regression's real caller: two live-map requests at once."""
        from hokieday import livebus

        http = FakeHTTP(result={"data": [{"id": "1"}]}, delay=0.05)
        cache._http = http
        results, errors = self._run_threads(
            2, lambda: livebus.fetch_vehicles())
        self.assertEqual(errors, [])
        self.assertEqual(http.n, 1, "two concurrent polls must be one upstream call")
        self.assertTrue(all(r == [{"id": "1"}] for r in results))
        # A little later the cached snapshot is still fresh under the 60s window.
        self.assertEqual(livebus.fetch_vehicles(), [{"id": "1"}])
        self.assertEqual(http.n, 1)


if __name__ == "__main__":
    unittest.main()