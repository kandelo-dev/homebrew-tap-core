require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Libpng < Formula
  include KandeloFormulaSupport

  desc "PNG reference library for Kandelo"
  homepage "https://www.libpng.org/pub/png/libpng.html"
  url "https://download.sourceforge.net/libpng/libpng-1.6.43.tar.xz"
  sha256 "6a5ca0652392a2d7c9db2ae5b40210843c0bbc081cbd410825ab00cc59f14a6c"
  license "libpng-2.0"

  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "lib/libpng16.a"

  def install
    kandelo_require_arch!("wasm32")
    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")

    kandelo_wasm_build do
      ENV["CPPFLAGS"] = "-I#{zlib}/include"
      ENV["LDFLAGS"] = "-L#{zlib}/lib"

      system kandelo_configure, *kandelo_std_configure_args,
        "--enable-static",
        "--disable-shared",
        "--with-zlib-prefix=#{zlib}"
      system "make"
      system "make", "install"
    end

    rm_r bin if bin.exist?
  end

  test do
    assert_path_exists lib/"libpng16.a"
    assert_path_exists include/"png.h"
    assert_path_exists lib/"pkgconfig/libpng16.pc"

    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")
    source = testpath/"libpng-smoke.c"
    wasm = testpath/"libpng-smoke.wasm"
    source.write <<~C
      #include <png.h>
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>

      int main(void) {
        static const unsigned char pixel[] = { 0x12, 0x34, 0x56, 0xff };
        unsigned char decoded[sizeof(pixel)] = { 0 };
        png_alloc_size_t encoded_size = 0;
        png_image write_image;
        png_image read_image;
        unsigned char *encoded;

        memset(&write_image, 0, sizeof(write_image));
        write_image.version = PNG_IMAGE_VERSION;
        write_image.width = 1;
        write_image.height = 1;
        write_image.format = PNG_FORMAT_RGBA;
        if (!png_image_write_to_memory(&write_image, NULL, &encoded_size, 0, pixel, 0, NULL)) return 1;

        encoded = malloc(encoded_size);
        if (encoded == NULL) return 2;
        if (!png_image_write_to_memory(&write_image, encoded, &encoded_size, 0, pixel, 0, NULL)) return 3;
        png_image_free(&write_image);

        memset(&read_image, 0, sizeof(read_image));
        read_image.version = PNG_IMAGE_VERSION;
        if (!png_image_begin_read_from_memory(&read_image, encoded, encoded_size)) return 4;
        read_image.format = PNG_FORMAT_RGBA;
        if (!png_image_finish_read(&read_image, NULL, decoded, 0, NULL)) return 5;
        if (memcmp(pixel, decoded, sizeof(pixel)) != 0) return 6;

        png_image_free(&read_image);
        free(encoded);
        puts("libpng-ok");
        return 0;
      }
    C

    kandelo_wasm_build do
      system kandelo_cc, source,
        "-I#{include}", "-I#{zlib}/include",
        "-L#{lib}", "-L#{zlib}/lib",
        "-lpng16", "-lz", "-lm", "-o", wasm
    end
    assert_equal "libpng-ok\n", kandelo_run_wasm(wasm, [])
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 2
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "19b59336d04d75bc2fd69df42c71da9c3029b2caf3cb6fceb9e7b56ed81f05db"
  end

end
