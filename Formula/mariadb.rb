require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Mariadb < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/mariadb".freeze

  desc "Relational database server and test client for Kandelo"
  homepage "https://mariadb.org/"
  url "https://archive.mariadb.org/mariadb-10.5.28/source/mariadb-10.5.28.tar.gz"
  version "10.5.28"
  sha256 "0b5070208da0116640f20bd085f1136527f998cc23268715bcbf352e7b7f3cc1"
  license "GPL-2.0-only"

  depends_on "bison" => :build
  depends_on "cmake" => :build
  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build
  depends_on "llvm" => :build
  depends_on "make" => :build
  depends_on "kandelo-dev/tap-core/libcxx"
  depends_on "kandelo-dev/tap-core/ncurses"
  depends_on "kandelo-dev/tap-core/openssl"
  depends_on "kandelo-dev/tap-core/pcre2"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/mariadbd", "bin/mariadb-test"

  patch :DATA

  def install
    # ncurses is not yet published for wasm64. Reject that architecture rather
    # than pretending libc is a curses implementation or privately rebuilding
    # a dependency outside Homebrew's package graph.
    kandelo_require_arch!("wasm32")

    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "e80180fa36d7ac7a97ab50edae62f4faf3af082806bb04fd5d2ccccb83a9b408",
      script_env:      {
        # WHY: schema-3 mounts the versioned native dependency kegs, while the
        # SDK LLVM directory appears earlier on PATH for target compilation.
        # Passing these Formula-owned roots lets the recipe select native tools
        # without accepting caller-controlled executable paths.
        "MARIADB_NATIVE_BISON_DIR" => kandelo_formula("bison").prefix,
        "MARIADB_NATIVE_CMAKE_DIR" => kandelo_formula("cmake").prefix,
        "MARIADB_NATIVE_LLVM_DIR"  => kandelo_formula("llvm").prefix,
        "MARIADB_NATIVE_MAKE_DIR"  => kandelo_formula("make").prefix,
      },
    )

    mariadbd = out_dir/"bin/mariadbd.wasm"
    mariadb_test = out_dir/"bin/mariadb-test.wasm"
    target_dependencies = %w[libcxx ncurses openssl pcre2 zlib].map do |formula|
      formula_opt_prefix("kandelo-dev/tap-core/#{formula}")
    end
    root = kandelo_require_root!
    guards = "#{root}/scripts/wasm-artifact-guards.sh"
    instrument = "#{root}/scripts/run-wasm-fork-instrument.sh"
    openssl_version = kandelo_formula("kandelo-dev/tap-core/openssl").version.to_s
    forbidden_tls_markers = ["wolfSSL", "wolfcrypt", "/extra/wolfssl/"]

    # WHY: the static publisher admits only audited Formula hooks. Keeping
    # finalization in `install` makes both artifacts visible to that audit
    # instead of hiding executable behavior behind a private helper.
    [mariadbd, mariadb_test].each do |artifact|
      system "bash", "-c", <<~SH
        set -euo pipefail
        . #{guards.shellescape}
        artifact=#{artifact.to_s.shellescape}
        if wasm_imports_kernel_fork "$artifact"; then
          #{instrument.shellescape} "$artifact" -o "$artifact.instrumented"
          mv "$artifact.instrumented" "$artifact"
        fi
        wasm-strip -k name -k target_features -k wasm-posix-abi "$artifact"
      SH

      contents = artifact.binread
      odie "#{artifact} does not identify declared OpenSSL #{openssl_version}" unless
        contents.include?("OpenSSL #{openssl_version}")
      forbidden_tls_markers.each do |marker|
        odie "#{artifact} contains bundled WolfSSL identity #{marker}" if contents.include?(marker)
      end
      kandelo_validate_wasm_artifact(
        artifact, fork: :auto, forbidden_paths: target_dependencies
      )

      # `--allow-undefined` is required by Kandelo's syscall link model. Prove
      # it did not turn an accidental native or undeclared target symbol into
      # a new host ABI import.
      validator = buildpath/"validate-#{artifact.basename}.mjs"
      validator.write <<~JS
        import { readFileSync } from "node:fs";

        const [artifact] = process.argv.slice(2);
        const bytes = readFileSync(artifact);
        const module = await WebAssembly.compile(bytes);
        const imports = WebAssembly.Module.imports(module);
        const allowedEnvImports = new Set(["memory", "__channel_base", "__cxa_thread_atexit"]);
        const unexpectedImports = imports.filter(({ module, name }) =>
          (module !== "env" && module !== "kernel") ||
          (module === "env" && !allowedEnvImports.has(name)));
        if (unexpectedImports.length !== 0) {
          const names = unexpectedImports.map(({ module, name }) => `${module}.${name}`);
          throw new Error(`MariaDB has unexpected host imports: ${names.join(", ")}`);
        }
      JS
      command = [
        "node", "--experimental-wasm-exnref", "--import", "tsx/esm",
        validator, artifact
      ].shelljoin
      system "bash", "-c", "cd #{root.shellescape} && #{command}"
      chmod 0755, artifact
    end

    kandelo_install_bin(out_dir/"bin", "mariadbd.wasm", "mariadbd")
    kandelo_install_bin(out_dir/"bin", "mariadb-test.wasm", "mariadb-test")
    bin.install_symlink "mariadb-test" => "mysqltest"
    share.install (out_dir/"share").children
    prefix.install out_dir/"mysql-test"
  end

  test do
    assert_path_exists bin/"mariadbd"
    assert_path_exists bin/"mariadb-test"
    assert_equal "mariadb-test", (bin/"mysqltest").readlink.to_s
    assert_path_exists share/"mysql/english/errmsg.sys"
    assert_path_exists share/"mysql/mysql_system_tables.sql"
    assert_path_exists share/"mysql/mysql_system_tables_data.sql"
    assert_path_exists share/"mysql/charsets/Index.xml"
    assert_path_exists prefix/"mysql-test/main"

    version = kandelo_run_wasm(
      bin/"mariadbd", ["--no-defaults", "--version"], merge_stderr: true
    )
    assert_match(/Ver 10\.5\.28-MariaDB for Linux on wasm32/, version)

    bootstrap_sql = testpath/"bootstrap.sql"
    bootstrap_sql.write <<~SQL
      USE mysql;
      #{(share/"mysql/mysql_system_tables.sql").read}
      #{(share/"mysql/mysql_system_tables_data.sql").read}
      CREATE DATABASE IF NOT EXISTS test;
    SQL
    query_test = testpath/"homebrew.test"
    query_test.write <<~SQL
      CREATE DATABASE IF NOT EXISTS kandelo_homebrew;
      USE kandelo_homebrew;
      CREATE TABLE messages (id INTEGER PRIMARY KEY, body VARCHAR(64)) ENGINE=Aria;
      INSERT INTO messages VALUES (1, 'mariadb-homebrew-ok');
      SELECT id, body FROM messages;
      SHOW VARIABLES LIKE 'version_ssl_library';
      SHUTDOWN;
    SQL

    # WHY: the lifecycle supervisor performs bootstrap, socket readiness,
    # client execution, and exact child reaping inside Kandelo. A host-side port
    # probe would bypass the virtual network and could not prove fork/exec/wait
    # behavior shared by the Node and Chromium hosts.
    supervisor_source = testpath/"mariadb-supervisor.c"
    supervisor = testpath/"mariadb-supervisor.wasm"
    supervisor_source.write <<~C
      #include <arpa/inet.h>
      #include <errno.h>
      #include <fcntl.h>
      #include <signal.h>
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <sys/socket.h>
      #include <sys/stat.h>
      #include <sys/types.h>
      #include <sys/wait.h>
      #include <unistd.h>

      static const char *server = "/usr/sbin/mariadbd";
      static const char *client = "/usr/bin/mariadb-test";
      static const char *data = "/tmp/mariadb-data";
      static const char *tmp = "/tmp/mariadb-data/tmp";

      static int wait_for_exit(pid_t pid, int seconds, int *status) {
        for (int i = 0; i < seconds; i++) {
          pid_t result = waitpid(pid, status, WNOHANG);
          if (result == pid) return 1;
          if (result < 0) return -1;
          sleep(1);
        }
        return 0;
      }

      static int wait_for_port(int port) {
        struct sockaddr_in address;
        memset(&address, 0, sizeof(address));
        address.sin_family = AF_INET;
        address.sin_port = htons((unsigned short)port);
        if (inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) != 1) return -1;

        for (int i = 0; i < 240; i++) {
          int fd = socket(AF_INET, SOCK_STREAM, 0);
          if (fd >= 0) {
            int result = connect(fd, (struct sockaddr *)&address, sizeof(address));
            close(fd);
            if (result == 0) return 0;
          }
          usleep(500000);
        }
        return -1;
      }

      static pid_t spawn(char *const argv[], const char *stdin_path) {
        pid_t pid = fork();
        if (pid != 0) return pid;
        if (stdin_path != NULL) {
          int fd = open(stdin_path, O_RDONLY);
          if (fd < 0 || dup2(fd, STDIN_FILENO) < 0) _exit(126);
          close(fd);
        }
        execv(argv[0], argv);
        _exit(127);
      }

      int main(void) {
        int status = 0;
        mkdir(data, 0755);
        mkdir("/tmp/mariadb-data/mysql", 0755);
        mkdir(tmp, 0755);

        char *bootstrap_argv[] = {
          (char *)server, "--no-defaults", "--user=root",
          "--datadir=/tmp/mariadb-data", "--tmpdir=/tmp/mariadb-data/tmp",
          "--lc-messages-dir=/usr/share/mariadb",
          "--character-sets-dir=/usr/share/mariadb/charsets",
          "--default-storage-engine=Aria", "--skip-grant-tables",
          "--key-buffer-size=1048576", "--table-open-cache=10",
          "--sort-buffer-size=262144", "--bootstrap", "--skip-networking",
          "--log-warnings=0", NULL
        };
        pid_t bootstrap = spawn(bootstrap_argv, "/usr/share/mariadb/bootstrap.sql");
        if (bootstrap < 0) return 1;
        int exited = wait_for_exit(bootstrap, 60, &status);
        if (exited == 0) {
          kill(bootstrap, SIGTERM);
          exited = wait_for_exit(bootstrap, 5, &status);
        }
        if (exited == 0) {
          kill(bootstrap, SIGKILL);
          if (waitpid(bootstrap, &status, 0) != bootstrap) return 2;
        } else if (exited < 0) {
          return 3;
        }
        struct stat system_table;
        if (stat("/tmp/mariadb-data/mysql/global_priv.MAI", &system_table) != 0) return 4;
        unlink("/tmp/mariadb-data/aria_log.00000001");
        unlink("/tmp/mariadb-data/aria_log_control");

        char *server_argv[] = {
          (char *)server, "--no-defaults", "--user=root",
          "--datadir=/tmp/mariadb-data", "--tmpdir=/tmp/mariadb-data/tmp",
          "--lc-messages-dir=/usr/share/mariadb",
          "--character-sets-dir=/usr/share/mariadb/charsets",
          "--default-storage-engine=Aria", "--skip-grant-tables",
          "--key-buffer-size=1048576", "--table-open-cache=10",
          "--sort-buffer-size=262144", "--skip-networking=0", "--port=3306",
          "--bind-address=0.0.0.0", "--socket=", "--max-connections=10",
          "--wait-timeout=10", "--net-read-timeout=10", "--net-write-timeout=10",
          NULL
        };
        pid_t server_pid = spawn(server_argv, NULL);
        if (server_pid < 0 || wait_for_port(3306) != 0) return 5;

        char *client_argv[] = {
          (char *)client, "--no-defaults", "--host=127.0.0.1", "--port=3306",
          "--user=root", "--database=mysql", "--protocol=tcp",
          "--test-file=/usr/share/mariadb/homebrew.test",
          "--basedir=/usr/share/mariadb", "--tmpdir=/tmp/mariadb-data/tmp", NULL
        };
        pid_t client_pid = spawn(client_argv, NULL);
        if (client_pid < 0 || waitpid(client_pid, &status, 0) != client_pid) return 6;
        if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) return 7;

        exited = wait_for_exit(server_pid, 60, &status);
        if (exited != 1 || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
          kill(server_pid, SIGKILL);
          waitpid(server_pid, &status, 0);
          return 8;
        }
        const char *runtime = getenv("KANDELO_RUNTIME");
        puts("mariadb-lifecycle-ok");
        printf("mariadb-%s-service-ok\\n", runtime == NULL ? "unknown" : runtime);
        return 0;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, supervisor_source, "-O2", "-o", supervisor
      kandelo_fork_instrument(supervisor)
      kandelo_validate_wasm_artifact(supervisor, fork: :required)
    end

    guest_files = {
      "/usr/share/mariadb/bootstrap.sql"      => bootstrap_sql,
      "/usr/share/mariadb/homebrew.test"      => query_test,
      "/usr/share/mariadb/english/errmsg.sys" => share/"mysql/english/errmsg.sys",
    }
    (share/"mysql/charsets").glob("*").select(&:file?).each do |charset|
      guest_files["/usr/share/mariadb/charsets/#{charset.basename}"] = charset
    end
    exec_programs = {
      "/usr/sbin/mariadbd"    => bin/"mariadbd",
      "/usr/bin/mariadb-test" => bin/"mariadb-test",
    }
    assert_lifecycle = lambda do |output, runtime|
      assert_includes output, "mariadb-homebrew-ok"
      assert_includes output, "mariadb-lifecycle-ok"
      openssl_version = kandelo_formula("kandelo-dev/tap-core/openssl").version.to_s
      assert_includes output, "OpenSSL #{openssl_version}"
      assert_includes output, "mariadb-#{runtime}-service-ok"
    end

    node_lifecycle = kandelo_run_wasm(
      supervisor, [],
      env:                       { "KANDELO_RUNTIME" => "node", "TIMEOUT" => "240000" },
      network:                   true,
      exec_programs:             exec_programs,
      guest_files:               guest_files,
      expected_fork_descendants: 3,
      merge_stderr:              true
    )
    assert_lifecycle.call(node_lifecycle, "node")

    browser_lifecycle = kandelo_run_browser_wasm(
      supervisor, [],
      argv0:              "mariadb-supervisor",
      guest_program_path: "/usr/local/bin/mariadb-supervisor",
      env:                { "KANDELO_RUNTIME" => "browser" },
      exec_programs:      exec_programs,
      guest_files:        guest_files,
      timeout_ms:         240_000,
      allow_stderr:       true,
      merge_stderr:       true
    )
    assert_lifecycle.call(browser_lifecycle, "browser")
  end
