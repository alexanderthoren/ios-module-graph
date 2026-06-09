"""Render the interactive HTML graph by injecting the payload into the template."""
from __future__ import annotations

import json
from pathlib import Path

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "template.html"
_VIS_NETWORK_PATH = (
    Path(__file__).resolve().parent / "templates" / "vendor" / "vis-network.min.js"
)


def _load_template() -> str:
    """Read the HTML template shipped alongside this module."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def _load_vis_network() -> str:
    """Read the vendored vis-network UMD bundle inlined into every output.

    Vendored (not loaded from a CDN) so the generated graph is a self-contained
    single file that works offline and from ``file://`` — the whole point of the
    artifact. The bundle is verified to contain no ``</script`` / placeholder
    sequences, so it is safe to inline verbatim into the ``<script>`` block.
    """
    return _VIS_NETWORK_PATH.read_text(encoding="utf-8")


def _json_for_script(payload) -> str:
    """Serialize ``payload`` to JSON safe to inline inside an HTML ``<script>``.

    The template embeds the payload as ``const DATA = __PAYLOAD__;`` inside a
    ``<script>`` block. A bare ``json.dumps`` is unsafe there: if any string in
    the payload (a folder name, type name, symbol, …) contains ``</script`` —
    or even just ``</`` — the HTML parser would close the script element early
    and corrupt the page. ``<`` / ``>`` / ``&`` only ever occur inside JSON
    string values (structural JSON has none), so escaping them to their
    ``\\uXXXX`` form keeps the JSON identical to a JS parser while making it
    inert to the HTML parser.
    """
    return (
        json.dumps(payload)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_html(tree, leaf_edges, multi_decl_types, file_records, type_owners,
                plan, stuck, root_label, root_path, initial_migrated,
                migrated_prefixes, out_path, type_kinds=None,
                initial_excluded=None, excluded_file=None,
                folder_package=None, packages=None, file_edges=None, type_edges=None,
                divisions=None, module_graph=None, recommendations=None,
                history=None):
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
        # Module-level build graph (SPM targets + app target) scored for warm
        # blast radius + cold critical path — powers "Build" mode.
        # {"nodes": [...], "edges": [...], "summary": {...}}.
        "module_graph": module_graph or {"nodes": [], "edges": [], "summary": {}},
        # Modules ranked by the build-time payoff of separating them (Build mode's
        # Recommendations tab). {"items": [...], "summary": {...}}.
        "recommendations": recommendations or {"items": [], "summary": {}},
        # Build-cost snapshots over time (oldest→newest), one per real change,
        # keyed to the target project's git commit — powers Build mode's
        # "Improvements" tab. See modgraph/history.py for the row shape.
        "history": history or [],
    }
    html = (
        _load_template()
        .replace("__VIS_NETWORK_JS__", _load_vis_network())
        .replace("__PAYLOAD__", _json_for_script(payload))
        .replace("__ROOT_LABEL__", root_label)
    )
    out_path.write_text(html, encoding="utf-8")
