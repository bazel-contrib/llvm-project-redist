"""Helpers for detecting changed ``versions/`` directories.

Used by ``run_presubmit.py`` for dynamic Buildkite pipeline generation.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def versions_from_git_diff_lines(lines: str) -> list[str]:
    """Parse ``git diff --name-only`` output for ``versions/<name>/`` paths."""
    versions: set[str] = set()
    for line in lines.splitlines():
        line = line.strip()
        m = re.match(r"versions/([^/]+)/", line)
        if m:
            versions.add(m.group(1))
    return sorted(versions)


def changed_version_dirs(git_base_ref: str, repo_root: str) -> list[str]:
    """Return sorted unique ``versions/X`` directory names changed vs *git_base_ref*."""
    out = subprocess.run(
        ["git", "diff", f"{git_base_ref}...HEAD", "--name-only", "--pretty=format:"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git diff failed (exit {out.returncode}): {out.stderr.strip() or out.stdout.strip()}")
    return versions_from_git_diff_lines(out.stdout)


def latest_version_dir(versions_dir: str) -> str | None:
    """Return the latest ``versions/X`` directory name by sorting, or None."""
    p = Path(versions_dir)
    if not p.is_dir():
        return None
    dirs = [d.name for d in p.iterdir() if d.is_dir() and (d / "presubmit.yml").is_file()]
    if not dirs:
        return None
    return sorted(dirs, reverse=True)[0]


def read_version_string(llvm_version: str, versions_dir: str) -> str:
    p = Path(versions_dir) / llvm_version / "version.txt"
    if p.is_file():
        return p.read_text().strip()
    return llvm_version
