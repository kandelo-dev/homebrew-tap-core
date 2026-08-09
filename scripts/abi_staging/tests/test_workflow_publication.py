from __future__ import annotations

import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.abi_staging.tests.test_execution import _active_bundle
from scripts.abi_staging.tests.test_verification_execution import _fixture
from scripts.abi_staging.workflow_artifact import WorkflowArtifactV1, WorkflowJobV1


PUBLICATION_RUN = {
    "repository": "kandelo-dev/homebrew-tap-core",
    "workflow_ref": (
        "kandelo-dev/homebrew-tap-core/"
        ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
    ),
    "run_id": 808,
    "run_attempt": 2,
    "job": "publish-candidate",
}
VERIFICATION_RUN = {**PUBLICATION_RUN, "job": "verify-candidate"}


class WorkflowPublicationTests(unittest.TestCase):
    def test_reuse_publisher_cli_requires_exact_coordination_and_readback_guards(self) -> None:
        from scripts.abi_staging import cli

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(cli, "_publish_workflow_reuse") as publish:
                status = cli.main(
                    [
                        "publish-workflow-reuse",
                        "--run-id",
                        "808",
                        "--run-attempt",
                        "2",
                        "--head-sha",
                        "7" * 40,
                        "--work-id",
                        "a" * 64,
                        "--coordination-artifact-id",
                        "501",
                        "--coordination-artifact-digest",
                        "d" * 64,
                        "--require-github-digest",
                        "--anonymous-readback",
                        "--immutable",
                        "--out",
                        str(Path(temporary) / "publication.json"),
                    ]
                )
        self.assertEqual(status, 0)
        publish.assert_called_once()

    def test_receipt_publisher_cli_requires_the_exact_github_bridge_guards(self) -> None:
        from scripts.abi_staging import cli

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(cli, "_publish_workflow_receipt") as publish:
                status = cli.main(
                    [
                        "publish-workflow-receipt",
                        "--run-id",
                        "808",
                        "--run-attempt",
                        "2",
                        "--head-sha",
                        "7" * 40,
                        "--work-id",
                        "a" * 64,
                        "--coordination-artifact-id",
                        "501",
                        "--coordination-artifact-digest",
                        "d" * 64,
                        "--producer-conclusion",
                        "success",
                        "--handoff-artifact-id",
                        "1001",
                        "--handoff-artifact-digest",
                        "e" * 64,
                        "--require-github-digest",
                        "--anonymous-readback",
                        "--immutable",
                        "--out",
                        str(Path(temporary) / "publication.json"),
                    ]
                )
        self.assertEqual(status, 0)
        publish.assert_called_once()

    def test_candidate_publisher_cli_requires_the_exact_github_bridge_guards(self) -> None:
        from scripts.abi_staging import cli

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(cli, "_publish_workflow_candidate") as publish:
                status = cli.main(
                    [
                        "publish-workflow-candidate",
                        "--run-id",
                        "808",
                        "--run-attempt",
                        "2",
                        "--head-sha",
                        "7" * 40,
                        "--work-id",
                        "a" * 64,
                        "--coordination-artifact-id",
                        "501",
                        "--coordination-artifact-digest",
                        "d" * 64,
                        "--producer-conclusion",
                        "success",
                        "--handoff-artifact-id",
                        "1001",
                        "--handoff-artifact-digest",
                        "e" * 64,
                        "--require-github-digest",
                        "--anonymous-readback",
                        "--immutable",
                        "--out",
                        str(Path(temporary) / "publication.json"),
                    ]
                )
        self.assertEqual(status, 0)
        publish.assert_called_once()

    def test_successful_attempt_binds_exact_job_artifact_and_candidate(self) -> None:
        try:
            module = importlib.import_module("scripts.abi_staging.workflow_publication")
        except ModuleNotFoundError:
            module = None
        self.assertIsNotNone(module, "protected workflow publication support is absent")
        assert module is not None
        bundle = _active_bundle()
        work = bundle["workflow"]["build_work"][0]
        artifact = WorkflowArtifactV1(
            id=1001,
            name=f"{work['artifact_name']}-808-2",
            sha256="b" * 64,
            size_in_bytes=1024,
        )
        record = module.build_protected_attempt_outcome(
            bundle=bundle,
            work=work,
            job=WorkflowJobV1(
                "build-candidate " + work["work_id"],
                "success",
                "2026-08-09T10:00:00.000Z",
            ),
            artifact=artifact,
            application_outcome="success",
            application_guard=None,
            candidate_record_sha256="c" * 64,
            publication_run=PUBLICATION_RUN,
        )
        attempt = record["attempt"]
        self.assertEqual(attempt["outcome"], "success")
        self.assertEqual(attempt["candidate_record_sha256"], "c" * 64)
        self.assertEqual(
            attempt["handoff"], {"sha256": "b" * 64, "bytes": 1024}
        )
        self.assertEqual(attempt["completed_at"], "2026-08-09T10:00:00.000Z")

    def test_missing_build_handoff_is_not_invented_as_transient_runner_loss(self) -> None:
        module = importlib.import_module("scripts.abi_staging.workflow_publication")
        bundle = _active_bundle()
        work = bundle["workflow"]["build_work"][0]
        for conclusion in (
            "action_required",
            "cancelled",
            "failure",
            "stale",
            "startup_failure",
        ):
            with self.subTest(conclusion=conclusion):
                record = module.build_protected_attempt_outcome(
                    bundle=bundle,
                    work=work,
                    job=WorkflowJobV1(
                        "build-candidate " + work["work_id"],
                        conclusion,
                        "2026-08-09T10:00:00.000Z",
                    ),
                    artifact=None,
                    application_outcome=None,
                    application_guard=None,
                    candidate_record_sha256=None,
                    publication_run=PUBLICATION_RUN,
                )
                self.assertEqual(record["attempt"]["guard_code"], "build_failed")
        timed_out = module.build_protected_attempt_outcome(
            bundle=bundle,
            work=work,
            job=WorkflowJobV1(
                "build-candidate " + work["work_id"],
                "timed_out",
                "2026-08-09T10:00:00.000Z",
            ),
            artifact=None,
            application_outcome=None,
            application_guard=None,
            candidate_record_sha256=None,
            publication_run=PUBLICATION_RUN,
        )
        self.assertEqual(timed_out["attempt"]["outcome"], "timeout")
        self.assertEqual(timed_out["attempt"]["guard_code"], "build_timeout")
        hostile = dict(PUBLICATION_RUN, job="build-candidate")
        with self.assertRaises(module.WorkflowPublicationError):
            module.build_protected_attempt_outcome(
                bundle=bundle,
                work=work,
                job=WorkflowJobV1(
                    "build-candidate " + work["work_id"],
                    "success",
                    "2026-08-09T10:00:00.000Z",
                ),
                artifact=None,
                application_outcome="success",
                application_guard=None,
                candidate_record_sha256=None,
                publication_run=hostile,
            )

    def test_missing_verification_handoff_is_not_invented_as_runner_loss(self) -> None:
        module = importlib.import_module("scripts.abi_staging.workflow_publication")
        bundle, _fetched = _fixture()
        work = bundle["workflow"]["verify_work"][0]
        for conclusion in (
            "action_required",
            "cancelled",
            "failure",
            "stale",
            "startup_failure",
        ):
            with self.subTest(conclusion=conclusion):
                result = module.build_protected_verification_outcome(
                    bundle=bundle,
                    work=work,
                    job=WorkflowJobV1(
                        "verify-candidate " + work["work_id"],
                        conclusion,
                        "2026-08-09T10:00:00.000Z",
                    ),
                    artifact=None,
                    application_outcome=None,
                    application_guard=None,
                    verification_run=VERIFICATION_RUN,
                )
                self.assertEqual(result["guard_code"], "verification_failed")
                self.assertEqual(
                    result["attempt_ordinal"], work["attempt_ordinal"]
                )
        timed_out = module.build_protected_verification_outcome(
            bundle=bundle,
            work=work,
            job=WorkflowJobV1(
                "verify-candidate " + work["work_id"],
                "timed_out",
                "2026-08-09T10:00:00.000Z",
            ),
            artifact=None,
            application_outcome=None,
            application_guard=None,
            verification_run=VERIFICATION_RUN,
        )
        self.assertEqual(timed_out["outcome"], "timeout")
        self.assertEqual(timed_out["guard_code"], "verification_timeout")

    def test_valid_verification_application_cannot_be_detached_from_its_artifact(self) -> None:
        module = importlib.import_module("scripts.abi_staging.workflow_publication")
        bundle, _fetched = _fixture()
        work = bundle["workflow"]["verify_work"][0]
        with self.assertRaises(module.WorkflowPublicationError):
            module.build_protected_verification_outcome(
                bundle=bundle,
                work=work,
                job=WorkflowJobV1(
                    "verify-candidate " + work["work_id"],
                    "success",
                    "2026-08-09T10:00:00.000Z",
                ),
                artifact=None,
                application_outcome="success",
                application_guard=None,
                verification_run=VERIFICATION_RUN,
            )

    def test_artifact_service_failure_is_transient_even_after_a_successful_job(self) -> None:
        module = importlib.import_module("scripts.abi_staging.workflow_publication")
        bundle, _fetched = _fixture()
        work = bundle["workflow"]["verify_work"][0]
        result = module.build_protected_verification_outcome(
            bundle=bundle,
            work=work,
            job=WorkflowJobV1(
                "verify-candidate " + work["work_id"],
                "success",
                "2026-08-09T10:00:00.000Z",
            ),
            artifact=None,
            application_outcome=None,
            application_guard=None,
            verification_run=VERIFICATION_RUN,
            infrastructure_kind="artifact-service-unavailable",
            infrastructure_http_status=503,
        )
        self.assertEqual(result["outcome"], "failure")
        self.assertEqual(
            result["guard_code"], "transient_infrastructure_failure"
        )


if __name__ == "__main__":
    unittest.main()
