require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Modeset < Formula
  include KandeloFormulaSupport

  desc "DRM/KMS fluid simulation for Kandelo"
  homepage "https://github.com/Automattic/kandelo"
  url "https://github.com/Automattic/kandelo/archive/110619f8e7e5d51ec85b62f526176535462cd3bd.tar.gz"
  version "0.1.0"
  sha256 "c082b12eafdcb81e7162b79d22dc329bf1ba5184ce09b0d446c7419d51eb0009"
  license "GPL-2.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/modeset"

  def install
    kandelo_require_arch!("wasm32")
    source_dir = kandelo_stage_verified_formula_source

    # Transitional Tier-2 bridge: the registry recipe binds the program to
    # Kandelo's ABI-coupled libdrm/GBM/EGL/GLES sysroot stubs.
    out_dir = kandelo_build_package(
      "modeset", "build-modeset.sh", stable.url, stable.checksum.hexdigest,
      script_env: {
        "WASM_POSIX_DEP_SOURCE_DIR"       => source_dir,
        "WASM_POSIX_INSTALL_LOCAL_MIRROR" => "0",
      }
    )
    kandelo_validate_wasm_artifact(out_dir/"modeset.wasm")
    kandelo_install_bin(out_dir, "modeset.wasm", "modeset")
  end

  test do
    kandelo_run_kms_wasm(bin/"modeset", min_page_flips: 2)
    kandelo_run_kms_browser_wasm(bin/"modeset", min_page_flips: 2)
  end
end
