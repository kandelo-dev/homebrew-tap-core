#!/usr/bin/env python3
"""Exercise the protected prefix-campaign controller boundary."""

from __future__ import annotations

import copy
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
ABANDONED_CAMPAIGN = (
    ROOT
    / "Kandelo/campaigns/prefix-v1/aborted-campaigns/"
    "ab0290a46e54e67efbb0da2ac0e4b1f7176236e7877012b4872498798ce293ad.json"
)
ABANDONED_C2 = (
    ROOT
    / "Kandelo/campaigns/prefix-v1/aborted-campaigns/"
    "ced456302ccb3cc7198f20ac43afb2d54ba2b990afa7b02f86e48670be12f9b8.json"
)
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
OLD_TAP_COMMIT = "c" * 40
DEPENDENCY_TAG = "homebrew-prefix-handoff-sha256-" + "3" * 64
ROOTFS_GENERATION = (
    "package-generation-rootfs-wasm32-abi-v42-sha256-" + "4" * 64
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
    admission_kind: str = "anonymous-absence",
) -> dict[str, object]:
    probe_status = (
        "auth-required"
        if admission_kind
        == "first-package-namespace-bootstrap-required"
        else "missing"
    )
    variants = [variant(arch, disposition) for arch in arches]
    if admission_kind == "first-package-namespace-bootstrap-required":
        variants = [
            {
                **value,
                "build_input": {"kind": "formula-source"},
                "disposition": {
                    "kind": "required-build",
                    "reasons": ["new-campaign-entrant"],
                },
                "selected_by": "reviewed-campaign-input",
            }
            for value in variants
        ]
    return {
        "dependencies": dependencies,
        "destination": {
            "admission": {
                "kind": admission_kind,
                "method": "anonymous-oras-manifest-probe",
                "probe": {
                    "digest": None,
                    "kind": "manifest",
                    "schema": 1,
                    "status": probe_status,
                },
                "schema": 1,
            },
            "bottle_rebuild": 1,
            "reference": "2.0-rebuild1",
            "remote": f"ghcr.io/kandelo-dev/homebrew-tap-core/{name}",
        },
        "formula_source": {
            "sha256": formula_sha256,
        },
        "name": name,
        **(
            {"source_kind": "reviewed-new-entrant"}
            if admission_kind
            == "first-package-namespace-bootstrap-required"
            else {}
        ),
        "variants": variants,
        "version": version,
    }


