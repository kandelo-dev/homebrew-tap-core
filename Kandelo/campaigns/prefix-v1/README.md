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

## One-time finalization

The finalizer prepares one normal Git commit whose parent is the exact live
`main` commit it inspected. It applies the complete reviewed target and bottle
catalog, adds `completion.json`, and removes the dispatch authority plus its
staged source inputs in that same commit:

- `.github/workflows/prefix-campaign-bottles.yml`
- `Kandelo/prefix-campaign-authority.json`
- `Kandelo/campaigns/prefix-v1/manifest.json`
- `Kandelo/campaigns/prefix-v1/source/`

The publisher may push that commit only when the remote `main` ref still equals
the tombstone's `expected_parent_commit`. This compare-and-swap rule prevents a
concurrent tap change from being silently overwritten. A normal single-parent
commit also leaves an auditable transition from active authority to permanent
retirement.

`completion.schema.json` describes the retained tombstone. Its source and
campaign-release fields copy the final active authority. The remaining digests
bind the exact guest layout and the complete, sorted Formula handoff cohort.
The finalizer's outer receipt additionally binds the tombstone bytes and the
candidate tap tree, avoiding an impossible self-reference inside the tree.

After finalization, validation reads the active authority from the tombstone
commit's parent. It does not require today's Formulae, README, or shared helper
to keep their historical cutover bytes. This is why later service Formulae may
evolve normally without reopening or resealing the retired campaign.
