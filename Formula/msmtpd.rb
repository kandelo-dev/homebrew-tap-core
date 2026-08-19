require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Msmtpd < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX =
    "#{KandeloFormulaSupport::KANDELO_GUEST_HOMEBREW_PREFIX}/opt/msmtpd".freeze

  desc "Minimal SMTP server for Kandelo"
  homepage "https://marlam.de/msmtp/"
  url "https://marlam.de/msmtp/releases/msmtp-1.8.32.tar.xz"
  sha256 "20cd58b58dd007acf7b937fa1a1e21f3afb3e9ef5bbcfb8b4f5650deadc64db4"
  license "GPL-3.0-or-later"
  revision 1

  depends_on KandeloFormulaSupport::BinaryenRequirement => [:build, :test]
  depends_on KandeloFormulaSupport::PkgconfRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  # WHY: upstream executes every accepted message's delivery command through
  # POSIX popen(), so a real /bin/sh is part of the runtime contract.
  depends_on "kandelo-dev/tap-core/dash"

  skip_clean "bin/msmtpd"

  # Upstream reaps only the PID attached to one delivered SIGCHLD. Standard
  # signals can coalesce, so a burst can leave exited children counted as
  # active forever and make the listener stop accepting at its 16-session
  # limit. This is the accepted upstream fix after the latest 1.8.33 release:
  # https://github.com/marlam/msmtp/commit/3b21f759a64a25922c9f4488166fe05206752c6e
  patch :DATA

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

    standalone_helper_source = testpath/"msmtpd-standalone-helper.c"
    standalone_helper = testpath/"msmtpd-standalone-helper.wasm"
    standalone_helper_source.write <<~'C'
      #define _POSIX_C_SOURCE 200809L

      #include <arpa/inet.h>
      #include <errno.h>
      #include <fcntl.h>
      #include <netinet/in.h>
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <sys/socket.h>
      #include <sys/stat.h>
      #include <time.h>
      #include <unistd.h>

      static const char *receipt_path = "/tmp/msmtpd-standalone.receipt";
      static const char receipt[] =
          "sender=sender@example.test\n"
          "recipient=recipient@example.test\n"
          "subject=Kandelo standalone test\n"
          "body=standalone-message-body\n";

      static int write_all(int fd, const void *bytes, size_t length) {
        const unsigned char *cursor = bytes;
        while (length > 0) {
          ssize_t count = write(fd, cursor, length);
          if (count < 0 && errno == EINTR) continue;
          if (count <= 0) return -1;
          cursor += (size_t)count;
          length -= (size_t)count;
        }
        return 0;
      }

      static int send_all(int fd, const char *bytes) {
        size_t length = strlen(bytes);
        const char *cursor = bytes;
        while (length > 0) {
          ssize_t count = send(fd, cursor, length, 0);
          if (count < 0 && errno == EINTR) continue;
          if (count <= 0) return -1;
          cursor += (size_t)count;
          length -= (size_t)count;
        }
        return 0;
      }

      static int expect_line(int fd, const char *expected) {
        char line[256];
        size_t used = 0;
        while (used + 1 < sizeof(line)) {
          ssize_t count = recv(fd, line + used, 1, 0);
          if (count < 0 && errno == EINTR) continue;
          if (count <= 0) return -1;
          if (line[used++] == '\n') break;
        }
        line[used] = '\0';
        return strcmp(line, expected) == 0 ? 0 : -1;
      }

      static int connect_smtp(int port) {
        struct sockaddr_in address;
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 10 * 1000 * 1000 };

        memset(&address, 0, sizeof(address));
        address.sin_family = AF_INET;
        address.sin_port = htons((unsigned short)port);
        address.sin_addr.s_addr = htonl(0x7f000001UL);

        for (int attempt = 0; attempt < 500; attempt++) {
          int fd = socket(AF_INET, SOCK_STREAM, 0);
          if (fd < 0) return -1;
          if (connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0) return fd;
          close(fd);
          nanosleep(&pause, NULL);
        }
        return -1;
      }

      static int run_session(int port, int deliver_message) {
        int fd = connect_smtp(port);
        if (fd < 0) return 20;
        if (expect_line(fd, "220 localhost ESMTP msmtpd\r\n") != 0) return 21;
        if (send_all(fd, "EHLO standalone.test\r\n") != 0) return 22;
        if (expect_line(fd, "250 localhost\r\n") != 0) return 23;

        if (deliver_message) {
          if (send_all(fd, "MAIL FROM:<sender@example.test>\r\n") != 0) return 24;
          if (expect_line(fd, "250 Ok\r\n") != 0) return 25;
          if (send_all(fd, "RCPT TO:<recipient@example.test>\r\n") != 0) return 26;
          if (expect_line(fd, "250 Ok\r\n") != 0) return 27;
          if (send_all(fd, "DATA\r\n") != 0) return 28;
          if (expect_line(fd, "354 Send data\r\n") != 0) return 29;
          if (send_all(
              fd,
              "Subject: Kandelo standalone test\r\n"
              "\r\n"
              "standalone-message-body\r\n"
              ".\r\n") != 0) {
            return 30;
          }
          if (expect_line(fd, "250 Ok, mail was piped\r\n") != 0) return 31;
        }

        if (send_all(fd, "QUIT\r\n") != 0) return 32;
        if (expect_line(fd, "221 Bye\r\n") != 0) return 33;
        if (close(fd) != 0) return 34;
        return 0;
      }

      static int client_main(int argc, char **argv) {
        if (argc != 3) return 40;
        int port = atoi(argv[2]);
        if (port < 1 || port > 65535) return 41;

        /*
         * WHY: msmtpd stops accepting once 16 session children are active.
         * Completing a 17th sequential connection proves its SIGCHLD handler
         * waited for earlier children and released their session slots.
         */
        for (int session = 0; session < 17; session++) {
          int status = run_session(port, session == 0);
          if (status != 0) return status;
        }
        puts("msmtpd-client-ok sessions=17");
        return 0;
      }

      static int capture_main(int argc, char **argv) {
        if (argc != 5 ||
            strcmp(argv[2], "sender@example.test") != 0 ||
            strcmp(argv[3], "--") != 0 ||
            strcmp(argv[4], "recipient@example.test") != 0) {
          return 50;
        }

        char message[8192];
        size_t used = 0;
        for (;;) {
          if (used == sizeof(message) - 1) return 51;
          ssize_t count = read(STDIN_FILENO, message + used, sizeof(message) - 1 - used);
          if (count < 0 && errno == EINTR) continue;
          if (count < 0) return 52;
          if (count == 0) break;
          used += (size_t)count;
        }
        message[used] = '\0';
        if (strstr(message, "Received: from standalone.test (") == NULL ||
            strstr(message, "[127.0.0.1]") == NULL ||
            strstr(message, "Subject: Kandelo standalone test") == NULL ||
            strstr(message, "standalone-message-body") == NULL) {
          return 53;
        }

        int fd = open(receipt_path, O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (fd < 0) return 54;
        int result = write_all(fd, receipt, sizeof(receipt) - 1);
        if (close(fd) != 0 || result != 0) return 55;
        return 0;
      }

      static int verify_main(int argc) {
        if (argc != 2) return 60;
        int fd = open(receipt_path, O_RDONLY);
        if (fd < 0) return 61;

        struct stat metadata;
        if (fstat(fd, &metadata) != 0 ||
            !S_ISREG(metadata.st_mode) ||
            (metadata.st_mode & 0777) != 0600 ||
            metadata.st_nlink != 1 ||
            metadata.st_size != (off_t)(sizeof(receipt) - 1)) {
          close(fd);
          return 62;
        }

        char actual[sizeof(receipt)];
        size_t used = 0;
        while (used < sizeof(receipt) - 1) {
          ssize_t count = read(fd, actual + used, sizeof(receipt) - 1 - used);
          if (count < 0 && errno == EINTR) continue;
          if (count <= 0) {
            close(fd);
            return 63;
          }
          used += (size_t)count;
        }
        char extra;
        if (read(fd, &extra, 1) != 0 || close(fd) != 0 ||
            memcmp(actual, receipt, sizeof(receipt) - 1) != 0) {
          return 64;
        }
        if (unlink(receipt_path) != 0) return 65;
        puts("msmtpd-receipt-ok");
        return 0;
      }

      int main(int argc, char **argv) {
        if (argc < 2) return 2;
        if (strcmp(argv[1], "client") == 0) return client_main(argc, argv);
        if (strcmp(argv[1], "capture") == 0) return capture_main(argc, argv);
        if (strcmp(argv[1], "verify") == 0) return verify_main(argc);
        return 3;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, "-std=c17", "-O2", standalone_helper_source, "-o", standalone_helper
      kandelo_validate_wasm_artifact(standalone_helper, fork: :forbidden)
    end

    standalone_script = <<~'SH'
      set -eu
      server=/usr/local/bin/msmtpd
      helper=/usr/local/bin/msmtpd-standalone-helper
      port=25252

      "$server" \
        --interface=127.0.0.1 \
        --port="$port" \
        "--command=$helper capture %F --" &
      server_pid=$!
      cleanup() {
        kill -TERM "$server_pid" 2>/dev/null || :
        wait "$server_pid" 2>/dev/null || :
      }
      trap cleanup EXIT HUP INT TERM

      "$helper" client "$port"
      "$helper" verify
      kill -TERM "$server_pid"
      server_status=0
      wait "$server_pid" 2>/dev/null || server_status=$?
      if [ "$server_status" -ne 143 ]; then
        printf 'unexpected msmtpd shutdown status: %s\n' "$server_status" >&2
        exit 70
      fi
      trap - EXIT HUP INT TERM
      printf 'msmtpd-standalone-%s-ok\n' "$KANDELO_RUNTIME"
    SH
    hosts = testpath/"hosts"
    # WHY: msmtpd reverse-resolves each accepted peer before forking. Formula
    # runners start from an intentionally empty VFS, so provide the normal
    # loopback host identity instead of turning every session into a DNS
    # timeout that says nothing about the standalone service lifecycle.
    hosts.write "127.0.0.1 localhost\n::1 localhost\n"
    dash = formula_opt_bin("kandelo-dev/tap-core/dash")/"dash"
    standalone_programs = {
      "/usr/local/bin/msmtpd"                   => artifact,
      "/usr/local/bin/msmtpd-standalone-helper" => standalone_helper,
    }
    node_standalone_output = kandelo_run_wasm(
      dash,
      ["-c", standalone_script],
      argv0:                             "/bin/sh",
      env:                               { "KANDELO_RUNTIME" => "node", "TIMEOUT" => "180000" },
      exec_programs:                     standalone_programs.merge("/bin/sh" => dash),
      guest_files:                       { "/etc/hosts" => hosts },
      network:                           true,
      merge_stderr:                      true,
      expected_fork_descendant_statuses: [*Array.new(20, 0), 143],
    )
    assert_equal <<~OUTPUT, node_standalone_output
      msmtpd-client-ok sessions=17
      msmtpd-receipt-ok
      msmtpd-standalone-node-ok
    OUTPUT

    browser_standalone_output = kandelo_run_browser_wasm(
      dash,
      ["-c", standalone_script],
      argv0:              "sh",
      guest_program_path: "/bin/sh",
      env:                { "KANDELO_RUNTIME" => "browser" },
      exec_programs:      standalone_programs,
      guest_files:        { "/etc/hosts" => hosts },
      timeout_ms:         180_000,
      merge_stderr:       true,
    )
    assert_equal <<~OUTPUT, browser_standalone_output
      msmtpd-client-ok sessions=17
      msmtpd-receipt-ok
      msmtpd-standalone-browser-ok
    OUTPUT
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core-abi-43/msmtpd"
    sha256 cellar: "/opt/kandelo/homebrew/Cellar", wasm32_kandelo: "76e9ddbe808ee2b7ee86b868e3c5d82704756a2fb20c737967b3a950c4f18fc7"
  end
end

__END__
diff --git a/src/msmtpd.c b/src/msmtpd.c
index 32702a2..092709c 100644
--- a/src/msmtpd.c
+++ b/src/msmtpd.c
@@ -603,9 +603,10 @@
 void sigchld_action(int signum, siginfo_t* si, void* ucontext)
 {
     (void)signum;   /* unused */
+    (void)si;       /* unused */
     (void)ucontext; /* unused */
-    int wstatus;
-    if (waitpid(si->si_pid, &wstatus, 0) == si->si_pid) {
+    int wstatus;
+    while (waitpid(-1, &wstatus, WNOHANG) > 0) {
         int child_exit_status = -1;
         if (WIFEXITED(wstatus))
             child_exit_status = WEXITSTATUS(wstatus);
