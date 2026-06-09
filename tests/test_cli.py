"""Tests for modgraph.cli: argument parsing and the main orchestration flow.

Follows the style of tests/test_graph.py: one behaviour per method, concrete
assertions, inputs built from tests.fixtures. All filesystem output is confined
to a tempfile.TemporaryDirectory so the repo's real artifacts are never touched.
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from modgraph import cli
from modgraph.config import DEFAULT_OUT, REPO_ROOT
from tests import fixtures


class ParseArgsTest(unittest.TestCase):
    """parse_args reads sys.argv; patch it with mock.patch.object."""

    def _parse(self, argv):
        # argv[0] is the program name; the rest are real CLI tokens.
        with mock.patch.object(cli.sys, "argv", ["modgraph", *argv]):
            return cli.parse_args()

    def test_defaults_when_only_project_root(self):
        ns = self._parse(["/some/project"])
        self.assertEqual(ns.project_root, Path("/some/project"))
        self.assertIsNone(ns.graph)
        self.assertIsNone(ns.list)
        self.assertEqual(ns.ext, ".swift")
        self.assertEqual(ns.list_format, "markdown")

    def test_default_collections_are_empty_lists(self):
        ns = self._parse(["/some/project"])
        self.assertEqual(ns.ignore, [])
        self.assertEqual(ns.migrated_prefix, [])

    def test_include_tests_defaults_false(self):
        ns = self._parse(["/some/project"])
        self.assertFalse(ns.include_tests)

    def test_include_tests_store_true(self):
        ns = self._parse(["/some/project", "--include-tests"])
        self.assertTrue(ns.include_tests)

    def test_no_auto_detect_spm_store_true(self):
        ns = self._parse(["/some/project", "--no-auto-detect-spm"])
        self.assertTrue(ns.no_auto_detect_spm)

    def test_from_index_sets_a_path(self):
        ns = self._parse(["/some/project", "--from-index", "/tmp/index_graph.json"])
        self.assertEqual(ns.from_index, Path("/tmp/index_graph.json"))
        self.assertIsInstance(ns.from_index, Path)

    def test_from_index_default_none(self):
        ns = self._parse(["/some/project"])
        self.assertIsNone(ns.from_index)

    def test_graph_with_explicit_value(self):
        ns = self._parse(["/some/project", "--graph", "/tmp/out.html"])
        self.assertEqual(ns.graph, Path("/tmp/out.html"))

    def test_graph_without_value_uses_const_default(self):
        # nargs="?" with const=DEFAULT_OUT: bare --graph -> DEFAULT_OUT.
        ns = self._parse(["/some/project", "--graph"])
        self.assertEqual(ns.graph, DEFAULT_OUT)

    def test_list_without_value_uses_const_default(self):
        ns = self._parse(["/some/project", "--list"])
        self.assertEqual(ns.list, REPO_ROOT / "migration_plan.md")

    def test_list_with_explicit_value(self):
        ns = self._parse(["/some/project", "--list", "/tmp/plan.md"])
        self.assertEqual(ns.list, Path("/tmp/plan.md"))

    def test_list_format_json_choice(self):
        ns = self._parse(["/some/project", "--list-format", "json"])
        self.assertEqual(ns.list_format, "json")

    def test_out_alias_sets_path(self):
        ns = self._parse(["/some/project", "--out", "/tmp/alias.html"])
        self.assertEqual(ns.out, Path("/tmp/alias.html"))
        # --out defaults to None when absent so main can tell it apart.
        self.assertIsNone(self._parse(["/some/project"]).out)

    def test_ext_normalisation_is_not_done_by_parse_args(self):
        # parse_args stores the raw value; the leading-dot fix happens in main.
        ns = self._parse(["/some/project", "--ext", "kt"])
        self.assertEqual(ns.ext, "kt")

    def test_ignore_is_repeatable(self):
        ns = self._parse(["/some/project", "--ignore", "foo", "--ignore", "bar"])
        self.assertEqual(ns.ignore, ["foo", "bar"])

    def test_migrated_prefix_is_repeatable(self):
        ns = self._parse([
            "/some/project",
            "--migrated-prefix", "Packages",
            "--migrated-prefix", "Modules",
        ])
        self.assertEqual(ns.migrated_prefix, ["Packages", "Modules"])

    def test_label_default_none(self):
        ns = self._parse(["/some/project"])
        self.assertIsNone(ns.label)

    def test_project_root_is_required(self):
        # Missing positional -> argparse exits with code 2.
        with mock.patch.object(cli.sys, "argv", ["modgraph"]):
            with self.assertRaises(SystemExit) as ctx:
                cli.parse_args()
        self.assertEqual(ctx.exception.code, 2)


class MainTest(unittest.TestCase):
    """main reads sys.argv; outputs are always forced into a temp dir."""

    def setUp(self):
        # main() auto-appends a build-cost snapshot to --history (default lands in
        # the repo root). Redirect it into a per-test temp file so the suite never
        # writes under the repo, per the project's test rules.
        self._hist_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._hist_dir.cleanup)
        self._history = str(Path(self._hist_dir.name) / "build_history.jsonl")

    def _run(self, argv):
        if "--history" not in argv:
            argv = [*argv, "--history", self._history]
        with mock.patch.object(cli.sys, "argv", ["modgraph", *argv]):
            return cli.main()

    def test_scan_path_writes_graph_and_list(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = tmp / "proj"
            project.mkdir()
            fixtures.make_swift_tree(project)
            graph_out = tmp / "out.html"
            list_out = tmp / "plan.md"
            rc = self._run([
                str(project),
                "--graph", str(graph_out),
                "--list", str(list_out),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(graph_out.exists())
            self.assertTrue(list_out.exists())
            self.assertGreater(graph_out.stat().st_size, 0)
            self.assertGreater(list_out.stat().st_size, 0)

    def test_from_index_path_writes_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = tmp / "proj"
            project.mkdir()  # project_root must be an existing dir.
            index_json = fixtures.write_index_json(tmp)
            graph_out = tmp / "out.html"
            list_out = tmp / "plan.md"
            rc = self._run([
                str(project),
                "--from-index", str(index_json),
                "--graph", str(graph_out),
                "--list", str(list_out),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(graph_out.exists())
            self.assertTrue(list_out.exists())

    def test_from_index_with_json_list_format(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = tmp / "proj"
            project.mkdir()
            index_json = fixtures.write_index_json(tmp)
            list_out = tmp / "plan.json"
            rc = self._run([
                str(project),
                "--from-index", str(index_json),
                "--list", str(list_out),
                "--list-format", "json",
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(list_out.exists())

    def test_out_alias_overrides_graph_target(self):
        # --out is the deprecated alias; main writes the graph to --out's path.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = tmp / "proj"
            project.mkdir()
            fixtures.make_swift_tree(project)
            alias_out = tmp / "alias.html"
            rc = self._run([str(project), "--out", str(alias_out)])
            self.assertEqual(rc, 0)
            self.assertTrue(alias_out.exists())

    def test_scan_path_warns_about_phantom_edges(self):
        # Running without --from-index must print a loud fallback banner so the
        # degraded (regex-scan) mode is impossible to miss.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = tmp / "proj"
            project.mkdir()
            fixtures.make_swift_tree(project)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = self._run([str(project), "--graph", str(tmp / "o.html")])
            self.assertEqual(rc, 0)
            self.assertIn("REGEX-SCANNER FALLBACK", err.getvalue())
            self.assertIn("phantom edges", err.getvalue())

    def test_from_index_path_does_not_warn(self):
        # The accurate path must stay quiet — no false alarm.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = tmp / "proj"
            project.mkdir()
            index_json = fixtures.write_index_json(tmp)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = self._run([
                    str(project), "--from-index", str(index_json),
                    "--graph", str(tmp / "o.html"),
                ])
            self.assertEqual(rc, 0)
            self.assertNotIn("REGEX-SCANNER FALLBACK", err.getvalue())

    def test_nonexistent_project_root_returns_1(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does_not_exist"
            graph_out = Path(td) / "out.html"
            rc = self._run([str(missing), "--graph", str(graph_out)])
            self.assertEqual(rc, 1)
            self.assertFalse(graph_out.exists())

    def test_empty_project_no_swift_files_returns_1(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            project = tmp / "empty"
            project.mkdir()
            graph_out = tmp / "out.html"
            list_out = tmp / "plan.md"
            rc = self._run([
                str(project),
                "--graph", str(graph_out),
                "--list", str(list_out),
            ])
            self.assertEqual(rc, 1)
            # Bails before writing any output.
            self.assertFalse(graph_out.exists())
            self.assertFalse(list_out.exists())


if __name__ == "__main__":
    unittest.main()
