from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.cli import _local_locator, _publication_result
from scripts.abi_staging.custody import source_capsule_digest
from scripts.abi_staging.oci import build_oci_manifest
from scripts.abi_staging.records import (
    BOTTLE_CONTRACT_MEDIA_TYPE,
    BOTTLE_LAYER_MEDIA_TYPE,
    BOTTLE_METADATA_MEDIA_TYPE,
    CANDIDATE_RECORD_MEDIA_TYPE,
    ATTEMPT_OUTCOME_MEDIA_TYPE,
    OCI_MANIFEST_MEDIA_TYPE,
    SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
    TapRecordError,
    build_candidate_oci_plan,
    build_attempt_outcome_oci_plan,
    build_attempt_outcome_record,
    build_source_custody_oci_plan,
    validate_candidate_record,
    validate_attempt_outcome_record,
)


TAP_ROOT = Path(__file__).resolve().parents[3]
REQUEST = "a" * 64
SUBJECT = '{"architecture":"wasm32","identity":"mini-tool","kind":"formula"}'
CANDIDATE_REPOSITORY = (
    "kandelo-dev/homebrew-tap-core-abi-8-candidates/mini-tool"
)
SOURCE_REPOSITORY = "kandelo-dev/homebrew-tap-core-abi-8-source-custody"


def _artifact(body: bytes) -> dict[str, object]:
    return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def _write_custody(root: Path) -> None:
    root.mkdir()
    (root / "submodules").mkdir()
    bodies = {
        "kandelo.bundle": b"exact kandelo bundle\n",
        "kandelo-tree.tar": b"exact kandelo tree\n",
        "tap.bundle": b"exact tap bundle\n",
        "tap-tree.tar": b"exact tap tree\n",
    }
    for relative, body in bodies.items():
        (root / relative).write_bytes(body)
    manifest: dict[str, object] = {
        "schema": 1,
        "kind": "kandelo-source-custody-manifest",
        "request_sha256": REQUEST,
        "subject": SUBJECT,
        "sources": [
            {
                "role": "kandelo",
                "repository": "Automattic/kandelo",
                "commit": "1" * 40,
                "tree": "2" * 40,
                "bundle": {"path": "kandelo.bundle", **_artifact(bodies["kandelo.bundle"])},
                "tree_archive": {
                    "path": "kandelo-tree.tar",
                    **_artifact(bodies["kandelo-tree.tar"]),
                },
            },
            {
                "role": "tap",
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": "3" * 40,
                "tree": "4" * 40,
                "bundle": {"path": "tap.bundle", **_artifact(bodies["tap.bundle"])},
                "tree_archive": {
                    "path": "tap-tree.tar",
                    **_artifact(bodies["tap-tree.tar"]),
                },
            },
        ],
        "submodules": [],
        "capsule_sha256": "0" * 64,
    }
    manifest["capsule_sha256"] = source_capsule_digest(manifest)
    (root / "manifest.json").write_bytes(canonical_bytes(manifest))


