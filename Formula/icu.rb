require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s
require "digest"

class Icu < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/icu".freeze
  GUEST_DATA_DIR = "#{GUEST_OPT_PREFIX}/share/icu/74.2".freeze
  ICU_DATA_SHA256 = "dc778b9ffe18ed319ad3fb70754f80e51cf7b6dbfff38fc0c0a5f27bb5463dad".freeze
  ICU_DATA_BYTES = 30_782_896

  desc "Unicode and globalization libraries and data for Kandelo"
  homepage "https://icu.unicode.org/"
  url "https://github.com/unicode-org/icu/releases/download/release-74-2/icu4c-74_2-src.tgz"
  version "74.2"
  sha256 "68db082212a96d6f53e35d60f47d38b962e9f9d207a74cfac78029ae8ff5e08c"
  license "ICU"

  keg_only "its Kandelo target headers and libraries conflict with native ICU"

  depends_on "pkgconf" => [:build, :test]
  depends_on "binaryen" => :test
  depends_on "wabt" => :test
  depends_on "kandelo-dev/tap-core/libcxx"

  skip_clean "lib/libicudata.a"
  skip_clean "lib/libicui18n.a"
  skip_clean "lib/libicuio.a"
  skip_clean "lib/libicuuc.a"

  def install
    kandelo_require_arch!("wasm32")
    libcxx = formula_opt_prefix("kandelo-dev/tap-core/libcxx")
    source = buildpath/"source"
    host_build = buildpath/"host-build"

    odie "ICU source tree is missing source/runConfigureICU" unless (source/"runConfigureICU").executable?

    # ICU's target build consumes native data generators. Run their complete
    # configure/build phase once inside Kandelo's declared dev shell so the
    # target libcxx dependency cannot redirect host C++ compilation.
    system kandelo_host_tool("bash"), "-c", <<~SH, "icu-host-build", source, host_build, ENV.make_jobs
      set -euo pipefail
      source_dir=$1
      host_build=$2
      jobs=$3
      case "$(uname -s)" in
        Darwin)
          platform=MacOSX
          ;;
        Linux)
          platform=Linux
          ;;
        *)
          echo "unsupported ICU host platform: $(uname -s)" >&2
          exit 1
          ;;
      esac
      host_cxxflags="-nostdinc++ -isystem $LLVM_PREFIX/include/c++/v1 -stdlib=libc++"
      host_ldflags="-L$LLVM_PREFIX/lib -Wl,-rpath,$LLVM_PREFIX/lib"
      mkdir -p "$host_build"
      cd "$host_build"
      CC="$LLVM_PREFIX/bin/clang" CXX="$LLVM_PREFIX/bin/clang++" \
        CXXFLAGS="$host_cxxflags" LDFLAGS="$host_ldflags" \
        "$source_dir/runConfigureICU" "$platform" \
          --enable-static --disable-shared \
          --disable-samples --disable-tests --disable-extras
      make -j"$jobs"
    SH

    odie "ICU host icupkg was not built" unless (host_build/"bin/icupkg").executable?
    odie "ICU host pkgdata was not built" unless (host_build/"bin/pkgdata").executable?

    # Kandelo's target triple selects ICU's deliberate mh-unknown stop file.
    # The static-only build needs the generic compile rules from mh-linux; its
    # shared-library rules remain disabled by the configure contract below.
    rm source/"config/mh-unknown"
    cp source/"config/mh-linux", source/"config/mh-unknown"

    archive_forbidden_paths = []
    kandelo_wasm_build do |sdk_root|
      stable_source = "/usr/src/icu-#{version}"
      prefix_maps = {
        buildpath.to_s => stable_source,
        sdk_root.to_s  => "/usr/src/kandelo",
        libcxx.to_s    => "/usr/src/libcxx",
        "/nix/store"   => "/usr/src/toolchain",
      }.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end
      ENV["CFLAGS"] = "-O2 -fPIC #{prefix_maps.join(" ")}"
      ENV["CXXFLAGS"] = [
        "-O2", "-std=c++17", "-fPIC", "-nostdinc++", "-isystem", libcxx/"include/c++/v1", *prefix_maps
      ].join(" ")
      ENV["LDFLAGS"] = "-L#{libcxx}/lib -lc++ -lc++abi"
      ENV["PKG_CONFIG_LIBDIR"] = "#{libcxx}/lib/pkgconfig"
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")

      cd source do
        system kandelo_configure(sdk_root),
          "--with-cross-build=#{host_build}",
          "--enable-static",
          "--disable-shared",
          "--disable-tools",
          "--disable-tests",
          "--disable-samples",
          "--disable-extras",
          "--disable-layoutex",
          "--with-data-packaging=archive",
          "--prefix=#{prefix}"
        system "make", "-j#{ENV.make_jobs}", "ICUDATA_DIR=#{GUEST_DATA_DIR}"
        system "make", "install"
      end

      expected_members = {
        "libicudata.a" => 1,
        "libicui18n.a" => 243,
        "libicuio.a"   => 12,
        "libicuuc.a"   => 201,
      }
      expected_members.each do |archive_name, count|
        archive = lib/archive_name
        odie "ICU did not install #{archive_name}" unless archive.file?

        members = Utils.safe_popen_read(kandelo_ar(sdk_root), "t", archive).lines
        if members.length != count
          odie "#{archive_name} member count drifted: expected #{count}, found #{members.length}"
        end
      end
      archive_forbidden_paths = [buildpath, sdk_root, libcxx, prefix].map(&:to_s)
    end

    # Preserve the registry consumer contract in addition to ICU's normal
    # versioned archive. PHP intentionally loads this exact file with
    # udata_setCommonData rather than relying on ICU's filename search.
    installed_data = pkgshare/"74.2/icudt74l.dat"
    odie "ICU did not install its common data archive" unless installed_data.file?
    cp installed_data, share/"icu.dat"

    [installed_data, share/"icu.dat"].each do |data|
      odie "#{data} has the wrong byte length" if data.size != ICU_DATA_BYTES
      odie "#{data} has the wrong ICU 74.2 data digest" if Digest::SHA256.file(data).hexdigest != ICU_DATA_SHA256
    end

    expected_prefix = "prefix = #{prefix}"
    relative_prefix = "prefix = ${pcfiledir}/../.."
    expected_pkglibdir = "pkglibdir=${libdir}/icu${ICULIBSUFFIX}/74.2"
    relative_pkglibdir = "pkglibdir=${libdir}"
    %w[icu-uc.pc icu-i18n.pc icu-io.pc].each do |pc_name|
      pc = lib/"pkgconfig"/pc_name
      odie "ICU did not install #{pc_name}" unless pc.file?

      lines = pc.read.lines.map(&:chomp)
      odie "#{pc_name} prefix metadata drifted" if lines.grep(/^prefix = /) != [expected_prefix]
      odie "#{pc_name} pkglibdir metadata drifted" if lines.grep(/^pkglibdir=/) != [expected_pkglibdir]
      inreplace pc, expected_prefix, relative_prefix
      inreplace pc, expected_pkglibdir, relative_pkglibdir
      rewritten_lines = pc.read.lines.map(&:chomp)
      odie "#{pc_name} prefix was not relocated" if rewritten_lines.grep(/^prefix = /) != [relative_prefix]
      odie "#{pc_name} pkglibdir was not relocated" if rewritten_lines.grep(/^pkglibdir=/) != [relative_pkglibdir]
    end

    # --disable-tools deliberately omits the target generators. Remove their
    # launchers and build metadata instead of advertising executables that are
    # not in this library-only package.
    rm bin/"icu-config"
    rm_r lib/"icu"
    rm man/"man1/icu-config.1"
    rm_r pkgshare/"74.2/config"
    rm pkgshare/"74.2/install-sh"
    rm pkgshare/"74.2/mkinstalldirs"

    odie "ICU header set is incomplete" if include.glob("unicode/*.h").length != 197
    reject_builder_paths!(archive_forbidden_paths)
  end

  test do
    root = Pathname(kandelo_require_root!)
    libcxx = formula_opt_prefix("kandelo-dev/tap-core/libcxx")
    archives = {
      "libicudata.a" => 1,
      "libicui18n.a" => 243,
      "libicuio.a"   => 12,
      "libicuuc.a"   => 201,
    }

    assert_equal 197, include.glob("unicode/*.h").length
    archives.each_key { |archive| assert_path_exists lib/archive }
    %w[icu-uc.pc icu-i18n.pc icu-io.pc].each { |pc| assert_path_exists lib/"pkgconfig"/pc }
    refute_path_exists bin/"icu-config"
    refute_path_exists lib/"icu"
    refute_path_exists man/"man1/icu-config.1"
    refute_path_exists pkgshare/"74.2/config"
    refute_path_exists pkgshare/"74.2/install-sh"
    refute_path_exists pkgshare/"74.2/mkinstalldirs"
    assert_path_exists pkgshare/"74.2/icudt74l.dat"
    assert_path_exists share/"icu.dat"
    assert_equal ICU_DATA_BYTES, (share/"icu.dat").size
    assert_equal ICU_DATA_SHA256, Digest::SHA256.file(share/"icu.dat").hexdigest
    assert_equal ICU_DATA_SHA256, Digest::SHA256.file(pkgshare/"74.2/icudt74l.dat").hexdigest

    metadata_files = lib.glob("pkgconfig/icu-*.pc")
    metadata_files.each do |pc|
      lines = pc.read.lines.map(&:chomp)
      assert_equal ["prefix = ${pcfiledir}/../.."], lines.grep(/^prefix = /)
      assert_equal ["pkglibdir=${libdir}"], lines.grep(/^pkglibdir=/)
    end
    metadata = metadata_files.map(&:read).join("\n")
    refute_includes metadata, prefix.to_s
    refute_includes metadata, root.to_s
    refute_includes metadata, libcxx.to_s
    refute_includes metadata, "/private/tmp/"
    refute_includes metadata, "/private/var/"
    refute_match %r{/Users/[^/]+/}, metadata
    refute_includes metadata, "/nix/store/"

    source = testpath/"icu-smoke.cpp"
    wasm = testpath/"icu-smoke.wasm"
    source.write <<~CPP
      #include <unicode/uclean.h>
      #include <unicode/ucol.h>
      #include <unicode/udata.h>
      #include <unicode/uloc.h>
      #include <unicode/unorm2.h>
      #include <unicode/ustdio.h>
      #include <unicode/ustring.h>
      #include <fcntl.h>
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <sys/stat.h>
      #include <unistd.h>

      static void fail(const char *operation, UErrorCode status) {
        fprintf(stderr, "%s: %s\\n", operation, u_errorName(status));
        exit(1);
      }

      static void load_common_data(const char *path) {
        int fd = open(path, O_RDONLY);
        struct stat st;
        void *data;
        size_t offset = 0;
        UErrorCode status = U_ZERO_ERROR;

        if (fd < 0 || fstat(fd, &st) != 0 || st.st_size != #{ICU_DATA_BYTES}) exit(2);
        data = malloc((size_t)st.st_size);
        if (data == NULL) exit(3);
        while (offset < (size_t)st.st_size) {
          ssize_t count = read(fd, (char *)data + offset, (size_t)st.st_size - offset);
          if (count <= 0) exit(4);
          offset += (size_t)count;
        }
        close(fd);
        udata_setCommonData(data, &status);
        if (U_FAILURE(status)) fail("udata_setCommonData", status);
      }

      int main(int argc, char **argv) {
        UErrorCode status = U_ZERO_ERROR;
        UChar language[32];
        UChar formatted[64];
        char utf8[64];
        int32_t utf8_length = 0;
        UChar zed[] = { 'z', 0 };
        UChar a_umlaut[] = { 0x00e4, 0 };
        UChar decomposed[] = { 'e', 0x0301, 0 };
        UChar normalized[4];
        const UNormalizer2 *normalizer;
        UCollator *collator;

        if (argc > 2) return 5;
        if (argc == 2) load_common_data(argv[1]);
        u_init(&status);
        if (U_FAILURE(status)) fail("u_init", status);

        status = U_ZERO_ERROR;
        if (uloc_getDisplayLanguage("fr", "en", language, 32, &status) != 6 || U_FAILURE(status)) {
          fail("uloc_getDisplayLanguage", status);
        }
        if (u_sprintf(formatted, "%S:%d", language, 42) != 9) return 6;
        u_strToUTF8(utf8, sizeof(utf8), &utf8_length, formatted, -1, &status);
        if (U_FAILURE(status) || strcmp(utf8, "French:42") != 0) return 7;

        status = U_ZERO_ERROR;
        collator = ucol_open("sv_SE", &status);
        if (U_FAILURE(status) || collator == NULL) fail("ucol_open", status);
        if (ucol_strcoll(collator, zed, 1, a_umlaut, 1) != UCOL_LESS) return 8;
        ucol_close(collator);

        status = U_ZERO_ERROR;
        normalizer = unorm2_getNFCInstance(&status);
        if (U_FAILURE(status) || normalizer == NULL) fail("unorm2_getNFCInstance", status);
        if (unorm2_normalize(normalizer, decomposed, 2, normalized, 4, &status) != 1 ||
            U_FAILURE(status) || normalized[0] != 0x00e9) return 9;

        puts("icu-ok:French:42:sv-SE:NFC");
        return 0;
      }
    CPP

    kandelo_wasm_build do |sdk_root|
      ENV["PKG_CONFIG_LIBDIR"] = "#{lib}/pkgconfig"
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")
      pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
      flags = Utils.safe_popen_read(pkgconf, "--static", "--cflags", "--libs", "icu-io").split
      %w[-licuio -licui18n -licuuc -licudata -lpthread -lm].each do |flag|
        assert_includes flags, flag
      end

      archives.each do |archive, count|
        members = Utils.safe_popen_read(kandelo_ar(sdk_root), "t", lib/archive).lines
        assert_equal count, members.length
        system kandelo_tool("nm", sdk_root), "--print-file-name", "--defined-only", lib/archive
      end
      system kandelo_tool("c++", sdk_root), source,
        "-fwasm-exceptions",
        "-nostdinc++", "-isystem", libcxx/"include/c++/v1",
        *flags, "-L#{libcxx}/lib", "-lc++", "-lc++abi", "-o", wasm
      kandelo_validate_wasm_artifact(wasm, fork: :forbidden)
    end

    versioned_guest_data = "#{GUEST_DATA_DIR}/icudt74l.dat"
    explicit_guest_data = "#{GUEST_OPT_PREFIX}/share/icu.dat"
    conventional_files = { versioned_guest_data => pkgshare/"74.2/icudt74l.dat" }
    explicit_files = { explicit_guest_data => share/"icu.dat" }
    expected = "icu-ok:French:42:sv-SE:NFC\n"
    assert_equal expected, kandelo_run_wasm(wasm, [], guest_files: conventional_files)
    assert_equal expected, kandelo_run_wasm(wasm, [explicit_guest_data], guest_files: explicit_files)
    assert_equal expected,
      kandelo_run_browser_wasm(wasm, [], guest_files: conventional_files, timeout_ms: 180_000)
    assert_equal expected,
      kandelo_run_browser_wasm(wasm, [explicit_guest_data], guest_files: explicit_files, timeout_ms: 180_000)
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 4
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "e55e177231052b0ff7ebf1b221d04c2a8d920c9965c52473aec59a898af69f9a"
  end

  private

  def reject_builder_paths!(formula_paths)
    forbidden = [
      *formula_paths,
      "/private/tmp/",
      "/private/var/",
      "/nix/store/",
      "/opt/homebrew/Cellar/",
      "/usr/local/Cellar/",
    ].reject(&:empty?).uniq

    lib.glob("libicu*.a").each do |archive|
      bytes = archive.binread
      forbidden.each do |path|
        odie "#{archive.basename} contains builder path #{path}" if bytes.include?(path)
      end
      odie "#{archive.basename} contains a builder home path" if bytes.match?(%r{/Users/[^/]+/})
    end
  end
end
