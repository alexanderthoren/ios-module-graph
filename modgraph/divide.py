"""Divide one module into smaller SPM modules.

Counterpart to the whole-project migration plan: instead of asking "in what
order do I extract these folders into packages", this asks "I already have an
SPM module (or any folder) that grew too big — how do I split *it* into smaller
modules?".

The units are the module's **immediate subfolders** (a spike proved unsupervised
clustering over-fragments, ignores edge direction, and loses to the existing
folder structure on public-API cost). Everything load-bearing is reused:

  leaf_edges + pair_types  --aggregate by unit-->  unit graph (+ crossing types)
                                     |
        graph.compute_migration_plan  (SCC-bundle the unavoidable cycles, order)
        cycles.compute_cycle_breakers (feedback-arc-set for each cyclic bundle)
                                     |
              ordered extraction plan + public-API cost per candidate module

The one genuinely new metric is **public surface**: per candidate sub-module,
the count of its types referenced from another unit — every such type must flip
``internal`` -> ``public`` when the boundary becomes a module boundary. It is the
real cost of a split and discriminates hard (a leaf utils folder pays ~0; a
glue/repository folder pays most of its types).

Requires the USR-resolved index path: ``pair_types`` is empty on the regex-scan
fallback, so public surface can't be computed there.
"""
from __future__ import annotations

from collections import defaultdict

from .cycles import compute_cycle_breakers, compute_extraction_targets
from .graph import compute_migration_plan

# A folder qualifies as a "module you can divide" when it splits into at least
# this many type-declaring subfolders and holds at least this many types — below
# that there is nothing meaningful to divide.
MIN_UNITS = 2
MIN_TYPES = 12
PUBLIC_SAMPLE = 8


def unit_of(folder: str, prefix: str) -> str:
    """The immediate subfolder of *prefix* that *folder* lives in.

    ``prefix`` itself (a file declared directly in the module root) maps to the
    synthetic unit ``"(root)"`` so it is still a placeable sub-module.
    """
    rest = folder[len(prefix):].lstrip("/")
    return rest.split("/")[0] if rest else "(root)"


def _under(folder: str, prefix: str) -> bool:
    return folder == prefix or folder.startswith(prefix + "/")


def aggregate_module(
    prefix: str,
    leaf_edges: dict[tuple[str, str], int],
    pair_types: dict[tuple[str, str], set[str]],
    decls: dict[str, set[str]],
):
    """Collapse the folder graph inside *prefix* to its immediate-subfolder units.

    Returns ``(units, unit_edges, unit_pair_types, type_counts)`` where:
      units            set of unit ids (immediate subfolders, plus "(root)")
      unit_edges       (unit_a, unit_b) -> ref weight   [leaf_edges shape]
      unit_pair_types  (unit_a, unit_b) -> set of crossing type names
      type_counts      unit -> number of types declared in it
    """
    type_counts: dict[str, int] = defaultdict(int)
    units: set[str] = set()
    for folder, names in decls.items():
        if _under(folder, prefix):
            u = unit_of(folder, prefix)
            units.add(u)
            type_counts[u] += len(names)

    unit_edges: dict[tuple[str, str], int] = defaultdict(int)
    unit_pair_types: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (a, b), w in leaf_edges.items():
        if not (_under(a, prefix) and _under(b, prefix)):
            continue
        ua, ub = unit_of(a, prefix), unit_of(b, prefix)
        if ua == ub:
            continue  # same future module — not a boundary
        units.add(ua)
        units.add(ub)
        unit_edges[(ua, ub)] += w
        if pair_types:
            unit_pair_types[(ua, ub)] |= pair_types.get((a, b), set())
    return units, dict(unit_edges), dict(unit_pair_types), dict(type_counts)


