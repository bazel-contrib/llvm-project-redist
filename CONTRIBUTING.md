# Contributing

## Adding a new LLVM version

New upstream releases are detected automatically by the `check-llvm-release` workflow, which runs twice daily. It opens a PR with the scaffolding for each new release:

- `versions/{version}/version.txt`
- `versions/{version}/presubmit.yml` (from template)

Review the presubmit configuration, add any necessary patches, then merge to trigger the release pipeline. The upstream tarball, its GPG signature, and the LLVM release signing key are fetched from official sources at build time — none of them are committed.

To manually seed a version, run the **Check LLVM Release** workflow from the Actions tab with the `llvm_version` input, or:

```bash
mkdir -p versions/{version}
echo "{version}" > versions/{version}/version.txt
cp .bcr/presubmit.yml versions/{version}/presubmit.yml
```

## Adding or updating patches

Patches apply to the **post-overlay source tree**: the build extracts the upstream tarball, copies `utils/bazel/llvm-project-overlay/**` into the source root, and *then* runs `patch -p1` on each `versions/{version}/patches/NNN_*.patch` in numeric order. A hunk path is `llvm/BUILD.bazel`, not `utils/bazel/llvm-project-overlay/llvm/BUILD.bazel`.

### Finding candidate commits

When refreshing patches on an older version, you usually want to know *what's changed upstream under `utils/bazel/` since this release was cut*. The `discover` subcommand lists those commits, skipping any that are already represented in `versions/{version}/patches/`:

```bash
# Zero-config: lazily clones a bare mirror under build/llvm-project-mirror/
bazel run //tools:cherry_pick -- discover --llvm-version 17.0.3

# Reuse an existing llvm-project checkout you already have
bazel run //tools:cherry_pick -- discover \
    --llvm-version 17.0.3 \
    --llvm-checkout ~/src/llvm-project

# Or set LLVM_PROJECT once and forget about the flag
LLVM_PROJECT=~/src/llvm-project bazel run //tools:cherry_pick -- discover --llvm-version 17.0.3
```

Output is one line per unpicked commit (`<short-sha>  <date>  <subject>`); pass `--json` for scripting. The defaults scan `llvmorg-<llvm_version>..main`, overridable with `--base` / `--head`.

Already-picked commits are detected via the `# Upstream-Commit:` header that `pick` stamps on saved patches (and the `From <sha>` line on existing format-patch-style files), so the list shrinks as you work through it.

If you'd rather skip the wrapper, the equivalent raw command against a local clone is:

```bash
git -C ~/src/llvm-project log llvmorg-17.0.3..main -- utils/bazel/
```

### Writing a new patch

Materialize the post-overlay tree with a git baseline, edit in place, and export the diff:

```bash
# Materialize build/17.0.3/llvm-project-17.0.3.bcr.5.bzl/ with a git baseline commit
bazel run //tools:cherry_pick -- prepare --llvm-version 17.0.3

# Edit files in the printed tree path. When done, export the patch:
git -C build/17.0.3/llvm-project-17.0.3.bcr.5.bzl diff \
    > versions/17.0.3/patches/013_my_fix.patch
git -C build/17.0.3/llvm-project-17.0.3.bcr.5.bzl reset --hard
```

`prepare` reuses an existing tree if it's already a git repo, so re-running between iterations is cheap.

### Backporting an upstream commit

Upstream Bazel files live under `utils/bazel/llvm-project-overlay/`, so a raw `git format-patch` from llvm-project won't apply. Use `pick` to fetch a commit, rewrite the paths, materialize the source tree, and try to apply:

```bash
# Pick into the highest version directory; description derived from commit subject
bazel run //tools:cherry_pick -- pick <commit-sha-or-url>

# Or pin the target version and description
bazel run //tools:cherry_pick -- pick \
    https://github.com/llvm/llvm-project/commit/<sha> \
    --llvm-version 17.0.3 \
    --description fix_build_for_msvc
```

If the patch applies cleanly, the canonical diff is written to the next free `NNN_*.patch` slot under `versions/{version}/patches/`, prefixed with a `# Upstream-Commit: <url>` header so `discover` knows it's been picked. Review and commit.

If the patch has conflicts, the materialized tree at `build/{llvm-version}/llvm-project-{version}.bzl/` is left with git-style conflict markers (`<<<<<<<`/`=======`/`>>>>>>>`) in the affected files. Resolve in place, then run the export command the helper prints (it includes the `# Upstream-Commit:` header).

Pass `--no-apply` to skip materialization and just write the rewritten patch (useful when you trust the commit applies cleanly and want to skip the tarball download), or `--dry-run` to fetch and print a summary of touched paths without writing anything.

Commits that don't touch `utils/bazel/` error out by default — pass `--allow-non-overlay` if you intentionally want to pick one anyway.

### Submitting

1. Bump `versions/{version}/version.txt` to the next `.bcr.N` (e.g. `17.0.3.bcr.6`).
2. Open a pull request. CI validates patch naming, builds the archive, and Buildkite runs presubmit tests.
3. On merge, CI dispatches the release workflow.

### Patch requirements

- **Bazel-only patches** (e.g. `BUILD` files, module configuration) require a link to an upstream [llvm-project](https://github.com/llvm/llvm-project) change — pending or merged — that has engagement from an LLVM maintainer.
- **Source code patches** must already be merged upstream in the current or a future LLVM version.
- Patches may be backported to older releases to provide wide version compatibility, provided the change has been accepted in a newer version.
- Files must match `NNN_description.patch` or `NNN-description.patch`.
- Numbers must start at `001` and be strictly sequential with no gaps.
- All patches are applied with `patch -p1` from the source root in numeric order.

## Presubmit testing

Each `versions/{version}/presubmit.yml` follows the [Bazel CI presubmit format](https://github.com/bazelbuild/continuous-integration). Tests run on Buildkite with remote caching, matching the BCR presubmit experience.

To reproduce locally:

```bash
bazel run //tools:setup_presubmit -- 17.0.3
```

This prepares the LLVM source, creates an anonymous test workspace, and prints the exact bazel commands to run for your platform.

## Development setup

```bash
# Install pre-commit hooks
pip install pre-commit
pre-commit install

# Create a venv for local tooling
python3 -m venv .venv
.venv/bin/pip install -r tools/requirements.in
```

Pre-commit runs: buildifier (format + lint), ruff (format + lint), and mypy (strict). The same checks run in CI.

## Release pipeline

```
Push to main (version.txt changed)
  -> CI builds archives (.tar.xz + .tar.zst)
  -> CI dispatches release workflow
    -> Generates attestations (SLSA)
    -> Creates GitHub release with all artifacts
      -> BCR publish workflow opens PR to bazel-central-registry
```
