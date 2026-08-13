#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
WORK_ROOT="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_ROOT="${WASM_POSIX_DEP_OUT_DIR:?}"
PACKAGE_NAME="${WASM_POSIX_DEP_NAME:?}"
PACKAGE_VERSION="${WASM_POSIX_DEP_VERSION:?}"
TARGET_ARCH="${WASM_POSIX_DEP_TARGET_ARCH:?}"
SOURCE_URL="${WASM_POSIX_DEP_SOURCE_URL:?}"
SOURCE_SHA256="${WASM_POSIX_DEP_SOURCE_SHA256:?}"
SOURCE_COMMIT="5669d27fa171ad1bccf50031914dc6d997666276"
SOURCE_PATH="programs/login.c"
EXPECTED_SOURCE_URL="https://github.com/Automattic/kandelo/archive/${SOURCE_COMMIT}.tar.gz"
EXPECTED_SOURCE_SHA256="af0984c5312b6396e86e62910342a0e23cd4c8822353b3d58787d8f071a7b6f4"

if [ "$PACKAGE_NAME" != "login" ] || [ "$PACKAGE_VERSION" != "0.1.0" ] ||
   [ "$TARGET_ARCH" != "wasm32" ] || [ "$SOURCE_URL" != "$EXPECTED_SOURCE_URL" ] ||
   [ "$SOURCE_SHA256" != "$EXPECTED_SOURCE_SHA256" ]; then
    echo "login: Formula identity differs from the reviewed tap recipe" >&2
    exit 2
fi

SOURCE_FILE="$SOURCE_ROOT/$SOURCE_PATH"
if [ ! -f "$SOURCE_FILE" ] || [ -L "$SOURCE_FILE" ]; then
    echo "login: exact source file is unavailable: $SOURCE_PATH" >&2
    exit 2
fi
if [ ! -f "${WASM_POSIX_SYSROOT:?}/lib/libc.a" ]; then
    echo "login: Kandelo sysroot is incomplete" >&2
    exit 2
fi
for tool in wasm32posix-cc; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "login: required SDK tool is unavailable: $tool" >&2
        exit 2
    }
done

BUILD_ROOT="$WORK_ROOT/login"
if [ -e "$BUILD_ROOT" ] || [ -L "$BUILD_ROOT" ]; then
    echo "login: private build root is already occupied" >&2
    exit 2
fi
mkdir -m 0700 "$BUILD_ROOT"
mkdir -p "$OUT_ROOT"
wasm32posix-cc \
    -std=c11 \
    -O2 \
    -Wall \
    -Wextra \
    -c \
    "$SOURCE_FILE" \
    -o "$BUILD_ROOT/login.o"
wasm32posix-cc \
    -D_GNU_SOURCE \
    "$BUILD_ROOT/login.o" \
    -o "$BUILD_ROOT/login.wasm"
install -m 0755 "$BUILD_ROOT/login.wasm" "$OUT_ROOT/login.wasm"
