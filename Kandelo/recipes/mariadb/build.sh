#!/usr/bin/env bash
set -euo pipefail

# Build MariaDB 10.5 LTS for Kandelo.
#
# Two-step cross-compilation inside one sealed recipe:
#   1. Host build: generates import_executables.cmake (native helper programs)
#   2. Cross build: uses the Kandelo wasm32 CMake toolchain

: "${WASM_POSIX_DEP_SOURCE_DIR:?}"
: "${WASM_POSIX_DEP_WORK_DIR:?}"
: "${WASM_POSIX_DEP_OUT_DIR:?}"
: "${WASM_POSIX_DEP_RECIPE_DIR:?}"
: "${WASM_POSIX_DEP_TARGET_ARCH:?}"
: "${WASM_POSIX_DEP_LIBCXX_DIR:?}"
: "${WASM_POSIX_DEP_NCURSES_DIR:?}"
: "${WASM_POSIX_DEP_OPENSSL_DIR:?}"
: "${WASM_POSIX_DEP_PCRE2_DIR:?}"
: "${WASM_POSIX_DEP_ZLIB_DIR:?}"
: "${MARIADB_NATIVE_BISON_DIR:?}"
: "${MARIADB_NATIVE_CMAKE_DIR:?}"
: "${MARIADB_NATIVE_LLVM_DIR:?}"
: "${MARIADB_NATIVE_MAKE_DIR:?}"
: "${WASM_POSIX_GLUE_DIR:?}"
: "${WASM_POSIX_LLVM_DIR:?}"
: "${WASM_POSIX_SYSROOT:?}"

: "${WASM_POSIX_DEP_VERSION:?}"
WASM_ARCH="$WASM_POSIX_DEP_TARGET_ARCH"
SRC_DIR="$WASM_POSIX_DEP_SOURCE_DIR"
WORK_DIR="$WASM_POSIX_DEP_WORK_DIR"
INSTALL_DIR="$WASM_POSIX_DEP_OUT_DIR"
GUEST_PREFIX="/home/linuxbrew/.linuxbrew/opt/mariadb"
RECIPE_DIR="$WASM_POSIX_DEP_RECIPE_DIR"
HOST_BUILD_DIR="$WORK_DIR/host-build"
GLUE_DIR="$WASM_POSIX_GLUE_DIR"
BASE_SYSROOT="${WASM_POSIX_SYSROOT:?}"
SYSROOT="$WORK_DIR/sysroot"
CROSS_BUILD_DIR="$WORK_DIR/cross-build"
TOOLCHAIN_FILE="$RECIPE_DIR/wasm32-posix-toolchain.cmake"
WASM_TARGET="wasm32-unknown-unknown"
TARGET_LLVM_BIN="$WASM_POSIX_LLVM_DIR"

case "$WASM_ARCH" in
    wasm32) ;;
    *)
        echo "ERROR: MariaDB requires the real ncurses dependency, currently published only for wasm32" >&2
        exit 1
        ;;
esac

# WHY: dependency headers and archives historically leaked into Kandelo's
# shared sysroot. A recipe-private copy preserves the SDK's complete libc
# contract while making every Formula dependency explicit and preventing one
# concurrent bottle build from changing another build's inputs.
mkdir -p "$SYSROOT"
cp -R "$BASE_SYSROOT/." "$SYSROOT/"
chmod -R u+w "$SYSROOT"
export WASM_POSIX_SYSROOT="$SYSROOT"

NPROC="$(sysctl -n hw.ncpu 2>/dev/null || nproc)"
HOST_HELPERS=(
    "$HOST_BUILD_DIR/extra/comp_err"
    "$HOST_BUILD_DIR/scripts/comp_sql"
    "$HOST_BUILD_DIR/dbug/factorial"
    "$HOST_BUILD_DIR/sql/gen_lex_hash"
    "$HOST_BUILD_DIR/sql/gen_lex_token"
)

host_helpers_ready() {
    [ -f "$HOST_BUILD_DIR/import_executables.cmake" ] || return 1
    local helper
    for helper in "${HOST_HELPERS[@]}"; do
        [ -x "$helper" ] || return 1
    done
}

