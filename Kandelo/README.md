# Kandelo Homebrew Tap Data

The active shipping lane publishes each bottle independently through
`.github/workflows/publish-bottles.yml`. Canonical files in
`Kandelo/selections/` choose exact bottle bytes for VFS composition; they do
not consume a campaign, aggregate seal, Formula bottle block, or legacy
sidecar. `.github/workflows/selection-checks.yml` validates those selections.

The campaign/trust workflows and enforcement tools are preserved under
`deferred/campaign-trust-implementation/` and are not active contracts. The
remaining campaign records and sections explicitly labeled historical below
are retained only as migration history until the separate post-cutover archive
change.

## Active ABI request data

`staging/request-issuers.toml` is the tap-owned authority for accepting public
Kandelo staging requests. It binds one issuer repository and immutable workflow
path, this exact tap identity, canonical schema and kind, public GitHub hosts,
and all input bounds. It does not select an ABI or grant candidate execution.
`staging/reconciliation-activation.toml` accepts `active` requests, while
candidate publication, product evidence, promotion, and cleanup retain their
independent protected activation boundaries.

Protected code under `scripts/abi_staging/` treats request and GitHub response
bytes as inert data. It rejects noncanonical or oversized requests, filename
and digest drift, unauthorized issuers, wrong Release identities, redirect
escapes, and unaddressed taps. The scheduled and manual workflow can report
whether an exact issued head is current, historical, closed, reopened, or
merged and can schedule the uncredentialed build lane for an exact current
request. Its credentialed coordinator still cannot execute candidate code.

The first active request is a hosted cutover canary. A reconciled request is
not bottle admission, verification, endorsement, promotion, or a current-ABI
update; those steps remain disabled until their own protected activations and
canaries land.

GHCR does not let anonymous discovery distinguish a never-created staging
repository from a private one. The coordinator treats that response only as
an empty public inventory. The credentialed publisher must independently find
the package absent, or already public and associated with this tap, before its
first write; afterward exact digest readback must succeed anonymously.

## Legacy sidecar metadata

Trusted publish workflows generate this directory in the
`kandelo-dev/tap-core` tap in the `kandelo-dev/homebrew-tap-core` repository.
Checked-in files make metadata reviewable in the tap commit.
`bottles-abi-v<N>` is the sidecar ABI namespace; the current workflow does not
duplicate this payload into a GitHub Release.

These two names serve different contracts. Homebrew references, receipts, OCI
titles, Brewfiles, and sidecar tap fields use the canonical tap identity
`kandelo-dev/tap-core`. Public bottle URLs use the exact repository-rooted GHCR
namespace `https://ghcr.io/v2/kandelo-dev/homebrew-tap-core`, retaining the
repository's `homebrew-` prefix. Production child and version-index writes use
only the caller repository's scoped built-in `GITHUB_TOKEN` (`github.token`);
the workflow accepts no package PAT and finalizes sidecars only after anonymous
bottle readback.

## Files

```text
metadata.schema.json
formula.schema.json
link-manifest.schema.json
provenance.schema.json
vfs-acceptance.json                                # optional tap-owned gate selection
vfs-acceptance.Brewfile                           # optional selected static roots
vfs-acceptance-shell.json                         # optional reviewed image shell policy

metadata.json                                      # generated tap state
formula/<name>.json                               # generated tap state
link/<name>-<version>-rebuild<N>-<arch>.json      # generated tap state
reports/<name>-<version>-rebuild<N>-<arch>.provenance.json
```

The `examples/` directory contains fixture data for schema and semantic
validator development. It is not published metadata.

When present, `vfs-acceptance.json` and its referenced files are reviewed tap
policy, not generated sidecars. The publisher reads them from the exact tap
commit and never rewrites them.

The tap has no checked-in legacy VFS-acceptance policy. Canonical flat
selections under `Kandelo/selections/` replace that aggregate sidecar policy
for the current `/opt/kandelo/homebrew` layout. The normal bottle publisher
treats legacy dependency-closure acceptance as optional; Kandelo's flat VFS
workflow proves an exact selected image through Node and Chromium lifecycle
tests.

## Generation

The publish workflow generates this directory with:

