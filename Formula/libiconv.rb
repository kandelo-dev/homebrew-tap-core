require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Libiconv < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/libiconv".freeze

  desc "Character-set conversion library and CLI for Kandelo"
  homepage "https://www.gnu.org/software/libiconv/"
  url "https://ftpmirror.gnu.org/gnu/libiconv/libiconv-1.19.tar.gz"
  mirror "https://ftp.gnu.org/gnu/libiconv/libiconv-1.19.tar.gz"
  sha256 "88dd96a8c0464eca144fc791ae60cd31cd8ee78321e67397e25fc095c4a19aa6"
  license all_of: ["GPL-3.0-or-later", "LGPL-2.0-or-later"]

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  on_macos do
    keg_only :provided_by_macos
  end

  skip_clean "bin/iconv", "lib/libcharset.a", "lib/libiconv.a"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/libiconv-#{version}"
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
      ENV["CFLAGS"] = ["-O2", "-fPIC", "-gline-tables-only", *prefix_maps].join(" ")

      system kandelo_configure(root),
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--disable-shared",
        "--enable-static",
        "--enable-extra-encodings",
        "--disable-nls",
        "--disable-dependency-tracking"
      system "make", "-j#{ENV.make_jobs}"

      stage = buildpath/"kandelo-stage"
      system "make", "install", "DESTDIR=#{stage}"
      staged_prefix = stage/GUEST_OPT_PREFIX.delete_prefix("/")
      odie "libiconv did not install into the guest opt prefix" unless staged_prefix.directory?
      kandelo_validate_wasm_artifact(staged_prefix/"bin/iconv", fork: :forbidden)
      prefix.install staged_prefix.children
    end
  end

  test do
    assert_path_exists bin/"iconv"
    assert_path_exists include/"iconv.h"
    assert_path_exists include/"localcharset.h"
    assert_path_exists lib/"libiconv.a"
    assert_path_exists lib/"libcharset.a"

    input = "caf\xE9\n".b
    expected = "caf\xC3\xA9\n".b
    assert_equal expected,
      kandelo_run_wasm(bin/"iconv", ["-f", "ISO-8859-1", "-t", "UTF-8"], stdin: input).b
    assert_equal input,
      kandelo_run_wasm(bin/"iconv", ["-f", "UTF-8", "-t", "ISO-8859-1"], stdin: expected).b
    assert_empty kandelo_run_wasm(
      bin/"iconv", ["-f", "UTF-8", "-t", "UTF-8"], stdin: "\xFF".b, expected_status: 1
    )

    encodings = kandelo_run_wasm(bin/"iconv", ["--list"])
    assert_match(/ISO-8859-1/, encodings)
    assert_match(/UTF-16LE/, encodings)

    kandelo_activate_sdk!
    kandelo_activate_sysroot!
    smoke_c = testpath/"libiconv-smoke.c"
    smoke_wasm = testpath/"libiconv-smoke.wasm"
    smoke_c.write <<~C
      #include <errno.h>
      #include <iconv.h>
      #include <stdio.h>

      int main(void) {
        char input[] = { 'c', 'a', 'f', (char)0xe9 };
        char output[16] = { 0 };
        char *in_ptr = input;
        char *out_ptr = output;
        size_t in_left = sizeof(input);
        size_t out_left = sizeof(output);
        iconv_t cd = iconv_open("UTF-8", "ISO-8859-1");

        if (cd == (iconv_t)-1) return 2;
        if (iconv(cd, &in_ptr, &in_left, &out_ptr, &out_left) == (size_t)-1) return 3;
        if (iconv_close(cd) != 0) return 4;
        if (in_left != 0 || output[3] != (char)0xc3 || output[4] != (char)0xa9) return 5;
        printf("libiconv %02x%02x ok\\n", (unsigned char)output[3], (unsigned char)output[4]);
        return 0;
      }
    C
    system kandelo_cc, smoke_c, "-I#{include}", "-L#{lib}", "-liconv", "-o", smoke_wasm
    assert_equal "libiconv c3a9 ok\n", kandelo_run_wasm(smoke_wasm, [])
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 1
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "d9ce9c3641115bd99a419a0baec1e8df218bf15096036d081705440d79cba769"
  end

end
