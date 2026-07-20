# Formula Directory

This directory contains Kandelo's first-party Homebrew Formulae.

Formulae should use normal Homebrew DSL for source identity, dependencies,
patches, installation, bottles, and `test do`. Kandelo-specific SDK activation,
wasm cross-compilation, and kernel-backed tests belong in the shared
`KandeloFormulaSupport` mixin.

Simple packages build Homebrew's staged source directly in `install`. The
transitional `kandelo_build_package` registry-script bridge is reserved for
heavy ports whose build logic has not yet been decomposed, and each use remains
explicit migration debt.

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

Registry-bridged build scripts must declare every native tool they execute as a
direct build dependency. The sealed publisher removes ambient host tools and
retains only declared native dependency paths. For example, Ruby declares
`depends_on "rust" => :build` because its bridge resolves the host target with
`rustc` and builds `wasm-local-root-spill` with `cargo` and `rustc` inside
caller-owned scratch space.

Final linked programs must declare WABT and Binaryen as build dependencies and
call `kandelo_validate_wasm_artifact` after their last optimizer or fork
instrumentation transform and before installation. WABT reads the export
surface; Binaryen is the fallback disassembler for opcodes WABT cannot yet
decode. Use `fork: :required` for programs that must carry the complete
continuation interface, `fork: :forbidden` for programs that must remain
fork-free, and the default `:auto` only when the program's imported fork
surface is authoritative. The validator rejects ABI mismatches, legacy
Asyncify, incoherent fork imports and exports, and embedded staging or
host-workspace paths.
