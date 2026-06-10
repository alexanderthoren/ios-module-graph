"""Tests for modgraph.module_graph — collapse folders to build units."""
import unittest

from modgraph.module_graph import compute_module_graph, module_of


class ModuleOf(unittest.TestCase):
    def test_app_target_when_no_prefix_matches(self):
        self.assertEqual(module_of("Fever/Features/Checkout", ["Pkg/Sources"]), "app")

    def test_sources_prefix_resolves_to_target_dir(self):
        # …/Sources/<Target>/<anything> → …/Sources/<Target>
        self.assertEqual(
            module_of("Pkg/Sources/Core/Impl/Deep", ["Pkg/Sources"]),
            "Pkg/Sources/Core",
        )

    def test_longest_prefix_wins(self):
        self.assertEqual(
            module_of("A/B/Sources/X/Y", ["A/B/Sources", "A"]),
            "A/B/Sources/X",
        )

    def test_non_sources_prefix_is_itself_the_module(self):
        # A package detected without a Sources/ layout: the prefix is the module.
        self.assertEqual(module_of("Legacy/Pkg/Foo", ["Legacy/Pkg"]), "Legacy/Pkg")


def _toy_graph():
    # app target + two SPM targets. App depends on Core; Core depends on Util.
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
    return compute_module_graph(folders, leaf_edges, ["Pkg/Sources"], decls)


class CollapseToModules(unittest.TestCase):
    def setUp(self):
        self.g = _toy_graph()
        self.by_id = {n["id"]: n for n in self.g["nodes"]}

    def test_three_build_units(self):
        self.assertEqual(set(self.by_id), {"app", "Pkg/Sources/Core", "Pkg/Sources/Util"})

    def test_app_node_kind_and_label(self):
        self.assertEqual(self.by_id["app"]["kind"], "app")
        self.assertEqual(self.by_id["app"]["label"], "App (xcodeproj)")
        # App is the top consumer: nothing depends on it.
        self.assertEqual(self.by_id["app"]["warm"], 0)

    def test_spm_labels_are_target_names(self):
        self.assertEqual(self.by_id["Pkg/Sources/Core"]["label"], "Core")
        self.assertEqual(self.by_id["Pkg/Sources/Util"]["label"], "Util")

    def test_warm_blast_radius(self):
        # Util is depended on (transitively) by Core and app → 2 of 3 = 66.7%.
        self.assertEqual(self.by_id["Pkg/Sources/Util"]["warm"], 2)
        self.assertEqual(self.by_id["Pkg/Sources/Util"]["warm_pct"], 66.7)
        # Core only by app.
        self.assertEqual(self.by_id["Pkg/Sources/Core"]["warm"], 1)

    def test_cold_cohorts(self):
        self.assertEqual(self.by_id["Pkg/Sources/Util"]["level"], 0)  # compiles first
        self.assertEqual(self.by_id["Pkg/Sources/Core"]["level"], 1)
        self.assertEqual(self.by_id["app"]["level"], 2)

    def test_types_aggregated_per_module(self):
        self.assertEqual(self.by_id["Pkg/Sources/Util"]["types"], 2)

    def test_edges_collapsed_and_sorted(self):
        self.assertEqual(self.g["edges"], [
            {"from": "Pkg/Sources/Core", "to": "Pkg/Sources/Util", "w": 1},
            {"from": "app", "to": "Pkg/Sources/Core", "w": 2},
        ])

    def test_deterministic(self):
        self.assertEqual(self.g, _toy_graph())

    def test_churn_defaults_to_zero_and_unflagged(self):
        self.assertEqual(self.by_id["app"]["churn"], 0)
        self.assertFalse(self.g["summary"]["churned"])


class ChurnAttachment(unittest.TestCase):
    def test_commits_counted_once_per_module(self):
        folders = {"App/Main", "Pkg/Sources/Core/Impl", "Pkg/Sources/Util"}
        leaf_edges = {("App/Main", "Pkg/Sources/Core/Impl"): 1}
        decls = {f: {"T"} for f in folders}
        # One commit touches two folders of Core's module + the app; another
        # touches only the app. A third touches a folder outside the graph —
        # it folds into app via module_of like any unprefixed path.
        commits = [
            {"Pkg/Sources/Core/Impl", "Pkg/Sources/Core/Other", "App/Main"},
            {"App/Main"},
            {"Gone/Deleted"},
        ]
        g = compute_module_graph(folders, leaf_edges, ["Pkg/Sources"], decls,
                                 churn_commits=commits)
        by_id = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(by_id["Pkg/Sources/Core"]["churn"], 1)
        self.assertEqual(by_id["app"]["churn"], 3)
        self.assertEqual(by_id["Pkg/Sources/Util"]["churn"], 0)
        self.assertTrue(g["summary"]["churned"])


if __name__ == "__main__":
    unittest.main()
