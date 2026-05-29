"""Tests for modgraph.cycles: feedback-arc-set + cycle breakers + extraction targets.

Follows the shape of tests/test_graph.py: build inputs from tests.fixtures (the
Core⇄Util toy cycle) or inline (a hand-built 3-cycle), call the function, assert
on concrete values, one behaviour per test method.

Notes on observed module behaviour (asserted below):
  * compute_cycle_breakers / compute_extraction_targets docstrings say they
    "return a list", but the code returns a dict / a list respectively. Tests
    assert the real return shapes.
  * total_fas_estimate is the full FAS length, independent of how many breaker
    edges actually get applied (a single edge can break a 3-cycle even when the
    greedy FAS contains two), and independent of max_steps.
"""
from __future__ import annotations

import unittest

from modgraph import cycles
from tests import fixtures


def _toy_internal() -> dict[tuple[str, str], int]:
    """Internal edges of the {Core, Util} cycle from the toy fixture."""
    le = fixtures.leaf_edges()
    members = {"Core", "Util"}
    return {
        (a, b): w
        for (a, b), w in le.items()
        if a in members and b in members and a != b
    }


# A hand-built 3-cycle A->B->C->A, lightest edge is C->A.
THREE_CYCLE_EDGES: dict[tuple[str, str], int] = {
    ("A", "B"): 5,
    ("B", "C"): 5,
    ("C", "A"): 1,
}
# An extra edge from outside the cycle, to prove members filtering.
THREE_CYCLE_EDGES_WITH_OUTSIDE = dict(THREE_CYCLE_EDGES)
THREE_CYCLE_EDGES_WITH_OUTSIDE[("X", "A")] = 9
THREE_CYCLE_PAIR_TYPES: dict[tuple[str, str], set[str]] = {
    ("A", "B"): {"TypeAB1", "TypeAB2"},
    ("B", "C"): {"TypeBC"},
    ("C", "A"): {"TypeCA"},
}


class FeedbackArcSetTest(unittest.TestCase):
    def test_two_cycle_breaks_on_lightest_edge(self):
        # Core⇄Util: Core->Util weighs 1, Util->Core weighs 2; the lighter one
        # is the single feedback arc.
        fas = cycles._feedback_arc_set({"Core", "Util"}, _toy_internal())
        self.assertEqual(fas, [("Core", "Util", 1)])

    def test_three_cycle_returns_arcs(self):
        fas = cycles._feedback_arc_set(set("ABC"), THREE_CYCLE_EDGES)
        # The greedy linearisation puts the lightest reversed edge first.
        self.assertEqual(fas[0], ("C", "A", 1))
        # Every returned arc is a real edge with its real weight.
        for a, b, w in fas:
            self.assertIn((a, b), THREE_CYCLE_EDGES)
            self.assertEqual(THREE_CYCLE_EDGES[(a, b)], w)

    def test_weight_sorted_lightest_first(self):
        members = {"P", "Q", "R", "S"}
        internal = {
            ("P", "Q"): 1, ("Q", "R"): 1, ("R", "S"): 1, ("S", "P"): 1,
            ("R", "P"): 10, ("S", "Q"): 5,
        }
        fas = cycles._feedback_arc_set(members, internal)
        weights = [w for *_, w in fas]
        self.assertEqual(weights, sorted(weights))

    def test_tie_break_is_alphabetical_on_endpoints(self):
        # Equal-weight arcs sort by (weight, src, dst).
        members = {"P", "Q", "R", "S"}
        internal = {
            ("P", "Q"): 1, ("Q", "R"): 1, ("R", "S"): 1, ("S", "P"): 1,
            ("R", "P"): 10, ("S", "Q"): 5,
        }
        fas = cycles._feedback_arc_set(members, internal)
        self.assertEqual(fas, sorted(fas, key=lambda t: (t[2], t[0], t[1])))

    def test_deterministic_across_calls(self):
        a = cycles._feedback_arc_set(set("ABC"), THREE_CYCLE_EDGES)
        b = cycles._feedback_arc_set(set("ABC"), THREE_CYCLE_EDGES)
        self.assertEqual(a, b)

    def test_acyclic_internal_returns_empty_fas(self):
        # Regression: a pure chain A->B->C is acyclic, so the correct
        # Eades-Lin-Smyth FAS is EMPTY. (An earlier version appended sinks to
        # `right` and used left+right, reversing the topo order so every forward
        # edge looked like a back-edge — it wrongly returned both edges. Sinks
        # are now reversed back so they are effectively prepended.)
        fas = cycles._feedback_arc_set(set("ABC"), {("A", "B"): 1, ("B", "C"): 1})
        self.assertEqual(fas, [])

    def test_acyclic_branching_returns_empty_fas(self):
        # A small DAG with a fork/join is still fully acyclic -> empty FAS.
        edges = {("A", "B"): 1, ("A", "C"): 1, ("B", "D"): 1, ("C", "D"): 1}
        self.assertEqual(cycles._feedback_arc_set(set("ABCD"), edges), [])

    def test_no_edges_empty_fas(self):
        self.assertEqual(cycles._feedback_arc_set({"A", "B"}, {}), [])


