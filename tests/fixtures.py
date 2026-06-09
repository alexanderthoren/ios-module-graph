"""Shared test fixtures and builders.

A single, well-understood toy project is reused across the suite so assertions
read against a known graph. The shape mirrors what the ``index_graph`` Swift
tool emits and what :func:`modgraph.index_loader.load_index_graph` consumes.

Toy graph (4 folders, one 2-folder cycle):

    App ──▶ Feature ──▶ Core ◀──▶ Util
     └───────────────▶ Core

  * Core ⇄ Util form a cycle (SCC of size 2).
  * Feature depends on the cycle; App depends on Feature + the cycle.
  * Type ``Shared`` is declared in BOTH Feature and Util (a multi-decl type).

Migration order therefore starts with the {Core, Util} cycle bundle (it has no
first-party outgoing deps), then Feature, then App.
"""
from __future__ import annotations

import json
from pathlib import Path

from modgraph import index_loader

# --- folder-level declarations ---------------------------------------------
FOLDER_DECLS: dict[str, list[str]] = {
    "App": ["AppCoordinator"],
    "Feature": ["FeatureView", "Shared"],
    "Core": ["CoreService"],
    "Util": ["UtilHelper", "Shared"],
}

# type name -> declaring folders ("Shared" lives in two folders).
TYPE_OWNERS: dict[str, list[str]] = {
    "AppCoordinator": ["App"],
    "FeatureView": ["Feature"],
    "CoreService": ["Core"],
    "UtilHelper": ["Util"],
    "Shared": ["Feature", "Util"],
}

TYPE_KINDS: dict[str, str] = {
    "AppCoordinator": "class",
    "FeatureView": "struct",
    "CoreService": "class",
    "UtilHelper": "enum",
    "Shared": "struct",
}

# (src, dst) -> weight. Mirrors the arrows above.
EDGES: list[dict] = [
    {"src": "App", "dst": "Feature", "w": 2},
    {"src": "App", "dst": "Core", "w": 1},
    {"src": "Feature", "dst": "Core", "w": 3},
    {"src": "Core", "dst": "Util", "w": 1},
    {"src": "Util", "dst": "Core", "w": 2},
]

PAIR_TYPES: list[dict] = [
    {"src": "App", "dst": "Feature", "types": ["FeatureView"]},
    {"src": "App", "dst": "Core", "types": ["CoreService"]},
    {"src": "Feature", "dst": "Core", "types": ["CoreService"]},
    {"src": "Core", "dst": "Util", "types": ["UtilHelper"]},
    {"src": "Util", "dst": "Core", "types": ["CoreService"]},
]

FILES: list[dict] = [
    {"folder": "App", "name": "AppCoordinator.swift",
     "decls": ["AppCoordinator"], "refs": ["FeatureView", "CoreService"],
     "ref_owners": [["Feature"], ["Core"]]},
    {"folder": "Feature", "name": "FeatureView.swift",
     "decls": ["FeatureView", "Shared"], "refs": ["CoreService"],
     "ref_owners": [["Core"]]},
    {"folder": "Core", "name": "CoreService.swift",
     "decls": ["CoreService"], "refs": ["UtilHelper"],
     "ref_owners": [["Util"]]},
    {"folder": "Util", "name": "UtilHelper.swift",
     "decls": ["UtilHelper", "Shared"], "refs": ["CoreService"],
     "ref_owners": [["Core"]]},
]

FILE_EDGES: list[dict] = [
    {"src": "App/AppCoordinator.swift", "dst": "Core/CoreService.swift",
     "w": 1, "symbols": ["CoreService"]},
]

TYPE_EDGES: list[dict] = [
    {"src": "AppCoordinator", "dst": "CoreService", "w": 1,
     "symbols": ["CoreService"], "src_file": "App/AppCoordinator.swift",
     "dst_file": "Core/CoreService.swift"},
]


def sample_index_dict() -> dict:
    """A complete index_graph.json payload for the toy project."""
    return {
        "schema_version": index_loader.INDEX_SCHEMA_VERSION,
        "folder_decls": {k: list(v) for k, v in FOLDER_DECLS.items()},
        "files": [dict(f) for f in FILES],
        "edges": [dict(e) for e in EDGES],
        "type_owners": {k: list(v) for k, v in TYPE_OWNERS.items()},
        "type_kinds": dict(TYPE_KINDS),
        "pair_types": [dict(p) for p in PAIR_TYPES],
        "file_edges": [dict(e) for e in FILE_EDGES],
        "type_edges": [dict(e) for e in TYPE_EDGES],
    }


