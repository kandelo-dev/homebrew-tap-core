require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Coreutils < Formula
  include KandeloFormulaSupport

  desc "GNU file, shell, and text utilities for Kandelo"
  homepage "https://www.gnu.org/software/coreutils/"
  url "https://ftpmirror.gnu.org/gnu/coreutils/coreutils-9.6.tar.xz"
  mirror "https://ftp.gnu.org/gnu/coreutils/coreutils-9.6.tar.xz"
  sha256 "7a0124327b398fd9eb1a6abde583389821422c744ffa10734b24f557610d3283"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/coreutils"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-nls",
        "--without-selinux",
        "--without-libgmp",
        "--without-openssl",
        "--disable-acl",
        "--disable-xattr",
        "--disable-dependency-tracking",
        "--enable-single-binary=symlinks",
        "--enable-install-program=arch",
        "--enable-no-install-program=stdbuf,pinky,who,users,uptime"
      system "make", "-j#{ENV.make_jobs}"

      coreutils = buildpath/"src/coreutils"
      instrumented = buildpath/"src/coreutils.instrumented"
      system "#{root}/scripts/run-wasm-fork-instrument.sh", coreutils, "-o", instrumented
      mv instrumented, coreutils
      kandelo_validate_wasm_artifact(coreutils, fork: :required)
      system "make", "install"
    end
  end

  test do
    assert_path_exists man1/"ls.1"
    assert_path_exists info/"coreutils.info"
    assert_match(/ls \(GNU coreutils\) 9\.6$/,
      kandelo_run_wasm(bin/"ls", ["--version"], preserve_argv0: true))
    assert_equal "wasm32\n", kandelo_run_wasm(bin/"arch", [], preserve_argv0: true)
    assert_match(/kill \(GNU coreutils\) 9\.6$/,
      kandelo_run_wasm(bin/"kill", ["--version"], preserve_argv0: true))
    assert_empty kandelo_run_wasm(
      bin/"false", [], preserve_argv0: true, expected_status: 1
    )

    mkdir_output = kandelo_run_wasm(
      bin/"mkdir", ["--verbose", "/tmp/coreutils-dir"], preserve_argv0: true
    )
    assert_match(/created directory.*coreutils-dir/, mkdir_output)
    temp_dir = kandelo_run_wasm(
      bin/"mktemp", ["--directory", "/tmp/coreutils.XXXXXX"], preserve_argv0: true
    )
    assert_match(%r{^/tmp/coreutils\.[A-Za-z0-9]{6}$}, temp_dir.chomp)
    assert_equal "directory\n",
      kandelo_run_wasm(bin/"stat", ["--format=%F", "/tmp"], preserve_argv0: true)

    workspace = testpath/"workspace"
    workspace.mkpath
    (workspace/"source.txt").write("gamma\nalpha\nbeta\n")
    cwd_env = { "KERNEL_CWD" => workspace }
    kandelo_run_wasm(
      bin/"coreutils", ["--coreutils-prog=mkdir", "nested"], env: cwd_env
    )
    kandelo_run_wasm(
      bin/"coreutils", ["--coreutils-prog=cp", "source.txt", "nested/copy.txt"], env: cwd_env
    )
    assert_equal "gamma\nalpha\nbeta\n",
      kandelo_run_wasm(
        bin/"coreutils", ["--coreutils-prog=cat", "nested/copy.txt"], env: cwd_env
      )
    assert_equal "17 nested/copy.txt\n",
      kandelo_run_wasm(
        bin/"coreutils", ["--coreutils-prog=wc", "-c", "nested/copy.txt"], env: cwd_env
      )

    assert_equal "alpha\nbeta\ngamma\n",
      kandelo_run_wasm(
        bin/"sort", [], stdin: "gamma\nalpha\nbeta\n", preserve_argv0: true
      )
    assert_equal "name=coreutils value=0042\n",
      kandelo_run_wasm(
        bin/"printf", ["name=%s value=%04d\\n", "coreutils", "42"], preserve_argv0: true
      )

    assert_equal "51\n",
      kandelo_run_wasm(bin/"expr", ["17", "*", "3"], preserve_argv0: true)
    assert_equal "08\n10\n12\n",
      kandelo_run_wasm(bin/"seq", ["-w", "8", "2", "12"], preserve_argv0: true)
    assert_empty kandelo_run_wasm(
      bin/"[", ["17", "-gt", "3", "]"], preserve_argv0: true
    )

    dependency_workspace = testpath/"dependency-workspace"
    dependency_workspace.mkpath
    (dependency_workspace/"remove-me.txt").write "remove me\n"
    (dependency_workspace/"remove-tree/nested").mkpath
    (dependency_workspace/"remove-tree/nested/file.txt").write "remove tree\n"
    dependency_mount = { "/work" => dependency_workspace }
    dependency_env = { "KERNEL_CWD" => "/work" }
    assert_empty kandelo_run_wasm(
      bin/"mkdir", ["-p", "created/nested"],
      env: dependency_env, preserve_argv0: true, writable_host_directories: dependency_mount
    )
    assert_predicate dependency_workspace/"created/nested", :directory?
    assert_empty kandelo_run_wasm(
      bin/"rm", ["remove-me.txt"],
      env: dependency_env, preserve_argv0: true, writable_host_directories: dependency_mount
    )
    refute_path_exists dependency_workspace/"remove-me.txt"
    assert_empty kandelo_run_wasm(
      bin/"rm", ["-r", "remove-tree"],
      env: dependency_env, preserve_argv0: true, writable_host_directories: dependency_mount
    )
    refute_path_exists dependency_workspace/"remove-tree"

    assert_empty kandelo_run_wasm(
      bin/"timeout", ["1", "/bin/true"],
      exec_programs:             { "/bin/true" => bin/"true" },
      expected_fork_descendants: 1,
      preserve_argv0:            true
    )
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "12f2f7db009d7a28e82134726b303da720ba058adb6f202a9eeb39b09b8db53b"
  end

end
