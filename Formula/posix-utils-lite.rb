require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class PosixUtilsLite < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true

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

    # Keep the current tap-owned 37-command multicall recipe intact. Splitting
    # commands into their maintained upstream Formulae remains migration debt.
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "6f490cb52cef4ffc3e905fd38b635d746a2513ca27d9ae33d491cec413c79ee7",
      script_env:      {},
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


  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core-abi-43/posix-utils-lite"
    rebuild 3
    sha256 cellar: "/opt/kandelo/homebrew/Cellar", wasm32_kandelo: "58386197e0ef265d6280fe554e255d5b4832d3efd19d02b0598045c4308693fa"
  end
end
