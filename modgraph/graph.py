"""Core graph algorithms: Tarjan SCC, SCC-aware migration plan, folder tree."""
from __future__ import annotations

from collections import defaultdict


def _tarjan_sccs(nodes: set[str], deps: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan's SCC. Returns list of SCCs (each a list of folder ids)."""
    index_counter = [0]
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    sccs: list[list[str]] = []

    def strongconnect(start: str):
        work = [(start, iter(deps.get(start, ())))]
        index_of[start] = index_counter[0]
        lowlink[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack[start] = True

        while work:
            v, it = work[-1]
            pushed = False
            for w in it:
                if w not in index_of:
                    index_of[w] = index_counter[0]
                    lowlink[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append((w, iter(deps.get(w, ()))))
                    pushed = True
                    break
                elif on_stack.get(w):
                    lowlink[v] = min(lowlink[v], index_of[w])
            if pushed:
                continue
            # Finished exploring v
            if lowlink[v] == index_of[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == v:
                        break
                sccs.append(comp)
            work.pop()
            if work:
                parent_v, _ = work[-1]
                lowlink[parent_v] = min(lowlink[parent_v], lowlink[v])

    for n in nodes:
        if n not in index_of:
            strongconnect(n)
    return sccs


def compute_migration_plan(leaf_edges: dict, source_folders: set[str],
                           scores: dict[str, dict] | None = None):
    """Condense the folder graph into SCCs, then produce a topologically-ordered
    migration plan over the condensation DAG. Each step is an SCC (one or more
    folders that must migrate together because they cyclically depend).

    ``scores`` (optional) is the per-folder dict from
    :func:`modgraph.scoring.compute_folder_scores` (its ``["folders"]``). When
    present, the eligible frontier is ranked by **ROI** first — aggregated
    payoff (churn-weighted ``hot`` when available, else the structural
    ``combined``) over aggregated effort per SCC — so the plan front-loads
    cheap, high-payoff steps. The topological constraint is untouched: only
    the order *within* the eligible set changes. Without ``scores`` the
    ordering is exactly the legacy structural one (which is what
    graph_logic.js `migrationPlanOrder` mirrors — the JS wizard stays on the
    structural key until the UI ships scores).

    Returns (plan, stuck_sccs) where:
      plan = [{
        step, folders: [folder, ...],            # the SCC to migrate this step
        size: int,
        is_cycle: bool,                          # True if SCC has >1 folder
        unlocks: [{step, folders, size}],        # SCCs that became eligible
        payoff, effort, roi,                     # from `scores`; None without
      }]
      stuck_sccs = list of SCCs never reachable (shouldn't occur with full DAG
                   plan, but kept for symmetry).
    """
    deps: dict[str, set[str]] = defaultdict(set)
    for (a, b) in leaf_edges:
        if a == b:
            continue
        if a in source_folders and b in source_folders:
            deps[a].add(b)

    sccs = _tarjan_sccs(source_folders, deps)
    # Sort folders inside each SCC alphabetically; assign each folder to an SCC id.
    sccs = [sorted(c) for c in sccs]
    scc_of: dict[str, int] = {}
    for i, comp in enumerate(sccs):
        for f in comp:
            scc_of[f] = i

    # Condensation DAG: scc -> set(scc).
    scc_deps: dict[int, set[int]] = defaultdict(set)
    scc_rdeps: dict[int, set[int]] = defaultdict(set)
    for a, ts in deps.items():
        sa = scc_of[a]
        for b in ts:
            sb = scc_of[b]
            if sa != sb:
                scc_deps[sa].add(sb)
                scc_rdeps[sb].add(sa)

    n_sccs = len(sccs)

    # Per-SCC ROI from the folder scores: payoff = Σ member hot (churn-weighted;
    # falls back to the structural `combined`), effort = Σ member effort. A
    # bundle migrates together, so its members' efforts genuinely add; summed
    # payoff can double-count shared dependents across members, but it only
    # ranks the frontier — the topological order stays exact.
    scc_payoff: dict[int, float] = {}
    scc_effort: dict[int, int] = {}
    scc_roi: dict[int, float] = {}
    if scores is not None:
        for i in range(n_sccs):
            payoff = 0.0
            effort = 0
            for f in sccs[i]:
                row = scores.get(f) or {}
                hot = row.get("hot")
                payoff += hot if hot is not None else row.get("combined", 0.0)
                effort += row.get("effort", 0)
            scc_payoff[i] = round(payoff, 1)
            scc_effort[i] = effort
            scc_roi[i] = round(payoff / max(effort, 1), 2)

    remaining = {i: len(scc_deps.get(i, ())) for i in range(n_sccs)}
    eligible: set[int] = {i for i in range(n_sccs) if remaining[i] == 0}
    migrated: set[int] = set()
    plan: list[dict] = []
    step = 0

    # Precompute transitive reverse-reach per SCC — how many other SCCs
    # ultimately depend on this one (directly OR transitively). Used as primary
    # rank: migrating a high-reach SCC frees up the most downstream work, so the
    # rest of the plan rolls forward faster. Iterative DP over the condensation
    # DAG in reverse-topological order.
    topo_order: list[int] = []
    indeg = {i: len(scc_deps.get(i, ())) for i in range(n_sccs)}
    queue = [i for i in range(n_sccs) if indeg[i] == 0]
    while queue:
        v = queue.pop()
        topo_order.append(v)
        for w in scc_rdeps.get(v, ()):
            indeg[w] -= 1
            if indeg[w] == 0:
                queue.append(w)
    reverse_reach: dict[int, int] = {i: 0 for i in range(n_sccs)}
    for v in reversed(topo_order):
        seen: set[int] = set()
        for w in scc_rdeps.get(v, ()):
            seen.add(w)
        # Union descendants of each direct predecessor via inclusion-exclusion is
        # expensive; an exact DP needs bitset closure. For typical project sizes
        # the direct + 1-hop approximation tracks the user-facing intuition
        # ("things that depend on me") well enough — keep it cheap by counting
        # direct dependents plus their reverse_reach (double-counts in diamonds,
        # but ordering remains stable).
        reverse_reach[v] = sum(1 + reverse_reach[w] for w in scc_rdeps.get(v, ()))

    def immediate_unlocks(i: int) -> int:
        return sum(
            1
            for s in scc_rdeps.get(i, ())
            if s not in migrated and remaining.get(s, 1) == 1 and s != i
        )

    while eligible:
        # Rank: highest ROI first when scores are present (quick wins lead),
        # then highest transitive reverse-reach (unblocks most downstream), then
        # immediate unlocks (next-step momentum), then smaller SCC (easier first),
        # then the SCC's first folder name ascending. The full folder name (not
        # just its first character) is the final tie-break so the plan is fully
        # deterministic — independent of set iteration order / PYTHONHASHSEED.
        # `min` over negated numeric ranks lets the string tie-break sort ascending.
        pick = min(
            eligible,
            key=lambda i: ((-scc_roi[i],) if scores is not None else ())
                          + (-reverse_reach[i], -immediate_unlocks(i), len(sccs[i]),
                             sccs[i][0] if sccs[i] else ""),
        )
        eligible.remove(pick)
        migrated.add(pick)
        step += 1
        unlocked_now: list[dict] = []
        # Iterate the dependent SCCs in a fixed order so the emitted `unlocks`
        # list is stable run-to-run (scc_rdeps values are sets).
        for s in sorted(scc_rdeps.get(pick, ())):
            if s in migrated:
                continue
            remaining[s] -= 1
            if remaining[s] == 0:
                eligible.add(s)
                unlocked_now.append(
                    {"step": None, "folders": sccs[s], "size": len(sccs[s])}
                )
        unlocked_now.sort(key=lambda u: u["folders"])
        plan.append({
            "step": step,
            "folders": sccs[pick],
            "size": len(sccs[pick]),
            "is_cycle": len(sccs[pick]) > 1,
            "unlocks": unlocked_now,
            "payoff": scc_payoff.get(pick),
            "effort": scc_effort.get(pick),
            "roi": scc_roi.get(pick),
        })

    # Backfill the 'step' field on unlocks now that all steps exist.
    step_by_scc: dict[tuple, int] = {tuple(p["folders"]): p["step"] for p in plan}
    for p in plan:
        for u in p["unlocks"]:
            u["step"] = step_by_scc.get(tuple(u["folders"]))

    stuck_sccs = [sccs[i] for i in range(n_sccs) if i not in migrated]
    return plan, stuck_sccs


def build_tree(all_folders: set[str], decls: dict[str, set[str]], root_label: str = "Project"):
    """Return nested dict tree + folder metadata.

    tree[node_id] = {
        'id': node_id,                     # e.g. 'Core/Utils'
        'name': last_segment,
        'parent': parent_id_or_None,
        'children': [child_id, ...],
        'types': declared_type_count_in_subtree,
    }
    Includes synthetic 'ROOT' for top-level so navigation is uniform.
    """
    nodes: dict[str, dict] = {}

    def ensure(path: str) -> dict:
        if path in nodes:
            return nodes[path]
        if path == "":
            nodes[path] = {
                "id": "",
                "name": root_label,
                "parent": None,
                "children": [],
                "types": 0,
            }
            return nodes[path]
        parts = path.split("/")
        parent = "/".join(parts[:-1])
        parent_node = ensure(parent)
        nodes[path] = {
            "id": path,
            "name": parts[-1],
            "parent": parent,
            "children": [],
            "types": 0,
        }
        if path not in parent_node["children"]:
            parent_node["children"].append(path)
        return nodes[path]

    for folder in all_folders:
        ensure(folder)
    # also create chain for any intermediate folders missing
    for path in list(nodes):
        cur = path
        while cur:
            ensure(cur)
            cur = nodes[cur]["parent"] or ""
            if cur == "":
                ensure("")
                break

    # propagate declared-type counts up the tree
    for folder, types in decls.items():
        n = len(types)
        cur: str | None = folder
        while cur is not None:
            nodes[cur]["types"] += n
            cur = nodes[cur]["parent"]

    for n in nodes.values():
        n["children"].sort()
    # Return with sorted keys: `nodes` was populated from a set, so its insertion
    # order is hash-seed dependent. Sorting makes the emitted tree deterministic.
    return {k: nodes[k] for k in sorted(nodes)}