end

__END__
diff --git a/cmake/mariadb_connector_c.cmake b/cmake/mariadb_connector_c.cmake
--- a/cmake/mariadb_connector_c.cmake
+++ b/cmake/mariadb_connector_c.cmake
@@ -11 +11 @@
-IF(NOT CONC_WITH_SSL)
+IF(NOT CONC_WITH_SSL AND NOT CONC_WITH_SSL STREQUAL "OFF")
diff --git a/mysys/get_password.c b/mysys/get_password.c
--- a/mysys/get_password.c
+++ b/mysys/get_password.c
@@ -22,0 +23 @@
+#include <ctype.h>
diff --git a/mysys/my_gethwaddr.c b/mysys/my_gethwaddr.c
--- a/mysys/my_gethwaddr.c
+++ b/mysys/my_gethwaddr.c
@@ -26 +26 @@
-#if defined(_AIX) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__linux__) || defined(__sun) || defined(_WIN32)
+#if defined(_AIX) || defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__) || defined(__linux__) || defined(__sun) || defined(_WIN32) || defined(__wasm__)
@@ -83 +83 @@
-#elif defined(_AIX) || defined(__linux__) || defined(__sun)
+#elif defined(_AIX) || defined(__linux__) || defined(__sun) || defined(__wasm__)
@@ -119,2 +119,2 @@
-#if defined(_AIX) || defined(__linux__)
-#if defined(__linux__)
+#if defined(_AIX) || defined(__linux__) || defined(__wasm__)
+#if defined(__linux__) || defined(__wasm__)
diff --git a/mysys/my_largepage.c b/mysys/my_largepage.c
--- a/mysys/my_largepage.c
+++ b/mysys/my_largepage.c
@@ -23 +23 @@
-#if defined(__linux__) || defined(MAP_ALIGNED)
+#if defined(__linux__) || defined(MAP_ALIGNED) || defined(MAP_HUGETLB)
diff --git a/mysys/my_new.cc b/mysys/my_new.cc
--- a/mysys/my_new.cc
+++ b/mysys/my_new.cc
@@ -32 +32 @@
-  return (void *) my_malloc (sz ? sz : 1, MYF(0));
+  return (void *) my_malloc (PSI_NOT_INSTRUMENTED, sz ? sz : 1, MYF(0));
@@ -37 +37 @@
-  return (void *) my_malloc (sz ? sz : 1, MYF(0));
+  return (void *) my_malloc (PSI_NOT_INSTRUMENTED, sz ? sz : 1, MYF(0));
@@ -42 +42 @@
-  return (void *) my_malloc (sz ? sz : 1, MYF(0));
+  return (void *) my_malloc (PSI_NOT_INSTRUMENTED, sz ? sz : 1, MYF(0));
@@ -47 +47 @@
-  return (void *) my_malloc (sz ? sz : 1, MYF(0));
+  return (void *) my_malloc (PSI_NOT_INSTRUMENTED, sz ? sz : 1, MYF(0));
diff --git a/client/mysqltest.cc b/client/mysqltest.cc
--- a/client/mysqltest.cc
+++ b/client/mysqltest.cc
@@ -38,0 +39,7 @@
+/*
+ * Kandelo links mysqltest against libmariadbclient rather than the embedded
+ * server. Route these lifecycle calls to the matching client-library hooks.
+ */
+#define mysql_server_init(a,b,c) mysql_client_plugin_init()
+#define mysql_server_end()       mysql_client_plugin_deinit()
+
