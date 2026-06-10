"""Cross-language parity: the JS migration-plan ordering (graph_logic.js
`migrationPlanOrder`, behind the in-browser wizard) must agree with Python's
`modgraph.graph.compute_migration_plan` (the CLI) on the **step order** — so the
plan a user sees in the HTML matches the plan the CLI emits.

Caveat: the wizard adds a secondary "most-used" (inbound-weight) tiebreaker that
Python doesn't have, so the two are only guaranteed to agree when transitive
reverse-reach alone determines the order. The cases below are chosen so that
holds (linear chains, the fixture topology, distinct-reach trees); they always
agree on the *set* of steps regardless.

Requires Node; skips (not fails) when `node` isn't on PATH so the stdlib-only
Python suite stays hermetic.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from collections import defaultdict
from pathlib import Path

from modgraph import graph

_GRAPH_LOGIC = Path(graph.__file__).resolve().parent / "templates" / "graph_logic.js"
_NODE = shutil.which("node")

# Each case: list of (src, dst, weight) edges (src depends on dst). Chosen so
# reverse-reach uniquely orders every step (no reliance on the wizard's extra
# inbound-weight tiebreaker).
_CASES = {
    "linear_chain": [("A", "B", 1), ("B", "C", 1), ("C", "D", 1)],
    "fixture": [("App", "Feature", 1), ("Feature", "Core", 1),
                ("Core", "Util", 1), ("Util", "Core", 1)],
    "converging_tree": [("A", "C", 1), ("B", "C", 1), ("C", "D", 1)],
    "two_levels": [("A", "B", 1), ("A", "C", 1), ("B", "D", 1), ("C", "D", 1)],
}


def _nodes(edges):
    s = set()
    for a, b, _ in edges:
        s.add(a)
        s.add(b)
    return sorted(s)


def _py_plan_folders(edges, scores=None):
    leaf_edges = {(a, b): w for a, b, w in edges}
    # Python scores rows use `combined` (hot absent) — payoff falls back to it.
    py_scores = ({f: {"combined": s["payoff"], "effort": s["effort"]}
                  for f, s in scores.items()} if scores is not None else None)
    plan, _ = graph.compute_migration_plan(leaf_edges, set(_nodes(edges)),
                                           py_scores)
    return [step["folders"] for step in plan]


def _js_plan_folders(edges, scores=None):
    nodes = _nodes(edges)
    deps = defaultdict(list)
    for a, b, _ in edges:
        if a != b:
            deps[a].append(b)
    wedges = [{"src": a, "dst": b, "w": w} for a, b, w in edges]
    script = (
        "const {migrationPlanOrder} = require(" + json.dumps(str(_GRAPH_LOGIC)) + ");"
        "let raw='';process.stdin.on('data',d=>raw+=d).on('end',()=>{"
        "const o=JSON.parse(raw);"
        "const steps=migrationPlanOrder(o.nodes,o.deps,o.wedges,o.scores??null);"
        "process.stdout.write(JSON.stringify(steps.map(s=>s.folders)));});"
    )
    proc = subprocess.run(
        [_NODE, "-e", script],
        input=json.dumps({"nodes": nodes, "deps": dict(deps), "wedges": wedges,
                          "scores": scores}),
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def _scores_for(edges):
    """Distinct integer payoff per folder (effort 1) so the ROI key alone
    determines every frontier pick — no reliance on the wizard's extra
    inbound-weight tiebreaker, and integer values round identically in both
    languages."""
    return {n: {"payoff": 10 * (i + 1), "effort": 1}
            for i, n in enumerate(_nodes(edges))}


@unittest.skipIf(_NODE is None, "node not on PATH; skipping JS↔Python plan parity")
class PlanParityTest(unittest.TestCase):
    def test_step_order_matches_python(self):
        for name, edges in _CASES.items():
            with self.subTest(case=name):
                self.assertEqual(_js_plan_folders(edges), _py_plan_folders(edges))

    def test_step_order_matches_python_with_scores(self):
        # ROI-ranked variant (scores given): both engines must agree too.
        for name, edges in _CASES.items():
            with self.subTest(case=name):
                scores = _scores_for(edges)
                self.assertEqual(_js_plan_folders(edges, scores),
                                 _py_plan_folders(edges, scores))

    def test_scores_change_the_order_in_both(self):
        # Sanity: the scored order genuinely differs from the structural one
        # (A and B tie on reverse-reach; B's higher ROI must flip them).
        edges = _CASES["converging_tree"]
        scores = _scores_for(edges)
        scored = _py_plan_folders(edges, scores)
        self.assertEqual(_js_plan_folders(edges, scores), scored)
        self.assertNotEqual(scored, _py_plan_folders(edges))

    def test_same_set_of_steps(self):
        # Even where order could differ, the partition into steps must match.
        for name, edges in _CASES.items():
            with self.subTest(case=name):
                js = {frozenset(g) for g in _js_plan_folders(edges)}
                py = {frozenset(g) for g in _py_plan_folders(edges)}
                self.assertEqual(js, py)


if __name__ == "__main__":
    unittest.main()
