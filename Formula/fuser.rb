require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Fuser < Formula
  include KandeloFormulaSupport

  desc "Identify processes using open files on Kandelo"
  homepage "https://gitlab.com/psmisc/psmisc"
  url "https://downloads.sourceforge.net/project/psmisc/psmisc/psmisc-23.7.tar.xz"
  sha256 "58c55d9c1402474065adae669511c191de374b0871eec781239ab400b907c327"
  license "GPL-2.0-or-later"

  depends_on "binaryen" => [:build, :test]
  depends_on "wabt" => [:build, :test]

  skip_clean "bin/fuser"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/psmisc-#{version}"
      mapped_roots = {
        buildpath.to_s               => stable_source,
        root.to_s                    => "/usr/src/kandelo",
        Pathname(root).realpath.to_s => "/usr/src/kandelo",
        "/nix/store"                 => "/usr/src/toolchain",
      }
      prefix_maps = mapped_roots.uniq.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end
      ENV["CFLAGS"] = [
        "-O2", "-gline-tables-only", "-fdebug-compilation-dir=#{stable_source}", *prefix_maps
      ].join(" ")

      # psmisc probes terminfo for pstree even when only fuser is built.
      # fuser neither links nor loads terminfo, so skip that unrelated link probe.
      ENV["ac_cv_lib_tinfo_tgetent"] = "yes"
      # musl does not provide rpmatch; psmisc's own y/n fallback is the target path.
      ENV["ac_cv_func_rpmatch"] = "no"

      system kandelo_configure(root), *kandelo_std_configure_args,
        "--disable-nls",
        "--disable-selinux",
        "--disable-apparmor",
        "--disable-harden-flags",
        "--disable-dependency-tracking"
      system "make", "-j#{ENV.make_jobs}", "src/fuser"
      kandelo_validate_wasm_artifact(
        buildpath/"src/fuser",
        fork:            :forbidden,
        forbidden_paths: [buildpath.to_s, prefix.to_s],
      )
    end

    kandelo_install_bin(buildpath/"src", "fuser", "fuser")
    man1.install "doc/fuser.1"
  end

  def caveats
    <<~EOS
      Kandelo currently reports descriptor-backed regular-file ownership.
      Working-directory, executable, process-root, mapped-file, and TCP, UDP,
      or Unix-domain socket ownership requires live procfs metadata that is not
      yet available.
    EOS
  end

  test do
    version_output = kandelo_run_wasm(bin/"fuser", ["--version"], merge_stderr: true)
    assert_match(/fuser \(PSmisc\) 23\.7/, version_output)

    source = testpath/"fuser-held-file.c"
    helper = testpath/"fuser-held-file.wasm"
    source.write <<~'C'
      #define _POSIX_C_SOURCE 200809L

      #include <ctype.h>
      #include <errno.h>
      #include <fcntl.h>
      #include <spawn.h>
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <sys/wait.h>
      #include <unistd.h>

      extern char **environ;

      static int run_fuser(const char *path, char *output, size_t capacity) {
        posix_spawn_file_actions_t actions;
        char *argv[] = { "/bin/fuser", (char *)path, NULL };
        pid_t child;
        int pipefd[2];
        int status;
        size_t used = 0;

        if (pipe(pipefd) != 0) return -1;
        if (posix_spawn_file_actions_init(&actions) != 0) {
          close(pipefd[0]);
          close(pipefd[1]);
          return -2;
        }
        if (posix_spawn_file_actions_adddup2(&actions, pipefd[1], STDOUT_FILENO) != 0 ||
            posix_spawn_file_actions_addclose(&actions, pipefd[0]) != 0 ||
            posix_spawn_file_actions_addclose(&actions, pipefd[1]) != 0 ||
            posix_spawn_file_actions_addopen(
              &actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0
            ) != 0) {
          posix_spawn_file_actions_destroy(&actions);
          close(pipefd[0]);
          close(pipefd[1]);
          return -3;
        }

        status = posix_spawn(&child, "/bin/fuser", &actions, NULL, argv, environ);
        posix_spawn_file_actions_destroy(&actions);
        close(pipefd[1]);
        if (status != 0) {
          fprintf(stderr, "posix_spawn(/bin/fuser): %s\n", strerror(status));
          close(pipefd[0]);
          return -4;
        }

        while (used + 1 < capacity) {
          ssize_t count = read(pipefd[0], output + used, capacity - used - 1);
          if (count == 0) break;
          if (count < 0) {
            if (errno == EINTR) continue;
            close(pipefd[0]);
            return -5;
          }
          used += (size_t)count;
        }
        close(pipefd[0]);
        output[used] = '\0';

        if (waitpid(child, &status, 0) != child) return -6;
        if (WIFEXITED(status)) return WEXITSTATUS(status);
        if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
        return -7;
      }

      static int only_whitespace(const char *text) {
        while (*text != '\0') {
          if (!isspace((unsigned char)*text)) return 0;
          text++;
        }
        return 1;
      }

      int main(void) {
        static const char path[] = "/tmp/fuser-held.txt";
        char output[256];
        char *end;
        long found_pid;
        pid_t expected_pid = getpid();
        int fd;
        int status;

        fd = open(path, O_CREAT | O_RDWR | O_TRUNC, 0600);
        if (fd < 0) return 10;
        if (write(fd, "held\n", 5) != 5) return 11;
        if (fcntl(fd, F_SETFD, FD_CLOEXEC) != 0) return 12;

        status = run_fuser(path, output, sizeof(output));
        if (status != 0) return 20;
        errno = 0;
        found_pid = strtol(output, &end, 10);
        if (errno != 0 || end == output || found_pid != (long)expected_pid ||
            !only_whitespace(end)) return 21;

        if (close(fd) != 0) return 30;
        status = run_fuser(path, output, sizeof(output));
        if (status != 1 || !only_whitespace(output)) return 31;
        if (unlink(path) != 0) return 32;

        printf("held-pid=%ld\n", found_pid);
        printf("released-status=%d\n", status);
        return 0;
      }
    C

    kandelo_wasm_build do |root|
      system kandelo_cc(root), "-O2", source, "-o", helper
      kandelo_validate_wasm_artifact(helper, fork: :forbidden)
    end

    node_output = kandelo_run_wasm(
      helper,
      [],
      exec_programs:                     { "/bin/fuser" => bin/"fuser" },
      expected_fork_descendant_statuses: [0, 1],
    )
    assert_match(/\Aheld-pid=[1-9][0-9]*\nreleased-status=1\n\z/, node_output)

    browser_output = kandelo_run_browser_wasm(
      helper,
      [],
      argv0:         "fuser-held-file",
      exec_programs: { "/bin/fuser" => bin/"fuser" },
      timeout_ms:    120_000,
    )
    assert_match(/\Aheld-pid=[1-9][0-9]*\nreleased-status=1\n\z/, browser_output)
  end
end
