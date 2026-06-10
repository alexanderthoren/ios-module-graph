"""Tests for modgraph.graph: Tarjan SCC, migration plan, folder tree.

This module doubles as the suite's exemplar — other test modules follow its
shape: build inputs from tests.fixtures (or inline), call the function, assert
on concrete values, and keep one behaviour per test method.
"""
from __future__ import annotations

import unittest

from modgraph import graph
from tests import fixtures


class TarjanSCCTest(unittest.TestCase):
    def test_singletons_when_acyclic(self):
        nodes = {"a", "b", "c"}
        deps = {"a": {"b"}, "b": {"c"}}
        sccs = graph._tarjan_sccs(nodes, deps)
        # Every SCC is a singleton in a DAG.
        self.assertEqual(sorted(len(c) for c in sccs), [1, 1, 1])

    def test_detects_cycle(self):
        nodes = {"x", "y", "z"}
        deps = {"x": {"y"}, "y": {"x"}, "z": {"x"}}
        sccs = graph._tarjan_sccs(nodes, deps)
        cycles = [sorted(c) for c in sccs if len(c) > 1]
        self.assertEqual(cycles, [["x", "y"]])

    def test_self_loop_is_single_scc(self):
        sccs = graph._tarjan_sccs({"a"}, {"a": {"a"}})
        self.assertEqual([sorted(c) for c in sccs], [["a"]])

    def test_handles_large_chain_without_recursion_error(self):
        # Iterative implementation must survive a deep chain.
        n = 5000
        nodes = {str(i) for i in range(n)}
        deps = {str(i): {str(i + 1)} for i in range(n - 1)}
        sccs = graph._tarjan_sccs(nodes, deps)
        self.assertEqual(len(sccs), n)


class MigrationPlanTest(unittest.TestCase):
    def setUp(self):
        self.edges = fixtures.leaf_edges()
        self.folders = fixtures.source_folders()

    def test_toy_plan_shape(self):
        plan, stuck = graph.compute_migration_plan(self.edges, self.folders)
        self.assertEqual(stuck, [])
        self.assertEqual(len(plan), 3)
        # Cycle bundle migrates first.
        self.assertEqual(plan[0]["folders"], ["Core", "Util"])
        self.assertTrue(plan[0]["is_cycle"])
        self.assertEqual(plan[0]["size"], 2)
        # Then Feature, then App.
        self.assertEqual(plan[1]["folders"], ["Feature"])
        self.assertFalse(plan[1]["is_cycle"])
        self.assertEqual(plan[2]["folders"], ["App"])

    def test_steps_are_numbered_sequentially(self):
        plan, _ = graph.compute_migration_plan(self.edges, self.folders)
        self.assertEqual([p["step"] for p in plan], [1, 2, 3])

    def test_unlocks_reference_later_steps(self):
        plan, _ = graph.compute_migration_plan(self.edges, self.folders)
        # The cycle unlocks Feature; Feature unlocks App.
        self.assertEqual([u["folders"] for u in plan[0]["unlocks"]], [["Feature"]])
        self.assertEqual([u["folders"] for u in plan[1]["unlocks"]], [["App"]])
        self.assertEqual(plan[2]["unlocks"], [])
        # Backfilled step numbers point at the real step.
        self.assertEqual(plan[0]["unlocks"][0]["step"], 2)

    def test_deterministic_across_calls(self):
        # Regression: the plan used to reshuffle between runs because ties fell
        # back to set-iteration order. It must now be identical every call.
        a, _ = graph.compute_migration_plan(self.edges, self.folders)
        b, _ = graph.compute_migration_plan(self.edges, self.folders)
        self.assertEqual(a, b)

    def test_tie_break_is_alphabetical(self):
        # Two independent leaves, equal rank — alphabetically smaller goes first.
        plan, _ = graph.compute_migration_plan({}, {"Bravo", "Alpha"})
        self.assertEqual([p["folders"][0] for p in plan], ["Alpha", "Bravo"])

    def test_edges_into_unknown_folders_ignored(self):
        # Edges whose endpoints aren't both in source_folders don't create deps.
        edges = {("A", "External"): 5}
        plan, _ = graph.compute_migration_plan(edges, {"A"})
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["folders"], ["A"])

    def test_self_edges_skipped(self):
        plan, _ = graph.compute_migration_plan({("A", "A"): 3}, {"A"})
        self.assertEqual(len(plan), 1)
        self.assertFalse(plan[0]["is_cycle"])


