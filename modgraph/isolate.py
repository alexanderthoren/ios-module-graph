"""Type-isolation engine: pull the single hottest type out of one module.

The migration plan extracts **whole folders** in dependency order; :mod:`divide`
splits one module into smaller modules along its **subfolder** seams. This module
answers a third, finer question — the one a developer actually asks when staring
at a module that everything depends on:

    "Inside *Containers* (or any module), which **type** is referenced the most —
     from outside the module *and* from inside it — so that if I extract *that
     one type* into its own module, every outside consumer can depend on the
     small new module instead of the whole of *Containers*?"

That is the highest-leverage decoupling move available: a single type with a
large external fan-in is the reason half the app links against the module. Lift
it out and the dependency collapses onto a leaf.

What the analysis computes, per module ``prefix``, for each declared type:

* **Reference mass** — ``ext_refs`` (references reaching the type from *outside*
  the module) and ``int_refs`` (from *inside* it). The headline ranking is the
  sum: the most-used type, exactly as asked. ``ext_modules`` counts the distinct
  *build modules* (via :func:`modgraph.module_graph.module_of`) that would be
  freed — the real prize, since each stops depending on the whole parent.
* **Drag closure** — extracting a type ``T`` is only honest if you also move
  everything ``T`` transitively uses *within the module* (otherwise the new
  module would point back at its parent and form a cycle). The closure is that
  set; its size is the true cost of the cut, and the types it pulls are listed
  (``pulls``) with their own external fan-in so a heavy hub (drags a third of the
  module) is visibly distinct from a clean leaf (drags nothing).
* **Public surface** — how many types in the new module are referenced from
  outside it (must flip ``internal`` → ``public``), mirroring :mod:`divide`.
* **ROI** — external reference mass freed over the size of the resulting module
  (closure + public surface). High ``ext_refs`` with a tiny closure wins; a type
  that drags half the module scores low even with a big fan-in.

Pure interpretation over ``type_edges`` (which carry the USR-resolved
``"<name>\\t<owner_folder>"`` token on both endpoints); deterministic (sorted
iteration only). Requires the index path — the regex-scan fallback emits no
``type_edges``, so isolations stay empty there. Ships as ``payload["isolations"]``
(``prefix`` → analysis), precomputed for every :func:`modgraph.divide.dividable_modules`
prefix so the HTML renders the "Isolate" view instantly client-side.
"""
from __future__ import annotations

from collections import defaultdict, deque

from .config import ISOLATE_MIN_EXT_REFS, ISOLATE_PULL_SAMPLE, ISOLATE_TOP_N
from .divide import dividable_modules
from .module_graph import APP_ID, module_of


def _split_token(tok: str) -> tuple[str, str]:
    """``"Name\\tfolder"`` → ``("Name", "folder")``; missing tab → empty folder."""
    name, sep, folder = tok.partition("\t")
    return name, (folder if sep else "")


def _under(folder: str, prefix: str) -> bool:
    return folder == prefix or folder.startswith(prefix + "/")


