#!/usr/bin/env python3
"""Validate the sealed, registry-free Homebrew bootstrap Formula contract."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORMULA = ROOT / "Formula" / "homebrew-bootstrap.rb"
RECIPE_ROOT = ROOT / "Kandelo" / "recipes" / "homebrew-bootstrap"
LOCK = RECIPE_ROOT / "source-lock.json"
VERIFY = RECIPE_ROOT / "verify-source-lock.rb"
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
KANDELO_GUEST_PREFIX = b"/opt/kandelo/homebrew"
RETIRED_GUEST_PREFIX = b"/home/linuxbrew/.linuxbrew"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_all(source: str, needles: tuple[str, ...], owner: Path) -> None:
    for needle in needles:
        assert needle in source, f"{owner} is missing {needle!r}"


def reject_all(source: str, needles: tuple[str, ...], owner: Path) -> None:
    for needle in needles:
        assert needle not in source, f"{owner} retains forbidden {needle!r}"


def reject_retired_guest_prefix(source: bytes, owner: Path) -> None:
    assert RETIRED_GUEST_PREFIX not in source, (
        f"{owner} retains retired guest prefix "
        f"{RETIRED_GUEST_PREFIX.decode()!r}"
    )


def assert_formula_contract() -> None:
    source = FORMULA.read_text()
    reject_all(source, FORBIDDEN_AUTHORITY, FORMULA)
    require_all(
        source,
        (
            "KANDELO_TAP_RECIPE = true",
            'KANDELO_BOTTLE_TEST_CONTRACT = "support-data".freeze',
            'url "https://github.com/Homebrew/brew/archive/'
            'cf5bc21c6b127e168ef7cfa982ba7db62874690e.tar.gz"',
            'version "6.0.12-153-gcf5bc21"',
            "revision 1",
            'sha256 "18d3c5384b1a90e0dca3c044b31d8a2b61b500bc5b880a14b1e52a590088de40"',
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
            "26ac98e328573244d3e7c0c149f30114ef5d9c8882200f5a22e56f97d2541482",
            "assert_equal 5_251_369, archive.size",
            "HOMEBREW_NO_AUTO_UPDATE=1",
            "HOMEBREW_NO_INSTALL_FROM_API=1",
            "HOMEBREW_KANDELO_BOTTLE_TAG=wasm32_kandelo",
            'system formula_opt_bin("unzip")/"unzip", "-q", archive',
            'assert_predicate extracted/"bin/brew", :executable?',
            'assert_path_exists extracted/"LICENSE.txt"',
            "KANDELO_GUEST_HOMEBREW_PREFIX",
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

    # WHY: this recipe is the source of the guest Homebrew tree. Checking every
    # sealed input prevents a future patch refresh from silently restoring the
    # host-only Linux default as Kandelo's product-visible package layout.
    for relative in sorted(expected_files):
        reject_retired_guest_prefix(
            (RECIPE_ROOT / relative).read_bytes(),
            RECIPE_ROOT / relative,
        )
    patch = RECIPE_ROOT / "patches/0001-add-kandelo-wasm-bottle-tags.patch"
    assert KANDELO_GUEST_PREFIX in patch.read_bytes()

    # Exercise the rejecting branch so this contract cannot become a no-op
    # while the checked-in recipe happens to remain clean.
    try:
        reject_retired_guest_prefix(
            b"prefix=/home/linuxbrew/.linuxbrew",
            Path("<adversarial-recipe>"),
        )
    except AssertionError as error:
        assert "retired guest prefix" in str(error)
    else:
        raise AssertionError("retired guest prefix guard accepted its fixture")

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
            'PACKAGE_VERSION="${WASM_POSIX_DEP_PKG_VERSION:-}"',
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
    assert 'PACKAGE_VERSION="${WASM_POSIX_DEP_VERSION:-}"' not in entrypoint

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
        "version": "6.0.12-153-gcf5bc21_1",
        "arch": "wasm32",
    }
    assert lock["source"]["revision"] == "cf5bc21c6b127e168ef7cfa982ba7db62874690e"
    assert lock["source"]["tree_git_oid"] == "df4aa7ac462564d14c713e0a6e07e33cbd0a4f8a"
    assert lock["patch"]["sha256"] == sha256(
        RECIPE_ROOT / "patches" / "0001-add-kandelo-wasm-bottle-tags.patch"
    )
    assert lock["license"]["upstream"]["path"] == "LICENSE.txt"
    assert lock["license"]["kandelo_patch"]["evidence_sha256"] == sha256(
        RECIPE_ROOT / "PATCH-LICENSE.md"
    )
    assert lock["prepared"]["patched_tree_git_oid"] == (
        "ae657d9bdebaa2218527f3e3a6b8b51e6907d365"
    )
    assert lock["prepared"]["archive_format"] == "kandelo-deterministic-zip-v1"
    assert lock["outputs"]["archive"] == {
        "path": "homebrew-bootstrap.zip",
        "sha256": "26ac98e328573244d3e7c0c149f30114ef5d9c8882200f5a22e56f97d2541482",
        "bytes": 5_251_369,
    }
    assert lock["outputs"]["environment"] == {
        "path": "homebrew-brew.env",
        "sha256": "2eb3f05703b6a6f23feabda24f622bacd068115c7f74a0eac51bb4085e9eec5a",
        "bytes": 210,
    }


def run_lock_verifier(
    lock: dict,
    package_version: str,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "source-lock.json"
        candidate.write_text(json.dumps(lock, indent=2) + "\n")
        return subprocess.run(
            [
                "ruby",
                str(VERIFY),
                "--lock",
                str(candidate),
                "--package-version",
                package_version,
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )


def assert_package_version_contract() -> None:
    original = json.loads(LOCK.read_text())
    base = "6.0.12-153-gcf5bc21"
    for package_version in (base, f"{base}_1", f"{base}_27"):
        candidate = json.loads(json.dumps(original))
        candidate["package"]["version"] = package_version
        result = run_lock_verifier(candidate, package_version)
        assert result.returncode == 0, result.stdout

    for package_version in (
        f"{base}_0",
        f"{base}_01",
        f"{base}_1_2",
        f"{base}_",
    ):
        candidate = json.loads(json.dumps(original))
        candidate["package"]["version"] = package_version
        result = run_lock_verifier(candidate, package_version)
        assert result.returncode != 0, package_version
        assert "package.version is invalid" in result.stdout

    result = run_lock_verifier(original, base)
    assert result.returncode != 0
    assert "package-version mismatch" in result.stdout


def main() -> None:
    assert_formula_contract()
    assert_recipe_contract()
    assert_source_lock_contract()
    assert_package_version_contract()
    print("Homebrew bootstrap Formula migration contract: ok")


if __name__ == "__main__":
    main()
