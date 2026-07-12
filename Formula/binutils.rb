require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Binutils < Formula
  include KandeloFormulaSupport

  desc "GNU binary utilities for Kandelo"
  homepage "https://www.gnu.org/software/binutils/"
  url "https://ftpmirror.gnu.org/gnu/binutils/binutils-2.46.1.tar.xz"
  mirror "https://ftp.gnu.org/gnu/binutils/binutils-2.46.1.tar.xz"
  sha256 "e127a709cba24c76de8936cb7083dd768f28cd37eb010492e2f19b71eb1294e4"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "texinfo" => :build
  depends_on "wabt" => [:build, :test]

  skip_clean "bin/addr2line", "bin/ar", "bin/c++filt", "bin/elfedit",
             "bin/nm", "bin/objcopy", "bin/objdump", "bin/ranlib",
             "bin/readelf", "bin/size", "bin/strings", "bin/strip"

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/binutils".freeze
  PROGRAMS = {
    "addr2line" => "addr2line",
    "ar"        => "ar",
    "c++filt"   => "cxxfilt",
    "elfedit"   => "elfedit",
    "nm"        => "nm-new",
    "objcopy"   => "objcopy",
    "objdump"   => "objdump",
    "ranlib"    => "ranlib",
    "readelf"   => "readelf",
    "size"      => "size",
    "strings"   => "strings",
    "strip"     => "strip-new",
  }.freeze

  patch :DATA

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      ENV["CFLAGS"] = "-O2"
      ENV["CXXFLAGS"] = "-O2"
      ENV["CC_FOR_BUILD"] = kandelo_host_cc.to_s
      ENV["CXX_FOR_BUILD"] = kandelo_host_cxx.to_s
      ENV["AR_FOR_BUILD"] = kandelo_host_tool("ar").to_s
      ENV["RANLIB_FOR_BUILD"] = kandelo_host_tool("ranlib").to_s

      # Binutils' link-only probes accept unresolved Wasm imports. Pin the
      # target decisions for APIs that are absent from Kandelo's musl.
      %w[spawnve spawnvpe fopen64 fseeko64 ftello64 pstat_getdynamic pstat_getstatic].each do |function|
        ENV["ac_cv_func_#{function}"] = "no"
      end

      system kandelo_configure,
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--target=wasm32-unknown-none",
        "--disable-dependency-tracking",
        "--disable-gas",
        "--disable-gdb",
        "--disable-gdbserver",
        "--disable-gold",
        "--disable-gprofng",
        "--disable-ld",
        "--disable-nls",
        "--disable-shared",
        "--disable-sim",
        "--disable-werror",
        "--enable-deterministic-archives",
        "--enable-static",
        "--without-system-zlib",
        "--without-zstd"
      system "make", "-j#{ENV.make_jobs}", "all-binutils"

      PROGRAMS.each_value do |source_name|
        kandelo_validate_wasm_artifact(buildpath/"binutils"/source_name, fork: :forbidden)
      end
    end

    PROGRAMS.each do |installed_name, source_name|
      kandelo_install_bin(buildpath/"binutils", source_name, installed_name)
    end
    PROGRAMS.each_key do |name|
      man1.install buildpath/"binutils/doc/#{name}.1"
    end
    info.install buildpath/"binutils/doc/binutils.info"
  end

  def caveats
    <<~EOS
      Relocatable WebAssembly objects can be inspected and archived, but objcopy
      and strip transformations are rejected until BFD can rewrite linking metadata.
      Linked modules with trailing, representable custom names support exact .wasm.*
      custom-section operations, --strip-debug, and --strip-all. Dynamic modules and
      modules with leading, interleaved, or ambiguous custom names are inspect-only.
      Address, alignment, byte, flag, and symbol rewrites are explicitly rejected.
    EOS
  end

  test do
    PROGRAMS.each_key do |name|
      assert_match(/GNU .* 2\.46\.1/,
        kandelo_run_wasm(bin/name, ["--version"]))
    end

    root = kandelo_activate_sdk!
    kandelo_activate_sysroot!(root)
    (testpath/"alpha.c").write <<~C
      const char *archive_message(void) {
        return "Kandelo binutils archive payload";
      }

      int archive_value(void) {
        return 17;
      }
    C
    (testpath/"beta.c").write <<~C
      int beta_value(void) {
        return 25;
      }
    C
    (testpath/"main.c").write <<~C
      #include <stdio.h>

      const char *archive_message(void);
      int archive_value(void);
      int beta_value(void);

      int main(void) {
        printf("%s:%d\\n", archive_message(), archive_value() + beta_value());
        return 0;
      }
    C

    %w[alpha beta].each do |name|
      system kandelo_cc(root), "-g", "-O0", "-c", testpath/"#{name}.c",
        "-o", testpath/"#{name}.o"
    end

    env = { "KERNEL_CWD" => testpath }
    assert_empty kandelo_run_wasm(
      bin/"ar", ["rcs", "libsample.a", "alpha.o", "beta.o"], env: env
    )
    assert_equal "alpha.o\nbeta.o\n",
      kandelo_run_wasm(bin/"ar", ["t", "libsample.a"], env: env)

    symbols = kandelo_run_wasm(bin/"nm", ["-g", "--defined-only", "alpha.o"], env: env)
    assert_match(/ T archive_message$/, symbols)
    assert_match(/ T archive_value$/, symbols)

    archive_map = kandelo_run_wasm(bin/"nm", ["--print-armap", "libsample.a"], env: env)
    assert_match(/^Archive index:$/, archive_map)
    assert_match(/^archive_message in alpha\.o$/, archive_map)
    assert_match(/^beta_value in beta\.o$/, archive_map)

    strings = kandelo_run_wasm(bin/"strings", ["-n", "16", "alpha.o"], env: env)
    assert_includes strings, "Kandelo binutils archive payload"

    sections = kandelo_run_wasm(bin/"objdump", ["-h", "alpha.o"], env: env)
    assert_match(/\.wasm\.data_count/, sections)
    assert_match(/\.wasm\.linking/, sections)

    object_bytes = (testpath/"alpha.o").binread
    object_output = testpath/"alpha-stripped.o"
    diagnostic = kandelo_run_wasm(
      bin/"strip", ["--strip-debug", "-o", object_output.basename, "alpha.o"],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/rewriting WebAssembly linking metadata is not supported/, diagnostic)
    refute_path_exists object_output
    assert_equal object_bytes, (testpath/"alpha.o").binread

    in_place = testpath/"alpha-in-place.o"
    in_place.binwrite(object_bytes)
    diagnostic = kandelo_run_wasm(
      bin/"strip", ["--strip-debug", in_place.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/rewriting WebAssembly linking metadata is not supported/, diagnostic)
    assert_equal object_bytes, in_place.binread

    removed_linking = testpath/"alpha-without-linking.o"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy", ["--remove-section=.wasm.linking", "alpha.o", removed_linking.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/rewriting WebAssembly linking metadata is not supported/, diagnostic)
    refute_path_exists removed_linking
    assert_equal object_bytes, (testpath/"alpha.o").binread

    binary_object = testpath/"alpha.bin"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy", ["-O", "binary", "alpha.o", binary_object.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/rewriting WebAssembly linking metadata is not supported/, diagnostic)
    refute_path_exists binary_object
    assert_equal object_bytes, (testpath/"alpha.o").binread

    tagged = testpath/"tagged.wasm"
    tagged.binwrite("AGFzbQEAAAABBQFgAX8ADQMBAAAGBgF/AEEHCw==".unpack1("m0"))
    tagged_copy = testpath/"tagged-copy.wasm"
    assert_empty kandelo_run_wasm(
      bin/"objcopy", [tagged.basename, tagged_copy.basename], env: env
    )
    system "wasm-validate", "--enable-all", tagged_copy

    {
      "aliased-custom-name" => "AGFzbQEAAAABAQAABgR0eXBlWA==",
      "nul-custom-name"     => "AGFzbQEAAAAACQdmb28AYmFyWA==",
    }.each do |name, encoded|
      source = testpath/"#{name}.wasm"
      source.binwrite(encoded.unpack1("m0"))
      source_bytes = source.binread
      system "wasm-validate", "--enable-all", source
      output = testpath/"#{name}-copy.wasm"
      diagnostic = kandelo_run_wasm(
        bin/"objcopy", [source.basename, output.basename],
        env: env, merge_stderr: true, expected_status: 1
      )
      assert_match(/custom section names BFD cannot represent unambiguously/, diagnostic)
      refute_path_exists output
      assert_equal source_bytes, source.binread
    end

    interleaved = testpath/"interleaved-custom.wasm"
    interleaved.binwrite("AGFzbQEAAAAABANmb28BAQA=".unpack1("m0"))
    interleaved_bytes = interleaved.binread
    system "wasm-validate", "--enable-all", interleaved
    interleaved_copy = testpath/"interleaved-custom-copy.wasm"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy", [interleaved.basename, interleaved_copy.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/order-sensitive custom sections is not supported/, diagnostic)
    refute_path_exists interleaved_copy
    assert_equal interleaved_bytes, interleaved.binread

    empty_custom = testpath/"empty-custom.wasm"
    empty_custom.binwrite("AGFzbQEAAAAABANmb28=".unpack1("m0"))
    empty_custom_copy = testpath/"empty-custom-copy.wasm"
    assert_empty kandelo_run_wasm(
      bin/"objcopy", [empty_custom.basename, empty_custom_copy.basename], env: env
    )
    assert_equal empty_custom.binread, empty_custom_copy.binread
    system "wasm-validate", "--enable-all", empty_custom_copy
    empty_custom_sections = kandelo_run_wasm(
      bin/"objdump", ["-h", empty_custom_copy.basename], env: env
    )
    assert_match(/\.wasm\.foo/, empty_custom_sections)

    (testpath/"shared.c").write("int kandelo_shared_value(void) { return 42; }\n")
    shared = testpath/"libkandelo-shared.wasm"
    system kandelo_cc(root), "-shared", "-fPIC", testpath/"shared.c", "-o", shared
    system "wasm-validate", "--enable-all", shared
    shared_sections = kandelo_run_wasm(bin/"objdump", ["-h", shared.basename], env: env)
    assert_match(/\.wasm\.dylink\.0/, shared_sections)
    shared_bytes = shared.binread
    shared_copy = testpath/"libkandelo-shared-copy.wasm"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy", [shared.basename, shared_copy.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/order-sensitive custom sections is not supported/, diagnostic)
    refute_path_exists shared_copy
    assert_equal shared_bytes, shared.binread

    app = testpath/"archive-app.wasm"
    system kandelo_cc(root), "-g", "-O0", testpath/"main.c", testpath/"libsample.a",
      "-o", app
    expected = "Kandelo binutils archive payload:42\n"
    assert_equal expected, kandelo_run_wasm(app, [], env: env)
    app_sections = kandelo_run_wasm(bin/"objdump", ["-h", app.basename], env: env)
    assert_match(/\.wasm\.\.debug_/, app_sections)

    app_bytes = app.binread
    symbol_stripped = testpath/"archive-app-symbol-stripped.wasm"
    diagnostic = kandelo_run_wasm(
      bin/"strip", ["--strip-symbol=archive_message", "-o", symbol_stripped.basename, app.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/requested WebAssembly transformation is not supported/, diagnostic)
    refute_path_exists symbol_stripped
    assert_equal app_bytes, app.binread

    renamed_symbol = testpath/"archive-app-renamed.wasm"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy",
      ["--redefine-sym=archive_message=renamed_message", app.basename, renamed_symbol.basename],
      env: env, merge_stderr: true, expected_status: 1,
    )
    assert_match(/requested WebAssembly transformation is not supported/, diagnostic)
    refute_path_exists renamed_symbol
    assert_equal app_bytes, app.binread

    changed_start = testpath/"archive-app-changed-start.wasm"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy", ["--set-start=1234", app.basename, changed_start.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/requested WebAssembly transformation is not supported/, diagnostic)
    refute_path_exists changed_start
    assert_equal app_bytes, app.binread

    prefixed_sections = testpath/"archive-app-prefixed-sections.wasm"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy", ["--prefix-sections=x", app.basename, prefixed_sections.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/requested WebAssembly transformation is not supported/, diagnostic)
    refute_path_exists prefixed_sections
    assert_equal app_bytes, app.binread

    converted = testpath/"archive-app.bin"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy", ["-O", "binary", app.basename, converted.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/converting WebAssembly to another output format is not supported/, diagnostic)
    refute_path_exists converted
    assert_equal app_bytes, app.binread

    binary_input = testpath/"binary-input"
    binary_input.binwrite("not a WebAssembly module\n")
    converted_to_wasm = testpath/"binary-input.wasm"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy", ["-I", "binary", "-O", "wasm", binary_input.basename, converted_to_wasm.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/converting another input format to WebAssembly is not supported/, diagnostic)
    refute_path_exists converted_to_wasm
    assert_equal "not a WebAssembly module\n", binary_input.binread

    removed_code = testpath/"archive-app-without-code.wasm"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy", ["--remove-section=.wasm.code", app.basename, removed_code.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/requested WebAssembly transformation is not supported/, diagnostic)
    refute_path_exists removed_code
    assert_equal app_bytes, app.binread

    only_section = testpath/"archive-app-only-section.wasm"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy", ["--only-section=.wasm.name", app.basename, only_section.basename],
      env: env, merge_stderr: true, expected_status: 1
    )
    assert_match(/requested WebAssembly transformation is not supported/, diagnostic)
    refute_path_exists only_section
    assert_equal app_bytes, app.binread

    payload = testpath/"custom-section.txt"
    payload.write("Kandelo binutils custom section\n")

    added_dylink = testpath/"archive-app-added-dylink.wasm"
    diagnostic = kandelo_run_wasm(
      bin/"objcopy",
      ["--add-section=.wasm.dylink.0=#{payload.basename}", app.basename, added_dylink.basename],
      env: env, merge_stderr: true, expected_status: 1,
    )
    assert_match(/requested WebAssembly transformation is not supported/, diagnostic)
    refute_path_exists added_dylink
    assert_equal app_bytes, app.binread

    sectioned = testpath/"archive-app-sectioned.wasm"
    assert_empty kandelo_run_wasm(
      bin/"objcopy",
      ["--add-section=.wasm.kandelo.test=#{payload.basename}", app.basename, sectioned.basename],
      env: env,
    )
    sectioned_sections = kandelo_run_wasm(bin/"objdump", ["-h", sectioned.basename], env: env)
    assert_match(/\.wasm\.kandelo\.test/, sectioned_sections)
    system "wasm-validate", "--enable-all", sectioned
    assert_equal expected, kandelo_run_wasm(sectioned, [], env: env)

    unsectioned = testpath/"archive-app-unsectioned.wasm"
    assert_empty kandelo_run_wasm(
      bin/"objcopy",
      ["--remove-section=.wasm.kandelo.test", sectioned.basename, unsectioned.basename],
      env: env,
    )
    unsectioned_sections = kandelo_run_wasm(bin/"objdump", ["-h", unsectioned.basename], env: env)
    refute_match(/\.wasm\.kandelo\.test/, unsectioned_sections)
    system "wasm-validate", "--enable-all", unsectioned
    assert_equal expected, kandelo_run_wasm(unsectioned, [], env: env)

    stripped = testpath/"archive-app-stripped.wasm"
    assert_empty kandelo_run_wasm(
      bin/"strip", ["--strip-debug", "-o", stripped.basename, app.basename], env: env
    )
    assert_operator stripped.size, :<, app.size
    stripped_sections = kandelo_run_wasm(bin/"objdump", ["-h", stripped.basename], env: env)
    refute_match(/\.wasm\.\.debug_/, stripped_sections)
    assert_match(/\.wasm\.name/, stripped_sections)
    assert_match(/\.wasm\.producers/, stripped_sections)
    assert_match(/\.wasm\.target_features/, stripped_sections)
    system "wasm-validate", "--enable-all", stripped
    assert_equal expected, kandelo_run_wasm(stripped, [], env: env)

    fully_stripped = testpath/"archive-app-strip-all.wasm"
    assert_empty kandelo_run_wasm(
      bin/"strip", ["--strip-all", "-o", fully_stripped.basename, app.basename], env: env
    )
    assert_operator fully_stripped.size, :<, stripped.size
    fully_stripped_sections = kandelo_run_wasm(
      bin/"objdump", ["-h", fully_stripped.basename], env: env
    )
    refute_match(/\.wasm\.\.debug_/, fully_stripped_sections)
    refute_match(/\.wasm\.name/, fully_stripped_sections)
    refute_match(/\.wasm\.producers/, fully_stripped_sections)
    assert_match(/\.wasm\.target_features/, fully_stripped_sections)
    system "wasm-validate", "--enable-all", fully_stripped
    assert_equal expected, kandelo_run_wasm(fully_stripped, [], env: env)

    kept_name = testpath/"archive-app-kept-name.wasm"
    assert_empty kandelo_run_wasm(
      bin/"strip",
      ["--strip-all", "--keep-section=.wasm.name", "-o", kept_name.basename, app.basename],
      env: env,
    )
    kept_name_sections = kandelo_run_wasm(bin/"objdump", ["-h", kept_name.basename], env: env)
    assert_match(/\.wasm\.name/, kept_name_sections)
    refute_match(/\.wasm\.producers/, kept_name_sections)
    assert_match(/\.wasm\.target_features/, kept_name_sections)
    system "wasm-validate", "--enable-all", kept_name
    assert_equal expected, kandelo_run_wasm(kept_name, [], env: env)

    PROGRAMS.each_key do |name|
      assert_path_exists bin/name
      assert_path_exists man1/"#{name}.1"
    end
    assert_path_exists info/"binutils.info"
  end
