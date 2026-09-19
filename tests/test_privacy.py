"""Privacy regressions for the dynamic-place registry.

Two separate guarantees are covered here:

1. A device position is session-scoped. Its lookup key embeds the rounded
   coordinates of wherever the student actually is, so it must NEVER be echoed
   back in an error payload (``infeasible_reason.known_places`` or a walk_time
   error string). Those lists expose STATIC campus places only.

2. The registry is bounded and safe under concurrent registration. The demo
   server is threaded, so the check/insert/evict sequence runs under a lock.

Offline. DEMO_MODE=cache only -- no network, stdlib unittest only.
"""
from __future__ import annotations

import sys
import threading
import unittest

from app import server
from hokieday import config, tools

BURRUSS = (37.22957, -80.41394)
DEVICE_A = (37.22990, -80.41420)
DEVICE_B = (37.22550, -80.41640)


class _RegistryIsolated(unittest.TestCase):
    """Snapshot/restore the module-level registry so tests stay independent."""

    def setUp(self):
        self._saved_places = dict(config.PLACES)
        self._saved_order = list(config._DYNAMIC_ORDER)

    def tearDown(self):
        config.PLACES.clear()
        config.PLACES.update(self._saved_places)
        config._DYNAMIC_ORDER[:] = self._saved_order


class TestKnownPlacesPrivacy(_RegistryIsolated):
    def test_unknown_place_payload_hides_device_coordinates(self):
        device_key = config.register_dynamic_place(*DEVICE_A, "your location", 7.0)
        self.assertTrue(config.is_dynamic(device_key))

        out = tools.plan_day("demo-student-1", "11:22", "13:00",
                             {"from_place": "Atlantis", "eat": False})
        self.assertFalse(out["feasible"])
        reason = out["infeasible_reason"]
        self.assertEqual(reason["code"], "unknown_place")

        known = reason["known_places"]
        self.assertIn("Burruss Hall", known, "static options must remain")
        self.assertNotIn(device_key, known, "another session's device key leaked")
        for key in known:
            self.assertFalse(config.is_dynamic(key),
                             f"dynamic place {key!r} leaked into known_places")

        # The rationale string must not carry the rounded coordinates either.
        self.assertNotIn("37.22990", out["rationale"])
        self.assertNotIn("37.22550", out["rationale"])

    def test_no_legs_payload_hides_device_coordinates(self):
        device_key = config.register_dynamic_place(*DEVICE_B, "right outside D2", 9.0)

        # from == to with the eat leg disabled produces a usable-but-empty plan,
        # i.e. the no_legs branch.
        out = tools.plan_day("demo-student-1", "11:22", "13:00", {
            "from_place": "Burruss Hall", "to_place": "Burruss Hall",
            "eat": False,
        })
        self.assertFalse(out["feasible"])
        reason = out["infeasible_reason"]
        self.assertEqual(reason["code"], "no_legs")

        known = reason["known_places"]
        self.assertIn("Burruss Hall", known)
        self.assertNotIn(device_key, known, "another session's device key leaked")
        for key in known:
            self.assertFalse(config.is_dynamic(key),
                             f"dynamic place {key!r} leaked into known_places")

    def test_walk_time_error_hides_device_coordinates(self):
        device_key = config.register_dynamic_place(*DEVICE_A, "your location", 7.0)
        out = tools.walk_time("Atlantis", "McBryde Hall")
        self.assertIn("error", out)
        self.assertIn("Burruss Hall", out["error"])
        self.assertNotIn(device_key, out["error"])
        self.assertNotIn("37.22990", out["error"])

    def test_static_place_helper_excludes_dynamic(self):
        device_key = config.register_dynamic_place(*DEVICE_A, "your location", 7.0)
        static = config.static_place_keys()
        self.assertIn("Burruss Hall", static)
        self.assertNotIn(device_key, static)
        self.assertEqual(static, sorted(static))