class ComputeCycleBreakersTest(unittest.TestCase):
    def test_returns_dict_with_estimate_and_edges(self):
        result = cycles.compute_cycle_breakers(["Core", "Util"],
                                               fixtures.leaf_edges(),
                                               fixtures.pair_types())
        self.assertIsInstance(result, dict)
        self.assertEqual(set(result), {"total_fas_estimate", "edges"})
        self.assertIsInstance(result["total_fas_estimate"], int)
        self.assertIsInstance(result["edges"], list)

    def test_two_cycle_single_breaker(self):
        result = cycles.compute_cycle_breakers(["Core", "Util"],
                                               fixtures.leaf_edges(),
                                               fixtures.pair_types())
        self.assertEqual(result["total_fas_estimate"], 1)
        self.assertEqual(len(result["edges"]), 1)
        edge = result["edges"][0]
        self.assertEqual(edge["src"], "Core")
        self.assertEqual(edge["dst"], "Util")
        self.assertEqual(edge["refs"], 1)

    def test_breaker_edge_has_required_keys(self):
        result = cycles.compute_cycle_breakers(["Core", "Util"],
                                               fixtures.leaf_edges(),
                                               fixtures.pair_types())
        edge = result["edges"][0]
        for key in ("src", "dst", "refs", "types",
                    "remaining_cycles", "biggest_cycle"):
            self.assertIn(key, edge)

    def test_breaker_types_sorted_from_pair_types(self):
        # Core->Util references UtilHelper per the fixture pair_types.
        result = cycles.compute_cycle_breakers(["Core", "Util"],
                                               fixtures.leaf_edges(),
                                               fixtures.pair_types())
        self.assertEqual(result["edges"][0]["types"], ["UtilHelper"])

    def test_applying_breakers_drives_remaining_cycles_to_zero(self):
        result = cycles.compute_cycle_breakers(["Core", "Util"],
                                               fixtures.leaf_edges(),
                                               fixtures.pair_types())
        # The last applied breaker should leave no cycles remaining.
        self.assertEqual(result["edges"][-1]["remaining_cycles"], 0)
        self.assertEqual(result["edges"][-1]["biggest_cycle"], 0)

    def test_three_cycle_one_breaker_breaks_cycle(self):
        # A 3-cycle needs exactly one arc removed; the corrected FAS estimates 1,
        # and removing the single lightest arc (C->A) breaks it.
        result = cycles.compute_cycle_breakers(
            ["A", "B", "C"], THREE_CYCLE_EDGES_WITH_OUTSIDE, THREE_CYCLE_PAIR_TYPES)
        self.assertEqual(result["total_fas_estimate"], 1)
        self.assertEqual(len(result["edges"]), 1)
        edge = result["edges"][0]
        self.assertEqual((edge["src"], edge["dst"]), ("C", "A"))
        self.assertEqual(edge["refs"], 1)
        self.assertEqual(edge["types"], ["TypeCA"])
        self.assertEqual(edge["remaining_cycles"], 0)

    def test_outside_member_edges_ignored(self):
        # The X->A edge (X not a member) must not influence the breakers.
        with_outside = cycles.compute_cycle_breakers(
            ["A", "B", "C"], THREE_CYCLE_EDGES_WITH_OUTSIDE, THREE_CYCLE_PAIR_TYPES)
        without = cycles.compute_cycle_breakers(
            ["A", "B", "C"], dict(THREE_CYCLE_EDGES), THREE_CYCLE_PAIR_TYPES)
        self.assertEqual(with_outside, without)

    def test_no_internal_edges_empty(self):
        result = cycles.compute_cycle_breakers(["A", "B"], {}, {})
        self.assertEqual(result, {"total_fas_estimate": 0, "edges": []})

    def test_max_steps_zero_applies_no_breakers_but_keeps_estimate(self):
        # max_steps caps the simulation; the FAS estimate is still the full size.
        result = cycles.compute_cycle_breakers(
            ["A", "B"], {("A", "B"): 1, ("B", "A"): 1}, {}, max_steps=0)
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["total_fas_estimate"], 1)

    def test_self_edges_among_members_excluded(self):
        # a == b is filtered out before FAS, so a lone self-edge yields nothing.
        result = cycles.compute_cycle_breakers(["A"], {("A", "A"): 9}, {})
        self.assertEqual(result, {"total_fas_estimate": 0, "edges": []})


