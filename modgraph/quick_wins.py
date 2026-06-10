"""Quick-win engine: what can be extracted *today*, where to put it, what blocks it.

The migration plan answers "in what order"; this module answers the faster
question every modularization actually runs on (study 2026-06-10, decisions
D1/D2): for **every** source folder —

* **Cut-set** — the outgoing first-party edges that block extracting it right
  now, each with its crossing types and an advisory fix classification:
  ``move_file`` (the edge comes from a couple of files whose home is the
  target folder — move them and the edge dies), ``shared_primitive`` (every
  crossing type is consumed by several folders — push it down into a shared
  module), or ``invert`` (own a protocol, let the target conform). This
  generalizes :func:`modgraph.cycles.compute_extraction_targets` from cycle
  members to everything.
* **Destination (D2)** — absorbing into an **existing** SPM module is the
  default outcome. The auto-pick is the module with the most references
  to/from the folder that (a) would not create a module-graph cycle and
  (b) stays within the complexity bounds. A new module is proposed only when
  nothing fits.
* **Ranking (D1)** — items sorted by the folder's churn-weighted payoff over
  effort (``roi`` from :mod:`modgraph.scoring`): improving the warm build,
  the cold build, or unblocking the rest all raise payoff, so "core" folders
  surface first; the cut-set size is the tiebreak (less surgery first).

Pure interpretation over inputs the pipeline already has; deterministic
(sorted iteration only). Ships as ``payload["quick_wins"]``.
"""
from __future__ import annotations

from collections import defaultdict, deque

from .config import (ABSORB_MAX_FILES, ABSORB_MAX_PUBLIC, MOVE_FILE_MAX,
                     SHARED_MIN_CONSUMERS)
from .module_graph import APP_ID, module_of