class TestDynamicPlaceConcurrency(_RegistryIsolated):
    def test_concurrent_distinct_positions_are_bounded_and_consistent(self):
        n_threads, per_thread = 16, 20
        barrier = threading.Barrier(n_threads)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(t: int) -> None:
            try:
                barrier.wait(timeout=10)
                for i in range(per_thread):
                    config.register_dynamic_place(
                        37.0 + t * 0.001 + i * 0.00001, -80.4, f"worker-{t}")
            except BaseException as exc:                     # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=20)

        self.assertEqual(errors, [])
        dynamic = [k for k, v in config.PLACES.items() if v.get("dynamic")]
        self.assertLessEqual(len(dynamic), config.MAX_DYNAMIC_PLACES)
        self.assertLessEqual(len(config._DYNAMIC_ORDER), config.MAX_DYNAMIC_PLACES)
        # Eviction must not leave a dangling order entry pointing at a place
        # that was already removed.
        for key in config._DYNAMIC_ORDER:
            self.assertIn(key, config.PLACES)

    def test_concurrent_same_position_shares_one_entry(self):
        n_threads = 12
        barrier = threading.Barrier(n_threads)
        keys: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=10)
            key = config.register_dynamic_place(*DEVICE_A, "your location", 5.0)
            with lock:
                keys.append(key)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=20)

        self.assertEqual(len(keys), n_threads)
        self.assertEqual(len(set(keys)), 1, "stable coordinate key must dedupe")
        dynamic = [k for k, v in config.PLACES.items() if v.get("dynamic")]
        self.assertIn(keys[0], dynamic)
        self.assertEqual(dynamic.count(keys[0]), 1)
        self.assertEqual(config._DYNAMIC_ORDER.count(keys[0]), 1)

    def test_accuracy_update_keeps_one_entry(self):
        first = config.register_dynamic_place(*DEVICE_A, "your location", 5.0)
        second = config.register_dynamic_place(*DEVICE_A, "your location", 25.0)
        self.assertEqual(first, second)
        self.assertEqual(config.PLACES[first]["accuracy_m"], 25.0)
        self.assertEqual(config._DYNAMIC_ORDER.count(first), 1)


class TestDynamicPlaceLockedReads(_RegistryIsolated):
    """The read side of the registry must not observe a half-updated dict."""

    def test_read_helpers_serialize_on_the_dynamic_lock(self):
        # Deterministic proof that every runtime read path shares the writer's
        # lock: with the lock held, each helper must block instead of iterating
        # the registry concurrently. This is what prevents the RuntimeError.
        probes = (
            ("static_place_keys", lambda: config.static_place_keys()),
            ("static_places", lambda: config.static_places()),
            ("place_snapshot", lambda: config.place_snapshot()),
            ("find_place", lambda: config.find_place("Burruss Hall")),
            ("lookup_place", lambda: config.lookup_place("Burruss Hall")),
            ("is_dynamic", lambda: config.is_dynamic("Burruss Hall")),
        )
        for name, fn in probes:
            with self.subTest(helper=name):
                done = threading.Event()

                def run():
                    fn()
                    done.set()

                config._DYNAMIC_LOCK.acquire()
                try:
                    th = threading.Thread(target=run)
                    th.start()
                    self.assertFalse(done.wait(timeout=0.2),
                                     f"{name} did not block on the dynamic lock")
                finally:
                    config._DYNAMIC_LOCK.release()
                th.join(timeout=5)
                self.assertTrue(done.is_set(), f"{name} never completed")

    def test_static_snapshot_excludes_dynamic_rows(self):
        device_key = config.register_dynamic_place(*DEVICE_A, "your location", 5.0)
        static = config.static_places()
        self.assertTrue(static, "static campus places must still be listed")
        static_keys = [k for k, _ in static]
        self.assertIn("Burruss Hall", static_keys)
        self.assertNotIn(device_key, static_keys)
        for key, row in static:
            self.assertFalse(row.get("dynamic"), f"{key!r} leaked a device row")

    def test_lookup_and_snapshot_rows_are_copies(self):
        device_key = config.register_dynamic_place(*DEVICE_A, "your location", 5.0)
        row = config.lookup_place(device_key)
        self.assertIsNotNone(row)
        self.assertIsNot(row, config.PLACES[device_key])
        row["lat"] = 0.0
        self.assertEqual(config.PLACES[device_key]["lat"], DEVICE_A[0],
                         "mutating a returned row must not corrupt the registry")
        snap = config.place_snapshot()
        self.assertIsNot(snap[device_key], config.PLACES[device_key])

    def test_find_place_matches_case_insensitively_without_sharing(self):
        found = config.find_place("burruss hall")
        self.assertIsNotNone(found)
        self.assertIsNot(found, config.PLACES["Burruss Hall"])
        self.assertIsNone(config.find_place("Atlantis"))
        self.assertIsNone(config.find_place(None))