# --- Verify sealed native and target prerequisites ---
if [ ! -f "$SYSROOT/lib/libc.a" ]; then
    echo "ERROR: Kandelo sysroot not found at $SYSROOT" >&2
    exit 1
fi

if [ ! -f "$TOOLCHAIN_FILE" ]; then
    echo "ERROR: Toolchain file not found at $TOOLCHAIN_FILE" >&2
    exit 1
fi

NATIVE_CMAKE="$MARIADB_NATIVE_CMAKE_DIR/bin/cmake"
NATIVE_BISON="$MARIADB_NATIVE_BISON_DIR/bin/bison"
NATIVE_CLANG="$MARIADB_NATIVE_LLVM_DIR/bin/clang"
NATIVE_CLANGXX="$MARIADB_NATIVE_LLVM_DIR/bin/clang++"
NATIVE_AR="$MARIADB_NATIVE_LLVM_DIR/bin/llvm-ar"
NATIVE_RANLIB="$MARIADB_NATIVE_LLVM_DIR/bin/llvm-ranlib"
if [ -x "$MARIADB_NATIVE_MAKE_DIR/bin/gmake" ]; then
    NATIVE_MAKE="$MARIADB_NATIVE_MAKE_DIR/bin/gmake"
else
    NATIVE_MAKE="$MARIADB_NATIVE_MAKE_DIR/bin/make"
fi
for tool in \
    "$NATIVE_CMAKE" "$NATIVE_BISON" "$NATIVE_CLANG" "$NATIVE_CLANGXX" \
    "$NATIVE_AR" "$NATIVE_RANLIB" "$NATIVE_MAKE"; do
    if [ ! -x "$tool" ]; then
        echo "ERROR: declared native build tool is unavailable: $tool" >&2
        exit 1
    fi
done

NATIVE_PATH="$MARIADB_NATIVE_CMAKE_DIR/bin:$MARIADB_NATIVE_BISON_DIR/bin:$MARIADB_NATIVE_LLVM_DIR/bin:$MARIADB_NATIVE_MAKE_DIR/bin:/usr/bin:/bin"

# WHY: the sealed runner activates the target SDK before invoking this recipe.
# MariaDB's generators must be native executables, so build them in a scrubbed
# subprocess that can see only declared native tool kegs and baseline host
# utilities. The target phase below retains the SDK environment.
native_env() {
    env \
        -u ACLOCAL_PATH -u AR -u AS -u CC -u CFLAGS -u CMAKE_PREFIX_PATH \
        -u CONFIG_SITE -u CPP -u CPPFLAGS -u CXX -u CXXFLAGS -u LD -u LDFLAGS \
        -u LIBS -u NM -u OBJCOPY -u OBJDUMP -u PKG_CONFIG -u PKG_CONFIG_LIBDIR \
        -u PKG_CONFIG_PATH -u PKG_CONFIG_SYSROOT_DIR -u RANLIB -u READELF \
        -u SIZE -u STRINGS -u STRIP -u LLVM_BIN -u WASM_POSIX_LLVM_DIR \
        -u WASM_POSIX_SYSROOT \
        PATH="$NATIVE_PATH" \
        "$@"
}

HOST_ARGS=(
    "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"
    "-DCMAKE_C_COMPILER=$NATIVE_CLANG"
    "-DCMAKE_CXX_COMPILER=$NATIVE_CLANGXX"
    "-DCMAKE_AR=$NATIVE_AR"
    "-DCMAKE_RANLIB=$NATIVE_RANLIB"
    "-DCMAKE_MAKE_PROGRAM=$NATIVE_MAKE"
    "-DBISON_EXECUTABLE=$NATIVE_BISON"
    "-DWITH_UNIT_TESTS=OFF"
    "-DWITH_MARIABACKUP=OFF"
    "-DPLUGIN_CONNECT=NO"
    "-DPLUGIN_ROCKSDB=NO"
    "-DPLUGIN_TOKUDB=NO"
    "-DPLUGIN_MROONGA=NO"
    "-DPLUGIN_SPIDER=NO"
    "-DPLUGIN_OQGRAPH=NO"
    "-DPLUGIN_PERFSCHEMA=NO"
    "-DPLUGIN_SPHINX=NO"
    "-DPLUGIN_COLUMNSTORE=NO"
    "-DPLUGIN_S3=NO"
    "-DPLUGIN_CRACKLIB_PASSWORD_CHECK=NO"
    "-DWITH_SSL=OFF"
    "-DCONC_WITH_SSL=OFF"
    "-DWITH_PCRE=bundled"
    "-DWITH_EDITLINE=bundled"
    "-DWITH_ZLIB=bundled"
)
native_env "$NATIVE_CMAKE" -S "$SRC_DIR" -B "$HOST_BUILD_DIR" "${HOST_ARGS[@]}"
native_env "$NATIVE_CMAKE" --build "$HOST_BUILD_DIR" \
    --target import_executables --parallel "$NPROC"
