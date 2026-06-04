"""Module-level build graph: collapse the folder graph to real compile units.

A folder is *not* a compilation unit. In this project the build units are:

* each **SPM target** — one directory under a package's ``Sources/`` (``…/Sources/<Target>``);
* the **xcodeproj app target** — everything not under any SPM package (one unit).

Recompilation happens per build unit, so warm-rebuild blast radius and cold-build
cohorts are only *truthful* at this granularity (folder-level over-counts: a folder
inside the app target never recompiles alone — the whole target does, and
folder-level cycles inside one module are irrelevant because the module compiles
atomically).

``compute_module_graph`` maps every folder to its module, collapses folder edges to
module→module edges, and reuses :func:`modgraph.build_impact.compute_build_impact`
(which is node/edge-agnostic) to score each module. Powers Build mode.
"""
from __future__ import annotations

from collections import defaultdict

from .build_impact import compute_build_impact
from .graph import build_tree
from .spm import _package_label

APP_ID = "app"


def module_of(folder: str, migrated_prefixes: list[str]) -> str:
    """Map a folder to its build unit (SPM target id, or ``"app"``).

    The longest matching migrated prefix wins. A prefix that ends in ``Sources``
    resolves to ``<prefix>/<next-segment>`` (the SPM target dir); any other
    matched prefix is itself the module (a package without a ``Sources/`` layout).
    Folders under no prefix belong to the single app target.
    """
    best: str | None = None
    for p in migrated_prefixes:
        pn = p.rstrip("/")
        if folder == pn or folder.startswith(pn + "/"):
            if best is None or len(pn) > len(best):
                best = pn
    if best is None:
        return APP_ID
    if best.split("/")[-1] == "Sources":
        rest = folder[len(best) + 1:].split("/") if len(folder) > len(best) + 1 else []
        return best + "/" + rest[0] if rest and rest[0] else best
    return best


def compute_module_graph(all_folders, leaf_edges, migrated_prefixes, decls,
                         root_label: str = "App") -> dict:
    """Return ``{"nodes": [...], "edges": [...], "summary": {...}}`` for Build mode.

    Each node: ``{id, label, kind: 'app'|'spm', folders, types, warm, warm_pct,
    fan_in, level, crit, scc}``. Edges: ``{from, to, w}`` (from depends on to).
    Deterministic — nodes sorted by id, edges by (from, to).
    """
    prefixes = list(migrated_prefixes)

    def mod(f: str) -> str:
        return module_of(f, prefixes)

    modules: set[str] = set()
    folders_count: dict[str, int] = defaultdict(int)
    types_count: dict[str, int] = defaultdict(int)
    for f in all_folders:
        m = mod(f)
        modules.add(m)
        folders_count[m] += 1
    for f, ts in decls.items():
        types_count[mod(f)] += len(ts)

    module_edges: dict[tuple[str, str], int] = defaultdict(int)
    for (a, b), w in leaf_edges.items():
        ma, mb = mod(a), mod(b)
        modules.add(ma)
        modules.add(mb)
        if ma != mb:
            module_edges[(ma, mb)] += w

    # Reuse the generic build-impact scorer over the module graph. build_tree
    # nests the slash-bearing module ids, but each real module id is itself a
    # graph node, so its subtree is just {itself} and its metrics are exact; the
    # synthetic intermediate nodes (e.g. "FeverApp/Sources") are simply not read.
    tree = build_tree(modules, {m: set() for m in modules}, root_label=root_label)
    bi = compute_build_impact(tree, dict(module_edges))
    bi_nodes = bi["nodes"]
    default = {"warm": 0, "warm_pct": 0.0, "fan_in": 0, "level": 0, "crit": False, "scc": 1}

    nodes = []
    for m in sorted(modules):
        met = bi_nodes.get(m, default)
        nodes.append({
            "id": m,
            "label": "App (xcodeproj)" if m == APP_ID else _package_label(m),
            "kind": "app" if m == APP_ID else "spm",
            "folders": folders_count.get(m, 0),
            "types": types_count.get(m, 0),
            "warm": met["warm"],
            "warm_pct": met["warm_pct"],
            "fan_in": met["fan_in"],
            "level": met["level"],
            "crit": met["crit"],
            "scc": met["scc"],
        })
    edges = [{"from": a, "to": b, "w": w} for (a, b), w in sorted(module_edges.items())]
    return {"nodes": nodes, "edges": edges, "summary": bi["summary"]}
