"""Persisted \"won't be modularized\" folders + transitive blocked-by set."""
from __future__ import annotations

import json
from pathlib import Path


def compute_blocked_by_excluded(
    leaf_edges: dict, excluded: set[str], migrated: set[str]
) -> dict[str, set[str]]:
    """Folders that transitively depend on a won't-modularize folder.

    Reverse-BFS over leaf_edges starting from ``excluded``. Migrated nodes
    break the chain (SPM→SPM is fine — the new package can link against any
    already-migrated package), so they don't propagate. Returns a mapping
    ``blocked_folder -> {excluded_folder, …}`` so the markdown can name the
    blockers per folder.
    """
    rev: dict[str, set[str]] = {}
    for (src, dst) in leaf_edges:
        rev.setdefault(dst, set()).add(src)
    reasons: dict[str, set[str]] = {}
    frontier: list[tuple[str, str]] = [(e, e) for e in excluded]
    seen_edge: set[tuple[str, str]] = set(frontier)
    while frontier:
        nxt: list[tuple[str, str]] = []
        for node, root_excl in frontier:
            for pred in rev.get(node, ()):
                if pred in migrated:
                    continue
                if pred in excluded:
                    continue
                reasons.setdefault(pred, set()).add(root_excl)
                key = (pred, root_excl)
                if key in seen_edge:
                    continue
                seen_edge.add(key)
                nxt.append(key)
        frontier = nxt
    return reasons


def load_exclusions(path: Path) -> set[str]:
    """Read the persisted "won't be modularized" folder list.

    Format is a JSON array of folder paths (the HTML graph's Exclude button
    downloads exactly this shape); a ``{"excluded": [...]}`` wrapper is also
    tolerated. A missing or malformed file yields an empty set so a fresh
    checkout just works. Entries may be ancestor folders — expansion to the
    actual source-folder subtree happens in main().
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(raw, dict):
        raw = raw.get("excluded", [])
    if not isinstance(raw, list):
        return set()
    return {str(x).strip().rstrip("/") for x in raw if str(x).strip()}
