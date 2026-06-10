"""Tests for modgraph.file_affinity — misplaced-file move suggestions.

Inline file_edges fixtures; thresholds under test: a move needs
FILE_AFFINITY_MIN_REFS (3) mass in one foreign folder AND
FILE_AFFINITY_DOMINANCE (2x) over every alternative including home.
"""
from __future__ import annotations

import unittest

from modgraph.file_affinity import compute_file_moves


def edge(src, dst, w, symbols=()):
    return {"src": src, "dst": dst, "w": w, "symbols": list(symbols)}


class MoveSuggestionTest(unittest.TestCase):
    def test_dominant_foreign_folder_suggests_move(self):
        edges = [edge("A/Stray.swift", "B/Home.swift", 5, ["HomeType"])]
        out = compute_file_moves(edges)
        self.assertEqual(len(out["items"]), 2)   # both endpoints scanned
        move = next(i for i in out["items"] if i["file"] == "A/Stray.swift")
        self.assertEqual(move["from"], "A")
        self.assertEqual(move["to"], "B")
        self.assertEqual(move["refs"], 5)
        self.assertEqual(move["symbols"], ["HomeType"])

    def test_below_min_refs_ignored(self):
        out = compute_file_moves([edge("A/X.swift", "B/Y.swift", 2)])
        self.assertEqual(out["items"], [])

    def test_coordinator_fails_dominance(self):
        # Talks to B (4) and C (3): 4 < 2*3 — no single home elsewhere.
        edges = [edge("A/Coord.swift", "B/Y.swift", 4),
                 edge("A/Coord.swift", "C/Z.swift", 3)]
        out = compute_file_moves(edges)
        self.assertNotIn("A/Coord.swift", [i["file"] for i in out["items"]])

    def test_own_folder_mass_counts_against_dominance(self):
        # 4 refs to B but 3 inside its own folder: 4 < 2*3 — stays put.
        edges = [edge("A/X.swift", "B/Y.swift", 4),
                 edge("A/X.swift", "A/Sibling.swift", 3)]
        out = compute_file_moves(edges)
        self.assertNotIn("A/X.swift", [i["file"] for i in out["items"]])

    def test_incoming_references_also_pull(self):
        # B/Y references A/Stray heavily — affinity is bidirectional.
        edges = [edge("B/Y.swift", "A/Stray.swift", 6, ["StrayType"])]
        out = compute_file_moves(edges, source_folders={"A", "B"})
        move = next(i for i in out["items"] if i["file"] == "A/Stray.swift")
        self.assertEqual(move["to"], "B")

    def test_scope_filter_drops_migrated_homes_and_destinations(self):
        edges = [edge("Pkg/Sources/Lib/X.swift", "A/Y.swift", 9),
                 edge("A/Z.swift", "Pkg/Sources/Lib/W.swift", 9)]
        out = compute_file_moves(edges, source_folders={"A"})
        self.assertEqual(out["items"], [])

    def test_items_sorted_biggest_first_and_deterministic(self):
        # B/Y is anchored home by intra-folder mass so only the A files move.
        edges = [edge("A/Small.swift", "B/Y.swift", 3),
                 edge("A/Big.swift", "B/Y.swift", 9),
                 edge("B/Y.swift", "B/Other.swift", 30)]
        out = compute_file_moves(edges, source_folders={"A", "B"})
        self.assertEqual([i["file"] for i in out["items"]],
                         ["A/Big.swift", "A/Small.swift"])
        self.assertEqual(out, compute_file_moves(edges,
                                                 source_folders={"A", "B"}))

    def test_empty_input(self):
        out = compute_file_moves([])
        self.assertEqual(out["items"], [])
        self.assertEqual(out["summary"]["files_scanned"], 0)


if __name__ == "__main__":
    unittest.main()
