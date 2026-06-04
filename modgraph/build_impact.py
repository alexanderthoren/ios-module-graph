"""Build-impact metrics: warm-rebuild blast radius + cold-build critical path.

Structural model only (no git churn). An edge ``(a, b)`` means folder *a* depends
on folder *b*, so a change to *b* forces a **warm** rebuild of *a* and every
transitive dependent. The blast radius is therefore the size of *b*'s transitive
reverse-dependent set. This is a worst-case upper bound — Swift only cascades to
importers when a module's *public interface* changes, not on every edit — but it
is exactly the structural ceiling a modularization sets out to lower, so it is
the right thing to color the graph by.

Two lenses, both shipped for every tree node so the HTML can color any zoom
level:

* **warm** — ``warm`` = # of folders OUTSIDE this node's subtree that transitively
  depend on it (touch anything inside → they rebuild). ``fan_in`` = the direct
  slice of that. High warm = expensive to touch.
* **cold** — over the SCC-condensed DAG: ``level`` = build cohort (longest chain
  of dependencies beneath it; folders sharing a level can compile in parallel),
  ``crit`` = lies on a globally-longest dependency chain (these serialize the
  whole cold build). ``scc`` = size of the cyclic bundle it belongs to (>1 ⇒
  fused: always rebuilds together AND can't parallelize — worst for both lenses).

Aggregation onto an internal (non-leaf) tree node uses its descendant graph
nodes: warm = external dependents of the whole subtree, level/scc = the max over
descendants, crit = any descendant on the critical path.

Output is deterministic (only counts/bools/sorted lists reach the result),
matching the determinism contract in :mod:`modgraph.graph`.
"""
from __future__ import annotations

from collections import defaultdict, deque

from .graph import _tarjan_sccs


