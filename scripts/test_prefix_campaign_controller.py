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


def variant(arch: str, kind: str) -> dict[str, object]:
    return {
        "arch": arch,
        "disposition": {"kind": kind},
    }


def campaign_document(
    *,
    formula: str = "leaf",
    disposition: str = "required-build",
    arches: tuple[str, ...] = ("wasm32",),
) -> dict[str, object]:
    selected = {
        "dependencies": [
            {"full_name": "kandelo-dev/tap-core/dependency"}
        ],
        "name": formula,
        "variants": [
            variant(arch, disposition)
            for arch in arches
        ],
    }
    formulae = [
        {
            "dependencies": [],
            "name": "dependency",
            "variants": [
                variant("wasm32", "required-build"),
            ],
        },
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


class Fixture:
    def __init__(
        self,
        root: pathlib.Path,
        *,
        disposition: str = "required-build",
        arches: tuple[str, ...] = ("wasm32",),
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
            event_document(arches=arches),
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
    def test_checked_in_authority_is_explicitly_inert(self) -> None:
        with self.assertRaises(CONTROLLER.ControllerError) as raised:
            CONTROLLER.load_authority(
                AUTHORITY,
                require_active=True,
            )
        self.assertEqual(
            raised.exception.status,
            "campaign-authority-inert",
        )
        self.assertEqual(
            raised.exception.exit_code,
            CONTROLLER.INERT_EXIT,
        )

    def test_cli_reports_inert_before_external_checkout(self) -> None:
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
        self.assertIn(
            "status=campaign-authority-inert",
            result.stderr,
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
            self.assertEqual(output.read_bytes(), CONTROLLER.pretty_json(document))
            values = dict(
                line.split("=", 1)
                for line in github_output.read_text().splitlines()
            )
            self.assertEqual(values["formula"], "leaf")
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
            tag = "homebrew-prefix-handoff-sha256-" + "8" * 64
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
                    fetched / "manifest.json",
                    {
                        "formula": {"name": "leaf"},
                        "handoff_kind": "build",
                    },
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
