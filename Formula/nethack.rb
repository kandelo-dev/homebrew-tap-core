require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Nethack < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

  GUEST_HOMEBREW_PREFIX =
    KandeloFormulaSupport::KANDELO_GUEST_HOMEBREW_PREFIX
  GUEST_OPT_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/nethack".freeze
  GUEST_HACKDIR = "#{GUEST_OPT_PREFIX}/libexec".freeze
  GUEST_VAR_PLAYGROUND = "#{GUEST_HOMEBREW_PREFIX}/share/nethack".freeze

  desc "Classic dungeon exploration game for Kandelo"
  homepage "https://www.nethack.org/"
  url "https://www.nethack.org/download/3.6.7/nethack-367-src.tgz"
  version "3.6.7"
  sha256 "98cf67df6debf9668a61745aa84c09bcab362e5d33f5b944ec5155d44d2aacb2"
  license :cannot_represent
  revision 1

  depends_on "bison" => :build
  depends_on "flex" => :build
  depends_on "gpatch" => :build
  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build
  depends_on "llvm" => :build
  depends_on "make" => :build
  depends_on "kandelo-dev/tap-core/ncurses"

  skip_clean "bin/nethack"

  def install
    kandelo_require_arch!("wasm32")
    ENV.deparallelize
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "0424b58585f85cfd9e1e55cde1427515cf883c84a8f449dff881b641b7ae3d94",
      script_env:      {
        "WASM_POSIX_DEP_HOST_CC"              => formula_opt_bin("llvm")/"clang",
        "WASM_POSIX_DEP_MAKE"                 => formula_opt_bin("make")/"make",
        "WASM_POSIX_DEP_PATCH"                => formula_opt_bin("gpatch")/"patch",
        "WASM_POSIX_DEP_BISON"                => formula_opt_bin("bison")/"bison",
        "WASM_POSIX_DEP_FLEX"                 => formula_opt_bin("flex")/"flex",
        "WASM_POSIX_DEP_GUEST_HACKDIR"        => GUEST_HACKDIR,
        "WASM_POSIX_DEP_GUEST_VAR_PLAYGROUND" => GUEST_VAR_PLAYGROUND,
      },
    )
    kandelo_validate_wasm_artifact(out_dir/"nethack.wasm", fork: :required)
    kandelo_install_bin(out_dir, "nethack.wasm", "nethack")
    libexec.install out_dir/"runtime/nhdat", out_dir/"runtime/symbols",
                    out_dir/"runtime/license"
  end

  test do
    playground = testpath/"nethack-runtime"
    save = playground/"save"
    [playground, save].each(&:mkpath)
    chmod 0755, playground
    chmod 0755, save
    %w[perm record logfile xlogfile].each do |name|
      path = playground/name
      path.write ""
      chmod 0600, path
    end

    assert_predicate playground, :directory?
    assert_predicate save, :directory?
    assert_equal 0755, playground.stat.mode & 0777
    assert_equal 0755, save.stat.mode & 0777
    %w[perm record logfile xlogfile].each do |name|
      path = playground/name
      assert_predicate path, :file?
      assert_equal 0600, path.stat.mode & 0777
    end

    runtime_files = {}
    libexec.glob("*").select(&:file?).each do |path|
      runtime_files["#{GUEST_HACKDIR}/#{path.basename}"] = path
    end
    assert_path_exists libexec/"nhdat"

    %w[perm record logfile xlogfile].each do |name|
      runtime_files["#{GUEST_VAR_PLAYGROUND}/#{name}"] = playground/name
    end

    ["/home/player-one", "/home/player-two"].each do |home|
      paths = kandelo_run_wasm(
        bin/"nethack", ["-showpaths"],
        env:         { "HOME" => home },
        guest_files: runtime_files
      )
      assert_includes paths, GUEST_HACKDIR
      assert_includes paths, GUEST_VAR_PLAYGROUND
      assert_includes paths, "#{home}/.nethackrc"
      refute_includes paths, File.join("/home", ".nethack")
      refute_includes paths, Dir.home
    end

    output = kandelo_run_pty_wasm(
      bin/"nethack", ["-u", "Kandelo-Arc-Hum-Mal-Law"],
      argv0:                      "#{GUEST_OPT_PREFIX}/bin/nethack",
      env:                        {
        "HOME" => "/home/player",
        "TERM" => "xterm",
        "NETHACKOPTIONS" => "windowtype:tty",
      },
      inputs:                     [" ", "S", "y"],
      rerun_inputs:               [" ", "S", "y"],
      guest_files:                runtime_files,
      guest_directories:          ["#{GUEST_VAR_PLAYGROUND}/save"],
      writable_guest_directories: [GUEST_VAR_PLAYGROUND],
      initial_delay_ms:           1200,
      input_delay_ms:             350,
      timeout_ms:                 120_000
    )
    assert_includes output, "Saving..."
    assert_includes output, "Restoring save file"
    refute_match(/Cannot open save file/i, output)
  end
end
