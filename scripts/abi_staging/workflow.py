"""Deterministic, bounded job identities for protected reconciliation runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
import hashlib
import json
import re
from typing import Any

from .canonical import canonical_bytes, canonical_sha256
from .oci import OciPublicationError, parse_public_record_locator
from .plan import exact_formula_subject, validate_tap_plan
from .scheduler import SchedulingDecisionV1


SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
WORK_ARTIFACT = re.compile(r"^abi-staging-(?:build|verification)-[0-9a-f]{64}$")


class WorkflowError(ValueError):
    """Raised when protected work identity is incomplete or contradictory."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WorkflowError(f"{field} must be an array")
    return value


def _exact(value: Any, keys: frozenset[str], field: str) -> Mapping[str, Any]:
    result = _mapping(value, field)
    if frozenset(result) != keys:
        raise WorkflowError(f"{field} fields changed")
    return result


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise WorkflowError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise WorkflowError(f"{field} is not a full lowercase Git SHA")
    return value


def _text(value: Any, field: str, maximum: int = 8192) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise WorkflowError(f"{field} is not bounded text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise WorkflowError(f"{field} is not UTF-8") from error
    if size > maximum:
        raise WorkflowError(f"{field} exceeds its byte bound")
    return value


def _positive(value: Any, field: str, maximum: int = 2**32 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise WorkflowError(f"{field} is not a bounded nonnegative integer")
    return value


def _subject(value: Any, field: str) -> str:
    text = _text(value, field, 512)
    try:
        parsed = _exact(
            json.loads(text),
            frozenset({"architecture", "identity", "kind"}),
            field,
        )
    except (json.JSONDecodeError, WorkflowError) as error:
        raise WorkflowError(f"{field} is not an exact Formula subject") from error
    if parsed["kind"] != "formula":
        raise WorkflowError(f"{field} is outside the Formula namespace")
    try:
        expected = exact_formula_subject(parsed["identity"], parsed["architecture"])
    except ValueError as error:
        raise WorkflowError(f"{field} is invalid: {error}") from error
    if text != expected:
        raise WorkflowError(f"{field} is not canonical")
    return text


def _formulae(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for formula in plan["formulae"]:
        identity = formula["identity"]
        subject = exact_formula_subject(identity["name"], identity["architecture"])
        result[subject] = formula
    return result


def _work_identity(
    *,
    request_sha256: str,
    subject: str,
    action: str,
    attempt_ordinal: int,
    contract_sha256: str,
    candidate_record_sha256: str | None,
    test_definition_sha256: str | None,
    host: str | None,
) -> str:
    return canonical_sha256(
        {
            "action": action,
            "attempt_ordinal": attempt_ordinal,
            "candidate_record_sha256": candidate_record_sha256,
            "contract_sha256": contract_sha256,
            "host": host,
            "request_sha256": request_sha256,
            "subject": json.loads(subject),
            "test_definition_sha256": test_definition_sha256,
        }
    )


def build_workflow_manifest(
    *,
    mode: str,
    request: Mapping[str, Any],
    request_sha256: str,
    request_asset_url: str,
    lifecycle: Mapping[str, Any],
    tap_plan: Mapping[str, Any],
    scheduling: SchedulingDecisionV1,
    candidate_locators: Mapping[str, Mapping[str, Any]],
    max_ready_subjects: int,
) -> dict[str, Any]:
    """Project one pure scheduler result into stable matrix-safe work IDs."""

    if mode not in {"observe", "active"}:
        raise WorkflowError("workflow mode is unsupported")
    _digest(request_sha256, "workflow request")
    if scheduling.request_sha256 != request_sha256:
        raise WorkflowError("scheduler and workflow name different requests")
    validate_tap_plan(tap_plan)
    if tap_plan["request_digest"] != request_sha256:
        raise WorkflowError("tap plan and workflow name different requests")
    if not 1 <= max_ready_subjects <= 256:
        raise WorkflowError("workflow ready-work bound is invalid")
    if len(scheduling.ready) > max_ready_subjects:
        raise WorkflowError("scheduler exceeded the protected ready-work bound")
    source = _exact(
        request.get("build_source"),
        frozenset({"repository", "commit", "tree"}),
        "request build source",
    )
    checked_source = {
        "repository": _text(source["repository"], "request repository", 256),
        "commit": _git_sha(source["commit"], "request commit"),
        "tree": _git_sha(source["tree"], "request tree"),
    }
    formulae = _formulae(tap_plan)
    build_work: list[dict[str, Any]] = []
    verify_work: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_class = "required"
    for ready in scheduling.ready:
        subject = _subject(ready.subject, "ready-work subject")
        formula = formulae.get(subject)
        if formula is None:
            raise WorkflowError("ready work names a Formula outside the tap plan")
        contract = _digest(ready.contract_sha256, "ready-work contract")
        if formula["contract_sha256"] != contract:
            raise WorkflowError("ready-work contract differs from the tap plan")
        if ready.work_class not in {"required", "background"}:
            raise WorkflowError("ready-work class is unsupported")
        if previous_class == "background" and ready.work_class == "required":
            raise WorkflowError("required work must sort before background work")
        previous_class = ready.work_class
        ordinal = _positive(ready.attempt_ordinal, "ready-work attempt", 3)
        work_id = _work_identity(
            request_sha256=request_sha256,
            subject=subject,
            action=ready.action,
            attempt_ordinal=ordinal,
            contract_sha256=contract,
            candidate_record_sha256=ready.candidate_record_sha256,
            test_definition_sha256=ready.test_definition_sha256,
            host=ready.host,
        )
        if work_id in seen:
            raise WorkflowError("scheduler produced duplicate work identity")
        seen.add(work_id)
        common = {
            "work_id": work_id,
            "work_class": ready.work_class,
            "subject": subject,
            "subject_sha256": hashlib.sha256(subject.encode()).hexdigest(),
            "attempt_ordinal": ordinal,
            "contract_sha256": contract,
            "formula_plan_sha256": canonical_sha256(formula),
        }
        if ready.action == "build-candidate":
            if any(
                value is not None
                for value in (
                    ready.candidate_record_sha256,
                    ready.test_definition_sha256,
                    ready.host,
                )
            ):
                raise WorkflowError("build work carries verification identity")
            build_work.append(
                {
                    **common,
                    "action": "build-candidate",
                    "artifact_name": f"abi-staging-build-{work_id}",
                }
            )
        elif ready.action == "verify-candidate":
            candidate_digest = _digest(
                ready.candidate_record_sha256, "verification candidate record"
            )
            test_digest = _digest(
                ready.test_definition_sha256, "verification test definition"
            )
            if ready.host not in {"build", "node", "browser"}:
                raise WorkflowError("verification work host is unsupported")
            locator_value = candidate_locators.get(candidate_digest)
            if locator_value is None:
                raise WorkflowError("verification work lacks an exact candidate locator")
            try:
                locator = parse_public_record_locator(locator_value)
            except OciPublicationError as error:
                raise WorkflowError(f"verification candidate locator is invalid: {error}") from error
            if locator["digest"] != "sha256:" + candidate_digest:
                raise WorkflowError("verification candidate locator differs from its record")
            verify_work.append(
                {
                    **common,
                    "action": "verify-candidate",
                    "artifact_name": f"abi-staging-verification-{work_id}",
                    "candidate_record_sha256": candidate_digest,
                    "candidate_locator": locator,
                    "test_definition_sha256": test_digest,
                    "host": ready.host,
                }
            )
        else:
            raise WorkflowError("ready-work action is unsupported")

    manifest = {
        "schema": 1,
        "kind": "kandelo-abi-staging-workflow-plan",
        "mode": mode,
        "request": {
            "sha256": request_sha256,
            "asset_url": _text(request_asset_url, "request asset URL"),
            "source": checked_source,
            "lifecycle": copy.deepcopy(dict(lifecycle)),
        },
        "tap_source": copy.deepcopy(tap_plan["tap_source"]),
        "tap_plan_sha256": canonical_sha256(tap_plan),
        "build_work": build_work,
        "verify_work": verify_work,
        "build_matrix": {
            "include": []
            if mode == "observe"
            else [{"work_id": item["work_id"]} for item in build_work]
        },
        "verify_matrix": {
            "include": []
            if mode == "observe"
            else [{"work_id": item["work_id"]} for item in verify_work]
        },
        "blocked": [dict(item.__dict__) for item in scheduling.blocked],
        "complete": list(scheduling.complete),
        "pending": list(scheduling.pending),
    }
    validate_workflow_manifest(manifest, max_ready_subjects=max_ready_subjects)
    return json.loads(canonical_bytes(manifest))


def validate_workflow_manifest(
    value: Mapping[str, Any], *, max_ready_subjects: int
) -> None:
    manifest = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "mode",
                "request",
                "tap_source",
                "tap_plan_sha256",
                "build_work",
                "verify_work",
                "build_matrix",
                "verify_matrix",
                "blocked",
                "complete",
                "pending",
            }
        ),
        "workflow plan",
    )
    if manifest["schema"] != 1 or manifest["kind"] != "kandelo-abi-staging-workflow-plan":
        raise WorkflowError("workflow plan protocol is unsupported")
    mode = manifest["mode"]
    if mode not in {"observe", "active"}:
        raise WorkflowError("workflow plan mode is unsupported")
    request = _exact(
        manifest["request"],
        frozenset({"sha256", "asset_url", "source", "lifecycle"}),
        "workflow request",
    )
    _digest(request["sha256"], "workflow request digest")
    _text(request["asset_url"], "workflow request asset URL")
    source = _exact(
        request["source"],
        frozenset({"repository", "commit", "tree"}),
        "workflow request source",
    )
    _text(source["repository"], "workflow request repository", 256)
    _git_sha(source["commit"], "workflow request commit")
    _git_sha(source["tree"], "workflow request tree")
    lifecycle = _exact(
        request["lifecycle"],
        frozenset({"state", "current_head", "merged_commit"}),
        "workflow request lifecycle",
    )
    if lifecycle["state"] not in {"open", "closed", "merged"}:
        raise WorkflowError("workflow lifecycle state is unsupported")
    for key in ("current_head", "merged_commit"):
        if lifecycle[key] is not None:
            _git_sha(lifecycle[key], f"workflow lifecycle {key}")
    tap = _exact(
        manifest["tap_source"],
        frozenset({"repository", "commit", "tree"}),
        "workflow tap source",
    )
    _text(tap["repository"], "workflow tap repository", 256)
    _git_sha(tap["commit"], "workflow tap commit")
    _git_sha(tap["tree"], "workflow tap tree")
    _digest(manifest["tap_plan_sha256"], "workflow tap plan")
    if not 1 <= max_ready_subjects <= 256:
        raise WorkflowError("workflow ready-work bound is invalid")

    seen: set[str] = set()
    total = 0
    for field, action in (
        ("build_work", "build-candidate"),
        ("verify_work", "verify-candidate"),
    ):
        entries = _sequence(manifest[field], field)
        total += len(entries)
        previous_class = "required"
        for index, candidate in enumerate(entries):
            base = {
                "work_id",
                "work_class",
                "subject",
                "subject_sha256",
                "attempt_ordinal",
                "contract_sha256",
                "formula_plan_sha256",
                "action",
                "artifact_name",
            }
            if action == "verify-candidate":
                base.update(
                    {
                        "candidate_record_sha256",
                        "candidate_locator",
                        "test_definition_sha256",
                        "host",
                    }
                )
            work = _exact(candidate, frozenset(base), f"{field} {index}")
            work_id = _digest(work["work_id"], f"{field} work ID")
            if work_id in seen:
                raise WorkflowError("workflow plan repeats a work ID")
            seen.add(work_id)
            if work["action"] != action:
                raise WorkflowError(f"{field} action changed")
            if work["work_class"] not in {"required", "background"}:
                raise WorkflowError(f"{field} work class is unsupported")
            if previous_class == "background" and work["work_class"] == "required":
                raise WorkflowError("workflow required work follows background work")
            previous_class = work["work_class"]
            subject = _subject(work["subject"], f"{field} subject")
            if work["subject_sha256"] != hashlib.sha256(subject.encode()).hexdigest():
                raise WorkflowError(f"{field} subject digest differs")
            _positive(work["attempt_ordinal"], f"{field} attempt", 3)
            _digest(work["contract_sha256"], f"{field} contract")
            _digest(work["formula_plan_sha256"], f"{field} Formula plan")
            if (
                not isinstance(work["artifact_name"], str)
                or WORK_ARTIFACT.fullmatch(work["artifact_name"]) is None
                or not work["artifact_name"].endswith(work_id)
            ):
                raise WorkflowError(f"{field} artifact name is not content addressed")
            if action == "verify-candidate":
                candidate_digest = _digest(
                    work["candidate_record_sha256"], "verification candidate"
                )
                _digest(work["test_definition_sha256"], "verification definition")
                if work["host"] not in {"build", "node", "browser"}:
                    raise WorkflowError("verification host is unsupported")
                try:
                    locator = parse_public_record_locator(work["candidate_locator"])
                except OciPublicationError as error:
                    raise WorkflowError(f"verification locator is invalid: {error}") from error
                if locator["digest"] != "sha256:" + candidate_digest:
                    raise WorkflowError("verification locator differs from candidate")
    if total > max_ready_subjects:
        raise WorkflowError("workflow plan exceeds the protected ready-work bound")

    for matrix_field, work_field in (
        ("build_matrix", "build_work"),
        ("verify_matrix", "verify_work"),
    ):
        matrix = _exact(manifest[matrix_field], frozenset({"include"}), matrix_field)
        include = _sequence(matrix["include"], f"{matrix_field} include")
        actual = []
        for index, candidate in enumerate(include):
            item = _exact(candidate, frozenset({"work_id"}), f"{matrix_field} item {index}")
            actual.append({"work_id": _digest(item["work_id"], f"{matrix_field} work ID")})
        expected = (
            []
            if mode == "observe"
            else [{"work_id": item["work_id"]} for item in manifest[work_field]]
        )
        if actual != expected:
            raise WorkflowError(f"{matrix_field} differs from protected work")
    for field in ("blocked", "complete", "pending"):
        values = _sequence(manifest[field], f"workflow {field}")
        if len(values) > 65_536:
            raise WorkflowError(f"workflow {field} exceeds its bound")
    if len(canonical_bytes(manifest)) > 16 * 1024 * 1024:
        raise WorkflowError("workflow plan exceeds its byte bound")
