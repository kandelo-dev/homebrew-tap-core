require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Sudo < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

  desc "Execute commands as another user on Kandelo"
  homepage "https://www.sudo.ws/"
  url "https://github.com/sudo-project/sudo/archive/refs/tags/v1.9.17p2.tar.gz"
  version "1.9.17p2"
  sha256 "cabee23359afa698d147478c3a141437dbfecb510382e114eaf4b5087a1f8ca5"
  license "ISC"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on "gpatch" => :build
  depends_on "make" => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin"

  def install
    kandelo_require_arch!("wasm32")
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "e65a7aee1e637c04b83c94c68042721c792debdc55cf45ab3c2acaa49b8ea90f",
      script_env:      {
        "WASM_POSIX_DEP_MAKE"  => formula_opt_bin("make")/"make",
        "WASM_POSIX_DEP_PATCH" => formula_opt_bin("gpatch")/"patch",
      },
    )
    %w[cvtsudoers sudo sudoreplay visudo].each do |program|
      kandelo_validate_wasm_artifact(out_dir/"#{program}.wasm")
      kandelo_install_bin(out_dir, "#{program}.wasm", program)
    end
  end

  test do
    output = kandelo_run_wasm(bin/"sudoreplay", ["-V"], merge_stderr: true)
    assert_match(/1\.9\.17p2/, output)
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core-abi-43/sudo"
    sha256 cellar: "/opt/kandelo/homebrew/Cellar", wasm32_kandelo: "adb1211379b0f885a560a537a79e05a51a5c91491913e6a401a4f74e7ca02693"
  end
end
