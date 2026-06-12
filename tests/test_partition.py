"""Tests for modgraph.partition — usage-cohort module splitting."""
from __future__ import annotations

import json
import unittest

from modgraph.partition import compute_partition, compute_partitions


def _e(src_name, src_folder, dst_name, dst_folder, w=1):
    return {"src": f"{src_name}\t{src_folder}",
            "dst": f"{dst_name}\t{dst_folder}", "w": w}


def _module_graph():
    return {
        "nodes": [
            {"id": "app", "kind": "app", "label": "App", "types": 50,
             "churn": 5},
            {"id": "Pkg/Sources/M", "kind": "spm", "label": "M", "types": 9,
             "churn": 3},
            {"id": "Pkg/Sources/A", "kind": "spm", "label": "A", "types": 10,
             "churn": 1},
            {"id": "Pkg/Sources/B", "kind": "spm", "label": "B", "types": 10,
             "churn": 1},
        ],
        "edges": [
            {"from": "app", "to": "Pkg/Sources/M", "w": 2},
            {"from": "Pkg/Sources/A", "to": "Pkg/Sources/M", "w": 3},
            {"from": "Pkg/Sources/B", "to": "Pkg/Sources/M", "w": 3},
        ],
        "summary": {"churned": True},
    }


class TwoCohortModule(unittest.TestCase):
    """Module M: consumer A uses {A1->A2->A3->Shared}, consumer B uses
    {B1->B2->B3->Shared}; Shared is reachable from both -> core; U is
    unreached -> rump."""

    PREFIX = "Pkg/Sources/M"
    PREFIXES = ["Pkg/Sources/A", "Pkg/Sources/B", "Pkg/Sources/M"]

    def edges(self):
        M = self.PREFIX
        return [
            # external touch points
            _e("AUser", "Pkg/Sources/A/Src", "A1", M, 4),
            _e("BUser", "Pkg/Sources/B/Src", "B1", M, 5),
            # A's slice
            _e("A1", M, "A2", M),
            _e("A2", M, "A3", M),
            _e("A3", M, "Shared", M),
            # B's slice
            _e("B1", M, "B2", M),
            _e("B2", M, "B3", M),
            _e("B3", M, "Shared", M),
            # unreached internal type
            _e("U", M, "A1", M),
        ]

    def part(self, module_graph=None):
        return compute_partition(self.PREFIX, self.edges(), {"A1": "class"},
                                 self.PREFIXES, module_graph)

    def test_verdict_ok_with_two_disjoint_slices(self):
        p = self.part()
        self.assertEqual(p["verdict"], "ok")
        self.assertEqual(p["summary"]["parts"], 2)

    def test_slices_are_exclusive_closures(self):
        p = self.part()
        samples = {tuple(x["type_sample"]) for x in p["parts"]}
        self.assertIn(("B1", "B2", "B3"), samples)
        self.assertIn(("A1", "A2", "A3"), samples)

    def test_shared_type_lands_in_core(self):
        p = self.part()
        self.assertEqual(p["core"]["types"], 1)
        for x in p["parts"]:
            self.assertNotIn("Shared", x["type_sample"])

    def test_unreached_type_stays_in_rump(self):
        self.assertEqual(self.part()["rump"]["types"], 1)

    def test_blockers_list_the_shared_hub(self):
        p = self.part()
        self.assertEqual(p["blockers"][0]["type"], "Shared")
        self.assertEqual(p["blockers"][0]["cohorts"], 2)

    def test_public_surface_counts_entry_points(self):
        p = self.part()
        a = next(x for x in p["parts"] if "A1" in x["type_sample"])
        # A1 is externally referenced; A2 is referenced only from inside the
        # part (by A1) — not public. (U -> A1 also makes A1 rump-referenced.)
        self.assertEqual(a["public"], 1)

    def test_sim_prices_against_module_graph(self):
        p = self.part(_module_graph())
        self.assertIsNotNone(p["sim"])
        self.assertIn("→", p["sim"]["warm_cost"])
        self.assertEqual(p["sim"]["unit"], "types")

    def test_partition_lowers_warm_cost_here(self):
        # A and B each stop rebuilding the other's slice: warm must drop.
        p = self.part(_module_graph())
        self.assertLess(p["sim"]["warm_delta"], 0)

    def test_deterministic(self):
        a = json.dumps(self.part(_module_graph()), sort_keys=True)
        b = json.dumps(self.part(_module_graph()), sort_keys=True)
        self.assertEqual(a, b)


class NoSeamModule(unittest.TestCase):
    PREFIX = "Pkg/Sources/M"
    PREFIXES = ["Pkg/Sources/A", "Pkg/Sources/B", "Pkg/Sources/M"]

    def test_god_hub_collapses_to_no_seam(self):
        # Both consumers' closures pass through Hub which drags everything:
        # the core swallows the module.
        M = self.PREFIX
        edges = [
            _e("AUser", "Pkg/Sources/A/Src", "Hub", M, 2),
            _e("BUser", "Pkg/Sources/B/Src", "Hub", M, 2),
            _e("Hub", M, "X1", M), _e("Hub", M, "X2", M),
            _e("Hub", M, "X3", M), _e("Hub", M, "X4", M),
        ]
        p = compute_partition(M, edges, {}, self.PREFIXES)
        self.assertEqual(p["verdict"], "no_seam")
        self.assertEqual(p["blockers"][0]["type"], "Hub")

    def test_welded_cycle_is_atomic(self):
        # A1 <-> B1 mutual refs: one SCC, reachable from both -> core, and
        # the blocker reports the weld size.
        M = self.PREFIX
        edges = [
            _e("AUser", "Pkg/Sources/A/Src", "A1", M, 2),
            _e("BUser", "Pkg/Sources/B/Src", "B1", M, 2),
            _e("A1", M, "B1", M), _e("B1", M, "A1", M),
        ]
        p = compute_partition(M, edges, {}, self.PREFIXES)
        self.assertEqual(p["verdict"], "no_seam")
        self.assertEqual(p["blockers"][0]["welded"], 2)

    def test_no_consumers_means_no_entry(self):
        M = self.PREFIX
        edges = [_e("A1", M, "A2", M)]
        out = compute_partitions([M], edges, {}, self.PREFIXES)
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