```bash
cd /path/to/kandelo
scripts/dev-shell.sh cargo xtask homebrew-sidecars \
  --tap-root /path/to/homebrew-tap-core \
  --input /path/to/sidecars-input.json \
  --previous-metadata /path/to/previous/Kandelo/metadata.json
```

The input manifest is workflow evidence: tap and Kandelo commits, ABI release
tag, formula identities, bottle status, link-plan data, build evidence,
validation outcome lists, and local `bottle_file` paths. The generator hashes
the local bottle files itself and writes the resulting `sha256` and `bytes`
into metadata, formula sidecars, link manifests, and provenance reports.

The publisher carries this evidence across fresh jobs only in strict data
handoffs. Artifact-provided scripts and environment files are rejected. The
trusted in-tree generator creates sidecars on a read-only verification runner,
and a separate tap finalizer validates the complete publication payload as
inert data before acquiring push credentials.

When a current bottle is `failed`, `pending`, or `building`,
`--previous-metadata` provides the last-green fallback. The fallback is copied
only for the same ABI, package, version, rebuild, and arch.

## Validation Split

JSON Schema validates object shape, required fields, enum values, scalar
formats, and basic path syntax.

The semantic validator must still check cross-file and artifact facts:

- metadata ABI matches the `bottles-abi-v<N>` namespace;
- formula sidecars match their package entry in `metadata.json`;
- bottle `arch` and `bottle_tag` agree;
- Formula bottle root, tags, and SHA-256 digests exactly match the successful
  or last-green fallback bottles in sidecar metadata;
- an empty `runtime_support` array has one successful `support_data_test` and
  no successful executable-runtime evidence;
- `node` and `browser` runtime claims have their matching successful smoke
  evidence, and `browser_compatible` agrees exactly with the `browser` claim;
- link-manifest paths do not escape the Homebrew prefix;
- link sources exist inside the verified bottle payload;
- bottle sha256, cache key, metadata sha, and provenance fields agree;
- fallback link manifests still exist for non-success bottles.

Run the repo-local validator against a generated tap checkout:

```bash
cd /path/to/kandelo
scripts/dev-shell.sh cargo xtask homebrew-validate \
  --tap-root /path/to/homebrew-tap-core
```

The validator checks the current sidecar JSON, Ripper-parsed static Formula
bottle structure and data, link-manifest consistency, provenance reports, and
fallback link references. It does not fetch bottle bytes or evaluate Formula
Ruby.

A passing local semantic validator is necessary but not sufficient publication
evidence. The trusted publisher enforces public GHCR readback, exact bottle-byte
verification, Homebrew pour/test evidence, and runtime and browser gates.
Formula-specific provenance records the bottle and runtime results; run-scoped
transport receipts provide the registry proof.

## Historical first `libyaml` GHCR child

`deferred/campaign-trust-implementation/workflows/repository-namespace-canary.yml`
was the narrow bootstrap caller for the then-absent public
`homebrew-tap-core/libyaml` package. It is
fixed in reviewed YAML to Formula `libyaml`, architecture `wasm32`, this tap's
exact protected-main commit, and one immutable Kandelo workflow commit. The
workflow can run only through the separate `publish-first-homebrew-child`
repository dispatch; neither an ordinary dry run nor the prefix campaign can
invoke it.

The dispatch supplies evidence, not publication policy: one completed-success
`dry-run-bottles.yml` run ID and attempt from the same tap commit, the unique
unexpired Actions archive digest for
`homebrew-oci-child-libyaml-wasm32-attempt-<N>`, and that child's OCI manifest
digest. The reusable workflow validates the archive and its receipt without
registry credentials. Only then does its repository-scoped `GITHUB_TOKEN`
require authenticated absence of both the descriptor and package repository,
copy the actual content-derived child, retire its ORAS credentials, and require
anonymous readback of the exact digest. A mutable ref, a different or failed
run, ambiguous or expired artifact evidence, an existing public or private
package, or non-public readback fails closed.

This path publishes no marker, mutable version index, Formula edit, sidecar, or
campaign handoff. It is serialized per Formula and must reject every
replay after the package repository exists. A separate normal `libyaml`
publication was still required to publish and verify the complete index,
finalize tap state, and pass the acceptance procedure below. Its complete C6
handoff is now among C7's selected reuse evidence. The one-time namespace
bootstrap is complete and must not be run again.

