require_relative "../Kandelo/formula_support/kandelo_formula_support"

class Modeset < Formula
  include KandeloFormulaSupport

  desc "DRM/KMS WebGL fluid simulation for Kandelo"
  homepage "https://github.com/Automattic/kandelo/tree/main/programs"
  url "https://github.com/Automattic/kandelo/archive/fff6e37bf288cd9fb7d4ebc0f8bd3d6abd7998aa.tar.gz"
  version "0.1.0"
  sha256 "d8b7f263638cd87b81e2cd07df9602fbea15da8999fbc21c045d8c38a45fb652"
  license "GPL-2.0-or-later"

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

      # Preserve the registry recipe's instrumentation stage. The instrumenter
      # binds the executable to the active ABI and installs the continuation
      # runtime even though modeset does not currently call fork itself.
      kandelo_fork_instrument artifact
      validate_artifact!(artifact, root)
    end

    kandelo_install_bin(buildpath, artifact.basename, "modeset")
  end

  def validate_artifact!(artifact, root)
    expected_abi = (Pathname(root)/"crates/shared/src/lib.rs").read[
      /^pub const ABI_VERSION: u32 = (\d+);$/,
      1,
    ]
    odie "could not read Kandelo ABI version" if expected_abi.nil?

    # wasm-objdump labels this exported function with its source-level name
    # under Homebrew's debug flags. Use Kandelo's binary parser, which follows
    # the export index and function body rather than depending on name metadata.
    host_dist = Pathname(root)/"host/dist"
    rm_r host_dist if host_dist.exist?
    abi_probe = <<~JS
      import { readFileSync } from "node:fs";
      import { pathToFileURL } from "node:url";
      const { extractAbiVersion } = await import(pathToFileURL(process.argv[1]).href);
      const bytes = readFileSync(process.argv[2]);
      const program = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      const abi = extractAbiVersion(program);
      if (abi === null) process.exit(2);
      process.stdout.write(String(abi));
    JS
    artifact_abi = cd(root) do
      Utils.safe_popen_read(
        "node", "--import", "tsx/esm", "--input-type=module", "--eval", abi_probe,
        Pathname(root)/"host/src/constants.ts", artifact
      ).strip
    end
    odie "modeset ABI #{artifact_abi} does not match Kandelo ABI #{expected_abi}" if artifact_abi != expected_abi

    guards = Pathname(root)/"scripts/wasm-artifact-guards.sh"
    system "bash", "-c", <<~SH
      set -euo pipefail
      . #{guards.to_s.shellescape}
      wasm_require_no_legacy_asyncify #{artifact.to_s.shellescape}
      wasm_require_fork_instrumentation_if_needed #{artifact.to_s.shellescape}
      if ! wasm_has_complete_fork_instrumentation #{artifact.to_s.shellescape}; then
        echo "ERROR: modeset lacks complete fork instrumentation exports" >&2
        exit 1
      fi
    SH

    binary = artifact.binread
    {
      "formula build path"    => buildpath.to_s,
      "formula Cellar path"   => prefix.to_s,
      "Kandelo checkout path" => root.to_s,
      "Nix store path"        => "/nix/store/",
      "temporary build path"  => "/private/tmp/",
    }.each do |description, marker|
      odie "modeset embeds #{description}: #{marker}" if binary.include?(marker)
    end
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
