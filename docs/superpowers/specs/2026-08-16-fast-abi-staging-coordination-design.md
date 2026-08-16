# Fast ABI Staging Coordination Design

## Goal

Reduce repeated ABI staging wave startup from roughly fifteen minutes to a
small bounded interval without changing Formula contracts, candidate bottle
bytes, or promotion eligibility.

## Current bottleneck

`prepare-workflow` reconstructs public scheduling state for 71 Formula names.
For each name it reads candidate, attempt, verification, and reuse OCI
repositories. These immutable public reads are independent, but the current
implementation performs every Formula scan serially. The 2026-08-16 ABI 43
wave spent 13 minutes 25 seconds in this step, plus 1 minute 56 seconds deriving
Formula roots.

This cost is distinct from Kandelo's heavyweight publisher regression suite.
That suite should run when publisher policy or implementation changes, and at
the publication boundary, rather than on unrelated candidate iterations.

## Design

Keep the existing three-phase inventory dependency order:

1. Candidate and attempt repositories establish candidate facts.
2. Verification repositories bind receipts to those candidates.
3. Reuse repositories bind the current request to verified candidates.

Within each phase, scan Formula repositories concurrently with a fixed,
reviewed worker bound. Merge results only after every worker in that phase
finishes. Sort all merged facts and mappings exactly as today so scheduling
outputs are byte-deterministic regardless of completion order.

Use one OCI transport per worker. The production transport caches bearer
tokens in mutable state and is not a documented thread-safe object; isolated
worker transports avoid sharing that state. Tests may provide a factory that
returns one shared fake registry when they need an in-memory namespace.

The default public API remains synchronous. A failed worker cancels pending
work and propagates the original inventory error. Existing retry handling
continues to retry the complete scan only for explicitly transient OCI
failures.

## Bottle reuse boundary

The change touches only public record discovery and workflow scheduling. It
does not modify Formula sources, bottle contracts, candidate builders, VFS
composition descriptors, or candidate record schemas. Before dispatching a
wave from the new tap revision, compare the generated Formula contract map with
the preceding coordination artifact. Existing candidate records and bottle
layers remain reusable when those maps are identical.

## Validation

- A timing-aware unit regression proves independent Formula reads overlap.
- Existing inventory tests prove exact validation, deterministic aggregation,
  and hostile-record rejection remain intact.
- The full tap Python suite must pass.
- A real `prepare-workflow` run against the immutable ABI 43 request measures
  hosted wall time and compares its Formula contract map to the preceding
  coordination artifact before the next wave is dispatched.

## Separate publisher-suite follow-up

Kandelo's heavyweight publisher regression suite is a separate CI cost. Move
it out of ordinary candidate-only iterations and retain only cheap workflow
syntax, schema, generated-freshness, and scope checks there. Run the complete
suite when its declared policy/implementation path set changes and immediately
before a credentialed publication. Land that as a separate Kandelo change so
it cannot alter the already-published ABI 43 request or candidate identities.
