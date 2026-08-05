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
   authority is now `armed`, so dispatch fails closed. When the successor
   deliberately changes the sealed target source, supply all three successor
   source identities together. The helper verifies those identities against
   the manifest and inert source checkout before it changes the authority.
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

### Complete the terminal C6 to Ruby-only C7 transition

The C7 candidate starts from protected tap commit
`1d7d63673d70c7204fef83f9284f4367b30a8b8a`, which activated campaign
`f692a88aa001ae95bf9c7265012b082498ad4c1b0b84cd1c4c94b8b9a73d7f41`.
It records the terminal C6 ledger as 40 publicly verified handoffs plus the
failed Ruby workflow with no handoff. The canonical archive digest is
`3b1e288aadb23fa85db549cfc874aabc035756a18bace01b606ed0d1c54b9f07`;
the 40-reuse/Ruby-build scope digest is
`227830740f1c179e6194b32d7383d358b321763d1bbb7ff2ec029a549a47c315`.
The canonical 41-task graph remains unchanged at
`40a651d2ebe3a3aaab4bf9b65d91cf34db9908cb764a518437ac850747c4b139`.
The exact protected Kandelo commit is
`M=c157026d1234c9a28dc630d02f963828525897a7`; its anonymously validated
rootfs generation is
`G=package-generation-rootfs-wasm32-abi-v42-sha256-f44d50ad73b5bdd6c6f396b47806babff3b3fdc6869ee9f1d2f88f9460581fb4`.

Ruby failed before recipe execution because LLVM's bottle exposes
`etc/clang` through a keg-local symlink whose target was absent from the two
sealed native execution roots. C7 keeps every exact direct C6 handoff,
including Git, and rebuilds only Ruby under the corrected Kandelo executor.
The correction seals and admits exactly the prefix runtime root `etc/clang`;
it does not admit the rest of `etc`.

Before making the candidate executable:

1. Re-query protected Kandelo `main` and require exact `M`. Recheck the public
   generation and require exact `G`, then query every predecessor slot from
   the current tap tree. Do not substitute a PR head, synthetic merge, or
   expected future commit.
2. In one candidate tree, preview and apply `archive-active` with activation
   commit `1d7d63673d70c7204fef83f9284f4367b30a8b8a`; then preview and apply
   `rotate-publisher-trust` with the complete queried predecessor tuple,
   exact `M`, and exact `G`. Commit the archive, finalized scope,
   campaign-release caller, docs, trust tests, armed authority, and all
   rotation-owned files together as `T_ARM`.

The sealed target overlay is unchanged in C7: manifest SHA-256
`b430d1b934e3b5b07e8f7fcf1b3c1ab6737a82eb6722dad7b5fdaa81ea949243`,
source tree Git OID `8e825398d9ce414d6148ed2f8eac4e5de4ffb16c`, and target tree Git OID
`7e314590d18936d0ad3bf8ab42e49d7b4f234892`. `archive-active` validates the
already-present archive and writes the intermediate armed authority
atomically; the trust-rotation helper then advances the two equal Kandelo
fields to exact `M`. `archive-active` alone does not authorize C7, and rotating
an active C6 authority in place remains forbidden.

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
"f692-successor-scope.json"

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

## Establish the C7 executor and generation

The exact C7 executor and rootfs generation were established before this tap
arm. Do not repeat the earlier #1160 `force-rebuild.yml` procedure or promote
from the mutable `binaries-abi-v42` tag. C7 uses the same rootfs package bytes
through an independently verified package-cache projection because the
intervening Kandelo changes do not alter that package closure.

1. Set `M` to exact protected Kandelo commit
   `c157026d1234c9a28dc630d02f963828525897a7` and `G` to exact public release
   `package-generation-rootfs-wasm32-abi-v42-sha256-f44d50ad73b5bdd6c6f396b47806babff3b3fdc6869ee9f1d2f88f9460581fb4`.
