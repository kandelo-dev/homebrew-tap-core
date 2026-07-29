#!/usr/bin/env python3
"""Validate CPython's complete, sealed, registry-free Formula contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORMULA = ROOT / "Formula" / "python.rb"
RECIPE_ROOT = ROOT / "Kandelo" / "recipes" / "python"
EXPECTED_RECIPE_FILES = [
    "build.sh",
    "config.site-wasm32-posix",
]
FORBIDDEN_AUTHORITY = (
    "KANDELO_REGISTRY_BRIDGE",
    "kandelo_build_package",
    "packages/registry",
    "build-deps",
    "install-local-binary",
    "WASM_POSIX_BINARY_CACHE_ROOT",
    "WASM_POSIX_XTASK_BIN",
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


def assert_formula_contract() -> None:
    source = FORMULA.read_text()
    reject_all(source, FORBIDDEN_AUTHORITY, FORMULA)
    require_all(
        source,
        (
            "KANDELO_TAP_RECIPE = true",
            'url "https://www.python.org/ftp/python/3.13.3/Python-3.13.3.tar.xz"',
            'version "3.13.3"',
            'sha256 "40f868bcbdeb8149a3149580bb9bfd407b3321cd48f0be631af955ac92c0e041"',
            'kandelo_require_arch!("wasm32")',
            "kandelo_build_tap_recipe",
            'depends_on "llvm" => :build',
            'depends_on "make" => :build',
            'depends_on "python@3.13" => :build',
            'depends_on "unzip" => :build',
            'depends_on "kandelo-dev/tap-core/zlib"',
            'kandelo_validate_wasm_artifact(out_dir/"python.wasm", fork: :required)',
            'kandelo_install_bin(out_dir, "python.wasm", "python3")',
            'bin.install_symlink "python3" => "python"',
            'bin.install_symlink "python3" => "python#{PYTHON_MAJOR_MINOR}"',
            'out_dir/"python-runtime.zip"',
            'lib.install stdlib',
            '(share/"licenses/cpython").install runtime_license',
        ),
        FORMULA,
    )
    assert 'kandelo_require_arch!("wasm32", "wasm64")' not in source

    # The test must exercise the installed interpreter and standard library,
    # including the declared zlib keg, through both first-class hosts.
    require_all(
        source,
        (
            'assert_operator runtime_files.length, :>, 500',
            "import json",
            "import site",
            "import sys",
            "import zlib",
            'assert sys.version_info[:3] == (3, 13, 3)',
            'assert json.loads(\'{"kandelo": [3, 1, 3]}\')',
            'zlib.decompress(zlib.compress(b"kandelo-python"))',
            "kandelo_run_wasm(",
            "python-node-ok:3.13.3",
            "kandelo_run_browser_wasm(",
            "python-browser-ok:3.13.3",
        ),
        FORMULA,
    )


def assert_recipe_contract() -> None:
    manifest_path = RECIPE_ROOT / "recipe.json"
    manifest = json.loads(manifest_path.read_text())
    assert list(manifest) == ["schema", "dependencies", "entrypoint", "files"]
    assert manifest["schema"] == 1
    assert manifest["dependencies"] == ["kandelo-dev/tap-core/zlib"]
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
    assert "WASM_POSIX_SDK_CONFIG_SITE=" not in entrypoint
    require_all(
        entrypoint,
        (
            'REPO_ROOT="${HOMEBREW_KANDELO_ROOT:?}"',
            'SOURCE_DIR="${WASM_POSIX_DEP_SOURCE_DIR:?}"',
            'SYSROOT_SOURCE="${WASM_POSIX_SYSROOT:?}"',
            'ZLIB_PREFIX="${WASM_POSIX_DEP_ZLIB_DIR:?}"',
            'GUEST_PREFIX="${WASM_POSIX_DEP_GUEST_PREFIX:?}"',
            '[ "$TARGET_ARCH" != "wasm32" ]',
            '[ "$PYTHON_VERSION" != "3.13.3" ]',
            'SOURCE_SHA256" != "40f868bcbdeb8149a3149580bb9bfd407b3321cd48f0be631af955ac92c0e041"',
            'CC=clang',
            'AR=llvm-ar',
            'RANLIB=llvm-ranlib',
            'python3.13 - "$SOURCE_DIR/Lib"',
            'CONFIG_SITE="$RECIPE_DIR/config.site-wasm32-posix"',
            "--with-build-python=\"$HOST_PYTHON\"",
            "CONFIGURE_LDFLAGS_NODIST=",
            '"$REPO_ROOT/scripts/run-wasm-fork-instrument.sh"',
            'cp "$FINAL_PYTHON" "$OUT_DIR/python.wasm"',
            'cp "$RUNTIME_ZIP" "$OUT_DIR/python-runtime.zip"',
        ),
        entrypoint_path,
    )

    # Preserve reproducible path and archive ownership. The source tree and
    # caller-owned staging paths must not leak into final program identities,
    # and the standard-library archive has fixed metadata and ordering.
    require_all(
        entrypoint,
        (
            "-ffile-prefix-map=$SOURCE_DIR=$STABLE_SOURCE",
            "-ffile-prefix-map=$WORK_DIR=/usr/src/kandelo-build/cpython",
            "-ffile-prefix-map=$REPO_ROOT=/usr/src/kandelo",
            'timestamp = (2023, 11, 14, 22, 13, 20)',
            'sorted((item for item in root.rglob("*") if item.is_file())',
            "compression=zipfile.ZIP_STORED",
            "info.create_system = 3",
            "info.external_attr = (stat.S_IFREG | 0o644) << 16",
        ),
        entrypoint_path,
    )

    config = (RECIPE_ROOT / "config.site-wasm32-posix").read_text()
    require_all(
        config,
        (
            "WASM_POSIX_SDK_CONFIG_SITE",
            "ac_cv_func__getpty",
            "ac_cv_func_fork1",
            "py_cv_module__ssl",
            "py_cv_module__sqlite3",
        ),
        RECIPE_ROOT / "config.site-wasm32-posix",
    )


def main() -> None:
    assert_formula_contract()
    assert_recipe_contract()
    print("Python Formula migration contract: ok")


if __name__ == "__main__":
    main()