For audit history, the completed bootstrap first ran a fresh
`libyaml`/`wasm32` dry run after both prerequisite commits reached their exact
protected `main` refs. The operator inspected the run and artifact through the
GitHub API, downloaded the exact named child to read `.oci.manifest.digest`
from `receipt.json`, and only then dispatched the reviewed event with
string-valued evidence:

```json
{
  "event_type": "publish-first-homebrew-child",
  "client_payload": {
    "dry_run_run_id": "<successful-run-id>",
    "dry_run_run_attempt": "<attempt>",
    "dry_run_child_artifact_digest": "sha256:<actions-archive-digest>",
    "expected_child_manifest_digest": "sha256:<oci-manifest-digest>"
  }
}
```

## Deferred prefix-campaign implementation (historical)

The workflows described below are preserved under
`deferred/campaign-trust-implementation/workflows/`; they are not executable
GitHub Actions workflows and do not govern the flat-selection lane.

### Prefix-campaign activation

A campaign has three explicit authority states:

- `inert` contains no live execution, campaign, generation, or source
  commit;
- `armed` pins the final Kandelo executor and caller workflow bytes while
  its campaign, generation, and source identities remain zero; and
- `active` fills those three data identities without changing any
  workflow.

Both `inert` and `armed` reject dispatch with exit status 78. The armed
state exists because GitHub does not let a workflow's `GITHUB_TOKEN`
create a release that targets a historical commit whose
`.github/workflows` tree differs from the default branch. The campaign
must therefore be derived from a protected armed commit that already
contains its final workflow tree. Its later activation may change
authority data, tests, and rollout records, but must not change
`.github/workflows` until every Formula handoff is sealed. Controller
tests compare the active checkout's workflow tree with the sealed source
tree, and task admission repeats that comparison before doing bottle
work.

### Protected campaign-release publication

`.github/workflows/publish-prefix-campaign-release.yml` is the only
supported writer for the campaign descriptor itself. It is available
only while the authority is `armed`. The manual dispatch accepts the
exact protected tap `main` commit that owns the run and the SHA-256 of
an independently derived `campaign.json`. Neither input selects source
code, a Git checkout, or release bytes.

The read-only admission job requires `refs/heads/main`, equality with
the live protected-main ref, the complete armed authority shape, and the
committed successor scope and task graph. It then exports the Kandelo
tool commit from that authority. A separate read-only job creates
independent clean checkouts for the current source, predecessor recovery
archive, historical bottle tap, and reviewed native Homebrew. It derives
and rechecks the campaign in the Kandelo dev shell before it uploads
inert content-addressed bytes. Publication stops unless the bytes equal
the supplied digest. The C12 selected task graph must be exactly 40 C11
predecessor handoffs plus a fresh Ruby build. The narrow write job
downloads and validates only that handoff. It checks out the exact Kandelo
helper and protected source again. It then invokes the unchanged immutable-release
helper inside the real Actions run. Both content ancestry and exact
current execution authority are required. A final step invokes Kandelo's
`fetch-campaign-release` executor without credentials. This proves the
public release is non-draft, non-prerelease, and immutable. It targets
the exact source and contains only the exact `campaign.json`. It also
resolves the direct tag without credentials.

The historical C7 campaign-release caller started from active C6 tap commit
`1d7d63673d70c7204fef83f9284f4367b30a8b8a` and pins Kandelo commit
`c157026d1234c9a28dc630d02f963828525897a7`. The combined arm rotates the live
package-generation consumers to public rootfs generation
`package-generation-rootfs-wasm32-abi-v42-sha256-f44d50ad73b5bdd6c6f396b47806babff3b3fdc6869ee9f1d2f88f9460581fb4`;
the armed campaign authority kept its release, generation, and source
identities zero until the published successor was activated. Its successor
scope is sealed as
`227830740f1c179e6194b32d7383d358b321763d1bbb7ff2ec029a549a47c315`,
which in turn pins terminal C6 archive
`3b1e288aadb23fa85db549cfc874aabc035756a18bace01b606ed0d1c54b9f07`.
The canonical 41-task graph and sealed target overlay are unchanged; the graph
remains
`40a651d2ebe3a3aaab4bf9b65d91cf34db9908cb764a518437ac850747c4b139`.
All 40 direct C6 handoffs, including Git, remained reuse inputs. Ruby alone
rebuilt after the executor correction sealed and admitted LLVM's exact prefix
runtime root `etc/clang`; no broader `etc` tree is admitted.
The workflow's digest admission makes any later scope or archive change fail
closed. The campaign remained non-dispatchable while its authority was armed;
exact protected Kandelo `main`, a publicly verified generation, and the
subsequently published content-addressed campaign activated it at protected
commit `454e5d54456c8d870496bacc0ba9c2759c863ab1`.

