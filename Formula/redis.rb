require_relative "../Kandelo/formula_support/kandelo_formula_support"

class Redis < Formula
  include KandeloFormulaSupport

  desc "In-memory data structure server and client for Kandelo"
  homepage "https://redis.io/"
  url "https://github.com/redis/redis/archive/refs/tags/7.2.5.tar.gz"
  sha256 "98a8502a2e902d2a9785ef46a69a5f8d5e24cbf9ea3ae4d845afcfc6778aa783"
  license "BSD-3-Clause"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/redis-server"
  skip_clean "bin/redis-cli"

  # Redis defines every module API function-pointer global before selecting its
  # non-TLS branch; LLVM's Wasm AsmPrinter crashes on that unused global set.
  # Keep the module-only declarations inside the existing TLS condition.
  patch :DATA

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      ENV["CFLAGS"] = "-O2 -gline-tables-only -fdebug-compilation-dir=."

      # Redis' Makefiles inspect the build host with uname even while using a
      # cross compiler. Select the Linux-compatible target link set so pthread,
      # realtime, and dl APIs come from Kandelo rather than the macOS host.
      inreplace "src/Makefile" do |s|
        s.sub!(/^uname_S :=.*$/, "uname_S := Linux")
        s.sub!(/^uname_M :=.*$/, "uname_M := wasm32")
      end
      inreplace "deps/Makefile" do |s|
        s.sub!(/^uname_S:=.*$/, "uname_S:= Linux")
        s.sub!(/^AR=ar$/, "AR?=ar")
        s.sub!(/^ARFLAGS=rc$/, "ARFLAGS?=rc\nRANLIB?=ranlib")
        s.sub!("cd hiredis && $(MAKE) static $(HIREDIS_MAKE_FLAGS)",
          'cd hiredis && $(MAKE) static $(HIREDIS_MAKE_FLAGS) AR="$(AR)"')
        s.sub!(/^\tcd hdr_histogram && \$\(MAKE\)$/, "\tcd hdr_histogram && $(MAKE) AR=\"$(AR)\" ARFLAGS=rcs")
        s.sub!(/^\tcd fpconv && \$\(MAKE\)$/, "\tcd fpconv && $(MAKE) AR=\"$(AR)\" ARFLAGS=rcs")
        s.sub!('AR="$(AR) $(ARFLAGS)"', 'AR="$(AR) $(ARFLAGS)" RANLIB="$(RANLIB)"')
      end

      # Redis prefixes the top-level dependency recipe with `-`, so Make would
      # ignore a failed archive build and the SDK linker would preserve those
      # symbols as env imports. Build the dependencies as a checked step.
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

      optimized_server = buildpath/"src/redis-server.optimized"
      optimized_cli = buildpath/"src/redis-cli.optimized"
      instrumented_server = buildpath/"src/redis-server.instrumented"
      system "wasm-opt", "-O2", buildpath/"src/redis-server", "-o", optimized_server
      system "wasm-opt", "-O2", buildpath/"src/redis-cli", "-o", optimized_cli
      system "#{root}/scripts/run-wasm-fork-instrument.sh",
        optimized_server, "-o", instrumented_server

      artifact_guards = "#{root}/scripts/wasm-artifact-guards.sh"
      system "bash", "-c", <<~SH
        set -euo pipefail
        . #{artifact_guards.shellescape}
        expected_abi=$(wasm_current_abi_version #{root.to_s.shellescape})
        for artifact in #{instrumented_server.to_s.shellescape} #{optimized_cli.to_s.shellescape}; do
          artifact_abi=$(wasm_extract_abi_version "$artifact")
          if [ -z "$expected_abi" ] || [ "$artifact_abi" != "$expected_abi" ]; then
            echo "ERROR: Redis ABI $artifact_abi does not match Kandelo ABI $expected_abi: $artifact" >&2
            exit 1
          fi
          wasm_require_no_legacy_asyncify "$artifact"
          wasm_require_fork_instrumentation_if_needed "$artifact"
          unexpected_env_imports=$(wasm-objdump -x "$artifact" |
            awk '/<- env[.]/ { sub(/^.*<- env[.]/, ""); print $1 }' |
            grep -Ev '^(__channel_base|memory|__wasm_dlclose|__wasm_dlerror|__wasm_dlopen|__wasm_dlsym)$' || true)
          if [ -n "$unexpected_env_imports" ]; then
            echo "ERROR: Redis contains unresolved non-ABI env imports: $artifact" >&2
            echo "$unexpected_env_imports" >&2
            exit 1
          fi
        done
        if ! wasm_has_complete_fork_instrumentation #{instrumented_server.to_s.shellescape}; then
          echo "ERROR: redis-server has incomplete fork instrumentation" >&2
          exit 1
        fi
      SH
    end

    kandelo_install_bin(buildpath/"src", "redis-server.instrumented", "redis-server")
    kandelo_install_bin(buildpath/"src", "redis-cli.optimized", "redis-cli")
  end

  test do
    server_version = kandelo_run_wasm(bin/"redis-server", ["--version"])
    assert_match(/Redis server v=7\.2\.5 .*malloc=libc bits=32 /, server_version)
    assert_equal "redis-cli 7.2.5\n", kandelo_run_wasm(bin/"redis-cli", ["--version"])

    commands = <<~COMMANDS
      SET kandelo homebrew
      GET kandelo
      INCR formula-counter
      INCR formula-counter
      EVAL "return redis.call('GET', KEYS[1])" 1 kandelo
      INFO server
      SHUTDOWN NOSAVE
    COMMANDS
    service_output = kandelo_run_virtual_network_pairs(
      bin/"redis-server",
      [{
        name:                         "redis",
        transport:                    "tcp",
        serverArgs:                   [
          "redis-server",
          "--bind", "10.88.0.2",
          "--port", "26379",
          "--protected-mode", "no",
          "--save", "",
          "--appendonly", "no",
          "--daemonize", "no",
          "--loglevel", "warning",
          "--maxmemory", "64mb",
          "--maxmemory-policy", "noeviction",
          "--tcp-backlog", "128",
          "--dir", "/tmp"
        ],
        clientArgs:                   ["redis-cli", "-h", "10.88.0.2", "-p", "26379", "--raw"],
        serverStdin:                  "",
        clientStdin:                  commands,
        expectedServerStdoutIncludes: ["Redis is now ready to exit, bye bye..."],
        expectedClientStdoutIncludes: [
          "OK\nhomebrew\n1\n2\nhomebrew\n",
          "redis_version:7.2.5",
          "multiplexing_api:select",
          "atomicvar_api:c11-builtin",
        ],
        timeoutMs:                    20_000,
      }],
      client_bin_path: bin/"redis-cli",
    )
    assert_includes service_output, '"redis"'

    [bin/"redis-server", bin/"redis-cli"].each do |binary|
      bytes = File.binread(binary)
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
