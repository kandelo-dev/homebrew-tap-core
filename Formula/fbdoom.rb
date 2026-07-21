require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Fbdoom < Formula
  include KandeloFormulaSupport

  FBDOOM_COMMIT = "17280163bc95e5d954d2efaa0633489b763b4cd1".freeze
  CHOCOLATE_DOOM_COMMIT = "35fb1372d10756ca27eca05665bd8a7cebc71c05".freeze
  CHOCOLATE_DOOM_URL = "https://github.com/chocolate-doom/chocolate-doom/archive/#{CHOCOLATE_DOOM_COMMIT}.tar.gz".freeze
  CHOCOLATE_DOOM_SHA256 = "dc62c13cab469e19e0ad295b2dd7e460263c637a39c51d3771e96dabb08ecab2".freeze

  desc "Framebuffer Doom engine for Kandelo"
  homepage "https://github.com/maximevince/fbDOOM"
  url "https://github.com/maximevince/fbDOOM/archive/17280163bc95e5d954d2efaa0633489b763b4cd1.tar.gz"
  version "0.1.0"
  sha256 "77f57cee68fed438dffdba96f6070b8975c16652a63ddf4fb967994e5585a38a"
  license "GPL-2.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/fbdoom"

  resource "chocolate-doom" do
    url CHOCOLATE_DOOM_URL
    version "3.1.0"
    sha256 CHOCOLATE_DOOM_SHA256
  end

  resource "doom-shareware" do
    url "https://cdn.jsdelivr.net/gh/gaborbata/vanilla-mocha-doom@15825a07a48806bcfb242a42afd5ee7cb3c9a3a4/wads/doom1.wad"
    version "1.9"
    sha256 "1d7d43be501e67d927e415e0b8f3e29c3bf33075e859721816f652a526cac771"
  end

  def install
    kandelo_require_arch!("wasm32")
    source_dir = kandelo_stage_verified_formula_source
    chocolate_source = buildpath/"kandelo-chocolate-doom-source"

    # Transitional Tier-2 bridge: preserve the registry recipe's reviewed
    # fbdev/input/audio patch set. Homebrew verifies both pinned archives; the
    # registry recipe copies and patches them only in caller-owned work space.
    resource("chocolate-doom").stage do
      chocolate_source.mkpath
      Pathname.pwd.children.each do |entry|
        cp_r(entry, chocolate_source/entry.basename)
      end
    end

    out_dir = kandelo_build_package(
      "fbdoom", "build-fbdoom.sh", stable.url, stable.checksum.hexdigest,
      script_env: {
        "FBDOOM_CHOCOLATE_DOOM_SOURCE_DIR"    => chocolate_source,
        "FBDOOM_CHOCOLATE_DOOM_SOURCE_SHA256" => CHOCOLATE_DOOM_SHA256,
        "FBDOOM_CHOCOLATE_DOOM_SOURCE_URL"    => CHOCOLATE_DOOM_URL,
        "WASM_POSIX_DEP_SOURCE_DIR"           => source_dir,
        "WASM_POSIX_INSTALL_LOCAL_MIRROR"     => "0",
      }
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