The historical C8 campaign-release caller started from active C7 tap commit
`454e5d54456c8d870496bacc0ba9c2759c863ab1`, which activates campaign
`homebrew-prefix-campaign-sha256-8edea42ae932691b45c8695d5d6ab93a4a7ce1e08ee492ce3d7ead51fa45a185`,
and pins Kandelo commit
`75885de70c80448f08600b31a9466608e369713c`. The combined arm rotates the live
package-generation consumers to public rootfs generation
`package-generation-rootfs-wasm32-abi-v42-sha256-697af3ea327198ae4fcfb8100662e504cf58d32de4b2045423b821c6e905a0a5`;
the armed campaign authority again keeps its release, generation, and source
identities zero until the published successor is activated. Its successor
scope is sealed as
`dce71abbeb512b74adb3469a1388ccbdcbbfda28c124fe46f6773d96b8e59841`,
which pins terminal C7 archive
`76c26c5af78a97bdcb840884451ca007ab95a37645b7db7804008646b2ca4150`.
The canonical 41-task graph and sealed target overlay remain unchanged. All 40
direct C7 handoffs remain reuse inputs; Ruby alone rebuilds after run
`31017507098` proved that authenticated LLVM 22.1.8 expands to
2,624,809,107 regular-file bytes. The corrected executor gives each
authenticated native tool keg and its exact target-Cellar proxy a 4 GiB
aggregate bound. It retains the 1 GiB per-file bound and the existing 2 GiB
bounds for true target dependencies, recipe source, and recipe output.

The historical C9 arm started from active C8 tap commit
`9bbdbd334e4f45bf780e4d139cda1dc865a21419`, whose terminal campaign is
`homebrew-prefix-campaign-sha256-a516aa5e61f4b7513c18c3e5b279a6a1f2d8b07e6a7348706238bc261a63ada4`.
That campaign publicly verified 40 of 41 wasm32 tasks. Ruby run
`31043674986` failed before recipe execution and produced no handoff because
the later generic target-Cellar seal rejected LLVM 22.1.8's launcher-registered
`etc/clang` bridge into the separately sealed native prefix.

The terminal C8 archive is
`Kandelo/campaigns/prefix-v1/aborted-campaigns/a516aa5e61f4b7513c18c3e5b279a6a1f2d8b07e6a7348706238bc261a63ada4.json`,
sealed as
`7d8a7a9d1ac4df5c5dda459990384a5fe296511217053edf2a8d13c16703a483`.
The C9 successor scope is
`Kandelo/campaigns/prefix-v1/successor/a516-successor-scope.json`, sealed as
`a721afcecf9cde3185dcb6d5791a80e35ae99169bdd1a82666d63775ac32e187`.
It preserves all 40 exact C8 handoffs and schedules only Ruby for a fresh
build; the canonical 41-task graph and target overlay remain unchanged.

The C9 executor correction composes the two existing seals instead of
weakening either one. Only a proxy created by the launcher's registered native
bridge transaction may retain a link into its separately sealed native
closure; the exact immutable proxy shape and its component-aware link audit
are revalidated before that one keg is excluded from the ordinary
target-Cellar containment rule. Unregistered, redirected, writable, or
otherwise changed bridges still fail closed.

The C9 arm was finalized against the live protected Kandelo authority and its
independently, anonymously verified rootfs generation:

- `45a45fed06ff053ee4dd2cc2bb6564a99d5ce106`; and
- `package-generation-rootfs-wasm32-abi-v42-sha256-e3701277b519832435260e183b83ca7e1e82b12f84de6c24605db03552719e40`.

Those identities remained current through C11. They are historical authority
for those campaigns and must not be rewritten during C12. A PR head,
synthetic merge, predecessor executor, mutable package tag, or merely expected
future identity is not a substitute for current authority.

