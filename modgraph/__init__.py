"""modgraph — folder-level Swift dependency graph + SPM-migration planner.

Stage 2 of the pipeline (Stage 1 is the ``index_graph`` Swift reader). Walks a
Swift project, builds a type-reference graph at folder granularity, computes an
SCC-aware topological migration order, and renders an interactive HTML view plus
a PR-sized migration task list.

Public entry point:
    from modgraph.cli import main

Module map:
    config         constants, regexes, default paths
    scanner        regex-scan fallback (no index store)
    index_loader   load the USR-resolved graph from the index_graph Swift tool
    graph          Tarjan SCC, SCC-aware migration plan, folder tree
    cycles         feedback-arc-set + per-folder extraction targets for cycles
    spm            SPM package map + auto-detection of migrated prefixes
    exclusions     persisted "won't modularize" set + transitive blocked-by
    tasks          flatten the plan into tasks; markdown / json writers
    render         inject the payload into the HTML template
    cli            argparse + main orchestration
"""
from __future__ import annotations

__all__ = ["main"]


def main() -> int:
    """Convenience re-export of :func:`modgraph.cli.main`."""
    from .cli import main as _main

    return _main()
