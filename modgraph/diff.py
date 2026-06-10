"""Diff two USR-resolved index graphs: what changed in the architecture.

Compare ``index_graph.json`` from two points — main vs a PR branch, or before
vs after an extraction — and report the deltas that matter for modularization:

* folders added / removed,
* folder-edges added / removed, each annotated with the **type references that
  explain it** (from ``pair_types``, so a reviewer sees *why* the new
  dependency exists, not just that it does),
* cycles formed / broken (Tarjan SCCs over each side's folder graph).

Weight drift on existing edges is deliberately ignored: a ratchet cares about
*new dependencies*, not about an existing one being used once more.

Usage::

    python3 -m modgraph.diff <old.json> <new.json> [--format markdown|json]
                             [--exit-code]

``--exit-code`` exits 1 when the graphs differ (git-diff convention) — handy in
scripts; ``modgraph check`` builds the real CI gate on top of this module.
Output is deterministic (everything sorted), like the rest of the pipeline.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from .graph import _tarjan_sccs
from .index_loader import IndexSchemaError, load_index_graph
from .models import GraphData

# Show at most this many explaining type names per edge in markdown output.
_MAX_TYPES_SHOWN = 8


def _cycles(gd: GraphData) -> list[list[str]]:
    """Sorted list of this graph's folder cycles (SCCs of size ≥ 2)."""
    deps: dict[str, set[str]] = defaultdict(set)
    for (src, dst) in gd.leaf_edges:
        deps[src].add(dst)
    nodes = set(gd.all_folders) | set(deps)
    return sorted(sorted(c) for c in _tarjan_sccs(nodes, deps) if len(c) > 1)


def _edge_record(key: tuple[str, str], gd: GraphData) -> dict:
    """One added/removed edge with the type names that explain it."""
    src, dst = key
    return {
        "src": src,
        "dst": dst,
        "w": gd.leaf_edges[key],
        "types": sorted((gd.pair_types or {}).get(key, set())),
    }


def _side_summary(gd: GraphData, cycles: list[list[str]]) -> dict:
    return {
        "commit": gd.target_commit,
        "folders": len(gd.all_folders),
        "edges": len(gd.leaf_edges),
        "cycles": len(cycles),
    }


def compute_graph_diff(old: GraphData, new: GraphData) -> dict:
    """Pure, deterministic delta between two folder graphs."""
    old_cycles, new_cycles = _cycles(old), _cycles(new)
    old_cycle_set = {tuple(c) for c in old_cycles}
    new_cycle_set = {tuple(c) for c in new_cycles}
    added_edge_keys = sorted(set(new.leaf_edges) - set(old.leaf_edges))
    removed_edge_keys = sorted(set(old.leaf_edges) - set(new.leaf_edges))
    return {
        "old": _side_summary(old, old_cycles),
        "new": _side_summary(new, new_cycles),
        "added_folders": sorted(new.all_folders - old.all_folders),
        "removed_folders": sorted(old.all_folders - new.all_folders),
        "added_edges": [_edge_record(k, new) for k in added_edge_keys],
        "removed_edges": [_edge_record(k, old) for k in removed_edge_keys],
        "cycles_formed": [list(c) for c in sorted(new_cycle_set - old_cycle_set)],
        "cycles_broken": [list(c) for c in sorted(old_cycle_set - new_cycle_set)],
    }


def has_changes(diff: dict) -> bool:
    return any(diff[k] for k in (
        "added_folders", "removed_folders", "added_edges", "removed_edges",
        "cycles_formed", "cycles_broken",
    ))


def _commit_label(commit: dict | None) -> str:
    """``abc1234* ("subject")`` — the same shape the Improvements tab uses."""
    if not commit or not commit.get("sha"):
        return "unknown"
    label = commit["sha"][:7]
    if commit.get("dirty"):
        label += "*"
    if commit.get("subject"):
        label += f" (“{commit['subject']}”)"
    return label


def _format_edge(e: dict) -> str:
    shown = e["types"][:_MAX_TYPES_SHOWN]
    extra = len(e["types"]) - len(shown)
    types = ", ".join(shown) + (f", +{extra} more" if extra > 0 else "")
    detail = f" — {types}" if types else ""
    return f"- `{e['src']}` → `{e['dst']}` (w={e['w']}){detail}"


def format_diff_markdown(diff: dict) -> str:
    """Human-readable report; sections appear only when non-empty."""
    old, new = diff["old"], diff["new"]
    lines = [
        "# Dependency-graph diff",
        "",
        "| | old | new |",
        "|---|---|---|",
        f"| commit | {_commit_label(old['commit'])} | {_commit_label(new['commit'])} |",
        f"| folders | {old['folders']} | {new['folders']} |",
        f"| edges | {old['edges']} | {new['edges']} |",
        f"| cycles | {old['cycles']} | {new['cycles']} |",
    ]
    if not has_changes(diff):
        lines += ["", "No structural changes."]
        return "\n".join(lines) + "\n"

    def section(title: str, rows: list[str]):
        if rows:
            lines.extend(["", f"## {title} ({len(rows)})", ""])
            lines.extend(rows)

    section("Added edges", [_format_edge(e) for e in diff["added_edges"]])
    section("Removed edges", [_format_edge(e) for e in diff["removed_edges"]])
    section("Added folders", [f"- `{f}`" for f in diff["added_folders"]])
    section("Removed folders", [f"- `{f}`" for f in diff["removed_folders"]])
    section("Cycles formed", [f"- {' ⇄ '.join(f'`{f}`' for f in c)}"
                              for c in diff["cycles_formed"]])
    section("Cycles broken", [f"- {' ⇄ '.join(f'`{f}`' for f in c)}"
                              for c in diff["cycles_broken"]])
    return "\n".join(lines) + "\n"


def _main(argv) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m modgraph.diff",
        description="Diff two index_graph.json files: edges/folders/cycles "
                    "added or removed, with the types explaining each edge.",
    )
    ap.add_argument("old", type=Path, help="baseline index_graph.json")
    ap.add_argument("new", type=Path, help="candidate index_graph.json")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--exit-code", action="store_true",
                    help="exit 1 when the graphs differ (git-diff convention)")
    args = ap.parse_args(argv)
    try:
        old = load_index_graph(args.old.expanduser().resolve())
        new = load_index_graph(args.new.expanduser().resolve())
    except (OSError, ValueError, IndexSchemaError) as e:
        print(f"modgraph.diff: {e}", file=sys.stderr)
        return 2
    diff = compute_graph_diff(old, new)
    if args.format == "json":
        print(json.dumps(diff, indent=2, sort_keys=True))
    else:
        print(format_diff_markdown(diff), end="")
    return 1 if (args.exit_code and has_changes(diff)) else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
