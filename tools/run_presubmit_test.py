"""Unit tests for ``presubmit_logic`` and ``run_presubmit`` pipeline generation."""

from __future__ import annotations

import types
import unittest
from typing import Any

from tools import presubmit_logic as pl
from tools import run_presubmit as rp


def _fake_bazelci() -> types.ModuleType:
    """Return a minimal mock of the ``bazelci`` module for testing."""
    mod = types.ModuleType("bazelci")
    mod.fetch_ci_scripts_command = lambda: "curl -sS https://example.com/bazelci.py -o bazelci.py"  # type: ignore[attr-defined]
    mod.PLATFORMS = {  # type: ignore[attr-defined]
        "debian10": {
            "docker-image": "gcr.io/bazel-public/debian10-java11",
            "queue": "default",
            "python": "python3",
            "emoji-name": ":debian: Debian 10 Buster (OpenJDK 11, gcc 8.3.0)",
        },
        "ubuntu2004": {
            "docker-image": "gcr.io/bazel-public/ubuntu2004-java11",
            "queue": "default",
            "python": "python3",
            "emoji-name": ":ubuntu: Ubuntu 20.04 LTS",
        },
        "macos_arm64": {
            "queue": "macos_arm64",
            "python": "python3",
            "emoji-name": ":darwin: macOS arm64",
        },
        "windows": {
            "queue": "windows",
            "python": "python",
            "emoji-name": ":windows: Windows",
        },
    }

    def create_step(label: str, commands: list[str], platform: str, **_kwargs: Any) -> dict[str, Any]:
        if "docker-image" in mod.PLATFORMS[platform]:
            return {
                "label": label,
                "command": commands,
                "agents": {"queue": mod.PLATFORMS[platform].get("queue", "default")},
                "plugins": {"docker#v3.8.0": {"image": mod.PLATFORMS[platform]["docker-image"]}},
            }
        return {
            "label": label,
            "command": commands,
            "agents": {"queue": mod.PLATFORMS[platform]["queue"]},
        }

    mod.create_step = create_step  # type: ignore[attr-defined]

    def get_platform_for_task(task: str, task_config: dict[str, Any]) -> str:
        result: str = task_config.get("platform", task)
        return result

    mod.get_platform_for_task = get_platform_for_task  # type: ignore[attr-defined]
    return mod


class VersionDetectionTest(unittest.TestCase):
    def test_versions_from_git_diff_lines(self) -> None:
        out = """versions/17.0.3/presubmit.yml
versions/17.0.3/version.txt
README.md
versions/20.1.0/patches/001_x.patch
"""
        self.assertEqual(pl.versions_from_git_diff_lines(out), ["17.0.3", "20.1.0"])


