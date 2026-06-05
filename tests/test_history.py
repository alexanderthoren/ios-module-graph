"""Tests for modgraph.history: snapshot shape, JSONL round-trip, dedup.

Follows tests/test_graph.py's shape: build inputs inline, call the function,
assert concrete values, one behaviour per test. All file IO goes through a
TemporaryDirectory — never the repo root. Git state is exercised indirectly:
project_dir points at a non-repo temp dir, so sha resolves to '' (best-effort).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from modgraph import history


def _module_graph(*, modules=3, cycles=0, warm=(2, 1, 0), est_wall=10.0,
                  total_build=40.0):
    """A minimal module_graph dict shaped like compute_module_graph's output."""
    nodes = []
    for i in range(modules):
        nodes.append({
            "id": f"m{i}",
            "kind": "app" if i == 0 else "spm",
            "types": 10 + i,
            "warm": warm[i] if i < len(warm) else 0,
        })
    return {
        "nodes": nodes,
        "edges": [{"from": "m1", "to": "m0", "w": 1}],
        "summary": {
            "n_cycles": cycles, "crit_len": 4, "max_width": 2,
            "est_wall_s": est_wall, "total_build_s": total_build,
            "cores": 8, "measured": True,
        },
    }


_PLAN = [{"is_cycle": False}, {"is_cycle": True}, {"is_cycle": False}]


class BuildSnapshotTest(unittest.TestCase):
    def test_extracts_structural_and_wall_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            snap = history.build_snapshot(
                _module_graph(), _PLAN, d, captured_at="2026-06-05T00:00:00+00:00")
        self.assertEqual(snap["structural"]["modules"], 3)
        self.assertEqual(snap["structural"]["spm"], 2)
        self.assertEqual(snap["structural"]["app"], 1)
        self.assertEqual(snap["structural"]["edges"], 1)
        self.assertEqual(snap["structural"]["warm_max"], 2)
        self.assertEqual(snap["structural"]["warm_total"], 3)
        self.assertEqual(snap["wall"]["est_wall_s"], 10.0)
        self.assertTrue(snap["wall"]["measured"])

    def test_plan_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            snap = history.build_snapshot(_module_graph(), _PLAN, d,
                                          captured_at="2026-06-05T00:00:00+00:00")
        self.assertEqual(snap["plan"]["steps"], 3)
        self.assertEqual(snap["plan"]["cyclic_bundles"], 1)

    def test_non_repo_dir_yields_empty_sha(self):
        # A plain temp dir is not a git repo → sha is '' (best-effort, not fatal).
        with tempfile.TemporaryDirectory() as d:
            snap = history.build_snapshot(_module_graph(), _PLAN, d,
                                          captured_at="2026-06-05T00:00:00+00:00")
        self.assertEqual(snap["sha"], "")
        self.assertFalse(snap["dirty"])


class AppendAndLoadTest(unittest.TestCase):
    def test_append_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "build_history.jsonl"
            written = history.append_snapshot(p, _module_graph(), _PLAN, d,
                                              captured_at="2026-06-05T00:00:00+00:00")
            self.assertIsNotNone(written)
            rows = history.load_history(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["structural"]["modules"], 3)

    def test_load_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            rows = history.load_history(Path(d) / "nope.jsonl")
        self.assertEqual(rows, [])

    def test_load_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "h.jsonl"
            p.write_text('{"sha":"a","structural":{"modules":1}}\n'
                         "not json\n"
                         "\n"
                         '{"sha":"b","structural":{"modules":2}}\n',
                         encoding="utf-8")
            rows = history.load_history(p)
        self.assertEqual([r["sha"] for r in rows], ["a", "b"])


class DedupTest(unittest.TestCase):
    def test_identical_state_is_deduped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "h.jsonl"
            first = history.append_snapshot(p, _module_graph(), _PLAN, d,
                                            captured_at="2026-06-05T00:00:00+00:00")
            # Same metrics, later timestamp — must NOT add a row.
            second = history.append_snapshot(p, _module_graph(), _PLAN, d,
                                             captured_at="2026-06-06T00:00:00+00:00")
            rows = history.load_history(p)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(rows), 1)

    def test_changed_state_appends(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "h.jsonl"
            history.append_snapshot(p, _module_graph(cycles=2), _PLAN, d,
                                    captured_at="2026-06-05T00:00:00+00:00")
            # Cycles dropped 2 → 0: a real improvement, must record a second row.
            history.append_snapshot(p, _module_graph(cycles=0), _PLAN, d,
                                    captured_at="2026-06-06T00:00:00+00:00")
            rows = history.load_history(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["structural"]["cycles"] for r in rows], [2, 0])

    def test_appended_line_is_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "h.jsonl"
            history.append_snapshot(p, _module_graph(), _PLAN, d,
                                    captured_at="2026-06-05T00:00:00+00:00")
            line = p.read_text(encoding="utf-8").strip()
        json.loads(line)  # raises if the appended row isn't valid JSON


if __name__ == "__main__":
    unittest.main()
