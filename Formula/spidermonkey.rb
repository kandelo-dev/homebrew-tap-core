require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Spidermonkey < Formula
  include KandeloFormulaSupport

  desc "Mozilla JS shell with Kandelo's Node API compatibility layer"
  homepage "https://spidermonkey.dev/"
  url "https://ftp.mozilla.org/pub/firefox/releases/140.11.0esr/source/firefox-140.11.0esr.source.tar.xz"
  version "140.11.0esr"
  sha256 "1b034d2117356fda24807a151055132315c6ba58ad2bdf7ec71ee707fac5e028"
  license all_of: ["MPL-2.0", "GPL-2.0-or-later"]

  depends_on "binaryen" => :build
  depends_on "cbindgen" => :build
  depends_on "pkgconf" => :build
  depends_on "python@3.12" => :build
  depends_on "rust" => :build
  depends_on "wabt" => [:build, :test]
  depends_on "automattic/kandelo-homebrew/libcxx"
  depends_on "automattic/kandelo-homebrew/openssl"
  depends_on "automattic/kandelo-homebrew/zlib"

  conflicts_with "node", because: "both install a node executable"

  skip_clean "bin"

  GUEST_SOURCE_PREFIX = "/usr/src/spidermonkey".freeze
  GUEST_KANDELO_PREFIX = "/usr/src/kandelo".freeze
  GUEST_TOOLCHAIN_PREFIX = "/usr/src/toolchain".freeze
  EXPECTED_PATCHES = %w[
    0001-allow-static-cxx-runtime-for-wasm-linux.patch
    0002-map-kandelo-wasm-linux-rust-target.patch
    0003-jsonprinter-size-t-wasm32.patch
    0004-disable-wasm32-return-address-stackwalk.patch
    0005-getrandom-custom-backend-wasm32.patch
    0006-randomnum-use-sys-random-on-wasm32.patch
    0007-skip-elf-network-check-for-wasm-target.patch
    0008-use-wasm-trap-for-moz-crash.patch
    0009-use-wasm-frame-address-for-native-stack-base.patch
    0010-use-wasm-icu-data-section-syntax.patch
    0011-heap-autorunparallel-task-on-wasm32.patch
    0012-kandelo-node-compat-shell-entry.patch
    0013-kandelo-join-shell-workers.patch
    0014-disable-mozglue-interposers-on-wasm32.patch
  ].freeze

  def install
    kandelo_require_arch!("wasm32")
    root = Pathname(kandelo_require_root!).realpath
    local_sysroot = buildpath/"kandelo-sysroot"
    local_sysroot.mkpath
    cp_r (root/"sysroot").children, local_sysroot

    dependencies = dependency_prefixes
    activate_host_build_tools!
    libcxx = dependencies.fetch("libcxx")
    openssl = dependencies.fetch("openssl")
    zlib = dependencies.fetch("zlib")
    c_family_maps = compiler_prefix_maps(root, dependencies)
    # rustc applies the last matching remap. Put broad temporary roots first
    # so the exact staged source, checkout, and dependency mappings win.
    rust_maps = remapped_paths(root, dependencies).sort_by { |source, _| source.length }.map do |source, destination|
      "--remap-path-prefix=#{source}=#{destination}"
    end

    apply_registry_patches!(root)

    # SpiderMonkey's Mozilla build, Rust target selection, generated Node
    # bootstrap, and fourteen reviewed wasm/POSIX compatibility patches remain
    # registry-owned migration debt. The Tier-2 bridge builds Homebrew's staged
    # Mozilla source and consumes only this formula's declared target kegs.
    out_dir = kandelo_build_package(
      "spidermonkey", "build-spidermonkey.sh", stable.url, stable.checksum.hexdigest,
      script_env: {
        "SPIDERMONKEY_SRC_DIR"       => buildpath,
        "WASM_POSIX_SYSROOT"         => local_sysroot,
        "WASM_POSIX_DEP_LIBCXX_DIR"  => libcxx,
        "WASM_POSIX_DEP_OPENSSL_DIR" => openssl,
        "WASM_POSIX_DEP_ZLIB_DIR"    => zlib,
        "CFLAGS"                     => c_family_maps,
        "CXXFLAGS"                   => c_family_maps,
        "RUSTFLAGS"                  => rust_maps.join(" "),
      }
    )

    js_wasm = out_dir/"js.wasm"
    node_wasm = out_dir/"node.wasm"
    odie "SpiderMonkey build did not produce js.wasm" unless js_wasm.file?
    odie "SpiderMonkey build did not produce node.wasm" unless node_wasm.file?
    odie "SpiderMonkey shell and Node entry point bytes diverged" unless compare_file(js_wasm, node_wasm)

    validate_kandelo_artifact!(js_wasm, root)
    reject_builder_paths!(js_wasm, root, dependencies)

    # Node mode is selected by argv[0] inside the same static shell. Homebrew
    # should store one 30+ MB artifact, not three byte-identical copies.
    kandelo_install_bin(out_dir, "js.wasm", "js")
    bin.install_symlink "js" => "spidermonkey-node"
    bin.install_symlink "js" => "node"
  end

  def caveats
    <<~EOS
      The node and spidermonkey-node commands provide Kandelo's Node.js API
      compatibility layer. They are not the Node.js runtime.

      POSIX fork and Node child_process APIs are unsupported. This build remains
      uninstrumented because fork instrumentation exhausts Chromium's Wasm call
      stack. The tested worker_threads subset creates eval workers with
      SharedArrayBuffer/Atomics workerData. Worker.postMessage and worker-side
      parentPort messaging are not implemented.
    EOS
  end

  test do
    root = Pathname(kandelo_require_root!).realpath
    dependencies = dependency_prefixes
    assert_path_exists bin/"js"
    assert_predicate bin/"spidermonkey-node", :symlink?
    assert_predicate bin/"node", :symlink?
    assert_equal "js", (bin/"spidermonkey-node").readlink.to_s
    assert_equal "js", (bin/"node").readlink.to_s
    validate_kandelo_artifact!(bin/"js", root)
    reject_builder_paths!(bin/"js", root, dependencies)

    shell_source = <<~JAVASCRIPT
      print([3, 1, 2].toSorted().join(','));
      print(new Intl.NumberFormat('de-DE').format(1234567.89));
    JAVASCRIPT
    assert_equal "1,2,3\n1.234.567,89\n",
      kandelo_run_wasm(bin/"js", ["-e", shell_source], preserve_argv0: true)

    node_source = <<~JAVASCRIPT
      const crypto = require('crypto');
      const zlib = require('zlib');
      const { Worker } = require('worker_threads');
      const payload = Buffer.from('kandelo-spidermonkey');
      const roundtrip = zlib.gunzipSync(zlib.gzipSync(payload));
      if (!roundtrip.equals(payload)) throw new Error('zlib roundtrip failed');
      const digest = crypto.createHash('sha256').update(payload).digest('hex');
      if (digest !== 'a4e94d95efabb99816519fcbe170c87674c3ba41baa9b9005b1d10db6db9ff0d') {
        throw new Error('sha256 failed');
      }
      const shared = new SharedArrayBuffer(8);
      const view = new Int32Array(shared);
      const worker = new Worker(
        "const v = new Int32Array(workerData); " +
        "Atomics.store(v, 0, 42); Atomics.store(v, 1, 1); Atomics.notify(v, 1);",
        { eval: true, workerData: shared },
      );
      if (Atomics.load(view, 1) === 0) Atomics.wait(view, 1, 0, 10000);
      if (Atomics.load(view, 1) !== 1 || Atomics.load(view, 0) !== 42) {
        throw new Error('worker_threads shared memory failed');
      }
      worker.terminate();
      console.log('spidermonkey-node-ok');
    JAVASCRIPT
    env = { "HOME" => "/root", "TMPDIR" => "/tmp", "LANG" => "C.UTF-8" }
    assert_equal "spidermonkey-node-ok\n",
      kandelo_run_wasm(bin/"node", ["-e", node_source], env: env, preserve_argv0: true)
    assert_equal "spidermonkey-node-ok\n",
      kandelo_run_browser_wasm(
        bin/"node", ["-e", node_source], argv0: "node", env: env,
        timeout_ms: 180_000
      )
  end

  private

  def activate_host_build_tools!
    python_libexec_bin = formula_opt_libexec("python@3.12")/"bin"
    python_opt_bin = formula_opt_bin("python@3.12")
    pkgconf_opt_bin = formula_opt_bin("pkgconf")
    ENV.prepend_path "PATH", python_libexec_bin
    ENV.prepend_path "PATH", pkgconf_opt_bin

    python3 = which("python3")
    declared_python_paths = [
      python_libexec_bin/"python3",
      python_opt_bin/"python3",
    ].select(&:executable?).map(&:to_s)
    if python3.nil? || declared_python_paths.exclude?(python3.to_s)
      odie "python3 did not resolve from declared python@3.12: #{python3 || "missing"}"
    end
    version_script = <<~PYTHON
      import sys
      print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    PYTHON
    python_version = Utils.safe_popen_read(
      python3, "-c", version_script
    ).strip
    python_version_parts = python_version.split(".").first(2).map(&:to_i)
    unsupported_python = (python_version_parts <=> [3, 10]) == -1 || (python_version_parts <=> [3, 13]) != -1
    odie "SpiderMonkey requires Python >=3.10 and <3.13, got #{python_version}" if unsupported_python

    pkgconf_bin = which("pkgconf")
    expected_pkgconf = pkgconf_opt_bin/"pkgconf"
    if pkgconf_bin.nil? || pkgconf_bin.to_s != expected_pkgconf.to_s
      odie "pkgconf did not resolve from declared keg: #{pkgconf_bin || "missing"}"
    end
    pkgconf_version = Utils.safe_popen_read(pkgconf_bin, "--version").strip
    odie "declared pkgconf returned an empty version" if pkgconf_version.empty?

    ohai "SpiderMonkey host Python: #{python3} (#{python_version})"
    ohai "SpiderMonkey host pkgconf: #{pkgconf_bin} (#{pkgconf_version})"
  end

  def dependency_prefixes
    {
      "libcxx"  => formula_opt_prefix("automattic/kandelo-homebrew/libcxx"),
      "openssl" => formula_opt_prefix("automattic/kandelo-homebrew/openssl"),
      "zlib"    => formula_opt_prefix("automattic/kandelo-homebrew/zlib"),
    }
  end

  def apply_registry_patches!(root)
    patch_dir = root/"packages/registry/spidermonkey/patches"
    actual_patches = patch_dir.glob("*.patch").map { |path| path.basename.to_s }.sort
    if actual_patches != EXPECTED_PATCHES
      odie "SpiderMonkey patch set drifted: expected #{EXPECTED_PATCHES.join(", ")}; " \
           "found #{actual_patches.join(", ")}"
    end

    EXPECTED_PATCHES.each do |patch_name|
      system "patch", "-p1", "-N", "-s", "-d", buildpath, "-i", patch_dir/patch_name
    end
  end

  def remapped_paths(root, dependencies)
    build_paths = [buildpath.to_s]
    build_paths << buildpath.realpath.to_s if buildpath.exist?
    dependency_paths = dependencies.flat_map do |name, path|
      [path.to_s, path.realpath.to_s].uniq.map do |source|
        [source, "/home/linuxbrew/.linuxbrew/opt/#{name}"]
      end
    end
    candidates = [
      *build_paths.map { |path| [path, GUEST_SOURCE_PREFIX] },
      *dependency_paths,
      [root.to_s, GUEST_KANDELO_PREFIX],
      [Dir.home, "/usr/src/home"],
      ["/private/tmp", "/usr/src/tmp"],
      ["/tmp", "/usr/src/tmp"],
      ["/nix/store", GUEST_TOOLCHAIN_PREFIX],
    ]
    candidates.uniq(&:first)
  end

  def compiler_prefix_maps(root, dependencies)
    remapped_paths(root, dependencies).flat_map do |source, destination|
      %W[
        -ffile-prefix-map=#{source}=#{destination}
        -fdebug-prefix-map=#{source}=#{destination}
        -fmacro-prefix-map=#{source}=#{destination}
      ]
    end.join(" ")
  end

  def validate_kandelo_artifact!(wasm, root)
    guards = root/"scripts/wasm-artifact-guards.sh"
    system "bash", "-c", <<~SH, "spidermonkey-artifact-guard", guards, wasm, root
      set -euo pipefail
      . "$1"
      expected_abi=$(wasm_current_abi_version "$3")
      artifact_abi=$(wasm_extract_abi_version "$2")
      if [ -z "$artifact_abi" ] || [ "$artifact_abi" != "$expected_abi" ]; then
        echo "ERROR: SpiderMonkey ABI ${artifact_abi:-missing} does not match Kandelo ABI $expected_abi" >&2
        exit 1
      fi
      wasm_require_no_legacy_asyncify "$2"
      wasm_require_no_fork_instrumentation "$2"
      if ! wasm_imports_kernel_fork "$2"; then
        echo "ERROR: SpiderMonkey disabled-fork policy drifted; review its runtime/VFS status" >&2
        exit 1
      fi
    SH
  end

  def reject_builder_paths!(wasm, root, dependencies)
    binary = File.binread(wasm)
    formula_buildpath = buildpath
    build_paths = []
    unless formula_buildpath.nil?
      build_paths << formula_buildpath.to_s
      build_paths << formula_buildpath.realpath.to_s if formula_buildpath.exist?
    end
    dependency_paths = dependencies.values.flat_map do |path|
      [path.to_s, path.realpath.to_s]
    end
    markers = [
      *build_paths,
      *dependency_paths,
      root.to_s,
      prefix.to_s,
      "/private/tmp/",
      "/nix/store/",
      "/opt/homebrew/Cellar/",
      "/usr/local/Cellar/",
      "/usr/src/tmp/",
    ].uniq
    markers.each do |marker|
      odie "SpiderMonkey contains builder path marker #{marker}" if binary.include?(marker)
    end
    odie "SpiderMonkey contains a builder home path" if binary.match?(%r{/Users/[^/]+/})
  end
end