def compute_isolation_targets(
    prefix: str,
    type_edges: list[dict],
    type_kinds: dict[str, str],
    migrated_prefixes: list[str],
    *,
    min_ext_refs: int = ISOLATE_MIN_EXT_REFS,
    top_n: int = ISOLATE_TOP_N,
) -> dict:
    """Rank the types worth extracting out of the module rooted at *prefix*.

    Returns ``{"module", "type_total", "ext_total", "candidates": [...],
    "summary": {...}}``. Each candidate carries its reference mass, the build
    modules it would free, the in-module types it drags along (the closure), the
    resulting module's public surface, and an ROI. Candidates are sorted by total
    reference mass (most-used first); ties break toward the smaller drag closure
    (less surgery) then the name (determinism).

    Only types with ``ext_refs >= min_ext_refs`` are seeds — extracting a type no
    outside module references frees nobody. ``type_edges`` endpoints are the
    USR-resolved ``"name\\tfolder"`` tokens; *prefix* is the module's folder id.
    """
    # Incoming reference mass per in-module type token, split inside/outside, plus
    # the in-module out-adjacency that defines each type's drag closure. One pass.
    ext_refs: dict[str, int] = defaultdict(int)
    int_refs: dict[str, int] = defaultdict(int)
    ext_mods: dict[str, set[str]] = defaultdict(set)
    out_adj: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    in_module: set[str] = set()

    for e in type_edges:
        src, dst, w = e["src"], e["dst"], e["w"]
        if src == dst:
            continue
        _sn, sf = _split_token(src)
        _dn, df = _split_token(dst)
        s_in, d_in = _under(sf, prefix), _under(df, prefix)
        if d_in:
            in_module.add(dst)
            if s_in:
                int_refs[dst] += w
            else:
                ext_refs[dst] += w
                m = module_of(sf, migrated_prefixes)
                ext_mods[dst].add(m if m != APP_ID else APP_ID)
        if s_in:
            in_module.add(src)
        if s_in and d_in:
            out_adj[src][dst] += w

    # Freeze the defaultdicts the closure walk reads, so a stray subscript can't
    # insert phantom (zero-weight) successors mid-traversal.
    out_adj = {k: dict(v) for k, v in out_adj.items()}

    def closure(seed: str) -> set[str]:
        """Seed plus every in-module type it transitively uses — what must move."""
        seen = {seed}
        dq = deque([seed])
        while dq:
            for nxt in out_adj.get(dq.popleft(), ()):  # already in-module by build
                if nxt not in seen:
                    seen.add(nxt)
                    dq.append(nxt)
        return seen

    # A type in module N (a closure) is public when anything *outside* N references
    # it: an external module, or an in-module type not pulled into N.
    referrers: dict[str, set[str]] = defaultdict(set)  # dst token -> src tokens (in-module)
    for s, succs in out_adj.items():
        for d in succs:
            referrers[d].add(s)

    candidates: list[dict] = []
    seeds = sorted(t for t in in_module if ext_refs.get(t, 0) >= min_ext_refs)
    for seed in seeds:
        name, folder = _split_token(seed)
        cl = closure(seed)
        # Public surface of the would-be module: members referenced from outside it.
        public = 0
        for m in cl:
            if ext_refs.get(m, 0) > 0 or any(r not in cl for r in referrers.get(m, ())):
                public += 1
        # Reference mass / modules freed aggregate over the whole closure — every
        # member rides along, so every member's outside consumers are freed too.
        mod_ext = sum(ext_refs.get(m, 0) for m in cl)
        mods_freed: set[str] = set()
        for m in cl:
            mods_freed |= ext_mods.get(m, set())
        # Drag = closure minus the seed, heaviest external fan-in first.
        pulls = sorted(
            ((mm := _split_token(m))[0], type_kinds.get(mm[0], ""),
             ext_refs.get(m, 0), int_refs.get(m, 0), mm[1])
            for m in cl if m != seed
        )
        pulls.sort(key=lambda p: (-p[2], -p[3], p[0]))
        e, i = ext_refs.get(seed, 0), int_refs.get(seed, 0)
        size = len(cl)
        roi = round(mod_ext / (size + public), 2) if (size + public) else 0.0
        candidates.append({
            "type": name,
            "folder": folder,
            "kind": type_kinds.get(name, ""),
            "ext_refs": e,
            "int_refs": i,
            "total_refs": e + i,
            "ext_modules": len(mods_freed),
            "module_size": size,
            "module_public": public,
            "module_ext_refs": mod_ext,
            "module_ext_modules": len(mods_freed),
            "roi": roi,
            "pulls_total": len(cl) - 1,
            "pulls": [
                {"type": n, "kind": k, "ext_refs": pe, "int_refs": pi}
                for (n, k, pe, pi, _f) in pulls[:ISOLATE_PULL_SAMPLE]
            ],
        })

    candidates.sort(key=lambda c: (-c["total_refs"], c["module_size"], c["type"]))
    candidates = candidates[:top_n]

    ext_total = sum(ext_refs.values())
    top = candidates[0] if candidates else None
    summary = {
        "candidates": len(candidates),
        "ext_total": ext_total,
        # Concentration: how much of the module's external fan-in the single best
        # extraction would carry off. High share = one type is the whole reason.
        "top_share": (round(100 * top["module_ext_refs"] / ext_total)
                      if top and ext_total else 0),
    }
    return {
        "module": prefix,
        "type_total": len(in_module),
        "ext_total": ext_total,
        "candidates": candidates,
        "summary": summary,
    }


def isolatable_modules(
    decls: dict[str, set[str]], migrated_prefixes: list[str]
) -> list[str]:
    """Folder prefixes that are a single real *module* to isolate a type from.

    "Inside vs outside the module" is only meaningful at a real compile-unit
    boundary, so this is deliberately stricter than
    :func:`modgraph.divide.dividable_modules` (which returns every aggregating
    ancestor, e.g. ``Packages`` — useless here):

    * every **SPM target root** (a distinct :func:`module_of` value), and
    * every **dividable app subtree** whose folders all live in the app target
      (the not-yet-a-module case, e.g. a ``Containers/…`` feature tree) — the
      :func:`modgraph.divide.dividable_modules` size gate keeps these meaningful.

    An aggregating prefix that spans more than one compile unit (``Packages``,
    ``…/Sources`` holding several targets) is excluded — its "external" fan-in
    would be mislabelled.
    """
    targets: set[str] = set()
    for folder in decls:
        m = module_of(folder, migrated_prefixes)
        if m != APP_ID:
            targets.add(m)

    app_subtrees: set[str] = set()
    for p in dividable_modules(decls):
        members = [f for f in decls if _under(f, p)]
        if members and all(
            module_of(f, migrated_prefixes) == APP_ID for f in members
        ):
            app_subtrees.add(p)
    return sorted(targets | app_subtrees)


def compute_isolations(
    decls: dict[str, set[str]],
    type_edges: list[dict],
    type_kinds: dict[str, str],
    migrated_prefixes: list[str],
) -> dict[str, dict]:
    """Precompute an isolation analysis for every real module worth offering.

    Mirrors how ``cli.main`` precomputes :func:`modgraph.divide.compute_division_plan`
    for the "Divide" view: one entry per :func:`isolatable_modules` prefix, keyed
    by prefix, so the HTML's "Isolate" action renders client-side with no
    recompute. Modules whose hottest type frees nobody (no candidate clears
    ``min_ext_refs``) are dropped — there is nothing to isolate.
    """
    out: dict[str, dict] = {}
    for prefix in isolatable_modules(decls, migrated_prefixes):
        analysis = compute_isolation_targets(
            prefix, type_edges, type_kinds, migrated_prefixes
        )
        if analysis["candidates"]:
            out[prefix] = analysis
    return out
