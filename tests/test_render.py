"""Tests for modgraph.render: template loading + HTML payload injection.

Follows the exemplar (tests/test_graph.py): build inputs from fixtures and
modgraph.graph, call render_html into a temp path, and assert concrete facts
about the produced HTML — placeholders are gone, the root label lands in the
<title>, and the embedded ``const DATA = {...};`` parses back to the expected
payload.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from modgraph import graph, render
from tests import fixtures


def _extract_data_json(html: str) -> dict:
    """Pull the ``const DATA = {...};`` line out of the rendered HTML and parse
    its JSON object (strip the ``const DATA = `` prefix and trailing ``;``)."""
    line = None
    for raw in html.splitlines():
        if raw.startswith("const DATA = "):
            line = raw
            break
    if line is None:
        raise AssertionError("no `const DATA = ...;` line found in rendered HTML")
    body = line[len("const DATA = "):]
    assert body.endswith(";"), f"DATA line not terminated by ';': {line!r}"
    return json.loads(body[:-1])


def _toy_inputs(root_label="ToyProj", out_path=None):
    """Assemble a full, realistic render_html kwargs dict from the toy fixture."""
    leaf_edges = fixtures.leaf_edges()
    folders = fixtures.source_folders()
    decls = {k: set(v) for k, v in fixtures.FOLDER_DECLS.items()}
    tree = graph.build_tree(folders, decls, root_label=root_label)
    plan, stuck = graph.compute_migration_plan(leaf_edges, folders)
    return {
        "tree": tree,
        "leaf_edges": leaf_edges,
        "multi_decl_types": {"Shared"},
        "file_records": [dict(f) for f in fixtures.FILES],
        "type_owners": dict(fixtures.TYPE_OWNERS),
        "plan": plan,
        "stuck": stuck,
        "root_label": root_label,
        "root_path": "/tmp/toy",
        "initial_migrated": [],
        "migrated_prefixes": [],
        "out_path": out_path,
        "type_kinds": dict(fixtures.TYPE_KINDS),
        "packages": [{"name": "CorePkg", "folders": ["Core", "Util"]}],
        "file_edges": [dict(e) for e in fixtures.FILE_EDGES],
        "type_edges": [dict(e) for e in fixtures.TYPE_EDGES],
    }


class LoadTemplateTest(unittest.TestCase):
    def test_returns_non_empty_string(self):
        tmpl = render._load_template()
        self.assertIsInstance(tmpl, str)
        self.assertGreater(len(tmpl), 0)

    def test_still_contains_payload_placeholder(self):
        # The on-disk template is the un-rendered source; the placeholder must
        # survive a bare load (it is only replaced inside render_html).
        self.assertIn("__PAYLOAD__", render._load_template())

    def test_contains_root_label_placeholder(self):
        self.assertIn("__ROOT_LABEL__", render._load_template())


class RenderHtmlOutputTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.out_path = self.tmpdir / "dependency_graph.html"
        self.inputs = _toy_inputs(root_label="ToyProj", out_path=self.out_path)
        render.render_html(**self.inputs)
        self.html = self.out_path.read_text(encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_output_file_created(self):
        self.assertTrue(self.out_path.exists())
        self.assertGreater(self.out_path.stat().st_size, 0)

    def test_payload_placeholder_replaced(self):
        self.assertNotIn("__PAYLOAD__", self.html)

    def test_root_label_placeholder_replaced(self):
        self.assertNotIn("__ROOT_LABEL__", self.html)

    def test_root_label_appears_in_title(self):
        m = re.search(r"<title>(.*?)</title>", self.html, re.IGNORECASE | re.DOTALL)
        self.assertIsNotNone(m, "no <title> in rendered HTML")
        self.assertIn("ToyProj", m.group(1))

    def test_data_line_parses_as_json(self):
        data = _extract_data_json(self.html)
        self.assertIsInstance(data, dict)

    def test_data_has_expected_top_level_keys(self):
        data = _extract_data_json(self.html)
        for key in ("tree", "edges", "plan", "packages", "files", "type_owners"):
            self.assertIn(key, data)


class RenderHtmlPayloadContentTest(unittest.TestCase):
    """The parsed DATA object should faithfully reflect the render inputs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.out_path = self.tmpdir / "g.html"
        self.inputs = _toy_inputs(root_label="ToyProj", out_path=self.out_path)
        render.render_html(**self.inputs)
        self.data = _extract_data_json(self.out_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_edges_serialised_as_src_dst_w_dicts(self):
        # leaf_edges is a {(src,dst): w} map; render_html flattens it to dicts.
        edges = self.data["edges"]
        self.assertEqual(len(edges), len(self.inputs["leaf_edges"]))
        for e in edges:
            self.assertEqual(set(e), {"src", "dst", "w"})
        as_map = {(e["src"], e["dst"]): e["w"] for e in edges}
        self.assertEqual(as_map, self.inputs["leaf_edges"])

    def test_multi_decl_is_count_not_collection(self):
        # render_html stores len(multi_decl_types) under key "multi_decl".
        self.assertEqual(self.data["multi_decl"], 1)

    def test_root_label_and_path_passthrough(self):
        self.assertEqual(self.data["root_label"], "ToyProj")
        self.assertEqual(self.data["root_path"], "/tmp/toy")

    def test_plan_matches_compute_migration_plan(self):
        self.assertEqual(self.data["plan"], self.inputs["plan"])

    def test_tree_root_uses_root_label(self):
        self.assertEqual(self.data["tree"][""]["name"], "ToyProj")

    def test_type_owners_passthrough(self):
        self.assertEqual(self.data["type_owners"], self.inputs["type_owners"])

    def test_packages_passthrough(self):
        self.assertEqual(self.data["packages"], self.inputs["packages"])


class RenderHtmlDefaultsTest(unittest.TestCase):
    """The many optional kwargs default to empty containers in the payload."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.out_path = self.tmpdir / "min.html"
        render.render_html(
            tree={"": {"id": "", "name": "Mini", "parent": None,
                       "children": [], "types": 0}},
            leaf_edges={},
            multi_decl_types=set(),
            file_records=[],
            type_owners={},
            plan=[],
            stuck=[],
            root_label="Mini",
            root_path="/x",
            initial_migrated=[],
            migrated_prefixes=[],
            out_path=self.out_path,
        )
        self.data = _extract_data_json(self.out_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_optional_collections_default_empty(self):
        self.assertEqual(self.data["type_kinds"], {})
        self.assertEqual(self.data["initial_excluded"], [])
        self.assertEqual(self.data["folder_package"], {})
        self.assertEqual(self.data["packages"], [])
        self.assertEqual(self.data["file_edges"], [])
        self.assertEqual(self.data["type_edges"], [])

    def test_excluded_file_name_default(self):
        # When excluded_file is None, the basename defaults to the dotfile name.
        self.assertEqual(self.data["excluded_file_name"],
                         ".modularization_excluded.json")
        self.assertEqual(self.data["excluded_file_path"], "")


class RenderHtmlExcludedFileTest(unittest.TestCase):
    """When an excluded_file path is given, name = basename, path = full str."""

    def test_excluded_file_name_is_basename(self):
        with tempfile.TemporaryDirectory() as d:
            out_path = Path(d) / "e.html"
            excluded = "/some/dir/.my_excluded.json"
            render.render_html(
                tree={},
                leaf_edges={},
                multi_decl_types=set(),
                file_records=[],
                type_owners={},
                plan=[],
                stuck=[],
                root_label="E",
                root_path="/x",
                initial_migrated=[],
                migrated_prefixes=[],
                out_path=out_path,
                excluded_file=excluded,
            )
            data = _extract_data_json(out_path.read_text(encoding="utf-8"))
            self.assertEqual(data["excluded_file_name"], ".my_excluded.json")
            self.assertEqual(data["excluded_file_path"], excluded)


class RenderHtmlScriptEscapeTest(unittest.TestCase):
    """Payload strings containing ``</script>`` (or ``<``/``>``/``&``) must not be
    able to break out of the inline ``<script>`` block. Regression for the bug
    where a bare ``json.dumps`` let a crafted folder/type/path name close the
    script element early and corrupt the page."""

    PAYLOAD = "</script><img src=x onerror=alert(1)>&amp;<b>"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_path = Path(self._tmp.name) / "x.html"
        # Smuggle the dangerous string through several payload-carried fields.
        render.render_html(
            tree={},
            leaf_edges={},
            multi_decl_types=set(),
            file_records=[{"folder": self.PAYLOAD, "name": self.PAYLOAD,
                           "decls": [self.PAYLOAD], "refs": [], "ref_owners": []}],
            type_owners={self.PAYLOAD: [self.PAYLOAD]},
            plan=[],
            stuck=[],
            root_label="Safe",
            root_path=self.PAYLOAD,
            initial_migrated=[],
            migrated_prefixes=[],
            out_path=self.out_path,
        )
        self.html = self.out_path.read_text(encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_dangerous_sequence_not_emitted_verbatim(self):
        # The unescaped breakout sequence must never reach the HTML — its ``<``
        # had to be escaped to <. (The template's own legit </script> tags
        # don't contain this suffix, so a plain substring check is safe.)
        self.assertNotIn("</script><img", self.html)
        self.assertNotIn("<img src=x onerror", self.html)

    def test_escaped_form_present(self):
        # Proof the escaping actually fired on the payload's '<'.
        self.assertIn("\\u003c", self.html)

    def test_payload_still_round_trips(self):
        # Escaping is transparent to a JSON/JS parser: the original string comes
        # back intact, so the data is unchanged — only the HTML parser is fooled.
        data = _extract_data_json(self.html)
        self.assertEqual(data["root_path"], self.PAYLOAD)
        self.assertIn(self.PAYLOAD, data["type_owners"])
        self.assertEqual(data["files"][0]["folder"], self.PAYLOAD)


class RenderHtmlSelfContainedTest(unittest.TestCase):
    """The rendered graph must be a self-contained single file — no external
    asset URLs — so it works offline and from file://. vis-network is vendored
    and inlined; the webfont CDN was dropped for the system font stack."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out_path = Path(self._tmp.name) / "sc.html"
        render.render_html(**_toy_inputs(out_path=self.out_path))
        self.html = self.out_path.read_text(encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_external_loadable_references(self):
        # A loadable external asset is a src=/href= pointing at http(s). (Bare
        # URLs inside the inlined vis-network banner/strings are harmless and
        # expected, so we match the attribute form, not any occurrence.)
        loadable = re.findall(r'(?:src|href)\s*=\s*["\']https?:', self.html)
        self.assertEqual(loadable, [],
                         f"rendered HTML still loads external assets: {loadable}")

    def test_vis_network_inlined(self):
        # The vendored UMD banner and a public symbol prove the lib is embedded.
        self.assertIn("vis-network", self.html)
        self.assertIn("__VIS_NETWORK_JS__", render._load_template())
        self.assertNotIn("__VIS_NETWORK_JS__", self.html)

    def test_vis_bundle_has_no_script_breakout(self):
        # Inlining is only safe if the bundle can't close the <script> early.
        self.assertNotIn("</script", render._load_vis_network())


if __name__ == "__main__":
    unittest.main()
