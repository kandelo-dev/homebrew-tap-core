require_relative "../Kandelo/formula_support/kandelo_formula_support"

class Msmtpd < Formula
  include KandeloFormulaSupport

  desc "Minimal SMTP server for Kandelo"
  homepage "https://marlam.de/msmtp/"
  url "https://marlam.de/msmtp/releases/msmtp-1.8.32.tar.xz"
  sha256 "20cd58b58dd007acf7b937fa1a1e21f3afb3e9ef5bbcfb8b4f5650deadc64db4"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "pkgconf" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/dash" => :test

  skip_clean "bin/msmtpd"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      ENV["CFLAGS"] = "-O2 -gline-tables-only -fdebug-compilation-dir=."
      ENV["ac_cv_func_malloc_0_nonnull"] = "yes"
      ENV["ac_cv_func_realloc_0_nonnull"] = "yes"

      system kandelo_configure, *kandelo_std_configure_args,
        "--bindir=/usr/local/bin",
        "--sysconfdir=/etc",
        "--localedir=/usr/share/locale",
        "--disable-nls",
        "--with-tls=no",
        "--without-libgsasl",
        "--without-libidn",
        "--without-libsecret",
        "--without-macosx-keyring",
        "--with-msmtpd"
      system "make", "-C", "src", "-j#{ENV.make_jobs}", "msmtpd"

      optimized = buildpath/"src/msmtpd.optimized"
      instrumented = buildpath/"src/msmtpd.instrumented"
      system "wasm-opt", "-O2", buildpath/"src/msmtpd", "-o", optimized
      system "#{root}/scripts/run-wasm-fork-instrument.sh",
        optimized, "-o", instrumented

      artifact_guards = "#{root}/scripts/wasm-artifact-guards.sh"
      system "bash", "-c", <<~SH
        set -euo pipefail
        . #{artifact_guards.shellescape}
        expected_abi=$(wasm_current_abi_version #{root.to_s.shellescape})
        artifact_abi=$(wasm_extract_abi_version #{instrumented.to_s.shellescape})
        if [ -z "$expected_abi" ] || [ "$artifact_abi" != "$expected_abi" ]; then
          echo "ERROR: msmtpd ABI $artifact_abi does not match Kandelo ABI $expected_abi" >&2
          exit 1
        fi
        wasm_require_no_legacy_asyncify #{instrumented.to_s.shellescape}
        wasm_require_fork_instrumentation_if_needed #{instrumented.to_s.shellescape}
        if ! wasm_has_complete_fork_instrumentation #{instrumented.to_s.shellescape}; then
          echo "ERROR: msmtpd has incomplete fork instrumentation" >&2
          exit 1
        fi
        unexpected_env_imports=$(wasm-objdump -x #{instrumented.to_s.shellescape} |
          awk '/<- env[.]/ { sub(/^.*<- env[.]/, ""); print $1 }' |
          grep -Ev '^(__channel_base|memory)$' || true)
        if [ -n "$unexpected_env_imports" ]; then
          echo "ERROR: msmtpd contains unresolved non-ABI env imports" >&2
          echo "$unexpected_env_imports" >&2
          exit 1
        fi
      SH
    end

    kandelo_install_bin(buildpath/"src", "msmtpd.instrumented", "msmtpd")
    man1.install "doc/msmtpd.1"
    pkgshare.install "COPYING"
  end

  test do
    version_output = kandelo_run_wasm(bin/"msmtpd", ["--version"])
    assert_match(/msmtpd version 1\.8\.32/, version_output)

    help_output = kandelo_run_wasm(bin/"msmtpd", ["--help"])
    assert_includes help_output, "--inetd"
    assert_includes help_output, "--command=cmd"

    # Upstream inetd mode exercises the complete SMTP and delivery path without
    # the virtual network's pending reverse-DNS boundary in daemon mode.
    smtp_input = [
      "EHLO formula.test",
      "MAIL FROM:<sender@formula.test>",
      "RCPT TO:<recipient@formula.test>",
      "DATA",
      "Subject: Kandelo Homebrew",
      "",
      "message delivered through popen",
      ".",
      "QUIT",
      "",
    ].join("\r\n")
    smtp_output = kandelo_run_wasm(
      bin/"msmtpd",
      ["--inetd", "--command=:"],
      stdin:         smtp_input,
      exec_programs: { "/bin/sh" => formula_opt_bin("automattic/kandelo-homebrew/dash")/"dash" },
    )
    assert_includes smtp_output, "220 localhost ESMTP msmtpd\r\n"
    assert_includes smtp_output, "354 Send data\r\n"
    assert_includes smtp_output, "250 Ok, mail was piped\r\n"
    assert_includes smtp_output, "221 Bye\r\n"

    binary = File.binread(bin/"msmtpd")
    refute_includes binary, prefix.to_s
    refute_includes binary, "/nix/store/"
    refute_match %r{/private/tmp/[^/]+/}, binary
    refute_match %r{/Users/[^/]+/}, binary
  end
end
