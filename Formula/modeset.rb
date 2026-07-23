require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Modeset < Formula
  include KandeloFormulaSupport

  KANDELO_REGISTRY_BRIDGE = true

  desc "DRM/KMS fluid simulation for Kandelo"
  homepage "https://github.com/Automattic/kandelo"
  url "https://github.com/Automattic/kandelo/archive/1a83af5de608c10f485082c6ef0efa845f747436.tar.gz"
  version "0.1.0"
  sha256 "07e7a7ebff8003114f6b4bef1ccdc2e9b15ecfbd5e6ccc3bf8563107b8151fde"
  license "GPL-2.0-or-later"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin/modeset"

  def install
    kandelo_require_arch!("wasm32")

    # Transitional Tier-2 bridge: the registry recipe binds the program to
    # Kandelo's ABI-coupled libdrm/GBM/EGL/GLES sysroot stubs.
    out_dir = kandelo_build_package(script_env: {})
    kandelo_validate_wasm_artifact(out_dir/"modeset.wasm")
    kandelo_install_bin(out_dir, "modeset.wasm", "modeset")
  end

  test do
    kandelo_run_kms_wasm(bin/"modeset", min_page_flips: 2)
    kandelo_run_kms_browser_wasm(bin/"modeset", min_page_flips: 2)
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 2
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "30171effab671b29d6dab42b437bcff6a15e1dee2636e29ddcb114e5aa28f8c4"
  end

end