end

__END__
--- a/bfd/wasm-module.c
+++ b/bfd/wasm-module.c
@@ -21,8 +21,8 @@
    MA 02110-1301, USA.  */

 /* The WebAssembly module format is a simple object file format
-   including up to 11 numbered sections, plus any number of named
-   "custom" sections. It is described at:
+   including numbered sections plus any number of named "custom"
+   sections. It is described at:
    https://github.com/WebAssembly/design/blob/master/BinaryEncoding.md. */

 #include "sysdep.h"
@@ -40,6 +40,9 @@
 {
   asymbol *      symbols;
   bfd_size_type  symcount;
+  bool           has_linking_metadata;
+  bool           has_unrewritable_custom_names;
+  bool           has_order_sensitive_custom_sections;
 } tdata_type;

 static const char * const wasm_numbered_sections[] =
@@ -56,10 +59,35 @@
   WASM_SECTION ( 9, "element"),
   WASM_SECTION (10, "code"),
   WASM_SECTION (11, "data"),
+  WASM_SECTION (12, "data_count"),
+  WASM_SECTION (13, "tag"),
 };

 #define WASM_NUMBERED_SECTIONS ARRAY_SIZE (wasm_numbered_sections)

+/* Tag and DataCount do not follow numeric order in the binary format.  */
+static const unsigned int wasm_numbered_section_order[] =
+{
+  1, 2, 3, 4, 5, 13, 6, 7, 8, 9, 12, 10, 11
+};
+
+#define WASM_LINKING_VERSION 2
+#define WASM_SYMBOL_TABLE_SUBSECTION 8
+
+#define WASM_SYMBOL_FUNCTION 0
+#define WASM_SYMBOL_DATA 1
+#define WASM_SYMBOL_GLOBAL 2
+#define WASM_SYMBOL_SECTION 3
+#define WASM_SYMBOL_TAG 4
+#define WASM_SYMBOL_TABLE 5
+#define WASM_SYMBOL_KIND_COUNT 6
+
+#define WASM_SYMBOL_BINDING_WEAK 0x1
+#define WASM_SYMBOL_BINDING_LOCAL 0x2
+#define WASM_SYMBOL_UNDEFINED 0x10
+#define WASM_SYMBOL_EXPLICIT_NAME 0x40
+#define WASM_SYMBOL_ABSOLUTE 0x200
+
 /* Resolve SECTION_CODE to a section name if there is one, NULL
    otherwise.  */