def _folder_of_file(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."


def _files_by_pair(file_edges: list[dict]) -> dict[tuple[str, str], set[str]]:
    """(src_folder, dst_folder) -> source files of src_folder feeding that edge."""
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for fe in file_edges or []:
        out[(_folder_of_file(fe["src"]), _folder_of_file(fe["dst"]))].add(fe["src"])
    return out


def _type_consumers(pair_types: dict) -> dict[tuple[str, str], int]:
    """(owner_folder, type) -> number of distinct folders referencing it there."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for (_a, b), types in sorted(pair_types.items()):
        for t in sorted(types):
            counts[(b, t)] += 1
    return counts


def _classify_cut(src: str, dst: str, types: list[str],
                  files_by_pair: dict, consumers: dict) -> tuple[str, list[str]]:
    """(fix, evidence) for one blocking edge — advisory, not a proof."""
    files = sorted(files_by_pair.get((src, dst), ()))
    if files and len(files) <= MOVE_FILE_MAX:
        return "move_file", files
    if types and all(consumers.get((dst, t), 0) >= SHARED_MIN_CONSUMERS
                     for t in types):
        return "shared_primitive", types
    return "invert", types


def _reaches(fwd: dict[str, set[str]], start: str, goal: str) -> bool:
    """True when `goal` is reachable from `start` over the module DAG."""
    if start == goal:
        return True
    seen = {start}
    dq = deque([start])
    while dq:
        for nxt in fwd.get(dq.popleft(), ()):
            if nxt == goal:
                return True
            if nxt not in seen:
                seen.add(nxt)
                dq.append(nxt)
    return False


def _absorb_candidate(folder: str, leaf_edges: dict, source_folders: set[str],
                      migrated_prefixes: list[str], module_labels: dict[str, str],
                      module_fwd: dict[str, set[str]], row: dict) -> dict | None:
    """Auto-picked existing-module destination, or None when nothing fits."""
    if row.get("files", 0) > ABSORB_MAX_FILES or row.get("public", 0) > ABSORB_MAX_PUBLIC:
        return None

    # Reference weight between the folder and each SPM module, both directions.
    w_to: dict[str, int] = defaultdict(int)     # folder depends on module
    w_from: dict[str, int] = defaultdict(int)   # module consumes folder
    for (a, b), w in sorted(leaf_edges.items()):
        if a == folder and b not in source_folders:
            m = module_of(b, migrated_prefixes)
            if m != APP_ID:
                w_to[m] += w
        elif b == folder and a not in source_folders:
            m = module_of(a, migrated_prefixes)
            if m != APP_ID:
                w_from[m] += w

    candidates = sorted(set(w_to) | set(w_from))
    valid: list[tuple[int, str]] = []
    for m in candidates:
        # Absorbing `folder` into m adds m -> dep for each module it uses, and
        # consumer -> m for each module using it. Reject anything that would
        # close a module-graph cycle (modules must stay a DAG).
        ok = all(not _reaches(module_fwd, dep, m) for dep in sorted(w_to) if dep != m) \
            and all(not _reaches(module_fwd, m, c) for c in sorted(w_from) if c != m)
        if ok:
            valid.append((w_to[m] + w_from[m], m))
    if not valid:
        return None
    refs, pick = max(valid, key=lambda t: (t[0], t[1]))
    return {
        "module": pick,
        "label": module_labels.get(pick, pick),
        "refs": refs,
        "uses": w_to.get(pick, 0),
        "used_by": w_from.get(pick, 0),
    }


def compute_quick_wins(folder_scores: dict, plan_edges: dict, pair_types: dict | None,
                       leaf_edges: dict, source_folders: set[str],
                       migrated_prefixes: list[str], file_edges: list[dict],
                       module_graph: dict) -> dict:
    """Return ``{"items": [...], "summary": {...}}`` ranked quick-win candidates.

    ``folder_scores`` is :func:`modgraph.scoring.compute_folder_scores` output;
    ``plan_edges`` the migration plan's edge set (both endpoints source
    folders); ``leaf_edges`` the full edge map (for references into already-
    migrated modules); ``module_graph`` the Build-mode graph (labels + edges
    for the cycle check). ``pair_types``/``file_edges`` may be empty on the
    regex-scan path — classification degrades to ``invert`` with no evidence.
    """
    rows = folder_scores.get("folders", {})
    pair_types = pair_types or {}
    fbp = _files_by_pair(file_edges)
    consumers = _type_consumers(pair_types)

    module_labels = {n["id"]: n.get("label", n["id"])
                     for n in module_graph.get("nodes", [])}
    module_fwd: dict[str, set[str]] = defaultdict(set)
    for e in module_graph.get("edges", []):
        module_fwd[e["from"]].add(e["to"])

    out_by_src: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (a, b), w in sorted(plan_edges.items()):
        if a != b:
            out_by_src[a].append((b, w))

    items: list[dict] = []
    for f in sorted(source_folders):
        row = rows.get(f, {})
        cut_edges = []
        for dst, w in sorted(out_by_src.get(f, ()), key=lambda t: (t[1], t[0])):
            types = sorted(pair_types.get((f, dst), ()))
            fix, evidence = _classify_cut(f, dst, types, fbp, consumers)
            cut_edges.append({"dst": dst, "refs": w, "types": types,
                              "fix": fix, "evidence": evidence})
        cut_refs = sum(e["refs"] for e in cut_edges)
        extractable = not cut_edges

        destination = None
        if extractable:
            destination = _absorb_candidate(
                f, leaf_edges, source_folders, migrated_prefixes,
                module_labels, module_fwd, row)
        if extractable:
            action = "absorb" if destination else "new_module"
        else:
            action = "cut_first"

        payoff = row.get("hot")
        if payoff is None:
            payoff = row.get("combined", 0.0)
        items.append({
            "folder": f,
            "roi": row.get("roi", 0.0),
            "payoff": payoff,
            "effort": row.get("effort", 0),
            "files": row.get("files", 0),
            "public": row.get("public", 0),
            "churn": row.get("churn", 0),
            "warm": row.get("warm", 0),
            "extractable_now": extractable,
            "action": action,
            "destination": destination,
            "cut": {"edges": cut_edges, "total_refs": cut_refs},
        })

    items.sort(key=lambda i: (-i["roi"], i["cut"]["total_refs"], i["folder"]))
    summary = {
        "total": len(items),
        "extractable_now": sum(1 for i in items if i["extractable_now"]),
        "absorbable": sum(1 for i in items if i["action"] == "absorb"),
        "cut_first": sum(1 for i in items if i["action"] == "cut_first"),
        "churned": folder_scores.get("summary", {}).get("churned", False),
    }
    return {"items": items, "summary": summary}
