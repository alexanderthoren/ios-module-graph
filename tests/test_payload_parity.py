"""Cross-language parity: the payload interning encoder
(`modgraph.render._intern_payload`, Python) and its decoder
(graph_logic.js `decodePayload`, run by the browser before any UI code reads
DATA) must be exact mirrors — otherwise the rendered page would read garbled
edges/files while the Python tests keep passing.

This encodes a payload in Python, decodes it under Node with the real
graph_logic.js, and requires the result to equal the original byte-for-byte
(after JSON normalization).

Requires Node for the JS side. When `node` isn't on PATH the case is skipped
(not failed), so the stdlib-only Python suite stays hermetic.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from modgraph import render
from tests import fixtures

_GRAPH_LOGIC = Path(render.__file__).resolve().parent / "templates" / "graph_logic.js"
_NODE = shutil.which("node")


def _payload() -> dict:
    """All four interned sections, fed from the shared toy fixture, plus the
    awkward cases: tab-keyed type ids, unicode, empty lists, and an untouched
    sibling key that must pass through unchanged."""
    return {
        "edges": [{"src": e["src"], "dst": e["dst"], "w": e["w"]}
                  for e in fixtures.EDGES],
        "files": [dict(f) for f in fixtures.FILES] + [
            {"folder": "Üñí/cødé", "name": "Émoji🎉.swift",
             "decls": [], "refs": [], "ref_owners": []},
        ],
        "file_edges": [dict(e) for e in fixtures.FILE_EDGES],
        "type_edges": [dict(e) for e in fixtures.TYPE_EDGES] + [
            {"src": "Foo\tA/B", "dst": "Bar\tC/D", "w": 3,
             "symbols": [], "src_file": "A/B/F.swift", "dst_file": "C/D/B.swift"},
        ],
        "plan": [{"step": 1, "folders": ["Core", "Util"]}],
        "root_label": "Toy",
    }


def _js_decode(encoded: dict) -> dict:
    """Run graph_logic.js `decodePayload` under Node on the encoded payload."""
    script = (
        "const {decodePayload} = require(" + json.dumps(str(_GRAPH_LOGIC)) + ");"
        "let raw='';process.stdin.on('data',d=>raw+=d).on('end',()=>{"
        "process.stdout.write(JSON.stringify(decodePayload(JSON.parse(raw))));});"
    )
    proc = subprocess.run(
        [_NODE, "-e", script],
        input=json.dumps(encoded),
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


@unittest.skipIf(_NODE is None, "node not on PATH; skipping payload parity")
class PayloadParityTest(unittest.TestCase):
    def test_js_decode_inverts_python_encode(self):
        original = json.loads(json.dumps(_payload()))   # normalize once
        decoded = _js_decode(render._intern_payload(_payload()))
        self.assertEqual(decoded, original)

    def test_js_decode_passes_uninterned_payload_through(self):
        # A payload without a strings table (older renderer) must be untouched.
        original = json.loads(json.dumps(_payload()))
        self.assertEqual(_js_decode(_payload()), original)


if __name__ == "__main__":
    unittest.main()
