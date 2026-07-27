require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Msmtpd < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/msmtpd".freeze

  desc "Minimal SMTP server for Kandelo"
  homepage "https://marlam.de/msmtp/"
  url "https://marlam.de/msmtp/releases/msmtp-1.8.32.tar.xz"
  sha256 "20cd58b58dd007acf7b937fa1a1e21f3afb3e9ef5bbcfb8b4f5650deadc64db4"
  license "GPL-3.0-or-later"

  depends_on KandeloFormulaSupport::BinaryenRequirement => [:build, :test]
  depends_on KandeloFormulaSupport::PkgconfRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  # WHY: upstream executes every accepted message's delivery command through
  # POSIX popen(), so a real /bin/sh is part of the runtime contract.
  depends_on "kandelo-dev/tap-core/dash"

  skip_clean "bin/msmtpd"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      ENV["CFLAGS"] = "-O2"

      # WHY: this Formula intentionally ships only the small SMTP listener, not
      # msmtp's outbound client. Disabling client-only integrations keeps the
      # target dependency graph honest while upstream's normal configure and
      # msmtpd target still define the complete server feature surface. The
      # compiled paths name stable guest locations; a Homebrew build-host Cellar
      # path would be unusable after the bottle is poured into Kandelo.
      system kandelo_configure,
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--bindir=/usr/bin",
        "--sysconfdir=/etc",
        "--localedir=/usr/share/locale",
        "--disable-nls",
        "--disable-gai-idn",
        "--with-tls=no",
        "--with-libgsasl=no",
        "--with-libidn=no",
        "--with-libsecret=no",
        "--with-macosx-keyring=no",
        "--with-msmtpd=yes"
      system "make", "-C", "src", "-j#{ENV.make_jobs}", "msmtpd"

      artifact = buildpath/"src/msmtpd"
      kandelo_fork_instrument(artifact)
      kandelo_validate_wasm_artifact(artifact, fork: :required)
    end

    kandelo_install_bin(buildpath/"src", "msmtpd", "msmtpd")
    man1.install "doc/msmtpd.1"
  end

  test do
    artifact = bin/"msmtpd"
    assert_path_exists artifact
    assert_equal 0755, artifact.stat.mode & 0777
    assert_path_exists man1/"msmtpd.1"
    kandelo_validate_wasm_artifact(artifact, fork: :required)
    assert_includes artifact.binread, "/usr/bin/msmtp"

    version_output = kandelo_run_wasm(artifact, ["--version"])
    assert_match(/^msmtpd version 1[.]8[.]32$/, version_output)
    assert_match(/^msmtpd version 1[.]8[.]32$/,
      kandelo_run_browser_wasm(artifact, ["--version"], argv0: "msmtpd"))

    capture_source = testpath/"capture-message.c"
    capture = testpath/"capture-message.wasm"
    capture_source.write <<~C
      #include <stdio.h>
      #include <string.h>

      int main(void) {
        char message[8192];
        size_t used = fread(message, 1, sizeof(message) - 1, stdin);
        if (ferror(stdin) || !feof(stdin)) return 2;
        message[used] = '\\0';
        if (strstr(message, "Received: from ") == NULL) return 3;
        if (strstr(message, "Subject: Kandelo Formula test") == NULL) return 4;
        if (strstr(message, "formula-message-body") == NULL) return 5;
        return 0;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, "-std=c17", "-O2", capture_source, "-o", capture
      kandelo_validate_wasm_artifact(capture, fork: :forbidden)
    end

    smtp_session = <<~SMTP.gsub("\n", "\r\n")
      EHLO localhost
      MAIL FROM:<sender@example.test>
      RCPT TO:<recipient@example.test>
      DATA
      Subject: Kandelo Formula test

      formula-message-body
      .
      QUIT
    SMTP
    smtp_output = kandelo_run_wasm(
      artifact,
      ["--inetd", "--command=/bin/capture-message"],
      stdin:         smtp_session,
      exec_programs: {
        "/bin/capture-message" => capture,
        "/bin/sh"              => formula_opt_bin("kandelo-dev/tap-core/dash")/"dash",
      },
    )
    assert_match(/^220 localhost ESMTP msmtpd\r?$/, smtp_output)
    assert_match(/^250 Ok, mail was piped\r?$/, smtp_output)
    assert_match(/^221 Bye\r?$/, smtp_output)
  end
end
