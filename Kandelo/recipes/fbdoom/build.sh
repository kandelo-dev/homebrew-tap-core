#!/usr/bin/env bash
# Cross-compile maximevince/fbDOOM for Kandelo using wasm32posix-cc. The
# fbdev frontend writes BGRA32 pixels into the framebuffer mmap; the canvas
# renderer consumes them.
#
# Output: WASM_POSIX_DEP_OUT_DIR/fbdoom.wasm.
set -euo pipefail

HERE="${WASM_POSIX_DEP_RECIPE_DIR:?}"
SRC="${WASM_POSIX_DEP_SOURCE_DIR:?}"
CDOOM_SRC="${FBDOOM_CHOCOLATE_DOOM_SOURCE_DIR:?}"
OUT_BIN="${WASM_POSIX_DEP_OUT_DIR:?}/fbdoom.wasm"

# fbDOOM has no release tarball, so pin the exact upstream commit represented
# by both the package manifest and Homebrew Formula. fbDOOM removed its
# OPL/MIDI/MUS sources with SDL; pin chocolate-doom 3.1.0 for those files.
FBDOOM_COMMIT="17280163bc95e5d954d2efaa0633489b763b4cd1"
FBDOOM_SOURCE_URL="${WASM_POSIX_DEP_SOURCE_URL:?}"
FBDOOM_SOURCE_SHA256="${WASM_POSIX_DEP_SOURCE_SHA256:?}"
CDOOM_COMMIT="35fb1372d10756ca27eca05665bd8a7cebc71c05"
CDOOM_SOURCE_URL="${FBDOOM_CHOCOLATE_DOOM_SOURCE_URL:?}"
CDOOM_SOURCE_SHA256="${FBDOOM_CHOCOLATE_DOOM_SOURCE_SHA256:?}"

[ "${WASM_POSIX_DEP_TARGET_ARCH:?}" = "wasm32" ] &&
    [ "${WASM_POSIX_DEP_VERSION:?}" = "0.1.0" ] || {
    echo "ERROR: fbDOOM is currently packaged as version 0.1.0 for wasm32 only" >&2
    exit 2
}
[ "$FBDOOM_SOURCE_URL" = "https://github.com/maximevince/fbDOOM/archive/${FBDOOM_COMMIT}.tar.gz" ] &&
    [ "$FBDOOM_SOURCE_SHA256" = "77f57cee68fed438dffdba96f6070b8975c16652a63ddf4fb967994e5585a38a" ] &&
    [ "$CDOOM_SOURCE_URL" = "https://github.com/chocolate-doom/chocolate-doom/archive/${CDOOM_COMMIT}.tar.gz" ] &&
    [ "$CDOOM_SOURCE_SHA256" = "dc62c13cab469e19e0ad295b2dd7e460263c637a39c51d3771e96dabb08ecab2" ] || {
    echo "ERROR: fbDOOM source identity differs from the reviewed recipe" >&2
    exit 2
}

# Sentinel: last file added by patches/0005-add-music-support.patch. If it is
# present, the source tree is already fully vendored and patched. Re-vendoring
# would clobber the earlier patch's edits to these imported sources.
SENTINEL="$SRC/fbdoom/opl/opl_kernel.c"

apply_patches() {
    local mode="${1:-strict}"
    local name patch_file
    echo "==> Applying patches..."
    for patch_file in "$HERE/patches/"*.patch; do
        [ -f "$patch_file" ] || continue
        name="$(basename "$patch_file")"
        if patch --forward --dry-run -d "$SRC" -p1 <"$patch_file" \
            >/dev/null 2>&1; then
            echo "    $name"
            patch --forward -d "$SRC" -p1 <"$patch_file"
        elif patch --reverse --dry-run -d "$SRC" -p1 <"$patch_file" \
            >/dev/null 2>&1; then
            echo "    $name (already applied)"
        elif [ "$mode" = "lenient" ]; then
            echo "    $name (already applied or superseded)"
        else
            echo "ERROR: patch $name does not apply cleanly" >&2
            exit 1
        fi
    done
}

if [ -e "$SENTINEL" ]; then
    echo "==> Source tree already vendored (sentinel present); checking patches."
    apply_patches lenient
else
    echo "==> Vendoring OPL/MIDI/MUS sources from chocolate-doom..."
    mkdir -p "$SRC/fbdoom/opl"
    for file in opl.c opl.h opl3.c opl3.h opl_internal.h opl_queue.c opl_queue.h; do
        cp "$CDOOM_SRC/opl/$file" "$SRC/fbdoom/opl/$file"
    done
    for file in mus2mid.c mus2mid.h midifile.c midifile.h; do
        cp "$CDOOM_SRC/src/$file" "$SRC/fbdoom/$file"
    done

    apply_patches strict
fi

cd "$SRC/fbdoom"

echo "==> Cleaning previous build..."
make clean || true

echo "==> Cross-compiling fbdoom (wasm32, NOSDL=1)..."
# fbDOOM's Makefile wires NOSDL=1 to the framebuffer and null-audio frontend.
# Passing -lc explicitly would duplicate the SDK-injected channel syscall glue;
# retain -lm because the SDK does not inject libm.
make CC=wasm32posix-cc \
     LD=wasm32posix-cc \
     CFLAGS="-O2 -DNORMALUNIX -DLINUX -D_DEFAULT_SOURCE -Iopl" \
     LDFLAGS="" \
     LIBS="-lm" \
     NOSDL=1

cp fbdoom "$OUT_BIN"

# fbDOOM does not fork, so it must remain free of fork instrumentation.
ls -la "$OUT_BIN"
echo "==> fbdoom.wasm built."

# No IWAD is bundled. The browser demo fetches the freely redistributable Doom
# shareware IWAD at page load and caches it via the Cache API.
