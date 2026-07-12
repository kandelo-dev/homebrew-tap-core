require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Grep < Formula
  include KandeloFormulaSupport

  desc "GNU regular expression search tool for Kandelo"
  homepage "https://www.gnu.org/software/grep/"
  url "https://ftpmirror.gnu.org/gnu/grep/grep-3.11.tar.xz"
  sha256 "1db2aedde89d0dea42b16d9528f894c8d15dae4e190b59aecc78f5a951276eab"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/grep"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-nls",
        "--disable-perl-regexp",
        "--disable-dependency-tracking"
      system "make", "-j#{ENV.make_jobs}"
      kandelo_validate_wasm_artifact(buildpath/"src/grep", fork: :forbidden)
    end

    kandelo_install_bin(buildpath/"src", "grep", "grep")
  end

  test do
    version_output = kandelo_run_wasm(bin/"grep", ["--version"])
    assert_match(/grep(?:\.wasm)? \(GNU grep\) 3\.11$/, version_output)

    stdin = "skip\nAlpha\nalpha42\nalphabet\n"
    assert_equal "2:Alpha\n3:alpha42\n",
      kandelo_run_wasm(bin/"grep", ["-inE", "^alpha([0-9]+)?$"], stdin: stdin)

    inputs = testpath/"inputs"
    inputs.mkpath
    (inputs/"first.txt").write("red\nblue\nred blue\n")
    (inputs/"second.txt").write("blue\ngreen\n")
    cwd_env = { "KERNEL_CWD" => inputs }

    assert_equal "first.txt:2\nsecond.txt:1\n",
      kandelo_run_wasm(bin/"grep", ["-HcF", "blue", "first.txt", "second.txt"], env: cwd_env)
    assert_empty kandelo_run_wasm(
      bin/"grep", ["absent", "first.txt"], env: cwd_env, merge_stderr: true, expected_status: 1
    )

    missing_output = kandelo_run_wasm(
      bin/"grep", ["needle", "missing.txt"], env: cwd_env, merge_stderr: true, expected_status: 2
    )
    assert_match(/missing\.txt.*No such file or directory/, missing_output)
  end
end
