# typed: strict
# frozen_string_literal: true

require "minitest/autorun"
# Standalone Ruby does not preload Homebrew's Pathname helper.
require "pathname" # rubocop:disable Lint/RedundantRequireStatement
require "tmpdir"
require_relative "../kandelo_formula_support"

# Regression coverage for Formula runtime execution evidence.
class KandeloFormulaSupportTest < Minitest::Test
  # Minimal Formula double for command-construction tests.
  class Harness
    include KandeloFormulaSupport

    attr_accessor :build_path, :nix_path, :root_path, :test_path
    attr_reader :command, :expected_status, :recorded_launcher, :system_args

    def kandelo_require_root!
      root_path || "/tmp/kandelo root"
    end

    def testpath
      test_path || Pathname("/tmp/formula test")
    end

    def buildpath
      build_path || testpath
    end

    def kandelo_nix_executable
      nix_path || super
    end

    def odie(message)
      raise RuntimeError, message
    end

    def shell_output(command, expected_status = 0)
      @command = command
      @expected_status = expected_status
      "runtime-ok\n"
    end

    def odie(message)
      raise message
    end

    # The Formula double must intercept Kernel#system under its real name.
    # rubocop:disable Naming/PredicateMethod
    def system(*args)
      @system_args = args
      output = args.fetch(args.index("-o") + 1)
      File.binwrite(output, "instrumented")
      true
    end
    # rubocop:enable Naming/PredicateMethod

    def kandelo_record_node_execution!(_wasm_path, _argv, launcher: "kandelo_run_wasm")
      @recorded_launcher = launcher
      nil
    end
  end

  def test_node_execution_receipt_is_optional
    previous = ENV.delete("HOMEBREW_KANDELO_NODE_RECEIPT_PATH")

    assert_nil Harness.new.kandelo_record_node_execution!("program.wasm", [])
  ensure
    ENV["HOMEBREW_KANDELO_NODE_RECEIPT_PATH"] = previous if previous
  end

  def test_fork_instrumentation_replaces_the_linked_program
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = Harness.new
      wasm = Pathname(dir)/"program.wasm"
      wasm.binwrite("linked")

      assert_equal wasm, harness.kandelo_fork_instrument(wasm)
      assert_equal "instrumented", wasm.binread
      assert_equal "/tmp/kandelo root/scripts/run-wasm-fork-instrument.sh", harness.system_args.first
      assert_equal [wasm.to_s, "-o", "#{wasm}.fork-instrumented"], harness.system_args.drop(1)
      refute File.exist?("#{wasm}.fork-instrumented")
    end
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

  def test_network_execution_uses_tap_owned_runner
    harness = Harness.new
    output = harness.kandelo_run_wasm(
      "program.wasm", ["a b"], env: { "TOKEN" => "x y" }, network: true
    )

    assert_equal "runtime-ok\n", output
    assert_includes harness.command, "run-network-wasm.ts"
    assert_includes harness.command, "/tmp/kandelo\\ root"
    assert_includes harness.command, "KANDELO_FORMULA_GUEST_ENV_JSON="
    assert_includes harness.command, "KANDELO_FORMULA_ENABLE_NETWORK=1"
    assert_includes harness.command, "TOKEN"
    refute_includes harness.command, "TOKEN=x\\ y"
    assert_includes harness.command, "program.wasm a\\ b"
    refute_includes harness.command, "examples/run-example.ts"
  end

  def test_default_execution_keeps_standard_runner
    harness = Harness.new

    harness.kandelo_run_wasm("program.wasm", [])

    assert_includes harness.command, "examples/run-example.ts"
    refute_includes harness.command, "run-network-wasm.ts"
    refute_includes harness.command, "KANDELO_FORMULA_ENABLE_NETWORK="
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
      argv0:                       "/home/linuxbrew/.linuxbrew/opt/texlive/bin/pdflatex",
      exec_programs:               {
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
          Pathname("["), ["value", "="], argv0: "[",
          guest_files: { "/formula/relative.dat" => Pathname("relative.dat") }
        )
      end

      assert_includes harness.command, 'argv0\":\"\[\"'
      assert_includes harness.command, command.to_s.shellescape
      manifest = harness.test_path/"[.browser-guest-files.json"
      assert_equal({ "/formula/relative.dat" => relative_guest_file.to_s }, JSON.parse(manifest.read))
    end
  end

  def test_browser_execution_rejects_dot_dot_command_name
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_browser_wasm("program.wasm", [], argv0: "..")
    end

    assert_equal "invalid browser guest command name: ..", error.message
  end

  def test_pty_execution_uses_tap_owned_runner
    harness = Harness.new
    output = harness.kandelo_run_pty_wasm(
      "program.wasm", ["note.txt"],
      env:               { "KERNEL_CWD" => "/tmp/formula test" },
      inputs:            ["\u001c", "beta", "\r"],
      rerun_inputs:      ["\u0018"],
      guest_files:       { "/etc/program.conf" => "/formula/program.conf" },
      guest_directories: ["/home/linuxbrew/.linuxbrew/var/program/save"],
      writable_guest_directories: ["/home/linuxbrew/.linuxbrew/var/program"]
    )

    assert_equal "runtime-ok\n", output
    assert_includes harness.command, "run-pty-wasm.ts"
    assert_includes harness.command, "KANDELO_FORMULA_PTY_CONFIG_JSON="
    assert_includes harness.command, "note.txt"
    assert_includes harness.command, "beta"
    assert_includes harness.command, "rerunInputs"
    assert_includes harness.command, "/etc/program.conf"
    assert_includes harness.command, "/home/linuxbrew/.linuxbrew/var/program"
    assert_includes harness.command, "writableGuestDirectories"
    assert_includes harness.command, "program.wasm"
    assert_equal "kandelo_run_pty_wasm", harness.recorded_launcher
  end
end
