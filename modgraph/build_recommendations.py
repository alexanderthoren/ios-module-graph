"""Rank modules by the build-time payoff of splitting them.

Operates on the module graph (real compile units). The lever is **separating a
module** — either splitting an SPM target into smaller targets, or (for the app
target) extracting features into SPM. Each module is scored on how much that would
cut build time, structurally (no git churn):

* **warm payoff** — ``downstream_cost`` = Σ compile-cost of every module that
  recompiles when this one changes. Splitting a module localizes edits, so the
  warm cascade shrinks toward zero; this sum is the work that's on the table.
* **cold payoff** — ``cold_score`` = the module's own compile cost **iff it sits on
  the cold critical path** (the longest dependency chain that floors a clean
  build). Splitting a big serial critical-path module lets its pieces compile in
  parallel, shaving up to that cost off the floor. Off-path modules give no cold
  benefit (splitting them doesn't shorten the longest chain).

``combined`` = ``50·warm/maxWarm + 50·cold/maxCold`` (0–100), the ranking key — so
the list is ordered by total build-time improvement, surfacing modules that help
warm, cold, or both. Compile cost is proxied by declared-type count (falls back to
folder count). Deterministic: sorted by combined desc, then warm, then id.

A module is *dividable* when a precomputed division plan exists for it (it has
sub-folder structure); the UI links those rows straight to the Divide modal.
"""
from __future__ import annotations

from collections import defaultdict, deque


def _cost(node: dict) -> int:
    return node.get("types") or node.get("folders") or 1


def compute_split_recommendations(module_graph: dict, divisions: dict | None = None) -> dict:
    """Return ``{"items": [...], "summary": {...}}`` ranked by split payoff."""
    divisions = divisions or {}
    nodes = module_graph.get("nodes", [])
    edges = module_graph.get("edges", [])
    by_id = {n["id"]: n for n in nodes}
    cost = {n["id"]: _cost(n) for n in nodes}

    # Reverse adjacency: edge from→to means "from depends on to", so a change to
    # `to` rebuilds `from`. radj[to] = direct dependents.
    radj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        radj[e["to"]].append(e["from"])

    def dependents(mid: str) -> set[str]:
        seen: set[str] = set()
        dq = deque([mid])
        while dq:
            x = dq.popleft()
            for p in radj.get(x, ()):
                if p not in seen and p != mid:
                    seen.add(p)
                    dq.append(p)
        return seen

    raw = []
    for n in nodes:
        mid = n["id"]
        deps = dependents(mid)
        downstream_cost = sum(cost[d] for d in deps)
        crit = bool(n.get("crit"))
        cold_score = cost[mid] if crit else 0
        div = divisions.get(mid)
        raw.append({
            "id": mid,
            "label": n.get("label", mid),
            "kind": n.get("kind", "spm"),
            "cost": cost[mid],
            "dependents": len(deps),
            "downstream_cost": downstream_cost,
            "crit": crit,
            "warm_score": downstream_cost,
            "cold_score": cold_score,
            "dividable": div is not None,
            "n_subunits": (div or {}).get("unit_count", 0),
            "split_public": (div or {}).get("total_public", 0),
        })

    max_warm = max((r["warm_score"] for r in raw), default=0) or 1
    max_cold = max((r["cold_score"] for r in raw), default=0) or 1
    for r in raw:
        r["warm_norm"] = round(50.0 * r["warm_score"] / max_warm, 1)
        r["cold_norm"] = round(50.0 * r["cold_score"] / max_cold, 1)
        r["combined"] = round(r["warm_norm"] + r["cold_norm"], 1)
        r["action"], r["reason"] = _advise(r)

    # Only modules with something to gain; ordered by total payoff.
    items = [r for r in raw if r["combined"] > 0]
    items.sort(key=lambda r: (-r["combined"], -r["warm_score"], r["id"]))

    summary = {
        "modules": len(nodes),
        "actionable": len(items),
        "dividable": sum(1 for r in items if r["dividable"]),
        "addressable_warm_cost": sum(r["downstream_cost"] for r in raw),
        "crit_path_cost": sum(r["cold_score"] for r in raw),
    }
    return {"items": items, "summary": summary}


def _advise(r: dict) -> tuple[str, str]:
    """(action, reason) for a recommendation row."""
    parts = []
    if r["dependents"]:
        parts.append(f"editing it recompiles {r['dependents']} module(s)"
                     f" (~{r['downstream_cost']} types of downstream work)")
    if r["crit"]:
        parts.append(f"on the cold critical path (+{r['cost']} types to the floor;"
                     f" splitting lets its pieces compile in parallel)")
    reason = "; ".join(parts).capitalize() + "." if parts else "Low build-time leverage."

    if r["kind"] == "app":
        return ("Extract features into SPM", reason
                + " The app target compiles as one unit and tops the chain — pull"
                  " features out (Migration mode) for the biggest cold-build win.")
    if r["dividable"]:
        return (f"Split into ~{r['n_subunits']} sub-modules", reason
                + f" {r['split_public']} type(s) would need to become public.")
    return ("Stabilize / narrow public API", reason
            + " No sub-folder structure to split on — lower its blast radius by"
              " shrinking and freezing its public interface.")
