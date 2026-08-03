# Prefix campaign target source

This directory stages the reviewed `/opt/kandelo/homebrew` cutover without
changing the tap's active Formulae, helper, bootstrap recipe, metadata, or
bottle selections.

`source/` mirrors the 41 files that differ between the protected base recorded
in `manifest.json` and the reviewed target tree. The manifest binds every base
preimage and target file by mode, byte length, Git blob ID, and SHA-256. The
caller authority additionally binds the canonical manifest bytes, the complete
`source/` Git tree, and the reconstructed target tree.

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