if ! host_helpers_ready; then
    echo "ERROR: sealed MariaDB host-helper build is incomplete at $HOST_BUILD_DIR" >&2
    exit 1
fi

# --- Populate the private sysroot from declared target Formulae ---
LLVM_CLANG="$TARGET_LLVM_BIN/clang"
if [ ! -x "$LLVM_CLANG" ]; then
    echo "ERROR: target clang not found at $LLVM_CLANG" >&2
    exit 1
fi

LIBCXX_PREFIX="$WASM_POSIX_DEP_LIBCXX_DIR"
NCURSES_PREFIX="$WASM_POSIX_DEP_NCURSES_DIR"
OPENSSL_PREFIX="$WASM_POSIX_DEP_OPENSSL_DIR"
PCRE2_PREFIX="$WASM_POSIX_DEP_PCRE2_DIR"
ZLIB_PREFIX="$WASM_POSIX_DEP_ZLIB_DIR"
[ -f "$LIBCXX_PREFIX/lib/libc++.a" ] || {
    echo "ERROR: libcxx Formula missing libc++.a at $LIBCXX_PREFIX" >&2
    exit 1
}
[ -f "$LIBCXX_PREFIX/lib/libc++abi.a" ] || {
    echo "ERROR: libcxx Formula missing libc++abi.a at $LIBCXX_PREFIX" >&2
    exit 1
}
[ -d "$LIBCXX_PREFIX/include/c++/v1" ] || {
    echo "ERROR: libcxx Formula missing include/c++/v1 at $LIBCXX_PREFIX" >&2
    exit 1
}
[ -f "$NCURSES_PREFIX/lib/libncursesw.a" ] || {
    echo "ERROR: ncurses Formula missing libncursesw.a at $NCURSES_PREFIX" >&2
    exit 1
}
[ -f "$NCURSES_PREFIX/lib/libtinfow.a" ] || {
    echo "ERROR: ncurses Formula missing libtinfow.a at $NCURSES_PREFIX" >&2
    exit 1
}
[ -f "$NCURSES_PREFIX/include/ncursesw/curses.h" ] || {
    echo "ERROR: ncurses Formula missing ncursesw/curses.h at $NCURSES_PREFIX" >&2
    exit 1
}
[ -f "$OPENSSL_PREFIX/lib/libssl.a" ] || {
    echo "ERROR: openssl Formula missing libssl.a at $OPENSSL_PREFIX" >&2
    exit 1
}
[ -f "$OPENSSL_PREFIX/lib/libcrypto.a" ] || {
    echo "ERROR: openssl Formula missing libcrypto.a at $OPENSSL_PREFIX" >&2
    exit 1
}
[ -f "$OPENSSL_PREFIX/include/openssl/ssl.h" ] || {
    echo "ERROR: openssl Formula missing openssl/ssl.h at $OPENSSL_PREFIX" >&2
    exit 1
}
[ -f "$PCRE2_PREFIX/lib/libpcre2-8.a" ] || {
    echo "ERROR: pcre2 Formula missing libpcre2-8.a at $PCRE2_PREFIX" >&2
    exit 1
}
[ -f "$PCRE2_PREFIX/lib/libpcre2-posix.a" ] || {
    echo "ERROR: pcre2 Formula missing libpcre2-posix.a at $PCRE2_PREFIX" >&2
    exit 1
}
[ -f "$PCRE2_PREFIX/include/pcre2.h" ] || {
    echo "ERROR: pcre2 Formula missing pcre2.h at $PCRE2_PREFIX" >&2
    exit 1
}
[ -f "$PCRE2_PREFIX/include/pcre2posix.h" ] || {
    echo "ERROR: pcre2 Formula missing pcre2posix.h at $PCRE2_PREFIX" >&2
    exit 1
}
[ -f "$ZLIB_PREFIX/lib/libz.a" ] || {
    echo "ERROR: zlib Formula missing libz.a at $ZLIB_PREFIX" >&2
    exit 1
}

