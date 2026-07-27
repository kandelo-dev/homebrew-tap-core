require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Redis < Formula
  include KandeloFormulaSupport

  desc "In-memory data structure server and client for Kandelo"
  homepage "https://redis.io/"
  url "https://github.com/redis/redis/archive/refs/tags/7.2.5.tar.gz"
  sha256 "98a8502a2e902d2a9785ef46a69a5f8d5e24cbf9ea3ae4d845afcfc6778aa783"
  license "BSD-3-Clause"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build
  depends_on "kandelo-dev/tap-core/dash" => :test

  skip_clean "bin/redis-server"
  skip_clean "bin/redis-cli"

  # WHY: Redis declares its module API function-pointer globals before selecting
  # the non-TLS branch. LLVM 21's Wasm backend crashes while emitting that
  # unused global set even when tls.c is compiled at -O0. Keep only those
  # declarations behind Redis' existing TLS feature condition; the normal
  # non-TLS stub remains upstream code.
  patch :DATA

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      # WHY: build directories are ephemeral, but debug strings become bottle
      # bytes. Mapping them to stable guest-source names makes identical inputs
      # reproducible without exposing a publisher or developer filesystem.
      stable_source = "/usr/src/redis-#{version}"
      prefix_maps = {
        buildpath => stable_source,
        root      => "/usr/src/kandelo",
      }.flat_map do |from, to|
        [Pathname(from), Pathname(from).realpath].uniq.flat_map do |source|
          [
            "-ffile-prefix-map=#{source}=#{to}",
            "-fdebug-prefix-map=#{source}=#{to}",
            "-fmacro-prefix-map=#{source}=#{to}",
          ]
        end
      end
      ENV["CFLAGS"] = [
        "-O2",
        "-gline-tables-only",
        "-fdebug-compilation-dir=#{stable_source}",
        *prefix_maps,
      ].join(" ")

      # WHY: Redis' Makefiles inspect the build host even when CC is a cross
      # compiler. Select the Linux-compatible target link set so pthread,
      # realtime, and dl APIs come from Kandelo rather than the macOS host.
      inreplace "src/Makefile" do |s|
        odie "Redis operating-system probe changed" unless s.sub!(/^uname_S :=.*$/, "uname_S := Linux")
        odie "Redis machine probe changed" unless s.sub!(/^uname_M :=.*$/, "uname_M := wasm32")
      end
      inreplace "deps/Makefile" do |s|
        odie "Redis dependency operating-system probe changed" unless s.sub!(/^uname_S:=.*$/, "uname_S:= Linux")
        odie "Redis dependency archiver declaration changed" unless s.sub!(/^AR=ar$/, "AR?=ar")
        odie "Redis dependency archiver flags changed" unless
          s.sub!(/^ARFLAGS=rc$/, "ARFLAGS?=rc\nRANLIB?=ranlib")
        unless s.sub!(
          "cd hiredis && $(MAKE) static $(HIREDIS_MAKE_FLAGS)",
          'cd hiredis && $(MAKE) static $(HIREDIS_MAKE_FLAGS) AR="$(AR)"',
        )
          odie "Redis hiredis dependency rule changed"
        end
        unless s.sub!(
          /^\tcd hdr_histogram && \$\(MAKE\)$/,
          "\tcd hdr_histogram && $(MAKE) AR=\"$(AR)\" ARFLAGS=rcs",
        )
          odie "Redis hdr_histogram dependency rule changed"
        end
        unless s.sub!(
          /^\tcd fpconv && \$\(MAKE\)$/,
          "\tcd fpconv && $(MAKE) AR=\"$(AR)\" ARFLAGS=rcs",
        )
          odie "Redis fpconv dependency rule changed"
        end
        unless s.sub!(
          'AR="$(AR) $(ARFLAGS)"',
          'AR="$(AR) $(ARFLAGS)" RANLIB="$(RANLIB)"',
        )
          odie "Redis Lua dependency rule changed"
        end
      end

      # WHY: the top-level Redis prerequisite recipe ignores a failed dependency
      # sub-make. Build the exact dependency set as a checked step so a missing
      # archive cannot survive as unresolved imports in the linked Wasm.
      system "make", "-C", "deps", "-j#{ENV.make_jobs}",
        "BUILD_TLS=no",
        "hiredis",
        "linenoise",
        "lua",
        "hdr_histogram",
        "fpconv"

      system "make", "-C", "src", "-j#{ENV.make_jobs}",
        "MALLOC=libc",
        "USE_SYSTEMD=no",
        "BUILD_TLS=no",
        "OPTIMIZATION=-O2",
        "redis-server",
        "redis-cli"

      server = buildpath/"src/redis-server.optimized"
      cli = buildpath/"src/redis-cli.optimized"
      system "wasm-opt", "-O2", "--strip-debug", buildpath/"src/redis-server", "-o", server
      system "wasm-opt", "-O2", "--strip-debug", buildpath/"src/redis-cli", "-o", cli
      kandelo_fork_instrument(server)

      kandelo_validate_wasm_artifact(server, fork: :required)
      kandelo_validate_wasm_artifact(cli, fork: :forbidden)

      # WHY: Redis uses Kandelo's dynamic-loader bridge, and the instrumented server
      # imports the continuation frame protocol validated above. Reject every
      # other env import so a suppressed dependency-build failure stays loud.
      system "bash", "-c", <<~SH
        set -euo pipefail
        for artifact in #{server.to_s.shellescape} #{cli.to_s.shellescape}; do
          unexpected_env_imports=$(wasm-objdump -x "$artifact" |
            awk '/<- env[.]/ { sub(/^.*<- env[.]/, ""); print $1 }' |
            grep -Ev '^(__channel_base|memory|__wasm_dlclose|__wasm_dlerror|__wasm_dlopen|__wasm_dlsym|__wpk_fork_frame_(commit|next|reserve))$' || true)
          if [ -n "$unexpected_env_imports" ]; then
            echo "ERROR: Redis contains unresolved non-ABI env imports: $artifact" >&2
            echo "$unexpected_env_imports" >&2
            exit 1
          fi
        done
      SH
    end

    kandelo_install_bin(buildpath/"src", "redis-server.optimized", "redis-server")
    kandelo_install_bin(buildpath/"src", "redis-cli.optimized", "redis-cli")
    (share/"licenses/redis").install "COPYING"
  end

  test do
    assert_path_exists bin/"redis-server"
    assert_path_exists bin/"redis-cli"
    assert_path_exists share/"licenses/redis/COPYING"
    assert_match(/Redis server v=7\.2\.5 .*malloc=libc bits=32 /,
      kandelo_run_wasm(bin/"redis-server", ["--version"]))
    assert_equal "redis-cli 7.2.5\n", kandelo_run_wasm(bin/"redis-cli", ["--version"])

    probe_source = testpath/"redis-ready-probe.c"
    probe = testpath/"redis-ready-probe.wasm"
    probe_source.write <<~C
      #include <arpa/inet.h>
      #include <errno.h>
      #include <netinet/in.h>
      #include <stdio.h>
      #include <string.h>
      #include <sys/socket.h>
      #include <time.h>
      #include <unistd.h>

      int main(int argc, char **argv) {
        const char request[] = "*1\\r\\n$4\\r\\nPING\\r\\n";
        const char expected[] = "+PONG\\r\\n";
        struct sockaddr_in address;
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 20 * 1000 * 1000 };
        char response[sizeof(expected) - 1];
        int port;

        if (argc != 2 || sscanf(argv[1], "%d", &port) != 1 ||
            port < 1 || port > 65535) return 2;
        memset(&address, 0, sizeof(address));
        address.sin_family = AF_INET;
        address.sin_port = htons((unsigned short)port);
        address.sin_addr.s_addr = htonl(0x7f000001UL);

        for (int attempt = 0; attempt < 500; attempt++) {
          int fd = socket(AF_INET, SOCK_STREAM, 0);
          if (fd < 0) return 3;
          if (connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0) {
            size_t sent = 0;
            size_t received = 0;
            while (sent < sizeof(request) - 1) {
              ssize_t count = send(fd, request + sent, sizeof(request) - 1 - sent, 0);
              if (count <= 0) return 4;
              sent += (size_t)count;
            }
            while (received < sizeof(response)) {
              ssize_t count = recv(fd, response + received, sizeof(response) - received, 0);
              if (count <= 0) return 5;
              received += (size_t)count;
            }
            close(fd);
            if (memcmp(response, expected, sizeof(response)) != 0) return 6;
            puts("redis-probe-ok");
            return 0;
          }
          close(fd);
          nanosleep(&pause, NULL);
        }
        fprintf(stderr, "Redis did not become ready: %s\\n", strerror(errno));
        return 7;
      }
    C
    kandelo_wasm_build { system kandelo_cc, "-O2", probe_source, "-o", probe }

    guest_programs = {
      "/usr/local/bin/redis-server" => bin/"redis-server",
      "/usr/local/bin/redis-cli"    => bin/"redis-cli",
      "/usr/local/bin/redis-probe"  => probe,
    }
    script = <<~'SH'
      set -eu
      port=26379
      server=/usr/local/bin/redis-server
      client=/usr/local/bin/redis-cli

      "$server" \
        --bind 127.0.0.1 \
        --port "$port" \
        --protected-mode no \
        --save "" \
        --appendonly no \
        --daemonize no \
        --logfile /tmp/redis-formula.log \
        --maxmemory 64mb \
        --maxmemory-policy noeviction \
        --tcp-backlog 128 \
        --dir /tmp &
      server_pid=$!
      cleanup() {
        if kill -0 "$server_pid" 2>/dev/null; then
          "$client" -h 127.0.0.1 -p "$port" SHUTDOWN NOSAVE >/dev/null 2>&1 || :
          kill "$server_pid" 2>/dev/null || :
        fi
        wait "$server_pid" 2>/dev/null || :
      }
      trap cleanup EXIT HUP INT TERM

      /usr/local/bin/redis-probe "$port"
      "$client" -h 127.0.0.1 -p "$port" --raw <<'REDIS'
      SET kandelo homebrew
      GET kandelo
      INCR formula-counter
      INCR formula-counter
      EVAL "return redis.call('GET', KEYS[1])" 1 kandelo
      INFO server
      SHUTDOWN NOSAVE
      REDIS
      wait "$server_pid"
      trap - EXIT HUP INT TERM
      printf 'redis-%s-service-ok\n' "$KANDELO_RUNTIME"
    SH
    assert_service = lambda do |output, runtime|
      assert_includes output, "redis-probe-ok\n"
      assert_includes output, "OK\nhomebrew\n1\n2\nhomebrew\n"
      assert_includes output, "redis_version:7.2.5"
      assert_includes output, "multiplexing_api:select"
      assert_includes output, "redis-#{runtime}-service-ok\n"
    end

    dash = formula_opt_bin("kandelo-dev/tap-core/dash")/"dash"
    node_output = kandelo_run_wasm(
      dash,
      ["-c", script],
      argv0:         "/bin/sh",
      env:           { "KANDELO_RUNTIME" => "node", "TIMEOUT" => "180000" },
      exec_programs: guest_programs.merge("/bin/sh" => dash),
      network:       true,
      merge_stderr:  true,
    )
    assert_service.call(node_output, "node")

    browser_output = kandelo_run_browser_wasm(
      dash,
      ["-c", script],
      argv0:              "sh",
      guest_program_path: "/bin/sh",
      env:                { "KANDELO_RUNTIME" => "browser" },
      exec_programs:      guest_programs,
      timeout_ms:         180_000,
      merge_stderr:       true,
    )
    assert_service.call(browser_output, "browser")

    [bin/"redis-server", bin/"redis-cli"].each do |binary|
      bytes = binary.binread
      refute_includes bytes, prefix.to_s
      refute_includes bytes, "/nix/store/"
      refute_match %r{/private/tmp/[^/]+/}, bytes
      refute_match %r{/Users/[^/]+/}, bytes
    end
  end
end

__END__
diff --git a/src/tls.c b/src/tls.c
index 0fce662..d6466c5 100644
--- a/src/tls.c
+++ b/src/tls.c
@@ -29,0 +30 @@
+#if (USE_OPENSSL == 1 /* BUILD_YES */ ) || ((USE_OPENSSL == 2 /* BUILD_MODULE */) && (BUILD_TLS_MODULE == 2))
@@ -30,0 +32,2 @@
+#define REDIS_TLS_COMPILED 1
+#endif
@@ -32,0 +36,2 @@
+
+#ifdef REDIS_TLS_COMPILED
@@ -36,2 +40,0 @@
-#if (USE_OPENSSL == 1 /* BUILD_YES */ ) || ((USE_OPENSSL == 2 /* BUILD_MODULE */) && (BUILD_TLS_MODULE == 2))
-
