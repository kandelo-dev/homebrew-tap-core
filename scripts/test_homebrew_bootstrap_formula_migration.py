#!/usr/bin/env python3
"""Validate the sealed, registry-free Homebrew bootstrap Formula contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORMULA = ROOT / "Formula" / "homebrew-bootstrap.rb"
RECIPE_ROOT = ROOT / "Kandelo" / "recipes" / "homebrew-bootstrap"
LOCK = RECIPE_ROOT / "source-lock.json"
EXPECTED_RECIPE_FILES = [
    "PATCH-LICENSE.md",
    "build.sh",
    "create-deterministic-zip.sh",
    "patches/0001-add-kandelo-wasm-bottle-tags.patch",
    "source-lock.json",
    "verify-source-lock.rb",
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
    "scripts/dev-shell.sh",
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
            'KANDELO_BOTTLE_TEST_CONTRACT = "support-data".freeze',
            'url "https://github.com/Homebrew/brew/archive/'
            'd6c1be418446eec7de09fc72441ba4462282a142.tar.gz"',
            'version "6.0.4-3-gd6c1be4"',
            'sha256 "d3a38612b71eba6ab297a67c06b367829b96250fef48bc0a5088e832a659fc5c"',
            'license all_of: ["BSD-2-Clause", "GPL-2.0-or-later"]',
            'depends_on "git" => :build',
            'depends_on "ruby" => :build',
            'depends_on "unzip" => [:build, :test]',
            'depends_on "zip" => :build',
            'kandelo_require_arch!("wasm32")',
            "kandelo_build_tap_recipe",
            '"HOMEBREW_BOOTSTRAP_RUBY" => formula_opt_bin("ruby")/"ruby"',
            "libexec.install out_dir/BOOTSTRAP_ARCHIVE",
            "libexec.install out_dir/ENVIRONMENT_POLICY",
        ),
        FORMULA,
    )
    assert 'kandelo_require_arch!("wasm32", "wasm64")' not in source
    assert "RbConfig.ruby" not in source

    # This Formula packages support data rather than a Wasm executable. Its
    # local test therefore owns the complete installed byte contract; the
    # Kandelo guest lifecycle owns execution of these bytes with real Ruby.
    assert (
        source.count(
            'KANDELO_BOTTLE_TEST_CONTRACT = "support-data".freeze'
        )
        == 1
    )
    require_all(
        source,
        (
            "96aafa1546d0f737b2242589dbd0e47decf2af8352a3069d0552638eb2ebe03b",
            "assert_equal 5_046_915, archive.size",
            "HOMEBREW_NO_AUTO_UPDATE=1",
            "HOMEBREW_NO_INSTALL_FROM_API=1",
            "HOMEBREW_KANDELO_BOTTLE_TAG=wasm32_kandelo",
            'system formula_opt_bin("unzip")/"unzip", "-q", archive',
            'assert_predicate extracted/"bin/brew", :executable?',
            'assert_path_exists extracted/"LICENSE.txt"',
            "Retain its `homebrew-` prefix",
            "Process.spawn",
        ),
        FORMULA,
    )


def assert_recipe_contract() -> None:
    manifest_path = RECIPE_ROOT / "recipe.json"
    manifest = json.loads(manifest_path.read_text())
    assert list(manifest) == ["schema", "dependencies", "entrypoint", "files"]
    assert manifest["schema"] == 1
    assert manifest["dependencies"] == []
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
    assert re.search(
        r"(?m)^\s*(?:command\s+)?(?:curl|wget|gh|brew)\b", entrypoint
    ) is None, f"{entrypoint_path} can acquire external authority"
    require_all(
        entrypoint,
        (
            'SOURCE_DIR="${WASM_POSIX_DEP_SOURCE_DIR:-}"',
            'RECIPE_DIR="${WASM_POSIX_DEP_RECIPE_DIR:-}"',
            'WORK_DIR="${WASM_POSIX_DEP_WORK_DIR:-}"',
            'OUT_DIR="${WASM_POSIX_DEP_OUT_DIR:-}"',
            'RUBY="${HOMEBREW_BOOTSTRAP_RUBY:-}"',
            "GIT_CONFIG_GLOBAL=/dev/null",
            "GIT_CONFIG_NOSYSTEM=1",
            "GIT_TERMINAL_PROMPT=0",
            "isolated_git init --bare -q",
            "isolated_git --work-tree=\"$SOURCE_DIR\" add -f -A -- .",
            "EXPECTED_UPSTREAM_TREE=",
            "isolated_git apply --cached --check",
            "EXPECTED_PATHS=(",
            '"Library/Homebrew/github_packages.rb"',
            '"Library/Homebrew/utils/bottles.rb"',
            '"bin/brew"',
            "EXPECTED_PATCHED_TREE=",
            "isolated_git archive --format=tar",
            "isolated_git --work-tree=\"$STAGE_DIR\" checkout-index",
            '"$ZIPPER" "$STAGE_DIR" "$ARCHIVE"',
            '"$RUBY" "$VERIFY"',
            "--write-provenance",
            'cp "$ARCHIVE" "$OUT_DIR/$ARCHIVE_NAME"',
            'cp "$ENVIRONMENT" "$OUT_DIR/$ENVIRONMENT_NAME"',
        ),
        entrypoint_path,
    )

    zipper = (RECIPE_ROOT / "create-deterministic-zip.sh").read_text()
    require_all(
        zipper,
        (
            "output must be outside the staging tree",
            "ZIP entry names must not contain newlines",
            "unsupported special file",
            "chmod 0755",
            "chmod 0644",
            "touch -t 200001010000.00",
            "zip -X -y -6 -q",
        ),
        RECIPE_ROOT / "create-deterministic-zip.sh",
    )


def assert_source_lock_contract() -> None:
    lock = json.loads(LOCK.read_text())
    assert list(lock) == [
        "schema",
        "kind",
        "package",
        "source",
        "patch",
        "license",
        "prepared",
        "outputs",
    ]
    assert lock["schema"] == 1
    assert lock["kind"] == "kandelo-homebrew-bootstrap-tap-recipe-lock"
    assert lock["package"] == {
        "name": "homebrew-bootstrap",
        "version": "6.0.4-3-gd6c1be4",
        "arch": "wasm32",
    }
    assert lock["source"]["revision"] == (
        "d6c1be418446eec7de09fc72441ba4462282a142"
    )
    assert lock["source"]["tree_git_oid"] == (
        "3f8819e0d323511fdc15c1f6132849ed3b64aebe"
    )
    assert lock["patch"]["sha256"] == sha256(
        RECIPE_ROOT / "patches" / "0001-add-kandelo-wasm-bottle-tags.patch"
    )
    assert lock["license"]["upstream"]["path"] == "LICENSE.txt"
    assert lock["license"]["kandelo_patch"]["evidence_sha256"] == sha256(
        RECIPE_ROOT / "PATCH-LICENSE.md"
    )
    assert lock["prepared"]["patched_tree_git_oid"] == (
        "aa63e300318064a4d0801723574ff7c9430f12ce"
    )
    assert lock["prepared"]["archive_format"] == "kandelo-deterministic-zip-v1"
    assert lock["outputs"]["archive"] == {
        "path": "homebrew-bootstrap.zip",
        "sha256": "96aafa1546d0f737b2242589dbd0e47decf2af8352a3069d0552638eb2ebe03b",
        "bytes": 5_046_915,
    }
    assert lock["outputs"]["environment"] == {
        "path": "homebrew-brew.env",
        "sha256": "2eb3f05703b6a6f23feabda24f622bacd068115c7f74a0eac51bb4085e9eec5a",
        "bytes": 210,
    }


def main() -> None:
    assert_formula_contract()
    assert_recipe_contract()
    assert_source_lock_contract()
    print("Homebrew bootstrap Formula migration contract: ok")


if __name__ == "__main__":
    main()