mkdir -p "$SYSROOT/lib" "$SYSROOT/include/c++"
cp "$LIBCXX_PREFIX/lib/libc++.a" "$SYSROOT/lib/libc++.a"
cp "$LIBCXX_PREFIX/lib/libc++abi.a" "$SYSROOT/lib/libc++abi.a"
rm -rf "$SYSROOT/include/c++/v1"
cp -R "$LIBCXX_PREFIX/include/c++/v1" "$SYSROOT/include/c++/v1"
cp "$NCURSES_PREFIX/lib/libncursesw.a" "$SYSROOT/lib/libncursesw.a"
cp "$NCURSES_PREFIX/lib/libtinfow.a" "$SYSROOT/lib/libtinfow.a"
cp -R "$NCURSES_PREFIX/include/." "$SYSROOT/include/"
cp "$OPENSSL_PREFIX/lib/libssl.a" "$SYSROOT/lib/libssl.a"
cp "$OPENSSL_PREFIX/lib/libcrypto.a" "$SYSROOT/lib/libcrypto.a"
rm -rf "$SYSROOT/include/openssl"
cp -R "$OPENSSL_PREFIX/include/openssl" "$SYSROOT/include/openssl"
cp "$PCRE2_PREFIX/lib/libpcre2-8.a" "$SYSROOT/lib/libpcre2-8.a"
cp "$PCRE2_PREFIX/lib/libpcre2-posix.a" "$SYSROOT/lib/libpcre2-posix.a"
cp "$PCRE2_PREFIX/include/pcre2.h" "$SYSROOT/include/pcre2.h"
cp "$PCRE2_PREFIX/include/pcre2posix.h" "$SYSROOT/include/pcre2posix.h"
cp "$ZLIB_PREFIX/lib/libz.a" "$SYSROOT/lib/libz.a"
cp "$ZLIB_PREFIX/include/zlib.h" "$SYSROOT/include/zlib.h"
cp "$ZLIB_PREFIX/include/zconf.h" "$SYSROOT/include/zconf.h"

# --- Pre-compile glue objects ---
WASM_COMPILE_FLAGS=(
    "--target=$WASM_TARGET"
    -matomics
    -mbulk-memory
    -mexception-handling
    -mllvm
    -wasm-enable-sjlj
    -fno-trapping-math
    "--sysroot=$SYSROOT"
)

GLUE_OBJ_DIR="$WORK_DIR/glue-objs"
mkdir -p "$GLUE_OBJ_DIR"

NEED_GLUE_REBUILD=0
if [ ! -f "$GLUE_OBJ_DIR/channel_syscall.o" ]; then
    NEED_GLUE_REBUILD=1
elif [ "$GLUE_DIR/channel_syscall.c" -nt "$GLUE_OBJ_DIR/channel_syscall.o" ] || \
     [ "$GLUE_DIR/compiler_rt.c" -nt "$GLUE_OBJ_DIR/compiler_rt.o" ]; then
    NEED_GLUE_REBUILD=1
fi
if [ "$NEED_GLUE_REBUILD" = "1" ]; then
    echo "==> Compiling glue objects..."
    "$LLVM_CLANG" "${WASM_COMPILE_FLAGS[@]}" -O2 \
        -c "$GLUE_DIR/channel_syscall.c" -o "$GLUE_OBJ_DIR/channel_syscall.o"
    "$LLVM_CLANG" "${WASM_COMPILE_FLAGS[@]}" -O2 \
        -c "$GLUE_DIR/compiler_rt.c" -o "$GLUE_OBJ_DIR/compiler_rt.o"
    echo "==> Glue objects compiled."
