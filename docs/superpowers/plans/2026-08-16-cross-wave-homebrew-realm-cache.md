# Cross-wave Homebrew Realm Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse one exact packed Homebrew realm across reconciliation waves with identical Kandelo sources and realm producer inputs.

**Architecture:** The producer restores an immutable GitHub Actions cache entry keyed by runner platform, exact Kandelo commit, and the protected realm producer-script digest. Cache misses run the existing prepare/pack path and save the archive; cache hits skip it. Formula-only tap changes preserve the key. Every wave still uploads the archive through the existing run-scoped artifact interface, and downstream workflows retain their exact restore validation.

**Tech Stack:** GitHub Actions YAML, pinned `actions/cache` v6.1.0, Ruby workflow checker and Minitest mutation tests.

## Global Constraints

- Candidate and verification reusable workflow inputs remain unchanged.
- Cache lookup has one exact key and no prefix restore keys.
- Cache miss behavior retains the existing preparer and packer.
- The run-scoped artifact remains the only consumer handoff.
- All third-party actions remain pinned to full 40-character commits.
- Formula-only tap changes must not invalidate the prepared realm.
- Changes to either protected realm producer script must invalidate it.

---

### Task 1: Reuse the exact packed realm across waves

**Files:**
- Modify: `.github/workflows/abi-staging-reconcile.yml:166-247`
- Modify: `scripts/check_abi_staging_workflows.rb:8-13,352-378`
- Modify: `scripts/test_check_abi_staging_workflows.rb:342-389`

**Interfaces:**
- Consumes: protected `discover-plan` outputs `kandelo-head` and `tap-commit`.
- Produces: unchanged `artifact-id`, `artifact-digest`, `archive-sha256`, and `source-tree` outputs from `prepare-homebrew-realm`.

- [x] **Step 1: Write the failing workflow contract and mutation tests**

Require `actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9`, `actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9`, the exact archive path, an exact key containing runner OS/architecture plus both protected commits, no restore prefixes, miss guards on setup/preparation/packing/save, and unconditional identity/upload steps. Add mutations that remove the cache, weaken the key, add restore prefixes, or remove a miss guard.

- [x] **Step 2: Run the focused checker to verify RED**

Run:

```bash
scripts/dev-shell.sh env KANDELO_TAP_ROOT="$PWD" \
  ruby scripts/test_check_abi_staging_workflows.rb \
  --name /prepared_homebrew_realm/
```

Expected: failure because `prepare-homebrew-realm` has no cache restore/save steps and builds unconditionally.

- [x] **Step 3: Implement the minimal cache path**

Add exact restore/save steps around the existing archive. Guard setup, preparation, packing, and save with:

```yaml
if: steps.restore-realm-cache.outputs.cache-hit != 'true'
```

Split archive identity derivation into an unconditional step so cached archives produce the existing outputs. Use this exact key:

```yaml
key: abi-staging-homebrew-realm-${{ runner.os }}-${{ runner.arch }}-${{ needs.discover-plan.outputs.kandelo-head }}-${{ needs.discover-plan.outputs.tap-commit }}
```

- [x] **Step 4: Run focused and full workflow gates to verify GREEN**

Run:

```bash
scripts/dev-shell.sh env KANDELO_TAP_ROOT="$PWD" \
  ruby scripts/test_check_abi_staging_workflows.rb
scripts/dev-shell.sh env KANDELO_TAP_ROOT="$PWD" \
  ruby scripts/check_abi_staging_workflows.rb
scripts/dev-shell.sh actionlint .github/workflows/*.yml
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit and open the tap PR**

```bash
git add .github/workflows/abi-staging-reconcile.yml \
  scripts/check_abi_staging_workflows.rb \
  scripts/test_check_abi_staging_workflows.rb \
  docs/superpowers/specs/2026-08-16-cross-wave-homebrew-realm-cache-design.md \
  docs/superpowers/plans/2026-08-16-cross-wave-homebrew-realm-cache.md
git commit -m "[ABI] Reuse exact Homebrew realms across waves"
git push -u origin optimize/cross-wave-homebrew-realm-cache
```

---

### Task 2: Remove whole-tap overbinding from the cache key

**Files:**
- Add: `scripts/abi-staging-homebrew-realm-cache-key.sh`
- Add: `scripts/test-abi-staging-homebrew-realm-cache-key.sh`
- Modify: `.github/workflows/abi-staging-reconcile.yml`
- Modify: `scripts/check_abi_staging_workflows.rb`
- Modify: `scripts/test_check_abi_staging_workflows.rb`

- [x] **Step 1: Record RED for Formula-only reuse and producer invalidation**

- [x] **Step 2: Derive a versioned key from the exact producer-script closure**

- [x] **Step 3: Migrate only the exact compatible legacy cache**

- [x] **Step 4: Run focused and full workflow gates**

- [ ] **Step 5: Merge the tap fix before retrying ABI 43**
