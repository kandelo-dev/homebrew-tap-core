require_relative "../Kandelo/formula_support/kandelo_formula_support"

class Libcurl < Formula
  include KandeloFormulaSupport

  desc "Multiprotocol file transfer library for Kandelo"
  homepage "https://curl.se/"
  url "https://curl.se/download/curl-8.11.1.tar.xz"
  sha256 "c7ca7db48b0909743eaef34250da02c19bc61d4f1dcedd6603f109409536ab56"
  license "curl"

  depends_on "pkgconf" => [:build, :test]
  depends_on "automattic/kandelo-homebrew/openssl"
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "lib/libcurl.a"

  def install
    kandelo_require_arch!("wasm32", "wasm64")
    openssl = formula_opt_prefix("automattic/kandelo-homebrew/openssl")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")

    kandelo_wasm_build do
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

      system kandelo_configure, *kandelo_std_configure_args,
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
        "--disable-docs"
      system "make"
      system "make", "install"
    end

    rm_r bin if bin.exist?
    rm_r share if share.exist?
    rm lib/"libcurl.la" if (lib/"libcurl.la").exist?
  end

  test do
    assert_path_exists lib/"libcurl.a"
    assert_path_exists include/"curl/curl.h"
    assert_path_exists lib/"pkgconfig/libcurl.pc"

    openssl = formula_opt_prefix("automattic/kandelo-homebrew/openssl")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
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
    assert_equal "libcurl-ok http=200 ca=/etc/ssl/certs/ca-certificates.crt\n",
      kandelo_run_wasm(wasm, [], network: true)
  end
end
