#!/usr/bin/env python3
"""Render a version's presubmit.yml with bazelrc named-configs expanded inline.

The published llvm-project tarball ships a ``.bazelrc`` at its source root
(copied from upstream's ``utils/bazel/.bazelrc`` during the overlay step).
That file defines named configs like ``generic_clang``, ``clang-cl``, ``ci``,
etc. But the synthetic test workspace ``tools/run_presubmit.py`` creates for
bazelci has no ``.bazelrc`` of its own — so a bare ``--config=X`` reference
in ``versions/{X}/presubmit.yml`` would error at bazel-test time with
"Config value 'X' is not defined in any .rc file".

This tool reads a prepared source's ``.bazelrc`` and emits a presubmit.yml
where every ``--config=X`` reference has been recursively expanded into the
literal flags it would have selected. The result is fully self-contained:
bazel doesn't need to find the config definition anywhere.

The presubmit task structure (what platforms, what targets, what configs to
exercise) is hard-coded below — it's the canonical shape from
``versions/17.0.5/presubmit.yml``. Per-version variations (different
``bazel:`` matrix, different project-invariant flags) are CLI flags.

Usage:
    bazel run //tools:render_presubmit -- --llvm-version 17.0.5
    bazel run //tools:render_presubmit -- --llvm-version 17.0.5 --bazel-versions 7.x,8.x,9.x
    bazel run //tools:render_presubmit -- --llvm-version 17.0.5 --check  # diff-only
"""

from __future__ import annotations

import argparse
import collections
import difflib
import logging as std_logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

logging = std_logging.getLogger(__name__)

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent


def _workspace_root() -> Path:
    """Return the source tree this invocation should read from and write to.

    ``bazel run`` execs the script out of the runfiles tree, so
    ``Path(__file__).parent.parent`` resolves to the runfiles ``_main``
    directory rather than the checkout. Nothing this tool touches is a
    runfiles entry: ``versions/<v>/presubmit.yml`` is written back to the
    source tree, and the prepared source it reads ``.bazelrc`` from is
    generated at runtime by ``cherry_pick prepare`` (so it can never be a
    build-time ``data`` dep). ``BUILD_WORKSPACE_DIRECTORY`` — set by
    ``bazel run`` to the workspace root — is the right anchor for all of
    them. Fall back to the ``__file__``-relative root so running the script
    directly still works. Mirrors ``setup_presubmit._workspace_root``.
    """
    env = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if env:
        return Path(env)
    return _REPO_ROOT


def _user_cwd_path(s: str) -> Path:
    """Resolve a relative path against the shell's working directory.

    ``bazel run`` changes the process CWD to the runfiles dir before
    exec-ing the script, which would break relative paths the user typed on
    the command line. Anchor them to ``BUILD_WORKING_DIRECTORY`` (set by
    ``bazel run`` to the invoking shell's CWD), falling back to the current
    CWD when not under bazel. Mirrors ``release_notes._user_cwd_path``.
    """
    p = Path(s)
    if p.is_absolute():
        return p
    base = os.environ.get("BUILD_WORKING_DIRECTORY") or os.getcwd()
    return Path(base) / p


# Lines in .bazelrc look like:
#   <cmd>[:<config>] <flag> [<flag> ...]
# where <cmd> is one of: build, common, test, run, query, fetch, sync, etc.
# We collect flags from build/common/test directives (they all propagate to
# `bazel test` invocations).
_RC_LINE_RE = re.compile(r"^\s*(common|build|test)(?::([A-Za-z0-9_-]+))?\s+(.+?)\s*$")

# Commands whose flags we aggregate. `build` flags propagate to `test`; `common`
# applies to every bazel command; `test` is the most-specific layer.
_AGGREGATED_COMMANDS = frozenset({"common", "build", "test"})

# Project invariants — patches in versions/{X}/patches/ enforce these even
# when upstream's .bazelrc doesn't. Always appended after expansion.
PROJECT_INVARIANT_FLAGS: list[str] = [
    "--incompatible_disallow_empty_glob=true",
    "--incompatible_autoload_externally=",
]

# CI-ergonomic flags we want on every task regardless of which compiler
# config is in play. (`-nobuildkite` is the tag-filter pair that makes
# ``@llvm-project//...`` sustainable as a target expression.)
COMMON_TASK_FLAGS: list[str] = [
    "--build_tag_filters=-nobuildkite",
    "--test_tag_filters=-nobuildkite",
    "--keep_going",
]


