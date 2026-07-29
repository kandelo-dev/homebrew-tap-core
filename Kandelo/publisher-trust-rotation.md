# Publisher Trust Rotation

This runbook rotates the three protected tap callers and their in-repository
trust roots from one complete authority tuple to another:

- `P_M`: the exact predecessor Kandelo main commit selected by every caller;
- `P_G`: the predecessor content-addressed rootfs-wasm32 generation;
- `P_C`: the SHA-256 of the predecessor raw production caller bytes;
- `M`: the exact new Kandelo main commit;
- `G`: the new content-addressed rootfs-wasm32 generation admitted by `M`; and
- `C`: the SHA-256 of the final raw
  `.github/workflows/publish-bottles.yml` bytes. The helper derives `C`; it is
  not an operator input.

The predecessor is explicit input rather than code in the helper. This keeps a
future merge SHA and generation out of the repository while still making the
tree prove exactly which current authority it is replacing.

## Post-#1123 predecessor

The protected tap `main` and tap PR #129 selected this tuple when this runbook
was prepared:

```bash
P_M=c647adda31d0918de944135543fb94039135cef1
P_G=package-generation-rootfs-wasm32-abi-v42-sha256-e7e56ceac71c2f78d8f8078021a71ab9502c76e72a2e96ba8046334139be1f2f
P_C=05b3e1e851d437d1706c6ff32cfdf9548e0c46e6e911c026c9c6c8f5c6447603
export P_M P_G P_C
```

Re-query protected tap `main` immediately before preparing the live rotation.
If any current caller or trust-root slot differs, stop and review the new
predecessor instead of editing these values until the helper accepts the tree.
Formula sidecars and aggregate metadata also contain historical
`kandelo_commit` values. They are artifact provenance and are intentionally not
rotation-owned; never globally replace `P_M`.

## Establish the successor

1. Merge Automattic/kandelo PR #1123 and query its actual merge commit from
   protected `main`. Do not use the PR head or GitHub's synthetic test merge as
   `M`.
2. Confirm `M` is still the freshly queried `Automattic/kandelo` `main`.
3. Let the canonical `binaries-abi-v42` package release finish for `M`.
4. From exact `M`, promote a fresh rootfs-wasm32 package generation with
   exact-main authority, `source-tag=binaries-abi-v42`,
   `validation-method=identical-git-tree-v1`, `expected-abi=42`,
   `selection-kind=root-package`, `root-package=rootfs`, and both producer and
   validated-main SHAs equal to `M`.
5. Record the content-derived public release tag as `G`. Its immutable
   `generation.json` must validate `M` as its authority, consumer, package
   source, and required archive source. A successful workflow summary alone is
   not generation authority.

For example:

```bash
set -euo pipefail

M="$(
  gh api repos/Automattic/kandelo/commits/main --jq .sha
)"
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

G='<exact package-generation-rootfs-wasm32-abi-v42-sha256-... tag>'
export M G
```

If Kandelo `main` advances before promotion or before the final publisher write
boundary, use the new current-main commit and promote its own generation.
Publication and maintenance re-query `main`; a stale `M` fails closed even
after the tap has selected it.

## Audit authority before changing files

Start from a clean checkout containing the final tap changes that the rotation
will accompany. If the closed-recipe work from PR #129 lands first, stack the
rotation helper commit onto that exact result. PR #129 did not change the five
rotation-owned files when this runbook was prepared.

Confirm the complete predecessor production caller, not only its visible pins:

```bash
set -euo pipefail

observed_c="$(
  python3 -B - <<'PY'
import hashlib
import pathlib

print(hashlib.sha256(
    pathlib.Path(".github/workflows/publish-bottles.yml").read_bytes()
).hexdigest())
PY
)"
test "$observed_c" = "$P_C"
```

Also confirm that no active publish or maintenance run still needs the old
caller and that no private rollout ledger needs recovery under `P_C`.

The controller currently exposes its live tuple through
`CURRENT_MAIN_SHA`, `CURRENT_ROOTFS_GENERATION_TAG`, and
`CURRENT_CALLER_SHA256`; those constants populate both current authority maps.
Rotating them removes the predecessor from those maps. If a private ledger or
failed run must remain recoverable under `P_C`, first add a purpose-named
historical tuple to both `APPROVED_PUBLICATION_WORKFLOWS` and
`APPROVED_CAMPAIGN_CONTRACTS`, with tests proving its exact historical caller
bytes. Do not make the generic helper invent that recovery authority.

### Prepared predecessor recovery audit

The 2026-07-28 preparation audit found no recovery reason to retain
`P_M/P_G/P_C` as a historical controller tuple:

- runs `30323151878`, `30324284741`, and `30329085073` were direct
  `repository_dispatch` operations, not controller-led dispatches recorded in
  a private rollout ledger;
- commit `b5ffda55d9b0e27efdfdec30ebb38d48a21518c4` atomically finalized the
  independently verified Libmagic, Make, Nano, NetHack, Unzip, and Wget
  outputs from run `30323151878`; run `30324284741` was the failed
  reproducibility retry and owns no remaining unpublished identity;
