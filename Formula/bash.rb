require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Bash < Formula
  include KandeloFormulaSupport

  desc "GNU Bourne-Again SHell for Kandelo"
  homepage "https://www.gnu.org/software/bash/"
  url "https://ftpmirror.gnu.org/gnu/bash/bash-5.2.37.tar.gz"
  mirror "https://ftp.gnu.org/gnu/bash/bash-5.2.37.tar.gz"
  sha256 "9599b22ecd1d5787ad7d3b7bf0c59f312b3396d1e281175dd1f8a4014da621ff"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/ncurses"

  skip_clean "bin/bash"

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/bash".freeze

  CLEANUP_WRAPPERS = {
    "pop_stream"                => "pop_stream_w",
    "parser_restore_alias"      => "parser_restore_alias_w",
    "pop_args"                  => "pop_args_w",
    "pop_context"               => "pop_context_w",
    "bashline_reset_event_hook" => "bashline_reset_event_hook_w",
    "merge_temporary_env"       => "merge_temporary_env_w",
    "reset_timeout"             => "reset_timeout_w",
    "reset_locals"              => "reset_locals_w",
    "set_verbose_flag"          => "set_verbose_flag_w",
    "set_history_remembering"   => "set_history_remembering_w",
    "close"                     => "close_w",
    "unlink"                    => "unlink_w",
    "restore_signal_mask"       => "restore_signal_mask_w",
  }.freeze

  def install
    kandelo_require_arch!("wasm32")
    ncurses = formula_opt_prefix("automattic/kandelo-homebrew/ncurses")

    normalize_wasm_cleanup_callbacks

    kandelo_wasm_build do |root|
      ENV["CPPFLAGS"] = "-I#{ncurses}/include"
      ENV["LDFLAGS"] = "-L#{ncurses}/lib"
      ENV["CFLAGS"] = [
        "-O2",
        "-gline-tables-only",
        "-fdebug-compilation-dir=.",
        "-Wno-implicit-function-declaration",
        "-Wno-int-conversion",
        "-Wno-incompatible-pointer-types",
      ].join(" ")

      # These Bash-specific runtime/path probes cannot be inferred by a cross
      # compile. Shared function and type facts remain owned by the SDK or by
      # configure's target link/compile probes.
      {
        "bash_cv_dev_fd"          => "standard",
        "bash_cv_dev_stdin"       => "present",
        "bash_cv_getcwd_malloc"   => "yes",
        "bash_cv_mail_dir"        => "/var/mail",
        "bash_cv_printf_a_format" => "yes",
        "bash_cv_sys_errlist"     => "no",
        "bash_cv_sys_named_pipes" => "present",
        "bash_cv_termcap_lib"     => "libtinfo",
        "bash_cv_unusable_rtsigs" => "no",
      }.each { |key, value| ENV[key] = value }

      # Compile runtime paths against the stable guest opt link; make install
      # overrides only the Cellar destination after the binary is finalized.
      system kandelo_configure, "--prefix=#{GUEST_OPT_PREFIX}",
        "--without-bash-malloc",
        "--enable-readline",
        "--enable-history",
        "--enable-bang-history",
        "--with-curses",
        "--disable-nls",
        "--disable-mem-scramble",
        "--disable-net-redirections",
        "--disable-progcomp"

      config_h = (buildpath/"config.h").read
      {
        '#define DEV_FD_PREFIX "/dev/fd/"' => "target descriptor alias prefix",
        "#define FNMATCH_EQUIV_FALLBACK 0" => "target fnmatch equivalence behavior",
        "#define HAVE_DEV_FD 1"            => "target descriptor aliases",
        "#define HAVE_DEV_STDIN 1"         => "target standard-stream aliases",
        "#define HAVE_MBSNRTOWCS 1"        => "target mbsnrtowcs availability",
        "#define HAVE_ULIMIT 1"            => "target ulimit availability",
        "#define RLIMTYPE rlim_t"          => "target rlim_t type",
      }.each do |definition, description|
        odie "Bash configure misreported #{description}" unless config_h.include?(definition)
      end
      %w[HAVE_MBSCHR HAVE_SETDTABLESIZE HAVE_SETOSTYPE HAVE_SYS_ERRLIST].each do |name|
        odie "Bash configure found absent #{name}" if config_h.include?("#define #{name} 1")
      end
      makefile = (buildpath/"Makefile").read
      odie "Bash configure did not select ncurses libtinfo" unless makefile.match?(/^TERMCAP_LIB = -ltinfo$/)

      # Bash 5.2's recursive make graph races its library Makefiles against the
      # final link. The maintained registry recipe therefore builds serially.
      system "make", "-j1"

      optimized = buildpath/"bash.optimized"
      instrumented = buildpath/"bash.instrumented"
      system "wasm-opt", "-O2", buildpath/"bash", "-o", optimized
      system "#{root}/scripts/run-wasm-fork-instrument.sh", optimized, "-o", instrumented

      artifact_guards = "#{root}/scripts/wasm-artifact-guards.sh"
      system "bash", "-c", <<~SH
        set -euo pipefail
        . #{artifact_guards.shellescape}
        expected_abi=$(wasm_current_abi_version #{root.to_s.shellescape})
        artifact_abi=$(wasm_extract_abi_version #{instrumented.to_s.shellescape})
        if [ -z "$expected_abi" ] || [ "$artifact_abi" != "$expected_abi" ]; then
          echo "ERROR: Bash ABI $artifact_abi does not match Kandelo ABI $expected_abi" >&2
          exit 1
        fi
        wasm_require_no_legacy_asyncify #{instrumented.to_s.shellescape}
        wasm_has_complete_fork_instrumentation #{instrumented.to_s.shellescape}
      SH

      artifact = instrumented.binread
      [buildpath.to_s, prefix.to_s].each do |staging_path|
        odie "Bash artifact embeds staging path #{staging_path}" if artifact.include?(staging_path)
      end
      odie "Bash artifact embeds a host workspace path" if artifact.match?(%r{/(?:Users/|home/runner/work/)})

      mv instrumented, buildpath/"bash"
      system "make", "install", "prefix=#{prefix}", "exec_prefix=#{prefix}"
    end

    inreplace bin/"bashbug", "#!/bin/sh -", "#!#{GUEST_OPT_PREFIX}/bin/bash"
    chmod 0755, bin/"bash"
  end

  test do
    env = {
      "HOME"       => testpath,
      "HISTFILE"   => "/dev/null",
      "KERNEL_CWD" => testpath,
      "TERM"       => "dumb",
    }

    version_output = kandelo_run_wasm(bin/"bash", ["--version"], env: env)
    assert_match(/^GNU bash, version 5\.2\.37/, version_output)
    assert_path_exists bin/"bashbug"
    assert_path_exists man1/"bash.1"
    assert_path_exists man1/"bashbug.1"
    assert_path_exists info/"bash.info"
    assert_path_exists share/"doc/bash/CHANGES"
    bashbug = (bin/"bashbug").read
    assert_equal "#!#{GUEST_OPT_PREFIX}/bin/bash\n", bashbug.lines.first
    assert_includes bashbug, 'VERSTR="GNU bashbug, version ${RELEASE}.${PATCHLEVEL}-${RELSTATUS}"'
    assert_empty kandelo_run_wasm(bin/"bash", ["-n", bin/"bashbug"], env: env)

    source_fixture = testpath/"source-args.sh"
    source_fixture.write <<~'BASH'
      printf 'source=%s\n' "$1"
    BASH
    source_script = <<~'BASH'
      set -- outer
      source ./source-args.sh "two words"
      printf 'after-source=%s\n' "$1"
    BASH
    assert_equal "source=two words\nafter-source=outer\n",
      kandelo_run_wasm(bin/"bash", ["-c", source_script], env: env)

    timed_read_script = <<~'BASH'
      coproc HOLDER { while :; do :; done; }
      holder_pid=$HOLDER_PID
      IFS= read -r -t 0.5 value <&"${HOLDER[0]}"
      status=$?
      kill "$holder_pid"
      wait "$holder_pid" || :
      printf 'read-timeout=%s\n' "$status"
    BASH
    assert_equal "read-timeout=142\n",
      kandelo_run_wasm(bin/"bash", ["-c", timed_read_script], env: env)

    fc_input = <<~'BASH'
      history -c
      printf 'fc-replay\n' >> fc.log
      fc -e : -1
      exit
    BASH
    kandelo_run_wasm(
      bin/"bash", ["--noprofile", "--norc", "-i"], env: env, stdin: fc_input
    )
    assert_equal "fc-replay\nfc-replay\n", (testpath/"fc.log").read

    language_script = <<~'BASH'
      set -u
      declare -A values=([alpha]=17 [beta]=25)
      text=Kandelo42
      [[ $text =~ ^([A-Za-z]+)([0-9]+)$ ]]
      printf 'array=%s regex=%s:%s brace=%s\n' \
        "$((values[alpha] + values[beta]))" \
        "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "$(printf '%s' {a..c})"
    BASH
    assert_equal "array=42 regex=Kandelo:42 brace=abc\n",
      kandelo_run_wasm(bin/"bash", ["-c", language_script], env: env)

    process_script = <<~'BASH'
      pipeline=$(printf 'alpha\nbeta\n' | while IFS= read -r line; do printf '<%s>' "$line"; done)
      substitution=$(printf 'child:%s' "$(printf command)")
      (exit 7)
      subshell_status=$?
      printf 'pipe=%s substitution=%s subshell=%s\n' \
        "$pipeline" "$substitution" "$subshell_status"
    BASH
    assert_equal "pipe=<alpha><beta> substitution=child:command subshell=7\n",
      kandelo_run_wasm(bin/"bash", ["-c", process_script], env: env)

    recursive_script = <<~'BASH'
      child_output=$("$0" -c 'printf "child=%s" "$1"; exit 9' bash 'two words')
      child_status=$?
      printf '%s status=%s\n' "$child_output" "$child_status"
    BASH
    assert_equal "child=two words status=9\n",
      kandelo_run_wasm(bin/"bash", ["-c", recursive_script], env: env)

    process_substitution_script = <<~'BASH'
      IFS= read -r value < <(printf 'fifo-ready\n')
      printf 'process-substitution=%s\n' "$value"
    BASH
    assert_equal "process-substitution=fifo-ready\n",
      kandelo_run_wasm(bin/"bash", ["-c", process_substitution_script], env: env)

    signal_script = <<~'BASH'
      trap 'printf "signal=USR1\n"' USR1
      kill -USR1 $$
      printf 'after-signal\n'
    BASH
    assert_equal "signal=USR1\nafter-signal\n",
      kandelo_run_wasm(bin/"bash", ["-c", signal_script], env: env)
    # Signal names must follow Kandelo's Linux-compatible guest ABI, not the
    # macOS table of the machine that generated Bash's signames header.
    assert_equal "USR1\n", kandelo_run_wasm(bin/"bash", ["-c", "kill -l 10"], env: env)
  end

  private

  def normalize_wasm_cleanup_callbacks
    write_wasm_wrapper_sources

    sources = Dir["*.c", "builtins/*.c", "builtins/*.def"]
    CLEANUP_WRAPPERS.each do |function, wrapper|
      needle = "add_unwind_protect (#{function},"
      sources.each do |source|
        contents = (buildpath/source).read
        next unless contents.include?(needle)

        add_header = contents.exclude?("wasm32_uw.h")

        inreplace source do |s|
          s.gsub!(needle, "add_unwind_protect (#{wrapper},")
          next unless add_header

          header = source.start_with?("builtins/") ? "../wasm32_uw.h" : "wasm32_uw.h"
          s.sub!(/^(#include [<"]config\.h[>"])$/, "\\1\n#include \"#{header}\"")
        end
      end
    end

    inreplace "unwind_prot.c",
      "(*(elt->head.cleanup)) (elt->arg.v);",
      "((void (*)(void *))(elt->head.cleanup)) (elt->arg.v);"

    inreplace "builtins/source.def" do |s|
      s.sub!(
        "add_unwind_protect ((Function *)maybe_pop_dollar_vars, (char *)NULL);",
        "add_unwind_protect (maybe_pop_dollar_vars_w, (char *)NULL);",
      )
      marker = "/* Read and execute commands from the file passed as argument.  Guess what."
      s.sub!(marker, <<~C + marker)
        static void
        maybe_pop_dollar_vars_w (void *unused)
        {
          maybe_pop_dollar_vars ();
        }

      C
    end

    inreplace "builtins/read.def" do |s|
      marker = "void\ncheck_read_timeout ()"
      s.sub!(marker, <<~C + marker)
        static void
        reset_timeout_w (void *unused)
        {
          reset_timeout ();
        }

      C
    end

    inreplace "builtins/fc.def" do |s|
      marker = "/* String to execute on a file that we want to edit. */"
      s.sub!(marker, <<~C + marker)
        static void
        set_verbose_flag_w (void *unused)
        {
          set_verbose_flag ();
        }

      C
    end

    inreplace "builtins/evalstring.c" do |s|
      s.sub!(
        "#include <config.h>",
        "#include <config.h>\n#if defined (HISTORY)\nstatic void set_history_remembering_w (void *unused);\n#endif",
      )
      s.sub!(/\z/, <<~C)

        #if defined (HISTORY)
        static void set_history_remembering_w (void *unused) { set_history_remembering (); }
        #endif
      C
    end

    inreplace "print_cmd.c" do |s|
      s.sub!(
        "static void reset_locals PARAMS((void));",
        "static void reset_locals PARAMS((void));\nstatic void reset_locals_w (void *unused);",
      )
      s.sub!(/\z/, "\nstatic void reset_locals_w (void *unused) { reset_locals (); }\n")
    end

    inreplace "execute_cmd.c" do |s|
      s.sub!(
        "static int\nrestore_signal_mask (set)",
        <<~C.chomp,
          static int restore_signal_mask (sigset_t *set);
          static void restore_signal_mask_w (void *set)
          {
            (void)restore_signal_mask ((sigset_t *)set);
          }

          static int
          restore_signal_mask (set)
        C
      )
    end

    inreplace "Makefile.in", "OBJECTS\t = shell.o", "OBJECTS\t = wasm32_uw.o main_wrapper.o shell.o"
    inreplace "lib/readline/Makefile.in" do |s|
      s.sub!(
        "HISTOBJ = history.o histexpand.o histfile.o histsearch.o shell.o savestring.o \\\n",
        "HISTOBJ = history.o histexpand.o histfile.o histsearch.o savestring.o \\\n",
      )
      s.sub!("\t  xmalloc.o xfree.o compat.o ", "\t  compat.o ")
      s.sub!("libhistory.a: $(HISTOBJ) xmalloc.o xfree.o", "libhistory.a: $(HISTOBJ)")
      s.sub!(
        "$(AR) $(ARFLAGS) $@ $(HISTOBJ) xmalloc.o xfree.o",
        "$(AR) $(ARFLAGS) $@ $(HISTOBJ)",
      )
    end
  end

  def write_wasm_wrapper_sources
    (buildpath/"wasm32_uw.h").write <<~C
      #ifndef BASH_WASM32_UW_H
      #define BASH_WASM32_UW_H
      extern void pop_stream_w (void *unused);
      extern void parser_restore_alias_w (void *unused);
      extern void pop_args_w (void *unused);
      extern void pop_context_w (void *unused);
      extern void bashline_reset_event_hook_w (void *unused);
      extern void merge_temporary_env_w (void *unused);
      extern void close_w (void *fdp);
      extern void unlink_w (void *path);
      #endif
    C

    (buildpath/"wasm32_uw.c").write <<~C
      #include "config.h"
      #include <unistd.h>

      extern void pop_stream (void);
      extern void parser_restore_alias (void);
      extern void pop_args (void);
      extern void pop_context (void);
      extern void bashline_reset_event_hook (void);
      extern void merge_temporary_env (void);

      void pop_stream_w (void *unused) { pop_stream (); }
      void parser_restore_alias_w (void *unused) { parser_restore_alias (); }
      void pop_args_w (void *unused) { pop_args (); }
      void pop_context_w (void *unused) { pop_context (); }
      void bashline_reset_event_hook_w (void *unused) { bashline_reset_event_hook (); }
      void merge_temporary_env_w (void *unused) { merge_temporary_env (); }
      void close_w (void *fdp) { (void)close ((int)(long)fdp); }
      void unlink_w (void *path) { (void)unlink ((const char *)path); }
    C

    (buildpath/"main_wrapper.c").write <<~C
      extern int main(int argc, char **argv, char **envp);
      extern char **environ;

      int __main_argc_argv(int argc, char **argv)
      {
        return main(argc, argv, environ);
      }
    C
  end
end
