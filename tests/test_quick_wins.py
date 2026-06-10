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
        # Traffic from consumers (w_from) keeps both candidates level-valid:
        # absorbing A adds no new deps to either, so the heavier one wins.
        modules = {
            "nodes": [{"id": "app", "label": "App"},
                      {"id": "P/Sources/X", "label": "X"},
                      {"id": "P/Sources/Y", "label": "Y"}],
            "edges": [],
        }
        edges = {("P/Sources/X", "A"): 1, ("P/Sources/Y", "A"): 4}
        out = compute_quick_wins(flat_scores(["A"]), {}, {}, edges, {"A"},
                                 ["P/Sources"], [], modules)
        self.assertEqual(out["items"][0]["destination"]["module"], "P/Sources/Y")

    def test_same_level_dep_pair_vetoes_both(self):
        # A uses X and Y, both L0. Absorbing into either gives it a dep on the
        # other at its own level — a genuine level raise. Both vetoed, new
        # module is the honest outcome.
        modules = {
            "nodes": [{"id": "app", "label": "App"},
                      {"id": "P/Sources/X", "label": "X", "level": 0},
                      {"id": "P/Sources/Y", "label": "Y", "level": 0}],
            "edges": [],
        }
        edges = {("A", "P/Sources/X"): 1, ("A", "P/Sources/Y"): 4}
        out = compute_quick_wins(flat_scores(["A"]), {}, {}, edges, {"A"},
                                 ["P/Sources"], [], modules)
        item = out["items"][0]
        self.assertEqual(item["action"], "new_module")
        self.assertEqual({r["reason"] for r in item["rejected"]},
                         {"raises_level"})

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


class LevelPredicateTest(unittest.TestCase):
    """Layer-inversion veto (raises_level) + the rejected report."""

    MODULES = {
        "nodes": [{"id": "app", "label": "App"},
                  {"id": "P/Sources/Low", "label": "Low", "level": 0},
                  {"id": "P/Sources/High", "label": "High", "level": 2}],
        "edges": [],
    }

    def run_case(self, edges):
        return compute_quick_wins(flat_scores(["A"]), {}, {}, edges, {"A"},
                                  ["P/Sources"], [], self.MODULES)["items"][0]

    def test_low_destination_vetoed_high_picked(self):
        # A uses Low(L0) and High(L2). Absorbing into Low would raise it to
        # L3 (gains a dep on High); High already sits above Low — fine.
        item = self.run_case({("A", "P/Sources/Low"): 5,
                              ("A", "P/Sources/High"): 1})
        self.assertEqual(item["destination"]["module"], "P/Sources/High")
        self.assertEqual(item["destination"]["level"], 2)
        self.assertEqual(len(item["rejected"]), 1)
        rej = item["rejected"][0]
        self.assertEqual(rej["module"], "P/Sources/Low")
        self.assertEqual(rej["reason"], "raises_level")
        self.assertEqual(rej["evidence"],
                         ["High is L2, destination is L0"])
        self.assertEqual(rej["refs"], 5)

    def test_single_lower_dep_is_fine(self):
        # A uses only Low; absorbing into High gains High a dep below its
        # level — no raise. But A only references Low, so Low is the only
        # candidate, and absorbing A (no other deps) into Low is also fine.
        item = self.run_case({("A", "P/Sources/Low"): 3})
        self.assertEqual(item["destination"]["module"], "P/Sources/Low")
        self.assertEqual(item["rejected"], [])

    def test_rejected_sorted_by_traffic_and_bounded(self):
        # Six same-level modules all vetoed; report keeps the 4 heaviest,
        # heaviest first.
        nodes = [{"id": "app", "label": "App"}]
        edges = {}
        for i in range(6):
            mid = f"P/Sources/M{i}"
            nodes.append({"id": mid, "label": f"M{i}", "level": 0})
            edges[("A", mid)] = i + 1
        modules = {"nodes": nodes, "edges": []}
        out = compute_quick_wins(flat_scores(["A"]), {}, {}, edges, {"A"},
                                 ["P/Sources"], [], modules)
        item = out["items"][0]
        self.assertIsNone(item["destination"])
        self.assertEqual([r["refs"] for r in item["rejected"]], [6, 5, 4, 3])


