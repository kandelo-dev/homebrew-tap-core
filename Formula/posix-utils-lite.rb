require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class PosixUtilsLite < Formula
  include KandeloFormulaSupport

  UTILITIES = %w[
    ar asa cal cflow compress ctags cxref ed ex fuser gencat getconf gettext
    iconv ipcrm ipcs lex locale logger man more msgfmt ngettext nm patch pax
    pgrep ps renice strings strip uncompress uudecode uuencode what xgettext
    yacc
  ].freeze

  desc "Compact POSIX utility set for Kandelo"
  homepage "https://github.com/Automattic/kandelo"
  url "https://github.com/Automattic/kandelo/archive/110619f8e7e5d51ec85b62f526176535462cd3bd.tar.gz"
  version "0.1.0"
  sha256 "c082b12eafdcb81e7162b79d22dc329bf1ba5184ce09b0d446c7419d51eb0009"
  license "GPL-2.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin"

  def install
    kandelo_require_arch!("wasm32")
    source_dir = kandelo_stage_verified_formula_source

    # Transitional Tier-2 bridge: keep the current 37-command multicall
    # recipe intact for the exact-shell proof. Splitting commands into their
    # maintained upstream Formulae remains explicit migration debt.
    out_dir = kandelo_build_package(
      "posix-utils-lite", "build-posix-utils-lite.sh", stable.url, stable.checksum.hexdigest,
      script_env: {
        "WASM_POSIX_DEP_SOURCE_DIR"       => source_dir,
        "WASM_POSIX_INSTALL_LOCAL_MIRROR" => "0",
      }
    )
    UTILITIES.each do |utility|
      kandelo_validate_wasm_artifact(out_dir/"#{utility}.wasm", fork: :forbidden)
    end

    kandelo_install_bin(out_dir, "ar.wasm", "ar")
    UTILITIES.drop(1).each { |utility| bin.install_symlink "ar" => utility }
  end

  test do
    UTILITIES.each { |utility| assert_path_exists bin/utility }
    assert_equal "C\nPOSIX\nC.UTF-8\n",
      kandelo_run_wasm(bin/"locale", ["-a"], preserve_argv0: true)
  end
end
