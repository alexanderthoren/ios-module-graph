"""Plan-policy evaluation: prove the master plan beats the alternatives.

"Is this plan any good?" needs a comparison, not an assertion. This module
replays three *policies* over the same graph through the same cumulative
simulator (:class:`modgraph.master_plan._PlanSim`) and prices each with the
same cost model, so the differences are attributable to the planning
decisions alone:

* **master** — the real master plan: advisor ordering, shape decisions
  (API/impl pairs, absorbs), API retrofits, joins, stop line.
* **no_api** — ablation: the same actions in the same order, but every
  extraction ships as a bare single module and retrofits are skipped.
  Isolates the contribution of the API-firewall decision.
* **topo** — the classic baseline: the SCC-aware topological migration plan
  (every folder extracted bottom-up as its own module, no shapes, no stop
  line) — what the tool produced before the advisor existed.

Reported per policy: the projected end state (warm cost, cold chain, app
share, modules), the state after ``EVAL_EARLY_STEPS`` steps (time-to-value —
a plan that pays off early beats one that pays off eventually), and a
uniform effort proxy (types moved + public surface + 1 per file move) so
"better end state" can't hide "for triple the work".

All numbers are type-unit simulations of the structural model — direction
and ranking are meaningful, absolute seconds are not; the report says so.
Pure interpretation; deterministic. CLI:

    python3 -m modgraph.evaluate <project_root> <index_graph.json> [out.md]
"""
from __future__ import annotations

import sys
from pathlib import Path

from .graph import compute_migration_plan
from .master_plan import _PlanSim, _module_name

EVAL_EARLY_STEPS = 10


def _effort_of_step(step: dict) -> float:
    kind = step["kind"]
    if kind == "move_file":
        return 1.0
    sh = step.get("shape") or {}
    what = step.get("what") or {}
    return float((what.get("types") or 0) + (sh.get("api_surface_count") or 0))


def _replay_master(master_plan: dict) -> dict:
    """The master plan already simulated itself — read its trajectory."""
    traj = master_plan.get("trajectory") or {}
    rows = traj.get("steps") or []
    early = rows[min(EVAL_EARLY_STEPS, len(rows)) - 1] if rows else None
    return {
        "policy": "master",
        "steps": len(master_plan.get("steps", [])),
        "effort": round(sum(_effort_of_step(s)
                            for s in master_plan.get("steps", [])), 1),
        "baseline": traj.get("baseline"),
        "early": ({k: v for k, v in early.items()
                   if k not in ("id", "simulated")} if early else None),
        "final": traj.get("final"),
    }


def _replay_actions(master_plan: dict, leaf_edges, prefixes, decls,
                    churn_commits, *, api: bool) -> dict:
    """Replay the master plan's actions through a fresh sim.

    With ``api=False`` every extraction is a bare module and retrofits are
    skipped — the ablation that prices the API decision.
    """
    sim = _PlanSim(leaf_edges, prefixes, decls, churn_commits)
    baseline = sim.snapshot()
    rows = []
    effort = 0.0
    n_steps = 0
    for s in master_plan.get("steps", []):
        kind, subject = s["kind"], s["subject"]
        sh = s.get("shape") or {}
        if kind in ("absorb", "new_module", "cut_then_extract"):
            if sh.get("mode") == "absorb":
                dest = sh.get("destination")
                it_dest = (s.get("details") or {}).get("destination")
                impl = it_dest or dest or f"new:{_module_name(subject)}"
                sim.extract(subject, None, impl)
            else:
                name = sh.get("impl_module") or _module_name(subject)
                use_api = api and sh.get("mode") == "api_impl"
                sim.extract(subject, f"new:{name}API" if use_api else None,
                            f"new:{name}")
        elif kind == "api_retrofit":
            if not api:
                continue  # ablated away — costs nothing, does nothing
            label = sh.get("impl_module") or subject
            sim.retrofit(subject, f"new:{label}API")
        elif kind == "join_module":
            sim.join(subject, (s.get("details") or {}).get("into"))
        elif kind != "move_file":
            continue  # isolate/split/partition: type-level, not simulated
        n_steps += 1
        eff = _effort_of_step(s)
        if not api:  # no API package -> no surface to publish there
            eff = float((s.get("what") or {}).get("types") or 0) \
                if kind != "move_file" else 1.0
        effort += eff
        if n_steps <= EVAL_EARLY_STEPS:
            rows.append(sim.snapshot())
    final = sim.snapshot()
    early = rows[-1] if rows else baseline
    pub = lambda d: {k: v for k, v in d.items() if k != "dependents"}  # noqa: E731
    return {
        "policy": "no_api" if not api else "master_replayed",
        "steps": n_steps,
        "effort": round(effort, 1),
        "baseline": pub(baseline),
        "early": pub(early),
        "final": pub(final),
    }


