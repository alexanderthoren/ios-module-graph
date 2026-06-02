"""Render the interactive HTML graph by injecting the payload into the template."""
from __future__ import annotations

import json
from pathlib import Path

_TEMPLATE_PATH = Path(__file__).resolve().parent / "template.html"


def _load_template() -> str:
    """Read the HTML template shipped alongside this module."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def render_html(tree, leaf_edges, multi_decl_types, file_records, type_owners,
                plan, stuck, root_label, root_path, initial_migrated,
                migrated_prefixes, out_path, type_kinds=None,
                initial_excluded=None, excluded_file=None,
                folder_package=None, packages=None, file_edges=None, type_edges=None,
                divisions=None):
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
        "file_edges": file_edges or [],
        "type_edges": type_edges or [],
        # folder id -> precomputed division plan (how to split that folder into
        # smaller SPM modules). Only populated on the USR-resolved index path.
        "divisions": divisions or {},
    }
    html = _load_template().replace("__PAYLOAD__", json.dumps(payload)).replace(
        "__ROOT_LABEL__", root_label
    )
    out_path.write_text(html, encoding="utf-8")