class BuildStepCommandsTest(unittest.TestCase):
    def test_uses_bazelci_runner(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {"platform": "debian10", "bazel": "8.x", "test_targets": ["//a"]}
        cmds = rp._build_step_commands(
            mod,
            "17.0.3",
            "17.0.3.bcr.1",
            "run_tests",
            task_config,
            "llvm-project-17.0.3.bcr.1.bzl",
            "debian10",
        )
        self.assertTrue(any("bazelci.py" in c for c in cmds))
        self.assertTrue(any("--prepare-only" in c for c in cmds))
        runner_cmd = [c for c in cmds if "bazelci.py runner" in c]
        self.assertEqual(len(runner_cmd), 1)
        self.assertIn("--task=run_tests", runner_cmd[0])
        self.assertIn(
            "--repo_location=llvm-project-17.0.3.bcr.1.bzl.test_workspace",
            runner_cmd[0],
        )

    def test_creates_test_workspace(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {"platform": "debian10", "bazel": "8.x", "test_targets": ["//a"]}
        cmds = rp._build_step_commands(
            mod,
            "17.0.3",
            "17.0.3",
            "run_tests",
            task_config,
            "src",
            "debian10",
        )
        ws_cmd = [c for c in cmds if "MODULE.bazel" in c][0]
        self.assertIn("llvm-project", ws_cmd)
        self.assertIn("local_path_override", ws_cmd)
        self.assertIn("src.test_workspace", ws_cmd)

    def test_writes_concrete_config(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {"platform": "debian10", "bazel": "8.x", "test_targets": ["//a"]}
        cmds = rp._build_step_commands(
            mod,
            "17.0.3",
            "17.0.3",
            "run_tests",
            task_config,
            "src",
            "debian10",
        )
        config_cmd = [c for c in cmds if ".task_config.json" in c and "runner" not in c][0]
        self.assertIn('"run_tests"', config_cmd)
        self.assertIn('"test_targets"', config_cmd)

    def test_original_task_name(self) -> None:
        self.assertEqual(rp._original_task_name("run_tests_config_01"), "run_tests")
        self.assertEqual(rp._original_task_name("run_tests_config_12"), "run_tests")
        self.assertEqual(rp._original_task_name("run_tests"), "run_tests")

    def test_windows_uses_batch_syntax(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {"platform": "windows", "bazel": "8.x", "test_targets": ["//a"]}
        cmds = rp._build_step_commands(
            mod,
            "17.0.3",
            "17.0.3",
            "run_tests",
            task_config,
            "src",
            "windows",
        )
        runner_cmd = [c for c in cmds if "bazelci.py runner" in c][0]
        self.assertIn("%CD%", runner_cmd)
        self.assertNotIn("$(pwd)", runner_cmd)

    def test_windows_escapes_parens_in_module_bazel(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {"platform": "windows", "bazel": "8.x", "test_targets": ["//a"]}
        cmds = rp._build_step_commands(
            mod,
            "17.0.3",
            "17.0.3",
            "run_tests",
            task_config,
            "src",
            "windows",
        )
        ws_cmd = [c for c in cmds if "MODULE.bazel" in c][0]
        self.assertIn("^(", ws_cmd)
        self.assertIn("^)", ws_cmd)
        self.assertNotIn("(echo", ws_cmd)

    def test_step_linux_has_docker(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {"platform": "debian10", "bazel": "8.x", "test_targets": ["//a"]}
        cmds = rp._build_step_commands(
            mod,
            "17.0.3",
            "17.0.3",
            "run_tests",
            task_config,
            "src",
            "debian10",
        )
        step = mod.create_step("test", cmds, "debian10")
        self.assertIn("plugins", step)
        self.assertIn("docker#v3.8.0", step["plugins"])

    def test_step_macos_no_docker(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {
            "platform": "macos_arm64",
            "bazel": "8.x",
            "test_targets": ["//a"],
        }
        cmds = rp._build_step_commands(
            mod,
            "17.0.3",
            "17.0.3",
            "run_tests",
            task_config,
            "src",
            "macos_arm64",
        )
        step = mod.create_step("test", cmds, "macos_arm64")
        self.assertNotIn("plugins", step)
        self.assertEqual(step["agents"]["queue"], "macos_arm64")


class StepLabelTest(unittest.TestCase):
    """Step labels must read like bazel-central-registry's presubmit steps.

    BCR's ``add_presubmit_jobs`` renders
    ``:bazel:<ver> - <module>@<version> - <platform emoji-name> - <task name>``;
    the dynamic steps here run the same ``presubmit.yml`` and should be
    indistinguishable in the Buildkite UI.
    """

    def test_matches_bcr_format(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {
            "name": "bazel test //... (windows, msvc)",
            "platform": "windows",
            "bazel": "9.x",
            "test_targets": ["//a"],
        }
        label = rp._step_label(mod, "17.0.5", "run_tests_windows_msvc", task_config, "windows")
        self.assertEqual(
            label,
            ":bazel:9.x - llvm-project@17.0.5 - :windows: Windows - bazel test //... (windows, msvc)",
        )

    def test_uses_module_version_not_directory(self) -> None:
        """The ``@version`` segment is the BCR module version from version.txt."""
        mod = _fake_bazelci()
        task_config: dict[str, object] = {"name": "t", "platform": "macos_arm64", "bazel": "8.x"}
        label = rp._step_label(mod, "17.0.5.bcr.1", "run_tests_macos_arm64", task_config, "macos_arm64")
        self.assertEqual(label, ":bazel:8.x - llvm-project@17.0.5.bcr.1 - :darwin: macOS arm64 - t")

    def test_falls_back_to_task_key_without_name(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {"platform": "debian10", "bazel": "7.x"}
        label = rp._step_label(mod, "17.0.5", "run_tests", task_config, "debian10")
        self.assertEqual(
            label,
            ":bazel:7.x - llvm-project@17.0.5 - :debian: Debian 10 Buster (OpenJDK 11, gcc 8.3.0) - run_tests",
        )

    def test_omits_bazel_prefix_without_version(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {"name": "t", "platform": "debian10"}
        label = rp._step_label(mod, "17.0.5", "run_tests", task_config, "debian10")
        self.assertEqual(label, "llvm-project@17.0.5 - :debian: Debian 10 Buster (OpenJDK 11, gcc 8.3.0) - t")

    def test_unknown_platform_falls_back_to_key(self) -> None:
        mod = _fake_bazelci()
        task_config: dict[str, object] = {"name": "t", "platform": "rockylinux8", "bazel": "9.x"}
        label = rp._step_label(mod, "17.0.5", "run_tests", task_config, "rockylinux8")
        self.assertEqual(label, ":bazel:9.x - llvm-project@17.0.5 - rockylinux8 - t")


if __name__ == "__main__":
    unittest.main()