def _replay_topo(leaf_edges, plan_edges, source_folders, prefixes, decls,
                 churn_commits) -> dict:
    """The pre-advisor baseline: extract every folder bottom-up, no shapes."""
    plan, _stuck = compute_migration_plan(plan_edges, set(source_folders))
    sim = _PlanSim(leaf_edges, prefixes, decls, churn_commits)
    baseline = sim.snapshot()
    rows = []
    effort = 0.0
    n_steps = 0
    types_of = {f: len(ts) for f, ts in (decls or {}).items()}
    for step in plan:
        for folder in step["folders"]:
            sim.extract(folder, None, f"new:{_module_name(folder)}")
            effort += float(types_of.get(folder, 0))
        n_steps += 1
        if n_steps <= EVAL_EARLY_STEPS:
            rows.append(sim.snapshot())
    final = sim.snapshot()
    early = rows[-1] if rows else baseline
    pub = lambda d: {k: v for k, v in d.items() if k != "dependents"}  # noqa: E731
    return {
        "policy": "topo",
        "steps": n_steps,
        "effort": round(effort, 1),
        "baseline": pub(baseline),
        "early": pub(early),
        "final": pub(final),
    }


def compute_evaluation(master_plan: dict, leaf_edges, plan_edges,
                       source_folders, prefixes, decls,
                       churn_commits=None) -> dict:
    """Replay the three policies; return ``{"policies": [...], "verdict"}``.

    Pure over the pipeline products the caller already computed. The verdict
    names the policy with the lowest final warm cost, with effort and
    time-to-value reported alongside so the trade is visible.
    """
    policies = [
        _replay_master(master_plan),
        _replay_actions(master_plan, leaf_edges, prefixes, decls,
                        churn_commits, api=False),
        _replay_topo(leaf_edges, plan_edges, source_folders, prefixes, decls,
                     churn_commits),
    ]
    ranked = sorted((p for p in policies if p["final"]),
                    key=lambda p: (p["final"]["warm_cost"], p["effort"]))
    verdict = ranked[0]["policy"] if ranked else "n/a"
    return {"policies": policies, "verdict": verdict,
            "early_steps": EVAL_EARLY_STEPS}


