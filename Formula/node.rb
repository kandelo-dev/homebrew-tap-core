require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Node < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true
  RUNTIME_OUTPUTS = { "node.wasm" => "bin/node" }.freeze

  desc "Node-compatible JavaScript runtime for Kandelo"
  homepage "https://firefox-source-docs.mozilla.org/js/"
  url "https://ftp.mozilla.org/pub/firefox/releases/140.11.0esr/source/firefox-140.11.0esr.source.tar.xz"
  version "140.11.0esr"
  sha256 "1b034d2117356fda24807a151055132315c6ba58ad2bdf7ec71ee707fac5e028"
  license "MPL-2.0"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  depends_on "kandelo-dev/tap-core/libcxx"
  depends_on "kandelo-dev/tap-core/openssl"
  depends_on "kandelo-dev/tap-core/zlib"

  skip_clean "bin/node"

  def install
    kandelo_require_arch!("wasm32")
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "22b396bf81b6126c99b31c5b0cf86a240f92d0f113652b60d7bb3b9f0057af6d",
      script_env:      {},
    )
    kandelo_validate_wasm_artifact(out_dir/"node.wasm", fork: :forbidden)
    RUNTIME_OUTPUTS.each do |source_name, relative|
      destination = prefix/relative
      destination.dirname.install out_dir/source_name => destination.basename
      chmod 0755, destination
    end
  end

  test do
    assert_path_exists bin/"node"
    output = kandelo_run_wasm(bin/"node", ["-e", "print('node-ok')"])
    assert_equal "node-ok", output.strip
  end
end
