"""Tests for the type-isolation engine (modgraph/isolate.py).

Asserts against the shared "Mod" isolate fixture: one clean hub (Hub, drags
nothing), one glue type whose extraction drags a third of the module (Glue), and
an internal-only type that must NOT surface as a seed (Helper).
"""
import unittest

from modgraph.isolate import (compute_isolation_targets, compute_isolations,
                              isolatable_modules)

from tests import fixtures


class ComputeIsolationTargetsTest(unittest.TestCase):
    def setUp(self):
        (self.prefix, self.type_edges, self.type_kinds,
         self.migrated, self.decls) = fixtures.isolate_inputs()

    def _run(self):
        return compute_isolation_targets(
            self.prefix, self.type_edges, self.type_kinds, self.migrated
        )

    def _by_name(self, analysis):
        return {c["type"]: c for c in analysis["candidates"]}

    def test_only_externally_referenced_types_are_seeds(self):
        # Helper is referenced only from inside the module -> not extractable.
        cands = self._by_name(self._run())
        self.assertEqual(set(cands), {"Hub", "Glue"})
        self.assertNotIn("Helper", cands)

    def test_ranked_by_total_reference_mass(self):
        # Hub: ext 11 + int 4 = 15; Glue: ext 4 + int 0 = 4. Most-used first.
        names = [c["type"] for c in self._run()["candidates"]]
        self.assertEqual(names, ["Hub", "Glue"])

    def test_reference_mass_split_inside_outside(self):
        hub = self._by_name(self._run())["Hub"]
        self.assertEqual(hub["ext_refs"], 11)
        self.assertEqual(hub["int_refs"], 4)
        self.assertEqual(hub["total_refs"], 15)

    def test_modules_freed_counts_distinct_build_units(self):
        # Hub is referenced from two SPM modules + the app target = 3 freed.
        hub = self._by_name(self._run())["Hub"]
        self.assertEqual(hub["ext_modules"], 3)

    def test_clean_hub_drags_nothing(self):
        hub = self._by_name(self._run())["Hub"]
        self.assertEqual(hub["module_size"], 1)
        self.assertEqual(hub["pulls_total"], 0)
        self.assertEqual(hub["pulls"], [])

    def test_glue_extraction_drags_its_closure(self):
        # Glue -> Hub and Glue -> Helper, so the new module is {Glue, Hub, Helper}.
        glue = self._by_name(self._run())["Glue"]
        self.assertEqual(glue["module_size"], 3)
        self.assertEqual(glue["pulls_total"], 2)
        pulled = {p["type"] for p in glue["pulls"]}
        self.assertEqual(pulled, {"Hub", "Helper"})

    def test_glue_drag_is_ordered_by_external_fan_in(self):
        # Hub (ext 11) drags ahead of Helper (ext 0).
        glue = self._by_name(self._run())["Glue"]
        self.assertEqual([p["type"] for p in glue["pulls"]], ["Hub", "Helper"])

    def test_closure_aggregates_external_reference_mass(self):
        # Glue's module carries Glue's 4 + Hub's 11 external refs.
        glue = self._by_name(self._run())["Glue"]
        self.assertEqual(glue["module_ext_refs"], 15)
        # Freed modules are the union over the whole closure (Hub's 3).
        self.assertEqual(glue["ext_modules"], 3)

    def test_public_surface_counts_members_used_from_outside(self):
        # In Glue's module {Glue, Hub, Helper}: Glue & Hub are referenced from
        # outside the module; Helper only from Glue (inside) -> not public.
        glue = self._by_name(self._run())["Glue"]
        self.assertEqual(glue["module_public"], 2)

    def test_roi_rewards_small_clean_extractions(self):
        cands = self._by_name(self._run())
        # Hub: 11 ext over module (1) + public (1) = 5.5.
        self.assertEqual(cands["Hub"]["roi"], 5.5)
        # Glue: 15 ext over module (3) + public (2) = 3.0 — bigger, lower ROI.
        self.assertEqual(cands["Glue"]["roi"], 3.0)

    def test_summary_concentration(self):
        analysis = self._run()
        # ext_total = Hub 11 + Glue 4 = 15; top (Hub) carries 11/15 = 73%.
        self.assertEqual(analysis["ext_total"], 15)
        self.assertEqual(analysis["summary"]["top_share"], 73)
        self.assertEqual(analysis["summary"]["candidates"], 2)

    def test_min_ext_refs_gate(self):
        # Raise the bar above Glue's 4 -> only Hub survives.
        a = compute_isolation_targets(
            self.prefix, self.type_edges, self.type_kinds, self.migrated,
            min_ext_refs=5,
        )
        self.assertEqual([c["type"] for c in a["candidates"]], ["Hub"])

    def test_deterministic(self):
        self.assertEqual(self._run(), self._run())


class ComputeIsolationsTest(unittest.TestCase):
    def setUp(self):
        (_p, self.type_edges, self.type_kinds,
         self.migrated, self.decls) = fixtures.isolate_inputs()

    def test_offers_only_dividable_modules_with_candidates(self):
        out = compute_isolations(
            self.decls, self.type_edges, self.type_kinds, self.migrated
        )
        # "Mod" clears the dividable gate (3 units, 15 types) and has candidates.
        self.assertEqual(set(out), {"Mod"})
        self.assertEqual([c["type"] for c in out["Mod"]["candidates"]],
                         ["Hub", "Glue"])

    def test_empty_without_type_edges(self):
        # Regex-scan fallback path: no type_edges -> nothing to isolate.
        out = compute_isolations(self.decls, [], self.type_kinds, self.migrated)
        self.assertEqual(out, {})


class IsolatableModulesTest(unittest.TestCase):
    def test_spm_targets_and_app_subtrees_in_aggregators_out(self):
        # Two SPM targets under one package's Sources, plus an app subtree big
        # enough to be dividable. The aggregating ancestors (Packages,
        # Packages/Lib/Sources) must NOT appear — they span >1 compile unit.
        decls = {
            "Packages/Lib/Sources/Core": {f"C{i}" for i in range(8)},
            "Packages/Lib/Sources/Net": {f"N{i}" for i in range(8)},
            "App/Containers/A": {f"A{i}" for i in range(6)},
            "App/Containers/B": {f"B{i}" for i in range(6)},
            "App/Containers/C": {f"D{i}" for i in range(6)},
        }
        migrated = ["Packages/Lib/Sources"]
        mods = isolatable_modules(decls, migrated)
        self.assertIn("Packages/Lib/Sources/Core", mods)   # real SPM target
        self.assertIn("Packages/Lib/Sources/Net", mods)    # real SPM target
        self.assertIn("App/Containers", mods)              # dividable app subtree
        self.assertNotIn("Packages", mods)                 # aggregator — excluded
        self.assertNotIn("Packages/Lib/Sources", mods)     # aggregator — excluded


if __name__ == "__main__":
    unittest.main()