2. Re-query protected Kandelo `main` and require it still equals `M`. Do not use
   a PR head, synthetic merge, or expected future commit.
3. Require the immutable public `generation.json` and its anonymous readback
   receipt to bind `.identity.authority_sha=M`,
   `.identity.validated_against_main.commit=M`, `.identity.abi_version=42`,
   `.identity.projection.root_package=rootfs`,
   `.identity.projection.arch=wasm32`, and
   `.identity.validated_against_main.method=identical-package-cache-projection-v1`.
4. Require its exact preserved source tag to be
   `preserved-package-generation-rootfs-wasm32-abi-v42-source-662f00c44f3e1d0ebc0d1a573df101e721b73006-sha256-0f60546befd9287a17420a00c0e2d68a5dbd22bc9d5861d31bd3e75acb38eb48`
   at `.identity.producer.evidence.tag` and its producer SHA to be
   `662f00c44f3e1d0ebc0d1a573df101e721b73006` at
   `.identity.producer.evidence.producer_sha`. Verify every selected archive
   against that preserved source; a successful workflow summary alone is not
   generation authority.

Use the exact already-published identities:

```bash
set -euo pipefail

M=c157026d1234c9a28dc630d02f963828525897a7
G=package-generation-rootfs-wasm32-abi-v42-sha256-f44d50ad73b5bdd6c6f396b47806babff3b3fdc6869ee9f1d2f88f9460581fb4
test "$(gh api repos/Automattic/kandelo/commits/main --jq .sha)" = "$M"
export M G
```

If Kandelo `main` advances before the arm merge or campaign-release write
boundary, stop. Derive a new authority bundle and independently validate an
appropriate generation against the new current-main commit; do not substitute
the new commit or relabel `G` inside this reviewed C7 tree. Publication and
maintenance re-query `main`, so stale `M` fails closed even after the tap has
selected it.

### Historical one-time Libyaml bootstrap overlap

An earlier migration stage overlapped its rootfs rebuild with the one-time
Libyaml namespace bootstrap. That bootstrap completed before C7 and must not be
repeated. This history explains why the predecessor tuple can contain distinct
dry-run and first-publication pins:

1. The stage started its exact-`M` rootfs rebuild immediately.
2. A separate tap pull request rotated only the dry-run caller, the
   first-publication caller, and their two Ruby trust constants to `M`.
   Production, maintenance, generation, and rollout-controller
   authority remained unchanged.
3. That tap pull request merged and its exact protected-main commit was
   recorded as `D`.
4. At exact `D`, the stage ran the Libyaml dry run and one-time first-child
   publication while tap `main` remained at `D`.
5. The first-publication workflow was disabled again after the child
   succeeded.
6. Once the rootfs generation yielded `G`, the stage ran the complete helper
   with `P_D="$M"` and `P_F="$M"`, while retaining the recorded
   predecessor values for `P_M`, `P_A`, `P_S`, `P_G`, and `P_C`.

The split state was intentional: dry-run could not publish, and
first-publication could create only the exact absent Libyaml child proved
by that dry run. Normal Formula publication remained pinned to
`P_M/P_A/P_S/P_G/P_C` until the complete rotation landed.
`Kandelo/test-workflow-trust.rb` permits these two live callers to
converge on current authority but still rejects collisions with
historical test fixtures.

For C7, query all predecessor slots from exact active base
`1d7d63673d70c7204fef83f9284f4367b30a8b8a` and rotate them together. Do not
rerun the namespace canary or introduce a new split rotation.

## Audit authority before changing files

Start from a clean checkout at exact active C6 tap commit
`1d7d63673d70c7204fef83f9284f4367b30a8b8a`. Apply the generated C7 archive,
scope, authority transition, complete trust rotation, campaign-release caller,
tests, and documentation in one reviewed arm tree. Do not stack unrelated work
or silently rebase these identities onto a different protected base.

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
