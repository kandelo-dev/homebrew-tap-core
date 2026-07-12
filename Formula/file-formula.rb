require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

# File is a reserved Ruby class name.
class FileFormula < Formula
  include KandeloFormulaSupport

  desc "Command-line file type identification tool for Kandelo"
  homepage "https://www.darwinsys.com/file/"
  url "https://astron.com/pub/file/file-5.45.tar.gz"
  sha256 "fc97f51029bb0e2c9f4e3bffefdaf678f0e039ee872b9de5c002a6d09c784d82"
  license "BSD-2-Clause-Darwin"

  depends_on "pkgconf" => :build
  depends_on "automattic/kandelo-homebrew/bzip2"
  depends_on "automattic/kandelo-homebrew/libmagic"
  depends_on "automattic/kandelo-homebrew/xz"
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "bin/file"

  def install
    kandelo_require_arch!("wasm32")
    bzip2 = formula_opt_prefix("automattic/kandelo-homebrew/bzip2")
    libmagic = formula_opt_prefix("automattic/kandelo-homebrew/libmagic")
    xz = formula_opt_prefix("automattic/kandelo-homebrew/xz")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    instrumented = buildpath/"src/file.instrumented"

    kandelo_wasm_build do |root|
      ENV["PKG_CONFIG_LIBDIR"] = [
        libmagic/"lib/pkgconfig",
        xz/"lib/pkgconfig",
        zlib/"lib/pkgconfig",
      ].join(":")
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")

      pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
      libmagic_version = Utils.safe_popen_read(pkgconf, "--modversion", "libmagic").strip
      odie "file #{version} requires libmagic #{version}, found #{libmagic_version}" if libmagic_version != version.to_s
      compile_flags = Utils.safe_popen_read(pkgconf, "--cflags", "libmagic").split
      link_flags = Utils.safe_popen_read(pkgconf, "--static", "--libs", "libmagic").split
      %W[-lmagic -llzma -L#{bzip2}/lib -lbz2 -lz -lm].each do |flag|
        odie "libmagic static interface omits #{flag}" unless link_flags.include?(flag)
      end

      ENV["CPPFLAGS"] = compile_flags.join(" ")
      system kandelo_configure, *kandelo_std_configure_args,
        "--disable-shared",
        "--enable-static",
        "--enable-fsect-man5",
        "--disable-zlib",
        "--disable-bzlib",
        "--disable-xzlib",
        "--disable-zstdlib",
        "--disable-lzlib",
        "--disable-libseccomp",
        "--disable-silent-rules"

      system "make", "-C", "src", "magic.h"

      # Upstream's file target hardcodes the sibling libmagic.la. Build only
      # the CLI and link it against the installed tap library contract.
      system "make", "-C", "src", "file",
        "MAGIC=#{libmagic}/share/misc/magic",
        "file_DEPENDENCIES=",
        "file_LDADD=#{link_flags.join(" ")}"
      system "#{root}/scripts/run-wasm-fork-instrument.sh",
        buildpath/"src/file", "-o", instrumented

      # The CLI owns file(1), with the installed libmagic opt path documented
      # as its default database. libmagic owns the library manuals.
      system "make", "-C", "doc", "file.1",
        "MAGIC=#{libmagic}/share/misc/magic"
    end

    kandelo_install_bin(buildpath/"src", "file.instrumented", "file")
    man1.install buildpath/"doc/file.1"
  end

  test do
    assert_path_exists man1/"file.1"

    workspace = testpath/"workspace"
    workspace.mkpath
    (workspace/"module.wasm").binwrite("\0asm\x01\0\0\0")
    (workspace/"document.pdf").write("%PDF-1.7\n")
    (workspace/"plain.txt").write("Kandelo file utility test payload.\n")
    (workspace/"plain-link").make_symlink("plain.txt")

    # These are the same ASCII payload encoded as zlib, bzip2, xz, and gzip streams.
    (workspace/"payload.z").binwrite([
      "789cf34ecc4b49cdc95748cbcc4955282dc9ccc92ca95448cecf2d284a2d2e" \
      "4e4d512848acccc94f4cd1e3020047220f4a",
    ].pack("H*"))
    (workspace/"payload.bz2").binwrite([
      "425a6839314159265359ee7e1d9a00000355800010400100082f27de20200022" \
      "80d190da9885309a680d312b0ba3a62d91a230ac8d90e4d240bcc498dc29c6" \
      "cb89f177245385090ee7e1d9a0",
    ].pack("H*"))
    (workspace/"payload.xz").binwrite([
      "fd377a585a000004e6d6b44604c02d29210116000000000000000000727c299" \
      "a0100284b616e64656c6f2066696c65207574696c69747920636f6d70726573" \
      "736564207061796c6f61642e0a00000000a6f1317d726e0bb9000149290bd9" \
      "8f431fb6f37d010000000004595a",
    ].pack("H*"))
    (workspace/"payload.gz").binwrite([
      "1f8b0800000000000003f34ecc4b49cdc95748cbcc4955282dc9ccc92ca9544" \
      "8cecf2d284a2d2e4e4d512848acccc94f4cd1e3020006c327d729000000",
    ].pack("H*"))

    env = { "KERNEL_CWD" => workspace }
    version_output = kandelo_run_wasm(bin/"file", ["--version"])
    assert_match(/^file(?:\.wasm)?-5\.45$/, version_output)
    assert_match(%r{magic file from .*/opt/libmagic/share/misc/magic$}, version_output)

    assert_match(/^WebAssembly \(wasm\) binary module/,
      kandelo_run_wasm(bin/"file", ["-b", "module.wasm"], env: env))
    assert_equal "application/pdf\n",
      kandelo_run_wasm(bin/"file", ["-b", "--mime-type", "document.pdf"], env: env)
    assert_match(/^PDF document/,
      kandelo_run_wasm(bin/"file", ["-b", "-"], stdin: "%PDF-1.7\n"))

    assert_equal "symbolic link to plain.txt\n",
      kandelo_run_wasm(bin/"file", ["-b", "plain-link"], env: env)
    assert_match(/^ASCII text/,
      kandelo_run_wasm(bin/"file", ["-L", "-b", "plain-link"], env: env))

    {
      "payload.z"   => "zlib compressed data",
      "payload.bz2" => "bzip2 compressed data",
      "payload.xz"  => "XZ compressed data",
      "payload.gz"  => "gzip compressed data",
    }.each do |path, compression|
      output = kandelo_run_wasm(bin/"file", ["-z", "-b", path], env: env)
      assert_match(/^ASCII text \(#{Regexp.escape(compression)}/, output)
    end
  end
end
