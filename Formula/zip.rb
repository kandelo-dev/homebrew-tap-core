require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Zip < Formula
  include KandeloFormulaSupport

  desc "Compression and archive utility for Kandelo"
  homepage "https://infozip.sourceforge.net/Zip.html"
  url "https://downloads.sourceforge.net/project/infozip/Zip%203.x%20%28latest%29/3.0/zip30.tar.gz"
  version "3.0"
  sha256 "f0e8bb1f9b7eb0b01285495a2699df3a4b766784c1765a8f1aeedf63c0806369"
  license "Info-ZIP"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build
  depends_on "kandelo-dev/tap-core/unzip"

  skip_clean "bin/zip", "bin/zipcloak", "bin/zipnote", "bin/zipsplit"

  # Upstream is unmaintained. Follow Homebrew's maintained formula and apply
  # Debian's security and reproducibility fixes at this upstream boundary.
  patch do
    url "https://deb.debian.org/debian/pool/main/z/zip/zip_3.0-15.debian.tar.xz"
    sha256 "6dc1711c67640e8d1dee867ff53e84387ddb980c40885bd088ac98c330bffce9"
    type :unofficial
    apply %w[
      patches/01-typo-it-is-transferring-not-transfering.patch
      patches/02-typo-it-is-privileges-not-priviliges.patch
      patches/03-manpages-in-section-1-not-in-section-1l.patch
      patches/04-do-not-set-unwanted-cflags.patch
      patches/05-typo-it-is-preceding-not-preceeding.patch
      patches/06-stack-markings-to-avoid-executable-stack.patch
      patches/07-fclose-in-file-not-fclose-x.patch
      patches/08-hardening-build-fix-1.patch
      patches/09-hardening-build-fix-2.patch
      patches/10-remove-build-date.patch
      patches/11-typo-it-is-ambiguities-not-amgibuities.patch
      patches/13-typo-it-is-os-2-not-risc-os-2.patch
      patches/14-buffer-overflow-unicode-filename.patch
      patches/15-buffer-overflow-cve-2018-13410.patch
      patches/16-fix-symlink-update-detection.patch
    ]
  end

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      system "make", "-f", "unix/Makefile",
        "CC=#{kandelo_cc}",
        "CPP=#{kandelo_cc} -E",
        "CFLAGS=-I. -DUNIX -O2 -DUIDGID_NOT_16BIT -DHAVE_DIRENT_H -DHAVE_TERMIOS_H " \
        "-DLARGE_FILE_SUPPORT",
        "OBJA=",
        "OCRCU8=crc32_.o ",
        "OCRCTB=",
        "LFLAGS1=",
        "LFLAGS2=",
        "LN=ln -s",
        "IZ_BZIP2=",
        "LIB_BZ=",
        "zips"
      %w[zip zipcloak zipnote zipsplit].each do |program|
        kandelo_validate_wasm_artifact(buildpath/program, fork: :forbidden)
      end
      system "make", "-f", "unix/Makefile",
        "BINDIR=#{bin}",
        "MANDIR=#{man1}",
        "install"
    end
  end

  test do
    inputs = testpath/"inputs"
    (inputs/"nested").mkpath
    (inputs/"alpha.txt").write("alpha from Kandelo\n")
    (inputs/"nested/beta.txt").write("beta from Kandelo\n")
    unzip = inputs/"unzip"
    unzip.binwrite((formula_opt_bin("kandelo-dev/tap-core/unzip")/"unzip").binread)
    unzip.chmod 0755
    cwd_env = { "KERNEL_CWD" => inputs, "KERNEL_PATH" => inputs }

    assert_empty kandelo_run_wasm(
      bin/"zip", ["-q", "archive.zip", "alpha.txt", "nested/beta.txt"], env: cwd_env
    )
    assert_path_exists inputs/"archive.zip"

    listing = kandelo_run_wasm(bin/"zip", ["-sf", "archive.zip"], env: cwd_env)
    assert_match(/^  alpha\.txt$/, listing)
    assert_match(%r{^  nested/beta\.txt$}, listing)
    assert_match(/Total 2 entries/, listing)

    integrity = kandelo_run_wasm(bin/"zip", ["-T", "archive.zip"], env: cwd_env)
    assert_match(/test of archive\.zip OK/, integrity)

    zipnote = kandelo_run_wasm(bin/"zipnote", ["archive.zip"], env: cwd_env)
    assert_match(/^@ alpha\.txt$/, zipnote)
    assert_match(%r{^@ nested/beta\.txt$}, zipnote)
    assert_match(/ZipCloak 3\.0/, kandelo_run_wasm(bin/"zipcloak", ["-h"]))
    assert_match(/ZipSplit 3\.0/, kandelo_run_wasm(bin/"zipsplit", ["-h"]))
    assert_match(/1 zip files would be made/,
      kandelo_run_wasm(bin/"zipsplit", ["-t", "archive.zip"], env: cwd_env))

    %w[zip zipcloak zipnote zipsplit].each do |program|
      assert_path_exists man1/"#{program}.1"
    end

    nothing_to_do = kandelo_run_wasm(
      bin/"zip", ["empty.zip"], env: cwd_env, merge_stderr: true, expected_status: 12
    )
    assert_match(/Nothing to do/, nothing_to_do)
    refute_path_exists inputs/"empty.zip"
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 1
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "c28a0fb31eba5991fd3a3ee0e000a163c319d7a42a8f7c305fc4c1d4371ff65c"
  end

end
