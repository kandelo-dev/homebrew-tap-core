require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Tcl < Formula
  include KandeloFormulaSupport

  desc "Tool Command Language interpreter and development files for Kandelo"
  homepage "https://www.tcl-lang.org/"
  url "https://github.com/tcltk/tcl/archive/refs/tags/core-9-0-1.tar.gz"
  version "9.0.1"
  sha256 "053ce8cdc632a6484f6a1416524fb66b614cd13e77366ad2722bc205d53eff95"
  license "TCL"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "bin/tclsh9.0"
  skip_clean "lib/libtcl9.0.a"
  skip_clean "lib/libtclstub.a"

  def install
    kandelo_require_arch!("wasm32")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    guest_prefix = "/home/linuxbrew/.linuxbrew/opt/tcl"
    stage = buildpath/"kandelo-stage"

    # Tcl's cross configure establishes tcl_cv_sys_version, then bypasses it
    # with direct host uname calls. Keep all platform branches target-derived.
    %w[unix/configure unix/configure.ac].each do |file|
      inreplace file, "`uname -s`", "${tcl_cv_sys_version%%-*}"
    end

    # POSIX and Tcl give their thread entry points different return types.
    # Native ABIs tolerate Tcl's cast, but WebAssembly tables enforce exact
    # signatures. Call Tcl's callback through a correctly typed adapter.
    inreplace "unix/tclUnixThrd.c" do |s|
      s.sub! "static PMutex *allocLockPtr = &allocLock;\n", <<~C
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
      data_declaration = "    ThreadClientData *dataPtr = " \
                         "(ThreadClientData *)Tcl_Alloc(sizeof(ThreadClientData));\n"
      s.sub! "    int result;\n", "    int result;\n#{data_declaration}"
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
      s.sub! old_create, new_create
    end

    kandelo_wasm_build do |root|
      ENV.prepend_path "PATH", formula_opt_bin("wabt")
      ENV.prepend_path "PATH", formula_opt_bin("binaryen")
      prefix_maps = [
        "-ffile-prefix-map=#{buildpath}=/usr/src/tcl",
        "-fdebug-prefix-map=#{buildpath}=/usr/src/tcl",
        "-fmacro-prefix-map=#{buildpath}=/usr/src/tcl",
        "-ffile-prefix-map=#{root}=/usr/src/kandelo",
        "-fdebug-prefix-map=#{root}=/usr/src/kandelo",
        "-fmacro-prefix-map=#{root}=/usr/src/kandelo",
        "-ffile-prefix-map=#{zlib}=/home/linuxbrew/.linuxbrew/opt/zlib",
        "-fdebug-prefix-map=#{zlib}=/home/linuxbrew/.linuxbrew/opt/zlib",
        "-fmacro-prefix-map=#{zlib}=/home/linuxbrew/.linuxbrew/opt/zlib",
      ]
      ENV["CFLAGS"] = "-O2 -gline-tables-only -fdebug-compilation-dir=. #{prefix_maps.join(" ")}"
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
          "--prefix=#{guest_prefix}",
          "--includedir=#{guest_prefix}/include/tcl",
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
          "install-headers"

        installed_lib = "#{guest_prefix}/lib"
        inreplace stage/guest_prefix.delete_prefix("/")/"lib/tclConfig.sh" do |s|
          portable_cflags = "-O2 -gline-tables-only -fdebug-compilation-dir=. -pipe -finput-charset=UTF-8 " \
                            "-I/home/linuxbrew/.linuxbrew/opt/zlib/include"
          s.sub!(/^TCL_EXTRA_CFLAGS=.*$/, "TCL_EXTRA_CFLAGS='#{portable_cflags}'")
          s.sub!(/^TCL_BUILD_LIB_SPEC=.*$/, "TCL_BUILD_LIB_SPEC='-L#{installed_lib} -ltcl9.0'")
          s.sub!(/^TCL_SRC_DIR=.*$/, "TCL_SRC_DIR=''")
          s.sub!(/^TCL_BUILD_STUB_LIB_SPEC=.*$/, "TCL_BUILD_STUB_LIB_SPEC='-L#{installed_lib} -ltclstub'")
          s.sub!(/^TCL_BUILD_STUB_LIB_PATH=.*$/, "TCL_BUILD_STUB_LIB_PATH='#{installed_lib}/libtclstub.a'")
        end
      end
    end

    staged_prefix = stage/guest_prefix.delete_prefix("/")
    prefix.install staged_prefix.children
    bin.install_symlink "tclsh9.0" => "tclsh"
  end
  test do
    assert_path_exists bin/"tclsh9.0"
    assert_path_exists lib/"libtcl9.0.a"
    assert_path_exists lib/"libtclstub.a"
    assert_path_exists lib/"pkgconfig/tcl.pc"
    assert_path_exists include/"tcl/tcl.h"

    env = {
      "HOME"        => testpath,
      "KERNEL_CWD"  => testpath,
      "TCL_LIBRARY" => lib/"tcl9.0",
    }
    language_test = <<~TCL
      if {[info patchlevel] ne "9.0.1"} { error "unexpected Tcl version" }
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
      foreach package {http msgcat platform} { package require $package }
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
      puts "tcl-language-ok"
    TCL
    language_script = testpath/"language-test.tcl"
    language_script.write language_test
    assert_equal "tcl-language-ok\n", kandelo_run_wasm(bin/"tclsh9.0", [language_script], env: env)

    extension_source = testpath/"kandelo-extension.c"
    extension = testpath/"kandelo-extension.so"
    extension_source.write <<~C
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
      system kandelo_cc, "-shared", "-fPIC", "-O2", "-I#{include}/tcl", extension_source, "-o", extension
    end
    load_test = <<~TCL
      load #{extension} Kandelo
      if {[package require kandelo_extension] ne "1.0"} { error "extension package failed" }
      if {[kandelo_extension] ne "extension-ok"} { error "extension command failed" }
      puts "tcl-load-ok"
    TCL
    load_script = testpath/"load-test.tcl"
    load_script.write load_test
    assert_equal "tcl-load-ok\n", kandelo_run_wasm(bin/"tclsh9.0", [load_script], env: env)

    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    thread_source = testpath/"tcl-thread.c"
    thread_wasm = testpath/"tcl-thread.wasm"
    thread_source.write <<~C
      #include <stdio.h>
      #include <tcl.h>

      static Tcl_ThreadCreateType RunThread(void *opaque) {
        int *value = opaque;
        *value = 42;
        Tcl_ExitThread(0);
        TCL_THREAD_CREATE_RETURN;
      }

      int main(void) {
        Tcl_ThreadId thread;
        int result = -1;
        int value = 0;

        Tcl_FindExecutable("tcl-thread-smoke");
        if (Tcl_CreateThread(&thread, RunThread, &value, TCL_THREAD_STACK_DEFAULT,
                             TCL_THREAD_JOINABLE) != TCL_OK) return 1;
        if (Tcl_JoinThread(thread, &result) != TCL_OK || result != 0 || value != 42) return 2;
        Tcl_Finalize();
        puts("tcl-thread-ok");
        return 0;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, thread_source, "-I#{include}/tcl", "-L#{lib}", "-ltcl9.0",
        "-L#{zlib}/lib", "-lz", "-ldl", "-lpthread", "-lm", "-o", thread_wasm
    end
    assert_equal "tcl-thread-ok\n", kandelo_run_wasm(thread_wasm, [], env: env)

    exec_test = <<~TCL
      if {[string trim [exec /bin/echo child-process]] ne "child-process"} { error "exec failed" }
      puts "tcl-exec-ok"
    TCL
    exec_script = testpath/"exec-test.tcl"
    exec_script.write exec_test
    assert_equal "tcl-exec-ok\n", kandelo_run_wasm(bin/"tclsh9.0", [exec_script], env: env)
  end
end
