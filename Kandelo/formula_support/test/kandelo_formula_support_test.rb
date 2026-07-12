# typed: strict
# frozen_string_literal: true

require "minitest/autorun"
# Standalone Ruby does not preload Homebrew's Pathname helper.
require "pathname" # rubocop:disable Lint/RedundantRequireStatement
require "tmpdir"
require_relative "../kandelo_formula_support"

# Regression coverage for Formula runtime execution evidence.
class KandeloFormulaSupportTest < Minitest::Test
  DependencyFormula = Struct.new(:full_name, :opt_bin, :opt_sbin, :opt_libexec, keyword_init: true)
  InstalledFormula = Struct.new(:rack, :pkg_version, keyword_init: true)

  # Minimal Formula double for command-construction tests.
  class Harness
    include KandeloFormulaSupport

    attr_accessor :build_path, :dependency_formulae, :nix_path, :prefix_path, :root_path, :runtime_formulae,
                  :shell_result, :test_path
    attr_reader :command, :expected_status, :recorded_launcher, :system_args, :system_calls

    def kandelo_require_root!
      root_path || "/tmp/kandelo root"
    end

    def testpath
      test_path || Pathname("/tmp/formula test")
    end

    def buildpath
      build_path || testpath
    end

    def prefix
      prefix_path || Pathname("/tmp/formula prefix")
    end

    def kandelo_nix_executable
      nix_path || super
    end

    def kandelo_formula(formula_name)
      return dependency_formulae.fetch(formula_name) if dependency_formulae&.key?(formula_name)

      super
    end

    def runtime_formula_dependencies(read_from_tab:, undeclared:)
      raise "runtime dependency lookup must use declarations" if read_from_tab || undeclared

      runtime_formulae || []
    end

    def shell_output(command, expected_status = 0)
      @command = command
      @expected_status = expected_status
      shell_result || "runtime-ok\n"
    end

    def odie(message)
      raise message
    end

    # The Formula double must intercept Kernel#system under its real name.
    # rubocop:disable Naming/PredicateMethod
    def system(*args)
      @system_calls ||= []
      @system_calls << args
      @system_args = args
      if (output_index = args.index("-o"))
        File.binwrite(args.fetch(output_index + 1), "instrumented")
      end
      true
    end
    # rubocop:enable Naming/PredicateMethod

    def kandelo_record_node_execution!(_wasm_path, _argv, launcher: "kandelo_run_wasm")
      @recorded_launcher = launcher
      nil
    end
  end

  # Executes validator commands with a controlled PATH for fail-closed tests.
  class ExecutingHarness < Harness
    attr_accessor :system_path

    def system(*args)
      return true if Kernel.system({ "PATH" => system_path }, *args)

      raise "command failed: #{args.join(" ")}"
    end
  end

  def test_node_execution_receipt_is_optional
    previous = ENV.delete("HOMEBREW_KANDELO_NODE_RECEIPT_PATH")

    assert_nil Harness.new.kandelo_record_node_execution!("program.wasm", [])
  ensure
    ENV["HOMEBREW_KANDELO_NODE_RECEIPT_PATH"] = previous if previous
  end

  def test_target_dependency_paths_use_the_exact_installed_keg
    Dir.mktmpdir("kandelo-dependency-prefix") do |dir|
      harness = Harness.new
      target = "automattic/kandelo-homebrew/openssl"
      rack = Pathname(dir)/"Cellar/openssl"
      keg = rack/"3.3.2_2"
      keg.mkpath
      harness.dependency_formulae = {
        target => InstalledFormula.new(rack:, pkg_version: "3.3.2_2"),
      }

      assert_equal keg, harness.formula_opt_prefix(target)
      assert_equal keg/"bin", harness.formula_opt_bin(target)
      assert_equal keg/"lib", harness.formula_opt_lib(target)
      assert_equal keg/"libexec", harness.formula_opt_libexec(target)
      assert_equal keg/"include", harness.formula_opt_include(target)
      refute_equal Pathname(dir)/"opt/openssl", harness.formula_opt_prefix(target)
    end
  end

  def test_target_dependency_paths_reject_a_missing_current_keg
    harness = Harness.new
    target = "automattic/kandelo-homebrew/openssl"
    harness.dependency_formulae = {
      target => InstalledFormula.new(rack: Pathname("/missing/Cellar/openssl"), pkg_version: "3.3.2_2"),
    }

    error = assert_raises(RuntimeError) { harness.formula_opt_prefix(target) }
    assert_includes error.message, "is not installed at /missing/Cellar/openssl/3.3.2_2"
  end

  def test_fork_instrumentation_replaces_the_linked_program
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = Harness.new
      wasm = Pathname(dir)/"program.wasm"
      wasm.binwrite("linked")
      wasm.chmod 0751

      assert_equal wasm, harness.kandelo_fork_instrument(wasm)
      assert_equal "instrumented", wasm.binread
      assert_equal 0751, wasm.stat.mode & 0777
      assert_equal "/tmp/kandelo root/scripts/run-wasm-fork-instrument.sh", harness.system_args.first
      assert_equal [wasm.to_s, "-o", "#{wasm}.fork-instrumented"], harness.system_args.drop(1)
      refute File.exist?("#{wasm}.fork-instrumented")
    end
  end

  def test_artifact_validation_requires_abi_asyncify_and_fork_guards
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = artifact_validation_harness(dir)
      wasm = harness.buildpath/"program.wasm"
      wasm.binwrite("\0asm")

      assert_equal wasm, harness.kandelo_validate_wasm_artifact(wasm, fork: :required)
      command = harness.system_args.fetch(2)
      assert_includes command, "wasm_current_abi_version"
      assert_includes command, "wasm_extract_abi_version"
      assert_includes command, "wasm_require_no_legacy_asyncify"
      assert_includes command, "wasm_imports_kernel_fork"
      assert_includes command, "wasm_has_complete_fork_instrumentation"
      assert_includes command, "for tool in wasm-objdump wasm-dis wasm-opt"
    end
  end

  def test_artifact_validation_enforces_fork_free_policy
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = artifact_validation_harness(dir)
      wasm = harness.buildpath/"program.wasm"
      wasm.binwrite("\0asm")

      harness.kandelo_validate_wasm_artifact(wasm, fork: :forbidden)
      command = harness.system_args.fetch(2)
      assert_includes command, "fork-free artifact imports kernel.kernel_fork"
      assert_includes command, "wasm_require_no_fork_instrumentation"
    end
  end

  def test_artifact_validation_rejects_staging_and_host_paths
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = artifact_validation_harness(dir)
      wasm = harness.buildpath/"program.wasm"
      wasm.binwrite("debug path: #{harness.prefix}")

      error = assert_raises(RuntimeError) do
        harness.kandelo_validate_wasm_artifact(wasm)
      end
      assert_includes error.message, harness.prefix.to_s

      wasm.binwrite("debug path: /home/runner/work/kandelo/build")
      error = assert_raises(RuntimeError) do
        harness.kandelo_validate_wasm_artifact(wasm)
      end
      assert_includes error.message, "host workspace path"
    end
  end

  def test_artifact_validation_allows_stable_guest_opt_paths
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = artifact_validation_harness(dir)
      wasm = harness.buildpath/"program.wasm"
      wasm.binwrite("/home/linuxbrew/.linuxbrew/opt/formula")

      assert_equal wasm, harness.kandelo_validate_wasm_artifact(wasm)
    end
  end

  def test_artifact_validation_requires_wasm_objdump
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = artifact_validation_harness(dir, ExecutingHarness)
      harness.system_path = "/bin:/usr/bin"
      wasm = harness.buildpath/"program.wasm"
      wasm.binwrite("\0asm")

      assert_raises(RuntimeError) do
        harness.kandelo_validate_wasm_artifact(wasm)
      end
    end
  end

  def test_artifact_validation_requires_wasm_dis
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = artifact_validation_harness(dir, ExecutingHarness)
      tool_dir = Pathname(dir)/"tools"
      tool_dir.mkpath
      wasm_objdump = tool_dir/"wasm-objdump"
      wasm_objdump.binwrite("#!/bin/sh\nexit 0\n")
      wasm_objdump.chmod 0755
      harness.system_path = "#{tool_dir}:/bin:/usr/bin"
      wasm = harness.buildpath/"program.wasm"
      wasm.binwrite("\0asm")

      assert_raises(RuntimeError) do
        harness.kandelo_validate_wasm_artifact(wasm)
      end
    end
  end

  def test_artifact_validation_requires_wasm_opt
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = artifact_validation_harness(dir, ExecutingHarness)
      tool_dir = Pathname(dir)/"tools"
      tool_dir.mkpath
      ["wasm-objdump", "wasm-dis"].each do |name|
        tool = tool_dir/name
        tool.binwrite("#!/bin/sh\nexit 0\n")
        tool.chmod 0755
      end
      harness.system_path = "#{tool_dir}:/bin:/usr/bin"
      wasm = harness.buildpath/"program.wasm"
      wasm.binwrite("\0asm")

      assert_raises(RuntimeError) do
        harness.kandelo_validate_wasm_artifact(wasm)
      end
    end
  end

  def test_artifact_validation_rejects_failed_wasm_objdump_inspection
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = artifact_validation_harness(dir, ExecutingHarness)
      tool_dir = Pathname(dir)/"tools"
      tool_dir.mkpath
      { "wasm-objdump" => 1, "wasm-dis" => 0, "wasm-opt" => 0 }.each do |name, status|
        tool = tool_dir/name
        tool.binwrite("#!/bin/sh\nexit #{status}\n")
        tool.chmod 0755
      end
      harness.system_path = "#{tool_dir}:/bin:/usr/bin"
      wasm = harness.buildpath/"program.wasm"
      wasm.binwrite("\0asm")

      assert_raises(RuntimeError) do
        harness.kandelo_validate_wasm_artifact(wasm)
      end
    end
  end

  def test_artifact_validation_rejects_unknown_fork_policy
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_validate_wasm_artifact("program.wasm", fork: :sometimes)
    end

    assert_includes error.message, "invalid Kandelo fork policy"
  end

  def test_host_tool_reenters_the_dev_shell_and_preserves_the_caller_directory
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = Harness.new
      harness.build_path = Pathname(dir)/"build"
      harness.build_path.mkpath
      harness.nix_path = Pathname(dir)/"nix profile/bin/nix"
      harness.nix_path.dirname.mkpath
      harness.nix_path.binwrite("#!/bin/sh\n")
      File.chmod(0755, harness.nix_path)

      wrapper = harness.kandelo_host_cxx
      contents = wrapper.read

      assert wrapper.executable?
      assert_includes contents, "export PATH=#{harness.nix_path.dirname.to_s.shellescape}:"
      assert_includes contents, "caller_pwd=$PWD"
      assert_includes contents, "cd /tmp/kandelo\\ root"
      assert_includes contents,
                      'exec ./scripts/dev-shell.sh sh -c \'cd "$1"; shift; exec "$@"\' sh "$caller_pwd" c++ "$@"'
    end
  end

  def test_host_tool_executes_from_the_caller_directory
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      root = Pathname(dir)/"kandelo root"
      caller = Pathname(dir)/"formula build"
      wrapper_dir = Pathname(dir)/"wrappers"
      nix = Pathname(dir)/"nix profile/bin/nix"
      [root/"scripts", caller, wrapper_dir, nix.dirname].each(&:mkpath)
      (root/"scripts/dev-shell.sh").binwrite("#!/bin/sh\nexec \"$@\"\n")
      nix.binwrite("#!/bin/sh\n")
      File.chmod(0755, root/"scripts/dev-shell.sh")
      File.chmod(0755, nix)

      harness = Harness.new
      harness.build_path = wrapper_dir
      harness.nix_path = nix
      harness.root_path = root.to_s
      wrapper = harness.kandelo_host_tool("pwd")

      output = Dir.chdir(caller) { IO.popen([wrapper.to_s], &:read) }

      assert_equal "#{caller.realpath}\n", output
    end
  end

  def test_host_build_path_keeps_native_tools_and_removes_target_runtime_bins
    harness = Harness.new
    harness.runtime_formulae = [
      DependencyFormula.new(
        full_name:   "automattic/kandelo-homebrew/coreutils",
        opt_bin:     Pathname("/prefix/opt/coreutils/bin"),
        opt_sbin:    Pathname("/prefix/opt/coreutils/sbin"),
        opt_libexec: Pathname("/prefix/opt/coreutils/libexec"),
      ),
      DependencyFormula.new(
        full_name:   "wabt",
        opt_bin:     Pathname("/prefix/opt/wabt/bin"),
        opt_sbin:    Pathname("/prefix/opt/wabt/sbin"),
        opt_libexec: Pathname("/prefix/opt/wabt/libexec"),
      ),
    ]
    original = ENV.to_hash
    ENV["PATH"] = [
      "/prefix/opt/coreutils/bin",
      "/prefix/opt/coreutils/sbin",
      "/prefix/opt/coreutils/libexec/bin",
      "/prefix/opt/wabt/bin",
      "/usr/bin",
    ].join(File::PATH_SEPARATOR)

    harness.kandelo_isolate_host_build_path!
    build_path = ENV.fetch("PATH")

    refute_includes build_path, "/prefix/opt/coreutils/bin"
    refute_includes build_path, "/prefix/opt/coreutils/sbin"
    refute_includes build_path, "/prefix/opt/coreutils/libexec/bin"
    assert_includes build_path, "/prefix/opt/wabt/bin"
    assert_includes build_path, "/usr/bin"
  ensure
    ENV.replace(original) if original
  end

  def test_network_execution_uses_tap_owned_runner
    harness = Harness.new
    output = harness.kandelo_run_wasm(
      "program.wasm", ["a b"], env: { "TOKEN" => "x y" }, network: true,
      expected_fork_descendants: 1
    )

    assert_equal "runtime-ok\n", output
    assert_includes harness.command, "run-network-wasm.ts"
    assert_includes harness.command, "/tmp/kandelo\\ root"
    assert_includes harness.command, "KANDELO_FORMULA_GUEST_ENV_JSON="
    assert_includes harness.command, "KANDELO_FORMULA_ENABLE_NETWORK=1"
    assert_includes harness.command, "KANDELO_FORMULA_EXPECTED_FORK_DESCENDANTS=1"
    assert_includes harness.command, "TOKEN"
    refute_includes harness.command, "TOKEN=x\\ y"
    assert_includes harness.command, "program.wasm a\\ b"
    refute_includes harness.command, "examples/run-example.ts"
  end

  def test_execution_rejects_invalid_expected_fork_descendant_count
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_wasm(
        "program.wasm", [], expected_fork_descendants: -1
      )
    end

    assert_includes error.message, "expected fork descendant count must be a nonnegative integer"
  end

  def test_default_execution_keeps_standard_runner_and_removes_stale_host_dist
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      root = Pathname(dir)/"kandelo root"
      host_dist = root/"host/dist"
      host_dist.mkpath
      (host_dist/"stale.js").binwrite("stale")

      harness = Harness.new
      harness.root_path = root.to_s
      harness.kandelo_run_wasm("program.wasm", [])

      assert_includes harness.command, "examples/run-example.ts"
      refute_includes harness.command, "run-network-wasm.ts"
      refute_includes harness.command, "KANDELO_FORMULA_ENABLE_NETWORK="
      refute_path_exists host_dist
    end
  end

  def test_kms_execution_uses_stats_runner_and_removes_stale_host_dist
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      root = Pathname(dir)/"kandelo root"
      host_dist = root/"host/dist"
      host_dist.mkpath
      (host_dist/"stale.js").binwrite("stale")
      command = Pathname(dir)/"modeset"
      command.binwrite("\0asm")

      harness = Harness.new
      harness.root_path = root.to_s
      harness.test_path = Pathname(dir)/"formula test"
      harness.test_path.mkpath
      output = harness.kandelo_run_kms_wasm(
        command, argv: ["modeset", "--demo"], min_page_flips: 3, timeout_ms: 4_000
      )

      assert_equal "runtime-ok\n", output
      assert_includes harness.command, "run-kms-wasm.ts"
      assert_includes harness.command, root.to_s.shellescape
      assert_includes harness.command, "modeset.kms.wasm"
      assert_includes harness.command, "modeset"
      assert_includes harness.command, "--demo"
      assert_includes harness.command, "3 4000"
      assert_equal "kandelo_run_kms_wasm", harness.recorded_launcher
      refute_path_exists host_dist
    end
  end

  def test_kms_browser_execution_uses_focused_chromium_runner_and_removes_stale_host_dist
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      root = Pathname(dir)/"kandelo root"
      host_dist = root/"host/dist"
      host_dist.mkpath
      (host_dist/"stale.js").binwrite("stale")
      command = Pathname(dir)/"modeset"
      command.binwrite("\0asm")

      harness = Harness.new
      harness.root_path = root.to_s
      output = harness.kandelo_run_kms_browser_wasm(
        command, argv: ["modeset", "--demo"], min_page_flips: 4, timeout_ms: 5_000
      )

      assert_equal "runtime-ok\n", output
      assert_includes harness.command, "run-kms-browser-wasm.ts"
      assert_includes harness.command, root.to_s.shellescape
      assert_includes harness.command, command.to_s
      assert_includes harness.command, "minPageFlips"
      assert_includes harness.command, "timeoutMs"
      assert_includes harness.command, "modeset"
      assert_includes harness.command, "--demo"
      refute_path_exists host_dist
    end
  end

  def test_framebuffer_execution_uses_browser_runner_and_removes_stale_host_dist
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      root = Pathname(dir)/"kandelo root"
      host_dist = root/"host/dist"
      host_dist.mkpath
      (host_dist/"stale.js").binwrite("stale")
      command = Pathname(dir)/"fbdoom"
      wad = Pathname(dir)/"doom1.wad"
      command.binwrite("\0asm")
      wad.binwrite("IWAD")

      harness = Harness.new
      harness.root_path = root.to_s
      output = harness.kandelo_run_framebuffer_wasm(
        command,
        argv:                ["-iwad", "/doom1.wad"],
        guest_files:         { "/doom1.wad" => wad },
        min_writes:          3,
        min_nonblank_pixels: 2_000,
        timeout_ms:          4_000,
      )

      assert_equal "runtime-ok\n", output
      assert_includes harness.command, "run-framebuffer-wasm.ts"
      assert_includes harness.command, root.to_s.shellescape
      assert_includes harness.command, command.to_s.shellescape
      assert_includes harness.command, "doom1.wad"
      assert_includes harness.command, "minWrites"
      assert_includes harness.command, "minNonBlankPixels"
      assert_includes harness.command, "2000"
      assert_includes harness.command, "4000"
      refute_path_exists host_dist
    end
  end

  def test_framebuffer_execution_uses_meaningful_pixel_default
    harness = Harness.new

    harness.kandelo_run_framebuffer_wasm("fbdoom.wasm")

    assert_includes harness.command, "minNonBlankPixels"
    assert_includes harness.command, "1000"
  end

  def test_framebuffer_execution_expands_relative_formula_paths
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      command = Pathname(dir)/"fbdoom"
      wad = Pathname(dir)/"doom1.wad"
      command.binwrite("\0asm")
      wad.binwrite("IWAD")
      harness = Harness.new

      Dir.chdir(dir) do
        harness.kandelo_run_framebuffer_wasm(
          Pathname("fbdoom"), guest_files: { "/doom1.wad" => Pathname("doom1.wad") }
        )
      end

      assert_includes harness.command, command.to_s.shellescape
      assert_includes harness.command, wad.to_s
    end
  end

  def test_execution_accepts_explicit_guest_exec_programs
    harness = Harness.new

    harness.kandelo_run_wasm(
      "program.wasm",
      [],
      exec_programs: { "/bin/sh" => "/formula/dash" },
    )

    assert_includes harness.command, "run-network-wasm.ts"
    assert_includes harness.command, "KANDELO_FORMULA_EXEC_PROGRAMS_JSON="
    assert_includes harness.command, "/bin/sh"
    assert_includes harness.command, "/formula/dash"
  end

  def test_execution_accepts_explicit_guest_files
    harness = Harness.new

    harness.kandelo_run_wasm(
      "program.wasm",
      [],
      guest_files: { "/etc/service.conf" => "/formula/service.conf" },
    )

    assert_includes harness.command, "run-network-wasm.ts"
    assert_includes harness.command, "KANDELO_FORMULA_GUEST_FILES_JSON="
    assert_includes harness.command, "/etc/service.conf"
    assert_includes harness.command, "/formula/service.conf"
  end

  def test_execution_accepts_guest_argv0_and_writable_host_directory
    harness = Harness.new

    harness.kandelo_run_wasm(
      "program.wasm",
      ["input.tex"],
      argv0:                     "/home/linuxbrew/.linuxbrew/opt/texlive/bin/pdflatex",
      exec_programs:             {
        "/home/linuxbrew/.linuxbrew/opt/texlive/bin/pdflatex" => "/formula/pdflatex",
      },
      writable_host_directories: { "/work" => "/formula/test-output" },
    )

    assert_includes harness.command, "run-network-wasm.ts"
    assert_includes harness.command, "KANDELO_FORMULA_ARGV0="
    assert_includes harness.command, "KANDELO_FORMULA_WRITABLE_HOST_DIRS_JSON="
    assert_includes harness.command, "/home/linuxbrew/.linuxbrew/opt/texlive/bin/pdflatex"
    assert_includes harness.command, "/work"
    assert_includes harness.command, "/formula/test-output"
  end

  def test_execution_rejects_an_empty_guest_argv0
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_wasm("program.wasm", [], argv0: "")
    end

    assert_includes error.message, "guest argv0 must be a nonempty normalized absolute path"
  end

  def test_preserve_argv0_stages_the_original_command_name
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = Harness.new
      harness.test_path = Pathname(dir)/"test"
      harness.test_path.mkpath
      command = Pathname(dir)/"gunzip"
      command.binwrite("\0asm")

      harness.kandelo_run_wasm(command, ["-c"], preserve_argv0: true)

      assert_equal "\0asm", (harness.test_path/"gunzip").binread
      assert_includes harness.command, (harness.test_path/"gunzip").to_s
      refute_includes harness.command, "gunzip.wasm"
      assert_includes harness.command, "run-network-wasm.ts"
      assert_includes harness.command, "KANDELO_FORMULA_ENABLE_NETWORK=0"
    end
  end

  def test_execution_accepts_an_expected_nonzero_status
    harness = Harness.new

    output = harness.kandelo_run_wasm("program.wasm", ["missing"], expected_status: 2)

    assert_equal "runtime-ok\n", output
    assert_equal 2, harness.expected_status
  end

  def test_http_service_execution_uses_isolated_runner_and_removes_stale_host_dist
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      root = Pathname(dir)/"kandelo root"
      host_dist = root/"host/dist"
      host_dist.mkpath
      (host_dist/"stale.js").binwrite("stale")
      server = Pathname(dir)/"server"
      server.binwrite("\0asm")

      harness = Harness.new
      harness.root_path = root.to_s
      harness.test_path = Pathname(dir)/"formula test"
      harness.test_path.mkpath
      harness.shell_result = '[{"status":200,"text":"service-ok"}]'

      responses = harness.kandelo_run_http_service(
        server,
        ["-c", "/etc/server.conf"],
        port:     8080,
        requests: [{ path: "/health", headers: { "Host" => "localhost" } }],
        mounts:   { "/opt/server" => "/tmp/server keg" },
        env:      { "KERNEL_CWD" => "/opt/server" },
        uid:      1000,
        gid:      1000,
      )

      assert_equal [{ "status" => 200, "text" => "service-ok" }], responses
      assert_includes harness.command, "run-http-service-wasm.ts"
      assert_includes harness.command, "KANDELO_FORMULA_HTTP_SERVICE_JSON="
      assert_includes harness.command, "KANDELO_FORMULA_GUEST_ENV_JSON="
      assert_includes harness.command, "server.service.wasm"
      assert_includes harness.command, "server\\ keg"
      assert_includes harness.command, "1000"
      assert_equal "kandelo_run_http_service", harness.recorded_launcher
      assert_equal "\0asm", (harness.test_path/"server.service.wasm").binread
      refute_path_exists host_dist
    end
  end

  def test_http_service_execution_rejects_invalid_request_contract
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_http_service("server.wasm", [], port: 0, requests: [{ path: "/" }])
    end
    assert_equal "HTTP service port must be an integer from 1 through 65535", error.message

    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_http_service("server.wasm", [], port: 8080, requests: [])
    end
    assert_equal "HTTP service requests must be a nonempty array", error.message

    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_http_service(
        "server.wasm", [], port: 8080, requests: [{ path: "/" }], timeout: 0
      )
    end
    assert_equal "HTTP service timeout must be a positive number", error.message
  end

  def test_browser_execution_uses_focused_chromium_runner_and_removes_stale_host_dist
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      root = Pathname(dir)/"kandelo root"
      host_dist = root/"host/dist"
      host_dist.mkpath
      (host_dist/"stale.js").binwrite("stale")
      command = Pathname(dir)/"node"
      command.binwrite("\0asm")

      harness = Harness.new
      harness.root_path = root.to_s
      harness.test_path = Pathname(dir)/"formula test"
      harness.test_path.mkpath
      guest_file = Pathname(dir)/"format.dat"
      guest_file.binwrite("immutable")
      output = harness.kandelo_run_browser_wasm(
        command, ["-e", "console.log(42)"],
        argv0: "node", env: { "HOME" => "/root" },
        guest_files: { "/opt/formula/format.dat" => guest_file }, timeout_ms: 5_000
      )

      assert_equal "runtime-ok\n", output
      assert_includes harness.command, "run-browser-wasm.ts"
      assert_includes harness.command, root.to_s.shellescape
      assert_includes harness.command, command.to_s
      assert_includes harness.command, "console.log"
      assert_includes harness.command, "allowStderr"
      assert_includes harness.command, "node"
      manifest = harness.test_path/"node.browser-guest-files.json"
      assert_equal({ "/opt/formula/format.dat" => guest_file.to_s }, JSON.parse(manifest.read))
      assert_includes harness.command, manifest.to_s.shellescape
      refute_includes harness.command, guest_file.to_s
      refute_path_exists host_dist
    end
  end

  def test_browser_execution_accepts_posix_multicall_bracket_name
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = Harness.new
      harness.root_path = Pathname(dir)/"kandelo root"
      harness.test_path = Pathname(dir)/"formula test"
      harness.test_path.mkpath
      command = Pathname(dir)/"["
      command.binwrite("\0asm")
      relative_guest_file = Pathname(dir)/"relative.dat"
      relative_guest_file.binwrite("relative")

      Dir.chdir(dir) do
        harness.kandelo_run_browser_wasm(
          Pathname("["),
          ["value", "="],
          argv0:       "[",
          guest_files: { "/formula/relative.dat" => Pathname("relative.dat") },
        )
      end

      assert_includes harness.command, 'argv0\":\"\[\"'
      assert_includes harness.command, command.to_s.shellescape
      manifest = harness.test_path/"[.browser-guest-files.json"
      manifest_guest_file = Pathname(JSON.parse(manifest.read).fetch("/formula/relative.dat"))
      assert_equal relative_guest_file.realpath, manifest_guest_file.realpath
    end
  end

  def test_browser_execution_rejects_dot_dot_command_name
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_browser_wasm("program.wasm", [], argv0: "..")
    end

    assert_equal "invalid browser guest command name: ..", error.message
  end

  def test_pty_execution_uses_tap_owned_runner_and_removes_stale_host_dist
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      root = Pathname(dir)/"kandelo root"
      host_dist = root/"host/dist"
      host_dist.mkpath
      (host_dist/"stale.js").binwrite("stale")

      harness = Harness.new
      harness.root_path = root.to_s
      output = harness.kandelo_run_pty_wasm(
        "program.wasm", ["note.txt"],
        argv0:                      "/home/linuxbrew/.linuxbrew/opt/program/bin/program",
        env:                        { "KERNEL_CWD" => "/tmp/formula test" },
        inputs:                     ["\u001c", "beta", "\r"],
        rerun_inputs:               ["\u0018"],
        exec_programs:              { "/opt/program/bin/helper" => "/formula/helper" },
        guest_files:                { "/etc/program.conf" => "/formula/program.conf" },
        guest_directories:          ["/home/linuxbrew/.linuxbrew/var/program/save"],
        writable_guest_directories: ["/home/linuxbrew/.linuxbrew/var/program"],
        writable_host_directories:  { "/work" => "/formula/test output" }
      )

      assert_equal "runtime-ok\n", output
      assert_includes harness.command, "run-pty-wasm.ts"
      assert_includes harness.command, "KANDELO_FORMULA_PTY_CONFIG_JSON="
      assert_includes harness.command, "/home/linuxbrew/.linuxbrew/opt/program/bin/program"
      assert_includes harness.command, "note.txt"
      assert_includes harness.command, "beta"
      assert_includes harness.command, "rerunInputs"
      assert_includes harness.command, "/opt/program/bin/helper"
      assert_includes harness.command, "/formula/helper"
      assert_includes harness.command, "/etc/program.conf"
      assert_includes harness.command, "/home/linuxbrew/.linuxbrew/var/program"
      assert_includes harness.command, "writableGuestDirectories"
      assert_includes harness.command, "writableHostDirectories"
      assert_includes harness.command, "/work"
      assert_includes harness.command, "/formula/test\\ output"
      assert_includes harness.command, "program.wasm"
      assert_equal "kandelo_run_pty_wasm", harness.recorded_launcher
      refute_path_exists host_dist
    end
  end

  def test_pty_execution_rejects_an_empty_guest_argv0
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_pty_wasm("program.wasm", [], inputs: [], argv0: "")
    end

    assert_includes error.message, "guest argv0 must be a nonempty normalized absolute path"
  end

  private

  def artifact_validation_harness(dir, harness_class = Harness)
    root = Pathname(dir)/"kandelo root"
    build = Pathname(dir)/"build"
    (root/"scripts").mkpath
    build.mkpath
    (root/"scripts/wasm-artifact-guards.sh").binwrite("# validation fixture\n")

    harness = harness_class.new
    harness.root_path = root.to_s
    harness.build_path = build
    harness.prefix_path = Pathname(dir)/"cellar/formula/1.0"
    harness
  end
end
