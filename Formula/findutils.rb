require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Findutils < Formula
  include KandeloFormulaSupport

  desc "GNU find and xargs utilities for Kandelo"
  homepage "https://www.gnu.org/software/findutils/"
  url "https://ftpmirror.gnu.org/gnu/findutils/findutils-4.10.0.tar.xz"
  mirror "https://ftp.gnu.org/gnu/findutils/findutils-4.10.0.tar.xz"
  sha256 "1387e0b67ff247d2abde998f90dfbf70c1491391a59ddfecb8ae698789f0a4f5"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/dash" => :test

  skip_clean "bin/find", "bin/xargs"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-nls",
        "--without-selinux",
        "--localstatedir=/var",
        "--disable-dependency-tracking"
      system "make", "-j#{ENV.make_jobs}"

      {
        buildpath/"find/find"   => buildpath/"find/find.instrumented",
        buildpath/"xargs/xargs" => buildpath/"xargs/xargs.instrumented",
      }.each do |source, instrumented|
        system "#{root}/scripts/run-wasm-fork-instrument.sh", source, "-o", instrumented
        kandelo_validate_wasm_artifact(instrumented, fork: :required)
        mv instrumented, source
      end

      system "make", "-C", "find", "install"
      system "make", "-C", "xargs", "install"
      system "make", "-C", "doc", "install"
    end
  end

  test do
    [man1/"find.1", man1/"xargs.1", info/"find.info", info/"find-maint.info"].each do |document|
      assert_path_exists document
    end
    assert_match(/find \(GNU findutils\) 4\.10\.0$/,
      kandelo_run_wasm(bin/"find", ["--version"]))
    assert_match(/xargs \(GNU findutils\) 4\.10\.0$/,
      kandelo_run_wasm(bin/"xargs", ["--version"]))

    workspace = testpath/"workspace"
    (workspace/"nested").mkpath
    (workspace/"alpha.txt").write("alpha\n")
    (workspace/"nested/beta.txt").write("beta\n")
    (workspace/"nested/skip.log").write("skip\n")
    env = { "KERNEL_CWD" => "/work" }
    mount = { "/work" => workspace }
    dash = formula_opt_bin("automattic/kandelo-homebrew/dash")/"dash"
    exec_programs = { "/bin/sh" => dash }

    listing = kandelo_run_wasm(
      bin/"find", [".", "-type", "f", "-name", "*.txt", "-printf", "%P:%s\\n"],
      env: env, writable_host_directories: mount
    )
    assert_equal ["alpha.txt:6", "nested/beta.txt:5"], listing.lines.map(&:chomp).sort

    exec_output = kandelo_run_wasm(
      bin/"find",
      [".", "-type", "f", "-name", "*.txt", "-exec", "/bin/sh", "-c", 'printf "found %s\\n" "$1"',
       "sh", "{}", ";"],
      env:                       env,
      exec_programs:             exec_programs,
      writable_host_directories: mount,
      expected_fork_descendants: 2,
    )
    assert_equal ["found ./alpha.txt", "found ./nested/beta.txt"],
      exec_output.lines.map(&:chomp).sort

    assert_equal "item alpha beta\nitem gamma\n",
      kandelo_run_wasm(
        bin/"xargs",
        ["-n", "2", "/bin/sh", "-c", 'printf "item"; printf " %s" "$@"; printf "\\n"', "sh"],
        stdin:                     "alpha beta gamma\n",
        exec_programs:             exec_programs,
        expected_fork_descendants: 2,
      )

    missing = kandelo_run_wasm(
      bin/"find", ["missing"], env: env, merge_stderr: true,
      writable_host_directories: mount, expected_status: 1
    )
    assert_match(/missing.*No such file or directory/, missing)
  end
end
