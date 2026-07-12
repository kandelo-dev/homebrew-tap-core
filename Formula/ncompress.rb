require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Ncompress < Formula
  include KandeloFormulaSupport

  desc "LZW compress and uncompress utilities for Kandelo"
  homepage "https://vapier.github.io/ncompress/"
  url "https://github.com/vapier/ncompress/archive/refs/tags/v5.0.tar.gz"
  sha256 "96ec931d06ab827fccad377839bfb91955274568392ddecf809e443443aead46"
  license "Unlicense"
  revision 1

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/compress"

  def install
    kandelo_require_arch!("wasm32")
    artifact = buildpath/"compress.wasm"

    kandelo_wasm_build do |root|
      source_identity = "/usr/src/ncompress-#{version}"
      path_flags = [
        "-ffile-prefix-map=#{buildpath}=#{source_identity}",
        "-fdebug-prefix-map=#{buildpath}=#{source_identity}",
        "-fmacro-prefix-map=#{buildpath}=#{source_identity}",
        "-ffile-prefix-map=#{root}=/usr/src/kandelo-sdk",
        "-fdebug-prefix-map=#{root}=/usr/src/kandelo-sdk",
        "-fmacro-prefix-map=#{root}=/usr/src/kandelo-sdk",
      ]
      system kandelo_cc,
        "-std=gnu17", "-O2", "-gline-tables-only", "-D_POSIX_C_SOURCE=200809L",
        "-DUSERMEM=800000", "-DUTIME_H=1", "-DLSTAT=1", *path_flags,
        "compress.c", "-o", artifact
      kandelo_validate_wasm_artifact(
        artifact,
        fork:            :forbidden,
        forbidden_paths: [buildpath.to_s, prefix.to_s],
      )
    end

    kandelo_install_bin(buildpath, artifact.basename, "compress")
    bin.install_symlink "compress" => "uncompress"
    man1.install "compress.1", "uncompress.1"
  end

  test do
    assert_equal "compress", (bin/"uncompress").readlink.to_s
    # GNU gzip owns zcat and also decodes legacy compress streams.
    refute_path_exists bin/"zcat"
    assert_path_exists man1/"compress.1"
    assert_path_exists man1/"uncompress.1"

    input = ((0..255).to_a.pack("C*") + "Kandelo LZW compatibility\n") * 32
    compressed = kandelo_run_wasm(bin/"compress", ["-c", "-b", "12"], stdin: input).b
    assert_equal [0x1F, 0x9D], compressed.bytes.first(2)
    refute_equal input, compressed
    assert_equal input,
      kandelo_run_wasm(bin/"uncompress", ["-c"], stdin: compressed, preserve_argv0: true).b

    workspace = testpath/"filesystem"
    workspace.mkpath
    file_input = "Kandelo file compression\n" * 256
    (workspace/"sample.txt").write(file_input)
    mount = { "/work" => workspace }
    env = { "KERNEL_CWD" => "/work" }
    assert_empty kandelo_run_wasm(
      bin/"compress", ["-f", "sample.txt"],
      env: env, writable_host_directories: mount
    )
    refute_path_exists workspace/"sample.txt"
    assert_path_exists workspace/"sample.txt.Z"
    assert_empty kandelo_run_wasm(
      bin/"uncompress", ["-f", "sample.txt.Z"],
      env: env, preserve_argv0: true, writable_host_directories: mount
    )
    assert_equal file_input, (workspace/"sample.txt").read
    refute_path_exists workspace/"sample.txt.Z"
  end
end
