require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Dinit < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/dinit".freeze
  GUEST_LIBCXX_PREFIX = "/home/linuxbrew/.linuxbrew/opt/libcxx".freeze
  PROGRAMS = %w[dinit dinitctl dinitcheck].freeze

  desc "Service manager and process supervisor for Kandelo"
  homepage "https://davmac.org/projects/dinit/"
  url "https://github.com/davmac314/dinit/archive/refs/tags/v0.19.4.tar.gz"
  sha256 "3c0f624eb958f8e884631be4ef687da1e475ebaa6241e7ee330b864e6cd9e30b"
  license "Apache-2.0"

  depends_on KandeloFormulaSupport::BinaryenRequirement => [:build, :test]
  depends_on "m4" => :build
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  depends_on "kandelo-dev/tap-core/libcxx"

  skip_clean "sbin/dinit", "sbin/dinitctl", "sbin/dinitcheck"

  # LLVM lowers Wasm setjmp/longjmp through exceptions. Dasynq's pselect
  # backend installs the sigsetjmp landing pad in pull_events(), so marking
  # that same function noexcept lets SIGCHLD reach std::terminate before the
  # landing pad can receive the longjmp. Keep Dinit's real C++ exception
  # handling and remove only that conflicting promise.
  patch :DATA

  def install
    kandelo_require_arch!("wasm32")
    libcxx = formula_opt_prefix("kandelo-dev/tap-core/libcxx")
    build_cxx = kandelo_host_cxx

    # mconfig-gen is a native generator which emits target configuration.
    # Build it through Kandelo's declared dev shell before target dependency
    # isolation, rather than allowing Homebrew's target libcxx paths or an
    # ambient host compiler to select its C++ environment.
    system build_cxx,
      "-DNDEBUG", "-std=c++14", "-O2",
      "build/tools/mconfig-gen.cc", "-o", "build/tools/mconfig-gen"

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/dinit-#{version}"
      prefix_maps = {
        buildpath.to_s => stable_source,
        root.to_s      => "/usr/src/kandelo",
        libcxx.to_s    => GUEST_LIBCXX_PREFIX,
        "/nix/store"   => "/usr/src/toolchain",
      }.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end
      target_cxxflags = [
        "-std=c++14",
        "-O2",
        "-gline-tables-only",
        "-fdebug-compilation-dir=#{stable_source}",
        "-fwasm-exceptions",
        "-nostdinc++",
        "-isystem #{libcxx}/include/c++/v1",
        *prefix_maps,
      ].join(" ")
      target_ldflags = "-fwasm-exceptions -L#{libcxx}/lib -lc++ -lc++abi"

      ENV["CXXFLAGS"] = target_cxxflags
      ENV["TEST_CXXFLAGS"] = target_cxxflags
      ENV["CPPFLAGS"] = "-D_POSIX_C_SOURCE=200809L"
      ENV["LDFLAGS"] = target_ldflags
      ENV["TEST_LDFLAGS"] = target_ldflags
      ENV["CXX_FOR_BUILD"] = build_cxx.to_s
      ENV["CXXFLAGS_FOR_BUILD"] = "-DNDEBUG -std=c++14 -O2"
      ENV["CPPFLAGS_FOR_BUILD"] = ""
      ENV["LDFLAGS_FOR_BUILD"] = ""

      # Dinit's configure script supports explicit cross-compilation. Linux
      # selects its system-manager defaults; the switches below then remove
      # Linux-only cgroups, shutdown helpers, and utmp integration. The target
      # compiler does not define __linux__, so dasynq selects its portable
      # POSIX pselect backend.
      system "./configure",
        "--platform=Linux",
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--exec-prefix=#{GUEST_OPT_PREFIX}",
        "--sbindir=#{GUEST_OPT_PREFIX}/sbin",
        "--mandir=#{GUEST_OPT_PREFIX}/share/man",
        "--syscontrolsocket=/run/dinitctl",
        "--disable-strip",
        "--disable-shutdown",
        "--disable-cgroups",
        "--disable-utmpx",
        "--default-auto-restart=on-failure"

      system "make", "-C", "build", "includes/mconfig.h"
      system "make", "-j#{ENV.make_jobs}", "-C", "src", *PROGRAMS
      system "make", "-j#{ENV.make_jobs}", "-C", "doc/manpages",
        "dinit.8", "dinitctl.8", "dinitcheck.8", "dinit-service.5"

      # Only the supervisor launches services with fork()+exec(). The client
      # and static checker stay fork-free and must not carry continuation
      # state that they can never use.
      kandelo_fork_instrument(buildpath/"src/dinit")
      PROGRAMS.each do |program|
        artifact = buildpath/"src"/program
        fork_policy = (program == "dinit") ? :required : :forbidden
        kandelo_validate_wasm_artifact(
          artifact,
          fork:            fork_policy,
          forbidden_paths: [libcxx.to_s],
        )

        imports = Utils.safe_popen_read("wasm-objdump", "-x", artifact)
                       .scan(/<- env[.]([^\s]+)/).flatten
        unexpected = imports - %w[__channel_base memory setjmp longjmp]
        odie "#{program} contains unresolved non-ABI env imports: #{unexpected.join(", ")}" if unexpected.any?
      end
    end

    PROGRAMS.each do |program|
      artifact = buildpath/"src"/program
      chmod 0755, artifact
      sbin.install artifact
      chmod 0755, sbin/program
    end
    man5.install "doc/manpages/dinit-service.5"
    man8.install "doc/manpages/dinit.8",
      "doc/manpages/dinitctl.8",
      "doc/manpages/dinitcheck.8"
  end

  test do
    assert_supervisor_run = lambda do |output|
      assert_includes output, "service-helper-ok"
      assert_includes output, "service-stop-ok"
      %w[worker list stop boot].each { |service| assert_includes output, service }
      assert_match(/\[\s*OK\s*\] worker/, output)
      assert_match(/^\[[\[{][^\n]*\] boot$/, output)
      refute_includes output, "libc++abi: terminating"
    end
    assert_malformed_service = lambda do |output|
      assert_includes output, "malformed"
      assert_includes output, "restart must be one of"
      refute_includes output, "libc++abi: terminating"
    end

    PROGRAMS.each do |program|
      artifact = sbin/program
      assert_path_exists artifact
      assert_equal 0755, artifact.stat.mode & 0777
      kandelo_validate_wasm_artifact(
        artifact,
        fork: (program == "dinit") ? :required : :forbidden,
      )

      contents = artifact.binread
      [
        prefix.to_s,
        "/private/tmp/",
        "/Users/",
        "/home/runner/work/",
        "/home/runner/_work/",
        "/nix/store/",
      ].each { |path| refute_includes contents, path }
    end

    assert_path_exists man5/"dinit-service.5"
    %w[dinit.8 dinitctl.8 dinitcheck.8].each do |manual|
      page = man8/manual
      assert_path_exists page
      assert_includes page.read, "Dinit 0.19.4"
    end

    assert_match(/Dinit version 0[.]19[.]4/, kandelo_run_wasm(sbin/"dinit", ["--version"]))
    assert_match(/Dinit version 0[.]19[.]4/, kandelo_run_wasm(sbin/"dinitctl", ["--version"]))
    assert_includes kandelo_run_wasm(sbin/"dinitcheck", ["--help"]), "dinitcheck"

    helper_source = testpath/"service-helper.c"
    helper = testpath/"service-helper.wasm"
    helper_source.write <<~C
      #include <signal.h>
      #include <stdio.h>
      #include <string.h>
      #include <unistd.h>

      int main(int argc, char **argv) {
        if (argc == 2 && strcmp(argv[1], "stop") == 0) {
          puts("service-stop-ok");
          return kill(getppid(), SIGTERM) == 0 ? 0 : 1;
        }
        puts("service-helper-ok");
        return 0;
      }
    C
    kandelo_wasm_build do
      system ENV.fetch("CC"), "-O2", helper_source, "-o", helper
    end
    kandelo_validate_wasm_artifact(helper, fork: :forbidden)

    service_dir = testpath/"services"
    service_dir.mkpath
    (service_dir/"worker").write <<~EOS
      type = scripted
      command = /bin/service-helper run
      options = starts-on-console
      restart = false
    EOS
    (service_dir/"list").write <<~EOS
      type = scripted
      command = /sbin/dinitctl -p /tmp/dinitctl list
      depends-on = worker
      options = starts-on-console
      restart = false
    EOS
    # A dinitctl shutdown process managed by Dinit would wait for the same
    # supervisor that must reap it, creating a test-only circular wait. After
    # dinitctl proves the live control socket and service state, this final
    # child uses Dinit's documented SIGTERM shutdown path instead.
    (service_dir/"stop").write <<~EOS
      type = scripted
      command = /bin/service-helper stop
      depends-on = list
      options = starts-on-console
      restart = false
    EOS
    (service_dir/"boot").write <<~EOS
      type = internal
      depends-on = stop
    EOS
    service_files = service_dir.children.to_h do |path|
      ["/etc/dinit.d/#{path.basename}", path]
    end
    service_programs = {
      "/bin/service-helper" => helper,
      "/sbin/dinitctl"      => sbin/"dinitctl",
    }
    supervisor_args = [
      "--container",
      "--services-dir", "/etc/dinit.d",
      "--socket-path", "/tmp/dinitctl",
      "boot"
    ]

    node_output = kandelo_run_wasm(
      sbin/"dinit",
      supervisor_args,
      argv0:                     "/sbin/dinit",
      exec_programs:             service_programs,
      expected_fork_descendants: 3,
      guest_files:               service_files,
      merge_stderr:              true,
    )
    assert_supervisor_run.call(node_output)

    browser_output = kandelo_run_browser_wasm(
      sbin/"dinit",
      supervisor_args,
      allow_stderr:       true,
      argv0:              "dinit",
      exec_programs:      service_programs,
      guest_files:        service_files,
      guest_program_path: "/sbin/dinit",
      merge_stderr:       true,
      timeout_ms:         120_000,
    )
    assert_supervisor_run.call(browser_output)

    malformed = testpath/"malformed"
    malformed.write <<~EOS
      type = scripted
      command = /bin/service-helper
      restart = banana
    EOS
    malformed_files = { "/etc/dinit.bad/malformed" => malformed }
    malformed_args = ["--services-dir", "/etc/dinit.bad", "malformed"]
    malformed_supervisor_args = [
      "--container",
      "--services-dir", "/etc/dinit.bad",
      "--socket-path", "/tmp/dinit-bad",
      "malformed"
    ]
    malformed_supervisor_node = kandelo_run_wasm(
      sbin/"dinit",
      malformed_supervisor_args,
      argv0:        "/sbin/dinit",
      guest_files:  malformed_files,
      merge_stderr: true,
    )
    assert_malformed_service.call(malformed_supervisor_node)

    malformed_supervisor_browser = kandelo_run_browser_wasm(
      sbin/"dinit",
      malformed_supervisor_args,
      allow_stderr:       true,
      argv0:              "dinit",
      guest_files:        malformed_files,
      guest_program_path: "/sbin/dinit",
      merge_stderr:       true,
    )
    assert_malformed_service.call(malformed_supervisor_browser)

    malformed_node = kandelo_run_wasm(
      sbin/"dinitcheck",
      malformed_args,
      argv0:           "/sbin/dinitcheck",
      guest_files:     malformed_files,
      merge_stderr:    true,
      expected_status: 1,
    )
    assert_malformed_service.call(malformed_node)

    malformed_browser = kandelo_run_browser_wasm(
      sbin/"dinitcheck",
      malformed_args,
      allow_stderr:       true,
      argv0:              "dinitcheck",
      guest_files:        malformed_files,
      guest_program_path: "/sbin/dinitcheck",
      merge_stderr:       true,
      expected_status:    1,
    )
    assert_malformed_service.call(malformed_browser)
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: "/home/linuxbrew/.linuxbrew/Cellar", wasm32_kandelo: "211e70a89b0a3e61d41767056d5b05e4637416802a7d1c65eaeb164b6c05cfa5"
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
