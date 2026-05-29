"""SPM package mapping + auto-detection of already-migrated prefixes."""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from .scanner import should_skip_dir


def _package_label(prefix: str) -> str:
    """Human-friendly label for an SPM-prefix path.

    Drops a *trailing* ``Sources`` segment, otherwise uses the last segment:
    ``Modules/Foo/Sources`` → ``Foo``, ``Modules/Foo/Sources/Bar`` → ``Bar``,
    bare ``Sources`` → ``Sources``, ``.`` → ``.``.
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
    # Sort the input set so folder_pkg's key order is deterministic (the payload
    # ships this dict; an unsorted set would reorder it per PYTHONHASHSEED).
    for f in sorted(all_source_folders):
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
