require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Fbdoom < Formula
  include KandeloFormulaSupport

  desc "Framebuffer-native Doom engine for Kandelo"
  homepage "https://github.com/maximevince/fbDOOM"
  url "https://github.com/maximevince/fbDOOM/archive/17280163bc95e5d954d2efaa0633489b763b4cd1.tar.gz"
  version "0.1.0"
  sha256 "77f57cee68fed438dffdba96f6070b8975c16652a63ddf4fb967994e5585a38a"
  license "GPL-2.0-or-later"

  depends_on "wabt" => [:build, :test]
  skip_clean "bin/fbdoom"

  resource "chocolate-doom" do
    url "https://github.com/chocolate-doom/chocolate-doom/archive/35fb1372d10756ca27eca05665bd8a7cebc71c05.tar.gz"
    sha256 "dc62c13cab469e19e0ad295b2dd7e460263c637a39c51d3771e96dabb08ecab2"
  end

  resource "doom-shareware-test" do
    url "https://distro.ibiblio.org/slitaz/sources/packages/d/doom1.wad"
    sha256 "1d7d43be501e67d927e415e0b8f3e29c3bf33075e859721816f652a526cac771"
  end

  SOURCE_DATE_EPOCH = "1775830255".freeze

  def install
    kandelo_require_arch!("wasm32")
    vendor_music_sources
    apply_kandelo_patches

    artifact = buildpath/"fbdoom/fbdoom"
    kandelo_wasm_build do |root|
      source_identity = "/usr/src/fbdoom-#{version}"
      path_flags = [
        "-ffile-prefix-map=#{buildpath}=#{source_identity}",
        "-fdebug-prefix-map=#{buildpath}=#{source_identity}",
        "-fmacro-prefix-map=#{buildpath}=#{source_identity}",
        "-ffile-prefix-map=#{root}=/usr/src/kandelo",
        "-fdebug-prefix-map=#{root}=/usr/src/kandelo",
        "-fmacro-prefix-map=#{root}=/usr/src/kandelo",
      ]
      cflags = [
        "-O2",
        "-DNORMALUNIX",
        "-DLINUX",
        "-D_DEFAULT_SOURCE",
        "-Iopl",
        *path_flags,
      ].join(" ")

      ENV["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
      system "make", "-C", "fbdoom", "clean"
      system "make", "-C", "fbdoom", "-j#{ENV.make_jobs}",
        "CC=#{kandelo_cc(root)}",
        "LD=#{kandelo_cc(root)}",
        "CFLAGS=#{cflags}",
        "LDFLAGS=-Wl,--gc-sections",
        "LIBS=-lm",
        "NOSDL=1"

      validate_artifact!(artifact, root)
    end

    kandelo_install_bin(buildpath/"fbdoom", "fbdoom", "fbdoom")
  end

  def vendor_music_sources
    resource("chocolate-doom").stage do
      opl_dir = buildpath/"fbdoom/opl"
      opl_dir.mkpath
      %w[opl.c opl.h opl3.c opl3.h opl_internal.h opl_queue.c opl_queue.h].each do |name|
        cp "opl/#{name}", opl_dir/name
      end
      %w[mus2mid.c mus2mid.h midifile.c midifile.h].each do |name|
        cp "src/#{name}", buildpath/"fbdoom"/name
      end
    end
  end

  def apply_kandelo_patches
    tap_root = Pathname(__dir__).parent
    patches = (tap_root/"patches/fbdoom").glob("*.patch").sort
    odie "fbDOOM patch set is incomplete" if patches.length != 8

    script = <<~SH
      set -euo pipefail
      for patch in "$@"; do
        git apply --check "$patch"
        git apply "$patch"
      done
    SH
    system kandelo_host_tool("bash"), "-c", script, "bash", *patches
  end

  def validate_artifact!(artifact, root)
    expected_abi = (Pathname(root)/"crates/shared/src/lib.rs").read[
      /^pub const ABI_VERSION: u32 = (\d+);$/,
      1,
    ]
    odie "could not read Kandelo ABI version" if expected_abi.nil?

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
    odie "fbDOOM ABI #{artifact_abi} does not match Kandelo ABI #{expected_abi}" if artifact_abi != expected_abi

    guards = Pathname(root)/"scripts/wasm-artifact-guards.sh"
    system "bash", "-c", <<~SH
      set -euo pipefail
      . #{guards.to_s.shellescape}
      wasm_require_no_legacy_asyncify #{artifact.to_s.shellescape}
      wasm_require_fork_instrumentation_if_needed #{artifact.to_s.shellescape}
      unexpected_env_imports=$(wasm-objdump -x #{artifact.to_s.shellescape} |
        awk '/<- env[.]/ { sub(/^.*<- env[.]/, ""); print $1 }' |
        grep -Ev '^(__channel_base|memory|setjmp|longjmp)$' || true)
      if [ -n "$unexpected_env_imports" ]; then
        echo "ERROR: fbDOOM contains unresolved non-ABI env imports" >&2
        echo "$unexpected_env_imports" >&2
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
      odie "fbDOOM embeds #{description}: #{marker}" if binary.include?(marker)
    end
    odie "fbDOOM embeds a builder home path" if binary.match?(%r{/Users/[^/]+/})
  end

  test do
    assert_equal "\0asm".b, File.binread(bin/"fbdoom", 4)

    # The shareware data is staged only for this test and is never installed
    # in the keg or bottle. It is the same immutable IWAD used by Kandelo's
    # Doom browser demo.
    resource("doom-shareware-test").stage testpath
    wad = testpath/"doom1.wad"
    assert_path_exists wad

    # The timedemo loads and renders demo1 through Kandelo's Node host, then
    # reaches fbDOOM's timing-report failure exit instead of its old NOSDL
    # infinite loop. That path also covers the patched atexit callback
    # signature instead of treating --version as game execution evidence.
    node_output = kandelo_run_pty_wasm(
      bin/"fbdoom", ["-iwad", "/doom1.wad", "-timedemo", "demo1"],
      inputs:          [],
      env:             {
        "HOME"       => "/tmp",
        "KERNEL_CWD" => "/",
        "TERM"       => "xterm-256color",
        "TIMEOUT"    => "120000",
      },
      guest_files:     { "/doom1.wad" => wad },
      expected_status: 1
    )
    refute_includes node_output, "process timed out after"
    assert_match(/timed \d+ gametics in \d+ realtics \([0-9.]+ fps\)/, node_output)

    browser_output = kandelo_run_framebuffer_wasm(
      bin/"fbdoom",
      argv:                ["-iwad", "/doom1.wad"],
      guest_files:         { "/doom1.wad" => wad },
      min_writes:          2,
      min_nonblank_pixels: 100_000,
      timeout_ms:          45_000,
    )
    assert_match(
      /^kandelo-framebuffer-ok
       \s+binds=\d+
       \s+writes=\d+
       \s+bytes=\d+
       \s+size=\d+x\d+
       \s+format=\S+
       \s+nonblank=\d+
       \s+screenshot-bytes=\d+$/x,
      browser_output,
    )

    binary = File.binread(bin/"fbdoom")
    refute_includes binary, prefix.to_s
    refute_includes binary, "/nix/store/"
    refute_match %r{/private/tmp/[^/]+/}, binary
    refute_match %r{/Users/[^/]+/}, binary
  end
end
