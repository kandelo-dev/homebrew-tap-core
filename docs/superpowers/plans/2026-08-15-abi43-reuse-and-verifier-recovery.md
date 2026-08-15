# ABI 43 Reuse And Verifier Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resume ABI 43 staging with the 42 existing candidate bottles and no candidate rebuilds.

**Architecture:** Reuse records share each Formula's already-public candidate package but use a dedicated immutable tag prefix. Kandelo's verifier places the prepared Playwright cache in its private Homebrew realm without changing selected Formula source.

**Tech Stack:** Python 3 ABI-staging controller, Bash verifier adapters, GHCR OCI Distribution API, Homebrew, Playwright.

## Global Constraints

- Do not rebuild candidate bottles.
- Do not weaken Formula archive identity comparison.
- Do not introduce a credential into verification.
- Preserve exact digest collision checks and anonymous OCI readback.
- Run validation through the repository dev shell.

---

### Task 1: Publish reuse records in existing public candidate packages

**Files:**
- Modify: `scripts/abi_staging/oci.py`
- Modify: `scripts/abi_staging/policy.py`
- Modify: `scripts/abi_staging/reuse.py`
- Modify: `scripts/abi_staging/inventory.py`
- Modify: `scripts/abi_staging/promotion.py`
- Test: `scripts/abi_staging/tests/test_oci.py`
- Test: `scripts/abi_staging/tests/test_reuse.py`
- Test: `scripts/abi_staging/tests/test_inventory.py`
- Test: `scripts/abi_staging/tests/test_promotion.py`

**Interfaces:**
- Produces: `REUSE_TAG_PREFIX = "reuse-sha256-"` and reuse locators whose
  repository equals the bound candidate repository.
- Consumes: existing `publish_immutable_oci_plan`, canonical reuse record
  validation, and public candidate repository identity.

- [x] **Step 1: Write failing repository and tag-filter tests**

  Assert that reuse publication targets `candidate_repository(...)`, emits a
  `reuse-sha256-*` tag, candidate inventory ignores that valid separate class,
  and reuse inventory selects it. Assert that an unknown tag still fails.

- [x] **Step 2: Run the focused tests and verify RED**

  Run:

  ```bash
  cd /private/tmp/kandelo-abi43-main-merge-20260813
  scripts/dev-shell.sh bash -lc '
    cd /private/tmp/tap-protected-verifier.oToGmF
    env KANDELO_TAP_ROOT=$PWD \
      KANDELO_ROOT=/private/tmp/kandelo-abi43-main-merge-20260813 \
      PYTHONDONTWRITEBYTECODE=1 \
      python3 -m unittest \
        scripts.abi_staging.tests.test_oci \
        scripts.abi_staging.tests.test_reuse \
        scripts.abi_staging.tests.test_inventory \
        scripts.abi_staging.tests.test_promotion -v
  '
  ```

  Expected: failures show the `/reuse` repository and unsupported reuse tag.

- [x] **Step 3: Implement the minimal shared-package layout**

  Add the closed reuse tag grammar to `list_public_record_locators`, publish
  the reuse plan with `tag_prefix="reuse-sha256-"`, select that prefix during
  reuse inventory, and require promotion reuse locators to use the candidate
  repository itself.

- [x] **Step 4: Run focused tests and verify GREEN**

  Run the command from Step 2. Expected: all selected tests pass.

- [x] **Step 5: Run workflow and policy regressions**

  ```bash
  cd /private/tmp/kandelo-abi43-main-merge-20260813
  scripts/dev-shell.sh bash -lc '
    cd /private/tmp/tap-protected-verifier.oToGmF
    env KANDELO_TAP_ROOT=$PWD \
      KANDELO_ROOT=/private/tmp/kandelo-abi43-main-merge-20260813 \
      PYTHONDWRITEBYTECODE=1 \
      python3 -m unittest \
        scripts.abi_staging.tests.test_workflow_publication \
        scripts.abi_staging.tests.test_scheduler \
        scripts.abi_staging.tests.test_coordination -v
  '
  scripts/dev-shell.sh bash -lc '
    cd /private/tmp/tap-protected-verifier.oToGmF
    env KANDELO_ROOT=/private/tmp/kandelo-abi43-main-merge-20260813 \
      ruby scripts/test_check_abi_staging_workflows.rb
  '
  ```

  Expected: all tests pass.

