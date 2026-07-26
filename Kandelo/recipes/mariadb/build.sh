#!/usr/bin/env bash
set -euo pipefail

# Build MariaDB 10.5 LTS for kandelo.
#
# Usage:
#   bash build-mariadb.sh           # build for wasm32 (ILP32)
#   bash build-mariadb.sh --wasm64  # build for wasm64 (LP64)
#
# Two-step cross-compilation:
#   1. Host build: generates import_executables.cmake (native helper programs)
#   2. Cross build: uses CMake toolchain file for wasm32 or wasm64

: "${WASM_POSIX_DEP_SOURCE_DIR:?}"
: "${WASM_POSIX_DEP_WORK_DIR:?}"
: "${WASM_POSIX_DEP_OUT_DIR:?}"
: "${WASM_POSIX_DEP_RECIPE_DIR:?}"
: "${WASM_POSIX_DEP_TARGET_ARCH:?}"
: "${WASM_POSIX_DEP_LIBCXX_DIR:?}"
: "${WASM_POSIX_DEP_PCRE2_DIR:?}"
: "${WASM_POSIX_DEP_ZLIB_DIR:?}"
: "${MARIADB_HOST_BUILD_DIR:?}"
: "${HOMEBREW_KANDELO_ROOT:?}"

MARIADB_VERSION="${WASM_POSIX_DEP_VERSION:?}"
WASM_ARCH="$WASM_POSIX_DEP_TARGET_ARCH"
SRC_DIR="$WASM_POSIX_DEP_SOURCE_DIR"
WORK_DIR="$WASM_POSIX_DEP_WORK_DIR"
INSTALL_DIR="$WASM_POSIX_DEP_OUT_DIR"
GUEST_PREFIX="/home/linuxbrew/.linuxbrew/opt/mariadb"
RECIPE_DIR="$WASM_POSIX_DEP_RECIPE_DIR"
HOST_BUILD_DIR="$MARIADB_HOST_BUILD_DIR"
GLUE_DIR="$HOMEBREW_KANDELO_ROOT/libc/glue"
BASE_SYSROOT="${WASM_POSIX_SYSROOT:?}"
SYSROOT="$WORK_DIR/sysroot"

case "$WASM_ARCH" in
    wasm32|wasm64) ;;
    *) echo "ERROR: unsupported MariaDB architecture: $WASM_ARCH" >&2; exit 1 ;;
esac

if [ "$WASM_ARCH" = "wasm64" ]; then
    CROSS_BUILD_DIR="$WORK_DIR/cross-build"
    TOOLCHAIN_FILE="$RECIPE_DIR/wasm64-posix-toolchain.cmake"
    WASM_TARGET="wasm64-unknown-unknown"
    # LLVM 21 wasm64 backend has -O2 miscompilation bugs (sign-extension of i32 to i64
    # in table lookups). Use -O1 until the LLVM wasm64 backend matures.
    : "${MARIADB_OPT_LEVEL:=-O1}"
else
    CROSS_BUILD_DIR="$WORK_DIR/cross-build"
    TOOLCHAIN_FILE="$RECIPE_DIR/wasm32-posix-toolchain.cmake"
    WASM_TARGET="wasm32-unknown-unknown"
fi

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

# --- Verify prerequisites ---
if [ ! -f "$SYSROOT/lib/libc.a" ]; then
    if [ "$WASM_ARCH" = "wasm64" ]; then
        echo "ERROR: sysroot64 not found at $SYSROOT. Run: bash scripts/build-musl.sh --arch wasm64posix" >&2
    else
        echo "ERROR: sysroot not found at $SYSROOT. Run: bash scripts/build-musl.sh" >&2
    fi
    exit 1
fi

if [ ! -f "$TOOLCHAIN_FILE" ]; then
    echo "ERROR: Toolchain file not found at $TOOLCHAIN_FILE" >&2
    exit 1
fi

# Check for cmake
if ! command -v cmake &>/dev/null; then
    echo "ERROR: cmake not found. Run through scripts/dev-shell.sh." >&2
    exit 1
fi

if ! command -v bison &>/dev/null; then
    echo "ERROR: bison not found. Run through scripts/dev-shell.sh." >&2
    exit 1
fi

# --- Apply wasm32 source patches ---
echo "==> Applying wasm32 source patches..."

# 1. Patch mariadb_connector_c.cmake: disable SSL for cross-builds
CONC_CMAKE="$SRC_DIR/cmake/mariadb_connector_c.cmake"
if grep -q 'IF(NOT CONC_WITH_SSL)' "$CONC_CMAKE" 2>/dev/null; then
    echo "  Patching cmake/mariadb_connector_c.cmake (disable SSL for cross-build)..."
    sed -i.bak 's/IF(NOT CONC_WITH_SSL)/IF(NOT CONC_WITH_SSL AND NOT CONC_WITH_SSL STREQUAL "OFF")/' "$CONC_CMAKE"
