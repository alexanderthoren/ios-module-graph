"""Tests for modgraph.config: regex-scanner patterns and default constants.

Follows the shape of tests/test_graph.py — one behaviour per method, concrete
assertions, clear names. The config module is pure data (compiled regexes,
Paths, and frozenset-like sets), so these tests pin down the exact matching
semantics the regex-scanner fallback relies on, plus the default path/skip-list
contracts the rest of the tool depends on.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import modgraph
from modgraph import config


class DeclReTest(unittest.TestCase):
    def test_matches_each_declaration_keyword(self):
        # class/struct/enum/protocol/actor/typealias all introduce a decl.
        for kw in ("class", "struct", "enum", "protocol", "actor", "typealias"):
            with self.subTest(keyword=kw):
                self.assertEqual(config.DECL_RE.findall(kw + " Foo"), ["Foo"])

    def test_captures_only_the_type_name(self):
        # The capture group is the name, not the keyword.
        m = config.DECL_RE.search("public final class CoreService {}")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "CoreService")

    def test_name_must_be_capitalized(self):
        # A lowercase identifier after the keyword is not captured.
        self.assertEqual(config.DECL_RE.findall("class lowercase {}"), [])

    def test_single_capital_letter_name_allowed(self):
        # DECL_RE has no minimum length: a one-letter capitalized name matches.
        self.assertEqual(config.DECL_RE.findall("protocol P {}"), ["P"])

    def test_keyword_only_no_name_does_not_match(self):
        self.assertEqual(config.DECL_RE.findall("class"), [])

    def test_keyword_must_be_a_whole_word(self):
        # \b before the keyword: "classy Foo" is not a declaration.
        self.assertEqual(config.DECL_RE.findall("classy Foo"), [])

    def test_other_keywords_are_not_declarations(self):
        # func/var/let are not in the alternation.
        self.assertEqual(config.DECL_RE.findall("func Run() {}"), [])
        self.assertEqual(config.DECL_RE.findall("var Thing = 1"), [])

    def test_name_may_contain_digits_and_underscores(self):
        self.assertEqual(config.DECL_RE.findall("enum Foo_Bar2 {}"), ["Foo_Bar2"])

    def test_finds_multiple_declarations(self):
        src = "class Foo {}\nstruct Bar {}\nenum Baz {}\n"
        self.assertEqual(config.DECL_RE.findall(src), ["Foo", "Bar", "Baz"])


class RefReTest(unittest.TestCase):
    def test_matches_capitalized_ident_length_three(self):
        # Minimum match is a capital + 2 more chars (length 3).
        self.assertEqual(config.REF_RE.findall("Foo"), ["Foo"])

    def test_two_char_capitalized_ident_is_ignored(self):
        # "Ab" (len 2) and "AB" (len 2) do not satisfy [A-Z][A-Za-z0-9_]{2,}.
        self.assertEqual(config.REF_RE.findall("Ab"), [])
        self.assertEqual(config.REF_RE.findall("AB"), [])

    def test_single_capital_letter_is_ignored(self):
        self.assertEqual(config.REF_RE.findall("X"), [])

    def test_lowercase_ident_is_ignored(self):
        # Must start with an uppercase letter.
        self.assertEqual(config.REF_RE.findall("foobar"), [])

    def test_capital_followed_by_digits_matches(self):
        # The trailing chars may be digits: "X12" is length 3 and matches.
        self.assertEqual(config.REF_RE.findall("X12"), ["X12"])

    def test_trailing_chars_may_be_underscore(self):
        # "_" is in the trailing char class.
        self.assertEqual(config.REF_RE.findall("A_b"), ["A_b"])

    def test_picks_only_capitalized_words_from_mixed_text(self):
        found = config.REF_RE.findall("let svc = CoreService(); foo Ab Abc")
        self.assertEqual(found, ["CoreService", "Abc"])

    def test_word_boundary_at_both_ends(self):
        # Embedded in a longer all-word run, \b still delimits whole identifiers.
        self.assertEqual(config.REF_RE.findall("FeatureView"), ["FeatureView"])


class CommentAndStringReTest(unittest.TestCase):
    def test_line_comment_stripped_to_end_of_line_only(self):
        out = config.LINE_COMMENT_RE.sub("", "code // a comment\nnext line")
        self.assertEqual(out, "code \nnext line")

    def test_line_comment_is_multiline(self):
        # MULTILINE: each line's // ... is removed up to its own newline.
        src = "a // x\nb // y\n"
        self.assertEqual(config.LINE_COMMENT_RE.sub("", src), "a \nb \n")

    def test_line_comment_re_has_multiline_flag(self):
        self.assertTrue(config.LINE_COMMENT_RE.flags & re.MULTILINE)

    def test_block_comment_spans_newlines(self):
        # DOTALL: /* ... */ is removed even across line breaks.
        out = config.BLOCK_COMMENT_RE.sub("", "a /* b\nc */ d")
        self.assertEqual(out, "a  d")

    def test_block_comment_re_has_dotall_flag(self):
        self.assertTrue(config.BLOCK_COMMENT_RE.flags & re.DOTALL)

    def test_block_comment_is_non_greedy(self):
        # Two separate blocks are removed individually, text between survives.
        out = config.BLOCK_COMMENT_RE.sub("", "/*a*/keep/*b*/")
        self.assertEqual(out, "keep")

    def test_string_literal_removed(self):
        out = config.STRING_RE.sub("", 'let x = "hello" + y')
        self.assertEqual(out, "let x =  + y")

    def test_string_handles_escaped_quote(self):
        # The pattern allows \\. so an escaped quote stays inside the literal.
        out = config.STRING_RE.sub("", 'a "hi\\"there" b')
        self.assertEqual(out, "a  b")

    def test_string_non_greedy_across_two_literals(self):
        out = config.STRING_RE.sub("", '"one" mid "two"')
        self.assertEqual(out, " mid ")


class DefaultPathsTest(unittest.TestCase):
    def test_repo_root_is_package_parent_parent(self):
        # REPO_ROOT == the directory that contains the modgraph/ package.
        self.assertEqual(
            config.REPO_ROOT,
            Path(modgraph.__file__).resolve().parent.parent,
        )

    def test_repo_root_contains_modgraph_package(self):
        self.assertEqual(
            Path(modgraph.__file__).resolve().parent.parent.name,
            config.REPO_ROOT.name,
        )
        self.assertTrue((config.REPO_ROOT / "modgraph").is_dir())

    def test_repo_root_is_absolute(self):
        self.assertTrue(config.REPO_ROOT.is_absolute())

    def test_default_out_filename(self):
        self.assertEqual(config.DEFAULT_OUT.name, "dependency_graph.html")

    def test_default_out_under_repo_root(self):
        self.assertEqual(config.DEFAULT_OUT.parent, config.REPO_ROOT)
        self.assertEqual(config.DEFAULT_OUT, config.REPO_ROOT / "dependency_graph.html")

    def test_default_excluded_filename(self):
        self.assertEqual(config.DEFAULT_EXCLUDED.name, ".modularization_excluded.json")

    def test_default_excluded_under_repo_root(self):
        self.assertEqual(config.DEFAULT_EXCLUDED.parent, config.REPO_ROOT)
        self.assertEqual(
            config.DEFAULT_EXCLUDED,
            config.REPO_ROOT / ".modularization_excluded.json",
        )


class SkipListTest(unittest.TestCase):
    def test_default_skip_names_expected_members(self):
        expected = {
            "DerivedData", "build", "Build", "dist", "out",
            "Pods", "Carthage", "node_modules", "vendor",
            "__pycache__", "venv", "env",
            "SourcePackages", "checkouts",
        }
        self.assertEqual(config.DEFAULT_SKIP_NAMES, expected)

    def test_default_skip_names_is_a_set(self):
        self.assertIsInstance(config.DEFAULT_SKIP_NAMES, set)

    def test_skip_names_are_case_sensitive(self):
        # Both "build" and "Build" are present (case matters for basename match).
        self.assertIn("build", config.DEFAULT_SKIP_NAMES)
        self.assertIn("Build", config.DEFAULT_SKIP_NAMES)

    def test_skip_names_includes_checkout_caches(self):
        self.assertIn("SourcePackages", config.DEFAULT_SKIP_NAMES)
        self.assertIn("checkouts", config.DEFAULT_SKIP_NAMES)

    def test_test_dir_names_expected_members(self):
        self.assertEqual(
            config.TEST_DIR_NAMES,
            {"Tests", "Test", "UITests", "SnapshotTests", "tests"},
        )

    def test_test_dir_names_is_a_set(self):
        self.assertIsInstance(config.TEST_DIR_NAMES, set)

    def test_ext_skips_expected_members(self):
        self.assertEqual(
            config.EXT_SKIPS,
            {".xcodeproj", ".xcworkspace", ".bundle", ".framework", ".app"},
        )

    def test_ext_skips_entries_start_with_dot(self):
        self.assertTrue(all(e.startswith(".") for e in config.EXT_SKIPS))

    def test_skip_lists_are_disjoint_from_ext_skips(self):
        # Extension skips and name skips are different namespaces.
        self.assertEqual(config.EXT_SKIPS & config.DEFAULT_SKIP_NAMES, set())


if __name__ == "__main__":
    unittest.main()
