"""Tests for modgraph.evaluate — the plan-policy comparison harness."""
from __future__ import annotations

import json
import unittest

from modgraph.evaluate import compute_evaluation, evaluation_markdown
from modgraph.master_plan import compute_master_plan

from tests.test_master_plan import _inputs, _kwargs


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        kw = _kwargs()
        self.plan = compute_master_plan(*_inputs(), **kw)
        source = {"Feat", "Solo", "Crit", "Abs", "Home"}
        plan_edges = {(a, b): w for (a, b), w in kw["leaf_edges"].items()
                      if a in source and b in source}
        self.ev = compute_evaluation(
            self.plan, kw["leaf_edges"], plan_edges, source,
            kw["migrated_prefixes"], kw["decls"])

    def test_three_policies(self):
        self.assertEqual([p["policy"] for p in self.ev["policies"]],
                         ["master", "no_api", "topo"])

    def test_master_reads_its_own_trajectory(self):
        master = self.ev["policies"][0]
        self.assertEqual(master["final"],
                         self.plan["trajectory"]["final"])
        self.assertEqual(master["steps"], len(self.plan["steps"]))

    def test_every_policy_replays_to_a_final_state(self):
        for p in self.ev["policies"]:
            self.assertIn("warm_cost", p["final"])
            self.assertIn("app_share_pct", p["final"])
            self.assertGreaterEqual(p["effort"], 0)

    def test_no_api_builds_fewer_modules_than_master(self):
        by = {p["policy"]: p for p in self.ev["policies"]}
        self.assertLess(by["no_api"]["final"]["modules"],
                        by["master"]["final"]["modules"])

    def test_topo_extracts_every_source_folder(self):
        topo = next(p for p in self.ev["policies"] if p["policy"] == "topo")
        self.assertEqual(topo["final"]["app_share_pct"], 0.0)

    def test_verdict_names_a_policy(self):
        self.assertIn(self.ev["verdict"], ("master", "no_api", "topo"))

    def test_markdown_renders_all_policies(self):
        md = evaluation_markdown(self.ev, "toy")
        for name in ("master", "no_api", "topo", "Verdict"):
            self.assertIn(name, md)

    def test_deterministic(self):
        kw = _kwargs()
        plan = compute_master_plan(*_inputs(), **kw)
        source = {"Feat", "Solo", "Crit", "Abs", "Home"}
        plan_edges = {(a, b): w for (a, b), w in kw["leaf_edges"].items()
                      if a in source and b in source}
        again = compute_evaluation(plan, kw["leaf_edges"], plan_edges, source,
                                   kw["migrated_prefixes"], kw["decls"])
        self.assertEqual(json.dumps(self.ev, sort_keys=True),
                         json.dumps(again, sort_keys=True))

    def test_json_serializable(self):
        json.dumps(self.ev)


if __name__ == "__main__":
    unittest.main()
