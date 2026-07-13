require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s
require "digest"

class Libcurl < Formula
  include KandeloFormulaSupport

  desc "Multiprotocol file transfer library for Kandelo"
  homepage "https://curl.se/"
  url "https://curl.se/download/curl-8.11.1.tar.xz"
  sha256 "c7ca7db48b0909743eaef34250da02c19bc61d4f1dcedd6603f109409536ab56"
  license "curl"
  revision 1

  depends_on "pkgconf" => [:build, :test]
  depends_on "binaryen" => :test
  depends_on "wabt" => :test
  depends_on "automattic/kandelo-homebrew/openssl"
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "lib/libcurl.a"
  skip_clean "lib/libcurl-pic.a"

  def install
    kandelo_require_arch!("wasm32", "wasm64")
    openssl = formula_opt_prefix("automattic/kandelo-homebrew/openssl")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")

    kandelo_wasm_build do |root|
      pic_source = buildpath.parent/"#{buildpath.basename}-pic"
      odie "stale PIC libcurl source tree exists: #{pic_source}" if pic_source.exist?
      cp_r buildpath, pic_source
      prefix_maps = lambda do |source_root|
        {
          source_root => "/usr/src/libcurl",
          root        => "/usr/src/kandelo",
          openssl     => "/usr/src/kandelo-deps/openssl",
          zlib        => "/usr/src/kandelo-deps/zlib",
        }.flat_map do |from, to|
          [Pathname(from), Pathname(from).realpath].uniq.flat_map do |source|
            [
              "-ffile-prefix-map=#{source}=#{to}",
              "-fdebug-prefix-map=#{source}=#{to}",
              "-fmacro-prefix-map=#{source}=#{to}",
            ]
          end
        end
      end
      ENV["CPPFLAGS"] = "-I#{openssl}/include -I#{zlib}/include"
      ENV["LDFLAGS"] = "-L#{openssl}/lib -L#{zlib}/lib"
      ENV["LIBS"] = "-ldl -pthread"
      ENV["OPENSSL_CFLAGS"] = "-I#{openssl}/include"
      ENV["OPENSSL_LIBS"] = "-L#{openssl}/lib -lssl -lcrypto -ldl -pthread"
      ENV["PKG_CONFIG_LIBDIR"] = "#{openssl}/lib/pkgconfig:#{zlib}/lib/pkgconfig"
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")

      # These Autoconf probes call fully prototyped APIs with no arguments.
      # That creates invalid Wasm call signatures even though correctly typed
      # consumers link successfully against the declared dependency versions.
      ENV["ac_cv_lib_z_gzread"] = "yes"
      ENV["ac_cv_func_SSL_set0_wbio"] = "yes"

      configure_args = [
        *kandelo_std_configure_args,
        "--disable-shared",
        "--enable-static",
        "--with-openssl=#{openssl}",
        "--with-zlib=#{zlib}",
        "--with-ca-bundle=/etc/ssl/certs/ca-certificates.crt",
        "--without-ca-path",
        "--enable-threaded-resolver",
        "--enable-unix-sockets",
        "--without-brotli",
        "--without-zstd",
        "--without-nghttp2",
        "--without-libidn2",
        "--without-libssh2",
        "--without-librtmp",
        "--without-libpsl",
        "--without-libgsasl",
        "--disable-ldap",
        "--disable-ldaps",
        "--disable-manual",
        "--disable-docs",
      ]
      ENV["WASM_POSIX_TARGET_ARCH"] = kandelo_arch
      # Keep the main archive on curl's exact in-tree default CFLAGS contract;
      # changing user CFLAGS changes static member selection for existing
      # main-module consumers. The cloned tree owns PIC code generation.
      ENV["CFLAGS"] = ""
      system kandelo_configure(root), *configure_args
      cd pic_source do
        ENV["CFLAGS"] = ["-O2", "-fPIC", *prefix_maps.call(pic_source)].join(" ")
        system kandelo_configure(root), *configure_args
      end

      # The builds use distinct relocation/CFLAGS policies. Fail closed if
      # configure nevertheless selected a different target feature set.
      normal_config = (buildpath/"lib/curl_config.h").binread
      pic_config = (pic_source/"lib/curl_config.h").binread
      odie "normal and PIC libcurl target facts differ" if normal_config != pic_config
      %w[
        HAVE_LIBZ
        USE_OPENSSL
        USE_UNIX_SOCKETS
      ].each do |fact|
        odie "libcurl target fact missing: #{fact}" unless normal_config.include?("#define #{fact} 1")
      end
      ca_fact = '#define CURL_CA_BUNDLE "/etc/ssl/certs/ca-certificates.crt"'
      odie "libcurl CA bundle target fact drifted" unless normal_config.include?(ca_fact)
      expected_pointer_size = (kandelo_arch == "wasm64") ? 8 : 4
      %w[SIZEOF_LONG SIZEOF_SIZE_T].each do |fact|
        expected = "#define #{fact} #{expected_pointer_size}"
        odie "libcurl #{fact} target width drifted" unless normal_config.include?(expected)
      end

      ENV["CFLAGS"] = ""
      system "make"
      system "make", "install"
      cd pic_source do
        ENV["CFLAGS"] = ["-O2", "-fPIC", *prefix_maps.call(pic_source)].join(" ")
        system "make"
        pic_archive = pic_source/"lib/.libs/libcurl.a"
        odie "PIC libcurl archive was not built" unless pic_archive.file?

        lib.install pic_archive => "libcurl-pic.a"
      end
      rm_r pic_source
    end

    rm_r bin if bin.exist?
    rm_r share if share.exist?
    rm lib/"libcurl.la" if (lib/"libcurl.la").exist?
  end

  test do
    assert_path_exists lib/"libcurl.a"
    assert_path_exists lib/"libcurl-pic.a"
    assert_path_exists include/"curl/curl.h"
    assert_path_exists lib/"pkgconfig/libcurl.pc"

    root = kandelo_require_root!
    openssl = formula_opt_prefix("automattic/kandelo-homebrew/openssl")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    forbidden_paths = [
      root,
      prefix,
      openssl,
      openssl.realpath,
      zlib,
      zlib.realpath,
      "/opt/homebrew/Cellar/",
      "/usr/local/Cellar/",
      "/private/tmp/",
      "/private/var/",
      "/nix/store/",
    ].map(&:to_s).uniq
    %w[libcurl.a libcurl-pic.a].each do |archive_name|
      archive = (lib/archive_name).binread
      forbidden_paths.each do |forbidden|
        refute_includes archive, forbidden
      end
      refute_match %r{/Users/[^/]+/}, archive
    end
    refute_equal Digest::SHA256.file(lib/"libcurl.a").hexdigest,
      Digest::SHA256.file(lib/"libcurl-pic.a").hexdigest
    refute_includes (lib/"pkgconfig/libcurl.pc").read, "libcurl-pic"
    normal_members = Utils.safe_popen_read(kandelo_ar(root), "t", lib/"libcurl.a").lines.map(&:strip)
    pic_members = Utils.safe_popen_read(kandelo_ar(root), "t", lib/"libcurl-pic.a").lines.map(&:strip)
    assert_operator normal_members.length, :>, 100
    assert_equal normal_members, pic_members

    source = testpath/"libcurl-smoke.c"
    wasm = testpath/"libcurl-smoke.wasm"
    source.write <<~C
      #include <curl/curl.h>
      #include <pthread.h>
      #include <poll.h>
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <sys/socket.h>
      #include <sys/un.h>
      #include <unistd.h>

      struct transfer_result {
        CURLcode code;
        long status;
        char body[8192];
        char ca_info[256];
        size_t length;
      };

      static size_t collect_body(char *data, size_t size, size_t count, void *opaque) {
        struct transfer_result *result = opaque;
        size_t bytes = size * count;
        size_t available = sizeof(result->body) - result->length - 1;
        size_t copied = bytes < available ? bytes : available;

        memcpy(result->body + result->length, data, copied);
        result->length += copied;
        result->body[result->length] = '\\0';
        return bytes;
      }

      struct unix_server {
        int fd;
        int status;
      };

      static int write_all(int fd, const void *data, size_t length) {
        const char *cursor = data;
        while (length > 0) {
          ssize_t written = write(fd, cursor, length);
          if (written <= 0) return -1;
          cursor += written;
          length -= (size_t)written;
        }
        return 0;
      }

      static void *serve_unix_http(void *opaque) {
        static const char headers[] =
          "HTTP/1.1 200 OK\\r\\n"
          "Content-Encoding: gzip\\r\\n"
          "Content-Length: 38\\r\\n"
          "Connection: close\\r\\n\\r\\n";
        static const unsigned char body[] = {
          0x1f, 0x8b, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03,
          0xcb, 0xc9, 0x4c, 0x4a, 0x2e, 0x2d, 0xca, 0xd1, 0x2d, 0xcd,
          0xcb, 0xac, 0xd0, 0xad, 0xca, 0xc9, 0x4c, 0xe2, 0x02, 0x00,
          0x9b, 0x5a, 0xb6, 0xaa, 0x12, 0x00, 0x00, 0x00,
        };
        struct unix_server *server = opaque;
        struct pollfd ready = { .fd = server->fd, .events = POLLIN };
        int client = -1;

        server->status = -1;
        if (poll(&ready, 1, 10000) == 1 && (ready.revents & POLLIN) != 0) {
          client = accept(server->fd, NULL, NULL);
        }
        if (client >= 0 &&
            write_all(client, headers, sizeof(headers) - 1) == 0 &&
            write_all(client, body, sizeof(body)) == 0) {
          server->status = 0;
        }
        if (client >= 0) close(client);
        close(server->fd);
        server->fd = -1;
        return NULL;
      }

      static void wake_unix_server(const char *socket_path) {
        struct sockaddr_un address = { 0 };
        char buffer[256];
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);

        if (fd < 0) return;
        address.sun_family = AF_UNIX;
        snprintf(address.sun_path, sizeof(address.sun_path), "%s", socket_path);
        if (connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0) {
          while (read(fd, buffer, sizeof(buffer)) > 0) {}
        }
        close(fd);
      }

      static int perform_unix_zlib(void) {
        static const char socket_path[] = "/tmp/libcurl-test.sock";
        struct sockaddr_un address = { 0 };
        struct unix_server server = { .fd = -1, .status = -1 };
        struct transfer_result result = { 0 };
        pthread_t thread;
        CURL *curl = NULL;
        int thread_started = 0;
        int status = 1;

        unlink(socket_path);
        server.fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (server.fd < 0) goto done;
        address.sun_family = AF_UNIX;
        snprintf(address.sun_path, sizeof(address.sun_path), "%s", socket_path);
        if (bind(server.fd, (struct sockaddr *)&address, sizeof(address)) != 0) goto done;
        if (listen(server.fd, 1) != 0) goto done;
        curl = curl_easy_init();
        if (curl == NULL) goto done;
        if (curl_easy_setopt(curl, CURLOPT_URL, "http://localhost/") != CURLE_OK ||
            curl_easy_setopt(curl, CURLOPT_UNIX_SOCKET_PATH, socket_path) != CURLE_OK ||
            curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "") != CURLE_OK ||
            curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, collect_body) != CURLE_OK ||
            curl_easy_setopt(curl, CURLOPT_WRITEDATA, &result) != CURLE_OK ||
            curl_easy_setopt(curl, CURLOPT_TIMEOUT, 10L) != CURLE_OK) goto done;
        if (pthread_create(&thread, NULL, serve_unix_http, &server) != 0) goto done;
        thread_started = 1;
        result.code = curl_easy_perform(curl);

      done:
        if (thread_started) {
          wake_unix_server(socket_path);
          if (pthread_join(thread, NULL) != 0) {
            _exit(1);
          }
        }
        if (result.code == CURLE_OK && server.status == 0 &&
            strcmp(result.body, "libcurl-unix-zlib\\n") == 0) {
          status = 0;
        }
        if (curl != NULL) curl_easy_cleanup(curl);
        if (server.fd >= 0) close(server.fd);
        unlink(socket_path);
        return status;
      }

      static int supports_protocol(const curl_version_info_data *info, const char *wanted) {
        const char *const *protocol = info->protocols;
        while (protocol != NULL && *protocol != NULL) {
          if (strcmp(*protocol, wanted) == 0) return 1;
          protocol++;
        }
        return 0;
      }

      static void *perform_https(void *opaque) {
        struct transfer_result *result = opaque;
        const char *ca_bundle = getenv("CURL_CA_BUNDLE");
        char *ca_info = NULL;
        CURL *curl = curl_easy_init();

        if (curl == NULL) {
          result->code = CURLE_FAILED_INIT;
          return NULL;
        }

        curl_easy_setopt(curl, CURLOPT_URL, "https://example.com/");
        curl_easy_setopt(curl, CURLOPT_HTTP_VERSION, CURL_HTTP_VERSION_1_1);
        curl_easy_setopt(curl, CURLOPT_SSLVERSION,
          CURL_SSLVERSION_TLSv1_2 | CURL_SSLVERSION_MAX_TLSv1_2);
        if (ca_bundle != NULL) curl_easy_setopt(curl, CURLOPT_CAINFO, ca_bundle);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, collect_body);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, result);
        curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT, 30L);
        if (curl_easy_getinfo(curl, CURLINFO_CAINFO, &ca_info) != CURLE_OK || ca_info == NULL) {
          result->code = CURLE_SSL_CACERT_BADFILE;
          curl_easy_cleanup(curl);
          return NULL;
        }
        snprintf(result->ca_info, sizeof(result->ca_info), "%s", ca_info);
        result->code = curl_easy_perform(curl);
        if (result->code == CURLE_OK) {
          curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &result->status);
        }
        curl_easy_cleanup(curl);
        return NULL;
      }

      static void *create_handle(void *opaque) {
        int *result = opaque;
        CURL *curl = curl_easy_init();

        *result = curl == NULL;
        curl_easy_cleanup(curl);
        return NULL;
      }

      int main(void) {
        const curl_version_info_data *info;
        struct transfer_result result = { 0 };
        pthread_t thread;
        int thread_result = -1;
        int required_features = CURL_VERSION_SSL | CURL_VERSION_LIBZ |
          CURL_VERSION_ASYNCHDNS | CURL_VERSION_UNIX_SOCKETS | CURL_VERSION_THREADSAFE;

        if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) return 1;
        info = curl_version_info(CURLVERSION_NOW);
        if (info == NULL || (info->features & required_features) != required_features) return 2;
        if (!supports_protocol(info, "https") || !supports_protocol(info, "smtp") ||
            !supports_protocol(info, "file")) return 3;
        if (perform_unix_zlib() != 0) return 4;
        if (pthread_create(&thread, NULL, create_handle, &thread_result) != 0) return 5;
        if (pthread_join(thread, NULL) != 0 || thread_result != 0) return 6;
        perform_https(&result);
        curl_global_cleanup();

        if (result.code != CURLE_OK) {
          fprintf(stderr, "libcurl transfer failed: %s\\n", curl_easy_strerror(result.code));
          return 7;
        }
        if (result.status != 200 || strstr(result.body, "Example Domain") == NULL) return 8;
        if (strcmp(result.ca_info, "/etc/ssl/certs/ca-certificates.crt") != 0) return 9;
        printf("libcurl-ok http=%ld ca=%s\\n", result.status, result.ca_info);
        return 0;
      }
    C

    kandelo_wasm_build do
      ENV["PKG_CONFIG_LIBDIR"] = [
        lib/"pkgconfig",
        openssl/"lib/pkgconfig",
        zlib/"lib/pkgconfig",
      ].join(":")
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")
      pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
      flags = shell_output("#{pkgconf} --static --cflags --libs libcurl").split
      %w[-lcurl -lssl -lcrypto -lz -ldl -pthread].each do |flag|
        assert_includes flags, flag
      end
      system kandelo_cc, source, *flags, "-o", wasm
    end
    ca_bundle = Pathname(root)/"images/rootfs/etc/ssl/cert.pem"
    assert_path_exists ca_bundle
    guest_ca_bundle = "/etc/ssl/certs/ca-certificates.crt"
    assert_equal "libcurl-ok http=200 ca=#{guest_ca_bundle}\n",
      kandelo_run_wasm(
        wasm, [], network: true, guest_files: { guest_ca_bundle => ca_bundle }
      )

    side_source = testpath/"libcurl-pic-side.c"
    side_module = testpath/"libcurl-pic-side.so"
    loader_source = testpath/"libcurl-pic-loader.c"
    loader = testpath/"libcurl-pic-loader.wasm"
    side_source.write <<~C
      #include <curl/curl.h>

      __attribute__((visibility("default")))
      const char *kandelo_libcurl_pic_version(void) {
        return curl_version();
      }
    C
    loader_source.write <<~C
      #include <curl/curl.h>
      #include <dlfcn.h>
      #include <stdio.h>
      #include <string.h>

      typedef const char *(*version_fn)(void);

      int main(int argc, char **argv) {
        const curl_version_info_data *main_info;
        version_fn side_version;
        void *module;
        const char *value;

        if (argc != 2 || curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) return 1;
        main_info = curl_version_info(CURLVERSION_NOW);
        if (main_info == NULL || main_info->version == NULL) return 2;
        module = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
        if (module == NULL) {
          fprintf(stderr, "dlopen: %s\\n", dlerror());
          return 3;
        }
        side_version = (version_fn)dlsym(module, "kandelo_libcurl_pic_version");
        if (side_version == NULL || (value = side_version()) == NULL) return 4;
        if (strstr(value, "libcurl/8.11.1") == NULL) return 5;
        printf("libcurl-pic-ok %s\\n", value);
        curl_global_cleanup();
        return dlclose(module) == 0 ? 0 : 6;
      }
    C

    kandelo_wasm_build do |sdk_root|
      pic_whole_archive = [
        "-Wl,--whole-archive", lib/"libcurl-pic.a", "-Wl,--no-whole-archive"
      ]
      normal_whole_archive = [
        "-Wl,--whole-archive", lib/"libcurl.a", "-Wl,--no-whole-archive"
      ]
      system kandelo_cc(sdk_root), side_source, "-O2", "-fPIC", "-shared", "-I#{include}",
        *pic_whole_archive, "-o", side_module

      nonpic_module = testpath/"libcurl-nonpic-negative.so"
      nonpic_command = [
        kandelo_cc(sdk_root), side_source, "-O2", "-fPIC", "-shared", "-I#{include}",
        *normal_whole_archive, "-o", nonpic_module
      ].shelljoin
      nonpic_output = shell_output("#{nonpic_command} 2>&1", 1)
      assert_match(/relocation R_WASM_.*recompile with -fPIC/m, nonpic_output)
      refute_path_exists nonpic_module

      system kandelo_cc(sdk_root), loader_source, "-O2", "-I#{include}", "-Wl,--export-all",
        *normal_whole_archive,
        openssl/"lib/libssl.a", openssl/"lib/libcrypto.a", zlib/"lib/libz.a",
        "-ldl", "-pthread", "-o", loader
    end

    side_info = Utils.safe_popen_read("wasm-objdump", "-x", side_module)
    assert_match(/dylink\.0/, side_info)
    assert_match(/memory.*<- env\.memory/, side_info)
    refute_match(/<- env\.(?:Curl_|curl_|curlx_)/, side_info)

    # Wasm64 side-module paths and lengths cross the JS import boundary as
    # BigInts. The host's current __wasm_dlopen adapter still constructs a
    # Uint8Array with those values directly, so runtime dlopen coverage remains
    # wasm32-only until that platform gap is fixed. The build/link checks above
    # still cover every member of both wasm64 archives.
    if kandelo_arch == "wasm32"
      expected_prefix = "libcurl-pic-ok libcurl/8.11.1"
      node_output = kandelo_run_wasm(loader, [side_module])
      assert_match(/^#{Regexp.escape(expected_prefix)}/, node_output)
      guest_side = "/usr/lib/libcurl-pic-side.so"
      browser_output = kandelo_run_browser_wasm(
        loader, [guest_side], guest_files: { guest_side => side_module }
      )
      assert_match(/^#{Regexp.escape(expected_prefix)}/, browser_output)
    end
  end
end
