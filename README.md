# Kandelo Core Homebrew Tap

`kandelo-dev/homebrew-tap-core` is Kandelo's first-party core Homebrew tap. It
owns Formula source, Kandelo-specific Formula support, generated bottle blocks,
and publication evidence. The `Automattic/kandelo` repository owns the kernel,
host runtime, SDK, package-build infrastructure, and trusted publisher.

The tap is still experimental. Do not publish user-facing `brew tap` or
`brew install` instructions until a stock guest Homebrew install has been
validated inside Kandelo.

## Formulae

Formulae under `Formula/` use normal Homebrew metadata and build their staged
upstream source through Kandelo's worktree-local SDK. Shared cross-compilation
and runtime-test mechanics live in
`Kandelo/formula_support/kandelo_formula_support.rb`.

Formula source currently present in this repository includes:

- `zlib` and `ruby`, the first dependency and heavy-runtime Formulae;
- `python`, CPython 3.13.3 with its complete standard library and license tree;
- `erlang`, an embedded Erlang/OTP 28.2 runtime with the real `erlexec`, BEAM,
  boot tree, and fork helper path;
- `sqlite`, including the library and real command-line shell, plus the `bzip2`/`xz`
  compression tools and static libraries from the dependency-first source-build pilot;
- `zstd`, the threaded Zstandard library and command-line dependency root;
- `libmagic`, the full file-type database and compression-aware identification library;
- `openssl`, the first dependency-root library migration;
- `libpng` and `libxml2`, zlib-backed dependency-root libraries;
- `libzip`, the zlib-backed ZIP library and upstream archive comparison, merge, and inspection tools;
- `libcxx`, the LLVM C++ standard library, ABI runtime, and bundled unwinder;
- `icu`, the ICU 74.2 Unicode and globalization libraries with the complete
  common data archive;
- `musl-fts`, the BSD hierarchy traversal library for portable archive and filesystem tools;
- `libcurl`, the TLS, compression, threaded-resolver, and Unix-socket transfer library;
- `curl`, the matching command-line transfer client linked against the tap library;
- `ncurses`, the wide-character terminal library and CLI dependency root;
- `less` and its upstream `more` compatibility mode, terminal pagers linked against the tap's real ncurses termcap interface;
- `bash`, the GNU interactive shell with real pipelines, subprocesses, and process substitution;
- `sed`, the GNU stream-editing CLI used by shell and build workflows;
- `gzip`, the GNU compression CLI with native gunzip and zcat aliases;
- `grep`, GNU regular-expression and file search for the leaf CLI wave;
- `pcre2`, the Unicode-capable regex library, POSIX wrapper, and upstream CLI tools;
- `dash`, the dependency-free POSIX shell with instrumented subprocess support;
- `make`, GNU dependency-driven build automation using the tap's POSIX shell;
- `ed`, the conforming line editor and restricted editor required by patch workflows;
- `patch`, GNU's real multi-format file transformation utility replacing the compact metadata scanner;
- `asa`, FreeBSD's POSIX carriage-control translator for FORTRAN output;
- `m4`, the GNU macro processor with process-executing builtins backed by the tap's Dash shell;
- `gawk`, GNU's pattern scanning and text-processing language;
- `binutils`, GNU's native WebAssembly archive, symbol, and inspection suite,
  with exact trailing/representable `.wasm.*` custom-section and strip transforms,
  plus explicit rejection of relocatable, dynamic, cross-format, or lossy rewrites;
- `file`, compression-aware file type identification backed by the complete
  `libmagic` database;
- `what`, FreeBSD's SCCS identification-string extractor;
- `zip` and `unzip`, the security-patched Info-ZIP creation, extraction, and inspection tools.
- `libiconv`, GNU's complete character-set conversion library and CLI,
  replacing the compact base-image byte-copy fallback;
- `ncompress`, the upstream LZW `compress` and `uncompress` tools replacing the
  compact base-image fallback; GNU `gzip` owns the shared `zcat` command and
  reads both gzip and legacy compress streams.
- `pax`, the MirBSD pax, cpio, and tar interfaces for portable archive interchange.
- `gencat`, the POSIX message-catalog compiler producing catalogs consumed by
  Kandelo's musl `catopen` and `catgets` implementation.
- `procps`, the upstream `ps` process reporter backed by Kandelo's truthful
  cross-process procfs state.
- `getconf`, OpenBSD's POSIX configuration frontend over Kandelo's real
  `sysconf`, `pathconf`, and `confstr` interfaces.
