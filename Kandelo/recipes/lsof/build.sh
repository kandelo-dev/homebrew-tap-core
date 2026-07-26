#!/usr/bin/env bash
# Build the in-tree lsof.c (a small /proc reader) for wasm32-posix-kernel.
# Source: examples/lsof.c.  Not the upstream lsof — this is a minimal
# implementation tailored to this kernel's procfs.

set -euo pipefail

REPO_ROOT="${HOMEBREW_KANDELO_ROOT:?}"
SOURCE_ROOT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
SRC="$SOURCE_ROOT/examples/lsof.c"
OUT_BIN="${WASM_POSIX_DEP_OUT_DIR:?}/lsof.wasm"

[ "${WASM_POSIX_DEP_TARGET_ARCH:?}" = "wasm32" ] || {
    echo "ERROR: lsof is currently packaged for wasm32 only" >&2
    exit 2
}
[ "${WASM_POSIX_DEP_VERSION:?}" = "0.1.0" ] || {
    echo "ERROR: lsof source version differs from the reviewed recipe" >&2
    exit 2
}

if [ ! -f "$SRC" ] || [ -L "$SRC" ]; then
    echo "ERROR: lsof source must be a regular file: $SRC" >&2
    exit 1
fi

# Match scripts/build-programs.sh CC + flags so the resulting wasm is
# binary-compatible with everything else in the release.
SYSROOT="${WASM_POSIX_SYSROOT:?}"
GLUE_DIR="$REPO_ROOT/libc/glue"

find_llvm_bin() {
    if [ -n "${LLVM_BIN:-}" ] && [ -x "$LLVM_BIN/clang" ]; then
        echo "$LLVM_BIN"
        return
    fi
    if [ -n "${LLVM_PREFIX:-}" ] && [ -x "$LLVM_PREFIX/bin/clang" ]; then
        echo "$LLVM_PREFIX/bin"
        return
    fi
    if command -v clang >/dev/null 2>&1; then
        dirname "$(command -v clang)"
        return
    fi
    echo "Error: LLVM/clang not found. Run scripts/dev-shell.sh or set LLVM_BIN/LLVM_PREFIX." >&2
    exit 1
}

LLVM_BIN="$(find_llvm_bin)"
CC="$LLVM_BIN/clang"
WASM_OPT="$(command -v wasm-opt)"

if [ ! -f "$SYSROOT/lib/libc.a" ]; then
    echo "ERROR: sysroot not found at $SYSROOT. Run scripts/build-musl.sh first." >&2
    exit 1
fi

CFLAGS=(
    --target=wasm32-unknown-unknown
    --sysroot="$SYSROOT"
    -nostdlib
    -O2
    -matomics -mbulk-memory
    -fno-trapping-math
    -mllvm -wasm-enable-sjlj
    -mllvm -wasm-use-legacy-eh=false
)

LINK_FLAGS=(
    "$GLUE_DIR/channel_syscall.c"
    "$GLUE_DIR/compiler_rt.c"
    "$SYSROOT/lib/crt1.o"
    "$SYSROOT/lib/libc.a"
    -Wl,--entry=_start
    -Wl,--export=_start
    -Wl,--import-memory
    -Wl,--shared-memory
    -Wl,--max-memory=1073741824
    -Wl,--allow-undefined
    -Wl,--table-base=3
    -Wl,--export-table
    -Wl,--growable-table
    -Wl,--export=__wasm_init_tls
    -Wl,--export=__tls_base
    -Wl,--export=__tls_size
    -Wl,--export=__tls_align
    -Wl,--export=__stack_pointer
    -Wl,--export=__wasm_thread_init
    -Wl,--export=__abi_version
)

echo "==> Building lsof.wasm from $SRC"
"$CC" "${CFLAGS[@]}" "$SRC" "${LINK_FLAGS[@]}" -o "$OUT_BIN"

"$WASM_OPT" -O2 "$OUT_BIN" -o "$OUT_BIN"

ls -lh "$OUT_BIN"
echo "==> lsof built successfully!"
