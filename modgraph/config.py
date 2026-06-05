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

DECL_RE = re.compile(
    r"\b(?:class|struct|enum|protocol|actor|typealias)\s+([A-Z][A-Za-z0-9_]*)"
)
REF_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\b")
LINE_COMMENT_RE = re.compile(r"//.*?$", re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
