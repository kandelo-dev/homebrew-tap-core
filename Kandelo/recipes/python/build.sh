#!/usr/bin/env bash
set -euo pipefail

RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
SOURCE_DIR="${WASM_POSIX_DEP_SOURCE_DIR:?}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
SYSROOT_SOURCE="${WASM_POSIX_SYSROOT:?}"
FORK_INSTRUMENT="${WASM_POSIX_FORK_INSTRUMENT:?}"
PYTHON_VERSION="${WASM_POSIX_DEP_VERSION:?}"
SOURCE_URL="${WASM_POSIX_DEP_SOURCE_URL:?}"
SOURCE_SHA256="${WASM_POSIX_DEP_SOURCE_SHA256:?}"
TARGET_ARCH="${WASM_POSIX_DEP_TARGET_ARCH:?}"
ZLIB_PREFIX="${WASM_POSIX_DEP_ZLIB_DIR:?}"
GUEST_PREFIX="${WASM_POSIX_DEP_GUEST_PREFIX:?}"

if [ "$TARGET_ARCH" != "wasm32" ] ||
   [ "$PYTHON_VERSION" != "3.13.3" ] ||
   [ "$SOURCE_URL" != "https://www.python.org/ftp/python/3.13.3/Python-3.13.3.tar.xz" ] ||
   [ "$SOURCE_SHA256" != "40f868bcbdeb8149a3149580bb9bfd407b3321cd48f0be631af955ac92c0e041" ]; then
    echo "ERROR: Python Formula identity differs from the reviewed tap recipe" >&2
    exit 2
