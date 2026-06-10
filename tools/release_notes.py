"""Generate release notes from a template with a path-filtered changelog.

Produces Markdown release notes by rendering ``.github/release_notes.template``
with version metadata and a changelog containing only commits that affect
common files or the specific version directory being released.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_DEFAULT_WORKSPACE = (
    Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", ""))
    if os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    else Path(__file__).resolve().parent.parent
)


def _user_cwd_path(s: str) -> Path:
    """Resolve a relative path against the shell's working directory.

    ``bazel run`` changes the process CWD to the runfiles dir before exec-ing
    the script, which would break relative paths the user typed on the
    command line. Anchor them to ``BUILD_WORKING_DIRECTORY`` (set by
    ``bazel run`` to the invoking shell's CWD), falling back to the current
    CWD when not under bazel.
    """
    p = Path(s)
    if p.is_absolute():
        return p
    base = os.environ.get("BUILD_WORKING_DIRECTORY") or os.getcwd()
    return Path(base) / p


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--workspace",
        type=_user_cwd_path,
        help="Path to git repo root (default: BUILD_WORKSPACE_DIRECTORY or script-relative)",
    )
    parser.add_argument(
        "--template",
        required=True,
        type=_user_cwd_path,
        help="Path to release notes template file",
    )
    parser.add_argument(
        "--llvm-version",
        required=True,
        type=str,
        help="Upstream LLVM version (e.g. 17.0.3)",
    )
    parser.add_argument(
        "--version",
        required=True,
        type=str,
        help="Release version string (e.g. 17.0.3.bcr.5)",
    )
    parser.add_argument(
        "--tag",
        required=True,
        type=str,
        help="Git tag for this release (e.g. llvmorg-17.0.3.bcr.5)",
    )
    parser.add_argument("-o", "--output", type=_user_cwd_path, help="Write to file instead of stdout")

    return parser.parse_args()


def _run(cmd: list[str], **kwargs: Any) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if r.returncode != 0:
        print(f"WARNING: {' '.join(cmd)}: {r.stderr.strip()}", file=sys.stderr)
        return ""
    stdout: str = r.stdout.strip()
    return stdout


def _find_previous_tag(tag: str, llvm_version: str, workspace: Path) -> str:
    tags = _run(
        ["git", "tag", "--list", f"llvmorg-{llvm_version}*", "--sort=-version:refname"],
        cwd=workspace,
    )
    for t in tags.splitlines():
        if t.strip() != tag:
            return t.strip()
    return _run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=workspace,
    ).splitlines()[0]


def _get_commits(prev_ref: str, llvm_version: str, workspace: Path) -> list[dict[str, str]]:
    fmt = "%H%x00%s"
    log = _run(
        [
            "git",
            "log",
            f"{prev_ref}..HEAD",
            f"--format={fmt}",
            "--",
            ".",
            ":!versions/",
            f"versions/{llvm_version}/",
        ],
        cwd=workspace,
    )
    commits: list[dict[str, str]] = []
    for line in log.splitlines():
        if "\x00" not in line:
            continue
        sha, subject = line.split("\x00", 1)
        commits.append({"sha": sha, "subject": subject})
    return commits


def _find_pr_number(sha: str) -> str | None:
    """Use ``gh`` to find the PR that merged a commit."""
    out = _run(
        [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/commits/{sha}/pulls",
            "--jq",
            ".[0].number",
        ]
    )
    return out if out else None


def _repo_url(workspace: Path) -> str:
    origin = _run(["git", "remote", "get-url", "origin"], cwd=workspace)
    if origin.startswith("git@github.com:"):
        origin = "https://github.com/" + origin[len("git@github.com:") :]
    if origin.endswith(".git"):
        origin = origin[:-4]
    return origin


def _format_changelog(commits: list[dict[str, str]], repo_url: str) -> str:
    if not commits:
        return "- No notable changes"

    lines = []
    for c in commits:
        sha_short = c["sha"][:7]
        subject = c["subject"]
        pr = _find_pr_number(c["sha"])
        if pr:
            lines.append(f"- {subject} ([#{pr}]({repo_url}/pull/{pr}))")
        else:
            lines.append(f"- {subject} ([`{sha_short}`]({repo_url}/commit/{c['sha']}))")
    return "\n".join(lines)


def generate(
    *,
    template: Path,
    llvm_version: str,
    version: str,
    tag: str,
    workspace: Path | None = None,
) -> str:
    ws = workspace or _DEFAULT_WORKSPACE
    prev_ref = _find_previous_tag(tag, llvm_version, ws)
    commits = _get_commits(prev_ref, llvm_version, ws)
    repo_url = _repo_url(ws)
    changelog = _format_changelog(commits, repo_url)

    text = template.read_text()
    return (
        text.replace("${VERSION}", version)
        .replace("${LLVM_VERSION}", llvm_version)
        .replace(
            "${DESCRIPTION}",
            f"Patched LLVM {llvm_version} source with Bazel overlay (version {version})."
            if version != llvm_version
            else f"Repackaged LLVM {version} source with Bazel overlay pre-applied.",
        )
        .replace("${CHANGELOG}", changelog)
    )


def main() -> None:
    args = parse_args()

    notes = generate(
        workspace=args.workspace,
        template=args.template,
        llvm_version=args.llvm_version,
        version=args.version,
        tag=args.tag,
    )

    if args.output:
        args.output.write_text(notes)
    else:
        print(notes)


if __name__ == "__main__":
    main()
