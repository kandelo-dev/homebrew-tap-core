# Post-#1121 Publisher Trust Rotation

This runbook rotates the three protected tap callers from the live
`88d26f4c` authority to one complete authority tuple. The helper also accepts
the reviewed but unshipped `b90eff73` intermediate value per slot so a partial
earlier rotation or interrupted replacement can converge safely:

- `M`: the exact 40-character merge commit of Automattic/kandelo PR #1121;
- `G`: the fresh public
  `package-generation-rootfs-wasm32-abi-v42-sha256-...` tag generated and
  promoted by exact `M`; and
- `C`: the SHA-256 of the final raw
  `.github/workflows/publish-bottles.yml` bytes. The helper derives `C`; it is
  not an operator input.

Do not reuse the earlier b90 generation. Do not invoke the generation workflow
from an unmerged workflow SHA.

## Preconditions

1. Merge #1121 and query its actual merge commit. Do not predict `M` from its
   PR head or synthetic merge.
2. Confirm `M` is still the freshly queried `Automattic/kandelo` `main`.
3. Let the canonical `binaries-abi-v42` package release finish for `M`.
4. Dispatch `promote-package-generation.yml` from exact `M` with exact-main
   authority, `source-tag=binaries-abi-v42`,
   `validation-method=identical-git-tree-v1`, `expected-abi=42`,
   `selection-kind=root-package`, `root-package=rootfs`, `arch=wasm32`, and
   both producer and validated-main SHAs equal to `M`:

   ```bash
   set -euo pipefail

   gh workflow run promote-package-generation.yml \
     --repo Automattic/kandelo \
     --ref main \
     -f source-tag=binaries-abi-v42 \
     -f producer-sha="$M" \
     -f validated-main-sha="$M" \
     -f validation-method=identical-git-tree-v1 \
     -f expected-abi=42 \
     -f selection-kind=root-package \
     -f root-package=rootfs \
     -f arch=wasm32
   ```

   The exact-main checks fail closed if `main` moves before the dispatched run
   starts.
5. Record the content-derived public release tag as `G`. The generation must
   validate `M` as its authority, consumer, package source, and required archive
   source. A successful run summary alone is not a substitute for the immutable
   release and `generation.json`.
6. Start from a clean checkout of the latest protected tap `main`; no prepared
   or uncommitted placeholder edits are a prerequisite. The helper accepts
   only the exact live `88d26f4c` predecessor, the reviewed `b90eff73`
   intermediate predecessor, or the requested new value in each SHA slot.
7. Confirm no active publication run or private ledger still needs recovery
   under caller hash
   `1d36416c57ba168f0d4b310dfb98c1f1b9a9d17926cb491079e18eba299b1e19`.
   This rotation starts a new campaign; it does not silently migrate an old
   ledger.

If Kandelo `main` advances before generation promotion, exact-main admission
will reject `M`; either finish the promotion before that movement or use the
new current-main commit and its own fresh generation. If tap `main` advances,
rebase the prepared tap change before rotation and recompute `C` from the final
caller bytes.

Coordinate an Automattic/kandelo main-freeze window through the complete bottle
publication wave, not only through generation promotion. The write publisher
requeries `refs/heads/main` during planning and again before mutations; once
main differs from `M`, publication and maintenance fail closed even if G and
the tap rotation are already complete. If an unrelated Kandelo change lands,
select that new current main as the next M, promote its own fresh G, rerun this
rotation to derive a new C, and merge the replacement tap authority before
resuming writes.

## Preview and apply

Use full values, never short SHAs:

```bash
set -euo pipefail

export M='<exact merged Automattic/kandelo main SHA>'
export G='<exact package-generation-rootfs-wasm32-abi-v42-sha256-... tag>'

python3 -B scripts/rotate-publisher-trust.py \
  --kandelo-sha "$M" \
  --generation-tag "$G"

python3 -B scripts/rotate-publisher-trust.py \
  --kandelo-sha "$M" \
  --generation-tag "$G" \
  --apply
```

The preview performs the complete transformation in memory and writes nothing.
`--apply` changes only these reviewed slots:

- dry-run reusable publisher: `M`; the event-selected `kandelo-ref` remains,
  and no package-generation input is added;
- maintenance reusable publisher and `kandelo-ref`: `M`; rootfs generation:
  `G`;
- production reusable publisher and `kandelo-ref`: `M`; rootfs generation:
  `G`;
- Ruby caller trust constants: `M` and `G`; and
- rollout-controller current authority: `M`, `G`, and derived `C`.

The helper rejects unknown predecessor values, extra dry-run generation
authority, malformed inputs, or mixed unreviewed slots. A second invocation is
a no-op. A rerun after an interrupted multi-file replacement converges slots
that contain the live predecessor, the reviewed b90 intermediate predecessor,
or the requested new value.

