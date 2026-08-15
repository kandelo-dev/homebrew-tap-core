require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s
require "zlib"

class Mandoc < Formula
  include KandeloFormulaSupport

  GUEST_PREFIX = "/home/linuxbrew/.linuxbrew".freeze
  GUEST_CELLAR = "#{GUEST_PREFIX}/Cellar".freeze
  GUEST_MANPATH = "#{GUEST_PREFIX}/share/man:/usr/share/man".freeze
  GUEST_OPT_PREFIX = "#{GUEST_PREFIX}/opt/mandoc".freeze
  GUEST_PAGER = "#{GUEST_PREFIX}/opt/less/bin/less".freeze

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
    alias_page = testpath/"hello-alias.1"
    compressed_page = testpath/"compressed.1.gz"
    mdoc_page = testpath/"kandelo.7"
    soelim_page = testpath/"soelim-main.roff"
    soelim_include = testpath/"soelim-include.roff"
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
    alias_page.write ".so man1/hello.1\n"
    Zlib::GzipWriter.open(compressed_page) do |gzip|
      gzip.write <<~ROFF
        .TH COMPRESSED 1 "July 2026" "Kandelo" "General Commands Manual"
        .SH NAME
        compressed \\- exercise a compressed bottle manual
        .SH DESCRIPTION
        This text came from the compressed page.
      ROFF
    end
    mdoc_page.write <<~MDOC
      .Dd July 23, 2026
      .Dt KANDELO 7
      .Os Kandelo
      .Sh NAME
      .Nm kandelo
      .Nd run POSIX software in WebAssembly
      .Sh DESCRIPTION
      This text came from the mdoc page.
    MDOC
    soelim_page.write "before include\n.so soelim-include.roff\nafter include\n"
    soelim_include.write "included text\n"
    manual_files = {
      "#{manual_root}/man1/compressed.1.gz" => compressed_page,
      "#{manual_root}/man1/hello-alias.1"   => alias_page,
      "#{manual_root}/man1/hello.1"         => section_one,
      "#{manual_root}/man5/hello.5"         => section_five,
      "#{manual_root}/man7/kandelo.7"       => mdoc_page,
      "#{manual_root}/soelim-include.roff"  => soelim_include,
      "#{manual_root}/soelim-main.roff"     => soelim_page,
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

    assert_equal "#{manual_root}/man1/hello.1\n", kandelo_run_wasm(
      bin/"man", ["-M", manual_root, "-w", "1", "hello"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/man", env: env, guest_files: manual_files
    )

    alias_output = kandelo_run_wasm(
      bin/"man", ["-M", manual_root, "hello-alias"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/man", env: env, guest_files: manual_files
    )
    assert_includes alias_output, "This text came from the section one page."

    compressed_output = kandelo_run_wasm(
      bin/"man", ["-M", manual_root, "compressed"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/man", env: env, guest_files: manual_files
    )
    assert_includes compressed_output, "This text came from the compressed page."

    mdoc_output = kandelo_run_wasm(
      bin/"man", ["-M", manual_root, "7", "kandelo"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/man", env: env, guest_files: manual_files
    )
    assert_includes mdoc_output, "This text came from the mdoc page."

    demandoc_output = kandelo_run_wasm(bin/"demandoc", [], stdin: section_one.read)
    assert_includes demandoc_output, "print a greeting"
    refute_includes demandoc_output, ".TH HELLO"

    soelim_output = kandelo_run_wasm(
      bin/"soelim", ["soelim-main.roff"],
      env:         env.merge("KERNEL_CWD" => manual_root),
      guest_files: manual_files
    )
    assert_equal "before include\nincluded text\nafter include\n", soelim_output

    missing_output = kandelo_run_wasm(
      bin/"man", ["-M", manual_root, "missing"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/man", env: env, merge_stderr: true,
      guest_files: manual_files, expected_status: 5
    )
    assert_match(/No entry for missing in the manual/, missing_output)

    # Homebrew keeps a package's page in its versioned keg and exposes the
    # ordinary global MANPATH entry as a relative symlink. This fixture matches
    # the composed VFS shape: resolving the link is metadata-only, while
    # opening its Cellar target is what materializes the owning bottle.
    homebrew_prefix = testpath/"homebrew-prefix"
    bottled_page = homebrew_prefix/"Cellar/bottled/1.0/share/man/man1/bottled.1"
    bottled_link = homebrew_prefix/"share/man/man1/bottled.1"
    bottled_page.dirname.mkpath
    bottled_link.dirname.mkpath
    bottled_page.write(
      section_one.read
                 .sub("HELLO", "BOTTLED")
                 .sub("hello \\- print a greeting", "bottled \\- read a linked bottle page")
                 .sub("section one page", "linked bottle page"),
    )
    bottled_link.make_symlink("../../../Cellar/bottled/1.0/share/man/man1/bottled.1")
    prefix_mount = { GUEST_PREFIX => homebrew_prefix.realpath }
    default_path = kandelo_run_wasm(
      bin/"man", ["1", "bottled"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/man", env: env, merge_stderr: true,
      writable_host_directories: prefix_mount
    )
    assert_includes default_path, "This text came from the linked bottle page."
    refute_includes default_path, "outdated mandoc.db"

    # Database-backed lookup is optional for viewing but remains a supported
    # user-facing surface for apropos(1), whatis(1), and makewhatis(8).
    assert_empty kandelo_run_wasm(
      sbin/"makewhatis", ["#{GUEST_PREFIX}/share/man"],
      argv0: "#{GUEST_OPT_PREFIX}/sbin/makewhatis", env: env,
      writable_host_directories: prefix_mount
    )
    assert_path_exists homebrew_prefix/"share/man/mandoc.db"
    whatis_output = kandelo_run_wasm(
      bin/"whatis", ["-M", "#{GUEST_PREFIX}/share/man", "bottled"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/whatis", env: env,
      writable_host_directories: prefix_mount
    )
    assert_match(/^bottled\(1\) - read a linked bottle page$/, whatis_output)
    apropos_output = kandelo_run_wasm(
      bin/"apropos", ["-M", "#{GUEST_PREFIX}/share/man", "linked bottle"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/apropos", env: env,
      writable_host_directories: prefix_mount
    )
    assert_match(/^bottled\(1\) - read a linked bottle page$/, apropos_output)

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
