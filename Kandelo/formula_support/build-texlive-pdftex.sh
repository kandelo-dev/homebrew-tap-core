#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage:
  build-texlive-pdftex.sh engine SOURCE HOST_BUILD CROSS_BUILD KANDELO_ROOT ZLIB LIBPNG OUTPUT GUEST_PREFIX JOBS
  build-texlive-pdftex.sh formats HOST_PDFTEX TEXMF_DIST FORMAT_DIR FIXTURE OUTPUT_DIR
EOF
  exit 2
}

require_directory() {
  if [[ ! -d "$1" ]]; then
    echo "ERROR: required directory does not exist: $1" >&2
    exit 1
  fi
}

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "ERROR: required file does not exist: $1" >&2
    exit 1
  fi
}

build_engine() {
  if [[ "$#" -ne 9 ]]; then
    usage
  fi

  local source_dir="$1"
  local host_build_dir="$2"
  local cross_build_dir="$3"
  local kandelo_root="$4"
  local zlib_prefix="$5"
  local libpng_prefix="$6"
  local output="$7"
  local guest_prefix="$8"
  local jobs="$9"

  require_directory "$source_dir"
  require_directory "$kandelo_root"
  require_file "$zlib_prefix/lib/libz.a"
  if [[ ! -f "$libpng_prefix/lib/libpng16.a" && ! -f "$libpng_prefix/lib/libpng.a" ]]; then
    echo "ERROR: libpng static archive is missing under $libpng_prefix" >&2
    exit 1
  fi

  # Use only the checkout-local SDK and the native tools declared by Kandelo's
  # dev shell. This script never downloads sources or resolves dependencies.
  # shellcheck source=/dev/null
  source "$kandelo_root/sdk/activate.sh"

  # TeX Live recurses into LuaJIT's configure even when every Lua engine is
  # disabled. Removing execute permission is upstream's supported skip signal
  # for a source subdirectory that is intentionally outside this build.
  chmod -x "$source_dir/libs/luajit/configure"

  mkdir -p "$host_build_dir"
  pushd "$host_build_dir" >/dev/null
  "$source_dir/configure" \
    --disable-all-pkgs \
    --enable-web2c \
    --enable-pdftex \
    --disable-luatex \
    --disable-luajittex \
    --disable-luahbtex \
    --disable-luajithbtex \
    --disable-xetex \
    --disable-aleph \
    --disable-euptex \
    --disable-hitex \
    --disable-mp \
    --disable-mflua \
    --disable-mfluajit \
    --disable-synctex \
    --without-x \
    --disable-shared \
    --enable-static

  make -j"$jobs" all-local
  make -C libs recurse
  make -C libs/xpdf -j"$jobs"
  make -C texk -j"$jobs"
  make -C texk/web2c -j"$jobs" pdftex otangle
  popd >/dev/null

  local host_web2c="$host_build_dir/texk/web2c"
  require_file "$host_web2c/pdftex"
  require_file "$host_web2c/otangle"

  mkdir -p "$cross_build_dir"
  cat >"$cross_build_dir/config.site" <<'EOF'
ac_cv_func_strerror_r=no
ac_cv_func_working_strerror_r=no
kpse_cv_have_decl_putenv=yes
kpse_cv_have_decl_getcwd=yes
enable_aleph=no
enable_xetex=no
enable_omfonts=no
EOF

  pushd "$cross_build_dir" >/dev/null
  export CONFIG_SITE="$cross_build_dir/config.site"
  export PATH="$host_web2c:$PATH"
  "$source_dir/configure" \
    --host=wasm32-unknown-none \
    --build="$(cc -dumpmachine)" \
    --prefix="$guest_prefix" \
    --disable-all-pkgs \
    --enable-pdftex \
    --disable-native-texlive-build \
    --disable-aleph \
    --disable-xetex \
    --disable-omfonts \
    --disable-synctex \
    --without-x \
    --disable-shared \
    --enable-static \
    --with-system-zlib \
    --with-system-libpng \
    CC=wasm32posix-cc \
    CXX=wasm32posix-c++ \
    AR=wasm32posix-ar \
    RANLIB=wasm32posix-ranlib \
    PKG_CONFIG=wasm32posix-pkg-config \
    BUILDCC=cc \
    BUILDCXX=c++ \
    BUILDCFLAGS=-O2 \
    BUILDCPPFLAGS= \
    BUILDLDFLAGS= \
    CFLAGS="-O2 -gline-tables-only -fdebug-compilation-dir=. -I$zlib_prefix/include -I$libpng_prefix/include" \
    LDFLAGS="-L$zlib_prefix/lib -L$libpng_prefix/lib" \
    ZLIB_CFLAGS="-I$zlib_prefix/include" \
    ZLIB_LIBS="-L$zlib_prefix/lib -lz" \
    LIBPNG_CFLAGS="-I$libpng_prefix/include" \
    LIBPNG_LIBS="-L$libpng_prefix/lib -lpng -lz"

  make -j"$jobs" AR=wasm32posix-ar RANLIB=wasm32posix-ranlib
  make -C texk/kpathsea -j"$jobs" AR=wasm32posix-ar RANLIB=wasm32posix-ranlib
  make -C libs/xpdf -j"$jobs" AR=wasm32posix-ar RANLIB=wasm32posix-ranlib
  make -C texk/web2c -j"$jobs" pdftex AR=wasm32posix-ar RANLIB=wasm32posix-ranlib
  popd >/dev/null

  require_file "$cross_build_dir/texk/web2c/pdftex"
  mkdir -p "$(dirname "$output")"
  cp "$cross_build_dir/texk/web2c/pdftex" "$output"
}

