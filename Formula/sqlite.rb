require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Sqlite < Formula
  include KandeloFormulaSupport

  desc "SQL database engine and command-line shell for Kandelo"
  homepage "https://www.sqlite.org/"
  url "https://www.sqlite.org/2025/sqlite-amalgamation-3490100.zip"
  version "3.49.1"
  sha256 "6cebd1d8403fc58c30e93939b246f3e6e58d0765a5cd50546f16c00fd805d2c3"
  license "blessing"
  revision 1

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => :build

  skip_clean "bin/sqlite3"
  skip_clean "lib/libsqlite3.a"

  def install
    # Browser and Node Wasm engines exhaust their host stacks before SQLite's
    # upstream recursive SQL limits are reached.
    sqlite_cflags = %w[
      -O2
      -DSQLITE_OMIT_LOAD_EXTENSION
      -DSQLITE_THREADSAFE=1
      -DSQLITE_DEFAULT_SYNCHRONOUS=0
      -DSQLITE_ENABLE_SETLK_TIMEOUT=2
      -DSQLITE_MAX_COMPOUND_SELECT=50
      -DSQLITE_MAX_EXPR_DEPTH=100
      -DSQLITE_JSON_MAX_DEPTH=100
      -DSQLITE_MAX_TRIGGER_DEPTH=50
      -DHAVE_PREAD=1
      -DHAVE_PWRITE=1
      -DSQLITE_ENABLE_COLUMN_METADATA
      -DSQLITE_ENABLE_FTS5
      -DSQLITE_ENABLE_JSON1
      -DSQLITE_ENABLE_MATH_FUNCTIONS
    ]

    kandelo_wasm_build do
      system kandelo_cc, *sqlite_cflags, "-c", "sqlite3.c", "-o", "sqlite3.o"
      system kandelo_ar, "rcs", "libsqlite3.a", "sqlite3.o"
      system kandelo_cc, *sqlite_cflags, "shell.c", "libsqlite3.a", "-lm", "-o", "sqlite3"

      kandelo_validate_wasm_artifact(buildpath/"sqlite3", fork: :forbidden)
      system "bash", "-c", <<~SH
        set -euo pipefail
        unexpected_env_imports=$(wasm-objdump -x #{(buildpath/"sqlite3").to_s.shellescape} |
          awk '/<- env[.]/ { sub(/^.*<- env[.]/, ""); print $1 }' |
          grep -Ev '^(__channel_base|memory)$' || true)
        if [ -n "$unexpected_env_imports" ]; then
          echo "ERROR: sqlite3 contains unresolved non-ABI env imports" >&2
          echo "$unexpected_env_imports" >&2
          exit 1
        fi
      SH
    end

    kandelo_install_bin(buildpath, "sqlite3", "sqlite3")
    include.install "sqlite3.h", "sqlite3ext.h"
    lib.install "libsqlite3.a"
    (lib/"pkgconfig").mkpath
    (lib/"pkgconfig/sqlite3.pc").write <<~EOS
      prefix=#{prefix}
      libdir=${prefix}/lib
      includedir=${prefix}/include

      Name: SQLite
      Description: SQL database engine
      Version: #{version}
      Libs: -L${libdir} -lsqlite3
      Cflags: -I${includedir}
    EOS
  end

  test do
    source = testpath/"sqlite-smoke.c"
    wasm = testpath/"sqlite-smoke.wasm"
    source.write <<~C
      #include <stdio.h>
      #include <string.h>
      #include <sqlite3.h>

      int main(void) {
        sqlite3 *db = NULL;
        sqlite3_stmt *stmt = NULL;
        const char *table = NULL;
        const char *value = NULL;

        if (sqlite3_open(":memory:", &db) != SQLITE_OK) return 1;
        if (sqlite3_exec(db, "CREATE TABLE t(v TEXT); INSERT INTO t VALUES('kandelo');",
                         NULL, NULL, NULL) != SQLITE_OK) return 2;
        if (sqlite3_prepare_v2(db, "SELECT v FROM t", -1, &stmt, NULL) != SQLITE_OK) return 3;
        if (sqlite3_step(stmt) != SQLITE_ROW) return 4;
        table = sqlite3_column_table_name(stmt, 0);
        if (table == NULL || strcmp(table, "t") != 0) return 6;
        value = (const char *)sqlite3_column_text(stmt, 0);
        if (value == NULL || strcmp(value, "kandelo") != 0) return 5;

        sqlite3_finalize(stmt);
        sqlite3_close(db);
        puts("sqlite-ok");
        return 0;
      }
    C

    kandelo_wasm_build do
      system kandelo_cc, source, "-I#{include}", "-L#{lib}", "-lsqlite3", "-lm", "-o", wasm
    end
    assert_equal "sqlite-ok\n", kandelo_run_wasm(wasm, [])

    runtime_env = { "HOME" => testpath, "KERNEL_CWD" => testpath }
    database = testpath/"cli.db"
    csv = testpath/"items.csv"
    schema = testpath/"schema.sql"
    csv.write "alpha,7\nbeta,11\n"
    schema.write "CREATE TABLE notes(value TEXT); INSERT INTO notes VALUES('from-read');\n"
    assert_match(/^3\.49\.1 /, kandelo_run_wasm(bin/"sqlite3", ["--version"], env: runtime_env))
    output = kandelo_run_wasm(
      bin/"sqlite3",
      [database.basename],
      env:   runtime_env,
      stdin: <<~SQL,
        .read schema.sql
        CREATE TABLE imported(name TEXT, value INTEGER);
        .mode csv
        .import items.csv imported
        .mode list
        SELECT name || '=' || value FROM imported ORDER BY name;
      SQL
    )
    assert_equal "alpha=7\nbeta=11\n", output

    assert_equal "18\n", kandelo_run_wasm(
      bin/"sqlite3",
      [database.basename, "SELECT sum(value) FROM imported;"],
      env: runtime_env,
    )
    assert_equal "from-read\n", kandelo_run_wasm(
      bin/"sqlite3",
      [database.basename, "SELECT value FROM notes;"],
      env: runtime_env,
    )
    json = kandelo_run_wasm(
      bin/"sqlite3",
      [database.basename],
      env:   runtime_env,
      stdin: ".mode json\nSELECT name, value FROM imported ORDER BY name;\n",
    )
    assert_includes json, '{"name":"alpha","value":7}'
    assert_includes json, '{"name":"beta","value":11}'

    dump = kandelo_run_wasm(bin/"sqlite3", [database.basename, ".dump imported"], env: runtime_env)
    assert_includes dump, "CREATE TABLE imported(name TEXT, value INTEGER);"
    assert_includes dump, "INSERT INTO imported VALUES('alpha',7);"
    assert_includes dump, "INSERT INTO imported VALUES('beta',11);"

    child = kandelo_run_wasm(
      bin/"sqlite3",
      [":memory:", ".shell printf sqlite-child"],
      env:                       { "HOME" => "/" },
      expected_fork_descendants: 1,
      merge_stderr:              true,
    )
    assert_equal "sqlite-child", child
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    rebuild 2
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "d8a7a49c269a651b66be6a8b2d371f4f0a740e8a5db587f147a7ee437360bf83"
    sha256 cellar: :any_skip_relocation, wasm64_kandelo: "e5b06a7fee3e85b98095ce06144aba3ed5b7cbced77af1420115507d6a878f44"
  end

end
