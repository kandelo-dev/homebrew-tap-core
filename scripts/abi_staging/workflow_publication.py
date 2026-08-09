"""Protected reconstruction of durable facts from candidate workflow jobs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .execution import ExecutionError, select_build_work, select_verification_work
from .records import build_attempt_outcome_record, validate_publication_failure
from .scheduler import (
    KNOWN_GUARDS,
    ProtectedFailureFactsV1,
    classify_protected_failure,
)
from .workflow_artifact import WorkflowArtifactV1, WorkflowJobV1


class WorkflowPublicationError(ValueError):
    """Raised when protected job facts cannot form one durable outcome."""


def _protected_run(
    value: Mapping[str, Any], *, expected_repository: str, expected_job: str
) -> dict[str, Any]:
    expected = {"repository", "workflow_ref", "run_id", "run_attempt", "job"}
    if set(value) != expected:
        raise WorkflowPublicationError("protected run fields changed")
    if value.get("repository") != expected_repository or value.get("job") != expected_job:
        raise WorkflowPublicationError("protected run names another authority")
    for field in ("run_id", "run_attempt"):
        candidate = value.get(field)
        if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 1:
            raise WorkflowPublicationError(f"protected run {field} is invalid")
    workflow_ref = value.get("workflow_ref")
    if not isinstance(workflow_ref, str) or not workflow_ref or len(workflow_ref.encode()) > 1024:
        raise WorkflowPublicationError("protected run workflow ref is invalid")
    return dict(value)


def build_protected_attempt_outcome(
    *,
    bundle: Mapping[str, Any],
    work: Mapping[str, Any],
    job: WorkflowJobV1,
    artifact: WorkflowArtifactV1 | None,
    application_outcome: str | None,
    application_guard: str | None,
    candidate_record_sha256: str | None,
    publication_run: Mapping[str, Any],
    infrastructure_kind: str | None = None,
    infrastructure_http_status: int | None = None,
    publication_failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one retry fact without trusting candidate failure classification."""

    try:
        selected = select_build_work(bundle, str(work.get("work_id", "")))
    except ExecutionError as error:
        raise WorkflowPublicationError(f"candidate work identity is invalid: {error}") from error
    if dict(selected) != dict(work):
        raise WorkflowPublicationError("candidate work differs from coordination")
    expected_job_name = "build-candidate " + selected["work_id"]
    if job.name != expected_job_name:
        raise WorkflowPublicationError("candidate job names another work item")
    run = _protected_run(
        publication_run,
        expected_repository=bundle["tap_plan"]["tap_source"]["repository"],
        expected_job="publish-candidate",
    )
    expected_artifact_name = (
        f"{selected['artifact_name']}-{run['run_id']}-{run['run_attempt']}"
    )
    if artifact is not None and artifact.name != expected_artifact_name:
        raise WorkflowPublicationError("candidate artifact differs from protected job facts")
    if application_outcome not in {None, "success", "failure", "timeout"}:
        raise WorkflowPublicationError("candidate application outcome is unsupported")
    if application_guard is not None and application_guard not in KNOWN_GUARDS:
        raise WorkflowPublicationError("candidate application guard is not registered")

    checked_publication_failure = None
    if publication_failure is not None:
        try:
            checked_publication_failure = validate_publication_failure(
                publication_failure
            )
        except ValueError as error:
            raise WorkflowPublicationError(
                f"candidate publication failure facts are invalid: {error}"
            ) from error
        if (
            infrastructure_kind is not None
            or infrastructure_http_status is not None
            or artifact is None
            or application_outcome != "success"
            or application_guard is not None
            or candidate_record_sha256 is not None
            or job.conclusion != "success"
        ):
            raise WorkflowPublicationError(
                "candidate publication failure facts are contradictory"
            )
        outcome = "failure"
        guard = (
            "transient_infrastructure_failure"
            if checked_publication_failure["retryable"]
            else checked_publication_failure["guard_code"]
        )
    elif infrastructure_kind is not None:
        if (
            artifact is not None
            or application_outcome is not None
            or application_guard is not None
            or candidate_record_sha256 is not None
        ):
            raise WorkflowPublicationError(
                "candidate infrastructure facts are contradictory"
            )
        classification = classify_protected_failure(
            ProtectedFailureFactsV1(
                authority="protected-workflow",
                kind=infrastructure_kind,
                job_conclusion=job.conclusion,
                http_status=infrastructure_http_status,
                application_started=False,
                application_outcome=None,
            )
        )
        if not classification.automatic_retry:
            raise WorkflowPublicationError(
                "candidate infrastructure fact is not retryable"
            )
        outcome = "failure"
        guard = classification.guard_code
    elif infrastructure_http_status is not None:
        raise WorkflowPublicationError(
            "candidate infrastructure status lacks a failure kind"
        )
    elif application_outcome == "success":
        if (
            job.conclusion != "success"
            or artifact is None
            or application_guard is not None
            or candidate_record_sha256 is None
        ):
            raise WorkflowPublicationError("successful candidate facts are contradictory")
        outcome = "success"
        guard = None
    elif application_outcome in {"failure", "timeout"}:
        if (
            artifact is None
            or application_guard is None
            or candidate_record_sha256 is not None
            or (
                job.conclusion == "success"
                and application_guard
                not in {
                    "candidate_integrity_mismatch",
                    "source_custody_mismatch",
                    "source_identity_mismatch",
                }
            )
        ):
            raise WorkflowPublicationError("failed candidate facts are contradictory")
        outcome = application_outcome
        guard = application_guard
    else:
        if candidate_record_sha256 is not None or application_guard is not None:
            raise WorkflowPublicationError("missing application cannot assert candidate facts")
        if job.conclusion == "success":
            outcome = "failure"
            guard = "candidate_integrity_mismatch"
        elif job.conclusion == "timed_out":
            classification = classify_protected_failure(
                ProtectedFailureFactsV1(
                    authority="protected-workflow",
                    kind="build-timeout",
                    job_conclusion=job.conclusion,
                    http_status=None,
                    application_started=False,
                    application_outcome=None,
                )
            )
            outcome = "timeout"
            guard = classification.guard_code
        else:
            if job.conclusion not in {
                "action_required",
                "cancelled",
                "failure",
                "skipped",
                "stale",
                "startup_failure",
            }:
                raise WorkflowPublicationError(
                    "job without application has no failure fact"
                )
            # A terminal job conclusion says what GitHub observed; it does not
            # prove that the runner disappeared or that retrying is safe.
            outcome = "canceled" if job.conclusion == "cancelled" else "failure"
            guard = "build_failed"

    handoff = (
        None
        if artifact is None
        else {"sha256": artifact.sha256, "bytes": artifact.size_in_bytes}
    )
    try:
        return build_attempt_outcome_record(
            request_sha256=bundle["request_sha256"],
            subject=selected["subject"],
            contract_sha256=selected["contract_sha256"],
            retry_ordinal=selected["attempt_ordinal"],
            outcome=outcome,
            guard_code=guard,
            completed_at=job.completed_at,
            run=run,
            handoff=handoff,
            candidate_record_sha256=candidate_record_sha256,
            publication_failure=checked_publication_failure,
        )
    except ValueError as error:
        raise WorkflowPublicationError(f"protected attempt outcome is invalid: {error}") from error


