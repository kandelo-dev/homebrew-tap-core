require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Bc < Formula
  include KandeloFormulaSupport

  desc "Arbitrary-precision numeric processing language for Kandelo"
  homepage "https://www.gnu.org/software/bc/"
  url "https://ftpmirror.gnu.org/gnu/bc/bc-1.07.1.tar.gz"
  sha256 "62adfca89b0a1c0164c2cdca59ca210c1d44c3ffc46daf9931cf4942664cb02a"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "bison" => :build
  depends_on "flex" => :build
  depends_on "m4" => :build
  depends_on "wabt" => :build

  skip_clean "bin/bc"

  def install
    kandelo_require_arch!("wasm32")
    source_dir = kandelo_stage_verified_formula_source

    out_dir = kandelo_build_package("bc", "build-bc.sh", stable.url, stable.checksum.hexdigest,
      script_env: {
        "WASM_POSIX_DEP_SOURCE_DIR"       => source_dir,
        "WASM_POSIX_INSTALL_LOCAL_MIRROR" => "0",
      })
    kandelo_validate_wasm_artifact(out_dir/"bc.wasm", fork: :forbidden)
    kandelo_install_bin(out_dir, "bc.wasm", "bc")
  end

  test do
    assert_equal "3.50\n", kandelo_run_wasm(bin/"bc", [], stdin: "scale=2; 7/2\n")
  end
end
