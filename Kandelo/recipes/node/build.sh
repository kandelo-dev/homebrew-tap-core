#!/usr/bin/env bash
# Build Mozilla SpiderMonkey's JS shell for the Kandelo wasm32 POSIX target.
#
# It produces `js.wasm` plus `node.wasm`, a Node-compatible shell entry point
# that runs the Kandelo CommonJS bootstrap when invoked as node.

set -euo pipefail

RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
SOURCE_INPUT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
VERSION="${WASM_POSIX_DEP_VERSION:?}"
SOURCE_URL="${WASM_POSIX_DEP_SOURCE_URL:?}"
SOURCE_SHA256="${WASM_POSIX_DEP_SOURCE_SHA256:?}"
ARCH="${WASM_POSIX_DEP_TARGET_ARCH:?}"
if [ "${WASM_POSIX_DEP_NAME:?}" != "node" ] ||
   [ "$VERSION" != "140.11.0esr" ] ||
   [ "$SOURCE_URL" != "https://ftp.mozilla.org/pub/firefox/releases/140.11.0esr/source/firefox-140.11.0esr.source.tar.xz" ] ||
   [ "$SOURCE_SHA256" != "1b034d2117356fda24807a151055132315c6ba58ad2bdf7ec71ee707fac5e028" ] ||
   [ "$ARCH" != "wasm32" ]; then
    echo "ERROR: Node Formula identity differs from the reviewed tap recipe" >&2
    exit 2
fi

BIN_DIR="$WORK_DIR/bin"
SRC_DIR="$WORK_DIR/source"
OBJ_DIR="$WORK_DIR/obj-wasm32"
MOZCONFIG_PATH="$WORK_DIR/mozconfig-wasm32"
MOZBUILD_STATE_PATH="$WORK_DIR/.mozbuild"
SOURCE_SYSROOT="${WASM_POSIX_SYSROOT:?}"
SYSROOT="$WORK_DIR/node-sysroot"
mkdir -p "$WORK_DIR" "$OUT_DIR" "$BIN_DIR"
[ ! -e "$SRC_DIR" ] || { echo "ERROR: private Node source already exists" >&2; exit 2; }
[ ! -e "$SYSROOT" ] || { echo "ERROR: private Node sysroot already exists" >&2; exit 2; }
mkdir -p "$SRC_DIR" "$SYSROOT"
cp -a --no-preserve=ownership "$SOURCE_INPUT/." "$SRC_DIR/"
cp -a --no-preserve=ownership "$SOURCE_SYSROOT/." "$SYSROOT/"
find -P "$SRC_DIR" "$SYSROOT" -type d -exec chmod u+rwx {} +
find -P "$SRC_DIR" "$SYSROOT" -type f -exec chmod u+rw {} +
export WASM_POSIX_SYSROOT="$SYSROOT"
HOST_OS="$(uname -s)"
MACOS_SDK_DIR="${WASM_POSIX_MACOS_SDK_DIR:-}"

if [ "$HOST_OS" = "Darwin" ] && [ -z "$MACOS_SDK_DIR" ] && command -v xcrun >/dev/null 2>&1; then
    SYSTEM_DEVELOPER_DIR="/Applications/Xcode.app/Contents/Developer"
    if [ -d "$SYSTEM_DEVELOPER_DIR" ]; then
        MACOS_SDK_DIR="$(DEVELOPER_DIR="$SYSTEM_DEVELOPER_DIR" xcrun --sdk macosx --show-sdk-path 2>/dev/null || true)"
        if [ -n "$MACOS_SDK_DIR" ]; then
            export DEVELOPER_DIR="$SYSTEM_DEVELOPER_DIR"
            export SDKROOT="$MACOS_SDK_DIR"
        fi
    else
        MACOS_SDK_DIR="$(xcrun --sdk macosx --show-sdk-path 2>/dev/null || true)"
    fi
fi

if [ -n "$MACOS_SDK_DIR" ] && [ ! -d "$MACOS_SDK_DIR" ]; then
    echo "ERROR: macOS SDK not found at $MACOS_SDK_DIR." >&2
    exit 1
fi

if [ ! -f "$SYSROOT/lib/libc.a" ]; then
    echo "ERROR: sysroot not found at $SYSROOT. Run 'bash build.sh' first." >&2
    exit 1
fi

for required_tool in python3 rustc cargo cbindgen node make; do
    if ! command -v "$required_tool" >/dev/null 2>&1; then
        echo "ERROR: required host tool '$required_tool' not found in PATH." >&2
        exit 1
    fi
