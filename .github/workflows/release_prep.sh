#!/usr/bin/env bash
# Build the LLVM redistribution archives for release.
# Invoked by bazel-contrib/.github/.github/workflows/release_ruleset.yaml.
#
# Args:
#   $1: tag name (e.g. llvmorg-17.0.4 or llvmorg-17.0.3.bcr.5)
#
# Side effects:
#   Writes llvm-project-${VERSION}.bzl.tar.{xz,zst} and their .sha256 /
#   .integrity sidecars to the current directory. These match the
#   release_files glob in release.yaml.
#
# Output:
#   Release notes to stdout. The release_ruleset workflow redirects this
#   into release_notes.txt for the GitHub release body.

set -euo pipefail

TAG="${1:?tag_name required}"
VERSION="${TAG#llvmorg-}"
LLVM_VERSION="${VERSION%.bcr.*}"

VERSION_FILE="versions/${LLVM_VERSION}/version.txt"
if [ ! -f "$VERSION_FILE" ]; then
    echo "ERROR: ${VERSION_FILE} not found" >&2
    exit 1
fi
ON_DISK_VERSION=$(tr -d '[:space:]' < "$VERSION_FILE")
if [ "$ON_DISK_VERSION" != "$VERSION" ]; then
    echo "ERROR: tag ${TAG} implies version ${VERSION}, but ${VERSION_FILE} contains ${ON_DISK_VERSION}" >&2
    exit 1
fi

# Build .tar.xz and .tar.zst archives via existing tooling.
bazel run //tools:build -- \
    --llvm-version "${LLVM_VERSION}" \
    --version "${VERSION}" \
    --versions-dir=versions \
    --output-dir=. >&2

# Generate .sha256 and .integrity (SRI) sidecars for each archive.
for archive in \
    "llvm-project-${VERSION}.bzl.tar.xz" \
    "llvm-project-${VERSION}.bzl.tar.zst"; do
    sha256sum "${archive}" > "${archive}.sha256"
    printf 'sha256-%s' "$(sha256sum "${archive}" | cut -d' ' -f1 | xxd -r -p | base64 -w0)" > "${archive}.integrity"
done

# Render release notes to a temp file, then emit on stdout so that the
# workflow captures only the notes (not bazel's progress output).
NOTES=$(mktemp)
bazel run //tools:release_notes -- \
    --workspace "$(pwd)" \
    --template ".github/release_notes.template" \
    --llvm-version "${LLVM_VERSION}" \
    --version "${VERSION}" \
    --tag "${TAG}" \
    -o "${NOTES}" >&2
cat "${NOTES}"
