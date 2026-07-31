#!/usr/bin/env python3
"""Tests for the inert prefix-campaign source overlay."""

from __future__ import annotations

import importlib.util
import json
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


class PrefixCampaignSourceTests(unittest.TestCase):
    def test_checked_in_overlay_is_exact_and_live_tree_stays_at_base(
        self,
    ) -> None:
        summary = SOURCE.verify_source(
            root=ROOT,
            authority_path=AUTHORITY,
            manifest_path=MANIFEST,
            require_live_base=True,
        )
        self.assertEqual(summary["files"], 41)
        self.assertEqual(
            summary["base_commit"],
            "2e192c8cf318044078e5426d39717636131cec60",
        )
        self.assertEqual(
            summary["source_tree_git_oid"],
            "aedd9a2e443f18d651ae7c3c1c3e23ed012a474c",
        )
        self.assertEqual(
            summary["target_tree_git_oid"],
            "a0645bd773df86b89d70083a5883ff40a1b4c88f",
        )

        active_helper = (
            ROOT / "Kandelo/formula_support/kandelo_formula_support.rb"
        ).read_text(encoding="utf-8")
        target_helper = (
            SOURCE_ROOT
            / "Kandelo/formula_support/kandelo_formula_support.rb"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            'KANDELO_GUEST_HOMEBREW_PREFIX = "/opt/kandelo/homebrew"',
            active_helper,
        )
        self.assertIn(
            'KANDELO_GUEST_HOMEBREW_PREFIX = "/opt/kandelo/homebrew"',
            target_helper,
        )

    def test_materialization_is_the_exact_reviewed_target_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory) / "target"
            SOURCE.materialize(
                root=ROOT,
                authority_path=AUTHORITY,
                manifest_path=MANIFEST,
                output=output,
            )
            self.assertEqual(
                SOURCE.source_tree_oid(output),
                "a0645bd773df86b89d70083a5883ff40a1b4c88f",
            )

    def test_materialized_prefix_contracts_are_mutually_green(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "target"
            SOURCE.materialize(
                root=ROOT,
                authority_path=AUTHORITY,
                manifest_path=MANIFEST,
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
                    "formula_sources_use_the_shared_guest_homebrew_prefix/",
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
