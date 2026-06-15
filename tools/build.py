#!/usr/bin/env python3
"""Build an LLVM redistribution artifact with Bazel overlay.

Unifies CI and local workflows into a single cross-platform Python script.
Downloads the upstream source, applies the Bazel overlay and patches,
transforms MODULE.bazel / extensions.bzl, generates build files, and
repackages everything into a deterministic .tar.xz archive.

Usage:
    # Local build (from repo root; output under build/17.0.3/)
    bazel run //tools:build -- --llvm-version 17.0.3

    # BCR patch release
    bazel run //tools:build -- --llvm-version 17.0.3 --bcr-version 1

    # CI-style (cwd = checkout root; versions under ./versions)
    bazel run //tools:build -- \\
        --llvm-version 17.0.3 \\
        --version 17.0.3.bcr.preview \\
        --versions-dir versions \\
        --output-dir . \\
        --metadata-dir metadata
"""

import argparse
import hashlib
import logging as std_logging
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.request import urlretrieve

logging = std_logging.getLogger(__name__)

import zstandard as zstd

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))

from tools.transform_extensions_bzl import transform as _transform_extensions_source
from tools.transform_module_bazel import transform as _transform_module_source

UPSTREAM_URL_TEMPLATE = (
    "https://github.com/llvm/llvm-project/releases/download/llvmorg-{version}/llvm-project-{version}.src.tar.xz"
)
UPSTREAM_SIG_URL_TEMPLATE = (
    "https://github.com/llvm/llvm-project/releases/download/llvmorg-{version}/llvm-project-{version}.src.tar.xz.sig"
)
UPSTREAM_RELEASE_KEYS_URL = "https://releases.llvm.org/release-keys.asc"

OVERLAY_EXTRA_FILES = ("vulkan_sdk.bzl", "BUILD.bazel", ".bazelrc", ".bazelversion")

LLVM_TARGETS = [
    "AArch64",
    "AMDGPU",
    "ARM",
    "AVR",
    "BPF",
    "Hexagon",
    "Lanai",
    "LoongArch",
    "Mips",
    "MSP430",
    "NVPTX",
    "PowerPC",
    "RISCV",
    "Sparc",
    "SPIRV",
    "SystemZ",
    "VE",
    "WebAssembly",
    "X86",
    "XCore",
]

BOLT_SUPPORTED = frozenset(("AArch64", "X86", "RISCV"))


# ── Pure helpers ──────────────────────────────────────────────────────────


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, capturing combined stdout/stderr.

    On failure, logs the command and its output before raising.
    """
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        logging.error("Command failed: %s\n%s", " ".join(cmd), result.stdout)
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout)
    return result


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_signature(tarball: Path, sig_file: Path, key_file: Path) -> None:
    """Verify a GPG signature for *tarball* using the provided public key.

    Requires ``gpg`` to be installed. Raises SystemExit if any file is
    missing, gpg is not found, or verification fails.
    """
    if not sig_file.is_file():
        raise SystemExit(f"ERROR: signature file not found: {sig_file}")
    if not key_file.is_file():
        raise SystemExit(f"ERROR: signing key not found: {key_file}")
    if shutil.which("gpg") is None:
        raise SystemExit("ERROR: gpg is required for signature verification but was not found")

    logging.info("Verifying GPG signature %s", sig_file.name)
    run(["gpg", "--import", str(key_file)])
    run(["gpg", "--verify", str(sig_file), str(tarball)])
    logging.info("Signature verified")


def extract_cmake_var(filepath: Path, varname: str) -> str | None:
    """Extract a variable value from a CMake ``set()`` command."""
    pattern = re.compile(rf"\s*set\s*\(\s*{re.escape(varname)}\s+(\S+)")
    with open(filepath) as f:
        for line in f:
            m = pattern.match(line)
            if m:
                return m.group(1).rstrip(")")
    return None


def _make_deterministic(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Zero out non-deterministic tar entry metadata."""
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


# ── Build pipeline steps ─────────────────────────────────────────────────


