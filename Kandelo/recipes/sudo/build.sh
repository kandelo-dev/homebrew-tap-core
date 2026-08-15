#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
RECIPE_ROOT="${WASM_POSIX_DEP_RECIPE_DIR:?}"
WORK_ROOT="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_ROOT="${WASM_POSIX_DEP_OUT_DIR:?}"
PACKAGE_NAME="${WASM_POSIX_DEP_NAME:?}"
PACKAGE_VERSION="${WASM_POSIX_DEP_VERSION:?}"
TARGET_ARCH="${WASM_POSIX_DEP_TARGET_ARCH:?}"
SOURCE_URL="${WASM_POSIX_DEP_SOURCE_URL:?}"
SOURCE_SHA256="${WASM_POSIX_DEP_SOURCE_SHA256:?}"
PATCH="${WASM_POSIX_DEP_PATCH:?}"
MAKE="${WASM_POSIX_DEP_MAKE:?}"
FORK_INSTRUMENT="${WASM_POSIX_FORK_INSTRUMENT:?}"
SDK_ROOT="$(cd "$(dirname "$(command -v wasm32posix-configure)")/.." && pwd)"
FORMULA_ROOT="$(dirname "$SOURCE_ROOT")"
EXPECTED_SOURCE_URL="https://github.com/sudo-project/sudo/archive/refs/tags/v1.9.17p2.tar.gz"
EXPECTED_SOURCE_SHA256="cabee23359afa698d147478c3a141437dbfecb510382e114eaf4b5087a1f8ca5"

if [ "$PACKAGE_NAME" != "sudo" ] || [ "$PACKAGE_VERSION" != "1.9.17p2" ] ||
   [ "$TARGET_ARCH" != "wasm32" ] || [ "$SOURCE_URL" != "$EXPECTED_SOURCE_URL" ] ||
   [ "$SOURCE_SHA256" != "$EXPECTED_SOURCE_SHA256" ]; then
    echo "sudo: Formula identity differs from the reviewed tap recipe" >&2
    exit 2
fi

MAIN_ENVP_PATCH="$RECIPE_ROOT/patches/wasm-main-envp.patch"
if [ ! -f "$MAIN_ENVP_PATCH" ] || [ -L "$MAIN_ENVP_PATCH" ]; then
    echo "sudo: reviewed compatibility patch is unavailable" >&2
    exit 2
fi
for input in "$PATCH" "$MAKE" "$FORK_INSTRUMENT"; do
    if [ ! -f "$input" ] || [ -L "$input" ] || [ ! -x "$input" ]; then
        echo "sudo: required recipe input is unavailable: $input" >&2
        exit 2
    fi
done
if [ ! -f "$SDK_ROOT/config.site" ] || [ -L "$SDK_ROOT/config.site" ]; then
    echo "sudo: SDK cross-compilation facts are unavailable" >&2
    exit 2
fi
if [ ! -d "$SOURCE_ROOT" ] || [ -L "$SOURCE_ROOT" ] ||
   [ ! -f "${WASM_POSIX_SYSROOT:?}/lib/libc.a" ]; then
    echo "sudo: source or Kandelo sysroot is incomplete" >&2
    exit 2
fi
for tool in wasm32posix-configure wasm-objdump; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "sudo: required build tool is unavailable: $tool" >&2
        exit 2
    }
done

SRC_DIR="$WORK_ROOT/sudo-source"
BUILD_DIR="$WORK_ROOT/sudo-build"
if [ -e "$SRC_DIR" ] || [ -L "$SRC_DIR" ] || [ -e "$BUILD_DIR" ] || [ -L "$BUILD_DIR" ]; then
    echo "sudo: private build roots are already occupied" >&2
    exit 2
fi
mkdir -m 0700 "$SRC_DIR" "$BUILD_DIR"
cp -R "$SOURCE_ROOT/." "$SRC_DIR/"
chmod -R u+w "$SRC_DIR"
"$PATCH" -d "$SRC_DIR" -p1 < "$MAIN_ENVP_PATCH"

(
    cd "$BUILD_DIR"
    export ac_cv_func_devname=no
    export ac_cv_func_freezero=no
    export ac_cv_func_getutsid=no
    export ac_cv_func_getutxid=yes
    export ac_cv_func__innetgr=no
    export ac_cv_func_innetgr=no
    export ac_cv_func_mkdtempat=no
    export ac_cv_func_mkostempsat=no
    export ac_cv_func_pw_dup=no
    export ac_cv_func_setgroupent=no
    export ac_cv_func_setpassent=no
    export ac_cv_func_sysctl=no
    export CONFIG_SITE="$SDK_ROOT/config.site"
    PREFIX_MAPS="-ffile-prefix-map=${FORMULA_ROOT}=/usr/src/sudo-1.9.17p2"
    PREFIX_MAPS+=" -fdebug-prefix-map=${FORMULA_ROOT}=/usr/src/sudo-1.9.17p2"
    PREFIX_MAPS+=" -fmacro-prefix-map=${FORMULA_ROOT}=/usr/src/sudo-1.9.17p2"

    "$SRC_DIR/configure" \
        --host=wasm32-unknown-none \
        --prefix=/usr \
        --sysconfdir=/etc \
        --localstatedir=/var \
        --runstatedir=/var/run \
        --disable-nls \
        --without-pam \
        --without-sendmail \
        --without-interfaces \
        --disable-log-server \
        --disable-log-client \
        --disable-shared-libutil \
        --enable-static-sudoers \
        --disable-shared \
        --enable-static \
        --disable-hardening \
        --disable-pie \
        --with-logging=file \
        --with-rundir=/var/run/sudo \
        --with-vardir=/var/run/sudo \
        --with-iologdir=/var/log/sudo-io \
        --with-secure-path-value=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        CC=wasm32posix-cc \
        CXX=wasm32posix-c++ \
        AR=wasm32posix-ar \
        RANLIB=wasm32posix-ranlib \
        NM=wasm32posix-nm \
        STRIP=wasm32posix-strip \
        CFLAGS="-O2 -D_GNU_SOURCE $PREFIX_MAPS"
    "$MAKE" -j2
)

mkdir -p "$OUT_ROOT"
while IFS=: read -r relative output; do
    source_path="$BUILD_DIR/$relative"
    artifact="$WORK_ROOT/$output"
    if [ ! -f "$source_path" ] || [ -L "$source_path" ]; then
        echo "sudo: expected build output is unavailable: $relative" >&2
        exit 1
    fi
    install -m 0755 "$source_path" "$artifact"
    if wasm-objdump -x "$artifact" | grep 'kernel_fork' >/dev/null; then
        instrumented="$WORK_ROOT/.${output}.instrumented"
        "$FORK_INSTRUMENT" "$artifact" -o "$instrumented"
        install -m 0755 "$instrumented" "$artifact"
    fi
    install -m 0755 "$artifact" "$OUT_ROOT/$output"
done <<'OUTPUTS'
src/sudo:sudo.wasm
plugins/sudoers/visudo:visudo.wasm
plugins/sudoers/cvtsudoers:cvtsudoers.wasm
plugins/sudoers/sudoreplay:sudoreplay.wasm
OUTPUTS
