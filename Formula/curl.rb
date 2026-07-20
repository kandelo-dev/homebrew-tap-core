require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Curl < Formula
  include KandeloFormulaSupport

  desc "Command-line multiprotocol file transfer tool for Kandelo"
  homepage "https://curl.se/"
  url "https://curl.se/download/curl-8.11.1.tar.xz"
  sha256 "c7ca7db48b0909743eaef34250da02c19bc61d4f1dcedd6603f109409536ab56"
  license "curl"

  depends_on "pkgconf" => :build
  depends_on "kandelo-dev/tap-core/libcurl"
  depends_on "kandelo-dev/tap-core/openssl"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/curl"

  def install
    kandelo_require_arch!("wasm32", "wasm64")
    libcurl = formula_opt_prefix("kandelo-dev/tap-core/libcurl")
    openssl = formula_opt_prefix("kandelo-dev/tap-core/openssl")
    zlib = formula_opt_prefix("kandelo-dev/tap-core/zlib")

    kandelo_wasm_build do
      ENV["CPPFLAGS"] = "-I#{libcurl}/include -I#{openssl}/include -I#{zlib}/include"
      ENV["LDFLAGS"] = "-L#{libcurl}/lib -L#{openssl}/lib -L#{zlib}/lib"
      ENV["LIBS"] = "-ldl -pthread"
      ENV["OPENSSL_CFLAGS"] = "-I#{openssl}/include"
      ENV["OPENSSL_LIBS"] = "-L#{openssl}/lib -lssl -lcrypto -ldl -pthread"
      ENV["PKG_CONFIG_LIBDIR"] = [
        libcurl/"lib/pkgconfig",
        openssl/"lib/pkgconfig",
        zlib/"lib/pkgconfig",
      ].join(":")
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")

      # Match libcurl's cross-probe results while generating the private
      # configuration headers used by curl's command-line sources.
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

      pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
      libcurl_version = Utils.safe_popen_read(pkgconf, "--modversion", "libcurl").strip
      odie "curl #{version} requires libcurl #{version}, found #{libcurl_version}" if libcurl_version != version.to_s
      link_flags = Utils.safe_popen_read(pkgconf, "--static", "--libs", "libcurl").split

      # Upstream's curl target hardcodes its sibling libcurl.la. Build only the
      # CLI and replace that dependency with the installed tap libcurl contract.
      system "make", "-C", "src", "curl", "curl_DEPENDENCIES=", "curl_LDADD=#{link_flags.join(" ")}"
    end

    kandelo_install_bin(buildpath/"src", "curl", "curl")
  end

  test do
    root = kandelo_require_root!
    version_output = kandelo_run_wasm(bin/"curl", ["--version"])
    assert_match(%r{^curl 8\.11\.1 .* libcurl/8\.11\.1 }, version_output)
    assert_match(%r{ OpenSSL/[0-9]}, version_output)
    assert_match(%r{ zlib/[0-9]}, version_output)
    assert_match(/^Protocols: .*\bfile\b.*\bhttp\b.*\bhttps\b/, version_output)
    assert_match(/^Features: .*\bAsynchDNS\b/, version_output)
    assert_match(/^Features: .*\bSSL\b/, version_output)
    assert_match(/^Features: .*\bUnixSockets\b/, version_output)
    assert_match(/^Features: .*\blibz\b/, version_output)
    assert_match(/^Features: .*\bthreadsafe\b/, version_output)

    write_out = "curl-ok %" + "{http_code} %" + "{ssl_verify_result}\\n"
    ca_bundle = Pathname(root)/"images/rootfs/etc/ssl/cert.pem"
    assert_path_exists ca_bundle
    guest_ca_bundle = "/etc/ssl/certs/ca-certificates.crt"
    output = kandelo_run_wasm(
      bin/"curl",
      [
        "--disable",
        "--fail",
        "--silent",
        "--show-error",
        "--compressed",
        "--http1.1",
        "--tlsv1.2",
        "--tls-max", "1.2",
        "--max-time", "20",
        "--output", "/dev/null",
        "--write-out", write_out,
        "https://example.com/"
      ],
      network:     true,
      guest_files: { guest_ca_bundle => ca_bundle },
    )
    assert_equal "curl-ok 200 0\n", output
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "4a9d6cd91c21b3cd5ea72b2c8b02a7bff712126a67fbc0c93a230b364eacdf02"
    sha256 cellar: :any_skip_relocation, wasm64_kandelo: "331fb01d9e47a0c7cfdb3eee50c3e0f57609996f2851d074b741b8410bc29284"
  end

end