fi

# 2. my_gethwaddr: Enable Linux code path for wasm (SIOCGIFCONF + SIOCGIFHWADDR)
HWADDR_FILE="$SRC_DIR/mysys/my_gethwaddr.c"
if ! grep -q '__wasm' "$HWADDR_FILE" 2>/dev/null; then
    echo "  Patching mysys/my_gethwaddr.c (enable MAC address retrieval for wasm)..."
    sed -i.bak 's/defined(__linux__) || defined(__sun) || defined(_WIN32)/defined(__linux__) || defined(__sun) || defined(_WIN32) || defined(__wasm32__) || defined(__wasm64__)/' "$HWADDR_FILE"
    sed -i.bak 's/#elif defined(_AIX) || defined(__linux__) || defined(__sun)/#elif defined(_AIX) || defined(__linux__) || defined(__sun) || defined(__wasm32__) || defined(__wasm64__)/' "$HWADDR_FILE"
fi

if ! host_helpers_ready; then
    echo "ERROR: Formula-owned MariaDB host helpers are incomplete at $HOST_BUILD_DIR" >&2
    exit 1
fi

# --- Populate the private sysroot from declared target Formulae ---
LLVM_PREFIX="${LLVM_PREFIX:?LLVM_PREFIX not set. Run through scripts/dev-shell.sh.}"
LLVM_CLANG="$LLVM_PREFIX/bin/clang"
if [ ! -x "$LLVM_CLANG" ]; then
    echo "ERROR: clang not found at $LLVM_CLANG. Run through scripts/dev-shell.sh." >&2
    exit 1
fi

LIBCXX_PREFIX="$WASM_POSIX_DEP_LIBCXX_DIR"
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
cp "$PCRE2_PREFIX/lib/libpcre2-8.a" "$SYSROOT/lib/libpcre2-8.a"
cp "$PCRE2_PREFIX/lib/libpcre2-posix.a" "$SYSROOT/lib/libpcre2-posix.a"
cp "$PCRE2_PREFIX/include/pcre2.h" "$SYSROOT/include/pcre2.h"
cp "$PCRE2_PREFIX/include/pcre2posix.h" "$SYSROOT/include/pcre2posix.h"
cp "$ZLIB_PREFIX/lib/libz.a" "$SYSROOT/lib/libz.a"
cp "$ZLIB_PREFIX/include/zlib.h" "$SYSROOT/include/zlib.h"
cp "$ZLIB_PREFIX/include/zconf.h" "$SYSROOT/include/zconf.h"

# --- Pre-compile glue objects ---
WASM_COMPILE_FLAGS="--target=$WASM_TARGET -matomics -mbulk-memory -mexception-handling -mllvm -wasm-enable-sjlj -fno-trapping-math --sysroot=$SYSROOT"

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
    $LLVM_CLANG $WASM_COMPILE_FLAGS -O2 -c "$GLUE_DIR/channel_syscall.c" -o "$GLUE_OBJ_DIR/channel_syscall.o"
    $LLVM_CLANG $WASM_COMPILE_FLAGS -O2 -c "$GLUE_DIR/compiler_rt.c" -o "$GLUE_OBJ_DIR/compiler_rt.o"
    echo "==> Glue objects compiled."
fi
export MARIADB_GLUE_OBJ_DIR="$GLUE_OBJ_DIR"

# --- Step 2: Cross build ---
echo "==> Step 2: Cross build for $WASM_ARCH..."
mkdir -p "$CROSS_BUILD_DIR"
cd "$CROSS_BUILD_DIR"

export WASM_POSIX_SYSROOT="$SYSROOT"

cmake "$SRC_DIR" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN_FILE" \
    -DCMAKE_INSTALL_PREFIX="$GUEST_PREFIX" \
    -DIMPORT_EXECUTABLES="$HOST_BUILD_DIR/import_executables.cmake" \
    \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_FLAGS_RELEASE="${MARIADB_OPT_LEVEL:--O2} -DNDEBUG" \
    -DCMAKE_CXX_FLAGS_RELEASE="${MARIADB_OPT_LEVEL:--O2} -DNDEBUG" \
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
    -DWITH_SSL=OFF \
    -DCONC_WITH_SSL=OFF \
    -DWITH_PCRE=system \
    -DWITH_EDITLINE=bundled \
    -DWITH_ZLIB=system \
    -DZLIB_INCLUDE_DIR="$SYSROOT/include" \
    -DZLIB_LIBRARY_RELEASE="$SYSROOT/lib/libz.a" \
    -DZLIB_LIBRARY_DEBUG="$SYSROOT/lib/libz.a" \
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
    2>&1 | tail -40

