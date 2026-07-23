require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Bc < Formula
  include KandeloFormulaSupport

  KANDELO_REGISTRY_BRIDGE = true

  desc "Arbitrary-precision numeric processing language for Kandelo"
  homepage "https://www.gnu.org/software/bc/"
  url "https://ftpmirror.gnu.org/gnu/bc/bc-1.07.1.tar.gz"
  version "1.07.1"
  sha256 "62adfca89b0a1c0164c2cdca59ca210c1d44c3ffc46daf9931cf4942664cb02a"
  license "GPL-3.0-or-later"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on "bison" => :build
  depends_on "flex" => :build
  depends_on "m4" => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin/bc"

  def install
    kandelo_require_arch!("wasm32")
    out_dir = kandelo_build_package(script_env: {})
    kandelo_validate_wasm_artifact(out_dir/"bc.wasm", fork: :forbidden)
    kandelo_install_bin(out_dir, "bc.wasm", "bc")
  end

  test do
    assert_equal "3.50\n", kandelo_run_wasm(bin/"bc", [], stdin: "scale=2; 7/2\n")
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 2
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "1eb1d3486fc3befaedd8b8d4ccd08589d11d9e1f9492f4c7fc8b8d0999512825"
  end

end
