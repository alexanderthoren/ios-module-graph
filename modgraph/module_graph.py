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

import os
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
                         root_label: str = "App", build_times=None,
                         build_floors=None) -> dict:
    """Return ``{"nodes": [...], "edges": [...], "summary": {...}}`` for Build mode.

    Each node: ``{id, label, kind: 'app'|'spm', folders, types, warm, warm_pct,
    fan_in, level, crit, scc, build_ms, cold_wall_ms, measured}``. Edges:
    ``{from, to, w}`` (from depends on to). When ``build_times``
    (``{target_name: seconds}``) is supplied, each node gets its measured compile
    *work* (``build_ms``); SPM modules match by label, and any target not matching
    an SPM label folds into the app node.

    ``cold_wall_ms`` = estimated **from-scratch wall-clock** to build that module
    *and everything it depends on*, clean — the time-weighted longest path through
    its dependency closure. Each module's own wall on that path is
    ``max(work/cores, serial_floor)`` where ``serial_floor`` = its longest single
    file (from ``build_floors``); this respects both within-module parallelism
    (files split across cores) and its irreducible floor (can't beat the slowest
    file). The app target, being the root, gets the whole-graph critical path —
    i.e. the total clean-build wall. Without ``build_floors`` it degrades to a
    cores-only estimate. Deterministic — nodes sorted by id, edges by (from, to).
    """
    build_times = build_times or {}
    build_floors = build_floors or {}
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

    # Resolve measured build times: SPM modules match the target name by label;
    # every target not matching an SPM label (the app/exe targets) folds into app.
    labels = {m: ("App (xcodeproj)" if m == APP_ID else _package_label(m)) for m in modules}
    spm_labels = {labels[m] for m in modules if m != APP_ID}
    secs: dict[str, float] = {}
    floors: dict[str, float] = {}
    if build_times:
        for m in modules:
            if m == APP_ID:
                continue
            secs[m] = build_times.get(labels[m], 0.0)
            floors[m] = build_floors.get(labels[m], 0.0)
        app_secs = sum(v for name, v in build_times.items() if name not in spm_labels)
        # App serial floor = its single longest file across all folded targets
        # (a floor is a max, not a sum — unlike work).
        app_floor = max((v for name, v in build_floors.items() if name not in spm_labels),
                        default=0.0)
        if APP_ID in modules:
            secs[APP_ID] = app_secs
            floors[APP_ID] = app_floor

    cores = os.cpu_count() or 1

    # Per-module own wall = within-module parallelism (work spread over cores),
    # but never below its serial floor (longest single file).
    def node_wall(m: str) -> float:
        return max(secs.get(m, 0.0) / cores, floors.get(m, 0.0))

    # cold_wall = time-weighted longest path through the depends-on closure:
    # node's own wall + the slowest dependency's cold_wall (deps compile in
    # parallel, so we wait only on the deepest chain). Memoized, cycle-guarded.
    fwd: dict[str, set[str]] = defaultdict(set)
    for (a, b) in module_edges:
        fwd[a].add(b)
    _cw: dict[str, float] = {}
    _stack: set[str] = set()

    def cold_wall(m: str) -> float:
        if m in _cw:
            return _cw[m]
        if m in _stack:        # defensive: modules are acyclic, but never recurse forever
            return node_wall(m)
        _stack.add(m)
        deepest_dep = max((cold_wall(d) for d in fwd.get(m, ())), default=0.0)
        _stack.discard(m)
        _cw[m] = node_wall(m) + deepest_dep
        return _cw[m]

    nodes = []
    for m in sorted(modules):
        met = bi_nodes.get(m, default)
        s = secs.get(m, 0.0)
        nodes.append({
            "id": m,
            "label": labels[m],
            "kind": "app" if m == APP_ID else "spm",
            "folders": folders_count.get(m, 0),
            "types": types_count.get(m, 0),
            "warm": met["warm"],
            "warm_pct": met["warm_pct"],
            "fan_in": met["fan_in"],
            "level": met["level"],
            "crit": met["crit"],
            "scc": met["scc"],
            "build_ms": int(round(s * 1000)),
            "cold_wall_ms": int(round(cold_wall(m) * 1000)) if secs else 0,
            "measured": s > 0,
        })
    edges = [{"from": a, "to": b, "w": w} for (a, b), w in sorted(module_edges.items())]
    summary = dict(bi["summary"])
    summary["measured"] = bool(build_times)
    summary["total_build_s"] = round(sum(secs.values()), 1) if secs else 0.0

    # Estimated real (wall-clock) clean build. The headline `total_build_s` is
    # summed CPU *work* across every file (each `build_ms` is itself a sum over a
    # module's files, which compile in parallel), so it over-states the wait. Two
    # honest floors bound the wall:
    #   • resource floor — total work ÷ cores (can't use more cores than exist);
    #   • dependency floor — the deepest cold_wall chain (== the app/root's, since
    #     it depends on everything), which respects both inter-module ordering and
    #     each module's own serial floor.
    # The clean build can't beat either, so the estimate is their max.
    summary["cores"] = cores
    if secs:
        resource_floor = summary["total_build_s"] / cores
        dep_floor = max((cold_wall(m) for m in modules), default=0.0)
        summary["est_wall_s"] = round(max(resource_floor, dep_floor), 1)
    else:
        summary["est_wall_s"] = 0.0
    return {"nodes": nodes, "edges": edges, "summary": summary}
