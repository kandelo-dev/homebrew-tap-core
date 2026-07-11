# typed: strict
# frozen_string_literal: true

require "json"
require "shellwords"

# KandeloFormulaSupport is the single place Kandelo-specific mechanics live so
# that formula bodies stay idiomatic Homebrew. It owns SDK/toolchain activation
# (via the HOMEBREW_KANDELO_ROOT env bridge), the wasm cross-compile
# environment, the transitional shell-out to a registry build script, installing
# a built `.wasm` as an executable, and running a `.wasm` under the Node kernel
# host for `test do`.
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
  # behavior. `network: true` opts into Node's real external-TCP backend.
  def kandelo_run_wasm(bin_path, argv, env: {}, stdin: nil, merge_stderr: false, network: false)
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

    command = +"cd "
    command << Shellwords.escape(root) << " && "
    if network
      guest_env = JSON.generate(env.transform_values(&:to_s))
      command << "KANDELO_FORMULA_GUEST_ENV_JSON=#{Shellwords.escape(guest_env)} "
    else
      env.each { |key, value| command << "#{key}=#{Shellwords.escape(value.to_s)} " }
    end
    command << "node --experimental-wasm-exnref --import tsx/esm "
    if network
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

    output = shell_output(command)
    kandelo_record_node_execution!(wasm_path, argv)
    output
  end

  def kandelo_record_node_execution!(wasm_path, argv)
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
      launcher:    "kandelo_run_wasm",
      argv:        [wasm_path.to_s, *argv.map(&:to_s)],
      status:      "success",
    }))
    File.rename(temp_path, receipt_path)
  ensure
    File.delete(temp_path) if temp_path&.exist?
  end
end
