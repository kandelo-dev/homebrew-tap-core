require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Netcat < Formula
  include KandeloFormulaSupport

  desc "GNU network utility for Kandelo"
  homepage "https://netcat.sourceforge.net/"
  url "https://downloads.sourceforge.net/project/netcat/netcat/0.7.1/netcat-0.7.1.tar.gz"
  sha256 "30719c9a4ffbcf15676b8f528233ccc54ee6cba96cb4590975f5fd60c68a066f"
  license "GPL-2.0-or-later"

  depends_on "automake" => :build
  depends_on "binaryen" => :build
  depends_on "gpatch" => :build
  depends_on "wabt" => :build

  skip_clean "bin/nc"

  def install
    kandelo_require_arch!("wasm32")

    # Transitional Tier-2 bridge: the registry recipe owns the reviewed
    # network compatibility patch set and its exact configure assertions.
    out_dir = kandelo_build_package(
      "netcat", "build-netcat.sh", stable.url, stable.checksum.hexdigest,
      script_env: { "WASM_POSIX_INSTALL_LOCAL_MIRROR" => "0" }
    )
    kandelo_validate_wasm_artifact(out_dir/"nc.wasm", fork: :forbidden)
    kandelo_install_bin(out_dir, "nc.wasm", "nc")
  end

  test do
    output = kandelo_run_wasm(bin/"nc", ["--version"], merge_stderr: true)
    assert_match(/netcat \(The GNU Netcat\) 0\.7\.1/i, output)
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "c91ab7e7944a79927cc609fe78dffe789e77172f777852f964cb36242370ec9d"
  end

end
