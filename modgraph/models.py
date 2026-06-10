"""Typed container for the resolved dependency graph.

Both producers — :func:`modgraph.scanner.scan` (regex fallback) and
:func:`modgraph.index_loader.load_index_graph` (USR-resolved) — return one of
these. It replaces the positional tuple the single-file version unpacked, so
callers reference fields by name and the index-only extras carry sensible
defaults for the scan path.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GraphData:
    """A folder-level type-reference graph plus the metadata the UI/plan need.

    Fields common to both producers:
        decls            folder id -> set of declared type names
        leaf_edges       (src_folder, dst_folder) -> reference count (src != dst)
        multi_decl_types type names declared in 2+ folders
        all_folders      every folder id seen (declarers, refs, edge endpoints)
        file_records     per-file {folder, name, decls, refs[, ref_owners]}
        type_owners      type name -> sorted list of declaring folders
        raw_owners       type name -> set of declaring folders (unsorted)

    Index-only extras (empty/None on the regex-scan path):
        pair_types  (src, dst) -> set of type names crossing that edge. ``None``
                    on the scan path signals "recompute by name from refs".
        type_kinds  type name -> symbol kind (class/struct/…); scanner has none.
        file_edges  file-to-file couplings the folder graph hides.
        type_edges  type-to-type couplings with containing-file info.
        target_commit  {"sha", "dirty", "subject"} of the target repo at INDEX
                    time (what the graph describes — HEAD may have moved since).
                    ``None`` for non-git targets or pre-field JSON.
    """

    decls: dict[str, set[str]]
    leaf_edges: dict[tuple[str, str], int]
    multi_decl_types: set[str]
    all_folders: set[str]
    file_records: list[dict]
    type_owners: dict[str, list[str]]
    raw_owners: dict[str, set[str]]
    pair_types: dict[tuple[str, str], set[str]] | None = None
    type_kinds: dict[str, str] = field(default_factory=dict)
    file_edges: list[dict] = field(default_factory=list)
    type_edges: list[dict] = field(default_factory=list)
    target_commit: dict | None = None