fi
if [[ "$GUEST_PREFIX" != /* || "$GUEST_PREFIX" == *$'\n'* ||
      "/${GUEST_PREFIX#/}/" == *'/../'* || "/${GUEST_PREFIX#/}/" == *'/./'* ||
      "/${GUEST_PREFIX#/}/" == *'//'* ]]; then
    echo "ERROR: Python guest prefix must be a normalized absolute path" >&2
    exit 2
fi
if [ ! -f "$SYSROOT_SOURCE/lib/libc.a" ]; then
    echo "ERROR: Python requires the attested Kandelo sysroot" >&2
    exit 1
fi
if [ ! -f "$ZLIB_PREFIX/lib/libz.a" ] || [ ! -f "$ZLIB_PREFIX/include/zlib.h" ]; then
    echo "ERROR: Python requires the selected zlib keg" >&2
    exit 1
fi
for tool in gmake python3.13 wasm32posix-cc wasm32posix-c++ wasm32posix-ar \
    wasm32posix-ranlib wasm32posix-nm wasm32posix-strip \
    wasm32posix-pkg-config wasm-opt; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: required Python build tool is unavailable: $tool" >&2
        exit 1
    }
done

PYTHON_MAJOR_MINOR="${PYTHON_VERSION%.*}"
CROSS_BUILD_DIR="$WORK_DIR/cpython-cross-build"
RUNTIME_STAGE="$WORK_DIR/python-runtime-stage"
PRIVATE_SYSROOT="$WORK_DIR/cpython-sysroot"
STABLE_SOURCE="/usr/src/cpython-${PYTHON_VERSION}"
BUILD_JOBS="${WASM_POSIX_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu)}"

# CPython's WASI configure path names three empty emulation archives. The
# selected sysroot is trusted and read-only, so augment a private copy instead
# of mutating platform input shared by sibling Formula builds.
mkdir -p "$PRIVATE_SYSROOT"
cp -a "$SYSROOT_SOURCE/." "$PRIVATE_SYSROOT/"
# WHY: cp -a preserves the sealed sysroot's read-only directory modes. Open
# only the recipe-owned copy's library directory before adding CPython's three
# emulation archives; the publisher-owned source remains untouched.
chmod u+w "$PRIVATE_SYSROOT/lib"
export WASM_POSIX_SYSROOT="$PRIVATE_SYSROOT"
for library in \
    libwasi-emulated-signal.a \
    libwasi-emulated-getpid.a \
    libwasi-emulated-process-clocks.a; do
    [ -f "$PRIVATE_SYSROOT/lib/$library" ] ||
        wasm32posix-ar rcs "$PRIVATE_SYSROOT/lib/$library"
done

BUILD_TRIPLET="$("$SOURCE_DIR/config.guess")"
if [ -z "$BUILD_TRIPLET" ]; then
    echo "ERROR: CPython config.guess returned no native build triplet" >&2
    exit 1
fi

# WHY: CPython explicitly supports any native build Python with the same
# major/minor version. Use the declared, sealed Homebrew build dependency
# instead of treating Kandelo's unwrapped target LLVM as a native compiler.
HOST_PYTHON="$(command -v python3.13)"
HOST_PYTHON="$(/usr/bin/realpath -- "$HOST_PYTHON")"
# WHY: the runner projects conventional loader and host-tool paths from `/usr`
# into every recipe. Read-only mode alone would therefore let an undeclared
# ambient Python satisfy this build. Require the canonical executable to remain
# inside the declared Homebrew keg whose complete native closure was sealed.
case "$HOST_PYTHON" in
    */Cellar/python@3.13/*/bin/python3.13) ;;
    *)
        echo "ERROR: CPython build Python left its declared Homebrew keg" >&2
        exit 1
        ;;
esac
if [ ! -f "$HOST_PYTHON" ] || [ ! -x "$HOST_PYTHON" ] ||
   [ -w "$HOST_PYTHON" ]; then
    echo "ERROR: CPython build Python is not one sealed executable" >&2
    exit 1
fi
HOST_PYTHON_MOUNT_OPTIONS="$(
    /usr/bin/findmnt --noheadings --output VFS-OPTIONS \
        --target "$HOST_PYTHON"
)"
case ",${HOST_PYTHON_MOUNT_OPTIONS// /}," in
    *,ro,*) ;;
    *)
        echo "ERROR: CPython build Python is not on a read-only projection" >&2
        exit 1
        ;;
esac
HOST_PYTHON_VERSION="$(
    "$HOST_PYTHON" -c \
        'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
)"
if [ "$HOST_PYTHON_VERSION" != "$PYTHON_MAJOR_MINOR" ]; then
    echo "ERROR: CPython build Python must be $PYTHON_MAJOR_MINOR" >&2
    exit 1
fi

PREFIX_MAPS="-ffile-prefix-map=$SOURCE_DIR=$STABLE_SOURCE"
PREFIX_MAPS="$PREFIX_MAPS -fdebug-prefix-map=$SOURCE_DIR=$STABLE_SOURCE"
PREFIX_MAPS="$PREFIX_MAPS -fmacro-prefix-map=$SOURCE_DIR=$STABLE_SOURCE"
PREFIX_MAPS="$PREFIX_MAPS -ffile-prefix-map=$WORK_DIR=/usr/src/kandelo-build/cpython"
PREFIX_MAPS="$PREFIX_MAPS -fdebug-prefix-map=$WORK_DIR=/usr/src/kandelo-build/cpython"
PREFIX_MAPS="$PREFIX_MAPS -fmacro-prefix-map=$WORK_DIR=/usr/src/kandelo-build/cpython"
echo "==> Configuring CPython $PYTHON_VERSION for wasm32-posix"
mkdir -p "$CROSS_BUILD_DIR"
(
    cd "$CROSS_BUILD_DIR"
    CONFIG_SITE="$RECIPE_DIR/config.site-wasm32-posix" \
    PKG_CONFIG_PATH="$ZLIB_PREFIX/lib/pkgconfig" \
    CC=wasm32posix-cc \
    CXX=wasm32posix-c++ \
    AR=wasm32posix-ar \
    RANLIB=wasm32posix-ranlib \
    NM=wasm32posix-nm \
    STRIP=wasm32posix-strip \
    PKG_CONFIG=wasm32posix-pkg-config \
    py_cv_module__ssl=n/a \
    py_cv_module__hashlib=n/a \
    py_cv_module__decimal=n/a \
    py_cv_module__ctypes=n/a \
    py_cv_module__ctypes_test=n/a \
    py_cv_module__bz2=n/a \
    py_cv_module__lzma=n/a \
    py_cv_module__sqlite3=n/a \
    py_cv_module_readline=n/a \
    py_cv_module__tkinter=n/a \
    py_cv_module__dbm=n/a \
    py_cv_module__gdbm=n/a \
    "$SOURCE_DIR/configure" \
        --host=wasm32-unknown-wasi \
        --build="$BUILD_TRIPLET" \
        --with-build-python="$HOST_PYTHON" \
        --without-ensurepip \
        --disable-test-modules \
        --disable-shared \
        --without-mimalloc \
        --with-suffix=.wasm \
        --prefix="$GUEST_PREFIX" \
        CFLAGS="-O2 -gline-tables-only -fdebug-compilation-dir=$STABLE_SOURCE $PREFIX_MAPS -D_WASI_EMULATED_SIGNAL -D_WASI_EMULATED_PROCESS_CLOCKS" \
        CPPFLAGS="-I$ZLIB_PREFIX/include" \
        LDFLAGS="-L$ZLIB_PREFIX/lib"
)

# WHY: CPython embeds Makefile VPATH as a literal rather than a compiler source
# path, so the normal prefix-map flags cannot remove the publisher's staging
# root. Rewrite only that generated macro; Make's real VPATH remains intact.
"$HOST_PYTHON" - "$CROSS_BUILD_DIR/Makefile" "$STABLE_SOURCE" <<'PY'
from pathlib import Path
import sys

makefile = Path(sys.argv[1])
stable_source = sys.argv[2]
text = makefile.read_text()
needle = "-DVPATH='\"$(VPATH)\"'"
replacement = f"-DVPATH='\"{stable_source}\"'"
if text.count(needle) == 1 and replacement not in text:
    makefile.write_text(text.replace(needle, replacement))
elif text.count(replacement) != 1:
    raise SystemExit(f"expected exactly one CPython getpath VPATH define in {makefile}")
PY

echo "==> Building CPython wasm32 runtime"
gmake -C "$CROSS_BUILD_DIR" CONFIGURE_LDFLAGS_NODIST= -j"$BUILD_JOBS"

RAW_PYTHON="$CROSS_BUILD_DIR/python.wasm"
[ -f "$RAW_PYTHON" ] || RAW_PYTHON="$CROSS_BUILD_DIR/python"
if [ ! -f "$RAW_PYTHON" ]; then
    echo "ERROR: CPython build did not produce the interpreter" >&2
    exit 1
fi

OPTIMIZED_PYTHON="$WORK_DIR/python.optimized.wasm"
FINAL_PYTHON="$WORK_DIR/python.wasm"
wasm-opt -O2 "$RAW_PYTHON" -o "$OPTIMIZED_PYTHON"
# Fork instrumentation must remain the final Wasm transform.
# WHY: use the sealed runner-owned tool directly. The closed recipe must not
# regain broad Kandelo checkout authority merely to reach a wrapper script.
"$FORK_INSTRUMENT" \
    "$OPTIMIZED_PYTHON" -o "$FINAL_PYTHON"
chmod 0755 "$FINAL_PYTHON"

mkdir -p "$RUNTIME_STAGE/lib/python${PYTHON_MAJOR_MINOR}" \
    "$RUNTIME_STAGE/share/licenses/cpython"
# Reuse the same declared build Python for deterministic archive assembly.
"$HOST_PYTHON" - "$SOURCE_DIR/Lib" "$RUNTIME_STAGE/lib/python${PYTHON_MAJOR_MINOR}" <<'PY'
from pathlib import Path
import shutil
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
excluded = {"__pycache__", "test", "tests"}
for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
    relative = path.relative_to(source)
    if any(part in excluded for part in relative.parts):
        continue
    target = destination / relative
    if path.is_dir():
        target.mkdir(parents=True, exist_ok=True)
    elif path.is_file() and path.suffix not in {".pyc", ".pyo"}:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
PY
cp "$SOURCE_DIR/LICENSE" "$RUNTIME_STAGE/share/licenses/cpython/LICENSE"

RUNTIME_ZIP="$WORK_DIR/python-runtime.zip"
"$HOST_PYTHON" - "$RUNTIME_STAGE" "$RUNTIME_ZIP" <<'PY'
from pathlib import Path
import stat
import sys
import zipfile

root = Path(sys.argv[1])
output = Path(sys.argv[2])
timestamp = (2023, 11, 14, 22, 13, 20)
with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        info = zipfile.ZipInfo(relative, date_time=timestamp)
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, path.read_bytes())
PY

if [ -e "$OUT_DIR/python.wasm" ] || [ -L "$OUT_DIR/python.wasm" ] ||
   [ -e "$OUT_DIR/python-runtime.zip" ] || [ -L "$OUT_DIR/python-runtime.zip" ]; then
    echo "ERROR: Python output already exists" >&2
    exit 1
fi
cp "$FINAL_PYTHON" "$OUT_DIR/python.wasm"
cp "$RUNTIME_ZIP" "$OUT_DIR/python-runtime.zip"
chmod 0755 "$OUT_DIR/python.wasm"
chmod 0644 "$OUT_DIR/python-runtime.zip"

echo "==> Built Python $PYTHON_VERSION and its complete standard-library archive"
