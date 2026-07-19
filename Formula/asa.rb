require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Asa < Formula
  include KandeloFormulaSupport

  desc "Interpret FORTRAN carriage-control characters for Kandelo"
  homepage "https://man.freebsd.org/cgi/man.cgi?query=asa&sektion=1"
  url "https://raw.githubusercontent.com/freebsd/freebsd-src/7aedc8de6446ad5a10d553b926423c689f0a3363/usr.bin/asa/asa.c"
  version "15.0.0"
  sha256 "7d9722ea86e05e716500f47852f950fd3f5bfd961e38639c54884f56587ca0a0"
  license "BSD-4-Clause"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/asa"

  resource "manpage" do
    url "https://raw.githubusercontent.com/freebsd/freebsd-src/7aedc8de6446ad5a10d553b926423c689f0a3363/usr.bin/asa/asa.1"
    sha256 "accc646d376de08b406bbdceaa266ca20d39fae285e62f4095fd36ec8e8728c2"
  end

  def install
    kandelo_require_arch!("wasm32")
    artifact = buildpath/"asa.wasm"

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/freebsd-asa-#{version}"
      mapped_roots = {
        buildpath.to_s               => stable_source,
        root.to_s                    => "/usr/src/kandelo",
        Pathname(root).realpath.to_s => "/usr/src/kandelo",
        "/nix/store"                 => "/usr/src/toolchain",
      }
      prefix_maps = mapped_roots.uniq.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end

      # FreeBSD's source uses its non-POSIX noreturn spelling and fgetln(3).
      # Kandelo's musl exposes the latter under the normal BSD feature gate.
      system kandelo_cc,
        "-std=c17", "-O2", "-gline-tables-only", "-D_BSD_SOURCE",
        "-D__dead2=__attribute__((__noreturn__))",
        "-fdebug-compilation-dir=#{stable_source}", *prefix_maps,
        buildpath/"asa.c", "-o", artifact
      kandelo_validate_wasm_artifact(artifact, fork: :forbidden)
    end

    kandelo_install_bin(buildpath, artifact.basename, "asa")
    resource("manpage").stage { man1.install "asa.1" }
  end

  test do
    assert_path_exists man1/"asa.1"

    carriage_control = " first\n0second\n1third\n+overprint\nXdefault\n"
    expected = "first\n\nsecond\n\fthird\roverprint\ndefault\n"
    assert_equal expected, kandelo_run_wasm(bin/"asa", [], stdin: carriage_control)
    assert_equal "unterminated", kandelo_run_wasm(bin/"asa", [], stdin: " unterminated")

    workspace = testpath/"workspace"
    workspace.mkpath
    (workspace/"first.txt").write " left\n"
    (workspace/"second.txt").write "0right"
    output = kandelo_run_wasm(
      bin/"asa", ["first.txt", "missing.txt", "second.txt"],
      env:                       { "KERNEL_CWD" => "/work" },
      writable_host_directories: { "/work" => workspace },
      expected_status:           1
    )
    assert_equal "left\n\nright", output
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "8dc6dea5229d1b01b06ce66059c235ec0b552f1e3ccda12cd526aa92a42a3634"
  end

end
