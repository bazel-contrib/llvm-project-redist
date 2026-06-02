# llvm-project-redist

Repackages [llvm-project](https://github.com/llvm/llvm-project) source releases with the Bazel build overlay pre-applied and publishes them to the [Bazel Central Registry](https://github.com/bazelbuild/bazel-central-registry) as the `llvm-project` module. Bazel is not tested as part of the LLVM release process, so patches are sometimes required to fix build issues after a release is cut. This repo manages those patches and the automation that gets working archives into the BCR.

This is not a fork of llvm-project. Patches should be reflected upstream and not deviate from the desires and direction of the LLVM org.

## Discussion and issues

For issues related to LLVM itself, please use the upstream [llvm-project](https://github.com/llvm/llvm-project) issue tracker or [LLVM Discourse forums](https://discourse.llvm.org/). This repo's issue tracker is only for problems specific to the BCR packaging and release automation.

## How it works

When LLVM cuts a release, a GitHub Actions workflow detects it, downloads the upstream source tarball, verifies its GPG signature, applies the Bazel overlay and any version-specific patches, and repackages the result as deterministic `.tar.xz` and `.tar.zst` archives. The archives are published as a GitHub release with provenance attestations and SRI integrity files, then automatically submitted to the BCR.

## Version scheme

| Release type | Version | Tag | Example |
|---|---|---|---|
| Base release | `X.Y.Z` | `llvmorg-X.Y.Z` | `20.1.0` |
| Patched release | `X.Y.Z.bcr.N` | `llvmorg-X.Y.Z.bcr.N` | `20.1.0.bcr.1` |

Base releases match upstream LLVM versions. Patched releases (`.bcr.N`) incorporate fixes for Bazel compatibility issues discovered after the upstream release.

## Repository structure

```
versions/
  17.0.3/
    version.txt                              # release version (e.g. 17.0.3.bcr.5)
    presubmit.yml                            # BCR presubmit test config
    patches/
      001_fix_build.patch                    # git-formatted patches, applied in order
.bcr/
  presubmit.yml                              # template for new versions
  source.template.json                       # BCR source.json template
  metadata.template.json                     # BCR metadata.json template
```

The upstream tarball, its GPG signature, and the LLVM release signing key are downloaded from official sources at build time and never committed.

## Local development

```bash
# Set up local presubmit testing (mirrors BCR workflow)
bazel run //tools:setup_presubmit -- 17.0.3

# Build a release archive locally
bazel run //tools:build -- --llvm-version 17.0.3

# Validate version directories
bazel run //tools:validate_patches -- versions

# Regenerate Python dependency lockfile
bazel run //tools:requirements.update

# Run tests
bazel test //tools/...

# Run pre-commit hooks
pre-commit run --all-files
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Maintenance

See [MAINTENANCE.md](MAINTENANCE.md) for workflow descriptions and failure recovery procedures for maintainers of `llvm-project-redist`.
