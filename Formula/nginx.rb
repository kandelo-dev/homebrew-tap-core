require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Nginx < Formula
  include KandeloFormulaSupport

  GUEST_HOMEBREW_PREFIX = "/home/linuxbrew/.linuxbrew".freeze
  GUEST_OPT_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/nginx".freeze
  GUEST_PCRE2_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/pcre2".freeze
  GUEST_ZLIB_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/zlib".freeze

  desc "HTTP and reverse proxy server for Kandelo"
  homepage "https://nginx.org/"
  url "https://nginx.org/download/nginx-1.30.3.tar.gz"
  sha256 "e5823dc6f45610993def93ebf6cfce68264af4958c77e874b7d20f3709001b8f"
  license "BSD-2-Clause"

  depends_on KandeloFormulaSupport::BinaryenRequirement => [:build, :test]
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  depends_on "kandelo-dev/tap-core/pcre2"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/nginx"

  def install
    kandelo_require_arch!("wasm32")
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
      configure_runner = Pathname(root)/"examples/run-example.ts"
      node = ENV.fetch("HOMEBREW_KANDELO_NODE", "node")
      node = "node" if node.empty?
      runner_environment = kandelo_node_runner_environment
      runner_invocation = if runner_environment.empty?
        # Outside the isolated publisher there is no sealed program-index
        # checker to restore. Re-enter this checkout's declared dev shell and
        # remove target compiler variables before the host resolver runs.
        "PATH=/nix/var/nix/profiles/default/bin:$PATH exec " \
          "#{(Pathname(root)/"scripts/dev-shell.sh").to_s.shellescape} " \
          "env -u CC -u CXX -u AR -u RANLIB -u NM -u STRIP -u PKG_CONFIG " \
          "#{node.shellescape}"
      else
        "#{runner_environment}exec #{node.shellescape}"
      end
      target_runner.write <<~SH
        #!/bin/sh
        set -eu
        case "$1" in
          /*) program="$1" ;;
          *) program="$PWD/$1" ;;
        esac
        # run-example.ts recognizes absolute Wasm paths by their suffix, while
        # nginx deliberately names configure probes without an extension.
        # A sibling hard link preserves the probe bytes and is removed by
        # nginx's existing autotest cleanup.
        wasm_program="$program.wasm"
        ln -f "$program" "$wasm_program"
        cd #{root.to_s.shellescape}
        #{runner_invocation} \
          --experimental-wasm-exnref --import tsx/esm \
          #{configure_runner.to_s.shellescape} \
          "$wasm_program" </dev/null
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

    assert_path_exists conf/"mime.types"
    assert_path_exists conf/"fastcgi_params"
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
  end
end
