require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Nginx < Formula
  include KandeloFormulaSupport

  desc "HTTP and reverse proxy server for Kandelo"
  homepage "https://nginx.org/"
  url "https://nginx.org/download/nginx-1.30.3.tar.gz"
  sha256 "e5823dc6f45610993def93ebf6cfce68264af4958c77e874b7d20f3709001b8f"
  license "BSD-2-Clause"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/pcre2"
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "bin/nginx"

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/nginx".freeze

  def install
    kandelo_require_arch!("wasm32")
    pcre2 = formula_opt_prefix("automattic/kandelo-homebrew/pcre2")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")

    kandelo_wasm_build do |root|
      # nginx's supported --crossbuild mode still executes target feature
      # probes directly. Route those probes through Kandelo so configure sees
      # target behavior instead of host behavior or guessed cache answers.
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

      rm_r Pathname(root)/"host/dist", force: true
      target_runner = buildpath/"kandelo-nginx-configure-runner"
      tap_root = Tap.fetch("automattic", "kandelo-homebrew").path
      configure_runner = tap_root/"Kandelo/formula_support/run-network-wasm.ts"
      target_runner.write <<~SH
        #!/bin/sh
        case "$1" in
          /*) program="$1" ;;
          *) program="$PWD/$1" ;;
        esac
        cd #{root.to_s.shellescape}
        exec node --experimental-wasm-exnref --import tsx/esm \
          #{configure_runner.to_s.shellescape} \
          #{root.to_s.shellescape} "$program" </dev/null
      SH
      chmod 0755, target_runner
      ENV["NGX_TEST_RUNNER"] = target_runner
      ENV["NGX_USER"] = "nobody"
      ENV["NGX_GROUP"] = "nobody"

      include_flags = [pcre2/"include", zlib/"include"].map { |path| "-I#{path}" }
      include_flags << "-O2" << "-gline-tables-only"
      include_flags << "-Wno-sign-compare"
      include_flags << "-ffile-prefix-map=#{buildpath}=."
      link_flags = [pcre2/"lib", zlib/"lib"].map { |path| "-L#{path}" }

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
        "--with-poll_module",
        "--without-select_module",
        "--with-http_stub_status_module",
        "--with-cc=#{kandelo_cc(root)}",
        "--with-cc-opt=#{include_flags.join(" ")}",
        "--with-ld-opt=#{link_flags.join(" ")}"

      # Keep nginx -V useful without embedding the build machine's dependency
      # and Cellar paths in the executable.
      inreplace "objs/ngx_auto_config.h", /^#define NGX_CONFIGURE .*$/,
        '#define NGX_CONFIGURE " --crossbuild=Kandelo:wasm32 --with-poll_module --with-http_stub_status_module"'

      system "make", "-j#{ENV.make_jobs}"

      optimized = buildpath/"objs/nginx.optimized"
      system "wasm-opt", "-O2", "objs/nginx", "-o", optimized
      kandelo_fork_instrument(optimized)
      kandelo_validate_wasm_artifact(optimized, fork: :required)

      bin.install optimized => "nginx"
      chmod 0755, bin/"nginx"
      prefix.install "conf", "html"
      man8.install "objs/nginx.8"
    end
  end

  test do
    version_output = kandelo_run_wasm(bin/"nginx", ["-V"], merge_stderr: true)
    assert_match "nginx version: nginx/1.30.3", version_output
    assert_match "--crossbuild=Kandelo:wasm32", version_output

    (testpath/"html/new").mkpath
    [testpath, testpath/"html", testpath/"html/new"].each { |path| chmod 0755, path }
    body = "nginx rewrite and gzip through Kandelo\n" * 8
    (testpath/"html/new/message.txt").write body
    guest_testpath = "/tmp/kandelo-nginx-test"
    (testpath/"nginx.conf").write <<~EOS
      daemon off;
      master_process on;
      pid /tmp/kandelo-nginx.pid;
      error_log /dev/stderr notice;
      events { worker_connections 64; }
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

    responses = kandelo_run_http_service(
      bin/"nginx",
      ["-p", "#{guest_testpath}/", "-c", "#{guest_testpath}/nginx.conf"],
      port:     18080,
      mounts:   {
        guest_testpath   => testpath.to_s,
        GUEST_OPT_PREFIX => opt_prefix.to_s,
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
    )

    assert_equal 200, responses[0]["status"]
    assert_equal body, responses[0]["text"]
    assert_equal 200, responses[1]["status"]
    gzip_headers = responses[1]["headers"].transform_keys(&:downcase)
    assert_equal "gzip", gzip_headers["content-encoding"]
    assert responses[1]["body"].start_with?("H4sI")
    assert_equal 404, responses[2]["status"]
  end
end
