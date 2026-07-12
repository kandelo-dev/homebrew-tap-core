require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s
require "openssl"
require "socket"
require "zlib"

class Wget < Formula
  include KandeloFormulaSupport

  GUEST_HOMEBREW_PREFIX = "/home/linuxbrew/.linuxbrew".freeze
  GUEST_OPT_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/wget".freeze
  GUEST_OPENSSL_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/openssl".freeze
  GUEST_ZLIB_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/zlib".freeze
  GUEST_WGETRC = "#{GUEST_HOMEBREW_PREFIX}/etc/wgetrc".freeze

  desc "GNU network file retriever for Kandelo"
  homepage "https://www.gnu.org/software/wget/"
  url "https://ftpmirror.gnu.org/gnu/wget/wget-1.25.0.tar.gz"
  mirror "https://ftp.gnu.org/gnu/wget/wget-1.25.0.tar.gz"
  sha256 "766e48423e79359ea31e41db9e5c289675947a7fcf2efdcedb726ac9d0da3784"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/openssl"
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "bin/wget"

  def install
    kandelo_require_arch!("wasm32")
    openssl = formula_opt_prefix("automattic/kandelo-homebrew/openssl")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")

    kandelo_wasm_build do |root|
      path_maps = {
        buildpath.to_s => "/usr/src/wget",
        root.to_s      => "/usr/src/kandelo",
        openssl.to_s   => GUEST_OPENSSL_PREFIX,
        zlib.to_s      => GUEST_ZLIB_PREFIX,
        prefix.to_s    => GUEST_OPT_PREFIX,
      }
      prefix_map_flags = path_maps.map do |source, destination|
        "-ffile-prefix-map=#{source}=#{destination} " \
          "-fdebug-prefix-map=#{source}=#{destination} " \
          "-fmacro-prefix-map=#{source}=#{destination}"
      end

      # Wget records its compiler and flags in `wget --version`; use the stable
      # SDK tool name and map build locations to guest/source identities.
      ENV["CC"] = "#{kandelo_arch}posix-cc"
      ENV["CFLAGS"] = "-O2 #{prefix_map_flags.join(" ")}"
      ENV["CPPFLAGS"] = "-I#{openssl}/include -I#{zlib}/include"
      ENV["LDFLAGS"] = "-L#{openssl}/lib -L#{zlib}/lib"
      ENV["OPENSSL_CFLAGS"] = "-I#{openssl}/include"
      ENV["OPENSSL_LIBS"] = "-L#{openssl}/lib -lssl -lcrypto -ldl"
      ENV["ZLIB_CFLAGS"] = "-I#{zlib}/include"
      ENV["ZLIB_LIBS"] = "-L#{zlib}/lib -lz"

      system kandelo_configure,
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--sysconfdir=#{GUEST_HOMEBREW_PREFIX}/etc",
        "--disable-nls",
        "--disable-iri",
        "--disable-pcre",
        "--disable-pcre2",
        "--disable-xattr",
        "--without-libpsl",
        "--without-metalink",
        "--without-libuuid",
        "--with-ssl=openssl"
      system "make", "-j#{ENV.make_jobs}"

      # Upstream deliberately exposes the full compile/link flags in --version.
      # Preserve that information while replacing host staging paths with the
      # same stable identities used in debug metadata.
      version_source = buildpath/"src/version.c"
      version_contents = version_source.read
      path_maps.each { |source, destination| version_contents.gsub!(source, destination) }
      version_source.atomic_write(version_contents)
      [buildpath/"src/version.o", buildpath/"src/wget-version.o", buildpath/"src/wget"].each do |object|
        object.delete if object.exist?
      end
      system "make", "-C", "src", "wget"

      artifact = kandelo_fork_instrument(buildpath/"src/wget")
      host_only_paths = path_maps.filter_map do |source, destination|
        source if source != destination
      end
      kandelo_validate_wasm_artifact(
        artifact,
        fork:            :required,
        forbidden_paths: host_only_paths,
      )
    end

    kandelo_install_bin(buildpath/"src", "wget", "wget")
    etc.install buildpath/"doc/sample.wgetrc" => "wgetrc"
    info.install buildpath/"doc/wget.info"
    man1.install buildpath/"doc/wget.1"
  end

  test do
    assert_path_exists etc/"wgetrc"
    assert_path_exists info/"wget.info"
    assert_path_exists man1/"wget.1"

    test_wgetrc = testpath/"wgetrc"
    test_wgetrc.binwrite((etc/"wgetrc").binread + "\nquiet = on\n")
    version_guest_files = { GUEST_WGETRC => test_wgetrc }
    guest_files = { GUEST_WGETRC => etc/"wgetrc" }
    version_output = kandelo_run_wasm(
      bin/"wget", ["--version"], guest_files: version_guest_files
    )
    assert_match(/^GNU Wget 1\.25\.0 /, version_output)
    assert_match(%r{(?:\A|\s)\+ssl/openssl(?:\s|\z)}, version_output)
    %w[-gpgme -iri -metalink -nls -psl].each do |feature|
      assert_includes version_output.split, feature
    end
    assert_match(/#{Regexp.escape(GUEST_WGETRC)} \(system\)/o, version_output)
    [
      [prefix.to_s, GUEST_OPT_PREFIX],
      [etc.to_s, File.dirname(GUEST_WGETRC)],
      [formula_opt_prefix("automattic/kandelo-homebrew/openssl").to_s, GUEST_OPENSSL_PREFIX],
      [formula_opt_prefix("automattic/kandelo-homebrew/zlib").to_s, GUEST_ZLIB_PREFIX],
    ].each do |source, destination|
      assert_includes version_output, destination
      refute_includes version_output, source if source != destination
    end

    ca_key = OpenSSL::PKey::RSA.new(2048)
    ca_cert = OpenSSL::X509::Certificate.new
    ca_cert.version = 2
    ca_cert.serial = 1
    ca_cert.subject = OpenSSL::X509::Name.parse("/CN=Kandelo Wget Test CA")
    ca_cert.issuer = ca_cert.subject
    ca_cert.public_key = ca_key.public_key
    ca_cert.not_before = Time.now - 60
    ca_cert.not_after = Time.now + 3600
    ca_extensions = OpenSSL::X509::ExtensionFactory.new
    ca_extensions.subject_certificate = ca_cert
    ca_extensions.issuer_certificate = ca_cert
    ca_cert.add_extension(ca_extensions.create_extension("basicConstraints", "CA:TRUE", true))
    ca_cert.add_extension(ca_extensions.create_extension("keyUsage", "keyCertSign,cRLSign", true))
    ca_cert.add_extension(ca_extensions.create_extension("subjectKeyIdentifier", "hash"))
    ca_cert.sign(ca_key, OpenSSL::Digest.new("SHA256"))

    server_key = OpenSSL::PKey::RSA.new(2048)
    server_cert = OpenSSL::X509::Certificate.new
    server_cert.version = 2
    server_cert.serial = 2
    server_cert.subject = OpenSSL::X509::Name.parse("/CN=127.0.0.1")
    server_cert.issuer = ca_cert.subject
    server_cert.public_key = server_key.public_key
    server_cert.not_before = Time.now - 60
    server_cert.not_after = Time.now + 3600
    server_extensions = OpenSSL::X509::ExtensionFactory.new
    server_extensions.subject_certificate = server_cert
    server_extensions.issuer_certificate = ca_cert
    server_cert.add_extension(server_extensions.create_extension("basicConstraints", "CA:FALSE", true))
    server_cert.add_extension(
      server_extensions.create_extension("keyUsage", "digitalSignature,keyEncipherment", true),
    )
    server_cert.add_extension(server_extensions.create_extension("extendedKeyUsage", "serverAuth"))
    server_cert.add_extension(server_extensions.create_extension("subjectAltName", "IP:127.0.0.1"))
    server_cert.sign(ca_key, OpenSSL::Digest.new("SHA256"))

    ca_file = testpath/"wget-test-ca.pem"
    ca_file.write(ca_cert.to_pem)
    compressed_payload = Zlib.gzip("{\"gzipped\":true}\n")
    tls_server = TCPServer.new("127.0.0.1", 0)
    tls_context = OpenSSL::SSL::SSLContext.new
    tls_context.cert = server_cert
    tls_context.key = server_key
    ssl_server = OpenSSL::SSL::SSLServer.new(tls_server, tls_context)
    tls_error = nil
    tls_thread = Thread.new do
      client = nil
      begin
        client = ssl_server.accept
        request = +""
        request << client.readpartial(1024) until request.include?("\r\n\r\n")
        raise "unexpected Wget TLS request: #{request.lines.first.inspect}" unless request.start_with?("GET /gzip ")
        raise "Wget did not request gzip compression" unless request.match?(/^Accept-Encoding:\s*gzip\s*$/i)

        client.write([
          "HTTP/1.1 200 OK",
          "Content-Type: application/json",
          "Content-Encoding: gzip",
          "Content-Length: #{compressed_payload.bytesize}",
          "Connection: close",
          "",
          "",
        ].join("\r\n"))
        client.write(compressed_payload)
      rescue => e
        tls_error = e
      ensure
        client&.close
      end
    end

    begin
      compressed_page = kandelo_run_wasm(
        bin/"wget",
        [
          "--quiet",
          "--no-hsts",
          "--compression=auto",
          "--ca-certificate=/etc/wget-test-ca.pem",
          "--timeout=10",
          "--tries=1",
          "--output-document=-",
          "https://127.0.0.1:#{tls_server.addr[1]}/gzip",
        ],
        guest_files: guest_files.merge("/etc/wget-test-ca.pem" => ca_file),
        network:     true,
      )
      assert tls_thread.join(2), "Wget did not complete its HTTPS request"
      raise tls_error if tls_error

      assert_equal "{\"gzipped\":true}\n", compressed_page
    ensure
      tls_server.close
      tls_thread.kill if tls_thread.alive?
      tls_thread.join
    end

    background_dir = testpath/"background"
    background_dir.mkpath
    background_payload = "Kandelo Wget background child completed\n"
    server = TCPServer.new("127.0.0.1", 0)
    server_error = nil
    server_thread = Thread.new do
      client = nil
      begin
        client = server.accept
        request = +""
        request << client.readpartial(1024) until request.include?("\r\n\r\n")
        raise "unexpected Wget request: #{request.lines.first.inspect}" unless request.start_with?("GET /background ")

        client.write([
          "HTTP/1.1 200 OK",
          "Content-Type: text/plain",
          "Content-Length: #{background_payload.bytesize}",
          "Connection: close",
          "",
          background_payload,
        ].join("\r\n"))
      rescue => e
        server_error = e
      ensure
        client&.close
      end
    end

    begin
      background_output = kandelo_run_wasm(
        bin/"wget",
        [
          "--background",
          "--no-hsts",
          "--timeout=10",
          "--tries=1",
          "--output-document=/work/background.txt",
          "--output-file=/work/wget.log",
          "http://127.0.0.1:#{server.addr[1]}/background",
        ],
        env:                       { "TIMEOUT" => "15000" },
        merge_stderr:              true,
        network:                   true,
        guest_files:               guest_files,
        writable_host_directories: { "/work" => background_dir },
        expected_fork_descendants: 1,
      )
      background_pid = background_output[/Continuing in background, pid ([1-9]\d*)\./, 1]
      refute_nil background_pid
      assert server_thread.join(2), "background Wget child did not complete its HTTP request"
      raise server_error if server_error

      assert_equal background_payload, (background_dir/"background.txt").read
      assert_match(/saved/, (background_dir/"wget.log").read)
    ensure
      server.close
      server_thread.kill if server_thread.alive?
      server_thread.join
    end

    failure = kandelo_run_wasm(
      bin/"wget",
      [
        "--no-hsts",
        "--timeout=2",
        "--tries=1",
        "--output-document=-",
        "http://127.0.0.1:1/",
      ],
      merge_stderr:    true,
      guest_files:     guest_files,
      network:         true,
      expected_status: 4,
    )
    assert_match(/Connection refused/, failure)
  end
end
