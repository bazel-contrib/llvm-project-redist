#!/usr/bin/env python3
"""Validate version directories under versions/.

Each versions/{version}/ directory may contain:
  - ``version.txt`` (required): version string matching the directory name,
    optionally with a ``.bcr.N`` suffix (e.g. ``17.0.3`` or ``17.0.3.bcr.1``).
  - ``presubmit.yml`` (required): BCR presubmit test configuration.
  - ``patches/`` (optional): patch files matching ``NNN_description.patch``
    (three-digit zero-padded prefix, sequential starting at 001).
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PATCH_RE = re.compile(r"^(\d{3})[_-].+\.patch$")

IGNORED_FILES = {".gitkeep"}


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("versions_dir", type=Path, help="Path to versions/ directory")
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref to compare version.txt against for increment validation (default: origin/main)",
    )
    return parser.parse_args()


def _is_non_empty(version_dir: Path) -> bool:
    """True if the directory has meaningful content beyond placeholders."""
    for item in version_dir.iterdir():
        if item.is_file() and item.name not in IGNORED_FILES:
            return True
        if item.is_dir() and any(item.iterdir()):
            return True
    return False


def validate_version_txt(version_dir: Path, *, base_ref: str = "origin/main") -> list[str]:
    """Validate the version.txt file in a version directory.

    The content must be exactly the directory name, optionally followed
    by ``.bcr.N`` where N is one or more digits.

    Returns a list of error strings (empty if valid).
    """
    errors: list[str] = []
    version_file = version_dir / "version.txt"
    dir_name = version_dir.name

    if not version_file.is_file():
        if _is_non_empty(version_dir):
            errors.append(f"{dir_name}: missing required version.txt")
        return errors

    version = version_file.read_text().strip()
    if not version:
        errors.append(f"{dir_name}: version.txt is empty")
        return errors

    pattern = re.compile(rf"^{re.escape(dir_name)}(\.bcr\.\d+)?$")
    if not pattern.match(version):
        errors.append(f"{dir_name}: version.txt contains '{version}' but must be '{dir_name}' or '{dir_name}.bcr.N'")
        return errors

    errors.extend(_check_version_increment(version_dir, base_ref=base_ref))
    return errors


_BCR_SUFFIX_RE = re.compile(r"\.bcr\.(\d+)$")


def _parse_bcr_number(version: str) -> int:
    m = _BCR_SUFFIX_RE.search(version)
    return int(m.group(1)) if m else 0


def _check_version_increment(version_dir: Path, *, base_ref: str = "origin/main") -> list[str]:
    """Check that the .bcr.N suffix increments by exactly 1 relative to *base_ref*."""
    version_file = version_dir / "version.txt"
    rel_path = f"versions/{version_dir.name}/version.txt"
    repo_root = version_dir.parent.parent

    result = subprocess.run(
        ["git", "show", f"{base_ref}:{rel_path}"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        return []

    old_version = result.stdout.strip()
    new_version = version_file.read_text().strip()

    if old_version == new_version:
        return []

    old_n = _parse_bcr_number(old_version)
    new_n = _parse_bcr_number(new_version)

    if new_n != old_n + 1:
        return [
            f"{version_dir.name}: version.txt changed from '{old_version}' to "
            f"'{new_version}' but .bcr.N must increment by exactly 1"
        ]
    return []


def validate_version(version_dir: Path, *, base_ref: str = "origin/main") -> list[str]:
    """Validate a single version directory.

    Checks version.txt, presubmit.yml presence, and patches/ subdirectory
    for naming/sequencing.

    Returns a list of error strings (empty if valid).
    """
    errors: list[str] = []
    dir_name = version_dir.name

    if _is_non_empty(version_dir) and not (version_dir / "presubmit.yml").exists():
        errors.append(f"{dir_name}: missing required presubmit.yml")

    errors.extend(validate_version_txt(version_dir, base_ref=base_ref))

    patches_dir = version_dir / "patches"
    if not patches_dir.is_dir():
        return errors

    patches = sorted(p for p in patches_dir.iterdir() if p.suffix == ".patch")

    if not patches:
        return errors

    numbers: list[int] = []
    for patch in patches:
        m = PATCH_RE.match(patch.name)
        if not m:
            errors.append(
                f"{version_dir.name}/patches/{patch.name}: "
                f"does not match NNN_description.patch or NNN-description.patch"
            )
            continue
        numbers.append(int(m.group(1)))

    if len(numbers) != len(patches):
        return errors

    for i, n in enumerate(numbers):
        expected = i + 1
        if n != expected:
            errors.append(
                f"{version_dir.name}: expected patch {expected:03d} but found {n:03d} (gap or out-of-order sequence)"
            )
            return errors

    return errors


def validate(versions_dir: Path, *, base_ref: str = "origin/main") -> list[str]:
    """Validate all version directories under a versions/ root.

    Returns a list of error strings (empty if everything is valid).
    """
    errors: list[str] = []

    if not versions_dir.is_dir():
        return errors

    for entry in sorted(versions_dir.iterdir()):
        if not entry.is_dir():
            continue
        errors.extend(validate_version(entry, base_ref=base_ref))

    return errors


def main() -> None:
    args = parse_args()

    errors = validate(args.versions_dir, base_ref=args.base_ref)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        print("All version directories valid.")


if __name__ == "__main__":
    _cwd = os.environ.get("BUILD_WORKING_DIRECTORY")
    if _cwd:
        os.chdir(_cwd)
    main()
