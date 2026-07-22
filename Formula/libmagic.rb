require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Libmagic < Formula
  include KandeloFormulaSupport

  desc "File type identification library for Kandelo"
  homepage "https://www.darwinsys.com/file/"
  url "https://astron.com/pub/file/file-5.45.tar.gz"
  sha256 "fc97f51029bb0e2c9f4e3bffefdaf678f0e039ee872b9de5c002a6d09c784d82"
  license all_of: ["BSD-2-Clause-Darwin", "BSD-2-Clause", :public_domain]

  depends_on KandeloFormulaSupport::PkgconfRequirement => [:build, :test]
  depends_on "kandelo-dev/tap-core/bzip2"
  depends_on "kandelo-dev/tap-core/xz"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "lib/libmagic.a"

  def install
    kandelo_require_arch!("wasm32")
    bzip2 = formula_opt_prefix("kandelo-dev/tap-core/bzip2")
    xz = formula_opt_prefix("kandelo-dev/tap-core/xz")
    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")
    jobs = "-j#{ENV.make_jobs}"

    # A version-matched native file(1) compiles architecture-independent
    # magic records for the cross build. The compiled format contains fixed
    # width records and an endian marker, not host pointers.
    host_build = buildpath/"host-build"
    host_build.mkpath
    cd host_build do
      system buildpath/"configure",
        "--disable-shared",
        "--enable-static",
        "--disable-zlib",
        "--disable-bzlib",
        "--disable-xzlib",
        "--disable-zstdlib",
        "--disable-lzlib",
        "--disable-libseccomp",
        "--disable-silent-rules"
      system "make", "-C", "src", "magic.h"
      system "make", jobs, "-C", "src", "file"
    end
    host_file = host_build/"src/file"
    odie "native FILE_COMPILE helper was not built" unless host_file.executable?

    kandelo_wasm_build do
      ENV["CPPFLAGS"] = "-I#{bzip2}/include -I#{xz}/include -I#{zlib}/include"
      ENV["LDFLAGS"] = "-L#{bzip2}/lib -L#{xz}/lib -L#{zlib}/lib"

      # AC_CHECK_LIB calls these fully prototyped functions with no arguments,
      # which is an invalid Wasm signature even though real consumers link.
      ENV["ac_cv_lib_bz2_BZ2_bzCompressInit"] = "yes"
      ENV["ac_cv_lib_lzma_lzma_stream_decoder"] = "yes"
      ENV["ac_cv_lib_z_gzopen"] = "yes"

      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-shared",
        "--enable-static",
        "--enable-fsect-man5",
        "--enable-zlib",
        "--enable-bzlib",
        "--enable-xzlib",
        "--disable-zstdlib",
        "--disable-lzlib",
        "--disable-libseccomp",
        "--disable-silent-rules"

      %w[ZLIBSUPPORT BZLIBSUPPORT XZLIBSUPPORT].each do |feature|
        odie "#{feature} was not enabled" unless (buildpath/"config.h").read.include?("#define #{feature} 1")
      end
      %w[ZSTDLIBSUPPORT LZLIBSUPPORT].each do |feature|
        odie "#{feature} was unexpectedly enabled" if (buildpath/"config.h").read.include?("#define #{feature} 1")
      end

      system "make", jobs, "FILE_COMPILE=#{host_file}"

      # Embed the stable opt path in libmagic while installing data into the
      # versioned keg. Make does not track command-line variable changes, so
      # rebuild src explicitly before the normal recursive install.
      system "make", "-C", "src", "clean"
      system "make", jobs, "-C", "src", "MAGIC=#{opt_prefix}/share/misc/magic"
      system "make", "install", "FILE_COMPILE=#{host_file}"
    end

    rm_r bin
    rm man1/"file.1"
    rm lib/"libmagic.la"

    # Upstream's raw private library names omit keg search paths, and its
    # src/Makefile links static consumers with libm without recording it.
    # liblzma and zlib publish pkg-config metadata; bzip2 1.0.8 does not.
    inreplace lib/"pkgconfig/libmagic.pc" do |s|
      s.sub!(/^Libs\.private:.*$/, "Libs.private: -L#{bzip2}/lib -lbz2 -lm")
      s.sub!(/^Cflags:/, "Requires.private: liblzma zlib\nCflags:")
    end

    magic_sources = share/"misc/magic"
    magic_sources.install buildpath/"magic/Header", buildpath/"magic/Localstuff"
    magic_sources.install buildpath.glob("magic/Magdir/*")

    [man3/"libmagic.3", man5/"magic.5"].each do |manual|
      inreplace manual, prefix.to_s, opt_prefix.to_s
    end

    source_files = magic_sources.glob("*")
    source_lines = source_files.sum { |source| source.each_line.count }
    odie "incomplete magic source provenance" if source_files.length != 342 || source_lines != 45_082

    database = share/"misc/magic.mgc"
    database_bytes = File.binread(database)
    header = database_bytes.unpack("L<2")
    records, remainder = database_bytes.length.divmod(376)
    odie "invalid magic database header" if header != [0xF11E041C, 18]
    odie "incomplete compiled magic database" if records != 22_642 || !remainder.zero?
  end

  test do
    assert_path_exists lib/"libmagic.a"
    assert_path_exists include/"magic.h"
    assert_path_exists lib/"pkgconfig/libmagic.pc"
    assert_path_exists share/"misc/magic.mgc"
    assert_path_exists man3/"libmagic.3"
    assert_path_exists man5/"magic.5"
    assert_equal 342, (share/"misc/magic").glob("*").length
    assert_equal 45_082, (share/"misc/magic").glob("*").sum { |source| source.each_line.count }
    assert_equal 22_642, (share/"misc/magic.mgc").size/376
    archive = File.binread(lib/"libmagic.a")
    assert_includes archive, "#{opt_prefix}/share/misc/magic"
    refute_includes archive, (prefix/"share/misc/magic").to_s

    source = testpath/"libmagic-smoke.c"
    wasm = testpath/"libmagic-smoke.wasm"
    instrumented = testpath/"libmagic-smoke.instrumented.wasm"
    source.write <<~C
      #include <bzlib.h>
      #include <lzma.h>
      #include <magic.h>
      #include <stdint.h>
      #include <stdio.h>
      #include <string.h>
      #include <zlib.h>

      static int classify(magic_t cookie, const char *label,
          const void *data, size_t length, const char *expected) {
        const char *result = magic_buffer(cookie, data, length);
        if (result == NULL || strstr(result, expected) == NULL) {
          fprintf(stderr, "%s: %s\\n", label, result == NULL ? magic_error(cookie) : result);
          return -1;
        }
        printf("%s=%s\\n", label, result);
        return 0;
      }

      int main(void) {
        static const uint8_t wasm_module[] = { 0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00 };
        static const uint8_t pdf[] = "%PDF-1.7\\n";
        static const uint8_t text[] =
          "Kandelo built-in compression support identifies this ASCII payload.\\n";
        uint8_t compressed[512];
        unsigned long zlib_length = sizeof(compressed);
        unsigned int bzip2_length = sizeof(compressed);
        size_t xz_length = 0;
        magic_t cookie = magic_open(MAGIC_COMPRESS);

        if (cookie == NULL || magic_load(cookie, NULL) != 0) return 1;
        if (classify(cookie, "wasm", wasm_module, sizeof(wasm_module), "WebAssembly") != 0) return 2;
        if (classify(cookie, "pdf", pdf, sizeof(pdf) - 1, "PDF document") != 0) return 3;

        if (compress2(compressed, &zlib_length, text, sizeof(text) - 1, 9) != Z_OK) return 4;
        if (classify(cookie, "zlib", compressed, zlib_length, "ASCII text") != 0) return 5;

        if (BZ2_bzBuffToBuffCompress((char *)compressed, &bzip2_length,
              (char *)text, sizeof(text) - 1, 9, 0, 30) != BZ_OK) return 6;
        if (classify(cookie, "bzip2", compressed, bzip2_length, "ASCII text") != 0) return 7;

        if (lzma_easy_buffer_encode(6, LZMA_CHECK_CRC64, NULL,
              text, sizeof(text) - 1, compressed, &xz_length, sizeof(compressed)) != LZMA_OK) return 8;
        if (classify(cookie, "xz", compressed, xz_length, "ASCII text") != 0) return 9;

        magic_close(cookie);
        return 0;
      }
    C

    kandelo_wasm_build do |root|
      bzip2 = formula_opt_prefix("kandelo-dev/tap-core/bzip2")
      xz = formula_opt_prefix("kandelo-dev/tap-core/xz")
      zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")
      ENV["PKG_CONFIG_LIBDIR"] = [lib/"pkgconfig", xz/"lib/pkgconfig", zlib/"lib/pkgconfig"].join(":")
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")
      pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
      flags = shell_output("#{pkgconf} --static --cflags --libs libmagic").split
      %w[-lmagic -llzma -lbz2 -lz -lm].each { |flag| assert_includes flags, flag }
      system kandelo_cc, source, *flags, "-I#{bzip2}/include", "-o", wasm
      system "#{root}/scripts/run-wasm-fork-instrument.sh", wasm, "-o", instrumented
    end

    output = kandelo_run_wasm(instrumented, [])
    assert_match(/^wasm=WebAssembly \(wasm\) binary module/, output)
    assert_match(/^pdf=PDF document/, output)
    %w[zlib bzip2 xz].each { |format| assert_match(/^#{format}=ASCII text/, output) }
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "6ddb4a4bba8fbe68e457d1abc8baa7b0567e2d6c583ee7578693426fd017018e"
  end

end
