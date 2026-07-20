require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Lsof < Formula
  include KandeloFormulaSupport

  desc "Open-file reporter for Kandelo procfs"
  homepage "https://github.com/Automattic/kandelo"
  url "https://github.com/Automattic/kandelo/archive/110619f8e7e5d51ec85b62f526176535462cd3bd.tar.gz"
  version "0.1.0"
  sha256 "c082b12eafdcb81e7162b79d22dc329bf1ba5184ce09b0d446c7419d51eb0009"
  license "GPL-2.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/lsof"

  def install
    kandelo_require_arch!("wasm32")
    source_dir = kandelo_stage_verified_formula_source

    # Transitional Tier-2 bridge: this intentionally packages Kandelo's
    # procfs-aware implementation, not native lsof with Linux-only probes.
    out_dir = kandelo_build_package(
      "lsof", "build-lsof.sh", stable.url, stable.checksum.hexdigest,
      script_env: {
        "WASM_POSIX_DEP_SOURCE_DIR"       => source_dir,
        "WASM_POSIX_INSTALL_LOCAL_MIRROR" => "0",
      }
    )
    kandelo_validate_wasm_artifact(out_dir/"lsof.wasm")
    kandelo_install_bin(out_dir, "lsof.wasm", "lsof")
  end

  test do
    assert_equal "Usage: lsof [-p pid] [-c command] [file]\n",
      kandelo_run_wasm(bin/"lsof", ["--help"])
  end
end
