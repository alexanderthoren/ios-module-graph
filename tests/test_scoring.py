"""Tests for modgraph.scoring — per-folder extraction payoff/effort scores.

Asserts against the shared toy project (see tests/fixtures.py):

    App ──▶ Feature ──▶ Core ◀──▶ Util
     └───────────────▶ Core

Warm (transitive reverse-dependents within the plan edges): Core 3, Util 3,
Feature 1, App 0. SCC levels: {Core, Util} = 0, Feature = 1, App = 2 — the
single chain means every folder is on the cold critical path.
"""
from __future__ import annotations

import unittest

from modgraph.graph import build_tree
from modgraph.scoring import compute_folder_scores
from tests.fixtures import FOLDER_DECLS, FILES, leaf_edges, pair_types, source_folders


def toy_scores(churn_commits=None, with_pair_types=True):
    decls = {f: set(t) for f, t in FOLDER_DECLS.items()}
    tree = build_tree(source_folders(), decls, root_label="Toy")
    return compute_folder_scores(
        tree,
        leaf_edges(),
        source_folders(),
        pair_types() if with_pair_types else None,
        decls,
        [dict(f) for f in FILES],
        churn_commits=churn_commits,
    )


class WarmAndColdTest(unittest.TestCase):
    def test_warm_is_transitive_reverse_dependents(self):
        rows = toy_scores()["folders"]
        self.assertEqual(rows["Core"]["warm"], 3)
        self.assertEqual(rows["Util"]["warm"], 3)   # via the Core cycle
        self.assertEqual(rows["Feature"]["warm"], 1)
        self.assertEqual(rows["App"]["warm"], 0)

    def test_cold_score_is_types_iff_on_critical_path(self):
        rows = toy_scores()["folders"]
        # Single chain: everything is critical, so cold = declared-type count.
        self.assertTrue(all(r["crit"] for r in rows.values()))
        self.assertEqual(rows["Feature"]["cold_score"], 2)
        self.assertEqual(rows["Core"]["cold_score"], 1)

    def test_level_and_crit_reach_folder_rows(self):
        # quick_wins consumes these for the level projection — they must
        # survive the copy out of build_impact.
        rows = toy_scores()["folders"]
        self.assertEqual(rows["Core"]["level"], 0)   # {Core, Util} SCC
        self.assertEqual(rows["Util"]["level"], 0)
        self.assertEqual(rows["Feature"]["level"], 1)
        self.assertEqual(rows["App"]["level"], 2)
        self.assertTrue(all(isinstance(r["crit"], bool)
                            for r in rows.values()))

    def test_isolated_folder_gets_zero_impact(self):
        decls = {"A": {"T1"}, "B": {"T2"}, "Iso": {"T3"}}
        tree = build_tree(set(decls), decls, root_label="X")
        out = compute_folder_scores(
            tree, {("A", "B"): 1}, set(decls), None, decls,
            [{"folder": "Iso", "name": "T3.swift"}],
        )
        iso = out["folders"]["Iso"]
        self.assertEqual(iso["warm"], 0)
        self.assertFalse(iso["crit"])
        self.assertEqual(iso["cold_score"], 0)


class EffortTest(unittest.TestCase):
    def test_effort_components(self):
        rows = toy_scores()["folders"]
        # Core: 1 file + 1 outgoing ref (Core->Util) + 1 public type (CoreService).
        self.assertEqual(rows["Core"]["files"], 1)
        self.assertEqual(rows["Core"]["out_refs"], 1)
        self.assertEqual(rows["Core"]["public"], 1)
        self.assertEqual(rows["Core"]["effort"], 3)
        # App references others (3 refs out) but exposes nothing.
        self.assertEqual(rows["App"]["public"], 0)
        self.assertEqual(rows["App"]["effort"], 4)

    def test_public_surface_zero_without_pair_types(self):
        rows = toy_scores(with_pair_types=False)["folders"]
        self.assertEqual(rows["Core"]["public"], 0)
        self.assertEqual(rows["Core"]["effort"], 2)   # files + out_refs only


class ScoreTest(unittest.TestCase):
    def test_combined_normalizes_warm_and_cold_to_100(self):
        rows = toy_scores()["folders"]
        # Util maxes both lenses: warm 3/3, cold 2/2.
        self.assertEqual(rows["Util"]["combined"], 100.0)
        self.assertEqual(rows["Core"]["combined"], 75.0)

    def test_roi_is_payoff_over_effort(self):
        rows = toy_scores()["folders"]
        self.assertEqual(rows["Core"]["roi"], 25.0)    # 75.0 / 3
        self.assertEqual(rows["Util"]["roi"], 25.0)    # 100.0 / 4

    def test_hot_none_without_churn(self):
        out = toy_scores()
        self.assertFalse(out["summary"]["churned"])
        self.assertIsNone(out["folders"]["Core"]["hot"])

    def test_churn_weights_payoff(self):
        out = toy_scores(churn_commits=[{"Core"}, {"Core", "Util"}, {"Feature"}])
        rows = out["folders"]
        self.assertTrue(out["summary"]["churned"])
        self.assertEqual(rows["Core"]["churn"], 2)
        self.assertEqual(rows["Core"]["hot"], 75.0)    # combined * 2/2
        self.assertEqual(rows["Util"]["hot"], 50.0)    # combined * 1/2
        self.assertEqual(rows["App"]["hot"], 0.0)      # untouched all window
        self.assertEqual(rows["App"]["roi"], 0.0)      # hot payoff, not combined


class DeterminismTest(unittest.TestCase):
    def test_folders_keyed_sorted(self):
        self.assertEqual(list(toy_scores()["folders"]),
                         sorted(toy_scores()["folders"]))

    def test_repeated_runs_identical(self):
        self.assertEqual(toy_scores(), toy_scores())


class SummaryTest(unittest.TestCase):
    def test_summary_counts(self):
        s = toy_scores()["summary"]
        self.assertEqual(s["folders"], 4)
        self.assertEqual(s["max_warm"], 3)
        self.assertEqual(s["max_cold"], 2)
        self.assertEqual(s["crit_len"], 3)   # App -> Feature -> {Core, Util}

    def test_empty_input(self):
        out = compute_folder_scores({}, {}, set(), None, {}, [])
        self.assertEqual(out["folders"], {})
        self.assertEqual(out["summary"]["folders"], 0)


if __name__ == "__main__":
    unittest.main()
