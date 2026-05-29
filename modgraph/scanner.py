"""Regex-scanner fallback: build the folder graph straight from source text.

Used only when --from-index is NOT supplied. Produces phantom edges on
same-named types across folders — the index path (index_loader) exists to
avoid exactly that. Prefer the index path.
"""
from __future__ import annotations

import fnmatch
import sys
from collections import defaultdict
from pathlib import Path

from .config import (
    BLOCK_COMMENT_RE,
    DECL_RE,
    DEFAULT_SKIP_NAMES,
    EXT_SKIPS,
    LINE_COMMENT_RE,
    REF_RE,
    STRING_RE,
    TEST_DIR_NAMES,
)
from .models import GraphData


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


def scan(root: Path, include_tests: bool, ignore_patterns: list[str], ext: str) -> GraphData:
    """Regex-scan ``root`` and return a :class:`~modgraph.models.GraphData`.

    Edges are derived by type-name matching, so a reference to ``Foo`` produces
    an edge to *every* folder declaring a ``Foo`` (phantom edges on collisions).
    ``pair_types``/``type_kinds``/``file_edges``/``type_edges`` are left at their
    GraphData defaults — only the index path can resolve those accurately.
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
    return GraphData(
        decls=folder_decls,
        leaf_edges=leaf_edges,
        multi_decl_types=multi_decl_types,
        all_folders=all_folders,
        file_records=file_records,
        type_owners=type_owners,
        raw_owners=dict(owners),
    )
