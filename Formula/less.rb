require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Less < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/less".freeze
  GUEST_NCURSES_PREFIX = "/home/linuxbrew/.linuxbrew/opt/ncurses".freeze

  desc "Terminal pager with more-compatible mode for Kandelo"
  homepage "https://www.greenwoodsoftware.com/less/"
  url "https://www.greenwoodsoftware.com/less/less-668.tar.gz"
  sha256 "2819f55564d86d542abbecafd82ff61e819a3eec967faa36cd3e68f1596a44b8"
  license "GPL-3.0-or-later"
  revision 1

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "kandelo-dev/tap-core/dash" => :test
  depends_on "kandelo-dev/tap-core/ncurses"

  skip_clean "bin/less", "bin/lesskey", "bin/lessecho"

  def install
    kandelo_require_arch!("wasm32")
    ncurses = formula_opt_prefix("kandelo-dev/tap-core/ncurses")

    kandelo_wasm_build do
      ENV["CPPFLAGS"] = "-I#{ncurses}/include"
      ENV["LDFLAGS"] = "-L#{ncurses}/lib"

      system kandelo_configure, *kandelo_std_configure_args, "--with-regex=posix"
      # Less compiles BINDIR and SYSDIR into the pager for system lesskey
      # files. Use the stable opt path there while leaving the install target
      # at the versioned keg configured above.
      system "make", "-j#{ENV.make_jobs}",
             "bindir=#{GUEST_OPT_PREFIX}/bin", "sysconfdir=#{GUEST_OPT_PREFIX}/etc"

      stage = buildpath/"kandelo-stage"
      system "make", "install", "DESTDIR=#{stage}"
      staged_prefix = stage/prefix.to_s.delete_prefix("/")
      odie "Less did not install into its staged prefix" unless staged_prefix.directory?
      %w[less lesskey lessecho].each do |name|
        kandelo_validate_wasm_artifact(staged_prefix/"bin"/name, fork: :forbidden)
      end
      prefix.install staged_prefix.children
    end

    bin.install_symlink "less" => "more"
    man1.install_symlink "less.1" => "more.1"
  end

  test do
    assert_path_exists bin/"less"
    assert_equal "less", (bin/"more").readlink.to_s
    assert_path_exists bin/"lesskey"
    assert_path_exists bin/"lessecho"
    assert_path_exists man1/"less.1"
    assert_match(/COMPATIBILITY WITH MORE/, (man1/"more.1").read)
    assert_path_exists man1/"lesskey.1"
    assert_path_exists man1/"lessecho.1"

    assert_match(/^less 668 \(POSIX regular expressions\)$/, kandelo_run_wasm(bin/"less", ["--version"]).lines.first)
    assert_match(/^lesskey  version 668$/, kandelo_run_wasm(bin/"lesskey", ["--version"]).lines.first)
    assert_equal "alpha\\ beta plain\n",
      kandelo_run_wasm(bin/"lessecho", ["-m ", "alpha beta", "plain"])

    input = testpath/"input.txt"
    input.write "alpha\nbeta\ngamma\n"
    dash = formula_opt_prefix("kandelo-dev/tap-core/dash")/"bin/dash"
    env = {
      "HOME"       => testpath,
      "KERNEL_CWD" => testpath,
      "TERM"       => "xterm-256color",
      "TERMINFO"   => testpath/"empty-terminfo",
    }
    (testpath/"empty-terminfo").mkpath
    assert_equal input.read,
      kandelo_run_wasm(bin/"less", ["-F", "-X", input.basename], env: env)

    more_input = "alpha\n\n\nbeta\n"
    # Upstream Less uses its nonterminal cat path here, where screen formatting
    # such as more's -s blank-line squeezing intentionally does not apply.
    assert_equal more_input,
      kandelo_run_wasm(
        bin/"more", [], env: env.merge("HOME" => "/", "KERNEL_CWD" => "/", "MORE" => "-s"),
        stdin: more_input, preserve_argv0: true,
        exec_programs: { "/bin/sh" => dash }
      )

    more_file = testpath/"more-input.txt"
    more_file.write more_input
    more_transcript = kandelo_run_pty_wasm(
      bin/"more", ["/more-input.txt"], inputs: [],
      argv0: "#{GUEST_OPT_PREFIX}/bin/more",
      env: {
        "HOME"       => "/",
        "KERNEL_CWD" => "/",
        "MORE"       => "-s -F -X",
        "TERM"       => "xterm-256color",
      },
      guest_files: { "/more-input.txt" => more_file }, initial_delay_ms: 100
    )
    assert_includes more_transcript, "\ralpha\r\n\r\nbeta\r\n"
    refute_includes more_transcript, "\ralpha\r\n\r\n\r\nbeta\r\n"

    filter_env = {
      "HOME"       => "/work",
      "KERNEL_CWD" => "/work",
      "LESSOPEN"   => "|echo filtered:%s",
      "SHELL"      => "/bin/sh",
    }
    filter_output = kandelo_run_wasm(
      bin/"less", ["-F", "-X", input.basename],
      env: filter_env, exec_programs: { "/bin/sh" => dash },
      guest_files: { "/work/input.txt" => input }, expected_fork_descendants: 1
    )
    assert_equal "filtered:input.txt\n", filter_output

    # The terminal implementation must come from the ncurses keg. This also
    # guards against restoring the registry recipe's fake termcap library.
    less_bytes = File.binread(bin/"less")
    assert_includes less_bytes, "#{GUEST_NCURSES_PREFIX}/share/terminfo"
    assert_includes less_bytes, "#{GUEST_OPT_PREFIX}/bin/.sysless"
    assert_includes less_bytes, "#{GUEST_OPT_PREFIX}/etc/sysless"
    assert_includes less_bytes, "#{GUEST_OPT_PREFIX}/etc/syslesskey"
    [bin/"less", bin/"lesskey", bin/"lessecho"].each do |command|
      refute_includes File.binread(command), prefix.to_s
    end
  end
end
