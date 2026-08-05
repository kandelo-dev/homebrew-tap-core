# Kandelo Sidecar Metadata

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

The tap currently has no checked-in VFS acceptance selection. The previous
selection described the retired `/home/linuxbrew/.linuxbrew` guest prefix and
Python 3.13.3, so retaining it would test a product Kandelo no longer ships.
The normal publisher treats the policy as optional unless its caller requests
the legacy dependency-closure acceptance rung. A future policy must describe
the current `/opt/kandelo/homebrew` layout and be reviewed and proven before it
is checked in. The mostly-lazy shell cutover instead proves its closed
selection through the shell's Node and Chromium lifecycle tests.

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

## First `libyaml` GHCR Child

`.github/workflows/repository-namespace-canary.yml` is the narrow bootstrap
caller for the absent public `homebrew-tap-core/libyaml` package. It is fixed in
reviewed YAML to Formula `libyaml`, architecture `wasm32`, this tap's exact
protected-main commit, and one immutable Kandelo workflow commit. The workflow
can run only through the separate `publish-first-homebrew-child` repository
dispatch; neither an ordinary dry run nor the prefix campaign can invoke it.

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
publication must later publish and verify the complete index, finalize
tap state, and pass the acceptance procedure below. A prefix campaign
may be `inert` or `armed`, but it is not dispatchable until it is
`active`; campaign selection and reuse continue to require anonymous
public evidence.

After the prerequisite Kandelo commit and this caller are both on their exact
protected `main` refs, first run a fresh `libyaml`/`wasm32` dry run at that tap
commit. Inspect the run and artifact through the GitHub API, download the exact
named child to read `.oci.manifest.digest` from `receipt.json`, and only then
dispatch the reviewed event with string-valued evidence:

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

## Prefix-campaign activation

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
the supplied digest. The C6 selected task graph must be exactly 39 C5
predecessor handoffs plus fresh Git and Ruby builds. The narrow write job
downloads and validates only that handoff. It checks out the exact Kandelo
helper and protected source again. It then invokes the unchanged immutable-release
helper inside the real Actions run. Both content ancestry and exact
current execution authority are required. A final step invokes Kandelo's
`fetch-campaign-release` executor without credentials. This proves the
public release is non-draft, non-prerelease, and immutable. It targets
the exact source and contains only the exact `campaign.json`. It also
resolves the direct tag without credentials.

The checked-in C6 structure is deliberately inert while
`__C5_TERMINAL_ARCHIVE_SHA256__` or `__C6_SUCCESSOR_SCOPE_SHA256__` remains.
Replace those markers only from the exact terminal C5 archive and the final
scope bytes. The workflow's digest admission then makes any later scope or
archive change fail closed.

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

## Prefix-campaign package bootstrap

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

## Prefix-campaign bottle reuse

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
