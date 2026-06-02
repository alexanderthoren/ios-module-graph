"""Tests for modgraph.divide: split one module into smaller SPM modules.

Follows tests.test_graph's shape: build inputs from tests.fixtures, call the
function, assert concrete values, one behaviour per test. The "Lib" divide
fixture (see fixtures.divide_inputs) is a depended-on leaf + an unused leaf + a
root unit + a nested folder + a 2-unit cycle, so each branch has coverage.
"""
from __future__ import annotations

import json
import unittest

from modgraph import divide
from tests import fixtures


class UnitMappingTest(unittest.TestCase):
    def test_unit_of_immediate_subfolder(self):
        self.assertEqual(divide.unit_of("Lib/Models", "Lib"), "Models")

    def test_unit_of_nested_folder_collapses_to_immediate(self):
        # A folder two levels deep maps to its immediate-subfolder unit.
        self.assertEqual(divide.unit_of("Lib/Models/DTO", "Lib"), "Models")

    def test_unit_of_module_root_is_synthetic_root(self):
        self.assertEqual(divide.unit_of("Lib", "Lib"), "(root)")


class AggregateModuleTest(unittest.TestCase):
    def setUp(self):
        self.prefix, self.le, self.pt, self.decls = fixtures.divide_inputs()

    def test_units_are_immediate_subfolders(self):
        units, _, _, _ = divide.aggregate_module(
            self.prefix, self.le, self.pt, self.decls
        )
        self.assertEqual(units, {"(root)", "Models", "Core", "Net", "Util"})

    def test_nested_folder_types_count_under_immediate_unit(self):
        _, _, _, counts = divide.aggregate_module(
            self.prefix, self.le, self.pt, self.decls
        )
        # Models (2) + Models/DTO (1) collapse into the "Models" unit.
        self.assertEqual(counts["Models"], 3)

    def test_same_unit_edges_are_dropped(self):
        # An edge between two folders in the same unit is not a boundary.
        decls = {"Lib/A/One": {"X"}, "Lib/A/Two": {"Y"}, "Lib/B": {"Z"}}
        le = {("Lib/A/One", "Lib/A/Two"): 5, ("Lib/A/One", "Lib/B"): 1}
        units, edges, _, _ = divide.aggregate_module("Lib", le, {}, decls)
        # A/One -> A/Two collapses to A->A (dropped); only A->B survives.
        self.assertEqual(edges, {("A", "B"): 1})


class PublicSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.prefix, self.le, self.pt, self.decls = fixtures.divide_inputs()
        self.div = divide.compute_division_plan(
            self.prefix, self.le, self.pt, self.decls
        )

    def _row(self, unit):
        return next(u for u in self.div["units"] if u["unit"] == unit)

    def test_total_public_surface(self):
        # ModelA + CoreService + NetClient cross unit boundaries.
        self.assertEqual(self.div["total_public"], 3)

    def test_depended_on_leaf_exposes_only_referenced_type(self):
        models = self._row("Models")
        # ModelA is referenced from Core/Net; ModelB and ModelC are not.
        self.assertEqual(models["public"], 1)
        self.assertEqual(models["public_sample"], ["ModelA"])
        self.assertEqual(models["types"], 3)
        self.assertEqual(models["public_pct"], 33)

    def test_unused_leaf_has_zero_public_surface(self):
        self.assertEqual(self._row("Util")["public"], 0)
        self.assertEqual(self._row("(root)")["public"], 0)

    def test_cycle_members_expose_their_crossing_types(self):
        self.assertEqual(self._row("Core")["public_sample"], ["CoreService"])
        self.assertEqual(self._row("Net")["public_sample"], ["NetClient"])

    def test_units_sorted_by_descending_public_then_name(self):
        order = [u["unit"] for u in self.div["units"]]
        self.assertEqual(order, ["Core", "Models", "Net", "(root)", "Util"])


class DivisionPlanTest(unittest.TestCase):
    def setUp(self):
        self.prefix, self.le, self.pt, self.decls = fixtures.divide_inputs()
        self.div = divide.compute_division_plan(
            self.prefix, self.le, self.pt, self.decls
        )

    def test_exactly_one_cycle_bundle(self):
        cycles = [s for s in self.div["plan"] if s["is_cycle"]]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["folders"], ["Core", "Net"])

    def test_depended_on_leaf_extracted_before_cycle(self):
        step_of = {tuple(s["folders"]): s["step"] for s in self.div["plan"]}
        self.assertLess(step_of[("Models",)], step_of[("Core", "Net")])

    def test_cycle_breaker_cuts_lightest_edge_first(self):
        cyc = next(s for s in self.div["plan"] if s["is_cycle"])
        self.assertEqual(cyc["fas_estimate"], 1)  # a 2-cycle needs one cut
        first = cyc["breakers"][0]
        # Core->Net (1 ref) is lighter than Net->Core (2 refs).
        self.assertEqual((first["src"], first["dst"]), ("Core", "Net"))
        self.assertEqual(first["types"], ["NetClient"])
        self.assertEqual(first["remaining_cycles"], 0)

    def test_no_stuck_units(self):
        self.assertEqual(self.div["stuck"], [])

    def test_summary_counts(self):
        self.assertEqual(self.div["unit_count"], 5)
        self.assertEqual(self.div["type_total"], 7)
        self.assertEqual(self.div["edge_count"], 4)

    def test_unit_edges_shipped_for_step_visualization(self):
        # The before/after graph needs the full inter-unit edge list, sorted and
        # carrying the crossing type names.
        edges = self.div["unit_edges"]
        self.assertEqual([(e["src"], e["dst"]) for e in edges],
                         [("Core", "Models"), ("Core", "Net"),
                          ("Net", "Core"), ("Net", "Models")])
        core_net = next(e for e in edges if e["src"] == "Core" and e["dst"] == "Net")
        self.assertEqual(core_net["w"], 1)
        self.assertEqual(core_net["types"], ["NetClient"])


class DeterminismTest(unittest.TestCase):
    def test_plan_is_byte_identical_across_runs(self):
        prefix, le, pt, decls = fixtures.divide_inputs()
        a = divide.compute_division_plan(prefix, le, pt, decls)
        b = divide.compute_division_plan(prefix, le, pt, decls)
        self.assertEqual(json.dumps(a), json.dumps(b))

    def test_dividable_modules_sorted(self):
        _, _, _, decls = fixtures.divide_inputs()
        mods = divide.dividable_modules(decls, min_units=2, min_types=3)
        self.assertEqual(mods, sorted(mods))
        self.assertIn("Lib", mods)


class DividableModulesTest(unittest.TestCase):
    def test_requires_min_units_and_min_types(self):
        _, _, _, decls = fixtures.divide_inputs()
        # Lib has 5 units / 7 types; Lib/Models has 2 units (Models root + DTO).
        mods = divide.dividable_modules(decls, min_units=2, min_types=3)
        self.assertIn("Lib", mods)
        # A high type floor excludes everything.
        self.assertEqual(divide.dividable_modules(decls, min_types=999), [])

    def test_single_unit_module_not_dividable(self):
        decls = {"Solo": {"A", "B", "C"}}
        self.assertEqual(divide.dividable_modules(decls, min_types=1), [])


if __name__ == "__main__":
    unittest.main()
