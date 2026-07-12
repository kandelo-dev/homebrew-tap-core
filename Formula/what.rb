require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class What < Formula
  include KandeloFormulaSupport

  desc "Extract SCCS identification strings for Kandelo"
  homepage "https://man.freebsd.org/cgi/man.cgi?query=what&sektion=1"
  url "https://raw.githubusercontent.com/freebsd/freebsd-src/7aedc8de6446ad5a10d553b926423c689f0a3363/usr.bin/what/what.c"
  version "15.0.0"
  sha256 "218d0e29d362c49eec614bdbde551ee1ef3e9fa254e28748598a06100bd6bbd7"
  license "BSD-3-Clause"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/what"

  resource "manpage" do
    url "https://raw.githubusercontent.com/freebsd/freebsd-src/7aedc8de6446ad5a10d553b926423c689f0a3363/usr.bin/what/what.1"
    sha256 "b4577871109a3290ad807a28576830f884e94ad0eb9b0861a78a0f88f35e9556"
  end

  def install
    kandelo_require_arch!("wasm32")
    artifact = buildpath/"what.wasm"

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/freebsd-what-#{version}"
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

      # FreeBSD spells its compiler noreturn annotation as __dead2.
      system kandelo_cc,
        "-std=c17", "-O2", "-gline-tables-only", "-D_POSIX_C_SOURCE=200809L",
        "-D__dead2=__attribute__((__noreturn__))",
        "-fdebug-compilation-dir=#{stable_source}", *prefix_maps,
        buildpath/"what.c", "-o", artifact
      kandelo_validate_wasm_artifact(artifact, fork: :forbidden)
    end

    kandelo_install_bin(buildpath, artifact.basename, "what")
    resource("manpage").stage { man1.install "what.1" }
  end

  test do
    assert_path_exists man1/"what.1"

    identifiers = "noise @(#)alpha\n@(x)not-a-marker @(#)beta\" @(#)gamma> @(#)delta\\ @(#)epsilon\0tail".b
    expected = "\talpha\n\tbeta\n\tgamma\n\tdelta\n\tepsilon\n"
    assert_equal expected, kandelo_run_wasm(bin/"what", [], stdin: identifiers)
    assert_equal "alpha\n", kandelo_run_wasm(bin/"what", ["-q", "-s"], stdin: identifiers)
    assert_empty kandelo_run_wasm(bin/"what", [], stdin: "no identifiers", expected_status: 1)

    workspace = testpath/"workspace"
    workspace.mkpath
    (workspace/"artifact.bin").binwrite "prefix\0not-a-marker\0@(#)file-version\\trailer".b
    output = kandelo_run_wasm(
      bin/"what", ["artifact.bin"],
      env:                       { "KERNEL_CWD" => "/work" },
      writable_host_directories: { "/work" => workspace }
    )
    assert_equal "artifact.bin:\n\tfile-version\n", output
    assert_empty kandelo_run_wasm(
      bin/"what", ["-q", "missing.bin"],
      env:                       { "KERNEL_CWD" => "/work" },
      writable_host_directories: { "/work" => workspace },
      expected_status:           1
    )
  end
end
