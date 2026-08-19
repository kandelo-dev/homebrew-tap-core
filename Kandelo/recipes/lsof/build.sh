#!/usr/bin/env bash
set -euo pipefail

RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
if [ "${WASM_POSIX_DEP_NAME:?}" != "lsof" ] ||
   [ "${WASM_POSIX_DEP_VERSION:?}" != "0.1.0" ] ||
   [ "${WASM_POSIX_DEP_SOURCE_URL:?}" != "https://github.com/Automattic/kandelo/archive/1a83af5de608c10f485082c6ef0efa845f747436.tar.gz" ] ||
   [ "${WASM_POSIX_DEP_SOURCE_SHA256:?}" != "07e7a7ebff8003114f6b4bef1ccdc2e9b15ecfbd5e6ccc3bf8563107b8151fde" ] ||
   [ "${WASM_POSIX_DEP_TARGET_ARCH:?}" != "wasm32" ]; then
    echo "ERROR: lsof Formula identity differs from the reviewed tap recipe" >&2
    exit 2
fi

SOURCE="$RECIPE_DIR/src/lsof.c"
if [ ! -f "$SOURCE" ] || [ -L "$SOURCE" ]; then
    echo "ERROR: lsof source is not a direct regular file: $SOURCE" >&2
    exit 2
fi
mkdir -p "$WORK_DIR" "$OUT_DIR"
wasm32posix-cc -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -Wall -Wextra \
    "$SOURCE" -o "$WORK_DIR/lsof.wasm"
install -m 0755 "$WORK_DIR/lsof.wasm" "$OUT_DIR/lsof.wasm"
