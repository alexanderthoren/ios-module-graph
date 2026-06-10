"""Tests for modgraph.diff: the structural delta between two index graphs.

Follows the exemplar (tests/test_graph.py): payloads built from tests.fixtures
(mutated per case), concrete assertions, one behaviour per test, all file IO in
tempfile.TemporaryDirectory.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from modgraph import diff as graph_diff
from modgraph.index_loader import load_index_graph
from tests import fixtures


def _load(directory: Path, data: dict):
    return load_index_graph(fixtures.write_index_json(directory, data))


class DiffTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.old_dir = self.dir / "old"
        self.new_dir = self.dir / "new"
        self.old_dir.mkdir()
        self.new_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _diff(self, old_data: dict, new_data: dict) -> dict:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):   # silence the loader's progress
            old = _load(self.old_dir, old_data)
            new = _load(self.new_dir, new_data)
        return graph_diff.compute_graph_diff(old, new)


class IdenticalGraphsTest(DiffTestBase):
    def test_no_changes(self):
        d = self._diff(fixtures.sample_index_dict(), fixtures.sample_index_dict())
        self.assertFalse(graph_diff.has_changes(d))

    def test_side_summaries(self):
        d = self._diff(fixtures.sample_index_dict(), fixtures.sample_index_dict())
        self.assertEqual(d["old"], d["new"])
        self.assertEqual(d["old"]["folders"], 4)
        self.assertEqual(d["old"]["edges"], 5)
        self.assertEqual(d["old"]["cycles"], 1)        # Core ⇄ Util
        self.assertEqual(d["old"]["commit"], fixtures.TARGET_COMMIT)

    def test_markdown_says_no_changes(self):
        d = self._diff(fixtures.sample_index_dict(), fixtures.sample_index_dict())
        md = graph_diff.format_diff_markdown(d)
        self.assertIn("No structural changes.", md)
        self.assertIn("a1b2c3d", md)                   # commit short in header


class AddedEdgeTest(DiffTestBase):
    """A new Feature→App edge appears: reported with its types, forms a cycle."""

    def _new_data(self) -> dict:
        data = fixtures.sample_index_dict()
        data["edges"].append({"src": "Feature", "dst": "App", "w": 1})
        data["pair_types"].append(
            {"src": "Feature", "dst": "App", "types": ["AppCoordinator"]})
        return data

    def test_added_edge_with_explaining_types(self):
        d = self._diff(fixtures.sample_index_dict(), self._new_data())
        self.assertEqual(d["added_edges"], [
            {"src": "Feature", "dst": "App", "w": 1, "types": ["AppCoordinator"]},
        ])
        self.assertEqual(d["removed_edges"], [])

    def test_cycle_formed(self):
        d = self._diff(fixtures.sample_index_dict(), self._new_data())
        self.assertEqual(d["cycles_formed"], [["App", "Feature"]])
        self.assertEqual(d["cycles_broken"], [])

    def test_markdown_sections(self):
        d = self._diff(fixtures.sample_index_dict(), self._new_data())
        md = graph_diff.format_diff_markdown(d)
        self.assertIn("## Added edges (1)", md)
        self.assertIn("`Feature` → `App` (w=1) — AppCoordinator", md)
        self.assertIn("## Cycles formed (1)", md)
        self.assertIn("`App` ⇄ `Feature`", md)


class RemovedEdgeTest(DiffTestBase):
    """Dropping Util→Core: edge reported with the OLD side's types, cycle broken."""

    def _new_data(self) -> dict:
        data = fixtures.sample_index_dict()
        data["edges"] = [e for e in data["edges"]
                         if (e["src"], e["dst"]) != ("Util", "Core")]
        data["pair_types"] = [p for p in data["pair_types"]
                              if (p["src"], p["dst"]) != ("Util", "Core")]
        return data

    def test_removed_edge_with_old_types(self):
        d = self._diff(fixtures.sample_index_dict(), self._new_data())
        self.assertEqual(d["removed_edges"], [
            {"src": "Util", "dst": "Core", "w": 2, "types": ["CoreService"]},
        ])
        self.assertEqual(d["added_edges"], [])

    def test_cycle_broken(self):
        d = self._diff(fixtures.sample_index_dict(), self._new_data())
        self.assertEqual(d["cycles_broken"], [["Core", "Util"]])
        self.assertEqual(d["cycles_formed"], [])
        self.assertEqual(d["new"]["cycles"], 0)


