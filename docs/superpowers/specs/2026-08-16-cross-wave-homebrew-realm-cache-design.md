# Cross-wave Homebrew realm cache

## Goal

Avoid rebuilding an identical prepared Homebrew test realm in every ABI
staging reconciliation wave, including waves separated only by Formula or
other unrelated tap edits. Candidate and verification jobs must continue to
consume the existing run-scoped artifact interface and validate the restored
realm against the exact Kandelo source commit and tree.

## Design

The `prepare-homebrew-realm` job restores one GitHub Actions cache entry before
running the realm preparer. The cache key includes the runner operating system,
runner architecture, exact Kandelo commit selected by protected discovery, and
a digest of the protected tap scripts that prepare and pack the realm. It does
not include the whole tap commit: Formula, inventory-fixture, or unrelated tap
changes do not alter the realm. The cached object is only the already-packed
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

A missing cache entry falls back to the existing build. The first content-keyed
run may migrate the exact cache produced by the original cache implementation,
but only while the current producer-script digest equals the reviewed legacy
digest; this is an exact-key lookup, never a prefix restore. A producer-script
change disables migration and forces a rebuild. A malformed or stale
cache entry is not accepted as a different realm: the producer hashes it and
the downstream restore contract rejects any source/tree/inventory mismatch.
The cache lookup uses no prefix restore keys, so it cannot substitute a nearby
commit.

## Validation

The workflow checker must require fully pinned cache actions, the content-key
derivation, exact primary and migration keys, no restore prefixes, preparation
and packing guarded by both cache misses, and the unchanged run-scoped upload.
The cache-key integration must prove Formula-only changes preserve the key and
producer-script changes rotate it. Mutation tests must reject removal or
weakening of each boundary. The existing shared-realm pack/restore integration
remains the byte-level validation gate.
