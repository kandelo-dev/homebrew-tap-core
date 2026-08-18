# Batched Formula Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote each ready dependency level with one inventory plan and one
atomic tap metadata commit, without rebuilding bottle candidates.

**Architecture:** Keep per-Formula candidate, verification, canonical, and
admission validation. Treat missing dependency metadata as local deferral, then
compose the ready members' four-path patches into one batch whose shared top
index is generated once and committed by one compare-and-swap writer.

**Tech Stack:** Python 3.13 dataclasses and `unittest`, GitHub Actions matrices,
Git contents commits, OCI/GHCR immutable records.

## Global Constraints

- Do not rebuild or rewrite candidate bottle layers.
- Keep exact ABI, Formula contract, bottle digest/bytes, current verification,
  actual producer custody, history barrier, and anonymous readback checks.
- A missing dependency defers only its dependants.
- One batch may contain at most `MAX_PROMOTION_WAVE` members.
- Duplicate or conflicting output paths fail before a Git mutation.

---

### Task 1: Defer unavailable dependency metadata

**Files:**
- Modify: `scripts/abi_staging/cli.py`
- Test: `scripts/abi_staging/tests/test_promotion.py`

**Interfaces:**
- Consumes: `_current_dependency_layers(tap_root, formula_plan)`.
- Produces: `None` for a Formula whose direct dependency has no selected
  canonical bottle; exact layer mapping otherwise.

- [ ] **Step 1: Write the failing regression**

Add a planner test with one ready root and one dependant whose dependency
sidecar is not yet successful. Assert planning retains the root and omits the
dependant instead of raising `promotion dependency has no exact selected
bottle`.

- [ ] **Step 2: Verify RED**

Run:
`python3 -m unittest scripts.abi_staging.tests.test_promotion -v`

Expected: the new test errors with the current global dependency failure.

- [ ] **Step 3: Implement local deferral**

Return `None` for absent dependency selection and make
`_collect_active_promotion_inputs` skip only that Formula before calling
`evaluate_promotion`. Keep malformed sidecars and conflicting selections as
errors.

- [ ] **Step 4: Verify GREEN**

Run the Task 1 command and require zero failures.

- [ ] **Step 5: Commit**

Commit as `[ABI] Defer promotion until dependencies are admitted`.

### Task 2: Compose one exact metadata batch

**Files:**
- Modify: `scripts/abi_staging/tap_metadata.py`
- Modify: `scripts/abi_staging/promotion.py`
- Test: `scripts/abi_staging/tests/test_tap_metadata.py`
- Test: `scripts/abi_staging/tests/test_promotion.py`

**Interfaces:**
- Produces: `FormulaMetadataBatchV1`, an ordered tuple of exact
  `FormulaMetadataUpdateV1` members plus one `TapMetadataPatchV1` union.
- Produces: `plan_formula_metadata_batch(tap_root, members)` which validates
  each member, rejects path conflicts, and composes `Kandelo/metadata.json`
  once.

- [ ] **Step 1: Write atomic composition regressions**

Test two independent Formula updates from one base. Assert the batch changes
both Formulae, both sidecars, both link manifests, and one top index. Add
mutations for duplicate Formula/architecture, conflicting output path, moved
base, changed member layer, and oversized membership.

- [ ] **Step 2: Verify RED**

Run the focused tap-metadata and promotion test modules. Expected: missing
batch interfaces.

- [ ] **Step 3: Implement the batch protocol**

Validate every member using the existing per-Formula rules. Build each unique
Formula/sidecar/link output, update all corresponding top-index rows in memory,
then emit one sorted path union. Simulate the full projection and call
`check_tap_metadata` before returning.

- [ ] **Step 4: Verify GREEN**

Run the focused modules and require zero failures.

- [ ] **Step 5: Commit**

Commit as `[ABI] Compose Formula metadata by dependency level`.

### Task 3: Publish and consume a batch readback

**Files:**
- Modify: `scripts/abi_staging/reconcile.py`
- Modify: `scripts/abi_staging/cli.py`
- Modify: `.github/workflows/abi-staging-reconcile.yml`
- Modify: `scripts/check_abi_staging_workflows.rb`
- Test: `scripts/abi_staging/tests/test_reconcile.py`
- Test: `scripts/abi_staging/tests/test_workflow_publication.py`
- Test: `scripts/test_check_abi_staging_workflows.rb`

**Interfaces:**
- Planner output: one metadata batch matrix item with ordered member work IDs.
- Writer output: one immutable metadata readback mapping every member to the
  same landed commit/tree and its exact update digest.
- Admission input: the member entry selected by Formula work ID.

- [ ] **Step 1: Write planner, writer, and workflow RED tests**

Assert all ready roots enter one batch; dependants remain blocked; the writer
requires all canonical handoffs; one push occurs; admissions accept only their
exact member; workflow permissions and artifact identities remain bounded.

- [ ] **Step 2: Verify RED**

Run focused reconciliation/publication/Ruby workflow tests. Expected: current
planner emits one metadata owner and no batch handoff exists.

- [ ] **Step 3: Implement the protected batch writer**

Load all member details from the exact plan artifact, anonymously re-read every
canonical manifest, validate the history barrier once, compose/apply one batch
patch, and upload one bounded readback. Update admission publication to select
and validate its member from that readback.

- [ ] **Step 4: Verify GREEN**

Run the focused Python and Ruby commands plus `actionlint` on the workflow.

- [ ] **Step 5: Commit**

Commit as `[ABI] Publish one metadata batch per dependency level`.

### Task 4: Stop invalidating products on metadata-only tap movement

**Files:**
- Modify: `scripts/abi_staging/product.py`
- Test: `scripts/abi_staging/tests/test_product.py`

**Interfaces:**
- Keeps candidate custody bound to the candidate's actual producer.
- Removes only the equality between that producer tap source and current tap
  main after the Formula contract remains exact.

- [ ] **Step 1: Write the failing product-resolution regression**

Move only `tap_plan.tap_source` while retaining the exact candidate record,
producer custody, Formula contract, layer, ABI, and current receipt. Assert
resolution succeeds. Mutate producer custody and assert rejection remains.

- [ ] **Step 2: Verify RED**

Run `python3 -m unittest scripts.abi_staging.tests.test_product -v` and require
the metadata-only movement case to fail with stale source custody.

- [ ] **Step 3: Remove the redundant current-tap equality**

Keep custody-to-producer and current Formula-contract checks unchanged.

- [ ] **Step 4: Verify GREEN**

Run the focused product module and require zero failures.

- [ ] **Step 5: Commit and hosted verification**

Commit as `[Pages] Preserve exact candidates across tap metadata changes`, run
the focused promotion/product/workflow suites, then dispatch the exact ABI 43
request and require zero bottle builds plus advancing canonical/admission work.
