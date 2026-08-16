# Shared ABI Staging Realm Design

## Goal

Build Kandelo's expensive Formula-test prerequisites once per reconciliation
run instead of rebuilding them independently in every candidate and
verification matrix job.

## Current problem

Every candidate and verification job currently performs the same work before
it touches a Formula: it builds musl, the kernel, fork and root-spill tools,
xtask, three Node dependency trees, a minimal ABI-bound VFS image, and a
Chromium installation. A twelve-job wave therefore builds the same closure
twelve times. The isolation provided by separate GitHub runners does not make
those identical builds more correct.

## Design

Add one `prepare-homebrew-realm` job after `discover-plan`. It checks out the
exact Kandelo source and protected tap revision, builds the superset needed by
both wasm32 and wasm64 Formula tests, and writes one run-scoped prepared-source
archive. The archive contains the exact checkout plus its generated build and
test outputs, but no credentials or Homebrew package cache.

The preparation job uploads the archive as an immutable workflow artifact and
exposes its artifact ID, digest, archive SHA-256, and source tree. Candidate
and verification reusable workflows require those values, download by
artifact ID, replace their
fresh candidate checkout with the prepared checkout, and validate its Git
commit/tree plus a protected manifest before Formula work begins.

Per-runner state remains local: the Homebrew prefix, temporary/cache
directories, resolved-tap document, optional Ruby build identities, and Linux
package installation are recreated in each job. Those operations depend on
runner-local users, ownership, mounts, and absolute paths and therefore must
not be represented as portable artifact state.

The prepared archive is run-scoped rather than a durable cross-run cache. It
is produced in an uncredentialed job from the exact selected source, contains
no candidate bottles, and is consumed only by jobs in the same reconciliation
run. Formula contracts, candidate bottle identities, and promotion records do
not include the realm artifact and therefore do not change because of how the
test prerequisites are transported.

## Failure behavior

- If realm preparation or upload fails, no candidate or verification job runs.
- If download, archive SHA, manifest, source commit, source tree, or required
  output validation fails, the consuming job stops before Formula code runs.
- Generated private-cache symlinks are materialized before upload; only
  normalized in-tree links and deterministic `/nix/store` links remain.
- An archive may contain only one normalized `kandelo-source/` root and must be
  extracted into a new absent directory.
- Candidate and verification jobs never fall back to rebuilding the closure.

## Validation

- Workflow contract tests must fail when either matrix omits the shared-realm
  dependency or exact artifact inputs.
- A shell fixture must build one miniature prepared source archive, restore it
  into two independent consumer roots, and reject digest, manifest, path, and
  source-identity mutations.
- The existing workflow mutation suite and actionlint must pass.
- One hosted ABI 43 wave must show a single preparation job and no matrix job
  executing the heavyweight build commands.
