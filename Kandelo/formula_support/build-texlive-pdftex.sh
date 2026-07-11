#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage:
  build-texlive-pdftex.sh engine SOURCE HOST_BUILD CROSS_BUILD KANDELO_ROOT ZLIB LIBPNG LIBCXX PKG_CONFIG OUTPUT GUEST_PREFIX JOBS
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

snapshot_texmf_tree() {
  local tree="$1"
  local output="$2"

  (
    cd "$tree"
    find . -printf '%y\t%m\t%p\n' | LC_ALL=C sort
    find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
  ) >"$output"
}

verify_object_archive() {
  local archive="$1"
  require_file "$archive"

  local listing member
  if ! listing="$(wasm32posix-ar t "$archive")"; then
    echo "ERROR: could not inspect static archive: $archive" >&2
    exit 1
  fi
  if [[ -z "$listing" ]]; then
    echo "ERROR: static archive has no object members: $archive" >&2
    exit 1
  fi
  while IFS= read -r member; do
    if [[ -n "$member" && "$member" != *.o ]]; then
      echo "ERROR: static archive contains a non-object member: $archive: $member" >&2
      exit 1
    fi
  done <<<"$listing"
}

build_engine() {
  if [[ "$#" -ne 11 ]]; then
    usage
  fi

  local source_dir="$1"
  local host_build_dir="$2"
  local cross_build_dir="$3"
  local kandelo_root="$4"
  local zlib_prefix="$5"
  local libpng_prefix="$6"
  local libcxx_prefix="$7"
  local pkg_config="$8"
  local output="$9"
  local guest_prefix="${10}"
  local jobs="${11}"

  require_directory "$source_dir"
  require_directory "$kandelo_root"
  require_file "$zlib_prefix/lib/libz.a"
  if [[ ! -f "$libpng_prefix/lib/libpng16.a" && ! -f "$libpng_prefix/lib/libpng.a" ]]; then
    echo "ERROR: libpng static archive is missing under $libpng_prefix" >&2
    exit 1
  fi
  require_file "$libcxx_prefix/lib/libc++.a"
  require_file "$libcxx_prefix/lib/libc++abi.a"
  require_file "$libcxx_prefix/include/c++/v1/vector"
  require_file "$pkg_config"
  if [[ ! -x "$pkg_config" ]]; then
    echo "ERROR: target pkg-config parser is not executable: $pkg_config" >&2
    exit 1
  fi
  local kandelo_realpath zlib_realpath libpng_realpath libcxx_realpath
  kandelo_realpath="$(cd "$kandelo_root" && pwd -P)"
  zlib_realpath="$(cd "$zlib_prefix" && pwd -P)"
  libpng_realpath="$(cd "$libpng_prefix" && pwd -P)"
  libcxx_realpath="$(cd "$libcxx_prefix" && pwd -P)"

  # Use only the checkout-local SDK and the native tools declared by Kandelo's
  # dev shell. This script never downloads sources or resolves dependencies.
  # shellcheck source=/dev/null
  source "$kandelo_root/sdk/activate.sh"
  export LC_ALL=C
  export TZ=UTC
  export SOURCE_DATE_EPOCH=1741392000
  export FORCE_SOURCE_DATE=1
  export ZERO_AR_DATE=1

  # TeX Live recurses into LuaJIT's configure even when every Lua engine is
  # disabled. Removing execute permission is upstream's supported skip signal
  # for a source subdirectory that is intentionally outside this build.
  chmod -x "$source_dir/libs/luajit/configure"

  mkdir -p "$host_build_dir"
  pushd "$host_build_dir" >/dev/null
  unset CONFIG_SITE
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

  make -j"$jobs" \
    CONF_SUBDIRS="texk/kpathsea" \
    MAKE_SUBDIRS="texk/kpathsea" recurse
  make -C libs -j"$jobs" \
    CONF_SUBDIRS="zlib libpng xpdf" \
    MAKE_SUBDIRS="zlib libpng xpdf" recurse
  make -C libs/xpdf -j"$jobs"
  make -C texk -j"$jobs" \
    CONF_SUBDIRS="web2c" \
    MAKE_SUBDIRS= recurse
  make -C texk/web2c -j"$jobs" pdftex otangle
  popd >/dev/null

  local host_web2c="$host_build_dir/texk/web2c"
  require_file "$host_web2c/pdftex"
  require_file "$host_web2c/otangle"

  mkdir -p "$cross_build_dir"
  pushd "$cross_build_dir" >/dev/null
  local sdk_config_site="$kandelo_root/sdk/config.site"
  require_file "$sdk_config_site"
  if ! grep -Fq 'ac_cv_func_mprotect=${ac_cv_func_mprotect=no}' "$sdk_config_site"; then
    echo "ERROR: Kandelo SDK config.site does not preserve mprotect=no" >&2
    exit 1
  fi
  export CONFIG_SITE="$sdk_config_site"
  if [[ "$CONFIG_SITE" != "$kandelo_root/sdk/config.site" ]]; then
    echo "ERROR: cross configure did not select Kandelo's SDK config.site" >&2
    exit 1
  fi
  echo "==> Cross CONFIG_SITE: $CONFIG_SITE (TeX overrides: none)"
  export PATH="$host_web2c:$PATH"
  unset PKG_CONFIG_PATH PKG_CONFIG_SYSROOT_DIR
  export PKG_CONFIG_LIBDIR="$libpng_prefix/lib/pkgconfig:$zlib_prefix/lib/pkgconfig"
  local zlib_pkg_flags libpng_pkg_flags
  zlib_pkg_flags="$("$pkg_config" zlib --cflags --libs)"
  libpng_pkg_flags="$("$pkg_config" libpng --cflags --libs)"
  if [[ "$zlib_pkg_flags" != *"$zlib_realpath"* || "$libpng_pkg_flags" != *"$libpng_realpath"* ]]; then
    echo "ERROR: target pkg-config did not resolve the declared zlib/libpng kegs" >&2
    exit 1
  fi
  echo "==> Target zlib pkg-config: $zlib_pkg_flags"
  echo "==> Target libpng pkg-config: $libpng_pkg_flags"
  local prefix_maps
  prefix_maps="-ffile-prefix-map=$source_dir=. -fdebug-prefix-map=$source_dir=. -fmacro-prefix-map=$source_dir=."
  prefix_maps+=" -ffile-prefix-map=$host_build_dir=. -fdebug-prefix-map=$host_build_dir=. -fmacro-prefix-map=$host_build_dir=."
  prefix_maps+=" -ffile-prefix-map=$cross_build_dir=. -fdebug-prefix-map=$cross_build_dir=. -fmacro-prefix-map=$cross_build_dir=."
  prefix_maps+=" -ffile-prefix-map=$kandelo_root=/usr/src/kandelo -fdebug-prefix-map=$kandelo_root=/usr/src/kandelo -fmacro-prefix-map=$kandelo_root=/usr/src/kandelo"
  if [[ "$kandelo_realpath" != "$kandelo_root" ]]; then
    prefix_maps+=" -ffile-prefix-map=$kandelo_realpath=/usr/src/kandelo -fdebug-prefix-map=$kandelo_realpath=/usr/src/kandelo -fmacro-prefix-map=$kandelo_realpath=/usr/src/kandelo"
  fi
  prefix_maps+=" -ffile-prefix-map=$zlib_prefix=/opt/kandelo-deps/zlib -fdebug-prefix-map=$zlib_prefix=/opt/kandelo-deps/zlib -fmacro-prefix-map=$zlib_prefix=/opt/kandelo-deps/zlib"
  if [[ "$zlib_realpath" != "$zlib_prefix" ]]; then
    prefix_maps+=" -ffile-prefix-map=$zlib_realpath=/opt/kandelo-deps/zlib -fdebug-prefix-map=$zlib_realpath=/opt/kandelo-deps/zlib -fmacro-prefix-map=$zlib_realpath=/opt/kandelo-deps/zlib"
  fi
  prefix_maps+=" -ffile-prefix-map=$libpng_prefix=/opt/kandelo-deps/libpng -fdebug-prefix-map=$libpng_prefix=/opt/kandelo-deps/libpng -fmacro-prefix-map=$libpng_prefix=/opt/kandelo-deps/libpng"
  if [[ "$libpng_realpath" != "$libpng_prefix" ]]; then
    prefix_maps+=" -ffile-prefix-map=$libpng_realpath=/opt/kandelo-deps/libpng -fdebug-prefix-map=$libpng_realpath=/opt/kandelo-deps/libpng -fmacro-prefix-map=$libpng_realpath=/opt/kandelo-deps/libpng"
  fi
  prefix_maps+=" -ffile-prefix-map=$libcxx_prefix=/opt/kandelo-deps/libcxx -fdebug-prefix-map=$libcxx_prefix=/opt/kandelo-deps/libcxx -fmacro-prefix-map=$libcxx_prefix=/opt/kandelo-deps/libcxx"
  if [[ "$libcxx_realpath" != "$libcxx_prefix" ]]; then
    prefix_maps+=" -ffile-prefix-map=$libcxx_realpath=/opt/kandelo-deps/libcxx -fdebug-prefix-map=$libcxx_realpath=/opt/kandelo-deps/libcxx -fmacro-prefix-map=$libcxx_realpath=/opt/kandelo-deps/libcxx"
  fi
  local common_flags
  common_flags="-O2 -gline-tables-only -fdebug-compilation-dir=. $prefix_maps -I$zlib_prefix/include -I$libpng_prefix/include"
  local cxx_flags
  cxx_flags="$common_flags -nostdinc++ -isystem $libcxx_prefix/include/c++/v1"
  export kpse_cv_cxx_hack=ok
  export kpse_cv_cxx_flags="$libcxx_prefix/lib/libc++.a $libcxx_prefix/lib/libc++abi.a"
  "$source_dir/configure" \
    --host=wasm32-unknown-linux-musl \
    --build="$(cc -dumpmachine)" \
    --prefix="$guest_prefix" \
    --disable-all-pkgs \
    --enable-web2c \
    --enable-pdftex \
    --disable-native-texlive-build \
    --enable-cxx-runtime-hack \
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
    PKG_CONFIG="$pkg_config" \
    BUILDCC=cc \
    BUILDCXX=c++ \
    BUILDCFLAGS=-O2 \
    BUILDCPPFLAGS= \
    BUILDLDFLAGS= \
    CFLAGS="$common_flags" \
    CXXFLAGS="$cxx_flags" \
    LDFLAGS="-L$libcxx_prefix/lib -L$zlib_prefix/lib -L$libpng_prefix/lib"

  make -j"$jobs" \
    CONF_SUBDIRS="texk/kpathsea" \
    MAKE_SUBDIRS="texk/kpathsea" \
    AR=wasm32posix-ar RANLIB=wasm32posix-ranlib recurse
  verify_object_archive "$cross_build_dir/texk/kpathsea/.libs/libkpathsea.a"
  make -C libs -j"$jobs" \
    CONF_SUBDIRS="xpdf" \
    MAKE_SUBDIRS="xpdf" \
    AR=wasm32posix-ar RANLIB=wasm32posix-ranlib recurse
  make -C libs/xpdf -j"$jobs" AR=wasm32posix-ar RANLIB=wasm32posix-ranlib
  verify_object_archive "$cross_build_dir/libs/xpdf/libxpdf.a"
  make -C texk -j"$jobs" \
    CONF_SUBDIRS="web2c" \
    MAKE_SUBDIRS= \
    AR=wasm32posix-ar RANLIB=wasm32posix-ranlib recurse
  require_file "$cross_build_dir/texk/web2c/CXXLD.sh"
  if [[ ! -x "$cross_build_dir/texk/web2c/CXXLD.sh" ]] ||
     ! grep -Fq "$kpse_cv_cxx_flags" "$cross_build_dir/texk/web2c/CXXLD.sh"; then
    echo "ERROR: web2c did not generate the pinned C++ program-link wrapper" >&2
    exit 1
  fi
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
  export TZ=UTC
  export SOURCE_DATE_EPOCH=1741392000
  export FORCE_SOURCE_DATE=1
  export ZERO_AR_DATE=1
  export TEXMFDIST="$texmf_dist"
  export TEXMF="$texmf_dist"
  export TEXMFCNF="$texmf_dist/web2c"
  export TEXMFCONFIG="$format_dir/config"
  export TEXMFVAR="$format_dir/var"
  export TEXFORMATS="$format_dir"
  export HOME="$format_dir/home"
  mkdir -p "$format_dir" "$TEXMFCONFIG" "$TEXMFVAR" "$HOME"

  pushd "$format_dir" >/dev/null
  "$host_pdftex" -ini -interaction=nonstopmode -halt-on-error \
    -jobname=pdftex -progname=pdftex \
    -translate-file=cp227.tcx "*pdfetex.ini"
  "$host_pdftex" -ini -interaction=nonstopmode -halt-on-error \
    -jobname=pdflatex -progname=pdflatex \
    -translate-file=cp227.tcx "*pdflatex.ini"
  "$host_pdftex" -ini -interaction=nonstopmode -halt-on-error \
    -jobname=latex -progname=pdflatex \
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
  local texmf_before="$format_dir/texmf-before-smoke.manifest"
  local texmf_after="$format_dir/texmf-after-smoke.manifest"
  local smoke_log="$output_dir/pdftex-smoke.log"
  local latex_smoke_log="$output_dir/latex-smoke.log"
  snapshot_texmf_tree "$texmf_dist" "$texmf_before"
  pushd "$output_dir" >/dev/null
  "$host_pdftex" \
    -progname=pdflatex \
    -fmt=pdflatex \
    -recorder \
    -interaction=nonstopmode \
    -halt-on-error \
    -output-format=pdf \
    -output-directory="$output_dir" \
    "$output_dir/input.tex" 2>&1 | tee "$smoke_log"

  cp "$fixture" "$output_dir/latex-input.tex"
  "$host_pdftex" \
    -progname=latex \
    -fmt=latex \
    -recorder \
    -interaction=nonstopmode \
    -halt-on-error \
    -jobname=latex-input \
    -output-format=dvi \
    -output-directory="$output_dir" \
    "$output_dir/latex-input.tex" 2>&1 | tee "$latex_smoke_log"
  popd >/dev/null

  if grep -Fq 'pdfTeX warning:' "$smoke_log" || grep -Fq 'pdfTeX warning:' "$latex_smoke_log"; then
    echo "ERROR: host pdfTeX smoke emitted a warning" >&2
    exit 1
  fi
  if grep -Eiq 'mktex(pk|tfm)|fonts/pk/' "$smoke_log" ||
     grep -Eiq 'mktex(pk|tfm)|fonts/pk/' "$latex_smoke_log"; then
    echo "ERROR: host pdfTeX smoke fell back to generated or bitmap fonts" >&2
    exit 1
  fi
  snapshot_texmf_tree "$texmf_dist" "$texmf_after"
  if ! cmp -s "$texmf_before" "$texmf_after"; then
    echo "ERROR: host pdfTeX smoke mutated texmf-dist" >&2
    diff -u "$texmf_before" "$texmf_after" >&2 || true
    exit 1
  fi

  require_file "$output_dir/input.pdf"
  require_file "$output_dir/input.fls"
  require_file "$output_dir/latex-input.dvi"
  require_file "$output_dir/latex-input.fls"
  if [[ "$(head -c 5 "$output_dir/input.pdf")" != "%PDF-" ]]; then
    echo "ERROR: host pdfTeX smoke output is not a PDF" >&2
    exit 1
  fi
  if ! tail -c 1024 "$output_dir/input.pdf" | grep -a -q '%%EOF'; then
    echo "ERROR: host pdfTeX smoke output has no PDF EOF marker" >&2
    exit 1
  fi
  if [[ "$(od -An -tu1 -N2 "$output_dir/latex-input.dvi" | xargs)" != "247 2" ]]; then
    echo "ERROR: host LaTeX smoke output has no DVI preamble" >&2
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