fi
export MARIADB_GLUE_OBJ_DIR="$GLUE_OBJ_DIR"

# --- Step 2: Cross build ---
echo "==> Step 2: Cross build for $WASM_ARCH..."
mkdir -p "$CROSS_BUILD_DIR"
cd "$CROSS_BUILD_DIR"

export WASM_POSIX_SYSROOT="$SYSROOT"
export PATH="$NATIVE_PATH:$PATH"

# WHY: MariaDB records absolute source paths through __FILE__ and debug
# metadata even in release builds. Normalize every projected build root to a
# stable guest-independent identity before the final artifact guards run.
PREFIX_MAP_FLAGS=()
add_prefix_map() {
    local from="$1"
    local to="$2"
    PREFIX_MAP_FLAGS+=(
        "-ffile-prefix-map=$from=$to"
        "-fdebug-prefix-map=$from=$to"
        "-fmacro-prefix-map=$from=$to"
    )
}
add_prefix_map "$SRC_DIR" "/usr/src/mariadb"
add_prefix_map "$WORK_DIR" "/usr/src/mariadb-build"
add_prefix_map "$SYSROOT" "/usr/src/kandelo-sysroot"
add_prefix_map "$GLUE_DIR" "/usr/src/kandelo-glue"
add_prefix_map "$LIBCXX_PREFIX" "/usr/src/deps/libcxx"
add_prefix_map "$NCURSES_PREFIX" "/usr/src/deps/ncurses"
add_prefix_map "$OPENSSL_PREFIX" "/usr/src/deps/openssl"
add_prefix_map "$PCRE2_PREFIX" "/usr/src/deps/pcre2"
add_prefix_map "$ZLIB_PREFIX" "/usr/src/deps/zlib"
PREFIX_MAP_STRING="${PREFIX_MAP_FLAGS[*]}"

