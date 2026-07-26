require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Mariadb < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/mariadb".freeze
  HOST_HELPERS = %w[
    extra/comp_err
    scripts/comp_sql
    dbug/factorial
    sql/gen_lex_hash
    sql/gen_lex_token
  ].freeze

  desc "Relational database server and test client for Kandelo"
  homepage "https://mariadb.org/"
  url "https://archive.mariadb.org/mariadb-10.5.28/source/mariadb-10.5.28.tar.gz"
  sha256 "0b5070208da0116640f20bd085f1136527f998cc23268715bcbf352e7b7f3cc1"
  license "GPL-2.0-only"

  depends_on "bison" => :build
  depends_on "cmake" => :build
  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build
  depends_on "kandelo-dev/tap-core/libcxx"
  depends_on "kandelo-dev/tap-core/pcre2"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/mariadbd", "bin/mysqltest"

  patch :DATA

  def install
    kandelo_require_arch!("wasm32", "wasm64")

    # MariaDB first builds native code generators, then imports their exact
    # paths into the target CMake build. Keep that native phase outside SDK
    # activation so target compiler variables cannot accidentally select the
    # host tools. The reserved resource directory remains stable when the
    # closed recipe moves Homebrew's verified source into its isolated root.
    resource_dir = buildpath/"kandelo-package-resources"
    host_build = resource_dir/"mariadb-host-build"
    resource_dir.mkpath

    inreplace "cmake/mariadb_connector_c.cmake",
      "IF(NOT CONC_WITH_SSL)",
      'IF(NOT CONC_WITH_SSL AND NOT CONC_WITH_SSL STREQUAL "OFF")'
    inreplace "mysys/my_gethwaddr.c" do |s|
      s.gsub!(
        "defined(__linux__) || defined(__sun) || defined(_WIN32)",
        "defined(__linux__) || defined(__sun) || defined(_WIN32) || " \
        "defined(__wasm32__) || defined(__wasm64__)",
      )
      s.gsub!(
        "#elif defined(_AIX) || defined(__linux__) || defined(__sun)",
        "#elif defined(_AIX) || defined(__linux__) || defined(__sun) || " \
        "defined(__wasm32__) || defined(__wasm64__)",
      )
    end

    host_cmake = kandelo_host_tool("cmake")
    llvm_bin = Pathname(ENV.fetch("HOMEBREW_KANDELO_LLVM_BIN"))
    llvm_prefix = llvm_bin.parent
    host_args = [
      "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
      "-DCMAKE_C_COMPILER=#{llvm_bin}/clang",
      "-DCMAKE_CXX_COMPILER=#{llvm_bin}/clang++",
      "-DCMAKE_CXX_FLAGS=-stdlib=libc++ -isystem #{llvm_prefix}/include/c++/v1",
      "-DCMAKE_AR=#{llvm_bin}/llvm-ar",
      "-DCMAKE_RANLIB=#{llvm_bin}/llvm-ranlib",
      "-DWITH_UNIT_TESTS=OFF",
      "-DWITH_MARIABACKUP=OFF",
      "-DPLUGIN_CONNECT=NO",
      "-DPLUGIN_ROCKSDB=NO",
      "-DPLUGIN_TOKUDB=NO",
      "-DPLUGIN_MROONGA=NO",
      "-DPLUGIN_SPIDER=NO",
      "-DPLUGIN_OQGRAPH=NO",
      "-DPLUGIN_PERFSCHEMA=NO",
      "-DPLUGIN_SPHINX=NO",
      "-DPLUGIN_COLUMNSTORE=NO",
      "-DPLUGIN_S3=NO",
      "-DPLUGIN_CRACKLIB_PASSWORD_CHECK=NO",
      "-DWITH_SSL=OFF",
      "-DCONC_WITH_SSL=OFF",
      "-DWITH_PCRE=bundled",
      "-DWITH_EDITLINE=bundled",
      "-DWITH_ZLIB=bundled",
    ]
    system host_cmake, "-S", buildpath, "-B", host_build, *host_args
    system host_cmake, "--build", host_build,
      "--target", "import_executables",
      "--parallel", ENV.make_jobs

    odie "MariaDB did not generate import_executables.cmake" unless
      (host_build/"import_executables.cmake").file?
    HOST_HELPERS.each do |relative|
      helper = host_build/relative
      odie "MariaDB did not build native helper #{relative}" unless helper.executable?
    end

    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "92dd539e5f905b786175e7ad2db73090bfdf72e3ab103fbcc7fa36adba5ad12d",
      script_env:      {
        "MARIADB_HOST_BUILD_DIR" => host_build,
      },
    )

    mariadbd = out_dir/"bin/mariadbd.wasm"
    mysqltest = out_dir/"bin/mysqltest.wasm"
    kandelo_fork_instrument(mariadbd)
    kandelo_fork_instrument(mysqltest)
    kandelo_validate_wasm_artifact(mariadbd, fork: :required)
    kandelo_validate_wasm_artifact(mysqltest, fork: :auto)

    kandelo_install_bin(out_dir/"bin", "mariadbd.wasm", "mariadbd")
    kandelo_install_bin(out_dir/"bin", "mysqltest.wasm", "mysqltest")
    share.install (out_dir/"share").children
    prefix.install out_dir/"mysql-test"
  end

  test do
    assert_path_exists share/"mysql/mysql_system_tables.sql"
    assert_path_exists prefix/"mysql-test/main"

    node_server = kandelo_run_wasm(bin/"mariadbd", ["--version"], merge_stderr: true)
    assert_match(/Ver 10\.5\.28-MariaDB/, node_server)
    node_client = kandelo_run_wasm(bin/"mysqltest", ["--version"], merge_stderr: true)
    assert_match(/mysqltest.*10\.5\.28/i, node_client)

    browser_server = kandelo_run_browser_wasm(
      bin/"mariadbd", ["--version"], allow_stderr: true, merge_stderr: true
    )
    assert_match(/Ver 10\.5\.28-MariaDB/, browser_server)
    browser_client = kandelo_run_browser_wasm(
      bin/"mysqltest", ["--version"], allow_stderr: true, merge_stderr: true
    )
    assert_match(/mysqltest.*10\.5\.28/i, browser_client)
  end
