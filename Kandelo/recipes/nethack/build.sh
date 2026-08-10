#!/usr/bin/env bash
set -euo pipefail

# Build the Formula's verified NetHack 3.6.7 source for wasm32-posix. The
# schema-1 Formula helper supplies every source, tool, dependency, and output
# path; this recipe has no registry, resolver, downloader, or install authority.
SCRIPT_DIR="${WASM_POSIX_DEP_RECIPE_DIR:?}"
SOURCE_INPUT="${WASM_POSIX_DEP_SOURCE_DIR:?}"
WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:?}"
OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:?}"
SYSROOT="${WASM_POSIX_SYSROOT:?}"
FORK_INSTRUMENT="${WASM_POSIX_FORK_INSTRUMENT:?}"
HOST_CC="${WASM_POSIX_DEP_HOST_CC:?}"
MAKE="${WASM_POSIX_DEP_MAKE:?}"
PATCH="${WASM_POSIX_DEP_PATCH:?}"
BISON="${WASM_POSIX_DEP_BISON:?}"
FLEX="${WASM_POSIX_DEP_FLEX:?}"
VERSION="${WASM_POSIX_DEP_VERSION:?}"
SOURCE_URL="${WASM_POSIX_DEP_SOURCE_URL:?}"
SOURCE_SHA256="${WASM_POSIX_DEP_SOURCE_SHA256:?}"
TARGET_ARCH="${WASM_POSIX_DEP_TARGET_ARCH:?}"
NCURSES_PREFIX="${WASM_POSIX_DEP_NCURSES_DIR:?}"
GUEST_HACKDIR="${WASM_POSIX_DEP_GUEST_HACKDIR:?}"
GUEST_VAR_PLAYGROUND="${WASM_POSIX_DEP_GUEST_VAR_PLAYGROUND:?}"
SRC_DIR="$WORK_DIR/nethack-source"

if [ "$TARGET_ARCH" != wasm32 ] || [ "$VERSION" != 3.6.7 ] ||
   [ "$SOURCE_URL" != "https://www.nethack.org/download/3.6.7/nethack-367-src.tgz" ] ||
   [ "$SOURCE_SHA256" != "98cf67df6debf9668a61745aa84c09bcab362e5d33f5b944ec5155d44d2aacb2" ]; then
  echo "ERROR: NetHack Formula identity differs from the reviewed tap recipe" >&2
  exit 2
fi

validate_guest_path() {
  local label="$1" value="$2"
  if [[ ! "$value" =~ ^/([A-Za-z0-9._@%+=:-]+/)*[A-Za-z0-9._@%+=:-]+$ ]] ||
     [[ "/${value#/}/" == *'/../'* || "/${value#/}/" == *'/./'* ||
        "/${value#/}/" == *'//'* ]]; then
    echo "ERROR: $label must be a safe normalized absolute guest path: $value" >&2
    exit 2
  fi
}

validate_guest_path HACKDIR "$GUEST_HACKDIR"
validate_guest_path VAR_PLAYGROUND "$GUEST_VAR_PLAYGROUND"
test -f "$SYSROOT/lib/libc.a"
test -f "$NCURSES_PREFIX/lib/libncursesw.a"
for tool in "$FORK_INSTRUMENT" "$HOST_CC" "$MAKE" "$PATCH" "$BISON" "$FLEX"; do
  test -x "$tool" || { echo "ERROR: missing declared tool: $tool" >&2; exit 1; }
done
for tool in wasm32posix-ar wasm32posix-cc wasm32posix-ranlib; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: missing SDK tool: $tool" >&2; exit 1; }
done

test ! -e "$SRC_DIR"
mkdir -p "$SRC_DIR"
cp -a --no-preserve=ownership "$SOURCE_INPUT/." "$SRC_DIR/"
find -P "$SRC_DIR" -type d -exec chmod u+rwx {} +
find -P "$SRC_DIR" -type f -exec chmod u+rw {} +

(cd "$SRC_DIR/sys/unix" && sh setup.sh hints/linux)
"$PATCH" -d "$SRC_DIR" -p1 < "$SCRIPT_DIR/patches/kandelo-terminal.patch"
"$PATCH" -d "$SRC_DIR" -p1 < "$SCRIPT_DIR/patches/kandelo-portable-data-layout.patch"