- `ctags`, Universal Ctags' maintained tag generator, `readtags` query client,
  and optscript interpreter with complete C and C++ workflows.
- `nginx`, the forked HTTP and reverse proxy service with PCRE2 rewrite and
  zlib compression support.
- `tar`, the GNU archive creation and extraction CLI.
- `wget`, GNU HTTP and HTTPS retrieval linked against the tap TLS and compression roots.
- `coreutils`, the GNU filesystem, text, checksum, and shell utility suite.
- `diffutils`, GNU `diff`, `cmp`, `diff3`, and `sdiff` file-comparison tools.
- `findutils`, GNU filesystem traversal and argument-driven process execution.
- `vim`, the ncurses-backed editor, Ex mode, runtime, and `xxd` tools.
- `git`, distributed version control with Kandelo-native HTTP and HTTPS transport.
- `texlive`, the pdfTeX engine plus its pinned macro, font, and format runtime.
- `bc`, GNU's arbitrary-precision calculator used by the main shell image;
- `posix-utils-lite`, the initial bundled 37-command compatibility Formula
  preserving the exact current shell output set while maintained upstream
  replacements continue to move into independent Formulae;
- `netcat`, GNU's virtual-network client and server utility;
- `msmtpd`, the minimal local SMTP listener used to capture or relay messages
  through a Dash-backed delivery command;
- `lsof`, Kandelo's procfs-aware open-file reporter;
- `nethack`, the ncurses game binary and its complete immutable data tree;
- `fbdoom`, the pinned framebuffer Doom engine with its reviewed Kandelo
  input, audio, and save-path adaptations (the shareware IWAD remains an
  external, integrity-checked demo asset);
- `tcl`, the threaded Tcl 9 interpreter, standard library, extension loader,
  and development files;
- `redis`, the Redis 7.2.5 threaded in-memory service and command-line client,
  built directly from the checksum-pinned upstream source; and
- `modeset`, the DRM/KMS fluid simulation used by the browser demo.

These seven exact-shell Formulae and Ruby intentionally use the transitional
`kandelo_build_package` bridge for their first bottle proof. Their Formulae pin
source identity, declare native and target dependencies, retain every current
shell output, validate final Wasm artifacts, and run through Kandelo.
The six recipes that accept already-extracted source isolate Homebrew's
checksum-verified tree from sibling caller-owned work and output roots; neither
the verified source nor the reviewed Kandelo checkout is a build destination.
NetHack compiles and tests its data lookup against
`/home/linuxbrew/.linuxbrew/opt/nethack/share/nethack`, so a composed image must
link both its executable and installed share tree at the poured guest opt path.
Decomposing their registry scripts into idiomatic Formula build steps remains
explicit follow-up work rather than a hidden change to the proof's scope.

