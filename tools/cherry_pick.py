#!/usr/bin/env python3
"""Tools for authoring patches against post-overlay LLVM source trees.

Patches in this repo apply to the *post-overlay* source tree, where files
under ``utils/bazel/llvm-project-overlay/`` have been copied to the source
root. Two subcommands:

  prepare
      Materialize the post-overlay source tree at
      ``build/{llvm_version}/llvm-project-{version}.bzl/`` and initialize
      a git repo with a baseline commit. Edit files in place, then export
      changes with ``git -C <tree> diff > versions/{version}/patches/NNN_*.patch``.

  pick
      Fetch an upstream llvm-project commit, strip the overlay prefix from
      every diff path, materialize the tree (via ``prepare`` semantics),
      and attempt to apply the rewritten patch. On clean apply, writes the
      canonical diff to ``versions/{version}/patches/NNN_*.patch``. On
      conflicts, leaves the tree with git-style conflict markers
      (``<<<<<<<``/``=======``/``>>>>>>>``) for the user to resolve.

Usage:
    bazel run //tools:cherry_pick -- prepare [options]
    bazel run //tools:cherry_pick -- pick <commit-or-url> [options]

Examples:
    # Materialize the highest version's source tree for manual editing
    bazel run //tools:cherry_pick -- prepare

    # Cherry-pick a commit into the latest version directory
    bazel run //tools:cherry_pick -- pick abc1234

    # Pick into a specific version with a custom description
    bazel run //tools:cherry_pick -- pick \\
        https://github.com/llvm/llvm-project/commit/abc1234 \\
        --llvm-version 17.0.3 \\
        --description fix_build

    # Skip materialization; just fetch + rewrite + write the patch
    bazel run //tools:cherry_pick -- pick abc1234 --no-apply
"""

import argparse
import logging as std_logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

logging = std_logging.getLogger(__name__)

OVERLAY_PREFIX = "utils/bazel/llvm-project-overlay/"
PATCH_URL_TEMPLATE = "https://github.com/llvm/llvm-project/commit/{sha}.patch"

_SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b", re.IGNORECASE)
_SUBJECT_RE = re.compile(r"^Subject: (?:\[PATCH(?: \d+/\d+)?\] )?(.+)$", re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Upstream subjects for overlay changes almost always start with "[bazel]" or
# "bazel:". The prefix carries no information here, so strip it before slugging.
_BAZEL_PREFIX_RE = re.compile(r"^\s*(?:\[bazel\]|bazel:)\s*", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--llvm-version",
            help="Target version directory under versions/ (default: highest existing)",
        )
        p.add_argument(
            "--versions-dir",
            type=Path,
            help="Path to versions/ directory (default: <repo>/versions)",
        )

    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser(
        "prepare",
        help="Materialize the post-overlay source tree with a git baseline",
    )
    add_common(prepare)

    pick = sub.add_parser("pick", help="Fetch an upstream commit and apply it as a patch")
    pick.add_argument("commit", help="Commit SHA or commit URL on llvm/llvm-project")
    add_common(pick)
    pick.add_argument(
        "--description",
        help="Patch description used in the filename slug (default: derived from commit subject)",
    )
    pick.add_argument(
        "--no-apply",
        action="store_true",
        help="Just write the rewritten patch; do not materialize a source tree or try to apply",
    )

    return parser.parse_args()


def extract_sha(commit_or_url: str) -> str:
    """Extract a 7-40 char hex SHA from a commit string or URL."""
    m = _SHA_RE.search(commit_or_url)
    if not m:
        raise SystemExit(f"ERROR: could not find a commit SHA in '{commit_or_url}'")
    return m.group(1).lower()


def fetch_patch(sha: str) -> str:
    """Download the git-format patch for *sha* from llvm/llvm-project."""
    url = PATCH_URL_TEMPLATE.format(sha=sha)
    with urlopen(url) as resp:
        if resp.status != 200:
            raise SystemExit(f"ERROR: HTTP {resp.status} fetching {url}")
        body: bytes = resp.read()
        return body.decode("utf-8")


def rewrite_paths(patch: str) -> str:
    """Strip the overlay prefix from every path in *patch*."""
    return patch.replace(OVERLAY_PREFIX, "")


def latest_version(versions_dir: Path) -> str:
    """Return the highest-numbered version directory name."""
    candidates = sorted(
        (d.name for d in versions_dir.iterdir() if d.is_dir()),
        key=lambda n: tuple(int(p) for p in n.split(".") if p.isdigit()),
    )
    if not candidates:
        raise SystemExit(f"ERROR: no version directories under {versions_dir}")
    return candidates[-1]


def next_patch_number(patches_dir: Path) -> int:
    """Return the next NNN slot in *patches_dir*."""
    if not patches_dir.is_dir():
        return 1
    nums = []
    for p in patches_dir.glob("*.patch"):
        m = re.match(r"^(\d{3})[_-]", p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def derive_description(patch: str) -> str:
    """Derive a snake_case slug from the patch's Subject: line."""
    m = _SUBJECT_RE.search(patch)
    subject = m.group(1) if m else "cherry_pick"
    subject = _BAZEL_PREFIX_RE.sub("", subject)
    slug = _SLUG_RE.sub("_", subject.lower()).strip("_")
    return slug[:50] or "cherry_pick"


def _git(tree: Path, *args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(tree), *args]
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def materialize_baseline(*, llvm_version: str, version: str, versions_dir: Path, repo_root: Path) -> Path:
    """Materialize the post-overlay source tree with a git baseline commit.

    Reuses ``build/{llvm_version}/llvm-project-{version}.bzl`` if it already
    has a ``.git`` directory; otherwise materializes via ``prepare_source``
    and initializes a fresh git repo on top.
    """
    from tools.build import prepare_source

    build_dir = repo_root / "build" / llvm_version
    tree = build_dir / f"llvm-project-{version}.bzl"

    if (tree / ".git").is_dir():
        logging.info("Reusing materialized tree at %s", tree)
        # Reset any leftover changes from a prior aborted cherry-pick.
        _git(tree, "reset", "-q", "--hard")
        _git(tree, "clean", "-fdq")
        return tree

    if tree.is_dir():
        logging.info("Removing stale tree without git baseline: %s", tree)
        shutil.rmtree(tree)

    logging.info("Materializing source at %s (this may take a minute)", tree)
    tree, _ = prepare_source(
        llvm_version=llvm_version,
        version=version,
        versions_dir=versions_dir,
        output_dir=build_dir,
        verify_sig=False,
    )

    _git(tree, "init", "-q", "-b", "main")
    _git(tree, "config", "user.email", "cherry-pick@local")
    _git(tree, "config", "user.name", "cherry-pick")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "baseline")
    return tree


def apply_patch(patch_text: str, tree: Path) -> tuple[int, list[str]]:
    """Apply *patch_text* in *tree* using ``patch --merge=merge``.

    Returns ``(returncode, conflicted_files)``. ``conflicted_files`` is the
    set of files containing ``<<<<<<<`` conflict markers after the apply,
    relative to *tree*. Empty when the patch applied cleanly.
    """
    proc = subprocess.run(
        ["patch", "-p1", "--merge=merge", "--no-backup-if-mismatch"],
        input=patch_text,
        cwd=tree,
        capture_output=True,
        text=True,
    )
    sys.stderr.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)

    check = _git(tree, "diff", "--check", check=False, capture=True)
    conflicted: set[str] = set()
    for line in check.stdout.splitlines():
        m = re.match(r"^([^:]+):\d+:", line)
        if m:
            conflicted.add(m.group(1))
    return proc.returncode, sorted(conflicted)


