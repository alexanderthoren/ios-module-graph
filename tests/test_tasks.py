"""Tests for modgraph.tasks: flatten plan into PR-sized tasks + md/json writers.

Follows the shape of tests/test_graph.py — one behaviour per test, concrete
assertions, inputs built minimally (or from tests.fixtures where useful).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from modgraph import tasks
from tests import fixtures


# --- helpers ---------------------------------------------------------------
def singleton_step(step, folder, unlocks=None):
    """A minimal non-cycle plan step extracting one *folder*."""
    return {
        "step": step,
        "is_cycle": False,
        "folders": [folder],
        "size": 1,
        "unlocks": unlocks if unlocks is not None else [],
    }


def cycle_step(step, folders, extractions):
    """A minimal cycle plan step bundling *folders* with given *extractions*."""
    return {
        "step": step,
        "is_cycle": True,
        "folders": list(folders),
        "size": len(folders),
        "extractions": extractions,
    }


def sample_meta(**overrides):
    """A realistic meta dict matching write_task_list_* expectations."""
    meta = {
        "root_label": "ToyProj",
        "root_path": "/tmp/toyproj",
        "migrated_prefixes": ["Legacy"],
        "baseline_count": 1,
        "source_total": 5,
        "excluded_count": 0,
        "blocked_reasons": {},
        "tasks_total": 3,
    }
    meta.update(overrides)
    return meta


# --- build_task_list -------------------------------------------------------
def quick_wins_for(folder, destination):
    """Minimal quick_wins payload marking *folder* absorbable into *destination*."""
    return {"items": [{
        "folder": folder, "action": "absorb",
        "destination": {"module": destination, "label": destination.split("/")[-1],
                        "refs": 4, "uses": 4, "used_by": 0},
    }]}


class AbsorbTaskTest(unittest.TestCase):
    def test_absorbable_leaf_becomes_absorb_task(self):
        out = tasks.build_task_list(
            [singleton_step(1, "Core")], [], "Root", "/root", [], 0, 1,
            quick_wins=quick_wins_for("Core", "Pkg/Sources/Lib"))
        self.assertEqual(out[0]["type"], "absorb_into_existing")
        self.assertEqual(out[0]["destination"]["label"], "Lib")
        self.assertIn("Lib", out[0]["notes"])

    def test_leaf_without_destination_stays_extract_leaf(self):
        out = tasks.build_task_list(
            [singleton_step(1, "Core")], [], "Root", "/root", [], 0, 1,
            quick_wins={"items": [{"folder": "Core", "action": "new_module",
                                   "destination": None}]})
        self.assertEqual(out[0]["type"], "extract_leaf")
        self.assertIsNone(out[0]["destination"])

    def test_markdown_renders_destination_and_absorb_steps(self):
        out = tasks.build_task_list(
            [singleton_step(1, "Core")], [], "Root", "/root", [], 0, 1,
            quick_wins=quick_wins_for("Core", "Pkg/Sources/Lib"))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.md"
            tasks.write_task_list_markdown(out, sample_meta(), path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("**Destination:** absorb into `Lib`", text)
        self.assertIn("Move every file into `Lib`'s sources", text)
        self.assertNotIn("1. Create a new SPM target", text)


class WaveTest(unittest.TestCase):
    def _two_wave_tasks(self):
        s1 = singleton_step(1, "Core")
        s1["wave"] = 1
        s2 = singleton_step(2, "Feature")
        s2["wave"] = 2
        s3 = singleton_step(3, "Util")
        s3["wave"] = 1
        return tasks.build_task_list([s1, s2, s3], [], "Root", "/root", [], 0, 3)

    def test_tasks_copy_step_wave(self):
        out = self._two_wave_tasks()
        self.assertEqual([t["wave"] for t in out], [1, 2, 1])

    def test_markdown_groups_by_wave(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.md"
            tasks.write_task_list_markdown(self._two_wave_tasks(),
                                           sample_meta(), path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("## Wave 1 — 2 task(s), parallelizable", text)
        self.assertIn("## Wave 2 — 1 task(s), parallelizable", text)
        # Wave 1's Util (task 3) is listed before wave 2's Feature (task 2).
        self.assertLess(text.index("`Util`"), text.index("`Feature`"))
        self.assertIn("grouped into **waves**", text)

    def test_markdown_without_waves_keeps_sequential_guidance(self):
        out = tasks.build_task_list(
            [singleton_step(1, "Core")], [], "Root", "/root", [], 0, 1)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.md"
            tasks.write_task_list_markdown(out, sample_meta(), path)
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("## Wave", text)
        self.assertIn("complete them sequentially", text)


class TaskScoreFieldsTest(unittest.TestCase):
    def test_tasks_copy_step_scores(self):
        step = singleton_step(1, "Core")
        step.update({"payoff": 75.0, "effort": 3, "roi": 25.0})
        out = tasks.build_task_list([step], [], "Root", "/root", [], 0, 1)
        self.assertEqual(out[0]["roi"], 25.0)
        self.assertEqual(out[0]["payoff"], 75.0)
        self.assertEqual(out[0]["effort"], 3)

    def test_tasks_default_scores_to_none(self):
        out = tasks.build_task_list(
            [singleton_step(1, "Core")], [], "Root", "/root", [], 0, 1)
        self.assertIsNone(out[0]["roi"])

    def test_markdown_shows_roi_line_when_scored(self):
        step = singleton_step(1, "Core")
        step.update({"payoff": 75.0, "effort": 3, "roi": 25.0})
        out = tasks.build_task_list([step], [], "Root", "/root", [], 0, 1)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.md"
            tasks.write_task_list_markdown(out, sample_meta(), path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("**ROI:** 25.0 (payoff 75.0, effort 3", text)

    def test_markdown_omits_roi_line_without_scores(self):
        out = tasks.build_task_list(
            [singleton_step(1, "Core")], [], "Root", "/root", [], 0, 1)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.md"
            tasks.write_task_list_markdown(out, sample_meta(), path)
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("**ROI:**", text)


class BuildTaskListSingletonTest(unittest.TestCase):
    def setUp(self):
        self.file_records = [
            {"folder": "Core", "name": "CoreService.swift"},
            {"folder": "Core", "name": "Aux.swift"},
            {"folder": "Feature", "name": "FeatureView.swift"},
        ]

    def test_singleton_emits_one_extract_leaf_task(self):
        plan = [singleton_step(1, "Core")]
        out = tasks.build_task_list(
            plan, self.file_records, "Root", "/root", [], 0, 3)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "extract_leaf")

    def test_singleton_task_id_and_plan_step(self):
        plan = [singleton_step(7, "Core")]
        out = tasks.build_task_list(
            plan, self.file_records, "Root", "/root", [], 0, 3)
        self.assertEqual(out[0]["id"], 1)
        self.assertEqual(out[0]["plan_step"], 7)
        self.assertEqual(out[0]["folder"], "Core")

    def test_singleton_files_sorted_and_counted(self):
        plan = [singleton_step(1, "Core")]
        out = tasks.build_task_list(
            plan, self.file_records, "Root", "/root", [], 0, 3)
        # Core has two files, returned sorted.
        self.assertEqual(out[0]["files"], ["Aux.swift", "CoreService.swift"])
        self.assertEqual(out[0]["files_count"], 2)

    def test_singleton_with_no_files(self):
        plan = [singleton_step(1, "Empty")]
        out = tasks.build_task_list(
            plan, self.file_records, "Root", "/root", [], 0, 3)
        self.assertEqual(out[0]["files"], [])
        self.assertEqual(out[0]["files_count"], 0)

    def test_singleton_has_zero_refactor_edges(self):
        plan = [singleton_step(1, "Core")]
        out = tasks.build_task_list(
            plan, self.file_records, "Root", "/root", [], 0, 3)
        self.assertEqual(out[0]["refactor_edges"], [])
        self.assertEqual(out[0]["outgoing_refs_to_refactor"], 0)

    def test_singleton_unlocks_size_one_carries_folder(self):
        # Plan-step unlocks use {folders:[..], size:N}; size==1 -> carries folder.
        plan = [singleton_step(1, "Core", unlocks=[{"folders": ["Feature"], "size": 1}])]
        out = tasks.build_task_list(
            plan, self.file_records, "Root", "/root", [], 0, 3)
        self.assertEqual(out[0]["unlocks"], [{"folder": "Feature", "size": 1}])

    def test_singleton_unlocks_size_gt_one_folder_is_none(self):
        # A cycle being unlocked (size > 1) maps to folder=None, size preserved.
        plan = [singleton_step(1, "Core",
                               unlocks=[{"folders": ["X", "Y"], "size": 2}])]
        out = tasks.build_task_list(
            plan, self.file_records, "Root", "/root", [], 0, 3)
        self.assertEqual(out[0]["unlocks"], [{"folder": None, "size": 2}])

    def test_singleton_notes_mention_zero_outgoing_deps(self):
        plan = [singleton_step(1, "Core")]
        out = tasks.build_task_list(
            plan, self.file_records, "Root", "/root", [], 0, 3)
        self.assertIn("zero first-party outgoing deps", out[0]["notes"])

    def test_singleton_has_no_cycle_only_keys(self):
        # extract_leaf tasks do not carry cycle_size / incoming_consumers.
        plan = [singleton_step(1, "Core")]
        out = tasks.build_task_list(
            plan, self.file_records, "Root", "/root", [], 0, 3)
        self.assertNotIn("cycle_size", out[0])
        self.assertNotIn("incoming_consumers", out[0])


class BuildTaskListCycleTest(unittest.TestCase):
    def setUp(self):
        self.file_records = [
            {"folder": "Core", "name": "CoreService.swift"},
            {"folder": "Util", "name": "UtilHelper.swift"},
        ]
        self.step = cycle_step(
            2, ["Core", "Util"],
            extractions=[
                {
                    "folder": "Util",
                    "edges": [{"dst": "Core", "refs": 2, "types": ["CoreService"]}],
                    "outgoing_refs": 2,
                    "incoming_count": 1,
                },
                {
                    "folder": "Core",
                    "edges": [{"dst": "Util", "refs": 1, "types": ["UtilHelper"]}],
                    "outgoing_refs": 1,
                    "incoming_count": 3,
                },
            ],
        )

    def test_cycle_emits_one_task_per_extraction(self):
        out = tasks.build_task_list(
            [self.step], self.file_records, "Root", "/root", [], 0, 2)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(t["type"] == "extract_from_cycle" for t in out))

    def test_cycle_task_ids_are_sequential(self):
        out = tasks.build_task_list(
            [self.step], self.file_records, "Root", "/root", [], 0, 2)
        self.assertEqual([t["id"] for t in out], [1, 2])

    def test_cycle_tasks_preserve_extraction_order(self):
        out = tasks.build_task_list(
            [self.step], self.file_records, "Root", "/root", [], 0, 2)
        self.assertEqual([t["folder"] for t in out], ["Util", "Core"])

    def test_cycle_refactor_edges_remapped(self):
        out = tasks.build_task_list(
            [self.step], self.file_records, "Root", "/root", [], 0, 2)
        # dst -> target_folder; refs and types carried through verbatim.
        self.assertEqual(out[0]["refactor_edges"], [
            {"target_folder": "Core", "refs": 2, "types": ["CoreService"]},
        ])

    def test_cycle_carries_outgoing_and_incoming_counts(self):
        out = tasks.build_task_list(
            [self.step], self.file_records, "Root", "/root", [], 0, 2)
        self.assertEqual(out[0]["outgoing_refs_to_refactor"], 2)
        self.assertEqual(out[0]["incoming_consumers"], 1)
        self.assertEqual(out[1]["incoming_consumers"], 3)

    def test_cycle_size_set_from_step(self):
        out = tasks.build_task_list(
            [self.step], self.file_records, "Root", "/root", [], 0, 2)
        self.assertEqual(out[0]["cycle_size"], 2)
        self.assertEqual(out[1]["cycle_size"], 2)

    def test_cycle_unlocks_always_empty(self):
        out = tasks.build_task_list(
            [self.step], self.file_records, "Root", "/root", [], 0, 2)
        self.assertEqual(out[0]["unlocks"], [])

    def test_cycle_notes_mention_cycle_membership(self):
        out = tasks.build_task_list(
            [self.step], self.file_records, "Root", "/root", [], 0, 2)
        self.assertIn("2-folder cycle", out[0]["notes"])

    def test_cycle_files_sorted_and_counted(self):
        out = tasks.build_task_list(
            [self.step], self.file_records, "Root", "/root", [], 0, 2)
        util_task = next(t for t in out if t["folder"] == "Util")
        self.assertEqual(util_task["files"], ["UtilHelper.swift"])
        self.assertEqual(util_task["files_count"], 1)


class BuildTaskListMixedTest(unittest.TestCase):
    def test_ids_increment_across_cycle_then_singleton(self):
        file_records = [{"folder": "Util", "name": "U.swift"}]
        plan = [
            cycle_step(1, ["Core", "Util"], extractions=[
                {"folder": "Util", "edges": [], "outgoing_refs": 0,
                 "incoming_count": 0},
                {"folder": "Core", "edges": [], "outgoing_refs": 0,
                 "incoming_count": 0},
            ]),
            singleton_step(2, "Feature"),
            singleton_step(3, "App"),
        ]
        out = tasks.build_task_list(
            plan, file_records, "Root", "/root", [], 0, 4)
        self.assertEqual([t["id"] for t in out], [1, 2, 3, 4])
        self.assertEqual(
            [t["type"] for t in out],
            ["extract_from_cycle", "extract_from_cycle",
             "extract_leaf", "extract_leaf"],
        )

    def test_empty_plan_yields_no_tasks(self):
        out = tasks.build_task_list([], [], "Root", "/root", [], 0, 0)
        self.assertEqual(out, [])


# --- write_task_list_markdown ----------------------------------------------
class WriteMarkdownTest(unittest.TestCase):
    def setUp(self):
        self.file_records = [{"folder": "Core", "name": "CoreService.swift"}]
        self.tasks = tasks.build_task_list(
            [singleton_step(1, "Core")], self.file_records,
            "Root", "/root", [], 0, 1)

    def _write(self, tasklist, meta):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "migration_plan.md"
        tasks.write_task_list_markdown(tasklist, meta, out)
        return out.read_text(encoding="utf-8")

    def test_has_migration_plan_title(self):
        text = self._write(self.tasks, sample_meta())
        self.assertIn("# Migration Plan", text)
        self.assertIn("ToyProj", text)

    def test_has_task_header(self):
        text = self._write(self.tasks, sample_meta())
        self.assertIn("## Task 1", text)
        self.assertIn("`Core`", text)

    def test_lists_files_when_present(self):
        text = self._write(self.tasks, sample_meta())
        self.assertIn("`CoreService.swift`", text)

    def test_migrated_prefixes_rendered(self):
        text = self._write(self.tasks, sample_meta())
        self.assertIn("Legacy", text)

    def test_no_blocked_section_when_empty(self):
        text = self._write(self.tasks, sample_meta(blocked_reasons={}))
        self.assertNotIn("Can't modularize", text)

    def test_blocked_section_when_blocked_reasons_present(self):
        meta = sample_meta(blocked_reasons={"Widgets": ["AppCore"]})
        text = self._write(self.tasks, meta)
        self.assertIn("Can't modularize", text)
        self.assertIn("`Widgets`", text)
        self.assertIn("`AppCore`", text)

    def test_excluded_count_line_shown_when_nonzero(self):
        text = self._write(self.tasks, sample_meta(excluded_count=2))
        self.assertIn("won't-be-modularized (excluded): **2**", text)

    def test_cycle_task_renders_edges_and_steps(self):
        cyc_tasks = tasks.build_task_list(
            [cycle_step(1, ["Core", "Util"], extractions=[
                {"folder": "Util",
                 "edges": [{"dst": "Core", "refs": 2, "types": ["CoreService"]}],
                 "outgoing_refs": 2, "incoming_count": 1},
            ])],
            [{"folder": "Util", "name": "U.swift"}],
            "Root", "/root", [], 0, 1)
        text = self._write(cyc_tasks, sample_meta(tasks_total=1))
        self.assertIn("Cycle size:", text)
        self.assertIn("Edges to refactor:", text)
        self.assertIn("→ `Core`", text)
        self.assertIn("`CoreService`", text)

    def test_prefixes_none_placeholder_when_empty(self):
        text = self._write(self.tasks, sample_meta(migrated_prefixes=[]))
        self.assertIn("(none)", text)

    def test_unlocks_rendered_in_markdown(self):
        tl = tasks.build_task_list(
            [singleton_step(1, "Core",
                            unlocks=[{"folders": ["Feature"], "size": 1}])],
            self.file_records, "Root", "/root", [], 0, 1)
        text = self._write(tl, sample_meta())
        self.assertIn("Unlocks:", text)
        self.assertIn("`Feature`", text)


# --- write_task_list_json --------------------------------------------------
class WriteJsonTest(unittest.TestCase):
    def setUp(self):
        self.file_records = [{"folder": "Core", "name": "CoreService.swift"}]
        self.tasks = tasks.build_task_list(
            [singleton_step(1, "Core")], self.file_records,
            "Root", "/root", [], 0, 1)

    def _write_and_reload(self, tasklist, meta):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "migration_plan.json"
        tasks.write_task_list_json(tasklist, meta, out)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_top_level_has_meta_and_tasks(self):
        data = self._write_and_reload(self.tasks, sample_meta())
        self.assertIn("meta", data)
        self.assertIn("tasks", data)

    def test_meta_roundtrips(self):
        meta = sample_meta()
        data = self._write_and_reload(self.tasks, meta)
        self.assertEqual(data["meta"], meta)

    def test_tasks_roundtrip(self):
        data = self._write_and_reload(self.tasks, sample_meta())
        self.assertEqual(data["tasks"], self.tasks)

    def test_tasks_count_matches(self):
        data = self._write_and_reload(self.tasks, sample_meta())
        self.assertEqual(len(data["tasks"]), 1)
        self.assertEqual(data["tasks"][0]["folder"], "Core")

    def test_json_is_valid_for_cycle_tasks(self):
        cyc_tasks = tasks.build_task_list(
            [cycle_step(1, ["Core", "Util"], extractions=[
                {"folder": "Util",
                 "edges": [{"dst": "Core", "refs": 2, "types": ["CoreService"]}],
                 "outgoing_refs": 2, "incoming_count": 1},
            ])],
            [{"folder": "Util", "name": "U.swift"}],
            "Root", "/root", [], 0, 1)
        data = self._write_and_reload(cyc_tasks, sample_meta(tasks_total=1))
        self.assertEqual(data["tasks"][0]["type"], "extract_from_cycle")
        self.assertEqual(data["tasks"][0]["cycle_size"], 2)


# --- integration with fixtures ---------------------------------------------
class FixturesIntegrationTest(unittest.TestCase):
    def test_build_from_fixture_files(self):
        # Use the toy project's file records (each carries folder + name).
        file_records = fixtures.sample_index_dict()["files"]
        plan = [singleton_step(1, "Core")]
        out = tasks.build_task_list(
            plan, file_records, "Toy", "/toy", [], 0,
            len(fixtures.source_folders()))
        self.assertEqual(out[0]["files"], ["CoreService.swift"])
        self.assertEqual(out[0]["files_count"], 1)


if __name__ == "__main__":
    unittest.main()
