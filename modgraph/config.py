"""Shared constants: default paths, skip-lists, and the regex-scanner patterns.

Paths resolve relative to the repository root (the package's parent dir) so the
defaults land next to the tool exactly as they did when everything lived in a
single monolithic script.
"""
from __future__ import annotations

import re
from pathlib import Path

# Repo root = the directory that contains the modgraph/ package. Generated
# artifacts (HTML graph, migration plan, exclusion file) default to here.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "dependency_graph.html"
DEFAULT_EXCLUDED = REPO_ROOT / ".modularization_excluded.json"
# Append-only build-cost history (one row per real change, keyed to the target
# project's git commit). Deliberately NOT wiped by `just clean` — it tracks
# improvement across extractions. Powers Build mode's "Improvements" tab.
DEFAULT_HISTORY = REPO_ROOT / "build_history.jsonl"

# Generic skip-list: applies to ANY project. Names matched against directory
# basename. Patterns matched against the relative-path-from-root via fnmatch.
DEFAULT_SKIP_NAMES = {
    "DerivedData", "build", "Build", "dist", "out",
    "Pods", "Carthage", "node_modules", "vendor",
    "__pycache__", "venv", "env",
    # Xcode/SPM cache locations inside checked-out source trees.
    "SourcePackages", "checkouts",
}
# Skipped unless --include-tests is passed.
TEST_DIR_NAMES = {"Tests", "Test", "UITests", "SnapshotTests", "tests"}
# Xcode/Resource bundles — never useful as source.
EXT_SKIPS = {".xcodeproj", ".xcworkspace", ".bundle", ".framework", ".app"}

# Quick-win engine tunables (modgraph/quick_wins.py; decisions D1/D2 of the
# 2026-06-10 study). Absorption into an existing module is the default outcome
# but only while it stays simple enough to review:
ABSORB_MAX_FILES = 30    # bigger folders deserve their own module
ABSORB_MAX_PUBLIC = 20   # types forced public — beyond this the API is its own design job
# Cut-edge classification: "move these files" only stays actionable for a
# couple of files; a type counts as a shared primitive when this many folders
# consume it.
MOVE_FILE_MAX = 2
SHARED_MIN_CONSUMERS = 2
# Level-aware absorb predicates (study 2026-06-10, level-aware quick wins):
# a destination is vetoed when absorbing would raise its build level (layer
# inversion — everything stacked on it pays), and hot folders must not land in
# widely-depended-on modules (every consumer pays the churn on warm rebuilds).
# Both are hard rejects; the reasons ship with the item so a reviewer can
# override deliberately.
ABSORB_CHURN_HOT = 5     # commits touching the folder in the churn window
ABSORB_WARM_WIDE = 3     # modules transitively depending on the destination
ABSORB_REJECTED_MAX = 4  # rejected-destination explanations kept per item
# Misplaced-file detector (modgraph/file_affinity.py): suggest moving a file
# only when one foreign folder holds at least MIN_REFS of its reference mass
# AND dominates every alternative (own folder included) by DOMINANCE× —
# coordinators talking to many folders fail dominance and stay put.
FILE_AFFINITY_MIN_REFS = 3
FILE_AFFINITY_DOMINANCE = 2

# Type-isolation engine (modgraph/isolate.py): within ONE module, the
# single most-referenced declared type is the highest-leverage thing to pull
# into its own sub-module — every outside consumer then depends on the small
# new module instead of the whole parent. A type qualifies as an isolation
# seed only when at least this many references reach it from *outside* the
# module (below that, extracting it frees nobody); the analysis keeps the top
# N seeds per module and shows a sample of the types each one drags along.
ISOLATE_MIN_EXT_REFS = 3
ISOLATE_TOP_N = 10
ISOLATE_PULL_SAMPLE = 8

DECL_RE = re.compile(
    r"\b(?:class|struct|enum|protocol|actor|typealias)\s+([A-Z][A-Za-z0-9_]*)"
)
REF_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\b")
LINE_COMMENT_RE = re.compile(r"//.*?$", re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
