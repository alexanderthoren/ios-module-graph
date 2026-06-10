"""Tests for modgraph.module_splits — low-level cores trapped in high modules.

Toy: module Big (L3) holds two units — CoreKit (8 types, references no other
first-party module, intrinsic L0) and SDKGlue (6 types, references Vendor at
L2, intrinsic L3). The app consumes only CoreKit (releasable); module UI
consumes SDKGlue (pinned). SDKGlue references one CoreKit type internally,
so splitting CoreKit off prices exactly that type public.
"""
from __future__ import annotations

import unittest

from modgraph.module_splits import MIN_SPREAD, compute_module_splits

BIG = "Pkg/Sources/Big"
VENDOR = "Pkg/Sources/Vendor"
UI = "Pkg/Sources/UI"
PREFIXES = ["Pkg/Sources"]


def module_graph(big_level=3):
    return {
        "nodes": [
            {"id": "app", "label": "App (xcodeproj)", "kind": "app", "level": 5},
            {"id": BIG, "label": "Big", "kind": "spm", "level": big_level},
            {"id": VENDOR, "label": "Vendor", "kind": "spm", "level": 2},
            {"id": UI, "label": "UI", "kind": "spm", "level": 4},
        ],
        "edges": [],
    }


def leaf_edges():
    return {
        (f"{BIG}/SDKGlue", VENDOR): 2,            # external dep -> intrinsic L3
        (f"{BIG}/SDKGlue", f"{BIG}/CoreKit"): 3,  # internal unit edge
        ("App/Feature", f"{BIG}/CoreKit"): 2,     # app consumes the low unit
        (UI, f"{BIG}/SDKGlue"): 1,                # UI pinned to the high unit
    }


def pair_types():
    return {
        (f"{BIG}/SDKGlue", f"{BIG}/CoreKit"): {"CoreThing"},
        # External consumption is public already — must not inflate the cost.
        ("App/Feature", f"{BIG}/CoreKit"): {"CoreThing2"},
    }


def decls():
    return {
        f"{BIG}/CoreKit": {f"C{i}" for i in range(8)},
        f"{BIG}/SDKGlue": {f"G{i}" for i in range(6)},
    }


def run(big_level=3, pt=None):
    return compute_module_splits(
        module_graph(big_level), leaf_edges(),
        pair_types() if pt is None else pt, decls(), PREFIXES)


class DetectorTest(unittest.TestCase):
    def test_trapped_core_detected(self):
        out = run()
        self.assertEqual(out["summary"]["candidates"], 1)
        item = out["items"][0]
        self.assertEqual(item["module"], BIG)
        self.assertEqual(item["label"], "Big")
        self.assertEqual(item["level"], 3)
        self.assertEqual(item["min_intrinsic"], 0)
        self.assertEqual(item["spread"], 3)
        self.assertEqual(item["low_units"], ["CoreKit"])

    def test_units_sorted_low_first_with_intrinsic_levels(self):
        units = run()["items"][0]["units"]
        self.assertEqual(
            units,
            [{"unit": "CoreKit", "types": 8, "intrinsic_level": 0},
             {"unit": "SDKGlue", "types": 6, "intrinsic_level": 3}])

    def test_releasable_consumers_touch_only_low_units(self):
        item = run()["items"][0]
        self.assertEqual(item["consumers"], 2)            # app + UI
        self.assertEqual(item["releasable"], ["App (xcodeproj)"])

    def test_public_cost_counts_internal_crossings_only(self):
        # SDKGlue -> CoreKit crosses the split (1 type); the app's external
        # consumption is public today and costs nothing extra.
        item = run()["items"][0]
        self.assertEqual(item["public_cost"], 1)
        self.assertEqual(item["public_sample"], ["CoreThing"])

    def test_score_is_releasable_times_spread_over_cost(self):
        self.assertEqual(run()["items"][0]["score"], 3.0)  # 1 * 3 / 1


class GatesTest(unittest.TestCase):
    def test_small_spread_skipped(self):
        out = run(big_level=MIN_SPREAD - 1)  # spread 1 < MIN_SPREAD
        self.assertEqual(out["items"], [])
        self.assertEqual(out["summary"]["scanned"], 3)

    def test_single_unit_module_skipped(self):
        d = {f"{BIG}/CoreKit": {f"C{i}" for i in range(20)}}
        out = compute_module_splits(module_graph(), leaf_edges(),
                                    pair_types(), d, PREFIXES)
        self.assertEqual(out["items"], [])

    def test_too_few_types_skipped(self):
        d = {f"{BIG}/CoreKit": {"C0"}, f"{BIG}/SDKGlue": {"G0"}}
        out = compute_module_splits(module_graph(), leaf_edges(),
                                    pair_types(), d, PREFIXES)
        self.assertEqual(out["items"], [])

    def test_regex_path_reports_unavailable(self):
        out = run(pt={})
        self.assertEqual(out["items"], [])
        self.assertFalse(out["summary"]["available"])


class DeterminismTest(unittest.TestCase):
    def test_repeated_runs_identical(self):
        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main()
