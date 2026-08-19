require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Php < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true
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
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "d9e0c628f7e13b83c5b6831557d07e4f3d0d1b6b00fce5f5d0b3dc769b40804b",
      script_env:      {},
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
