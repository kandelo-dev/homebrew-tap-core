#!/usr/bin/env bash
set -euo pipefail

RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
SOURCE_INPUT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
CHOCOLATE_INPUT="${WASM_POSIX_DEP_RESOURCE_CHOCOLATE_DOOM_DIR:?}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
if [ "${WASM_POSIX_DEP_NAME:?}" != "fbdoom" ] ||
   [ "${WASM_POSIX_DEP_VERSION:?}" != "0.1.0" ] ||
   [ "${WASM_POSIX_DEP_SOURCE_URL:?}" != "https://github.com/maximevince/fbDOOM/archive/17280163bc95e5d954d2efaa0633489b763b4cd1.tar.gz" ] ||
   [ "${WASM_POSIX_DEP_SOURCE_SHA256:?}" != "77f57cee68fed438dffdba96f6070b8975c16652a63ddf4fb967994e5585a38a" ] ||
   [ "${WASM_POSIX_DEP_TARGET_ARCH:?}" != "wasm32" ]; then
    echo "ERROR: fbdoom Formula identity differs from the reviewed tap recipe" >&2
    exit 2
fi

SRC_DIR="$WORK_DIR/fbdoom-src"
CHOCOLATE_DIR="$WORK_DIR/chocolate-doom-src"
mkdir -p "$WORK_DIR" "$OUT_DIR" "$SRC_DIR" "$CHOCOLATE_DIR"
cp -a --no-preserve=ownership "$SOURCE_INPUT/." "$SRC_DIR/"
cp -a --no-preserve=ownership "$CHOCOLATE_INPUT/." "$CHOCOLATE_DIR/"
find -P "$SRC_DIR" "$CHOCOLATE_DIR" -type d -exec chmod u+rwx {} +
find -P "$SRC_DIR" "$CHOCOLATE_DIR" -type f -exec chmod u+rw {} +

mkdir -p "$SRC_DIR/fbdoom/opl"
for file in opl.c opl.h opl3.c opl3.h opl_internal.h opl_queue.c opl_queue.h; do
    cp "$CHOCOLATE_DIR/opl/$file" "$SRC_DIR/fbdoom/opl/$file"
done
for file in mus2mid.c mus2mid.h midifile.c midifile.h; do
    cp "$CHOCOLATE_DIR/src/$file" "$SRC_DIR/fbdoom/$file"
done
for patch_file in "$RECIPE_DIR"/patches/*.patch; do
    git -C "$SRC_DIR" apply --check "$patch_file"
    git -C "$SRC_DIR" apply "$patch_file"
done

cd "$SRC_DIR/fbdoom"
make clean || true
make CC=wasm32posix-cc LD=wasm32posix-cc \
    CFLAGS="-O2 -DNORMALUNIX -DLINUX -D_DEFAULT_SOURCE -Iopl" \
    LDFLAGS="" LIBS="-lm" NOSDL=1
[ -f fbdoom ] || { echo "ERROR: fbdoom build did not produce fbdoom" >&2; exit 2; }
install -m 0755 fbdoom "$OUT_DIR/fbdoom.wasm"
