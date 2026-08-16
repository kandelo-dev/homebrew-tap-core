require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Nginx < Formula
  include KandeloFormulaSupport

  GUEST_HOMEBREW_PREFIX =
    KandeloFormulaSupport::KANDELO_GUEST_HOMEBREW_PREFIX
  GUEST_OPT_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/nginx".freeze
  GUEST_PCRE2_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/pcre2".freeze
  GUEST_ZLIB_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/zlib".freeze

  desc "HTTP and reverse proxy server for Kandelo"
  homepage "https://nginx.org/"
  url "https://nginx.org/download/nginx-1.30.3.tar.gz"
  sha256 "e5823dc6f45610993def93ebf6cfce68264af4958c77e874b7d20f3709001b8f"
  license "BSD-2-Clause"

  # Exact staged builds hydrate a runtime-only local dependency map.
  depends_on "kandelo-dev/tap-core/dash"
  depends_on KandeloFormulaSupport::BinaryenRequirement => [:build, :test]
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  depends_on "kandelo-dev/tap-core/pcre2"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/nginx"

  def install
    kandelo_require_arch!("wasm32")
    dash = formula_opt_bin("kandelo-dev/tap-core/dash")/"dash"
    pcre2 = formula_opt_prefix("kandelo-dev/tap-core/pcre2")
    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")

    kandelo_wasm_build do |root|
      # nginx's supported --crossbuild mode still executes target feature
      # probes directly. Route those probes through Kandelo so configure sees
      # real target behavior instead of host behavior or guessed answers.
      inreplace "configure", "LC_ALL=C\n", <<~SH
        LC_ALL=C

        ngx_run_test() {
            if test -n "$NGX_TEST_RUNNER"; then
                "$NGX_TEST_RUNNER" "$NGX_AUTOTEST"
            else
                "$NGX_AUTOTEST"
            fi
        }
      SH
      inreplace "auto/feature" do |s|
        s.gsub! "/bin/sh -c $NGX_AUTOTEST", "ngx_run_test"
        s.gsub! "`$NGX_AUTOTEST`", "`ngx_run_test`"
      end
      inreplace "auto/types/sizeof", "`$NGX_AUTOTEST`", "`ngx_run_test`"
      inreplace "auto/endianness", "if $NGX_AUTOTEST", "if ngx_run_test"

      # Compiled host output shadows TypeScript source under tsx. Configure
      # probes must exercise the exact checkout that owns this SDK and ABI.
      rm_r Pathname(root)/"host/dist", force: true
      target_runner = buildpath/"kandelo-nginx-configure-runner"
      # WHY: Formula#path identifies the exact reviewed Formula source. Its
      # sibling support runtime is already sealed into the source closure, so
      # resolving from that identity avoids a second mutable Tap lookup.
      configure_runner =
        Pathname(path).realpath.parent.parent/"Kandelo/formula_support/run-network-wasm.ts"
      node = ENV.fetch("HOMEBREW_KANDELO_NODE", "node")
      node = "node" if node.empty?
      runner_environment = kandelo_node_runner_environment
      runner_exec_programs = JSON.generate("/bin/sh" => dash.to_s)
      runner_guest_env = JSON.generate("PATH" => "/bin")
      runner_contract =
        "KANDELO_FORMULA_EXEC_PROGRAMS_JSON=#{runner_exec_programs.shellescape} " \
        "KANDELO_FORMULA_GUEST_ENV_JSON=#{runner_guest_env.shellescape} "
      runner_invocation = "#{runner_environment}#{runner_contract}exec #{node.shellescape}"
      target_runner.write <<~SH
        #!/bin/sh
        set -eu
        case "$1" in
          /*) program="$1" ;;
          *) program="$PWD/$1" ;;
        esac
        cd #{root.to_s.shellescape}
        # WHY: configure probes are individual target programs, not the full
        # example shell. The isolated Formula runner stages an explicitly
        # declared /bin/sh without resolving Kandelo's unrelated demo catalog
        # or emitting dev-shell setup text into output-valued probes.
        #{runner_invocation} \
          --experimental-wasm-exnref --import tsx/esm \
          #{configure_runner.to_s.shellescape} \
          #{root.to_s.shellescape} "$program" </dev/null
      SH
      chmod 0755, target_runner
      ENV["NGX_TEST_RUNNER"] = target_runner

      stable_source = "/usr/src/nginx-#{version}"
      path_maps = {
        buildpath.to_s       => stable_source,
        root.to_s            => "/usr/src/kandelo",
        pcre2.to_s           => GUEST_PCRE2_PREFIX,
        zlib.to_s            => GUEST_ZLIB_PREFIX,
        prefix.to_s          => GUEST_OPT_PREFIX,
        HOMEBREW_PREFIX.to_s => GUEST_HOMEBREW_PREFIX,
        "/nix/store"         => "/usr/src/toolchain",
      }
      prefix_map_flags = path_maps.flat_map do |source, destination|
        [
          "-ffile-prefix-map=#{source}=#{destination}",
          "-fdebug-prefix-map=#{source}=#{destination}",
          "-fmacro-prefix-map=#{source}=#{destination}",
        ]
      end
      include_flags = [
        "-O2",
        "-gline-tables-only",
        "-fdebug-compilation-dir=#{stable_source}",
        "-Wno-sign-compare",
        "-I#{pcre2}/include",
        "-I#{zlib}/include",
        *prefix_map_flags,
      ]
      link_flags = ["-L#{pcre2}/lib", "-L#{zlib}/lib"]

      system "./configure",
        "--crossbuild=Kandelo:wasm32",
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--sbin-path=#{GUEST_OPT_PREFIX}/bin/nginx",
        "--conf-path=#{GUEST_OPT_PREFIX}/conf/nginx.conf",
        "--pid-path=/tmp/nginx.pid",
        "--lock-path=/tmp/nginx.lock",
        "--http-log-path=/dev/stdout",
        "--error-log-path=/dev/stderr",
        "--http-client-body-temp-path=/tmp/nginx_client_body",
        "--http-proxy-temp-path=/tmp/nginx_proxy",
        "--http-fastcgi-temp-path=/tmp/nginx_fastcgi",
        "--http-uwsgi-temp-path=/tmp/nginx_uwsgi",
        "--http-scgi-temp-path=/tmp/nginx_scgi",
        "--user=nobody",
        "--group=nobody",
        "--with-poll_module",
        "--without-select_module",
        "--with-http_stub_status_module",
        "--with-cc=#{kandelo_cc(root)}",
        "--with-cc-opt=#{include_flags.join(" ")}",
        "--with-ld-opt=#{link_flags.join(" ")}"

      # nginx -V deliberately exposes configure arguments. Retain the target
      # capability contract without publishing native Cellar or build paths.
      inreplace "objs/ngx_auto_config.h", /^#define NGX_CONFIGURE .*$/,
        '#define NGX_CONFIGURE " --crossbuild=Kandelo:wasm32 --with-poll_module ' \
        '--without-select_module --with-http_stub_status_module"'

      system "make", "-j#{ENV.make_jobs}"

      optimized = buildpath/"objs/nginx.optimized"
      system "wasm-opt", "-O2", "objs/nginx", "-o", optimized
      kandelo_fork_instrument(optimized)
      kandelo_validate_wasm_artifact(
        optimized,
        fork:            :required,
        forbidden_paths: [pcre2, zlib],
      )

      bin.install optimized => "nginx"
      chmod 0755, bin/"nginx"
      prefix.install "conf", "html"
      man8.install "objs/nginx.8"
      pkgshare.install "LICENSE"
    end
  end

  test do
    artifact = bin/"nginx"
    pcre2 = formula_opt_prefix("kandelo-dev/tap-core/pcre2")
    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")
    kandelo_validate_wasm_artifact(
      artifact,
      fork:            :required,
      forbidden_paths: [pcre2, zlib],
    )

    assert_path_exists prefix/"conf/mime.types"
    assert_path_exists prefix/"conf/fastcgi_params"
    assert_path_exists man8/"nginx.8"
    assert_path_exists pkgshare/"LICENSE"
    assert_path_exists prefix/"html/index.html"

    version_output = kandelo_run_wasm(artifact, ["-V"], merge_stderr: true)
    assert_match "nginx version: nginx/1.30.3", version_output
    assert_match "--crossbuild=Kandelo:wasm32", version_output

    browser_version = kandelo_run_browser_wasm(
      artifact,
      ["-V"],
      allow_stderr:       true,
      guest_program_path: "#{GUEST_OPT_PREFIX}/bin/nginx",
      merge_stderr:       true,
    )
    assert_match "nginx version: nginx/1.30.3", browser_version

    (testpath/"html/new").mkpath
    [testpath, testpath/"html", testpath/"html/new"].each { |path| chmod 0755, path }
    body = "nginx rewrite and gzip through Kandelo\n" * 8
    (testpath/"html/new/message.txt").write body
    guest_testpath = "/tmp/kandelo-nginx-test"
    (testpath/"nginx.conf").write <<~EOS
      daemon off;
      master_process on;
      worker_processes 2;
      pid /tmp/kandelo-nginx.pid;
      error_log /dev/stderr notice;
      events {
        use poll;
        worker_connections 64;
      }
      http {
        include #{GUEST_OPT_PREFIX}/conf/mime.types;
        access_log off;
        gzip on;
        gzip_min_length 1;
        gzip_types text/plain;
        server {
          listen 18080;
          server_name localhost;
          root #{guest_testpath}/html;
          location /old/ {
            rewrite ^/old/(.*)$ /new/$1 last;
          }
        }
      }
    EOS

    # A successful response can only come from a forked worker when
    # master_process is enabled. This covers the real master/worker lifecycle,
    # the in-kernel TCP bridge, PCRE2 rewrites, gzip, and an honest 404.
    responses = kandelo_run_http_service(
      artifact,
      ["-p", "#{guest_testpath}/", "-c", "#{guest_testpath}/nginx.conf"],
      port:     18080,
      mounts:   {
        guest_testpath   => testpath.to_s,
        GUEST_OPT_PREFIX => prefix.to_s,
      },
      env:      { "KERNEL_CWD" => guest_testpath },
      uid:      1000,
      gid:      1000,
      requests: [
        { path: "/old/message.txt", headers: { "Host" => "localhost" } },
        {
          path:    "/new/message.txt",
          headers: { "Host" => "localhost", "Accept-Encoding" => "gzip" },
        },
        { path: "/missing", headers: { "Host" => "localhost" } },
      ],
      timeout:  60,
    )

    assert_equal 200, responses[0]["status"]
    assert_equal body, responses[0]["text"]
    assert_equal 200, responses[1]["status"]
    gzip_headers = responses[1]["headers"].transform_keys(&:downcase)
    assert_equal "gzip", gzip_headers["content-encoding"]
    assert responses[1]["body"].start_with?("H4sI")
    assert_equal 404, responses[2]["status"]

    browser_probe_source = testpath/"nginx-browser-probe.c"
    browser_probe = testpath/"nginx-browser-probe.wasm"
    browser_probe_source.write <<~'C'
      #define _POSIX_C_SOURCE 200809L

      #include <arpa/inet.h>
      #include <netinet/in.h>
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <sys/socket.h>
      #include <time.h>
      #include <unistd.h>

      static int request(
          int port,
          const char *path,
          const char *extra_header,
          int expected_status,
          const char *expected_text,
          int expect_gzip) {
        struct sockaddr_in address;
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 20 * 1000 * 1000 };
        char request_bytes[512];
        char response[65536];
        int fd = -1;

        memset(&address, 0, sizeof(address));
        address.sin_family = AF_INET;
        address.sin_port = htons((unsigned short)port);
        address.sin_addr.s_addr = htonl(0x7f000001UL);

        for (int attempt = 0; attempt < 500; attempt++) {
          fd = socket(AF_INET, SOCK_STREAM, 0);
          if (fd < 0) return 10;
          if (connect(fd, (struct sockaddr *)&address, sizeof(address)) == 0) break;
          close(fd);
          fd = -1;
          nanosleep(&pause, NULL);
        }
        if (fd < 0) return 11;

        int request_length = snprintf(
          request_bytes,
          sizeof(request_bytes),
          "GET %s HTTP/1.1\r\n"
          "Host: localhost\r\n"
          "%s"
          "Connection: close\r\n\r\n",
          path,
          extra_header);
        if (request_length < 0 || (size_t)request_length >= sizeof(request_bytes)) return 12;

        size_t sent = 0;
        while (sent < (size_t)request_length) {
          ssize_t count = send(fd, request_bytes + sent, (size_t)request_length - sent, 0);
          if (count <= 0) return 13;
          sent += (size_t)count;
        }

        size_t used = 0;
        while (used < sizeof(response) - 1) {
          ssize_t count = recv(fd, response + used, sizeof(response) - 1 - used, 0);
          if (count < 0) return 14;
          if (count == 0) break;
          used += (size_t)count;
        }
        close(fd);
        response[used] = '\0';

        char expected_status_line[32];
        snprintf(
          expected_status_line,
          sizeof(expected_status_line),
          "HTTP/1.1 %d ",
          expected_status);
        if (strncmp(response, expected_status_line, strlen(expected_status_line)) != 0) return 15;

        char *body = strstr(response, "\r\n\r\n");
        if (body == NULL) return 16;
        body += 4;
        if (expected_text != NULL && strstr(body, expected_text) == NULL) return 17;

        if (expect_gzip) {
          if (strstr(response, "\r\nContent-Encoding: gzip\r\n") == NULL) return 18;
          unsigned char *cursor = (unsigned char *)body;
          unsigned char *end = (unsigned char *)response + used;
          int found_magic = 0;
          while (cursor + 1 < end) {
            if (cursor[0] == 0x1f && cursor[1] == 0x8b) {
              found_magic = 1;
              break;
            }
            cursor++;
          }
          if (!found_magic) return 19;
        }
        return 0;
      }

      int main(int argc, char **argv) {
        if (argc != 2) return 2;
        int port = atoi(argv[1]);
        if (port < 1 || port > 65535) return 3;

        int result = request(
          port,
          "/old/message.txt",
          "",
          200,
          "nginx rewrite and gzip through Kandelo\n",
          0);
        if (result != 0) return result;

        result = request(
          port,
          "/new/message.txt",
          "Accept-Encoding: gzip\r\n",
          200,
          NULL,
          1);
        if (result != 0) return result;

        result = request(port, "/missing", "", 404, NULL, 0);
        if (result != 0) return result;

        puts("nginx-browser-http-ok");
        return 0;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, "-std=c17", "-O2", browser_probe_source, "-o", browser_probe
      kandelo_validate_wasm_artifact(browser_probe, fork: :forbidden)
    end

    browser_root = "/opt/kandelo-nginx-test"
    browser_config = testpath/"nginx-browser.conf"
    # WHY: the Node lifecycle uses a writable /tmp host mount, while the
    # browser runner stages immutable fixture files outside its scratch mounts.
    browser_config.write((testpath/"nginx.conf").read.sub(guest_testpath, browser_root))
    browser_script = <<~SH
      set -eu
      server=#{GUEST_OPT_PREFIX}/bin/nginx
      "$server" -p #{browser_root}/ -c #{browser_root}/nginx.conf &
      server_pid=$!
      cleanup() {
        kill -QUIT "$server_pid" 2>/dev/null || :
        wait "$server_pid" 2>/dev/null || :
      }
      trap cleanup EXIT HUP INT TERM

      /usr/local/bin/nginx-browser-probe 18080
      kill -QUIT "$server_pid"
      wait "$server_pid"
      trap - EXIT HUP INT TERM
      printf 'nginx-browser-service-ok\n'
    SH
    dash = formula_opt_bin("kandelo-dev/tap-core/dash")/"dash"
    browser_output = kandelo_run_browser_wasm(
      dash,
      ["-c", browser_script],
      argv0:              "sh",
      guest_program_path: "/bin/sh",
      exec_programs:      {
        "#{GUEST_OPT_PREFIX}/bin/nginx"      => artifact,
        "/usr/local/bin/nginx-browser-probe" => browser_probe,
      },
      guest_files:        {
        "#{GUEST_OPT_PREFIX}/conf/mime.types"  => prefix/"conf/mime.types",
        "#{browser_root}/nginx.conf"           => browser_config,
        "#{browser_root}/html/new/message.txt" => testpath/"html/new/message.txt",
      },
      timeout_ms:         180_000,
      merge_stderr:       true,
    )
    assert_includes browser_output, "nginx-browser-http-ok\n"
    assert_includes browser_output, "nginx-browser-service-ok\n"
  end
end
