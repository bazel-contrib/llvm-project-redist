#!/usr/bin/env python3
"""Generate per-version Buildkite presubmit steps for changed ``versions/``.

This script is appended to the Buildkite pipeline command after
``bazelci.py project_pipeline`` so it can dynamically upload one step per
expanded task in each changed version's ``versions/X/presubmit.yml`` — each
preparing the LLVM source then delegating to ``bazelci.py runner`` for the
actual build/test execution.

The always-on ``bazel test //...`` repo-tests step is *not* generated
here; it's a task in ``.bazelci/presubmit.yml`` that ``bazelci.py
project_pipeline`` expands (its canonical default config path), so
Buildkite always has at least one statically-declared step regardless of
whether ``versions/`` changed. Keeping per-version expansion separate
preserves the 1:1 mirror with what bazel-central-registry runs after
publishing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from shutil import which
from types import ModuleType

from tools.presubmit_logic import (
    changed_version_dirs,
    read_version_string,
)

_BAZELCI_URL = "https://raw.githubusercontent.com/bazelbuild/continuous-integration/master/buildkite/bazelci.py"


def _load_bazelci() -> ModuleType:
    """Download ``bazelci.py`` and import it for pipeline step construction."""
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
        urllib.request.urlretrieve(_BAZELCI_URL, tmp.name)
        spec = importlib.util.spec_from_file_location("bazelci", tmp.name)
        if spec is None or spec.loader is None:
            raise RuntimeError("Failed to create module spec for bazelci.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(out.stdout.strip())


def _is_windows_platform(platform: str) -> bool:
    return platform.lower().startswith("windows")


def _original_task_name(expanded_name: str) -> str:
    """Strip ``_config_NN`` suffix added by ``bazelci.expand_task_config``."""
    return re.sub(r"_config_\d+$", "", expanded_name)


def _build_step_commands(
    bazelci_mod: ModuleType,
    llvm_version: str,
    version: str,
    task_name: str,
    task_config: dict[str, object],
    source_dir_name: str,
    platform: str,
) -> list[str]:
    """Shell commands for one Buildkite step (POSIX or batch)."""
    test_workspace = f"{source_dir_name}.test_workspace"
    build_args = " ".join(
        [
            "bazel",
            "run",
            "//tools:build",
            "--",
            f"--llvm-version={llvm_version}",
            f"--version={version}",
            "--versions-dir=versions",
            "--output-dir=.",
            "--prepare-only",
        ]
    )

    step_config = json.dumps({"tasks": {task_name: task_config}})

    module_bazel = (
        f'bazel_dep(name = "llvm-project", version = "{version}")\n'
        f"local_path_override(\n"
        f'    module_name = "llvm-project",\n'
        f'    path = "../{source_dir_name}",\n'
        f")\n"
    )

    if _is_windows_platform(platform):
        setup_workspace = (
            f"mkdir {test_workspace} 2>nul & "
            f"echo.> {test_workspace}\\WORKSPACE & "
            f"echo.> {test_workspace}\\BUILD & "
            f'echo bazel_dep^(name = "llvm-project", version = "{version}"^)'
            f" > {test_workspace}\\MODULE.bazel & "
            f'echo local_path_override^(module_name = "llvm-project", path = "../{source_dir_name}"^)'
            f" >> {test_workspace}\\MODULE.bazel"
        )
        write_config = f"echo {step_config} > .task_config.json"
        runner_cmd = (
            f"python3 bazelci.py runner"
            f" --task={task_name}"
            f" --file_config=%CD%\\.task_config.json"
            f" --repo_location={test_workspace}"
        )
    else:
        setup_workspace = (
            f"mkdir -p {test_workspace} && "
            f"touch {test_workspace}/WORKSPACE && "
            f"touch {test_workspace}/BUILD && "
            f"cat > {test_workspace}/MODULE.bazel <<'MODULE_EOF'\n{module_bazel}MODULE_EOF"
        )
        write_config = f"cat > .task_config.json <<'BAZELCI_TASK_EOF'\n{step_config}\nBAZELCI_TASK_EOF"
        runner_cmd = (
            f"python3 bazelci.py runner"
            f" --task={task_name}"
            f" --file_config=$(pwd)/.task_config.json"
            f" --repo_location={test_workspace}"
        )

    return [
        bazelci_mod.fetch_ci_scripts_command(),
        build_args,
        setup_workspace,
        write_config,
        runner_cmd,
    ]


def cmd_pipeline(
    versions_dir: Path,
    git_base_ref: str,
    dry_run: bool,
) -> int:
    repo_root = _repo_root()
    rel_versions = versions_dir
    if not rel_versions.is_absolute():
        rel_versions = (repo_root / rel_versions).resolve()

    bazelci_mod = _load_bazelci()

    try:
        changed = changed_version_dirs(git_base_ref, str(repo_root))
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Always-on repo tests come from .bazelci/presubmit.yml expanded by
    # bazelci.py project_pipeline — this script only emits the dynamic
    # per-version steps. When no versions/ dirs changed, we upload nothing;
    # the repo_tests step from project_pipeline carries the build either way.
    steps: list[dict[str, object]] = []
    for llvm_version in sorted(changed):
        version = read_version_string(llvm_version, str(rel_versions))
        source_dir_name = f"llvm-project-{version}.bzl"

        presubmit = repo_root / "versions" / llvm_version / "presubmit.yml"
        if not presubmit.is_file():
            print(f"ERROR: missing {presubmit}", file=sys.stderr)
            return 1

        config = bazelci_mod.load_config(None, str(presubmit))

        for expanded_name, task_config in config.get("tasks", {}).items():
            platform = bazelci_mod.get_platform_for_task(expanded_name, task_config)
            task_name = _original_task_name(expanded_name)
            bazel_version = task_config.get("bazel", "")
            label = f"{llvm_version} / {task_name} ({platform}, {bazel_version})"
            commands = _build_step_commands(
                bazelci_mod,
                llvm_version,
                version,
                task_name,
                task_config,
                source_dir_name,
                platform,
            )
            steps.append(bazelci_mod.create_step(label, commands, platform))

    if not steps:
        # Nothing to upload — the repo_tests step from root presubmit.yml is
        # independent and is already running. An empty upload would be a no-op;
        # skip it to keep the build log clean.
        print("No changed versions/ directories; nothing to upload.")
        return 0

    payload = {"steps": steps}
    text = json.dumps(payload, indent=2)
    if dry_run:
        print(text)
        return 0

    agent = which("buildkite-agent")
    if not agent:
        print("ERROR: buildkite-agent not found in PATH", file=sys.stderr)
        return 1
    print("Uploading dynamic pipeline to Buildkite...", flush=True)
    subprocess.run(
        [agent, "pipeline", "upload"],
        input=text.encode(),
        cwd=repo_root,
        check=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pipeline",
        action="store_true",
        required=True,
        help="Generate and upload Buildkite dynamic pipeline steps",
    )
    parser.add_argument(
        "--versions-dir",
        type=Path,
        default=Path("versions"),
        help="versions/ directory (default: versions)",
    )
    parser.add_argument(
        "--git-base-ref",
        default=(os.environ.get("BUILDKITE_PULL_REQUEST_BASE_BRANCH") or "main"),
        help="Git ref for detecting changed versions (default: BUILDKITE_PULL_REQUEST_BASE_BRANCH or main)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print JSON instead of uploading",
    )

    args = parser.parse_args(argv)
    return cmd_pipeline(args.versions_dir, args.git_base_ref, args.dry_run)


if __name__ == "__main__":
    _cwd = os.environ.get("BUILD_WORKING_DIRECTORY")
    if _cwd:
        os.chdir(_cwd)
    raise SystemExit(main())
