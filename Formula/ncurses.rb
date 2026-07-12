require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Ncurses < Formula
  include KandeloFormulaSupport

  desc "Text-based user interface library for Kandelo"
  homepage "https://invisible-island.net/ncurses/"
  url "https://ftpmirror.gnu.org/gnu/ncurses/ncurses-6.5.tar.gz"
  sha256 "136d91bc269a9a5785e5f9e980bc76ab57428f604ce3e5a5a90cebc767971cc6"
  license "X11"

  depends_on "pkgconf" => [:build, :test]
  depends_on "automattic/kandelo-homebrew/libcxx"

  skip_clean "bin"
  skip_clean "lib/libformw.a"
  skip_clean "lib/libformw_g.a"
  skip_clean "lib/libmenuw.a"
  skip_clean "lib/libmenuw_g.a"
  skip_clean "lib/libncurses++w.a"
  skip_clean "lib/libncurses++w_g.a"
  skip_clean "lib/libncursesw.a"
  skip_clean "lib/libncursesw_g.a"
  skip_clean "lib/libpanelw.a"
  skip_clean "lib/libpanelw_g.a"
  skip_clean "lib/libtinfow.a"
  skip_clean "lib/libtinfow_g.a"

  def install
    kandelo_require_arch!("wasm32")
    root = Pathname(kandelo_require_root!)
    libcxx = formula_opt_prefix("automattic/kandelo-homebrew/libcxx")
    guest_opt_prefix = "/home/linuxbrew/.linuxbrew/opt/ncurses"

    host_build = buildpath/"host-build"
    host_tic = host_build/"progs/tic"
    host_infocmp = host_build/"progs/infocmp"
    jobs = "-j#{ENV.make_jobs}"

    # tic and infocmp generate architecture-independent terminfo data and the
    # fallback C table. They must run on the build host, never in Kandelo.
    mkdir host_build do
      system "../configure",
        "--without-cxx",
        "--without-cxx-binding",
        "--without-ada",
        "--without-tests",
        "--without-manpages",
        "--with-termlib",
        "--enable-mixed-case",
        "--disable-pc-files"
      system "make", jobs, "-C", "include"
      system "make", jobs, "-C", "ncurses"
      system "make", jobs, "-C", "progs", "tic", "infocmp"
    end

    fallback_names = %w[xterm-256color xterm vt100 dumb]
    terminfo_build = buildpath/"terminfo-build"
    terminfo_build.mkpath
    ENV["TERMINFO"] = terminfo_build
    system host_tic, "-x", "-e", fallback_names.join(","), buildpath/"misc/terminfo.src"
    ENV.delete("TERMINFO")

    archive_builder_paths = []
    kandelo_wasm_build do |sdk_root|
      llvm_bin = ENV["LLVM_BIN"] || ENV["WASM_POSIX_LLVM_DIR"]
      llvm_prefix = Pathname(llvm_bin).parent unless llvm_bin.to_s.empty?
      stable_source = "/usr/src/ncurses-#{version}"
      mapped_roots = {
        buildpath.to_s => stable_source,
        sdk_root.to_s  => "/usr/src/kandelo",
        libcxx.to_s    => "/usr/src/libcxx",
        "/nix/store"   => "/usr/src/toolchain",
      }
      mapped_roots[llvm_prefix.to_s] = "/usr/src/llvm" if llvm_prefix
      prefix_maps = mapped_roots.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end
      debug_flags = ["-fdebug-compilation-dir=#{stable_source}", *prefix_maps].join(" ")
      ENV["CFLAGS"] = "-O2 #{debug_flags}"
      ENV["CXXFLAGS"] = "-O2 -DNDEBUG -fexceptions -nostdinc++ -isystem #{libcxx}/include/c++/v1 #{debug_flags}"
      archive_builder_paths = [buildpath, root, libcxx, llvm_prefix, prefix, opt_prefix].compact.map(&:to_s)

      # ncurses probes LLVM ar with -U first, which preserves build-time member metadata.
      ENV["cf_cv_ar_flags"] = "crD"
      ENV["cf_cv_func_mkstemp"] = "yes"
      ENV["cf_cv_func_nanosleep"] = "yes"
      ENV["cf_cv_link_funcs"] = "link symlink"
      ENV["cf_cv_working_poll"] = "yes"
      ENV["cf_cv_func_poll"] = "yes"
      ENV["cf_cv_posix_saved_ids"] = "yes"

      ENV["ac_cv_sizeof_signed_char"] = "1"
      ENV["ac_cv_sizeof_short"] = "2"
      ENV["ac_cv_sizeof_int"] = "4"
      ENV["ac_cv_sizeof_long"] = "4"
      ENV["ac_cv_sizeof_void_p"] = "4"

      system kandelo_configure, *kandelo_std_configure_args,
        "--without-ada",
        "--without-tests",
        "--with-manpages",
        "--with-termlib",
        "--with-debug",
        "--without-profile",
        "--without-shared",
        "--with-normal",
        "--disable-db-install",
        "--with-default-terminfo-dir=#{guest_opt_prefix}/share/terminfo",
        "--with-terminfo-dirs=#{guest_opt_prefix}/share/terminfo:/usr/share/terminfo",
        "--with-fallbacks=#{fallback_names.join(",")}",
        "--with-tic-path=#{host_tic}",
        "--with-infocmp-path=#{host_infocmp}",
        "--enable-pc-files",
        "--with-pkg-config-libdir=#{lib}/pkgconfig",
        "--disable-stripping",
        "--enable-mixed-case",
        "--enable-sigwinch",
        "--enable-symlinks",
        "--enable-widec",
        "--disable-overwrite"

      # ncurses' Darwin linker probe pairs -static with the host-only -dynamic
      # flag. The Kandelo SDK links static Wasm and clang rejects that pair.
      %w[c++/Makefile ncurses/Makefile progs/Makefile].each do |makefile|
        next unless (buildpath/makefile).read.include?("-dynamic")

        inreplace makefile, /[[:space:]]-dynamic/, ""
      end

      # MKfallback.sh uses GNU sed word boundaries, which Darwin sed leaves
      # unchanged. Keep its generated numeric table aligned with the target's
      # 32-bit NCURSES_INT2 layout when extended colors are enabled.
      inreplace buildpath/"ncurses/tinfo/MKfallback.sh",
        's/\<short\>/NCURSES_INT2/g',
        "s/static short /static NCURSES_INT2 /g"

      fallback_command = [
        "TERMINFO=#{terminfo_build.to_s.shellescape}",
        "bash", "-e", (buildpath/"ncurses/tinfo/MKfallback.sh").to_s.shellescape,
        terminfo_build.to_s.shellescape,
        (buildpath/"misc/terminfo.src").to_s.shellescape,
        host_tic.to_s.shellescape,
        host_infocmp.to_s.shellescape,
        *fallback_names,
        ">", (buildpath/"ncurses/fallback.c").to_s.shellescape
      ].join(" ")
      system "bash", "-c", fallback_command
      fallback_source = buildpath/"ncurses/fallback.c"
      odie "fallback numeric table has the wrong target width" unless
        fallback_source.read.include?("static NCURSES_INT2 xterm_256color_number_data[]")

      system "make", jobs
      system "make", "install"
    end

    verify_archive_paths!(
      archive_builder_paths + ["/nix/store/", "/private/tmp/", "/private/var/", "/Users/"],
      allowed_paths: [guest_opt_prefix],
    )

    # Ship the full terminal database as data while retaining compiled-in
    # fallbacks for the common terminals used before a VFS has that data.
    (share/"terminfo").mkpath
    system host_tic, "-x", "-o", share/"terminfo", buildpath/"misc/terminfo.src"
    terminfo_entries = (share/"terminfo").glob("*/*")
    odie "full terminfo database was not installed" if terminfo_entries.length <= 2_000

    %w[form menu ncurses panel ncurses++].each do |name|
      lib.install_symlink "lib#{name}w.a" => "lib#{name}.a"
      lib.install_symlink "lib#{name}w_g.a" => "lib#{name}_g.a"
    end
    lib.install_symlink "libtinfow.a" => "libtinfo.a"
    lib.install_symlink "libtinfow_g.a" => "libtinfo_g.a"
    lib.install_symlink "libtinfow.a" => "libtermcap.a"
    lib.install_symlink "libtinfow_g.a" => "libtermcap_g.a"
    lib.install_symlink "libncurses.a" => "libcurses.a"
    %w[form menu ncurses panel tinfo ncurses++].each do |name|
      (lib/"pkgconfig").install_symlink "#{name}w.pc" => "#{name}.pc"
    end

    # Match the established ncurses consumer layout: namespaced wide headers,
    # an ncurses compatibility namespace, and top-level header aliases.
    include.install_symlink "ncursesw" => "ncurses"
    (include/"ncursesw").glob("*.h").each do |header|
      next if (include/header.basename).exist?

      include.install_symlink "ncursesw/#{header.basename}" => header.basename
    end

    # The generated script inherits the build host's shell path. Keep this
    # standard ncurses interface, but bind it to Kandelo's guest shell and to
    # Homebrew's stable opt prefix.
    config_script = bin/"ncursesw6-config"
    build_shell = config_script.read.lines.first.delete_prefix("#!").strip
    inreplace config_script do |s|
      s.gsub! build_shell, "/bin/sh"
      s.gsub! prefix.to_s, guest_opt_prefix
    end
    bin.install_symlink "ncursesw6-config" => "ncurses6-config"
  end

  def verify_archive_paths!(forbidden_paths, allowed_paths: [])
    archives = lib.glob("*.a").reject(&:symlink?)
    odie "ncurses installed no static archives" if archives.empty?

    archives.each do |archive|
      contents = File.binread(archive).b
      allowed_paths.each { |path| contents = contents.gsub(path.to_s.b, "".b) }
      forbidden_paths.uniq.each do |path|
        marker = path.to_s.b
        next if marker.empty? || contents.exclude?(marker)

        odie "#{archive.basename} contains builder path #{path}"
      end
    end
  end

  test do
    guest_opt_prefix = "/home/linuxbrew/.linuxbrew/opt/ncurses"
    assert_path_exists lib/"libncursesw.a"
    assert_path_exists lib/"libncursesw_g.a"
    assert_path_exists lib/"libncurses++w.a"
    assert_path_exists lib/"libncurses++w_g.a"
    assert_path_exists lib/"libtinfow.a"
    assert_equal "libtinfow.a", (lib/"libtermcap.a").readlink.to_s
    assert_path_exists include/"curses.h"
    assert_equal "ncursesw/curses.h", (include/"curses.h").readlink.to_s
    assert_path_exists include/"ncursesw/curses.h"
    assert_equal "ncursesw", (include/"ncurses").readlink.to_s
    assert_path_exists lib/"pkgconfig/ncursesw.pc"
    assert_path_exists man3/"ncurses.3x"
    assert_operator (share/"terminfo").glob("*/*").length, :>, 2_000
    config_contents = (bin/"ncursesw6-config").read
    assert_equal "#!/bin/sh\n", config_contents.lines.first
    assert_includes config_contents, %Q(prefix="#{guest_opt_prefix}")
    assert_includes config_contents, 'THIS="ncursesw"'
    assert_includes config_contents, 'TINFO_LIB="tinfow"'
    assert_includes config_contents, 'LIBS="-l${THIS} -l${TINFO_LIB} $LIBS"'
    assert_includes config_contents, %Q(echo "#{guest_opt_prefix}/share/terminfo:/usr/share/terminfo")
    refute_includes config_contents, "/nix/store/"
    refute_includes config_contents, prefix.to_s
    refute_includes config_contents, opt_prefix.to_s if opt_prefix.to_s != guest_opt_prefix
    assert_includes File.binread(lib/"libtinfow.a"), "#{guest_opt_prefix}/share/terminfo"
    refute_includes File.binread(lib/"libtinfow.a"), (prefix/"share/terminfo").to_s
    if opt_prefix.to_s != guest_opt_prefix
      refute_includes File.binread(lib/"libtinfow.a"), (opt_prefix/"share/terminfo").to_s
    end
    verify_archive_paths!(
      [
        HOMEBREW_PREFIX.to_s,
        Pathname(kandelo_require_root!).to_s,
        prefix.to_s,
        opt_prefix.to_s,
        Dir.home,
        "/nix/store/",
        "/private/tmp/",
        "/private/var/",
        "/Users/",
      ],
      allowed_paths: [guest_opt_prefix],
    )
    %w[form menu ncurses panel ncurses++].each do |name|
      assert_equal "lib#{name}w.a", (lib/"lib#{name}.a").readlink.to_s
      assert_equal "lib#{name}w_g.a", (lib/"lib#{name}_g.a").readlink.to_s
    end
    assert_equal "libncurses.a", (lib/"libcurses.a").readlink.to_s
    %w[form menu ncurses panel tinfo ncurses++].each do |name|
      assert_equal "#{name}w.pc", (lib/"pkgconfig/#{name}.pc").readlink.to_s
    end
    %w[captoinfo clear infocmp infotocap reset tabs tic toe tput tset].each do |utility|
      assert_path_exists bin/utility
    end

    source = testpath/"ncurses-smoke.c"
    wasm = testpath/"ncurses-smoke.wasm"
    source.write <<~C
      #include <curses.h>
      #include <form.h>
      #include <menu.h>
      #include <panel.h>
      #include <stdio.h>
      #include <term.h>
      #include <unistd.h>

      int main(void) {
        int status = 0;
        char *clear_sequence;
        FIELD *(*field_factory)(int, int, int, int, int, int) = new_field;
        ITEM *(*item_factory)(const char *, const char *) = new_item;
        PANEL *(*panel_factory)(WINDOW *) = new_panel;

        if (field_factory == NULL || item_factory == NULL || panel_factory == NULL) return 4;
        if (setupterm("xterm-256color", STDOUT_FILENO, &status) != OK || status != 1) return 1;
        if (tigetnum("colors") != 256) return 2;
        clear_sequence = tigetstr("clear");
        if (clear_sequence == NULL || clear_sequence == (char *)-1) return 3;
        puts("ncurses-ok");
        return 0;
      }
    C

    kandelo_wasm_build do
      ENV["PKG_CONFIG_LIBDIR"] = "#{lib}/pkgconfig"
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")
      pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
      flags = shell_output("#{pkgconf} --static --cflags --libs form menu panel ncurses").split
      %w[-lformw -lmenuw -lpanelw -lncursesw -ltinfow].each do |flag|
        assert_includes flags, flag
      end
      system kandelo_cc, source, *flags, "-o", wasm
    end

    namespace_source = testpath/"ncurses-namespace-smoke.c"
    namespace_wasm = testpath/"ncurses-namespace-smoke.wasm"
    namespace_source.write <<~C
      #include <ncurses/curses.h>
      #include <ncursesw/curses.h>

      int main(void) {
        return NCURSES_VERSION_MAJOR == 6 ? 0 : 1;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, namespace_source, "-I#{include}", "-L#{lib}", "-lncursesw", "-ltinfow", "-o", namespace_wasm
    end
    assert_empty kandelo_run_wasm(namespace_wasm, [])

    # Isolate the compiled fallback table from every on-disk search path.
    empty_terminfo = testpath/"empty-terminfo"
    empty_home = testpath/"empty-home"
    empty_terminfo.mkpath
    empty_home.mkpath
    fallback_env = {
      "HOME"          => empty_home,
      "TERM"          => "xterm-256color",
      "TERMINFO"      => empty_terminfo,
      "TERMINFO_DIRS" => empty_terminfo,
    }
    assert_equal "ncurses-ok\n", kandelo_run_wasm(wasm, [], env: fallback_env)
    assert_equal "256\n", kandelo_run_wasm(bin/"tput", ["colors"], env: fallback_env)

    # screen-256color is not compiled into the fallback table. Exercise the
    # full database explicitly; the string checks above verify its guest opt path.
    database_env = {
      "HOME"     => empty_home,
      "TERM"     => "screen-256color",
      "TERMINFO" => share/"terminfo",
    }
    assert_equal "256\n", kandelo_run_wasm(bin/"tput", ["colors"], env: database_env)
    assert_match "ncurses #{version}", kandelo_run_wasm(bin/"infocmp", ["-V"], env: database_env)

    # Run the target tic inside Kandelo and read its output back through the
    # target utilities, proving the shipped compiler is functional.
    generated_terminfo = testpath/"generated-terminfo"
    generated_source = testpath/"kandelo-test.terminfo"
    generated_terminfo.mkpath
    generated_source.write <<~TERMINFO
      kandelo-test|Kandelo target tic test,
          am,
          cols#91,
          lines#37,
          clear=\\E[H\\E[2J,
    TERMINFO
    assert_empty kandelo_run_wasm(
      bin/"tic", ["-x", "-o", generated_terminfo, generated_source], env: { "HOME" => empty_home }
    )
    generated_env = { "HOME" => empty_home, "TERM" => "kandelo-test", "TERMINFO" => generated_terminfo }
    assert_equal "91\n", kandelo_run_wasm(bin/"tput", ["cols"], env: generated_env)
    generated_dump = kandelo_run_wasm(bin/"infocmp", ["kandelo-test"], env: generated_env)
    assert_match "kandelo-test", generated_dump
    assert_match "cols#91", generated_dump

    termcap_source = testpath/"termcap-smoke.c"
    termcap_wasm = testpath/"termcap-smoke.wasm"
    termcap_source.write <<~C
      #include <stdio.h>
      #include <termcap.h>

      int main(void) {
        char buffer[2048];
        if (tgetent(buffer, "dumb") != 1) return 1;
        puts("termcap-ok");
        return 0;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, termcap_source, "-I#{include}", "-L#{lib}", "-ltermcap", "-o", termcap_wasm
    end
    termcap_env = fallback_env.merge("TERM" => "dumb")
    assert_equal "termcap-ok\n", kandelo_run_wasm(termcap_wasm, [], env: termcap_env)

    libcxx = formula_opt_prefix("automattic/kandelo-homebrew/libcxx")
    cxx_source = testpath/"ncurses-cxx-smoke.cpp"
    cxx_wasm = testpath/"ncurses-cxx-smoke.wasm"
    cxx_source.write <<~CPP
      #include <cstdio>
      #include <cursesapp.h>

      class SmokeApplication : public NCursesApplication {
      public:
        SmokeApplication() : NCursesApplication(false) {}

      protected:
        int run() override {
          try {
            throw NCursesException("ncurses-cxx-ok");
          } catch (const NCursesException& error) {
            std::puts(error.message);
          }
          return 0;
        }
      };

      static SmokeApplication application;
    CPP
    kandelo_wasm_build do |root|
      system kandelo_tool("c++", root), cxx_source,
        "-fwasm-exceptions", "-nostdinc++", "-isystem", libcxx/"include/c++/v1",
        "-I#{include}", "-L#{lib}", "-L#{libcxx}/lib",
        "-lncurses++", "-lform", "-lmenu", "-lpanel", "-lncurses", "-ltinfo",
        "-lc++", "-lc++abi", "-o", cxx_wasm
    end
    cxx_output = kandelo_run_wasm(cxx_wasm, [], env: fallback_env.merge("TERM" => "dumb"))
    assert_includes cxx_output, "ncurses-cxx-ok\n"
  end
end
