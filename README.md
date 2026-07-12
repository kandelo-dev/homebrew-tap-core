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
- `sqlite`, `bzip2`, and `xz`, the dependency-first source-build pilot;
- `zstd`, the threaded Zstandard library and command-line dependency root;
- `openssl`, the first dependency-root library migration;
- `libpng` and `libxml2`, zlib-backed dependency-root libraries;
- `libcxx`, the LLVM C++ standard library, ABI runtime, and bundled unwinder;
- `libcurl`, the TLS, compression, threaded-resolver, and Unix-socket transfer library;
- `curl`, the matching command-line transfer client linked against the tap library;
- `ncurses`, the wide-character terminal library and CLI dependency root;
- `sed`, the GNU stream-editing CLI used by shell and build workflows;
- `gzip`, the GNU compression CLI with native gunzip and zcat aliases;
- `grep`, GNU regular-expression and file search for the leaf CLI wave;
- `pcre2`, the Unicode-capable regex library, POSIX wrapper, and upstream CLI tools;
- `dash`, the dependency-free POSIX shell with instrumented subprocess support.

The SDK is not yet a Homebrew dependency. Trusted builds supply an
`HOMEBREW_KANDELO_ROOT` checkout containing the SDK, sysroot, kernel, and Node
host used by Formula `test do` blocks. Guest installation therefore requires a
published Kandelo bottle; building from source is currently a maintainer and CI
workflow.

## Publication State

Bottle metadata must be generated from the same trusted build that produces
the bottle bytes. Do not hand-write placeholder hashes or reuse bottle data
across Kandelo ABI versions. The existing `hello` sidecar files are historical
publication evidence; broader publication is gated on the native Homebrew OCI
publisher and real guest-install validation in `Automattic/kandelo`.

Formula Ruby and Homebrew bottle metadata remain authoritative for Homebrew.
Files under `Kandelo/` are additive validation and provenance data and must not
be required for a stock guest install.
