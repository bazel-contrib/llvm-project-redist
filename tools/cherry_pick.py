#!/usr/bin/env python3
"""Tools for authoring patches against post-overlay LLVM source trees.

Patches in this repo apply to the *post-overlay* source tree, where files
under ``utils/bazel/llvm-project-overlay/`` have been copied to the source
root. Three subcommands:

  prepare
      Materialize the post-overlay source tree at
      ``build/{llvm_version}/llvm-project-{version}.bzl/`` and initialize
      a git repo with a baseline commit. Edit files in place, then export
      changes with ``git -C <tree> diff > versions/{version}/patches/NNN_*.patch``.

  pick
      Fetch an upstream llvm-project commit, strip the overlay prefix from
      every diff path, materialize the tree (via ``prepare`` semantics),
      and attempt to apply the rewritten patch. On clean apply, writes the
      canonical diff to ``versions/{version}/patches/NNN_*.patch`` with a
      ``# Upstream-Commit:`` header that links back to the source commit.
      On conflicts, leaves the tree with git-style conflict markers
      (``<<<<<<<``/``=======``/``>>>>>>>``) for the user to resolve.

  discover
      List upstream overlay commits that haven't yet been picked into a
      given version. Uses an existing checkout via ``--llvm-checkout`` or
      ``$LLVM_PROJECT``, otherwise lazily clones a managed bare mirror
      under ``build/llvm-project-mirror/``.

Usage:
    bazel run //tools:cherry_pick -- prepare [options]
    bazel run //tools:cherry_pick -- pick <commit-or-url> [options]
    bazel run //tools:cherry_pick -- discover [options]

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

    # Peek at what a commit would touch without writing a patch file
    bazel run //tools:cherry_pick -- pick abc1234 --dry-run

    # List unpicked upstream overlay commits for 17.0.3
    bazel run //tools:cherry_pick -- discover --llvm-version 17.0.3

    # Same, but use an existing llvm-project checkout instead of the mirror
    bazel run //tools:cherry_pick -- discover \\
        --llvm-version 17.0.3 \\
        --llvm-checkout ~/src/llvm-project
"""

import argparse
import json
import logging as std_logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

logging = std_logging.getLogger(__name__)

OVERLAY_PREFIX = "utils/bazel/llvm-project-overlay/"
BAZEL_SCOPE = "utils/bazel"
PATCH_URL_TEMPLATE = "https://github.com/llvm/llvm-project/commit/{sha}.patch"
UPSTREAM_COMMIT_URL_PREFIX = "https://github.com/llvm/llvm-project/commit/"
UPSTREAM_HEADER_PREFIX = "# Upstream-Commit: "
MIRROR_REMOTE = "https://github.com/llvm/llvm-project.git"
MIRROR_REL_PATH = ("build", "llvm-project-mirror")
MIN_SHA_MATCH_LEN = 7

_SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b", re.IGNORECASE)
_SUBJECT_RE = re.compile(r"^Subject: (?:\[PATCH(?: \d+/\d+)?\] )?(.+)$", re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Upstream subjects for overlay changes almost always start with "[bazel]" or
# "bazel:". The prefix carries no information here, so strip it before slugging.
_BAZEL_PREFIX_RE = re.compile(r"^\s*(?:\[bazel\]|bazel:)\s*", re.IGNORECASE)
# A saved patch can record its upstream SHA two ways: the explicit
# `# Upstream-Commit:` line written by `pick`, or the `From <sha>` header that
# git format-patch (and the GitHub `.patch` URL) emits when `--no-apply` is used.
_UPSTREAM_COMMENT_RE = re.compile(
    r"^# Upstream-Commit:\s*https?://github\.com/llvm/llvm-project/commit/([0-9a-f]{7,40})",
    re.IGNORECASE | re.MULTILINE,
)
_FROM_HEADER_RE = re.compile(r"^From ([0-9a-f]{40})\b", re.IGNORECASE | re.MULTILINE)
_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent


def _workspace_root() -> Path:
    """Return the source tree this invocation should read from and write to.

    ``bazel run`` execs the script out of the runfiles tree, so
    ``Path(__file__).parent.parent`` resolves to the runfiles ``_main``
    directory rather than the checkout — and ``versions/<v>/`` isn't a
    ``data`` dep of this target, so it isn't there at all. Everything this
    tool touches lives in the checkout: it reads ``versions/<v>/patches/``,
    writes new patches back to it, and materializes source trees under
    ``build/``. ``BUILD_WORKSPACE_DIRECTORY`` — set by ``bazel run`` to the
    workspace root — is the right anchor for all of them. Fall back to the
    ``__file__``-relative root so running the script directly still works.
    Mirrors ``render_presubmit._workspace_root``.
    """
    env = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if env:
        return Path(env)
    return _REPO_ROOT


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
    pick.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and rewrite without writing a patch file; print a summary of touched paths",
    )
    pick.add_argument(
        "--allow-non-overlay",
        action="store_true",
        help=f"Allow picking commits that don't touch {OVERLAY_PREFIX} (default: error)",
    )

    discover = sub.add_parser(
        "discover",
        help="List upstream overlay commits not yet picked into this version",
    )
    add_common(discover)
    discover.add_argument(
        "--llvm-checkout",
        type=Path,
        help="Path to an existing llvm/llvm-project checkout (default: $LLVM_PROJECT, else managed mirror)",
    )
    discover.add_argument(
        "--base",
        help="Lower bound of the commit range (default: llvmorg-<llvm_version>)",
    )
    discover.add_argument(
        "--head",
        help="Upper bound of the commit range (default: main / origin/main depending on clone type)",
    )
    discover.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip 'git fetch' before listing commits (use cached refs)",
    )
    discover.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of one line per commit",
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


