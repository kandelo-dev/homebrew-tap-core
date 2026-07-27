#!/usr/bin/env python3
"""Validate MariaDB's complete, sealed, registry-free Formula contract."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORMULA = ROOT / "Formula" / "mariadb.rb"
RECIPE_ROOT = ROOT / "Kandelo" / "recipes" / "mariadb"
EXPECTED_DEPENDENCIES = [
    "kandelo-dev/tap-core/libcxx",
    "kandelo-dev/tap-core/ncurses",
    "kandelo-dev/tap-core/openssl",
    "kandelo-dev/tap-core/pcre2",
    "kandelo-dev/tap-core/zlib",
]
EXPECTED_RECIPE_FILES = [
    "build.sh",
    "wasm32-posix-toolchain.cmake",
]
NATIVE_ENV = [
    "MARIADB_NATIVE_BISON_DIR",
    "MARIADB_NATIVE_CMAKE_DIR",
    "MARIADB_NATIVE_LLVM_DIR",
    "MARIADB_NATIVE_MAKE_DIR",
]
FORBIDDEN_AUTHORITY = (
    "KANDELO_REGISTRY_BRIDGE",
    "kandelo_build_package",
    "packages/registry",
    "build-deps",
    "install-local-binary",
    "WASM_POSIX_BINARY_CACHE_ROOT",
    "WASM_POSIX_XTASK_BIN",
    "kandelo_host_tool",
    "/nix/store",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_all(source: str, needles: tuple[str, ...], owner: Path) -> None:
    for needle in needles:
        assert needle in source, f"{owner} is missing {needle!r}"


def reject_all(source: str, needles: tuple[str, ...], owner: Path) -> None:
    for needle in needles:
        assert needle not in source, f"{owner} retains forbidden {needle!r}"


def data_patch(formula_source: str) -> str:
    marker = "\n__END__\n"
    assert marker in formula_source, f"{FORMULA} has no embedded patch"
    return formula_source.split(marker, 1)[1]


def assert_formula_contract() -> None:
    source = FORMULA.read_text()
    reject_all(source, FORBIDDEN_AUTHORITY, FORMULA)
    require_all(
        source,
        (
            "KANDELO_TAP_RECIPE = true",
            'kandelo_require_arch!("wasm32")',
            'depends_on "bison" => :build',
            'depends_on "cmake" => :build',
            'depends_on "llvm" => :build',
            'depends_on "make" => :build',
            'depends_on "kandelo-dev/tap-core/libcxx"',
            'depends_on "kandelo-dev/tap-core/ncurses"',
            'depends_on "kandelo-dev/tap-core/openssl"',
            'depends_on "kandelo-dev/tap-core/pcre2"',
            'depends_on "kandelo-dev/tap-core/zlib"',
            "kandelo_build_tap_recipe",
            '"MARIADB_NATIVE_BISON_DIR" => kandelo_formula("bison").prefix',
            '"MARIADB_NATIVE_CMAKE_DIR" => kandelo_formula("cmake").prefix',
            '"MARIADB_NATIVE_LLVM_DIR"  => kandelo_formula("llvm").prefix',
            '"MARIADB_NATIVE_MAKE_DIR"  => kandelo_formula("make").prefix',
            'mariadb_test = out_dir/"bin/mariadb-test.wasm"',
            "[mariadbd, mariadb_test].each do |artifact|",
            "forbidden_paths: target_dependencies",
            'kandelo_install_bin(out_dir/"bin", "mariadbd.wasm", "mariadbd")',
            'kandelo_install_bin(out_dir/"bin", "mariadb-test.wasm", "mariadb-test")',
            'bin.install_symlink "mariadb-test" => "mysqltest"',
        ),
        FORMULA,
    )
    assert 'kandelo_require_arch!("wasm32", "wasm64")' not in source
    assert "wasm64-posix-toolchain.cmake" not in source
    assert "MARIADB_HOST_BUILD_DIR" not in source
    assert "HOMEBREW_KANDELO_LLVM_BIN" not in source
    assert "\n  private\n" not in source

    # Installation validation covers both final programs, their dependency
    # identities, portable paths, fork shape, and host import surface.
    require_all(
        source,
        (
            "wasm_imports_kernel_fork",
            "run-wasm-fork-instrument.sh",
            "wasm-strip -k name -k target_features -k wasm-posix-abi",
            'contents.include?("OpenSSL #{openssl_version}")',
            '"wolfSSL", "wolfcrypt", "/extra/wolfssl/"',
            "forbidden_paths: target_dependencies",
            "WebAssembly.Module.imports(module)",
            "MariaDB has unexpected host imports",
        ),
        FORMULA,
    )

    # The Formula test is a real database lifecycle, not a --version smoke
    # test: bootstrap, TCP readiness, query/TLS output, shutdown, and exact
    # child reaping must run under both first-class hosts.
    require_all(
        source,
        (
            "mysql_system_tables.sql",
            "mysql_system_tables_data.sql",
            'share/"mysql/charsets/Index.xml"',
            "global_priv.MAI",
            "wait_for_port(3306)",
            "--protocol=tcp",
            "CREATE TABLE messages",
            "INSERT INTO messages",
            "SHOW VARIABLES LIKE 'version_ssl_library'",
            "mariadb-homebrew-ok",
            "mariadb-lifecycle-ok",
            "OpenSSL #{openssl_version}",
            "expected_fork_descendants: 3",
            "kandelo_run_browser_wasm",
            '"KANDELO_RUNTIME" => "node"',
            '"KANDELO_RUNTIME" => "browser"',
            "mariadb-#{runtime}-service-ok",
        ),
        FORMULA,
    )
    assert source.count("assert_lifecycle.call(") == 2


def assert_patch_contract(formula_source: str) -> None:
    patch = data_patch(formula_source)
    require_all(
        patch,
        (
            "diff --git a/cmake/mariadb_connector_c.cmake",
            'NOT CONC_WITH_SSL STREQUAL "OFF"',
            "diff --git a/mysys/get_password.c",
            "#include <ctype.h>",
            "diff --git a/mysys/my_gethwaddr.c",
            "diff --git a/mysys/my_largepage.c",
            "defined(MAP_HUGETLB)",
            "diff --git a/mysys/my_new.cc",
            "PSI_NOT_INSTRUMENTED",
            "diff --git a/client/mysqltest.cc",
            "#define mysql_server_init(a,b,c) mysql_client_plugin_init()",
            "#define mysql_server_end()       mysql_client_plugin_deinit()",
        ),
        FORMULA,
    )
    # All four nested/outer platform selections must avoid the Solaris ARP
    # implementation on Wasm. Fixing only the first two is unsafe.
    assert patch.count("+") > 0
    assert patch.count("defined(__wasm__)") == 4
    assert "maria_upgrade" not in patch
    assert "return 0;" not in patch
    assert not any(line == " " for line in patch.splitlines()), (
        "embedded patch contains whitespace-only context and may require fuzz"
    )
    assert not any(line.rstrip() != line for line in patch.splitlines()), (
        "embedded patch contains trailing whitespace"
    )


def assert_recipe_contract() -> None:
    manifest_path = RECIPE_ROOT / "recipe.json"
    manifest = json.loads(manifest_path.read_text())
    assert list(manifest) == ["schema", "dependencies", "entrypoint", "files"]
    assert manifest["schema"] == 1
    assert manifest["dependencies"] == EXPECTED_DEPENDENCIES
    assert manifest["entrypoint"] == "build.sh"

    records = manifest["files"]
    assert [record["path"] for record in records] == EXPECTED_RECIPE_FILES
    expected_files = {"recipe.json", *(record["path"] for record in records)}
    actual_files = {
        str(path.relative_to(RECIPE_ROOT))
        for path in RECIPE_ROOT.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files

    for record in records:
        assert list(record) == ["bytes", "mode", "path", "sha256"]
        member = RECIPE_ROOT / record["path"]
        assert not member.is_symlink()
        assert member.stat().st_nlink == 1
        assert member.stat().st_size == record["bytes"]
        assert f"{member.stat().st_mode & 0o777:04o}" == record["mode"]
        assert sha256(member) == record["sha256"]

    formula_source = FORMULA.read_text()
    literal = re.search(r'manifest_sha256: "([0-9a-f]{64})"', formula_source)
    assert literal is not None
    assert literal.group(1) == sha256(manifest_path)

    entrypoint_path = RECIPE_ROOT / manifest["entrypoint"]
    entrypoint = entrypoint_path.read_text()
    reject_all(entrypoint, FORBIDDEN_AUTHORITY, entrypoint_path)
    reject_all(
        entrypoint,
        (
            "MARIADB_HOST_BUILD_DIR",
            "HOMEBREW_KANDELO_ROOT",
            "LLVM_PREFIX",
            "Curses found (stub)",
            'CURSES_LIBRARY="${SYSROOT}/lib/libc.a"',
        ),
        entrypoint_path,
    )
    # TLS is disabled only for the native helper graph. The target graph below
    # must bind the declared OpenSSL keg.
    assert entrypoint.count('"-DWITH_SSL=OFF"') == 1
    require_all(
        entrypoint,
        (
            "WASM_POSIX_DEP_SOURCE_DIR",
            "WASM_POSIX_DEP_WORK_DIR",
            "WASM_POSIX_DEP_OUT_DIR",
            "WASM_POSIX_DEP_RECIPE_DIR",
            "WASM_POSIX_DEP_TARGET_ARCH",
            "WASM_POSIX_DEP_LIBCXX_DIR",
            "WASM_POSIX_DEP_NCURSES_DIR",
            "WASM_POSIX_DEP_OPENSSL_DIR",
            "WASM_POSIX_DEP_PCRE2_DIR",
            "WASM_POSIX_DEP_ZLIB_DIR",
            "WASM_POSIX_GLUE_DIR",
            "WASM_POSIX_LLVM_DIR",
            "WASM_POSIX_SYSROOT",
            'HOST_BUILD_DIR="$WORK_DIR/host-build"',
            "native_env()",
            "-u CC",
            "-u CXX",
            "-u PKG_CONFIG_PATH",
            "-u WASM_POSIX_LLVM_DIR",
            "-u WASM_POSIX_SYSROOT",
            "--target import_executables",
            "host_helpers_ready",
            "-DWITH_SSL=system",
            "-DCONC_WITH_SSL=OPENSSL",
            "-DOPENSSL_SSL_LIBRARY=\"$SYSROOT/lib/libssl.a\"",
            "-DOPENSSL_CRYPTO_LIBRARY=\"$SYSROOT/lib/libcrypto.a\"",
            "-DWITH_PCRE=system",
            "-DPCRE_LIBRARY_DIRS=\"$SYSROOT/lib\"",
            "-DWITH_ZLIB=system",
            "-DZLIB_LIBRARY=\"$SYSROOT/lib/libz.a\"",
            "-DCURSES_LIBRARY=\"$SYSROOT/lib/libtinfow.a\"",
            "require_cache_value OPENSSL_SSL_LIBRARY",
            "require_cache_value OPENSSL_CRYPTO_LIBRARY",
            "validate_link_command",
            "extra/wolfssl",
            "extra/pcre2",
            "mariadb-test.wasm",
            "mysql_system_tables.sql",
            "mysql_system_tables_data.sql",
            "sql/share/charsets",
            "english/errmsg.sys",
            "mysql-test/main",
        ),
        entrypoint_path,
    )
    for key in NATIVE_ENV:
        assert f': "${{{key}:?}}"' in entrypoint

    toolchain_path = RECIPE_ROOT / "wasm32-posix-toolchain.cmake"
    toolchain = toolchain_path.read_text()
    reject_all(toolchain, ("LLVM_PREFIX", "/nix/store", "Curses found (stub)"), toolchain_path)
    require_all(
        toolchain,
        (
            "CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY",
            "WASM_POSIX_LLVM_DIR",
            "WASM_POSIX_SYSROOT",
            "--target=wasm32-unknown-unknown",
            'set(WITH_SSL "system"',
            'set(CONC_WITH_SSL "OPENSSL"',
            "lib/libssl.a",
            "lib/libcrypto.a",
            "lib/libncursesw.a",
            "lib/libtinfow.a",
            "PCRE_LIBRARY_DIRS",
            "HAVE_PCRE2_MATCH_8",
        ),
        toolchain_path,
    )
    assert not (RECIPE_ROOT / "wasm64-posix-toolchain.cmake").exists()


def main() -> None:
    os.chdir(ROOT)
    formula_source = FORMULA.read_text()
    assert_formula_contract()
    assert_patch_contract(formula_source)
    assert_recipe_contract()
    print("MariaDB Formula migration contract: ok")


if __name__ == "__main__":
    main()
