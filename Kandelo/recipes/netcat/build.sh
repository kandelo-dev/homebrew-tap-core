#!/usr/bin/env bash
set -euo pipefail

# Build GNU Netcat 0.7.1 for wasm32-posix-kernel.

SCRIPT_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
NETCAT_VERSION="${WASM_POSIX_DEP_VERSION:?}"
SOURCE_URL="${WASM_POSIX_DEP_SOURCE_URL:?}"
SOURCE_SHA256="${WASM_POSIX_DEP_SOURCE_SHA256:?}"
TARGET_ARCH="${WASM_POSIX_DEP_TARGET_ARCH:?}"
SOURCE_INPUT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:?}"
SRC_DIR="$WORK_DIR/netcat-source"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
SYSROOT="${WASM_POSIX_SYSROOT:?}"

if [ "$TARGET_ARCH" != "wasm32" ]; then
    echo "ERROR: GNU Netcat is currently packaged for wasm32 only, got $TARGET_ARCH" >&2
    exit 2
fi
[ "$NETCAT_VERSION" = "0.7.1" ] &&
    [ "$SOURCE_URL" = "https://downloads.sourceforge.net/project/netcat/netcat/0.7.1/netcat-0.7.1.tar.gz" ] &&
    [ "$SOURCE_SHA256" = "30719c9a4ffbcf15676b8f528233ccc54ee6cba96cb4590975f5fd60c68a066f" ] || {
    echo "ERROR: GNU Netcat source identity differs from the reviewed recipe" >&2
    exit 2
}

if ! command -v wasm32posix-cc &>/dev/null; then
    echo "ERROR: wasm32posix-cc not found after sourcing sdk/activate.sh." >&2
    exit 1
fi

if ! command -v automake &>/dev/null; then
    echo "ERROR: automake not found in the Kandelo dev shell." >&2
    exit 1
fi

if [ ! -f "$SYSROOT/lib/libc.a" ]; then
    echo "ERROR: sysroot not found. Run: bash scripts/build-musl.sh" >&2
    exit 1
fi

export WASM_POSIX_SYSROOT="$SYSROOT"
# WHY: the publisher projects only the sealed glue directory required by the
# SDK; a closed recipe must not depend on the broader Kandelo checkout path.
: "${WASM_POSIX_GLUE_DIR:?}"

mkdir "$SRC_DIR"
cp -a "$SOURCE_INPUT/." "$SRC_DIR/"
# WHY: upstream config helpers, patches, configure, and make all write in
# tree. Keep the authenticated source sealed and grant writes only to this
# private copy. -P prevents chmod from following source-tree symlinks.
find -P "$SRC_DIR" -type d -exec chmod u+rwx {} +
find -P "$SRC_DIR" -type f -exec chmod u+rw {} +
cd "$SRC_DIR"

# GNU Netcat 0.7.1 predates Wasm and musl target tuples. Use the canonical
# helpers from the declared Automake build input so configure can validate the
# SDK's truthful wasm*-unknown-linux-musl host identity on every POSIX builder.
AUTOMAKE_AUX_DIR="$(automake --print-libdir)"
for auxiliary in config.sub config.guess; do
    source_auxiliary="$AUTOMAKE_AUX_DIR/$auxiliary"
    if [ ! -f "$source_auxiliary" ] || [ -L "$source_auxiliary" ]; then
        echo "ERROR: Automake support file is unavailable: $source_auxiliary" >&2
        exit 1
    fi
    install -m 0755 "$source_auxiliary" "$SRC_DIR/$auxiliary"
done

PATCH_SET=(
    "listen-success-exit.patch"
    "udp-listen-single-socket.patch"
    "disable-pktinfo.patch"
    "disable-abortive-linger.patch"
)
echo "==> Verifying Kandelo netcat portability patches..."
for patch_name in "${PATCH_SET[@]}"; do
    patch_file="$SCRIPT_DIR/patches/$patch_name"
    if gpatch --reverse --dry-run -p1 < "$patch_file" >/dev/null 2>&1; then
        echo "    $patch_name already applied"
    elif gpatch --forward --dry-run -p1 < "$patch_file" >/dev/null 2>&1; then
        gpatch -p1 < "$patch_file"
    else
        echo "ERROR: $patch_name does not apply and is not already present" >&2
        exit 1
    fi
done

if ! awk '
    /if \(netcat_mode == NETCAT_LISTEN\)/ { in_listen = 1; next }
    in_listen && /glob_ret = EXIT_SUCCESS;/ { ok = 1; exit }
    in_listen && /if \(opt_exec\)/ { exit }
    END { exit ok ? 0 : 1 }
' src/netcat.c; then
    echo "ERROR: listen-success-exit.patch is missing from src/netcat.c" >&2
    exit 1
fi

udp_marker_count=$(grep -c "Kandelo exposes normal POSIX UDP sockets" src/core.c || true)
if [ "$udp_marker_count" -ne 1 ]; then
    echo "ERROR: udp-listen-single-socket.patch marker count is $udp_marker_count, expected 1" >&2
    exit 1
fi

if ! grep -q "/\\* #  define USE_PKTINFO \\*/" src/netcat.h; then
    echo "ERROR: disable-pktinfo.patch is missing from src/netcat.h" >&2
    exit 1
fi

if ! grep -q "Kandelo cannot yet model abortive SO_LINGER" src/network.c; then
    echo "ERROR: disable-abortive-linger.patch is missing from src/network.c" >&2
    exit 1
fi

if [ ! -f Makefile ]; then
    echo "==> Configuring GNU Netcat for wasm32..."
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

    wasm32posix-configure \
        --disable-nls \
        --without-included-gettext \
        2>&1 | tail -40
fi

echo "==> Building GNU Netcat..."
rm -f "$SRC_DIR/src/netcat" "$SRC_DIR/src/"*.o
make -j"$(sysctl -n hw.ncpu 2>/dev/null || nproc)" 2>&1 | tail -30

NETCAT_BIN="$SRC_DIR/src/netcat"
if [ ! -f "$NETCAT_BIN" ]; then
    echo "ERROR: netcat binary not found after build" >&2
    exit 1
fi

cp "$NETCAT_BIN" "$OUT_DIR/nc.wasm"
ls -lh "$OUT_DIR/nc.wasm"
