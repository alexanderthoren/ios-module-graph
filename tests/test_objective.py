"""Tests for modgraph.objective — the expected-build-cost model."""
from __future__ import annotations

import unittest

from modgraph.objective import compute_cost_model, compute_objective


class CostModelTest(unittest.TestCase):
    """A chain a -> b -> c (a depends on b, b on c) with known work."""

    def setUp(self):
        self.modules = {"a", "b", "c"}
        self.edges = {("a", "b"): 1, ("b", "c"): 2}
        self.work = {"a": 10.0, "b": 5.0, "c": 1.0}

    def test_rebuild_work_includes_transitive_dependents(self):
        cost = compute_cost_model(self.modules, self.edges, self.work)
        # Editing c rebuilds c, b and a; editing a rebuilds only a.
        self.assertEqual(cost["rebuild"]["c"], 16.0)
        self.assertEqual(cost["rebuild"]["b"], 15.0)
        self.assertEqual(cost["rebuild"]["a"], 10.0)

    def test_dependent_counts(self):
        cost = compute_cost_model(self.modules, self.edges, self.work)
        self.assertEqual(cost["dependents"], {"a": 0, "b": 1, "c": 2})

    def test_warm_cost_unweighted_is_sum_of_rebuilds(self):
        cost = compute_cost_model(self.modules, self.edges, self.work)
        self.assertEqual(cost["warm_cost"], 16.0 + 15.0 + 10.0)

    def test_warm_cost_weights_by_churn(self):
        churn = {"a": 10, "b": 0, "c": 1}
        cost = compute_cost_model(self.modules, self.edges, self.work, churn)
        # A hot leaf (a) is cheap to edit; a touched foundation (c) is not.
        self.assertEqual(cost["warm_cost"], 10 * 10.0 + 0 * 15.0 + 1 * 16.0)

    def test_cold_cost_is_work_weighted_longest_chain(self):
        cost = compute_cost_model(self.modules, self.edges, self.work)
        self.assertEqual(cost["cold_cost"], 16.0)

    def test_parallel_siblings_shorten_cold_not_warm(self):
        # a and b both depend on c only: chain is max(a, b) + c.
        edges = {("a", "c"): 1, ("b", "c"): 1}
        cost = compute_cost_model(self.modules, edges, self.work)
        self.assertEqual(cost["cold_cost"], 11.0)
        self.assertEqual(cost["rebuild"]["c"], 16.0)

    def test_efficiency_against_cores(self):
        cost = compute_cost_model(self.modules, self.edges, self.work, cores=4)
        # resource floor 16/4 = 4.0; wall floor = chain 16.0 -> 0.25.
        self.assertEqual(cost["efficiency"], 0.25)

    def test_cycle_degrades_instead_of_recursing(self):
        edges = {("a", "b"): 1, ("b", "a"): 1}
        cost = compute_cost_model({"a", "b"}, edges, {"a": 2.0, "b": 3.0})
        self.assertGreaterEqual(cost["cold_cost"], 3.0)

    def test_deterministic(self):
        runs = {repr(compute_cost_model(self.modules, self.edges, self.work))
                for _ in range(5)}
        self.assertEqual(len(runs), 1)


class ObjectiveTest(unittest.TestCase):
    def _graph(self, *, measured):
        return {
            "nodes": [
                {"id": "app", "kind": "app", "types": 50, "build_ms": 8000,
                 "churn": 9},
                {"id": "P", "kind": "spm", "types": 10, "build_ms": 2000,
                 "churn": 1},
            ],
            "edges": [{"from": "app", "to": "P", "w": 4}],
            "summary": {"measured": measured, "churned": True, "cores": 2},
        }

    def test_proxy_unit_uses_types(self):
        obj = compute_objective(self._graph(measured=False))
        self.assertEqual(obj["unit"], "types")
        self.assertEqual(obj["rebuild"]["P"], 60.0)

    def test_measured_unit_uses_seconds(self):
        obj = compute_objective(self._graph(measured=True))
        self.assertEqual(obj["unit"], "s")
        self.assertEqual(obj["rebuild"]["P"], 10.0)

    def test_churn_weights_apply(self):
        obj = compute_objective(self._graph(measured=True))
        # warm = churn(app)*rebuild(app) + churn(P)*rebuild(P) = 9*8 + 1*10.
        self.assertEqual(obj["warm_cost"], 82.0)


if __name__ == "__main__":
    unittest.main()
