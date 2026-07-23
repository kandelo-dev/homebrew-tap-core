require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Pcre2 < Formula
  include KandeloFormulaSupport

  desc "Perl-compatible regular expression library and tools for Kandelo"
  homepage "https://www.pcre.org/"
  url "https://github.com/PCRE2Project/pcre2/releases/download/pcre2-10.44/pcre2-10.44.tar.gz"
  sha256 "86b9cb0aa3bcb7994faa88018292bc704cdbb708e785f7c74352ff6ea7d3175b"
  license "BSD-3-Clause"
  revision 1

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::PkgconfRequirement => [:build, :test]
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin", "lib/libpcre2-8.a", "lib/libpcre2-16.a", "lib/libpcre2-32.a",
             "lib/libpcre2-posix.a"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/pcre2-#{version}"
      prefix_maps = {
        buildpath.to_s => stable_source,
      }.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end
      ENV["CFLAGS"] = [
        "-O2", "-gline-tables-only", "-fdebug-compilation-dir=#{stable_source}", *prefix_maps
      ].join(" ")

      system kandelo_configure(root), *kandelo_std_configure_args,
        "--disable-shared",
        "--enable-static",
        "--enable-pcre2-16",
        "--enable-pcre2-32",
        "--enable-unicode",
        "--disable-jit",
        # The authoritative source-kind registry contract has no target dependencies.
        # Keep compressed-input support out of this first formula contract;
        # tap main does not yet publish libbz2's headers/static library.
        "--disable-pcre2grep-libz",
        "--disable-pcre2grep-libbz2",
        "--disable-pcre2test-libedit",
        "--disable-pcre2test-libreadline",
        "--disable-dependency-tracking"
      system "make", "-j#{ENV.make_jobs}"

      pcre2grep = buildpath/"pcre2grep"
      instrumented = buildpath/"pcre2grep.instrumented"
      system "#{root}/scripts/run-wasm-fork-instrument.sh", pcre2grep, "-o", instrumented

      pcre2test = buildpath/"pcre2test"
      kandelo_validate_wasm_artifact(instrumented, fork: :required)
      kandelo_validate_wasm_artifact(pcre2test, fork: :auto)
      mv instrumented, pcre2grep

      system "make", "install"
    end

    stable_metadata = [bin/"pcre2-config", *lib.glob("pkgconfig/*.pc")]
    stable_metadata.each { |path| inreplace path, prefix, opt_prefix }
  end

  test do
    %w[8 16 32].each do |width|
      assert_path_exists lib/"libpcre2-#{width}.a"
      assert_path_exists lib/"pkgconfig/libpcre2-#{width}.pc"
    end
    assert_path_exists lib/"libpcre2-posix.a"
    assert_path_exists lib/"pkgconfig/libpcre2-posix.pc"
    assert_path_exists include/"pcre2.h"
    assert_path_exists include/"pcre2posix.h"
    assert_path_exists bin/"pcre2grep"
    assert_path_exists bin/"pcre2test"
    assert_path_exists bin/"pcre2-config"

    stable_metadata = [bin/"pcre2-config", *lib.glob("pkgconfig/*.pc")]
    stable_metadata.each do |path|
      contents = path.read
      assert_includes contents, opt_prefix.to_s
      refute_includes contents, prefix.to_s
    end
    assert_equal opt_prefix.to_s, Utils.safe_popen_read(bin/"pcre2-config", "--prefix").strip
    runtime_artifacts = [bin/"pcre2grep", bin/"pcre2test", *lib.glob("libpcre2*.a")]
    runtime_artifacts.each do |path|
      contents = File.binread(path)
      refute_includes contents, prefix.to_s
      refute_includes contents, "/private/tmp/"
      refute_includes contents, "/Users/"
      refute_includes contents, "/nix/store/"
    end

    version_output = kandelo_run_wasm(bin/"pcre2grep", ["--version"])
    assert_match(/pcre2grep version 10\.44/, version_output)

    config_output = kandelo_run_wasm(bin/"pcre2test", ["-C"])
    assert_match(/PCRE2 version 10\.44/, config_output)
    %w[8 16 32].each { |width| assert_match(/#{width}-bit support/, config_output) }
    assert_match(/UTF and UCP support/, config_output)
    assert_match(/No just-in-time compiler support/, config_output)

    unicode_input = "Gr\u00fc\u00dfe\u{1F680}\n123\u{1F680}\n\u6771\u4eac\u{1F680}\n"
    assert_equal "Gr\u00fc\u00dfe\n\u6771\u4eac\n", kandelo_run_wasm(
      bin/"pcre2grep", ["-u", "-o1", "^(\\p{L}+)\\x{1F680}$"], stdin: unicode_input
    )

    # /bin/sh is Kandelo base-system state supplied by dash, not a formula edge.
    callout_pattern = 'abc(?C"/bin/sh|-c|printf callout-ok")'
    assert_equal "callout-okabc\n", kandelo_run_wasm(
      bin/"pcre2grep", [callout_pattern], stdin: "abc\n"
    )

    source = testpath/"pcre2-consumer.c"
    wasm = testpath/"pcre2-consumer.wasm"
    source.write <<~C
      #define PCRE2_CODE_UNIT_WIDTH 8
      #include <pcre2.h>
      #include <pcre2posix.h>
      #include <stdint.h>
      #include <stdio.h>
      #include <string.h>

      int main(void) {
        static const PCRE2_UCHAR pattern[] =
          "^(?<word>\\\\p{L}+)\\\\x{1F680}$";
        static const PCRE2_UCHAR subject[] =
          "Gr\\xc3\\xbc\\xc3\\x9f" "e\\xf0\\x9f\\x9a\\x80";
        int error_code;
        int unicode = 0;
        PCRE2_SIZE error_offset;
        pcre2_code *code;
        pcre2_match_data *match_data;
        PCRE2_SIZE *ovector;
        regex_t posix;

        if (pcre2_config(PCRE2_CONFIG_UNICODE, &unicode) != 0 || unicode != 1) return 1;
        code = pcre2_compile(pattern, PCRE2_ZERO_TERMINATED, PCRE2_UTF | PCRE2_UCP,
          &error_code, &error_offset, NULL);
        if (code == NULL) return 2;
        match_data = pcre2_match_data_create_from_pattern(code, NULL);
        if (match_data == NULL) return 3;
        if (pcre2_match(code, subject, PCRE2_ZERO_TERMINATED, 0, 0, match_data, NULL) != 2) return 4;
        ovector = pcre2_get_ovector_pointer(match_data);
        if (ovector[0] != 0 || ovector[1] != 11 || ovector[2] != 0 || ovector[3] != 7) return 5;

        if (regcomp(&posix, "^Kandelo[[:space:]][0-9]+$", REG_EXTENDED) != 0) return 6;
        if (regexec(&posix, "Kandelo 2026", 0, NULL, 0) != 0) return 7;
        regfree(&posix);
        pcre2_match_data_free(match_data);
        pcre2_code_free(code);
        puts("pcre2-unicode-ok:0-7");
        return 0;
      }
    C

    kandelo_wasm_build do
      ENV["PKG_CONFIG_LIBDIR"] = lib/"pkgconfig"
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")
      pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
      flags = shell_output("#{pkgconf} --static --cflags --libs libpcre2-posix").split
      %w[-lpcre2-posix -lpcre2-8].each { |flag| assert_includes flags, flag }
      assert_includes flags, "-I#{opt_include}"
      assert_includes flags, "-L#{opt_lib}"
      refute_includes flags.join(" "), prefix.to_s
      system kandelo_cc, source, *flags, "-o", wasm
    end
    assert_equal "pcre2-unicode-ok:0-7\n", kandelo_run_wasm(wasm, [])

    %w[16 32].each do |width|
      width_source = testpath/"pcre2-#{width}-consumer.c"
      width_wasm = testpath/"pcre2-#{width}-consumer.wasm"
      width_source.write <<~C
        #define PCRE2_CODE_UNIT_WIDTH #{width}
        #include <pcre2.h>
        #include <stdio.h>

        int main(void) {
          static const PCRE2_UCHAR pattern[] = {'^', 'K', '$', 0};
          static const PCRE2_UCHAR subject[] = {'K', 0};
          int error_code;
          int unicode = 0;
          PCRE2_SIZE error_offset;
          pcre2_code *code;
          pcre2_match_data *match_data;

          if (pcre2_config(PCRE2_CONFIG_UNICODE, &unicode) != 0 || unicode != 1) return 1;
          code = pcre2_compile(pattern, PCRE2_ZERO_TERMINATED, PCRE2_UTF,
            &error_code, &error_offset, NULL);
          if (code == NULL) return 2;
          match_data = pcre2_match_data_create_from_pattern(code, NULL);
          if (match_data == NULL) return 3;
          if (pcre2_match(code, subject, 1, 0, 0, match_data, NULL) != 1) return 4;
          pcre2_match_data_free(match_data);
          pcre2_code_free(code);
          puts("pcre2-#{width}-ok");
          return 0;
        }
      C
      kandelo_wasm_build do
        ENV["PKG_CONFIG_LIBDIR"] = lib/"pkgconfig"
        ENV.delete("PKG_CONFIG_PATH")
        ENV.delete("PKG_CONFIG_SYSROOT_DIR")
        pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
        flags = shell_output("#{pkgconf} --static --cflags --libs libpcre2-#{width}").split
        assert_includes flags, "-lpcre2-#{width}"
        system kandelo_cc, width_source, *flags, "-o", width_wasm
      end
      assert_equal "pcre2-#{width}-ok\n", kandelo_run_wasm(width_wasm, [])
    end
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 1
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "ed4db7b6a3ab57b837e545d910deeb841f908333b20f49cdafe30430cc9651de"
  end

end
