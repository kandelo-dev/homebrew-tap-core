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
3. When advancing the executable tuple to both a new Kandelo SHA and a newly
   admitted generation, run the trust-rotation helper in the same candidate
   tree. If `M`, `G`, and the production caller tuple all remain exact, do not
   manufacture a rotation: prove the live caller and controller bytes
   unchanged instead. A mixed transition needs its own reviewed procedure.
   Commit the reviewed successor scope and its exact task graph in this tree.
   Merge that complete candidate as tap commit `T_ARM`.
4. Derive and publish the successor campaign with Kandelo `M` and
   source tap commit `T_ARM`. The immutable release must target
   `T_ARM`. Verify it anonymously before activation.
5. Start a new branch from exact `T_ARM`. Preview and apply
   `activate-successor` with the public campaign bytes, `G`, and
   `T_ARM`. The resulting commit may change only the data authority.
6. Merge the data-only activation, then dispatch from its exact
   protected-main commit.

### Historical terminal C6 to Ruby-only C7 transition

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

### Historical terminal C7 to Ruby-only C8 transition

The C8 candidate starts from protected tap commit
`454e5d54456c8d870496bacc0ba9c2759c863ab1`, which activated campaign
`8edea42ae932691b45c8695d5d6ab93a4a7ce1e08ee492ce3d7ead51fa45a185`.
Its terminal ledger contains 40 publicly verified handoffs and failed
Ruby/wasm32 run `31017507098` with no handoff. The exact terminal archive
digest is
`76c26c5af78a97bdcb840884451ca007ab95a37645b7db7804008646b2ca4150`;
the 40-reuse/Ruby-build scope digest is
`dce71abbeb512b74adb3469a1388ccbdcbbfda28c124fe46f6773d96b8e59841`.
The canonical 41-task graph and sealed target overlay remain unchanged.

Ruby again failed before recipe execution, this time because authenticated
LLVM 22.1.8 expands to 2,624,809,107 regular-file bytes, above the former
2 GiB native-keg aggregate. C8 raises only each authenticated native tool keg
and its exact target-Cellar proxy to 4 GiB. It preserves the 1 GiB per-file
limit and the existing 2 GiB limits for true target dependencies, recipe
source, and recipe output.

### Historical terminal C8 to Ruby-only C9 transition

The C9 candidate starts from protected tap commit
`9bbdbd334e4f45bf780e4d139cda1dc865a21419`, which activated terminal C8
campaign
`a516aa5e61f4b7513c18c3e5b279a6a1f2d8b07e6a7348706238bc261a63ada4`.
Its terminal ledger contains 40 publicly verified handoffs and failed
Ruby/wasm32 run `31043674986` with no handoff. The exact terminal archive is
`Kandelo/campaigns/prefix-v1/aborted-campaigns/a516aa5e61f4b7513c18c3e5b279a6a1f2d8b07e6a7348706238bc261a63ada4.json`,
sealed as
`7d8a7a9d1ac4df5c5dda459990384a5fe296511217053edf2a8d13c16703a483`.
The 40-reuse/Ruby-build scope is
`Kandelo/campaigns/prefix-v1/successor/a516-successor-scope.json`, sealed as
`a721afcecf9cde3185dcb6d5791a80e35ae99169bdd1a82666d63775ac32e187`.
The canonical 41-task graph and sealed target overlay remain unchanged.

Ruby failed before recipe execution because the generic target-Cellar seal
rejected authenticated LLVM 22.1.8's launcher-registered `etc/clang` bridge
into the separately sealed native prefix. C9 preserves that link only for the
exact immutable proxy recorded by the launcher's native-bridge transaction.
It revalidates the registered source, proxy shape, ownership, modes, opt link,
and component-aware native projection before excluding only that selected keg
from the ordinary containment rule. Unregistered or redirected cross-prefix
links and writable or changed proxies still fail closed.

The C9 candidate was finalized with the protected Kandelo commit and
independently admitted generation:

- `M=45a45fed06ff053ee4dd2cc2bb6564a99d5ce106`; and
- `G=package-generation-rootfs-wasm32-abi-v42-sha256-e3701277b519832435260e183b83ca7e1e82b12f84de6c24605db03552719e40`.

Both were required across every rotation-owned executable, trust, authority,
and documentation slot before merge. Historical C6, C7, and C8 evidence was
not a finalization target.

