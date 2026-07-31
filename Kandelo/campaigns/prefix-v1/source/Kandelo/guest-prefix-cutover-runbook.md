# Kandelo Guest-Prefix Bottle Cutover Runbook

Date: 2026-07-29

This runbook records the exact preparation and publication order for the
one-time move to Kandelo's guest-owned Homebrew layout. It is an operator
guide, not selected package metadata.

`Kandelo/guest-prefix-cutover-inventory.json` is the machine-checked count
contract for the dated source snapshot and final retention arithmetic.

The guest contract is:

- prefix and repository: `/opt/kandelo/homebrew`;
- Cellar: `/opt/kandelo/homebrew/Cellar`;
- stable command: `/usr/bin/brew`; and
- no compatibility user, directory, or alias for the retired guest
  layout.

Native Linux publisher paths are host-tool implementation details. They
must never enter a target Formula, target bottle, sidecar, or VFS image.

## Authority Boundaries

Four exact Git snapshots own different facts:

1. Kandelo owns the ABI, guest-layout contract, publisher, inspector, and
   campaign implementation.
2. The old selected tap owns the current bottle records and their original
   provenance.
3. The source tap owns the candidate Formula, support, and sealed recipe
   bytes.
4. Exact upstream Homebrew owns Formula version interpretation and native
   package behavior.

Do not substitute a pull-request merge ref, a mutable branch, or an
equal-tree commit for any of these authorities.

The trusted publisher alone may produce or accept:

- a GHCR child manifest, top-level version index, and bottle blob;
- anonymous exact-byte readback evidence;
- a canonical `bottle do` block;
- `Kandelo/formula/<name>.json`;
- `Kandelo/link/<identity>.json`;
- `Kandelo/reports/<identity>.provenance.json`;
- the selected `Kandelo/metadata.json` record; and
- build, verifier, reuse-admission, and finalization handoffs.

Do not hand-author a bottle digest, byte count, Cellar value, runtime
claim, rebuild, link manifest, provenance record, or aggregate package
record. The reviewed Formula and campaign manifest reserve an identity;
they do not assert that its artifact exists.

The bootstrap recipe lock is a reviewed source input. Its deterministic
inner ZIP digest and size do not predict the outer Homebrew bottle digest
or size.

## Audited Campaign Inventory

The exact 2026-07-31 inventory contains:

- 52 selected ABI-42 Formulae and 58 selected variants;
- 63 Formula sidecars and 70 sidecar variants;
- 11 additional Formula sidecars containing 12 ABI-41 variants;
- 34 ABI-42, retired-prefix-free reuse candidates;
- 36 required replacement variants; and
- one new `homebrew-bootstrap/wasm32` variant; and
- one new `libyaml/wasm32` variant.

The final catalog therefore contains 65 Formulae and 72 variants if no
Formula changes before cutover.

The source snapshot is intentionally not a partially migrated catalog.
Before the atomic final wave, the retired prefix remains in all 63 Formula
sidecars and their 70 variants, all 139 live link manifests, 12 Formula
bottle blocks, two VFS acceptance configurations, and four schema examples.
The selected metadata still contains 52 Formulae and 58 variants. Those
counts are staging-input evidence, not permission to expose a mixed catalog.

All 139 existing root-level provenance reports also name the retired prefix,
but they are immutable historical evidence of what was actually built. They
must remain byte-for-byte truthful after cutover; they are not live
installation metadata and must not be rewritten or pruned.

The 34 reuse candidates total about 17.8 MiB compressed:

```text
asa/wasm32
bc/wasm32
bzip2/wasm32
coreutils/wasm32
ctags/wasm32
dash/wasm32
ed/wasm32
fbdoom/wasm32
findutils/wasm32
gencat/wasm32
getconf/wasm32
grep/wasm32
gzip/wasm32
libcurl/wasm32
libcurl/wasm64
libcxx/wasm32
libcxx/wasm64
libzip/wasm32
lsof/wasm32
m4/wasm32
modeset/wasm32
musl-fts/wasm32
musl-fts/wasm64
ncompress/wasm32
netcat/wasm32
pcre2/wasm32
posix-utils-lite/wasm32
sed/wasm32
unzip/wasm32
xz/wasm32
zip/wasm32
zlib/wasm32
zlib/wasm64
zstd/wasm32
```