def compute_build_impact(tree: dict, leaf_edges: dict) -> dict:
    """Return ``{"nodes": {id: metrics}, "summary": {...}}`` (see module docstring).

    ``tree`` is the nested dict from :func:`modgraph.graph.build_tree`; ``leaf_edges``
    is the ``{(src, dst): weight}`` folder-dependency map (src depends on dst).
    """
    # Forward (depends-on) and reverse (depended-on-by) adjacency over the real
    # folder graph. Self-loops are ignored — a folder never rebuilds itself.
    fwd: dict[str, set[str]] = defaultdict(set)
    rev: dict[str, set[str]] = defaultdict(set)
    graph_nodes: set[str] = set()
    for (a, b) in leaf_edges:
        if a == b:
            continue
        fwd[a].add(b)
        rev[b].add(a)
        graph_nodes.add(a)
        graph_nodes.add(b)

    n_total = len(graph_nodes)

    # ── descendant graph-nodes per tree node (one post-order pass) ────────────
    # subtree[id] = every graph node at or below `id` in the folder tree.
    subtree: dict[str, set[str]] = {}

    def fill_subtree(nid: str) -> set[str]:
        node = tree.get(nid)
        acc: set[str] = set()
        if nid in graph_nodes:
            acc.add(nid)
        if node is not None:
            for c in node["children"]:
                acc |= fill_subtree(c)
        subtree[nid] = acc
        return acc

    # Roots of the tree = nodes whose parent is None (the synthetic '' root).
    for nid, node in tree.items():
        if node["parent"] is None:
            fill_subtree(nid)
    # Safety net for any orphan not reached from a root.
    for nid in tree:
        if nid not in subtree:
            fill_subtree(nid)

    # ── warm: external transitive dependents of each subtree ──────────────────
    def warm_blast(seeds: set[str]) -> tuple[int, int]:
        """(transitive external dependents, direct external dependents)."""
        if not seeds:
            return 0, 0
        direct: set[str] = set()
        for s in seeds:
            direct |= rev.get(s, set())
        direct -= seeds
        seen = set(seeds)
        dq = deque(seeds)
        while dq:
            x = dq.popleft()
            for p in rev.get(x, ()):
                if p not in seen:
                    seen.add(p)
                    dq.append(p)
        return len(seen - seeds), len(direct)

    # ── cold: SCC condensation, build cohort (level), critical path ──────────
    sccs = [sorted(c) for c in _tarjan_sccs(graph_nodes, fwd)]
    scc_of: dict[str, int] = {}
    for i, comp in enumerate(sccs):
        for f in comp:
            scc_of[f] = i
    n_sccs = len(sccs)

    scc_fwd: dict[int, set[int]] = defaultdict(set)   # scc -> sccs it depends on
    scc_rev: dict[int, set[int]] = defaultdict(set)   # scc -> sccs depending on it
    for a, ts in fwd.items():
        sa = scc_of[a]
        for b in ts:
            sb = scc_of[b]
            if sa != sb:
                scc_fwd[sa].add(sb)
                scc_rev[sb].add(sa)

    # level = longest chain of dependencies beneath an SCC (memoized DFS).
    level: dict[int, int] = {}

    def calc_level(i: int) -> int:
        if i in level:
            return level[i]
        level[i] = 0  # guard (DAG, so no real cycles here)
        deps = scc_fwd.get(i, ())
        level[i] = (1 + max((calc_level(d) for d in deps), default=-1)) if deps else 0
        return level[i]

    for i in range(n_sccs):
        calc_level(i)

    # height = longest chain of dependents above an SCC.
    height: dict[int, int] = {}

    def calc_height(i: int) -> int:
        if i in height:
            return height[i]
        height[i] = 0
        ups = scc_rev.get(i, ())
        height[i] = (1 + max((calc_height(u) for u in ups), default=-1)) if ups else 0
        return height[i]

    for i in range(n_sccs):
        calc_height(i)

    global_max = max(level.values(), default=0)
    # An SCC lies on a globally-longest chain iff its longest-below + longest-above
    # spans the whole graph. This marks the entire critical band, not one chain.
    on_crit_scc = {i for i in range(n_sccs) if level[i] + height[i] == global_max}

    # Per-graph-node cold metrics.
    node_level = {f: level[scc_of[f]] for f in graph_nodes}
    node_crit = {f: (scc_of[f] in on_crit_scc) for f in graph_nodes}
    node_scc_size = {f: len(sccs[scc_of[f]]) for f in graph_nodes}

    # ── assemble per-tree-node output ─────────────────────────────────────────
    nodes_out: dict[str, dict] = {}
    for nid, leaves in subtree.items():
        if not leaves:
            continue  # tree node with no graph nodes below it — nothing to show
        warm, fan_in = warm_blast(leaves)
        nodes_out[nid] = {
            "warm": warm,
            "warm_pct": round(100.0 * warm / n_total, 1) if n_total else 0.0,
            "fan_in": fan_in,
            "level": max(node_level[f] for f in leaves),
            "crit": any(node_crit[f] for f in leaves),
            "scc": max(node_scc_size[f] for f in leaves),
        }

    # ── one canonical longest chain (for the cold panel), deepest → shallowest ─
    critical_path: list[list[str]] = []
    if n_sccs:
        # Start at the on-critical SCC with the greatest level (deepest), break
        # ties by first folder name so the trace is deterministic.
        cur = min(
            (i for i in on_crit_scc),
            key=lambda i: (-level[i], sccs[i][0] if sccs[i] else ""),
            default=None,
        )
        while cur is not None:
            critical_path.append(sccs[cur])
            nxt = [d for d in scc_fwd.get(cur, ()) if level[d] == level[cur] - 1]
            if not nxt:
                break
            cur = min(nxt, key=lambda i: sccs[i][0] if sccs[i] else "")

    width = defaultdict(int)
    for i in range(n_sccs):
        width[level[i]] += 1
    n_cycles = sum(1 for c in sccs if len(c) > 1)
    biggest_scc = max((len(c) for c in sccs), default=0)

    summary = {
        "graph_nodes": n_total,
        "cohorts": global_max + 1 if n_sccs else 0,
        "crit_len": len(critical_path),
        "max_width": max(width.values(), default=0),
        "n_cycles": n_cycles,
        "biggest_scc": biggest_scc,
        "critical_path": critical_path,
    }
    return {"nodes": nodes_out, "summary": summary}
