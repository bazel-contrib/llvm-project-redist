import io
import sys
import unittest

from tools.transform_module_bazel import transform

# Realistic upstream MODULE.bazel (matches llvm/llvm-project main)
UPSTREAM = """\
# This file is licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

\"\"\"bzlmod configuration for llvm-project\"\"\"

module(name = "llvm-project-overlay")

bazel_dep(name = "apple_support", version = "1.24.1", repo_name = "build_bazel_apple_support")
bazel_dep(name = "bazel_skylib", version = "1.8.2")
bazel_dep(name = "platforms", version = "1.0.0")
bazel_dep(name = "protobuf", version = "31.1", repo_name = "com_google_protobuf")
bazel_dep(name = "rules_android", version = "0.6.6")
bazel_dep(name = "rules_cc", version = "0.2.11")
bazel_dep(name = "rules_python", version = "1.9.0")
bazel_dep(name = "rules_shell", version = "0.6.1")
bazel_dep(name = "zlib-ng", version = "2.0.7", repo_name = "llvm_zlib")
bazel_dep(name = "zstd", version = "1.5.7", repo_name = "llvm_zstd")

llvm_repos_extension = use_extension(":extensions.bzl", "llvm_repos_extension")
use_repo(
    llvm_repos_extension,
    "gmp",
    "llvm-raw",
    "mpc",
    "mpfr",
    "nanobind",
    "pfm",
    "pyyaml",
    "robin_map",
    "vulkan_headers",
    "vulkan_sdk",
)

llvm_configure = use_repo_rule("@llvm-raw//utils/bazel:configure.bzl", "llvm_configure")

llvm_configure(name = "llvm-project")
"""


class TransformModuleBazelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.result = transform(UPSTREAM, "22.1.4")

    # ---- module() declaration ----

    def test_module_renamed(self) -> None:
        self.assertIn('module(name = "llvm-project"', self.result)

    def test_module_version_injected(self) -> None:
        self.assertIn('version = "22.1.4"', self.result)

    def test_old_module_name_absent(self) -> None:
        self.assertNotIn("llvm-project-overlay", self.result)

    # ---- bazel_dep pins preserved ----

    def test_all_bazel_deps_retained(self) -> None:
        for dep in [
            "apple_support",
            "bazel_skylib",
            "platforms",
            "protobuf",
            "rules_android",
            "rules_cc",
            "rules_python",
            "rules_shell",
            "zlib-ng",
            "zstd",
        ]:
            with self.subTest(dep=dep):
                self.assertIn(f'name = "{dep}"', self.result)

    def test_bazel_dep_versions_preserved(self) -> None:
        self.assertIn('version = "1.24.1"', self.result)  # apple_support
        self.assertIn('version = "31.1"', self.result)  # protobuf
        self.assertIn('version = "1.5.7"', self.result)  # zstd

    def test_repo_name_aliases_preserved(self) -> None:
        self.assertIn('repo_name = "build_bazel_apple_support"', self.result)
        self.assertIn('repo_name = "com_google_protobuf"', self.result)
        self.assertIn('repo_name = "llvm_zlib"', self.result)
        self.assertIn('repo_name = "llvm_zstd"', self.result)

    # ---- use_repo: llvm-raw removed, others kept ----

    def test_llvm_raw_removed_from_use_repo(self) -> None:
        self.assertNotIn('"llvm-raw"', self.result)

    def test_other_repos_retained_in_use_repo(self) -> None:
        for repo in [
            "gmp",
            "mpc",
            "mpfr",
            "nanobind",
            "pfm",
            "pyyaml",
            "robin_map",
            "vulkan_headers",
            "vulkan_sdk",
        ]:
            with self.subTest(repo=repo):
                self.assertIn(f'"{repo}"', self.result)

    def test_use_extension_preserved(self) -> None:
        self.assertIn(
            'use_extension(":extensions.bzl", "llvm_repos_extension")',
            self.result,
        )

    # ---- llvm_configure removed ----

    def test_use_repo_rule_removed(self) -> None:
        self.assertNotIn("use_repo_rule(", self.result)

    def test_llvm_configure_invocation_removed(self) -> None:
        self.assertNotIn("llvm_configure(", self.result)

    def test_configure_bzl_reference_removed(self) -> None:
        self.assertNotIn("configure.bzl", self.result)

    # ---- structural / formatting ----

    def test_header_comments_preserved(self) -> None:
        self.assertIn("Apache License v2.0 with LLVM Exceptions", self.result)

    def test_no_triple_blank_lines(self) -> None:
        self.assertNotIn("\n\n\n", self.result)

    def test_ends_with_newline(self) -> None:
        self.assertTrue(self.result.endswith("\n"))

    # ---- warning when llvm-raw is missing ----

    def test_warning_when_llvm_raw_missing(self) -> None:
        source_without_llvm_raw = UPSTREAM.replace('    "llvm-raw",\n', "")
        stderr = io.StringIO()
        old_stderr = sys.stderr
        try:
            sys.stderr = stderr
            transform(source_without_llvm_raw, "1.0.0")
        finally:
            sys.stderr = old_stderr
        self.assertIn("WARNING", stderr.getvalue())

    def test_no_warning_on_normal_input(self) -> None:
        stderr = io.StringIO()
        old_stderr = sys.stderr
        try:
            sys.stderr = stderr
            transform(UPSTREAM, "1.0.0")
        finally:
            sys.stderr = old_stderr
        self.assertEqual("", stderr.getvalue())

    # ---- different version strings ----

    def test_version_with_suffix(self) -> None:
        result = transform(UPSTREAM, "22.0.0-rc1")
        self.assertIn('version = "22.0.0-rc1"', result)

    # ---- multi-line use_repo_rule (hypothetical future format) ----

    def test_multiline_use_repo_rule(self) -> None:
        src = UPSTREAM.replace(
            'llvm_configure = use_repo_rule("@llvm-raw//utils/bazel:configure.bzl", "llvm_configure")',
            'llvm_configure = use_repo_rule(\n    "@llvm-raw//utils/bazel:configure.bzl",\n    "llvm_configure",\n)',
        )
        result = transform(src, "1.0.0")
        self.assertNotIn("use_repo_rule(", result)
        self.assertNotIn("configure.bzl", result)

    def test_multiline_llvm_configure_invocation(self) -> None:
        src = UPSTREAM.replace(
            'llvm_configure(name = "llvm-project")',
            'llvm_configure(\n    name = "llvm-project",\n)',
        )
        result = transform(src, "1.0.0")
        self.assertNotIn("llvm_configure(", result)


if __name__ == "__main__":
    unittest.main()
