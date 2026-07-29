# Formula Directory

This directory contains Kandelo's first-party Homebrew Formulae.

Formulae should use normal Homebrew DSL for source identity, dependencies,
patches, installation, bottles, and `test do`. Kandelo-specific SDK activation,
wasm cross-compilation, and kernel-backed tests belong in the shared
`KandeloFormulaSupport` mixin.

Simple packages build Homebrew's staged source directly in `install`. A port
that still needs a substantial script should own a closed tap recipe at
`Kandelo/recipes/<formula>/` and invoke `kandelo_build_tap_recipe`. The
transitional `kandelo_build_package` registry-script bridge is reserved for
ports that have not yet moved, and each use remains explicit migration debt.
Both helpers are executable only in the protected publisher after it installs
a fixed, read-only attestation. Ordinary `brew install` consumers do not
receive that authority: they must pour a published bottle, and a scripted
Formula without a bottle must fail closed rather than fall back to source.

A tap recipe contains `recipe.json` plus every script, patch, or configuration
input it uses. The manifest lists the sorted direct target Formula dependencies,
one `.sh` entrypoint, and every other member's canonical relative path, byte
count, `0644`/`0755` mode, and SHA-256. The Formula repeats the manifest
SHA-256 as a literal:

```ruby
KANDELO_TAP_RECIPE = true

def install
  out_dir = kandelo_build_tap_recipe(
    manifest_sha256: "...",
    script_env: {
      "EXAMPLE_FEATURE" => "enabled",
    },
  )
  prefix.install out_dir.children
end
```

Do not copy a registry build script unchanged. Convert source reads to
`WASM_POSIX_DEP_SOURCE_DIR`, scratch writes to `WASM_POSIX_DEP_WORK_DIR`, final
artifacts to `WASM_POSIX_DEP_OUT_DIR`, patches and other recipe inputs to
`WASM_POSIX_DEP_RECIPE_DIR`, and each direct target dependency to its
`WASM_POSIX_DEP_<NAME>_DIR` Homebrew keg. Tap recipes receive the SDK, sysroot,
and fork instrumenter, but the publisher makes the package registry, resolver,
local-binary mirror, transported registry cache, and
`scripts/install-local-binary.sh` inaccessible.

Closed recipes do not execute as the Formula build user. Formula support sends
one bounded, canonical request through the publisher's fixed root-owned client
to a one-request root supervisor. The supervisor starts a network-disabled
transient service under a distinct recipe uid, mounts only the attested
recipe/source/platform/sysroot/dependency closure, waits for the entrypoint,
kills and collects the entire recipe cgroup, validates the result, and copies it
into a root-owned read-only tree. Formula support validates that sealed evidence
in full, then copies the complete tree once into a private, Formula-owned
materialization below the original `kandelo-package-out`. It preserves file
bytes, executable meaning, nested directories, and contained relative
symlinks; directories become `0755`, data files become `0644`, and executable
files become `0755`. The sealed tree is never chmodded, unlinked, or moved.
Only after both trees validate does Formula support return the private
randomly named materialization. Method return is the publication boundary:
there is no fixed-name rename that could replace a foreign path. Formulae can
therefore use normal Homebrew `prefix.install out_dir.children` and
`Pathname#install` move semantics for binaries, archives, and complete runtime
trees. The supervisor, client, and sealed-output parent are outside the
platform projection and are never mounted into the recipe service; putting the
client below `HOMEBREW_KANDELO_ROOT` would let a recipe invoke the privileged
boundary recursively.

If a failure occurs after private materialization begins, Formula support
leaves its partial tree below the mode-`0700` Formula-owned output root and
raises. It never deletes a pathname during failure handling because an identity
check followed by `unlink` or `rmdir` would still have a replacement race.
Homebrew's outer workspace cleanup owns removal after the failed Formula
process is finished.

