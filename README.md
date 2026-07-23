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
- `mandoc`, the normal `man`, `apropos`, `whatis`, and `makewhatis` toolset,
  with roff formatting, section lookup, compressed-page support, and real
  terminal paging through the tap's `less`;
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
- `lsof`, Kandelo's procfs-aware open-file reporter;
- `nethack`, the ncurses game binary and its complete immutable data tree;
- `fbdoom`, the pinned framebuffer Doom engine with its reviewed Kandelo
  input, audio, and save-path adaptations (the shareware IWAD remains an
  external, integrity-checked demo asset);
- `tcl`, the threaded Tcl 9 interpreter, standard library, extension loader,
  and development files; and
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
source-build support.

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
finalization logic.

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

Write publication accepts formulae, arches, and an optional release tag, but
hardcodes both executable repositories to reviewed `main`. Set
`KANDELO_FORMULA` to the exact short name of one dependency-ready Formula, then
submit the dispatch:

```bash
: "${KANDELO_FORMULA:?set KANDELO_FORMULA to one dependency-ready Formula name}"
gh api --method POST repos/kandelo-dev/homebrew-tap-core/dispatches \
  -f event_type=publish-kandelo-bottles \
  -f "client_payload[formulae]=${KANDELO_FORMULA}" \
  -f 'client_payload[arches]=wasm32'
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

After a failed publication or a publisher-pin change, submit a fresh
`repository_dispatch`; do not select **Re-run jobs** on the old run. A rerun
retains the original caller workflow and its pinned reusable-workflow revision,
while a fresh dispatch loads the reviewed caller now on tap `main`, creates new
run-local receipts, and replans against current tap state. Preserve the old run
and failure report, and never move artifacts manually between runs. The
[authoritative Homebrew publishing contract](https://github.com/Automattic/kandelo/blob/main/docs/homebrew-publishing.md#public-package-creation-and-legacy-namespace-retirement)
owns the complete trust, readiness, read-only acceptance, and legacy-cleanup
procedure. This tap links to that procedure instead of duplicating operator
commands that must change with the publisher and namespace contracts.

If the failed run already uploaded public bottle bytes and a retry could produce
different bytes, reserve the next bottle identity before dispatching it. Set the
Formula to the next positive `rebuild` and keep the SHA-256 of the occupied
public child in that temporary bottle block as reviewable evidence; do not invent
a placeholder or overwrite the existing registry reference. The retry's trusted
finalizer replaces the complete block with its generated checksum and matching
sidecars. Run `homebrew-validate` before merging the reservation. A Formula with
no generated catalog entry should remain validator-clean; if last-green
sidecars exist, document only their temporary rebuild mismatch and do not waive
unrelated validator failures.

Production keeps `kandelo-dev/tap-core` as the canonical Homebrew identity for
Formula references, OCI titles, and sidecars. Bottle transport instead uses the
exact public source-repository namespace,
`ghcr.io/kandelo-dev/homebrew-tap-core/<formula>`. Child and version-index
uploads use only this repository's scoped built-in `GITHUB_TOKEN`; the caller
passes no package PAT and the publisher performs no visibility mutation. A
write publication cannot finalize Formula or sidecar state until the exact
uploaded digest is anonymously readable and its SHA-256 and byte count match.

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
gh api --method POST repos/kandelo-dev/homebrew-tap-core/dispatches \
  -f event_type=maintain-kandelo-bottles \
  -f 'client_payload[mode]=rebuild' \
  -f 'client_payload[formulae]=bzip2,xz' \
  -f 'client_payload[arches]=wasm32'
```

Formula Ruby and Homebrew bottle metadata remain authoritative for Homebrew.
Files under `Kandelo/` are additive validation and provenance data and must not
be required for a stock guest install.
