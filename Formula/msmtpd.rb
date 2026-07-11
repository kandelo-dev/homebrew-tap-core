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

  skip_clean "bin/msmtp"
  skip_clean "bin/msmtpd"

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/msmtpd".freeze

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      ENV["CFLAGS"] = "-O2 -gline-tables-only -fdebug-compilation-dir=."

      system kandelo_configure, *kandelo_std_configure_args,
        "--bindir=#{GUEST_OPT_PREFIX}/bin",
        "--sysconfdir=/etc",
        "--localedir=/usr/share/locale",
        "--disable-nls",
        "--with-tls=no",
        "--without-libgsasl",
        "--without-libidn",
        "--without-libsecret",
        "--without-macosx-keyring",
        "--with-msmtpd"
      programs = %w[msmtp msmtpd]
      system "make", "-C", "src", "-j#{ENV.make_jobs}", *programs
      artifact_guards = "#{root}/scripts/wasm-artifact-guards.sh"
      programs.each do |program|
        optimized = buildpath/"src/#{program}.optimized"
        instrumented = buildpath/"src/#{program}.instrumented"
        system "wasm-opt", "-O2", buildpath/"src/#{program}", "-o", optimized
        system "#{root}/scripts/run-wasm-fork-instrument.sh",
          optimized, "-o", instrumented

        system "bash", "-c", <<~SH
          set -euo pipefail
          . #{artifact_guards.shellescape}
          expected_abi=$(wasm_current_abi_version #{root.to_s.shellescape})
          artifact_abi=$(wasm_extract_abi_version #{instrumented.to_s.shellescape})
          if [ -z "$expected_abi" ] || [ "$artifact_abi" != "$expected_abi" ]; then
            echo "ERROR: #{program} ABI $artifact_abi does not match Kandelo ABI $expected_abi" >&2
            exit 1
          fi
          wasm_require_no_legacy_asyncify #{instrumented.to_s.shellescape}
          wasm_require_fork_instrumentation_if_needed #{instrumented.to_s.shellescape}
          if ! wasm_has_complete_fork_instrumentation #{instrumented.to_s.shellescape}; then
            echo "ERROR: #{program} has incomplete fork instrumentation" >&2
            exit 1
          fi
          unexpected_env_imports=$(wasm-objdump -x #{instrumented.to_s.shellescape} |
            awk '/<- env[.]/ { sub(/^.*<- env[.]/, ""); print $1 }' |
            grep -Ev '^(__channel_base|memory)$' || true)
          if [ -n "$unexpected_env_imports" ]; then
            echo "ERROR: #{program} contains unresolved non-ABI env imports" >&2
            echo "$unexpected_env_imports" >&2
            exit 1
          fi
        SH
      end
    end

    kandelo_install_bin(buildpath/"src", "msmtp.instrumented", "msmtp")
    kandelo_install_bin(buildpath/"src", "msmtpd.instrumented", "msmtpd")
    man1.install "doc/msmtp.1", "doc/msmtpd.1"
    pkgshare.install "COPYING"
  end

  test do
    version_output = kandelo_run_wasm(bin/"msmtpd", ["--version"])
    assert_match(/msmtpd version 1\.8\.32/, version_output)

    help_output = kandelo_run_wasm(bin/"msmtpd", ["--help"])
    assert_includes help_output, "--inetd"
    assert_includes help_output, "--command=cmd"
    assert_includes help_output, "#{GUEST_OPT_PREFIX}/bin/msmtp -f %F --"

    client_version = kandelo_run_wasm(bin/"msmtp", ["--version"])
    assert_match(/msmtp version 1\.8\.32/, client_version)
    assert_path_exists man1/"msmtp.1"

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
    dash = formula_opt_bin("automattic/kandelo-homebrew/dash")/"dash"
    delivery_command = <<~SH.lines.map(&:strip).join(" ")
      subject_seen=0; body_seen=0;
      while IFS= read -r line; do
        case "$line" in
          "Subject: Kandelo Homebrew"*) subject_seen=1 ;;
          "message delivered through popen"*) body_seen=1 ;;
        esac;
      done;
      [ "$subject_seen" -eq 1 ] && [ "$body_seen" -eq 1 ] #
    SH
    smtp_output = kandelo_run_wasm(
      bin/"msmtpd",
      ["--inetd", "--command=#{delivery_command}"],
      stdin:         smtp_input,
      exec_programs: { "/bin/sh" => dash },
    )
    assert_includes smtp_output, "220 localhost ESMTP msmtpd\r\n"
    assert_includes smtp_output, "354 Send data\r\n"
    assert_includes smtp_output, "250 Ok, mail was piped\r\n"
    assert_includes smtp_output, "221 Bye\r\n"

    rejecting_command = "while IFS= read -r line; do :; done; exit 75 #"
    rejected_output = kandelo_run_wasm(
      bin/"msmtpd",
      ["--inetd", "--command=#{rejecting_command}"],
      stdin:           smtp_input,
      exec_programs:   { "/bin/sh" => dash },
      expected_status: 1,
    )
    assert_includes rejected_output, "451 Pipe command reported error 75\r\n"
    refute_includes rejected_output, "250 Ok, mail was piped\r\n"

    default_output = kandelo_run_wasm(
      bin/"msmtpd",
      ["--inetd"],
      stdin:           smtp_input,
      exec_programs:   {
        "/bin/sh"                       => dash,
        "#{GUEST_OPT_PREFIX}/bin/msmtp" => bin/"msmtp",
      },
      expected_status: 1,
    )
    assert_includes default_output, "554 Pipe command reported error 78\r\n"
    refute_includes default_output, "Cannot start pipe command"

    [bin/"msmtp", bin/"msmtpd"].each do |program|
      binary = File.binread(program)
      refute_includes binary, prefix.to_s
      refute_includes binary, "/nix/store/"
      refute_match %r{/private/tmp/[^/]+/}, binary
      refute_match %r{/Users/[^/]+/}, binary
    end
  end
end