### Complete the terminal C10 to Ruby-only C11 transition

The C11 candidate starts from protected tap commit
`5fec71d3e3de0f0fc8a0b543bee0c4afbe4bb810`, which activated terminal C10
campaign
`ac950955718d406fa3ee31a7396c22c13ede154f948673f28171ca49592c2f34`.
Its terminal ledger contains 40 publicly verified handoffs and failed
Ruby/wasm32 run `31069244063` with no handoff. The run installed the declared
authenticated `gpatch` dependency and entered the isolated recipe runner, but
stopped before patching or compiling because the Linux `gpatch` keg exposes
`bin/patch`, while the recipe required the macOS-prefixed name `gpatch`.

The exact terminal archive is
`Kandelo/campaigns/prefix-v1/aborted-campaigns/ac950955718d406fa3ee31a7396c22c13ede154f948673f28171ca49592c2f34.json`,
sealed as `f861ae7e8b4f2669ec1851a943c1ac6ad92c780e20e2e38fac5785cd84109b15`.
The 40-reuse/Ruby-build scope is
`Kandelo/campaigns/prefix-v1/successor/ac95-successor-scope.json`, sealed as
`a5073d0351dd3d802b87bb0ff48052dc741c12e547e0184963549846cf81aba5`.
The canonical 41-task graph remains unchanged.

The C11 correction is tap-only. It passes exact executable paths from the
declared `gpatch`, `make`, `perl`, and `python@3.13` kegs into the isolated
recipe and invokes only those paths. This fixes the observed Linux `patch`
name and the same latent Linux `make` versus macOS `gmake` mismatch while
preventing ambient Perl or Python from satisfying declared dependencies. It
also removes the unused Formula-level Rust build dependency; the authenticated
local-root-spill transform remains a separately sealed campaign input. The
corrected target source is sealed as manifest SHA-256
`3359e8d45d6c04de2d3cac146c225a3bc54beb176b4018d082b337c7a49c298e`, source
tree Git OID `17bcb5910fd3d403d861b695f9ee945f1ce14d30`, and target tree Git OID
`f235ec029446883f067db5ea5d7e179710167dc6`.

No Kandelo executor or rootfs package input changes. The candidate retains:

- `M=45a45fed06ff053ee4dd2cc2bb6564a99d5ce106`; and
- `G=package-generation-rootfs-wasm32-abi-v42-sha256-e3701277b519832435260e183b83ca7e1e82b12f84de6c24605db03552719e40`.

Require those exact values and the five resolved archive, scope, manifest, and
tree seals across the atomic arm before merge. Historical C10 campaign bytes
and all 40 predecessor handoffs remain immutable evidence.

Before making the candidate executable:

1. Re-query protected Kandelo `main` and require exact `M`. Recheck the public
   generation and require exact `G`, then query every predecessor slot from
   the current tap tree. Do not substitute a PR head, synthetic merge, or
   expected future commit.
2. In one candidate tree, preview and apply `archive-active` with activation
   commit `5fec71d3e3de0f0fc8a0b543bee0c4afbe4bb810` and all three corrected
   target-source identities. Do not run `rotate-publisher-trust`: every live
   Kandelo pin already equals exact `M`, every generation pin already equals
   exact `G`, and the production caller digest remains exact `P_C`. Commit the
   archive, finalized scope, campaign-release caller, docs, trust tests,
   armed authority, and target-contract consumers together as `T_ARM`.

The sealed target overlay changes in C11: manifest SHA-256
`3359e8d45d6c04de2d3cac146c225a3bc54beb176b4018d082b337c7a49c298e`, source
tree Git OID `17bcb5910fd3d403d861b695f9ee945f1ce14d30`, and target tree Git OID
`f235ec029446883f067db5ea5d7e179710167dc6`. `archive-active` validates the
already-present archive and writes the intermediate armed authority
atomically. It preserves the two already-equal Kandelo fields at exact `M`
while clearing campaign, generation, and source-commit execution data. The
later data-only activation restores exact `G`; no live publisher trust changes
at the arm boundary. `archive-active` alone does not authorize C11, and
rotating an active C10 authority in place remains forbidden.

Archive and arm the predecessor without rotating the unchanged live caller
tuple:

