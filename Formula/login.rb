require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Login < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

  desc "POSIX login program for Kandelo"
  homepage "https://github.com/Automattic/kandelo"
  url "https://github.com/Automattic/kandelo/archive/5669d27fa171ad1bccf50031914dc6d997666276.tar.gz"
  version "0.1.0"
  sha256 "af0984c5312b6396e86e62910342a0e23cd4c8822353b3d58787d8f071a7b6f4"
  license "GPL-2.0-or-later"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin/login"

  def install
    kandelo_require_arch!("wasm32")
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "f4f6b62c864e32440c5286a1c62856f7f2eef4dec8809bcde021a50398c98ee6",
      script_env:      {},
    )
    kandelo_validate_wasm_artifact(out_dir/"login.wasm", fork: :forbidden)
    kandelo_install_bin(out_dir, "login.wasm", "login")
  end

  test do
    output = kandelo_run_wasm(
      bin/"login", ["--definitely-invalid"], merge_stderr: true,
      expected_status: 2
    )
    assert_match(/usage: .*login/, output)
  end
end
