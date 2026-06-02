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

1. Add git-formatted patch files to `versions/{version}/patches/` named `NNN_description.patch` (three-digit zero-padded, sequential from `001`).
2. Bump the version in `versions/{version}/version.txt` to the next `.bcr.N` (e.g. `17.0.3.bcr.6`).
3. Open a pull request. CI validates patch naming, builds the archive, and Buildkite runs presubmit tests.
4. On merge, CI dispatches the release workflow.

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
