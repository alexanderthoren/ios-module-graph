"""Tests for modgraph.exclusions: persisted exclusion list + blocked-by set.

Follows tests/test_graph.py's shape: build inputs (from tests.fixtures or
inline), call the function, assert on concrete values, one behaviour per
method. All file IO happens inside a tempfile.TemporaryDirectory so the repo
tree is never touched.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from modgraph import exclusions
from tests import fixtures


class LoadExclusionsTest(unittest.TestCase):
    def _write(self, directory, text: str) -> Path:
        path = Path(directory) / "exclusions.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_plain_json_array(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps(["Core", "Util"]))
            self.assertEqual(exclusions.load_exclusions(path), {"Core", "Util"})

    def test_dict_wrapper_form(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps({"excluded": ["Core", "App"]}))
            self.assertEqual(exclusions.load_exclusions(path), {"Core", "App"})

    def test_dict_without_excluded_key_is_empty(self):
        # A dict with no "excluded" key falls back to [] -> empty set.
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps({"other": ["X"]}))
            self.assertEqual(exclusions.load_exclusions(path), set())

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "does_not_exist.json"
            self.assertEqual(exclusions.load_exclusions(path), set())

    def test_malformed_json_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, "{ this is not valid json ")
            self.assertEqual(exclusions.load_exclusions(path), set())

    def test_non_list_non_dict_scalar_is_empty(self):
        # A bare JSON string is neither dict nor list -> empty set.
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps("Core"))
            self.assertEqual(exclusions.load_exclusions(path), set())

    def test_json_number_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps(42))
            self.assertEqual(exclusions.load_exclusions(path), set())

    def test_json_null_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps(None))
            self.assertEqual(exclusions.load_exclusions(path), set())

    def test_strips_trailing_slashes(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps(["Core/", "Util///"]))
            # rstrip("/") removes every trailing slash.
            self.assertEqual(exclusions.load_exclusions(path), {"Core", "Util"})

    def test_strips_surrounding_whitespace(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps(["  Core  ", "\tUtil\n"]))
            self.assertEqual(exclusions.load_exclusions(path), {"Core", "Util"})

    def test_whitespace_then_trailing_slash(self):
        # .strip() runs first, then .rstrip("/"); a trailing slash with
        # surrounding whitespace still resolves to the bare name.
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps(["  Core/  "]))
            self.assertEqual(exclusions.load_exclusions(path), {"Core"})

    def test_drops_empty_and_whitespace_only_entries(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps(["Core", "", "   ", "Util"]))
            self.assertEqual(exclusions.load_exclusions(path), {"Core", "Util"})

    def test_empty_array_is_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps([]))
            self.assertEqual(exclusions.load_exclusions(path), set())

    def test_non_string_entries_are_coerced(self):
        # Entries are coerced via str(); non-empty strings survive.
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps([123, "Core"]))
            self.assertEqual(exclusions.load_exclusions(path), {"123", "Core"})

    def test_duplicate_entries_collapse(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps(["Core", "Core/", "Core"]))
            self.assertEqual(exclusions.load_exclusions(path), {"Core"})

    def test_returns_a_set(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, json.dumps(["Core"]))
            self.assertIsInstance(exclusions.load_exclusions(path), set)


class ComputeBlockedByExcludedTest(unittest.TestCase):
    def test_empty_when_no_exclusions(self):
        edges = fixtures.leaf_edges()
        self.assertEqual(
            exclusions.compute_blocked_by_excluded(edges, set(), set()), {}
        )

    def test_direct_dependent_is_blocked(self):
        # App -> Feature; excluding Feature blocks App, named by Feature.
        edges = {("App", "Feature"): 1}
        result = exclusions.compute_blocked_by_excluded(edges, {"Feature"}, set())
        self.assertEqual(result, {"App": {"Feature"}})

    def test_transitive_dependent_is_blocked_with_excluded_root_named(self):
        # App -> Feature -> Core, exclude Core. Both App and Feature transitively
        # reach Core, and both are blocked naming the excluded root "Core".
        edges = {("App", "Feature"): 1, ("Feature", "Core"): 1}
        result = exclusions.compute_blocked_by_excluded(edges, {"Core"}, set())
        self.assertEqual(result, {"Feature": {"Core"}, "App": {"Core"}})

    def test_excluded_folder_itself_not_in_result(self):
        edges = {("App", "Feature"): 1, ("Feature", "Core"): 1}
        result = exclusions.compute_blocked_by_excluded(edges, {"Core"}, set())
        self.assertNotIn("Core", result)

    def test_migrated_node_breaks_the_chain(self):
        # App -> Feature -> Core, exclude Core, Feature is migrated. The migrated
        # Feature breaks propagation: it isn't blocked AND App (its upstream) is
        # not blocked either.
        edges = {("App", "Feature"): 1, ("Feature", "Core"): 1}
        result = exclusions.compute_blocked_by_excluded(
            edges, {"Core"}, {"Feature"}
        )
        self.assertEqual(result, {})

    def test_migrated_leaf_dependent_not_blocked_but_chain_continues(self):
        # Two upstreams of Core: Feature (migrated) and App (not). Feature is
        # skipped; App is still blocked because it has its own direct edge.
        edges = {("Feature", "Core"): 1, ("App", "Core"): 1}
        result = exclusions.compute_blocked_by_excluded(
            edges, {"Core"}, {"Feature"}
        )
        self.assertEqual(result, {"App": {"Core"}})

    def test_independent_folder_not_blocked(self):
        # Other depends on nothing excluded; it stays clear.
        edges = {("App", "Core"): 1, ("Other", "App"): 1}
        result = exclusions.compute_blocked_by_excluded(edges, {"Core"}, set())
        # App reaches Core, Other reaches Core via App -> both blocked.
        self.assertEqual(result, {"App": {"Core"}, "Other": {"Core"}})
        # Sanity: an isolated folder with no path is absent.
        edges2 = {("App", "Core"): 1}
        result2 = exclusions.compute_blocked_by_excluded(
            edges2, {"Core"}, set()
        )
        self.assertNotIn("Lonely", result2)

    def test_multiple_excluded_roots_accumulate_per_folder(self):
        # App -> Core and App -> Util, exclude both. App is blocked by BOTH.
        edges = {("App", "Core"): 1, ("App", "Util"): 1}
        result = exclusions.compute_blocked_by_excluded(
            edges, {"Core", "Util"}, set()
        )
        self.assertEqual(result, {"App": {"Core", "Util"}})

    def test_dependency_direction_only_blocks_upstream(self):
        # Core -> Detail (Core depends on Detail). Excluding Core does NOT block
        # Detail, because Detail is downstream (a dependency, not a dependent).
        edges = {("Core", "Detail"): 1}
        result = exclusions.compute_blocked_by_excluded(edges, {"Core"}, set())
        self.assertEqual(result, {})

    def test_toy_graph_exclude_core(self):
        # Toy graph: App->Feature, App->Core, Feature->Core, Core<->Util.
        # Exclude Core. Everything that reaches Core is blocked: Util (Util->Core
        # direct), Feature (Feature->Core), App (App->Core). Core excluded itself.
        edges = fixtures.leaf_edges()
        result = exclusions.compute_blocked_by_excluded(edges, {"Core"}, set())
        self.assertNotIn("Core", result)
        self.assertEqual(set(result), {"Util", "Feature", "App"})
        for folder in ("Util", "Feature", "App"):
            self.assertEqual(result[folder], {"Core"})

    def test_toy_graph_migrated_feature_shields_app_from_core_via_feature(self):
        # Exclude Core, mark Feature migrated. App still reaches Core directly
        # (App -> Core) so App is blocked; Feature is migrated so absent.
        edges = fixtures.leaf_edges()
        result = exclusions.compute_blocked_by_excluded(
            edges, {"Core"}, {"Feature"}
        )
        self.assertNotIn("Feature", result)
        self.assertNotIn("Core", result)
        self.assertIn("App", result)
        self.assertIn("Util", result)
        self.assertEqual(result["App"], {"Core"})

    def test_cycle_does_not_loop_forever(self):
        # A <-> B mutual dependency, exclude A. B reaches A; B is blocked. The
        # seen_edge guard must terminate the BFS despite the cycle.
        edges = {("A", "B"): 1, ("B", "A"): 1}
        result = exclusions.compute_blocked_by_excluded(edges, {"A"}, set())
        self.assertEqual(result, {"B": {"A"}})

    def test_excluded_node_is_not_propagated_through(self):
        # X -> Excluded1 -> Excluded2 path: Excluded1 is itself excluded, so it
        # is skipped as a predecessor and its own upstream X is not reached
        # through it. (Both excluded folders stay out of the result.)
        edges = {("X", "Excluded1"): 1, ("Excluded1", "Excluded2"): 1}
        result = exclusions.compute_blocked_by_excluded(
            edges, {"Excluded1", "Excluded2"}, set()
        )
        # X depends directly on Excluded1 -> blocked by Excluded1.
        self.assertEqual(result, {"X": {"Excluded1"}})

    def test_accepts_list_of_edge_tuples(self):
        # The function iterates the edges container's items as (src, dst); a
        # list of 2-tuples works the same as a dict's keys.
        edges = [("App", "Feature"), ("Feature", "Core")]
        result = exclusions.compute_blocked_by_excluded(edges, {"Core"}, set())
        self.assertEqual(result, {"Feature": {"Core"}, "App": {"Core"}})


if __name__ == "__main__":
    unittest.main()
