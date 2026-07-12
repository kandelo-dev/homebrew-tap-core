# Kandelo Homebrew Tap

This repository is Kandelo's first-party Homebrew tap. It owns Formula source,
Kandelo-specific Formula support, generated bottle blocks, and publication
evidence. The `Automattic/kandelo` repository owns the kernel, host runtime,
SDK, package-build infrastructure, and trusted publisher.

The tap is still experimental. Do not publish user-facing `brew tap` or
`brew install` instructions until a stock guest Homebrew install has been
validated inside Kandelo.

## Formulae

Formulae under `Formula/` use normal Homebrew metadata and build their staged
upstream source through Kandelo's worktree-local SDK. Shared cross-compilation
and runtime-test mechanics live in
`Kandelo/formula_support/kandelo_formula_support.rb`.

Current migration controls and pilots include:

- `hello`, the original publication control;
- `zlib` and `ruby`, the first dependency and heavy-runtime Formulae;
- `sqlite` and the `bzip2`/`xz` compression tools and static libraries, the dependency-first source-build pilot;
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
- `less`, the terminal pager linked against the tap's real ncurses termcap interface;
- `bash`, the GNU interactive shell with real pipelines, subprocesses, and process substitution;
- `sed`, the GNU stream-editing CLI used by shell and build workflows;
- `gzip`, the GNU compression CLI with native gunzip and zcat aliases;
- `grep`, GNU regular-expression and file search for the leaf CLI wave;
- `pcre2`, the Unicode-capable regex library, POSIX wrapper, and upstream CLI tools;
- `dash`, the dependency-free POSIX shell with instrumented subprocess support;
- `make`, GNU dependency-driven build automation using the tap's POSIX shell;
- `ed`, the conforming line editor and restricted editor required by patch workflows;
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
- `ctags`, Universal Ctags' maintained tag generator, `readtags` query client,
  and optscript interpreter with complete C and C++ workflows.
- `tar`, the GNU archive creation and extraction CLI.
- `wget`, GNU HTTP and HTTPS retrieval linked against the tap TLS and compression roots.

The SDK is not yet a Homebrew dependency. Trusted builds supply an
`HOMEBREW_KANDELO_ROOT` checkout containing the SDK, sysroot, kernel, and Node
host used by Formula `test do` blocks. Guest installation therefore requires a
published Kandelo bottle; building from source is currently a maintainer and CI
workflow.

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
exercise another shell.

## Publication State

Bottle metadata must be generated from the same trusted build that produces
the bottle bytes. Do not hand-write placeholder hashes or reuse bottle data
across Kandelo ABI versions. The existing `hello` sidecar files are historical
publication evidence; broader publication is gated on the native Homebrew OCI
publisher and real guest-install validation in `Automattic/kandelo`.

Bottle workflows use `repository_dispatch`, which always loads the workflow
definition from tap `main`. A read-only dry run may select unmerged formula and
Kandelo code only through event payload refs:

```bash
gh api --method POST repos/Automattic/kandelo-homebrew/dispatches \
  -f event_type=dry-run-kandelo-bottles \
  -f 'client_payload[formulae]=bzip2,xz' \
  -f 'client_payload[arches]=wasm32' \
  -f 'client_payload[tap_ref]=migrate/compression-library-surfaces' \
  -f 'client_payload[kandelo_ref]=main'
```

Write publication accepts formulae, arches, and an optional release tag, but
hardcodes both executable repositories to reviewed `main`:

```bash
gh api --method POST repos/Automattic/kandelo-homebrew/dispatches \
  -f event_type=publish-kandelo-bottles \
  -f 'client_payload[formulae]=bzip2,xz' \
  -f 'client_payload[arches]=wasm32'
```

Dry runs cannot publish bottle blobs or sidecar commits. They may upload
run-scoped diagnostic artifacts and use GitHub Actions storage, but no
write-capable bottle workflow restores dependency caches produced by them.

Formula Ruby and Homebrew bottle metadata remain authoritative for Homebrew.
Files under `Kandelo/` are additive validation and provenance data and must not
be required for a stock guest install.
