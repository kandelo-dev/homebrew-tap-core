# Fast ABI Staging Coordination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parallelize immutable Formula inventory reconstruction while preserving exact scheduling outputs and reusable candidate bottles.

**Architecture:** Preserve the candidate/attempt, verification, and reuse phase ordering, but execute independent Formula reads within each phase through a bounded executor and isolated transport factory. Deterministically merge and sort results after each phase.

**Tech Stack:** Python 3.13, `concurrent.futures`, `unittest`, GHCR OCI APIs.

## Global Constraints

- Do not change Formula contracts, candidate schemas, bottle bytes, or promotion predicates.
- Keep all public inventory validation fail-closed.
- Use at most eight concurrent Formula readers.
- Preserve deterministic output independent of worker completion order.

---

### Task 1: Bounded concurrent inventory scan

**Files:**
- Modify: `scripts/abi_staging/inventory.py`
- Test: `scripts/abi_staging/tests/test_inventory.py`

**Interfaces:**
- Consumes: the existing `scan_scheduling_inventory(...)` arguments.
- Produces: the same `PublicSchedulingInventoryV1` value, plus an optional internal transport-factory seam for tests and production worker isolation.

- [ ] **Step 1: Write the failing overlap and deterministic-order tests**

Add a transport fixture that blocks the first Formula repository until a
second Formula repository begins. Assert the scan completes, both entered
before release, and repeated reversed completion orders produce equal
inventories.

- [ ] **Step 2: Run the focused tests and verify RED**

Run from the Kandelo checkout:

```bash
scripts/dev-shell.sh env \
  KANDELO_TAP_ROOT=/private/tmp/tap-abi43-fast-coordination-20260816 \
  KANDELO_ROOT="$PWD" \
  PYTHONPATH=/private/tmp/tap-abi43-fast-coordination-20260816 \
  python3 -m unittest \
  scripts.abi_staging.tests.test_inventory.PublicInventoryTests.test_formula_inventory_reads_overlap \
  scripts.abi_staging.tests.test_inventory.PublicInventoryTests.test_parallel_inventory_is_deterministic -v
```

Expected: FAIL because Formula scans are serial and the worker factory seam is absent.

- [ ] **Step 3: Implement the minimal bounded executor**

Create internal phase helpers using `ThreadPoolExecutor(max_workers=min(8,
len(items)))`. Give each worker its own production transport from a supplied
factory, preserve phase barriers, and reuse the existing validators and merge
checks.

- [ ] **Step 4: Run focused and full inventory tests**

Run the two focused tests, then:

```bash
scripts/dev-shell.sh env \
  KANDELO_TAP_ROOT=/private/tmp/tap-abi43-fast-coordination-20260816 \
  KANDELO_ROOT="$PWD" \
  PYTHONPATH=/private/tmp/tap-abi43-fast-coordination-20260816 \
  python3 -m unittest scripts.abi_staging.tests.test_inventory -v
```

Expected: PASS.

- [ ] **Step 5: Run the full tap Python suite**

```bash
scripts/dev-shell.sh env \
  KANDELO_TAP_ROOT=/private/tmp/tap-abi43-fast-coordination-20260816 \
  KANDELO_ROOT="$PWD" \
  PYTHONPATH=/private/tmp/tap-abi43-fast-coordination-20260816 \
  python3 -m unittest discover \
  -s /private/tmp/tap-abi43-fast-coordination-20260816/scripts/abi_staging/tests -v
```

Expected: PASS.

- [ ] **Step 6: Commit the tap optimization**

```bash
git add scripts/abi_staging/inventory.py \
  scripts/abi_staging/tests/test_inventory.py \
  docs/superpowers/specs/2026-08-16-fast-abi-staging-coordination-design.md \
  docs/superpowers/plans/2026-08-16-fast-abi-staging-coordination.md
git commit -m "[ABI] Parallelize immutable staging inventory"
```

### Task 2: Hosted timing and compatibility proof

**Files:**
- No production files.

**Interfaces:**
- Consumes: the immutable ABI 43 request and prior coordination artifact.
- Produces: measured workflow duration and a Formula-contract equality result.

- [ ] **Step 1: Push a PR and run the protected workflow tests**

Push the branch, open a tap PR, and let the repository's required checks run.

- [ ] **Step 2: Compare contract maps**

Generate coordination from the same request and compare each Formula identity,
architecture, and `bottle_contract_sha256` with run `31924747223`.

- [ ] **Step 3: Merge and measure one reconciliation wave**

After checks and contract equality pass, merge the tap PR, dispatch the same
immutable request, and record `Prepare exact protected coordination` wall time.
Do not cancel or rebuild any successful candidate solely because of this tap
revision.
