"""Tests for modgraph.build_impact — warm blast radius + cold critical path.

Asserts against the shared toy graph (see tests/fixtures.py):

    App ──▶ Feature ──▶ Core ◀──▶ Util
     └───────────────▶ Core

So rev-dependents: Core/Util are depended on by ~everything (high warm),
App by nobody (zero warm). Build cohorts: {Core,Util}=0, Feature=1, App=2.
"""
import unittest

from modgraph.build_impact import compute_build_impact
from modgraph.graph import build_tree

from tests import fixtures


def _impact():
    decls = {f: set(t) for f, t in fixtures.FOLDER_DECLS.items()}
    tree = build_tree(set(decls), decls, root_label="Toy")
    return compute_build_impact(tree, fixtures.leaf_edges())


class WarmBlastRadius(unittest.TestCase):
    def test_core_is_the_top_offender(self):
        nodes = _impact()["nodes"]
        # Core is depended on (transitively) by App, Feature and Util.
        self.assertEqual(nodes["Core"]["warm"], 3)
        self.assertEqual(nodes["Core"]["warm_pct"], 75.0)

    def test_app_has_zero_blast_radius(self):
        # Nothing depends on App, so touching it rebuilds nothing else.
        nodes = _impact()["nodes"]
        self.assertEqual(nodes["App"]["warm"], 0)
        self.assertEqual(nodes["App"]["fan_in"], 0)

    def test_fan_in_is_direct_dependents_only(self):
        nodes = _impact()["nodes"]
        # Util is depended on directly only by Core (the cycle partner)...
        self.assertEqual(nodes["Util"]["fan_in"], 1)
        # ...but transitively by Core, App and Feature.
        self.assertEqual(nodes["Util"]["warm"], 3)

    def test_root_has_no_external_blast(self):
        # The whole-project root's subtree contains everything, so there are no
        # external dependents to rebuild.
        nodes = _impact()["nodes"]
        self.assertEqual(nodes[""]["warm"], 0)


class ColdCriticalPath(unittest.TestCase):
    def test_build_cohorts(self):
        nodes = _impact()["nodes"]
        # {Core,Util} compile first (no first-party deps), then Feature, then App.
        self.assertEqual(nodes["Core"]["level"], 0)
        self.assertEqual(nodes["Util"]["level"], 0)
        self.assertEqual(nodes["Feature"]["level"], 1)
        self.assertEqual(nodes["App"]["level"], 2)

    def test_cycle_is_flagged(self):
        nodes = _impact()["nodes"]
        # Core ⇄ Util is a fused SCC of size 2.
        self.assertEqual(nodes["Core"]["scc"], 2)
        self.assertEqual(nodes["Util"]["scc"], 2)
        self.assertEqual(nodes["App"]["scc"], 1)

    def test_whole_chain_is_critical(self):
        # The toy graph is a single dependency spine, so every node lies on it.
        nodes = _impact()["nodes"]
        for f in ("App", "Feature", "Core", "Util"):
            self.assertTrue(nodes[f]["crit"], f)

    def test_summary(self):
        s = _impact()["summary"]
        self.assertEqual(s["graph_nodes"], 4)
        self.assertEqual(s["cohorts"], 3)
        self.assertEqual(s["crit_len"], 3)
        self.assertEqual(s["n_cycles"], 1)
        self.assertEqual(s["biggest_scc"], 2)
        # Canonical longest chain, deepest cohort first: App → Feature → {Core,Util}.
        self.assertEqual(s["critical_path"], [["App"], ["Feature"], ["Core", "Util"]])


class Determinism(unittest.TestCase):
    def test_repeated_runs_identical(self):
        self.assertEqual(_impact(), _impact())


if __name__ == "__main__":
    unittest.main()