@@ -188,6 +216,15 @@
     }									\
   while (0)

+#define READ_SLEB128(x, p, end)                                       \
+  do                                                                  \
+    {                                                                 \
+      if ((p) >= (end))                                               \
+        goto error_return;                                            \
+      (x) = _bfd_safe_read_leb128 (abfd, &(p), true, (end));          \
+    }                                                                 \
+  while (0)
+
 /* Verify the magic number at the beginning of a WebAssembly module
    ABFD, setting ERRORPTR if there's a mismatch.  */

@@ -234,9 +271,571 @@
     return false;

   if (! wasm_read_version (abfd, errorptr))
+    return false;
+
+  return true;
+}
+
+struct wasm_import_names
+{
+  const char **names[WASM_SYMBOL_KIND_COUNT];
+  size_t counts[WASM_SYMBOL_KIND_COUNT];
+};
+
+/* LLVM object files carry symbols in the version 2 "linking" custom
+   section.  Decode that table into BFD symbols so nm and archive maps see
+   the same definitions as the WebAssembly linker.  */
+
+static bool
+wasm_read_string (bfd *abfd, bfd_byte **cursor, bfd_byte *end,
+                  const char **namep)
+{
+  bfd_byte *p = *cursor;
+  bfd_vma len;
+  char *name;
+
+  READ_LEB128 (len, p, end);
+  if (len > (size_t) (end - p))
+    goto error_return;
+
+  name = bfd_alloc (abfd, len + 1);
+  if (name == NULL)
+    goto error_return;
+  memcpy (name, p, len);
+  name[len] = 0;
+  p += len;
+
+  *cursor = p;
+  *namep = name;
+  return true;
+
+ error_return:
+  return false;
+}
+
+static bool
+wasm_skip_string (bfd *abfd, bfd_byte **cursor, bfd_byte *end)
+{
+  bfd_byte *p = *cursor;
+  bfd_vma len;
+
+  READ_LEB128 (len, p, end);
+  if (len > (size_t) (end - p))
+    goto error_return;
+  p += len;
+  *cursor = p;
+  return true;
+
+ error_return:
+  return false;
+}
+
+static bool
+wasm_skip_value_type (bfd *abfd, bfd_byte **cursor, bfd_byte *end)
+{
+  bfd_byte *p = *cursor;
+  bfd_vma ignored;
+  bfd_byte type;
+
+  if (p >= end)
+    goto error_return;
+  type = *p++;
+  if (type == 0x63 || type == 0x64)
+    READ_SLEB128 (ignored, p, end);
+  *cursor = p;
+  return true;
+
+ error_return:
+  return false;
+}
+
+static bool
+wasm_skip_limits (bfd *abfd, bfd_byte **cursor, bfd_byte *end)
+{
+  bfd_byte *p = *cursor;
+  bfd_vma flags;
+  bfd_vma ignored;
+
+  READ_LEB128 (flags, p, end);
+  READ_LEB128 (ignored, p, end);
+  if (flags & 1)
+    READ_LEB128 (ignored, p, end);
+  if (flags & 8)
+    READ_LEB128 (ignored, p, end);
+  *cursor = p;
+  return true;
+
+ error_return:
+  return false;
+}
+
+static bool
+wasm_read_import_names (bfd *abfd, struct wasm_import_names *imports)
+{
+  sec_ptr section;
+  bfd_byte *p;
+  bfd_byte *end;
+  bfd_vma import_count;
+  bfd_vma i;
+  size_t kind;
+  size_t amt;
+
+  memset (imports, 0, sizeof (*imports));
+  section = bfd_get_section_by_name (abfd, WASM_SECTION (2, "import"));
+  if (section == NULL || section->contents == NULL)
+    return true;
+
+  p = section->contents;
+  end = p + section->size;
+  READ_LEB128 (import_count, p, end);
+
+  if (_bfd_mul_overflow (import_count, sizeof (const char *), &amt))
+    {
+      bfd_set_error (bfd_error_file_too_big);
+      return false;
+    }
+  for (kind = 0; kind < WASM_SYMBOL_KIND_COUNT; kind++)
+    {
+      imports->names[kind] = bfd_zalloc (abfd, amt);
+      if (imports->names[kind] == NULL && amt != 0)
+        return false;
+    }
+
+  for (i = 0; i < import_count; i++)
+    {
+      const char *field_name;
+      bfd_byte external_kind;
+      size_t symbol_kind;
+      bfd_vma ignored;
+
+      if (!wasm_skip_string (abfd, &p, end)
+          || !wasm_read_string (abfd, &p, end, &field_name)
+          || p >= end)
+        goto error_return;
+
+      external_kind = *p++;
+      symbol_kind = WASM_SYMBOL_KIND_COUNT;
+      switch (external_kind)
+        {
+        case 0:
+          symbol_kind = WASM_SYMBOL_FUNCTION;
+          READ_LEB128 (ignored, p, end);
+          break;
+        case 1:
+          symbol_kind = WASM_SYMBOL_TABLE;
+          if (!wasm_skip_value_type (abfd, &p, end)
+              || !wasm_skip_limits (abfd, &p, end))
+            goto error_return;
+          break;
+        case 2:
+          if (!wasm_skip_limits (abfd, &p, end))
+            goto error_return;
+          break;
+        case 3:
+          symbol_kind = WASM_SYMBOL_GLOBAL;
+          if (!wasm_skip_value_type (abfd, &p, end) || p >= end)
+            goto error_return;
+          p++;
+          break;
+        case 4:
+          symbol_kind = WASM_SYMBOL_TAG;
+          READ_LEB128 (ignored, p, end);
+          READ_LEB128 (ignored, p, end);
+          break;
+        default:
+          goto error_return;
+        }
+
+      if (symbol_kind != WASM_SYMBOL_KIND_COUNT)
+        imports->names[symbol_kind][imports->counts[symbol_kind]++] = field_name;
+    }
+
+  if (p != end)
+    goto error_return;
+  return true;
+
+ error_return:
+  return false;
+}
+
+static bool
+wasm_read_code_offsets (bfd *abfd, bfd_vma **offsetsp, size_t *countp)
+{
+  sec_ptr section;
+  bfd_byte *start;
+  bfd_byte *p;
+  bfd_byte *end;
+  bfd_vma count;
+  bfd_vma body_size;
+  bfd_vma i;
+  bfd_vma *offsets;
+  size_t amt;
+
+  *offsetsp = NULL;
+  *countp = 0;
+  section = bfd_get_section_by_name (abfd, WASM_SECTION (10, "code"));
+  if (section == NULL || section->contents == NULL)
+    return true;
+
+  start = section->contents;
+  p = start;
+  end = p + section->size;
+  READ_LEB128 (count, p, end);
+  if (_bfd_mul_overflow (count, sizeof (*offsets), &amt))
+    {
+      bfd_set_error (bfd_error_file_too_big);
+      return false;
+    }
+  offsets = bfd_alloc (abfd, amt);
+  if (offsets == NULL && amt != 0)
+    return false;
+
+  for (i = 0; i < count; i++)
+    {
+      offsets[i] = p - start;
+      READ_LEB128 (body_size, p, end);
+      if (body_size > (size_t) (end - p))
+        goto error_return;
+      p += body_size;
+    }
+  if (p != end)
+    goto error_return;
+
+  *offsetsp = offsets;
+  *countp = count;
+  return true;
+
+ error_return:
+  return false;
+}
+
+static bool
+wasm_read_data_bases (bfd *abfd, bfd_vma **basesp, size_t *countp)
+{
+  sec_ptr section;
+  bfd_byte *p;
+  bfd_byte *end;
+  bfd_vma count;
+  bfd_vma i;
+  bfd_vma *bases;
+  size_t amt;
+
+  *basesp = NULL;
+  *countp = 0;
+  section = bfd_get_section_by_name (abfd, WASM_SECTION (11, "data"));
+  if (section == NULL || section->contents == NULL)
+    return true;
+
+  p = section->contents;
+  end = p + section->size;
+  READ_LEB128 (count, p, end);
+  if (_bfd_mul_overflow (count, sizeof (*bases), &amt))
+    {
+      bfd_set_error (bfd_error_file_too_big);
+      return false;
+    }
+  bases = bfd_zalloc (abfd, amt);
+  if (bases == NULL && amt != 0)
+    return false;
+
+  for (i = 0; i < count; i++)
+    {
+      bfd_vma flags;
+      bfd_vma ignored;
+      bfd_vma base;
+      bfd_vma size;
+
+      READ_LEB128 (flags, p, end);
+      if (flags > 2)
+        goto error_return;
+      if (flags == 2)
+        READ_LEB128 (ignored, p, end);
+      if ((flags & 1) == 0)
+        {
+          if (p >= end || *p++ != 0x41)
+            goto error_return;
+          READ_SLEB128 (base, p, end);
+          if (p >= end || *p++ != 0x0b)
+            goto error_return;
+          bases[i] = base;
+        }
+
+      READ_LEB128 (size, p, end);
+      if (size > (size_t) (end - p))
+        goto error_return;
+      p += size;
+    }
+  if (p != end)
+    goto error_return;
+
+  *basesp = bases;
+  *countp = count;
+  return true;
+
+ error_return:
+  return false;
+}
+
+static sec_ptr
+wasm_section_by_index (bfd *abfd, bfd_vma index)
+{
+  sec_ptr section;
+  bfd_vma i = 0;
+
+  for (section = abfd->sections; section != NULL; section = section->next, i++)
+    if (i == index)
+      return section;
+  return NULL;
+}
+
+static bool
+wasm_scan_linking_symbol_section (bfd *abfd, sec_ptr linking_section)
+{
+  tdata_type *tdata = abfd->tdata.any;
+  struct wasm_import_names imports;
+  bfd_vma *code_offsets;
+  bfd_vma *data_bases;
+  size_t code_count;
+  size_t data_count;
+  bfd_byte *p;
+  bfd_byte *end;
+  bfd_byte *symbols_end = NULL;
+  bfd_vma version;
+  bfd_vma symbol_count = 0;
+  bfd_vma symbol_index = 0;
+  asymbol *symbols = NULL;
+  sec_ptr function_space;
+  sec_ptr data_space;
+  sec_ptr global_space;
+  sec_ptr table_space;
+  sec_ptr tag_space;
+  size_t amt;
+
+  if (linking_section->contents == NULL)
     return false;
+  p = linking_section->contents;
+  end = p + linking_section->size;
+  READ_LEB128 (version, p, end);
+  if (version != WASM_LINKING_VERSION)
+    goto error_return;
+
+  while (p < end)
+    {
+      bfd_byte subsection_kind;
+      bfd_vma payload_size;
+      bfd_byte *payload_end;
+
+      subsection_kind = *p++;
+      READ_LEB128 (payload_size, p, end);
+      if (payload_size > (size_t) (end - p))
+        goto error_return;
+      payload_end = p + payload_size;
+      if (subsection_kind == WASM_SYMBOL_TABLE_SUBSECTION)
+        {
+          READ_LEB128 (symbol_count, p, payload_end);
+          symbols_end = payload_end;
+          break;
+        }
+      p = payload_end;
+    }
+
+  if (symbols_end == NULL)
+    {
+      tdata->symbols = NULL;
+      tdata->symcount = 0;
+      abfd->symcount = 0;
+      return true;
+    }
+  if (!wasm_read_import_names (abfd, &imports))
+    goto error_return;
+  if (!wasm_read_code_offsets (abfd, &code_offsets, &code_count))
+    goto error_return;
+  if (!wasm_read_data_bases (abfd, &data_bases, &data_count))
+    goto error_return;
+
+  if (_bfd_mul_overflow (symbol_count, sizeof (*symbols), &amt))
+    {
+      bfd_set_error (bfd_error_file_too_big);
+      return false;
+    }
+  symbols = bfd_alloc (abfd, amt);
+  if (symbols == NULL && amt != 0)
+    return false;
+
+  function_space
+    = bfd_make_section_with_flags (abfd, WASM_SECTION_FUNCTION_INDEX,
+                                   SEC_READONLY | SEC_CODE);
+  if (function_space == NULL)
+    function_space = bfd_get_section_by_name (abfd,
+                                              WASM_SECTION_FUNCTION_INDEX);
+  if (function_space == NULL)
+    goto error_return;
+
+  data_space = bfd_make_section_with_flags (abfd, WASM_SECTION_DATA_INDEX,
+                                            SEC_DATA);
+  if (data_space == NULL)
+    data_space = bfd_get_section_by_name (abfd, WASM_SECTION_DATA_INDEX);
+  if (data_space == NULL)
+    goto error_return;
+  global_space = bfd_make_section_with_flags (abfd, WASM_SECTION_GLOBAL_INDEX,
+                                              SEC_DATA);
+  if (global_space == NULL)
+    global_space = bfd_get_section_by_name (abfd, WASM_SECTION_GLOBAL_INDEX);
+  table_space = bfd_make_section_with_flags (abfd, WASM_SECTION_TABLE_INDEX,
+                                             SEC_DATA);
+  if (table_space == NULL)
+    table_space = bfd_get_section_by_name (abfd, WASM_SECTION_TABLE_INDEX);
+  tag_space = bfd_make_section_with_flags (abfd, WASM_SECTION_TAG_INDEX,
+                                           SEC_READONLY | SEC_CODE);
+  if (tag_space == NULL)
+    tag_space = bfd_get_section_by_name (abfd, WASM_SECTION_TAG_INDEX);
+  if (global_space == NULL || table_space == NULL || tag_space == NULL)
+    goto error_return;
+
+  for (symbol_index = 0; symbol_index < symbol_count; symbol_index++)
+    {
+      asymbol *symbol = &symbols[symbol_index];
+      bfd_byte kind;
+      bfd_vma flags;
+      bfd_vma index = 0;
+      bfd_vma offset = 0;
+      bfd_vma ignored_size;
+      const char *name = NULL;
+      sec_ptr section = NULL;
+      flagword bfd_flags;
+
+
+      if (p >= symbols_end)
+        goto error_return;
+      kind = *p++;
+      if (kind >= WASM_SYMBOL_KIND_COUNT)
+        goto error_return;
+      READ_LEB128 (flags, p, symbols_end);
+
+      if (kind == WASM_SYMBOL_DATA)
+        {
+          if (!wasm_read_string (abfd, &p, symbols_end, &name))
+            goto error_return;
+          if ((flags & WASM_SYMBOL_UNDEFINED) == 0)
+            {
+              READ_LEB128 (index, p, symbols_end);
+              READ_LEB128 (offset, p, symbols_end);
+              READ_LEB128 (ignored_size, p, symbols_end);
+              if (index >= data_count)
+                goto error_return;
+              if ((flags & WASM_SYMBOL_ABSOLUTE) == 0)
+                offset += data_bases[index];
+            }
+        }
+      else if (kind == WASM_SYMBOL_SECTION)
+        {
+          READ_LEB128 (index, p, symbols_end);
+          section = wasm_section_by_index (abfd, index);
+          if (section == NULL)
+            goto error_return;
+          name = section->name;
+        }
+      else
+        {
+          READ_LEB128 (index, p, symbols_end);
+          if ((flags & WASM_SYMBOL_UNDEFINED) == 0
+              || (flags & WASM_SYMBOL_EXPLICIT_NAME) != 0)
+            {
+              if (!wasm_read_string (abfd, &p, symbols_end, &name))
+                goto error_return;
+            }
+          else
+            {
+              if (index >= imports.counts[kind])
+                goto error_return;
+              name = imports.names[kind][index];
+            }
+        }
+
+      if (name == NULL)
+        goto error_return;
+
+      if ((flags & WASM_SYMBOL_BINDING_LOCAL) != 0)
+        bfd_flags = BSF_LOCAL;
+      else if ((flags & WASM_SYMBOL_BINDING_WEAK) != 0)
+        bfd_flags = BSF_WEAK;
+      else
+        bfd_flags = BSF_GLOBAL;
+
+      if (kind == WASM_SYMBOL_SECTION)
+        bfd_flags = BSF_LOCAL | BSF_SECTION_SYM;
+      else if (kind == WASM_SYMBOL_FUNCTION)
+        bfd_flags |= BSF_FUNCTION;
+      else if (kind == WASM_SYMBOL_DATA
+               && (flags & WASM_SYMBOL_BINDING_WEAK) == 0)
+        bfd_flags |= BSF_OBJECT;
+
+      if ((flags & WASM_SYMBOL_UNDEFINED) != 0)
+        {
+          section = bfd_und_section_ptr;
+          offset = 0;
+        }
+      else
+        switch (kind)
+          {
+          case WASM_SYMBOL_FUNCTION:
+            if (index < imports.counts[kind]
+                || index - imports.counts[kind] >= code_count)
+              goto error_return;
+            section = function_space;
+            offset = code_offsets[index - imports.counts[kind]];
+            break;
+          case WASM_SYMBOL_DATA:
+            section = data_space;
+            break;
+          case WASM_SYMBOL_GLOBAL:
+            if (index < imports.counts[kind])
+              goto error_return;
+            section = global_space;
+            offset = index - imports.counts[kind];
+            break;
+          case WASM_SYMBOL_TAG:
+            if (index < imports.counts[kind])
+              goto error_return;
+            section = tag_space;
+            offset = index - imports.counts[kind];
+            break;
+          case WASM_SYMBOL_TABLE:
+            if (index < imports.counts[kind])
+              goto error_return;
+            section = table_space;
+            offset = index - imports.counts[kind];
+            break;
+          case WASM_SYMBOL_SECTION:
+            offset = 0;
+            break;
+          default:
+            goto error_return;
+          }
+      if (section == NULL)
+        goto error_return;

+      symbol->the_bfd = abfd;
+      symbol->name = name;
+      symbol->value = offset;
+      symbol->flags = bfd_flags;
+      symbol->section = section;
+      symbol->udata.p = NULL;
+    }
+
+  if (p != symbols_end)
+    goto error_return;
+
+  tdata->symbols = symbols;
+  tdata->symcount = symbol_count;
+  abfd->symcount = symbol_count;
   return true;
+
+ error_return:
+  tdata->symbols = NULL;
+  tdata->symcount = 0;
+  abfd->symcount = 0;
+  return false;
 }

 /* Scan the "function" subsection of the "name" section ASECT in the
@@ -373,7 +972,9 @@

   if (bfd_read (&byte, 1, abfd) != 1)
     {
-      if (bfd_get_error () != bfd_error_file_truncated)
+      bfd_error_type error = bfd_get_error ();
+
+      if (error != bfd_error_file_truncated && error != bfd_error_no_error)
	*errorptr = true;
       return EOF;
     }
@@ -388,12 +989,14 @@
 wasm_scan (bfd *abfd)
 {
   bool error = false;
+  bool saw_custom_section = false;
   /* Fake VMAs for now. Choose 0x80000000 as base to avoid clashes
      with actual data addresses.  */
   bfd_vma vma = 0x80000000;
   int section_code;
   unsigned int bytes_read;
   asection *bfdsec;
