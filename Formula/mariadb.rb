require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Mariadb < Formula
  include KandeloFormulaSupport

  desc "Relational database server for Kandelo"
  homepage "https://mariadb.org/"
  url "https://archive.mariadb.org/mariadb-10.5.28/source/mariadb-10.5.28.tar.gz"
  sha256 "0b5070208da0116640f20bd085f1136527f998cc23268715bcbf352e7b7f3cc1"
  license "GPL-2.0-only"

  depends_on "binaryen" => :build
  depends_on "bison" => :build
  depends_on "cmake" => :build
  depends_on "pkgconf" => :build
  depends_on "wabt" => [:build, :test]
  depends_on "automattic/kandelo-homebrew/libcxx"
  depends_on "automattic/kandelo-homebrew/ncurses"
  depends_on "automattic/kandelo-homebrew/openssl"
  depends_on "automattic/kandelo-homebrew/pcre2"
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "bin"

  patch :DATA

  def install
    # The first-class PCRE2 and ncurses formulae currently publish wasm32
    # artifacts only. Reject wasm64 rather than mixing target archives or
    # silently rebuilding private copies of direct dependencies.
    kandelo_require_arch!("wasm32")

    libcxx = formula_opt_prefix("automattic/kandelo-homebrew/libcxx")
    ncurses = formula_opt_prefix("automattic/kandelo-homebrew/ncurses")
    openssl = formula_opt_prefix("automattic/kandelo-homebrew/openssl")
    pcre2 = formula_opt_prefix("automattic/kandelo-homebrew/pcre2")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    openssl_version = kandelo_formula("automattic/kandelo-homebrew/openssl").version.to_s
    guest_prefix = "/home/linuxbrew/.linuxbrew/opt/mariadb"
    host_cmake = kandelo_host_tool("cmake")

    # MariaDB generates parsers, error tables, and SQL token data with native
    # executables. Build only those generators on the host, then import them
    # into the target CMake graph.
    host_build = buildpath/"host-build"
    system host_cmake, "-S", ".", "-B", host_build,
      "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
      "-DCMAKE_C_COMPILER=cc",
      "-DCMAKE_CXX_COMPILER=c++",
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
      "-DWITH_READLINE=ON",
      "-DWITH_ZLIB=bundled"
    system host_cmake, "--build", host_build, "--parallel", ENV.make_jobs,
      "--target", "import_executables"

    kandelo_wasm_build do |root|
      toolchain = buildpath/"kandelo-mariadb-toolchain.cmake"
      write_toolchain(toolchain, root, libcxx, ncurses, openssl, pcre2, zlib)

      pkgconfig_dirs = [ncurses, openssl, pcre2, zlib].map { |dep| dep/"lib/pkgconfig" }
      ENV["PKG_CONFIG_LIBDIR"] = pkgconfig_dirs.join(File::PATH_SEPARATOR)
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")

      prefix_maps = {
        buildpath.to_s  => "/usr/src/mariadb",
        root.to_s       => "/usr/src/kandelo",
        HOMEBREW_PREFIX => "/home/linuxbrew/.linuxbrew",
        "/nix/store"    => "/usr/src/toolchain",
      }.flat_map do |from, to|
        %W[
          -ffile-prefix-map=#{from}=#{to}
          -fdebug-prefix-map=#{from}=#{to}
          -fmacro-prefix-map=#{from}=#{to}
        ]
      end
      common_flags = [
        "-O2",
        "-DNDEBUG",
        "-gline-tables-only",
        "-fdebug-compilation-dir=.",
        *prefix_maps,
        "-I#{ncurses}/include",
        "-I#{openssl}/include",
        "-I#{pcre2}/include",
        "-I#{zlib}/include",
      ].join(" ")
      cxx_flags = [
        common_flags,
        "-fwasm-exceptions",
        "-nostdinc++",
        "-isystem #{libcxx}/include/c++/v1",
      ].join(" ")

      system "cmake", "-S", ".", "-B", "target-build",
        "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        "-DCMAKE_TOOLCHAIN_FILE=#{toolchain}",
        "-DPKG_CONFIG_EXECUTABLE=#{formula_opt_bin("pkgconf")}/pkg-config",
        "-DCMAKE_INSTALL_PREFIX=#{guest_prefix}",
        "-DIMPORT_EXECUTABLES=#{buildpath}/host-build/import_executables.cmake",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_C_FLAGS_RELEASE=#{common_flags}",
        "-DCMAKE_CXX_FLAGS_RELEASE=#{cxx_flags}",
        "-DINSTALL_MYSQLSHAREDIR=share/mysql",
        "-DMYSQL_DATADIR=/home/linuxbrew/.linuxbrew/var/mysql",
        "-DWITH_UNIT_TESTS=OFF",
        "-DWITH_MARIABACKUP=OFF",
        "-DSECURITY_HARDENED=OFF",
        "-DWITH_SAFEMALLOC=OFF",
        "-DWITH_EMBEDDED_SERVER=OFF",
        "-DENABLED_PROFILING=OFF",
        "-DWITHOUT_DYNAMIC_PLUGIN=ON",
        "-DDISABLE_SHARED=ON",
        "-DWITH_SSL=system",
        "-DCONC_WITH_SSL=OPENSSL",
        "-DOPENSSL_ROOT_DIR=#{openssl}",
        "-DOPENSSL_USE_STATIC_LIBS=TRUE",
        "-DOPENSSL_INCLUDE_DIR=#{openssl}/include",
        "-DOPENSSL_SSL_LIBRARY=#{openssl}/lib/libssl.a",
        "-DOPENSSL_CRYPTO_LIBRARY=#{openssl}/lib/libcrypto.a",
        "-DWITH_PCRE=system",
        "-DWITH_READLINE=ON",
        "-DWITH_ZLIB=system",
        "-DWITH_SYSTEMD=no",
        "-DWITH_WSREP=OFF",
        "-DDISABLE_THREADPOOL=ON",
        "-DPLUGIN_INNODB=STATIC",
        "-DPLUGIN_INNOBASE=STATIC",
        "-DPLUGIN_XTRADB=NO",
        "-DPLUGIN_CONNECT=NO",
        "-DPLUGIN_ROCKSDB=NO",
        "-DPLUGIN_TOKUDB=NO",
        "-DPLUGIN_MROONGA=NO",
        "-DPLUGIN_SPIDER=NO",
        "-DPLUGIN_OQGRAPH=NO",
        "-DPLUGIN_SPHINX=NO",
        "-DPLUGIN_COLUMNSTORE=NO",
        "-DPLUGIN_S3=NO",
        "-DPLUGIN_PERFSCHEMA=NO",
        "-DPLUGIN_CRACKLIB_PASSWORD_CHECK=NO",
        "-DPLUGIN_AUTH_GSSAPI=NO",
        "-DPLUGIN_AUTH_PAM=NO",
        "-DPLUGIN_FEEDBACK=NO",
        "-DPLUGIN_QUERY_RESPONSE_TIME=NO",
        "-DPLUGIN_SERVER_AUDIT=NO",
        "-DPLUGIN_DISKS=NO",
        "-DPLUGIN_METADATA_LOCK_INFO=NO",
        "-DPLUGIN_QUERY_CACHE_INFO=NO",
        "-DPLUGIN_LOCALE_INFO=NO",
        "-DPLUGIN_SIMPLE_PASSWORD_CHECK=NO",
        "-DPLUGIN_ARIA=STATIC",
        "-DPLUGIN_MYISAM=STATIC",
        "-DPLUGIN_MYISAMMRG=STATIC",
        "-DPLUGIN_CSV=STATIC",
        "-DPLUGIN_HEAP=STATIC",
        "-DPLUGIN_PARTITION=STATIC",
        "-DSTACK_DIRECTION=-1",
        "-DHAVE_LLVM_LIBCPP=OFF",
        "-DZLIB_INCLUDE_DIR=#{zlib}/include",
        "-DZLIB_LIBRARY=#{zlib}/lib/libz.a",
        "-DPCRE_INCLUDE_DIRS=#{pcre2}/include",
        "-DPCRE_LIBRARY_DIRS=#{pcre2}/lib",
        "-DHAVE_PCRE2_MATCH_8=1",
        "-DNEEDS_PCRE2_DEBIAN_HACK=FALSE"

      validate_tls_configuration!(openssl)

      system "cmake", "--build", "target-build", "--parallel", ENV.make_jobs,
        "--target", "mariadbd"
      system "cmake", "--build", "target-build", "--parallel", ENV.make_jobs,
        "--target", "mariadb-test"

      mariadbd = buildpath/"target-build/sql/mariadbd"
      mariadb_test = buildpath/"target-build/client/mariadb-test"
      validate_tls_linkage!(openssl, buildpath/"target-build/sql/CMakeFiles/mariadbd.dir/link.txt")
      validate_tls_linkage!(openssl, buildpath/"target-build/client/CMakeFiles/mariadb-test.dir/link.txt")
      prepare_artifact(root, mariadbd, openssl_version)
      prepare_artifact(root, mariadb_test, openssl_version)

      bin.install mariadbd => "mariadbd"
      bin.install mariadb_test => "mariadb-test"
      bin.install_symlink "mariadb-test" => "mysqltest"
    end

    mysql_share = share/"mysql"
    mysql_share.install "scripts/mysql_system_tables.sql"
    mysql_share.install "scripts/mysql_system_tables_data.sql"
    mysql_share.install "sql/share/charsets"
    (buildpath/"target-build/sql/share").glob("*/errmsg.sys").each do |errmsg|
      (mysql_share/errmsg.dirname.basename).install errmsg
    end
  end

  test do
    assert_path_exists bin/"mariadbd"
    assert_path_exists bin/"mariadb-test"
    assert_equal "mariadb-test", (bin/"mysqltest").readlink.to_s
    assert_path_exists share/"mysql/english/errmsg.sys"
    assert_path_exists share/"mysql/mysql_system_tables.sql"
    assert_path_exists share/"mysql/mysql_system_tables_data.sql"

    output = kandelo_run_wasm(bin/"mariadbd", ["--no-defaults", "--version"], merge_stderr: true)
    assert_match(/Ver 10\.5\.28-MariaDB for Linux on wasm32/, output)

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

        for (int i = 0; i < 120; i++) {
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
        int exited = wait_for_exit(bootstrap, 15, &status);
        if (exited == 0) {
          kill(bootstrap, SIGTERM);
          exited = wait_for_exit(bootstrap, 2, &status);
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

        exited = wait_for_exit(server_pid, 30, &status);
        if (exited != 1 || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
          kill(server_pid, SIGKILL);
          waitpid(server_pid, &status, 0);
          return 8;
        }
        puts("mariadb-lifecycle-ok");
        return 0;
      }
    C
    kandelo_wasm_build do |root|
      system kandelo_cc(root), supervisor_source, "-O2", "-o", supervisor
      kandelo_fork_instrument(supervisor)
    end

    guest_files = {
      "/usr/share/mariadb/bootstrap.sql"      => bootstrap_sql,
      "/usr/share/mariadb/homebrew.test"      => query_test,
      "/usr/share/mariadb/english/errmsg.sys" => share/"mysql/english/errmsg.sys",
    }
    (share/"mysql/charsets").glob("*").select(&:file?).each do |charset|
      guest_files["/usr/share/mariadb/charsets/#{charset.basename}"] = charset
    end
    lifecycle = kandelo_run_wasm(
      supervisor, [],
      env:                       { "TIMEOUT" => "90000" },
      network:                   true,
      exec_programs:             {
        "/usr/sbin/mariadbd"    => bin/"mariadbd",
        "/usr/bin/mariadb-test" => bin/"mariadb-test",
      },
      guest_files:               guest_files,
      expected_fork_descendants: 3
    )
    assert_includes lifecycle, "mariadb-homebrew-ok"
    assert_includes lifecycle, "mariadb-lifecycle-ok"
    openssl_version = kandelo_formula("automattic/kandelo-homebrew/openssl").version.to_s
    assert_includes lifecycle, "OpenSSL #{openssl_version}"

    tls_identity_markers = ["wolfSSL", "wolfcrypt", "/extra/wolfssl/"]
    [bin/"mariadbd", bin/"mariadb-test"].each do |artifact|
      contents = File.binread(artifact)
      paths = [prefix.to_s, buildpath.to_s, "/private/tmp/", "/Users/", "/nix/store/"].reject(&:empty?)
      paths.each do |path|
        refute contents.include?(path), "#{artifact} contains build path #{path}"
      end
      assert_includes contents, "OpenSSL #{openssl_version}"
      tls_identity_markers.each do |marker|
        refute_includes contents, marker, "#{artifact} contains bundled WolfSSL identity #{marker}"
      end
    end
  end

  private

  def write_toolchain(path, root, libcxx, ncurses, openssl, pcre2, zlib)
    path.write <<~CMAKE
      set(CMAKE_SYSTEM_NAME Linux)
      set(CMAKE_SYSTEM_PROCESSOR wasm32)
      set(CMAKE_CROSSCOMPILING TRUE)
      set(CMAKE_SYSROOT "#{root}/sysroot")
      set(CMAKE_C_COMPILER "#{kandelo_cc(root)}")
      set(CMAKE_CXX_COMPILER "#{kandelo_tool("c++", root)}")
      set(CMAKE_AR "#{kandelo_ar(root)}" CACHE FILEPATH "")
      set(CMAKE_RANLIB "#{kandelo_ranlib(root)}" CACHE FILEPATH "")
      set(CMAKE_NM "#{kandelo_tool("nm", root)}" CACHE FILEPATH "")
      set(CMAKE_SIZEOF_VOID_P 4)
      set(CMAKE_C_SIZEOF_DATA_PTR 4)
      set(CMAKE_CXX_SIZEOF_DATA_PTR 4)
      set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)
      set(CMAKE_FIND_ROOT_PATH "#{root}/sysroot;#{libcxx};#{ncurses};#{openssl};#{pcre2};#{zlib}")
      set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
      set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
      set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
      set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
      set(CMAKE_EXE_LINKER_FLAGS_INIT "-L#{libcxx}/lib -L#{ncurses}/lib -L#{openssl}/lib -L#{pcre2}/lib -L#{zlib}/lib")
      set(CMAKE_CXX_STANDARD_LIBRARIES "-lc++ -lc++abi")

      set(SIZEOF_CHAR 1 CACHE STRING "")
      set(SIZEOF_SHORT 2 CACHE STRING "")
      set(SIZEOF_INT 4 CACHE STRING "")
      set(SIZEOF_LONG 4 CACHE STRING "")
      set(SIZEOF_LONG_LONG 8 CACHE STRING "")
      set(SIZEOF_OFF_T 8 CACHE STRING "")
      set(SIZEOF_CHARP 4 CACHE STRING "")
      set(SIZEOF_VOIDP 4 CACHE STRING "")

      set(HAVE_BFILL 0 CACHE INTERNAL "")
      set(HAVE_BZERO 0 CACHE INTERNAL "")
      set(HAVE_GETPASSPHRASE 0 CACHE INTERNAL "")
      set(HAVE_GETPASS 0 CACHE INTERNAL "")
      set(HAVE_AIO_READ 0 CACHE INTERNAL "")
      set(HAVE_AIO_WRITE 0 CACHE INTERNAL "")
      set(HAVE_TIMER_CREATE 0 CACHE INTERNAL "")
      set(HAVE_TIMER_SETTIME 0 CACHE INTERNAL "")
      set(HAVE_KQUEUE 0 CACHE INTERNAL "")
      set(HAVE_SETNS 0 CACHE INTERNAL "")
      set(HAVE_LINUX_UNISTD_H 0 CACHE INTERNAL "")
      set(HAVE_SYS_IOCTL_H 1 CACHE INTERNAL "")
      set(HAVE_TCGETATTR 1 CACHE INTERNAL "")
      set(HAVE_TELL 0 CACHE INTERNAL "")
      set(HAVE_PRINTSTACK 0 CACHE INTERNAL "")
      set(HAVE_BACKTRACE 0 CACHE INTERNAL "")
      set(HAVE_BACKTRACE_SYMBOLS 0 CACHE INTERNAL "")
      set(HAVE_BACKTRACE_SYMBOLS_FD 0 CACHE INTERNAL "")
      set(HAVE_ACCEPT4 0 CACHE INTERNAL "")
      set(HAVE_ABI_CXA_DEMANGLE 0 CACHE INTERNAL "")
      set(HAVE_CXX_NEW 0 CACHE INTERNAL "")
      set(HAVE_CRYPT 0 CACHE INTERNAL "")
      set(HAVE_CUSERID 0 CACHE INTERNAL "")
      set(HAVE_FEDISABLEEXCEPT 0 CACHE INTERNAL "")
      set(HAVE_GETHRTIME 0 CACHE INTERNAL "")
      set(HAVE_GETIFADDRS 0 CACHE INTERNAL "")
      set(HAVE_GETMNTENT 0 CACHE INTERNAL "")
      set(HAVE_GETHOSTBYADDR_R 0 CACHE INTERNAL "")
      set(HAVE_INITGROUPS 0 CACHE INTERNAL "")
      set(HAVE_MALLINFO 0 CACHE INTERNAL "")
      set(HAVE_MALLINFO2 0 CACHE INTERNAL "")
      set(HAVE_MEMALIGN 0 CACHE INTERNAL "")
      set(HAVE_MLOCKALL 0 CACHE INTERNAL "")
      set(HAVE_MMAP64 0 CACHE INTERNAL "")
      set(HAVE_PTHREAD_ATTR_CREATE 0 CACHE INTERNAL "")
      set(HAVE_PTHREAD_CONDATTR_CREATE 0 CACHE INTERNAL "")
      set(HAVE_PTHREAD_GETAFFINITY_NP 0 CACHE INTERNAL "")
      set(HAVE_PTHREAD_GETATTR_NP 0 CACHE INTERNAL "")
      set(HAVE_PTHREAD_YIELD_NP 0 CACHE INTERNAL "")
      set(HAVE_READ_REAL_TIME 0 CACHE INTERNAL "")
      set(HAVE_READDIR_R 0 CACHE INTERNAL "")
      set(HAVE_RWLOCK_INIT 0 CACHE INTERNAL "")
      set(HAVE_SETMNTENT 0 CACHE INTERNAL "")
      set(HAVE_SIGTHREADMASK 0 CACHE INTERNAL "")
      set(HAVE_THR_YIELD 0 CACHE INTERNAL "")
      set(HAVE_UCONTEXT_H 0 CACHE INTERNAL "")
      set(HAVE_VFORK 0 CACHE INTERNAL "")
      set(HAVE_MALLOC_ZONE 0 CACHE INTERNAL "")
      set(HAVE_POSIX_FALLOCATE 0 CACHE INTERNAL "")
      set(HAVE_SYS_PRCTL_H 0 CACHE INTERNAL "")
      set(HAVE_SYS_SYSCALL_H 0 CACHE INTERNAL "")
      set(HAVE_LINK_H 0 CACHE INTERNAL "")
      set(HAVE_MALLOC_H 0 CACHE INTERNAL "")

      set(WITH_SSL "system" CACHE STRING "" FORCE)
      set(CONC_WITH_SSL "OPENSSL" CACHE STRING "" FORCE)
      set(GNUTLS_FOUND FALSE CACHE BOOL "" FORCE)
      set(GNUTLS_LIBRARY "GNUTLS_LIBRARY-NOTFOUND" CACHE FILEPATH "" FORCE)
      set(GNUTLS_INCLUDE_DIR "GNUTLS_INCLUDE_DIR-NOTFOUND" CACHE PATH "" FORCE)
      set(OPENSSL_ROOT_DIR "#{openssl}" CACHE PATH "" FORCE)
      set(OPENSSL_USE_STATIC_LIBS TRUE CACHE BOOL "" FORCE)
      set(OPENSSL_INCLUDE_DIR "#{openssl}/include" CACHE PATH "" FORCE)
      set(OPENSSL_SSL_LIBRARY "#{openssl}/lib/libssl.a" CACHE FILEPATH "" FORCE)
      set(OPENSSL_CRYPTO_LIBRARY "#{openssl}/lib/libcrypto.a" CACHE FILEPATH "" FORCE)

      set(CURSES_FOUND TRUE CACHE BOOL "" FORCE)
      set(CURSES_INCLUDE_PATH "#{ncurses}/include" CACHE PATH "" FORCE)
      set(CURSES_INCLUDE_DIRS "#{ncurses}/include" CACHE PATH "" FORCE)
      set(CURSES_LIBRARY "#{ncurses}/lib/libtinfow.a" CACHE FILEPATH "" FORCE)
      set(CURSES_LIBRARIES "#{ncurses}/lib/libncursesw.a;#{ncurses}/lib/libtinfow.a" CACHE STRING "" FORCE)
      set(CURSES_HAVE_CURSES_H TRUE CACHE BOOL "" FORCE)
      set(HAVE_TPUTS_IN_CURSES TRUE CACHE BOOL "" FORCE)
      set(HAVE_SETUPTERM TRUE CACHE BOOL "" FORCE)
      set(HAVE_VIDATTR TRUE CACHE BOOL "" FORCE)

      set(PCRE_INCLUDE_DIRS "#{pcre2}/include" CACHE PATH "" FORCE)
      set(PCRE_LIBRARY_DIRS "#{pcre2}/lib" CACHE PATH "" FORCE)
      set(HAVE_PCRE2_MATCH_8 1 CACHE INTERNAL "" FORCE)
      set(NEEDS_PCRE2_DEBIAN_HACK FALSE CACHE BOOL "" FORCE)
      set(ZLIB_INCLUDE_DIR "#{zlib}/include" CACHE PATH "" FORCE)
      set(ZLIB_LIBRARY "#{zlib}/lib/libz.a" CACHE FILEPATH "" FORCE)
      set(ENABLE_DTRACE OFF CACHE BOOL "" FORCE)
    CMAKE
  end

  def validate_tls_configuration!(openssl)
    cache = (buildpath/"target-build/CMakeCache.txt").read.each_line.filter_map do |line|
      next if line.start_with?("#", "//") || line.exclude?("=")

      key_and_type, value = line.chomp.split("=", 2)
      [key_and_type.split(":", 2).first, value]
    end.to_h
    expected = {
      "WITH_SSL"               => "system",
      "CONC_WITH_SSL"          => "OPENSSL",
      "OPENSSL_ROOT_DIR"       => openssl.to_s,
      "OPENSSL_INCLUDE_DIR"    => (openssl/"include").to_s,
      "OPENSSL_SSL_LIBRARY"    => (openssl/"lib/libssl.a").to_s,
      "OPENSSL_CRYPTO_LIBRARY" => (openssl/"lib/libcrypto.a").to_s,
    }
    expected.each do |key, value|
      next if cache[key] == value

      odie "MariaDB TLS dependency drifted: #{key}=#{cache[key].inspect}, expected #{value.inspect}"
    end
    odie "MariaDB configured its bundled WolfSSL build" if (buildpath/"target-build/extra/wolfssl").exist?
  end

  def validate_tls_linkage!(openssl, link_file)
    odie "MariaDB TLS link command is missing: #{link_file}" unless link_file.file?

    command = link_file.read
    [openssl/"lib/libssl.a", openssl/"lib/libcrypto.a"].each do |archive|
      odie "MariaDB TLS link command omitted declared dependency #{archive}" unless command.include?(archive.to_s)
    end
    odie "MariaDB TLS link command selected bundled WolfSSL" if command.match?(/wolfssl/i)
  end

  def prepare_artifact(root, artifact, openssl_version)
    guards = "#{root}/scripts/wasm-artifact-guards.sh"
    instrument = "#{root}/scripts/run-wasm-fork-instrument.sh"
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

    contents = File.binread(artifact)
    odie "#{artifact} does not identify declared OpenSSL #{openssl_version}" unless
      contents.include?("OpenSSL #{openssl_version}")
    ["wolfSSL", "wolfcrypt", "/extra/wolfssl/"].each do |marker|
      odie "#{artifact} contains bundled WolfSSL identity #{marker}" if contents.include?(marker)
    end

    kandelo_validate_wasm_artifact(artifact, fork: :auto)

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
    validator_command = [
      "node", "--experimental-wasm-exnref", "--import", "tsx/esm",
      validator, artifact
    ].shelljoin
    system "bash", "-c", "cd #{root.to_s.shellescape} && #{validator_command}"
    chmod 0755, artifact
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
