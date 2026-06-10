"""Tests for modgraph.churn — parse git history into per-module change counts.

The capture (compute_churn) shells out to git; everything testable is pure:
commit_folder_sets parses `git log --name-only --pretty=format:` text, and
churn_by_module counts commits per build unit. One behaviour per test.
"""
from __future__ import annotations

import unittest

from modgraph.churn import churn_by_folder, churn_by_module, commit_folder_sets

# Two commits: one touches Core (two files) + Feature, one touches only docs.
LOG = """\

Core/A.swift
Core/B.swift
Feature/F.swift

README.md
docs/guide.md

Root.swift
"""


class CommitFolderSetsTest(unittest.TestCase):
    def test_blank_lines_separate_commits(self):
        sets = commit_folder_sets(LOG)
        self.assertEqual(len(sets), 2)          # the docs-only commit vanishes

    def test_folders_deduped_within_a_commit(self):
        sets = commit_folder_sets(LOG)
        self.assertEqual(sets[0], {"Core", "Feature"})   # Core counted once

    def test_non_swift_paths_ignored(self):
        # README.md and docs/guide.md contribute nothing — only Swift folders
        # (and "." from the root-level Root.swift) survive.
        flat = set().union(*commit_folder_sets(LOG))
        self.assertEqual(flat, {"Core", "Feature", "."})

    def test_root_level_swift_maps_to_dot(self):
        sets = commit_folder_sets(LOG)
        self.assertEqual(sets[1], {"."})

    def test_empty_log_yields_no_commits(self):
        self.assertEqual(commit_folder_sets(""), [])
        self.assertEqual(commit_folder_sets("\n\n\n"), [])


class ChurnByModuleTest(unittest.TestCase):
    @staticmethod
    def _module_of(folder: str) -> str:
        return "CorePkg" if folder.startswith("Core") else "app"

    def test_counts_commits_once_per_module(self):
        # One commit touching two Core folders counts CorePkg once.
        sets = [{"Core", "Core/Sub"}, {"Core"}, {"Feature"}]
        counts = churn_by_module(sets, self._module_of)
        self.assertEqual(counts, {"CorePkg": 2, "app": 1})

    def test_one_commit_can_touch_several_modules(self):
        counts = churn_by_module([{"Core", "Feature"}], self._module_of)
        self.assertEqual(counts, {"CorePkg": 1, "app": 1})

    def test_no_commits_no_counts(self):
        self.assertEqual(churn_by_module([], self._module_of), {})


class ChurnByFolderTest(unittest.TestCase):
    def test_counts_commits_once_per_folder(self):
        sets = [{"Core"}, {"Core", "Util"}, {"Feature"}]
        counts = churn_by_folder(sets)
        self.assertEqual(counts, {"Core": 2, "Util": 1, "Feature": 1})

    def test_no_commits_no_counts(self):
        self.assertEqual(churn_by_folder([]), {})


if __name__ == "__main__":
    unittest.main()
