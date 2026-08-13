require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Clang < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true
  CLANG_RECIPE_MANIFEST_SHA256 = "79214a37742d6eba4b8ee80ab6d1b46cebe41ede3712393893d35fc678fe1b38".freeze

  desc "LLVM C and C++ compiler toolchain for Kandelo"
  homepage "https://llvm.org/"
  url "https://github.com/llvm/llvm-project/releases/download/llvmorg-21.1.7/llvm-project-21.1.7.src.tar.xz"
  version "21.1.7"
  sha256 "e5b65fd79c95c343bb584127114cb2d252306c1ada1e057899b6aacdd445899e"
  license "Apache-2.0" => { with: "LLVM-exception" }

  depends_on "cmake" => :build
  depends_on "gpatch" => :build
  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  depends_on "llvm@21" => :build
  depends_on "ninja" => :build
  depends_on "python@3.13" => :build
  depends_on "kandelo-dev/tap-core/libcxx"

  skip_clean "libexec/llvm/bin"

  def install
    kandelo_require_arch!("wasm32")
    out = kandelo_build_tap_recipe(
      manifest_sha256: CLANG_RECIPE_MANIFEST_SHA256,
      script_env:      {
        "WASM_POSIX_DEP_CMAKE"      => formula_opt_bin("cmake")/"cmake",
        "WASM_POSIX_DEP_LLVM21_DIR" => formula_opt_prefix("llvm@21"),
        "WASM_POSIX_DEP_NINJA"      => formula_opt_bin("ninja")/"ninja",
        "WASM_POSIX_DEP_PATCH"      => formula_opt_bin("gpatch")/"patch",
        "WASM_POSIX_DEP_PYTHON"     => formula_opt_bin("python@3.13")/"python3.13",
      },
    )
    llvm = libexec/"llvm"
    llvm.install out/"bin", out/"lib"
    %w[clang clang++ wasm-ld llvm-ar llvm-ranlib llvm-nm].each do |name|
      kandelo_validate_wasm_artifact llvm/"bin"/name, fork: :forbidden
      bin.install_symlink llvm/"bin"/name
    end
    (share/"licenses/clang").install out/"LICENSE.TXT"
  end

  test do
    assert_match(/clang version 21\.1\.7/,
                 kandelo_run_wasm(bin/"clang", ["--version"]))
    assert_match(/LLD 21\.1\.7/,
                 kandelo_run_wasm(bin/"wasm-ld", ["--version"]))
    %w[llvm-ar llvm-ranlib llvm-nm].each do |name|
      assert_match(/LLVM version 21\.1\.7/,
                   kandelo_run_wasm(bin/name, ["--version"]))
    end
    assert_path_exists share/"licenses/clang/LICENSE.TXT"
    refute_path_exists libexec/"llvm/bin/llvm-tblgen"
    refute_path_exists libexec/"llvm/bin/clang-tblgen"
  end
end
