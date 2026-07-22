require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Netcat < Formula
  include KandeloFormulaSupport

  KANDELO_REGISTRY_BRIDGE = true

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

    # Transitional Tier-2 bridge: the registry recipe owns the reviewed
    # network compatibility patch set and its exact configure assertions.
    out_dir = kandelo_build_package(script_env: {})
    kandelo_validate_wasm_artifact(out_dir/"nc.wasm", fork: :forbidden)
    kandelo_install_bin(out_dir, "nc.wasm", "nc")
  end

  test do
    output = kandelo_run_wasm(bin/"nc", ["--version"], merge_stderr: true)
    assert_match(/netcat \(The GNU Netcat\) 0\.7\.1/i, output)
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 1
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "e9bd9050f4c0ecc924aaf03521e963e71294b9e711822c62f767c5147944cee1"
  end

end
