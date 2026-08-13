require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class KandeloSdk < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true
  KANDELO_SDK_RECIPE_MANIFEST_SHA256 = "a783a300183a644e5f99dbb23cccb7beb08873460f47b49fc334a3dd80d7ad3a".freeze

  desc "C and C++ development kit for Kandelo"
  homepage "https://github.com/Automattic/kandelo"
  url "https://github.com/Automattic/kandelo/archive/6d34c6d5183920c97454994bcf4fec060ee2f8d7.tar.gz"
  version "0.1.0"
  sha256 "3d9f18dcefb73819b7b158fda1662bb8b17cad8865e4d281915d8a22e757c588"
  license all_of: [
    "GPL-2.0-or-later",
    "MIT",
  ]

  depends_on KandeloFormulaSupport::WabtRequirement => :test
  depends_on "kandelo-dev/tap-core/clang"
  depends_on "kandelo-dev/tap-core/libcxx"

  def install
    kandelo_require_arch!("wasm32")
    out = kandelo_build_tap_recipe(
      manifest_sha256: KANDELO_SDK_RECIPE_MANIFEST_SHA256,
      script_env:      {},
    )
    bin.install Dir[out/"bin/*"]
    libexec.install out/"wasm32posix"
    (share/"kandelo-sdk").install Dir[out/"share/kandelo-sdk/*"]
    %w[cc c++ ar ranlib nm].each do |name|
      target = "wasm32posix-#{name}"
      bin.install_symlink target => name
    end
  end

  test do
    %w[
      wasm32posix-cc wasm32posix-c++ wasm32posix-ar
      wasm32posix-ranlib wasm32posix-nm cc c++ ar ranlib nm
    ].each { |name| assert_path_exists bin/name }
    assert_path_exists libexec/"wasm32posix/sysroot/lib/libc.a"
    assert_path_exists libexec/"wasm32posix/glue/channel_syscall.c"
    assert_path_exists libexec/"wasm32posix/glue-objects/channel_syscall.o"
    assert_path_exists share/"kandelo-sdk/examples/hello.c"
    assert_path_exists share/"kandelo-sdk/licenses/KANDELO-GPL-2.0"
    assert_path_exists share/"kandelo-sdk/licenses/KANDELO-RUNTIME-MIT"
    assert_path_exists share/"kandelo-sdk/licenses/KANDELO-LICENSING"
    assert_path_exists share/"kandelo-sdk/licenses/MUSL-COPYRIGHT"
    wrappers = (bin/"wasm32posix-cc").read + (bin/"wasm32posix-c++").read
    refute_match(%r{/Users/|/private/tmp/|/home/runner/|/nix/store/}, wrappers)
    refute_path_exists libexec/"wasm32posix/sysroot/include/c++"
    refute_path_exists libexec/"wasm32posix/sysroot/lib/libc++.a"
    refute_path_exists libexec/"wasm32posix/sysroot/lib/libc++abi.a"
  end
end