class MigrationPlanRoiTest(unittest.TestCase):
    """ROI-ranked frontier: `scores` reorders the eligible set, never the topology."""

    # Two independent leaves consumed by C: both eligible at step 1.
    EDGES = {("C", "A"): 1, ("C", "B"): 1}
    FOLDERS = {"A", "B", "C"}

    def test_without_scores_steps_carry_none(self):
        plan, _ = graph.compute_migration_plan(self.EDGES, self.FOLDERS)
        self.assertIsNone(plan[0]["roi"])
        self.assertIsNone(plan[0]["payoff"])
        self.assertIsNone(plan[0]["effort"])

    def test_higher_roi_jumps_the_frontier(self):
        # Default order is alphabetical (A, B tie on reverse-reach); a higher
        # ROI on B flips it.
        scores = {"A": {"combined": 10.0, "effort": 10},
                  "B": {"combined": 50.0, "effort": 1},
                  "C": {"combined": 0.0, "effort": 1}}
        plan, _ = graph.compute_migration_plan(self.EDGES, self.FOLDERS, scores)
        self.assertEqual([p["folders"][0] for p in plan], ["B", "A", "C"])

    def test_roi_never_violates_topology(self):
        # C has a huge score but depends on A and B — it still goes last.
        scores = {"A": {"combined": 1.0, "effort": 1},
                  "B": {"combined": 1.0, "effort": 1},
                  "C": {"combined": 100.0, "effort": 1}}
        plan, _ = graph.compute_migration_plan(self.EDGES, self.FOLDERS, scores)
        self.assertEqual(plan[2]["folders"], ["C"])

    def test_hot_preferred_over_combined(self):
        # A wins on `combined` but B wins on churn-weighted `hot`.
        scores = {"A": {"combined": 50.0, "hot": 0.0, "effort": 1},
                  "B": {"combined": 10.0, "hot": 10.0, "effort": 1},
                  "C": {"combined": 0.0, "effort": 1}}
        plan, _ = graph.compute_migration_plan(self.EDGES, self.FOLDERS, scores)
        self.assertEqual(plan[0]["folders"], ["B"])

    def test_steps_annotated_with_aggregates(self):
        scores = {"A": {"combined": 10.0, "effort": 4},
                  "B": {"combined": 50.0, "effort": 1},
                  "C": {"combined": 0.0, "effort": 1}}
        plan, _ = graph.compute_migration_plan(self.EDGES, self.FOLDERS, scores)
        b = next(p for p in plan if p["folders"] == ["B"])
        self.assertEqual(b["payoff"], 50.0)
        self.assertEqual(b["effort"], 1)
        self.assertEqual(b["roi"], 50.0)

    def test_cycle_bundle_sums_member_scores(self):
        edges = {("X", "Y"): 1, ("Y", "X"): 1}
        scores = {"X": {"combined": 30.0, "effort": 2},
                  "Y": {"combined": 10.0, "effort": 2}}
        plan, _ = graph.compute_migration_plan(edges, {"X", "Y"}, scores)
        self.assertEqual(plan[0]["payoff"], 40.0)
        self.assertEqual(plan[0]["effort"], 4)
        self.assertEqual(plan[0]["roi"], 10.0)

    def test_missing_score_rows_default_to_zero(self):
        plan, _ = graph.compute_migration_plan(self.EDGES, self.FOLDERS, {})
        self.assertEqual([p["folders"][0] for p in plan], ["A", "B", "C"])
        self.assertEqual(plan[0]["roi"], 0.0)

    def test_deterministic_across_calls_with_scores(self):
        scores = {"A": {"combined": 10.0, "effort": 10},
                  "B": {"combined": 50.0, "effort": 1},
                  "C": {"combined": 0.0, "effort": 1}}
        a, _ = graph.compute_migration_plan(self.EDGES, self.FOLDERS, scores)
        b, _ = graph.compute_migration_plan(self.EDGES, self.FOLDERS, scores)
        self.assertEqual(a, b)


class BuildTreeTest(unittest.TestCase):
    def test_keys_sorted_for_determinism(self):
        # Regression: tree dict came from a set and reordered per hash seed.
        tree = graph.build_tree({"App", "Core", "Util", "Feature"}, {})
        self.assertEqual(list(tree), ["", "App", "Core", "Feature", "Util"])

    def test_root_node_uses_label(self):
        tree = graph.build_tree({"App"}, {}, root_label="MyProj")
        self.assertEqual(tree[""]["name"], "MyProj")
        self.assertIsNone(tree[""]["parent"])
        self.assertIn("App", tree[""]["children"])

    def test_intermediate_folders_synthesised(self):
        tree = graph.build_tree({"A/B/C"}, {})
        # The chain A, A/B, A/B/C all exist with correct parent links.
        self.assertEqual(tree["A/B/C"]["parent"], "A/B")
        self.assertEqual(tree["A/B"]["parent"], "A")
        self.assertEqual(tree["A"]["parent"], "")
        self.assertEqual(tree["A"]["children"], ["A/B"])

    def test_type_counts_propagate_up(self):
        decls = {"A/B": {"One", "Two"}, "A": {"Three"}}
        tree = graph.build_tree({"A", "A/B"}, decls)
        self.assertEqual(tree["A/B"]["types"], 2)
        self.assertEqual(tree["A"]["types"], 3)   # own decls
        self.assertEqual(tree[""]["types"], 3)    # root = sum of subtree

    def test_children_sorted(self):
        tree = graph.build_tree({"R/z", "R/a", "R/m", "R"}, {})
        self.assertEqual(tree["R"]["children"], ["R/a", "R/m", "R/z"])


if __name__ == "__main__":
    unittest.main()