def build_protected_verification_outcome(
    *,
    bundle: Mapping[str, Any],
    work: Mapping[str, Any],
    job: WorkflowJobV1,
    artifact: WorkflowArtifactV1 | None,
    application_outcome: str | None,
    application_guard: str | None,
    verification_run: Mapping[str, Any],
    infrastructure_kind: str | None = None,
    infrastructure_http_status: int | None = None,
    publication_failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind one verifier or infrastructure outcome to protected workflow facts."""

    try:
        selected = select_verification_work(bundle, str(work.get("work_id", "")))
    except ExecutionError as error:
        raise WorkflowPublicationError(
            f"verification work identity is invalid: {error}"
        ) from error
    if dict(selected) != dict(work):
        raise WorkflowPublicationError("verification work differs from coordination")
    if job.name != "verify-candidate " + selected["work_id"]:
        raise WorkflowPublicationError("verification job names another work item")
    run = _protected_run(
        verification_run,
        expected_repository=bundle["tap_plan"]["tap_source"]["repository"],
        expected_job="verify-candidate",
    )
    expected_artifact_name = (
        f"{selected['artifact_name']}-{run['run_id']}-{run['run_attempt']}"
    )
    if artifact is not None and artifact.name != expected_artifact_name:
        raise WorkflowPublicationError(
            "verification artifact differs from protected job facts"
        )
    allowed_application = {
        "success": {None},
        "failure": {
            "verification_failed",
            "candidate_integrity_mismatch",
        },
        "timeout": {"verification_timeout"},
    }
    if infrastructure_kind is not None:
        if (
            artifact is not None
            or application_outcome is not None
            or application_guard is not None
        ):
            raise WorkflowPublicationError(
                "verification infrastructure facts are contradictory"
            )
        classification = classify_protected_failure(
            ProtectedFailureFactsV1(
                authority="protected-workflow",
                kind=infrastructure_kind,
                job_conclusion=job.conclusion,
                http_status=infrastructure_http_status,
                application_started=False,
                application_outcome=None,
            )
        )
        if not classification.automatic_retry:
            raise WorkflowPublicationError(
                "verification infrastructure fact is not retryable"
            )
        outcome = "failure"
        guard = classification.guard_code
    elif infrastructure_http_status is not None:
        raise WorkflowPublicationError(
            "verification infrastructure status lacks a failure kind"
        )
    elif application_outcome is not None:
        if (
            artifact is None
            or application_outcome not in allowed_application
            or application_guard not in allowed_application[application_outcome]
            or (application_outcome == "success" and job.conclusion != "success")
            or (
                job.conclusion == "success"
                and application_outcome != "success"
                and application_guard != "candidate_integrity_mismatch"
            )
        ):
            raise WorkflowPublicationError(
                "verification application facts are contradictory"
            )
        outcome = application_outcome
        guard = application_guard
    else:
        if artifact is not None or application_guard is not None:
            raise WorkflowPublicationError(
                "missing verification application cannot assert output facts"
            )
        if job.conclusion == "success":
            outcome = "failure"
            guard = "candidate_integrity_mismatch"
        elif job.conclusion == "timed_out":
            classification = classify_protected_failure(
                ProtectedFailureFactsV1(
                    authority="protected-workflow",
                    kind="verification-timeout",
                    job_conclusion=job.conclusion,
                    http_status=None,
                    application_started=False,
                    application_outcome=None,
                )
            )
            outcome = "timeout"
            guard = classification.guard_code
        else:
            if job.conclusion not in {
                "action_required",
                "cancelled",
                "failure",
                "skipped",
                "stale",
                "startup_failure",
            }:
                raise WorkflowPublicationError(
                    "verification job has no protected failure classification"
                )
            # Missing candidate handoff cannot distinguish runner loss from a
            # deterministic setup, checkout, or command failure.
            outcome = "canceled" if job.conclusion == "cancelled" else "failure"
            guard = "verification_failed"
    checked_publication_failure = None
    if publication_failure is not None:
        try:
            checked_publication_failure = validate_publication_failure(
                publication_failure
            )
        except ValueError as error:
            raise WorkflowPublicationError(
                f"verification publication failure facts are invalid: {error}"
            ) from error
        if outcome == "success":
            outcome = "failure"
            guard = (
                "transient_infrastructure_failure"
                if checked_publication_failure["retryable"]
                else checked_publication_failure["guard_code"]
            )
    result = {
        "request_sha256": bundle["request_sha256"],
        "subject": selected["subject"],
        "candidate_record_sha256": selected["candidate_record_sha256"],
        "test_definition_sha256": selected["test_definition_sha256"],
        "host": selected["host"],
        "attempt_ordinal": selected["attempt_ordinal"],
        "outcome": outcome,
        "guard_code": guard,
        "completed_at": job.completed_at,
        "run": run,
        "handoff": (
            None
            if artifact is None
            else {"sha256": artifact.sha256, "bytes": artifact.size_in_bytes}
        ),
    }
    if checked_publication_failure is not None:
        result["publication_failure"] = checked_publication_failure
    return result