The C9 campaign was activated at protected tap commit
`47c232b5332ff2acad25c301ef6ba5f3f1e883b1` with campaign tag
`homebrew-prefix-campaign-sha256-f3f4cb4cda613c5cb6bbc73ec1a6952d3454971bfa92a31c9a10f9526b7308c3`.
It publicly preserved all 40 predecessor handoffs. Ruby/wasm32 run
`31062254998` passed campaign admission and planning, materialized the sealed
tap source, installed the required C9 Libyaml and Zlib handoffs, and entered
the separated unprivileged tap-recipe runner. It then failed before compiling
Ruby because the staged `Kandelo/recipes/ruby/build.sh` still required
`HOMEBREW_KANDELO_ROOT`, a publisher checkout variable that is intentionally
absent from the isolated recipe environment. The run produced no Ruby bottle
or immutable handoff.

The C10 successor archive is
`Kandelo/campaigns/prefix-v1/aborted-campaigns/f3f4cb4cda613c5cb6bbc73ec1a6952d3454971bfa92a31c9a10f9526b7308c3.json`,
sealed as `a451e756879e38dea3834ee873d445fbfff8777ecd6812a9876c0129dd65dce8`. Its successor scope is
`Kandelo/campaigns/prefix-v1/successor/f3f4-successor-scope.json`, sealed as
`4cfbb756def4280f4a9b74d330ba1f4c34298308da88dd0f1b0730764a7ec8b1`. It selects the same exact 40 public C9
handoffs and rebuilds only Ruby against the unchanged canonical 41-task graph.

That tap-only correction stayed inside the sealed target source. The
Ruby recipe was staged explicitly, consumed the authenticated
`WASM_POSIX_LOCAL_ROOT_SPILL` and `WASM_POSIX_FORK_INSTRUMENT` inputs instead
of reaching through a publisher checkout root, and copied the authenticated
source into its writable work directory before applying patches. Its sealed
identities are manifest SHA-256 `48f2f519beba22237d857b7b6860d5eccb57d5cb8abad2d7733f10b424fb34bf`, source
tree Git OID `7917903175fb2f75714ec2bc6fa0ab603efb6975`, and target tree Git OID
`af6215547bcd9fb2703e5f358721f7283b97eaee`. C10 was armed at
`c4039570825e9a0bd5932f84f933056368ccdf0a` and activated at
`5fec71d3e3de0f0fc8a0b543bee0c4afbe4bb810` with campaign
`homebrew-prefix-campaign-sha256-ac950955718d406fa3ee31a7396c22c13ede154f948673f28171ca49592c2f34`.

C10 publicly verified the same 40 predecessor tasks. Ruby/wasm32 run
`31069244063` installed the declared authenticated `gpatch` dependency and
entered the isolated recipe runner, but stopped before patching or compiling.
On Linux, Homebrew's `gpatch` Formula exposes `bin/patch`; the recipe required
the macOS-prefixed name `gpatch`. The run published no Ruby bottle or handoff.

The C10 terminal archive for C11 is
`Kandelo/campaigns/prefix-v1/aborted-campaigns/ac950955718d406fa3ee31a7396c22c13ede154f948673f28171ca49592c2f34.json`,
sealed as `f861ae7e8b4f2669ec1851a943c1ac6ad92c780e20e2e38fac5785cd84109b15`.
Its successor scope is
`Kandelo/campaigns/prefix-v1/successor/ac95-successor-scope.json`, sealed as
`a5073d0351dd3d802b87bb0ff48052dc741c12e547e0184963549846cf81aba5`.
It preserves all 40 exact public C10 handoffs and rebuilds only Ruby against
the unchanged canonical 41-task graph.

