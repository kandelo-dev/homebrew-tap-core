require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Unzip < Formula
  include KandeloFormulaSupport

  desc "Extraction utility for ZIP archives on Kandelo"
  homepage "https://infozip.sourceforge.net/UnZip.html"
  url "https://downloads.sourceforge.net/project/infozip/UnZip%206.x%20%28latest%29/UnZip%206.0/unzip60.tar.gz"
  version "6.0"
  sha256 "036d96991646d0449ed0aa952e4fbe21b476ce994abc276e49d30e686708bd37"
  license "Info-ZIP"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/unzip", "bin/funzip", "bin/unzipsfx", "bin/zipinfo"

  # Upstream is unmaintained. Follow Homebrew's maintained formula and apply
  # Ubuntu's complete security, correctness, and reproducibility quilt series.
  patch do
    url "https://archive.ubuntu.com/ubuntu/pool/main/u/unzip/unzip_6.0-28ubuntu4.1.debian.tar.xz"
    sha256 "d123c8e6972dbdd17ba1a4920fb57ed2ede9237dbae149dcbf55df829c77baf3"
    apply %w[
      patches/01-manpages-in-section-1-not-in-section-1l.patch
      patches/02-this-is-debian-unzip.patch
      patches/03-include-unistd-for-kfreebsd.patch
      patches/04-handle-pkware-verification-bit.patch
      patches/05-fix-uid-gid-handling.patch
      patches/06-initialize-the-symlink-flag.patch
      patches/07-increase-size-of-cfactorstr.patch
      patches/08-allow-greater-hostver-values.patch
      patches/09-cve-2014-8139-crc-overflow.patch
      patches/10-cve-2014-8140-test-compr-eb.patch
      patches/11-cve-2014-8141-getzip64data.patch
      patches/12-cve-2014-9636-test-compr-eb.patch
      patches/13-remove-build-date.patch
      patches/14-cve-2015-7696.patch
      patches/15-cve-2015-7697.patch
      patches/16-fix-integer-underflow-csiz-decrypted.patch
      patches/17-restore-unix-timestamps-accurately.patch
      patches/18-cve-2014-9913-unzip-buffer-overflow.patch
      patches/19-cve-2016-9844-zipinfo-buffer-overflow.patch
      patches/20-unzip60-alt-iconv-utf8.patch
      patches/20-cve-2018-1000035-unzip-buffer-overflow.patch
      patches/21-fix-warning-messages-on-big-files.patch
      patches/22-cve-2019-13232-fix-bug-in-undefer-input.patch
      patches/23-cve-2019-13232-zip-bomb-with-overlapped-entries.patch
      patches/24-cve-2019-13232-do-not-raise-alert-for-misplaced-central-directory.patch
      patches/25-cve-2019-13232-fix-bug-in-uzbunzip2.patch
      patches/26-cve-2019-13232-fix-bug-in-uzinflate.patch
      patches/27-zipgrep-avoid-test-errors.patch
      patches/28-cve-2022-0529-and-cve-2022-0530.patch
      patches/handle_windows_zip64.patch
      patches/29-fix-troff-warning.patch
      patches/CVE-2021-4217.patch
    ]
  end

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      cflags = %w[
        -O2
        -Wall
        -I.
        -DUNIX
        -DSYSV
        -DMODERN
        -Dlinux
        -DHAVE_UNISTD_H
        -DHAVE_DIRENT_H
        -DHAVE_TERMIOS_H
        -DACORN_FTYPE_NFS
        -DWILD_STOP_AT_DIR
        -DLARGE_FILE_SUPPORT
        -DUNICODE_SUPPORT
        -DUNICODE_WCHAR
        -DUTF8_MAYBE_NATIVE
        -DNO_WORKING_ISPRINT
        -DNO_LCHMOD
        -DDATE_FORMAT=DF_YMD
        -DIZ_HAVE_STRDUP
        -DIZ_HAVE_STRCASECMP
      ]
      system "make", "-f", "unix/Makefile",
        "CC=#{kandelo_cc}",
        "CF=#{cflags.join(" ")}",
        "LF2=",
        "unzips"
      %w[unzip funzip unzipsfx].each do |program|
        kandelo_validate_wasm_artifact(buildpath/program, fork: :forbidden)
      end
      system "make", "-f", "unix/Makefile",
        "BINDIR=#{bin}",
        "MANDIR=#{man1}",
        "install"
    end

    # zipgrep relies on Kandelo's POSIX base shell, egrep, sed, and basename;
    # same-keg unzip is its only non-base command.
    File.open(man1/"unzipsfx.1", "a") do |manual|
      manual.write <<~MANPAGE
        .SH KANDELO WASM PACKAGING
        A Kandelo self-extractor must remain a valid WebAssembly module, so a ZIP archive
        cannot be concatenated directly to unzipsfx.  Use the Kandelo SDK toolchain to
        embed the archive in a custom section:
        .PP
        .nf
        llvm-objcopy --add-section kandelo.sfx=archive.zip unzipsfx.wasm output.wasm
        .fi
      MANPAGE
    end
  end

  test do
    archive = testpath/"fixture.zip"
    archive.binwrite(
      "UEsDBBQAAAAIAAAAIVxY+qxoIAAAAMAEAAAJAAAAYWxwaGEudHh0S8wpyEhUSCvKz1XwTsxLSc3J50ocFRoV" \
      "GhUaFRoKQgBQSwMEFAAAAAgAAAAhXJsvleEdAAAAYAMAAA8AAABuZXN0ZWQvYmV0YS50eHRLSi1JVEgrys9V" \
      "8E7MS0nNyedKGhUZFRkVoZIIAFBLAQIeAxQAAAAIAAAAIVxY+qxoIAAAAMAEAAAJAAAAAAAAAAEAAACkgQAA" \
      "AABhbHBoYS50eHRQSwECHgMUAAAACAAAACFcmy+V4R0AAABgAwAADwAAAAAAAAABAAAApIFHAAAAbmVzdGVk" \
      "L2JldGEudHh0UEsFBgAAAAACAAIAdAAAAJEAAAAAAA==".unpack1("m0"),
    )
    cwd_env = { "KERNEL_CWD" => testpath }

    listing = kandelo_run_wasm(bin/"unzip", ["-l", "fixture.zip"], env: cwd_env)
    assert_match(/alpha\.txt/, listing)
    assert_match(%r{nested/beta\.txt}, listing)
    assert_match(/2 files/, listing)

    extracted = testpath/"extracted"
    extracted.mkpath
    assert_empty kandelo_run_wasm(
      bin/"unzip", ["-q", "fixture.zip", "-d", "extracted"], env: cwd_env
    )
    assert_equal "alpha from Kandelo\n" * 64, (extracted/"alpha.txt").read
    assert_equal "beta from Kandelo\n" * 48, (extracted/"nested/beta.txt").read

    assert_equal "alpha.txt\nnested/beta.txt\n",
      kandelo_run_wasm(bin/"unzip", ["-Z", "-1", "fixture.zip"], env: cwd_env)
    assert_match(/^ZipInfo 3\.00/, kandelo_run_wasm(bin/"zipinfo", ["-h"], preserve_argv0: true))
    assert_match(/^UnZipSFX 6\.00/, kandelo_run_wasm(bin/"unzipsfx", ["-h"]))
    assert_predicate bin/"zipgrep", :executable?

    guest_unzip_bin = "/home/linuxbrew/.linuxbrew/opt/unzip/bin"
    dash = kandelo_resolve_binary("programs/dash.wasm")
    exec_programs = {
      "/bin/sh"                    => dash,
      "/usr/bin/egrep"             => kandelo_resolve_binary("programs/grep.wasm"),
      "/usr/bin/sed"               => kandelo_resolve_binary("programs/sed.wasm"),
      "/usr/bin/basename"          => kandelo_resolve_binary("programs/coreutils.wasm"),
      "#{guest_unzip_bin}/unzip"   => bin/"unzip",
      "#{guest_unzip_bin}/zipgrep" => bin/"zipgrep",
    }
    zipgrep_env = {
      "KERNEL_CWD" => "/work",
      "PATH"       => "#{guest_unzip_bin}:/usr/bin:/bin",
    }
    zipgrep_files = { "/work/fixture.zip" => archive }
    zipgrep_command = "exec #{guest_unzip_bin}/zipgrep"

    assert_equal "alpha.txt\nnested/beta.txt\n", kandelo_run_wasm(
      dash,
      ["-c", "#{zipgrep_command} -l Kandelo fixture.zip"],
      argv0:         "/bin/sh",
      env:           zipgrep_env,
      exec_programs: exec_programs,
      guest_files:   zipgrep_files,
    )
    usage = kandelo_run_wasm(
      dash,
      ["-c", zipgrep_command],
      argv0:           "/bin/sh",
      env:             zipgrep_env,
      exec_programs:   exec_programs,
      expected_status: 1,
      guest_files:     zipgrep_files,
    )
    assert_match(/^usage: zipgrep /, usage)
    %w[funzip unzip unzipsfx zipgrep zipinfo].each do |program|
      assert_path_exists man1/"#{program}.1"
    end
    assert_includes (man1/"unzipsfx.1").read, "llvm-objcopy --add-section kandelo.sfx="

    encode_uleb = lambda do |value|
      encoded = +"".b
      loop do
        byte = value & 0x7f
        value >>= 7
        byte |= 0x80 unless value.zero?
        encoded << byte
        break if value.zero?
      end
      encoded
    end
    section_name = "kandelo.sfx".b
    section_payload = encode_uleb.call(section_name.bytesize) + section_name + archive.binread
    self_extractor = testpath/"fixture-sfx.wasm"
    self_extractor.binwrite(
      (bin/"unzipsfx").binread + "\0".b + encode_uleb.call(section_payload.bytesize) + section_payload,
    )
    self_extractor.chmod 0755
    guest_sfx = "/usr/local/bin/fixture-sfx.wasm"
    sfx_program = { guest_sfx => self_extractor }
    sfx_listing = kandelo_run_wasm(
      self_extractor, ["-t"], argv0: guest_sfx, exec_programs: sfx_program
    )
    assert_match(/testing: alpha\.txt/, sfx_listing)
    assert_match(%r{testing: nested/beta\.txt}, sfx_listing)

    sfx_extracted = testpath/"sfx-extracted"
    sfx_extracted.mkpath
    sfx_output = kandelo_run_wasm(
      self_extractor,
      ["-q", "-d", "/work"],
      argv0:                     guest_sfx,
      exec_programs:             sfx_program,
      writable_host_directories: { "/work" => sfx_extracted },
    )
    assert_match(/^UnZipSFX 6\.00 /, sfx_output)
    assert_equal "alpha from Kandelo\n" * 64, (sfx_extracted/"alpha.txt").read
    assert_equal "beta from Kandelo\n" * 48, (sfx_extracted/"nested/beta.txt").read

    funzip_archive =
      "UEsDBBQAAAAIAAAAIVxY+qxoIAAAAMAEAAAJAAAAYWxwaGEudHh0S8wpyEhUSCvKz1XwTsxLSc3J50ocFRoV" \
      "GhUaFRoKQgBQSwECHgMUAAAACAAAACFcWPqsaCAAAADABAAACQAAAAAAAAABAAAApIEAAAAAYWxwaGEudHh0" \
      "UEsFBgAAAAABAAEANwAAAEcAAAAAAA==".unpack1("m0")
    assert_equal "alpha from Kandelo\n" * 64,
      kandelo_run_wasm(bin/"funzip", [], stdin: funzip_archive, preserve_argv0: true)

    missing = kandelo_run_wasm(
      bin/"unzip", ["missing.zip"], env: cwd_env, merge_stderr: true, expected_status: 9
    )
    assert_match(/cannot find or open missing\.zip/, missing)
  end
end