```bash
python3 -B scripts/transition-prefix-campaign-authority.py \
  archive-active \
  --archive "$PREDECESSOR_ARCHIVE" \
  --activation-commit "$PREDECESSOR_ACTIVATION" \
  --successor-manifest-sha256 \
    3359e8d45d6c04de2d3cac146c225a3bc54beb176b4018d082b337c7a49c298e \
  --successor-source-tree-git-oid \
    17bcb5910fd3d403d861b695f9ee945f1ce14d30 \
  --successor-target-tree-git-oid \
    f235ec029446883f067db5ea5d7e179710167dc6

python3 -B scripts/transition-prefix-campaign-authority.py \
  archive-active \
  --archive "$PREDECESSOR_ARCHIVE" \
  --activation-commit "$PREDECESSOR_ACTIVATION" \
  --successor-manifest-sha256 \
    3359e8d45d6c04de2d3cac146c225a3bc54beb176b4018d082b337c7a49c298e \
  --successor-source-tree-git-oid \
    17bcb5910fd3d403d861b695f9ee945f1ce14d30 \
  --successor-target-tree-git-oid \
    f235ec029446883f067db5ea5d7e179710167dc6 \
  --apply
```

After `T_ARM` is protected main and the successor release is public,
activate only its data:

```bash
SUCCESSOR_SCOPE="Kandelo/campaigns/prefix-v1/successor/"\
"ac95-successor-scope.json"

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

### Complete the terminal C11 to Ruby-only C12 transition

The C12 candidate starts from protected tap commit
`f4daa689d89b2de2a4359bf358854a7db130ca97`, which activated terminal C11
campaign
`b0476cd05b16a835bd42292bcd34bffdada50f6d06bb1129bc106a9f86763896`.
Its terminal ledger contains 40 publicly verified handoffs and failed
Ruby/wasm32 run `31075257926` with no handoff. The run passed campaign
admission and planning, then failed in the signed native API contract before
native dependency installation, Formula recipe execution, bottle publication,
or handoff publication because the signed Homebrew API selected a newer
`python@3.13` than the checked-in compatibility lock.

The exact terminal archive is
`Kandelo/campaigns/prefix-v1/aborted-campaigns/b0476cd05b16a835bd42292bcd34bffdada50f6d06bb1129bc106a9f86763896.json`,
sealed as `0c31f4b6a4eb24f1bc193a1b807d9352e81a76a3995453020c5bd16847573f32`.
The 40-reuse/Ruby-build scope is
`Kandelo/campaigns/prefix-v1/successor/b047-successor-scope.json`, sealed as
`84a43358c03dd6700b2edf6c337f7d22523af69207a07eb9babc99452c7a0d88`.
The canonical 41-task graph and sealed target source remain unchanged:
manifest SHA-256
`3359e8d45d6c04de2d3cac146c225a3bc54beb176b4018d082b337c7a49c298e`,
source tree Git OID `17bcb5910fd3d403d861b695f9ee945f1ce14d30`, and target tree Git OID
`f235ec029446883f067db5ea5d7e179710167dc6`.

C12 advances the executor to the reviewed native Formula records on exact
protected Kandelo `main` and independently admits the preserved rootfs
projection against that commit:

- `M=af80a443a6b4820e3b04845a64ab5cb8854638cd`; and
- `G=package-generation-rootfs-wasm32-abi-v42-sha256-7ed33d5d51b7362c2ac04c0aca812a49c859bde25a2930d0e876f1c1e1aafcc9`.

The predecessor production caller is sealed as
`bdc530070e9586e517daba7a2ec5e1f832e3342ab178b71e8f84746d2dd18cf0`.
The complete reviewed rotation renders its successor as
`e219c9b2fca71f28494374259b58978b75ce780983868410aacc5ed506c9c381`.
No historical campaign, handoff, source tree, or rootfs archive is relabeled
by that executable-authority transition.

In one candidate tree, preview and apply `archive-active` with activation
commit `f4daa689d89b2de2a4359bf358854a7db130ca97` and the exact C11 archive.
Because the target source is unchanged, do not supply replacement target-source
identities. Require the intermediate authority to retain the C11 Kandelo pins
while clearing the campaign, generation, and source commit. Then preview and
apply `rotate-publisher-trust` with every predecessor Kandelo slot equal to
`45a45fed06ff053ee4dd2cc2bb6564a99d5ce106`, predecessor generation
`package-generation-rootfs-wasm32-abi-v42-sha256-e3701277b519832435260e183b83ca7e1e82b12f84de6c24605db03552719e40`,
and predecessor caller digest `bdc530070e9586e517daba7a2ec5e1f832e3342ab178b71e8f84746d2dd18cf0`.
The resulting armed authority must name exact `M` in both Kandelo fields while
its campaign, generation, and source identities remain zero. Commit the
archive, scope, campaign-release caller, helper-owned rotation, authority,
tests, and documentation together as `T_ARM`.

## Advance the executor and generation for C12

C12 changes the Kandelo-owned reviewed native Formula records, so it cannot
retain C11's executor as current authority. Exact protected Kandelo `main` is
`af80a443a6b4820e3b04845a64ab5cb8854638cd`, and its independently admitted
rootfs generation is
`package-generation-rootfs-wasm32-abi-v42-sha256-7ed33d5d51b7362c2ac04c0aca812a49c859bde25a2930d0e876f1c1e1aafcc9`.
The older executor and generation remain C11 archive authority; the rotation
does not relabel them.

Do not repeat the earlier #1160 `force-rebuild.yml` procedure or promote from
the mutable `binaries-abi-v42` tag. C12 uses the independently admitted,
content-addressed projection of the preserved rootfs source against exact `M`.

1. Keep both exact identities equal in every rotation-owned slot. Do not
   substitute a PR head, synthetic merge, predecessor executor, or expected
   future commit.
2. Keep protected Kandelo `main` frozen at `M`; `G` is the anonymously verified
   content-addressed projection of the preserved rootfs source against `M`.
3. Re-query protected Kandelo `main` and require it still equals `M`.
4. Require the immutable public `generation.json` and its anonymous readback
   receipt to bind `.identity.authority_sha=M`,
   `.identity.validated_against_main.commit=M`, `.identity.abi_version=42`,
   `.identity.projection.root_package=rootfs`,
   `.identity.projection.arch=wasm32`, and
   `.identity.validated_against_main.method=identical-package-cache-projection-v1`.
5. Require its exact preserved source tag to be
   `preserved-package-generation-rootfs-wasm32-abi-v42-source-662f00c44f3e1d0ebc0d1a573df101e721b73006-sha256-0f60546befd9287a17420a00c0e2d68a5dbd22bc9d5861d31bd3e75acb38eb48`
   at `.identity.producer.evidence.tag` and its producer SHA to be
   `662f00c44f3e1d0ebc0d1a573df101e721b73006` at
   `.identity.producer.evidence.producer_sha`. Verify every selected archive
   against that preserved source; a successful workflow summary alone is not
   generation authority.

The finalized arm uses the independently verified values:

```bash
set -euo pipefail