done

LIBCXX_PREFIX="${WASM_POSIX_DEP_LIBCXX_DIR:?}"
[ -f "$LIBCXX_PREFIX/lib/libc++.a" ] || {
    echo "ERROR: libcxx resolve missing libc++.a at $LIBCXX_PREFIX" >&2
    exit 1
}
[ -f "$LIBCXX_PREFIX/lib/libc++abi.a" ] || {
    echo "ERROR: libcxx resolve missing libc++abi.a at $LIBCXX_PREFIX" >&2
    exit 1
}
[ -d "$LIBCXX_PREFIX/include/c++/v1" ] || {
    echo "ERROR: libcxx resolve missing include/c++/v1 at $LIBCXX_PREFIX" >&2
    exit 1
}

OPENSSL_PREFIX="${WASM_POSIX_DEP_OPENSSL_DIR:?}"
[ -f "$OPENSSL_PREFIX/lib/libssl.a" ] || {
    echo "ERROR: openssl resolve missing libssl.a at $OPENSSL_PREFIX" >&2
    exit 1
}
[ -f "$OPENSSL_PREFIX/lib/libcrypto.a" ] || {
    echo "ERROR: openssl resolve missing libcrypto.a at $OPENSSL_PREFIX" >&2
    exit 1
}
[ -d "$OPENSSL_PREFIX/include" ] || {
    echo "ERROR: openssl resolve missing include directory at $OPENSSL_PREFIX" >&2
    exit 1
}

ZLIB_PREFIX="${WASM_POSIX_DEP_ZLIB_DIR:?}"
[ -f "$ZLIB_PREFIX/lib/libz.a" ] || {
    echo "ERROR: zlib resolve missing libz.a at $ZLIB_PREFIX" >&2
    exit 1
}
[ -d "$ZLIB_PREFIX/include" ] || {
    echo "ERROR: zlib resolve missing include directory at $ZLIB_PREFIX" >&2
    exit 1
}

# Mozilla's build uses the compiler driver directly and expects libc++ to be
# visible from the target sysroot. Keep the sysroot as an index of cache-managed
# libcxx artifacts, matching the MariaDB and C++ program build paths.
mkdir -p "$SYSROOT/lib" "$SYSROOT/include/c++"
ln -sf "$LIBCXX_PREFIX/lib/libc++.a" "$SYSROOT/lib/libc++.a"
ln -sf "$LIBCXX_PREFIX/lib/libc++abi.a" "$SYSROOT/lib/libc++abi.a"
ln -sf "$OPENSSL_PREFIX/lib/libssl.a" "$SYSROOT/lib/libssl.a"
ln -sf "$OPENSSL_PREFIX/lib/libcrypto.a" "$SYSROOT/lib/libcrypto.a"
ln -sf "$ZLIB_PREFIX/lib/libz.a" "$SYSROOT/lib/libz.a"
rm -rf "$SYSROOT/include/c++/v1"
ln -sfn "$LIBCXX_PREFIX/include/c++/v1" "$SYSROOT/include/c++/v1"

if [ ! -f "$SRC_DIR/mach" ]; then
    echo "ERROR: Formula-owned SpiderMonkey source has no mach entry point: $SRC_DIR" >&2
    exit 1