CONFIGURE_LOG="$CROSS_BUILD_DIR/configure.log"
if "$NATIVE_CMAKE" "$SRC_DIR" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN_FILE" \
    -DCMAKE_MAKE_PROGRAM="$NATIVE_MAKE" \
    -DBISON_EXECUTABLE="$NATIVE_BISON" \
    -DCMAKE_INSTALL_PREFIX="$GUEST_PREFIX" \
    -DINSTALL_MYSQLSHAREDIR=share/mysql \
    -DMYSQL_DATADIR=/home/linuxbrew/.linuxbrew/var/mysql \
    -DIMPORT_EXECUTABLES="$HOST_BUILD_DIR/import_executables.cmake" \
    \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_FLAGS_RELEASE="-O2 -DNDEBUG -gline-tables-only -fdebug-compilation-dir=/usr/src/mariadb $PREFIX_MAP_STRING" \
    -DCMAKE_CXX_FLAGS_RELEASE="-O2 -DNDEBUG -gline-tables-only -fdebug-compilation-dir=/usr/src/mariadb $PREFIX_MAP_STRING" \
    \
    -DWITH_UNIT_TESTS=OFF \
    -DWITH_MARIABACKUP=OFF \
    -DSECURITY_HARDENED=OFF \
    -DWITH_SAFEMALLOC=OFF \
    -DWITH_EMBEDDED_SERVER=OFF \
    -DENABLED_PROFILING=OFF \
    -DWITHOUT_DYNAMIC_PLUGIN=ON \
    -DDISABLE_SHARED=ON \
    \
    -DWITH_SSL=system \
    -DCONC_WITH_SSL=OPENSSL \
    -DOPENSSL_ROOT_DIR="$SYSROOT" \
    -DOPENSSL_USE_STATIC_LIBS=TRUE \
    -DOPENSSL_INCLUDE_DIR="$SYSROOT/include" \
    -DOPENSSL_SSL_LIBRARY="$SYSROOT/lib/libssl.a" \
    -DOPENSSL_CRYPTO_LIBRARY="$SYSROOT/lib/libcrypto.a" \
    -DWITH_PCRE=system \
    -DPCRE_INCLUDE_DIRS="$SYSROOT/include" \
    -DPCRE_LIBRARY_DIRS="$SYSROOT/lib" \
    -DHAVE_PCRE2_MATCH_8=1 \
    -DNEEDS_PCRE2_DEBIAN_HACK=FALSE \
    -DWITH_READLINE=ON \
    -DCURSES_FOUND=TRUE \
    -DCURSES_INCLUDE_PATH="$SYSROOT/include" \
    -DCURSES_INCLUDE_DIRS="$SYSROOT/include" \
    -DCURSES_LIBRARY="$SYSROOT/lib/libtinfow.a" \
    "-DCURSES_LIBRARIES=$SYSROOT/lib/libncursesw.a;$SYSROOT/lib/libtinfow.a" \
    -DCURSES_HAVE_CURSES_H=TRUE \
    -DHAVE_TPUTS_IN_CURSES=TRUE \
    -DHAVE_SETUPTERM=TRUE \
    -DHAVE_VIDATTR=TRUE \
    -DWITH_ZLIB=system \
    -DZLIB_INCLUDE_DIR="$SYSROOT/include" \
    -DZLIB_LIBRARY="$SYSROOT/lib/libz.a" \
    -DZLIB_LIBRARY_RELEASE="$SYSROOT/lib/libz.a" \
    -DZLIB_LIBRARY_DEBUG="$SYSROOT/lib/libz.a" \
    -DCMAKE_DISABLE_FIND_PACKAGE_PkgConfig=TRUE \
    -DWITH_SYSTEMD=no \
    -DWITH_WSREP=OFF \
    -DDISABLE_THREADPOOL=ON \
    \
    -DPLUGIN_INNODB=STATIC \
    -DPLUGIN_INNOBASE=STATIC \
    -DPLUGIN_XTRADB=NO \
    -DPLUGIN_CONNECT=NO \
    -DPLUGIN_ROCKSDB=NO \
    -DPLUGIN_TOKUDB=NO \
    -DPLUGIN_MROONGA=NO \
    -DPLUGIN_SPIDER=NO \
    -DPLUGIN_OQGRAPH=NO \
    -DPLUGIN_SPHINX=NO \
    -DPLUGIN_COLUMNSTORE=NO \
    -DPLUGIN_S3=NO \
    -DPLUGIN_PERFSCHEMA=NO \
    -DPLUGIN_CRACKLIB_PASSWORD_CHECK=NO \
    -DPLUGIN_AUTH_GSSAPI=NO \
    -DPLUGIN_AUTH_PAM=NO \
    -DPLUGIN_FEEDBACK=NO \
    -DPLUGIN_QUERY_RESPONSE_TIME=NO \
    -DPLUGIN_SERVER_AUDIT=NO \
    -DPLUGIN_DISKS=NO \
    -DPLUGIN_METADATA_LOCK_INFO=NO \
    -DPLUGIN_QUERY_CACHE_INFO=NO \
    -DPLUGIN_LOCALE_INFO=NO \
    -DPLUGIN_SIMPLE_PASSWORD_CHECK=NO \
    \
    -DPLUGIN_ARIA=STATIC \
    -DPLUGIN_MYISAM=STATIC \
    -DPLUGIN_MYISAMMRG=STATIC \
    -DPLUGIN_CSV=STATIC \
    -DPLUGIN_HEAP=STATIC \
    -DPLUGIN_PARTITION=STATIC \
    \
    -DSTACK_DIRECTION=-1 \
    -DHAVE_LLVM_LIBCPP=OFF \
    > "$CONFIGURE_LOG" 2>&1; then
    tail -40 "$CONFIGURE_LOG"
else
    echo "==> MariaDB CMake configuration failed:" >&2
    tail -100 "$CONFIGURE_LOG" >&2
    exit 1
fi