class ChurnPredicateTest(unittest.TestCase):
    """Hot folder x widely-depended-on destination veto (churn_hostile)."""

    def modules(self, warm):
        return {
            "nodes": [{"id": "app", "label": "App"},
                      {"id": "P/Sources/Wide", "label": "Wide",
                       "level": 1, "warm": warm}],
            "edges": [],
        }

    def scores(self, churn, churned):
        s = flat_scores(["A"], A={"churn": churn})
        s["summary"]["churned"] = churned
        return s

    def run_case(self, churn, churned, warm):
        return compute_quick_wins(
            self.scores(churn, churned), {}, {},
            {("A", "P/Sources/Wide"): 3}, {"A"}, ["P/Sources"], [],
            self.modules(warm))["items"][0]

    def test_hot_folder_into_wide_module_vetoed(self):
        item = self.run_case(churn=5, churned=True, warm=3)
        self.assertEqual(item["action"], "new_module")
        rej = item["rejected"][0]
        self.assertEqual(rej["reason"], "churn_hostile")
        self.assertEqual(rej["evidence"],
                         ["folder churn 5 commit(s)",
                          "destination has 3 dependent module(s)"])

    def test_cold_folder_absorbs_into_wide_module(self):
        item = self.run_case(churn=4, churned=True, warm=3)
        self.assertEqual(item["action"], "absorb")

    def test_hot_folder_into_narrow_module_absorbs(self):
        item = self.run_case(churn=9, churned=True, warm=2)
        self.assertEqual(item["action"], "absorb")

    def test_predicate_noop_without_churn_data(self):
        # churn 0 with churned=False means "no data", not "untouched" — the
        # veto must not fire on data we don't have.
        item = self.run_case(churn=0, churned=False, warm=9)
        self.assertEqual(item["action"], "absorb")


class LandingLevelTest(unittest.TestCase):
    MODULES = {
        "nodes": [{"id": "app", "label": "App"},
                  {"id": "P/Sources/Low", "label": "Low", "level": 0},
                  {"id": "P/Sources/High", "label": "High", "level": 2}],
        "edges": [],
    }

    def test_no_module_deps_lands_at_zero(self):
        out = compute_quick_wins(flat_scores(["A"]), {}, {}, {}, {"A"},
                                 ["P/Sources"], [], self.MODULES)
        self.assertEqual(out["items"][0]["landing_level"], 0)

    def test_lands_one_above_deepest_module_dep(self):
        out = compute_quick_wins(
            flat_scores(["A"]), {}, {},
            {("A", "P/Sources/Low"): 1, ("A", "P/Sources/High"): 1}, {"A"},
            ["P/Sources"], [], self.MODULES)
        self.assertEqual(out["items"][0]["landing_level"], 3)

    def test_cut_first_folders_get_a_projection_too(self):
        # A is blocked by its edge to B (cut_first), but its module deps
        # already say where it lands once the cut is fixed.
        plan_edges = {("A", "B"): 2}
        leaf = {("A", "B"): 2, ("A", "P/Sources/High"): 1}
        out = compute_quick_wins(
            flat_scores(["A", "B"]), plan_edges, {}, leaf, {"A", "B"},
            ["P/Sources"], [], self.MODULES)
        a = next(i for i in out["items"] if i["folder"] == "A")
        self.assertEqual(a["action"], "cut_first")
        self.assertEqual(a["landing_level"], 3)
        self.assertEqual(a["rejected"], [])

    def test_items_carry_current_level_and_crit(self):
        scores = flat_scores(["A"], A={"level": 4, "crit": True})
        out = compute_quick_wins(scores, {}, {}, {}, {"A"},
                                 [], [], NO_MODULES)
        self.assertEqual(out["items"][0]["level"], 4)
        self.assertTrue(out["items"][0]["crit"])

    def test_pinned_by_is_the_deepest_module_dep(self):
        out = compute_quick_wins(
            flat_scores(["A"]), {}, {},
            {("A", "P/Sources/Low"): 9, ("A", "P/Sources/High"): 1}, {"A"},
            ["P/Sources"], [], self.MODULES)
        pin = out["items"][0]["pinned_by"]
        self.assertEqual(pin, {"module": "P/Sources/High", "label": "High",
                               "level": 2})

    def test_no_module_deps_means_no_pin(self):
        out = compute_quick_wins(flat_scores(["A"]), {}, {}, {}, {"A"},
                                 ["P/Sources"], [], self.MODULES)
        self.assertIsNone(out["items"][0]["pinned_by"])

    def test_summary_carries_app_level(self):
        modules = {"nodes": [{"id": "app", "label": "App", "level": 8}],
                   "edges": []}
        out = compute_quick_wins(flat_scores(["A"]), {}, {}, {}, {"A"},
                                 [], [], modules)
        self.assertEqual(out["summary"]["app_level"], 8)


class RankingTest(unittest.TestCase):
    def test_items_sorted_by_roi_desc(self):
        scores = flat_scores(["A", "B"], A={"roi": 1.0}, B={"roi": 9.0})
        out = compute_quick_wins(scores, {}, {}, {}, {"A", "B"}, [], [],
                                 NO_MODULES)
        self.assertEqual([i["folder"] for i in out["items"]], ["B", "A"])

    def test_lower_landing_breaks_roi_ties(self):
        # Equal ROI, both extractable, no cuts: A lands at L3 (references
        # High), B lands at L0 (references nothing) — the foundation builder
        # ranks first.
        modules = {
            "nodes": [{"id": "app", "label": "App"},
                      {"id": "P/Sources/High", "label": "High", "level": 2}],
            "edges": [],
        }
        out = compute_quick_wins(
            flat_scores(["A", "B"]), {}, {},
            {("A", "P/Sources/High"): 1}, {"A", "B"},
            ["P/Sources"], [], modules)
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
