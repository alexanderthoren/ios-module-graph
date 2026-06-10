"""Tests for modgraph.quick_wins — extract-today cut-sets + absorb destinations.

Toy fixture (see tests/fixtures.py): every folder has outgoing first-party
edges, so all four are `cut_first` — good for cut-set/classification asserts.
Absorb scenarios are built inline with one migrated SPM package.
"""
from __future__ import annotations

import unittest

from modgraph.graph import build_tree
from modgraph.quick_wins import compute_quick_wins
from modgraph.scoring import compute_folder_scores
from tests.fixtures import (FILE_EDGES, FILES, FOLDER_DECLS, leaf_edges,
                            pair_types, source_folders)

NO_MODULES = {"nodes": [{"id": "app", "label": "App (xcodeproj)"}], "edges": []}


def toy_quick_wins():
    decls = {f: set(t) for f, t in FOLDER_DECLS.items()}
    tree = build_tree(source_folders(), decls, root_label="Toy")
    scores = compute_folder_scores(
        tree, leaf_edges(), source_folders(), pair_types(), decls,
        [dict(f) for f in FILES])
    return compute_quick_wins(
        scores, leaf_edges(), pair_types(), leaf_edges(), source_folders(),
        [], [dict(e) for e in FILE_EDGES], NO_MODULES)


def flat_scores(folders, **overrides):
    """Minimal folder_scores for inline scenarios."""
    row = {"roi": 1.0, "combined": 10.0, "hot": None, "effort": 2,
           "files": 1, "public": 0, "churn": 0, "warm": 0}
    return {"folders": {f: dict(row, **overrides.get(f, {})) for f in folders},
            "summary": {"churned": False}}


class CutSetTest(unittest.TestCase):
    def item(self, folder):
        return next(i for i in toy_quick_wins()["items"] if i["folder"] == folder)

    def test_every_toy_folder_blocked(self):
        out = toy_quick_wins()
        self.assertEqual(out["summary"]["cut_first"], 4)
        self.assertEqual(out["summary"]["extractable_now"], 0)

    def test_cut_edges_sorted_lightest_first(self):
        app = self.item("App")
        self.assertEqual([e["dst"] for e in app["cut"]["edges"]],
                         ["Core", "Feature"])     # 1 ref before 2 refs
        self.assertEqual(app["cut"]["total_refs"], 3)

    def test_move_file_when_edge_comes_from_few_files(self):
        # FILE_EDGES: App/AppCoordinator.swift -> Core/CoreService.swift.
        edge = next(e for e in self.item("App")["cut"]["edges"]
                    if e["dst"] == "Core")
        self.assertEqual(edge["fix"], "move_file")
        self.assertEqual(edge["evidence"], ["App/AppCoordinator.swift"])

    def test_shared_primitive_when_types_widely_consumed(self):
        # CoreService is referenced from App, Feature and Util (3 consumers);
        # no file_edges feed Feature -> Core, so the shared rule fires.
        edge = self.item("Feature")["cut"]["edges"][0]
        self.assertEqual(edge["dst"], "Core")
        self.assertEqual(edge["fix"], "shared_primitive")
        self.assertEqual(edge["evidence"], ["CoreService"])

    def test_invert_is_the_fallback(self):
        # UtilHelper has a single consumer (Core) and no file-edge evidence.
        edge = self.item("Core")["cut"]["edges"][0]
        self.assertEqual(edge["dst"], "Util")
        self.assertEqual(edge["fix"], "invert")
        self.assertEqual(edge["types"], ["UtilHelper"])

    def test_no_pair_types_degrades_to_invert(self):
        decls = {f: set(t) for f, t in FOLDER_DECLS.items()}
        tree = build_tree(source_folders(), decls, root_label="Toy")
        scores = compute_folder_scores(
            tree, leaf_edges(), source_folders(), None, decls, [])
        out = compute_quick_wins(scores, leaf_edges(), None, leaf_edges(),
                                 source_folders(), [], [], NO_MODULES)
        fixes = {e["fix"] for i in out["items"] for e in i["cut"]["edges"]}
        self.assertEqual(fixes, {"invert"})


