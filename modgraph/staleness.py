"""Warn when the cached index_graph.json no longer matches the target repo.

``just tree``/``list``/``all`` reuse ``index_graph.json`` until ``just clean`` —
nothing else invalidates it, and the only hint is a one-line "reusing" note. The
Swift reader embeds the target repo's commit at index time
(``GraphData.target_commit``), so the renderer can compare that against the
repo's *current* state and warn loudly instead of silently analyzing an old
world.

The decision (:func:`classify_staleness`) is pure and covers the whole case
matrix; :func:`warn_if_stale` adds the git lookup + stderr banner. Best-effort
by design: no embedded commit (old JSON, non-git target) or no current git
state (git missing/broken) means *no verdict*, never a crash.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .history import _git_state

REBUILD_HINT = "Rebuild: `just clean` then re-run."

# Exit code the CLI returns when the cached graph is verifiably stale, so the
# justfile's `_prep` can branch on it (an incremental refresh). Every other
# outcome — fresh, unverifiable, unreadable JSON, bad args — exits 0 so a refresh
# only ever fires on a *definite* staleness verdict, never on uncertainty.
STALE_EXIT = 10


def classify_staleness(
    target_commit: dict | None, current_sha: str, current_dirty: bool
) -> str | None:
    """Return a human-readable staleness warning, or ``None`` when the cached
    graph is verifiably current — or nothing can be verified at all.

    ``target_commit`` is ``GraphData.target_commit`` (``{"sha", "dirty",
    "subject"}`` captured when the index was read); ``current_sha`` /
    ``current_dirty`` describe the target repo now. Empty values mean
    "unknown" and yield ``None`` — a missing git can't prove staleness.
    """
    if not target_commit or not target_commit.get("sha") or not current_sha:
        return None
    indexed_sha = target_commit["sha"]
    short, short_now = indexed_sha[:7], current_sha[:7]
    subject = target_commit.get("subject", "")

    if indexed_sha != current_sha:
        built = f"{short} (“{subject}”)" if subject else short
        return (
            f"index_graph.json was built at {built} but the project is now at "
            f"{short_now} — folders and edges may have changed since. {REBUILD_HINT}"
        )
    if target_commit.get("dirty"):
        return (
            f"index_graph.json was built from a DIRTY working tree at {short} — "
            f"it may include changes that were never committed, and the tree may "
            f"have changed since. {REBUILD_HINT}"
        )
    if current_dirty:
        return (
            f"the working tree has uncommitted changes, but index_graph.json was "
            f"built from clean {short} — the graph reflects the committed state "
            f"only. {REBUILD_HINT}"
        )
    return None


def warn_if_stale(target_commit, project_dir, *, git_state=_git_state) -> str | None:
    """Compare the embedded commit against *project_dir*'s current git state and
    print a prominent stderr banner when the cached graph looks stale.

    Returns the warning text (for callers/tests), or ``None`` when fresh or
    unverifiable. ``git_state`` is injectable so tests don't need a repo.
    """
    sha, _subject, dirty = git_state(project_dir)
    message = classify_staleness(target_commit, sha, dirty)
    if message is None:
        return None
    banner = (
        "\n"
        "  ┌────────────────────────────────────────────────────────────────┐\n"
        "  │  ⚠  STALE INDEX — the cached graph may not match the project.  │\n"
        "  └────────────────────────────────────────────────────────────────┘\n"
        f"  {message}\n"
    )
    print(banner, file=sys.stderr)
    return message


def _embedded_commit(json_path: Path) -> dict | None:
    """Read just ``target_commit`` out of a saved index_graph.json, tolerating
    anything (missing file, bad JSON, old schema) by returning ``None`` — the CLI
    must never crash a `just tree`; an unverifiable graph is simply reused."""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tc = data.get("target_commit")
    return tc if isinstance(tc, dict) else None


def main(argv=None) -> int:
    """`python3 -m modgraph.staleness <index_graph.json> <project_dir>`.

    Exits :data:`STALE_EXIT` when the cached graph is verifiably stale vs the
    target repo's current git state (so the justfile triggers an incremental
    refresh), and 0 for fresh / unverifiable / error — refresh only on a definite
    verdict. Prints a one-line reason to stderr for visibility.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python3 -m modgraph.staleness <index_graph.json> <project_dir>",
              file=sys.stderr)
        return 0  # never block a reuse on a usage slip
    json_path, project_dir = Path(argv[0]), argv[1]

    target_commit = _embedded_commit(json_path)
    sha, _subject, dirty = _git_state(project_dir)
    message = classify_staleness(target_commit, sha, dirty)
    if message is None:
        return 0
    print(f"stale: {message}", file=sys.stderr)
    return STALE_EXIT


if __name__ == "__main__":
    sys.exit(main())