def public_surface(
    unit_pair_types: dict[tuple[str, str], set[str]]
) -> dict[str, set[str]]:
    """unit -> set of its types referenced from *other* units (-> must go public).

    A type owned by unit ``b`` and referenced by some unit ``a`` (a != b) becomes
    part of ``b``'s public API surface once ``b`` is its own module.
    """
    pub: dict[str, set[str]] = defaultdict(set)
    for (_a, b), types in unit_pair_types.items():
        pub[b] |= types
    return pub


def compute_division_plan(
    prefix: str,
    leaf_edges: dict[tuple[str, str], int],
    pair_types: dict[tuple[str, str], set[str]],
    decls: dict[str, set[str]],
) -> dict:
    """Full division analysis for the module rooted at *prefix*.

    Mirrors the enrichment ``cli.main`` does for the whole-project plan: each
    cyclic step carries its feedback-arc-set breakers and per-unit extraction
    candidates. The returned dict is the single contract consumed by both the
    markdown writer and the HTML payload — keep them in step.
    """
    units, unit_edges, unit_pair_types, type_counts = aggregate_module(
        prefix, leaf_edges, pair_types, decls
    )
    pub = public_surface(unit_pair_types)

    unit_rows = []
    for u in sorted(units, key=lambda x: (-len(pub.get(x, set())), x)):
        n_types = type_counts.get(u, 0)
        pub_names = sorted(pub.get(u, set()))
        unit_rows.append({
            "unit": u,
            "types": n_types,
            "public": len(pub_names),
            "public_pct": round(100 * len(pub_names) / n_types) if n_types else 0,
            "public_sample": pub_names[:PUBLIC_SAMPLE],
        })

    plan, stuck = compute_migration_plan(unit_edges, units)
    for step in plan:
        if step["is_cycle"]:
            br = compute_cycle_breakers(step["folders"], unit_edges, unit_pair_types)
            step["breakers"] = br["edges"]
            step["fas_estimate"] = br["total_fas_estimate"]
            step["extractions"] = compute_extraction_targets(
                step["folders"], unit_edges, unit_pair_types
            )
        else:
            step["breakers"] = []
            step["fas_estimate"] = 0
            step["extractions"] = []

    # The full inter-unit edge list (sorted for determinism) lets the HTML draw a
    # before/after graph per step without recomputing anything in the browser.
    edge_rows = [
        {"src": a, "dst": b, "w": w, "types": sorted(unit_pair_types.get((a, b), set()))}
        for (a, b), w in sorted(unit_edges.items())
    ]

    return {
        "module": prefix,
        "unit_count": len(units),
        "type_total": sum(type_counts.values()),
        "edge_count": len(unit_edges),
        "total_public": sum(len(v) for v in pub.values()),
        "units": unit_rows,
        "unit_edges": edge_rows,
        "plan": plan,
        "stuck": [sorted(s) for s in stuck],
    }


def dividable_modules(
    decls: dict[str, set[str]],
    min_units: int = MIN_UNITS,
    min_types: int = MIN_TYPES,
) -> list[str]:
    """Folder prefixes worth offering a 'divide' on, sorted deterministically.

    A prefix qualifies when its declared types span at least *min_units*
    immediate subfolders and total at least *min_types*. Candidate prefixes are
    every proper ancestor directory of a type-declaring folder (so intermediate
    tree nodes like ``…/FeverUI`` qualify even when they declare nothing
    directly).
    """
    # ancestor dir -> {immediate unit -> type count}
    by_prefix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for folder, names in decls.items():
        parts = folder.split("/")
        for depth in range(len(parts)):
            prefix = "/".join(parts[:depth + 1])
            # `folder` sits under (or is) `prefix`; record its unit beneath prefix.
            unit = unit_of(folder, prefix)
            by_prefix[prefix][unit] += len(names)
    out = []
    for prefix, units in by_prefix.items():
        type_bearing = {u for u, n in units.items() if n > 0}
        if len(type_bearing) >= min_units and sum(units.values()) >= min_types:
            out.append(prefix)
    return sorted(out)
