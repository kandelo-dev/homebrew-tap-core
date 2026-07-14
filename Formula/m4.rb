require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class M4 < Formula
  include KandeloFormulaSupport

  desc "GNU macro processor for Kandelo"
  homepage "https://www.gnu.org/software/m4/"
  url "https://ftpmirror.gnu.org/gnu/m4/m4-1.4.21.tar.xz"
  mirror "https://ftp.gnu.org/gnu/m4/m4-1.4.21.tar.xz"
  sha256 "f25c6ab51548a73a75558742fb031e0625d6485fe5f9155949d6486a2408ab66"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/dash"

  skip_clean "bin/m4"

  # Gnulib identifies musl from the GNU host tuple but assumes that every
  # musl compiler also defines __linux__. Kandelo intentionally does not.
  patch :DATA

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      # The SDK site owns target facts; this gnulib runtime probe is package-specific.
      ENV["gl_cv_func_strerror_0_works"] = "yes"

      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-nls",
        "--disable-dependency-tracking",
        "--with-syscmd-shell=dash"
      system "make", "-j#{ENV.make_jobs}"
      kandelo_validate_wasm_artifact(buildpath/"src/m4", fork: :forbidden)

      system "make", "install"
    end
  end

  test do
    assert_match(/m4(?:\.wasm)? \(GNU M4\) 1\.4\.21$/,
      kandelo_run_wasm(bin/"m4", ["--version"]))

    dash = testpath/"dash"
    dash.binwrite((formula_opt_bin("automattic/kandelo-homebrew/dash")/"dash").binread)
    dash.chmod 0755

    definitions = testpath/"definitions.m4"
    definitions.write("define(`VALUE', `42')dnl\n")
    source = <<~M4
      include(`definitions.m4')dnl
      define(`triple', `$1-$1-$1')dnl
      triple(Kandelo):VALUE
      esyscmd(`printf child-process')
      ifelse(sysval, `0', `child-ok', `child-failed')
    M4

    assert_equal "Kandelo-Kandelo-Kandelo:42\nchild-process\nchild-ok\n",
      kandelo_run_wasm(
        bin/"m4", [], env: { "KERNEL_CWD" => testpath, "KERNEL_PATH" => testpath }, stdin: source
      )
  end
end

__END__
diff --git a/lib/getlocalename_l-unsafe.c b/lib/getlocalename_l-unsafe.c
index 67479be..b212a7b 100644
--- a/lib/getlocalename_l-unsafe.c
+++ b/lib/getlocalename_l-unsafe.c
@@ -37 +37 @@
-#if (__GLIBC__ >= 2 && !defined __UCLIBC__) || (defined __linux__ && HAVE_LANGINFO_H) || defined __CYGWIN__
+#if (__GLIBC__ >= 2 && !defined __UCLIBC__) || ((defined __linux__ || MUSL_LIBC) && HAVE_LANGINFO_H) || defined __CYGWIN__
@@ -468 +468 @@ getlocalename_l_unsafe (int category, locale_t locale)
-#elif defined __linux__ && HAVE_LANGINFO_H && defined NL_LOCALE_NAME
+#elif (defined __linux__ || MUSL_LIBC) && HAVE_LANGINFO_H && defined NL_LOCALE_NAME
