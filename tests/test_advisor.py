"""Tests for modgraph.advisor — the unified one-feed arbiter."""
from __future__ import annotations

import json
import unittest

from modgraph.advisor import compute_advice


def _qw(folder, payoff, *, extractable=True, action="absorb", dest=None,
        cut_edges=None, roi=1.0, effort=10, landing=1):
    cut_edges = cut_edges or []
    return {
        "folder": folder, "roi": roi, "payoff": payoff, "effort": effort,
        "files": 3, "public": 1, "churn": 2, "warm": 4, "level": 0,
        "crit": False, "landing_level": landing, "pinned_by": None,
        "extractable_now": extractable,
        "action": action if extractable else "cut_first",
        "destination": dest, "rejected": [],
        "cut": {"edges": cut_edges,
                "total_refs": sum(e["refs"] for e in cut_edges)},
    }


def _iso(module, *, share, ext_total=20, typ="Hub", roi=6.0, pulls=0):
    return {
        "module": module, "type_total": 10, "ext_total": ext_total,
        "candidates": [{
            "type": typ, "folder": module, "kind": "class",
            "ext_refs": 11, "int_refs": 4, "total_refs": 15,
            "ext_modules": 3, "module_size": 1 + pulls, "module_public": 1,
            "module_ext_refs": 12, "module_ext_modules": 3, "roi": roi,
            "pulls_total": pulls, "pulls": [],
        }],
        "summary": {"candidates": 1, "ext_total": ext_total,
                    "top_share": share},
    }


def _reco(mid, combined, *, dividable, dependents=2, crit=False,
          n_subunits=0, split_public=0, label=None):
    return {
        "id": mid, "label": label or mid, "kind": "spm", "churn": 0,
        "cost": 10.0, "cost_human": "10 types", "dependents": dependents,
        "downstream_cost": 120.0, "downstream_human": "120 types",
        "crit": crit, "warm_score": 120.0, "cold_score": 0, "measured": False,
        "dividable": dividable, "n_subunits": n_subunits,
        "split_public": split_public, "warm_norm": 0.0, "cold_norm": 0.0,
        "combined": combined, "hot": None,
        "action": "x", "reason": "y",
    }


def _inputs():
    """One shared scenario exercising every wave; see assertions for the map."""
    quick_wins = {"items": [
        # A: ready, absorbs into P. Top payoff (sets the tail floor at 4.0).
        _qw("A", 40.0, dest={"module": "P", "label": "P", "refs": 5,
                             "uses": 3, "used_by": 2, "level": 1}),
        # B: blocked by ONE move_file edge whose file wave 0 already moves.
        _qw("B", 30.0, extractable=False, cut_edges=[
            {"dst": "X", "refs": 2, "types": ["T1"], "fix": "move_file",
             "evidence": ["B/F.swift"]}]),
        # C: ready but payoff under the tail floor -> deferred.
        _qw("C", 1.0, action="new_module", dest=None),
        # D: blocked by a big inversion cut -> deferred (blocked_cut).
        _qw("D", 30.0, extractable=False, cut_edges=[
            {"dst": "Y", "refs": 50, "types": ["T2"], "fix": "invert",
             "evidence": ["T2"]}]),
        # E: blocked by a small manual cut (6 refs <= ADVISOR_CUT_MAX).
        _qw("E", 20.0, extractable=False, cut_edges=[
            {"dst": "Z", "refs": 6, "types": ["T3"], "fix": "invert",
             "evidence": ["T3"]}]),
    ], "summary": {}}
    file_moves = {"items": [
        {"file": "B/F.swift", "from": "B", "to": "X", "refs": 4,
         "runner_up_refs": 1, "own_refs": 1, "symbols": ["T1"]},
    ], "summary": {}}
    isolations = {
        "R": _iso("R", share=60),            # alternative on R's split
        "T": _iso("T", share=70, roi=5.0),   # dominant -> isolate action
        "Feat": _iso("Feat", share=55),      # app subtree, no quick win
    }
    module_splits = {"items": [
        {"module": "R", "label": "R", "level": 2, "min_intrinsic": 0,
         "spread": 2, "low_units": ["R/Core"], "units": [], "consumers": 3,
         "releasable": ["P"], "public_cost": 4, "public_sample": [],
         "score": 1.0},
    ], "summary": {"available": True}}
    recommendations = {"items": [
        _reco("R", 80.0, dividable=True, n_subunits=3, split_public=5,
              crit=True),
        _reco("U", 60.0, dividable=True, n_subunits=4, split_public=6),
        _reco("P", 40.0, dividable=False),
    ], "summary": {}}
    module_graph = {
        "nodes": [
            {"id": "app", "kind": "app", "label": "App", "types": 100},
            {"id": "P", "kind": "spm", "label": "P", "types": 30},
            {"id": "Q", "kind": "spm", "label": "Q", "types": 4},
            {"id": "R", "kind": "spm", "label": "R", "types": 40},
            {"id": "T", "kind": "spm", "label": "T", "types": 20},
            {"id": "U", "kind": "spm", "label": "U", "types": 25},
            {"id": "S", "kind": "spm", "label": "S", "types": 2},
            {"id": "V", "kind": "spm", "label": "V", "types": 3},
        ],
        "edges": [
            {"from": "app", "to": "P", "w": 5},
            {"from": "app", "to": "R", "w": 5},
            {"from": "app", "to": "T", "w": 5},
            {"from": "app", "to": "U", "w": 5},
            {"from": "app", "to": "V", "w": 1},
            {"from": "P", "to": "Q", "w": 3},
        ],
        "summary": {},
    }
    return (quick_wins, file_moves, isolations, module_splits,
            recommendations, module_graph)


