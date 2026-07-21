require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Nethack < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/nethack".freeze
  GUEST_HACKDIR = "#{GUEST_OPT_PREFIX}/share/nethack".freeze

  desc "Classic dungeon exploration game for Kandelo"
  homepage "https://www.nethack.org/"
  url "https://www.nethack.org/download/3.6.7/nethack-367-src.tgz"
  version "3.6.7"
  sha256 "98cf67df6debf9668a61745aa84c09bcab362e5d33f5b944ec5155d44d2aacb2"
  license :cannot_represent

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: "/home/linuxbrew/.linuxbrew/Cellar", wasm32_kandelo: "a6c10be29d4053f31e73108d1fe661f8cd2140f89c58d8da464ce199a9ec3771"
  end

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "kandelo-dev/tap-core/ncurses"

  skip_clean "bin/nethack"

  def install
    kandelo_require_arch!("wasm32")
    ENV.deparallelize
    source_dir = kandelo_stage_verified_formula_source

    # Transitional Tier-2 bridge: NetHack's registry recipe still owns the
    # host code-generator phase and the target/data serialization patches.
    # Its compiled data path is the stable guest opt prefix, not a staging keg
    # path and not the registry image's historical /usr/share location.

    out_dir = kandelo_build_package(
      "nethack", "build-nethack.sh", stable.url, stable.checksum.hexdigest,
      script_env: {
        "WASM_POSIX_DEP_NCURSES_DIR"      => formula_opt_prefix("kandelo-dev/tap-core/ncurses"),
        "WASM_POSIX_DEP_SOURCE_DIR"       => source_dir,
        "WASM_POSIX_INSTALL_LOCAL_MIRROR" => "0",
        "NETHACK_HACKDIR"                 => GUEST_HACKDIR,
      }
    )
    kandelo_validate_wasm_artifact(out_dir/"nethack.wasm", fork: :required)
    kandelo_install_bin(out_dir, "nethack.wasm", "nethack")
    (share/"nethack").install Dir["#{out_dir}/runtime/share/nethack/*"]
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
