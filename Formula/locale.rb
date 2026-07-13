require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Locale < Formula
  include KandeloFormulaSupport

  desc "Inspect musl locale settings and public locales on Kandelo"
  homepage "https://git.adelielinux.org/adelie/musl-locales/-/wikis/home"
  url "https://git.adelielinux.org/adelie/musl-locales/uploads/7e855b894b18ca4bf4ecb11b5bcbc4c1/musl-locales-0.1.0.tar.xz"
  sha256 "877c247f0d2765379efd71174a10cef787597ab5d8e4e7fe939ccb65c0abe9aa"
  license "LGPL-3.0-only"

  depends_on "binaryen" => [:build, :test]
  depends_on "wabt" => [:build, :test]

  skip_clean "bin/locale"
  patch :DATA

  def install
    kandelo_require_arch!("wasm32")
    (buildpath/"config.h").write <<~HEADER
      #define PACKAGE "musl-locales"
      #define LOCALEDIR "/usr/share/locale"
    HEADER
    artifact = buildpath/"locale.wasm"

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/musl-locales-#{version}"
      mapped_roots = {
        buildpath.to_s               => stable_source,
        root.to_s                    => "/usr/src/kandelo",
        Pathname(root).realpath.to_s => "/usr/src/kandelo",
        "/nix/store"                 => "/usr/src/toolchain",
      }
      prefix_maps = mapped_roots.uniq.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end

      system kandelo_cc,
        "-std=gnu11", "-O2", "-gline-tables-only", "-Wall", "-Wextra", "-Werror",
        "-D_GNU_SOURCE", "-I#{buildpath}",
        "-fdebug-compilation-dir=#{stable_source}", *prefix_maps,
        buildpath/"locale.c", buildpath/"categories.c", "-o", artifact
      kandelo_validate_wasm_artifact(
        artifact,
        fork:            :forbidden,
        forbidden_paths: [buildpath.to_s, prefix.to_s],
      )
    end

    kandelo_install_bin(buildpath, artifact.basename, "locale")
  end

  test do
    assert (bin/"locale").file?
    assert_equal "\0asm".b, (bin/"locale").binread(4)
    system "wasm-validate", "--enable-all", bin/"locale"

    public_locales = "C\nC.UTF-8\nPOSIX\n"
    public_env = { "MUSL_LOCPATH" => "" }
    assert_equal public_locales, kandelo_run_wasm(bin/"locale", ["-a"], env: public_env)
    assert_equal public_locales,
      kandelo_run_browser_wasm(bin/"locale", ["-a"], env: public_env)

    assert_equal "ASCII\nUTF-8\n", kandelo_run_wasm(bin/"locale", ["-m"])
    {
      "C"       => "ASCII\n",
      "C.UTF-8" => "UTF-8\n",
    }.each do |locale_name, expected|
      env = { "LC_ALL" => locale_name }
      assert_equal expected, kandelo_run_wasm(bin/"locale", ["charmap"], env: env)
      assert_equal expected, kandelo_run_browser_wasm(bin/"locale", ["charmap"], env: env)
    end

    categories = <<~OUTPUT
      LC_CTYPE
      charmap="ASCII"
      LC_NUMERIC
      decimal_point="."
      thousands_sep=""
      grouping=-1
      LC_TIME
      abday="Sun;Mon;Tue;Wed;Thu;Fri;Sat"
      day="Sunday;Monday;Tuesday;Wednesday;Thursday;Friday;Saturday"
      abmon="Jan;Feb;Mar;Apr;May;Jun;Jul;Aug;Sep;Oct;Nov;Dec"
      ab_alt_mon="Jan;Feb;Mar;Apr;May;Jun;Jul;Aug;Sep;Oct;Nov;Dec"
      mon="January;February;March;April;May;June;July;August;September;October;November;December"
      alt_mon="January;February;March;April;May;June;July;August;September;October;November;December"
      d_t_fmt="%a %b %e %T %Y"
      d_fmt="%m/%d/%y"
      t_fmt="%H:%M:%S"
      am_pm="AM;PM"
      t_fmt_ampm="%I:%M:%S %p"
      era=""
      era_d_fmt="%m/%d/%y"
      era_t_fmt="%H:%M:%S"
      era_d_t_fmt="%a %b %e %T %Y"
      alt_digits="0123456789"
      LC_COLLATE
      LC_MONETARY
      int_curr_symbol=""
      currency_symbol=""
      mon_decimal_point=""
      mon_thousands_sep=""
      mon_grouping=-1
      positive_sign=""
      negative_sign=""
      int_frac_digits=-1
      frac_digits=-1
      p_cs_precedes=-1
      p_sep_by_space=-1
      n_cs_precedes=-1
      n_sep_by_space=-1
      p_sign_posn=-1
      n_sign_posn=-1
      int_p_cs_precedes=-1
      int_p_sep_by_space=-1
      int_n_cs_precedes=-1
      int_n_sep_by_space=-1
      int_p_sign_posn=-1
      int_n_sign_posn=-1
      LC_MESSAGES
      yesexpr="^[yY]"
      noexpr="^[nN]"
      yesstr="yes"
      nostr="no"
    OUTPUT
    category_args = %w[
      -ck LC_CTYPE LC_NUMERIC LC_TIME LC_COLLATE LC_MONETARY LC_MESSAGES
    ]
    c_env = { "LC_ALL" => "C" }
    assert_equal categories, kandelo_run_wasm(bin/"locale", category_args, env: c_env)
    assert_equal categories,
      kandelo_run_browser_wasm(bin/"locale", category_args, env: c_env)

    alt_months = <<~OUTPUT
      alt_mon="January;February;March;April;May;June;July;August;September;October;November;December"
      ab_alt_mon="Jan;Feb;Mar;Apr;May;Jun;Jul;Aug;Sep;Oct;Nov;Dec"
    OUTPUT
    assert_equal alt_months,
      kandelo_run_wasm(bin/"locale", %w[-k alt_mon ab_alt_mon], env: c_env)

    locale_files = testpath/"locale-files"
    (locale_files/"one").mkpath
    (locale_files/"two").mkpath
    catalog = locale_files/"catalog.mo"
    catalog.binwrite(
      [0x950412de, 0, 1, 28, 36, 0, 0].pack("V7") +
      [3, 44].pack("V2") + [3, 48].pack("V2") + "Sun\0Dim\0",
    )
    guest_files = {
      "/locales/one/.hidden"     => catalog,
      "/locales/one/fr_FR"       => catalog,
      "/locales/one/shared"      => catalog,
      "/locales/one/#{"x" * 24}" => catalog,
      "/locales/two/de_DE"       => catalog,
      "/locales/two/shared"      => catalog,
    }
    search_env = { "MUSL_LOCPATH" => "/locales/one:/missing:/locales/two" }
    searched_locales = "C\nC.UTF-8\nPOSIX\nde_DE\nfr_FR\nshared\n"
    assert_equal searched_locales,
      kandelo_run_wasm(bin/"locale", ["-a"], env: search_env, guest_files: guest_files)
    assert_equal searched_locales,
      kandelo_run_browser_wasm(bin/"locale", ["-a"], env: search_env, guest_files: guest_files)

    selected_days = "abday=\"Dim;Mon;Tue;Wed;Thu;Fri;Sat\"\n"
    selected_env = { "LANG" => "C", "LC_TIME" => "fr_FR", "MUSL_LOCPATH" => "/locales/one" }
    assert_equal selected_days,
      kandelo_run_wasm(bin/"locale", %w[-k abday], env: selected_env, guest_files: guest_files)
    assert_equal selected_days,
      kandelo_run_browser_wasm(bin/"locale", %w[-k abday], env: selected_env, guest_files: guest_files)

    long_component = "/#{"p" * 248}"
    assert_equal 249, long_component.bytesize
    long_files = { "#{long_component}/fr_FR" => catalog }
    long_env = { "LANG" => "C", "LC_TIME" => "fr_FR", "MUSL_LOCPATH" => long_component }
    assert_equal public_locales,
      kandelo_run_wasm(bin/"locale", ["-a"], env: long_env, guest_files: long_files)
    assert_equal public_locales,
      kandelo_run_browser_wasm(bin/"locale", ["-a"], env: long_env, guest_files: long_files)
    assert_equal "abday=\"Sun;Mon;Tue;Wed;Thu;Fri;Sat\"\n",
      kandelo_run_wasm(bin/"locale", %w[-k abday], env: long_env, guest_files: long_files)

    link_dir = locale_files/"links"
    link_dir.mkpath
    (link_dir/"real-locale").binwrite(catalog.binread)
    File.symlink("real-locale", link_dir/"alias-locale")
    linked_locales = "C\nC.UTF-8\nPOSIX\nalias-locale\nreal-locale\n"
    assert_equal linked_locales, kandelo_run_wasm(
      bin/"locale", ["-a"],
      env:                       { "MUSL_LOCPATH" => "/host-locales" },
      writable_host_directories: { "/host-locales" => link_dir }
    )
    assert_equal selected_days, kandelo_run_wasm(
      bin/"locale", %w[-k abday],
      env:                       { "LC_TIME" => "alias-locale", "MUSL_LOCPATH" => "/host-locales" },
      writable_host_directories: { "/host-locales" => link_dir }
    )

    root_locale = locale_files/"root-locale"
    root_locale.binwrite(catalog.binread)
    root_files = guest_files.merge("/root-locale" => root_locale)
    root_env = { "MUSL_LOCPATH" => ":/locales/one" }
    assert_includes(
      kandelo_run_wasm(bin/"locale", ["-a"], env: root_env, guest_files: root_files).lines,
      "root-locale\n",
    )
    assert_includes(
      kandelo_run_browser_wasm(bin/"locale", ["-a"], env: root_env, guest_files: root_files).lines,
      "root-locale\n",
    )

    mixed = <<~OUTPUT
      LANG=C
      LC_CTYPE=C.UTF-8
      LC_NUMERIC="C"
      LC_TIME="C"
      LC_COLLATE="C"
      LC_MONETARY="C"
      LC_MESSAGES="C"
      LC_ALL=
    OUTPUT
    mixed_env = { "LANG" => "C", "LC_CTYPE" => "C.UTF-8", "LC_ALL" => "" }
    assert_equal mixed, kandelo_run_wasm(bin/"locale", [], env: mixed_env)
    assert_equal mixed, kandelo_run_browser_wasm(bin/"locale", [], env: mixed_env)

    roundtrip_script = testpath/"locale-roundtrip.sh"
    roundtrip_script.write <<~SH
      set -eu
      . "$1"
      case "$2" in
        LANG) value=$LANG ;;
        LC_TIME) value=$LC_TIME ;;
        LC_ALL) value=$LC_ALL ;;
        *) exit 2 ;;
      esac
      printf %s "$value"
    SH
    unusual_value = ["61207e3b246022275c0a62"].pack("H*")
    explicit_value = "'#{unusual_value.split("'").join("'\\''")}'"
    roundtrip_cases = {
      "LANG"    => { "LANG" => unusual_value, "LC_ALL" => "" },
      "LC_TIME" => { "LANG" => "C", "LC_TIME" => unusual_value, "LC_ALL" => "" },
      "LC_ALL"  => { "LANG" => "C", "LC_ALL" => unusual_value },
    }
    roundtrip_cases.each do |variable, env|
      node_output = kandelo_run_wasm(bin/"locale", [], argv0: "/bin/locale", env: env)
      browser_output = kandelo_run_browser_wasm(bin/"locale", [], env: env)
      assert_equal node_output, browser_output
      assignment = "#{variable}=#{explicit_value}\n"
      if variable == "LANG"
        assert node_output.start_with?(assignment)
      elsif variable == "LC_ALL"
        assert node_output.end_with?(assignment)
      else
        assert_includes node_output, assignment
      end

      [node_output, browser_output].each_with_index do |output, index|
        assignments = testpath/"#{variable.downcase}-#{index}.sh"
        assignments.binwrite(output)
        command = ["/bin/sh", roundtrip_script, assignments, variable]
                  .map { |arg| Shellwords.escape(arg.to_s) }.join(" ")
        assert_equal unusual_value, shell_output(command)
      end
    end

    unknown = kandelo_run_wasm(
      bin/"locale", ["not_a_keyword"], merge_stderr: true, expected_status: 1
    )
    assert_match "locale: unknown name: not_a_keyword", unknown
  end
