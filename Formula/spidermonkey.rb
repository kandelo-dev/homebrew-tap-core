require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Spidermonkey < Formula
  include KandeloFormulaSupport

  KANDELO_SOURCE_COMMIT = "88d26f4c627a363e01e567574916aff4e00828ee".freeze
  PATCH_RESOURCES = %w[
    patch-0001 patch-0002 patch-0003 patch-0004 patch-0005 patch-0006 patch-0007
    patch-0008 patch-0009 patch-0010 patch-0011 patch-0012 patch-0013 patch-0014
  ].freeze
  EXPECTED_IMPORTS = %w[
    env.__channel_base
    env.memory
    kernel.kernel_apply_fork_fd_actions
    kernel.kernel_argv_read
    kernel.kernel_clear_fork_exec
    kernel.kernel_clone
    kernel.kernel_environ_count
    kernel.kernel_environ_get
    kernel.kernel_execve
    kernel.kernel_exit
    kernel.kernel_fork
    kernel.kernel_get_argc
    kernel.kernel_get_fork_exec_argc
    kernel.kernel_get_fork_exec_argv
    kernel.kernel_get_fork_exec_path
    kernel.kernel_is_fork_child
    kernel.kernel_push_argv
  ].freeze

  desc "Mozilla JavaScript engine shell for Kandelo"
  homepage "https://spidermonkey.dev/"
  url "https://ftp.mozilla.org/pub/firefox/releases/140.11.0esr/source/firefox-140.11.0esr.source.tar.xz"
  version "140.11.0esr"
  sha256 "1b034d2117356fda24807a151055132315c6ba58ad2bdf7ec71ee707fac5e028"
  license all_of: ["MPL-2.0", "GPL-2.0-or-later"]

  depends_on "cbindgen" => :build
  depends_on "gpatch" => :build
  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::PkgconfRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  depends_on "llvm" => :build
  depends_on "node" => :build
  depends_on "python@3.13" => :build
  depends_on "rust" => :build
  depends_on "kandelo-dev/tap-core/libcxx"
  depends_on "kandelo-dev/tap-core/openssl"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/js"

  resource "patch-0001" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0001-allow-static-cxx-runtime-for-wasm-linux.patch"
    sha256 "d0c2ccf64c46d11cc512011b8b1b3b72af2e7f6e3bf65b96bd93d41c2c6c9e2e"
  end

  resource "patch-0002" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0002-map-kandelo-wasm-linux-rust-target.patch"
    sha256 "7deeacd2052e7a964aa5ac0147bc313c1d28b5bda7e817fda705660203eecbcf"
  end

  resource "patch-0003" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0003-jsonprinter-size-t-wasm32.patch"
    sha256 "e259107555c745cff184092667548679f9e7e8993e6ab329e3e3afb65c946ff4"
  end

  resource "patch-0004" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0004-disable-wasm32-return-address-stackwalk.patch"
    sha256 "d075352ab5f8ab76620a964891a81063e44543dda10d4b1235da681a9c1c1ea6"
  end

  resource "patch-0005" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0005-getrandom-custom-backend-wasm32.patch"
    sha256 "615eb35b13d9ee74c5bcb5d3b8e4768dd817be71e9f57f310cbe6234c4437aa9"
  end

  resource "patch-0006" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0006-randomnum-use-sys-random-on-wasm32.patch"
    sha256 "03c2496b0e6ddc4bafd8f3b1e84a0034eb0c8228517a9a3b8c3dc921326ae8c7"
  end

  resource "patch-0007" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0007-skip-elf-network-check-for-wasm-target.patch"
    sha256 "778bc6bc2817ed36de9c325f1c20d06b412f45b8ac6e6da94c9c6120a6395ef6"
  end

  resource "patch-0008" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0008-use-wasm-trap-for-moz-crash.patch"
    sha256 "59a87abf141cc4bf40acf11c48ab662ec1ac9e5be896b997bebcf42904d6b95e"
  end

  resource "patch-0009" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0009-use-wasm-frame-address-for-native-stack-base.patch"
    sha256 "5e954b61c692e799e216c8f732cc936ad3be1b8e0aef0db2459f68748cf99cc0"
  end

  resource "patch-0010" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0010-use-wasm-icu-data-section-syntax.patch"
    sha256 "d655d6fc1b13e1b892c01be43ccf421524d8bf45fa0bea17dc3a51967f6309bf"
  end

  resource "patch-0011" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0011-heap-autorunparallel-task-on-wasm32.patch"
    sha256 "62147693a4932d0f431ea4739d9bb73bc51126dceda735dec5314e306ad78637"
  end

  resource "patch-0012" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0012-kandelo-node-compat-shell-entry.patch"
    sha256 "7436168a358bc4a0ba1174bf82f44dcfc23d3992f66f8db2731e2a047ecc38b8"
  end

  resource "patch-0013" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0013-kandelo-join-shell-workers.patch"
    sha256 "f0aa3c64bddba28865089c5796fff310050380e6b7cc85239e6e8b55ccfe9923"
  end

  resource "patch-0014" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/patches/0014-disable-mozglue-interposers-on-wasm32.patch"
    sha256 "f0926fbbcbf085fea29057db1b377ca3e77e740bc84028acda06164be5e9dd46"
  end

  resource "kandelo-node-adapter" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/node-compat/adapter.js"
    sha256 "381433bd5b55e2269feb905e70474abfaeba06fb51f546ec8ea2b38fc3a5e60a"
  end

  resource "kandelo-node-bootstrap" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/node-compat/bootstrap.js"
    sha256 "799e78a91f00b203a701dc353a47e038b82e9c37b9ae7b7c06bbbf9a87738788"
  end

  resource "kandelo-node-suffix" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/spidermonkey/node-compat/suffix.js"
    sha256 "49fac5a3039bbad287ac815d533cf3abd4b4217920cbe2c93bb34a6e30003142"
  end

  resource "kandelo-gpl-license" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/COPYING"
    sha256 "ead02ff1f91603ff84965fe76e86976a3587dc7faf45fb48affe02536b744b86"
  end

  def install
    kandelo_require_arch!("wasm32")
    apply_kandelo_patches!
    generate_kandelo_node_bootstrap!

    libcxx = formula_opt_prefix("kandelo-dev/tap-core/libcxx")
    openssl = formula_opt_prefix("kandelo-dev/tap-core/openssl")
    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")
    host_llvm = formula_opt_prefix("llvm")
    objdir = buildpath/"obj-wasm32"
    mozconfig = buildpath/"mozconfig-wasm32"
    stable_source = "/usr/src/firefox-#{version}"

    kandelo_wasm_build do |root|
      # Mozilla's build contains native generators and target libraries in one
      # graph. Name every declared native tool explicitly, while the CC/CXX
      # variables below keep all engine objects on Kandelo's SDK path.
      kandelo_prepend_path! formula_opt_bin("python@3.13")
      kandelo_prepend_path! formula_opt_bin("rust")
      kandelo_prepend_path! formula_opt_bin("cbindgen")
      kandelo_prepend_path! formula_opt_bin("node")

      prefix_maps = {
        buildpath => stable_source,
        root      => "/usr/src/kandelo",
        libcxx    => "/usr/src/kandelo-deps/libcxx",
        openssl   => "/usr/src/kandelo-deps/openssl",
        zlib      => "/usr/src/kandelo-deps/zlib",
      }.flat_map do |from, to|
        [Pathname(from), Pathname(from).realpath].uniq.flat_map do |source|
          [
            "-ffile-prefix-map=#{source}=#{to}",
            "-fdebug-prefix-map=#{source}=#{to}",
            "-fmacro-prefix-map=#{source}=#{to}",
          ]
        end
      end

      mozconfig.write <<~MOZCONFIG
        export RUSTFLAGS='-Ctarget-feature=+atomics,+bulk-memory,+mutable-globals --cfg=getrandom_backend="custom" --remap-path-prefix=#{buildpath}=#{stable_source}'
        ac_add_options --enable-project=js
        ac_add_options --target=wasm32-unknown-linux-musl
        ac_add_options --disable-debug
        ac_add_options --enable-optimize="-O2"
        ac_add_options --disable-jit
        ac_add_options --disable-jemalloc
        ac_add_options --disable-stdcxx-compat
        ac_add_options --without-system-zlib
        ac_add_options --with-intl-api
        ac_add_options --enable-icu4x
        ac_add_options --disable-shared-js
        ac_add_options --enable-shared-memory
        ac_add_options --disable-clang-plugin
        ac_add_options --disable-tests
        ac_add_options --disable-debug-symbols
        mk_add_options MOZ_OBJDIR=#{objdir}
      MOZCONFIG

      target_defines = "-D__linux__=1 -D__unix__=1"
      ENV["MOZCONFIG"] = mozconfig
      ENV["MOZBUILD_STATE_PATH"] = buildpath/".mozbuild"
      ENV["MACH_BUILD_PYTHON_NATIVE_PACKAGE_SOURCE"] = "system"
      ENV["CC"] = "#{kandelo_cc(root)} #{target_defines}"
      ENV["CXX"] = "#{kandelo_tool("c++", root)} #{target_defines}"
      ENV["AS"] = "#{kandelo_cc(root)} #{target_defines}"
      ENV["AR"] = kandelo_ar(root)
      ENV["RANLIB"] = kandelo_ranlib(root)
      ENV["NM"] = kandelo_tool("nm", root)
      ENV["STRIP"] = kandelo_tool("strip", root)
      ENV["HOST_CC"] = host_llvm/"bin/clang"
      ENV["HOST_CXX"] = host_llvm/"bin/clang++"
      ENV["RUSTC"] = formula_opt_bin("rust")/"rustc"
      ENV["CARGO"] = formula_opt_bin("rust")/"cargo"
      ENV["CBINDGEN"] = formula_opt_bin("cbindgen")/"cbindgen"
      ENV["NODEJS"] = formula_opt_bin("node")/"node"
      ENV["CFLAGS"] = [
        "-O2", "-D_GNU_SOURCE", "-I#{openssl}/include", "-I#{zlib}/include", *prefix_maps
      ].join(" ")
      ENV["CXXFLAGS"] = [
        "-O2", "-D_GNU_SOURCE", "-fexceptions", "-nostdinc++",
        "-isystem", libcxx/"include/c++/v1",
        "-I#{openssl}/include", "-I#{zlib}/include", *prefix_maps
      ].join(" ")
      ENV["LDFLAGS"] = [
        "-L#{libcxx}/lib", "-lc++", "-lc++abi",
        openssl/"lib/libssl.a", openssl/"lib/libcrypto.a", zlib/"lib/libz.a",
        "-Wl,-z,stack-size=16777216"
      ].join(" ")

      system "./mach", "--no-interactive", "build"

      built_shell = [objdir/"dist/bin/js", objdir/"dist/bin/js.wasm"].find(&:file?)
      odie "Mozilla build did not produce the SpiderMonkey shell" if built_shell.nil?

      optimized = buildpath/"js.wasm"
      system "wasm-opt", "-O2", built_shell, "-o", optimized
      validate_import_surface!(optimized)

      # WHY: The universal libc startup object retains kernel_fork, but this
      # engine has no reachable fork API. Rewriting SpiderMonkey's very large
      # C++ control-flow graph overflows Chromium's Wasm call stack before JS
      # starts. Freeze the complete import set above and reject any partial
      # continuation transform until that engine/runtime boundary is removed.
      kandelo_validate_wasm_artifact(
        optimized,
        fork:            :disabled,
        forbidden_paths: [libcxx, openssl, zlib],
      )
      bin.install optimized => "js"
      chmod 0755, bin/"js"
    end

    license_file = buildpath/"LICENSE"
    odie "Firefox source is missing its MPL license file" unless license_file.file?
    license_dir = share/"licenses/spidermonkey"
    license_dir.install license_file => "LICENSE-MPL-2.0"
    (license_dir/"COPYING-GPL-2.0-or-later").binwrite(
      staged_resource_bytes("kandelo-gpl-license"),
    )
  end

  def caveats
    <<~EOS
      This is Mozilla's SpiderMonkey shell, not upstream Node.js. The same
      engine contains Kandelo's Node compatibility entry point, which the
      separate node Formula exposes under the node command.

      The shell does not expose POSIX fork. Its universal libc still retains
      an unreachable kernel_fork import, and the module intentionally remains
      uninstrumented because rewriting this C++ control-flow graph currently
      exhausts Chromium's Wasm call stack. A future reachable fork API must
      remove this exception or fail the Formula's frozen-import validation.
    EOS
  end

  test do
    assert_path_exists share/"licenses/spidermonkey/LICENSE-MPL-2.0"
    assert_path_exists share/"licenses/spidermonkey/COPYING-GPL-2.0-or-later"
    assert_equal EXPECTED_IMPORTS, wasm_imports(bin/"js")

    behavior_source = <<~JAVASCRIPT
      print(1 + 1);
      print([3, 1, 2].toSorted().join(","));
      print(Object.groupBy(["a", "bb", "c"], value => value.length)[1].join(","));
      print(typeof Promise.withResolvers);
      print((2n ** 64n).toString());
      print(typeof Intl);
      print(new Intl.NumberFormat("de-DE").format(1234567.89));
      print(new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "long" })
        .format(new Date(Date.UTC(2020, 0, 2))));
      print(new Intl.PluralRules("en-US").select(1));
      print(new Intl.PluralRules("en-US").select(2));
      print(new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(-1, "day"));
      print(new Intl.Locale("ja-JP-u-ca-japanese").calendar);
      print(new Intl.NumberFormat("ar-EG").format(12345) !== "12345");
      print(Intl.supportedValuesOf("timeZone").includes("UTC"));
      setTimeZone("UTC");
      var utcOffset = new Date(0).getTimezoneOffset();
      setTimeZone("PST8PDT");
      var pacificOffset = new Date(0).getTimezoneOffset();
      setTimeZone("UTC");
      print(utcOffset + "," + pacificOffset);
      try {
        (function recurse() { return 1 + recurse(); })();
      } catch (error) {
        print(error.name + ":" + /recursion|stack/i.test(String(error)));
      }
      function asciiBytes(string) {
        var bytes = new Uint8Array(string.length);
        for (var index = 0; index < string.length; index++) bytes[index] = string.charCodeAt(index);
        return bytes;
      }
      os.file.writeTypedArrayToFile("/tmp/spidermonkey-load.js",
        asciiBytes("var loadedValue = 37; print('loaded:' + loadedValue);\\n"));
      load("/tmp/spidermonkey-load.js");
      print(snarf("/tmp/spidermonkey-load.js").includes("loadedValue"));
      print("unicode:" + "\\u2603" + ":" + "\\u00e9" + ":" + "\\u6f22\\u5b57");
      try { eval("function {"); } catch (error) { print(error.name); }
      var buffer = new ArrayBuffer(8);
      var view = new DataView(buffer);
      view.setUint32(0, 0x12345678, true);
      print(Array.from(new Uint8Array(buffer).slice(0, 4)).join(","));
      var target = { value: 42 };
      var weak = new WeakRef(target);
      new FinalizationRegistry(() => {}).register(target, "held");
      print(weak.deref().value);
      var total = 0;
      for (var round = 0; round < 5; round++) {
        var values = [];
        for (var item = 0; item < 10000; item++) values.push({ item, text: "value-" + item });
        total += values[9999].item;
        if (typeof gc === "function") gc();
      }
      print(total);
      var order = [];
      Promise.resolve().then(() => order.push("promise"));
      order.push("sync");
      drainJobQueue();
      print(order.join(","));
      print(typeof WebAssembly);
      print(typeof wasmIsSupported === "function" ? wasmIsSupported() : "missing");
    JAVASCRIPT
    expected_behavior = <<~OUTPUT
      2
      1,2,3
      a,c
      function
      18446744073709551616
      object
      1.234.567,89
      January
      one
      other
      yesterday
      japanese
      true
      true
      0,480
      InternalError:true
      loaded:37
      true
      unicode:☃:é:漢字
      SyntaxError
      120,86,52,18
      42
      49995
      sync,promise
      undefined
      false
    OUTPUT
    assert_equal expected_behavior, kandelo_run_wasm(bin/"js", ["-e", behavior_source])

    worker_source = <<~JAVASCRIPT
      var shared = new SharedArrayBuffer(8);
      var words = new Int32Array(shared);
      print(Atomics.wait(words, 0, 0, 1));
      setSharedObject(shared);
      evalInWorker(`var words = new Int32Array(getSharedObject());
        Atomics.store(words, 0, 42); Atomics.store(words, 1, 1); Atomics.notify(words, 1);`);
      if (Atomics.load(words, 1) === 0) Atomics.wait(words, 1, 0, 10000);
      if (Atomics.load(words, 1) !== 1) throw new Error("worker wait failed");
      joinWorkerThreads();
      print(Atomics.load(words, 0));
      print("after-first-join");
      Atomics.store(words, 0, 0);
      Atomics.store(words, 1, 0);
      evalInWorker(`var words = new Int32Array(getSharedObject());
        Atomics.store(words, 0, 7); Atomics.store(words, 1, 1); Atomics.notify(words, 1);`);
      if (Atomics.load(words, 1) === 0) Atomics.wait(words, 1, 0, 10000);
      if (Atomics.load(words, 1) !== 1) throw new Error("second worker wait failed");
      joinWorkerThreads();
      print(Atomics.load(words, 0));
      print("after-second-join");
      Atomics.store(words, 0, 0);
      Atomics.store(words, 1, 0);
      for (var index = 0; index < 3; index++) {
        evalInWorker(`var words = new Int32Array(getSharedObject());
          Atomics.add(words, 0, 1); Atomics.add(words, 1, 1); Atomics.notify(words, 1);`);
      }
      while (Atomics.load(words, 1) < 3) {
        Atomics.wait(words, 1, Atomics.load(words, 1), 10000);
      }
      joinWorkerThreads();
      print(Atomics.load(words, 0) + "," + Atomics.load(words, 1));
    JAVASCRIPT
    2.times do
      assert_equal "timed-out\n42\nafter-first-join\n7\nafter-second-join\n3,3\n",
        kandelo_run_wasm(bin/"js", ["--shared-memory=on", "-e", worker_source])
    end

    worker_teardown_source = <<~JAVASCRIPT
      var shared = new SharedArrayBuffer(4);
      var words = new Int32Array(shared);
      setSharedObject(shared);
      evalInWorker(`var words = new Int32Array(getSharedObject());
        Atomics.store(words, 0, 1); Atomics.notify(words, 0);`);
      if (Atomics.wait(words, 0, 0, 10000) !== "ok") throw new Error("worker wait failed");
      var garbage = [];
      for (var index = 0; index < 1000; index++) {
        garbage.push({ index, text: "gc-pressure-" + index });
      }
      if (typeof gc === "function") gc();
      print("worker-teardown-ok");
    JAVASCRIPT
    2.times do
      assert_equal "worker-teardown-ok\n",
        kandelo_run_wasm(bin/"js", ["--shared-memory=on", "-e", worker_teardown_source])
    end

    script = testpath/"args.js"
    script.write("print('scriptArgs:' + scriptArgs.join('|'));\n")
    assert_equal "scriptArgs:alpha|beta\n", kandelo_run_wasm(
      bin/"js", ["/opt/spidermonkey-test/args.js", "alpha", "beta"],
      guest_files: { "/opt/spidermonkey-test/args.js" => script }
    )

    assert_equal expected_behavior, kandelo_run_browser_wasm(
      bin/"js", ["-e", behavior_source],
      argv0: "js", timeout_ms: 180_000
    )
    assert_equal "timed-out\n42\nafter-first-join\n7\nafter-second-join\n3,3\n", kandelo_run_browser_wasm(
      bin/"js", ["--shared-memory=on", "-e", worker_source],
      argv0: "js", timeout_ms: 180_000
    )
    assert_equal "scriptArgs:alpha|beta\n", kandelo_run_browser_wasm(
      bin/"js", ["/opt/spidermonkey-test/args.js", "alpha", "beta"],
      argv0: "js", guest_files: { "/opt/spidermonkey-test/args.js" => script },
      timeout_ms: 180_000
    )

    # The engine owns the exact compatibility bootstrap bytes. Exercise that
    # embedded entry point here as well as the standalone JS shell so the
    # later zero-copy node Formula cannot become the first place a bad engine
    # build is discovered.
    node_source = <<~JAVASCRIPT
      const assert = require("assert");
      const crypto = require("crypto");
      const zlib = require("zlib");
      const { execFileSync } = require("child_process");
      const { Worker } = require("worker_threads");

      assert.strictEqual(process.version, "v22.0.0");
      assert.strictEqual(Buffer.from("node-mode").toString("hex"), "6e6f64652d6d6f6465");
      assert.strictEqual(
        crypto.createHash("sha256").update("abc").digest("hex"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
      );
      assert.strictEqual(
        zlib.gunzipSync(zlib.gzipSync(Buffer.from("embedded-node"))).toString(),
        "embedded-node",
      );
      assert.strictEqual(
        execFileSync("/bin/sh", ["-c", "printf child-ok"], { encoding: "utf8" }),
        "child-ok",
      );

      const shared = new SharedArrayBuffer(8);
      const values = new Int32Array(shared);
      const worker = new Worker(
        "const values = new Int32Array(workerData);" +
        "Atomics.store(values, 0, 42); Atomics.store(values, 1, 1); Atomics.notify(values, 1);",
        { eval: true, workerData: shared },
      );
      if (Atomics.load(values, 1) === 0) Atomics.wait(values, 1, 0, 10000);
      assert.strictEqual(Atomics.load(values, 1), 1);
      assert.strictEqual(Atomics.load(values, 0), 42);
      worker.terminate();
      console.log("embedded-node-ok");
    JAVASCRIPT
    assert_equal "embedded-node-ok\n", kandelo_run_wasm(
      bin/"js", ["-e", node_source],
      argv0: "/usr/bin/node", expected_fork_descendants: 1
    )
    assert_equal "embedded-node-ok\n" * 3, kandelo_run_browser_wasm(
      bin/"js", ["-e", node_source],
      argv0:                    "node",
      guest_program_path:       "/usr/bin/node",
      timeout_ms:               180_000,
      launch_count:             3,
      max_process_memory_bytes: 512 * 1024 * 1024
    )

    syntax_error = kandelo_run_wasm(
      bin/"js", ["-e", "function {"],
      merge_stderr: true, expected_status: 3
    )
    assert_includes syntax_error, "SyntaxError"
    uncaught_error = kandelo_run_wasm(
      bin/"js", ["-e", "throw new Error('spidermonkey-boom')"],
      merge_stderr: true, expected_status: 3
    )
    assert_includes uncaught_error, "spidermonkey-boom"
    browser_syntax_error = kandelo_run_browser_wasm(
      bin/"js", ["-e", "function {"],
      argv0: "js", merge_stderr: true, expected_status: 3, timeout_ms: 180_000
    )
    assert_includes browser_syntax_error, "SyntaxError"
    browser_uncaught_error = kandelo_run_browser_wasm(
      bin/"js", ["-e", "throw new Error('spidermonkey-boom')"],
      argv0: "js", merge_stderr: true, expected_status: 3, timeout_ms: 180_000
    )
    assert_includes browser_uncaught_error, "spidermonkey-boom"

    # One Chromium kernel owns all seven launches. This retains the old
    # package's process-leak and initial-memory regression instead of turning
    # seven independent browser boots into weaker evidence.
    browser_stress = <<~JAVASCRIPT
      setTimeZone("UTC");
      setTimeZone("PST8PDT");
      setTimeZone("UTC");
      var deadline = Date.now() + 50;
      while (Date.now() < deadline) {}
      print("stress-ok");
    JAVASCRIPT
    assert_equal "stress-ok\n" * 7, kandelo_run_browser_wasm(
      bin/"js", ["-e", browser_stress],
      argv0:                    "js",
      timeout_ms:               180_000,
      launch_count:             7,
      max_process_memory_bytes: 512 * 1024 * 1024
    )
  end

  private

  def staged_resource_bytes(name)
    bytes = nil
    resource(name).stage do
      files = Pathname.pwd.children.select(&:file?)
      odie "resource #{name} did not stage exactly one file" if files.length != 1

      bytes = files.fetch(0).binread
    end
    odie "resource #{name} was empty" if bytes.blank?

    bytes
  end

  def apply_kandelo_patches!
    # Homebrew names GNU patch `gpatch` on macOS to avoid shadowing the
    # platform tool; Linux bottles retain its upstream executable name.
    host_patch = formula_opt_bin("gpatch")/(OS.mac? ? "gpatch" : "patch")
    PATCH_RESOURCES.each do |name|
      resource(name).stage do
        files = Pathname.pwd.children.select(&:file?)
        odie "resource #{name} did not stage exactly one patch" if files.length != 1

        system host_patch, "-d", buildpath, "-p1", "--forward", "--batch", "-i", files.fetch(0)
      end
    end
  end

  def generate_kandelo_node_bootstrap!
    adapter = staged_resource_bytes("kandelo-node-adapter").force_encoding(Encoding::UTF_8)
    shared = staged_resource_bytes("kandelo-node-bootstrap").force_encoding(Encoding::UTF_8)
    suffix = staged_resource_bytes("kandelo-node-suffix").force_encoding(Encoding::UTF_8)
    [adapter, shared, suffix].each do |source|
      odie "Kandelo Node bootstrap resource is not UTF-8" unless source.valid_encoding?
    end
    shared_lines = shared.lines.reject { |line| line.start_with?("import * as ") }
    source = "#{adapter.rstrip}\n#{shared_lines.join.rstrip}\n#{suffix.rstrip}\n".b
    header = buildpath/"js/src/shell/kandelo-node-bootstrap.h"
    header.dirname.mkpath
    header.open("w") do |file|
      file << "#ifndef shell_kandelo_node_bootstrap_h\n"
      file << "#define shell_kandelo_node_bootstrap_h\n\n"
      file << "static const unsigned char kKandeloNodeBootstrap[] = {\n"
      source.bytes.each_slice(12) do |slice|
        file << "  #{slice.map { |byte| format("0x%02x", byte) }.join(", ")},\n"
      end
      file << "};\n"
      file << "static const size_t kKandeloNodeBootstrapLen = sizeof(kKandeloNodeBootstrap);\n"
      file << "\n#endif  // shell_kandelo_node_bootstrap_h\n"
    end
  end

  def wasm_imports(path)
    dump = Utils.safe_popen_read("wasm-objdump", "-x", path)
    imports = dump.lines.filter_map do |line|
      line[/<-\s+([A-Za-z0-9_.$-]+\.[A-Za-z0-9_.$-]+)\s*\z/, 1]
    end
    odie "SpiderMonkey import table contains duplicate entries" if imports.uniq.length != imports.length

    imports.sort
  end

  def validate_import_surface!(path)
    actual = wasm_imports(path)
    return if actual == EXPECTED_IMPORTS

    odie "SpiderMonkey import surface changed:\nexpected #{EXPECTED_IMPORTS.inspect}\nactual #{actual.inspect}"
  end
end