## Validate before live use

```bash
set -euo pipefail

python3 -B scripts/test_rotate_publisher_trust.py
ruby Kandelo/test-workflow-trust.rb
python3 -B - <<'PY'
import ast
import pathlib

for source in (
    "scripts/rotate-publisher-trust.py",
    "scripts/test_rotate_publisher_trust.py",
    "scripts/abi42-rollout.py",
):
    ast.parse(pathlib.Path(source).read_bytes(), filename=source)
PY
git diff --check
```

The ABI 42 controller loads the caller from Git `HEAD`, not from uncommitted
working-tree bytes. Therefore its authority tests must run against a local
commit containing the rotation:

```bash
set -euo pipefail

python3 -B scripts/test_abi42_rollout.py \
  RolloutControllerTests.test_exact_plan_has_63_formulae_and_70_architecture_identities \
  RolloutControllerTests.test_failed_m3_campaign_authority_remains_auditable \
  RolloutControllerTests.test_fresh_campaign_requires_one_exact_reviewed_publication_contract \
  RolloutControllerTests.test_workflow_pins_one_main_and_one_admitted_rootfs_generation \
  RolloutControllerTests.test_run_name_is_the_only_non_bottle_affecting_workflow_difference \
  RolloutControllerTests.test_cli_requires_the_complete_campaign_contract_only_for_initialization
```

Do not interpret the expected pre-commit mismatch as a caller defect. Create
and inspect the local tap commit first, then run these tests before push.

The complete `scripts/test_abi42_rollout.py` suite should also run for a
controller change. At prepared base `0011ed2`, it already has 27 unrelated
failures (14 failures and 13 errors across 145 tests): current generated
Formula state no longer matches several frozen campaign fixtures, most
frequently because `sqlite.rb` no longer retains the fixture's last-green
wasm32 checksum. A synthetic M/G/C rotation produces the identical complete
failure set, while all six authority-focused tests above pass. Repair or
replace those stale campaign fixtures before requiring an all-green complete
suite; do not attribute that existing baseline to the trust rotation.

