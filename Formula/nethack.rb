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
    runtime_files = {}
    (share/"nethack").glob("**/*").select(&:file?).each do |path|
      relative = path.relative_path_from(share/"nethack")
      runtime_files["#{GUEST_HACKDIR}/#{relative}"] = path
    end
    assert_path_exists share/"nethack/nhdat"

    record = testpath/"record"
    record.write("")
    runtime_files["/home/.nethack/record"] = record

    paths = kandelo_run_wasm(
      bin/"nethack", ["-showpaths"],
      env:         { "HOME" => "/home/player" },
      guest_files: runtime_files
    )
    assert_includes paths, GUEST_HACKDIR
    refute_includes paths, "/usr/share/nethack"

    scores = kandelo_run_wasm(
      bin/"nethack", ["-s", "all"],
      env:         { "HOME" => "/home/player" },
      guest_files: runtime_files, merge_stderr: true
    )
    refute_match(/Cannot (?:chdir|open record file)/i, scores)
  end
end
