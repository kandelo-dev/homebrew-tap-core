#!/usr/bin/env python3
"""Validate MariaDB's closed, registry-free Formula ownership boundary."""

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
    "kandelo-dev/tap-core/pcre2",
    "kandelo-dev/tap-core/zlib",
]
FORBIDDEN_AUTHORITY = (
    "KANDELO_REGISTRY_BRIDGE",
    "kandelo_build_package",
    "packages/registry",
    "build-deps",
    "install-local-binary",
    "WASM_POSIX_BINARY_CACHE_ROOT",
    "WASM_POSIX_XTASK_BIN",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_formula_contract() -> None:
    source = FORMULA.read_text()
    for forbidden in FORBIDDEN_AUTHORITY:
        assert forbidden not in source, (
            f"{FORMULA} retains forbidden authority: {forbidden}"
        )

    for required in (
        "KANDELO_TAP_RECIPE = true",
        'depends_on "llvm" => :build',
        'depends_on "kandelo-dev/tap-core/libcxx"',
        'depends_on "kandelo-dev/tap-core/pcre2"',
        'depends_on "kandelo-dev/tap-core/zlib"',
        "kandelo_build_tap_recipe",
        "kandelo_fork_instrument(mariadbd)",
        "kandelo_fork_instrument(mysqltest)",
        "kandelo_validate_wasm_artifact",
        "kandelo_run_wasm",
        "kandelo_run_browser_wasm",
    ):
        assert required in source, f"{FORMULA} is missing {required!r}"

    # WHY: MariaDB's native generators must come from declared Homebrew build
    # dependencies, not a caller-selected LLVM or CMake installation.
    for forbidden in (
        "HOMEBREW_KANDELO_LLVM_BIN",
        'kandelo_host_tool("cmake")',
    ):
        assert forbidden not in source, (
            f"{FORMULA} accepts caller-selected native tooling: {forbidden}"
        )
    for tool in ("clang", "clang++", "llvm-ar", "llvm-ranlib"):
        assert tool in source, f"{FORMULA} does not bind native tool {tool}"


def assert_recipe_contract() -> None:
    manifest_path = RECIPE_ROOT / "recipe.json"
    manifest = json.loads(manifest_path.read_text())
    assert list(manifest) == ["schema", "dependencies", "entrypoint", "files"]
    assert manifest["schema"] == 1
    assert manifest["dependencies"] == EXPECTED_DEPENDENCIES
    assert manifest["entrypoint"] == "build.sh"

    records = manifest["files"]
    assert [record["path"] for record in records] == sorted(
        record["path"] for record in records
    )
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
    literal = re.search(
        r'manifest_sha256: "([0-9a-f]{64})"',
        formula_source,
    )
    assert literal is not None
    assert literal.group(1) == sha256(manifest_path)

    entrypoint = (RECIPE_ROOT / manifest["entrypoint"]).read_text()
    for forbidden in FORBIDDEN_AUTHORITY:
        assert forbidden not in entrypoint, (
            f"MariaDB recipe retains forbidden authority: {forbidden}"
        )
    for required in (
        "WASM_POSIX_DEP_SOURCE_DIR",
        "WASM_POSIX_DEP_WORK_DIR",
        "WASM_POSIX_DEP_OUT_DIR",
        "WASM_POSIX_DEP_TARGET_ARCH",
        "WASM_POSIX_DEP_LIBCXX_DIR",
        "WASM_POSIX_DEP_PCRE2_DIR",
        "WASM_POSIX_DEP_ZLIB_DIR",
        "MARIADB_HOST_BUILD_DIR",
    ):
        assert required in entrypoint, f"MariaDB recipe is missing {required}"

    # The declared PCRE2 and Zlib Formulae must remain real build inputs. A
    # bundled fallback would make the dependency graph decorative.
    for required in (
        "-DWITH_PCRE=system",
        "-DWITH_ZLIB=system",
        "MariaDB did not select the declared Zlib Formula dependency",
    ):
        assert required in entrypoint
    assert "pcre2-source" not in formula_source

    for arch in ("wasm32", "wasm64"):
        toolchain = RECIPE_ROOT / f"{arch}-posix-toolchain.cmake"
        source = toolchain.read_text()
        assert "CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY" in source
        assert "WASM_POSIX_SYSROOT" in source
        assert f"--target={arch}-unknown-unknown" in source


def main() -> None:
    os.chdir(ROOT)
    assert_formula_contract()
    assert_recipe_contract()
    print("MariaDB Formula migration contract: ok")


if __name__ == "__main__":
    main()
