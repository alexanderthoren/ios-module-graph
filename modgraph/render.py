"""Render the interactive HTML graph by injecting the payload into the template."""
from __future__ import annotations

import json
from pathlib import Path

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "template.html"
_VIS_NETWORK_PATH = (
    Path(__file__).resolve().parent / "templates" / "vendor" / "vis-network.min.js"
)
_GRAPH_LOGIC_PATH = Path(__file__).resolve().parent / "templates" / "graph_logic.js"


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


def _load_graph_logic() -> str:
    """Read the extracted pure-JS graph/format helpers inlined into the output.

    Kept in its own file (not embedded in template.html) so Node can unit-test
    it (tests/js/graph_logic.test.js); inlined here so the rendered graph stays
    a single self-contained file. It is our own code with no ``</script`` /
    placeholder sequences, so it inlines verbatim.
    """
    return _GRAPH_LOGIC_PATH.read_text(encoding="utf-8")


def _json_for_script(payload) -> str:
    """Serialize ``payload`` to JSON safe to inline inside an HTML ``<script>``.

    The template embeds the payload as ``const DATA = …(__PAYLOAD__)`` inside a
    ``<script>`` block. A bare ``json.dumps`` is unsafe there: if any string in
    the payload (a folder name, type name, symbol, …) contains ``</script`` —
    or even just ``</`` — the HTML parser would close the script element early
    and corrupt the page. ``<`` / ``>`` / ``&`` only ever occur inside JSON
    string values (structural JSON has none), so escaping them to their
    ``\\uXXXX`` form keeps the JSON identical to a JS parser while making it
    inert to the HTML parser. Compact separators: the default ``", "``/``": "``
    spaces are pure dead weight at payload scale.
    """
    return (
        json.dumps(payload, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _intern_payload(payload: dict) -> dict:
    """String-intern the payload's heaviest sections to shrink the output file.

    ``edges``/``files``/``file_edges``/``type_edges`` repeat the same folder
    names, type names, file paths, and symbols thousands of times — on a large
    project they are ~70% of the HTML. Each unique string is stored once in a
    ``strings`` table and the records become flat arrays of indices:

        edges       [src, dst, w]
        files       [folder, name, [decls…], [refs…], [[ref_owner pair]…]]
        file_edges  [src, dst, w, [symbols…]]
        type_edges  [src, dst, w, [symbols…], src_file, dst_file]

    The exact mirror lives in ``graph_logic.js`` ``decodePayload`` — the UI
    decodes once at load and every consumer sees the original object shapes.
    Change one side, change the other (the parity test in
    ``tests/test_payload_parity.py`` proves the round-trip). Table order is
    first-seen over already-deterministic inputs, so output stays byte-stable.
    """
    table: dict[str, int] = {}

    def intern(s: str) -> int:
        if s not in table:
            table[s] = len(table)
        return table[s]

    enc = dict(payload)
    enc["edges"] = [
        [intern(e["src"]), intern(e["dst"]), e["w"]] for e in payload["edges"]
    ]
    enc["files"] = [
        [intern(f["folder"]), intern(f["name"]),
         [intern(d) for d in f["decls"]],
         [intern(r) for r in f["refs"]],
         [[intern(p) for p in pair] for pair in f.get("ref_owners", [])]]
        for f in payload["files"]
    ]
    enc["file_edges"] = [
        [intern(e["src"]), intern(e["dst"]), e["w"],
         [intern(s) for s in e["symbols"]]]
        for e in payload["file_edges"]
    ]
    enc["type_edges"] = [
        [intern(e["src"]), intern(e["dst"]), e["w"],
         [intern(s) for s in e["symbols"]],
         intern(e["src_file"]), intern(e["dst_file"])]
        for e in payload["type_edges"]
    ]
    enc["strings"] = list(table)
    return enc


def _escape_html_text(s: str) -> str:
    """Escape a string for HTML *element text* content.

    ``__ROOT_LABEL__`` is replaced verbatim into element text (the ``<title>``
    and the ``#projectLabel`` div), not into the JSON payload — so unlike the
    payload it isn't protected by ``_json_for_script``. A project label
    containing ``<`` / ``>`` / ``&`` (e.g. ``A<B``) would otherwise corrupt the
    markup or inject elements. ``&`` is escaped first so the others' entities
    aren't double-escaped.
    """
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_html(tree, leaf_edges, multi_decl_types, file_records, type_owners,
                plan, stuck, root_label, root_path, initial_migrated,
                migrated_prefixes, out_path, type_kinds=None,
                initial_excluded=None, excluded_file=None,
                folder_package=None, packages=None, file_edges=None, type_edges=None,
                divisions=None, module_graph=None, recommendations=None,
                history=None, resources=None, quick_wins=None):
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
        # folder id -> bundle-resource names directly inside it (xibs, asset
        # catalogs, .strings, …) — the migration prompts list what a step must
        # move besides code. See modgraph/resources.py.
        "resources": resources or {},
        # ROI-ranked extract-today candidates: cut-sets with fix advice and the
        # auto-picked absorb-into-existing destination per folder. See
        # modgraph/quick_wins.py. {"items": [...], "summary": {...}}.
        "quick_wins": quick_wins or {"items": [], "summary": {}},
    }
    html = (
        _load_template()
        .replace("__VIS_NETWORK_JS__", _load_vis_network())
        .replace("__GRAPH_LOGIC_JS__", _load_graph_logic())
        .replace("__PAYLOAD__", _json_for_script(_intern_payload(payload)))
        .replace("__ROOT_LABEL__", _escape_html_text(root_label))
    )
    out_path.write_text(html, encoding="utf-8")