class FolderChangeTest(DiffTestBase):
    def test_added_folder(self):
        data = fixtures.sample_index_dict()
        data["folder_decls"]["Fresh"] = ["FreshType"]
        data["files"].append({"folder": "Fresh", "name": "FreshType.swift",
                              "decls": ["FreshType"], "refs": []})
        data["type_owners"]["FreshType"] = ["Fresh"]
        d = self._diff(fixtures.sample_index_dict(), data)
        self.assertEqual(d["added_folders"], ["Fresh"])
        self.assertEqual(d["removed_folders"], [])

    def test_removed_folder(self):
        data = fixtures.sample_index_dict()
        del data["folder_decls"]["App"]
        data["files"] = [f for f in data["files"] if f["folder"] != "App"]
        data["edges"] = [e for e in data["edges"] if e["src"] != "App"]
        data["pair_types"] = [p for p in data["pair_types"] if p["src"] != "App"]
        del data["type_owners"]["AppCoordinator"]
        d = self._diff(fixtures.sample_index_dict(), data)
        self.assertEqual(d["removed_folders"], ["App"])


class TypeListCapTest(DiffTestBase):
    def test_markdown_caps_types_per_edge(self):
        many = [f"Type{i:02d}" for i in range(10)]
        data = fixtures.sample_index_dict()
        data["edges"].append({"src": "Feature", "dst": "Util", "w": 10})
        data["pair_types"].append({"src": "Feature", "dst": "Util", "types": many})
        d = self._diff(fixtures.sample_index_dict(), data)
        md = graph_diff.format_diff_markdown(d)
        self.assertIn("Type07", md)
        self.assertNotIn("Type08", md)
        self.assertIn("+2 more", md)


class CliTest(DiffTestBase):
    """_main: formats, exit codes, schema failures."""

    def _run(self, *argv) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = graph_diff._main(list(argv))
        return rc, out.getvalue()

    def _paths(self, old_data: dict, new_data: dict) -> tuple[str, str]:
        return (str(fixtures.write_index_json(self.old_dir, old_data)),
                str(fixtures.write_index_json(self.new_dir, new_data)))

    def test_identical_exit_zero_with_exit_code_flag(self):
        old, new = self._paths(fixtures.sample_index_dict(), fixtures.sample_index_dict())
        rc, _ = self._run(old, new, "--exit-code")
        self.assertEqual(rc, 0)

    def test_different_exit_one_with_exit_code_flag(self):
        changed = fixtures.sample_index_dict()
        changed["edges"].append({"src": "Feature", "dst": "App", "w": 1})
        old, new = self._paths(fixtures.sample_index_dict(), changed)
        rc, _ = self._run(old, new, "--exit-code")
        self.assertEqual(rc, 1)

    def test_different_exit_zero_without_flag(self):
        changed = fixtures.sample_index_dict()
        changed["edges"].append({"src": "Feature", "dst": "App", "w": 1})
        old, new = self._paths(fixtures.sample_index_dict(), changed)
        rc, _ = self._run(old, new)
        self.assertEqual(rc, 0)

    def test_json_format_parses(self):
        old, new = self._paths(fixtures.sample_index_dict(), fixtures.sample_index_dict())
        rc, out = self._run(old, new, "--format", "json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["added_edges"], [])

    def test_schema_mismatch_exits_two(self):
        bad = fixtures.sample_index_dict()
        bad["schema_version"] = 999
        old, new = self._paths(fixtures.sample_index_dict(), bad)
        rc, _ = self._run(old, new)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
