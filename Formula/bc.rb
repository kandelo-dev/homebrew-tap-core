require_relative "../Kandelo/formula_support/kandelo_formula_support"

class Bc < Formula
  include KandeloFormulaSupport

  desc "Arbitrary-precision numeric processing language for Kandelo"
  homepage "https://www.gnu.org/software/bc/"
  url "https://ftpmirror.gnu.org/gnu/bc/bc-1.08.2.tar.gz"
  mirror "https://ftp.gnu.org/gnu/bc/bc-1.08.2.tar.gz"
  sha256 "ae470fec429775653e042015edc928d07c8c3b2fc59765172a330d3d87785f86"
  license "GPL-3.0-or-later"

  depends_on "texinfo" => :build

  skip_clean "bin/bc", "bin/dc"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      ENV.delete("BC_ENV_ARGS")

      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-dependency-tracking",
        "--without-libedit",
        "--without-readline"
      system "make", "-j#{ENV.make_jobs}"

      instrumented = buildpath/"dc/dc.instrumented"
      system "#{root}/scripts/run-wasm-fork-instrument.sh",
        buildpath/"dc/dc", "-o", instrumented
      mv instrumented, buildpath/"dc/dc"

      system "make", "install"
    end
  end

  test do
    assert_match(/bc(?:\.wasm)? 1\.08\.2$/,
      kandelo_run_wasm(bin/"bc", ["--version"]))
    assert_match(/\Adc(?:\.wasm)? 1\.5\.2 \(GNU bc 1\.08\.2\)$/,
      kandelo_run_wasm(bin/"dc", ["--version"]))

    source = <<~BC
      scale=30
      2^128
      1/7
    BC
    assert_equal <<~EOS, kandelo_run_wasm(bin/"bc", ["-q"], stdin: source)
      340282366920938463463374607431768211456
      .142857142857142857142857142857
    EOS

    assert_equal "dc-child5\n",
      kandelo_run_wasm(bin/"dc", [], stdin: "!printf dc-child\n2 3 + p\n")
  end
end