end

__END__
diff --git a/mysys/get_password.c b/mysys/get_password.c
--- a/mysys/get_password.c
+++ b/mysys/get_password.c
@@ -20,6 +20,7 @@
  */
 #include <my_global.h>
 #include <my_sys.h>
+#include <ctype.h>
 #include "mysql.h"
 #include <m_string.h>
 #include <m_ctype.h>
diff --git a/mysys/my_largepage.c b/mysys/my_largepage.c
--- a/mysys/my_largepage.c
+++ b/mysys/my_largepage.c
@@ -22,7 +22,7 @@
 #ifdef __linux__
 #include <dirent.h>
 #endif
-#if defined(__linux__) || defined(MAP_ALIGNED)
+#if defined(__linux__) || defined(MAP_ALIGNED) || defined(MAP_HUGETLB)
 #include "my_bit.h"
 #endif
 #ifdef HAVE_LINUX_MMAN_H
diff --git a/storage/maria/ma_init.c b/storage/maria/ma_init.c
--- a/storage/maria/ma_init.c
+++ b/storage/maria/ma_init.c
@@ -130,6 +130,10 @@ my_bool maria_upgrade()
 {
   char name[FN_REFLEN], new_name[FN_REFLEN];
+#ifdef __wasm__
+  /* Fresh Kandelo databases have no legacy maria_log files to rename. */
+  return 0;
+#endif
   DBUG_ENTER("maria_upgrade");
 
   fn_format(name, "maria_log_control", maria_data_root, "", MYF(MY_WME));
 
diff --git a/mysys/my_new.cc b/mysys/my_new.cc
--- a/mysys/my_new.cc
+++ b/mysys/my_new.cc
@@ -29,22 +29,22 @@
 
 void *operator new (size_t sz)
 {
-  return (void *) my_malloc (sz ? sz : 1, MYF(0));
+  return (void *) my_malloc (PSI_NOT_INSTRUMENTED, sz ? sz : 1, MYF(0));
 }
 
 void *operator new[] (size_t sz)
 {
-  return (void *) my_malloc (sz ? sz : 1, MYF(0));
+  return (void *) my_malloc (PSI_NOT_INSTRUMENTED, sz ? sz : 1, MYF(0));
 }
 
 void* operator new(std::size_t sz, const std::nothrow_t&) throw()
 {
-  return (void *) my_malloc (sz ? sz : 1, MYF(0));
+  return (void *) my_malloc (PSI_NOT_INSTRUMENTED, sz ? sz : 1, MYF(0));
 }
 
 void* operator new[](std::size_t sz, const std::nothrow_t&) throw()
 {
-  return (void *) my_malloc (sz ? sz : 1, MYF(0));
+  return (void *) my_malloc (PSI_NOT_INSTRUMENTED, sz ? sz : 1, MYF(0));
 }
 
 void operator delete (void *ptr, std::size_t)
diff --git a/client/mysqltest.cc b/client/mysqltest.cc
--- a/client/mysqltest.cc
+++ b/client/mysqltest.cc
@@ -36,6 +36,13 @@
 #define MTEST_VERSION "3.5"
 
 #include "client_priv.h"
+/*
+ * Kandelo links mysqltest against libmariadbclient rather than the embedded
+ * server. Route these lifecycle calls to the matching client-library hooks.
+ */
+#define mysql_server_init(a,b,c) mysql_client_plugin_init()
+#define mysql_server_end()       mysql_client_plugin_deinit()
+
 #include <mysql_version.h>
 #include <mysqld_error.h>
 #include <sql_common.h>
