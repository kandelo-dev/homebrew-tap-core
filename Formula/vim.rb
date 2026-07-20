require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Vim < Formula
  include KandeloFormulaSupport

  desc "Vi workalike with additional editing features for Kandelo"
  homepage "https://www.vim.org/"
  # Intentional pinned upgrade from the registry recipe's Vim 9.1.0900 source.
  # The tap validates upstream's 9.2.0750 tag independently rather than
  # inheriting stale registry provenance.
  url "https://github.com/vim/vim/archive/refs/tags/v9.2.0750.tar.gz"
  sha256 "7d460830e12082b541c34b0b96942ebface1ad9fa0b77245930717c0ccf8b664"
  license "Vim"

  depends_on "binaryen" => [:build, :test]
  depends_on "wabt" => :build
  depends_on "kandelo-dev/tap-core/dash" => :test
  depends_on "kandelo-dev/tap-core/ncurses"

  skip_clean "bin"

  def install
    kandelo_require_arch!("wasm32")
    ncurses = formula_opt_prefix("kandelo-dev/tap-core/ncurses")
    guest_brew_prefix = "/home/linuxbrew/.linuxbrew"
    guest_opt_prefix = "#{guest_brew_prefix}/opt/vim"
    guest_ncurses = "#{guest_brew_prefix}/opt/ncurses"

    kandelo_wasm_build do |root|
      llvm_bin = ENV["LLVM_BIN"] || ENV["WASM_POSIX_LLVM_DIR"]
      llvm_prefix = Pathname(llvm_bin).parent unless llvm_bin.to_s.empty?
      stable_source = "/usr/src/vim-#{version}"
      mapped_roots = {
        buildpath.to_s       => stable_source,
        root.to_s            => "/usr/src/kandelo",
        prefix.to_s          => guest_opt_prefix,
        opt_prefix.to_s      => guest_opt_prefix,
        ncurses.to_s         => guest_ncurses,
        HOMEBREW_PREFIX.to_s => guest_brew_prefix,
        "/nix/store"         => "/usr/src/toolchain",
      }
      mapped_roots[llvm_prefix.to_s] = "/usr/src/llvm" if llvm_prefix
      # Clang applies the last matching debug prefix map. Emit generic roots
      # before exact formula paths so target kegs retain their guest opt identity.
      prefix_maps = mapped_roots.sort_by { |from, _| from.length }.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end
      debug_flags = ["-fdebug-compilation-dir=#{stable_source}", *prefix_maps].join(" ")

      ENV["vim_cv_toupper_broken"] = "no"
      ENV["vim_cv_terminfo"] = "yes"
      ENV["vim_cv_tgetent"] = "zero"
      ENV["vim_cv_getcwd_broken"] = "no"
      ENV["vim_cv_stat_ignores_slash"] = "no"
      ENV["vim_cv_memmove_handles_overlap"] = "yes"
      # Cross builds cannot run Vim's SIGEV_THREAD probe. The Formula test
      # below executes the same timer creation path under Kandelo.
      ENV["vim_cv_timer_create_works"] = "yes"
      ENV["vim_cv_uname_output"] = "Kandelo"
      ENV["vim_cv_uname_m_output"] = "wasm32"
      ENV["vim_cv_uname_r_output"] = "1"
      ENV["ac_cv_func_tgetent"] = "yes"

      ENV["CFLAGS"] = "-O2 -gline-tables-only #{debug_flags} -I#{ncurses}/include"
      ENV["LDFLAGS"] = "-L#{ncurses}/lib"
      ENV["LIBS"] = "-lncursesw -ltinfow"

      system kandelo_configure, *kandelo_std_configure_args,
        "--prefix=#{guest_opt_prefix}",
        "--with-features=normal",
        "--with-tlib=tinfow",
        "--with-global-runtime=#{guest_opt_prefix}/share/vim/vim92",
        "--with-compiledby=Kandelo Homebrew",
        "--with-vim-name=vim",
        "--with-modified-by=",
        "--disable-gui",
        "--without-x",
        "--disable-gpm",
        "--disable-sysmouse",
        "--disable-nls",
        "--enable-multibyte",
        "--disable-netbeans",
        "--enable-channel",
        "--enable-terminal",
        "--disable-canberra",
        "--disable-libsodium",
        "--disable-smack",
        "--disable-selinux",
        "--disable-xsmp",
        "--disable-xsmp-interact",
        "--disable-darwin",
        "--disable-icon-cache-update",
        "--disable-desktop-database-update"

      config_h = (buildpath/"src/auto/config.h").read
      %w[HAVE_TIMER_CREATE HAVE_DLOPEN HAVE_DLSYM].each do |macro|
        odie "Vim configure did not select #{macro}" unless config_h.match?(/^#define #{macro} 1$/)
      end

      # Vim's custom link probes accept musl's weak dlfcn stubs, which would
      # advertise +libcall without functional dynamic loading. EXTRA_LIBS is
      # used by Vim's final link but not by the xxd submake.
      ENV["EXTRA_LIBS"] = "-ldl"

      system "make", "-C", "src", "auto/osdef.h"
      osdef = buildpath/"src/auto/osdef.h"
      inreplace osdef, /^extern int.*sigsetjmp.*$/,
        "/* sigsetjmp is a macro in musl */"
      %w[tgetent tgetflag tgetnum tputs tgoto].each do |function|
        inreplace osdef, /^extern .*\b#{function}\(.*$/,
          "/* #{function} is declared by <termcap.h> */"
      end

      # Vim exposes its compilation and link commands in :version. Normalize
      # the generated source before it is compiled so those useful diagnostics
      # retain stable source and guest dependency identities.
      system "make", "-C", "src", "auto/pathdef.c"
      pathdef = buildpath/"src/auto/pathdef.c"
      inreplace pathdef do |s|
        mapped_roots.sort_by { |from, _| -from.length }.each do |from, to|
          s.gsub! from, to
        end
      end
      pathdef_contents = pathdef.binread
      odie "Vim path diagnostics lost the stable source identity" unless
        pathdef_contents.include?(stable_source)
      odie "Vim path diagnostics lost the guest runtime identity" unless
        pathdef_contents.include?(guest_opt_prefix)
      odie "Vim path diagnostics lost the guest ncurses identity" unless
        pathdef_contents.include?(guest_ncurses)
      forbidden_paths = [
        buildpath, root, prefix, llvm_prefix, Dir.home, "/nix/store/", "/private/tmp/",
        "/private/var/", "/Users/"
      ]
      forbidden_paths << opt_prefix if opt_prefix.to_s != guest_opt_prefix
      forbidden_paths << ncurses if ncurses.to_s != guest_ncurses
      forbidden_paths << HOMEBREW_PREFIX if HOMEBREW_PREFIX.to_s != guest_brew_prefix
      forbidden_paths.compact.map(&:to_s).reject(&:empty?).uniq.each do |path|
        odie "Vim path diagnostics retain builder path #{path}" if pathdef_contents.include?(path)
      end

      system "make", "-j#{ENV.make_jobs}"

      instrumented = buildpath/"src/vim.instrumented"
      system "#{root}/scripts/run-wasm-fork-instrument.sh",
        buildpath/"src/vim", "-o", instrumented
      mv instrumented, buildpath/"src/vim"
      kandelo_validate_wasm_artifact(
        buildpath/"src/vim",
        fork:            :required,
        forbidden_paths: [ncurses],
      )
      kandelo_validate_wasm_artifact(
        buildpath/"src/xxd/xxd",
        fork:            :forbidden,
        forbidden_paths: [ncurses],
      )

      system "make", "install", "prefix=#{prefix}", "STRIP=true"
    end

    bin.install_symlink "vim" => "vi"
    %w[evim.1 vim.1 vimtutor.1].each do |manual|
      inreplace man1/manual, prefix, opt_prefix
    end
  end

  test do
    guest_brew_prefix = "/home/linuxbrew/.linuxbrew"
    guest_opt_prefix = "/home/linuxbrew/.linuxbrew/opt/vim"
    guest_ncurses = "/home/linuxbrew/.linuxbrew/opt/ncurses"
    stable_source = "/usr/src/vim-#{version}"
    runtime = pkgshare/"vim92"
    assert_path_exists runtime/"syntax/c.vim"
    assert_path_exists runtime/"doc/tags"
    assert_path_exists bin/"xxd"
    vim_bytes = File.binread(bin/"vim")
    assert_includes vim_bytes, guest_opt_prefix
    assert_includes vim_bytes, guest_ncurses
    assert_includes vim_bytes, stable_source
    host_path_pattern = %r{/(?:private/tmp/|private/var/|Users/|home/runner/(?:_work|work)/|nix/store/)}
    guest_cellar_pattern = %r{/home/linuxbrew/\.linuxbrew/Cellar/(?:vim|ncurses)/}
    [vim_bytes, File.binread(bin/"xxd")].each do |artifact|
      refute_match host_path_pattern, artifact
      refute_match guest_cellar_pattern, artifact
      forbidden_paths = [Pathname(kandelo_require_root!), prefix]
      forbidden_paths << HOMEBREW_PREFIX if HOMEBREW_PREFIX.to_s != guest_brew_prefix
      forbidden_paths << opt_prefix if opt_prefix.to_s != guest_opt_prefix
      forbidden_paths.map(&:to_s).reject(&:empty?).uniq.each do |path|
        refute_includes artifact, path
      end
    end
    %w[evim.1 vim.1 vimtutor.1].each do |manual|
      contents = (man1/manual).read
      assert_includes contents, opt_prefix.to_s
      refute_includes contents, prefix.to_s
    end
    %w[ex rview rvim vi view vimdiff].each do |command|
      assert_path_exists bin/command
    end

    version_output = kandelo_run_wasm(bin/"vim", ["--version"])
    assert_match(/VIM - Vi IMproved 9\.2/, version_output)
    assert_includes version_output, "+fork()"
    assert_includes version_output, "+libcall"
    assert_includes version_output, "+multi_byte"
    assert_includes version_output, "+terminal"
    assert_includes version_output, "+terminfo"

    source = testpath/"input.txt"
    commands = testpath/"commands.vim"
    startup_commands = testpath/"startup.vim"
    libcall_source = testpath/"vim-libcall.c"
    libcall_module = testpath/"vim-libcall.so"
    guest_runtime = "#{guest_opt_prefix}/share/vim/vim92"
    runtime_files = {}
    runtime.glob("**/*").select(&:file?).each do |file|
      relative = file.relative_path_from(runtime)
      runtime_files["#{guest_runtime}/#{relative}"] = runtime/relative
    end
    source.write("alpha\nbeta\n")
    libcall_source.write <<~C
      int kandelo_vim_libcall(char *argument) {
        return argument != 0 && argument[0] == 'K' && argument[6] == 'o' && argument[7] == '\\0'
          ? 73 : -1;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, "-shared", "-fPIC", libcall_source, "-o", libcall_module
    end
    startup_commands.write("set nomore\nquit\n")
    commands.write <<~VIM
      set nomore
      " This test selects the shipped Vim syntax directly. `syntax on` also
      " drives optional user ftdetect globs, which are not installed runtime
      " content and are outside this focused runtime and syntax assertion.
      runtime syntax/synload.vim
      set filetype=vim
      set syntax=vim
      if !exists("b:current_syntax") || b:current_syntax !=# "vim"
        cquit
      endif
      if search('kandelo-timeout-path-must-not-match', 'W', 0, 10) != 0
        cquit
      endif
      if libcallnr('/work/vim-libcall.so', 'kandelo_vim_libcall', 'Kandelo') != 73
        cquit
      endif
      silent 0read !printf child-line
      %substitute/alpha/ALPHA/
      write
      quit
    VIM
    vim_env = {
      "HOME"       => "/work",
      "KERNEL_CWD" => "/work",
      "SHELL"      => "/bin/sh",
      "TERM"       => "xterm-256color",
      "TMPDIR"     => "/work",
    }
    assert_empty kandelo_run_wasm(
      bin/"vim",
      ["-Nu", "NONE", "-n", "-es", "-S", startup_commands.basename],
      env:                       vim_env,
      guest_files:               runtime_files,
      merge_stderr:              true,
      writable_host_directories: { "/work" => testpath },
    )
    assert_empty kandelo_run_wasm(
      bin/"vim",
      ["-Nu", "NONE", "-n", "-es", "-S", commands.basename, source.basename],
      env:                       vim_env,
      exec_programs:             { "/bin/sh" => formula_opt_bin("kandelo-dev/tap-core/dash")/"dash" },
      expected_fork_descendants: 1,
      guest_files:               runtime_files,
      merge_stderr:              true,
      writable_host_directories: { "/work" => testpath },
    )
    assert_equal "child-line\nALPHA\nbeta\n", source.read

    hex = kandelo_run_wasm(bin/"xxd", ["-p"], stdin: "Kandelo\n")
    assert_equal "4b616e64656c6f0a\n", hex
    assert_equal "Kandelo\n", kandelo_run_wasm(bin/"xxd", ["-r", "-p"], stdin: hex)
  end
end
