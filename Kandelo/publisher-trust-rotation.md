# Publisher Trust Rotation

This runbook rotates the four protected tap callers and their
in-repository trust roots from one complete authority tuple to another:

- `P_M`: the exact predecessor Kandelo main commit selected by write callers;
- `P_D`: the exact predecessor Kandelo main commit selected by the dry-run
  caller;
- `P_F`: the exact predecessor Kandelo commit selected by the
  first-publication namespace canary;
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

## Pre-#1160 predecessor

Protected tap `main` selected this tuple before the #1160 rotation was
prepared:

```bash
P_M=4322468ce11f386c30f0cb4cdba6f3414eb0b737
P_D=3ef821db380d4008c5fb48f953a2e97d83a9a597
P_F=5d133fcfd42a25f5ddaec21294b2d71d1564fee0
P_G=package-generation-rootfs-wasm32-abi-v42-sha256-8d08f8cc73b165b75d8367f257011ec1724974114e056fac2dfb0e63a4304454
P_C=eea191a190495a0b760df906122d3de55f61d2281b7e360d855feaa3a200e094
export P_M P_D P_F P_G P_C
```

Re-query protected tap `main` immediately before preparing the live rotation.
If any current caller or trust-root slot differs, stop and review the new
predecessor instead of editing these values until the helper accepts the tree.
Formula sidecars and aggregate metadata also contain historical
`kandelo_commit` values. They are artifact provenance and are intentionally not
rotation-owned; never globally replace `P_M`.

## Establish the successor

1. Merge Automattic/kandelo PR #1160 and query its actual merge commit from
   protected `main`. Do not use the PR head or GitHub's synthetic test merge as
   `M`.
2. Confirm `M` is still the freshly queried `Automattic/kandelo` `main`.
3. Rebuild `rootfs/wasm32` and its package closure from exact `M`.
   The reviewed pull-request head already passed the complete suites,
   so this provenance-only rebuild may use `skip_tests=true`; bottle
   publication and the later live lifecycle still supply shipping
   evidence.
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
gh workflow run force-rebuild.yml \
  --repo Automattic/kandelo \
  --ref main \
  -f packages=rootfs \
  -f arches=wasm32 \
  -f ref="$M" \
  -f skip_tests=true \
  -f bump_packages=false

# Wait for the exact-M rebuild before promotion.
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

### Overlap the one-time Libyaml bootstrap

The rootfs rebuild normally takes longer than the namespace bootstrap.
This migration may overlap them without granting production publication
authority early:

1. Start the exact-`M` rootfs rebuild immediately.
2. In a separate tap pull request, rotate only the dry-run caller, the
   first-publication caller, and their two Ruby trust constants to `M`.
   Leave production, maintenance, generation, and rollout-controller
   authority unchanged.
3. Merge that tap pull request and record its exact protected-main
   commit as `D`.
4. At exact `D`, run the Libyaml dry run and one-time first-child
   publication. Keep tap `main == D` between those two runs.
5. Disable the first-publication workflow again after the child
   succeeds.
6. Once the rootfs generation yields `G`, run the complete helper below
   with `P_D="$M"` and `P_F="$M"`, while retaining the recorded
   predecessor values for `P_M`, `P_G`, and `P_C`.

The split state is intentional: dry-run cannot publish, and
first-publication can create only the exact absent Libyaml child proved
by that dry run. Normal Formula publication remains pinned to
`P_M/P_G/P_C` until the complete rotation lands.
`Kandelo/test-workflow-trust.rb` permits these two live callers to
converge on current authority but still rejects collisions with
historical test fixtures.

## Audit authority before changing files

Start from a clean checkout containing the final tap changes that the rotation
will accompany. If the closed-recipe work from PR #129 lands first, stack the
rotation helper commit onto that exact result. PR #129 did not change the six
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

### Historical predecessor recovery audit

The 2026-07-28 preparation audit found no recovery reason to retain its
then-current `P_M/P_G/P_C` as a historical controller tuple:

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
Those facts justified retiring that historical tuple instead of granting it
open-ended authority. They do not authorize retiring the current values above.
Re-audit active runs, protected-main history, canonical sidecars, and every
private ledger created since this record. If unresolved evidence names current
`P_C`, stop and add one bounded, purpose-named historical tuple before applying
the rotation.

## Preview and apply

Use full values:

```bash
set -euo pipefail

python3 -B scripts/rotate-publisher-trust.py \
  --predecessor-kandelo-sha "$P_M" \
  --predecessor-dry-run-kandelo-sha "$P_D" \
  --predecessor-first-publication-kandelo-sha "$P_F" \
  --predecessor-generation-tag "$P_G" \
  --predecessor-caller-sha256 "$P_C" \
  --kandelo-sha "$M" \
  --generation-tag "$G"

python3 -B scripts/rotate-publisher-trust.py \
  --predecessor-kandelo-sha "$P_M" \
  --predecessor-dry-run-kandelo-sha "$P_D" \
  --predecessor-first-publication-kandelo-sha "$P_F" \
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
- first-publication reusable namespace canary and its exact `kandelo-ref`:
  `M`; it receives no package-generation input;
- maintenance reusable publisher and `kandelo-ref`: `M`; generation: `G`;
- production reusable publisher and `kandelo-ref`: `M`; generation: `G`;
- Ruby caller trust constants, including first-publication trust: `M` and
  `G`; and
- rollout-controller current authority: `M`, `G`, and derived `C`.

The helper accepts only predecessor or successor values in each owned slot. It
also requires the raw production caller to hash to either `P_C` or the rendered
successor `C`; this prevents scalar-only validation from approving extra jobs,
permissions, secrets, or other unreviewed caller bytes. Files are replaced
atomically one at a time. If the host stops between files, rerun with the same
seven inputs to converge the partial application. A complete second invocation
is a no-op.

Review exactly the owned diff and the derived caller digest:

```bash
git diff -- \
  .github/workflows/dry-run-bottles.yml \
  .github/workflows/maintain-bottles.yml \
  .github/workflows/publish-bottles.yml \
  .github/workflows/repository-namespace-canary.yml \
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
`publisher-trust` job and the local validations above. Review the exact
convergence of `P_M`, `P_D`, and `P_F` to `M`, and of `P_G/P_C` to `G/C`.
Use only the explicitly authorized branch-protection/admin path for this
trust-root change.

Do not dispatch a Formula until the exact rotation commit is on protected tap
`main`, candidate trust checks are green, any required historical controller
authority has been reviewed, and Kandelo `main` still equals `M`.
