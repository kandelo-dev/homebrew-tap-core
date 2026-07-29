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
  KANDELO_PORTABLE_BINARY_CACHE_BASENAME = ".ci-test-binary-cache"
  KANDELO_TIER2_ATTESTATION_BASENAME = ".kandelo-publisher-tier2-attestation.json"
  KANDELO_TIER2_ATTESTATION_MAX_BYTES = 65_536
  KANDELO_TIER2_SOURCE_MAX_BYTES = 1_048_576
  KANDELO_SUPPORT_RUNTIME_MAX_FILES = 128
  KANDELO_SUPPORT_RUNTIME_FILE_MAX_BYTES = 1_048_576
  KANDELO_SUPPORT_RUNTIME_MAX_BYTES = 16_777_216
  KANDELO_TIER2_SCRIPT_ENV_MAX_KEYS = 64
  KANDELO_TIER2_SCRIPT_ENV_KEY_MAX_BYTES = 4_096
  KANDELO_TIER2_SCRIPT_ENV_VALUE_MAX_BYTES = 4_096
  KANDELO_TIER2_SCRIPT_ENV_VALUE_TOTAL_BYTES = 16_384
  KANDELO_TAP_RECIPE_MANIFEST_MAX_BYTES = 65_536
  KANDELO_TAP_RECIPE_MAX_FILES = 512
  KANDELO_TAP_RECIPE_FILE_MAX_BYTES = 16_777_216
  KANDELO_TAP_RECIPE_MAX_BYTES = 67_108_864
  KANDELO_TAP_RECIPE_MAX_RESOURCES = 32
  KANDELO_TAP_RECIPE_OUTPUT_MAX_ENTRIES = 262_144
  KANDELO_TAP_RECIPE_OUTPUT_FILE_MAX_BYTES = 1_073_741_824
  KANDELO_TAP_RECIPE_OUTPUT_MAX_BYTES = 2_147_483_648
  KANDELO_TAP_RECIPE_OUTPUT_PATH_MAX_BYTES = 4_096
  KANDELO_TAP_RECIPE_OUTPUT_DIRECTORY_MODES = [0555, 0755].freeze
  KANDELO_TAP_RECIPE_OUTPUT_FILE_MODES = [0444, 0555, 0644, 0755].freeze
  KANDELO_TAP_RECIPE_RUNNER_REQUEST_MAX_BYTES = 262_144
  KANDELO_TAP_RECIPE_RUNNER_RESPONSE_MAX_BYTES = 4_096
  KANDELO_TAP_RECIPE_RUNNER_ENV_MAX_KEYS = 512
  KANDELO_TAP_RECIPE_RUNNER_ENV_KEY_MAX_BYTES = 255
  KANDELO_TAP_RECIPE_RUNNER_ENV_VALUE_MAX_BYTES = 8_192
  KANDELO_TAP_RECIPE_RUNNER_ENV_MAX_BYTES = 262_144
  # WHY: the publisher authenticates support constants without executing tap
  # Ruby, so keep the authority root a static literal and construct Pathname
  # objects only inside the guarded runtime method.
  KANDELO_TAP_RECIPE_PROTECTED_ANCHOR = "/run/kandelo-homebrew-publisher".freeze
  KANDELO_TAP_RECIPE_RUNNER_INHERITED_ENV_KEYS = %w[
    ACLOCAL_PATH AR AS CC CFLAGS CMAKE_BUILD_PARALLEL_LEVEL CMAKE_PREFIX_PATH
    CONFIG_SITE CPP CPPFLAGS CXX CXXFLAGS LANG LC_ALL LD LDFLAGS LIBS MAKEFLAGS
    MFLAGS NINJAFLAGS NM OBJCOPY OBJDUMP PKG_CONFIG PKG_CONFIG_LIBDIR
    PKG_CONFIG_PATH PKG_CONFIG_SYSROOT_DIR RANLIB READELF SIZE SOURCE_DATE_EPOCH
    STRINGS STRIP TZ
  ].freeze
  KANDELO_TAP_RECIPE_RUNNER_PLATFORM_ENV_KEYS = %w[
    LLVM_BIN WASM_POSIX_FORK_INSTRUMENT WASM_POSIX_GLUE_DIR
    WASM_POSIX_LLVM_DIR WASM_POSIX_LOCAL_ROOT_SPILL WASM_POSIX_SYSROOT
  ].freeze
  KANDELO_TAP_RECIPE_RUNNER_USER = "kandelo-homebrew-recipe"
  KANDELO_TIER2_TOP_KEYS = %w[
    arch formula formula_sha256 full_name schema support_runtime_sha256
    support_sha256 tap tier2_bridge
  ].freeze
  KANDELO_TAP_RECIPE_TOP_KEYS = %w[
    arch formula formula_sha256 full_name schema support_runtime_sha256
    support_sha256 tap tap_recipe tier2_bridge
  ].freeze
  KANDELO_TIER2_BRIDGE_KEYS = %w[
    build_toml_sha256 package package_toml_sha256 script script_env_keys
    script_sha256 source_mode source_sha256 source_url version
  ].freeze
  KANDELO_TAP_RECIPE_KEYS = %w[
    dependencies entrypoint file_count manifest_sha256 resources script_env_keys
    source_sha256 source_url total_bytes version
  ].freeze
  KANDELO_TIER2_TRUSTED_ENV_KEYS = %w[
    HOMEBREW_KANDELO_ARCH HOMEBREW_KANDELO_FORK_INSTRUMENT
    HOMEBREW_KANDELO_LLVM_BIN HOMEBREW_KANDELO_LOCAL_ROOT_SPILL HOMEBREW_KANDELO_NODE
    HOMEBREW_KANDELO_PRIMARY_TAP_ROOT HOMEBREW_KANDELO_ROOT
    HOMEBREW_KANDELO_SYSROOT HOMEBREW_KANDELO_TAP_RECIPE_RUNNER
    HOMEBREW_KANDELO_TAP_RECIPE_SEALED_ROOT
    HOMEBREW_KANDELO_XTASK_BIN KANDELO_HOMEBREW_ARCH
    KANDELO_HOMEBREW_KANDELO_ROOT LLVM_BIN WASM_POSIX_LLVM_DIR
    WASM_POSIX_SYSROOT
  ].freeze

  # These tools exist only in the trusted Linux publisher. Model them as
  # Homebrew Requirements so a stock Kandelo guest can prune them with the
  # build graph before it tries to resolve homebrew/core Formula metadata.
  # The publisher statically validates this exact class shape and restores the
  # named, sealed host tools to source builds and Formula tests.
  # Declares the sealed Binaryen optimizer used by artifact validation.
  class BinaryenRequirement < Requirement
    KANDELO_NATIVE_FORMULA = "binaryen"
    KANDELO_NATIVE_SENTINEL = "wasm-opt"
    fatal true
    satisfy(build_env: false) { which("wasm-opt") }
  end

  # Declares the sealed pkgconf executable used by build and test probes.
  class PkgconfRequirement < Requirement
    KANDELO_NATIVE_FORMULA = "pkgconf"
    KANDELO_NATIVE_SENTINEL = "pkg-config"
    fatal true
    satisfy(build_env: false) { which("pkg-config") }
  end

  # Declares the sealed WABT validator used by artifact validation.
  class WabtRequirement < Requirement
    KANDELO_NATIVE_FORMULA = "wabt"
    KANDELO_NATIVE_SENTINEL = "wasm-validate"
    fatal true
    satisfy(build_env: false) { which("wasm-validate") }
  end

  # The publisher writes one root-owned, read-only attestation at a fixed path
  # before Homebrew evaluates any Formula. Load it while this support module is
  # required, validate the exact target Formula and support bytes, and freeze
  # all authority before Formula class code can run. Ordinary pours do not have
  # this file and retain an inert nil authority.
  def self.kandelo_load_tier2_runtime!
    support_path = Pathname(__FILE__).realpath
    secure_read = lambda do |path, max_bytes, label, expected_uid: nil, expected_mode: nil,
                            allow_empty: false, utf8: true|
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
      bytes = +"".b if allow_empty && bytes.nil?
      minimum_bytes = allow_empty ? 0 : 1
      unless bytes&.bytesize&.between?(minimum_bytes, max_bytes)
        raise "#{label} must contain #{minimum_bytes} to #{max_bytes} bytes: #{path}"
      end
      if utf8
        bytes.force_encoding(Encoding::UTF_8)
        raise "#{label} is not UTF-8: #{path}" unless bytes.valid_encoding?
      end

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
    support_runtime_files = {}
    support_runtime_bytes = 0
    support_dir.each_child do |entry|
      basename = entry.basename.to_s
      if basename == "test"
        exact_directory.call(entry, "Kandelo Formula support test path")
        next
      end
      unless basename.match?(/\A[A-Za-z0-9][A-Za-z0-9._-]*\z/) &&
             entry.parent == support_dir
        raise "Kandelo Formula support runtime entry has a noncanonical path: #{entry}"
      end
      if support_runtime_files.length >= KANDELO_SUPPORT_RUNTIME_MAX_FILES
        raise "Kandelo Formula support runtime exceeds #{KANDELO_SUPPORT_RUNTIME_MAX_FILES} files: #{support_dir}"
      end
      entry_source = secure_read.call(
        entry,
        KANDELO_SUPPORT_RUNTIME_FILE_MAX_BYTES,
        "Kandelo Formula support runtime entry",
        allow_empty: true,
        utf8: false,
      )
      support_runtime_bytes += entry_source.bytesize
      if support_runtime_bytes > KANDELO_SUPPORT_RUNTIME_MAX_BYTES
        raise "Kandelo Formula support runtime exceeds the byte limit: #{support_dir}"
      end
      support_runtime_files[basename] = Digest::SHA256.hexdigest(entry_source)
    end
    support_runtime_files = support_runtime_files.sort.to_h
    unless support_runtime_files.fetch(support_path.basename.to_s, nil) == support_sha256
      raise "Kandelo Formula support changed while its runtime tree was read: #{support_path}"
    end
    support_runtime_sha256 = Digest::SHA256.hexdigest(JSON.generate(support_runtime_files))

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
    formula_binary_cache_root = nil
    formula_checker_path = nil
    checker_value = trusted_env.fetch("HOMEBREW_KANDELO_XTASK_BIN").to_s
    unless checker_value.empty?
      root_value = trusted_env.fetch("HOMEBREW_KANDELO_ROOT").to_s
      if root_value.empty?
        raise "Kandelo Formula checker requires the authoritative Kandelo root"
      end
      root, = exact_directory.call(Pathname(root_value), "Kandelo root")
      checker = Pathname(checker_value)
      expanded_checker = checker.expand_path.cleanpath
      unless checker.absolute? && checker == expanded_checker
        raise "Kandelo Formula checker must be an absolute normalized path: #{checker}"
      end
      begin
        before = checker.lstat
        resolved = checker.realpath
      rescue SystemCallError => e
        raise "Kandelo Formula checker is unavailable at #{checker}: #{e.message}"
      end
      relative_checker = checker.relative_path_from(root)
      unless resolved == checker && relative_checker.to_s != "." &&
             relative_checker.each_filename.none? { |part| part == ".." }
        raise "Kandelo Formula checker must be inside the authoritative Kandelo root: #{checker}"
      end
      # Match the publisher's one sealed alias location exactly. Merely being a
      # protected file below the checkout is not enough: unrelated root-owned
      # tooling there must never become Formula runner authority.
      checker_parts = relative_checker.each_filename.to_a
      unless checker_parts.length == 4 &&
             checker_parts[0] == "target" &&
             checker_parts[1].match?(/\A[A-Za-z0-9_.+\-]+\z/) &&
             checker_parts[2] == "release" &&
             checker_parts[3] == "xtask"
        raise "Kandelo Formula checker must be at target/<host>/release/xtask " \
              "inside the authoritative Kandelo root: #{checker}"
      end
      unless before.file? && !before.symlink? && before.size.positive? && before.nlink == 1 &&
             before.uid.zero? && (before.mode & 07777) == 0555
        raise "Kandelo Formula checker must be a nonempty, root-owned, mode-0555 " \
              "regular file with one link: #{checker}"
      end

      # The publisher makes this source-alias tree non-replaceable by the
      # Formula user. Opening the reviewed inode also closes the lstat/open
      # race before we freeze the only checker path runners may propagate.
      File.open(checker, "rb") do |file|
        opened_before = file.stat
        identity = [
          before.dev, before.ino, before.size, before.uid, before.gid,
          before.mode, before.nlink,
        ]
        opened_identity = [
          opened_before.dev, opened_before.ino, opened_before.size,
          opened_before.uid, opened_before.gid, opened_before.mode,
          opened_before.nlink,
        ]
        raise "Kandelo Formula checker changed before it was opened: #{checker}" unless opened_identity == identity

        opened_after = file.stat
        after = checker.lstat
        final_identity = [
          after.dev, after.ino, after.size, after.uid, after.gid,
          after.mode, after.nlink,
        ]
        opened_final_identity = [
          opened_after.dev, opened_after.ino, opened_after.size,
          opened_after.uid, opened_after.gid, opened_after.mode,
          opened_after.nlink,
        ]
        unless final_identity == identity && opened_final_identity == identity
          raise "Kandelo Formula checker changed while it was opened: #{checker}"
        end
      end

      # WHY: binaries/ contains relative links into this transported cache.
      # Keeping both fixed below the same frozen source root preserves the
      # package generation identity that prevents cross-package mixing.
      binary_cache_candidate = root/KANDELO_PORTABLE_BINARY_CACHE_BASENAME
      binary_cache_root, = exact_directory.call(
        binary_cache_candidate, "Kandelo Formula binary cache"
      )
      unless binary_cache_root.parent == root &&
             binary_cache_root.basename.to_s == KANDELO_PORTABLE_BINARY_CACHE_BASENAME
        raise "Kandelo Formula binary cache must be the fixed direct child of " \
              "the authoritative Kandelo root: #{binary_cache_candidate}"
      end
      programs_candidate = binary_cache_root/"programs"
      programs_root, = exact_directory.call(
        programs_candidate, "Kandelo Formula binary cache programs root"
      )
      unless programs_root.parent == binary_cache_root &&
             programs_root.basename.to_s == "programs"
        raise "Kandelo Formula binary cache programs root must be the fixed direct child of " \
              "the Formula binary cache: #{programs_candidate}"
      end

      formula_binary_cache_root = binary_cache_root.to_s
      formula_checker_path = checker.to_s
    end
    runtime = {
      "attestation" => nil,
      "attestation_path" => attestation_path&.to_s,
      "formula_binary_cache_root" => formula_binary_cache_root,
      "formula_checker_path" => formula_checker_path,
      "formula_path" => nil,
      "support_path" => support_path.to_s,
      "support_runtime_sha256" => support_runtime_sha256,
      "support_sha256" => support_sha256,
      "tap_recipe_runner_path" => nil,
      "tap_recipe_runner_uid" => nil,
      "tap_recipe_sealed_root" => nil,
      "tap_recipe_tools" => nil,
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
    unless document.is_a?(Hash)
      raise "Tier-2 attestation must be one JSON object"
    end
    expected_top_keys = case document["schema"]
    when 2
      KANDELO_TIER2_TOP_KEYS
    when 3
      KANDELO_TAP_RECIPE_TOP_KEYS
    else
      raise "Tier-2 attestation uses an unsupported schema"
    end
    unless document.keys.sort == expected_top_keys
      raise "Tier-2 attestation must use the exact top-level schema"
    end
    tap_identity = document["tap"]
    formula = document["formula"]
    full_name = document["full_name"]
    arch = document["arch"]
    formula_sha256 = document["formula_sha256"]
    attested_support_sha256 = document["support_sha256"]
    attested_support_runtime_sha256 = document["support_runtime_sha256"]
    bridge = document["tier2_bridge"]
    recipe = document["tap_recipe"] if document["schema"] == 3
    valid_sha256 = lambda do |value|
      value.is_a?(String) && value.match?(/\A[0-9a-f]{64}\z/)
    end
    unless tap_identity.is_a?(String) && tap_identity.match?(/\A[a-z0-9._-]+\/[a-z0-9._-]+\z/) &&
           formula.is_a?(String) && formula.match?(/\A[a-z0-9][a-z0-9._-]{0,254}\z/) &&
           full_name == "#{tap_identity}/#{formula}" && ["wasm32", "wasm64"].include?(arch) &&
           valid_sha256.call(formula_sha256) &&
           ((attested_support_sha256.nil? && attested_support_runtime_sha256.nil?) ||
             (valid_sha256.call(attested_support_sha256) &&
              valid_sha256.call(attested_support_runtime_sha256)))
      raise "Tier-2 attestation has an invalid target identity"
    end
    unless bridge.nil? || (bridge.is_a?(Hash) && bridge.keys.sort == KANDELO_TIER2_BRIDGE_KEYS)
      raise "Tier-2 attestation must use the exact bridge schema"
    end
    unless recipe.nil? || (recipe.is_a?(Hash) && recipe.keys.sort == KANDELO_TAP_RECIPE_KEYS)
      raise "Tier-2 attestation must use the exact tap recipe schema"
    end
    if !bridge.nil? && !recipe.nil?
      raise "Tier-2 attestation cannot authorize both a registry bridge and tap recipe"
    end
    if document["schema"] == 3 && recipe.nil?
      raise "Tier-2 tap recipe attestation is missing its recipe"
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
    unless recipe.nil?
      dependencies = recipe["dependencies"]
      resources = recipe["resources"]
      script_env_keys = recipe["script_env_keys"]
      dependency_env_keys = if dependencies.is_a?(Array) &&
                               dependencies.all? { |dependency| dependency.is_a?(String) }
        # WHY: the publisher admits exactly one class-level initializer after
        # statically inspecting this file. Keep this pre-execution derivation
        # local; the instance helper below serves the already-admitted runtime.
        dependencies.map do |dependency|
          short_name = dependency.rpartition("/").last
          "WASM_POSIX_DEP_#{short_name.upcase.gsub(/[^A-Z0-9]/, "_")}_DIR"
        end
      else
        []
      end
      resource_env_keys = if resources.is_a?(Array) &&
                             resources.all? { |resource| resource.is_a?(Hash) }
        resources.filter_map do |resource|
          resource_name = resource["name"]
          next unless resource_name.is_a?(String)

          "WASM_POSIX_DEP_RESOURCE_" \
            "#{resource_name.upcase.gsub(/[^A-Z0-9]/, "_")}_DIR"
        end
      else
        []
      end
      valid_recipe = recipe["entrypoint"].is_a?(String) &&
                     recipe["entrypoint"].match?(/\A[A-Za-z0-9][A-Za-z0-9._\/-]{0,1023}\.sh\z/) &&
                     !recipe["entrypoint"].split("/").any? { |part| part == "." || part == ".." } &&
                     recipe["file_count"].is_a?(Integer) &&
                     recipe["file_count"].between?(1, KANDELO_TAP_RECIPE_MAX_FILES) &&
                     recipe["total_bytes"].is_a?(Integer) &&
                     recipe["total_bytes"].between?(0, KANDELO_TAP_RECIPE_MAX_BYTES) &&
                     recipe["version"].is_a?(String) &&
                     recipe["version"].match?(/\A[A-Za-z0-9][A-Za-z0-9._+,-]{0,254}\z/) &&
                     recipe["source_url"].is_a?(String) &&
                     recipe["source_url"].bytesize.between?(9, 2048) &&
                     recipe["source_url"].start_with?("https://") &&
                     %w[manifest_sha256 source_sha256].all? do |key|
                       valid_sha256.call(recipe[key])
                     end &&
                     dependencies.is_a?(Array) &&
                     dependencies == dependencies.sort.uniq &&
                     dependencies.length <= 128 &&
                     dependencies.all? do |dependency|
                       dependency.is_a?(String) &&
                         dependency.match?(/\A[a-z0-9._-]+\/[a-z0-9._-]+\/[a-z0-9][a-z0-9._-]{0,254}\z/)
                     end &&
                     dependency_env_keys.length == dependency_env_keys.uniq.length &&
                     resources.is_a?(Array) &&
                     resources.length <= KANDELO_TAP_RECIPE_MAX_RESOURCES &&
                     resources.all? { |resource| resource.is_a?(Hash) } &&
                     resources == resources.sort_by { |resource| resource["name"].to_s } &&
                     resources.all? do |resource|
                       resource.keys.sort == %w[name source_sha256 source_url] &&
                         resource["name"].is_a?(String) &&
                         resource["name"].match?(/\A[a-z0-9][a-z0-9._+-]{0,127}\z/) &&
                         valid_sha256.call(resource["source_sha256"]) &&
                         resource["source_url"].is_a?(String) &&
                         resource["source_url"].bytesize.between?(9, 1024) &&
                         resource["source_url"].start_with?("https://")
                     end &&
                     resource_env_keys.length == resources.length &&
                     resource_env_keys.length == resource_env_keys.uniq.length &&
                     (dependency_env_keys & resource_env_keys).empty? &&
                     script_env_keys.is_a?(Array) &&
                     script_env_keys == script_env_keys.sort.uniq &&
                     script_env_keys.length <= KANDELO_TIER2_SCRIPT_ENV_MAX_KEYS &&
                     script_env_keys.sum(&:bytesize) <= KANDELO_TIER2_SCRIPT_ENV_KEY_MAX_BYTES &&
                     script_env_keys.all? do |key|
                       key.is_a?(String) && key.match?(/\A[A-Z][A-Z0-9_]{0,254}\z/)
                     end &&
                     (dependency_env_keys & script_env_keys).empty? &&
                     (resource_env_keys & script_env_keys).empty? &&
                     valid_sha256.call(attested_support_sha256)
      raise "Tier-2 attestation has invalid tap recipe values" unless valid_recipe
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
    if !attested_support_runtime_sha256.nil? &&
       support_runtime_sha256 != attested_support_runtime_sha256
      raise "loaded Kandelo Formula support runtime differs from the Tier-2 attestation"
    end
    formula_path = primary_tap_root/"Formula"/"#{formula}.rb"
    formula_source = secure_read.call(
      formula_path, KANDELO_TIER2_SOURCE_MAX_BYTES, "Tier-2 Formula"
    )
    unless Digest::SHA256.hexdigest(formula_source) == formula_sha256
      raise "loaded Formula differs from the Tier-2 attestation"
    end

    unless bridge.nil? && recipe.nil?
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
      root, root_stat = exact_directory.call(Pathname(primary_root), "Kandelo root")
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

      unless recipe.nil?
        unless root_stat.uid.zero? && (root_stat.mode & 07022).zero?
          raise "closed tap recipe platform root must be root-owned and not group/world-writable"
        end
        tool_specs = {
          "fork_instrument" => [
            "HOMEBREW_KANDELO_FORK_INSTRUMENT",
            "tools/bin/wasm-fork-instrument",
          ],
          "local_root_spill" => [
            "HOMEBREW_KANDELO_LOCAL_ROOT_SPILL",
            "tools/bin/wasm-local-root-spill",
          ],
        }
        runtime["tap_recipe_tools"] = tool_specs.to_h do |key, (environment_key, relative)|
          expected = root/relative
          selected = trusted_env.fetch(environment_key).to_s
          unless selected == expected.to_s
            raise "closed tap recipe #{key.tr("_", " ")} must use the fixed platform projection path"
          end

          current = root
          Pathname(relative).dirname.each_filename do |component|
            current /= component
            directory, stat = exact_directory.call(
              current, "closed tap recipe tool ancestor"
            )
            unless directory == current && stat.uid.zero? &&
                   (stat.mode & 07022).zero?
              raise "closed tap recipe tool ancestor must be root-owned and " \
                    "not group/world-writable: #{current}"
            end
          end
          begin
            before = expected.lstat
            resolved = expected.realpath
          rescue SystemCallError => e
            raise "closed tap recipe #{key.tr("_", " ")} is unavailable: #{e.message}"
          end
          unless resolved == expected && before.file? && !before.symlink? &&
                 before.uid.zero? && before.size.positive? && before.nlink == 1 &&
                 (before.mode & 07777) == 0555
            raise "closed tap recipe #{key.tr("_", " ")} must be one root-owned " \
                  "mode-0555 regular executable with one link"
          end
          File.open(expected, "rb") do |file|
            opened_before = file.stat
            identity = [
              before.dev, before.ino, before.size, before.uid, before.gid,
              before.mode, before.nlink, before.mtime, before.ctime,
            ]
            opened_identity = [
              opened_before.dev, opened_before.ino, opened_before.size,
              opened_before.uid, opened_before.gid, opened_before.mode,
              opened_before.nlink, opened_before.mtime, opened_before.ctime,
            ]
            unless opened_identity == identity
              raise "closed tap recipe #{key.tr("_", " ")} changed before it was opened"
            end

            opened_after = file.stat
            after = expected.lstat
            final_identity = [
              after.dev, after.ino, after.size, after.uid, after.gid,
              after.mode, after.nlink, after.mtime, after.ctime,
            ]
            opened_final_identity = [
              opened_after.dev, opened_after.ino, opened_after.size,
              opened_after.uid, opened_after.gid, opened_after.mode,
              opened_after.nlink, opened_after.mtime, opened_after.ctime,
            ]
            unless final_identity == identity && opened_final_identity == identity
              raise "closed tap recipe #{key.tr("_", " ")} changed while it was opened"
            end
          end
          [key, expected.to_s]
        end
        runner_value = trusted_env.fetch("HOMEBREW_KANDELO_TAP_RECIPE_RUNNER").to_s
        runner = Pathname(runner_value)
        expanded_runner = runner.expand_path.cleanpath
        unless !runner_value.empty? && runner.absolute? && runner == expanded_runner &&
               runner.basename.to_s == "homebrew-tap-recipe-runner"
          raise "closed tap recipe runner must use the fixed protected path"
        end
        protected_parent, protected_parent_stat = exact_directory.call(
          runner.parent, "closed tap recipe protected runner parent"
        )
        protected_anchor, protected_anchor_stat = exact_directory.call(
          Pathname(KANDELO_TAP_RECIPE_PROTECTED_ANCHOR),
          "closed tap recipe protected runner anchor"
        )
        protected_anchor_parent, protected_anchor_parent_stat = exact_directory.call(
          protected_anchor.parent, "closed tap recipe protected runner anchor parent"
        )
        begin
          runner_stat = runner.lstat
          resolved_runner = runner.realpath
        rescue SystemCallError => e
          raise "closed tap recipe runner is unavailable: #{e.message}"
        end
        unless runner.parent == protected_parent &&
               protected_parent.parent == protected_anchor &&
               protected_parent.basename.to_s.match?(/\Abuild-[0-9a-f]{64}\z/) &&
               protected_anchor.parent == protected_anchor_parent &&
               protected_anchor_parent_stat.uid.zero? &&
               protected_anchor_parent_stat.gid.zero? &&
               (protected_anchor_parent_stat.mode & 00022).zero? &&
               protected_anchor_stat.uid.zero? &&
               protected_anchor_stat.gid.zero? &&
               (protected_anchor_stat.mode & 07777) == 0711 &&
               protected_parent_stat.uid.zero? &&
               protected_parent_stat.gid.zero? &&
               (protected_parent_stat.mode & 07777) == 0555 &&
               resolved_runner == runner && runner_stat.file? &&
               !runner_stat.symlink? && runner_stat.uid.zero? &&
               runner_stat.gid.zero? &&
               runner_stat.size.positive? && runner_stat.nlink == 1 &&
               (runner_stat.mode & 07777) == 0555
          raise "closed tap recipe runner must be one protected root-owned " \
                "mode-0555 executable"
        end
        File.open(runner, "rb") do |file|
          opened = file.stat
          identity = [
            runner_stat.dev, runner_stat.ino, runner_stat.size, runner_stat.uid,
            runner_stat.gid, runner_stat.mode, runner_stat.nlink,
            runner_stat.mtime, runner_stat.ctime,
          ]
          opened_identity = [
            opened.dev, opened.ino, opened.size, opened.uid, opened.gid,
            opened.mode, opened.nlink, opened.mtime, opened.ctime,
          ]
          raise "closed tap recipe runner changed before it was opened" unless
            opened_identity == identity

          opened_after = file.stat
          after = runner.lstat
          final_identity = [
            after.dev, after.ino, after.size, after.uid, after.gid, after.mode,
            after.nlink, after.mtime, after.ctime,
          ]
          opened_final_identity = [
            opened_after.dev, opened_after.ino, opened_after.size,
            opened_after.uid, opened_after.gid, opened_after.mode,
            opened_after.nlink, opened_after.mtime, opened_after.ctime,
          ]
          unless final_identity == identity && opened_final_identity == identity
            raise "closed tap recipe runner changed while it was opened"
          end
        end
        runtime["tap_recipe_runner_path"] = runner.to_s
        runtime["tap_recipe_runner_uid"] = 0
        sealed_root_value =
          trusted_env.fetch("HOMEBREW_KANDELO_TAP_RECIPE_SEALED_ROOT").to_s
        if sealed_root_value.empty?
          raise "closed tap recipe publisher did not provide a sealed output root"
        end
        sealed_root, sealed_root_stat = exact_directory.call(
          Pathname(sealed_root_value), "closed tap recipe sealed output root"
        )
        unless sealed_root.parent == protected_parent &&
               sealed_root.basename.to_s == "sealed-outputs" &&
               sealed_root_stat.uid.zero? &&
               sealed_root_stat.gid.zero? &&
               (sealed_root_stat.mode & 07777) == 0555
          raise "closed tap recipe sealed output root must be the fixed " \
                "root-owned protected directory"
        end
        runtime["tap_recipe_sealed_root"] = sealed_root.to_s
      end
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
      identity = [
        before.dev, before.ino, before.size, before.nlink, before.mode,
        before.mtime, before.ctime,
      ]
      opened_identity = [
        opened_before.dev, opened_before.ino, opened_before.size, opened_before.nlink,
        opened_before.mode, opened_before.mtime, opened_before.ctime,
      ]
      odie "#{label} changed before it was read: #{path}" unless opened_identity == identity

      bytes = file.read(max_bytes + 1)
      opened_after = file.stat
      after = path.lstat
      final_identity = [
        after.dev, after.ino, after.size, after.nlink, after.mode,
        after.mtime, after.ctime,
      ]
      opened_final_identity = [
        opened_after.dev, opened_after.ino, opened_after.size, opened_after.nlink,
        opened_after.mode, opened_after.mtime, opened_after.ctime,
      ]
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

  def kandelo_tier2_restore_environment!(runtime, package, resolver: true)
    package_prefix = "#{package.upcase.gsub(/[^A-Z0-9]/, "_")}_"
    explicit = %w[
      HOMEBREW_KANDELO_ARCH HOMEBREW_KANDELO_PRIMARY_TAP_ROOT
      HOMEBREW_KANDELO_FORK_INSTRUMENT HOMEBREW_KANDELO_LOCAL_ROOT_SPILL
      HOMEBREW_KANDELO_ROOT HOMEBREW_KANDELO_SYSROOT
      HOMEBREW_KANDELO_TAP_RECIPE_RUNNER HOMEBREW_KANDELO_TAP_RECIPE_SEALED_ROOT
      KANDELO_HOMEBREW_ARCH KANDELO_HOMEBREW_KANDELO_ROOT
      WASM_POSIX_BINARY_CACHE_ROOT WASM_POSIX_BINARY_INDEX_URL
      WASM_POSIX_BINARY_RESOLVER_REPO_ROOT WASM_POSIX_DEFAULT_ARCH
      WASM_POSIX_DEPS_REGISTRY WASM_POSIX_INSTALL_LOCAL_MIRROR
      WASM_POSIX_LOCAL_BIN_DIR WASM_POSIX_SYSROOT WASM_POSIX_XTASK_BIN
    ]
    ENV.keys.each do |key|
      ENV.delete(key) if key.start_with?("WASM_POSIX_DEP_") ||
                         key.start_with?(package_prefix) || explicit.include?(key)
    end
    runtime.fetch("trusted_env").each do |key, value|
      next if !resolver && key == "HOMEBREW_KANDELO_XTASK_BIN"
      next if %w[
        HOMEBREW_KANDELO_TAP_RECIPE_RUNNER HOMEBREW_KANDELO_TAP_RECIPE_SEALED_ROOT
      ].include?(key)
      next if resolver && %w[
        HOMEBREW_KANDELO_FORK_INSTRUMENT HOMEBREW_KANDELO_LOCAL_ROOT_SPILL
      ].include?(key)

      value.nil? ? ENV.delete(key) : ENV[key] = value
    end
    unless resolver
      # WHY: tap recipes must consume Homebrew-poured dependency prefixes.
      # Withholding the package checker and transported registry cache makes an
      # accidental build-deps/install-local-binary fallback fail immediately.
      ENV.delete("HOMEBREW_KANDELO_XTASK_BIN")
      ENV.delete("WASM_POSIX_XTASK_BIN")
      tools = runtime.fetch("tap_recipe_tools")
      unless tools.is_a?(Hash) && tools.keys.sort == %w[fork_instrument local_root_spill]
        odie "Kandelo tap recipe platform tool authority is incomplete"
      end
      ENV["WASM_POSIX_FORK_INSTRUMENT"] = tools.fetch("fork_instrument")
      ENV["WASM_POSIX_LOCAL_ROOT_SPILL"] = tools.fetch("local_root_spill")
      return
    end
    binary_cache_root = runtime.fetch("formula_binary_cache_root", nil)
    unless binary_cache_root.nil?
      ENV["WASM_POSIX_BINARY_CACHE_ROOT"] = binary_cache_root
      ENV["WASM_POSIX_BINARY_RESOLVER_REPO_ROOT"] =
        runtime.fetch("trusted_env").fetch("HOMEBREW_KANDELO_ROOT")
    end
  end

  # Move Homebrew's checksum-verified primary source into a fixed root. A
  # Formula may stage separately attested resource inputs under the one fixed
  # resource directory; those inputs are deliberately not moved with the
  # primary source.
  def kandelo_stage_verified_formula_source
    source_dir = buildpath/"kandelo-package-source"
    resource_dir = buildpath/"kandelo-package-resources"
    homebrew_stage_home = buildpath/".brew_home"
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

    if homebrew_stage_home.exist? || homebrew_stage_home.symlink?
      begin
        home_stat = homebrew_stage_home.lstat
      rescue SystemCallError => e
        odie "Homebrew Formula stage home is unavailable: #{e.message}"
      end
      unless home_stat.directory? && !home_stat.symlink? &&
             homebrew_stage_home.realpath == homebrew_stage_home
        odie "Homebrew Formula stage home must be a canonical real directory"
      end
    end

    # WHY: Formula#stage creates .brew_home after verifying the source archive
    # and writes build-tool configuration there. It is Homebrew runtime state,
    # not upstream source, so keep it outside the immutable source projection.
    excluded = [resource_dir, homebrew_stage_home]
    source_entries = buildpath.children.reject { |entry| excluded.include?(entry) }
    odie "Homebrew did not stage Formula source under #{buildpath}" if source_entries.empty?

    source_dir.mkdir
    source_entries.each do |entry|
      FileUtils.mv(entry, source_dir/entry.basename)
    end
    source_dir
  end

  def kandelo_tap_recipe_runtime!
    runtime = KANDELO_TIER2_RUNTIME
    recipe = runtime.dig("attestation", "tap_recipe")
    odie "Kandelo tap recipes require a valid publisher attestation" if recipe.nil?

    [runtime, recipe]
  end

  def kandelo_tap_recipe_dependency_env_key(dependency_name)
    short_name = dependency_name.rpartition("/").last
    "WASM_POSIX_DEP_#{short_name.upcase.gsub(/[^A-Z0-9]/, "_")}_DIR"
  end

  def kandelo_tap_recipe_resource_env_key(resource_name)
    "WASM_POSIX_DEP_RESOURCE_" \
      "#{resource_name.upcase.gsub(/[^A-Z0-9]/, "_")}_DIR"
  end

  def kandelo_stage_tap_recipe_resources!(recipe)
    records = recipe.fetch("resources")
    resource_root = buildpath/"kandelo-package-resources"
    if resource_root.exist? || resource_root.symlink?
      odie "Kandelo Formula resource root was already staged: #{resource_root}"
    end
    return [{}, {}] if records.empty?

    resource_root.mkdir
    host_roots = {}
    guest_environment = {}
    records.each do |record|
      resource_name = record.fetch("name")
      selected = resource(resource_name)
      unless selected.url.to_s == record.fetch("source_url") &&
             selected.checksum.hexdigest == record.fetch("source_sha256")
        odie "Kandelo tap recipe resource identity differs from the publisher attestation: " \
             "#{resource_name}"
      end

      destination = resource_root/resource_name
      # WHY: the privileged runner accepts only this helper-owned directory
      # layout. Staging the checksum-verified resource here lets it snapshot
      # the bytes and mount them read-only at a deterministic guest path
      # without trusting a Formula-provided absolute pathname.
      selected.stage do
        entries = Pathname.pwd.children
        odie "Kandelo tap recipe resource is empty: #{resource_name}" if entries.empty?
        destination.mkdir
        entries.each { |entry| FileUtils.cp_r(entry, destination/entry.basename) }
      end
      begin
        stat = destination.lstat
        resolved = destination.realpath
      rescue SystemCallError => e
        odie "Kandelo tap recipe resource is unavailable: #{resource_name}: #{e.message}"
      end
      unless stat.directory? && !stat.symlink? && resolved == destination
        odie "Kandelo tap recipe resource must use its canonical staging directory: " \
             "#{resource_name}"
      end
      host_roots[resource_name] = destination.to_s
      guest_environment[kandelo_tap_recipe_resource_env_key(resource_name)] =
        "/kandelo/resources/#{resource_name}"
    end
    [host_roots.sort.to_h.freeze, guest_environment.sort.to_h.freeze]
  end

  def kandelo_tap_recipe_relative_path!(value, label)
    unless value.is_a?(String) && value.bytesize.between?(1, 1024) &&
           value.ascii_only? && !value.start_with?("/") && !value.end_with?("/") &&
           !value.include?("\\") && value.split("/").all? do |part|
             part.match?(/\A[A-Za-z0-9][A-Za-z0-9._-]{0,254}\z/) &&
               part != "." && part != ".."
           end
      odie "#{label} is not a canonical relative path: #{value.inspect}"
    end
    Pathname(value)
  end

  def kandelo_tap_recipe_read_file(
    path, max_bytes, label, allow_empty: false, expected_mode: nil
  )
    begin
      before = path.lstat
    rescue SystemCallError => e
      odie "#{label} is unavailable at #{path}: #{e.message}"
    end
    unless before.file? && !before.symlink? && before.nlink == 1
      odie "#{label} must be a regular non-symlink file with one link: #{path}"
    end
    if !expected_mode.nil? && (before.mode & 0777) != expected_mode
      odie "#{label} must have mode #{format("%04o", expected_mode)}: #{path}"
    end

    bytes = nil
    File.open(path, "rb") do |file|
      opened_before = file.stat
      identity = [
        before.dev, before.ino, before.size, before.nlink, before.mode,
        before.mtime, before.ctime,
      ]
      opened_identity = [
        opened_before.dev, opened_before.ino, opened_before.size, opened_before.nlink,
        opened_before.mode, opened_before.mtime, opened_before.ctime,
      ]
      odie "#{label} changed before it was read: #{path}" unless opened_identity == identity

      bytes = file.read(max_bytes + 1)
      opened_after = file.stat
      after = path.lstat
      final_identity = [
        after.dev, after.ino, after.size, after.nlink, after.mode,
        after.mtime, after.ctime,
      ]
      opened_final_identity = [
        opened_after.dev, opened_after.ino, opened_after.size, opened_after.nlink,
        opened_after.mode, opened_after.mtime, opened_after.ctime,
      ]
      unless final_identity == identity && opened_final_identity == identity
        odie "#{label} changed while it was read: #{path}"
      end
      if !expected_mode.nil? &&
         ((opened_before.mode & 0777) != expected_mode ||
          (opened_after.mode & 0777) != expected_mode ||
          (after.mode & 0777) != expected_mode)
        odie "#{label} mode changed while it was read: #{path}"
      end
    end
    minimum_bytes = allow_empty ? 0 : 1
    unless bytes&.bytesize&.between?(minimum_bytes, max_bytes)
      odie "#{label} must contain #{minimum_bytes} to #{max_bytes} bytes: #{path}"
    end
    bytes
  end

  def kandelo_verify_tap_recipe_tree!(runtime, recipe)
    tap_root = Pathname(runtime.fetch("trusted_env").fetch("HOMEBREW_KANDELO_PRIMARY_TAP_ROOT"))
    begin
      tap_root = tap_root.realpath
      tap_root_stat = tap_root.lstat
    rescue SystemCallError => e
      odie "selected primary tap root is unavailable: #{e.message}"
    end
    # WHY: the closed publisher seals the complete Homebrew overlay before it
    # starts any Formula process. Requiring that physical view here prevents a
    # writable checkout-shaped tree from satisfying the content-only recipe
    # manifest after the launcher has established its immutable boundary.
    unless tap_root_stat.directory? && !tap_root_stat.symlink? &&
           (tap_root_stat.mode & 0777) == 0555
      odie "selected primary tap root must be one sealed mode-0555 real directory"
    end
    recipe_root = tap_root/"Kandelo"/"recipes"/name.to_s
    current = tap_root
    ["Kandelo", "recipes", name.to_s].each do |component|
      candidate = current/component
      begin
        stat = candidate.lstat
        resolved = candidate.realpath
      rescue SystemCallError => e
        odie "Formula recipe directory is unavailable at #{candidate}: #{e.message}"
      end
      unless stat.directory? && !stat.symlink? && resolved == candidate &&
             candidate.parent == current && (stat.mode & 0777) == 0555
        odie "Formula recipe directory must be one canonical sealed mode-0555 " \
             "real child of #{current}: #{candidate}"
      end
      current = candidate
    end
    unless current == recipe_root
      odie "Formula recipe root differs from the selected Formula"
    end

    manifest_path = recipe_root/"recipe.json"
    manifest_bytes = kandelo_tap_recipe_read_file(
      manifest_path, KANDELO_TAP_RECIPE_MANIFEST_MAX_BYTES, "Formula recipe manifest",
      expected_mode: 0444
    )
    unless Digest::SHA256.hexdigest(manifest_bytes) == recipe.fetch("manifest_sha256")
      odie "Formula recipe manifest differs from the publisher attestation"
    end
    manifest_bytes.force_encoding(Encoding::UTF_8)
    odie "Formula recipe manifest is not UTF-8" unless manifest_bytes.valid_encoding?
    begin
      manifest = JSON.parse(manifest_bytes, create_additions: false)
    rescue JSON::ParserError => e
      odie "Formula recipe manifest is invalid JSON: #{e.message}"
    end
    unless manifest.is_a?(Hash) &&
           manifest.keys.sort == %w[dependencies entrypoint files schema] &&
           manifest["schema"] == 1
      odie "Formula recipe manifest must use the exact schema"
    end
    dependencies = manifest["dependencies"]
    entrypoint = manifest["entrypoint"]
    files = manifest["files"]
    unless dependencies.is_a?(Array) && dependencies == recipe.fetch("dependencies") &&
           dependencies == dependencies.sort.uniq &&
           files.is_a?(Array) &&
           files.length == recipe.fetch("file_count") &&
           files.length.between?(1, KANDELO_TAP_RECIPE_MAX_FILES)
      odie "Formula recipe manifest differs from the publisher attestation"
    end
    dependency_env_keys = dependencies.map do |dependency|
      kandelo_tap_recipe_dependency_env_key(dependency)
    end
    unless dependency_env_keys.length == dependency_env_keys.uniq.length
      odie "Formula recipe dependencies collide in their build environment names"
    end
    entrypoint_path = kandelo_tap_recipe_relative_path!(entrypoint, "Formula recipe entrypoint")
    unless entrypoint == recipe.fetch("entrypoint") && entrypoint.end_with?(".sh")
      odie "Formula recipe entrypoint differs from the publisher attestation"
    end

    expected_files = {}
    expected_directories = { "" => true }
    total_bytes = 0
    files.each do |record|
      unless record.is_a?(Hash) && record.keys.sort == %w[bytes mode path sha256] &&
             record["bytes"].is_a?(Integer) &&
             record["bytes"].between?(0, KANDELO_TAP_RECIPE_FILE_MAX_BYTES) &&
             ["0644", "0755"].include?(record["mode"]) &&
             record["sha256"].is_a?(String) &&
             record["sha256"].match?(/\A[0-9a-f]{64}\z/)
        odie "Formula recipe manifest contains an invalid file record"
      end
      relative = kandelo_tap_recipe_relative_path!(record["path"], "Formula recipe file")
      relative_string = relative.to_s
      if expected_files.key?(relative_string)
        odie "Formula recipe manifest repeats #{relative_string}"
      end
      expected_files[relative_string] = record
      parent = relative.parent
      until parent.to_s == "."
        expected_directories[parent.to_s] = true
        parent = parent.parent
      end
      total_bytes += record["bytes"]
      if total_bytes > KANDELO_TAP_RECIPE_MAX_BYTES
        odie "Formula recipe exceeds the total byte limit"
      end
    end
    unless expected_files.keys == expected_files.keys.sort &&
           expected_files.key?(entrypoint_path.to_s) &&
           total_bytes == recipe.fetch("total_bytes")
      odie "Formula recipe manifest has a noncanonical file closure"
    end

    actual_files = {}
    actual_directories = { "" => true }
    visit = nil
    visit = lambda do |directory, relative_directory|
      directory.children.sort_by { |entry| entry.basename.to_s }.each do |entry|
        relative = relative_directory/entry.basename
        relative_string = relative.to_s
        kandelo_tap_recipe_relative_path!(relative_string, "Formula recipe tree entry")
        begin
          stat = entry.lstat
        rescue SystemCallError => e
          odie "Formula recipe entry is unavailable at #{entry}: #{e.message}"
        end
        odie "Formula recipe tree must not contain symlinks: #{entry}" if stat.symlink?
        if stat.directory?
          unless (stat.mode & 0777) == 0555
            odie "Formula recipe directory must have sealed mode 0555: #{entry}"
          end
          actual_directories[relative_string] = true
          visit.call(entry, relative)
        elsif stat.file?
          odie "Formula recipe file must have one link: #{entry}" unless stat.nlink == 1
          actual_files[relative_string] = entry unless relative_string == "recipe.json"
        else
          odie "Formula recipe tree contains a non-file node: #{entry}"
        end
      end
    end
    visit.call(recipe_root, Pathname(""))
    unless actual_files.keys.sort == expected_files.keys &&
           actual_directories.keys.sort == expected_directories.keys.sort
      odie "Formula recipe tree differs from its closed manifest"
    end
    expected_files.each do |relative, record|
      # The manifest retains the semantic Git mode, while the closed launcher
      # removes every write bit from its physical projection. Preserve the
      # executable/data distinction without accepting an unsealed source mode.
      sealed_mode = record.fetch("mode") == "0755" ? 0555 : 0444
      bytes = kandelo_tap_recipe_read_file(
        actual_files.fetch(relative), KANDELO_TAP_RECIPE_FILE_MAX_BYTES,
        "Formula recipe file", allow_empty: true,
        expected_mode: sealed_mode
      )
      unless bytes.bytesize == record.fetch("bytes") &&
             Digest::SHA256.hexdigest(bytes) == record.fetch("sha256")
        odie "Formula recipe file differs from its manifest: #{relative}"
      end
    end

    [recipe_root, recipe_root/entrypoint_path]
  end

  def kandelo_tap_recipe_script_env(recipe, script_env)
    odie "Kandelo tap recipe script_env must be a Hash" unless script_env.instance_of?(Hash)

    package_prefix = "#{name.to_s.upcase.gsub(/[^A-Z0-9]/, "_")}_"
    resource_keys = recipe.fetch("resources").map do |resource_record|
      kandelo_tap_recipe_resource_env_key(resource_record.fetch("name"))
    end
    values = {}
    script_env.each do |key, value|
      unless key.is_a?(String) && key.match?(/\A[A-Z][A-Z0-9_]{0,254}\z/)
        odie "Kandelo tap recipe script_env has an invalid key: #{key.inspect}"
      end
      unless key.start_with?("WASM_POSIX_DEP_") || key.start_with?(package_prefix)
        odie "Kandelo tap recipe script_env key is outside the approved namespace: #{key.inspect}"
      end
      if %w[
        WASM_POSIX_DEP_NAME WASM_POSIX_DEP_OUT_DIR WASM_POSIX_DEP_RECIPE_DIR
        WASM_POSIX_DEP_SOURCE_DIR WASM_POSIX_DEP_SOURCE_SHA256
        WASM_POSIX_DEP_SOURCE_URL WASM_POSIX_DEP_TARGET_ARCH
        WASM_POSIX_DEP_VERSION WASM_POSIX_DEP_WORK_DIR
        WASM_POSIX_INSTALL_LOCAL_MIRROR
      ].include?(key) || resource_keys.include?(key)
        odie "Kandelo tap recipe script_env overrides a helper-owned key: #{key.inspect}"
      end
      unless value.is_a?(String) || value.is_a?(Pathname)
        odie "Kandelo tap recipe script_env value must be a String or Pathname: #{key.inspect}"
      end
      converted = value.to_s.dup
      converted.force_encoding(Encoding::UTF_8)
      unless converted.valid_encoding? && !converted.include?("\0") &&
             converted.bytesize <= KANDELO_TIER2_SCRIPT_ENV_VALUE_MAX_BYTES
        odie "Kandelo tap recipe script_env value is invalid or oversized: #{key.inspect}"
      end
      values[key.dup.freeze] = converted.freeze
    end
    keys = values.keys.sort
    unless keys == recipe.fetch("script_env_keys") &&
           keys.length <= KANDELO_TIER2_SCRIPT_ENV_MAX_KEYS &&
           keys.sum(&:bytesize) <= KANDELO_TIER2_SCRIPT_ENV_KEY_MAX_BYTES &&
           values.values.sum(&:bytesize) <= KANDELO_TIER2_SCRIPT_ENV_VALUE_TOTAL_BYTES
      odie "Kandelo tap recipe script_env differs from the publisher attestation"
    end
    values.freeze
  end

  def kandelo_validate_tap_recipe_output!(
    out_dir,
    max_entries: KANDELO_TAP_RECIPE_OUTPUT_MAX_ENTRIES,
    max_file_bytes: KANDELO_TAP_RECIPE_OUTPUT_FILE_MAX_BYTES,
    max_bytes: KANDELO_TAP_RECIPE_OUTPUT_MAX_BYTES,
    max_path_bytes: KANDELO_TAP_RECIPE_OUTPUT_PATH_MAX_BYTES,
    expected_uid: nil,
    sealed: false
  )
    begin
      root_stat = out_dir.lstat
      root = out_dir.realpath
    rescue SystemCallError => e
      odie "tap recipe output root is unavailable at #{out_dir}: #{e.message}"
    end
    expanded_root = out_dir.expand_path.cleanpath
    directory_modes = sealed ? [0555] : KANDELO_TAP_RECIPE_OUTPUT_DIRECTORY_MODES
    file_modes = sealed ? [0444, 0555] : KANDELO_TAP_RECIPE_OUTPUT_FILE_MODES
    unless out_dir.absolute? && out_dir == expanded_root && root == out_dir &&
           root_stat.directory? && !root_stat.symlink? &&
           directory_modes.include?(root_stat.mode & 07777) &&
           (expected_uid.nil? || root_stat.uid == expected_uid)
      odie "tap recipe output root must be a canonical real directory with a safe mode: #{out_dir}"
    end

    entries = 0
    total_bytes = 0
    records = [["d", "", root_stat.mode & 07777, root_stat.uid]]
    pending = [root]
    until pending.empty?
      directory = pending.pop
      directory.each_child do |entry|
        entries += 1
        odie "tap recipe output contains too many filesystem entries" if entries > max_entries

        relative = entry.relative_path_from(root).to_s.dup
        relative.force_encoding(Encoding::UTF_8)
        unless relative.valid_encoding? &&
               relative.bytesize.between?(1, max_path_bytes) &&
               !relative.match?(/[[:cntrl:]]/)
          odie "tap recipe output contains an invalid or oversized path: #{entry}"
        end

        begin
          stat = entry.lstat
        rescue SystemCallError => e
          odie "tap recipe output is unavailable at #{entry}: #{e.message}"
        end
        if stat.directory? && !stat.symlink?
          unless directory_modes.include?(stat.mode & 07777) &&
                 (expected_uid.nil? || stat.uid == expected_uid)
            odie "tap recipe output directory has an unsafe mode: #{entry}"
          end
          # WHY: walking only real directories ensures an output symlink can
          # never redirect validation into an unrelated writable tree.
          begin
            resolved = entry.realpath
          rescue SystemCallError => e
            odie "tap recipe output directory is unavailable at #{entry}: #{e.message}"
          end
          unless resolved == entry && resolved.to_s.start_with?("#{root}/")
            odie "tap recipe output directory escapes its staging root: #{entry}"
          end
          records << ["d", relative, stat.mode & 07777, stat.uid]
          pending << entry
        elsif stat.file? && !stat.symlink?
          odie "tap recipe output file must have one link: #{entry}" unless stat.nlink == 1
          unless file_modes.include?(stat.mode & 07777) &&
                 (expected_uid.nil? || stat.uid == expected_uid)
            odie "tap recipe output file has an unsafe mode: #{entry}"
          end
          if stat.size > max_file_bytes
            odie "tap recipe output file exceeds the byte limit: #{entry}"
          end
          total_bytes += stat.size
          if total_bytes > max_bytes
            odie "tap recipe output exceeds the total byte limit"
          end
          digest = Digest::SHA256.new
          begin
            File.open(entry, "rb") do |file|
              opened_before = file.stat
              identity = [
                stat.dev, stat.ino, stat.size, stat.uid, stat.gid, stat.mode,
                stat.nlink, stat.mtime, stat.ctime,
              ]
              opened_identity = [
                opened_before.dev, opened_before.ino, opened_before.size,
                opened_before.uid, opened_before.gid, opened_before.mode,
                opened_before.nlink, opened_before.mtime, opened_before.ctime,
              ]
              unless opened_identity == identity
                odie "tap recipe output file changed before it was opened: #{entry}"
              end
              while (chunk = file.read(1_048_576))
                digest.update(chunk)
              end
              opened_after = file.stat
              after = entry.lstat
              final_identity = [
                after.dev, after.ino, after.size, after.uid, after.gid, after.mode,
                after.nlink, after.mtime, after.ctime,
              ]
              opened_final_identity = [
                opened_after.dev, opened_after.ino, opened_after.size,
                opened_after.uid, opened_after.gid, opened_after.mode,
                opened_after.nlink, opened_after.mtime, opened_after.ctime,
              ]
              unless final_identity == identity && opened_final_identity == identity
                odie "tap recipe output file changed while it was read: #{entry}"
              end
            end
          rescue SystemCallError => e
            odie "tap recipe output file is unavailable at #{entry}: #{e.message}"
          end
          records << [
            "f", relative, stat.mode & 07777, stat.uid, stat.size, digest.hexdigest,
          ]
        elsif stat.symlink?
          if !expected_uid.nil? && stat.uid != expected_uid
            odie "tap recipe output symlink has an unsafe owner: #{entry}"
          end
          begin
            target = entry.readlink
          rescue SystemCallError => e
            odie "tap recipe output symlink is unavailable at #{entry}: #{e.message}"
          end
          target_string = target.to_s.dup
          target_string.force_encoding(Encoding::UTF_8)
          unless !target.absolute? && target_string.bytesize.between?(1, max_path_bytes) &&
                 target_string.valid_encoding? && !target_string.match?(/[[:cntrl:]]/)
            odie "tap recipe output symlink must use a bounded relative target: #{entry}"
          end
          # Preserve useful relative links such as ../lib/libfoo.so while
          # rejecting links that would become an escape when Homebrew installs
          # the staged output under the Formula prefix.
          destination = (entry.dirname/target).cleanpath
          unless destination == root || destination.to_s.start_with?("#{root}/")
            odie "tap recipe output symlink escapes its staging root: #{entry}"
          end
          after = entry.lstat
          identity = [
            stat.dev, stat.ino, stat.size, stat.uid, stat.gid, stat.mode,
            stat.nlink, stat.mtime, stat.ctime,
          ]
          final_identity = [
            after.dev, after.ino, after.size, after.uid, after.gid, after.mode,
            after.nlink, after.mtime, after.ctime,
          ]
          unless final_identity == identity
            odie "tap recipe output symlink changed while it was read: #{entry}"
          end
          records << ["l", relative, stat.uid, target_string]
        else
          odie "tap recipe output contains an unsupported filesystem node: #{entry}"
        end
      end
    end
    records.sort_by! { |record| record.fetch(1) }
    {
      "entry_count"            => entries,
      "output_manifest_sha256" => Digest::SHA256.hexdigest(JSON.generate(records)),
      "total_bytes"            => total_bytes,
    }.freeze
  end

  def kandelo_tap_recipe_runner_path(root, native_roots, runtime)
    trusted_env = runtime.fetch("trusted_env")
    entries = [Pathname(root)/"sdk/bin", Pathname(root)/"tools/bin"]
    llvm_bin = ENV.fetch("LLVM_BIN", ENV.fetch("WASM_POSIX_LLVM_DIR", "")).to_s
    entries << Pathname(llvm_bin) unless llvm_bin.empty?
    node = trusted_env.fetch("HOMEBREW_KANDELO_NODE", nil).to_s
    entries << Pathname(node).dirname unless node.empty?
    native_roots.each do |native_root|
      %w[bin sbin libexec/bin].each do |relative|
        candidate = Pathname(native_root)/relative
        entries << candidate if candidate.directory? && !candidate.symlink?
      end
    end
    entries.concat([Pathname("/usr/bin"), Pathname("/bin")])
    entries.map(&:to_s).uniq.join(File::PATH_SEPARATOR)
  end

  def kandelo_tap_recipe_runner_environment(
    runtime:, root:, work_root:, native_roots:, formula_env:, helper_env:
  )
    # WHY: the publisher process can carry repository credentials and resolver
    # authority. Constructing the recipe environment from an exact allowlist
    # keeps those ambient values out of both the request and the nested service.
    values = KANDELO_TAP_RECIPE_RUNNER_INHERITED_ENV_KEYS.to_h do |key|
      [key, ENV.fetch(key, nil)]
    end.compact
    KANDELO_TAP_RECIPE_RUNNER_PLATFORM_ENV_KEYS.each do |key|
      value = ENV.fetch(key, nil)
      values[key] = value unless value.nil?
    end
    values.merge!(formula_env)
    values.merge!(helper_env)
    values.merge!(
      "HOME"    => (Pathname(work_root)/"home").to_s,
      "LOGNAME" => KANDELO_TAP_RECIPE_RUNNER_USER,
      "PATH"    => kandelo_tap_recipe_runner_path(root, native_roots, runtime),
      "TMPDIR"  => (Pathname(work_root)/"tmp").to_s,
      "USER"    => KANDELO_TAP_RECIPE_RUNNER_USER,
    )
    values = values.sort.to_h
    unless values.length <= KANDELO_TAP_RECIPE_RUNNER_ENV_MAX_KEYS &&
           values.keys.all? do |key|
             key.bytesize.between?(1, KANDELO_TAP_RECIPE_RUNNER_ENV_KEY_MAX_BYTES) &&
               key.match?(/\A[A-Za-z_][A-Za-z0-9_]*\z/)
           end
      odie "tap recipe runner environment has invalid or excessive keys"
    end
    total_bytes = 0
    values.each do |key, value|
      encoded = value.to_s.dup
      encoded.force_encoding(Encoding::UTF_8)
      unless encoded.valid_encoding? && !encoded.include?("\0") &&
             encoded.bytesize <= KANDELO_TAP_RECIPE_RUNNER_ENV_VALUE_MAX_BYTES
        odie "tap recipe runner environment has an invalid or oversized value: #{key}"
      end
      total_bytes += key.bytesize + encoded.bytesize
      if total_bytes > KANDELO_TAP_RECIPE_RUNNER_ENV_MAX_BYTES
        odie "tap recipe runner environment exceeds the total byte limit"
      end
      values[key] = encoded
    end
    values.freeze
  end

  def kandelo_tap_recipe_native_build_roots
    return [] unless respond_to?(:deps)

    roots = deps.filter_map do |dependency|
      next unless dependency.build? || dependency.test?

      formula = dependency.to_formula
      next if kandelo_target_formula?(formula.full_name)

      keg = Pathname(formula.rack)/formula.pkg_version.to_s
      begin
        stat = keg.lstat
        resolved = keg.realpath
      rescue SystemCallError => e
        odie "native build dependency is not installed at #{keg}: #{e.message}"
      end
      unless stat.directory? && !stat.symlink? && resolved == keg
        odie "native build dependency must use one canonical versioned keg: #{keg}"
      end
      keg.to_s
    end
    roots.sort.uniq.freeze
  end

  def kandelo_write_tap_recipe_runner_request!(path, request)
    bytes = JSON.generate(request)
    unless bytes.bytesize.between?(1, KANDELO_TAP_RECIPE_RUNNER_REQUEST_MAX_BYTES)
      odie "tap recipe runner request exceeds the byte limit"
    end
    if path.exist? || path.symlink?
      odie "tap recipe runner request path is already occupied: #{path}"
    end
    flags = File::WRONLY | File::CREAT | File::EXCL
    File.open(path, flags, 0400) do |file|
      file.binmode
      file.write(bytes)
      file.flush
      file.fsync
    end
    unless (path.lstat.mode & 07777) == 0400
      odie "tap recipe runner request does not have mode 0400"
    end
    [bytes, Digest::SHA256.hexdigest(bytes)]
  rescue SystemCallError => e
    odie "tap recipe runner request could not be created: #{e.message}"
  end

  def kandelo_read_tap_recipe_runner_response!(path, expected_uid)
    begin
      before = path.lstat
    rescue SystemCallError => e
      odie "tap recipe runner response is unavailable: #{e.message}"
    end
    unless before.file? && !before.symlink? && before.nlink == 1 &&
           before.uid == expected_uid && (before.mode & 07777) == 0444
      odie "tap recipe runner response must be one sealed mode-0444 regular file"
    end
    bytes = nil
    begin
      File.open(path, "rb") do |file|
        opened_before = file.stat
        identity = [
          before.dev, before.ino, before.size, before.uid, before.gid,
          before.mode, before.nlink, before.mtime, before.ctime,
        ]
        opened_identity = [
          opened_before.dev, opened_before.ino, opened_before.size,
          opened_before.uid, opened_before.gid, opened_before.mode,
          opened_before.nlink, opened_before.mtime, opened_before.ctime,
        ]
        unless opened_identity == identity
          odie "tap recipe runner response changed before it was opened"
        end
        bytes = file.read(KANDELO_TAP_RECIPE_RUNNER_RESPONSE_MAX_BYTES + 1)
        opened_after = file.stat
        after = path.lstat
        final_identity = [
          after.dev, after.ino, after.size, after.uid, after.gid,
          after.mode, after.nlink, after.mtime, after.ctime,
        ]
        opened_final_identity = [
          opened_after.dev, opened_after.ino, opened_after.size,
          opened_after.uid, opened_after.gid, opened_after.mode,
          opened_after.nlink, opened_after.mtime, opened_after.ctime,
        ]
        unless final_identity == identity && opened_final_identity == identity
          odie "tap recipe runner response changed while it was read"
        end
      end
    rescue SystemCallError => e
      odie "tap recipe runner response could not be read: #{e.message}"
    end
    unless bytes&.bytesize&.between?(1, KANDELO_TAP_RECIPE_RUNNER_RESPONSE_MAX_BYTES)
      odie "tap recipe runner response exceeds the byte limit"
    end
    begin
      response = JSON.parse(bytes, create_additions: false)
    rescue JSON::ParserError => e
      odie "tap recipe runner response is invalid JSON: #{e.message}"
    end
    unless response.is_a?(Hash) && JSON.generate(response) == bytes
      odie "tap recipe runner response must use canonical JSON without duplicate keys"
    end
    expected_keys = %w[
      entry_count output_manifest_sha256 request_sha256 schema
      sealed_output_root total_bytes
    ]
    unless response.keys == expected_keys &&
           response["schema"] == 1 &&
           response["entry_count"].is_a?(Integer) &&
           response["entry_count"].between?(0, KANDELO_TAP_RECIPE_OUTPUT_MAX_ENTRIES) &&
           response["total_bytes"].is_a?(Integer) &&
           response["total_bytes"].between?(0, KANDELO_TAP_RECIPE_OUTPUT_MAX_BYTES) &&
           %w[output_manifest_sha256 request_sha256].all? do |key|
             response[key].is_a?(String) &&
               response[key].match?(/\A[0-9a-f]{64}\z/)
           end &&
           response["sealed_output_root"].is_a?(String) &&
           response["sealed_output_root"].bytesize.between?(1, 4_096) &&
           !response["sealed_output_root"].match?(/[[:cntrl:]]/)
      odie "tap recipe runner response does not use the exact schema"
    end
    response
  end

  def kandelo_accept_tap_recipe_runner_response!(
    response, request_sha256, sealed_root, expected_uid
  )
    unless response.fetch("request_sha256") == request_sha256
      odie "tap recipe runner response does not bind the exact request"
    end
    selected = Pathname(response.fetch("sealed_output_root"))
    expanded = selected.expand_path.cleanpath
    unless selected.absolute? && selected == expanded
      odie "tap recipe runner returned a noncanonical sealed output path"
    end
    begin
      resolved_root = Pathname(sealed_root).realpath
      resolved = selected.realpath
    rescue SystemCallError => e
      odie "tap recipe sealed output is unavailable: #{e.message}"
    end
    unless resolved == selected && resolved.parent == resolved_root
      odie "tap recipe runner returned output outside the protected sealed root"
    end
    evidence = kandelo_validate_tap_recipe_output!(
      resolved, expected_uid:, sealed: true
    )
    unless response.fetch("entry_count") == evidence.fetch("entry_count") &&
           response.fetch("total_bytes") == evidence.fetch("total_bytes") &&
           response.fetch("output_manifest_sha256") ==
             evidence.fetch("output_manifest_sha256")
      odie "tap recipe sealed output differs from the runner response"
    end
    resolved
  end

  # Run one tap-owned recipe closure against Homebrew's verified source and
  # poured target dependencies. Registry resolver authority is deliberately
  # withheld; a fixed publisher runner executes the recipe as a distinct uid,
  # kills its complete cgroup, and returns only a root-owned sealed output.
  def kandelo_build_tap_recipe(manifest_sha256:, resources: [], script_env: {})
    saved = ENV.to_hash
    request_path = nil
    response_path = nil
    runtime, recipe = kandelo_tap_recipe_runtime!
    attestation = runtime.fetch("attestation")
    unless manifest_sha256.is_a?(String) &&
           manifest_sha256 == recipe.fetch("manifest_sha256")
      odie "Kandelo tap recipe manifest differs from the publisher attestation"
    end
    expected_resources = recipe.fetch("resources").map { |record| record.fetch("name") }
    unless resources.instance_of?(Array) &&
           resources.all? { |resource_name| resource_name.instance_of?(String) } &&
           resources == expected_resources
      odie "Kandelo tap recipe resource selection differs from the publisher attestation"
    end

    formula_name = name.to_s
    formula_full_name = respond_to?(:full_name) ? full_name.to_s : "kandelo-dev/tap-core/#{formula_name}"
    unless formula_name == attestation.fetch("formula") &&
           formula_full_name == attestation.fetch("full_name") &&
           version.to_s == recipe.fetch("version") &&
           stable.url.to_s == recipe.fetch("source_url") &&
           stable.checksum.hexdigest == recipe.fetch("source_sha256")
      odie "Kandelo tap recipe Formula identity differs from the publisher attestation"
    end
    formula_env = kandelo_tap_recipe_script_env(recipe, script_env)
    formula_path = Pathname(path).realpath
    support_path = Pathname(runtime.fetch("support_path"))
    unless formula_path.to_s == runtime.fetch("formula_path")
      odie "Kandelo tap recipe Formula path differs from the publisher attestation"
    end
    kandelo_tier2_read_attested_file(
      formula_path, attestation.fetch("formula_sha256"), KANDELO_TIER2_SOURCE_MAX_BYTES,
      "tap recipe Formula"
    )
    kandelo_tier2_read_attested_file(
      support_path, attestation.fetch("support_sha256"), KANDELO_TIER2_SOURCE_MAX_BYTES,
      "Kandelo Formula support"
    )
    recipe_root, entrypoint = kandelo_verify_tap_recipe_tree!(runtime, recipe)

    trusted_env = runtime.fetch("trusted_env")
    root = Pathname(trusted_env.fetch("HOMEBREW_KANDELO_ROOT"))
    arch = trusted_env.fetch("HOMEBREW_KANDELO_ARCH")
    unless arch == attestation.fetch("arch")
      odie "Kandelo tap recipe architecture differs from the publisher attestation"
    end
    resource_roots, resource_env =
      kandelo_stage_tap_recipe_resources!(recipe)
    source_dir = kandelo_stage_verified_formula_source
    work_dir = buildpath/"kandelo-package-work"
    out_dir = buildpath/"kandelo-package-out"
    work_dir.mkdir
    out_dir.mkdir

    prefix_existed = prefix.exist?
    if prefix_existed
      begin
        prefix_stat = prefix.lstat
      rescue SystemCallError => e
        odie "Formula staging prefix is unavailable: #{e.message}"
      end
      unless prefix_stat.directory? && !prefix_stat.symlink? && prefix.children.empty?
        odie "tap recipe must run before Formula staging output is created"
      end
    end

    kandelo_tier2_restore_environment!(runtime, formula_name, resolver: false)
    activated_root = kandelo_activate_sdk!
    unless Pathname(activated_root).realpath == root
      odie "Kandelo SDK activation changed the attested root"
    end
    kandelo_activate_sysroot!(activated_root)
    formula_env.each { |key, value| ENV[key] = value }
    dependency_env = {}
    available_dependencies = kandelo_target_runtime_dependencies.to_h do |dependency|
      [dependency.full_name.to_s, dependency]
    end
    recipe.fetch("dependencies").each do |dependency_name|
      dependency = available_dependencies.fetch(dependency_name, nil)
      odie "tap recipe dependency is not a selected target dependency: #{dependency_name}" if dependency.nil?

      key = kandelo_tap_recipe_dependency_env_key(dependency_name)
      odie "tap recipe script_env overrides dependency prefix #{key}" if formula_env.key?(key)
      if dependency_env.key?(key)
        odie "tap recipe dependencies collide in their build environment names: #{key}"
      end
      dependency_env[key] = kandelo_formula_prefix(dependency_name).to_s
    end
    helper_env = {
      "WASM_POSIX_DEP_NAME"             => formula_name,
      "WASM_POSIX_DEP_OUT_DIR"          => out_dir,
      "WASM_POSIX_DEP_RECIPE_DIR"       => recipe_root,
      "WASM_POSIX_DEP_SOURCE_DIR"       => source_dir,
      "WASM_POSIX_DEP_SOURCE_SHA256"    => recipe.fetch("source_sha256"),
      "WASM_POSIX_DEP_SOURCE_URL"       => recipe.fetch("source_url"),
      "WASM_POSIX_DEP_TARGET_ARCH"      => arch,
      "WASM_POSIX_DEP_VERSION"          => recipe.fetch("version"),
      "WASM_POSIX_DEP_WORK_DIR"         => work_dir,
      "WASM_POSIX_INSTALL_LOCAL_MIRROR" => "0",
    }.merge(dependency_env).merge(resource_env)
    helper_env.each { |key, value| ENV[key] = value.to_s }
    %w[
      HOMEBREW_KANDELO_XTASK_BIN WASM_POSIX_BINARY_CACHE_ROOT
      WASM_POSIX_BINARY_INDEX_URL WASM_POSIX_BINARY_RESOLVER_REPO_ROOT
      WASM_POSIX_DEPS_REGISTRY WASM_POSIX_LOCAL_BIN_DIR WASM_POSIX_XTASK_BIN
    ].each { |key| ENV.delete(key) }

    runner = Pathname(runtime.fetch("tap_recipe_runner_path"))
    sealed_root = runtime.fetch("tap_recipe_sealed_root")
    runner_uid = runtime.fetch("tap_recipe_runner_uid")
    unless runner_uid.is_a?(Integer) && runner_uid >= 0
      odie "Kandelo tap recipe runner owner is invalid"
    end
    request_path = buildpath/".kandelo-tap-recipe-request.json"
    response_path = buildpath/".kandelo-tap-recipe-response.json"
    if response_path.exist? || response_path.symlink?
      odie "tap recipe runner response path is already occupied: #{response_path}"
    end
    native_roots = kandelo_tap_recipe_native_build_roots
    environment = kandelo_tap_recipe_runner_environment(
      runtime:,
      root:,
      work_root: work_dir,
      native_roots:,
      formula_env:,
      helper_env:,
    )
    request = {
      "arch"            => arch,
      "dependencies"    => dependency_env.sort.to_h,
      "entrypoint"       => entrypoint.to_s,
      "environment"      => environment,
      "formula"          => formula_full_name,
      "limits"           => {
        "max_bytes"      => KANDELO_TAP_RECIPE_OUTPUT_MAX_BYTES,
        "max_entries"    => KANDELO_TAP_RECIPE_OUTPUT_MAX_ENTRIES,
        "max_file_bytes" => KANDELO_TAP_RECIPE_OUTPUT_FILE_MAX_BYTES,
        "max_path_bytes" => KANDELO_TAP_RECIPE_OUTPUT_PATH_MAX_BYTES,
      },
      "manifest_sha256" => recipe.fetch("manifest_sha256"),
      "native_roots"    => native_roots,
      "output_root"     => out_dir.to_s,
      "platform_root"   => root.to_s,
      "recipe_root"     => recipe_root.to_s,
      "resources"       => resource_roots,
      "schema"          => 1,
      "source_root"     => source_dir.to_s,
      "sysroot"         => ENV.fetch("WASM_POSIX_SYSROOT"),
      "version"         => recipe.fetch("version"),
      "work_root"       => work_dir.to_s,
    }
    _request_bytes, request_sha256 =
      kandelo_write_tap_recipe_runner_request!(request_path, request)

    # Revalidate immediately around the privileged boundary. The runner receives
    # only the closed request, runs the recipe in its own cgroup, kills every
    # descendant, and seals a fresh copy before reporting success.
    kandelo_verify_tap_recipe_tree!(runtime, recipe)
    # WHY: Formula#system raises on failure and returns nil on success.
    # Treating it like Kernel.system rejects every successful recipe.
    system(
      runner.to_s,
      "--request", request_path.to_s,
      "--response", response_path.to_s,
    )
    kandelo_verify_tap_recipe_tree!(runtime, recipe)

    [source_dir, work_dir].each do |directory|
      kandelo_tier2_exact_directory(directory, buildpath.realpath, "tap recipe build root")
    end
    if prefix_existed
      odie "tap recipe wrote directly into the Formula staging prefix" unless prefix.children.empty?
    elsif prefix.exist? || prefix.symlink?
      odie "tap recipe created the Formula staging prefix directly"
    end
    response = kandelo_read_tap_recipe_runner_response!(response_path, runner_uid)
    kandelo_accept_tap_recipe_runner_response!(
      response, request_sha256, sealed_root, runner_uid
    )
  ensure
    [request_path, response_path].compact.each do |transient|
      transient.delete if transient.exist? || transient.symlink?
    rescue SystemCallError
      nil
    end
    ENV.replace(saved) if saved
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

  def kandelo_formula_checker_path
    KANDELO_TIER2_RUNTIME.fetch("formula_checker_path")
  end

  def kandelo_formula_binary_cache_root
    KANDELO_TIER2_RUNTIME.fetch("formula_binary_cache_root")
  end

  def kandelo_formula_resolver_repo_root
    KANDELO_TIER2_RUNTIME.fetch("trusted_env").fetch("HOMEBREW_KANDELO_ROOT")
  end

  def kandelo_node_runner_environment
    checker = kandelo_formula_checker_path
    return "" if checker.nil?

    binary_cache_root = kandelo_formula_binary_cache_root
    resolver_repo_root = kandelo_formula_resolver_repo_root
    if binary_cache_root.nil? || resolver_repo_root.to_s.empty?
      odie "sealed Kandelo Formula runner authority is incomplete"
    end

    # WHY: Homebrew preserves HOMEBREW_* variables when it re-execs Formula
    # tests but removes the resolver's ordinary variables. The support loader
    # validates and freezes one source root, checker, and transported package
    # cache before Formula code runs. Restoring that exact set keeps binaries/
    # links bound to their complete content-addressed package generations.
    [
      "WASM_POSIX_BINARY_CACHE_ROOT=#{Shellwords.escape(binary_cache_root)}",
      "WASM_POSIX_BINARY_RESOLVER_REPO_ROOT=#{Shellwords.escape(resolver_repo_root)}",
      "WASM_POSIX_XTASK_BIN=#{Shellwords.escape(checker)}",
    ].join(" ") << " "
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
    command << kandelo_node_runner_environment
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
    command << kandelo_node_runner_environment
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
      invocation << kandelo_node_runner_environment
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
    output = shell_output(
      "cd #{Shellwords.escape(root)} && #{kandelo_node_runner_environment}#{command} < /dev/null",
    )
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

    shell_output(
      "cd #{Shellwords.escape(root)} && #{kandelo_node_runner_environment}#{command} < /dev/null",
    )
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

    shell_output(
      "cd #{Shellwords.escape(root)} && #{kandelo_node_runner_environment}#{command} < /dev/null",
    )
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

    shell_output(
      "cd #{Shellwords.escape(root)} && #{kandelo_node_runner_environment}#{command} < /dev/null",
    )
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
