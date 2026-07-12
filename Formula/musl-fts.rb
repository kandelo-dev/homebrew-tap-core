require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class MuslFts < Formula
  include KandeloFormulaSupport

  desc "BSD file-hierarchy traversal library for Kandelo"
  homepage "https://github.com/pullmoll/musl-fts"
  url "https://github.com/pullmoll/musl-fts/archive/refs/tags/v1.2.7.tar.gz"
  sha256 "49ae567a96dbab22823d045ffebe0d6b14b9b799925e9ca9274d47d26ff482a6"
  license "BSD-3-Clause"
  keg_only "macOS provides fts.h and the fts API in libc"

  depends_on "autoconf" => :build
  depends_on "automake" => :build
  depends_on "libtool" => :build
  depends_on "pkgconf" => :build
  depends_on "binaryen" => :test
  depends_on "wabt" => :test

  skip_clean "lib/libfts.a"

  def install
    kandelo_require_arch!("wasm32", "wasm64")

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/musl-fts-#{version}"
      prefix_maps = {
        buildpath.to_s => stable_source,
        root.to_s      => "/usr/src/kandelo",
        "/nix/store"   => "/usr/src/toolchain",
      }.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end
      ENV["CC"] = "#{kandelo_arch}posix-cc"
      ENV["CFLAGS"] = [
        "-O2",
        "-gline-tables-only",
        "-fdebug-compilation-dir=#{stable_source}",
        *prefix_maps,
      ].join(" ")
      ENV.prepend_path "ACLOCAL_PATH", Formula["pkgconf"].opt_share/"aclocal"

      system "autoreconf", "-fi"
      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-shared",
        "--enable-static"
      system "make", "-j#{ENV.make_jobs}"
      system "make", "install"
    end

    rm lib/"libfts.la" if (lib/"libfts.la").exist?
    inreplace lib/"pkgconfig/musl-fts.pc" do |s|
      s.gsub!(/^prefix=.*/, "prefix=#{opt_prefix}")
      s.gsub!(/^exec_prefix=.*/, "exec_prefix=${prefix}")
      s.gsub!(/^libdir=.*/, "libdir=${exec_prefix}/lib")
      s.gsub!(/^includedir=.*/, "includedir=${prefix}/include")
    end
  end

  test do
    assert_path_exists include/"fts.h"
    assert_path_exists lib/"libfts.a"
    assert_path_exists lib/"pkgconfig/musl-fts.pc"
    assert_includes (lib/"pkgconfig/musl-fts.pc").read, "prefix=#{opt_prefix}"

    archive = (lib/"libfts.a").binread
    [prefix.to_s, kandelo_require_root!, "/private/tmp/", "/nix/store/"].each do |path|
      refute_includes archive, path
    end
    refute_match(%r{/Users/[^/]+/}, archive)

    tree = testpath/"tree"
    (tree/"nested").mkpath
    (tree/"root.txt").write "root\n"
    (tree/"nested/child.txt").write "nested\n"
    ln_s "root.txt", tree/"root-link"

    source = testpath/"fts-smoke.c"
    wasm = testpath/"fts-smoke.wasm"
    source.write <<~C
      #include <fts.h>
      #include <stdio.h>

      int main(void) {
        char *paths[] = { "tree", NULL };
        FTS *tree = fts_open(paths, FTS_PHYSICAL, NULL);
        FTSENT *entry;
        int directories = 0;
        int files = 0;
        int links = 0;

        if (tree == NULL) return 1;
        while ((entry = fts_read(tree)) != NULL) {
          switch (entry->fts_info) {
          case FTS_D:
            ++directories;
            break;
          case FTS_F:
            ++files;
            break;
          case FTS_SL:
          case FTS_SLNONE:
            ++links;
            break;
          case FTS_DNR:
          case FTS_ERR:
          case FTS_NS:
            return 2;
          default:
            break;
          }
        }
        if (fts_close(tree) != 0) return 3;
        printf("directories=%d files=%d links=%d\\n", directories, files, links);
        return 0;
      }
    C

    kandelo_wasm_build do
      system kandelo_cc, source, "-I#{include}", lib/"libfts.a", "-o", wasm
      kandelo_validate_wasm_artifact(wasm, fork: :forbidden)
    end
    assert_equal "directories=2 files=2 links=1\n",
      kandelo_run_wasm(wasm, [], env: { "KERNEL_CWD" => testpath })
  end
end
