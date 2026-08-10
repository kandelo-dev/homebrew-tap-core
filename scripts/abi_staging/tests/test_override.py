from __future__ import annotations

from dataclasses import replace
import copy
import hashlib
import io
import json
from pathlib import Path
import unittest
import zipfile

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.override import (
    OverrideError,
    accept_artifact_risk,
    authorize_capture,
    build_capture_authorization_oci_plan,
    build_override_receipt_oci_plan,
    GitHubMaintenanceClientV1,
    load_guard_registry,
    load_maintenance_evidence,
    load_maintenance_evidence_archive,
    retry_exhausted,
)
from scripts.abi_staging.policy import load_tap_staging_policy
from scripts.abi_staging.scheduler import AttemptFactV1


TAP_ROOT = Path(__file__).resolve().parents[3]
POLICY = load_tap_staging_policy(TAP_ROOT / "Kandelo/staging/tap-policy.toml")
SOURCE = {
    "repository": "Automattic/kandelo",
    "commit": "1" * 40,
    "tree": "2" * 40,
}
RUN = {
    "repository": "kandelo-dev/homebrew-tap-core",
    "workflow_ref": ".github/workflows/abi-staging-maintenance.yml@refs/heads/main",
    "run_id": 818,
    "run_attempt": 1,
    "job": "maintain",
}
MAINTAINER = {
    "login": "maintainer",
    "permission": "maintain",
    "authorization_reference": (
        "https://github.com/kandelo-dev/homebrew-tap-core/actions/runs/818"
    ),
}
FORMULA = {
    "tap": "kandelo-dev/homebrew-tap-core",
    "formula": "mini-tool",
    "architecture": "wasm32",
    "target_abi": 8,
    "bottle_contract_sha256": "3" * 64,
}


def _guard_registry() -> tuple[bytes, str]:
    guards = [
        {
            "code": "build_failed",
            "default_disposition": "record-no-candidate",
            "override_policy": "never",
            "recovery_policy": "rebuild",
            "summary": "The application build failed.",
        },
        {
            "code": "build_input_capture_incomplete",
            "default_disposition": "fail-before-build",
            "override_policy": "exact-subject-build-risk",
            "recovery_policy": "none",
            "summary": "One exact build input could not be captured.",
        },
        {
            "code": "candidate_integrity_mismatch",
            "default_disposition": "reject-candidate",
            "override_policy": "never",
            "recovery_policy": "none",
            "summary": "Candidate bytes disagree.",
        },
        {
            "code": "transient_infrastructure_failure",
            "default_disposition": "schedule-retry",
            "override_policy": "never",
            "recovery_policy": "manual-retry-after-exhaustion",
            "summary": "Protected infrastructure interrupted execution.",
        },
        {
            "code": "verification_failed",
            "default_disposition": "mark-ineligible",
            "override_policy": "exact-artifact",
            "recovery_policy": "none",
            "summary": "Exact candidate verification failed.",
        },
        {
            "code": "verification_timeout",
            "default_disposition": "mark-ineligible",
            "override_policy": "exact-artifact",
            "recovery_policy": "retry-policy",
            "summary": "Exact candidate verification timed out.",
        },
    ]
    body = canonical_bytes(
        {
            "schema": 1,
            "kind": "kandelo-abi-staging-guard-codes",
            "version": 1,
            "guards": guards,
        }
    )
    return body, hashlib.sha256(body).hexdigest()


def _request() -> tuple[dict[str, object], str, bytes]:
    registry_body, registry_sha256 = _guard_registry()
    request = {
        "schema": 1,
        "kind": "kandelo-abi-staging-request",
        "pull_request": {"repository": "Automattic/kandelo", "number": 19},
        "build_source": SOURCE,
        "target_abi": {"version": 8, "snapshot_sha256": "4" * 64},
        "requirements": {
            "digest": "5" * 64,
            "change_classes": ["abi"],
            "products": [
                {
                    "id": "mini-shell",
                    "path": "images/vfs/products/mini-shell.toml",
                    "manifest_sha256": "6" * 64,
                }
            ],
            "registries": [
                {
                    "kind": "tests",
                    "path": "tests/vfs-products.toml",
                    "sha256": "7" * 64,
                }
            ],
            "evidence": [],
        },
        "issuance": {
            "issuer_repository": "Automattic/kandelo",
            "issuer_workflow_ref": (
                "Automattic/kandelo/.github/workflows/"
                "abi-staging-request-feed.yml@" + "8" * 40
            ),
            "policy_version": 1,
            "policy_sha256": "9" * 64,
            "guard_registry_version": 1,
            "guard_registry_sha256": registry_sha256,
            "authorization": {"mode": "same-repository", "head": "1" * 40},
        },
        "informational_context": {},
    }
    return request, canonical_sha256(request), registry_body


