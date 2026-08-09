from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.abi_staging.inventory import (
    InventoryError,
    inspect_candidate_reuse_repository,
    inspect_verification_repository,
    scan_attempt_repository,
    scan_candidate_repository,
    scan_verification_repository,
    scan_scheduling_inventory,
)
from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.contract import (
    load_bottle_contract,
    make_candidate_reuse_record,
)
from scripts.abi_staging.oci import build_oci_manifest, publish_record
from scripts.abi_staging.records import (
    OciBlobV1,
    OciRecordPlanV1,
    build_attempt_outcome_oci_plan,
    build_attempt_outcome_record,
    build_candidate_oci_plan,
    build_candidate_reuse_oci_plan,
    build_source_custody_oci_plan,
)
from scripts.abi_staging.policy import (
    load_tap_staging_policy,
    load_verification_tests,
)
from scripts.abi_staging.verification import (
    VERIFICATION_RECEIPT_MEDIA_TYPE,
    VERIFICATION_RESULT_MEDIA_TYPE,
    receipt_repository,
)
from scripts.abi_staging.tests.test_oci import FakeRegistryTransport
from scripts.abi_staging.tests.test_records import _write_handoff


TAP_ROOT = Path(__file__).resolve().parents[3]
PLAN = json.loads((TAP_ROOT / "Kandelo/staging/fixtures/tap-plan.json").read_bytes())
SOURCE_REPOSITORY = "kandelo-dev/homebrew-tap-core-abi-8-source-custody"
CANDIDATE_REPOSITORY = (
    "kandelo-dev/homebrew-tap-core-abi-8-candidates/mini-tool"
)


