"""Cycle analysis for SCC bundles: feedback-arc-set + extraction targets."""
from __future__ import annotations

from collections import defaultdict

from .graph import _tarjan_sccs


def _feedback_arc_set(
    members: set[str], internal: dict[tuple[str, str], int]
) -> list[tuple[str, str, int]]:
    """Eades-Lin-Smyth greedy FAS approximation, weight-aware.

    Linearize nodes: repeatedly peel sinks (no outgoing among the remaining
    nodes) onto the right end, sources (no incoming) onto the left end, otherwise
    pick the node maximizing (weighted_out - weighted_in) onto the left. Edges
    that point right-to-left in the resulting ordering form the feedback arc set.

    Sinks are *prepended* to the right side (the classic Eades-Lin-Smyth `s2`):
    the first sink peeled is the global sink and must end up rightmost. We collect
    them by appending and reverse once at the end. Getting this wrong yields a
    non-empty FAS even for an acyclic graph (where the correct answer is empty).
    Sink/source batches are sorted so the linearization is deterministic.
    """
    out_w: dict[str, int] = defaultdict(int)
    in_w: dict[str, int] = defaultdict(int)
    out_adj: dict[str, set[str]] = defaultdict(set)
    in_adj: dict[str, set[str]] = defaultdict(set)
    for (a, b), w in internal.items():
        out_w[a] += w
        in_w[b] += w
        out_adj[a].add(b)
        in_adj[b].add(a)

    alive = set(members)
    left: list[str] = []
    right: list[str] = []
    while alive:
        # Drain sinks (no outgoing among alive nodes) onto the right side.
        while True:
            sinks = sorted(n for n in alive if not (out_adj[n] & alive))
            if not sinks:
                break
            for s in sinks:
                right.append(s)
                alive.discard(s)
        if not alive:
            break
        # Drain sources (no incoming among alive nodes) onto the left side.
        while True:
            sources = sorted(n for n in alive if not (in_adj[n] & alive))
            if not sources:
                break
            for s in sources:
                left.append(s)
                alive.discard(s)
        if not alive:
            break
        # Pick node with max (out_w_alive - in_w_alive); tie-break alphabetical.
        def delta_fast(n: str) -> int:
            o = sum(internal.get((n, b), 0) for b in out_adj[n] if b in alive)
            i = sum(internal.get((a, n), 0) for a in in_adj[n] if a in alive)
            return o - i
        pick = max(alive, key=lambda n: (delta_fast(n), n))
        left.append(pick)
        alive.discard(pick)

    # Sinks were appended in peel order; reverse so the first-peeled (global)
    # sink sits rightmost — i.e. they are effectively prepended to `right`.
    order = left + right[::-1]
    pos = {n: i for i, n in enumerate(order)}
    fas: list[tuple[str, str, int]] = []
    for (a, b), w in internal.items():
        if pos[a] >= pos[b]:
            fas.append((a, b, w))
    fas.sort(key=lambda t: (t[2], t[0], t[1]))
    return fas


def compute_cycle_breakers(
    scc_folders: list[str], leaf_edges: dict, pair_types: dict, max_steps: int = 30
) -> dict:
    """For a cycle SCC, propose a feedback-arc-set refactor sequence (global view).

    Use the FAS to estimate the total surgery needed and pick the lightest edges
    first. The simulation walks the FAS until the cycle is fully broken or until
    max_steps is reached (whichever comes first). This list is informational —
    the per-folder "extraction targets" computed separately are usually more
    actionable for individual PRs.
    """
    members = set(scc_folders)
    internal = {
        (a, b): w for (a, b), w in leaf_edges.items()
        if a in members and b in members and a != b
    }
    fas = _feedback_arc_set(members, internal)
    removed: list[dict] = []
    remaining = dict(internal)
    for (a, b, w) in fas[:max_steps]:
        types_here = sorted(pair_types.get((a, b), []))
        del remaining[(a, b)]
        deps_after: dict[str, set[str]] = defaultdict(set)
        for (x, y) in remaining:
            deps_after[x].add(y)
        sccs_after = _tarjan_sccs(members, deps_after)
        remaining_cycles = [c for c in sccs_after if len(c) > 1]
        biggest = max((len(c) for c in remaining_cycles), default=0)
        removed.append({
            "src": a, "dst": b, "refs": w, "types": types_here,
            "remaining_cycles": len(remaining_cycles), "biggest_cycle": biggest,
        })
        if not remaining_cycles:
            break
    return {
        "total_fas_estimate": len(fas),
        "edges": removed,
    }


def compute_extraction_targets(
    scc_folders: list[str], leaf_edges: dict, pair_types: dict, top_n: int = 30
) -> list[dict]:
    """For a cycle SCC, rank each member by how easy it would be to extract
    individually (i.e., refactor its outgoing references to other cycle members
    so it no longer participates in the cycle).

    Each target lists the internal-outgoing edges with ref counts and the
    specific types that need to be abstracted/moved. Sorted lightest first —
    these are the smallest PR-sized refactors that yield one full extraction.
    """
    members = set(scc_folders)
    by_folder: dict[str, list[tuple[str, int]]] = {f: [] for f in members}
    in_count: dict[str, int] = defaultdict(int)
    for (a, b), w in leaf_edges.items():
        if a in members and b in members and a != b:
            by_folder.setdefault(a, []).append((b, w))
            in_count[b] += 1
    targets: list[dict] = []
    for f, outs in by_folder.items():
        outs_sorted = sorted(outs, key=lambda x: (x[1], x[0]))
        total_refs = sum(w for _, w in outs)
        edges = [
            {
                "dst": b,
                "refs": w,
                "types": sorted(pair_types.get((f, b), []))[:8],
            }
            for b, w in outs_sorted
        ]
        targets.append({
            "folder": f,
            "outgoing_count": len(outs),
            "outgoing_refs": total_refs,
            "incoming_count": in_count.get(f, 0),
            "edges": edges,
        })
    # Lightest first: smallest total refs to refactor, then smallest edge count,
    # then alphabetical.
    targets.sort(key=lambda t: (t["outgoing_refs"], t["outgoing_count"], t["folder"]))
    return targets[:top_n]
