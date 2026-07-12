require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Gzip < Formula
  include KandeloFormulaSupport

  desc "GNU compression utility for Kandelo"
  homepage "https://www.gnu.org/software/gzip/"
  url "https://ftpmirror.gnu.org/gnu/gzip/gzip-1.14.tar.xz"
  mirror "https://ftp.gnu.org/gnu/gzip/gzip-1.14.tar.xz"
  sha256 "01a7b881bd220bfdf615f97b8718f80bdfd3f6add385b993dcf6efd14e8c0ac6"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/gzip"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      # Upstream's supported non-shell mode makes gunzip and zcat dispatch
      # from argv[0], matching Kandelo's executable-alias rootfs contract.
      ENV.append "CPPFLAGS", "-DGNU_STANDARD=0"

      system kandelo_configure, *kandelo_std_configure_args, "--disable-nls"
      system "make"
      kandelo_validate_wasm_artifact(buildpath/"gzip", fork: :forbidden)
    end

    kandelo_install_bin(buildpath, "gzip", "gzip")
    bin.install_symlink "gzip" => "gunzip"
    bin.install_symlink "gzip" => "zcat"
  end

  test do
    first = "first Kandelo gzip member\n".b
    second = "second Kandelo gzip member\n".b
    first_gzip = kandelo_run_wasm(bin/"gzip", ["-c", "-f"], stdin: first).b
    second_gzip = kandelo_run_wasm(bin/"gzip", ["-c", "-f"], stdin: second).b

    assert_equal [0x1f, 0x8b], first_gzip.bytes.first(2)
    assert_equal first + second,
      kandelo_run_wasm(
        bin/"gunzip", ["-c"], stdin: first_gzip + second_gzip, preserve_argv0: true
      ).b
    assert_equal first,
      kandelo_run_wasm(bin/"zcat", [], stdin: first_gzip, preserve_argv0: true).b
  end
end
