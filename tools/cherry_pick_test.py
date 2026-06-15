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
    patch_touched_paths,
    picked_shas,
    rewrite_paths,
    sha_already_picked,
    stamp_upstream_header,
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


class StampUpstreamHeaderTest(unittest.TestCase):
    def test_prepends_header(self) -> None:
        body = "diff --git a/foo b/foo\n"
        out = stamp_upstream_header(body, "abc1234")
        self.assertTrue(out.startswith("# Upstream-Commit: https://github.com/llvm/llvm-project/commit/abc1234\n"))
        self.assertTrue(out.endswith(body))

    def test_patch_p1_skips_leading_comment(self) -> None:
        """Sanity: prepending a `#` line in front of unified diff is benign for `patch -p1`."""
        # A unified-diff parser skips lines until the first `diff --git` / `---` / `+++`,
        # so the header has no effect on apply. We assert the contract by inspecting
        # the boundary directly rather than spawning the `patch` binary.
        body = "diff --git a/x b/x\n--- a/x\n+++ b/x\n"
        out = stamp_upstream_header(body, "deadbeef")
        first_diff = out.index("diff --git")
        self.assertEqual(out[:first_diff].count("\n"), 1)


class PatchTouchedPathsTest(unittest.TestCase):
    def test_extracts_destination_paths(self) -> None:
        patch = (
            "diff --git a/llvm/BUILD.bazel b/llvm/BUILD.bazel\n"
            "--- a/llvm/BUILD.bazel\n"
            "+++ b/llvm/BUILD.bazel\n"
            "diff --git a/clang/BUILD.bazel b/clang/BUILD.bazel\n"
        )
        self.assertEqual(
            patch_touched_paths(patch),
            ["clang/BUILD.bazel", "llvm/BUILD.bazel"],
        )

    def test_empty_patch(self) -> None:
        self.assertEqual(patch_touched_paths(""), [])


class PickedShasTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.patches = Path(self.tmpdir.name) / "patches"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_missing_dir_returns_empty(self) -> None:
        self.assertEqual(picked_shas(self.patches), set())

    def test_reads_upstream_commit_comment(self) -> None:
        self.patches.mkdir()
        (self.patches / "010_foo.patch").write_text(
            "# Upstream-Commit: https://github.com/llvm/llvm-project/commit/abc1234\ndiff --git a/x b/x\n"
        )
        self.assertEqual(picked_shas(self.patches), {"abc1234"})

    def test_reads_from_header(self) -> None:
        self.patches.mkdir()
        full_sha = "0123456789abcdef0123456789abcdef01234567"
        (self.patches / "010_bar.patch").write_text(f"From {full_sha} Mon Sep 17 00:00:00 2001\nSubject: foo\n")
        self.assertEqual(picked_shas(self.patches), {full_sha})

    def test_combines_both_header_styles(self) -> None:
        self.patches.mkdir()
        (self.patches / "001_a.patch").write_text(
            "# Upstream-Commit: https://github.com/llvm/llvm-project/commit/abc1234\n"
        )
        (self.patches / "002_b.patch").write_text("From 0123456789abcdef0123456789abcdef01234567 Mon Sep 17\n")
        self.assertEqual(
            picked_shas(self.patches),
            {"abc1234", "0123456789abcdef0123456789abcdef01234567"},
        )

    def test_normalizes_case(self) -> None:
        self.patches.mkdir()
        (self.patches / "001_a.patch").write_text(
            "# Upstream-Commit: https://github.com/llvm/llvm-project/commit/ABC1234\n"
        )
        self.assertEqual(picked_shas(self.patches), {"abc1234"})


class ShaAlreadyPickedTest(unittest.TestCase):
    def test_full_sha_matches_short_picked(self) -> None:
        self.assertTrue(sha_already_picked({"abc1234"}, "abc1234567890" * 3 + "abcd"))

    def test_short_upstream_matches_full_picked(self) -> None:
        full = "abc1234567890" + "1" * 27
        self.assertTrue(sha_already_picked({full}, "abc12345"))

    def test_below_min_length_picked_ignored(self) -> None:
        # A 6-char picked SHA is too short to trust as a unique match.
        self.assertFalse(sha_already_picked({"abc123"}, "abc1234" + "0" * 33))

    def test_no_match(self) -> None:
        self.assertFalse(sha_already_picked({"abc1234"}, "deadbeef" + "0" * 32))

    def test_empty_picked(self) -> None:
        self.assertFalse(sha_already_picked(set(), "abc1234"))


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
