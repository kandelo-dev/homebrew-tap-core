require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Perl < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/perl".freeze
  PERL_PRIVLIB = "5.40.3".freeze

  desc "Highly capable programming language for Kandelo"
  homepage "https://www.perl.org/"
  url "https://www.cpan.org/src/5.0/perl-5.40.3.tar.gz"
  sha256 "4c155b4e6160682b38919b55ac319081b898db11857cf18a7d9ffed2648ccaff"
  license any_of: ["Artistic-1.0-Perl", "GPL-1.0-or-later"]

  depends_on "binaryen" => :build
  depends_on "gnu-sed" => :build
  depends_on "wabt" => :build

  skip_clean "bin/perl"
  skip_clean "lib/perl5"

  resource "perl-cross" do
    url "https://github.com/arsv/perl-cross/releases/download/1.6.4/perl-cross-1.6.4.tar.gz"
    sha256 "b6202173b0a8a43fb312867d85a8cd33527f3f234b1b6e591cdaa9895c9920c7"
  end

  def install
    kandelo_require_arch!("wasm32")

    # perl-cross replaces Perl's interactive Configure with a real two-phase
    # cross build: native miniperl generators and the Kandelo target runtime.
    chmod_R "u+w", buildpath
    resource("perl-cross").stage do
      cp_r Pathname.pwd.children, buildpath
    end

    patch_dir = Pathname(__dir__).parent/"Kandelo/patches/perl"
    system "patch", "-p1", "-i", patch_dir/"0001-perl-cross-native-probes.patch"
    system "patch", "-p1", "-i", patch_dir/"0002-errno-sysroot-headers.patch"
    system "patch", "-p1", "-i", patch_dir/"0003-perl-cross-stage-static-modules.patch"

    host_env = kandelo_host_tool("env")
    host_make = kandelo_host_tool("make")

    kandelo_wasm_build do |root|
      ENV.prepend_path "PATH", formula_opt_libexec("gnu-sed")/"gnubin"

      # The SDK permits unresolved host imports so libc glue can satisfy them
      # at link time. Perl's static interpreter must therefore name all of its
      # core objects explicitly; otherwise wasm-ld may leave archive members as
      # imports and garbage-collect live interpreter code.
      inreplace "Makefile" do |s|
        s.gsub!(/^perl\$x: LDFLAGS \+= -Wl,-E\n/, "")
        s.gsub!(
          "$(CC) $(LDFLAGS) -o $@ $(filter %$o,$^) $(LIBPERL) $(statars) $(LIBS) $(extlibs)",
          "$(CC) $(LDFLAGS) -Wl,--no-gc-sections -o $@ perlmain$o op$o perl$o " \
          "$(obj) $(dynaloader_o) $(statars) $(LIBS) $(extlibs)",
        )
      end

      configure_args = [
        "--target=wasm32-unknown-none",
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--host-cc=cc",
        "--host-ar=ar",
        "--host-ranlib=ranlib",
        "-Dcc=#{kandelo_arch}posix-cc",
        "-Dld=#{kandelo_arch}posix-cc",
        "-Dar=#{kandelo_arch}posix-ar",
        "-Dranlib=#{kandelo_arch}posix-ranlib",
        "-Dnm=#{kandelo_arch}posix-nm",
        "-Doptimize=-O2",
        "-Dosname=linux",
        "-Dccflags=-D_GNU_SOURCE -DNO_ENV_ARRAY_IN_MAIN -fvisibility=default " \
        "-fno-strict-aliasing -ffile-prefix-map=#{buildpath}=.",
        "-Dldflags=",
        "-Dlddlflags=",
        "-Dccdlflags=",
        "-Dlibs=",
        "-Dperllibs=",
        "-Uusethreads",
        "-Uuseithreads",
        "-Uusemultiplicity",
        "-Uuselargefiles",
        "-Duse64bitint",
        "-Duseperlio",
        "-Uusedl",
        "-Dcharsize=1",
        "-Dshortsize=2",
        "-Dintsize=4",
        "-Dlongsize=4",
        "-Dlonglongsize=8",
        "-Dptrsize=4",
        "-Ddoublesize=8",
        "-Dlongdblsize=8",
        "-Di16size=2",
        "-Di32size=4",
        "-Di64size=8",
        "-Duvsize=4",
        "-Divsize=4",
        "-Dnvsize=8",
        "-Dsizesize=4",
        "-Dfpossize=8",
        "-Dlseeksize=8",
        "-Duidsize=4",
        "-Dgidsize=4",
        "-Dtimesize=8",
        "-Dssizetype=int",
        "-Dsizetype=size_t",
        "-Dbyteorder=1234",

        # musl returns a positional LC_ALL composite. Perl-cross defaults to
        # glibc's name=value notation, which otherwise aborts at startup.
        "-Ud_perl_lc_all_uses_name_value_pairs",
        "-Dd_perl_lc_all_separator=define",
        '-Dperl_lc_all_separator=";"',
        "-Dd_perl_lc_all_category_positions_init=define",
        "-Dperl_lc_all_category_positions_init={ LC_CTYPE, LC_NUMERIC, LC_TIME, " \
        "LC_COLLATE, LC_MONETARY, LC_MESSAGES }",

        "-Dd_fork=define",
        "-Dd_vfork=undef",
        "-Dd_pseudofork=undef",
        "-Dd_exec=define",
        "-Dd_waitpid=define",
        "-Dd_wait4=undef",
        "-Dd_getpid_proto=define",
        "-Dd_getppid=define",
        "-Dd_getpgrp=define",
        "-Dd_setpgid=define",
        "-Dd_setsid=define",
        "-Dd_getuid=define",
        "-Dd_geteuid=define",
        "-Dd_getgid=define",
        "-Dd_getegid=define",
        "-Dd_kill=define",
        "-Dd_killpg=define",
        "-Dd_alarm=define",
        "-Dd_setitimer=define",
        "-Dd_getitimer=define",
        "-Dd_sigaction=define",
        "-Dd_sigprocmask=define",
        "-Dd_sigfillset=define",
        "-Dd_nanosleep=define",
        "-Dd_usleep=define",
        "-Dd_usleepproto=define",
        "-Dd_clock_gettime=define",
        "-Dd_socket=define",
        "-Dd_oldsock=undef",
        "-Dd_sockpair=define",
        "-Dd_bind=define",
        "-Dd_listen=define",
        "-Dd_accept=define",
        "-Dd_connect=define",
        "-Dd_shutdown=define",
        "-Dd_getsockopt=define",
        "-Dd_setsockopt=define",
        "-Dd_recvmsg=define",
        "-Dd_sendmsg=define",
        "-Dd_getsockname=define",
        "-Dd_getpeername=define",
        "-Dd_gethostname=define",
        "-Dd_gethostbyname=define",
        "-Dd_getaddrinfo=define",
        "-Dd_getnameinfo=define",
        "-Dd_inetpton=define",
        "-Dd_inetntop=define",
        "-Dd_inet_aton=define",
        "-Dd_htonl=define",
        "-Dd_open3=define",
        "-Dd_fcntl=define",
        "-Dd_flock=define",
        "-Dd_lockf=undef",
        "-Dd_dup2=define",
        "-Dd_dup3=define",
        "-Dd_pipe=define",
        "-Dd_pipe2=define",
        "-Dd_select=define",
        "-Dd_poll=define",
        "-Dd_stat=define",
        "-Dd_fstat=define",
        "-Dd_lstat=define",
        "-Dd_fstatat=define",
        "-Dd_truncate=define",
        "-Dd_ftruncate=define",
        "-Dd_access=define",
        "-Dd_faccessat=define",
        "-Dd_umask=define",
        "-Dd_link=define",
        "-Dd_symlink=define",
        "-Dd_readlink=define",
        "-Dd_rename=define",
        "-Dd_unlink=define",
        "-Dd_mkdir=define",
        "-Dd_rmdir=define",
        "-Dd_chdir=define",
        "-Dd_fchdir=define",
        "-Dd_mkfifo=define",
        "-Dd_getcwd=define",
        "-Dd_mmap=define",
        "-Dd_munmap=define",
        "-Dd_utimensat=define",
        "-Dd_futimens=define",
        "-Dd_dlopen=undef",
        "-Dd_dlerror=undef",
        "-Dd_dlsym=undef",
        "-Dd_dlclose=undef",
        "-Dd_libm_lib_version=undef",
        "-Dd_mprotect=undef",
        "-Dd_mremap=undef",
        "-Dd_madvise=undef",
        "-Dd_getrlimit=undef",
        "-Dd_setrlimit=undef",
        "-Dd_eaccess=undef",
        "-Dd_setlinebuf=undef",
        "-Dd_statvfs=undef",
        "-Dd_fstatvfs=undef",
        "-Dd_getpwent=undef",
        "-Dd_getpwnam=undef",
        "-Dd_getpwuid=undef",
        "-Dd_getpwnam_r=undef",
        "-Dd_getpwuid_r=undef",
        "-Dd_endpwent=undef",
        "-Dd_setpwent=undef",
        "-Dd_getgrent=undef",
        "-Dd_getgrnam=undef",
        "-Dd_getgrgid=undef",
        "-Dd_getgrnam_r=undef",
        "-Dd_getgrgid_r=undef",
        "-Dd_endgrent=undef",
        "-Dd_setgrent=undef",
        "-Dd_getspnam=undef",
        "-Dd_getspnam_r=undef",
        "-Dd_getlogin=undef",
        "-Dd_getlogin_r=undef",
        "-Dd_chown=undef",
        "-Dd_fchown=undef",
        "-Dd_lchown=undef",
        "-Dd_chroot=undef",
        "-Dd_sethostname=undef",
        "-Dd_setuid=undef",
        "-Dd_seteuid=undef",
        "-Dd_setreuid=undef",
        "-Dd_setresuid=undef",
        "-Dd_setgid=undef",
        "-Dd_setegid=undef",
        "-Dd_setregid=undef",
        "-Dd_setresgid=undef",
        "-Dd_getrusage=undef",
        "-Dd_nice=undef",
        "-Dd_getpriority=undef",
        "-Dd_setpriority=undef",
        "-Dd_tcgetpgrp=undef",
        "-Dd_tcsetpgrp=undef",
        "-Dd_syslog=undef",
        "-Dd_shm=undef",
        "-Dd_shmget=undef",
        "-Dd_shmctl=undef",
        "-Dd_shmat=undef",
        "-Dd_shmdt=undef",
        "-Dd_sem=undef",
        "-Dd_semget=undef",
        "-Dd_semctl=undef",
        "-Dd_semop=undef",
        "-Dd_msg=undef",
        "-Dd_msgget=undef",
        "-Dd_msgctl=undef",
        "-Dd_msgsnd=undef",
        "-Dd_msgrcv=undef",
        "-Dd_crypt=undef",
        "-Dd_times=undef",
        "-Dd_system=undef",

        # Build core XS statically because Kandelo does not provide Wasm DSO
        # loading. Disable only extensions whose libraries/threads are absent,
        # plus ext/re, which duplicates the core regexp symbols when static.
        "--disable-mod=ext/re,cpan/Compress-Raw-Bzip2,cpan/Compress-Raw-Zlib," \
        "ext/PerlIO-encoding,cpan/Encode,cpan/Sys-Syslog,cpan/I18N-Langinfo,cpan/NDBM_File," \
        "cpan/Unicode-Collate,cpan/Unicode-Normalize,dist/PerlIO-via-QuotedPrint," \
        "dist/threads,dist/threads-shared",
      ]

      # Perl's native miniperl relies on aliasing behavior that Clang's
      # optimizer may otherwise break. Set this inside the isolated host
      # shell so the respawned buildmini configure retains it.
      system host_env, "HOSTCFLAGS=-Wno-format -fno-strict-aliasing", "./configure", *configure_args

      # MakeMaker consumers need stable SDK tool names, not this build's host
      # worktree. Keep the real prefix-map in Makefile.config for compilation,
      # while normalizing the target Config metadata before Config.pm is made.
      inreplace "config.sh" do |s|
        s.gsub!(buildpath.to_s, ".")
        s.gsub!(/^config_argc=.*$/, "config_argc='0'")
        s.gsub!(/^config_args=.*$/, "config_args='Kandelo wasm32 cross build'")
      end

      # perl-cross can leave empty target feature variables as `# NAME`, which
      # is not a valid preprocessor directive. Convert only those generated,
      # uppercase placeholders to ordinary defines in the native xconfig.
      system "gsed", "-i", "-E", "s/^# ([A-Z][A-Z0-9_]+)([[:space:]])/#define \\1\\2/", "xconfig.h"

      system host_make, "-j#{ENV.make_jobs}", "all"

      %w[XSLoader.pm Config.pm File/Spec.pm File/Spec/Unix.pm Cwd.pm Errno.pm].each do |runtime_file|
        odie "Perl runtime file was not generated: #{runtime_file}" unless (buildpath/"lib"/runtime_file).exist?
      end

      optimized = buildpath/"perl.optimized"
      instrumented = buildpath/"perl.instrumented"
      system "wasm-opt", "-O2", "perl", "-o", optimized
      system "#{root}/scripts/run-wasm-fork-instrument.sh", optimized, "-o", instrumented
      kandelo_validate_wasm_artifact(instrumented, fork: :required)

      bin.install instrumented => "perl"
      chmod 0755, bin/"perl"

      runtime = lib/"perl5"/PERL_PRIVLIB
      rm buildpath.glob("lib/**/*.{bak,orig}")
      # This generator manifest contains only its wall-clock start time. Perl
      # does not read it at runtime; the generated Unicode tables remain.
      rm buildpath/"lib/unicore/mktables.lst"
      runtime.install buildpath.glob("lib/*")
    end
  end

  test do
    perl5lib = (lib/"perl5"/PERL_PRIVLIB).to_s
    env = { "PERL5LIB" => perl5lib, "HOME" => testpath.to_s, "TMPDIR" => testpath.to_s }

    program = <<~PERL
      use strict;
      use warnings;
      use Config;
      use Errno qw(ENOENT EACCES);
      use File::Spec;
      use POSIX qw(floor);
      use List::Util qw(sum);
      my $path = File::Spec->catfile("a", "b", "c.txt");
      die "path" unless $path eq "a/b/c.txt";
      die "version" unless $Config{version} eq "5.40.3";
      die "errno" unless ENOENT == 2 && EACCES == 13;
      die "xs" unless floor(3.7) == 3 && sum(1, 2, 3, 4) == 10;
      pipe(my $reader, my $writer) or die "pipe: $!";
      my $pid = fork();
      die "fork: $!" unless defined $pid;
      if ($pid == 0) {
        close $reader;
        print {$writer} "child-ok\n";
        close $writer;
        exit 0;
      }
      close $writer;
      my $child = <$reader>;
      close $reader;
      waitpid($pid, 0);
      die "child" unless $? == 0 && $child eq "child-ok\n";
      print "perl-ok $path $child";
    PERL
    assert_equal "perl-ok a/b/c.txt child-ok\n", kandelo_run_wasm(bin/"perl", ["-e", program], env: env)

    # The default locale exercises musl's positional LC_ALL representation.
    assert_equal "locale-ok\n", kandelo_run_wasm(
      bin/"perl", ["-e", 'print "locale-ok\\n"'], env: env.except("LC_ALL", "LANG")
    )
  end
end