def parse_bazelrc(path: Path) -> dict[str | None, list[str]]:
    """Parse a .bazelrc into a map of config_name → list of flags.

    The special key ``None`` collects unconditional flags (lines like
    ``build --some-flag`` with no ``:config`` suffix). Named-config lines
    (``build:generic_clang --some-flag``) are aggregated under their config
    name. Multiple lines for the same config are concatenated in source
    order. Backslash continuations are joined before parsing. Comment
    lines and blank lines are ignored.

    Only ``common``/``build``/``test`` directives are aggregated — ``run``
    and ``query`` flags don't propagate to ``bazel test`` invocations.
    Imports are NOT currently followed (none of llvm-project's bazelrc
    uses ``import``; if it ever does, this function will need extending).
    """
    raw = path.read_text(encoding="utf-8")
    # Join backslash-continued lines so a single logical directive ends up
    # on one parsed line. Trailing-backslash + newline + leading whitespace
    # collapses to a single space.
    joined = re.sub(r"\\\n[ \t]*", " ", raw)

    configs: dict[str | None, list[str]] = collections.defaultdict(list)
    for raw_line in joined.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _RC_LINE_RE.match(line)
        if not m:
            continue
        cmd, config_name, flags_str = m.groups()
        if cmd not in _AGGREGATED_COMMANDS:
            continue
        # shlex.split handles quoted args with embedded spaces and (with
        # comments=True) trims trailing `# ...` inline comments — `.bazelrc`
        # uses these heavily (e.g. ``build:msvc --copt=/wd4141 # inline used...``).
        flags = shlex.split(flags_str, comments=True)
        configs[config_name].extend(flags)
    return dict(configs)


def expand_config(
    configs: dict[str | None, list[str]],
    config_name: str,
    _seen: frozenset[str] | None = None,
) -> list[str]:
    """Recursively expand --config=X references in a named config's flags.

    Walks the flags for *config_name*. Each ``--config=Y`` flag is replaced
    by the expanded flags of ``Y`` (transitively). Cycles raise.
    """
    if _seen is None:
        _seen = frozenset()
    if config_name in _seen:
        chain = " → ".join(list(_seen) + [config_name])
        raise ValueError(f"Circular --config reference: {chain}")
    if config_name not in configs:
        raise KeyError(f"Config '{config_name}' not defined in bazelrc")

    next_seen = _seen | {config_name}
    result: list[str] = []
    for flag in configs[config_name]:
        if flag.startswith("--config="):
            inherited = flag[len("--config=") :]
            result.extend(expand_config(configs, inherited, next_seen))
        else:
            result.append(flag)
    return result


def flags_for(
    configs: dict[str | None, list[str]],
    config_name: str,
    dropped_flags: frozenset[str] = frozenset(),
) -> list[str]:
    """Compose the full flag list a task should pass to ``bazel test``.

    Order: unconditional bazelrc flags → expanded named-config flags →
    common task flags → project invariant flags. (Later flags override
    earlier ones in Bazel's command-line semantics.) Any flag in
    *dropped_flags* is filtered out at the end — used by tasks that
    can't satisfy a specific upstream `.bazelrc` assumption.
    """
    unconditional = configs.get(None, [])
    expanded = expand_config(configs, config_name)
    combined = [*unconditional, *expanded, *COMMON_TASK_FLAGS, *PROJECT_INVARIANT_FLAGS]
    if not dropped_flags:
        return combined
    return [f for f in combined if f not in dropped_flags]


# Per-task shell commands to run before `bazel test`. Upstream's
# `build:generic_clang` sets ``--linkopt=-fuse-ld=lld`` with the note
# "assume that anybody using clang also has lld available"; that holds
# for llvm-zorg's Google Bazel bot (which bakes ``lld-N`` into its
# custom image — see ``google-bazel-bot/docker/Dockerfile``) but not
# for bazelci's stock ``gcr.io/bazel-public/{debian10,ubuntu2004}``
# runners, where ``clang: error: invalid linker name in argument
# '-fuse-ld=lld'`` fires on every link. Install ``lld`` before the
# build so the flag resolves. ``debian10`` and ``ubuntu2004`` are both
# Debian-based and accept the same apt-get invocation.
_LINUX_CLANG_SHELL_COMMANDS: list[str] = [
    "sudo apt-get update",
    "sudo apt-get install -y --no-install-recommends lld",
]

