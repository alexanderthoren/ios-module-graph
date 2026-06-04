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
                         root_label: str = "App", build_times=None) -> dict:
    """Return ``{"nodes": [...], "edges": [...], "summary": {...}}`` for Build mode.

    Each node: ``{id, label, kind: 'app'|'spm', folders, types, warm, warm_pct,
    fan_in, level, crit, scc, build_ms, measured}``. Edges: ``{from, to, w}`` (from
    depends on to). When ``build_times`` (``{target_name: seconds}``) is supplied,
    each node gets its measured compile time (``build_ms``); SPM modules match by
    label, and any target not matching an SPM label folds into the app node.
    Deterministic — nodes sorted by id, edges by (from, to).
    """
    build_times = build_times or {}
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
    if build_times:
        for m in modules:
            if m == APP_ID:
                continue
            secs[m] = build_times.get(labels[m], 0.0)
        app_secs = sum(v for name, v in build_times.items() if name not in spm_labels)
        if APP_ID in modules:
            secs[APP_ID] = app_secs

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
            "measured": s > 0,
        })
    edges = [{"from": a, "to": b, "w": w} for (a, b), w in sorted(module_edges.items())]
    summary = dict(bi["summary"])
    summary["measured"] = bool(build_times)
    summary["total_build_s"] = round(sum(secs.values()), 1) if secs else 0.0

    # Estimated real (wall-clock) build. The headline `total_build_s` is summed
    # CPU *work* across every file (each module's `build_ms` is already a sum over
    # its files, which compile in parallel), so it over-states the wait wildly. The
    # binding floor is the **resource floor** — that work spread across the build
    # machine's cores. (A work-weighted critical path can't help: chain-work ≤
    # total-work always, so chain_work/cores ≤ total_work/cores. A *true*
    # dependency floor would need per-module wall times, which the summed stats
    # don't carry; cold-build dependency depth is exposed separately as `crit_len`
    # cohorts.) Only meaningful with measured times.
    summary["cores"] = os.cpu_count() or 1
    summary["est_wall_s"] = round(summary["total_build_s"] / summary["cores"], 1) if secs else 0.0
    return {"nodes": nodes, "edges": edges, "summary": summary}
