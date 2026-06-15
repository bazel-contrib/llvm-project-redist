#!/usr/bin/env python3
"""Tests for scripts/build.py."""

import hashlib
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools.build import (
    BOLT_SUPPORTED,
    LLVM_TARGETS,
    _make_deterministic,
    apply_overlay,
    compute_sha256,
    create_archive,
    extract_cmake_var,
    generate_targets_bzl,
    generate_vars_bzl,
    transform_module,
)


class ComputeSha256Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_known_content(self) -> None:
        p = self.tmpdir / "hello.txt"
        p.write_bytes(b"hello world\n")
        expected = hashlib.sha256(b"hello world\n").hexdigest()
        self.assertEqual(compute_sha256(p), expected)

    def test_empty_file(self) -> None:
        p = self.tmpdir / "empty"
        p.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        self.assertEqual(compute_sha256(p), expected)


class ExtractCmakeVarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.cmake = self.tmpdir / "CMakeLists.txt"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_simple_set(self) -> None:
        self.cmake.write_text("set(LLVM_VERSION_MAJOR 17)\n")
        self.assertEqual(extract_cmake_var(self.cmake, "LLVM_VERSION_MAJOR"), "17")

    def test_trailing_paren(self) -> None:
        self.cmake.write_text("set(LLVM_VERSION_MAJOR 17)\n")
        self.assertEqual(extract_cmake_var(self.cmake, "LLVM_VERSION_MAJOR"), "17")

    def test_with_spaces(self) -> None:
        self.cmake.write_text("  set( LLVM_VERSION_MINOR  0 )\n")
        self.assertEqual(extract_cmake_var(self.cmake, "LLVM_VERSION_MINOR"), "0")

    def test_missing_variable(self) -> None:
        self.cmake.write_text("set(OTHER_VAR 42)\n")
        self.assertIsNone(extract_cmake_var(self.cmake, "LLVM_VERSION_MAJOR"))

    def test_multiple_variables(self) -> None:
        self.cmake.write_text("set(LLVM_VERSION_MAJOR 17)\nset(LLVM_VERSION_MINOR 0)\nset(LLVM_VERSION_PATCH 3)\n")
        self.assertEqual(extract_cmake_var(self.cmake, "LLVM_VERSION_MAJOR"), "17")
        self.assertEqual(extract_cmake_var(self.cmake, "LLVM_VERSION_MINOR"), "0")
        self.assertEqual(extract_cmake_var(self.cmake, "LLVM_VERSION_PATCH"), "3")


class ApplyOverlayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.src = self.tmpdir / "src"
        self.src.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_copies_overlay_files(self) -> None:
        overlay = self.src / "utils" / "bazel" / "llvm-project-overlay"
        overlay.mkdir(parents=True)
        llvm = overlay / "llvm"
        llvm.mkdir()
        (llvm / "BUILD.bazel").write_text("# overlay llvm build")

        apply_overlay(self.src)

        self.assertTrue((self.src / "llvm" / "BUILD.bazel").is_file())
        self.assertEqual((self.src / "llvm" / "BUILD.bazel").read_text(), "# overlay llvm build")

    def test_copies_extra_files(self) -> None:
        overlay = self.src / "utils" / "bazel" / "llvm-project-overlay"
        overlay.mkdir(parents=True)
        bazel_utils = self.src / "utils" / "bazel"
        (bazel_utils / "BUILD.bazel").write_text("# root build")
        (bazel_utils / ".bazelrc").write_text("# bazelrc")

        apply_overlay(self.src)

        self.assertEqual((self.src / "BUILD.bazel").read_text(), "# root build")
        self.assertEqual((self.src / ".bazelrc").read_text(), "# bazelrc")

    def test_missing_overlay_is_warning(self) -> None:
        apply_overlay(self.src)

    def test_removes_utils_after_overlay(self) -> None:
        overlay = self.src / "utils" / "bazel" / "llvm-project-overlay"
        overlay.mkdir(parents=True)
        (overlay / "llvm").mkdir()
        (overlay / "llvm" / "BUILD.bazel").write_text("# overlay llvm build")
        # Sibling content under utils/ that has nothing to do with the overlay
        # — e.g. utils/arcanist/clang-format.sh — should also be removed, since
        # the published tarball treats the entire utils/ tree as dead code.
        (self.src / "utils" / "arcanist").mkdir(parents=True)
        (self.src / "utils" / "arcanist" / "clang-format.sh").write_text("#!/bin/sh\n")

        apply_overlay(self.src)

        self.assertFalse((self.src / "utils").exists())
        self.assertTrue((self.src / "llvm" / "BUILD.bazel").is_file())

    def test_missing_overlay_leaves_utils_alone(self) -> None:
        """Guard early-returns before rmtree, so a pre-existing utils/ stays put."""
        (self.src / "utils").mkdir()
        (self.src / "utils" / "marker").write_text("keep me")

        apply_overlay(self.src)

        self.assertTrue((self.src / "utils" / "marker").is_file())


