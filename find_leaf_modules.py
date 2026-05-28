#!/usr/bin/env python3
"""Hierarchical Swift module dependency explorer + SPM-migration planner.

Walks every folder under <project_root> that contains .swift files, builds a
type-reference graph at the deepest folder granularity, and emits an
interactive HTML view that:

  - lets you drill into folders / drill into a single folder's types
  - computes a topologically-ordered migration plan (SCC-aware: cyclically
    coupled folders are bundled into a single step)
  - shows "start here" → "next" → "next" guidance with click-to-apply

Run:
    python3 find_leaf_modules.py <project_root> [--out PATH] [options]

Options:
    --include-tests       Include Tests / UITests / SnapshotTests folders.
    --ignore PATTERN      Glob (matched against folder name OR relative path)
                          to skip. Repeatable. Defaults skip common build /
                          dependency / IDE folders.
    --ext .swift          File extension to scan (only .swift is supported by
                          the current regex; flag reserved for future use).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR / "dependency_graph.html"
DEFAULT_EXCLUDED = SCRIPT_DIR / ".modularization_excluded.json"

# Generic skip-list: applies to ANY project. Names matched against directory
# basename. Patterns matched against the relative-path-from-root via fnmatch.
DEFAULT_SKIP_NAMES = {
    "DerivedData", "build", "Build", "dist", "out",
    "Pods", "Carthage", "node_modules", "vendor",
    "__pycache__", "venv", "env",
    # Xcode/SPM cache locations inside checked-out source trees.
    "SourcePackages", "checkouts",
}
# Skipped unless --include-tests is passed.
TEST_DIR_NAMES = {"Tests", "Test", "UITests", "SnapshotTests", "tests"}
# Xcode/Resource bundles — never useful as source.
EXT_SKIPS = {".xcodeproj", ".xcworkspace", ".bundle", ".framework", ".app"}

DECL_RE = re.compile(
    r"\b(?:class|struct|enum|protocol|actor|typealias)\s+([A-Z][A-Za-z0-9_]*)"
)
REF_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\b")
LINE_COMMENT_RE = re.compile(r"//.*?$", re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')


def strip_noise(src: str) -> str:
    src = BLOCK_COMMENT_RE.sub("", src)
    src = LINE_COMMENT_RE.sub("", src)
    src = STRING_RE.sub('""', src)
    return src


def should_skip_dir(
    dir_path: Path, rel_path: str, include_tests: bool, ignore_patterns: list[str]
) -> bool:
    name = dir_path.name
    # Skip every dot-prefixed dir (.git, .build, .swiftpm, .idea, .tmpBuildData …).
    if name.startswith("."):
        return True
    if name in DEFAULT_SKIP_NAMES:
        return True
    if not include_tests and name in TEST_DIR_NAMES:
        return True
    if any(name.endswith(ext) for ext in EXT_SKIPS):
        return True
    for pat in ignore_patterns:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_path, pat):
            return True
    return False


def collect_swift_files(
    root: Path, include_tests: bool, ignore_patterns: list[str], ext: str
) -> list[Path]:
    """Walk root recursively. Respect skip rules. Return matching files."""
    files: list[Path] = []

    def walk(d: Path):
        try:
            entries = list(d.iterdir())
        except (PermissionError, OSError):
            return
        for child in entries:
            if child.is_symlink():
                continue
            if child.is_dir():
                rel = child.relative_to(root).as_posix()
                if should_skip_dir(child, rel, include_tests, ignore_patterns):
                    continue
                walk(child)
            elif child.is_file() and child.suffix == ext:
                files.append(child)

    walk(root)
    return files


def rel_folder(file_path: Path, root: Path) -> str:
    """Return POSIX-style folder path relative to root.

    Empty string means the file sits at the project root. Folder ids must be
    non-empty for the tree, so we map root-level files into the synthetic
    bucket '.' (rendered as the root display name).
    """
    rel = file_path.relative_to(root).parent.as_posix()
    return rel if rel else "."


def compute_pair_types(file_records: list[dict], owners: dict[str, set[str]]) -> dict:
    """Map (src_folder, dst_folder) -> set of type names referenced from src
    whose declarer folder includes dst."""
    pair: dict[tuple[str, str], set[str]] = defaultdict(set)
    for f in file_records:
        src = f["folder"]
        for t in f["refs"]:
            for d in owners.get(t, ()):
                if d != src:
                    pair[(src, d)].add(t)
    return pair


def _feedback_arc_set(
    members: set[str], internal: dict[tuple[str, str], int]
) -> list[tuple[str, str, int]]:
    """Eades-Lin-Smyth greedy FAS approximation, weight-aware.

    Linearize nodes: repeatedly pick a sink (out=0) for the right side, a source
    (in=0) for the left side, otherwise pick the node maximizing
    (weighted_out - weighted_in). Edges going right-to-left in the resulting
    ordering form the feedback arc set.
    """
    out_w: dict[str, int] = defaultdict(int)
    in_w: dict[str, int] = defaultdict(int)
    out_adj: dict[str, set[str]] = defaultdict(set)
    in_adj: dict[str, set[str]] = defaultdict(set)
    for (a, b), w in internal.items():
        out_w[a] += w
        in_w[b] += w
        out_adj[a].add(b)
        in_adj[b].add(a)

    alive = set(members)
    left: list[str] = []
    right: list[str] = []
    while alive:
        # Drain sinks (no outgoing among alive nodes).
        while True:
            sinks = [n for n in alive if not (out_adj[n] & alive)]
            if not sinks:
                break
            for s in sinks:
                right.append(s)
                alive.discard(s)
        if not alive:
            break
        # Drain sources (no incoming among alive nodes).
        while True:
            sources = [n for n in alive if not (in_adj[n] & alive)]
            if not sources:
                break
            for s in sources:
                left.append(s)
                alive.discard(s)
        if not alive:
            break
        # Pick node with max (out_w_alive - in_w_alive); tie-break alphabetical.
        def delta(n: str) -> int:
            o = sum(w for m, w in [((a, b), internal[(a, b)]) for a, b in internal if a == n and b in alive])
            i = sum(w for m, w in [((a, b), internal[(a, b)]) for a, b in internal if b == n and a in alive])
            return o - i
        # Cheaper delta: recompute by iterating out_adj/in_adj weights.
        def delta_fast(n: str) -> int:
            o = sum(internal.get((n, b), 0) for b in out_adj[n] if b in alive)
            i = sum(internal.get((a, n), 0) for a in in_adj[n] if a in alive)
            return o - i
        pick = max(alive, key=lambda n: (delta_fast(n), n))
        left.append(pick)
        alive.discard(pick)

    order = left + right
    pos = {n: i for i, n in enumerate(order)}
    fas: list[tuple[str, str, int]] = []
    for (a, b), w in internal.items():
        if pos[a] >= pos[b]:
            fas.append((a, b, w))
    fas.sort(key=lambda t: (t[2], t[0], t[1]))
    return fas


def compute_cycle_breakers(
    scc_folders: list[str], leaf_edges: dict, pair_types: dict, max_steps: int = 30
) -> list[dict]:
    """For a cycle SCC, propose a feedback-arc-set refactor sequence (global view).

    Use the FAS to estimate the total surgery needed and pick the lightest edges
    first. The simulation walks the FAS until the cycle is fully broken or until
    max_steps is reached (whichever comes first). This list is informational —
    the per-folder "extraction targets" computed separately are usually more
    actionable for individual PRs.
    """
    members = set(scc_folders)
    internal = {
        (a, b): w for (a, b), w in leaf_edges.items()
        if a in members and b in members and a != b
    }
    fas = _feedback_arc_set(members, internal)
    removed: list[dict] = []
    remaining = dict(internal)
    for (a, b, w) in fas[:max_steps]:
        types_here = sorted(pair_types.get((a, b), []))
        del remaining[(a, b)]
        deps_after: dict[str, set[str]] = defaultdict(set)
        for (x, y) in remaining:
            deps_after[x].add(y)
        sccs_after = _tarjan_sccs(members, deps_after)
        remaining_cycles = [c for c in sccs_after if len(c) > 1]
        biggest = max((len(c) for c in remaining_cycles), default=0)
        removed.append({
            "src": a, "dst": b, "refs": w, "types": types_here,
            "remaining_cycles": len(remaining_cycles), "biggest_cycle": biggest,
        })
        if not remaining_cycles:
            break
    return {
        "total_fas_estimate": len(fas),
        "edges": removed,
    }


def compute_extraction_targets(
    scc_folders: list[str], leaf_edges: dict, pair_types: dict, top_n: int = 30
) -> list[dict]:
    """For a cycle SCC, rank each member by how easy it would be to extract
    individually (i.e., refactor its outgoing references to other cycle members
    so it no longer participates in the cycle).

    Each target lists the internal-outgoing edges with ref counts and the
    specific types that need to be abstracted/moved. Sorted lightest first —
    these are the smallest PR-sized refactors that yield one full extraction.
    """
    members = set(scc_folders)
    by_folder: dict[str, list[tuple[str, int]]] = {f: [] for f in members}
    in_count: dict[str, int] = defaultdict(int)
    for (a, b), w in leaf_edges.items():
        if a in members and b in members and a != b:
            by_folder.setdefault(a, []).append((b, w))
            in_count[b] += 1
    targets: list[dict] = []
    for f, outs in by_folder.items():
        outs_sorted = sorted(outs, key=lambda x: (x[1], x[0]))
        total_refs = sum(w for _, w in outs)
        edges = [
            {
                "dst": b,
                "refs": w,
                "types": sorted(pair_types.get((f, b), []))[:8],
            }
            for b, w in outs_sorted
        ]
        targets.append({
            "folder": f,
            "outgoing_count": len(outs),
            "outgoing_refs": total_refs,
            "incoming_count": in_count.get(f, 0),
            "edges": edges,
        })
    # Lightest first: smallest total refs to refactor, then smallest edge count,
    # then alphabetical.
    targets.sort(key=lambda t: (t["outgoing_refs"], t["outgoing_count"], t["folder"]))
    return targets[:top_n]


def scan(root: Path, include_tests: bool, ignore_patterns: list[str], ext: str):
    """Build:
      - folder_decls: {folder_path: set(declared_type_names)}
      - folder_refs:  {folder_path: set(referenced_type_names)}
      - file_records: [{folder, name, decls, refs}]  per-file granularity
      - leaf_edges, multi_decl_types, all_folders, type_owners
    """
    print(f"Scanning {root} ...", file=sys.stderr)
    files = collect_swift_files(root, include_tests, ignore_patterns, ext)
    print(f"  found {len(files)} {ext} file(s)", file=sys.stderr)
    folder_decls: dict[str, set[str]] = defaultdict(set)
    folder_refs: dict[str, set[str]] = defaultdict(set)
    file_records: list[dict] = []
    for f in files:
        folder = rel_folder(f, root)
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = strip_noise(text)
        decls = set(DECL_RE.findall(text))
        refs = set(REF_RE.findall(text))
        folder_decls[folder].update(decls)
        folder_refs[folder].update(refs)
        file_records.append(
            {"folder": folder, "name": f.name, "decls": sorted(decls), "refs": sorted(refs)}
        )

    # type -> set of folders declaring it (keep ALL declarers, not just unique).
    # When a type is declared in several folders, every declarer gets an edge;
    # the display-time aggregation collapses ones that share a common ancestor.
    owners: dict[str, set[str]] = defaultdict(set)
    for folder, types in folder_decls.items():
        for t in types:
            owners[t].add(folder)
    multi_decl_types = {t for t, fs in owners.items() if len(fs) > 1}

    # leaf-edges: (src_folder, dst_folder) -> ref_count (src != dst).
    leaf_edges: dict[tuple[str, str], int] = defaultdict(int)
    for folder, refs in folder_refs.items():
        for t in refs:
            for tgt in owners.get(t, ()):
                if tgt != folder:
                    leaf_edges[(folder, tgt)] += 1

    all_folders = set(folder_decls) | set(folder_refs)
    type_owners = {t: sorted(fs) for t, fs in owners.items()}
    return (folder_decls, leaf_edges, multi_decl_types, all_folders,
            file_records, type_owners, dict(owners))


def load_index_graph(json_path: Path):
    """Load the resolved folder dependency graph emitted by the index_graph Swift
    tool and return the same shape scan() produces, PLUS a resolved pair_types.

    The Swift tool resolved every reference by USR against the compiler index
    store, so leaf_edges/pair_types are exact — a reference to a type named `Foo`
    points at the one folder that actually declares the bound `Foo`, never at all
    folders that happen to declare a same-named type. We therefore return the
    resolved pair_types and the caller skips the by-name compute_pair_types().

    Returns: (decls, leaf_edges, multi_decl_types, all_folders, file_records,
              type_owners, raw_owners, pair_types)
    """
    print(f"Loading index graph {json_path} ...", file=sys.stderr)
    data = json.loads(json_path.read_text(encoding="utf-8"))

    decls: dict[str, set[str]] = {f: set(names) for f, names in data["folder_decls"].items()}
    file_records = [
        {"folder": r["folder"], "name": r["name"],
         "decls": list(r["decls"]), "refs": list(r["refs"]),
         "ref_owners": [list(p) for p in r.get("ref_owners", [])]}
        for r in data["files"]
    ]
    leaf_edges: dict[tuple[str, str], int] = {
        (e["src"], e["dst"]): e["w"] for e in data["edges"]
    }
    raw_owners: dict[str, set[str]] = {t: set(fs) for t, fs in data["type_owners"].items()}
    type_owners = {t: sorted(fs) for t, fs in raw_owners.items()}
    type_kinds: dict[str, str] = dict(data.get("type_kinds", {}))
    multi_decl_types = {t for t, fs in raw_owners.items() if len(fs) > 1}
    pair_types: dict[tuple[str, str], set[str]] = {
        (pt["src"], pt["dst"]): set(pt["types"]) for pt in data["pair_types"]
    }

    all_folders = set(decls)
    for (a, b) in leaf_edges:
        all_folders.add(a)
        all_folders.add(b)
    for r in file_records:
        all_folders.add(r["folder"])

    print(f"  {len(leaf_edges)} edge(s), {len(all_folders)} folder(s), "
          f"{sum(len(v) for v in decls.values())} type decl(s)", file=sys.stderr)
    return (decls, leaf_edges, multi_decl_types, all_folders,
            file_records, type_owners, raw_owners, pair_types, type_kinds)


def _tarjan_sccs(nodes: set[str], deps: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan's SCC. Returns list of SCCs (each a list of folder ids)."""
    index_counter = [0]
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    sccs: list[list[str]] = []

    def strongconnect(start: str):
        work = [(start, iter(deps.get(start, ())))]
        index_of[start] = index_counter[0]
        lowlink[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack[start] = True

        while work:
            v, it = work[-1]
            pushed = False
            for w in it:
                if w not in index_of:
                    index_of[w] = index_counter[0]
                    lowlink[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append((w, iter(deps.get(w, ()))))
                    pushed = True
                    break
                elif on_stack.get(w):
                    lowlink[v] = min(lowlink[v], index_of[w])
            if pushed:
                continue
            # Finished exploring v
            if lowlink[v] == index_of[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == v:
                        break
                sccs.append(comp)
            work.pop()
            if work:
                parent_v, _ = work[-1]
                lowlink[parent_v] = min(lowlink[parent_v], lowlink[v])

    for n in nodes:
        if n not in index_of:
            strongconnect(n)
    return sccs


def compute_migration_plan(leaf_edges: dict, source_folders: set[str]):
    """Condense the folder graph into SCCs, then produce a topologically-ordered
    migration plan over the condensation DAG. Each step is an SCC (one or more
    folders that must migrate together because they cyclically depend).

    Returns (plan, stuck_sccs) where:
      plan = [{
        step, folders: [folder, ...],            # the SCC to migrate this step
        size: int,
        is_cycle: bool,                          # True if SCC has >1 folder
        unlocks: [{step, folders, size}],        # SCCs that became eligible
      }]
      stuck_sccs = list of SCCs never reachable (shouldn't occur with full DAG
                   plan, but kept for symmetry).
    """
    deps: dict[str, set[str]] = defaultdict(set)
    for (a, b) in leaf_edges:
        if a == b:
            continue
        if a in source_folders and b in source_folders:
            deps[a].add(b)

    sccs = _tarjan_sccs(source_folders, deps)
    # Sort folders inside each SCC alphabetically; assign each folder to an SCC id.
    sccs = [sorted(c) for c in sccs]
    scc_of: dict[str, int] = {}
    for i, comp in enumerate(sccs):
        for f in comp:
            scc_of[f] = i

    # Condensation DAG: scc -> set(scc).
    scc_deps: dict[int, set[int]] = defaultdict(set)
    scc_rdeps: dict[int, set[int]] = defaultdict(set)
    for a, ts in deps.items():
        sa = scc_of[a]
        for b in ts:
            sb = scc_of[b]
            if sa != sb:
                scc_deps[sa].add(sb)
                scc_rdeps[sb].add(sa)

    n_sccs = len(sccs)
    remaining = {i: len(scc_deps.get(i, ())) for i in range(n_sccs)}
    eligible: set[int] = {i for i in range(n_sccs) if remaining[i] == 0}
    migrated: set[int] = set()
    plan: list[dict] = []
    step = 0
    # Pre-sort SCCs by size descending then alphabetical for deterministic ranking.

    def impact(i: int) -> int:
        return sum(
            1
            for s in scc_rdeps.get(i, ())
            if s not in migrated and remaining.get(s, 1) == 1 and s != i
        )

    while eligible:
        # Rank: highest impact, then smallest SCC (easier first), then alphabetical.
        pick = max(
            eligible,
            key=lambda i: (impact(i), -len(sccs[i]), -ord(sccs[i][0][0]) if sccs[i] else 0),
        )
        eligible.remove(pick)
        migrated.add(pick)
        step += 1
        unlocked_now: list[dict] = []
        for s in scc_rdeps.get(pick, ()):
            if s in migrated:
                continue
            remaining[s] -= 1
            if remaining[s] == 0:
                eligible.add(s)
                unlocked_now.append(
                    {"step": None, "folders": sccs[s], "size": len(sccs[s])}
                )
        plan.append({
            "step": step,
            "folders": sccs[pick],
            "size": len(sccs[pick]),
            "is_cycle": len(sccs[pick]) > 1,
            "unlocks": unlocked_now,
        })

    # Backfill the 'step' field on unlocks now that all steps exist.
    step_by_scc: dict[tuple, int] = {tuple(p["folders"]): p["step"] for p in plan}
    for p in plan:
        for u in p["unlocks"]:
            u["step"] = step_by_scc.get(tuple(u["folders"]))

    stuck_sccs = [sccs[i] for i in range(n_sccs) if i not in migrated]
    return plan, stuck_sccs


def build_tree(all_folders: set[str], decls: dict[str, set[str]], root_label: str = "Project"):
    """Return nested dict tree + folder metadata.

    tree[node_id] = {
        'id': node_id,                     # e.g. 'Core/Utils'
        'name': last_segment,
        'parent': parent_id_or_None,
        'children': [child_id, ...],
        'types': declared_type_count_in_subtree,
    }
    Includes synthetic 'ROOT' for top-level so navigation is uniform.
    """
    nodes: dict[str, dict] = {}

    def ensure(path: str) -> dict:
        if path in nodes:
            return nodes[path]
        if path == "":
            nodes[path] = {
                "id": "",
                "name": root_label,
                "parent": None,
                "children": [],
                "types": 0,
            }
            return nodes[path]
        parts = path.split("/")
        parent = "/".join(parts[:-1])
        parent_node = ensure(parent)
        nodes[path] = {
            "id": path,
            "name": parts[-1],
            "parent": parent,
            "children": [],
            "types": 0,
        }
        if path not in parent_node["children"]:
            parent_node["children"].append(path)
        return nodes[path]

    for folder in all_folders:
        ensure(folder)
    # also create chain for any intermediate folders missing
    for path in list(nodes):
        cur = path
        while cur:
            ensure(cur)
            cur = nodes[cur]["parent"] or ""
            if cur == "":
                ensure("")
                break

    # propagate declared-type counts up the tree
    for folder, types in decls.items():
        n = len(types)
        cur: str | None = folder
        while cur is not None:
            nodes[cur]["types"] += n
            cur = nodes[cur]["parent"]

    for n in nodes.values():
        n["children"].sort()
    return nodes


def _package_label(prefix: str) -> str:
    """Human-friendly label for an SPM-prefix path.

    `Modules/Foo/Sources` → `Foo`, `Modules/Foo/Sources/Bar` keeps `Foo`,
    bare `Sources` → `Sources`, `.` → `.`.
    """
    parts = [p for p in prefix.split("/") if p and p != "."]
    if not parts:
        return prefix or "."
    # Drop trailing "Sources" segment for prettier labels.
    if parts[-1] == "Sources" and len(parts) >= 2:
        return parts[-2]
    return parts[-1]


def _build_package_map(all_source_folders, migrated_prefixes):
    """Return (folder→package_id, packages list).

    package_id is `app` for folders not under any SPM prefix, else the prefix
    string itself (stable id). `packages` is a sorted list of
    {id, label, prefix, kind, folders} dicts so the UI can render groups.
    """
    folder_pkg = {}
    pkg_folders = defaultdict(list)
    for f in all_source_folders:
        match = None
        for p in migrated_prefixes:
            pn = p.rstrip("/")
            if f == pn or f.startswith(pn + "/"):
                if match is None or len(pn) > len(match.rstrip("/")):
                    match = p
        pid = match if match else "app"
        folder_pkg[f] = pid
        pkg_folders[pid].append(f)
    packages = []
    for pid, fs in pkg_folders.items():
        if pid == "app":
            packages.append({
                "id": "app", "label": "App (xcodeproj)", "prefix": "",
                "kind": "app", "folders": sorted(fs),
            })
        else:
            packages.append({
                "id": pid, "label": _package_label(pid), "prefix": pid,
                "kind": "spm", "folders": sorted(fs),
            })
    packages.sort(key=lambda p: (p["kind"] != "app", p["label"].lower()))
    return folder_pkg, packages


def render_html(tree, leaf_edges, multi_decl_types, file_records, type_owners,
                plan, stuck, root_label, root_path, initial_migrated,
                migrated_prefixes, out_path, type_kinds=None,
                initial_excluded=None, excluded_file=None,
                folder_package=None, packages=None):
    edges_list = [
        {"src": a, "dst": b, "w": w} for (a, b), w in leaf_edges.items()
    ]
    payload = {
        "tree": tree,
        "edges": edges_list,
        "multi_decl": len(multi_decl_types),
        "files": file_records,
        "type_owners": type_owners,
        "plan": plan,
        "stuck": stuck,
        "root_label": root_label,
        "root_path": root_path,
        "initial_migrated": initial_migrated,
        "migrated_prefixes": migrated_prefixes,
        "type_kinds": type_kinds or {},
        "initial_excluded": initial_excluded or [],
        "excluded_file_name": Path(excluded_file).name if excluded_file else ".modularization_excluded.json",
        "excluded_file_path": str(excluded_file) if excluded_file else "",
        "folder_package": folder_package or {},
        "packages": packages or [],
    }
    html = HTML_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload)).replace(
        "__ROOT_LABEL__", root_label
    )
    out_path.write_text(html, encoding="utf-8")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>__ROOT_LABEL__ — folder dependency explorer</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
  <style>
    /* ── design tokens ─────────────────────────────────────────────────────── */
    :root {
      --bg: #f6f7fb;
      --surface: #ffffff;
      --surface-2: #f1f3f9;
      --border: #e6e8ef;
      --border-strong: #d4d8e3;
      --text: #1c2333;
      --text-dim: #697089;
      --text-faint: #969cb0;
      --accent: #6366f1;        /* indigo-500 */
      --accent-strong: #4f46e5;
      --accent-soft: #eef0fe;
      --green: #16a34a; --green-soft: #e9f9ef;
      --orange: #f59e0b; --orange-soft: #fef4e2;
      --blue: #3b82f6;  --blue-soft: #e8f1fe;
      --gray: #94a3b8;  --gray-soft: #eef1f6;
      --red: #ef4444;
      --r-sm: 8px; --r: 12px; --r-lg: 16px;
      --shadow-sm: 0 1px 2px rgba(20,28,55,.06), 0 1px 3px rgba(20,28,55,.05);
      --shadow: 0 4px 12px rgba(20,28,55,.08), 0 2px 4px rgba(20,28,55,.04);
      --shadow-lg: 0 12px 32px rgba(20,28,55,.12);
      --ring: 0 0 0 3px rgba(99,102,241,.25);
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; height: 100%;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      color: var(--text); background: var(--bg); -webkit-font-smoothing: antialiased; font-size: 13px; }
    code { font-family: ui-monospace, 'SF Mono', Menlo, monospace; background: var(--surface-2); padding: 1px 5px; border-radius: 5px; font-size: 11.5px; }
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-thumb { background: #cbd0dd; border-radius: 8px; border: 2px solid transparent; background-clip: content-box; }
    ::-webkit-scrollbar-thumb:hover { background: #aeb4c6; background-clip: content-box; }

    #wrap { display: flex; height: 100vh; padding: 10px; gap: 10px; }
    #side { width: 412px; flex-shrink: 0; overflow: hidden; background: var(--surface);
      border: 1px solid var(--border); border-radius: var(--r-lg); box-shadow: var(--shadow);
      display: flex; flex-direction: column; }
    #netWrap { flex: 1; position: relative; display: flex; }
    #net { flex: 1; background: var(--surface);
      background-image: radial-gradient(circle at 1px 1px, #e3e6ef 1px, transparent 0);
      background-size: 22px 22px;
      border: 1px solid var(--border); border-radius: var(--r-lg); box-shadow: var(--shadow); overflow: hidden; }
    /* Floating doc-explorer-style nav over the graph: back/forward + crumbs. */
    #graphNav { position: absolute; top: 14px; left: 14px; right: 14px; z-index: 5;
      display: flex; align-items: center; gap: 8px; pointer-events: none; }
    #graphNav > * { pointer-events: auto; }
    #graphNav .navBtns { display: flex; gap: 4px; background: var(--surface);
      border: 1px solid var(--border); border-radius: var(--r); padding: 4px;
      box-shadow: var(--shadow-sm); backdrop-filter: blur(6px); }
    #graphNav .navBtns button { padding: 5px 11px; }
    #graphNav .navBtns button.active { background: var(--accent); color: #fff; border-color: var(--accent-strong); }
    #graphNav .settingsBtn { padding: 7px 14px; font-size: 13px; font-weight: 600;
      display: inline-flex; align-items: center; gap: 6px; }
    #graphNav .settingsBtn span { font-size: 15px; }
    /* Default vis-network tooltip — replaced by our custom popover. */
    .vis-tooltip { display: none !important; }
    /* Mode-scoped visibility: hide migration-only UI in Explore mode and
       explore-only UI in Migration mode without clobbering inline display. */
    html[data-app-mode="explore"] .migrationOnly { display: none !important; }
    html[data-app-mode="migration"] .exploreOnly { display: none !important; }
    /* Custom node popover: stats + action buttons, follows hovered/selected node. */
    .node-popover { position: absolute; z-index: 7; min-width: 230px; max-width: 280px;
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--r);
      box-shadow: var(--shadow-lg); padding: 10px 12px; font-size: 12px;
      color: var(--text); pointer-events: auto; transform: translate(-50%, calc(-100% - 14px)); }
    .node-popover::after { content: ''; position: absolute; left: 50%; bottom: -7px;
      transform: translateX(-50%); width: 12px; height: 12px; background: var(--surface);
      border-right: 1px solid var(--border); border-bottom: 1px solid var(--border);
      transform-origin: center; rotate: 45deg; }
    .np-header { display: flex; align-items: center; gap: 8px; font-weight: 700; margin-bottom: 6px; }
    .np-swatch { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0;
      box-shadow: 0 0 0 1px rgba(0,0,0,0.06); }
    .np-name { font-size: 12.5px; word-break: break-all; }
    .np-stats { font-size: 11.5px; color: var(--text-dim); line-height: 1.55; margin-bottom: 8px; }
    .np-stats b { color: var(--text); font-weight: 600; }
    .np-state { font-size: 11px; padding: 4px 7px; border-radius: var(--r-sm); margin-bottom: 8px;
      display: inline-block; font-weight: 600; }
    .np-state.excluded { background: #fbe9e9; color: #b91c1c; }
    .np-state.migrated { background: #e2e8f0; color: #475569; }
    [data-theme="dark"] .np-state.excluded { background: #3a2a2e; color: #fca5a5; }
    [data-theme="dark"] .np-state.migrated { background: #3a4152; color: #cbd5e1; }
    .np-actions { display: flex; flex-direction: column; gap: 4px; }
    .np-actions button { width: 100%; padding: 6px 10px; font-size: 11.5px; }
    .np-hint { font-size: 10.5px; color: var(--text-faint); margin-top: 6px; }
    /* Settings popover — anchored to gear button, appears below the floating nav. */
    .settings-popover { position: absolute; top: 60px; right: 14px; z-index: 6;
      width: 340px; max-height: calc(100% - 80px); overflow-y: auto;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--r-lg); box-shadow: var(--shadow);
      padding: 14px 16px; }
    #graphNav .crumbs { flex: 1; margin: 0; background: var(--surface);
      border: 1px solid var(--border); box-shadow: var(--shadow-sm); backdrop-filter: blur(6px); }

    /* ── header ────────────────────────────────────────────────────────────── */
    .appbar { padding: 16px 18px 14px; background: linear-gradient(135deg, #1e2334 0%, #2b2f6b 100%); color: #fff; position: relative; }
    .appbar .eyebrow { font-size: 10px; letter-spacing: 1.4px; text-transform: uppercase; color: #a9b0e6; font-weight: 600; }
    .appbar .title { font-size: 18px; font-weight: 700; letter-spacing: -0.3px; margin-top: 2px; }
    .appbar .path { font-size: 11px; color: rgba(255,255,255,.55); word-break: break-all; margin-top: 3px; }

    /* ── top-level mode toggle ────────────────────────────────────────────── */
    .modes { display: flex; gap: 6px; padding: 10px 12px 0; }
    .mode { flex: 1; text-align: center; padding: 9px 10px; font-size: 13px; font-weight: 700; cursor: pointer;
      border: 1px solid var(--border); border-radius: var(--r); background: var(--surface-2); color: var(--text-dim);
      transition: all .15s ease; user-select: none; }
    .mode:hover { color: var(--text); border-color: var(--border-strong); }
    .mode.active { background: linear-gradient(135deg, var(--accent), var(--accent-strong)); color: #fff;
      border-color: var(--accent-strong); box-shadow: var(--shadow-sm); }

    /* ── segmented tabs ────────────────────────────────────────────────────── */
    .tabs { display: flex; gap: 3px; padding: 8px; background: var(--surface-2); margin: 12px; border-radius: var(--r); }
    .tab[data-hidden="1"] { display: none; }
    .tab { flex: 1; text-align: center; padding: 8px 10px; font-size: 12.5px; font-weight: 600; cursor: pointer;
      border-radius: var(--r-sm); color: var(--text-dim); transition: all .15s ease; user-select: none; }
    .tab:hover { color: var(--text); }
    .tab.active { background: var(--surface); color: var(--accent-strong); box-shadow: var(--shadow-sm); }
    .tab-body { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 4px 14px 18px; }
    .panel { display: none; animation: fade .2s ease; }
    .panel.active { display: block; }
    @keyframes fade { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }

    /* ── typography ────────────────────────────────────────────────────────── */
    h2 { margin: 14px 0 8px; font-size: 11px; text-transform: uppercase; letter-spacing: .8px; color: var(--text-faint); font-weight: 700; display: flex; align-items: center; gap: 7px; }
    h3 { margin: 16px 0 6px; font-size: 13px; font-weight: 700; color: var(--text); }
    .small { color: var(--text-dim); font-size: 12px; line-height: 1.5; }
    .info { font-size: 12px; color: var(--text-dim); line-height: 1.55; }

    /* ── breadcrumbs / nav ─────────────────────────────────────────────────── */
    .crumbs { font-size: 12px; margin-bottom: 10px; padding: 7px 10px; background: var(--surface-2);
      border-radius: var(--r-sm); line-height: 1.9; }
    .crumbs a { color: var(--accent-strong); text-decoration: none; cursor: pointer; padding: 2px 7px; border-radius: 6px; transition: background .12s; }
    .crumbs a:hover { background: var(--accent-soft); }

    /* ── pills / badges ────────────────────────────────────────────────────── */
    .pill { display: inline-flex; align-items: center; background: var(--green); color: #fff; padding: 2px 9px; border-radius: 999px; font-size: 11px; font-weight: 700; }
    .pill.gray { background: var(--gray); }

    /* ── lists ─────────────────────────────────────────────────────────────── */
    ul { padding-left: 2px; font-size: 13px; margin: 4px 0; list-style: none; }
    ol { padding-left: 18px; }
    li { margin: 2px 0; cursor: pointer; line-height: 1.45; padding: 4px 8px; border-radius: var(--r-sm); transition: background .12s; }
    li:hover { background: var(--accent-soft); }

    /* ── inputs ────────────────────────────────────────────────────────────── */
    input { width: 100%; padding: 9px 12px; margin: 6px 0 12px; border: 1px solid var(--border-strong); border-radius: var(--r-sm);
      font-size: 13px; font-family: inherit; background: var(--surface); transition: border-color .12s, box-shadow .12s; color: var(--text); }
    input::placeholder { color: var(--text-faint); }
    input:focus { outline: none; border-color: var(--accent); box-shadow: var(--ring); }

    /* ── buttons ───────────────────────────────────────────────────────────── */
    button { background: var(--accent); color: #fff; border: none; padding: 7px 13px; border-radius: var(--r-sm); cursor: pointer;
      font-size: 12px; font-weight: 600; font-family: inherit; transition: background .12s, transform .06s, box-shadow .12s; box-shadow: var(--shadow-sm); }
    button:hover { background: var(--accent-strong); }
    button:active { transform: translateY(1px); }
    button:disabled { background: var(--surface-2); color: var(--text-faint); cursor: default; box-shadow: none; }
    button.ghost { background: var(--surface-2); color: var(--text); box-shadow: none; }
    button.ghost:hover:not(:disabled) { background: var(--border); }
    button.danger { background: var(--red); }
    button.danger:hover { background: #dc2626; }

    /* ── cards / steps ─────────────────────────────────────────────────────── */
    .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--r); padding: 14px 16px; margin-bottom: 10px; box-shadow: var(--shadow-sm); overflow-wrap: anywhere; word-break: break-word; }
    .card.start { background: linear-gradient(135deg, #eafaf1, #f0fdf6); border-color: #b6ebcb; }
    /* Sticky header for the Plan tab: stats + next-step card + global
       actions + list heading + filter all pin together while the step list
       scrolls underneath. Solid surface so list rows don't bleed through. */
    #recSticky { position: sticky; top: -4px; z-index: 4;
      background: var(--surface); padding: 10px 12px 10px;
      margin: -4px -14px 8px; border-bottom: 1px solid var(--border);
      box-shadow: 0 4px 6px -4px rgba(0,0,0,0.18); }
    #recSticky .rec-stats { margin-bottom: 8px; }
    #recSticky #recCard { margin-bottom: 8px; }
    #recSticky .rec-list-heading { margin: 8px 0 6px; }
    #recSticky #planFilter { margin: 0 0 4px; }
    .rec-global-actions { display: flex; justify-content: center; gap: 8px; padding: 2px 0 8px; }
    .rec-global-actions button { min-width: 120px; }
    .rec-global-actions button.success { background: var(--green); border-color: var(--green); color: #fff; }
    .rec-global-actions button.success:hover { filter: brightness(1.08); }
    .rec-global-actions button.danger { background: var(--red); border-color: var(--red); color: #fff; }
    .rec-global-actions button.danger:hover { filter: brightness(1.08); }
    .card.start h3 { color: var(--green); margin-top: 0; }
    .card .card-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
    .card .card-actions button { font-size: 12px; padding: 7px 11px; max-width: 100%; white-space: normal; text-align: left; }
    .card .card-headline { font-size: 13px; font-weight: 600; margin: 4px 0; line-height: 1.35; word-break: break-all; }
    .step { border: 1px solid var(--border); border-radius: var(--r-sm); padding: 9px 11px; margin: 6px 0; background: var(--surface); font-size: 12px; box-shadow: var(--shadow-sm); transition: border-color .12s, box-shadow .12s; overflow-wrap: anywhere; word-break: break-word; cursor: pointer; }
    .step:hover { border-color: var(--border-strong); }
    .step.done { background: var(--green-soft); border-color: #b6ebcb; }
    .step.next { background: var(--orange-soft); border-color: #fcd9a0; box-shadow: var(--ring); }
    .step .stepNum { font-weight: 700; color: var(--text-faint); margin-right: 6px; }
    .step .unlocks { color: var(--text-dim); font-size: 11px; margin-top: 3px; }
    .step .unlocks .u { color: var(--green); font-weight: 600; }
    .step-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .step-actions button { font-size: 11px; padding: 4px 9px; }

    /* ── legend ────────────────────────────────────────────────────────────── */
    .legend { font-size: 12px; color: var(--text); padding: 0; background: transparent;
      display: flex; flex-direction: column; gap: 8px; line-height: 1.35; }
    .legend > div { display: flex; align-items: center; gap: 8px; }
    .legend b { color: var(--text); font-weight: 700; }
    .legend .swatch { display: inline-block; width: 12px; height: 12px; border-radius: 50%;
      flex-shrink: 0; box-shadow: 0 0 0 1px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.08); }
    .legend .swatch.sq { border-radius: 3px; }
    .legend .swatch.star { border-radius: 50%; }
    .legend .swatch.dashed { border: 1.5px dashed #ef4444; background: transparent !important; box-shadow: none; }
    .legend-row-label { color: var(--text); }
    .legend-row-label .dim { color: var(--text-dim); font-weight: 400; }
    /* Kind chips: compact pill with swatch + name, wrap into a tidy grid. */
    .legend-kinds { display: flex; flex-wrap: wrap; gap: 6px; }
    .legend-chip { display: inline-flex; align-items: center; gap: 6px; padding: 3px 8px 3px 6px;
      background: var(--surface-2); border: 1px solid var(--border); border-radius: 999px;
      font-size: 11.5px; color: var(--text); }
    .legend-chip .swatch { width: 9px; height: 9px; box-shadow: none; }
    .legend-divider { height: 1px; background: var(--border); margin: 4px 0; opacity: .8; }
    .legend-edge-line { display: inline-block; width: 22px; height: 2px; border-radius: 2px;
      vertical-align: middle; margin-right: 2px; }
    .stuck { color: var(--red); }
    details > summary { cursor: pointer; font-size: 12px; color: var(--text-dim); margin: 6px 0; font-weight: 600; }
    #newlyBanner { background: var(--orange-soft); border: 1px solid #fcd9a0; padding: 11px 13px; border-radius: var(--r); font-size: 12px; margin: 10px 0; }

    /* ── segmented control (settings) ──────────────────────────────────────── */
    .seg { display: inline-flex; gap: 3px; background: var(--surface-2); padding: 4px; border-radius: var(--r-sm); }
    .seg button { background: transparent; color: var(--text-dim); box-shadow: none; padding: 7px 13px; }
    .seg button:hover:not(.active) { color: var(--text); background: transparent; }
    .seg button.active { background: var(--surface); color: var(--accent-strong); box-shadow: var(--shadow-sm); }

    /* ── always-visible legend dock ────────────────────────────────────────── */
    .legend-dock { position: fixed; right: 24px; bottom: 24px; width: 280px; z-index: 50;
      background: var(--surface); border: 1px solid var(--border); border-radius: var(--r-lg);
      box-shadow: var(--shadow-lg); font-size: 12px; overflow: hidden;
      backdrop-filter: blur(8px); }
    .legend-dock-head { display: flex; justify-content: space-between; align-items: center;
      padding: 10px 14px; font-weight: 700; font-size: 11px; letter-spacing: .6px;
      text-transform: uppercase; cursor: pointer; user-select: none;
      color: var(--text-dim); background: transparent; border-bottom: 1px solid var(--border); }
    .legend-dock-head:hover { color: var(--text); }
    .legend-dock-body { padding: 12px 14px 14px; max-height: 56vh; overflow: auto;
      display: flex; flex-direction: column; gap: 12px; }
    .legend-dock.collapsed .legend-dock-body { display: none; }
    .legend-dock.collapsed .legend-dock-head { border-bottom: none; }
    .legend-dock.collapsed #legendCaret { transform: rotate(-90deg); }
    #legendCaret { transition: transform .15s; color: var(--text-faint); font-size: 10px; }
    .legend-section { display: flex; flex-direction: column; gap: 8px; }
    .legend-group-title { font-size: 10px; text-transform: uppercase; letter-spacing: .8px;
      color: var(--text-faint); font-weight: 700; margin-bottom: 1px; }

    /* ── wizard ────────────────────────────────────────────────────────────── */
    .wiz-row { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: var(--r-sm); cursor: pointer; }
    .wiz-row:hover { background: var(--accent-soft); }
    .wiz-row input { width: auto; margin: 0; }
    .wiz-asgn { display: flex; align-items: center; justify-content: space-between; gap: 8px;
      padding: 5px 8px; border-bottom: 1px solid var(--border); font-size: 12px; }
    .wiz-asgn:last-child { border-bottom: none; }
    .wiz-folder { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 11px;
      color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
    .wiz-chip { background: var(--accent-soft); color: var(--accent-strong); padding: 3px 9px;
      border-radius: 999px; font-size: 11px; font-weight: 600; border: 1px solid var(--accent); box-shadow: none; }
    .wiz-chip:hover { background: var(--accent); color: #fff; }

    /* ── plan target banner ────────────────────────────────────────────────── */
    .target-banner { background: linear-gradient(135deg, var(--accent-soft), var(--surface)); border: 1px solid var(--accent); }
    .target-banner h3 { color: var(--accent-strong); margin-top: 0; }
    .path-step { display: flex; gap: 9px; align-items: baseline; padding: 7px 10px; margin: 4px 0; border-radius: var(--r-sm); background: var(--surface); border: 1px solid var(--border); font-size: 12px; box-shadow: var(--shadow-sm); }
    .path-step .ord { font-weight: 700; color: var(--accent-strong); min-width: 20px; }
    .path-step.is-target { background: var(--accent-soft); border-color: var(--accent); }
    .path-step.done { background: var(--green-soft); border-color: #b6ebcb; }

    /* ── DARK THEME ────────────────────────────────────────────────────────── */
    :root[data-theme="dark"] {
      --bg: #0e1016; --surface: #171a23; --surface-2: #1e2230; --border: #2a2f3e; --border-strong: #3a4152;
      --text: #e7e9f0; --text-dim: #9aa1b5; --text-faint: #6b7286;
      --accent: #818cf8; --accent-strong: #a5b4fc; --accent-soft: #232844;
      --green: #34d399; --green-soft: #11261e;
      --orange: #fbbf24; --orange-soft: #2a2113;
      --blue: #60a5fa; --blue-soft: #15233d;
      --gray: #64748b; --gray-soft: #1c212c;
      --shadow-sm: 0 1px 2px rgba(0,0,0,.4); --shadow: 0 4px 14px rgba(0,0,0,.5); --shadow-lg: 0 14px 38px rgba(0,0,0,.6);
      --ring: 0 0 0 3px rgba(129,140,248,.35);
    }
    @media (prefers-color-scheme: dark) {
      :root[data-theme="auto"] {
        --bg: #0e1016; --surface: #171a23; --surface-2: #1e2230; --border: #2a2f3e; --border-strong: #3a4152;
        --text: #e7e9f0; --text-dim: #9aa1b5; --text-faint: #6b7286;
        --accent: #818cf8; --accent-strong: #a5b4fc; --accent-soft: #232844;
        --green: #34d399; --green-soft: #11261e;
        --orange: #fbbf24; --orange-soft: #2a2113;
        --blue: #60a5fa; --blue-soft: #15233d;
        --gray: #64748b; --gray-soft: #1c212c;
        --shadow-sm: 0 1px 2px rgba(0,0,0,.4); --shadow: 0 4px 14px rgba(0,0,0,.5); --shadow-lg: 0 14px 38px rgba(0,0,0,.6);
        --ring: 0 0 0 3px rgba(129,140,248,.35);
      }
    }
    :root[data-theme="dark"] #net,
    :root[data-theme="dark"] .card.start { background-image: none; }
    :root[data-theme="dark"] .card.start { background-color: var(--surface); }
    :root[data-theme="dark"] #net { background-image: radial-gradient(circle at 1px 1px, #232838 1px, transparent 0); }
    @media (prefers-color-scheme: dark) {
      :root[data-theme="auto"] #net,
      :root[data-theme="auto"] .card.start { background-image: none; }
      :root[data-theme="auto"] .card.start { background-color: var(--surface); }
      :root[data-theme="auto"] #net { background-image: radial-gradient(circle at 1px 1px, #232838 1px, transparent 0); }
    }
  </style>
</head>
<body>
<div id="wrap">
  <div id="side">
    <div class="appbar">
      <div class="eyebrow">Module graph</div>
      <div class="title" id="projectLabel">__ROOT_LABEL__</div>
      <div class="path" id="projectPath"></div>
    </div>
    <div class="modes">
      <div class="mode active" data-mode="explore">🔍 Explore mode</div>
      <div class="mode" data-mode="migration">🧭 Migration mode</div>
    </div>
    <div class="tabs">
      <div class="tab" data-tab="wizard" data-mode="migration">🧭 Setup</div>
      <div class="tab" data-tab="plan" data-mode="migration">🎯 Plan</div>
      <div class="tab active" data-tab="explore" data-mode="both">🌳 Hierarchy</div>
    </div>

    <div class="tab-body">
      <!-- EXPLORE TAB -->
      <div class="panel active" id="panel-explore">
        <div class="small migrationOnly">Click a circle to drill in. Use <b>✓ migrate</b> to mark a folder migrated, or <b>🎯 plan</b> to generate the path to extract it. Clicking never migrates.</div>
        <div class="small exploreOnly">Click a circle to drill in. Clicking never mutates state.</div>
        <input id="filter" placeholder="filter children..." />
        <h2>Hierarchy</h2>
        <span id="leafCount" style="display:none;"></span>
        <ul id="kids"></ul>
        <div id="newlyBanner" class="migrationOnly" style="display:none;"></div>
        <div class="migrationOnly">
          <h2 style="margin-top: 16px;">Migrated <span class="pill gray" id="migCount2">0</span></h2>
          <ul id="migList" style="font-size:12px;"></ul>
          <h2 style="margin-top: 16px;">🚫 Won't modularize <span class="pill gray" id="exclCount">0</span></h2>
          <ul id="exclList" style="font-size:12px;"></ul>
          <div id="exclSync" style="display:none;margin:6px 0;"></div>
        </div>
      </div>

      <!-- MIGRATION WIZARD TAB -->
      <div class="panel" id="panel-wizard">
        <h3 style="margin-top: 4px;">What are you migrating?</h3>
        <div class="small" style="margin-bottom: 10px;">
          Pick where code is moving <b>from</b>, where it should land,
          and we'll spit out an ordered step-by-step plan respecting your real dependency graph.
        </div>

        <h2>1 · Source <span class="small" style="font-weight:500;">(extract folders out of)</span></h2>
        <div id="wizSource"></div>

        <h2 style="margin-top:14px;">2 · Targets <span class="small" style="font-weight:500;">(destinations)</span></h2>
        <div class="small" style="margin-bottom:6px;">Tick one or more existing SPM packages, and/or add new ones.</div>
        <div id="wizTargets"></div>
        <div style="display:flex;gap:6px;margin:6px 0 10px;">
          <input id="wizNewTarget" placeholder="New package name…" style="margin:0;flex:1;" />
          <button id="wizAddTarget" class="ghost">+ Add</button>
        </div>

        <h2 style="margin-top:14px;">3 · Assignment <span class="small" style="font-weight:500;">(which folder → which target)</span></h2>
        <div class="small" style="margin-bottom:6px;">
          Defaults to the first target. Click a chip to cycle through chosen targets, or
          tag a folder as <b>stay</b> to keep it in the source.
        </div>
        <div id="wizAssign" style="max-height: 280px; overflow:auto; padding: 4px;"></div>

        <div style="display:flex;gap:6px;margin-top:14px;">
          <button id="wizCompute">⚙ Compute plan →</button>
          <button id="wizReset" class="ghost">Reset</button>
        </div>
      </div>

      <!-- PLAN TAB -->
      <div class="panel" id="panel-plan">
        <div id="targetBanner" style="display:none;"></div>
        <div id="recSticky">
          <div class="info rec-stats">
            <div>Already in SPM (baseline): <b id="baselineCount">0</b> folder(s) <span id="baselinePrefixes" style="font-size: 11px; color: var(--text-faint);"></span></div>
            <div>Migrated by you so far: <b id="userMigCount">0</b> of <b id="totalToMigrate">0</b> folders to extract.</div>
            <span id="stuckInfo"></span>
          </div>
          <div id="recCard" class="card start"></div>
          <div class="rec-global-actions">
            <button id="applyAll" class="success">Apply full plan</button>
            <button id="resetMig" class="danger">Reset</button>
          </div>
          <h3 id="planListHeading" class="rec-list-heading">Recommended order</h3>
          <input id="planFilter" placeholder="filter steps..." />
        </div>
        <div id="planList"></div>
        <details id="stuckDetails" style="margin-top: 10px;">
          <summary>Stuck in cycles (<span id="stuckCount">0</span>)</summary>
          <ul id="stuckList" style="font-size: 12px;"></ul>
        </details>
      </div>

    </div>
  </div>

  <div id="netWrap">
    <div id="graphNav">
      <div class="navBtns">
        <button id="navBack" class="ghost" title="Back (previous view)" disabled>←</button>
        <button id="navFwd" class="ghost" title="Forward" disabled>→</button>
      </div>
      <div class="crumbs" id="crumbs"></div>
      <div class="navBtns">
        <button id="navSettings" class="ghost settingsBtn" title="Settings"><span>⚙</span> Settings</button>
      </div>
    </div>
    <div class="panel settings-popover" id="panel-settings">
      <h3 style="margin-top:0;">Appearance</h3>
      <div class="small" style="margin-bottom:8px;">Theme</div>
      <div class="seg" id="themeSeg">
        <button data-theme="light">☀ Light</button>
        <button data-theme="dark">🌙 Dark</button>
        <button data-theme="auto">🖥 Auto</button>
      </div>

      <h3>How to use</h3>
      <ol style="font-size: 12px; line-height: 1.7; padding-left:18px; color:var(--text-dim);">
        <li><b>Explore</b> the graph — click a circle to drill in.</li>
        <li>Found something you want to extract? Hit <b>🎯 plan</b> on it — the Plan tab shows the exact ordered path of folders to migrate first.</li>
        <li>Or open <b>Plan</b> for the global recommended order; click <b>Migrate</b> on a step to simulate extraction and watch what unlocks (orange).</li>
        <li>The <b>Legend</b> (bottom-right of the graph) is always visible.</li>
      </ol>

      <h3>About</h3>
      <div class="info">
        Each folder with <code>.swift</code> files is a node. An edge <b>A → B</b> means a file in A references a type declared in B. References are resolved by USR from the compiler index store, so name collisions don't create phantom edges.
      </div>
      <div class="info" style="margin-top: 10px;">
        Types declared in 2+ folders: <span id="amb"></span> (all declarers kept).
      </div>
    </div>
    <div id="net"></div>
    <div id="nodePopover" class="node-popover" style="display:none;"></div>
  </div>

  <!-- Always-visible legend dock (floats over the graph) -->
  <div id="legendDock" class="legend-dock">
    <div class="legend-dock-head" id="legendToggle">
      <span>Legend</span>
      <span id="legendCaret">▾</span>
    </div>
    <div class="legend-dock-body" id="legendBody">
      <div class="legend-section exploreOnly">
        <div class="legend-group-title">Nodes</div>
        <div class="legend">
          <div><span class="swatch" style="background:#6366f1"></span><span class="legend-row-label">Folder</span></div>
          <div><span class="swatch" style="background:#eab308"></span><span class="legend-row-label">Loose files anchor <span class="dim">(★ in this folder)</span></span></div>
          <div><span class="swatch sq" style="background:#fde68a"></span><span class="legend-row-label">Swift file</span></div>
        </div>
      </div>
      <div class="legend-section migrationOnly">
        <div class="legend-group-title">Folders</div>
        <div class="legend">
          <div><span class="swatch" style="background:#22c55e"></span><span class="legend-row-label">Migratable leaf <span class="dim">(no deps)</span></span></div>
          <div><span class="swatch" style="background:#f59e0b"></span><span class="legend-row-label">Newly unlocked</span></div>
          <div><span class="swatch" style="background:#3b82f6"></span><span class="legend-row-label">Has dependencies</span></div>
          <div><span class="swatch" style="background:#cbd5e1"></span><span class="legend-row-label">Already migrated</span></div>
          <div><span class="swatch dashed"></span><span class="legend-row-label">Won't modularize</span></div>
        </div>
      </div>
      <div class="legend-divider"></div>
      <div class="legend-section">
        <div class="legend-group-title">Type view</div>
        <div class="legend">
          <div><span class="swatch" style="background:#8b5cf6"></span><span class="legend-row-label">Inspected folder <span class="dim">(★)</span></span></div>
          <div><span class="swatch sq" style="background:#eef1f6;border:1px solid #cbd5e1;box-shadow:none;"></span><span class="legend-row-label">File</span></div>
          <div><span class="swatch" style="background:#e2e8f0"></span><span class="legend-row-label">External folder</span></div>
          <div class="legend-kinds">
            <span class="legend-chip"><span class="swatch sq" style="background:#3b82f6"></span>class</span>
            <span class="legend-chip"><span class="swatch sq" style="background:#22c55e"></span>struct</span>
            <span class="legend-chip"><span class="swatch sq" style="background:#f59e0b"></span>enum</span>
            <span class="legend-chip"><span class="swatch sq" style="background:#a855f7"></span>protocol</span>
            <span class="legend-chip"><span class="swatch sq" style="background:#14b8a6"></span>typealias</span>
          </div>
        </div>
      </div>
      <div class="legend-divider"></div>
      <div class="legend-section">
        <div class="legend-group-title">Edges</div>
        <div class="legend">
          <div><span class="legend-edge-line" style="background:#ef4444"></span><span class="legend-row-label">Outbound <span class="dim">— this → other</span></span></div>
          <div><span class="legend-edge-line" style="background:#3b82f6"></span><span class="legend-row-label">Inbound <span class="dim">— other → this</span></span></div>
          <div><span class="legend-edge-line" style="background:#9ca3af"></span><span class="legend-row-label">Thickness = reference count</span></div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
const DATA = __PAYLOAD__;
const tree = DATA.tree;
const edges = DATA.edges;
const files = DATA.files;
const typeOwners = DATA.type_owners;
const typeKinds = DATA.type_kinds || {};   // type name -> "class"|"struct"|"enum"|"protocol"|"typealias"
function kindOf(t) { return typeKinds[t] || 'type'; }
const plan = DATA.plan;            // [{step, folder, unlocks: [...]}]
const stuck = DATA.stuck;
document.getElementById('amb').textContent = DATA.multi_decl;

const filesByFolder = {};
for (const f of files) {
  (filesByFolder[f.folder] = filesByFolder[f.folder] || []).push(f);
}
const totalSource = Object.keys(filesByFolder).length;
if (DATA.root_path) document.getElementById('projectPath').textContent = DATA.root_path;

// ── theme (light / dark / auto) ─────────────────────────────────────────────
const THEME_KEY = 'modgraph-theme';
function themeText() { return (getComputedStyle(document.documentElement).getPropertyValue('--text') || '#1c2333').trim(); }
function isDark() {
  const t = document.documentElement.getAttribute('data-theme');
  if (t === 'dark') return true;
  if (t === 'light') return false;
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}
// vis-network node palettes. Saturated fills read on both themes; the pale
// "neutral" groups (migrated / excluded / file / external) flip to dark
// surfaces under a dark theme so they stop glaring as white blobs.
// Folder graph uses shape:'dot' — labels render OUTSIDE the dot, on the canvas.
// So label color must contrast with the canvas (themeText, applied globally via
// nodes.font), NOT the dot fill. Only fills/borders are set here.
function folderGroups() {
  const d = isDark();
  return {
    leaf:     { color: { background: '#22c55e', border: '#16a34a' } },
    newly:    { color: { background: '#f59e0b', border: '#d97706' }, borderWidth: 3 },
    migrated: d ? { color: { background: '#3a4152', border: '#586074' } }
                : { color: { background: '#cbd5e1', border: '#94a3b8' } },
    excluded: d ? { color: { background: '#3a2a2e', border: '#f87171' }, borderWidth: 2, shapeProperties: { borderDashes: [4, 3] } }
                : { color: { background: '#fbe9e9', border: '#ef4444' }, borderWidth: 2, shapeProperties: { borderDashes: [4, 3] } },
    mid:      { color: { background: '#3b82f6', border: '#2563eb' } },
    spm:      { color: { background: '#8b5cf6', border: '#7c3aed' }, borderWidth: 2 },
    // Explore-mode: every folder uses the same neutral indigo regardless of
    // leaf/migration state — kind colors (class/struct/enum/...) carry meaning.
    folder:   { color: { background: '#6366f1', border: '#4f46e5' } },
    external: d ? { color: { background: '#475569', border: '#64748b' } }
                : { color: { background: '#334155', border: '#1e293b' } },
  };
}
function typeGroups() {
  const d = isDark();
  return {
    self:           { color: { background: '#8b5cf6', border: '#7c3aed' }, font: { color: '#fff' } },
    file:           d ? { color: { background: '#2a3142', border: '#3f4862' }, font: { size: 11, color: '#c2c8d6' } }
                      : { color: { background: '#eef1f6', border: '#cbd5e1' }, font: { size: 11, color: '#475569' } },
    kind_class:     { color: { background: '#3b82f6', border: '#2563eb' }, font: { color: '#fff' } },
    kind_struct:    { color: { background: '#22c55e', border: '#16a34a' }, font: { color: '#06281a' } },
    kind_enum:      { color: { background: '#f59e0b', border: '#d97706' }, font: { color: '#3d2c06' } },
    kind_protocol:  { color: { background: '#a855f7', border: '#9333ea' }, font: { color: '#fff' } },
    kind_typealias: { color: { background: '#14b8a6', border: '#0d9488' }, font: { color: '#fff' } },
    kind_type:      { color: { background: '#eab308', border: '#ca8a04' }, font: { color: '#3d2c06' } },
    type:           { color: { background: '#eab308', border: '#ca8a04' }, font: { color: '#3d2c06' } },
    ext:            d ? { color: { background: '#475569', border: '#64748b' }, font: { color: '#e7e9f0' } }
                      : { color: { background: '#e2e8f0', border: '#94a3b8' }, font: { color: '#334155' } },
  };
}
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
  document.querySelectorAll('#themeSeg button').forEach(b => b.classList.toggle('active', b.dataset.theme === t));
  if (network) {
    // Theme swap changes group colors + font color — bypass the update path.
    lastRenderedFocusId = undefined; folderNodesDS = null; folderEdgesDS = null;
    render();
  }
}
(function initTheme() {
  let t = 'auto';
  try { t = localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) {}
  document.documentElement.setAttribute('data-theme', t);
})();

// ── target-driven migration planning ────────────────────────────────────────
// "I want to move X" → compute everything that must be migrated first.
let migrationTarget = null;
const depOut = {};                                   // folder -> Set(folders it depends on), USR-resolved
for (const e of edges) { (depOut[e.src] = depOut[e.src] || new Set()).add(e.dst); }
function sourceDescendants(id) {                      // descendants that actually hold .swift files
  return [...descendantsOf(id)].filter(d => (filesByFolder[d] || []).length > 0);
}
// Transitive closure of the target's outgoing deps (everything it needs), plus
// the target's own source folders, minus what's already migrated.
function migrationClosure(targetId) {
  const seedSet = new Set(sourceDescendants(targetId));
  const closure = new Set(seedSet);
  const stack = [...seedSet];
  while (stack.length) {
    const f = stack.pop();
    for (const d of (depOut[f] || [])) { if (!closure.has(d)) { closure.add(d); stack.push(d); } }
  }
  for (const f of [...closure]) if (outOfScope(f)) closure.delete(f);
  return { closure, seedSet };
}
function setTarget(id) { migrationTarget = id; switchTab('plan'); renderPlan(); }
function clearTarget() { migrationTarget = null; renderPlan(); }

// Tab switching
document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => {
    if (t.dataset.hidden === '1') return;
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('panel-' + t.dataset.tab).classList.add('active');
  };
});

// ── top-level mode switching (Explore | Migration) ───────────────────────────
// Each tab declares which mode(s) it belongs to via data-mode. Mode change
// hides tabs from the other mode and falls back to the mode's first tab.
let currentMode = 'explore';
function applyMode(mode) {
  currentMode = mode;
  document.querySelectorAll('.mode').forEach(m =>
    m.classList.toggle('active', m.dataset.mode === mode));
  let firstVisible = null;
  document.querySelectorAll('.tab').forEach(t => {
    const dm = t.dataset.mode || 'both';
    const visible = dm === mode || dm === 'both';
    t.dataset.hidden = visible ? '0' : '1';
    if (visible && firstVisible === null && t.dataset.tab !== 'settings') firstVisible = t;
  });
  // If active tab now hidden, switch to mode's first non-settings tab.
  const active = document.querySelector('.tab.active');
  if (!active || active.dataset.hidden === '1') {
    if (firstVisible) {
      document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
      firstVisible.classList.add('active');
      document.getElementById('panel-' + firstVisible.dataset.tab).classList.add('active');
      if (firstVisible.dataset.tab === 'wizard') renderWizard();
      if (firstVisible.dataset.tab === 'plan') renderPlan();
    }
  }
  // Use a root-level mode flag + CSS so we don't clobber inline display values
  // (e.g. newlyBanner is normally `display:none` and only shown after applyStep).
  document.documentElement.setAttribute('data-app-mode', mode);
  // Explore mode only has one tab (Graph) — hide the whole tab bar.
  const tabsBar = document.querySelector('.tabs');
  if (tabsBar) tabsBar.style.display = (mode === 'explore') ? 'none' : '';
  // Re-render the graph so node grouping (spm vs migrated) refreshes.
  if (typeof network !== 'undefined' && network) { try { render(); } catch (e) {} }
}
document.querySelectorAll('.mode').forEach(m => {
  m.onclick = () => applyMode(m.dataset.mode);
});

// Index every leaf-edge endpoint to its ancestor chain for fast aggregation.
function ancestors(id) {
  const chain = [];
  let cur = id;
  while (cur !== null && cur !== undefined) {
    chain.push(cur);
    cur = (tree[cur] && tree[cur].parent !== undefined) ? tree[cur].parent : null;
    if (cur === null) break;
  }
  return chain;
}
// Resolve which displayed-node owns a leaf-folder, given the set of displayed nodes.
function ownerInDisplay(leafId, displaySet) {
  let cur = leafId;
  while (cur !== null) {
    if (displaySet.has(cur)) return cur;
    cur = tree[cur].parent;
  }
  return null;
}

// Initial focus: prefer the largest non-migrated top-level subtree so the user
// lands on the "to-migrate" side of the project (typically the app's main
// source tree) instead of the noisy root that mixes SPM packages + docs + caches.
function pickInitialFocus() {
  const root = tree[''];
  if (!root || !root.children || root.children.length === 0) return '';
  function subtreeSourceCount(id, migratedCheck) {
    let c = 0;
    const seen = new Set();
    function walk(x) {
      if (seen.has(x)) return;
      seen.add(x);
      const f = (DATA.files || []).length; // unused
      (tree[x].children || []).forEach(walk);
    }
    // Simpler: precompute below via iteration over filesByFolder.
    return 0;
  }
  // Count source folders per top-level child that are NOT initially migrated.
  const counts = {};
  Object.keys(filesByFolder).forEach(f => {
    const top = f.split('/')[0] || '.';
    if (!(DATA.initial_migrated || []).includes(f)) {
      counts[top] = (counts[top] || 0) + 1;
    }
  });
  let best = '';
  let bestCount = 0;
  for (const id of root.children) {
    const top = (tree[id].name);
    const c = counts[top] || 0;
    if (c > bestCount) { bestCount = c; best = id; }
  }
  return best || '';
}
// Start at the project root so Explore opens on the top-level folders (the
// app target, local packages, …) instead of diving straight into a huge subtree.
let focusId = '';
let network = null;
// Track the focusId of the currently mounted network so state toggles
// (migrate/unmigrate/exclude) can do in-place node/edge updates instead of
// tearing the whole vis-network down — which would re-stabilize the layout
// and feel like a full re-render every time the user clicks a button.
let lastRenderedFocusId = undefined;
let folderNodesDS = null;
let folderEdgesDS = null;

// ── view history (back / forward) ─────────────────────────────────────────────
// Every navigation goes through go(); pure repaints (filter, migrate) call
// render() directly and don't touch history.
let viewHistory = [focusId];
let viewPos = 0;
function go(id) {
  if (id === focusId) return;                    // already here — no-op, keep graph layout
  viewHistory = viewHistory.slice(0, viewPos + 1); // drop any forward entries
  viewHistory.push(id);
  viewPos = viewHistory.length - 1;
  focusId = id;
  render();
  updateNavButtons();
}
function goHistory(delta) {
  const next = viewPos + delta;
  if (next < 0 || next >= viewHistory.length) return;
  viewPos = next;
  focusId = viewHistory[viewPos];
  render();
  updateNavButtons();
}
function updateNavButtons() {
  const b = document.getElementById('navBack');
  const f = document.getElementById('navFwd');
  if (b) b.disabled = viewPos <= 0;
  if (f) f.disabled = viewPos >= viewHistory.length - 1;
}
const INITIAL_MIGRATED = new Set(DATA.initial_migrated || []);
let migrated = new Set(INITIAL_MIGRATED);  // pre-populated with already-SPM folders
let currentOutDeg = new Map();
let lastNewlyRevealed = [];

// ── "won't be modularized" exclusions ───────────────────────────────────────
// Disk file (DATA.initial_excluded, read by Python every run) seeds the FIRST
// open; after that localStorage is the working source of truth so toggles
// survive HTML re-renders with zero manual steps. The sync badge prompts a
// one-click download of the file whenever the browser state drifts from disk.
const INITIAL_EXCLUDED = new Set(DATA.initial_excluded || []);
const EXCL_KEY = 'modgraph-excluded::' + (DATA.root_path || '');
const EXCLUDED_FILE = DATA.excluded_file_name || '.modularization_excluded.json';
const EXCLUDED_FILE_PATH = DATA.excluded_file_path || EXCLUDED_FILE;
function loadExcludedLS() {
  try { const v = JSON.parse(localStorage.getItem(EXCL_KEY) || 'null');
        if (Array.isArray(v)) return new Set(v); } catch (e) {}
  return null;
}
function saveExcludedLS() {
  try { localStorage.setItem(EXCL_KEY, JSON.stringify([...excluded].sort())); } catch (e) {}
}
let excluded = loadExcludedLS() || new Set(INITIAL_EXCLUDED);

// All source folders in id's subtree are excluded (mirrors isAllMigrated).
function isAllExcluded(id) {
  let anySource = false;
  for (const d of descendantsOf(id)) {
    if ((filesByFolder[d] || []).length === 0) continue;
    anySource = true;
    if (!excluded.has(d)) return false;
  }
  return anySource;
}
// Migrated OR excluded — both are out of the migration plan and hide their edges.
function outOfScope(id) {
  // Explore mode keeps SPM folders in scope so SPM-to-SPM coupling is visible.
  // Migration mode drops migrated/excluded so they don't pollute the plan view.
  if (currentMode === 'explore') return excluded.has(id);
  return migrated.has(id) || excluded.has(id);
}

function toggleExclude(displayNodeId) {
  const sub = [...descendantsOf(displayNodeId)].filter(d => (filesByFolder[d] || []).length > 0);
  if (sub.length === 0) return;
  const allExcl = sub.every(d => excluded.has(d));
  sub.forEach(d => { if (allExcl) excluded.delete(d); else excluded.add(d); });
  saveExcludedLS();
  document.getElementById('newlyBanner').style.display = 'none';
  lastNewlyRevealed = [];
  updateExcludeSidebar();
  renderPlan();
  render();
}

// Symmetric diff vs the on-disk set — how many marks aren't saved yet.
function unsavedExclusions() {
  let n = 0;
  for (const x of excluded) if (!INITIAL_EXCLUDED.has(x)) n++;
  for (const x of INITIAL_EXCLUDED) if (!excluded.has(x)) n++;
  return n;
}
function downloadExclusions() {
  const data = JSON.stringify([...excluded].sort(), null, 2);
  const blob = new Blob([data], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = EXCLUDED_FILE;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function updateExcludeSidebar() {
  // Dedup to top-most excluded ancestor (same approach as the migrated list).
  const items = [];
  Object.keys(tree).forEach(id => {
    if (!id) return;
    if (!isAllExcluded(id)) return;
    const parent = tree[id].parent;
    if (parent !== null && parent !== undefined && isAllExcluded(parent)) return;
    items.push(id);
  });
  items.sort();
  document.getElementById('exclCount').textContent = items.length;
  const ul = document.getElementById('exclList');
  ul.innerHTML = '';
  items.forEach(id => {
    const li = document.createElement('li');
    li.innerHTML = '🚫 ' + escapeHtml(id) + ' <span style="color:var(--text-faint);">↩ put back</span>';
    li.title = 'Click to put this folder back into the migration plan';
    li.onclick = () => toggleExclude(id);
    ul.appendChild(li);
  });
  const sync = document.getElementById('exclSync');
  const n = unsavedExclusions();
  sync.innerHTML = '';
  if (n === 0) {
    sync.style.display = 'none';
  } else {
    sync.style.display = 'block';
    const msg = document.createElement('div');
    msg.style.cssText = 'font-size:11px;color:var(--orange);margin-bottom:6px;';
    msg.textContent = '⚠ ' + n + ' change(s) not saved to disk';
    sync.appendChild(msg);
    const b = document.createElement('button');
    b.textContent = '⬇ Save to ' + EXCLUDED_FILE;
    b.title = 'Downloads ' + EXCLUDED_FILE + '. Move it to ' + EXCLUDED_FILE_PATH
            + ' (replace the existing file) so the next `just tree`/`just list` picks it up.';
    b.onclick = downloadExclusions;
    sync.appendChild(b);
  }
}

document.getElementById('resetMig').onclick = () => {
  migrated = new Set(INITIAL_MIGRATED);
  lastNewlyRevealed = [];
  document.getElementById('newlyBanner').style.display = 'none';
  renderPlan();
  render();
};
document.getElementById('applyAll').onclick = () => {
  migrated = new Set(INITIAL_MIGRATED);
  plan.forEach(s => s.folders.forEach(f => migrated.add(f)));
  lastNewlyRevealed = [];
  document.getElementById('newlyBanner').style.display = 'none';
  renderPlan();
  render();
};
document.getElementById('planFilter').oninput = () => renderPlan();

function descendantsOf(id) {
  const out = new Set();
  function walk(x) {
    out.add(x);
    (tree[x].children || []).forEach(walk);
  }
  walk(id);
  return out;
}

function isAllMigrated(id) {
  let anySource = false;
  for (const d of descendantsOf(id)) {
    if ((filesByFolder[d] || []).length === 0) continue;
    anySource = true;
    if (!migrated.has(d)) return false;
  }
  return anySource;
}

function snapshotLeavesAtFocus() {
  const focus = tree[focusId];
  const displaySet = new Set(focus.children);
  const outDeg = new Map();
  for (const e of edges) {
    if (outOfScope(e.src) || outOfScope(e.dst)) continue;
    const a = ownerInDisplay(e.src, displaySet);
    const b = ownerInDisplay(e.dst, displaySet);
    if (!a) continue;
    if (a === b) continue;
    outDeg.set(a, (outDeg.get(a) || 0) + 1);
  }
  return new Set(focus.children.filter(id => !(outDeg.get(id) > 0) && !isAllMigrated(id) && !isAllExcluded(id)));
}

function goUp() {
  const cur = tree[focusId];
  if (cur && cur.parent !== null && cur.parent !== undefined) {
    go(cur.parent);
  }
}

function migrate(displayNodeId) {
  const before = snapshotLeavesAtFocus();
  // mark every leaf-folder in subtree as migrated
  descendantsOf(displayNodeId).forEach(d => migrated.add(d));
  const after = snapshotLeavesAtFocus();
  lastNewlyRevealed = [...after].filter(x => !before.has(x));
  render();
  // Banner
  const banner = document.getElementById('newlyBanner');
  if (lastNewlyRevealed.length === 0) {
    banner.innerHTML = '<b>Migrated:</b> ' + displayNodeId + '<br/>No new leaves revealed at this zoom.';
  } else {
    banner.innerHTML = '<b>Migrated:</b> ' + displayNodeId + '<br/><b>Newly migratable (' + lastNewlyRevealed.length + '):</b><br/>' + lastNewlyRevealed.map(x => '• ' + x).join('<br/>');
  }
  banner.style.display = 'block';
  updateMigratedSidebar();
  renderPlan();
}

// True only for folders that were ALREADY in SPM at load (baseline). Those
// can't be un-migrated; user-applied migrations can.
function isInitialMigrated(id) {
  for (const d of descendantsOf(id)) {
    if ((filesByFolder[d] || []).length === 0) continue;
    if (!INITIAL_MIGRATED.has(d)) return false;
  }
  return true;
}

// Undo a user migration: clear this subtree's folders from `migrated`, but keep
// any that were part of the SPM baseline.
function unmigrate(displayNodeId) {
  descendantsOf(displayNodeId).forEach(d => {
    if (!INITIAL_MIGRATED.has(d)) migrated.delete(d);
  });
  lastNewlyRevealed = [];
  const banner = document.getElementById('newlyBanner');
  banner.innerHTML = '<b>↩ Un-migrated:</b> ' + displayNodeId;
  banner.style.display = 'block';
  updateMigratedSidebar();
  renderPlan();
  render();
}

function stepIsDone(step) {
  return step.folders.every(f => migrated.has(f));
}
function stepLabel(step) {
  if (step.size === 1) return step.folders[0];
  return '⚠ Cycle of ' + step.size + ' folders';
}
function stepUnlocksText(step) {
  if (step.unlocks.length === 0) return null;
  return step.unlocks.map(u => u.size === 1 ? u.folders[0] : ('cycle×' + u.size)).join(', ');
}

function renderPlan() {
  if (migrationTarget) { renderTargetPlan(); return; }
  // Normal (global) plan mode — restore the default chrome.
  document.getElementById('targetBanner').style.display = 'none';
  document.getElementById('planListHeading').textContent = 'Recommended order';
  document.getElementById('planFilter').style.display = '';
  const rec = document.getElementById('recCard');
  rec.style.display = '';
  const nextStep = plan.find(s => !stepIsDone(s));
  if (!nextStep) {
    if (plan.length === 0) {
      rec.innerHTML = '<h3>No actionable plan</h3><div class="info">No SCCs to process.</div>';
    } else {
      rec.innerHTML = '<h3>🎉 All ' + plan.length + ' steps applied</h3><div class="info">Every folder is migrated.</div>';
    }
  } else {
    const isStart = !plan.some(s => stepIsDone(s));
    const isCycle = nextStep.is_cycle;
    const headline = isCycle
      ? '⚠ Migrate a tightly-coupled bundle (' + nextStep.size + ' folders)'
      : nextStep.folders[0];
    rec.innerHTML =
      '<h3>' + (isStart ? '🚀 Start here' : '➡️ Next step') + ' — step ' + nextStep.step + ' of ' + plan.length + '</h3>' +
      '<div class="card-headline">' + escapeHtml(headline) + '</div>' +
      (isCycle
        ? (
            '<div class="info" style="background:#fdf2e9;padding:8px;border-radius:4px;border-left:3px solid #e67e22;margin: 6px 0;">' +
              '<b>⚠ Big cycle (' + nextStep.size + ' folders, ~' + (nextStep.fas_estimate || 0) + ' edges form the cycle).</b> ' +
              'Migrating the whole bundle in one PR is not safe. ' +
              'Instead, <b>extract one folder at a time</b> by refactoring its outgoing references to other cycle members. ' +
              'The tool ranked the easiest-to-extract folders below.' +
            '</div>' +
            (nextStep.extractions && nextStep.extractions.length > 0
              ? '<div class="info"><b>🔧 Lightest folder extractions (smallest PR first):</b><ol style="margin: 4px 0; padding-left: 18px;">' +
                nextStep.extractions.slice(0, 5).map(ext => {
                  const sample = ext.edges.slice(0, 2).map(e => escapeHtml(e.dst.split('/').slice(-2).join('/')) + ' (' + e.refs + ')').join(', ');
                  return '<li><code>' + escapeHtml(ext.folder) + '</code> ' +
                    '<span style="color:#7f8c8d;">— ' + ext.outgoing_refs + ' ref' + (ext.outgoing_refs === 1 ? '' : 's') + ' across ' + ext.outgoing_count + ' edge' + (ext.outgoing_count === 1 ? '' : 's') + ' to refactor</span>' +
                    (sample ? '<div style="font-size: 11px; color: #555; padding-left: 8px;">touches: ' + sample + (ext.edges.length > 2 ? ', +' + (ext.edges.length - 2) + ' more' : '') + '</div>' : '') +
                    '</li>';
                }).join('') +
                (nextStep.extractions.length > 5 ? '<li>…and ' + (nextStep.extractions.length - 5) + ' more in the step card below</li>' : '') +
                '</ol></div>'
              : '<div class="info">No extraction candidates computed.</div>')
          )
        : '') +
      '<div class="info">' +
        (nextStep.unlocks.length === 0
          ? 'Migrating this <b>unlocks nothing new</b> — it\'s a sink in the DAG (other folders depend on it but it depends on nothing — or only on already-migrated things).'
          : 'Migrating this <b>unlocks ' + nextStep.unlocks.length + ' bundle(s)</b>:<ul style="margin: 4px 0; padding-left: 18px;">' +
            nextStep.unlocks.slice(0, 8).map(u => '<li>' + (u.size === 1 ? escapeHtml(u.folders[0]) : '⚠ cycle of ' + u.size + ' folders (' + escapeHtml(u.folders.slice(0,3).join(', ')) + (u.size>3?'…':'') + ')') + '</li>').join('') +
            (nextStep.unlocks.length > 8 ? '<li>…and ' + (nextStep.unlocks.length - 8) + ' more</li>' : '') +
            '</ul>') +
      '</div>' +
      '<div class="card-actions">' +
        '<button id="recMigrate">✓ ' + (isCycle ? 'Mark bundle complete (' + nextStep.size + ')' : 'Mark migrated') + '</button>' +
        '<button class="ghost" id="recReveal">Show in graph</button>' +
      '</div>';
    document.getElementById('recMigrate').onclick = () => applyStep(nextStep);
    document.getElementById('recReveal').onclick = () => {
      // Focus the node in the graph; leave the current tab alone so the
      // user keeps their Plan-mode context.
      focusToFolder(nextStep.folders[0]);
    };
  }

  // Plan list
  const filter = (document.getElementById('planFilter').value || '').toLowerCase();
  const wrap = document.getElementById('planList');
  wrap.innerHTML = '';
  plan.forEach(s => {
    const hay = s.folders.join(' ') + ' ' + s.unlocks.map(u => u.folders.join(' ')).join(' ');
    if (filter && !hay.toLowerCase().includes(filter)) return;
    const isDone = stepIsDone(s);
    const isNext = !isDone && (!nextStep || s.step === nextStep.step);
    const div = document.createElement('div');
    div.className = 'step' + (isDone ? ' done' : '') + (isNext ? ' next' : '');
    const unlocksTxt = stepUnlocksText(s);
    const folderListHtml = s.size === 1
      ? '<b>' + escapeHtml(s.folders[0]) + '</b>'
      : '<b>⚠ Cycle bundle (' + s.size + ' folders)</b>';
    let inner =
      '<span class="stepNum">#' + s.step + '</span>' +
      (isDone ? '✓ ' : '') + folderListHtml +
      (unlocksTxt
        ? '<div class="unlocks">→ unlocks: <span class="u">' + escapeHtml(unlocksTxt) + '</span></div>'
        : '<div class="unlocks">no downstream unlocks</div>');
    if (s.size > 1) {
      inner += '<details style="margin-top: 4px;"><summary style="font-size:11px;">view ' + s.size + ' members</summary>'
            + '<ul style="font-size:11px;max-height:200px;overflow:auto;">'
            + s.folders.map(f => '<li data-folder="' + escapeHtml(f) + '">' + escapeHtml(f) + '</li>').join('')
            + '</ul></details>';
      if (s.extractions && s.extractions.length > 0) {
        inner += '<details style="margin-top: 4px;" open><summary style="font-size:11px;color:#d35400;font-weight:600;">🔧 Lightest folder extractions (' + s.extractions.length + ' candidates, smallest PR first)</summary>'
          + '<div style="font-size:11px;max-height:340px;overflow:auto;margin-top: 4px;">'
          + s.extractions.map((ext, i) => {
              const edgesHtml = ext.edges.slice(0, 8).map(e => {
                const typesTxt = e.types.length === 0 ? '' : ' — ' + escapeHtml(e.types.join(', ')) + (e.types.length === 8 ? '…' : '');
                return '<div style="padding: 2px 0 2px 12px; color: #555;">→ <code data-folder="' + escapeHtml(e.dst) + '">' + escapeHtml(e.dst) + '</code> <span style="color:#7f8c8d;">(' + e.refs + ' ref' + (e.refs === 1 ? '' : 's') + ')</span>' + typesTxt + '</div>';
              }).join('');
              return '<div style="border-left:3px solid #e67e22;padding:6px 8px;margin:4px 0;background:#fef5e7;">'
                + '<div><b>#' + (i+1) + '</b> Extract <code data-folder="' + escapeHtml(ext.folder) + '">' + escapeHtml(ext.folder) + '</code></div>'
                + '<div style="color:#7f8c8d;font-size:10px;padding-left:8px;">'
                + 'refactor ' + ext.outgoing_refs + ' ref' + (ext.outgoing_refs === 1 ? '' : 's') + ' across ' + ext.outgoing_count + ' outgoing edge' + (ext.outgoing_count === 1 ? '' : 's') + ' • '
                + ext.incoming_count + ' folder' + (ext.incoming_count === 1 ? '' : 's') + ' depend on this'
                + '</div>'
                + (edgesHtml ? '<div style="margin-top: 3px; font-size: 11px;">' + edgesHtml + (ext.edges.length > 8 ? '<div style="color:#7f8c8d;padding-left:12px;">…+' + (ext.edges.length - 8) + ' more edge(s)</div>' : '') + '</div>' : '')
                + '</div>';
            }).join('')
          + '</div></details>';
      }
      if (s.breakers && s.breakers.length > 0) {
        inner += '<details style="margin-top: 4px;"><summary style="font-size:11px;color:#7f8c8d;">🌐 Global cycle-break sequence (informational — ~' + (s.fas_estimate || 0) + ' edges form the cycle)</summary>'
          + '<div style="font-size:11px;max-height:240px;overflow:auto;margin-top: 4px;">'
          + s.breakers.slice(0, 30).map((b, i) => {
              const typesTxt = b.types.length === 0 ? '' : b.types.slice(0, 5).join(', ') + (b.types.length > 5 ? ', +' + (b.types.length - 5) + ' more' : '');
              const after = b.remaining_cycles === 0
                ? '<span style="color:#1e874b;">✓ cycle resolved</span>'
                : 'still ' + b.remaining_cycles + ' cycle(s), biggest=' + b.biggest_cycle;
              return '<div style="border-left:2px solid #bdc3c7;padding:3px 6px;margin:2px 0;">'
                + '<b>#' + (i+1) + '</b> <code data-folder="' + escapeHtml(b.src) + '">' + escapeHtml(b.src) + '</code> → <code data-folder="' + escapeHtml(b.dst) + '">' + escapeHtml(b.dst) + '</code> <span style="color:#7f8c8d;">(' + b.refs + ' ref' + (b.refs === 1 ? '' : 's') + (typesTxt ? ': ' + escapeHtml(typesTxt) : '') + ')</span> '
                + '<span style="color:#7f8c8d;font-size:10px;">after: ' + after + '</span>'
                + '</div>';
            }).join('')
          + (s.breakers.length > 30 ? '<div style="color:#7f8c8d;">…' + (s.fas_estimate - 30) + ' more edges</div>' : '')
          + '</div></details>';
      }
    }
    // Action bar: explicit "complete" button so tapping the row never mutates
    // state — the row itself just previews the step in the graph.
    const actionHtml = isDone
      ? '<button class="ghost" data-act="uncomplete">↩ Unmark</button>'
      : '<button data-act="complete">✓ Mark complete</button>'
        + (s.step > 1 ? ' <button class="ghost" data-act="completeUpTo">⏩ Mark all up to here</button>' : '');
    inner += '<div class="step-actions">' + actionHtml + '</div>';
    div.innerHTML = inner;
    // Member / breaker-endpoint clicks → inspect.
    div.querySelectorAll('[data-folder]').forEach(el => {
      el.onclick = (ev) => {
        ev.stopPropagation();
        ev.preventDefault();
        focusToFolder(el.dataset.folder);
      };
      el.style.cursor = 'pointer';
    });
    // Explicit action buttons handle state mutation; row click only previews.
    div.querySelectorAll('.step-actions button').forEach(b => {
      b.onclick = (ev) => {
        ev.stopPropagation();
        const act = b.dataset.act;
        if (act === 'complete') applyStep(s);
        else if (act === 'completeUpTo') applyUpTo(s);
        else if (act === 'uncomplete') {
          s.folders.forEach(f => { if (!INITIAL_MIGRATED.has(f)) migrated.delete(f); });
          renderPlan();
          render();
        }
      };
    });
    // Row click → preview step in the graph. Keep the current tab so the user
    // stays in Plan view; tab switching is opt-in via the Hierarchy tab itself.
    div.addEventListener('click', (ev) => {
      if (ev.target.closest('details') && !ev.target.closest('summary')) return;
      if (ev.target.closest('.step-actions')) return;
      if (ev.target.closest('[data-folder]')) return;
      focusToFolder(s.folders[0]);
    });
    wrap.appendChild(div);
  });

  // Stuck (shouldn't happen with full DAG plan)
  const stuckFlat = [].concat(...stuck);
  document.getElementById('stuckCount').textContent = stuckFlat.length;
  document.getElementById('stuckInfo').innerHTML = stuckFlat.length > 0
    ? '  <span class="stuck">' + stuckFlat.length + ' folder(s) unreachable.</span>' : '';
  const sl = document.getElementById('stuckList');
  sl.innerHTML = '';
  stuckFlat.slice(0, 200).forEach(s => {
    const li = document.createElement('li');
    li.className = 'stuck';
    li.textContent = s;
    li.onclick = () => { focusToFolder(s); };
    sl.appendChild(li);
  });

  updateMigrationCounts();
}

function updateMigrationCounts() {
  const userCount = migrated.size - INITIAL_MIGRATED.size;
  document.getElementById('migCount2').textContent = migrated.size;
  document.getElementById('userMigCount').textContent = userCount;
  document.getElementById('baselineCount').textContent = INITIAL_MIGRATED.size;
  const totalToMig = totalSource - INITIAL_MIGRATED.size;
  document.getElementById('totalToMigrate').textContent = totalToMig;
  const prefixes = DATA.migrated_prefixes || [];
  document.getElementById('baselinePrefixes').textContent =
    prefixes.length ? '(' + prefixes.join(', ') + ')' : '';
}

// Plan focused on a single target: the ordered prerequisite path to extract it.
function renderTargetPlan() {
  const banner = document.getElementById('targetBanner');
  const rec = document.getElementById('recCard');
  rec.style.display = 'none';
  document.getElementById('planListHeading').textContent = 'Path to extract this folder';
  document.getElementById('planFilter').style.display = 'none';

  const { closure, seedSet } = migrationClosure(migrationTarget);
  // Prerequisites = closure minus the target's own source folders.
  const prereq = new Set([...closure].filter(f => !seedSet.has(f)));
  // Order everything by the global plan; cycles shown as one entry.
  const orderedSteps = plan
    .map((s, i) => ({ s, i }))
    .filter(({ s }) => s.folders.some(f => closure.has(f)));

  banner.className = 'card target-banner';
  banner.style.display = 'block';
  const name = tree[migrationTarget] ? tree[migrationTarget].name : migrationTarget;
  banner.innerHTML =
    '<h3>🎯 Plan to move: ' + escapeHtml(name) + '</h3>' +
    '<div class="info">' + escapeHtml(migrationTarget) + '</div>' +
    '<div class="info" style="margin-top:6px;">' +
      (prereq.size === 0
        ? 'Nothing blocks it — its dependencies are already migrated. Extract it directly.'
        : '<b>' + prereq.size + '</b> folder(s) must be migrated first, then the target itself.') +
    '</div>' +
    '<div style="margin-top:8px;display:flex;gap:6px;">' +
      '<button id="targetApply">Migrate this whole path</button>' +
      '<button class="ghost" id="targetClear">← Back to full plan</button>' +
    '</div>';
  document.getElementById('targetClear').onclick = () => clearTarget();
  document.getElementById('targetApply').onclick = () => {
    closure.forEach(f => migrated.add(f));
    lastNewlyRevealed = [];
    renderPlan(); render();
  };

  // Render ordered path list.
  const wrap = document.getElementById('planList');
  wrap.innerHTML = '';
  if (orderedSteps.length === 0) {
    wrap.innerHTML = '<div class="info">Already fully migrated.</div>';
  } else {
    let ord = 1;
    orderedSteps.forEach(({ s }) => {
      const isTargetStep = s.folders.some(f => seedSet.has(f));
      const done = stepIsDone(s);
      const label = s.size === 1 ? s.folders[0] : ('⚠ cycle of ' + s.size + ' folders');
      const div = document.createElement('div');
      div.className = 'path-step' + (isTargetStep ? ' is-target' : '') + (done ? ' done' : '');
      div.innerHTML = '<span class="ord">' + (done ? '✓' : ord) + '</span>' +
        '<span>' + (isTargetStep ? '🎯 ' : '') + escapeHtml(label) +
        (isTargetStep ? ' <span style="color:var(--text-faint);font-size:11px;">(your target)</span>' : '') + '</span>';
      div.onclick = () => { focusToFolder(s.folders[0]); };
      wrap.appendChild(div);
      if (!done) ord++;
    });
  }
  document.getElementById('stuckCount').textContent = '0';
  document.getElementById('stuckInfo').innerHTML = '';
  document.getElementById('stuckList').innerHTML = '';
  updateMigrationCounts();
}

function applyStep(step) {
  step.folders.forEach(f => migrated.add(f));
  lastNewlyRevealed = [].concat(...step.unlocks.map(u => u.folders));
  const banner = document.getElementById('newlyBanner');
  const label = step.size === 1 ? step.folders[0] : ('cycle of ' + step.size + ' folders');
  banner.innerHTML = '<b>✓ Migrated step ' + step.step + ' (' + escapeHtml(label) + ')</b><br/>' +
    (step.unlocks.length === 0
      ? 'No new folders unlocked.'
      : '<b>Unlocked ' + step.unlocks.length + ' bundle(s):</b> ' +
        step.unlocks.map(u => u.size === 1 ? escapeHtml(u.folders[0]) : ('⚠cycle×' + u.size)).join(', '));
  banner.style.display = 'block';
  renderPlan();
  render();
}
function applyUpTo(step) {
  migrated = new Set();
  for (const s of plan) {
    s.folders.forEach(f => migrated.add(f));
    if (s.step === step.step) break;
  }
  lastNewlyRevealed = [].concat(...step.unlocks.map(u => u.folders));
  const banner = document.getElementById('newlyBanner');
  banner.innerHTML = '<b>Applied plan through step ' + step.step + '</b>';
  banner.style.display = 'block';
  renderPlan();
  render();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(x => x.classList.toggle('active', x.id === 'panel-' + name));
  const sb = document.getElementById('navSettings');
  if (sb) sb.classList.toggle('active', name === 'settings');
}

function focusToFolder(folder) {
  // Climb to nearest ancestor that has children, focus there, then highlight.
  let cur = folder;
  while (cur && (!tree[cur] || tree[cur].children.length === 0)) {
    if (!tree[cur]) break;
    cur = tree[cur].parent;
  }
  go((cur === null || cur === undefined) ? '' : cur);
  if (network && tree[folder]) {
    try { network.focus(folder, { scale: 1.4, animation: true }); network.selectNodes([folder]); } catch (e) {}
  }
}

function updateMigratedSidebar() {
  // Show only the user-selected display nodes (we approximate: any folder
  // whose direct .swift container is migrated AND whose parent is not all-migrated).
  const items = [];
  Object.keys(tree).forEach(id => {
    if (!id) return;
    if (!isAllMigrated(id)) return;
    const parent = tree[id].parent;
    if (parent !== null && parent !== undefined && isAllMigrated(parent)) return; // dedup: parent covers it
    items.push(id);
  });
  items.sort();
  document.getElementById('migCount2').textContent = items.length;
  const ul = document.getElementById('migList');
  ul.innerHTML = '';
  items.forEach(id => {
    const li = document.createElement('li');
    li.textContent = '✓ ' + id;
    ul.appendChild(li);
  });
}

function render() {
  const focus = tree[focusId];
  const kids = focus.children;
  // Terminal folder (no child folders) -> show type-level view.
  if (kids.length === 0 && focusId !== '') {
    lastRenderedFocusId = undefined; folderNodesDS = null; folderEdgesDS = null;
    renderTypeView(focus);
    return;
  }
  // displaySet = children of focus (each represents its whole subtree).
  const displaySet = new Set(kids);

  // Aggregate sibling-to-sibling edges (for visual graph).
  // Track outDeg/inDeg using ALL first-party edges leaving each child's subtree,
  // not only sibling-to-sibling — otherwise edges leaving the focused subtree
  // are invisible and folders look like leaves when they actually have deep deps.
  const aggEdges = new Map();
  const outDeg = new Map();
  const inDeg = new Map();
  const outExternal = new Map();   // child -> edges leaving displaySet's parent subtree
  const inExternal = new Map();    // child -> edges coming from outside displaySet's parent
  for (const e of edges) {
    if (outOfScope(e.src) || outOfScope(e.dst)) continue;
    const a = ownerInDisplay(e.src, displaySet);
    const b = ownerInDisplay(e.dst, displaySet);
    if (!a && !b) continue;
    if (a && b && a !== b) {
      const k = a + '|' + b;
      aggEdges.set(k, (aggEdges.get(k) || 0) + e.w);
      outDeg.set(a, (outDeg.get(a) || 0) + 1);
      inDeg.set(b, (inDeg.get(b) || 0) + 1);
    } else if (a && !b) {
      // outgoing to outside the current view — still a dep, counted in outDeg.
      outDeg.set(a, (outDeg.get(a) || 0) + 1);
      outExternal.set(a, (outExternal.get(a) || 0) + e.w);
    } else if (!a && b) {
      inDeg.set(b, (inDeg.get(b) || 0) + 1);
      inExternal.set(b, (inExternal.get(b) || 0) + e.w);
    }
    // self-loop (a === b): ignored.
  }

  const newlySet = new Set(lastNewlyRevealed);
  // Smart labels: with many siblings, always-on labels overlap into mush.
  // Label only the hubs (top by total degree) + actionable nodes (leaf/newly);
  // the rest stay unlabelled and reveal on hover (see handlers below).
  const degOf = id => (outDeg.get(id) || 0) + (inDeg.get(id) || 0);
  const sortedDeg = kids.map(degOf).sort((a, b) => b - a);
  const HUB_COUNT = 12;
  const labelCut = kids.length > HUB_COUNT + 4 ? (sortedDeg[HUB_COUNT - 1] || 0) : -1;
  const exploreMode = currentMode === 'explore';
  const nodes = kids.map(id => {
    const n = tree[id];
    const isLeaf = !(outDeg.get(id) > 0);
    const hasKids = n.children.length > 0;
    const isMigrated = isAllMigrated(id);
    const isExcluded = isAllExcluded(id);
    let group;
    if (exploreMode) {
      // Explore: every folder gets the same color — node TYPE drives color,
      // not migration/leaf state. Kind colors live on the file/type nodes.
      group = 'folder';
    } else {
      group = 'mid';
      if (isExcluded) group = 'excluded';
      else if (isMigrated) group = 'migrated';
      else if (newlySet.has(id)) group = 'newly';
      else if (isLeaf && !hasKids) group = 'leaf';
    }
    const oe = outExternal.get(id) || 0;
    const ie = inExternal.get(id) || 0;
    const fullLabel = n.name + (hasKids ? '  ▸' : '')
      + (exploreMode ? '' : (isExcluded ? ' 🚫' : (isMigrated ? ' ✓' : '')));
    const actionable = !exploreMode && (group === 'leaf' || group === 'newly');
    const keepLabel = labelCut < 0 || actionable || degOf(id) >= labelCut;
    const migrationTitle = (isExcluded ? '\n🚫 WON\'T BE MODULARIZED (excluded from the plan)'
      : (isMigrated ? '\nMIGRATED (treated as external)'
      : (isLeaf ? (hasKids ? '\nLEAF AT THIS ZOOM — no first-party outgoing deps left, but has sub-folders to migrate first.'
                          : '\nLEAF — no first-party outgoing deps. Click to open it; migrate from the type-view button.') : '')));
    return {
      id,
      label: keepLabel ? fullLabel : '',
      _full: fullLabel,
      _keep: keepLabel,
      title: id + '\n'
        + 'types: ' + n.types + '\n'
        + 'outgoing deps: ' + (outDeg.get(id) || 0) + '  (' + (oe > 0 ? oe + ' to elsewhere in project, ' : '') + ((outDeg.get(id) || 0) - (oe>0?1:0)) + ' to siblings)\n'
        + 'incoming refs: ' + (inDeg.get(id) || 0)
        + (exploreMode ? '' : migrationTitle)
        + '\n(click to drill in)',
      group,
      value: Math.max(1, n.types),
      font: { size: 14 },
    };
  });

  const edgeData = [];
  for (const [k, w] of aggEdges) {
    const [a, b] = k.split('|');
    edgeData.push({ from: a, to: b, value: w, title: a + ' → ' + b + ' (' + w + ' refs)' });
  }

  // Mixed folder: this folder holds its own .swift files alongside subfolders.
  // Surface those files as nodes too, otherwise the graph hides them entirely
  // and the user thinks the folder is just a container. Click any of them to
  // drop into the type-view for this folder.
  const ownFilesHere = filesByFolder[focusId] || [];
  if (ownFilesHere.length > 0) {
    const selfAnchorId = 'self::' + focusId;
    // Park the file cluster off to the left at fixed coordinates with physics
    // disabled — otherwise N file nodes inject N more bodies into barnesHut
    // and push the folder graph around the canvas every time the user drills
    // into a mixed folder.
    const clusterX = -520;
    const clusterY0 = -((ownFilesHere.length - 1) * 28);
    nodes.push({
      id: selfAnchorId,
      label: '📂 ' + (focus.name || focus.id) + ' (files)',
      title: focus.id + '\n' + ownFilesHere.length + ' file(s) directly in this folder\n(click to open type-view)',
      shape: 'star',
      color: { background: '#eab308', border: '#ca8a04' },
      font: { size: 13, color: '#1a1300' },
      value: 4,
      x: clusterX, y: clusterY0 - 70, fixed: true, physics: false,
    });
    ownFilesHere.forEach((f, i) => {
      const fid = 'file::' + focusId + '::' + f.name;
      const declCount = (f.decls || []).length;
      nodes.push({
        id: fid,
        label: '📄 ' + f.name,
        title: f.name + '\nfolder: ' + focus.id
          + (declCount ? '\ndeclares: ' + f.decls.join(', ') : '\n(no declared types)')
          + '\n(click to open type-view)',
        shape: 'box',
        color: { background: '#fde68a', border: '#ca8a04' },
        font: { size: 12, color: '#1a1300' },
        value: 2,
        x: clusterX, y: clusterY0 + i * 56, fixed: true, physics: false,
      });
      edgeData.push({ from: selfAnchorId, to: fid, arrows: '', dashes: true, color: { color: '#bdc3c7', opacity: 0.5 }, physics: false });
    });
  }

  // Synthetic "outside view" node aggregating edges that leave the focused
  // subtree, so users see deep dependencies even when zoomed-in.
  const hasExternal = [...outExternal.values()].some(v => v > 0) || [...inExternal.values()].some(v => v > 0);
  if (hasExternal) {
    nodes.push({
      id: '__ext__',
      label: '🌐 elsewhere in project',
      title: 'Edges to/from folders not under "' + (focus.name || 'root') + '".\nClick to go up a level.',
      group: 'external',
      shape: 'diamond',
      value: 4,
      font: { size: 12 },
    });
    outExternal.forEach((w, id) => {
      edgeData.push({ from: id, to: '__ext__', value: w, dashes: true, color: { color: '#c0392b', opacity: 0.45 }, title: id + ' → outside (' + w + ' refs)' });
    });
    inExternal.forEach((w, id) => {
      edgeData.push({ from: '__ext__', to: id, value: w, dashes: true, color: { color: '#2980b9', opacity: 0.45 }, title: 'outside → ' + id + ' (' + w + ' refs)' });
    });
  }

  currentOutDeg = outDeg;

  if (typeof vis === 'undefined') {
    document.getElementById('net').innerHTML =
      '<div style="padding: 40px; color: #d73a49; font-family: monospace;">' +
      '<h2>vis-network failed to load</h2>' +
      '<p>The graph library is loaded from a CDN. If you are offline or the CDN is blocked, ' +
      'the graph cannot render. The plan/explore lists in the sidebar still work.</p>' +
      '<p>To run fully offline, download vis-network and inline the script tag, or open this file with internet access.</p>' +
      '</div>';
    return;
  }
  // In-place update path: same focus, network already mounted — just refresh
  // node groups/labels/titles and swap edges. Positions are preserved.
  if (network && lastRenderedFocusId === focusId && folderNodesDS && folderEdgesDS) {
    const existingIds = new Set(folderNodesDS.getIds());
    const newIds = new Set(nodes.map(n => n.id));
    folderNodesDS.update(nodes);
    [...existingIds].filter(id => !newIds.has(id)).forEach(id => folderNodesDS.remove(id));
    folderEdgesDS.clear();
    folderEdgesDS.add(edgeData);
    currentOutDeg = outDeg;
    renderCrumbs();
    renderKids(kids, outDeg);
    return;
  }
  if (network) network.destroy();
  const nodesDS = new vis.DataSet(nodes);
  const edgesDS = new vis.DataSet(edgeData);
  folderNodesDS = nodesDS;
  folderEdgesDS = edgesDS;
  lastRenderedFocusId = focusId;
  // Spread scales with node count so 40 siblings don't pack into a hairball.
  const spread = 1 + Math.min(1.5, Math.max(0, (kids.length - 12) / 24));
  network = new vis.Network(document.getElementById('net'),
    { nodes: nodesDS, edges: edgesDS }, {
    nodes: { shape: 'dot', scaling: { min: 10, max: 52 }, borderWidth: 2,
      shadow: { enabled: true, color: 'rgba(20,28,55,0.12)', size: 10, x: 0, y: 3 },
      font: { color: themeText(), face: 'Inter, sans-serif', size: 14 } },
    edges: { arrows: { to: { scaleFactor: 0.6 } }, smooth: { type: 'continuous' }, color: { color: '#c3c8d6', highlight: '#6366f1', opacity: 0.7 } },
    physics: { stabilization: { iterations: 320 },
      barnesHut: { gravitationalConstant: -9000 * spread, springLength: 170 * spread, springConstant: 0.035, centralGravity: 0.12, damping: 0.45, avoidOverlap: 0.55 } },
    interaction: { hover: true, tooltipDelay: 100 },
    groups: folderGroups(),
  });
  network.once('stabilizationIterationsDone', () => {
    try { network.fit({ animation: false }); } catch (e) {}
  });
  // Hover-reveal: temporarily show labels of the hovered node + its neighbours
  // that were hidden by the smart-label filter, then restore on blur.
  let hoverShown = [];
  network.on('hoverNode', p => {
    const ids = network.getConnectedNodes(p.node).concat([p.node]);
    const upd = [];
    ids.forEach(nid => {
      const n = nodesDS.get(nid);
      if (n && n._full && !n.label) { upd.push({ id: nid, label: n._full }); hoverShown.push(nid); }
    });
    if (upd.length) nodesDS.update(upd);
  });
  network.on('blurNode', () => {
    if (!hoverShown.length) return;
    nodesDS.update(hoverShown.map(id => ({ id, label: '' })));
    hoverShown = [];
    schedulePopoverHide();
  });
  // Custom popover: stats + action buttons (mark/unmark migrated/excluded).
  // Appears on hover, sticks while pointer is over it so buttons are clickable.
  const pop = document.getElementById('nodePopover');
  let popHideTimer = null;
  let popNodeId = null;
  function schedulePopoverHide() {
    clearTimeout(popHideTimer);
    popHideTimer = setTimeout(() => { pop.style.display = 'none'; popNodeId = null; }, 220);
  }
  function cancelPopoverHide() { clearTimeout(popHideTimer); }
  pop.onmouseenter = cancelPopoverHide;
  pop.onmouseleave = schedulePopoverHide;
  function positionPopover(nodeId) {
    const canvasPos = network.getPositions([nodeId])[nodeId];
    if (!canvasPos) return;
    const dom = network.canvasToDOM(canvasPos);
    const wrap = document.getElementById('netWrap').getBoundingClientRect();
    const net = document.getElementById('net').getBoundingClientRect();
    pop.style.left = (net.left - wrap.left + dom.x) + 'px';
    pop.style.top = (net.top - wrap.top + dom.y) + 'px';
  }
  function renderPopover(nodeId) {
    if (!tree[nodeId]) { pop.style.display = 'none'; return; }
    const n = tree[nodeId];
    const isLeaf = !(outDeg.get(nodeId) > 0);
    const hasKids = n.children.length > 0;
    const isMigrated = isAllMigrated(nodeId);
    const isExcluded = isAllExcluded(nodeId);
    const baseline = isMigrated && isInitialMigrated(nodeId);
    const exploreMode = currentMode === 'explore';
    let swatch = '#3b82f6';
    if (exploreMode) swatch = '#6366f1';
    else if (isExcluded) swatch = '#ef4444';
    else if (isMigrated) swatch = '#cbd5e1';
    else if (newlySet.has(nodeId)) swatch = '#f59e0b';
    else if (isLeaf && !hasKids) swatch = '#22c55e';
    const stateBadge = (!exploreMode && isExcluded) ? '<div class="np-state excluded">🚫 Won\'t modularize</div>'
      : (!exploreMode && baseline) ? '<div class="np-state migrated">⚪️ SPM baseline</div>'
      : (!exploreMode && isMigrated) ? '<div class="np-state migrated">✓ Migrated</div>'
      : '';
    let actions = '';
    if (!exploreMode && !baseline) {
      const migLabel = isMigrated ? '↩ Unmark migrated' : '✓ Mark migrated';
      const exclLabel = isExcluded ? '↩ Will modularize' : '🚫 Won\'t modularize';
      const migCls = isExcluded ? 'ghost' : (isMigrated ? 'ghost' : '');
      const exclCls = isExcluded ? 'ghost' : 'danger';
      const canPlan = !isExcluded && !isMigrated;
      actions = '<div class="np-actions">'
        + (canPlan ? '<button class="ghost" data-act="plan">🎯 Plan to move</button>' : '')
        + (isExcluded ? '' : '<button class="' + migCls + '" data-act="mig">' + migLabel + '</button>')
        + '<button class="' + exclCls + '" data-act="excl">' + exclLabel + '</button>'
        + '</div>';
    }
    pop.innerHTML =
      '<div class="np-header"><span class="np-swatch" style="background:' + swatch + ';"></span>'
        + '<span class="np-name">' + escapeHtml(n.name || nodeId) + '</span></div>'
      + stateBadge
      + '<div class="np-stats">'
      +   '<div><b>' + n.types + '</b> declared type(s)</div>'
      +   '<div><b>' + (outDeg.get(nodeId) || 0) + '</b> outgoing dep(s)</div>'
      +   '<div><b>' + (inDeg.get(nodeId) || 0) + '</b> incoming ref(s)</div>'
      + '</div>'
      + actions
      + '<div class="np-hint">Click node to drill in</div>';
    pop.querySelectorAll('.np-actions button').forEach(b => {
      b.onclick = (ev) => {
        ev.stopPropagation();
        const act = b.dataset.act;
        if (act === 'mig') (isMigrated ? unmigrate(nodeId) : migrate(nodeId));
        else if (act === 'excl') toggleExclude(nodeId);
        else if (act === 'plan') setTarget(nodeId);
        pop.style.display = 'none'; popNodeId = null;
      };
    });
  }
  network.on('hoverNode', p => {
    const id = p.node;
    if (id === '__ext__' || id.startsWith('self::') || id.startsWith('file::')) {
      pop.style.display = 'none'; popNodeId = null; return;
    }
    cancelPopoverHide();
    popNodeId = id;
    renderPopover(id);
    positionPopover(id);
    pop.style.display = 'block';
  });
  // Reposition popover while network animates (drag, zoom, stabilization).
  network.on('afterDrawing', () => { if (popNodeId && pop.style.display === 'block') positionPopover(popNodeId); });
  network.on('doubleClick', params => {
    if (!params.nodes.length) return;
    const id = params.nodes[0];
    if (id === '__ext__') { goUp(); return; }
    if (id.startsWith('self::') || id.startsWith('file::')) { renderTypeView(tree[focusId]); return; }
    if (tree[id] && tree[id].children.length > 0) { go(id); }
  });
  network.on('click', params => {
    pop.style.display = 'none'; popNodeId = null;
    if (!params.nodes.length) return;
    const id = params.nodes[0];
    if (id === '__ext__') { goUp(); return; }
    // self/file nodes belong to the focused folder's own files — open the
    // type-view instead of treating them as navigation targets.
    if (id.startsWith('self::') || id.startsWith('file::')) { renderTypeView(tree[focusId]); return; }
    // Clicking NEVER migrates — it only navigates. Drilling into a terminal
    // leaf opens its type-view, which carries an explicit "Mark migrated"
    // button. Lets you explore the graph without mutating migration state.
    go(id);
  });

  renderCrumbs();
  renderKids(kids, outDeg);
}

function renderTypeView(focus) {
  // Type-view owns its own network — invalidate the folder-graph cache so a
  // later state toggle re-creates instead of trying to update the wrong DS.
  lastRenderedFocusId = undefined; folderNodesDS = null; folderEdgesDS = null;
  // Build nodes:
  //  - a central 'self' node (📂 folder name) — always present.
  //  - yellow box per declared type in this folder.
  //  - gray ellipse per *other folder* this folder references or is referenced by.
  // Outbound edges (red, labeled with the external type name):
  //    declared type T  ──U──▶  ext(D)        — T's file references U declared in D
  //    self             ──U──▶  ext(D)        — file with no declared types still references U in D
  // Inbound edges (blue, labeled with the local type name):
  //    ext(srcFolder)   ──T──▶  T             — a file in srcFolder references local type T
  // Intra-folder edges (gray): file-level type→type references between local types.
  const folderFiles = filesByFolder[focus.id] || [];
  const localDecls = new Set();
  const declToFile = {};
  folderFiles.forEach(f => f.decls.forEach(t => { localDecls.add(t); declToFile[t] = f.name; }));

  const selfId = 'self::' + focus.id;
  const nodes = [];
  const edgeList = [];
  const externals = new Set();
  const outboundByExt = new Map();   // ext -> Set(typeName) for sidebar summary
  const inboundByExt = new Map();    // ext -> Set(typeName)

  // Every reference is matched to the EXACT type the compiler bound, by its
  // USR-resolved owner folder (ref_owners = [[name, ownerFolder], ...]). This
  // is what kills the name-collision hairball: a file referencing some other
  // module's "Button" no longer draws an edge to THIS folder's "Button", and a
  // private type (whose USR has no cross-file references) shows no false uses.
  const HAS_REF_OWNERS = files.some(f => Array.isArray(f.ref_owners) && f.ref_owners.length > 0);
  function resolvedRefs(f) {
    if (HAS_REF_OWNERS && Array.isArray(f.ref_owners)) return f.ref_owners;
    // Legacy (regex scan, no USR): expand each name to all its declared owners.
    const out = [];
    (f.refs || []).forEach(u => (typeOwners[u] || []).forEach(d => out.push([u, d])));
    return out;
  }

  // Outbound (files in this folder referencing external types).
  folderFiles.forEach(f => {
    const hasDecls = f.decls.length > 0;
    resolvedRefs(f).forEach(([u, owner]) => {
      if (!owner || owner === focus.id) return;   // unresolved or intra-folder
      externals.add(owner);
      if (!outboundByExt.has(owner)) outboundByExt.set(owner, new Set());
      outboundByExt.get(owner).add(u);
      const sources = hasDecls ? f.decls.map(t => 't::' + t) : [selfId];
      sources.forEach(src => {
        edgeList.push({ from: src, to: 'd::' + owner, arrows: 'to', label: u, font: { size: 10, align: 'middle' }, color: { color: '#c0392b', opacity: 0.55 } });
      });
    });
  });

  // Intra-folder type-to-type refs (only refs bound to THIS folder's types).
  folderFiles.forEach(f => {
    resolvedRefs(f).forEach(([u, owner]) => {
      if (owner !== focus.id || !localDecls.has(u)) return;
      f.decls.forEach(t => {
        if (t === u) return;
        edgeList.push({ from: 't::' + t, to: 't::' + u, arrows: 'to', color: { color: '#7f8c8d', opacity: 0.5 } });
      });
    });
  });

  // Inbound (files in other folders that reference local types) — only when the
  // reference resolves to a type OWNED by this folder.
  for (const f of files) {
    if (f.folder === focus.id) continue;
    resolvedRefs(f).forEach(([u, owner]) => {
      if (owner !== focus.id || !localDecls.has(u)) return;
      externals.add(f.folder);
      if (!inboundByExt.has(f.folder)) inboundByExt.set(f.folder, new Set());
      inboundByExt.get(f.folder).add(u);
      edgeList.push({ from: 'd::' + f.folder, to: 't::' + u, arrows: 'to', label: u, font: { size: 10, align: 'middle' }, color: { color: '#2980b9', opacity: 0.55 } });
    });
  }

  // Hierarchical L→R layers: inbound-only folders (0) → self+files (1) →
  // types (2) → outbound folders (3). Keeps the dependency flow left-to-right
  // instead of collapsing into a force-directed hairball.
  // Self node (always present).
  nodes.push({
    id: selfId,
    label: '📂 ' + (focus.name || focus.id),
    title: focus.id + '\ndeclared types: ' + localDecls.size + '\nfiles: ' + folderFiles.length,
    group: 'self',
    shape: 'star',
    value: 4,
    level: 1,
  });
  // File nodes (📄) — one per source file in this folder, linked to the folder
  // anchor. Surfaces file names directly, and (below) which types each declares.
  folderFiles.forEach(f => {
    nodes.push({
      id: 'file::' + f.name,
      label: '📄 ' + f.name,
      title: f.name + '\nfolder: ' + focus.id + '\ndeclares: ' + (f.decls.length ? f.decls.join(', ') : '(none)'),
      group: 'file',
      shape: 'box',
      level: 1,
    });
    edgeList.push({ from: selfId, to: 'file::' + f.name, arrows: '', color: { color: '#bdc3c7', opacity: 0.5 }, dashes: true });
  });

  // Type nodes — labelled and coloured by kind (class/struct/enum/protocol/...).
  // Each is linked to the file that declares it.
  localDecls.forEach(t => {
    const kind = kindOf(t);
    nodes.push({
      id: 't::' + t,
      label: kind + '\n' + t,
      title: kind + ' ' + t + '\nfile: ' + declToFile[t] + '\nfolder: ' + focus.id,
      group: 'kind_' + kind,
      shape: 'box',
      level: 2,
    });
    if (declToFile[t]) {
      edgeList.push({ from: 'file::' + declToFile[t], to: 't::' + t, arrows: 'to', color: { color: '#7f8c8d', opacity: 0.35 } });
    }
  });
  // External folder nodes (click to navigate)
  externals.forEach(d => {
    const out = outboundByExt.get(d);
    const inn = inboundByExt.get(d);
    const lines = ['External folder: ' + d, '(click to navigate)'];
    if (out && out.size) lines.push('this folder USES from there: ' + [...out].sort().join(', '));
    if (inn && inn.size) lines.push('that folder USES from here: ' + [...inn].sort().join(', '));
    // Outbound (or bidirectional) folders sit on the right (level 3); folders
    // that only consume this one sit on the left (level 0).
    nodes.push({
      id: 'd::' + d,
      label: '📁 ' + d,
      title: lines.join('\n'),
      group: 'ext',
      shape: 'ellipse',
      level: (out && out.size) ? 3 : 0,
    });
  });

  if (network) network.destroy();
  network = new vis.Network(document.getElementById('net'),
    { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edgeList) }, {
    nodes: { font: { size: 13, face: 'Inter, sans-serif', color: themeText() }, borderWidth: 2, shapeProperties: { borderRadius: 8 },
      shadow: { enabled: true, color: 'rgba(20,28,55,0.12)', size: 8, x: 0, y: 2 } },
    // Hierarchical L→R: dependency flow reads inbound → types → outbound in
    // tidy layered columns. physics off so layout is deterministic & fast.
    layout: { hierarchical: {
      enabled: true, direction: 'LR', sortMethod: 'directed',
      levelSeparation: 320, nodeSpacing: 45, treeSpacing: 60,
      blockShifting: true, edgeMinimization: true, parentCentralization: true,
    } },
    physics: false,
    edges: { smooth: { type: 'cubicBezier', forceDirection: 'horizontal', roundness: 0.5 } },
    interaction: { hover: true, tooltipDelay: 100 },
    groups: typeGroups(),
  });
  network.on('click', params => {
    if (!params.nodes.length) return;
    const id = params.nodes[0];
    if (id.startsWith('d::')) { go(id.slice(3)); }
  });
  // Hierarchical layout is computed synchronously (physics off) — fit once.
  try { network.fit({ animation: false }); } catch (e) {}

  renderCrumbs();
  const ul = document.getElementById('kids');
  const filter = document.getElementById('filter').value.toLowerCase();
  ul.innerHTML = '';
  document.getElementById('leafCount').textContent =
    outboundByExt.size === 0
      ? 'LEAF (0 external deps)'
      : (outboundByExt.size + (currentMode === 'explore' ? ' dependency folder(s)' : ' blocker folder(s)'));

  // Hierarchy tab is read-only across both modes — migration/plan/exclude
  // actions live in Plan mode and on the node popover instead.

  // Files & the types they declare (grouped by file) — shown FIRST: it's what
  // this folder IS, before what it depends on.
  const KIND_ICON = { class: '🔵', struct: '🟢', enum: '🟠', protocol: '🟣', typealias: '🩵', type: '🟡' };
  if (folderFiles.length > 0) {
    const h = document.createElement('li');
    h.style.listStyle = 'none';
    h.style.fontWeight = 'bold';
    h.style.marginTop = '4px';
    h.style.cursor = 'default';
    h.textContent = 'Files (' + folderFiles.length + ') & declared types (' + localDecls.size + '):';
    ul.appendChild(h);
    folderFiles
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name))
      .forEach(f => {
        const decls = f.decls.slice()
          .filter(t => !filter || t.toLowerCase().includes(filter) || f.name.toLowerCase().includes(filter));
        if (filter && decls.length === 0 && !f.name.toLowerCase().includes(filter)) return;
        const fileLi = document.createElement('li');
        fileLi.style.listStyle = 'none';
        fileLi.style.marginTop = '4px';
        fileLi.innerHTML = '📄 <b>' + f.name + '</b>';
        ul.appendChild(fileLi);
        decls.sort().forEach(t => {
          const k = kindOf(t);
          const li = document.createElement('li');
          li.style.paddingLeft = '14px';
          li.textContent = (KIND_ICON[k] || '🟡') + ' ' + k + ' ' + t;
          li.onclick = () => { network.focus('t::' + t, { scale: 1.6, animation: true }); network.selectNodes(['t::' + t]); };
          ul.appendChild(li);
        });
      });
  }

  // Blockers section: external folders this folder depends on + the types it pulls in.
  if (outboundByExt.size > 0) {
    const h = document.createElement('li');
    h.style.listStyle = 'none';
    h.style.fontWeight = 'bold';
    h.style.marginTop = '8px';
    h.textContent = currentMode === 'explore'
      ? 'Depends on:'
      : 'To migrate, first resolve deps to:';
    h.style.cursor = 'default';
    ul.appendChild(h);
    [...outboundByExt.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .filter(([d]) => !filter || d.toLowerCase().includes(filter))
      .forEach(([d, types]) => {
        const li = document.createElement('li');
        const typeList = [...types].sort();
        li.innerHTML = '<span style="color:#c0392b;">📁 ' + d + '</span>'
          + '<div style="font-size:11px;color:#555;padding-left:14px;">uses: ' + typeList.join(', ') + '</div>';
        li.onclick = () => { go(d); };
        ul.appendChild(li);
      });
  }
  // Consumers section: who uses local types
  if (inboundByExt.size > 0) {
    const h = document.createElement('li');
    h.style.listStyle = 'none';
    h.style.fontWeight = 'bold';
    h.style.marginTop = '8px';
    h.style.cursor = 'default';
    h.textContent = 'Consumers (' + inboundByExt.size + ' folder(s)):';
    ul.appendChild(h);
    [...inboundByExt.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .filter(([d]) => !filter || d.toLowerCase().includes(filter))
      .forEach(([d, types]) => {
        const li = document.createElement('li');
        const typeList = [...types].sort();
        li.innerHTML = '<span style="color:#2980b9;">📁 ' + d + '</span>'
          + '<div style="font-size:11px;color:#555;padding-left:14px;">uses: ' + typeList.join(', ') + '</div>';
        li.onclick = () => { go(d); };
        ul.appendChild(li);
      });
  }
}

function renderCrumbs() {
  const chain = [];
  let cur = focusId;
  while (cur !== null && cur !== undefined) {
    chain.unshift(cur);
    cur = tree[cur].parent;
  }
  const el = document.getElementById('crumbs');
  el.innerHTML = '';
  chain.forEach((id, i) => {
    const a = document.createElement('a');
    a.textContent = tree[id].name;
    a.onclick = () => { go(id); };
    el.appendChild(a);
    if (i < chain.length - 1) el.appendChild(document.createTextNode(' / '));
  });
}

function renderKids(kids, outDeg) {
  const ul = document.getElementById('kids');
  const filter = document.getElementById('filter').value.toLowerCase();
  ul.innerHTML = '';
  // When this folder ALSO holds its own .swift files (alongside subfolders),
  // list them inline so the user can spot loose files they'd otherwise miss
  // behind a single drill-in entry. Header row still drills into the full
  // type-view (which has consumer/blocker breakdowns); rows beneath are just
  // a visible index.
  const ownFiles = filesByFolder[focusId] || [];
  if (ownFiles.length > 0 && focusId && tree[focusId]) {
    const KIND_ICON = { class: '🔵', struct: '🟢', enum: '🟠', protocol: '🟣', typealias: '🩵', type: '🟡' };
    const declCount = ownFiles.reduce((n, f) => n + (f.decls ? f.decls.length : 0), 0);
    const header = document.createElement('li');
    header.style.listStyle = 'none';
    header.style.fontWeight = '600';
    header.style.background = 'var(--accent-soft)';
    header.style.color = 'var(--accent-strong)';
    header.style.cursor = 'pointer';
    header.innerHTML = '📄 Files in this folder <span class="small">(' +
      ownFiles.length + ' file' + (ownFiles.length === 1 ? '' : 's') +
      ', ' + declCount + ' type' + (declCount === 1 ? '' : 's') + ') →</span>';
    header.onclick = () => renderTypeView(tree[focusId]);
    ul.appendChild(header);
    ownFiles
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name))
      .forEach(f => {
        const decls = (f.decls || []).slice()
          .filter(t => !filter || t.toLowerCase().includes(filter) || f.name.toLowerCase().includes(filter));
        if (filter && decls.length === 0 && !f.name.toLowerCase().includes(filter)) return;
        const fileLi = document.createElement('li');
        fileLi.style.listStyle = 'none';
        fileLi.style.marginTop = '2px';
        fileLi.style.cursor = 'pointer';
        fileLi.innerHTML = '📄 <b>' + f.name + '</b>';
        fileLi.onclick = () => renderTypeView(tree[focusId]);
        ul.appendChild(fileLi);
        decls.sort().forEach(t => {
          const k = kindOf(t);
          const tli = document.createElement('li');
          tli.style.paddingLeft = '14px';
          tli.style.cursor = 'pointer';
          tli.textContent = (KIND_ICON[k] || '🟡') + ' ' + k + ' ' + t;
          tli.onclick = () => renderTypeView(tree[focusId]);
          ul.appendChild(tli);
        });
      });
  }
  const newlySet = new Set(lastNewlyRevealed);
  const leaves = kids.filter(id => !(outDeg.get(id) > 0) && !isAllMigrated(id) && !isAllExcluded(id) && tree[id].children.length === 0);
  document.getElementById('leafCount').textContent = leaves.length + ' leaf';
  kids
    .slice()
    .sort((a, b) => {
      const ma = isAllMigrated(a), mb = isAllMigrated(b);
      if (ma !== mb) return ma ? 1 : -1;
      const la = !(outDeg.get(a) > 0), lb = !(outDeg.get(b) > 0);
      if (la !== lb) return la ? -1 : 1;
      return a.localeCompare(b);
    })
    .filter(id => !filter || tree[id].name.toLowerCase().includes(filter))
    .forEach(id => {
      const li = document.createElement('li');
      const isLeaf = !(outDeg.get(id) > 0);
      const isMig = isAllMigrated(id);
      const isExcl = isAllExcluded(id);
      const hasKids = tree[id].children.length > 0;
      const userMig = isMig && !isInitialMigrated(id);
      const exploreMode = currentMode === 'explore';
      // Sidebar swatch must match the exact hex used in the vis-network graph
      // so a folder's color in the list = its color in the canvas.
      let swatchColor = '#3b82f6';   // graph 'mid' / Explore 'folder' both indigo-blue
      let iconPrefix = '';
      if (exploreMode) {
        swatchColor = '#6366f1';     // matches folderGroups.folder background
      } else {
        if (isExcl) { swatchColor = '#ef4444'; iconPrefix = '🚫 '; }
        else if (isMig) swatchColor = '#cbd5e1';
        else if (newlySet.has(id)) swatchColor = '#f59e0b';
        else if (isLeaf && !hasKids) swatchColor = '#22c55e';
      }
      const label = document.createElement('span');
      const suffix = hasKids ? '  ▸' : '';
      const stateSuffix = exploreMode ? '' : (isExcl ? '  (won\'t modularize)' : (isMig ? '  (migrated)' : ''));
      label.innerHTML = '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:'
        + swatchColor + ';margin-right:8px;vertical-align:middle;flex-shrink:0;"></span>'
        + iconPrefix + escapeHtml(tree[id].name) + escapeHtml(suffix + stateSuffix);
      label.style.cursor = 'pointer';
      // Click only navigates — never mutates migration/exclude state.
      label.onclick = () => { go(id); };
      if (!exploreMode) {
        if (isExcl) label.style.color = '#b91c1c';
        else if (isMig) label.style.color = '#7f8c8d';
        else if (newlySet.has(id)) label.style.color = '#a26209';
      }
      li.appendChild(label);

      // Hierarchy tab is read-only — state mutations happen in Plan mode and
      // via the floating node popover on the graph.
      ul.appendChild(li);
    });
}

document.getElementById('filter').oninput = () => render();
document.getElementById('navBack').onclick = () => goHistory(-1);
document.getElementById('navFwd').onclick = () => goHistory(1);
document.getElementById('navSettings').onclick = () => {
  const settingsOpen = document.getElementById('panel-settings').classList.contains('active');
  if (settingsOpen) {
    // Toggle back to the first visible non-settings tab for the current mode.
    const fallback = document.querySelector('.tab:not([data-hidden="1"])');
    if (fallback) switchTab(fallback.dataset.tab);
  } else {
    switchTab('settings');
  }
  document.getElementById('navSettings').classList.toggle('active',
    document.getElementById('panel-settings').classList.contains('active'));
};
// Theme segmented control (reflect stored choice, then handle clicks).
document.querySelectorAll('#themeSeg button').forEach(b => {
  b.classList.toggle('active', b.dataset.theme === document.documentElement.getAttribute('data-theme'));
  b.onclick = () => applyTheme(b.dataset.theme);
});
// Always-visible legend dock: collapse / expand.
document.getElementById('legendToggle').onclick = () =>
  document.getElementById('legendDock').classList.toggle('collapsed');
// Browser-style shortcuts: Alt+← / Alt+→ (and back/forward mouse buttons).
window.addEventListener('keydown', e => {
  if (e.altKey && e.key === 'ArrowLeft') { e.preventDefault(); goHistory(-1); }
  else if (e.altKey && e.key === 'ArrowRight') { e.preventDefault(); goHistory(1); }
});
window.addEventListener('mouseup', e => {
  if (e.button === 3) { e.preventDefault(); goHistory(-1); }
  else if (e.button === 4) { e.preventDefault(); goHistory(1); }
});
// ── Migration wizard ─────────────────────────────────────────────────────────
const PACKAGES = DATA.packages || [];
const FOLDER_PKG = DATA.folder_package || {};
const wiz = { sourceId: null, targetIds: [], assign: {}, newPkgs: [] };
let wizPlan = null;

function wizSourceFolders() {
  if (!wiz.sourceId) return [];
  return Object.keys(FOLDER_PKG).filter(f => FOLDER_PKG[f] === wiz.sourceId);
}
function labelForTarget(id) {
  if (id === 'stay') return 'stay';
  if (id.startsWith('new:')) return id.slice(4) + ' (new SPM)';
  const p = PACKAGES.find(pp => pp.id === id);
  return p ? p.label : id;
}

function renderWizard() {
  // 1. Source
  const srcEl = document.getElementById('wizSource');
  if (!srcEl) return;
  srcEl.innerHTML = '';
  if (!PACKAGES.length) {
    srcEl.innerHTML = '<div class="small">No packages detected.</div>';
  }
  PACKAGES.forEach(p => {
    const row = document.createElement('label');
    row.className = 'wiz-row';
    const sub = p.kind === 'app' ? 'xcodeproj remainder' : 'SPM package';
    row.innerHTML = '<input type="radio" name="wizSrc">' +
      '<span><b>' + escapeHtml(p.label) + '</b> ' +
      '<span class="small">· ' + sub + ' · ' + p.folders.length + ' folder' +
      (p.folders.length === 1 ? '' : 's') + '</span></span>';
    const radio = row.querySelector('input');
    radio.checked = wiz.sourceId === p.id;
    radio.onchange = () => {
      wiz.sourceId = p.id;
      wiz.assign = {};
      // Drop the source from chosen targets if previously selected.
      wiz.targetIds = wiz.targetIds.filter(t => t !== p.id);
      renderWizard();
    };
    srcEl.appendChild(row);
  });

  // 2. Targets — existing SPMs (excluding the chosen source) + user-added new.
  const tgtEl = document.getElementById('wizTargets');
  tgtEl.innerHTML = '';
  const candidates = [
    ...PACKAGES.filter(p => p.kind === 'spm' && p.id !== wiz.sourceId),
    ...wiz.newPkgs.map(n => ({ id: 'new:' + n, label: n + ' (new SPM)', kind: 'new', folders: [] })),
  ];
  if (!candidates.length) {
    tgtEl.innerHTML = '<div class="small">No candidate targets yet — add one below.</div>';
  }
  candidates.forEach(p => {
    const row = document.createElement('label');
    row.className = 'wiz-row';
    const checked = wiz.targetIds.includes(p.id);
    row.innerHTML = '<input type="checkbox"> <span>' + escapeHtml(p.label) + '</span>';
    const cb = row.querySelector('input');
    cb.checked = checked;
    cb.onchange = () => {
      if (cb.checked) { if (!wiz.targetIds.includes(p.id)) wiz.targetIds.push(p.id); }
      else { wiz.targetIds = wiz.targetIds.filter(x => x !== p.id);
        // Reassign folders pointed at this target back to first remaining or stay.
        Object.keys(wiz.assign).forEach(f => {
          if (wiz.assign[f] === p.id) wiz.assign[f] = wiz.targetIds[0] || 'stay';
        });
      }
      renderWizard();
    };
    tgtEl.appendChild(row);
  });

  // 3. Assignment
  const asEl = document.getElementById('wizAssign');
  asEl.innerHTML = '';
  if (!wiz.sourceId) { asEl.innerHTML = '<div class="small">Pick a source first.</div>'; return; }
  if (!wiz.targetIds.length) { asEl.innerHTML = '<div class="small">Pick at least one target.</div>'; return; }
  const choices = [...wiz.targetIds, 'stay'];
  const srcFolders = wizSourceFolders().sort();
  srcFolders.forEach(f => {
    if (wiz.assign[f] === undefined || !choices.includes(wiz.assign[f])) wiz.assign[f] = wiz.targetIds[0];
    const row = document.createElement('div');
    row.className = 'wiz-asgn';
    row.innerHTML = '<span class="wiz-folder" title="' + escapeHtml(f) + '">' + escapeHtml(f) + '</span>' +
      '<button class="wiz-chip">' + escapeHtml(labelForTarget(wiz.assign[f])) + '</button>';
    row.querySelector('button').onclick = () => {
      const i = choices.indexOf(wiz.assign[f]);
      wiz.assign[f] = choices[(i + 1) % choices.length];
      renderWizard();
    };
    asEl.appendChild(row);
  });
}

// Tarjan SCC over a chosen folder subset, iterative to dodge deep stacks.
function wizComputeSccs(set) {
  const S = new Set(set);
  const deps = {};
  edges.forEach(e => {
    if (S.has(e.src) && S.has(e.dst) && e.src !== e.dst) {
      (deps[e.src] = deps[e.src] || []).push(e.dst);
    }
  });
  const idx = {}, low = {}, onS = {}, st = [], sccs = [];
  let counter = 0;
  for (const start of S) {
    if (idx[start] !== undefined) continue;
    const work = [[start, 0]];
    idx[start] = counter; low[start] = counter; counter++;
    st.push(start); onS[start] = true;
    while (work.length) {
      const top = work[work.length - 1];
      const v = top[0];
      const out = deps[v] || [];
      if (top[1] < out.length) {
        const w = out[top[1]++];
        if (idx[w] === undefined) {
          idx[w] = counter; low[w] = counter; counter++;
          st.push(w); onS[w] = true;
          work.push([w, 0]);
        } else if (onS[w]) {
          low[v] = Math.min(low[v], idx[w]);
        }
      } else {
        if (low[v] === idx[v]) {
          const comp = [];
          while (true) {
            const w = st.pop(); onS[w] = false; comp.push(w);
            if (w === v) break;
          }
          sccs.push(comp);
        }
        work.pop();
        if (work.length) { const p = work[work.length - 1][0]; low[p] = Math.min(low[p], low[v]); }
      }
    }
  }
  return { deps, sccs };
}

function computeWizardPlan(folderSet) {
  const { deps, sccs } = wizComputeSccs(folderSet);
  const sccOf = {};
  sccs.forEach((c, i) => c.forEach(v => sccOf[v] = i));
  const sdeps = sccs.map(() => new Set());
  Object.keys(deps).forEach(a => {
    for (const b of deps[a]) if (sccOf[a] !== sccOf[b]) sdeps[sccOf[a]].add(sccOf[b]);
  });
  const remaining = sdeps.map(s => s.size);
  const reverse = sccs.map(() => new Set());
  sdeps.forEach((s, i) => { for (const j of s) reverse[j].add(i); });
  const eligible = [];
  for (let i = 0; i < sccs.length; i++) if (remaining[i] === 0) eligible.push(i);
  const out = [];
  while (eligible.length) {
    eligible.sort((a, b) => sccs[a].length - sccs[b].length || (sccs[a][0] || '').localeCompare(sccs[b][0] || ''));
    const pick = eligible.shift();
    const fs = sccs[pick].slice().sort();
    out.push({
      step: out.length + 1,
      folders: fs,
      is_cycle: fs.length > 1,
      size: fs.length,
      targets: fs.map(f => wiz.assign[f]),
    });
    for (const r of reverse[pick]) {
      remaining[r]--;
      if (remaining[r] === 0) eligible.push(r);
    }
  }
  return out;
}

function renderWizardPlan() {
  document.getElementById('targetBanner').style.display = 'none';
  document.getElementById('planFilter').style.display = 'none';
  document.getElementById('stuckDetails').style.display = 'none';
  const rec = document.getElementById('recCard');
  const srcLabel = (PACKAGES.find(p => p.id === wiz.sourceId) || {}).label || '?';
  const tgtLabels = wiz.targetIds.map(labelForTarget).join(', ') || '(none)';
  if (!wizPlan || !wizPlan.length) {
    rec.innerHTML = '<h3>No steps</h3><div class="info">Nothing to migrate — every source folder is set to <b>stay</b>.</div>';
    document.getElementById('planList').innerHTML = '';
    return;
  }
  rec.innerHTML = '<h3>🧭 Wizard plan</h3>' +
    '<div class="info"><b>From:</b> ' + escapeHtml(srcLabel) + '<br><b>To:</b> ' + escapeHtml(tgtLabels) + '<br>' +
    '<b>' + wizPlan.length + '</b> step(s), ordered so each step\'s deps are already migrated.</div>';
  const wrap = document.getElementById('planList');
  wrap.innerHTML = '';
  wizPlan.forEach(s => {
    const div = document.createElement('div');
    div.className = 'step';
    const lines = s.folders.map((f, i) =>
      '<div style="margin: 2px 0;"><code>' + escapeHtml(f) + '</code>' +
      ' <span class="small">→ ' + escapeHtml(labelForTarget(s.targets[i])) + '</span></div>'
    ).join('');
    div.innerHTML = '<div><span class="stepNum">' + s.step + '.</span>' +
      (s.is_cycle ? '<b>⚠ Bundle of ' + s.size + ' (cyclically coupled)</b>' : '<b>Move folder</b>') +
      '</div>' + lines;
    wrap.appendChild(div);
  });
}

document.getElementById('wizAddTarget').onclick = () => {
  const el = document.getElementById('wizNewTarget');
  const name = el.value.trim();
  if (!name) return;
  if (!wiz.newPkgs.includes(name)) {
    wiz.newPkgs.push(name);
    if (!wiz.targetIds.includes('new:' + name)) wiz.targetIds.push('new:' + name);
  }
  el.value = '';
  renderWizard();
};
document.getElementById('wizReset').onclick = () => {
  wiz.sourceId = null; wiz.targetIds = []; wiz.assign = {}; wiz.newPkgs = [];
  wizPlan = null;
  renderWizard();
};
document.getElementById('wizCompute').onclick = () => {
  if (!wiz.sourceId) { alert('Pick a source.'); return; }
  if (!wiz.targetIds.length) { alert('Pick at least one target.'); return; }
  const moving = Object.keys(wiz.assign).filter(f => wiz.assign[f] !== 'stay');
  if (!moving.length) { alert('Every folder is set to stay — nothing to move.'); return; }
  wizPlan = computeWizardPlan(moving);
  switchTab('plan');
  renderWizardPlan();
};

// Patch renderPlan to honor wizard plan when active + in migration mode.
const _origRenderPlan = renderPlan;
renderPlan = function() {
  if (currentMode === 'migration' && wizPlan) { renderWizardPlan(); return; }
  _origRenderPlan();
};

renderPlan();
renderWizard();
applyMode('explore');
updateMigratedSidebar();
updateExcludeSidebar();
// Defer the first graph render until layout is settled. Rendering synchronously
// at parse time builds the vis network into a not-yet-sized #net container, so
// it draws into a 0×0 canvas and shows blank until the next render. Waiting for
// `load` guarantees the flex container has its size (and the CDN vis script is
// ready). Keep the graph fitted on window resize too.
if (document.readyState === 'complete') { render(); }
else { window.addEventListener('load', render); }
window.addEventListener('resize', () => { if (network) try { network.fit({ animation: false }); } catch (e) {} });
</script>
</body>
</html>
"""


def build_task_list(
    plan: list[dict],
    file_records: list[dict],
    root_label: str,
    root_path: str,
    migrated_prefixes: list[str],
    initial_migrated_count: int,
    source_total: int,
) -> list[dict]:
    """Flatten the plan into a sequence of PR-sized refactor tasks.

    For singleton steps: one task = extract that folder.
    For cycle steps: one task per extraction candidate inside the cycle, ordered
    lightest first. Each task carries enough info for an automated agent to
    execute it: target folder, files involved, outgoing refs to refactor, and
    the specific type names that need to be abstracted/moved.
    """
    files_by_folder: dict[str, list[str]] = defaultdict(list)
    for f in file_records:
        files_by_folder[f["folder"]].append(f["name"])

    tasks: list[dict] = []
    task_num = 0

    for step in plan:
        if not step["is_cycle"]:
            folder = step["folders"][0]
            task_num += 1
            tasks.append({
                "id": task_num,
                "plan_step": step["step"],
                "type": "extract_leaf",
                "folder": folder,
                "files": sorted(files_by_folder.get(folder, [])),
                "files_count": len(files_by_folder.get(folder, [])),
                "refactor_edges": [],
                "outgoing_refs_to_refactor": 0,
                "unlocks": [
                    {"folder": u["folders"][0] if u["size"] == 1 else None,
                     "size": u["size"]}
                    for u in step["unlocks"]
                ],
                "notes": (
                    "This folder has zero first-party outgoing deps. "
                    "Extract directly: create a new SPM target, move the files, "
                    "update consumers to import the new module."
                ),
            })
        else:
            cycle_size = step["size"]
            for ext in step["extractions"]:
                task_num += 1
                folder = ext["folder"]
                tasks.append({
                    "id": task_num,
                    "plan_step": step["step"],
                    "type": "extract_from_cycle",
                    "folder": folder,
                    "files": sorted(files_by_folder.get(folder, [])),
                    "files_count": len(files_by_folder.get(folder, [])),
                    "refactor_edges": [
                        {
                            "target_folder": e["dst"],
                            "refs": e["refs"],
                            "types": e["types"],
                        }
                        for e in ext["edges"]
                    ],
                    "outgoing_refs_to_refactor": ext["outgoing_refs"],
                    "incoming_consumers": ext["incoming_count"],
                    "cycle_size": cycle_size,
                    "unlocks": [],
                    "notes": (
                        f"Member of a {cycle_size}-folder cycle. "
                        "Before extraction, refactor every outgoing reference: "
                        "move the type into a shared module, invert the dependency via "
                        "a protocol the target folder owns, or move this folder's "
                        "responsibility into the target. Then move the folder into "
                        "its own SPM target."
                    ),
                })

    return tasks


def write_task_list_markdown(tasks: list[dict], meta: dict, out_path: Path) -> None:
    lines: list[str] = []
    lines.append(f"# Migration Plan — {meta['root_label']}")
    lines.append("")
    lines.append(f"Project root: `{meta['root_path']}`  ")
    lines.append(f"Folders already in SPM (baseline): **{meta['baseline_count']}**"
                 f" — prefixes: `{', '.join(meta['migrated_prefixes']) or '(none)'}`  ")
    excluded_count = meta.get('excluded_count', 0)
    if excluded_count:
        lines.append(f"Folders marked won't-be-modularized (excluded): **{excluded_count}**  ")
    lines.append(f"Folders to extract: "
                 f"**{meta['source_total'] - meta['baseline_count'] - excluded_count}**  ")
    lines.append(f"Total tasks: **{len(tasks)}**")
    lines.append("")
    lines.append("## How to use this list")
    lines.append("")
    lines.append(
        "Each task is one PR. Tasks are ordered: complete them sequentially. "
        "Within a single task, do **not** combine refactor + move + import update "
        "for multiple folders — one folder, one PR. After each task ships, "
        "regenerate this plan (`./find_leaf_modules.py <root> --list`) so the "
        "remaining plan reflects the new state."
    )
    lines.append("")
    lines.append("Task types:")
    lines.append("- `extract_leaf` — folder has zero remaining first-party deps. "
                 "Just move it.")
    lines.append("- `extract_from_cycle` — folder participates in a dependency cycle. "
                 "Refactor each outgoing edge first, then move.")
    lines.append("")
    lines.append("---")
    lines.append("")
    for t in tasks:
        lines.append(f"## Task {t['id']} — `{t['folder']}`")
        lines.append("")
        lines.append(f"- **Type:** `{t['type']}`")
        lines.append(f"- **Plan step:** {t['plan_step']}")
        lines.append(f"- **Files:** {t['files_count']}")
        if t["files"]:
            lines.append("  - " + ", ".join(f"`{n}`" for n in t["files"]))
        if t["type"] == "extract_from_cycle":
            lines.append(f"- **Cycle size:** {t['cycle_size']} folders")
            lines.append(f"- **Outgoing refs to refactor:** {t['outgoing_refs_to_refactor']}")
            lines.append(f"- **Incoming consumers:** {t['incoming_consumers']}")
        if t["refactor_edges"]:
            lines.append("- **Edges to refactor:**")
            for e in t["refactor_edges"]:
                types_str = ", ".join(f"`{x}`" for x in e["types"]) or "(no specific types resolved)"
                lines.append(
                    f"  - → `{e['target_folder']}` ({e['refs']} ref"
                    f"{'' if e['refs'] == 1 else 's'}): {types_str}"
                )
        if t["unlocks"]:
            unlocked_strs = [
                f"`{u['folder']}`" if u["folder"]
                else f"⚠cycle of {u['size']}"
                for u in t["unlocks"]
            ]
            lines.append("- **Unlocks:** " + ", ".join(unlocked_strs))
        lines.append("")
        lines.append("**Instructions for this PR:**")
        lines.append("")
        lines.append(t["notes"])
        if t["type"] == "extract_from_cycle" and t["refactor_edges"]:
            lines.append("")
            lines.append("Steps:")
            lines.append("")
            lines.append("1. For each edge listed above, refactor so the source folder "
                         "no longer references types in the target folder. Options:")
            lines.append("   - Move the referenced type into a shared lower-level module "
                         "both folders can depend on.")
            lines.append("   - Define a protocol in this folder that the target folder "
                         "conforms to (dependency inversion).")
            lines.append("   - Pass the dependency in by injection from a coordinator above.")
            lines.append("2. Verify the codebase still compiles and tests pass.")
            lines.append("3. Re-run the analysis to confirm this folder's outgoing refs "
                         "are now zero (or only go to already-migrated folders).")
            lines.append("4. Create a new SPM target/package, move the folder's files into it.")
            lines.append("5. Update consumers to import the new module.")
        else:
            lines.append("")
            lines.append("Steps:")
            lines.append("")
            lines.append("1. Create a new SPM target/package for this folder.")
            lines.append("2. Move every file into the new module's sources.")
            lines.append("3. Mark types `public` where consumers need them.")
            lines.append("4. Update every consumer to import the new module.")
            lines.append("5. Verify the build and tests are green.")
        lines.append("")
        lines.append("---")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def write_task_list_json(tasks: list[dict], meta: dict, out_path: Path) -> None:
    out_path.write_text(
        json.dumps({"meta": meta, "tasks": tasks}, indent=2),
        encoding="utf-8",
    )


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
    p.add_argument("--list", nargs="?", type=Path, const=SCRIPT_DIR / "migration_plan.md",
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
    p.add_argument("--from-index", type=Path, default=None, metavar="JSON",
                   help="Load a resolved dependency graph produced by the index_graph "
                        "Swift tool (reads the compiler index store) instead of "
                        "regex-scanning sources. Edges are resolved by USR, so name "
                        "collisions never fabricate edges. project_root MUST be the "
                        "same root passed to index_graph (folder ids are relative to it).")
    return p.parse_args()


def auto_detect_migrated_prefixes(root: Path) -> list[str]:
    """Any folder under root that contains Package.swift is treated as an SPM
    package; its Sources/ subtree (if present) is the migrated code. Falls back
    to the package dir itself when Sources/ is absent.

    Walks recursively (not just root's direct children) so nested packages —
    e.g. a monolith being split into App/Modules/Foo, App/Modules/Bar — are all
    picked up. Reuses should_skip_dir() so it never descends into build output,
    SPM checkouts, .git, tests, etc., and prunes a package's own subtree once
    found (a package's inner targets are part of that one migrated prefix, and a
    vendored package inside checkouts/ is skipped by the skip-list).
    """
    prefixes: list[str] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dir_path = Path(dirpath)
        rel = "" if dir_path == root else str(dir_path.relative_to(root))
        # Prune skipped subdirs in place so os.walk doesn't descend into them.
        dirnames[:] = [
            d for d in dirnames
            if not should_skip_dir(
                dir_path / d,
                f"{rel}/{d}" if rel else d,
                include_tests=False,
                ignore_patterns=[],
            )
        ]
        if "Package.swift" not in filenames:
            continue
        pkg_rel = rel  # folder of this Package.swift, relative to root
        if (dir_path / "Sources").is_dir():
            prefixes.append((f"{pkg_rel}/Sources" if pkg_rel else "Sources").rstrip("/"))
        else:
            prefixes.append(pkg_rel or ".")
        # Don't descend further: everything below belongs to this package.
        dirnames[:] = []
    return prefixes


def is_migrated(folder: str, prefixes: list[str]) -> bool:
    for p in prefixes:
        p = p.rstrip("/")
        if folder == p or folder.startswith(p + "/"):
            return True
    return False


def load_exclusions(path: Path) -> set[str]:
    """Read the persisted "won't be modularized" folder list.

    Format is a JSON array of folder paths (the HTML graph's Exclude button
    downloads exactly this shape); a ``{"excluded": [...]}`` wrapper is also
    tolerated. A missing or malformed file yields an empty set so a fresh
    checkout just works. Entries may be ancestor folders — expansion to the
    actual source-folder subtree happens in main().
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(raw, dict):
        raw = raw.get("excluded", [])
    if not isinstance(raw, list):
        return set()
    return {str(x).strip().rstrip("/") for x in raw if str(x).strip()}


def main() -> int:
    args = parse_args()
    root = args.project_root.expanduser().resolve()
    if not root.is_dir():
        print(f"Project root not found: {root}", file=sys.stderr)
        return 1
    if not args.ext.startswith("."):
        args.ext = "." + args.ext
    root_label = args.label or root.name or str(root)

    resolved_pair_types: dict | None = None
    if args.from_index is not None:
        (decls, leaf_edges, multi_decl_types, all_folders, file_records, type_owners,
         raw_owners, resolved_pair_types, type_kinds) = load_index_graph(
            args.from_index.expanduser().resolve()
        )
    else:
        (decls, leaf_edges, multi_decl_types, all_folders, file_records, type_owners,
         raw_owners) = scan(
            root, include_tests=args.include_tests, ignore_patterns=args.ignore, ext=args.ext
        )
        type_kinds = {}  # regex scanner has no symbol-kind info
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
    source_folders = all_source_folders - initial_migrated - excluded

    tree = build_tree(all_folders, decls, root_label=root_label)
    # Plan computed over source folders only. Edges into pre-migrated folders
    # are dropped (treated as external SPM deps).
    plan_edges = {
        (a, b): w for (a, b), w in leaf_edges.items()
        if a in source_folders and b in source_folders
    }
    plan, stuck = compute_migration_plan(plan_edges, source_folders)

    # For each cycle bundle in the plan, compute ordered cycle-breaker edges.
    # Use plan_edges (not leaf_edges) so already-migrated targets are ignored.
    # In index mode pair_types is already resolved by USR; recomputing by name
    # would reintroduce the collision false-edges we built this tool to avoid.
    pair_types = (resolved_pair_types if resolved_pair_types is not None
                  else compute_pair_types(file_records, raw_owners))
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
        print(f"  → Start with: {h_label} (unlocks {len(head['unlocks'])} bundle(s))")

    # Ship every edge to the HTML — Explore mode now renders SPM-to-SPM
    # coupling as first-class. Migration mode filters per the chosen
    # source/target at render time.
    edges_for_html = dict(leaf_edges)
    folder_package, packages = _build_package_map(
        all_source_folders, migrated_prefixes
    )

    # Decide which outputs to emit. --out is a back-compat alias for --graph.
    graph_path: Path | None = args.graph
    if args.out is not None:
        graph_path = args.out
    list_path: Path | None = args.list
    # If user passed neither, default to graph at DEFAULT_OUT (preserve old behavior).
    if graph_path is None and list_path is None:
        graph_path = DEFAULT_OUT

    if graph_path is not None:
        graph_path = graph_path.expanduser().resolve()
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        render_html(
            tree, edges_for_html, multi_decl_types, file_records, type_owners,
            plan, stuck, root_label, str(root), sorted(initial_migrated),
            migrated_prefixes, graph_path, type_kinds=type_kinds,
            initial_excluded=sorted(excluded), excluded_file=excluded_file,
            folder_package=folder_package, packages=packages,
        )
        print(f"\nWrote graph: {graph_path}")

    if list_path is not None:
        list_path = list_path.expanduser().resolve()
        list_path.parent.mkdir(parents=True, exist_ok=True)
        tasks = build_task_list(
            plan, file_records, root_label, str(root),
            migrated_prefixes, len(initial_migrated), len(all_source_folders),
        )
        meta = {
            "root_label": root_label,
            "root_path": str(root),
            "migrated_prefixes": migrated_prefixes,
            "baseline_count": len(initial_migrated),
            "source_total": len(all_source_folders),
            "excluded_count": len(excluded),
            "tasks_total": len(tasks),
        }
        if args.list_format == "json":
            write_task_list_json(tasks, meta, list_path)
        else:
            write_task_list_markdown(tasks, meta, list_path)
        print(f"Wrote list:  {list_path}  ({len(tasks)} task(s), format={args.list_format})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
