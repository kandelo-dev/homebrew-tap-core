require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Cpython < Formula
  include KandeloFormulaSupport

  desc "Python programming language runtime for Kandelo"
  homepage "https://www.python.org/"
  url "https://www.python.org/ftp/python/3.13.3/Python-3.13.3.tar.xz"
  sha256 "40f868bcbdeb8149a3149580bb9bfd407b3321cd48f0be631af955ac92c0e041"
  license "Python-2.0"

  depends_on "binaryen" => :build
  depends_on "pkgconf" => [:build, :test]
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/zlib"

  # The registry contract declares zlib. Add further standard-library modules
  # only as their archives become validated Kandelo target dependencies.
  skip_clean "bin", "lib"

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/cpython".freeze
  GUEST_ZLIB_PREFIX = "/home/linuxbrew/.linuxbrew/opt/zlib".freeze
  GUEST_SOURCE_PREFIX = "/usr/src/cpython".freeze
  BUILD_TRIPLET = "x86_64-pc-linux-gnu".freeze
  EMBED_PRIVATE_LIBS = [
    ["Modules/_decimal/libmpdec/libmpdec.a", "libmpdec.a"],
    ["Modules/_hacl/libHacl_Hash_SHA2.a", "libHacl_Hash_SHA2.a"],
    ["Modules/expat/libexpat.a", "libexpat.a"],
  ].freeze
  STATIC_STDLIB_MODULES = %w[
    _asyncio _bisect _blake2 _codecs_cn _codecs_hk _codecs_iso2022
    _codecs_jp _codecs_kr _codecs_tw _contextvars _csv _datetime _decimal
    _elementtree _heapq _interpchannels _interpqueues _interpreters _json
    _lsprof _md5 _multibytecodec _multiprocessing _opcode _pickle
    _posixshmem _posixsubprocess _queue _random _sha1 _sha2 _sha3 _socket
    _statistics _struct _zoneinfo array binascii cmath fcntl grp math mmap
    pyexpat resource select syslog termios unicodedata zlib
  ].freeze

  def install
    kandelo_require_arch!("wasm32")

    host_bash = kandelo_host_tool("bash")
    host_make = kandelo_host_tool("make")
    host_build = buildpath/"host-build"
    host_build.mkpath
    cd host_build do
      system host_bash, buildpath/"configure",
        "--prefix=#{host_build}/install",
        "--without-ensurepip",
        "--disable-test-modules"
      system host_make, "-j#{ENV.make_jobs}"
    end

    host_python = if (host_build/"python.exe").executable?
      host_build/"python.exe"
    else
      host_build/"python"
    end
    odie "native build did not produce a host Python" unless host_python.executable?

    target_build = buildpath/"kandelo-build"
    target_build.mkpath
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    zlib_real = zlib.realpath
    pkgconf = formula_opt_prefix("pkgconf")
    pkgconf_real = pkgconf.realpath
    pkg_config = pkgconf/"bin/pkg-config"
    kandelo_wasm_build do |root|
      ENV["CONFIG_SITE"] = "#{root}/sdk/config.site"
      ENV["PKG_CONFIG"] = pkg_config
      ENV.delete("PKG_CONFIG_PATH")
      ENV["PKG_CONFIG_LIBDIR"] = "#{zlib}/lib/pkgconfig"
      ENV["CPPFLAGS"] = "-I#{zlib}/include"
      ENV["CFLAGS"] = [
        "-O2",
        "-gline-tables-only",
        "-fdebug-compilation-dir=#{GUEST_SOURCE_PREFIX}",
        "-ffile-prefix-map=#{buildpath}=#{GUEST_SOURCE_PREFIX}",
        "-fdebug-prefix-map=#{buildpath}=#{GUEST_SOURCE_PREFIX}",
        "-fmacro-prefix-map=#{buildpath}=#{GUEST_SOURCE_PREFIX}",
        "-ffile-prefix-map=#{root}=/usr/src/kandelo",
        "-fdebug-prefix-map=#{root}=/usr/src/kandelo",
        "-fmacro-prefix-map=#{root}=/usr/src/kandelo",
        "-ffile-prefix-map=#{zlib}=#{GUEST_ZLIB_PREFIX}",
        "-fdebug-prefix-map=#{zlib}=#{GUEST_ZLIB_PREFIX}",
        "-fmacro-prefix-map=#{zlib}=#{GUEST_ZLIB_PREFIX}",
        "-ffile-prefix-map=#{zlib_real}=#{GUEST_ZLIB_PREFIX}",
        "-fdebug-prefix-map=#{zlib_real}=#{GUEST_ZLIB_PREFIX}",
        "-fmacro-prefix-map=#{zlib_real}=#{GUEST_ZLIB_PREFIX}",
      ].join(" ")
      ENV["LDFLAGS"] = "-L#{zlib}/lib -Wl,--export-all"
      ENV["LIBS"] = "-ldl"

      # CPython rejects the generic wasm32-unknown-none Autoconf identity and
      # its WASI profile intentionally removes fork, subprocess, and dlopen.
      # This target identity selects CPython's Linux/POSIX feature model while
      # the Kandelo SDK compiler still emits wasm32-unknown-unknown modules.
      {
        "ac_cv_buggy_getaddrinfo"       => "no",
        "ac_cv_file__dev_ptmx"          => "yes",
        "ac_cv_file__dev_ptc"           => "no",
        "ac_cv_func_dlopen"             => "yes",
        "ac_cv_lib_dl_dlopen"           => "yes",
        "ac_cv_func_fork1"              => "no",
        # CPython's probe uses this exact mixed-case cache variable.
        "ac_cv_func_rtpSpawn"           => "no",
        # Setup.stdlib is processed after Setup.local. Suppress its entries so
        # _posixsubprocess can be linked into the instrumented main module and
        # undeclared ncurses dependencies remain disabled.
        "py_cv_module__posixsubprocess" => "n/a",
        "py_cv_module__curses"          => "n/a",
        "py_cv_module__curses_panel"    => "n/a",
        "py_cv_module__dbm"             => "n/a",
      }.each { |key, value| ENV[key] = value }

      setup_local = target_build/"Modules/Setup.local"
      setup_local.dirname.mkpath
      setup_local.write <<~EOS
        # The fork call and every continuation frame above it must live in
        # the main module so Kandelo's fork instrumenter can rewrite them.
        *static*
        _posixsubprocess _posixsubprocess.c
      EOS

      cd target_build do
        system buildpath/"configure",
          "--host=wasm32-unknown-linux-musl",
          # The native bootstrap interpreter was already built above. Keep the
          # cross-build identity stable so target metadata does not depend on
          # the builder kernel and config.guess output.
          "--build=#{BUILD_TRIPLET}",
          "--with-build-python=#{host_python}",
          "--prefix=#{GUEST_OPT_PREFIX}",
          "--without-ensurepip",
          "--disable-test-modules",
          "--with-pkg-config=yes"

        # CPython quotes its source VPATH into getpath.o for build-tree
        # detection. Preserve that provenance without embedding Homebrew's
        # ephemeral source directory in the executable.
        inreplace target_build/"Makefile",
          "-DVPATH='\"$(VPATH)\"'",
          "-DVPATH='\"#{GUEST_SOURCE_PREFIX}\"'"

        system "make", "-j#{ENV.make_jobs}", "all"
        shared_extensions = Dir[target_build/"build/lib.*/**/*.so"]
        if shared_extensions.any?
          names = shared_extensions.join(", ")
          odie "CPython built uninstrumented shared standard-library modules: #{names}"
        end

        optimized = target_build/"python.optimized.wasm"
        instrumented = target_build/"python.instrumented.wasm"
        system "wasm-opt", "-O2", target_build/"python.exe", "-o", optimized
        system "#{root}/scripts/run-wasm-fork-instrument.sh", optimized, "-o", instrumented

        static_library = target_build/"libpython3.13.a"
        odie "CPython did not build libpython3.13.a" unless static_library.file?
        kandelo_validate_wasm_artifact(
          instrumented,
          fork:            :required,
          forbidden_paths: [zlib.to_s, zlib_real.to_s],
        )

        [instrumented, static_library].each do |artifact|
          contents = artifact.binread
          [buildpath.to_s, prefix.to_s, root.to_s, zlib.to_s, zlib_real.to_s].each do |staging_path|
            odie "#{artifact} embeds CPython staging path #{staging_path}" if contents.include?(staging_path)
          end
          odie "CPython artifact embeds a host workspace path" if contents.match?(%r{/(?:Users/|home/runner/work/)})
        end

        install_root = buildpath/"install-root"
        system "make", "install", "DESTDIR=#{install_root}"
        staged_prefix = install_root/GUEST_OPT_PREFIX.delete_prefix("/")
        EMBED_PRIVATE_LIBS.each do |source, installed_name|
          (staged_prefix/"lib").install target_build/source => installed_name
        end
        rm staged_prefix/"bin/python3.13"
        (staged_prefix/"bin").install instrumented => "python3.13"
        prefix.install(*staged_prefix.children)
      end
    end

    bin.install_symlink "python3.13" => "python" unless (bin/"python").exist?
    bin.install_symlink "python3.13" => "cpython" unless (bin/"cpython").exist?
    chmod 0755, bin/"python3.13"
    Dir[lib/"**/*.a"].each { |archive| chmod 0644, archive }
    sanitize_build_metadata!(zlib, zlib_real, pkgconf, pkgconf_real)
    repair_static_embed_metadata!
    regenerate_python_bytecode!(host_python)
    reject_staging_paths!(zlib, zlib_real, pkgconf, pkgconf_real)
  end

  test do
    env = {
      "HOME"                    => testpath,
      "KERNEL_CWD"              => testpath,
      "PYTHONDONTWRITEBYTECODE" => "1",
      "PYTHONHOME"              => prefix,
      "PYTHONPATH"              => "",
    }
    pathlib_pyc = Dir[lib/"python3.13/pathlib/__pycache__/_local.cpython-313.pyc"].first
    refute_nil pathlib_pyc
    assert_includes File.binread(pathlib_pyc),
      "#{GUEST_OPT_PREFIX}/lib/python3.13/pathlib/_local.py"

    config_script = bin/"python3.13-config"
    config_cflags = kandelo_run_wasm(
      bin/"python3.13", [config_script, "--cflags"], env: env
    ).strip
    config_ldflags = kandelo_run_wasm(
      bin/"python3.13", [config_script, "--embed", "--ldflags"], env: env
    ).strip
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    translate_guest_paths = lambda do |flags|
      Shellwords.split(flags).map do |flag|
        flag.gsub(GUEST_OPT_PREFIX, prefix.to_s).gsub(GUEST_ZLIB_PREFIX, zlib.to_s)
      end
    end
    pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
    pkgconfig_flags = kandelo_wasm_build do
      ENV["PKG_CONFIG_LIBDIR"] = (lib/"pkgconfig").to_s
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")
      shell_output("#{pkgconf} --static --cflags --libs python3-embed").strip
    end
    %W[
      -I#{GUEST_OPT_PREFIX}/include/python3.13
      -L#{GUEST_OPT_PREFIX}/lib
      -lpython3.13
      -lresolv
      -ldl
      -lmpdec
      -lHacl_Hash_SHA2
      -lexpat
      -L#{GUEST_ZLIB_PREFIX}/lib
      -lz
      -lm
    ].each { |flag| assert_includes Shellwords.split(pkgconfig_flags), flag }

    embed_source = testpath/"embed-smoke.c"
    embed_source.write <<~C
      #include <Python.h>

      int main(void) {
          Py_Initialize();
          int status = PyRun_SimpleString("print('embed-ok')");
          if (Py_FinalizeEx() < 0) return 120;
          return status;
      }
    C
    embed_wasm = testpath/"embed-smoke.wasm"
    kandelo_wasm_build do
      system "wasm32posix-cc",
        *translate_guest_paths.call(config_cflags), embed_source,
        *translate_guest_paths.call(config_ldflags), "-o", embed_wasm
      kandelo_fork_instrument(embed_wasm)
    end
    assert_equal "embed-ok\n", kandelo_run_wasm(embed_wasm, [], env: env)

    pkgconfig_embed_wasm = testpath/"embed-pkgconfig-smoke.wasm"
    kandelo_wasm_build do
      system "wasm32posix-cc", embed_source,
        *translate_guest_paths.call(pkgconfig_flags), "-o", pkgconfig_embed_wasm
      kandelo_fork_instrument(pkgconfig_embed_wasm)
    end
    assert_equal "embed-ok\n", kandelo_run_wasm(pkgconfig_embed_wasm, [], env: env)

    runtime_script = <<~PYTHON
      import array
      import importlib
      import json
      import pathlib
      import sys
      import sysconfig
      import threading
      import zlib

      assert sys.version_info[:3] == (3, 13, 3)
      assert json.loads('{"answer": 42}')["answer"] == 42
      assert array.array("I", [17, 25]).tolist() == [17, 25]
      assert "array" in sys.builtin_module_names
      assert "zlib" in sys.builtin_module_names
      assert zlib.decompress(zlib.compress(b"kandelo-python")) == b"kandelo-python"

      failures = {}
      for module in #{JSON.generate(STATIC_STDLIB_MODULES)}:
          try:
              importlib.import_module(module)
              assert module in sys.builtin_module_names
          except Exception as error:
              failures[module] = repr(error)
      assert not failures, failures

      thread_values = []
      thread = threading.Thread(target=lambda: thread_values.append(6 * 7))
      thread.start()
      thread.join()
      assert not thread.is_alive() and thread_values == [42]

      path = pathlib.Path("python-filesystem-smoke.txt")
      path.write_text("filesystem-ok", encoding="utf-8")
      renamed = path.with_name("python-filesystem-renamed.txt")
      path.rename(renamed)
      assert renamed.read_text(encoding="utf-8") == "filesystem-ok"
      assert renamed.stat().st_size == len("filesystem-ok")

      config = sysconfig.get_config_var("CC")
      assert config.startswith("wasm32posix-cc "), config
      assert "/Users/" not in config and "/home/runner/work/" not in config
      print("runtime-stdlib-static-filesystem-ok")
    PYTHON
    assert_equal "runtime-stdlib-static-filesystem-ok\n",
      kandelo_run_wasm(bin/"python3.13", ["-c", runtime_script], env: env)

    fork_script = <<~PYTHON
      import array
      import os
      import zlib

      read_fd, write_fd = os.pipe()
      child = os.fork()
      if child == 0:
          os.close(read_fd)
          values = array.array("I", [17, 25])
          os.write(write_fd, zlib.compress(bytes(values)))
          os.close(write_fd)
          os._exit(0)

      os.close(write_fd)
      payload = os.read(read_fd, 128)
      os.close(read_fd)
      waited, status = os.waitpid(child, 0)
      assert waited == child and status == 0
      values = array.array("I")
      values.frombytes(zlib.decompress(payload))
      assert values.tolist() == [17, 25]
      print("fork-static-extension-ok")
    PYTHON
    assert_equal "fork-static-extension-ok\n",
      kandelo_run_wasm(bin/"python3.13", ["-c", fork_script], env: env)

    subprocess_script = <<~PYTHON
      import subprocess
      import sys

      completed = subprocess.run(
          [sys.executable, "-c", "print(6 * 7)"],
          check=True,
          capture_output=True,
          text=True,
      )
      assert completed.stdout == "42\\n"
      assert completed.stderr == ""
      print("subprocess-ok")
    PYTHON
    assert_equal "subprocess-ok\n",
      kandelo_run_wasm(bin/"python3.13", ["-c", subprocess_script], env: env)

    active_extension_fork_script = <<~PYTHON
      import _json
      import json
      import os
      import sys

      assert "_json" in sys.builtin_module_names
      assert json.encoder.c_make_encoder is not None
      assert json.encoder.c_make_encoder is _json.make_encoder

      class Marker:
          pass

      read_fd, write_fd = os.pipe()
      forked_child = None

      def fork_from_json_callback(_value):
          global forked_child
          child = os.fork()
          if child == 0:
              os.close(read_fd)
              forked_child = 0
              return "child"
          os.close(write_fd)
          forked_child = child
          return "parent"

      result = json.dumps(Marker(), default=fork_from_json_callback)
      if forked_child == 0:
          os.write(write_fd, result.encode("utf-8"))
          os.close(write_fd)
          os._exit(0)

      payload = os.read(read_fd, 64)
      os.close(read_fd)
      waited, status = os.waitpid(forked_child, 0)
      assert waited == forked_child and status == 0
      assert result == '"parent"'
      assert payload == b'"child"'
      print("active-static-extension-fork-ok")
    PYTHON
    assert_equal "active-static-extension-fork-ok\n",
      kandelo_run_wasm(bin/"python3.13", ["-c", active_extension_fork_script], env: env)
  end

  private

  def repair_static_embed_metadata!
    private_flags = [
      "-L#{GUEST_OPT_PREFIX}/lib",
      "-lmpdec",
      "-lHacl_Hash_SHA2",
      "-lexpat",
      "-L#{GUEST_ZLIB_PREFIX}/lib",
      "-lz",
    ].join(" ")
    libs = "-lresolv -ldl #{private_flags}"
    module_libs = "-lm #{private_flags}"

    Dir[lib/"python3.13/_sysconfigdata_*.py"].each do |path|
      inreplace path do |s|
        s.gsub!(/^    'LIBS': '.*',$/, "    'LIBS': '#{libs}',")
        s.gsub!(/^    'LOCALMODLIBS': '.*',$/, "    'LOCALMODLIBS': '#{module_libs}',")
        s.gsub!(/^    'MODLIBS': '.*',$/, "    'MODLIBS': '#{module_libs}',")
      end
    end

    Dir[lib/"python3.13/config-*/Makefile"].each do |path|
      inreplace path do |s|
        s.gsub!(/^LIBS=.*$/, "LIBS=\t\t#{libs}")
        s.gsub!(/^LOCALMODLIBS=.*$/, "LOCALMODLIBS= #{module_libs}")
        s.gsub!(/^MODLIBS=.*$/, "MODLIBS=           $(LOCALMODLIBS) $(BASEMODLIBS)")
      end
    end

    inreplace lib/"pkgconfig/python-3.13-embed.pc" do |s|
      s.gsub!(/^Libs\.private:.*$/, "Libs.private: #{libs} -lm")
    end
  end

  def sanitize_build_metadata!(zlib, zlib_real, pkgconf, pkgconf_real)
    root = kandelo_require_root!
    replacements = {
      "#{root}/sdk/bin/"               => "",
      "#{pkgconf_real}/bin/pkg-config" => "pkg-config",
      "#{pkgconf}/bin/pkg-config"      => "pkg-config",
      buildpath.to_s                   => GUEST_SOURCE_PREFIX,
      prefix.to_s                      => GUEST_OPT_PREFIX,
      zlib_real.to_s                   => GUEST_ZLIB_PREFIX,
      zlib.to_s                        => GUEST_ZLIB_PREFIX,
      root.to_s                        => "/usr/src/kandelo",
    }
    config_metadata = Dir[
      lib/"python3.13/config-*/{Makefile,Setup*,config.c,config.c.in,install-sh,makesetup,python-config.py}",
    ]
    metadata = [
      *Dir[lib/"python3.13/_sysconfigdata_*.py"],
      *config_metadata,
      bin/"python3.13-config",
      *Dir[bin/"{2to3,idle3,pydoc3}*"],
      lib/"pkgconfig/python-3.13.pc",
      lib/"pkgconfig/python-3.13-embed.pc",
    ].map { |path| Pathname(path) }.select(&:file?)

    metadata.each do |path|
      inreplace path do |s|
        replacements.each { |from, to| s.gsub!(from, to, audit_result: false) }
      end
      odie "CPython metadata still embeds a host path" if path.read.match?(%r{/(?:Users/|home/runner/work/)})
    end
  end

  def reject_staging_paths!(zlib, zlib_real, pkgconf, pkgconf_real)
    root = kandelo_require_root!
    staging_paths = [buildpath, prefix, root, zlib, zlib_real, pkgconf, pkgconf_real].map(&:to_s)

    Dir[prefix/"**/*"].each do |path|
      artifact = Pathname(path)
      next unless artifact.file?

      contents = artifact.binread
      staging_paths.each do |staging_path|
        odie "#{artifact} embeds staging path #{staging_path}" if contents.include?(staging_path)
      end
      odie "#{artifact} embeds a Nix store path" if contents.include?("/nix/store/")
    end
  end

  def regenerate_python_bytecode!(host_python)
    stdlib = lib/"python3.13"
    rm Dir[stdlib/"**/*.pyc"]
    system host_python, "-m", "compileall",
      "--invalidation-mode", "checked-hash",
      "-d", "#{GUEST_OPT_PREFIX}/lib/python3.13",
      "-o", "0", "-o", "1", "-o", "2",
      "-q", "-f", stdlib
  end
end