M=af80a443a6b4820e3b04845a64ab5cb8854638cd
G=package-generation-rootfs-wasm32-abi-v42-sha256-7ed33d5d51b7362c2ac04c0aca812a49c859bde25a2930d0e876f1c1e1aafcc9
[[ "$M" =~ ^[0-9a-f]{40}$ ]]
[[ "$G" =~ ^package-generation-rootfs-wasm32-abi-v42-sha256-[0-9a-f]{64}$ ]]
test "$(gh api repos/Automattic/kandelo/commits/main --jq .sha)" = "$M"
export M G
```

If Kandelo `main` advances before the arm merge or campaign-release write
boundary, stop. Derive a new authority bundle and independently validate an
appropriate generation against the new current-main commit; do not substitute
the new commit or relabel `G` inside this reviewed C12 tree. Publication and
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

For C12, query all predecessor slots from exact active base
`f4daa689d89b2de2a4359bf358854a7db130ca97` and require them to remain the
complete C11 tuple. Run the rotation helper only with that complete
predecessor and the exact reviewed `M`, `G`, and derived caller digest. Do not
rerun the namespace canary or introduce a split rotation.

## Audit authority before changing files

Start from a clean checkout at exact active C11 tap commit
`f4daa689d89b2de2a4359bf358854a7db130ca97`. Apply the generated terminal-C11
archive, C12 scope, authority transition, campaign-release caller, complete
helper-owned trust rotation, tests, and documentation in one reviewed arm
tree. Prove the sealed target source and every path outside that reviewed
boundary byte-identical to the protected base. Do not stack unrelated work or
silently rebase these identities onto a different protected base.

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
