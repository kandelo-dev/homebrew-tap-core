require "find"
require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Erlang < Formula
  include KandeloFormulaSupport

  desc "Functional programming language runtime and toolchain for Kandelo"
  homepage "https://www.erlang.org/"
  url "https://github.com/erlang/otp/archive/refs/tags/OTP-28.2.tar.gz"
  version "28.2"
  sha256 "b984f9e02bb61637997a35daa9070ae8f41cea1667676416438c467fda3d141f"
  license "Apache-2.0"

  depends_on "binaryen" => :build
  depends_on "bison" => :build
  depends_on "erlang@28" => :build
  depends_on "make" => :build
  depends_on "perl" => :build
  depends_on "wabt" => :build

  skip_clean "bin"
  skip_clean "lib/erlang"

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/erlang".freeze
  GUEST_RUNTIME_ROOT = "#{GUEST_OPT_PREFIX}/lib/erlang".freeze

  # OTP needs exact Wasm callback signatures, musl resolver selection, and a
  # narrow LLVM 21 code-generation boundary for one mutable stack traversal.
  patch :DATA

  def install
    kandelo_require_arch!("wasm32")

    build_triplet = Utils.safe_popen_read(buildpath/"make/autoconf/config.guess").strip
    native_cc_root = ENV["NIX_CC_FOR_BUILD"].to_s
    native_cc_root = ENV["NIX_CC"].to_s if native_cc_root.empty?
    if native_cc_root.empty?
      native_cc_root = Utils.safe_popen_read(
        kandelo_host_tool("sh"), "-c", 'printf %s "${NIX_CC_FOR_BUILD:-${NIX_CC:-}}"'
      ).strip
    end
    native_cc = Pathname(native_cc_root)/"bin/cc"
    native_cxx = Pathname(native_cc_root)/"bin/c++"
    odie "Kandelo dev shell did not provide a native C compiler" unless native_cc.executable?
    odie "Kandelo dev shell did not provide a native C++ compiler" unless native_cxx.executable?

    # OTP first builds the native bootstrap compiler that generates target
    # BEAM files. Absolute Nix compilers avoid Homebrew's target dependency
    # shims entering this host phase.
    %w[CFLAGS CPPFLAGS CXXFLAGS LDFLAGS MACOSX_DEPLOYMENT_TARGET].each { |key| ENV.delete(key) }
    ENV["CC"] = native_cc.to_s
    ENV["CXX"] = native_cxx.to_s
    ENV["LD"] = native_cc.to_s
    ENV["ERL_TOP"] = buildpath.to_s
    system "./configure", "--enable-bootstrap-only", "--enable-deterministic-build"
    system "gmake", "-j#{ENV.make_jobs}"
    odie "OTP native bootstrap did not produce erlc" unless (buildpath/"bootstrap/bin/erlc").executable?

    # OTP's clean target preserves the completed bootstrap while removing the
    # native configuration, generated modules, and objects before configure.
    system "gmake", "clean"
    odie "OTP clean removed the native bootstrap" unless (buildpath/"bootstrap/bin/erlc").executable?
    # The top-level clean target intentionally preserves preloaded BEAM files.
    # Rebuild them under the target's deterministic compiler configuration.
    system "gmake", "-C", "erts/preloaded/src", "clean"

    # OTP exposes its full configure-time flags through
    # erlang:system_info(compile_info). Preserve those flags while mapping the
    # ephemeral source roots in the recorded copy, just as the compiler maps
    # them in debug and __FILE__ data.
    inreplace "erts/emulator/utils/make_compiler_flags",
      "    my $value = $constants{$_};\n",
      <<~'PERL'
        my $value = $constants{$_};
            if (defined $ENV{KANDELO_OTP_BUILD_ROOT}) {
                $value =~ s/\Q$ENV{KANDELO_OTP_BUILD_ROOT}\E/\/usr\/src\/erlang/g;
            }
            if (defined $ENV{KANDELO_OTP_PLATFORM_ROOT}) {
                $value =~ s/\Q$ENV{KANDELO_OTP_PLATFORM_ROOT}\E/\/usr\/src\/kandelo/g;
            }
      PERL

    strict_wrapper = Pathname(__dir__).parent/"Kandelo/formula_support/strict-wasm-link-cc.sh"
    strict_cc = buildpath/"strict-cc"
    strict_cxx = buildpath/"strict-c++"
    cp strict_wrapper, strict_cc
    cp strict_wrapper, strict_cxx
    chmod 0755, strict_cc
    chmod 0755, strict_cxx

    release_root = buildpath/"kandelo-release"
    kandelo_wasm_build do |root|
      ENV.prepend_path "PATH", formula_opt_bin("wabt")
      ENV["CONFIG_SITE"] = "#{root}/sdk/config.site"
      ENV["KANDELO_STRICT_REAL_CC"] = kandelo_cc(root)
      ENV["KANDELO_STRICT_REAL_CXX"] = kandelo_tool("c++", root)
      ENV["KANDELO_OTP_BUILD_ROOT"] = buildpath.to_s
      ENV["KANDELO_OTP_PLATFORM_ROOT"] = root
      ENV["CC"] = strict_cc.to_s
      ENV["CXX"] = strict_cxx.to_s
      ENV["LD"] = strict_cc.to_s
      ENV["CC_FOR_BUILD"] = native_cc.to_s
      ENV["CXX_FOR_BUILD"] = native_cxx.to_s
      ENV["ERL_TOP"] = buildpath.to_s
      prefix_maps = [
        "-ffile-prefix-map=#{buildpath}=/usr/src/erlang",
        "-fdebug-prefix-map=#{buildpath}=/usr/src/erlang",
        "-fmacro-prefix-map=#{buildpath}=/usr/src/erlang",
        "-ffile-prefix-map=#{root}=/usr/src/kandelo",
        "-fdebug-prefix-map=#{root}=/usr/src/kandelo",
        "-fmacro-prefix-map=#{root}=/usr/src/kandelo",
      ]
      ENV["CFLAGS"] = [
        "-O1", "-D_GNU_SOURCE", "-gline-tables-only", "-fdebug-compilation-dir=.", *prefix_maps
      ].join(" ")
      ENV["CXXFLAGS"] = ENV["CFLAGS"]
      ENV["LDFLAGS"] = "--kandelo-thread-slots=15"

      system "./configure",
        "--host=wasm32-unknown-wasi",
        "--build=#{build_triplet}",
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--enable-deterministic-build",
        "--disable-jit",
        "--disable-hipe",
        "--without-termcap",
        "--without-wx",
        "--without-odbc",
        "--without-ssl",
        "--without-crypto",
        "--without-ssh",
        "--without-megaco",
        "--without-diameter",
        "--without-snmp",
        "--without-ftp",
        "--without-tftp",
        "--without-observer",
        "--without-debugger",
        "--without-dialyzer",
        "--without-jinterface",
        "--without-et",
        "--without-eldap",
        "--without-common_test",
        "--without-eunit",
        "--without-tools",
        "--without-runtime_tools",
        "--without-reltool",
        "--without-xmerl",
        "--without-mnesia",
        "--without-os_mon",
        "--without-public_key",
        "--without-asn1",
        "--disable-kernel-poll",
        "--disable-sctp",
        "--disable-sharing-preserving",
        "erl_xcomp_sysroot=#{ENV.fetch("WASM_POSIX_SYSROOT")}",
        "erl_xcomp_bigendian=no",
        "erl_xcomp_double_middle_endian=no",
        "erl_xcomp_poll=yes",
        "erl_xcomp_clock_gettime_cpu_time=yes",
        "erl_xcomp_putenv_copy=no",
        "erl_xcomp_after_morecore_hook=no",
        "erl_xcomp_dlsym_brk_wrappers=no"

      system "gmake", "-j#{ENV.make_jobs}"
      system "gmake", "release", "RELEASE_ROOT=#{release_root}"

      linked_modules = wasm_files(release_root)
      odie "OTP release did not contain any linked Wasm modules" if linked_modules.empty?
      fork_policies = linked_modules.to_h do |wasm|
        relative = wasm.relative_path_from(release_root).to_s
        policy = standalone_wasm_fork_policy(wasm)
        kandelo_fork_instrument(wasm) if policy == :required
        [relative, policy]
      end

      cd release_root do
        system "./Install", "-cross", "-minimal", GUEST_RUNTIME_ROOT
      end
      validate_release!(release_root, root, fork_policies)
    end

    (lib/"erlang").install release_root.children
    bin.install_symlink (lib/"erlang/bin").children.select(&:executable?)
  end

  test do
    runtime_root = lib/"erlang"
    erts_dir = runtime_root.children.find { |path| path.directory? && path.basename.to_s.start_with?("erts-") }
    odie "installed Erlang runtime has no ERTS directory" if erts_dir.nil?

    guest_erts_bin = "#{GUEST_RUNTIME_ROOT}/#{erts_dir.basename}/bin"
    guest_files = {}
    exec_programs = {}
    Find.find(runtime_root.to_s) do |candidate|
      next unless File.file?(candidate)

      path = Pathname(candidate)
      relative = path.relative_path_from(runtime_root)
      guest_path = "#{GUEST_RUNTIME_ROOT}/#{relative}"
      if File.binread(path, 4) == "\0asm".b
        exec_programs[guest_path] = path
      else
        guest_files[guest_path] = path
      end
    end

    port_child_source = testpath/"erlang-port-child.c"
    port_child = testpath/"erlang-port-child.wasm"
    port_child_source.write <<~C
      #include <stdio.h>

      int main(void) {
        puts("port-child-ok");
        return 0;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, port_child_source, "-o", port_child
    end
    guest_port_child = "#{GUEST_RUNTIME_ROOT}/kandelo-test/erlang-port-child"
    exec_programs[guest_port_child] = port_child

    expression = <<~ERLANG.lines.map(&:strip).join(" ")
      {module, user_sup} = code:ensure_loaded(user_sup),
      Parent = self(),
      spawn(fun() -> Parent ! {worker, 6 * 7} end),
      receive {worker, 42} -> ok after 10000 -> erlang:error(worker_timeout) end,
      Port = open_port(
        {spawn_executable, "#{guest_port_child}"},
        [binary, exit_status, use_stdio, stderr_to_stdout]
      ),
      receive
        {Port, {data, <<"port-child-ok\\n">>}} -> ok
      after 10000 -> erlang:error(port_data_timeout)
      end,
      receive
        {Port, {exit_status, 0}} -> ok
      after 10000 -> erlang:error(port_exit_timeout)
      end,
      io:format("erlang-ok~n"),
      halt().
    ERLANG
    args = [
      "-S", "1:1",
      "-A", "0",
      "-SDio", "1",
      "-SDcpu", "1:1",
      "-P", "262144",
      "--",
      "-root", GUEST_RUNTIME_ROOT,
      "-bindir", guest_erts_bin,
      "-progname", "erl",
      "-home", "/tmp",
      "-start_epmd", "false",
      "-boot", "#{GUEST_RUNTIME_ROOT}/releases/28/start_clean",
      "-noshell",
      "-eval", expression
    ]
    env = {
      "BINDIR"     => guest_erts_bin,
      "EMU"        => "beam",
      "HOME"       => "/tmp",
      "KERNEL_CWD" => "/tmp",
      "PROGNAME"   => "erl",
      "ROOTDIR"    => GUEST_RUNTIME_ROOT,
      "TIMEOUT"    => "120000",
    }
    output = kandelo_run_wasm(
      erts_dir/"bin/beam.smp", args,
      env: env, exec_programs: exec_programs, guest_files: guest_files, max_workers: 16
    )
    assert_includes output, "erlang-ok\n"
  end

  private

  def wasm_files(root)
    files = []
    Find.find(root.to_s) do |candidate|
      next unless File.file?(candidate)
      next if File.symlink?(candidate)

      path = Pathname(candidate)
      files << path if File.binread(path, 4) == "\0asm".b
    end
    files
  end

  def standalone_wasm_fork_policy(wasm)
    dump = Utils.safe_popen_read("wasm-objdump", "-x", wasm).to_s
    odie "OTP release contains an unexpected Wasm side module: #{wasm}" if dump.match?(/name:\s+"dylink\.0"/)

    imports_fork = dump.each_line.any? { |line| line.match?(/<-\s+kernel\.kernel_fork(?:\s|$)/) }
    imports_fork ? :required : :forbidden
  end

  def validate_release!(release_root, root, fork_policies)
    wasm = wasm_files(release_root)
    final_paths = wasm.map { |path| path.relative_path_from(release_root).to_s }.sort
    odie "OTP install changed the classified Wasm artifact set" if final_paths != fork_policies.keys.sort

    wasm.each do |artifact|
      relative = artifact.relative_path_from(release_root).to_s
      kandelo_validate_wasm_artifact(artifact, fork: fork_policies.fetch(relative))
    end
    validate_non_wasm_release_data_paths!(release_root, wasm, root)

    validator = buildpath/"validate-erlang-artifacts.mjs"
    validator.write <<~JS
      import { readFileSync } from "node:fs";

      const paths = process.argv.slice(2);
      const allowedEnvImports = new Set([
        "__channel_base",
        "memory",
        "__wasm_dlclose",
        "__wasm_dlerror",
        "__wasm_dlopen",
        "__wasm_dlsym",
      ]);
      let beamUsesFork = false;

      for (const path of paths) {
        const bytes = readFileSync(path);
        const module = await WebAssembly.compile(bytes);
        const imports = WebAssembly.Module.imports(module);
        if (WebAssembly.Module.customSections(module, "dylink.0").length !== 0) {
          throw new Error(`${path} is a side module instead of a standalone executable`);
        }
        const unexpected = imports.filter(({ module, name }) =>
          module === "env" && !allowedEnvImports.has(name)
        );
        if (unexpected.length !== 0) {
          throw new Error(`${path} has unresolved non-ABI env imports: ${unexpected.map(({ name }) => name).join(", ")}`);
        }

        const importsFork = imports.some(({ module, name }) =>
          module === "kernel" && name === "kernel_fork"
        );
        if (path.endsWith("/bin/beam.smp")) beamUsesFork ||= importsFork;
      }

      if (!beamUsesFork) throw new Error("beam.smp does not retain its fork/exec path");
    JS
    cd(root) do
      system "node", "--experimental-wasm-exnref", "--import", "tsx/esm",
        validator, *wasm
    end
  end

  def validate_non_wasm_release_data_paths!(release_root, wasm, root)
    wasm_paths = wasm.to_h { |path| [File.expand_path(path), true] }
    forbidden = [buildpath.to_s, prefix.to_s, root.to_s, "/nix/store/"]
    Find.find(release_root.to_s) do |candidate|
      next unless File.file?(candidate)
      next if File.symlink?(candidate)
      next if wasm_paths.key?(File.expand_path(candidate))

      contents = File.binread(candidate)
      forbidden.each do |marker|
        odie "OTP release data embeds staging path #{marker} in #{candidate}" if contents.include?(marker)
      end
    end
  end
end

__END__
diff --git a/lib/erl_interface/src/connect/ei_resolve.c b/lib/erl_interface/src/connect/ei_resolve.c
--- a/lib/erl_interface/src/connect/ei_resolve.c
+++ b/lib/erl_interface/src/connect/ei_resolve.c
@@ -403 +403 @@
-#elif (defined(__GLIBC__) || defined(__linux__) || (defined(__FreeBSD_version) && (__FreeBSD_version >= 602000)) || defined(__DragonFly__))
+#elif (defined(__GLIBC__) || defined(__linux__) || defined(__wasm__) || (defined(__FreeBSD_version) && (__FreeBSD_version >= 602000)) || defined(__DragonFly__))
@@ -426 +426 @@
-#elif (defined(__GLIBC__) || defined(__linux__) || (defined(__FreeBSD_version) && (__FreeBSD_version >= 602000)) || defined(__DragonFly__) || defined(__ANDROID__))
+#elif (defined(__GLIBC__) || defined(__linux__) || defined(__wasm__) || (defined(__FreeBSD_version) && (__FreeBSD_version >= 602000)) || defined(__DragonFly__) || defined(__ANDROID__))
diff --git a/erts/emulator/beam/io.c b/erts/emulator/beam/io.c
--- a/erts/emulator/beam/io.c
+++ b/erts/emulator/beam/io.c
@@ -531,0 +532,19 @@
+static ERTS_INLINE ErlDrvData
+call_driver_start(erts_driver_t *driver, ErlDrvPort port, char *name,
+                  SysDriverOpts *opts)
+{
+#ifdef __wasm__
+    /* Regular ErlDrvEntry callbacks take two arguments. System drivers use
+     * the internal three-argument extension, which Wasm must call exactly. */
+    if (driver != &fd_driver && driver != &spawn_driver
+#ifndef __WIN32__
+        && driver != &forker_driver
+#endif
+    ) {
+        typedef ErlDrvData (*RegularDriverStart)(ErlDrvPort, char *);
+        return ((RegularDriverStart) driver->start)(port, name);
+    }
+#endif
+    return (*driver->start)(port, name, opts);
+}
+
@@ -713 +732 @@
-	drv_data = (*driver->start)(ERTS_Port2ErlDrvPort(port), name, opts);
+	drv_data = call_driver_start(driver, ERTS_Port2ErlDrvPort(port), name, opts);
diff --git a/erts/emulator/beam/erl_db_util.c b/erts/emulator/beam/erl_db_util.c
--- a/erts/emulator/beam/erl_db_util.c
+++ b/erts/emulator/beam/erl_db_util.c
@@ -3917,0 +3918,31 @@
+#ifdef __wasm__
+/* LLVM 21 miscompiles the inlined mutable stack traversal for Wasm. Keep the
+ * same ESTACK operations behind call boundaries so their state remains sound. */
+static ERTS_NOINLINE bool db_wasm_estack_is_empty(const ErtsEStack *stack)
+{
+    return stack->sp == stack->start;
+}
+
+static ERTS_NOINLINE void db_wasm_estack_push(ErtsEStack *stack, Eterm term)
+{
+    if (stack->end - stack->sp < 1) {
+        erl_grow_estack(stack, 1);
+    }
+    *stack->sp++ = term;
+}
+
+static ERTS_NOINLINE Eterm db_wasm_estack_pop(ErtsEStack *stack)
+{
+    ASSERT(stack->sp != stack->start);
+    return *(--stack->sp);
+}
+
+#  define DB_ESTACK_ISEMPTY(s) db_wasm_estack_is_empty(&(s))
+#  define DB_ESTACK_PUSH(s, term) db_wasm_estack_push(&(s), (term))
+#  define DB_ESTACK_POP(s) db_wasm_estack_pop(&(s))
+#else
+#  define DB_ESTACK_ISEMPTY(s) ESTACK_ISEMPTY(s)
+#  define DB_ESTACK_PUSH(s, term) ESTACK_PUSH(s, term)
+#  define DB_ESTACK_POP(s) ESTACK_POP(s)
+#endif
+
@@ -3921,3 +3952,3 @@
-    ESTACK_PUSH(s,node);
-    while (!ESTACK_ISEMPTY(s)) {
-	node = ESTACK_POP(s);
+    DB_ESTACK_PUSH(s,node);
+    while (!DB_ESTACK_ISEMPTY(s)) {
+	node = DB_ESTACK_POP(s);
@@ -3927 +3958 @@
-		ESTACK_PUSH(s,CAR(list_val(node)));
+		DB_ESTACK_PUSH(s,CAR(list_val(node)));
@@ -3930 +3961 @@
-	    ESTACK_PUSH(s,node);    /* Non wellformed list or [] */
+	    DB_ESTACK_PUSH(s,node);    /* Non wellformed list or [] */
@@ -3937 +3968 @@
-		    ESTACK_PUSH(s,*(++tuple));
+		    DB_ESTACK_PUSH(s,*(++tuple));
@@ -3958,0 +3990,4 @@
+#undef DB_ESTACK_ISEMPTY
+#undef DB_ESTACK_PUSH
+#undef DB_ESTACK_POP
+
