"""Cross-language parity: the JS Tarjan SCC (graph_logic.js `tarjanSccs`, used by
the in-browser migration wizard) must agree with Python's
`modgraph.graph._tarjan_sccs` (used by the CLI) — otherwise the plan shown in
the HTML could differ from the plan the CLI emits.

Both implement the same iterative Tarjan; this drives them with identical graphs
and compares the resulting partition (as a set of frozensets, since component
and within-component order are implementation details).

Requires Node to run the JS side. When `node` isn't on PATH the whole case is
skipped (not failed), so the stdlib-only Python suite stays hermetic — it never
hard-depends on Node.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from modgraph import graph

_GRAPH_LOGIC = Path(graph.__file__).resolve().parent / "templates" / "graph_logic.js"
_NODE = shutil.which("node")

# (nodes, deps-as-adjacency-lists) — covers a DAG, a 2-cycle (the shared fixture
# topology), a full 3-cycle, two disjoint cycles, and self/forward mixes.
_CASES = [
    (["A", "B", "C"], {"A": ["B"], "B": ["C"], "C": []}),
    (["App", "Feature", "Core", "Util"],
     {"App": ["Feature"], "Feature": ["Core"], "Core": ["Util"], "Util": ["Core"]}),
    (["A", "B", "C"], {"A": ["B"], "B": ["C"], "C": ["A"]}),
    (["A", "B", "C", "D", "E"],
     {"A": ["B"], "B": ["A"], "C": ["D"], "D": ["C"], "E": ["A"]}),
    (["X", "Y", "Z"], {"X": ["Y", "Z"], "Y": ["Z"], "Z": []}),
]


def _js_sccs(nodes, deps):
    """Run graph_logic.js `tarjanSccs` under Node, return list of components."""
    script = (
        "const {tarjanSccs} = require(" + json.dumps(str(_GRAPH_LOGIC)) + ");"
        "let raw='';process.stdin.on('data',d=>raw+=d).on('end',()=>{"
        "const o=JSON.parse(raw);"
        "process.stdout.write(JSON.stringify(tarjanSccs(o.nodes,o.deps)));});"
    )
    proc = subprocess.run(
        [_NODE, "-e", script],
        input=json.dumps({"nodes": nodes, "deps": deps}),
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def _partition(sccs):
    """A comparable, order-independent view of an SCC list."""
    return {frozenset(c) for c in sccs}


@unittest.skipIf(_NODE is None, "node not on PATH; skipping JS↔Python SCC parity")
class SccParityTest(unittest.TestCase):
    def test_js_matches_python_on_each_case(self):
        for nodes, deps in _CASES:
            with self.subTest(nodes=nodes):
                py = graph._tarjan_sccs(set(nodes), {k: set(v) for k, v in deps.items()})
                js = _js_sccs(nodes, deps)
                self.assertEqual(_partition(js), _partition(py))

    def test_partition_covers_all_nodes_exactly_once(self):
        # Sanity on the comparison itself: both sides partition the node set.
        for nodes, deps in _CASES:
            with self.subTest(nodes=nodes):
                py = graph._tarjan_sccs(set(nodes), {k: set(v) for k, v in deps.items()})
                flat = [n for c in py for n in c]
                self.assertCountEqual(flat, nodes)


if __name__ == "__main__":
    unittest.main()
