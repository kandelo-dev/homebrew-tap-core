#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${WASM_POSIX_DEP_SOURCE_DIR:?}"
RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
TARGET_ARCH="${WASM_POSIX_DEP_TARGET_ARCH:?}"
SOURCE_URL="${WASM_POSIX_DEP_SOURCE_URL:?}"
SOURCE_SHA256="${WASM_POSIX_DEP_SOURCE_SHA256:?}"
SYSROOT="${WASM_POSIX_SYSROOT:?}"
CLANG_DIR="${WASM_POSIX_DEP_CLANG_DIR:?}"
LIBCXX_DIR="${WASM_POSIX_DEP_LIBCXX_DIR:?}"

EXPECTED_SOURCE_URL="https://github.com/Automattic/kandelo/archive/6d34c6d5183920c97454994bcf4fec060ee2f8d7.tar.gz"
EXPECTED_SOURCE_SHA256="3d9f18dcefb73819b7b158fda1662bb8b17cad8865e4d281915d8a22e757c588"
if [ "$TARGET_ARCH" != "wasm32" ] ||
   [ "$SOURCE_URL" != "$EXPECTED_SOURCE_URL" ] ||
   [ "$SOURCE_SHA256" != "$EXPECTED_SOURCE_SHA256" ]; then
    echo "ERROR: Kandelo SDK Formula identity differs from the reviewed source lock" >&2
    exit 2
fi

for recipe_entry in build.sh recipe.json source-lock.json; do
    if [ ! -f "$RECIPE_DIR/$recipe_entry" ] || [ -L "$RECIPE_DIR/$recipe_entry" ]; then
        echo "ERROR: sealed SDK recipe input is unavailable: $recipe_entry" >&2
        exit 2
    fi
done
while IFS= read -r -d '' recipe_entry; do
    case "${recipe_entry#"$RECIPE_DIR"/}" in
        build.sh|recipe.json|source-lock.json) ;;
        *)
            echo "ERROR: undeclared file entered the sealed SDK recipe: $recipe_entry" >&2
            exit 2
            ;;
    esac
done < <(find -P "$RECIPE_DIR" -mindepth 1 -maxdepth 1 -print0)

for source_input in \
    "$SOURCE_DIR/COPYING" \
    "$SOURCE_DIR/COPYING.runtime" \
    "$SOURCE_DIR/LICENSE" \
    "$SOURCE_DIR/sdk/config.site" \
    "$SOURCE_DIR/sdk/kandelo/notices/MUSL-COPYRIGHT"; do
    if [ ! -f "$source_input" ] || [ -L "$source_input" ]; then
        echo "ERROR: exact Kandelo SDK source is unavailable: $source_input" >&2
        exit 2
    fi
done
for source_directory in "$SOURCE_DIR/sdk/kandelo/bin" "$SOURCE_DIR/libc/glue"; do
    if [ ! -d "$source_directory" ] || [ -L "$source_directory" ]; then
        echo "ERROR: exact Kandelo SDK source directory is unavailable: $source_directory" >&2
        exit 2
    fi
done
if [ ! -f "$SYSROOT/lib/libc.a" ]; then
    echo "ERROR: Kandelo SDK requires the attested wasm32 sysroot" >&2
    exit 2
fi
for tool in clang wasm-ld; do
    if [ ! -x "$CLANG_DIR/libexec/llvm/bin/$tool" ]; then
        echo "ERROR: Kandelo SDK requires the selected Clang tool: $tool" >&2
        exit 2
    fi
done
if [ ! -f "$LIBCXX_DIR/include/c++/v1/vector" ] ||
   [ ! -f "$LIBCXX_DIR/lib/libc++.a" ] ||
   [ ! -f "$LIBCXX_DIR/lib/libc++abi.a" ]; then
    echo "ERROR: Kandelo SDK requires the selected libcxx keg" >&2
    exit 2
fi

SDK_DIR="$OUT_DIR/wasm32posix"
mkdir -p "$OUT_DIR/bin" "$SDK_DIR/sysroot" "$SDK_DIR/glue" \
    "$SDK_DIR/glue-objects" "$OUT_DIR/share/kandelo-sdk/examples" \
    "$OUT_DIR/share/kandelo-sdk/licenses"

cp -R "$SYSROOT/." "$SDK_DIR/sysroot/"
rm -rf -- "$SDK_DIR/sysroot/include/c++"
rm -f -- "$SDK_DIR/sysroot/lib/libc++.a" \
    "$SDK_DIR/sysroot/lib/libc++abi.a" \
    "$SDK_DIR/sysroot/lib/libc++experimental.a"
