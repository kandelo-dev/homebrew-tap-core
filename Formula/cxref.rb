require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Cxref < Formula
  include KandeloFormulaSupport

  desc "Generate POSIX C-language cross-reference tables for Kandelo"
  homepage "https://pubs.opengroup.org/onlinepubs/9799919799/utilities/cxref.html"
  url "https://downloads.sourceforge.net/project/mcpp/mcpp/V.2.7.2/mcpp-2.7.2.tar.gz"
  version "0.1.0"
  sha256 "3b9b4421888519876c4fc68ade324a3bbd81ceeb7092ecdbbc2055099fcb8864"
  license all_of: ["GPL-2.0-or-later", "BSD-2-Clause", "MIT"]

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/cxref"

  resource "tree-sitter" do
    url "https://github.com/tree-sitter/tree-sitter/archive/refs/tags/v0.26.11.tar.gz"
    sha256 "1bab01ed21464f3272665b9c60e39ee79f68da1333e80b23f2c9356569d06971"
  end

  resource "tree-sitter-c" do
    url "https://github.com/tree-sitter/tree-sitter-c/archive/refs/tags/v0.24.2.tar.gz"
    sha256 "2eeb4db31f8fa0865e45488503d13403923bcb485a1bdb637abff8c42dd97364"
  end

  def install
    kandelo_require_arch!("wasm32")

    tap_sources = Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_sources/cxref"
    frontend = tap_sources/"cxref.c"
    odie "tap-owned cxref frontend is missing: #{frontend}" unless frontend.file?

    tree_sitter = buildpath/"vendor/tree-sitter"
    tree_sitter_c = buildpath/"vendor/tree-sitter-c"
    resource("tree-sitter").stage { tree_sitter.install Dir["*"] }
    resource("tree-sitter-c").stage { tree_sitter_c.install Dir["*"] }
    artifact = buildpath/"cxref.wasm"

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/kandelo-cxref-#{version}"
      prefix_maps = {
        buildpath.to_s               => "#{stable_source}/vendor/mcpp",
        tap_sources.to_s             => stable_source,
        root.to_s                    => "/usr/src/kandelo",
        Pathname(root).realpath.to_s => "/usr/src/kandelo",
        "/nix/store"                 => "/usr/src/toolchain",
      }.uniq.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end

      # MCPP's maintained library interface provides in-memory preprocessing.
      # Its old source predates modern Clang's strict prototype diagnostics.
      ENV["CFLAGS"] = [
        "-O2", "-Wno-implicit-function-declaration", "-Wno-incompatible-pointer-types",
        *prefix_maps
      ].join(" ")

      # MCPP's library cleanup predates fclose(NULL) being a hard failure in
      # common libcs. Failed input/output opens leave the corresponding stream
      # null; guard all three optional streams before closing them.
      inreplace "src/main.c", "if (fp_in != stdin)", "if (fp_in != NULL && fp_in != stdin)"
      inreplace "src/main.c", "if (fp_out != stdout)", "if (fp_out != NULL && fp_out != stdout)"
      inreplace "src/main.c", "if (fp_err != stderr)", "if (fp_err != NULL && fp_err != stderr)"
      inreplace "src/main.c", "    errors = src_col = 0;",
        "    errors = src_col = 0;\n    wrong_line = FALSE;"
      # sharp() keeps its duplicate-line state in function-local statics, so
      # an allocator-reused FILEINFO can suppress the next library call's
      # initial #line directive. Make that state resettable with the rest of
      # MCPP's library globals.
      inreplace "src/system.c", "static char *   sharp_filename = NULL;",
        ["static char *   sharp_filename = NULL;", "static FILEINFO *   sh_file = NULL;",
         "static int  sh_line = 0;"].join("\n")
      inreplace "src/system.c", "    sharp_filename = NULL;",
        ["    sharp_filename = NULL;", "    sh_file = NULL;", "    sh_line = 0;"].join("\n")
      inreplace "src/system.c",
        ["    static FILEINFO *   sh_file;", "    static int  sh_line;", ""].join("\n"), ""
      inreplace "src/system.c",
        ["        if (tmp)                        /* The file exists          */",
         "            *in_pp = tmp;",
         "            /* Else mcpp_main() will diagnose *in_pp and exit   */"].join("\n"),
        ["        if (tmp)                        /* The file exists          */",
         "            *in_pp = tmp;", "        else if (mcpp_debug & MACRO_CALL)",
         "            *in_pp = save_string( *in_pp);",
         "            /* Else mcpp_main() will diagnose *in_pp and exit   */"].join("\n")
      inreplace "src/mbchar.c",
        ["    }", "    strcpy( norm, name);", "    if (norm[ 5] == '.')"].join("\n"),
        ["        return  NULL;", "    }", "    strcpy( norm, name);",
         "    if (strlen( norm) > 5 && norm[ 5] == '.')"].join("\n")
      system kandelo_configure(root), *kandelo_std_configure_args,
        "--disable-dependency-tracking",
        "--disable-shared",
        "--enable-mcpplib"
      system "make", "-j#{ENV.make_jobs}"

      mcpp = buildpath/"src/.libs/libmcpp.a"
      odie "MCPP did not produce its static library" unless mcpp.file?
      system kandelo_cc(root),
        "-std=c17", "-O2", "-gline-tables-only", "-D_POSIX_C_SOURCE=200809L",
        "-D_XOPEN_SOURCE=700", "-Wall", "-Wextra", "-Werror",
        "-fdebug-compilation-dir=#{stable_source}", *prefix_maps,
        "-I#{buildpath}/src",
        "-I#{tree_sitter}/lib/include",
        "-I#{tree_sitter}/lib/src",
        "-I#{tree_sitter_c}/src",
        tree_sitter/"lib/src/lib.c",
        tree_sitter_c/"src/parser.c",
        frontend,
        mcpp,
        "-o", artifact
      kandelo_validate_wasm_artifact(
        artifact,
        fork:            :forbidden,
        forbidden_paths: [buildpath.to_s, tap_sources.to_s, prefix.to_s],
      )
    end

    kandelo_install_bin(buildpath, artifact.basename, "cxref")
    man1.install tap_sources/"cxref.1"
  end

  test do
    assert_path_exists man1/"cxref.1"

    workspace = testpath/"workspace"
    include_dir = workspace/"include"
    include_dir.mkpath
    (include_dir/"fixture.h").write <<~HEADER
      #define HEADER_SCALE 2
      extern int shared_value;
    HEADER
    (workspace/"one.c").write <<~C
      #include "fixture.h"
      #define LOCAL_SCALE 3
      static int file_static;
      int shared_value;

      static int helper(int parameter) {
        int local_value = parameter + shared_value;
        return local_value;
      }

      int main(void) {
        return helper(file_static) + LOCAL_SCALE + HEADER_SCALE;
      }
    C
    (workspace/"conditional.c").write <<~C
      #ifdef SELECT_PATH
      int enabled_symbol;
      #else
      int fallback_symbol;
      #endif

      int choose(void) {
      #ifdef SELECT_PATH
        return enabled_symbol;
      #else
        return fallback_symbol;
      #endif
      }
    C
    (workspace/"review.c").write <<~C
      #define ID(value) value
      #define FEATURE 1
      int source_value;
      int macro_global;
      #define READ_GLOBAL() macro_global

      int target(void) {
        return 1;
      }

      int invoke(int (*target)(void)) {
        return target();
      }

      int macro_read(void) {
        return READ_GLOBAL();
      }

      int multiline(void) {
        return ID(
          source_value
        );
      }

      #if FEATURE
      int active_conditional;
      #else
      int inactive_conditional;
      #endif

      int after_shadow(void) {
        {
          int target = 0;
          target += 1;
        }
        return target();
      }
    C
    first_include = workspace/"first"
    second_include = workspace/"second"
    first_include.mkpath
    second_include.mkpath
    (first_include/"choice.h").write "int first_choice;\n"
    (second_include/"choice.h").write "int second_choice;\n"
    (workspace/"option_order.c").write <<~C
      #include "choice.h"
      #if ORDERED == 2
      int final_define;
      #else
      int wrong_define;
      #endif
    C
    (workspace/"-input.c").write "int leading_operand;\n"
    (workspace/"-").write "int dash_operand;\n"
    (workspace/"linkage_a.c").write <<~C
      static int helper(void) {
        return 1;
      }

      int from_a(void) {
        return helper();
      }
    C
    (workspace/"linkage_b.c").write <<~C
      int helper;

      int from_b(void) {
        return helper;
      }
    C
    (workspace/"macro_generated.c").write <<~C
      #define ID(value) value
      #define DECLARE(name) int name(void)
      #define LOCAL(name) int name

      int target(void) {
        return 1;
      }

      DECLARE(generated);

      int f(void) {
        return ID(target)();
      }

      int g(void) {
        return generated();
      }

      int local_user(void) {
        LOCAL(local_value) = 1;
        return local_value;
      }
      #define DECLARE_LITERAL int literal_generated(void)
      #define LOCAL_LITERAL int literal_local

      DECLARE_LITERAL;

      int literal_call(void) {
        return literal_generated();
      }

      int literal_local_user(void) {
        LOCAL_LITERAL = 1;
        return literal_local;
      }
    C
    (workspace/"old_style.c").write <<~C
      int target(void) {
        return 1;
      }

      int old_style(target)
      int target;
      {
        return target;
      }

      int after_old_style(void) {
        return target();
      }
    C

    env = { "KERNEL_CWD" => "/work" }
    mount = { "/work" => workspace }
    separate = kandelo_run_wasm(
      bin/"cxref", ["-Iinclude", "one.c", "conditional.c"],
      env: env, writable_host_directories: mount
    )
    assert_includes separate.lines.map(&:chomp), "one.c"
    assert_includes separate.lines.map(&:chomp), "conditional.c"
    assert_match(%r{^HEADER_SCALE \| /work/include/fixture\.h \| - \| \*1$}, separate)
    assert_match(%r{^LOCAL_SCALE \| /work/one\.c \| - \| \*2$}, separate)
    assert_match(%r{^file_static \| /work/one\.c \| - \| \*3$}, separate)
    assert_match(%r{^helper \| /work/one\.c \| - \| \*6$}, separate)
    assert_match(%r{^local_value \| /work/one\.c \| helper \| \*7$}, separate)
    assert_match(%r{^parameter \| /work/one\.c \| helper \| 7$}, separate)
    assert_match(%r{^fallback_symbol \| /work/conditional\.c \| - \| \*4$}, separate)

    reviewed = kandelo_run_wasm(
      bin/"cxref", ["-cs", "review.c"], env: env, writable_host_directories: mount
    )
    assert_match(%r{^ID \| /work/review\.c \| multiline \| 20$}, reviewed)
    assert_match(%r{^source_value \| /work/review\.c \| multiline \| 21$}, reviewed)
    assert_match(%r{^target \| /work/review\.c \| invoke \| \*11$}, reviewed)
    assert_match(%r{^target \| /work/review\.c \| invoke \| 12$}, reviewed)
    assert_match(%r{^FEATURE \| /work/review\.c \| - \| 25$}, reviewed)
    assert_match(%r{^macro_global \| /work/review\.c \| - \| 5$}, reviewed)
    refute_match(%r{^macro_global \| /work/review\.c \| macro_read \| 16$}, reviewed)
    assert_match(%r{^target \| /work/review\.c \| after_shadow \| \*33$}, reviewed)
    assert_match(%r{^target \| /work/review\.c \| - \| 36$}, reviewed)
    refute_match(%r{^target \| /work/review\.c \| after_shadow \| 36$}, reviewed)

    linked = kandelo_run_wasm(
      bin/"cxref", ["-cs", "linkage_a.c", "linkage_b.c"],
      env: env, writable_host_directories: mount
    )
    assert_match(%r{^helper \| /work/linkage_a\.c \| - \| 6$}, linked)
    assert_match(%r{^helper \| /work/linkage_b\.c \| from_b \| 4$}, linked)
    refute_match(%r{^helper \| /work/linkage_b\.c \| - \| 4$}, linked)

    generated = kandelo_run_wasm(
      bin/"cxref", ["-cs", "macro_generated.c"], env: env, writable_host_directories: mount
    )
    assert_match(%r{^generated \| /work/macro_generated\.c \| - \| \*9$}, generated)
    assert_match(%r{^generated \| /work/macro_generated\.c \| - \| 16$}, generated)
    assert_match(%r{^target \| /work/macro_generated\.c \| - \| 12$}, generated)
    assert_match(%r{^local_value \| /work/macro_generated\.c \| local_user \| \*20$}, generated)
    assert_match(%r{^local_value \| /work/macro_generated\.c \| local_user \| 21$}, generated)
    refute_match(%r{^generated \| /work/macro_generated\.c \| g \| 16$}, generated)
    refute_match(%r{^target \| /work/macro_generated\.c \| f \| 12$}, generated)
    assert_match(%r{^literal_generated \| /work/macro_generated\.c \| - \| \*23$}, generated)
    assert_match(%r{^literal_generated \| /work/macro_generated\.c \| - \| 29$}, generated)
    assert_match(%r{^literal_local \| /work/macro_generated\.c \| - \| \*24$}, generated)
    assert_match(%r{^literal_local \| /work/macro_generated\.c \| literal_local_user \| 34$}, generated)

    old_style = kandelo_run_wasm(
      bin/"cxref", ["-cs", "old_style.c"], env: env, writable_host_directories: mount
    )
    assert_match(%r{^target \| /work/old_style\.c \| old_style \| \*5$}, old_style)
    assert_match(%r{^target \| /work/old_style\.c \| old_style \| \*6$}, old_style)
    assert_match(%r{^target \| /work/old_style\.c \| old_style \| 8$}, old_style)
    assert_match(%r{^target \| /work/old_style\.c \| - \| 12$}, old_style)
    refute_match(%r{^target \| /work/old_style\.c \| after_old_style \| 12$}, old_style)

    ordered = kandelo_run_wasm(
      bin/"cxref",
      ["-cs", "-Ifirst", "-DORDERED=1", "-Isecond", "-UORDERED", "-DORDERED=2", "option_order.c"],
      env: env, writable_host_directories: mount,
    )
    assert_match(%r{^first_choice \| /work/first/choice\.h \| - \| \*1$}, ordered)
    assert_match(%r{^final_define \| /work/option_order\.c \| - \| \*3$}, ordered)
    refute_match(/^second_choice /, ordered)
    refute_match(/^wrong_define /, ordered)

    literal_operands = kandelo_run_wasm(
      bin/"cxref", ["-cs", "--", "-input.c", "-"],
      env: env, writable_host_directories: mount
    )
    assert_match(%r{^leading_operand \| /work/-input\.c \| - \| \*1$}, literal_operands)
    assert_match(%r{^dash_operand \| /work/- \| - \| \*1$}, literal_operands)

    # MCPP groups -D and -U internally, so the frontend must preserve the
    # POSIX command order before invoking it.
    undefined_last = kandelo_run_wasm(
      bin/"cxref", ["-cs", "-DSELECT_PATH", "-USELECT_PATH", "conditional.c"],
      env: env, writable_host_directories: mount
    )
    assert_match(/^fallback_symbol /, undefined_last)
    refute_match(/^enabled_symbol /, undefined_last)

    defined_last = kandelo_run_wasm(
      bin/"cxref", ["-cs", "-USELECT_PATH", "-DSELECT_PATH", "conditional.c"],
      env: env, writable_host_directories: mount
    )
    assert_match(/^enabled_symbol /, defined_last)
    refute_match(/^fallback_symbol /, defined_last)
    assert_equal defined_last.lines, defined_last.lines.sort

    assert_empty kandelo_run_wasm(
      bin/"cxref",
      ["-cs", "-w", "51", "-Iinclude", "-o", "report.txt", "one.c", "conditional.c"],
      env: env, writable_host_directories: mount,
    )
    report = (workspace/"report.txt").read
    assert report.lines.all? { |line| line.chomp.length <= 51 }

    missing = kandelo_run_wasm(
      bin/"cxref", ["missing.c"], env: env, merge_stderr: true,
      writable_host_directories: mount, expected_status: 1
    )
    assert_match(/missing\.c/, missing)

    browser_output = kandelo_run_browser_wasm(
      bin/"cxref",
      [
        "-cs", "-I/work/include", "-USELECT_PATH", "-DSELECT_PATH",
        "-I/work/first", "-DORDERED=1", "-I/work/second", "-UORDERED", "-DORDERED=2",
        "/work/one.c", "/work/conditional.c", "/work/review.c", "/work/option_order.c",
        "/work/linkage_a.c", "/work/linkage_b.c", "/work/macro_generated.c", "/work/old_style.c"
      ],
      argv0:       "cxref",
      guest_files: {
        "/work/include/fixture.h" => include_dir/"fixture.h",
        "/work/one.c"             => workspace/"one.c",
        "/work/conditional.c"     => workspace/"conditional.c",
        "/work/review.c"          => workspace/"review.c",
        "/work/option_order.c"    => workspace/"option_order.c",
        "/work/first/choice.h"    => first_include/"choice.h",
        "/work/second/choice.h"   => second_include/"choice.h",
        "/work/linkage_a.c"       => workspace/"linkage_a.c",
        "/work/linkage_b.c"       => workspace/"linkage_b.c",
        "/work/macro_generated.c" => workspace/"macro_generated.c",
        "/work/old_style.c"       => workspace/"old_style.c",
      },
      timeout_ms:  120_000,
    )
    assert_match(%r{^HEADER_SCALE \| /work/include/fixture\.h \| - \| \*1$}, browser_output)
    assert_match(%r{^enabled_symbol \| /work/conditional\.c \| - \| \*2$}, browser_output)
    refute_match(/^fallback_symbol /, browser_output)
    assert_match(%r{^source_value \| /work/review\.c \| multiline \| 21$}, browser_output)
    assert_match(%r{^target \| /work/review\.c \| invoke \| \*11$}, browser_output)
    assert_match(%r{^FEATURE \| /work/review\.c \| - \| 25$}, browser_output)
    assert_match(%r{^macro_global \| /work/review\.c \| - \| 5$}, browser_output)
    refute_match(%r{^macro_global \| /work/review\.c \| macro_read \| 16$}, browser_output)
    assert_match(%r{^target \| /work/review\.c \| - \| 36$}, browser_output)
    refute_match(%r{^target \| /work/review\.c \| after_shadow \| 36$}, browser_output)
    assert_match(%r{^helper \| /work/linkage_a\.c \| - \| 6$}, browser_output)
    assert_match(%r{^helper \| /work/linkage_b\.c \| from_b \| 4$}, browser_output)
    refute_match(%r{^helper \| /work/linkage_b\.c \| - \| 4$}, browser_output)
    assert_match(%r{^generated \| /work/macro_generated\.c \| - \| \*9$}, browser_output)
    assert_match(%r{^generated \| /work/macro_generated\.c \| - \| 16$}, browser_output)
    assert_match(%r{^target \| /work/macro_generated\.c \| - \| 12$}, browser_output)
    assert_match(%r{^local_value \| /work/macro_generated\.c \| local_user \| \*20$}, browser_output)
    assert_match(%r{^local_value \| /work/macro_generated\.c \| local_user \| 21$}, browser_output)
    refute_match(%r{^generated \| /work/macro_generated\.c \| g \| 16$}, browser_output)
    refute_match(%r{^target \| /work/macro_generated\.c \| f \| 12$}, browser_output)
    assert_match(%r{^literal_generated \| /work/macro_generated\.c \| - \| \*23$}, browser_output)
    assert_match(%r{^literal_generated \| /work/macro_generated\.c \| - \| 29$}, browser_output)
    assert_match(%r{^literal_local \| /work/macro_generated\.c \| - \| \*24$}, browser_output)
    assert_match(%r{^literal_local \| /work/macro_generated\.c \| literal_local_user \| 34$}, browser_output)
    assert_match(%r{^target \| /work/old_style\.c \| old_style \| \*5$}, browser_output)
    assert_match(%r{^target \| /work/old_style\.c \| old_style \| \*6$}, browser_output)
    assert_match(%r{^target \| /work/old_style\.c \| old_style \| 8$}, browser_output)
    assert_match(%r{^target \| /work/old_style\.c \| - \| 12$}, browser_output)
    refute_match(%r{^target \| /work/old_style\.c \| after_old_style \| 12$}, browser_output)
    assert_match(%r{^first_choice \| /work/first/choice\.h \| - \| \*1$}, browser_output)
    assert_match(%r{^final_define \| /work/option_order\.c \| - \| \*3$}, browser_output)
  end
end
