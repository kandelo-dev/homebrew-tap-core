from __future__ import annotations

from contextlib import nullcontext
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.oci import (
    OciPublicationError,
    PublishedRecordLocatorV1,
    build_oci_manifest,
)
from scripts.abi_staging.reconcile import PullRequestLifecycleV1
from scripts.abi_staging.tests.test_execution import _active_bundle
from scripts.abi_staging.tests.test_verification_execution import _fixture
from scripts.abi_staging.verification import VerificationPublicationError
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
    def test_candidate_publisher_cli_durably_records_registry_failures(self) -> None:
        from scripts.abi_staging import cli

        bundle = _active_bundle()
        work = bundle["workflow"]["build_work"][0]
        tap_source = bundle["tap_plan"]["tap_source"]
        coordination_artifact = WorkflowArtifactV1(
            501,
            "abi-staging-coordination-808-2",
            "d" * 64,
            2048,
        )
        handoff_artifact = WorkflowArtifactV1(
            1001,
            f"{work['artifact_name']}-808-2",
            "e" * 64,
            1024,
        )
        outer = self

        class ArtifactClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def artifact_by_id(self, *, artifact_id, name, sha256):
                artifact = {
                    coordination_artifact.id: coordination_artifact,
                    handoff_artifact.id: handoff_artifact,
                }[artifact_id]
                outer.assertEqual((artifact.name, artifact.sha256), (name, sha256))
                return artifact

            def extract_artifact(self, artifact, destination, **_bounds):
                destination.mkdir()
                if artifact.id == coordination_artifact.id:
                    (destination / "coordination.json").write_bytes(
                        canonical_bytes(bundle)
                    )
                else:
                    (destination / "build-result.json").write_bytes(b"{}\n")
                return {}

        class PublicClient:
            def __init__(self, _policy):
                pass

            def pull_request_lifecycle(self, _number):
                return PullRequestLifecycleV1(
                    "open", bundle["lifecycle"]["current_head"], None
                )

        cases = (
            ("registry-http", 429, True, "candidate_public_readback_failed"),
            ("registry-http", 503, True, "namespace_bootstrap_failed"),
            ("transport-reset", None, True, "namespace_bootstrap_failed"),
            ("registry-contract", None, False, "candidate_public_readback_failed"),
        )
        for kind, http_status, retryable, original_guard in cases:
            with self.subTest(kind=kind, http_status=http_status), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "publication.json"
                error = OciPublicationError(
                    "bounded registry failure",
                    guard_code=original_guard,
                    retryable=retryable,
                    phase="candidate-record-publication",
                    kind=kind,
                    http_status=http_status,
                )
                published_plans = []

                def publish_attempt(plan, **_kwargs):
                    published_plans.append(plan)
                    manifest = build_oci_manifest(plan)
                    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
                    return PublishedRecordLocatorV1(
                        "ghcr.io/" + plan.repository,
                        digest,
                        f"ghcr.io/{plan.repository}@{digest}",
                        "f" * 64,
                    )

                environment = {
                    "GITHUB_REPOSITORY": PUBLICATION_RUN["repository"],
                    "GITHUB_WORKFLOW_REF": PUBLICATION_RUN["workflow_ref"],
                    "GITHUB_ACTOR": "github-actions",
                    "GITHUB_TOKEN": "github-token",
                    "HOMEBREW_GITHUB_PACKAGES_TOKEN": "package-token",
                    "HOMEBREW_GITHUB_PACKAGES_USER": "publisher",
                }
                expectations = {
                    "request_sha256": bundle["request_sha256"],
                    "subject": work["subject"],
                    "kandelo_source": bundle["request"]["build_source"],
                    "tap_source": tap_source,
                }
                with (
                    patch.dict(os.environ, environment, clear=False),
                    patch.object(cli, "GitHubWorkflowArtifactClientV1", ArtifactClient),
                    patch.object(cli, "GitHubPublicClient", PublicClient),
                    patch.object(cli, "snapshot_tap_source", return_value=tap_source),
                    patch.object(cli, "_recheck_workflow_activation"),
                    patch.object(
                        cli,
                        "load_handoff_validation_expectations",
                        return_value=expectations,
                    ),
                    patch.object(cli, "validate_handoff"),
                    patch.object(
                        cli,
                        "load_build_result",
                        return_value={"outcome": "success", "exit_code": 0},
                    ),
                    patch.object(
                        cli, "_publish_candidate_paths", side_effect=error
                    ) as candidate_publisher,
                    patch.object(
                        cli,
                        "isolated_oras_transport",
                        return_value=nullcontext(object()),
                    ) as registry_transport,
                    patch.object(cli, "publish_record", side_effect=publish_attempt),
                ):
                    status_code = cli.main(
                        [
                            "publish-workflow-candidate",
                            "--run-id",
                            "808",
                            "--run-attempt",
                            "2",
                            "--head-sha",
                            tap_source["commit"],
                            "--work-id",
                            work["work_id"],
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
                            str(output),
                        ]
                    )
                self.assertEqual(status_code, 0)
                self.assertEqual(
                    candidate_publisher.call_args.kwargs["registry_username"],
                    "github-actions",
                )
                self.assertEqual(
                    candidate_publisher.call_args.kwargs["registry_token"],
                    "github-token",
                )
                registry_transport.assert_called_once_with(
                    username="github-actions", token="github-token"
                )
                self.assertEqual(len(published_plans), 1)
                attempt = json.loads(published_plans[0].config.body)["attempt"]
                self.assertEqual(
                    attempt["retry_ordinal"], work["attempt_ordinal"]
                )
                self.assertEqual(
                    attempt["guard_code"],
                    "transient_infrastructure_failure"
                    if retryable
                    else original_guard,
                )
                self.assertEqual(
                    attempt["publication_failure"],
                    {
                        "phase": "candidate-record-publication",
                        "kind": kind,
                        "http_status": http_status,
                        "retryable": retryable,
                        "guard_code": original_guard,
                    },
                )

    def test_verification_publisher_cli_recovers_with_a_protected_failure_receipt(self) -> None:
        from scripts.abi_staging import cli

        bundle, _fetched = _fixture()
        work = bundle["workflow"]["verify_work"][0]
        tap_source = bundle["tap_plan"]["tap_source"]
        coordination_artifact = WorkflowArtifactV1(
            501,
            "abi-staging-coordination-808-2",
            "d" * 64,
            2048,
        )
        result_artifact = WorkflowArtifactV1(
            1001,
            f"{work['artifact_name']}-808-2",
            "e" * 64,
            1024,
        )

        class ArtifactClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def artifact_by_id(self, *, artifact_id, name, sha256):
                artifact = {
                    coordination_artifact.id: coordination_artifact,
                    result_artifact.id: result_artifact,
                }[artifact_id]
                self_outer.assertEqual((artifact.name, artifact.sha256), (name, sha256))
                return artifact

            def extract_artifact(self, artifact, destination, **_bounds):
                destination.mkdir()
                if artifact.id == coordination_artifact.id:
                    (destination / "coordination.json").write_bytes(
                        canonical_bytes(bundle)
                    )
                else:
                    (destination / "result.json").write_bytes(b"{}\n")
                return {}

        class PublicClient:
            def __init__(self, _policy):
                pass

            def pull_request_lifecycle(self, _number):
                return PullRequestLifecycleV1(
                    "open", bundle["lifecycle"]["current_head"], None
                )

        self_outer = self
        oci_error = OciPublicationError(
            "registry unavailable",
            guard_code="candidate_public_readback_failed",
            retryable=True,
            kind="registry-http",
            http_status=503,
        )
        publication_error = VerificationPublicationError(
            oci_error,
            phase="verification-receipt-publication",
            context="cannot publish verification receipt",
        )
        loaded_result = {"outcome": "success"}
        recovered = []

        def publish_recovery(**kwargs):
            recovered.append(kwargs)
            return PublishedRecordLocatorV1(
                "ghcr.io/example/receipts",
                "sha256:" + "a" * 64,
                "ghcr.io/example/receipts@sha256:" + "a" * 64,
                "b" * 64,
            )

        environment = {
            "GITHUB_REPOSITORY": PUBLICATION_RUN["repository"],
            "GITHUB_WORKFLOW_REF": PUBLICATION_RUN["workflow_ref"],
            "GITHUB_TOKEN": "github-token",
            "HOMEBREW_GITHUB_PACKAGES_TOKEN": "package-token",
            "HOMEBREW_GITHUB_PACKAGES_USER": "publisher",
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, environment, clear=False),
            patch.object(cli, "GitHubWorkflowArtifactClientV1", ArtifactClient),
            patch.object(cli, "GitHubPublicClient", PublicClient),
            patch.object(cli, "snapshot_tap_source", return_value=tap_source),
            patch.object(cli, "_recheck_workflow_activation"),
            patch.object(cli, "_recheck_coordinated_lifecycle"),
            patch.object(cli, "load_coordination_bundle", return_value=bundle),
            patch.object(cli, "load_verification_result", return_value=loaded_result),
            patch.object(cli, "_verification_result_matches_work", return_value=True),
            patch.object(cli, "publish_verification_receipt", side_effect=publication_error),
            patch.object(cli, "publish_protected_verification_outcome", side_effect=publish_recovery),
            patch.object(
                cli,
                "isolated_oras_transport",
                return_value=nullcontext(object()),
            ),
        ):
            output = Path(temporary) / "publication.json"
            status_code = cli.main(
                [
                    "publish-workflow-receipt",
                    "--run-id",
                    "808",
                    "--run-attempt",
                    "2",
                    "--head-sha",
                    tap_source["commit"],
                    "--work-id",
                    work["work_id"],
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
                    str(output),
                ]
            )
            result = json.loads(output.read_bytes())
        self.assertEqual(status_code, 0)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(
            recovered[0]["publication_failure"],
            {
                "phase": "verification-receipt-publication",
                "kind": "registry-http",
                "http_status": 503,
                "retryable": True,
                "guard_code": "candidate_public_readback_failed",
            },
        )
        self.assertEqual(
            recovered[0]["guard_code"], "transient_infrastructure_failure"
        )
        self.assertEqual(result["outcome"]["publication_failure"], recovered[0]["publication_failure"])

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

    def test_registry_failure_becomes_a_durable_retry_fact_at_the_same_ordinal(self) -> None:
        module = importlib.import_module("scripts.abi_staging.workflow_publication")
        bundle = _active_bundle()
        work = bundle["workflow"]["build_work"][0]
        artifact = WorkflowArtifactV1(
            id=1001,
            name=f"{work['artifact_name']}-808-2",
            sha256="b" * 64,
            size_in_bytes=1024,
        )
        publication_failure = {
            "phase": "candidate-record-publication",
            "kind": "registry-http",
            "http_status": 429,
            "retryable": True,
            "guard_code": "candidate_public_readback_failed",
        }
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
            candidate_record_sha256=None,
            publication_run=PUBLICATION_RUN,
            publication_failure=publication_failure,
        )
        attempt = record["attempt"]
        self.assertEqual(attempt["retry_ordinal"], work["attempt_ordinal"])
        self.assertEqual(attempt["outcome"], "failure")
        self.assertEqual(
            attempt["guard_code"], "transient_infrastructure_failure"
        )
        self.assertEqual(attempt["publication_failure"], publication_failure)

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

    def test_verification_publication_failure_preserves_the_exact_retry_fact(self) -> None:
        module = importlib.import_module("scripts.abi_staging.workflow_publication")
        bundle, _fetched = _fixture()
        work = bundle["workflow"]["verify_work"][0]
        artifact = WorkflowArtifactV1(
            id=1001,
            name=f"{work['artifact_name']}-808-2",
            sha256="b" * 64,
            size_in_bytes=1024,
        )
        publication_failure = {
            "phase": "verification-receipt-publication",
            "kind": "registry-http",
            "http_status": 503,
            "retryable": True,
            "guard_code": "candidate_public_readback_failed",
        }
        result = module.build_protected_verification_outcome(
            bundle=bundle,
            work=work,
            job=WorkflowJobV1(
                "verify-candidate " + work["work_id"],
                "success",
                "2026-08-09T10:00:00.000Z",
            ),
            artifact=artifact,
            application_outcome="success",
            application_guard=None,
            verification_run=VERIFICATION_RUN,
            publication_failure=publication_failure,
        )
        self.assertEqual(result["outcome"], "failure")
        self.assertEqual(
            result["guard_code"], "transient_infrastructure_failure"
        )
        self.assertEqual(result["publication_failure"], publication_failure)


if __name__ == "__main__":
    unittest.main()
