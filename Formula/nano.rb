require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Nano < Formula
  include KandeloFormulaSupport

  desc "Small, friendly text editor for Kandelo"
  homepage "https://www.nano-editor.org/"
  url "https://ftpmirror.gnu.org/gnu/nano/nano-8.0.tar.xz"
  mirror "https://www.nano-editor.org/dist/v8/nano-8.0.tar.xz"
  sha256 "c17f43fc0e37336b33ee50a209c701d5beb808adc2d9f089ca831b40539c9ac4"
  license "GPL-3.0-or-later"

  depends_on "automattic/kandelo-homebrew/ncurses"

  skip_clean "bin/nano"

  def install
    kandelo_require_arch!("wasm32")
    ncurses = formula_opt_prefix("automattic/kandelo-homebrew/ncurses")
    guest_prefix = "/home/linuxbrew/.linuxbrew"

    kandelo_wasm_build do |root|
      ENV["CFLAGS"] = "-O2 -gline-tables-only -fdebug-compilation-dir=."
      ENV["NCURSESW_CFLAGS"] = "-I#{ncurses}/include"
      ENV["NCURSESW_LIBS"] = "-L#{ncurses}/lib -lncursesw -ltinfow"

      system kandelo_configure, *kandelo_std_configure_args,
        "--sysconfdir=#{guest_prefix}/etc",
        "--disable-nls",
        "--disable-browser",
        "--disable-speller",
        "--disable-libmagic",
        "--enable-utf8",
        "--disable-extra"

      system "make", "-j#{ENV.make_jobs}"

      instrumented = buildpath/"src/nano.instrumented"
      system "#{root}/scripts/run-wasm-fork-instrument.sh", buildpath/"src/nano", "-o", instrumented
      mv instrumented, buildpath/"src/nano"

      system "make", "install"
    end
  end

  test do
    version_output = kandelo_run_wasm(bin/"nano", ["--version"])
    assert_match(/^ GNU nano, version 8\.0$/, version_output.lines.first)

    help_output = kandelo_run_wasm(bin/"nano", ["--help"])
    assert_includes help_output, "--rcfile=<file>"
    assert_includes help_output, "--ignorercfiles"
    assert_includes help_output, "--modernbindings"

    note = testpath/"note.txt"
    empty_terminfo = testpath/"empty-terminfo"
    note.write "alpha\nbeta\n"
    empty_terminfo.mkpath
    transcript = kandelo_run_pty_wasm(
      bin/"nano", [note.basename],
      env:    {
        "HOME"       => testpath,
        "KERNEL_CWD" => testpath,
        "LANG"       => "C.UTF-8",
        "TERM"       => "xterm-256color",
        "TERMINFO"   => empty_terminfo,
      },
      inputs: ["\u001c", "beta", "\r", "BETA", "\r", "a", "\u000f", "\r", "\u0018"]
    )
    assert_includes transcript, "Search"
    assert_includes transcript, "Wrote 2 lines"
    assert_equal "alpha\nBETA\n", note.read

    binary = File.binread(bin/"nano")
    assert_includes binary, "/home/linuxbrew/.linuxbrew/etc/nanorc"
    refute_includes binary, prefix.to_s
    refute_match %r{/Users/[^/]+/}, binary
  end
end
