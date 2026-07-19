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

metadata.json                                      # generated tap state
formula/<name>.json                               # generated tap state
link/<name>-<version>-rebuild<N>-<arch>.json      # generated tap state
reports/<name>-<version>-rebuild<N>-<arch>.provenance.json
```

The `examples/` directory contains fixture data for schema and semantic
validator development. It is not published metadata.

`vfs-acceptance.json` and its referenced Brewfile are reviewed tap policy, not
generated sidecars. The publisher reads them from the exact tap commit and
never rewrites them.

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
- browser-compatible entries have browser validation evidence;
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
  --package hello \
  --arch wasm32 \
  --runtime node \
  --out target/homebrew-hello.vfs.zst \
  --report target/homebrew-hello.vfs-report.json
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