class MakeDeterministicTest(unittest.TestCase):
    def test_zeros_metadata(self) -> None:
        info = tarfile.TarInfo(name="test.txt")
        info.uid = 1000
        info.gid = 1000
        info.uname = "user"
        info.gname = "group"
        info.mtime = 1234567890

        result = _make_deterministic(info)

        self.assertEqual(result.uid, 0)
        self.assertEqual(result.gid, 0)
        self.assertEqual(result.uname, "")
        self.assertEqual(result.gname, "")
        self.assertEqual(result.mtime, 0)


class CreateArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def _make_tree(self) -> Path:
        src = self.tmpdir / "test-dir"
        src.mkdir()
        (src / "a.txt").write_text("aaa")
        (src / "z.txt").write_text("zzz")
        sub = src / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("bbb")
        return src

    def test_deterministic_output(self) -> None:
        src = self._make_tree()
        out1 = self.tmpdir / "out1.tar.xz"
        out2 = self.tmpdir / "out2.tar.xz"

        sha1 = create_archive(src, out1)
        sha2 = create_archive(src, out2)

        self.assertEqual(sha1, sha2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())

    def test_sorted_within_directories(self) -> None:
        src = self._make_tree()
        out = self.tmpdir / "out.tar.xz"

        create_archive(src, out)

        with tarfile.open(out, "r:xz") as tar:
            names = tar.getnames()

        # Depth-first walk: files sorted within each directory, then subdirs
        self.assertEqual(
            names,
            [
                "test-dir",
                "test-dir/a.txt",
                "test-dir/z.txt",
                "test-dir/sub",
                "test-dir/sub/b.txt",
            ],
        )

    def test_zeroed_metadata(self) -> None:
        src = self._make_tree()
        out = self.tmpdir / "out.tar.xz"

        create_archive(src, out)

        with tarfile.open(out, "r:xz") as tar:
            for member in tar.getmembers():
                self.assertEqual(member.uid, 0, f"{member.name} uid")
                self.assertEqual(member.gid, 0, f"{member.name} gid")
                self.assertEqual(member.uname, "", f"{member.name} uname")
                self.assertEqual(member.gname, "", f"{member.name} gname")
                self.assertEqual(member.mtime, 0, f"{member.name} mtime")

    def test_content_preserved(self) -> None:
        src = self._make_tree()
        out = self.tmpdir / "out.tar.xz"

        create_archive(src, out)

        with tarfile.open(out, "r:xz") as tar:
            member = tar.getmember("test-dir/a.txt")
            content = tar.extractfile(member).read()  # type: ignore[union-attr]
        self.assertEqual(content, b"aaa")


class TransformModuleBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.src = self.tmpdir / "src"
        self.src.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_baseline_includes_rules_cc(self) -> None:
        transform_module(self.src, "17.0.3")
        content = (self.src / "MODULE.bazel").read_text()
        self.assertIn("rules_cc", content)

    def test_baseline_includes_bazel_skylib(self) -> None:
        transform_module(self.src, "17.0.3")
        content = (self.src / "MODULE.bazel").read_text()
        self.assertIn("bazel_skylib", content)

    def test_baseline_includes_platforms(self) -> None:
        transform_module(self.src, "17.0.3")
        content = (self.src / "MODULE.bazel").read_text()
        self.assertIn("platforms", content)

    def test_baseline_has_module_name_and_version(self) -> None:
        transform_module(self.src, "17.0.3.bcr.5")
        content = (self.src / "MODULE.bazel").read_text()
        self.assertIn('name = "llvm-project"', content)
        self.assertIn('version = "17.0.3.bcr.5"', content)

    def test_upstream_module_bazel_is_transformed(self) -> None:
        upstream_dir = self.src / "utils" / "bazel"
        upstream_dir.mkdir(parents=True)
        (upstream_dir / "MODULE.bazel").write_text(
            'module(name = "llvm-project-overlay")\n'
            'bazel_dep(name = "rules_cc", version = "0.2.11")\n'
            'use_repo_rule("@llvm-raw//utils/bazel:configure.bzl", "llvm_configure")\n'
            'llvm_configure(name = "llvm-project")\n'
        )
        transform_module(self.src, "22.1.0")
        content = (self.src / "MODULE.bazel").read_text()
        self.assertIn("rules_cc", content)
        self.assertNotIn("llvm_configure", content)


class GenerateTargetsBzlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.src = self.tmpdir / "src"
        self.src.mkdir()
        (self.src / "llvm").mkdir()
        (self.src / "bolt").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_generates_both_files(self) -> None:
        generate_targets_bzl(self.src)

        llvm_content = (self.src / "llvm" / "targets.bzl").read_text()
        bolt_content = (self.src / "bolt" / "targets.bzl").read_text()

        self.assertIn("X86", llvm_content)
        self.assertIn("AArch64", llvm_content)
        self.assertIn("AArch64", bolt_content)
        self.assertIn("X86", bolt_content)

    def test_bolt_excludes_unsupported(self) -> None:
        generate_targets_bzl(self.src)
        bolt_content = (self.src / "bolt" / "targets.bzl").read_text()

        for t in LLVM_TARGETS:
            if t in BOLT_SUPPORTED:
                self.assertIn(t, bolt_content)
            else:
                self.assertNotIn(t, bolt_content)


class GenerateVarsBzlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.src = self.tmpdir / "src"
        self.src.mkdir()
        (self.src / "llvm").mkdir()
        cmake_dir = self.src / "cmake" / "Modules"
        cmake_dir.mkdir(parents=True)

        (cmake_dir / "LLVMVersion.cmake").write_text(
            "set(LLVM_VERSION_MAJOR 17)\nset(LLVM_VERSION_MINOR 0)\nset(LLVM_VERSION_PATCH 3)\n"
        )
        (self.src / "llvm" / "CMakeLists.txt").write_text("set(CMAKE_CXX_STANDARD 17)\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_generates_vars(self) -> None:
        generate_vars_bzl(self.src)
        content = (self.src / "vars.bzl").read_text()

        self.assertIn('LLVM_VERSION_MAJOR = "17"', content)
        self.assertIn('LLVM_VERSION_MINOR = "0"', content)
        self.assertIn('LLVM_VERSION_PATCH = "3"', content)
        self.assertIn('LLVM_VERSION = "17.0.3"', content)
        self.assertIn("llvm_vars", content)

    def test_fallback_cmake(self) -> None:
        (self.src / "cmake" / "Modules" / "LLVMVersion.cmake").unlink()
        (self.src / "llvm" / "CMakeLists.txt").write_text(
            "set(LLVM_VERSION_MAJOR 16)\n"
            "set(LLVM_VERSION_MINOR 0)\n"
            "set(LLVM_VERSION_PATCH 0)\n"
            "set(CMAKE_CXX_STANDARD 17)\n"
        )

        generate_vars_bzl(self.src)
        content = (self.src / "vars.bzl").read_text()
        self.assertIn('LLVM_VERSION_MAJOR = "16"', content)


if __name__ == "__main__":
    unittest.main()