The C11 correction binds the exact declared Linux-keg executables for patch,
make, Perl, and Python into the isolated recipe environment and invokes only
those paths. It removes the unused Formula-level Rust build dependency; the
authenticated local-root-spill transform remains a separately sealed campaign
input. The corrected target is sealed by manifest SHA-256
`3359e8d45d6c04de2d3cac146c225a3bc54beb176b4018d082b337c7a49c298e`, source
tree Git OID `17bcb5910fd3d403d861b695f9ee945f1ce14d30`, and target tree Git OID
`f235ec029446883f067db5ea5d7e179710167dc6`. C11 used exact Kandelo executor
`45a45fed06ff053ee4dd2cc2bb6564a99d5ce106` and
`package-generation-rootfs-wasm32-abi-v42-sha256-e3701277b519832435260e183b83ca7e1e82b12f84de6c24605db03552719e40`.
It was armed at `be405601ca9cbc8cff9aa3ce023e0490040cd035` and activated at
`f4daa689d89b2de2a4359bf358854a7db130ca97` with campaign
`homebrew-prefix-campaign-sha256-b0476cd05b16a835bd42292bcd34bffdada50f6d06bb1129bc106a9f86763896`.

C11 publicly verified 40 of the 41 selected tasks. Ruby/wasm32 run
`31075257926` passed campaign admission and planning, then failed in the signed
native API contract before native dependency installation, Formula recipe
execution, bottle publication, or handoff publication. The signed Homebrew
API selected a newer `python@3.13` than the checked-in compatibility lock.

The C11 terminal archive for C12 is
`Kandelo/campaigns/prefix-v1/aborted-campaigns/b0476cd05b16a835bd42292bcd34bffdada50f6d06bb1129bc106a9f86763896.json`,
sealed as `0c31f4b6a4eb24f1bc193a1b807d9352e81a76a3995453020c5bd16847573f32`.
Its successor scope is
`Kandelo/campaigns/prefix-v1/successor/b047-successor-scope.json`, sealed as
`84a43358c03dd6700b2edf6c337f7d22523af69207a07eb9babc99452c7a0d88`.
It preserves the 40 exact public C11 handoffs and selects only Ruby for a
fresh build against the unchanged canonical graph and target source.

C12 advances executable trust to exact protected Kandelo commit
`af80a443a6b4820e3b04845a64ab5cb8854638cd`, whose reviewed native Formula
records include the selected `python@3.13`, and to independently admitted
rootfs generation
`package-generation-rootfs-wasm32-abi-v42-sha256-7ed33d5d51b7362c2ac04c0aca812a49c859bde25a2930d0e876f1c1e1aafcc9`.
The tap target source does not change: it remains sealed by manifest SHA-256
`3359e8d45d6c04de2d3cac146c225a3bc54beb176b4018d082b337c7a49c298e`,
source tree Git OID `17bcb5910fd3d403d861b695f9ee945f1ce14d30`, and target tree Git OID
`f235ec029446883f067db5ea5d7e179710167dc6`.

C12 was activated at protected tap commit
`54e115487584710196de5db770ef92a9be600bec` with campaign
`homebrew-prefix-campaign-sha256-f8268d7b236b0957a9e084654e807e335502fe2e4a7541e2505b45e862e3e9f7`.
It publicly verified all 40 reused tasks. Ruby/wasm32 run `31092671659`
passed admission, planning, signed native dependency installation, and
Libyaml/Zlib handoff installation, then entered the isolated recipe runner.
It failed before configure or compilation because archive-mode copying
preserved the authenticated sysroot projection's sealed modes on Ruby's
private sysroot, preventing the recipe uid from adding `include/yaml.h`. It
published no Ruby bottle or handoff.

The C12 terminal archive is
`Kandelo/campaigns/prefix-v1/aborted-campaigns/f8268d7b236b0957a9e084654e807e335502fe2e4a7541e2505b45e862e3e9f7.json`,
sealed as `d602d7173445d5cc2f8702d8bf6ddc489106b63831f092d735370ddd2405ed8f`.
Its successor scope is
`Kandelo/campaigns/prefix-v1/successor/f826-successor-scope.json`, sealed as
`0708397e38c6ca4a3414c1b7a4d136269b0b0e529c523296460ba616c9d1ecc7`.
It preserves the 40 exact public C12 handoffs and selects only Ruby for a
fresh build against the unchanged canonical graph.

