#!/usr/bin/env bash
set -euo pipefail

# Build the Formula's verified LLVM 21.1.7 source into the six wasm32 runtime
# tools admitted for Kandelo. The sealed recipe runner supplies every source,
# tool, dependency, and destination path used below.
RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
SOURCE_INPUT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
SYSROOT="${WASM_POSIX_SYSROOT:?}"
LIBCXX_DIR="${WASM_POSIX_DEP_LIBCXX_DIR:?}"
CMAKE="${WASM_POSIX_DEP_CMAKE:?}"
NINJA="${WASM_POSIX_DEP_NINJA:?}"
PATCH="${WASM_POSIX_DEP_PATCH:?}"
PYTHON="${WASM_POSIX_DEP_PYTHON:?}"
LLVM21_DIR="${WASM_POSIX_DEP_LLVM21_DIR:?}"
VERSION="${WASM_POSIX_DEP_VERSION:?}"
SOURCE_URL="${WASM_POSIX_DEP_SOURCE_URL:?}"
SOURCE_SHA256="${WASM_POSIX_DEP_SOURCE_SHA256:?}"
TARGET_ARCH="${WASM_POSIX_DEP_TARGET_ARCH:?}"

EXPECTED_VERSION="21.1.7"
EXPECTED_URL="https://github.com/llvm/llvm-project/releases/download/llvmorg-21.1.7/llvm-project-21.1.7.src.tar.xz"
EXPECTED_SHA256="e5b65fd79c95c343bb584127114cb2d252306c1ada1e057899b6aacdd445899e"
if [ "$TARGET_ARCH" != "wasm32" ] ||
   [ "$VERSION" != "$EXPECTED_VERSION" ] ||
   [ "$SOURCE_URL" != "$EXPECTED_URL" ] ||
   [ "$SOURCE_SHA256" != "$EXPECTED_SHA256" ]; then
    echo "ERROR: Clang Formula identity differs from the reviewed tap recipe" >&2
    exit 2
fi

for tool in "$CMAKE" "$NINJA" "$PATCH" "$PYTHON"; do
    if [ ! -x "$tool" ]; then
        echo "ERROR: declared Clang build tool is unavailable: $tool" >&2
        exit 1
    fi
done
for tool in wasm32posix-ar wasm32posix-c++ wasm32posix-cc \
    wasm32posix-nm wasm32posix-ranlib; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: required Clang cross-build tool is unavailable: $tool" >&2
        exit 1
    }
done
if [ ! -f "$SYSROOT/lib/libc.a" ]; then
    echo "ERROR: Clang requires the attested Kandelo sysroot" >&2
    exit 1
fi
if [ ! -f "$LIBCXX_DIR/lib/libc++.a" ] ||
   [ ! -f "$LIBCXX_DIR/lib/libc++abi.a" ] ||
   [ ! -d "$LIBCXX_DIR/include/c++/v1" ]; then
    echo "ERROR: Clang requires the selected libcxx keg" >&2
    exit 1
fi

LLVM_TABLEGEN="$LLVM21_DIR/bin/llvm-tblgen"
CLANG_TABLEGEN="$LLVM21_DIR/bin/clang-tblgen"
for tool in "$LLVM_TABLEGEN" "$CLANG_TABLEGEN"; do
    if [ ! -x "$tool" ]; then
        echo "ERROR: LLVM 21 table-generation tool is unavailable: $tool" >&2
        exit 1
    fi
    if ! "$tool" --version 2>&1 | grep -Eq 'LLVM version 21([.]|$)'; then
        echo "ERROR: table-generation tool does not report LLVM 21: $tool" >&2
        exit 1
    fi
done

SRC_DIR="$WORK_DIR/llvm-project"
BUILD_DIR="$WORK_DIR/wasm32"
if [ -e "$SRC_DIR" ]; then
    echo "ERROR: private LLVM source already exists: $SRC_DIR" >&2
    exit 1
fi
mkdir -p "$SRC_DIR"
cp -a --no-preserve=ownership "$SOURCE_INPUT/." "$SRC_DIR/"
find -P "$SRC_DIR" -type d -exec chmod u+rwx {} +
find -P "$SRC_DIR" -type f -exec chmod u+rw {} +

for patch_file in \
    0001-kandelo-deterministic-runtime.patch \
    0002-kandelo-vfs-output.patch \
    0003-kandelo-wasm-only-lld.patch; do
    "$PATCH" -d "$SRC_DIR" -p1 < "$RECIPE_DIR/patches/$patch_file"
done

# LLVM 21.1.7 already passes only F_executable here. The VFS patch carries a
# static assertion as defense in depth; keep a source-level check so a future
# source update cannot silently restore mmap-backed Wasm LLD output.
if grep -A2 'FileOutputBuffer::create(ctx.arg.outputFile' \
    "$SRC_DIR/lld/wasm/Writer.cpp" | grep -q 'F_mmap'; then
    echo "ERROR: Wasm LLD output unexpectedly enables F_mmap" >&2
    exit 1
fi

