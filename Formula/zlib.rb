require_relative "../Kandelo/formula_support/kandelo_formula_support"

class Zlib < Formula
  include KandeloFormulaSupport

  desc "Compression library for Kandelo"
  homepage "https://zlib.net/"
  url "https://github.com/madler/zlib/releases/download/v1.3.1/zlib-1.3.1.tar.gz"
  sha256 "9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23"
  license "Zlib"
  revision 2

  # No bottle block yet: bottles are machine-generated on publish (Track C) via
  # brew bottle / pr-pull. Until then `brew install` builds from source. A
  # hand-written placeholder sha would make a default install try to pour a
  # nonexistent bottle and fail rather than build from source.
  skip_clean "lib/libz.a"

  def install
    kandelo_require_arch!("wasm32", "wasm64")

    kandelo_wasm_build do
      # zlib uses CHOST as both its target-system identity and cross-build
      # signal. Without it, configure sees the macOS build host and replaces
      # the SDK archiver with Apple's host-only libtool.
      ENV["CHOST"] = "#{kandelo_arch}-unknown-none"
      # Static archives linked into Wasm side modules must contain PIC objects.
      # Keep libz.a usable by both executables and dlopen-loaded extensions.
      ENV["CFLAGS"] = "-O2 -fPIC"

      system "./configure", "--static", *kandelo_std_configure_args
      system "make", "libz.a"
      system "make", "install"
    end
  end

  test do
    assert_path_exists lib/"libz.a"
    assert_path_exists include/"zlib.h"
    assert_path_exists include/"zconf.h"
    assert_path_exists lib/"pkgconfig/zlib.pc"

    kandelo_activate_sdk!
    kandelo_activate_sysroot!

    smoke_c = testpath/"zlib-smoke.c"
    smoke_wasm = testpath/"zlib-smoke.wasm"
    plugin_c = testpath/"zlib-plugin.c"
    plugin_so = testpath/"zlib-plugin.so"
    loader_c = testpath/"zlib-loader.c"
    loader_wasm = testpath/"zlib-loader.wasm"
    smoke_c.write <<~C
      #include <stdio.h>
      #include <string.h>
      #include <zlib.h>

      int main(void) {
        const unsigned char input[] = "kandelo zlib smoke";
        unsigned char compressed[128];
        unsigned char output[128];
        unsigned long compressed_len = sizeof(compressed);
        unsigned long output_len = sizeof(output);

        if (compress(compressed, &compressed_len, input, sizeof(input)) != Z_OK) {
          puts("compress failed");
          return 1;
        }
        if (uncompress(output, &output_len, compressed, compressed_len) != Z_OK) {
          puts("uncompress failed");
          return 1;
        }
        if (output_len != sizeof(input) || memcmp(input, output, sizeof(input)) != 0) {
          puts("roundtrip mismatch");
          return 1;
        }

        printf("zlib %s ok\\n", zlibVersion());
        return 0;
      }
    C

    system kandelo_cc, smoke_c, "-I#{include}", "-L#{lib}", "-lz", "-o", smoke_wasm
    output = kandelo_run_wasm(smoke_wasm, [])
    assert_match "zlib #{version} ok", output

    plugin_c.write <<~C
      #include <zlib.h>

      const char *kandelo_zlib_version(void) {
        return zlibVersion();
      }
    C
    loader_c.write <<~C
      #include <dlfcn.h>
      #include <stdio.h>
      #include <stdlib.h>

      typedef const char *(*version_fn)(void);

      int main(int argc, char **argv) {
        void *handle;
        void *allocation;
        version_fn version;

        if (argc != 2) return 2;
        allocation = calloc(1, 1);
        if (allocation == NULL) return 5;
        free(allocation);
        handle = dlopen(argv[1], RTLD_NOW);
        if (handle == NULL) {
          fprintf(stderr, "dlopen: %s\\n", dlerror());
          return 3;
        }
        version = (version_fn)dlsym(handle, "kandelo_zlib_version");
        if (version == NULL) {
          fprintf(stderr, "dlsym: %s\\n", dlerror());
          return 4;
        }
        printf("zlib-side-module %s ok\\n", version());
        return 0;
      }
    C

    system kandelo_cc, "-shared", "-fPIC", plugin_c,
      "-I#{include}", "-L#{lib}", "-lz", "-o", plugin_so
    system kandelo_cc, loader_c, "-ldl", "-Wl,--export-all", "-o", loader_wasm
    assert_equal "zlib-side-module #{version} ok\n",
      kandelo_run_wasm(loader_wasm, [plugin_so])
  end
end