# WHY: MariaDB's CMake probes can silently select bundled or native libraries
# during a cross build. Assert the complete cache identity before compilation,
# then inspect each final link command below.
require_cache_value() {
    local key="$1"
    local expected="$2"
    local line
    line="$(grep -E "^${key}(:[^=]*)?=" CMakeCache.txt | tail -1 || true)"
    if [ "${line#*=}" != "$expected" ]; then
        echo "ERROR: MariaDB dependency drifted: $key=${line#*=}, expected $expected" >&2
        exit 1
    fi
}
require_cache_value WITH_SSL system
require_cache_value CONC_WITH_SSL OPENSSL
require_cache_value OPENSSL_ROOT_DIR "$SYSROOT"
require_cache_value OPENSSL_INCLUDE_DIR "$SYSROOT/include"
require_cache_value OPENSSL_SSL_LIBRARY "$SYSROOT/lib/libssl.a"
require_cache_value OPENSSL_CRYPTO_LIBRARY "$SYSROOT/lib/libcrypto.a"
require_cache_value WITH_PCRE system
require_cache_value PCRE_LIBRARY_DIRS "$SYSROOT/lib"
require_cache_value HAVE_PCRE2_MATCH_8 1
require_cache_value WITH_ZLIB system
require_cache_value ZLIB_LIBRARY_RELEASE "$SYSROOT/lib/libz.a"
require_cache_value CURSES_LIBRARY "$SYSROOT/lib/libtinfow.a"
if [ -d "$CROSS_BUILD_DIR/extra/wolfssl" ] ||
   [ -d "$CROSS_BUILD_DIR/extra/pcre2" ] ||
   [ -f "$CROSS_BUILD_DIR/zlib/CMakeFiles/zlib.dir/link.txt" ]; then
    echo "ERROR: MariaDB configured a bundled target dependency" >&2
    exit 1
fi

echo "==> CMake configuration complete. Starting build..."

# Build mysqld. Capture full output to a log so a failed build doesn't
# bury the actual diagnostic in `tail -N`. Show the tail on success and
# the relevant error context on failure.
MARIADBD_LOG="$CROSS_BUILD_DIR/build-mariadbd.log"
if "$NATIVE_MAKE" -j"$NPROC" mariadbd > "$MARIADBD_LOG" 2>&1; then
    tail -10 "$MARIADBD_LOG"
else
    echo "==> mariadbd build failed; printing error context:" >&2
    grep -B 2 -E "[Ee]rror|fatal|undefined" "$MARIADBD_LOG" | tail -50 >&2 || true
    echo "" >&2
    echo "Full log: $MARIADBD_LOG" >&2
    exit 1
fi

# Build mysqltest client (mariadb-test target)
echo "==> Building mysqltest..."
MYSQLTEST_LOG="$CROSS_BUILD_DIR/build-mysqltest.log"
if "$NATIVE_MAKE" -j"$NPROC" mariadb-test > "$MYSQLTEST_LOG" 2>&1; then
    tail -10 "$MYSQLTEST_LOG"
else
    echo "==> mariadb-test build failed; printing error context:" >&2
    grep -B 2 -E "[Ee]rror|fatal|undefined" "$MYSQLTEST_LOG" | tail -50 >&2 || true
    echo "" >&2
    echo "Full log: $MYSQLTEST_LOG" >&2
    exit 1
fi

# The cache proves configuration selection; the final commands prove that the
# selected dependency closure survived generator expressions and target-level
# overrides.
validate_link_command() {
    local link_file="$1"
    if [ ! -f "$link_file" ]; then
        echo "ERROR: MariaDB link command is missing: $link_file" >&2
        exit 1
    fi
    for archive in "$SYSROOT/lib/libssl.a" "$SYSROOT/lib/libcrypto.a"; do
        if ! grep -Fq "$archive" "$link_file"; then
            echo "ERROR: MariaDB link command omitted declared dependency $archive" >&2
            exit 1
        fi
    done
    if grep -Eiq 'wolfssl|extra/pcre2|(^|[ /])zlib/' "$link_file"; then
        echo "ERROR: MariaDB link command selected a bundled target library: $link_file" >&2
        exit 1
    fi
}
validate_link_command "$CROSS_BUILD_DIR/sql/CMakeFiles/mariadbd.dir/link.txt"
validate_link_command "$CROSS_BUILD_DIR/client/CMakeFiles/mariadb-test.dir/link.txt"

