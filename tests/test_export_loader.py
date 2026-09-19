"""Unit checks for the gold exporter -> Databricks loader handoff.

These do NOT touch Databricks: they import the local exporter/loader modules and
verify that the new multi-location sections are produced, counted, commented and
wired through both the script loader and the notebook loader.

Offline, DEMO_MODE=cache.
"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DEMO_MODE", "cache")   # must precede hokieday imports

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hokieday import config  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestGoldBundleSections(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not config.CACHE_ONLY:
            raise RuntimeError("requires DEMO_MODE=cache")
        cls.export_gold = _load("hokieday_export_gold",
                                REPO / "scripts" / "export_gold.py")
        cls.bundle = cls.export_gold.build_bundle()

    def test_new_sections_exist_and_counts_match_stats(self):
        for name in ("dining_locations", "dining_status", "food_basic"):
            with self.subTest(section=name):
                self.assertIn(name, self.bundle)
                self.assertEqual(self.bundle["_stats"][name],
                                 len(self.bundle[name]))

    def test_directory_is_all_twelve_locations(self):
        rows = self.bundle["dining_locations"]
        self.assertEqual(len(rows), 12)
        self.assertEqual(len({r["location_num"] for r in rows}), 12)
        self.assertEqual({r["location_num"] for r in rows},
                         set(config.DINING_LOCATIONS))

    def test_status_rows_expose_independent_substates(self):
        rows = self.bundle["dining_status"]
        self.assertEqual(len(rows), 12)
        for row in rows:
            self.assertIn(row["status"],
                          {"ok", "closed", "empty", "unavailable"})
            self.assertIn("menu_status", row)
            self.assertIn("hours_status", row)

    def test_basic_rows_carry_provenance(self):
        rows = self.bundle["food_basic"]
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("fetched_at", row)
            self.assertIn("source", row)
            self.assertIn("allergens_known", row)
            self.assertIn("venue_allergen_free", row)

    def test_venue_exception_is_d2_only(self):
        for row in self.bundle["food_basic"]:
            if row["venue_allergen_free"]:
                self.assertEqual(row["location_num"], "15")
                self.assertTrue(row["section"].lower().startswith("viridian"))


class TestDatabricksLoaderWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loader = _load("hokieday_load_to_databricks",
                           REPO / "scripts" / "load_to_databricks.py")

    def test_new_tables_are_in_the_load_list(self):
        for name in ("dining_locations", "dining_status", "food_basic"):
            self.assertIn(name, self.loader.TABLES)

    def test_every_table_has_a_comment(self):
        bundle = {"_meta": {"service_date": "2026-09-19"}}
        for name in self.loader.TABLES:
            with self.subTest(table=name):
                comment = self.loader.table_comment(name, bundle)
                self.assertIsInstance(json.loads(comment), str)

    def test_loader_maps_new_stats_keys(self):
        src = (REPO / "scripts" / "load_to_databricks.py").read_text(
            encoding="utf-8")
        for name in ("dining_locations", "dining_status", "food_basic"):
            self.assertIn(f'stats.get("{name}")', src)
            self.assertIn(name, src)

    def test_notebook_wires_new_tables(self):
        text = (REPO / "notebooks" / "01_load_gold_bundle.py").read_text(
            encoding="utf-8")
        for name in ("dining_locations", "dining_status", "food_basic"):
            self.assertIn(f'"{name}"', text)


if __name__ == "__main__":
    unittest.main()