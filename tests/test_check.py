"""Tests for modgraph.check: the rule engine and the CI gate's exit codes.

Follows the exemplar (tests/test_graph.py): payloads from tests.fixtures
(mutated per case), one behaviour per test, file IO in temp dirs only.
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from modgraph import check as graph_check
from modgraph.index_loader import load_index_graph
from tests import fixtures


class ParseForbidTest(unittest.TestCase):
    def test_splits_and_strips(self):
        self.assertEqual(graph_check.parse_forbid("Features/* -> Legacy*"),
                         ("Features/*", "Legacy*"))

    def test_missing_arrow_raises(self):
        with self.assertRaises(ValueError):
            graph_check.parse_forbid("Features/Legacy")

    def test_empty_side_raises(self):
        with self.assertRaises(ValueError):
            graph_check.parse_forbid("Features/* -> ")

    def test_double_arrow_raises(self):
        with self.assertRaises(ValueError):
            graph_check.parse_forbid("A -> B -> C")


class CheckGraphTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _load(self, data: dict, name: str = "g"):
        sub = self.dir / name
        sub.mkdir(exist_ok=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):    # silence the loader's progress
            return load_index_graph(fixtures.write_index_json(sub, data))


class MaxCyclesRuleTest(CheckGraphTestBase):
    def test_within_limit_passes(self):
        gd = self._load(fixtures.sample_index_dict())
        self.assertEqual(graph_check.check_graph(gd, max_cycles=1), [])

    def test_over_limit_fails_with_cycle_members(self):
        gd = self._load(fixtures.sample_index_dict())   # has the Core ⇄ Util cycle
        violations = graph_check.check_graph(gd, max_cycles=0)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["rule"], "max-cycles 0")
        self.assertEqual(violations[0]["details"], [["Core", "Util"]])


class ForbidRuleTest(CheckGraphTestBase):
    def test_exact_match_fails_with_explaining_types(self):
        gd = self._load(fixtures.sample_index_dict())
        violations = graph_check.check_graph(gd, forbid=["App -> Core"])
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["details"], [
            {"src": "App", "dst": "Core", "w": 1, "types": ["CoreService"]},
        ])

    def test_glob_matches_every_declarer(self):
        gd = self._load(fixtures.sample_index_dict())
        violations = graph_check.check_graph(gd, forbid=["* -> Core"])
        matched = [(d["src"], d["dst"]) for d in violations[0]["details"]]
        self.assertEqual(matched,
                         [("App", "Core"), ("Feature", "Core"), ("Util", "Core")])

    def test_no_match_passes(self):
        gd = self._load(fixtures.sample_index_dict())
        self.assertEqual(graph_check.check_graph(gd, forbid=["Core -> App"]), [])


class RatchetRulesTest(CheckGraphTestBase):
    def _changed(self) -> dict:
        data = fixtures.sample_index_dict()
        data["edges"].append({"src": "Feature", "dst": "App", "w": 1})
        data["pair_types"].append(
            {"src": "Feature", "dst": "App", "types": ["AppCoordinator"]})
        return data

    def test_no_new_edges_passes_on_identical(self):
        old = self._load(fixtures.sample_index_dict(), "old")
        new = self._load(fixtures.sample_index_dict(), "new")
        self.assertEqual(graph_check.check_graph(new, old=old, no_new_edges=True), [])

    def test_no_new_edges_fails_on_added_edge(self):
        old = self._load(fixtures.sample_index_dict(), "old")
        new = self._load(self._changed(), "new")
        violations = graph_check.check_graph(new, old=old, no_new_edges=True)
        self.assertEqual(violations[0]["rule"], "no-new-edges")
        self.assertEqual(violations[0]["details"][0]["src"], "Feature")

    def test_no_new_cycles_fails_on_formed_cycle(self):
        old = self._load(fixtures.sample_index_dict(), "old")
        new = self._load(self._changed(), "new")   # Feature→App closes App ⇄ Feature
        violations = graph_check.check_graph(new, old=old, no_new_cycles=True)
        self.assertEqual(violations[0]["rule"], "no-new-cycles")
        self.assertEqual(violations[0]["details"], [["App", "Feature"]])

    def test_existing_cycle_is_tolerated_by_ratchet(self):
        # Core ⇄ Util exists in BOTH → not "new", no violation.
        old = self._load(fixtures.sample_index_dict(), "old")
        new = self._load(fixtures.sample_index_dict(), "new")
        self.assertEqual(graph_check.check_graph(new, old=old, no_new_cycles=True), [])


class ReportTest(CheckGraphTestBase):
    def test_clean_report(self):
        self.assertIn("✓ all checks passed", graph_check.format_report([]))

    def test_violation_report_lists_rule_and_edges(self):
        gd = self._load(fixtures.sample_index_dict())
        report = graph_check.format_report(
            graph_check.check_graph(gd, forbid=["App -> Core"]))
        self.assertIn("✗ 1 rule violation(s)", report)
        self.assertIn("## forbid App -> Core", report)
        self.assertIn("`App` → `Core` (w=1) — CoreService", report)

    def test_details_capped(self):
        details = [{"src": f"F{i:02d}", "dst": "X", "w": 1, "types": []}
                   for i in range(25)]
        report = graph_check.format_report(
            [{"rule": "r", "message": "m", "details": details}])
        self.assertIn("F19", report)
        self.assertNotIn("F20", report)
        self.assertIn("+5 more", report)


class CliTest(CheckGraphTestBase):
    def _write(self, data: dict, name: str) -> str:
        sub = self.dir / name
        sub.mkdir(exist_ok=True)
        return str(fixtures.write_index_json(sub, data))

    def _run(self, *argv) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = graph_check._main(list(argv))
        return rc, out.getvalue()

    def test_passing_rules_exit_zero(self):
        new = self._write(fixtures.sample_index_dict(), "new")
        rc, out = self._run(new, "--max-cycles", "1")
        self.assertEqual(rc, 0)
        self.assertIn("✓", out)

    def test_violations_exit_one(self):
        new = self._write(fixtures.sample_index_dict(), "new")
        rc, out = self._run(new, "--max-cycles", "0")
        self.assertEqual(rc, 1)
        self.assertIn("✗", out)

    def test_no_rules_is_usage_error(self):
        new = self._write(fixtures.sample_index_dict(), "new")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                graph_check._main([new])
        self.assertEqual(cm.exception.code, 2)

    def test_ratchet_without_baseline_is_usage_error(self):
        new = self._write(fixtures.sample_index_dict(), "new")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                graph_check._main([new, "--no-new-edges"])
        self.assertEqual(cm.exception.code, 2)

    def test_bad_forbid_pattern_exits_two(self):
        new = self._write(fixtures.sample_index_dict(), "new")
        rc, _ = self._run(new, "--forbid", "no-arrow-here")
        self.assertEqual(rc, 2)

    def test_ratchet_violation_via_cli(self):
        old = self._write(fixtures.sample_index_dict(), "old")
        changed = fixtures.sample_index_dict()
        changed["edges"].append({"src": "Feature", "dst": "App", "w": 1})
        new = self._write(changed, "new")
        rc, out = self._run(new, "--against", old, "--no-new-edges")
        self.assertEqual(rc, 1)
        self.assertIn("no-new-edges", out)


if __name__ == "__main__":
    unittest.main()
