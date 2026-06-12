"""Master plan: Migration mode's whole surface — one Setup, one Plan.

The :mod:`modgraph.advisor` arbiter already merges every advice stream into a
wave-ordered feed with a stop line. This module wraps it (wrap, don't fork)
and turns that feed into the *complete* migration product:

* **Setup** — a detection-driven, one-time checklist. Items are emitted only
  when the pipeline's own data says the project is missing them (no SPM
  packages yet, no API-suffixed modules, no measured build baseline, …).
* **Steps** — every advisor action, upgraded with the three things a feed row
  never carried:

  - a **shape** decision — *how* the code should ship: an ``api_impl`` pair
    (``<Name>API`` protocols/values + ``<Name>`` implementation, consumers
    rewired to the API, impl bound at the composition root), a
    ``single_module`` (one consumer — an API split here is a boundary wave 4
    would later fold), the quick-win ``absorb`` pick, or an ``api_retrofit``
    (an existing multi-consumer module gains the API package it is missing).
    The rule that fired ships with the step so the decision is auditable.
  - a **why** grounded in build cost — folder warm blast radius, critical
    path membership, churn, and **cumulative** post-step deltas: the plan is
    replayed step by step through :class:`_PlanSim` (folder edges mapped to
    the evolving module set, API rewires applied, costs re-priced by
    :func:`modgraph.objective.compute_cost_model`), so step N's numbers
    assume steps 1…N−1 landed — not a frozen baseline.
  - a **verify** block — the commands to run after the step and the metric
    movements to expect, so every step closes its own loop.

* **Trajectory** — one row per simulated step (modules, crit_len, warm/cold
  cost, app share), plus the projected end state, so "where does this plan
  take us" is answerable before the first PR. Simulated costs are always in
  type-units (measured seconds can't be attributed to modules that don't
  exist yet); the unit ships with the data.
* **Equilibrium** — the measurable definition of done: app target thin
  (``EQ_APP_SHARE_PCT``), warm blast radius bounded — churn-aware when churn
  data exists (only churn-hot modules count against ``EQ_WARM_MAX_PCT``;
  a stable foundation with wide fan-in is fine) — zero module cycles, API
  coverage for multi-consumer modules, cold-build parallelism efficiency
  (``EQ_PAR_EFF_PCT``, only when measured), and an empty action feed. Each
  criterion ships current/target/met, and ``projected`` carries the same
  numbers at the simulated end of the plan.

Pure interpretation over already-computed inputs; deterministic (sorted
iteration only); every stream optional (regex-scan path degrades to
``single_module``/``absorb`` shapes with an empty API surface and says so).
Ships as ``payload["master_plan"]`` — it replaces ``payload["advice"]`` as
Migration mode's data source.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .advisor import WAVE_LABELS, compute_advice
from .build_impact import compute_build_impact
from .config import (ABSORB_CHURN_HOT, ABSORB_WARM_WIDE, API_MIN_CONSUMERS,
                     API_SURFACE_SAMPLE, EQ_APP_SHARE_PCT, EQ_PAR_EFF_PCT,
                     EQ_WARM_MAX_PCT)
from .graph import build_tree
from .module_graph import APP_ID, module_of
from .objective import compute_cost_model

PHASE_LABELS = dict(WAVE_LABELS)

# Reference types with behavior become protocols in the API package; value
# types (and protocols themselves) move into the API package whole.
_PROTOCOL_KINDS = {"class", "actor"}

_RESOURCE_SAMPLE = 6


def _under(folder: str, prefix: str) -> bool:
    return folder == prefix or folder.startswith(prefix + "/")


# Folder names that are layer vocabulary, not domain vocabulary — a module
# named after one would say nothing ("ViewModel", "Actions"). Prefix parent
# segments until the name carries the feature.
_GENERIC_SEGMENTS = {
    "viewmodel", "viewmodels", "view", "views", "actions", "model", "models",
    "state", "reducers", "helpers", "utils", "common", "core", "extensions",
    "managers", "services", "protocols", "types", "sources",
}


def _module_name(folder: str) -> str:
    """A Swift-module-ish name from a folder id, alnum only.

    The last segment names the module; generic layer segments get their
    parent prefixed (``Features/Login/ViewModel`` → ``LoginViewModel``) so
    the suggested package says what it is, not what layer it is."""
    segs = [re.sub(r"[^A-Za-z0-9]", "", s)
            for s in folder.rstrip("/").split("/")]
    segs = [s for s in segs if s]
    if not segs:
        return "Module"
    name = segs[-1]
    i = len(segs) - 2
    while name.lower() in _GENERIC_SEGMENTS and i >= 0:
        name = segs[i] + name
        i -= 1
    return name or "Module"


def _api_surface(folders, pair_types: dict | None) -> list[str]:
    """Types declared under *folders* (one prefix or a set of folder ids) that
    other folders reference — the surface that goes ``public``, i.e. the
    contents of the API package."""
    if not pair_types:
        return []
    if isinstance(folders, str):
        prefix = folders
        inside = lambda f: _under(f, prefix)  # noqa: E731
    else:
        members = set(folders)
        inside = lambda f: f in members  # noqa: E731
    out: set[str] = set()
    for (a, b), types in pair_types.items():
        if inside(b) and not inside(a):
            out |= set(types)
    return sorted(out)


def _consumer_modules(folder: str, leaf_edges: dict,
                      migrated_prefixes: list[str]) -> list[str]:
    """Distinct current-or-future consumer modules referencing into *folder*.

    Migrated consumers count as their build module. App-target consumers count
    as their top-level folder — the boundary they will most plausibly become
    once their own extraction lands. (Counting today's compile units would
    collapse every app consumer into one and the API decision would never
    fire before the very end of a migration.)"""
    mods: set[str] = set()
    for (a, b) in leaf_edges or {}:
        if _under(b, folder) and not _under(a, folder):
            m = module_of(a, migrated_prefixes or [])
            mods.add(m if m != APP_ID else a.split("/", 1)[0])
    return sorted(mods)


class _PlanSim:
    """Cumulative what-if module graph: the plan replayed move by move.

    Holds the folder→module mapping as it *evolves* — every applied step
    (extraction, API retrofit, join) changes where folders land, and each
    :meth:`snapshot` re-derives the module set, edges, per-module work
    (type-count proxy) and churn from scratch under the current overrides.
    Step N is therefore priced against the world steps 1…N−1 produced, not
    against a frozen baseline. Deterministic: inputs are sorted once, every
    iteration below is over sorted data.
    """

    def __init__(self, leaf_edges: dict, prefixes: list[str], decls: dict,
                 churn_commits=None):
        self._edges = sorted((leaf_edges or {}).items())
        self._prefixes = prefixes or []
        self._ftypes = {f: len(ts) for f, ts in sorted((decls or {}).items())}
        self._churn = [sorted(s) for s in (churn_commits or [])]
        folders = set(self._ftypes)
        for (a, b), _w in self._edges:
            folders.add(a)
            folders.add(b)
        for cset in self._churn:
            folders.update(cset)
        self._folders = sorted(folders)
        # (folder prefix, api id or None, impl id) — longest prefix wins.
        self._over: list[tuple[str, str | None, str]] = []
        self._retro: dict[str, str] = {}   # module id -> its new API package id
        self._joined: dict[str, str] = {}  # module id -> folded-into id

    # -- mutations (one per applied plan step) ------------------------------
    def extract(self, folder: str, api_id: str | None, impl_id: str) -> None:
        self._over.append((folder.rstrip("/"), api_id, impl_id))

    def retrofit(self, mid: str, api_id: str) -> None:
        self._retro[mid] = api_id

    def join(self, mid: str, into: str) -> None:
        self._joined[mid] = into

    # -- mapping -------------------------------------------------------------
    def _route(self, folder: str) -> tuple[str, str | None]:
        """(module the folder's code lives in, API id consumers should hit)."""
        best: tuple[str, str | None, str] | None = None
        for ov in self._over:
            if _under(folder, ov[0]) and (best is None or
                                          len(ov[0]) > len(best[0])):
                best = ov
        if best is not None:
            return self._joined.get(best[2], best[2]), best[1]
        m = module_of(folder, self._prefixes)
        return self._joined.get(m, m), None

    def snapshot(self) -> dict:
        route = {f: self._route(f) for f in self._folders}
        mods: set[str] = set()
        medges: dict[tuple[str, str], int] = defaultdict(int)
        for (a, b), w in self._edges:
            ma, _ = route[a]
            mb, api_b = route[b]
            if ma == mb:
                continue
            dst = api_b or mb
            # Retrofit rewire: consumer edges land on the module's API
            # package; only the composition root (the app) keeps the impl.
            if dst in self._retro and ma != APP_ID:
                dst = self._retro[dst]
            mods.add(ma)
            mods.add(dst)
            if ma != dst:
                medges[(ma, dst)] += w
        for (_p, api_id, impl_id) in self._over:
            impl = self._joined.get(impl_id, impl_id)
            mods.add(impl)
            if api_id:
                mods.add(api_id)
                medges[(impl, api_id)] += 1
        for mid, api_id in sorted(self._retro.items()):
            mods.add(api_id)
            if mid in mods:
                medges[(mid, api_id)] += 1

        work: dict[str, float] = defaultdict(float)
        app_types = 0
        total_types = 0
        for f, n in self._ftypes.items():
            m = route[f][0]
            mods.add(m)
            work[m] += n
            total_types += n
            if m == APP_ID:
                app_types += n
        churn: dict[str, int] = defaultdict(int)
        for cset in self._churn:
            for m in sorted({route[f][0] for f in cset if f in route}):
                churn[m] += 1

        cost = compute_cost_model(mods, dict(medges), dict(work),
                                  dict(churn) if self._churn else None)
        tree = build_tree(mods, {m: set() for m in mods}, root_label="sim")
        bi = compute_build_impact(tree, dict(medges))
        n_mods = len(mods)
        warm_max = max(cost["dependents"].values(), default=0)
        return {
            "modules": n_mods,
            "crit_len": bi["summary"]["crit_len"],
            "n_cycles": bi["summary"]["n_cycles"],
            "warm_max": warm_max,
            "warm_max_pct": (round(100.0 * warm_max / (n_mods - 1), 1)
                             if n_mods > 1 else 0.0),
            "app_types": app_types,
            "app_share_pct": (round(100.0 * app_types / total_types, 1)
                              if total_types else 0.0),
            "warm_cost": cost["warm_cost"],
            "cold_cost": cost["cold_cost"],
            "dependents": cost["dependents"],
        }


def _public_snap(snap: dict) -> dict:
    """The snapshot fields that ship (the dependents map stays internal)."""
    return {k: v for k, v in snap.items() if k != "dependents"}


def _shape_for_extraction(folder: str, qw_item: dict, pair_types: dict | None,
                          type_kinds: dict | None, leaf_edges: dict,
                          migrated_prefixes: list[str], churned: bool) -> dict:
    """The shape decision for one folder extraction, with the rule that fired."""
    dest = qw_item.get("destination")
    if dest:
        return {
            "mode": "absorb",
            "rule": "absorb pick survived the level/churn/cycle vetoes — an "
                    "existing module already owns this boundary",
            "destination": dest.get("label", dest.get("module")),
            "api_module": None, "impl_module": None,
            "consumers": None, "api_surface_count": 0,
            "api_surface": [], "protocols_for": [],
        }
    consumers = _consumer_modules(folder, leaf_edges, migrated_prefixes)
    surface = _api_surface(folder, pair_types)
    kinds = type_kinds or {}
    protocols = [t for t in surface if kinds.get(t, "") in _PROTOCOL_KINDS]
    churn_hot = (churned and qw_item.get("churn", 0) >= ABSORB_CHURN_HOT
                 and qw_item.get("warm", 0) >= ABSORB_WARM_WIDE)
    if len(consumers) >= API_MIN_CONSUMERS:
        rule = (f"{len(consumers)} consumer module(s) — they should depend on "
                f"the protocol surface, not the implementation")
    elif churn_hot:
        rule = (f"churn-hot ({qw_item.get('churn', 0)} commit(s)) with "
                f"{qw_item.get('warm', 0)} dependent folder(s) — a stable API "
                f"stops the edit cascade")
    elif qw_item.get("crit"):
        rule = ("on the cold critical path — an API/impl pair turns the "
                "implementation into a parallelizable leaf")
    else:
        rule = ("one consumer, low churn, off the critical path — an API "
                "split here is a boundary the join wave would later fold")
    api = (len(consumers) >= API_MIN_CONSUMERS) or churn_hot or bool(
        qw_item.get("crit"))
    if not pair_types:
        api = False
        rule = ("regex-scan path: no USR-resolved type surface — ship a "
                "single module; rerun the index path for API planning")
    name = _module_name(folder)
    return {
        "mode": "api_impl" if api else "single_module",
        "rule": rule,
        "destination": None,
        "api_module": f"{name}API" if api else None,
        "impl_module": name,
        "consumers": len(consumers),
        "api_surface_count": len(surface),
        "api_surface": surface[:API_SURFACE_SAMPLE],
        "protocols_for": protocols[:API_SURFACE_SAMPLE],
    }


def _resources_under(resources: dict | None, folder: str) -> list[str]:
    names: list[str] = []
    for fid in sorted(resources or {}):
        if _under(fid, folder):
            names.extend(resources[fid])
    return names


def _types_under(decls: dict | None, folder: str) -> int:
    return sum(len(ts) for f, ts in (decls or {}).items() if _under(f, folder))


def _verify_block(expect: dict) -> dict:
    return {
        "commands": [
            "just refresh    # incremental re-index: structural delta, fast",
            "just check <graph.json> --against <baseline.json> "
            "--no-new-edges --no-new-cycles    # ratchet",
            "just clean && just tree    # periodic: measured wall + history row",
        ],
        "expect": expect,
    }


def _setup_items(module_graph: dict, history: list | None,
                 excluded_count: int) -> list[dict]:
    """Detection-driven one-time checklist; only applicable items emitted."""
    nodes = (module_graph or {}).get("nodes", [])
    spm = [n for n in nodes if n.get("kind") == "spm"]
    labels = {n.get("label", n["id"]) for n in spm}
    measured = (module_graph or {}).get("summary", {}).get("measured", False)
    items: list[dict] = []
    if not spm:
        items.append({
            "id": "setup:packages",
            "title": "Create the local-packages workspace layout",
            "why": "No SPM package exists yet — there is nowhere to extract "
                   "code to.",
            "how": [
                "Create a Packages/ directory next to the app project.",
                "Scaffold the first package (swift package init --type library).",
                "Add it as a local package to the workspace/project and link "
                "it from the app target.",
            ],
            "done_when": "The graph shows at least one SPM module.",
        })
    if not any(lb.endswith("API") for lb in labels):
        items.append({
            "id": "setup:api_convention",
            "title": "Adopt the <Name>API / <Name> pair convention",
            "why": "Consumers must depend on small, stable interface packages "
                   "so implementation edits stop cascading through warm builds.",
            "how": [
                "For each feature, plan two targets: <Name>API (protocols, "
                "value types, errors — depends on nothing or other APIs) and "
                "<Name> (the implementation — depends on its own API plus "
                "other modules' APIs, never their implementations).",
                "Document the rule: importing a non-API module outside the "
                "composition root is a review error.",
            ],
            "done_when": "At least one API/impl pair exists and the rule is "
                         "written down where reviews can point at it.",
        })
        items.append({
            "id": "setup:composition_root",
            "title": "Create the composition root in the app target",
            "why": "Someone has to bind each <Name>API protocol to its <Name> "
                   "implementation; doing it once at the app's entry point "
                   "keeps every other module implementation-free.",
            "how": [
                "Add a single wiring point (factory or DI container) at app "
                "startup that instantiates implementations and hands them out "
                "as API protocols.",
                "Only the composition root may import implementation modules.",
            ],
            "done_when": "Implementation modules are imported in exactly one "
                         "place: the composition root.",
        })
    items.append({
        "id": "setup:ratchet",
        "title": "Wire the architecture ratchet into CI",
        "why": "Each step's structural win must be locked in — new edges and "
               "cycles regress silently otherwise. Skip if already wired.",
        "how": [
            "Commit a baseline index_graph.json snapshot.",
            "Run `just check <graph.json> --against <baseline.json> "
            "--no-new-edges --no-new-cycles` in CI (exit 1 on violations).",
            "Refresh the baseline as part of each merged extraction PR.",
        ],
        "done_when": "CI fails on any new folder edge or cycle.",
    })
    if not measured or not history:
        items.append({
            "id": "setup:baseline",
            "title": "Capture the measured build baseline",
            "why": "Without a cold-build measurement the plan prices modules "
                   "by type counts and improvements can't be verified.",
            "how": [
                "Run one cold build: `just clean && just tree` — this captures "
                "per-module compile times (.swiftstats) and appends the first "
                "build_history.jsonl row.",
            ],
            "done_when": "Build mode shows measured seconds and the "
                         "Improvements tab has a baseline row.",
        })
    if excluded_count:
        items.append({
            "id": "setup:exclusions",
            "title": f"Review the {excluded_count} excluded folder(s)",
            "why": "Excluded folders block everything that depends on them "
                   "(SPM code can't link back into non-SPM app code).",
            "how": [
                "Open .modularization_excluded.json and confirm each entry is "
                "deliberate; unexclude anything that was a placeholder.",
            ],
            "done_when": "Every exclusion is intentional and documented.",
        })
    return items


def _equilibrium(module_graph: dict, actions: list[dict]) -> dict:
    """Measurable definition of done — each criterion current/target/met."""
    nodes = (module_graph or {}).get("nodes", [])
    edges = (module_graph or {}).get("edges", [])
    summary = (module_graph or {}).get("summary", {})
    total_types = sum(n.get("types", 0) for n in nodes)
    app_types = sum(n.get("types", 0) for n in nodes if n.get("kind") == "app")
    spm = [n for n in nodes if n.get("kind") == "spm"]
    labels = {n.get("label", n["id"]) for n in spm}
    n_modules = len(nodes)

    app_share = round(100.0 * app_types / total_types, 1) if total_types else 0.0
    n_cycles = summary.get("n_cycles", 0)

    # Warm bound — churn-aware when churn data exists: a stable foundation
    # with wide fan-in is *fine* (nobody edits it); the real offender is a
    # churn-hot module whose edits cascade widely. Without churn data the
    # blanket bound applies to every module (today's worst case).
    churned = bool(summary.get("churned"))
    if churned:
        offenders = sorted(
            n.get("label", n["id"]) for n in nodes
            if n.get("churn", 0) >= ABSORB_CHURN_HOT
            and n.get("warm_pct", 0.0) > EQ_WARM_MAX_PCT)
        warm_current = (f"{len(offenders)} churn-hot module(s) over "
                        f"{EQ_WARM_MAX_PCT}% warm radius"
                        + (f": {', '.join(offenders[:4])}" if offenders else ""))
        warm_met = bool(spm) and not offenders
        warm_target = (f"0 modules with ≥ {ABSORB_CHURN_HOT} commits and "
                       f"> {EQ_WARM_MAX_PCT}% warm radius")
    else:
        warm_max = max((n.get("warm", 0) for n in nodes), default=0)
        warm_pct = (round(100.0 * warm_max / (n_modules - 1), 1)
                    if n_modules > 1 else 0.0)
        warm_current = (f"worst module rebuilds {warm_pct}% of modules "
                        f"({warm_max} of {max(n_modules - 1, 0)}) — no churn "
                        f"data, every module counts")
        warm_met = bool(spm) and warm_pct <= EQ_WARM_MAX_PCT
        warm_target = f"≤ {EQ_WARM_MAX_PCT}%"

    direct_consumers: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        direct_consumers[e["to"]].add(e["from"])
    multi = [n for n in spm if len(direct_consumers.get(n["id"], ())) >=
             API_MIN_CONSUMERS and not n.get("label", "").endswith("API")]
    missing_api = sorted(n.get("label", n["id"]) for n in multi
                         if f"{n.get('label', n['id'])}API" not in labels)

    criteria = [
        {"id": "app_thin",
         "label": "App target is a composition root",
         "current": f"{app_share}% of types in the app target",
         "target": f"≤ {EQ_APP_SHARE_PCT}%",
         "met": bool(spm) and app_share <= EQ_APP_SHARE_PCT},
        {"id": "warm_bounded",
         "label": "Warm blast radius bounded"
                  + (" (churn-aware)" if churned else ""),
         "current": warm_current,
         "target": warm_target,
         "met": warm_met},
        {"id": "no_cycles",
         "label": "No module cycles",
         "current": f"{n_cycles} module cycle(s)",
         "target": "0",
         "met": n_cycles == 0},
        {"id": "api_coverage",
         "label": "Multi-consumer modules expose an API package",
         "current": (f"{len(missing_api)} module(s) missing an API "
                     f"counterpart" + (f": {', '.join(missing_api[:4])}"
                                       if missing_api else "")),
         "target": "0 (by the <Name>API naming convention)",
         "met": bool(spm) and not missing_api},
        {"id": "work_exhausted",
         "label": "No remaining action pays",
         "current": f"{len(actions)} action(s) still on the board",
         "target": "0 — neither splitting nor joining pays",
         "met": not actions},
    ]
    # Cold-build parallelism efficiency — only priceable with a measured
    # baseline (the setup checklist demands one): resource floor ÷ actual
    # wall floor. ≈1 ⇒ resource-bound (restructuring can't help); low ⇒
    # dependency-bound (chains serialize the build — flatten them).
    if summary.get("measured") and summary.get("est_wall_s"):
        cores = summary.get("cores") or 1
        resource = (summary.get("total_build_s", 0.0) or 0.0) / cores
        eff = round(100.0 * resource / summary["est_wall_s"], 1)
        criteria.insert(3, {
            "id": "parallel_efficiency",
            "label": "Cold build is resource-bound, not chain-bound",
            "current": f"{eff}% parallelism efficiency "
                       f"(resource floor ÷ estimated wall)",
            "target": f"≥ {EQ_PAR_EFF_PCT}%",
            "met": eff >= EQ_PAR_EFF_PCT})
    return {"criteria": criteria, "met": all(c["met"] for c in criteria)}


def compute_master_plan(quick_wins: dict, file_moves: dict,
                        isolations: dict[str, dict], module_splits: dict,
                        recommendations: dict, module_graph: dict, *,
                        pair_types: dict | None = None,
                        type_kinds: dict | None = None,
                        leaf_edges: dict | None = None,
                        migrated_prefixes: list[str] | None = None,
                        decls: dict | None = None,
                        resources: dict | None = None,
                        history: list | None = None,
                        excluded_count: int = 0,
                        churn_commits=None) -> dict:
    """Return ``{"setup", "steps", "deferred", "trajectory", "equilibrium",
    "summary"}``.

    Wraps :func:`modgraph.advisor.compute_advice` (which orders the work and
    draws the stop line) and decorates every action into a full step: shape
    decision, build-grounded why, cumulative simulated deltas, verify block.
    All keyword inputs are optional and degrade exactly like their producers.
    """
    advice = compute_advice(quick_wins, file_moves, isolations,
                            module_splits, recommendations, module_graph)
    leaf_edges = leaf_edges or {}
    prefixes = migrated_prefixes or []
    qw_by_folder = {i["folder"]: i for i in (quick_wins or {}).get("items", [])}
    churned = (quick_wins or {}).get("summary", {}).get("churned", False)
    nodes_by_id = {n["id"]: n for n in (module_graph or {}).get("nodes", [])}
    app_node = next((n for n in (module_graph or {}).get("nodes", [])
                     if n.get("kind") == "app"), None)
    measured = (module_graph or {}).get("summary", {}).get("measured", False)

    sim = _PlanSim(leaf_edges, prefixes, decls or {}, churn_commits)
    baseline = sim.snapshot()
    prev = baseline
    trajectory: list[dict] = []

    steps: list[dict] = []
    for a in advice["actions"]:
        kind, subject = a["kind"], a["subject"]
        step = {
            "id": a["id"], "kind": kind, "phase": a["wave"],
            "subject": subject, "title": a["title"],
            "after": a["after"], "unblocks": a["unblocks"],
            "details": a["details"],
            "shape": None, "what": {}, "why": {
                "narrative": a["why"], "roi": a["roi"],
                "payoff": a["payoff"], "effort": a["effort"],
                "warm": None, "crit": None, "churn": None,
                "cold_delta_s": None,
            },
            "verify": _verify_block({}),
        }
        simulated = False
        if kind == "move_file":
            step["shape"] = {"mode": "move_file", "rule": "reference affinity",
                             "destination": a["details"].get("dst")}
            step["what"] = {"files": 1, "types": 0, "resources_count": 0,
                            "resources": []}
            step["verify"] = _verify_block({
                "edges": "the source-folder → destination edge thins or dies",
            })
        elif kind in ("absorb", "new_module", "cut_then_extract"):
            f = subject
            it = qw_by_folder.get(f, {})
            shape = _shape_for_extraction(f, it, pair_types, type_kinds,
                                          leaf_edges, prefixes, churned)
            res = _resources_under(resources, f)
            n_types = _types_under(decls, f)
            step["shape"] = shape
            step["what"] = {"files": it.get("files", 0), "types": n_types,
                            "resources_count": len(res),
                            "resources": res[:_RESOURCE_SAMPLE]}
            step["why"].update({"warm": it.get("warm"), "crit": it.get("crit"),
                                "churn": it.get("churn")})
            if shape["mode"] == "absorb":
                dest_mod = (it.get("destination") or {}).get("module")
                impl_id = dest_mod or f"new:{_module_name(f)}"
                sim.extract(f, None, impl_id)
            else:
                name = shape["impl_module"] or _module_name(f)
                impl_id = f"new:{name}"
                sim.extract(f, f"new:{name}API" if shape["api_module"]
                            else None, impl_id)
            post = sim.snapshot()
            simulated = True
            expect = {
                "modules": f"{prev['modules']} → {post['modules']}",
                "crit_len": f"{prev['crit_len']} → {post['crit_len']} "
                            f"(simulated, cumulative)",
                "warm_cost": f"{prev['warm_cost']} → {post['warm_cost']} "
                             f"type-unit(s) of churn-weighted rebuild",
                "app_types": f"−{n_types}" if n_types else "unchanged",
            }
            if shape["mode"] == "api_impl":
                deps = post["dependents"].get(impl_id, 0)
                expect["impl_warm"] = (
                    f"editing {shape['impl_module']} rebuilds {deps} "
                    f"module(s) — consumers sit on {shape['api_module']}")
            if measured and app_node and app_node.get("types"):
                share = n_types / app_node["types"]
                est = round(app_node.get("build_ms", 0) * share / 1000.0, 1)
                if est:
                    step["why"]["cold_delta_s"] = est
                    expect["app_work"] = (f"~{est}s of compile work leaves the "
                                          f"app target (estimated)")
            step["verify"] = _verify_block(expect)
        elif kind == "api_retrofit":
            n = nodes_by_id.get(subject, {})
            label = n.get("label", subject)
            mod_folders = sorted(f for f in (decls or {})
                                 if module_of(f, prefixes) == subject)
            surface = _api_surface(mod_folders, pair_types)
            kinds_map = type_kinds or {}
            protocols = [t for t in surface
                         if kinds_map.get(t, "") in _PROTOCOL_KINDS]
            step["shape"] = {
                "mode": "api_retrofit",
                "rule": a["why"],
                "destination": None,
                "api_module": f"{label}API",
                "impl_module": label,
                "consumers": a["details"].get("consumers"),
                "api_surface_count": len(surface),
                "api_surface": surface[:API_SURFACE_SAMPLE],
                "protocols_for": protocols[:API_SURFACE_SAMPLE],
            }
            step["what"] = {"files": 0, "types": n.get("types", 0),
                            "resources_count": 0, "resources": []}
            step["why"].update({"warm": n.get("warm"), "crit": n.get("crit"),
                                "churn": n.get("churn")})
            sim.retrofit(subject, f"new:{label}API")
            post = sim.snapshot()
            simulated = True
            pre_deps = prev["dependents"].get(subject, 0)
            post_deps = post["dependents"].get(subject, 0)
            step["verify"] = _verify_block({
                "modules": f"{prev['modules']} → {post['modules']} "
                           f"(+{label}API)",
                "impl_warm": f"editing {label} rebuilds {pre_deps} → "
                             f"{post_deps} module(s) (simulated, cumulative)",
                "warm_cost": f"{prev['warm_cost']} → {post['warm_cost']} "
                             f"type-unit(s) of churn-weighted rebuild",
            })
        elif kind == "isolate_type":
            iso = (isolations or {}).get(subject, {})
            top = (iso.get("candidates") or [{}])[0]
            step["shape"] = {
                "mode": "isolate",
                "rule": f"{iso.get('summary', {}).get('top_share', 0)}% of the "
                        f"module's external fan-in lands on one type",
                "destination": None,
                "impl_module": _module_name(top.get("type", subject)),
                "api_module": None,
                "consumers": top.get("ext_modules"),
                "api_surface_count": top.get("module_public", 0),
                "api_surface": [], "protocols_for": [],
            }
            step["what"] = {"files": 0, "types": top.get("module_size", 0),
                            "resources_count": 0, "resources": []}
            step["verify"] = _verify_block({
                "modules": "+1 (the isolated type and its closure; "
                           "not simulated — type-level move)",
                "consumers": f"{top.get('ext_modules', 0)} module(s) drop the "
                             f"dependency on {subject}",
            })
        elif kind == "split_module":
            n = nodes_by_id.get(subject, {})
            step["shape"] = {"mode": "split", "rule": a["why"],
                             "destination": None}
            step["what"] = {"files": 0, "types": n.get("types", 0),
                            "resources_count": 0, "resources": []}
            step["verify"] = _verify_block({
                "modules": "+N (one per split unit; not simulated — "
                           "unit-level move)",
                "warm": f"{n.get('warm', 0)} dependent module(s) stop "
                        f"rebuilding on every edit",
            })
        elif kind == "join_module":
            n = nodes_by_id.get(subject, {})
            into = a["details"].get("into")
            step["shape"] = {"mode": "join", "rule": "boundary without benefit",
                             "destination": nodes_by_id.get(into, {}).get(
                                 "label", into)}
            step["what"] = {"files": 0, "types": n.get("types", 0),
                            "resources_count": 0, "resources": []}
            sim.join(subject, into)
            post = sim.snapshot()
            simulated = True
            step["verify"] = _verify_block({
                "modules": f"{prev['modules']} → {post['modules']} "
                           f"(one manifest and resolution step removed)",
            })
        else:  # pragma: no cover — future kinds fall through untyped
            step["shape"] = {"mode": kind, "rule": "", "destination": None}
        if simulated:
            prev = post
        trajectory.append({"id": step["id"], "simulated": simulated,
                           **_public_snap(prev)})
        steps.append(step)

    final = prev
    setup = _setup_items(module_graph, history, excluded_count)
    equilibrium = _equilibrium(module_graph, advice["actions"])
    # Where the plan lands if every simulated step ships — the projected end
    # state, same fields as the trajectory rows (type-unit costs).
    equilibrium["projected"] = {
        **_public_snap(final),
        "app_share_met": final["app_share_pct"] <= EQ_APP_SHARE_PCT,
        "warm_met": final["warm_max_pct"] <= EQ_WARM_MAX_PCT,
        "cycles_met": final["n_cycles"] == 0,
    }

    shapes: dict[str, int] = defaultdict(int)
    for s in steps:
        shapes[s["shape"]["mode"]] += 1
    summary = dict(advice["summary"])
    summary["shapes"] = dict(sorted(shapes.items()))
    summary["setup_items"] = len(setup)
    summary["equilibrium_met"] = equilibrium["met"]
    summary["warm_cost_delta"] = round(final["warm_cost"]
                                       - baseline["warm_cost"], 1)
    summary["cold_cost_delta"] = round(final["cold_cost"]
                                       - baseline["cold_cost"], 1)
    return {"setup": setup, "steps": steps, "deferred": advice["deferred"],
            "trajectory": {"baseline": _public_snap(baseline),
                           "steps": trajectory,
                           "final": _public_snap(final),
                           "unit": "types"},
            "equilibrium": equilibrium, "summary": summary}
