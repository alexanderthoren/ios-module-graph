"""Load the resolved graph emitted by the index_graph Swift tool.

This is the accurate path: every reference was resolved by USR against the
compiler index store, so edges never come from name collisions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .models import GraphData


def load_index_graph(json_path: Path) -> GraphData:
    """Load the resolved folder dependency graph emitted by the index_graph Swift
    tool and return it as a :class:`~modgraph.models.GraphData`, including a
    resolved ``pair_types``.

    The Swift tool resolved every reference by USR against the compiler index
    store, so leaf_edges/pair_types are exact — a reference to a type named `Foo`
    points at the one folder that actually declares the bound `Foo`, never at all
    folders that happen to declare a same-named type. Because ``pair_types`` is
    populated, the caller skips the by-name ``compute_pair_types()``.

    ``file_edges`` (list of {"src", "dst", "w", "symbols"}) surfaces file-to-file
    couplings the type-only folder graph hides (e.g. a computed property declared
    in file A and used by file B). ``type_edges`` adds containing-file info.
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

    file_edges = [
        {"src": fe["src"], "dst": fe["dst"], "w": fe["w"],
         "symbols": list(fe.get("symbols", []))}
        for fe in data.get("file_edges", [])
    ]
    type_edges = [
        {"src": te["src"], "dst": te["dst"], "w": te["w"],
         "symbols": list(te.get("symbols", [])),
         "src_file": te.get("src_file", ""), "dst_file": te.get("dst_file", "")}
        for te in data.get("type_edges", [])
    ]

    print(f"  {len(leaf_edges)} edge(s), {len(all_folders)} folder(s), "
          f"{sum(len(v) for v in decls.values())} type decl(s), "
          f"{len(file_edges)} file edge(s), {len(type_edges)} type edge(s)", file=sys.stderr)
    return GraphData(
        decls=decls,
        leaf_edges=leaf_edges,
        multi_decl_types=multi_decl_types,
        all_folders=all_folders,
        file_records=file_records,
        type_owners=type_owners,
        raw_owners=raw_owners,
        pair_types=pair_types,
        type_kinds=type_kinds,
        file_edges=file_edges,
        type_edges=type_edges,
    )
