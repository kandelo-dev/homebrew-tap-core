require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Python < Formula
  include KandeloFormulaSupport

  KANDELO_REGISTRY_BRIDGE = true

  PYTHON_MAJOR_MINOR = "3.13".freeze
  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/python".freeze
  GUEST_RUNTIME = "#{GUEST_OPT_PREFIX}/lib/python3.13".freeze

  desc "CPython interpreter and standard library for Kandelo"
  homepage "https://www.python.org/"
  url "https://www.python.org/ftp/python/3.13.3/Python-3.13.3.tar.xz"
  version "3.13.3"
  sha256 "40f868bcbdeb8149a3149580bb9bfd407b3321cd48f0be631af955ac92c0e041"
  license "Python-2.0"
  revision 1

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on "unzip" => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin"
  skip_clean "lib/python3.13"

  def install
    kandelo_require_arch!("wasm32")
    out_dir = kandelo_build_package(
      package:    "cpython",
      script_env: {
        "WASM_POSIX_DEP_GUEST_PREFIX" => GUEST_OPT_PREFIX,
        "WASM_POSIX_DEP_ZLIB_DIR"     => formula_opt_prefix("kandelo-dev/tap-core/zlib"),
      },
    )
    kandelo_validate_wasm_artifact(out_dir/"python.wasm", fork: :required)
    kandelo_install_bin(out_dir, "python.wasm", "python3")
    bin.install_symlink "python3" => "python"
    bin.install_symlink "python3" => "python#{PYTHON_MAJOR_MINOR}"

    runtime_stage = buildpath/"python-runtime-stage"
    system formula_opt_bin("unzip")/"unzip", "-q", out_dir/"python-runtime.zip", "-d", runtime_stage
    stdlib = runtime_stage/"lib/python#{PYTHON_MAJOR_MINOR}"
    runtime_license = runtime_stage/"share/licenses/cpython/LICENSE"
    odie "CPython runtime archive has no standard library" unless (stdlib/"json/__init__.py").file?
    odie "CPython runtime archive has no license" unless runtime_license.file?

    lib.install stdlib
    # CPython's getpath initialization requires this installed-prefix directory
    # even when every supported extension module is statically linked. Retain a
    # marker so Homebrew's bottle cannot discard the otherwise empty directory.
    lib_dynload = lib/"python#{PYTHON_MAJOR_MINOR}/lib-dynload"
    lib_dynload.mkpath
    (lib_dynload/"README.txt").write <<~EOS
      Kandelo CPython builds supported extension modules into the interpreter.
      This directory is intentionally retained for CPython prefix discovery.
    EOS
    (share/"licenses/cpython").install runtime_license
  end

  test do
    stdlib = lib/"python#{PYTHON_MAJOR_MINOR}"
    assert_path_exists stdlib/"json/__init__.py"
    assert_path_exists stdlib/"lib-dynload/README.txt"
    assert_path_exists share/"licenses/cpython/LICENSE"
    %w[python python3 python3.13].each { |command| assert_path_exists bin/command }

    runtime_files = {}
    stdlib.glob("**/*").select(&:file?).each do |file|
      relative = file.relative_path_from(stdlib)
      runtime_files["#{GUEST_RUNTIME}/#{relative}"] = file
    end
    assert_operator runtime_files.length, :>, 500
    env = {
      "HOME"                    => "/tmp",
      "PYTHONDONTWRITEBYTECODE" => "1",
    }
    program = <<~PYTHON
      import json
      import site
      import sys
      import zlib
      assert sys.version_info[:3] == (3, 13, 3)
      assert sys.prefix == "#{GUEST_OPT_PREFIX}"
      assert "#{GUEST_RUNTIME}/site-packages" in site.getsitepackages()
      assert json.loads('{"kandelo": [3, 1, 3]}') == {"kandelo": [3, 1, 3]}
      assert zlib.decompress(zlib.compress(b"kandelo-python")) == b"kandelo-python"
      print("python-node-ok:3.13.3")
    PYTHON
    assert_equal "python-node-ok:3.13.3\n", kandelo_run_wasm(
      bin/"python3", ["-c", program],
      argv0: GUEST_OPT_PREFIX + "/bin/python3", env: env,
      guest_files: runtime_files, merge_stderr: true
    )

    browser_program = program.sub("python-node-ok", "python-browser-ok")
    assert_equal "python-browser-ok:3.13.3\n", kandelo_run_browser_wasm(
      bin/"python3",
      ["-c", browser_program],
      argv0:              "python3",
      guest_program_path: GUEST_OPT_PREFIX + "/bin/python3",
      env:                env,
      guest_files:        runtime_files,
      timeout_ms:         180_000,
    )
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: "/home/linuxbrew/.linuxbrew/Cellar", wasm32_kandelo: "4389c20b2d0dcd71d8111f08c5e5d54add1af0ba51e7110549bc24a3cd07bdb2"
  end

end
