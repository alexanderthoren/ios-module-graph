"""CI architecture gate: fail the build when the dependency graph regresses.

Where ``modgraph.diff`` *reports* what changed, ``modgraph.check`` *judges* a
graph against explicit rules and exits non-zero on violations — the standing
guardrail for teams mid-migration:

* ``--max-cycles N`` — absolute ceiling on folder cycles.
* ``--forbid 'SRC -> DST'`` — no edges from folders matching SRC to folders
  matching DST (fnmatch globs over folder ids, e.g. ``'Features/* -> Legacy*'``;
  ``*`` crosses ``/``). Repeatable.
* ``--no-new-edges`` / ``--no-new-cycles`` — ratchet rules against a baseline
  graph (``--against old.json``): whatever coupling exists is tolerated, adding
  more fails.

Usage::

    python3 -m modgraph.check <new.json> [--against <old.json>]
        [--max-cycles N] [--forbid 'A -> B']... [--no-new-edges]
        [--no-new-cycles]

Exit codes: 0 = all rules pass, 1 = violations (report on stdout), 2 = usage /
schema errors. The rule engine (:func:`check_graph`) is pure; output is
deterministic like the rest of the pipeline.
"""
from __future__ import annotations

import argparse
import sys
from fnmatch import fnmatch
from pathlib import Path

from .diff import _cycles, _format_edge, compute_graph_diff
from .index_loader import IndexSchemaError, load_index_graph
from .models import GraphData

# Cap per-violation detail lines in the report; the count is always exact.
_MAX_DETAILS_SHOWN = 20


def parse_forbid(pattern: str) -> tuple[str, str]:
    """Split ``'SRC -> DST'`` into its two glob halves.

    Raises ``ValueError`` on anything without exactly one ``->`` separator or
    with an empty side — a silently-unmatchable rule would be worse than none.
    """
    parts = pattern.split("->")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            f"invalid --forbid pattern {pattern!r} — expected 'SRC -> DST' "
            f"(fnmatch globs, e.g. 'Features/* -> Legacy*')"
        )
    return parts[0].strip(), parts[1].strip()


def _edge_detail(key: tuple[str, str], gd: GraphData) -> dict:
    src, dst = key
    return {"src": src, "dst": dst, "w": gd.leaf_edges[key],
            "types": sorted((gd.pair_types or {}).get(key, set()))}


def check_graph(
    new: GraphData,
    *,
    old: GraphData | None = None,
    max_cycles: int | None = None,
    forbid: list[str] = (),
    no_new_edges: bool = False,
    no_new_cycles: bool = False,
) -> list[dict]:
    """Evaluate every requested rule; return violations (empty = all pass).

    Each violation: ``{"rule", "message", "details"}`` where details are
    edge records or cycle member-lists, always sorted. Ratchet rules require
    ``old`` — the caller validates that (argparse does it for the CLI).
    """
    violations: list[dict] = []

    if max_cycles is not None:
        cycles = _cycles(new)
        if len(cycles) > max_cycles:
            violations.append({
                "rule": f"max-cycles {max_cycles}",
                "message": f"{len(cycles)} folder cycle(s), limit is {max_cycles}",
                "details": cycles,
            })

    for pattern in forbid:
        src_pat, dst_pat = parse_forbid(pattern)
        matched = sorted(k for k in new.leaf_edges
                         if fnmatch(k[0], src_pat) and fnmatch(k[1], dst_pat))
        if matched:
            violations.append({
                "rule": f"forbid {src_pat} -> {dst_pat}",
                "message": f"{len(matched)} forbidden edge(s)",
                "details": [_edge_detail(k, new) for k in matched],
            })

    if no_new_edges or no_new_cycles:
        diff = compute_graph_diff(old, new)
        if no_new_edges and diff["added_edges"]:
            violations.append({
                "rule": "no-new-edges",
                "message": f"{len(diff['added_edges'])} edge(s) not in the baseline",
                "details": diff["added_edges"],
            })
        if no_new_cycles and diff["cycles_formed"]:
            violations.append({
                "rule": "no-new-cycles",
                "message": f"{len(diff['cycles_formed'])} cycle(s) not in the baseline",
                "details": diff["cycles_formed"],
            })

    return violations


def format_report(violations: list[dict]) -> str:
    """Deterministic, human-readable verdict for CI logs."""
    if not violations:
        return "✓ all checks passed\n"
    lines = [f"✗ {len(violations)} rule violation(s)", ""]
    for v in violations:
        lines.append(f"## {v['rule']} — {v['message']}")
        lines.append("")
        shown = v["details"][:_MAX_DETAILS_SHOWN]
        for d in shown:
            if isinstance(d, dict):                       # an edge record
                lines.append(_format_edge(d))
            else:                                         # a cycle member-list
                lines.append("- " + " ⇄ ".join(f"`{f}`" for f in d))
        hidden = len(v["details"]) - len(shown)
        if hidden > 0:
            lines.append(f"- … +{hidden} more")
        lines.append("")
    return "\n".join(lines)


def _main(argv) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m modgraph.check",
        description="Fail (exit 1) when the dependency graph violates the "
                    "given rules — an architecture gate for CI.",
    )
    ap.add_argument("new", type=Path, help="index_graph.json to judge")
    ap.add_argument("--against", type=Path, metavar="OLD",
                    help="baseline index_graph.json for the ratchet rules")
    ap.add_argument("--max-cycles", type=int, default=None, metavar="N",
                    help="fail when the graph has more than N folder cycles")
    ap.add_argument("--forbid", action="append", default=[], metavar="'A -> B'",
                    help="fail on any edge from folders matching glob A to "
                         "folders matching glob B (repeatable)")
    ap.add_argument("--no-new-edges", action="store_true",
                    help="fail on any edge absent from the baseline (needs --against)")
    ap.add_argument("--no-new-cycles", action="store_true",
                    help="fail on any cycle absent from the baseline (needs --against)")
    args = ap.parse_args(argv)

    if args.max_cycles is None and not args.forbid \
            and not args.no_new_edges and not args.no_new_cycles:
        ap.error("no rules given — pass --max-cycles, --forbid, "
                 "--no-new-edges and/or --no-new-cycles")
    if (args.no_new_edges or args.no_new_cycles) and args.against is None:
        ap.error("--no-new-edges/--no-new-cycles need a baseline (--against OLD)")

    try:
        for pattern in args.forbid:
            parse_forbid(pattern)                 # fail fast on bad rules
        new = load_index_graph(args.new.expanduser().resolve())
        old = (load_index_graph(args.against.expanduser().resolve())
               if args.against is not None else None)
    except (OSError, ValueError, IndexSchemaError) as e:
        print(f"modgraph.check: {e}", file=sys.stderr)
        return 2

    violations = check_graph(
        new, old=old, max_cycles=args.max_cycles, forbid=args.forbid,
        no_new_edges=args.no_new_edges, no_new_cycles=args.no_new_cycles,
    )
    print(format_report(violations), end="")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
