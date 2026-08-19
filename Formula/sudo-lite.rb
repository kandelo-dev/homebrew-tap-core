require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class SudoLite < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

  desc "Minimal authenticated privilege transition for Kandelo"
  homepage "https://github.com/Automattic/kandelo"
  url "https://github.com/Automattic/kandelo/archive/5669d27fa171ad1bccf50031914dc6d997666276.tar.gz"
  version "0.1.0"
  sha256 "af0984c5312b6396e86e62910342a0e23cd4c8822353b3d58787d8f071a7b6f4"
  license "GPL-2.0-or-later"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin/sudo-lite"

  def install
    kandelo_require_arch!("wasm32")
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "99d2b52e1c0d43164015f8d1f07c3099617d305824b137c592908c8cfcc40fbf",
      script_env:      {},
    )
    kandelo_validate_wasm_artifact(out_dir/"sudo-lite.wasm", fork: :forbidden)
    kandelo_install_bin(out_dir, "sudo-lite.wasm", "sudo-lite")
  end

  test do
    output = kandelo_run_wasm(
      bin/"sudo-lite", ["--definitely-invalid"], merge_stderr: true,
      expected_status: 2
    )
    assert_match(/sudo-lite: unsupported option/, output)
    assert_match(/usage: .*sudo-lite/, output)
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core-abi-43/sudo-lite"
    sha256 cellar: "/opt/kandelo/homebrew/Cellar", wasm32_kandelo: "d9ea481112c25ea278b36888ed5e814358ffa663be203a90cb4081ada40fc828"
  end
end
