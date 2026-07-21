# typed: strict
# frozen_string_literal: true

require "minitest/autorun"
require "open3"
# Standalone Ruby does not preload Homebrew's Pathname helper.
require "pathname" # rubocop:disable Lint/RedundantRequireStatement
require "rbconfig"
require "tmpdir"
require_relative "../kandelo_formula_support"

# Regression coverage for Formula runtime execution evidence.
class KandeloFormulaSupportTest < Minitest::Test
  DependencyFormula = Struct.new(:full_name, :opt_bin, :opt_sbin, :opt_libexec, keyword_init: true)
  InstalledFormula = Struct.new(:rack, :pkg_version, keyword_init: true)
  StableSpec = Struct.new(:url, :checksum, keyword_init: true)
  StableChecksum = Struct.new(:hexdigest, keyword_init: true)

  # Minimal Formula double for command-construction tests.
  class Harness
    include KandeloFormulaSupport

    attr_accessor :build_path, :dependency_formulae, :formula_full_name, :formula_name, :formula_path,
                  :formula_version, :homebrew_prefix_path, :nix_path, :prefix_path, :root_path,
                  :runtime_formulae, :shell_result, :stable_spec, :test_path, :tier2_runtime
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

    def kandelo_tier2_runtime!
      return tier2_runtime unless tier2_runtime.nil?

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

  def with_tier2_loader_fixture
    Dir.mktmpdir("kandelo-tier2-loader") do |dir|
      base = Pathname(dir).realpath
      tap_root = base/"kandelo-dev/homebrew-tap-core"
      support_path = tap_root/"Kandelo/formula_support/kandelo_formula_support.rb"
      formula_path = tap_root/"Formula/hello.rb"
      prefix = base/"prefix"
      root = base/"kandelo-root"
      sysroot = root/"sysroot"
      [support_path.dirname, formula_path.dirname, prefix, sysroot].each(&:mkpath)
      FileUtils.cp(File.expand_path("../kandelo_formula_support.rb", __dir__), support_path)
      formula_path.binwrite("class Hello < Formula\nend\n")
      yield({
        base:,
        formula_path:,
        prefix:,
        root:,
        support_path:,
        sysroot:,
      })
    end
  end

  def tier2_loader_attestation(fixture, bridge: true)
    nested = if bridge
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
    {
      "schema"            => 1,
      "arch"              => "wasm32",
      "tap"               => "kandelo-dev/tap-core",
      "formula"           => "hello",
      "full_name"         => "kandelo-dev/tap-core/hello",
      "formula_sha256"    => Digest::SHA256.file(fixture.fetch(:formula_path)).hexdigest,
      "support_sha256"    => Digest::SHA256.file(fixture.fetch(:support_path)).hexdigest,
      "tier2_bridge"      => nested,
    }
  end

  def write_tier2_loader_attestation(fixture, contents)
    path = fixture.fetch(:prefix)/KandeloFormulaSupport::KANDELO_TIER2_ATTESTATION_BASENAME
    path.binwrite(contents)
    path.chmod(0444)
    path
  end

  def run_tier2_support_load(fixture, after_require, environment: {}, homebrew_filtered: false)
    env = {
      "HOMEBREW_PREFIX"                 => fixture.fetch(:prefix).to_s,
      "HOMEBREW_KANDELO_ARCH"           => "wasm32",
      "HOMEBREW_KANDELO_ROOT"           => fixture.fetch(:root).to_s,
      "HOMEBREW_KANDELO_SYSROOT"        => fixture.fetch(:sysroot).to_s,
      "KANDELO_HOMEBREW_ARCH"           => "wasm32",
      "KANDELO_HOMEBREW_KANDELO_ROOT"   => fixture.fetch(:root).to_s,
      "WASM_POSIX_SYSROOT"              => fixture.fetch(:sysroot).to_s,
    }.merge(environment)
    source = <<~RUBY
      require #{fixture.fetch(:support_path).to_s.inspect}
      #{after_require}
    RUBY
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

  def with_tier2_build_fixture(script_env: nil)
    original = ENV.to_hash
    Dir.mktmpdir("kandelo-tier2-build") do |dir|
      base = Pathname(dir).realpath
      root = base/"kandelo-root"
      registry_root = root/"packages/registry"
      package_root = registry_root/"hello"
      sysroot = root/"sysroot"
      build_path = base/"formula-build"
      formula_path = base/"hello.rb"
      support_path = base/"kandelo_formula_support.rb"
      [package_root, sysroot, build_path].each(&:mkpath)
      package_toml = package_root/"package.toml"
      build_toml = package_root/"build.toml"
      script = package_root/"build-hello.sh"
      package_toml.binwrite("name = \"hello\"\nversion = \"1.0\"\n")
      build_toml.binwrite("script_path = \"packages/registry/hello/build-hello.sh\"\n")
      script.binwrite("#!/usr/bin/env bash\nset -euo pipefail\n")
      formula_path.binwrite("class Hello < Formula\nend\n")
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
        "package"             => "hello",
        "package_toml_sha256" => Digest::SHA256.file(package_toml).hexdigest,
        "script"              => "build-hello.sh",
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
        "formula"         => "hello",
        "full_name"       => "kandelo-dev/tap-core/hello",
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
        "attestation"     => attestation,
        "attestation_path" => (base/"attestation.json").to_s,
        "formula_path"    => formula_path.to_s,
        "support_path"    => support_path.to_s,
        "support_sha256"  => attestation.fetch("support_sha256"),
        "trusted_env"     => trusted_env,
      }
      activation_calls = []
      harness = Harness.new
      harness.build_path = build_path
      harness.formula_name = "hello"
      harness.formula_full_name = "kandelo-dev/tap-core/hello"
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

  def test_verified_formula_source_rejects_an_empty_buildpath
    Dir.mktmpdir("kandelo-empty-formula-source") do |dir|
      harness = Harness.new
      harness.build_path = Pathname(dir)

      error = assert_raises(RuntimeError) { harness.kandelo_stage_verified_formula_source }
      assert_includes error.message, "did not stage Formula source"
    end
  end

  def test_support_load_succeeds_without_a_publisher_attestation
    with_tier2_loader_fixture do |fixture|
      marker = fixture.fetch(:base)/"formula-evaluated"
      _stdout, stderr, status = run_tier2_support_load(
        fixture, "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")"
      )

      assert status.success?, stderr
      assert_equal "evaluated\n", marker.binread
    end
  end

  def test_support_load_validates_and_recursively_freezes_an_active_attestation
    with_tier2_loader_fixture do |fixture|
      document = tier2_loader_attestation(fixture)
      write_tier2_loader_attestation(fixture, JSON.generate(document))
      assertion = <<~'RUBY'
        runtime = KandeloFormulaSupport::KANDELO_TIER2_RUNTIME
        values = [runtime, runtime["attestation"], runtime["attestation"]["tier2_bridge"],
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
        "missing authoritative root" => { "HOMEBREW_KANDELO_ROOT" => nil },
        "missing authoritative arch" => { "HOMEBREW_KANDELO_ARCH" => nil },
        "conflicting root alias" => {
          "KANDELO_HOMEBREW_KANDELO_ROOT" => fixture.fetch(:base).to_s,
        },
        "conflicting arch alias" => { "KANDELO_HOMEBREW_ARCH" => "wasm64" },
      }
      mutations.each do |label, environment|
        marker = fixture.fetch(:base)/"#{label.tr(" ", "-")}-evaluated"
        _stdout, stderr, status = run_tier2_support_load(
          fixture,
          "File.binwrite(#{marker.to_s.inspect}, \"evaluated\\n\")",
          environment:,
        )

        refute status.success?, label
        assert_includes stderr, "publisher root or architecture environment is inconsistent", label
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
      mutations = {
        "duplicate key"       => valid_json.sub('"schema":1', '"schema":1,"schema":1'),
        "missing top key"     => JSON.generate(missing_top),
        "unknown top key"     => JSON.generate(valid.merge("unknown" => true)),
        "missing bridge key"  => JSON.generate(missing_bridge),
        "unknown bridge key"  => JSON.generate(unknown_bridge),
        "bridge value type"   => JSON.generate(invalid_bridge_type),
        "schema"              => JSON.generate(valid.merge("schema" => 2)),
        "formula hash"        => JSON.generate(valid.merge("formula_sha256" => "f" * 64)),
        "support hash"        => JSON.generate(valid.merge("support_sha256" => "f" * 64)),
        "trailing JSON value" => "#{valid_json} true",
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
      ENV["HELLO_RESOURCE"] = "/ambient/resource"
      ENV["WASM_POSIX_BINARY_INDEX_URL"] = "https://ambient.invalid/index.toml"
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
      refute environment.key?("WASM_POSIX_BINARY_INDEX_URL")
      refute environment.key?("WASM_POSIX_DEFAULT_ARCH")
      refute environment.key?("HELLO_AMBIENT")
      assert_path_exists fixture.fetch(:resource_dir)/"resource/input.txt"
      assert_path_exists fixture.fetch(:build_path)/"kandelo-package-source/upstream.c"
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

  def test_ruby_declares_every_registry_script_native_build_dependency
    formula = File.read(File.expand_path("../../../Formula/ruby.rb", __dir__))
    native_declarations = formula.lines.grep(/^\s*depends_on "(?:rust|wabt)"/)

    assert_equal [
      %Q(  depends_on "rust" => :build\n),
      %Q(  depends_on "wabt" => :build\n),
    ], native_declarations
  end

  def test_nethack_declares_its_canonical_dotted_version
    formula = File.read(File.expand_path("../../../Formula/nethack.rb", __dir__))
    version_declarations = formula.lines.grep(/^\s*version /)

    assert_equal [%Q(  version "3.6.7"\n)], version_declarations
  end

  def test_changed_tier2_formulae_advance_their_finalized_bottle_identity
    %w[bc fbdoom lsof modeset netcat posix-utils-lite].each do |name|
      formula = File.read(File.expand_path("../../../Formula/#{name}.rb", __dir__))
      rebuild_declarations = formula.lines.grep(/^\s*rebuild /)

      assert_equal [%Q(    rebuild 1\n)], rebuild_declarations, name
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
      guest_executable = Pathname(dir)/"helper.wasm"
      guest_executable.binwrite("\0asm")
      output = harness.kandelo_run_browser_wasm(
        command, ["-e", "console.log(42)"],
        argv0: "node", env: { "HOME" => "/root" },
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
        argv0:                      "/home/linuxbrew/.linuxbrew/opt/program/bin/program",
        env:                        { "KERNEL_CWD" => "/tmp/formula test" },
        inputs:                     ["\u001c", "beta", "\r"],
        input_ready_text:           "editor ready",
        rerun_inputs:               ["\u0018"],
        exec_programs:              { "/opt/program/bin/helper" => "/formula/helper" },
        guest_files:                { "/etc/program.conf" => "/formula/program.conf" },
        guest_directories:          ["/home/linuxbrew/.linuxbrew/var/program/save"],
        writable_guest_directories: ["/home/linuxbrew/.linuxbrew/var/program"],
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
      assert_equal "/home/linuxbrew/.linuxbrew/opt/program/bin/program", config.fetch("argv0")
      assert_equal ["\u001c", "beta", "\r"], config.fetch("inputs")
      assert_equal "editor ready", config.fetch("inputReadyText")
      assert_equal ["\u0018"], config.fetch("rerunInputs")
      assert_equal({ "/opt/program/bin/helper" => "/formula/helper" }, config.fetch("execPrograms"))
      assert_equal({ "/etc/program.conf" => "/formula/program.conf" }, config.fetch("guestFiles"))
      assert_equal(
        ["/home/linuxbrew/.linuxbrew/var/program"],
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