- Modeset was intentionally excluded from that recovery because its rebuild-2
  identity was already occupied. Protected `main` still records the earlier
  finalized Modeset bottle from run `30102527435`, so the cancelled run has no
  pending Modeset recovery;
- commit `6ad0e3dbc60e5572c4288c86919238f71c1bc110` atomically finalized the
  independently verified File and Zip outputs from run `30329085073`; and
- the two preserved private controller ledgers contained only the older
  mostly-lazy-shell campaigns. The failed M3 ledger remains recoverable
  through the purpose-named `FAILED_M3_*` tuple, while the later Bash campaign
  completed successfully. Neither ledger names `P_C`, any of the three runs,
  or their dispatch tokens, and neither has a pending or unresolved dispatch.

There were no active publish or maintenance writes when that audit completed.
These facts justify retiring `P_M/P_G/P_C` from the current controller maps
instead of granting it open-ended historical authority. They are a preparation
record, not a substitute for the immediate pre-rotation check: re-audit active
runs, protected-main history, canonical sidecars, and every private ledger
created since this record. If new unresolved evidence names `P_C`, stop and add
one bounded, purpose-named historical tuple before applying the rotation.

## Preview and apply

Use full values:

```bash
set -euo pipefail

python3 -B scripts/rotate-publisher-trust.py \
  --predecessor-kandelo-sha "$P_M" \
  --predecessor-generation-tag "$P_G" \
  --predecessor-caller-sha256 "$P_C" \
  --kandelo-sha "$M" \
  --generation-tag "$G"

python3 -B scripts/rotate-publisher-trust.py \
  --predecessor-kandelo-sha "$P_M" \
  --predecessor-generation-tag "$P_G" \
  --predecessor-caller-sha256 "$P_C" \
  --kandelo-sha "$M" \
  --generation-tag "$G" \
  --apply
```

Preview performs the complete transformation in memory and writes nothing.
Apply changes only these reviewed slots:

- dry-run reusable publisher: `M`; its event-selected `kandelo-ref` remains
  unchanged and it receives no package-generation input;
- maintenance reusable publisher and `kandelo-ref`: `M`; generation: `G`;
- production reusable publisher and `kandelo-ref`: `M`; generation: `G`;
- Ruby caller trust constants: `M` and `G`; and
- rollout-controller current authority: `M`, `G`, and derived `C`.

The helper accepts only predecessor or successor values in each owned slot. It
also requires the raw production caller to hash to either `P_C` or the rendered
successor `C`; this prevents scalar-only validation from approving extra jobs,
permissions, secrets, or other unreviewed caller bytes. Files are replaced
atomically one at a time. If the host stops between files, rerun with the same
five inputs to converge the partial application. A complete second invocation
is a no-op.

Review exactly the owned diff and the derived caller digest:

```bash
git diff -- \
  .github/workflows/dry-run-bottles.yml \
  .github/workflows/maintain-bottles.yml \
  .github/workflows/publish-bottles.yml \
  Kandelo/test-workflow-trust.rb \
  scripts/abi42-rollout.py

python3 -B - <<'PY'
import hashlib
import pathlib

print("C=" + hashlib.sha256(
    pathlib.Path(".github/workflows/publish-bottles.yml").read_bytes()
).hexdigest())
PY
```

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

The rollout controller loads the production caller from Git `HEAD`, not from
uncommitted working-tree bytes. Create and inspect a local commit containing
the rotation before running its authority tests:

```bash
python3 -B scripts/test_abi42_rollout.py \
  RolloutControllerTests.test_exact_plan_has_63_formulae_and_70_architecture_identities \
  RolloutControllerTests.test_failed_m3_campaign_authority_remains_auditable \
  RolloutControllerTests.test_fresh_campaign_requires_one_exact_reviewed_publication_contract \
  RolloutControllerTests.test_workflow_pins_one_main_and_one_admitted_rootfs_generation \
  RolloutControllerTests.test_run_name_is_the_only_non_bottle_affecting_workflow_difference \
  RolloutControllerTests.test_cli_requires_the_complete_campaign_contract_only_for_initialization

python3 -B scripts/test_abi42_rollout.py
```

Report focused and complete-suite results separately; do not turn a preexisting
controller fixture failure into evidence for or against the rotation.

The base-owned `publisher-trust-base` pull-request-target job intentionally
requires protected trust-root bytes to equal the current base. It therefore
cannot pass a legitimate authority rotation. Require the candidate-owned
`publisher-trust` job and the local validations above, review the exact
`P_M/P_G/P_C -> M/G/C` diff, and use only the explicitly authorized
branch-protection/admin path for this trust-root change.

Do not dispatch a Formula until the exact rotation commit is on protected tap
`main`, candidate trust checks are green, any required historical controller
authority has been reviewed, and Kandelo `main` still equals `M`.
