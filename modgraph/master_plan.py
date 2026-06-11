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
    would later fold), or the quick-win ``absorb`` pick. The rule that fired
    ships with the step so the decision is auditable.
  - a **why** grounded in build cost — folder warm blast radius, critical
    path membership, churn, and (when the step moves code out of a module) a
    **simulated** post-step module graph: crit-path length and module-count
    deltas computed by replaying the move over the module edges and re-running
    :func:`modgraph.build_impact.compute_build_impact`.
  - a **verify** block — the commands to run after the step and the metric
    movements to expect, so every step closes its own loop.

* **Equilibrium** — the measurable definition of done: app target thin
  (``EQ_APP_SHARE_PCT``), warm blast radius bounded (``EQ_WARM_MAX_PCT``),
  zero module cycles, API coverage for multi-consumer modules, and an empty
  action feed (neither splitting nor joining pays). Each criterion ships
  current/target/met so the UI can answer "are we done?" from data alone.

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
                     API_SURFACE_SAMPLE, EQ_APP_SHARE_PCT, EQ_WARM_MAX_PCT)
from .graph import build_tree
from .module_graph import APP_ID, module_of

PHASE_LABELS = dict(WAVE_LABELS)

# Reference types with behavior become protocols in the API package; value
# types (and protocols themselves) move into the API package whole.
_PROTOCOL_KINDS = {"class", "actor"}

_RESOURCE_SAMPLE = 6


def _under(folder: str, prefix: str) -> bool:
    return folder == prefix or folder.startswith(prefix + "/")


def _module_name(folder: str) -> str:
    """A Swift-module-ish name from a folder id: last segment, alnum only."""
    seg = folder.rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9]", "", seg)
    return name or "Module"


def _api_surface(folder: str, pair_types: dict | None) -> list[str]:
    """Types declared under *folder* that other folders reference — the
    surface that goes ``public``, i.e. the contents of the API package."""
    if not pair_types:
        return []
    out: set[str] = set()
    for (a, b), types in pair_types.items():
        if _under(b, folder) and not _under(a, folder):
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


def _sim_extract(leaf_edges: dict, migrated_prefixes: list[str],
                 folder: str | None, api_id: str | None,
                 impl_id: str | None) -> dict:
    """Module-graph metrics after (hypothetically) extracting *folder*.

    Replays every folder edge through :func:`module_of`, except that folders
    under *folder* become *impl_id* and references INTO the subtree land on
    *api_id* when one is given (the API-rewire: consumers depend on the API
    package, the implementation keeps the subtree's outgoing deps). With
    ``folder=None`` it is the as-is baseline. Returns ``{"modules", "crit_len"}``.
    """
    medges: dict[tuple[str, str], int] = defaultdict(int)
    mods: set[str] = set()
    prefixes = migrated_prefixes or []
    for (a, b), w in sorted((leaf_edges or {}).items()):
        a_in = folder is not None and _under(a, folder)
        b_in = folder is not None and _under(b, folder)
        if a_in and b_in:
            continue
        ma = impl_id if a_in else module_of(a, prefixes)
        mb = (api_id or impl_id) if b_in else module_of(b, prefixes)
        mods.add(ma)
        mods.add(mb)
        if ma != mb:
            medges[(ma, mb)] += w
    if folder is not None and impl_id:
        mods.add(impl_id)
        if api_id:
            mods.add(api_id)
            medges[(impl_id, api_id)] += 1
    tree = build_tree(mods, {m: set() for m in mods}, root_label="sim")
    bi = compute_build_impact(tree, dict(medges))
    return {"modules": len(mods), "crit_len": bi["summary"]["crit_len"]}


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
    warm_max = max((n.get("warm", 0) for n in nodes), default=0)
    warm_pct = (round(100.0 * warm_max / (n_modules - 1), 1)
                if n_modules > 1 else 0.0)
    n_cycles = summary.get("n_cycles", 0)

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
         "label": "Warm blast radius bounded",
         "current": f"worst module rebuilds {warm_pct}% of modules "
                    f"({warm_max} of {max(n_modules - 1, 0)})",
         "target": f"≤ {EQ_WARM_MAX_PCT}%",
         "met": bool(spm) and warm_pct <= EQ_WARM_MAX_PCT},
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
                        excluded_count: int = 0) -> dict:
    """Return ``{"setup", "steps", "deferred", "equilibrium", "summary"}``.

    Wraps :func:`modgraph.advisor.compute_advice` (which orders the work and
    draws the stop line) and decorates every action into a full step: shape
    decision, build-grounded why, simulated deltas, verify block. All keyword
    inputs are optional and degrade exactly like their producers do.
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
    baseline = _sim_extract(leaf_edges, prefixes, None, None, None)

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
            # Simulated post-step module graph: where do the cold metrics land.
            if shape["mode"] == "absorb":
                dest_mod = (it.get("destination") or {}).get("module")
                sim = _sim_extract(leaf_edges, prefixes, f, None, dest_mod)
            else:
                name = shape["impl_module"] or _module_name(f)
                sim = _sim_extract(leaf_edges, prefixes, f,
                                   f"new:{name}API" if shape["api_module"]
                                   else None, f"new:{name}")
            expect = {
                "modules": f"{baseline['modules']} → {sim['modules']}",
                "crit_len": f"{baseline['crit_len']} → {sim['crit_len']} "
                            f"(simulated)",
                "app_types": f"−{n_types}" if n_types else "unchanged",
            }
            if measured and app_node and app_node.get("types"):
                share = n_types / app_node["types"]
                est = round(app_node.get("build_ms", 0) * share / 1000.0, 1)
                if est:
                    step["why"]["cold_delta_s"] = est
                    expect["app_work"] = (f"~{est}s of compile work leaves the "
                                          f"app target (estimated)")
            step["verify"] = _verify_block(expect)
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
                "modules": "+1 (the isolated type and its closure)",
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
                "modules": "+N (one per split unit)",
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
            step["verify"] = _verify_block({
                "modules": "−1 (one manifest and resolution step removed)",
            })
        else:  # pragma: no cover — future kinds fall through untyped
            step["shape"] = {"mode": kind, "rule": "", "destination": None}
        steps.append(step)

    setup = _setup_items(module_graph, history, excluded_count)
    equilibrium = _equilibrium(module_graph, advice["actions"])

    shapes: dict[str, int] = defaultdict(int)
    for s in steps:
        shapes[s["shape"]["mode"]] += 1
    summary = dict(advice["summary"])
    summary["shapes"] = dict(sorted(shapes.items()))
    summary["setup_items"] = len(setup)
    summary["equilibrium_met"] = equilibrium["met"]
    return {"setup": setup, "steps": steps, "deferred": advice["deferred"],
            "equilibrium": equilibrium, "summary": summary}
