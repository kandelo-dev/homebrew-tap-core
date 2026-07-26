#!/usr/bin/env bash
set -euo pipefail

# Build Kandelo's compact POSIX utility set for wasm32-posix-kernel.
# Output: WASM_POSIX_DEP_OUT_DIR/<utility>.wasm.

SOURCE_ROOT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
SRC="$SOURCE_ROOT/packages/registry/posix-utils-lite/src/posix-utils-lite.c"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
SYSROOT="${WASM_POSIX_SYSROOT:?}"

[ "${WASM_POSIX_DEP_TARGET_ARCH:?}" = "wasm32" ] || {
    echo "ERROR: posix-utils-lite is currently packaged for wasm32 only" >&2
    exit 2
}
[ "${WASM_POSIX_DEP_VERSION:?}" = "0.1.0" ] || {
    echo "ERROR: posix-utils-lite source version differs from the reviewed recipe" >&2
    exit 2
}

if [ ! -f "$SRC" ] || [ -L "$SRC" ]; then
  echo "ERROR: posix-utils-lite source must be a regular file: $SRC" >&2
  exit 1
fi

UTILITIES=(
  ar asa cal cflow compress ctags cxref ed ex fuser gencat getconf gettext
  iconv ipcrm ipcs lex locale logger man more msgfmt ngettext nm patch pax
  pgrep ps renice strings strip uncompress uudecode uuencode what xgettext
  yacc
)

if ! command -v wasm32posix-cc &>/dev/null; then
    echo "ERROR: wasm32posix-cc not found. Run 'npm link' in sdk/ first." >&2
    exit 1
fi

if [ ! -f "$SYSROOT/lib/libc.a" ]; then
    echo "ERROR: sysroot not found. Run: bash build.sh && bash scripts/build-musl.sh" >&2
    exit 1
fi

export WASM_POSIX_SYSROOT="$SYSROOT"

echo "==> Building posix-utils-lite multicall binary..."
wasm32posix-cc \
    -std=c11 \
    -D_POSIX_C_SOURCE=200809L \
    -O2 \
    -Wall \
    -Wextra \
    -Wno-unused-parameter \
    "$SRC" \
    -o "$OUT_DIR/ar.wasm"

for utility in "${UTILITIES[@]:1}"; do
    cp "$OUT_DIR/ar.wasm" "$OUT_DIR/$utility.wasm"
done

echo "==> posix-utils-lite built successfully."
