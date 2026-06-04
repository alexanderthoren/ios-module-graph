"""Tests for modgraph.build_times + the measured-cost path through the build graph."""
import json
import tempfile
import unittest
from pathlib import Path

from modgraph.build_recommendations import compute_split_recommendations
from modgraph.build_times import load_build_times, parse_duration
from modgraph.module_graph import compute_module_graph


class ParseDuration(unittest.TestCase):
    def test_units(self):
        self.assertAlmostEqual(parse_duration("12.4s"), 12.4)
        self.assertAlmostEqual(parse_duration("234ms"), 0.234)   # ms not minutes
        self.assertAlmostEqual(parse_duration("2m"), 120.0)
        self.assertAlmostEqual(parse_duration("1m2.3s"), 62.3)
        self.assertAlmostEqual(parse_duration("1h3m"), 3780.0)

    def test_empty_and_garbage(self):
        self.assertEqual(parse_duration(""), 0.0)
        self.assertEqual(parse_duration(None), 0.0)
        self.assertEqual(parse_duration("n/a"), 0.0)


class LoadBuildTimes(unittest.TestCase):
    def _write(self, data):
        d = tempfile.mkdtemp()
        p = Path(d) / "build_times.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_parses_targets(self):
        p = self._write({"build_info": {"targets": [
            {"name": "Core", "duration": "3s"},
            {"name": "App", "duration": "1m"},
        ]}})
        self.assertEqual(load_build_times(p), {"Core": 3.0, "App": 60.0})

    def test_parses_xclogparser_flatjson(self):
        # XCLogParser flatJson: target steps with numeric compilationDuration.
        p = self._write([
            {"type": "target", "title": "Build target LocalizationAPI",
             "compilationDuration": 12.4, "duration": 14.0},
            {"type": "target", "title": "Fever", "compilationDuration": 60.0},
            {"type": "target", "title": "Run pre-actions", "compilationDuration": 0},  # phase → skipped
            {"type": "detail", "title": "CompileSwift", "compilationDuration": 3.0},   # not a target
        ])
        self.assertEqual(load_build_times(p), {"LocalizationAPI": 12.4, "Fever": 60.0})

    def test_missing_file_is_empty(self):
        self.assertEqual(load_build_times("/no/such/file.json"), {})

    def test_malformed_is_empty(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "x.json"
        p.write_text("not json", encoding="utf-8")
        self.assertEqual(load_build_times(p), {})


def _graph(build_times):
    folders = {"App/Main", "Pkg/Sources/Core/Impl", "Pkg/Sources/Util"}
    leaf_edges = {
        ("App/Main", "Pkg/Sources/Core/Impl"): 2,
        ("Pkg/Sources/Core/Impl", "Pkg/Sources/Util"): 1,
    }
    decls = {"App/Main": {"AppDelegate"}, "Pkg/Sources/Core/Impl": {"CoreService"},
             "Pkg/Sources/Util": {"UtilA", "UtilB"}}
    return compute_module_graph(folders, leaf_edges, ["Pkg/Sources"], decls,
                                build_times=build_times)


class MeasuredCost(unittest.TestCase):
    def setUp(self):
        # Core target = "Core", Util = "Util", app target name = "MyApp".
        self.mg = _graph({"Core": 5.0, "Util": 2.0, "MyApp": 30.0})
        self.by_id = {n["id"]: n for n in self.mg["nodes"]}

    def test_spm_times_matched_by_label(self):
        self.assertEqual(self.by_id["Pkg/Sources/Core"]["build_ms"], 5000)
        self.assertTrue(self.by_id["Pkg/Sources/Core"]["measured"])

    def test_unmatched_target_folds_into_app(self):
        # "MyApp" matches no SPM label → folded into the app node.
        self.assertEqual(self.by_id["app"]["build_ms"], 30000)

    def test_summary_flags_measured(self):
        self.assertTrue(self.mg["summary"]["measured"])
        self.assertEqual(self.mg["summary"]["total_build_s"], 37.0)

    def test_est_wall_is_work_over_cores(self):
        # Estimated wall = total CPU work ÷ cores (the binding resource floor),
        # never larger than the summed work.
        sm = self.mg["summary"]
        self.assertGreater(sm["cores"], 0)
        self.assertEqual(sm["est_wall_s"], round(sm["total_build_s"] / sm["cores"], 1))
        self.assertLessEqual(sm["est_wall_s"], sm["total_build_s"])

    def test_est_wall_zero_without_measured_times(self):
        sm = _graph({})["summary"]
        self.assertEqual(sm["est_wall_s"], 0.0)
        self.assertGreater(sm["cores"], 0)

    def test_recommendations_cost_in_seconds(self):
        rec = compute_split_recommendations(self.mg)
        self.assertTrue(rec["summary"]["measured"])
        util = next(i for i in rec["items"] if i["id"] == "Pkg/Sources/Util")
        # downstream of Util = Core(5s) + app(30s) = 35s of build work.
        self.assertEqual(util["downstream_cost"], 35000.0)
        self.assertEqual(util["downstream_human"], "35.0s")

    def test_no_times_falls_back_to_proxy(self):
        mg = _graph({})
        self.assertFalse(mg["summary"]["measured"])
        self.assertFalse(any(n["measured"] for n in mg["nodes"]))
        rec = compute_split_recommendations(mg)
        util = next(i for i in rec["items"] if i["id"] == "Pkg/Sources/Util")
        self.assertIn("types", util["downstream_human"])


if __name__ == "__main__":
    unittest.main()
