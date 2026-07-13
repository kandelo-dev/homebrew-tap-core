require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Libzip < Formula
  include KandeloFormulaSupport

  desc "C library and tools for reading and modifying ZIP archives on Kandelo"
  homepage "https://libzip.org/"
  url "https://libzip.org/download/libzip-1.11.4.tar.gz"
  sha256 "82e9f2f2421f9d7c2466bbc3173cd09595a88ea37db0d559a9d0a2dc60dc722e"
  license "BSD-3-Clause"

  depends_on "binaryen" => :build
  depends_on "cmake" => [:build, :test]
  depends_on "ninja" => [:build, :test]
  depends_on "pkgconf" => [:build, :test]
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "bin", "lib/libzip.a"

  def install
    kandelo_require_arch!("wasm32")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")

    # Kandelo executables permit unresolved kernel imports, so link-based
    # CMake probes otherwise report host-only Annex K and Windows APIs as
    # available. Seed the wasm32+musl platform facts while leaving normal
    # compile-only header and type probes to upstream.
    platform_facts = %w[
      HAVE___PROGNAME=OFF
      HAVE__CLOSE=OFF
      HAVE__DUP=OFF
      HAVE__FDOPEN=OFF
      HAVE__FILENO=OFF
      HAVE__FSEEKI64=OFF
      HAVE__FSTAT64=OFF
      HAVE__SETMODE=OFF
      HAVE__SNPRINTF=OFF
      HAVE__SNPRINTF_S=OFF
      HAVE__SNWPRINTF_S=OFF
      HAVE__STAT64=OFF
      HAVE__STRDUP=OFF
      HAVE__STRICMP=OFF
      HAVE__STRTOI64=OFF
      HAVE__STRTOUI64=OFF
      HAVE__UNLINK=OFF
      HAVE_ARC4RANDOM=OFF
      HAVE_CLONEFILE=OFF
      HAVE_EXPLICIT_BZERO=ON
      HAVE_EXPLICIT_MEMSET=OFF
      HAVE_FCHMOD=ON
      HAVE_FICLONERANGE=OFF
      HAVE_FILENO=ON
      HAVE_FSEEKO=ON
      HAVE_FTELLO=ON
      HAVE_FTS_H=OFF
      HAVE_GETPROGNAME=OFF
      HAVE_GETSECURITYINFO=OFF
      HAVE_LOCALTIME_R=ON
      HAVE_LOCALTIME_S=OFF
      HAVE_MEMCPY_S=OFF
      HAVE_MKSTEMP=ON
      HAVE_RANDOM=ON
      HAVE_SETMODE=OFF
      HAVE_SNPRINTF=ON
      HAVE_SNPRINTF_S=OFF
      HAVE_STRCASECMP=ON
      HAVE_STRDUP=ON
      HAVE_STRERROR_S=OFF
      HAVE_STRERRORLEN_S=OFF
      HAVE_STRICMP=OFF
      HAVE_STRNCPY_S=OFF
      HAVE_STRTOLL=ON
      HAVE_STRTOULL=ON
      HAVE_STRUCT_TM_TM_ZONE=ON
      HAVE_DIRENT_H=ON
      HAVE_NDIR_H=OFF
      HAVE_SYS_DIR_H=OFF
      HAVE_SYS_NDIR_H=OFF
      WORDS_BIGENDIAN=OFF
    ].map { |fact| "-D#{fact}" }

    kandelo_wasm_build do |root|
      sysroot = Pathname(ENV.fetch("WASM_POSIX_SYSROOT"))
      reproducible_flags = [
        "-O2", "-DNDEBUG", "-fPIC",
        "-ffile-prefix-map=#{buildpath}=/usr/src/libzip",
        "-fdebug-prefix-map=#{buildpath}=/usr/src/libzip",
        "-fmacro-prefix-map=#{buildpath}=/usr/src/libzip",
        "-ffile-prefix-map=#{root}=/usr/src/kandelo",
        "-fdebug-prefix-map=#{root}=/usr/src/kandelo",
        "-fmacro-prefix-map=#{root}=/usr/src/kandelo",
        "-ffile-prefix-map=#{zlib}=/usr/src/kandelo-deps/zlib",
        "-fdebug-prefix-map=#{zlib}=/usr/src/kandelo-deps/zlib",
        "-fmacro-prefix-map=#{zlib}=/usr/src/kandelo-deps/zlib"
      ].join(" ")

      ENV["LC_ALL"] = "C"
      ENV["TZ"] = "UTC"
      ENV["SOURCE_DATE_EPOCH"] = "0"
      ENV["ZERO_AR_DATE"] = "1"

      system "cmake", "-S", ".", "-B", "build", "-G", "Ninja",
        "-DCMAKE_INSTALL_PREFIX=#{prefix}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
        "-DCMAKE_INSTALL_INCLUDEDIR=include",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_SYSTEM_NAME=Generic",
        "-DCMAKE_SYSTEM_PROCESSOR=wasm32",
        "-DCMAKE_C_COMPILER=#{kandelo_cc(root)}",
        "-DCMAKE_AR=#{kandelo_ar(root)}",
        "-DCMAKE_RANLIB=#{kandelo_ranlib(root)}",
        "-DCMAKE_STRIP=#{kandelo_tool("strip", root)}",
        "-DCMAKE_C_FLAGS_RELEASE=#{reproducible_flags}",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        "-DCMAKE_FIND_ROOT_PATH=#{sysroot};#{zlib}",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER",
        "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DBUILD_TOOLS=ON",
        "-DBUILD_REGRESS=OFF",
        "-DBUILD_OSSFUZZ=OFF",
        "-DBUILD_EXAMPLES=OFF",
        "-DBUILD_DOC=OFF",
        "-DENABLE_BZIP2=OFF",
        "-DENABLE_COMMONCRYPTO=OFF",
        "-DENABLE_GNUTLS=OFF",
        "-DENABLE_LZMA=OFF",
        "-DENABLE_MBEDTLS=OFF",
        "-DENABLE_OPENSSL=OFF",
        "-DENABLE_WINDOWS_CRYPTO=OFF",
        "-DENABLE_ZSTD=OFF",
        "-DENABLE_FDOPEN=ON",
        "-DZLIB_ROOT=#{zlib}",
        "-DZLIB_INCLUDE_DIR=#{zlib}/include",
        "-DZLIB_LIBRARY=#{zlib}/lib/libz.a",
        "-DZLIB_LINK_LIBRARY_NAME=z",
        *platform_facts
      system "cmake", "--build", "build", "--parallel", ENV.make_jobs
      system "cmake", "--install", "build"

      %w[zipcmp zipmerge ziptool].each do |program|
        kandelo_validate_wasm_artifact(bin/program, fork: :forbidden, forbidden_paths: [zlib])
      end
    end

    inreplace lib/"pkgconfig/libzip.pc" do |s|
      contents = s.inreplace_string
      odie "libzip.pc does not contain its staged prefix" unless contents.include?(prefix.to_s)
      private_libs = contents.scan(/^Libs\.private:.*$/)
      odie "libzip.pc must contain exactly one Libs.private record" if private_libs.length != 1

      s.gsub! prefix.to_s, opt_prefix.to_s
      replacement = s.sub!(/^Libs\.private:.*$/, "Requires.private: zlib\nLibs.private:")
      odie "libzip.pc private dependency replacement failed" if replacement.nil?
    end
  end

  test do
    assert_path_exists lib/"libzip.a"
    assert_path_exists include/"zip.h"
    assert_path_exists include/"zipconf.h"
    assert_path_exists lib/"pkgconfig/libzip.pc"
    %w[zipcmp zipmerge ziptool].each { |program| assert_path_exists bin/program }
    pkgconfig = (lib/"pkgconfig/libzip.pc").read
    assert_equal 1, pkgconfig.scan(/^Requires\.private: zlib$/).length
    assert_equal ["Libs.private:"], pkgconfig.scan(/^Libs\.private:.*$/)

    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    source = testpath/"libzip-smoke.c"
    wasm = testpath/"libzip-smoke.wasm"
    cmake_source = testpath/"cmake-consumer"
    cmake_build = testpath/"cmake-build"
    source.write <<~C
      #include <stdio.h>
      #include <string.h>
      #include <time.h>
      #include <zip.h>

      int main(void) {
        static const char payload[] = "Kandelo libzip deflate round trip";
        static const char comment[] = "kandelo-libzip";
        char output[sizeof(payload)] = { 0 };
        zip_t *archive;
        zip_source_t *source;
        zip_file_t *file;
        zip_stat_t stat;
        zip_int64_t index;
        int comment_length = 0;
        const char *actual_comment;
        int error = 0;

        archive = zip_open("libzip-smoke.zip", ZIP_CREATE | ZIP_TRUNCATE, &error);
        if (archive == NULL) return 1;
        source = zip_source_buffer(archive, payload, sizeof(payload), 0);
        if (source == NULL) return 2;
        index = zip_file_add(archive, "payload.txt", source, ZIP_FL_ENC_UTF_8);
        if (index < 0) return 3;
        if (zip_set_file_compression(archive, index, ZIP_CM_DEFLATE, 0) != 0) return 4;
        if (zip_file_set_mtime(archive, index, (time_t)1700000000, 0) != 0) return 5;
        if (zip_set_archive_comment(archive, comment, sizeof(comment) - 1) != 0) return 6;
        if (zip_close(archive) != 0) return 7;

        archive = zip_open("libzip-smoke.zip", ZIP_RDONLY, &error);
        if (archive == NULL || zip_get_num_entries(archive, 0) != 1) return 8;
        zip_stat_init(&stat);
        if (zip_stat_index(archive, 0, 0, &stat) != 0) return 9;
        if (strcmp(stat.name, "payload.txt") != 0 || stat.size != sizeof(payload)) return 10;
        if (stat.comp_method != ZIP_CM_DEFLATE || stat.mtime != (time_t)1700000000) return 11;
        actual_comment = zip_get_archive_comment(archive, &comment_length, 0);
        if (actual_comment == NULL || comment_length != sizeof(comment) - 1 ||
            memcmp(actual_comment, comment, comment_length) != 0) return 12;
        file = zip_fopen_index(archive, 0, 0);
        if (file == NULL) return 13;
        if (zip_fread(file, output, sizeof(output)) != sizeof(output)) return 14;
        if (zip_fclose(file) != 0 || memcmp(output, payload, sizeof(payload)) != 0) return 15;
        if (zip_close(archive) != 0) return 16;

        printf("libzip %s roundtrip-ok\\n", zip_libzip_version());
        return 0;
      }
    C

    kandelo_wasm_build do
      ENV["PKG_CONFIG_LIBDIR"] = "#{lib}/pkgconfig:#{zlib}/lib/pkgconfig"
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")
      pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
      flags = Utils.safe_popen_read(pkgconf, "--static", "--cflags", "--libs", "libzip").split
      %w[-lzip -lz].each { |flag| assert_includes flags, flag }
      system kandelo_cc, source, *flags, "-o", wasm
    end
    assert_equal "libzip #{version} roundtrip-ok\n", kandelo_run_wasm(wasm, [])

    cmake_source.mkpath
    (cmake_source/"main.c").write <<~C
      #include <stdio.h>
      #include <zip.h>

      int main(void) {
        printf("libzip-cmake %s ok\\n", zip_libzip_version());
        return 0;
      }
    C
    (cmake_source/"CMakeLists.txt").write <<~CMAKE
      cmake_minimum_required(VERSION 3.20)
      project(libzip_consumer C)
      find_package(libzip CONFIG REQUIRED)
      get_target_property(libzip_links libzip::zip INTERFACE_LINK_LIBRARIES)
      string(FIND "${libzip_links}" "ZLIB::ZLIB" zlib_index)
      if(zlib_index EQUAL -1)
        message(FATAL_ERROR "libzip::zip does not propagate ZLIB::ZLIB")
      endif()
      add_executable(libzip-cmake main.c)
      target_link_libraries(libzip-cmake PRIVATE libzip::zip)
    CMAKE
    kandelo_wasm_build do |root|
      sysroot = Pathname(ENV.fetch("WASM_POSIX_SYSROOT"))
      system "cmake", "-S", cmake_source, "-B", cmake_build, "-G", "Ninja",
        "-DCMAKE_SYSTEM_NAME=Generic",
        "-DCMAKE_SYSTEM_PROCESSOR=wasm32",
        "-DCMAKE_C_COMPILER=#{kandelo_cc(root)}",
        "-DCMAKE_AR=#{kandelo_ar(root)}",
        "-DCMAKE_RANLIB=#{kandelo_ranlib(root)}",
        "-DCMAKE_FIND_ROOT_PATH=#{sysroot};#{prefix};#{zlib}",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PROGRAM=NEVER",
        "-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY",
        "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=ONLY",
        "-DCMAKE_PREFIX_PATH=#{prefix};#{zlib}",
        "-DZLIB_INCLUDE_DIR=#{zlib}/include",
        "-DZLIB_LIBRARY=#{zlib}/lib/libz.a"
      system "cmake", "--build", cmake_build, "--parallel", ENV.make_jobs
    end
    assert_equal "libzip-cmake #{version} ok\n", kandelo_run_wasm(cmake_build/"libzip-cmake", [])

    mounts = { "/work" => testpath }
    assert_empty kandelo_run_wasm(
      bin/"ziptool",
      ["-n", "/work/one.zip", "add", "alpha.txt", "kandelo-alpha",
       "set_file_compression", "0", "deflate", "0", "set_archive_comment", "kandelo-one"],
      writable_host_directories: mounts,
    )
    assert_empty kandelo_run_wasm(
      bin/"ziptool", ["-n", "/work/two.zip", "add", "beta.txt", "kandelo-beta"],
      writable_host_directories: mounts
    )
    assert_path_exists testpath/"one.zip"
    assert_path_exists testpath/"two.zip"

    one = kandelo_run_wasm(
      bin/"ziptool", ["/work/one.zip", "get_num_entries", "0", "cat", "0", "get_archive_comment"],
      writable_host_directories: mounts
    )
    assert_match(/1 entry in archive/, one)
    assert_match(/kandelo-alpha/, one)
    assert_match(/Archive comment: kandelo-one/, one)

    comparison = kandelo_run_wasm(
      bin/"zipcmp", ["-s", "/work/one.zip", "/work/two.zip"],
      writable_host_directories: mounts,
      expected_status:           1
    )
    assert_match(/1 files removed, 1 files added/, comparison)

    assert_empty kandelo_run_wasm(
      bin/"zipmerge", ["/work/merged.zip", "/work/one.zip", "/work/two.zip"],
      writable_host_directories: mounts
    )
    merged = kandelo_run_wasm(
      bin/"ziptool", ["/work/merged.zip", "get_num_entries", "0", "stat", "0", "stat", "1"],
      writable_host_directories: mounts
    )
    assert_match(/2 entries in archive/, merged)
    assert_match(/name: 'alpha\.txt'/, merged)
    assert_match(/name: 'beta\.txt'/, merged)
    assert_match(/compression method: '8'/, merged)

    metadata = [lib/"pkgconfig/libzip.pc", *lib.glob("cmake/**/*.cmake")]
    [lib/"libzip.a", *metadata, *bin.children].each do |artifact|
      contents = File.binread(artifact)
      refute_includes contents, prefix.to_s
      refute_includes contents, zlib.to_s
      refute_includes contents, "/private/tmp/"
      refute_includes contents, "/Users/"
      refute_includes contents, "/nix/store/"
    end
  end
end
