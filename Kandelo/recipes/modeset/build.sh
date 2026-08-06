#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
MODESET_SOURCE="$SOURCE_ROOT/programs/modeset.c"
OUT_BIN="${WASM_POSIX_DEP_OUT_DIR:?}/modeset.wasm"

[ "${WASM_POSIX_DEP_TARGET_ARCH:?}" = "wasm32" ] || {
    echo "ERROR: modeset is currently packaged for wasm32 only" >&2
    exit 2
}
[ "${WASM_POSIX_DEP_VERSION:?}" = "0.1.0" ] || {
    echo "ERROR: modeset source version differs from the reviewed recipe" >&2
    exit 2
}

if [ ! -f "$MODESET_SOURCE" ] || [ -L "$MODESET_SOURCE" ]; then
    echo "ERROR: modeset source must be a regular file: $MODESET_SOURCE" >&2
    exit 1
fi

if [ ! -f "$WASM_POSIX_SYSROOT/lib/libdrm.a" ] ||
   [ ! -f "$WASM_POSIX_SYSROOT/lib/libgbm.a" ] ||
   [ ! -f "$WASM_POSIX_SYSROOT/lib/libEGL.a" ] ||
   [ ! -f "$WASM_POSIX_SYSROOT/lib/libGLESv2.a" ]; then
    echo "ERROR: DRI/EGL/GLES sysroot libraries are missing." >&2
    echo "Run: scripts/dev-shell.sh bash scripts/build-musl.sh" >&2
    exit 1
fi

PKG_CFLAGS="$(wasm32posix-pkg-config --cflags libdrm gbm egl glesv2)"
PKG_LIBS="$(wasm32posix-pkg-config --libs gbm libdrm egl glesv2)"

echo "==> Building modeset fluid simulation..."
wasm32posix-cc \
    -std=c11 \
    -O2 \
    -Wall \
    -Wextra \
    -Wno-unused-parameter \
    -D_DEFAULT_SOURCE \
    $PKG_CFLAGS \
    "$MODESET_SOURCE" \
    $PKG_LIBS \
    -lm \
    -o "$OUT_BIN"