def _write_handoff(root: Path) -> None:
    contract = (TAP_ROOT / "Kandelo/staging/fixtures/bottle-contract.json").read_bytes()
    bottle = b"candidate bottle layer\n"
    metadata = canonical_bytes(
        {"architecture": "wasm32", "formula": "mini-tool", "nonendorsed": True}
    )
    (root / "bottle-contract.json").write_bytes(contract)
    (root / "bottle.tar.gz").write_bytes(bottle)
    (root / "bottle-metadata.json").write_bytes(metadata)
    attempt = {
        "schema": 1,
        "kind": "kandelo-abi-staging-attempt",
        "common": {
            "request_sha256": REQUEST,
            "subject": {
                "kind": "formula",
                "identity": "kandelo-dev/homebrew-tap-core/mini-tool",
                "architecture": "wasm32",
            },
            "source": {
                "repository": "Automattic/kandelo",
                "commit": "1" * 40,
                "tree": "2" * 40,
            },
            "run": {
                "repository": "kandelo-dev/homebrew-tap-core",
                "workflow_ref": ".github/workflows/staging.yml@refs/heads/main",
                "run_id": 101,
                "run_attempt": 1,
                "job": "build-candidate",
            },
            "guard_codes": [],
            "work_state": "complete",
            "outcome": "success",
            "artifact_class": "candidate",
            "artifact": {
                **_artifact(bottle),
                "immutable_reference": (
                    "handoff:bottle.tar.gz@sha256:" + hashlib.sha256(bottle).hexdigest()
                ),
            },
            "promotion_state": "eligible",
            "retry_state": {
                "attempts": 1,
                "eligible": False,
                "exhausted": False,
                "next_action": "none",
            },
            "blockers": [],
        },
        "attempt": {
            "formula": {
                "tap": "kandelo-dev/homebrew-tap-core",
                "formula": "mini-tool",
                "architecture": "wasm32",
                "target_abi": 8,
                "bottle_contract_sha256": hashlib.sha256(contract).hexdigest(),
            },
            "source_capsule": {
                "sha256": "5" * 64,
                "bytes": 1024,
                "immutable_reference": "handoff:source-custody@sha256:" + "5" * 64,
            },
            "build": {
                "runner_image": "uncredentialed-candidate",
                "command_sha256": "6" * 64,
                "result_sha256": "7" * 64,
                "diagnostics": [],
            },
            "retry_ordinal": 0,
            "candidate": {
                **_artifact(bottle),
                "immutable_reference": (
                    "handoff:bottle.tar.gz@sha256:" + hashlib.sha256(bottle).hexdigest()
                ),
            },
        },
    }
    (root / "attempt-record.json").write_bytes(canonical_bytes(attempt))
    _write_custody(root / "source-custody")


class CandidateRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.handoff = Path(self.temporary.name) / "handoff"
        self.handoff.mkdir()
        _write_handoff(self.handoff)
        self.publication_run = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "workflow_ref": ".github/workflows/staging.yml@refs/heads/main",
            "run_id": 202,
            "run_attempt": 1,
            "job": "publish-candidate",
        }

    def test_protected_attempt_outcome_carries_retry_clock_and_exact_work(self) -> None:
        record = build_attempt_outcome_record(
            request_sha256=REQUEST,
            subject=SUBJECT,
            contract_sha256="b" * 64,
            retry_ordinal=2,
            outcome="failure",
            guard_code="build_failed",
            completed_at="2026-08-09T10:00:00.000Z",
            run=self.publication_run,
            handoff={"sha256": "c" * 64, "bytes": 1024},
            candidate_record_sha256=None,
        )
        validate_attempt_outcome_record(record)
        plan = build_attempt_outcome_oci_plan(
            record, repository=CANDIDATE_REPOSITORY + "/attempts"
        )
        self.assertEqual(plan.artifact_type, ATTEMPT_OUTCOME_MEDIA_TYPE)
        self.assertEqual(
            plan.annotations["dev.kandelo.abi-staging.completed-at"],
            "2026-08-09T10:00:00.000Z",
        )
        changed = json.loads(canonical_bytes(record))
        changed["attempt"]["guard_code"] = None
        with self.assertRaises(TapRecordError):
            validate_attempt_outcome_record(changed)

    def _plans(self):
        source = build_source_custody_oci_plan(
            self.handoff / "source-custody", repository=SOURCE_REPOSITORY
        )
        source_manifest = build_oci_manifest(source)
        source_digest = hashlib.sha256(source_manifest).hexdigest()
        candidate = build_candidate_oci_plan(
            self.handoff,
            repository=CANDIDATE_REPOSITORY,
            source_record={
                "repository": "ghcr.io/" + SOURCE_REPOSITORY,
                "digest": "sha256:" + source_digest,
                "immutable_reference": (
                    f"ghcr.io/{SOURCE_REPOSITORY}@sha256:{source_digest}"
                ),
            },
            source_manifest_bytes=source_manifest,
            publication_run=self.publication_run,
        )
        return source, candidate

    def test_source_and_candidate_records_have_exact_media_and_descriptor_order(self) -> None:
        source, candidate = self._plans()
        self.assertEqual(source.config.media_type, SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE)
        self.assertEqual(
            [layer.role for layer in source.layers],
            [
                "kandelo-bundle",
                "kandelo-tree",
                "tap-bundle",
                "tap-tree",
            ],
        )
        self.assertEqual(candidate.config.media_type, CANDIDATE_RECORD_MEDIA_TYPE)
        self.assertEqual(
            [(layer.role, layer.media_type) for layer in candidate.layers],
            [
                ("bottle-layer", BOTTLE_LAYER_MEDIA_TYPE),
                ("bottle-metadata", BOTTLE_METADATA_MEDIA_TYPE),
                ("bottle-contract", BOTTLE_CONTRACT_MEDIA_TYPE),
                ("attempt-record", "application/vnd.kandelo.abi-staging.attempt.v1+json"),
                ("source-custody-record", OCI_MANIFEST_MEDIA_TYPE),
            ],
        )

    def test_candidate_is_factual_nonendorsed_and_separate_from_trust_decisions(self) -> None:
        source, candidate = self._plans()
        record = json.loads(candidate.config.body)
        validate_candidate_record(record)
        self.assertIs(record["candidate"]["nonendorsed"], True)
        self.assertEqual(
            candidate.annotations["dev.kandelo.abi-staging.classification"],
            "public-candidate-not-endorsed",
        )
        rendered = canonical_bytes(record)
        for forbidden in (
            b"verification",
            b"admission",
            b"candidate_record_digest",
            b"candidate_record_sha256",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(
            record["candidate"]["source_custody_sha256"],
            hashlib.sha256(build_oci_manifest(source)).hexdigest(),
        )

    def test_record_digest_and_locator_are_outside_the_hashed_candidate_bytes(self) -> None:
        _, candidate = self._plans()
        first = build_oci_manifest(candidate)
        second = build_oci_manifest(candidate)
        self.assertEqual(first, second)
        digest = hashlib.sha256(first).hexdigest()
        record = json.loads(candidate.config.body)
        self.assertNotIn(digest.encode(), candidate.config.body)
        self.assertNotIn("candidate_record_sha256", record)
        self.assertEqual(json.loads(first)["mediaType"], OCI_MANIFEST_MEDIA_TYPE)

    def test_candidate_record_rejects_embedded_identity_or_trust_fields(self) -> None:
        _, candidate = self._plans()
        record = json.loads(candidate.config.body)
        for field, value in (
            ("candidate_record_sha256", "f" * 64),
            ("verification", []),
            ("admission", None),
        ):
            with self.subTest(field=field):
                changed = json.loads(canonical_bytes(record))
                changed[field] = value
                with self.assertRaises(ValueError):
                    validate_candidate_record(changed)

    def test_observe_publication_is_a_plan_not_an_anonymous_readback_claim(self) -> None:
        source, candidate = self._plans()
        source_locator = _local_locator(
            SOURCE_REPOSITORY, build_oci_manifest(source)
        )
        candidate_locator = _local_locator(
            CANDIDATE_REPOSITORY, build_oci_manifest(candidate)
        )
        result = _publication_result(
            mode="observe",
            candidate_plan=candidate,
            planned_source_locator=source_locator,
            planned_candidate_locator=candidate_locator,
            published_source_locator=None,
            published_candidate_locator=None,
        )
        self.assertEqual(result["mode"], "observe")
        self.assertEqual(result["planned"]["source_custody"], source_locator)
        self.assertEqual(result["planned"]["candidate_record"], candidate_locator)
        self.assertIsNone(result["published"])
        self.assertNotIn(b"secret", canonical_bytes(result))

    def test_publication_result_rejects_partial_or_unproven_active_locators(self) -> None:
        source, candidate = self._plans()
        source_locator = _local_locator(
            SOURCE_REPOSITORY, build_oci_manifest(source)
        )
        candidate_locator = _local_locator(
            CANDIDATE_REPOSITORY, build_oci_manifest(candidate)
        )
        with self.assertRaisesRegex(ValueError, "appear together"):
            _publication_result(
                mode="active",
                candidate_plan=candidate,
                planned_source_locator=source_locator,
                planned_candidate_locator=candidate_locator,
                published_source_locator={
                    **source_locator,
                    "anonymous_readback_sha256": "f" * 64,
                },
                published_candidate_locator=None,
            )
        with self.assertRaisesRegex(ValueError, "anonymous readback"):
            _publication_result(
                mode="active",
                candidate_plan=candidate,
                planned_source_locator=source_locator,
                planned_candidate_locator=candidate_locator,
                published_source_locator={
                    **source_locator,
                    "anonymous_readback_sha256": "f" * 64,
                },
                published_candidate_locator=candidate_locator,
            )


if __name__ == "__main__":
    unittest.main()