+  ufile_ptr filesize;

   if (bfd_seek (abfd, 0, SEEK_SET) != 0)
     goto error_return;
@@ -401,17 +1004,28 @@
   if (!wasm_read_header (abfd, &error))
     goto error_return;

-  while ((section_code = wasm_read_byte (abfd, &error)) != EOF)
+  filesize = abfd->my_archive != NULL ? arelt_size (abfd) : bfd_get_size (abfd);
+  while ((filesize == 0 || (ufile_ptr) bfd_tell (abfd) < filesize)
+         && (section_code = wasm_read_byte (abfd, &error)) != EOF)
     {
       if (section_code != 0)
	{
	  const char *sname = wasm_section_code_to_name (section_code);
+	  flagword flags = SEC_HAS_CONTENTS | SEC_READONLY;

+	  if (saw_custom_section)
+	    ((tdata_type *) abfd->tdata.any)->has_order_sensitive_custom_sections = true;
	  if (!sname)
	    goto error_return;
+	  if (section_code == 10)
+	    flags |= SEC_CODE;
+	  else if (section_code == 11)
+	    {
+	      flags &= ~SEC_READONLY;
+	      flags |= SEC_DATA;
+	    }

-	  bfdsec = bfd_make_section_anyway_with_flags (abfd, sname,
-						       SEC_HAS_CONTENTS);
+	  bfdsec = bfd_make_section_anyway_with_flags (abfd, sname, flags);
	  if (bfdsec == NULL)
	    goto error_return;

@@ -427,7 +1041,9 @@
	  char *prefix = WASM_SECTION_PREFIX;
	  size_t prefixlen = strlen (prefix);
	  ufile_ptr filesize;
+	  flagword flags = SEC_HAS_CONTENTS | SEC_READONLY;

+	  saw_custom_section = true;
	  payload_len = wasm_read_leb128 (abfd, &error, &bytes_read, false);
	  if (error)
	    goto error_return;
@@ -449,9 +1065,17 @@
	  if (bfd_read (name + prefixlen, namelen, abfd) != namelen)
	    goto error_return;
	  name[prefixlen + namelen] = 0;
+	  if (strcmp (name, WASM_DYLINK_SECTION) == 0
+	      || strcmp (name, WASM_SECTION (0, "dylink.0")) == 0)
+	    ((tdata_type *) abfd->tdata.any)->has_order_sensitive_custom_sections = true;
+	  if (wasm_section_name_to_code (name) != 0
+	      || memchr (name + prefixlen, 0, namelen) != NULL)
+	    ((tdata_type *) abfd->tdata.any)->has_unrewritable_custom_names = true;
+	  if (startswith (name + prefixlen, ".debug_")
+	      || startswith (name + prefixlen, "reloc..debug_"))
+	    flags |= SEC_DEBUGGING;

-	  bfdsec = bfd_make_section_anyway_with_flags (abfd, name,
-						       SEC_HAS_CONTENTS);
+	  bfdsec = bfd_make_section_anyway_with_flags (abfd, name, flags);
	  if (bfdsec == NULL)
	    goto error_return;

@@ -574,19 +1198,48 @@
    this writes all numbered sections first, in order, then all custom
    sections, in section order.

-   The spec says that the numbered sections must appear in order of
-   their ids, but custom sections can appear in any position and any
-   order, and more than once. FIXME: support that.  */
+   The spec defines an order for numbered sections that is not numeric once
+   Tag and DataCount are present.  Custom sections can appear in any position
+   and any order, and more than once.  FIXME: support that.  */

 static bool
 wasm_compute_section_file_positions (bfd *abfd)
 {
+  tdata_type *tdata = abfd->tdata.any;
   bfd_byte magic[SIZEOF_WASM_MAGIC] = WASM_MAGIC;
   bfd_byte vers[SIZEOF_WASM_VERSION] = WASM_VERSION;
   sec_ptr numbered_sections[WASM_NUMBERED_SECTIONS];
   struct compute_section_arg fs;
   unsigned int i;

+  if ((tdata != NULL && tdata->has_linking_metadata)
+      || bfd_get_section_by_name (abfd, WASM_LINKING_SECTION) != NULL)
+    {
+      _bfd_error_handler
+        (_("%pB: rewriting WebAssembly linking metadata is not supported"),
+         abfd);
+      bfd_set_error (bfd_error_invalid_operation);
+      return false;
+    }
+
+  if (tdata != NULL && tdata->has_unrewritable_custom_names)
+    {
+      _bfd_error_handler
+        (_("%pB: rewriting custom section names BFD cannot represent unambiguously is not supported"),
+         abfd);
+      bfd_set_error (bfd_error_invalid_operation);
+      return false;
+    }
+
+  if (tdata != NULL && tdata->has_order_sensitive_custom_sections)
+    {
+      _bfd_error_handler
+        (_("%pB: rewriting modules with order-sensitive custom sections is not supported"),
+         abfd);
+      bfd_set_error (bfd_error_invalid_operation);
+      return false;
+    }
+
   if (bfd_seek (abfd, (bfd_vma) 0, SEEK_SET) != 0
       || bfd_write (magic, sizeof (magic), abfd) != (sizeof magic)
       || bfd_write (vers, sizeof (vers), abfd) != sizeof (vers))
@@ -598,9 +1251,10 @@
   bfd_map_over_sections (abfd, wasm_register_section, numbered_sections);

   fs.pos = bfd_tell (abfd);
-  for (i = 0; i < WASM_NUMBERED_SECTIONS; i++)
+  for (i = 0; i < ARRAY_SIZE (wasm_numbered_section_order); i++)
     {
-      sec_ptr sec = numbered_sections[i];
+      unsigned int section_code = wasm_numbered_section_order[i];
+      sec_ptr sec = numbered_sections[section_code];
       bfd_size_type size;

       if (! sec)
@@ -608,7 +1262,7 @@
       size = sec->size;
       if (bfd_seek (abfd, fs.pos, SEEK_SET) != 0)
	return false;
-      if (! wasm_write_uleb128 (abfd, i)
+      if (! wasm_write_uleb128 (abfd, section_code)
	  || ! wasm_write_uleb128 (abfd, size))
	return false;
       fs.pos = sec->filepos = bfd_tell (abfd);
@@ -654,6 +1308,10 @@
   bfd_byte magic[] = WASM_MAGIC;
   bfd_byte vers[] = WASM_VERSION;

+  if (!abfd->output_has_begun
+      && !wasm_compute_section_file_positions (abfd))
+    return false;
+
   if (bfd_seek (abfd, 0, SEEK_SET) != 0)
     return false;

@@ -674,6 +1332,9 @@

   tdata->symbols = NULL;
   tdata->symcount = 0;
+  tdata->has_linking_metadata = false;
+  tdata->has_unrewritable_custom_names = false;
+  tdata->has_order_sensitive_custom_sections = false;

   abfd->tdata.any = tdata;

@@ -739,6 +1400,39 @@
		      symbol_info *ret)
 {
   bfd_symbol_info (symbol, ret);
+}
+
+static bool
+wasm_bfd_copy_private_bfd_data (bfd *ibfd, bfd *obfd)
+{
+  tdata_type *itdata = ibfd->tdata.any;
+  tdata_type *otdata = obfd->tdata.any;
+
+  if (itdata == NULL
+      || (!itdata->has_linking_metadata
+          && !itdata->has_unrewritable_custom_names
+          && !itdata->has_order_sensitive_custom_sections))
+    return true;
+
+  if (otdata != NULL)
+    {
+      otdata->has_linking_metadata = itdata->has_linking_metadata;
+      otdata->has_unrewritable_custom_names = itdata->has_unrewritable_custom_names;
+      otdata->has_order_sensitive_custom_sections = itdata->has_order_sensitive_custom_sections;
+    }
+  if (itdata->has_linking_metadata)
+    _bfd_error_handler
+      (_("%pB: rewriting WebAssembly linking metadata is not supported"), ibfd);
+  else if (itdata->has_unrewritable_custom_names)
+    _bfd_error_handler
+      (_("%pB: rewriting custom section names BFD cannot represent unambiguously is not supported"),
+       ibfd);
+  else
+    _bfd_error_handler
+      (_("%pB: rewriting modules with order-sensitive custom sections is not supported"),
+       ibfd);
+  bfd_set_error (bfd_error_invalid_operation);
+  return false;
 }

 /* Check whether ABFD is a WebAssembly module; if so, scan it.  */
@@ -761,17 +1455,39 @@
   if (!wasm_mkobject (abfd))
     return NULL;

-  if (!wasm_scan (abfd)
-      || !bfd_default_set_arch_mach (abfd, bfd_arch_wasm32, 0))
+  if (!wasm_scan (abfd))
     {
       bfd_release (abfd, abfd->tdata.any);
       abfd->tdata.any = NULL;
       return NULL;
     }
+  if (!bfd_default_set_arch_mach (abfd, bfd_arch_wasm32, 0))
+    {
+      bfd_release (abfd, abfd->tdata.any);
+      abfd->tdata.any = NULL;
+      return NULL;
+    }

-  s = bfd_get_section_by_name (abfd, WASM_NAME_SECTION);
-  if (s != NULL && wasm_scan_name_function_section (abfd, s))
-    abfd->flags |= HAS_SYMS;
+  s = bfd_get_section_by_name (abfd, WASM_LINKING_SECTION);
+  if (s != NULL)
+    {
+      ((tdata_type *) abfd->tdata.any)->has_linking_metadata = true;
+      if (!wasm_scan_linking_symbol_section (abfd, s))
+        {
+          bfd_set_error (bfd_error_bad_value);
+          bfd_release (abfd, abfd->tdata.any);
+          abfd->tdata.any = NULL;
+          return NULL;
+        }
+      if (abfd->symcount != 0)
+        abfd->flags |= HAS_SYMS;
+    }
+  else
+    {
+      s = bfd_get_section_by_name (abfd, WASM_NAME_SECTION);
+      if (s != NULL && wasm_scan_name_function_section (abfd, s))
+        abfd->flags |= HAS_SYMS;
+    }

   return _bfd_no_cleanup;
 }
@@ -779,6 +1495,14 @@
 /* BFD_JUMP_TABLE_WRITE */
 #define wasm_set_arch_mach		  _bfd_generic_set_arch_mach

+/* BFD_JUMP_TABLE_COPY */
+#define wasm_bfd_merge_private_bfd_data _bfd_generic_bfd_merge_private_bfd_data
+#define wasm_bfd_copy_private_section_data _bfd_generic_bfd_copy_private_section_data
+#define wasm_bfd_copy_private_symbol_data _bfd_generic_bfd_copy_private_symbol_data
+#define wasm_bfd_copy_private_header_data _bfd_generic_bfd_copy_private_header_data
+#define wasm_bfd_set_private_flags _bfd_generic_bfd_set_private_flags
+#define wasm_bfd_print_private_bfd_data _bfd_generic_bfd_print_private_bfd_data
+
 /* BFD_JUMP_TABLE_SYMBOLS */
 #define wasm_get_symbol_version_string	  _bfd_nosymbols_get_symbol_version_string
 #define wasm_bfd_is_local_label_name	   bfd_generic_is_local_label_name
@@ -836,7 +1560,7 @@
   },

   BFD_JUMP_TABLE_GENERIC (_bfd_generic),
-  BFD_JUMP_TABLE_COPY (_bfd_generic),
+  BFD_JUMP_TABLE_COPY (wasm),
   BFD_JUMP_TABLE_CORE (_bfd_nocore),
   BFD_JUMP_TABLE_ARCHIVE (_bfd_noarchive),
   BFD_JUMP_TABLE_SYMBOLS (wasm),
--- a/bfd/wasm-module.h
+++ b/bfd/wasm-module.h
@@ -48,5 +48,9 @@

 /* The section to report wasm symbols in.  */
 #define WASM_SECTION_FUNCTION_INDEX ".space.function_index"
+#define WASM_SECTION_DATA_INDEX ".space.data_index"
+#define WASM_SECTION_GLOBAL_INDEX ".space.global_index"
+#define WASM_SECTION_TABLE_INDEX ".space.table_index"
+#define WASM_SECTION_TAG_INDEX ".space.tag_index"

 #endif /* _WASM_MODULE_H */
--- a/binutils/objcopy.c
+++ b/binutils/objcopy.c
@@ -96,6 +96,7 @@
 static bool preserve_dates;	/* Preserve input file timestamp.  */
 static int deterministic = -1;		/* Enable deterministic archives.  */
 static int status = 0;			/* Exit status.  */
+static bool unsupported_wasm_transform;

 static bool    merge_notes = false;	/* Merge note sections.  */
 static bool strip_section_headers = false;/* Strip section headers.  */
@@ -1346,12 +1347,18 @@
 /* See if a non-group section is being removed.  */

 static bool
-is_strip_section_1 (bfd *abfd ATTRIBUTE_UNUSED, asection *sec)
+is_strip_section_1 (bfd *abfd, asection *sec)
 {
   if (find_section_list (bfd_section_name (sec), false, SECTION_CONTEXT_KEEP)
       != NULL)
     return false;

+  if (strip_symbols == STRIP_ALL
+      && strcmp (bfd_get_target (abfd), "wasm") == 0
+      && (strcmp (bfd_section_name (sec), ".wasm.name") == 0
+          || strcmp (bfd_section_name (sec), ".wasm.producers") == 0))
+    return true;
+
   if (sections_removed || sections_copied)
     {
       struct section_list *p;
@@ -2664,6 +2671,104 @@
	style = bfd_coff_long_section_names (input_bfd) ? ENABLE : DISABLE;
       bfd_coff_set_long_section_names (output_bfd, style != DISABLE);
     }
+}
+
+static bool
+wasm_numbered_section_name (const char *name)
+{
+  static const char * const names[] =
+    {
+      ".wasm.type", ".wasm.import", ".wasm.function", ".wasm.table",
+      ".wasm.memory", ".wasm.tag", ".wasm.global", ".wasm.export",
+      ".wasm.start", ".wasm.element", ".wasm.data_count", ".wasm.code",
+      ".wasm.data"
+    };
+  size_t i;
+
+  for (i = 0; i < ARRAY_SIZE (names); i++)
+    if (strcmp (name, names[i]) == 0)
+      return true;
+  return false;
+}
+
+static bool
+wasm_invalid_section_option (void)
+{
+  const unsigned int supported = SECTION_CONTEXT_REMOVE | SECTION_CONTEXT_KEEP;
+  struct section_list *section;
+
+  for (section = change_sections; section != NULL; section = section->next)
+    if ((section->context & ~supported) != 0
+        || ((section->context & SECTION_CONTEXT_REMOVE) != 0
+            && (!startswith (section->pattern, ".wasm.")
+                || wasm_numbered_section_name (section->pattern)
+                || strpbrk (section->pattern, "*?[\\") != NULL)))
+      return true;
+  return false;
+}
+
+static bool
+wasm_order_sensitive_custom_section_name (const char *name)
+{
+  return (strcmp (name, ".wasm.dylink") == 0
+          || strcmp (name, ".wasm.dylink.0") == 0);
+}
+
+static bool
+wasm_invalid_custom_section_transform (void)
+{
+  struct section_add *section;
+  section_rename *rename;
+
+  for (section = add_sections; section != NULL; section = section->next)
+    if (!startswith (section->name, ".wasm.")
+        || wasm_numbered_section_name (section->name)
+        || wasm_order_sensitive_custom_section_name (section->name))
+      return true;
+  for (section = update_sections; section != NULL; section = section->next)
+    if (!startswith (section->name, ".wasm.")
+        || wasm_numbered_section_name (section->name)
+        || wasm_order_sensitive_custom_section_name (section->name))
+      return true;
+  for (rename = section_rename_list; rename != NULL; rename = rename->next)
+    if (rename->flags != (flagword) -1
+        || !startswith (rename->old_name, ".wasm.")
+        || !startswith (rename->new_name, ".wasm.")
+        || wasm_numbered_section_name (rename->old_name)
+        || wasm_numbered_section_name (rename->new_name)
+        || wasm_order_sensitive_custom_section_name (rename->new_name))
+      return true;
+  return false;
+}
+
+static bool
+wasm_unsupported_transform_requested (void)
+{
+  return (unsupported_wasm_transform
+          || wasm_invalid_section_option ()
+          || wasm_invalid_custom_section_transform ()
+          || (strip_symbols != STRIP_NONE
+           && strip_symbols != STRIP_DEBUG
+           && strip_symbols != STRIP_ALL)
+          || discard_locals != LOCALS_UNDEF
+          || localize_hidden
+          || htab_elements (strip_specific_htab) != 0
+          || htab_elements (strip_unneeded_htab) != 0
+          || htab_elements (keep_specific_htab) != 0
+          || htab_elements (localize_specific_htab) != 0
+          || htab_elements (globalize_specific_htab) != 0
+          || htab_elements (keepglobal_specific_htab) != 0
+          || htab_elements (weaken_specific_htab) != 0
+          || htab_elements (redefine_specific_htab) != 0
+          || convert_debugging
+          || change_leading_char
+          || remove_leading_char
+          || keep_file_symbols
+          || keep_section_symbols
+          || prefix_symbols_string != NULL
+          || weaken
+          || add_symbols != 0
+          || extract_symbol);
 }

 /* Copy object file IBFD onto OBFD.
@@ -2700,6 +2805,38 @@
     {
       non_fatal (_("unable to modify '%s' due to errors"),
		 bfd_get_archive_filename (ibfd));
+      return false;
+    }
+
+  if (strcmp (bfd_get_target (ibfd), "wasm") != 0
+      && strcmp (bfd_get_target (obfd), "wasm") == 0)
+    {
+      non_fatal (_("converting another input format to WebAssembly is not supported for '%s'"),
+                 bfd_get_archive_filename (ibfd));
+      return false;
+    }
+
+  if (strcmp (bfd_get_target (ibfd), "wasm") == 0
+      && bfd_get_section_by_name (ibfd, ".wasm.linking") != NULL)
+    {
+      non_fatal (_("rewriting WebAssembly linking metadata is not supported for '%s'"),
+                 bfd_get_archive_filename (ibfd));
+      return false;
+    }
+
+  if (strcmp (bfd_get_target (ibfd), "wasm") == 0
+      && strcmp (bfd_get_target (obfd), "wasm") != 0)
+    {
+      non_fatal (_("converting WebAssembly to another output format is not supported for '%s'"),
+                 bfd_get_archive_filename (ibfd));
+      return false;
+    }
+
+  if (strcmp (bfd_get_target (ibfd), "wasm") == 0
+      && wasm_unsupported_transform_requested ())
+    {
+      non_fatal (_("requested WebAssembly transformation is not supported for '%s'"),
+                 bfd_get_archive_filename (ibfd));
       return false;
     }

@@ -5372,6 +5509,38 @@
     {
       switch (c)
	{
+	case 'B':
+	case 'I':
+	case 's':
+	case 'O':
+	case 'd':
+	case 'F':
+	case 'R':
+	case 'S':
+	case 'g':
+	case 'p':
+	case 'D':
+	case 'U':
+	case 'v':
+	case 'V':
+	case OPTION_ADD_SECTION:
+	case OPTION_CHANGE_WARNINGS:
+	case OPTION_DUMP_SECTION:
+	case OPTION_FORMATS_INFO:
+	case OPTION_KEEP_SECTION:
+	case OPTION_NO_CHANGE_WARNINGS:
+	case OPTION_NO_MERGE_NOTES:
+	case OPTION_PLUGIN:
+	case OPTION_RENAME_SECTION:
+	case OPTION_UPDATE_SECTION:
+	  break;
+	default:
+	  unsupported_wasm_transform = true;
+	  break;
+	}
+
+      switch (c)
+	{
	case 'b':
	  copy_byte = atoi (optarg);
	  if (copy_byte < 0)
@@ -5891,6 +6060,7 @@
	    fl = strchr (eq, ',');
	    if (fl)
	      {
+		unsupported_wasm_transform = true;
		flags = parse_flags (fl + 1);
		len = fl - eq;
	      }