Reuse is permitted only when the campaign rederives all of the following:

- exact public bytes still match the selected digest and size;
- the old bottle is ABI 42;
- bounded inspection finds no retired guest prefix;
- historical and candidate Formula identities match outside the canonical
  bottle block;
- canonical relocation/static inspection and a bounded canonical-prefix
  pour/layout check succeed without rerunning the Formula's declared runtime
  test; and
- a reuse handoff preserves the original build time, builder, source
  commit, Formula digest, URL, digest, and byte count.

Never relabel ABI-41 bytes as ABI 42. Never rewrite `built_from` to make a
reused bottle appear newly built. The 34 exact byte-clean ABI-42 reuse tasks
do not rerun declared Formula tests. A newly built bottle runs its declared
test exactly once in the mandatory anonymous public-readback verifier.

The required replacement identities are:

| Formula | Architectures | Reserved rebuild |
|---|---|---:|
| `bash` | `wasm32` | 6 |
| `binutils` | `wasm32` | 2 |
| `curl` | `wasm32`, `wasm64` | 2 |
| `diffutils` | `wasm32` | 2 |
| `dinit` | `wasm32` | 1 |
| `erlang` | `wasm32` | 1 |
| `file-formula` | `wasm32` | 4 |
| `gawk` | `wasm32` | 2 |
| `git` | `wasm32` | 2 |
| `icu` | `wasm32` | 6 |
| `less` | `wasm32` | 5 |
| `libiconv` | `wasm32` | 2 |
| `libmagic` | `wasm32` | 3 |
| `libpng` | `wasm32` | 2 |
| `libxml2` | `wasm32` | 1 |
| `make` | `wasm32` | 2 |
| `nano` | `wasm32` | 5 |
| `ncurses` | `wasm32` | 2 |
| `nethack` | `wasm32` | 2 |
| `openssl` | `wasm32`, `wasm64` | 3 |
| `patch` | `wasm32` | 1 |
| `pax` | `wasm32` | 1 |
| `perl` | `wasm32` | 2 |
| `procps` | `wasm32` | 2 |
| `python` | `wasm32` | 1 |
| `ruby` | `wasm32` | 2 |
| `sqlite` | `wasm32`, `wasm64` | 1 |
| `tar` | `wasm32` | 2 |
| `tcl` | `wasm32` | 1 |
| `texlive` | `wasm32` | 1 |
| `vim` | `wasm32` | 2 |
| `wget` | `wasm32` | 2 |
| `what` | `wasm32` | 2 |

These values are dated audit evidence. The exact campaign manifest must
rederive them and anonymously prove every destination absent immediately
before any upload.

The new bootstrap identity is:

```text
package:   homebrew-tap-core/homebrew-bootstrap
version:   6.0.12-153-gcf5bc21
arch:      wasm32
rebuild:   0
top tag:   6.0.12-153-gcf5bc21
```

The exact top reference was anonymously absent during the 2026-07-29
audit. A failed older bootstrap attempt created an unrelated older version
in the same public package. It does not authorize or collide with this
identity. Recheck absence during the campaign.

## Earliest Bootstrap-Critical Wave

The shortest dependency path to an unselected, immutable bootstrap bottle
does not wait for unrelated packages.

Build `homebrew-bootstrap` immediately after checking the exact campaign
source. In parallel, build `libyaml`. The bootstrap is an independent
Formula-scoped task with no target runtime dependency. Libyaml is Ruby's
new tap-owned target dependency.

In parallel, produce canonical-prefix reuse handoffs for:

```text
coreutils
dash
ed
grep
libcxx
sed
unzip
zip
zlib
```

Then keep these dependency-ready builds moving:

