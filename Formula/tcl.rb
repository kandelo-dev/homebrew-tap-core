require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Tcl < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/tcl".freeze
  GUEST_RUNTIME = "#{GUEST_OPT_PREFIX}/lib/tcl9.0".freeze
  GUEST_MODULES = "#{GUEST_OPT_PREFIX}/lib/tcl9".freeze
  GUEST_TCLSH = "#{GUEST_OPT_PREFIX}/bin/tclsh9.0".freeze

  desc "Tool Command Language interpreter and development files for Kandelo"
  homepage "https://www.tcl-lang.org/"
  url "https://github.com/tcltk/tcl/archive/refs/tags/core-9-0-1.tar.gz"
  version "9.0.1"
  sha256 "053ce8cdc632a6484f6a1416524fb66b614cd13e77366ad2722bc205d53eff95"
  license "TCL"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/tclsh9.0"
  skip_clean "lib/libtcl9.0.a"
  skip_clean "lib/libtclstub.a"

  def install
    kandelo_require_arch!("wasm32")
    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")
    stage = buildpath/"kandelo-stage"
    stable_source = "/usr/src/tcl-#{version}"

    # Tcl's cross configure establishes tcl_cv_sys_version, then bypasses it
    # with direct host uname calls. Keep all platform branches target-derived.
    %w[unix/configure unix/configure.ac].each do |file|
      inreplace file, "`uname -s`", "${tcl_cv_sys_version%%-*}"
    end

    # Tcl prefers vfork whenever configure finds it, but Kandelo's documented
    # vfork boundary is a fork alias. Threaded Tcl's atfork child hook then
    # restarts the notifier thread before exec. Prefer Kandelo's non-forking
    # posix_spawn path while retaining Tcl's fallback for real spawn failures.
    inreplace "unix/tclUnixPipe.c" do |s|
      enabled_spawn = s.sub!(
        "    && defined(HAVE_POSIX_SPAWNATTR_SETFLAGS) \\\n" \
        "\t    && !defined(HAVE_VFORK)",
        "    && defined(HAVE_POSIX_SPAWNATTR_SETFLAGS)",
      )
      odie "Tcl posix_spawn feature guard changed" unless enabled_spawn
      forced_spawn = s.sub!("    static int use_spawn = -1;", "    static int use_spawn = 1;")
      odie "Tcl posix_spawn runtime guard changed" unless forced_spawn
    end

    # POSIX and Tcl give their thread entry points different return types.
    # Native ABIs tolerate Tcl's cast, but WebAssembly tables enforce exact
    # signatures. Call Tcl's callback through a correctly typed adapter.
    inreplace "unix/tclUnixThrd.c" do |s|
      inserted_adapter = s.sub! "static PMutex *allocLockPtr = &allocLock;\n", <<~C
        static PMutex *allocLockPtr = &allocLock;

        typedef struct ThreadClientData {
            Tcl_ThreadCreateProc *proc;
            void *clientData;
        } ThreadClientData;

        static void *
        ThreadCreateProc(
            void *clientData)
        {
            ThreadClientData *dataPtr = (ThreadClientData *)clientData;
            Tcl_ThreadCreateProc *proc = dataPtr->proc;
            void *tclClientData = dataPtr->clientData;

            Tcl_Free(dataPtr);
            proc(tclClientData);
            return NULL;
        }
      C
      odie "Tcl thread adapter insertion point changed" unless inserted_adapter
      data_declaration = "    ThreadClientData *dataPtr = " \
                         "(ThreadClientData *)Tcl_Alloc(sizeof(ThreadClientData));\n"
      inserted_data = s.sub! "    int result;\n", "    int result;\n#{data_declaration}"
      odie "Tcl thread client-data insertion point changed" unless inserted_data
      old_create = "    if (pthread_create(&theThread, &attr,\n" \
                   "\t    (void * (*)(void *))(void *)proc, (void *)clientData) &&\n" \
                   "\t    pthread_create(&theThread, NULL,\n" \
                   "\t\t    (void * (*)(void *))(void *)proc, (void *)clientData)) {\n" \
                   "\tresult = TCL_ERROR;\n    " \
                   "} else {\n" \
                   "\t*idPtr = (Tcl_ThreadId)theThread;\n" \
                   "\tresult = TCL_OK;\n    " \
                   "}\n"
      new_create = "    dataPtr->proc = proc;\n    " \
                   "dataPtr->clientData = clientData;\n    " \
                   "if (pthread_create(&theThread, &attr, ThreadCreateProc, dataPtr) &&\n" \
                   "\t    pthread_create(&theThread, NULL, ThreadCreateProc, dataPtr)) {\n" \
                   "\tTcl_Free(dataPtr);\n" \
                   "\tresult = TCL_ERROR;\n    " \
                   "} else {\n" \
                   "\t*idPtr = (Tcl_ThreadId)theThread;\n" \
                   "\tresult = TCL_OK;\n    " \
                   "}\n"
      replaced_create = s.sub! old_create, new_create
      odie "Tcl pthread_create adapter call site changed" unless replaced_create
    end

    kandelo_wasm_build do |root|
      ENV.prepend_path "PATH", formula_opt_bin("wabt")
      ENV.prepend_path "PATH", formula_opt_bin("binaryen")
      prefix_maps = {
        buildpath => stable_source,
        root      => "/usr/src/kandelo",
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
      ENV["CFLAGS"] = [
        "-O2",
        "-gline-tables-only",
        "-fdebug-compilation-dir=#{stable_source}",
        *prefix_maps,
      ].join(" ")
      ENV["CPPFLAGS"] = "-I#{zlib}/include"
      ENV["LDFLAGS"] = "-L#{zlib}/lib"
      ENV["tcl_cv_sys_version"] = "Linux"

      # Tcl's epoll notifier also requires non-POSIX sys/queue.h. The portable
      # threaded select notifier preserves pthread support on Kandelo's POSIX
      # sysroot without pretending that extra header exists.
      ENV["ac_cv_header_sys_epoll_h"] = "no"

      # Kandelo permits unresolved Wasm imports at link time, so this
      # AC_SEARCH_LIBS probe cannot discover -lz by linking alone.
      ENV["ac_cv_search_deflateSetHeader"] = "-lz"

      cd "unix" do
        system kandelo_configure,
          "--prefix=#{GUEST_OPT_PREFIX}",
          "--includedir=#{GUEST_OPT_PREFIX}/include/tcl",
          "--disable-shared",
          "--disable-rpath",
          "--disable-corefoundation",
          "--disable-zipfs",
          "--enable-load",
          "--with-tzdata=yes",
          "--without-system-libtommath"
        system "make", "-j#{ENV.make_jobs}", "binaries"

        # Fork instrumentation must remain the final Wasm transform because it
        # records mutable-global offsets used by continuation replay.
        tclsh = buildpath/"unix/tclsh"
        instrumented = buildpath/"unix/tclsh.instrumented"
        system "#{root}/scripts/run-wasm-fork-instrument.sh", tclsh, "-o", instrumented
        mv instrumented, tclsh
        chmod 0755, tclsh

        kandelo_validate_wasm_artifact(tclsh, fork: :required, forbidden_paths: [zlib.to_s])

        # Appending Tcl's ZIPFS payload produces trailing bytes that standard
        # WebAssembly engines reject. Install the normal runtime library instead.
        system "make", "DESTDIR=#{stage}",
          "install-binaries",
          "install-libraries",
          "install-tzdata",
          "install-msgs",
          "install-headers",
          "install-doc"

        installed_lib = "#{GUEST_OPT_PREFIX}/lib"
        staged_prefix = stage/GUEST_OPT_PREFIX.delete_prefix("/")
        config = staged_prefix/"lib/tclConfig.sh"
        inreplace config do |s|
          portable_cflags = "-O2 -gline-tables-only -fdebug-compilation-dir=#{stable_source} " \
                            "-I/home/linuxbrew/.linuxbrew/opt/zlib/include"
          replacements = {
            /^TCL_CC=.*$/                  => "TCL_CC='wasm32posix-cc'",
            /^TCL_EXTRA_CFLAGS=.*$/        => "TCL_EXTRA_CFLAGS='#{portable_cflags}'",
            /^TCL_SHLIB_LD=.*$/            => "TCL_SHLIB_LD='wasm32posix-cc -shared'",
            /^TCL_STLIB_LD=.*$/            => "TCL_STLIB_LD='wasm32posix-ar cr'",
            /^TCL_LD_FLAGS=.*$/            => "TCL_LD_FLAGS='-L/home/linuxbrew/.linuxbrew/opt/zlib/lib'",
            /^TCL_RANLIB=.*$/              => "TCL_RANLIB='wasm32posix-ranlib'",
            /^TCL_BUILD_LIB_SPEC=.*$/      => "TCL_BUILD_LIB_SPEC='-L#{installed_lib} -ltcl9.0'",
            /^TCL_SRC_DIR=.*$/             => "TCL_SRC_DIR=''",
            /^TCL_BUILD_STUB_LIB_SPEC=.*$/ => "TCL_BUILD_STUB_LIB_SPEC='-L#{installed_lib} -ltclstub'",
            /^TCL_BUILD_STUB_LIB_PATH=.*$/ => "TCL_BUILD_STUB_LIB_PATH='#{installed_lib}/libtclstub.a'",
          }
          replacements.each do |pattern, replacement|
            odie "Tcl configuration entry changed: #{pattern.inspect}" unless s.sub!(pattern, replacement)
          end
        end

        forbidden_paths = [
          buildpath, buildpath.realpath, root, Pathname(root).realpath,
          zlib, zlib.realpath, "/private/tmp/", "/private/var/", "/nix/store/"
        ].map(&:to_s).uniq
        [config, staged_prefix/"lib/pkgconfig/tcl.pc", staged_prefix/"lib/libtcl9.0.a"].each do |artifact|
          contents = artifact.binread
          forbidden_paths.each do |forbidden|
            odie "#{artifact} embeds builder path #{forbidden}" if contents.include?(forbidden)
          end
          odie "#{artifact} embeds a builder home path" if contents.match?(%r{/Users/[^/]+/})
        end
      end
    end

    staged_prefix = stage/GUEST_OPT_PREFIX.delete_prefix("/")
    prefix.install staged_prefix.children
    bin.install_symlink "tclsh9.0" => "tclsh"
    (share/"licenses/tcl").install "license.terms"
  end

  test do
    assert_path_exists bin/"tclsh9.0"
    assert_path_exists bin/"tclsh"
    assert_path_exists lib/"libtcl9.0.a"
    assert_path_exists lib/"libtclstub.a"
    assert_path_exists lib/"pkgconfig/tcl.pc"
    assert_path_exists lib/"tclConfig.sh"
    assert_path_exists lib/"tcl9.0/init.tcl"
    assert_path_exists lib/"tcl9/9.0/http-2.10.0.tm"
    assert_path_exists include/"tcl/tcl.h"
    assert_path_exists man1/"tclsh.1"
    assert_path_exists man3/"Tcl_CreateInterp.3"
    assert_path_exists man/"mann/after.n"
    assert_path_exists share/"licenses/tcl/license.terms"
    assert_operator man3.glob("*.3").length, :>, 100
    assert_operator (man/"mann").glob("*.n").length, :>, 100

    config = (lib/"tclConfig.sh").read
    assert_includes config, "TCL_PREFIX='#{GUEST_OPT_PREFIX}'"
    assert_includes config, "TCL_CC='wasm32posix-cc'"
    assert_includes config, "TCL_RANLIB='wasm32posix-ranlib'"
    assert_includes config, "TCL_BUILD_LIB_SPEC='-L#{GUEST_OPT_PREFIX}/lib -ltcl9.0'"
    assert_includes config, "TCL_BUILD_STUB_LIB_PATH='#{GUEST_OPT_PREFIX}/lib/libtclstub.a'"
    pc = (lib/"pkgconfig/tcl.pc").read
    assert_includes pc, "prefix=#{GUEST_OPT_PREFIX}"
    assert_includes pc, "Requires.private:  zlib >= 1.2.3"
    [config, pc].each do |contents|
      [
        prefix.to_s,
        "/private/tmp/",
        "/private/var/",
        "/nix/store/",
        "/opt/homebrew/Cellar/",
        "/usr/local/Cellar/",
      ].each { |path| refute_includes contents, path }
      refute_match %r{/Users/[^/]+/}, contents
    end

    runtime_files = {}
    {
      GUEST_RUNTIME => lib/"tcl9.0",
      GUEST_MODULES => lib/"tcl9",
    }.each do |guest_root, host_root|
      host_root.glob("**/*").select(&:file?).each do |file|
        relative = file.relative_path_from(host_root)
        runtime_files["#{guest_root}/#{relative}"] = file
      end
    end
    assert_operator runtime_files.length, :>, 700

    extension_source = testpath/"kandelo-extension.c"
    extension = testpath/"kandelo-extension.so"
    extension_source.write <<~C
      #define USE_TCL_STUBS
      #include <tcl.h>

      static int KandeloCommand(
          void *client_data, Tcl_Interp *interp, int objc, Tcl_Obj *const objv[]) {
        (void)client_data;
        (void)objc;
        (void)objv;
        Tcl_SetObjResult(interp, Tcl_NewStringObj("extension-ok", -1));
        return TCL_OK;
      }

      int Kandelo_Init(Tcl_Interp *interp) {
        if (Tcl_InitStubs(interp, "9.0", 0) == NULL) return TCL_ERROR;
        Tcl_CreateObjCommand(interp, "kandelo_extension", KandeloCommand, NULL, NULL);
        return Tcl_PkgProvide(interp, "kandelo_extension", "1.0");
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, "-shared", "-fPIC", "-O2", "-I#{include}/tcl",
        extension_source, "-L#{lib}", "-ltclstub", "-o", extension
    end

    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")
    thread_source = testpath/"tcl-thread.c"
    thread_wasm = testpath/"tcl-thread.wasm"
    thread_source.write <<~C
      #include <stdio.h>
      #include <tcl.h>

      static Tcl_ThreadCreateType RunThread(void *opaque) {
        int *value = opaque;
        *value = 42;
        Tcl_ExitThread(7);
        TCL_THREAD_CREATE_RETURN;
      }

      int main(void) {
        Tcl_ThreadId thread;
        int result = -1;
        int value = 0;

        Tcl_FindExecutable("tcl-thread-smoke");
        if (Tcl_CreateThread(&thread, RunThread, &value, TCL_THREAD_STACK_DEFAULT,
                             TCL_THREAD_JOINABLE) != TCL_OK) return 1;
        if (Tcl_JoinThread(thread, &result) != TCL_OK || result != 7 || value != 42) return 2;
        Tcl_Finalize();
        puts("tcl-thread-ok");
        return 0;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, thread_source, "-I#{include}/tcl", "-L#{lib}", "-ltcl9.0",
        "-L#{zlib}/lib", "-lz", "-ldl", "-pthread", "-lm", "-o", thread_wasm
    end

    child_source = testpath/"tcl-child.c"
    child_wasm = testpath/"tcl-child.wasm"
    child_source.write <<~C
      #include <stdio.h>

      int main(int argc, char **argv) {
        if (argc != 2) return 2;
        printf("child:%s\\n", argv[1]);
        return 0;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, child_source, "-O2", "-o", child_wasm
    end

    runtime_test = <<~TCL
      if {[info patchlevel] ne "9.0.1"} { error "unexpected Tcl version" }
      if {[info library] ne "#{GUEST_RUNTIME}"} { error "unexpected Tcl library: [info library]" }
      if {[lmap n {1 2 3 4} {expr {$n * $n}}] ne {1 4 9 16}} { error "lmap failed" }
      if {[dict get [dict create name kandelo] name] ne "kandelo"} { error "dict failed" }
      oo::class create Counter {
        variable value
        constructor {} { set value 0 }
        method next {} { incr value }
      }
      set counter [Counter new]
      if {[$counter next] != 1 || [$counter next] != 2} { error "TclOO failed" }
      set first [coroutine flow apply {{} { yield ready; return done }}]
      if {$first ne "ready" || [flow] ne "done"} { error "coroutine failed" }

      foreach package {http msgcat platform tcl::zlib} { package require $package }
      set payload "Kandelo Tcl compression"
      if {[zlib decompress [zlib compress $payload]] ne $payload} { error "zlib failed" }
      set encoded [binary encode hex [encoding convertto iso8859-1 "caf\u00e9"]]
      if {$encoded ne "636166e9"} { error "encoding data failed: $encoded" }
      set epoch [clock format 0 -timezone :America/New_York -format "%Y-%m-%d %H:%M:%S %z"]
      if {$epoch ne "1969-12-31 19:00:00 -0500"} { error "timezone data failed: $epoch" }

      set path [file join $env(KERNEL_CWD) tcl-file.txt]
      set channel [open $path w]
      puts $channel file-ok
      close $channel
      file rename $path ${path}.renamed
      set channel [open ${path}.renamed r]
      set contents [read $channel]
      close $channel
      file delete ${path}.renamed
      if {[string trim $contents] ne "file-ok"} { error "file workflow failed" }

      load /work/kandelo-extension.so Kandelo
      if {[package require kandelo_extension] ne "1.0"} { error "extension package failed" }
      if {[kandelo_extension] ne "extension-ok"} { error "extension command failed" }
      if {[string trim [exec /work/tcl-child {two words}]] ne "child:two words"} {
        error "exec failed"
      }
      puts "tcl-$env(KANDELO_RUNTIME)-runtime-ok"
    TCL
    runtime_script = testpath/"runtime-test.tcl"
    runtime_script.write runtime_test
    guest_files = runtime_files.merge({
      "/work/kandelo-extension.so" => extension,
      "/work/runtime-test.tcl"     => runtime_script,
    })
    exec_programs = { "/work/tcl-child" => child_wasm }
    base_env = {
      "HOME"       => "/tmp",
      "KERNEL_CWD" => "/tmp",
    }

    assert_equal "tcl-node-runtime-ok\n", kandelo_run_wasm(
      bin/"tclsh9.0",
      ["/work/runtime-test.tcl"],
      argv0:                     GUEST_TCLSH,
      env:                       base_env.merge("KANDELO_RUNTIME" => "node"),
      exec_programs:             exec_programs,
      expected_fork_descendants: 1,
      guest_files:               guest_files,
    )
    assert_equal "tcl-browser-runtime-ok\n", kandelo_run_browser_wasm(
      bin/"tclsh9.0",
      ["/work/runtime-test.tcl"],
      argv0:              "tclsh9.0",
      guest_program_path: GUEST_TCLSH,
      env:                base_env.merge("KANDELO_RUNTIME" => "browser"),
      exec_programs:      exec_programs,
      guest_files:        guest_files,
      timeout_ms:         180_000,
    )

    assert_equal "tcl-thread-ok\n", kandelo_run_wasm(thread_wasm, [])
    assert_equal "tcl-thread-ok\n",
      kandelo_run_browser_wasm(thread_wasm, [], timeout_ms: 180_000)
  end
end
