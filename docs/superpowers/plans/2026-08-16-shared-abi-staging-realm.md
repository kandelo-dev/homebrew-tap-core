# Shared ABI Staging Realm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and transport one exact Kandelo Formula-test closure per reconciliation run.

**Architecture:** A new protected preparation script builds both architecture prerequisites and packs a bounded run-scoped source archive with a canonical manifest. The reconcile workflow uploads it once; candidate and verification workflows download, validate, and restore it before recreating only runner-local Homebrew state.

**Tech Stack:** Bash, GitHub Actions reusable workflows, Ruby workflow contract tests, SHA-256, GNU tar/zstd.

## Global Constraints

- Do not change Formula contracts, candidate bottle bytes, promotion records, or request identity.
- The shared artifact contains no credentials, Homebrew download cache, or candidate bottle bytes.
- No consuming job may fall back to rebuilding the prepared source closure.
- Candidate and verification jobs validate artifact identity before Formula code executes.
- Preserve exact Kandelo commit/tree and tap commit binding.

---

### Task 1: Prepared-source archive contract

**Files:**
- Create: `scripts/abi-staging-pack-homebrew-realm.sh`
- Create: `scripts/abi-staging-restore-homebrew-realm.sh`
- Create: `scripts/test-abi-staging-homebrew-realm.sh`

**Interfaces:**
- Consumes: exact clean Kandelo checkout, expected commit/tree, output archive path.
- Produces: one zstd tar archive rooted at `kandelo-source/` with `realm-manifest.json`.
- Restore consumes the archive, expected archive SHA, commit, and tree and creates one absent destination.

- [x] **Step 1: Write the failing shell fixture**

Create a miniature Git checkout with representative executable, VFS, and
dependency-directory outputs. Invoke the absent pack/restore commands twice
and assert exact identity; add mutations for digest, manifest, traversal, and
source tree.

- [x] **Step 2: Run the fixture and verify RED**

Run from the Kandelo checkout:

```bash
scripts/dev-shell.sh env \
  KANDELO_TAP_ROOT=/private/tmp/tap-shared-homebrew-realm.Oixc3Y \
  bash /private/tmp/tap-shared-homebrew-realm.Oixc3Y/scripts/test-abi-staging-homebrew-realm.sh
```

Expected: FAIL because both production scripts are absent.

- [x] **Step 3: Implement the minimal pack/restore scripts**

Require regular bounded inputs, canonical lowercase SHA-256, exact Git
commit/tree, one normalized archive root, no absolute or parent paths, and an
absent restore destination. Write the manifest before packing and revalidate it
after extraction.

- [x] **Step 4: Run the fixture and verify GREEN**

Run the exact command from Step 2. Expected: `ABI staging shared Homebrew realm: PASS`.

### Task 2: One producer and exact consumer wiring

**Files:**
- Modify: `.github/workflows/abi-staging-reconcile.yml`
- Modify: `.github/workflows/abi-staging-candidate.yml`
- Modify: `.github/workflows/abi-staging-verification.yml`
- Modify: `scripts/check_abi_staging_workflows.rb`
- Modify: `scripts/test_check_abi_staging_workflows.rb`

**Interfaces:**
- Reconcile produces `realm-artifact-id`, `realm-artifact-digest`,
  `realm-archive-sha256`, and `realm-source-tree`.
- Candidate and verification reusable workflows require and restore those exact values.

- [x] **Step 1: Add failing workflow contract tests**

Require one `prepare-homebrew-realm` job, exact `needs` edges, exact artifact
inputs, a single heavyweight-build command occurrence in the complete
workflow graph, and no matrix fallback build.

- [x] **Step 2: Run the focused workflow suite and verify RED**

```bash
scripts/dev-shell.sh env \
  KANDELO_TAP_ROOT=/private/tmp/tap-shared-homebrew-realm.Oixc3Y \
  KANDELO_ROOT="$PWD" \
  ruby /private/tmp/tap-shared-homebrew-realm.Oixc3Y/scripts/test_check_abi_staging_workflows.rb
```

Expected: FAIL because the producer, edges, and inputs are absent.

- [x] **Step 3: Implement producer and restore wiring**

Move the heavyweight source-build block into the preparation job, upload one
archive with compression disabled, pass all three exact identities to both
reusable workflows, restore before per-runner Homebrew setup, and remove the
heavyweight block from matrix jobs.

- [x] **Step 4: Run focused tests and actionlint**

Run the workflow suite and shell fixture commands above, then:

```bash
scripts/dev-shell.sh actionlint \
  /private/tmp/tap-shared-homebrew-realm.Oixc3Y/.github/workflows/*.yml
```

Expected: all three commands exit zero.

### Task 3: Full gate and hosted proof

**Files:**
- No production files; mechanical generated-policy changes, if required by an
  existing checker, remain part of Task 2's workflow commit.

**Interfaces:**
- Consumes: the immutable ABI 43 request.
- Produces: one hosted run with exactly one realm producer and no repeated realm build.

- [ ] **Step 1: Run the full tap Python and Ruby workflow suites**

Run `python3 -m unittest discover -s scripts/abi_staging/tests -v` with the tap
on `PYTHONPATH`, then the complete Ruby workflow mutation suite.

- [ ] **Step 2: Verify diff and workflow syntax**

Run `git diff --check`, the exact actionlint command from Task 2, and the
existing workflow checker. If that checker names a generated freshness
command, run the named command before committing.

- [ ] **Step 3: Commit, push, and open the tap PR**

Use commit and PR title `[ABI] Share one prepared Formula test realm` with a
plain-language `## Why` section before implementation details.

- [ ] **Step 4: Merge on green evidence and dispatch ABI 43**

Confirm the hosted workflow contains one realm preparation job, that all
matrix consumers restore the same artifact ID/digest/SHA, and that required
Formula work reaches its build or verification command without rebuilding the
closure.