def _download_progress(block_num: int, block_size: int, total_size: int) -> None:
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(100, downloaded * 100 // total_size)
        mb = downloaded / (1 << 20)
        total_mb = total_size / (1 << 20)
        print(f"\r    {mb:.1f}/{total_mb:.1f} MB ({pct}%)", end="", flush=True)


def download_tarball(llvm_version: str, dest_dir: Path) -> tuple[Path, str]:
    """Download the upstream LLVM source tarball.

    Returns ``(tarball_path, sha256)``.
    Reuses a cached file when present.
    """
    url = UPSTREAM_URL_TEMPLATE.format(version=llvm_version)
    tarball = dest_dir / f"llvm-project-{llvm_version}.src.tar.xz"

    if tarball.is_file():
        logging.info("Using cached %s", tarball.name)
    else:
        logging.info("Downloading %s", url)
        dest_dir.mkdir(parents=True, exist_ok=True)
        urlretrieve(url, tarball, reporthook=_download_progress)
        print()

    sha256 = compute_sha256(tarball)
    logging.info("SHA-256: %s", sha256)
    return tarball, sha256


def download_signature(llvm_version: str, dest_dir: Path) -> Path:
    """Download the upstream LLVM GPG signature for the source tarball."""
    url = UPSTREAM_SIG_URL_TEMPLATE.format(version=llvm_version)
    sig = dest_dir / f"llvm-project-{llvm_version}.src.tar.xz.sig"

    if sig.is_file():
        logging.info("Using cached %s", sig.name)
        return sig

    logging.info("Downloading %s", url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    urlretrieve(url, sig)
    return sig


def download_signing_key(dest_dir: Path) -> Path:
    """Download the upstream LLVM release signing keyring."""
    key = dest_dir / "signing-key.asc"

    if key.is_file():
        logging.info("Using cached %s", key.name)
        return key

    logging.info("Downloading %s", UPSTREAM_RELEASE_KEYS_URL)
    dest_dir.mkdir(parents=True, exist_ok=True)
    urlretrieve(UPSTREAM_RELEASE_KEYS_URL, key)
    return key


def extract_tarball(tarball: Path, dest_dir: Path) -> Path:
    """Extract a ``.tar.xz`` archive and return the top-level directory."""
    logging.info("Extracting %s", tarball.name)
    with tarfile.open(tarball, "r:xz") as tar:
        top_level = tar.getnames()[0].split("/")[0]
        target = dest_dir / top_level
        if target.exists():
            shutil.rmtree(target)
        if sys.version_info >= (3, 12):
            tar.extractall(path=dest_dir, filter="data")
        else:
            tar.extractall(path=dest_dir)
    return target


def apply_overlay(src_dir: Path) -> None:
    """Copy the Bazel overlay files into the source root, then drop ``utils/``.

    After the overlay copy, ``utils/bazel/llvm-project-overlay/`` is a 1.4 MB
    duplicate of files now living at the source root, and its BUILD files
    still reference the upstream ``@bazel_tools`` constraint labels that the
    project's patches replace at the source root. Leaving it in the published
    tarball means ``bazel //...`` consumers would silently see broken targets
    if they (or a future upstream regression) lost the ``.bazelignore`` that
    masks ``utils/bazel``. The cheap and correct fix is to delete ``utils/``
    entirely once the overlay has been applied — nothing under it is
    referenced by any build target at the source root.
    """
    logging.info("Applying Bazel overlay")
    overlay = src_dir / "utils" / "bazel" / "llvm-project-overlay"
    if not overlay.is_dir():
        logging.warning("Overlay directory not found at %s", overlay)
        return

    shutil.copytree(overlay, src_dir, dirs_exist_ok=True)

    bazel_utils = src_dir / "utils" / "bazel"
    for name in OVERLAY_EXTRA_FILES:
        src_file = bazel_utils / name
        if src_file.is_file():
            shutil.copy2(src_file, src_dir / name)

    utils_dir = src_dir / "utils"
    if utils_dir.is_dir():
        logging.info("Removing %s (overlay sources are now at the source root)", utils_dir)
        shutil.rmtree(utils_dir)


def apply_patches(src_dir: Path, patch_dir: Path) -> int:
    """Apply all ``*.patch`` files from *patch_dir* to *src_dir*.

    Returns the number of patches applied.
    """
    if not patch_dir.is_dir():
        return 0

    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        return 0

    logging.info("Applying %d patch(es)", len(patches))
    for p in patches:
        logging.info("  %s", p.name)
        run(["patch", "-p1", "-d", str(src_dir), "-i", str(p.resolve())])
    return len(patches)


_BASELINE_MODULE_BAZEL = """\
module(name = "llvm-project", version = "{version}")

bazel_dep(name = "apple_support", version = "1.24.1", repo_name = "build_bazel_apple_support")
bazel_dep(name = "bazel_skylib", version = "1.8.2")
bazel_dep(name = "platforms", version = "1.0.0")
bazel_dep(name = "rules_cc", version = "0.2.11")
bazel_dep(name = "rules_python", version = "1.9.0")
bazel_dep(name = "rules_shell", version = "0.6.1")
"""


def transform_module(src_dir: Path, version: str) -> None:
    """Transform the upstream ``MODULE.bazel`` or generate a baseline."""
    upstream = src_dir / "utils" / "bazel" / "MODULE.bazel"
    output = src_dir / "MODULE.bazel"

    if upstream.is_file():
        logging.info("Transforming MODULE.bazel")
        result = _transform_module_source(upstream.read_text(), version)
        output.write_text(result)
    else:
        logging.info("No upstream MODULE.bazel, generating baseline")
        output.write_text(_BASELINE_MODULE_BAZEL.format(version=version))


def transform_extensions(src_dir: Path) -> None:
    """Transform ``extensions.bzl`` if present in the upstream Bazel files."""
    upstream = src_dir / "utils" / "bazel" / "extensions.bzl"
    output = src_dir / "extensions.bzl"

    if upstream.is_file():
        logging.info("Transforming extensions.bzl")
        result = _transform_extensions_source(upstream.read_text())
        output.write_text(result)
    else:
        logging.info("No upstream extensions.bzl, skipping")


def generate_vars_bzl(src_dir: Path) -> None:
    """Generate ``vars.bzl`` from CMake version variables."""
    logging.info("Generating vars.bzl")

    version_file = src_dir / "cmake" / "Modules" / "LLVMVersion.cmake"
    if not version_file.is_file():
        version_file = src_dir / "llvm" / "CMakeLists.txt"

    cmake_file = src_dir / "llvm" / "CMakeLists.txt"

    major = extract_cmake_var(version_file, "LLVM_VERSION_MAJOR") or "0"
    minor = extract_cmake_var(version_file, "LLVM_VERSION_MINOR") or "0"
    patch = extract_cmake_var(version_file, "LLVM_VERSION_PATCH") or "0"
    suffix = extract_cmake_var(version_file, "LLVM_VERSION_SUFFIX") or ""

    cxx_std = (
        extract_cmake_var(cmake_file, "LLVM_REQUIRED_CXX_STANDARD")
        or extract_cmake_var(cmake_file, "CMAKE_CXX_STANDARD")
        or "17"
    )

    llvm_ver = f"{major}.{minor}.{patch}"
    package_version = f"{llvm_ver}{suffix}"

    variables = {
        "CMAKE_CXX_STANDARD": cxx_std,
        "LLVM_VERSION_MAJOR": major,
        "LLVM_VERSION_MINOR": minor,
        "LLVM_VERSION_PATCH": patch,
        "LLVM_VERSION_SUFFIX": suffix,
        "LLVM_VERSION": llvm_ver,
        "PACKAGE_VERSION": package_version,
    }

    rel_cmake = cmake_file.relative_to(src_dir)
    lines = [f"# Generated from {rel_cmake}\n"]
    for k, v in variables.items():
        lines.append(f'{k} = "{v}"')
    lines.append("")
    lines.append("llvm_vars = {")
    for k, v in variables.items():
        lines.append(f'    "{k}": "{v}",')
    lines.append("}")
    lines.append("")

    (src_dir / "vars.bzl").write_text("\n".join(lines))


def generate_targets_bzl(src_dir: Path) -> None:
    """Generate ``llvm/targets.bzl`` and ``bolt/targets.bzl``."""
    logging.info("Generating targets.bzl files")

    bolt_targets = [t for t in LLVM_TARGETS if t in BOLT_SUPPORTED]

    llvm_dir = src_dir / "llvm"
    bolt_dir = src_dir / "bolt"
    llvm_dir.mkdir(parents=True, exist_ok=True)
    bolt_dir.mkdir(parents=True, exist_ok=True)

    (llvm_dir / "targets.bzl").write_text(f"llvm_targets = {LLVM_TARGETS!r}\n")
    (bolt_dir / "targets.bzl").write_text(f"bolt_targets = {bolt_targets!r}\n")


def _write_deterministic_tar(tar: tarfile.TarFile, src_dir: Path) -> None:
    """Write *src_dir* into *tar* with sorted entries and zeroed metadata."""
    parent = src_dir.parent
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, parent).replace(os.sep, "/")

        info = tar.gettarinfo(dirpath, arcname=rel_dir)
        _make_deterministic(info)
        tar.addfile(info)

        for fname in sorted(filenames):
            fpath = os.path.join(dirpath, fname)
            arcname = f"{rel_dir}/{fname}"
            info = tar.gettarinfo(fpath, arcname=arcname)
            _make_deterministic(info)

            if info.issym() or info.islnk():
                tar.addfile(info)
            else:
                with open(fpath, "rb") as f:
                    tar.addfile(info, f)


def create_archive(src_dir: Path, output: Path) -> str:
    """Create a deterministic ``.tar.xz`` archive.

    Entries are sorted by name, with uid/gid/mtime zeroed.
    Returns the SHA-256 of the written file.
    """
    logging.info("Repackaging as %s (deterministic)", output.name)

    with lzma.open(output, "wb") as xz, tarfile.open(fileobj=xz, mode="w") as tar:
        _write_deterministic_tar(tar, src_dir)

    sha256 = compute_sha256(output)
    mb = output.stat().st_size / (1 << 20)
    logging.info("Size: %.1f MB, SHA-256: %s", mb, sha256)
    return sha256


def create_archive_zstd(src_dir: Path, output: Path) -> str:
    """Create a deterministic ``.tar.zst`` archive.

    Returns the SHA-256 of the written file.
    """
    logging.info("Repackaging as %s (deterministic)", output.name)

    cctx = zstd.ZstdCompressor(level=19, threads=-1)
    with open(output, "wb") as fh, cctx.stream_writer(fh) as writer, tarfile.open(fileobj=writer, mode="w") as tar:
        _write_deterministic_tar(tar, src_dir)

    sha256 = compute_sha256(output)
    mb = output.stat().st_size / (1 << 20)
    logging.info("Size: %.1f MB, SHA-256: %s", mb, sha256)
    return sha256


# ── Orchestration ─────────────────────────────────────────────────────────


def prepare_source(
    *,
    llvm_version: str,
    version: str,
    versions_dir: Path,
    output_dir: Path,
    verify_sig: bool = True,
) -> tuple[Path, str]:
    """Download, extract, patch, and transform LLVM source. Returns ``(source_dir, upstream_sha256)``."""
    output_dir.mkdir(parents=True, exist_ok=True)

    tarball, upstream_sha256 = download_tarball(llvm_version, output_dir)

    if verify_sig:
        sig_file = download_signature(llvm_version, output_dir)
        key_file = download_signing_key(output_dir)
        verify_signature(tarball, sig_file, key_file)

    src_dir = extract_tarball(tarball, output_dir)

    apply_overlay(src_dir)

    patch_dir = versions_dir / llvm_version / "patches"
    apply_patches(src_dir, patch_dir)

    transform_module(src_dir, version)
    transform_extensions(src_dir)

    generate_vars_bzl(src_dir)
    generate_targets_bzl(src_dir)

    final_name = f"llvm-project-{version}.bzl"
    final_dir = output_dir / final_name
    if final_dir.exists() and final_dir != src_dir:
        shutil.rmtree(final_dir)
    src_dir.rename(final_dir)

    return final_dir, upstream_sha256


def build(
    *,
    llvm_version: str,
    version: str,
    versions_dir: Path,
    output_dir: Path,
    metadata_dir: Path | None = None,
) -> dict[str, str]:
    """Run the full build pipeline and return a results dict."""
    final_dir, upstream_sha256 = prepare_source(
        llvm_version=llvm_version,
        version=version,
        versions_dir=versions_dir,
        output_dir=output_dir,
    )

    artifact = output_dir / f"{final_dir.name}.tar.xz"
    artifact_sha256 = create_archive(final_dir, artifact)

    artifact_zst = output_dir / f"{final_dir.name}.tar.zst"
    artifact_zst_sha256 = create_archive_zstd(final_dir, artifact_zst)

    if metadata_dir is not None:
        metadata_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(final_dir / "MODULE.bazel", metadata_dir / "MODULE.bazel")

    result = {
        "llvm_version": llvm_version,
        "version": version,
        "artifact": str(artifact),
        "artifact_sha256": artifact_sha256,
        "upstream_sha256": upstream_sha256,
        "source_dir": str(final_dir),
    }

    result["artifact_zst"] = str(artifact_zst)
    result["artifact_zst_sha256"] = artifact_zst_sha256

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            for k, v in result.items():
                f.write(f"{k}={v}\n")

    return result


# ── CLI ───────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an LLVM redistribution artifact with Bazel overlay.",
    )
    parser.add_argument(
        "--llvm-version",
        required=True,
        help="LLVM version (e.g. 17.0.3)",
    )
    parser.add_argument(
        "--version",
        help="Output version string (default: same as --llvm-version)",
    )
    parser.add_argument(
        "--bcr-version",
        help="BCR patch version (e.g. 1 → 17.0.3.bcr.1)",
    )
    parser.add_argument(
        "--versions-dir",
        type=Path,
        help="Path to versions/ directory (default: <repo>/versions)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: build/<llvm-version>)",
    )
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        help="Copy MODULE.bazel to this directory",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Download, patch, and transform source without creating an archive",
    )
    return parser.parse_args(argv)


