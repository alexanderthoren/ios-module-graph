"""Git churn for Build mode: how often each part of the code actually changes.

Structural metrics (blast radius, critical path) say what a change *would*
cost; churn says how often you *actually pay* it. A foundational module nobody
touches is cheap to leave alone no matter how many dependents it has — a hot
module with the same blast radius hurts every week. Build mode multiplies the
two so the Split-plan ranking surfaces what actually hurts.

Capture (``compute_churn``) shells one ``git log --name-only`` over the target
repo (best-effort, like ``history``/``staleness``: no git → no churn, never
fatal). Interpretation is pure and unit-tested: ``commit_folder_sets`` parses
the log into one folder-set per commit, ``churn_by_module`` counts, per build
unit, the commits that touched it (a commit touching five files of one module
counts once — commits, not file-events).
"""
from __future__ import annotations

import posixpath
import subprocess

# One year of history: long enough to smooth release cycles, short enough that
# long-dead hotspots age out.
CHURN_DAYS = 365


def commit_folder_sets(log_text: str) -> list[set[str]]:
    """Parse ``git log --name-only --pretty=format:`` output into one set of
    touched folders per commit.

    Commits are separated by blank lines; each non-blank line is a path. Only
    ``.swift`` paths count (churn weights *compile* cost — assets/docs don't
    recompile anything). A root-level file maps to folder ``"."``. Commits
    touching no Swift files yield no set.
    """
    commits: list[set[str]] = []
    current: set[str] = set()
    for line in log_text.splitlines():
        line = line.strip()
        if not line:
            if current:
                commits.append(current)
                current = set()
            continue
        if line.endswith(".swift"):
            current.add(posixpath.dirname(line) or ".")
    if current:
        commits.append(current)
    return commits


def churn_by_module(commit_sets: list[set[str]], module_of_fn) -> dict[str, int]:
    """Count, per build unit, the commits that touched it.

    ``module_of_fn`` maps a folder id to its module (the caller binds
    :func:`modgraph.module_graph.module_of` to its prefixes). A commit counts
    once per module no matter how many of its folders it touched.
    """
    counts: dict[str, int] = {}
    for folders in commit_sets:
        for m in {module_of_fn(f) for f in folders}:
            counts[m] = counts.get(m, 0) + 1
    return counts


def churn_by_folder(commit_sets: list[set[str]]) -> dict[str, int]:
    """Count, per folder, the commits that touched it (once per commit).

    The folder-granularity sibling of :func:`churn_by_module`, for scoring
    individual migration steps before they belong to any build unit.
    """
    counts: dict[str, int] = {}
    for folders in commit_sets:
        for f in folders:
            counts[f] = counts.get(f, 0) + 1
    return counts


def compute_churn(project_dir, days: int = CHURN_DAYS) -> list[set[str]]:
    """Folder-sets of every commit in the window — ``[]`` on any git failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_dir), "log", f"--since={days}.days",
             "--name-only", "--pretty=format:", "--no-renames"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return commit_folder_sets(out.stdout)
