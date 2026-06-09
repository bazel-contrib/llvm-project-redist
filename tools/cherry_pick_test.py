#!/usr/bin/env python3
"""Unit tests for cherry_pick.py."""

import tempfile
import unittest
from pathlib import Path

from tools.cherry_pick import (
    derive_description,
    extract_sha,
    latest_version,
    next_patch_number,
    rewrite_paths,
)


class ExtractShaTest(unittest.TestCase):
    def test_bare_sha(self) -> None:
        self.assertEqual(extract_sha("abc1234"), "abc1234")

    def test_full_sha(self) -> None:
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(extract_sha(sha), sha)

    def test_commit_url(self) -> None:
        url = "https://github.com/llvm/llvm-project/commit/abc1234"
        self.assertEqual(extract_sha(url), "abc1234")

    def test_pr_commit_url(self) -> None:
        url = "https://github.com/llvm/llvm-project/pull/12345/commits/deadbeef"
        self.assertEqual(extract_sha(url), "deadbeef")

    def test_case_normalized(self) -> None:
        self.assertEqual(extract_sha("ABC1234"), "abc1234")

    def test_rejects_non_hex(self) -> None:
        with self.assertRaises(SystemExit):
            extract_sha("not-a-sha")


class RewritePathsTest(unittest.TestCase):
    def test_strips_overlay_prefix(self) -> None:
        patch = (
            "diff --git a/utils/bazel/llvm-project-overlay/llvm/BUILD.bazel "
            "b/utils/bazel/llvm-project-overlay/llvm/BUILD.bazel\n"
            "--- a/utils/bazel/llvm-project-overlay/llvm/BUILD.bazel\n"
            "+++ b/utils/bazel/llvm-project-overlay/llvm/BUILD.bazel\n"
        )
        result = rewrite_paths(patch)
        self.assertNotIn("utils/bazel/llvm-project-overlay/", result)
        self.assertIn("a/llvm/BUILD.bazel", result)
        self.assertIn("b/llvm/BUILD.bazel", result)

    def test_leaves_non_overlay_paths_alone(self) -> None:
        patch = "--- a/llvm/lib/Foo.cpp\n+++ b/llvm/lib/Foo.cpp\n"
        self.assertEqual(rewrite_paths(patch), patch)


class NextPatchNumberTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.patches = Path(self.tmpdir.name) / "patches"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_missing_dir_returns_one(self) -> None:
        self.assertEqual(next_patch_number(self.patches), 1)

    def test_empty_dir_returns_one(self) -> None:
        self.patches.mkdir()
        self.assertEqual(next_patch_number(self.patches), 1)

    def test_picks_max_plus_one(self) -> None:
        self.patches.mkdir()
        (self.patches / "001_a.patch").touch()
        (self.patches / "002_b.patch").touch()
        (self.patches / "005_c.patch").touch()
        self.assertEqual(next_patch_number(self.patches), 6)

    def test_ignores_non_numeric_prefixes(self) -> None:
        self.patches.mkdir()
        (self.patches / "README.md").touch()
        (self.patches / "001_a.patch").touch()
        self.assertEqual(next_patch_number(self.patches), 2)


class DeriveDescriptionTest(unittest.TestCase):
    def test_simple_subject(self) -> None:
        patch = "Subject: [PATCH] Fix the BUILD file\n"
        self.assertEqual(derive_description(patch), "fix_the_build_file")

    def test_no_patch_tag(self) -> None:
        patch = "Subject: Add scope resolution\n"
        self.assertEqual(derive_description(patch), "add_scope_resolution")

    def test_numbered_patch_tag(self) -> None:
        patch = "Subject: [PATCH 1/3] First commit\n"
        self.assertEqual(derive_description(patch), "first_commit")

    def test_truncates_long_subjects(self) -> None:
        patch = "Subject: " + ("a" * 200) + "\n"
        self.assertLessEqual(len(derive_description(patch)), 50)

    def test_missing_subject_fallback(self) -> None:
        self.assertEqual(derive_description("no subject here"), "cherry_pick")

    def test_strips_bracket_bazel_prefix(self) -> None:
        patch = "Subject: [bazel] Fix the build\n"
        self.assertEqual(derive_description(patch), "fix_the_build")

    def test_strips_bracket_bazel_prefix_uppercase(self) -> None:
        patch = "Subject: [Bazel] Update BUILD files\n"
        self.assertEqual(derive_description(patch), "update_build_files")

    def test_strips_colon_bazel_prefix(self) -> None:
        patch = "Subject: bazel: add support for foo\n"
        self.assertEqual(derive_description(patch), "add_support_for_foo")

    def test_strips_bazel_after_patch_tag(self) -> None:
        patch = "Subject: [PATCH] [bazel] Fix the build\n"
        self.assertEqual(derive_description(patch), "fix_the_build")

    def test_keeps_bazel_in_middle_of_subject(self) -> None:
        patch = "Subject: Add new bazel rules for foo\n"
        self.assertEqual(derive_description(patch), "add_new_bazel_rules_for_foo")


class LatestVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.versions = Path(self.tmpdir.name) / "versions"
        self.versions.mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_picks_highest(self) -> None:
        (self.versions / "17.0.3").mkdir()
        (self.versions / "20.1.0").mkdir()
        (self.versions / "18.1.0").mkdir()
        self.assertEqual(latest_version(self.versions), "20.1.0")

    def test_errors_on_empty(self) -> None:
        with self.assertRaises(SystemExit):
            latest_version(self.versions)


if __name__ == "__main__":
    unittest.main()
