require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Ruby < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

  # WHY — TEMPORARY UPSTREAM DEVIATION: this Formula applies a
  # Kandelo-only CRuby patch that routes exactly representable command
  # launches through posix_spawn. Kandelo currently copies the complete
  # WebAssembly.Memory for fork, and Brew can create those copies faster
  # than browsers reclaim them. Other command shapes keep CRuby's fork path.
  # Remove the patch when the platform fix meets the criteria in:
  # https://github.com/Automattic/kandelo/pull/1166
  RUBY_API_VERSION = "4.0.0".freeze
  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/ruby".freeze
  GUEST_RUNTIME = "#{GUEST_OPT_PREFIX}/lib/ruby/#{RUBY_API_VERSION}".freeze

  desc "Interpreter for the Ruby scripting language on Kandelo (with psych/YAML)"
  homepage "https://www.ruby-lang.org/"
  url "https://cache.ruby-lang.org/pub/ruby/4.0/ruby-4.0.5.tar.gz"
  version "4.0.5"
  sha256 "7d6149079a63f8ae1d326c9fa65c6019ba2dc3155eae7b39159817911c88958e"
  license any_of: ["Ruby", "BSD-2-Clause"]
  revision 2

  depends_on "gpatch" => :build
  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build
  depends_on "llvm" => :build
  depends_on "make" => :build
  depends_on "perl" => :build
  depends_on "python@3.13" => :build
  # wasm-local-root-spill is a trusted Kandelo build transform implemented in
  # Rust. Keep its toolchain explicit instead of inheriting publisher state.
  depends_on "rust" => :build
  depends_on "unzip" => :build
  depends_on "kandelo-dev/tap-core/libyaml"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin"
  skip_clean "lib/ruby"

  def install
    kandelo_require_arch!("wasm32")
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "4274b55d135925220109e278320c5b1d47b5484e368bdb6eb6bd9bbbabdde041",
      script_env:      {
        "WASM_POSIX_DEP_GUEST_PREFIX" => GUEST_OPT_PREFIX,
      },
    )
    kandelo_validate_wasm_artifact(out_dir/"ruby.wasm", fork: :required)
    kandelo_install_bin(out_dir, "ruby.wasm", "ruby")

    runtime_stage = buildpath/"ruby-runtime-stage"
    system formula_opt_bin("unzip")/"unzip", "-q", out_dir/"ruby-runtime.zip", "-d", runtime_stage
    runtime_root = runtime_stage/"usr/lib/ruby"
    runtime = runtime_root/RUBY_API_VERSION
    %w[yaml.rb psych.rb json.rb rubygems.rb bundler.rb].each do |required|
      odie "Ruby runtime archive is incomplete: #{required}" unless (runtime/required).file?
    end
    odie "Ruby runtime archive has no architecture config" unless (runtime/"wasm32-none/rbconfig.rb").file?

    (lib/"ruby").install Dir["#{runtime_root}/*"]
    bin.install Dir["#{runtime_stage}/usr/bin/*"]
  end

  test do
    runtime_root = lib/"ruby"
    runtime = runtime_root/RUBY_API_VERSION
    %w[yaml.rb psych.rb json.rb rubygems.rb bundler.rb wasm32-none/rbconfig.rb].each do |required|
      assert_path_exists runtime/required
    end
    %w[gem bundle bundler].each { |command| assert_path_exists bin/command }

    runtime_files = {}
    runtime_root.glob("**/*").select(&:file?).each do |file|
      relative = file.relative_path_from(runtime_root)
      runtime_files["#{GUEST_OPT_PREFIX}/lib/ruby/#{relative}"] = file
    end
    %w[gem bundle bundler].each do |command|
      runtime_files["#{GUEST_OPT_PREFIX}/bin/#{command}"] = bin/command
    end
    assert_operator runtime_files.length, :>, 800

    program = <<~RUBY
      raise 'RUBYLIB leaked into installed runtime test' if ENV.key?('RUBYLIB')
      expected_arch = '#{GUEST_RUNTIME}/wasm32-none'
      expected_paths = ['#{GUEST_RUNTIME}', expected_arch]
      unless expected_paths.all? { |path| $LOAD_PATH.include?(path) }
        raise "missing default load paths: %p" % [$LOAD_PATH]
      end
      if $LOAD_PATH.any? { |path| path.start_with?('/usr/lib/ruby') }
        raise "stale /usr load path: %p" % [$LOAD_PATH]
      end

      require 'rbconfig'
      install_prefix = RbConfig::CONFIG['prefix']
      allowed_install_prefixes = [
        '#{GUEST_OPT_PREFIX}',
        '/home/linuxbrew/.linuxbrew/Cellar/ruby/#{pkg_version}',
      ]
      unless allowed_install_prefixes.include?(install_prefix)
        raise "unexpected install prefix: %s" % install_prefix
      end
      expected_config = {
        'RUBY_EXEC_PREFIX' => '#{GUEST_OPT_PREFIX}',
        'rubylibdir' => install_prefix + '/lib/ruby/#{RUBY_API_VERSION}',
        'rubyarchdir' => install_prefix + '/lib/ruby/#{RUBY_API_VERSION}/wasm32-none',
        'bindir' => install_prefix + '/bin',
        'ruby_version' => '#{RUBY_API_VERSION}',
      }
      expected_config.each do |key, value|
        next if RbConfig::CONFIG[key] == value

        raise "RbConfig mismatch for %s: %p" % [key, RbConfig::CONFIG[key]]
      end

      require 'pathname'
      require 'json'
      require 'yaml'
      require 'zlib'
      require 'rubygems'
      require 'bundler'
      data = { 'name' => 'kandelo', 'nums' => [1, 2, 3], 'nested' => { 'ok' => true } }
      raise 'pathname mismatch' unless Pathname('/tmp').join('ruby').to_s == '/tmp/ruby'
      raise 'json roundtrip mismatch' unless JSON.parse(JSON.generate(data)) == data
      raise 'yaml roundtrip mismatch' unless YAML.load(YAML.dump(data)) == data
      compressed = Zlib::Deflate.deflate('kandelo-ruby')
      raise 'zlib roundtrip mismatch' unless Zlib::Inflate.inflate(compressed) == 'kandelo-ruby'
      expected_gem_dir = install_prefix + '/lib/ruby/gems/#{RUBY_API_VERSION}'
      raise "gem default dir mismatch: %s" % Gem.default_dir unless Gem.default_dir == expected_gem_dir
      unless Gem.default_path.include?(expected_gem_dir)
        raise "gem default path mismatch: %p" % [Gem.default_path]
      end
      unless Gem.bindir == install_prefix + '/bin'
        raise "gem bindir mismatch: %s" % Gem.bindir
      end
      puts "ruby-runtime-ok:%s:rubygems-%s:bundler-%s" % [RUBY_VERSION, Gem::VERSION, Bundler::VERSION]
    RUBY
    output = kandelo_run_wasm(
      bin/"ruby", ["-e", program], env: { "HOME" => "/tmp" }, guest_files: runtime_files
    )
    assert_equal "ruby-runtime-ok:4.0.5:rubygems-4.0.10:bundler-4.0.10\n", output

    browser_program = program.sub("ruby-runtime-ok", "ruby-browser-runtime-ok")
    browser_output = kandelo_run_browser_wasm(
      bin/"ruby", ["-e", browser_program],
      env:          { "HOME" => "/tmp" },
      guest_files:  runtime_files,
      allow_stderr: false,
      timeout_ms:   180_000
    )
    assert_equal "ruby-browser-runtime-ok:4.0.5:rubygems-4.0.10:bundler-4.0.10\n", browser_output

    spawn_executable = "#{GUEST_OPT_PREFIX}/bin/ruby"
    spawn_program = <<~RUBY
      executable = #{spawn_executable.dump}
      child_program = <<~'CHILD'
        argv0 = File.binread('/proc/self/cmdline').split("\\0", -1).fetch(0)
        puts "argv0=" + argv0
        puts "arg1=" + ARGV.fetch(0)
        puts "env=" + ENV.fetch('K_TEST')
        puts "cwd=" + Dir.pwd
        puts "pid=" + Process.pid.to_s
        puts "pgrp=" + Process.getpgrp.to_s
        puts "input=" + STDIN.read
        warn "stderr-ok"
        exit 23
      CHILD

      input_read, input_write = IO.pipe
      output_read, output_write = IO.pipe
      error_read, error_write = IO.pipe

      # WHY: Homebrew's SystemCommand#exec3 uses this exact Process.spawn
      # shape. Keep it here as an installed-bottle compatibility test on both
      # hosts. Kandelo's package tests separately inspect fork counts to prove
      # that Ruby selects its direct posix_spawn backend for this shape.
      pid = Process.spawn(
        { "COLUMNS" => "80", "K_TEST" => "homebrew-env-ok" },
        [executable, executable],
        "--disable-gems",
        "-e",
        child_program,
        "homebrew-argument",
        in: input_read,
        out: output_write,
        err: error_write,
        pgroup: true,
        chdir: "/tmp",
      )

      input_read.close
      output_write.close
      error_write.close
      input_write.write("homebrew-input")
      input_write.close

      stdout = output_read.read
      stderr = error_read.read
      status = Process.detach(pid).value
      lines = stdout.lines.map(&:chomp)
      expected = [
        "argv0=" + executable,
        "arg1=homebrew-argument",
        "env=homebrew-env-ok",
        "cwd=/tmp",
        "input=homebrew-input",
      ]
      unless status.exitstatus == 23 && expected.all? { |line| lines.include?(line) }
        raise "unexpected Homebrew-shaped spawn result: %p %p" % [status, stdout]
      end
      pid_field = stdout[/^pid=(\d+)$/, 1]
      pgrp_field = stdout[/^pgrp=(\d+)$/, 1]
      unless pid_field && pid_field == pgrp_field
        raise "spawned process did not enter its own process group: %p" % stdout
      end
      raise "unexpected child stderr: %p" % stderr unless stderr == "stderr-ok\n"

      puts "ruby-homebrew-spawn-ok"
    RUBY
    refute_includes spawn_program, "\0"
    assert_includes spawn_program, 'split("\0", -1)'
    spawn_exec_programs = { spawn_executable => bin/"ruby" }
    spawn_output = kandelo_run_wasm(
      bin/"ruby", ["--disable-gems", "-e", spawn_program],
      env:           { "HOME" => "/tmp" },
      exec_programs: spawn_exec_programs,
      guest_files:   runtime_files
    )
    assert_equal "ruby-homebrew-spawn-ok\n", spawn_output

    browser_spawn_program = spawn_program.sub(
      "ruby-homebrew-spawn-ok", "ruby-browser-homebrew-spawn-ok"
    )
    browser_spawn_output = kandelo_run_browser_wasm(
      bin/"ruby", ["--disable-gems", "-e", browser_spawn_program],
      env:           { "HOME" => "/tmp" },
      exec_programs: spawn_exec_programs,
      guest_files:   runtime_files,
      allow_stderr:  false,
      timeout_ms:    180_000
    )
    assert_equal "ruby-browser-homebrew-spawn-ok\n", browser_spawn_output

    command_versions = {
      "gem"     => "4.0.10\n",
      "bundle"  => "4.0.10\n",
      "bundler" => "4.0.10\n",
    }
    command_versions.each do |command, expected|
      output = kandelo_run_wasm(
        bin/"ruby", ["#{GUEST_OPT_PREFIX}/bin/#{command}", "--version"],
        env: { "HOME" => "/tmp" }, guest_files: runtime_files
      )
      assert_equal expected, output, command
    end
  end
end