def evaluation_markdown(ev: dict, label: str) -> str:
    lines = [f"# Plan-policy evaluation — {label}", ""]
    lines.append(
        "Three policies replayed over the same graph through the same "
        "cumulative simulator and cost model. Type-unit simulations of the "
        "structural model: rankings and directions are meaningful, absolute "
        "numbers are not seconds.")
    lines.append("")
    lines.append("| policy | steps | effort (types+surface) | "
                 f"warm cost @{ev['early_steps']} steps | final warm cost | "
                 "final cold chain | final app share | final modules |")
    lines.append("|---|---|---|---|---|---|---|")
    base = next((p["baseline"] for p in ev["policies"] if p["baseline"]), {})
    lines.append(f"| *(baseline)* | — | — | {base.get('warm_cost')} | "
                 f"{base.get('warm_cost')} | {base.get('cold_cost')} | "
                 f"{base.get('app_share_pct')}% | {base.get('modules')} |")
    for p in ev["policies"]:
        f, e = p.get("final") or {}, p.get("early") or {}
        lines.append(f"| **{p['policy']}** | {p['steps']} | {p['effort']} | "
                     f"{e.get('warm_cost')} | {f.get('warm_cost')} | "
                     f"{f.get('cold_cost')} | {f.get('app_share_pct')}% | "
                     f"{f.get('modules')} |")
    lines.append("")
    lines.append(f"**Verdict:** `{ev['verdict']}` reaches the lowest "
                 f"churn-weighted rebuild cost (ties broken by effort).")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print("usage: python3 -m modgraph.evaluate <project_root> "
              "<index_graph.json> [out.md]", file=sys.stderr)
        return 2
    root = Path(argv[0]).expanduser().resolve()
    graph_json = Path(argv[1]).expanduser().resolve()
    out_md = Path(argv[2]).expanduser() if len(argv) > 2 else None

    # Assemble the pipeline products the policies need (the same calls
    # cli.main makes, minus rendering).
    from .churn import compute_churn
    from .index_loader import load_index_graph
    from .isolate import compute_isolations, isolatable_modules
    from .file_affinity import compute_file_moves
    from .master_plan import compute_master_plan
    from .module_graph import compute_module_graph
    from .module_splits import compute_module_splits
    from .partition import compute_partitions
    from .build_recommendations import compute_split_recommendations
    from .divide import compute_division_plan, dividable_modules
    from .graph import build_tree
    from .quick_wins import compute_quick_wins
    from .scoring import compute_folder_scores
    from .spm import auto_detect_migrated_prefixes, is_migrated

    data = load_index_graph(graph_json)
    prefixes = auto_detect_migrated_prefixes(root)
    all_source = {f["folder"] for f in data.file_records}
    migrated = {f for f in all_source if is_migrated(f, prefixes)}
    source_folders = all_source - migrated
    plan_edges = {(a, b): w for (a, b), w in data.leaf_edges.items()
                  if a in source_folders and b in source_folders}
    churn_commits = compute_churn(root)
    tree = build_tree(data.all_folders, data.decls, root_label=root.name)
    scores = compute_folder_scores(tree, plan_edges, source_folders,
                                   data.pair_types, data.decls,
                                   data.file_records,
                                   churn_commits=churn_commits)
    module_graph = compute_module_graph(all_source, data.leaf_edges, prefixes,
                                        data.decls, root_label=root.name,
                                        churn_commits=churn_commits)
    divisions = {p: compute_division_plan(p, data.leaf_edges, data.pair_types,
                                          data.decls)
                 for p in dividable_modules(data.decls)}
    quick_wins = compute_quick_wins(scores, plan_edges, data.pair_types,
                                    data.leaf_edges, source_folders, prefixes,
                                    data.file_edges, module_graph)
    partitions = compute_partitions(isolatable_modules(data.decls, prefixes),
                                    data.type_edges, data.type_kinds,
                                    prefixes, module_graph)
    master_plan = compute_master_plan(
        quick_wins, compute_file_moves(data.file_edges, source_folders),
        compute_isolations(data.decls, data.type_edges, data.type_kinds,
                           prefixes),
        compute_module_splits(module_graph, data.leaf_edges,
                              data.pair_types or {}, data.decls, prefixes),
        compute_split_recommendations(module_graph, divisions),
        module_graph, pair_types=data.pair_types, type_kinds=data.type_kinds,
        leaf_edges=data.leaf_edges, migrated_prefixes=prefixes,
        decls=data.decls, churn_commits=churn_commits, partitions=partitions)

    ev = compute_evaluation(master_plan, data.leaf_edges, plan_edges,
                            source_folders, prefixes, data.decls,
                            churn_commits)
    md = evaluation_markdown(ev, root.name)
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md, encoding="utf-8")
        print(f"Wrote {out_md}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
