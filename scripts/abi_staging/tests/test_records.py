from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cli import _local_locator, _publication_result
from scripts.abi_staging.contract import (
    build_bottle_contract,
    make_candidate_reuse_record,
)
from scripts.abi_staging.custody import source_capsule_digest
from scripts.abi_staging.oci import build_oci_manifest
from scripts.abi_staging.plan import exact_formula_subject
from scripts.abi_staging.records import (
    BOTTLE_CONTRACT_MEDIA_TYPE,
    BOTTLE_LAYER_MEDIA_TYPE,
    BOTTLE_METADATA_MEDIA_TYPE,
    CANDIDATE_RECORD_MEDIA_TYPE,
    CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
    ATTEMPT_OUTCOME_MEDIA_TYPE,
    OCI_MANIFEST_MEDIA_TYPE,
    SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
    TapRecordError,
    build_candidate_oci_plan,
    build_candidate_reuse_oci_plan,
    build_attempt_outcome_oci_plan,
    build_attempt_outcome_record,
    build_source_custody_oci_plan,
    validate_abi_epoch_status,
    validate_abi_history_record,
    validate_admission_record,
    validate_candidate_record,
    validate_durable_record,
    validate_historical_maintenance_authorization,
    validate_attempt_outcome_record,
)
from scripts.abi_staging.tests.test_contract import (
    _candidate as _reusable_candidate,
    _inputs as _contract_inputs,
    _new_request_context,
)


TAP_ROOT = Path(__file__).resolve().parents[3]
REQUEST = "a" * 64
SUBJECT = '{"architecture":"wasm32","identity":"mini-tool","kind":"formula"}'
CANDIDATE_REPOSITORY = (
    "kandelo-dev/homebrew-tap-core-abi-8-candidates/mini-tool"
)
SOURCE_REPOSITORY = "kandelo-dev/homebrew-tap-core-abi-8-source-custody"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40
TREE_A = "2" * 40


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


def _run() -> dict[str, object]:
    return {
        "repository": "kandelo-dev/homebrew-tap-core",
        "workflow_ref": ".github/workflows/abi-history.yml@refs/heads/main",
        "run_id": 9,
        "run_attempt": 1,
        "job": "verify-history",
    }


def _record_link(digest: str = SHA_A) -> dict[str, str]:
    return {
        "record_sha256": digest,
        "immutable_reference": (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7-records/history@sha256:"
            + digest
        ),
    }


def _history_record() -> dict[str, object]:
    return {
        "schema": 1,
        "kind": "kandelo-abi-history-record",
        "plan": {
            "source_abi": 7,
            "successor_abi": 8,
            "preactivation_tap_commit": COMMIT_A,
            "preactivation_tap_tree": TREE_A,
            "branch": "abi/7",
            "expected_current_metadata_sha256": SHA_A,
            "protection_requirement_sha256": SHA_B,
        },
        "created_ref_object": COMMIT_A,
        "protection_evidence": {
            "branch": "abi/7",
            "covered": True,
            "observed_protection_sha256": SHA_C,
            "protection_requirement_sha256": SHA_B,
            "ref_object": COMMIT_A,
            "ref_tree": TREE_A,
            "source": "ruleset",
        },
        "metadata_verification_sha256": SHA_B,
        "public_readback_sha256": SHA_C,
        "run": _run(),
    }


def _maintenance_authorization() -> dict[str, object]:
    return {
        "schema": 1,
        "kind": "kandelo-abi-historical-maintenance-authorization",
        "abi": 7,
        "branch": "abi/7",
        "source": {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": COMMIT_A,
            "tree": TREE_A,
        },
        "formula": {
            "tap": "kandelo-dev/homebrew-tap-core",
            "formula": "bash",
            "architecture": "wasm32",
        },
        "reason": "failed-package-repair",
        "maintainer": {
            "login": "maintainer",
            "permission": "maintain",
            "authorization_reference": (
                "https://github.com/kandelo-dev/homebrew-tap-core/issues/9#issuecomment-1"
            ),
        },
        "policy": {
            "policy_version": 1,
            "policy_sha256": SHA_A,
            "guard_registry_version": 1,
            "guard_registry_sha256": SHA_B,
        },
        "history_record": _record_link(SHA_A),
        "run": _run(),
    }


