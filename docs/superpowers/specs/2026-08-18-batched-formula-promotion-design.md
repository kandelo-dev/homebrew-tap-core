# Batched Formula Promotion Design

## Goal

Promote an already-built, verified ABI bottle graph without repeating
public-inventory planning or requiring its dependencies to be selected in tap
metadata first.

## Preserved invariants

Each promoted Formula must still bind the exact target ABI, normalized Formula
contract, immutable candidate record and bottle layer, current-request
verification receipts, actual producer source custody, canonical anonymous
readback, and protected ABI history barrier. Promotion does not compare those
inputs to the currently selected dependency bottles: candidate construction
and verification already bind the dependencies that were actually used.

## Dependency readiness

Current tap dependency selection is not a promotion input. A Formula can be
promoted as soon as its own exact candidate and verification are present. A
missing exact candidate or verification receipt still blocks that Formula,
but missing dependency sidecars or admissions do not create a circular
new-ABI gate.

## Metadata batching

Canonical bottle manifests are published in parallel. The protected metadata
writer then validates every selected
per-Formula update against the same tap base, composes the shared
`Kandelo/metadata.json` projection once, and commits the union of Formula,
sidecar, link-manifest, and top-index changes atomically. The batch document
names every member and its exact immutable bottle identity. Admission writers
consume the batch readback and publish per-Formula admissions in parallel.

No candidate bytes are rebuilt or rewritten. Any duplicate path, conflicting
top-index row, moved tap main, missing canonical readback, or failed member
validation aborts the whole metadata commit.

## Product input custody

Product planning validates the source-custody record against the candidate's
actual producer. It does not require that historical producer tap commit to
equal current tap main when the current Formula contract, candidate digest,
ABI, and verification receipts are exact. This prevents metadata-only tap
commits from invalidating bottle contents.

## Validation

Tests cover dependency-selection independence, atomic multi-Formula metadata
composition, conflicting-path rejection, current-main compare-and-swap
failure, per-member admission readback, and product resolution across
metadata-only tap movement.
The hosted ABI 43 request must continue to schedule zero builds and must
advance canonical publication and admissions before Pages composition begins.
