require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Gawk < Formula
  include KandeloFormulaSupport

  desc "GNU pattern scanning and processing language for Kandelo"
  homepage "https://www.gnu.org/software/gawk/"
  url "https://ftpmirror.gnu.org/gnu/gawk/gawk-5.3.0.tar.xz"
  mirror "https://ftp.gnu.org/gnu/gawk/gawk-5.3.0.tar.xz"
  sha256 "ca9c16d3d11d0ff8c69d79dc0b47267e1329a69b39b799895604ed447d3ca90b"
  license "GPL-3.0-or-later"

  skip_clean "bin/gawk"

  def install
    kandelo_require_arch!("wasm32")

    guest_prefix = "/home/linuxbrew/.linuxbrew"
    instrumented = buildpath/"gawk.instrumented"
    kandelo_wasm_build do |root|
      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-nls",
        "--disable-extensions",
        "--without-readline",
        "--without-mpfr",
        "--datadir=#{guest_prefix}/share",
        "--libdir=#{guest_prefix}/lib",
        "--disable-dependency-tracking"
      system "make", "-j#{ENV.make_jobs}"
      system "#{root}/scripts/run-wasm-fork-instrument.sh", buildpath/"gawk", "-o", instrumented
    end

    kandelo_install_bin(buildpath, "gawk.instrumented", "gawk")
    man1.install buildpath/"doc/gawk.1"
  end

  test do
    assert_match(/GNU Awk 5\.3\.0$/, kandelo_run_wasm(bin/"gawk", ["--version"]))

    (testpath/"sales.csv").write <<~CSV
      region,amount
      east,7
      west,3
      east,5
    CSV
    aggregate = <<~AWK
      BEGIN { FS = ","; OFS = ":" }
      NR > 1 { total[$1] += $2; count[$1]++ }
      END {
        print "east", total["east"], count["east"]
        print "west", total["west"], count["west"]
      }
    AWK
    assert_equal "east:12:2\nwest:3:1\n",
      kandelo_run_wasm(
        bin/"gawk", [aggregate, "sales.csv"], env: { "KERNEL_CWD" => testpath }
      )

    extensions = <<~'AWK'
      BEGIN {
        match("alpha42", /([a-z]+)([0-9]+)/, capture)
        print capture[1], capture[2]
        print gensub(/([a-z]+)([0-9]+)/, "\\2-\\1", 1, "beta17")
      }
    AWK
    assert_equal "alpha 42\n17-beta\n", kandelo_run_wasm(bin/"gawk", [extensions])

    (testpath/"first.txt").write("a\nb\n")
    (testpath/"second.txt").write("c\n")
    assert_equal "1:1:a\n1:2:b\n2:1:c\n",
      kandelo_run_wasm(
        bin/"gawk",
        ['{ print ARGIND ":" FNR ":" $0 }', "first.txt", "second.txt"],
        env: { "KERNEL_CWD" => testpath },
      )

    missing = kandelo_run_wasm(
      bin/"gawk", ["{ print }", "missing.txt"],
      env:             { "KERNEL_CWD" => testpath },
      merge_stderr:    true,
      expected_status: 2
    )
    assert_match(/missing\.txt.*No such file or directory/, missing)
  end
end
