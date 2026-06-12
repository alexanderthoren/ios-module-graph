"""Tests for modgraph.tasks — the master-plan markdown / JSON writers."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from modgraph.tasks import (master_plan_markdown, write_master_plan_json,
                            write_master_plan_markdown)


def _meta(**over):
    base = {
        "root_label": "ToyProj",
        "root_path": "/tmp/toy",
        "migrated_prefixes": ["Packages/P/Sources"],
        "baseline_count": 2,
        "source_total": 10,
        "excluded_count": 0,
        "blocked_reasons": {},
        "steps_total": 2,
    }
    base.update(over)
    return base


def _plan(**over):
    base = {
        "setup": [{
            "id": "setup:ratchet",
            "title": "Wire the architecture ratchet into CI",
            "why": "Lock in each step's structural win.",
            "how": ["Commit a baseline snapshot.", "Run just check in CI."],
            "done_when": "CI fails on any new folder edge or cycle.",
        }],
        "steps": [
            {
                "id": "setup:ratchet", "kind": "setup", "phase": -1,
                "subject": "setup:ratchet",
                "title": "Wire the architecture ratchet into CI",
                "shape": {"mode": "setup",
                          "rule": "Lock in each step's structural win.",
                          "destination": None},
                "what": {"files": 0, "types": 0, "resources_count": 0,
                         "resources": []},
                "why": {"narrative": "Lock in each step's structural win.",
                        "roi": None, "payoff": 0.0, "effort": 0.0,
                        "warm": None, "crit": None, "churn": None,
                        "cold_delta_s": None},
                "after": [], "unblocks": [],
                "details": {"how": ["Commit a baseline snapshot.",
                                    "Run just check in CI."],
                            "done_when": "CI fails on any new folder edge "
                                         "or cycle."},
                "verify": {"commands": [],
                           "expect": {"done_when": "CI fails on any new "
                                                   "folder edge or cycle."}},
            },
            {
                "id": "move:B/F.swift", "kind": "move_file", "phase": 0,
                "subject": "B/F.swift", "title": "Move F.swift → X/",
                "shape": {"mode": "move_file", "rule": "reference affinity",
                          "destination": "X"},
                "what": {"files": 1, "types": 0, "resources_count": 0,
                         "resources": []},
                "why": {"narrative": "4 of its references bind to X.",
                        "roi": 4.0, "payoff": 4.0, "effort": 1,
                        "warm": None, "crit": None, "churn": None,
                        "cold_delta_s": None},
                "after": [], "unblocks": ["Feat"], "details": {},
                "verify": {"commands": ["just refresh"],
                           "expect": {"edges": "the edge thins or dies"}},
            },
            {
                "id": "qw:Feat", "kind": "cut_then_extract", "phase": 2,
                "subject": "Feat", "title": "Move 1 file(s), then extract Feat",
                "shape": {"mode": "api_impl", "rule": "2 consumer module(s)",
                          "destination": None, "api_module": "FeatAPI",
                          "impl_module": "Feat", "consumers": 2,
                          "api_surface_count": 3,
                          "api_surface": ["FeatModel", "FeatVM"],
                          "protocols_for": ["FeatVM"]},
                "what": {"files": 3, "types": 4, "resources_count": 1,
                         "resources": ["Feat.xcassets"]},
                "why": {"narrative": "Blocked only by 1 misplaced file(s).",
                        "roi": 1.2, "payoff": 30.0, "effort": 25,
                        "warm": 4, "crit": True, "churn": 6,
                        "cold_delta_s": 2.5},
                "after": ["move:B/F.swift"], "unblocks": [],
                "details": {"alternative": "or isolate Hub instead"},
                "verify": {"commands": ["just refresh"],
                           "expect": {"modules": "5 → 7",
                                      "crit_len": "4 → 4 (simulated)"}},
            },
        ],
        "deferred": [{"id": "qw:C", "kind": "cut_then_extract", "subject": "C",
                      "title": "Extract C", "reason": "tail",
                      "why": "payoff under the floor."}],
        "equilibrium": {"criteria": [
            {"id": "app_thin", "label": "App target is a composition root",
             "current": "60% of types in the app target", "target": "≤ 20%",
             "met": False},
            {"id": "no_cycles", "label": "No module cycles",
             "current": "0 module cycle(s)", "target": "0", "met": True},
        ], "met": False},
        "summary": {"actions": 2, "deferred": 1, "stop": "Stop here.",
                    "waves": {"0": 1, "2": 1}, "shapes": {}, "setup_items": 1,
                    "equilibrium_met": False},
    }
    base.update(over)
    return base


class MarkdownTest(unittest.TestCase):
    def setUp(self):
        self.md = master_plan_markdown(_plan(), _meta())

    def test_title_and_header_counts(self):
        self.assertIn("# Migration Plan — ToyProj", self.md)
        self.assertIn("Steps: **3** in **3** phase(s)", self.md)
        self.assertIn("equilibrium **not met**", self.md)

    def test_setup_renders_as_the_leading_phase(self):
        # No separate Setup section: prerequisites are phase -1 of the feed.
        self.assertNotIn("## Setup (one-time, do these first)", self.md)
        p_setup = self.md.index("One-time prerequisites")
        p0 = self.md.index("## Phase 0 —")
        self.assertLess(p_setup, p0)
        self.assertIn("Wire the architecture ratchet into CI", self.md)
        self.assertIn("1. Commit a baseline snapshot.", self.md)
        self.assertIn("done_when CI fails", self.md)

    def test_steps_grouped_by_phase_in_order(self):
        p0 = self.md.index("## Phase 0 —")
        p2 = self.md.index("## Phase 2 —")
        self.assertLess(p0, p2)
        self.assertIn("Move misplaced files", self.md)

    def test_api_impl_shape_names_both_packages(self):
        self.assertIn("`api_impl`", self.md)
        self.assertIn("`FeatAPI` (API: protocols + value", self.md)
        self.assertIn("`Feat` (implementation, bound", self.md)

    def test_api_surface_and_protocols_rendered(self):
        self.assertIn("**API surface (3):** `FeatModel`, `FeatVM`, … (1 more)",
                      self.md)
        self.assertIn("protocols for (reference types with behavior): `FeatVM`",
                      self.md)

    def test_why_carries_build_grounding(self):
        self.assertIn("roi 1.2 (payoff 30.0 ÷ effort 25)", self.md)
        self.assertIn("churn 6 commit(s)", self.md)
        self.assertIn("on the cold critical path", self.md)
        self.assertIn("~2.5s compile work leaves the app target", self.md)

    def test_after_unblocks_and_alternative(self):
        self.assertIn("- **After:** `move:B/F.swift`", self.md)
        self.assertIn("- **Unblocks:** `Feat`", self.md)
        self.assertIn("- **Alternative:** or isolate Hub instead", self.md)

    def test_verify_commands_and_expect(self):
        self.assertIn("- `just refresh`", self.md)
        self.assertIn("expect: crit_len 4 → 4 (simulated) · modules 5 → 7",
                      self.md)

    def test_moves_line_lists_resources(self):
        self.assertIn("- **Moves:** 3 file(s) · 4 type(s) · 1 resource(s) "
                      "(Feat.xcassets)", self.md)

    def test_stop_line_and_deferred_with_reason(self):
        self.assertIn("## 🛑 Stop line", self.md)
        self.assertIn("Stop here.", self.md)
        self.assertIn("- **Extract C** — `tail`: payoff under the floor.",
                      self.md)

    def test_equilibrium_checklist(self):
        self.assertIn("- [ ] **App target is a composition root**", self.md)
        self.assertIn("- [x] **No module cycles**", self.md)

    def test_behavior_preserving_guidance(self):
        self.assertIn("behavior-preserving", self.md)
        self.assertIn("composition root", self.md)

    def test_blocked_section_when_present(self):
        md = master_plan_markdown(
            _plan(), _meta(blocked_reasons={"Stuck": ["Legacy"]}))
        self.assertIn("## Can't modularize", md)
        self.assertIn("- `Stuck` — blocked by: `Legacy`", md)

    def test_no_blocked_section_when_empty(self):
        self.assertNotIn("## Can't modularize", self.md)

    def test_no_setup_section_when_empty(self):
        md = master_plan_markdown(_plan(setup=[]), _meta())
        self.assertNotIn("## Setup", md)

    def test_deterministic(self):
        self.assertEqual(self.md, master_plan_markdown(_plan(), _meta()))


class WritersTest(unittest.TestCase):
    def test_markdown_writer_writes_the_document(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "plan.md"
            write_master_plan_markdown(_plan(), _meta(), out)
            self.assertIn("# Migration Plan — ToyProj",
                          out.read_text(encoding="utf-8"))

    def test_json_writer_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "plan.json"
            write_master_plan_json(_plan(), _meta(), out)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["meta"]["root_label"], "ToyProj")
            self.assertEqual(len(data["master_plan"]["steps"]), 3)
            self.assertIn("equilibrium", data["master_plan"])


if __name__ == "__main__":
    unittest.main()
