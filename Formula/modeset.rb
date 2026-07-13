require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Modeset < Formula
  include KandeloFormulaSupport

  desc "DRM/KMS WebGL fluid simulation for Kandelo"
  homepage "https://github.com/Automattic/kandelo/tree/main/programs"
  url "https://github.com/Automattic/kandelo/archive/fff6e37bf288cd9fb7d4ebc0f8bd3d6abd7998aa.tar.gz"
  version "0.1.0"
  sha256 "d8b7f263638cd87b81e2cd07df9602fbea15da8999fbc21c045d8c38a45fb652"
  license "GPL-2.0-or-later"

  depends_on "binaryen" => [:build, :test]
  depends_on "wabt" => [:build, :test]

  skip_clean "bin/modeset"

  def install
    kandelo_require_arch!("wasm32")
    artifact = buildpath/"modeset.wasm"

    kandelo_wasm_build do |root|
      pkg_config = kandelo_tool("pkg-config", root)
      cflags = Utils.safe_popen_read(
        pkg_config, "--cflags", "libdrm", "gbm", "egl", "glesv2"
      ).shellsplit
      libraries = Utils.safe_popen_read(
        pkg_config, "--libs", "gbm", "libdrm", "egl", "glesv2"
      ).shellsplit
      source_identity = "/usr/src/modeset-#{version}"
      path_flags = [
        "-ffile-prefix-map=#{buildpath}=#{source_identity}",
        "-fdebug-prefix-map=#{buildpath}=#{source_identity}",
        "-fmacro-prefix-map=#{buildpath}=#{source_identity}",
      ]

      system kandelo_cc(root),
        "-std=c11", "-O2", "-Wall", "-Wextra", "-Wno-unused-parameter",
        "-D_DEFAULT_SOURCE", *path_flags, *cflags, buildpath/"programs/modeset.c",
        *libraries, "-lm", "-o", artifact

      kandelo_validate_wasm_artifact(artifact, fork: :forbidden)
    end

    kandelo_install_bin(buildpath, artifact.basename, "modeset")
  end

  test do
    assert_equal "\0asm".b, File.binread(bin/"modeset", 4)
    output = kandelo_run_kms_wasm(
      bin/"modeset", argv: ["modeset"], min_page_flips: 2, timeout_ms: 30_000
    )
    flips = output[/^kandelo-kms-ok flips=(\d+)$/, 1]
    refute_nil flips
    assert_operator Integer(flips), :>=, 2

    browser_output = kandelo_run_kms_browser_wasm(
      bin/"modeset", argv: ["modeset"], min_page_flips: 2, timeout_ms: 90_000
    )
    assert_match(
      /^kandelo-kms-browser-ok flips=\d+ size=\d+x\d+ pixels=\d+ luma-range=\d+ screenshot-bytes=\d+$/,
      browser_output,
    )

    binary = File.binread(bin/"modeset")
    refute_includes binary, prefix.to_s
    refute_includes binary, "/nix/store/"
    refute_match %r{/private/tmp/[^/]+/}, binary
    refute_match %r{/Users/[^/]+/}, binary
  end
end
