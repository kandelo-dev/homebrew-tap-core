# typed: strict
# frozen_string_literal: true

require "digest"
require "fileutils"
require "json"
require "pathname"
require "shellwords"
require "tempfile"

if defined?(KandeloFormulaSupport)
  unless KandeloFormulaSupport::KANDELO_FORMULA_SUPPORT_API_VERSION == 1 &&
         Digest::SHA256.file(Pathname(__FILE__).realpath).hexdigest ==
           KandeloFormulaSupport::KANDELO_TIER2_RUNTIME.fetch("support_sha256")
    raise "loaded Kandelo Formula support copies are incompatible"
  end
else
# KandeloFormulaSupport is the single place Kandelo-specific mechanics live so
# that formula bodies stay idiomatic Homebrew. It owns SDK/toolchain activation
# (via the HOMEBREW_KANDELO_ROOT env bridge), the wasm cross-compile
# environment, host/target dependency isolation, isolated native build tools,
# fork instrumentation, final Wasm artifact validation, the transitional
# shell-out to a registry build script, installing a built `.wasm` as an
# executable, and running a `.wasm` under the Node kernel host for `test do`.
#
# See docs/plans/2026-07-05-homebrew-tap-layout-idiomatic-spec.md (Track A0) for
# the contract this implements. The `kandelo_build_package` shell-out is the
# accepted Tier-2 deviation (spec §6) for heavy ported formulae (ruby/perl/…)
# whose 49 KB `build-<name>.sh` is not yet decomposed into idiomatic steps.
module KandeloFormulaSupport
  KANDELO_FORMULA_SUPPORT_API_VERSION = 1
  KANDELO_CORE_TAP_FORMULA_PREFIX = "kandelo-dev/tap-core/"
  KANDELO_TIER2_ATTESTATION_BASENAME = ".kandelo-publisher-tier2-attestation.json"
  KANDELO_TIER2_ATTESTATION_MAX_BYTES = 16_384
  KANDELO_TIER2_SOURCE_MAX_BYTES = 1_048_576
  KANDELO_TIER2_SCRIPT_ENV_MAX_KEYS = 64
  KANDELO_TIER2_SCRIPT_ENV_KEY_MAX_BYTES = 4_096
  KANDELO_TIER2_SCRIPT_ENV_VALUE_MAX_BYTES = 4_096
  KANDELO_TIER2_SCRIPT_ENV_VALUE_TOTAL_BYTES = 16_384
  KANDELO_TIER2_TOP_KEYS = %w[
    arch formula formula_sha256 full_name schema support_sha256 tap tier2_bridge
  ].freeze
  KANDELO_TIER2_BRIDGE_KEYS = %w[
    build_toml_sha256 package package_toml_sha256 script script_env_keys
    script_sha256 source_mode source_sha256 source_url version
  ].freeze
  KANDELO_TIER2_TRUSTED_ENV_KEYS = %w[
    HOMEBREW_KANDELO_ARCH HOMEBREW_KANDELO_LLVM_BIN HOMEBREW_KANDELO_NODE
    HOMEBREW_KANDELO_PRIMARY_TAP_ROOT HOMEBREW_KANDELO_ROOT
    HOMEBREW_KANDELO_SYSROOT KANDELO_HOMEBREW_ARCH
    KANDELO_HOMEBREW_KANDELO_ROOT LLVM_BIN WASM_POSIX_LLVM_DIR
    WASM_POSIX_SYSROOT
  ].freeze

  # The publisher writes one root-owned, read-only attestation at a fixed path
  # before Homebrew evaluates any Formula. Load it while this support module is
  # required, validate the exact target Formula and support bytes, and freeze
  # all authority before Formula class code can run. Ordinary pours do not have
  # this file and retain an inert nil authority.
  def self.kandelo_load_tier2_runtime!
    support_path = Pathname(__FILE__).realpath
    secure_read = lambda do |path, max_bytes, label, expected_uid: nil, expected_mode: nil|
      begin
        before = path.lstat
      rescue SystemCallError => e
        raise "#{label} is unavailable at #{path}: #{e.message}"
      end
      unless before.file? && !before.symlink? && before.nlink == 1
        raise "#{label} must be a regular non-symlink file with one link: #{path}"
      end
      if !expected_uid.nil? && before.uid != expected_uid
        raise "#{label} owner differs from its protected parent: #{path}"
      end
      if !expected_mode.nil? && (before.mode & 0777) != expected_mode
        raise "#{label} must have mode #{format("%04o", expected_mode)}: #{path}"
      end

      bytes = nil
      File.open(path, "rb") do |file|
        opened_before = file.stat
        identity = [before.dev, before.ino, before.size, before.nlink]
        opened_identity = [opened_before.dev, opened_before.ino, opened_before.size, opened_before.nlink]
        raise "#{label} changed before it was read: #{path}" unless opened_identity == identity

        bytes = file.read(max_bytes + 1)
        opened_after = file.stat
        after = path.lstat
        final_identity = [after.dev, after.ino, after.size, after.nlink]
        opened_final_identity = [opened_after.dev, opened_after.ino, opened_after.size, opened_after.nlink]
        unless final_identity == identity && opened_final_identity == identity
          raise "#{label} changed while it was read: #{path}"
        end
      end
      unless bytes&.bytesize&.between?(1, max_bytes)
        raise "#{label} must contain 1 to #{max_bytes} bytes: #{path}"
      end
      bytes.force_encoding(Encoding::UTF_8)
      raise "#{label} is not UTF-8: #{path}" unless bytes.valid_encoding?

      bytes
    end
    exact_directory = lambda do |path, label|
      expanded = path.expand_path.cleanpath
      unless path.absolute? && path == expanded
        raise "#{label} must be an absolute normalized path: #{path}"
      end
      begin
        stat = path.lstat
        resolved = path.realpath
      rescue SystemCallError => e
        raise "#{label} is unavailable at #{path}: #{e.message}"
      end
      unless stat.directory? && !stat.symlink? && resolved == path
        raise "#{label} must be a canonical real directory: #{path}"
      end
      [resolved, stat]
    end
    deep_freeze = nil
    deep_freeze = lambda do |value|
      case value
      when Hash
        value.each do |key, child|
          deep_freeze.call(key)
          deep_freeze.call(child)
        end
      when Array
        value.each { |child| deep_freeze.call(child) }
      end
      value.freeze
    end

    support_dir = support_path.dirname
    kandelo_dir = support_dir.dirname
    loaded_tap_root = kandelo_dir.dirname
    unless support_path.basename.to_s == "kandelo_formula_support.rb" &&
           support_dir.basename.to_s == "formula_support" &&
           kandelo_dir.basename.to_s == "Kandelo"
      raise "Kandelo Formula support has an unexpected path: #{support_path}"
    end
    [support_dir, kandelo_dir, loaded_tap_root].each do |directory|
      exact_directory.call(directory, "Kandelo Formula support ancestor")
    end
    support_source = secure_read.call(
      support_path, KANDELO_TIER2_SOURCE_MAX_BYTES, "Kandelo Formula support"
    )
    support_sha256 = Digest::SHA256.hexdigest(support_source)

    prefix_value = if defined?(HOMEBREW_PREFIX)
      HOMEBREW_PREFIX.to_s
    else
      ENV.fetch("HOMEBREW_PREFIX", "").to_s
    end
    attestation_path = if prefix_value.empty?
      nil
    else
      Pathname(prefix_value)/KANDELO_TIER2_ATTESTATION_BASENAME
    end
    trusted_env = KANDELO_TIER2_TRUSTED_ENV_KEYS.to_h do |key|
      value = ENV.fetch(key, nil)
      [key, value.nil? ? nil : value.to_s]
    end
    runtime = {
      "attestation" => nil,
      "attestation_path" => attestation_path&.to_s,
      "formula_path" => nil,
      "support_path" => support_path.to_s,
      "support_sha256" => support_sha256,
      "trusted_env" => trusted_env,
    }
    unless attestation_path && (attestation_path.exist? || attestation_path.symlink?)
      return deep_freeze.call(runtime)
    end

    prefix, prefix_stat = exact_directory.call(Pathname(prefix_value), "Homebrew prefix")
    unless attestation_path.parent == prefix && attestation_path.basename.to_s == KANDELO_TIER2_ATTESTATION_BASENAME
      raise "Tier-2 attestation path differs from the fixed Homebrew prefix child"
    end
    attestation_source = secure_read.call(
      attestation_path, KANDELO_TIER2_ATTESTATION_MAX_BYTES, "Tier-2 attestation",
      expected_uid: prefix_stat.uid, expected_mode: 0444
    )

    begin
      index = 0
      skip_whitespace = lambda do
        index += 1 while index < attestation_source.bytesize &&
                         [0x20, 0x09, 0x0a, 0x0d].include?(attestation_source.getbyte(index))
      end
      scan_string = lambda do
        raise JSON::ParserError, "expected JSON string" unless attestation_source.getbyte(index) == 0x22

        start = index
        index += 1
        loop do
          raise JSON::ParserError, "unterminated JSON string" if index >= attestation_source.bytesize

          byte = attestation_source.getbyte(index)
          index += 1
          if byte == 0x5c
            raise JSON::ParserError, "unterminated JSON escape" if index >= attestation_source.bytesize

            index += 1
          elsif byte == 0x22
            break
          end
        end
        attestation_source.byteslice(start, index - start)
      end
      scan_value = nil
      scan_value = lambda do
        skip_whitespace.call
        case attestation_source.getbyte(index)
        when 0x7b
          index += 1
          keys = {}
          skip_whitespace.call
          unless attestation_source.getbyte(index) == 0x7d
            loop do
              literal = scan_string.call
              key = JSON.parse(literal)
              raise JSON::ParserError, "duplicate JSON object key #{key.inspect}" if keys.key?(key)

              keys[key] = true
              skip_whitespace.call
              raise JSON::ParserError, "expected JSON object colon" unless attestation_source.getbyte(index) == 0x3a

              index += 1
              scan_value.call
              skip_whitespace.call
              separator = attestation_source.getbyte(index)
              if separator == 0x7d
                break
              end
              raise JSON::ParserError, "expected JSON object separator" unless separator == 0x2c

              index += 1
              skip_whitespace.call
            end
          end
          index += 1
        when 0x5b
          index += 1
          skip_whitespace.call
          unless attestation_source.getbyte(index) == 0x5d
            loop do
              scan_value.call
              skip_whitespace.call
              separator = attestation_source.getbyte(index)
              if separator == 0x5d
                break
              end
              raise JSON::ParserError, "expected JSON array separator" unless separator == 0x2c

              index += 1
            end
          end
          index += 1
        when 0x22
          scan_string.call
        else
          start = index
          index += 1 while index < attestation_source.bytesize &&
                           ![0x20, 0x09, 0x0a, 0x0d, 0x2c, 0x5d, 0x7d].include?(attestation_source.getbyte(index))
          raise JSON::ParserError, "missing JSON value" if index == start
        end
      end
      scan_value.call
      skip_whitespace.call
      raise JSON::ParserError, "trailing JSON content" unless index == attestation_source.bytesize

      document = JSON.parse(attestation_source, create_additions: false)
    rescue JSON::ParserError => e
      raise "Tier-2 attestation is invalid JSON: #{e.message}"
    end
    unless document.is_a?(Hash) && document.keys.sort == KANDELO_TIER2_TOP_KEYS
      raise "Tier-2 attestation must use the exact top-level schema"
    end
    unless document["schema"] == 1
      raise "Tier-2 attestation uses an unsupported schema"
    end
    tap_identity = document["tap"]
    formula = document["formula"]
    full_name = document["full_name"]
    arch = document["arch"]
    formula_sha256 = document["formula_sha256"]
    attested_support_sha256 = document["support_sha256"]
    bridge = document["tier2_bridge"]
    valid_sha256 = lambda do |value|
      value.is_a?(String) && value.match?(/\A[0-9a-f]{64}\z/)
    end
    unless tap_identity.is_a?(String) && tap_identity.match?(/\A[a-z0-9._-]+\/[a-z0-9._-]+\z/) &&
           formula.is_a?(String) && formula.match?(/\A[a-z0-9][a-z0-9._-]{0,254}\z/) &&
           full_name == "#{tap_identity}/#{formula}" && ["wasm32", "wasm64"].include?(arch) &&
           valid_sha256.call(formula_sha256) &&
           (attested_support_sha256.nil? || valid_sha256.call(attested_support_sha256))
      raise "Tier-2 attestation has an invalid target identity"
    end
    unless bridge.nil? || (bridge.is_a?(Hash) && bridge.keys.sort == KANDELO_TIER2_BRIDGE_KEYS)
      raise "Tier-2 attestation must use the exact bridge schema"
    end
    unless bridge.nil?
      script_env_keys = bridge["script_env_keys"]
      valid_bridge = bridge["package"].is_a?(String) &&
                     bridge["package"].match?(/\A[a-z0-9][a-z0-9._-]{0,254}\z/) &&
                     bridge["version"].is_a?(String) &&
                     bridge["version"].match?(/\A[A-Za-z0-9][A-Za-z0-9._+,-]{0,254}\z/) &&
                     bridge["script"].is_a?(String) &&
                     bridge["script"].match?(/\A[A-Za-z0-9][A-Za-z0-9._-]{0,254}\z/) &&
                     bridge["source_url"].is_a?(String) &&
                     bridge["source_url"].bytesize.between?(9, 2048) &&
                     bridge["source_url"].start_with?("https://") &&
                     ["exact", "in-repository-source"].include?(bridge["source_mode"]) &&
                     %w[
                       build_toml_sha256 package_toml_sha256 script_sha256 source_sha256
                     ].all? { |key| valid_sha256.call(bridge[key]) } &&
                     script_env_keys.is_a?(Array) &&
                     script_env_keys.all? do |key|
                       key.is_a?(String) && key.match?(/\A[A-Z][A-Z0-9_]{0,254}\z/)
                     end &&
                     script_env_keys == script_env_keys.sort.uniq &&
                     script_env_keys.length <= KANDELO_TIER2_SCRIPT_ENV_MAX_KEYS &&
                     script_env_keys.sum(&:bytesize) <= KANDELO_TIER2_SCRIPT_ENV_KEY_MAX_BYTES &&
                     valid_sha256.call(attested_support_sha256)
      raise "Tier-2 attestation has invalid bridge values" unless valid_bridge
    end

    primary_tap_root_value = trusted_env.fetch("HOMEBREW_KANDELO_PRIMARY_TAP_ROOT").to_s
    if primary_tap_root_value.empty?
      raise "Tier-2 publisher did not identify the selected primary tap root"
    end
    primary_tap_root, = exact_directory.call(
      Pathname(primary_tap_root_value), "selected primary tap root"
    )
    owner, short_tap = tap_identity.split("/", 2)
    unless primary_tap_root.basename.to_s == "homebrew-#{short_tap}" &&
           primary_tap_root.parent.basename.to_s == owner
      raise "Tier-2 attestation tap identity differs from the selected primary tap root"
    end
    if !attested_support_sha256.nil? && support_sha256 != attested_support_sha256
      raise "loaded Kandelo Formula support differs from the Tier-2 attestation"
    end
    formula_path = primary_tap_root/"Formula"/"#{formula}.rb"
    formula_source = secure_read.call(
      formula_path, KANDELO_TIER2_SOURCE_MAX_BYTES, "Tier-2 Formula"
    )
    unless Digest::SHA256.hexdigest(formula_source) == formula_sha256
      raise "loaded Formula differs from the Tier-2 attestation"
    end

    unless bridge.nil?
      primary_root = trusted_env.fetch("HOMEBREW_KANDELO_ROOT").to_s
      secondary_root = trusted_env.fetch("KANDELO_HOMEBREW_KANDELO_ROOT").to_s
      primary_arch = trusted_env.fetch("HOMEBREW_KANDELO_ARCH").to_s
      secondary_arch = trusted_env.fetch("KANDELO_HOMEBREW_ARCH").to_s
      # Homebrew intentionally re-execs `brew` with only its fixed allowlist
      # and HOMEBREW_* variables. The HOMEBREW_KANDELO_* values are therefore
      # the authoritative Formula-evaluation bridge. Older direct callers may
      # still provide the KANDELO_HOMEBREW_* aliases; accept their absence, but
      # fail closed if a present alias conflicts with the authoritative value.
      if primary_root.empty? || (!secondary_root.empty? && secondary_root != primary_root) ||
         primary_arch != arch || (!secondary_arch.empty? && secondary_arch != arch)
        raise "Tier-2 publisher root or architecture environment is inconsistent"
      end
      root, = exact_directory.call(Pathname(primary_root), "Kandelo root")
      sysroot_value = trusted_env.fetch("HOMEBREW_KANDELO_SYSROOT").to_s
      wasm_sysroot_value = trusted_env.fetch("WASM_POSIX_SYSROOT").to_s
      if !sysroot_value.empty? && !wasm_sysroot_value.empty? && sysroot_value != wasm_sysroot_value
        raise "Tier-2 publisher sysroot environment is inconsistent"
      end
      if sysroot_value.empty?
        sysroot_value = wasm_sysroot_value
      end
      if sysroot_value.empty?
        sysroot_value = (root/(arch == "wasm64" ? "sysroot64" : "sysroot")).to_s
      end
      sysroot, = exact_directory.call(Pathname(sysroot_value), "Kandelo sysroot")
      trusted_env["HOMEBREW_KANDELO_ROOT"] = root.to_s
      trusted_env["KANDELO_HOMEBREW_KANDELO_ROOT"] = root.to_s
      trusted_env["HOMEBREW_KANDELO_ARCH"] = arch
      trusted_env["KANDELO_HOMEBREW_ARCH"] = arch
      trusted_env["HOMEBREW_KANDELO_SYSROOT"] = sysroot.to_s
      trusted_env["WASM_POSIX_SYSROOT"] = sysroot.to_s
    end

    runtime["attestation"] = document
    runtime["attestation_path"] = attestation_path.realpath.to_s
    runtime["formula_path"] = formula_path.realpath.to_s
    deep_freeze.call(runtime)
  end

  KANDELO_TIER2_RUNTIME = kandelo_load_tier2_runtime!

  # Treat dependencies from both the canonical core tap and the Formula's own
  # tap as Kandelo target programs. During publication, the protected primary
  # tap root binds that identity independently of support-file load order.
  # Ordinary local Formula evaluation has no attestation, so use Homebrew's
  # fully qualified Formula identity instead.
  def kandelo_primary_tap_formula_prefix
    primary_tap = KANDELO_TIER2_RUNTIME.dig("attestation", "tap").to_s
    primary_tap = full_name.to_s.rpartition("/").first if primary_tap.empty?
    unless primary_tap.match?(/\A[a-z0-9._-]+\/[a-z0-9._-]+\z/)
      odie "Kandelo Formula support cannot resolve the primary tap identity"
    end

    "#{primary_tap}/"
  end

  def kandelo_target_formula?(formula_name)
    primary_tap_formula_prefix = kandelo_primary_tap_formula_prefix
    formula_name.start_with?(primary_tap_formula_prefix) ||
      formula_name.start_with?(KANDELO_CORE_TAP_FORMULA_PREFIX)
  end

  # Homebrew's formula_opt_* helpers discard the tap name and resolve through
  # HOMEBREW_PREFIX/opt. A native formula alias can therefore redirect a
  # Kandelo dependency to a host keg with the same short name. Resolve full tap
  # dependencies to their exact installed keg; Formulae still map those host
  # paths to stable guest opt paths in their compiler and runtime contracts.
  def formula_opt_prefix(formula_name)
    return Utils::Path.formula_opt_prefix(formula_name) unless kandelo_target_formula?(formula_name)

    kandelo_formula_prefix(formula_name)
  end

  def formula_opt_bin(formula_name)
    formula_opt_prefix(formula_name)/"bin"
  end

  def formula_opt_lib(formula_name)
    formula_opt_prefix(formula_name)/"lib"
  end

  def formula_opt_libexec(formula_name)
    formula_opt_prefix(formula_name)/"libexec"
  end

  def formula_opt_include(formula_name)
    formula_opt_prefix(formula_name)/"include"
  end

  def kandelo_formula_prefix(formula_name)
    formula = kandelo_formula(formula_name)
    prefix = formula.rack/formula.pkg_version.to_s
    odie "Kandelo dependency #{formula_name} is not installed at #{prefix}" unless prefix.directory?

    prefix
  end

  def kandelo_formula(formula_name)
    Formula[formula_name]
  end

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

  def kandelo_prepend_path!(path)
    if ENV.respond_to?(:prepend_path)
      ENV.prepend_path "PATH", path
      return
    end

    entries = ENV.fetch("PATH", "").split(File::PATH_SEPARATOR)
    ENV["PATH"] = [path.to_s, *entries.reject { |entry| entry == path.to_s }].join(File::PATH_SEPARATOR)
  end

  # Prepend the Kandelo SDK, Node, and LLVM to PATH, export the LLVM env the SDK
  # wrappers read, then remove global and target executable directories. The
  # isolation must run after activation because an explicit Node or LLVM bridge
  # can itself name Homebrew's global bin directory. Returns the resolved
  # Kandelo root. This is the single place SDK/toolchain activation happens.
  def kandelo_activate_sdk!
    root = kandelo_require_root!
    kandelo_prepend_path! "#{root}/sdk/bin"

    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      kandelo_prepend_path! File.dirname(node)
    end

    if (llvm_bin = ENV.fetch("HOMEBREW_KANDELO_LLVM_BIN", nil)).to_s != ""
      ENV["WASM_POSIX_LLVM_DIR"] = llvm_bin
      ENV["LLVM_BIN"] = llvm_bin
      kandelo_prepend_path! llvm_bin
    end

    target_dependencies = kandelo_target_runtime_dependencies
    kandelo_isolate_host_build_path!(target_dependencies)
    kandelo_export_target_pkg_config_path!(target_dependencies)
    root
  end

  # Export the wasm cross-compile sysroot/glue env and clear host compiler
  # search paths that would otherwise leak native headers or libraries into a
  # wasm compile. Used by `test do` blocks that compile against a library keg.
  def kandelo_activate_sysroot!(root = kandelo_require_root!)
    sysroot = (kandelo_arch == "wasm64") ? "sysroot64" : "sysroot"
    protected_sysroot = ENV.fetch("HOMEBREW_KANDELO_SYSROOT", "").to_s
    ENV["WASM_POSIX_SYSROOT"] = if protected_sysroot.empty?
      "#{root}/#{sysroot}"
    else
      protected_sysroot
    end
    ENV["WASM_POSIX_GLUE_DIR"] = "#{root}/libc/glue"
    %w[
      SDKROOT
      HOMEBREW_SDKROOT
      CPATH
      C_INCLUDE_PATH
      CPLUS_INCLUDE_PATH
      LD_RUN_PATH
      LIBRARY_PATH
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

  def kandelo_homebrew_prefix
    return Pathname(HOMEBREW_PREFIX.to_s) if defined?(HOMEBREW_PREFIX)

    value = ENV.fetch("HOMEBREW_PREFIX", nil)
    value.to_s.empty? ? nil : Pathname(value)
  end

  def kandelo_target_runtime_dependencies
    runtime_formula_dependencies(read_from_tab: false, undeclared: false).select do |dependency|
      kandelo_target_formula?(dependency.full_name)
    end
  end

  # Homebrew assumes every dependency executable runs on the build host and
  # adds every opt_bin to PATH. Its global bin directories can also contain
  # linked executables from unrelated Kandelo Formulae. Those executables are
  # target Wasm, so executing them during configure would cross the host/target
  # boundary. Keep native build-dependency opt paths while removing the global
  # prefix and declared target executable directories; Formulae still address
  # target headers and libraries through their explicit formula_opt_prefix
  # paths.
  def kandelo_isolate_host_build_path!(target_dependencies = kandelo_target_runtime_dependencies)
    target_paths = []
    if (homebrew_prefix = kandelo_homebrew_prefix)
      target_paths.push(homebrew_prefix/"bin", homebrew_prefix/"sbin")
    end
    target_paths.concat(target_dependencies.flat_map do |dependency|
      [dependency.opt_bin, dependency.opt_sbin, dependency.opt_libexec/"bin"]
    end)
    target_paths.map! { |path| File.expand_path(path.to_s) }

    return if target_paths.empty?

    entries = ENV.fetch("PATH", "").split(File::PATH_SEPARATOR)
    ENV["PATH"] = entries.reject do |entry|
      !entry.empty? && target_paths.include?(File.expand_path(entry))
    end.join(File::PATH_SEPARATOR)
  end

  # Declare the exact pkg-config directories owned by the installed Kandelo
  # runtime dependency closure. The SDK intersects this authorization set with
  # PKG_CONFIG_PATH, which remains Formula-owned search selection. Resolve full
  # tap identities through the versioned Cellar keg and replace any ambient
  # declaration so native, undeclared, global, or mutable opt paths cannot leak.
  def kandelo_export_target_pkg_config_path!(target_dependencies = kandelo_target_runtime_dependencies)
    formula_names = target_dependencies.map(&:full_name).uniq.sort
    pkg_config_paths = formula_names.flat_map do |formula_name|
      keg = kandelo_formula_prefix(formula_name)
      [keg/"lib/pkgconfig", keg/"share/pkgconfig"]
    end
    existing_paths = pkg_config_paths.select(&:directory?)
    normalized_paths = existing_paths.map { |path| File.expand_path(path.to_s) }.uniq.sort

    ENV["WASM_POSIX_DEP_PKG_CONFIG_PATH"] = normalized_paths.join(File::PATH_SEPARATOR)
  end

  # Establish a clean cross-build environment for an idiomatic Formula
  # install block, then restore Homebrew's environment when the block exits.
  def kandelo_wasm_build
    saved = ENV.to_hash
    root = kandelo_activate_sdk!
    kandelo_activate_sysroot!(root)

    # CMake treats its ambient search paths as program roots, so Homebrew's
    # injected prefix can bypass PATH isolation and select a linked target tool.
    %w[
      CFLAGS
      CMAKE_APPBUNDLE_PATH
      CMAKE_FRAMEWORK_PATH
      CMAKE_INCLUDE_PATH
      CMAKE_LIBRARY_PATH
      CMAKE_PREFIX_PATH
      CMAKE_PROGRAM_PATH
      CPPFLAGS
      CXXFLAGS
      LDFLAGS
      MACOSX_DEPLOYMENT_TARGET
    ].each { |key| ENV.delete(key) }
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
    instrumented.chmod(wasm.stat.mode & 07777)
    instrumented.rename(wasm)
    wasm
  ensure
    instrumented&.delete if instrumented&.exist?
  end

  # Reject a final linked artifact unless its ABI and continuation surface
  # match the Kandelo checkout that is building it. Callers must declare WABT
  # and Binaryen as build dependencies because the authoritative guards inspect
  # Wasm sections with wasm-objdump and use Binaryen for fallback extraction.
  def kandelo_validate_wasm_artifact(wasm_path, fork: :auto, forbidden_paths: [])
    unless [:auto, :required, :forbidden].include?(fork)
      odie "invalid Kandelo fork policy #{fork.inspect}; expected :auto, :required, or :forbidden"
    end

    root = kandelo_require_root!
    wasm = Pathname(wasm_path)
    artifact_guards = Pathname(root)/"scripts/wasm-artifact-guards.sh"
    odie "Kandelo artifact guards not found at #{artifact_guards}" unless artifact_guards.file?

    fork_guard = case fork
    when :required
      <<~SH
        if ! wasm_imports_kernel_fork "$artifact"; then
          echo "ERROR: required fork-capable artifact does not import kernel.kernel_fork: $artifact" >&2
          exit 1
        fi
        if ! wasm_has_complete_fork_instrumentation "$artifact"; then
          echo "ERROR: required fork-capable artifact has incomplete instrumentation: $artifact" >&2
          exit 1
        fi
      SH
    when :forbidden
      <<~SH
        if wasm_imports_kernel_fork "$artifact"; then
          echo "ERROR: fork-free artifact imports kernel.kernel_fork: $artifact" >&2
          exit 1
        fi
        wasm_require_no_fork_instrumentation "$artifact"
      SH
    else
      <<~SH
        if wasm_imports_kernel_fork "$artifact"; then
          if ! wasm_has_complete_fork_instrumentation "$artifact"; then
            echo "ERROR: fork-capable artifact has incomplete instrumentation: $artifact" >&2
            exit 1
          fi
        else
          wasm_require_no_fork_instrumentation "$artifact"
        fi
      SH
    end

    system "bash", "-c", <<~SH
      set -euo pipefail
      for tool in wasm-objdump wasm-dis wasm-opt; do
        if ! command -v "$tool" >/dev/null 2>&1; then
          echo "ERROR: required Kandelo artifact inspection tool is unavailable: $tool" >&2
          exit 1
        fi
      done
      . #{artifact_guards.to_s.shellescape}
      artifact=#{wasm.to_s.shellescape}
      if ! wasm-objdump -x "$artifact" >/dev/null 2>&1; then
        echo "ERROR: wasm-objdump could not inspect artifact imports and exports: $artifact" >&2
        exit 1
      fi
      expected_abi=$(wasm_current_abi_version #{root.to_s.shellescape} || true)
      artifact_abi=$(wasm_extract_abi_version "$artifact" || true)
      if [ -z "$expected_abi" ] || [ -z "$artifact_abi" ] || [ "$artifact_abi" != "$expected_abi" ]; then
        echo "ERROR: artifact ABI ${artifact_abi:-missing} does not match Kandelo ABI ${expected_abi:-missing}: $artifact" >&2
        exit 1
      fi
      wasm_require_no_legacy_asyncify "$artifact"
      #{fork_guard}
    SH

    contents = wasm.binread
    staging_paths = [buildpath, root]
    staging_paths << prefix if respond_to?(:prefix)
    staging_paths.concat(forbidden_paths)
    staging_paths.compact.map(&:to_s).reject(&:empty?).uniq.each do |path|
      odie "Wasm artifact embeds staging path #{path}: #{wasm}" if contents.include?(path)
    end
    if contents.match?(%r{/(?:private/tmp/|Users/|home/runner/(?:_work|work)/|nix/store/)})
      odie "Wasm artifact embeds a host workspace path: #{wasm}"
    end

    wasm
  end

  def kandelo_tier2_runtime!
    runtime = KANDELO_TIER2_RUNTIME
    bridge = runtime.dig("attestation", "tier2_bridge")
    odie "Kandelo Tier-2 source builds require a valid publisher attestation" if bridge.nil?

    runtime
  end

  def kandelo_tier2_read_attested_file(path, expected_sha256, max_bytes, label)
    begin
      before = path.lstat
    rescue SystemCallError => e
      odie "#{label} is unavailable at #{path}: #{e.message}"
    end
    unless before.file? && !before.symlink? && before.nlink == 1
      odie "#{label} must be a regular non-symlink file with one link: #{path}"
    end

    bytes = nil
    File.open(path, "rb") do |file|
      opened_before = file.stat
      identity = [before.dev, before.ino, before.size, before.nlink]
      opened_identity = [opened_before.dev, opened_before.ino, opened_before.size, opened_before.nlink]
      odie "#{label} changed before it was read: #{path}" unless opened_identity == identity

      bytes = file.read(max_bytes + 1)
      opened_after = file.stat
      after = path.lstat
      final_identity = [after.dev, after.ino, after.size, after.nlink]
      opened_final_identity = [opened_after.dev, opened_after.ino, opened_after.size, opened_after.nlink]
      unless final_identity == identity && opened_final_identity == identity
        odie "#{label} changed while it was read: #{path}"
      end
    end
    unless bytes&.bytesize&.between?(1, max_bytes)
      odie "#{label} must contain 1 to #{max_bytes} bytes: #{path}"
    end
    bytes.force_encoding(Encoding::UTF_8)
    odie "#{label} is not UTF-8: #{path}" unless bytes.valid_encoding?
    unless Digest::SHA256.hexdigest(bytes) == expected_sha256
      odie "#{label} differs from the publisher attestation: #{path}"
    end

    bytes
  end

  def kandelo_tier2_exact_directory(path, parent, label)
    begin
      stat = path.lstat
      resolved = path.realpath
    rescue SystemCallError => e
      odie "#{label} is unavailable at #{path}: #{e.message}"
    end
    unless path.absolute? && path == path.expand_path.cleanpath &&
           stat.directory? && !stat.symlink? && resolved == path && resolved.parent == parent
      odie "#{label} must be one canonical real child of #{parent}: #{path}"
    end
    resolved
  end

  def kandelo_tier2_script_env(bridge, script_env)
    odie "Kandelo Tier-2 script_env must be a Hash" unless script_env.instance_of?(Hash)

    package = bridge.fetch("package")
    package_prefix = "#{package.upcase.gsub(/[^A-Z0-9]/, "_")}_"
    values = {}
    script_env.each do |key, value|
      unless key.is_a?(String) && key.match?(/\A[A-Z][A-Z0-9_]{0,254}\z/)
        odie "Kandelo Tier-2 script_env has an invalid key: #{key.inspect}"
      end
      unless key.start_with?("WASM_POSIX_DEP_") || key.start_with?(package_prefix)
        odie "Kandelo Tier-2 script_env key is outside the approved namespace: #{key.inspect}"
      end
      if %w[
        WASM_POSIX_DEP_NAME WASM_POSIX_DEP_OUT_DIR WASM_POSIX_DEP_SOURCE_DIR
        WASM_POSIX_DEP_SOURCE_SHA256 WASM_POSIX_DEP_SOURCE_URL
        WASM_POSIX_DEP_TARGET_ARCH WASM_POSIX_DEP_VERSION WASM_POSIX_DEP_WORK_DIR
        WASM_POSIX_INSTALL_LOCAL_MIRROR
      ].include?(key)
        odie "Kandelo Tier-2 script_env overrides a helper-owned key: #{key.inspect}"
      end
      unless value.is_a?(String) || value.is_a?(Pathname)
        odie "Kandelo Tier-2 script_env value must be a String or Pathname: #{key.inspect}"
      end
      converted = value.to_s.dup
      converted.force_encoding(Encoding::UTF_8)
      unless converted.valid_encoding? && !converted.include?("\0") &&
             converted.bytesize <= KANDELO_TIER2_SCRIPT_ENV_VALUE_MAX_BYTES
        odie "Kandelo Tier-2 script_env value is invalid or oversized: #{key.inspect}"
      end
      values[key.dup.freeze] = converted.freeze
    end
    keys = values.keys.sort
    unless keys == bridge.fetch("script_env_keys") &&
           keys.length <= KANDELO_TIER2_SCRIPT_ENV_MAX_KEYS &&
           keys.sum(&:bytesize) <= KANDELO_TIER2_SCRIPT_ENV_KEY_MAX_BYTES &&
           values.values.sum(&:bytesize) <= KANDELO_TIER2_SCRIPT_ENV_VALUE_TOTAL_BYTES
      odie "Kandelo Tier-2 script_env differs from the publisher attestation"
    end
    values.freeze
  end

  def kandelo_tier2_restore_environment!(runtime, package)
    package_prefix = "#{package.upcase.gsub(/[^A-Z0-9]/, "_")}_"
    explicit = %w[
      HOMEBREW_KANDELO_ARCH HOMEBREW_KANDELO_PRIMARY_TAP_ROOT
      HOMEBREW_KANDELO_ROOT HOMEBREW_KANDELO_SYSROOT
      KANDELO_HOMEBREW_ARCH KANDELO_HOMEBREW_KANDELO_ROOT
      WASM_POSIX_BINARY_INDEX_URL WASM_POSIX_DEFAULT_ARCH
      WASM_POSIX_INSTALL_LOCAL_MIRROR WASM_POSIX_SYSROOT
    ]
    ENV.keys.each do |key|
      ENV.delete(key) if key.start_with?("WASM_POSIX_DEP_") ||
                         key.start_with?(package_prefix) || explicit.include?(key)
    end
    runtime.fetch("trusted_env").each do |key, value|
      value.nil? ? ENV.delete(key) : ENV[key] = value
    end
  end

  # Move Homebrew's checksum-verified primary source into a fixed root. A
  # Formula may stage separately attested resource inputs under the one fixed
  # resource directory; those inputs are deliberately not moved with the
  # primary source.
  def kandelo_stage_verified_formula_source
    source_dir = buildpath/"kandelo-package-source"
    resource_dir = buildpath/"kandelo-package-resources"
    reserved = [
      source_dir,
      buildpath/"kandelo-package-out",
      buildpath/"kandelo-package-work",
    ]
    if source_dir.exist? || source_dir.symlink?
      odie "Kandelo Formula source was already staged at #{source_dir}"
    end
    reserved.drop(1).each do |path|
      odie "Kandelo Formula build root already exists: #{path}" if path.exist? || path.symlink?
    end
    if resource_dir.exist? || resource_dir.symlink?
      begin
        resource_stat = resource_dir.lstat
      rescue SystemCallError => e
        odie "Kandelo Formula resource root is unavailable: #{e.message}"
      end
      unless resource_stat.directory? && !resource_stat.symlink? && resource_dir.realpath == resource_dir
        odie "Kandelo Formula resource root must be a canonical real directory: #{resource_dir}"
      end
    end

    source_entries = buildpath.children.reject { |entry| entry == resource_dir }
    odie "Homebrew did not stage Formula source under #{buildpath}" if source_entries.empty?

    source_dir.mkdir
    source_entries.each do |entry|
      FileUtils.mv(entry, source_dir/entry.basename)
    end
    source_dir
  end

  # Transitional Tier-2 bridge (spec §6 deviation). Every source, identity,
  # registry, environment, and execution input is bound to the publisher's
  # frozen attestation before the SDK is activated or a build script runs.
  def kandelo_build_package(package: nil, script_env: {})
    runtime = kandelo_tier2_runtime!
    attestation = runtime.fetch("attestation")
    bridge = attestation.fetch("tier2_bridge")
    attested_package = bridge.fetch("package")
    requested_package = package.nil? ? name.to_s : package

    formula_name = name.to_s
    formula_full_name = respond_to?(:full_name) ? full_name.to_s : "kandelo-dev/tap-core/#{formula_name}"
    formula_version = version.to_s
    formula_url = stable.url.to_s
    formula_sha256 = stable.checksum.hexdigest
    unless formula_name == attestation.fetch("formula") &&
           formula_full_name == attestation.fetch("full_name") &&
           formula_version == bridge.fetch("version") &&
           formula_url == bridge.fetch("source_url") &&
           formula_sha256 == bridge.fetch("source_sha256")
      odie "Kandelo Tier-2 Formula identity differs from the publisher attestation"
    end
    unless requested_package.is_a?(String) && requested_package == attested_package
      odie "Kandelo Tier-2 registry package differs from the publisher attestation"
    end
    formula_env = kandelo_tier2_script_env(bridge, script_env)
    package = attested_package

    formula_path = Pathname(path).realpath
    support_path = Pathname(runtime.fetch("support_path"))
    unless formula_path.to_s == runtime.fetch("formula_path")
      odie "Kandelo Tier-2 Formula path differs from the publisher attestation"
    end
    kandelo_tier2_read_attested_file(
      formula_path, attestation.fetch("formula_sha256"), KANDELO_TIER2_SOURCE_MAX_BYTES,
      "Tier-2 Formula"
    )
    kandelo_tier2_read_attested_file(
      support_path, attestation.fetch("support_sha256"), KANDELO_TIER2_SOURCE_MAX_BYTES,
      "Kandelo Formula support"
    )

    trusted_env = runtime.fetch("trusted_env")
    root = Pathname(trusted_env.fetch("HOMEBREW_KANDELO_ROOT"))
    arch = trusted_env.fetch("HOMEBREW_KANDELO_ARCH")
    unless arch == attestation.fetch("arch")
      odie "Kandelo Tier-2 architecture differs from the publisher attestation"
    end
    packages_root = kandelo_tier2_exact_directory(root/"packages", root, "Kandelo packages root")
    registry_root = kandelo_tier2_exact_directory(
      packages_root/"registry", packages_root, "Kandelo registry root"
    )
    package_root = kandelo_tier2_exact_directory(
      registry_root/package, registry_root, "Tier-2 registry package"
    )
    package_toml = package_root/"package.toml"
    build_toml = package_root/"build.toml"
    script = package_root/bridge.fetch("script")
    kandelo_tier2_read_attested_file(
      package_toml, bridge.fetch("package_toml_sha256"), 65_536, "registry package.toml"
    )
    kandelo_tier2_read_attested_file(
      build_toml, bridge.fetch("build_toml_sha256"), 65_536, "registry build.toml"
    )
    kandelo_tier2_read_attested_file(
      script, bridge.fetch("script_sha256"), KANDELO_TIER2_SOURCE_MAX_BYTES,
      "registry build script"
    )

    source_dir = kandelo_stage_verified_formula_source
    work_dir = buildpath/"kandelo-package-work"
    out_dir = buildpath/"kandelo-package-out"
    work_dir.mkdir
    out_dir.mkdir
    helper_env = {
      "WASM_POSIX_DEP_NAME"                 => package,
      "WASM_POSIX_DEP_OUT_DIR"              => out_dir,
      "WASM_POSIX_DEP_SOURCE_DIR"           => source_dir,
      "WASM_POSIX_DEP_SOURCE_SHA256"        => bridge.fetch("source_sha256"),
      "WASM_POSIX_DEP_SOURCE_URL"           => bridge.fetch("source_url"),
      "WASM_POSIX_DEP_TARGET_ARCH"          => arch,
      "WASM_POSIX_DEP_VERSION"              => bridge.fetch("version"),
      "WASM_POSIX_DEP_WORK_DIR"             => work_dir,
      "WASM_POSIX_INSTALL_LOCAL_MIRROR"     => "0",
    }
    kandelo_tier2_restore_environment!(runtime, package)
    activated_root = kandelo_activate_sdk!
    unless Pathname(activated_root).realpath == root
      odie "Kandelo SDK activation changed the attested root"
    end
    kandelo_activate_sysroot!(activated_root)
    formula_env.each { |key, value| ENV[key] = value }
    helper_env.each { |key, value| ENV[key] = value.to_s }

    # Re-read the script immediately before the only process execution.
    kandelo_tier2_read_attested_file(
      script, bridge.fetch("script_sha256"), KANDELO_TIER2_SOURCE_MAX_BYTES,
      "registry build script"
    )
    system "/usr/bin/bash", script.to_s

    [source_dir, work_dir, out_dir].each do |directory|
      kandelo_tier2_exact_directory(directory, buildpath.realpath, "Tier-2 build root")
    end
    out_dir
  end

  # Install a built `.wasm` from an out dir as an executable `bin/<bin_name>`.
  def kandelo_install_bin(out_dir, wasm_name, bin_name)
    wasm = Pathname(out_dir)/wasm_name
    chmod 0755, wasm
    bin.install wasm => bin_name
    chmod 0755, bin/bin_name
  end

  def kandelo_run_texlive_pdftex(*arguments)
    runner = Pathname(__dir__)/"build-texlive-pdftex.sh"
    command = [
      kandelo_host_tool("bash"), runner
    ].map { |arg| Shellwords.escape(arg.to_s) }.join(" ")
    command << " #{arguments.map { |arg| Shellwords.escape(arg.to_s) }.join(" ")}"
    system kandelo_host_tool("bash"), "-c", command
  end

  def kandelo_generate_texlive_runtime_config(module_root, *arguments)
    runner = Pathname(__dir__)/"generate-texlive-runtime-config.pl"
    command = [
      kandelo_host_tool("perl"), "-I#{module_root}", runner
    ].map { |arg| Shellwords.escape(arg.to_s) }.join(" ")
    command << " #{arguments.map { |arg| Shellwords.escape(arg.to_s) }.join(" ")}"
    system kandelo_host_tool("bash"), "-c", command
  end

  # Run a built `.wasm` under the Node kernel host and return its stdout. The
  # guest inherits the passed `env:`, matching how a real `brew test` exercises
  # behavior. `network: true` opts into Node's real external-TCP backend, while
  # `preserve_argv0: true` keeps multicall command names such as gunzip,
  # `argv0:` supplies an explicit staged guest executable path,
  # `exec_programs:` stages explicit guest exec targets, `guest_files:` stages
  # ordinary files in the guest VFS, `writable_host_directories:` exposes
  # explicit host directories as writable guest mounts for output validation,
  # `expected_fork_descendants:` requires exactly that many fork descendants to
  # exit successfully. `expected_fork_descendant_statuses:` instead requires an
  # exact multiset of descendant exit statuses for service teardown paths where
  # a signal exit is intentional. `merge_stderr: true` returns guest fd 1 and fd
  # 2 in callback order without merging host-runtime diagnostics.
  # `expected_status:` permits tests for specified nonzero results such as a grep
  # no-match status.
  def kandelo_run_wasm(
    bin_path, argv, env: {}, stdin: nil, merge_stderr: false, network: false,
    preserve_argv0: false, argv0: nil, exec_programs: {}, guest_files: {},
    writable_host_directories: {}, expected_fork_descendants: 0,
    expected_fork_descendant_statuses: nil, expected_status: 0
  )
    root = kandelo_require_root!
    kandelo_validate_guest_argv0!(argv0)
    valid_descendant_count = expected_fork_descendants.is_a?(Integer) && expected_fork_descendants >= 0
    odie "expected fork descendant count must be a nonnegative integer" unless valid_descendant_count
    unless expected_fork_descendant_statuses.nil?
      valid_statuses = expected_fork_descendant_statuses.is_a?(Array) &&
                       expected_fork_descendant_statuses.any? &&
                       expected_fork_descendant_statuses.all? do |status|
                         status.is_a?(Integer) && status.between?(0, 255)
                       end
      odie "expected fork descendant statuses must be a nonempty array of byte integers" unless valid_statuses
      odie "expected fork descendant count and statuses cannot both be set" if expected_fork_descendants.positive?
    end
    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      ENV.prepend_path "PATH", File.dirname(node)
    end

    # Compiled host output shadows TypeScript source under tsx. Formula tests
    # must exercise the checkout supplied by HOMEBREW_KANDELO_ROOT.
    FileUtils.rm_rf(Pathname(root)/"host/dist")

    wasm_path = Pathname(bin_path)
    if wasm_path.extname != ".wasm"
      staged_name = preserve_argv0 ? wasm_path.basename : "#{wasm_path.basename}.wasm"
      staged_wasm = testpath/staged_name
      File.binwrite(staged_wasm, File.binread(wasm_path))
      wasm_path = staged_wasm
    end
    guest_output_path = merge_stderr ? testpath/".#{wasm_path.basename}.guest-output" : nil
    FileUtils.rm_f(guest_output_path) if guest_output_path

    command = +"cd "
    command << Shellwords.escape(root) << " && "
    isolated_runner = network || preserve_argv0 || !argv0.nil? || exec_programs.any? ||
                      guest_files.any? || writable_host_directories.any? ||
                      expected_fork_descendants.positive? || !expected_fork_descendant_statuses.nil?
    if isolated_runner
      guest_env = JSON.generate(env.transform_values(&:to_s))
      guest_exec_programs = JSON.generate(exec_programs.transform_values(&:to_s))
      guest_files_manifest = if guest_files.any?
        # Guest runtimes such as Vim contain thousands of files. Keep that map
        # out of the process environment so host ARG_MAX never limits valid VFS
        # staging. The manifest lives in Homebrew's ephemeral Formula testpath;
        # the runner still validates every guest path and host file as before.
        manifest = testpath/".#{wasm_path.basename}.guest-files.json"
        File.binwrite(manifest, JSON.generate(guest_files.transform_values(&:to_s)))
        manifest
      end
      writable_mounts = JSON.generate(writable_host_directories.transform_values(&:to_s))
      command << "KANDELO_FORMULA_GUEST_ENV_JSON=#{Shellwords.escape(guest_env)} "
      command << "KANDELO_FORMULA_EXEC_PROGRAMS_JSON=#{Shellwords.escape(guest_exec_programs)} "
      if guest_files_manifest
        command << "KANDELO_FORMULA_GUEST_FILES_MANIFEST=#{Shellwords.escape(guest_files_manifest.to_s)} "
      end
      command << "KANDELO_FORMULA_WRITABLE_HOST_DIRS_JSON=#{Shellwords.escape(writable_mounts)} "
      command << "KANDELO_FORMULA_ARGV0=#{Shellwords.escape(argv0.to_s)} " if argv0
      command << "KANDELO_FORMULA_ENABLE_NETWORK=#{network ? 1 : 0} "
      if expected_fork_descendants.positive?
        command << "KANDELO_FORMULA_EXPECTED_FORK_DESCENDANTS=#{expected_fork_descendants} "
      end
      unless expected_fork_descendant_statuses.nil?
        statuses = JSON.generate(expected_fork_descendant_statuses)
        command << "KANDELO_FORMULA_EXPECTED_FORK_DESCENDANT_STATUSES_JSON=#{Shellwords.escape(statuses)} "
      end
    else
      env.each { |key, value| command << "#{key}=#{Shellwords.escape(value.to_s)} " }
    end
    command << "KANDELO_GUEST_OUTPUT_FILE=#{Shellwords.escape(guest_output_path.to_s)} " if guest_output_path
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

    status_matched = false
    begin
      output = shell_output(command, expected_status)
      status_matched = true
      if guest_output_path
        # A configured runner writes all guest bytes to the sink. Anything it
        # writes to process stdout is host-side output and remains observable
        # on the embedding process's diagnostic stream.
        $stderr.write(output) unless output.empty?
        odie "guest output sink was not created: #{guest_output_path}" unless guest_output_path.file?
        output = guest_output_path.binread
      end
    ensure
      if guest_output_path
        $stderr.write(guest_output_path.binread) if !status_matched && guest_output_path.file?
        FileUtils.rm_f(guest_output_path)
      end
    end
    kandelo_record_node_execution!(wasm_path, argv)
    output
  end

  # Start a long-running Wasm service under NodeKernelHost, issue in-kernel
  # HTTP requests, and return the decoded response records. This exercises the
  # same forked server and kernel TCP path used by browser-hosted services while
  # keeping Formula tests independent of host TCP ports.
  def kandelo_run_http_service(
    bin_path, argv, port:, requests:, mounts: {}, env: {}, uid: nil, gid: nil, timeout: 30
  )
    valid_port = port.is_a?(Integer) && port.between?(1, 65_535)
    valid_requests = requests.is_a?(Array) && requests.any?
    valid_timeout = timeout.is_a?(Numeric) && timeout.positive?
    odie "HTTP service port must be an integer from 1 through 65535" unless valid_port
    odie "HTTP service requests must be a nonempty array" unless valid_requests
    odie "HTTP service timeout must be a positive number" unless valid_timeout

    root = kandelo_require_root!
    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      ENV.prepend_path "PATH", File.dirname(node)
    end

    # Compiled host output shadows TypeScript source under tsx. Service tests
    # must exercise the checkout supplied by HOMEBREW_KANDELO_ROOT.
    FileUtils.rm_rf(Pathname(root)/"host/dist")

    wasm_path = Pathname(bin_path)
    if wasm_path.extname != ".wasm"
      staged_wasm = testpath/"#{wasm_path.basename}.service.wasm"
      File.binwrite(staged_wasm, File.binread(wasm_path))
      wasm_path = staged_wasm
    end

    spec = JSON.generate({ port:, requests:, mounts:, uid:, gid:, timeout_ms: timeout * 1000 })
    guest_env = JSON.generate(env.transform_values(&:to_s))
    runner = Pathname(__dir__)/"run-http-service-wasm.ts"
    command = "cd #{Shellwords.escape(root)} && "
    command << "KANDELO_FORMULA_HTTP_SERVICE_JSON=#{Shellwords.escape(spec)} "
    command << "KANDELO_FORMULA_GUEST_ENV_JSON=#{Shellwords.escape(guest_env)} "
    command << "node --experimental-wasm-exnref --import tsx/esm "
    command << "#{Shellwords.escape(runner.to_s)} #{Shellwords.escape(root)} "
    command << Shellwords.escape(wasm_path.to_s)
    argv.each { |arg| command << " #{Shellwords.escape(arg.to_s)}" }
    command << " < /dev/null"

    output = shell_output(command)
    kandelo_record_node_execution!(wasm_path, argv, launcher: "kandelo_run_http_service")
    JSON.parse(output)
  end

  # Run an interactive Wasm program through Kandelo's real PTY path. Inputs
  # are written in order after the process starts, with short delays so curses
  # applications can render and transition between prompts. `exec_programs:`
  # stages explicit guest exec targets. Writable guest directories use
  # isolated mounts that survive every spawn in this run, while
  # `writable_host_directories:` exposes caller-owned output directories.
  # `expected_fork_descendants:` requires exactly that many fork descendants to
  # exit successfully before each PTY run is considered complete. `timeout_ms:`
  # sets a bounded host-side deadline without leaking runner policy into the
  # guest environment. `completion_output:` ends an intentionally long-lived
  # process only after observing the required literal on its real output.
  def kandelo_run_pty_wasm(
    bin_path, argv, inputs:, argv0: nil, env: {}, exec_programs: {}, guest_files: {},
    guest_directories: [], writable_guest_directories: [], writable_host_directories: {},
    input_ready_text: nil, rerun_inputs: nil, expected_fork_descendants: 0, expected_status: 0,
    initial_delay_ms: 500, input_delay_ms: 180, cols: 100, rows: 30, timeout_ms: nil,
    completion_output: nil
  )
    root = kandelo_require_root!
    kandelo_validate_guest_argv0!(argv0)
    valid_descendant_count = expected_fork_descendants.is_a?(Integer) && expected_fork_descendants >= 0
    odie "expected fork descendant count must be a nonnegative integer" unless valid_descendant_count
    valid_ready_text = input_ready_text.nil? ||
                       (input_ready_text.is_a?(String) && !input_ready_text.empty? &&
                        input_ready_text.bytesize <= 4 * 1024)
    odie "input readiness text must be a nonempty string no larger than 4096 bytes" unless valid_ready_text
    valid_timeout = timeout_ms.nil? || (timeout_ms.is_a?(Integer) && timeout_ms.positive?)
    odie "PTY timeout must be a positive integer number of milliseconds" unless valid_timeout
    valid_completion_output = completion_output.nil? ||
                              (completion_output.is_a?(String) && !completion_output.empty? &&
                               completion_output.bytesize <= 4096 && completion_output.index("\0").nil?)
    unless valid_completion_output
      odie "PTY completion output must be a nonempty string of at most 4096 bytes without NUL"
    end
    odie "PTY completion output requires expected status zero" if completion_output && expected_status != 0
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
      argv0:                    argv0,
      env:                      env,
      inputs:                   inputs,
      inputReadyText:           input_ready_text,
      rerunInputs:              rerun_inputs,
      execPrograms:             exec_programs.transform_values(&:to_s),
      guestFiles:               guest_files.transform_values(&:to_s),
      guestDirectories:         guest_directories.map(&:to_s),
      writableGuestDirectories: writable_guest_directories.map(&:to_s),
      writableHostDirectories:  writable_host_directories.transform_values(&:to_s),
      initialDelayMs:           initial_delay_ms,
      inputDelayMs:             input_delay_ms,
      cols:                     cols,
      rows:                     rows,
      timeoutMs:                timeout_ms,
      completionOutput:         completion_output,
      expectedForkDescendants:  expected_fork_descendants,
    })
    # Compiled host output shadows TypeScript source under tsx. PTY formula
    # tests must exercise the checkout supplied by HOMEBREW_KANDELO_ROOT.
    FileUtils.rm_rf(Pathname(root)/"host/dist")

    runner = Pathname(__dir__)/"run-pty-wasm.ts"
    command = +"node --experimental-wasm-exnref --import tsx/esm "
    command << "#{Shellwords.escape(runner.to_s)} #{Shellwords.escape(root)} "
    command << Shellwords.escape(wasm_path.to_s)
    argv.each { |arg| command << " #{Shellwords.escape(arg.to_s)}" }
    command << " 2>&1"

    # Editor runtimes can contribute thousands of mapped files. Keep that
    # bounded data out of argv and the process environment so the host's
    # ARG_MAX limit cannot prevent Node from starting. Only the small path to
    # a mode-0600 temporary file crosses the process boundary.
    config_file = Tempfile.new(["kandelo-pty-config-", ".json"], testpath.to_s)
    begin
      config_file.chmod(0600)
      config_file.binmode
      config_file.write(config)
      config_file.flush

      invocation = "cd #{Shellwords.escape(root)} && "
      invocation << "KANDELO_FORMULA_PTY_CONFIG_PATH=#{Shellwords.escape(config_file.path)} "
      invocation << command

      output = shell_output(invocation, expected_status)
    ensure
      config_file.close!
    end
    kandelo_record_node_execution!(wasm_path, argv, launcher: "kandelo_run_pty_wasm")
    output
  end

  def kandelo_validate_guest_argv0!(argv0)
    return if argv0.nil?

    invalid = argv0.empty? || !argv0.start_with?("/") || argv0.include?("\0") ||
              Pathname(argv0).cleanpath.to_s != argv0
    odie "guest argv0 must be a nonempty normalized absolute path: #{argv0.inspect}" if invalid
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
      runner, root, wasm_path, JSON.generate(argv.map(&:to_s)), min_page_flips, timeout_ms
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
      runner, root, Pathname(bin_path), config
    ].map { |arg| Shellwords.escape(arg.to_s) }.join(" ")

    shell_output("cd #{Shellwords.escape(root)} && #{command} < /dev/null")
  end

  # Run a formula executable through Kandelo's Chromium browser host. This is
  # intentionally separate from the Node runner: browser worker startup,
  # SharedArrayBuffer isolation, Wasm memory, and process teardown are distinct
  # platform contracts. `argv0:` controls the guest command name for multicall
  # runtimes whose behavior depends on argv[0]. `guest_program_path:` stages
  # the primary executable at an installed absolute guest path when runtime
  # prefix discovery depends on that path. `exec_programs:` stages executable
  # Wasm programs for spawn/exec behavior, while immutable `guest_files:` use
  # the same absolute-path and bounded-rootfs contract as Node formula tests.
  # `expected_status:` and `merge_stderr:` permit exact negative-path checks
  # without converting a guest failure into a browser-runner failure.
  def kandelo_run_browser_wasm(
    bin_path, argv, argv0: nil, guest_program_path: nil, env: {}, exec_programs: {}, guest_files: {},
    timeout_ms: 120_000, allow_stderr: false, merge_stderr: false, expected_status: 0
  )
    root = kandelo_require_root!
    valid_status = expected_status.is_a?(Integer) && expected_status.between?(0, 255)
    odie "expected browser status must be an integer from 0 through 255" unless valid_status
    if (node = ENV.fetch("HOMEBREW_KANDELO_NODE", nil)).to_s != ""
      ENV.prepend_path "PATH", File.dirname(node)
    end

    wasm_path = Pathname(bin_path).expand_path
    command_name = (argv0 || wasm_path.basename).to_s
    invalid_command_name = command_name.empty? || command_name.include?("/") ||
                           command_name.include?("\0") || [".", ".."].include?(command_name)
    odie "invalid browser guest command name: #{command_name}" if invalid_command_name
    kandelo_validate_guest_argv0!(guest_program_path)

    config_values = {
      argv:           argv.map(&:to_s),
      argv0:          command_name,
      env:            env.transform_values(&:to_s),
      timeoutMs:      timeout_ms,
      allowStderr:    allow_stderr,
      mergeStderr:    merge_stderr,
      expectedStatus: expected_status,
    }
    config_values[:guestProgram] = guest_program_path unless guest_program_path.nil?
    config = JSON.generate(config_values)
    guest_files_manifest = testpath/"#{wasm_path.basename}.browser-guest-files.json"
    File.binwrite(
      guest_files_manifest,
      JSON.generate(guest_files.transform_values { |path| Pathname(path).expand_path.to_s }),
    )
    exec_programs_manifest = testpath/"#{wasm_path.basename}.browser-exec-programs.json"
    File.binwrite(
      exec_programs_manifest,
      JSON.generate(exec_programs.transform_values { |path| Pathname(path).expand_path.to_s }),
    )

    # Compiled host output shadows TypeScript source under tsx/Vite. Browser
    # formula tests must exercise the checkout supplied by the build contract.
    FileUtils.rm_rf(Pathname(root)/"host/dist")

    runner = Pathname(__dir__)/"run-browser-wasm.ts"
    command = [
      "node", "--experimental-wasm-exnref", "--import", "tsx/esm",
      runner, root, wasm_path, config, guest_files_manifest, exec_programs_manifest
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
end
