require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Lsof < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

  desc "Open-file reporter for Kandelo procfs"
  homepage "https://github.com/Automattic/kandelo"
  url "https://github.com/Automattic/kandelo/archive/1a83af5de608c10f485082c6ef0efa845f747436.tar.gz"
  version "0.1.0"
  sha256 "07e7a7ebff8003114f6b4bef1ccdc2e9b15ecfbd5e6ccc3bf8563107b8151fde"
  license "GPL-2.0-or-later"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin/lsof"

  def install
    kandelo_require_arch!("wasm32")

    # This intentionally packages the tap-owned procfs-aware implementation,
    # not native lsof with Linux-only probes.
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "73061bc1d6d1be766c83462215b4b9f6f7aae60fac38542ee2f217e077ebd842",
      script_env:      {},
    )
    kandelo_validate_wasm_artifact(out_dir/"lsof.wasm")
    kandelo_install_bin(out_dir, "lsof.wasm", "lsof")
  end

  test do
    assert_equal "Usage: lsof [-p pid] [-c command] [file]\n",
      kandelo_run_wasm(bin/"lsof", ["--help"])
  end


  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core-abi-43/lsof"
    rebuild 3
    sha256 cellar: "/opt/kandelo/homebrew/Cellar", wasm32_kandelo: "be4680c74cd8e934b442f85f614bba539b1fcf46904cdef54850728fc974c3e1"
  end
end
