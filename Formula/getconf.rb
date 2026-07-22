require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Getconf < Formula
  include KandeloFormulaSupport

  desc "Query POSIX system and pathname configuration for Kandelo"
  homepage "https://man.openbsd.org/getconf.1"
  url "https://raw.githubusercontent.com/openbsd/src/d7259957e8a5d4370d76bfccd4a30d5d1fe80f38/usr.bin/getconf/getconf.c"
  version "1.23"
  sha256 "e1c8be153cc3cfefa1a24bcaf62fe74d4d78eeadba660f320b09931e29d95c65"
  license "BSD-4-Clause"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin/getconf"

  resource "manpage" do
    url "https://raw.githubusercontent.com/openbsd/src/d7259957e8a5d4370d76bfccd4a30d5d1fe80f38/usr.bin/getconf/getconf.1"
    sha256 "0acea5eed79da7b0dd04ad39a4e7b811a92da5311a8f321e8bb04339ed8ad328"
  end

  def install
    kandelo_require_arch!("wasm32")
    artifact = buildpath/"getconf.wasm"
    compat = buildpath/"kandelo-openbsd-compat.h"
    compat.write <<~HEADER
      #ifndef KANDELO_OPENBSD_GETCONF_COMPAT_H
      #define KANDELO_OPENBSD_GETCONF_COMPAT_H

      #ifndef __dead
      #define __dead __attribute__((__noreturn__))
      #endif

      static inline int pledge(const char *promises, const char *execpromises) {
        (void)promises;
        (void)execpromises;
        return 0;
      }

      static inline int unveil(const char *path, const char *permissions) {
        (void)path;
        (void)permissions;
        return 0;
      }

      #endif
    HEADER

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/openbsd-getconf-#{version}"
      prefix_maps = {
        buildpath.to_s => stable_source,
        root.to_s      => "/usr/src/kandelo",
        "/nix/store"   => "/usr/src/toolchain",
      }.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end

      # pledge(2) and unveil(2) are OpenBSD host-sandbox boundaries. Kandelo's
      # process remains confined by its VFS and host runtime; every reported
      # value still comes from the target's sysconf, pathconf, or confstr API.
      system kandelo_cc,
        "-std=c17", "-O2", "-gline-tables-only", "-D_POSIX_C_SOURCE=200809L",
        "-include", compat.basename,
        "-fdebug-compilation-dir=#{stable_source}", *prefix_maps,
        buildpath/"getconf.c", "-o", artifact
      kandelo_validate_wasm_artifact(
        artifact,
        fork:            :forbidden,
        forbidden_paths: [buildpath.to_s],
      )
    end

    kandelo_install_bin(buildpath, artifact.basename, "getconf")
    resource("manpage").stage { man1.install "getconf.1" }
  end

  test do
    assert_path_exists man1/"getconf.1"

    node = lambda do |argv, **options|
      kandelo_run_wasm(bin/"getconf", argv, argv0: "/usr/local/bin/getconf", **options)
    end
    chromium = lambda do |argv, **options|
      kandelo_run_browser_wasm(bin/"getconf", argv, argv0: "getconf", **options)
    end

    {
      ["PAGESIZE"]               => "65536\n",
      ["OPEN_MAX"]               => "1024\n",
      ["NPROCESSORS_ONLN"]       => "1\n",
      ["PATH"]                   => "/bin:/usr/bin\n",
      ["_POSIX_VERSION"]         => "200809\n",
      ["_POSIX_V7_ILP32_OFFBIG"] => "1\n",
      ["_POSIX_V7_LP64_OFF64"]   => "undefined\n",
    }.each do |argv, expected|
      assert_equal expected, node.call(argv)
      assert_equal expected, chromium.call(argv)
    end

    v7_flags = {}
    %w[CFLAGS LDFLAGS LIBS].each do |kind|
      variable = "POSIX_V7_ILP32_OFFBIG_#{kind}"
      argv = ["-v", "POSIX_V7_ILP32_OFFBIG", variable]
      v7_flags[kind] = node.call(argv).chomp
      assert_equal v7_flags[kind] + "\n", chromium.call(argv)
    end

    workspace = testpath/"workspace"
    workspace.mkpath
    sample = workspace/"sample.txt"
    sample.write "sample\n"
    node_mount = { "/work" => workspace }
    browser_files = { "/work/sample.txt" => sample }

    assert_equal "255\n", node.call(
      ["NAME_MAX", "/work/sample.txt"], writable_host_directories: node_mount
    )
    assert_equal "255\n", chromium.call(
      ["NAME_MAX", "/work/sample.txt"], guest_files: browser_files
    )
    assert_equal "4096\n", node.call(
      ["PATH_MAX", "/work"], writable_host_directories: node_mount
    )
    assert_equal "4096\n", chromium.call(
      ["PATH_MAX", "/work"], guest_files: browser_files
    )

    unknown_expected = "getconf: NOT_A_VARIABLE: unknown variable\n"
    assert_equal unknown_expected, node.call(
      ["NOT_A_VARIABLE"], merge_stderr: true, expected_status: 1
    )
    assert_equal unknown_expected, chromium.call(
      ["NOT_A_VARIABLE"], merge_stderr: true, expected_status: 1
    )

    missing_expected = "getconf: /work/missing: No such file or directory\n"
    assert_equal missing_expected, node.call(
      ["NAME_MAX", "/work/missing"],
      merge_stderr: true, expected_status: 1, writable_host_directories: node_mount,
    )
    assert_equal missing_expected, chromium.call(
      ["NAME_MAX", "/work/missing"],
      guest_files: browser_files, merge_stderr: true, expected_status: 1,
    )

    unsupported_env = [
      "-v", "POSIX_V7_LP64_OFF64", "POSIX_V7_LP64_OFF64_CFLAGS"
    ]
    unsupported_expected = "getconf: POSIX_V7_LP64_OFF64: unknown specification\n"
    assert_equal unsupported_expected, node.call(
      unsupported_env, merge_stderr: true, expected_status: 1
    )
    assert_equal unsupported_expected, chromium.call(
      unsupported_env, merge_stderr: true, expected_status: 1
    )

    kandelo_activate_sdk!
    kandelo_activate_sysroot!
    smoke_c = testpath/"v7-flags-smoke.c"
    smoke_wasm = testpath/"v7-flags-smoke.wasm"
    smoke_c.write <<~C
      #include <stdio.h>
      int main(void) {
        puts("v7-flags-ok");
        return 0;
      }
    C
    system kandelo_cc,
      *Shellwords.split(v7_flags.fetch("CFLAGS")), smoke_c,
      *Shellwords.split(v7_flags.fetch("LDFLAGS")),
      *Shellwords.split(v7_flags.fetch("LIBS")), "-o", smoke_wasm
    assert_equal "v7-flags-ok\n", kandelo_run_wasm(smoke_wasm, [])
    assert_equal "v7-flags-ok\n", kandelo_run_browser_wasm(smoke_wasm, [])

    interleave_c = testpath/"interleaved-output.c"
    interleave_wasm = testpath/"interleaved-output.wasm"
    interleave_c.write <<~C
      #include <stddef.h>
      #include <unistd.h>

      static int write_all(int fd, const char *data, size_t length) {
        while (length > 0) {
          ssize_t written = write(fd, data, length);
          if (written <= 0) return -1;
          data += written;
          length -= (size_t)written;
        }
        return 0;
      }

      int main(void) {
        if (write_all(STDOUT_FILENO, "stdout-1\\n", 9) != 0) return 2;
        if (write_all(STDERR_FILENO, "stderr-1\\n", 9) != 0) return 2;
        if (write_all(STDOUT_FILENO, "stdout-2\\n", 9) != 0) return 2;
        if (write_all(STDERR_FILENO, "stderr-2\\n", 9) != 0) return 2;
        return 0;
      }
    C
    system kandelo_cc, interleave_c, "-o", interleave_wasm
    interleaved = "stdout-1\nstderr-1\nstdout-2\nstderr-2\n"
    assert_equal interleaved, kandelo_run_wasm(interleave_wasm, [], merge_stderr: true)
    assert_equal interleaved, kandelo_run_browser_wasm(
      interleave_wasm, [], argv0: "interleaved-output", merge_stderr: true
    )
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "8ac10cdc394fc6ac9e9538c1e4726b294129cf9971fa6d8fa0c29588791c4e62"
  end

end
