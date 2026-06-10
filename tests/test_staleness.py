"""Tests for modgraph.staleness: the cached-index staleness verdict.

Follows the exemplar (tests/test_graph.py): one behaviour per test, concrete
assertions. The decision function is pure; warn_if_stale takes an injectable
git_state so no test needs a real repository.
"""
from __future__ import annotations

import contextlib
import io
import unittest

from modgraph.staleness import classify_staleness, warn_if_stale
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


if __name__ == "__main__":
    unittest.main()
