require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Ctags < Formula
  include KandeloFormulaSupport

  desc "Maintained source-code tag generator for Kandelo"
  homepage "https://ctags.io/"
  url "https://github.com/universal-ctags/ctags/releases/download/v6.2.1/universal-ctags-6.2.1.tar.gz"
  sha256 "2c63efe9e0e083dc50e6fdd8c5414781cc8873d8c8940cf553c01870ed962f8c"
  license "GPL-2.0-or-later"

  depends_on "binaryen" => :build
  depends_on "docutils" => :build
  depends_on "pkgconf" => :build
  depends_on "wabt" => :build

  skip_clean "bin/ctags", "bin/optscript", "bin/readtags"

  def install
    kandelo_require_arch!("wasm32")

    # The bundled Acutest header reads errno without including its declaring
    # header. Glibc exposes it transitively, while conforming libc headers do not.
    inreplace "extra-cmds/acutest.h", "#include <ctype.h>\n", "#include <ctype.h>\n#include <errno.h>\n"

    # Upstream probes the build filesystem even while cross-compiling. Kandelo's
    # VFS is case-sensitive, regardless of the macOS build volume.
    inreplace "configure", "if test -f CONFTEST.CIF; then",
              'if test "$cross_compiling" != yes && test -f CONFTEST.CIF; then'

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/universal-ctags-#{version}"
      mapped_roots = {
        buildpath.to_s               => stable_source,
        root.to_s                    => "/usr/src/kandelo",
        Pathname(root).realpath.to_s => "/usr/src/kandelo",
        "/nix/store"                 => "/usr/src/toolchain",
      }
      prefix_maps = mapped_roots.uniq.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end
      ENV["CFLAGS"] = [
        "-O2", "-gline-tables-only", "-fdebug-compilation-dir=#{stable_source}", *prefix_maps
      ].join(" ")
      ENV["CC_FOR_BUILD"] = kandelo_host_cc

      # These are upstream's explicit optional integration boundaries. The core
      # commands and bundled parsers do not require their external libraries.
      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-dependency-tracking",
        "--disable-external-sort",
        "--disable-json",
        "--disable-pcre2",
        "--disable-seccomp",
        "--disable-xml",
        "--disable-yaml",
        "--enable-tmpdir=/tmp"
      system "make", "-j#{ENV.make_jobs}"

      stage = buildpath/"kandelo-stage"
      system "make", "install", "DESTDIR=#{stage}"
      staged_prefix = stage/prefix.to_s.delete_prefix("/")
      odie "Universal Ctags did not install into its staged prefix" unless staged_prefix.directory?
      %w[ctags readtags optscript].each do |name|
        kandelo_validate_wasm_artifact(staged_prefix/"bin"/name, fork: :forbidden)
      end
      prefix.install staged_prefix.children
    end
  end

  test do
    assert_path_exists bin/"ctags"
    assert_path_exists bin/"readtags"
    assert_path_exists bin/"optscript"
    assert_path_exists man1/"ctags.1"
    assert_path_exists man1/"readtags.1"
    assert_path_exists man5/"tags.5"
    assert_path_exists man7/"ctags-lang-c.7"
    assert_path_exists man7/"ctags-lang-c++.7"

    version_output = kandelo_run_wasm(bin/"ctags", ["--options=NONE", "--version"])
    assert_match(/^Universal Ctags 6\.2\.1,/, version_output)
    assert_match(/\+internal-sort/, version_output)
    assert_match(/\+iconv/, version_output)
    assert_match(/\+optscript/, version_output)
    refute_match(/case-insensitive-filenames/, version_output)

    languages = kandelo_run_wasm(bin/"ctags", ["--options=NONE", "--list-languages"])
    assert_match(/^C$/i, languages)
    assert_match(/^C\+\+$/, languages)
    assert_match(/Usage: .*optscript/, kandelo_run_wasm(bin/"optscript", ["--help"]))

    workspace = testpath/"workspace"
    workspace.mkpath
    (workspace/"api.c").write <<~C
      int c_helper(int value) {
        return value * 2;
      }
    C
    (workspace/"widget.hpp").write <<~CPP
      namespace demo {
      class Widget {
       public:
        int compute(int value) const;
      };
      }
    CPP
    (workspace/"widget.cpp").write <<~CPP
      #include "widget.hpp"
      int demo::Widget::compute(int value) const {
        return value + 1;
      }
    CPP
    (workspace/"added.cpp").write <<~CPP
      struct Added {
        void ping();
      };
      void Added::ping() {}
    CPP

    env = { "KERNEL_CWD" => "/work" }
    mount = { "/work" => workspace }
    assert_empty kandelo_run_wasm(
      bin/"ctags",
      ["--options=NONE", "--fields=+nKl", "--extras=+q", "-f", "tags", "api.c", "widget.hpp", "widget.cpp"],
      env: env, writable_host_directories: mount,
    )

    tags = (workspace/"tags").read
    assert_match(/^!_TAG_FILE_SORTED\t1\t/, tags)
    assert_match(/^c_helper\tapi\.c\t.*\tlanguage:C(?:\t|$)/i, tags)
    assert_match(/^demo::Widget::compute\twidget\.cpp\t.*\tlanguage:C\+\+(?:\t|$)/, tags)

    c_query = kandelo_run_wasm(
      bin/"readtags", ["-t", "tags", "-ne", "c_helper"],
      env: env, writable_host_directories: mount
    )
    assert_match(/^c_helper\tapi\.c\t/, c_query)
    assert_match(/kind:function/, c_query)
    assert_match(/\tlanguage:C(?:\t|\n|\z)/i, c_query)

    cpp_query = kandelo_run_wasm(
      bin/"readtags", ["-t", "tags", "-ne", "compute"],
      env: env, writable_host_directories: mount
    )
    assert_match(/^compute\twidget\.cpp\t/, cpp_query)
    assert_match(/class:demo::Widget/, cpp_query)
    assert_match(/language:C\+\+/, cpp_query)

    assert_empty kandelo_run_wasm(
      bin/"ctags",
      ["--options=NONE", "--fields=+nKl", "--extras=+q", "--append=yes", "-f", "tags", "added.cpp"],
      env: env, writable_host_directories: mount,
    )
    added_query = kandelo_run_wasm(
      bin/"readtags", ["-t", "tags", "-ne", "Added"],
      env: env, writable_host_directories: mount
    )
    assert_match(/^Added\tadded\.cpp\t/, added_query)
    assert_match(/kind:struct/, added_query)
    assert_match(/language:C\+\+/, added_query)

    filtered = kandelo_run_wasm(
      bin/"readtags", ["-t", "tags", "-ne", "-Q", '(eq? $language "C++")', "-l"],
      env: env, writable_host_directories: mount
    )
    assert_match(/^Added\tadded\.cpp\t/, filtered)
    assert_match(/^compute\twidget\.cpp\t/, filtered)
    refute_match(/^c_helper\t/, filtered)

    upper = testpath/"upper.cpp"
    lower = testpath/"lower.cpp"
    upper.write("int UpperCaseSymbol() { return 1; }\n")
    lower.write("int lower_case_symbol() { return 2; }\n")
    case_tags = kandelo_run_wasm(
      bin/"ctags", ["--options=NONE", "--fields=+K", "-f", "-", "Case.cpp", "case.cpp"],
      env:         { "KERNEL_CWD" => "/src" },
      guest_files: { "/src/Case.cpp" => upper, "/src/case.cpp" => lower }
    )
    assert_match(/^UpperCaseSymbol\tCase\.cpp\t/, case_tags)
    assert_match(/^lower_case_symbol\tcase\.cpp\t/, case_tags)
  end
end