- [x] **Step 6: Commit the tap fix**

  ```bash
  git add scripts/abi_staging docs/superpowers
  git commit -m "[ABI] Publish reuse records with public candidates"
  ```

### Task 2: Keep Playwright cache transport outside Formula identity

**Files in `Automattic/kandelo`:**
- Modify: `scripts/abi-staging-verify-bottle.sh`
- Modify: `scripts/homebrew-verify-poured-bottle.sh`
- Test: `scripts/test-abi-staging-verify-bottle.sh`
- Test: `scripts/test-homebrew-publish-workflow.sh`

**Files in `Kandelo-dev/homebrew-tap-core`:**
- Modify: `scripts/abi_staging/execution.py`
- Test: `scripts/abi_staging/tests/test_verification_execution.py`

**Interfaces:**
- Produces: explicit `--playwright-browsers-path <absolute-directory>` verifier
input and a private verifier-home cache projection consumed by ordinary
Playwright discovery.
- Consumes: the workflow-prepared `PLAYWRIGHT_BROWSERS_PATH` and the exact
  Kandelo verifier checkout.

- [x] **Step 1: Write failing tap executor tests**

  Require the executor to leave composed Formula bytes unchanged and pass the
  prepared browser directory as an explicit adapter argument. Reject a missing
  prepared directory before adapter invocation.

- [x] **Step 2: Verify the tap tests fail for the current overlay**

  ```bash
  scripts/dev-shell.sh env KANDELO_ROOT=/private/tmp/kandelo-abi43-main-merge-20260813 \
    python3 -m unittest \
      scripts.abi_staging.tests.test_verification_execution -v
  ```

  Expected: the Formula contains `KandeloVerificationPlaywrightOverlay` and no
  explicit browser-cache argument exists.

- [x] **Step 3: Write failing Kandelo verifier fixtures**

  Extend the shell fixtures so the outer verifier rejects an unsafe cache path,
  passes a valid path to the normal verifier, and the normal verifier exposes
  it at Playwright's default `ms-playwright` directory in its private native
  home without modifying Formula source.

- [x] **Step 4: Verify the Kandelo fixtures fail**

  ```bash
  scripts/dev-shell.sh bash scripts/test-abi-staging-verify-bottle.sh
  scripts/dev-shell.sh env \
    KANDELO_HOMEBREW_PUBLISH_TEST_FOCUS=verification-playwright-cache \
    bash scripts/test-homebrew-publish-workflow.sh
  ```

  Expected: the new argument is rejected or the private cache projection is
  absent.

- [x] **Step 5: Implement the explicit cache bridge**

  Remove `_install_verification_playwright_overlay`. Validate the prepared
cache as an absolute real directory in the tap executor and pass it to the
Kandelo adapter. Thread it through the exact outer verifier and place it at
the Playwright default cache location owned by the private target verifier
home. The native-dependency launcher has a separate home and must not receive
the target Formula's browser cache.

- [x] **Step 6: Run focused GREEN validation**

  Run the commands from Steps 2 and 4. Expected: all pass and composed Formula
  bytes remain unchanged.

- [x] **Step 7: Commit both repository changes separately**

  Kandelo:

  ```bash
  git add scripts/abi-staging-verify-bottle.sh \
    scripts/homebrew-verify-poured-bottle.sh \
    scripts/test-abi-staging-verify-bottle.sh \
    scripts/test-homebrew-publish-workflow.sh
  git commit -m "[Homebrew] Isolate browser cache from Formula identity"
  ```

  Tap:

  ```bash
  git add scripts/abi_staging/execution.py \
    scripts/abi_staging/tests/test_verification_execution.py
  git commit -m "[ABI] Pass the prepared verifier browser cache"
  ```

### Task 3: Land and rerun the immutable request

**Files:** None.

**Interfaces:**
- Consumes: merged tap and Kandelo verifier fixes.
- Produces: a fresh exact staging run with `build_count == 0`.

- [ ] **Step 1: Verify each branch immediately before PR creation**

  Run focused suites from Tasks 1 and 2 plus `git diff --check` in each repo.

- [ ] **Step 2: Create Why-first PRs and merge after required CI**

  Preserve the Kandelo commit author, report exact focused validation, and
  state that no candidate bottle bytes or Formula build contracts changed.