# Bind the selected ncurses keg and the stable guest data paths in the only
# generated Makefile whose compiler and linker flags need target overrides.
sed -i.bak -E \
  -e 's|^WINTTYLIB *=.*|WINTTYLIB=-lncursesw -ltinfow|' \
  -e 's|^WINCURSESLIB *=.*|WINCURSESLIB=-lncursesw -ltinfow|' \
  -e 's|^CFLAGS\+=-DSYSCF -DSYSCF_FILE=.*|# SYSCF disabled for Kandelo single-user install|' \
  -e 's|^CFLAGS\+=-DCONFIG_ERROR_SECURE=.*|# CONFIG_ERROR_SECURE disabled for Kandelo|' \
  "$SRC_DIR/src/Makefile"
awk -v ncurses="$NCURSES_PREFIX" -v sysroot="$SYSROOT" \
    -v hackdir="$GUEST_HACKDIR" -v vardir="$GUEST_VAR_PLAYGROUND" '
  /^CFLAGS\+=-DHACKDIR=/ {
    print "CFLAGS+=-DHACKDIR=\\\"" hackdir "\\\" -DVAR_PLAYGROUND=\\\"" vardir "\\\""
    next
  }
  /^CFLAGS\+=-DCURSES_GRAPHICS/ {
    print
    print "CFLAGS+=-I" ncurses "/include/ncursesw -I" ncurses "/include -I" sysroot "/include"
    print "LFLAGS+=-L" ncurses "/lib"
    next
  }
  /^LFLAGS=-rdynamic$/ { print "LFLAGS+=-rdynamic -L" ncurses "/lib"; next }
  { print }
' "$SRC_DIR/src/Makefile" > "$SRC_DIR/src/Makefile.new"
mv "$SRC_DIR/src/Makefile.new" "$SRC_DIR/src/Makefile"
rm -f "$SRC_DIR/src/Makefile.bak"

# NetHack's host generators and parsers must finish before the target objects
# are rebuilt with the wasm SDK. Serialization patches keep their data layouts
# identical across the host and wasm32 phases.
rm -f "$SRC_DIR/dat/quest.dat" "$SRC_DIR/dat/nhdat" \
  "$SRC_DIR"/util/{makedefs,dgn_comp,lev_comp,dlb,recover}
"$MAKE" -j1 -C "$SRC_DIR/util" \
  CC="$HOST_CC" LD="$HOST_CC" YACC="$BISON -y" LEX="$FLEX" \
  makedefs dgn_comp lev_comp dlb recover
"$MAKE" -j1 -C "$SRC_DIR/dat" \
  CC="$HOST_CC" LD="$HOST_CC" YACC="$BISON -y" LEX="$FLEX" all
"$MAKE" -j1 -C "$SRC_DIR" \
  CC="$HOST_CC" LD="$HOST_CC" YACC="$BISON -y" LEX="$FLEX" dlb

touch -t 204001010101 \
  "$SRC_DIR"/util/{makedefs,dgn_comp,lev_comp,dlb,recover} \
  "$SRC_DIR"/include/{onames.h,pm.h,date.h,vis_tab.h} \
  "$SRC_DIR/dat/nhdat" 2>/dev/null || true
"$MAKE" -C "$SRC_DIR/src" clean
"$MAKE" -C "$SRC_DIR/src" \
  CC=wasm32posix-cc LINK=wasm32posix-cc \
  AR=wasm32posix-ar RANLIB=wasm32posix-ranlib nethack

mkdir -p "$OUT_DIR/runtime"
cp "$SRC_DIR/src/nethack" "$OUT_DIR/nethack.wasm"
"$FORK_INSTRUMENT" "$OUT_DIR/nethack.wasm" -o "$OUT_DIR/nethack.wasm.instr"
mv "$OUT_DIR/nethack.wasm.instr" "$OUT_DIR/nethack.wasm"
cp "$SRC_DIR/dat/nhdat" "$SRC_DIR/dat/symbols" "$SRC_DIR/dat/license" \
  "$OUT_DIR/runtime/"
chmod 0644 "$OUT_DIR/runtime/nhdat" "$OUT_DIR/runtime/symbols" \
  "$OUT_DIR/runtime/license"
