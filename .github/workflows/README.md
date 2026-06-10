# Workflows

## CI (`ci.yaml`)

Runs on every PR and push to `main`. Validates patches, runs pre-commit checks, and builds release archives for any changed version directories. On pushes to `main`, dispatches the release workflow for affected versions.

**If it fails:** Fix the issue and push again, or re-run the failed job. If the dispatch step fails after a successful build, manually trigger the release workflow from the Actions tab with the appropriate `llvm_version`.

## Check LLVM Release (`check-llvm-release.yaml`)

Runs twice daily on a schedule. Scans upstream llvm-project releases and opens a PR for any new version not yet tracked.

**If it fails:** Run the workflow manually from the Actions tab, optionally with a specific `llvm_version`. If the PR fails to open, the workflow is idempotent and will retry on the next scheduled run.

## Release (`release.yaml`)

Builds release archives, generates SLSA provenance attestations, creates a GitHub release, and opens a Bazel Central Registry PR. Normally dispatched automatically by CI.

Internally calls two reusable workflows whose builder identities the BCR's `slsa-verifier` trusts:
- [`bazel-contrib/.github/.github/workflows/release_ruleset.yaml`](https://github.com/bazel-contrib/.github/blob/v7.6.0/.github/workflows/release_ruleset.yaml) (archive build + attestation)
- [`bazel-contrib/publish-to-bcr/.github/workflows/publish.yaml`](https://github.com/bazel-contrib/publish-to-bcr/blob/v1.3.0/.github/workflows/publish.yaml) (BCR entry + MODULE.bazel/source.json attestations + PR)

The actual build runs in [`release_prep.sh`](release_prep.sh) (path is hardcoded by [`release_ruleset.yaml`](https://github.com/bazel-contrib/.github/blob/v7.6.0/.github/workflows/release_ruleset.yaml)).

**If it fails:** Delete the failed release and its tag, fix the issue, then re-trigger the workflow from the Actions tab with the `llvm_version`. Re-runs will recreate the tag and force-push the BCR branch, so a stale partial state from a previous run must be cleaned up before retrying.