def write_index_json(directory: Path, data: dict | None = None) -> Path:
    """Write an index_graph.json into *directory* and return its path."""
    path = Path(directory) / "index_graph.json"
    path.write_text(json.dumps(data if data is not None else sample_index_dict()),
                    encoding="utf-8")
    return path


def leaf_edges() -> dict[tuple[str, str], int]:
    """``leaf_edges`` mapping for the toy project (as scan/load produce)."""
    return {(e["src"], e["dst"]): e["w"] for e in EDGES}


def source_folders() -> set[str]:
    return set(FOLDER_DECLS)


def pair_types() -> dict[tuple[str, str], set[str]]:
    return {(p["src"], p["dst"]): set(p["types"]) for p in PAIR_TYPES}


# --- divide fixture: one module "Lib" to be split into smaller modules -------
#
# Module "Lib" with five subfolder units. Exercises every branch of the divide
# analysis: a depended-on leaf, an unused leaf, a root-level unit, a nested
# folder that collapses into its immediate-subfolder unit, and a 2-unit cycle.
#
#     Lib            (root)  : declares LibFacade — referenced by nobody
#     Lib/Models             : ModelA (used by Core+Net), ModelB (unused)
#     Lib/Models/DTO         : ModelC — collapses into the "Models" unit
#     Lib/Core   ⇄ Lib/Net   : CoreService ⇄ NetClient (a 2-unit cycle)
#     Lib/Util               : UtilHelper — referenced by nobody (clean leaf)
#
# Public surface (types referenced from another unit): ModelA, CoreService,
# NetClient = 3. Models extracts before the {Core, Net} cycle bundle; Util and
# the root unit are clean leaves.
DIVIDE_PREFIX = "Lib"
DIVIDE_DECLS: dict[str, list[str]] = {
    "Lib": ["LibFacade"],
    "Lib/Models": ["ModelA", "ModelB"],
    "Lib/Models/DTO": ["ModelC"],
    "Lib/Core": ["CoreService"],
    "Lib/Net": ["NetClient"],
    "Lib/Util": ["UtilHelper"],
}
# (src_folder, dst_folder, weight)
DIVIDE_EDGES: list[tuple[str, str, int]] = [
    ("Lib/Core", "Lib/Net", 1),
    ("Lib/Net", "Lib/Core", 2),
    ("Lib/Core", "Lib/Models", 1),
    ("Lib/Net", "Lib/Models", 1),
]
DIVIDE_PAIR_TYPES: dict[tuple[str, str], list[str]] = {
    ("Lib/Core", "Lib/Net"): ["NetClient"],
    ("Lib/Net", "Lib/Core"): ["CoreService"],
    ("Lib/Core", "Lib/Models"): ["ModelA"],
    ("Lib/Net", "Lib/Models"): ["ModelA"],
}


def divide_inputs():
    """Return ``(prefix, leaf_edges, pair_types, decls)`` for the Lib module."""
    decls = {f: set(t) for f, t in DIVIDE_DECLS.items()}
    leaf_edges = {(a, b): w for a, b, w in DIVIDE_EDGES}
    pair_types = {k: set(v) for k, v in DIVIDE_PAIR_TYPES.items()}
    return DIVIDE_PREFIX, leaf_edges, pair_types, decls


def make_swift_tree(base: Path) -> Path:
    """Create a small on-disk Swift project under *base* for scanner tests.

    Layout:
        base/Core/CoreService.swift        (declares CoreService)
        base/Feature/FeatureView.swift     (declares FeatureView, refs CoreService)
        base/Tests/CoreTests.swift         (a test dir — skipped by default)
        base/build/Generated.swift         (a build dir — always skipped)
        base/RootType.swift                (a root-level file -> folder ".")
    """
    base = Path(base)
    (base / "Core").mkdir(parents=True, exist_ok=True)
    (base / "Feature").mkdir(parents=True, exist_ok=True)
    (base / "Tests").mkdir(parents=True, exist_ok=True)
    (base / "build").mkdir(parents=True, exist_ok=True)

    (base / "Core" / "CoreService.swift").write_text(
        "// a comment mentioning FeatureView that must NOT count\n"
        'let s = "FeatureView string literal"\n'
        "public class CoreService {}\n",
        encoding="utf-8",
    )
    (base / "Feature" / "FeatureView.swift").write_text(
        "struct FeatureView {\n  let svc = CoreService()\n}\n",
        encoding="utf-8",
    )
    (base / "Tests" / "CoreTests.swift").write_text(
        "class CoreServiceTests { let s = CoreService() }\n", encoding="utf-8",
    )
    (base / "build" / "Generated.swift").write_text(
        "enum Generated {}\n", encoding="utf-8",
    )
    (base / "RootType.swift").write_text("actor RootType {}\n", encoding="utf-8")
    return base
