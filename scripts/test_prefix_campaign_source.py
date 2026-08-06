#!/usr/bin/env python3
"""Tests for the inert prefix-campaign source overlay."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/prefix-campaign-source.py"
SPEC = importlib.util.spec_from_file_location(
    "prefix_campaign_source",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
SOURCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SOURCE)

AUTHORITY = ROOT / "Kandelo/prefix-campaign-authority.json"
MANIFEST = ROOT / "Kandelo/campaigns/prefix-v1/manifest.json"
SOURCE_ROOT = ROOT / "Kandelo/campaigns/prefix-v1/source"
EXPECTED_SOURCE_TREE_GIT_OID = "f9ec87e3b50beea1c71cede57abe160e639fb5d8"
EXPECTED_TARGET_TREE_GIT_OID = "7d22236c4234fe91100d19f5bf72214e5f191c8a"
EXPECTED_BASE_COMMIT = "2e192c8cf318044078e5426d39717636131cec60"
PRE_CUTOVER_FIXTURE_COMMIT = "d98a00a0c087e366aa95a3b0b2c73c5eb8181f3f"
PROMOTED_NON_FORMULA_PRODUCT_PATHS = (
    "Kandelo/formula_support/kandelo_formula_support.rb",
    "Kandelo/formula_support/test/kandelo_formula_support_test.rb",
    "Kandelo/formula_support/test/run-browser-wasm.test.ts",
    "Kandelo/recipes/homebrew-bootstrap/PATCH-LICENSE.md",
    "Kandelo/recipes/homebrew-bootstrap/build.sh",
    "Kandelo/recipes/homebrew-bootstrap/patches/"
    "0001-add-kandelo-wasm-bottle-tags.patch",
    "Kandelo/recipes/homebrew-bootstrap/recipe.json",
    "Kandelo/recipes/homebrew-bootstrap/source-lock.json",
    "Kandelo/recipes/homebrew-bootstrap/verify-source-lock.rb",
    "Kandelo/recipes/ruby/build.sh",
    "Kandelo/recipes/ruby/recipe.json",
)


class PrefixCampaignSourceTests(unittest.TestCase):
    def post_cutover_fixture(
        self,
        directory: str,
    ) -> pathlib.Path:
        root = pathlib.Path(directory) / "post-cutover"
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--shared",
                "--no-checkout",
                str(ROOT),
                str(root),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "checkout",
                "--quiet",
                "--detach",
                PRE_CUTOVER_FIXTURE_COMMIT,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        manifest = json.loads(
            (root / MANIFEST.relative_to(ROOT)).read_text(encoding="utf-8")
        )
        target_modes = {
            file_record["path"]: file_record["target"]["mode"]
            for file_record in manifest["files"]
        }
        for path_value in PROMOTED_NON_FORMULA_PRODUCT_PATHS:
            path = pathlib.PurePosixPath(path_value)
            source = (
                root
                / SOURCE_ROOT.relative_to(ROOT)
                / pathlib.Path(*path.parts)
            )
            destination = root / pathlib.Path(*path.parts)
            shutil.copyfile(source, destination, follow_symlinks=False)
            os.chmod(
                destination,
                0o755 if target_modes[path_value] == "100755" else 0o644,
            )
        return root

    def materialize_post_cutover(
        self,
        *,
        root: pathlib.Path,
        output: pathlib.Path,
    ) -> dict[str, object]:
        return SOURCE.materialize(
            root=root,
            authority_path=root / AUTHORITY.relative_to(ROOT),
            manifest_path=root / MANIFEST.relative_to(ROOT),
            output=output,
            require_live_base=False,
        )

    def assert_promoted_product_support_is_exact(
        self,
        root: pathlib.Path,
    ) -> None:
        for path_value in PROMOTED_NON_FORMULA_PRODUCT_PATHS:
            path = pathlib.Path(path_value)
            self.assertEqual(
                (root / path).read_bytes(),
                (
                    root
                    / SOURCE_ROOT.relative_to(ROOT)
                    / path
                ).read_bytes(),
                path_value,
            )
        active_helper = (
            root / "Kandelo/formula_support/kandelo_formula_support.rb"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'KANDELO_GUEST_HOMEBREW_PREFIX = "/opt/kandelo/homebrew"',
            active_helper,
        )

    def test_materialization_requires_live_base_by_default(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.post_cutover_fixture(directory)
            output = pathlib.Path(directory) / "target"
            with self.assertRaisesRegex(
                SOURCE.SourceError,
                "live pre-cutover path .* differs",
            ):
                SOURCE.materialize(
                    root=root,
                    authority_path=root / AUTHORITY.relative_to(ROOT),
                    manifest_path=root / MANIFEST.relative_to(ROOT),
                    output=output,
                )

    def test_post_cutover_materialization_is_exact_reviewed_target_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.post_cutover_fixture(directory)
            output = pathlib.Path(directory) / "target"
            summary = self.materialize_post_cutover(
                root=root,
                output=output,
            )
            self.assertEqual(summary["files"], 48)
            self.assertEqual(summary["base_commit"], EXPECTED_BASE_COMMIT)
            self.assertEqual(
                summary["source_tree_git_oid"],
                EXPECTED_SOURCE_TREE_GIT_OID,
            )
            self.assertEqual(
                summary["target_tree_git_oid"],
                EXPECTED_TARGET_TREE_GIT_OID,
            )
            self.assert_promoted_product_support_is_exact(root)
            self.assertEqual(
                SOURCE.source_tree_oid(output),
                EXPECTED_TARGET_TREE_GIT_OID,
            )
            expected_revisions = {
                "file-formula": 1,
                "findutils": 1,
                "homebrew-bootstrap": 1,
                "less": 2,
                "libyaml": 1,
                "ruby": 2,
                "vim": 1,
            }
            for formula, revision in expected_revisions.items():
                source = (output / "Formula" / f"{formula}.rb").read_text()
                self.assertEqual(
                    source.count(f"\n  revision {revision}\n"),
                    1,
                )
            ruby_recipe = json.loads(
                (
                    output / "Kandelo/recipes/ruby/recipe.json"
                ).read_text(encoding="utf-8")
            )
            ruby_files = [item["path"] for item in ruby_recipe["files"]]
            self.assertEqual(ruby_files, sorted(set(ruby_files)))
            ruby_formula = (output / "Formula/ruby.rb").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                'manifest_sha256: "7b9b4f2a94665b1a81bffe90452d4c28'
                '188d0ff325322b05c01c469126e507e2"',
                ruby_formula,
            )

    def test_materialized_prefix_contracts_are_mutually_green(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.post_cutover_fixture(directory)
            target = pathlib.Path(directory) / "target"
            self.materialize_post_cutover(
                root=root,
                output=target,
            )
            commands = [
                [
                    sys.executable,
                    str(
                        target
                        / "scripts/"
                        "test_guest_prefix_cutover_inventory.py"
                    ),
                ],
                [
                    sys.executable,
                    str(
                        target
                        / "scripts/"
                        "test_homebrew_bootstrap_formula_migration.py"
                    ),
                ],
                [
                    "ruby",
                    str(
                        target
                        / "Kandelo/formula_support/test/"
                        "kandelo_formula_support_test.rb"
                    ),
                    "--name",
                    "/guest_homebrew_paths_use_kandelo_identity|"
                    "formula_sources_use_the_shared_guest_homebrew_prefix|"
                    "ruby_closed_recipe_uses_only_sealed_source_and_"
                    "transform_inputs|"
                    "ruby_closed_recipe_owns_the_posix_spawn_backend|"
                    "tap_recipe_helper_exposes_formula_and_package_versions|"
                    "tap_recipe_helper_owns_the_package_version_environment/",
                ],
            ]
            for command in commands:
                result = subprocess.run(
                    command,
                    cwd=target,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{command[0]} contract failed:\n{result.stdout}",
                )

    def test_authority_rejects_a_different_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = pathlib.Path(directory)
            authority = temporary / "authority.json"
            value = json.loads(AUTHORITY.read_text(encoding="utf-8"))
            value["target_source"]["manifest_sha256"] = "0" * 64
            authority.write_bytes(SOURCE.canonical_json(value))
            with self.assertRaisesRegex(
                SOURCE.SourceError,
                "authority differs",
            ):
                SOURCE.verify_source(
                    root=ROOT,
                    authority_path=authority,
                    manifest_path=MANIFEST,
                    require_live_base=False,
                )

    def test_unsealed_source_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = pathlib.Path(directory)
            campaign = temporary / "Kandelo/campaigns/prefix-v1"
            shutil.copytree(SOURCE_ROOT, campaign / "source")
            shutil.copyfile(MANIFEST, campaign / "manifest.json")
            authority = json.loads(
                AUTHORITY.read_text(encoding="utf-8")
            )
            authority["target_source"]["source_tree_git_oid"] = (
                SOURCE.source_tree_oid(campaign / "source")
            )
            authority_path = (
                temporary / "Kandelo/prefix-campaign-authority.json"
            )
            authority_path.parent.mkdir(parents=True, exist_ok=True)
            authority_path.write_bytes(SOURCE.canonical_json(authority))
            (campaign / "source/extra").write_text(
                "not sealed\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(SOURCE, "verify_target_tree"),
                self.assertRaisesRegex(
                    SOURCE.SourceError,
                    "authority differs|unsealed",
                ),
            ):
                SOURCE.verify_source(
                    root=temporary,
                    authority_path=authority_path,
                    manifest_path=campaign / "manifest.json",
                    require_live_base=False,
                )


if __name__ == "__main__":
    unittest.main()
