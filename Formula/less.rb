require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Less < Formula
  include KandeloFormulaSupport

  desc "Terminal pager for Kandelo"
  homepage "https://www.greenwoodsoftware.com/less/"
  url "https://www.greenwoodsoftware.com/less/less-668.tar.gz"
  sha256 "2819f55564d86d542abbecafd82ff61e819a3eec967faa36cd3e68f1596a44b8"
  license "GPL-3.0-or-later"

  depends_on "automattic/kandelo-homebrew/ncurses"

  skip_clean "bin/less", "bin/lesskey", "bin/lessecho"

  def install
    kandelo_require_arch!("wasm32")
    ncurses = formula_opt_prefix("automattic/kandelo-homebrew/ncurses")

    kandelo_wasm_build do |root|
      ENV["CPPFLAGS"] = "-I#{ncurses}/include"
      ENV["LDFLAGS"] = "-L#{ncurses}/lib"

      system kandelo_configure, *kandelo_std_configure_args, "--with-regex=posix"
      # Less compiles BINDIR and SYSDIR into the pager for system lesskey
      # files. Use the stable opt path there while leaving the install target
      # at the versioned keg configured above.
      system "make", "-j#{ENV.make_jobs}", "bindir=#{opt_bin}", "sysconfdir=#{opt_prefix}/etc"

      instrumented = buildpath/"less.instrumented"
      system "#{root}/scripts/run-wasm-fork-instrument.sh", buildpath/"less", "-o", instrumented
      mv instrumented, buildpath/"less"

      system "make", "install"
    end
  end

  test do
    assert_path_exists bin/"less"
    assert_path_exists bin/"lesskey"
    assert_path_exists bin/"lessecho"
    assert_path_exists man1/"less.1"
    assert_path_exists man1/"lesskey.1"
    assert_path_exists man1/"lessecho.1"

    assert_match(/^less 668 \(POSIX regular expressions\)$/, kandelo_run_wasm(bin/"less", ["--version"]).lines.first)
    assert_match(/^lesskey  version 668$/, kandelo_run_wasm(bin/"lesskey", ["--version"]).lines.first)
    assert_equal "alpha\\ beta plain\n",
      kandelo_run_wasm(bin/"lessecho", ["-m ", "alpha beta", "plain"])

    input = testpath/"input.txt"
    input.write "alpha\nbeta\ngamma\n"
    env = {
      "HOME"       => testpath,
      "KERNEL_CWD" => testpath,
      "TERM"       => "xterm-256color",
      "TERMINFO"   => testpath/"empty-terminfo",
    }
    (testpath/"empty-terminfo").mkpath
    assert_equal input.read,
      kandelo_run_wasm(bin/"less", ["-F", "-X", input.basename], env: env)

    filter_env = env.merge(
      "LESSOPEN" => "|echo filtered:%s",
      "SHELL"    => "/bin/sh",
    )
    assert_equal "filtered:input.txt\n",
      kandelo_run_wasm(bin/"less", ["-F", "-X", input.basename], env: filter_env)

    # The terminal implementation must come from the ncurses keg. This also
    # guards against restoring the registry recipe's fake termcap library.
    ncurses = formula_opt_prefix("automattic/kandelo-homebrew/ncurses")
    less_bytes = File.binread(bin/"less")
    assert_includes less_bytes, "#{ncurses}/share/terminfo"
    assert_includes less_bytes, "#{opt_bin}/.sysless"
    assert_includes less_bytes, "#{opt_prefix}/etc/sysless"
    assert_includes less_bytes, "#{opt_prefix}/etc/syslesskey"
    [bin/"less", bin/"lesskey", bin/"lessecho"].each do |command|
      refute_includes File.binread(command), prefix.to_s
    end
  end
end