The tap-only correction keeps the authenticated sysroot projection sealed,
copies its exact bytes without root ownership, and restores owner writes only
on the isolated recipe-owned copy. The corrected target is sealed by manifest
SHA-256 `1de80fb5172240d9368f9053eb621befed35183217e91649617b01227b505f0b`,
source tree Git OID `f9ec87e3b50beea1c71cede57abe160e639fb5d8`, and target tree Git OID
`7d22236c4234fe91100d19f5bf72214e5f191c8a`. Kandelo executor
`af80a443a6b4820e3b04845a64ab5cb8854638cd` and rootfs generation
`package-generation-rootfs-wasm32-abi-v42-sha256-7ed33d5d51b7362c2ac04c0aca812a49c859bde25a2930d0e876f1c1e1aafcc9`
remain unchanged because the correction changes no Kandelo or rootfs input.

After independently deriving the candidate from the newly merged armed
commit `T_ARM` and recording its digest as `C`, dispatch only that exact
tuple:

```sh
gh workflow run publish-prefix-campaign-release.yml \
  --repo kandelo-dev/homebrew-tap-core \
  --ref main \
  -f expected_caller_sha="$T_ARM" \
  -f expected_campaign_sha256="$C"
```

Keep the resulting run ID, immutable-release receipt, and
evidence-artifact digest. Keep the direct-tag proof and the anonymous
release readback receipt with the activation record. Do not supply
another workflow's run ID to a local publisher. Do not use a personal
token to imitate an Actions lock owner.
Activation changes the authority to `active`, which permanently closes
this descriptor-publication entry point for that campaign.

### Prefix-campaign package bootstrap

A non-active prefix-campaign caller has a separate route for a reviewed new
Formula whose package repository does not exist yet. The campaign manifest
must use schema 2 and the Formula's `destination.admission` must use schema 1
with kind `first-package-namespace-bootstrap-required`. That kind is valid only
for an exact `reviewed-new-entrant` build. An anonymous authentication
challenge alone is not enough: it can also mean that a private package already
exists.

After activation, that route has four ordered phases:

1. build and test the bottle without registry credentials or writes;
2. let the first-child reusable require authenticated absence of the whole
   package repository, publish one content-addressed child with the repository
   `GITHUB_TOKEN`, and read the exact digest back anonymously;
3. run the ordinary campaign publisher, including its version-index
   publication and public verification; and
4. derive and publish the ordinary schema-2 per-variant handoff.

The first-child phase cannot publish a version index, Formula edit, sidecar, or
campaign handoff. Its receipt therefore is not enough to finalize a Formula.
The following ordinary publisher owns those operations and supplies the
normal handoff consumed by the campaign controller. Ordinary Formulae retain
the `anonymous-absence` route and its exact missing-manifest requirement.

The campaign-specific first-child phase is resumable without permitting a
second bootstrap write. If the exact content-derived child is already public,
it validates the anonymous digest and continues without credentials. If that
exact child is not public, the writer must still prove authenticated absence
of the whole package repository immediately before its sole upload. A
different public child, an existing private package, an authentication
challenge after authenticated inspection, or an ambiguous transport failure
fails closed. This read-only resumption rule does not apply to the older
one-off `libyaml` canary above, which continues to reject every replay.

The dry run and both writers stay in one caller run. Kandelo's reusable bottle
publisher must give the campaign bootstrap dry-run artifacts a distinct fixed
name so the later ordinary publisher can emit its normal artifacts without an
Actions artifact-name collision. Kandelo must also admit `dry-run: true` only
for this exact protected campaign caller and bootstrap admission. The pinned
`reusable-homebrew-prefix-first-child-publish.yml` must validate the same
campaign, Formula, architecture, source commits, dependencies, and dry-run
artifacts before it receives package-write authority. Until those Kandelo-side
contracts and their trust tests are present at the authority's immutable
workflow commit, the campaign authority must remain non-active.

### Prefix-campaign bottle reuse

Reusing bottle bytes avoids a rebuild, but it is not permission to skip normal
Homebrew publication. A campaign reuse handoff is safe to consume only after
the selected archive also exists as a public OCI child in the current tap
namespace and the Formula's public version index names that exact child.

The reuse route performs these operations in order:

1. Kandelo's frozen campaign executor downloads and validates the historical
   bottle and derives the immutable Formula handoff.
2. The same executor composes a normal OCI child from those validated bytes and
   the sealed target Formula source. It does not rebuild the software or change
   the bottle layer.
3. The tap publishes that content-addressed child, or resumes only when the
   exact digest is already public, and validates an anonymous readback receipt.
