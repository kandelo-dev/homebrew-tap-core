#!/usr/bin/env python3
"""Exercise the protected prefix-campaign controller boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/prefix-campaign-controller.py"
AUTHORITY = ROOT / "Kandelo/prefix-campaign-authority.json"
COMPLETION = ROOT / "Kandelo/campaigns/prefix-v1/completion.json"
SPEC = importlib.util.spec_from_file_location(
    "prefix_campaign_controller",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
CONTROLLER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONTROLLER
SPEC.loader.exec_module(CONTROLLER)

KANDELO_COMMIT = "1" * 40
SOURCE_TAP_COMMIT = "2" * 40
DEPENDENCY_TAG = "homebrew-prefix-handoff-sha256-" + "3" * 64
ROOTFS_GENERATION = (
    "package-generation-rootfs-wasm32-abi-v42-sha256-" + "4" * 64
)
BROWSER_WASM32_GENERATION = (
    "package-generation-browser-inputs-wasm32-abi-v42-sha256-"
    + "5" * 64
)
BROWSER_WASM64_GENERATION = (
    "package-generation-browser-inputs-wasm64-abi-v42-sha256-"
    + "6" * 64
)
DEPENDENCY_FORMULA_SHA256 = "a" * 64
LEAF_FORMULA_SHA256 = "b" * 64


def variant(arch: str, kind: str) -> dict[str, object]:
    return {
        "arch": arch,
        "disposition": {"kind": kind},
    }


def formula_document(
    name: str,
    version: str,
    dependencies: list[dict[str, str]],
    disposition: str,
    arches: tuple[str, ...],
    formula_sha256: str,
) -> dict[str, object]:
    return {
        "dependencies": dependencies,
        "destination": {
            "bottle_rebuild": 1,
        },
        "formula_source": {
            "sha256": formula_sha256,
        },
        "name": name,
        "variants": [
            variant(arch, disposition)
            for arch in arches
        ],
        "version": version,
    }


def campaign_document(
    *,
    formula: str = "leaf",
    disposition: str = "required-build",
    arches: tuple[str, ...] = ("wasm32",),
) -> dict[str, object]:
    selected = formula_document(
        formula,
        "2.0",
        [
            {
                "full_name": "kandelo-dev/tap-core/dependency",
                "version": "1.0",
            }
        ],
        disposition,
        arches,
        LEAF_FORMULA_SHA256,
    )
    formulae = [
        formula_document(
            "dependency",
            "1.0",
            [],
            "required-build",
            ("wasm32",),
            DEPENDENCY_FORMULA_SHA256,
        ),
        selected,
    ]
    return {
        "authority": {
            "kandelo_commit": KANDELO_COMMIT,
            "source_tap_commit": SOURCE_TAP_COMMIT,
            "tap_name": "kandelo-dev/tap-core",
            "tap_repository": "kandelo-dev/homebrew-tap-core",
        },
        "formulae": sorted(formulae, key=lambda item: item["name"]),
    }


def write_pretty(path: pathlib.Path, value: object) -> bytes:
    payload = CONTROLLER.pretty_json(value)
    path.write_bytes(payload)
    return payload


def active_authority(
    campaign_payload: bytes,
) -> dict[str, object]:
    digest = hashlib.sha256(campaign_payload).hexdigest()
    return {
        "campaign_release": {
            "repository": "kandelo-dev/homebrew-tap-core",
            "tag": f"homebrew-prefix-campaign-sha256-{digest}",
        },
        "kandelo_commit": KANDELO_COMMIT,
        "kandelo_repository": "Automattic/kandelo",
        "kind": "kandelo-homebrew-prefix-campaign-caller-authority",
        "package_generations": {
            "browser_inputs_wasm32":
            BROWSER_WASM32_GENERATION,
            "browser_inputs_wasm64":
            BROWSER_WASM64_GENERATION,
            "rootfs_wasm32": ROOTFS_GENERATION,
        },
        "release_tag": "bottles-abi-v42",
        "reusable_workflow_commit": KANDELO_COMMIT,
        "schema": 1,
        "source_tap_commit": SOURCE_TAP_COMMIT,
        "source_tap_name": "kandelo-dev/tap-core",
        "source_tap_repository": "kandelo-dev/homebrew-tap-core",
        "state": "active",
        "target_source": {
            "manifest_path":
            "Kandelo/campaigns/prefix-v1/manifest.json",
            "manifest_sha256": "7" * 64,
            "source_root": "Kandelo/campaigns/prefix-v1/source",
            "source_tree_git_oid": "8" * 40,
            "target_tree_git_oid": "9" * 40,
        },
    }


def event_document(
    *,
    formula: str = "leaf",
    arches: tuple[str, ...] = ("wasm32",),
    dependencies: tuple[tuple[str, str], ...] = (
        ("dependency", DEPENDENCY_TAG),
    ),
) -> dict[str, object]:
    return {
        "action": CONTROLLER.EVENT_TYPE,
        "client_payload": {
            "arches": list(arches),
            "dependency_handoffs": [
                {"formula": name, "tag": tag}
                for name, tag in dependencies
            ],
            "formula": formula,
        },
    }


def handoff_document(
    authority: CONTROLLER.Authority,
    plan: CONTROLLER.TaskPlan,
) -> dict[str, object]:
    formula = plan.formula
    publications = []
    for arch in plan.request.arches:
        files = [
            {
                "asset_name": (
                    f"{arch}.{relative.replace('/', '.')}"
                ),
                "bytes": index + 1,
                "path": f"payload/{arch}/{relative}",
                "sha256": f"{index + 1:x}" * 64,
            }
            for index, relative in enumerate(
                CONTROLLER.HANDOFF_PUBLICATION_FILES
            )
        ]
        publications.append({"arch": arch, "files": files})
    return {
        "campaign": {
            "sha256": hashlib.sha256(
                plan.campaign_payload
            ).hexdigest(),
        },
        "dependency_handoffs": [
            {
                "formula": name,
                "manifest_sha256": tag.removeprefix(
                    "homebrew-prefix-handoff-sha256-"
                ),
                "tag": tag,
            }
            for name, tag in plan.request.dependency_tags
        ],
        "formula": {
            "bottle_rebuild": (
                formula["destination"]["bottle_rebuild"]
            ),
            "dependencies": formula["dependencies"],
            "formula_sha256": (
                formula["formula_source"]["sha256"]
            ),
            "name": formula["name"],
            "version": formula["version"],
        },
        "kind": CONTROLLER.HANDOFF_KIND,
        "publications": publications,
        "schema": 1,
        "source": {
            "kandelo_commit": authority.kandelo_commit,
            "source_tap_commit": authority.source_tap_commit,
            "target_tree_git_oid": authority.target_tree,
            "tap_name": authority.source_tap_name,
            "tap_repository": authority.source_tap_repository,
        },
    }


def tag_for_handoff(value: object) -> str:
    return (
        "homebrew-prefix-handoff-sha256-"
        + hashlib.sha256(CONTROLLER.pretty_json(value)).hexdigest()
    )


class Fixture:
    def __init__(
        self,
        root: pathlib.Path,
        *,
        disposition: str = "required-build",
        arches: tuple[str, ...] = ("wasm32",),
        request_arch: str | None = None,
    ) -> None:
        self.root = root
        self.campaign = root / "campaign.json"
        campaign = campaign_document(
            disposition=disposition,
            arches=arches,
        )
        payload = write_pretty(self.campaign, campaign)
        self.authority = root / "authority.json"
        write_pretty(self.authority, active_authority(payload))
        self.event = root / "event.json"
        write_pretty(
            self.event,
            event_document(
                arches=(request_arch or arches[0],),
            ),
        )
        self.kandelo = root / "kandelo"
        self.source_tap = root / "source-tap"
        self.kandelo.mkdir()
        self.source_tap.mkdir()

    def plan(self) -> object:
        authority = CONTROLLER.load_authority(
            self.authority,
            require_active=True,
        )
        request = CONTROLLER.load_task_request(self.event)
        return CONTROLLER.validate_campaign(
            self.campaign,
            authority,
            request,
        )


class PrefixCampaignControllerTests(unittest.TestCase):
    def test_checked_in_authority_is_inert_or_permanently_retired(
        self,
    ) -> None:
        with self.assertRaises(CONTROLLER.ControllerError) as raised:
            CONTROLLER.load_authority(
                AUTHORITY,
                require_active=True,
            )
        expected_status = (
            "campaign-authority-retired"
            if COMPLETION.exists()
            else "campaign-authority-inert"
        )
        self.assertEqual(
            raised.exception.status,
            expected_status,
        )
        self.assertEqual(
            raised.exception.exit_code,
            CONTROLLER.INERT_EXIT,
        )

    def test_cli_reports_inert_or_retired_before_external_checkout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event = pathlib.Path(directory) / "event.json"
            write_pretty(event, event_document())
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "preflight",
                    "--event",
                    str(event),
                ],
                cwd=ROOT,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertEqual(result.returncode, CONTROLLER.INERT_EXIT)
        expected_status = (
            "campaign-authority-retired"
            if COMPLETION.exists()
            else "campaign-authority-inert"
        )
        self.assertIn(
            f"status={expected_status}",
            result.stderr,
        )

    def test_missing_authority_with_completion_is_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            completion = root / "Kandelo/campaigns/prefix-v1/completion.json"
            completion.parent.mkdir(parents=True)
            completion.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(CONTROLLER.ControllerError) as raised:
                CONTROLLER.load_authority(
                    root / "Kandelo/prefix-campaign-authority.json",
                    require_active=True,
                )
        self.assertEqual(
            raised.exception.status,
            "campaign-authority-retired",
        )
        self.assertEqual(
            raised.exception.exit_code,
            CONTROLLER.INERT_EXIT,
        )

    def test_event_accepts_only_exact_task_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            event = root / "event.json"
            value = event_document()
            value["client_payload"]["source_tap_commit"] = "9" * 40
            write_pretty(event, value)
            with self.assertRaises(CONTROLLER.ControllerError):
                CONTROLLER.load_task_request(event)

            value = event_document(
                arches=("wasm64",),
            )
            write_pretty(event, value)
            self.assertEqual(
                CONTROLLER.load_task_request(event).arches,
                ("wasm64",),
            )

            value = event_document(
                arches=("wasm32", "wasm64"),
            )
            write_pretty(event, value)
            with self.assertRaises(CONTROLLER.ControllerError):
                CONTROLLER.load_task_request(event)

            value = event_document(
                dependencies=(
                    ("dependency", DEPENDENCY_TAG),
                    ("dependency", DEPENDENCY_TAG),
                ),
            )
            write_pretty(event, value)
            with self.assertRaises(CONTROLLER.ControllerError):
                CONTROLLER.load_task_request(event)

    def test_campaign_binds_bytes_authority_and_dependency_closure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            plan = fixture.plan()
            self.assertEqual(plan.disposition, "build")
            self.assertEqual(plan.generation_kind, "rootfs-wasm32")
            self.assertEqual(
                CONTROLLER.dependency_order(plan),
                ("dependency",),
            )

            event = json.loads(fixture.event.read_text())
            event["client_payload"]["dependency_handoffs"] = []
            write_pretty(fixture.event, event)
            with self.assertRaises(CONTROLLER.ControllerError) as raised:
                fixture.plan()
            self.assertEqual(
                raised.exception.status,
                "invalid-task-selection",
            )

            fixture.campaign.write_bytes(
                fixture.campaign.read_bytes() + b" "
            )
            with self.assertRaises(CONTROLLER.ControllerError):
                fixture.plan()

    def test_preflight_exports_only_fixed_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            github_output = fixture.root / "github-output"
            github_output.touch()
            CONTROLLER.preflight(
                fixture.authority,
                fixture.event,
                github_output,
            )
            values = dict(
                line.split("=", 1)
                for line in github_output.read_text().splitlines()
            )
            self.assertEqual(
                values,
                {
                    "browser-wasm32-generation":
                    BROWSER_WASM32_GENERATION,
                    "browser-wasm64-generation":
                    BROWSER_WASM64_GENERATION,
                    "campaign-tag":
                    CONTROLLER.load_authority(
                        fixture.authority,
                        require_active=True,
                    ).campaign_tag,
                    "kandelo-commit": KANDELO_COMMIT,
                    "release-tag": "bottles-abi-v42",
                    "rootfs-wasm32-generation":
                    ROOTFS_GENERATION,
                    "source-tap-commit": SOURCE_TAP_COMMIT,
                },
            )

    def test_admit_emits_canonical_build_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            output = fixture.root / "task.json"
            github_output = fixture.root / "github-output"
            github_output.touch()
            with (
                mock.patch.object(
                    CONTROLLER,
                    "require_exact_checkout",
                    side_effect=lambda root, *_args: root.resolve(),
                ),
                mock.patch.object(
                    CONTROLLER,
                    "require_target_source_checkout",
                ),
                mock.patch.object(
                    CONTROLLER,
                    "fetch_campaign",
                    return_value=fixture.campaign,
                ),
            ):
                document = CONTROLLER.admit(
                    authority_path=fixture.authority,
                    kandelo_root=fixture.kandelo,
                    source_tap_root=fixture.source_tap,
                    event_path=fixture.event,
                    output=output,
                    github_output=github_output,
                )
            self.assertEqual(document["disposition"], "build")
            self.assertEqual(
                document["target_source"],
                active_authority(
                    fixture.campaign.read_bytes()
                )["target_source"],
            )
            self.assertEqual(output.read_bytes(), CONTROLLER.pretty_json(document))
            values = dict(
                line.split("=", 1)
                for line in github_output.read_text().splitlines()
            )
            self.assertEqual(values["formula"], "leaf")
            self.assertEqual(values["arch"], "wasm32")
            self.assertEqual(values["arches"], "wasm32")
            self.assertEqual(
                values["dependencies"],
                '{"dependencies":[{"formula":"dependency",'
                f'"tag":"{DEPENDENCY_TAG}"}}],"schema":1}}',
            )
            self.assertEqual(
                values["generation-wasm32"],
                ROOTFS_GENERATION,
            )
            self.assertEqual(values["generation-wasm64"], "")

    def test_reuse_api_gap_fails_before_task_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(
                pathlib.Path(directory),
                disposition="byte-clean-reuse-candidate",
            )
            output = fixture.root / "task.json"
            with (
                mock.patch.object(
                    CONTROLLER,
                    "require_exact_checkout",
                    side_effect=lambda root, *_args: root.resolve(),
                ),
                mock.patch.object(
                    CONTROLLER,
                    "require_target_source_checkout",
                ),
                mock.patch.object(
                    CONTROLLER,
                    "fetch_campaign",
                    return_value=fixture.campaign,
                ),
                self.assertRaises(
                    CONTROLLER.ControllerError
                ) as raised,
            ):
                CONTROLLER.admit(
                    authority_path=fixture.authority,
                    kandelo_root=fixture.kandelo,
                    source_tap_root=fixture.source_tap,
                    event_path=fixture.event,
                    output=output,
                    github_output=None,
                )
            self.assertEqual(
                raised.exception.status,
                "reuse-admission-api-unavailable",
            )
            self.assertEqual(
                raised.exception.exit_code,
                CONTROLLER.UNAVAILABLE_EXIT,
            )
            self.assertFalse(output.exists())

    def test_campaign_fetch_uses_frozen_executor_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            authority = CONTROLLER.load_authority(
                fixture.authority,
                require_active=True,
            )
            output = fixture.root / "fetch"
            with mock.patch.object(
                CONTROLLER,
                "run_command",
            ) as run:
                campaign = CONTROLLER.fetch_campaign(
                    authority,
                    fixture.kandelo,
                    output,
                )
            self.assertEqual(campaign, output / "campaign.json")
            command = run.call_args.args[0]
            self.assertEqual(
                command[:3],
                [
                    "python3",
                    str(
                        fixture.kandelo
                        / "scripts/"
                        "homebrew-prefix-campaign-executor.py"
                    ),
                    "fetch-campaign-release",
                ],
            )
            self.assertEqual(
                command[3:],
                [
                    "--repository",
                    "kandelo-dev/homebrew-tap-core",
                    "--tag",
                    authority.campaign_tag,
                    "--out",
                    str(output / "campaign.json"),
                    "--receipt-out",
                    str(output / "campaign-receipt.json"),
                ],
            )

    def test_target_source_uses_the_frozen_overlay_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            source_authority = (
                fixture.source_tap
                / "Kandelo/prefix-campaign-authority.json"
            )
            source_authority.parent.mkdir(parents=True)
            source_authority.write_bytes(fixture.authority.read_bytes())
            authority = CONTROLLER.load_authority(
                fixture.authority,
                require_active=True,
            )
            with mock.patch.object(
                CONTROLLER,
                "run_command",
            ) as run:
                result = CONTROLLER.require_target_source_checkout(
                    authority,
                    fixture.source_tap,
                )
            self.assertEqual(
                result,
                fixture.source_tap
                / "Kandelo/campaigns/prefix-v1/source",
            )
            command = run.call_args.args[0]
            self.assertEqual(
                command[:3],
                [
                    "python3",
                    str(
                        fixture.source_tap
                        / "scripts/prefix-campaign-source.py"
                    ),
                    "verify",
                ],
            )
            self.assertIn(
                str(
                    fixture.source_tap
                    / "Kandelo/campaigns/prefix-v1/manifest.json"
                ),
                command,
            )

    def test_build_release_uses_publications_and_dependency_handoffs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            authority = CONTROLLER.load_authority(
                fixture.authority,
                require_active=True,
            )
            plan = fixture.plan()
            publications = fixture.root / "publications"
            (publications / "wasm32").mkdir(parents=True)
            dependency = fixture.root / "dependency-handoff"
            dependency.mkdir()
            output = fixture.root / "prepared"
            github_output = fixture.root / "github-output"
            github_output.touch()
            commands: list[list[str]] = []

            def command_side_effect(
                arguments: list[str],
                **_kwargs: object,
            ) -> mock.Mock:
                commands.append(arguments)
                if "prepare-release" in arguments:
                    prepared = pathlib.Path(
                        arguments[arguments.index("--out") + 1]
                    )
                    (prepared / "assets").mkdir(parents=True)
                    write_pretty(
                        prepared / "release-manifest.json",
                        {
                            "repository":
                            "kandelo-dev/homebrew-tap-core",
                            "tag":
                            "homebrew-prefix-handoff-sha256-"
                            + "7" * 64,
                            "target_commitish": SOURCE_TAP_COMMIT,
                        },
                    )
                return mock.Mock(returncode=0)

            with (
                mock.patch.object(
                    CONTROLLER,
                    "prepare_task",
                    return_value=(authority, plan),
                ),
                mock.patch.object(
                    CONTROLLER,
                    "materialize_target_source",
                    return_value=fixture.source_tap,
                ),
                mock.patch.object(
                    CONTROLLER,
                    "fetch_dependency_handoffs",
                    return_value={"dependency": dependency},
                ),
                mock.patch.object(
                    CONTROLLER,
                    "run_command",
                    side_effect=command_side_effect,
                ),
            ):
                summary = CONTROLLER.prepare_build_release(
                    authority_path=fixture.authority,
                    kandelo_root=fixture.kandelo,
                    source_tap_root=fixture.source_tap,
                    event_path=fixture.event,
                    publications_root=publications,
                    output=output,
                    github_output=github_output,
                )

            self.assertEqual(len(commands), 2)
            derive = commands[0]
            self.assertEqual(derive[2], "derive-build")
            self.assertEqual(
                derive[
                    derive.index("--source-tap-root") + 1
                ],
                str(fixture.source_tap),
            )
            self.assertIn(
                f"wasm32={(publications / 'wasm32').resolve()}",
                derive,
            )
            self.assertEqual(
                derive[
                    derive.index("--dependency-handoff") + 1
                ],
                str(dependency),
            )
            self.assertEqual(commands[1][2], "prepare-release")
            self.assertEqual(
                summary["release_tag"],
                "homebrew-prefix-handoff-sha256-" + "7" * 64,
            )
            self.assertTrue(output.is_dir())
            self.assertTrue(
                (fixture.root / "controller-summary.json").is_file()
            )
            self.assertEqual(
                github_output.read_text().strip(),
                "handoff-tag="
                "homebrew-prefix-handoff-sha256-"
                + "7" * 64,
            )

    def test_dependency_fetch_uses_frozen_anonymous_release_cli(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            plan = fixture.plan()
            with mock.patch.object(
                CONTROLLER,
                "run_command",
            ) as run:
                roots = CONTROLLER.fetch_dependency_handoffs(
                    kandelo_root=fixture.kandelo,
                    plan=plan,
                    root=fixture.root / "dependencies",
                )
            output = (
                fixture.root
                / "dependencies/handoffs/dependency"
            )
            self.assertEqual(roots, {"dependency": output})
            command = run.call_args.args[0]
            self.assertEqual(
                command,
                [
                    "python3",
                    str(
                        fixture.kandelo
                        / "scripts/"
                        "homebrew-prefix-campaign-executor.py"
                    ),
                    "fetch-release",
                    "--campaign",
                    str(fixture.campaign),
                    "--tag",
                    DEPENDENCY_TAG,
                    "--out",
                    str(output),
                    "--receipt-out",
                    str(
                        fixture.root
                        / "dependencies/receipts/dependency.json"
                    ),
                ],
            )

    def test_release_readback_is_anonymous_and_retains_receipt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            authority = CONTROLLER.load_authority(
                fixture.authority,
                require_active=True,
            )
            plan = fixture.plan()
            dependency = fixture.root / "dependency-handoff"
            dependency.mkdir()
            output = fixture.root / "readback"
            handoff = handoff_document(authority, plan)
            tag = tag_for_handoff(handoff)
            commands: list[list[str]] = []

            def command_side_effect(
                arguments: list[str],
                **_kwargs: object,
            ) -> mock.Mock:
                commands.append(arguments)
                fetched = pathlib.Path(
                    arguments[arguments.index("--out") + 1]
                )
                fetched.mkdir()
                write_pretty(
                    fetched / "handoff.json",
                    handoff,
                )
                receipt = pathlib.Path(
                    arguments[
                        arguments.index("--receipt-out") + 1
                    ]
                )
                write_pretty(receipt, {"status": "success"})
                return mock.Mock(returncode=0)

            with (
                mock.patch.object(
                    CONTROLLER,
                    "prepare_task",
                    return_value=(authority, plan),
                ),
                mock.patch.object(
                    CONTROLLER,
                    "fetch_dependency_handoffs",
                    return_value={"dependency": dependency},
                ),
                mock.patch.object(
                    CONTROLLER,
                    "run_command",
                    side_effect=command_side_effect,
                ),
            ):
                summary = CONTROLLER.verify_published_release(
                    authority_path=fixture.authority,
                    kandelo_root=fixture.kandelo,
                    source_tap_root=fixture.source_tap,
                    event_path=fixture.event,
                    tag=tag,
                    output=output,
                )

            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][2], "fetch-release")
            self.assertIn(tag, commands[0])
            self.assertEqual(
                commands[0][
                    commands[0].index("--dependency-handoff") + 1
                ],
                str(dependency),
            )
            self.assertEqual(summary["status"], "verified")
            self.assertTrue(
                (fixture.root / "readback-receipt.json").is_file()
            )
            self.assertEqual(
                json.loads(
                    (
                        fixture.root
                        / "readback-summary.json"
                    ).read_text()
                ),
                summary,
            )

    def test_readback_requires_the_exact_formula_handoff_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            authority = CONTROLLER.load_authority(
                fixture.authority,
                require_active=True,
            )
            plan = fixture.plan()
            valid = handoff_document(authority, plan)
            handoff_path = fixture.root / "handoff.json"
            write_pretty(handoff_path, valid)
            tag = tag_for_handoff(valid)
            self.assertEqual(
                CONTROLLER.validate_readback_handoff(
                    handoff_path,
                    authority=authority,
                    plan=plan,
                    tag=tag,
                ),
                valid,
            )

            mutations: list[tuple[str, dict[str, object]]] = []
            for label in (
                "extra top-level field",
                "legacy kind",
                "different campaign",
                "different Formula",
                "different dependency",
                "different source",
                "different architecture",
                "noncanonical payload path",
                "noncanonical asset name",
                "malformed payload SHA-256",
                "invalid byte count",
                "boolean byte count",
                "oversized file",
                "oversized aggregate",
                "reordered files",
            ):
                value = json.loads(
                    json.dumps(valid)
                )
                if label == "extra top-level field":
                    value["unexpected"] = True
                elif label == "legacy kind":
                    value["kind"] = "build"
                elif label == "different campaign":
                    value["campaign"]["sha256"] = "0" * 64
                elif label == "different Formula":
                    value["formula"]["name"] = "other"
                elif label == "different dependency":
                    value["dependency_handoffs"][0][
                        "manifest_sha256"
                    ] = "0" * 64
                elif label == "different source":
                    value["source"]["kandelo_commit"] = "0" * 40
                elif label == "different architecture":
                    value["publications"][0]["arch"] = "wasm64"
                elif label == "noncanonical payload path":
                    value["publications"][0]["files"][0][
                        "path"
                    ] = "payload/wasm32/other"
                elif label == "noncanonical asset name":
                    value["publications"][0]["files"][0][
                        "asset_name"
                    ] = "wasm32.other"
                elif label == "malformed payload SHA-256":
                    value["publications"][0]["files"][0][
                        "sha256"
                    ] = "not-a-sha256"
                elif label == "invalid byte count":
                    value["publications"][0]["files"][0][
                        "bytes"
                    ] = 0
                elif label == "boolean byte count":
                    value["publications"][0]["files"][0][
                        "bytes"
                    ] = True
                elif label == "oversized file":
                    value["publications"][0]["files"][0][
                        "bytes"
                    ] = CONTROLLER.MAX_HANDOFF_ASSET_BYTES + 1
                elif label == "oversized aggregate":
                    for record in value["publications"][0][
                        "files"
                    ]:
                        record["bytes"] = (
                            CONTROLLER.MAX_HANDOFF_ASSET_BYTES
                        )
                elif label == "reordered files":
                    files = value["publications"][0]["files"]
                    files[0], files[1] = files[1], files[0]
                mutations.append((label, value))

            for label, value in mutations:
                with self.subTest(label=label):
                    write_pretty(handoff_path, value)
                    with self.assertRaises(
                        CONTROLLER.ControllerError
                    ):
                        CONTROLLER.validate_readback_handoff(
                            handoff_path,
                            authority=authority,
                            plan=plan,
                            tag=tag_for_handoff(value),
                        )

            write_pretty(handoff_path, valid)
            with self.assertRaises(CONTROLLER.ControllerError):
                CONTROLLER.validate_readback_handoff(
                    handoff_path,
                    authority=authority,
                    plan=plan,
                    tag=(
                        "homebrew-prefix-handoff-sha256-"
                        + "0" * 64
                    ),
                )

    def test_dual_arch_formula_selects_one_sibling_and_generation_lane(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(
                pathlib.Path(directory),
                arches=("wasm32", "wasm64"),
                request_arch="wasm32",
            )
            authority = CONTROLLER.load_authority(
                fixture.authority,
                require_active=True,
            )
            plan = fixture.plan()
            self.assertEqual(plan.request.arches, ("wasm32",))
            self.assertEqual(plan.generation_kind, "browser-inputs")
            handoff = handoff_document(authority, plan)
            path = fixture.root / "handoff.json"
            write_pretty(path, handoff)
            validated = CONTROLLER.validate_readback_handoff(
                path,
                authority=authority,
                plan=plan,
                tag=tag_for_handoff(handoff),
            )
            self.assertEqual(
                [
                    publication["arch"]
                    for publication in validated["publications"]
                ],
                ["wasm32"],
            )

            event = json.loads(fixture.event.read_text())
            event["client_payload"]["arches"] = ["wasm64"]
            write_pretty(fixture.event, event)
            wasm64_plan = fixture.plan()
            self.assertEqual(wasm64_plan.request.arches, ("wasm64",))
            self.assertEqual(
                wasm64_plan.generation_kind,
                "browser-inputs",
            )

    def test_sibling_dispositions_are_independent_but_all_validated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(
                pathlib.Path(directory),
                arches=("wasm32", "wasm64"),
                request_arch="wasm32",
            )
            campaign = json.loads(fixture.campaign.read_text())
            selected = next(
                formula
                for formula in campaign["formulae"]
                if formula["name"] == "leaf"
            )
            selected["variants"][0]["disposition"]["kind"] = (
                "byte-clean-reuse-candidate"
            )
            payload = write_pretty(fixture.campaign, campaign)
            write_pretty(fixture.authority, active_authority(payload))
            self.assertEqual(fixture.plan().disposition, "reuse")

            event = json.loads(fixture.event.read_text())
            event["client_payload"]["arches"] = ["wasm64"]
            write_pretty(fixture.event, event)
            self.assertEqual(fixture.plan().disposition, "build")

            selected["variants"][0]["disposition"]["kind"] = (
                "required-build"
            )
            selected["variants"][1]["disposition"]["kind"] = "unknown"
            payload = write_pretty(fixture.campaign, campaign)
            write_pretty(fixture.authority, active_authority(payload))
            event["client_payload"]["arches"] = ["wasm32"]
            write_pretty(fixture.event, event)
            with self.assertRaises(CONTROLLER.ControllerError) as raised:
                fixture.plan()
            self.assertEqual(raised.exception.status, "invalid-campaign")

    def test_campaign_rejects_an_undeclared_requested_architecture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(
                pathlib.Path(directory),
                arches=("wasm32",),
                request_arch="wasm64",
            )
            with self.assertRaises(CONTROLLER.ControllerError) as raised:
                fixture.plan()
            self.assertEqual(
                raised.exception.status,
                "invalid-task-selection",
            )

    def test_anonymous_environment_removes_all_token_authority(
        self,
    ) -> None:
        with mock.patch.dict(
            os.environ,
            {name: "secret" for name in CONTROLLER.TOKEN_ENV},
        ):
            environment = CONTROLLER.anonymous_environment()
        for name in CONTROLLER.TOKEN_ENV:
            self.assertNotIn(name, environment)
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    def test_publication_root_rejects_extra_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "wasm32").mkdir()
            (root / "diagnostics").mkdir()
            with self.assertRaises(CONTROLLER.ControllerError):
                CONTROLLER.require_publication_roots(
                    root,
                    ("wasm32",),
                )


if __name__ == "__main__":
    unittest.main()
