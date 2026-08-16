# Cross-wave Homebrew realm cache

## Goal

Avoid rebuilding an identical prepared Homebrew test realm in every ABI
staging reconciliation wave. Candidate and verification jobs must continue to
consume the existing run-scoped artifact interface and validate the restored
realm against the exact Kandelo source commit and tree.

## Design

The `prepare-homebrew-realm` job restores one GitHub Actions cache entry before
running the realm preparer. The cache key includes the runner operating system,
runner architecture, and exact Kandelo commit selected by protected discovery.
The cached object is only the already-packed
`shared-homebrew-realm.tar.zst` archive.

On a cache hit, the job skips realm preparation and packing. It derives the
archive SHA-256 and expected source tree from the protected coordination
outputs and exact checked-out Kandelo source, then uploads the archive as the
same run-scoped artifact used today. On a cache miss, existing preparation and
packing run unchanged before the immutable cache entry is saved and uploaded.

Candidate and verification reusable workflows remain unchanged. Their existing
restore step must continue to verify the artifact digest, archive digest,
source commit, source tree, and archive inventory before using the realm.

## Failure behavior

A missing cache entry falls back to the existing build. A malformed or stale
cache entry is not accepted as a different realm: the producer hashes it and
the downstream restore contract rejects any source/tree/inventory mismatch.
The cache lookup uses no prefix restore keys, so it cannot substitute a nearby
commit.

## Validation

The workflow checker must require one fully pinned cache action, the exact key,
no restore prefixes, preparation and packing guarded by cache miss, and the
unchanged run-scoped upload. Mutation tests must reject removal or weakening of
each boundary. The existing shared-realm pack/restore integration remains the
byte-level validation gate.
