require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class PosixUtilsLite < Formula
  include KandeloFormulaSupport

  KANDELO_REGISTRY_BRIDGE = true

  UTILITIES = %w[
    ar asa cal cflow compress ctags cxref ed ex fuser gencat getconf gettext
    iconv ipcrm ipcs lex locale logger man more msgfmt ngettext nm patch pax
    pgrep ps renice strings strip uncompress uudecode uuencode what xgettext
    yacc
  ].freeze

  desc "Compact POSIX utility set for Kandelo"
  homepage "https://github.com/Automattic/kandelo"
  url "https://github.com/Automattic/kandelo/archive/1a83af5de608c10f485082c6ef0efa845f747436.tar.gz"
  version "0.1.0"
  sha256 "07e7a7ebff8003114f6b4bef1ccdc2e9b15ecfbd5e6ccc3bf8563107b8151fde"
  license "GPL-2.0-or-later"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin"

  def install
    kandelo_require_arch!("wasm32")

    # Transitional Tier-2 bridge: keep the current 37-command multicall
    # recipe intact for the exact-shell proof. Splitting commands into their
    # maintained upstream Formulae remains explicit migration debt.
    out_dir = kandelo_build_package(script_env: {})
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

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 2
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "00f5d975d41a5af97c7534b46f08c40bfc296c45a385156a36b4bace4a01c345"
  end

end