cp -R "$SOURCE_DIR/libc/glue/." "$SDK_DIR/glue/"
cp "$SOURCE_DIR/sdk/config.site" "$SDK_DIR/config.site"
cp "$SOURCE_DIR/sdk/kandelo/bin/"* "$OUT_DIR/bin/"
chmod 0755 "$OUT_DIR/bin/"*
chmod 0644 "$SDK_DIR/config.site"

export WASM_POSIX_LLVM_DIR="$CLANG_DIR/libexec/llvm"
export WASM_POSIX_SYSROOT="$SYSROOT"
export WASM_POSIX_GLUE_DIR="$SDK_DIR/glue"
export WASM_POSIX_GLUE_OBJ_DIR="$SDK_DIR/glue-objects"
export WASM_POSIX_CLANG_RESOURCE_DIR="$CLANG_DIR/libexec/llvm/lib/clang/21"
export WASM_POSIX_LIBCXX_DIR="$LIBCXX_DIR"
SDK_CC="$SOURCE_DIR/sdk/kandelo/bin/wasm32posix-cc"

for source in channel_syscall compiler_rt cxxrt dlopen; do
    "$SDK_CC" -O2 -c "$SDK_DIR/glue/$source.c" \
        -o "$SDK_DIR/glue-objects/$source.o"
done
chmod 0644 "$SDK_DIR/glue-objects/"*.o

printf '#include <stdio.h>\nint main(void){puts("hello from Kandelo C");}\n' \
    > "$OUT_DIR/share/kandelo-sdk/examples/hello.c"
printf '#include <iostream>\nint main(){std::cout<<"hello from Kandelo C++\\n";}\n' \
    > "$OUT_DIR/share/kandelo-sdk/examples/hello.cpp"
cp "$SOURCE_DIR/COPYING" \
    "$OUT_DIR/share/kandelo-sdk/licenses/KANDELO-GPL-2.0"
cp "$SOURCE_DIR/COPYING.runtime" \
    "$OUT_DIR/share/kandelo-sdk/licenses/KANDELO-RUNTIME-MIT"
cp "$SOURCE_DIR/LICENSE" \
    "$OUT_DIR/share/kandelo-sdk/licenses/KANDELO-LICENSING"
cp "$SOURCE_DIR/sdk/kandelo/notices/MUSL-COPYRIGHT" \
    "$OUT_DIR/share/kandelo-sdk/licenses/MUSL-COPYRIGHT"
chmod 0644 "$OUT_DIR/share/kandelo-sdk/examples/"* \
    "$OUT_DIR/share/kandelo-sdk/licenses/"*

if [ -e "$SDK_DIR/sysroot/include/c++/v1" ] ||
   [ -e "$SDK_DIR/sysroot/lib/libc++.a" ] ||
   [ -e "$SDK_DIR/sysroot/lib/libc++abi.a" ]; then
    echo "ERROR: SDK output duplicates libcxx-owned files" >&2
    exit 1
fi
if [ -e "$OUT_DIR/bin/clang" ] || [ -e "$OUT_DIR/lib/clang/21/include" ]; then
    echo "ERROR: SDK output duplicates Clang-owned files" >&2
    exit 1
fi
for wrapper in "$OUT_DIR/bin/"*; do
    if [ ! -f "$wrapper" ] || [ -L "$wrapper" ]; then
        echo "ERROR: SDK wrapper must be a regular file: $wrapper" >&2
        exit 1
    fi
    mode=$(stat -f '%Lp' "$wrapper" 2>/dev/null || stat -c '%a' "$wrapper")
    if [ "$mode" != "755" ]; then
        echo "ERROR: SDK wrapper mode is not 0755: $wrapper" >&2
        exit 1
    fi
done
for notice in "$OUT_DIR/share/kandelo-sdk/licenses/"*; do
    if [ ! -f "$notice" ] || [ -L "$notice" ]; then
        echo "ERROR: SDK notice must be a regular file: $notice" >&2
        exit 1
    fi
    mode=$(stat -f '%Lp' "$notice" 2>/dev/null || stat -c '%a' "$notice")
    if [ "$mode" != "644" ]; then
        echo "ERROR: SDK notice mode is not 0644: $notice" >&2
        exit 1
    fi
done
