# typed: strict
# frozen_string_literal: true

require "fileutils"
require "json"
require "shellwords"

# KandeloFormulaSupport is the single place Kandelo-specific mechanics live so
# that formula bodies stay idiomatic Homebrew. It owns SDK/toolchain activation
# (via the HOMEBREW_KANDELO_ROOT env bridge), the wasm cross-compile
# environment, isolated native build tools, fork instrumentation, the
# transitional shell-out to a registry build script, installing a built `.wasm`
# as an executable, and running a `.wasm` under the Node kernel host for
# `test do`.
#
# See docs/plans/2026-07-05-homebrew-tap-layout-idiomatic-spec.md (Track A0) for
# the contract this implements. The `kandelo_build_package` shell-out is the
# accepted Tier-2 deviation (spec §6) for heavy ported formulae (ruby/perl/…)
# whose 49 KB `build-<name>.sh` is not yet decomposed into idiomatic steps.
module KandeloFormulaSupport
  # Resolve the Kandelo checkout the SDK/toolchain lives in. Returns the path
  # string, or nil when the env bridge is not configured.
  def kandelo_root
    root = ENV["HOMEBREW_KANDELO_ROOT"] || ENV.fetch("KANDELO_HOMEBREW_KANDELO_ROOT", nil)
    root.to_s.empty? ? nil : root
  end

  # Like #kandelo_root but aborts the build when the env bridge is missing. The
  # SDK/toolchain is worktree-local, not a brew dep yet (spec §6 deviation).
  def kandelo_require_root!
    root = kandelo_root
    odie "HOMEBREW_KANDELO_ROOT must point at a Kandelo checkout" if root.nil?
    root
  end

  # The wasm target arch (wasm32 default). Drives the SDK tool prefix and sysroot.
  def kandelo_arch
    ENV.fetch("HOMEBREW_KANDELO_ARCH", ENV.fetch("KANDELO_HOMEBREW_ARCH", "wasm32"))
  end

  def kandelo_require_arch!(*supported)
    return if supported.include?(kandelo_arch)

    odie "unsupported Kandelo architecture #{kandelo_arch}; expected #{supported.join(", ")}"
  end

  # Prepend the Kandelo SDK, Node, and LLVM to PATH and export the LLVM env the
  # SDK wrappers read. Returns the resolved Kandelo root. This is the single
  # place SDK/toolchain activation happens.
  def kandelo_activate_sdk!
    root = kandelo_require_root!
    ENV.prepend_path "PATH", "#{root}/sdk/bin"

    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      ENV.prepend_path "PATH", File.dirname(node)
    end

    if (llvm_bin = ENV.fetch("HOMEBREW_KANDELO_LLVM_BIN", nil)).to_s != ""
      ENV["WASM_POSIX_LLVM_DIR"] = llvm_bin
      ENV["LLVM_BIN"] = llvm_bin
      ENV.prepend_path "PATH", llvm_bin
    end

    root
  end

  # Export the wasm cross-compile sysroot/glue env and clear host include vars
  # that would otherwise leak macOS/Xcode headers into a wasm compile. Used by
  # `test do` blocks that compile a smoke program against a library keg.
  def kandelo_activate_sysroot!(root = kandelo_require_root!)
    sysroot = (kandelo_arch == "wasm64") ? "sysroot64" : "sysroot"
    ENV["WASM_POSIX_SYSROOT"] = "#{root}/#{sysroot}"
    ENV["WASM_POSIX_GLUE_DIR"] = "#{root}/libc/glue"
    %w[
      SDKROOT
      HOMEBREW_SDKROOT
      CPATH
      C_INCLUDE_PATH
      CPLUS_INCLUDE_PATH
      OBJC_INCLUDE_PATH
    ].each { |key| ENV.delete(key) }
    root
  end

  # Absolute path to the SDK C compiler wrapper for the active arch.
  def kandelo_cc(root = kandelo_require_root!)
    kandelo_tool("cc", root)
  end

  def kandelo_ar(root = kandelo_require_root!)
    kandelo_tool("ar", root)
  end

  def kandelo_ranlib(root = kandelo_require_root!)
    kandelo_tool("ranlib", root)
  end

  def kandelo_configure(root = kandelo_require_root!)
    kandelo_tool("configure", root)
  end

  def kandelo_tool(name, root = kandelo_require_root!)
    kandelo_require_arch!("wasm32", "wasm64")
    "#{root}/sdk/bin/#{kandelo_arch}posix-#{name}"
  end

  # Return a wrapper for a native build tool from Kandelo's canonical dev
  # shell. Homebrew's compiler shims include Formula dependency paths, so a
  # cross Formula that depends on target libcxx cannot use those shims for host
  # generators. The wrapper changes to the Kandelo checkout while evaluating
  # its flake, then restores the Formula caller's working directory before
  # executing the tool. Wrap the highest-level build driver practical so a
  # multi-file native phase enters the dev shell once rather than once per
  # compiler invocation.
  def kandelo_host_tool(name)
    odie "invalid host tool name: #{name}" unless name.match?(/\A[+._a-z0-9-]+\z/i)

    root = kandelo_require_root!
    nix = kandelo_nix_executable
    odie "Nix executable not found at #{nix}" unless nix.executable?

    wrapper = buildpath/"kandelo-host-#{name.tr("+", "x")}"
    wrapper.delete if wrapper.exist?
    wrapper.write <<~SH
      #!/bin/sh
      set -eu
      export PATH=#{nix.dirname.to_s.shellescape}:/usr/bin:/bin
      caller_pwd=$PWD
      cd #{root.shellescape}
      exec ./scripts/dev-shell.sh sh -c 'cd "$1"; shift; exec "$@"' sh "$caller_pwd" #{name} "$@"
    SH
    File.chmod(0755, wrapper)
    wrapper
  end

  def kandelo_host_cc
    kandelo_host_tool("cc")
  end

  def kandelo_host_cxx
    kandelo_host_tool("c++")
  end

  def kandelo_nix_executable
    Pathname("/nix/var/nix/profiles/default/bin/nix")
  end

  # Establish a clean cross-build environment for an idiomatic Formula
  # install block, then restore Homebrew's environment when the block exits.
  def kandelo_wasm_build
    saved = ENV.to_hash
    root = kandelo_activate_sdk!
    kandelo_activate_sysroot!(root)

    %w[CFLAGS CPPFLAGS CXXFLAGS LDFLAGS MACOSX_DEPLOYMENT_TARGET].each { |key| ENV.delete(key) }
    ENV["CC"] = kandelo_cc(root)
    ENV["CXX"] = kandelo_tool("c++", root)
    ENV["AR"] = kandelo_ar(root)
    ENV["RANLIB"] = kandelo_ranlib(root)
    ENV["NM"] = kandelo_tool("nm", root)
    ENV["STRIP"] = kandelo_tool("strip", root)
    ENV["PKG_CONFIG"] = kandelo_tool("pkg-config", root)

    yield root
  ensure
    ENV.replace(saved) if saved
  end

  # The SDK configure wrapper supplies the target host and a default prefix.
  # The later Formula prefix wins and keeps installed paths keg-relative.
  def kandelo_std_configure_args
    ["--prefix=#{prefix}"]
  end

  # Instrument a linked program in place when its normal runtime path can call
  # fork(). The Kandelo checkout owns the ABI-coupled instrumentation tool.
  def kandelo_fork_instrument(wasm_path)
    root = kandelo_require_root!
    wasm = Pathname(wasm_path)
    instrumented = Pathname("#{wasm}.fork-instrumented")
    instrumented.delete if instrumented.exist?

    system "#{root}/scripts/run-wasm-fork-instrument.sh", wasm.to_s,
           "-o", instrumented.to_s
    instrumented.rename(wasm)
    wasm
  ensure
    instrumented&.delete if instrumented&.exist?
  end

  # Transitional Tier-2 bridge (spec §6 deviation): activate the SDK, export the
  # WASM_POSIX_DEP_* build-script contract, and shell out to the registry
  # `build-<name>.sh`. Returns the out dir the script installed into.
  #
  # Deprecated by design: heavy ported formulae land through here before their
  # `install` is decomposed into idiomatic steps. Every call is a tracked
  # deviation.
  def kandelo_build_package(name, script, source_url, source_sha256, script_env: {})
    root = kandelo_activate_sdk!

    out_dir = buildpath/"kandelo-package-out"
    ENV["WASM_POSIX_DEP_NAME"] = name
    ENV["WASM_POSIX_DEP_VERSION"] = version.to_s
    ENV["WASM_POSIX_DEP_SOURCE_URL"] = source_url
    ENV["WASM_POSIX_DEP_SOURCE_SHA256"] = source_sha256
    ENV["WASM_POSIX_DEP_OUT_DIR"] = out_dir
    ENV["WASM_POSIX_DEP_WORK_DIR"] = buildpath/"kandelo-package-work"
    ENV["WASM_POSIX_DEP_TARGET_ARCH"] = kandelo_arch
    script_env.each { |key, value| ENV[key] = value.to_s }

    system "bash", "#{root}/packages/registry/#{name}/#{script}"

    out_dir
  end

  # Install a built `.wasm` from an out dir as an executable `bin/<bin_name>`.
  def kandelo_install_bin(out_dir, wasm_name, bin_name)
    wasm = Pathname(out_dir)/wasm_name
    chmod 0755, wasm
    bin.install wasm => bin_name
    chmod 0755, bin/bin_name
  end

  # Run a built `.wasm` under the Node kernel host and return its stdout. The
  # guest inherits the passed `env:`, matching how a real `brew test` exercises
  # behavior. `network: true` opts into Node's real external-TCP backend, while
  # `preserve_argv0: true` keeps multicall command names such as gunzip,
  # `argv0:` supplies an explicit staged guest executable path,
  # `exec_programs:` stages explicit guest exec targets, `guest_files:` stages
  # ordinary files in the guest VFS, `writable_host_directories:` exposes
  # explicit host directories as writable guest mounts for output validation,
  # and `expected_status:` permits tests for specified nonzero results such as
  # a grep no-match status.
  def kandelo_run_wasm(
    bin_path, argv, env: {}, stdin: nil, merge_stderr: false, network: false,
    preserve_argv0: false, argv0: nil, exec_programs: {}, guest_files: {},
    writable_host_directories: {}, expected_status: 0
  )
    root = kandelo_require_root!
    if !argv0.nil? && (
      argv0.empty? || !argv0.start_with?("/") || argv0.include?("\0") ||
      Pathname(argv0).cleanpath.to_s != argv0
    )
      odie "guest argv0 must be a nonempty normalized absolute path: #{argv0.inspect}"
    end
    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      ENV.prepend_path "PATH", File.dirname(node)
    end

    wasm_path = Pathname(bin_path)
    if wasm_path.extname != ".wasm"
      staged_name = preserve_argv0 ? wasm_path.basename : "#{wasm_path.basename}.wasm"
      staged_wasm = testpath/staged_name
      File.binwrite(staged_wasm, File.binread(wasm_path))
      wasm_path = staged_wasm
    end

    command = +"cd "
    command << Shellwords.escape(root) << " && "
    isolated_runner = network || preserve_argv0 || !argv0.nil? || exec_programs.any? ||
                      guest_files.any? || writable_host_directories.any?
    if isolated_runner
      guest_env = JSON.generate(env.transform_values(&:to_s))
      guest_exec_programs = JSON.generate(exec_programs.transform_values(&:to_s))
      staged_guest_files = JSON.generate(guest_files.transform_values(&:to_s))
      writable_mounts = JSON.generate(writable_host_directories.transform_values(&:to_s))
      command << "KANDELO_FORMULA_GUEST_ENV_JSON=#{Shellwords.escape(guest_env)} "
      command << "KANDELO_FORMULA_EXEC_PROGRAMS_JSON=#{Shellwords.escape(guest_exec_programs)} "
      command << "KANDELO_FORMULA_GUEST_FILES_JSON=#{Shellwords.escape(staged_guest_files)} "
      command << "KANDELO_FORMULA_WRITABLE_HOST_DIRS_JSON=#{Shellwords.escape(writable_mounts)} "
      command << "KANDELO_FORMULA_ARGV0=#{Shellwords.escape(argv0.to_s)} " if argv0
      command << "KANDELO_FORMULA_ENABLE_NETWORK=#{network ? 1 : 0} "
    else
      env.each { |key, value| command << "#{key}=#{Shellwords.escape(value.to_s)} " }
    end
    command << "node --experimental-wasm-exnref --import tsx/esm "
    if isolated_runner
      runner = Pathname(__dir__)/"run-network-wasm.ts"
      command << "#{Shellwords.escape(runner.to_s)} #{Shellwords.escape(root)} "
    else
      command << "examples/run-example.ts "
    end
    command << Shellwords.escape(wasm_path.to_s)
    argv.each { |arg| command << " " << Shellwords.escape(arg.to_s) }

    if stdin.nil?
      command << " < /dev/null"
    else
      stdin_path = testpath/"#{wasm_path.basename}.stdin"
      File.binwrite(stdin_path, stdin)
      command << " < #{Shellwords.escape(stdin_path.to_s)}"
    end
    command << " 2>&1" if merge_stderr

    output = shell_output(command, expected_status)
    kandelo_record_node_execution!(wasm_path, argv)
    output
  end

  # Run an interactive Wasm program through Kandelo's real PTY path. Inputs
  # are written in order after the process starts, with short delays so curses
  # applications can render and transition between prompts. Writable guest
  # directories use isolated mounts that survive every spawn in this run.
  def kandelo_run_pty_wasm(
    bin_path, argv, inputs:, env: {}, guest_files: {}, guest_directories: [],
    writable_guest_directories: [], rerun_inputs: nil, expected_status: 0,
    initial_delay_ms: 500, input_delay_ms: 180, cols: 100, rows: 30
  )
    root = kandelo_require_root!
    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      ENV.prepend_path "PATH", File.dirname(node)
    end

    wasm_path = Pathname(bin_path)
    if wasm_path.extname != ".wasm"
      staged_wasm = testpath/"#{wasm_path.basename}.wasm"
      File.binwrite(staged_wasm, File.binread(wasm_path))
      wasm_path = staged_wasm
    end

    config = JSON.generate({
      env:              env,
      inputs:           inputs,
      rerunInputs:      rerun_inputs,
      guestFiles:       guest_files.transform_values(&:to_s),
      guestDirectories: guest_directories.map(&:to_s),
      writableGuestDirectories: writable_guest_directories.map(&:to_s),
      initialDelayMs:   initial_delay_ms,
      inputDelayMs:     input_delay_ms,
      cols:             cols,
      rows:             rows,
    })

    # Compiled host output shadows TypeScript source under tsx. PTY formula
    # tests must exercise the checkout supplied by HOMEBREW_KANDELO_ROOT.
    FileUtils.rm_rf(Pathname(root)/"host/dist")

    runner = Pathname(__dir__)/"run-pty-wasm.ts"
    command = "cd #{Shellwords.escape(root)} && "
    command << "KANDELO_FORMULA_PTY_CONFIG_JSON=#{Shellwords.escape(config)} "
    command << "node --experimental-wasm-exnref --import tsx/esm "
    command << "#{Shellwords.escape(runner.to_s)} #{Shellwords.escape(root)} "
    command << Shellwords.escape(wasm_path.to_s)
    argv.each { |arg| command << " #{Shellwords.escape(arg.to_s)}" }
    command << " 2>&1"

    output = shell_output(command, expected_status)
    kandelo_record_node_execution!(wasm_path, argv, launcher: "kandelo_run_pty_wasm")
    output
  end

  # Run a long-lived DRM/KMS program until it has completed real PAGE_FLIP
  # commits. The runner attaches the kernel's KMS stats channel before spawn,
  # so this verifies the guest libdrm path without requiring a Node canvas.
  # Browser/WebGL rendering remains a separate Chromium validation gate.
  def kandelo_run_kms_wasm(bin_path, argv: [], min_page_flips: 2, timeout_ms: 30_000)
    root = kandelo_require_root!
    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      ENV.prepend_path "PATH", File.dirname(node)
    end

    wasm_path = Pathname(bin_path)
    if wasm_path.extname != ".wasm"
      staged_wasm = testpath/"#{wasm_path.basename}.kms.wasm"
      File.binwrite(staged_wasm, File.binread(wasm_path))
      wasm_path = staged_wasm
    end

    # Compiled host output shadows TypeScript source under tsx. KMS tests must
    # exercise the checkout supplied by HOMEBREW_KANDELO_ROOT.
    FileUtils.rm_rf(Pathname(root)/"host/dist")

    runner = Pathname(__dir__)/"run-kms-wasm.ts"
    command = [
      "node", "--experimental-wasm-exnref", "--import", "tsx/esm",
      runner, root, wasm_path, JSON.generate(argv.map(&:to_s)), min_page_flips, timeout_ms,
    ].map { |arg| Shellwords.escape(arg.to_s) }.join(" ")
    output = shell_output("cd #{Shellwords.escape(root)} && #{command} < /dev/null")
    kandelo_record_node_execution!(wasm_path, argv, launcher: "kandelo_run_kms_wasm")
    output
  end

  # Run a DRM/KMS program through the browser host with a real transferred
  # OffscreenCanvas. The focused page attaches the canvas before spawning the
  # guest, waits for kernel PAGE_FLIP telemetry, and the runner verifies that
  # Chromium composed nonuniform pixels from the WebGL-owned canvas.
  def kandelo_run_kms_browser_wasm(bin_path, argv: [], min_page_flips: 2, timeout_ms: 60_000)
    root = kandelo_require_root!
    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      ENV.prepend_path "PATH", File.dirname(node)
    end

    config = JSON.generate({
      argv:         argv.map(&:to_s),
      minPageFlips: min_page_flips,
      timeoutMs:    timeout_ms,
    })

    # Compiled host output shadows TypeScript source under tsx. Browser formula
    # tests must exercise the checkout supplied by HOMEBREW_KANDELO_ROOT.
    FileUtils.rm_rf(Pathname(root)/"host/dist")

    runner = Pathname(__dir__)/"run-kms-browser-wasm.ts"
    command = [
      "node", "--experimental-wasm-exnref", "--import", "tsx/esm",
      runner, root, Pathname(bin_path), config,
    ].map { |arg| Shellwords.escape(arg.to_s) }.join(" ")

    shell_output("cd #{Shellwords.escape(root)} && #{command} < /dev/null")
  end

  # Run a formula executable through Kandelo's Chromium browser host. This is
  # intentionally separate from the Node runner: browser worker startup,
  # SharedArrayBuffer isolation, Wasm memory, and process teardown are distinct
  # platform contracts. `argv0:` controls the guest command name for multicall
  # runtimes whose behavior depends on argv[0]. Immutable `guest_files:` use
  # the same absolute-path and bounded-rootfs contract as Node formula tests.
  def kandelo_run_browser_wasm(
    bin_path, argv, argv0: nil, env: {}, guest_files: {}, timeout_ms: 120_000,
    allow_stderr: false
  )
    root = kandelo_require_root!
    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      ENV.prepend_path "PATH", File.dirname(node)
    end

    wasm_path = Pathname(bin_path).expand_path
    command_name = (argv0 || wasm_path.basename).to_s
    invalid_command_name = command_name.empty? || command_name.include?("/") ||
                           command_name.include?("\0") || [".", ".."].include?(command_name)
    odie "invalid browser guest command name: #{command_name}" if invalid_command_name

    config = JSON.generate({
      argv:        argv.map(&:to_s),
      argv0:       command_name,
      env:         env.transform_values(&:to_s),
      timeoutMs:   timeout_ms,
      allowStderr: allow_stderr,
    })
    guest_files_manifest = testpath/"#{wasm_path.basename}.browser-guest-files.json"
    File.binwrite(
      guest_files_manifest,
      JSON.generate(guest_files.transform_values { |path| Pathname(path).expand_path.to_s }),
    )

    # Compiled host output shadows TypeScript source under tsx/Vite. Browser
    # formula tests must exercise the checkout supplied by the build contract.
    FileUtils.rm_rf(Pathname(root)/"host/dist")

    runner = Pathname(__dir__)/"run-browser-wasm.ts"
    command = [
      "node", "--experimental-wasm-exnref", "--import", "tsx/esm",
      runner, root, wasm_path, config, guest_files_manifest
    ].map { |arg| Shellwords.escape(arg.to_s) }.join(" ")

    shell_output("cd #{Shellwords.escape(root)} && #{command} < /dev/null")
  end

  # Run a framebuffer program through Kandelo's browser host and require
  # observable /dev/fb0 rendering. The tap-owned runner builds a temporary VFS
  # from the installed executable and explicitly staged guest files, boots the
  # program with a PTY, then checks framebuffer bind/write telemetry and canvas
  # pixels in Chromium.
  def kandelo_run_framebuffer_wasm(
    bin_path, argv: [], guest_files: {}, min_writes: 1,
    min_nonblank_pixels: 1_000, timeout_ms: 30_000
  )
    root = kandelo_require_root!
    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      ENV.prepend_path "PATH", File.dirname(node)
    end

    wasm_path = Pathname(bin_path).expand_path
    config = JSON.generate({
      argv:              argv.map(&:to_s),
      guestFiles:        guest_files.transform_values { |path| Pathname(path).expand_path.to_s },
      minWrites:         min_writes,
      minNonBlankPixels: min_nonblank_pixels,
      timeoutMs:         timeout_ms,
    })

    # Compiled host output shadows TypeScript source under tsx. Browser formula
    # tests must exercise the checkout supplied by HOMEBREW_KANDELO_ROOT.
    FileUtils.rm_rf(Pathname(root)/"host/dist")

    runner = Pathname(__dir__)/"run-framebuffer-wasm.ts"
    command = [
      "node", "--experimental-wasm-exnref", "--import", "tsx/esm",
      runner, root, wasm_path, config
    ].map { |arg| Shellwords.escape(arg.to_s) }.join(" ")

    shell_output("cd #{Shellwords.escape(root)} && #{command} < /dev/null")
  end

  def kandelo_record_node_execution!(wasm_path, argv, launcher: "kandelo_run_wasm")
    receipt = ENV.fetch("HOMEBREW_KANDELO_NODE_RECEIPT_PATH", nil)
    return if receipt.to_s.empty?

    abi = Integer(ENV.fetch("HOMEBREW_KANDELO_ABI"), 10)
    receipt_path = Pathname(receipt)
    temp_path = Pathname("#{receipt}.tmp-#{Process.pid}")
    receipt_path.dirname.mkpath
    File.binwrite(temp_path, JSON.generate({
      schema:      1,
      formula:     name,
      arch:        kandelo_arch,
      kandelo_abi: abi,
      runtime:     "node",
      launcher:    launcher,
      argv:        [wasm_path.to_s, *argv.map(&:to_s)],
      status:      "success",
    }))
    File.rename(temp_path, receipt_path)
  ensure
    File.delete(temp_path) if temp_path&.exist?
  end
end
