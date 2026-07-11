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
- `libcxx`, the LLVM C++ standard library, ABI runtime, and bundled unwinder;
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
- `ctags`, Universal Ctags' maintained tag generator, `readtags` query client,
  and optscript interpreter with complete C and C++ workflows.
- `netcat`, GNU TCP and UDP client/listener workflows across virtual Kandelo machines.
- `redis`, the threaded in-memory service and its network client.

The SDK is not yet a Homebrew dependency. Trusted builds supply an
`HOMEBREW_KANDELO_ROOT` checkout containing the SDK, sysroot, kernel, and Node
host used by Formula `test do` blocks. Guest installation therefore requires a
published Kandelo bottle; building from source is currently a maintainer and CI
workflow.

During a source build, the shared Formula support removes Kandelo runtime
dependency executable directories from the host `PATH`. Those dependencies are
target Wasm. Full tap names passed to the `formula_opt_*` helpers resolve to the
exact installed target keg, so a native Homebrew alias with the same short name
cannot redirect a cross build to host headers or libraries. Formulae map those
host keg paths to stable guest opt paths for compiled runtime identities and
explicit test staging. Native Homebrew build dependencies remain on the host
`PATH`.

## Publication State

Bottle metadata must be generated from the same trusted build that produces
the bottle bytes. Do not hand-write placeholder hashes or reuse bottle data
across Kandelo ABI versions. The existing `hello` sidecar files are historical
publication evidence; broader publication is gated on the native Homebrew OCI
publisher and real guest-install validation in `Automattic/kandelo`.

Formula Ruby and Homebrew bottle metadata remain authoritative for Homebrew.
Files under `Kandelo/` are additive validation and provenance data and must not
be required for a stock guest install.
