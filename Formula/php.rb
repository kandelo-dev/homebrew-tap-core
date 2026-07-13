require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s
require "digest"
require "json"
require "shellwords"

class Php < Formula
  include KandeloFormulaSupport

  GUEST_PREFIX = "/home/linuxbrew/.linuxbrew".freeze
  GUEST_OPT_PREFIX = "#{GUEST_PREFIX}/opt/php".freeze
  GUEST_SYSCONFDIR = "#{GUEST_PREFIX}/etc".freeze
  GUEST_PHP_CONFIG_DIR = "#{GUEST_SYSCONFDIR}/php".freeze
  GUEST_LOCALSTATEDIR = "#{GUEST_PREFIX}/var".freeze
  GUEST_EXTENSION_DIR = "#{GUEST_OPT_PREFIX}/lib/php/extensions".freeze
  GUEST_ICU_DATA = "#{GUEST_OPT_PREFIX}/lib/php/icu.dat".freeze
  MINIMUM_KANDELO_ABI = 39
  ICU_DATA_SHA256 = "dc778b9ffe18ed319ad3fb70754f80e51cf7b6dbfff38fc0c0a5f27bb5463dad".freeze
  ICU_DATA_BYTES = 30_782_896

  OPCACHE_OBJECTS = %w[
    ZendAccelerator zend_accelerator_blacklist zend_accelerator_debug
    zend_accelerator_hash zend_accelerator_module zend_persist zend_persist_calc
    zend_file_cache zend_shared_alloc zend_accelerator_util_funcs
    shared_alloc_shm shared_alloc_mmap shared_alloc_posix
  ].freeze
  CURL_OBJECTS = %w[interface multi share curl_file].freeze
  PHAR_OBJECTS = %w[
    util tar zip stream func_interceptors dirstream phar phar_object phar_path_check
  ].freeze
  ZEND_TEST_OBJECTS = %w[test observer fiber iterators object_handlers].freeze
  ZIP_OBJECTS = %w[php_zip zip_stream].freeze
  MAIN_ONLY_EXPORTS = %w[
    setgid setuid initgroups writev asctime rand srand remove inet_pton inet_ntop
    sched_yield alarm basename OCSP_basic_verify OCSP_cert_status_str
    OCSP_crl_reason_str OCSP_response_status_str SSL_alert_desc_string_long
    aligned_alloc div modf round tanhf swprintf wcstod wcstof wcstol wcstold
    wcstoll wcstoul wcstoull wmemchr wmemcmp pthread_cond_broadcast
    pthread_cond_destroy pthread_cond_signal pthread_cond_timedwait
    pthread_cond_wait pthread_detach pthread_getspecific pthread_key_create
    pthread_self pthread_setspecific
  ].freeze

  desc "Server-side scripting language CLI and FastCGI runtime for Kandelo"
  homepage "https://www.php.net/"
  url "https://www.php.net/distributions/php-8.3.15.tar.gz"
  sha256 "67073c3c9c56c86461e0715d9e1806af5ddffe8e6e2eb9781f7923bbb5bd67fa"
  license "PHP-3.01"

  depends_on "binaryen" => [:build, :test]
  depends_on "pkgconf" => [:build, :test]
  depends_on "wabt" => [:build, :test]
  depends_on "automattic/kandelo-homebrew/icu"
  depends_on "automattic/kandelo-homebrew/libcurl"
  depends_on "automattic/kandelo-homebrew/libcxx"
  depends_on "automattic/kandelo-homebrew/libiconv"
  depends_on "automattic/kandelo-homebrew/libxml2"
  depends_on "automattic/kandelo-homebrew/libzip"
  depends_on "automattic/kandelo-homebrew/openssl"
  depends_on "automattic/kandelo-homebrew/sqlite"
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "bin/php"
  skip_clean "sbin/php-fpm"
  skip_clean "lib/php/extensions"

  def install
    kandelo_require_arch!("wasm32")
    dependencies = {
      "icu"      => formula_opt_prefix("automattic/kandelo-homebrew/icu"),
      "libcxx"   => formula_opt_prefix("automattic/kandelo-homebrew/libcxx"),
      "libcurl"  => formula_opt_prefix("automattic/kandelo-homebrew/libcurl"),
      "libiconv" => formula_opt_prefix("automattic/kandelo-homebrew/libiconv"),
      "libxml2"  => formula_opt_prefix("automattic/kandelo-homebrew/libxml2"),
      "libzip"   => formula_opt_prefix("automattic/kandelo-homebrew/libzip"),
      "openssl"  => formula_opt_prefix("automattic/kandelo-homebrew/openssl"),
      "sqlite"   => formula_opt_prefix("automattic/kandelo-homebrew/sqlite"),
      "zlib"     => formula_opt_prefix("automattic/kandelo-homebrew/zlib"),
    }
    {
      "icu"      => %w[lib/libicudata.a lib/libicui18n.a lib/libicuio.a lib/libicuuc.a share/icu.dat],
      "libcxx"   => %w[lib/libc++.a lib/libc++abi.a lib/libc++-pic.a lib/libc++abi-pic.a],
      "libcurl"  => %w[lib/libcurl.a lib/libcurl-pic.a],
      "libiconv" => %w[lib/libiconv.a lib/libcharset.a],
      "libxml2"  => %w[lib/libxml2.a],
      "libzip"   => %w[lib/libzip.a],
      "openssl"  => %w[lib/libssl.a lib/libcrypto.a],
      "sqlite"   => %w[lib/libsqlite3.a],
      "zlib"     => %w[lib/libz.a],
    }.each do |name, files|
      files.each do |relative_path|
        odie "#{name} is missing PHP input #{relative_path}" unless (dependencies.fetch(name)/relative_path).file?
      end
    end
    libcxx = dependencies.fetch("libcxx")
    odie "libcxx is missing C++ headers" unless (libcxx/"include/c++/v1").directory?

    tap_root = Pathname(__dir__).parent
    patch_file = tap_root/"Kandelo/patches/php-8.3.15-kandelo.patch"
    icu_loader = tap_root/"Kandelo/formula_support/php/intl-icu-data-loader.c"
    odie "PHP Kandelo patch is missing" unless patch_file.file?
    odie "PHP intl ICU loader is missing" unless icu_loader.file?
    system "patch", "-p1", "-i", patch_file
    patch_phar_fixture!

    installed_modules = {}
    kandelo_wasm_build do |root|
      private_sysroot = build_private_sysroot!(libcxx)
      ENV["WASM_POSIX_SYSROOT"] = private_sysroot

      prefix_maps = {
        buildpath.to_s                      => "/usr/src/php-#{version}",
        root.to_s                           => "/usr/src/kandelo",
        private_sysroot.to_s                => "/usr/src/kandelo-sysroot",
        dependencies.fetch("zlib").to_s     => "/usr/src/kandelo-deps/zlib",
        dependencies.fetch("sqlite").to_s   => "/usr/src/kandelo-deps/sqlite",
        dependencies.fetch("openssl").to_s  => "/usr/src/kandelo-deps/openssl",
        dependencies.fetch("libxml2").to_s  => "/usr/src/kandelo-deps/libxml2",
        dependencies.fetch("libiconv").to_s => "/usr/src/kandelo-deps/libiconv",
        dependencies.fetch("libzip").to_s   => "/usr/src/kandelo-deps/libzip",
        dependencies.fetch("libcurl").to_s  => "/usr/src/kandelo-deps/libcurl",
        dependencies.fetch("icu").to_s      => "/usr/src/kandelo-deps/icu",
        libcxx.to_s                         => "/usr/src/kandelo-deps/libcxx",
        "/nix/store"                        => "/usr/src/toolchain",
      }.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end
      ENV["CFLAGS"] = ["-O2", "-gline-tables-only", "-DZEND_USE_ASM_ARITHMETIC=0", *prefix_maps].join(" ")
      ENV["CPPFLAGS"] = dependencies.values.map { |dependency| "-I#{dependency}/include" }.join(" ")
      ENV["LDFLAGS"] = [
        *dependencies.values.map { |dependency| "-L#{dependency}/lib" },
        "-ldl",
        "-Wl,--export-all",
        *MAIN_ONLY_EXPORTS.flat_map { |symbol| ["-u", symbol] },
        "-Wl,-z,stack-size=4194304",
      ].join(" ")
      ENV["PKG_CONFIG_PATH"] = dependencies.values
                                           .map { |dependency| dependency/"lib/pkgconfig" }
                                           .join(File::PATH_SEPARATOR)
      ENV.delete("PKG_CONFIG_LIBDIR")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")

      zlib = dependencies.fetch("zlib")
      sqlite = dependencies.fetch("sqlite")
      openssl = dependencies.fetch("openssl")
      libxml2 = dependencies.fetch("libxml2")
      libiconv = dependencies.fetch("libiconv")
      libzip = dependencies.fetch("libzip")
      libcurl = dependencies.fetch("libcurl")
      icu = dependencies.fetch("icu")
      ENV["ZLIB_CFLAGS"] = "-I#{zlib}/include"
      ENV["ZLIB_LIBS"] = "-L#{zlib}/lib -lz"
      ENV["SQLITE_CFLAGS"] = "-I#{sqlite}/include"
      ENV["SQLITE_LIBS"] = "-L#{sqlite}/lib -lsqlite3"
      ENV["OPENSSL_CFLAGS"] = "-I#{openssl}/include"
      ENV["OPENSSL_LIBS"] = "-L#{openssl}/lib -lssl -lcrypto"
      ENV["LIBXML_CFLAGS"] = "-I#{libxml2}/include/libxml2 -I#{libxml2}/include"
      ENV["LIBXML_LIBS"] = "-L#{libxml2}/lib -lxml2 -L#{libiconv}/lib -liconv -lcharset -L#{zlib}/lib -lz"
      ENV["ICONV_CFLAGS"] = "-I#{libiconv}/include"
      ENV["ICONV_LIBS"] = "-L#{libiconv}/lib -liconv -lcharset"
      ENV["LIBZIP_CFLAGS"] = "-I#{libzip}/include"
      ENV["LIBZIP_LIBS"] = "-L#{libzip}/lib -lzip -L#{zlib}/lib -lz"
      ENV["CURL_CFLAGS"] = "-I#{libcurl}/include -DCURL_STATICLIB"
      ENV["CURL_LIBS"] = "-L#{libcurl}/lib -lcurl"
      ENV["ICU_CFLAGS"] = "-I#{icu}/include"
      ENV["ICU_LIBS"] = "-L#{icu}/lib -licui18n -licuio -licuuc -licudata"
      ENV["PHP_UNAME"] = "Kandelo wasm32-posix-kernel"
      ENV["ac_cv_lib_iconv_libiconv"] = "yes"
      ENV["ac_cv_lib_curl_curl_easy_perform"] = "yes"
      %w[
        zip_file_set_mtime zip_file_set_encryption zip_libzip_version
        zip_register_progress_callback_with_state zip_register_cancel_callback_with_state
        zip_compression_method_supported
      ].each { |symbol| ENV["ac_cv_lib_zip_#{symbol}"] = "yes" }
      ENV["ac_cv_func_unshare"] = "no"
      ENV["ac_cv_func_setproctitle_fast"] = "no"
      ENV["ac_cv_func_std_syslog"] = "no"

      system kandelo_configure(root),
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--sysconfdir=#{GUEST_SYSCONFDIR}",
        "--localstatedir=#{GUEST_LOCALSTATEDIR}",
        "--with-config-file-path=#{GUEST_PHP_CONFIG_DIR}",
        "--with-config-file-scan-dir=#{GUEST_PHP_CONFIG_DIR}/conf.d",
        "--disable-all",
        "--disable-rpath",
        "--disable-cgi",
        "--disable-phpdbg",
        "--enable-cli",
        "--enable-fpm",
        "--with-fpm-user=root",
        "--with-fpm-group=root",
        "--enable-opcache",
        "--disable-opcache-jit",
        "--disable-huge-code-pages",
        "--enable-intl=shared",
        "--enable-mbstring",
        "--disable-mbregex",
        "--enable-ctype",
        "--enable-tokenizer",
        "--enable-filter",
        "--enable-bcmath",
        "--enable-calendar",
        "--enable-dba",
        "--enable-ftp",
        "--with-iconv=#{libiconv}",
        "--with-curl=shared",
        "--enable-pcntl",
        "--enable-phar=shared",
        "--enable-posix",
        "--enable-shmop",
        "--enable-soap",
        "--enable-sockets",
        "--enable-sysvmsg",
        "--enable-sysvsem",
        "--enable-sysvshm",
        "--enable-zend-test=shared",
        "--without-valgrind",
        "--without-pcre-jit",
        "--disable-fiber-asm",
        "--disable-zend-signals",
        "--enable-zend-max-execution-timers",
        "--enable-session",
        "--with-sqlite3",
        "--enable-pdo",
        "--with-pdo-sqlite",
        "--with-pdo-mysql=mysqlnd",
        "--with-mysqli=mysqlnd",
        "--enable-fileinfo",
        "--enable-exif",
        "--with-zlib",
        "--with-openssl",
        "--with-libxml",
        "--enable-xml",
        "--enable-dom",
        "--enable-simplexml",
        "--enable-xmlreader",
        "--enable-xmlwriter",
        "--with-zip=shared",
        "--cache-file=#{buildpath}/config.cache"

      patch_generated_build_files!
      extra_includes = "-I#{libxml2}/include"
      system "make", "-j#{ENV.make_jobs}", "EXTRA_CFLAGS=#{extra_includes}", "cli"
      system "make", "-j#{ENV.make_jobs}", "EXTRA_CFLAGS=#{extra_includes}", "fpm"

      output = buildpath/"kandelo-output"
      output.mkpath
      side_modules = {
        "opcache"   => build_opcache!(root, output, extra_includes),
        "curl"      => build_curl!(root, output, extra_includes, libcurl),
        "phar"      => build_phar!(root, output, extra_includes),
        "zend_test" => build_zend_test!(root, output, extra_includes),
        "zip"       => build_zip!(root, output, extra_includes, libzip),
        "intl"      => build_intl!(root, output, extra_includes, dependencies, icu_loader),
      }
      php = output/"php"
      php_fpm = output/"php-fpm"
      system "wasm-opt", "-O2", "sapi/cli/php", "-o", php
      system "wasm-opt", "-O2", "sapi/fpm/php-fpm", "-o", php_fpm
      kandelo_fork_instrument php
      kandelo_fork_instrument php_fpm
      [php, php_fpm].each { |artifact| chmod 0755, artifact }

      dependency_paths = dependencies.values
      kandelo_validate_wasm_artifact(php, fork: :required, forbidden_paths: dependency_paths)
      kandelo_validate_wasm_artifact(php_fpm, fork: :required, forbidden_paths: dependency_paths)
      validate_php_artifacts!(root, php, php_fpm, side_modules, dependency_paths)

      stage = buildpath/"kandelo-stage"
      system "make", "install-cli", "install-fpm", "INSTALL_ROOT=#{stage}"
      staged_prefix = stage/GUEST_OPT_PREFIX.delete_prefix("/")
      odie "PHP did not stage its guest opt prefix" unless staged_prefix.directory?
      rm staged_prefix/"bin/php" if (staged_prefix/"bin/php").exist?
      rm staged_prefix/"sbin/php-fpm" if (staged_prefix/"sbin/php-fpm").exist?
      prefix.install staged_prefix.children
      bin.install php
      sbin.install php_fpm
      (lib/"php/extensions").install side_modules.values

      icu_data = dependencies.fetch("icu")/"share/icu.dat"
      odie "ICU data byte length drifted" if icu_data.size != ICU_DATA_BYTES
      odie "ICU data digest drifted" if Digest::SHA256.file(icu_data).hexdigest != ICU_DATA_SHA256
      (lib/"php").mkpath
      cp icu_data, lib/"php/icu.dat"
      installed_modules = side_modules
    end

    install_runtime_config!
    odie "PHP CLI was not installed" unless (bin/"php").file?
    odie "PHP FPM was not installed" unless (sbin/"php-fpm").file?
    installed_modules.each_key do |name|
      odie "#{name}.so was not installed" unless (lib/"php/extensions/#{name}.so").file?
    end
  end

  def post_install
    return if HOMEBREW_PREFIX.to_s != GUEST_PREFIX

    (etc/"php-fpm.d").mkpath
    (var/"log").mkpath
    (var/"run").mkpath
    config_root = libexec/"config"
    {
      config_root/"php.ini"            => etc/"php/php.ini",
      config_root/"php-fpm.conf"       => etc/"php-fpm.conf",
      config_root/"php-fpm.d/www.conf" => etc/"php-fpm.d/www.conf",
    }.each do |source, destination|
      destination.dirname.mkpath
      cp source, destination unless destination.exist?
    end
  end

  private

  def patch_phar_fixture!
    fixture = buildpath/"ext/phar/tests/files/nophar.phar"
    return unless fixture.file?

    bytes = fixture.binread
    return unless bytes.include?("0xffffffff")

    rewritten = bytes.gsub("0xffffffff", "(-1)      ")
    odie "nophar.phar signature magic drifted" unless rewritten.end_with?("GBMB")
    algorithm = rewritten.byteslice(-8, 4).unpack1("V")
    odie "nophar.phar signature algorithm drifted: #{algorithm}" if algorithm != 2
    rewritten[-28, 20] = Digest::SHA1.digest(rewritten.byteslice(0...-28))
    fixture.binwrite(rewritten)
  end

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

  def patch_generated_build_files!
    config = buildpath/"main/php_config.h"
    text = config.read
    # PHP's cross-compile fallback treats every *linux* host as glibc, whose
    # fopencookie seek callback uses off64_t. musl's callback uses off_t.
    %w[
      HAVE_DNS_SEARCH HAVE_DNS_SEARCH_FUNC HAVE_RES_NSEARCH HAVE_RES_NDESTROY
      HAVE_RES_SEARCH HAVE_FUNOPEN HAVE_STD_SYSLOG HAVE_SETPROCTITLE
      HAVE_SETPROCTITLE_FAST HAVE_RAND_EGD HAVE_FORKX HAVE_RFORK
      HAVE_SQLITE3_COLUMN_TABLE_NAME COOKIE_SEEKER_USES_OFF64_T
    ].each do |define|
      text.gsub!(/^#define #{define} 1$/, "/* #undef #{define} */")
    end
    text.gsub!("/* #undef HAVE_SQLITE3_EXPANDED_SQL */", "#define HAVE_SQLITE3_EXPANDED_SQL 1")
    text.gsub!("/* #undef SQLITE_OMIT_LOAD_EXTENSION */", "#define SQLITE_OMIT_LOAD_EXTENSION 1")
    text.gsub!("/* #undef HAVE_FOPENCOOKIE */", "#define HAVE_FOPENCOOKIE 1")
    text.gsub!("/* #undef HAVE_PRCTL */", "#define HAVE_PRCTL 1")
    text.gsub!(/^#define PHP_OS .*$/, '#define PHP_OS "Kandelo"')
    text.gsub!(/^#define PHP_UNAME .*$/, '#define PHP_UNAME "Kandelo wasm32-posix-kernel"')
    File.write(config, text)
    odie "PHP configure did not select GNU libiconv aliases" unless text.include?("#define ICONV_ALIASED_LIBICONV 1")
    odie "PHP configure did not retain musl fopencookie support" unless text.include?("#define HAVE_FOPENCOOKIE 1")
    if text.include?("#define COOKIE_SEEKER_USES_OFF64_T 1")
      odie "PHP configure retained the glibc-only fopencookie callback type"
    end

    build_defs = buildpath/"main/build-defs.h"
    text = build_defs.read
    text.gsub!(/^#define CONFIGURE_COMMAND .*$/, '#define CONFIGURE_COMMAND "Kandelo reproducible Homebrew build"')
    text.gsub!(/^#define PHP_EXTENSION_DIR .*$/, %Q(#define PHP_EXTENSION_DIR       "#{GUEST_EXTENSION_DIR}"))
    File.write(build_defs, text)

    makefile = buildpath/"Makefile"
    File.write(makefile, makefile.read.gsub(/ -MMD -MF \S+ -MT \S+/, ""))

    libtool = buildpath/"libtool"
    text = libtool.read
    shared_mode_markers = text.scan(/^build_libtool_libs=no$/).length
    odie "PHP libtool shared mode markers drifted" if shared_mode_markers < 2
    File.write(libtool, text.gsub(/^build_libtool_libs=no$/, "build_libtool_libs=yes"))
  end

  def build_opcache!(root, output, extra_includes)
    targets = OPCACHE_OBJECTS.map { |name| "ext/opcache/#{name}.lo" }
    system "make", "-j#{ENV.make_jobs}", "EXTRA_CFLAGS=#{extra_includes}", *targets
    objects = OPCACHE_OBJECTS.map { |name| buildpath/"ext/opcache/.libs/#{name}.o" }
    artifact = output/"opcache.so"
    system kandelo_cc(root), "-shared", "-fPIC", "-o", artifact, *objects
    instrumented = output/"opcache.so.instrumented"
    system "#{root}/scripts/run-wasm-fork-instrument.sh",
      artifact, "-o", instrumented, "--entry", "env.fork"
    mv instrumented, artifact
    artifact
  end

  def build_curl!(root, output, extra_includes, libcurl)
    targets = CURL_OBJECTS.map { |name| "ext/curl/#{name}.lo" }
    cflags = "#{extra_includes} -I#{libcurl}/include -DCURL_STATICLIB"
    system "make", "-j#{ENV.make_jobs}", "EXTRA_CFLAGS=#{cflags}", *targets
    objects = CURL_OBJECTS.map { |name| buildpath/"ext/curl/.libs/#{name}.o" }
    artifact = output/"curl.so"
    system kandelo_cc(root), "-shared", "-fPIC", "-o", artifact,
      *objects, libcurl/"lib/libcurl-pic.a"
    artifact
  end

  def build_phar!(root, output, extra_includes)
    targets = PHAR_OBJECTS.map { |name| "ext/phar/#{name}.lo" }
    system "make", "-j#{ENV.make_jobs}", "EXTRA_CFLAGS=#{extra_includes}", *targets
    objects = PHAR_OBJECTS.map { |name| buildpath/"ext/phar/.libs/#{name}.o" }.sort
    artifact = output/"phar.so"
    system kandelo_cc(root), "-shared", "-fPIC", "-o", artifact, *objects
    artifact
  end

  def build_zend_test!(root, output, extra_includes)
    targets = ZEND_TEST_OBJECTS.map { |name| "ext/zend_test/#{name}.lo" }
    system "make", "-j#{ENV.make_jobs}", "EXTRA_CFLAGS=#{extra_includes}", *targets
    objects = (buildpath/"ext/zend_test/.libs").glob("*.o").sort
    odie "zend_test did not produce PIC objects" if objects.empty?
    artifact = output/"zend_test.so"
    system kandelo_cc(root), "-shared", "-fPIC", "-o", artifact, *objects
    artifact
  end

  def build_zip!(root, output, extra_includes, libzip)
    targets = ZIP_OBJECTS.map { |name| "ext/zip/#{name}.lo" }
    cflags = "#{extra_includes} -I#{libzip}/include"
    system "make", "-j#{ENV.make_jobs}", "EXTRA_CFLAGS=#{cflags}", *targets
    objects = ZIP_OBJECTS.map { |name| buildpath/"ext/zip/.libs/#{name}.o" }
    artifact = output/"zip.so"
    system kandelo_cc(root), "-shared", "-fPIC", "-o", artifact,
      *objects, libzip/"lib/libzip.a"
    artifact
  end

  def build_intl!(root, output, extra_includes, dependencies, icu_loader)
    make_fragment = buildpath/"print-intl-objects.mk"
    make_fragment.write <<~MAKE
      .PHONY: print-intl-objects
      print-intl-objects:
      \t@printf '%s\\n' $(shared_objects_intl)
    MAKE
    targets = Utils.safe_popen_read(
      "make", "-s", "--no-print-directory", "-f", "Makefile", "-f", make_fragment, "print-intl-objects"
    ).lines.map(&:strip).reject(&:empty?)
    odie "PHP Makefile did not declare shared_objects_intl" if targets.empty?
    system "make", "-j#{ENV.make_jobs}", "EXTRA_CFLAGS=#{extra_includes}", *targets

    icu = dependencies.fetch("icu")
    loader_object = buildpath/"ext/intl/kandelo_icu_data_loader.o"
    system kandelo_cc(root), "-fPIC", "-O2", "-c", icu_loader,
      "-I#{icu}/include", "-o", loader_object
    objects = targets.map do |target|
      path = buildpath/target
      path.dirname/".libs/#{path.basename(".lo")}.o"
    end
    missing_objects = objects.reject(&:file?)
    unless missing_objects.empty?
      missing_paths = missing_objects.map { |path| path.relative_path_from(buildpath) }
      odie "intl did not produce declared PIC objects: #{missing_paths.join(", ")}"
    end
    libcxx = dependencies.fetch("libcxx")
    artifact = output/"intl.so"
    system kandelo_cc(root), "-shared", "-fPIC", "-Wl,--export=__tls_base", "-o", artifact,
      *objects,
      loader_object,
      icu/"lib/libicui18n.a",
      icu/"lib/libicuio.a",
      icu/"lib/libicuuc.a",
      icu/"lib/libicudata.a",
      libcxx/"lib/libc++-pic.a",
      libcxx/"lib/libc++abi-pic.a"
    artifact
  end

  def validate_php_artifacts!(root, php, php_fpm, side_modules, forbidden_paths)
    root = Pathname(root)
    [php, php_fpm, *side_modules.values].each do |artifact|
      bytes = artifact.binread
      odie "#{artifact.basename} contains legacy Asyncify instrumentation" if bytes.include?("asyncify_".b)
      forbidden_paths.each do |path|
        odie "#{artifact.basename} embeds dependency path #{path}" if bytes.include?(path.to_s)
      end
      if bytes.match?(%r{/(?:private/tmp/|Users/|home/runner/(?:_work|work)/|nix/store/)})
        odie "#{artifact.basename} embeds a host build path"
      end
    end

    expected_abi = File.read(root/"crates/shared/src/lib.rs")[/^pub const ABI_VERSION: u32 = (\d+);$/, 1]
    odie "could not determine Kandelo ABI" if expected_abi.nil?
    odie "PHP requires Kandelo ABI #{MINIMUM_KANDELO_ABI} or newer" if expected_abi.to_i < MINIMUM_KANDELO_ABI

    validator = buildpath/"validate-php-artifacts.mjs"
    validator.write <<~JS
      import { readFileSync } from "node:fs";
      import { extractAbiVersion } from "#{root}/host/src/constants.ts";

      const spec = JSON.parse(readFileSync(process.argv[2], "utf8"));
      const expectedAbi = Number(spec.expectedAbi);
      const forkExports = [
        "wpk_fork_unwind_begin",
        "wpk_fork_unwind_end",
        "wpk_fork_rewind_begin",
        "wpk_fork_rewind_end",
        "wpk_fork_state",
      ];
      function describe(path) {
        const bytes = readFileSync(path);
        return { bytes, module: new WebAssembly.Module(bytes) };
      }
      for (const [label, path] of Object.entries(spec.programs)) {
        const { bytes, module } = describe(path);
        const imports = WebAssembly.Module.imports(module);
        const exports = new Set(WebAssembly.Module.exports(module).map(({ name }) => name));
        const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
        const abi = extractAbiVersion(buffer);
        if (abi !== expectedAbi) throw new Error(label + ": ABI " + abi + " != " + expectedAbi);
        if (!imports.some(({ module, name }) => module === "kernel" && name === "kernel_fork")) {
          throw new Error(label + ": missing kernel.kernel_fork");
        }
        if (!imports.some(({ module, name }) =>
          module === "env" && name === "__wasm_posix_vm_interrupt_after")) {
          throw new Error(label + ": missing ABI " + spec.minimumAbi + " VM interrupt import");
        }
        const missing = forkExports.filter((name) => !exports.has(name));
        if (missing.length) throw new Error(label + ": missing fork exports " + missing.join(", "));
      }
      for (const [label, path] of Object.entries(spec.sideModules)) {
        const { module } = describe(path);
        const imports = WebAssembly.Module.imports(module);
        const exports = new Set(WebAssembly.Module.exports(module).map(({ name }) => name));
        if (!imports.some(({ module, kind }) => module === "env" && kind === "memory")) {
          throw new Error(label + ": missing imported env memory");
        }
        if (!imports.some(({ module, kind }) => module === "env" && kind === "table")) {
          throw new Error(label + ": missing imported env table");
        }
        if (!exports.has("__wasm_apply_data_relocs")) {
          throw new Error(label + ": missing dynamic relocation export");
        }
        if (label === "opcache") {
          if (!imports.some(({ module, name }) => module === "env" && name === "fork")) {
            throw new Error("opcache: missing env.fork replay entry");
          }
          const missing = forkExports.filter((name) => !exports.has(name));
          if (missing.length) throw new Error("opcache: missing fork exports " + missing.join(", "));
        } else if (forkExports.some((name) => exports.has(name))) {
          throw new Error(label + ": unexpected executable fork instrumentation");
        }
      }
    JS
    spec = buildpath/"validate-php-artifacts.json"
    spec.write JSON.generate({
      expectedAbi: expected_abi.to_i,
      minimumAbi:  MINIMUM_KANDELO_ABI,
      programs:    { php: php.to_s, "php-fpm": php_fpm.to_s },
      sideModules: side_modules.transform_values(&:to_s),
    })
    command = [
      "node", "--experimental-wasm-exnref", "--import", "tsx/esm", validator, spec
    ].map { |argument| argument.to_s.shellescape }.join(" ")
    system "bash", "-c", "cd #{root.to_s.shellescape} && #{command}"
  end

  def install_runtime_config!
    config = libexec/"config"
    (config/"php-fpm.d").mkpath
    (config/"php.ini").write <<~INI
      [PHP]
      extension_dir = "#{GUEST_EXTENSION_DIR}"
      expose_php = Off
      variables_order = "GPCS"
    INI
    (config/"php-fpm.conf").write <<~INI
      [global]
      pid = #{GUEST_LOCALSTATEDIR}/run/php-fpm.pid
      error_log = #{GUEST_LOCALSTATEDIR}/log/php-fpm.log
      daemonize = no
      include = #{GUEST_SYSCONFDIR}/php-fpm.d/*.conf
    INI
    (config/"php-fpm.d/www.conf").write <<~INI
      [www]
      listen = #{GUEST_LOCALSTATEDIR}/run/php-fpm.sock
      user = root
      group = root
      pm = static
      pm.max_children = 1
      clear_env = no
      catch_workers_output = yes
      decorate_workers_output = no
    INI
  end

  test do
    root = Pathname(kandelo_require_root!)
    extensions = %w[opcache curl phar zend_test zip intl].to_h do |name|
      [name, lib/"php/extensions/#{name}.so"]
    end
    assert_path_exists bin/"php"
    assert_path_exists sbin/"php-fpm"
    extensions.each_value { |path| assert_path_exists path }
    assert_path_exists lib/"php/icu.dat"
    assert_equal ICU_DATA_BYTES, (lib/"php/icu.dat").size
    assert_equal ICU_DATA_SHA256, Digest::SHA256.file(lib/"php/icu.dat").hexdigest
    assert_path_exists libexec/"config/php-fpm.conf"
    assert_path_exists libexec/"config/php-fpm.d/www.conf"

    guest_files = extensions.to_h do |name, path|
      ["#{GUEST_EXTENSION_DIR}/#{name}.so", path]
    end
    guest_files[GUEST_ICU_DATA] = lib/"php/icu.dat"

    php_program = <<~PHP
      $required = ['Zend OPcache', 'curl', 'Phar', 'zend_test', 'zip', 'intl'];
      foreach ($required as $name) {
          if (!extension_loaded($name)) {
              throw new RuntimeException("missing extension: " . $name);
          }
      }
      $curl = curl_version();
      if (strpos($curl['version'], '8.11.1') !== 0) {
          throw new RuntimeException("wrong libcurl: " . $curl['version']);
      }

      $zipPath = '/tmp/kandelo-php.zip';
      $zip = new ZipArchive();
      if ($zip->open($zipPath, ZipArchive::CREATE | ZipArchive::OVERWRITE) !== true) {
          throw new RuntimeException('zip open failed');
      }
      $zip->addFromString('value.txt', 'zip-ok');
      $zip->close();
      $zip = new ZipArchive();
      $zip->open($zipPath);
      $zipValue = $zip->getFromName('value.txt');
      $zip->close();

      $pharPath = '/tmp/kandelo-php.phar';
      $phar = new Phar($pharPath);
      $phar['value.txt'] = 'phar-ok';
      $pharValue = file_get_contents("phar://$pharPath/value.txt");

      $collator = new Collator('en_US');
      $values = ['z', 'a'];
      $collator->sort($values);
      if ($values !== ['a', 'z']) {
          throw new RuntimeException('intl collation failed');
      }
      $opcache = opcache_get_status(false);
      if (!$opcache || !$opcache['file_cache_only']) {
          throw new RuntimeException('opcache file cache mode is inactive');
      }

      $pid = pcntl_fork();
      if ($pid === 0) {
          $child = new Collator('en_US');
          echo $child->compare('a', 'b') < 0 ? "intl-child-ok\n" : "intl-child-bad\n";
          exit(0);
      }
      if ($pid < 0) {
          throw new RuntimeException('pcntl_fork failed');
      }
      pcntl_waitpid($pid, $status);
      if (!pcntl_wifexited($status) || pcntl_wexitstatus($status) !== 0) {
          throw new RuntimeException('intl child failed');
      }
      echo json_encode([
          'zip' => $zipValue,
          'phar' => $pharValue,
          'intl' => Locale::getDisplayLanguage('fr', 'en'),
          'curl' => $curl['version'],
      ], JSON_THROW_ON_ERROR), "\n";
    PHP
    extension_args = [
      "-n",
      "-d", "zend_extension=#{GUEST_EXTENSION_DIR}/opcache.so",
      "-d", "opcache.enable=1",
      "-d", "opcache.enable_cli=1",
      "-d", "opcache.file_cache=/tmp",
      "-d", "opcache.file_cache_only=1",
      "-d", "phar.readonly=0",
      *%w[curl phar zend_test zip intl].flat_map do |name|
        ["-d", "extension=#{GUEST_EXTENSION_DIR}/#{name}.so"]
      end,
      "-r", php_program
    ]
    node_output = kandelo_run_wasm(
      bin/"php",
      extension_args,
      guest_files:               guest_files,
      expected_fork_descendants: 1,
    )
    assert_includes node_output, "intl-child-ok\n"
    node_result = JSON.parse(node_output.lines.last)
    assert_equal "zip-ok", node_result.fetch("zip")
    assert_equal "phar-ok", node_result.fetch("phar")
    assert_equal "French", node_result.fetch("intl")
    assert_match(/\A8\.11\.1/, node_result.fetch("curl"))

    browser_output = kandelo_run_browser_wasm(
      bin/"php",
      extension_args,
      guest_files: guest_files,
      timeout_ms:  180_000,
    )
    assert_includes browser_output, "intl-child-ok\n"
    assert_equal node_result, JSON.parse(browser_output.lines.last)

    run_fpm_fastcgi_test!(root)
  end

  def run_fpm_fastcgi_test!(root)
    harness_source = testpath/"php-fpm-fastcgi.c"
    harness = testpath/"php-fpm-fastcgi.wasm"
    harness_source.write <<~C
      #include <errno.h>
      #include <signal.h>
      #include <stdint.h>
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <sys/socket.h>
      #include <sys/un.h>
      #include <sys/wait.h>
      #include <unistd.h>

      #define FCGI_BEGIN_REQUEST 1
      #define FCGI_END_REQUEST 3
      #define FCGI_PARAMS 4
      #define FCGI_STDIN 5
      #define FCGI_STDOUT 6
      #define FCGI_STDERR 7
      #define FCGI_RESPONDER 1

      static const char fpm_path[] = "#{GUEST_OPT_PREFIX}/sbin/php-fpm";
      static const char socket_path[] = "#{GUEST_LOCALSTATEDIR}/run/php-fpm.sock";

      static int write_all(int fd, const void *buffer, size_t length) {
        const unsigned char *cursor = buffer;
        while (length > 0) {
          ssize_t written = write(fd, cursor, length);
          if (written < 0 && errno == EINTR) continue;
          if (written <= 0) return -1;
          cursor += written;
          length -= (size_t)written;
        }
        return 0;
      }

      static int read_all(int fd, void *buffer, size_t length) {
        unsigned char *cursor = buffer;
        while (length > 0) {
          ssize_t received = read(fd, cursor, length);
          if (received < 0 && errno == EINTR) continue;
          if (received <= 0) return -1;
          cursor += received;
          length -= (size_t)received;
        }
        return 0;
      }

      static int record(int fd, unsigned char type, const void *content, size_t length) {
        unsigned char header[8] = {
          1, type, 0, 1,
          (unsigned char)(length >> 8), (unsigned char)length,
          0, 0
        };
        if (length > 65535 || write_all(fd, header, sizeof(header)) != 0) return -1;
        return length == 0 || write_all(fd, content, length) == 0 ? 0 : -1;
      }

      static size_t param(unsigned char *output, const char *name, const char *value) {
        size_t name_length = strlen(name);
        size_t value_length = strlen(value);
        if (name_length >= 128 || value_length >= 128) return 0;
        output[0] = (unsigned char)name_length;
        output[1] = (unsigned char)value_length;
        memcpy(output + 2, name, name_length);
        memcpy(output + 2 + name_length, value, value_length);
        return 2 + name_length + value_length;
      }

      int main(void) {
        pid_t master = fork();
        if (master < 0) return 2;
        if (master == 0) {
          char *const argv[] = { (char *)fpm_path, "-F", "-R", NULL };
          execv(fpm_path, argv);
          _exit(127);
        }

        int fd = -1;
        for (int attempt = 0; attempt < 200; attempt++) {
          struct sockaddr_un address;
          memset(&address, 0, sizeof(address));
          address.sun_family = AF_UNIX;
          strcpy(address.sun_path, socket_path);
          fd = socket(AF_UNIX, SOCK_STREAM, 0);
          if (fd >= 0 && connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0) break;
          if (fd >= 0) close(fd);
          fd = -1;
          usleep(25000);
        }
        if (fd < 0) {
          kill(master, SIGQUIT);
          waitpid(master, NULL, 0);
          return 3;
        }

        unsigned char begin[8] = { 0, FCGI_RESPONDER, 0, 0, 0, 0, 0, 0 };
        unsigned char params[2048];
        size_t used = 0;
        used += param(params + used, "GATEWAY_INTERFACE", "CGI/1.1");
        used += param(params + used, "REQUEST_METHOD", "GET");
        used += param(params + used, "REQUEST_URI", "/index.php");
        used += param(params + used, "SCRIPT_FILENAME", "#{GUEST_OPT_PREFIX}/share/php-test/index.php");
        used += param(params + used, "SCRIPT_NAME", "/index.php");
        used += param(params + used, "SERVER_PROTOCOL", "HTTP/1.1");
        used += param(params + used, "SERVER_SOFTWARE", "kandelo-test");
        used += param(params + used, "SERVER_NAME", "localhost");
        used += param(params + used, "SERVER_ADDR", "127.0.0.1");
        used += param(params + used, "REMOTE_ADDR", "127.0.0.1");
        used += param(params + used, "SERVER_PORT", "80");
        if (record(fd, FCGI_BEGIN_REQUEST, begin, sizeof(begin)) != 0 ||
            record(fd, FCGI_PARAMS, params, used) != 0 ||
            record(fd, FCGI_PARAMS, NULL, 0) != 0 ||
            record(fd, FCGI_STDIN, NULL, 0) != 0) {
          return 4;
        }

        char response[8192];
        size_t response_length = 0;
        int ended = 0;
        while (!ended) {
          unsigned char header[8];
          if (read_all(fd, header, sizeof(header)) != 0) return 5;
          size_t content_length = ((size_t)header[4] << 8) | header[5];
          size_t padding_length = header[6];
          unsigned char *content = malloc(content_length + padding_length);
          if (content == NULL) return 6;
          if (read_all(fd, content, content_length + padding_length) != 0) return 7;
          if (header[1] == FCGI_STDOUT && response_length + content_length < sizeof(response)) {
            memcpy(response + response_length, content, content_length);
            response_length += content_length;
          } else if (header[1] == FCGI_STDERR && content_length > 0) {
            write_all(STDERR_FILENO, content, content_length);
          } else if (header[1] == FCGI_END_REQUEST) {
            ended = 1;
          }
          free(content);
        }
        close(fd);
        response[response_length] = '\\0';
        int ok = strstr(response, "fpm-ok") != NULL;
        kill(master, SIGQUIT);
        int status = 0;
        waitpid(master, &status, 0);
        if (!ok) {
          fprintf(stderr, "FastCGI response did not contain fpm-ok: %s\\n", response);
          return 8;
        }
        if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
          fprintf(stderr, "PHP-FPM master did not exit successfully: %d\\n", status);
          return 9;
        }
        puts("php-fpm-fastcgi-ok");
        return 0;
      }
    C
    kandelo_activate_sdk!
    kandelo_activate_sysroot!
    system kandelo_cc(root), harness_source, "-O2", "-o", harness
    kandelo_fork_instrument harness
    kandelo_validate_wasm_artifact(harness, fork: :required)

    script = testpath/"index.php"
    script.write("<?php echo 'fpm-ok';\n")
    placeholder = testpath/"placeholder"
    placeholder.write("")
    passwd = testpath/"passwd"
    passwd.write("root:x:0:0:root:/root:/bin/sh\n")
    group = testpath/"group"
    group.write("root:x:0:\n")
    guest_files = {
      "/etc/passwd"                                  => passwd,
      "/etc/group"                                   => group,
      "#{GUEST_SYSCONFDIR}/php-fpm.conf"             => libexec/"config/php-fpm.conf",
      "#{GUEST_SYSCONFDIR}/php-fpm.d/www.conf"       => libexec/"config/php-fpm.d/www.conf",
      "#{GUEST_PHP_CONFIG_DIR}/php.ini"              => libexec/"config/php.ini",
      "#{GUEST_LOCALSTATEDIR}/run/.keep"             => placeholder,
      "#{GUEST_LOCALSTATEDIR}/log/.keep"             => placeholder,
      "#{GUEST_OPT_PREFIX}/share/php-test/index.php" => script,
    }
    output = kandelo_run_wasm(
      harness,
      [],
      exec_programs:                     { "#{GUEST_OPT_PREFIX}/sbin/php-fpm" => sbin/"php-fpm" },
      guest_files:                       guest_files,
      # PHP 8.3.15's fpm_pctl_action_next() escalates a live worker from
      # SIGQUIT to SIGTERM while the FPM master completes a finishing shutdown.
      expected_fork_descendant_statuses: [0, 143],
    )
    assert_equal "php-fpm-fastcgi-ok\n", output
  end
end