fi
PATCH_DIR="$RECIPE_DIR/patches"
if [ -d "$PATCH_DIR" ]; then
    for patch_file in "$PATCH_DIR"/*.patch; do
        [ -f "$patch_file" ] || continue
        if patch -p1 -N --dry-run --silent -d "$SRC_DIR" < "$patch_file" >/dev/null 2>&1; then
            echo "==> Applying $(basename "$patch_file")..."
            patch -p1 -N -d "$SRC_DIR" < "$patch_file"
        fi
    done
fi

NODE_ADAPTER_JS="$RECIPE_DIR/node-compat/adapter.js"
NODE_SHARED_BOOTSTRAP_JS="$RECIPE_DIR/node-compat/bootstrap.js"
NODE_SUFFIX_JS="$RECIPE_DIR/node-compat/suffix.js"
NODE_BOOTSTRAP_JS="$OBJ_DIR/kandelo-node-bootstrap.generated.js"
NODE_BOOTSTRAP_INC="$SRC_DIR/js/src/shell/kandelo-node-bootstrap.h"
for bootstrap_part in "$NODE_ADAPTER_JS" "$NODE_SHARED_BOOTSTRAP_JS" "$NODE_SUFFIX_JS"; do
    [ -f "$bootstrap_part" ] || {
        echo "ERROR: SpiderMonkey Node bootstrap input missing at $bootstrap_part" >&2
        exit 1
    }
done
mkdir -p "$OBJ_DIR"
echo "==> Generating SpiderMonkey Node bootstrap include..."
python3 - "$NODE_ADAPTER_JS" "$NODE_SHARED_BOOTSTRAP_JS" "$NODE_SUFFIX_JS" "$NODE_BOOTSTRAP_JS" "$NODE_BOOTSTRAP_INC" <<'PY'
from pathlib import Path
import sys

adapter = Path(sys.argv[1]).read_text(encoding="utf-8")
shared = Path(sys.argv[2]).read_text(encoding="utf-8")
suffix = Path(sys.argv[3]).read_text(encoding="utf-8")
generated = Path(sys.argv[4])
dest = Path(sys.argv[5])

shared_lines = [
    line for line in shared.splitlines()
    if not line.startswith("import * as ")
]
source_text = adapter.rstrip() + "\n" + "\n".join(shared_lines) + "\n" + suffix.rstrip() + "\n"
generated.write_text(source_text, encoding="utf-8")
source = source_text.encode("utf-8")
dest.parent.mkdir(parents=True, exist_ok=True)
with dest.open("w", encoding="ascii") as f:
    f.write("#ifndef shell_kandelo_node_bootstrap_h\n")
    f.write("#define shell_kandelo_node_bootstrap_h\n\n")
    f.write("static const unsigned char kKandeloNodeBootstrap[] = {\n")
    for i in range(0, len(source), 12):
        chunk = source[i:i + 12]
        f.write("  " + ", ".join(f"0x{b:02x}" for b in chunk) + ",\n")
    f.write("};\n")
    f.write("static const size_t kKandeloNodeBootstrapLen = sizeof(kKandeloNodeBootstrap);\n")
    f.write("\n#endif  // shell_kandelo_node_bootstrap_h\n")
PY

WASM_RUST_TARGET_FEATURES='-Ctarget-feature=+atomics,+bulk-memory,+mutable-globals'
GETRANDOM_BACKEND_RUSTFLAG='--cfg=getrandom_backend=\"custom\"'
WASM_RUSTFLAGS="$WASM_RUST_TARGET_FEATURES $GETRANDOM_BACKEND_RUSTFLAG"

cat > "$MOZCONFIG_PATH" <<EOF
export RUSTFLAGS="\${RUSTFLAGS:+\$RUSTFLAGS }$WASM_RUSTFLAGS"
ac_add_options --enable-project=js
ac_add_options --target=wasm32-unknown-linux-musl
ac_add_options --disable-debug
ac_add_options --enable-optimize="-O2"
ac_add_options --disable-jit
ac_add_options --disable-jemalloc
ac_add_options --disable-stdcxx-compat
ac_add_options --without-system-zlib
ac_add_options --with-intl-api
ac_add_options --enable-icu4x
ac_add_options --disable-shared-js
ac_add_options --enable-shared-memory
ac_add_options --disable-clang-plugin
ac_add_options --disable-tests
ac_add_options --disable-debug-symbols
mk_add_options MOZ_OBJDIR=$OBJ_DIR
EOF
if [ -n "$MACOS_SDK_DIR" ]; then
    echo "ac_add_options --with-macos-sdk=$MACOS_SDK_DIR" >> "$MOZCONFIG_PATH"
fi

export MOZCONFIG="$MOZCONFIG_PATH"
export MOZBUILD_STATE_PATH
export MACH_BUILD_PYTHON_NATIVE_PACKAGE_SOURCE=system

TARGET_OS_DEFINES="${WASM_POSIX_TARGET_OS_DEFINES:--D__linux__=1 -D__unix__=1}"
export CC="${WASM_POSIX_TARGET_CC:-wasm32posix-cc} $TARGET_OS_DEFINES"
export CXX="${WASM_POSIX_TARGET_CXX:-wasm32posix-c++} $TARGET_OS_DEFINES"
export AS="${WASM_POSIX_TARGET_AS:-wasm32posix-cc} $TARGET_OS_DEFINES"
export AR="${WASM_POSIX_TARGET_AR:-wasm32posix-ar}"
export RANLIB="${WASM_POSIX_TARGET_RANLIB:-wasm32posix-ranlib}"
export NM="${WASM_POSIX_TARGET_NM:-wasm32posix-nm}"
export STRIP="${WASM_POSIX_TARGET_STRIP:-wasm32posix-strip}"
if [ "$HOST_OS" = "Darwin" ]; then
    export HOST_CC="${HOST_CC:-/usr/bin/cc}"
    export HOST_CXX="${HOST_CXX:-/usr/bin/c++}"
else
    export HOST_CC="${HOST_CC:-cc}"
    export HOST_CXX="${HOST_CXX:-c++}"
fi
export CFLAGS="${CFLAGS:-} -D_GNU_SOURCE -I$OPENSSL_PREFIX/include -I$ZLIB_PREFIX/include"
export CXXFLAGS="${CXXFLAGS:-} -D_GNU_SOURCE -fexceptions -I$OPENSSL_PREFIX/include -I$ZLIB_PREFIX/include"
export LDFLAGS="${LDFLAGS:-} -lc++ -lc++abi $OPENSSL_PREFIX/lib/libssl.a $OPENSSL_PREFIX/lib/libcrypto.a $ZLIB_PREFIX/lib/libz.a -Wl,-z,stack-size=16777216"

if [ -f "$OBJ_DIR/config.status" ] && {
    ! grep -q 'getrandom_backend' "$OBJ_DIR/config.status" ||
        grep -q 'getrandom_backend=custom' "$OBJ_DIR/config.status" ||
        ! grep -q 'target-feature=+atomics' "$OBJ_DIR/config.status"
}; then
    echo "==> Refreshing stale Mozilla configure state for Rust target flags..."
    rm -f "$OBJ_DIR/config.status" "$OBJ_DIR/.mozconfig.json"
fi

RUST_RELEASE_DIR="$OBJ_DIR/wasm32-unknown-unknown/release"
JSRUST_FINGERPRINT="$(
    find "$RUST_RELEASE_DIR/.fingerprint" -path '*/lib-jsrust.json' -type f -print -quit 2>/dev/null || true
)"
if [ -n "$JSRUST_FINGERPRINT" ] && ! grep -q 'target-feature=+atomics' "$JSRUST_FINGERPRINT"; then
    echo "==> Rebuilding stale SpiderMonkey Rust archive for wasm atomics..."
    rm -f "$RUST_RELEASE_DIR/libjsrust.a"
    rm -rf "$RUST_RELEASE_DIR/.fingerprint"/jsrust-*
