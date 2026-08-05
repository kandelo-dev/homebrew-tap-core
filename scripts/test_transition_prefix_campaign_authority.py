#!/usr/bin/env python3
"""Tests for the two-commit prefix-campaign authority transition."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).with_name(
    "transition-prefix-campaign-authority.py"
)
SPEC = importlib.util.spec_from_file_location(
    "transition_prefix_campaign_authority", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
transition = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = transition
SPEC.loader.exec_module(transition)
ROOT = SCRIPT.parent.parent
NEW_GENERATION = (
    "package-generation-rootfs-wasm32-abi-v42-sha256-" + "9" * 64
)
SUCCESSOR_TARGET = {
    "manifest_path": "Kandelo/campaigns/prefix-v1/manifest.json",
    "manifest_sha256": "a" * 64,
    "source_root": "Kandelo/campaigns/prefix-v1/source",
    "source_tree_git_oid": "b" * 40,
    "target_tree_git_oid": "c" * 40,
}
FIXTURE_ARCHIVE_PATH = ROOT / transition.ARCHIVE_ROOT / (
    "f90144f439caa3806cbd145fc0d5f34ddbf6905d43a15b023106389696376de0.json"
)


def active_fixture_authority(source: dict[str, object]) -> dict[str, object]:
    archive, _payload = transition.load_json(
        FIXTURE_ARCHIVE_PATH, "fixture predecessor archive"
    )
    archived = archive["authority"]
    value = copy.deepcopy(source)
    value["campaign_release"]["tag"] = archived["campaign_release"]["tag"]
    value["kandelo_commit"] = archived["kandelo_commit"]
    value["package_generations"]["rootfs_wasm32"] = archived[
        "rootfs_wasm32"
    ]
    value["reusable_workflow_commit"] = archived["kandelo_commit"]
    value["source_tap_commit"] = archived["source_tap_commit"]
    value["state"] = "active"
    value["target_source"] = copy.deepcopy(archived["target_source"])
    transition.validate_authority(value, state="active")
    return value


class PrefixCampaignTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        authority_path = self.root / transition.AUTHORITY_PATH
        authority_path.parent.mkdir(parents=True)
        source_authority, _source_payload = transition.load_json(
            ROOT / transition.AUTHORITY_PATH,
            "checked-in campaign authority",
        )
        authority_path.write_bytes(
            transition.canonical(active_fixture_authority(source_authority))
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Kandelo Test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "add", transition.AUTHORITY_PATH.as_posix()],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "activate fixture"],
            cwd=self.root,
            check=True,
        )
        self.activation_commit = self.head()
        self.active, self.active_payload = transition.load_json(
            authority_path, "fixture authority"
        )
        self.archive_path = self.write_archive(self.archive_document())
        self.extra_archive_path = self.write_extra_archive()
        self.graph_relative = (
            "Kandelo/campaigns/prefix-v1/successor/test-graph.json"
        )
        self.scope_relative = (
            "Kandelo/campaigns/prefix-v1/successor/test-scope.json"
        )
        self.graph_path = self.write_repository_json(
            self.graph_relative, self.graph_document()
        )
        self.scope_path = self.write_repository_json(
            self.scope_relative, self.scope_document()
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def head(self) -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True
        ).strip()

    def archive_document(self) -> dict[str, object]:
        return {
            "abandoned_at": "2026-08-04T03:45:13Z",
            "authority": {
                "activation_commit": self.activation_commit,
                "campaign_release": {
                    "id": 364554275,
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "tag": self.active["campaign_release"]["tag"],
                },
                "kandelo_commit": self.active["kandelo_commit"],
                "payload_sha256": hashlib.sha256(
                    self.active_payload
                ).hexdigest(),
                "rootfs_wasm32": self.active["package_generations"][
                    "rootfs_wasm32"
                ],
                "source_tap_commit": self.active["source_tap_commit"],
                "target_source": self.active["target_source"],
            },
            "cause": {
                "corrective_workstream": "test successor",
                "kind": "frozen-publisher-requires-successor-campaign",
                "summary": "The test publisher is frozen.",
            },
            "dispatches": [
                {
                    "arch": "wasm32",
                    "formula": "make",
                    "handoff_release": {
                        "id": 364590983,
                        "tag": "homebrew-prefix-handoff-sha256-" + "7" * 64,
                    },
                    "result": "handoff-published-and-publicly-verified",
                    "run_id": 30870822055,
                }
            ],
            "kind": "kandelo-homebrew-prefix-abandoned-campaign",
            "recovery": {
                "authority_state": "armed",
                "fresh_builds_require_successor_campaign": True,
                "partial_publications_require_successor_revalidation": True,
                "predecessor_handoffs_are_not_successor_authority": True,
                "published_handoffs_remain_independently_usable": True,
            },
            "schema": 1,
        }

    def write_archive(self, value: dict[str, object]) -> pathlib.Path:
        match = transition.CAMPAIGN.fullmatch(
            self.active["campaign_release"]["tag"]
        )
        assert match is not None
        path = self.root / transition.ARCHIVE_ROOT / f"{match.group(1)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(transition.canonical(value))
        return path

    def write_extra_archive(self) -> pathlib.Path:
        value = self.archive_document()
        campaign_sha = "6" * 64
        value["authority"]["campaign_release"]["tag"] = (
            "homebrew-prefix-campaign-sha256-" + campaign_sha
        )
        value["dispatches"] = [
            {
                "arch": "wasm32",
                "formula": "zlib",
                "handoff_release": {
                    "id": 364590984,
                    "tag": "homebrew-prefix-handoff-sha256-" + "5" * 64,
                },
                "result": "handoff-published-and-publicly-verified",
                "run_id": 30870822056,
            }
        ]
        path = self.root / transition.ARCHIVE_ROOT / f"{campaign_sha}.json"
        path.write_bytes(transition.canonical(value))
        return path

    def write_repository_json(
        self, relative: str, value: dict[str, object]
    ) -> pathlib.Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(transition.canonical(value))
        return path

    def graph_document(self) -> dict[str, object]:
        return {
            "kind": transition.GRAPH_KIND,
            "max_active": 2,
            "repository": "Kandelo-dev/homebrew-tap-core",
            "schema": 1,
            "tasks": [
                {"arch": "wasm32", "formula": "make"},
                {"arch": "wasm32", "formula": "ruby"},
            ],
            "workflow": ".github/workflows/prefix-campaign-bottles.yml",
        }

    def scope_document(self) -> dict[str, object]:
        archive_relative = self.archive_path.relative_to(self.root).as_posix()
        return {
            "build_tasks": [{"arch": "wasm32", "formula": "ruby"}],
            "graph": {
                "path": self.graph_relative,
                "sha256": hashlib.sha256(
                    self.graph_path.read_bytes()
                ).hexdigest(),
            },
            "kind": transition.SCOPE_KIND,
            "predecessor_archive": {
                "path": archive_relative,
                "sha256": hashlib.sha256(
                    self.archive_path.read_bytes()
                ).hexdigest(),
            },
            "reuse_tasks": [{"arch": "wasm32", "formula": "make"}],
            "schema": 1,
        }

    def archive_arguments(
        self,
        *,
        apply: bool = False,
        successor_target: bool = False,
    ) -> list[str]:
        values = [
            "archive-active",
            "--root",
            str(self.root),
            "--archive",
            str(self.archive_path),
            "--activation-commit",
            self.activation_commit,
        ]
        if successor_target:
            values.extend(
                [
                    "--successor-manifest-sha256",
                    SUCCESSOR_TARGET["manifest_sha256"],
                    "--successor-source-tree-git-oid",
                    SUCCESSOR_TARGET["source_tree_git_oid"],
                    "--successor-target-tree-git-oid",
                    SUCCESSOR_TARGET["target_tree_git_oid"],
                ]
            )
        if apply:
            values.append("--apply")
        return values

    def arm_and_commit(self) -> str:
        self.assertEqual(0, transition.main(self.archive_arguments(apply=True)))
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "arm successor"],
            cwd=self.root,
            check=True,
        )
        return self.head()

    def commit_json(
        self,
        path: pathlib.Path,
        value: dict[str, object],
        message: str,
    ) -> str:
        path.write_bytes(transition.canonical(value))
        subprocess.run(["git", "add", path], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", message],
            cwd=self.root,
            check=True,
        )
        return self.head()

    def campaign_document(self, source_commit: str) -> dict[str, object]:
        armed, _payload = transition.load_json(
            self.root / transition.AUTHORITY_PATH,
            "armed authority",
        )
        target = armed["target_source"]
        archive, archive_payload, handoffs = transition.parse_archive(
            self.archive_path
        )
        archive_relative = self.archive_path.relative_to(self.root).as_posix()
        make_handoff = handoffs[("make", "wasm32")]
        return {
            "authority": {
                "current_kandelo_abi": 42,
                "kandelo_commit": armed["kandelo_commit"],
                "predecessor_recovery": [
                    transition.archive_recovery_record(
                        archive, archive_relative, archive_payload
                    )
                ],
                "predecessor_recovery_source": {
                    "commit": source_commit,
                    "repository": "kandelo-dev/homebrew-tap-core",
                },
                "source_materialization": {
                    "kind": "sealed-target-overlay-v1",
                    "manifest": {
                        "path": target["manifest_path"],
                        "sha256": target["manifest_sha256"],
                    },
                    "source_root": target["source_root"],
                    "source_tree_git_oid": target["source_tree_git_oid"],
                    "target_tree_git_oid": target["target_tree_git_oid"],
                },
                "source_tap_commit": source_commit,
                "successor_scope": {
                    "path": self.scope_relative,
                    "sha256": hashlib.sha256(
                        self.scope_path.read_bytes()
                    ).hexdigest(),
                },
                "tap_name": "kandelo-dev/tap-core",
                "tap_repository": "kandelo-dev/homebrew-tap-core",
            },
            "formulae": [
                {
                    "name": "make",
                    "variants": [
                        {
                            "arch": "wasm32",
                            "disposition": {"kind": "required-rebuild"},
                            "reuse_source": {
                                "arch": "wasm32",
                                "campaign_tag": archive["authority"][
                                    "campaign_release"
                                ]["tag"],
                                "handoff_tag": make_handoff,
                                "kind": "predecessor-handoff",
                            },
                        }
                    ],
                },
                {
                    "name": "ruby",
                    "variants": [
                        {
                            "arch": "wasm32",
                            "disposition": {"kind": "required-build"},
                        }
                    ],
                },
            ],
            "kind": "kandelo-homebrew-guest-prefix-campaign",
            "schema": 3,
        }

    def write_campaign(self, value: dict[str, object]) -> pathlib.Path:
        path = self.root.parent / f"{self.root.name}-campaign.json"
        path.write_bytes(transition.canonical(value))
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def activate_arguments(
        self,
        campaign: pathlib.Path,
        source_commit: str,
        *,
        apply: bool = False,
    ) -> list[str]:
        values = [
            "activate-successor",
            "--root",
            str(self.root),
            "--campaign",
            str(campaign),
            "--scope",
            self.scope_relative,
            "--rootfs-generation",
            NEW_GENERATION,
            "--source-tap-commit",
            source_commit,
        ]
        if apply:
            values.append("--apply")
        return values

    def test_two_commit_transition_is_previewable_and_data_only(self) -> None:
        before = (self.root / transition.AUTHORITY_PATH).read_bytes()
        self.assertEqual(0, transition.main(self.archive_arguments()))
        self.assertEqual(
            before, (self.root / transition.AUTHORITY_PATH).read_bytes()
        )
        self.assertEqual(
            0, transition.main(self.archive_arguments(apply=True))
        )
        armed_once = (self.root / transition.AUTHORITY_PATH).read_bytes()
        self.assertEqual(
            0, transition.main(self.archive_arguments(apply=True))
        )
        self.assertEqual(
            armed_once, (self.root / transition.AUTHORITY_PATH).read_bytes()
        )
        source_commit = self.arm_and_commit()
        armed, _payload = transition.load_json(
            self.root / transition.AUTHORITY_PATH, "armed authority"
        )
        transition.validate_authority(armed, state="armed")
        campaign = self.write_campaign(
            self.campaign_document(source_commit)
        )
        armed_bytes = (self.root / transition.AUTHORITY_PATH).read_bytes()
        self.assertEqual(
            0,
            transition.main(
                self.activate_arguments(campaign, source_commit)
            ),
        )
        self.assertEqual(
            armed_bytes, (self.root / transition.AUTHORITY_PATH).read_bytes()
        )
        self.assertEqual(
            0,
            transition.main(
                self.activate_arguments(
                    campaign, source_commit, apply=True
                )
            ),
        )
        active_once = (self.root / transition.AUTHORITY_PATH).read_bytes()
        self.assertEqual(
            0,
            transition.main(
                self.activate_arguments(
                    campaign, source_commit, apply=True
                )
            ),
        )
        self.assertEqual(
            active_once, (self.root / transition.AUTHORITY_PATH).read_bytes()
        )
        active, _payload = transition.load_json(
            self.root / transition.AUTHORITY_PATH, "successor authority"
        )
        transition.validate_authority(active, state="active")
        self.assertEqual(source_commit, active["source_tap_commit"])
        self.assertEqual(NEW_GENERATION, active["package_generations"]["rootfs_wasm32"])
        status = subprocess.check_output(
            ["git", "status", "--porcelain=v1"],
            cwd=self.root,
            text=True,
        )
        self.assertEqual(
            f" M {transition.AUTHORITY_PATH.as_posix()}\n", status
        )

    def test_active_fixture_is_independent_of_repository_state(self) -> None:
        source, _payload = transition.load_json(
            ROOT / transition.AUTHORITY_PATH,
            "checked-in campaign authority",
        )
        active = active_fixture_authority(source)
        armed = transition.armed_authority(active)
        armed["kandelo_commit"] = "8" * 40
        armed["reusable_workflow_commit"] = "8" * 40
        self.assertEqual(active, active_fixture_authority(active))
        self.assertEqual(active, active_fixture_authority(armed))

    def test_archive_can_atomically_select_verified_successor_source(
        self,
    ) -> None:
        authority_path = self.root / transition.AUTHORITY_PATH
        with mock.patch.object(
            transition, "verify_successor_target_source"
        ) as verify:
            arguments = self.archive_arguments(
                apply=True,
                successor_target=True,
            )
            self.assertEqual(0, transition.main(arguments))
            armed, _payload = transition.load_json(
                authority_path, "retargeted armed authority"
            )
            self.assertEqual(SUCCESSOR_TARGET, armed["target_source"])
            verify.assert_called_once_with(
                root=self.root.resolve(),
                authority=armed,
            )
            verify.reset_mock()
            self.assertEqual(0, transition.main(arguments))
            verify.assert_called_once()

    def test_archive_retarget_requires_complete_identity(self) -> None:
        before = (self.root / transition.AUTHORITY_PATH).read_bytes()
        arguments = self.archive_arguments(apply=True)
        arguments.extend(
            [
                "--successor-manifest-sha256",
                SUCCESSOR_TARGET["manifest_sha256"],
            ]
        )
        self.assertEqual(2, transition.main(arguments))
        self.assertEqual(
            before,
            (self.root / transition.AUTHORITY_PATH).read_bytes(),
        )

    def test_archive_retarget_requires_verified_source(self) -> None:
        before = (self.root / transition.AUTHORITY_PATH).read_bytes()
        with mock.patch.object(
            transition,
            "verify_successor_target_source",
            side_effect=transition.TransitionError("test mismatch"),
        ):
            self.assertEqual(
                2,
                transition.main(
                    self.archive_arguments(
                        apply=True,
                        successor_target=True,
                    )
                ),
            )
        self.assertEqual(
            before,
            (self.root / transition.AUTHORITY_PATH).read_bytes(),
        )

    def test_archive_must_match_activation_commit_bytes(self) -> None:
        authority_path = self.root / transition.AUTHORITY_PATH
        value = json.loads(authority_path.read_text())
        value["source_tap_commit"] = "8" * 40
        authority_path.write_bytes(transition.canonical(value))
        self.assertEqual(2, transition.main(self.archive_arguments()))

    def test_archive_path_is_content_addressed(self) -> None:
        wrong = self.archive_path.with_name("0" * 64 + ".json")
        wrong.write_bytes(self.archive_path.read_bytes())
        self.addCleanup(lambda: wrong.unlink(missing_ok=True))
        arguments = self.archive_arguments()
        arguments[arguments.index(str(self.archive_path))] = str(wrong)
        self.assertEqual(2, transition.main(arguments))

    def test_archive_rejects_duplicate_run_ids(self) -> None:
        document = self.archive_document()
        duplicate = copy.deepcopy(document["dispatches"][0])
        duplicate["formula"] = "dash"
        document["dispatches"].append(duplicate)
        self.write_archive(document)
        self.assertEqual(2, transition.main(self.archive_arguments()))

    def test_activation_requires_exact_armed_head(self) -> None:
        source_commit = self.arm_and_commit()
        campaign = self.write_campaign(
            self.campaign_document(source_commit)
        )
        self.assertEqual(
            2,
            transition.main(
                self.activate_arguments(campaign, "8" * 40)
            ),
        )

    def test_activation_rejects_changed_kandelo_authority(self) -> None:
        source_commit = self.arm_and_commit()
        document = self.campaign_document(source_commit)
        document["authority"]["kandelo_commit"] = "8" * 40
        campaign = self.write_campaign(document)
        self.assertEqual(
            2,
            transition.main(
                self.activate_arguments(campaign, source_commit)
            ),
        )

    def test_activation_rejects_changed_target_source(self) -> None:
        source_commit = self.arm_and_commit()
        document = self.campaign_document(source_commit)
        document["authority"]["source_materialization"]["target_tree_git_oid"] = (
            "8" * 40
        )
        campaign = self.write_campaign(document)
        self.assertEqual(
            2,
            transition.main(
                self.activate_arguments(campaign, source_commit)
            ),
        )

    def test_activation_requires_schema_three_recovery(self) -> None:
        source_commit = self.arm_and_commit()
        for label, mutate in (
            (
                "schema two",
                lambda document: document.__setitem__("schema", 2),
            ),
            (
                "missing recovery",
                lambda document: document["authority"].pop(
                    "predecessor_recovery"
                ),
            ),
            (
                "missing recovery source",
                lambda document: document["authority"].pop(
                    "predecessor_recovery_source"
                ),
            ),
            (
                "missing successor scope",
                lambda document: document["authority"].pop(
                    "successor_scope"
                ),
            ),
        ):
            with self.subTest(label=label):
                document = self.campaign_document(source_commit)
                mutate(document)
                campaign = self.write_campaign(document)
                self.assertEqual(
                    2,
                    transition.main(
                        self.activate_arguments(campaign, source_commit)
                    ),
                )

    def test_activation_binds_successor_scope_authority(self) -> None:
        source_commit = self.arm_and_commit()
        mutations = (
            (
                "scope path",
                lambda scope: scope.__setitem__(
                    "path",
                    "Kandelo/campaigns/prefix-v1/successor/wrong.json",
                ),
            ),
            (
                "scope digest",
                lambda scope: scope.__setitem__("sha256", "8" * 64),
            ),
            (
                "scope field set",
                lambda scope: scope.__setitem__("extra", "unreviewed"),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                document = self.campaign_document(source_commit)
                mutate(document["authority"]["successor_scope"])
                campaign = self.write_campaign(document)
                self.assertEqual(
                    2,
                    transition.main(
                        self.activate_arguments(campaign, source_commit)
                    ),
                )

    def test_activation_binds_exact_predecessor_record(self) -> None:
        source_commit = self.arm_and_commit()

        def recovery(document: dict[str, object]) -> dict[str, object]:
            return document["authority"]["predecessor_recovery"][0]

        mutations = (
            (
                "activation",
                lambda document: recovery(document).__setitem__(
                    "activation_commit", "8" * 40
                ),
            ),
            (
                "archive path",
                lambda document: recovery(document)["archive"].__setitem__(
                    "path", "Kandelo/campaigns/prefix-v1/wrong.json"
                ),
            ),
            (
                "archive digest",
                lambda document: recovery(document)["archive"].__setitem__(
                    "sha256", "8" * 64
                ),
            ),
            (
                "recovery source",
                lambda document: document["authority"][
                    "predecessor_recovery_source"
                ].__setitem__("commit", "8" * 40),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                document = self.campaign_document(source_commit)
                mutate(document)
                campaign = self.write_campaign(document)
                self.assertEqual(
                    2,
                    transition.main(
                        self.activate_arguments(campaign, source_commit)
                    ),
                )

    def test_activation_rejects_incomplete_inventory(self) -> None:
        source_commit = self.arm_and_commit()
        document = self.campaign_document(source_commit)
        document["formulae"].pop()
        campaign = self.write_campaign(document)
        self.assertEqual(
            2,
            transition.main(
                self.activate_arguments(campaign, source_commit)
            ),
        )

    def test_activation_allows_valid_unselected_variants(self) -> None:
        source_commit = self.arm_and_commit()
        document = self.campaign_document(source_commit)
        document["formulae"][1]["variants"].append(
            {
                "arch": "wasm64",
                "disposition": {"kind": "required-build"},
            }
        )
        extra, extra_payload, extra_handoffs = transition.parse_archive(
            self.extra_archive_path
        )
        extra_relative = self.extra_archive_path.relative_to(
            self.root
        ).as_posix()
        extra_record = transition.archive_recovery_record(
            extra, extra_relative, extra_payload
        )
        document["authority"]["predecessor_recovery"].insert(
            0, extra_record
        )
        document["formulae"].append(
            {
                "name": "zlib",
                "variants": [
                    {
                        "arch": "wasm32",
                        "disposition": {"kind": "required-rebuild"},
                        "reuse_source": {
                            "arch": "wasm32",
                            "campaign_tag": extra["authority"][
                                "campaign_release"
                            ]["tag"],
                            "handoff_tag": extra_handoffs[
                                ("zlib", "wasm32")
                            ],
                            "kind": "predecessor-handoff",
                        },
                    }
                ],
            }
        )
        campaign = self.write_campaign(document)
        self.assertEqual(
            0,
            transition.main(
                self.activate_arguments(campaign, source_commit)
            ),
        )

    def test_activation_rejects_unreviewed_worktree_changes(self) -> None:
        source_commit = self.arm_and_commit()
        document = self.campaign_document(source_commit)
        campaign = self.write_campaign(document)
        (self.root / "unreviewed.txt").write_text("not reviewed\n")
        self.assertEqual(
            2,
            transition.main(
                self.activate_arguments(campaign, source_commit)
            ),
        )

    def test_activation_binds_protected_scope_bytes(self) -> None:
        self.arm_and_commit()
        original = json.loads(self.scope_path.read_text())
        mutations = (
            (
                "graph digest",
                lambda scope: scope["graph"].__setitem__(
                    "sha256", "8" * 64
                ),
            ),
            (
                "archive digest",
                lambda scope: scope["predecessor_archive"].__setitem__(
                    "sha256", "8" * 64
                ),
            ),
            (
                "overlapping partition",
                lambda scope: scope["build_tasks"].insert(
                    0, {"arch": "wasm32", "formula": "make"}
                ),
            ),
            (
                "missing partition member",
                lambda scope: scope["build_tasks"].clear(),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = copy.deepcopy(original)
                mutate(changed)
                source_commit = self.commit_json(
                    self.scope_path, changed, f"alter {label}"
                )
                campaign = self.write_campaign(
                    self.campaign_document(source_commit)
                )
                self.assertEqual(
                    2,
                    transition.main(
                        self.activate_arguments(campaign, source_commit)
                    ),
                )
                self.commit_json(
                    self.scope_path, original, f"restore {label}"
                )

        source_commit = self.head()
        document = self.campaign_document(source_commit)
        document["formulae"][0]["variants"][0]["reuse_source"][
            "handoff_tag"
        ] = "homebrew-prefix-handoff-sha256-" + "8" * 64
        campaign = self.write_campaign(document)
        self.assertEqual(
            2,
            transition.main(
                self.activate_arguments(campaign, source_commit)
            ),
        )

        self.scope_path.write_bytes(transition.canonical(original))
        dirty = json.loads(self.scope_path.read_text())
        dirty["graph"]["sha256"] = "8" * 64
        self.scope_path.write_bytes(transition.canonical(dirty))
        campaign = self.write_campaign(
            self.campaign_document(source_commit)
        )
        self.assertEqual(
            2,
            transition.main(
                self.activate_arguments(campaign, source_commit)
            ),
        )


if __name__ == "__main__":
    unittest.main()
