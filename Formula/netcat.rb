require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Netcat < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

  desc "GNU network utility for Kandelo"
  homepage "https://netcat.sourceforge.net/"
  url "https://downloads.sourceforge.net/project/netcat/netcat/0.7.1/netcat-0.7.1.tar.gz"
  version "0.7.1"
  sha256 "30719c9a4ffbcf15676b8f528233ccc54ee6cba96cb4590975f5fd60c68a066f"
  license "GPL-2.0-or-later"

  depends_on "automake" => :build
  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on "gpatch" => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin/nc"

  def install
    kandelo_require_arch!("wasm32")

    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "23b1b99cd162ce9237176002041db74b7cf3bdd9257190c1c39caaa6ece90dd2",
      script_env:      {},
    )
    kandelo_validate_wasm_artifact(out_dir/"nc.wasm", fork: :forbidden)
    kandelo_install_bin(out_dir, "nc.wasm", "nc")
  end

  test do
    output = kandelo_run_wasm(bin/"nc", ["--version"], merge_stderr: true)
    assert_match(/netcat \(The GNU Netcat\) 0\.7\.1/i, output)
  end


  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core-abi-43/netcat"
    rebuild 3
    sha256 cellar: "/opt/kandelo/homebrew/Cellar", wasm32_kandelo: "e9ef81e35f4c5e691b1cf49eee97cdd768e589c31dfa02e0fb1819b492a996ba"
  end
end