fi

echo "==> Building SpiderMonkey JS shell for wasm32..."
(cd "$SRC_DIR" && ./mach --no-interactive build)

JS_BIN=""
for candidate in "$OBJ_DIR/dist/bin/js" "$OBJ_DIR/dist/bin/js.wasm"; do
    if [ -f "$candidate" ]; then
        JS_BIN="$candidate"
        break
    fi
done

if [ -z "$JS_BIN" ]; then
    echo "ERROR: SpiderMonkey build finished but no js shell was found under $OBJ_DIR/dist/bin." >&2
    exit 1
fi

cp "$JS_BIN" "$BIN_DIR/js.wasm"

WASM_OPT="${WASM_OPT:-wasm-opt}"
if command -v "$WASM_OPT" >/dev/null 2>&1; then
    echo "==> Optimizing js.wasm with wasm-opt -O2..."
    "$WASM_OPT" -O2 "$BIN_DIR/js.wasm" -o "$BIN_DIR/js.wasm"
else
    echo "WARNING: wasm-opt not found; leaving unoptimized js.wasm." >&2
fi

# Do not run SpiderMonkey through wasm-fork-instrument. The rewrite expands
# SpiderMonkey's already-large C++ control flow enough that Chromium workers
# exhaust their Wasm call stack before the shell reaches user JavaScript.
# SpiderMonkey worker_threads use clone/pthreads, which remain supported
# without POSIX fork-stack instrumentation.

JS_SIZE="$(wc -c < "$BIN_DIR/js.wasm" | tr -d ' ')"
echo "==> SpiderMonkey built successfully: $BIN_DIR/js.wasm ($JS_SIZE bytes)"

cp "$BIN_DIR/js.wasm" "$BIN_DIR/node.wasm"
NODE_SIZE="$(wc -c < "$BIN_DIR/node.wasm" | tr -d ' ')"
echo "==> SpiderMonkey Node-compatible runtime staged: $BIN_DIR/node.wasm ($NODE_SIZE bytes)"

install -m 0755 "$BIN_DIR/node.wasm" "$OUT_DIR/node.wasm"
