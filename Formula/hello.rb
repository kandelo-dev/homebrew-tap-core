require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Hello < Formula
  include KandeloFormulaSupport

  desc "Friendly greeting program for Kandelo"
  homepage "https://www.gnu.org/software/hello/"
  url "https://ftpmirror.gnu.org/gnu/hello/hello-2.12.3.tar.gz"
  sha256 "0d5f60154382fee10b114a1c34e785d8b1f492073ae2d3a6f7b147687b366aa0"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/hello"

  def install
    kandelo_root = kandelo_activate_sdk!

    out_dir = buildpath/"kandelo-package-out"
    ENV["WASM_POSIX_DEP_VERSION"] = version.to_s
    ENV["WASM_POSIX_DEP_SOURCE_URL"] = "https://ftpmirror.gnu.org/gnu/hello/hello-#{version}.tar.gz"
    ENV["WASM_POSIX_DEP_SOURCE_SHA256"] = "0d5f60154382fee10b114a1c34e785d8b1f492073ae2d3a6f7b147687b366aa0"
    ENV["WASM_POSIX_DEP_OUT_DIR"] = out_dir
    ENV["WASM_POSIX_DEP_WORK_DIR"] = buildpath/"kandelo-package-work"
    ENV["WASM_POSIX_DEP_TARGET_ARCH"] = ENV.fetch(
      "HOMEBREW_KANDELO_ARCH", ENV.fetch("KANDELO_HOMEBREW_ARCH", "wasm32")
    )

    system "bash", "#{kandelo_root}/packages/registry/hello/build-hello.sh"
    chmod 0755, out_dir/"hello.wasm"
    bin.install out_dir/"hello.wasm" => "hello"
    chmod 0755, bin/"hello"
  end

  test do
    hello = bin/"hello"
    assert_equal "\0asm".b, File.binread(hello, 4)

    output = kandelo_run_wasm(hello, ["--version"])
    assert_match "hello (GNU Hello) #{version}", output
  end
end
