#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRY_RUN="$ROOT/.github/workflows/dry-run-bottles.yml"
PUBLISH="$ROOT/.github/workflows/publish-bottles.yml"

fail() {
  echo "test-workflow-trust.sh: $*" >&2
  exit 1
}

for workflow in "$DRY_RUN" "$PUBLISH"; do
  grep -Fx '  repository_dispatch:' "$workflow" >/dev/null ||
    fail "$(basename "$workflow") is not default-branch dispatched"
  ! grep -Fx '  workflow_dispatch:' "$workflow" >/dev/null ||
    fail "$(basename "$workflow") can execute a branch-selected workflow definition"
  ! grep -Eq 'uses:[[:space:]]+actions/cache(/restore)?@' "$workflow" ||
    fail "$(basename "$workflow") restores caller-writable cache state"
  ! grep -F 'secrets: inherit' "$workflow" >/dev/null ||
    fail "$(basename "$workflow") passes repository secrets to executable refs"
  grep -F '[ "$GITHUB_REF" = "refs/heads/main" ]' "$workflow" >/dev/null ||
    fail "$(basename "$workflow") does not assert the default-branch event invariant"
done

! grep -Eq '^[[:space:]]+(contents|packages): write$' "$DRY_RUN" ||
  fail "dry-run workflow grants write authority"
grep -Fx '      dry-run: true' "$DRY_RUN" >/dev/null ||
  fail "dry-run caller does not pass literal dry-run mode"
grep -Fx '    uses: Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main' \
  "$DRY_RUN" >/dev/null || fail "dry-run caller does not use the reviewed reusable workflow"

grep -Fx '      kandelo-ref: main' "$PUBLISH" >/dev/null ||
  fail "publisher does not use Kandelo main"
grep -Fx '      tap-ref: main' "$PUBLISH" >/dev/null ||
  fail "publisher does not use tap main"
grep -Fx '      dry-run: false' "$PUBLISH" >/dev/null ||
  fail "write publisher exposes dry-run mode"
! grep -F 'client_payload.kandelo_ref' "$PUBLISH" >/dev/null ||
  fail "write publisher accepts executable Kandelo refs"
! grep -F 'client_payload.tap_ref' "$PUBLISH" >/dev/null ||
  fail "write publisher accepts executable tap refs"
grep -Fx '    uses: Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main' \
  "$PUBLISH" >/dev/null || fail "publisher does not use the reviewed reusable workflow"

echo "test-workflow-trust.sh: ok"
