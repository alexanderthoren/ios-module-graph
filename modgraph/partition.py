"""Usage-cohort partition: split ONE module along how it is *used*.

:mod:`modgraph.divide` splits a module along its immediate **subfolders** — a
proxy that works only when the folder layout already mirrors usage. This
module computes the split from first principles, at declaration granularity,
driven by the two things that actually matter for build cost:

* **how the module is consumed** — for every external consumer, the set of
  in-module types it references plus everything those types transitively use
  inside the module (the *drag closure*, same machinery as
  :mod:`modgraph.isolate` scaled from one seed type to every consumer);
* **what a split would do to warm/cold builds** — each candidate partition is
  priced by replaying it into the module graph and re-running
  :func:`modgraph.objective.compute_cost_model`.

Construction (acyclic by construction, no post-hoc repair):

1. Build the intra-module type graph from ``type_edges`` and condense its
   SCCs — mutually-referencing types are welded and can never cross a module
   boundary (the escape hatch is an inversion, reported, not assumed).
2. For each external consumer boundary (a migrated module, or an app
   top-level folder — the boundary it will most plausibly become), take the
   closure of the types it references.
3. Types reachable from **two or more** cohorts form the **core** — the
   shared bottom every part may depend on. Each cohort keeps its *exclusive*
   remainder. Exclusive sets are pairwise disjoint, and an exclusive type can
   only reference its own cohort or the core (anything reachable from two
   cohorts is core by definition) — so parts never reference each other and
   the quotient is a DAG without any legality search.
4. Types no consumer reaches stay in the **rump** (the module keeps them);
   identical exclusives merge; exclusives below ``PARTITION_MIN_PART_TYPES``
   fold back into the rump (a boundary that small frees nobody).

When the core swallows the module (``PARTITION_CORE_MAX_PCT``) or every
cohort collapses into one, there is **no usage seam** — and saying so is the
result: the verdict ships the blocking hub types (the SCCs most cohorts
share, heaviest external mass first) so the isolate/invert actions that would
*create* the seam are one click away. The honest failure mirrors the
quick-win REJECT-is-first-class stance.

Index path only (``type_edges`` are empty on the regex-scan fallback).
Deterministic: sorted iteration only. Ships as ``payload["partitions"]``.
"""
from __future__ import annotations

from collections import defaultdict, deque

from .config import (PARTITION_BLOCKER_SAMPLE, PARTITION_CORE_MAX_PCT,
                     PARTITION_MIN_PART_TYPES, PARTITION_SAMPLE)
from .graph import _tarjan_sccs
from .module_graph import APP_ID, module_of
from .objective import compute_cost_model


def _split_token(tok: str) -> tuple[str, str]:
    name, sep, folder = tok.partition("\t")
    return name, (folder if sep else "")


def _under(folder: str, prefix: str) -> bool:
    return folder == prefix or folder.startswith(prefix + "/")


def _consumer_boundary(folder: str, migrated_prefixes: list[str]) -> str:
    """The consumer's *future* module boundary (mirrors master_plan's
    consumer counting): a migrated consumer is its build module; an
    app-target consumer is its top-level folder."""
    m = module_of(folder, migrated_prefixes)
    return m if m != APP_ID else folder.split("/", 1)[0]