export WASM_POSIX_LIBCXX_DIR="$LIBCXX_DIR"
"$CMAKE" -G Ninja \
    -S "$SRC_DIR/llvm" \
    -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=MinSizeRel \
    -DCMAKE_SYSTEM_NAME=Generic \
    -DCMAKE_SYSTEM_PROCESSOR=wasm32 \
    -DCMAKE_C_COMPILER=wasm32posix-cc \
    -DCMAKE_CXX_COMPILER=wasm32posix-c++ \
    -DCMAKE_AR=wasm32posix-ar \
    -DCMAKE_RANLIB=wasm32posix-ranlib \
    -DCMAKE_NM=wasm32posix-nm \
    -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
    -DCMAKE_MAKE_PROGRAM="$NINJA" \
    -DPython3_EXECUTABLE="$PYTHON" \
    -DLLVM_TABLEGEN="$LLVM_TABLEGEN" \
    -DCLANG_TABLEGEN="$CLANG_TABLEGEN" \
    -DLLVM_ENABLE_PROJECTS='clang;lld' \
    -DLLVM_TARGETS_TO_BUILD=WebAssembly \
    -DLLVM_DEFAULT_TARGET_TRIPLE=wasm32-unknown-unknown \
    -DLLVM_HOST_TRIPLE=wasm32-unknown-unknown \
    -DLLVM_INCLUDE_TESTS=OFF \
    -DLLVM_INCLUDE_BENCHMARKS=OFF \
    -DLLVM_INCLUDE_EXAMPLES=OFF \
    -DLLVM_ENABLE_THREADS=OFF \
    -DLLVM_ENABLE_ZLIB=OFF \
    -DLLVM_ENABLE_ZSTD=OFF \
    -DLLVM_ENABLE_LIBXML2=OFF \
    -DLLVM_ENABLE_TERMINFO=OFF \
    -DLLVM_ENABLE_LIBEDIT=OFF \
    -DLLVM_ENABLE_EH=OFF \
    -DLLVM_ENABLE_RTTI=OFF \
    -DCLANG_ENABLE_ARCMT=OFF \
    -DCLANG_ENABLE_STATIC_ANALYZER=OFF \
    -DCLANG_ENABLE_PLUGIN_SUPPORT=OFF

"$CMAKE" --build "$BUILD_DIR" \
    --target clang lld llvm-ar llvm-ranlib llvm-nm -- -j1

mkdir -p "$OUT_DIR/bin" "$OUT_DIR/lib/clang/21"
for name in clang wasm-ld llvm-ar llvm-ranlib llvm-nm; do
    if [ ! -f "$BUILD_DIR/bin/$name" ]; then
        echo "ERROR: Clang build omitted runtime tool: $name" >&2
        exit 1
    fi
    cp "$BUILD_DIR/bin/$name" "$OUT_DIR/bin/$name"
    chmod 0755 "$OUT_DIR/bin/$name"
done
ln -s clang "$OUT_DIR/bin/clang++"
if [ ! -d "$BUILD_DIR/lib/clang/21/include" ]; then
    echo "ERROR: Clang build omitted version 21 resource headers" >&2
    exit 1
fi
cp -R "$BUILD_DIR/lib/clang/21/include" "$OUT_DIR/lib/clang/21/include"
find -P "$OUT_DIR/lib/clang/21/include" -type d -exec chmod 0755 {} +
find -P "$OUT_DIR/lib/clang/21/include" -type f -exec chmod 0644 {} +
cp "$SRC_DIR/llvm/LICENSE.TXT" "$OUT_DIR/LICENSE.TXT"
chmod 0644 "$OUT_DIR/LICENSE.TXT"

runtime_files="$WORK_DIR/runtime-files.txt"
find -P "$OUT_DIR" -type f -print | LC_ALL=C sort > "$runtime_files"
for forbidden in llvm-tblgen clang-tblgen cmake ninja python; do
    if grep -F "/$forbidden" "$runtime_files"; then
        echo "ERROR: clang runtime contains build-host tool $forbidden" >&2
        exit 1
    fi
done

file_mode() {
    if stat -c '%a' -- "$1" >/dev/null 2>&1; then
        stat -c '%a' -- "$1"
    else
        stat -f '%Lp' "$1"
    fi
}

while IFS= read -r -d '' entry; do
    relative="${entry#"$OUT_DIR"/}"
    if [ -L "$entry" ]; then
        if [ "$relative" != "bin/clang++" ] ||
           [ "$(readlink "$entry")" != "clang" ]; then
            echo "ERROR: undeclared Clang runtime symlink: $relative" >&2
            exit 1
        fi
        continue
    fi
    if [ -d "$entry" ]; then
        case "$relative" in
            bin|lib|lib/clang|lib/clang/21|lib/clang/21/include|lib/clang/21/include/*) ;;
            *)
                echo "ERROR: undeclared Clang runtime directory: $relative" >&2
                exit 1
                ;;
        esac
        continue
    fi
    if [ ! -f "$entry" ]; then
        echo "ERROR: unsupported Clang runtime node: $relative" >&2
        exit 1
    fi
    case "$relative" in
        bin/clang|bin/wasm-ld|bin/llvm-ar|bin/llvm-ranlib|bin/llvm-nm)
            expected_mode=755
            ;;
        LICENSE.TXT|lib/clang/21/include/*)
            expected_mode=644
            ;;
        *)
            echo "ERROR: undeclared Clang runtime file: $relative" >&2
            exit 1
            ;;
    esac
    actual_mode="$(file_mode "$entry")"
    if [ "$actual_mode" != "$expected_mode" ]; then
        echo "ERROR: Clang runtime mode $actual_mode is not $expected_mode: $relative" >&2
        exit 1
    fi
done < <(find -P "$OUT_DIR" -mindepth 1 -print0)
