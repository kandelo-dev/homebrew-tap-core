# ABI 43 Reuse And Verifier Recovery Design

## Goal

Resume ABI 43 staging without rebuilding any of the 42 existing candidate
bottles.

## Current Failures

The exact plan for request
`f7d73cda68c0c1b3841b929e050b63fec3b5c5f94678d0f4c4255e758a131105`
contains zero build work, 24 reuse records, and 18 verification items. Two
independent defects prevent the first wave from converging:

1. A newly created GHCR package is private by default. Reuse publication uses
   a new `<candidate>/reuse` package, so the immutable upload succeeds and the
   required anonymous readback then fails.
2. The protected verifier appends a Playwright environment overlay to the
   selected Formula before Kandelo compares that Formula with the copy archived
   in the candidate bottle. All four first-wave verifications therefore fail
   the Formula identity check before running a Formula test.

## Reuse Record Layout

Publish reuse records into the Formula's existing public candidate package.
Use a dedicated immutable tag prefix,
`reuse-sha256-<manifest-sha256>`, so candidate records, verification receipts,
and reuse records remain disjoint even though they share one package.

Public inventory accepts exactly three tag classes:

- `record-sha256-*` for candidate records;
- `verification-<test-key>-<host>-sha256-*` for verification receipts;
- `reuse-sha256-*` for reuse records.

Each scanner selects only its own tag prefix and validates the fetched OCI
artifact type and canonical record body. Promotion requires a reuse record to
come from the same Formula candidate package as the candidate it binds. Failed
private `<candidate>/reuse` packages are not read or migrated; they never
became valid public scheduling facts.

## Verification Browser Cache

Do not modify selected Formula source. Remove the tap-side Formula overlay.
Instead, pass the already prepared absolute Playwright browser directory as an
explicit verifier input. Kandelo's exact verifier validates that input and
exports the resulting canonical directory as `PLAYWRIGHT_BROWSERS_PATH` only
for the private Homebrew verification process. Homebrew changes `HOME` to each
Formula test's temporary directory, so a symlink created under the verifier's
initial home is not a reliable transport boundary. The explicit Playwright
environment input remains stable across that Homebrew-owned `HOME` transition.
Formula receipt comparison therefore observes the unmodified Formula, while
Formula-owned browser tests discover the already prepared Chromium without a
download or Formula contract change.

The cache bridge is verification-only. Candidate construction, bottle bytes,
Formula build contracts, and ordinary Homebrew publication are unchanged.

## Historical Verification Results

An immutable successful verification remains reusable when the candidate
record, test-definition digest, and host are unchanged. A historical failure,
timeout, or cancellation does not exhaust a later immutable request: verifier
and host-runtime corrections can make the same immutable bottle pass without
changing its Formula or bytes.

For each candidate/test/host tuple, scheduling first looks for any historical
success. If none exists, it applies the bounded retry policy only to terminal
receipts whose `request_sha256` equals the current request. A new request with
no current-request failure begins at ordinal zero. Current-request failures
still advance normally and still become exhausted at the configured limit.

This is deliberately asymmetric. Reusing success avoids redundant work;
reusing failure as a retry counter would strand unchanged bottle bytes after a
reviewed verifier correction. No public receipt is deleted or rewritten.

## Failure Handling

- Unknown or mixed record tags fail inventory rather than being ignored.
- Reuse publication retains exact authenticated collision checks and anonymous
  manifest/blob readback in the already-public package.
- Missing, relative, symlinked, or unreadable browser cache inputs fail before
  Formula tests. The verifier replaces any caller-provided
  `PLAYWRIGHT_BROWSERS_PATH` with the validated explicit input.
- Historical unsuccessful receipts remain public evidence, but only failures
  for the current immutable request consume that request's retry budget.
- Formula archive identity remains strict; no overlay or normalization is
  added to the inspector.

## Validation

Use focused tap tests for OCI tag filtering, reuse publication/inventory,
promotion repository binding, and request-local failure retries. Use Kandelo
verifier shell fixtures to prove Homebrew can change `HOME` while Formula tests
still receive the canonical browser cache path and Formula identity inspection
still reads unmodified source. Run the tap workflow contract checker and the
exact focused Python suites before publishing either fix.