- [ ] **Step 3: Publish a new immutable request for the updated Kandelo head**

  Use the existing protected request publication workflow. Do not modify or
  reuse the prior request document under a new source identity.

- [ ] **Step 4: Dispatch and inspect coordination**

  Require `build_count == 0`, then monitor reuse and required verification
  jobs through their exact job IDs.

- [ ] **Step 5: Continue dependency waves through admission**

  Redispatch the immutable request until required verification is complete,
  merge the ABI 43 PR, and run post-merge promotion, tap metadata admission,
  Pages readiness, and deployment.

### Task 4: Recover corrected verification without rebuilding candidates

**Files in `Kandelo-dev/homebrew-tap-core`:**
- Modify: `scripts/abi_staging/scheduler.py`
- Test: `scripts/abi_staging/tests/test_scheduler.py`

**Files in `Automattic/kandelo`:**
- Modify: `scripts/homebrew-verify-poured-bottle.sh`
- Test: `scripts/test-homebrew-publish-workflow.sh`

**Interfaces:**
- Produces: request-local unsuccessful verification retry budgets while
  preserving historical successful verification reuse.
- Produces: validated `PLAYWRIGHT_BROWSERS_PATH` propagation across Homebrew's
  Formula-test `HOME` replacement.
- Consumes: immutable verification receipts, the current request digest, and
  the explicit `--playwright-browsers-path` verifier input from Task 2.

- [x] **Step 1: Write scheduler RED coverage**

  Add a test with one historical failed receipt at the maximum ordinal and an
  otherwise identical current request. Assert that scheduling emits
  `verify-candidate` at ordinal zero. Keep the existing historical-success
  reuse assertion.

- [x] **Step 2: Run the focused scheduler test and verify RED**

  ```bash
  cd /private/tmp/kandelo-abi43-main-merge-20260813
  scripts/dev-shell.sh bash -lc '
    cd /private/tmp/tap-protected-verifier.oToGmF
    env KANDELO_TAP_ROOT=$PWD \
      KANDELO_ROOT=/private/tmp/kandelo-abi43-main-merge-20260813 \
      PYTHONDONTWRITEBYTECODE=1 \
      python3 -m unittest \
        scripts.abi_staging.tests.test_scheduler.SchedulerTests.test_historical_failure_does_not_exhaust_current_request -v
  '
  ```

  Expected: no ready verification exists because the historical failure is
  incorrectly treated as the current request's terminal attempt.

- [x] **Step 3: Filter retry failures to the current request**

  Keep the historical-success lookup over every matching receipt. Before
  selecting failure ordinals, restrict unsuccessful receipts to
  `item.request_sha256 == request_sha256`. With no current-request failure,
  schedule ordinal zero.

- [x] **Step 4: Run scheduler GREEN and regression suites**

  Run the command from Step 2, then the complete scheduler, coordination, and
  workflow-publication Python modules. Expected: all pass.

- [x] **Step 5: Write browser-cache RED coverage**

  Extend the normal verifier fixture so the fake Homebrew test changes `HOME`
  before asserting that `PLAYWRIGHT_BROWSERS_PATH` equals the canonical
  prepared browser root. Assert that caller poisoning is replaced.

- [x] **Step 6: Run the focused verifier fixture and verify RED**

  ```bash
  cd /private/tmp/kandelo-abi43-main-merge-20260813
  scripts/dev-shell.sh env \
    KANDELO_HOMEBREW_PUBLISH_TEST_FOCUS=verification-playwright-cache \
    bash scripts/test-homebrew-publish-workflow.sh
  ```

  Expected: the fake Formula test sees no canonical Playwright browser path.

- [x] **Step 7: Export the validated explicit browser path**

  After canonical input validation, export
  `PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH_INPUT"` for the normal
  verifier process. Remove the ineffective verifier-home cache symlink and its
  cleanup state.

- [x] **Step 8: Run verifier GREEN and exact outer fixture**

  Run the command from Step 6 and
  `scripts/dev-shell.sh bash scripts/test-abi-staging-verify-bottle.sh`.
  Expected: both pass and selected Formula bytes remain unchanged.

- [ ] **Step 9: Commit, land, republish, and redispatch**

  Commit the repositories separately, land both Why-first PR updates, publish
  a new immutable request for the new Kandelo head, and require the next tap
  plan to retain `build_count == 0` while retrying the four formerly stranded
  required verifications at ordinal zero.