def _read_version_txt(version_dir: Path) -> str:
    return (version_dir / "version.txt").read_text().strip()


def _resolve_version(args: argparse.Namespace, repo_root: Path) -> tuple[str, str, Path, Path]:
    """Resolve ``--llvm-version`` / ``--versions-dir`` to a concrete target.

    Returns ``(llvm_version, version, version_dir, versions_dir)``.
    """
    versions_dir = args.versions_dir or repo_root / "versions"
    llvm_version = args.llvm_version or latest_version(versions_dir)
    version_dir = versions_dir / llvm_version
    if not version_dir.is_dir():
        raise SystemExit(f"ERROR: {version_dir} does not exist")
    version = _read_version_txt(version_dir)
    return llvm_version, version, version_dir, versions_dir


def cmd_prepare(args: argparse.Namespace, repo_root: Path) -> None:
    llvm_version, version, _, versions_dir = _resolve_version(args, repo_root)
    tree = materialize_baseline(
        llvm_version=llvm_version,
        version=version,
        versions_dir=versions_dir,
        repo_root=repo_root,
    )
    logging.info(
        "Source tree ready. Edit in place, then export with:\n"
        "  git -C %s diff > versions/%s/patches/NNN_description.patch\n"
        "  git -C %s reset --hard",
        tree,
        llvm_version,
        tree,
    )
    print(tree)


def cmd_pick(args: argparse.Namespace, repo_root: Path) -> None:
    llvm_version, version, version_dir, versions_dir = _resolve_version(args, repo_root)

    sha = extract_sha(args.commit)
    logging.info("Fetching %s", PATCH_URL_TEMPLATE.format(sha=sha))
    raw = fetch_patch(sha)

    if OVERLAY_PREFIX not in raw:
        logging.warning(
            "'%s' not found in patch — nothing to rewrite. This commit may not touch any Bazel overlay files.",
            OVERLAY_PREFIX,
        )

    rewritten = rewrite_paths(raw)
    description = args.description or derive_description(raw)
    patches_dir = version_dir / "patches"
    patches_dir.mkdir(exist_ok=True)
    nnn = next_patch_number(patches_dir)
    target = patches_dir / f"{nnn:03d}_{description}.patch"

    if args.no_apply:
        target.write_text(rewritten)
        print(target)
        return

    tree = materialize_baseline(
        llvm_version=llvm_version,
        version=version,
        versions_dir=versions_dir,
        repo_root=repo_root,
    )
    rc, conflicts = apply_patch(rewritten, tree)

    if rc == 0 and not conflicts:
        diff = _git(tree, "diff", capture=True).stdout
        target.write_text(diff)
        _git(tree, "reset", "-q", "--hard")
        logging.info("Patch applied cleanly. Wrote %s", target)
        return

    logging.error("Cherry-pick applied with conflicts. Resolve them in:\n  %s", tree)
    if conflicts:
        logging.error("Conflicted files:")
        for f in conflicts:
            logging.error("  %s", f)
    logging.error(
        "Once resolved, export the patch with:\n  git -C %s diff > %s\n  git -C %s reset --hard",
        tree,
        target,
        tree,
    )
    sys.exit(1)


def main() -> None:
    std_logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=std_logging.INFO)
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent

    if args.command == "prepare":
        cmd_prepare(args, repo_root)
    elif args.command == "pick":
        cmd_pick(args, repo_root)
    else:
        raise SystemExit(f"ERROR: unknown command {args.command!r}")


if __name__ == "__main__":
    _cwd = os.environ.get("BUILD_WORKING_DIRECTORY")
    if _cwd:
        os.chdir(_cwd)
    main()