1. after Libyaml, build `ruby`; in parallel, build `diffutils`, `ncurses`,
   and `openssl`;
2. after OpenSSL: admit `libcurl`;
3. after Ncurses: `less` and `vim` in parallel; and
4. after Diffutils, Less, Libcurl, OpenSSL, and Vim: `git`.

`homebrew-bootstrap` has no target runtime dependency. Its Git, Ruby, Unzip,
and Zip declarations are native publisher build tools, so the static target
dependency resolver correctly excludes them. Build the support-data bottle
early. The later live in-guest proof still requires target Git, Ruby, Unzip,
and Zip handoffs before it can claim a usable `brew`.

Bash is not a bootstrap-bottle build dependency. Curl, Findutils, Gawk,
Tar, and `posix-utils-lite` are needed for the later in-guest Homebrew
lifecycle, not for producing the support-data bottle.

## Complete Dependency-Ready Queue

`homebrew-bootstrap` and `libyaml` are already ready at campaign start.
Neither is a final wave after Git.

After all reuse handoffs exist, the remaining replacement graph is:

1. `binutils`, `diffutils`, `dinit`, `erlang`, `gawk`, `icu`,
   `libiconv`, `libmagic`, `libpng`, `libyaml`, `make`, `ncurses`,
   `openssl`, `patch`, `pax`, `perl`, `procps`, `python`, `sqlite`, `tar`,
   `tcl`, and `what`;
2. `bash`, `curl`, `file-formula`, `less`, `libxml2`, `nano`,
   `nethack`, `ruby`, `texlive`, `vim`, and `wget`;
3. `git`.

This is a readiness graph, not four global barriers. Keep no more than
eight Formula tasks active, refill a slot immediately, and prioritize
Ncurses, OpenSSL, Libmagic, Libiconv, and Libpng. Keep every Formula's
selected architectures in one task. `libcxx`, `zlib`, `openssl`, and
`libcurl` therefore include both siblings. The bootstrap path uses their
`wasm32` bottles. Tex Live has no downstream consumer and must not delay
the bootstrap critical path.

The exact 22-Formula in-guest runtime-support closure is:

```text
zlib
libyaml
ruby
coreutils
dash
ed
diffutils
grep
libcxx
ncurses
less
openssl
libcurl
sed
vim
git
curl
findutils
gawk
gzip
tar
posix-utils-lite
```

In addition to the bootstrap-critical reuse set, admit `findutils`,
`gzip`, and `posix-utils-lite`; build `gawk`, `tar`, and `curl`. That
completes the runtime support needed for the first live guest proof.

## Trusted Execution Sequence

### 1. Admit the native publisher

Wait for the native publisher compatibility pull request to be completely
green. Merge it, then read its actual protected-main merge commit:

```bash
set -euo pipefail

M_PUBLISHER="$(
  gh pr view 1141 \
    --repo Automattic/kandelo \
    --json mergeCommit \
    --jq '.mergeCommit.oid'
)"
test -n "$M_PUBLISHER"
test "$M_PUBLISHER" = "$(
  gh api repos/Automattic/kandelo/commits/main --jq .sha
)"
export M_PUBLISHER
```

Create and merge a tap-only trust rotation that pins all three reusable
workflow callers and the tap trust root to `M_PUBLISHER`. Do not use the PR
head. If write callers also advance their exact Kandelo consumer, promote
and validate the matching exact-main rootfs generation before using those
callers. A generation validated against an older main commit is not
bottle-production authority even when its selected closure still compares
equal. A dry run has no package-generation input.

Read the resulting tap-main commit as `T_PUBLISHER`, then dispatch the real
trusted build and verifier realms:

```bash
set -euo pipefail

T_PUBLISHER="$(
  gh api repos/Kandelo-dev/homebrew-tap-core/commits/main --jq .sha
)"

jq -n \
  --arg kandelo "$M_PUBLISHER" \
  --arg tap "$T_PUBLISHER" \
  '{
    event_type: "dry-run-kandelo-bottles",
    client_payload: {
      kandelo_repository: "Automattic/kandelo",
      kandelo_ref: $kandelo,
      tap_repository: "kandelo-dev/homebrew-tap-core",
      tap_name: "kandelo-dev/tap-core",
      tap_ref: $tap,
      formulae: "bzip2",
      arches: "wasm32"
    }
  }' |
  gh api --method POST \
    repos/Kandelo-dev/homebrew-tap-core/dispatches \
    --input -
```

