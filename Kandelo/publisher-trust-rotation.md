# Publisher Trust Rotation

This runbook rotates every live protected tap caller and its in-repository
trust roots from one complete authority tuple to another:

- `P_M`: the exact predecessor Kandelo main commit selected by write callers;
- `P_D`: the exact predecessor Kandelo main commit selected by the dry-run
  caller;
- `P_F`: the exact predecessor Kandelo commit selected by the
  first-publication namespace canary;
- `P_A`: the exact predecessor Kandelo commit selected by the prefix-campaign
  callers and campaign authority;
- `P_S`: the exact predecessor Kandelo commit selected by the
  closed-selection caller;
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

This is historical evidence, not an executable input tuple for the
expanded helper: the pre-#1160 record predates `P_A` and `P_S`.
Query the current protected tree and record both before any new
rotation. Do not infer them from this section.

Re-query protected tap `main` immediately before preparing the live rotation.
If any current caller or trust-root slot differs, stop and review the new
predecessor instead of editing these values until the helper accepts the tree.
The prefix workflow's three bottle-publisher pins and one first-child pin, the
campaign authority's two Kandelo fields, and
`PREFIX_CAMPAIGN_KANDELO_SHA` must all name `P_A`. The closed-selection
workflow's publisher and `kandelo-ref`, plus
`CLOSED_SELECTION_KANDELO_SHA`, must all name `P_S`.
Formula sidecars and aggregate metadata also contain historical
`kandelo_commit` values. They are artifact provenance and are intentionally not
rotation-owned; never globally replace `P_M`.

## Replace an active campaign in two protected commits

Do not rotate an active campaign's Kandelo fields in place. Its
immutable release binds the old executor, generation, source tap, and
handoffs. A successor also cannot select workflow bytes that GitHub has
never seen on the default branch.

Use two tap commits:

1. Build one canonical abandoned-campaign archive from the old public
   runs and handoffs. Put it at the content-addressed path for the old
   campaign digest.
2. Preview and apply `archive-active`. It proves the old active
   authority came from the named activation commit and validates the
   archive's recorded authority against those exact bytes. It clears
   the campaign tag, generation, and source commit together. The
   authority is now `armed`, so dispatch fails closed.
3. In the same candidate tree, run the trust-rotation helper. This
   rotates every reusable workflow, including the prefix campaign, to
   `M`. Commit the reviewed successor scope and its exact task graph in
   this tree. Merge that complete candidate as tap commit `T_ARM`.
4. Derive and publish the successor campaign with Kandelo `M` and
   source tap commit `T_ARM`. The immutable release must target
   `T_ARM`. Verify it anonymously before activation.
5. Start a new branch from exact `T_ARM`. Preview and apply
   `activate-successor` with the public campaign bytes, `G`, and
   `T_ARM`. The resulting commit may change only the data authority.
6. Merge the data-only activation, then dispatch from its exact
   protected-main commit.

### Finalize the terminal C5 to Ruby-only C6 candidate

The current structural candidate starts from protected tap commit
`03b53348c4291ca421a48d2d0890f4b5a56ae380`, which activated campaign
`9705e20fa5cdbbf41bb0254aab4eb75278e091549e4bf6ee6ae79decdf029eae`.
It deliberately does not guess terminal run evidence, the next Kandelo commit
`M` (K2), or generation `G`.

Before making the candidate executable:

1. Freeze the real terminal C5 ledger at
   `Kandelo/campaigns/prefix-v1/aborted-campaigns/`
   `9705e20fa5cdbbf41bb0254aab4eb75278e091549e4bf6ee6ae79decdf029eae.json`.
   Preserve every actual dispatch and unique run ID. Exactly the 40 selected
   non-Ruby tasks must end in `handoff-published-and-publicly-verified`; Ruby
   must not have a verified handoff and remains the sole fresh build. Do not
   infer a public handoff from a successful build or an authenticated query.
2. Compute the canonical archive bytes' SHA-256 and replace
   `__C5_TERMINAL_ARCHIVE_SHA256__` in
   `successor/9705-successor-scope.json` and its trust-test constant.