class PublicInventoryTests(unittest.TestCase):
    def test_empty_public_namespaces_form_one_complete_scheduler_inventory(self) -> None:
        transport = FakeRegistryTransport()
        inventory = scan_scheduling_inventory(
            PLAN,
            policy=load_tap_staging_policy(
                TAP_ROOT / "Kandelo/staging/tap-policy.toml"
            ),
            verification_tests=load_verification_tests(
                TAP_ROOT / "Kandelo/staging/verification-tests.toml"
            ),
            transport=transport,
        )
        self.assertEqual(inventory.records.attempts, ())
        self.assertEqual(inventory.records.candidates, ())
        self.assertEqual(inventory.records.verifications, ())
        self.assertEqual(inventory.candidate_locators, {})

    def test_verification_inventory_binds_candidate_subject_and_completion_clock(self) -> None:
        candidate_record = "c" * 64
        request = "a" * 64
        subject = '{"architecture":"wasm32","identity":"mini-tool","kind":"formula"}'
        test_definition = "d" * 64
        layer = {
            "sha256": "e" * 64,
            "bytes": 73,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                "mini-tool@sha256:" + "e" * 64
            ),
        }
        receipt = {
            "schema": 1,
            "kind": "kandelo-abi-staging-verification",
            "common": {
                "request_sha256": request,
                "subject": {"kind": "candidate", "identity": candidate_record},
                "source": {
                    "repository": "Automattic/kandelo",
                    "commit": "1" * 40,
                    "tree": "2" * 40,
                },
                "run": {
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "workflow_ref": ".github/workflows/abi-staging-reconcile.yml@refs/heads/main",
                    "run_id": 404,
                    "run_attempt": 1,
                    "job": "verify-candidate",
                },
                "guard_codes": [],
                "work_state": "complete",
                "outcome": "success",
                "artifact_class": "none",
                "promotion_state": "eligible",
                "retry_state": {
                    "attempts": 1,
                    "eligible": False,
                    "exhausted": False,
                    "next_action": "none",
                },
                "blockers": [],
            },
            "verification": {
                "candidate_record_sha256": candidate_record,
                "candidate_layer": layer,
                "test_definition_sha256": test_definition,
                "host": "build",
                "attempt_ordinal": 0,
                "diagnostics": [],
            },
        }
        repository = receipt_repository(
            CANDIDATE_REPOSITORY, "bottle-structure", "build"
        )
        plan = OciRecordPlanV1(
            repository=repository,
            artifact_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
            config=OciBlobV1(
                role="verification-receipt",
                media_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
                body=canonical_bytes(receipt),
                title="verification-receipt.json",
            ),
            layers=(
                OciBlobV1(
                    role="verification-result",
                    media_type=VERIFICATION_RESULT_MEDIA_TYPE,
                    body=b"{}\n",
                    title="verification-result.json",
                ),
            ),
            annotations={
                "dev.kandelo.abi-staging.candidate-record-sha256": candidate_record,
                "dev.kandelo.abi-staging.classification": "factual-verification-receipt",
                "dev.kandelo.abi-staging.completed-at": "2026-08-09T10:00:00.000Z",
                "dev.kandelo.abi-staging.host": "build",
                "dev.kandelo.abi-staging.kind": "verification-receipt",
                "dev.kandelo.abi-staging.outcome": "success",
                "dev.kandelo.abi-staging.test-definition-sha256": test_definition,
                "org.opencontainers.image.source": "https://github.com/kandelo-dev/homebrew-tap-core",
            },
        )
        transport = FakeRegistryTransport()
        published = publish_record(
            plan,
            transport=transport,
            expected_source_repository="kandelo-dev/homebrew-tap-core",
        )
        facts = scan_verification_repository(
            repository,
            candidates={
                candidate_record: {
                    "request_sha256": request,
                    "subject": subject,
                    "bottle_layer": layer,
                }
            },
            transport=transport,
        )
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].subject, subject)
        self.assertEqual(facts[0].completed_at, "2026-08-09T10:00:00.000Z")
        self.assertEqual(facts[0].record_sha256, published.digest.removeprefix("sha256:"))

        manifest = json.loads(transport.manifests[(repository, published.digest)])
        manifest["annotations"]["dev.kandelo.abi-staging.completed-at"] = (
            "2026-08-09T10:00:01.000Z"
        )
        transport.manifests[(repository, published.digest)] = canonical_bytes(manifest)
        with self.assertRaises(InventoryError):
            scan_verification_repository(
                repository,
                candidates={
                    candidate_record: {
                        "request_sha256": request,
                        "subject": subject,
                        "bottle_layer": layer,
                    }
                },
                transport=transport,
            )

    def test_attempt_inventory_preserves_exact_retry_clock(self) -> None:
        repository = CANDIDATE_REPOSITORY + "/attempts"
        record = build_attempt_outcome_record(
            request_sha256="a" * 64,
            subject='{"architecture":"wasm32","identity":"mini-tool","kind":"formula"}',
            contract_sha256="b" * 64,
            retry_ordinal=1,
            outcome="timeout",
            guard_code="build_timeout",
            completed_at="2026-08-09T10:00:00.000Z",
            run={
                "repository": "kandelo-dev/homebrew-tap-core",
                "workflow_ref": ".github/workflows/abi-staging-reconcile.yml@refs/heads/main",
                "run_id": 303,
                "run_attempt": 1,
                "job": "publish-candidate",
            },
            handoff=None,
            candidate_record_sha256=None,
        )
        transport = FakeRegistryTransport()
        published = publish_record(
            build_attempt_outcome_oci_plan(record, repository=repository),
            transport=transport,
            expected_source_repository="kandelo-dev/homebrew-tap-core",
        )
        facts = scan_attempt_repository(repository, transport=transport)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].retry_ordinal, 1)
        self.assertEqual(facts[0].guard_code, "build_timeout")
        self.assertEqual(facts[0].completed_at, "2026-08-09T10:00:00.000Z")
        self.assertEqual(facts[0].record_sha256, published.digest.removeprefix("sha256:"))

    def test_candidate_inventory_reconstructs_exact_scheduler_fact_and_locator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            handoff = Path(temporary) / "handoff"
            handoff.mkdir()
            _write_handoff(handoff)
            source = build_source_custody_oci_plan(
                handoff / "source-custody", repository=SOURCE_REPOSITORY
            )
            source_manifest = build_oci_manifest(source)
            source_digest = hashlib.sha256(source_manifest).hexdigest()
            candidate = build_candidate_oci_plan(
                handoff,
                repository=CANDIDATE_REPOSITORY,
                source_record={
                    "repository": "ghcr.io/" + SOURCE_REPOSITORY,
                    "digest": "sha256:" + source_digest,
                    "immutable_reference": (
                        f"ghcr.io/{SOURCE_REPOSITORY}@sha256:{source_digest}"
                    ),
                },
                source_manifest_bytes=source_manifest,
                publication_run={
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "workflow_ref": (
                        ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
                    ),
                    "run_id": 202,
                    "run_attempt": 1,
                    "job": "publish-candidate",
                },
            )
            transport = FakeRegistryTransport()
            published = publish_record(
                candidate,
                transport=transport,
                expected_source_repository="kandelo-dev/homebrew-tap-core",
            )
            before_scan = len(transport.calls)
            facts, locators = scan_candidate_repository(
                CANDIDATE_REPOSITORY, transport=transport
            )
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0].record_sha256, published.digest.removeprefix("sha256:"))
            self.assertEqual(facts[0].request_sha256, "a" * 64)
            self.assertEqual(facts[0].subject, '{"architecture":"wasm32","identity":"mini-tool","kind":"formula"}')
            self.assertEqual(locators[facts[0].record_sha256]["digest"], published.digest)
            scan_calls = transport.calls[before_scan:]
            candidate_record = json.loads(candidate.config.body)
            layer_sha256 = candidate_record["candidate"]["bottle_layer"]["sha256"]
            self.assertFalse(
                any(layer_sha256 in call[1] for call in scan_calls)
            )

            transport.manifests[(CANDIDATE_REPOSITORY, "latest")] = build_oci_manifest(candidate)
            with self.assertRaises(InventoryError):
                scan_candidate_repository(CANDIDATE_REPOSITORY, transport=transport)

    def test_reuse_inventory_binds_new_request_to_original_candidate_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            handoff = Path(temporary) / "handoff"
            handoff.mkdir()
            _write_handoff(handoff)
            source = build_source_custody_oci_plan(
                handoff / "source-custody", repository=SOURCE_REPOSITORY
            )
            source_manifest = build_oci_manifest(source)
            source_digest = hashlib.sha256(source_manifest).hexdigest()
            candidate_plan = build_candidate_oci_plan(
                handoff,
                repository=CANDIDATE_REPOSITORY,
                source_record={
                    "repository": "ghcr.io/" + SOURCE_REPOSITORY,
                    "digest": "sha256:" + source_digest,
                    "immutable_reference": (
                        f"ghcr.io/{SOURCE_REPOSITORY}@sha256:{source_digest}"
                    ),
                },
                source_manifest_bytes=source_manifest,
                publication_run={
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "workflow_ref": ".github/workflows/abi-staging-reconcile.yml@refs/heads/main",
                    "run_id": 202,
                    "run_attempt": 1,
                    "job": "publish-candidate",
                },
            )
            transport = FakeRegistryTransport()
            candidate_locator = publish_record(
                candidate_plan,
                transport=transport,
                expected_source_repository="kandelo-dev/homebrew-tap-core",
            )
            candidate = json.loads(candidate_plan.config.body)
            candidate_digest = candidate_locator.digest.removeprefix("sha256:")
            receipt_repository_name = receipt_repository(
                CANDIDATE_REPOSITORY, "bottle-structure", "build"
            )
            receipt = {
                "schema": 1,
                "kind": "kandelo-abi-staging-verification",
                "common": {
                    "request_sha256": candidate["common"]["request_sha256"],
                    "subject": {"kind": "candidate", "identity": candidate_digest},
                    "source": candidate["common"]["source"],
                    "run": {
                        "repository": "kandelo-dev/homebrew-tap-core",
                        "workflow_ref": ".github/workflows/abi-staging-reconcile.yml@refs/heads/main",
                        "run_id": 303,
                        "run_attempt": 1,
                        "job": "verify-candidate",
                    },
                    "guard_codes": [],
                    "work_state": "complete",
                    "outcome": "success",
                    "artifact_class": "none",
                    "promotion_state": "eligible",
                    "retry_state": {
                        "attempts": 1,
                        "eligible": False,
                        "exhausted": False,
                        "next_action": "none",
                    },
                    "blockers": [],
                },
                "verification": {
                    "candidate_record_sha256": candidate_digest,
                    "candidate_layer": candidate["candidate"]["bottle_layer"],
                    "test_definition_sha256": "d" * 64,
                    "host": "build",
                    "attempt_ordinal": 0,
                    "diagnostics": [],
                },
            }
            receipt_plan = OciRecordPlanV1(
                repository=receipt_repository_name,
                artifact_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
                config=OciBlobV1(
                    role="verification-receipt",
                    media_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
                    body=canonical_bytes(receipt),
                    title="verification-receipt.json",
                ),
                layers=(
                    OciBlobV1(
                        role="verification-result",
                        media_type=VERIFICATION_RESULT_MEDIA_TYPE,
                        body=b"{}\n",
                        title="verification-result.json",
                    ),
                ),
                annotations={
                    "dev.kandelo.abi-staging.candidate-record-sha256": candidate_digest,
                    "dev.kandelo.abi-staging.classification": "factual-verification-receipt",
                    "dev.kandelo.abi-staging.completed-at": "2026-08-09T10:00:00.000Z",
                    "dev.kandelo.abi-staging.host": "build",
                    "dev.kandelo.abi-staging.kind": "verification-receipt",
                    "dev.kandelo.abi-staging.outcome": "success",
                    "dev.kandelo.abi-staging.test-definition-sha256": "d" * 64,
                    "org.opencontainers.image.source": "https://github.com/kandelo-dev/homebrew-tap-core",
                },
            )
            receipt_locator = publish_record(
                receipt_plan,
                transport=transport,
                expected_source_repository="kandelo-dev/homebrew-tap-core",
            )
            receipt_inventory = inspect_verification_repository(
                receipt_repository_name,
                candidates={
                    candidate_digest: {
                        "request_sha256": candidate["common"]["request_sha256"],
                        "subject": '{"architecture":"wasm32","identity":"mini-tool","kind":"formula"}',
                        "bottle_layer": candidate["candidate"]["bottle_layer"],
                    }
                },
                transport=transport,
            )
            source_component = next(
                item["artifact"]
                for item in candidate["candidate"]["normalized_components"]
                if item["id"] == "source-custody"
            )
            existing = {
                "schema": 1,
                "kind": "kandelo-existing-candidate",
                "contract_sha256": candidate["candidate"]["formula"]["bottle_contract_sha256"],
                "formula": {
                    key: candidate["candidate"]["formula"][key]
                    for key in ("tap", "formula", "architecture", "target_abi")
                },
                "candidate_record": {
                    "record_sha256": candidate_digest,
                    "immutable_reference": candidate_locator.immutable_reference,
                },
                "source_custody": {
                    "record_sha256": source_component["sha256"],
                    "immutable_reference": source_component["immutable_reference"],
                },
                "bottle_layer": candidate["candidate"]["bottle_layer"],
                "qualifying_receipts": [
                    {
                        "record_sha256": receipt_locator.digest.removeprefix("sha256:"),
                        "immutable_reference": receipt_locator.immutable_reference,
                    }
                ],
                "original_producer": candidate["candidate"]["producer"],
                "nonendorsed": True,
            }
            reuse_record = make_candidate_reuse_record(
                load_bottle_contract((handoff / "bottle-contract.json").read_bytes()),
                '{"architecture":"wasm32","identity":"mini-tool","kind":"formula"}',
                existing,
                {
                    "request_sha256": "f" * 64,
                    "source": {
                        "repository": "Automattic/kandelo",
                        "commit": "8" * 40,
                        "tree": "9" * 40,
                    },
                    "run": {
                        "repository": "kandelo-dev/homebrew-tap-core",
                        "workflow_ref": ".github/workflows/abi-staging-reconcile.yml@refs/heads/main",
                        "run_id": 404,
                        "run_attempt": 1,
                        "job": "publish-reuse",
                    },
                },
            )
            reuse_repository_name = CANDIDATE_REPOSITORY + "/reuse"
            reuse_locator = publish_record(
                build_candidate_reuse_oci_plan(
                    reuse_record, repository=reuse_repository_name
                ),
                transport=transport,
                expected_source_repository="kandelo-dev/homebrew-tap-core",
            )
            reuse_inventory = inspect_candidate_reuse_repository(
                reuse_repository_name,
                candidates={candidate_digest: candidate},
                candidate_locators={
                    candidate_digest: {
                        "repository": candidate_locator.repository,
                        "digest": candidate_locator.digest,
                        "immutable_reference": candidate_locator.immutable_reference,
                    }
                },
                verifications={
                    fact.record_sha256: fact for fact in receipt_inventory.facts
                },
                verification_locators=receipt_inventory.locators,
                transport=transport,
            )
            self.assertEqual(len(reuse_inventory.facts), 1)
            fact = reuse_inventory.facts[0]
            self.assertEqual(fact.request_sha256, "f" * 64)
            self.assertEqual(fact.record_sha256, candidate_digest)
            self.assertEqual(
                fact.binding_record_sha256,
                reuse_locator.digest.removeprefix("sha256:"),
            )


if __name__ == "__main__":
    unittest.main()