Do not start the prefix campaign until this dry run proves the isolated
builder and verifier realms.

### 2. Freeze exact campaign authority

Land the guest-layout and campaign implementation in Kandelo. Keep the
tap's source-only prefix work off selected tap main until the campaign can
finalize the complete catalog atomically.

Record exact values:

```bash
M_CUTOVER='<exact protected Kandelo main SHA>'
T_OLD='<exact old selected tap SHA>'
T_SOURCE='<exact reviewed source tap SHA>'
BREW_SHA='cf5bc21c6b127e168ef7cfa982ba7db62874690e'

METADATA_SHA256="$(
  shasum -a 256 "$OLD_TAP/Kandelo/metadata.json" |
    awk '{print $1}'
)"
LAYOUT_SHA256="$(
  shasum -a 256 \
    "$KANDELO/homebrew/kandelo-guest-layout.json" |
    awk '{print $1}'
)"
```

Every checkout must be clean and have the exact recorded `HEAD`. The
manifest output must be outside all four input worktrees.

The campaign command is:

```bash
bash scripts/dev-shell.sh \
  python3 scripts/homebrew-prefix-campaign.py derive \
    --kandelo-root "$KANDELO" \
    --kandelo-commit "$M_CUTOVER" \
    --old-tap-root "$OLD_TAP" \
    --old-tap-commit "$T_OLD" \
    --source-tap-root "$SOURCE_TAP" \
    --source-tap-commit "$T_SOURCE" \
    --native-brew-root "$NATIVE_BREW" \
    --native-brew-commit "$BREW_SHA" \
    --metadata-sha256 "$METADATA_SHA256" \
    --guest-layout-sha256 "$LAYOUT_SHA256" \
    --jobs 8 \
    --out "$CAMPAIGN_MANIFEST"
```

Run `check` with the same inputs and `--manifest`. It must repeat public
readback and destination-absence checks.

Committed source inspection uses repository-declared Node 24 TypeScript
stripping and a digest-bound tool closure. The exact archived input
intentionally contains no `node_modules`, and the campaign never invokes
ambient `npx` or downloads an execution tool.

### 3. Produce immutable handoffs without selecting them

For every reuse or build task:

1. derive the task only from the checked campaign manifest;
2. verify source, dependency, architecture, and reserved destination;
3. build or anonymously read the exact bytes;
4. run inspection, canonical relocation/pour-layout admission, and readback;
5. emit a canonical immutable handoff;
6. re-read and verify that handoff in a separate identity; and
7. apply only its generated bottle block and sidecars to a local publisher
   overlay.

New-build tasks additionally run the Formula's declared runtime test exactly
once in the mandatory anonymous public-readback verifier; the build lane
defers that test rather than duplicating it. Reuse tasks do not rerun the
declared Formula test. Their admission is the exact-byte, ABI,
static-inspection, Formula-identity, provenance, anonymous-readback, and
bounded pour/layout proof above.

The overlay is not tap main and is never pushed. It is a deterministic clean
Git commit whose sole parent is the reviewed source commit and whose diff is
limited to exact sealed dependency-overlay files plus its tracked ledger.
The normal bottle-build implementation can therefore clone the exact
dependency bytes inside a trusted campaign job. The ledger retains the
reviewed source-tap commit separately from the synthetic overlay commit;
modified overlay bytes are never claimed to be the reviewed source commit.
Downstream tasks resolve only verified same-campaign dependency handoffs from
it. A missing dependency stops the task rather than falling back to old
selected metadata.