3. Recompute the now-final scope bytes' SHA-256 and replace
   `__C6_SUCCESSOR_SCOPE_SHA256__` in the campaign-release caller and its
   trust-test constant. The scope must still be the exact union of 40 C5 reuse
   tasks and the Ruby build in `canonical-shell41-wasm32.json`.
4. Query protected Kandelo `main` for exact `M`, publish and validate the
   exact `M` rootfs generation `G`, and query every predecessor slot from the
   current tap tree. Do not substitute a PR head, synthetic merge, or expected
   future commit.
5. In one candidate tree, preview and apply `archive-active` with activation
   commit `03b53348c4291ca421a48d2d0890f4b5a56ae380`; then preview and apply
   `rotate-publisher-trust` with the complete queried predecessor tuple,
   exact `M`, and exact `G`. Commit the archive, finalized scope,
   campaign-release caller, docs, trust tests, armed authority, and all
   rotation-owned files together as `T_ARM`.

The two placeholder strings are intentionally not SHA-256 values. The scope
parser and campaign-release admission therefore reject this structural
prototype until steps 1-3 have used exact evidence. `archive-active` alone
does not authorize C6, and rotating an active C5 authority in place remains
forbidden.

Archive and arm the predecessor before running the rotation helper:

```bash
python3 -B scripts/transition-prefix-campaign-authority.py \
  archive-active \
  --archive "$PREDECESSOR_ARCHIVE" \
  --activation-commit "$PREDECESSOR_ACTIVATION"

python3 -B scripts/transition-prefix-campaign-authority.py \
  archive-active \
  --archive "$PREDECESSOR_ARCHIVE" \
  --activation-commit "$PREDECESSOR_ACTIVATION" \
  --apply
```

After `T_ARM` is protected main and the successor release is public,
activate only its data:

```bash
SUCCESSOR_SCOPE="Kandelo/campaigns/prefix-v1/successor/"\
"9705-successor-scope.json"

python3 -B scripts/transition-prefix-campaign-authority.py \
  activate-successor \
  --campaign "$SUCCESSOR_CAMPAIGN" \
  --scope "$SUCCESSOR_SCOPE" \
  --rootfs-generation "$G" \
  --source-tap-commit "$T_ARM"

python3 -B scripts/transition-prefix-campaign-authority.py \
  activate-successor \
  --campaign "$SUCCESSOR_CAMPAIGN" \
  --scope "$SUCCESSOR_SCOPE" \
  --rootfs-generation "$G" \
  --source-tap-commit "$T_ARM" \
  --apply

test "$(git status --porcelain=v1)" = \
  " M Kandelo/prefix-campaign-authority.json"
```

The activation helper derives the release tag from the exact campaign
bytes. Kandelo derives that campaign only when the successor scope path
and SHA-256 are supplied together. The campaign's optional schema-3
`authority.successor_scope = {path, sha256}` record is mandatory for
this activation and must name the exact scope bytes from `T_ARM`. The
helper also reparses those scope, graph, and archive bytes from exact
`T_ARM`. The graph is the selected 41-task shell proof, not the full
campaign inventory. It must be the exact union of the reviewed reuse
and build routes. Every selected reuse route must name its
archive-verified handoff, and every selected build must remain a build.
Other valid campaign Formula variants remain available for independent
dispatch but are not scheduled by this graph. If they reuse an older
handoff, their recovery record and route must bind exact archive bytes
from `T_ARM`; unused or altered recovery records are rejected. The
campaign's Kandelo commit, source commit, ABI, tap identity, and sealed
target source must also match the armed tap.

The transition helper binds the archive's reviewed authority bytes. It
does not query GitHub and cannot prove that historical runs or handoffs
were public. The successor publisher/executor from PR #1215 later
fetches each selected predecessor campaign, handoff, and bottle
anonymously and validates their content-addressed evidence. The
scheduler independently reads each new successor handoff before
marking it usable. Those checks, publication, and anonymous release
readback remain separate because this helper has no credentials.

