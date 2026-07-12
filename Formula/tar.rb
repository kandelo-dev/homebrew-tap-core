require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Tar < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/tar".freeze

  desc "GNU archiving utility for Kandelo"
  homepage "https://www.gnu.org/software/tar/"
  url "https://ftpmirror.gnu.org/gnu/tar/tar-1.35.tar.xz"
  mirror "https://ftp.gnu.org/gnu/tar/tar-1.35.tar.xz"
  sha256 "4d62ff37342ec7aed748535323930c7cf94acf71c3591882b26a7ea50f3edc16"
  license "GPL-3.0-or-later"

  depends_on "automattic/kandelo-homebrew/gzip"

  skip_clean "bin/tar", "libexec/rmt"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      ENV["gl_cv_func_strerror_0_works"] = "yes"
      ENV["DEFAULT_RMT_DIR"] = "#{GUEST_OPT_PREFIX}/libexec"

      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-nls",
        "--without-selinux",
        "--without-posix-acls",
        "--with-xattrs=no"
      system "make"
      kandelo_fork_instrument(buildpath/"src/tar")
    end

    kandelo_install_bin(buildpath/"src", "tar", "tar")
    rmt = buildpath/"rmt/rmt"
    chmod 0755, rmt
    libexec.install rmt
  end

  test do
    source = testpath/"source"
    extracted = testpath/"extracted"
    gzip_extracted = testpath/"gzip-extracted"
    source.mkpath
    extracted.mkpath
    gzip_extracted.mkpath
    (source/"alpha.txt").write "alpha\n"
    (source/"nested").mkpath
    (source/"nested/beta.txt").write "beta\n"

    gzip = testpath/"gzip"
    gzip.binwrite((formula_opt_bin("automattic/kandelo-homebrew/gzip")/"gzip").binread)
    gzip.chmod 0755
    env = { "KERNEL_CWD" => testpath, "KERNEL_PATH" => testpath }

    tar_binary = (bin/"tar").binread
    assert_includes tar_binary, "#{GUEST_OPT_PREFIX}/libexec/rmt"
    refute_includes tar_binary, prefix.to_s
    assert_match(/^rmt \(GNU tar\) 1\.35$/,
      kandelo_run_wasm(libexec/"rmt", ["--version"]).lines.first.chomp)

    kandelo_run_wasm(bin/"tar", ["-cf", "archive.tar", "source"], env: env)
    listing = kandelo_run_wasm(bin/"tar", ["-tf", "archive.tar"], env: env)
    expected_entries = [
      "source/",
      "source/alpha.txt",
      "source/nested/",
      "source/nested/beta.txt",
    ]
    assert_equal expected_entries.sort, listing.lines.map(&:chomp).sort

    kandelo_run_wasm(bin/"tar", ["-xf", "archive.tar", "-C", "extracted"], env: env)
    assert_equal "alpha\n", (extracted/"source/alpha.txt").read
    assert_equal "beta\n", (extracted/"source/nested/beta.txt").read

    kandelo_run_wasm(bin/"tar", ["-czf", "archive.tar.gz", "source"], env: env)
    assert_equal [0x1f, 0x8b], (testpath/"archive.tar.gz").binread(2).bytes
    gzip_listing = kandelo_run_wasm(bin/"tar", ["-tzf", "archive.tar.gz"], env: env)
    assert_equal expected_entries.sort, gzip_listing.lines.map(&:chomp).sort

    kandelo_run_wasm(bin/"tar", ["-xzf", "archive.tar.gz", "-C", "gzip-extracted"], env: env)
    assert_equal "alpha\n", (gzip_extracted/"source/alpha.txt").read
    assert_equal "beta\n", (gzip_extracted/"source/nested/beta.txt").read
  end
end