The ordinary repository-dispatch write workflow accepts only protected
tap-main history; it must not be tricked into accepting this detached
synthetic commit. The campaign runner therefore needs a reviewed same-job or
immutable-handoff transport that reconstructs the commit, verifies its ledger,
and then invokes the unchanged bottle builder. Do not push intermediate
overlays merely to satisfy the ordinary source gate.

Actions cache is not handoff authority. A cross-run handoff must live in a
content-addressed immutable release or registry object and be retrieved by
its recorded digest and byte count.

Use the bootstrap-critical order above while filling unused slots with
other ready work.

### 4. Build the bootstrap

Build the bootstrap support-data bottle as soon as the campaign source is
checked. Do not confuse its native publisher build tools with target runtime
dependencies. Delay only the live in-guest lifecycle claim until exact Git,
Ruby, Unzip, and Zip handoffs exist in the candidate overlay.

Require the support-data bottle test to reproduce:

```text
homebrew-bootstrap.zip
  SHA-256 26ac98e328573244d3e7c0c149f30114ef5d9c8882200f5a22e56f97d2541482
  bytes   5251369

homebrew-brew.env
  SHA-256 2eb3f05703b6a6f23feabda24f622bacd068115c7f74a0eac51bb4085e9eec5a
  bytes   210
```

Those are inner recipe outputs. Accept the outer bottle identity only from
the trusted publisher's Homebrew bottle JSON, OCI layout, anonymous
readback, and verifier handoff.

### 5. Finalize once

After all 65 Formula-scoped handoffs covering 72 variants are available:

1. compose every handoff into one fresh local publisher-tap candidate;
2. select exactly 65 Formula sidecars containing 72 variants and retain
   exactly 72 selected live link manifests;
3. preserve all 139 existing root-level provenance reports byte-for-byte,
   add the 38 new build/bootstrap reports, and therefore retain 177 root
   provenance reports when the audited inventory has not changed;
4. preserve historical failure and rollback namespaces;
5. remove every unreferenced live formula variant and link sidecar;
6. regenerate the 12 retired-prefix Formula bottle blocks, add blocks for
   Ruby, Libyaml, and the bootstrap, and regenerate two VFS acceptance
   configurations and four schema examples;
7. run the complete tap validator once;
8. run the final retired-prefix guard over live metadata while explicitly
   preserving truthful historical provenance;
9. acquire one tap state lock;
10. recheck protected Kandelo and tap refs;
11. commit and push one tap update; and
12. discard and rebase the candidate if tap main advanced.

Do not three-way merge a partially composed catalog.

### 6. Rotate products

From the exact final tap commit:

1. regenerate shell migration, runtime-support, artifact, and mirror locks;
2. rebuild the mostly-lazy shell and every shell-derived image;
3. publish and anonymously verify immutable bottle mirrors and VFS assets;
4. prove exact Node.js and Chromium shell acceptance;
5. prove install, execute, reinstall or upgrade, uninstall, and reboot
   persistence for the first-party tap;
6. repeat add, install, and execute for the independent third-party tap;
7. require all three Homebrew path queries to report the Kandelo contract;
8. prove the retired guest tree remains absent; and
9. rotate product indexes only after all public-URL evidence is green.

## Tooling Gate

Do not use the ordinary per-Formula finalizer for this migration. It would
expose a mixed selected catalog.

Before live work, the campaign implementation must provide:

- canonical reuse and build handoff schemas and independent verification;
- a no-push, Git-backed publisher-overlay composer;
- a trusted campaign publisher adapter that verifies, rather than weakens,
  the ordinary workflow's protected-main source boundary;
- immutable cross-task handoff storage and a complete campaign ledger;
- sparse dependency-ready scheduling with an eight-task bound;
- reserved-rebuild publication without preselecting the new block;
- exact directory-closure validation and final sidecar pruning;
- one atomic finalizer invocation; and
- direct repository-declared Node 24 readback without ambient `npx`.

If truthful reuse takes longer than rebuilding the clean set, rebuild all
34 reuse candidates. That is an allowed throughput choice, not permission
to omit the inert overlay or atomic finalization.
