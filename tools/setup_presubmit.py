#!/usr/bin/env python3
"""Set up repos for reproducing presubmit builds locally.

Prepares the LLVM source for a given version and prints the bazel commands
to run presubmit tests on the host platform.

Usage:
    bazel run //tools:setup_presubmit -- 17.0.3
    bazel run //tools:setup_presubmit -- --module llvm-project@17.0.3
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.presubmit_logic import read_version_string


def _workspace_root() -> Path:
    env = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if env:
        return Path(env)
    return _REPO_ROOT


def _host_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def _task_platform(task: dict[str, object]) -> str:
    platform = str(task.get("platform", ""))
    if platform.startswith("macos"):
        return "macos"
    if platform.startswith("windows"):
        return "windows"
    return "linux"


def _load_yaml(path: Path) -> dict[str, object]:
    try:
        import yaml
    except ImportError as e:
        logging.error("PyYAML is required. Use:\n  bazel run //tools:setup_presubmit -- ...\nor: pip install pyyaml")
        raise SystemExit(1) from e
    result: dict[str, object] = yaml.safe_load(path.read_text()) or {}
    return result


def _expand_matrix(doc: dict[str, object]) -> dict[str, dict[str, object]]:
    """Expand matrix variables in tasks, returning {task_name: concrete_config}."""
    matrix = doc.get("matrix", {})
    tasks = doc.get("tasks", {})
    if not matrix or not tasks:
        if isinstance(tasks, dict):
            return dict(tasks)
        return {}

    if not isinstance(matrix, dict) or not isinstance(tasks, dict):
        return {}

    matrix_var_re = re.compile(r"\$\{\{\s*(\w+)\s*\}\}")

    def substitute(value: object, row: dict[str, object]) -> object:
        if isinstance(value, str):
            m = matrix_var_re.fullmatch(value.strip())
            if m and m.group(1) in row:
                return row[m.group(1)]
            return matrix_var_re.sub(
                lambda mm: str(row[mm.group(1)]) if mm.group(1) in row else mm.group(0),
                value,
            )
        if isinstance(value, list):
            return [substitute(v, row) for v in value]
        if isinstance(value, dict):
            return {k: substitute(v, row) for k, v in value.items()}
        return value

    def referenced_keys(task: object) -> set[str]:
        found: set[str] = set()

        def walk(x: object) -> None:
            if isinstance(x, str):
                for m in matrix_var_re.finditer(x):
                    found.add(m.group(1))
            elif isinstance(x, list):
                for i in x:
                    walk(i)
            elif isinstance(x, dict):
                for v in x.values():
                    walk(v)

        walk(task)
        return found & set(matrix.keys())

    def cartesian(mat: dict[str, object]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = [{}]
        for key, vals in mat.items():
            if not isinstance(vals, list):
                continue
            rows = [{**r, key: v} for r in rows for v in vals]
        return rows

    expanded: dict[str, dict[str, object]] = {}
    for task_id, task in tasks.items():
        if not isinstance(task, dict):
            continue
        used = referenced_keys(task)
        if used:
            task_matrix = {k: matrix[k] for k in used}
            for i, row in enumerate(cartesian(task_matrix), 1):
                name = f"{task_id}_config_{i:02d}" if len(cartesian(task_matrix)) > 1 else task_id
                substituted = substitute(task, row)
                if isinstance(substituted, dict):
                    expanded[name] = substituted
        else:
            expanded[task_id] = task
    return expanded


def _create_anonymous_repo(test_ws: Path, source_dir: Path, version: str) -> None:
    """Create an anonymous Bazel module that depends on llvm-project."""
    shutil.rmtree(test_ws, ignore_errors=True)
    test_ws.mkdir(exist_ok=True, parents=True)
    (test_ws / "WORKSPACE").touch()
    (test_ws / "BUILD").touch()
    rel_source = Path(os.path.relpath(source_dir, test_ws)).as_posix()
    (test_ws / "MODULE.bazel").write_text(
        f'bazel_dep(name = "llvm-project", version = "{version}")\n'
        f"local_path_override(\n"
        f'    module_name = "llvm-project",\n'
        f'    path = "{rel_source}",\n'
        f")\n"
    )
    (test_ws / ".bazelrc").write_text(
        "common --enable_bzlmod\n"
        "common --announce_rc\n"
        "common --repository_cache=\n"
        "common --lockfile_mode=off\n"
        "build --verbose_failures\n"
    )


def _print_build_instruction(
    llvm_version: str,
    repo_root: Path,
    task_configs: dict[str, dict[str, object]],
    presubmit_yml: Path,
) -> None:
    host = _host_platform()

    task_name: str | None = None
    build_flags: list[str] = []
    build_targets: list[str] = []
    test_flags: list[str] = []
    test_targets: list[str] = []
    bazel_version: object = None
    for task_id, task in task_configs.items():
        if not isinstance(task, dict):
            continue
        if _task_platform(task) == host:
            task_name_val = task.get("name", task_id)
            task_name = str(task_name_val) if task_name_val is not None else task_id
            raw_build_flags = task.get("build_flags", [])
            build_flags = list(raw_build_flags) if isinstance(raw_build_flags, list) else []
            raw_build_targets = task.get("build_targets", [])
            build_targets = list(raw_build_targets) if isinstance(raw_build_targets, list) else []
            raw_test_flags = task.get("test_flags", [])
            test_flags = list(raw_test_flags) if isinstance(raw_test_flags, list) else []
            raw_test_targets = task.get("test_targets", [])
            test_targets = list(raw_test_targets) if isinstance(raw_test_targets, list) else []
            bazel_version = task.get("bazel")
            break

    if not task_name:
        print(f"\nNo task found for the host platform: {host}")
        print(f"Please check {presubmit_yml} on which targets to build.\n")
        return

    if not build_targets and not test_targets:
        print("\nNo build or test targets found in the task config.")
        print(f"Please check {presubmit_yml} on which targets to build.\n")
        return

    print(
        f'\nTo reproduce task "{task_name}" on {host} with Bazel {bazel_version}, '
        f"follow these steps (make sure Bazelisk is installed as bazel):\n"
    )

    if bazel_version:
        if host == "windows":
            print(f"    set USE_BAZEL_VERSION={bazel_version}")
        else:
            print(f"    export USE_BAZEL_VERSION={bazel_version}")

    print(f"    cd {repo_root}")
    print("    bazel clean --expunge")

    if build_targets:
        flags = " ".join(str(f) for f in build_flags)
        targets = " ".join(str(t) for t in build_targets)
        print(f"    bazel --nosystem_rc --nohome_rc build {flags} -- {targets}")
    if test_targets:
        flags = " ".join(str(f) for f in test_flags)
        targets = " ".join(str(t) for t in test_targets)
        print(f"    bazel --nosystem_rc --nohome_rc test {flags} -- {targets}")

    print(f"\nMake sure to check {presubmit_yml} for additional build and test configurations.\n")


def cmd_local(llvm_version: str, workspace: Path) -> int:
    versions_dir = workspace / "versions"
    version_dir = versions_dir / llvm_version

    if not version_dir.is_dir():
        logging.error("Version directory not found: %s", version_dir)
        return 1

    presubmit = version_dir / "presubmit.yml"
    if not presubmit.is_file():
        logging.error("Presubmit YAML file does not exist: %s", presubmit)
        return 1

    version = read_version_string(llvm_version, str(versions_dir))
    source_dir = workspace / f"llvm-project-{version}.bzl"
    test_ws = workspace / "temp_test_repos" / "llvm-project" / llvm_version / "anonymous_module"

    # Prepare source
    #
    # build.py runs as a bare script rather than through its py_binary
    # bootstrap, so it does not inherit this process's in-memory sys.path.
    # Export it as PYTHONPATH so build.py's own deps (zstandard) resolve.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)

    logging.info("Preparing source for LLVM %s...", llvm_version)
    subprocess.run(
        [
            sys.executable,
            str(workspace / "tools" / "build.py"),
            f"--llvm-version={llvm_version}",
            f"--version={version}",
            f"--versions-dir={versions_dir}",
            f"--output-dir={workspace}",
            "--prepare-only",
        ],
        check=True,
        env=env,
    )

    # Create anonymous test workspace
    logging.info("Creating anonymous module repo at: %s", test_ws)
    _create_anonymous_repo(test_ws, source_dir, version)
    logging.info("Anonymous module repo ready at: %s", test_ws)

    # Read, expand, and print instructions
    doc = _load_yaml(presubmit)
    tasks = _expand_matrix(doc)
    _print_build_instruction(llvm_version, test_ws, tasks, presubmit)

    return 0


def _parse_version(value: str) -> str:
    """Extract LLVM version from a positional arg or ``--module`` value."""
    if "@" in value:
        module, ver = value.rsplit("@", 1)
        if module != "llvm-project":
            raise SystemExit(f"ERROR: --module must be llvm-project@<version>, got {value}")
        value = ver
    value = re.sub(r"\.bcr\.\d+$", "", value)
    return value


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Setup repos for reproducing presubmit builds locally.",
        usage="bazel run //tools:setup_presubmit -- [VERSION | --module llvm-project@VERSION]",
    )
    parser.add_argument(
        "version",
        nargs="?",
        type=str,
        help="LLVM version to test (e.g. 17.0.3)",
    )
    parser.add_argument(
        "--module",
        type=str,
        help="Module and version for BCR compatibility (e.g. llvm-project@17.0.3)",
    )

    args = parser.parse_args(argv)

    if args.version and args.module:
        parser.error("Specify VERSION or --module, not both")

    raw = args.version or args.module
    if not raw:
        parser.error("VERSION is required (positional or via --module)")

    llvm_version = _parse_version(raw)
    workspace = _workspace_root()
    logging.info("Testing using repo at: %s", workspace)

    return cmd_local(llvm_version, workspace)


if __name__ == "__main__":
    raise SystemExit(main())