def stamp_upstream_header(patch_body: str, sha: str) -> str:
    """Prepend a `# Upstream-Commit:` line so saved patches link back to source."""
    header = f"{UPSTREAM_HEADER_PREFIX}{UPSTREAM_COMMIT_URL_PREFIX}{sha}\n"
    return header + patch_body


def patch_touched_paths(patch: str) -> list[str]:
    """Return the sorted set of file paths touched by *patch* (from `diff --git` lines)."""
    paths: set[str] = set()
    for m in _DIFF_GIT_RE.finditer(patch):
        paths.add(m.group(2))
    return sorted(paths)


def picked_shas(patches_dir: Path) -> set[str]:
    """Return the set of upstream SHAs referenced by patches in *patches_dir*.

    Recognizes both the `# Upstream-Commit:` comment written by ``pick`` and the
    `From <sha>` line from raw git format-patch headers (used by older patches
    and by ``pick --no-apply``).
    """
    shas: set[str] = set()
    if not patches_dir.is_dir():
        return shas
    for p in sorted(patches_dir.glob("*.patch")):
        try:
            # Headers always live at the very top of the file; read a bounded
            # prefix to avoid slurping multi-MB diffs into memory.
            with p.open("r", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            continue
        for rx in (_UPSTREAM_COMMENT_RE, _FROM_HEADER_RE):
            for m in rx.finditer(head):
                shas.add(m.group(1).lower())
    return shas


def sha_already_picked(picked: set[str], upstream_sha: str) -> bool:
    """True if *upstream_sha* matches any picked SHA by prefix in either direction."""
    upstream_sha = upstream_sha.lower()
    for s in picked:
        if len(s) < MIN_SHA_MATCH_LEN:
            continue
        if upstream_sha.startswith(s) or s.startswith(upstream_sha):
            return True
    return False


def _git(tree: Path, *args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    # ``-c safe.bareRepository=all`` lets us operate on the managed bare mirror
    # under ``build/llvm-project-mirror/`` even when the user's global config
    # sets ``safe.bareRepository=explicit`` (git's default since 2.38).
    cmd = ["git", "-c", "safe.bareRepository=all", "-C", str(tree), *args]
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


def _validate_llvm_origin(repo: Path) -> None:
    proc = _git(repo, "remote", "get-url", "origin", check=False, capture=True)
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: {repo} has no 'origin' remote")
    url = proc.stdout.strip()
    if "llvm/llvm-project" not in url.lower():
        raise SystemExit(f"ERROR: {repo} origin is {url!r}; expected an llvm/llvm-project clone")


def _is_bare(repo: Path) -> bool:
    proc = _git(repo, "rev-parse", "--is-bare-repository", check=False, capture=True)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _looks_like_git_repo(path: Path) -> bool:
    return (path / ".git").exists() or (path / "HEAD").is_file()


def resolve_upstream_repo(
    *,
    checkout: Path | None,
    repo_root: Path,
    fetch: bool,
) -> Path:
    """Resolve an upstream llvm-project repo to query.

    Order of precedence: explicit ``--llvm-checkout`` / ``$LLVM_PROJECT`` first,
    otherwise the managed bare mirror under ``build/llvm-project-mirror/``.
    """
    explicit = checkout
    if explicit is None:
        env = os.environ.get("LLVM_PROJECT")
        if env:
            explicit = Path(env)
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not _looks_like_git_repo(path):
            raise SystemExit(f"ERROR: {path} is not a git repository")
        _validate_llvm_origin(path)
        if fetch:
            logging.info("Fetching from %s", path)
            _git(path, "fetch", "--tags", "origin")
        return path

    mirror = repo_root.joinpath(*MIRROR_REL_PATH)
    if _looks_like_git_repo(mirror):
        if fetch:
            logging.info("Fetching upstream into %s", mirror)
            _git(mirror, "fetch", "--tags", "origin")
        return mirror

    mirror.parent.mkdir(parents=True, exist_ok=True)
    logging.info("Cloning %s -> %s (this may take a minute)", MIRROR_REMOTE, mirror)
    subprocess.run(
        [
            "git",
            "clone",
            "--bare",
            "--filter=blob:none",
            MIRROR_REMOTE,
            str(mirror),
        ],
        check=True,
    )
    return mirror


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
        if not args.allow_non_overlay:
            raise SystemExit(
                f"ERROR: commit {sha} does not touch {OVERLAY_PREFIX}. "
                "Pass --allow-non-overlay if you intend to pick a non-overlay commit."
            )
        logging.warning(
            "'%s' not found in patch — proceeding with --allow-non-overlay.",
            OVERLAY_PREFIX,
        )

    rewritten = rewrite_paths(raw)
    description = args.description or derive_description(raw)
    patches_dir = version_dir / "patches"
    nnn = next_patch_number(patches_dir)
    target = patches_dir / f"{nnn:03d}_{description}.patch"

    if args.dry_run:
        touched = patch_touched_paths(rewritten)
        print(f"Would write: {target}")
        print(f"Touched files ({len(touched)}):")
        for f in touched:
            print(f"  {f}")
        return

    patches_dir.mkdir(exist_ok=True)

    if args.no_apply:
        target.write_text(stamp_upstream_header(rewritten, sha))
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
        target.write_text(stamp_upstream_header(diff, sha))
        _git(tree, "reset", "-q", "--hard")
        logging.info("Patch applied cleanly. Wrote %s", target)
        return

    upstream_line = f"{UPSTREAM_HEADER_PREFIX}{UPSTREAM_COMMIT_URL_PREFIX}{sha}"
    logging.error("Cherry-pick applied with conflicts. Resolve them in:\n  %s", tree)
    if conflicts:
        logging.error("Conflicted files:")
        for f in conflicts:
            logging.error("  %s", f)
    logging.error(
        "Once resolved, export the patch with:\n  (echo %s; git -C %s diff) > %s\n  git -C %s reset --hard",
        shlex.quote(upstream_line),
        tree,
        target,
        tree,
    )
    sys.exit(1)


def cmd_discover(args: argparse.Namespace, repo_root: Path) -> None:
    llvm_version, _version, version_dir, _versions_dir = _resolve_version(args, repo_root)

    upstream = resolve_upstream_repo(
        checkout=args.llvm_checkout,
        repo_root=repo_root,
        fetch=not args.no_fetch,
    )

    base = args.base or f"llvmorg-{llvm_version}"
    head = args.head or ("main" if _is_bare(upstream) else "origin/main")

    log_proc = _git(
        upstream,
        "log",
        f"{base}..{head}",
        "--format=%H%x00%cs%x00%s",
        "--",
        BAZEL_SCOPE,
        check=False,
        capture=True,
    )
    if log_proc.returncode != 0:
        sys.stderr.write(log_proc.stderr)
        raise SystemExit(f"ERROR: git log {base}..{head} failed; check that both refs exist in {upstream}")

    picked = picked_shas(version_dir / "patches")
    candidates: list[dict[str, str]] = []
    for line in log_proc.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\x00", 2)
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        if sha_already_picked(picked, sha):
            continue
        candidates.append({"sha": sha, "date": date, "subject": subject})

    if args.json:
        json.dump(candidates, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    if not candidates:
        logging.info(
            "No unpicked upstream commits touching %s in %s..%s.",
            BAZEL_SCOPE,
            base,
            head,
        )
        return

    for c in candidates:
        print(f"{c['sha'][:12]}  {c['date']}  {c['subject']}")


def main() -> None:
    std_logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=std_logging.INFO)
    args = parse_args()
    repo_root = _workspace_root()

    if args.command == "prepare":
        cmd_prepare(args, repo_root)
    elif args.command == "pick":
        cmd_pick(args, repo_root)
    elif args.command == "discover":
        cmd_discover(args, repo_root)
    else:
        raise SystemExit(f"ERROR: unknown command {args.command!r}")


if __name__ == "__main__":
    _cwd = os.environ.get("BUILD_WORKING_DIRECTORY")
    if _cwd:
        os.chdir(_cwd)
    main()
