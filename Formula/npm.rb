require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Npm < Formula
  include KandeloFormulaSupport

  KANDELO_SOURCE_COMMIT = "88d26f4c627a363e01e567574916aff4e00828ee".freeze
  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/npm".freeze
  GUEST_NODE = "/home/linuxbrew/.linuxbrew/opt/node/bin/node".freeze
  GUEST_NPM_ROOT = "#{GUEST_OPT_PREFIX}/libexec/npm".freeze
  GUEST_SUPPORT_ROOT = "#{GUEST_OPT_PREFIX}/libexec/kandelo".freeze
  UPSTREAM_FILE_COUNT = 2_408

  NPM_RUNNER = <<~JAVASCRIPT.freeze
    const invoked = process.argv[2] || "npm";
    process.argv.splice(2, 1);

    const npmRoot = "#{GUEST_NPM_ROOT}";
    process.argv[1] =
      invoked === "npx"
        ? `${npmRoot}/bin/npm-cli.js`
        : "#{GUEST_OPT_PREFIX}/bin/npm";
    if (invoked === "npx") {
      process.argv.splice(2, 0, "exec");
    }

    const run = require(`${npmRoot}/lib/cli.js`);
    let settled = false;
    let failure = null;
    Promise.resolve(run(process)).then(
      () => {
        settled = true;
      },
      (error) => {
        failure = error;
        settled = true;
      },
    );

    const sleepView =
      typeof SharedArrayBuffer === "function" && typeof Atomics === "object"
        ? new Int32Array(new SharedArrayBuffer(4))
        : null;

    function pumpSpiderMonkeyJobs() {
      if (typeof drainJobQueue === "function") drainJobQueue();
      if (typeof __kandeloRunDueTimers === "function") __kandeloRunDueTimers();
      if (sleepView && typeof __kandeloNextTimerDelay === "function") {
        const delay = __kandeloNextTimerDelay();
        if (delay > 0) {
          try {
            Atomics.wait(sleepView, 0, 0, Math.min(delay, 5));
          } catch {}
        }
      }
    }

    // WHY: SpiderMonkey embeds Node's promise/timer surface without Node's
    // native event loop. npm returns a promise, so the command must explicitly
    // advance both queues until that promise settles. The time bound turns a
    // compatibility regression into a truthful failure instead of leaving a
    // guest process hung.
    let spins = 0;
    const started = Date.now();
    while (!settled && typeof drainJobQueue === "function") {
      pumpSpiderMonkeyJobs();
      if (++spins > 500000 && Date.now() - started > 300000) {
        failure = new Error(
          "npm did not settle after draining the SpiderMonkey job queue",
        );
        settled = true;
      }
    }

    if (failure) {
      console.error(failure && failure.stack ? failure.stack : failure);
      process.exitCode = process.exitCode || 1;
    }
    pumpSpiderMonkeyJobs();
    process.exit(process.exitCode || 0);
  JAVASCRIPT

  NPM_DISPLAY_SHIM = <<~JAVASCRIPT.freeze
    function plain(...args) {
      return args.map((arg) => String(arg)).join(" ");
    }

    function makeChalk() {
      const fn = (...args) => plain(...args);
      return new Proxy(fn, {
        apply(_target, _thisArg, args) {
          return plain(...args);
        },
        get(target, property) {
          if (property === "level") return 0;
          if (property === "supportsColor") return false;
          if (property === "constructor") return Chalk;
          if (property === Symbol.toStringTag) return "Function";
          return target;
        },
      });
    }

    class Chalk {
      constructor() {
        return makeChalk();
      }
    }

    function createSupportsColor() {
      return {
        level: 0,
        hasBasic: false,
        has256: false,
        has16m: false,
      };
    }

    module.exports = { Chalk, createSupportsColor };
  JAVASCRIPT

  NPM_IS_CIDR_SHIM = <<~JAVASCRIPT.freeze
    function isCidrV4(value) {
      const match = String(value).match(
        /^([0-9]{1,3}(?:\\.[0-9]{1,3}){3})\\/(3[0-2]|[12]?[0-9])$/,
      );
      if (!match) return false;
      return match[1].split(".").every((part) => Number(part) <= 255);
    }

    function isCidrV6(value) {
      const text = String(value);
      const slash = text.lastIndexOf("/");
      if (slash < 0) return false;
      const prefix = Number(text.slice(slash + 1));
      if (!Number.isInteger(prefix) || prefix < 0 || prefix > 128) return false;
      const address = text.slice(0, slash);
      return /^[0-9a-fA-F:]+$/.test(address) && address.includes(":");
    }

    module.exports = { v4: isCidrV4, v6: isCidrV6 };
  JAVASCRIPT

  NPM_LAUNCHER = <<~JAVASCRIPT.freeze
    #!#{GUEST_NODE}
    process.argv.splice(2, 0, "npm");
    require("#{GUEST_SUPPORT_ROOT}/npm-runner.js");
  JAVASCRIPT

  NPX_LAUNCHER = <<~JAVASCRIPT.freeze
    #!#{GUEST_NODE}
    process.argv.splice(2, 0, "npx");
    require("#{GUEST_SUPPORT_ROOT}/npm-runner.js");
  JAVASCRIPT

  desc "Node package manager and complete runtime tree for Kandelo"
  homepage "https://www.npmjs.com/"
  url "https://registry.npmjs.org/npm/-/npm-10.9.2.tgz"
  version "10.9.2"
  sha256 "5cd1e5ab971ea6333f910bc2d50700167c5ef4e66da279b2a3efc874c6b116e4"
  license all_of: ["Artistic-2.0", "GPL-2.0-or-later"]

  depends_on "kandelo-dev/tap-core/node"

  resource "kandelo-gpl-license" do
    url "https://raw.githubusercontent.com/Automattic/kandelo/#{KANDELO_SOURCE_COMMIT}/COPYING"
    sha256 "ead02ff1f91603ff84965fe76e86976a3587dc7faf45fb48affe02536b744b86"
  end

  def install
    kandelo_require_arch!("wasm32")

    # WHY: npm loads thousands of bundled JavaScript modules by relative path.
    # Keep that complete tree as one Formula-level lazy unit; per-file lazy
    # placeholders would turn ordinary require() calls into a fragile package
    # protocol. Node remains a separate declared dependency because its large
    # engine bytes have a different owner and can be shared with non-npm uses.
    npm_root = libexec/"npm"
    npm_root.install buildpath.children
    validate_upstream_tree!(npm_root)

    support_root = libexec/"kandelo"
    support_root.mkpath
    (support_root/"npm-runner.js").write NPM_RUNNER
    (support_root/"npm-display-shim.js").write NPM_DISPLAY_SHIM
    (support_root/"is-cidr-shim.js").write NPM_IS_CIDR_SHIM
    bin.mkpath
    (bin/"npm").write NPM_LAUNCHER
    (bin/"npx").write NPX_LAUNCHER
    # Stable opt-prefix shebangs let the same bottle launch from the default
    # shell and from derived Node demo images without capturing a build Cellar.
    chmod 0755, bin/"npm", bin/"npx"

    patch_for_spidermonkey!(npm_root)

    # Keep upstream's complete documentation tree in the runtime root because
    # npm's own help paths resolve relative to that tree. These ordinary
    # Homebrew man links expose the same files without copying their bytes into
    # a second bottle-owned location.
    {
      man1 => npm_root/"man/man1",
      man5 => npm_root/"man/man5",
      man7 => npm_root/"man/man7",
    }.each do |destination, source|
      source.children.each { |page| destination.install_symlink page }
    end
    # WHY: The complete npm tree keeps its Artistic license, while the
    # Kandelo-authored runner and compatibility shims embedded above are GPL.
    # Install both texts so the bottle's declared combined license is auditable.
    license_root = share/"licenses/npm"
    license_root.install_symlink npm_root/"LICENSE" => "LICENSE-Artistic-2.0"
    resource("kandelo-gpl-license").stage do
      license_root.install "COPYING" => "COPYING-GPL-2.0-or-later"
    end
  end

  test do
    npm_root = libexec/"npm"
    manifest = JSON.parse((npm_root/"package.json").read)
    assert_equal "npm", manifest.fetch("name")
    assert_equal version.to_s, manifest.fetch("version")
    assert_equal "Artistic-2.0", manifest.fetch("license")
    assert_equal({ "npm" => "bin/npm-cli.js", "npx" => "bin/npx-cli.js" },
      manifest.fetch("bin"))
    assert_equal UPSTREAM_FILE_COUNT, npm_root.find.count(&:file?)
    assert_path_exists npm_root/"LICENSE"
    assert_path_exists npm_root/"lib/cli.js"
    assert_path_exists npm_root/"node_modules/@npmcli/arborist/lib/arborist/index.js"
    assert_path_exists npm_root/"node_modules/pacote/lib/index.js"
    assert_path_exists npm_root/"node_modules/tar/index.js"
    assert_path_exists man1/"npm-install.1"
    assert_path_exists man5/"package-json.5"
    assert_path_exists man7/"package-spec.7"
    assert_path_exists share/"licenses/npm/LICENSE-Artistic-2.0"
    assert_path_exists share/"licenses/npm/COPYING-GPL-2.0-or-later"

    display = (npm_root/"lib/utils/display.js").read
    assert_includes display,
      "require('#{GUEST_SUPPORT_ROOT}/npm-display-shim.js')"
    refute_includes display, "import('chalk')"
    token = (npm_root/"lib/commands/token.js").read
    assert_includes token,
      "require('#{GUEST_SUPPORT_ROOT}/is-cidr-shim.js')"
    refute_includes token, "import('is-cidr')"
    %w[
      node_modules/cacache/lib/entry-index.js
      node_modules/cacache/lib/verify.js
    ].each do |relative|
      source = (npm_root/relative).read
      assert_includes source, "const pMap = require('p-map')"
      refute_includes source, "import('p-map')"
    end

    assert_equal "#!#{GUEST_NODE}\n", (bin/"npm").read.lines.first
    assert_equal "#!#{GUEST_NODE}\n", (bin/"npx").read.lines.first
    assert_includes (bin/"npm").read,
      "require(\"#{GUEST_SUPPORT_ROOT}/npm-runner.js\")"
    assert_includes (bin/"npx").read,
      "require(\"#{GUEST_SUPPORT_ROOT}/npm-runner.js\")"

    node_prefix = formula_opt_prefix("kandelo-dev/tap-core/node")
    node = node_prefix/"bin/node"
    assert_path_exists node
    refute node.to_s.start_with?("#{prefix}/")
    # WHY: npm is a composable data/runtime tree. Its Node Formula dependency
    # remains the sole view of SpiderMonkey's Wasm bytes; including any Wasm in
    # this bottle would create a second drifting owner for the same engine.
    assert_empty prefix.glob("**/*.wasm")

    fixture_root = testpath/"fixture/kandelo-npm-fixture"
    fixture_root.mkpath
    (fixture_root/"package.json").write(JSON.generate({
      "name"    => "kandelo-npm-fixture",
      "version" => "1.0.0",
      "main"    => "index.js",
      "bin"     => { "kandelo-npm-fixture" => "cli.js" },
      "license" => "MIT",
    }))
    (fixture_root/"index.js").write("module.exports = { answer: 42 };\n")
    (fixture_root/"cli.js").write <<~JAVASCRIPT
      #!#{GUEST_NODE}
      const fixture = require("./index.js");
      console.log(`npm-fixture-ok:${process.argv[2]}:${fixture.answer}`);
    JAVASCRIPT
    chmod 0755, fixture_root/"cli.js"

    work_manifest = testpath/"work-package.json"
    work_manifest.write(JSON.generate({
      "name"    => "kandelo-npm-formula-test",
      "version" => "1.0.0",
      "private" => true,
    }))

    guest_files = npm_runtime_guest_files.merge(
      "/fixtures/kandelo-npm-fixture/package.json" => fixture_root/"package.json",
      "/fixtures/kandelo-npm-fixture/index.js"     => fixture_root/"index.js",
      "/fixtures/kandelo-npm-fixture/cli.js"       => fixture_root/"cli.js",
      "/work/package.json"                         => work_manifest,
    )
    exec_programs = { GUEST_NODE => node }
    env = {
      "HOME"                       => "/work",
      "PWD"                        => "/work",
      "KERNEL_CWD"                 => "/work",
      "PATH"                       => "#{GUEST_OPT_PREFIX}/bin:#{GUEST_NODE.delete_suffix("/node")}:/usr/bin:/bin",
      "TMPDIR"                     => "/tmp",
      "TERM"                       => "dumb",
      "npm_config_audit"           => "false",
      "npm_config_cache"           => "/tmp/npm-cache",
      "npm_config_color"           => "false",
      "npm_config_fund"            => "false",
      "npm_config_offline"         => "true",
      "npm_config_progress"        => "false",
      "npm_config_update_notifier" => "false",
      "TIMEOUT"                    => "300000",
    }
    package_spec = "file:/fixtures/kandelo-npm-fixture"
    common_args = [
      "--offline", "--yes", "--cache=/tmp/npm-cache",
      "--package=#{package_spec}", "--", "kandelo-npm-fixture"
    ]

    # No test host grants a network backend, and npm is explicitly offline.
    # The only package source is this staged guest directory, so a passing
    # result proves real npm installation, bin linking, module resolution, and
    # Node execution without a registry response or a mocked success path.
    node_output = kandelo_run_wasm(
      node,
      ["#{GUEST_SUPPORT_ROOT}/npm-runner.js", "npm", "exec", *common_args, "node"],
      argv0:                     GUEST_NODE,
      env:                       env,
      exec_programs:             exec_programs,
      guest_files:               guest_files,
      merge_stderr:              true,
      expected_fork_descendants: 1,
    )
    assert_includes node_output, "npm-fixture-ok:node:42"

    browser_output = kandelo_run_browser_wasm(
      node,
      ["#{GUEST_SUPPORT_ROOT}/npm-runner.js", "npx", *common_args, "chromium"],
      argv0:                    "node",
      guest_program_path:       GUEST_NODE,
      env:                      env.except("KERNEL_CWD", "TIMEOUT"),
      exec_programs:            exec_programs,
      guest_files:              guest_files,
      merge_stderr:             true,
      timeout_ms:               300_000,
      max_process_memory_bytes: 512 * 1024 * 1024,
    )
    assert_includes browser_output, "npm-fixture-ok:chromium:42"
  end

  private

  def validate_upstream_tree!(npm_root)
    package_json = JSON.parse((npm_root/"package.json").read)
    invalid = package_json.fetch("name", nil) != "npm" ||
              package_json.fetch("version", nil) != version.to_s ||
              package_json.fetch("license", nil) != "Artistic-2.0" ||
              npm_root.find.count(&:file?) != UPSTREAM_FILE_COUNT
    odie "npm source archive does not match the pinned complete runtime tree" if invalid
  rescue JSON::ParserError => e
    odie "npm source package.json is invalid JSON: #{e.message}"
  end

  def patch_for_spidermonkey!(npm_root)
    # WHY: npm 10 uses a few dynamic CommonJS-to-ESM imports. Kandelo's
    # SpiderMonkey-backed Node compatibility runtime cannot yet bridge those
    # package shapes, so keep the compatibility boundary in npm's own bottle
    # and require exact source matches before changing the pinned archive.
    replace_exact!(
      npm_root/"lib/utils/display.js",
      "const [{ Chalk }, { createSupportsColor }] = await Promise.all([\n      " \
      "import('chalk'),\n      " \
      "import('supports-color'),\n    " \
      "])",
      "const { Chalk, createSupportsColor } = " \
      "require('#{GUEST_SUPPORT_ROOT}/npm-display-shim.js')",
      expected: 1,
    )
    replace_exact!(
      npm_root/"lib/commands/token.js",
      "const { v4: isCidrV4, v6: isCidrV6 } = await import('is-cidr')",
      "const { v4: isCidrV4, v6: isCidrV6 } = " \
      "require('#{GUEST_SUPPORT_ROOT}/is-cidr-shim.js')",
      expected: 1,
    )
    {
      "node_modules/cacache/lib/entry-index.js" => 1,
      "node_modules/cacache/lib/verify.js"      => 2,
    }.each do |relative, expected|
      replace_exact!(
        npm_root/relative,
        "const { default: pMap } = await import('p-map')",
        "const pMap = require('p-map')",
        expected: expected,
      )
    end
  end

  def replace_exact!(path, before, after, expected:)
    source = path.read
    matches = source.scan(Regexp.new(Regexp.escape(before))).length
    odie "npm compatibility patch expected #{expected} match(es) in #{path}, found #{matches}" if matches != expected
    path.write(source.gsub(before, after))
  end

  def npm_runtime_guest_files
    {
      libexec/"npm"     => GUEST_NPM_ROOT,
      libexec/"kandelo" => GUEST_SUPPORT_ROOT,
    }.each_with_object({}) do |(host_root, guest_root), files|
      host_root.glob("**/*").each do |host_path|
        next unless host_path.file?

        relative = host_path.relative_path_from(host_root)
        files["#{guest_root}/#{relative}"] = host_path
      end
    end
  end
end
