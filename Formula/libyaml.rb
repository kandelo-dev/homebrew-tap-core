require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Libyaml < Formula
  include KandeloFormulaSupport

  desc "YAML parser and emitter library for Kandelo"
  homepage "https://github.com/yaml/libyaml"
  url "https://pyyaml.org/download/libyaml/yaml-0.2.5.tar.gz"
  sha256 "c642ae9b75fee120b2d96c712538bd2cf283228d2337df2cf2988e3c02678ef4"
  license "MIT"

  # WHY: C2 published revision-zero OCI bytes before its final handoff failed.
  # Reserve a new Homebrew identity so recovery never overwrites or relabels
  # those public bytes.
  revision 1

  skip_clean "lib/libyaml.a"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      # Static archives can also feed Kandelo side modules, whose objects must
      # be position-independent even though the primary Ruby consumer is an
      # executable.
      ENV["CFLAGS"] = "-O2 -fPIC"
      system kandelo_configure(root),
        "--prefix=#{prefix}",
        "--disable-shared",
        "--enable-static",
        "--disable-dependency-tracking"
      system "make", "-j#{ENV.make_jobs}"
      system "make", "install"
    end
  end

  test do
    assert_path_exists include/"yaml.h"
    assert_path_exists lib/"libyaml.a"
    assert_path_exists lib/"pkgconfig/yaml-0.1.pc"

    kandelo_activate_sdk!
    kandelo_activate_sysroot!
    smoke_c = testpath/"libyaml-smoke.c"
    smoke_wasm = testpath/"libyaml-smoke.wasm"
    smoke_c.write <<~C
      #include <stdio.h>
      #include <string.h>
      #include <yaml.h>

      int main(void) {
        static const unsigned char document[] = "project: kandelo\\nready: true\\n";
        yaml_parser_t parser;
        yaml_event_t event;
        int found_project = 0;
        int done = 0;

        if (!yaml_parser_initialize(&parser)) return 2;
        yaml_parser_set_input_string(&parser, document, sizeof(document) - 1);
        while (!done) {
          if (!yaml_parser_parse(&parser, &event)) {
            yaml_parser_delete(&parser);
            return 3;
          }
          if (event.type == YAML_SCALAR_EVENT &&
              event.data.scalar.length == 7 &&
              memcmp(event.data.scalar.value, "kandelo", 7) == 0) {
            found_project = 1;
          }
          done = event.type == YAML_STREAM_END_EVENT;
          yaml_event_delete(&event);
        }
        yaml_parser_delete(&parser);
        if (!found_project) return 4;
        printf("libyaml %s ok\\n", yaml_get_version_string());
        return 0;
      }
    C

    system kandelo_cc, smoke_c, "-I#{include}", "-L#{lib}", "-lyaml", "-o", smoke_wasm
    expected = "libyaml #{version} ok\n"
    assert_equal expected, kandelo_run_wasm(smoke_wasm, [])
    assert_equal expected, kandelo_run_browser_wasm(smoke_wasm, [], allow_stderr: false)
  end


  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core-abi-43/libyaml"
    sha256 cellar: "/opt/kandelo/homebrew/Cellar", wasm32_kandelo: "03b03a8dad7cdb6e94955e7f1dedd197341078a05f9314cef1057b5afbaeee28"
  end
end
