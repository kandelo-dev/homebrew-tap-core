require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s
require "digest"

class Php < Formula
  include KandeloFormulaSupport

  GUEST_HOMEBREW_PREFIX = "/home/linuxbrew/.linuxbrew".freeze
  GUEST_OPT_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/php".freeze
  GUEST_CONFIG_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/etc/php".freeze
  GUEST_EXTENSION_DIR = "#{GUEST_OPT_PREFIX}/lib/php/extensions".freeze
  GUEST_ICU_DATA = "#{GUEST_OPT_PREFIX}/share/php/icu.dat".freeze
  ICU_DATA_SHA256 = "dc778b9ffe18ed319ad3fb70754f80e51cf7b6dbfff38fc0c0a5f27bb5463dad".freeze
  ICU_DATA_BYTES = 30_782_896

  desc "General-purpose scripting language and FastCGI service for Kandelo"
  homepage "https://www.php.net/"
  url "https://www.php.net/distributions/php-8.3.15.tar.xz"
  sha256 "3df5d45637283f759eef8fc3ce03de829ded3e200c3da278936a684955d2f94f"
  license all_of: [
    "PHP-3.01",
    "Zend-2.0",
    "BSL-1.0",
    "MIT",
    "Apache-1.0",
    "bcrypt-Solar-Designer",
    "BSD-2-Clause-Darwin",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSD-4-Clause-UC",
    "ISC",
    "LGPL-2.1-only",
    "LGPL-2.1-or-later",
    "OLDAP-2.8",
    "TCL",
    "Zlib",
  ]

  depends_on KandeloFormulaSupport::BinaryenRequirement => [:build, :test]
  depends_on KandeloFormulaSupport::PkgconfRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  depends_on "make" => :build
  depends_on "kandelo-dev/tap-core/dash" => :test
  depends_on "kandelo-dev/tap-core/icu"
  depends_on "kandelo-dev/tap-core/libcurl"
  depends_on "kandelo-dev/tap-core/libcxx"
  depends_on "kandelo-dev/tap-core/libiconv"
  depends_on "kandelo-dev/tap-core/libxml2"
  depends_on "kandelo-dev/tap-core/libzip"
  depends_on "kandelo-dev/tap-core/openssl"
  depends_on "kandelo-dev/tap-core/sqlite"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/php"
  skip_clean "sbin/php-fpm"
  skip_clean "lib/php/extensions/curl.so"
  skip_clean "lib/php/extensions/intl.so"
  skip_clean "lib/php/extensions/opcache.so"
  skip_clean "lib/php/extensions/phar.so"
  skip_clean "lib/php/extensions/zend_test.so"
  skip_clean "lib/php/extensions/zip.so"

  # WHY: the publisher executes Formulae from an isolated tap clone and
  # deliberately rejects reads through mutable tap-local paths. Keep these
  # Kandelo platform-boundary sources in the tap, but fetch their reviewed
  # bytes through the immutable asset commit that precedes this Formula.
  resource "intl-icu-data-loader" do
    url "https://raw.githubusercontent.com/Kandelo-dev/homebrew-tap-core/966a6eef994b78ca4940722c17ed6ab73686663a/Kandelo/patches/php/intl-icu-data-loader.c"
    sha256 "cfc93a63190ce99ee73ee1373bc4723aa16fb02cf8729b5fdcc396b90624fa6e"
  end

  patch do
    url "https://raw.githubusercontent.com/Kandelo-dev/homebrew-tap-core/966a6eef994b78ca4940722c17ed6ab73686663a/Kandelo/patches/php/0001-kandelo-cooperative-timeouts.patch"
    sha256 "c513f64f6fd44987d32c8c6c376022365c11ce8fa5376ae29e627519b6256d28"
  end

  patch do
    url "https://raw.githubusercontent.com/Kandelo-dev/homebrew-tap-core/966a6eef994b78ca4940722c17ed6ab73686663a/Kandelo/patches/php/0002-kandelo-extension-boundaries.patch"
    sha256 "e4b12ada8f45d33a33cf4180127731a69d0d40a00fe94383927168b3bc3ecfdd"
  end

  def install
    kandelo_require_arch!("wasm32")

    icu = formula_opt_prefix("kandelo-dev/tap-core/icu")
    libcurl = formula_opt_prefix("kandelo-dev/tap-core/libcurl")
    libcxx = formula_opt_prefix("kandelo-dev/tap-core/libcxx")
    libiconv = formula_opt_prefix("kandelo-dev/tap-core/libiconv")
    libxml2 = formula_opt_prefix("kandelo-dev/tap-core/libxml2")
    libzip = formula_opt_prefix("kandelo-dev/tap-core/libzip")
    openssl = formula_opt_prefix("kandelo-dev/tap-core/openssl")
    sqlite = formula_opt_prefix("kandelo-dev/tap-core/sqlite")
    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")
    dependencies = [icu, libcurl, libcxx, libiconv, libxml2, libzip, openssl, sqlite, zlib]

    # WHY: upstream assumes every cross-compiled *-linux-* target has coherent
    # anonymous shared mappings and admits opcache without a runtime probe.
    # Kandelo uses that Autoconf identity for musl behavior, but separate Wasm
    # process memories cannot share direct writes. Keep opcache available for
    # its supported file-cache-only mode without silently enabling its
    # unsupported shared-memory mode whenever a user loads the side module.
    inreplace "ext/opcache/zend_accelerator_module.c" do |s|
      odie "PHP opcache default-enable directive changed" unless s.sub!(
        'STD_PHP_INI_BOOLEAN("opcache.enable"             , "1"',
        'STD_PHP_INI_BOOLEAN("opcache.enable"             , "0"',
      )
    end

    kandelo_wasm_build do |root|
      # WHY: this is the declared build-only native GNU Make, not the tap's
      # wasm `make` program. Homebrew names that native executable `gmake` on
      # macOS to avoid replacing the system tool, but simply `make` on Linux.
      host_make = formula_opt_bin("make")/(OS.mac? ? "gmake" : "make")
      odie "PHP requires the declared native GNU Make executable: #{host_make}" unless host_make.executable?
      stable_source = "/usr/src/php-#{version}"
      # WHY: the SDK's C++ driver treats its sysroot as one coherent C/C++
      # toolchain. Prepending libc++ headers from a bottle while leaving
      # another C++ header/archive set in that sysroot produces conflicting
      # declarations in standard headers. Give PHP an isolated copy whose C++
      # headers and runtime archives all come from its declared libcxx bottle.
      private_sysroot = build_private_sysroot!(libcxx)
      ENV["WASM_POSIX_SYSROOT"] = private_sysroot
      guest_dependency_paths = {
        icu      => "#{GUEST_HOMEBREW_PREFIX}/opt/icu",
        libcurl  => "#{GUEST_HOMEBREW_PREFIX}/opt/libcurl",
        libcxx   => "#{GUEST_HOMEBREW_PREFIX}/opt/libcxx",
        libiconv => "#{GUEST_HOMEBREW_PREFIX}/opt/libiconv",
        libxml2  => "#{GUEST_HOMEBREW_PREFIX}/opt/libxml2",
        libzip   => "#{GUEST_HOMEBREW_PREFIX}/opt/libzip",
        openssl  => "#{GUEST_HOMEBREW_PREFIX}/opt/openssl",
        sqlite   => "#{GUEST_HOMEBREW_PREFIX}/opt/sqlite",
        zlib     => "#{GUEST_HOMEBREW_PREFIX}/opt/zlib",
      }
      path_maps = {
        buildpath                 => stable_source,
        Pathname(root)            => "/usr/src/kandelo",
        private_sysroot           => "/usr/src/kandelo-sysroot",
        Pathname(prefix)          => GUEST_OPT_PREFIX,
        Pathname(HOMEBREW_PREFIX) => GUEST_HOMEBREW_PREFIX,
      }.merge(guest_dependency_paths)
      prefix_map_flags = path_maps.flat_map do |source, destination|
        [Pathname(source), Pathname(source).realpath].uniq.flat_map do |actual_source|
          [
            "-ffile-prefix-map=#{actual_source}=#{destination}",
            "-fdebug-prefix-map=#{actual_source}=#{destination}",
            "-fmacro-prefix-map=#{actual_source}=#{destination}",
          ]
        end
      end

      ENV["CFLAGS"] = [
        "-O2",
        "-gline-tables-only",
        "-fdebug-compilation-dir=#{stable_source}",
        *prefix_map_flags,
      ].join(" ")
      ENV["CXXFLAGS"] = ENV.fetch("CFLAGS")
      ENV["CPPFLAGS"] = dependencies.map { |dependency| "-I#{dependency}/include" }.join(" ")
      # WHY: PHP's loadable extensions resolve process-global libc, TLS,
      # OpenSSL, and compression state from the main module. Force the measured
      # closure into php.wasm, then export it, instead of statically embedding
      # competing copies of those libraries in each side module.
      side_module_symbols = %w[
        setgid setuid initgroups writev asctime rand srand remove inet_pton
        inet_ntop sched_yield alarm basename OCSP_basic_verify
        OCSP_cert_status_str OCSP_crl_reason_str OCSP_response_status_str
        SSL_alert_desc_string_long aligned_alloc div modf round tanhf swprintf
        wcstod wcstof wcstol wcstold wcstoll wcstoul wcstoull wmemchr wmemcmp
        pthread_cond_broadcast pthread_cond_destroy pthread_cond_signal
        pthread_cond_timedwait pthread_cond_wait pthread_detach
        pthread_getspecific pthread_key_create pthread_self pthread_setspecific
      ]
      ENV["LDFLAGS"] = [
        *dependencies.map { |dependency| "-L#{dependency}/lib" },
        "-ldl",
        "-Wl,--export-all",
        *side_module_symbols.map { |symbol| "-Wl,-u,#{symbol}" },
        "-Wl,-z,stack-size=4194304",
      ].join(" ")
      # Supply every target library probe explicitly. This keeps PHP's
      # pkgconf checks from falling back to native metadata when cross-building.
      ENV["LIBXML_CFLAGS"] = "-I#{libxml2}/include/libxml2"
      ENV["LIBXML_LIBS"] = [
        "-L#{libxml2}/lib", "-lxml2",
        "-L#{zlib}/lib", "-lz",
        "-L#{libiconv}/lib", "-liconv", "-lcharset",
        "-ldl", "-lm"
      ].join(" ")
      ENV["OPENSSL_CFLAGS"] = "-I#{openssl}/include"
      ENV["OPENSSL_LIBS"] = "-L#{openssl}/lib -lssl -lcrypto -ldl -pthread"
      ENV["SQLITE_CFLAGS"] = "-I#{sqlite}/include"
      ENV["SQLITE_LIBS"] = "-L#{sqlite}/lib -lsqlite3"
      ENV["ZLIB_CFLAGS"] = "-I#{zlib}/include"
      ENV["ZLIB_LIBS"] = "-L#{zlib}/lib -lz"
      ENV["ICONV_CFLAGS"] = "-I#{libiconv}/include"
      ENV["ICONV_LIBS"] = "-L#{libiconv}/lib -liconv -lcharset"
      ENV["CURL_CFLAGS"] = "-I#{libcurl}/include -DCURL_STATICLIB"
      ENV["CURL_LIBS"] = "-L#{libcurl}/lib -lcurl"
      ENV["LIBZIP_CFLAGS"] = "-I#{libzip}/include"
      ENV["LIBZIP_LIBS"] = "-L#{libzip}/lib -lzip -L#{zlib}/lib -lz"
      ENV["ICU_CFLAGS"] = "-I#{icu}/include"
      ENV["ICU_LIBS"] = [
        "-L#{icu}/lib", "-licui18n", "-licuio", "-licuuc", "-licudata",
        "-L#{libcxx}/lib", "-lc++", "-lc++abi"
      ].join(" ")
      ENV["ac_cv_lib_iconv_libiconv"] = "yes"
      ENV["ac_cv_lib_curl_curl_easy_perform"] = "yes"
      %w[
        zip_file_set_mtime
        zip_file_set_encryption
        zip_libzip_version
        zip_register_progress_callback_with_state
        zip_register_cancel_callback_with_state
        zip_compression_method_supported
      ].each { |symbol| ENV["ac_cv_lib_zip_#{symbol}"] = "yes" }
      # musl exposes unshare() as an honest ENOSYS compatibility stub.
      ENV["ac_cv_func_unshare"] = "no"
      ENV.delete("PKG_CONFIG_PATH")
      ENV["PHP_UNAME"] = "Kandelo wasm32-posix-kernel"

      system kandelo_configure(root),
        "--disable-all",
        "--disable-cgi",
        "--disable-fiber-asm",
        "--disable-opcache-jit",
        "--disable-phpdbg",
        "--disable-rpath",
        "--disable-zend-signals",
        "--enable-cli",
        "--enable-bcmath",
        "--enable-calendar",
        "--enable-ctype",
        "--enable-dba",
        "--enable-dom",
        "--enable-exif",
        "--enable-fileinfo",
        "--enable-filter",
        "--enable-fpm",
        "--enable-ftp",
        "--enable-intl=shared",
        "--enable-mbstring",
        "--disable-mbregex",
        "--enable-opcache",
        "--enable-pcntl",
        "--enable-pdo",
        "--enable-phar=shared",
        "--enable-posix",
        "--enable-session",
        "--enable-shmop",
        "--enable-simplexml",
        "--enable-soap",
        "--enable-sockets",
        "--enable-sysvmsg",
        "--enable-sysvsem",
        "--enable-sysvshm",
        "--enable-tokenizer",
        "--enable-xml",
        "--enable-xmlreader",
        "--enable-xmlwriter",
        "--enable-zend-max-execution-timers",
        "--enable-zend-test=shared",
        "--with-config-file-path=#{GUEST_CONFIG_PREFIX}",
        "--with-config-file-scan-dir=#{GUEST_CONFIG_PREFIX}/conf.d",
        "--with-curl=shared",
        "--with-fpm-group=nobody",
        "--with-fpm-user=nobody",
        "--with-iconv=#{libiconv}",
        "--with-libxml",
        "--with-mysqli=mysqlnd",
        "--with-openssl",
        "--with-pdo-mysql=mysqlnd",
        "--with-pdo-sqlite",
        "--with-sqlite3",
        "--with-zip=shared",
        "--with-zlib",
        "--without-pcre-jit",
        "--without-valgrind",
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--sysconfdir=#{GUEST_CONFIG_PREFIX}",
        "--localstatedir=/var"

      # WHY: Kandelo's linker deliberately permits unresolved kernel imports.
      # Link-only Autoconf probes can therefore claim functions that libc does
      # not define. Keep only the known-absent functions disabled; unlike the
      # retired registry recipe, do not disable fopencookie, dn_expand,
      # dn_skipname, prctl, or syslog now that Kandelo's libc provides them.
      absent_functions = %w[
        HAVE_DNS_SEARCH
        HAVE_DNS_SEARCH_FUNC
        HAVE_FUNOPEN
        HAVE_RAND_EGD
        HAVE_RES_NDESTROY
        HAVE_RES_NSEARCH
        HAVE_RES_SEARCH
        HAVE_SETPROCTITLE
        HAVE_SETPROCTITLE_FAST
        HAVE_STD_SYSLOG
        HAVE_FORKX
        HAVE_RFORK
      ]
      inreplace "main/php_config.h" do |s|
        absent_functions.each do |macro|
          definition = /^#define #{Regexp.escape(macro)} 1$/
          s.gsub!(definition, "/* #undef #{macro} */") if
            definition.match?(s.inreplace_string)
        end
        {
          "HAVE_FOPENCOOKIE"               => true,
          "HAVE_PRCTL"                     => true,
          "HAVE_SQLITE3_COLUMN_TABLE_NAME" => true,
          "HAVE_SQLITE3_EXPANDED_SQL"      => true,
          "COOKIE_SEEKER_USES_OFF64_T"     => false,
          "SQLITE_OMIT_LOAD_EXTENSION"     => true,
        }.each do |macro, enabled|
          replacement = enabled ? "#define #{macro} 1" : "/* #undef #{macro} */"
          pattern = %r{^(?:#define #{Regexp.escape(macro)} 1|/\* #undef #{Regexp.escape(macro)} \*/)$}
          odie "PHP config no longer declares #{macro}" unless s.gsub!(pattern, replacement)
        end
        odie "PHP recorded the native build host as its target OS" unless
          s.sub!(/^#define PHP_OS ".*"$/, '#define PHP_OS "Kandelo"')
        odie "PHP target identity macro changed" unless
          s.sub!(/^#define PHP_UNAME ".*"$/, '#define PHP_UNAME "Kandelo wasm32-posix-kernel"')
      end

      # PHP publishes this string through phpinfo(). Keep the useful target
      # feature summary without embedding an ephemeral configure invocation.
      inreplace "main/build-defs.h" do |s|
        changed_identity = s.sub!(
          /^#define CONFIGURE_COMMAND .*$/,
          '#define CONFIGURE_COMMAND "Kandelo wasm32 Homebrew build"',
        )
        changed_extension = s.sub!(
          /^#define PHP_EXTENSION_DIR .*$/,
          %Q(#define PHP_EXTENSION_DIR       "#{GUEST_EXTENSION_DIR}"),
        )
        odie "PHP configure identity header changed" unless changed_identity
        odie "PHP extension-directory header changed" unless changed_extension
      end

      # PHP's generated libtool wrapper misparses compiler dependency flags,
      # then tries to rename an object file that it never created.
      inreplace "Makefile" do |s|
        dependency_flags = / -MMD -MF \S+ -MT \S+/
        s.gsub!(dependency_flags, "") if dependency_flags.match?(s.inreplace_string)
        odie "PHP Makefile retained unsupported dependency-tracking flags" if
          dependency_flags.match?(s.inreplace_string)
      end

      # WHY: PHP's libtool decides from the unknown Wasm host triple that
      # shared libraries cannot exist. Kandelo does support Wasm side modules;
      # enable any disabled PIC-object tag, then perform the final side-module
      # links through the SDK below. Some generated tag sets are already
      # enabled, so the required invariant is that no disabled tag remains.
      inreplace "libtool" do |s|
        disabled_shared_build = /^build_libtool_libs=no$/
        s.gsub!(disabled_shared_build, "build_libtool_libs=yes") if
          disabled_shared_build.match?(s.inreplace_string)
        odie "PHP libtool retained a disabled shared-build switch" if
          disabled_shared_build.match?(s.inreplace_string)
      end

      system host_make, "-j#{ENV.make_jobs}", "cli", "fpm"

      extension_units = {
        "opcache"   => %w[
          ZendAccelerator
          zend_accelerator_blacklist
          zend_accelerator_debug
          zend_accelerator_hash
          zend_accelerator_module
          zend_persist
          zend_persist_calc
          zend_file_cache
          zend_shared_alloc
          zend_accelerator_util_funcs
          shared_alloc_shm
          shared_alloc_mmap
          shared_alloc_posix
        ],
        "curl"      => %w[
          interface
          multi
          share
          curl_file
        ],
        "phar"      => %w[
          util
          tar
          zip
          stream
          func_interceptors
          dirstream
          phar
          phar_object
          phar_path_check
        ],
        "zend_test" => %w[
          test
          observer
          fiber
          iterators
          object_handlers
        ],
        "zip"       => %w[
          php_zip
          zip_stream
        ],
      }
      extension_units.each do |extension, units|
        system host_make, "-j#{ENV.make_jobs}",
          *units.map { |unit| "ext/#{extension}/#{unit}.lo" }
      end

      module_objects = lambda do |extension, units|
        units.map do |unit|
          object = buildpath/"ext/#{extension}/.libs/#{unit}.o"
          odie "PHP #{extension} object was not built: #{object}" unless object.file?
          object
        end
      end

      opcache_objects = module_objects.call("opcache", extension_units.fetch("opcache"))
      opcache = buildpath/"opcache.so"
      system kandelo_cc(root), "-shared", "-fPIC", "-o", opcache, *opcache_objects

      # WHY: curl.so is a dynamic side module, so every archive absorbed into
      # it must use PIC Wasm relocations. libcurl intentionally ships separate
      # normal and PIC archives; selecting the normal archive here fails at
      # link time and would also violate that dependency's output contract.
      libcurl_pic = libcurl/"lib/libcurl-pic.a"
      odie "PHP curl extension requires libcurl-pic.a" unless libcurl_pic.file?
      curl = buildpath/"curl.so"
      system kandelo_cc(root), "-shared", "-fPIC", "-o", curl,
        *module_objects.call("curl", extension_units.fetch("curl")),
        libcurl_pic

      phar = buildpath/"phar.so"
      system kandelo_cc(root), "-shared", "-fPIC", "-o", phar,
        *module_objects.call("phar", extension_units.fetch("phar"))

      zend_test = buildpath/"zend_test.so"
      system kandelo_cc(root), "-shared", "-fPIC", "-o", zend_test,
        *module_objects.call("zend_test", extension_units.fetch("zend_test"))

      zip = buildpath/"zip.so"
      system kandelo_cc(root), "-shared", "-fPIC", "-o", zip,
        *module_objects.call("zip", extension_units.fetch("zip")),
        libzip/"lib/libzip.a"

      # Ask the generated Makefile for intl's authoritative shared-object set.
      # Keeping this data-driven avoids silently omitting new upstream units
      # when PHP changes the extension's C/C++ source inventory.
      intl_makefile = buildpath/"Kandelo.intl.mk"
      intl_makefile.write <<~MAKE
        include Makefile
        .PHONY: kandelo-print-intl-objects
        kandelo-print-intl-objects:
        \t@printf '%s\\n' $(shared_objects_intl)
      MAKE
      intl_targets = Utils.safe_popen_read(
        host_make,
        "-s",
        "--no-print-directory",
        "-f",
        intl_makefile,
        "kandelo-print-intl-objects",
      ).split
      if intl_targets.empty? ||
         intl_targets.any? { |target| !target.match?(%r{\Aext/intl/.+\.lo\z}) }
        odie "PHP Makefile declared an invalid intl object set: #{intl_targets.inspect}"
      end
      system host_make, "-j#{ENV.make_jobs}", *intl_targets

      intl_loader = buildpath/"ext/intl/kandelo_icu_data_loader.o"
      resource("intl-icu-data-loader").stage do
        system kandelo_cc(root), "-fPIC", "-O2", "-c", Pathname.pwd/"intl-icu-data-loader.c",
          "-I#{icu}/include", "-o", intl_loader
      end
      intl_objects = intl_targets.map do |target|
        source = buildpath/target
        source.dirname/".libs/#{source.basename(".lo")}.o"
      end
      missing_intl_objects = intl_objects.reject(&:file?)
      unless missing_intl_objects.empty?
        missing = missing_intl_objects.map { |object| object.relative_path_from(buildpath) }
        odie "PHP intl build omitted declared PIC objects: #{missing.join(", ")}"
      end
      intl = buildpath/"intl.so"
      system kandelo_cc(root), "-shared", "-fPIC", "-Wl,--export=__tls_base", "-o", intl,
        *intl_objects,
        intl_loader,
        icu/"lib/libicui18n.a",
        icu/"lib/libicuio.a",
        icu/"lib/libicuuc.a",
        icu/"lib/libicudata.a",
        libcxx/"lib/libc++-pic.a",
        libcxx/"lib/libc++abi-pic.a"

      icu_data = icu/"share/icu.dat"
      odie "PHP intl dependency is missing ICU common data" unless icu_data.file?

      cli = buildpath/"php.optimized"
      fpm = buildpath/"php-fpm.optimized"
      system "wasm-opt", "-O2", "sapi/cli/php", "-o", cli
      system "wasm-opt", "-O2", "sapi/fpm/php-fpm", "-o", fpm
      kandelo_fork_instrument(cli)
      kandelo_fork_instrument(fpm)

      # Opcache calls env.fork supplied by the main PHP module instead of
      # importing kernel.kernel_fork directly. Instrument it at that true
      # side-module boundary so a preloaded opcache survives child replay.
      opcache_instrumented = Pathname("#{opcache}.fork-instrumented")
      system Pathname(root)/"scripts/run-wasm-fork-instrument.sh",
        opcache, "-o", opcache_instrumented, "--entry", "env.fork"
      opcache_instrumented.rename(opcache)

      kandelo_validate_wasm_artifact(cli, fork: :required, forbidden_paths: dependencies)
      kandelo_validate_wasm_artifact(fpm, fork: :required, forbidden_paths: dependencies)

      side_modules = {
        opcache   => :required,
        curl      => :forbidden,
        phar      => :forbidden,
        zend_test => :forbidden,
        zip       => :forbidden,
        intl      => :forbidden,
      }
      side_modules.each do |artifact, fork_policy|
        # WHY: WABT 1.0.41 cannot decode the exnref instructions added by
        # Kandelo's fork instrumentation. Binaryen's all-feature parser covers
        # both those instructions and the shared-memory/exception features used
        # by every PHP side module.
        system "wasm-opt", "--all-features", artifact, "-o", File::NULL
        system "bash", "-c", <<~SH
          set -euo pipefail
          . #{(Pathname(root)/"scripts/wasm-artifact-guards.sh").to_s.shellescape}
          artifact=#{artifact.to_s.shellescape}
          # WHY: relocatable Wasm side modules do not own a process entry
          # point or __abi_version export; the ABI-checked php/php-fpm main
          # module and bottle metadata bind their compatibility. If a future
          # linker does add an ABI export, reject it unless it is current.
          expected_abi=$(wasm_current_abi_version #{root.to_s.shellescape})
          abi_status=0
          artifact_abi=$(wasm_extract_abi_version "$artifact") || abi_status=$?
          if [ "$abi_status" -eq 0 ] && [ "$artifact_abi" != "$expected_abi" ]; then
            echo "ERROR: PHP side-module ABI $artifact_abi does not match $expected_abi: $artifact" >&2
            exit 1
          elif [ "$abi_status" -gt 1 ]; then
            echo "ERROR: PHP side-module ABI metadata could not be inspected: $artifact" >&2
            exit 1
          fi
          wasm_require_no_legacy_asyncify "$artifact"
          if [ #{(fork_policy == :required) ? "1" : "0"} -eq 1 ]; then
            if ! wasm_has_complete_fork_instrumentation "$artifact"; then
              echo "ERROR: PHP fork-capable side module has incomplete instrumentation: $artifact" >&2
              exit 1
            fi
          else
            wasm_require_no_fork_instrumentation "$artifact"
          fi
        SH
      end

      [cli, fpm, *side_modules.keys].each do |artifact|
        bytes = artifact.binread
        [buildpath, root, prefix, *dependencies].each do |forbidden|
          odie "#{artifact.basename} embeds staging path #{forbidden}" if bytes.include?(forbidden.to_s)
        end
        odie "#{artifact.basename} embeds a host workspace path" if
          bytes.match?(%r{/(?:private/tmp/|Users/|home/runner/(?:_work|work)/|nix/store/)})
      end

      bin.install cli => "php"
      sbin.install fpm => "php-fpm"
      (lib/"php/extensions").install(*side_modules.keys)
      # WHY: Homebrew's `install` helper moves a source file into the new keg.
      # ICU data belongs to the dependency keg and later consumers still need
      # it, so copy the immutable runtime data instead of consuming it.
      (share/"php").mkpath
      cp icu_data, share/"php/icu.dat"
      odie "PHP copied the wrong ICU common data byte length" if
        (share/"php/icu.dat").size != ICU_DATA_BYTES
      odie "PHP copied the wrong ICU common data digest" if
        Digest::SHA256.file(share/"php/icu.dat").hexdigest != ICU_DATA_SHA256
      odie "PHP installation consumed ICU common data from its dependency" unless icu_data.file?
      [bin/"php", sbin/"php-fpm", *(lib/"php/extensions").children].each do |artifact|
        chmod 0755, artifact
      end
      chmod 0644, share/"php/icu.dat"
    end

    man1.install "sapi/cli/php.1"
    man8.install "sapi/fpm/php-fpm.8"
    (pkgshare/"config").install "php.ini-development", "php.ini-production"
    (pkgshare/"config").install "sapi/fpm/php-fpm.conf", "sapi/fpm/www.conf"
    (pkgshare/"fpm").install "sapi/fpm/status.html"

    license_root = share/"licenses/php"
    license_root.install "LICENSE", "README.REDIST.BINS"
    {
      "TSRM"                 => "TSRM/LICENSE",
      "Zend"                 => "Zend/LICENSE",
      "Zend-asm"             => "Zend/asm/LICENSE",
      "date"                 => "ext/date/lib/LICENSE.rst",
      "fileinfo-libmagic"    => "ext/fileinfo/libmagic/LICENSE",
      "fpm"                  => "sapi/fpm/LICENSE",
      "mbstring-libmbfl"     => "ext/mbstring/libmbfl/LICENSE",
      "standard-libavifinfo" => "ext/standard/libavifinfo/LICENSE",
    }.each do |name, source|
      (license_root/name).install source
    end
  end

  test do
    php = bin/"php"
    fpm = sbin/"php-fpm"
    extensions = {
      "curl"      => lib/"php/extensions/curl.so",
      "intl"      => lib/"php/extensions/intl.so",
      "opcache"   => lib/"php/extensions/opcache.so",
      "phar"      => lib/"php/extensions/phar.so",
      "zend_test" => lib/"php/extensions/zend_test.so",
      "zip"       => lib/"php/extensions/zip.so",
    }
    opcache = extensions.fetch("opcache")
    icu_data = share/"php/icu.dat"
    dependency_icu_data = formula_opt_prefix("kandelo-dev/tap-core/icu")/"share/icu.dat"
    dash = formula_opt_bin("kandelo-dev/tap-core/dash")/"dash"
    # WHY: the Formula declares its exact test shell. Stage that closure
    # directly so an isolated PHP test never reaches into Kandelo's unrelated
    # default-program registry merely to populate /bin/sh.
    base_exec_programs = { "/bin/sh" => dash }
    platform_root = Pathname(kandelo_require_root!)
    # WHY: the isolated Formula runner starts from only the files explicitly
    # staged below, while real Kandelo images layer these identities and
    # OpenSSL policy from the canonical rootfs. Reuse those authoritative
    # platform files instead of making PHP carry private substitutes.
    platform_files = {
      "/etc/group"           => platform_root/"images/rootfs/etc/group",
      "/etc/passwd"          => platform_root/"images/rootfs/etc/passwd",
      "/etc/ssl/openssl.cnf" => platform_root/"images/rootfs/etc/ssl/openssl.cnf",
    }
    [php, fpm, dash, icu_data, dependency_icu_data, *extensions.values, *platform_files.values].each do |artifact|
      assert_path_exists artifact
    end
    assert_equal ICU_DATA_BYTES, icu_data.size
    assert_equal ICU_DATA_SHA256, Digest::SHA256.file(icu_data).hexdigest
    assert_equal ICU_DATA_SHA256, Digest::SHA256.file(dependency_icu_data).hexdigest
    assert_path_exists man1/"php.1"
    assert_path_exists man8/"php-fpm.8"
    assert_path_exists pkgshare/"config/php.ini-development"
    assert_path_exists pkgshare/"config/php-fpm.conf"
    assert_path_exists share/"licenses/php/LICENSE"
    assert_path_exists share/"licenses/php/README.REDIST.BINS"

    dependencies = %w[icu libcurl libcxx libiconv libxml2 libzip openssl sqlite zlib].map do |name|
      formula_opt_prefix("kandelo-dev/tap-core/#{name}")
    end
    kandelo_validate_wasm_artifact(php, fork: :required, forbidden_paths: dependencies)
    kandelo_validate_wasm_artifact(fpm, fork: :required, forbidden_paths: dependencies)
    extensions.each_value do |extension|
      system "wasm-opt", "--all-features", extension, "-o", File::NULL
    end

    import_check = testpath/"php-side-module-imports.mjs"
    import_check.write <<~'JS'
      import { readFileSync } from "node:fs";

      const [mainPath, ...sidePaths] = process.argv.slice(2);
      const main = new WebAssembly.Module(readFileSync(mainPath));
      const mainExports = new Set(WebAssembly.Module.exports(main).map(({ name }) => name));
      const loaderImports = new Set([
        "memory",
        "__indirect_function_table",
        "__stack_pointer",
        "__memory_base",
        "__table_base",
        "__cpp_exception",
        "__c_longjmp",
      ]);

      for (const sidePath of sidePaths) {
        const side = new WebAssembly.Module(readFileSync(sidePath));
        const sideExports = new Set(WebAssembly.Module.exports(side).map(({ name }) => name));
        const missing = WebAssembly.Module.imports(side)
          .filter(({ module, name }) => {
            if (module === "env") {
              // Kandelo's dynamic linker provides loader imports and creates
              // trampolines for a side module's self-imported exports.
              return !loaderImports.has(name)
                && !mainExports.has(name)
                && !sideExports.has(name);
            }
            if (module === "GOT.mem" || module === "GOT.func") {
              return !mainExports.has(name) && !sideExports.has(name);
            }
            return true;
          })
          .map(({ module, name, kind }) => `${module}.${name}:${kind}`)
          .sort();
        if (missing.length > 0) {
          throw new Error(`${sidePath} has unresolved imports:\n${missing.join("\n")}`);
        }
      }
    JS
    node = Pathname(ENV.fetch("HOMEBREW_KANDELO_NODE"))
    odie "Kandelo Formula test Node executable is unavailable" unless node.executable?
    # WHY: opcache receives generated fork imports during instrumentation.
    # The shared artifact guard validates that ABI surface during the build,
    # and the Node.js and Chromium opcache checks below instantiate it. A
    # Formula-local import list would duplicate generated ABI names and drift.
    import_checked_extensions = extensions.except("opcache")
    system node, import_check, php, *import_checked_extensions.values

    # WHY: Formula tests exercise these installed programs in an otherwise
    # empty guest. Naming argv[0] selects that isolated runner instead of
    # assembling unrelated default programs from Kandelo's package registry.
    assert_match(
      /^PHP 8\.3\.15 /,
      kandelo_run_wasm(
        php,
        ["--version"],
        argv0:         "#{GUEST_OPT_PREFIX}/bin/php",
        exec_programs: base_exec_programs,
      ),
    )
    assert_match(
      /^PHP 8\.3\.15 /,
      kandelo_run_browser_wasm(
        php,
        ["--version"],
        guest_program_path: "#{GUEST_OPT_PREFIX}/bin/php",
      ),
    )
    assert_match(
      /^PHP 8\.3\.15 /,
      kandelo_run_wasm(
        fpm,
        ["--version"],
        argv0:         "#{GUEST_OPT_PREFIX}/sbin/php-fpm",
        exec_programs: base_exec_programs,
      ),
    )
    assert_match(
      /^PHP 8\.3\.15 /,
      kandelo_run_browser_wasm(
        fpm,
        ["--version"],
        guest_program_path: "#{GUEST_OPT_PREFIX}/sbin/php-fpm",
      ),
    )

    core_script = <<~'PHP'
      $fail = static function (string $message): never {
          fwrite(STDERR, $message . "\n");
          exit(1);
      };
      foreach (["curl", "intl", "Zend OPcache", "Phar", "zend_test", "zip"] as $extension) {
          !extension_loaded($extension) || $fail("optional module leaked into base PHP: " . $extension);
      }

      $key = 0x4b0000 | (posix_getpid() & 0xffff);
      $queue = msg_get_queue($key, 0600);
      $pid = pcntl_fork();
      $pid >= 0 || $fail("pcntl_fork");
      if ($pid === 0) {
          msg_send($queue, 1, "sysv-message-ok") || exit(11);
          exit(0);
      }
      msg_receive($queue, 1, $type, 1024, $message, true, 0, $error)
          || $fail("msg_receive: " . $error);
      $waited = pcntl_waitpid($pid, $status);
      ($message === "sysv-message-ok" && $type === 1 &&
          $waited === $pid && pcntl_wifexited($status) && pcntl_wexitstatus($status) === 0)
          || $fail("SysV message/fork lifecycle");
      msg_remove_queue($queue) || $fail("msg_remove_queue");

      $semaphore = sem_get($key, 1, 0600, true);
      ($semaphore && sem_acquire($semaphore) && sem_release($semaphore) && sem_remove($semaphore))
          || $fail("SysV semaphore lifecycle");
      $memory = shm_attach($key, 4096, 0600);
      ($memory && shm_put_var($memory, 1, ["state" => "sysv-shm-ok"]) &&
          shm_get_var($memory, 1)["state"] === "sysv-shm-ok" && shm_remove($memory))
          || $fail("SysV shared-memory lifecycle");
      shm_detach($memory);

      $shmop = shmop_open($key, "c", 0600, 64);
      ($shmop && shmop_write($shmop, "shmop-ok", 0) === 8 &&
          shmop_read($shmop, 0, 8) === "shmop-ok" && shmop_delete($shmop))
          || $fail("shmop lifecycle");

      socket_create_pair(AF_UNIX, SOCK_STREAM, 0, $sockets) || $fail("socketpair");
      socket_write($sockets[0], "socket-ok") === 9 || $fail("socket write");
      socket_read($sockets[1], 9) === "socket-ok" || $fail("socket read");
      socket_close($sockets[0]);
      socket_close($sockets[1]);

      $database = dba_open("/tmp/php-core-test.dba", "n", "flatfile");
      ($database && dba_insert("key", "dba-ok", $database) &&
          dba_fetch("key", $database) === "dba-ok") || $fail("DBA flatfile");
      dba_close($database);

      echo "php-core-posix-ipc-ok\n";
    PHP
    assert_equal "php-core-posix-ipc-ok\n", kandelo_run_wasm(
      php,
      ["-n", "-r", core_script],
      argv0:                     "#{GUEST_OPT_PREFIX}/bin/php",
      env:                       { "HOME" => "/tmp", "TMPDIR" => "/tmp" },
      exec_programs:             base_exec_programs,
      expected_fork_descendants: 1,
    )

    binary_output = kandelo_run_wasm(
      php,
      ["-r", 'fwrite(STDOUT, "A" . chr(0) . "B" . chr(255));'],
      argv0:         "#{GUEST_OPT_PREFIX}/bin/php",
      exec_programs: base_exec_programs,
    )
    assert_equal [0x41, 0x00, 0x42, 0xff], binary_output.b.bytes
    assert_equal "opcache-default-off\n", kandelo_run_wasm(
      php,
      [
        "-d", "extension_dir=/usr/lib/php/extensions",
        "-d", "zend_extension=opcache",
        "-r", 'echo opcache_get_status(false) === false ? "opcache-default-off\n" : "unexpected\n";'
      ],
      argv0:         "#{GUEST_OPT_PREFIX}/bin/php",
      exec_programs: base_exec_programs,
      guest_files:   { "/usr/lib/php/extensions/opcache.so" => opcache },
    )
    unsupported_opcache = kandelo_run_wasm(
      php,
      [
        "-d", "extension_dir=/usr/lib/php/extensions",
        "-d", "zend_extension=opcache",
        "-d", "opcache.enable=1",
        "-d", "opcache.enable_cli=1",
        "-r", 'echo "must-not-run\n";'
      ],
      argv0:           "#{GUEST_OPT_PREFIX}/bin/php",
      exec_programs:   base_exec_programs,
      expected_status: 254,
      guest_files:     { "/usr/lib/php/extensions/opcache.so" => opcache },
      merge_stderr:    true,
    )
    assert_includes unsupported_opcache,
      "Kandelo requires opcache.file_cache_only=1 because cross-process MAP_SHARED is unavailable."
    refute_includes unsupported_opcache, "must-not-run"

    file_script = testpath/"php-file-smoke.php"
    file_script.write <<~PHP
      <?php
      echo "php-file-ok\\n";
    PHP
    extension_script = <<~'PHP'
      $fail = static function (string $message): never {
          fwrite(STDERR, $message . "\n");
          exit(1);
      };
      $required = [
          "bcmath", "calendar", "ctype", "curl", "date", "dba", "dom",
          "exif", "fileinfo", "filter", "ftp", "hash", "iconv", "intl",
          "json", "libxml", "mbstring", "mysqli", "mysqlnd", "openssl",
          "pcntl", "PDO", "pdo_mysql", "pdo_sqlite", "Phar", "posix",
          "random", "session", "shmop", "SimpleXML", "soap", "sockets",
          "sqlite3", "sysvmsg", "sysvsem", "sysvshm", "tokenizer", "xml",
          "xmlreader", "xmlwriter", "zend_test", "zip", "zlib",
          "Zend OPcache",
      ];
      foreach ($required as $extension) {
          extension_loaded($extension) || $fail("missing extension: " . $extension);
      }
      PHP_OS === "Kandelo" || $fail("wrong target OS: " . PHP_OS);
      mb_strlen("héllo") === 5 || $fail("mbstring");
      ctype_alpha("hello") || $fail("ctype");
      filter_var("test@example.com", FILTER_VALIDATE_EMAIL) || $fail("filter");
      count(token_get_all("<?php echo 1; ?>")) > 0 || $fail("tokenizer");
      $db = new SQLite3(":memory:");
      $db->exec("CREATE TABLE t(v TEXT)");
      $db->exec("INSERT INTO t VALUES('sqlite-ok')");
      $db->querySingle("SELECT v FROM t") === "sqlite-ok" || $fail("sqlite3");
      $pdo = new PDO("sqlite::memory:");
      $pdo->exec("CREATE TABLE t(v TEXT)");
      $pdo->exec("INSERT INTO t VALUES('pdo-ok')");
      $statement = $pdo->query("SELECT v FROM t");
      $statement->fetchColumn() === "pdo-ok" || $fail("pdo_sqlite");
      $metadata = $statement->getColumnMeta(0);
      ($metadata["table"] ?? null) === "t" || $fail("pdo column table metadata");
      (new SimpleXMLElement("<root><item>xml-ok</item></root>"))->item->__toString() === "xml-ok"
          || $fail("simplexml");
      (new finfo(FILEINFO_MIME_TYPE))->buffer("GIF89a") === "image/gif" || $fail("fileinfo");
      gzuncompress(gzcompress("zlib-ok")) === "zlib-ok" || $fail("zlib");
      $key = openssl_pkey_new();
      $csr = $key ? openssl_csr_new(["commonName" => "kandelo.test"], $key) : false;
      ($key && $csr) || $fail("openssl key/csr");
      bcadd("1.25", "2.75", 2) === "4.00" || $fail("bcmath");
      cal_days_in_month(CAL_GREGORIAN, 2, 2024) === 29 || $fail("calendar");
      iconv(
          "UTF-16LE",
          "UTF-8",
          iconv("UTF-8", "UTF-16LE", "café")
      ) === "café" || $fail("iconv");
      posix_getpid() > 0 || $fail("posix");
      session_save_path("/tmp");
      session_start();
      session_id() !== "" || $fail("session");
      opcache_get_status(false) !== false || $fail("opcache");

      $archive = new ZipArchive();
      $archive->open("/tmp/php-test.zip", ZipArchive::CREATE | ZipArchive::OVERWRITE) === true
          || $fail("zip open");
      $archive->addFromString("payload.txt", "zip-ok");
      $archive->setCompressionName("payload.txt", ZipArchive::CM_DEFLATE);
      $archive->close() || $fail("zip close");
      $archive = new ZipArchive();
      $archive->open("/tmp/php-test.zip") === true || $fail("zip reopen");
      $archive->getFromName("payload.txt") === "zip-ok" || $fail("zip payload");
      $archive->close();

      $phar = new Phar("/tmp/php-test.phar");
      $phar->startBuffering();
      $phar->addFromString("payload.txt", "phar-ok");
      $phar->setStub("<?php __HALT_COMPILER();");
      $phar->stopBuffering();
      file_get_contents("phar:///tmp/php-test.phar/payload.txt") === "phar-ok" || $fail("phar");
      zend_test_zend_ini_parse_quantity("2K") === 2048 || $fail("zend_test");

      Locale::getDisplayLanguage("fr", "en") === "French" || $fail("intl locale");
      $collator = new Collator("en_US");
      $words = ["banana", "apple", "cherry"];
      $collator->sort($words);
      $words === ["apple", "banana", "cherry"] || $fail("intl collator");

      $server = stream_socket_server("tcp://127.0.0.1:0", $errno, $error);
      $server !== false || $fail("loopback server: " . $errno . ":" . $error);
      $address = stream_socket_get_name($server, false);
      $pid = pcntl_fork();
      $pid >= 0 || $fail("pcntl_fork");
      if ($pid === 0) {
          fclose($server);
          $childLocale = Locale::getDisplayLanguage("fr", "en");
          $handle = curl_init("http://" . $address . "/probe");
          curl_setopt($handle, CURLOPT_RETURNTRANSFER, true);
          curl_setopt($handle, CURLOPT_TIMEOUT, 10);
          $body = curl_exec($handle);
          if ($body !== "curl-fork-ok\n" || $childLocale !== "French") {
              fwrite(STDERR, "child curl/intl replay failed\n");
              exit(21);
          }
          echo "fork-child=curl+intl-ok\n";
          exit(0);
      }
      $client = stream_socket_accept($server, 10);
      $client !== false || $fail("loopback accept");
      $request = "";
      while (!str_contains($request, "\r\n\r\n")) {
          $chunk = fread($client, 4096);
          ($chunk !== false && $chunk !== "") || $fail("loopback request");
          $request .= $chunk;
      }
      $response = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n" .
          "Content-Length: 13\r\nConnection: close\r\n\r\ncurl-fork-ok\n";
      fwrite($client, $response);
      fclose($client);
      fclose($server);
      $waited = pcntl_waitpid($pid, $status);
      ($waited === $pid && pcntl_wifexited($status) && pcntl_wexitstatus($status) === 0)
          || $fail("pcntl_waitpid status: " . $status);

      require "/home/linuxbrew/.linuxbrew/opt/php/share/php/php-file-smoke.php";
      echo json_encode([
          "php" => PHP_VERSION,
          "extensions" => "ok",
          "opcache" => "ok",
          "side_modules" => "ok",
      ]), "\n";
    PHP
    runtime_args = [
      "-d", "extension_dir=/usr/lib/php/extensions",
      "-d", "zend_extension=opcache",
      "-d", "extension=curl",
      "-d", "extension=phar",
      "-d", "extension=zend_test",
      "-d", "extension=zip",
      "-d", "extension=intl",
      "-d", "phar.readonly=0",
      "-d", "opcache.enable=1",
      "-d", "opcache.enable_cli=1",
      "-d", "opcache.file_cache=/tmp",
      "-d", "opcache.file_cache_only=1",
      "-r", extension_script
    ]
    guest_files = {
      "#{GUEST_OPT_PREFIX}/share/php/php-file-smoke.php" => file_script,
      GUEST_ICU_DATA                                     => icu_data,
      "/usr/lib/php/extensions/curl.so"                  => extensions.fetch("curl"),
      "/usr/lib/php/extensions/intl.so"                  => extensions.fetch("intl"),
      "/usr/lib/php/extensions/opcache.so"               => opcache,
      "/usr/lib/php/extensions/phar.so"                  => extensions.fetch("phar"),
      "/usr/lib/php/extensions/zend_test.so"             => extensions.fetch("zend_test"),
      "/usr/lib/php/extensions/zip.so"                   => extensions.fetch("zip"),
    }.merge(platform_files.slice("/etc/ssl/openssl.cnf"))
    node_extensions = kandelo_run_wasm(
      php,
      runtime_args,
      argv0:                     "#{GUEST_OPT_PREFIX}/bin/php",
      env:                       { "HOME" => "/tmp", "TMPDIR" => "/tmp" },
      exec_programs:             base_exec_programs,
      expected_fork_descendants: 1,
      guest_files:               guest_files,
    )
    assert_includes node_extensions, "fork-child=curl+intl-ok\n"
    assert_includes node_extensions, "php-file-ok\n"
    assert_includes node_extensions, '"php":"8.3.15"'
    assert_includes node_extensions, '"extensions":"ok"'
    assert_includes node_extensions, '"opcache":"ok"'
    assert_includes node_extensions, '"side_modules":"ok"'

    browser_extensions = kandelo_run_browser_wasm(
      php,
      runtime_args,
      guest_program_path: "#{GUEST_OPT_PREFIX}/bin/php",
      env:                { "HOME" => "/tmp", "TMPDIR" => "/tmp" },
      guest_files:        guest_files,
      timeout_ms:         180_000,
    )
    assert_includes browser_extensions, "fork-child=curl+intl-ok\n"
    assert_includes browser_extensions, "php-file-ok\n"
    assert_includes browser_extensions, '"php":"8.3.15"'
    assert_includes browser_extensions, '"extensions":"ok"'
    assert_includes browser_extensions, '"opcache":"ok"'
    assert_includes browser_extensions, '"side_modules":"ok"'

    sqlite_dir = testpath/"sqlite"
    sqlite_dir.mkpath
    sqlite_script = <<~'PHP'
      $db = new PDO("sqlite:/work/test.db");
      $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
      $db->exec("CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT)");
      for ($i = 0; $i < 10; $i++) {
          $db->exec("BEGIN IMMEDIATE");
          $db->exec("INSERT INTO t(val) VALUES('row')");
          $db->exec("COMMIT");
      }
      echo "count=", $db->query("SELECT COUNT(*) FROM t")->fetchColumn(), "\n";
    PHP
    [10, 20].each do |expected_count|
      assert_equal "count=#{expected_count}\n", kandelo_run_wasm(
        php,
        ["-r", sqlite_script],
        argv0:                     "#{GUEST_OPT_PREFIX}/bin/php",
        env:                       { "HOME" => "/tmp", "TMPDIR" => "/tmp" },
        exec_programs:             base_exec_programs,
        writable_host_directories: { "/work" => sqlite_dir },
      )
    end

    fastcgi_source = testpath/"php-fastcgi-smoke.c"
    fastcgi = testpath/"php-fastcgi-smoke.wasm"
    fastcgi_source.write <<~'C'
      #include <arpa/inet.h>
      #include <errno.h>
      #include <netinet/in.h>
      #include <stdint.h>
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <sys/socket.h>
      #include <time.h>
      #include <unistd.h>

      enum {
        FCGI_VERSION_1 = 1,
        FCGI_BEGIN_REQUEST = 1,
        FCGI_END_REQUEST = 3,
        FCGI_PARAMS = 4,
        FCGI_STDIN = 5,
        FCGI_STDOUT = 6,
        FCGI_STDERR = 7,
        FCGI_RESPONDER = 1,
      };

      static int write_all(int fd, const void *buffer, size_t length) {
        const unsigned char *bytes = buffer;
        while (length > 0) {
          ssize_t count = write(fd, bytes, length);
          if (count <= 0) return -1;
          bytes += count;
          length -= (size_t)count;
        }
        return 0;
      }

      static int send_record(int fd, unsigned char type, const void *body, size_t length) {
        unsigned char header[8] = {
          FCGI_VERSION_1, type, 0, 1,
          (unsigned char)(length >> 8), (unsigned char)length, 0, 0,
        };
        return write_all(fd, header, sizeof(header)) ||
          (length > 0 && write_all(fd, body, length));
      }

      static int append_length(unsigned char *buffer, size_t capacity, size_t *offset, size_t length) {
        if (length < 128) {
          if (*offset + 1 > capacity) return -1;
          buffer[(*offset)++] = (unsigned char)length;
        } else {
          if (*offset + 4 > capacity) return -1;
          buffer[(*offset)++] = (unsigned char)((length >> 24) | 0x80);
          buffer[(*offset)++] = (unsigned char)(length >> 16);
          buffer[(*offset)++] = (unsigned char)(length >> 8);
          buffer[(*offset)++] = (unsigned char)length;
        }
        return 0;
      }

      static int append_param(
        unsigned char *buffer, size_t capacity, size_t *offset,
        const char *name, const char *value
      ) {
        size_t name_length = strlen(name);
        size_t value_length = strlen(value);
        if (append_length(buffer, capacity, offset, name_length) ||
            append_length(buffer, capacity, offset, value_length) ||
            *offset + name_length + value_length > capacity) return -1;
        memcpy(buffer + *offset, name, name_length);
        *offset += name_length;
        memcpy(buffer + *offset, value, value_length);
        *offset += value_length;
        return 0;
      }

      int main(int argc, char **argv) {
        static const char *params[][2] = {
          { "GATEWAY_INTERFACE", "CGI/1.1" },
          { "REQUEST_METHOD", "GET" },
          { "SCRIPT_FILENAME", "/home/linuxbrew/.linuxbrew/opt/php/share/php/php-fpm-smoke.php" },
          { "SCRIPT_NAME", "/php-fpm-smoke.php" },
          { "REQUEST_URI", "/php-fpm-smoke.php" },
          { "DOCUMENT_ROOT", "/tmp" },
          { "SERVER_PROTOCOL", "HTTP/1.1" },
          { "SERVER_SOFTWARE", "kandelo-formula-test" },
          { "REMOTE_ADDR", "127.0.0.1" },
          { "REMOTE_PORT", "31337" },
          { "SERVER_ADDR", "127.0.0.1" },
          { "SERVER_PORT", "19000" },
          { "SERVER_NAME", "localhost" },
          { "CONTENT_LENGTH", "0" },
        };
        struct sockaddr_in address;
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 20 * 1000 * 1000 };
        unsigned char encoded[2048];
        unsigned char begin[8] = { 0, FCGI_RESPONDER, 0, 0, 0, 0, 0, 0 };
        size_t encoded_length = 0;
        int fd = -1;
        int port;

        if (argc != 2 || sscanf(argv[1], "%d", &port) != 1 ||
            port < 1 || port > 65535) return 2;
        memset(&address, 0, sizeof(address));
        address.sin_family = AF_INET;
        address.sin_port = htons((unsigned short)port);
        address.sin_addr.s_addr = htonl(0x7f000001UL);
        for (int attempt = 0; attempt < 500; attempt++) {
          fd = socket(AF_INET, SOCK_STREAM, 0);
          if (fd < 0) return 3;
          if (connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0) break;
          close(fd);
          fd = -1;
          nanosleep(&pause, NULL);
        }
        if (fd < 0) {
          fprintf(stderr, "php-fpm did not become ready: %s\n", strerror(errno));
          return 4;
        }
        for (size_t index = 0; index < sizeof(params) / sizeof(params[0]); index++) {
          if (append_param(
            encoded, sizeof(encoded), &encoded_length, params[index][0], params[index][1]
          )) return 5;
        }
        if (send_record(fd, FCGI_BEGIN_REQUEST, begin, sizeof(begin)) ||
            send_record(fd, FCGI_PARAMS, encoded, encoded_length) ||
            send_record(fd, FCGI_PARAMS, NULL, 0) ||
            send_record(fd, FCGI_STDIN, NULL, 0)) return 6;

        for (;;) {
          unsigned char header[8];
          unsigned char body[UINT16_MAX + 255];
          size_t offset = 0;
          size_t body_length;
          size_t padding;
          while (offset < sizeof(header)) {
            ssize_t count = read(fd, header + offset, sizeof(header) - offset);
            if (count <= 0) return 7;
            offset += (size_t)count;
          }
          body_length = ((size_t)header[4] << 8) | header[5];
          padding = header[6];
          if (header[0] != FCGI_VERSION_1 || header[2] != 0 || header[3] != 1 ||
              body_length + padding > sizeof(body)) return 11;
          offset = 0;
          while (offset < body_length + padding) {
            ssize_t count = read(fd, body + offset, body_length + padding - offset);
            if (count <= 0) return 8;
            offset += (size_t)count;
          }
          if (header[1] == FCGI_STDOUT && body_length > 0) {
            if (write_all(STDOUT_FILENO, body, body_length)) return 9;
          } else if (header[1] == FCGI_STDERR && body_length > 0) {
            if (write_all(STDERR_FILENO, body, body_length)) return 10;
          } else if (header[1] == FCGI_END_REQUEST) {
            if (body_length != 8 || body[0] != 0 || body[1] != 0 ||
                body[2] != 0 || body[3] != 0 || body[4] != 0) return 12;
            break;
          }
        }
        close(fd);
        return 0;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, "-O2", fastcgi_source, "-o", fastcgi
    end

    fpm_config = testpath/"php-fpm.conf"
    fpm_config.write <<~CONF
      [global]
      daemonize = no
      error_log = /dev/stderr
      log_level = notice

      [www]
      user = nobody
      group = nobody
      listen = 127.0.0.1:19000
      pm = static
      pm.max_children = 1
      clear_env = no
      catch_workers_output = yes
    CONF
    fpm_script = testpath/"php-fpm-smoke.php"
    fpm_script.write <<~PHP
      <?php
      $required = ["mysqli", "pdo_mysql", "sqlite3", "SimpleXML", "openssl", "Zend OPcache"];
      foreach ($required as $extension) {
          if (!extension_loaded($extension)) {
              http_response_code(500);
              echo "missing-extension:", $extension;
              exit;
          }
      }
      $db = new SQLite3(":memory:");
      $db->exec("CREATE TABLE t(v TEXT)");
      $db->exec("INSERT INTO t VALUES('fpm-db-ok')");
      $xml = new SimpleXMLElement("<root><item>fpm-xml-ok</item></root>");
      $opcache = opcache_get_status(false);
      if ($db->querySingle("SELECT v FROM t") !== "fpm-db-ok" ||
          $xml->item->__toString() !== "fpm-xml-ok" ||
          $opcache === false) {
          http_response_code(500);
          echo "php-fpm-runtime-failed";
          exit;
      }
      header("Content-Type: text/plain");
      echo "php-fpm-opcache-ok";
    PHP
    service_script = <<~'SH'
      set -eu
      fpm=/usr/local/sbin/php-fpm
      "$fpm" \
        --nodaemonize \
        --fpm-config /etc/php-fpm.conf \
        -d extension_dir=/usr/lib/php/extensions \
        -d zend_extension=opcache \
        -d opcache.enable=1 \
        -d opcache.file_cache=/tmp \
        -d opcache.file_cache_only=1 \
        &
      fpm_pid=$!
      cleanup() {
        kill -TERM "$fpm_pid" 2>/dev/null || :
        wait "$fpm_pid" 2>/dev/null || :
      }
      trap cleanup EXIT HUP INT TERM
      /usr/local/bin/php-fastcgi-smoke 19000
      kill -TERM "$fpm_pid"
      wait "$fpm_pid"
      trap - EXIT HUP INT TERM
      printf '\nphp-fpm-service-%s-ok\n' "$KANDELO_RUNTIME"
    SH
    service_programs = {
      "/bin/sh"                          => dash,
      "/usr/local/bin/php-fastcgi-smoke" => fastcgi,
      "/usr/local/sbin/php-fpm"          => fpm,
    }
    service_files = {
      "/etc/php-fpm.conf"                               => fpm_config,
      "#{GUEST_OPT_PREFIX}/share/php/php-fpm-smoke.php" => fpm_script,
      "/usr/lib/php/extensions/opcache.so"              => opcache,
    }.merge(platform_files)
    node_service = kandelo_run_wasm(
      dash,
      ["-c", service_script],
      argv0:                             "/bin/sh",
      env:                               {
        "HOME"            => "/tmp",
        "KANDELO_RUNTIME" => "node",
        "TIMEOUT"         => "180000",
        "TMPDIR"          => "/tmp",
      },
      exec_programs:                     service_programs,
      # WHY: dash and the FastCGI client exit normally, while FPM intentionally
      # terminates its worker with SIGTERM during the graceful service teardown.
      expected_fork_descendant_statuses: [0, 0, 143],
      guest_files:                       service_files,
      merge_stderr:                      true,
    )
    assert_includes node_service, "php-fpm-opcache-ok"
    assert_includes node_service, "php-fpm-service-node-ok"

    browser_service = kandelo_run_browser_wasm(
      dash,
      ["-c", service_script],
      argv0:              "sh",
      guest_program_path: "/bin/sh",
      env:                {
        "HOME"            => "/tmp",
        "KANDELO_RUNTIME" => "browser",
        "TMPDIR"          => "/tmp",
      },
      exec_programs:      service_programs.except("/bin/sh"),
      guest_files:        service_files,
      timeout_ms:         180_000,
      merge_stderr:       true,
    )
    assert_includes browser_service, "php-fpm-opcache-ok"
    assert_includes browser_service, "php-fpm-service-browser-ok"

    [php, fpm, opcache].each do |artifact|
      bytes = artifact.binread
      refute_includes bytes, prefix.to_s
      refute_includes bytes, "/nix/store/"
      refute_match %r{/private/tmp/[^/]+/}, bytes
      refute_match %r{/Users/[^/]+/}, bytes
    end
  end

  private

  def build_private_sysroot!(libcxx)
    source = Pathname(ENV.fetch("WASM_POSIX_SYSROOT"))
    destination = buildpath/"kandelo-private-sysroot"
    destination.mkpath
    source.children.each { |entry| cp_r entry, destination }

    rm_r destination/"include/c++/v1" if (destination/"include/c++/v1").exist?
    (destination/"include/c++").mkpath
    cp_r libcxx/"include/c++/v1", destination/"include/c++/v1"

    (destination/"lib").mkpath
    %w[libc++.a libc++abi.a libstdc++.a].each do |archive|
      rm destination/"lib"/archive if (destination/"lib"/archive).exist?
    end
    cp libcxx/"lib/libc++.a", destination/"lib/libc++.a"
    cp libcxx/"lib/libc++abi.a", destination/"lib/libc++abi.a"
    cp libcxx/"lib/libc++.a", destination/"lib/libstdc++.a"
    destination
  end
end
