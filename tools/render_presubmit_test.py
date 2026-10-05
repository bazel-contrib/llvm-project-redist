#!/usr/bin/env python3
"""Unit tests for render_presubmit.py."""

import tempfile
import unittest
from pathlib import Path

from tools.render_presubmit import (
    expand_config,
    flags_for,
    parse_bazelrc,
    render_presubmit,
)


class ParseBazelrcTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.rc = Path(self.tmpdir.name) / ".bazelrc"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_unconditional_flag(self) -> None:
        self.rc.write_text("build --force_pic\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs[None], ["--force_pic"])

    def test_named_config(self) -> None:
        self.rc.write_text("build:generic_clang --cxxopt=-std=c++17\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs["generic_clang"], ["--cxxopt=-std=c++17"])

    def test_multiple_lines_aggregate(self) -> None:
        self.rc.write_text("build:generic_clang --cxxopt=-std=c++17\nbuild:generic_clang --copt=-Wall\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(
            configs["generic_clang"],
            ["--cxxopt=-std=c++17", "--copt=-Wall"],
        )

    def test_multiple_flags_per_line(self) -> None:
        self.rc.write_text("build:generic_clang --cxxopt=-std=c++17 --host_cxxopt=-std=c++17\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(
            configs["generic_clang"],
            ["--cxxopt=-std=c++17", "--host_cxxopt=-std=c++17"],
        )

    def test_skips_comments_and_blanks(self) -> None:
        self.rc.write_text("# header comment\n\nbuild --x\n    # indented comment\nbuild --y\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs[None], ["--x", "--y"])

    def test_aggregates_common_and_build_and_test(self) -> None:
        self.rc.write_text("common:ci --a\nbuild:ci --b\ntest:ci --c\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs["ci"], ["--a", "--b", "--c"])

    def test_ignores_run_and_query_commands(self) -> None:
        self.rc.write_text("run:ci --r\nquery:ci --q\nbuild:ci --b\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs["ci"], ["--b"])

    def test_backslash_continuation(self) -> None:
        self.rc.write_text("build:ci --a \\\n  --b \\\n  --c\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs["ci"], ["--a", "--b", "--c"])

    def test_inline_comment_after_flag(self) -> None:
        """Real bazelrc usage: ``build:msvc --copt=/WX  # Treat warnings as errors``."""
        self.rc.write_text(
            "build:msvc --copt=/WX --host_copt=/WX  # Treat warnings as errors...\n"
            "build:msvc --copt=/wd4141 # inline used more than once\n"
        )
        configs = parse_bazelrc(self.rc)
        # Comment content (`#`, `Treat`, `warnings`, etc.) MUST NOT leak in as flags.
        self.assertEqual(
            configs["msvc"],
            ["--copt=/WX", "--host_copt=/WX", "--copt=/wd4141"],
        )


class ExpandConfigTest(unittest.TestCase):
    def test_simple_no_inheritance(self) -> None:
        configs: dict[str | None, list[str]] = {"x": ["--a", "--b"]}
        self.assertEqual(expand_config(configs, "x"), ["--a", "--b"])

    def test_one_level_inheritance(self) -> None:
        configs: dict[str | None, list[str]] = {
            "base": ["--a"],
            "ci": ["--config=base", "--b"],
        }
        self.assertEqual(expand_config(configs, "ci"), ["--a", "--b"])

    def test_two_level_inheritance(self) -> None:
        configs: dict[str | None, list[str]] = {
            "base": ["--a"],
            "mid": ["--config=base", "--b"],
            "top": ["--config=mid", "--c"],
        }
        self.assertEqual(expand_config(configs, "top"), ["--a", "--b", "--c"])

    def test_unknown_config_errors(self) -> None:
        with self.assertRaises(KeyError):
            expand_config({"x": ["--a"]}, "missing")

    def test_cycle_detected(self) -> None:
        configs: dict[str | None, list[str]] = {
            "a": ["--config=b"],
            "b": ["--config=a"],
        }
        with self.assertRaises(ValueError):
            expand_config(configs, "a")


class FlagsForTest(unittest.TestCase):
    def test_includes_unconditional_then_config_then_common_then_invariants(self) -> None:
        configs: dict[str | None, list[str]] = {
            None: ["--unconditional"],
            "generic_clang": ["--cxxopt=-std=c++17"],
        }
        flags = flags_for(configs, "generic_clang")
        self.assertEqual(flags[0], "--unconditional")
        self.assertIn("--cxxopt=-std=c++17", flags)
        self.assertIn("--build_tag_filters=-nobuildkite", flags)
        self.assertIn("--incompatible_disallow_empty_glob=true", flags)
        # Invariants come last so they override anything earlier.
        self.assertEqual(flags[-1], "--incompatible_autoload_externally=")


class RenderPresubmitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.rc = Path(self.tmpdir.name) / ".bazelrc"
        # Minimal but realistic-shaped rc: every config the renderer asks
        # for must be present, plus an inheritance chain to verify expansion.
        self.rc.write_text(
            "build --force_pic\n"
            "\n"
            "build:generic_clang --cxxopt=-std=c++17\n"
            "build:generic_gcc --cxxopt=-std=c++17\n"
            "build:windows --cxxopt=/std:c++17\n"
            "build:clang-cl --config=windows\n"
            "build:clang-cl --compiler=clang-cl\n"
            "build:msvc --config=windows\n"
            "build:msvc --copt=/WX\n"
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_emits_all_expected_tasks(self) -> None:
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        self.assertEqual(
            set(out["tasks"].keys()),
            {
                "run_tests",
                "run_tests_gcc",
                "run_tests_macos",
                "run_tests_macos_arm64",
                "run_tests_windows_clang_cl",
                "run_tests_windows_msvc",
            },
        )

    def test_clang_cl_inherits_windows(self) -> None:
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        flags = out["tasks"]["run_tests_windows_clang_cl"]["test_flags"]
        # Inherited from `windows`:
        self.assertIn("--cxxopt=/std:c++17", flags)
        # Direct on `clang-cl`:
        self.assertIn("--compiler=clang-cl", flags)
        # NOT the --config=windows reference itself (it was expanded):
        self.assertNotIn("--config=windows", flags)

    def test_msvc_inherits_windows(self) -> None:
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        flags = out["tasks"]["run_tests_windows_msvc"]["test_flags"]
        self.assertIn("--cxxopt=/std:c++17", flags)
        self.assertIn("--copt=/WX", flags)

    def test_unconditional_flags_apply_to_every_task(self) -> None:
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        for name, task in out["tasks"].items():
            with self.subTest(task=name):
                self.assertIn("--force_pic", task["test_flags"])

    def test_matrix_uses_provided_values(self) -> None:
        out = render_presubmit(self.rc, ["debian10", "ubuntu2404"], ["7.x", "9.x"])
        self.assertEqual(out["matrix"]["platform"], ["debian10", "ubuntu2404"])
        self.assertEqual(out["matrix"]["bazel"], ["7.x", "9.x"])


if __name__ == "__main__":
    unittest.main()