The base-owned `publisher-trust-base` pull-request-target job intentionally
requires every protected caller and trust-root byte to equal the current base.
It therefore cannot turn green on a legitimate authority rotation. Require the
candidate-owned `publisher-trust` job and the validations above to pass, review
the exact M/G/C diff, then use an explicitly authorized branch-protection/admin
bypass for this one trust-root change (the same path used by tap PR #117).
Waiting for `publisher-trust-base` to pass will wait forever; bypassing it
without the candidate-owned evidence would remove the protection it is meant
to provide.

Do not dispatch any Formula until the exact tap commit is merged to protected
`main` and the applicable trust and controller gates are green.

## Direct two-wave publication

The immediate nine-Formula proof does not use `scripts/abi42-rollout.py` or a
private campaign ledger. The protected caller and reusable publisher natively
accept a comma-separated Formula list. They normalize it into one
Formula-by-architecture matrix, require exactly one validated handoff for every
matrix member, compose all successful handoffs under one tap state lock, run
whole-tap validation, and push one canonical tap commit for the wave.

Use two waves because `file-formula` consumes `libmagic`, and `zip` consumes
`unzip`. Wave B must plan from the new protected-main commit produced by Wave
A; do not launch the waves concurrently.

Generate a unique correlation token for every request. The dispatch endpoint
returns no run ID, so the token is the unambiguous way to find the resulting
workflow run:

```bash
set -euo pipefail

dispatch_wave() {
  local formulae="$1"
  local tap_sha="$2"
  local token
  if [ -z "$formulae" ] || [[ ! "$tap_sha" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'dispatch_wave: invalid Formula selection or tap SHA\n' >&2
    return 1
  fi
  if ! token="$(python3 -c \
      'import secrets; print("abi42-" + secrets.token_hex(16))')" ||
      [[ ! "$token" =~ ^abi42-[0-9a-f]{32}$ ]]; then
    printf 'dispatch_wave: could not generate a correlation token\n' >&2
    return 1
  fi

  if ! jq -nc \
      --arg formulae "$formulae" \
      --arg arches wasm32 \
      --arg tap_sha "$tap_sha" \
      --arg dispatch_token "$token" \
      '{
        event_type: "publish-kandelo-bottles",
        client_payload: {
          formulae: $formulae,
          arches: $arches,
          tap_sha: $tap_sha,
          dispatch_token: $dispatch_token,
          release_tag: "bottles-abi-v42",
          force: true,
          require_vfs_acceptance: false
        }
      }' |
      gh api --method POST \
        repos/kandelo-dev/homebrew-tap-core/dispatches \
        --input - >/dev/null; then
    return 1
  fi

  printf '%s\n' "$token"
}
```

The explicit `force: true` is part of this recovery-aware proof contract. It
plans every selected Formula and permits recovery only when the tap root is
proven unfinalized. It cannot replace an identity that already has finalized
Formula sidecars or aggregate metadata. Modeset rebuild 2 is finalized: its
new build must reproduce the occupied child bytes exactly and reuse them, or
the run must fail closed and Modeset must move to rebuild 3. The explicit
release and VFS-acceptance values keep the direct request independent of future
caller defaults; rootfs generation authority intentionally does not permit the
VFS-acceptance lane.

After the trust-rotation commit is merged and protected tap `main` contains
all reviewed Wave A sources, record its exact commit and dispatch:

```bash
set -euo pipefail

WAVE_A_T0="$(
  gh api repos/kandelo-dev/homebrew-tap-core/commits/main --jq .sha
)"
WAVE_A_TOKEN="$(
  dispatch_wave \
    libmagic,make,wget,unzip,nano,nethack,modeset \
    "$WAVE_A_T0"
)"
printf 'Wave A: %s from %s\n' "$WAVE_A_TOKEN" "$WAVE_A_T0"
```

Find the run by token, let every matrix entry and the single finalizer finish,
and review the finalizer's generated tap commit. Only after Wave A is green,
query the resulting protected-main commit as Wave B's fresh source:

```bash
set -euo pipefail

WAVE_A_RUN_ID="$(
  gh run list \
    --repo kandelo-dev/homebrew-tap-core \
    --workflow publish-bottles.yml \
    --event repository_dispatch \
    --limit 50 \
    --json databaseId,displayTitle |
    jq -er --arg token "$WAVE_A_TOKEN" '
      [.[] | select(.displayTitle | contains($token))] as $matches
      | if ($matches | length) == 1
        then $matches[0].databaseId
        else error("expected exactly one Wave A run")
        end
    '
)"
gh run watch "$WAVE_A_RUN_ID" \
  --repo kandelo-dev/homebrew-tap-core \
  --exit-status

WAVE_B_T0="$(
  gh api repos/kandelo-dev/homebrew-tap-core/commits/main --jq .sha
)"
if [ "$WAVE_B_T0" = "$WAVE_A_T0" ]; then
  printf 'Wave A produced no new protected-main commit; refusing Wave B\n' >&2
else
  WAVE_B_TOKEN="$(
    dispatch_wave file-formula,zip "$WAVE_B_T0"
  )"
  printf 'Wave B: %s from %s\n' "$WAVE_B_TOKEN" "$WAVE_B_T0"
fi
```

Keep Automattic/kandelo `main` exactly at `M` until both waves have crossed
their final write boundaries. Every bottle upload, index upload, tap update,
failure report, and optional VFS publication rechecks that authority. A main
advance fails closed, but it can leave already-uploaded public objects.

On the tap, do not merge changes to a selected Formula, its tap recipe or
resources, shared Formula support, or anything in its dependency closure while
its wave is running. The finalizer rebinds the complete dependency closure to
fresh protected `main` and rejects drift. Unrelated tap changes are
theoretically admissible, but deferring all non-finalizer tap merges through
these two short waves gives the clearest and least failure-prone proof. Never
run overlapping requests containing the same Formula.

Successful canonical tap publication is atomic per wave. OCI child blobs and
per-Formula version indexes are uploaded before the tap finalizer and are not
transactional across the matrix. If any member fails, the complete-handoff
gate prevents partial Formula/sidecar success from becoming canonical, but
orphan or partial public package objects can remain and the workflow may commit
a failure report without replacing last-green metadata. Inspect occupied
references and digests before retrying; if the rebuilt bytes differ, reserve a
new rebuild rather than blindly overwriting or redispatching.

## Rollout controller status

The direct publisher does not read the ABI 42 controller, its private ledger,
the campaign manifest, or `C` at runtime. Rotating the controller's M/G/C tuple
is still required for a coherent protected trust root and for future controller
use, but its stale campaign fixture does not block the direct waves above.

At prepared base `0011ed2`, the controller recognizes only the old
`mostly-lazy-shell-abi42-rootfs-wasm32` manifest. That manifest describes Bash
rebuild 5 plus 23 reused Formulae, is bound to reservation `1939b1f`, and does
not select this nine-Formula wave. Current Formula, support, and catalog state
also fails its frozen source invariant. Do not initialize or reuse that
campaign for these publications.

Before the controller is used for a later campaign, create a fresh reviewed
base/reservation/manifest contract, repair or replace the stale fixtures, and
make its complete test suite green. Until then, direct dispatch retains exact
caller, M, G, tap-source, matrix, handoff, dependency-provenance, whole-tap,
state-lock, and final-write checks, but intentionally lacks the controller's
durable request journal and automatic recovery record.
