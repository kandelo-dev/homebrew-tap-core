# Public GHCR Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every ABI staging GHCR publication use repository-scoped workflow authority and reject any configuration that can create a private package.

**Architecture:** Keep the existing shared OCI publisher as the runtime authority for public visibility, repository association, and anonymous digest readback. Remove PAT injection from every publication workflow and enforce the single workflow-token environment through the existing Ruby workflow checker.

**Tech Stack:** GitHub Actions YAML, Ruby/Minitest workflow contracts, Python OCI publisher integration tests, GitHub Container Registry.

## Global Constraints

- All GHCR targets remain under `kandelo-dev`; no Automattic GHCR publication is permitted.
- Every publisher retains `packages: write` only in its protected write job.
- Existing exact-digest, immutable-tag, source-association, and anonymous-readback checks remain unchanged.
- Bottle, Formula, product, and evidence bytes are not rebuilt or rewritten by this change.

---

### Task 1: Enforce repository-scoped publication authority

**Files:**
- Modify: `.github/workflows/abi-staging-candidate.yml`
- Modify: `.github/workflows/abi-staging-verification.yml`
- Modify: `.github/workflows/abi-staging-reuse.yml`
- Modify: `.github/workflows/abi-staging-reconcile.yml`
- Modify: `.github/workflows/abi-staging-abi-history.yml`
- Modify: `.github/workflows/abi-staging-maintenance.yml`
- Modify: `scripts/check_abi_staging_workflows.rb`
- Test: `scripts/test_check_abi_staging_workflows.rb`

**Interfaces:**
- Consumes: GitHub job `${{ github.token }}` and `${{ github.actor }}` with explicit `packages: write`.
- Produces: `HOMEBREW_GITHUB_PACKAGES_TOKEN` and `HOMEBREW_GITHUB_PACKAGES_USER` environments accepted by the unchanged Python OCI transport.

- [ ] **Step 1: Write the failing workflow-contract test**

Replace the PAT-positive assertions with a repository-wide assertion that every
publication environment equals the workflow actor/token pair. Add mutations
that restore the PAT or package-user variable and require rejection.

- [ ] **Step 2: Run the focused test and verify RED**

Run from the Kandelo declared dev shell:

```bash
env KANDELO_TAP_ROOT="$PWD" \
  KANDELO_ROOT=/Users/brandon/emdash/worktrees/Kandelo/emdash/homebrew-pr-staging-1q1w6 \
  ruby scripts/test_check_abi_staging_workflows.rb
```

Expected: failure because protected publishers and reusable workflow secrets
still reference the dedicated package PAT.

- [ ] **Step 3: Change the workflow environments minimally**

Set each publication environment to:

```yaml
HOMEBREW_GITHUB_PACKAGES_TOKEN: ${{ github.token }}
HOMEBREW_GITHUB_PACKAGES_USER: ${{ github.actor }}
```

Remove the reusable workflow secret declarations and reconciliation call-site
secret forwarding. Update the checker’s exact expected workflow shapes and
reject all package-secret references.

- [ ] **Step 4: Run focused and full workflow checks**

Run:

```bash
env KANDELO_TAP_ROOT="$PWD" \
  KANDELO_ROOT=/Users/brandon/emdash/worktrees/Kandelo/emdash/homebrew-pr-staging-1q1w6 \
  ruby scripts/test_check_abi_staging_workflows.rb
ruby scripts/check_abi_staging_workflows.rb
```

Expected: all Minitest cases pass and the direct checker exits zero.

- [ ] **Step 5: Run the existing shared OCI publication regression**

Run:

```bash
env KANDELO_TAP_ROOT="$PWD" \
  KANDELO_ROOT=/Users/brandon/emdash/worktrees/Kandelo/emdash/homebrew-pr-staging-1q1w6 \
  PYTHONPATH="$PWD" python3 -m unittest \
  scripts.abi_staging.tests.test_oci.OciPublicationTest.test_new_namespace_mount_upload_and_anonymous_readback \
  scripts.abi_staging.tests.test_oci.OciPublicationTest.test_existing_private_or_foreign_namespace_fails_before_any_write \
  scripts.abi_staging.tests.test_oci.OciPublicationTest.test_association_digest_size_visibility_and_readback_drift_fail
```

Expected: all shared runtime publication cases pass.

- [ ] **Step 6: Validate workflow syntax and commit**

Run the repository action linter, `git diff --check`, review the scoped diff,
and commit with a purpose-led message.

### Task 2: Prove the hosted rollout and resume ABI 43

**Files:**
- No source files.

**Interfaces:**
- Consumes: merged protected tap workflow and the existing public ABI 42 history record.
- Produces: successful exact history publication plus public canonical ABI 43 package creation.

- [ ] **Step 1: Merge the reviewed tap change**

Push the branch, open a purpose-led pull request, wait for required checks, and
merge only after the exact head is green.

- [ ] **Step 2: Rerun protected ABI history**

Dispatch `abi-staging-abi-history.yml` on protected `main`. Require the exact
history digest to remain unchanged and anonymous readback to succeed.

- [ ] **Step 3: Resume reconciliation**

Dispatch the protected reconciliation workflow. Require its first canonical
ABI 43 package to report `visibility: public`, association with the tap, and
credential-free exact digest readback.

- [ ] **Step 4: Repair background bottle failures**

Use the durable scheduling inventory to list remaining non-required Formula
subjects, group failures by root cause, and repair/retry independent groups
without rebuilding successful candidates.