# Check if mariadbd was built (10.5+ renames mysqld → mariadbd)
MYSQLD_BIN="$CROSS_BUILD_DIR/sql/mariadbd"
if [ -f "$MYSQLD_BIN" ]; then
    echo "==> MariaDB mysqld built successfully!"
    ls -lh "$MYSQLD_BIN"

    # Formula installation owns the guest-facing executable name. The recipe
    # emits one canonical Wasm payload and does not duplicate bytes for the
    # registry's historical no-extension local-build alias.
    mkdir -p "$INSTALL_DIR/bin" "$INSTALL_DIR/share/mysql"
    cp "$MYSQLD_BIN" "$INSTALL_DIR/bin/mariadbd.wasm"

    # Keep the service's runtime data at the same guest path compiled through
    # INSTALL_MYSQLSHAREDIR. Missing SQL, charset, or localized error data is a
    # broken package, not an optional build embellishment.
    cp "$SRC_DIR/scripts/mysql_system_tables.sql" "$INSTALL_DIR/share/mysql/"
    cp "$SRC_DIR/scripts/mysql_system_tables_data.sql" "$INSTALL_DIR/share/mysql/"
    cp -R "$SRC_DIR/sql/share/charsets" "$INSTALL_DIR/share/mysql/charsets"

    # Copy every generated localized error table. Do not maintain a second
    # language allowlist here: the source/build graph owns that set.
    SHARE_BUILD="$CROSS_BUILD_DIR/sql/share"
    [ -d "$SHARE_BUILD" ] || {
        echo "ERROR: MariaDB did not generate its localized error tables" >&2
        exit 1
    }
    echo "==> Copying error message files..."
    for errmsg in "$SHARE_BUILD"/*/errmsg.sys; do
        [ -f "$errmsg" ] || continue
        language="$(basename "$(dirname "$errmsg")")"
        mkdir -p "$INSTALL_DIR/share/mysql/$language"
        cp "$errmsg" "$INSTALL_DIR/share/mysql/$language/errmsg.sys"
    done
    [ -f "$INSTALL_DIR/share/mysql/english/errmsg.sys" ] || {
        echo "ERROR: MariaDB did not generate the required English error table" >&2
        exit 1
    }
    echo "==> Error message files copied."

    echo "==> MariaDB install directory: $INSTALL_DIR"
else
    echo "ERROR: mysqld not found after build" >&2
    echo "Check build log in $CROSS_BUILD_DIR for errors."
    exit 1
fi

# --- Install mysqltest ---
MYSQLTEST_BIN="$CROSS_BUILD_DIR/client/mariadb-test"
if [ -f "$MYSQLTEST_BIN" ]; then
    echo "==> mysqltest built successfully!"
    ls -lh "$MYSQLTEST_BIN"
    cp "$MYSQLTEST_BIN" "$INSTALL_DIR/bin/mariadb-test.wasm"
else
    echo "ERROR: mariadb-test not found at $MYSQLTEST_BIN" >&2
    exit 1
fi

# --- Copy mysql-test suite data ---
# MariaDB 10.5 layout: main test suite is in mysql-test/main/ (not t/ and r/).
# The .test and .result files are both in main/.
MYSQL_TEST_SRC="$SRC_DIR/mysql-test"
[ -d "$MYSQL_TEST_SRC/main" ] || {
    echo "ERROR: MariaDB source is missing the canonical mysql-test/main suite" >&2
    exit 1
}
echo "==> Copying mysql-test suite data..."
MYSQL_TEST_DST="$INSTALL_DIR/mysql-test"
mkdir -p "$MYSQL_TEST_DST"
for subdir in main include std_data suite; do
    if [ -d "$MYSQL_TEST_SRC/$subdir" ]; then
        cp -R "$MYSQL_TEST_SRC/$subdir" "$MYSQL_TEST_DST/"
    fi
done
# Copy top-level helper files needed by mysqltest when they are supplied by
# this upstream release.
for f in unstable-tests suite.pm; do
    [ -f "$MYSQL_TEST_SRC/$f" ] && cp "$MYSQL_TEST_SRC/$f" "$MYSQL_TEST_DST/"
done
echo "==> mysql-test data copied to $MYSQL_TEST_DST"
