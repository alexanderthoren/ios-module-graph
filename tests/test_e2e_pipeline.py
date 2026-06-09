"""End-to-end smoke test of the whole pipeline on a real (tiny) Swift package:

    swiftc/swift build  →  compiler index store
            │
    index_graph (Swift) reads it, resolves by USR  →  index_graph.json
            │
    modgraph (Python) builds the graph + plan       →  HTML + markdown

Every other test stubs one stage; this one wires them together so a contract
break between the Swift producer and the Python consumer (the thing schema
versioning guards) is actually exercised against the real binary.

It is **opt-in and gated** so it never slows the normal suite:
  - `MODGRAPH_E2E=1` must be set (the CI macOS job sets it);
  - host must be macOS with `swift` on PATH (the index store needs the Apple
    toolchain);
  - the index_graph release binary must already be built
    (`cd index_graph && swift build -c release`).
Otherwise the case is skipped, not failed.

The temp project is created under $HOME, not the default /var/folders tempdir:
on macOS the latter resolves through the /private symlink, which `index_graph`'s
`standardizingPath` rewrites to /var — so first-party path matching would drop
every file. $HOME has no such symlink. (Documenting the gotcha here so nobody
"fixes" it back to tempfile.mkdtemp().)
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from modgraph import graph

_REPO_ROOT = Path(graph.__file__).resolve().parent.parent
_READER = _REPO_ROOT / "index_graph" / ".build" / "release" / "index_graph"

_PACKAGE_SWIFT = """\
// swift-tools-version:5.9
import PackageDescription
let package = Package(name: "Toy", targets: [
  .target(name: "Core"),
  .target(name: "Feature", dependencies: ["Core"]),
])
"""
_CORE = "public struct CoreType { public init() {} ; public func ping() {} }\n"
_FEATURE = "import Core\nstruct FeatureType { let c = CoreType(); func use() { c.ping() } }\n"


def _gate_reason() -> str | None:
    if os.environ.get("MODGRAPH_E2E") != "1":
        return "set MODGRAPH_E2E=1 to run the end-to-end pipeline test"
    if platform.system() != "Darwin":
        return "index store requires the macOS/Apple Swift toolchain"
    if shutil.which("swift") is None:
        return "swift not on PATH"
    if not _READER.exists():
        return f"reader not built: {_READER} (cd index_graph && swift build -c release)"
    return None


@unittest.skipIf(_gate_reason() is not None, _gate_reason() or "")
class EndToEndPipelineTest(unittest.TestCase):
    def setUp(self):
        # $HOME base (not /var/folders) — see module docstring.
        self.proj = Path(tempfile.mkdtemp(prefix=".modgraph_e2e_", dir=Path.home())).resolve()
        self.addCleanup(shutil.rmtree, self.proj, ignore_errors=True)
        (self.proj / "Sources" / "Core").mkdir(parents=True)
        (self.proj / "Sources" / "Feature").mkdir(parents=True)
        (self.proj / "Package.swift").write_text(_PACKAGE_SWIFT, encoding="utf-8")
        (self.proj / "Sources" / "Core" / "CoreType.swift").write_text(_CORE, encoding="utf-8")
        (self.proj / "Sources" / "Feature" / "FeatureType.swift").write_text(_FEATURE, encoding="utf-8")
        # Populate the index store. The build may exit non-zero (link step) — the
        # store is written before linking, exactly as the real pipeline tolerates.
        subprocess.run(["swift", "build"], cwd=self.proj,
                       capture_output=True, text=True, timeout=300)
        stores = list((self.proj / ".build").rglob("index/store"))
        self.assertTrue(stores, "no index store produced by swift build")
        self.store = stores[0]

    def _run_reader(self) -> dict:
        out = self.proj / "index_graph.json"
        subprocess.run([str(_READER), str(self.store), str(self.proj), str(out)],
                       capture_output=True, text=True, check=True, timeout=120)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_reader_resolves_the_real_dependency_edge(self):
        g = self._run_reader()
        # The schema-versioned contract the Python loader validates.
        self.assertEqual(g["schema_version"], 1)
        decls = g["folder_decls"]
        self.assertIn("Sources/Core", decls)
        self.assertIn("Sources/Feature", decls)
        self.assertIn("CoreType", decls["Sources/Core"])
        # Feature uses CoreType → exactly one edge, Feature → Core, resolved by USR.
        self.assertEqual(
            g["edges"], [{"src": "Sources/Feature", "dst": "Sources/Core", "w": 1}])

    def test_modgraph_renders_outputs_from_the_index(self):
        idx = self.proj / "index_graph.json"
        self._run_reader()  # writes idx
        html = self.proj / "graph.html"
        plan = self.proj / "plan.md"
        hist = self.proj / "build_history.jsonl"  # keep out of the repo root
        proc = subprocess.run(
            [sys.executable, "-m", "modgraph", str(self.proj),
             "--from-index", str(idx), "--graph", str(html),
             "--list", str(plan), "--history", str(hist),
             # The toy is itself an SPM package; without this its Sources/ would
             # be auto-detected as already-migrated, leaving an empty plan.
             "--no-auto-detect-spm"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(html.exists() and html.stat().st_size > 0)
        self.assertTrue(plan.exists() and plan.stat().st_size > 0)
        # The migration plan must mention migrating Core before Feature.
        text = plan.read_text(encoding="utf-8")
        self.assertIn("Core", text)


if __name__ == "__main__":
    unittest.main()
