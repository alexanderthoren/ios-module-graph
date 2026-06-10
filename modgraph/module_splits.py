"""Composite-module split detector: low-level cores trapped in high modules.

A module's build ``level`` is set by its deepest dependency chain — one
subfolder importing a high-level SDK pins the *whole* module (and every
consumer waiting on it) at that height, even when most of its code needs
nothing. This module finds those cases (study 2026-06-10, level-aware quick
wins): for each migrated SPM module it aggregates the internals to
immediate-subfolder **units** (:func:`modgraph.divide.aggregate_module` — the
boundary a split would actually use) and computes each unit's **intrinsic
level** from its module-leaving edges only: ``0`` when it references no other
first-party module, else ``1 + max(level of referenced modules)``.

The signal is the **level spread** — the gap between the module's level and
its lowest-intrinsic-level type-bearing unit. The payoff is the consumers
that could drop their effective dependency height: a consuming module whose
references land only in low units could depend on the split-off low module
instead. The cost is the public surface the split creates *internally* —
low-unit types referenced by the remaining units must flip
``internal`` -> ``public`` (types already used from outside are public
today). Candidates rank by ``releasable-consumers x spread / public-cost``,
the same payoff-over-effort shape as the quick-win ROI.

Advisory, like everything here: the spread says a split *pays*; whether the
unit boundary makes *domain* sense is the reviewer's call (the per-candidate
review prompt in the Quick wins tab carries this data for exactly that
judgment). Index path only — without ``pair_types`` the public cost can't be
priced, so the detector reports unavailable (same stance as
:mod:`modgraph.divide`). Deterministic: sorted iteration only.

Ships as ``payload["module_splits"]``.
"""
from __future__ import annotations

from collections import defaultdict

from .divide import MIN_TYPES, MIN_UNITS, PUBLIC_SAMPLE, aggregate_module, unit_of
from .module_graph import module_of

# A split is only worth the surgery when the trapped core sits at least this
# many levels below the module — a 1-level gap disappears with normal drift.
MIN_SPREAD = 2


def compute_module_splits(module_graph: dict, leaf_edges: dict,
                          pair_types: dict, decls: dict,
                          migrated_prefixes: list[str]) -> dict:
    """Return ``{"items": [...], "summary": {...}}`` ranked split candidates.

    ``module_graph`` is :func:`modgraph.module_graph.compute_module_graph`
    output (node ``id`` doubles as the target's source-folder prefix);
    ``leaf_edges``/``pair_types``/``decls`` are the full index-wide maps —
    intra-module folders included (the index loader keeps them; only the
    migration plan filters them out). Empty ``pair_types`` (regex path) →
    ``summary["available"] = False`` and no items.
    """
    if not pair_types:
        return {"items": [], "summary": {"available": False, "scanned": 0,
                                         "candidates": 0}}

    levels = {n["id"]: n.get("level", 0) for n in module_graph.get("nodes", [])}
    labels = {n["id"]: n.get("label", n["id"])
              for n in module_graph.get("nodes", [])}
    spm_ids = sorted(n["id"] for n in module_graph.get("nodes", [])
                     if n.get("kind") == "spm")

    items: list[dict] = []
    for mid in spm_ids:
        units, _unit_edges, unit_pair_types, type_counts = aggregate_module(
            mid, leaf_edges, pair_types, decls)
        type_bearing = sorted(u for u in units if type_counts.get(u, 0) > 0)
        if len(type_bearing) < MIN_UNITS or sum(type_counts.values()) < MIN_TYPES:
            continue

        # Intrinsic level per unit: module-leaving edges only. Internal unit-
        # to-unit edges don't count — they stay inside whatever split happens
        # and the divide plan orders them.
        unit_ext: dict[str, set[str]] = defaultdict(set)
        consumer_units: dict[str, set[str]] = defaultdict(set)
        for (a, b), _w in sorted(leaf_edges.items()):
            a_in = a == mid or a.startswith(mid + "/")
            b_in = b == mid or b.startswith(mid + "/")
            if a_in and not b_in:
                m = module_of(b, migrated_prefixes)
                if m != mid:
                    unit_ext[unit_of(a, mid)].add(m)
            elif b_in and not a_in:
                consumer_units[module_of(a, migrated_prefixes)].add(
                    unit_of(b, mid))

        def intrinsic(u: str) -> int:
            ext = unit_ext.get(u)
            if not ext:
                return 0
            return 1 + max(levels.get(m, 0) for m in sorted(ext))

        intr = {u: intrinsic(u) for u in type_bearing}
        mod_level = levels.get(mid, 0)
        min_intr = min(intr.values())
        spread = mod_level - min_intr
        if spread < MIN_SPREAD:
            continue
        low_units = sorted(u for u in type_bearing if intr[u] == min_intr)
        low_set = set(low_units)

        # Consumers whose references land only in low units could retarget to
        # the split-off module and drop from L<mod_level> to L<min_intr>+1
        # effective dependency height. Upper bound by construction — the index
        # attributes type references; non-type couplings may pin them anyway.
        releasable = sorted(c for c, us in consumer_units.items()
                            if c != mid and us <= low_set)

        # Internal public cost: low-unit types the *remaining* units reference
        # must go public. Types consumed from outside the module are public
        # already and cost nothing extra.
        pub: set[str] = set()
        for (a, b), types in sorted(unit_pair_types.items()):
            if b in low_set and a not in low_set:
                pub |= types
        public_cost = len(pub)

        score = round(len(releasable) * spread / max(public_cost, 1), 2)
        items.append({
            "module": mid,
            "label": labels.get(mid, mid),
            "level": mod_level,
            "min_intrinsic": min_intr,
            "spread": spread,
            "low_units": low_units,
            "units": [{"unit": u, "types": type_counts.get(u, 0),
                       "intrinsic_level": intr[u]}
                      for u in sorted(type_bearing,
                                      key=lambda x: (intr[x], x))],
            "consumers": len(consumer_units),
            "releasable": [labels.get(c, c) for c in releasable],
            "public_cost": public_cost,
            "public_sample": sorted(pub)[:PUBLIC_SAMPLE],
            "score": score,
        })

    items.sort(key=lambda i: (-i["score"], i["module"]))
    summary = {"available": True, "scanned": len(spm_ids),
               "candidates": len(items)}
    return {"items": items, "summary": summary}
