require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Mariadb < Formula
  include KandeloFormulaSupport

  KANDELO_TAP_RECIPE = true
  PROGRAM_OUTPUTS = {
    "mariadbd.wasm" => "bin/mariadbd",
    "mysqltest.wasm" => "bin/mysqltest",
  }.freeze
  SOURCE_ROLE_OUTPUTS = {
    "system-tables" => "share/mariadb/system-tables",
    "test-suite"    => "share/mariadb/test-suite",
  }.freeze

  desc "MariaDB server and test client for Kandelo"
  homepage "https://mariadb.org/"
  url "https://archive.mariadb.org/mariadb-10.5.28/source/mariadb-10.5.28.tar.gz"
  version "10.5.28"
  sha256 "0b5070208da0116640f20bd085f1136527f998cc23268715bcbf352e7b7f3cc1"
  license "GPL-2.0-only"

  depends_on KandeloFormulaSupport::BinaryenRequirement => :build
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]
  depends_on "bison" => :build
  depends_on "cmake" => :build
  depends_on "kandelo-dev/tap-core/libcxx"
  depends_on "kandelo-dev/tap-core/pcre2"

  skip_clean "bin/mariadbd", "bin/mysqltest", "share/mariadb"

  def install
    kandelo_require_arch!("wasm32")
    out_dir = kandelo_build_tap_recipe(
      manifest_sha256: "44ee71a69a11465545a5e6c2febcc2983c4bbca1f37a08e40ed3ee0ed7772c05",
      script_env:      {
        "MARIADB_VFS_SOURCE_ROLES" => "system-tables,test-suite",
      },
    )

    kandelo_validate_wasm_artifact(out_dir/"mariadbd.wasm", fork: :auto)
    kandelo_validate_wasm_artifact(out_dir/"mysqltest.wasm", fork: :auto)
    PROGRAM_OUTPUTS.each do |source_name, relative|
      destination = prefix/relative
      destination.dirname.install out_dir/source_name => destination.basename
      chmod 0755, destination
    end
    SOURCE_ROLE_OUTPUTS.each do |role, relative|
      destination = prefix/relative
      destination.mkpath
      destination.install (out_dir/".kandelo-vfs-source-roles"/role).children
    end
  end

  test do
    assert_path_exists bin/"mariadbd"
    assert_path_exists bin/"mysqltest"
    system_tables = share/"mariadb/system-tables"
    test_suite = share/"mariadb/test-suite"
    assert_path_exists system_tables/"mysql_system_tables.sql"
    assert_path_exists system_tables/"mysql_system_tables_data.sql"
    assert_path_exists test_suite/"main"

    data = testpath/"data"
    (data/"mysql").mkpath
    (data/"tmp").mkpath
    bootstrap_sql = <<~SQL
      use mysql;
      #{(system_tables/"mysql_system_tables.sql").read}
      #{(system_tables/"mysql_system_tables_data.sql").read}
    SQL
    kandelo_run_wasm(
      bin/"mariadbd",
      [
        "--no-defaults", "--bootstrap", "--user=root", "--datadir=/data",
        "--tmpdir=/data/tmp", "--skip-grant-tables", "--skip-networking",
        "--key-buffer-size=1048576", "--table-open-cache=10",
        "--sort-buffer-size=262144", "--log-warnings=0",
      ],
      stdin: bootstrap_sql,
      writable_host_directories: { "/data" => data },
      merge_stderr: true,
    )
    assert_operator(
      (data/"mysql").children.length,
      :>,
      0,
      "mariadb-ok: bootstrap did not create any system tables",
    )
  end
end