class ComputeExtractionTargetsTest(unittest.TestCase):
    def test_returns_one_dict_per_member(self):
        targets = cycles.compute_extraction_targets(["Core", "Util"],
                                                    fixtures.leaf_edges(),
                                                    fixtures.pair_types())
        self.assertEqual({t["folder"] for t in targets}, {"Core", "Util"})

    def test_target_has_required_keys(self):
        targets = cycles.compute_extraction_targets(["Core", "Util"],
                                                    fixtures.leaf_edges(),
                                                    fixtures.pair_types())
        for key in ("folder", "outgoing_count", "outgoing_refs",
                    "incoming_count", "edges"):
            self.assertIn(key, targets[0])

    def test_sorted_lightest_first(self):
        # Core has 1 outgoing ref, Util has 2 -> Core (lighter) comes first.
        targets = cycles.compute_extraction_targets(["Core", "Util"],
                                                    fixtures.leaf_edges(),
                                                    fixtures.pair_types())
        self.assertEqual([t["folder"] for t in targets], ["Core", "Util"])
        self.assertEqual(targets[0]["outgoing_refs"], 1)
        self.assertEqual(targets[1]["outgoing_refs"], 2)

    def test_counts_and_refs_for_toy_cycle(self):
        targets = cycles.compute_extraction_targets(["Core", "Util"],
                                                    fixtures.leaf_edges(),
                                                    fixtures.pair_types())
        core = next(t for t in targets if t["folder"] == "Core")
        self.assertEqual(core["outgoing_count"], 1)
        self.assertEqual(core["outgoing_refs"], 1)
        self.assertEqual(core["incoming_count"], 1)

    def test_edge_shape_and_types(self):
        targets = cycles.compute_extraction_targets(["Core", "Util"],
                                                    fixtures.leaf_edges(),
                                                    fixtures.pair_types())
        core = next(t for t in targets if t["folder"] == "Core")
        self.assertEqual(len(core["edges"]), 1)
        edge = core["edges"][0]
        self.assertEqual(set(edge), {"dst", "refs", "types"})
        self.assertEqual(edge["dst"], "Util")
        self.assertEqual(edge["refs"], 1)
        self.assertEqual(edge["types"], ["UtilHelper"])

    def test_edges_sorted_by_weight_then_dst(self):
        # One folder fanning out to several members: edges sorted (refs, dst).
        members = ["A", "B", "C", "D"]
        le = {("A", "D"): 5, ("A", "B"): 2, ("A", "C"): 2}
        targets = cycles.compute_extraction_targets(members, le, {})
        a = next(t for t in targets if t["folder"] == "A")
        self.assertEqual([(e["dst"], e["refs"]) for e in a["edges"]],
                         [("B", 2), ("C", 2), ("D", 5)])

    def test_types_capped_at_eight(self):
        many = {"T%02d" % i for i in range(12)}
        le = {("A", "B"): 1, ("B", "A"): 1}
        pt = {("A", "B"): many, ("B", "A"): {"One"}}
        targets = cycles.compute_extraction_targets(["A", "B"], le, pt)
        a = next(t for t in targets if t["folder"] == "A")
        self.assertEqual(len(a["edges"][0]["types"]), 8)
        # Capped to the alphabetically-smallest 8 (sorted then sliced).
        self.assertEqual(a["edges"][0]["types"], sorted(many)[:8])

    def test_member_with_no_outgoing_included_as_empty(self):
        # C only receives; it must still appear with zeroed counts.
        le = {("A", "B"): 1, ("B", "A"): 1}
        targets = cycles.compute_extraction_targets(["A", "B", "C"], le, {})
        c = next(t for t in targets if t["folder"] == "C")
        self.assertEqual(c["outgoing_count"], 0)
        self.assertEqual(c["outgoing_refs"], 0)
        self.assertEqual(c["incoming_count"], 0)
        self.assertEqual(c["edges"], [])

    def test_incoming_count_is_number_of_incoming_edges(self):
        # Two members both point at C -> C has incoming_count 2.
        members = ["A", "B", "C"]
        le = {("A", "C"): 1, ("B", "C"): 4, ("C", "A"): 1}
        targets = cycles.compute_extraction_targets(members, le, {})
        c = next(t for t in targets if t["folder"] == "C")
        self.assertEqual(c["incoming_count"], 2)

    def test_outside_member_edges_ignored(self):
        # X->A and A->X (X not a member) must not contribute.
        le = {("A", "B"): 1, ("B", "A"): 1, ("X", "A"): 9, ("A", "X"): 7}
        targets = cycles.compute_extraction_targets(["A", "B"], le, {})
        a = next(t for t in targets if t["folder"] == "A")
        self.assertEqual(a["outgoing_count"], 1)
        self.assertEqual(a["outgoing_refs"], 1)
        self.assertEqual([e["dst"] for e in a["edges"]], ["B"])

    def test_self_edges_excluded(self):
        le = {("A", "A"): 9, ("A", "B"): 1, ("B", "A"): 1}
        targets = cycles.compute_extraction_targets(["A", "B"], le, {})
        a = next(t for t in targets if t["folder"] == "A")
        self.assertEqual(a["outgoing_count"], 1)
        self.assertEqual([e["dst"] for e in a["edges"]], ["B"])

    def test_top_n_limits_results(self):
        members = ["A", "B", "C", "D"]
        le = {("A", "B"): 1, ("B", "C"): 1, ("C", "D"): 1, ("D", "A"): 1}
        targets = cycles.compute_extraction_targets(members, le, {}, top_n=2)
        self.assertEqual(len(targets), 2)


if __name__ == "__main__":
    unittest.main()
