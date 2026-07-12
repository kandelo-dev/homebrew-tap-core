require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Make < Formula
  include KandeloFormulaSupport

  desc "GNU build automation tool for Kandelo"
  homepage "https://www.gnu.org/software/make/"
  url "https://ftpmirror.gnu.org/gnu/make/make-4.4.1.tar.gz"
  mirror "https://ftp.gnu.org/gnu/make/make-4.4.1.tar.gz"
  sha256 "dd16fb1d67bfab79a72f5e8390735c49e3e8e70b4945a15ab1f81ddb78658fb3"
  license "GPL-3.0-or-later"

  depends_on "automattic/kandelo-homebrew/dash"

  skip_clean "bin/make"
  patch :DATA

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      # This package-specific runtime probe cannot run while cross-compiling.
      ENV["make_cv_synchronous_posix_spawn"] = "yes"

      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-nls",
        "--disable-dependency-tracking"
      system "make", "-j#{ENV.make_jobs}"
      system "make", "install"
    end
  end

  test do
    assert_match(/^GNU Make 4\.4\.1$/,
      kandelo_run_wasm(bin/"make", ["--version"]))

    root = kandelo_activate_sdk!
    kandelo_activate_sysroot!(root)
    (testpath/"kandelo_plugin.c").write <<~C
      #include <gnumake.h>

      int plugin_is_GPL_compatible;

      static char *kandelo_plugin(const char *name, unsigned int argc, char **argv) {
        const char prefix[] = "plugin=";
        const char *value = argc == 0 ? "" : argv[0];
        unsigned int prefix_len = sizeof(prefix) - 1;
        unsigned int value_len = 0;
        while (value[value_len] != '\\0')
          ++value_len;

        char *result = gmk_alloc(prefix_len + value_len + 1);
        for (unsigned int i = 0; i < prefix_len; ++i)
          result[i] = prefix[i];
        for (unsigned int i = 0; i < value_len; ++i)
          result[prefix_len + i] = value[i];
        result[prefix_len + value_len] = '\\0';
        return result;
      }

      int kandelo_plugin_gmk_setup(const gmk_floc *floc) {
        gmk_add_function("kandelo-plugin", kandelo_plugin, 1, 1, GMK_FUNC_DEFAULT);
        return 1;
      }
    C
    system kandelo_cc(root), "-shared", "-fPIC", "-I#{include}",
      testpath/"kandelo_plugin.c", "-o", testpath/"kandelo_plugin.so"

    dash = testpath/"dash"
    dash.binwrite((formula_opt_bin("automattic/kandelo-homebrew/dash")/"dash").binread)
    dash.chmod 0755
    recursive_make = testpath/"make"
    recursive_make.binwrite((bin/"make").binread)
    recursive_make.chmod 0755

    (testpath/"alpha.txt").write "alpha\n"
    (testpath/"beta.txt").write "beta\n"
    (testpath/"Makefile").write <<~MAKEFILE
      SHELL := dash
      .PHONY: all
      load kandelo_plugin.so(kandelo_plugin_gmk_setup)
      all: report.txt recursive.txt plugin.txt

      report.txt: alpha.txt beta.txt
      \t@printf 'name=%s inputs=%s\\n' '$(NAME)' '$^' > $@

      recursive.txt:
      \t@make --no-print-directory -f child.mk CHILD='$(NAME)'

      plugin.txt:
      \t@printf '%s\\n' '$(kandelo-plugin $(NAME))' > $@
    MAKEFILE
    (testpath/"child.mk").write <<~MAKEFILE
      SHELL := dash
      all:
      \t@printf 'child=%s\\n' '$(CHILD)' > recursive.txt
    MAKEFILE

    env = { "KERNEL_CWD" => testpath, "KERNEL_PATH" => testpath }
    assert_empty kandelo_run_wasm(bin/"make", ["-j2", "NAME=Kandelo", "all"], env: env)
    assert_equal "name=Kandelo inputs=alpha.txt beta.txt\n", (testpath/"report.txt").read
    assert_equal "child=Kandelo\n", (testpath/"recursive.txt").read
    assert_equal "plugin=Kandelo\n", (testpath/"plugin.txt").read
    up_to_date = kandelo_run_wasm(bin/"make", ["NAME=Kandelo", "all"], env: env)
    assert_match(/\Amake(?:\.wasm)?: Nothing to be done for 'all'\.\n\z/, up_to_date)

    (testpath/"failure.mk").write <<~MAKEFILE
      failure:
      \t@missing-kandelo-command
    MAKEFILE
    failure = kandelo_run_wasm(
      bin/"make", ["-f", "failure.mk", "failure"], env: env, merge_stderr: true, expected_status: 2
    )
    assert_match(/missing-kandelo-command.*No such file or directory/, failure)
  end
end

__END__
diff --git a/src/gnumake.h b/src/gnumake.h
--- a/src/gnumake.h
+++ b/src/gnumake.h
@@ -35,6 +35,8 @@ typedef char *(*gmk_func_ptr)(const char *nm, unsigned int argc, char **argv);
 # else
 #  define GMK_EXPORT  __declspec(dllimport)
 # endif
+#elif defined(__wasm__)
+# define GMK_EXPORT  __attribute__((visibility("default")))
 #else
 # define GMK_EXPORT
 #endif

diff --git a/src/main.c b/src/main.c
index af01f36..b669875 100644
--- a/src/main.c
+++ b/src/main.c
@@ -1160,11 +1160,11 @@ temp_stdin_unlink ()
     }
 }
-
+
-#ifdef MK_OS_ZOS
+#if defined(MK_OS_ZOS) || defined(__wasm__)
 extern char **environ;
 #endif
-
+
-#if defined(_AMIGA) || defined(MK_OS_ZOS)
+#if defined(_AMIGA) || defined(MK_OS_ZOS) || defined(__wasm__)
 int
 main (int argc, char **argv)
 #else
@@ -1482,6 +1482,6 @@ main (int argc, char **argv, char **envp)
      done before $(MAKE) is figured out so its definitions will not be
      from the environment.  */
-
+
-#ifdef MK_OS_ZOS
+#if defined(MK_OS_ZOS) || defined(__wasm__)
   char **envp = environ;
 #endif
