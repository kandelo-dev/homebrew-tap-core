require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Dinit < Formula
  include KandeloFormulaSupport

  desc "Service manager and process supervisor for Kandelo"
  homepage "https://davmac.org/projects/dinit/"
  url "https://github.com/davmac314/dinit/archive/refs/tags/v0.19.4.tar.gz"
  sha256 "3c0f624eb958f8e884631be4ef687da1e475ebaa6241e7ee330b864e6cd9e30b"
  license "Apache-2.0"

  depends_on "binaryen" => :build
  depends_on "m4" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/libcxx"

  skip_clean "bin/dinit"
  skip_clean "bin/dinitctl"
  skip_clean "bin/dinitcheck"
  skip_clean "bin/dinit-monitor"

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/dinit".freeze
  GUEST_LIBCXX_PREFIX = "/home/linuxbrew/.linuxbrew/opt/libcxx".freeze

  # LLVM lowers Wasm setjmp/longjmp through exceptions. An exception escaping
  # these noexcept event loops is otherwise intercepted by C++ termination
  # cleanup before the generated setjmp dispatch can receive it.
  patch :DATA

  def install
    kandelo_require_arch!("wasm32")
    libcxx = formula_opt_prefix("automattic/kandelo-homebrew/libcxx")
    build_cxx = kandelo_host_cxx

    # Dinit uses this native generator to produce its target configuration
    # header. Build it in Kandelo's canonical native dev shell so target libcxx
    # dependency paths cannot enter the host compiler environment.
    system build_cxx,
      "-DNDEBUG", "-std=c++14", "-O2", "-pthread",
      "build/tools/mconfig-gen.cc", "-o", "build/tools/mconfig-gen"

    kandelo_wasm_build do |root|
      prefix_maps = [
        "-ffile-prefix-map=#{buildpath}=/usr/src/dinit",
        "-fdebug-prefix-map=#{buildpath}=/usr/src/dinit",
        "-fmacro-prefix-map=#{buildpath}=/usr/src/dinit",
        "-ffile-prefix-map=#{root}=/usr/src/kandelo",
        "-fdebug-prefix-map=#{root}=/usr/src/kandelo",
        "-fmacro-prefix-map=#{root}=/usr/src/kandelo",
        "-ffile-prefix-map=#{libcxx}=#{GUEST_LIBCXX_PREFIX}",
        "-fdebug-prefix-map=#{libcxx}=#{GUEST_LIBCXX_PREFIX}",
        "-fmacro-prefix-map=#{libcxx}=#{GUEST_LIBCXX_PREFIX}",
      ]
      target_cxxflags = [
        "-std=c++14",
        "-O2",
        "-fwasm-exceptions",
        "-nostdinc++",
        "-isystem #{libcxx}/include/c++/v1",
        *prefix_maps,
      ].join(" ")
      target_ldflags = "-L#{libcxx}/lib -lc++ -lc++abi"

      ENV["CXXFLAGS"] = target_cxxflags
      ENV["TEST_CXXFLAGS"] = target_cxxflags
      ENV["CPPFLAGS"] = "-D_POSIX_C_SOURCE=200809L"
      ENV["LDFLAGS"] = target_ldflags
      ENV["TEST_LDFLAGS"] = target_ldflags
      ENV["CXX_FOR_BUILD"] = build_cxx.to_s
      ENV["CXXFLAGS_FOR_BUILD"] = "-std=c++14 -O2"
      ENV["CPPFLAGS_FOR_BUILD"] = "-DNDEBUG"
      ENV["LDFLAGS_FOR_BUILD"] = "-pthread"

      system "./configure",
        "--platform=Linux",
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--exec-prefix=#{GUEST_OPT_PREFIX}",
        "--sbindir=#{GUEST_OPT_PREFIX}/bin",
        "--mandir=#{GUEST_OPT_PREFIX}/share/man",
        "--syscontrolsocket=/run/dinitctl",
        "--disable-strip",
        "--disable-shutdown",
        "--disable-cgroups",
        "--disable-utmpx",
        "--default-auto-restart=on-failure"
      system "make", "-j#{ENV.make_jobs}"

      # LLVM already optimized each translation unit. A post-link Binaryen pass
      # increases this program's fork-replay surface, so instrument the linked
      # fork callers directly and leave instrumentation as the final transform.
      fork_programs = %w[dinit dinit-monitor]
      fork_programs.each { |program| kandelo_fork_instrument(buildpath/"src"/program) }

      programs = %w[dinit dinitctl dinitcheck dinit-monitor]
      programs.each do |program|
        fork_policy = fork_programs.include?(program) ? :required : :forbidden
        kandelo_validate_wasm_artifact(buildpath/"src"/program, fork: fork_policy)
      end

      program_paths = programs.map { |program| (buildpath/"src"/program).to_s.shellescape }.join(" ")
      system "bash", "-c", <<~SH
        set -euo pipefail
        for program in #{program_paths}; do
          unexpected_env_imports=$(wasm-objdump -x "$program" |
            awk '/<- env[.]/ { sub(/^.*<- env[.]/, ""); print $1 }' |
            grep -Ev '^(__channel_base|memory|setjmp|longjmp)$' || true)
          if [ -n "$unexpected_env_imports" ]; then
            echo "ERROR: $program contains unresolved non-ABI env imports" >&2
            echo "$unexpected_env_imports" >&2
            exit 1
          fi
        done
      SH
    end

    %w[dinit dinitctl dinitcheck dinit-monitor].each do |program|
      kandelo_install_bin(buildpath/"src", program, program)
    end
    man5.install "doc/manpages/dinit-service.5"
    man8.install "doc/manpages/dinit.8",
      "doc/manpages/dinitctl.8",
      "doc/manpages/dinitcheck.8",
      "doc/manpages/dinit-monitor.8"
  end

  test do
    assert_match(/Dinit version 0\.19\.4/, kandelo_run_wasm(bin/"dinit", ["--version"]))
    assert_match(/Dinit version 0\.19\.4/, kandelo_run_wasm(bin/"dinitctl", ["--version"]))
    assert_match(/Dinit version 0\.19\.4/, kandelo_run_wasm(bin/"dinit-monitor", ["--version"]))
    assert_includes kandelo_run_wasm(bin/"dinitcheck", ["--help"]), "dinitcheck"

    probe_service = testpath/"probe.service"
    probe_service.write <<~EOS
      type = internal
    EOS
    monitor_service = testpath/"monitor.service"
    monitor_service.write <<~EOS
      type = process
      command = #{GUEST_OPT_PREFIX}/bin/dinit-monitor --socket-path /tmp/dinitctl --initial --exit --command "#{GUEST_OPT_PREFIX}/bin/dinitcheck --help" probe
      restart = false
    EOS

    # dinit-monitor's --initial --exit path returns only after its notification
    # command has forked, exec'd, exited, and been waited for.
    output = kandelo_run_wasm(
      bin/"dinit",
      [
        "--container",
        "--services-dir", "/etc/dinit.d",
        "--socket-path", "/tmp/dinitctl",
        "monitor"
      ],
      exec_programs: {
        "#{GUEST_OPT_PREFIX}/bin/dinit-monitor" => bin/"dinit-monitor",
        "#{GUEST_OPT_PREFIX}/bin/dinitcheck"    => bin/"dinitcheck",
      },
      guest_files:   {
        "/etc/dinit.d/monitor" => monitor_service,
        "/etc/dinit.d/probe"   => probe_service,
      },
    )
    assert_match(/\[\s*OK\s*\] monitor/, output)
    assert_match(/\[STOPPD\] monitor/, output)

    %w[dinit dinitctl dinitcheck dinit-monitor].each do |program|
      binary = File.binread(bin/program)
      refute_includes binary, prefix.to_s
      refute_includes binary, "/nix/store/"
      refute_match %r{/private/tmp/[^/]+/}, binary
      refute_match %r{/Users/[^/]+/}, binary
    end
    assert_includes File.binread(bin/"dinit"), "#{GUEST_OPT_PREFIX}/bin"
  end
end

__END__
diff --git a/dasynq/include/dasynq/pselect.h b/dasynq/include/dasynq/pselect.h
index d370be6..578ab39 100644
--- a/dasynq/include/dasynq/pselect.h
+++ b/dasynq/include/dasynq/pselect.h
@@ -225,7 +225,7 @@ template <class Base> class pselect_events : public signal_events<Base, false>
     //
     //  do_wait - if false, returns immediately if no events are
     //            pending.
-    void pull_events(bool do_wait) noexcept
+    void pull_events(bool do_wait)
     {
         struct timespec ts;
         struct timespec *wait_ts = nullptr;
diff --git a/dasynq/include/dasynq/select.h b/dasynq/include/dasynq/select.h
index 5aae627..b6edc3e 100644
--- a/dasynq/include/dasynq/select.h
+++ b/dasynq/include/dasynq/select.h
@@ -283,7 +283,7 @@ template <class Base> class select_events : public signal_events<Base, true>
     //
     //  do_wait - if false, returns immediately if no events are
     //            pending.
-    void pull_events(bool do_wait) noexcept
+    void pull_events(bool do_wait)
     {
         struct timeval ts;
         struct timeval *wait_ts = nullptr;