class AbsorbTest(unittest.TestCase):
    MODULES = {
        "nodes": [{"id": "app", "label": "App (xcodeproj)"},
                  {"id": "Pkg/Sources/Lib", "label": "Lib"}],
        "edges": [{"from": "app", "to": "Pkg/Sources/Lib", "w": 3}],
    }

    def test_leaf_depending_on_one_module_absorbs_there(self):
        out = compute_quick_wins(
            flat_scores(["A"]), {}, {}, {("A", "Pkg/Sources/Lib"): 3}, {"A"},
            ["Pkg/Sources"], [], self.MODULES)
        item = out["items"][0]
        self.assertTrue(item["extractable_now"])
        self.assertEqual(item["action"], "absorb")
        self.assertEqual(item["destination"]["module"], "Pkg/Sources/Lib")
        self.assertEqual(item["destination"]["label"], "Lib")
        self.assertEqual(item["destination"]["uses"], 3)

    def test_module_consuming_the_folder_is_a_destination(self):
        out = compute_quick_wins(
            flat_scores(["A"]), {}, {}, {("Pkg/Sources/Lib", "A"): 2}, {"A"},
            ["Pkg/Sources"], [], self.MODULES)
        dest = out["items"][0]["destination"]
        self.assertEqual(dest["module"], "Pkg/Sources/Lib")
        self.assertEqual(dest["used_by"], 2)

    def test_destination_creating_module_cycle_rejected(self):
        # A uses UI; Core uses A; UI already depends on Core. Absorbing A into
        # UI adds Core->UI (cycle), into Core adds Core->UI (cycle) — no fit.
        modules = {
            "nodes": [{"id": "app", "label": "App"},
                      {"id": "P/Sources/UI", "label": "UI"},
                      {"id": "P/Sources/Core", "label": "Core"}],
            "edges": [{"from": "P/Sources/UI", "to": "P/Sources/Core", "w": 1}],
        }
        edges = {("A", "P/Sources/UI"): 3, ("P/Sources/Core", "A"): 2}
        out = compute_quick_wins(flat_scores(["A"]), {}, {}, edges, {"A"},
                                 ["P/Sources"], [], modules)
        item = out["items"][0]
        self.assertIsNone(item["destination"])
        self.assertEqual(item["action"], "new_module")

    def test_highest_traffic_module_wins(self):
        modules = {
            "nodes": [{"id": "app", "label": "App"},
                      {"id": "P/Sources/X", "label": "X"},
                      {"id": "P/Sources/Y", "label": "Y"}],
            "edges": [],
        }
        edges = {("A", "P/Sources/X"): 1, ("A", "P/Sources/Y"): 4}
        out = compute_quick_wins(flat_scores(["A"]), {}, {}, edges, {"A"},
                                 ["P/Sources"], [], modules)
        self.assertEqual(out["items"][0]["destination"]["module"], "P/Sources/Y")

    def test_complexity_bounds_force_new_module(self):
        out = compute_quick_wins(
            flat_scores(["A"], A={"files": 31}), {}, {},
            {("A", "Pkg/Sources/Lib"): 3}, {"A"}, ["Pkg/Sources"], [],
            self.MODULES)
        self.assertEqual(out["items"][0]["action"], "new_module")

    def test_no_module_traffic_means_new_module(self):
        out = compute_quick_wins(flat_scores(["A"]), {}, {}, {}, {"A"},
                                 [], [], NO_MODULES)
        item = out["items"][0]
        self.assertTrue(item["extractable_now"])
        self.assertEqual(item["action"], "new_module")


class RankingTest(unittest.TestCase):
    def test_items_sorted_by_roi_desc(self):
        scores = flat_scores(["A", "B"], A={"roi": 1.0}, B={"roi": 9.0})
        out = compute_quick_wins(scores, {}, {}, {}, {"A", "B"}, [], [],
                                 NO_MODULES)
        self.assertEqual([i["folder"] for i in out["items"]], ["B", "A"])

    def test_smaller_cut_breaks_roi_ties(self):
        scores = flat_scores(["A", "B"])
        plan_edges = {("A", "B"): 5}
        out = compute_quick_wins(scores, plan_edges, {}, plan_edges, {"A", "B"},
                                 [], [], NO_MODULES)
        # Equal roi: B has no cuts, A has 5 refs to sever — B first.
        self.assertEqual([i["folder"] for i in out["items"]], ["B", "A"])

    def test_deterministic_across_calls(self):
        self.assertEqual(toy_quick_wins(), toy_quick_wins())


class SummaryTest(unittest.TestCase):
    def test_counts_consistent(self):
        out = compute_quick_wins(
            flat_scores(["A", "B"]), {("A", "B"): 1}, {},
            {("A", "B"): 1, ("B", "Pkg/Sources/Lib"): 2}, {"A", "B"},
            ["Pkg/Sources"], [], AbsorbTest.MODULES)
        s = out["summary"]
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["extractable_now"], 1)   # B
        self.assertEqual(s["absorbable"], 1)        # B -> Lib
        self.assertEqual(s["cut_first"], 1)         # A


if __name__ == "__main__":
    unittest.main()
