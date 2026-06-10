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
  to/from the folder that (a) would not create a module-graph cycle,
  (b) stays within the complexity bounds, (c) would not be **raised in build
  level** by the folder's own module deps (layer inversion: feature-ish code
  must not drag a low module upward), and (d) is not a **churn-hostile**
  landing spot (a hot folder into a widely-depended-on module makes every
  consumer pay the churn on warm rebuilds). A new module is proposed only
  when nothing fits. Every vetoed candidate ships with its reason and
  evidence (``rejected``) so the pick is auditable and overridable.
* **Levels (study 2026-06-10, level-aware quick wins)** — each item carries
  the folder's current build ``level``/``crit`` (from the folder scores) and
  its ``landing_level``: the module-graph level its own module would occupy
  if extracted today, given the migrated modules it references. Computed for
  blocked (``cut_first``) folders too — "fix the cut-set and this lands at
  LX" is the motivation for the cut. The surviving absorb pick is
  level-preserving *by construction* (predicate c), so no delta is reported
  on it; the deltas live in the rejections.
* **Ranking (D1)** — items sorted by the folder's churn-weighted payoff over
  effort (``roi`` from :mod:`modgraph.scoring`): improving the warm build,
  the cold build, or unblocking the rest all raise payoff, so "core" folders
  surface first; the cut-set size is the tiebreak (less surgery first).

