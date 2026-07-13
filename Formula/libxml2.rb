require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Libxml2 < Formula
  include KandeloFormulaSupport

  GUEST_SYSCONFDIR = "/home/linuxbrew/.linuxbrew/etc".freeze
  GUEST_CATALOG_URI = "file://#{GUEST_SYSCONFDIR}/xml/catalog".freeze

  desc "GNOME XML parsing library for Kandelo"
  homepage "https://gitlab.gnome.org/GNOME/libxml2/-/wikis/home"
  url "https://download.gnome.org/sources/libxml2/2.13/libxml2-2.13.8.tar.xz"
  sha256 "277294cb33119ab71b2bc81f2f445e9bc9435b893ad15bb2cd2b0e859a0ee84a"
  license "MIT"
  revision 2

  depends_on "cmake" => [:build, :test]
  depends_on "pkgconf" => [:build, :test]
  depends_on "automattic/kandelo-homebrew/libiconv"
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "lib/libxml2.a"

  def install
    kandelo_require_arch!("wasm32")
    libiconv = formula_opt_prefix("automattic/kandelo-homebrew/libiconv")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")

    # Force downstream cross-builds to resolve the same GNU libiconv keg rather
    # than accepting the host or Kandelo musl implementation as built in.
    inreplace "libxml2-config.cmake.cmake.in",
      "if(LIBXML2_WITH_ICONV)\n  find_dependency(Iconv)",
      "if(LIBXML2_WITH_ICONV)\n  set(Iconv_IS_BUILT_IN FALSE)\n  find_dependency(Iconv)"
    # debugXML.c uses the POSIX access() API but omits its declaring header.
    inreplace "debugXML.c", "#include <stdlib.h>\n", <<~EOS
      #include <stdlib.h>
      #ifdef HAVE_UNISTD_H
      #include <unistd.h>
      #endif
    EOS

    kandelo_wasm_build do |root|
      system "cmake", "-S", ".", "-B", "build",
        "-DCMAKE_INSTALL_PREFIX=#{prefix}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
        # Keep install metadata relocatable through the host keg while the
        # target library resolves its global catalog from the guest prefix.
        "-DCMAKE_INSTALL_SYSCONFDIR=#{GUEST_SYSCONFDIR}",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_SYSTEM_NAME=Generic",
        "-DCMAKE_SYSTEM_PROCESSOR=#{kandelo_arch}",
        "-DUNIX=ON",
        "-DCMAKE_C_COMPILER=#{kandelo_cc(root)}",
        "-DCMAKE_AR=#{kandelo_ar(root)}",
        "-DCMAKE_RANLIB=#{kandelo_ranlib(root)}",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DLIBXML2_WITH_ICONV=ON",
        "-DIconv_IS_BUILT_IN=FALSE",
        "-DIconv_INCLUDE_DIR=#{libiconv}/include",
        "-DIconv_LIBRARY=#{libiconv}/lib/libiconv.a",
        # Upstream's CMake generator omits its detected module loader from
        # static pkg-config metadata even though the exported target has it;
        # GNU libiconv's static closure also includes libcharset.
        "-DLIBS=-lcharset -ldl",
        "-DLIBXML2_WITH_PROGRAMS=OFF",
        "-DLIBXML2_WITH_PYTHON=OFF",
        "-DLIBXML2_WITH_TESTS=OFF",
        "-DLIBXML2_WITH_ZLIB=ON",
        "-DZLIB_INCLUDE_DIR=#{zlib}/include",
        "-DZLIB_LIBRARY=#{zlib}/lib/libz.a"
      system "cmake", "--build", "build", "--parallel"
      system "cmake", "--install", "build"
    end
  end

  test do
    assert_path_exists lib/"libxml2.a"
    assert_path_exists include/"libxml2/libxml/parser.h"
    assert_path_exists lib/"pkgconfig/libxml-2.0.pc"
    archive = (lib/"libxml2.a").binread
    assert_includes archive, GUEST_CATALOG_URI
    refute_includes archive, prefix.to_s

    libiconv = formula_opt_prefix("automattic/kandelo-homebrew/libiconv")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    metadata_paths = [
      lib/"pkgconfig/libxml-2.0.pc",
      *Dir[(lib/"cmake/**/*").to_s].select { |path| File.file?(path) },
    ]
    metadata = metadata_paths.map { |path| File.binread(path) }.join
    assert_includes metadata, "set(Iconv_IS_BUILT_IN FALSE)"
    assert_includes metadata, "-liconv"
    assert_includes metadata, "-lcharset"
    refute_includes metadata, libiconv.to_s

    source = testpath/"libxml2-smoke.c"
    wasm = testpath/"libxml2-smoke.wasm"
    cmake_source = testpath/"cmake-consumer"
    cmake_build = testpath/"cmake-build"
    source.write <<~C
      #include <libxml/parser.h>
      #include <libxml/tree.h>
      #include <libxml/xmlmodule.h>
      #include <libxml/xmlversion.h>
      #include <pthread.h>
      #include <stdio.h>
      #include <string.h>
      #include <zlib.h>

      #ifndef LIBXML_THREAD_ENABLED
      #error "libxml2 thread support is disabled"
      #endif
      #ifndef LIBXML_ICONV_ENABLED
      #error "libxml2 iconv support is disabled"
      #endif
      #ifndef LIBXML_DEBUG_ENABLED
      #error "libxml2 debug support is disabled"
      #endif
      #ifndef LIBXML_MODULES_ENABLED
      #error "libxml2 module support is disabled"
      #endif
      #ifndef LIBXML_ZLIB_ENABLED
      #error "libxml2 zlib support is disabled"
      #endif

      static void *parse_document(void *argument) {
        int *result = argument;
        xmlDocPtr document = xmlReadFile("document.xml.gz", NULL, XML_PARSE_NONET);
        xmlNodePtr root;
        xmlChar *content;

        if (document == NULL) { *result = 1; return NULL; }
        root = xmlDocGetRootElement(document);
        if (root == NULL || xmlStrcmp(root->name, BAD_CAST "root") != 0) {
          *result = 2;
          return NULL;
        }
        content = xmlNodeGetContent(root);
        if (content == NULL || xmlStrcmp(content, BAD_CAST "\\xd5\\xa1") != 0) {
          *result = 3;
          return NULL;
        }

        xmlFree(content);
        xmlFreeDoc(document);
        *result = 0;
        return NULL;
      }

      int main(void) {
        static const char xml[] =
          "<?xml version='1.0' encoding='ARMSCII-8'?><root>\\xb3</root>";
        gzFile compressed;
        pthread_t thread;
        int result = -1;

        compressed = gzopen("document.xml.gz", "wb");
        if (compressed == NULL) return 6;
        if (gzwrite(compressed, xml, sizeof(xml) - 1) != sizeof(xml) - 1) return 7;
        if (gzclose(compressed) != Z_OK) return 8;

        if (pthread_create(&thread, NULL, parse_document, &result) != 0) return 4;
        if (pthread_join(thread, NULL) != 0) return 5;
        if (result != 0) return result;
        if (xmlModuleOpen("missing-kandelo-test-module.so", 0) != NULL) return 9;
        xmlCleanupParser();
        puts("libxml2-ok");
        return 0;
      }
    C

    kandelo_wasm_build do
      ENV["PKG_CONFIG_LIBDIR"] = "#{lib}/pkgconfig:#{zlib}/lib/pkgconfig"
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")
      pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
      flags = shell_output("#{pkgconf} --static --cflags --libs libxml-2.0").split
      %w[-lxml2 -liconv -lcharset -lz -lm -ldl].each do |flag|
        assert_includes flags, flag
      end
      system kandelo_cc, source,
        "-I#{libiconv}/include", "-I#{zlib}/include",
        "-L#{libiconv}/lib", "-L#{zlib}/lib",
        *flags, "-o", wasm
    end
    assert_equal "libxml2-ok\n", kandelo_run_wasm(wasm, [])
    assert_equal "libxml2-ok\n", kandelo_run_browser_wasm(wasm, [])

    cmake_source.mkpath
    (cmake_source/"main.c").write <<~C
      #include <libxml/parser.h>
      #include <libxml/tree.h>
      #include <libxml/xmlmodule.h>
      #include <libxml/xmlversion.h>
      #include <pthread.h>
      #include <stdio.h>
      #include <string.h>

      #if !defined(LIBXML_THREAD_ENABLED) || !defined(LIBXML_ICONV_ENABLED) || \
          !defined(LIBXML_DEBUG_ENABLED) || !defined(LIBXML_MODULES_ENABLED) || \
          !defined(LIBXML_ZLIB_ENABLED)
      #error "libxml2 expected feature support is disabled"
      #endif

      static void *parse_document(void *argument) {
        int *result = argument;
        static const char xml[] = "<root>cmake</root>";
        xmlDocPtr document = xmlReadMemory(xml, strlen(xml), "memory.xml", NULL, XML_PARSE_NONET);
        xmlNodePtr root = document == NULL ? NULL : xmlDocGetRootElement(document);

        *result = root == NULL || xmlStrcmp(root->name, BAD_CAST "root") != 0;
        xmlFreeDoc(document);
        return NULL;
      }

      int main(void) {
        pthread_t thread;
        int result = -1;

        if (pthread_create(&thread, NULL, parse_document, &result) != 0) return 1;
        if (pthread_join(thread, NULL) != 0 || result != 0) return 2;
        if (xmlModuleOpen("missing-kandelo-test-module.so", 0) != NULL) return 3;
        xmlCleanupParser();
        puts("libxml2-cmake-ok");
        return 0;
      }
    C
    (cmake_source/"CMakeLists.txt").write <<~CMAKE
      cmake_minimum_required(VERSION 3.20)
      project(libxml2_consumer C)
      find_package(LibXml2 CONFIG REQUIRED)
      get_target_property(libxml2_links LibXml2::LibXml2 INTERFACE_LINK_LIBRARIES)
      get_target_property(iconv_includes Iconv::Iconv INTERFACE_INCLUDE_DIRECTORIES)
      get_target_property(iconv_links Iconv::Iconv INTERFACE_LINK_LIBRARIES)
      foreach(required IN ITEMS dl m Iconv::Iconv Threads::Threads ZLIB::ZLIB)
        string(FIND "${libxml2_links}" "${required}" required_index)
        if(required_index EQUAL -1)
          message(FATAL_ERROR "LibXml2::LibXml2 does not propagate ${required}")
        endif()
      endforeach()
      if(NOT iconv_includes STREQUAL "#{libiconv}/include")
        message(FATAL_ERROR "Iconv::Iconv does not use the tap libiconv headers")
      endif()
      if(NOT iconv_links STREQUAL "#{libiconv}/lib/libiconv.a")
        message(FATAL_ERROR "Iconv::Iconv does not use the tap libiconv archive")
      endif()
      add_executable(libxml2-cmake main.c)
      target_link_libraries(libxml2-cmake PRIVATE LibXml2::LibXml2)
    CMAKE
    kandelo_wasm_build do |root|
      system "cmake", "-S", cmake_source, "-B", cmake_build,
        "-DCMAKE_SYSTEM_NAME=Generic",
        "-DCMAKE_SYSTEM_PROCESSOR=#{kandelo_arch}",
        "-DCMAKE_C_COMPILER=#{kandelo_cc(root)}",
        "-DCMAKE_AR=#{kandelo_ar(root)}",
        "-DCMAKE_RANLIB=#{kandelo_ranlib(root)}",
        "-DCMAKE_PREFIX_PATH=#{prefix};#{libiconv};#{zlib}",
        "-DIconv_IS_BUILT_IN=FALSE",
        "-DIconv_INCLUDE_DIR=#{libiconv}/include",
        "-DIconv_LIBRARY=#{libiconv}/lib/libiconv.a",
        "-DZLIB_INCLUDE_DIR=#{zlib}/include",
        "-DZLIB_LIBRARY=#{zlib}/lib/libz.a"
      system "cmake", "--build", cmake_build, "--parallel"
    end
    cache = (cmake_build/"CMakeCache.txt").read
    {
      "Iconv_IS_BUILT_IN" => "FALSE",
      "Iconv_INCLUDE_DIR" => (libiconv/"include").to_s,
      "Iconv_LIBRARY"     => (libiconv/"lib/libiconv.a").to_s,
    }.each do |key, value|
      assert_match(/^#{key}:[^=]*=#{Regexp.escape(value)}$/, cache)
    end
    assert_equal "libxml2-cmake-ok\n", kandelo_run_wasm(cmake_build/"libxml2-cmake", [])
  end
end