end

__END__
diff --git a/categories.c b/categories.c
index 29a9b57..ca38774 100644
--- a/categories.c
+++ b/categories.c
@@ -11,0 +12 @@
+#include <stddef.h>
@@ -14,0 +16,13 @@
+#define NL_STRING(item, name) \
+    {item, name, CAT_TYPE_STRING, 0, 0, 0}
+#define NL_ARRAY(first, last, name) \
+    {first, name, CAT_TYPE_STRINGARRAY, first, last, 0}
+#define LCONV_STRING(member) \
+    {0, #member, CAT_TYPE_LCONV_STRING, 0, 0, offsetof(struct lconv, member)}
+#define LCONV_GROUPING(member) \
+    {0, #member, CAT_TYPE_LCONV_GROUPING, 0, 0, offsetof(struct lconv, member)}
+#define LCONV_BYTE(member) \
+    {0, #member, CAT_TYPE_LCONV_BYTE, 0, 0, offsetof(struct lconv, member)}
+#define CAT_END \
+    {0, "", CAT_TYPE_END, 0, 0, 0}
+
@@ -17,14 +31,17 @@ struct cat_item lc_time_cats [] =
-    {ABDAY_1,"abday",CAT_TYPE_STRINGARRAY,ABDAY_1,ABDAY_7},
-    {DAY_1,"day",CAT_TYPE_STRINGARRAY,DAY_1,DAY_7},
-    {ABMON_1,"abmon",CAT_TYPE_STRINGARRAY,ABMON_1,ABMON_12},
-    {MON_1,"mon",CAT_TYPE_STRINGARRAY,MON_1,MON_12},
-    {AM_STR,"am_pm", CAT_TYPE_STRINGARRAY,AM_STR,PM_STR},
-    {D_T_FMT,"d_t_fmt", CAT_TYPE_STRING,0,0},
-    {D_FMT,"d_fmt", CAT_TYPE_STRING,0,0},
-    {T_FMT,"t_fmt", CAT_TYPE_STRING,0,0},
-    {ERA,"era", CAT_TYPE_STRING,0,0},
-    {ERA_D_FMT,"era_d_fmt", CAT_TYPE_STRING,0,0},
-    {ALT_DIGITS,"alt_digits", CAT_TYPE_STRING,0,0},
-    {ERA_D_T_FMT,"era_d_t_fmt", CAT_TYPE_STRING,0,0},
-    {ERA_T_FMT,"era_t_fmt", CAT_TYPE_STRING,0,0},
-    {0,"", CAT_TYPE_END,0,0}
+    NL_ARRAY(ABDAY_1, ABDAY_7, "abday"),
+    NL_ARRAY(DAY_1, DAY_7, "day"),
+    NL_ARRAY(ABMON_1, ABMON_12, "abmon"),
+    NL_ARRAY(ABALTMON_1, ABALTMON_12, "ab_alt_mon"),
+    NL_ARRAY(MON_1, MON_12, "mon"),
+    NL_ARRAY(ALTMON_1, ALTMON_12, "alt_mon"),
+    NL_STRING(D_T_FMT, "d_t_fmt"),
+    NL_STRING(D_FMT, "d_fmt"),
+    NL_STRING(T_FMT, "t_fmt"),
+    NL_ARRAY(AM_STR, PM_STR, "am_pm"),
+    NL_STRING(T_FMT_AMPM, "t_fmt_ampm"),
+    NL_STRING(ERA, "era"),
+    NL_STRING(ERA_D_FMT, "era_d_fmt"),
+    NL_STRING(ERA_T_FMT, "era_t_fmt"),
+    NL_STRING(ERA_D_T_FMT, "era_d_t_fmt"),
+    NL_STRING(ALT_DIGITS, "alt_digits"),
+    CAT_END
@@ -35,2 +52,2 @@ struct cat_item lc_ctype_cats [] =
-    {CODESET, "charmap", CAT_TYPE_STRING,0,0},
-    {0,"", CAT_TYPE_END,0,0}
+    NL_STRING(CODESET, "charmap"),
+    CAT_END
@@ -41,2 +58,2 @@ struct cat_item lc_messages_cats [] =
-    {YESEXPR, "yesexpr",CAT_TYPE_STRING,0,0},
-    {NOEXPR, "noexpr",CAT_TYPE_STRING,0,0},
+    NL_STRING(YESEXPR, "yesexpr"),
+    NL_STRING(NOEXPR, "noexpr"),
@@ -44,2 +61,2 @@ struct cat_item lc_messages_cats [] =
-    {YESSTR, "yesstr",CAT_TYPE_STRING,0,0},
-    {NOSTR, "nostr",CAT_TYPE_STRING,0,0},
+    NL_STRING(YESSTR, "yesstr"),
+    NL_STRING(NOSTR, "nostr"),
@@ -47 +64 @@ struct cat_item lc_messages_cats [] =
-    {0,"", CAT_TYPE_END,0,0}
+    CAT_END
@@ -52,3 +69,4 @@ struct cat_item lc_numeric_cats [] =
-    {RADIXCHAR, "decimal_point",CAT_TYPE_STRING,0,0},
-    {THOUSEP, "thousands_sep",CAT_TYPE_STRING,0,0},
-    {0,"", CAT_TYPE_END,0,0}
+    LCONV_STRING(decimal_point),
+    LCONV_STRING(thousands_sep),
+    LCONV_GROUPING(grouping),
+    CAT_END
@@ -59,2 +77,22 @@ struct cat_item lc_monetary_cats [] =
-    {CRNCYSTR, "crncystr",CAT_TYPE_STRING,0,0},
-    {0,"", CAT_TYPE_END,0,0}
+    LCONV_STRING(int_curr_symbol),
+    LCONV_STRING(currency_symbol),
+    LCONV_STRING(mon_decimal_point),
+    LCONV_STRING(mon_thousands_sep),
+    LCONV_GROUPING(mon_grouping),
+    LCONV_STRING(positive_sign),
+    LCONV_STRING(negative_sign),
+    LCONV_BYTE(int_frac_digits),
+    LCONV_BYTE(frac_digits),
+    LCONV_BYTE(p_cs_precedes),
+    LCONV_BYTE(p_sep_by_space),
+    LCONV_BYTE(n_cs_precedes),
+    LCONV_BYTE(n_sep_by_space),
+    LCONV_BYTE(p_sign_posn),
+    LCONV_BYTE(n_sign_posn),
+    LCONV_BYTE(int_p_cs_precedes),
+    LCONV_BYTE(int_p_sep_by_space),
+    LCONV_BYTE(int_n_cs_precedes),
+    LCONV_BYTE(int_n_sep_by_space),
+    LCONV_BYTE(int_p_sign_posn),
+    LCONV_BYTE(int_n_sign_posn),
+    CAT_END
@@ -65 +103 @@ struct cat_item lc_collate_cats [] =
-    {0,"", CAT_TYPE_END,0,0}
+    CAT_END
@@ -78 +116 @@ struct cat_item* cats[] =
-const struct cat_item get_cat_item_for_name(const char *name)
+struct cat_item get_cat_item_for_name(const char *name)
@@ -80 +118 @@ const struct cat_item get_cat_item_for_name(const char *name)
-    struct cat_item invalid = {0,"", CAT_TYPE_END,0,0};
+    struct cat_item invalid = {0,"", CAT_TYPE_END,0,0,0};
diff --git a/categories.h b/categories.h
index a027020..3a4f989 100644
--- a/categories.h
+++ b/categories.h
@@ -15,0 +16 @@
+#include <stddef.h>
@@ -19,6 +20,4 @@
-#define CAT_TYPE_STRINGLIST 2
-#define CAT_TYPE_BYTE 3
-#define CAT_TYPE_BYTEARRAY 4
-#define CAT_TYPE_WORD 4
-#define CAT_TYPE_WORDARRAY 5
-#define CAT_TYPE_END 6
+#define CAT_TYPE_LCONV_STRING 2
+#define CAT_TYPE_LCONV_GROUPING 3
+#define CAT_TYPE_LCONV_BYTE 4
+#define CAT_TYPE_END 5
@@ -36,0 +36 @@ struct cat_item
+    size_t offset;
@@ -38 +38 @@ struct cat_item
-const struct cat_item get_cat_item_for_name(const char* name);
+struct cat_item get_cat_item_for_name(const char* name);
diff --git a/locale.c b/locale.c
index ede426d..e5b1072 100644
--- a/locale.c
+++ b/locale.c
@@ -13,0 +14,2 @@
+#include <errno.h>
+#include <limits.h>
@@ -23,0 +26 @@
+#include <sys/stat.h>
@@ -27,0 +31,2 @@
+#define MUSL_LOCALE_NAME_MAX 23
+#define MUSL_LOCALE_PATH_SIZE 256
@@ -105 +110 @@ static int argp_parse(int argc, char *argv[])
-static void list_locale()
+struct locale_list
@@ -106,0 +112,109 @@ static void list_locale()
+    char **names;
+    size_t count;
+    size_t capacity;
+};
+
+static bool add_locale_name(struct locale_list *list, const char *name)
+{
+    if (list->count == list->capacity) {
+        size_t capacity = list->capacity == 0 ? 8 : list->capacity * 2;
+        char **names = realloc(list->names, capacity * sizeof(*names));
+        if (names == NULL)
+            return false;
+        list->names = names;
+        list->capacity = capacity;
+    }
+
+    list->names[list->count] = strdup(name);
+    if (list->names[list->count] == NULL)
+        return false;
+    list->count++;
+    return true;
+}
+
+static bool locale_path_admissible(
+    const char *name,
+    size_t path_len,
+    size_t *name_len
+)
+{
+    size_t length = 0;
+    while (length < MUSL_LOCALE_NAME_MAX && name[length] != '\0' && name[length] != '/')
+        length++;
+
+    if (length == 0 || name[0] == '.' || name[length] != '\0')
+        return false;
+    if (path_len >= MUSL_LOCALE_PATH_SIZE - length - 2)
+        return false;
+
+    *name_len = length;
+    return true;
+}
+
+static bool add_locale_directory(
+    struct locale_list *list,
+    const char *path,
+    size_t path_len
+)
+{
+    char *directory = path_len == 0 ? strdup("/") : strndup(path, path_len);
+    if (directory == NULL)
+        return false;
+
+    DIR *dir = opendir(directory);
+    if (dir == NULL) {
+        free(directory);
+        return true;
+    }
+
+    bool success = true;
+    for (;;) {
+        errno = 0;
+        struct dirent *entry = readdir(dir);
+        if (entry == NULL) {
+            success = errno == 0;
+            break;
+        }
+        size_t entry_len;
+        if (!locale_path_admissible(entry->d_name, path_len, &entry_len))
+            continue;
+
+        size_t directory_len = strlen(directory);
+        bool needs_slash = directory_len == 0 || directory[directory_len - 1] != '/';
+        char *filename = malloc(directory_len + needs_slash + entry_len + 1);
+        if (filename == NULL) {
+            success = false;
+            break;
+        }
+        memcpy(filename, directory, directory_len);
+        if (needs_slash)
+            filename[directory_len++] = '/';
+        memcpy(filename + directory_len, entry->d_name, entry_len + 1);
+
+        struct stat status;
+        if (stat(filename, &status) == 0 && S_ISREG(status.st_mode) && status.st_size > 0 &&
+            !add_locale_name(list, entry->d_name))
+            success = false;
+        free(filename);
+        if (!success)
+            break;
+    }
+
+    closedir(dir);
+    free(directory);
+    return success;
+}
+
+static int compare_locale_names(const void *left, const void *right)
+{
+    const char *const *left_name = left;
+    const char *const *right_name = right;
+    return strcmp(*left_name, *right_name);
+}
+
+static bool list_locale()
+{
+    struct locale_list list = {0};
+    bool success = add_locale_name(&list, "C") &&
+        add_locale_name(&list, "C.UTF-8") &&
+        add_locale_name(&list, "POSIX");
@@ -108,9 +222,13 @@ static void list_locale()
-    printf("C\n");
-    printf("C.UTF-8\n");
-    if(locpath != NULL)
-    {
-        DIR *dir = opendir(locpath);
-        struct dirent *pDir;
-        while ((pDir = readdir(dir)) != NULL){
-            if (strcmp(pDir->d_name,".") && strcmp(pDir->d_name,".."))
-                printf("%s\n",pDir->d_name);
+
+    if (success && locpath != NULL) {
+        const char *component = locpath;
+        while (*component != '\0') {
+            const char *separator = strchr(component, ':');
+            const char *end = separator == NULL ? component + strlen(component) : separator;
+            if (!add_locale_directory(&list, component, (size_t)(end - component))) {
+                success = false;
+                break;
+            }
+            if (separator == NULL)
+                break;
+            component = separator + 1;
@@ -118,0 +237,12 @@ static void list_locale()
+
+    if (success) {
+        qsort(list.names, list.count, sizeof(*list.names), compare_locale_names);
+        for (size_t i = 0; i < list.count; i++)
+            if (i == 0 || strcmp(list.names[i - 1], list.names[i]) != 0)
+                puts(list.names[i]);
+    }
+
+    for (size_t i = 0; i < list.count; i++)
+        free(list.names[i]);
+    free(list.names);
+    return success;
@@ -127,0 +258,33 @@ static void list_charmaps()
+static void print_string(const char *value, bool compound, bool escape)
+{
+    if (value == NULL)
+        return;
+
+    for (; *value != '\0'; value++) {
+        unsigned char byte = (unsigned char)*value;
+        if (escape && ((compound && byte == ';') || byte == '\\' || byte == '"' ||
+            byte < ' ' || byte == 0x7f))
+            putchar('\\');
+        putchar(byte);
+    }
+}
+
+static void print_grouping(const char *grouping)
+{
+    if (grouping == NULL || grouping[0] == '\0') {
+        fputs("-1", stdout);
+        return;
+    }
+
+    for (size_t i = 0; grouping[i] != '\0'; i++) {
+        if (i != 0)
+            putchar(';');
+        unsigned char value = (unsigned char)grouping[i];
+        if (value == (unsigned char)CHAR_MAX) {
+            fputs("-1", stdout);
+            break;
+        }
+        printf("%u", value);
+    }
+}
+
@@ -129,0 +293,2 @@ static void print_item (struct cat_item item)
+    const char *locale_data = (const char *)localeconv();
+
@@ -135 +300 @@ static void print_item (struct cat_item item)
-        fputs (nl_langinfo (item.id) ? : "", stdout);
+        print_string(nl_langinfo(item.id), false, show_keyword_name);
@@ -149,2 +314 @@ static void print_item (struct cat_item item)
-            if (val != NULL)
-                fputs (val, stdout);
+            print_string(val, true, show_keyword_name);
@@ -158,0 +323,34 @@ static void print_item (struct cat_item item)
+    case CAT_TYPE_LCONV_STRING:
+    {
+        const char *value = *(char *const *)(locale_data + item.offset);
+        if (show_keyword_name)
+            printf("%s=\"", item.name);
+        print_string(value, false, show_keyword_name);
+        if (show_keyword_name)
+            putchar('"');
+        putchar('\n');
+    }
+        break;
+    case CAT_TYPE_LCONV_GROUPING:
+    {
+        const char *value = *(char *const *)(locale_data + item.offset);
+        if (show_keyword_name)
+            printf("%s=", item.name);
+        print_grouping(value);
+        putchar('\n');
+    }
+        break;
+    case CAT_TYPE_LCONV_BYTE:
+    {
+        unsigned char value = *(const unsigned char *)(locale_data + item.offset);
+        if (show_keyword_name)
+            printf("%s=", item.name);
+        if (value == (unsigned char)CHAR_MAX)
+            fputs("-1", stdout);
+        else
+            printf("%u", value);
+        putchar('\n');
+    }
+        break;
+    case CAT_TYPE_END:
+        break;
@@ -163 +361 @@ static void print_item (struct cat_item item)
-static void show_info(const char *name)
+static bool show_info(const char *name)
@@ -175 +373 @@ static void show_info(const char *name)
-            return;
+            return true;
@@ -177,0 +376,5 @@ static void show_info(const char *name)
+    struct cat_item item = get_cat_item_for_name(name);
+    if (item.type == CAT_TYPE_END) {
+        fprintf(stderr, gettext("locale: unknown name: %s\n"), name);
+        return false;
+    }
@@ -180 +383,42 @@ static void show_info(const char *name)
-    print_item(get_cat_item_for_name(name));
+    print_item(item);
+    return true;
+}
+
+static void print_assignment(const char *name, const char *value, bool quoted)
+{
+    printf("%s=", name);
+    if (value == NULL)
+        value = "";
+
+    if (quoted) {
+        putchar('"');
+        while (*value != '\0') {
+            if (strchr("$`\"\\", *value) != NULL)
+                putchar('\\');
+            putchar(*value++);
+        }
+        putchar('"');
+    } else {
+        const char *cursor = value;
+        while (*cursor != '\0' &&
+               ((*cursor >= 'a' && *cursor <= 'z') ||
+                (*cursor >= 'A' && *cursor <= 'Z') ||
+                (*cursor >= '0' && *cursor <= '9') ||
+                strchr("_./:+,@%-", *cursor) != NULL))
+            cursor++;
+
+        if (*cursor == '\0') {
+            fputs(value, stdout);
+        } else {
+            putchar('\'');
+            while (*value != '\0') {
+                if (*value == '\'')
+                    fputs("'\\''", stdout);
+                else
+                    putchar(*value);
+                value++;
+            }
+            putchar('\'');
+        }
+    }
+    putchar('\n');
@@ -185,2 +429,2 @@ static void show_locale_vars()
-    const char *lcall = getenv("LC_ALL") ? : "\0";
-    const char *lang = getenv("LANG") ? : "";
+    const char *lcall = getenv("LC_ALL");
+    const char *lang = getenv("LANG");
@@ -189 +433 @@ static void show_locale_vars()
-    printf("LANG=%s\n", lang);
+    print_assignment("LANG", lang, false);
@@ -192,3 +436,10 @@ static void show_locale_vars()
-        printf("%s=%s\n",get_name_for_cat(cat_no),lcall[0] != '\0' ? lcall
-                                                                : lang[0] != '\0' ? lang
-                                                                : "POSIX");
+        const char *name = get_name_for_cat(cat_no);
+        const char *explicit_value = getenv(name);
+        bool overridden = lcall != NULL && lcall[0] != '\0';
+        print_assignment(
+            name,
+            !overridden && explicit_value != NULL
+                ? explicit_value
+                : setlocale((int)cat_no, NULL),
+            overridden || explicit_value == NULL
+        );
@@ -197 +448 @@ static void show_locale_vars()
-    printf("LC_ALL=%s\n", lcall);
+    print_assignment("LC_ALL", lcall, false);
@@ -210,0 +462,7 @@ int main(int argc, char *argv[])
+    bool listing = do_all || do_charmaps;
+    if ((do_all && do_charmaps) ||
+        (listing && (show_category_name || show_keyword_name || remaining < argc)) ||
+        (!listing && (show_category_name || show_keyword_name) && remaining == argc)) {
+        usage(*argv);
+        return 1;
+    }
@@ -216,2 +474,5 @@ int main(int argc, char *argv[])
-            list_locale();
-        exit(EXIT_SUCCESS);
+            if (!list_locale()) {
+                fprintf(stderr, gettext("locale: cannot list locales\n"));
+                return EXIT_FAILURE;
+            }
+        return EXIT_SUCCESS;
@@ -240,0 +502 @@ int main(int argc, char *argv[])
+    bool success = true;
@@ -242 +504,2 @@ int main(int argc, char *argv[])
-        show_info(argv[remaining++]);
+        if (!show_info(argv[remaining++]))
+            success = false;
@@ -244 +507 @@ int main(int argc, char *argv[])
-    exit(EXIT_SUCCESS);
+    return success ? EXIT_SUCCESS : EXIT_FAILURE;
