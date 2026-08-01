#!/usr/bin/env python3
"""Tests for the one-time prefix-campaign source lifecycle."""

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
from collections.abc import Callable
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
COMPLETION = ROOT / "Kandelo/campaigns/prefix-v1/completion.json"
MANIFEST = ROOT / "Kandelo/campaigns/prefix-v1/manifest.json"
SOURCE_ROOT = ROOT / "Kandelo/campaigns/prefix-v1/source"


def run_git(root: pathlib.Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def commit_all(root: pathlib.Path, message: str) -> str:
    run_git(root, "add", "-A")
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=root,
        check=True,
        env={
            **dict(os.environ),
            "GIT_AUTHOR_NAME": "Prefix Test",
            "GIT_AUTHOR_EMAIL": "prefix-test@example.invalid",
            "GIT_COMMITTER_NAME": "Prefix Test",
            "GIT_COMMITTER_EMAIL": "prefix-test@example.invalid",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return run_git(root, "rev-parse", "HEAD")


def completion_fixture(
    root: pathlib.Path,
    *,
    authority_state: str = "active",
    keep_workflow: bool = False,
    mutate_completion: Callable[[dict[str, object]], None] | None = None,
) -> tuple[pathlib.Path, dict[str, object], str]:
    run_git(root, "init", "-q")
    campaign_sha256 = "1" * 64
    target_source = {
        "manifest_path": "Kandelo/campaigns/prefix-v1/manifest.json",
        "manifest_sha256": "2" * 64,
        "source_root": "Kandelo/campaigns/prefix-v1/source",
        "source_tree_git_oid": "3" * 40,
        "target_tree_git_oid": "4" * 40,
    }
    authority = {
        "campaign_release": {
            "repository": "kandelo-dev/homebrew-tap-core",
            "tag": (
                "homebrew-prefix-campaign-sha256-"
                f"{campaign_sha256}"
            ),
        },
        "kandelo_commit": "5" * 40,
        "kandelo_repository": "Automattic/kandelo",
        "kind": "kandelo-homebrew-prefix-campaign-caller-authority",
        "package_generations": {
            "browser_inputs_wasm32": "browser-32",
            "browser_inputs_wasm64": "browser-64",
            "rootfs_wasm32": "rootfs-32",
        },
        "release_tag": "bottles-abi-v42",
        "reusable_workflow_commit": "5" * 40,
        "schema": 1,
        "source_tap_commit": "6" * 40,
        "source_tap_name": "kandelo-dev/tap-core",
        "source_tap_repository": "kandelo-dev/homebrew-tap-core",
        "state": authority_state,
        "target_source": target_source,
    }
    authority_path = root / "Kandelo/prefix-campaign-authority.json"
    authority_path.parent.mkdir(parents=True)
    authority_path.write_bytes(SOURCE.canonical_json(authority))
    workflow = root / ".github/workflows/prefix-campaign-bottles.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: fixture\n", encoding="utf-8")
    campaign = root / "Kandelo/campaigns/prefix-v1"
    (campaign / "source/Formula").mkdir(parents=True)
    (campaign / "manifest.json").write_text("{}\n", encoding="utf-8")
    (campaign / "source/Formula/ruby.rb").write_text(
        "class Ruby; end\n",
        encoding="utf-8",
    )
    parent = commit_all(root, "active campaign")

    completion = {
        "campaign": "prefix-v1",
        "campaign_release": {
            "manifest_sha256": campaign_sha256,
            "repository": "kandelo-dev/homebrew-tap-core",
            "tag": (
                "homebrew-prefix-campaign-sha256-"
                f"{campaign_sha256}"
            ),
        },
        "catalog_cohort_sha256": "7" * 64,
        "expected_parent_commit": parent,
        "guest_layout_sha256": "8" * 64,
        "handoffs_sha256": "9" * 64,
        "kind": "kandelo-homebrew-prefix-campaign-completion",
        "schema": 1,
        "source": {
            "manifest_sha256": target_source["manifest_sha256"],
            "source_tree_git_oid": target_source[
                "source_tree_git_oid"
            ],
            "target_tree_git_oid": target_source[
                "target_tree_git_oid"
            ],
        },
    }
    if mutate_completion is not None:
        mutate_completion(completion)
    authority_path.unlink()
    if not keep_workflow:
        workflow.unlink()
    (campaign / "manifest.json").unlink()
    shutil.rmtree(campaign / "source")
    completion_path = campaign / "completion.json"
    completion_path.write_bytes(SOURCE.canonical_json(completion))
    commit_all(root, "retire campaign")
    return completion_path, completion, parent


def copy_active_source_fixture(destination: pathlib.Path) -> None:
    if AUTHORITY.exists():
        authority_payload = AUTHORITY.read_bytes()
        manifest_payload = MANIFEST.read_bytes()

        def payload(path: str) -> bytes:
            return (ROOT / path).read_bytes()

        def source_payload(path: str) -> bytes:
            return (SOURCE_ROOT / path).read_bytes()
    else:
        completion, _payload = SOURCE.load_completion(COMPLETION)
        revision = completion["expected_parent_commit"]

        def from_git(path: str) -> bytes:
            return subprocess.run(
                ["git", "show", f"{revision}:{path}"],
                cwd=ROOT,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout

        authority_payload = from_git(
            "Kandelo/prefix-campaign-authority.json"
        )
        manifest_payload = from_git(
            "Kandelo/campaigns/prefix-v1/manifest.json"
        )
        payload = from_git

        def source_payload(path: str) -> bytes:
            return from_git(
                f"Kandelo/campaigns/prefix-v1/source/{path}"
            )

    manifest = json.loads(manifest_payload)
    authority_path = destination / "Kandelo/prefix-campaign-authority.json"
    manifest_path = destination / "Kandelo/campaigns/prefix-v1/manifest.json"
    authority_path.parent.mkdir(parents=True)
    manifest_path.parent.mkdir(parents=True)
    authority_path.write_bytes(authority_payload)
    manifest_path.write_bytes(manifest_payload)
    for item in manifest["files"]:
        relative = item["path"]
        source = destination / manifest["source_root"] / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(source_payload(relative))
        source.chmod(
            0o755 if item["target"]["mode"] == "100755" else 0o644
        )
        if item["base"] is not None:
            live = destination / relative
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_bytes(payload(relative))
            live.chmod(
                0o755 if item["base"]["mode"] == "100755" else 0o644
            )


class PrefixCampaignSourceTests(unittest.TestCase):
    def test_checked_in_lifecycle_is_exact(self) -> None:
        summary = SOURCE.verify_lifecycle(
            root=ROOT,
            authority_path=AUTHORITY,
            manifest_path=MANIFEST,
            completion_path=COMPLETION,
            require_live_base=True,
            require_git_history=True,
        )
        if COMPLETION.exists():
            self.assertEqual(summary["state"], "retired")
        else:
            self.assertEqual(summary["files"], 41)

    @unittest.skipIf(COMPLETION.exists(), "prefix-v1 is retired")
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
            "8b8b5b12d687b26d071718eebdddb689d4f17fd5",
        )
        self.assertEqual(
            summary["target_tree_git_oid"],
            "77f06a124c2693031d84220cff57f8e351ddcf63",
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

    @unittest.skipIf(COMPLETION.exists(), "prefix-v1 is retired")
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
                "77f06a124c2693031d84220cff57f8e351ddcf63",
            )

    @unittest.skipIf(COMPLETION.exists(), "prefix-v1 is retired")
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

    @unittest.skipIf(COMPLETION.exists(), "prefix-v1 is retired")
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

    @unittest.skipIf(COMPLETION.exists(), "prefix-v1 is retired")
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

    def test_ruby_cannot_mutate_before_atomic_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = pathlib.Path(directory)
            copy_active_source_fixture(temporary)
            ruby = temporary / "Formula/ruby.rb"
            ruby.write_bytes(ruby.read_bytes() + b"# premature mutation\n")
            with (
                mock.patch.object(SOURCE, "verify_target_tree"),
                self.assertRaisesRegex(
                    SOURCE.SourceError,
                    "live pre-cutover path Formula/ruby.rb differs",
                ),
            ):
                SOURCE.verify_source(
                    root=temporary,
                    authority_path=(
                        temporary
                        / "Kandelo/prefix-campaign-authority.json"
                    ),
                    manifest_path=(
                        temporary
                        / "Kandelo/campaigns/prefix-v1/manifest.json"
                    ),
                    require_live_base=True,
                )

    def test_completion_retires_all_write_authority_at_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            completion_path, _completion, parent = completion_fixture(root)
            summary = SOURCE.verify_completion(
                root=root,
                completion_path=completion_path,
                require_git_history=True,
            )
            self.assertEqual(summary["state"], "retired")
            self.assertEqual(summary["expected_parent_commit"], parent)

    def test_completion_rejects_a_different_expected_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            completion_path, _completion, _parent = completion_fixture(
                root,
                mutate_completion=lambda value: value.__setitem__(
                    "expected_parent_commit",
                    "a" * 40,
                ),
            )
            with self.assertRaisesRegex(
                SOURCE.SourceError,
                "different live parent",
            ):
                SOURCE.verify_completion(
                    root=root,
                    completion_path=completion_path,
                    require_git_history=True,
                )

    def test_completion_rejects_inert_parent_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            completion_path, _completion, _parent = completion_fixture(
                root,
                authority_state="inert",
            )
            with self.assertRaisesRegex(
                SOURCE.SourceError,
                "not active and exact",
            ):
                SOURCE.verify_completion(
                    root=root,
                    completion_path=completion_path,
                    require_git_history=True,
                )

    def test_completion_rejects_partial_workflow_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            completion_path, _completion, _parent = completion_fixture(
                root,
                keep_workflow=True,
            )
            with self.assertRaisesRegex(
                SOURCE.SourceError,
                "is still live",
            ):
                SOURCE.verify_completion(
                    root=root,
                    completion_path=completion_path,
                    require_git_history=True,
                )

    def test_completion_rejects_changed_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)

            def mutate(value: dict[str, object]) -> None:
                source = value["source"]
                assert isinstance(source, dict)
                source["target_tree_git_oid"] = "a" * 40

            completion_path, _completion, _parent = completion_fixture(
                root,
                mutate_completion=mutate,
            )
            with self.assertRaisesRegex(
                SOURCE.SourceError,
                "differs from its active source",
            ):
                SOURCE.verify_completion(
                    root=root,
                    completion_path=completion_path,
                    require_git_history=True,
                )

    def test_lifecycle_rejects_authority_and_completion_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            completion_path, _completion, _parent = completion_fixture(root)
            authority = root / "Kandelo/prefix-campaign-authority.json"
            authority.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                SOURCE.SourceError,
                "exactly one",
            ):
                SOURCE.verify_lifecycle(
                    root=root,
                    authority_path=authority,
                    manifest_path=(
                        root / "Kandelo/campaigns/prefix-v1/manifest.json"
                    ),
                    completion_path=completion_path,
                    require_live_base=True,
                    require_git_history=True,
                )


if __name__ == "__main__":
    unittest.main()
