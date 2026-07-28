# Main-shell bottle-layer mirror publication

This runbook activates the tap-owned public bottle-layer mirror only after the
exact bottle-backed shell has passed its closed Node.js and Chromium proof in
Automattic/kandelo. The immutable tap release contains only the bottle-layer
mirror. The shell image and Homebrew bootstrap remain digest-locked
Automattic/kandelo package artifacts; this publication neither copies them into
the tap release nor makes their source release immutable.

The caller binds four distinct identities:

- `Mpre`: the exact live Kandelo `main` commit containing the sealed shell;
- `TF`: the earlier tap commit that owns every reviewed bottle in that shell;
- `C`: the exact live independent-canary `main` commit; and
- `TA`: the later live tap `main` commit containing the finalized caller.

`TA` is deliberately not an input. The reusable workflow derives it from
`${{ github.sha }}` after the caller is merged. This avoids a circular
Kandelo-to-tap-to-Kandelo commit dependency.

## Prepared state

The local caller template contains three visible placeholders:

- `__FINAL_KANDELO_MPRE_SHA__`;
- `__FINAL_TAP_CATALOG_SHA__`; and
- `__FINAL_CANARY_SHA__`.

Do not push or merge the placeholder state. It cannot resolve the reusable
workflow and the candidate trust parser rejects it. Finalize it only after
Kandelo PR #1116 and the complete Formula catalog are merged.

## Preconditions

1. Keep Kandelo `main` fixed from finalization through the complete public
   proof. If it advances, select the new live commit and revalidate the sealed
   shell before continuing.
2. Require the exact #1116 candidate to have passed its closed-transport
   shell proof in Node.js and Chromium, including Doom and Modeset.
3. Require `TF` to match both catalog locks in Kandelo and to be an ancestor
   of the tap branch used for this caller.
4. Require `C` to match the runtime-support lock and the independent canary's
   live public `main`.
5. Start from a clean branch at the tap's latest live `main`. No bottle
   publication or other tap mutation may run concurrently.
6. Keep GitHub Release immutability enabled for this repository so the
   published bottle-layer mirror cannot be changed after creation.

Read the live identities; do not predict merge SHAs:

```bash
set -euo pipefail

MPRE="$(git ls-remote https://github.com/Automattic/kandelo.git \
  refs/heads/main | awk '{print $1}')"
TF="<exact final bottle-catalog commit>"
C="$(git ls-remote \
  https://github.com/brandonpayton/homebrew-kandelo-canary.git \
  refs/heads/main | awk '{print $1}')"

for value in "$MPRE" "$TF" "$C"; do
  [[ "$value" =~ ^[0-9a-f]{40}$ ]]
done
```

From the exact Kandelo `Mpre` checkout, require checked-in agreement:

```bash
test "$(jq -er '.catalog.tap_commit' \
  homebrew/main-shell-migration-lock.json)" = "$TF"
test "$(jq -er '.catalog.tap_commit' \
  homebrew/main-shell-homebrew-runtime-support.json)" = "$TF"
test "$(jq -er '.lifecycle_installs[0].revision' \
  homebrew/main-shell-homebrew-runtime-support.json)" = "$C"
jq -e \
  '.state == "sealed" and .image.sha256 != null and .image.bytes > 0' \
  homebrew/main-shell-lazy-artifact-lock.json >/dev/null
```

Before the caller advances tap `main` from `TF` to `TA`, launch these two
independent Mpre gates:

```bash
gh workflow run force-rebuild.yml \
  --repo Automattic/kandelo \
  --ref main \
  -f packages=shell \
  -f arches=wasm32 \
  -f ref="$MPRE" \
  -f skip_tests=false

gh workflow run homebrew-main-shell-ci.yml \
  --repo Automattic/kandelo \
  --ref main \
  -f transport_mode=closed \
  -f kandelo_main_revision="$MPRE" \
  -f core_tap_final_revision="$TF" \
  -f canary_tap_revision="$C"
```

Wait for both runs to succeed. The first creates the canonical revision-22
shell archive from exact live `Mpre`; the mirror publisher later resolves that
public archive and must not consume synthetic merge provenance. The second is
the live closed first- and third-party lifecycle proof in both Node.js and
Chromium. It explicitly requires the tap's live `main` to equal `TF`, so it
cannot be postponed until after the caller merge creates `TA`.

From the clean tap branch, require the reviewed catalog to precede the
candidate:

```bash
git merge-base --is-ancestor "$TF" HEAD
```

## Finalize and validate the caller

Preview first; the first command writes nothing:

```bash
python3 -B scripts/finalize-main-shell-mirror-caller.py \
  --kandelo-sha "$MPRE" \
  --tap-catalog-sha "$TF" \
  --canary-sha "$C"

python3 -B scripts/finalize-main-shell-mirror-caller.py \
  --kandelo-sha "$MPRE" \
  --tap-catalog-sha "$TF" \
  --canary-sha "$C" \
  --apply
```

The helper accepts only placeholders or the requested final values. It updates
the trust constants first and makes the caller dispatchable last, is
idempotent, and converges after an interrupted partial write. Unknown
predecessors are rejected.

Validate the exact candidate:

```bash
set -euo pipefail

python3 -B scripts/test_finalize_main_shell_mirror_caller.py
ruby Kandelo/test-workflow-trust.rb
bash Kandelo/test-workflow-trust.sh
actionlint .github/workflows/*.yml
python3 -B - <<'PY'
import ast
import pathlib

for source in (
    "scripts/finalize-main-shell-mirror-caller.py",
    "scripts/test_finalize_main_shell_mirror_caller.py",
):
    ast.parse(pathlib.Path(source).read_bytes(), filename=source)
PY
git diff --check
```

Review that the caller is data-only:

- only `repository_dispatch` with type
  `publish-homebrew-main-shell-mirror`;
- exactly one reusable-workflow job;
- permissions exactly `actions: read` and `contents: write`;
- the same literal `Mpre` in `uses:` and `kandelo-ref`;
- literal `TF` and `C`;
- no secrets, package permission, local steps, environment, or event-selected
  input; and
- no input for `TA`.

## Merge expectation

This PR intentionally adds a new trust-root writer workflow. The
candidate-owned `Tap contract checks` job must pass. The existing
base-controlled `pull_request_target` job will reject the legitimate
workflow/trust-root byte change by design; waiting cannot make it green.
After an exact manual diff review, use the established explicitly authorized
merge path for this trust-root change.

Normal squash or rebase merge is safe here. The actual live-main merge
commit becomes `TA`; no bottle or shell lock predicts it.

The repository currently relies on exact-live-main workflow checks and review
practice rather than an enforced GitHub branch-protection ruleset. That is
sufficient to fail closed against stale dispatch inputs, but it does not stop a
repository writer from changing live caller authority.

Before treating “protected caller” as a GitHub-enforced production guarantee,
create this repository branch ruleset:

- name: `Protect live tap main`;
- enforcement: `Active`;
- target: `Default branch`;
- bypass list:
  - `GitHub Actions`, **Always allow**. Its GitHub App ID is `15368`;
  - `Organization administrators`, **For pull requests only**, so the
    intentionally red base-owned check on a reviewed trust-root rotation has
    an audited merge path;
- rules:
  - restrict deletions;
  - require linear history;
  - require a pull request, allowing only rebase merge, zero mandatory
    approvals, and requiring conversation resolution;
  - require status checks `publisher-trust` and `publisher-trust-base`, both
    from GitHub Actions integration `15368`, and require the branch to be up
    to date before merging; and
  - block force pushes.

Do not require signed commits: the current tap publisher creates ordinary
automation commits. Do not restrict all updates: that would duplicate the pull
request rule and make the intended automation bypass harder to audit.

Without the `GitHub Actions` **Always allow** entry, the pull-request and
required-check rules block the bottle finalizer's direct fast-forward push to
`main`. With it, existing jobs that explicitly request `contents: write`
continue to work because `GITHUB_TOKEN` is an installation token for the
GitHub Actions app. The repository-wide default token permission should remain
`read`.

This bypass is broader than one workflow: any repository workflow granted
`contents: write` acts as the same GitHub Actions app. The closed-world
workflow trust checks and read-only default reduce that exposure, but the
longer-term stronger design is a dedicated publisher app or a publisher branch
plus reviewed PR, at which point remove the broad Actions bypass.

## Publish and prove public transport

After the merge, re-read all live heads and fail if any authority moved:

```bash
set -euo pipefail

TA="$(git ls-remote \
  https://github.com/Kandelo-dev/homebrew-tap-core.git \
  refs/heads/main | awk '{print $1}')"
LIVE_MPRE="$(git ls-remote https://github.com/Automattic/kandelo.git \
  refs/heads/main | awk '{print $1}')"
LIVE_C="$(git ls-remote \
  https://github.com/brandonpayton/homebrew-kandelo-canary.git \
  refs/heads/main | awk '{print $1}')"

test "$LIVE_MPRE" = "$MPRE"
test "$LIVE_C" = "$C"
git fetch origin "$TA"
git merge-base --is-ancestor "$TF" "$TA"
```

Dispatch without a payload so event data cannot select any authority:

```bash
gh api --method POST \
  repos/Kandelo-dev/homebrew-tap-core/dispatches \
  -f event_type=publish-homebrew-main-shell-mirror
```

Find and watch the resulting `Publish Homebrew main-shell mirror` run. Its
single reusable workflow must complete all three phases:

1. prepare a bounded same-run handoff from public package and bottle sources,
   without publication credentials;
2. publish and anonymously re-read the immutable bottle-layer mirror with the
   tap's scoped `GITHUB_TOKEN`; and
3. resolve the public generation again, prove the full first- and third-party
   lifecycle in Node.js, and prove the public shell, bottle transport, and
   reviewed `brew` activation boundary in Chromium.

The public Chromium phase does not repeat the complete guest-install lifecycle.
That lifecycle is proved with closed transport in Chromium and with public
transport in Node.js.

The publication receipt must report a successful public anonymous readback of
the bottle-layer assets, an immutable bottle-layer release, and
`target_commitish == TA`. Do not describe the public cutover as complete until
the `public-proof` job is green.

If `Mpre`, `TA`, or `C` moves during the run, the workflow fails closed. If an
identical bottle collection was already published by an earlier `TA`, do not
redispatch this publisher: it intentionally refuses to relabel that release as
one created by a newer authority. Retain the proven immutable bottle-layer
mirror for the product cutover, or add a separately reviewed consume-only proof
path if a new authority must re-prove it without writing.
