"""Tests for modgraph.staleness: the cached-index staleness verdict.

Follows the exemplar (tests/test_graph.py): one behaviour per test, concrete
assertions. The decision function is pure; warn_if_stale takes an injectable
git_state so no test needs a real repository.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from modgraph.staleness import STALE_EXIT, classify_staleness, main, warn_if_stale
from tests import fixtures

SHA = fixtures.TARGET_COMMIT["sha"]                    # what the index embeds
OTHER_SHA = "f" * 40                                   # where HEAD moved to


def _embedded(**overrides) -> dict:
    base = dict(fixtures.TARGET_COMMIT)                # clean by default
    base.update(overrides)
    return base


class ClassifyStalenessNoVerdictTest(unittest.TestCase):
    """Unknowns can't prove staleness — they must stay silent, not crash."""

    def test_none_embedded_returns_none(self):
        self.assertIsNone(classify_staleness(None, SHA, False))

    def test_empty_embedded_returns_none(self):
        self.assertIsNone(classify_staleness({}, SHA, False))

    def test_embedded_without_sha_returns_none(self):
        self.assertIsNone(classify_staleness({"dirty": True}, SHA, False))

    def test_no_current_sha_returns_none(self):
        # git unavailable at render time — even a would-be mismatch stays quiet.
        self.assertIsNone(classify_staleness(_embedded(), "", False))


class ClassifyStalenessFreshTest(unittest.TestCase):
    def test_same_sha_both_clean_returns_none(self):
        self.assertIsNone(classify_staleness(_embedded(), SHA, False))


class ClassifyStalenessStaleTest(unittest.TestCase):
    def test_sha_mismatch_warns(self):
        msg = classify_staleness(_embedded(), OTHER_SHA, False)
        self.assertIsNotNone(msg)
        self.assertIn(SHA[:7], msg)
        self.assertIn(OTHER_SHA[:7], msg)
        self.assertIn("Rebuild", msg)

    def test_sha_mismatch_includes_subject(self):
        msg = classify_staleness(_embedded(subject="feat: toy commit"), OTHER_SHA, False)
        self.assertIn("feat: toy commit", msg)

    def test_sha_mismatch_without_subject_still_warns(self):
        msg = classify_staleness(_embedded(subject=""), OTHER_SHA, False)
        self.assertIn(SHA[:7], msg)
        self.assertNotIn("“", msg)

    def test_dirty_at_index_time_warns(self):
        msg = classify_staleness(_embedded(dirty=True), SHA, False)
        self.assertIn("DIRTY", msg)
        self.assertIn(SHA[:7], msg)

    def test_dirty_now_on_clean_index_warns(self):
        msg = classify_staleness(_embedded(), SHA, True)
        self.assertIn("uncommitted changes", msg)
        self.assertIn(SHA[:7], msg)

    def test_sha_mismatch_outranks_dirty_flags(self):
        # Moved HEAD is the strongest signal — it wins over dirtiness.
        msg = classify_staleness(_embedded(dirty=True), OTHER_SHA, True)
        self.assertIn(OTHER_SHA[:7], msg)
        self.assertNotIn("DIRTY", msg)


class WarnIfStaleTest(unittest.TestCase):
    """The IO wrapper: banner on stderr when stale, silence when not."""

    @staticmethod
    def _git_state(sha: str, dirty: bool = False):
        return lambda project_dir: (sha, "irrelevant", dirty)

    def test_stale_prints_banner_and_returns_message(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            msg = warn_if_stale(_embedded(), "/proj", git_state=self._git_state(OTHER_SHA))
        self.assertIsNotNone(msg)
        self.assertIn("STALE INDEX", err.getvalue())
        self.assertIn(msg, err.getvalue())

    def test_fresh_prints_nothing_and_returns_none(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            msg = warn_if_stale(_embedded(), "/proj", git_state=self._git_state(SHA))
        self.assertIsNone(msg)
        self.assertEqual(err.getvalue(), "")

    def test_unverifiable_prints_nothing(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            msg = warn_if_stale(_embedded(), "/proj", git_state=self._git_state(""))
        self.assertIsNone(msg)
        self.assertEqual(err.getvalue(), "")


class MainCliTest(unittest.TestCase):
    """The `python3 -m modgraph.staleness` entry the justfile branches on:
    exit STALE_EXIT only on a definite stale verdict, 0 for everything else
    (fresh / unverifiable / unreadable / bad args) so a refresh never fires on
    uncertainty. _git_state is patched so no test needs a real repo."""

    def _write_graph(self, target_commit) -> str:
        d = tempfile.mkdtemp()
        path = Path(d) / "index_graph.json"
        path.write_text(json.dumps({"target_commit": target_commit}), encoding="utf-8")
        return str(path)

    def _run(self, json_path, git_state):
        err = io.StringIO()
        with mock.patch("modgraph.staleness._git_state", return_value=git_state), \
                contextlib.redirect_stderr(err):
            code = main([json_path, "/proj"])
        return code, err.getvalue()

    def test_moved_head_exits_stale(self):
        path = self._write_graph(_embedded())
        code, err = self._run(path, (OTHER_SHA, "subj", False))
        self.assertEqual(code, STALE_EXIT)
        self.assertIn("stale:", err)

    def test_dirty_tree_exits_stale(self):
        path = self._write_graph(_embedded())
        code, _ = self._run(path, (SHA, "subj", True))
        self.assertEqual(code, STALE_EXIT)

    def test_current_exits_zero(self):
        path = self._write_graph(_embedded())
        code, err = self._run(path, (SHA, "subj", False))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_missing_file_exits_zero(self):
        code, _ = self._run("/no/such/index_graph.json", (OTHER_SHA, "subj", False))
        self.assertEqual(code, 0)

    def test_no_embedded_commit_exits_zero(self):
        path = self._write_graph(None)
        code, _ = self._run(path, (OTHER_SHA, "subj", False))
        self.assertEqual(code, 0)

    def test_git_unavailable_exits_zero(self):
        # Even a would-be mismatch stays silent when HEAD can't be read.
        path = self._write_graph(_embedded())
        code, _ = self._run(path, ("", "", False))
        self.assertEqual(code, 0)

    def test_bad_args_exits_zero(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main([]), 0)
        self.assertIn("usage:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
