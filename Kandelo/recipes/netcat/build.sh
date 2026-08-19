#!/usr/bin/env bash
set -euo pipefail

RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
SOURCE_INPUT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
if [ "${WASM_POSIX_DEP_NAME:?}" != "netcat" ] ||
   [ "${WASM_POSIX_DEP_VERSION:?}" != "0.7.1" ] ||
   [ "${WASM_POSIX_DEP_SOURCE_URL:?}" != "https://downloads.sourceforge.net/project/netcat/netcat/0.7.1/netcat-0.7.1.tar.gz" ] ||
   [ "${WASM_POSIX_DEP_SOURCE_SHA256:?}" != "30719c9a4ffbcf15676b8f528233ccc54ee6cba96cb4590975f5fd60c68a066f" ] ||
   [ "${WASM_POSIX_DEP_TARGET_ARCH:?}" != "wasm32" ]; then
    echo "ERROR: netcat Formula identity differs from the reviewed tap recipe" >&2
    exit 2
fi

SRC_DIR="$WORK_DIR/netcat-src"
mkdir -p "$WORK_DIR" "$OUT_DIR"
[ ! -e "$SRC_DIR" ] || { echo "ERROR: private netcat source already exists" >&2; exit 2; }
mkdir -p "$SRC_DIR"
cp -a --no-preserve=ownership "$SOURCE_INPUT/." "$SRC_DIR/"
find -P "$SRC_DIR" -type d -exec chmod u+rwx {} +
find -P "$SRC_DIR" -type f -exec chmod u+rw {} +

AUTOMAKE_BIN="$(command -v automake)"
case "$AUTOMAKE_BIN" in /*) ;; *) echo "ERROR: automake path is not absolute" >&2; exit 2;; esac
AUTOMAKE_PREFIX="$(cd "$(dirname "$AUTOMAKE_BIN")/.." && pwd -P)"
automake_aux_dirs=("$AUTOMAKE_PREFIX"/share/automake-*)
[ "${#automake_aux_dirs[@]}" -eq 1 ] && [ -d "${automake_aux_dirs[0]}" ] || {
    echo "ERROR: expected one Automake support-data directory" >&2
    exit 2
}
for auxiliary in config.sub config.guess; do
    install -m 0755 "${automake_aux_dirs[0]}/$auxiliary" "$SRC_DIR/$auxiliary"
done

cd "$SRC_DIR"
for patch_file in "$RECIPE_DIR"/patches/*.patch; do
    patch --batch --forward -p1 < "$patch_file"
done
grep -q "Kandelo exposes normal POSIX UDP sockets" src/core.c
grep -q "/\* #  define USE_PKTINFO \*/" src/netcat.h
grep -q "Kandelo cannot yet model abortive SO_LINGER" src/network.c

export ac_cv_func_malloc_0_nonnull=yes
export ac_cv_func_realloc_0_nonnull=yes
export ac_cv_func_gethostbyname=yes
export ac_cv_func_getservbyname=yes
export ac_cv_func_getaddrinfo=yes
export ac_cv_func_inet_pton=yes
export ac_cv_func_select=yes
export ac_cv_header_resolv_h=no
export ac_cv_lib_resolv_main=no
export gl_cv_func_gettimeofday_clobber=no
wasm32posix-configure --disable-nls --without-included-gettext
make -j"${WASM_POSIX_BUILD_JOBS:-2}"
[ -f src/netcat ] || { echo "ERROR: netcat build did not produce src/netcat" >&2; exit 2; }
install -m 0755 src/netcat "$OUT_DIR/nc.wasm"