class AdvisorTest(unittest.TestCase):
    def setUp(self):
        self.advice = compute_advice(*_inputs())
        self.actions = self.advice["actions"]
        self.deferred = self.advice["deferred"]
        self.by_id = {a["id"]: a for a in self.actions}

    def test_waves_are_monotonic(self):
        waves = [a["wave"] for a in self.actions]
        self.assertEqual(waves, sorted(waves))

    def test_wave0_move_first(self):
        self.assertEqual(self.actions[0]["id"], "move:B/F.swift")
        self.assertEqual(self.actions[0]["kind"], "move_file")
        self.assertEqual(self.actions[0]["wave"], 0)

    def test_wave1_ready_absorb(self):
        a = self.by_id["qw:A"]
        self.assertEqual(a["wave"], 1)
        self.assertEqual(a["kind"], "absorb")
        self.assertIn("Absorb A into P", a["title"])

    def test_wave2_unblocked_links_after_the_move(self):
        b = self.by_id["qw:B"]
        self.assertEqual(b["wave"], 2)
        self.assertEqual(b["kind"], "cut_then_extract")
        self.assertEqual(b["after"], ["move:B/F.swift"])

    def test_move_mirrors_unblocks(self):
        self.assertEqual(self.by_id["move:B/F.swift"]["unblocks"], ["B"])

    def test_wave2_small_manual_cut_included(self):
        e = self.by_id["qw:E"]
        self.assertEqual(e["wave"], 2)
        self.assertEqual(e["after"], [])
        self.assertIn("6 ref(s)", e["why"])

    def test_wave2_unblocked_sorts_before_manual_cut(self):
        ids = [a["id"] for a in self.actions if a["wave"] == 2]
        self.assertEqual(ids, ["qw:B", "qw:E"])

    def test_tail_quick_win_deferred(self):
        d = next(d for d in self.deferred if d["id"] == "qw:C")
        self.assertEqual(d["reason"], "tail")

    def test_big_cut_deferred(self):
        d = next(d for d in self.deferred if d["id"] == "qw:D")
        self.assertEqual(d["reason"], "blocked_cut")
        self.assertIn("50 ref(s)", d["why"])

    def test_split_wins_over_isolation_with_alternative(self):
        r = self.by_id["mod:R"]
        self.assertEqual(r["kind"], "split_module")
        self.assertIn("L0 core", r["title"])
        self.assertIn("isolate Hub", r["details"]["alternative"])

    def test_division_split_for_dividable_module(self):
        u = self.by_id["mod:U"]
        self.assertEqual(u["kind"], "split_module")
        self.assertIn("~4 sub-modules", u["title"])

    def test_dominant_isolation_decides_for_isolate(self):
        t = self.by_id["mod:T"]
        self.assertEqual(t["kind"], "isolate_type")
        self.assertIn("Isolate Hub", t["title"])

    def test_wave3_ordered_by_leverage(self):
        ids = [a["id"] for a in self.actions if a["wave"] == 3]
        self.assertEqual(ids[0], "mod:R")   # leverage 80
        self.assertEqual(ids[1], "mod:U")   # leverage 60

    def test_app_subtree_isolation_emitted_when_no_quick_win(self):
        feat = self.by_id["mod:Feat"]
        self.assertEqual(feat["kind"], "isolate_type")

    def test_app_subtree_isolation_skipped_when_folder_has_action(self):
        qw, fm, iso, ms, reco, mg = _inputs()
        qw["items"].append(_qw("Feat", 25.0, dest=None, action="new_module"))
        advice = compute_advice(qw, fm, iso, ms, reco, mg)
        ids = [a["id"] for a in advice["actions"]]
        self.assertIn("qw:Feat", ids)
        self.assertNotIn("mod:Feat", ids)

    def test_quick_win_mentions_isolation_alternative(self):
        qw, fm, iso, ms, reco, mg = _inputs()
        qw["items"].append(_qw("Feat", 25.0, dest=None, action="new_module"))
        advice = compute_advice(qw, fm, iso, ms, reco, mg)
        feat = next(a for a in advice["actions"] if a["id"] == "qw:Feat")
        self.assertIn("isolate Hub", feat["details"]["alternative"])

    def test_stabilize_api_deferred_for_flat_module(self):
        d = next(d for d in self.deferred if d["id"] == "mod:P")
        self.assertEqual(d["reason"], "stabilize_api")

    def test_join_tiny_single_consumer_module(self):
        q = self.by_id["join:Q"]
        self.assertEqual(q["wave"], 4)
        self.assertIn("Fold Q into P", q["title"])
        self.assertEqual(q["details"]["into"], "P")

    def test_join_skipped_when_sole_consumer_is_app(self):
        self.assertNotIn("join:V", self.by_id)
        self.assertFalse(any(d["id"] == "join:V" for d in self.deferred))

    def test_join_skipped_for_absorb_destination(self):
        qw, fm, iso, ms, reco, mg = _inputs()
        qw["items"][0]["destination"]["module"] = "Q"
        advice = compute_advice(qw, fm, iso, ms, reco, mg)
        self.assertNotIn("join:Q", [a["id"] for a in advice["actions"]])

    def test_unused_module_deferred(self):
        d = next(d for d in self.deferred if d["id"] == "join:S")
        self.assertEqual(d["reason"], "unused")

    def test_no_action_for_app_target(self):
        self.assertNotIn("mod:app", self.by_id)

    def test_summary_counts(self):
        s = self.advice["summary"]
        self.assertEqual(s["actions"], len(self.actions))
        self.assertEqual(s["deferred"], len(self.deferred))
        self.assertEqual(sum(s["waves"].values()), len(self.actions))
        self.assertIn("stop", s)

    def test_deterministic(self):
        a = json.dumps(compute_advice(*_inputs()), sort_keys=True)
        b = json.dumps(compute_advice(*_inputs()), sort_keys=True)
        self.assertEqual(a, b)

    def test_empty_inputs(self):
        advice = compute_advice({}, {}, {}, {}, {}, {})
        self.assertEqual(advice["actions"], [])
        self.assertEqual(advice["deferred"], [])
        self.assertIn("Nothing pays", advice["summary"]["stop"])

    def test_payload_is_json_serializable(self):
        json.dumps(self.advice)


if __name__ == "__main__":
    unittest.main()