def _epoch_status(*, state: str = "retired") -> dict[str, object]:
    subject = {"formula": "bash", "architecture": "wasm32"}
    return {
        "schema": 1,
        "kind": "kandelo-abi-epoch-status",
        "abi": 7,
        "scheduled_subjects": [subject],
        "terminal_outcomes": [
            {"subject": subject, "outcome": "failure", "record": _record_link()}
        ],
        "state": state,
        "repair_links": [],
        "run": _run(),
    }


def _admission_record() -> dict[str, object]:
    candidate_layer = {
        "sha256": SHA_A,
        "bytes": 12,
        "immutable_reference": (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/bash@sha256:"
            + SHA_A
        ),
    }
    canonical = {
        "sha256": SHA_B,
        "bytes": 99,
        "immutable_reference": (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8/bash@sha256:" + SHA_B
        ),
    }
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-admission",
        "common": {
            "request_sha256": SHA_C,
            "subject": {"kind": "candidate", "identity": SHA_C},
            "source": {
                "repository": "Automattic/kandelo",
                "commit": COMMIT_A,
                "tree": TREE_A,
            },
            "run": _run(),
            "guard_codes": [],
            "work_state": "complete",
            "outcome": "success",
            "artifact_class": "canonical",
            "artifact": canonical,
            "promotion_state": "promoted",
            "retry_state": {
                "attempts": 0,
                "eligible": False,
                "exhausted": False,
                "next_action": "none",
            },
            "blockers": [],
        },
        "admission": {
            "candidate_record_sha256": SHA_C,
            "promoted_layer": candidate_layer,
            "qualifying_receipt_sha256s": [SHA_A],
            "merged_pull_request": {
                "repository": "Automattic/kandelo",
                "number": 19,
                "head": COMMIT_A,
                "merge_commit": COMMIT_B,
            },
            "tap_source": {
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": COMMIT_A,
                "tree": TREE_A,
            },
            "canonical": canonical,
            "canonical_public_readback_sha256": SHA_B,
            "formula_metadata_source": {
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": COMMIT_B,
                "tree": "4" * 40,
            },
            "formula_metadata_update": {
                "formula": "bash",
                "architecture": "wasm32",
                "expected_main_commit": COMMIT_A,
                "expected_normalized_formula_sha256": SHA_A,
                "expected_generated_metadata_sha256": SHA_B,
                "allowed_paths": [
                    "Formula/bash.rb",
                    "Kandelo/formula/bash.json",
                    "Kandelo/metadata.json",
                    "Kandelo/link/bash-1.0-rebuild1-wasm32.json",
                ],
                "link_manifest_path": "Kandelo/link/bash-1.0-rebuild1-wasm32.json",
                "link_manifest_sha256": SHA_C,
                "canonical_manifest_digest": SHA_B,
                "bottle_layer_sha256": SHA_A,
                "bottle_layer_bytes": 12,
                "target_abi": 8,
            },
            "original_producer": {
                "request_sha256": SHA_C,
                "head": COMMIT_A,
                "run_id": 8,
            },
        },
    }


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

    def test_attempt_outcome_preserves_protected_publication_failure_facts(self) -> None:
        publication_failure = {
            "phase": "candidate-record-publication",
            "kind": "registry-http",
            "http_status": 503,
            "retryable": True,
            "guard_code": "candidate_public_readback_failed",
        }
        record = build_attempt_outcome_record(
            request_sha256=REQUEST,
            subject=SUBJECT,
            contract_sha256="b" * 64,
            retry_ordinal=1,
            outcome="failure",
            guard_code="transient_infrastructure_failure",
            completed_at="2026-08-09T10:00:00.000Z",
            run=self.publication_run,
            handoff={"sha256": "c" * 64, "bytes": 1024},
            candidate_record_sha256=None,
            publication_failure=publication_failure,
        )
        validate_attempt_outcome_record(record)
        self.assertEqual(record["attempt"]["publication_failure"], publication_failure)

        changed = json.loads(canonical_bytes(record))
        changed["attempt"]["guard_code"] = "candidate_public_readback_failed"
        with self.assertRaises(TapRecordError):
            validate_attempt_outcome_record(changed)

    def test_candidate_reuse_is_an_independent_immutable_record(self) -> None:
        contract = build_bottle_contract(_contract_inputs())
        record = make_candidate_reuse_record(
            contract,
            exact_formula_subject("curl", "wasm32"),
            _reusable_candidate(contract),
            _new_request_context(),
        )
        plan = build_candidate_reuse_oci_plan(
            record, repository=CANDIDATE_REPOSITORY + "/reuse"
        )
        self.assertEqual(plan.artifact_type, CANDIDATE_REUSE_RECORD_MEDIA_TYPE)
        self.assertEqual([layer.role for layer in plan.layers], ["immutable-record-bytes"])
        self.assertEqual(plan.config.body, canonical_bytes(record))
        self.assertEqual(
            plan.annotations["dev.kandelo.abi-staging.classification"],
            "public-candidate-reuse-not-endorsement",
        )

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

    def test_history_record_binds_adjacent_abi_ref_tree_and_protection(self) -> None:
        record = _history_record()
        validate_abi_history_record(record)
        validate_durable_record(record)
        self.assertEqual(
            canonical_sha256(record),
            "c804a1d25a868967caacb74fe3316f1066c9e95f1214c1518197fe6359410219",
        )

        for field, value in (
            ("successor_abi", 9),
            ("branch", "abi/6"),
            ("preactivation_tap_tree", "9" * 40),
        ):
            changed = json.loads(canonical_bytes(record))
            changed["plan"][field] = value
            with self.subTest(field=field), self.assertRaises(TapRecordError):
                validate_abi_history_record(changed)

        unprotected = json.loads(canonical_bytes(record))
        unprotected["protection_evidence"]["covered"] = False
        with self.assertRaisesRegex(TapRecordError, "protected"):
            validate_abi_history_record(unprotected)

    def test_historical_maintenance_is_not_an_override_or_candidate_identity(self) -> None:
        record = _maintenance_authorization()
        validate_historical_maintenance_authorization(record)
        validate_durable_record(record)
        self.assertEqual(
            canonical_sha256(record),
            "b9e3f502307e79990ea51650b6e4f3f9a50a598b1260439aedd3c9c1a6651781",
        )

        for field, value in (
            ("reason", "override"),
            ("candidate_record_sha256", SHA_A),
            ("branch", "main"),
        ):
            changed = json.loads(canonical_bytes(record))
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(TapRecordError):
                validate_historical_maintenance_authorization(changed)

    def test_retired_epoch_requires_every_scheduled_subject_terminal(self) -> None:
        record = _epoch_status()
        validate_abi_epoch_status(record)
        validate_durable_record(record)
        self.assertEqual(
            canonical_sha256(record),
            "8d5aad3c929647e5c3bb51c1eabf688c804a9c722999ba5ce1069023de359df9",
        )

        incomplete = json.loads(canonical_bytes(record))
        incomplete["terminal_outcomes"] = []
        with self.assertRaisesRegex(TapRecordError, "retired"):
            validate_abi_epoch_status(incomplete)

        skipped = json.loads(canonical_bytes(record))
        skipped["terminal_outcomes"][0]["outcome"] = "skipped"
        with self.assertRaisesRegex(TapRecordError, "terminal"):
            validate_abi_epoch_status(skipped)

    def test_admission_binds_layer_readback_producer_and_metadata_update(self) -> None:
        record = _admission_record()
        validate_admission_record(record)
        validate_durable_record(record)

        mutations = {
            "readback": lambda changed: changed["admission"].__setitem__(
                "canonical_public_readback_sha256", SHA_C
            ),
            "layer": lambda changed: changed["admission"][
                "formula_metadata_update"
            ].__setitem__("bottle_layer_sha256", SHA_C),
            "link": lambda changed: changed["admission"][
                "formula_metadata_update"
            ].__setitem__("link_manifest_path", "../outside.json"),
            "producer": lambda changed: changed["admission"][
                "original_producer"
            ].__setitem__("head", COMMIT_B),
            "metadata": lambda changed: changed["admission"].pop(
                "formula_metadata_update"
            ),
        }
        for name, mutate in mutations.items():
            changed = json.loads(canonical_bytes(record))
            mutate(changed)
            with self.subTest(name=name), self.assertRaises(TapRecordError):
                validate_admission_record(changed)

    def test_unknown_durable_record_kind_fails_closed(self) -> None:
        record = _history_record()
        record["kind"] = "kandelo-abi-history-ish"
        with self.assertRaisesRegex(TapRecordError, "unknown"):
            validate_durable_record(record)


if __name__ == "__main__":
    unittest.main()
