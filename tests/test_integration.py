"""Cross-module integration tests + invariant tripwires.

OWNER: integrator. These are the checks that no single worker could run, because
they only exist once the modules meet:

  * the replay clock is actually pinned, and every module agrees on "now"
  * the frozen store (fixtures/) is what cache mode reads, not the live cache/
  * no library code reaches for the raw wall clock (the drift bug)
  * the keystone realtime -> schedule join holds across module boundaries

All offline. DEMO_MODE=cache only.
"""
from __future__ import annotations

import ast
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hokieday import config

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "hokieday"

# Raw wall-clock calls are legitimate ONLY in these (file, function) pairs, each
# for a stated reason. Anything else is the replay-drift bug coming back.
ALLOWED_WALL_CLOCK = {
    ("config.py", "now"),               # the fallback inside the clock abstraction itself
    ("cache.py", "_now_iso"),           # provenance: when WE fetched
    ("cache.py", "age_seconds"),        # cache freshness is real-elapsed-time by definition
    ("livebus.py", "append_bronze"),    # provenance stamp on the ML training rows
}


def _wall_clock_callers(path: Path) -> set[tuple[str, str]]:
    """Find `datetime.now(...)` calls via AST and name the enclosing function.

    AST rather than grep, so a mention inside a docstring or comment does not
    count as a call (livebus.py's docstring mentions it legitimately).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()

    def scan(node, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                f = child.func
                if (isinstance(f, ast.Attribute) and f.attr == "now"
                        and isinstance(f.value, ast.Name) and f.value.id == "datetime"):
                    found.add((path.name, owner))
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scan(child, child.name)
            else:
                scan(child, owner)

    scan(tree, "<module>")
    return found


class TestReplayClock(unittest.TestCase):
    def test_clock_is_pinned_in_cache_mode(self):
        """cache mode must not use the wall clock, or deltas drift 1 min/min."""
        self.assertEqual(config.DEMO_MODE, "cache")
        self.assertTrue(config.CACHE_ONLY)
        drift = abs((datetime.now(timezone.utc) - config.now()).total_seconds())
        self.assertGreater(drift, 0, "expected the pinned clock to differ from the wall clock")

    def test_pin_comes_from_the_snapshot(self):
        pinned = config.now()
        envelope = __import__("json").loads(
            (config.CACHE_DIR / "bt_buses.json").read_text(encoding="utf-8"))
        self.assertEqual(pinned.isoformat(timespec="seconds"), envelope["fetched_at"])

    def test_dining_and_transit_share_one_clock(self):
        """The bug this guards: transit on the pinned clock, dining on the wall."""
        from hokieday import dining
        self.assertEqual(
            dining._now_campus(),
            config.now(ZoneInfo(config.CAMPUS_TZ)).replace(tzinfo=None),
        )

    def test_snapshot_day_is_a_saturday_and_hours_are_consistent(self):
        """The whole replay must describe one coherent moment."""
        pinned = config.now(ZoneInfo(config.CAMPUS_TZ))
        self.assertEqual(pinned.date(), date(2026, 9, 19))
        self.assertEqual(pinned.strftime("%A"), "Saturday")

        from hokieday import dining
        windows = dining.hours("15", pinned.date())
        self.assertTrue(windows, "D2 must have hours on the snapshot date")
        is_open, minutes = dining.is_open(windows, pinned.replace(tzinfo=None))
        self.assertTrue(is_open, f"D2 should be open at the pinned 11:22, got minutes={minutes}")
        self.assertGreater(minutes, 0)


class TestServerBootstrapOrder(unittest.TestCase):
    """The script entrypoint must set sys.path before importing repo modules.

    `python3 app/server.py` puts app/ (not the repo root) on sys.path[0], so an
    import of a repo-root module placed above the path setup fails at startup --
    and only in script mode, which is exactly how the deployed app and the local
    accounts setup run it. A direct `import app.server` during tests hides it.
    """

    def test_path_setup_precedes_repo_root_imports(self):
        source = (REPO / "app" / "server.py").read_text(encoding="utf-8")
        path_setup = source.index("sys.path.insert(0, str(REPO))")
        for mod in ("from auth import", "from hokieday import",
                    "from app.local_env import"):
            self.assertLess(
                path_setup, source.index(mod),
                f"{mod} must come after sys.path.insert(0, str(REPO))",
            )

    def test_dotenv_is_loaded_before_auth(self):
        """database.py reads SUPABASE_* at import, so .env must be loaded first."""
        source = (REPO / "app" / "server.py").read_text(encoding="utf-8")
        self.assertLess(source.index('load_local_env(REPO / ".env")'),
                        source.index("from auth import"))


class TestWallClockTripwire(unittest.TestCase):
    def test_no_unapproved_wall_clock_calls(self):
        offenders = set()
        for py in sorted(LIB.glob("*.py")):
            for pair in _wall_clock_callers(py):
                if pair not in ALLOWED_WALL_CLOCK:
                    offenders.add(pair)
        self.assertEqual(
            offenders, set(),
            f"raw datetime.now() outside the allowlist reintroduces replay drift: {sorted(offenders)}",
        )

    def test_every_allowlisted_pair_still_exists(self):
        """If a refactor moves these, the tripwire above must be revisited."""
        live = set()
        for py in sorted(LIB.glob("*.py")):
            live |= _wall_clock_callers(py)
        missing = ALLOWED_WALL_CLOCK - live
        self.assertEqual(missing, set(), f"allowlisted callers no longer exist: {sorted(missing)}")


class TestStoreSeparation(unittest.TestCase):
    def test_cache_mode_reads_the_frozen_store(self):
        self.assertEqual(config.CACHE_DIR, config.FIXTURES_DIR)

    def test_fixture_fingerprint_is_the_seeded_spike_capture(self):
        """Guards the bug where a live poll overwrote the fixture the tests read."""
        import json
        env = json.loads((config.CACHE_DIR / "bt_buses.json").read_text(encoding="utf-8"))
        self.assertEqual(env["fetched_at"], "2026-09-19T15:22:29+00:00")
        self.assertEqual(env.get("mode"), "spike")

    def test_live_cache_and_fixtures_are_distinct_dirs(self):
        self.assertNotEqual(REPO / "cache", REPO / "fixtures")


class TestKeystoneAcrossModules(unittest.TestCase):
    def test_all_live_vehicles_join_to_active_service(self):
        """The cross-module guarantee: livebus + gtfs together, 13/13."""
        from hokieday import gtfs as G, livebus as L
        g = G.load_gtfs()
        rows = L.live()
        self.assertEqual(len(rows), 13)
        self.assertTrue(all(r.sched_delta_min is not None for r in rows),
                        "an unmatched vehicle means the gtfs/livebus seam broke again")
        active = G.service_ids_for_date(g, date(2026, 9, 19))
        self.assertEqual(len(active), 2)
        for r in rows:
            svc = g.trips[r.gtfs_trip_id]["service_id"]
            self.assertIn(svc, active, f"bus {r.bus_id} runs on a service not active on 9/19")

    def test_deltas_are_centred_near_zero_when_replayed(self):
        """Sanity: buses sit near their scheduled time in the snapshot.
        Unpinned drift pushed the mean to +7.73; pinned it is ~+0.96."""
        from hokieday import livebus as L
        ds = [r.sched_delta_min for r in L.live() if r.sched_delta_min is not None]
        self.assertLess(abs(sum(ds) / len(ds)), 5.0, f"mean delta {sum(ds)/len(ds):.2f} looks drifted")

    def test_departures_are_in_the_future_of_the_pinned_clock(self):
        from hokieday import gtfs as G
        g = G.load_gtfs()
        at = config.now(ZoneInfo(config.CAMPUS_TZ))
        deps = G.next_departures(g, "1600", at, limit=4)
        self.assertEqual(len(deps), 4)
        self.assertTrue(all(d.in_min > 0 for d in deps))
        self.assertEqual([d.dep_time.strftime("%H:%M:%S") for d in deps],
                         ["11:48:29", "11:50:21", "12:18:29", "12:20:21"])


if __name__ == "__main__":
    unittest.main()