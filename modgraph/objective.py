"""Expected-build-cost model: ONE scalar pair every plan step can move.

The pipeline scores modules with several incomparable numbers (folder ROI,
split leverage, isolation ROI). This module prices any module graph in the two
costs a team actually pays, so alternatives become comparable and a step's
worth can be stated as a delta:

* **warm_cost** — the recompile bill of a churn window: for every module,
  editing it recompiles the module plus its transitive dependents, so
  ``warm_cost = Σ_m churn(m) × rebuild_work(m)`` where ``rebuild_work(m)`` is
  the summed compile work of ``m`` and everything that transitively depends on
  it. With churn data this approximates "compile work caused by a year of
  edits at the observed edit rate"; without it every module weighs 1 commit
  (structural fallback, flagged via ``churned``).
* **cold_cost** — the clean-build dependency floor: the work-weighted longest
  path through the depends-on DAG (a chain compiles serially; siblings
  parallelize). The resource floor (Σwork ÷ cores) is the other bound; both
  are reported so the parallelism efficiency (resource ÷ max(resource, dep))
  says whether the graph is dependency-bound (chains too long) or
  resource-bound (as parallel as the hardware allows).

Work per module is measured seconds when the module graph carries them
(``build_ms``), otherwise the declared-type-count proxy — ``unit`` says which,
so consumers never mix the two scales. Pure interpretation, deterministic
(sorted iteration only), node/edge agnostic — the master plan prices
*simulated* post-step graphs with the same function.
"""
from __future__ import annotations

from collections import defaultdict, deque


def compute_cost_model(modules, edges, work, churn=None, *,
                       cores: int = 1) -> dict:
    """Price one module graph. Returns the dict described in the module doc.

    ``modules`` is the node id set; ``edges`` maps ``(a, b) -> weight`` with
    *a depends on b*; ``work`` maps module id -> compile work (seconds or
    type count — caller's choice of unit); ``churn`` maps module id -> commits
    touching it (``None`` ⇒ structural fallback: every module weighs 1).
    """
    mods = sorted(modules)
    fwd: dict[str, set[str]] = defaultdict(set)   # m -> deps
    rev: dict[str, set[str]] = defaultdict(set)   # m -> dependents
    for (a, b) in sorted(edges):
        if a == b:
            continue
        fwd[a].add(b)
        rev[b].add(a)

    # rebuild_work + dependent count: reverse-BFS per module. Module graphs are
    # small (tens to low hundreds), so the quadratic walk is cheap and simple.
    rebuild: dict[str, float] = {}
    dependents: dict[str, int] = {}
    for m in mods:
        seen = {m}
        dq = deque([m])
        while dq:
            for p in rev.get(dq.popleft(), ()):
                if p not in seen:
                    seen.add(p)
                    dq.append(p)
        dependents[m] = len(seen) - 1
        rebuild[m] = float(sum(work.get(x, 0) for x in seen))

    weight = (lambda m: churn.get(m, 0)) if churn is not None else (lambda m: 1)
    warm_cost = float(sum(weight(m) * rebuild[m] for m in mods))
    warm_worst = max(rebuild.values(), default=0.0)

    # cold_cost: work-weighted longest path through the depends-on closure.
    # Memoized DFS with a visiting guard — module graphs should be acyclic, but
    # a cycle must degrade (own work only), never recurse forever.
    _cw: dict[str, float] = {}
    _stack: set[str] = set()

    def chain(m: str) -> float:
        if m in _cw:
            return _cw[m]
        if m in _stack:
            return float(work.get(m, 0))
        _stack.add(m)
        deepest = max((chain(d) for d in sorted(fwd.get(m, ()))), default=0.0)
        _stack.discard(m)
        _cw[m] = float(work.get(m, 0)) + deepest
        return _cw[m]

    cold_cost = max((chain(m) for m in mods), default=0.0)
    total_work = float(sum(work.get(m, 0) for m in mods))
    resource_floor = total_work / cores if cores else total_work
    wall_floor = max(resource_floor, cold_cost)
    efficiency = round(resource_floor / wall_floor, 3) if wall_floor else 1.0

    return {
        "warm_cost": round(warm_cost, 1),
        "warm_worst": round(warm_worst, 1),
        "cold_cost": round(cold_cost, 1),
        "total_work": round(total_work, 1),
        "efficiency": efficiency,
        "rebuild": {m: round(rebuild[m], 1) for m in mods},
        "dependents": dependents,
    }


def compute_objective(module_graph: dict, *, cores: int | None = None) -> dict:
    """Price the *current* module graph (Build mode's nodes/edges/summary).

    Work is measured seconds when ``summary["measured"]``, else the type-count
    proxy; ``unit`` records which. Churn weights apply when
    ``summary["churned"]``. Ships as ``payload["objective"]``.
    """
    nodes = (module_graph or {}).get("nodes", [])
    edges = (module_graph or {}).get("edges", [])
    summary = (module_graph or {}).get("summary", {})
    measured = bool(summary.get("measured"))
    cores = cores or summary.get("cores") or 1

    work = {n["id"]: (n.get("build_ms", 0) / 1000.0 if measured
                      else float(n.get("types", 0))) for n in nodes}
    churn = ({n["id"]: n.get("churn", 0) for n in nodes}
             if summary.get("churned") else None)
    cost = compute_cost_model({n["id"] for n in nodes},
                              {(e["from"], e["to"]): e.get("w", 1)
                               for e in edges},
                              work, churn, cores=cores)
    cost["unit"] = "s" if measured else "types"
    cost["churned"] = churn is not None
    return cost
