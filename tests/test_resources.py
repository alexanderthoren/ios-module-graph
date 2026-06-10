"""Tests for modgraph.resources: the per-folder bundle-resource inventory that
feeds the migration prompts.

Follows the exemplar (tests/test_graph.py): one behaviour per test, concrete
assertions, all file IO inside tempfile.TemporaryDirectory.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modgraph.resources import collect_resources


def _make_tree(base: Path) -> Path:
    """A toy project with every resource situation the walker must handle."""
    base = Path(base)
    (base / "Core").mkdir(parents=True)
    (base / "Core" / "View.xib").write_text("x", encoding="utf-8")
    (base / "Core" / "CoreService.swift").write_text("class C {}", encoding="utf-8")
    # An asset catalog is ONE resource; its contents must not be walked.
    icon = base / "Core" / "Assets.xcassets" / "AppIcon.appiconset"
    icon.mkdir(parents=True)
    (icon / "icon.png").write_bytes(b"\x89PNG")
    # A localization dir is ONE resource too.
    lproj = base / "Feature" / "en.lproj"
    lproj.mkdir(parents=True)
    (lproj / "Localizable.strings").write_text('"k"="v";', encoding="utf-8")
    (base / "Feature" / "Main.storyboard").write_text("<sb/>", encoding="utf-8")
    # Skipped dirs: tests (by default) and build output (always).
    (base / "Tests").mkdir()
    (base / "Tests" / "Fixture.xib").write_text("x", encoding="utf-8")
    (base / "build").mkdir()
    (base / "build" / "Generated.xib").write_text("x", encoding="utf-8")
    # A root-level resource lands in folder ".".
    (base / "Credits.strings").write_text('"a"="b";', encoding="utf-8")
    return base


class CollectResourcesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _make_tree(Path(self._tmp.name))
        self.res = collect_resources(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_loose_resource_files_collected_per_folder(self):
        self.assertEqual(self.res["Feature"], ["Main.storyboard", "en.lproj"])

    def test_asset_catalog_is_one_entry_and_not_descended(self):
        self.assertIn("Assets.xcassets", self.res["Core"])
        flat = [n for names in self.res.values() for n in names]
        self.assertNotIn("icon.png", flat)

    def test_lproj_is_one_entry_and_not_descended(self):
        self.assertIn("en.lproj", self.res["Feature"])
        flat = [n for names in self.res.values() for n in names]
        self.assertNotIn("Localizable.strings", flat)

    def test_swift_files_are_not_resources(self):
        self.assertNotIn("CoreService.swift", self.res["Core"])

    def test_root_level_resource_maps_to_dot(self):
        self.assertEqual(self.res["."], ["Credits.strings"])

    def test_test_dirs_skipped_by_default(self):
        self.assertNotIn("Tests", self.res)

    def test_test_dirs_included_on_request(self):
        res = collect_resources(self.root, include_tests=True)
        self.assertEqual(res["Tests"], ["Fixture.xib"])

    def test_build_dirs_always_skipped(self):
        self.assertNotIn("build", self.res)

    def test_resourceless_folders_absent(self):
        # Core has resources; a folder with only .swift must not appear at all.
        only_swift = Path(self._tmp.name) / "Plain"
        only_swift.mkdir()
        (only_swift / "P.swift").write_text("struct P {}", encoding="utf-8")
        res = collect_resources(self.root)
        self.assertNotIn("Plain", res)

    def test_names_sorted(self):
        self.assertEqual(self.res["Core"], sorted(self.res["Core"]))


if __name__ == "__main__":
    unittest.main()