def main() -> None:
    std_logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=std_logging.INFO)
    args = parse_args()

    repo_root = _SCRIPTS_DIR.parent
    versions_dir = args.versions_dir or repo_root / "versions"
    output_dir = args.output_dir or repo_root / "build" / args.llvm_version

    if args.version:
        version = args.version
    elif args.bcr_version:
        version = f"{args.llvm_version}.bcr.{args.bcr_version}"
    else:
        version = args.llvm_version

    if args.prepare_only:
        source_dir, _ = prepare_source(
            llvm_version=args.llvm_version,
            version=version,
            versions_dir=versions_dir,
            output_dir=output_dir,
            verify_sig=False,
        )
        logging.info("Done! Source prepared at: %s", source_dir)
        return

    result = build(
        llvm_version=args.llvm_version,
        version=version,
        versions_dir=versions_dir,
        output_dir=output_dir,
        metadata_dir=args.metadata_dir,
    )

    logging.info("Done! Artifact: %s", result["artifact"])
    logging.info("SHA-256: %s", result["artifact_sha256"])

    if not os.environ.get("GITHUB_OUTPUT"):
        logging.info("To test with Bazel, point a local_path_override at: %s", result["source_dir"])


if __name__ == "__main__":
    _cwd = os.environ.get("BUILD_WORKING_DIRECTORY")
    if _cwd:
        os.chdir(_cwd)
    main()
