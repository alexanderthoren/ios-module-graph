"""Command-line interface: argument parsing and the main orchestration flow."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import DEFAULT_EXCLUDED, DEFAULT_HISTORY, DEFAULT_OUT, REPO_ROOT
from .cycles import compute_cycle_breakers, compute_extraction_targets
from .history import append_snapshot, load_history
from .divide import compute_division_plan
from .exclusions import compute_blocked_by_excluded, load_exclusions
from .graph import build_tree, compute_migration_plan
from .build_recommendations import compute_split_recommendations
from .build_times import load_build_floors, load_build_times
from .churn import CHURN_DAYS, compute_churn
from .index_loader import load_index_graph
from .module_graph import compute_module_graph
from .module_splits import compute_module_splits
from .file_affinity import compute_file_moves
from .master_plan import compute_master_plan
from .isolate import compute_isolations, isolatable_modules
from .partition import compute_partitions
from .quick_wins import compute_quick_wins
from .render import render_html
from .resources import collect_resources
from .scanner import compute_pair_types, scan
from .scoring import compute_folder_scores
from .spm import _build_package_map, auto_detect_migrated_prefixes, is_migrated
from .staleness import warn_if_stale
from .tasks import write_master_plan_json, write_master_plan_markdown


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Hierarchical Swift folder dependency explorer + SPM migration planner."
    )
    p.add_argument("project_root", type=Path,
                   help="Path to the project root to scan (required).")
    p.add_argument("--graph", nargs="?", type=Path, const=DEFAULT_OUT, default=None,
                   metavar="PATH",
                   help=f"Generate the interactive HTML graph (default path: {DEFAULT_OUT}). "
                        f"If neither --graph nor --list is given, --graph is implied.")
    p.add_argument("--list", nargs="?", type=Path, const=REPO_ROOT / "migration_plan.md",
                   default=None, metavar="PATH",
                   help="Generate a machine-readable migration task list (default path: "
                        "./migration_plan.md). Suitable for handing to an AI assistant or for "
                        "tracking PR-sized refactor steps.")
    p.add_argument("--list-format", choices=["markdown", "json"], default="markdown",
                   help="List output format (default: markdown).")
    p.add_argument("--out", type=Path, default=None,
                   help="Deprecated: alias for --graph PATH. Use --graph PATH instead.")
    p.add_argument("--include-tests", action="store_true",
                   help="Include Tests/UITests/SnapshotTests folders (skipped by default).")
    p.add_argument("--ignore", action="append", default=[],
                   metavar="PATTERN",
                   help="Glob pattern to skip (matched against dir name OR relative path). "
                        "Repeatable. Defaults already skip common build/IDE/dependency dirs.")
    p.add_argument("--migrated-prefix", action="append", default=[],
                   metavar="PREFIX",
                   help="Relative path prefix whose folders are ALREADY in SPM (treated as "
                        "external destinations). Repeatable. Edges into these folders do not "
                        "block migration. Auto-detected for any top-level dir containing "
                        "Package.swift (its Sources/ subtree is added).")
    p.add_argument("--no-auto-detect-spm", action="store_true",
                   help="Disable auto-detection of Package.swift -> migrated prefix.")
    p.add_argument("--excluded-file", type=Path, default=DEFAULT_EXCLUDED,
                   metavar="JSON",
                   help="Folders marked 'won't be modularized'. JSON array of "
                        "folder paths, read every run (default: "
                        f"{DEFAULT_EXCLUDED.name}). These are dropped from the "
                        "migration plan/list and shown tagged (not as tasks) in "
                        "the graph. The graph's Exclude button writes this exact "
                        "file via one-click download.")
    p.add_argument("--ext", default=".swift",
                   help="File extension to scan (default .swift). "
                        "Only Swift is fully supported by the current regex.")
    p.add_argument("--label", default=None,
                   help="Display label for the project root (default: directory basename).")
    p.add_argument("--build-times", type=Path, default=None, metavar="JSON",
                   help="Optional xcsift --build-info JSON capturing real per-target "
                        "compile times from the cold build. When present, Build mode "
                        "uses measured seconds as module cost instead of the type-count "
                        "proxy. Missing/malformed file is ignored (falls back to proxy).")
    p.add_argument("--history", type=Path, default=DEFAULT_HISTORY, metavar="JSONL",
                   help="Append-only build-cost history (one row per real change, "
                        "keyed to the target project's git commit; deduped). "
                        f"Default: {DEFAULT_HISTORY.name}. Deliberately NOT wiped by "
                        "`just clean` — it tracks improvement across extractions and "
                        "powers Build mode's Improvements tab.")
    p.add_argument("--from-index", type=Path, default=None, metavar="JSON",
                   help="Load a resolved dependency graph produced by the index_graph "
                        "Swift tool (reads the compiler index store) instead of "
                        "regex-scanning sources. Edges are resolved by USR, so name "
                        "collisions never fabricate edges. project_root MUST be the "
                        "same root passed to index_graph (folder ids are relative to it).")
    return p.parse_args()


def _warn_scan_fallback() -> None:
    """Print a prominent banner when running the regex-scanner fallback.

    Without ``--from-index`` the graph is built by text-scanning source files,
    which cannot tell which folder a reference binds to when several declare a
    same-named type — it fabricates an edge to *every* declarer (phantom edges).
    The whole point of the index path is to avoid this, so make the degraded
    mode impossible to miss in the output.
    """
    banner = (
        "\n"
        "  ┌────────────────────────────────────────────────────────────────┐\n"
        "  │  ⚠  REGEX-SCANNER FALLBACK — results may contain phantom edges.  │\n"
        "  │                                                                  │\n"
        "  │  No --from-index given, so the graph is built by text-scanning.  │\n"
        "  │  When two folders declare a same-named type, every reference to  │\n"
        "  │  that name gets an edge to BOTH — edges you can't trust for a     │\n"
        "  │  migration plan. Build the index graph and pass --from-index     │\n"
        "  │  for USR-resolved, accurate edges (see README / `just tree`).    │\n"
        "  └────────────────────────────────────────────────────────────────┘\n"
    )
    print(banner, file=sys.stderr)


def main() -> int:
    args = parse_args()
    root = args.project_root.expanduser().resolve()
    if not root.is_dir():
        print(f"Project root not found: {root}", file=sys.stderr)
        return 1
    if not args.ext.startswith("."):
        args.ext = "." + args.ext
    root_label = args.label or root.name or str(root)

    if args.from_index is not None:
        data = load_index_graph(args.from_index.expanduser().resolve())
        # The JSON is cached across runs; if the target repo moved since it was
        # indexed (or either side is dirty), say so loudly instead of silently
        # rendering an old world.
        warn_if_stale(data.target_commit, root)
    else:
        _warn_scan_fallback()
        data = scan(
            root, include_tests=args.include_tests, ignore_patterns=args.ignore, ext=args.ext
        )
    # Unpack the GraphData into the locals the rest of main() works with.
    decls = data.decls
    leaf_edges = data.leaf_edges
    multi_decl_types = data.multi_decl_types
    all_folders = data.all_folders
    file_records = data.file_records
    type_owners = data.type_owners
    raw_owners = data.raw_owners
    resolved_pair_types = data.pair_types  # None on the scan path
    type_kinds = data.type_kinds           # {} on the scan path
    file_edges = data.file_edges           # [] on the scan path
    type_edges = data.type_edges           # [] on the scan path
    if not file_records:
        print(f"No {args.ext} files found under {root}. "
              f"(Try --include-tests or check --ignore patterns.)", file=sys.stderr)
        return 1

    migrated_prefixes = list(args.migrated_prefix)
    if not args.no_auto_detect_spm:
        auto = auto_detect_migrated_prefixes(root)
        for a in auto:
            if a not in migrated_prefixes:
                migrated_prefixes.append(a)

    all_source_folders = {f["folder"] for f in file_records}
    initial_migrated = {f for f in all_source_folders if is_migrated(f, migrated_prefixes)}

    # "Won't be modularized" — persisted exclusions. An entry may name an
    # ancestor folder; expand it to every source folder in its subtree. Excluded
    # folders are dropped from the plan exactly like migrated ones, but reported
    # and rendered as a distinct category.
    excluded_file = args.excluded_file.expanduser()
    raw_excluded = load_exclusions(excluded_file)
    excluded = {
        f for f in all_source_folders
        if any(f == e or f.startswith(e + "/") for e in raw_excluded)
    }
    # Transitive can't-modularize: any folder reaching an excluded folder via
    # leaf_edges (without crossing an already-migrated node) is itself stuck —
    # an SPM package can't link against the non-SPM app target where excluded
    # code stays. These come out of the migration plan and are reported as a
    # separate bucket.
    blocked_reasons = compute_blocked_by_excluded(
        leaf_edges, excluded, initial_migrated
    )
    blocked = set(blocked_reasons.keys())
    source_folders = all_source_folders - initial_migrated - excluded - blocked

    tree = build_tree(all_folders, decls, root_label=root_label)
    # Plan computed over source folders only. Edges into pre-migrated folders
    # are dropped (treated as external SPM deps).
    plan_edges = {
        (a, b): w for (a, b), w in leaf_edges.items()
        if a in source_folders and b in source_folders
    }
    # In index mode pair_types is already resolved by USR; recomputing by name
    # would reintroduce the collision false-edges we built this tool to avoid.
    pair_types = (resolved_pair_types if resolved_pair_types is not None
                  else compute_pair_types(file_records, raw_owners))

    # Git churn over the target repo (commits touching Swift in the last year).
    # Weights both the per-folder scores below and Build mode's Split-plan
    # ranking. Best-effort like every other git capture — no git, no churn,
    # never fatal.
    churn_commits = compute_churn(root)

    # Per-folder payoff/effort scores rank the plan's eligible frontier by ROI:
    # cheap, high-payoff steps lead while the topological order stays exact.
    folder_scores = compute_folder_scores(
        tree, plan_edges, source_folders, pair_types, decls, file_records,
        churn_commits=churn_commits,
    )
    plan, stuck = compute_migration_plan(plan_edges, source_folders,
                                         scores=folder_scores["folders"])

    # For each cycle bundle in the plan, compute ordered cycle-breaker edges.
    # Use plan_edges (not leaf_edges) so already-migrated targets are ignored.
    for step in plan:
        if step["is_cycle"]:
            br = compute_cycle_breakers(step["folders"], plan_edges, pair_types)
            step["breakers"] = br["edges"]
            step["fas_estimate"] = br["total_fas_estimate"]
            step["extractions"] = compute_extraction_targets(
                step["folders"], plan_edges, pair_types
            )
        else:
            step["breakers"] = []
            step["fas_estimate"] = 0
            step["extractions"] = []

    print(f"\nProject:           {root}  (label: {root_label})")
    print(f"Folders scanned:   {len(all_folders)}")
    print(f"Source folders:    {len(all_source_folders)}")
    if migrated_prefixes:
        print(f"Pre-migrated:      {len(initial_migrated)} folder(s) under {len(migrated_prefixes)} prefix(es):")
        for p in migrated_prefixes:
            n = sum(1 for f in initial_migrated if is_migrated(f, [p]))
            print(f"  - {p}/  ({n} folder(s))")
        print(f"To migrate:        {len(source_folders)} folder(s)")
    if excluded:
        print(f"Won't modularize:  {len(excluded)} folder(s) (from {excluded_file})")
    if blocked:
        print(f"Can't modularize:  {len(blocked)} folder(s) (transitively depend on excluded)")
    print(f"Leaf-edges:        {len(leaf_edges)} total, {len(plan_edges)} considered for plan")
    print(f"Types in 2+ folders: {len(multi_decl_types)} (all declarers kept)")
    n_cycles = sum(1 for p in plan if p["is_cycle"])
    n_singletons = len(plan) - n_cycles
    biggest = max((p["size"] for p in plan), default=0)
    print(f"Migration plan:    {len(plan)} steps "
          f"({n_singletons} single-folder, {n_cycles} cyclic bundles, biggest: {biggest})")
    if stuck:
        print(f"  Stuck: {len(stuck)} SCC(s) — unreachable (shouldn't happen)")
    if plan:
        head = plan[0]
        h_label = head["folders"][0] if head["size"] == 1 else f"cycle of {head['size']} folders"
        roi_note = f"roi {head['roi']}, " if head.get("roi") is not None else ""
        print(f"  → Start with: {h_label} ({roi_note}unlocks {len(head['unlocks'])} bundle(s))")

    # Ship every edge to the HTML — Explore mode now renders SPM-to-SPM
    # coupling as first-class. Migration mode filters per the chosen
    # source/target at render time.
    edges_for_html = dict(leaf_edges)
    folder_package, packages = _build_package_map(
        all_source_folders, migrated_prefixes
    )

    # Per-folder bundle-resource inventory (xibs, asset catalogs, .strings, …)
    # for the migration prompts: a step must move these along with the code,
    # declare them in Package.swift, and switch Bundle.main → Bundle.module.
    # Best-effort — a filesystem hiccup never blocks a render.
    try:
        resources = collect_resources(
            root, include_tests=args.include_tests, ignore_patterns=args.ignore
        )
    except OSError:
        resources = {}

    # Decide which outputs to emit. --out is a back-compat alias for --graph.
    graph_path: Path | None = args.graph
    if args.out is not None:
        graph_path = args.out
    list_path: Path | None = args.list
    # If user passed neither, default to graph at DEFAULT_OUT (preserve old behavior).
    if graph_path is None and list_path is None:
        graph_path = DEFAULT_OUT

    # Precompute a division plan for every dividable folder so the HTML's
    # "Divide" action can render instantly client-side. Only the USR-resolved
    # index path has accurate pair_types (the regex-scan path can't price the
    # public-API cost), so divisions stay empty there.
    divisions: dict[str, dict] = {}
    isolations: dict[str, dict] = {}
    if resolved_pair_types is not None:
        from .divide import dividable_modules
        for prefix in dividable_modules(decls):
            divisions[prefix] = compute_division_plan(
                prefix, leaf_edges, resolved_pair_types, decls
            )
        # Type-isolation: per module, the single hottest type to pull into its
        # own sub-module so outside consumers depend on it, not the whole parent.
        # Needs the USR-resolved type_edges (empty on the regex-scan path).
        isolations = compute_isolations(
            decls, type_edges, type_kinds, migrated_prefixes
        )
        if isolations:
            top_mod = max(
                isolations.values(),
                key=lambda a: a["candidates"][0]["total_refs"],
            )
            tc = top_mod["candidates"][0]
            print(f"Type isolation:    {len(isolations)} module(s) with a "
                  f"pull-out candidate (top: {tc['type']} in "
                  f"{top_mod['module']} — {tc['ext_refs']} external ref(s) from "
                  f"{tc['ext_modules']} module(s), drags {tc['pulls_total']})")

    # Build mode operates at real compile-unit granularity: the folder graph is
    # collapsed to SPM targets + the single xcodeproj app target, and scored for
    # warm-rebuild blast radius + cold-build critical path. Structural (no churn).
    # Real per-target compile times (xcsift --build-info JSON) become module cost
    # when available; otherwise the type-count proxy is used.
    build_times = load_build_times(args.build_times) if args.build_times else {}
    # Serial-floor sidecar (longest single file per module) lives next to the
    # build-times file; powers the from-scratch cold-build wall estimate.
    build_floors = (load_build_floors(Path(args.build_times).with_name("build_floors.json"))
                    if args.build_times else {})
    module_graph = compute_module_graph(
        all_source_folders, leaf_edges, migrated_prefixes, decls,
        root_label=root_label, build_times=build_times, build_floors=build_floors,
        churn_commits=churn_commits,
    )
    msum = module_graph["summary"]
    if build_times:
        print(f"Build times:       measured for {len(build_times)} target(s) "
              f"(~{msum['total_build_s']}s total compile work)")
    if churn_commits:
        print(f"Churn:             {len(churn_commits)} commit(s) touched Swift "
              f"in the last {CHURN_DAYS} days")
    n_app = sum(1 for n in module_graph["nodes"] if n["kind"] == "app")
    print(f"Build graph:       {len(module_graph['nodes'])} module(s) "
          f"({len(module_graph['nodes']) - n_app} SPM + {n_app} app), "
          f"{len(module_graph['edges'])} edge(s); cold critical path "
          f"{msum['crit_len']} deep, max parallel width {msum['max_width']}, "
          f"{msum['n_cycles']} module cycle(s)")

    # Usage-cohort partitions: per real compile unit, split-by-how-it-is-used
    # (consumer drag closures + shared core), priced against the module graph.
    # Index path only (needs type_edges); no_seam verdicts are kept — their
    # blockers are the actionable output.
    partitions: dict[str, dict] = {}
    if resolved_pair_types is not None and type_edges:
        partitions = compute_partitions(
            isolatable_modules(decls, migrated_prefixes), type_edges,
            type_kinds, migrated_prefixes, module_graph,
        )
        n_ok = sum(1 for p in partitions.values() if p["verdict"] == "ok")
        if partitions:
            print(f"Partitions:        {n_ok} module(s) with a usage seam, "
                  f"{len(partitions) - n_ok} blocked (no seam — hubs listed)")

    # Rank modules by the build-time payoff of separating them (links dividable
    # ones to the precomputed division plans). Computed after divisions so it can
    # flag which modules have an auto-split plan.
    recommendations = compute_split_recommendations(module_graph, divisions)

    # Quick wins: per source folder, the cut-set blocking an extract-today, the
    # auto-picked absorb-into-existing destination (D2), ranked by ROI (D1).
    # Needs the module graph for the absorb cycle check, hence computed here.
    quick_wins = compute_quick_wins(
        folder_scores, plan_edges, pair_types, leaf_edges, source_folders,
        migrated_prefixes, file_edges, module_graph,
    )
    qsum = quick_wins["summary"]
    print(f"Quick wins:        {qsum['extractable_now']} folder(s) extractable now "
          f"({qsum['absorbable']} absorbable into existing modules, "
          f"{qsum['cut_first']} need cuts first)")
    if quick_wins["items"]:
        top = quick_wins["items"][0]
        dest = (f"absorb into {top['destination']['label']}" if top["destination"]
                else top["action"].replace("_", " "))
        print(f"  → Top quick win: {top['folder']} (roi {top['roi']}, {dest})")

    # Composite-module splits: migrated SPM modules whose level spread says a
    # low-level core is trapped inside (consumers could drop their dependency
    # height). Advisory; index path only (public cost needs pair_types).
    module_splits = compute_module_splits(
        module_graph, leaf_edges,
        resolved_pair_types if resolved_pair_types is not None else {},
        decls, migrated_prefixes,
    )
    if module_splits["items"]:
        top_split = module_splits["items"][0]
        print(f"Module splits:     {module_splits['summary']['candidates']} "
              f"candidate(s) (top: {top_split['label']} L{top_split['level']} "
              f"hides L{top_split['min_intrinsic']} core, "
              f"{len(top_split['releasable'])} consumer(s) releasable)")

    # Misplaced files: the smallest PRs of all — moving a file whose references
    # overwhelmingly bind to another folder dissolves fake coupling before any
    # extraction. Advisory; index path only (empty without file_edges).
    file_moves = compute_file_moves(file_edges, source_folders)
    if file_moves["items"]:
        head_move = file_moves["items"][0]
        print(f"Misplaced files:   {file_moves['summary']['suggested_moves']} "
              f"move suggestion(s) (top: {head_move['file']} → "
              f"{head_move['to']}, {head_move['refs']} ref(s))")
    if recommendations["items"]:
        top = recommendations["items"][0]
        score = (f"hot {top['hot']}/100" if top.get("hot") is not None
                 else f"payoff {top['combined']}/100")
        print(f"  → Top split candidate: {top['label']} "
              f"({score}, {top['action'].lower()})")

    # Auto-record a build-cost snapshot keyed to the target project's git commit.
    # Survives `just clean`, deduped against the last row, so successive
    # extractions accumulate one comparable point each. Powers Build mode's
    # "Improvements" tab. Best-effort — never fatal.
    history_path = args.history.expanduser()
    try:
        snap = append_snapshot(history_path, module_graph, plan, root)
        if snap:
            tag = snap["sha_short"] or "no-git"
            if snap["dirty"]:
                tag += "*"
            print(f"History:           snapshot @ {tag} → {history_path.name} "
                  f"({snap['structural']['modules']} modules, "
                  f"{snap['structural']['cycles']} cycle(s))")
    except OSError:
        pass
    history = load_history(history_path)

    # The master plan IS Migration mode: a detection-driven Setup checklist
    # plus ONE ordered plan that wraps the advisor's arbiter and decorates
    # every action with a shape decision (API/impl pair vs single module vs
    # absorb), a build-grounded why with simulated deltas, a verify block,
    # and the equilibrium criteria that define "done". Computed after the
    # history load so the measurement-baseline setup item can see it.
    master_plan = compute_master_plan(
        quick_wins, file_moves, isolations, module_splits, recommendations,
        module_graph, pair_types=resolved_pair_types, type_kinds=type_kinds,
        leaf_edges=leaf_edges, migrated_prefixes=migrated_prefixes,
        decls=decls, resources=resources, history=history,
        excluded_count=len(excluded), churn_commits=churn_commits,
        partitions=partitions,
    )
    msum_plan = master_plan["summary"]
    print(f"Master plan:       {msum_plan['actions']} step(s) in "
          f"{len(msum_plan['waves'])} phase(s), {msum_plan['deferred']} "
          f"deferred, {msum_plan['setup_items']} setup item(s)"
          + (", equilibrium MET" if msum_plan["equilibrium_met"] else ""))
    if master_plan["steps"]:
        head_step = master_plan["steps"][0]
        print(f"  → First move: {head_step['title']}")
    traj = master_plan["trajectory"]
    print(f"  Projected: warm cost {traj['baseline']['warm_cost']} → "
          f"{traj['final']['warm_cost']} {traj['unit']}-units, cold chain "
          f"{traj['baseline']['cold_cost']} → {traj['final']['cold_cost']}, "
          f"app share {traj['baseline']['app_share_pct']}% → "
          f"{traj['final']['app_share_pct']}%")

    if graph_path is not None:
        graph_path = graph_path.expanduser().resolve()
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        render_html(
            tree, edges_for_html, multi_decl_types, file_records, type_owners,
            plan, stuck, root_label, str(root), sorted(initial_migrated),
            migrated_prefixes, graph_path, type_kinds=type_kinds,
            initial_excluded=sorted(excluded), excluded_file=excluded_file,
            folder_package=folder_package, packages=packages,
            file_edges=file_edges, type_edges=type_edges,
            divisions=divisions, module_graph=module_graph,
            recommendations=recommendations, history=history,
            resources=resources, quick_wins=quick_wins, file_moves=file_moves,
            module_splits=module_splits, isolations=isolations,
            master_plan=master_plan, partitions=partitions,
        )
        print(f"\nWrote graph: {graph_path}")

    if list_path is not None:
        # The list is the file-shaped twin of the HTML's Plan tab — both read
        # the same master plan, so the CLI and the UI can never disagree.
        list_path = list_path.expanduser().resolve()
        list_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "root_label": root_label,
            "root_path": str(root),
            "migrated_prefixes": migrated_prefixes,
            "baseline_count": len(initial_migrated),
            "source_total": len(all_source_folders),
            "excluded_count": len(excluded),
            "blocked_reasons": {k: sorted(v) for k, v in blocked_reasons.items()},
            "steps_total": len(master_plan["steps"]),
        }
        if args.list_format == "json":
            write_master_plan_json(master_plan, meta, list_path)
        else:
            write_master_plan_markdown(master_plan, meta, list_path)
        print(f"Wrote list:  {list_path}  ({len(master_plan['steps'])} step(s), "
              f"format={args.list_format})")

    return 0
