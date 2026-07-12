require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Openssl < Formula
  include KandeloFormulaSupport

  desc "TLS and cryptography library for Kandelo"
  homepage "https://www.openssl.org/"
  url "https://github.com/openssl/openssl/releases/download/openssl-3.3.2/openssl-3.3.2.tar.gz"
  sha256 "2e8a40b01979afe8be0bbfb3de5dc1c6709fedb46d6c89c10da114ab5fc3d281"
  license "Apache-2.0"
  revision 2

  keg_only "its Kandelo target headers and libraries conflict with native Homebrew OpenSSL"

  skip_clean "lib/libssl.a"
  skip_clean "lib/libcrypto.a"

  def install
    kandelo_require_arch!("wasm32", "wasm64")
    openssl_target = (kandelo_arch == "wasm64") ? "linux-generic64" : "linux-generic32"
    guest_opt = "/home/linuxbrew/.linuxbrew/opt/openssl"
    escaped_quote = %q(\")

    kandelo_wasm_build do
      # OpenSSL records CC verbatim in libcrypto's build information. PATH
      # already resolves this name through the activated Kandelo SDK.
      ENV["CC"] = "#{kandelo_arch}posix-cc"

      system "perl", "Configure", openssl_target,
        "-DOPENSSL_NO_AFALGENG=1",
        "no-asm",
        "no-dso",
        "no-shared",
        "no-async",
        "no-engine",
        "no-afalgeng",
        "no-tests",
        "no-apps",
        "--prefix=#{prefix}",
        "--libdir=lib",
        "--openssldir=/etc/ssl"

      # Compile runtime lookup paths against the stable guest opt symlink. Keep
      # the Make variables themselves on the real keg prefix so install_sw and
      # pkg-config metadata remain usable by later source builds.
      inreplace "Makefile",
                %q(-DENGINESDIR="\"$(ENGINESDIR)\""),
                %Q(-DENGINESDIR="#{escaped_quote}#{guest_opt}/lib/engines-3#{escaped_quote}")
      inreplace "Makefile",
                %q(-DMODULESDIR="\"$(MODULESDIR)\""),
                %Q(-DMODULESDIR="#{escaped_quote}#{guest_opt}/lib/ossl-modules#{escaped_quote}")

      system "make", "build_generated", "libssl.a", "libcrypto.a"
      system "make", "install_sw"
    end
  end

  test do
    assert_path_exists lib/"libssl.a"
    assert_path_exists lib/"libcrypto.a"
    assert_path_exists include/"openssl/ssl.h"
    assert_path_exists lib/"pkgconfig/libssl.pc"
    assert_path_exists lib/"pkgconfig/libcrypto.pc"
    crypto_pc = (lib/"pkgconfig/libcrypto.pc").read
    assert_includes crypto_pc, "enginesdir=${libdir}/engines-3"
    assert_includes crypto_pc, "modulesdir=${libdir}/ossl-modules"

    builder_path_markers = [
      prefix.to_s,
      kandelo_require_root!,
      "/private/tmp/",
      "/nix/store/",
    ]
    %w[libssl.a libcrypto.a].each do |archive|
      binary = File.binread(lib/archive)
      builder_path_markers.each do |marker|
        refute binary.include?(marker), "#{archive} contains builder path marker #{marker}"
      end
      refute binary.match?(%r{/Users/[^/]+/}), "#{archive} contains a builder home path"
    end

    source = testpath/"openssl-smoke.c"
    wasm = testpath/"openssl-smoke.wasm"
    source.write <<~C
      #include <openssl/crypto.h>
      #include <openssl/evp.h>
      #include <openssl/ssl.h>
      #include <stdio.h>
      #include <string.h>

      static int directory_matches(const char *value, const char *label, const char *path) {
        size_t label_len = strlen(label);
        size_t path_len = strlen(path);

        return strncmp(value, label, label_len) == 0 &&
          value[label_len] == '"' &&
          strncmp(value + label_len + 1, path, path_len) == 0 &&
          value[label_len + path_len + 1] == '"' &&
          value[label_len + path_len + 2] == 0;
      }

      int main(void) {
        static const unsigned char expected[] = {
          0x36, 0x37, 0xd0, 0x66, 0x5a, 0xfe, 0x8b, 0x23,
          0x3e, 0xd9, 0x20, 0xca, 0x6f, 0xa0, 0x7c, 0xda,
          0x5c, 0x35, 0xd1, 0x35, 0xd3, 0x56, 0xbb, 0x70,
          0x8a, 0xe4, 0xae, 0xe9, 0x65, 0x56, 0xfc, 0x26,
        };
        static const char expected_compiler[] = "compiler: #{kandelo_arch}posix-cc ";
        static const char expected_openssldir[] = "/etc/ssl";
        static const char expected_enginesdir[] =
          "/home/linuxbrew/.linuxbrew/opt/openssl/lib/engines-3";
        static const char expected_modulesdir[] =
          "/home/linuxbrew/.linuxbrew/opt/openssl/lib/ossl-modules";
        unsigned char digest[EVP_MAX_MD_SIZE];
        unsigned int digest_len = 0;
        const char *compiler = OpenSSL_version(OPENSSL_CFLAGS);
        SSL_CTX *ctx = SSL_CTX_new(TLS_client_method());

        if (ctx == NULL) return 1;
        if (EVP_Digest("kandelo", 7, digest, &digest_len, EVP_sha256(), NULL) != 1) return 2;
        if (digest_len != sizeof(expected) || memcmp(digest, expected, sizeof(expected)) != 0) return 3;
        if (strncmp(compiler, expected_compiler, sizeof(expected_compiler) - 1) != 0) return 4;
        if (!directory_matches(OpenSSL_version(OPENSSL_DIR), "OPENSSLDIR: ", expected_openssldir)) return 5;
        if (!directory_matches(OpenSSL_version(OPENSSL_ENGINES_DIR), "ENGINESDIR: ", expected_enginesdir)) return 6;
        if (!directory_matches(OpenSSL_version(OPENSSL_MODULES_DIR), "MODULESDIR: ", expected_modulesdir)) return 7;

        SSL_CTX_free(ctx);
        puts("openssl-ok");
        return 0;
      }
    C

    kandelo_wasm_build do
      system kandelo_cc, source, "-I#{include}", "-L#{lib}", "-lssl", "-lcrypto", "-ldl", "-o", wasm
    end
    assert_equal "openssl-ok\n", kandelo_run_wasm(wasm, [])
  end
end