def compute_partition(prefix: str, type_edges: list[dict],
                      type_kinds: dict[str, str],
                      migrated_prefixes: list[str],
                      module_graph: dict | None = None) -> dict:
    """Partition the module rooted at *prefix* by consumer-usage cohorts.

    Returns ``{"module", "verdict": "ok"|"no_seam", "parts": [...],
    "core": {...}, "rump": {...}, "blockers": [...], "sim": {...},
    "summary": {...}}``. ``parts`` excludes the core and the rump; each part
    carries the consumers it serves, its types (sample), public surface and
    freed external reference mass. ``sim`` prices the split against the
    current module graph when one is supplied.
    """
    prefixes = migrated_prefixes or []
    # ── intra-module graph + external touch points (one pass) ───────────────
    in_tokens: set[str] = set()
    out_adj: dict[str, set[str]] = defaultdict(set)       # in-module uses
    seeds_by_consumer: dict[str, set[str]] = defaultdict(set)
    ext_refs: dict[str, int] = defaultdict(int)           # token -> inbound w
    ext_out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in type_edges:
        src, dst, w = e["src"], e["dst"], e["w"]
        if src == dst:
            continue
        _sn, sf = _split_token(src)
        _dn, df = _split_token(dst)
        s_in, d_in = _under(sf, prefix), _under(df, prefix)
        if s_in:
            in_tokens.add(src)
        if d_in:
            in_tokens.add(dst)
        if s_in and d_in:
            out_adj[src].add(dst)
        elif d_in:
            seeds_by_consumer[_consumer_boundary(sf, prefixes)].add(dst)
            ext_refs[dst] += w
        elif s_in:
            ext_out[src][module_of(df, prefixes)] += w

    n_types = len(in_tokens)
    base = {"module": prefix, "type_total": n_types}
    if not in_tokens or not seeds_by_consumer:
        return {**base, "verdict": "no_seam", "parts": [], "blockers": [],
                "core": {"types": 0, "share_pct": 0.0}, "rump": {"types": n_types},
                "sim": None,
                "summary": {"consumers": 0, "parts": 0,
                            "reason": "no external consumers reach it"}}

    # ── SCC-condense: welded clusters are atomic ─────────────────────────────
    sccs = [sorted(c) for c in _tarjan_sccs(in_tokens, out_adj)]
    scc_of = {t: i for i, comp in enumerate(sccs) for t in comp}
    scc_fwd: dict[int, set[int]] = defaultdict(set)
    for s, dsts in out_adj.items():
        for d in dsts:
            if scc_of[s] != scc_of[d]:
                scc_fwd[scc_of[s]].add(scc_of[d])

    def closure(seed_sccs: set[int]) -> set[int]:
        seen = set(seed_sccs)
        dq = deque(seed_sccs)
        while dq:
            for nxt in sorted(scc_fwd.get(dq.popleft(), ())):
                if nxt not in seen:
                    seen.add(nxt)
                    dq.append(nxt)
        return seen

    cohort_cl: dict[str, set[int]] = {}
    for c in sorted(seeds_by_consumer):
        cohort_cl[c] = closure({scc_of[t] for t in seeds_by_consumer[c]})

    # ── core = reachable from ≥2 cohorts; exclusives are disjoint ────────────
    reach_count: dict[int, int] = defaultdict(int)
    for cl in cohort_cl.values():
        for i in cl:
            reach_count[i] += 1
    core_sccs = {i for i, n in reach_count.items() if n >= 2}
    scc_size = {i: len(comp) for i, comp in enumerate(sccs)}

    # Merge consumers whose exclusive remainder is identical (same slice).
    by_exclusive: dict[tuple[int, ...], list[str]] = defaultdict(list)
    for c in sorted(cohort_cl):
        excl = tuple(sorted(cohort_cl[c] - core_sccs))
        by_exclusive[excl].append(c)

    parts: list[dict] = []
    rump_sccs = set(range(len(sccs))) - core_sccs
    for excl, consumers in sorted(by_exclusive.items()):
        size = sum(scc_size[i] for i in excl)
        if not excl or size < PARTITION_MIN_PART_TYPES:
            continue  # too small to be a boundary — stays in the rump
        tokens = sorted(t for i in excl for t in sccs[i])
        rump_sccs -= set(excl)
        names = [_split_token(t)[0] for t in tokens]
        freed = sum(ext_refs.get(t, 0) for t in tokens)
        # Public surface of the part: externally referenced, or referenced
        # by in-module types outside the part.
        part_set = set(tokens)
        public = sorted(
            _split_token(t)[0] for t in tokens
            if ext_refs.get(t, 0) > 0
            or any(t in out_adj.get(s, ()) for s in in_tokens - part_set))
        parts.append({
            "consumers": consumers,
            "types": size,
            "type_sample": names[:PARTITION_SAMPLE],
            "tokens": tokens,
            "ext_refs": freed,
            "public": len(public),
            "public_sample": public[:PARTITION_SAMPLE],
        })
    parts.sort(key=lambda p: (-p["ext_refs"], -p["types"],
                              p["consumers"][0] if p["consumers"] else ""))

    core_types = sum(scc_size[i] for i in core_sccs)
    core_share = round(100.0 * core_types / n_types, 1) if n_types else 0.0
    rump_types = sum(scc_size[i] for i in rump_sccs)

    # ── blockers: the SCC hubs most cohorts share, heaviest first ───────────
    blockers: list[dict] = []
    for i in sorted(core_sccs,
                    key=lambda i: (-reach_count[i],
                                   -sum(ext_refs.get(t, 0) for t in sccs[i]),
                                   sccs[i][0])):
        toks = sccs[i]
        name, folder = _split_token(toks[0])
        blockers.append({
            "type": name,
            "folder": folder,
            "kind": type_kinds.get(name, ""),
            "welded": len(toks),
            "cohorts": reach_count[i],
            "ext_refs": sum(ext_refs.get(t, 0) for t in toks),
        })
        if len(blockers) >= PARTITION_BLOCKER_SAMPLE:
            break

    verdict = "ok" if (len(parts) >= 2 and core_share <= PARTITION_CORE_MAX_PCT
                       ) else "no_seam"
    reason = ""
    if verdict == "no_seam":
        reason = (f"core holds {core_share}% of the module"
                  if core_share > PARTITION_CORE_MAX_PCT else
                  f"{len(parts)} usage cohort(s) — nothing to separate")

    # ── price the split against the module graph (warm/cold deltas) ─────────
    # token -> destination zone: a kept part's index, "core", or "rump"
    # (small folded exclusives and unreached types stay in the rump).
    token_zone: dict[str, object] = {}
    for i, p in enumerate(parts):
        for t in p["tokens"]:
            token_zone[t] = i
    for i in core_sccs:
        for t in sccs[i]:
            token_zone.setdefault(t, "core")
    for t in in_tokens:
        token_zone.setdefault(t, "rump")
    sim = None
    if verdict == "ok" and module_graph:
        sim = _price_partition(prefix, parts, core_types, rump_types,
                               token_zone, out_adj, ext_out,
                               seeds_by_consumer, module_graph, prefixes)

    return {
        **base, "verdict": verdict,
        "parts": [{k: v for k, v in p.items() if k != "tokens"}
                  for p in parts],
        "core": {"types": core_types, "share_pct": core_share},
        "rump": {"types": rump_types},
        "blockers": blockers,
        "sim": sim,
        "summary": {"consumers": len(seeds_by_consumer),
                    "parts": len(parts), "reason": reason},
    }


