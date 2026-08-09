from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.oci import build_oci_manifest, publish_record
from scripts.abi_staging.policy import (
    VerificationTestDefinitionV1,
    load_tap_staging_policy,
)
from scripts.abi_staging.records import (
    build_candidate_oci_plan,
    build_source_custody_oci_plan,
)
from scripts.abi_staging.tests.test_oci import (
    FakeRegistryTransport,
    SOURCE_ASSOCIATION,
)
from scripts.abi_staging.tests.test_records import (
    CANDIDATE_REPOSITORY,
    SOURCE_REPOSITORY,
    _write_handoff,
)
from scripts.abi_staging.verification import (
    VerificationError,
    load_verification_result,
    publish_protected_verification_outcome,
    publish_verification_receipt,
    receipt_repository,
    validate_verification_receipt_record,
)


TEST_DEFINITION = VerificationTestDefinitionV1(
    id="bottle-structure",
    hosts=("build",),
    kandelo_paths=(
        "scripts/homebrew-inspect-bottle.py",
        "scripts/test-homebrew-inspect-bottle.sh",
    ),
    policy="kandelo-bottle-structure-v1",
    sha256=canonical_sha256(
        {
            "hosts": ["build"],
            "id": "bottle-structure",
            "kandelo_paths": [
                "scripts/homebrew-inspect-bottle.py",
                "scripts/test-homebrew-inspect-bottle.sh",
            ],
            "policy": "kandelo-bottle-structure-v1",
        }
    ),
)
VERIFIER_RUN = {
    "repository": "kandelo-dev/homebrew-tap-core",
    "workflow_ref": ".github/workflows/staging.yml@refs/heads/main",
    "run_id": 303,
    "run_attempt": 1,
    "job": "verify-candidate",
}
RUNTIME_ARTIFACTS = {"kernel": None, "host_runtime": None, "vfs": None}
TAP_ROOT = Path(__file__).resolve().parents[3]


def _artifact(body: bytes) -> dict[str, object]:
    return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


class VerificationReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.handoff = self.root / "handoff"
        self.handoff.mkdir()
        _write_handoff(self.handoff)
        self.transport = FakeRegistryTransport()
        self.tap_policy = load_tap_staging_policy(
            TAP_ROOT / "Kandelo/staging/tap-policy.toml"
        )

        source_plan = build_source_custody_oci_plan(
            self.handoff / "source-custody", repository=SOURCE_REPOSITORY
        )
        source_locator = publish_record(
            source_plan,
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        source_manifest = build_oci_manifest(source_plan)
        candidate_plan = build_candidate_oci_plan(
            self.handoff,
            repository=CANDIDATE_REPOSITORY,
            source_record={
                "repository": source_locator.repository,
                "digest": source_locator.digest,
                "immutable_reference": source_locator.immutable_reference,
            },
            source_manifest_bytes=source_manifest,
            publication_run={
                **VERIFIER_RUN,
                "run_id": 202,
                "job": "publish-candidate",
            },
        )
        self.candidate_locator = publish_record(
            candidate_plan,
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.candidate_manifest = build_oci_manifest(candidate_plan)
        self.candidate = json.loads(candidate_plan.config.body)

    def _write_result(
        self,
        name: str,
        *,
        outcome: str = "success",
        attempt_ordinal: int = 0,
        run: dict[str, object] | None = None,
        request_sha256: str | None = None,
        source: dict[str, str] | None = None,
    ) -> Path:
        root = self.root / name
        diagnostics = root / "diagnostics"
        diagnostics.mkdir(parents=True)
        summary = f"fixture {outcome}\n".encode()
        (diagnostics / "summary.txt").write_bytes(summary)
        layer = self.candidate["candidate"]["bottle_layer"]
        result = {
            "schema": 1,
            "kind": "kandelo-abi-staging-verification-result",
            "request_sha256": request_sha256
            or self.candidate["common"]["request_sha256"],
            "candidate_record": {
                "repository": self.candidate_locator.repository,
                "digest": self.candidate_locator.digest,
                "immutable_reference": self.candidate_locator.immutable_reference,
            },
            "candidate_layer": layer,
            "test_definition": {
                "id": TEST_DEFINITION.id,
                "sha256": TEST_DEFINITION.sha256,
                "host": "build",
            },
            "source": source or self.candidate["common"]["source"],
            "run": run or VERIFIER_RUN,
            "attempt_ordinal": attempt_ordinal,
            "outcome": outcome,
            "exit_code": 0 if outcome == "success" else (124 if outcome == "timeout" else 7),
            "runtime_artifacts": {
                "kernel": None,
                "host_runtime": None,
                "vfs": None,
            },
            "diagnostics": [
                {
                    "path": "diagnostics/summary.txt",
                    **_artifact(summary),
                }
            ],
        }
        result_body = canonical_bytes(result)
        (root / "result.json").write_bytes(result_body)
        inventory = {
            "schema": 1,
            "kind": "kandelo-abi-staging-verification-inventory",
            "files": [
                {
                    "path": "diagnostics/summary.txt",
                    "role": "diagnostic",
                    **_artifact(summary),
                },
                {
                    "path": "result.json",
                    "role": "result",
                    **_artifact(result_body),
                },
            ],
        }
        (root / "inventory.json").write_bytes(canonical_bytes(inventory))
        return root

    def _publish(self, result_root: Path):
        return publish_verification_receipt(
            result_root,
            candidate_locator={
                "repository": self.candidate_locator.repository,
                "digest": self.candidate_locator.digest,
                "immutable_reference": self.candidate_locator.immutable_reference,
            },
            test_definition=TEST_DEFINITION,
            tap_policy=self.tap_policy,
            expected_run=VERIFIER_RUN,
            expected_runtime_artifacts=RUNTIME_ARTIFACTS,
            expected_request_sha256=self.candidate["common"]["request_sha256"],
            expected_source=self.candidate["common"]["source"],
            completed_at="2026-08-09T10:00:00.000Z",
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )

    def _receipt(self, locator) -> tuple[dict[str, object], dict[str, object]]:
        repository = locator.repository.removeprefix("ghcr.io/")
        manifest = json.loads(
            self.transport.manifests[(repository, locator.digest)]
        )
        config_digest = manifest["config"]["digest"]
        return manifest, json.loads(self.transport.blobs[(repository, config_digest)])

    def test_success_failure_and_timeout_publish_distinct_factual_receipts(self) -> None:
        expected = {
            "success": ([], "eligible"),
            "failure": (["verification_failed"], "ineligible"),
            "timeout": (["verification_timeout"], "ineligible"),
        }
        locators = []
        for ordinal, (outcome, (guards, promotion)) in enumerate(expected.items()):
            run = {**VERIFIER_RUN, "run_id": VERIFIER_RUN["run_id"] + ordinal}
            root = self._write_result(
                outcome, outcome=outcome, attempt_ordinal=ordinal, run=run
            )
            locator = publish_verification_receipt(
                root,
                candidate_locator={
                    "repository": self.candidate_locator.repository,
                    "digest": self.candidate_locator.digest,
                    "immutable_reference": self.candidate_locator.immutable_reference,
                },
                test_definition=TEST_DEFINITION,
                tap_policy=self.tap_policy,
                expected_run=run,
                expected_runtime_artifacts=RUNTIME_ARTIFACTS,
                expected_request_sha256=self.candidate["common"]["request_sha256"],
                expected_source=self.candidate["common"]["source"],
                completed_at=f"2026-08-09T10:00:0{ordinal}.000Z",
                transport=self.transport,
                expected_source_repository=SOURCE_ASSOCIATION,
            )
            locators.append(locator)
            manifest, receipt = self._receipt(locator)
            validate_verification_receipt_record(receipt)
            self.assertEqual(
                manifest["annotations"]["dev.kandelo.abi-staging.completed-at"],
                f"2026-08-09T10:00:0{ordinal}.000Z",
            )
            self.assertEqual(receipt["common"]["outcome"], outcome)
            self.assertEqual(receipt["common"]["guard_codes"], guards)
            self.assertEqual(receipt["common"]["promotion_state"], promotion)
            self.assertEqual(
                receipt["verification"]["candidate_record_sha256"],
                self.candidate_locator.digest.removeprefix("sha256:"),
            )
            self.assertEqual(
                receipt["verification"]["attempt_ordinal"], ordinal
            )
            self.assertEqual(
                [
                    layer["annotations"]["dev.kandelo.abi-staging.role"]
                    for layer in manifest["layers"]
                ],
                ["verification-result", "diagnostic-0000"],
            )
        self.assertEqual(len({locator.digest for locator in locators}), 3)
        self.assertEqual(
            self.transport.manifests[
                (CANDIDATE_REPOSITORY, self.candidate_locator.digest)
            ],
            self.candidate_manifest,
        )

    def test_protected_runner_loss_publishes_a_retryable_receipt_without_candidate_output(self) -> None:
        locator = publish_protected_verification_outcome(
            candidate_locator={
                "repository": self.candidate_locator.repository,
                "digest": self.candidate_locator.digest,
                "immutable_reference": self.candidate_locator.immutable_reference,
            },
            test_definition=TEST_DEFINITION,
            host="build",
            tap_policy=self.tap_policy,
            expected_run=VERIFIER_RUN,
            expected_runtime_artifacts=RUNTIME_ARTIFACTS,
            expected_request_sha256=self.candidate["common"]["request_sha256"],
            expected_source=self.candidate["common"]["source"],
            completed_at="2026-08-09T10:00:00.000Z",
            attempt_ordinal=1,
            outcome="canceled",
            guard_code="transient_infrastructure_failure",
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        manifest, receipt = self._receipt(locator)
        validate_verification_receipt_record(receipt)
        self.assertEqual(
            [
                layer["annotations"]["dev.kandelo.abi-staging.role"]
                for layer in manifest["layers"]
            ],
            ["protected-verification-outcome"],
        )
        self.assertEqual(receipt["common"]["outcome"], "canceled")
        self.assertEqual(
            receipt["common"]["guard_codes"],
            ["transient_infrastructure_failure"],
        )

    def test_protected_publication_failure_is_durable_in_the_receipt(self) -> None:
        publication_failure = {
            "phase": "verification-candidate-readback",
            "kind": "registry-contract",
            "http_status": None,
            "retryable": False,
            "guard_code": "candidate_public_readback_failed",
        }
        locator = publish_protected_verification_outcome(
            candidate_locator={
                "repository": self.candidate_locator.repository,
                "digest": self.candidate_locator.digest,
                "immutable_reference": self.candidate_locator.immutable_reference,
            },
            test_definition=TEST_DEFINITION,
            host="build",
            tap_policy=self.tap_policy,
            expected_run=VERIFIER_RUN,
            expected_runtime_artifacts=RUNTIME_ARTIFACTS,
            expected_request_sha256=self.candidate["common"]["request_sha256"],
            expected_source=self.candidate["common"]["source"],
            completed_at="2026-08-09T10:00:00.000Z",
            attempt_ordinal=1,
            outcome="failure",
            guard_code="candidate_public_readback_failed",
            publication_failure=publication_failure,
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        _manifest, receipt = self._receipt(locator)
        validate_verification_receipt_record(receipt)
        self.assertEqual(
            receipt["verification"]["publication_failure"], publication_failure
        )
        self.assertEqual(receipt["verification"]["attempt_ordinal"], 1)

    def test_receipt_validator_rejects_contradictory_scheduler_facts(self) -> None:
        locator = self._publish(self._write_result("receipt-validator"))
        _manifest, receipt = self._receipt(locator)
        validate_verification_receipt_record(receipt)
        cases = []
        wrong_candidate = json.loads(canonical_bytes(receipt))
        wrong_candidate["verification"]["candidate_record_sha256"] = "f" * 64
        cases.append(wrong_candidate)
        wrong_outcome = json.loads(canonical_bytes(receipt))
        wrong_outcome["common"]["promotion_state"] = "ineligible"
        cases.append(wrong_outcome)
        wrong_attempts = json.loads(canonical_bytes(receipt))
        wrong_attempts["common"]["retry_state"]["attempts"] = 2
        cases.append(wrong_attempts)
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(VerificationError):
                validate_verification_receipt_record(candidate)

    def test_retesting_adds_receipt_without_changing_candidate_identity(self) -> None:
        first = self._publish(self._write_result("first"))
        second_run = {**VERIFIER_RUN, "run_id": 404}
        second = publish_verification_receipt(
            self._write_result("second", attempt_ordinal=1, run=second_run),
            candidate_locator={
                "repository": self.candidate_locator.repository,
                "digest": self.candidate_locator.digest,
                "immutable_reference": self.candidate_locator.immutable_reference,
            },
            test_definition=TEST_DEFINITION,
            tap_policy=self.tap_policy,
            expected_run=second_run,
            expected_runtime_artifacts=RUNTIME_ARTIFACTS,
            expected_request_sha256=self.candidate["common"]["request_sha256"],
            expected_source=self.candidate["common"]["source"],
            completed_at="2026-08-09T10:00:01.000Z",
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(
            first.repository,
            "ghcr.io/" + receipt_repository(
                CANDIDATE_REPOSITORY, TEST_DEFINITION.id, "build"
            ),
        )
        self.assertEqual(
            self.candidate_locator.digest,
            "sha256:" + hashlib.sha256(self.candidate_manifest).hexdigest(),
        )

    def test_historical_candidate_receipt_binds_current_request_without_rewriting_producer(self) -> None:
        current_request = "f" * 64
        current_source = {
            "repository": "Automattic/kandelo",
            "commit": "8" * 40,
            "tree": "9" * 40,
        }
        root = self._write_result(
            "historical-candidate-current-request",
            request_sha256=current_request,
            source=current_source,
        )
        locator = publish_verification_receipt(
            root,
            candidate_locator={
                "repository": self.candidate_locator.repository,
                "digest": self.candidate_locator.digest,
                "immutable_reference": self.candidate_locator.immutable_reference,
            },
            test_definition=TEST_DEFINITION,
            tap_policy=self.tap_policy,
            expected_run=VERIFIER_RUN,
            expected_runtime_artifacts=RUNTIME_ARTIFACTS,
            expected_request_sha256=current_request,
            expected_source=current_source,
            completed_at="2026-08-09T10:00:00.000Z",
            transport=self.transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        _manifest, receipt = self._receipt(locator)
        self.assertEqual(receipt["common"]["request_sha256"], current_request)
        self.assertEqual(receipt["common"]["source"], current_source)
        self.assertNotEqual(
            self.candidate["candidate"]["producer"]["request_sha256"],
            current_request,
        )

    def test_candidate_definition_run_and_inventory_mismatches_fail_closed(self) -> None:
        cases = (
            "candidate",
            "definition",
            "run",
            "inventory",
            "namespace",
            "runtime",
        )
        for case in cases:
            root = self._write_result(case)
            locator = {
                "repository": self.candidate_locator.repository,
                "digest": self.candidate_locator.digest,
                "immutable_reference": self.candidate_locator.immutable_reference,
            }
            definition = TEST_DEFINITION
            expected_run = VERIFIER_RUN
            policy = self.tap_policy
            if case == "candidate":
                locator = {
                    **locator,
                    "digest": "sha256:" + "f" * 64,
                    "immutable_reference": (
                        self.candidate_locator.repository + "@sha256:" + "f" * 64
                    ),
                }
            elif case == "definition":
                definition = replace(TEST_DEFINITION, sha256="f" * 64)
            elif case == "run":
                expected_run = {**VERIFIER_RUN, "run_id": 999}
            elif case == "namespace":
                policy = replace(self.tap_policy, candidate_suffix="-other")
            elif case == "runtime":
                result = json.loads((root / "result.json").read_bytes())
                result["runtime_artifacts"]["kernel"] = {
                    "bytes": 1,
                    "immutable_reference": "ghcr.io/example/runtime@sha256:"
                    + "e" * 64,
                    "sha256": "e" * 64,
                }
                result_body = canonical_bytes(result)
                (root / "result.json").write_bytes(result_body)
                inventory = json.loads((root / "inventory.json").read_bytes())
                inventory["files"][1].update(_artifact(result_body))
                (root / "inventory.json").write_bytes(canonical_bytes(inventory))
            else:
                inventory = json.loads((root / "inventory.json").read_bytes())
                inventory["files"][0]["sha256"] = "f" * 64
                (root / "inventory.json").write_bytes(canonical_bytes(inventory))
            with self.subTest(case=case), self.assertRaises(VerificationError):
                publish_verification_receipt(
                    root,
                    candidate_locator=locator,
                    test_definition=definition,
                    tap_policy=policy,
                    expected_run=expected_run,
                    expected_runtime_artifacts=RUNTIME_ARTIFACTS,
                    expected_request_sha256=self.candidate["common"]["request_sha256"],
                    expected_source=self.candidate["common"]["source"],
                    completed_at="2026-08-09T10:00:00.000Z",
                    transport=self.transport,
                    expected_source_repository=SOURCE_ASSOCIATION,
                )

    def test_result_loader_rejects_unknown_files_and_contradictory_status(self) -> None:
        root = self._write_result("malformed")
        (root / "extra.txt").write_text("extra\n", encoding="utf-8")
        with self.assertRaises(VerificationError):
            load_verification_result(root)

        root = self._write_result("contradictory", outcome="failure")
        result = json.loads((root / "result.json").read_bytes())
        result["exit_code"] = 0
        body = canonical_bytes(result)
        (root / "result.json").write_bytes(body)
        inventory = json.loads((root / "inventory.json").read_bytes())
        inventory["files"][1].update(_artifact(body))
        (root / "inventory.json").write_bytes(canonical_bytes(inventory))
        with self.assertRaisesRegex(VerificationError, "outcome"):
            load_verification_result(root)

    def test_public_candidate_is_refetched_anonymously_and_byte_drift_fails(self) -> None:
        before = len(self.transport.calls)
        self._publish(self._write_result("anonymous"))
        reads = self.transport.calls[before:]
        candidate_reads = [
            call
            for call in reads
            if call[0] == "GET"
            and (
                f"/manifests/{self.candidate_locator.digest}" in call[1]
                or self.candidate["candidate"]["bottle_layer"]["sha256"] in call[1]
            )
        ]
        self.assertTrue(candidate_reads)
        self.assertTrue(
            all(
                not authenticated
                for _method, _url, authenticated in candidate_reads
            )
        )

        self.transport.drift_anonymous = True
        with self.assertRaisesRegex(VerificationError, "exact public candidate"):
            self._publish(self._write_result("drift"))

    def test_result_loader_rejects_secret_shaped_diagnostics(self) -> None:
        root = self._write_result("secret")
        summary = b"github_pat_abcdefghijklmnopqrstuvwxyz0123456789\n"
        (root / "diagnostics/summary.txt").write_bytes(summary)
        result = json.loads((root / "result.json").read_bytes())
        result["diagnostics"][0].update(_artifact(summary))
        result_body = canonical_bytes(result)
        (root / "result.json").write_bytes(result_body)
        inventory = json.loads((root / "inventory.json").read_bytes())
        inventory["files"][0].update(_artifact(summary))
        inventory["files"][1].update(_artifact(result_body))
        (root / "inventory.json").write_bytes(canonical_bytes(inventory))
        with self.assertRaisesRegex(VerificationError, "secret"):
            load_verification_result(root)


if __name__ == "__main__":
    unittest.main()