# Flags to strip from the macOS clang tasks. Upstream's
# ``build:generic_clang`` ships ``--linkopt=-fuse-ld=lld``, but no
# upstream CI actually exercises bazel-on-macOS with lld: llvm-zorg's
# google-bazel-bot is Linux-only (no macOS Dockerfile, no macOS refs
# under ``google-bazel-bot/``), and the LLVM buildbot masters that do
# run macOS never invoke bazel. So the flag is an unverified claim on
# Mach-O. On the bazelci macOS runners it also can't be satisfied
# without extra setup: clang's ``-fuse-ld=lld`` looks for ``ld64.lld``
# (not ``ld.lld``) on Mach-O, and homebrew's ``lld`` formula is
# keg-only, so ``ld64.lld`` isn't on the sandbox PATH even after
# ``brew install lld``. Falling back to Apple's ``ld64`` is no worse
# than what upstream tests — which is nothing — so drop the flag.
_MACOS_CLANG_DROPPED_FLAGS: frozenset[str] = frozenset(
    {
        "--linkopt=-fuse-ld=lld",
        "--host_linkopt=-fuse-ld=lld",
    }
)

# Task structure — same shape across versions; only the .bazelrc expansion
# and the matrix entries differ. Each entry is:
#   (task_name, display_name, platform_expr, config_name,
#    shell_commands, dropped_flags)
# platform_expr is either a literal platform (e.g., "windows") or the
# matrix placeholder "${{ platform }}". shell_commands is a possibly-empty
# list of pre-`bazel test` commands (bazelci runs these via the task
# config's ``shell_commands`` field on Linux/macOS; omitted from the YAML
# entirely when empty). dropped_flags is a possibly-empty set of flags
# to strip after expansion (see ``_MACOS_CLANG_DROPPED_FLAGS``).
_TASK_SPEC: list[tuple[str, str, str, str, list[str], frozenset[str]]] = [
    (
        "run_tests",
        "bazel test //... (linux, clang)",
        "${{ platform }}",
        "generic_clang",
        _LINUX_CLANG_SHELL_COMMANDS,
        frozenset(),
    ),
    ("run_tests_gcc", "bazel test //... (linux, gcc)", "${{ platform }}", "generic_gcc", [], frozenset()),
    (
        "run_tests_macos",
        "bazel test //... (macOS x86_64, clang)",
        "macos",
        "generic_clang",
        [],
        _MACOS_CLANG_DROPPED_FLAGS,
    ),
    (
        "run_tests_macos_arm64",
        "bazel test //... (macOS arm64, clang)",
        "macos_arm64",
        "generic_clang",
        [],
        _MACOS_CLANG_DROPPED_FLAGS,
    ),
    ("run_tests_windows_clang_cl", "bazel test //... (windows, clang-cl)", "windows", "clang-cl", [], frozenset()),
    ("run_tests_windows_msvc", "bazel test //... (windows, msvc)", "windows", "msvc", [], frozenset()),
]


def render_presubmit(
    bazelrc_path: Path,
    linux_platforms: list[str],
    bazel_versions: list[str],
) -> dict[str, Any]:
    """Build the presubmit.yml dict for a given prepared source's .bazelrc."""
    configs = parse_bazelrc(bazelrc_path)

    tasks: dict[str, Any] = {}
    for task_name, display, platform, config_name, shell_commands, dropped_flags in _TASK_SPEC:
        task: dict[str, Any] = {
            "name": display,
            "platform": platform,
            "bazel": "${{ bazel }}",
        }
        if shell_commands:
            task["shell_commands"] = list(shell_commands)
        task["test_flags"] = flags_for(configs, config_name, dropped_flags)
        task["test_targets"] = ["@llvm-project//..."]
        tasks[task_name] = task

    return {
        "matrix": {
            "platform": linux_platforms,
            "bazel": bazel_versions,
        },
        "tasks": tasks,
    }


_HEADER = """\
# Generated by `bazel run //tools:render_presubmit -- --llvm-version {llvm_version}`.
# DO NOT EDIT BY HAND — re-run the renderer if you need to change the structure
# or to pick up upstream `.bazelrc` changes from this version's prepared source.
#
# The renderer expands `--config=X` references from this version's `.bazelrc`
# into the literal flags they select, so the test workspace bazelci synthesizes
# (which has no `.bazelrc` of its own) can resolve every flag without needing
# to find the config definition elsewhere.
"""


