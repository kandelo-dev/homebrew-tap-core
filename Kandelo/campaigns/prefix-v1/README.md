# Prefix campaign target source

This directory stages the reviewed `/opt/kandelo/homebrew` cutover without
changing the tap's active Formulae, helper, bootstrap recipe, metadata, or
bottle selections.

`source/` mirrors the 46 files that differ between the protected base
recorded in `manifest.json` and the reviewed target tree. The manifest
binds every base preimage and target file by mode, byte length, Git blob
ID, and SHA-256. The caller authority additionally binds the canonical
manifest bytes, the complete `source/` Git tree, and the reconstructed
target tree.

The staged tree is data, not a second public tap. Validation requires all live
destinations to remain at their recorded base identities. A build or finalizer
may materialize the overlay only in a separate output directory:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/prefix-campaign-source.py \
  materialize \
  --out /absolute/path/to/new-empty-directory
```

Do not copy individual staged files into the live tap. The caller authority
must remain `inert` until the reviewed Kandelo reusable publisher consumes this
exact overlay, and only the campaign finalizer may replace the active files
together with all selected Formula bottle blocks and generated catalog
metadata in one atomic tap commit.

The publication workflow handles one `(Formula, architecture)` variant
at a time. A build selected for wasm32 uses the exact rootfs package
generation bound by the caller authority. A reused bottle does not need
a package generation: the reviewed Kandelo executor checks out the
campaign's exact historical tap commit, downloads the public bottle
again, verifies every recorded byte and dependency, and produces the
same immutable handoff contract as a new build. The tap controller does
not duplicate those reuse rules.

New wasm64 builds are intentionally unavailable in this campaign. The
earlier browser-input generation includes complete browser images rather
than the smaller package build runtime, and its wasm64 closure cannot be
produced for Formulae that do not support wasm64. Admission fails before
dispatch instead of selecting that impossible input. An already-built
wasm64 bottle may still be reused because byte verification does not
execute a Formula.

Each successful handoff is independently immutable and anonymously
readable. It can be used to prepare a closed VFS selection even while
unrelated campaign variants fail. Only the optional whole-catalog final
tap commit waits for the complete selected campaign.

## Publish campaign authority

A campaign release is accepted only when its asset inventory contains
exactly one file named `campaign.json`. The file name is part of the
authority contract, in addition to the bytes, SHA-256, release tag, and
target commit.

Stage the derived manifest under that literal base name. Then describe it
as the only asset in Kandelo's schema-1 immutable-release manifest:

```sh
asset_root="$(mktemp -d)"
kandelo_root=/exact/kandelo
cp /absolute/path/to/derived-campaign.json \
  "$asset_root/campaign.json"
```

The release manifest must declare `campaign.json` with its exact byte
count and SHA-256, make it the only preferred asset, accept no existing
asset sets, target the exact source tap commit, and use the
content-addressed campaign tag. Validate that inert manifest without
credentials, then publish it only with the Kandelo commit pinned by the
tap authority:

```sh
env -u GH_TOKEN -u GITHUB_TOKEN PYTHONDONTWRITEBYTECODE=1 \
  python3 \
  "$kandelo_root/scripts/validate-immutable-github-release-manifest.py" \
  --manifest "$release_manifest" \
  --asset-root "$asset_root" \
  --stage-dir "$validation_root/assets" \
  --out-manifest "$validation_root/manifest.json"

GITHUB_REPOSITORY=kandelo-dev/homebrew-tap-core \
  bash "$kandelo_root/scripts/publish-immutable-github-release.sh" \
  --manifest "$release_manifest" \
  --asset-root "$asset_root" \
  --lock-root /exact/source/tap/checkout \
  --receipt "$publisher_receipt" \
  --exact-kandelo-main-sha "$kandelo_commit" \
  --exact-target-main-sha "$source_tap_commit"
```

Do not hand-roll this lifecycle with `gh release create`. In particular,
GitHub CLI's `path#label` syntax sets a display label; it does not rename
the uploaded asset. The pinned publisher owns tag creation, locking,
draft reconciliation, exact asset upload, protected-main rechecks,
publication, immutability checks, and anonymous readback. With release
immutability enabled, a published naming mistake cannot be corrected in
place.

Before activating the campaign, query the public release and require all
of these facts:

- `draft` and `prerelease` are both `false`;
- `immutable` is `true`;
- `target_commitish` is the campaign's exact source tap commit;
- the asset inventory is exactly `campaign.json`; and
- the asset byte count and `sha256:` digest match the derived file and
  content-addressed tag.

Then use Kandelo's `fetch-campaign-release` command without credentials.
It independently downloads the public bytes and writes the readback
receipt used by campaign operations. Do not activate an authority from a
maintainer-authenticated metadata check alone.

### Rejected immutable release from 2026-08-03

Release
`homebrew-prefix-campaign-sha256-9c8ba0ddd90f64bbbde0a182fee5154dc1ae6c74a967d5088b82a7f1dd4e5061`
is intentionally orphaned. Its bytes and digest are correct, but its sole
asset is named `campaign-7abe0a1.json` instead of `campaign.json`.
The controller rejects that inventory. No campaign authority may name
this release.

## Abandoned campaigns

An active campaign can be returned to its fail-closed `armed` state when its
frozen publisher cannot build the reviewed source. That transition clears the
campaign release, package generation, and source-tap commit together. It does
not delete public evidence or make an old handoff valid for a later campaign.

`aborted-campaigns/` retains the exact abandoned authority, dispatches, and
public handoffs. The planned bounded recovery may rebind a prior reuse handoff
without rebuilding or reinspecting its bottle only when the ABI, bottle bytes,
Formula source, guest layout, validation contract, and dependency digests are
all unchanged. That support is not active until its controller path and tests
land. Its successor receipt must name both campaigns and the immutable
predecessor handoff. A fresh build or any changed input must use the normal
successor task instead.