The preflight, Formula runtime, and root runner independently reject undeclared
or changed input nodes, dependency environment-name collisions, traversal,
symlinks or hard links at authority boundaries, invalid modes, dependency
drift, and environment overrides. Output paths are canonical UTF-8 without
control characters and are bounded to 4,096 bytes. Output is limited to
262,144 entries, 1 GiB per regular file, and 2 GiB of aggregate logical file
bytes; sparse files do not bypass the logical-byte limits. Directories must be
`0555`/`0755`, regular files must be `0444`/`0555`/`0644`/`0755`, and special,
set-id, or writable-by-group/other nodes are rejected. Formula support hashes
the complete sealed tree before copying and again afterward, and hashes the
Formula-owned materialization before returning it. A changed response,
post-seal mutation, incomplete copy, or ownership/mode drift therefore fails
rather than reaching `prefix.install`.

Changing recipe inputs requires updating `recipe.json` and its Formula literal;
the resulting Formula SHA-256 invalidates reuse for that Formula only.

Formula tests must execute produced Wasm through Kandelo. Formula `version`
plus `revision` defines the Homebrew package version; a bottle `rebuild`
distinguishes a new bottle for that same package version. A retry may keep that
identity only when its package source, Formula and support closure,
dependencies, target outputs, pinned Homebrew, and build environment remain
unchanged. Any input change that can change bottle bytes requires a new
supported Formula revision or bottle rebuild. Never replace bytes under an
existing package-version, rebuild, and architecture identity.

The trusted publisher owns the complete generated `bottle do` block, including
its `root_url`, `rebuild`, tags, and hashes. Do not add placeholders, reuse
cross-ABI hashes, or hand-edit a generated block. See the
[authoritative bottle-repeatability contract](https://github.com/Automattic/kandelo/blob/main/docs/homebrew-publishing.md#retained-receipt-bottle-repeatability)
for the exact immutable-input boundary and revision rules.

Registry-bridged build scripts must declare every native tool they execute.
Ordinary native Formula dependencies remain direct build dependencies. The
publisher-only Binaryen, pkgconf, and WABT tools instead use the closed
`KandeloFormulaSupport::{Binaryen,Pkgconf,Wabt}Requirement` allowlist. A tool
used only while building uses `:build`; a tool also used by `test do` uses
`[:build, :test]`. Never use `:test` alone: pinned Homebrew would retain that
Requirement while pouring a bottle and make the Kandelo guest resolve a host
tool it cannot run. The trusted publisher statically binds each Requirement to
one `homebrew/core` Formula and sentinel executable, then exposes only that
sealed native tool. Adding another Requirement therefore requires a matching
publisher-contract change; it is not a tap-local escape hatch. For example,
Ruby declares `rust` as an ordinary build dependency and WABT through the
allowlisted Requirement because its bridge resolves the host target with
`rustc`, builds `wasm-local-root-spill` with `cargo` and `rustc` inside
caller-owned scratch space, and inspects the result with `wasm-objdump`.

Closed recipes must not call `kandelo_host_tool`, Nix, `scripts/dev-shell.sh`,
or a Cargo fallback in the Kandelo source tree. Declare ordinary host tools as
unqualified Homebrew `:build` dependencies; Formula support sends their exact
versioned keg roots to the runner, which checks the recipe `PATH` against those
kegs and the publisher's sealed native requirements. Fork instrumentation and
local-root spilling use the publisher's prebuilt, root-owned
`WASM_POSIX_FORK_INSTRUMENT` and `WASM_POSIX_LOCAL_ROOT_SPILL` executables from
the minimal platform projection.

Final linked programs must declare the WABT and Binaryen Requirements with
`:build` and call `kandelo_validate_wasm_artifact` after their last optimizer
or fork instrumentation transform and before installation. WABT reads the
export surface; Binaryen is the fallback disassembler for opcodes WABT cannot
yet decode. Use `fork: :required` for programs that must carry the complete
continuation interface, `fork: :forbidden` for programs that must remain
fork-free, and the default `:auto` only when the program's imported fork
surface is authoritative. The validator rejects ABI mismatches, legacy
Asyncify, incoherent fork imports and exports, and embedded staging or
host-workspace paths.