def emit_yaml(rendered: dict[str, Any], llvm_version: str) -> str:
    body: str = yaml.dump(rendered, sort_keys=False, default_flow_style=False, width=200)
    return _HEADER.format(llvm_version=llvm_version) + body


def _resolve_prepared_source(repo_root: Path, llvm_version: str, versions_dir: Path) -> Path:
    """Locate the prepared source tree for *llvm_version*.

    Returns ``<repo>/build/<llvm_version>/llvm-project-<version>.bzl``, where
    ``version`` is read from ``versions/<llvm_version>/version.txt``. Errors
    with a clear message if the tree doesn't exist — the user must run
    ``cherry_pick prepare --llvm-version <llvm_version>`` first.
    """
    version_file = versions_dir / llvm_version / "version.txt"
    if not version_file.is_file():
        raise SystemExit(f"ERROR: missing {version_file}")
    version = version_file.read_text().strip()

    tree = repo_root / "build" / llvm_version / f"llvm-project-{version}.bzl"
    if not (tree / ".bazelrc").is_file():
        raise SystemExit(
            f"ERROR: {tree}/.bazelrc not found. Run:\n"
            f"  bazel run //tools:cherry_pick -- prepare --llvm-version {llvm_version}"
        )
    return tree


def main() -> None:
    std_logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=std_logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--llvm-version", required=True, help="Version directory under versions/ (e.g. 17.0.5)")
    parser.add_argument(
        "--versions-dir",
        type=_user_cwd_path,
        help="Path to versions/ directory (default: <repo>/versions)",
    )
    parser.add_argument(
        "--bazelrc",
        type=_user_cwd_path,
        help=(
            "Path to a .bazelrc to render from (default: read from the prepared "
            "source at build/<llvm_version>/llvm-project-<version>.bzl/.bazelrc, "
            "which requires `cherry_pick prepare` to have been run). Use this "
            "flag for seed-time generation when no prepared source exists yet "
            "(e.g. the new-version auto-PR workflow can download upstream's "
            "utils/bazel/.bazelrc directly via curl)."
        ),
    )
    parser.add_argument(
        "--linux-platforms",
        default="debian10,ubuntu2004",
        help="Comma-separated linux platform values for the matrix (default: debian10,ubuntu2004)",
    )
    parser.add_argument(
        "--bazel-versions",
        default="7.x,8.x,9.x",
        help="Comma-separated bazel versions for the matrix (default: 7.x,8.x,9.x)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=_user_cwd_path,
        help="Write the rendered YAML here (default: versions/<llvm_version>/presubmit.yml)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print a diff vs. the existing file and exit non-zero on drift; do not write.",
    )
    args = parser.parse_args()

    repo_root = _workspace_root()
    versions_dir = args.versions_dir or repo_root / "versions"

    if args.bazelrc is not None:
        bazelrc_path = args.bazelrc
        if not bazelrc_path.is_file():
            raise SystemExit(f"ERROR: --bazelrc {bazelrc_path} does not exist")
    else:
        tree = _resolve_prepared_source(repo_root, args.llvm_version, versions_dir)
        bazelrc_path = tree / ".bazelrc"

    rendered = render_presubmit(
        bazelrc_path=bazelrc_path,
        linux_platforms=[p.strip() for p in args.linux_platforms.split(",") if p.strip()],
        bazel_versions=[b.strip() for b in args.bazel_versions.split(",") if b.strip()],
    )
    output_text = emit_yaml(rendered, args.llvm_version)

    target = args.output or versions_dir / args.llvm_version / "presubmit.yml"

    if args.check:
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        if existing == output_text:
            logging.info("%s is up to date.", target)
            return
        diff = "\n".join(
            difflib.unified_diff(
                existing.splitlines(),
                output_text.splitlines(),
                fromfile=str(target),
                tofile=str(target) + " (rendered)",
                lineterm="",
            )
        )
        sys.stdout.write(diff + "\n")
        raise SystemExit(
            f"ERROR: {target} is out of date with the renderer's output. Re-run without --check to update."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    # Always UTF-8: the rendered header contains non-ASCII punctuation, and
    # Python's default encoding is the locale codepage on Windows (cp1252),
    # which would silently write mojibake into a checked-in file.
    target.write_text(output_text, encoding="utf-8")
    logging.info("Wrote %s", target)


if __name__ == "__main__":
    _cwd = os.environ.get("BUILD_WORKING_DIRECTORY")
    if _cwd:
        os.chdir(_cwd)
    main()
