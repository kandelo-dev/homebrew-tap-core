require_relative "../Kandelo/formula_support/kandelo_formula_support"

class Sed < Formula
  include KandeloFormulaSupport

  desc "GNU stream editor for Kandelo"
  homepage "https://www.gnu.org/software/sed/"
  url "https://ftpmirror.gnu.org/gnu/sed/sed-4.9.tar.xz"
  mirror "https://ftp.gnu.org/gnu/sed/sed-4.9.tar.xz"
  sha256 "6e226b732e1cd739464ad6862bd1a1aba42d7982922da7a53519631d24975181"
  license "GPL-3.0-or-later"

  skip_clean "bin/sed"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      ENV["gl_cv_func_strerror_0_works"] = "yes"

      system kandelo_configure, *kandelo_std_configure_args, "--disable-nls"
      system "make"
    end

    kandelo_install_bin(buildpath/"sed", "sed", "sed")
  end

  test do
    records = <<~EOS
      drop:zero
      keep:alpha=12
      keep:beta=34
    EOS
    select = "/^keep:/ { s/^keep:([a-z]+)=([0-9]+)$/\\1 \\2/; p; }"
    assert_equal "alpha 12\nbeta 34\n",
      kandelo_run_wasm(bin/"sed", ["-E", "-n", "-e", select], stdin: records)

    joined = kandelo_run_wasm(
      bin/"sed",
      ["-n", "-e", "1h; 2H; 2g; 2s/\\n/|/; 2p"],
      stdin: "one\ntwo\nthree\n",
    )
    assert_equal "one|two\n", joined

    transformed = kandelo_run_wasm(
      bin/"sed",
      ["-e", "2,3d", "-e", "y/abc/XYZ/"],
      stdin: "abc\nbca\ncab\nplain\n",
    )
    assert_equal "XYZ\nplXin\n", transformed
  end
end