def _artifact(digest: str, size: int = 73) -> dict[str, object]:
    return {
        "sha256": digest,
        "bytes": size,
        "immutable_reference": (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
            f"mini-tool@sha256:{digest}"
        ),
    }


def _candidate(request_sha256: str) -> tuple[dict[str, object], str]:
    layer = _artifact("a" * 64)
    contract = _artifact(FORMULA["bottle_contract_sha256"], 61)
    metadata = _artifact("d" * 64, 89)
    custody = _artifact("b" * 64, 79)
    composition = _artifact("e" * 64, 105)
    record = {
        "schema": 1,
        "kind": "kandelo-abi-staging-candidate",
        "common": {
            "request_sha256": request_sha256,
            "subject": {
                "kind": "candidate",
                "identity": (
                    "kandelo-dev/homebrew-tap-core/mini-tool@sha256:" + "a" * 64
                ),
            },
            "source": SOURCE,
            "run": {**RUN, "job": "publish-candidate"},
            "guard_codes": [],
            "work_state": "complete",
            "outcome": "success",
            "artifact_class": "candidate",
            "artifact": layer,
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
                **FORMULA,
                "version": "1.0",
                "revision": 0,
                "bottle_rebuild": 0,
            },
            "bottle_layer": layer,
            "normalized_components": [
                {"id": "bottle-contract", "artifact": contract},
                {"id": "bottle-metadata", "artifact": metadata},
                {"id": "source-custody", "artifact": custody},
                {
                    "id": "vfs-composition-descriptor",
                    "artifact": composition,
                },
            ],
            "direct_dependency_layers": [],
            "source_custody_sha256": custody["sha256"],
            "producer": {
                "request_sha256": request_sha256,
                "head": SOURCE["commit"],
                "run_id": 717,
            },
            "nonendorsed": True,
        },
    }
    return record, "c" * 64


def _attempts(request_sha256: str) -> tuple[AttemptFactV1, ...]:
    subject = json.dumps(
        {"architecture": "wasm32", "identity": "mini-tool", "kind": "formula"},
        separators=(",", ":"),
        sort_keys=True,
    )
    return tuple(
        AttemptFactV1(
            request_sha256=request_sha256,
            subject=subject,
            contract_sha256=FORMULA["bottle_contract_sha256"],
            retry_ordinal=ordinal,
            outcome="canceled",
            guard_code="transient_infrastructure_failure",
            completed_at=f"2026-08-09T0{ordinal}:00:00.000Z",
            record_sha256=hashlib.sha256(f"attempt:{ordinal}".encode()).hexdigest(),
        )
        for ordinal in range(4)
    )


class OverridePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request, self.request_sha256, registry_body = _request()
        self.registry = load_guard_registry(
            registry_body,
            expected_version=self.request["issuance"]["guard_registry_version"],
            expected_sha256=self.request["issuance"]["guard_registry_sha256"],
        )

    def _authorize(self, **changes):
        arguments = {
            "request": self.request,
            "request_sha256": self.request_sha256,
            "formula": FORMULA,
            "guard_code": "build_input_capture_incomplete",
            "guard_registry": self.registry,
            "maintainer": MAINTAINER,
            "justification": "Reviewed one exact inspectable host-tool input.",
            "run": RUN,
            "tap_repository": POLICY.tap_repository,
            "expected_formula": FORMULA,
        }
        arguments.update(changes)
        return authorize_capture(**arguments)

    def test_capture_authorization_binds_exact_request_formula_arch_and_contract(self) -> None:
        record = self._authorize()
        self.assertEqual(record["common"]["request_sha256"], self.request_sha256)
        self.assertEqual(
            record["common"]["subject"],
            {
                "kind": "formula",
                "identity": "kandelo-dev/homebrew-tap-core/mini-tool",
                "architecture": "wasm32",
            },
        )
        self.assertEqual(record["capture_authorization"]["formula"], FORMULA)
        self.assertNotIn("artifact", record["common"])
        self.assertNotIn("candidate", canonical_bytes(record).decode())

        wrong = dict(FORMULA, bottle_contract_sha256="d" * 64)
        with self.assertRaisesRegex(OverrideError, "Formula subject"):
            self._authorize(formula=wrong)
        with self.assertRaisesRegex(OverrideError, "request digest"):
            self._authorize(request_sha256="e" * 64)

    def test_capture_rejects_wrong_subject_guard_permission_and_justification(self) -> None:
        with self.assertRaisesRegex(OverrideError, "Formula subject"):
            self._authorize(formula=dict(FORMULA, architecture="wasm64"))
        with self.assertRaisesRegex(OverrideError, "unknown guard"):
            self._authorize(guard_code="not_registered")
        with self.assertRaisesRegex(OverrideError, "capture guard"):
            self._authorize(guard_code="verification_failed")
        with self.assertRaisesRegex(OverrideError, "permission"):
            self._authorize(maintainer=dict(MAINTAINER, permission="write"))
        for justification in ("", " \n", "x" * 2049):
            with self.subTest(justification_length=len(justification)):
                with self.assertRaisesRegex(OverrideError, "justification"):
                    self._authorize(justification=justification)

    def test_guard_registry_identity_is_exact_and_not_a_parallel_allowlist(self) -> None:
        body, digest = _guard_registry()
        with self.assertRaisesRegex(OverrideError, "guard-registry digest"):
            load_guard_registry(
                body, expected_version=1, expected_sha256="f" * 64
            )
        changed = json.loads(body)
        changed["guards"][0]["override_policy"] = "exact-artifact"
        changed_body = canonical_bytes(changed)
        with self.assertRaisesRegex(OverrideError, "guard-registry digest"):
            load_guard_registry(
                changed_body, expected_version=1, expected_sha256=digest
            )

    def test_artifact_override_requires_existing_exact_candidate_and_layer(self) -> None:
        candidate, candidate_sha256 = _candidate(self.request_sha256)
        record = accept_artifact_risk(
            request=self.request,
            request_sha256=self.request_sha256,
            candidate=candidate,
            candidate_record_sha256=candidate_sha256,
            accepted_guard_codes=("verification_failed",),
            guard_registry=self.registry,
            maintainer=MAINTAINER,
            justification="Reviewed the exact failing candidate behavior.",
            run=RUN,
            tap_repository=POLICY.tap_repository,
        )
        self.assertEqual(record["common"]["promotion_state"], "accepted-with-override")
        self.assertEqual(record["common"]["artifact"], candidate["candidate"]["bottle_layer"])
        self.assertEqual(
            record["override_receipt"]["candidate_record_sha256"],
            candidate_sha256,
        )
        for missing in (None, {}):
            with self.subTest(candidate=missing):
                with self.assertRaisesRegex(OverrideError, "exact candidate"):
                    accept_artifact_risk(
                        request=self.request,
                        request_sha256=self.request_sha256,
                        candidate=missing,
                        candidate_record_sha256=candidate_sha256,
                        accepted_guard_codes=("verification_failed",),
                        guard_registry=self.registry,
                        maintainer=MAINTAINER,
                        justification="Reviewed exact bytes.",
                        run=RUN,
                        tap_repository=POLICY.tap_repository,
                    )
        missing_layer = copy.deepcopy(candidate)
        del missing_layer["candidate"]["bottle_layer"]
        with self.assertRaisesRegex(OverrideError, "exact candidate"):
            accept_artifact_risk(
                request=self.request,
                request_sha256=self.request_sha256,
                candidate=missing_layer,
                candidate_record_sha256=candidate_sha256,
                accepted_guard_codes=("verification_failed",),
                guard_registry=self.registry,
                maintainer=MAINTAINER,
                justification="Reviewed exact bytes.",
                run=RUN,
                tap_repository=POLICY.tap_repository,
            )

    def test_never_overrideable_guards_and_wrong_candidate_are_rejected(self) -> None:
        candidate, candidate_sha256 = _candidate(self.request_sha256)
        for guard in ("build_failed", "candidate_integrity_mismatch"):
            with self.subTest(guard=guard):
                with self.assertRaisesRegex(OverrideError, "never be overridden"):
                    accept_artifact_risk(
                        request=self.request,
                        request_sha256=self.request_sha256,
                        candidate=candidate,
                        candidate_record_sha256=candidate_sha256,
                        accepted_guard_codes=(guard,),
                        guard_registry=self.registry,
                        maintainer=MAINTAINER,
                        justification="This must not become eligible.",
                        run=RUN,
                        tap_repository=POLICY.tap_repository,
                    )
        wrong = copy.deepcopy(candidate)
        wrong["common"]["request_sha256"] = "f" * 64
        with self.assertRaisesRegex(OverrideError, "exact request"):
            accept_artifact_risk(
                request=self.request,
                request_sha256=self.request_sha256,
                candidate=wrong,
                candidate_record_sha256=candidate_sha256,
                accepted_guard_codes=("verification_failed",),
                guard_registry=self.registry,
                maintainer=MAINTAINER,
                justification="Wrong request must fail.",
                run=RUN,
                tap_repository=POLICY.tap_repository,
            )

    def test_postbuild_capture_receipt_requires_matching_authorization(self) -> None:
        candidate, candidate_sha256 = _candidate(self.request_sha256)
        authorization = self._authorize()
        authorization_sha256 = canonical_sha256(authorization)
        receipt = accept_artifact_risk(
            request=self.request,
            request_sha256=self.request_sha256,
            candidate=candidate,
            candidate_record_sha256=candidate_sha256,
            accepted_guard_codes=("build_input_capture_incomplete",),
            guard_registry=self.registry,
            maintainer=MAINTAINER,
            justification="The authorized build produced these exact bytes.",
            run=RUN,
            tap_repository=POLICY.tap_repository,
            capture_authorization=authorization,
            capture_authorization_sha256=authorization_sha256,
        )
        self.assertEqual(
            receipt["override_receipt"]["capture_authorization_sha256"],
            authorization_sha256,
        )
        with self.assertRaisesRegex(OverrideError, "capture authorization"):
            accept_artifact_risk(
                request=self.request,
                request_sha256=self.request_sha256,
                candidate=candidate,
                candidate_record_sha256=candidate_sha256,
                accepted_guard_codes=("build_input_capture_incomplete",),
                guard_registry=self.registry,
                maintainer=MAINTAINER,
                justification="Missing authorization must fail.",
                run=RUN,
                tap_repository=POLICY.tap_repository,
            )
        mismatched = copy.deepcopy(authorization)
        mismatched["capture_authorization"]["formula"]["bottle_contract_sha256"] = "f" * 64
        with self.assertRaisesRegex(OverrideError, "capture authorization"):
            accept_artifact_risk(
                request=self.request,
                request_sha256=self.request_sha256,
                candidate=candidate,
                candidate_record_sha256=candidate_sha256,
                accepted_guard_codes=("build_input_capture_incomplete",),
                guard_registry=self.registry,
                maintainer=MAINTAINER,
                justification="Mismatched authorization must fail.",
                run=RUN,
                tap_repository=POLICY.tap_repository,
                capture_authorization=mismatched,
                capture_authorization_sha256=canonical_sha256(mismatched),
            )

    def test_oci_plans_are_immutable_and_stay_in_candidate_namespace(self) -> None:
        authorization = self._authorize()
        capture_plan = build_capture_authorization_oci_plan(
            authorization, policy=POLICY
        )
        self.assertIn("-candidates/mini-tool/authorizations/capture", capture_plan.repository)
        self.assertEqual(capture_plan.config.body, canonical_bytes(authorization))
        candidate, candidate_sha256 = _candidate(self.request_sha256)
        receipt = accept_artifact_risk(
            request=self.request,
            request_sha256=self.request_sha256,
            candidate=candidate,
            candidate_record_sha256=candidate_sha256,
            accepted_guard_codes=("verification_timeout",),
            guard_registry=self.registry,
            maintainer=MAINTAINER,
            justification="Reviewed this exact timeout.",
            run=RUN,
            tap_repository=POLICY.tap_repository,
        )
        override_plan = build_override_receipt_oci_plan(
            receipt, candidate=candidate, policy=POLICY
        )
        self.assertIn("-candidates/mini-tool/receipts/overrides", override_plan.repository)
        self.assertEqual(override_plan.config.body, canonical_bytes(receipt))

    def test_retry_exhaustion_schedules_new_attempt_without_override(self) -> None:
        intent = retry_exhausted(
            request=self.request,
            request_sha256=self.request_sha256,
            attempts=_attempts(self.request_sha256),
            guard_registry=self.registry,
            maintainer=MAINTAINER,
            justification="Retry exact exhausted infrastructure history.",
            tap_repository=POLICY.tap_repository,
        )
        self.assertEqual(intent["kind"], "kandelo-abi-staging-manual-retry")
        self.assertEqual(intent["previous_ordinal"], 3)
        self.assertEqual(intent["next_ordinal"], 4)
        self.assertNotIn("override", json.dumps(intent))
        self.assertNotIn("promotion_state", json.dumps(intent))

        attempts = list(_attempts(self.request_sha256))
        attempts[2] = replace(attempts[2], guard_code="build_failed")
        with self.assertRaisesRegex(OverrideError, "transient history"):
            retry_exhausted(
                request=self.request,
                request_sha256=self.request_sha256,
                attempts=attempts,
                guard_registry=self.registry,
                maintainer=MAINTAINER,
                justification="Application failure is not retry exhaustion.",
                tap_repository=POLICY.tap_repository,
            )
        with self.assertRaisesRegex(OverrideError, "ordinals"):
            retry_exhausted(
                request=self.request,
                request_sha256=self.request_sha256,
                attempts=attempts[:3],
                guard_registry=self.registry,
                maintainer=MAINTAINER,
                justification="Incomplete history must fail.",
                tap_repository=POLICY.tap_repository,
            )

    def test_operation_specific_evidence_cannot_guess_capture_candidate(self) -> None:
        registry_body, _ = _guard_registry()
        evidence = {
            "schema": 1,
            "kind": "kandelo-abi-staging-maintenance-evidence",
            "operation": "authorize-capture",
            "request": self.request,
            "request_sha256": self.request_sha256,
            "guard_registry": json.loads(registry_body),
            "formula": FORMULA,
            "guard_code": "build_input_capture_incomplete",
        }
        body = canonical_bytes(evidence)
        loaded = load_maintenance_evidence(body, operation="authorize-capture")
        self.assertEqual(loaded.payload["formula"], FORMULA)
        hostile = dict(evidence, candidate_record_sha256="f" * 64)
        with self.assertRaisesRegex(OverrideError, "fields changed"):
            load_maintenance_evidence(
                canonical_bytes(hostile), operation="authorize-capture"
            )

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("maintenance-evidence.json", body)
        archive_body = output.getvalue()
        archive_digest = hashlib.sha256(archive_body).hexdigest()
        loaded = load_maintenance_evidence_archive(
            archive_body,
            expected_sha256=archive_digest,
            operation="authorize-capture",
        )
        self.assertEqual(loaded.request_sha256, self.request_sha256)
        with self.assertRaisesRegex(OverrideError, "exact digest"):
            load_maintenance_evidence_archive(
                archive_body,
                expected_sha256="0" * 64,
                operation="authorize-capture",
            )

    def test_permission_response_must_name_authorized_actor(self) -> None:
        class Response:
            status = 200

            def __init__(self, body: dict[str, object]) -> None:
                self.body = json.dumps(body).encode()
                self.headers = {"content-length": str(len(self.body))}

            def read(self, amount: int = -1) -> bytes:
                return self.body if amount < 0 else self.body[:amount]

            def close(self) -> None:
                return None

        bodies = [
            {"permission": "admin", "user": {"login": "Maintainer"}},
            {"permission": "write", "user": {"login": "maintainer"}},
            {"permission": "maintain", "user": {"login": "someone-else"}},
        ]

        def opener(_request):
            return Response(bodies.pop(0))

        client = GitHubMaintenanceClientV1(
            POLICY.tap_repository, "fixture-token", opener=opener
        )
        authorization = client.maintainer(
            "Maintainer", MAINTAINER["authorization_reference"]
        )
        self.assertEqual(authorization["login"], "maintainer")
        self.assertEqual(authorization["permission"], "admin")
        with self.assertRaisesRegex(OverrideError, "permission"):
            client.maintainer("maintainer", MAINTAINER["authorization_reference"])
        with self.assertRaisesRegex(OverrideError, "another actor"):
            client.maintainer("maintainer", MAINTAINER["authorization_reference"])


if __name__ == "__main__":
    unittest.main()