build_formats() {
  if [[ "$#" -ne 5 ]]; then
    usage
  fi

  local host_pdftex="$1"
  local texmf_dist="$2"
  local format_dir="$3"
  local fixture="$4"
  local output_dir="$5"

  require_file "$host_pdftex"
  require_directory "$texmf_dist"
  require_file "$fixture"

  export LC_ALL=C
  export SOURCE_DATE_EPOCH=1741392000
  export FORCE_SOURCE_DATE=1
  export TEXMFDIST="$texmf_dist"
  export TEXMF="$texmf_dist"
  export TEXMFCNF="$texmf_dist/web2c"
  export TEXMFCONFIG="$format_dir/config"
  export TEXMFVAR="$format_dir/var"
  export TEXFORMATS="$format_dir"
  export HOME="$format_dir/home"
  mkdir -p "$format_dir" "$TEXMFCONFIG" "$TEXMFVAR" "$HOME"

  pushd "$format_dir" >/dev/null
  "$host_pdftex" -ini -jobname=pdftex -progname=pdftex \
    -translate-file=cp227.tcx "*pdfetex.ini"
  "$host_pdftex" -ini -jobname=pdflatex -progname=pdflatex \
    -translate-file=cp227.tcx "*pdflatex.ini"
  "$host_pdftex" -ini -jobname=latex -progname=pdflatex \
    -translate-file=cp227.tcx "*latex.ini"
  popd >/dev/null

  require_file "$format_dir/pdftex.fmt"
  require_file "$format_dir/pdflatex.fmt"
  require_file "$format_dir/latex.fmt"
  mkdir -p "$texmf_dist/web2c/pdftex"
  cp "$format_dir/pdftex.fmt" "$format_dir/pdflatex.fmt" \
    "$format_dir/latex.fmt" "$texmf_dist/web2c/pdftex/"

  mkdir -p "$output_dir"
  cp "$fixture" "$output_dir/input.tex"
  pushd "$output_dir" >/dev/null
  "$host_pdftex" \
    -progname=pdflatex \
    -fmt=pdflatex \
    -recorder \
    -interaction=nonstopmode \
    -halt-on-error \
    -output-format=pdf \
    -output-directory="$output_dir" \
    "$output_dir/input.tex"
  popd >/dev/null

  require_file "$output_dir/input.pdf"
  require_file "$output_dir/input.fls"
  if [[ "$(head -c 5 "$output_dir/input.pdf")" != "%PDF-" ]]; then
    echo "ERROR: host pdfTeX smoke output is not a PDF" >&2
    exit 1
  fi
  if ! tail -c 1024 "$output_dir/input.pdf" | grep -a -q '%%EOF'; then
    echo "ERROR: host pdfTeX smoke output has no PDF EOF marker" >&2
    exit 1
  fi
}

mode="${1:-}"
if [[ -z "$mode" ]]; then
  usage
fi
shift

case "$mode" in
  engine) build_engine "$@" ;;
  formats) build_formats "$@" ;;
  *) usage ;;
esac
