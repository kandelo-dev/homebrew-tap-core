# typed: strict
# frozen_string_literal: true

require "minitest/autorun"
require "open3"
# Standalone Ruby does not preload Homebrew's Pathname helper.
require "pathname" # rubocop:disable Lint/RedundantRequireStatement
require "rbconfig"
require "tmpdir"

# Standalone Ruby does not preload Homebrew's Requirement DSL. This minimal
# double exists only when the real Homebrew class is absent; the publisher's
# pinned-Homebrew lifecycle test exercises the real DSL separately.
unless defined?(Requirement)
  requirement_double = Class.new
  requirement_double.define_singleton_method(:fatal) { |*| nil }
  requirement_double.define_singleton_method(:satisfy) { |**_options, &_block| nil }
  Object.const_set(:Requirement, requirement_double)
end

require_relative "../kandelo_formula_support"

# Regression coverage for Formula runtime execution evidence.
class KandeloFormulaSupportTest < Minitest::Test
  DependencyFormula = Struct.new(:full_name, :opt_bin, :opt_sbin, :opt_libexec, keyword_init: true)
  InstalledFormula = Struct.new(:rack, :pkg_version, keyword_init: true)
  StableSpec = Struct.new(:url, :checksum, keyword_init: true)
  StableChecksum = Struct.new(:hexdigest, keyword_init: true)
  StagedResource = Struct.new(:url, :checksum, :source, keyword_init: true) do
    def stage
      Dir.chdir(source) { yield }
    end
  end

  NATIVE_REQUIREMENT_IDENTITIES = {
    KandeloFormulaSupport::BinaryenRequirement => ["binaryen", "wasm-opt"],
    KandeloFormulaSupport::PkgconfRequirement  => ["pkgconf", "pkg-config"],
    KandeloFormulaSupport::WabtRequirement     => ["wabt", "wasm-validate"],
  }.freeze

  GENERATED_BOTTLE_BLOCK = /\n  bottle do[ \t]*\n(?:    [^\n]*\n|\n)*  end[ \t]*\n(?:\n)?/

  def formula_source_without_generated_bottle(source)
    source.sub(GENERATED_BOTTLE_BLOCK, "")
  end

  def test_guest_homebrew_paths_use_kandelo_identity
    assert_equal(
      "/opt/kandelo/homebrew",
      KandeloFormulaSupport::KANDELO_GUEST_HOMEBREW_PREFIX,
    )
    assert_equal(
      "/opt/kandelo/homebrew/Cellar",
      KandeloFormulaSupport::KANDELO_GUEST_HOMEBREW_CELLAR,
    )
  end

  def test_formula_sources_use_the_shared_guest_homebrew_prefix
    formula_dir = Pathname(__dir__).join("../../..", "Formula").cleanpath

    formula_dir.glob("*.rb").sort.each do |path|
      # Bottle Cellar values are generated publication metadata. Formula build
      # and test code must use the shared source-level authority instead of
      # growing another literal that can drift during a future path migration.
      recipe_source = formula_source_without_generated_bottle(path.binread)
      refute_includes(
        recipe_source,
        "/home/linuxbrew/.linuxbrew",
        "#{path.basename} retains the retired guest prefix",
      )
      refute_includes(
        recipe_source,
        KandeloFormulaSupport::KANDELO_GUEST_HOMEBREW_PREFIX,
        "#{path.basename} hardcodes the canonical guest prefix",
      )
    end
  end

  def test_formula_source_normalization_preserves_post_bottle_content
    reviewed_target = <<~RUBY
      class Example < Formula
        bottle do
          sha256 cellar: :any, wasm32_kandelo: "reviewed"
        end

        def post_bottle_contract
          "reviewed"
        end
      end
    RUBY
    drifted_active = reviewed_target.sub(
      "  def post_bottle_contract\n    \"reviewed\"",
      "  def post_bottle_contract\n    \"drifted\"",
    )

    refute_equal(
      formula_source_without_generated_bottle(drifted_active),
      formula_source_without_generated_bottle(reviewed_target),
    )
    assert_includes formula_source_without_generated_bottle(reviewed_target), "post_bottle_contract"
  end

  def test_native_requirements_have_the_closed_publisher_identity
    support = Pathname(__dir__).join("..", "kandelo_formula_support.rb").binread
    NATIVE_REQUIREMENT_IDENTITIES.each do |requirement, (formula, sentinel)|
      assert_operator requirement, :<, Requirement
      assert_equal formula, requirement::KANDELO_NATIVE_FORMULA
      assert_equal sentinel, requirement::KANDELO_NATIVE_SENTINEL
      canonical_definition = [
        "  class #{requirement.name.split("::").last} < Requirement",
        "    KANDELO_NATIVE_FORMULA = #{formula.inspect}",
        "    KANDELO_NATIVE_SENTINEL = #{sentinel.inspect}",
        "    fatal true",
        "    satisfy(build_env: false) { which(#{sentinel.inspect}) }",
        "  end",
        "",
      ].join("\n")
      assert_includes support, canonical_definition
    end
  end

  def test_formulae_use_requirements_for_every_allowlisted_publisher_tool
    formula_dir = Pathname(__dir__).join("../../..", "Formula").cleanpath
    requirement_pattern = Regexp.new(
      "depends_on KandeloFormulaSupport::([A-Z][A-Za-z0-9]*Requirement) " \
      "=> (:[a-z]+|\\[(?::[a-z]+)(?:, :[a-z]+)*\\])",
    )
    allowed = NATIVE_REQUIREMENT_IDENTITIES.keys.map do |requirement|
      requirement.name.split("::").last
    end.sort
    support_require =
      %Q(require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s\n)
    used = []

    formula_dir.glob("*.rb").sort.each do |path|
      source = path.binread
      refute_match(
        /^  depends_on "(?:binaryen|pkgconf|wabt)"(?: => .+)?$/,
        source,
        "#{path.basename} exposes a publisher-only tool as a Formula dependency",
      )

      declarations = source.scan(requirement_pattern)
      next if declarations.empty?

      assert_includes source, support_require
      declarations.each do |class_name, tags|
        assert_includes allowed, class_name, "#{path.basename} uses an unsealed native Requirement"
        assert_includes(
          [":build", "[:build, :test]"],
          tags,
          "#{path.basename} lets a publisher-only Requirement enter the guest runtime graph",
        )
        used << class_name
      end
    end

    assert_equal allowed, used.uniq.sort
  end

  def test_fork_instrumented_formulae_delegate_fork_import_validation_to_shared_contract
    formula_dir = Pathname(__dir__).join("../../..", "Formula").cleanpath
    offenders = formula_dir.glob("*.rb").sort.filter_map do |path|
      source = path.binread
      instrumentation = source.index("kandelo_fork_instrument")
      next if instrumentation.nil?

      # WHY: non-ABI import audits remain useful before instrumentation, but
      # imports added afterward belong to Kandelo's generated fork contract.
      # Parsing them here or spelling their names in a Formula can drift from
      # the shared structural validator whenever that contract changes.
      after_instrumentation = source.byteslice(instrumentation..)
      formula_owns_fork_imports =
        after_instrumentation.include?("<- env") ||
        source.match?(/\b__wpk_fork_[A-Za-z0-9_]*\b/)
      path.basename.to_s if formula_owns_fork_imports
    end

    assert_empty(
      offenders,
      "fork-instrumented Formulae must delegate fork-import validation " \
      "to kandelo_validate_wasm_artifact",
    )
  end

  # Minimal Formula double for command-construction tests.
  class Harness
    include KandeloFormulaSupport

    attr_accessor :build_path, :dependency_formulae, :formula_full_name, :formula_name, :formula_path,
                  :formula_pkg_version, :formula_version, :formula_binary_cache_root,
                  :formula_checker_path,
                  :formula_resolver_repo_root, :homebrew_prefix_path, :nix_path, :prefix_path,
                  :root_path, :runtime_formulae, :shell_result, :stable_spec, :test_path,
                  :tier2_runtime, :resources
    attr_reader :command, :expected_status, :pty_config, :pty_config_mode, :pty_config_path,
                :recorded_launcher, :system_args, :system_calls, :system_environment

    def kandelo_require_root!
      root_path || "/tmp/kandelo root"
    end

    def testpath
      test_path || Pathname("/tmp/formula test")
    end

    def buildpath
      build_path || testpath
    end

    def name
      formula_name || "test-formula"
    end

    def version
      formula_version || "1.0"
    end

    def pkg_version
      formula_pkg_version || version
    end

    def full_name
      formula_full_name || "kandelo-dev/tap-core/#{name}"
    end

    def path
      formula_path || Pathname("/tmp/formula.rb")
    end

    def stable
      stable_spec || StableSpec.new(
        url: "https://example.test/test-formula-1.0.tar.gz",
        checksum: StableChecksum.new(hexdigest: "a" * 64),
      )
    end

    def resource(resource_name)
      resources.fetch(resource_name)
    end

    def kandelo_tier2_runtime!
      return tier2_runtime unless tier2_runtime.nil?

      super
    end

    def kandelo_formula_checker_path
      return formula_checker_path unless formula_checker_path.nil?

      super
    end

    def kandelo_formula_binary_cache_root
      return formula_binary_cache_root unless formula_binary_cache_root.nil?

      super
    end

    def kandelo_formula_resolver_repo_root
      return formula_resolver_repo_root unless formula_resolver_repo_root.nil?

      super
    end

    def prefix
      prefix_path || Pathname("/tmp/formula prefix")
    end

    def kandelo_nix_executable
      nix_path || super
    end

    def kandelo_homebrew_prefix
      homebrew_prefix_path || super
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
      config_assignment = Shellwords.shellsplit(command).find do |token|
        token.start_with?("KANDELO_FORMULA_PTY_CONFIG_PATH=")
      end
      if config_assignment
        @pty_config_path = Pathname(config_assignment.delete_prefix("KANDELO_FORMULA_PTY_CONFIG_PATH="))
        @pty_config_mode = @pty_config_path.stat.mode & 0777
        @pty_config = JSON.parse(@pty_config_path.read)
      end
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
      @system_environment = ENV.to_hash
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

  # Simulates the guest-output file contract without launching Node.
  class GuestOutputHarness < Harness
    attr_accessor :guest_output

    def shell_output(command, expected_status = 0)
      output = super
      assignment = Shellwords.shellsplit(command).find do |token|
        token.start_with?("KANDELO_GUEST_OUTPUT_FILE=")
      end
      raise "guest output sink assignment is missing" unless assignment

      output_path = assignment.delete_prefix("KANDELO_GUEST_OUTPUT_FILE=")
      File.binwrite(output_path, guest_output || "guest output\n")
      output
    end
  end

  # Executes Formula commands while retaining the embedding streams separately.
  class RuntimeHarness < Harness
    attr_reader :process_stderr, :process_stdout

    def shell_output(command, expected_status = 0)
      @command = command
      @expected_status = expected_status
      @process_stdout, @process_stderr, status = Open3.capture3(command)
      $stderr.write(process_stderr) unless process_stderr.empty?
      raise "unexpected exit status #{status.exitstatus}" if status.exitstatus != expected_status

      process_stdout
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

  def with_fake_formula_node
    original = ENV.to_hash
    Dir.mktmpdir("kandelo-formula-guest-output-runtime") do |dir|
      root = Pathname(dir)/"kandelo root"
      test_path = Pathname(dir)/"formula test"
      fake_bin = Pathname(dir)/"fake bin"
      root.mkpath
      test_path.mkpath
      fake_bin.mkpath
      fake_node = fake_bin/"node"
      fake_node.binwrite <<~SH
        #!/bin/sh
        printf 'guest stdout\\n' > "$KANDELO_GUEST_OUTPUT_FILE"
        printf 'guest stderr\\n' >> "$KANDELO_GUEST_OUTPUT_FILE"
        printf 'host diagnostic\\n' >&2
        exit 1
      SH
      fake_node.chmod(0755)
      ENV["PATH"] = [fake_bin, ENV.fetch("PATH")].join(File::PATH_SEPARATOR)
      ENV.delete("HOMEBREW_KANDELO_NODE")

      yield root, test_path
    end
  ensure
    ENV.replace(original) if original
  end

  def with_tier2_loader_fixture(tap_name: "kandelo-dev/tap-core", root_basename: "kandelo-root")
    Dir.mktmpdir("kandelo-tier2-loader") do |dir|
      base = Pathname(dir).realpath
      owner, short_tap = tap_name.split("/", 2)
      tap_root = base/owner/"homebrew-#{short_tap}"
      support_path = tap_root/"Kandelo/formula_support/kandelo_formula_support.rb"
      formula_path = tap_root/"Formula/hello.rb"
      prefix = base/"prefix"
      protected_anchor = base/"protected-anchor"
      protected_root = protected_anchor/"build-#{"a" * 64}"
      recipe_runner = protected_root/"homebrew-tap-recipe-runner"
      sealed_root = protected_root/"sealed-outputs"
      root = base/root_basename
      sysroot = root/"sysroot"
      tool_dir = root/"tools/bin"
      fork_instrument = tool_dir/"wasm-fork-instrument"
      local_root_spill = tool_dir/"wasm-local-root-spill"
      [
        support_path.dirname, formula_path.dirname, prefix, protected_anchor,
        protected_root,
        sealed_root, sysroot, tool_dir,
      ].each(&:mkpath)
      protected_anchor.chmod(0711)
      sealed_root.chmod(0555)
      [fork_instrument, local_root_spill, recipe_runner].each do |tool|
        tool.binwrite("#!/bin/sh\nexit 0\n")
        tool.chmod(0555)
      end
      protected_root.chmod(0555)
      FileUtils.cp(File.expand_path("../kandelo_formula_support.rb", __dir__), support_path)
      support_source = support_path.binread.sub(
        '"/run/kandelo-homebrew-publisher".freeze',
        "#{protected_anchor.to_s.inspect}.freeze",
      )
      support_path.binwrite(support_source)
      formula_path.binwrite("class Hello < Formula\nend\n")
      begin
        yield({
          base:,
          fork_instrument:,
          formula_path:,
          local_root_spill:,
          prefix:,
          protected_anchor:,
          protected_root:,
          recipe_runner:,
          root:,
          sealed_root:,
          support_path:,
          sysroot:,
          tap_name:,
          tap_root:,
        })
      ensure
        if protected_anchor.exist?
          protected_anchor.chmod(0755)
          protected_anchor.find do |entry|
            entry.chmod(0755) unless entry.symlink?
          end
        end
      end
    end
  end

  def write_sealed_formula_checker(fixture)
    checker = fixture.fetch(:root)/"target/x86_64-unknown-linux-gnu/release/xtask"
    checker.dirname.mkpath
    checker.binwrite("#!/bin/sh\nexit 0\n")
    checker.chmod(0555)
    checker
  end

  def write_formula_binary_cache(fixture)
    cache_root =
      fixture.fetch(:root)/KandeloFormulaSupport::KANDELO_PORTABLE_BINARY_CACHE_BASENAME
    (cache_root/"programs").mkpath
    cache_root
  end

  def tier2_loader_attestation(fixture, bridge: true, recipe: false)
    raise "loader fixture cannot authorize two build paths" if bridge && recipe

    nested_bridge = if bridge
      {
        "build_toml_sha256"   => "b" * 64,
        "package"             => "hello",
        "package_toml_sha256" => "c" * 64,
        "script"              => "build-hello.sh",
        "script_env_keys"     => [],
        "script_sha256"       => "d" * 64,
        "source_mode"         => "exact",
        "source_sha256"       => "e" * 64,
        "source_url"          => "https://example.test/hello-1.0.tar.gz",
        "version"             => "1.0",
      }
    end
    nested_recipe = if recipe
      {
        "dependencies"     => ["kandelo-dev/tap-core/zlib"],
        "entrypoint"        => "build.sh",
        "file_count"        => 1,
        "manifest_sha256"   => "b" * 64,
        "pkg_version"       => "1.0",
        "resources"         => [],
        "script_env_keys"   => ["HELLO_VALUE"],
        "source_sha256"     => "e" * 64,
        "source_url"        => "https://example.test/hello-1.0.tar.gz",
        "total_bytes"       => 32,
        "version"           => "1.0",
      }
    end
    document = {
      "schema"                 => recipe ? 3 : 2,
      "arch"                   => "wasm32",
      "tap"                    => fixture.fetch(:tap_name),
      "formula"                => "hello",
      "full_name"              => "#{fixture.fetch(:tap_name)}/hello",
      "formula_sha256"         => Digest::SHA256.file(fixture.fetch(:formula_path)).hexdigest,
      "support_runtime_sha256" => support_runtime_sha256(fixture.fetch(:support_path).dirname),
      "support_sha256"         => Digest::SHA256.file(fixture.fetch(:support_path)).hexdigest,
      "tier2_bridge"           => nested_bridge,
    }
    document["tap_recipe"] = nested_recipe if recipe
    document
  end

  def support_runtime_sha256(support_dir)
    entries = support_dir.children.each_with_object({}) do |entry, runtime|
      next if entry.basename.to_s == "test"

      runtime[entry.basename.to_s] = Digest::SHA256.file(entry).hexdigest
    end
    Digest::SHA256.hexdigest(JSON.generate(entries.sort.to_h))
  end

  def write_tier2_loader_attestation(fixture, contents)
    path = fixture.fetch(:prefix)/KandeloFormulaSupport::KANDELO_TIER2_ATTESTATION_BASENAME
    path.binwrite(contents)
    path.chmod(0444)
    path
  end

  def run_tier2_support_load(
    fixture, after_require, environment: {}, homebrew_filtered: false, simulated_owner_uid: nil
  )
    support_paths = environment.fetch("KANDELO_TEST_SUPPORT_PATHS", [fixture.fetch(:support_path)])
    environment = environment.reject { |key, _value| key == "KANDELO_TEST_SUPPORT_PATHS" }
    env = {
      "HOMEBREW_PREFIX"                 => fixture.fetch(:prefix).to_s,
      "HOMEBREW_KANDELO_ARCH"           => "wasm32",
      "HOMEBREW_KANDELO_FORK_INSTRUMENT" => fixture.fetch(:fork_instrument).to_s,
      "HOMEBREW_KANDELO_LOCAL_ROOT_SPILL" => fixture.fetch(:local_root_spill).to_s,
      "HOMEBREW_KANDELO_PRIMARY_TAP_ROOT" => fixture.fetch(:tap_root).to_s,
      "HOMEBREW_KANDELO_ROOT"           => fixture.fetch(:root).to_s,
      "HOMEBREW_KANDELO_SYSROOT"        => fixture.fetch(:sysroot).to_s,
      "HOMEBREW_KANDELO_TAP_RECIPE_RUNNER" => fixture.fetch(:recipe_runner).to_s,
      "HOMEBREW_KANDELO_TAP_RECIPE_SEALED_ROOT" => fixture.fetch(:sealed_root).to_s,
      "KANDELO_HOMEBREW_ARCH"           => "wasm32",
      "KANDELO_HOMEBREW_KANDELO_ROOT"   => fixture.fetch(:root).to_s,
      "WASM_POSIX_SYSROOT"              => fixture.fetch(:sysroot).to_s,
    }.merge(environment)
    source = [
      *(
        if simulated_owner_uid.nil?
          []
        else
          [
            "class File::Stat",
            "  def uid = #{Integer(simulated_owner_uid)}",
            "  def gid = 0",
            "end",
          ]
        end
      ),
      "class Requirement",
      "  def self.fatal(*) = nil",
      "  def self.satisfy(**_options, &_block) = nil",
      "end",
      *support_paths.map { |path| "require #{path.to_s.inspect}" },
      after_require,
    ].join("\n")
    if homebrew_filtered
      # `bin/brew` rebuilds Formula evaluation's environment from a fixed
      # allowlist plus every HOMEBREW_* value. None of this fixture's fixed
      # allowlist values carry publisher authority, so retain only that prefix.
      env = env.select { |key, _value| key.start_with?("HOMEBREW_") }
      Open3.capture3(env, RbConfig.ruby, "-e", source, unsetenv_others: true)
    else
      Open3.capture3(env, RbConfig.ruby, "-e", source)
    end
  end

  def with_cross_tap_loader_fixture
    with_tier2_loader_fixture(tap_name: "brandonpayton/kandelo-canary") do |fixture|
      dependency_tap_root = fixture.fetch(:base)/"kandelo-dev/homebrew-tap-core"
      dependency_support_path =
        dependency_tap_root/"Kandelo/formula_support/kandelo_formula_support.rb"
      dependency_support_path.dirname.mkpath
      FileUtils.cp(fixture.fetch(:support_path), dependency_support_path)
      primary_test_root = fixture.fetch(:support_path).dirname/"test"
      dependency_test_root = dependency_support_path.dirname/"test"
      primary_test_root.mkpath
      dependency_test_root.mkpath
      (primary_test_root/"tap-local.txt").binwrite("primary-only test bytes\n")
      (dependency_test_root/"tap-local.txt").binwrite("dependency-only test bytes\n")
      yield fixture.merge(dependency_support_path:, dependency_tap_root:)
    end
  end

  def with_tier2_build_fixture(script_env: nil, formula_name: "hello", package_name: formula_name)
    original = ENV.to_hash
    Dir.mktmpdir("kandelo-tier2-build") do |dir|
      base = Pathname(dir).realpath
      root = base/"kandelo-root"
      registry_root = root/"packages/registry"
      package_root = registry_root/package_name
      sysroot = root/"sysroot"
      build_path = base/"formula-build"
      formula_path = base/"#{formula_name}.rb"
      support_path = base/"kandelo_formula_support.rb"
      [package_root, sysroot, build_path].each(&:mkpath)
      package_toml = package_root/"package.toml"
      build_toml = package_root/"build.toml"
      script = package_root/"build-#{package_name}.sh"
      package_toml.binwrite("name = #{package_name.inspect}\nversion = \"1.0\"\n")
      build_toml.binwrite("script_path = \"packages/registry/#{package_name}/build-#{package_name}.sh\"\n")
      script.binwrite("#!/usr/bin/env bash\nset -euo pipefail\n")
      formula_path.binwrite("class #{formula_name.capitalize} < Formula\nend\n")
      FileUtils.cp(File.expand_path("../kandelo_formula_support.rb", __dir__), support_path)
      (build_path/"upstream.c").binwrite("int main(void) { return 0; }\n")
      resource_dir = build_path/"kandelo-package-resources"
      (resource_dir/"resource").mkpath
      (resource_dir/"resource/input.txt").binwrite("verified resource\n")
      script_env ||= {
        "HELLO_RESOURCE"                    => resource_dir/"resource",
        "WASM_POSIX_DEP_PKG_CONFIG_PATH"    => "/formula/pkgconfig",
      }
      bridge = {
        "build_toml_sha256"   => Digest::SHA256.file(build_toml).hexdigest,
        "package"             => package_name,
        "package_toml_sha256" => Digest::SHA256.file(package_toml).hexdigest,
        "script"              => "build-#{package_name}.sh",
        "script_env_keys"     => script_env.keys.sort,
        "script_sha256"       => Digest::SHA256.file(script).hexdigest,
        "source_mode"         => "exact",
        "source_sha256"       => "a" * 64,
        "source_url"          => "https://example.test/hello-1.0.tar.gz",
        "version"             => "1.0",
      }
      attestation = {
        "schema"          => 1,
        "arch"            => "wasm32",
        "tap"             => "kandelo-dev/tap-core",
        "formula"         => formula_name,
        "full_name"       => "kandelo-dev/tap-core/#{formula_name}",
        "formula_sha256"  => Digest::SHA256.file(formula_path).hexdigest,
        "support_sha256"  => Digest::SHA256.file(support_path).hexdigest,
        "tier2_bridge"    => bridge,
      }
      trusted_env = KandeloFormulaSupport::KANDELO_TIER2_TRUSTED_ENV_KEYS.to_h { |key| [key, nil] }
      trusted_env.merge!({
        "HOMEBREW_KANDELO_ARCH"         => "wasm32",
        "HOMEBREW_KANDELO_ROOT"         => root.to_s,
        "HOMEBREW_KANDELO_SYSROOT"      => sysroot.to_s,
        "KANDELO_HOMEBREW_ARCH"         => "wasm32",
        "KANDELO_HOMEBREW_KANDELO_ROOT" => root.to_s,
        "WASM_POSIX_SYSROOT"            => sysroot.to_s,
      })
      runtime = {
        "attestation"               => attestation,
        "attestation_path"          => (base/"attestation.json").to_s,
        "formula_binary_cache_root" => nil,
        "formula_checker_path"      => nil,
        "formula_path"              => formula_path.to_s,
        "support_path"              => support_path.to_s,
        "support_sha256"            => attestation.fetch("support_sha256"),
        "trusted_env"               => trusted_env,
      }
      activation_calls = []
      harness = Harness.new
      harness.build_path = build_path
      harness.formula_name = formula_name
      harness.formula_full_name = "kandelo-dev/tap-core/#{formula_name}"
      harness.formula_path = formula_path
      harness.formula_version = "1.0"
      harness.root_path = root.to_s
      harness.stable_spec = StableSpec.new(
        url: bridge.fetch("source_url"),
        checksum: StableChecksum.new(hexdigest: bridge.fetch("source_sha256")),
      )
      harness.tier2_runtime = runtime
      harness.define_singleton_method(:kandelo_activate_sdk!) do
        activation_calls << :sdk
        ENV["WASM_POSIX_DEP_PKG_CONFIG_PATH"] = "/sdk/overwrote-formula-value"
        root.to_s
      end
      harness.define_singleton_method(:kandelo_activate_sysroot!) do |activated_root|
        activation_calls << :sysroot
        raise "wrong activation root" unless activated_root == root.to_s

        ENV["WASM_POSIX_SYSROOT"] = sysroot.to_s
        activated_root
      end

      yield({
        activation_calls:,
        bridge:,
        build_path:,
        build_toml:,
        formula_path:,
        harness:,
        package_toml:,
        resource_dir:,
        root:,
        script:,
        script_env:,
        support_path:,
      })
    end
  ensure
    ENV.replace(original) if original
  end

  def set_tap_recipe_fixture_seal!(tap_root, sealed:)
    directories = []
    tap_root.find do |entry|
      stat = entry.lstat
      next if stat.symlink?

      if stat.directory?
        directories << entry
      elsif stat.file?
        executable = (stat.mode & 0111).positive?
        entry.chmod(
          if sealed
            executable ? 0555 : 0444
          else
            executable ? 0755 : 0644
          end,
        )
      end
    end
    directory_mode = sealed ? 0555 : 0755
    directories.reverse_each { |directory| directory.chmod(directory_mode) }
  end

  def mutate_sealed_tap_recipe_fixture!(fixture)
    tap_root = fixture.fetch(:tap_root)
    set_tap_recipe_fixture_seal!(tap_root, sealed: false)
    yield
  ensure
    set_tap_recipe_fixture_seal!(tap_root, sealed: true) if tap_root&.exist?
  end

  def with_tap_recipe_build_fixture(
    script_env: nil, formula_name: "hello", resource_records: []
  )
    original = ENV.to_hash
    Dir.mktmpdir("kandelo-tap-recipe-build") do |dir|
      base = Pathname(dir).realpath
      tap_root = base/"kandelo-dev/homebrew-tap-core"
      recipe_root = tap_root/"Kandelo/recipes"/formula_name
      support_path = tap_root/"Kandelo/formula_support/kandelo_formula_support.rb"
      formula_path = tap_root/"Formula/#{formula_name}.rb"
      root = base/"kandelo-root"
      protected_root = base/"protected"
      recipe_runner = protected_root/"homebrew-tap-recipe-runner"
      sealed_root = protected_root/"sealed-outputs"
      sysroot = root/"sysroot"
      tool_dir = root/"tools/bin"
      fork_instrument = tool_dir/"wasm-fork-instrument"
      local_root_spill = tool_dir/"wasm-local-root-spill"
      build_path = base/"formula-build"
      prefix_path = base/"formula-prefix"
      runner_output = base/"recipe-service-output"
      [
        recipe_root/"patches", support_path.dirname, formula_path.dirname,
        protected_root, sealed_root, sysroot, tool_dir, build_path,
      ].each(&:mkpath)
      [fork_instrument, local_root_spill, recipe_runner].each do |tool|
        tool.binwrite("#!/bin/sh\nexit 0\n")
        tool.chmod(0555)
      end
      FileUtils.cp(File.expand_path("../kandelo_formula_support.rb", __dir__), support_path)
      formula_path.binwrite("class Hello < Formula\nend\n")
      (build_path/"upstream.c").binwrite("int main(void) { return 0; }\n")

      entrypoint = recipe_root/"build.sh"
      patch = recipe_root/"patches/config.patch"
      entrypoint.binwrite("#!/usr/bin/env bash\nset -euo pipefail\n")
      entrypoint.chmod(0755)
      patch.binwrite("--- a/configure\n+++ b/configure\n")
      records = [entrypoint, patch].map do |path|
        relative = path.relative_path_from(recipe_root).to_s
        contents = path.binread
        {
          "bytes"  => contents.bytesize,
          "mode"   => (path.stat.mode & 0111).positive? ? "0755" : "0644",
          "path"   => relative,
          "sha256" => Digest::SHA256.hexdigest(contents),
        }
      end.sort_by { |record| record.fetch("path") }
      manifest = {
        "schema"       => 1,
        "dependencies" => ["kandelo-dev/tap-core/zlib"],
        "entrypoint"   => "build.sh",
        "files"        => records,
      }
      manifest_path = recipe_root/"recipe.json"
      manifest_path.binwrite("#{JSON.pretty_generate(manifest)}\n")

      script_env ||= { "HELLO_VALUE" => "attested-value" }
      recipe = {
        "dependencies"     => manifest.fetch("dependencies"),
        "entrypoint"        => manifest.fetch("entrypoint"),
        "file_count"        => records.length,
        "manifest_sha256"   => Digest::SHA256.file(manifest_path).hexdigest,
        "pkg_version"       => "1.0",
        "resources"         => resource_records,
        "script_env_keys"   => script_env.keys.sort,
        "source_sha256"     => "a" * 64,
        "source_url"        => "https://example.test/hello-1.0.tar.gz",
        "total_bytes"       => records.sum { |record| record.fetch("bytes") },
        "version"           => "1.0",
      }
      attestation = {
        "schema"                 => 3,
        "arch"                   => "wasm32",
        "tap"                    => "kandelo-dev/tap-core",
        "formula"                => formula_name,
        "full_name"              => "kandelo-dev/tap-core/#{formula_name}",
        "formula_sha256"         => Digest::SHA256.file(formula_path).hexdigest,
        "support_runtime_sha256" => support_runtime_sha256(support_path.dirname),
        "support_sha256"         => Digest::SHA256.file(support_path).hexdigest,
        "tap_recipe"             => recipe,
        "tier2_bridge"           => nil,
      }
      trusted_env =
        KandeloFormulaSupport::KANDELO_TIER2_TRUSTED_ENV_KEYS.to_h { |key| [key, nil] }
      trusted_env.merge!({
        "HOMEBREW_KANDELO_ARCH"             => "wasm32",
        "HOMEBREW_KANDELO_FORK_INSTRUMENT"  => fork_instrument.to_s,
        "HOMEBREW_KANDELO_LOCAL_ROOT_SPILL" => local_root_spill.to_s,
        "HOMEBREW_KANDELO_PRIMARY_TAP_ROOT" => tap_root.to_s,
        "HOMEBREW_KANDELO_ROOT"             => root.to_s,
        "HOMEBREW_KANDELO_SYSROOT"          => sysroot.to_s,
        "HOMEBREW_KANDELO_TAP_RECIPE_RUNNER" => recipe_runner.to_s,
        "HOMEBREW_KANDELO_TAP_RECIPE_SEALED_ROOT" => sealed_root.to_s,
        "HOMEBREW_KANDELO_XTASK_BIN"        => "/must/not/reach/recipe",
        "KANDELO_HOMEBREW_ARCH"             => "wasm32",
        "KANDELO_HOMEBREW_KANDELO_ROOT"     => root.to_s,
        "WASM_POSIX_SYSROOT"                => sysroot.to_s,
      })
      runtime = {
        "attestation"               => attestation,
        "attestation_path"          => (base/"attestation.json").to_s,
        "formula_binary_cache_root" => (root/".ci-test-binary-cache").to_s,
        "formula_checker_path"      => "/must/not/reach/recipe",
        "formula_path"              => formula_path.to_s,
        "support_path"              => support_path.to_s,
        "support_runtime_sha256"    => attestation.fetch("support_runtime_sha256"),
        "support_sha256"            => attestation.fetch("support_sha256"),
        "tap_recipe_runner_path"    => recipe_runner.to_s,
        "tap_recipe_runner_uid"     => Process.uid,
        "tap_recipe_sealed_root"    => sealed_root.to_s,
        "tap_recipe_tools"          => {
          "fork_instrument"  => fork_instrument.to_s,
          "local_root_spill" => local_root_spill.to_s,
        },
        "trusted_env"               => trusted_env,
      }

      dependency_name = recipe.fetch("dependencies").first
      dependency_rack = base/"Cellar/zlib"
      dependency_keg = dependency_rack/"1.3.1"
      (dependency_keg/"lib").mkpath
      dependency_formula = DependencyFormula.new(
        full_name: dependency_name,
        opt_bin: dependency_keg/"bin",
        opt_sbin: dependency_keg/"sbin",
        opt_libexec: dependency_keg/"libexec",
      )
      activation_calls = []
      runner_requests = []
      runner_return_hooks = []
      system_hooks = []
      harness = Harness.new
      harness.build_path = build_path
      harness.dependency_formulae = {
        dependency_name => InstalledFormula.new(rack: dependency_rack, pkg_version: "1.3.1"),
      }
      harness.formula_name = formula_name
      harness.formula_full_name = "kandelo-dev/tap-core/#{formula_name}"
      harness.formula_path = formula_path
      harness.formula_version = "1.0"
      harness.prefix_path = prefix_path
      harness.root_path = root.to_s
      harness.runtime_formulae = [dependency_formula]
      harness.resources = resource_records.to_h do |resource_record|
        resource_name = resource_record.fetch("name")
        staged_source = base/"resource-sources"/resource_name
        staged_source.mkpath
        (staged_source/"input.txt").binwrite("verified #{resource_name} resource\n")
        [
          resource_name,
          StagedResource.new(
            url: resource_record.fetch("source_url"),
            checksum: StableChecksum.new(
              hexdigest: resource_record.fetch("source_sha256"),
            ),
            source: staged_source,
          ),
        ]
      end
      harness.stable_spec = StableSpec.new(
        url: recipe.fetch("source_url"),
        checksum: StableChecksum.new(hexdigest: recipe.fetch("source_sha256")),
      )
      harness.define_singleton_method(:kandelo_tap_recipe_runtime!) { [runtime, recipe] }
      harness.define_singleton_method(:kandelo_activate_sdk!) do
        activation_calls << :sdk
        raise "registry checker leaked into tap recipe" if ENV.key?("HOMEBREW_KANDELO_XTASK_BIN")
        raise "registry cache leaked into tap recipe" if ENV.key?("WASM_POSIX_BINARY_CACHE_ROOT")

        ENV["WASM_POSIX_XTASK_BIN"] = "/sdk/attempted-resolver-authority"
        root.to_s
      end
      harness.define_singleton_method(:kandelo_activate_sysroot!) do |activated_root|
        activation_calls << :sysroot
        raise "wrong activation root" unless activated_root == root.to_s

        ENV["WASM_POSIX_SYSROOT"] = sysroot.to_s
        activated_root
      end
      harness.define_singleton_method(:system) do |*args|
        Harness.instance_method(:system).bind_call(self, *args)
        unless args.length == 5 && args.fetch(0) == recipe_runner.to_s &&
               args.fetch(1) == "--request" && args.fetch(3) == "--response"
          raise "unexpected tap recipe runner invocation: #{args.inspect}"
        end
        request_path = Pathname(args.fetch(2))
        response_path = Pathname(args.fetch(4))
        request_bytes = request_path.binread
        request = JSON.parse(request_bytes)
        runner_requests << request
        out_dir = Pathname(request.fetch("output_root"))
        # Model the real runner's mount namespace: the recipe writes to its
        # private output view, while the Formula-owned host path stays empty.
        (runner_output/"bin").mkpath
        (runner_output/"lib").mkpath
        (runner_output/"libexec/tools").mkpath
        (runner_output/"share/hello").mkpath
        (runner_output/"bin/hello").binwrite("tap recipe output\n")
        (runner_output/"bin/hello").chmod(0755)
        (runner_output/"bin/readme.txt").binwrite("non-executable bin data\n")
        (runner_output/"libexec/tools/helper").binwrite("nested helper\n")
        (runner_output/"libexec/tools/helper").chmod(0755)
        (runner_output/"lib/libhello.a").binwrite("archive payload\n")
        (runner_output/"share/hello/data.txt").binwrite("nested data\n")
        (runner_output/"share/hello/run-helper").binwrite(
          "executable shared helper\n",
        )
        (runner_output/"share/hello/run-helper").chmod(0755)
        (runner_output/"bin/hello-current").make_symlink(
          "../libexec/tools/helper",
        )
        system_hooks.each(&:call)
        unless out_dir.directory? && out_dir.children.empty?
          raise "runner changed the Formula-owned host output root"
        end
        kandelo_validate_tap_recipe_output!(runner_output)

        request_sha256 = Digest::SHA256.hexdigest(request_bytes)
        sealed_out = sealed_root/"output-#{request_sha256.slice(0, 16)}"
        sealed_out.mkdir
        FileUtils.cp_r(runner_output.children, sealed_out)
        sealed_directories = []
        sealed_out.find do |entry|
          stat = entry.lstat
          if stat.directory? && !stat.symlink?
            sealed_directories << entry
          elsif stat.file? && !stat.symlink?
            entry.chmod((stat.mode & 0111).zero? ? 0444 : 0555)
          end
        end
        sealed_directories.reverse_each { |directory| directory.chmod(0555) }
        evidence = kandelo_validate_tap_recipe_output!(
          sealed_out, expected_uid: Process.uid, sealed: true
        )
        response_path.binwrite(JSON.generate({
          "entry_count"            => evidence.fetch("entry_count"),
          "output_manifest_sha256" => evidence.fetch("output_manifest_sha256"),
          "request_sha256"         => request_sha256,
          "schema"                 => 1,
          "sealed_output_root"     => sealed_out.to_s,
          "total_bytes"            => evidence.fetch("total_bytes"),
        }))
        response_path.chmod(0444)
        runner_return_hooks.each { |hook| hook.call(sealed_out, response_path) }
        # Match Homebrew's Formula#system contract: it raises on failure and
        # returns nil after a successful command.
        nil
      end

      # Match the launcher boundary exactly: every overlay directory is 0555,
      # executable files are 0555, and data files are 0444 before Formula code
      # receives the protected primary-tap path.
      set_tap_recipe_fixture_seal!(tap_root, sealed: true)
      begin
        yield({
          activation_calls:,
          build_path:,
          dependency_keg:,
          entrypoint:,
          fork_instrument:,
          formula_path:,
          harness:,
          manifest_path:,
          local_root_spill:,
          patch:,
          prefix_path:,
          recipe:,
          recipe_runner:,
          recipe_root:,
          root:,
          runner_requests:,
          runner_return_hooks:,
          runner_output:,
          sealed_root:,
          script_env:,
          support_path:,
          system_hooks:,
          tap_root:,
        })
      ensure
        set_tap_recipe_fixture_seal!(tap_root, sealed: false) if tap_root.exist?
        if sealed_root.exist?
          sealed_root.find do |entry|
            entry.chmod(0755) unless entry.symlink?
          end
        end
      end
    end
  ensure
    ENV.replace(original) if original
  end

  def assert_tap_recipe_rejected_before_activation(
    fixture,
    resources: fixture.fetch(:recipe).fetch("resources").map { |record| record.fetch("name") },
    script_env: fixture.fetch(:script_env)
  )
    error = assert_raises(RuntimeError) do
      fixture.fetch(:harness).kandelo_build_tap_recipe(
        manifest_sha256: fixture.fetch(:recipe).fetch("manifest_sha256"),
        resources:,
        script_env:,
      )
    end
    assert_empty fixture.fetch(:activation_calls)
    assert_nil fixture.fetch(:harness).system_calls
    assert_path_exists fixture.fetch(:build_path)/"upstream.c"
    refute_path_exists fixture.fetch(:build_path)/"kandelo-package-source"
    error
  end

  def run_tap_recipe(
    fixture,
    resources: fixture.fetch(:recipe).fetch("resources").map { |record| record.fetch("name") },
    script_env: fixture.fetch(:script_env)
  )
    fixture.fetch(:harness).kandelo_build_tap_recipe(
      manifest_sha256: fixture.fetch(:recipe).fetch("manifest_sha256"),
      resources:,
      script_env:,
    )
  end

  def tap_recipe_tree_snapshot(root)
    root.find.map do |entry|
      stat = entry.lstat
      relative = entry.relative_path_from(root).to_s
      payload =
        if stat.file? && !stat.symlink?
          entry.binread
        elsif stat.symlink?
          entry.readlink.to_s
        end
      [relative, stat.ftype, stat.mode & 07777, stat.uid, stat.gid, payload]
    end.sort_by(&:first)
  end

  def assert_tier2_rejected_before_activation(fixture, script_env: fixture.fetch(:script_env))
    error = assert_raises(RuntimeError) do
      fixture.fetch(:harness).kandelo_build_package(script_env:)
    end
    assert_empty fixture.fetch(:activation_calls)
    assert_nil fixture.fetch(:harness).system_calls
    assert_path_exists fixture.fetch(:build_path)/"upstream.c"
    refute_path_exists fixture.fetch(:build_path)/"kandelo-package-source"
    error
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
      target = "kandelo-dev/tap-core/openssl"
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
    target = "kandelo-dev/tap-core/openssl"
    harness.dependency_formulae = {
      target => InstalledFormula.new(rack: Pathname("/missing/Cellar/openssl"), pkg_version: "3.3.2_2"),
    }

    error = assert_raises(RuntimeError) { harness.formula_opt_prefix(target) }
    assert_includes error.message, "is not installed at /missing/Cellar/openssl/3.3.2_2"
  end

  def test_tap_recipe_native_roots_use_declared_versioned_build_kegs_only
    Dir.mktmpdir("kandelo-native-build-roots") do |dir|
      base = Pathname(dir)
      native_rack = base/"Cellar/cmake"
      native_keg = native_rack/"4.1.0"
      target_rack = base/"Cellar/zlib"
      target_keg = target_rack/"1.3.1"
      [native_keg, target_keg].each(&:mkpath)
      formula_class = Struct.new(:full_name, :rack, :pkg_version, keyword_init: true)
      dependency_class = Struct.new(:formula, :build_tag, :test_tag, keyword_init: true) do
        def build? = build_tag
        def test? = test_tag
        def to_formula = formula
      end
      native = dependency_class.new(
        formula: formula_class.new(
          full_name: "cmake", rack: native_rack, pkg_version: "4.1.0"
        ),
        build_tag: true,
        test_tag: false,
      )
      target = dependency_class.new(
        formula: formula_class.new(
          full_name: "kandelo-dev/tap-core/zlib",
          rack: target_rack,
          pkg_version: "1.3.1",
        ),
        build_tag: true,
        test_tag: false,
      )
      runtime_only = dependency_class.new(
        formula: formula_class.new(
          full_name: "libidn2", rack: base/"Cellar/libidn2", pkg_version: "2.3.8"
        ),
        build_tag: false,
        test_tag: false,
      )
      harness = Harness.new
      harness.formula_full_name = "kandelo-dev/tap-core/hello"
      harness.define_singleton_method(:deps) { [target, runtime_only, native] }

      assert_equal [native_keg.to_s], harness.kandelo_tap_recipe_native_build_roots
    end
  end

  def test_verified_formula_source_is_isolated_from_bridge_work_and_output_roots
    Dir.mktmpdir("kandelo-formula-source") do |dir|
      build_path = Pathname(dir)/"build"
      (build_path/"src").mkpath
      (build_path/"src/main.c").write("int main(void) { return 0; }\n")
      (build_path/".upstream-marker").write("verified source\n")
      harness = Harness.new
      harness.build_path = build_path

      source_dir = harness.kandelo_stage_verified_formula_source

      assert_equal build_path/"kandelo-package-source", source_dir
      assert_equal "int main(void) { return 0; }\n", (source_dir/"src/main.c").read
      assert_equal "verified source\n", (source_dir/".upstream-marker").read
      assert_equal [source_dir], build_path.children
      refute_path_exists build_path/"kandelo-package-work"
      refute_path_exists build_path/"kandelo-package-out"

      error = assert_raises(RuntimeError) { harness.kandelo_stage_verified_formula_source }
      assert_includes error.message, "Formula source was already staged"
    end
  end

  def test_verified_formula_source_excludes_homebrew_stage_home
    Dir.mktmpdir("kandelo-formula-stage-home") do |dir|
      build_path = Pathname(dir)/"build"
      stage_home = build_path/".brew_home"
      stage_home.mkpath
      (stage_home/".bazelrc").write("startup --output_user_root=/tmp/bazel\n")
      (stage_home/".gitignore").write("*\n")
      (build_path/"upstream.c").write("int main(void) { return 0; }\n")
      harness = Harness.new
      harness.build_path = build_path

      source_dir = harness.kandelo_stage_verified_formula_source

      assert_equal "int main(void) { return 0; }\n",
                   (source_dir/"upstream.c").read
      refute_path_exists source_dir/".brew_home"
      assert_equal "startup --output_user_root=/tmp/bazel\n",
                   (stage_home/".bazelrc").read
      assert_equal "*\n", (stage_home/".gitignore").read
    end
  end

  def test_verified_formula_source_rejects_an_empty_buildpath
    Dir.mktmpdir("kandelo-empty-formula-source") do |dir|
      harness = Harness.new
      harness.build_path = Pathname(dir)

      error = assert_raises(RuntimeError) { harness.kandelo_stage_verified_formula_source }
      assert_includes error.message, "did not stage Formula source"
    end
  end

  def test_tap_recipe_helper_uses_only_attested_inputs_and_homebrew_dependency_kegs
    with_tap_recipe_build_fixture do |fixture|
      ENV["HELLO_AMBIENT"] = "remove-me"
      ENV["GITHUB_TOKEN"] = "must-not-reach-recipe"
      ENV["HOMEBREW_KANDELO_XTASK_BIN"] = "/ambient/xtask"
      ENV["HOMEBREW_GITHUB_PACKAGES_TOKEN"] = "must-not-reach-recipe"
      ENV["NIX_PATH"] = "/ambient/nixpkgs"
      ENV["WASM_POSIX_BINARY_CACHE_ROOT"] = "/ambient/cache"
      ENV["WASM_POSIX_BINARY_INDEX_URL"] = "https://ambient.invalid/index.toml"
      ENV["WASM_POSIX_BINARY_RESOLVER_REPO_ROOT"] = "/ambient/root"
      ENV["WASM_POSIX_DEPS_REGISTRY"] = "/ambient/registry"
      ENV["WASM_POSIX_FORK_INSTRUMENT"] = "/ambient/fork-instrument"
      ENV["WASM_POSIX_LOCAL_BIN_DIR"] = "/ambient/local-binaries"
      ENV["WASM_POSIX_LOCAL_ROOT_SPILL"] = "/ambient/local-root-spill"
      ENV["WASM_POSIX_XTASK_BIN"] = "/ambient/xtask"

      out_dir = run_tap_recipe(fixture)

      assert_equal(
        fixture.fetch(:build_path)/"kandelo-package-out",
        out_dir.parent,
      )
      assert_match(
        /\A\.kandelo-materializing-/,
        out_dir.basename.to_s,
      )
      output_root = out_dir.parent
      assert_equal 0700, output_root.lstat.mode & 07777
      assert_equal Process.uid, output_root.lstat.uid
      assert_equal [:sdk, :sysroot], fixture.fetch(:activation_calls)
      assert_equal fixture.fetch(:recipe_runner).to_s,
                   fixture.fetch(:harness).system_args.fetch(0)
      refute fixture.fetch(:recipe_runner).to_s.start_with?("#{fixture.fetch(:root)}/")
      assert_equal "--request", fixture.fetch(:harness).system_args.fetch(1)
      assert_equal "--response", fixture.fetch(:harness).system_args.fetch(3)
      request = fixture.fetch(:runner_requests).fetch(0)
      assert_equal %w[
        arch dependencies entrypoint environment formula limits
        manifest_sha256 native_roots output_root platform_root recipe_root resources schema
        source_root sysroot version work_root
      ], request.keys.sort
      assert_equal 1, request.fetch("schema")
      assert_empty request.fetch("native_roots")
      assert_equal fixture.fetch(:entrypoint).to_s, request.fetch("entrypoint")
      assert_equal fixture.fetch(:build_path).join("kandelo-package-out").to_s,
                   request.fetch("output_root")
      assert_equal(
        { "WASM_POSIX_DEP_ZLIB_DIR" => fixture.fetch(:dependency_keg).to_s },
        request.fetch("dependencies"),
      )
      assert_empty request.fetch("resources")
      environment = request.fetch("environment")
      refute_equal fixture.fetch(:harness).system_environment, environment
      %w[
        GITHUB_TOKEN HOMEBREW_GITHUB_PACKAGES_TOKEN
        HOMEBREW_KANDELO_TAP_RECIPE_RUNNER
        HOMEBREW_KANDELO_TAP_RECIPE_SEALED_ROOT NIX_PATH
      ].each { |key| refute environment.key?(key), key }
      assert_equal "kandelo-homebrew-recipe", environment.fetch("USER")
      assert_equal "kandelo-homebrew-recipe", environment.fetch("LOGNAME")
      assert_equal fixture.fetch(:build_path).join("kandelo-package-work/home").to_s,
                   environment.fetch("HOME")
      assert_equal fixture.fetch(:build_path).join("kandelo-package-work/tmp").to_s,
                   environment.fetch("TMPDIR")
      assert_equal [
        fixture.fetch(:root)/"sdk/bin",
        fixture.fetch(:root)/"tools/bin",
        Pathname("/usr/bin"),
        Pathname("/bin"),
      ].join(File::PATH_SEPARATOR), environment.fetch("PATH")
      assert_equal "hello", environment.fetch("WASM_POSIX_DEP_NAME")
      assert_equal "1.0", environment.fetch("WASM_POSIX_DEP_PKG_VERSION")
      assert_equal "attested-value", environment.fetch("HELLO_VALUE")
      assert_equal fixture.fetch(:recipe_root).to_s,
                   environment.fetch("WASM_POSIX_DEP_RECIPE_DIR")
      assert_equal fixture.fetch(:dependency_keg).to_s,
                   environment.fetch("WASM_POSIX_DEP_ZLIB_DIR")
      assert_equal fixture.fetch(:build_path).join("kandelo-package-source").to_s,
                   environment.fetch("WASM_POSIX_DEP_SOURCE_DIR")
      assert_equal fixture.fetch(:build_path).join("kandelo-package-work").to_s,
                   environment.fetch("WASM_POSIX_DEP_WORK_DIR")
      assert_equal fixture.fetch(:build_path).join("kandelo-package-out").to_s,
                   environment.fetch("WASM_POSIX_DEP_OUT_DIR")
      assert_equal fixture.fetch(:fork_instrument).to_s,
                   environment.fetch("WASM_POSIX_FORK_INSTRUMENT")
      assert_equal fixture.fetch(:local_root_spill).to_s,
                   environment.fetch("WASM_POSIX_LOCAL_ROOT_SPILL")
      %w[
        HOMEBREW_KANDELO_XTASK_BIN WASM_POSIX_BINARY_CACHE_ROOT
        WASM_POSIX_BINARY_INDEX_URL WASM_POSIX_BINARY_RESOLVER_REPO_ROOT
        WASM_POSIX_DEPS_REGISTRY WASM_POSIX_LOCAL_BIN_DIR WASM_POSIX_XTASK_BIN
      ].each { |key| refute environment.key?(key), key }
      refute environment.key?("HELLO_AMBIENT")
      assert_equal "int main(void) { return 0; }\n",
                   (fixture.fetch(:build_path)/"kandelo-package-source/upstream.c").binread
      assert_equal "tap recipe output\n", (out_dir/"bin/hello").binread
      assert_equal(
        Pathname("../libexec/tools/helper"),
        (out_dir/"bin/hello-current").readlink,
      )
      assert_equal "nested helper\n", (out_dir/"libexec/tools/helper").binread
      assert_equal "archive payload\n", (out_dir/"lib/libhello.a").binread
      assert_equal "nested data\n", (out_dir/"share/hello/data.txt").binread
      assert_equal "executable shared helper\n",
                   (out_dir/"share/hello/run-helper").binread
      assert_equal "non-executable bin data\n",
                   (out_dir/"bin/readme.txt").binread
      expected_file_modes = {
        "bin/hello"                 => 0755,
        "bin/readme.txt"            => 0644,
        "lib/libhello.a"            => 0644,
        "libexec/tools/helper"      => 0755,
        "share/hello/data.txt"      => 0644,
        "share/hello/run-helper"    => 0755,
      }
      observed_files = []
      out_dir.find do |entry|
        stat = entry.lstat
        assert_equal Process.uid, stat.uid, entry.to_s
        next if stat.symlink?

        if stat.directory?
          assert_equal 0755, stat.mode & 07777, entry.to_s
        else
          relative = entry.relative_path_from(out_dir).to_s
          observed_files << relative
          assert_equal(
            expected_file_modes.fetch(relative),
            stat.mode & 07777,
            entry.to_s,
          )
        end
      end
      assert_equal expected_file_modes.keys.sort, observed_files.sort
      refute_path_exists fixture.fetch(:prefix_path)

      # Formula evaluation must not permanently rewrite the caller's process
      # environment, even when the build succeeds.
      assert_equal "/ambient/cache", ENV.fetch("WASM_POSIX_BINARY_CACHE_ROOT")
      assert_equal "/ambient/fork-instrument", ENV.fetch("WASM_POSIX_FORK_INSTRUMENT")
      assert_equal "/ambient/local-root-spill", ENV.fetch("WASM_POSIX_LOCAL_ROOT_SPILL")
      assert_equal "/ambient/xtask", ENV.fetch("WASM_POSIX_XTASK_BIN")
    end
  end

  def test_tap_recipe_helper_exposes_formula_and_package_versions
    with_tap_recipe_build_fixture do |fixture|
      # Model a publisher process whose ambient environment is already
      # polluted. The sealed recipe attestation, not that process state, owns
      # the package identity handed to the privileged runner.
      ENV["WASM_POSIX_DEP_PKG_VERSION"] = "ambient-poison"
      fixture.fetch(:harness).formula_pkg_version = "1.0_7"
      fixture.fetch(:recipe)["pkg_version"] = "1.0_7"

      run_tap_recipe(fixture)

      request = fixture.fetch(:runner_requests).fetch(0)
      environment = request.fetch("environment")
      assert_equal "1.0", environment.fetch("WASM_POSIX_DEP_VERSION")
      assert_equal "1.0_7", environment.fetch("WASM_POSIX_DEP_PKG_VERSION")
      assert_equal "1.0", request.fetch("version")
      assert_equal "ambient-poison", ENV.fetch("WASM_POSIX_DEP_PKG_VERSION")
    end
  end

  def test_tap_recipe_helper_rejects_package_version_attestation_drift
    with_tap_recipe_build_fixture do |fixture|
      fixture.fetch(:harness).formula_pkg_version = "1.0_7"

      error = assert_tap_recipe_rejected_before_activation(fixture)

      assert_includes error.message, "Formula identity differs"
    end
  end

  def test_tap_recipe_helper_owns_the_package_version_environment
    with_tap_recipe_build_fixture(
      script_env: { "WASM_POSIX_DEP_PKG_VERSION" => "1.0_7" },
    ) do |fixture|
      error = assert_tap_recipe_rejected_before_activation(fixture)

      assert_includes error.message, "helper-owned key"
    end
  end

  def test_tap_recipe_helper_propagates_runner_failure_and_cleans_protocol_files
    with_tap_recipe_build_fixture do |fixture|
      message =
        "homebrew-tap-recipe-runner: recipe diagnostics: compiler failed"
      request_path =
        fixture.fetch(:build_path)/".kandelo-tap-recipe-request.json"
      response_path =
        fixture.fetch(:build_path)/".kandelo-tap-recipe-response.json"
      fixture.fetch(:system_hooks) << lambda do
        response_path.binwrite("incomplete runner response")
        raise message
      end

      error = assert_raises(RuntimeError) { run_tap_recipe(fixture) }

      assert_equal message, error.message
      refute_path_exists request_path
      refute_path_exists response_path
    end
  end

  def test_tap_recipe_materialization_survives_real_homebrew_install_moves
    with_tap_recipe_build_fixture do |fixture|
      sealed_before = nil
      fixture.fetch(:runner_return_hooks) << lambda do |sealed, _response|
        sealed_before = tap_recipe_tree_snapshot(sealed)
      end
      out_dir = run_tap_recipe(fixture)
      sealed_output = fixture.fetch(:sealed_root).children.fetch(0)
      assert_equal sealed_before, tap_recipe_tree_snapshot(sealed_output)
      installed = fixture.fetch(:build_path).parent/"installed-prefix"
      installed.mkpath

      # Exercise pinned Homebrew's real Pathname#install implementation. It
      # moves these Formula-owned nodes; the runner evidence must remain
      # byte-for-byte and mode-for-mode unchanged.
      installed.install(out_dir.children)

      assert_empty out_dir.children
      assert_equal sealed_before, tap_recipe_tree_snapshot(sealed_output)
      assert_equal "tap recipe output\n", (installed/"bin/hello").binread
      assert_equal "nested helper\n",
                   (installed/"libexec/tools/helper").binread
      assert_equal "archive payload\n", (installed/"lib/libhello.a").binread
      assert_equal "nested data\n",
                   (installed/"share/hello/data.txt").binread
      assert_equal 0644, (installed/"bin/readme.txt").lstat.mode & 07777
      assert_equal 0755,
                   (installed/"share/hello/run-helper").lstat.mode & 07777
      assert_equal(
        Pathname("../libexec/tools/helper"),
        (installed/"bin/hello-current").readlink,
      )
    end
  end

  def test_tap_recipe_rejects_output_root_replacement_mode_and_contents
    mutations = {
      "replacement" => lambda do |root|
        root.rmdir
        root.mkdir(0700)
      end,
      "symlink replacement" => lambda do |root|
        alternate = root.parent/"alternate-output-root"
        alternate.mkdir(0700)
        root.rmdir
        root.make_symlink(alternate)
      end,
      "mode drift" => ->(root) { root.chmod(0755) },
      "foreign contents" => ->(root) { (root/"foreign").binwrite("foreign\n") },
    }
    mutations.each do |label, mutate|
      with_tap_recipe_build_fixture do |fixture|
        root = fixture.fetch(:build_path)/"kandelo-package-out"
        fixture.fetch(:runner_return_hooks) << lambda do |_sealed, _response|
          mutate.call(root)
        end

        error = assert_raises(RuntimeError, label) { run_tap_recipe(fixture) }

        assert_match(/output root|unexpected entries/i, error.message, label)
        if label == "foreign contents"
          assert_equal "foreign\n", (root/"foreign").binread
        end
      end
    end
  end

  def test_tap_recipe_copy_failure_leaves_private_nodes_for_outer_cleanup
    with_tap_recipe_build_fixture do |fixture|
      harness = fixture.fetch(:harness)
      harness.define_singleton_method(
        :kandelo_tap_recipe_copy_stream,
      ) do |_source, destination|
        destination.write("partial bytes")
        raise "injected materialization copy failure"
      end

      error = assert_raises(RuntimeError) { run_tap_recipe(fixture) }

      assert_equal "injected materialization copy failure", error.message
      output_root = fixture.fetch(:build_path)/"kandelo-package-out"
      staging = output_root.children.fetch(0)
      assert_match(/\A\.kandelo-materializing-/, staging.basename.to_s)
      assert_equal 0700, staging.lstat.mode & 07777
      assert_equal "partial bytes",
                   (staging/"share/hello/data.txt").binread
    end

    with_tap_recipe_build_fixture do |fixture|
      harness = fixture.fetch(:harness)
      original = harness.method(:kandelo_tap_recipe_copy_file!)
      harness.define_singleton_method(
        :kandelo_tap_recipe_copy_file!,
      ) do |source, destination, source_stat|
        original.call(source, destination, source_stat)
        next unless source.basename.to_s == "data.txt"

        (destination.dirname/"foreign").binwrite("must survive failure\n")
        raise "injected copy failure with foreign entry"
      end

      error = assert_raises(RuntimeError) { run_tap_recipe(fixture) }

      assert_equal "injected copy failure with foreign entry", error.message
      output_root = fixture.fetch(:build_path)/"kandelo-package-out"
      staging = output_root.children.fetch(0)
      assert_match(/\A\.kandelo-materializing-/, staging.basename.to_s)
      foreign = staging/"share/hello/foreign"
      assert_equal "must survive failure\n", foreign.binread
      assert_equal "nested data\n",
                   (staging/"share/hello/data.txt").binread
    end

    with_tap_recipe_build_fixture do |fixture|
      harness = fixture.fetch(:harness)
      original = harness.method(:kandelo_tap_recipe_original_output_root!)
      private_checks = 0
      harness.define_singleton_method(
        :kandelo_tap_recipe_original_output_root!,
      ) do |out_dir, build_root, handle, identity, entries:|
        result = original.call(
          out_dir, build_root, handle, identity, entries:
        )
        if entries.one? &&
           entries.fetch(0).start_with?(".kandelo-materializing-")
          private_checks += 1
        end
        if private_checks == 2
          raise "injected final pre-return verification failure"
        end
        result
      end

      error = assert_raises(RuntimeError) { run_tap_recipe(fixture) }

      assert_equal(
        "injected final pre-return verification failure",
        error.message,
      )
      output_root =
        fixture.fetch(:build_path)/"kandelo-package-out"
      refute_path_exists output_root/"contents"
      published = output_root.children.fetch(0)
      assert_match(/\A\.kandelo-materializing-/,
                   published.basename.to_s)
      assert_equal "nested data\n",
                   (published/"share/hello/data.txt").binread
    end

    materializer = Pathname(__dir__).join(
      "..", "kandelo_formula_support.rb"
    ).binread.match(
      /  def kandelo_materialize_tap_recipe_output!.*?^  end$/m,
    )[0]
    refute_match(
      /(?:File\.rename|File\.unlink|Dir\.rmdir|FileUtils\.)/,
      materializer,
    )
  end

  def test_tap_recipe_helper_accepts_only_the_exact_launcher_sealed_input_modes
    with_tap_recipe_build_fixture do |fixture|
      runtime, recipe = fixture.fetch(:harness).kandelo_tap_recipe_runtime!
      recipe_root, entrypoint =
        fixture.fetch(:harness).kandelo_verify_tap_recipe_tree!(runtime, recipe)

      assert_equal fixture.fetch(:recipe_root), recipe_root
      assert_equal fixture.fetch(:entrypoint), entrypoint
      [
        fixture.fetch(:tap_root),
        fixture.fetch(:tap_root)/"Kandelo",
        fixture.fetch(:tap_root)/"Kandelo/recipes",
        fixture.fetch(:recipe_root),
        fixture.fetch(:recipe_root)/"patches",
      ].each do |directory|
        assert_equal 0555, directory.lstat.mode & 0777, directory.to_s
      end
      assert_equal 0444, fixture.fetch(:manifest_path).lstat.mode & 0777
      assert_equal 0555, fixture.fetch(:entrypoint).lstat.mode & 0777
      assert_equal 0444, fixture.fetch(:patch).lstat.mode & 0777
    end

    mode_mutations = {
      "tap root is writable"          => [:tap_root, nil, 0755],
      "Kandelo directory is writable" => [:tap_root, "Kandelo", 0755],
      "recipes directory is writable" => [:tap_root, "Kandelo/recipes", 0755],
      "recipe directory is writable"  => [:recipe_root, nil, 0755],
      "nested directory is writable"  => [:recipe_root, "patches", 0755],
      "directory is over-restricted"  => [:recipe_root, "patches", 0500],
      "manifest is writable"          => [:manifest_path, nil, 0644],
      "manifest is over-restricted"   => [:manifest_path, nil, 0400],
      "entrypoint retains source mode" => [:entrypoint, nil, 0755],
      "entrypoint loses executable meaning" => [:entrypoint, nil, 0444],
      "data input retains source mode" => [:patch, nil, 0644],
      "data input gains executable meaning" => [:patch, nil, 0555],
    }
    mode_mutations.each do |label, (fixture_key, relative, mode)|
      with_tap_recipe_build_fixture do |fixture|
        path = fixture.fetch(fixture_key)
        path = path/relative unless relative.nil?
        path.chmod(mode)

        error = assert_tap_recipe_rejected_before_activation(fixture)

        assert_match(/mode|sealed/i, error.message, label)
      end
    end
  end

  def test_tap_recipe_helper_stages_attested_resources_at_fixed_guest_paths
    resource_record = {
      "name"          => "chocolate-doom",
      "source_sha256" => "b" * 64,
      "source_url"    => "https://example.test/chocolate-doom.tar.gz",
    }
    with_tap_recipe_build_fixture(resource_records: [resource_record]) do |fixture|
      out_dir = run_tap_recipe(fixture)
      request = fixture.fetch(:runner_requests).fetch(0)
      staged = fixture.fetch(:build_path)/
        "kandelo-package-resources/chocolate-doom"

      assert_equal(
        { "chocolate-doom" => staged.to_s },
        request.fetch("resources"),
      )
      assert_equal(
        "/kandelo/resources/chocolate-doom",
        request.fetch("environment").fetch(
          "WASM_POSIX_DEP_RESOURCE_CHOCOLATE_DOOM_DIR",
        ),
      )
      assert_equal(
        "verified chocolate-doom resource\n",
        (staged/"input.txt").binread,
      )
      refute_path_exists(
        fixture.fetch(:build_path)/
          "kandelo-package-source/kandelo-package-resources",
      )
      assert_equal "tap recipe output\n", (out_dir/"bin/hello").binread
    end
  end

  def test_tap_recipe_helper_rejects_resource_identity_drift_before_activation
    resource_record = {
      "name"          => "resource",
      "source_sha256" => "b" * 64,
      "source_url"    => "https://example.test/resource.tar.gz",
    }
    with_tap_recipe_build_fixture(resource_records: [resource_record]) do |fixture|
      selected = fixture.fetch(:harness).resources.fetch("resource")
      selected.source_sha256 = "c" * 64 if selected.respond_to?(:source_sha256=)
      selected.checksum.hexdigest = "c" * 64

      error = assert_tap_recipe_rejected_before_activation(fixture)

      assert_includes error.message, "resource identity differs"
    end
  end

  def test_tap_recipe_helper_rejects_mutated_or_unlisted_inputs_before_execution
    mutations = {
      "manifest bytes" => lambda do |fixture|
        fixture.fetch(:manifest_path).binwrite("{}\n")
      end,
      "unlisted input" => lambda do |fixture|
        (fixture.fetch(:recipe_root)/"unlisted.patch").binwrite("unlisted\n")
      end,
      "missing input" => lambda do |fixture|
        fixture.fetch(:patch).delete
      end,
      "mutated input" => lambda do |fixture|
        fixture.fetch(:patch).binwrite("changed\n")
      end,
      "mode input" => lambda do |fixture|
        fixture.fetch(:patch).chmod(0755)
      end,
      "symlink input" => lambda do |fixture|
        target = fixture.fetch(:recipe_root).parent/"outside.patch"
        target.binwrite("outside\n")
        fixture.fetch(:patch).delete
        fixture.fetch(:patch).make_symlink(target)
      end,
      "hard-linked input" => lambda do |fixture|
        target = fixture.fetch(:recipe_root).parent/"outside.patch"
        target.binwrite(fixture.fetch(:patch).binread)
        fixture.fetch(:patch).delete
        File.link(target, fixture.fetch(:patch))
      end,
    }
    mutations.each do |label, mutate|
      with_tap_recipe_build_fixture do |fixture|
        mutate_sealed_tap_recipe_fixture!(fixture) { mutate.call(fixture) }
        error = assert_tap_recipe_rejected_before_activation(fixture)
        assert_match(/manifest|recipe|link|unavailable|closure|tree|file/i, error.message, label)
      end
    end
  end

  def test_tap_recipe_helper_rejects_traversal_even_in_an_attested_manifest
    with_tap_recipe_build_fixture do |fixture|
      mutate_sealed_tap_recipe_fixture!(fixture) do
        manifest = JSON.parse(fixture.fetch(:manifest_path).binread)
        manifest.fetch("files").first["path"] = "../build.sh"
        fixture.fetch(:manifest_path).binwrite(JSON.generate(manifest))
        fixture.fetch(:recipe)["manifest_sha256"] =
          Digest::SHA256.file(fixture.fetch(:manifest_path)).hexdigest
      end

      error = assert_tap_recipe_rejected_before_activation(fixture)

      assert_includes error.message, "canonical relative path"
    end
  end

  def test_tap_recipe_helper_rejects_dependency_environment_name_collisions
    with_tap_recipe_build_fixture do |fixture|
      dependencies = [
        "kandelo-dev/tap-core/foo-bar",
        "kandelo-dev/tap-core/foo_bar",
      ]
      mutate_sealed_tap_recipe_fixture!(fixture) do
        manifest = JSON.parse(fixture.fetch(:manifest_path).binread)
        manifest["dependencies"] = dependencies
        fixture.fetch(:manifest_path).binwrite("#{JSON.pretty_generate(manifest)}\n")
        fixture.fetch(:recipe)["dependencies"] = dependencies
        fixture.fetch(:recipe)["manifest_sha256"] =
          Digest::SHA256.file(fixture.fetch(:manifest_path)).hexdigest
      end

      error = assert_tap_recipe_rejected_before_activation(fixture)

      assert_includes error.message, "collide in their build environment names"
    end
  end

  def test_tap_recipe_helper_rejects_formula_environment_drift_before_execution
    with_tap_recipe_build_fixture do |fixture|
      error = assert_tap_recipe_rejected_before_activation(
        fixture, script_env: { "HELLO_DIFFERENT" => "value" }
      )

      assert_includes error.message, "script_env differs"
    end
  end

  def test_tap_recipe_helper_revalidates_inputs_after_the_script_returns
    with_tap_recipe_build_fixture do |fixture|
      fixture.fetch(:system_hooks) << lambda do
        mutate_sealed_tap_recipe_fixture!(fixture) do
          fixture.fetch(:patch).binwrite("changed by recipe\n")
        end
      end

      error = assert_raises(RuntimeError) { run_tap_recipe(fixture) }

      assert_includes error.message, "differs from its manifest"
      assert_equal [:sdk, :sysroot], fixture.fetch(:activation_calls)
      refute_nil fixture.fetch(:harness).system_calls
    end
  end

  def test_tap_recipe_helper_rejects_output_mutated_after_the_runner_seals_it
    with_tap_recipe_build_fixture do |fixture|
      fixture.fetch(:runner_return_hooks) << lambda do |sealed_out, _response|
        output = sealed_out/"bin/hello"
        output.chmod(0644)
        output.binwrite("changed after seal\n")
        output.chmod(0444)
      end

      error = assert_raises(RuntimeError) { run_tap_recipe(fixture) }

      assert_includes error.message, "differs from the runner response"
      assert_equal 1, fixture.fetch(:runner_requests).length
    end
  end

  def test_tap_recipe_helper_rejects_malformed_or_unsealed_runner_responses
    mutations = {
      "wrong request digest" => lambda do |_sealed_out, response|
        document = JSON.parse(response.binread)
        document["request_sha256"] = "0" * 64
        response.chmod(0644)
        response.binwrite(JSON.generate(document))
        response.chmod(0444)
      end,
      "unknown response field" => lambda do |_sealed_out, response|
        document = JSON.parse(response.binread)
        document["unknown"] = true
        response.chmod(0644)
        response.binwrite(JSON.generate(document))
        response.chmod(0444)
      end,
      "noncanonical response" => lambda do |_sealed_out, response|
        bytes = response.binread
        response.chmod(0644)
        response.binwrite("#{bytes}\n")
        response.chmod(0444)
      end,
      "reordered response fields" => lambda do |_sealed_out, response|
        document = JSON.parse(response.binread)
        reordered = document.keys.reverse.to_h { |key| [key, document.fetch(key)] }
        response.chmod(0644)
        response.binwrite(JSON.generate(reordered))
        response.chmod(0444)
      end,
      "writable response" => lambda do |_sealed_out, response|
        response.chmod(0644)
      end,
      "hard-linked response" => lambda do |_sealed_out, response|
        File.link(response, response.dirname/"response-alias")
      end,
      "outside output root" => lambda do |_sealed_out, response|
        document = JSON.parse(response.binread)
        document["sealed_output_root"] = response.dirname.to_s
        response.chmod(0644)
        response.binwrite(JSON.generate(document))
        response.chmod(0444)
      end,
      "writable sealed file" => lambda do |sealed_out, _response|
        (sealed_out/"bin/hello").chmod(0644)
      end,
    }
    mutations.each do |label, mutate|
      with_tap_recipe_build_fixture do |fixture|
        fixture.fetch(:runner_return_hooks) << mutate

        error = assert_raises(RuntimeError, label) { run_tap_recipe(fixture) }

        assert_match(
          /request|response|schema|sealed|safe mode|protected sealed root/i,
          error.message,
          label,
        )
      end
    end
  end

  def test_tap_recipe_helper_rejects_output_that_escapes_or_aliases_staging
    mutations = {
      "escaping symlink" => lambda do |fixture|
        link = fixture.fetch(:runner_output)/"bin/hello-current"
        link.delete
        link.make_symlink("../../outside")
      end,
      "hard-linked file" => lambda do |fixture|
        file = fixture.fetch(:runner_output)/"bin/hello"
        alias_path = fixture.fetch(:runner_output)/"bin/hello-alias"
        File.link(file, alias_path)
      end,
      "direct prefix output" => lambda do |fixture|
        fixture.fetch(:prefix_path).mkpath
        (fixture.fetch(:prefix_path)/"escaped").binwrite("wrong root\n")
      end,
    }
    mutations.each do |label, mutate|
      with_tap_recipe_build_fixture do |fixture|
        fixture.fetch(:system_hooks) << -> { mutate.call(fixture) }

        error = assert_raises(RuntimeError, label) { run_tap_recipe(fixture) }

        assert_match(/escapes|one link|staging prefix/i, error.message, label)
      end
    end
  end

  def test_tap_recipe_helper_rejects_unsafe_or_oversized_output
    mutations = {
      "world-writable file" => lambda do |fixture|
        (fixture.fetch(:runner_output)/"bin/hello").chmod(0666)
      end,
      "set-id file" => lambda do |fixture|
        (fixture.fetch(:runner_output)/"bin/hello").chmod(04644)
      end,
      "world-writable directory" => lambda do |fixture|
        (fixture.fetch(:runner_output)/"bin").chmod(0777)
      end,
      "control character in path" => lambda do |fixture|
        (fixture.fetch(:runner_output)/"bad\nname").binwrite("bad path\n")
      end,
      "control character in symlink target" => lambda do |fixture|
        link = fixture.fetch(:runner_output)/"bin/hello-current"
        link.delete
        link.make_symlink("hello\n")
      end,
      "oversized file" => lambda do |fixture|
        path = fixture.fetch(:runner_output)/"bin/oversized"
        File.open(path, "wb") do |file|
          file.truncate(KandeloFormulaSupport::KANDELO_TAP_RECIPE_OUTPUT_FILE_MAX_BYTES + 1)
        end
      end,
      "oversized tree" => lambda do |fixture|
        out = fixture.fetch(:runner_output)
        %w[first second].each do |basename|
          File.open(out/basename, "wb") do |file|
            file.truncate(KandeloFormulaSupport::KANDELO_TAP_RECIPE_OUTPUT_FILE_MAX_BYTES)
          end
        end
      end,
    }
    mutations.each do |label, mutate|
      with_tap_recipe_build_fixture do |fixture|
        fixture.fetch(:system_hooks) << -> { mutate.call(fixture) }

        error = assert_raises(RuntimeError, label) { run_tap_recipe(fixture) }

        assert_match(/mode|path|symlink|byte limit/i, error.message, label)
      end
    end
  end

  def test_tap_recipe_output_limits_count_entries_and_full_relative_paths
    Dir.mktmpdir("kandelo-tap-recipe-output-limits") do |dir|
      out = Pathname(dir)/"out"
      out.mkpath
      (out/"first").binwrite("first\n")
      (out/"second").binwrite("second\n")
      harness = Harness.new

      entry_error = assert_raises(RuntimeError) do
        harness.kandelo_validate_tap_recipe_output!(out, max_entries: 1)
      end
      assert_includes entry_error.message, "too many filesystem entries"

      path_error = assert_raises(RuntimeError) do
        harness.kandelo_validate_tap_recipe_output!(out, max_path_bytes: 4)
      end
      assert_includes path_error.message, "invalid or oversized path"

      alias_parent = Pathname(dir)/"alias"
      alias_parent.make_symlink(".")
      root_error = assert_raises(RuntimeError) do
        harness.kandelo_validate_tap_recipe_output!(alias_parent/"out")
      end
      assert_includes root_error.message, "canonical real directory"
    end
  end

  def test_tap_recipe_helper_rejects_an_unselected_or_missing_dependency
    with_tap_recipe_build_fixture do |fixture|
      fixture.fetch(:harness).runtime_formulae = []

      error = assert_raises(RuntimeError) { run_tap_recipe(fixture) }

      assert_includes error.message, "not a selected target dependency"
      assert_nil fixture.fetch(:harness).system_calls
    end
  end

  def test_support_load_succeeds_without_a_publisher_attestation
    with_tier2_loader_fixture do |fixture|
      write_formula_binary_cache(fixture)
      marker = fixture.fetch(:base)/"formula-evaluated"
      assertion = <<~RUBY
        runtime = KandeloFormulaSupport::KANDELO_TIER2_RUNTIME
        abort "ordinary evaluation captured a checker" unless
          runtime.fetch("formula_checker_path").nil?
        abort "ordinary evaluation captured a binary cache" unless
          runtime.fetch("formula_binary_cache_root").nil?
        abort "ordinary cache environment changed" unless
          ENV.fetch("WASM_POSIX_BINARY_CACHE_ROOT") == "/caller/cache"
        abort "ordinary resolver environment changed" unless
          ENV.fetch("WASM_POSIX_BINARY_RESOLVER_REPO_ROOT") == "/caller/root"
        File.binwrite(#{marker.to_s.inspect}, "evaluated\\n")
      RUBY
      _stdout, stderr, status = run_tier2_support_load(
        fixture,
        assertion,
        environment: {
          "WASM_POSIX_BINARY_CACHE_ROOT"         => "/caller/cache",
          "WASM_POSIX_BINARY_RESOLVER_REPO_ROOT" => "/caller/root",
        },
      )

      assert status.success?, stderr
      assert_equal "evaluated\n", marker.binread
    end
  end

  def test_support_load_accepts_and_deeply_freezes_a_tap_recipe_attestation
    with_tier2_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture, bridge: false, recipe: true)
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      assertion = <<~'RUBY'
        runtime = KandeloFormulaSupport::KANDELO_TIER2_RUNTIME
        document = runtime.fetch("attestation")
        recipe = document.fetch("tap_recipe")
        abort "tap recipe schema was not loaded" unless
          document.fetch("schema") == 3 &&
          document.fetch("tier2_bridge").nil? &&
          recipe.fetch("entrypoint") == "build.sh"
        abort "tap recipe authority was not deeply frozen" unless
          [runtime, document, recipe, recipe.fetch("dependencies"),
           recipe.fetch("dependencies").first,
           runtime.fetch("tap_recipe_tools"),
           runtime.fetch("tap_recipe_tools").fetch("fork_instrument"),
           runtime.fetch("tap_recipe_tools").fetch("local_root_spill"),
           runtime.fetch("tap_recipe_runner_path"),
           runtime.fetch("tap_recipe_sealed_root")].all?(&:frozen?)
      RUBY
      _stdout, stderr, status = run_tier2_support_load(
        fixture, assertion, simulated_owner_uid: 0
      )

      assert status.success?, stderr
    end
  end

  def test_support_load_rejects_unsealed_or_unbound_tap_recipe_platform_tools
    mutations = {
      "missing selection" => lambda do |_fixture|
        { "HOMEBREW_KANDELO_FORK_INSTRUMENT" => "" }
      end,
      "outside projection" => lambda do |fixture|
        outside = fixture.fetch(:base)/"outside-fork-instrument"
        outside.binwrite("#!/bin/sh\nexit 0\n")
        outside.chmod(0555)
        { "HOMEBREW_KANDELO_FORK_INSTRUMENT" => outside.to_s }
      end,
      "symlink" => lambda do |fixture|
        tool = fixture.fetch(:fork_instrument)
        tool.delete
        tool.make_symlink(fixture.fetch(:local_root_spill))
        {}
      end,
      "writable executable" => lambda do |fixture|
        fixture.fetch(:fork_instrument).chmod(0755)
        {}
      end,
      "hard-linked executable" => lambda do |fixture|
        File.link(
          fixture.fetch(:fork_instrument),
          fixture.fetch(:fork_instrument).dirname/"fork-instrument-alias",
        )
        {}
      end,
      "writable ancestor" => lambda do |fixture|
        fixture.fetch(:fork_instrument).dirname.chmod(0777)
        {}
      end,
      "missing runner selection" => lambda do |_fixture|
        { "HOMEBREW_KANDELO_TAP_RECIPE_RUNNER" => "" }
      end,
      "writable runner" => lambda do |fixture|
        fixture.fetch(:recipe_runner).chmod(0755)
        {}
      end,
      "empty runner" => lambda do |fixture|
        fixture.fetch(:recipe_runner).chmod(0755)
        fixture.fetch(:recipe_runner).binwrite("")
        fixture.fetch(:recipe_runner).chmod(0555)
        {}
      end,
      "symlink runner" => lambda do |fixture|
        fixture.fetch(:protected_root).chmod(0755)
        fixture.fetch(:recipe_runner).delete
        fixture.fetch(:recipe_runner).make_symlink(fixture.fetch(:local_root_spill))
        fixture.fetch(:protected_root).chmod(0555)
        {}
      end,
      "hard-linked runner" => lambda do |fixture|
        fixture.fetch(:protected_root).chmod(0755)
        File.link(
          fixture.fetch(:recipe_runner),
          fixture.fetch(:protected_root)/"runner-alias",
        )
        fixture.fetch(:protected_root).chmod(0555)
        {}
      end,
      "writable protected build root" => lambda do |fixture|
        fixture.fetch(:protected_root).chmod(0755)
        {}
      end,
      "wrong protected build root name" => lambda do |fixture|
        fixture.fetch(:protected_anchor).chmod(0755)
        invalid_root = fixture.fetch(:protected_anchor)/"current"
        invalid_runner = invalid_root/"homebrew-tap-recipe-runner"
        invalid_sealed_root = invalid_root/"sealed-outputs"
        [invalid_root, invalid_sealed_root].each(&:mkpath)
        invalid_runner.binwrite("#!/bin/sh\nexit 0\n")
        invalid_runner.chmod(0555)
        invalid_sealed_root.chmod(0555)
        invalid_root.chmod(0555)
        fixture.fetch(:protected_anchor).chmod(0711)
        {
          "HOMEBREW_KANDELO_TAP_RECIPE_RUNNER" => invalid_runner.to_s,
          "HOMEBREW_KANDELO_TAP_RECIPE_SEALED_ROOT" => invalid_sealed_root.to_s,
        }
      end,
      "writable protected anchor" => lambda do |fixture|
        fixture.fetch(:protected_anchor).chmod(0755)
        {}
      end,
      "missing sealed root selection" => lambda do |_fixture|
        { "HOMEBREW_KANDELO_TAP_RECIPE_SEALED_ROOT" => "" }
      end,
      "writable sealed root" => lambda do |fixture|
        fixture.fetch(:sealed_root).chmod(0755)
        {}
      end,
    }
    mutations.each do |label, mutate|
      with_tier2_loader_fixture do |fixture|
        document = tier2_loader_attestation(fixture, bridge: false, recipe: true)
        write_tier2_loader_attestation(fixture, JSON.generate(document))
        environment = mutate.call(fixture)
        marker = fixture.fetch(:base)/"#{label.tr(" ", "-")}-evaluated"

        _stdout, stderr, status = run_tier2_support_load(
          fixture,
          "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")",
          environment:,
          simulated_owner_uid: 0,
        )

        refute status.success?, label
        assert_match(/closed tap recipe|platform projection/i, stderr, label)
        refute_path_exists marker, label
      end
    end
  end

  def test_support_load_rejects_malformed_tap_recipe_authority_before_formula_evaluation
    with_tier2_loader_fixture do |fixture|
      valid = tier2_loader_attestation(fixture, bridge: false, recipe: true)
      missing_recipe_key = Marshal.load(Marshal.dump(valid))
      missing_recipe_key.fetch("tap_recipe").delete("manifest_sha256")
      unsorted_dependencies = Marshal.load(Marshal.dump(valid))
      unsorted_dependencies.fetch("tap_recipe")["dependencies"] = [
        "kandelo-dev/tap-core/zlib",
        "kandelo-dev/tap-core/bzip2",
      ]
      colliding_dependencies = Marshal.load(Marshal.dump(valid))
      colliding_dependencies.fetch("tap_recipe")["dependencies"] = [
        "kandelo-dev/tap-core/foo-bar",
        "kandelo-dev/tap-core/foo_bar",
      ]
      resource_record = {
        "name"          => "fixture-data",
        "source_sha256" => "e" * 64,
        "source_url"    => "https://example.test/fixture-data.tar.gz",
      }
      unknown_resource_key = Marshal.load(Marshal.dump(valid))
      unknown_resource_key.fetch("tap_recipe")["resources"] = [
        resource_record.merge("path" => "/caller/selected"),
      ]
      unsorted_resources = Marshal.load(Marshal.dump(valid))
      unsorted_resources.fetch("tap_recipe")["resources"] = [
        resource_record.merge("name" => "z-data"),
        resource_record.merge("name" => "a-data"),
      ]
      colliding_resources = Marshal.load(Marshal.dump(valid))
      colliding_resources.fetch("tap_recipe")["resources"] = [
        resource_record.merge("name" => "fixture-data"),
        resource_record.merge("name" => "fixture_data"),
      ]
      resource_env_override = Marshal.load(Marshal.dump(valid))
      resource_env_override.fetch("tap_recipe")["resources"] = [resource_record]
      resource_env_override.fetch("tap_recipe")["script_env_keys"] = [
        "WASM_POSIX_DEP_RESOURCE_FIXTURE_DATA_DIR",
      ]
      dependency_resource_collision = Marshal.load(Marshal.dump(valid))
      dependency_resource_collision.fetch("tap_recipe")["resources"] = [resource_record]
      dependency_resource_collision.fetch("tap_recipe")["dependencies"] = [
        "kandelo-dev/tap-core/resource-fixture-data",
      ]
      both_paths = Marshal.load(Marshal.dump(valid))
      both_paths["tier2_bridge"] =
        tier2_loader_attestation(fixture).fetch("tier2_bridge")
      mutations = {
        "missing recipe"         => valid.merge("tap_recipe" => nil),
        "missing recipe key"     => missing_recipe_key,
        "unknown recipe key"     => valid.merge(
          "tap_recipe" => valid.fetch("tap_recipe").merge("unknown" => true),
        ),
        "traversal entrypoint"   => valid.merge(
          "tap_recipe" => valid.fetch("tap_recipe").merge("entrypoint" => "../build.sh"),
        ),
        "unqualified dependency" => valid.merge(
          "tap_recipe" => valid.fetch("tap_recipe").merge("dependencies" => ["zlib"]),
        ),
        "unsorted dependencies"  => unsorted_dependencies,
        "colliding dependencies" => colliding_dependencies,
        "unknown resource key"   => unknown_resource_key,
        "unsorted resources"     => unsorted_resources,
        "colliding resources"    => colliding_resources,
        "resource env override"  => resource_env_override,
        "dependency resource collision" => dependency_resource_collision,
        "oversized resource URL" => valid.merge(
          "tap_recipe" => valid.fetch("tap_recipe").merge(
            "resources" => [
              resource_record.merge("source_url" => "https://example.test/#{"a" * 1005}"),
            ],
          ),
        ),
        "both build paths"       => both_paths,
        "wrong recipe schema"    => valid.merge("schema" => 2),
      }
      mutations.each do |label, document|
        marker = fixture.fetch(:base)/"#{label.tr(" ", "-")}-evaluated"
        path = write_tier2_loader_attestation(fixture, JSON.generate(document))
        _stdout, _stderr, status = run_tier2_support_load(
          fixture, "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")"
        )

        refute status.success?, label
        refute_path_exists marker, label
        path.chmod(0644)
      end
    end
  end

  def test_support_load_accepts_bounded_resource_attestation_above_legacy_limit
    with_tier2_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture, bridge: false, recipe: true)
      document.fetch("tap_recipe")["resources"] = 32.times.map do |index|
        {
          "name"          => format("resource-%02d", index),
          "source_sha256" => "e" * 64,
          "source_url"    => "https://example.test/#{index}/#{"a" * 600}",
        }
      end
      contents = JSON.generate(document)
      assert_operator contents.bytesize, :>, 16_384
      assert_operator(
        contents.bytesize,
        :<=,
        KandeloFormulaSupport::KANDELO_TIER2_ATTESTATION_MAX_BYTES,
      )
      write_tier2_loader_attestation(fixture, contents)
      marker = fixture.fetch(:base)/"large-resource-attestation-evaluated"

      _stdout, stderr, status = run_tier2_support_load(
        fixture,
        "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")",
        simulated_owner_uid: 0,
      )

      assert status.success?, stderr
      assert_path_exists marker
    end
  end

  def test_support_load_preserves_the_homebrew_checker_bridge_and_freezes_runner_propagation
    with_tier2_loader_fixture(root_basename: "kandelo root") do |fixture|
      document = tier2_loader_attestation(fixture, bridge: false)
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      checker = write_sealed_formula_checker(fixture)
      binary_cache_root = write_formula_binary_cache(fixture)
      replacement = fixture.fetch(:base)/"caller-selected-xtask"
      replacement.binwrite("#!/bin/sh\nexit 1\n")
      replacement.chmod(0555)
      poisoned_cache = fixture.fetch(:base)/"caller-selected-cache"
      poisoned_root = fixture.fetch(:base)/"caller-selected-root"
      assertion = <<~RUBY
        runtime = KandeloFormulaSupport::KANDELO_TIER2_RUNTIME
        abort "publisher attestation was not active" unless runtime.fetch("attestation")
        abort "idiomatic Formula unexpectedly gained a Tier-2 bridge" unless
          runtime.dig("attestation", "tier2_bridge").nil?
        captured_checker = runtime.fetch("formula_checker_path")
        captured_cache = runtime.fetch("formula_binary_cache_root")
        abort "checker bridge was not captured" unless
          captured_checker == #{checker.to_s.inspect}
        abort "binary cache was not captured" unless
          captured_cache == #{binary_cache_root.to_s.inspect}
        abort "runner authority was not deeply frozen" unless
          [runtime, captured_checker, captured_cache,
           runtime.fetch("trusted_env").fetch("HOMEBREW_KANDELO_ROOT")].all?(&:frozen?)
        ENV["HOMEBREW_KANDELO_XTASK_BIN"] = #{replacement.to_s.inspect}
        ENV["WASM_POSIX_XTASK_BIN"] = #{replacement.to_s.inspect}
        ENV["WASM_POSIX_BINARY_CACHE_ROOT"] = #{poisoned_cache.to_s.inspect}
        ENV["WASM_POSIX_BINARY_RESOLVER_REPO_ROOT"] = #{poisoned_root.to_s.inspect}
        harness = Class.new do
          include KandeloFormulaSupport
          attr_reader :command
          def kandelo_require_root! = ENV.fetch("HOMEBREW_KANDELO_ROOT")
          def testpath = Pathname(ENV.fetch("HOMEBREW_KANDELO_ROOT"))
          def shell_output(command, _expected_status = 0)
            @command = command
            "runtime-ok\\n"
          end
          def kandelo_record_node_execution!(*, **) = nil
        end.new
        harness.kandelo_run_wasm("program.wasm", [])
        prefixes = %w[
          WASM_POSIX_BINARY_CACHE_ROOT=
          WASM_POSIX_BINARY_RESOLVER_REPO_ROOT=
          WASM_POSIX_XTASK_BIN=
        ]
        assignments = Shellwords.shellsplit(harness.command).select do |token|
          prefixes.any? { |prefix| token.start_with?(prefix) }
        end
        expected = [
          "WASM_POSIX_BINARY_CACHE_ROOT=#{binary_cache_root}",
          "WASM_POSIX_BINARY_RESOLVER_REPO_ROOT=#{fixture.fetch(:root)}",
          "WASM_POSIX_XTASK_BIN=#{checker}",
        ]
        abort "runner did not receive exactly one frozen authority set" unless
          assignments == expected
        [#{replacement.to_s.inspect}, #{poisoned_cache.to_s.inspect},
         #{poisoned_root.to_s.inspect}].each do |poison|
          abort "mutable caller environment replaced runner authority" if
            harness.command.include?(poison)
        end
      RUBY
      _stdout, stderr, status = run_tier2_support_load(
        fixture,
        assertion,
        environment: { "HOMEBREW_KANDELO_XTASK_BIN" => checker.to_s },
        homebrew_filtered: true,
        simulated_owner_uid: 0,
      )

      assert status.success?, stderr
    end
  end

  def test_support_load_rejects_unsealed_or_unbound_formula_checkers
    mutations = {
      "missing checker" => lambda do |fixture|
        fixture.fetch(:root)/"target/x86_64-unknown-linux-gnu/release/xtask"
      end,
      "writable checker" => lambda do |fixture|
        checker = fixture.fetch(:root)/"target/x86_64-unknown-linux-gnu/release/xtask"
        checker.dirname.mkpath
        checker.binwrite("writable\n")
        checker.chmod(0755)
        checker
      end,
      "empty checker" => lambda do |fixture|
        checker = fixture.fetch(:root)/"target/x86_64-unknown-linux-gnu/release/xtask"
        checker.dirname.mkpath
        checker.binwrite("")
        checker.chmod(0555)
        checker
      end,
      "checker outside root" => lambda do |fixture|
        checker = fixture.fetch(:base)/"outside-xtask"
        checker.binwrite("outside\n")
        checker.chmod(0555)
        checker
      end,
      "misplaced root-owned checker" => lambda do |fixture|
        checker = fixture.fetch(:root)/"bin/xtask"
        checker.dirname.mkpath
        checker.binwrite("misplaced\n")
        checker.chmod(0555)
        checker
      end,
      "invalid checker host component" => lambda do |fixture|
        checker = fixture.fetch(:root)/"target/x86_64 unknown/release/xtask"
        checker.dirname.mkpath
        checker.binwrite("invalid host\n")
        checker.chmod(0555)
        checker
      end,
      "symlinked checker" => lambda do |fixture|
        target = fixture.fetch(:root)/"target/x86_64-unknown-linux-gnu/release/real-xtask"
        checker = fixture.fetch(:root)/"target/x86_64-unknown-linux-gnu/release/xtask"
        target.dirname.mkpath
        target.binwrite("real\n")
        target.chmod(0555)
        checker.make_symlink(target)
        checker
      end,
      "multiply linked checker" => lambda do |fixture|
        target = fixture.fetch(:root)/"target/x86_64-unknown-linux-gnu/release/real-xtask"
        checker = fixture.fetch(:root)/"target/x86_64-unknown-linux-gnu/release/xtask"
        target.dirname.mkpath
        target.binwrite("linked\n")
        target.chmod(0555)
        File.link(target, checker)
        checker
      end,
    }
    mutations.each do |label, mutation|
      with_tier2_loader_fixture do |fixture|
        checker = mutation.call(fixture)
        marker = fixture.fetch(:base)/"#{label.tr(" ", "-")}-evaluated"
        _stdout, stderr, status = run_tier2_support_load(
          fixture,
          "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")",
          environment: { "HOMEBREW_KANDELO_XTASK_BIN" => checker.to_s },
          homebrew_filtered: true,
          simulated_owner_uid: 0,
        )

        refute status.success?, label
        expected = case label
        when "missing checker"
          "checker is unavailable"
        when "checker outside root", "symlinked checker"
          "checker must be inside the authoritative Kandelo root"
        when "misplaced root-owned checker", "invalid checker host component"
          "checker must be at target/<host>/release/xtask"
        else
          "nonempty, root-owned, mode-0555 regular file with one link"
        end
        assert_includes stderr, expected, label
        refute_path_exists marker, label
      end
    end
  end

  def test_support_load_rejects_missing_symlinked_or_escaping_formula_binary_caches
    mutations = {
      "missing fixed cache" => lambda do |_fixture|
        "Kandelo Formula binary cache is unavailable"
      end,
      "cache only at another path" => lambda do |fixture|
        (fixture.fetch(:root)/"portable-cache/programs").mkpath
        "Kandelo Formula binary cache is unavailable"
      end,
      "symlinked in-root cache" => lambda do |fixture|
        target = fixture.fetch(:root)/"real-cache"
        (target/"programs").mkpath
        cache =
          fixture.fetch(:root)/KandeloFormulaSupport::KANDELO_PORTABLE_BINARY_CACHE_BASENAME
        cache.make_symlink(target)
        "Kandelo Formula binary cache must be a canonical real directory"
      end,
      "escaping cache symlink" => lambda do |fixture|
        target = fixture.fetch(:base)/"outside-cache"
        (target/"programs").mkpath
        cache =
          fixture.fetch(:root)/KandeloFormulaSupport::KANDELO_PORTABLE_BINARY_CACHE_BASENAME
        cache.make_symlink(target)
        "Kandelo Formula binary cache must be a canonical real directory"
      end,
      "missing programs root" => lambda do |fixture|
        (fixture.fetch(:root)/KandeloFormulaSupport::KANDELO_PORTABLE_BINARY_CACHE_BASENAME).mkpath
        "Kandelo Formula binary cache programs root is unavailable"
      end,
      "symlinked programs root" => lambda do |fixture|
        cache = fixture.fetch(:root)/KandeloFormulaSupport::KANDELO_PORTABLE_BINARY_CACHE_BASENAME
        real_programs = cache/"real-programs"
        real_programs.mkpath
        (cache/"programs").make_symlink(real_programs)
        "Kandelo Formula binary cache programs root must be a canonical real directory"
      end,
      "escaping programs symlink" => lambda do |fixture|
        cache = fixture.fetch(:root)/KandeloFormulaSupport::KANDELO_PORTABLE_BINARY_CACHE_BASENAME
        outside = fixture.fetch(:base)/"outside-programs"
        [cache, outside].each(&:mkpath)
        (cache/"programs").make_symlink(outside)
        "Kandelo Formula binary cache programs root must be a canonical real directory"
      end,
    }
    mutations.each do |label, mutate|
      with_tier2_loader_fixture do |fixture|
        checker = write_sealed_formula_checker(fixture)
        expected = mutate.call(fixture)
        marker = fixture.fetch(:base)/"#{label.tr(" ", "-")}-evaluated"
        _stdout, stderr, status = run_tier2_support_load(
          fixture,
          "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")",
          environment: {
            "HOMEBREW_KANDELO_XTASK_BIN"           => checker.to_s,
            "WASM_POSIX_BINARY_CACHE_ROOT"         => (fixture.fetch(:base)/"poison-cache").to_s,
            "WASM_POSIX_BINARY_RESOLVER_REPO_ROOT" => (fixture.fetch(:base)/"poison-root").to_s,
          },
          homebrew_filtered: true,
          simulated_owner_uid: 0,
        )

        refute status.success?, label
        assert_includes stderr, expected, label
        refute_path_exists marker, label
      end
    end
  end

  def test_support_load_rejects_a_checker_not_owned_by_root
    with_tier2_loader_fixture do |fixture|
      checker = fixture.fetch(:root)/"target/x86_64-unknown-linux-gnu/release/xtask"
      checker.dirname.mkpath
      checker.binwrite("unprivileged\n")
      checker.chmod(0555)
      marker = fixture.fetch(:base)/"unprivileged-checker-evaluated"
      _stdout, stderr, status = run_tier2_support_load(
        fixture,
        "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")",
        environment: { "HOMEBREW_KANDELO_XTASK_BIN" => checker.to_s },
        homebrew_filtered: true,
        simulated_owner_uid: 1,
      )

      refute status.success?
      assert_includes stderr, "nonempty, root-owned, mode-0555 regular file with one link"
      refute_path_exists marker
    end
  end

  def test_identical_cross_tap_support_is_idempotent_without_an_attestation
    with_cross_tap_loader_fixture do |fixture|
      primary = fixture.fetch(:support_path)
      dependency = fixture.fetch(:dependency_support_path)
      [
        [primary, dependency],
        [dependency, primary],
      ].each do |support_paths|
        assertion = <<~'RUBY'
          runtime = KandeloFormulaSupport::KANDELO_TIER2_RUNTIME
          abort "ordinary evaluation gained authority" unless runtime.fetch("attestation").nil?
          abort "support API version changed" unless
            KandeloFormulaSupport::KANDELO_FORMULA_SUPPORT_API_VERSION == 1
        RUBY
        _stdout, stderr, status = run_tier2_support_load(
          fixture,
          assertion,
          environment: { "KANDELO_TEST_SUPPORT_PATHS" => support_paths },
        )

        assert status.success?, "#{support_paths.map(&:to_s).join(" -> ")}: #{stderr}"
      end
    end
  end

  def test_identical_cross_tap_support_preserves_primary_attestation_in_both_load_orders
    with_cross_tap_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture)
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      primary = fixture.fetch(:support_path)
      dependency = fixture.fetch(:dependency_support_path)
      [
        [primary, dependency],
        [dependency, primary],
      ].each do |support_paths|
        expected_loaded_support = support_paths.first.realpath.to_s
        assertion = <<~RUBY
          runtime = KandeloFormulaSupport::KANDELO_TIER2_RUNTIME
          abort "attested Formula moved with support load order" unless
            runtime.fetch("formula_path") == #{fixture.fetch(:formula_path).realpath.to_s.inspect}
          abort "first support copy did not own the module" unless
            runtime.fetch("support_path") == #{expected_loaded_support.inspect}
          abort "first support runtime digest was not frozen" unless
            runtime.fetch("support_runtime_sha256").frozen?
          abort "first support runtime digest changed with load order" unless
            runtime.fetch("support_runtime_sha256") ==
              #{document.fetch("support_runtime_sha256").inspect}
          abort "primary tap authority changed" unless
            runtime.fetch("trusted_env").fetch("HOMEBREW_KANDELO_PRIMARY_TAP_ROOT") ==
              #{fixture.fetch(:tap_root).to_s.inspect}
        RUBY
        _stdout, stderr, status = run_tier2_support_load(
          fixture,
          assertion,
          environment: { "KANDELO_TEST_SUPPORT_PATHS" => support_paths },
        )

        assert status.success?, "#{support_paths.map(&:to_s).join(" -> ")}: #{stderr}"
      end
    end
  end

  def test_cross_tap_support_rejects_mismatched_bytes_in_both_load_orders
    with_cross_tap_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture)
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      primary = fixture.fetch(:support_path)
      dependency = fixture.fetch(:dependency_support_path)
      dependency.open("ab") { |file| file.write("# incompatible copy\n") }
      [
        [[primary, dependency], "support copies are incompatible"],
        [[dependency, primary], "differs from the Tier-2 attestation"],
      ].each do |support_paths, expected_error|
        marker = fixture.fetch(:base)/"mismatch-#{support_paths.first == primary ? "primary" : "dependency"}"
        _stdout, stderr, status = run_tier2_support_load(
          fixture,
          "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")",
          environment: { "KANDELO_TEST_SUPPORT_PATHS" => support_paths },
        )

        refute status.success?
        assert_includes stderr, expected_error
        refute_path_exists marker
      end
    end
  end

  def test_first_loaded_cross_tap_support_rejects_runtime_helper_drift
    [:primary, :dependency].each do |first_copy|
      with_cross_tap_loader_fixture do |fixture|
        primary = fixture.fetch(:support_path)
        dependency = fixture.fetch(:dependency_support_path)
        primary_helper = primary.dirname/"runtime-helper.ts"
        dependency_helper = dependency.dirname/"runtime-helper.ts"
        primary_helper.binwrite("export const reviewed = true;\n")
        FileUtils.cp(primary_helper, dependency_helper)
        document = tier2_loader_attestation(fixture)
        write_tier2_loader_attestation(fixture, JSON.generate(document))

        first = first_copy == :primary ? primary : dependency
        second = first_copy == :primary ? dependency : primary
        first_helper = first.dirname/"runtime-helper.ts"
        first_helper.open("ab") { |file| file.write("export const drift = true;\n") }
        marker = fixture.fetch(:base)/"#{first_copy}-runtime-drift-evaluated"
        _stdout, stderr, status = run_tier2_support_load(
          fixture,
          "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")",
          environment: { "KANDELO_TEST_SUPPORT_PATHS" => [first, second] },
        )

        refute status.success?, first_copy
        assert_includes stderr, "support runtime differs from the Tier-2 attestation", first_copy
        refute_path_exists marker, first_copy
      end
    end
  end

  def test_first_loaded_support_rejects_added_and_removed_runtime_helpers
    [:added, :removed].each do |mutation|
      with_tier2_loader_fixture do |fixture|
        helper = fixture.fetch(:support_path).dirname/"runtime-helper.ts"
        helper.binwrite("export const reviewed = true;\n") if mutation == :removed
        document = tier2_loader_attestation(fixture)
        write_tier2_loader_attestation(fixture, JSON.generate(document))
        if mutation == :added
          helper.binwrite("export const added = true;\n")
        else
          helper.delete
        end
        marker = fixture.fetch(:base)/"#{mutation}-helper-evaluated"
        _stdout, stderr, status = run_tier2_support_load(
          fixture, "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")"
        )

        refute status.success?, mutation
        assert_includes stderr, "support runtime differs from the Tier-2 attestation", mutation
        refute_path_exists marker, mutation
      end
    end
  end

  def test_first_loaded_support_rejects_a_symlinked_runtime_helper
    with_tier2_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture)
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      helper = fixture.fetch(:support_path).dirname/"runtime-helper.ts"
      helper.make_symlink(fixture.fetch(:formula_path))
      marker = fixture.fetch(:base)/"symlinked-helper-evaluated"

      _stdout, stderr, status = run_tier2_support_load(
        fixture, "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")"
      )

      refute status.success?
      assert_includes stderr, "must be a regular non-symlink file with one link"
      refute_path_exists marker
    end
  end

  def test_first_loaded_support_enforces_runtime_file_count_and_byte_limits
    mutations = {
      "file count" => lambda do |support_dir|
        KandeloFormulaSupport::KANDELO_SUPPORT_RUNTIME_MAX_FILES.times do |index|
          (support_dir/format("helper-%03d.ts", index)).binwrite("")
        end
      end,
      "per-file bytes" => lambda do |support_dir|
        (support_dir/"oversized-helper.ts").binwrite(
          "x" * (KandeloFormulaSupport::KANDELO_SUPPORT_RUNTIME_FILE_MAX_BYTES + 1),
        )
      end,
      "total bytes" => lambda do |support_dir|
        16.times do |index|
          (support_dir/format("large-helper-%02d.ts", index)).binwrite(
            "x" * KandeloFormulaSupport::KANDELO_SUPPORT_RUNTIME_FILE_MAX_BYTES,
          )
        end
      end,
    }
    mutations.each do |label, mutate|
      with_tier2_loader_fixture do |fixture|
        document = tier2_loader_attestation(fixture)
        write_tier2_loader_attestation(fixture, JSON.generate(document))
        mutate.call(fixture.fetch(:support_path).dirname)
        marker = fixture.fetch(:base)/"#{label.tr(" ", "-")}-evaluated"
        _stdout, stderr, status = run_tier2_support_load(
          fixture, "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")"
        )

        refute status.success?, label
        expected = case label
        when "file count"
          "exceeds 128 files"
        when "per-file bytes"
          "must contain 0 to 1048576 bytes"
        else
          "exceeds the byte limit"
        end
        assert_includes stderr, expected, label
        refute_path_exists marker, label
      end
    end
  end

  def test_support_load_validates_and_recursively_freezes_an_active_attestation
    with_tier2_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture)
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      assertion = <<~'RUBY'
        runtime = KandeloFormulaSupport::KANDELO_TIER2_RUNTIME
        values = [runtime, runtime["support_runtime_sha256"], runtime["attestation"],
                  runtime["attestation"]["support_runtime_sha256"],
                  runtime["attestation"]["tier2_bridge"],
                  runtime["attestation"]["tier2_bridge"]["script_env_keys"],
                  runtime["attestation"]["tier2_bridge"]["source_url"],
                  runtime["trusted_env"]]
        abort "runtime authority is not recursively frozen" unless values.all?(&:frozen?)
        puts runtime["attestation"]["full_name"]
      RUBY
      stdout, stderr, status = run_tier2_support_load(fixture, assertion)

      assert status.success?, stderr
      assert_equal "kandelo-dev/tap-core/hello\n", stdout
    end
  end

  def test_support_load_accepts_a_distinct_valid_registry_package_identity
    with_tier2_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture)
      document.fetch("tier2_bridge")["package"] = "cpython"
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      assertion = <<~'RUBY'
        runtime = KandeloFormulaSupport::KANDELO_TIER2_RUNTIME
        abort "Formula identity changed" unless runtime.dig("attestation", "formula") == "hello"
        abort "registry package mapping changed" unless
          runtime.dig("attestation", "tier2_bridge", "package") == "cpython"
      RUBY
      _stdout, stderr, status = run_tier2_support_load(fixture, assertion)

      assert status.success?, stderr
    end
  end

  def test_support_load_accepts_homebrew_filtered_aliases_and_synthesizes_compatibility_values
    with_tier2_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture)
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      assertion = <<~'RUBY'
        trusted = KandeloFormulaSupport::KANDELO_TIER2_RUNTIME.fetch("trusted_env")
        expected_root = ENV.fetch("HOMEBREW_KANDELO_ROOT")
        expected_arch = ENV.fetch("HOMEBREW_KANDELO_ARCH")
        expected_sysroot = ENV.fetch("HOMEBREW_KANDELO_SYSROOT")
        abort "authoritative root changed" unless trusted.fetch("HOMEBREW_KANDELO_ROOT") == expected_root
        abort "authoritative arch changed" unless trusted.fetch("HOMEBREW_KANDELO_ARCH") == expected_arch
        abort "authoritative sysroot changed" unless
          trusted.fetch("HOMEBREW_KANDELO_SYSROOT") == expected_sysroot
        abort "filtered root alias was not synthesized" unless
          trusted.fetch("KANDELO_HOMEBREW_KANDELO_ROOT") == expected_root
        abort "filtered arch alias was not synthesized" unless
          trusted.fetch("KANDELO_HOMEBREW_ARCH") == expected_arch
        abort "filtered sysroot alias was not synthesized" unless
          trusted.fetch("WASM_POSIX_SYSROOT") == expected_sysroot
      RUBY
      _stdout, stderr, status = run_tier2_support_load(
        fixture,
        assertion,
        homebrew_filtered: true,
      )

      assert status.success?, stderr
    end
  end

  def test_support_load_rejects_missing_authority_and_conflicting_legacy_aliases
    with_tier2_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture)
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      mutations = {
        "missing authoritative root" => [
          { "HOMEBREW_KANDELO_ROOT" => nil },
          "publisher root or architecture environment is inconsistent",
        ],
        "missing authoritative arch" => [
          { "HOMEBREW_KANDELO_ARCH" => nil },
          "publisher root or architecture environment is inconsistent",
        ],
        "missing primary tap root" => [
          { "HOMEBREW_KANDELO_PRIMARY_TAP_ROOT" => nil },
          "did not identify the selected primary tap root",
        ],
        "conflicting root alias" => [
          { "KANDELO_HOMEBREW_KANDELO_ROOT" => fixture.fetch(:base).to_s },
          "publisher root or architecture environment is inconsistent",
        ],
        "conflicting arch alias" => [
          { "KANDELO_HOMEBREW_ARCH" => "wasm64" },
          "publisher root or architecture environment is inconsistent",
        ],
        "conflicting sysroot alias" => [
          { "WASM_POSIX_SYSROOT" => fixture.fetch(:root).to_s },
          "publisher sysroot environment is inconsistent",
        ],
      }
      mutations.each do |label, mutation|
        environment, error_fragment = mutation
        marker = fixture.fetch(:base)/"#{label.tr(" ", "-")}-evaluated"
        _stdout, stderr, status = run_tier2_support_load(
          fixture,
          "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")",
          environment:,
        )

        refute status.success?, label
        assert_includes stderr, error_fragment, label
        refute_path_exists marker, label
      end
    end
  end

  def test_null_attestation_loads_but_cannot_authorize_the_tier2_helper
    with_tier2_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture, bridge: false)
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      assertion = <<~'RUBY'
        harness = Class.new do
          include KandeloFormulaSupport
          def odie(message)
            raise message
          end
        end.new
        begin
          harness.kandelo_build_package(script_env: {})
          abort "null Tier-2 authority unexpectedly built"
        rescue RuntimeError => error
          abort error.message unless error.message.include?("require a valid publisher attestation")
        end
      RUBY
      _stdout, stderr, status = run_tier2_support_load(fixture, assertion)

      assert status.success?, stderr
    end
  end

  def test_invalid_attestations_abort_before_formula_evaluation
    with_tier2_loader_fixture do |fixture|
      valid = tier2_loader_attestation(fixture)
      valid_json = JSON.generate(valid)
      missing_top = valid.dup
      missing_top.delete("formula_sha256")
      missing_bridge = JSON.parse(valid_json)
      missing_bridge.fetch("tier2_bridge").delete("script_sha256")
      unknown_bridge = JSON.parse(valid_json)
      unknown_bridge.fetch("tier2_bridge")["unknown"] = true
      invalid_bridge_type = JSON.parse(valid_json)
      invalid_bridge_type.fetch("tier2_bridge")["script_env_keys"] = "HELLO_VALUE"
      invalid_bridge_package = JSON.parse(valid_json)
      invalid_bridge_package.fetch("tier2_bridge")["package"] = "../hello"
      missing_support_runtime = valid.dup
      missing_support_runtime.delete("support_runtime_sha256")
      mutations = {
        "duplicate key"       => valid_json.sub('"schema":2', '"schema":2,"schema":2'),
        "missing top key"     => JSON.generate(missing_top),
        "missing runtime hash" => JSON.generate(missing_support_runtime),
        "unknown top key"     => JSON.generate(valid.merge("unknown" => true)),
        "missing bridge key"  => JSON.generate(missing_bridge),
        "unknown bridge key"  => JSON.generate(unknown_bridge),
        "bridge value type"   => JSON.generate(invalid_bridge_type),
        "bridge package"      => JSON.generate(invalid_bridge_package),
        "schema"              => JSON.generate(valid.merge("schema" => 1)),
        "formula hash"        => JSON.generate(valid.merge("formula_sha256" => "f" * 64)),
        "support hash"        => JSON.generate(valid.merge("support_sha256" => "f" * 64)),
        "support runtime hash" => JSON.generate(valid.merge("support_runtime_sha256" => "f" * 64)),
        "trailing JSON value" => "#{valid_json} true",
        "oversized document"  => valid_json.ljust(
          KandeloFormulaSupport::KANDELO_TIER2_ATTESTATION_MAX_BYTES + 1,
          " ",
        ),
      }
      mutations.each do |label, contents|
        marker = fixture.fetch(:base)/"#{label.tr(" ", "-")}-evaluated"
        path = write_tier2_loader_attestation(fixture, contents)
        _stdout, _stderr, status = run_tier2_support_load(
          fixture, "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")"
        )

        refute status.success?, label
        refute_path_exists marker, label
        path.chmod(0644)
      end
    end
  end

  def test_attestation_file_mode_and_identity_abort_before_formula_evaluation
    with_tier2_loader_fixture do |fixture|
      contents = JSON.generate(tier2_loader_attestation(fixture))
      path = write_tier2_loader_attestation(fixture, contents)
      path.chmod(0644)
      mode_marker = fixture.fetch(:base)/"mode-evaluated"

      _stdout, _stderr, status = run_tier2_support_load(
        fixture, "File.binwrite(#{mode_marker.to_s.inspect}, \"evaluated\\n\")"
      )

      refute status.success?
      refute_path_exists mode_marker

      path.delete
      target = fixture.fetch(:base)/"attestation-target.json"
      target.binwrite(contents)
      target.chmod(0444)
      path.make_symlink(target)
      symlink_marker = fixture.fetch(:base)/"symlink-evaluated"

      _stdout, _stderr, status = run_tier2_support_load(
        fixture, "File.binwrite(#{symlink_marker.to_s.inspect}, \"evaluated\\n\")"
      )

      refute status.success?
      refute_path_exists symlink_marker
    end
  end

  def test_absent_runtime_authority_rejects_before_sdk_activation_or_process_execution
    harness = Harness.new
    activated = false
    harness.define_singleton_method(:kandelo_activate_sdk!) do
      activated = true
      "/tmp/kandelo"
    end

    error = assert_raises(RuntimeError) { harness.kandelo_build_package(script_env: {}) }

    assert_includes error.message, "require a valid publisher attestation"
    refute activated
    assert_nil harness.system_calls
  end

  def test_tier2_helper_executes_the_exact_attested_script_with_authoritative_environment
    with_tier2_build_fixture do |fixture|
      binary_cache_root =
        fixture.fetch(:root)/KandeloFormulaSupport::KANDELO_PORTABLE_BINARY_CACHE_BASENAME
      (binary_cache_root/"programs").mkpath
      fixture.fetch(:harness).tier2_runtime["formula_binary_cache_root"] =
        binary_cache_root.to_s
      ENV["HELLO_RESOURCE"] = "/ambient/resource"
      ENV["WASM_POSIX_BINARY_CACHE_ROOT"] = "/ambient/cache"
      ENV["WASM_POSIX_BINARY_INDEX_URL"] = "https://ambient.invalid/index.toml"
      ENV["WASM_POSIX_BINARY_RESOLVER_REPO_ROOT"] = "/ambient/root"
      ENV["WASM_POSIX_DEFAULT_ARCH"] = "wasm64"
      ENV["WASM_POSIX_DEP_NAME"] = "ambient-name"
      ENV["WASM_POSIX_INSTALL_LOCAL_MIRROR"] = "1"
      ENV["HELLO_AMBIENT"] = "must-be-removed"

      out_dir = fixture.fetch(:harness).kandelo_build_package(
        script_env: fixture.fetch(:script_env)
      )

      assert_equal fixture.fetch(:build_path)/"kandelo-package-out", out_dir
      assert_equal [:sdk, :sysroot], fixture.fetch(:activation_calls)
      assert_equal ["/usr/bin/bash", fixture.fetch(:script).to_s], fixture.fetch(:harness).system_args
      environment = fixture.fetch(:harness).system_environment
      assert_equal fixture.fetch(:resource_dir).join("resource").to_s, environment.fetch("HELLO_RESOURCE")
      assert_equal "/formula/pkgconfig", environment.fetch("WASM_POSIX_DEP_PKG_CONFIG_PATH")
      assert_equal "hello", environment.fetch("WASM_POSIX_DEP_NAME")
      assert_equal "1.0", environment.fetch("WASM_POSIX_DEP_VERSION")
      assert_equal "wasm32", environment.fetch("WASM_POSIX_DEP_TARGET_ARCH")
      assert_equal "0", environment.fetch("WASM_POSIX_INSTALL_LOCAL_MIRROR")
      assert_equal fixture.fetch(:root).to_s, environment.fetch("HOMEBREW_KANDELO_ROOT")
      assert_equal binary_cache_root.to_s, environment.fetch("WASM_POSIX_BINARY_CACHE_ROOT")
      assert_equal fixture.fetch(:root).to_s,
                   environment.fetch("WASM_POSIX_BINARY_RESOLVER_REPO_ROOT")
      refute environment.key?("WASM_POSIX_BINARY_INDEX_URL")
      refute environment.key?("WASM_POSIX_DEFAULT_ARCH")
      refute environment.key?("HELLO_AMBIENT")
      assert_path_exists fixture.fetch(:resource_dir)/"resource/input.txt"
      assert_path_exists fixture.fetch(:build_path)/"kandelo-package-source/upstream.c"
    end
  end

  def test_tier2_helper_executes_an_explicit_attested_registry_package_mapping
    script_env = { "WASM_POSIX_DEP_PKG_CONFIG_PATH" => "/formula/pkgconfig" }
    with_tier2_build_fixture(
      formula_name: "python", package_name: "cpython", script_env:
    ) do |fixture|
      out_dir = fixture.fetch(:harness).kandelo_build_package(
        package: "cpython", script_env: fixture.fetch(:script_env)
      )

      assert_equal fixture.fetch(:build_path)/"kandelo-package-out", out_dir
      assert_equal [:sdk, :sysroot], fixture.fetch(:activation_calls)
      assert_equal ["/usr/bin/bash", fixture.fetch(:script).to_s], fixture.fetch(:harness).system_args
      assert_equal "cpython", fixture.fetch(:harness).system_environment.fetch("WASM_POSIX_DEP_NAME")
      assert_path_exists fixture.fetch(:build_path)/"kandelo-package-source/upstream.c"
    end
  end

  def test_tier2_helper_rejects_unattested_registry_package_mappings_before_activation
    script_env = { "WASM_POSIX_DEP_PKG_CONFIG_PATH" => "/formula/pkgconfig" }
    with_tier2_build_fixture(
      formula_name: "python", package_name: "cpython", script_env:
    ) do |fixture|
      attempts = {
        "omitted mapping"  => -> { fixture.fetch(:harness).kandelo_build_package(script_env:) },
        "wrong mapping"    => lambda do
          fixture.fetch(:harness).kandelo_build_package(package: "python", script_env:)
        end,
        "non-string mapping" => lambda do
          fixture.fetch(:harness).kandelo_build_package(package: 1, script_env:)
        end,
      }
      attempts.each do |label, attempt|
        error = assert_raises(RuntimeError, label, &attempt)
        assert_includes error.message, "registry package differs", label
        assert_empty fixture.fetch(:activation_calls), label
        assert_nil fixture.fetch(:harness).system_calls, label
        assert_path_exists fixture.fetch(:build_path)/"upstream.c", label
        refute_path_exists fixture.fetch(:build_path)/"kandelo-package-source", label
      end
    end
  end

  def test_tier2_helper_rejects_every_formula_identity_mismatch_before_activation
    mutations = {
      "name" => lambda do |fixture|
        fixture.fetch(:harness).formula_name = "other"
      end,
      "full name" => lambda do |fixture|
        fixture.fetch(:harness).formula_full_name = "other/tap/hello"
      end,
      "version" => lambda do |fixture|
        fixture.fetch(:harness).formula_version = "367"
      end,
      "source URL" => lambda do |fixture|
        fixture.fetch(:harness).stable_spec = StableSpec.new(
          url: "https://example.test/other.tar.gz",
          checksum: StableChecksum.new(hexdigest: fixture.fetch(:bridge).fetch("source_sha256")),
        )
      end,
      "source checksum" => lambda do |fixture|
        fixture.fetch(:harness).stable_spec = StableSpec.new(
          url: fixture.fetch(:bridge).fetch("source_url"),
          checksum: StableChecksum.new(hexdigest: "f" * 64),
        )
      end,
      "path" => lambda do |fixture|
        other = fixture.fetch(:build_path).parent/"other.rb"
        other.binwrite(fixture.fetch(:formula_path).binread)
        fixture.fetch(:harness).formula_path = other
      end,
    }
    mutations.each do |label, mutate|
      with_tier2_build_fixture do |fixture|
        mutate.call(fixture)
        error = assert_tier2_rejected_before_activation(fixture)
        assert_match(/Formula (?:identity|path) differs/, error.message, label)
      end
    end
  end

  def test_tier2_helper_rejects_formula_support_and_registry_hash_drift_before_activation
    paths = {
      "Formula"               => :formula_path,
      "Formula support"       => :support_path,
      "registry package.toml" => :package_toml,
      "registry build.toml"   => :build_toml,
      "registry build script" => :script,
    }
    paths.each do |label, key|
      with_tier2_build_fixture do |fixture|
        fixture.fetch(key).open("ab") { |file| file.write("# drift\n") }
        error = assert_tier2_rejected_before_activation(fixture)
        assert_includes error.message, label
      end
    end
  end

  def test_tier2_helper_rechecks_the_script_immediately_before_execution
    with_tier2_build_fixture do |fixture|
      harness = fixture.fetch(:harness)
      root = fixture.fetch(:root)
      sysroot = root/"sysroot"
      script = fixture.fetch(:script)
      harness.define_singleton_method(:kandelo_activate_sysroot!) do |activated_root|
        fixture.fetch(:activation_calls) << :sysroot
        ENV["WASM_POSIX_SYSROOT"] = sysroot.to_s
        script.open("ab") { |file| file.write("# late drift\n") }
        activated_root
      end

      error = assert_raises(RuntimeError) do
        harness.kandelo_build_package(script_env: fixture.fetch(:script_env))
      end

      assert_includes error.message, "registry build script differs"
      assert_equal [:sdk, :sysroot], fixture.fetch(:activation_calls)
      assert_nil harness.system_calls
    end
  end

  def test_tier2_helper_rejects_script_env_shape_and_value_boundaries_before_activation
    cases = {
      "exact keys" => ->(env) { env.reject { |key, _value| key == "HELLO_RESOURCE" } },
      "key type"   => ->(env) { env.merge(1 => "bad") },
      "value type" => ->(env) { env.merge("HELLO_RESOURCE" => 1) },
      "NUL"        => ->(env) { env.merge("HELLO_RESOURCE" => "bad\0value") },
      "value size" => ->(env) { env.merge("HELLO_RESOURCE" => "x" * 4_097) },
    }
    cases.each do |label, mutate|
      with_tier2_build_fixture do |fixture|
        error = assert_tier2_rejected_before_activation(
          fixture, script_env: mutate.call(fixture.fetch(:script_env))
        )
        assert_match(/script_env/, error.message, label)
      end
    end

    aggregate_env = (0...5).to_h { |index| ["HELLO_VALUE_#{index}", "x" * 4_096] }
    with_tier2_build_fixture(script_env: aggregate_env) do |fixture|
      error = assert_tier2_rejected_before_activation(fixture)
      assert_includes error.message, "differs from the publisher attestation"
    end

    {
      "reserved"  => { "WASM_POSIX_DEP_NAME" => "hello" },
      "namespace" => { "UNRELATED_VALUE" => "hello" },
    }.each do |label, env|
      with_tier2_build_fixture(script_env: env) do |fixture|
        error = assert_tier2_rejected_before_activation(fixture)
        assert_match(/(?:helper-owned|approved namespace)/, error.message, label)
      end
    end
  end

  def test_tier2_helper_rejects_stale_and_symlinked_build_roots_before_activation
    mutations = {
      "source" => lambda do |fixture|
        (fixture.fetch(:build_path)/"kandelo-package-source").mkpath
      end,
      "work" => lambda do |fixture|
        (fixture.fetch(:build_path)/"kandelo-package-work").make_symlink(fixture.fetch(:root))
      end,
      "out" => lambda do |fixture|
        (fixture.fetch(:build_path)/"kandelo-package-out").binwrite("stale\n")
      end,
      "resource" => lambda do |fixture|
        FileUtils.rm_rf(fixture.fetch(:resource_dir))
        fixture.fetch(:resource_dir).make_symlink(fixture.fetch(:root))
      end,
    }
    mutations.each do |label, mutate|
      with_tier2_build_fixture do |fixture|
        mutate.call(fixture)
        error = assert_raises(RuntimeError) do
          fixture.fetch(:harness).kandelo_build_package(script_env: fixture.fetch(:script_env))
        end
        assert_empty fixture.fetch(:activation_calls)
        assert_nil fixture.fetch(:harness).system_calls
        assert_match(/(?:build root|resource root|already staged)/, error.message, label)
      end
    end
  end

  def test_sdk_activation_declares_exact_direct_and_transitive_target_pkg_config_dirs
    original = ENV.to_hash
    Dir.mktmpdir("kandelo-pkg-config-closure") do |dir|
      harness = Harness.new
      harness.root_path = "/tmp/kandelo-root"
      zlib_name = "kandelo-dev/tap-core/zlib"
      openssl_name = "kandelo-dev/tap-core/openssl"
      zlib_rack = Pathname(dir)/"Cellar/zlib"
      openssl_rack = Pathname(dir)/"Cellar/openssl"
      zlib_keg = zlib_rack/"1.3.1_2"
      openssl_keg = openssl_rack/"3.3.2_2"
      (zlib_keg/"lib/pkgconfig").mkpath
      (openssl_keg/"share/pkgconfig").mkpath
      harness.dependency_formulae = {
        zlib_name    => InstalledFormula.new(rack: zlib_rack, pkg_version: "1.3.1_2"),
        openssl_name => InstalledFormula.new(rack: openssl_rack, pkg_version: "3.3.2_2"),
      }
      # Homebrew returns the declared runtime closure; these entries model a
      # direct target dep, its transitive target dep, a native dep, and a
      # duplicate closure entry.
      zlib_dependency = DependencyFormula.new(
        full_name: zlib_name, opt_bin: Pathname("/prefix/opt/zlib/bin"),
        opt_sbin: Pathname("/prefix/opt/zlib/sbin"), opt_libexec: Pathname("/prefix/opt/zlib/libexec")
      )
      openssl_dependency = DependencyFormula.new(
        full_name: openssl_name, opt_bin: Pathname("/prefix/opt/openssl/bin"),
        opt_sbin: Pathname("/prefix/opt/openssl/sbin"), opt_libexec: Pathname("/prefix/opt/openssl/libexec")
      )
      native_dependency = DependencyFormula.new(
        full_name: "pkgconf", opt_bin: Pathname("/prefix/opt/pkgconf/bin"),
        opt_sbin: Pathname("/prefix/opt/pkgconf/sbin"), opt_libexec: Pathname("/prefix/opt/pkgconf/libexec")
      )
      harness.runtime_formulae = [zlib_dependency, native_dependency, openssl_dependency, zlib_dependency]
      ENV["PATH"] = "/usr/bin"
      ENV["PKG_CONFIG_PATH"] = "/caller/selection/lib/pkgconfig"
      ENV["WASM_POSIX_DEP_PKG_CONFIG_PATH"] = "/ambient/native/lib/pkgconfig"

      harness.kandelo_activate_sdk!

      expected = [openssl_keg/"share/pkgconfig", zlib_keg/"lib/pkgconfig"].map(&:to_s).sort
      assert_equal expected.join(File::PATH_SEPARATOR), ENV.fetch("WASM_POSIX_DEP_PKG_CONFIG_PATH")
      assert_equal "/caller/selection/lib/pkgconfig", ENV.fetch("PKG_CONFIG_PATH")
      refute_includes ENV.fetch("WASM_POSIX_DEP_PKG_CONFIG_PATH"), "/prefix/opt/"
      refute_includes ENV.fetch("WASM_POSIX_DEP_PKG_CONFIG_PATH"), "/prefix/opt/pkgconf"
    end
  ensure
    ENV.replace(original) if original
  end

  def test_pkg_config_declaration_skips_missing_native_and_undeclared_dirs
    original = ENV.to_hash
    Dir.mktmpdir("kandelo-pkg-config-missing") do |dir|
      harness = Harness.new
      declared_name = "kandelo-dev/tap-core/ncurses"
      undeclared_name = "kandelo-dev/tap-core/openssl"
      declared_rack = Pathname(dir)/"Cellar/ncurses"
      declared_keg = declared_rack/"6.5_2"
      undeclared_rack = Pathname(dir)/"Cellar/openssl"
      declared_keg.mkpath
      (undeclared_rack/"3.3.2_2/lib/pkgconfig").mkpath
      harness.dependency_formulae = {
        declared_name   => InstalledFormula.new(rack: declared_rack, pkg_version: "6.5_2"),
        undeclared_name => InstalledFormula.new(rack: undeclared_rack, pkg_version: "3.3.2_2"),
      }
      harness.runtime_formulae = [
        DependencyFormula.new(full_name: declared_name),
        DependencyFormula.new(full_name: "pkgconf"),
      ]
      ENV["PKG_CONFIG_PATH"] = "/caller/selection/lib/pkgconfig"
      ENV["WASM_POSIX_DEP_PKG_CONFIG_PATH"] = "/ambient/native/lib/pkgconfig"

      harness.kandelo_export_target_pkg_config_path!

      assert_equal "", ENV.fetch("WASM_POSIX_DEP_PKG_CONFIG_PATH")
      assert_equal "/caller/selection/lib/pkgconfig", ENV.fetch("PKG_CONFIG_PATH")
    end
  ensure
    ENV.replace(original) if original
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

  def test_texlive_build_runner_uses_the_bound_support_child_and_escaped_arguments
    harness = Harness.new
    harness.define_singleton_method(:kandelo_host_tool) { |name| "/host tools/#{name}" }

    harness.kandelo_run_texlive_pdftex("engine", "/source tree", "$(false)")

    expected_runner = Pathname(__dir__).parent/"build-texlive-pdftex.sh"
    assert_equal ["/host tools/bash", "-c"], harness.system_args.first(2)
    assert_equal ["/host tools/bash", expected_runner.to_s, "engine", "/source tree", "$(false)"],
                 Shellwords.shellsplit(harness.system_args.fetch(2))
  end

  def test_texlive_config_runner_uses_the_bound_support_child_and_module_root
    harness = Harness.new
    harness.define_singleton_method(:kandelo_host_tool) { |name| "/host tools/#{name}" }

    harness.kandelo_generate_texlive_runtime_config("/module root", "/runtime root", "selected packages")

    expected_runner = Pathname(__dir__).parent/"generate-texlive-runtime-config.pl"
    assert_equal ["/host tools/bash", "-c"], harness.system_args.first(2)
    assert_equal [
      "/host tools/perl", "-I/module root", expected_runner.to_s, "/runtime root", "selected packages"
    ], Shellwords.shellsplit(harness.system_args.fetch(2))
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
      wasm.binwrite("/opt/kandelo/homebrew/opt/formula")

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

  def test_host_build_path_keeps_native_tools_and_removes_all_target_entry_points
    harness = Harness.new
    harness.homebrew_prefix_path = Pathname("/prefix")
    harness.runtime_formulae = [
      DependencyFormula.new(
        full_name:   "kandelo-dev/tap-core/coreutils",
        opt_bin:     Pathname("/prefix/opt/coreutils/bin"),
        opt_sbin:    Pathname("/prefix/opt/coreutils/sbin"),
        opt_libexec: Pathname("/prefix/opt/coreutils/libexec"),
      ),
      DependencyFormula.new(
        full_name:   "rust",
        opt_bin:     Pathname("/prefix/opt/rust/bin"),
        opt_sbin:    Pathname("/prefix/opt/rust/sbin"),
        opt_libexec: Pathname("/prefix/opt/rust/libexec"),
      ),
    ]
    original = ENV.to_hash
    ENV["PATH"] = [
      "/prefix/bin",
      "/prefix/sbin",
      "/prefix/opt/coreutils/bin",
      "/prefix/opt/coreutils/sbin",
      "/prefix/opt/coreutils/libexec/bin",
      "/prefix/opt/rust/bin",
      "/usr/bin",
    ].join(File::PATH_SEPARATOR)

    harness.kandelo_isolate_host_build_path!
    build_path = ENV.fetch("PATH").split(File::PATH_SEPARATOR)

    refute_includes build_path, "/prefix/bin"
    refute_includes build_path, "/prefix/sbin"
    refute_includes build_path, "/prefix/opt/coreutils/bin"
    refute_includes build_path, "/prefix/opt/coreutils/sbin"
    refute_includes build_path, "/prefix/opt/coreutils/libexec/bin"
    assert_includes build_path, "/prefix/opt/rust/bin"
    assert_includes build_path, "/usr/bin"
  ensure
    ENV.replace(original) if original
  end

  def test_ruby_declares_every_closed_recipe_native_build_dependency
    formula = File.read(File.expand_path("../../../Formula/ruby.rb", __dir__))
    native_declarations = formula.lines.grep(
      /^\s*depends_on (?:"(?:gpatch|llvm|make|perl|python@3\.13|unzip)"|KandeloFormulaSupport::(?:Binaryen|Wabt)Requirement) => :build/,
    )

    assert_equal [
      %Q(  depends_on "gpatch" => :build\n),
      "  depends_on KandeloFormulaSupport::BinaryenRequirement => :build\n",
      "  depends_on KandeloFormulaSupport::WabtRequirement => :build\n",
      %Q(  depends_on "llvm" => :build\n),
      %Q(  depends_on "make" => :build\n),
      %Q(  depends_on "perl" => :build\n),
      %Q(  depends_on "python@3.13" => :build\n),
      %Q(  depends_on "unzip" => :build\n),
    ], native_declarations

    assert_includes formula, "  KANDELO_TAP_RECIPE = true\n"
    assert_includes formula, 'depends_on "kandelo-dev/tap-core/libyaml"'
    assert_includes formula, "kandelo_build_tap_recipe("
    assert_includes formula,
                    '"WASM_POSIX_DEP_PATCH"        => formula_opt_bin("gpatch")/"patch"'
    assert_includes formula,
                    '"WASM_POSIX_DEP_MAKE"         => formula_opt_bin("make")/"make"'
    assert_includes formula,
                    '"WASM_POSIX_DEP_PERL"         => formula_opt_bin("perl")/"perl"'
    assert_includes formula,
                    '"WASM_POSIX_DEP_PYTHON"       => formula_opt_bin("python@3.13")/"python3.13"'
    refute_includes formula, 'depends_on "rust" => :build'
    refute_includes formula, "KANDELO_REGISTRY_BRIDGE"
    refute_includes formula, "kandelo_build_package("

    recipe = File.read(File.expand_path("../../recipes/ruby/build.sh", __dir__), encoding: "UTF-8")
    refute_match(/\b(?:curl|wget)\b/, recipe)
    refute_includes recipe, "build-deps resolve"
    refute_includes recipe, "install-local-binary"
  end

  def test_ruby_closed_recipe_uses_only_sealed_source_and_transform_inputs
    recipe = File.read(
      File.expand_path("../../recipes/ruby/build.sh", __dir__),
      encoding: "UTF-8",
    )

    assert_includes recipe, 'SOURCE_INPUT="${WASM_POSIX_DEP_SOURCE_DIR:?}"'
    assert_includes recipe, 'SRC_DIR="$WORK_DIR/ruby-source"'
    assert_includes recipe,
                    'cp -a --no-preserve=ownership "$SOURCE_INPUT/." "$SRC_DIR/"'
    assert_includes recipe,
                    'find -P "$SRC_DIR" -type d -exec chmod u+rwx {} +'
    assert_includes recipe,
                    'find -P "$SRC_DIR" -type f -exec chmod u+rw {} +'
    assert_includes recipe,
                    'cp -a --no-preserve=ownership "$SOURCE_SYSROOT/." "$SYSROOT/"'
    assert_includes recipe,
                    'find -P "$SYSROOT" -type d -exec chmod u+rwx {} +'
    assert_includes recipe,
                    'find -P "$SYSROOT" -type f -exec chmod u+rw {} +'
    assert_includes recipe,
                    'ROOT_SPILL="${WASM_POSIX_LOCAL_ROOT_SPILL:?}"'
    assert_includes recipe,
                    'FORK_INSTRUMENT="${WASM_POSIX_FORK_INSTRUMENT:?}"'
    assert_includes recipe, 'PATCH="${WASM_POSIX_DEP_PATCH:?}"'
    assert_includes recipe, 'MAKE="${WASM_POSIX_DEP_MAKE:?}"'
    assert_includes recipe, 'PERL="${WASM_POSIX_DEP_PERL:?}"'
    assert_includes recipe, 'PYTHON="${WASM_POSIX_DEP_PYTHON:?}"'
    assert_includes recipe, '[ ! -x "$MAKE" ]'
    assert_includes recipe, '[ ! -x "$PATCH" ]'
    assert_includes recipe, '[ ! -x "$PERL" ]'
    assert_includes recipe, '[ ! -x "$PYTHON" ]'
    assert_includes recipe,
                    '"$PATCH" -d "$SRC_DIR" -p1 < "$SCRIPT_DIR/patches/kandelo-require-libraries-roots.patch"'
    refute_match(/(^|[^$A-Z_])gpatch(?:\s|$)/, recipe)
    refute_match(/(^|[^$A-Z_])gmake(?:\s|$)/, recipe)
    refute_match(/(^|[^$A-Z_])perl(?:\s|$)/, recipe)
    refute_match(/(^|[^$A-Z_])python3\.13(?:\s|$)/, recipe)
    refute_includes recipe, "HOMEBREW_KANDELO_ROOT"
    refute_match(/\bREPO_ROOT\b/, recipe)
    refute_includes recipe, 'SRC_DIR="${WASM_POSIX_DEP_SOURCE_DIR:?}"'
    refute_includes recipe, 'cd "$SOURCE_INPUT"'
    assert_operator recipe.index('cp -a --no-preserve=ownership'), :<,
                    recipe.index("# ─── Source patches for wasm32-posix")
    assert_operator recipe.index('find -P "$SYSROOT" -type f'), :<,
                    recipe.index('cp "$LIBYAML_PREFIX/include/yaml.h"')
  end

  def test_ruby_exercises_the_installed_guest_runtime_without_rubylib
    formula = File.read(File.expand_path("../../../Formula/ruby.rb", __dir__))

    assert_includes formula, "  revision 2\n"
    assert_includes formula, '"WASM_POSIX_DEP_GUEST_PREFIX" => GUEST_OPT_PREFIX'
    assert_includes formula, "guest_files: runtime_files"
    assert_includes formula, "raise 'RUBYLIB leaked into installed runtime test'"
    assert_includes formula, 'browser_program = program.sub("ruby-runtime-ok", "ruby-browser-runtime-ok")'
    assert_includes formula, "kandelo_run_browser_wasm("
    assert_includes formula, "allow_stderr: false"
    assert_includes(
      formula,
      'assert_equal "ruby-browser-runtime-ok:4.0.5:rubygems-4.0.10:bundler-4.0.10\\n", browser_output',
    )
    %w[gem bundle bundler].each do |command|
      assert_match(/"#{Regexp.escape(command)}"\s*=>/, formula)
    end
    refute_match(/"RUBYLIB"\s*=>/, formula)
  end

  def test_ruby_closed_recipe_owns_the_posix_spawn_backend
    recipe_root = Pathname(File.expand_path("../../recipes/ruby", __dir__))
    build = (recipe_root/"build.sh").binread
    patch_path = recipe_root/"patches/kandelo-posix-spawn.patch"
    patch = patch_path.binread
    manifest_path = recipe_root/"recipe.json"
    manifest = JSON.parse(manifest_path.binread)
    manifest_paths = manifest.fetch("files").map { |entry| entry.fetch("path") }
    assert_equal manifest_paths.sort, manifest_paths
    build_record = manifest.fetch("files").find do |entry|
      entry.fetch("path") == "build.sh"
    end
    patch_record = manifest.fetch("files").find do |entry|
      entry.fetch("path") == "patches/kandelo-posix-spawn.patch"
    end

    assert_includes(
      build,
      '"$PATCH" -d "$SRC_DIR" -p1 < "$SCRIPT_DIR/patches/kandelo-posix-spawn.patch"',
    )
    refute_nil build_record
    assert_equal build.bytesize, build_record.fetch("bytes")
    assert_equal Digest::SHA256.hexdigest(build), build_record.fetch("sha256")
    assert_equal "0755", build_record.fetch("mode")
    refute_nil patch_record
    assert_equal patch.bytesize, patch_record.fetch("bytes")
    assert_equal Digest::SHA256.hexdigest(patch), patch_record.fetch("sha256")
    assert_equal "0644", patch_record.fetch("mode")
    %w[
      kandelo_execarg_can_posix_spawn
      kandelo_execarg_has_independent_redirects
      kandelo_execarg_clear_nonblock_stdio
      kandelo_execarg_restore_fd_flags
      posix_spawn_file_actions_adddup2
      posix_spawn_file_actions_addchdir
      POSIX_SPAWN_SETSIGMASK
      POSIX_SPAWN_SETSIGDEF
      POSIX_SPAWN_SETPGROUP
      ARGVSTR2ARGV
      RB_IMEMO_TMPBUF_PTR
      rb_is_absolute_path
      handle_fork_error
    ].each { |contract| assert_includes patch, contract }
    assert_includes patch, "eargp->close_others_do"
    assert_includes patch, "eargp->fd_close != Qfalse"
    assert_includes patch, "pid >= 0 || errno != ENOEXEC"
    refute_includes patch, "posix_spawn_file_actions_addclose"
    assert_operator patch.index("prefork();"), :<,
                    patch.index("posix_spawn_file_actions_init(&actions)")

    formula = File.read(File.expand_path("../../../Formula/ruby.rb", __dir__))
    assert_includes(
      formula,
      %Q(manifest_sha256: "#{Digest::SHA256.hexdigest(manifest_path.binread)}"),
    )
  end

  def test_ruby_exercises_homebrew_system_command_spawn_shape_on_both_hosts
    formula = File.read(File.expand_path("../../../Formula/ruby.rb", __dir__))
    separator_line = formula.lines.find do |line|
      line.include?("File.binread('/proc/self/cmdline')")
    end

    # WHY: the outer Formula heredoc must retain one backslash for the nested
    # Ruby program. A single source backslash would become a literal NUL in the
    # Process.spawn argv and fail before Ruby could start the child.
    assert_includes separator_line, 'split("\\\\0", -1)'
    assert_includes formula, 'refute_includes spawn_program, "\\0"'
    assert_includes(
      formula,
      %q(assert_includes spawn_program, 'split("\\0", -1)'),
    )
    assert_includes formula, "Process.spawn("
    assert_includes formula, "[executable, executable]"
    assert_includes formula, "in: input_read"
    assert_includes formula, "out: output_write"
    assert_includes formula, "err: error_write"
    assert_includes formula, "pgroup: true"
    assert_includes formula, 'chdir: "/tmp"'
    # WHY: spawn_program is an interpolating heredoc. Keep the numeric
    # captures escape-free so the transmitted guest program cannot silently
    # turn Ruby's `\d` into the literal character `d`.
    assert_includes formula, "pid_field = stdout[/^pid=([0-9]+)$/, 1]"
    assert_includes formula, "pgrp_field = stdout[/^pgrp=([0-9]+)$/, 1]"
    assert_includes formula, "kandelo_run_wasm("
    assert_includes formula, "kandelo_run_browser_wasm("
    assert_includes formula, "exec_programs: spawn_exec_programs"
    assert_includes(
      formula,
      'assert_equal "ruby-homebrew-spawn-ok\\n", spawn_output',
    )
    assert_includes(
      formula,
      'assert_equal "ruby-browser-homebrew-spawn-ok\\n", browser_spawn_output',
    )
  end

  def test_nethack_declares_its_canonical_dotted_version
    formula = File.read(File.expand_path("../../../Formula/nethack.rb", __dir__))
    version_declarations = formula.lines.grep(/^\s*version /)

    assert_equal [%Q(  version "3.6.7"\n)], version_declarations
  end

  def test_nethack_is_a_closed_tap_recipe_with_declared_tools
    formula = File.read(File.expand_path("../../../Formula/nethack.rb", __dir__))

    assert_includes formula, "  KANDELO_TAP_RECIPE = true\n"
    assert_includes formula, "  revision 1\n"
    assert_includes formula, "kandelo_build_tap_recipe("
    assert_includes formula, 'depends_on "bison" => :build'
    assert_includes formula, 'depends_on "flex" => :build'
    assert_includes formula, 'depends_on "gpatch" => :build'
    assert_includes formula, 'depends_on "llvm" => :build'
    assert_includes formula, 'depends_on "make" => :build'
    assert_includes formula, 'depends_on "kandelo-dev/tap-core/ncurses"'
    refute_includes formula, "KANDELO_REGISTRY_BRIDGE"
    refute_includes formula, "kandelo_build_package("
  end

  def test_nethack_recipe_is_complete_and_has_no_registry_authority
    recipe_root = Pathname(File.expand_path("../../recipes/nethack", __dir__))
    manifest_path = recipe_root/"recipe.json"
    manifest = JSON.parse(manifest_path.binread)
    paths = manifest.fetch("files").map { |entry| entry.fetch("path") }

    assert_equal 1, manifest.fetch("schema")
    assert_equal ["kandelo-dev/tap-core/ncurses"], manifest.fetch("dependencies")
    assert_equal "build.sh", manifest.fetch("entrypoint")
    assert_equal paths.sort, paths
    assert_equal [
      "build.sh",
      "patches/kandelo-portable-data-layout.patch",
      "patches/kandelo-terminal.patch",
    ], paths
    manifest.fetch("files").each do |record|
      bytes = (recipe_root/record.fetch("path")).binread
      assert_equal bytes.bytesize, record.fetch("bytes")
      assert_equal Digest::SHA256.hexdigest(bytes), record.fetch("sha256")
      assert_equal(record.fetch("path") == "build.sh" ? "0755" : "0644",
                   record.fetch("mode"))
    end

    build = (recipe_root/"build.sh").binread
    assert_includes build, 'SOURCE_INPUT="${WASM_POSIX_DEP_SOURCE_DIR:?}"'
    assert_includes build, 'NCURSES_PREFIX="${WASM_POSIX_DEP_NCURSES_DIR:?}"'
    assert_includes build, 'FORK_INSTRUMENT="${WASM_POSIX_FORK_INSTRUMENT:?}"'
    assert_includes build, 'HOST_CC="${WASM_POSIX_DEP_HOST_CC:?}"'
    assert_includes build, 'MAKE="${WASM_POSIX_DEP_MAKE:?}"'
    assert_includes build, 'PATCH="${WASM_POSIX_DEP_PATCH:?}"'
    assert_includes build, 'BISON="${WASM_POSIX_DEP_BISON:?}"'
    assert_includes build, 'FLEX="${WASM_POSIX_DEP_FLEX:?}"'
    refute_match(/\b(?:curl|wget)\b/, build)
    refute_includes build, "build-deps resolve"
    refute_includes build, "install-local-binary"
    refute_includes build, "WASM_POSIX_BINARY_CACHE_ROOT"
    refute_includes build, "HOMEBREW_KANDELO_ROOT"
    refute_match(/\bREPO_ROOT\b/, build)
  end

  def test_changed_tier2_formulae_keep_the_reviewed_abi42_bottle_identity
    # These Formulae already consumed rebuild 2 during the ABI 42 bottle
    # rebuild. The canonical-prefix bottles must use the next identity because
    # GHCR's Homebrew references do not include the Kandelo ABI.
    %w[bc fbdoom lsof modeset netcat posix-utils-lite].each do |name|
      formula = File.read(File.expand_path("../../../Formula/#{name}.rb", __dir__))
      rebuild_declarations = formula.lines.grep(/^\s*rebuild /)

      assert_equal [%Q(    rebuild 3\n)], rebuild_declarations, name
    end
  end

  def test_sdk_activation_cannot_reintroduce_the_global_homebrew_path
    harness = Harness.new
    harness.homebrew_prefix_path = Pathname("/prefix")
    harness.root_path = "/tmp/kandelo-root"
    harness.runtime_formulae = []
    original = ENV.to_hash
    ENV["PATH"] = ["/prefix/opt/cmake/bin", "/usr/bin"].join(File::PATH_SEPARATOR)
    ENV["HOMEBREW_KANDELO_NODE"] = "/prefix/bin/node"
    ENV["HOMEBREW_KANDELO_LLVM_BIN"] = "/prefix/bin"

    harness.kandelo_activate_sdk!
    build_path = ENV.fetch("PATH").split(File::PATH_SEPARATOR)

    refute_includes build_path, "/prefix/bin"
    assert_includes build_path, "/tmp/kandelo-root/sdk/bin"
    assert_includes build_path, "/prefix/opt/cmake/bin"
    assert_includes build_path, "/usr/bin"
  ensure
    ENV.replace(original) if original
  end

  def test_wasm_build_clears_cmake_host_search_paths_and_restores_environment
    harness = Harness.new
    harness.homebrew_prefix_path = Pathname("/prefix")
    harness.root_path = "/tmp/kandelo-root"
    harness.runtime_formulae = []
    original = ENV.to_hash
    ENV["PATH"] = ["/prefix/bin", "/usr/bin"].join(File::PATH_SEPARATOR)
    cmake_search_variables = %w[
      CMAKE_APPBUNDLE_PATH
      CMAKE_FRAMEWORK_PATH
      CMAKE_INCLUDE_PATH
      CMAKE_LIBRARY_PATH
      CMAKE_PREFIX_PATH
      CMAKE_PROGRAM_PATH
    ]
    cmake_search_variables.each { |key| ENV[key] = "/prefix" }
    ENV["LIBRARY_PATH"] = "/prefix/opt/xz/lib"
    ENV["LD_RUN_PATH"] = "/prefix/opt/xz/lib"
    scoped = ENV.to_hash

    build_environment = nil
    harness.kandelo_wasm_build { build_environment = ENV.to_hash }

    refute_includes build_environment.fetch("PATH").split(File::PATH_SEPARATOR), "/prefix/bin"
    cmake_search_variables.each { |key| refute build_environment.key?(key) }
    refute build_environment.key?("LIBRARY_PATH")
    refute build_environment.key?("LD_RUN_PATH")
    assert_equal scoped, ENV.to_hash
  ensure
    ENV.replace(original) if original
  end

  def test_sysroot_activation_clears_host_linker_search_paths
    harness = Harness.new
    original = ENV.to_hash
    ENV.delete("HOMEBREW_KANDELO_SYSROOT")
    ENV["LIBRARY_PATH"] = "/prefix/opt/xz/lib"
    ENV["LD_RUN_PATH"] = "/prefix/opt/xz/lib"

    harness.kandelo_activate_sysroot!("/tmp/kandelo-root")

    refute ENV.key?("LIBRARY_PATH")
    refute ENV.key?("LD_RUN_PATH")
    assert_equal "/tmp/kandelo-root/sysroot", ENV.fetch("WASM_POSIX_SYSROOT")
  ensure
    ENV.replace(original) if original
  end

  def test_sysroot_activation_uses_the_protected_publisher_sysroot
    harness = Harness.new
    original = ENV.to_hash
    ENV["HOMEBREW_KANDELO_SYSROOT"] = "/protected/source-aliases/sysroot"
    ENV["WASM_POSIX_SYSROOT"] = "/caller/poison"

    harness.kandelo_activate_sysroot!("/tmp/pristine-kandelo-source")

    assert_equal "/protected/source-aliases/sysroot", ENV.fetch("WASM_POSIX_SYSROOT")
    assert_equal "/tmp/pristine-kandelo-source/libc/glue", ENV.fetch("WASM_POSIX_GLUE_DIR")
  ensure
    ENV.replace(original) if original
  end

  def test_wasm_build_scopes_target_pkg_config_declaration_and_restores_environment
    original = ENV.to_hash
    Dir.mktmpdir("kandelo-pkg-config-scope") do |dir|
      harness = Harness.new
      harness.homebrew_prefix_path = Pathname("/prefix")
      harness.root_path = "/tmp/kandelo-root"
      target = "kandelo-dev/tap-core/zlib"
      rack = Pathname(dir)/"Cellar/zlib"
      keg = rack/"1.3.1_2"
      (keg/"lib/pkgconfig").mkpath
      harness.dependency_formulae = {
        target => InstalledFormula.new(rack:, pkg_version: "1.3.1_2"),
      }
      harness.runtime_formulae = [
        DependencyFormula.new(
          full_name: target, opt_bin: Pathname("/prefix/opt/zlib/bin"),
          opt_sbin: Pathname("/prefix/opt/zlib/sbin"), opt_libexec: Pathname("/prefix/opt/zlib/libexec")
        ),
      ]
      ENV["PATH"] = ["/prefix/bin", "/usr/bin"].join(File::PATH_SEPARATOR)
      ENV["PKG_CONFIG_PATH"] = "/caller/selection/lib/pkgconfig"
      ENV["WASM_POSIX_DEP_PKG_CONFIG_PATH"] = "/ambient/native/lib/pkgconfig"
      scoped = ENV.to_hash

      build_environment = nil
      harness.kandelo_wasm_build { build_environment = ENV.to_hash }

      assert_equal (keg/"lib/pkgconfig").to_s,
                   build_environment.fetch("WASM_POSIX_DEP_PKG_CONFIG_PATH")
      assert_equal "/caller/selection/lib/pkgconfig", build_environment.fetch("PKG_CONFIG_PATH")
      assert_equal scoped, ENV.to_hash
    end
  ensure
    ENV.replace(original) if original
  end

  def test_every_node_and_chromium_runner_propagates_only_the_frozen_resolver_authority
    original = ENV.to_hash
    Dir.mktmpdir("kandelo-formula-checker-runners") do |dir|
      root = Pathname(dir)/"kandelo root"
      test_path = Pathname(dir)/"formula test"
      checker = root/"target/host triple/release/xtask"
      binary_cache_root = root/KandeloFormulaSupport::KANDELO_PORTABLE_BINARY_CACHE_BASENAME
      [root, test_path, checker.dirname, binary_cache_root/"programs"].each(&:mkpath)
      checker.binwrite("sealed checker\n")
      checker.chmod(0555)
      ENV["HOMEBREW_KANDELO_XTASK_BIN"] = "/caller/mutable/xtask"
      ENV["WASM_POSIX_BINARY_CACHE_ROOT"] = "/caller/raw/cache"
      ENV["WASM_POSIX_BINARY_RESOLVER_REPO_ROOT"] = "/caller/raw/root"
      ENV["WASM_POSIX_XTASK_BIN"] = "/caller/raw/xtask"

      harness = Harness.new
      harness.root_path = root.to_s
      harness.test_path = test_path
      harness.formula_binary_cache_root = binary_cache_root.to_s.freeze
      harness.formula_checker_path = checker.to_s.freeze
      harness.formula_resolver_repo_root = root.to_s.freeze
      invocations = {
        "default Node runner" => lambda do
          harness.shell_result = "runtime-ok\n"
          harness.kandelo_run_wasm("program.wasm", [])
        end,
        "isolated Node runner" => lambda do
          harness.shell_result = "runtime-ok\n"
          harness.kandelo_run_wasm("program.wasm", [], network: true)
        end,
        "HTTP service runner" => lambda do
          harness.shell_result = "[]"
          harness.kandelo_run_http_service(
            "program.wasm", [], port: 8080, requests: [{ path: "/" }]
          )
        end,
        "PTY runner" => lambda do
          harness.shell_result = "runtime-ok\n"
          harness.kandelo_run_pty_wasm("program.wasm", [], inputs: [])
        end,
        "KMS Node runner" => lambda do
          harness.shell_result = "runtime-ok\n"
          harness.kandelo_run_kms_wasm("program.wasm")
        end,
        "KMS Chromium runner" => lambda do
          harness.shell_result = "runtime-ok\n"
          harness.kandelo_run_kms_browser_wasm("program.wasm")
        end,
        "general Chromium runner" => lambda do
          harness.shell_result = "runtime-ok\n"
          harness.kandelo_run_browser_wasm("program.wasm", [])
        end,
        "framebuffer Chromium runner" => lambda do
          harness.shell_result = "runtime-ok\n"
          harness.kandelo_run_framebuffer_wasm("program.wasm")
        end,
      }

      invocations.each do |label, invoke|
        invoke.call
        prefixes = %w[
          WASM_POSIX_BINARY_CACHE_ROOT=
          WASM_POSIX_BINARY_RESOLVER_REPO_ROOT=
          WASM_POSIX_XTASK_BIN=
        ]
        assignments = Shellwords.shellsplit(harness.command).select do |token|
          prefixes.any? { |prefix| token.start_with?(prefix) }
        end
        assert_equal(
          [
            "WASM_POSIX_BINARY_CACHE_ROOT=#{binary_cache_root}",
            "WASM_POSIX_BINARY_RESOLVER_REPO_ROOT=#{root}",
            "WASM_POSIX_XTASK_BIN=#{checker}",
          ],
          assignments,
          label,
        )
        refute_includes harness.command, "/caller/mutable/xtask", label
        refute_includes harness.command, "/caller/raw/cache", label
        refute_includes harness.command, "/caller/raw/root", label
        refute_includes harness.command, "/caller/raw/xtask", label
      end
    end
  ensure
    ENV.replace(original) if original
  end

  def test_formula_runners_keep_ordinary_nonpublisher_execution_when_checker_bridge_is_absent
    harness = Harness.new

    assert_equal "", harness.kandelo_node_runner_environment
    harness.kandelo_run_wasm("program.wasm", [])

    refute_includes harness.command, "WASM_POSIX_BINARY_CACHE_ROOT="
    refute_includes harness.command, "WASM_POSIX_BINARY_RESOLVER_REPO_ROOT="
    refute_includes harness.command, "WASM_POSIX_XTASK_BIN="
  end

  def test_node_process_receives_the_frozen_resolver_authority_instead_of_mutable_caller_environment
    original = ENV.to_hash
    Dir.mktmpdir("kandelo-formula-checker-process") do |dir|
      root = Pathname(dir)/"kandelo root"
      fake_bin = Pathname(dir)/"fake bin"
      checker = root/"target/host/release/xtask"
      binary_cache_root = root/KandeloFormulaSupport::KANDELO_PORTABLE_BINARY_CACHE_BASENAME
      [root, fake_bin, checker.dirname, binary_cache_root/"programs"].each(&:mkpath)
      checker.binwrite("sealed checker\n")
      checker.chmod(0555)
      fake_node = fake_bin/"node"
      fake_node.binwrite <<~SH
        #!/bin/sh
        printf '%s\\n' "$WASM_POSIX_BINARY_CACHE_ROOT"
        printf '%s\\n' "$WASM_POSIX_BINARY_RESOLVER_REPO_ROOT"
        printf '%s\\n' "$WASM_POSIX_XTASK_BIN"
      SH
      fake_node.chmod(0755)
      ENV["PATH"] = [fake_bin, ENV.fetch("PATH")].join(File::PATH_SEPARATOR)
      ENV.delete("HOMEBREW_KANDELO_NODE")
      ENV["HOMEBREW_KANDELO_XTASK_BIN"] = "/caller/homebrew/xtask"
      ENV["WASM_POSIX_BINARY_CACHE_ROOT"] = "/caller/raw/cache"
      ENV["WASM_POSIX_BINARY_RESOLVER_REPO_ROOT"] = "/caller/raw/root"
      ENV["WASM_POSIX_XTASK_BIN"] = "/caller/raw/xtask"

      [false, true].each do |network|
        harness = RuntimeHarness.new
        harness.root_path = root.to_s
        harness.formula_binary_cache_root = binary_cache_root.to_s.freeze
        harness.formula_checker_path = checker.to_s.freeze
        harness.formula_resolver_repo_root = root.to_s.freeze

        output = harness.kandelo_run_wasm(
          "program.wasm", [],
          env: {
            "WASM_POSIX_BINARY_CACHE_ROOT"         => "/caller/formula-env/cache",
            "WASM_POSIX_BINARY_RESOLVER_REPO_ROOT" => "/caller/formula-env/root",
            "WASM_POSIX_XTASK_BIN"                 => "/caller/formula-env/xtask",
          },
          network:,
        )

        assert_equal "#{binary_cache_root}\n#{root}\n#{checker}\n", output
      end
    end
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

  def test_merge_stderr_constructs_guest_only_capture_for_both_formula_runners
    Dir.mktmpdir("kandelo-formula-guest-output") do |dir|
      root = Pathname(dir)/"kandelo root"
      test_path = Pathname(dir)/"formula test"
      root.mkpath
      test_path.mkpath

      [[false, "examples/run-example.ts"], [true, "run-network-wasm.ts"]].each do |network, runner|
        harness = GuestOutputHarness.new
        harness.root_path = root.to_s
        harness.test_path = test_path
        harness.guest_output = "guest stdout\nguest stderr\n"
        harness.shell_result = "host process stdout\n"

        _, diagnostic_output = capture_io do
          output = harness.kandelo_run_wasm(
            "program.wasm", [], merge_stderr: true, network:, expected_status: 1
          )
          assert_equal "guest stdout\nguest stderr\n", output
        end

        assert_equal "host process stdout\n", diagnostic_output
        assert_includes harness.command, runner
        assert_includes harness.command, "KANDELO_GUEST_OUTPUT_FILE="
        refute_includes harness.command, "2>&1"
        assert_equal 1, harness.expected_status
        refute_path_exists test_path/".program.wasm.guest-output"
      end
    end
  end

  def test_merge_stderr_runtime_keeps_host_diagnostics_out_of_guest_output
    with_fake_formula_node do |root, test_path|
      [false, true].each do |network|
        harness = RuntimeHarness.new
        harness.root_path = root.to_s
        harness.test_path = test_path

        _, diagnostic_output = capture_io do
          output = harness.kandelo_run_wasm(
            "program.wasm", [], merge_stderr: true, network:, expected_status: 1
          )
          assert_equal "guest stdout\nguest stderr\n", output
        end

        assert_equal "", harness.process_stdout
        assert_equal "host diagnostic\n", harness.process_stderr
        assert_equal "host diagnostic\n", diagnostic_output
        refute_includes harness.command, "2>&1"
      end
    end
  end

  def test_merge_stderr_runtime_prints_guest_output_when_status_is_unexpected
    with_fake_formula_node do |root, test_path|
      [false, true].each do |network|
        harness = RuntimeHarness.new
        harness.root_path = root.to_s
        harness.test_path = test_path

        _, diagnostic_output = capture_io do
          error = assert_raises(RuntimeError) do
            harness.kandelo_run_wasm(
              "program.wasm", [], merge_stderr: true, network:, expected_status: 0
            )
          end
          assert_equal "unexpected exit status 1", error.message
        end

        assert_equal "host diagnostic\nguest stdout\nguest stderr\n", diagnostic_output
        refute_path_exists test_path/".program.wasm.guest-output"
      end
    end
  end

  def test_execution_rejects_invalid_expected_fork_descendant_count
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_wasm(
        "program.wasm", [], expected_fork_descendants: -1
      )
    end

    assert_includes error.message, "expected fork descendant count must be a nonnegative integer"
  end

  def test_execution_passes_exact_expected_fork_descendant_statuses
    harness = Harness.new
    harness.kandelo_run_wasm(
      "program.wasm", [], expected_fork_descendant_statuses: [0, 143]
    )

    assert_includes harness.command, "run-network-wasm.ts"
    assert_includes harness.command, "KANDELO_FORMULA_EXPECTED_FORK_DESCENDANT_STATUSES_JSON=\\[0,143\\]"
    refute_includes harness.command, "KANDELO_FORMULA_EXPECTED_FORK_DESCENDANTS="
  end

  def test_execution_rejects_invalid_expected_fork_descendant_statuses
    [[], [0, -1], [0, 256], [0, 1.5], "0,143"].each do |statuses|
      error = assert_raises(RuntimeError) do
        Harness.new.kandelo_run_wasm(
          "program.wasm", [], expected_fork_descendant_statuses: statuses
        )
      end

      assert_includes error.message, "expected fork descendant statuses must be a nonempty array of byte integers"
    end
  end

  def test_execution_rejects_combined_fork_descendant_count_and_statuses
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_wasm(
        "program.wasm", [],
        expected_fork_descendants:         2,
        expected_fork_descendant_statuses: [0, 143]
      )
    end

    assert_includes error.message, "expected fork descendant count and statuses cannot both be set"
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
    Dir.mktmpdir("kandelo-formula-guest-files") do |dir|
      harness = Harness.new
      harness.test_path = Pathname(dir)/"formula test"
      harness.test_path.mkpath
      guest_files = { "/etc/service.conf" => "/formula/service.conf" }

      harness.kandelo_run_wasm("program.wasm", [], guest_files:)

      assert_includes harness.command, "run-network-wasm.ts"
      assignment = Shellwords.shellsplit(harness.command).find do |token|
        token.start_with?("KANDELO_FORMULA_GUEST_FILES_MANIFEST=")
      end
      refute_nil assignment
      manifest = Pathname(assignment.delete_prefix("KANDELO_FORMULA_GUEST_FILES_MANIFEST="))
      assert_equal guest_files, JSON.parse(manifest.read)
      refute_includes harness.command, "KANDELO_FORMULA_GUEST_FILES_JSON="
      refute_includes harness.command, "/etc/service.conf"
      refute_includes harness.command, "/formula/service.conf"
    end
  end

  def test_execution_keeps_large_guest_file_maps_out_of_argv_and_environment
    original = ENV.to_hash
    Dir.mktmpdir("kandelo-formula-large-guest-files") do |dir|
      root = Pathname(dir)/"kandelo root"
      fake_bin = Pathname(dir)/"fake bin"
      root.mkpath
      fake_bin.mkpath
      fake_node = fake_bin/"node"
      fake_node.binwrite <<~SH
        #!/bin/sh
        set -eu
        test -f "$KANDELO_FORMULA_GUEST_FILES_MANIFEST"
        printf 'manifest-ok\n'
      SH
      fake_node.chmod(0755)
      ENV["PATH"] = [fake_bin, ENV.fetch("PATH")].join(File::PATH_SEPARATOR)
      ENV.delete("HOMEBREW_KANDELO_NODE")

      harness = RuntimeHarness.new
      harness.root_path = root.to_s
      harness.test_path = Pathname(dir)/"formula test"
      harness.test_path.mkpath
      guest_files = 2_085.times.to_h do |index|
        name = "runtime-#{format("%04d", index)}-#{"x" * 48}.vim"
        ["/opt/vim/share/vim/vim92/#{name}", "/formula/vim/runtime/#{name}"]
      end

      output = harness.kandelo_run_wasm("program.wasm", [], guest_files:)

      assert_equal "manifest-ok\n", output
      assignment = Shellwords.shellsplit(harness.command).find do |token|
        token.start_with?("KANDELO_FORMULA_GUEST_FILES_MANIFEST=")
      end
      refute_nil assignment
      manifest = Pathname(assignment.delete_prefix("KANDELO_FORMULA_GUEST_FILES_MANIFEST="))
      assert_equal guest_files, JSON.parse(manifest.read)
      assert_operator manifest.size, :>, 131_072
      assert_operator harness.command.bytesize, :<, 2_048
      refute_includes harness.command, guest_files.keys.last
      refute_includes harness.command, guest_files.values.last
    end
  ensure
    ENV.replace(original) if original
  end

  def test_execution_accepts_guest_argv0_and_writable_host_directory
    harness = Harness.new

    harness.kandelo_run_wasm(
      "program.wasm",
      ["input.tex"],
      argv0:                     "/opt/kandelo/homebrew/opt/texlive/bin/pdflatex",
      exec_programs:             {
        "/opt/kandelo/homebrew/opt/texlive/bin/pdflatex" => "/formula/pdflatex",
      },
      writable_host_directories: { "/work" => "/formula/test-output" },
    )

    assert_includes harness.command, "run-network-wasm.ts"
    assert_includes harness.command, "KANDELO_FORMULA_ARGV0="
    assert_includes harness.command, "KANDELO_FORMULA_WRITABLE_HOST_DIRS_JSON="
    assert_includes harness.command, "/opt/kandelo/homebrew/opt/texlive/bin/pdflatex"
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
      guest_executable = Pathname(dir)/"helper.wasm"
      guest_executable.binwrite("\0asm")
      output = harness.kandelo_run_browser_wasm(
        command, ["-e", "console.log(42)"],
        argv0: "node", guest_program_path: "/opt/node/bin/node", env: { "HOME" => "/root" },
        exec_programs: { "/opt/formula/bin/helper" => guest_executable },
        guest_files: { "/opt/formula/format.dat" => guest_file }, timeout_ms: 5_000
      )

      assert_equal "runtime-ok\n", output
      assert_includes harness.command, "run-browser-wasm.ts"
      assert_includes harness.command, root.to_s.shellescape
      assert_includes harness.command, command.to_s
      assert_includes harness.command, "console.log"
      assert_includes harness.command, "allowStderr"
      assert_includes harness.command, "expectedStatus"
      assert_includes harness.command, "mergeStderr"
      assert_includes harness.command, "node"
      assert_includes harness.command, 'guestProgram\":\"/opt/node/bin/node'
      manifest = harness.test_path/"node.browser-guest-files.json"
      assert_equal({ "/opt/formula/format.dat" => guest_file.to_s }, JSON.parse(manifest.read))
      assert_includes harness.command, manifest.to_s.shellescape
      refute_includes harness.command, guest_file.to_s
      exec_manifest = harness.test_path/"node.browser-exec-programs.json"
      assert_equal(
        { "/opt/formula/bin/helper" => guest_executable.to_s },
        JSON.parse(exec_manifest.read),
      )
      assert_includes harness.command, exec_manifest.to_s.shellescape
      refute_includes harness.command, guest_executable.to_s
      refute_path_exists host_dist
    end
  end

  def test_browser_execution_accepts_expected_nonzero_status_and_merged_stderr
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = Harness.new
      harness.root_path = (Pathname(dir)/"kandelo root").to_s
      harness.test_path = Pathname(dir)/"formula test"
      harness.test_path.mkpath
      command = Pathname(dir)/"getconf"
      command.binwrite("\0asm")

      output = harness.kandelo_run_browser_wasm(
        command, ["NOT_A_VARIABLE"],
        argv0: "getconf", expected_status: 1, merge_stderr: true
      )

      assert_equal "runtime-ok\n", output
      assert_includes harness.command, 'expectedStatus\":1'
      assert_includes harness.command, 'mergeStderr\":true'
    end
  end

  def test_browser_execution_rejects_invalid_expected_status
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_browser_wasm("program.wasm", [], expected_status: 256)
    end

    assert_equal "expected browser status must be an integer from 0 through 255", error.message
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

  def test_browser_execution_rejects_nonabsolute_guest_program_path
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_browser_wasm(
        "program.wasm", [], argv0: "program", guest_program_path: "opt/program/bin/program"
      )
    end

    assert_equal(
      'guest argv0 must be a nonempty normalized absolute path: "opt/program/bin/program"',
      error.message,
    )
  end

  def test_browser_execution_rejects_unnormalized_guest_program_path
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_browser_wasm(
        "program.wasm", [],
        argv0: "program", guest_program_path: "/opt/program/../bin/program"
      )
    end

    assert_equal(
      'guest argv0 must be a nonempty normalized absolute path: "/opt/program/../bin/program"',
      error.message,
    )
  end

  def test_pty_execution_uses_tap_owned_runner_and_removes_stale_host_dist
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      root = Pathname(dir)/"kandelo root"
      host_dist = root/"host/dist"
      host_dist.mkpath
      (host_dist/"stale.js").binwrite("stale")

      harness = Harness.new
      harness.root_path = root.to_s
      harness.test_path = Pathname(dir)/"formula test"
      harness.test_path.mkpath
      output = harness.kandelo_run_pty_wasm(
        "program.wasm", ["note.txt"],
        argv0:                      "/opt/kandelo/homebrew/opt/program/bin/program",
        env:                        { "KERNEL_CWD" => "/tmp/formula test" },
        inputs:                     ["\u001c", "beta", "\r"],
        input_ready_text:           "editor ready",
        rerun_inputs:               ["\u0018"],
        exec_programs:              { "/opt/program/bin/helper" => "/formula/helper" },
        guest_files:                { "/etc/program.conf" => "/formula/program.conf" },
        guest_directories:          ["/opt/kandelo/homebrew/var/program/save"],
        writable_guest_directories: ["/opt/kandelo/homebrew/var/program"],
        writable_host_directories:  { "/work" => "/formula/test output" },
        expected_fork_descendants:  2,
        timeout_ms:                 120_000,
        completion_output:          "ready now"
      )

      assert_equal "runtime-ok\n", output
      assert_includes harness.command, "run-pty-wasm.ts"
      refute_includes harness.command, "KANDELO_FORMULA_PTY_CONFIG_JSON="
      assert_includes harness.command, "KANDELO_FORMULA_PTY_CONFIG_PATH="
      assert_includes harness.command, "note.txt"
      assert_includes harness.command, "program.wasm"
      config = harness.pty_config
      assert_equal 0600, harness.pty_config_mode
      refute_path_exists harness.pty_config_path
      assert_equal "/opt/kandelo/homebrew/opt/program/bin/program", config.fetch("argv0")
      assert_equal ["\u001c", "beta", "\r"], config.fetch("inputs")
      assert_equal "editor ready", config.fetch("inputReadyText")
      assert_equal ["\u0018"], config.fetch("rerunInputs")
      assert_equal({ "/opt/program/bin/helper" => "/formula/helper" }, config.fetch("execPrograms"))
      assert_equal({ "/etc/program.conf" => "/formula/program.conf" }, config.fetch("guestFiles"))
      assert_equal(
        ["/opt/kandelo/homebrew/var/program"],
        config.fetch("writableGuestDirectories"),
      )
      assert_equal({ "/work" => "/formula/test output" }, config.fetch("writableHostDirectories"))
      assert_equal 2, config.fetch("expectedForkDescendants")
      assert_equal 120_000, config.fetch("timeoutMs")
      assert_equal "ready now", config.fetch("completionOutput")
      assert_equal "kandelo_run_pty_wasm", harness.recorded_launcher
      refute_path_exists host_dist
    end
  end

  def test_pty_execution_keeps_large_runtime_maps_out_of_the_process_environment
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness = Harness.new
      harness.root_path = (Pathname(dir)/"kandelo root").to_s
      harness.test_path = Pathname(dir)/"formula test"
      harness.test_path.mkpath
      guest_files = (0...4_000).to_h do |index|
        ["/usr/share/vim/runtime/file-#{index}", "/host/vim/runtime/file-#{index}"]
      end

      harness.kandelo_run_pty_wasm("program.wasm", [], inputs: [":wq\r"], guest_files:)

      config_bytes = JSON.generate(harness.pty_config).bytesize
      assert_operator config_bytes, :>, 128 * 1024
      assert_operator harness.command.bytesize, :<, 4 * 1024
      refute_includes harness.command, "/usr/share/vim/runtime/file-3999"
      assert_equal 0600, harness.pty_config_mode
      refute_path_exists harness.pty_config_path
      assert_equal guest_files, harness.pty_config.fetch("guestFiles")
    end
  end

  def test_pty_execution_removes_config_after_runner_failure
    Dir.mktmpdir("kandelo-formula-support") do |dir|
      harness_class = Class.new(Harness) do
        define_method(:shell_output) do |command, expected_status = 0|
          super(command, expected_status)
          raise "runner failed"
        end
      end
      harness = harness_class.new
      harness.root_path = (Pathname(dir)/"kandelo root").to_s
      harness.test_path = Pathname(dir)/"formula test"
      harness.test_path.mkpath

      error = assert_raises(RuntimeError) do
        harness.kandelo_run_pty_wasm("program.wasm", [], inputs: [])
      end

      assert_equal "runner failed", error.message
      refute_path_exists harness.pty_config_path
    end
  end

  def test_pty_execution_rejects_invalid_expected_fork_descendant_count
    [-1, 1.5, "1", nil].each do |count|
      error = assert_raises(RuntimeError) do
        Harness.new.kandelo_run_pty_wasm(
          "program.wasm", [], inputs: [], expected_fork_descendants: count
        )
      end

      assert_includes error.message, "expected fork descendant count must be a nonnegative integer"
    end
  end

  def test_pty_execution_rejects_invalid_input_readiness_text
    ["", "x" * 4_097, 17].each do |ready_text|
      error = assert_raises(RuntimeError) do
        Harness.new.kandelo_run_pty_wasm(
          "program.wasm", [], inputs: [], input_ready_text: ready_text
        )
      end

      assert_includes(
        error.message,
        "input readiness text must be a nonempty string no larger than 4096 bytes",
      )
    end
  end

  def test_pty_execution_rejects_invalid_timeout
    [0, -1, 1.5, "120000"].each do |timeout_ms|
      error = assert_raises(RuntimeError) do
        Harness.new.kandelo_run_pty_wasm(
          "program.wasm", [], inputs: [], timeout_ms: timeout_ms
        )
      end

      assert_includes error.message, "PTY timeout must be a positive integer number of milliseconds"
    end
  end

  def test_pty_execution_rejects_invalid_completion_output
    ["", "ready\0now", 1, "x" * 4097].each do |completion_output|
      error = assert_raises(RuntimeError) do
        Harness.new.kandelo_run_pty_wasm(
          "program.wasm", [], inputs: [], completion_output: completion_output
        )
      end

      assert_includes error.message, "PTY completion output must be a nonempty string"
    end
  end

  def test_pty_execution_rejects_nonzero_expected_status_with_completion_output
    error = assert_raises(RuntimeError) do
      Harness.new.kandelo_run_pty_wasm(
        "program.wasm", [], inputs: [], completion_output: "ready", expected_status: 1
      )
    end

    assert_includes error.message, "PTY completion output requires expected status zero"
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
