"""Tests for modgraph.spm: SPM package labelling, folder→package mapping, and
auto-detection of already-migrated prefixes.

Follows the exemplar (tests/test_graph.py): one behaviour per method, concrete
assertions, clear names. Filesystem tests use tempfile.TemporaryDirectory and
never touch the repo root.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modgraph import spm


class PackageLabelTest(unittest.TestCase):
    def test_modules_foo_sources_drops_trailing_sources(self):
        # `.../Sources` collapses to the parent segment for a prettier label.
        self.assertEqual(spm._package_label("Modules/Foo/Sources"), "Foo")

    def test_bare_sources_kept_when_only_segment(self):
        # A lone "Sources" has no parent to fall back to, so it stays.
        self.assertEqual(spm._package_label("Sources"), "Sources")

    def test_dot_returns_dot(self):
        # "." filters to no parts -> returns the prefix (or ".").
        self.assertEqual(spm._package_label("."), ".")

    def test_empty_string_returns_dot(self):
        # No parts and a falsy prefix -> the "." fallback.
        self.assertEqual(spm._package_label(""), ".")

    def test_deeper_than_sources_returns_last_segment_not_package(self):
        # NOTE: the docstring claims "Modules/Foo/Sources/Bar" keeps "Foo", but
        # the code only special-cases a *trailing* "Sources". Here the last
        # segment is "Bar" (!= "Sources"), so it returns "Bar". Documenting the
        # ACTUAL behaviour; the docstring is wrong.
        self.assertEqual(spm._package_label("Modules/Foo/Sources/Bar"), "Bar")

    def test_trailing_slash_stripped_segments(self):
        # Trailing slash produces an empty part that is filtered out.
        self.assertEqual(spm._package_label("Modules/Foo/Sources/"), "Foo")

    def test_single_segment_returned(self):
        self.assertEqual(spm._package_label("Foo"), "Foo")


class BuildPackageMapTest(unittest.TestCase):
    def test_folders_under_prefix_get_prefix_id_others_app(self):
        folders = {"App", "Modules/Foo/Sources", "Modules/Foo/Sources/Sub"}
        prefixes = ["Modules/Foo/Sources"]
        folder_pkg, _ = spm._build_package_map(folders, prefixes)
        self.assertEqual(folder_pkg["Modules/Foo/Sources"], "Modules/Foo/Sources")
        self.assertEqual(folder_pkg["Modules/Foo/Sources/Sub"], "Modules/Foo/Sources")
        # A folder outside every prefix lands in the app bucket.
        self.assertEqual(folder_pkg["App"], "app")

    def test_folder_pkg_key_order_is_sorted(self):
        # Regression guard: keys must be deterministic (sorted), not set order.
        folders = {"Util", "App", "Core", "Feature"}
        folder_pkg, _ = spm._build_package_map(folders, [])
        self.assertEqual(list(folder_pkg), ["App", "Core", "Feature", "Util"])

    def test_no_prefixes_makes_everything_app(self):
        folders = {"App", "Core"}
        folder_pkg, packages = spm._build_package_map(folders, [])
        self.assertEqual(set(folder_pkg.values()), {"app"})
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["id"], "app")

    def test_app_package_metadata(self):
        folder_pkg, packages = spm._build_package_map({"B", "A"}, [])
        app = packages[0]
        self.assertEqual(app["id"], "app")
        self.assertEqual(app["label"], "App (xcodeproj)")
        self.assertEqual(app["prefix"], "")
        self.assertEqual(app["kind"], "app")
        # Folders within a package are sorted.
        self.assertEqual(app["folders"], ["A", "B"])

    def test_spm_package_metadata_uses_package_label(self):
        folders = {"Modules/Foo/Sources", "Modules/Foo/Sources/Inner"}
        prefixes = ["Modules/Foo/Sources"]
        _, packages = spm._build_package_map(folders, prefixes)
        spm_pkgs = [p for p in packages if p["kind"] == "spm"]
        self.assertEqual(len(spm_pkgs), 1)
        pkg = spm_pkgs[0]
        self.assertEqual(pkg["id"], "Modules/Foo/Sources")
        self.assertEqual(pkg["prefix"], "Modules/Foo/Sources")
        self.assertEqual(pkg["label"], "Foo")  # via _package_label
        self.assertEqual(pkg["folders"],
                         ["Modules/Foo/Sources", "Modules/Foo/Sources/Inner"])

    def test_packages_app_first_then_alpha(self):
        # Two SPM packages ("Alpha", "Zeta") plus an app bucket: app leads, then
        # the spm packages sort alphabetically by (case-insensitive) label.
        folders = {
            "App",
            "Modules/Zeta/Sources",
            "Modules/Alpha/Sources",
        }
        prefixes = ["Modules/Zeta/Sources", "Modules/Alpha/Sources"]
        _, packages = spm._build_package_map(folders, prefixes)
        self.assertEqual([p["label"] for p in packages],
                         ["App (xcodeproj)", "Alpha", "Zeta"])

    def test_longest_prefix_wins_for_nested_packages(self):
        # A folder under two nested prefixes binds to the longer (more specific).
        folders = {"Modules/Foo/Sources/Bar"}
        prefixes = ["Modules/Foo/Sources", "Modules/Foo/Sources/Bar"]
        folder_pkg, _ = spm._build_package_map(folders, prefixes)
        self.assertEqual(folder_pkg["Modules/Foo/Sources/Bar"],
                         "Modules/Foo/Sources/Bar")

    def test_prefix_match_tolerates_trailing_slash(self):
        # Prefixes carrying a trailing slash are rstrip'd before matching, but
        # the package id keeps the original (slashed) string.
        folders = {"Pkg/Sources", "Pkg/Sources/Deep"}
        prefixes = ["Pkg/Sources/"]
        folder_pkg, packages = spm._build_package_map(folders, prefixes)
        self.assertEqual(folder_pkg["Pkg/Sources"], "Pkg/Sources/")
        self.assertEqual(folder_pkg["Pkg/Sources/Deep"], "Pkg/Sources/")
        spm_pkg = [p for p in packages if p["kind"] == "spm"][0]
        self.assertEqual(spm_pkg["id"], "Pkg/Sources/")

    def test_partial_name_prefix_does_not_falsely_match(self):
        # "Foobar" must NOT be captured by prefix "Foo" — match needs a "/"
        # boundary (or exact equality).
        folders = {"Foobar"}
        prefixes = ["Foo"]
        folder_pkg, _ = spm._build_package_map(folders, prefixes)
        self.assertEqual(folder_pkg["Foobar"], "app")


class AutoDetectMigratedPrefixesTest(unittest.TestCase):
    def test_nested_package_with_sources_yields_sources_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "Modules" / "Foo"
            (pkg / "Sources").mkdir(parents=True)
            (pkg / "Package.swift").write_text("// pkg\n", encoding="utf-8")
            prefixes = spm.auto_detect_migrated_prefixes(root)
            self.assertEqual(prefixes, ["Modules/Foo/Sources"])

    def test_package_without_sources_yields_package_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "Modules" / "Bar"
            pkg.mkdir(parents=True)
            (pkg / "Package.swift").write_text("// pkg\n", encoding="utf-8")
            prefixes = spm.auto_detect_migrated_prefixes(root)
            self.assertEqual(prefixes, ["Modules/Bar"])

    def test_root_package_with_sources_yields_bare_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Sources").mkdir()
            (root / "Package.swift").write_text("// pkg\n", encoding="utf-8")
            prefixes = spm.auto_detect_migrated_prefixes(root)
            self.assertEqual(prefixes, ["Sources"])

    def test_root_package_without_sources_yields_dot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Package.swift").write_text("// pkg\n", encoding="utf-8")
            prefixes = spm.auto_detect_migrated_prefixes(root)
            self.assertEqual(prefixes, ["."])

    def test_prunes_deeper_once_package_found(self):
        # A package found at Modules/Foo must NOT yield a second prefix for a
        # nested Package.swift inside it (the subtree belongs to that one pkg).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "Modules" / "Foo"
            (pkg / "Sources").mkdir(parents=True)
            (pkg / "Package.swift").write_text("// pkg\n", encoding="utf-8")
            nested = pkg / "Sources" / "Inner"
            nested.mkdir(parents=True)
            (nested / "Package.swift").write_text("// inner\n", encoding="utf-8")
            prefixes = spm.auto_detect_migrated_prefixes(root)
            self.assertEqual(prefixes, ["Modules/Foo/Sources"])

    def test_skips_build_and_checkout_dirs(self):
        # Package.swift buried in build/ or checkouts/ must be ignored.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for skip in ("build", "checkouts"):
                d = root / skip / "Vendored"
                (d / "Sources").mkdir(parents=True)
                (d / "Package.swift").write_text("// vendored\n", encoding="utf-8")
            prefixes = spm.auto_detect_migrated_prefixes(root)
            self.assertEqual(prefixes, [])

    def test_skips_dot_prefixed_dirs(self):
        # .build (and any dot dir) is skipped by should_skip_dir.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / ".build" / "Vendored"
            (d / "Sources").mkdir(parents=True)
            (d / "Package.swift").write_text("// vendored\n", encoding="utf-8")
            prefixes = spm.auto_detect_migrated_prefixes(root)
            self.assertEqual(prefixes, [])

    def test_multiple_sibling_packages_all_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("Alpha", "Beta"):
                pkg = root / "Modules" / name
                (pkg / "Sources").mkdir(parents=True)
                (pkg / "Package.swift").write_text("// pkg\n", encoding="utf-8")
            prefixes = spm.auto_detect_migrated_prefixes(root)
            self.assertEqual(sorted(prefixes),
                             ["Modules/Alpha/Sources", "Modules/Beta/Sources"])

    def test_empty_tree_yields_no_prefixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefixes = spm.auto_detect_migrated_prefixes(Path(tmp))
            self.assertEqual(prefixes, [])


class IsMigratedTest(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(spm.is_migrated("Modules/Foo/Sources",
                                        ["Modules/Foo/Sources"]))

    def test_subfolder_under_prefix(self):
        self.assertTrue(spm.is_migrated("Modules/Foo/Sources/Bar",
                                        ["Modules/Foo/Sources"]))

    def test_trailing_slash_prefix_matches(self):
        # The prefix is rstrip'd of "/" before comparison.
        self.assertTrue(spm.is_migrated("Modules/Foo/Sources",
                                        ["Modules/Foo/Sources/"]))
        self.assertTrue(spm.is_migrated("Modules/Foo/Sources/Bar",
                                        ["Modules/Foo/Sources/"]))

    def test_unrelated_folder_not_migrated(self):
        self.assertFalse(spm.is_migrated("App", ["Modules/Foo/Sources"]))

    def test_partial_name_is_not_a_match(self):
        # "Foobar" shares a prefix string with "Foo" but is not under it.
        self.assertFalse(spm.is_migrated("Foobar", ["Foo"]))

    def test_no_prefixes_never_migrated(self):
        self.assertFalse(spm.is_migrated("anything", []))

    def test_matches_any_of_several_prefixes(self):
        prefixes = ["A/Sources", "B/Sources"]
        self.assertTrue(spm.is_migrated("B/Sources/X", prefixes))


if __name__ == "__main__":
    unittest.main()
