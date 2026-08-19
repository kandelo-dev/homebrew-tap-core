#!/usr/bin/env bash
set -euo pipefail

RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
SOURCE_INPUT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
if [ "${WASM_POSIX_DEP_NAME:?}" != "bc" ] ||
   [ "${WASM_POSIX_DEP_VERSION:?}" != "1.07.1" ] ||
   [ "${WASM_POSIX_DEP_SOURCE_URL:?}" != "https://ftpmirror.gnu.org/gnu/bc/bc-1.07.1.tar.gz" ] ||
   [ "${WASM_POSIX_DEP_SOURCE_SHA256:?}" != "62adfca89b0a1c0164c2cdca59ca210c1d44c3ffc46daf9931cf4942664cb02a" ] ||
   [ "${WASM_POSIX_DEP_TARGET_ARCH:?}" != "wasm32" ]; then
    echo "ERROR: bc Formula identity differs from the reviewed tap recipe" >&2
    exit 2
fi

SRC_DIR="$WORK_DIR/bc-src"
HOST_BUILD_DIR="$WORK_DIR/bc-host-build"
LIBMATH_HEADER="$WORK_DIR/libmath.h"
mkdir -p "$WORK_DIR" "$OUT_DIR"
[ ! -e "$SRC_DIR" ] || { echo "ERROR: private bc source already exists" >&2; exit 2; }
mkdir -p "$SRC_DIR"
cp -a --no-preserve=ownership "$SOURCE_INPUT/." "$SRC_DIR/"
find -P "$SRC_DIR" -type d -exec chmod u+rwx {} +
find -P "$SRC_DIR" -type f -exec chmod u+rw {} +

mkdir -p "$HOST_BUILD_DIR"
cp -a --no-preserve=ownership "$SRC_DIR/." "$HOST_BUILD_DIR/"
(
    cd "$HOST_BUILD_DIR"
    ./configure --with-readline=no
    cp "$RECIPE_DIR/fix-libmath-h.py" bc/fix-libmath_h
    chmod 0755 bc/fix-libmath_h
    make -j"${WASM_POSIX_BUILD_JOBS:-2}"
)
cp "$HOST_BUILD_DIR/bc/libmath.h" "$LIBMATH_HEADER"

cd "$SRC_DIR"
export ac_cv_func_malloc_0_nonnull=yes
export ac_cv_func_realloc_0_nonnull=yes
export ac_cv_func_calloc_0_nonnull=yes
export ac_cv_func_strerror_r=yes
export ac_cv_func_strerror_r_char_p=no
export ac_cv_have_decl_strerror_r=yes
export ac_cv_sizeof_long=4
export ac_cv_sizeof_long_long=8
export ac_cv_sizeof_unsigned_long=4
export ac_cv_sizeof_int=4
export ac_cv_sizeof_size_t=4
wasm32posix-configure --with-readline=no
cp "$LIBMATH_HEADER" bc/libmath.h
sed -i.bak '/^libmath\.h:/,/rm -f \.\/fbc/c\
libmath.h: libmath.b\
	@echo "Using pre-generated libmath.h (cross-compilation)"' bc/Makefile
make -j"${WASM_POSIX_BUILD_JOBS:-2}"
[ -f bc/bc ] || { echo "ERROR: bc build did not produce bc/bc" >&2; exit 2; }
install -m 0755 bc/bc "$OUT_DIR/bc.wasm"