def _price_partition(prefix, parts, core_types, rump_types, token_zone,
                     out_adj, ext_out, seeds_by_consumer,
                     module_graph, prefixes) -> dict:
    """Warm/cold cost before vs after the split, over the real module graph.

    Replaces the module's node with part/core/rump nodes; consumer edges land
    on the slice that carries their seeds; the module's own external
    dependencies follow the slice that uses them. Work proxy is the type
    count; the module's churn splits by type share.
    """
    nodes = module_graph.get("nodes", [])
    edges = module_graph.get("edges", [])
    mid = module_of(prefix, prefixes)
    work = {n["id"]: float(n.get("types", 0)) for n in nodes}
    churned = bool(module_graph.get("summary", {}).get("churned"))
    churn = ({n["id"]: n.get("churn", 0) for n in nodes} if churned else None)
    eset = {(e["from"], e["to"]): e.get("w", 1) for e in edges}
    before = compute_cost_model({n["id"] for n in nodes}, eset, work, churn)

    core_id, rump_id = f"{mid}::core", f"{mid}::rump"

    def zone_id(tok: str) -> str:
        z = token_zone.get(tok, "rump")
        if z == "core":
            return core_id
        if z == "rump":
            return rump_id
        return f"{mid}::part{z}"

    new_nodes = {n["id"]: float(n.get("types", 0)) for n in nodes
                 if n["id"] != mid}
    n_total = max(sum(p["types"] for p in parts) + core_types + rump_types, 1)
    for i, p in enumerate(parts):
        new_nodes[f"{mid}::part{i}"] = float(p["types"])
    if core_types:
        new_nodes[core_id] = float(core_types)
    if rump_types:
        new_nodes[rump_id] = float(rump_types)

    def alive(z: str) -> str:
        """An empty core/rump can't receive edges — degrade to the other."""
        if z in new_nodes:
            return z
        return rump_id if rump_id in new_nodes else core_id

    new_edges: dict[tuple[str, str], int] = defaultdict(int)
    for (a, b), w in sorted(eset.items()):
        if a != mid and b != mid:
            new_edges[(a, b)] += w
    # consumer -> the slice it uses (its part, and the core when reached)
    for c in sorted(seeds_by_consumer):
        cmod = c if c in new_nodes else APP_ID
        targets = {alive(zone_id(t)) for t in sorted(seeds_by_consumer[c])}
        for tgt in sorted(targets):
            if tgt in new_nodes and cmod in new_nodes and cmod != tgt:
                new_edges[(cmod, tgt)] += 1
    # intra-module edges crossing slice boundaries
    for s in sorted(out_adj):
        ps = alive(zone_id(s))
        for d in sorted(out_adj[s]):
            pd = alive(zone_id(d))
            if ps != pd and ps in new_nodes and pd in new_nodes:
                new_edges[(ps, pd)] += 1
    # the module's external dependencies follow the slice that uses them
    for s in sorted(ext_out):
        ps = alive(zone_id(s))
        for dmod, w in sorted(ext_out[s].items()):
            if dmod in new_nodes and ps in new_nodes and ps != dmod:
                new_edges[(ps, dmod)] += w

    new_churn = None
    if churn is not None:
        new_churn = {m: c for m, c in churn.items() if m != mid}
        mc = churn.get(mid, 0)
        for i, p in enumerate(parts):
            new_churn[f"{mid}::part{i}"] = round(mc * p["types"] / n_total)
        if core_types:
            new_churn[core_id] = round(mc * core_types / n_total)
        if rump_types:
            new_churn[rump_id] = round(mc * rump_types / n_total)
    after = compute_cost_model(set(new_nodes), dict(new_edges), new_nodes,
                               new_churn)
    return {
        "warm_cost": f"{before['warm_cost']} → {after['warm_cost']}",
        "warm_delta": round(after["warm_cost"] - before["warm_cost"], 1),
        "cold_cost": f"{before['cold_cost']} → {after['cold_cost']}",
        "cold_delta": round(after["cold_cost"] - before["cold_cost"], 1),
        "modules": f"{len(nodes)} → {len(new_nodes)}",
        "unit": "types",
    }


def compute_partitions(prefixes_to_try, type_edges, type_kinds,
                       migrated_prefixes, module_graph=None) -> dict[str, dict]:
    """One partition analysis per candidate module, keyed by prefix.

    Mirrors :func:`modgraph.isolate.compute_isolations`: candidates come from
    the same :func:`modgraph.isolate.isolatable_modules` gate (real compile
    units only). Modules with no external consumers are dropped; ``no_seam``
    verdicts are KEPT — the blockers they carry are the actionable output.
    """
    out: dict[str, dict] = {}
    for prefix in prefixes_to_try:
        res = compute_partition(prefix, type_edges, type_kinds,
                                migrated_prefixes, module_graph)
        if res["summary"]["consumers"]:
            out[prefix] = res
    return out
