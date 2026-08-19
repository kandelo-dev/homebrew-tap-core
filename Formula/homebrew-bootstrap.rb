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
  url "https://github.com/Homebrew/brew/archive/cf5bc21c6b127e168ef7cfa982ba7db62874690e.tar.gz"
  version "6.0.12-153-gcf5bc21"
  sha256 "18d3c5384b1a90e0dca3c044b31d8a2b61b500bc5b880a14b1e52a590088de40"
  license all_of: ["BSD-2-Clause", "GPL-2.0-or-later"]

  # WHY: C2 published revision-zero OCI bytes before its final handoff failed.
  # Reserve a new Homebrew identity so recovery never overwrites or relabels
  # those public bytes.
  revision 1

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
      manifest_sha256: "fea6b73e01b5344f45bce8c60cc96d85380d42c807bbab12446d4533aa396b96",
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
    assert_equal "26ac98e328573244d3e7c0c149f30114ef5d9c8882200f5a22e56f97d2541482",
      Digest::SHA256.file(archive).hexdigest
    assert_equal 5_251_369, archive.size
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
    assert_includes (extracted/"Library/Homebrew/utils/bottles.rb").read,
      KANDELO_GUEST_HOMEBREW_PREFIX
    assert_includes (extracted/"Library/Homebrew/github_packages.rb").read,
      "Retain its `homebrew-` prefix"
    # WHY: Kandelo's Ruby maps this standard API to non-forking posix_spawn.
    # A bootstrap older than this upstream Homebrew change would clone the
    # long-lived Ruby address space for every external command.
    assert_includes (extracted/"Library/Homebrew/system_command.rb").read,
      "Process.spawn"
  end


  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core-abi-43/homebrew-bootstrap"
    sha256 cellar: "/opt/kandelo/homebrew/Cellar", wasm32_kandelo: "ab56ec75fce858dd049236963e26b2e0fde1e140b844197950a6825a18968500"
  end
end
