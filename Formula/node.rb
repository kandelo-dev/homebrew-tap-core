require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Node < Formula
  include KandeloFormulaSupport

  KANDELO_SOURCE_COMMIT = "88d26f4c627a363e01e567574916aff4e00828ee".freeze
  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/node".freeze
  NODE_COMPAT_COMPONENT_SHA256 = {
    "adapter"   => "381433bd5b55e2269feb905e70474abfaeba06fb51f546ec8ea2b38fc3a5e60a",
    "bootstrap" => "799e78a91f00b203a701dc353a47e038b82e9c37b9ae7b7c06bbbf9a87738788",
    "suffix"    => "49fac5a3039bbad287ac815d533cf3abd4b4217920cbe2c93bb34a6e30003142",
  }.freeze
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

  desc "SpiderMonkey-backed Node.js compatibility runtime for Kandelo"
  homepage "https://github.com/Automattic/kandelo"
  url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/packages/registry/node-compat/bootstrap.js",
      using: :nounzip
  version "22.0.0"
  sha256 "799e78a91f00b203a701dc353a47e038b82e9c37b9ae7b7c06bbbf9a87738788"
  license all_of: ["MPL-2.0", "GPL-2.0-or-later"]

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  depends_on "kandelo-dev/tap-core/spidermonkey"

  skip_clean "bin/node"
  skip_clean "bin/spidermonkey-node"

  def install
    kandelo_require_arch!("wasm32")

    spidermonkey = formula_opt_prefix("kandelo-dev/tap-core/spidermonkey")
    engine = spidermonkey/"bin/js"
    odie "SpiderMonkey dependency did not install bin/js" unless engine.file?
    manifest = read_node_compat_manifest(spidermonkey)

    # WHY: Node mode is selected by argv[0] inside the SpiderMonkey module.
    # A symlink keeps one authoritative Wasm byte sequence in the dependency
    # bottle; copying it here would publish two independent packages for the
    # same engine and let their provenance drift.
    if Digest::SHA256.file(engine).hexdigest != manifest.dig("engine", "sha256")
      odie "SpiderMonkey Node compatibility manifest does not describe its installed engine"
    end
    odie "SpiderMonkey Node compatibility import surface changed" if wasm_imports(engine) != EXPECTED_IMPORTS
    kandelo_validate_wasm_artifact(engine, fork: :disabled)
    bin.install_symlink engine => "node"
    bin.install_symlink "node" => "spidermonkey-node"

    source_files = buildpath.children.select(&:file?)
    odie "Node compatibility source did not stage as one file" if source_files.length != 1
    bootstrap = source_files.fetch(0)
    if Digest::SHA256.file(bootstrap).hexdigest != NODE_COMPAT_COMPONENT_SHA256.fetch("bootstrap")
      odie "Node compatibility source differs from the engine provenance"
    end
    (share/"kandelo/node-compat").install bootstrap => "bootstrap.js"

    engine_licenses = spidermonkey/"share/licenses/spidermonkey"
    %w[LICENSE-MPL-2.0 COPYING-GPL-2.0-or-later].each do |license_name|
      engine_license = engine_licenses/license_name
      odie "SpiderMonkey dependency did not install #{license_name}" unless engine_license.file?
      (share/"licenses/node").install_symlink engine_license
    end
  end

  def caveats
    <<~EOS
      This command is Kandelo's SpiderMonkey-backed Node.js API compatibility
      runtime. It reports the compatibility target v22.0.0, but it is not the
      upstream Node.js or V8 runtime.

      The child_process exec/spawn surface is a popen-backed compatibility
      subset; child_process.fork, native .node addons, and V8-specific APIs are
      not implemented. worker_threads supports the tested eval-worker and
      SharedArrayBuffer/Atomics subset, not the complete Node worker protocol.
    EOS
  end

  test do
    spidermonkey = formula_opt_prefix("kandelo-dev/tap-core/spidermonkey")
    engine = spidermonkey/"bin/js"
    manifest = read_node_compat_manifest(spidermonkey)
    assert_equal engine.realpath, (bin/"node").realpath
    assert_equal engine.realpath, (bin/"spidermonkey-node").realpath
    assert_equal manifest.dig("engine", "sha256"), Digest::SHA256.file(bin/"node").hexdigest
    assert_equal EXPECTED_IMPORTS, wasm_imports(bin/"node")
    assert_path_exists share/"kandelo/node-compat/bootstrap.js"
    assert_equal NODE_COMPAT_COMPONENT_SHA256.fetch("bootstrap"),
      Digest::SHA256.file(share/"kandelo/node-compat/bootstrap.js").hexdigest
    assert_path_exists share/"licenses/node/LICENSE-MPL-2.0"
    assert_path_exists share/"licenses/node/COPYING-GPL-2.0-or-later"

    guest_sources = {
      "/opt/node-formula-test/data.json"                => JSON.generate({ "value" => 41 }),
      "/opt/node-formula-test/helper.js"                => <<~JAVASCRIPT,
        const data = require("./data.json");
        exports.value = data.value + 1;
      JAVASCRIPT
      "/opt/node-formula-test/node_modules/pkg/main.js" => <<~JAVASCRIPT,
        module.exports = "pkg-main";
      JAVASCRIPT
    }
    guest_sources["/opt/node-formula-test/node_modules/pkg/package.json"] =
      JSON.generate({ "name" => "pkg", "main" => "main.js" })
    guest_sources["/opt/node-formula-test/package.json"] =
      JSON.generate({ "type" => "commonjs" })
    guest_files = {}
    guest_sources.each do |guest_path, contents|
      host_path = testpath/guest_path.delete_prefix("/")
      host_path.dirname.mkpath
      host_path.write(contents)
      guest_files[guest_path] = host_path
    end

    surface_script = <<~JAVASCRIPT
      const assert = require("node:assert");
      const crypto = require("crypto");
      const fs = require("fs");
      const fsp = require("node:fs/promises");
      const path = require("path");
      const util = require("util");
      const zlib = require("zlib");
      const { EventEmitter, once } = require("events");
      const helper = require("/opt/node-formula-test/helper");
      const pkg = require("/opt/node-formula-test/node_modules/pkg");

      assert.strictEqual(process.arch, "wasm32");
      assert.strictEqual(process.platform, "linux");
      assert.strictEqual(process.version, "v22.0.0");
      const bytes = Buffer.from("hello");
      assert.strictEqual(bytes.toString("hex"), "68656c6c6f");
      assert.strictEqual(path.join("/usr", "bin", "node"), "/usr/bin/node");
      assert.strictEqual(util.format("%s:%d", path.basename("/usr/bin/node"), bytes.length), "node:5");
      assert.strictEqual(helper.value, 42);
      assert.strictEqual(pkg, "pkg-main");
      assert.strictEqual(
        crypto.createHash("sha256").update("abc").digest("hex"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
      );
      assert.strictEqual(
        crypto.createHmac("sha256", "Jefe").update("what do ya want for nothing?").digest("hex"),
        "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
      );
      assert.strictEqual(
        zlib.gunzipSync(zlib.gzipSync(Buffer.from("kandelo-node"))).toString(),
        "kandelo-node",
      );
      assert.strictEqual(
        zlib.inflateSync(zlib.deflateSync(Buffer.from("kandelo-zlib"))).toString(),
        "kandelo-zlib",
      );

      class ReplayEmitter extends EventEmitter {
        constructor() { super(); this.seen = new Map(); }
        on(event, handler) {
          if (this.seen.has(event)) return handler(...this.seen.get(event));
          return super.on(event, handler);
        }
        emit(event, ...args) {
          this.seen.set(event, args);
          return super.emit(event, ...args);
        }
      }
      const emitter = new ReplayEmitter();
      emitter.emit("integrity", "sha512-test");

      fs.mkdirSync("/tmp/node-formula-test", { recursive: true });
      fs.writeFileSync("/tmp/node-formula-test/file.txt", "hello fs");
      fs.appendFileSync("/tmp/node-formula-test/file.txt", "!");
      let emfile = "missing";
      const descriptors = [];
      try {
        for (let index = 0; index < 2048; index++) {
          descriptors.push(fs.openSync("/tmp/node-formula-test/file.txt", "r"));
        }
      } catch (error) {
        emfile = error.code;
      } finally {
        for (const descriptor of descriptors) fs.closeSync(descriptor);
      }
      assert.strictEqual(emfile, "EMFILE");

      Promise.all([
        fsp.readFile("/tmp/node-formula-test/file.txt", "utf8"),
        once(emitter, "integrity").then(([value]) => value),
      ]).then(([contents, integrity]) => {
        assert.strictEqual(contents, "hello fs!");
        assert.strictEqual(integrity, "sha512-test");
        fs.rmSync("/tmp/node-formula-test", { recursive: true, force: true });
        console.log("node-surface-ok");
      });
      drainJobQueue();
    JAVASCRIPT
    assert_equal "node-surface-ok\n", kandelo_run_wasm(
      bin/"node", ["-e", surface_script],
      argv0: "/usr/bin/node", guest_files: guest_files
    )
    assert_equal "node-surface-ok\n" * 3, kandelo_run_browser_wasm(
      bin/"node", ["-e", surface_script],
      argv0:                    "node",
      guest_program_path:       "/usr/bin/node",
      guest_files:              guest_files,
      timeout_ms:               180_000,
      launch_count:             3,
      max_process_memory_bytes: 512 * 1024 * 1024
    )

    # These are the command identities the shell and dedicated Node demo
    # expose. They must all select Node mode in the shared engine.
    [
      "#{GUEST_OPT_PREFIX}/bin/node",
      "/usr/bin/node",
      "/bin/node",
      "/usr/local/bin/node",
      "/usr/bin/spidermonkey-node",
    ].each do |guest_command|
      assert_equal "v22.0.0\n", kandelo_run_wasm(
        bin/"node", ["--version"], argv0: guest_command
      )
    end
    assert_equal "v22.0.0\n", kandelo_run_browser_wasm(
      bin/"node", ["--version"],
      argv0: "node", guest_program_path: "#{GUEST_OPT_PREFIX}/bin/node", timeout_ms: 180_000
    )

    worker_script = <<~JAVASCRIPT
      const { Worker } = require("worker_threads");
      const shared = new SharedArrayBuffer(8);
      const values = new Int32Array(shared);
      const worker = new Worker(
        "const values = new Int32Array(workerData);" +
        "Atomics.store(values, 0, 42); Atomics.store(values, 1, 1); Atomics.notify(values, 1);",
        { eval: true, workerData: shared },
      );
      if (Atomics.load(values, 1) === 0) Atomics.wait(values, 1, 0, 10000);
      if (Atomics.load(values, 1) !== 1) throw new Error("worker did not finish");
      console.log(Atomics.load(values, 0));
      worker.terminate();
    JAVASCRIPT
    assert_equal "42\n", kandelo_run_wasm(
      bin/"node", ["-e", worker_script], argv0: "#{GUEST_OPT_PREFIX}/bin/node"
    )
    assert_equal "42\n", kandelo_run_browser_wasm(
      bin/"node", ["-e", worker_script],
      argv0: "node", guest_program_path: "#{GUEST_OPT_PREFIX}/bin/node", timeout_ms: 180_000
    )

    esm_guest_path = "/opt/node-formula-esm/bin/tool.js"
    esm_host_path = testpath/esm_guest_path.delete_prefix("/")
    esm_host_path.dirname.mkpath
    esm_host_path.write <<~JAVASCRIPT
      #!/usr/bin/env node
      import path from "path";
      import { createRequire } from "module";
      import { fileURLToPath } from "url";
      const require = createRequire(import.meta.url);
      const filename = fileURLToPath(import.meta.url);
      await Promise.resolve();
      console.log("esm", typeof require, path.basename(filename), process.argv.slice(2).join(","));
    JAVASCRIPT
    esm_package_path = testpath/"opt/node-formula-esm/package.json"
    esm_package_path.write(JSON.generate({ "name" => "node-formula-esm", "type" => "module" }))
    esm_files = {
      esm_guest_path                       => esm_host_path,
      "/opt/node-formula-esm/package.json" => esm_package_path,
    }
    assert_equal "esm function tool.js alpha,beta\n", kandelo_run_wasm(
      bin/"node", [esm_guest_path, "alpha", "beta"],
      argv0: "#{GUEST_OPT_PREFIX}/bin/node", guest_files: esm_files
    )
    assert_equal "esm function tool.js alpha,beta\n", kandelo_run_browser_wasm(
      bin/"node", [esm_guest_path, "alpha", "beta"],
      argv0: "node", guest_program_path: "#{GUEST_OPT_PREFIX}/bin/node",
      guest_files: esm_files, timeout_ms: 180_000
    )

    error_script = "throw new Error('visible node failure')"
    node_error = kandelo_run_wasm(
      bin/"node", ["-e", error_script],
      argv0: "#{GUEST_OPT_PREFIX}/bin/node", merge_stderr: true, expected_status: 1
    )
    assert_includes node_error, "Error: visible node failure"
    browser_error = kandelo_run_browser_wasm(
      bin/"node", ["-e", error_script],
      argv0: "node", guest_program_path: "#{GUEST_OPT_PREFIX}/bin/node",
      merge_stderr: true, expected_status: 1, timeout_ms: 180_000
    )
    assert_includes browser_error, "Error: visible node failure"
  end

  private

  def read_node_compat_manifest(spidermonkey)
    path = spidermonkey/"share/kandelo/spidermonkey/node-compat.json"
    odie "SpiderMonkey dependency has no Node compatibility manifest" unless path.file?
    document = JSON.parse(path.read)
    expected_node_compat = {
      "version"          => version.to_s,
      "source_commit"    => KANDELO_SOURCE_COMMIT,
      "component_sha256" => NODE_COMPAT_COMPONENT_SHA256,
    }
    invalid = document.keys.sort != %w[engine node_compat schema] ||
              document.fetch("schema") != 1 ||
              document.fetch("engine").keys.sort != %w[path sha256] ||
              document.dig("engine", "path") != "bin/js" ||
              document.fetch("node_compat") != expected_node_compat
    odie "SpiderMonkey dependency has incompatible Node composition provenance" if invalid

    document
  rescue JSON::ParserError => e
    odie "SpiderMonkey Node compatibility manifest is invalid JSON: #{e.message}"
  end

  def wasm_imports(path)
    dump = Utils.safe_popen_read("wasm-objdump", "-x", path)
    imports = dump.lines.filter_map do |line|
      line[/<-\s+([A-Za-z0-9_.$-]+\.[A-Za-z0-9_.$-]+)\s*\z/, 1]
    end
    odie "Node import table contains duplicate entries" if imports.uniq.length != imports.length

    imports.sort
  end
end
