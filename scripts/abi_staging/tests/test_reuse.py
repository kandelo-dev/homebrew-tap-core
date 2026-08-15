from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cli import _local_locator
from scripts.abi_staging.contract import (
    load_bottle_contract,
    validate_candidate_reuse_record,
)
from scripts.abi_staging.oci import build_oci_manifest
from scripts.abi_staging.reuse import (
    CandidateReuseError,
    build_candidate_reuse_from_bundle,
    publish_candidate_reuse,
)
from scripts.abi_staging.policy import load_tap_staging_policy
from scripts.abi_staging.records import (
    build_candidate_oci_plan,
    build_source_custody_oci_plan,
)
from scripts.abi_staging.tests.test_records import (
    CANDIDATE_REPOSITORY,
    SOURCE_REPOSITORY,
    _write_handoff,
)
from scripts.abi_staging.tests.test_oci import FakeRegistryTransport


SUBJECT = '{"architecture":"wasm32","identity":"mini-tool","kind":"formula"}'
PUBLICATION_RUN = {
    "repository": "kandelo-dev/homebrew-tap-core",
    "workflow_ref": ".github/workflows/abi-staging-reconcile.yml@refs/heads/main",
    "run_id": 404,
    "run_attempt": 1,
    "job": "publish-reuse",
}
TAP_ROOT = Path(__file__).resolve().parents[3]
REUSE_TAG_PREFIX = "reuse-sha256-"


class CandidateReusePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        handoff = Path(temporary.name) / "handoff"
        handoff.mkdir()
        _write_handoff(handoff)
        source_plan = build_source_custody_oci_plan(
            handoff / "source-custody", repository=SOURCE_REPOSITORY
        )
        source_manifest = build_oci_manifest(source_plan)
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
                **PUBLICATION_RUN,
                "run_id": 202,
                "job": "publish-candidate",
            },
        )
        self.candidate = json.loads(candidate_plan.config.body)
        candidate_manifest = build_oci_manifest(candidate_plan)
        self.candidate_locator = _local_locator(
            CANDIDATE_REPOSITORY, candidate_manifest
        )
        self.candidate_digest = self.candidate_locator["digest"].removeprefix(
            "sha256:"
        )
        self.contract = load_bottle_contract(
            (handoff / "bottle-contract.json").read_bytes()
        )
        self.contract_digest = canonical_sha256(self.contract)
        self.definition = {
            "hosts": ["build"],
            "id": "bottle-structure",
            "kandelo_paths": ["scripts/homebrew-inspect-bottle.py"],
            "policy": "kandelo-bottle-structure-v1",
        }
        self.definition["sha256"] = canonical_sha256(
            {key: self.definition[key] for key in ("hosts", "id", "kandelo_paths", "policy")}
        )
        receipt_digest = "d" * 64
        receipt_repository = CANDIDATE_REPOSITORY + "/receipts/bottle-structure/build"
        self.receipt = {
            "schema": 1,
            "kind": "kandelo-abi-staging-verification",
            "common": {
                "request_sha256": self.candidate["common"]["request_sha256"],
                "subject": {"kind": "candidate", "identity": self.candidate_digest},
                "source": self.candidate["common"]["source"],
                "run": {
                    **PUBLICATION_RUN,
                    "run_id": 303,
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
                "candidate_record_sha256": self.candidate_digest,
                "candidate_layer": self.candidate["candidate"]["bottle_layer"],
                "test_definition_sha256": self.definition["sha256"],
                "host": "build",
                "attempt_ordinal": 0,
                "diagnostics": [],
            },
        }
        self.work = {
            "work_id": "e" * 64,
            "work_class": "required",
            "subject": SUBJECT,
            "subject_sha256": hashlib.sha256(SUBJECT.encode()).hexdigest(),
            "attempt_ordinal": 0,
            "contract_sha256": self.contract_digest,
            "formula_plan_sha256": "1" * 64,
            "action": "reuse-candidate",
            "artifact_name": "abi-staging-reuse-" + "e" * 64,
            "candidate_record_sha256": self.candidate_digest,
            "candidate_locator": self.candidate_locator,
        }
        self.bundle = {
            "request_sha256": "f" * 64,
            "request": {
                "build_source": {
                    "repository": "Automattic/kandelo",
                    "commit": "8" * 40,
                    "tree": "9" * 40,
                }
            },
            "contracts": {SUBJECT: self.contract},
            "candidates": {
                "records": {self.candidate_digest: self.candidate},
                "locators": {self.candidate_digest: self.candidate_locator},
            },
            "verification_tests": [self.definition],
            "verification_receipts": {
                "records": {receipt_digest: self.receipt},
                "locators": {
                    receipt_digest: {
                        "repository": "ghcr.io/" + receipt_repository,
                        "digest": "sha256:" + receipt_digest,
                        "immutable_reference": (
                            f"ghcr.io/{receipt_repository}@sha256:{receipt_digest}"
                        ),
                    }
                },
            },
        }

    def test_reuse_preserves_original_producer_and_binds_current_request(self) -> None:
        with patch(
            "scripts.abi_staging.reuse.select_reuse_work",
            return_value=self.work,
        ):
            record = build_candidate_reuse_from_bundle(
                self.bundle,
                self.work["work_id"],
                publication_run=PUBLICATION_RUN,
            )
        validate_candidate_reuse_record(record)
        self.assertEqual(record["common"]["request_sha256"], "f" * 64)
        self.assertEqual(
            record["candidate_reuse"]["original_producer"],
            self.candidate["candidate"]["producer"],
        )
        self.assertEqual(
            record["candidate_reuse"]["existing_candidate"]["record_sha256"],
            self.candidate_digest,
        )
        self.assertNotEqual(
            record["common"]["request_sha256"],
            record["candidate_reuse"]["original_producer"]["request_sha256"],
        )

    def test_reuse_without_complete_current_verification_fails_closed(self) -> None:
        self.bundle["verification_receipts"] = {"records": {}, "locators": {}}
        with patch(
            "scripts.abi_staging.reuse.select_reuse_work",
            return_value=self.work,
        ), self.assertRaisesRegex(CandidateReuseError, "qualifying receipt"):
            build_candidate_reuse_from_bundle(
                self.bundle,
                self.work["work_id"],
                publication_run=PUBLICATION_RUN,
            )

    def test_reuse_publishes_in_the_public_candidate_package_with_its_own_tag(self) -> None:
        transport = FakeRegistryTransport()
        policy = load_tap_staging_policy(TAP_ROOT / "Kandelo/staging/tap-policy.toml")
        with patch(
            "scripts.abi_staging.reuse.select_reuse_work",
            return_value=self.work,
        ):
            _record, locator = publish_candidate_reuse(
                self.bundle,
                self.work["work_id"],
                publication_run=PUBLICATION_RUN,
                policy=policy,
                transport=transport,
            )

        self.assertEqual(locator.repository, "ghcr.io/" + CANDIDATE_REPOSITORY)
        self.assertIn(
            (CANDIDATE_REPOSITORY, REUSE_TAG_PREFIX + locator.digest.removeprefix("sha256:")),
            transport.manifests,
        )
        self.assertFalse(
            any(repository.endswith("/reuse") for repository, _tag in transport.manifests)
        )


if __name__ == "__main__":
    unittest.main()
