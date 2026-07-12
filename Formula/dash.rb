require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Dash < Formula
  include KandeloFormulaSupport

  desc "POSIX-compliant shell for Kandelo"
  homepage "http://gondor.apana.org.au/~herbert/dash/"
  url "https://cdn.netbsd.org/pub/pkgsrc/distfiles/dash-0.5.12.tar.gz"
  mirror "https://deb.debian.org/debian/pool/main/d/dash/dash_0.5.12.orig.tar.gz"
  mirror "http://gondor.apana.org.au/~herbert/dash/files/dash-0.5.12.tar.gz"
  sha256 "6a474ac46e8b0b32916c4c60df694c82058d3297d8b385b74508030ca4a8f28a"
  license all_of: ["BSD-3-Clause", "GPL-2.0-or-later"]

  skip_clean "bin/dash"
  patch :DATA

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      # mksignames runs on the build host, but its output is target data.
      ENV["CPPFLAGS_FOR_BUILD"] = "-DKANDELO_TARGET_SIGNALS"

      system kandelo_configure, *kandelo_std_configure_args,
        "--enable-static",
        "--with-libedit=no"
      system "make"

      dash = buildpath/"src/dash"
      instrumented = buildpath/"src/dash.instrumented"
      system "#{root}/scripts/run-wasm-fork-instrument.sh", dash, "-o", instrumented
      mv instrumented, dash
    end

    kandelo_install_bin(buildpath/"src", "dash", "dash")
  end

  test do
    parse_script = <<~'SH'
      set -eu
      phrase='two words'
      total=0
      for value in 2 3 4; do
        total=$((total + value))
      done
      choose() {
        if [ "$1" = yes ]; then
          printf then
        else
          printf else
        fi
      }
      nested=$(printf '%s' "$phrase")
      printf 'parse=%s total=%s branch=%s\n' "$nested" "$total" "$(choose yes)"
    SH
    assert_equal "parse=two words total=9 branch=then\n",
      kandelo_run_wasm(bin/"dash", ["-c", parse_script])

    exec_script = <<~'SH'
      set -u
      child_output=$("$0" -c 'printf "child=%s\n" "$1"; exit 7' dash 'a b')
      child_status=$?
      printf '%s\nparent=%s\n' "$child_output" "$child_status"
      [ "$child_status" -eq 7 ]
    SH
    assert_equal "child=a b\nparent=7\n",
      kandelo_run_wasm(bin/"dash", ["-c", exec_script])

    assert_equal "USR1\n", kandelo_run_wasm(bin/"dash", ["-c", "kill -l 10"])
    signal_script = <<~'SH'
      trap 'printf "got=10\n"' 10
      kill -USR1 $$
      printf 'done\n'
    SH
    assert_equal "got=10\ndone\n", kandelo_run_wasm(bin/"dash", ["-c", signal_script])
    assert_equal "STKFLT\n", kandelo_run_wasm(bin/"dash", ["-c", "kill -l 16"])
    assert_equal "IO\n", kandelo_run_wasm(bin/"dash", ["-c", "kill -l 29"])
    assert_equal "34\nRTMIN\nRTMAX\n",
      kandelo_run_wasm(bin/"dash", ["-c", "kill -l 34; kill -l 35; kill -l 64"])
  end
end

__END__
diff --git a/src/mksignames.c b/src/mksignames.c
index a832eab..d644721 100644
--- a/src/mksignames.c
+++ b/src/mksignames.c
@@ -21,9 +21,110 @@

 #include <stdio.h>
 #include <sys/types.h>
+#ifndef KANDELO_TARGET_SIGNALS
 #include <signal.h>
+#endif
 #include <stdlib.h>

+#ifdef KANDELO_TARGET_SIGNALS
+/* mksignames is a build-host tool that emits target data. */
+# undef SIGLOST
+# undef SIGMSG
+# undef SIGDANGER
+# undef SIGMIGRATE
+# undef SIGPRE
+# undef SIGVIRT
+# undef SIGALRM1
+# undef SIGWAITING
+# undef SIGGRANT
+# undef SIGKAP
+# undef SIGRETRACT
+# undef SIGSOUND
+# undef SIGSAK
+# undef SIGLWP
+# undef SIGFREEZE
+# undef SIGTHAW
+# undef SIGCANCEL
+# undef SIGDIL
+# undef SIGCLD
+# undef SIGWINDOW
+# undef SIGEMT
+# undef SIGINFO
+# undef SIGKILLTHR
+# undef NSIG
+# define NSIG 65
+# undef SIGRTMIN
+# define SIGRTMIN 35
+# undef SIGRTMAX
+# define SIGRTMAX 64
+# undef SIGHUP
+# define SIGHUP 1
+# undef SIGINT
+# define SIGINT 2
+# undef SIGQUIT
+# define SIGQUIT 3
+# undef SIGILL
+# define SIGILL 4
+# undef SIGTRAP
+# define SIGTRAP 5
+# undef SIGABRT
+# define SIGABRT 6
+# undef SIGIOT
+# define SIGIOT SIGABRT
+# undef SIGBUS
+# define SIGBUS 7
+# undef SIGFPE
+# define SIGFPE 8
+# undef SIGKILL
+# define SIGKILL 9
+# undef SIGUSR1
+# define SIGUSR1 10
+# undef SIGSEGV
+# define SIGSEGV 11
+# undef SIGUSR2
+# define SIGUSR2 12
+# undef SIGPIPE
+# define SIGPIPE 13
+# undef SIGALRM
+# define SIGALRM 14
+# undef SIGTERM
+# define SIGTERM 15
+# undef SIGSTKFLT
+# define SIGSTKFLT 16
+# undef SIGCHLD
+# define SIGCHLD 17
+# undef SIGCONT
+# define SIGCONT 18
+# undef SIGSTOP
+# define SIGSTOP 19
+# undef SIGTSTP
+# define SIGTSTP 20
+# undef SIGTTIN
+# define SIGTTIN 21
+# undef SIGTTOU
+# define SIGTTOU 22
+# undef SIGURG
+# define SIGURG 23
+# undef SIGXCPU
+# define SIGXCPU 24
+# undef SIGXFSZ
+# define SIGXFSZ 25
+# undef SIGVTALRM
+# define SIGVTALRM 26
+# undef SIGPROF
+# define SIGPROF 27
+# undef SIGWINCH
+# define SIGWINCH 28
+# undef SIGIO
+# define SIGIO 29
+# undef SIGPOLL
+# define SIGPOLL SIGIO
+# undef SIGPWR
+# define SIGPWR 30
+# undef SIGSYS
+# define SIGSYS 31
+#endif
+
 #if !defined (NSIG)
 #  define NSIG 64
 #endif
@@ -254,6 +355,10 @@ initialize_signames ()
   signal_names[SIGFPE] = "FPE";
 #endif

+#if defined (SIGSTKFLT) /* stack fault on coprocessor */
+  signal_names[SIGSTKFLT] = "STKFLT";
+#endif
+
 #if defined (SIGKILL)	/* kill (cannot be caught or ignored) */
   signal_names[SIGKILL] = "KILL";
 #endif
