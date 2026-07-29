require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class HomebrewBootstrap < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true
  # WHY: this bottle contains the Homebrew Ruby tree, not a Wasm executable.
  # Its Formula test proves the installed bytes; the separate Node and browser
  # guest lifecycle proves that real Ruby can execute those bytes in Kandelo.
  KANDELO_BOTTLE_TEST_CONTRACT = "support-data".freeze

  BOOTSTRAP_ARCHIVE = "homebrew-bootstrap.zip".freeze
  ENVIRONMENT_POLICY = "homebrew-brew.env".freeze

  desc "Patched Homebrew runtime source tree for Kandelo"
  homepage "https://brew.sh/"
  url "https://github.com/Homebrew/brew/archive/4ead8619231cb15cbe15e8e8188081e347d6f7cd.tar.gz"
  version "6.0.3-4-g4ead861"
  sha256 "4b9fdfb4872bd2fbff001c69f91ec7b2c2b7a956459132b6c3adba878f551155"
  license all_of: ["BSD-2-Clause", "GPL-2.0-or-later"]

  depends_on "git" => :build
  depends_on "ruby" => :build
  depends_on "unzip" => [:build, :test]
  depends_on "zip" => :build

  def install
    # WHY: this bottle's guest policy selects the wasm32_kandelo bottle tag.
    # A future wasm64 bootstrap needs its own policy and independently verified
    # archive rather than reusing bytes that would select the wrong tag.
    kandelo_require_arch!("wasm32")

    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "c56e5b08d3f581c47501193d831bd659f319112d6ff3a726430aed634783bbd6",
      script_env:      {
        # WHY: the sealed recipe root does not expose Homebrew's own portable
        # Ruby. Bind this declared native keg explicitly so the lock verifier
        # cannot fall through to a guest Ruby or an unprojected host path.
        "HOMEBREW_BOOTSTRAP_RUBY" => formula_opt_bin("ruby")/"ruby",
      },
    )
    libexec.install out_dir/BOOTSTRAP_ARCHIVE
    libexec.install out_dir/ENVIRONMENT_POLICY
  end

  test do
    archive = libexec/BOOTSTRAP_ARCHIVE
    environment = libexec/ENVIRONMENT_POLICY
    assert_path_exists archive
    assert_path_exists environment
    assert_equal "68095c118c8bb99245124fe8558f9211bf6d22e2331852ed27541bd34b725eb9",
      Digest::SHA256.file(archive).hexdigest
    assert_equal 5_026_784, archive.size
    assert_equal <<~ENVIRONMENT, environment.read
      HOMEBREW_NO_ANALYTICS=1
      HOMEBREW_NO_AUTO_UPDATE=1
      HOMEBREW_NO_INSTALL_FROM_API=1
      HOMEBREW_AUTOMATICALLY_SET_NO_INSTALL_FROM_API=1
      HOMEBREW_SYSTEM_ENV_TAKES_PRIORITY=1
      HOMEBREW_KANDELO_BOTTLE_TAG=wasm32_kandelo
    ENVIRONMENT

    extracted = testpath/"homebrew"
    extracted.mkpath
    system formula_opt_bin("unzip")/"unzip", "-q", archive, "-d", extracted
    assert_predicate extracted/"bin/brew", :executable?
    assert_path_exists extracted/"LICENSE.txt"
    assert_includes (extracted/"Library/Homebrew/utils/bottles.rb").read,
      "HOMEBREW_KANDELO_BOTTLE_TAG"
    assert_includes (extracted/"Library/Homebrew/github_packages.rb").read,
      "Retain its `homebrew-` prefix"
  end
end
