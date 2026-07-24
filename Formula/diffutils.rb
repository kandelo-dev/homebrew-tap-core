require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Diffutils < Formula
  include KandeloFormulaSupport

  GUEST_COREUTILS_PR = "/home/linuxbrew/.linuxbrew/opt/coreutils/bin/pr".freeze
  GUEST_DIFFUTILS_BIN = "/home/linuxbrew/.linuxbrew/opt/diffutils/bin".freeze
  GUEST_ED = "/home/linuxbrew/.linuxbrew/opt/ed/bin/ed".freeze

  desc "GNU file comparison utilities for Kandelo"
  homepage "https://www.gnu.org/software/diffutils/"
  url "https://ftpmirror.gnu.org/gnu/diffutils/diffutils-3.12.tar.xz"
  mirror "https://ftp.gnu.org/gnu/diffutils/diffutils-3.12.tar.xz"
  sha256 "7c8b7f9fc8609141fdea9cece85249d308624391ff61dedaf528fcb337727dfd"
  license "GPL-3.0-or-later"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build
  depends_on "kandelo-dev/tap-core/coreutils"
  depends_on "kandelo-dev/tap-core/ed"

  skip_clean "bin/diff", "bin/cmp", "bin/diff3", "bin/sdiff"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      ENV["PR_PROGRAM"] = GUEST_COREUTILS_PR

      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-nls",
        "--disable-dependency-tracking"
      system "make", "-j#{ENV.make_jobs}"

      stage = buildpath/"kandelo-stage"
      system "make", "install", "DESTDIR=#{stage}"
      staged_prefix = stage/prefix.to_s.delete_prefix("/")
      odie "Diffutils did not install into its staged prefix" unless staged_prefix.directory?

      %w[diff diff3 sdiff].each do |name|
        artifact = staged_prefix/"bin"/name
        kandelo_fork_instrument(artifact)
        kandelo_validate_wasm_artifact(artifact, fork: :required)
      end
      kandelo_validate_wasm_artifact(staged_prefix/"bin/cmp", fork: :forbidden)
      prefix.install staged_prefix.children
    end
  end

  test do
    assert_path_exists man1/"diff.1"
    assert_path_exists info/"diffutils.info"
    left = testpath/"left.txt"
    right = testpath/"right.txt"
    base = testpath/"base.txt"
    left.write "alpha\nbeta\ngamma\n"
    right.write "alpha\nBETA\ngamma\n"
    base.write "alpha\nbase\ngamma\n"

    mount = { "/work" => testpath }
    cwd_env = { "KERNEL_CWD" => "/work", "KERNEL_PATH" => GUEST_DIFFUTILS_BIN }
    diff_program = { "#{GUEST_DIFFUTILS_BIN}/diff" => bin/"diff" }
    assert_empty kandelo_run_wasm(
      bin/"diff", ["left.txt", "left.txt"], env: cwd_env, writable_host_directories: mount
    )

    brief = kandelo_run_wasm(
      bin/"diff", ["--brief", "left.txt", "right.txt"],
      env:                       cwd_env,
      writable_host_directories: mount,
      expected_status:           1
    )
    assert_equal "Files left.txt and right.txt differ\n", brief

    assert_empty kandelo_run_wasm(
      bin/"cmp", ["--silent", "left.txt", "right.txt"],
      env:                       cwd_env,
      writable_host_directories: mount,
      expected_status:           1
    )

    coreutils_pr = formula_opt_bin("kandelo-dev/tap-core/coreutils")/"pr"
    paginated = kandelo_run_wasm(
      bin/"diff", ["--paginate", "left.txt", "right.txt"],
      env:                       cwd_env,
      exec_programs:             { GUEST_COREUTILS_PR => coreutils_pr },
      writable_host_directories: mount,
      expected_status:           1
    )
    assert_match(/diff --paginate left\.txt right\.txt\s+Page 1/, paginated)
    assert_match(/^2c2$/, paginated)

    merged = kandelo_run_wasm(
      bin/"diff3", ["--merge", "left.txt", "base.txt", "right.txt"],
      env:                       cwd_env,
      exec_programs:             diff_program,
      writable_host_directories: mount,
      expected_status:           1
    )
    assert_match "<<<<<<< left.txt", merged
    assert_match "=======", merged
    assert_match ">>>>>>> right.txt", merged

    side_by_side = kandelo_run_wasm(
      bin/"sdiff", ["--width=50", "left.txt", "right.txt"],
      env:                       cwd_env,
      exec_programs:             diff_program,
      writable_host_directories: mount,
      expected_status:           1
    )
    assert_match(/beta\s+\|\s+BETA/, side_by_side)

    ed = formula_opt_bin("kandelo-dev/tap-core/ed")/"ed"
    editor_env = cwd_env.merge("KERNEL_PATH" => "#{GUEST_DIFFUTILS_BIN}:#{File.dirname(GUEST_ED)}")
    kandelo_run_wasm(
      bin/"sdiff", ["--output=merged.txt", "left.txt", "right.txt"],
      env:                       cwd_env,
      exec_programs:             diff_program,
      stdin:                     "l\n",
      writable_host_directories: mount,
      expected_status:           1
    )
    assert_equal left.read, (testpath/"merged.txt").read

    kandelo_run_pty_wasm(
      bin/"sdiff", ["--output=edited.txt", "left.txt", "right.txt"],
      argv0:                     "#{GUEST_DIFFUTILS_BIN}/sdiff",
      env:                       editor_env,
      exec_programs:             diff_program.merge(GUEST_ED => ed),
      inputs:                    ["e l\n", "1c\n", "edited\n", ".\n", "w\n", "q\n"],
      writable_host_directories: mount,
      expected_status:           1
    )
    assert_equal "alpha\nedited\ngamma\n", (testpath/"edited.txt").read
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 1
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "e0429807a455cac881779878506a095f63507ab0338c1e70cd0577b97ed42d41"
  end

end
