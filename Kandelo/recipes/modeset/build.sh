#!/usr/bin/env bash
set -euo pipefail

RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
if [ "${WASM_POSIX_DEP_NAME:?}" != "modeset" ] ||
   [ "${WASM_POSIX_DEP_VERSION:?}" != "0.1.0" ] ||
   [ "${WASM_POSIX_DEP_SOURCE_URL:?}" != "https://github.com/Automattic/kandelo/archive/1a83af5de608c10f485082c6ef0efa845f747436.tar.gz" ] ||
   [ "${WASM_POSIX_DEP_SOURCE_SHA256:?}" != "07e7a7ebff8003114f6b4bef1ccdc2e9b15ecfbd5e6ccc3bf8563107b8151fde" ] ||
   [ "${WASM_POSIX_DEP_TARGET_ARCH:?}" != "wasm32" ]; then
    echo "ERROR: modeset Formula identity differs from the reviewed tap recipe" >&2
    exit 2
fi

SOURCE="$RECIPE_DIR/src/modeset.c"
if [ ! -f "$SOURCE" ] || [ -L "$SOURCE" ]; then
    echo "ERROR: modeset source is not a direct regular file: $SOURCE" >&2
    exit 2
fi
SYSROOT="${WASM_POSIX_SYSROOT:?}"
for library in libdrm.a libgbm.a libEGL.a libGLESv2.a; do
    [ -f "$SYSROOT/lib/$library" ] || {
        echo "ERROR: modeset sysroot library is missing: $library" >&2
        exit 2
    }
done
mkdir -p "$WORK_DIR" "$OUT_DIR"
read -r -a package_cflags <<<"$(wasm32posix-pkg-config --cflags libdrm gbm egl glesv2)"
read -r -a package_libs <<<"$(wasm32posix-pkg-config --libs gbm libdrm egl glesv2)"
wasm32posix-cc -std=c11 -O2 -Wall -Wextra -Wno-unused-parameter \
    -D_DEFAULT_SOURCE "${package_cflags[@]}" "$SOURCE" \
    "${package_libs[@]}" -lm -o "$WORK_DIR/modeset.wasm"
install -m 0755 "$WORK_DIR/modeset.wasm" "$OUT_DIR/modeset.wasm"
