"""Per-folder bundle-resource inventory for the migration prompts.

Moving a folder into an SPM module moves more than ``.swift`` files: xibs,
storyboards, asset catalogs, and localization files must move too, be declared
in ``Package.swift``, and have their ``Bundle.main`` lookups switched to
``Bundle.module`` — the classic way an "it compiles" migration breaks at
runtime. The graph itself stays type-driven; this inventory only feeds the
UI's prompt generators so each migration step lists the resources it drags
along.

``collect_resources`` walks the target project once (same skip rules as the
scanner) and returns ``{folder_id: [resource names]}`` with the scanner's
folder-id scheme (path relative to root, ``"."`` for the root itself). Bundle
*directories* (``.xcassets``, ``.lproj``, ``.bundle``, ``.scnassets``) are
recorded as a single entry and never descended into.
"""
from __future__ import annotations

import os
from pathlib import Path

from .scanner import should_skip_dir

# Loose files that ship in a module's resource bundle.
RESOURCE_FILE_EXTS = {
    ".xib", ".storyboard",
    ".strings", ".stringsdict",
    ".ttf", ".otf",
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".svg", ".heic",
    ".mp3", ".wav", ".mp4",
}
# Directories that are themselves one opaque resource.
RESOURCE_DIR_EXTS = {".xcassets", ".lproj", ".bundle", ".scnassets"}


def collect_resources(
    root: Path,
    include_tests: bool = False,
    ignore_patterns: list[str] | None = None,
) -> dict[str, list[str]]:
    """Map each folder id to the sorted resource names directly inside it.

    Folders without resources are absent (the common case — keeps the payload
    small). Resource *directories* count as one name and are not entered.
    """
    root = Path(root)
    ignore_patterns = ignore_patterns or []
    out: dict[str, list[str]] = {}

    def folder_id(directory: Path) -> str:
        rel = os.path.relpath(directory, root)
        return "." if rel == "." else rel.replace(os.sep, "/")

    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        fid = folder_id(directory)
        names: list[str] = []

        kept_dirs = []
        for d in sorted(dirnames):
            sub = directory / d
            if any(d.endswith(ext) for ext in RESOURCE_DIR_EXTS):
                names.append(d)                 # one opaque resource, don't enter
                continue
            rel = folder_id(sub)
            if should_skip_dir(sub, rel, include_tests, ignore_patterns):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs                 # prune the walk in place

        for f in sorted(filenames):
            if any(f.endswith(ext) for ext in RESOURCE_FILE_EXTS):
                names.append(f)

        if names:
            out[fid] = sorted(names)
    return out