def campaign_document(
    *,
    formula: str = "leaf",
    disposition: str = "required-build",
    arches: tuple[str, ...] = ("wasm32",),
    admission_kind: str = "anonymous-absence",
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
        admission_kind,
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
        "kind": "kandelo-homebrew-guest-prefix-campaign",
        "schema": 2,
        "authority": {
            "kandelo_commit": KANDELO_COMMIT,
            "old_tap_commit": OLD_TAP_COMMIT,
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


def write_reuse_oci_shape(root: pathlib.Path) -> None:
    sha_root = root / "layout/blobs/sha256"
    sha_root.mkdir(parents=True)
    (root / "receipt.json").write_text("{}\n")
    (root / "layout/oci-layout").write_text("{}\n")
    (root / "layout/index.json").write_text("{}\n")
    for character in "abc":
        (sha_root / (character * 64)).write_text(character)


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
            "rootfs_wasm32": ROOTFS_GENERATION,
        },
        "release_tag": "bottles-abi-v42",
        "reusable_workflow_commit": KANDELO_COMMIT,
        "schema": 2,
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


def armed_authority() -> dict[str, object]:
    authority = active_authority(b"unused while armed")
    authority["state"] = "armed"
    authority["source_tap_commit"] = "0" * 40
    authority["campaign_release"]["tag"] = (
        "homebrew-prefix-campaign-sha256-" + "0" * 64
    )
    authority["package_generations"]["rootfs_wasm32"] = (
        "package-generation-rootfs-wasm32-abi-v42-sha256-"
        + "0" * 64
    )
    return authority


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
    expected_files = (
        CONTROLLER.BUILD_HANDOFF_PUBLICATION_FILES
        if plan.disposition == "build"
        else CONTROLLER.REUSE_HANDOFF_PUBLICATION_FILES
    )
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
                expected_files
            )
        ]
        publications.append(
            {
                "arch": arch,
                "files": files,
                "kind": plan.disposition,
            }
        )
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
        "schema": 2,
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
        admission_kind: str = "anonymous-absence",
    ) -> None:
        self.root = root
        self.campaign = root / "campaign.json"
        campaign = campaign_document(
            disposition=disposition,
            arches=arches,
            admission_kind=admission_kind,
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
    def test_abandoned_campaign_is_auditable_but_not_admissible(
        self,
    ) -> None:
        record, _payload = CONTROLLER.load_json_bytes(
            ABANDONED_CAMPAIGN,
            "abandoned campaign record",
            canonical=True,
        )
        self.assertEqual(record["schema"], 1)
        self.assertEqual(
            record["kind"],
            "kandelo-homebrew-prefix-abandoned-campaign",
        )
        campaign_tag = record["authority"]["campaign_release"]["tag"]
        campaign_sha256 = campaign_tag.removeprefix(
            "homebrew-prefix-campaign-sha256-"
        )
        self.assertEqual(campaign_sha256, ABANDONED_CAMPAIGN.stem)
        self.assertEqual(
            len({item["run_id"] for item in record["dispatches"]}),
            len(record["dispatches"]),
        )
        self.assertEqual(
            {
                (item["formula"], item["run_id"])
                for item in record["dispatches"]
            },
            {
                ("bzip2", 30780592137),
                ("zlib", 30780595122),
                ("libcxx", 30780597862),
                ("openssl", 30780600596),
                ("libyaml", 30780603037),
                ("homebrew-bootstrap", 30780606060),
                ("coreutils", 30780609955),
                ("posix-utils-lite", 30780612168),
            },
        )
        self.assertEqual(
            {
                item["handoff_release"]["id"]
                for item in record["dispatches"]
                if "handoff_release" in item
            },
            {363965880, 363965882, 363965943, 363966055, 363966067},
        )

        live_value, _live_payload = CONTROLLER.load_json_bytes(
            AUTHORITY,
            "checked-in campaign authority",
            canonical=True,
        )
        # WHY: the abandoned C1 record must remain reproducible whether the
        # current caller is between campaigns or has activated a successor.
        # Normalize only the live campaign slots before reconstructing C1.
        armed_value = copy.deepcopy(live_value)
        armed_value["campaign_release"]["tag"] = (
            "homebrew-prefix-campaign-sha256-" + "0" * 64
        )
        armed_value["package_generations"]["rootfs_wasm32"] = (
            "package-generation-rootfs-wasm32-abi-v42-sha256-"
            + "0" * 64
        )
        armed_value["source_tap_commit"] = "0" * 40
        armed_value["state"] = "armed"
        self.assertEqual(armed_value["state"], "armed")
        self.assertEqual(
            armed_value["campaign_release"]["tag"],
            "homebrew-prefix-campaign-sha256-" + "0" * 64,
        )
        active_value = copy.deepcopy(armed_value)
        active_value["kandelo_commit"] = record["authority"][
            "kandelo_commit"
        ]
        active_value["reusable_workflow_commit"] = record["authority"][
            "kandelo_commit"
        ]
        active_value["campaign_release"] = record["authority"][
            "campaign_release"
        ].copy()
        active_value["campaign_release"].pop("id")
        active_value["package_generations"]["rootfs_wasm32"] = record[
            "authority"
        ]["rootfs_wasm32"]
        active_value["source_tap_commit"] = record["authority"][
            "source_tap_commit"
        ]
        active_value["state"] = "active"
        active_value["target_source"] = record["authority"][
            "target_source"
        ]
        self.assertEqual(
            hashlib.sha256(
                CONTROLLER.pretty_json(active_value)
            ).hexdigest(),
            record["authority"]["payload_sha256"],
        )

        with tempfile.TemporaryDirectory() as directory:
            abandoned_authority = (
                pathlib.Path(directory) / "authority.json"
            )
            write_pretty(abandoned_authority, armed_value)
            abandoned_event = pathlib.Path(directory) / "event.json"
            write_pretty(
                abandoned_event,
                {
                    "action": CONTROLLER.EVENT_TYPE,
                    "client_payload": {
                        "arches": ["wasm32"],
                        "dependency_handoffs": [],
                        "formula": "bzip2",
                    },
                },
            )
            with self.assertRaises(
                CONTROLLER.ControllerError
            ) as raised:
                CONTROLLER.preflight(
                    abandoned_authority,
                    abandoned_event,
                    None,
                )
            self.assertEqual(
                raised.exception.status,
                "campaign-authority-inert",
            )

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            authority = CONTROLLER.load_authority(
                fixture.authority,
                require_active=True,
            )
            plan = fixture.plan()
            handoff = handoff_document(authority, plan)
            handoff["campaign"]["sha256"] = campaign_sha256
            handoff_path = fixture.root / "abandoned-handoff.json"
            write_pretty(handoff_path, handoff)
            # WHY: public handoff evidence remains truthful after an abort,
            # but a later campaign must explicitly rebind it after proving
            # unchanged inputs. It cannot pass as the successor handoff.
            with self.assertRaises(CONTROLLER.ControllerError):
                CONTROLLER.validate_readback_handoff(
                    handoff_path,
                    authority=authority,
                    plan=plan,
                    tag=tag_for_handoff(handoff),
                )

    def test_abandoned_c2_preserves_every_valid_dispatch(self) -> None:
        record, _payload = CONTROLLER.load_json_bytes(
            ABANDONED_C2,
            "abandoned C2 campaign record",
            canonical=True,
        )
        self.assertEqual(record["schema"], 1)
        self.assertEqual(
            record["kind"],
            "kandelo-homebrew-prefix-abandoned-campaign",
        )
        campaign_tag = record["authority"]["campaign_release"]["tag"]
        self.assertEqual(
            campaign_tag.removeprefix(
                "homebrew-prefix-campaign-sha256-"
            ),
            ABANDONED_C2.stem,
        )
        dispatches = record["dispatches"]
        run_ids = {item["run_id"] for item in dispatches}
        self.assertEqual(len(dispatches), 40)
        self.assertEqual(len(run_ids), 40)
        self.assertEqual(
            {item["run_id"] for item in record["invalid_events"]},
            {30783368233, 30783371138, 30783375101},
        )
        self.assertEqual(
            run_ids,
            {
                30782329550, 30782332723, 30782335537,
                30782338293, 30782340976, 30782342970,
                30782346529, 30782349527, 30782435588,
                30782439020, 30782473341, 30782477037,
                30782479871, 30782553770, 30782556280,
                30782574156, 30782627856, 30782631394,
                30782689328, 30782691852, 30782695219,
                30782844794, 30782847771, 30782850088,
                30783013274, 30783016134, 30783019135,
                30783174020, 30783176305, 30783179381,
                30783482573, 30783492949, 30783495916,
                30783517793, 30783521665, 30783737597,
                30783741445, 30783746354, 30783750879,
                30783754333,
            },
        )
        handoff_ids = {
            item["handoff_release"]["id"]
            for item in dispatches
            if "handoff_release" in item
        }
        self.assertEqual(len(handoff_ids), 31)
        self.assertEqual(
            record["recovery"]["reserved_successor_versions"],
            [
                {
                    "arch": "wasm32",
                    "formula": "homebrew-bootstrap",
                    "version": "6.0.12-153-gcf5bc21_1",
                },
                {
                    "arch": "wasm32",
                    "formula": "libyaml",
                    "version": "0.2.5_1",
                },
            ],
        )

        live_value, _live_payload = CONTROLLER.load_json_bytes(
            AUTHORITY,
            "checked-in campaign authority",
            canonical=True,
        )
        # WHY: the abandoned C2 record must remain reproducible after a
        # successor campaign activates. Reconstruct the inert template instead
        # of assuming that the checked-in caller remains armed forever.
        armed_value = copy.deepcopy(live_value)
        armed_value["campaign_release"]["tag"] = (
            "homebrew-prefix-campaign-sha256-" + "0" * 64
        )
        armed_value["package_generations"]["rootfs_wasm32"] = (
            "package-generation-rootfs-wasm32-abi-v42-sha256-"
            + "0" * 64
        )
        armed_value["source_tap_commit"] = "0" * 40
        armed_value["state"] = "armed"
        self.assertEqual(armed_value["state"], "armed")
        active_value = copy.deepcopy(armed_value)
        active_value["campaign_release"] = record["authority"][
            "campaign_release"
        ].copy()
        active_value["campaign_release"].pop("id")
        active_value["kandelo_commit"] = record["authority"][
            "kandelo_commit"
        ]
        active_value["package_generations"]["rootfs_wasm32"] = record[
            "authority"
        ]["rootfs_wasm32"]
        active_value["reusable_workflow_commit"] = record["authority"][
            "kandelo_commit"
        ]
        active_value["source_tap_commit"] = record["authority"][
            "source_tap_commit"
        ]
        active_value["state"] = "active"
        active_value["target_source"] = record["authority"][
            "target_source"
        ]
        self.assertEqual(
            hashlib.sha256(
                CONTROLLER.pretty_json(active_value)
            ).hexdigest(),
            record["authority"]["payload_sha256"],
        )

    def test_checked_in_authority_has_safe_rollout_state(self) -> None:
        authority = CONTROLLER.load_authority(
            AUTHORITY,
            require_active=False,
        )
        self.assertIn(authority.state, ("armed", "active"))
        if authority.state == "armed":
            self.assertEqual(
                authority.source_tap_commit,
                "0" * 40,
            )
            return

        # WHY: GitHub's GITHUB_TOKEN cannot create a release that targets a
        # historical commit with different workflow bytes. Activation may
        # fill campaign data, but it must preserve the armed source's exact
        # workflow tree until every handoff release has been sealed.
        self.assertEqual(
            CONTROLLER.git_output(
                ROOT,
                "rev-parse",
                "HEAD:.github/workflows",
            ),
            CONTROLLER.git_output(
                ROOT,
                "rev-parse",
                f"{authority.source_tap_commit}:.github/workflows",
            ),
        )

    def test_armed_authority_is_valid_but_cannot_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            authority_path = pathlib.Path(directory) / "authority.json"
            write_pretty(authority_path, armed_authority())
            authority = CONTROLLER.load_authority(
                authority_path,
                require_active=False,
            )
            self.assertEqual(authority.state, "armed")
            with self.assertRaises(CONTROLLER.ControllerError) as raised:
                CONTROLLER.load_authority(
                    authority_path,
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

            mixed = armed_authority()
            mixed["source_tap_commit"] = SOURCE_TAP_COMMIT
            write_pretty(authority_path, mixed)
            with self.assertRaises(CONTROLLER.ControllerError) as raised:
                CONTROLLER.load_authority(
                    authority_path,
                    require_active=False,
                )
            self.assertEqual(
                raised.exception.status,
                "invalid-contract",
            )

    def test_campaign_source_must_share_active_workflow_tree(self) -> None:
        with mock.patch.object(
            CONTROLLER,
            "git_output",
            side_effect=("1" * 40, "1" * 40),
        ):
            CONTROLLER.require_matching_workflow_tree(
                ROOT,
                ROOT,
            )
        with mock.patch.object(
            CONTROLLER,
            "git_output",
            side_effect=("1" * 40, "2" * 40),
        ):
            with self.assertRaises(CONTROLLER.ControllerError) as raised:
                CONTROLLER.require_matching_workflow_tree(
                    ROOT,
                    ROOT,
                )
        self.assertEqual(
            raised.exception.status,
            "invalid-checkout",
        )

    def test_cli_reports_inert_before_external_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            event = root / "event.json"
            write_pretty(event, event_document())
            authority_path = root / "authority.json"
            authority = json.loads(AUTHORITY.read_text())
            authority["state"] = "inert"
            authority["kandelo_commit"] = "0" * 40
            authority["reusable_workflow_commit"] = "0" * 40
            authority["source_tap_commit"] = "0" * 40
            authority["campaign_release"]["tag"] = (
                "homebrew-prefix-campaign-sha256-" + "0" * 64
            )
            authority["package_generations"]["rootfs_wasm32"] = (
                "package-generation-rootfs-wasm32-abi-v42-sha256-"
                + "0" * 64
            )
            write_pretty(authority_path, authority)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "preflight",
                    "--authority",
                    str(authority_path),
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
            raw_kandelo_root = pathlib.Path("kandelo")
            raw_source_tap_root = pathlib.Path("source-tap")

            def exact_checkout(
                root: pathlib.Path,
                *_args: object,
            ) -> pathlib.Path:
                if root == raw_kandelo_root:
                    return fixture.kandelo.resolve()
                if root == raw_source_tap_root:
                    return fixture.source_tap.resolve()
                raise AssertionError(f"unexpected checkout root: {root}")

            with (
                mock.patch.object(
                    CONTROLLER,
                    "require_exact_checkout",
                    side_effect=exact_checkout,
                ),
                mock.patch.object(
                    CONTROLLER,
                    "require_target_source_checkout",
                ) as target_source,
                mock.patch.object(
                    CONTROLLER,
                    "require_matching_workflow_tree",
                ) as matching_workflows,
                mock.patch.object(
                    CONTROLLER,
                    "fetch_campaign",
                    return_value=fixture.campaign,
                ) as fetch_campaign,
            ):
                document = CONTROLLER.admit(
                    authority_path=fixture.authority,
                    kandelo_root=raw_kandelo_root,
                    source_tap_root=raw_source_tap_root,
                    event_path=fixture.event,
                    output=output,
                    github_output=github_output,
                )
            self.assertTrue(
                fetch_campaign.call_args.kwargs["authenticated"]
            )
            self.assertEqual(
                fetch_campaign.call_args.args[1],
                fixture.kandelo.resolve(),
            )
            matching_workflows.assert_called_once_with(
                ROOT,
                fixture.source_tap.resolve(),
            )
            target_source.assert_called_once_with(
                CONTROLLER.load_authority(
                    fixture.authority,
                    require_active=True,
                ),
                fixture.source_tap.resolve(),
            )
            self.assertEqual(document["disposition"], "build")
            self.assertEqual(
                document["admission"],
                {"kind": "anonymous-absence", "schema": 1},
            )
            self.assertEqual(document["schema"], 2)
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
            self.assertEqual(
                values["admission-kind"],
                "anonymous-absence",
            )
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
            self.assertEqual(
                values["old-tap-commit"],
                OLD_TAP_COMMIT,
            )

    def test_reuse_is_admitted_without_a_package_generation(self) -> None:
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
                    "require_matching_workflow_tree",
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
                    github_output=None,
                )
            self.assertEqual(
                document["disposition"],
                "reuse",
            )
            self.assertEqual(
                document["generation_kind"],
                "none",
            )
            self.assertTrue(output.is_file())

    def test_first_package_namespace_bootstrap_routes_only_a_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(
                pathlib.Path(directory),
                admission_kind=(
                    "first-package-namespace-bootstrap-required"
                ),
            )
            plan = fixture.plan()
            self.assertEqual(plan.disposition, "build")
            self.assertEqual(
                plan.admission_kind,
                "first-package-namespace-bootstrap-required",
            )

            campaign = json.loads(fixture.campaign.read_text())
            selected = next(
                value
                for value in campaign["formulae"]
                if value["name"] == "leaf"
            )
            selected["destination"]["admission"]["probe"][
                "status"
            ] = "missing"
            payload = write_pretty(fixture.campaign, campaign)
            write_pretty(fixture.authority, active_authority(payload))
            with self.assertRaises(
                CONTROLLER.ControllerError
            ) as raised:
                fixture.plan()
            self.assertEqual(
                raised.exception.status,
                "invalid-campaign",
            )

            selected["destination"]["admission"]["probe"][
                "status"
            ] = "auth-required"
            selected["variants"][0]["disposition"] = {
                "kind": "byte-clean-reuse-candidate"
            }
            payload = write_pretty(fixture.campaign, campaign)
            write_pretty(fixture.authority, active_authority(payload))
            with self.assertRaises(
                CONTROLLER.ControllerError
            ) as raised:
                fixture.plan()
            self.assertEqual(
                raised.exception.status,
                "invalid-campaign",
            )

    def test_destination_admission_requires_the_exact_contract(self) -> None:
        mutations = {
            "old campaign schema": lambda campaign, _formula: campaign.update(
                schema=1
            ),
            "wrong campaign kind": lambda campaign, _formula: campaign.update(
                kind="other"
            ),
            "extra destination field": lambda _campaign, formula: formula[
                "destination"
            ].update(unexpected=True),
            "extra admission field": lambda _campaign, formula: formula[
                "destination"
            ]["admission"].update(unexpected=True),
            "unknown admission kind": lambda _campaign, formula: formula[
                "destination"
            ]["admission"].update(kind="other"),
            "ordinary authentication ambiguity": (
                lambda _campaign, formula: formula["destination"][
                    "admission"
                ]["probe"].update(status="auth-required")
            ),
            "digest on missing probe": lambda _campaign, formula: formula[
                "destination"
            ]["admission"]["probe"].update(digest="sha256:" + "0" * 64),
        }
        for label, mutate in mutations.items():
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = Fixture(pathlib.Path(directory))
                campaign = json.loads(fixture.campaign.read_text())
                selected = next(
                    value
                    for value in campaign["formulae"]
                    if value["name"] == "leaf"
                )
                mutate(campaign, selected)
                payload = write_pretty(fixture.campaign, campaign)
                write_pretty(fixture.authority, active_authority(payload))
                with self.assertRaises(
                    CONTROLLER.ControllerError
                ) as raised:
                    fixture.plan()
                self.assertIn(
                    raised.exception.status,
                    ("invalid-campaign", "invalid-contract"),
                )

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
                    authenticated=True,
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
            self.assertTrue(
                run.call_args.kwargs["inherit_github_token"]
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
            raw_kandelo_root = pathlib.Path("kandelo")
            raw_source_tap_root = pathlib.Path("source-tap")

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
                    return_value=(
                        authority,
                        plan,
                        fixture.kandelo.resolve(),
                        fixture.source_tap.resolve(),
                    ),
                ) as prepare_task,
                mock.patch.object(
                    CONTROLLER,
                    "materialize_target_source",
                    return_value=fixture.source_tap,
                ) as materialize_source,
                mock.patch.object(
                    CONTROLLER,
                    "fetch_dependency_handoffs",
                    return_value={"dependency": dependency},
                ) as fetch_dependencies,
                mock.patch.object(
                    CONTROLLER,
                    "run_command",
                    side_effect=command_side_effect,
                ) as run,
            ):
                summary = CONTROLLER.prepare_build_release(
                    authority_path=fixture.authority,
                    kandelo_root=raw_kandelo_root,
                    source_tap_root=raw_source_tap_root,
                    event_path=fixture.event,
                    publications_root=publications,
                    output=output,
                    github_output=github_output,
                )

            self.assertTrue(
                prepare_task.call_args.kwargs[
                    "authenticated_release_reads"
                ]
            )
            self.assertTrue(
                fetch_dependencies.call_args.kwargs["authenticated"]
            )
            self.assertEqual(
                fetch_dependencies.call_args.kwargs["kandelo_root"],
                fixture.kandelo.resolve(),
            )
            self.assertEqual(
                materialize_source.call_args.args[1],
                fixture.source_tap.resolve(),
            )
            self.assertTrue(run.call_args_list)
            for call in run.call_args_list:
                self.assertFalse(
                    call.kwargs.get("inherit_github_token", False)
                )
                self.assertEqual(
                    call.kwargs["cwd"],
                    fixture.kandelo.resolve(),
                )
            self.assertEqual(len(commands), 2)
            derive = commands[0]
            self.assertEqual(
                derive[1],
                str(
                    fixture.kandelo.resolve()
                    / "scripts/homebrew-prefix-campaign-executor.py"
                ),
            )
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

    def test_dependency_fetch_uses_bounded_authenticated_release_cli(
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
                    authenticated=True,
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
            self.assertTrue(
                run.call_args.kwargs["inherit_github_token"]
            )

    def test_reuse_release_calls_the_frozen_kandelo_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(
                pathlib.Path(directory),
                disposition="byte-clean-reuse-candidate",
            )
            authority = CONTROLLER.load_authority(
                fixture.authority,
                require_active=True,
            )
            plan = fixture.plan()
            dependency = fixture.root / "dependency-handoff"
            dependency.mkdir()
            old_tap = fixture.root / "old-tap"
            old_tap.mkdir()
            output = fixture.root / "prepared"
            commands: list[list[str]] = []
            raw_kandelo_root = pathlib.Path("kandelo")
            raw_source_tap_root = pathlib.Path("source-tap")

            def command_side_effect(
                arguments: list[str],
                **_kwargs: object,
            ) -> mock.Mock:
                commands.append(arguments)
                if "compose-reuse-child" in arguments:
                    reuse_oci = pathlib.Path(
                        arguments[arguments.index("--out") + 1]
                    )
                    write_reuse_oci_shape(reuse_oci)
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
                    return_value=(
                        authority,
                        plan,
                        fixture.kandelo.resolve(),
                        fixture.source_tap.resolve(),
                    ),
                ) as prepare_task,
                mock.patch.object(
                    CONTROLLER,
                    "require_exact_checkout",
                    return_value=old_tap.resolve(),
                ) as exact_checkout,
                mock.patch.object(
                    CONTROLLER,
                    "materialize_target_source",
                    return_value=fixture.source_tap,
                ) as materialize_source,
                mock.patch.object(
                    CONTROLLER,
                    "fetch_dependency_handoffs",
                    return_value={"dependency": dependency},
                ) as fetch_dependencies,
                mock.patch.object(
                    CONTROLLER,
                    "run_command",
                    side_effect=command_side_effect,
                ) as run,
            ):
                summary = CONTROLLER.prepare_reuse_release(
                    authority_path=fixture.authority,
                    kandelo_root=raw_kandelo_root,
                    source_tap_root=raw_source_tap_root,
                    old_tap_root=old_tap,
                    event_path=fixture.event,
                    output=output,
                    github_output=None,
                )

            self.assertTrue(
                prepare_task.call_args.kwargs[
                    "authenticated_release_reads"
                ]
            )
            self.assertTrue(
                fetch_dependencies.call_args.kwargs["authenticated"]
            )
            self.assertEqual(
                fetch_dependencies.call_args.kwargs["kandelo_root"],
                fixture.kandelo.resolve(),
            )
            self.assertEqual(
                materialize_source.call_args.args[1],
                fixture.source_tap.resolve(),
            )
            self.assertTrue(run.call_args_list)
            for call in run.call_args_list:
                self.assertFalse(
                    call.kwargs.get("inherit_github_token", False)
                )
                self.assertEqual(
                    call.kwargs["cwd"],
                    fixture.kandelo.resolve(),
                )
            exact_checkout.assert_called_once_with(
                old_tap,
                OLD_TAP_COMMIT,
                "historical tap checkout",
            )
            self.assertEqual(len(commands), 4)
            derive = commands[0]
            self.assertEqual(
                derive[1],
                str(
                    fixture.kandelo.resolve()
                    / "scripts/homebrew-prefix-campaign-executor.py"
                ),
            )
            self.assertEqual(derive[2], "derive-reuse")
            self.assertEqual(
                derive[derive.index("--old-tap-root") + 1],
                str(old_tap.resolve()),
            )
            self.assertEqual(
                derive[derive.index("--arch") + 1],
                "wasm32",
            )
            compose = commands[1]
            self.assertEqual(compose[2], "compose-reuse-child")
            self.assertEqual(
                compose[compose.index("--handoff") + 1],
                derive[derive.index("--out") + 1],
            )
            self.assertEqual(
                compose[compose.index("--source-tap-root") + 1],
                str(fixture.source_tap),
            )
            self.assertEqual(
                compose[compose.index("--arch") + 1],
                "wasm32",
            )
            validate = commands[2]
            self.assertEqual(validate[2], "validate-child")
            compose_output = pathlib.Path(
                compose[compose.index("--out") + 1]
            )
            self.assertEqual(
                validate[validate.index("--layout") + 1],
                str(compose_output / "layout"),
            )
            self.assertEqual(
                validate[validate.index("--receipt") + 1],
                str(compose_output / "receipt.json"),
            )
            self.assertEqual(commands[3][2], "prepare-release")
            self.assertEqual(summary["disposition"], "reuse")
            self.assertEqual(summary["schema"], 2)
            self.assertTrue(output.is_dir())
            self.assertTrue((output / "reuse-oci/layout").is_dir())
            self.assertTrue((output / "reuse-oci/receipt.json").is_file())

    def test_reuse_oci_tree_rejects_extra_and_special_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "reuse-oci"
            write_reuse_oci_shape(root)
            self.assertEqual(
                CONTROLLER.require_reuse_oci_tree(root),
                root,
            )
            (root / "extra").write_text("not sealed\n")
            with self.assertRaisesRegex(
                CONTROLLER.ControllerError,
                "unexpected layout shape",
            ):
                CONTROLLER.require_reuse_oci_tree(root)
            (root / "extra").unlink()
            receipt = root / "receipt.json"
            receipt.unlink()
            receipt.symlink_to(root / "layout/index.json")
            with self.assertRaisesRegex(
                CONTROLLER.ControllerError,
                "unsafe file",
            ):
                CONTROLLER.require_reuse_oci_tree(root)

    def test_handoff_rejects_a_preexisting_reuse_oci_destination(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(
                pathlib.Path(directory),
                disposition="byte-clean-reuse-candidate",
            )
            authority = CONTROLLER.load_authority(
                fixture.authority,
                require_active=True,
            )
            working = fixture.root / "working"
            working.mkdir()
            handoff = working / "handoff"
            handoff.mkdir()
            reuse_oci = working / "reuse-oci"
            write_reuse_oci_shape(reuse_oci)
            output = fixture.root / "prepared"

            def prepare_with_collision(
                arguments: list[str],
                **_kwargs: object,
            ) -> mock.Mock:
                prepared = pathlib.Path(
                    arguments[arguments.index("--out") + 1]
                )
                (prepared / "assets").mkdir(parents=True)
                (prepared / "reuse-oci").mkdir()
                write_pretty(
                    prepared / "release-manifest.json",
                    {
                        "repository": "kandelo-dev/homebrew-tap-core",
                        "tag": (
                            "homebrew-prefix-handoff-sha256-" + "7" * 64
                        ),
                        "target_commitish": SOURCE_TAP_COMMIT,
                    },
                )
                return mock.Mock(returncode=0)

            with (
                mock.patch.object(
                    CONTROLLER,
                    "run_command",
                    side_effect=prepare_with_collision,
                ),
                self.assertRaisesRegex(
                    CONTROLLER.ControllerError,
                    "already contains a reuse OCI child",
                ),
            ):
                CONTROLLER.prepare_handoff_release(
                    authority=authority,
                    plan=fixture.plan(),
                    kandelo_root=fixture.kandelo,
                    dependencies={},
                    handoff=handoff,
                    working=working,
                    output=output,
                    github_output=None,
                    reuse_oci=reuse_oci,
                )
            self.assertFalse(output.exists())

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
            validated_kandelo_root = fixture.root / "validated-kandelo"
            validated_source_tap_root = (
                fixture.root / "validated-source-tap"
            )
            raw_kandelo_root = pathlib.Path("kandelo")
            raw_source_tap_root = pathlib.Path("source-tap")

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
                    return_value=(
                        authority,
                        plan,
                        validated_kandelo_root,
                        validated_source_tap_root,
                    ),
                ) as prepare_task,
                mock.patch.object(
                    CONTROLLER,
                    "fetch_dependency_handoffs",
                    return_value={"dependency": dependency},
                ) as fetch_dependencies,
                mock.patch.object(
                    CONTROLLER,
                    "run_command",
                    side_effect=command_side_effect,
                ) as run,
            ):
                summary = CONTROLLER.verify_published_release(
                    authority_path=fixture.authority,
                    kandelo_root=raw_kandelo_root,
                    source_tap_root=raw_source_tap_root,
                    event_path=fixture.event,
                    tag=tag,
                    output=output,
                )

            self.assertFalse(
                prepare_task.call_args.kwargs[
                    "authenticated_release_reads"
                ]
            )
            self.assertEqual(
                prepare_task.call_args.kwargs["kandelo_root"],
                raw_kandelo_root,
            )
            self.assertEqual(
                prepare_task.call_args.kwargs["source_tap_root"],
                raw_source_tap_root,
            )
            self.assertFalse(
                fetch_dependencies.call_args.kwargs["authenticated"]
            )
            self.assertEqual(
                fetch_dependencies.call_args.kwargs["kandelo_root"],
                validated_kandelo_root,
            )
            self.assertFalse(
                run.call_args.kwargs["inherit_github_token"]
            )
            self.assertEqual(
                run.call_args.kwargs["cwd"],
                validated_kandelo_root,
            )
            self.assertEqual(len(commands), 1)
            self.assertEqual(
                commands[0][1],
                str(
                    validated_kandelo_root
                    / "scripts/homebrew-prefix-campaign-executor.py"
                ),
            )
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
                "different publication kind",
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
                elif label == "different publication kind":
                    value["publications"][0]["kind"] = "reuse"
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

    def test_readback_accepts_only_the_exact_reuse_inventory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(
                pathlib.Path(directory),
                disposition="byte-clean-reuse-candidate",
            )
            authority = CONTROLLER.load_authority(
                fixture.authority,
                require_active=True,
            )
            plan = fixture.plan()
            handoff = handoff_document(authority, plan)
            handoff_path = fixture.root / "handoff.json"
            write_pretty(handoff_path, handoff)
            self.assertEqual(
                CONTROLLER.validate_readback_handoff(
                    handoff_path,
                    authority=authority,
                    plan=plan,
                    tag=tag_for_handoff(handoff),
                ),
                handoff,
            )

            handoff["publications"][0]["files"][0]["path"] = (
                "payload/wasm32/build/bottle.json"
            )
            write_pretty(handoff_path, handoff)
            with self.assertRaises(CONTROLLER.ControllerError):
                CONTROLLER.validate_readback_handoff(
                    handoff_path,
                    authority=authority,
                    plan=plan,
                    tag=tag_for_handoff(handoff),
                )

    def test_dual_arch_formula_selects_one_sibling_runtime_lane(
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
            self.assertEqual(plan.generation_kind, "rootfs-wasm32")
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
            with self.assertRaises(
                CONTROLLER.ControllerError
            ) as raised:
                fixture.plan()
            self.assertEqual(
                raised.exception.status,
                "package-generation-unavailable",
            )
            self.assertEqual(
                raised.exception.exit_code,
                CONTROLLER.UNAVAILABLE_EXIT,
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
            reuse_plan = fixture.plan()
            self.assertEqual(reuse_plan.disposition, "reuse")
            self.assertEqual(reuse_plan.generation_kind, "none")

            event = json.loads(fixture.event.read_text())
            event["client_payload"]["arches"] = ["wasm64"]
            write_pretty(fixture.event, event)
            with self.assertRaises(
                CONTROLLER.ControllerError
            ) as raised:
                fixture.plan()
            self.assertEqual(
                raised.exception.status,
                "package-generation-unavailable",
            )

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

    def test_internal_release_environment_forwards_only_gh_token(
        self,
    ) -> None:
        values = {
            name: f"secret-{index}"
            for index, name in enumerate(CONTROLLER.TOKEN_ENV)
        }
        with mock.patch.dict(os.environ, values):
            environment = CONTROLLER.internal_release_environment()
        self.assertEqual(environment["GH_TOKEN"], values["GH_TOKEN"])
        for name in CONTROLLER.TOKEN_ENV:
            if name != "GH_TOKEN":
                self.assertNotIn(name, environment)

    def test_internal_release_environment_requires_gh_token(
        self,
    ) -> None:
        environment = os.environ.copy()
        for name in CONTROLLER.TOKEN_ENV:
            environment.pop(name, None)
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            self.assertRaises(CONTROLLER.ControllerError) as raised,
        ):
            CONTROLLER.internal_release_environment()
        self.assertEqual(
            raised.exception.status,
            "credential-unavailable",
        )

    def test_failed_internal_release_read_does_not_log_token(
        self,
    ) -> None:
        token = "github-token-that-must-not-escape"
        command = [
            sys.executable,
            "-c",
            (
                "import os, sys; "
                "sys.stderr.write(os.environ['GH_TOKEN']); "
                "raise SystemExit(1)"
            ),
        ]
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {"GH_TOKEN": token}),
            self.assertRaises(CONTROLLER.ControllerError) as raised,
        ):
            CONTROLLER.run_command(
                command,
                cwd=pathlib.Path(directory),
                inherit_github_token=True,
            )
        self.assertNotIn(token, str(raised.exception))
        self.assertIn("<redacted>", str(raised.exception))

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
