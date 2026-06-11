"""Tests for modgraph.master_plan — Setup + the one shaped, verifiable plan."""
from __future__ import annotations

import json
import unittest

from modgraph.master_plan import compute_master_plan


def _qw(folder, payoff, *, extractable=True, action="new_module", dest=None,
        cut_edges=None, roi=1.0, effort=10, crit=False, churn=2, warm=4,
        files=3):
    cut_edges = cut_edges or []
    return {
        "folder": folder, "roi": roi, "payoff": payoff, "effort": effort,
        "files": files, "public": 1, "churn": churn, "warm": warm, "level": 0,
        "crit": crit, "landing_level": 1, "pinned_by": None,
        "extractable_now": extractable,
        "action": action if extractable else "cut_first",
        "destination": dest, "rejected": [],
        "cut": {"edges": cut_edges,
                "total_refs": sum(e["refs"] for e in cut_edges)},
    }


def _iso(module, *, share, typ="Hub", roi=6.0):
    return {
        "module": module, "type_total": 10, "ext_total": 20,
        "candidates": [{
            "type": typ, "folder": module, "kind": "class",
            "ext_refs": 11, "int_refs": 4, "total_refs": 15,
            "ext_modules": 3, "module_size": 2, "module_public": 1,
            "module_ext_refs": 12, "module_ext_modules": 3, "roi": roi,
            "pulls_total": 1, "pulls": [],
        }],
        "summary": {"candidates": 1, "ext_total": 20, "top_share": share},
    }


def _reco(mid, combined, *, dividable, n_subunits=0, split_public=0):
    return {
        "id": mid, "label": mid, "kind": "spm", "churn": 0,
        "cost": 10.0, "cost_human": "10 types", "dependents": 2,
        "downstream_cost": 120.0, "downstream_human": "120 types",
        "crit": False, "warm_score": 120.0, "cold_score": 0, "measured": False,
        "dividable": dividable, "n_subunits": n_subunits,
        "split_public": split_public, "warm_norm": 0.0, "cold_norm": 0.0,
        "combined": combined, "hot": None, "action": "x", "reason": "y",
    }


def _inputs():
    """One scenario covering every step kind and both extraction shapes."""
    quick_wins = {"items": [
        # Feat: two consumer folders -> api_impl. Top payoff (tail floor 4.0).
        _qw("Feat", 40.0),
        # Solo: one consumer, low churn, off-path -> single_module.
        _qw("Solo", 30.0),
        # Crit: one consumer but on the cold critical path -> api_impl.
        _qw("Crit", 25.0, crit=True),
        # Abs: the quick-win absorb pick survives -> absorb.
        _qw("Abs", 20.0, action="absorb",
            dest={"module": "P", "label": "P", "refs": 5, "uses": 3,
                  "used_by": 2, "level": 1}),
    ], "summary": {"churned": False}}
    file_moves = {"items": [
        {"file": "Solo/F.swift", "from": "Solo", "to": "Home", "refs": 4,
         "runner_up_refs": 1, "own_refs": 1, "symbols": ["T1"]},
    ], "summary": {}}
    isolations = {"T": _iso("T", share=70)}
    module_splits = {"items": [
        {"module": "R", "label": "R", "level": 2, "min_intrinsic": 0,
         "spread": 2, "low_units": ["R/Core"], "units": [], "consumers": 3,
         "releasable": ["P"], "public_cost": 4, "public_sample": [],
         "score": 1.0},
    ], "summary": {"available": True}}
    recommendations = {"items": [_reco("R", 80.0, dividable=True,
                                       n_subunits=3, split_public=5)],
                       "summary": {}}
    module_graph = {
        "nodes": [
            {"id": "app", "kind": "app", "label": "App (xcodeproj)",
             "types": 100, "warm": 0, "build_ms": 0, "measured": False},
            {"id": "P", "kind": "spm", "label": "P", "types": 30, "warm": 1},
            {"id": "Q", "kind": "spm", "label": "Q", "types": 4, "warm": 2},
            {"id": "R", "kind": "spm", "label": "R", "types": 40, "warm": 1},
            {"id": "T", "kind": "spm", "label": "T", "types": 20, "warm": 1},
        ],
        "edges": [
            {"from": "app", "to": "P", "w": 5},
            {"from": "app", "to": "R", "w": 5},
            {"from": "app", "to": "T", "w": 5},
            {"from": "P", "to": "Q", "w": 3},
        ],
        "summary": {"n_cycles": 0, "measured": False},
    }
    return (quick_wins, file_moves, isolations, module_splits,
            recommendations, module_graph)


