require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Fbdoom < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

  FBDOOM_COMMIT = "17280163bc95e5d954d2efaa0633489b763b4cd1".freeze

  desc "Framebuffer Doom engine for Kandelo"
  homepage "https://github.com/maximevince/fbDOOM"
  url "https://github.com/maximevince/fbDOOM/archive/17280163bc95e5d954d2efaa0633489b763b4cd1.tar.gz"
  version "0.1.0"
  sha256 "77f57cee68fed438dffdba96f6070b8975c16652a63ddf4fb967994e5585a38a"
  license "GPL-2.0-or-later"

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 3
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "acde2bc27bfa90048e7960e8755604066de3ef33087f246b01318cde20e9b5ba"
  end

  depends_on "gpatch" => :build

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin/fbdoom"

  resource "chocolate-doom" do
    url "https://github.com/chocolate-doom/chocolate-doom/archive/35fb1372d10756ca27eca05665bd8a7cebc71c05.tar.gz"
    version "3.1.0"
    sha256 "dc62c13cab469e19e0ad295b2dd7e460263c637a39c51d3771e96dabb08ecab2"
  end

  resource "doom-shareware" do
    url "https://cdn.jsdelivr.net/gh/gaborbata/vanilla-mocha-doom@15825a07a48806bcfb242a42afd5ee7cb3c9a3a4/wads/doom1.wad"
    version "1.9"
    sha256 "1d7d43be501e67d927e415e0b8f3e29c3bf33075e859721816f652a526cac771"
  end

  def install
    kandelo_require_arch!("wasm32")
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "1e0621951982185a3b51e87eadb1c71bca0ad0afec3f3fabce5ca1e21670b6b0",
      resources:       ["chocolate-doom"],
      script_env:      {},
    )
    kandelo_validate_wasm_artifact(out_dir/"fbdoom.wasm", fork: :forbidden)
    kandelo_install_bin(out_dir, "fbdoom.wasm", "fbdoom")
  end

  test do
    # A poured bottle does not fetch source resources. Stage the verified test
    # IWAD explicitly so source builds and anonymous bottle tests are peers.
    resource("doom-shareware").stage testpath
    wad = testpath/"doom1.wad"
    guest_files = { "/doom1.wad" => wad }
    output = kandelo_run_pty_wasm(
      bin/"fbdoom", ["-iwad", "/doom1.wad", "-timedemo", "demo1", "-nodraw", "-nogui"],
      inputs: [], env: { "HOME" => "/home/doom" }, guest_files: guest_files,
      timeout_ms: 120_000, completion_output: " gametics in "
    )
    assert_match(/timed .* gametics/i, output)

    kandelo_run_framebuffer_wasm(
      bin/"fbdoom", argv: ["-iwad", "/doom1.wad", "-nogui"],
      guest_files: guest_files, min_writes: 1, min_nonblank_pixels: 1_000
    )
  end
end
