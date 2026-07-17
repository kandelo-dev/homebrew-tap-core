require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s
require "digest"

class Libcxx < Formula
  include KandeloFormulaSupport

  desc "LLVM C++ standard library and ABI runtime for Kandelo"
  homepage "https://libcxx.llvm.org/"
  url "https://github.com/llvm/llvm-project/releases/download/llvmorg-21.1.7/llvm-project-21.1.7.src.tar.xz"
  sha256 "e5b65fd79c95c343bb584127114cb2d252306c1ada1e057899b6aacdd445899e"
  license "Apache-2.0" => { with: "LLVM-exception" }
  revision 1

  depends_on "cmake" => :build
  depends_on "wabt" => :test

  skip_clean "lib/libc++.a"
  skip_clean "lib/libc++-pic.a"
  skip_clean "lib/libc++abi.a"
  skip_clean "lib/libc++abi-pic.a"
  skip_clean "lib/libc++experimental.a"

  # The register-save assembly files emit no code for Wasm, but include
  # assembly.h before their Wasm guard and use this directive afterward.
  patch :DATA

  def install
    kandelo_require_arch!("wasm32", "wasm64")
    pointer_size = (kandelo_arch == "wasm64") ? 8 : 4

    kandelo_wasm_build do |root|
      prefix_maps = [
        "-ffile-prefix-map=#{buildpath}=/usr/src/libcxx",
        "-fdebug-prefix-map=#{buildpath}=/usr/src/libcxx",
        "-fmacro-prefix-map=#{buildpath}=/usr/src/libcxx",
        "-ffile-prefix-map=#{root}=/usr/src/kandelo",
        "-fdebug-prefix-map=#{root}=/usr/src/kandelo",
        "-fmacro-prefix-map=#{root}=/usr/src/kandelo",
      ]
      cflags = "-O2 -DNDEBUG -fexceptions #{prefix_maps.join(" ")}"

      # Kandelo executables allow unresolved kernel imports, so CMake's
      # check_library_exists cannot distinguish a missing target symbol from a
      # real one. In particular, it reports __cxa_thread_atexit_impl even
      # though Kandelo's libc does not export it. Seed the audited target facts
      # instead of inheriting host or linker-policy false positives.
      cmake_args = [
        "-DCMAKE_INSTALL_PREFIX=#{prefix}",
        "-DCMAKE_INSTALL_LIBDIR=lib",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_SYSTEM_NAME=Generic",
        "-DCMAKE_SYSTEM_PROCESSOR=#{kandelo_arch}",
        "-DCMAKE_C_COMPILER=#{kandelo_cc(root)}",
        "-DCMAKE_CXX_COMPILER=#{kandelo_tool("c++", root)}",
        "-DCMAKE_AR=#{kandelo_ar(root)}",
        "-DCMAKE_RANLIB=#{kandelo_ranlib(root)}",
        "-DCMAKE_NM=#{kandelo_tool("nm", root)}",
        "-DCMAKE_SIZEOF_VOID_P=#{pointer_size}",
        "-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;libunwind",
        "-DLIBCXX_ENABLE_SHARED=OFF",
        "-DLIBCXX_ENABLE_STATIC=ON",
        "-DLIBCXX_ENABLE_EXCEPTIONS=ON",
        "-DLIBCXX_ENABLE_RTTI=ON",
        "-DLIBCXX_HAS_MUSL_LIBC=ON",
        "-DLIBCXX_HAS_PTHREAD_API=ON",
        "-DLIBCXX_CXX_ABI=libcxxabi",
        "-DLIBCXX_INCLUDE_BENCHMARKS=OFF",
        "-DLIBCXX_INCLUDE_TESTS=OFF",
        "-DLIBCXX_ENABLE_FILESYSTEM=ON",
        "-DLIBCXX_ENABLE_MONOTONIC_CLOCK=ON",
        "-DLIBCXX_ENABLE_RANDOM_DEVICE=ON",
        "-DLIBCXX_ENABLE_LOCALIZATION=ON",
        "-DLIBCXX_ENABLE_WIDE_CHARACTERS=ON",
        "-DLIBCXX_ENABLE_NEW_DELETE_DEFINITIONS=ON",
        "-DLIBCXXABI_ENABLE_SHARED=OFF",
        "-DLIBCXXABI_ENABLE_STATIC=ON",
        "-DLIBCXXABI_ENABLE_EXCEPTIONS=ON",
        "-DLIBCXXABI_USE_LLVM_UNWINDER=ON",
        "-DLIBCXXABI_ENABLE_STATIC_UNWINDER=ON",
        "-DLIBCXXABI_STATICALLY_LINK_UNWINDER_IN_STATIC_LIBRARY=ON",
        "-DLIBCXXABI_ENABLE_THREADS=ON",
        "-DLIBCXXABI_HAS_PTHREAD_API=ON",
        "-DLIBCXXABI_INCLUDE_TESTS=OFF",
        "-DLIBUNWIND_ENABLE_SHARED=OFF",
        "-DLIBUNWIND_ENABLE_STATIC=ON",
        "-DLIBUNWIND_ENABLE_THREADS=ON",
        "-DLIBUNWIND_USE_COMPILER_RT=OFF",
        "-DLIBUNWIND_INCLUDE_TESTS=OFF",
        "-DLIBUNWIND_HIDE_SYMBOLS=ON",
        "-DLIBUNWIND_INSTALL_HEADERS=ON",
        "-DLIBCXX_HAS_GCC_LIB=OFF",
        "-DLIBCXX_HAS_GCC_S_LIB=OFF",
        "-DLIBCXX_HAS_PTHREAD_LIB=ON",
        "-DLIBCXX_HAS_RT_LIB=ON",
        "-DLIBCXX_HAS_ATOMIC_LIB=OFF",
        "-DLIBCXXABI_HAS_C_LIB=ON",
        "-DLIBCXXABI_HAS_GCC_LIB=OFF",
        "-DLIBCXXABI_HAS_GCC_S_LIB=OFF",
        "-DLIBCXXABI_HAS_DL_LIB=ON",
        "-DLIBCXXABI_HAS_PTHREAD_LIB=ON",
        "-DLIBCXXABI_HAS_CXA_THREAD_ATEXIT_IMPL=OFF",
        "-DLIBUNWIND_HAS_C_LIB=ON",
        "-DLIBUNWIND_HAS_GCC_LIB=OFF",
        "-DLIBUNWIND_HAS_GCC_S_LIB=OFF",
        "-DLIBUNWIND_HAS_DL_LIB=ON",
        "-DLIBUNWIND_HAS_PTHREAD_LIB=ON",
        "-DLIBUNWIND_HAS_ROOT_LIB=OFF",
        "-DLIBUNWIND_HAS_BSD_LIB=OFF",
      ]
      system "cmake", "-S", "runtimes", "-B", "build",
        "-DCMAKE_C_FLAGS=#{cflags}", "-DCMAKE_CXX_FLAGS=#{cflags}", *cmake_args

      # Main Wasm modules use the default archives. Dynamic side modules
      # require every absorbed object to use position-independent Wasm
      # relocations, so build a genuinely separate PIC tree instead of
      # relabeling the default archives or changing their code generation for
      # existing consumers.
      pic_cflags = "#{cflags} -fPIC"
      system "cmake", "-S", "runtimes", "-B", "build-pic",
        "-DCMAKE_C_FLAGS=#{pic_cflags}", "-DCMAKE_CXX_FLAGS=#{pic_cflags}",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON", *cmake_args

      target_libraries = %w[
        LIBCXX_HAS_GCC_LIB=OFF
        LIBCXX_HAS_GCC_S_LIB=OFF
        LIBCXX_HAS_PTHREAD_LIB=ON
        LIBCXX_HAS_RT_LIB=ON
        LIBCXX_HAS_ATOMIC_LIB=OFF
        LIBCXXABI_HAS_C_LIB=ON
        LIBCXXABI_HAS_GCC_LIB=OFF
        LIBCXXABI_HAS_GCC_S_LIB=OFF
        LIBCXXABI_HAS_DL_LIB=ON
        LIBCXXABI_HAS_PTHREAD_LIB=ON
        LIBCXXABI_HAS_CXA_THREAD_ATEXIT_IMPL=OFF
        LIBUNWIND_HAS_C_LIB=ON
        LIBUNWIND_HAS_GCC_LIB=OFF
        LIBUNWIND_HAS_GCC_S_LIB=OFF
        LIBUNWIND_HAS_DL_LIB=ON
        LIBUNWIND_HAS_PTHREAD_LIB=ON
        LIBUNWIND_HAS_ROOT_LIB=OFF
        LIBUNWIND_HAS_BSD_LIB=OFF
      ]
      %w[build build-pic].each do |build_dir|
        cache = (buildpath/build_dir/"CMakeCache.txt").read
        target_libraries.each do |fact|
          variable, expected = fact.split("=", 2)
          entry = cache.each_line.find { |line| line.start_with?("#{variable}:") }
          value = entry ? entry.split("=", 2).last.strip : nil
          next if value == expected

          odie "CMake target-library fact drifted in #{build_dir}: #{variable}=#{value.inspect}"
        end
      end

      system "cmake", "--build", "build", "--parallel"
      system "cmake", "--install", "build"
      system "cmake", "--build", "build-pic", "--parallel"

      {
        "libc++-pic.a"    => "libc++.a",
        "libc++abi-pic.a" => "libc++abi.a",
      }.each do |installed_name, built_name|
        candidates = buildpath.glob("build-pic/**/#{built_name}").reject do |candidate|
          candidate.to_s.include?("/CMakeFiles/")
        end
        odie "expected one PIC #{built_name}, found #{candidates.map(&:to_s).inspect}" if candidates.length != 1

        lib.install candidates.fetch(0) => installed_name
      end
    end

    # libc++abi contains the static unwinder; consumers intentionally need only
    # -lc++ -lc++abi, matching Kandelo's existing libcxx package contract.
    rm lib/"libunwind.a"
  end

  test do
    root = kandelo_require_root!
    assert_path_exists lib/"libc++.a"
    assert_path_exists lib/"libc++-pic.a"
    assert_path_exists lib/"libc++abi.a"
    assert_path_exists lib/"libc++abi-pic.a"
    assert_path_exists lib/"libc++experimental.a"
    assert_path_exists include/"c++/v1/vector"
    assert_path_exists include/"libunwind.h"
    assert_path_exists include/"unwind.h"
    refute_path_exists lib/"libunwind.a"

    builder_path_markers = %w[/private/tmp/ /nix/store/]
    %w[libc++.a libc++-pic.a libc++abi.a libc++abi-pic.a libc++experimental.a].each do |archive|
      binary = File.binread(lib/archive)
      builder_path_markers.each do |marker|
        refute binary.include?(marker), "#{archive} contains builder path marker #{marker}"
      end
      refute binary.match?(%r{/Users/[^/]+/}), "#{archive} contains a builder home path"
      refute binary.match?(%r{/tmp/libcxx-[^/]+/}), "#{archive} contains a Linux build path"
      refute binary.include?(root), "#{archive} contains the Kandelo checkout path"
      refute binary.include?(prefix.to_s), "#{archive} contains its Homebrew Cellar path"
    end
    refute_equal Digest::SHA256.file(lib/"libc++.a").hexdigest,
      Digest::SHA256.file(lib/"libc++-pic.a").hexdigest
    refute_equal Digest::SHA256.file(lib/"libc++abi.a").hexdigest,
      Digest::SHA256.file(lib/"libc++abi-pic.a").hexdigest

    source = testpath/"libcxx-smoke.cpp"
    wasm = testpath/"libcxx-smoke.wasm"
    source.write <<~CPP
      #include <exception>
      #include <chrono>
      #include <cstdio>
      #include <filesystem>
      #include <fstream>
      #include <locale>
      #include <random>
      #include <stdexcept>
      #include <string>
      #include <thread>
      #include <vector>

      struct base { virtual ~base() = default; };
      struct derived : base {};

      int main() {
        derived value;
        base* polymorphic = &value;
        const std::filesystem::path path("/tmp/libcxx-ok.txt");
        const std::locale locale("C.UTF-8");
        std::string result;

        if (dynamic_cast<derived*>(polymorphic) == nullptr) return 1;
        {
          std::ofstream output(path);
          if (!output) return 2;
          output << "libcxx-ok";
        }
        if (!std::filesystem::is_regular_file(path)) return 3;
        if (std::filesystem::file_size(path) != 9) return 4;
        if (!std::filesystem::remove(path)) return 5;
        if (!std::use_facet<std::ctype<wchar_t>>(locale).is(std::ctype_base::alpha, L'K')) return 6;

        const auto before = std::chrono::steady_clock::now();
        const auto after = std::chrono::steady_clock::now();
        if (after < before) return 7;

        std::random_device random;
        volatile unsigned int sample = random();
        (void)sample;

        std::thread worker([&result] {
          std::vector<int> values;
          try {
            (void)values.at(0);
          } catch (const std::out_of_range&) {
            result = "libcxx-ok";
          }
        });
        worker.join();
        if (result != "libcxx-ok") return 8;
        std::puts(result.c_str());
        return 0;
      }
    CPP

    kandelo_wasm_build do |root|
      system kandelo_tool("c++", root), source,
        "-fwasm-exceptions", "--kandelo-thread-slots=1",
        "-nostdinc++", "-isystem", include/"c++/v1",
        "-L#{lib}", "-lc++", "-lc++abi", "-o", wasm
    end
    assert_equal "libcxx-ok\n", kandelo_run_wasm(wasm, [])

    side_source = testpath/"libcxx-pic-side.cpp"
    side_module = testpath/"libcxx-pic-side.so"
    loader_source = testpath/"libcxx-pic-loader.c"
    loader = testpath/"libcxx-pic-loader.wasm"
    side_source.write <<~CPP
      #include <algorithm>
      #include <cstring>
      #include <string>
      #include <typeinfo>

      struct base { virtual ~base() = default; };
      struct derived : base {};

      extern "C" int kandelo_libcxx_pic_value(char *output, unsigned int capacity) {
        derived value;
        base *polymorphic = &value;
        const std::string message("libcxx-pic-ok");
        if (dynamic_cast<derived *>(polymorphic) == nullptr || capacity <= message.size()) return 1;
        std::copy(message.begin(), message.end(), output);
        output[message.size()] = '\\0';
        return 0;
      }
    CPP
    loader_source.write <<~CPP
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <dlfcn.h>

      typedef int (*value_fn)(char *, unsigned int);

      int main(int argc, char **argv) {
        char value[32] = {};
        void *allocation;
        if (argc != 2) return 5;
        allocation = calloc(1, 1);
        if (allocation == NULL) return 6;
        free(allocation);
        void *module = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
        if (module == NULL) {
          fprintf(stderr, "dlopen: %s\\n", dlerror());
          return 1;
        }
        value_fn function = (value_fn)dlsym(module, "kandelo_libcxx_pic_value");
        if (function == NULL || function(value, sizeof(value)) != 0) return 2;
        if (strcmp(value, "libcxx-pic-ok") != 0) return 3;
        puts(value);
        return dlclose(module) == 0 ? 0 : 4;
      }
    CPP

    kandelo_wasm_build do |root|
      cxx = kandelo_tool("c++", root)
      common = ["-O2", "-fwasm-exceptions", "-nostdinc++", "-isystem", include/"c++/v1"]
      system cxx, side_source, *common, "-fPIC", "-shared", "-Wl,--export=__tls_base", "-nostdlib++",
        lib/"libc++-pic.a", lib/"libc++abi-pic.a", "-o", side_module

      nonpic_module = testpath/"libcxx-nonpic-negative.so"
      nonpic_command = [
        cxx, side_source, *common, "-fPIC", "-shared", "-Wl,--export=__tls_base", "-nostdlib++",
        lib/"libc++.a", lib/"libc++abi.a", "-o", nonpic_module
      ].shelljoin
      nonpic_output = shell_output("#{nonpic_command} 2>&1", 1)
      assert_match(/relocation R_WASM_.*recompile with -fPIC/m, nonpic_output)
      refute_path_exists nonpic_module

      side_imports = %w[
        __assert_fail abort aligned_alloc calloc fflush fprintf fputc free fwrite getenv malloc memchr memcmp
        pthread_mutex_lock pthread_mutex_unlock realloc snprintf strcmp strlen vfprintf
      ].map { |symbol| "-Wl,--undefined=#{symbol}" }
      system kandelo_cc(root), loader_source, "-O2", "-ldl", "-Wl,--export-all", *side_imports, "-o", loader
    end

    side_info = Utils.safe_popen_read("wasm-objdump", "-x", side_module)
    assert_match(/dylink\.0/, side_info)
    assert_match(/memory.*<- env\.memory/, side_info)
    assert_equal "libcxx-pic-ok\n", kandelo_run_wasm(loader, [side_module])
    guest_side = "/usr/lib/libcxx-pic-side.so"
    side_file = { guest_side => side_module }
    if kandelo_arch == "wasm32"
      assert_equal "libcxx-pic-ok\n", kandelo_run_browser_wasm(loader, [guest_side], guest_files: side_file)
    end
  end
end

__END__
diff --git a/libunwind/src/assembly.h b/libunwind/src/assembly.h
index 91ee30cd19ce..5c0c45e28179 100644
--- a/libunwind/src/assembly.h
+++ b/libunwind/src/assembly.h
@@ -222 +222,4 @@
-#elif defined(_AIX)
+#elif defined(__wasm__)
+#define NO_EXEC_STACK_DIRECTIVE
+
+#elif defined(_AIX)
