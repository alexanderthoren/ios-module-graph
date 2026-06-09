"""Tests for modgraph.index_loader: load_index_graph parses the JSON the
index_graph Swift tool emits into a GraphData.

Follows the exemplar (tests/test_graph.py): build inputs from tests.fixtures,
call the function, assert on concrete values, one behaviour per test method.
All file IO happens inside a tempfile.TemporaryDirectory — never the repo root.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modgraph import index_loader
from modgraph.models import GraphData
from tests import fixtures


class LoadIndexGraphFullPayloadTest(unittest.TestCase):
    """The complete toy payload parses into the expected GraphData."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = fixtures.write_index_json(self.dir)
        self.gd = index_loader.load_index_graph(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_graphdata(self):
        self.assertIsInstance(self.gd, GraphData)

    def test_leaf_edges_count(self):
        # The fixture declares exactly 5 edges.
        self.assertEqual(len(self.gd.leaf_edges), 5)

    def test_leaf_edges_keyed_by_tuple_with_weights(self):
        self.assertEqual(self.gd.leaf_edges[("App", "Feature")], 2)
        self.assertEqual(self.gd.leaf_edges[("App", "Core")], 1)
        self.assertEqual(self.gd.leaf_edges[("Feature", "Core")], 3)
        self.assertEqual(self.gd.leaf_edges[("Core", "Util")], 1)
        self.assertEqual(self.gd.leaf_edges[("Util", "Core")], 2)

    def test_leaf_edges_match_fixture_helper(self):
        self.assertEqual(self.gd.leaf_edges, fixtures.leaf_edges())

    def test_multi_decl_types_is_shared_only(self):
        # "Shared" is declared in both Feature and Util; nothing else is.
        self.assertEqual(self.gd.multi_decl_types, {"Shared"})

    def test_decls_are_sets(self):
        self.assertEqual(self.gd.decls["Feature"], {"FeatureView", "Shared"})
        self.assertIsInstance(self.gd.decls["Feature"], set)

    def test_type_owners_are_sorted_lists(self):
        self.assertEqual(self.gd.type_owners["Shared"], ["Feature", "Util"])
        self.assertIsInstance(self.gd.type_owners["Shared"], list)
        self.assertEqual(self.gd.type_owners["AppCoordinator"], ["App"])

    def test_raw_owners_are_sets(self):
        self.assertEqual(self.gd.raw_owners["Shared"], {"Feature", "Util"})
        self.assertIsInstance(self.gd.raw_owners["Shared"], set)

    def test_type_kinds_populated(self):
        self.assertEqual(self.gd.type_kinds["AppCoordinator"], "class")
        self.assertEqual(self.gd.type_kinds["FeatureView"], "struct")
        self.assertEqual(self.gd.type_kinds["UtilHelper"], "enum")
        self.assertEqual(self.gd.type_kinds["Shared"], "struct")

    def test_pair_types_resolved_as_dict_of_sets(self):
        self.assertEqual(self.gd.pair_types[("App", "Feature")], {"FeatureView"})
        self.assertEqual(self.gd.pair_types[("App", "Core")], {"CoreService"})
        self.assertEqual(self.gd.pair_types[("Feature", "Core")], {"CoreService"})
        self.assertEqual(self.gd.pair_types[("Core", "Util")], {"UtilHelper"})
        self.assertEqual(self.gd.pair_types[("Util", "Core")], {"CoreService"})
        self.assertIsInstance(self.gd.pair_types[("App", "Feature")], set)

    def test_pair_types_match_fixture_helper(self):
        self.assertEqual(self.gd.pair_types, fixtures.pair_types())

    def test_all_folders_has_all_four(self):
        self.assertEqual(self.gd.all_folders, {"App", "Feature", "Core", "Util"})

    def test_file_records_carry_ref_owners(self):
        app = next(r for r in self.gd.file_records if r["folder"] == "App")
        self.assertEqual(app["name"], "AppCoordinator.swift")
        self.assertEqual(app["decls"], ["AppCoordinator"])
        self.assertEqual(app["refs"], ["FeatureView", "CoreService"])
        # ref_owners is parsed as a list of lists (one list per ref).
        self.assertEqual(app["ref_owners"], [["Feature"], ["Core"]])

    def test_file_records_count(self):
        self.assertEqual(len(self.gd.file_records), 4)

    def test_file_edges_parsed(self):
        self.assertEqual(len(self.gd.file_edges), 1)
        fe = self.gd.file_edges[0]
        self.assertEqual(fe["src"], "App/AppCoordinator.swift")
        self.assertEqual(fe["dst"], "Core/CoreService.swift")
        self.assertEqual(fe["w"], 1)
        self.assertEqual(fe["symbols"], ["CoreService"])

    def test_type_edges_parsed(self):
        self.assertEqual(len(self.gd.type_edges), 1)
        te = self.gd.type_edges[0]
        self.assertEqual(te["src"], "AppCoordinator")
        self.assertEqual(te["dst"], "CoreService")
        self.assertEqual(te["w"], 1)
        self.assertEqual(te["symbols"], ["CoreService"])
        self.assertEqual(te["src_file"], "App/AppCoordinator.swift")
        self.assertEqual(te["dst_file"], "Core/CoreService.swift")


class LoadIndexGraphMinimalPayloadTest(unittest.TestCase):
    """A minimal payload lacking the optional keys still loads, defaulting
    the index-only extras to empty."""

    def _minimal_dict(self) -> dict:
        # Only the keys load_index_graph indexes unconditionally:
        # folder_decls, files, edges, type_owners, pair_types.
        # Omits the optional: type_kinds, file_edges, type_edges, and per-file
        # ref_owners.
        return {
            "schema_version": index_loader.INDEX_SCHEMA_VERSION,
            "folder_decls": {"Core": ["CoreService"], "Util": ["UtilHelper"]},
            "files": [
                {"folder": "Core", "name": "CoreService.swift",
                 "decls": ["CoreService"], "refs": ["UtilHelper"]},
            ],
            "edges": [{"src": "Core", "dst": "Util", "w": 1}],
            "type_owners": {"CoreService": ["Core"], "UtilHelper": ["Util"]},
            "pair_types": [{"src": "Core", "dst": "Util", "types": ["UtilHelper"]}],
        }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = fixtures.write_index_json(self.dir, self._minimal_dict())
        self.gd = index_loader.load_index_graph(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_loads_without_error(self):
        self.assertIsInstance(self.gd, GraphData)

    def test_type_kinds_defaults_empty(self):
        self.assertEqual(self.gd.type_kinds, {})

    def test_file_edges_defaults_empty(self):
        self.assertEqual(self.gd.file_edges, [])

    def test_type_edges_defaults_empty(self):
        self.assertEqual(self.gd.type_edges, [])

    def test_missing_ref_owners_defaults_empty(self):
        rec = self.gd.file_records[0]
        self.assertEqual(rec["ref_owners"], [])

    def test_core_payload_still_parsed(self):
        self.assertEqual(self.gd.leaf_edges, {("Core", "Util"): 1})
        self.assertEqual(self.gd.pair_types, {("Core", "Util"): {"UtilHelper"}})
        self.assertEqual(self.gd.all_folders, {"Core", "Util"})
        self.assertEqual(self.gd.multi_decl_types, set())


class LoadIndexGraphSchemaVersionTest(unittest.TestCase):
    """The loader hard-fails (clearly) when the JSON's schema_version is absent
    or does not match what it understands — guarding the Swift⇄Python contract."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_matching_version_loads(self):
        # The sample fixture is stamped with the current version → loads fine.
        gd = index_loader.load_index_graph(fixtures.write_index_json(self.dir))
        self.assertIsInstance(gd, GraphData)

    def test_missing_version_raises(self):
        data = fixtures.sample_index_dict()
        del data["schema_version"]
        path = fixtures.write_index_json(self.dir, data)
        with self.assertRaises(index_loader.IndexSchemaError) as cm:
            index_loader.load_index_graph(path)
        self.assertIn("pre-versioning", str(cm.exception))

    def test_mismatched_version_raises(self):
        data = fixtures.sample_index_dict()
        data["schema_version"] = index_loader.INDEX_SCHEMA_VERSION + 1
        path = fixtures.write_index_json(self.dir, data)
        with self.assertRaises(index_loader.IndexSchemaError) as cm:
            index_loader.load_index_graph(path)
        msg = str(cm.exception)
        self.assertIn(str(index_loader.INDEX_SCHEMA_VERSION), msg)
        self.assertIn("out of sync", msg)


class LoadIndexGraphFolderDiscoveryTest(unittest.TestCase):
    """all_folders is the union of declarers, edge endpoints, and file folders."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_edge_endpoint_only_folder_included(self):
        # "Ghost" appears only as an edge dst — no decls, no files — but must
        # still surface in all_folders.
        data = {
            "schema_version": index_loader.INDEX_SCHEMA_VERSION,
            "folder_decls": {"App": ["AppType"]},
            "files": [{"folder": "App", "name": "A.swift",
                       "decls": ["AppType"], "refs": []}],
            "edges": [{"src": "App", "dst": "Ghost", "w": 1}],
            "type_owners": {"AppType": ["App"]},
            "pair_types": [],
        }
        path = fixtures.write_index_json(self.dir, data)
        gd = index_loader.load_index_graph(path)
        self.assertIn("Ghost", gd.all_folders)
        self.assertEqual(gd.all_folders, {"App", "Ghost"})

    def test_file_only_folder_included(self):
        # A folder that only owns a file (no decls entry, no edges) is included.
        data = {
            "schema_version": index_loader.INDEX_SCHEMA_VERSION,
            "folder_decls": {"App": ["AppType"]},
            "files": [
                {"folder": "App", "name": "A.swift",
                 "decls": ["AppType"], "refs": []},
                {"folder": "Orphan", "name": "O.swift",
                 "decls": [], "refs": []},
            ],
            "edges": [],
            "type_owners": {"AppType": ["App"]},
            "pair_types": [],
        }
        path = fixtures.write_index_json(self.dir, data)
        gd = index_loader.load_index_graph(path)
        self.assertIn("Orphan", gd.all_folders)


if __name__ == "__main__":
    unittest.main()
