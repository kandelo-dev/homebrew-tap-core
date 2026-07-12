require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

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

      # The SDK site owns target facts; this gnulib runtime probe is package-specific.
      ENV["gl_cv_func_strerror_0_works"] = "yes"

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

    compressed_page = kandelo_run_wasm(
      bin/"wget",
      [
        "--quiet",
        "--no-hsts",
        "--compression=auto",
        "--timeout=20",
        "--tries=1",
        "--output-document=-",
        "https://nghttp2.org/httpbin/gzip",
      ],
      guest_files: guest_files,
      network:     true,
    )
    assert_match(/"gzipped":\s*true/, compressed_page)
    assert_match(/"Accept-Encoding":\s*"gzip"/, compressed_page)

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
