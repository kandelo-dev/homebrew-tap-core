#!/usr/bin/env bash
# Build the in-tree lsof.c (a small /proc reader) for wasm32-posix-kernel.
# Source: examples/lsof.c.  Not the upstream lsof — this is a minimal
# implementation tailored to this kernel's procfs.

set -euo pipefail

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

SYSROOT="${WASM_POSIX_SYSROOT:?}"
WASM_OPT="$(command -v wasm-opt)"

if ! command -v wasm32posix-cc >/dev/null 2>&1; then
    echo "ERROR: wasm32posix-cc is unavailable in the sealed SDK." >&2
    exit 1
fi

if [ ! -f "$SYSROOT/lib/libc.a" ]; then
    echo "ERROR: sysroot not found at $SYSROOT. Run scripts/build-musl.sh first." >&2
    exit 1
fi

echo "==> Building lsof.wasm from $SRC"
# WHY: the SDK wrapper owns Kandelo's target, sysroot, compiler glue,
# linker selection, ABI exports, and shared-memory contract. Copying those
# flags here let this one Formula drift from every other SDK consumer.
wasm32posix-cc -O2 "$SRC" -o "$OUT_BIN"

"$WASM_OPT" -O2 "$OUT_BIN" -o "$OUT_BIN"

ls -lh "$OUT_BIN"
echo "==> lsof built successfully!"