def _kwargs():
    return dict(
        pair_types={
            ("Home", "Feat"): {"FeatVM"},
            ("Profile", "Feat"): {"FeatModel"},
            ("Home", "Solo"): {"S1"},
            ("Home", "Crit"): {"C1"},
        },
        type_kinds={"FeatVM": "class", "FeatModel": "struct",
                    "S1": "struct", "C1": "class"},
        leaf_edges={
            ("Home", "Feat"): 3, ("Profile", "Feat"): 2,
            ("Home", "Solo"): 2, ("Home", "Crit"): 1,
            ("Home", "Abs"): 1, ("Abs", "P/Lib"): 2,
        },
        migrated_prefixes=["P"],
        decls={"Feat": {"FeatVM", "FeatModel", "FeatInt"}, "Solo": {"S1"},
               "Crit": {"C1"}, "Abs": {"A1"}, "Home": {"H1", "H2"}},
        resources={"Feat": ["Feat.xcassets"]},
        history=None,
        excluded_count=0,
    )


class MasterPlanTest(unittest.TestCase):
    def setUp(self):
        self.plan = compute_master_plan(*_inputs(), **_kwargs())
        self.steps = self.plan["steps"]
        self.by_id = {s["id"]: s for s in self.steps}

    def test_phases_are_monotonic(self):
        phases = [s["phase"] for s in self.steps]
        self.assertEqual(phases, sorted(phases))

    def test_move_file_shape(self):
        s = self.by_id["move:Solo/F.swift"]
        self.assertEqual(s["shape"]["mode"], "move_file")
        self.assertEqual(s["shape"]["destination"], "Home")
        self.assertEqual(s["what"]["files"], 1)

    def test_two_consumers_make_api_impl_pair(self):
        s = self.by_id["qw:Feat"]
        self.assertEqual(s["shape"]["mode"], "api_impl")
        self.assertEqual(s["shape"]["api_module"], "FeatAPI")
        self.assertEqual(s["shape"]["impl_module"], "Feat")
        self.assertEqual(s["shape"]["consumers"], 2)

    def test_api_surface_lists_cross_module_types(self):
        s = self.by_id["qw:Feat"]
        self.assertEqual(s["shape"]["api_surface"], ["FeatModel", "FeatVM"])
        self.assertEqual(s["shape"]["api_surface_count"], 2)

    def test_protocols_only_for_reference_types_with_behavior(self):
        self.assertEqual(self.by_id["qw:Feat"]["shape"]["protocols_for"],
                         ["FeatVM"])

    def test_single_consumer_makes_single_module(self):
        s = self.by_id["qw:Solo"]
        self.assertEqual(s["shape"]["mode"], "single_module")
        self.assertIn("one consumer", s["shape"]["rule"])
        self.assertIsNone(s["shape"]["api_module"])

    def test_critical_path_forces_api_impl(self):
        s = self.by_id["qw:Crit"]
        self.assertEqual(s["shape"]["mode"], "api_impl")
        self.assertIn("critical path", s["shape"]["rule"])

    def test_absorb_keeps_quick_win_destination(self):
        s = self.by_id["qw:Abs"]
        self.assertEqual(s["shape"]["mode"], "absorb")
        self.assertEqual(s["shape"]["destination"], "P")

    def test_what_counts_files_types_resources(self):
        s = self.by_id["qw:Feat"]
        self.assertEqual(s["what"]["files"], 3)
        self.assertEqual(s["what"]["types"], 3)
        self.assertEqual(s["what"]["resources"], ["Feat.xcassets"])

    def test_api_impl_simulation_adds_two_modules(self):
        before, after = (int(x) for x in
                         self.by_id["qw:Feat"]["verify"]["expect"]["modules"]
                         .split(" → "))
        self.assertEqual(after - before, 2)

    def test_single_module_simulation_adds_one(self):
        before, after = (int(x) for x in
                         self.by_id["qw:Solo"]["verify"]["expect"]["modules"]
                         .split(" → "))
        self.assertEqual(after - before, 1)

    def test_absorb_simulation_adds_none(self):
        before, after = (int(x) for x in
                         self.by_id["qw:Abs"]["verify"]["expect"]["modules"]
                         .split(" → "))
        self.assertEqual(after, before)

    def test_extraction_expect_carries_crit_and_app_types(self):
        e = self.by_id["qw:Feat"]["verify"]["expect"]
        self.assertIn("(simulated)", e["crit_len"])
        self.assertEqual(e["app_types"], "−3")

    def test_every_step_has_verify_commands(self):
        for s in self.steps:
            self.assertTrue(s["verify"]["commands"])

    def test_isolate_step_shape(self):
        s = self.by_id["mod:T"]
        self.assertEqual(s["shape"]["mode"], "isolate")
        self.assertEqual(s["shape"]["consumers"], 3)
        self.assertEqual(s["what"]["types"], 2)

    def test_split_step_shape(self):
        s = self.by_id["mod:R"]
        self.assertEqual(s["shape"]["mode"], "split")
        self.assertEqual(s["what"]["types"], 40)

    def test_join_step_shape(self):
        s = self.by_id["join:Q"]
        self.assertEqual(s["shape"]["mode"], "join")
        self.assertEqual(s["shape"]["destination"], "P")

    def test_setup_detects_missing_api_convention(self):
        ids = {i["id"] for i in self.plan["setup"]}
        self.assertIn("setup:api_convention", ids)
        self.assertIn("setup:composition_root", ids)
        self.assertIn("setup:ratchet", ids)
        self.assertIn("setup:baseline", ids)
        self.assertNotIn("setup:packages", ids)
        self.assertNotIn("setup:exclusions", ids)

    def test_setup_packages_when_no_spm(self):
        qw, fm, iso, ms, reco, _mg = _inputs()
        mg = {"nodes": [{"id": "app", "kind": "app", "label": "A",
                         "types": 10}], "edges": [], "summary": {}}
        plan = compute_master_plan(qw, fm, iso, ms, reco, mg, **_kwargs())
        self.assertIn("setup:packages", {i["id"] for i in plan["setup"]})

    def test_setup_api_items_dropped_when_api_module_exists(self):
        qw, fm, iso, ms, reco, mg = _inputs()
        mg["nodes"].append({"id": "PA", "kind": "spm", "label": "PAPI",
                            "types": 2, "warm": 0})
        plan = compute_master_plan(qw, fm, iso, ms, reco, mg, **_kwargs())
        ids = {i["id"] for i in plan["setup"]}
        self.assertNotIn("setup:api_convention", ids)
        self.assertNotIn("setup:composition_root", ids)

    def test_setup_exclusions_item_when_excluded(self):
        kw = _kwargs()
        kw["excluded_count"] = 2
        plan = compute_master_plan(*_inputs(), **kw)
        self.assertIn("setup:exclusions", {i["id"] for i in plan["setup"]})

    def test_equilibrium_criteria_ids(self):
        ids = [c["id"] for c in self.plan["equilibrium"]["criteria"]]
        self.assertEqual(ids, ["app_thin", "warm_bounded", "no_cycles",
                               "api_coverage", "work_exhausted"])

    def test_equilibrium_not_met_while_actions_remain(self):
        crits = {c["id"]: c for c in self.plan["equilibrium"]["criteria"]}
        self.assertFalse(crits["work_exhausted"]["met"])
        self.assertFalse(self.plan["equilibrium"]["met"])

    def test_api_coverage_unmet_for_multi_consumer_module(self):
        qw, fm, iso, ms, reco, mg = _inputs()
        mg["edges"].append({"from": "R", "to": "P", "w": 1})  # P: app + R
        plan = compute_master_plan(qw, fm, iso, ms, reco, mg, **_kwargs())
        cov = next(c for c in plan["equilibrium"]["criteria"]
                   if c["id"] == "api_coverage")
        self.assertFalse(cov["met"])
        self.assertIn("P", cov["current"])

    def test_scan_path_degrades_to_single_module(self):
        kw = _kwargs()
        kw["pair_types"] = None
        plan = compute_master_plan(*_inputs(), **kw)
        s = next(x for x in plan["steps"] if x["id"] == "qw:Feat")
        self.assertEqual(s["shape"]["mode"], "single_module")
        self.assertIn("regex-scan", s["shape"]["rule"])
        self.assertEqual(s["shape"]["api_surface"], [])

    def test_summary_shapes_histogram_covers_steps(self):
        self.assertEqual(sum(self.plan["summary"]["shapes"].values()),
                         len(self.steps))

    def test_deferred_passthrough_with_reasons(self):
        for d in self.plan["deferred"]:
            self.assertIn("reason", d)
            self.assertIn("why", d)

    def test_deterministic(self):
        a = json.dumps(compute_master_plan(*_inputs(), **_kwargs()),
                       sort_keys=True)
        b = json.dumps(compute_master_plan(*_inputs(), **_kwargs()),
                       sort_keys=True)
        self.assertEqual(a, b)

    def test_empty_inputs(self):
        plan = compute_master_plan({}, {}, {}, {}, {}, {})
        self.assertEqual(plan["steps"], [])
        self.assertFalse(plan["equilibrium"]["met"])
        ids = {i["id"] for i in plan["setup"]}
        self.assertIn("setup:packages", ids)
        self.assertIn("setup:ratchet", ids)

    def test_payload_is_json_serializable(self):
        json.dumps(self.plan)


if __name__ == "__main__":
    unittest.main()
