require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Procps < Formula
  include KandeloFormulaSupport

  desc "Utilities for browsing Kandelo procfs"
  homepage "https://gitlab.com/procps-ng/procps"
  url "https://downloads.sourceforge.net/project/procps-ng/Production/procps-ng-4.0.6.tar.xz"
  sha256 "67bea6fbc3a42a535a0230c9e891e5ddfb4d9d39422d46565a2990d1ace15216"
  license "GPL-2.0-or-later"

  depends_on "binaryen" => :build
  depends_on "pkgconf" => :build
  depends_on "wabt" => [:build, :test]

  skip_clean "bin/ps"

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/procps".freeze

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/procps-ng-#{version}"
      ENV["CFLAGS"] = [
        "-O2", "-gline-tables-only", "-fdebug-compilation-dir=#{stable_source}",
        "-ffile-prefix-map=#{buildpath}=#{stable_source}",
        "-fdebug-prefix-map=#{buildpath}=#{stable_source}",
        "-fmacro-prefix-map=#{buildpath}=#{stable_source}",
        "-ffile-prefix-map=#{root}=/usr/src/kandelo",
        "-fdebug-prefix-map=#{root}=/usr/src/kandelo",
        "-fmacro-prefix-map=#{root}=/usr/src/kandelo"
      ].join(" ")
      ENV["LC_ALL"] = "C"
      ENV["SOURCE_DATE_EPOCH"] = "0"
      ENV["TZ"] = "UTC"
      ENV["ZERO_AR_DATE"] = "1"

      # Kandelo executables permit unresolved kernel imports, so link-based
      # Autoconf probes can report absent libc APIs as present.
      ENV["ac_cv_func_pidfd_send_signal"] = "no"
      ENV["ac_cv_func_sigabbrev_np"] = "no"

      system kandelo_configure,
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--disable-harden-flags",
        "--disable-kill",
        "--disable-nls",
        "--disable-numa",
        "--disable-pidof",
        "--disable-pidwait",
        "--disable-shared",
        "--disable-w",
        "--enable-static",
        "--without-elogind",
        "--without-ncurses",
        "--without-systemd"
      system "make", "-j#{ENV.make_jobs}", "src/ps/pscommand"

      ps = buildpath/"src/ps/pscommand"
      kandelo_validate_wasm_artifact(
        ps,
        fork:            :forbidden,
        forbidden_paths: [buildpath.to_s, prefix.to_s],
      )
      kandelo_install_bin(buildpath/"src/ps", "pscommand", "ps")
    end

    man1.install "man/ps.1"
  end

  test do
    assert_path_exists bin/"ps"
    assert_path_exists man1/"ps.1"

    version_pattern = /from procps-ng #{Regexp.escape(version.to_s)}$/
    assert_match version_pattern, kandelo_run_wasm(bin/"ps", ["--version"])
    assert_match version_pattern, kandelo_run_browser_wasm(bin/"ps", ["--version"])

    process_args = ["-p", "1", "-o", "pid=,ppid=,nice=,comm="]
    process = "    1     0   0 init\n"
    assert_equal process, kandelo_run_wasm(bin/"ps", process_args)
    assert_equal process, kandelo_run_browser_wasm(bin/"ps", process_args)

    assert_empty kandelo_run_wasm(
      bin/"ps", ["-p", "999999", "-o", "pid="], expected_status: 1
    )

    contents = (bin/"ps").binread
    refute_includes contents, prefix.to_s
    refute_includes contents, "/private/tmp/"
    refute_includes contents, "/Users/"
    refute_includes contents, "/nix/store/"
  end
end
