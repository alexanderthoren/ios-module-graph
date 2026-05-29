"""Tests for modgraph.scanner: the regex-scanner fallback graph builder.

Follows the style of tests/test_graph.py — one behaviour per method, concrete
assertions, inputs built from tests.fixtures (the on-disk toy Swift tree) where
the shared graph helps. All filesystem work happens inside a TemporaryDirectory.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modgraph import scanner
from modgraph.models import GraphData
from tests import fixtures


class StripNoiseTest(unittest.TestCase):
    def test_removes_line_comments(self):
        src = "class Foo {} // a CoreService comment\n"
        out = scanner.strip_noise(src)
        # The trailing // comment (and the CoreService token inside it) is gone.
        self.assertNotIn("//", out)
        self.assertNotIn("CoreService", out)
        self.assertIn("class Foo {}", out)

    def test_removes_block_comments_including_multiline(self):
        src = "before /* a\n multiline FeatureView block */ after\n"
        out = scanner.strip_noise(src)
        self.assertNotIn("FeatureView", out)
        self.assertNotIn("/*", out)
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_blanks_string_literals_to_empty_pair(self):
        # String contents are removed but a literal "" is left in their place.
        src = 'let s = "FeatureView string literal"\n'
        out = scanner.strip_noise(src)
        self.assertNotIn("FeatureView", out)
        self.assertIn('""', out)

    def test_keeps_real_code_tokens(self):
        src = 'let x = "noise" // comment\npublic class CoreService {}\n'
        out = scanner.strip_noise(src)
        self.assertIn("CoreService", out)
        self.assertIn("public class", out)


class ShouldSkipDirTest(unittest.TestCase):
    def test_dot_prefixed_dirs_skipped(self):
        self.assertTrue(scanner.should_skip_dir(Path("/x/.git"), ".git", False, []))
        self.assertTrue(scanner.should_skip_dir(Path("/x/.build"), ".build", False, []))

    def test_default_skip_names_skipped(self):
        for name in ("build", "Pods", "node_modules", "DerivedData", "checkouts"):
            self.assertTrue(
                scanner.should_skip_dir(Path("/x/" + name), name, False, []),
                f"{name} should be skipped",
            )

    def test_test_dirs_skipped_when_tests_excluded(self):
        self.assertTrue(scanner.should_skip_dir(Path("/x/Tests"), "Tests", False, []))
        self.assertTrue(scanner.should_skip_dir(Path("/x/UITests"), "UITests", False, []))

    def test_test_dirs_kept_when_include_tests(self):
        self.assertFalse(scanner.should_skip_dir(Path("/x/Tests"), "Tests", True, []))
        self.assertFalse(scanner.should_skip_dir(Path("/x/UITests"), "UITests", True, []))

    def test_ext_skips_match_dir_suffix(self):
        self.assertTrue(
            scanner.should_skip_dir(Path("/x/My.xcodeproj"), "My.xcodeproj", False, [])
        )
        self.assertTrue(
            scanner.should_skip_dir(Path("/x/W.xcworkspace"), "W.xcworkspace", False, [])
        )

    def test_ignore_glob_matches_basename(self):
        # Pattern matched against the basename even when the relpath differs.
        self.assertTrue(
            scanner.should_skip_dir(Path("/x/Gen"), "sub/Gen", False, ["Gen"])
        )

    def test_ignore_glob_matches_relpath(self):
        # Pattern matched against the relative path, not just the basename.
        self.assertTrue(
            scanner.should_skip_dir(Path("/x/a/b"), "a/b", False, ["a/*"])
        )

    def test_ordinary_source_dir_not_skipped(self):
        self.assertFalse(scanner.should_skip_dir(Path("/x/Core"), "Core", False, []))


class CollectSwiftFilesTest(unittest.TestCase):
    def test_finds_source_swift_skips_build_and_tests(self):
        with tempfile.TemporaryDirectory() as d:
            base = fixtures.make_swift_tree(Path(d))
            files = scanner.collect_swift_files(base, False, [], ".swift")
            names = sorted(f.name for f in files)
            # Core, Feature and the root-level file; Tests/ and build/ skipped.
            self.assertEqual(
                names, ["CoreService.swift", "FeatureView.swift", "RootType.swift"]
            )

    def test_includes_test_dir_when_requested(self):
        with tempfile.TemporaryDirectory() as d:
            base = fixtures.make_swift_tree(Path(d))
            files = scanner.collect_swift_files(base, True, [], ".swift")
            names = sorted(f.name for f in files)
            self.assertIn("CoreTests.swift", names)

    def test_build_dir_always_skipped_even_with_tests(self):
        with tempfile.TemporaryDirectory() as d:
            base = fixtures.make_swift_tree(Path(d))
            files = scanner.collect_swift_files(base, True, [], ".swift")
            self.assertNotIn("Generated.swift", [f.name for f in files])

    def test_only_matches_given_extension(self):
        with tempfile.TemporaryDirectory() as d:
            base = fixtures.make_swift_tree(Path(d))
            files = scanner.collect_swift_files(base, False, [], ".m")
            self.assertEqual(files, [])

    def test_ignore_pattern_excludes_folder(self):
        with tempfile.TemporaryDirectory() as d:
            base = fixtures.make_swift_tree(Path(d))
            files = scanner.collect_swift_files(base, False, ["Feature"], ".swift")
            self.assertNotIn("FeatureView.swift", [f.name for f in files])


class RelFolderTest(unittest.TestCase):
    def test_root_level_file_maps_to_dot(self):
        root = Path("/root")
        self.assertEqual(scanner.rel_folder(root / "RootType.swift", root), ".")

    def test_nested_file_uses_posix_folder(self):
        root = Path("/root")
        self.assertEqual(
            scanner.rel_folder(root / "A" / "B" / "File.swift", root), "A/B"
        )

    def test_single_level_folder(self):
        root = Path("/root")
        self.assertEqual(scanner.rel_folder(root / "Core" / "C.swift", root), "Core")


class ComputePairTypesTest(unittest.TestCase):
    def test_maps_pair_to_referenced_type_set(self):
        recs = [{"folder": "Feature", "refs": ["CoreService"], "name": "x"}]
        owners = {"CoreService": {"Core"}}
        pair = scanner.compute_pair_types(recs, owners)
        self.assertEqual(dict(pair), {("Feature", "Core"): {"CoreService"}})

    def test_excludes_same_folder_owner(self):
        # A folder referencing a type it itself declares produces no pair.
        recs = [{"folder": "A", "refs": ["T"], "name": "x"}]
        owners = {"T": {"A"}}
        self.assertEqual(dict(scanner.compute_pair_types(recs, owners)), {})

    def test_type_with_multiple_owners_yields_edge_per_other_owner(self):
        recs = [{"folder": "Feature", "refs": ["Shared"], "name": "x"}]
        owners = {"Shared": {"Core", "Util"}}
        pair = scanner.compute_pair_types(recs, owners)
        self.assertEqual(pair[("Feature", "Core")], {"Shared"})
        self.assertEqual(pair[("Feature", "Util")], {"Shared"})

    def test_unknown_ref_contributes_nothing(self):
        recs = [{"folder": "A", "refs": ["Mystery"], "name": "x"}]
        owners = {"Known": {"B"}}
        self.assertEqual(dict(scanner.compute_pair_types(recs, owners)), {})


class ScanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = fixtures.make_swift_tree(Path(self._tmp.name))
        self.graph = scanner.scan(self.base, False, [], ".swift")

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_graphdata(self):
        self.assertIsInstance(self.graph, GraphData)

    def test_decls_from_real_code_only(self):
        # CoreService declared in Core; the comment + string literal that also
        # spell FeatureView in CoreService.swift must NOT add a decl there.
        self.assertEqual(self.graph.decls["Core"], {"CoreService"})
        self.assertEqual(self.graph.decls["Feature"], {"FeatureView"})

    def test_commented_and_string_tokens_are_not_refs(self):
        # Core only references the type it declares; the // comment and the
        # "FeatureView string literal" are stripped before scanning.
        core_rec = next(f for f in self.graph.file_records if f["folder"] == "Core")
        self.assertEqual(core_rec["refs"], ["CoreService"])
        self.assertNotIn("FeatureView", core_rec["refs"])

    def test_leaf_edge_feature_to_core_present(self):
        # Feature/FeatureView.swift uses CoreService() -> Feature depends on Core.
        self.assertIn(("Feature", "Core"), self.graph.leaf_edges)
        self.assertEqual(self.graph.leaf_edges[("Feature", "Core")], 1)

    def test_no_self_edges(self):
        # Even though Core references its own CoreService, src == dst is excluded.
        for (src, dst) in self.graph.leaf_edges:
            self.assertNotEqual(src, dst)

    def test_root_file_lands_in_dot_folder(self):
        self.assertIn(".", self.graph.decls)
        self.assertEqual(self.graph.decls["."], {"RootType"})

    def test_multi_decl_types_empty(self):
        # No type name is declared in two folders in this tree.
        self.assertEqual(self.graph.multi_decl_types, set())

    def test_all_folders_covers_declarers(self):
        self.assertEqual(self.graph.all_folders, {".", "Core", "Feature"})

    def test_type_owners_sorted_lists(self):
        self.assertEqual(self.graph.type_owners["CoreService"], ["Core"])
        self.assertEqual(self.graph.type_owners["FeatureView"], ["Feature"])
        self.assertEqual(self.graph.type_owners["RootType"], ["."])

    def test_raw_owners_are_sets(self):
        self.assertEqual(self.graph.raw_owners["CoreService"], {"Core"})

    def test_file_records_one_per_collected_file(self):
        folders = sorted(f["folder"] for f in self.graph.file_records)
        self.assertEqual(folders, [".", "Core", "Feature"])

    def test_index_only_extras_left_at_defaults(self):
        # The scan path cannot resolve these — they stay at GraphData defaults.
        self.assertIsNone(self.graph.pair_types)
        self.assertEqual(self.graph.type_kinds, {})
        self.assertEqual(self.graph.file_edges, [])
        self.assertEqual(self.graph.type_edges, [])

    def test_test_decls_included_when_requested(self):
        # With include_tests, the Tests/ folder's declaration is picked up.
        g = scanner.scan(self.base, True, [], ".swift")
        self.assertIn("CoreServiceTests", g.decls.get("Tests", set()))


if __name__ == "__main__":
    unittest.main()
