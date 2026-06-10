"""Tests for modgraph.build_recommendations — rank modules by split payoff."""
import unittest

from modgraph.build_recommendations import compute_split_recommendations
from modgraph.module_graph import compute_module_graph


def _recs(divisions=None):
    # app target + two SPM targets: app → Core → Util (Util is foundational).
    folders = {"App/Main", "Pkg/Sources/Core/Impl", "Pkg/Sources/Util"}
    leaf_edges = {
        ("App/Main", "Pkg/Sources/Core/Impl"): 2,
        ("Pkg/Sources/Core/Impl", "Pkg/Sources/Util"): 1,
    }
    decls = {
        "App/Main": {"AppDelegate"},
        "Pkg/Sources/Core/Impl": {"CoreService"},
        "Pkg/Sources/Util": {"UtilA", "UtilB"},
    }
    mg = compute_module_graph(folders, leaf_edges, ["Pkg/Sources"], decls)
    return compute_split_recommendations(mg, divisions)


class Ranking(unittest.TestCase):
    def setUp(self):
        self.r = _recs()
        self.items = self.r["items"]
        self.by_id = {i["id"]: i for i in self.items}

    def test_ordered_by_combined_payoff(self):
        # Util (depended on by all + foundational) > Core > app.
        self.assertEqual([i["id"] for i in self.items],
                         ["Pkg/Sources/Util", "Pkg/Sources/Core", "app"])

    def test_util_is_top_with_full_score(self):
        util = self.by_id["Pkg/Sources/Util"]
        self.assertEqual(util["combined"], 100.0)        # tops both warm and cold
        self.assertEqual(util["downstream_cost"], 2)     # Core(1) + app(1) recompile
        self.assertEqual(util["dependents"], 2)

    def test_app_has_no_warm_payoff_but_cold(self):
        app = self.by_id["app"]
        self.assertEqual(app["warm_score"], 0)           # nothing depends on the app
        self.assertGreater(app["cold_score"], 0)         # but it's on the critical path
        self.assertEqual(app["kind"], "app")

    def test_app_action_points_to_migration(self):
        self.assertIn("Extract features", self.by_id["app"]["action"])

    def test_summary_counts(self):
        self.assertEqual(self.r["summary"]["modules"], 3)
        self.assertEqual(self.r["summary"]["actionable"], 3)


class Dividability(unittest.TestCase):
    def test_dividable_action_when_plan_present(self):
        divisions = {"Pkg/Sources/Util": {"unit_count": 3, "total_public": 4}}
        r = _recs(divisions)
        util = next(i for i in r["items"] if i["id"] == "Pkg/Sources/Util")
        self.assertTrue(util["dividable"])
        self.assertEqual(util["n_subunits"], 3)
        self.assertIn("Split into ~3", util["action"])

    def test_non_dividable_suggests_stabilizing(self):
        util = next(i for i in _recs()["items"] if i["id"] == "Pkg/Sources/Util")
        self.assertFalse(util["dividable"])
        self.assertIn("Stabilize", util["action"])


class ChurnWeighting(unittest.TestCase):
    """With churn data, the ranking key becomes hot = combined × churn share —
    'foundational but never touched' sinks below 'touched weekly'."""

    def _recs_with_churn(self, commits):
        folders = {"App/Main", "Pkg/Sources/Core/Impl", "Pkg/Sources/Util"}
        leaf_edges = {
            ("App/Main", "Pkg/Sources/Core/Impl"): 2,
            ("Pkg/Sources/Core/Impl", "Pkg/Sources/Util"): 1,
        }
        decls = {
            "App/Main": {"AppDelegate"},
            "Pkg/Sources/Core/Impl": {"CoreService"},
            "Pkg/Sources/Util": {"UtilA", "UtilB"},
        }
        mg = compute_module_graph(folders, leaf_edges, ["Pkg/Sources"], decls,
                                  churn_commits=commits)
        return compute_split_recommendations(mg)

    def test_untouched_foundational_module_sinks(self):
        # Core is edited weekly; Util (structural top) was never touched.
        commits = [{"Pkg/Sources/Core/Impl"}] * 5
        items = self._recs_with_churn(commits)["items"]
        self.assertEqual(items[0]["id"], "Pkg/Sources/Core")
        util = next(i for i in items if i["id"] == "Pkg/Sources/Util")
        self.assertEqual(util["hot"], 0.0)
        self.assertEqual(util["churn"], 0)

    def test_hot_is_none_without_churn_data(self):
        items = _recs()["items"]
        self.assertTrue(all(i["hot"] is None for i in items))
        self.assertFalse(_recs()["summary"]["churned"])

    def test_summary_flags_churn(self):
        r = self._recs_with_churn([{"App/Main"}])
        self.assertTrue(r["summary"]["churned"])


class Determinism(unittest.TestCase):
    def test_repeated_runs_identical(self):
        self.assertEqual(_recs(), _recs())


if __name__ == "__main__":
    unittest.main()