If an original workflow published a complete handoff but failed in a later
tap-side validation step, do not rewrite its terminal scheduler entry and do
not use GitHub's re-run control. First merge the verifier correction to
protected main, then run the exact protected verifier bytes locally without
credentials and freeze the complete readback as a separately checksummed
supplement. The archive cause must name the protected correction commit and
supplement digest. Only then may the archive classify that original release as
publicly verified; its dispatch still names the original publication run and
release identity. A local pre-merge check or an authenticated metadata query is
not archive authority.

The transition helper intentionally does not import Kandelo's complete
campaign parser. That parser belongs to exact `M`, loads companion
Kandelo modules, and runs in Kandelo's declared build environment.
Making this tap-side data transition depend on an external checkout
would replace one reviewed contract with mutable operator state.
Instead, this helper validates only the cross-repository activation
boundary: exact source, recovery, task graph, and routes. Every task is
then passed through the complete `M`-pinned Kandelo validator before
any publication write.

Both transition commands are exact-state idempotent before their result
is committed. Repeating `--apply` accepts only the same derived state
and performs no second write.

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
   predecessor values for `P_M`, `P_A`, `P_S`, `P_G`, and `P_C`.

The split state is intentional: dry-run cannot publish, and
first-publication can create only the exact absent Libyaml child proved
by that dry run. Normal Formula publication remains pinned to
`P_M/P_A/P_S/P_G/P_C` until the complete rotation lands.
`Kandelo/test-workflow-trust.rb` permits these two live callers to
converge on current authority but still rejects collisions with
historical test fixtures.

## Audit authority before changing files

Start from a clean checkout containing the final tap changes that the rotation
will accompany. If the closed-recipe work from PR #129 lands first, stack the
rotation helper commit onto that exact result. PR #129 did not change the nine
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
  --predecessor-campaign-kandelo-sha "$P_A" \
  --predecessor-closed-selection-kandelo-sha "$P_S" \
  --predecessor-generation-tag "$P_G" \
  --predecessor-caller-sha256 "$P_C" \
  --kandelo-sha "$M" \
  --generation-tag "$G"

python3 -B scripts/rotate-publisher-trust.py \
  --predecessor-kandelo-sha "$P_M" \
  --predecessor-dry-run-kandelo-sha "$P_D" \
  --predecessor-first-publication-kandelo-sha "$P_F" \
  --predecessor-campaign-kandelo-sha "$P_A" \
  --predecessor-closed-selection-kandelo-sha "$P_S" \
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
- the closed-selection publisher and exact `kandelo-ref`: `M`;
- the armed campaign's three bottle publishers, first-child publisher,
  authority Kandelo commit, and reusable-workflow commit: `M`;
- Ruby caller trust constants, including first-publication and
  closed-selection trust: `M` and `G`; and
- rollout-controller current authority: `M`, `G`, and derived `C`.

The helper accepts only predecessor or successor values in each owned slot. It
also requires the raw production caller to hash to either `P_C` or the rendered
successor `C`; this prevents scalar-only validation from approving extra jobs,
permissions, secrets, or other unreviewed caller bytes. Files are replaced
atomically one at a time. If the host stops between files, rerun with
the same nine inputs to converge the partial application. A complete
second invocation is a no-op.

The closed-selection caller also forwards `expected_caller_sha` from
each dispatch. Capture the exact protected tap `main` commit
immediately before dispatch and pass that value. The reusable
publisher stops before preparation if GitHub resolves the run at any
other tap commit.

Review exactly the owned diff and the derived caller digest:

```bash
git diff -- \
  .github/workflows/dry-run-bottles.yml \
  .github/workflows/maintain-bottles.yml \
  .github/workflows/prefix-campaign-bottles.yml \
  .github/workflows/publish-bottles.yml \
  .github/workflows/publish-closed-selection.yml \
  .github/workflows/repository-namespace-canary.yml \
  Kandelo/prefix-campaign-authority.json \
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
convergence of `P_M`, `P_D`, `P_F`, `P_A`, and `P_S` to `M`, and of
`P_G/P_C` to `G/C`.
Use only the explicitly authorized branch-protection/admin path for this
trust-root change.

Do not dispatch a Formula until the exact rotation commit is on protected tap
`main`, candidate trust checks are green, any required historical controller
authority has been reviewed, and Kandelo `main` still equals `M`.