Pure interpretation over inputs the pipeline already has; deterministic
(sorted iteration only). Ships as ``payload["quick_wins"]``.
"""
from __future__ import annotations

from collections import defaultdict, deque

from .config import (ABSORB_CHURN_HOT, ABSORB_MAX_FILES, ABSORB_MAX_PUBLIC,
                     ABSORB_REJECTED_MAX, ABSORB_WARM_WIDE, MOVE_FILE_MAX,
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
                      module_fwd: dict[str, set[str]], module_levels: dict[str, int],
                      module_warm: dict[str, int], churn: int, churned: bool,
                      row: dict) -> tuple[dict | None, list[dict]]:
    """``(destination, rejected)`` — the auto-pick plus every vetoed candidate.

    ``destination`` is ``None`` when nothing fits (size bounds exceeded, or
    every candidate vetoed). Each ``rejected`` entry carries ``{module, label,
    refs, reason, evidence}`` — ``module_cycle`` / ``raises_level`` /
    ``churn_hostile`` — sorted heaviest-traffic first and capped at
    ``ABSORB_REJECTED_MAX`` so a reviewer can audit (and override) the veto.
    """
    if row.get("files", 0) > ABSORB_MAX_FILES or row.get("public", 0) > ABSORB_MAX_PUBLIC:
        return None, []

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

    # Freeze: defaultdict subscripts below would otherwise insert zero-weight
    # keys and pollute the dep set the level predicate iterates.
    w_to, w_from = dict(w_to), dict(w_from)
    candidates = sorted(set(w_to) | set(w_from))
    valid: list[tuple[int, str]] = []
    rejected: list[dict] = []

    def veto(m: str, reason: str, evidence: list[str]) -> None:
        rejected.append({
            "module": m,
            "label": module_labels.get(m, m),
            "refs": w_to.get(m, 0) + w_from.get(m, 0),
            "reason": reason,
            "evidence": evidence,
        })

    for m in candidates:
        # Absorbing `folder` into m adds m -> dep for each module it uses, and
        # consumer -> m for each module using it. Reject anything that would
        # close a module-graph cycle (modules must stay a DAG).
        cyc = sorted(dep for dep in w_to if dep != m and _reaches(module_fwd, dep, m))
        cyc += sorted(c for c in w_from if c != m and _reaches(module_fwd, m, c))
        if cyc:
            veto(m, "module_cycle",
                 [module_labels.get(x, x) for x in cyc])
            continue
        # Layer inversion: m's level is 1 + max over its deps, so gaining a dep
        # at or above its level necessarily raises it — and the level of every
        # module stacked on m. Hard reject (Q1 of the study).
        m_level = module_levels.get(m, 0)
        raising = sorted(d for d in w_to
                         if d != m and module_levels.get(d, 0) >= m_level)
        if raising:
            veto(m, "raises_level",
                 [f"{module_labels.get(d, d)} is L{module_levels.get(d, 0)}, "
                  f"destination is L{m_level}" for d in raising])
            continue
        # Churn hostility: hot folder x widely-depended-on destination means
        # every transitive consumer pays the churn on warm rebuilds. No-ops
        # without churn data (churned=False keeps "no data" != "untouched").
        warm_m = module_warm.get(m, 0)
        if churned and churn >= ABSORB_CHURN_HOT and warm_m >= ABSORB_WARM_WIDE:
            veto(m, "churn_hostile",
                 [f"folder churn {churn} commit(s)",
                  f"destination has {warm_m} dependent module(s)"])
            continue
        valid.append((w_to.get(m, 0) + w_from.get(m, 0), m))

    rejected.sort(key=lambda r: (-r["refs"], r["module"]))
    rejected = rejected[:ABSORB_REJECTED_MAX]
    if not valid:
        return None, rejected
    refs, pick = max(valid, key=lambda t: (t[0], t[1]))
    return {
        "module": pick,
        "label": module_labels.get(pick, pick),
        "refs": refs,
        "uses": w_to.get(pick, 0),
        "used_by": w_from.get(pick, 0),
        "level": module_levels.get(pick, 0),
    }, rejected


def compute_quick_wins(folder_scores: dict, plan_edges: dict, pair_types: dict | None,
                       leaf_edges: dict, source_folders: set[str],
                       migrated_prefixes: list[str], file_edges: list[dict],
                       module_graph: dict) -> dict:
    """Return ``{"items": [...], "summary": {...}}`` ranked quick-win candidates.

    ``folder_scores`` is :func:`modgraph.scoring.compute_folder_scores` output;
    ``plan_edges`` the migration plan's edge set (both endpoints source
    folders); ``leaf_edges`` the full edge map (for references into already-
    migrated modules); ``module_graph`` the Build-mode graph (labels + edges
    for the cycle check, per-module ``level``/``warm`` for the layer and churn
    predicates). ``pair_types``/``file_edges`` may be empty on the regex-scan
    path — classification degrades to ``invert`` with no evidence.
    """
    rows = folder_scores.get("folders", {})
    churned = folder_scores.get("summary", {}).get("churned", False)
    pair_types = pair_types or {}
    fbp = _files_by_pair(file_edges)
    consumers = _type_consumers(pair_types)

    module_labels = {n["id"]: n.get("label", n["id"])
                     for n in module_graph.get("nodes", [])}
    module_levels = {n["id"]: n.get("level", 0)
                     for n in module_graph.get("nodes", [])}
    module_warm = {n["id"]: n.get("warm", 0)
                   for n in module_graph.get("nodes", [])}
    module_fwd: dict[str, set[str]] = defaultdict(set)
    for e in module_graph.get("edges", []):
        module_fwd[e["from"]].add(e["to"])

    # Modules each folder references (one pass): the folder's first-party deps
    # *after* extraction, hence its landing level. Cut edges (to other source
    # folders) are excluded by definition — the projection answers "where does
    # this land once the cut-set is fixed".
    mod_deps: dict[str, set[str]] = defaultdict(set)
    for (a, b) in sorted(leaf_edges):
        if a in source_folders and b not in source_folders:
            m = module_of(b, migrated_prefixes)
            if m != APP_ID:
                mod_deps[a].add(m)

    def landing_level(f: str) -> int:
        deps = mod_deps.get(f)
        if not deps:
            return 0
        return 1 + max(module_levels.get(m, 0) for m in sorted(deps))

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
        rejected: list[dict] = []
        if extractable:
            destination, rejected = _absorb_candidate(
                f, leaf_edges, source_folders, migrated_prefixes,
                module_labels, module_fwd, module_levels, module_warm,
                row.get("churn", 0), churned, row)
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
            "level": row.get("level", 0),
            "crit": row.get("crit", False),
            "landing_level": landing_level(f),
            "extractable_now": extractable,
            "action": action,
            "destination": destination,
            "rejected": rejected,
            "cut": {"edges": cut_edges, "total_refs": cut_refs},
        })

    items.sort(key=lambda i: (-i["roi"], i["cut"]["total_refs"], i["folder"]))
    summary = {
        "total": len(items),
        "extractable_now": sum(1 for i in items if i["extractable_now"]),
        "absorbable": sum(1 for i in items if i["action"] == "absorb"),
        "cut_first": sum(1 for i in items if i["action"] == "cut_first"),
        "vetoed": sum(1 for i in items if i["rejected"]),
        "churned": churned,
    }
    return {"items": items, "summary": summary}