Presence in `Formula/` means that the source recipe is tracked; it does not mean
that a current bottle has been published. A bottle becomes available only after
the trusted publisher writes its generated `bottle do` block and matching
`Kandelo/` sidecars. Use those generated files and the
[post-publication acceptance procedure](Kandelo/README.md#post-publication-acceptance),
not this source inventory, to decide whether a bottle is live.

The SDK is not yet a Homebrew dependency. Trusted builds supply an
`HOMEBREW_KANDELO_ROOT` checkout containing the SDK, sysroot, kernel, and Node
host used by Formula `test do` blocks. Registry-bridged source builds also
require the trusted publisher's fixed, read-only Tier-2 attestation before
Homebrew evaluates the Formula. The attestation binds the exact Formula,
support module, package metadata, build script, source identity, architecture,
and permitted script environment. It is absent from ordinary consumer
installs, so those installs cannot use the bridge and require a published
Kandelo bottle. This is an intentional fail-closed boundary, not general
source-build support. The publisher exposes its sealed, root-owned checker at
`target/<host>/release/xtask` through a Homebrew-prefixed bridge because
Homebrew removes ordinary environment variables before Formula tests. The
support module validates that exact layout and freezes the path before Formula
code runs. A sealed publisher also carries the resolver's content-addressed
program generations in the fixed `.ci-test-binary-cache/programs` child of the
same authoritative checkout. The support module requires that exact real
directory at load time and restores its frozen cache root, repository root, and
checker only for Kandelo's Tier-2 build and Node/browser test processes. This
keeps the relative `binaries/` mirrors attached to their complete package
generation instead of flattening them into mutable source-checkout files.

During a source build, the shared Formula support removes Homebrew's global
`bin`/`sbin` directories and Kandelo runtime dependency executable directories
from the host `PATH`. Those paths can contain linked target Wasm from unrelated
Formulae as well as the current Formula's dependencies. Full tap names passed
to the `formula_opt_*` helpers resolve to the exact installed target keg, so a
native Homebrew alias with the same short name cannot redirect a cross build to
host headers or libraries. Formulae map those host keg paths to stable guest
opt paths for compiled runtime identities and explicit test staging. Native
Homebrew build dependencies remain available through their versioned `opt/bin`
paths.

SDK activation also exports `WASM_POSIX_DEP_PKG_CONFIG_PATH` from the existing
`lib/pkgconfig` and `share/pkgconfig` directories in the exact versioned kegs
of the Formula's declared Kandelo runtime dependency closure. The declaration
is rebuilt for each activation and replaces any ambient value; native,
undeclared, global, and mutable `opt` paths are never included. Formulae retain
ownership of `PKG_CONFIG_PATH`, which selects and orders the target `.pc`
directories the SDK may use.

Sysroot activation removes host `LIBRARY_PATH` before target compilation.
Otherwise pkgconf can classify a Kandelo dependency's library directory as a
native system path and remove its required `-L` flag. It also removes
`LD_RUN_PATH` so the native linker's implicit runtime search state cannot enter
the target build. The scoped Formula build helper restores the caller's
environment afterward.

Formula tests that fork process trees declare the exact descendant count. The
default contract requires every descendant to exit successfully; service tests
with intentional signal-based teardown may instead declare the exact multiset
of expected descendant statuses. Missing, extra, or unexpected descendants fail
the test.

Formula assertions that request merged output combine only the guest's stdout
and stderr callbacks in their original order. Host-runtime and worker
diagnostics remain on the embedding process's stderr and never become guest
assertion bytes.

The isolated Node runner used by `kandelo_run_wasm` receives `/bin/sh` from
Kandelo's reviewed binary resolver. The publisher materializes the wasm32 Dash
base-system artifact for every target architecture, including wasm64 Formula
builds, and a missing or stale artifact fails the test. An explicit `/bin/sh`
entry in `exec_programs:` remains authoritative for tests that deliberately
exercise another shell. Guest-file maps are written to an ephemeral testpath
manifest, and only that bounded path crosses the host process environment; the
runner still validates and stages every declared guest path individually.

## Publication State

Bottle metadata must be generated from the same trusted build that produces
the bottle bytes. Do not hand-write placeholder hashes or reuse bottle data
across Kandelo ABI versions. The repository-rooted native Homebrew Open
Container Initiative (OCI) publisher is implemented, and the first-party
bottle catalog rollout is in progress. User-facing installation instructions
remain gated on real stock-guest Homebrew validation in `Automattic/kandelo`.

Bottle operations use `repository_dispatch`, so GitHub always loads the small
caller workflow from tap `main`. These tap workflows contain no shell steps or
other executable logic. They pass request data to the reviewed reusable
publisher and maintenance workflows in `Automattic/kandelo`, which validate the
request and own the build, credential isolation, artifact verification, and tap
finalization logic. Write operations also pass the exact 40-character tap
commit that contains the reviewed Formula source. The workflow definition
comes from protected `main`, while the build input is immutable.

A dry run may select unmerged Formula or Kandelo code through event payload
repositories and refs:

```bash
gh api --method POST repos/kandelo-dev/homebrew-tap-core/dispatches \
  -f event_type=dry-run-kandelo-bottles \
  -f 'client_payload[formulae]=bzip2,xz' \
  -f 'client_payload[arches]=wasm32' \
  -f 'client_payload[tap_ref]=main' \
  -f 'client_payload[kandelo_ref]=main'
```

Replace `main` with a reviewed branch name or exact commit SHA when the dry run
needs to execute unmerged tap or Kandelo code. The repositories and refs are
data passed to the publisher; they never select the dispatch workflow
definition. The caller grants the reusable workflow's maximum permission
ceiling because a called workflow cannot elevate caller authority. The reusable
workflow narrows each scheduled job, and a dry run never schedules its bottle
upload or tap-finalization jobs.

Write publication accepts formulae, arches, and an optional release tag. The
current caller pins its reusable publisher and package consumer to the same
exact Kandelo `main` commit. It also pins one immutable
`package-generation-rootfs-wasm32-abi-v42-sha256-...` tag admitted by that
commit. The generation record proves that the selected rootfs build-input
closure is identical to the preserved pre-merge closure and verifies every
archive byte before use. The earlier staging tag remains promotion evidence;
it is neither caller authority nor a mutable package source.

This selected rootfs generation is a deliberately bounded bridge for the
wasm32 Bash/M4 shell proof. It cannot select wasm64, another Formula, or the
legacy dependency-bearing VFS acceptance graph. A write caller therefore
cannot expand the preserved closure through event data. Dry runs remain the
separate staging path: they may select reviewed branch or commit refs and do
not inherit the production generation pin.

The controller fixes Formula source to the exact reviewed tap commit that it
validated and recorded. The protected caller itself may load from a later
finalizer-only `main` commit than that reserved Formula source. The controller
records the actual `repository_dispatch` source separately and accepts it only
when the Formula catalog, support tree, and normalized caller contract remain
frozen and every intervening path is generated finalizer output.

Set `KANDELO_FORMULA` to the exact short name of one dependency-ready Formula,
then submit the dispatch:

```bash
: "${KANDELO_FORMULA:?set KANDELO_FORMULA to one dependency-ready Formula name}"
: "${KANDELO_TAP_SHA:?set KANDELO_TAP_SHA to the exact reviewed tap commit}"
gh api --method POST repos/kandelo-dev/homebrew-tap-core/dispatches \
  -f event_type=publish-kandelo-bottles \
  -f "client_payload[formulae]=${KANDELO_FORMULA}" \
  -f 'client_payload[arches]=wasm32' \
  -f "client_payload[tap_sha]=${KANDELO_TAP_SHA}"
```

The first-party catalog rollout uses one Formula per write dispatch, even
though the reusable workflow supports a comma-separated Formula list for other
controlled operations. Keep no more than eight write-publication runs queued or
in progress at once. This is a soft operator batch limit, not a correctness
boundary: Formula-scoped index concurrency serializes same-Formula OCI index
writers, and the tap-wide `homebrew-tap-publish` state lock serializes
finalizers; excess runner work may queue. Dispatch only a dependency-ready
Formula: every required
same-tap build, test, and runtime dependency must already have a successful
bottle on tap `main` for the selected architecture, current Kandelo ABI, and
repository-rooted bottle namespace. A failed Formula blocks its downstream
dependents, but it does not block unrelated ready Formulae from filling an
available slot.

### Fresh publication campaigns

This mostly-lazy-shell package generation starts from three reviewed tap
commits. `T0` is the
stable base with a package-owned last-green sidecar for every Formula; its
aggregate metadata may still contain only the product subset completed by an
earlier campaign. A Formula at `T0` may already be one or more rebuilds ahead
of that sidecar when an earlier campaign reserved an identity without replacing
the last-green bottle. `Tpre` is a strict descendant; it
retains `T0`'s aggregate metadata, package-owned sidecars, Formula support,
recipes, dependencies, architectures, and last-green hashes. Its reviewed
reservation changes only Bash's `rebuild` line to the exact successor of its
Formula identity at `T0`.

`Tmanifest` is an exact protected-main descendant of `Tpre`. It commits
`Kandelo/campaigns/mostly-lazy-shell-abi42-rootfs-wasm32.json`, the controller,
tests, and operator documentation, but changes none of `Tpre`'s package
publication sources. The typed manifest is the sole production selection
authority: it identifies ABI 42 and rootfs wasm32, binds Bash's exact old and
reserved identities, and binds exactly 23 reused bottles to their Formula
identity, byte count, blob SHA-256, and raw `T0` sidecar and link-manifest
paths and SHA-256 values. Every other `T0` Formula is derived as deferred; the
manifest stores no human-maintained deferred list, run ID, package ID, bearer
token, mutable download URL, or GitHub API object ID. The controller also pins
the reviewed manifest's raw SHA-256, so naming a different exact protected-main
commit cannot substitute another otherwise well-formed 23-entry selection.

A compatible Kandelo kernel or host-runtime change does not by itself make a
bottle payload stale. Keep the bottle's original `built_from` provenance and
place it in `reuse` only after digest-bound validation against the new main
commit. Put a Formula in `rebuild` when a real payload input changed: its
recipe/source, architecture, ABI contract, SDK/libc/sysroot, applicable fork
instrumenter, or a build dependency payload. Put intentionally unvalidated
Formulae in `deferred`. Never reserve a successor merely to make its producer
commit equal the current kernel commit. Initialization also rejects a reused
Formula unless its package-owned sidecar and every required architecture
already identify ABI 42; an older-ABI bottle belongs in `rebuild` or
`deferred`, never `reuse`.

The caller workflow at `Tmanifest` must be registered in
`APPROVED_CAMPAIGN_CONTRACTS` with its complete SHA-256, reusable publisher
SHA, Kandelo package-consumer SHA, and sealed package-generation SHA and tag.
Command-line values select that reviewed authority; they cannot bless an
arbitrary workflow.

Create a new private ledger before dispatching anything:

```bash
: "${KANDELO_ROLLOUT_STATE:?choose a new private state-file path}"
: "${KANDELO_T0:?set the exact reviewed last-green base tap SHA}"
: "${KANDELO_TPRE:?set the exact Bash-reservation tap SHA}"
: "${KANDELO_TMANIFEST:?set the exact protected manifest-authority tap SHA}"
: "${KANDELO_CONSUMER_SHA:?set the reviewed package-consumer SHA}"
: "${KANDELO_PUBLISHER_SHA:?set the reviewed reusable publisher SHA}"
: "${KANDELO_GENERATION_SHA:?set the reviewed package-generation SHA}"
: "${KANDELO_GENERATION_TAG:?set the reviewed package-generation tag}"
: "${KANDELO_CALLER_SHA256:?set the reviewed complete caller SHA-256}"
python3 scripts/abi42-rollout.py \
  --tap-root "$PWD" \
  --expected-kandelo-sha "$KANDELO_CONSUMER_SHA" \
  --state-file "$KANDELO_ROLLOUT_STATE" \
  --initialize-campaign \
  --campaign-id mostly-lazy-shell-abi42-rootfs-wasm32 \
  --campaign-base-tap-sha "$KANDELO_T0" \
  --campaign-reservation-tap-sha "$KANDELO_TPRE" \
  --campaign-manifest-tap-sha "$KANDELO_TMANIFEST" \
  --expected-publisher-sha "$KANDELO_PUBLISHER_SHA" \
  --expected-package-generation-sha "$KANDELO_GENERATION_SHA" \
  --expected-package-generation-tag "$KANDELO_GENERATION_TAG" \
  --expected-workflow-sha256 "$KANDELO_CALLER_SHA256"
```

Initialization sends no event. It rejects an existing state path, validates all
63 last-green sidecars and retained checksum blocks, reads the manifest bytes
from exact `Tmanifest` rather than the mutable worktree, and verifies its
embedded exact `T0` and `Tpre`. It freezes the separate last-green, `T0`, and
`Tpre` catalogs. It verifies every reused raw sidecar and link-manifest byte at
exact `T0`, checks their decoded identities, derives each immutable GHCR blob
endpoint from the fixed namespace, Formula, and digest, and anonymously streams
and hashes all 23 complete blobs. Operator-provided partition lists are
rejected.

Initialization records only Bash's new architecture identity and requires
anonymous absence only of its new OCI version-index reference. It also requires
an idle publication workflow and rechecks protected `main` immediately before
one mode-0600, fsynced ledger write. The ledger records both exact `Tmanifest`
and the raw manifest SHA-256. It never imports or reinterprets an older ledger.
Upload-child tags are content-derived and cannot be reserved before a build.

Never reuse, overwrite, copy, or reconstruct an earlier campaign ledger.
`--dispatch` requires an existing ledger and cannot initialize one implicitly.
The local ledger assumes this controller remains the sole production
dispatcher; it cannot prevent another authorized operator from manually
publishing between anonymous registry checks.

For the first mostly-lazy shell proof, the committed manifest rebuilds only
Bash. It reuses these 23 public ABI-42 wasm32 bottles after raw-metadata,
digest, and size
validation: `bzip2`, `coreutils`, `curl`, `dash`, `diffutils`, `ed`,
`findutils`, `gawk`, `git`, `grep`, `gzip`, `less`, `libcurl`, `libcxx`, `m4`,
`ncurses`, `openssl`, `posix-utils-lite`, `ruby`, `sed`, `tar`, `vim`, and
`zlib`. `libmagic` and `file-formula` remain deferred because their public
bottles are still ABI 41; all other Formulae outside this first product closure
are also deferred rather than rebuilt speculatively. Bzip2 already supplies
the first-party public-bottle lifecycle proof, while the independent
`brandonpayton/kandelo-canary` M4 bottle remains the live third-party-tap proof.
A dispatch allowlist further confines this campaign's only rebuild:

```bash
python3 scripts/abi42-rollout.py \
  --tap-root "$PWD" \
  --expected-kandelo-sha "$KANDELO_CONSUMER_SHA" \
  --state-file "$KANDELO_ROLLOUT_STATE" \
  --dispatch \
  --formulae bash
```

The allowlist is fail-closed: unknown, empty, or duplicate names are rejected.
Every transitive dependency that also belongs to the rebuild partition must
appear in the allowlist. Reused dependencies are not dispatchable; their
validation must already be finalized on tap main before a dependent becomes
ready. Immediately before the first write dispatch in every controller
invocation, the controller reloads and hashes the manifest from exact
`Tmanifest`, revalidates every raw `T0` sidecar and link manifest, and
anonymously streams and hashes all 23 complete reused blobs again. It then
rechecks protected main, active capacity, active Formulae, and anonymous
absence of every planned successor. That final absence check occurs
immediately before the first planned intent becomes `request-started`; an
identity occupied during the long blob proof leaves the whole batch planned
and sends nothing. The controller then submits a capacity-bounded batch drawn
only from the rebuild partition. Reused and deferred Formulae can never
consume a dispatch slot in that campaign.

The rollout controller first journals every Formula in the available
capacity-bounded batch in its already initialized private ledger. Before each
HTTP request it records a random `abi42-…` dispatch token and a
`request-started` marker, then submits the independent requests back-to-back.
The caller exposes the Formula and token in the workflow run name, so one
workflow-run snapshot can
acknowledge the whole batch as soon as GitHub creates the outer runs; the
controller does not wait several minutes for each reusable workflow's
generated job matrix. Active capacity comes from one unfiltered, paginated
snapshot filtered locally, avoiding gaps while GitHub moves a run between
statuses. Every snapshot is collected twice and must have the same total,
pages, and unique run IDs before the controller acts.

If acknowledgement times out, do not dispatch any pending Formula again: a
request may already have succeeded. Recover every currently visible exact token
match against the same ledger:

```bash
: "${KANDELO_ROLLOUT_STATE:?set this to the existing ABI 42 rollout ledger}"
python3 scripts/abi42-rollout.py \
  --tap-root "$PWD" \
  --expected-kandelo-sha "$KANDELO_CONSUMER_SHA" \
  --state-file "$KANDELO_ROLLOUT_STATE" \
  --recover-dispatch
```

Recovery sends no event. A legacy single-intent marker still uses its recorded
pre-dispatch run boundary and exact generated matrix. New markers query only
the bounded creation-time range around their durable HTTP requests and paginate
the whole result. They use their unguessable token and Formula to select exactly
one `repository_dispatch` run without depending on job creation. The ledger
keeps both the reserved tap commit and the run's actual source commit. The
latter may be a finalizer-only descendant after an earlier parallel run advances
`main`, but only when exact `Tmanifest` remains on its history and the frozen
Formula catalog, Formula support tree, and
normalized publication workflow remain equivalent. Recipe, support, workflow,
controller, or unrelated path drift fails closed.

One recovery pass atomically records every visible independent match and
retains later ones. No matches, duplicate token runs, a partial or changing
snapshot, or any identity mismatch leaves the affected pending markers
unchanged. Resume normal dispatching only after every request-started or
submitted marker is correlated; never retry an ambiguous token.

If an operator deliberately cancels the sole correlated run before any
external-write job starts, preserve that fact and release the intent with the
explicit run ID:

```bash
: "${KANDELO_CANCELLED_RUN_ID:?set this to the sole cancelled publication run}"
python3 scripts/abi42-rollout.py \
  --tap-root "$PWD" \
  --expected-kandelo-sha "$KANDELO_CONSUMER_SHA" \
  --state-file "$KANDELO_ROLLOUT_STATE" \
  --abandon-dispatch-run "$KANDELO_CANCELLED_RUN_ID"
```

Abandonment is narrower than recovery. It requires a completed, cancelled,
sole post-intent run with the exact Formula and architecture matrix on the
protected-main history. Every registry upload, index publication, tap
finalization, and VFS release job must exist and have zero steps. A missing
job, any started step, an ambiguous run, or an incomplete GitHub result leaves
the marker unchanged. The controller records the cancelled run and both tap
commits in `abandoned_dispatches` before clearing the unresolved marker; it
sends no event.

The rollout ledger is part of the write-safety boundary. Each replacement
fsyncs both the file and its parent directory so a host crash cannot erase a
durable request marker while leaving its GitHub dispatch alive. Preserve the
original private ledger after the first Formula is finalized: it freezes the
reviewed base, initial reservation catalog, complete publication authority, and
successful, failed, planned, request-started, submitted, unresolved, and safely
abandoned dispatch history. Schema-1 ABI 42 ledgers retain their explicitly
reviewed workflow-trust migration for recovery. The manifest-backed shell
campaign uses schema 4; it never inherits or reinterprets older campaign state
and never rotates its complete authority implicitly.

If a schema-4 campaign fails before its selected rebuild is published and the
repair advances the Kandelo consumer or package generation, preserve that
campaign's private ledger and committed failure report as historical evidence.
Do not recover, overwrite, or reuse the failed ledger. Land the corrected
publisher, consumer, generation tag, and complete caller hash together on a new
protected `Tmanifest`, then initialize a fresh mode-0600 ledger from that exact
authority. The typed manifest remains byte-identical, including all 23 reused
bottle identities and digests; only the caller authority changes.

Token-based dispatch acknowledgement accepts only workflow attempt 1. A
manual rerun retains the original run ID, title, token, and caller commit, but
it does not represent a new request journaled by the campaign ledger.

Read-only status may derive an implicit Formula version from that Formula's
package-owned sidecar, but a write-capable
controller cross-checks the result against the frozen ledger. A missing ledger
always blocks dispatch; either restore that campaign's original file or create
a genuinely new campaign from a reviewed last-green `T0` and mechanical
reservation `Tpre` plus exact manifest authority `Tmanifest`.

After a controller-recorded publication fails, do not immediately dispatch it
again. First land the reviewed Formula or publisher correction on tap `main`,
then retire the exact failed run through the same private ledger:

```bash
: "${KANDELO_FAILED_RUN_ID:?set this to the controller-recorded failed run ID}"
python3 scripts/abi42-rollout.py \
  --tap-root "$PWD" \
  --expected-kandelo-sha "$KANDELO_CONSUMER_SHA" \
  --state-file "$KANDELO_ROLLOUT_STATE" \
  --recover-failed-run "$KANDELO_FAILED_RUN_ID"
```

Repeat `--recover-failed-run RUN_ID` in the same invocation when one reviewed
tap change reserves or fixes multiple Formulae. The controller validates the
complete set against one tap snapshot and either migrates all of their ledger
entries in one file replacement or migrates none of them.

A plan-stage failure can occur before GitHub expands the Formula matrix. For an
unresolved controller intent, include that exact run ID in the same
`--recover-failed-run` batch. Recovery then requires the sole post-intent run on
the recorded caller head, the exact Formula and architecture inputs in the plan
log, a failed plan job, every downstream job skipped with zero steps, and an
anonymous 404 for the bottle identity.

If an operator already sent a narrowly reviewed replacement dispatch before it
could be recorded, retain an exact pre-matrix no-write failure explicitly:

```bash
python3 scripts/abi42-rollout.py \
  --tap-root "$PWD" \
  --expected-kandelo-sha "$KANDELO_CONSUMER_SHA" \
  --state-file "$KANDELO_ROLLOUT_STATE" \
  --recover-failed-run "$KANDELO_PREVIOUS_FAILED_RUN_ID" \
  --adopt-failed-run "make=$KANDELO_UNRECORDED_FAILED_RUN_ID"
```

Explicit adoption is deliberately limited to completed pre-matrix failures. It
requires the production workflow ID and an explicitly approved complete caller
hash, then parses the exact plan log to bind Formula, architectures, publisher,
consumer, and tap source. The failed plan job must have had exactly
`contents: read` and `metadata: read`; every downstream write-capable job must
also be proven skipped before the anonymous-404 proof can retain the same
identity. It cannot adopt a successful, active, post-matrix, mutable tap-source,
write-authorized, rerun (`run_attempt > 1`), or otherwise ambiguous run.

This recovery sends no event and uses no registry credential. It requires the
exact completed failed `repository_dispatch` and Formula/architecture matrix
recorded by the controller. It then reads the failed OCI reference anonymously:

- If the reference exists, the current Formula must reserve exactly the next
  `rebuild`, retain every last-green checksum, and change no other Formula byte.
  The manifest's exact digest is retained in the ledger.
- If the reference returns an exact anonymous 404, the current Formula must
  retain the same `rebuild` and last-green checksums. Every upload, version-index,
  successful tap-finalization, and VFS-release credential step must also be
  proven skipped by the complete GitHub job result. A failed-attempt report on
  tap `main` is preserved but does not occupy the bottle identity.

The controller moves the old dispatch into `failed_attempts`, records its old
and replacement catalog entries and evidence, and updates the one Formula's
frozen catalog in the same locked mode-0600 file replacement. Any ambiguous
run, changed stable identity, missing job or step, unexpected registry response,
or unrelated catalog drift leaves the ledger byte-for-byte unchanged. Resume
normal `--dispatch` operation only after recovery succeeds; that path creates
the fresh `repository_dispatch`. Do not select **Re-run jobs** on the old run:
a rerun retains the original caller workflow and its pinned reusable-workflow
revision, while a fresh dispatch loads the reviewed caller now on tap `main`,
creates new run-local receipts, and replans against current tap state. Preserve
the old run and failure report, and never move artifacts manually between runs.
The [authoritative Homebrew publishing contract](https://github.com/Automattic/kandelo/blob/main/docs/homebrew-publishing.md#public-package-creation-and-legacy-namespace-retirement)
owns the complete trust, readiness, read-only acceptance, and legacy-cleanup
procedure. This tap links to that procedure instead of duplicating operator
commands that must change with the publisher and namespace contracts.

When reviewed publisher code advances without changing the ABI 42 package
consumer, failed-run recovery also migrates the private workflow trust root in
that same atomic replacement. The ledger keeps the old workflow hash and
publisher SHA in `workflow_rotations`, activates the new workflow hash and
publisher SHA, and leaves `expected_kandelo_sha` unchanged. Historical attempts
remain auditable without allowing a publisher update to silently select a
different rootfs generation.

If the failed run already uploaded public bottle bytes and a retry could produce
different bytes, reserve the next bottle identity before dispatching it. Set the
Formula to the next positive `rebuild` and keep the complete last-green checksum
block until publication; do not invent a placeholder or overwrite the existing
registry reference. The failed-attempt ledger preserves the occupied public
manifest digest as evidence instead of making an unfinalized bottle appear
installable in Formula metadata. The retry's trusted finalizer replaces the
complete block with its generated checksum and matching sidecars. Run
`homebrew-validate` before merging the reservation. A Formula with no generated
catalog entry should remain validator-clean; if last-green sidecars exist,
document only their temporary rebuild mismatch and do not waive unrelated
validator failures.

Production keeps `kandelo-dev/tap-core` as the canonical Homebrew identity for
Formula references, OCI titles, and sidecars. Bottle transport instead uses the
exact public source-repository namespace,
`ghcr.io/kandelo-dev/homebrew-tap-core/<formula>`. Child and version-index
uploads use only this repository's scoped built-in `GITHUB_TOKEN`; the caller
passes no package PAT and the publisher performs no visibility mutation. A
write publication cannot finalize Formula or sidecar state until the exact
uploaded digest is anonymously readable and its SHA-256 and byte count match.

The mostly-lazy main shell uses a separate, release-only caller:
`.github/workflows/publish-main-shell-mirror.yml`. Its reviewed bytes pin the
exact Kandelo shell, bottle catalog, and independent canary revisions; dispatch
data cannot replace them. See
[`Kandelo/main-shell-mirror-publication.md`](Kandelo/main-shell-mirror-publication.md)
for the fail-closed finalization, merge, publication, and Node/Chromium proof
sequence.

The repository-rooted GHCR canary is completed historical evidence. Its
data-only caller remains pinned to one reviewed Kandelo commit and must not be
dispatched again: run `29652866481` already created
`homebrew-tap-core/zlib`, so the canary's required absent-destination preflight
would now fail. The run replayed the immutable zlib OCI child produced by the
original `GITHUB_TOKEN` control and proved credential-free readback from the
repository-rooted destination. It intentionally stopped before publishing a
mutable version index, editing Formula metadata, running release verification,
or finalizing tap state.

Dry runs cannot publish bottle blobs or sidecar commits. They may upload
run-scoped diagnostic artifacts, but later write-capable bottle jobs do not
restore state produced by an untrusted dry run.

Rebuild and rollback maintenance uses a separate reviewed entry point. Rebuilds
may provide expected cache keys and may explicitly force work that current
metadata would otherwise skip. Rollbacks preserve the last-green metadata and
record the reason; deleting a package additionally requires its URL and an
operational reason.

```bash
: "${KANDELO_TAP_SHA:?set KANDELO_TAP_SHA to the exact reviewed tap commit}"
gh api --method POST repos/kandelo-dev/homebrew-tap-core/dispatches \
  -f event_type=maintain-kandelo-bottles \
  -f 'client_payload[mode]=rebuild' \
  -f 'client_payload[formulae]=bzip2,xz' \
  -f 'client_payload[arches]=wasm32' \
  -f "client_payload[tap_sha]=${KANDELO_TAP_SHA}"
```

Formula Ruby and Homebrew bottle metadata remain authoritative for Homebrew.
Files under `Kandelo/` are additive validation and provenance data and must not
be required for a stock guest install.
