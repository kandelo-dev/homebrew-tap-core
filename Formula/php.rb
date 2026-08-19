require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Php < Formula
  include KandeloFormulaSupport

  KANDELO_REGISTRY_BRIDGE = true
  EXTENSION_DIRECTORY = "lib/php/extensions".freeze
  RUNTIME_OUTPUTS = {
    "php.wasm"     => "bin/php",
    "php-fpm.wasm" => "sbin/php-fpm",
    "opcache.so"   => "lib/php/extensions/opcache.so",
    "curl.so"      => "lib/php/extensions/curl.so",
    "phar.so"      => "lib/php/extensions/phar.so",
    "zip.so"       => "lib/php/extensions/zip.so",
    "intl.so"      => "lib/php/extensions/intl.so",
    "icu.dat"      => "share/php/icu.dat",
  }.freeze

  desc "PHP CLI and FastCGI runtime for Kandelo"
  homepage "https://www.php.net/"
  url "https://www.php.net/distributions/php-8.3.15.tar.gz"
  version "8.3.15"
  sha256 "67073c3c9c56c86461e0715d9e1806af5ddffe8e6e2eb9781f7923bbb5bd67fa"
  license "PHP-3.01"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  depends_on "kandelo-dev/tap-core/icu"
  depends_on "kandelo-dev/tap-core/libcurl"
  depends_on "kandelo-dev/tap-core/libcxx"
  depends_on "kandelo-dev/tap-core/libiconv"
  depends_on "kandelo-dev/tap-core/libxml2"
  depends_on "kandelo-dev/tap-core/libzip"
  depends_on "kandelo-dev/tap-core/openssl"
  depends_on "kandelo-dev/tap-core/sqlite"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/php", "sbin/php-fpm", EXTENSION_DIRECTORY

  def install
    kandelo_require_arch!("wasm32")
    out_dir = kandelo_build_package(
      package:    "php",
      script_env: {
        "WASM_POSIX_DEP_ICU_DIR"     => formula_opt_prefix("kandelo-dev/tap-core/icu"),
        "WASM_POSIX_DEP_LIBCURL_DIR" => formula_opt_prefix("kandelo-dev/tap-core/libcurl"),
        "WASM_POSIX_DEP_LIBCXX_DIR"  => formula_opt_prefix("kandelo-dev/tap-core/libcxx"),
        "WASM_POSIX_DEP_LIBICONV_DIR" => formula_opt_prefix("kandelo-dev/tap-core/libiconv"),
        "WASM_POSIX_DEP_LIBXML2_DIR" => formula_opt_prefix("kandelo-dev/tap-core/libxml2"),
        "WASM_POSIX_DEP_LIBZIP_DIR"  => formula_opt_prefix("kandelo-dev/tap-core/libzip"),
        "WASM_POSIX_DEP_OPENSSL_DIR" => formula_opt_prefix("kandelo-dev/tap-core/openssl"),
        "WASM_POSIX_DEP_SQLITE_DIR"  => formula_opt_prefix("kandelo-dev/tap-core/sqlite"),
        "WASM_POSIX_DEP_ZLIB_DIR"    => formula_opt_prefix("kandelo-dev/tap-core/zlib"),
      },
    )

    kandelo_validate_wasm_artifact(out_dir/"php.wasm", fork: :required)
    kandelo_validate_wasm_artifact(out_dir/"php-fpm.wasm", fork: :required)
    %w[opcache.so curl.so phar.so zip.so intl.so].each do |extension|
      kandelo_validate_wasm_artifact(out_dir/extension, fork: :auto)
    end

    RUNTIME_OUTPUTS.each do |source_name, relative|
      destination = prefix/relative
      destination.dirname.install out_dir/source_name => destination.basename
      chmod(relative.start_with?("bin/", "sbin/") ? 0755 : 0644, destination)
    end
  end

  test do
    assert_path_exists bin/"php"
    assert_path_exists sbin/"php-fpm"
    assert_path_exists lib/"php/extensions/opcache.so"
    assert_path_exists lib/"php/extensions/curl.so"
    assert_path_exists lib/"php/extensions/phar.so"
    assert_path_exists lib/"php/extensions/zip.so"
    assert_path_exists lib/"php/extensions/intl.so"
    assert_path_exists share/"php/icu.dat"
    assert_equal "php-ok\n", kandelo_run_wasm(bin/"php", ["-r", "echo \"php-ok\\n\";"])
  end
end
