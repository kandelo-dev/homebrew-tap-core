from __future__ import annotations

import copy
from dataclasses import asdict, replace
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.abi_staging import cli as cli_module
from scripts.abi_staging import promotion as promotion_module
from scripts.abi_staging.abi_history import (
    HISTORY_RECORD_MEDIA_TYPE,
    build_history_oci_plan,
    validate_protection_snapshot,
)
from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cli import main as cli_main
from scripts.abi_staging.contract import (
    load_bottle_contract,
    make_candidate_reuse_record,
)
from scripts.abi_staging.custody import source_capsule_digest
from scripts.abi_staging.oci import (
    FetchedOciRecordV1,
    OciPublicationError,
    build_oci_manifest,
    fetch_public_record,
    publish_immutable_oci_plan,
    publish_record,
)
from scripts.abi_staging.override import (
    OVERRIDE_RECEIPT_MEDIA_TYPE,
    accept_artifact_risk,
    build_override_receipt_oci_plan,
    load_guard_registry,
)
from scripts.abi_staging.plan import exact_formula_subject, validate_tap_plan
from scripts.abi_staging.policy import (
    load_tap_staging_policy,
    load_verification_tests,
)
from scripts.abi_staging.promotion import (
    ADMISSION_RECORD_MEDIA_TYPE,
    CANONICAL_BOTTLE_MANIFEST_MEDIA_TYPE,
    ProductEvidenceAuthorityV1,
    PromotionError,
    build_admission_oci_plan,
    build_canonical_bottle_plan,
    evaluate_promotion,
    expected_canonical_publication,
    finalize_admission_record,
    load_metadata_patch_document,
    metadata_patch_document,
    prepare_admission,
    publish_admission_record,
    publish_canonical_bottle,
    read_canonical_publication,
    validate_canonical_bottle_plan,
    validate_promotion_decision,
)
from scripts.abi_staging.records import (
    BOTTLE_CONTRACT_MEDIA_TYPE,
    BOTTLE_LAYER_MEDIA_TYPE,
    BOTTLE_METADATA_MEDIA_TYPE,
    CANDIDATE_RECORD_MEDIA_TYPE,
    CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
    SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
    VFS_COMPOSITION_DESCRIPTOR_MEDIA_TYPE,
    OciBlobV1,
    OciRecordPlanV1,
    build_candidate_reuse_oci_plan,
    validate_admission_record,
    validate_candidate_record,
)
from scripts.abi_staging.tap_metadata import TapMetadataPatchV1, load_promotion_policy
from scripts.abi_staging.reconcile import PullRequestLifecycleV1
from scripts.abi_staging.tests.test_oci import (
    FakeRegistryTransport,
    SOURCE_ASSOCIATION,
)
from scripts.abi_staging.tests.test_override import (
    MAINTAINER,
    RUN as OVERRIDE_RUN,
    _guard_registry,
    _request,
)
from scripts.abi_staging.tests.test_records import _write_custody
from scripts.abi_staging.verification import (
    VERIFICATION_RECEIPT_MEDIA_TYPE,
    receipt_repository,
    validate_verification_receipt_record,
)


TAP_ROOT = Path(__file__).resolve().parents[3]
PROMOTION_FIXTURE = TAP_ROOT / "Kandelo/staging/fixtures/promotion-decision.json"
ADMISSION_FIXTURE = TAP_ROOT / "Kandelo/staging/fixtures/admission-record.json"
SOURCE_ABI = 7
TARGET_ABI = SOURCE_ABI + 1
FORMULA = "bash"
ARCHITECTURE = "wasm32"
MERGE_COMMIT = "9" * 40
CURRENT_TAP_COMMIT = "7" * 40
CURRENT_TAP_TREE = "8" * 40
NEXT_TAP_COMMIT = "a" * 40
NEXT_TAP_TREE = "b" * 40


def _artifact(body: bytes, reference: str) -> dict[str, object]:
    digest = hashlib.sha256(body).hexdigest()
    return {
        "sha256": digest,
        "bytes": len(body),
        "immutable_reference": reference + "@sha256:" + digest,
    }


def _bottle_archive(formula: str, version: str) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            payload_root = f"{formula}/{version}"
            for path in (
                formula,
                payload_root,
                f"{payload_root}/.brew",
                f"{payload_root}/bin",
            ):
                member = tarfile.TarInfo(path)
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
                member.mtime = 0
                archive.addfile(member)
            files = {
                f"{payload_root}/.brew/{formula}.rb": (
                    b"class Fixture < Formula\nend\n",
                    0o644,
                ),
                f"{payload_root}/INSTALL_RECEIPT.json": (b"{}\n", 0o644),
                f"{payload_root}/bin/{formula}": (b"fixture executable\n", 0o755),
            }
            for path, (body, mode) in files.items():
                member = tarfile.TarInfo(path)
                member.size = len(body)
                member.mode = mode
                member.mtime = 0
                archive.addfile(member, io.BytesIO(body))
    return output.getvalue()


def _locator(value) -> dict[str, str]:
    return {
        "repository": value.repository,
        "digest": value.digest,
        "immutable_reference": value.immutable_reference,
    }


def _fetched_from_plan(plan: OciRecordPlanV1) -> FetchedOciRecordV1:
    manifest = build_oci_manifest(plan)
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()

    def blob(value: OciBlobV1):
        from scripts.abi_staging.oci import FetchedOciBlobV1

        return FetchedOciBlobV1(
            role=value.role,
            media_type=value.media_type,
            digest=value.digest,
            size=value.size,
            title=value.title,
            body=value.body,
        )

    return FetchedOciRecordV1(
        repository="ghcr.io/" + plan.repository,
        digest=digest,
        immutable_reference=f"ghcr.io/{plan.repository}@{digest}",
        artifact_type=plan.artifact_type,
        manifest=manifest,
        config=blob(plan.config),
        layers=tuple(blob(layer) for layer in plan.layers),
    )


class CandidateSelectionTests(unittest.TestCase):
    def _inventory(self, *, include_current: bool) -> SimpleNamespace:
        subject = exact_formula_subject("bash", ARCHITECTURE)
        legacy = SimpleNamespace(
            request_sha256="a" * 64,
            subject=subject,
            contract_sha256="b" * 64,
            record_sha256="1" * 64,
            binding_record_sha256=None,
            bottle_layer_sha256="2" * 64,
            descriptor_capable=False,
        )
        facts = [legacy]
        records = {
            legacy.record_sha256: {
                "candidate": {
                    "normalized_components": [
                        {"id": "bottle-contract"},
                        {"id": "bottle-metadata"},
                        {"id": "source-custody"},
                    ]
                }
            }
        }
        if include_current:
            current = SimpleNamespace(
                request_sha256=legacy.request_sha256,
                subject=subject,
                contract_sha256=legacy.contract_sha256,
                record_sha256="f" * 64,
                binding_record_sha256=None,
                bottle_layer_sha256="e" * 64,
                descriptor_capable=True,
            )
            facts.append(current)
            records[current.record_sha256] = {
                "candidate": {
                    "normalized_components": [
                        {"id": "bottle-contract"},
                        {"id": "bottle-metadata"},
                        {"id": "source-custody"},
                        {"id": "vfs-composition-descriptor"},
                    ]
                }
            }
        return SimpleNamespace(
            records=SimpleNamespace(candidates=tuple(facts)),
            candidate_records=records,
        )

    def test_descriptor_candidate_wins_over_compatible_legacy_record(self) -> None:
        selected = cli_module._select_current_candidate_fact(
            self._inventory(include_current=True),
            request_sha256="a" * 64,
            subject=exact_formula_subject("bash", ARCHITECTURE),
            contract_sha256="b" * 64,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.record_sha256, "f" * 64)

    def test_legacy_only_formula_returns_to_normal_rebuild_lane(self) -> None:
        selected = cli_module._select_current_candidate_fact(
            self._inventory(include_current=False),
            request_sha256="a" * 64,
            subject=exact_formula_subject("bash", ARCHITECTURE),
            contract_sha256="b" * 64,
        )
        self.assertIsNone(selected)


class PromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.transport = FakeRegistryTransport()
        self.tap_policy = load_tap_staging_policy(
            TAP_ROOT / "Kandelo/staging/tap-policy.toml"
        )
        self.promotion_policy = load_promotion_policy(
            TAP_ROOT / "Kandelo/staging/promotion-policy.toml"
        )
        definitions = load_verification_tests(
            TAP_ROOT / "Kandelo/staging/verification-tests.toml"
        )
        self.verification_tests = tuple(
            definition
            for definition in definitions
            if definition.id == "bottle-structure"
        )

        request, _old_digest, registry_body = _request()
        self.request = copy.deepcopy(request)
        self.registry = load_guard_registry(
            registry_body,
            expected_version=1,
            expected_sha256=hashlib.sha256(registry_body).hexdigest(),
        )
        self.request_digest = canonical_sha256(self.request)
        self.expected_request_policy = copy.deepcopy(self.request["issuance"])

        self.tap_plan = json.loads(
            (TAP_ROOT / "Kandelo/staging/fixtures/tap-plan.json").read_bytes()
        )
        self.tap_plan["request_digest"] = self.request_digest
        self.tap_plan["request_asset_url"] = (
            "https://github.com/Automattic/kandelo/releases/download/"
            "abi-staging-pr-19/candidate-request-"
            f"{self.request['build_source']['commit']}-sha256-"
            f"{self.request_digest}.json"
        )
        self.tap_plan["target_abi"] = copy.deepcopy(self.request["target_abi"])
        validate_tap_plan(self.tap_plan)
        self.tap_plan_digest = canonical_sha256(self.tap_plan)
        self.formula_plan = next(
            value
            for value in self.tap_plan["formulae"]
            if value["identity"]["name"] == FORMULA
            and value["identity"]["architecture"] == ARCHITECTURE
        )

        self.contract = self._contract()
        self.contract_body = canonical_bytes(self.contract)
        self.contract_sha256 = hashlib.sha256(self.contract_body).hexdigest()
        identity = self.formula_plan["identity"]
        pkg_version = (
            identity["version"]
            if identity["revision"] == 0
            else f"{identity['version']}_{identity['revision']}"
        )
        self.bottle_body = _bottle_archive(FORMULA, pkg_version)
        self.bottle_sha256 = hashlib.sha256(self.bottle_body).hexdigest()
        self.dependency_body = b"exact successor ncurses bottle\n"
        self.dependency_artifact = _artifact(
            self.dependency_body,
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/ncurses",
        )

        self.source_plan = self._source_plan()
        source_locator = publish_record(
            self.source_plan,
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.source = fetch_public_record(
            _locator(source_locator),
            transport=self.transport,
            expected_artifact_type=SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
            required_layer_roles=tuple(layer.role for layer in self.source_plan.layers),
        )

        self.candidate_plan = self._candidate_plan()
        candidate_locator = publish_record(
            self.candidate_plan,
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.candidate = fetch_public_record(
            _locator(candidate_locator),
            transport=self.transport,
            expected_artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
            required_layer_roles=(
                "bottle-layer",
                "bottle-metadata",
                "vfs-composition-descriptor",
                "bottle-contract",
            ),
        )
        self.candidate_record = json.loads(self.candidate.config.body)
        self.candidate_digest = self.candidate.digest.removeprefix("sha256:")

        self.verification_plan = self._verification_plan(outcome="success")
        verification_locator = publish_record(
            self.verification_plan,
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.verification = fetch_public_record(
            _locator(verification_locator),
            transport=self.transport,
            expected_artifact_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
            required_layer_roles=("immutable-record-bytes",),
        )

        self.history_plan, self.history_snapshot = self._history()
        history_locator = publish_record(
            self.history_plan,
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.history = fetch_public_record(
            _locator(history_locator),
            transport=self.transport,
            expected_artifact_type=HISTORY_RECORD_MEDIA_TYPE,
            required_layer_roles=("immutable-record-bytes",),
        )

    def _contract(self) -> dict[str, object]:
        value = json.loads(
            (TAP_ROOT / "Kandelo/staging/fixtures/bottle-contract.json").read_bytes()
        )
        identity = self.formula_plan["identity"]
        capture = self.formula_plan["capture"]
        value["target"] = {
            "abi": TARGET_ABI,
            "architecture": ARCHITECTURE,
            "snapshot_sha256": self.request["target_abi"]["snapshot_sha256"],
        }
        value["formula"] = {
            "name": FORMULA,
            "version": identity["version"],
            "revision": identity["revision"],
            "rebuild": identity["rebuild"],
            "normalized_source_sha256": capture["normalized_source_sha256"],
            "source_components": [
                {
                    "id": "formula",
                    "sha256": identity["normalized_formula_sha256"],
                },
                {
                    "id": "tap-input-0000",
                    "sha256": capture["tap_input_components"][0]["sha256"],
                },
            ],
        }
        value["direct_dependencies"] = [
            {
                "formula": "ncurses",
                "architecture": ARCHITECTURE,
                "bottle_layer_sha256": hashlib.sha256(
                    b"exact successor ncurses bottle\n"
                ).hexdigest(),
                "bottle_layer_bytes": len(b"exact successor ncurses bottle\n"),
                "materialization_policy_sha256": self.formula_plan[
                    "direct_dependencies"
                ][0]["materialization_policy_sha256"],
            }
        ]
        return load_bottle_contract(canonical_bytes(value))

    def _source_plan(self) -> OciRecordPlanV1:
        custody = self.root / "custody"
        _write_custody(custody)
        manifest_path = custody / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["request_sha256"] = self.request_digest
        manifest["sources"][0].update(self.request["build_source"])
        manifest["sources"][1].update(self.tap_plan["tap_source"])
        manifest["capsule_sha256"] = source_capsule_digest(manifest)
        manifest_path.write_bytes(canonical_bytes(manifest))
        from scripts.abi_staging.records import build_source_custody_oci_plan

        return build_source_custody_oci_plan(
            custody,
            repository="kandelo-dev/homebrew-tap-core-abi-8-source-custody",
        )

    def _candidate_record(self) -> dict[str, object]:
        candidate_repository = (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/bash"
        )
        bottle = _artifact(self.bottle_body, candidate_repository)
        metadata = canonical_bytes(
            {"architecture": ARCHITECTURE, "formula": FORMULA, "nonendorsed": True}
        )
        composition_descriptor = self._candidate_composition_descriptor()
        source_digest = hashlib.sha256(build_oci_manifest(self.source_plan)).hexdigest()
        record = {
            "schema": 1,
            "kind": "kandelo-abi-staging-candidate",
            "common": {
                "request_sha256": self.request_digest,
                "subject": {
                    "kind": "candidate",
                    "identity": (
                        "kandelo-dev/homebrew-tap-core/bash@sha256:"
                        + self.bottle_sha256
                    ),
                },
                "source": copy.deepcopy(self.request["build_source"]),
                "run": {
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "workflow_ref": (
                        ".github/workflows/abi-staging.yml@refs/heads/main"
                    ),
                    "run_id": 101,
                    "run_attempt": 1,
                    "job": "publish-candidate",
                },
                "guard_codes": [],
                "work_state": "complete",
                "outcome": "success",
                "artifact_class": "candidate",
                "artifact": bottle,
                "promotion_state": "unknown",
                "retry_state": {
                    "attempts": 1,
                    "eligible": False,
                    "exhausted": False,
                    "next_action": "none",
                },
                "blockers": [],
            },
            "candidate": {
                "formula": {
                    "tap": "kandelo-dev/homebrew-tap-core",
                    "formula": FORMULA,
                    "version": self.formula_plan["identity"]["version"],
                    "revision": self.formula_plan["identity"]["revision"],
                    "bottle_rebuild": self.formula_plan["identity"]["rebuild"],
                    "architecture": ARCHITECTURE,
                    "target_abi": TARGET_ABI,
                    "bottle_contract_sha256": self.contract_sha256,
                },
                "bottle_layer": bottle,
                "normalized_components": [
                    {
                        "id": "bottle-contract",
                        "artifact": _artifact(
                            self.contract_body, candidate_repository
                        ),
                    },
                    {
                        "id": "bottle-metadata",
                        "artifact": _artifact(metadata, candidate_repository),
                    },
                    {
                        "id": "source-custody",
                        "artifact": {
                            "sha256": source_digest,
                            "bytes": len(build_oci_manifest(self.source_plan)),
                            "immutable_reference": self.source.immutable_reference,
                        },
                    },
                    {
                        "id": "vfs-composition-descriptor",
                        "artifact": _artifact(
                            composition_descriptor, candidate_repository
                        ),
                    },
                ],
                "direct_dependency_layers": [
                    {"id": "ncurses-wasm32", "artifact": self.dependency_artifact}
                ],
                "source_custody_sha256": source_digest,
                "producer": {
                    "request_sha256": self.request_digest,
                    "head": self.request["build_source"]["commit"],
                    "run_id": 77,
                },
                "nonendorsed": True,
            },
        }
        validate_candidate_record(record)
        return record

    def _candidate_plan(self, record: dict[str, object] | None = None) -> OciRecordPlanV1:
        record = self._candidate_record() if record is None else record
        repository = "kandelo-dev/homebrew-tap-core-abi-8-candidates/bash"
        metadata = canonical_bytes(
            {"architecture": ARCHITECTURE, "formula": FORMULA, "nonendorsed": True}
        )
        composition_descriptor = self._candidate_composition_descriptor()
        return OciRecordPlanV1(
            repository=repository,
            artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
            config=OciBlobV1(
                role="candidate-record",
                media_type=CANDIDATE_RECORD_MEDIA_TYPE,
                body=canonical_bytes(record),
                title="candidate-record.json",
            ),
            layers=(
                OciBlobV1(
                    role="bottle-layer",
                    media_type=BOTTLE_LAYER_MEDIA_TYPE,
                    body=self.bottle_body,
                    title="bottle.tar.gz",
                ),
                OciBlobV1(
                    role="bottle-metadata",
                    media_type=BOTTLE_METADATA_MEDIA_TYPE,
                    body=metadata,
                    title="bottle-metadata.json",
                ),
                OciBlobV1(
                    role="vfs-composition-descriptor",
                    media_type=VFS_COMPOSITION_DESCRIPTOR_MEDIA_TYPE,
                    body=composition_descriptor,
                    title="vfs-composition-descriptor.json",
                ),
                OciBlobV1(
                    role="bottle-contract",
                    media_type=BOTTLE_CONTRACT_MEDIA_TYPE,
                    body=self.contract_body,
                    title="bottle-contract.json",
                ),
            ),
            annotations={
                "dev.kandelo.abi-staging.classification": (
                    "public-candidate-not-endorsed"
                ),
                "dev.kandelo.abi-staging.nonendorsed": "true",
            },
        )

    def _candidate_composition_descriptor(self) -> bytes:
        candidate_repository = (
            "kandelo-dev/homebrew-tap-core-abi-8-candidates/bash"
        )
        return canonical_bytes(
            {
                "architecture": ARCHITECTURE,
                "formula": FORMULA,
                "kind": "kandelo-homebrew-original-bottle-tree",
                "required_by": [FORMULA],
                "dependencies": ["kandelo-dev/tap-core/ncurses"],
                "schema": 2,
                "tap": "kandelo-dev/homebrew-tap-core",
                "tree": {
                    "activation": {
                        "capabilities": [f"homebrew-bottle:{FORMULA}"],
                        "mode": "first-use",
                        "roots": [
                            "/opt/kandelo/homebrew/Cellar/"
                            + FORMULA
                            + "/"
                            + str(self.formula_plan["identity"]["version"])
                        ],
                    },
                    "content": {
                        "bytes": len(self.bottle_body),
                        "decoder": "homebrew-bottle-tar-gzip-v1",
                        "media_type": (
                            "application/vnd.oci.image.layer.v1.tar+gzip"
                        ),
                        "sha256": self.bottle_sha256,
                    },
                    "id": FORMULA,
                    "inventory": {},
                    "package": "kandelo-dev/tap-core/bash",
                    "transports": [
                        {
                            "kind": "external-https",
                            "url": (
                                "https://ghcr.io/v2/"
                                + candidate_repository
                                + "/blobs/sha256:"
                                + self.bottle_sha256
                            ),
                        }
                    ],
                },
            }
        )

    def _verification_record(
        self,
        *,
        outcome: str,
        definition=None,
        host: str | None = None,
    ) -> dict[str, object]:
        definition = self.verification_tests[0] if definition is None else definition
        host = definition.hosts[0] if host is None else host
        success = outcome == "success"
        guard = None if success else "verification_failed"
        record = {
            "schema": 1,
            "kind": "kandelo-abi-staging-verification",
            "common": {
                "request_sha256": self.request_digest,
                "subject": {"kind": "candidate", "identity": self.candidate_digest},
                "source": copy.deepcopy(self.request["build_source"]),
                "run": {
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "workflow_ref": (
                        ".github/workflows/abi-staging.yml@refs/heads/main"
                    ),
                    "run_id": 202,
                    "run_attempt": 1,
                    "job": "verify-candidate",
                },
                "guard_codes": [] if success else [guard],
                "work_state": "complete",
                "outcome": outcome,
                "artifact_class": "none",
                "promotion_state": "eligible" if success else "ineligible",
                "retry_state": {
                    "attempts": 1,
                    "eligible": False,
                    "exhausted": False,
                    "next_action": "none",
                },
                "blockers": (
                    []
                    if success
                    else [
                        {
                            "guard_code": guard,
                            "subject_kind": "candidate",
                            "subject": self.candidate_digest,
                        }
                    ]
                ),
            },
            "verification": {
                "candidate_record_sha256": self.candidate_digest,
                "candidate_layer": copy.deepcopy(
                    self.candidate_record["candidate"]["bottle_layer"]
                ),
                "test_definition_sha256": definition.sha256,
                "host": host,
                "attempt_ordinal": 0,
                "diagnostics": [],
            },
        }
        validate_verification_receipt_record(record)
        return record

    def _verification_plan(
        self,
        *,
        outcome: str,
        definition=None,
        host: str | None = None,
    ) -> OciRecordPlanV1:
        definition = self.verification_tests[0] if definition is None else definition
        host = definition.hosts[0] if host is None else host
        record = self._verification_record(
            outcome=outcome,
            definition=definition,
            host=host,
        )
        body = canonical_bytes(record)
        return OciRecordPlanV1(
            repository=receipt_repository(
                "kandelo-dev/homebrew-tap-core-abi-8-candidates/bash",
                definition.id,
                host,
            ),
            artifact_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
            config=OciBlobV1(
                role="verification-receipt",
                media_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
                body=body,
                title="verification-receipt.json",
            ),
            layers=(
                OciBlobV1(
                    role="immutable-record-bytes",
                    media_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
                    body=body,
                    title="verification-receipt.json",
                ),
            ),
            annotations={"dev.kandelo.abi-staging.kind": "verification-receipt"},
        )

    def _verification_for_candidate_digest(
        self, candidate_digest: str
    ) -> FetchedOciRecordV1:
        receipt_record = self._verification_record(outcome="success")
        receipt_record["common"]["subject"]["identity"] = candidate_digest
        receipt_record["verification"]["candidate_record_sha256"] = candidate_digest
        receipt_body = canonical_bytes(receipt_record)
        receipt_plan = self._verification_plan(outcome="success")
        receipt_plan = replace(
            receipt_plan,
            config=replace(receipt_plan.config, body=receipt_body),
            layers=(replace(receipt_plan.layers[0], body=receipt_body),),
        )
        return _fetched_from_plan(receipt_plan)

    def _history(self) -> tuple[OciRecordPlanV1, dict[str, object]]:
        fixture = json.loads(
            (TAP_ROOT / "Kandelo/staging/fixtures/abi-history-record.json").read_bytes()
        )
        plan = fixture["plan"]
        plan["preactivation_tap_commit"] = self.tap_plan["tap_source"]["commit"]
        plan["preactivation_tap_tree"] = self.tap_plan["tap_source"]["tree"]
        fixture["created_ref_object"] = plan["preactivation_tap_commit"]
        snapshot = {
            "schema": 1,
            "kind": "kandelo-abi-history-protection-snapshot",
            "repository": "kandelo-dev/homebrew-tap-core",
            "branch": f"abi/{SOURCE_ABI}",
            "phase": "postcreate",
            "ref": {
                "object": plan["preactivation_tap_commit"],
                "tree": plan["preactivation_tap_tree"],
            },
            "direct": {
                "branch": f"abi/{SOURCE_ABI}",
                "allow_deletions": False,
                "allow_force_pushes": False,
                "enforce_admins": True,
            },
            "rulesets": [],
        }
        fixture["protection_evidence"] = validate_protection_snapshot(
            plan,
            snapshot,
            phase="postcreate",
            expected_repository="kandelo-dev/homebrew-tap-core",
        )
        history_plan = build_history_oci_plan(
            fixture,
            repository="kandelo-dev/homebrew-tap-core-abi-7-records/history",
        )
        return history_plan, snapshot

    def _merge_fact(self, **changes) -> dict[str, object]:
        value = {
            "repository": "Automattic/kandelo",
            "number": 19,
            "state": "merged",
            "head": self.request["build_source"]["commit"],
            "merge_commit": MERGE_COMMIT,
        }
        value.update(changes)
        return value

    def _current_dependencies(self) -> dict[str, dict[str, object]]:
        return {
            exact_formula_subject("ncurses", ARCHITECTURE): copy.deepcopy(
                self.dependency_artifact
            )
        }

    def _evaluate(self, **changes):
        arguments = {
            "request": self.request,
            "request_digest": self.request_digest,
            "merge_fact": self._merge_fact(),
            "tap_plan": self.tap_plan,
            "tap_plan_digest": self.tap_plan_digest,
            "candidate": self.candidate,
            "source_custody": self.source,
            "verification_receipts": (self.verification,),
            "override_receipts": (),
            "history": self.history,
            "history_protection_snapshot": self.history_snapshot,
            "current_tap_source": copy.deepcopy(self.tap_plan["tap_source"]),
            "current_formula": copy.deepcopy(self.formula_plan),
            "current_dependency_layers": self._current_dependencies(),
            "policy": self.promotion_policy,
            "expected_request_policy": self.expected_request_policy,
            "verification_tests": self.verification_tests,
        }
        arguments.update(changes)
        return evaluate_promotion(**arguments)

    def _new_request_reuse(self):
        request = copy.deepcopy(self.request)
        request["build_source"] = {
            "repository": self.request["build_source"]["repository"],
            "commit": "4" * 40,
            "tree": "5" * 40,
        }
        request_digest = canonical_sha256(request)
        tap_plan = copy.deepcopy(self.tap_plan)
        tap_plan["request_digest"] = request_digest
        tap_plan["request_asset_url"] = (
            "https://github.com/Automattic/kandelo/releases/download/"
            "abi-staging-pr-19/candidate-request-"
            f"{request['build_source']['commit']}-sha256-{request_digest}.json"
        )
        validate_tap_plan(tap_plan)
        payload = self.candidate_record["candidate"]
        source = next(
            item["artifact"]
            for item in payload["normalized_components"]
            if item["id"] == "source-custody"
        )
        existing = {
            "schema": 1,
            "kind": "kandelo-existing-candidate",
            "contract_sha256": payload["formula"]["bottle_contract_sha256"],
            "formula": {
                key: payload["formula"][key]
                for key in ("tap", "formula", "architecture", "target_abi")
            },
            "candidate_record": {
                "record_sha256": self.candidate_digest,
                "immutable_reference": self.candidate.immutable_reference,
            },
            "source_custody": {
                "record_sha256": source["sha256"],
                "immutable_reference": source["immutable_reference"],
            },
            "bottle_layer": copy.deepcopy(payload["bottle_layer"]),
            "qualifying_receipts": [
                {
                    "record_sha256": self.verification.digest.removeprefix(
                        "sha256:"
                    ),
                    "immutable_reference": self.verification.immutable_reference,
                }
            ],
            "original_producer": copy.deepcopy(payload["producer"]),
            "nonendorsed": True,
        }
        record = make_candidate_reuse_record(
            self.contract,
            exact_formula_subject(FORMULA, ARCHITECTURE),
            existing,
            {
                "request_sha256": request_digest,
                "source": copy.deepcopy(request["build_source"]),
                "run": {
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "workflow_ref": (
                        ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
                    ),
                    "run_id": 303,
                    "run_attempt": 1,
                    "job": "publish-reuse",
                },
            },
        )
        plan = build_candidate_reuse_oci_plan(
            record,
            repository=(
                "kandelo-dev/homebrew-tap-core-abi-8-candidates/bash/reuse"
            ),
        )
        return request, request_digest, tap_plan, record, _fetched_from_plan(plan)

    def test_exact_merged_candidate_is_eligible(self) -> None:
        decision = self._evaluate()
        validate_promotion_decision(asdict(decision))
        self.assertEqual(decision.eligibility, "eligible")
        self.assertEqual(decision.tap_source_state, "exact")
        self.assertEqual(decision.formula_subject, exact_formula_subject(FORMULA, ARCHITECTURE))
        self.assertEqual(decision.candidate_record_digest, self.candidate_digest)
        self.assertEqual(decision.bottle_layer_sha256, self.bottle_sha256)
        self.assertEqual(decision.source_custody_digest, self.source.digest.removeprefix("sha256:"))
        self.assertEqual(
            decision.qualifying_receipts,
            (self.verification.digest.removeprefix("sha256:"),),
        )
        self.assertEqual(decision.candidate_binding_digest, self.candidate_digest)

    def test_exact_historical_candidate_reuse_is_eligible_and_keeps_provenance(self) -> None:
        request, request_digest, tap_plan, _record, reuse = self._new_request_reuse()
        decision = self._evaluate(
            request=request,
            request_digest=request_digest,
            merge_fact={
                **self._merge_fact(),
                "head": request["build_source"]["commit"],
            },
            tap_plan=tap_plan,
            tap_plan_digest=canonical_sha256(tap_plan),
            candidate_reuse=reuse,
        )

        self.assertEqual(decision.eligibility, "eligible")
        self.assertEqual(decision.request_digest, request_digest)
        self.assertEqual(decision.candidate_record_digest, self.candidate_digest)
        self.assertEqual(
            decision.candidate_binding_digest,
            reuse.digest.removeprefix("sha256:"),
        )
        self.assertEqual(
            decision.qualifying_receipts,
            (self.verification.digest.removeprefix("sha256:"),),
        )
        plan = build_canonical_bottle_plan(
            decision,
            candidate=self.candidate,
            policy=self.promotion_policy,
        )
        transport = FakeRegistryTransport()
        layer = plan.layers[0]
        assert layer.mount_from is not None
        transport.blobs[(layer.mount_from, layer.digest)] = layer.body
        publication = publish_canonical_bottle(
            plan,
            decision=decision,
            candidate=self.candidate,
            policy=self.promotion_policy,
            transport=transport,
        )
        prepared = prepare_admission(
            decision,
            candidate=self.candidate,
            candidate_reuse=reuse,
            canonical_publication=publication,
            preactivation_tap_source=tap_plan["tap_source"],
            abi_history_record_sha256=self.history.digest.removeprefix("sha256:"),
            policy=self.promotion_policy,
        )
        self.assertEqual(dict(prepared.request_source), request["build_source"])
        self.assertEqual(
            dict(prepared.candidate_source), self.request["build_source"]
        )
        self.assertEqual(
            dict(prepared.original_producer),
            self.candidate_record["candidate"]["producer"],
        )
        metadata_source = {
            "repository": self.promotion_policy.tap_repository,
            "commit": NEXT_TAP_COMMIT,
            "tree": NEXT_TAP_TREE,
        }
        metadata_base_source = {
            "repository": self.promotion_policy.tap_repository,
            "commit": "6" * 40,
            "tree": "5" * 40,
        }
        formula_update = {
            "formula": FORMULA,
            "architecture": ARCHITECTURE,
            "expected_main_commit": metadata_base_source["commit"],
            "expected_normalized_formula_sha256": self.formula_plan["identity"][
                "normalized_formula_sha256"
            ],
            "expected_generated_metadata_sha256": "e" * 64,
            "allowed_paths": [
                "Formula/bash.rb",
                "Kandelo/formula/bash.json",
                "Kandelo/metadata.json",
                "Kandelo/link/bash-1.0-rebuild1-wasm32.json",
            ],
            "link_manifest_path": (
                "Kandelo/link/bash-1.0-rebuild1-wasm32.json"
            ),
            "link_manifest_sha256": "f" * 64,
            "canonical_manifest_digest": publication.artifact["sha256"],
            "bottle_layer_sha256": self.bottle_sha256,
            "bottle_layer_bytes": len(self.bottle_body),
            "target_abi": TARGET_ABI,
        }
        admission = finalize_admission_record(
            prepared,
            formula_metadata_base_source=metadata_base_source,
            formula_metadata_source=metadata_source,
            formula_metadata_update=formula_update,
            post_write_readback={
                "source": metadata_source,
                "formula_metadata_update": formula_update,
            },
            run=OVERRIDE_RUN,
        )
        self.assertEqual(
            admission["admission"]["candidate_binding_sha256"],
            reuse.digest.removeprefix("sha256:"),
        )
        self.assertEqual(admission["common"]["source"], request["build_source"])
        self.assertEqual(
            admission["admission"]["original_producer"],
            self.candidate_record["candidate"]["producer"],
        )

    def test_historical_candidate_requires_the_exact_reuse_binding(self) -> None:
        request, request_digest, tap_plan, record, reuse = self._new_request_reuse()
        arguments = {
            "request": request,
            "request_digest": request_digest,
            "merge_fact": {
                **self._merge_fact(),
                "head": request["build_source"]["commit"],
            },
            "tap_plan": tap_plan,
            "tap_plan_digest": canonical_sha256(tap_plan),
        }
        with self.assertRaises(PromotionError):
            self._evaluate(**arguments)

        changed = copy.deepcopy(record)
        changed["common"]["source"]["tree"] = "6" * 40
        body = canonical_bytes(changed)
        changed_plan = build_candidate_reuse_oci_plan(
            changed,
            repository=(
                "kandelo-dev/homebrew-tap-core-abi-8-candidates/bash/reuse"
            ),
        )
        self.assertEqual(
            changed_plan.artifact_type, CANDIDATE_REUSE_RECORD_MEDIA_TYPE
        )
        with self.assertRaises(PromotionError):
            self._evaluate(
                **arguments,
                candidate_reuse=_fetched_from_plan(changed_plan),
            )

        wrong_repository = replace(
            reuse,
            repository=(
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                "bash/other"
            ),
        )
        with self.assertRaises(PromotionError):
            self._evaluate(**arguments, candidate_reuse=wrong_repository)

    def test_separated_writer_revalidates_the_reuse_locator(self) -> None:
        request, request_digest, tap_plan, _record, reuse = self._new_request_reuse()
        decision = self._evaluate(
            request=request,
            request_digest=request_digest,
            merge_fact={
                **self._merge_fact(),
                "head": request["build_source"]["commit"],
            },
            tap_plan=tap_plan,
            tap_plan_digest=canonical_sha256(tap_plan),
            candidate_reuse=reuse,
        )
        expected = expected_canonical_publication(
            decision,
            candidate=self.candidate,
            policy=self.promotion_policy,
        )
        detail = {
            "decision": asdict(decision),
            "candidate_locator": _locator(self.candidate),
            "candidate_reuse_locator": _locator(reuse),
            "canonical": {
                "locator": asdict(expected.locator),
                "artifact": dict(expected.artifact),
            },
        }
        with (
            patch.object(
                cli_module,
                "_fetch_candidate_record",
                return_value=self.candidate,
            ),
            patch.object(
                cli_module,
                "_fetch_candidate_reuse",
                return_value=reuse,
            ),
        ):
            checked = cli_module._promotion_candidate_and_canonical(
                detail,
                policy=self.promotion_policy,
                transport=object(),
            )
            self.assertEqual(checked, (decision, self.candidate, reuse, expected))
            missing = copy.deepcopy(detail)
            missing["candidate_reuse_locator"] = None
            with self.assertRaises(cli_module.WorkflowPublicationError):
                cli_module._promotion_candidate_and_canonical(
                    missing,
                    policy=self.promotion_policy,
                    transport=object(),
                )

    def test_merge_fact_is_fetched_for_the_exact_request_pr(self) -> None:
        class PublicClient:
            def __init__(self, lifecycle: PullRequestLifecycleV1) -> None:
                self.policy = SimpleNamespace(issuer_repository="Automattic/kandelo")
                self.lifecycle = lifecycle
                self.requested: list[int] = []

            def pull_request_lifecycle(self, number: int) -> PullRequestLifecycleV1:
                self.requested.append(number)
                return self.lifecycle

        client = PublicClient(
            PullRequestLifecycleV1(
                "merged", self.request["build_source"]["commit"], MERGE_COMMIT
            )
        )
        fact = promotion_module.fetch_exact_merge_fact(self.request, client)
        self.assertEqual(client.requested, [self.request["pull_request"]["number"]])
        self.assertEqual(fact, self._merge_fact())

        for lifecycle in (
            PullRequestLifecycleV1(
                "open", self.request["build_source"]["commit"], None
            ),
            PullRequestLifecycleV1(
                "closed", self.request["build_source"]["commit"], None
            ),
        ):
            with self.subTest(state=lifecycle.state), self.assertRaises(PromotionError):
                promotion_module.fetch_exact_merge_fact(
                    self.request, PublicClient(lifecycle)
                )

        wrong_repository = PublicClient(
            PullRequestLifecycleV1(
                "merged", self.request["build_source"]["commit"], MERGE_COMMIT
            )
        )
        wrong_repository.policy = SimpleNamespace(
            issuer_repository="Other/repository"
        )
        with self.assertRaises(PromotionError):
            promotion_module.fetch_exact_merge_fact(self.request, wrong_repository)

    def test_open_closed_wrong_or_malformed_merge_facts_are_rejected(self) -> None:
        invalid = (
            self._merge_fact(state="open", merge_commit=None),
            self._merge_fact(state="closed", merge_commit=None),
            self._merge_fact(number=20),
            self._merge_fact(repository="Other/repository"),
            self._merge_fact(head="f" * 40),
            {**self._merge_fact(), "extra": True},
        )
        for merge_fact in invalid:
            with self.subTest(merge_fact=merge_fact), self.assertRaises(PromotionError):
                self._evaluate(merge_fact=merge_fact)

    def test_request_candidate_custody_and_public_hash_mismatches_fail_closed(self) -> None:
        wrong_request = copy.deepcopy(self.request)
        wrong_request["pull_request"]["number"] = 20
        corrupted_candidate = replace(self.candidate, manifest=self.candidate.manifest + b"x")
        corrupted_custody = replace(self.source, config=replace(self.source.config, body=b"{}\n"))
        incomplete_custody = replace(self.source, layers=self.source.layers[:-1])
        for changes in (
            {"request": wrong_request},
            {"candidate": corrupted_candidate},
            {"source_custody": corrupted_custody},
            {"source_custody": incomplete_custody},
        ):
            with self.subTest(changes=tuple(changes)), self.assertRaises(PromotionError):
                self._evaluate(**changes)

    def test_public_manifest_cannot_repeat_a_layer_role(self) -> None:
        duplicate_manifest = json.loads(self.candidate.manifest)
        duplicate_manifest["layers"].append(
            copy.deepcopy(duplicate_manifest["layers"][-1])
        )
        duplicate_manifest_body = canonical_bytes(duplicate_manifest)
        duplicate_manifest_digest = "sha256:" + hashlib.sha256(
            duplicate_manifest_body
        ).hexdigest()
        duplicate_candidate = replace(
            self.candidate,
            digest=duplicate_manifest_digest,
            immutable_reference=(
                f"{self.candidate.repository}@{duplicate_manifest_digest}"
            ),
            manifest=duplicate_manifest_body,
        )

        with self.assertRaises(PromotionError):
            self._evaluate(
                candidate=duplicate_candidate,
                verification_receipts=(
                    self._verification_for_candidate_digest(
                        duplicate_manifest_digest.removeprefix("sha256:")
                    ),
                ),
            )

    def test_candidate_normalized_custody_identity_matches_public_record(self) -> None:
        changed_record = copy.deepcopy(self.candidate_record)
        custody = next(
            item
            for item in changed_record["candidate"]["normalized_components"]
            if item["id"] == "source-custody"
        )
        custody["artifact"]["bytes"] += 1
        changed_candidate = _fetched_from_plan(self._candidate_plan(changed_record))
        changed_digest = changed_candidate.digest.removeprefix("sha256:")
        with self.assertRaises(PromotionError):
            self._evaluate(
                candidate=changed_candidate,
                verification_receipts=(
                    self._verification_for_candidate_digest(changed_digest),
                ),
            )

    def test_bottle_contract_and_candidate_dependency_layers_cannot_diverge(self) -> None:
        changed_contract = copy.deepcopy(self.contract)
        changed_contract["direct_dependencies"][0]["bottle_layer_sha256"] = (
            "f" * 64
        )
        changed_contract_body = canonical_bytes(changed_contract)
        changed_contract_digest = hashlib.sha256(changed_contract_body).hexdigest()
        changed_record = copy.deepcopy(self.candidate_record)
        changed_record["candidate"]["formula"]["bottle_contract_sha256"] = (
            changed_contract_digest
        )
        contract_component = next(
            item
            for item in changed_record["candidate"]["normalized_components"]
            if item["id"] == "bottle-contract"
        )
        contract_component["artifact"] = _artifact(
            changed_contract_body,
            self.candidate.repository,
        )
        plan = self._candidate_plan(changed_record)
        plan = replace(
            plan,
            layers=tuple(
                replace(layer, body=changed_contract_body)
                if layer.role == "bottle-contract"
                else layer
                for layer in plan.layers
            ),
        )
        changed_candidate = _fetched_from_plan(plan)
        changed_digest = changed_candidate.digest.removeprefix("sha256:")
        with self.assertRaises(PromotionError):
            self._evaluate(
                candidate=changed_candidate,
                verification_receipts=(
                    self._verification_for_candidate_digest(changed_digest),
                ),
            )

    def test_failed_verification_is_ineligible_but_exact_override_can_accept_it(self) -> None:
        failed = _fetched_from_plan(self._verification_plan(outcome="failure"))
        decision = self._evaluate(verification_receipts=(failed,))
        self.assertEqual(decision.eligibility, "ineligible")

        override = accept_artifact_risk(
            request=self.request,
            request_sha256=self.request_digest,
            candidate=self.candidate_record,
            candidate_record_sha256=self.candidate_digest,
            accepted_guard_codes=("verification_failed",),
            guard_registry=self.registry,
            maintainer=MAINTAINER,
            justification="Reviewed the exact failing successor bottle bytes.",
            run=OVERRIDE_RUN,
            tap_repository=self.tap_policy.tap_repository,
        )
        override_plan = build_override_receipt_oci_plan(
            override,
            candidate=self.candidate_record,
            policy=self.tap_policy,
        )
        accepted = self._evaluate(
            verification_receipts=(failed,),
            override_receipts=(_fetched_from_plan(override_plan),),
        )
        self.assertEqual(accepted.eligibility, "eligible")
        self.assertEqual(len(accepted.override_receipts), 1)

        wrong = copy.deepcopy(override)
        wrong["override_receipt"]["candidate_record_sha256"] = "f" * 64
        wrong_plan = replace(
            override_plan,
            config=replace(override_plan.config, body=canonical_bytes(wrong)),
            layers=(replace(override_plan.layers[0], body=canonical_bytes(wrong)),),
        )
        with self.assertRaises(PromotionError):
            self._evaluate(
                verification_receipts=(failed,),
                override_receipts=(_fetched_from_plan(wrong_plan),),
            )

        accepted_override = _fetched_from_plan(override_plan)
        hostile_repository = "ghcr.io/attacker/forged-receipts"
        hostile_override = replace(
            accepted_override,
            repository=hostile_repository,
            immutable_reference=f"{hostile_repository}@{accepted_override.digest}",
        )
        with self.assertRaises(PromotionError):
            self._evaluate(
                verification_receipts=(failed,),
                override_receipts=(hostile_override,),
            )

    def test_override_discovery_ignores_valid_receipts_for_other_candidates(self) -> None:
        exact_record = accept_artifact_risk(
            request=self.request,
            request_sha256=self.request_digest,
            candidate=self.candidate_record,
            candidate_record_sha256=self.candidate_digest,
            accepted_guard_codes=("verification_failed",),
            guard_registry=self.registry,
            maintainer=MAINTAINER,
            justification="Reviewed the exact failing successor bottle bytes.",
            run=OVERRIDE_RUN,
            tap_repository=self.tap_policy.tap_repository,
        )
        foreign_record = accept_artifact_risk(
            request=self.request,
            request_sha256=self.request_digest,
            candidate=self.candidate_record,
            candidate_record_sha256="f" * 64,
            accepted_guard_codes=("verification_failed",),
            guard_registry=self.registry,
            maintainer=MAINTAINER,
            justification="Reviewed another exact successor candidate bottle.",
            run=OVERRIDE_RUN,
            tap_repository=self.tap_policy.tap_repository,
        )
        exact = _fetched_from_plan(
            build_override_receipt_oci_plan(
                exact_record,
                candidate=self.candidate_record,
                policy=self.tap_policy,
            )
        )
        foreign = _fetched_from_plan(
            build_override_receipt_oci_plan(
                foreign_record,
                candidate=self.candidate_record,
                policy=self.tap_policy,
            )
        )
        with patch.object(
            cli_module,
            "list_public_record_locators",
            return_value=({}, {}),
        ), patch.object(
            cli_module,
            "fetch_public_record",
            side_effect=(exact, foreign),
        ):
            selected = cli_module._fetch_candidate_overrides(
                self.candidate,
                transport=SimpleNamespace(),
            )

        self.assertEqual(selected, (exact,))

    def test_every_protected_verification_identity_is_required(self) -> None:
        definitions = load_verification_tests(
            TAP_ROOT / "Kandelo/staging/verification-tests.toml"
        )
        receipts = tuple(
            _fetched_from_plan(
                self._verification_plan(
                    outcome="success",
                    definition=definition,
                    host=host,
                )
            )
            for definition in definitions
            for host in definition.hosts
        )
        complete = self._evaluate(
            verification_tests=definitions,
            verification_receipts=receipts,
        )
        self.assertEqual(complete.eligibility, "eligible")
        self.assertEqual(len(complete.qualifying_receipts), len(receipts))

        incomplete = self._evaluate(
            verification_tests=definitions,
            verification_receipts=receipts[:-1],
        )
        self.assertEqual(incomplete.eligibility, "ineligible")

    def test_verification_receipt_must_use_its_protected_candidate_namespace(self) -> None:
        hostile_repository = "ghcr.io/attacker/forged-receipts"
        hostile = replace(
            self.verification,
            repository=hostile_repository,
            immutable_reference=f"{hostile_repository}@{self.verification.digest}",
        )
        with self.assertRaises(PromotionError):
            self._evaluate(verification_receipts=(hostile,))

    def test_history_must_exist_remain_protected_and_match_target_abi(self) -> None:
        with self.assertRaises(PromotionError):
            self._evaluate(history=None)

        unprotected = copy.deepcopy(self.history_snapshot)
        unprotected["direct"] = None
        with self.assertRaises(PromotionError):
            self._evaluate(history_protection_snapshot=unprotected)

        moved = copy.deepcopy(self.history_snapshot)
        moved["ref"]["object"] = "f" * 40
        with self.assertRaises(PromotionError):
            self._evaluate(history_protection_snapshot=moved)

        moved_tree = copy.deepcopy(self.history_snapshot)
        moved_tree["ref"]["tree"] = "f" * 40
        with self.assertRaises(PromotionError):
            self._evaluate(history_protection_snapshot=moved_tree)

        wrong_plan = copy.deepcopy(self.tap_plan)
        wrong_plan["target_abi"]["version"] = TARGET_ABI + 1
        with self.assertRaises(PromotionError):
            self._evaluate(
                tap_plan=wrong_plan,
                tap_plan_digest=canonical_sha256(wrong_plan),
            )

    def test_history_recheck_accepts_equivalent_fresh_protection_observation(
        self,
    ) -> None:
        evolved = copy.deepcopy(self.history_snapshot)
        evolved["direct"] = None
        evolved["rulesets"] = [
            {
                "id": 73,
                "name": "Renamed protected ABI history policy",
                "target": "branch",
                "enforcement": "active",
                "include": ["refs/heads/abi/*"],
                "exclude": [],
                "rules": ["creation", "deletion", "non_fast_forward"],
                "bypass_actors": [],
            }
        ]

        decision = self._evaluate(history_protection_snapshot=evolved)

        self.assertEqual(decision.eligibility, "eligible")

    def test_history_recheck_rejects_changed_stable_protection_authority(
        self,
    ) -> None:
        record = json.loads(self.history.config.body)
        recorded = record["protection_evidence"]
        plan = record["plan"]
        fresh = validate_protection_snapshot(
            plan,
            self.history_snapshot,
            phase="postcreate",
            expected_repository="kandelo-dev/homebrew-tap-core",
        )
        mutations = {
            "covered": False,
            "ref_object": "f" * 40,
            "ref_tree": "f" * 40,
            "protection_requirement_sha256": "f" * 64,
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(fresh)
                changed[field] = value
                with self.assertRaises(PromotionError):
                    promotion_module._require_same_history_protection_authority(
                        recorded,
                        changed,
                    )

    def test_postactivation_plan_uses_immutable_preactivation_history_epoch(self) -> None:
        current_plan = copy.deepcopy(self.tap_plan)
        current_plan["tap_source"] = {
            "repository": self.promotion_policy.tap_repository,
            "commit": "8" * 40,
            "tree": "9" * 40,
        }

        decision = self._evaluate(
            tap_plan=current_plan,
            tap_plan_digest=canonical_sha256(current_plan),
            current_tap_source=current_plan["tap_source"],
            history_tap_source=self.tap_plan["tap_source"],
        )

        self.assertEqual(decision.eligibility, "eligible")
        self.assertEqual(decision.tap_source_state, "exact")

    def test_each_writer_rechecks_the_exact_history_ref_and_protection(self) -> None:
        locator = {
            "repository": self.history.repository,
            "digest": self.history.digest,
            "immutable_reference": self.history.immutable_reference,
        }
        client = SimpleNamespace(
            protection_snapshot=lambda branch, phase: copy.deepcopy(
                self.history_snapshot
            )
        )
        with patch.object(
            cli_module, "fetch_public_record", return_value=self.history
        ), patch.object(
            cli_module, "GitHubHistoryClient", return_value=client
        ):
            history, source = cli_module._require_promotion_history_barrier(
                {"history_locator": locator},
                policy=self.promotion_policy,
                expected_target_abi=TARGET_ABI,
                transport=SimpleNamespace(),
            )

        self.assertIs(history, self.history)
        self.assertEqual(source, self.tap_plan["tap_source"])

        unprotected = copy.deepcopy(self.history_snapshot)
        unprotected["direct"] = None
        client.protection_snapshot = lambda branch, phase: unprotected
        with patch.object(
            cli_module, "fetch_public_record", return_value=self.history
        ), patch.object(
            cli_module, "GitHubHistoryClient", return_value=client
        ), self.assertRaises(PromotionError):
            cli_module._require_promotion_history_barrier(
                {"history_locator": locator},
                policy=self.promotion_policy,
                expected_target_abi=TARGET_ABI,
                transport=SimpleNamespace(),
            )

    def test_current_policy_and_guard_identity_are_exact(self) -> None:
        for field in ("policy_version", "guard_registry_version"):
            expected = copy.deepcopy(self.expected_request_policy)
            expected[field] += 1
            with self.subTest(field=field), self.assertRaises(PromotionError):
                self._evaluate(expected_request_policy=expected)

    def test_promotion_decision_requires_one_exact_formula_subject(self) -> None:
        decision = asdict(self._evaluate())
        for subject in (
            '{"kind":"formula"}',
            '{"architecture":"wasm32","identity":"bash","kind":"product"}',
            '{"architecture":"native","identity":"bash","kind":"formula"}',
        ):
            changed = copy.deepcopy(decision)
            changed["formula_subject"] = subject
            with self.subTest(subject=subject), self.assertRaises(PromotionError):
                validate_promotion_decision(changed)

    def test_promotion_decision_carries_one_canonical_runtime_claim(self) -> None:
        decision = asdict(self._evaluate())
        claim = {
            "runtime_support": ["node", "browser"],
            "browser_compatible": True,
            "evidence": [
                {
                    "host": "node",
                    "product_id": "browser-main-shell",
                    "definition_id": "main-shell-toolchain-node",
                    "definition_sha256": "a" * 64,
                    "product_evidence_sha256": "c" * 64,
                },
                {
                    "host": "browser",
                    "product_id": "browser-main-shell",
                    "definition_id": "main-shell-toolchain-browser",
                    "definition_sha256": "b" * 64,
                    "product_evidence_sha256": "c" * 64,
                },
            ],
        }
        decision["runtime_claim"] = claim
        validate_promotion_decision(decision)
        for mutation in (
            {**claim, "runtime_support": ["browser", "node"]},
            {**claim, "browser_compatible": False},
            {**claim, "evidence": list(reversed(claim["evidence"]))},
        ):
            changed = copy.deepcopy(decision)
            changed["runtime_claim"] = mutation
            with self.subTest(mutation=mutation), self.assertRaises(PromotionError):
                validate_promotion_decision(changed)

    def test_policy_covered_formula_requires_exact_product_evidence(self) -> None:
        rule = replace(
            self.promotion_policy.runtime_claims[0],
            formulae=(FORMULA,),
        )
        policy = replace(self.promotion_policy, runtime_claims=(rule,))
        authority = ProductEvidenceAuthorityV1(
            request_digest=self.request_digest,
            source=copy.deepcopy(self.request["build_source"]),
            target_abi=TARGET_ABI,
            product_id=rule.product_id,
            record_sha256="c" * 64,
            outcome="success",
            promotion_state="eligible",
            resolved_formula_layers=(
                {
                    "id": f"homebrew-{FORMULA}",
                    "artifact": copy.deepcopy(
                        self.candidate_record["candidate"]["bottle_layer"]
                    ),
                },
            ),
            requirements=tuple(
                {
                    "host": requirement.host,
                    "id": requirement.definition_id,
                    "definition_sha256": character * 64,
                }
                for requirement, character in zip(
                    rule.requirements, ("a", "b"), strict=True
                )
            ),
        )

        decision = self._evaluate(
            policy=policy,
            product_evidence=(authority,),
        )
        self.assertEqual(decision.eligibility, "eligible")
        self.assertEqual(decision.runtime_claim["runtime_support"], ["node", "browser"])
        self.assertEqual(
            [item["definition_id"] for item in decision.runtime_claim["evidence"]],
            [item.definition_id for item in rule.requirements],
        )

        missing = self._evaluate(policy=policy, product_evidence=())
        self.assertEqual(missing.eligibility, "ineligible")
        self.assertIsNone(missing.runtime_claim)
        wrong_layer = replace(
            authority,
            resolved_formula_layers=(
                {
                    "id": f"homebrew-{FORMULA}",
                    "artifact": {
                        **authority.resolved_formula_layers[0]["artifact"],
                        "sha256": "d" * 64,
                        "immutable_reference": (
                            "ghcr.io/kandelo-dev/mismatched@sha256:" + "d" * 64
                        ),
                    },
                },
            ),
        )
        mismatched = self._evaluate(
            policy=policy,
            product_evidence=(wrong_layer,),
        )
        self.assertEqual(mismatched.eligibility, "ineligible")
        self.assertIsNone(mismatched.runtime_claim)

    def test_canonical_manifest_wraps_the_unchanged_candidate_layer(self) -> None:
        decision = self._evaluate()
        plan = build_canonical_bottle_plan(
            decision,
            candidate=self.candidate,
            policy=self.promotion_policy,
        )
        validate_canonical_bottle_plan(
            plan,
            decision=decision,
            candidate=self.candidate,
            policy=self.promotion_policy,
        )
        candidate_manifest = self.candidate.manifest
        canonical_manifest = build_oci_manifest(plan)
        self.assertNotEqual(candidate_manifest, canonical_manifest)
        self.assertEqual(plan.artifact_type, CANONICAL_BOTTLE_MANIFEST_MEDIA_TYPE)
        self.assertEqual(
            [layer.role for layer in plan.layers],
            [
                "bottle-layer",
                "bottle-metadata",
                "vfs-composition-descriptor",
            ],
        )
        self.assertEqual(plan.layers[0].body, self.bottle_body)
        self.assertEqual(plan.layers[0].digest.removeprefix("sha256:"), self.bottle_sha256)
        candidate_metadata = next(
            layer for layer in self.candidate.layers if layer.role == "bottle-metadata"
        )
        self.assertEqual(plan.layers[1].body, candidate_metadata.body)
        self.assertEqual(plan.layers[1].digest, candidate_metadata.digest)
        candidate_descriptor = next(
            layer
            for layer in self.candidate.layers
            if layer.role == "vfs-composition-descriptor"
        )
        candidate_descriptor_value = json.loads(candidate_descriptor.body)
        canonical_descriptor_value = json.loads(plan.layers[2].body)
        candidate_descriptor_value["tree"]["transports"] = (
            canonical_descriptor_value["tree"]["transports"]
        )
        self.assertEqual(canonical_descriptor_value, candidate_descriptor_value)
        self.assertEqual(
            canonical_descriptor_value["dependencies"],
            ["kandelo-dev/tap-core/ncurses"],
        )
        self.assertIn(
            "/homebrew-tap-core-abi-8/bash/blobs/sha256:",
            canonical_descriptor_value["tree"]["transports"][0]["url"],
        )
        self.assertNotIn("-candidates/", plan.layers[2].body.decode())
        self.assertEqual(
            plan.layers[0].mount_from,
            self.candidate.repository.removeprefix("ghcr.io/"),
        )
        self.assertEqual(
            plan.layers[1].mount_from,
            self.candidate.repository.removeprefix("ghcr.io/"),
        )
        self.assertIsNone(plan.layers[2].mount_from)

    def test_canonical_destination_and_layer_cannot_be_changed_or_rebuilt(self) -> None:
        decision = self._evaluate()
        wrong_subject = replace(
            decision,
            formula_subject=exact_formula_subject("curl", ARCHITECTURE),
        )
        with self.assertRaises(PromotionError):
            build_canonical_bottle_plan(
                wrong_subject,
                candidate=self.candidate,
                policy=self.promotion_policy,
            )
        with self.assertRaises(PromotionError):
            build_canonical_bottle_plan(
                decision,
                candidate=self.candidate,
                policy=self.promotion_policy,
                destination_repository=self.candidate.repository.removeprefix("ghcr.io/"),
            )
        plan = build_canonical_bottle_plan(
            decision,
            candidate=self.candidate,
            policy=self.promotion_policy,
        )
        changed = replace(
            plan,
            layers=(replace(plan.layers[0], body=b"rebuilt bottle bytes\n"),),
        )
        with self.assertRaises(PromotionError):
            validate_canonical_bottle_plan(
                changed,
                decision=decision,
                candidate=self.candidate,
                policy=self.promotion_policy,
            )

    def test_canonical_publication_uses_only_digest_tag_and_public_readback(self) -> None:
        decision = self._evaluate()
        plan = build_canonical_bottle_plan(
            decision,
            candidate=self.candidate,
            policy=self.promotion_policy,
        )
        transport = FakeRegistryTransport()
        layer = plan.layers[0]
        assert layer.mount_from is not None
        transport.blobs[(layer.mount_from, layer.digest)] = layer.body
        publication = publish_canonical_bottle(
            plan,
            decision=decision,
            candidate=self.candidate,
            policy=self.promotion_policy,
            transport=transport,
        )
        tags = [
            reference
            for repository, reference in transport.manifests
            if repository == plan.repository
        ]
        self.assertEqual(
            [tag for tag in tags if not tag.startswith("sha256:")],
            ["canonical-sha256-" + publication.locator.digest.removeprefix("sha256:")],
        )
        self.assertEqual(
            publication.artifact["sha256"],
            publication.locator.digest.removeprefix("sha256:"),
        )

        with self.assertRaises(OciPublicationError):
            publish_immutable_oci_plan(
                plan,
                transport=transport,
                expected_source_repository=SOURCE_ASSOCIATION,
                tag_prefix="latest",
            )

        private = FakeRegistryTransport()
        private.blobs[(layer.mount_from, layer.digest)] = layer.body
        private.private_anonymous = True
        with self.assertRaises(OciPublicationError):
            publish_canonical_bottle(
                plan,
                decision=decision,
                candidate=self.candidate,
                policy=self.promotion_policy,
                transport=private,
            )

        expected = expected_canonical_publication(
            decision, candidate=self.candidate, policy=self.promotion_policy
        )
        self.assertEqual(expected, publication)
        self.assertEqual(
            read_canonical_publication(
                decision,
                candidate=self.candidate,
                policy=self.promotion_policy,
                transport=transport,
            ),
            publication,
        )

    def test_admission_waits_for_exact_metadata_commit_and_readback(self) -> None:
        decision = self._evaluate()
        plan = build_canonical_bottle_plan(
            decision,
            candidate=self.candidate,
            policy=self.promotion_policy,
        )
        transport = FakeRegistryTransport()
        layer = plan.layers[0]
        assert layer.mount_from is not None
        transport.blobs[(layer.mount_from, layer.digest)] = layer.body
        publication = publish_canonical_bottle(
            plan,
            decision=decision,
            candidate=self.candidate,
            policy=self.promotion_policy,
            transport=transport,
        )
        prepared = prepare_admission(
            decision,
            candidate=self.candidate,
            canonical_publication=publication,
            preactivation_tap_source=self.tap_plan["tap_source"],
            abi_history_record_sha256=self.history.digest.removeprefix("sha256:"),
            policy=self.promotion_policy,
        )
        forged_publication = replace(
            publication,
            locator=replace(
                publication.locator,
                anonymous_readback_sha256="0" * 64,
            ),
        )
        with self.assertRaises(PromotionError):
            prepare_admission(
                decision,
                candidate=self.candidate,
                canonical_publication=forged_publication,
                preactivation_tap_source=self.tap_plan["tap_source"],
                abi_history_record_sha256=(
                    self.history.digest.removeprefix("sha256:")
                ),
                policy=self.promotion_policy,
            )
        with self.assertRaises(PromotionError):
            finalize_admission_record(
                prepared,
                formula_metadata_base_source=None,
                formula_metadata_source=None,
                formula_metadata_update=None,
                post_write_readback=None,
                run=OVERRIDE_RUN,
            )

        update = {
            "formula": FORMULA,
            "architecture": ARCHITECTURE,
            "expected_main_commit": "6" * 40,
            "expected_normalized_formula_sha256": self.formula_plan["identity"][
                "normalized_formula_sha256"
            ],
            "expected_generated_metadata_sha256": "e" * 64,
            "allowed_paths": [
                "Formula/bash.rb",
                "Kandelo/formula/bash.json",
                "Kandelo/metadata.json",
                "Kandelo/link/bash-1.0-rebuild1-wasm32.json",
            ],
            "link_manifest_path": "Kandelo/link/bash-1.0-rebuild1-wasm32.json",
            "link_manifest_sha256": "f" * 64,
            "canonical_manifest_digest": publication.artifact["sha256"],
            "bottle_layer_sha256": self.bottle_sha256,
            "bottle_layer_bytes": len(self.bottle_body),
            "target_abi": TARGET_ABI,
        }
        metadata_source = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": NEXT_TAP_COMMIT,
            "tree": NEXT_TAP_TREE,
        }
        admission = finalize_admission_record(
            prepared,
            formula_metadata_base_source={
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": "6" * 40,
                "tree": "5" * 40,
            },
            formula_metadata_source=metadata_source,
            formula_metadata_update=update,
            post_write_readback={
                "source": metadata_source,
                "formula_metadata_update": update,
            },
            run=OVERRIDE_RUN,
        )
        validate_admission_record(admission)
        self.assertEqual(
            admission["admission"]["original_producer"],
            self.candidate_record["candidate"]["producer"],
        )
        self.assertEqual(
            admission["admission"]["merged_pull_request"]["merge_commit"],
            MERGE_COMMIT,
        )
        self.assertEqual(
            admission["admission"]["preactivation_tap_source"],
            self.tap_plan["tap_source"],
        )
        self.assertEqual(
            admission["admission"]["tap_source"]["commit"], "6" * 40
        )
        self.assertEqual(
            admission["admission"]["formula_metadata_source"]["commit"],
            NEXT_TAP_COMMIT,
        )
        admission_plan = build_admission_oci_plan(
            admission, policy=self.promotion_policy
        )
        self.assertEqual(admission_plan.artifact_type, ADMISSION_RECORD_MEDIA_TYPE)
        self.assertTrue(admission_plan.repository.endswith("/bash/admissions"))
        admission_locator = publish_admission_record(
            admission,
            policy=self.promotion_policy,
            transport=transport,
        )
        self.assertTrue(admission_locator.immutable_reference.startswith("ghcr.io/"))
        self.assertEqual(
            [
                reference
                for repository, reference in transport.manifests
                if repository == admission_plan.repository
                and not reference.startswith("sha256:")
            ],
            ["record-sha256-" + admission_locator.digest.removeprefix("sha256:")],
        )

    def test_metadata_patch_handoff_is_canonical_bounded_and_exact(self) -> None:
        patch = TapMetadataPatchV1(
            operation="successor-activation",
            expected_main_commit="a" * 40,
            expected_main_tree="b" * 40,
            allowed_paths=("Kandelo/abi-state.json",),
            expected_files_sha256={"Kandelo/abi-state.json": "c" * 64},
            files={"Kandelo/abi-state.json": b'{"schema":1}\n'},
        )
        document = metadata_patch_document(patch, formula_update=None)
        loaded, update = load_metadata_patch_document(canonical_bytes(document))
        self.assertEqual(update, None)
        self.assertEqual(loaded.operation, patch.operation)
        self.assertEqual(dict(loaded.files), dict(patch.files))
        self.assertEqual(
            dict(loaded.expected_files_sha256), dict(patch.expected_files_sha256)
        )

        changed = copy.deepcopy(document)
        changed["files"][0]["base64"] = "e30K"
        with self.assertRaises(PromotionError):
            load_metadata_patch_document(canonical_bytes(changed))

    def test_tap_metadata_only_or_unrelated_drift_retains_eligibility(self) -> None:
        drifted_source = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": NEXT_TAP_COMMIT,
            "tree": NEXT_TAP_TREE,
        }
        decision = self._evaluate(current_tap_source=drifted_source)
        self.assertEqual(decision.tap_source_state, "drift")
        self.assertEqual(decision.eligibility, "eligible")

    def test_formula_source_install_dependency_patch_or_support_drift_rebuilds(self) -> None:
        mutations = (
            ("install", ("identity", "normalized_formula_sha256")),
            ("source", ("capture", "normalized_source_sha256")),
            ("patch", ("capture", "sources")),
            ("support", ("capture", "tap_input_components")),
        )
        for label, path in mutations:
            current = copy.deepcopy(self.formula_plan)
            if path[-1] in {"sources", "tap_input_components"}:
                current[path[0]][path[1]][0]["sha256"] = "f" * 64
            else:
                current[path[0]][path[1]] = "f" * 64
            with self.subTest(label=label):
                decision = self._evaluate(
                    current_tap_source={
                        "repository": "kandelo-dev/homebrew-tap-core",
                        "commit": NEXT_TAP_COMMIT,
                        "tree": NEXT_TAP_TREE,
                    },
                    current_formula=current,
                )
                self.assertEqual(decision.tap_source_state, "rebuild-required")
                self.assertEqual(decision.eligibility, "rebuild-required")

        dependency_definition = copy.deepcopy(self.formula_plan)
        dependency_definition["direct_dependencies"][0][
            "materialization_policy_sha256"
        ] = "f" * 64
        decision = self._evaluate(
            current_tap_source={
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": NEXT_TAP_COMMIT,
                "tree": NEXT_TAP_TREE,
            },
            current_formula=dependency_definition,
        )
        self.assertEqual(decision.eligibility, "rebuild-required")

    def test_reverse_dependant_only_rebuilds_when_dependency_layer_changes(self) -> None:
        same = self._evaluate(
            current_tap_source={
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": NEXT_TAP_COMMIT,
                "tree": NEXT_TAP_TREE,
            },
            current_dependency_layers=self._current_dependencies(),
        )
        self.assertEqual(same.eligibility, "eligible")

        promoted_reference = self._current_dependencies()
        dependency = promoted_reference[
            exact_formula_subject("ncurses", ARCHITECTURE)
        ]
        promoted_reference[exact_formula_subject("ncurses", ARCHITECTURE)] = {
            **dependency,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8/ncurses@sha256:"
                + dependency["sha256"]
            ),
        }
        promoted = self._evaluate(
            current_tap_source={
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": NEXT_TAP_COMMIT,
                "tree": NEXT_TAP_TREE,
            },
            current_dependency_layers=promoted_reference,
        )
        self.assertEqual(promoted.eligibility, "eligible")

        changed = self._current_dependencies()
        changed[exact_formula_subject("ncurses", ARCHITECTURE)] = _artifact(
            b"rebuilt changed ncurses bytes\n",
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8/ncurses",
        )
        decision = self._evaluate(
            current_tap_source={
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": NEXT_TAP_COMMIT,
                "tree": NEXT_TAP_TREE,
            },
            current_dependency_layers=changed,
        )
        self.assertEqual(decision.tap_source_state, "rebuild-required")
        self.assertEqual(decision.eligibility, "rebuild-required")

    def test_checked_promotion_and_admission_fixtures_are_canonical(self) -> None:
        promotion = json.loads(PROMOTION_FIXTURE.read_bytes())
        admission = json.loads(ADMISSION_FIXTURE.read_bytes())
        self.assertEqual(PROMOTION_FIXTURE.read_bytes(), canonical_bytes(promotion))
        self.assertEqual(ADMISSION_FIXTURE.read_bytes(), canonical_bytes(admission))
        validate_promotion_decision(promotion)
        validate_admission_record(admission)
        self.assertEqual(
            cli_main(["fixture-check", "--fixture", str(PROMOTION_FIXTURE)]), 0
        )
        self.assertEqual(
            cli_main(["fixture-check", "--fixture", str(ADMISSION_FIXTURE)]), 0
        )

    def test_cli_reports_an_invalid_protected_promotion_fixture(self) -> None:
        root = self.root / "protected-tap"
        fixture_root = root / "Kandelo/staging/fixtures"
        shutil.copytree(TAP_ROOT / "Kandelo/staging/fixtures", fixture_root)
        fixture = fixture_root / "promotion-decision.json"
        changed = json.loads(fixture.read_bytes())
        changed["formula_subject"] = '{"kind":"formula"}'
        fixture.write_bytes(canonical_bytes(changed))
        with patch.object(cli_module, "TAP_ROOT", root):
            self.assertEqual(
                cli_main(["fixture-check", "--fixture", str(fixture)]),
                1,
            )


if __name__ == "__main__":
    unittest.main()
