require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Mandoc < Formula
  include KandeloFormulaSupport

  GUEST_CELLAR = "/home/linuxbrew/.linuxbrew/Cellar".freeze
  GUEST_MANPATH = "/home/linuxbrew/.linuxbrew/share/man:/usr/share/man".freeze
  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/mandoc".freeze
  GUEST_PAGER = "/home/linuxbrew/.linuxbrew/opt/less/bin/less".freeze

  desc "Manual page formatter and viewer for Kandelo"
  homepage "https://mandoc.bsd.lv/"
  url "https://mandoc.bsd.lv/snapshots/mandoc-1.14.6.tar.gz"
  sha256 "8bf0d570f01e70a6e124884088870cbed7537f36328d512909eb10cd53179d9c"
  license "ISC"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "kandelo-dev/tap-core/less"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/mandoc", "bin/demandoc", "bin/soelim"

  def install
    kandelo_require_arch!("wasm32")
    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")

    kandelo_wasm_build do
      # Upstream treats its exact filename fallback as evidence that a global
      # mandoc.db is stale. Kandelo intentionally does not build that database:
      # doing so at runtime would enumerate and materialize manual pages from
      # every deferred bottle. Keep upstream's deterministic
      # MANPATH/man<section>/<topic>.<section> lookup without printing a
      # misleading warning for this supported no-database layout.
      fallback_warning = [
        "found:",
        "\twarnx(\"outdated mandoc.db lacks %s(%s) entry, run %s %s\",",
        "\t    name, sec, BINM_MAKEWHATIS, paths->paths[ipath]);",
      ].join("\n")
      unless (buildpath/"main.c").read.include?(fallback_warning)
        odie "Mandoc filename-fallback warning changed upstream"
      end
      inreplace buildpath/"main.c", fallback_warning, "found:"

      # Mandoc's custom configure compiles and executes every feature probe.
      # A cross-build must therefore declare the target results explicitly;
      # otherwise successful host probes silently describe macOS or Linux
      # rather than Kandelo.
      target_features = {
        "WFLAG"             => 1,
        "STATIC"            => 0,
        "ATTRIBUTE"         => 1,
        "CMSG"              => 1,
        "DIRENT_NAMLEN"     => 0,
        "EFTYPE"            => 0,
        "ENDIAN"            => 1,
        "ERR"               => 1,
        "FTS"               => 0,
        "FTS_COMPARE_CONST" => 0,
        "GETLINE"           => 1,
        "GETSUBOPT"         => 1,
        "ISBLANK"           => 1,
        "LESS_T"            => 1,
        "MKDTEMP"           => 1,
        "MKSTEMPS"          => 0,
        "NANOSLEEP"         => 1,
        "NTOHL"             => 1,
        "O_DIRECTORY"       => 1,
        "OHASH"             => 0,
        "PATH_MAX"          => 1,
        "PLEDGE"            => 0,
        "PROGNAME"          => 0,
        "REALLOCARRAY"      => 0,
        "RECALLOCARRAY"     => 0,
        "RECVMSG"           => 1,
        "REWB_BSD"          => 0,
        "REWB_SYSV"         => 0,
        "SANDBOX_INIT"      => 0,
        "STRCASESTR"        => 0,
        "STRINGLIST"        => 0,
        "STRLCAT"           => 0,
        "STRLCPY"           => 0,
        "STRNDUP"           => 1,
        "STRPTIME"          => 1,
        "STRSEP"            => 1,
        "STRTONUM"          => 0,
        "SYS_ENDIAN"        => 0,
        "VASPRINTF"         => 1,
        "WCHAR"             => 1,
      }
      configure_lines = [
        "CC=#{ENV.fetch("CC").shellescape}",
        "AR=#{ENV.fetch("AR").shellescape}",
        "CFLAGS=\"-O2 -I#{zlib}/include\"",
        "LDFLAGS=\"-L#{zlib}/lib\"",
        "PREFIX=\"#{prefix}\"",
        "MANDIR=\"#{man}\"",
        "MANPATH_DEFAULT=\"#{GUEST_MANPATH}\"",
        "MANPATH_BASE=\"#{GUEST_MANPATH}\"",
        "READ_ALLOWED_PATH=\"#{GUEST_CELLAR}\"",
        "BINM_PAGER=\"#{GUEST_PAGER}\"",
        "OSENUM=MANDOC_OS_OTHER",
        "OSNAME=\"Kandelo\"",
        "UTF8_LOCALE=C.UTF-8",
        "LN=\"ln -sf\"",
        *target_features.map { |name, value| "HAVE_#{name}=#{value}" },
      ]
      (buildpath/"configure.local").write("#{configure_lines.join("\n")}\n")

      system "./configure"
      system "make", "-j#{ENV.make_jobs}"

      # man(1) starts the user's pager through the normal fork/exec path.
      # Instrument the shared multicall binary before make creates its man,
      # apropos, whatis, and makewhatis links.
      kandelo_fork_instrument(buildpath/"mandoc")

      stage = buildpath/"kandelo-stage"
      system "make", "install", "DESTDIR=#{stage}"
      staged_prefix = stage/prefix.to_s.delete_prefix("/")
      odie "Mandoc did not install into its staged prefix" unless staged_prefix.directory?

      kandelo_validate_wasm_artifact(
        staged_prefix/"bin/mandoc", fork: :required, forbidden_paths: [zlib]
      )
      %w[demandoc soelim].each do |name|
        kandelo_validate_wasm_artifact(
          staged_prefix/"bin"/name, fork: :forbidden, forbidden_paths: [zlib]
        )
      end
      prefix.install staged_prefix.children
    end
  end

  test do
    %w[mandoc man apropos whatis demandoc soelim].each do |name|
      assert_path_exists bin/name
    end
    assert_path_exists sbin/"makewhatis"
    %w[mandoc.1 man.1 apropos.1 whatis.1 demandoc.1 soelim.1].each do |name|
      assert_path_exists man1/name
    end
    assert_path_exists man5/"man.conf.5"
    %w[man.7 mdoc.7 roff.7 eqn.7 tbl.7 mandoc_char.7].each do |name|
      assert_path_exists man7/name
    end
    assert_path_exists man8/"makewhatis.8"

    manual_root = "/manual"
    section_one = testpath/"hello.1"
    section_five = testpath/"hello.5"
    section_one.write <<~ROFF
      .TH HELLO 1 "July 2026" "Kandelo" "General Commands Manual"
      .SH NAME
      hello \\- print a greeting
      .SH DESCRIPTION
      This text came from the section one page.
    ROFF
    section_five.write <<~ROFF
      .TH HELLO 5 "July 2026" "Kandelo" "File Formats Manual"
      .SH NAME
      hello \\- describe the greeting configuration
      .SH DESCRIPTION
      This text came from the section five page.
    ROFF
    manual_files = {
      "#{manual_root}/man1/hello.1" => section_one,
      "#{manual_root}/man5/hello.5" => section_five,
    }
    env = {
      "HOME"       => "/",
      "KERNEL_CWD" => "/",
      "TERM"       => "xterm-256color",
    }

    first = kandelo_run_wasm(
      bin/"man", ["-M", manual_root, "hello"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/man", env: env, guest_files: manual_files
    )
    assert_includes first, "This text came from the section one page."
    refute_includes first, ".TH HELLO"

    fifth = kandelo_run_wasm(
      bin/"man", ["-M", manual_root, "5", "hello"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/man", env: env, guest_files: manual_files
    )
    assert_includes fifth, "This text came from the section five page."
    refute_includes fifth, "This text came from the section one page."

    default_path = kandelo_run_wasm(
      bin/"man", ["1", "hello"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/man", env: env, merge_stderr: true,
      guest_files: { "/home/linuxbrew/.linuxbrew/share/man/man1/hello.1" => section_one }
    )
    assert_includes default_path, "This text came from the section one page."
    refute_includes default_path, "outdated mandoc.db"

    browser = kandelo_run_browser_wasm(
      bin/"man", ["-M", manual_root, "1", "hello"],
      argv0: "man", guest_program_path: "#{GUEST_OPT_PREFIX}/bin/man", env: env,
      guest_files: { "#{manual_root}/man1/hello.1" => section_one },
      merge_stderr: true
    )
    assert_includes browser, "This text came from the section one page."
    refute_includes browser, ".TH HELLO"

    less = formula_opt_bin("kandelo-dev/tap-core/less")/"less"
    transcript = kandelo_run_pty_wasm(
      bin/"man", ["-M", manual_root, "1", "hello"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/man", env: env,
      exec_programs: { GUEST_PAGER => less },
      guest_files: { "#{manual_root}/man1/hello.1" => section_one },
      inputs: ["q"], expected_fork_descendants: 1, initial_delay_ms: 100
    )
    assert_includes transcript, "This text came from the section one page."

    bytes = File.binread(bin/"mandoc")
    assert_includes bytes, GUEST_MANPATH
    assert_includes bytes, GUEST_PAGER
    [buildpath, prefix, zlib].each do |path|
      refute_includes bytes, path.to_s
    end
  end
end
