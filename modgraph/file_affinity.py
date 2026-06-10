"""Misplaced-file detector: files whose references mostly bind elsewhere.

A folder often looks unextractable because of two or three files that
*belong* in another folder — they drag a whole dependency edge with them.
Moving just those files is the smallest possible modularization PR: it
dissolves fake coupling, shrinks cut-sets and public surfaces, and sometimes
deletes a folder cycle before any planning happens (study 2026-06-10,
Proposal D).

The evidence is ``file_edges`` (USR-resolved file-to-file couplings from the
index path). Per file, reference mass is summed per counterpart folder, both
directions. A move is suggested when one *foreign* folder's mass clears
``FILE_AFFINITY_MIN_REFS`` and dominates every alternative (the file's own
folder included) by ``FILE_AFFINITY_DOMINANCE``×. Coordinator-style files
that talk to many folders fail the dominance test and are left alone.

Advisory by design — reference affinity is necessary, not sufficient, for a
file to belong elsewhere; a human confirms. Deterministic (sorted iteration
only). Empty on the regex-scan path (no ``file_edges``).
"""
from __future__ import annotations

from collections import defaultdict

from .config import FILE_AFFINITY_DOMINANCE, FILE_AFFINITY_MIN_REFS


def _folder_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."


def compute_file_moves(file_edges: list[dict],
                       source_folders: set[str] | None = None) -> dict:
    """Return ``{"items": [...], "summary": {...}}`` suggested file moves.

    ``source_folders`` (optional) restricts suggestions to files living in (and
    moving to) in-scope folders — migrated/excluded code is not advised on.
    Each item: ``{file, from, to, refs, runner_up_refs, own_refs, symbols}``,
    sorted by refs desc (biggest fake coupling first).
    """
    # Per file: counterpart folder -> mass, plus the symbols carried.
    mass: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    symbols: dict[tuple[str, str], set[str]] = defaultdict(set)
    for fe in file_edges or []:
        src, dst, w = fe["src"], fe["dst"], fe.get("w", 1)
        sf, df = _folder_of(src), _folder_of(dst)
        mass[src][df] += w
        mass[dst][sf] += w
        for s in fe.get("symbols", ()):
            symbols[(src, df)].add(s)
            symbols[(dst, sf)].add(s)

    items: list[dict] = []
    for path in sorted(mass):
        home = _folder_of(path)
        if source_folders is not None and home not in source_folders:
            continue
        by_folder = mass[path]
        own = by_folder.get(home, 0)
        foreign = sorted(
            ((f, w) for f, w in by_folder.items() if f != home),
            key=lambda t: (-t[1], t[0]),
        )
        if source_folders is not None:
            foreign = [(f, w) for f, w in foreign if f in source_folders]
        if not foreign:
            continue
        best_folder, best = foreign[0]
        runner_up = max(own, foreign[1][1] if len(foreign) > 1 else 0)
        if best < FILE_AFFINITY_MIN_REFS or best < FILE_AFFINITY_DOMINANCE * max(runner_up, 1):
            continue
        items.append({
            "file": path,
            "from": home,
            "to": best_folder,
            "refs": best,
            "runner_up_refs": runner_up,
            "own_refs": own,
            "symbols": sorted(symbols.get((path, best_folder), ()))[:8],
        })

    items.sort(key=lambda i: (-i["refs"], i["file"]))
    return {
        "items": items,
        "summary": {
            "files_scanned": len(mass),
            "suggested_moves": len(items),
        },
    }