class TestDynamicPlaceConcurrentReaders(_RegistryIsolated):
    """Registering while readers run must never raise or leak device coords.

    The demo server is threaded, so a reader iterating ``config.PLACES``
    unlocked can hit ``RuntimeError: dictionary changed size during
    iteration`` while the bounded eviction pops entries. The runtime read
    paths must all go through the locked snapshot/lookup helpers, which this
    test hammers concurrently with writers.
    """

    def test_register_while_readers_run_never_raises_or_leaks(self):
        n_writers, n_readers = 2, 6
        barrier = threading.Barrier(n_writers + n_readers)
        stop = threading.Event()
        errors: list[BaseException] = []
        leaks: list[str] = []
        lock = threading.Lock()

        def writer(t: int) -> None:
            try:
                barrier.wait(timeout=10)
                i = 0
                while not stop.is_set():
                    # Monotonic, unique coordinates so every registration is a
                    # real insert/evict (not a dedupe), keeping the registry
                    # mutating for the whole read window.
                    config.register_dynamic_place(
                        37.0 + (i % 20000) * 0.000001, -80.4 - t * 0.001,
                        f"writer-{t}")
                    i += 1
            except BaseException as exc:                     # noqa: BLE001
                with lock:
                    errors.append(exc)

        def reader() -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(300):
                    # Static key list must never carry a dynamic (device) key.
                    for key in config.static_place_keys():
                        if config.is_dynamic(key):
                            with lock:
                                leaks.append(key)
                    # Runtime read paths that used to iterate PLACES unlocked.
                    self.assertIsNotNone(tools._place("Burruss Hall"))
                    self.assertIsNotNone(config.find_place("Burruss Hall"))
                    server._extract_explicit_places(
                        "from burruss to narnia by 1:25 PM",
                        "from burruss to narnia by 1:25 pm")
                    server.resolve_origin({"from_place": "Burruss Hall"})
                    walk = tools._walk_result("Atlantis", "Burruss Hall")
                    self.assertNotIn("(", walk["error"],
                                     "device coordinates leaked into a walk error")
                    for key, row in config.place_snapshot().items():
                        if row.get("dynamic") and "(" not in key:
                            with lock:
                                leaks.append(key)
            except BaseException as exc:                         # noqa: BLE001
                with lock:
                    errors.append(exc)

        writers = [threading.Thread(target=writer, args=(t,))
                   for t in range(n_writers)]
        readers = [threading.Thread(target=reader) for _ in range(n_readers)]
        # A tiny GIL switch interval makes the pre-fix unlocked-read race
        # reproduce reliably (verified: the old code tripped RuntimeError
        # here), so this test genuinely guards the regression rather than
        # passing by luck. Restored below.
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for th in writers + readers:
                th.start()
            for th in readers:
                th.join(timeout=60)
            stop.set()
            for th in writers:
                th.join(timeout=30)
        finally:
            stop.set()
            sys.setswitchinterval(old_interval)

        self.assertEqual(errors, [])
        self.assertEqual(leaks, [])
        dynamic = [k for k, v in config.PLACES.items() if v.get("dynamic")]
        self.assertLessEqual(len(dynamic), config.MAX_DYNAMIC_PLACES)
        for key in config._DYNAMIC_ORDER:
            self.assertIn(key, config.PLACES)


if __name__ == "__main__":
    unittest.main()