4. While holding the ordinary per-Formula GHCR lock, the tap anonymously reads
   the existing version index, merges the new child without dropping a wasm32
   or wasm64 sibling, publishes the index, and validates its anonymous readback
   receipt.
5. Only then may the tap publish the immutable Formula handoff release.

The child stands on its own once step 3 succeeds. If index publication or
handoff sealing later fails, a retry revalidates and reuses the already-public
child rather than rebuilding or hiding it. The mutable index is serialized
only to prevent two successful architectures from overwriting one another;
campaign-wide completion is not required for either bottle to remain useful.

Package-write credentials are exercised only by the child and index transport
steps. The controller's token is forwarded only to its internal release reads,
and the final handoff step uses separate GitHub release authority. Child
validation, public-index import and composition, and both publication-receipt
validations run without package credentials. The workflow retains the child,
index, and anonymous-readback receipts as bounded run evidence.

## Post-Publication Acceptance

Treat a write run as accepted only after the centralized
[public-package acceptance and namespace-retirement procedure](https://github.com/Automattic/kandelo/blob/main/docs/homebrew-publishing.md#public-package-creation-and-legacy-namespace-retirement)
confirms all of the following:

- the reviewed trusted workflow completed successfully;
- the GHCR package record is public and linked to
  `kandelo-dev/homebrew-tap-core`;
- credential-free manifest and blob readback verifies the exact published
  digest, SHA-256, and byte count;
- the live Formula bottle block and generated `Kandelo/` sidecars agree and
  pass semantic validation.

The authoritative contract owns the exact commands and the separate legacy
package cleanup gates so publisher or namespace changes have one place to
update. Formula presence, a local validator result, or a successful build alone
is not post-publication acceptance.

## VFS Planning

Host VFS tooling plans a Homebrew-prefix image with
`planHomebrewVfs(metadata, options)` from the host package. The planner is
shared by Node and browser callers. It consumes parsed `Kandelo/metadata.json`
and a caller-provided link-manifest loader, resolves requested packages plus
their dependency closure in dependency-first order, and rejects bad ABI,
unsupported arch, tap-identity drift, duplicate roots or metadata, cache-key
drift, missing packages, dependency cycles, unsafe paths, and link-manifest
bottle URL/sha/byte/cache-key drift before any bottle bytes are extracted.

For `failed`, `pending`, or `building` bottle entries, the planner uses the
complete last-green fallback fields when available. Without a complete fallback,
the package is not plannable for a VFS image.

## VFS Image Building

Build a precomposed Homebrew-prefix image from generated sidecars and verified
bottle bytes with:

```bash
cd /path/to/kandelo
scripts/dev-shell.sh npx tsx images/vfs/scripts/build-homebrew-vfs-image.ts \
  --metadata /path/to/homebrew-tap-core/Kandelo/metadata.json \
  --tap-root /path/to/homebrew-tap-core \
  --package what \
  --arch wasm32 \
  --runtime node \
  --out target/homebrew-what.vfs.zst \
  --report target/homebrew-what.vfs-report.json
```

The builder consumes only `metadata.json`, link manifests, and bottle tarballs.
It does not evaluate Formula Ruby. It verifies the selected bottle byte count
and sha256, rejects unsafe or unsupported tar entries, stages files under the
declared keg, validates receipts, applies the link manifest under the declared
prefix, writes `/etc/kandelo/homebrew-vfs.json`, saves a `.vfs.zst`, and emits a
JSON report beside the image.

Link and receipt paths starting with `Cellar/` are interpreted relative to the
Homebrew prefix. Other link and receipt paths are interpreted relative to the
staged keg. Bottle payload entries under `bottle.payload_root` map to the keg;
fixture entries that are already `Cellar/...` map to the prefix. This keeps the
checked-in example shape and generated sidecar fixture shape unambiguous.

The report records whether each package used a current `success` bottle or a
last-green `fallback`. A successful report is build evidence for the precomposed
image only; Node and browser runtime support still require their own smoke
tests before publishing gallery or user-facing claims.

`provenance_json.sha256` is a normalized self-hash: compute the sha256 of the
pretty-printed provenance document after replacing
`/metadata/provenance_json/sha256` with 64 zeroes. The generator and validator
both use that convention so provenance can name and hash itself without an
impossible recursive digest.