# WHY: MariaDB silently falls back to its bundled Zlib when CMake's
# cross-compiling search cannot prove that a "system" archive is usable. That
# would make the declared Homebrew dependency decorative and create a second
# hidden source of the same library. Require the closed recipe to consume the
# exact Zlib keg copied into its private sysroot.
if ! grep -Fq "ZLIB_LIBRARY_RELEASE:FILEPATH=$SYSROOT/lib/libz.a" CMakeCache.txt ||
   ! grep -Fq "WITH_ZLIB:STRING=system" CMakeCache.txt; then
    echo "ERROR: MariaDB did not select the declared Zlib Formula dependency" >&2
    exit 1
fi

echo "==> CMake configuration complete. Starting build..."

# Build mysqld. Capture full output to a log so a failed build doesn't
# bury the actual diagnostic in `tail -N`. Show the tail on success and
# the relevant error context on failure.
MARIADBD_LOG="$CROSS_BUILD_DIR/build-mariadbd.log"
if make -j"$NPROC" mariadbd > "$MARIADBD_LOG" 2>&1; then
    tail -10 "$MARIADBD_LOG"
else
    echo "==> mariadbd build failed; printing error context:" >&2
    grep -B 2 -E "[Ee]rror|fatal|undefined" "$MARIADBD_LOG" | tail -50 >&2
    echo "" >&2
    echo "Full log: $MARIADBD_LOG" >&2
    exit 1
fi

# Build mysqltest client (mariadb-test target)
echo "==> Building mysqltest..."
MYSQLTEST_LOG="$CROSS_BUILD_DIR/build-mysqltest.log"
if make -j"$NPROC" mariadb-test > "$MYSQLTEST_LOG" 2>&1; then
    tail -10 "$MYSQLTEST_LOG"
else
    echo "==> mariadb-test build failed; printing error context:" >&2
    grep -B 2 -E "[Ee]rror|fatal|undefined" "$MYSQLTEST_LOG" | tail -50 >&2
    echo "" >&2
    echo "Full log: $MYSQLTEST_LOG" >&2
    exit 1
fi

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

    # Copy system tables SQL for bootstrap
    if [ -d "$SRC_DIR/scripts" ]; then
        cp "$SRC_DIR/scripts/mysql_system_tables.sql" "$INSTALL_DIR/share/mysql/" 2>/dev/null || true
        cp "$SRC_DIR/scripts/mysql_system_tables_data.sql" "$INSTALL_DIR/share/mysql/" 2>/dev/null || true
    fi

    # Copy error message files (generated by comp_err during build)
    SHARE_BUILD="$CROSS_BUILD_DIR/sql/share"
    if [ -d "$SHARE_BUILD" ]; then
        echo "==> Copying error message files..."
        for lang in bulgarian chinese czech danish dutch english estonian french german greek hindi hungarian italian japanese korean norwegian norwegian-ny polish portuguese romanian russian serbian slovak spanish swedish ukrainian; do
            if [ -d "$SHARE_BUILD/$lang" ] && [ -f "$SHARE_BUILD/$lang/errmsg.sys" ]; then
                mkdir -p "$INSTALL_DIR/share/$lang"
                cp "$SHARE_BUILD/$lang/errmsg.sys" "$INSTALL_DIR/share/$lang/"
            fi
        done
        echo "==> Error message files copied."
    fi

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
    cp "$MYSQLTEST_BIN" "$INSTALL_DIR/bin/mysqltest.wasm"
else
    echo "WARNING: mysqltest not found at $MYSQLTEST_BIN (skipping)" >&2
fi

# --- Copy mysql-test suite data ---
# MariaDB 10.5 layout: main test suite is in mysql-test/main/ (not t/ and r/).
# The .test and .result files are both in main/.
MYSQL_TEST_SRC="$SRC_DIR/mysql-test"
if [ -d "$MYSQL_TEST_SRC" ]; then
    echo "==> Copying mysql-test suite data..."
    MYSQL_TEST_DST="$INSTALL_DIR/mysql-test"
    mkdir -p "$MYSQL_TEST_DST"
    for subdir in main include std_data suite; do
        if [ -d "$MYSQL_TEST_SRC/$subdir" ]; then
            cp -R "$MYSQL_TEST_SRC/$subdir" "$MYSQL_TEST_DST/"
        fi
    done
    # Copy top-level helper files needed by mysqltest
    for f in unstable-tests suite.pm; do
        [ -f "$MYSQL_TEST_SRC/$f" ] && cp "$MYSQL_TEST_SRC/$f" "$MYSQL_TEST_DST/"
    done
    echo "==> mysql-test data copied to $MYSQL_TEST_DST"
else
    echo "WARNING: mysql-test directory not found in source tree" >&2
fi
