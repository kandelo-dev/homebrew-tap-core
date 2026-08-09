"""Exact maintainer authorization, risk acceptance, and retry maintenance."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .canonical import CanonicalJsonError, canonical_bytes, canonical_sha256, parse_canonical_bytes
from .plan import exact_formula_subject
from .oci import isolated_oras_transport, publish_record
from .policy import TapStagingPolicyV1, candidate_repository, load_tap_staging_policy
from .records import OciBlobV1, OciRecordPlanV1, validate_candidate_record
from .scheduler import AttemptFactV1


CAPTURE_AUTHORIZATION_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.capture-override-authorization.v1+json"
)
OVERRIDE_RECEIPT_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.override-receipt.v1+json"
)
MANUAL_RETRY_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.manual-retry.v1+json"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
MAX_GUARDS = 256
MAX_EVIDENCE_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024


class OverrideError(ValueError):
    """Raised when maintenance would broaden or contradict exact authority."""


@dataclass(frozen=True)
class GuardDefinitionV1:
    code: str
    default_disposition: str
    override_policy: str
    recovery_policy: str
    summary: str


@dataclass(frozen=True)
class GuardRegistryV1:
    schema: int
    kind: str
    version: int
    sha256: str
    guards: Mapping[str, GuardDefinitionV1]


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OverrideError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise OverrideError(f"{field} must be an array")
    return value


def _exact(value: Any, fields: frozenset[str], field: str) -> Mapping[str, Any]:
    result = _mapping(value, field)
    if frozenset(result) != fields:
        raise OverrideError(f"{field} fields changed")
    return result


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise OverrideError(f"{field} must be a string")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise OverrideError(f"{field} is not UTF-8") from error
    if not 1 <= size <= maximum or "\0" in value:
        raise OverrideError(f"{field} is outside its bound")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise OverrideError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise OverrideError(f"{field} is not a full lowercase Git SHA")
    return value


def _stable_id(value: Any, field: str) -> str:
    text = _text(value, field, 128)
    if STABLE_ID.fullmatch(text) is None:
        raise OverrideError(f"{field} is not a stable identifier")
    return text


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**64 - 1:
        raise OverrideError(f"{field} is not a bounded positive integer")
    return value


def _justification(value: Any) -> str:
    result = _text(value, "override justification", 2048)
    if not result.strip():
        raise OverrideError("override justification cannot be blank")
    return result


def _run(value: Any) -> dict[str, Any]:
    run = _exact(
        value,
        frozenset({"repository", "workflow_ref", "run_id", "run_attempt", "job"}),
        "maintenance run",
    )
    repository = _text(run["repository"], "maintenance run repository", 255)
    if REPOSITORY.fullmatch(repository) is None:
        raise OverrideError("maintenance run repository is not owner/name")
    workflow_ref = _text(run["workflow_ref"], "maintenance workflow ref", 2048)
    if not workflow_ref.endswith("@refs/heads/main"):
        raise OverrideError("maintenance workflow must execute protected main code")
    return {
        "repository": repository,
        "workflow_ref": workflow_ref,
        "run_id": _positive(run["run_id"], "maintenance run ID"),
        "run_attempt": _positive(run["run_attempt"], "maintenance run attempt"),
        "job": _stable_id(run["job"], "maintenance job"),
    }


def _maintainer(value: Any) -> dict[str, str]:
    maintainer = _exact(
        value,
        frozenset({"login", "permission", "authorization_reference"}),
        "maintainer authorization",
    )
    login = _stable_id(maintainer["login"], "maintainer login")
    permission = _text(maintainer["permission"], "maintainer permission", 128)
    if permission not in {"maintain", "admin"}:
        raise OverrideError("maintainer permission must be maintain or admin")
    reference = _text(
        maintainer["authorization_reference"],
        "maintainer authorization reference",
        2048,
    )
    if not reference.startswith("https://github.com/"):
        raise OverrideError("maintainer authorization reference is not a GitHub URL")
    return {
        "login": login,
        "permission": permission,
        "authorization_reference": reference,
    }


def load_guard_registry(
    body: bytes, *, expected_version: int, expected_sha256: str
) -> GuardRegistryV1:
    """Load the exact request-bound guard authority without copying an allowlist."""

    _digest(expected_sha256, "expected guard-registry digest")
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
        raise OverrideError("expected guard-registry version must be positive")
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        raise OverrideError("guard-registry digest differs from the exact request")
    try:
        value = _plain(parse_canonical_bytes(body, maximum_bytes=4 * 1024 * 1024))
    except CanonicalJsonError as error:
        raise OverrideError(f"guard registry is not canonical: {error}") from error
    registry = _exact(
        value,
        frozenset({"schema", "kind", "version", "guards"}),
        "guard registry",
    )
    if (
        registry["schema"] != 1
        or registry["kind"] != "kandelo-abi-staging-guard-codes"
        or registry["version"] != expected_version
    ):
        raise OverrideError("guard-registry identity differs from the exact request")
    raw_guards = _sequence(registry["guards"], "guard registry entries")
    if not 1 <= len(raw_guards) <= MAX_GUARDS:
        raise OverrideError("guard registry entry count is outside its bound")
    guards: dict[str, GuardDefinitionV1] = {}
    previous = ""
    for index, candidate in enumerate(raw_guards):
        guard = _exact(
            candidate,
            frozenset(
                {
                    "code",
                    "default_disposition",
                    "override_policy",
                    "recovery_policy",
                    "summary",
                }
            ),
            f"guard registry entry {index}",
        )
        code = _stable_id(guard["code"], f"guard registry entry {index} code")
        if code <= previous:
            raise OverrideError("guard registry codes must be sorted and duplicate-free")
        previous = code
        override_policy = _stable_id(
            guard["override_policy"], f"guard {code} override policy"
        )
        if override_policy not in {"never", "exact-subject-build-risk", "exact-artifact"}:
            raise OverrideError(f"guard {code} has an unsupported override policy")
        guards[code] = GuardDefinitionV1(
            code=code,
            default_disposition=_stable_id(
                guard["default_disposition"], f"guard {code} disposition"
            ),
            override_policy=override_policy,
            recovery_policy=_stable_id(
                guard["recovery_policy"], f"guard {code} recovery policy"
            ),
            summary=_text(guard["summary"], f"guard {code} summary", 1024),
        )
    return GuardRegistryV1(
        schema=1,
        kind="kandelo-abi-staging-guard-codes",
        version=expected_version,
        sha256=expected_sha256,
        guards=MappingProxyType(guards),
    )


def _request_context(
    request: Any, request_sha256: str
) -> tuple[dict[str, str], int, dict[str, Any]]:
    value = _exact(
        request,
        frozenset(
            {
                "schema",
                "kind",
                "pull_request",
                "build_source",
                "target_abi",
                "requirements",
                "issuance",
                "informational_context",
            }
        ),
        "staging request",
    )
    _digest(request_sha256, "request digest")
    if canonical_sha256(value) != request_sha256:
        raise OverrideError("request digest differs from canonical request bytes")
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-request":
        raise OverrideError("staging request identity is unsupported")
    source = _exact(
        value["build_source"],
        frozenset({"repository", "commit", "tree"}),
        "request build source",
    )
    repository = _text(source["repository"], "request source repository", 255)
    if REPOSITORY.fullmatch(repository) is None:
        raise OverrideError("request source repository is not owner/name")
    checked_source = {
        "repository": repository,
        "commit": _git_sha(source["commit"], "request source commit"),
        "tree": _git_sha(source["tree"], "request source tree"),
    }
    target = _exact(
        value["target_abi"], frozenset({"version", "snapshot_sha256"}), "request target ABI"
    )
    target_abi = _positive(target["version"], "request target ABI version")
    if target_abi > 2**32 - 1:
        raise OverrideError("request target ABI does not fit an unsigned 32-bit integer")
    _digest(target["snapshot_sha256"], "request ABI snapshot")
    issuance = _mapping(value["issuance"], "request issuance")
    required = {
        "issuer_repository",
        "issuer_workflow_ref",
        "policy_version",
        "policy_sha256",
        "guard_registry_version",
        "guard_registry_sha256",
        "authorization",
    }
    if set(issuance) != required:
        raise OverrideError("request issuance fields changed")
    policy = {
        "policy_version": _positive(issuance["policy_version"], "request policy version"),
        "policy_sha256": _digest(issuance["policy_sha256"], "request policy digest"),
        "guard_registry_version": _positive(
            issuance["guard_registry_version"], "request guard-registry version"
        ),
        "guard_registry_sha256": _digest(
            issuance["guard_registry_sha256"], "request guard-registry digest"
        ),
    }
    authorization = _mapping(issuance["authorization"], "request authorization")
    if authorization.get("head") != checked_source["commit"]:
        raise OverrideError("request authorization differs from exact source head")
    return checked_source, target_abi, policy


def _formula(
    value: Any,
    *,
    target_abi: int,
    tap_repository: str,
    field: str,
) -> dict[str, Any]:
    formula = _exact(
        value,
        frozenset(
            {"tap", "formula", "architecture", "target_abi", "bottle_contract_sha256"}
        ),
        field,
    )
    tap = _text(formula["tap"], f"{field} tap", 255)
    if tap != tap_repository:
        raise OverrideError(f"{field} differs from the protected tap")
    name = _stable_id(formula["formula"], f"{field} name")
    architecture = _text(formula["architecture"], f"{field} architecture", 32)
    if architecture not in {"wasm32", "wasm64"}:
        raise OverrideError(f"{field} architecture is unsupported")
    if formula["target_abi"] != target_abi:
        raise OverrideError(f"{field} differs from the exact request target ABI")
    return {
        "tap": tap,
        "formula": name,
        "architecture": architecture,
        "target_abi": target_abi,
        "bottle_contract_sha256": _digest(
            formula["bottle_contract_sha256"], f"{field} bottle contract"
        ),
    }


def _guard(
    registry: GuardRegistryV1, code: Any, *, required_policy: str | None = None
) -> GuardDefinitionV1:
    if not isinstance(code, str) or code not in registry.guards:
        raise OverrideError("unknown guard code is not in the exact request registry")
    guard = registry.guards[code]
    if guard.override_policy == "never":
        raise OverrideError(f"guard {code} can never be overridden")
    if required_policy is not None and guard.override_policy != required_policy:
        raise OverrideError(f"guard {code} is not an allowed {required_policy} guard")
    return guard


def _check_registry_policy(
    registry: GuardRegistryV1, policy: Mapping[str, Any]
) -> None:
    if (
        registry.version != policy["guard_registry_version"]
        or registry.sha256 != policy["guard_registry_sha256"]
    ):
        raise OverrideError("guard registry differs from exact request policy")


def _common(
    *,
    request_sha256: str,
    subject: Mapping[str, Any],
    source: Mapping[str, str],
    run: Mapping[str, Any],
    guard_codes: Sequence[str],
    artifact: Mapping[str, Any] | None,
    promotion_state: str,
) -> dict[str, Any]:
    common: dict[str, Any] = {
        "request_sha256": request_sha256,
        "subject": dict(subject),
        "source": dict(source),
        "run": dict(run),
        "guard_codes": list(guard_codes),
        "work_state": "complete",
        "outcome": "success",
        "artifact_class": "none" if artifact is None else "candidate",
        "promotion_state": promotion_state,
        "retry_state": {
            "attempts": 1,
            "eligible": False,
            "exhausted": False,
            "next_action": "none",
        },
        "blockers": [],
    }
    if artifact is not None:
        common["artifact"] = dict(artifact)
    return common


def authorize_capture(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    formula: Mapping[str, Any],
    guard_code: str,
    guard_registry: GuardRegistryV1,
    maintainer: Mapping[str, Any],
    justification: str,
    run: Mapping[str, Any],
    tap_repository: str,
    expected_formula: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a pre-build authorization with no guessed candidate identity."""

    source, target_abi, policy = _request_context(request, request_sha256)
    _check_registry_policy(guard_registry, policy)
    checked_formula = _formula(
        formula,
        target_abi=target_abi,
        tap_repository=tap_repository,
        field="Formula subject",
    )
    if expected_formula is not None:
        planned = _formula(
            expected_formula,
            target_abi=target_abi,
            tap_repository=tap_repository,
            field="planned Formula subject",
        )
        if checked_formula != planned:
            raise OverrideError("Formula subject differs from the exact protected plan")
    guard = _guard(guard_registry, guard_code)
    if (
        guard.code != "build_input_capture_incomplete"
        or guard.override_policy != "exact-subject-build-risk"
    ):
        raise OverrideError("capture guard must be build_input_capture_incomplete")
    checked_maintainer = _maintainer(maintainer)
    record = {
        "schema": 1,
        "kind": "kandelo-abi-staging-capture-override-authorization",
        "common": _common(
            request_sha256=request_sha256,
            subject={
                "kind": "formula",
                "identity": f"{tap_repository}/{checked_formula['formula']}",
                "architecture": checked_formula["architecture"],
            },
            source=source,
            run=_run(run),
            guard_codes=(guard.code,),
            artifact=None,
            promotion_state="unknown",
        ),
        "capture_authorization": {
            "formula": checked_formula,
            "guard_code": guard.code,
            "maintainer": checked_maintainer,
            "justification": _justification(justification),
            "policy": policy,
        },
    }
    validate_capture_authorization(
        record,
        request=request,
        request_sha256=request_sha256,
        guard_registry=guard_registry,
        tap_repository=tap_repository,
        expected_formula=checked_formula,
    )
    return record


def validate_capture_authorization(
    record: Any,
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    guard_registry: GuardRegistryV1,
    tap_repository: str,
    expected_formula: Mapping[str, Any],
) -> dict[str, Any]:
    value = _exact(
        record,
        frozenset({"schema", "kind", "common", "capture_authorization"}),
        "capture authorization",
    )
    if (
        value["schema"] != 1
        or value["kind"] != "kandelo-abi-staging-capture-override-authorization"
    ):
        raise OverrideError("capture authorization identity is unsupported")
    source, target_abi, policy = _request_context(request, request_sha256)
    _check_registry_policy(guard_registry, policy)
    payload = _exact(
        value["capture_authorization"],
        frozenset({"formula", "guard_code", "maintainer", "justification", "policy"}),
        "capture authorization payload",
    )
    formula = _formula(
        payload["formula"],
        target_abi=target_abi,
        tap_repository=tap_repository,
        field="capture authorization Formula",
    )
    planned = _formula(
        expected_formula,
        target_abi=target_abi,
        tap_repository=tap_repository,
        field="planned Formula subject",
    )
    if formula != planned:
        raise OverrideError("capture authorization differs from exact Formula subject")
    guard = _guard(
        guard_registry,
        payload["guard_code"],
        required_policy="exact-subject-build-risk",
    )
    if guard.code != "build_input_capture_incomplete":
        raise OverrideError("capture authorization names the wrong capture guard")
    common = _mapping(value["common"], "capture authorization common")
    expected_subject = {
        "kind": "formula",
        "identity": f"{tap_repository}/{formula['formula']}",
        "architecture": formula["architecture"],
    }
    if (
        common.get("request_sha256") != request_sha256
        or common.get("subject") != expected_subject
        or common.get("source") != source
        or common.get("guard_codes") != [guard.code]
        or common.get("work_state") != "complete"
        or common.get("outcome") != "success"
        or common.get("artifact_class") != "none"
        or "artifact" in common
        or common.get("promotion_state") != "unknown"
        or common.get("blockers") != []
    ):
        raise OverrideError("capture authorization common subject or state is contradictory")
    if payload["policy"] != policy:
        raise OverrideError("capture authorization policy differs from exact request")
    _maintainer(payload["maintainer"])
    _justification(payload["justification"])
    _run(common.get("run"))
    return _plain(value)


def _candidate_formula(candidate: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(candidate.get("candidate"), "exact candidate payload")
    raw = _mapping(payload.get("formula"), "exact candidate Formula")
    return {
        key: raw[key]
        for key in ("tap", "formula", "architecture", "target_abi", "bottle_contract_sha256")
        if key in raw
    }


def accept_artifact_risk(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    candidate: Mapping[str, Any] | None,
    candidate_record_sha256: str,
    accepted_guard_codes: Sequence[str],
    guard_registry: GuardRegistryV1,
    maintainer: Mapping[str, Any],
    justification: str,
    run: Mapping[str, Any],
    tap_repository: str,
    capture_authorization: Mapping[str, Any] | None = None,
    capture_authorization_sha256: str | None = None,
) -> dict[str, Any]:
    """Accept an allowed risk only for one already-existing exact candidate."""

    source, target_abi, policy = _request_context(request, request_sha256)
    _check_registry_policy(guard_registry, policy)
    _digest(candidate_record_sha256, "candidate record digest")
    if not isinstance(candidate, Mapping) or not candidate:
        raise OverrideError("artifact override requires an exact candidate")
    try:
        validate_candidate_record(candidate)
    except (KeyError, TypeError, ValueError) as error:
        raise OverrideError(f"artifact override exact candidate is invalid: {error}") from error
    common = _mapping(candidate.get("common"), "exact candidate common")
    payload = _mapping(candidate.get("candidate"), "exact candidate payload")
    layer = _mapping(payload.get("bottle_layer"), "exact candidate bottle layer")
    if (
        common.get("request_sha256") != request_sha256
        or common.get("source") != source
        or common.get("artifact") != layer
    ):
        raise OverrideError("exact candidate differs from the exact request or bottle layer")
    formula = _formula(
        _candidate_formula(candidate),
        target_abi=target_abi,
        tap_repository=tap_repository,
        field="exact candidate Formula subject",
    )
    if (
        not accepted_guard_codes
        or len(accepted_guard_codes) > MAX_GUARDS
        or list(accepted_guard_codes) != sorted(set(accepted_guard_codes))
    ):
        raise OverrideError("accepted guard codes must be sorted and duplicate-free")
    guards = [
        _guard(guard_registry, code)
        for code in accepted_guard_codes
    ]
    policies = {guard.override_policy for guard in guards}
    if policies - {"exact-artifact", "exact-subject-build-risk"}:
        raise OverrideError("accepted guard policy is unsupported")
    capture_code = "build_input_capture_incomplete"
    if capture_code in accepted_guard_codes:
        if capture_authorization is None or capture_authorization_sha256 is None:
            raise OverrideError("capture authorization is required for postbuild risk acceptance")
        _digest(capture_authorization_sha256, "capture authorization digest")
        validate_capture_authorization(
            capture_authorization,
            request=request,
            request_sha256=request_sha256,
            guard_registry=guard_registry,
            tap_repository=tap_repository,
            expected_formula=formula,
        )
    elif capture_authorization is not None or capture_authorization_sha256 is not None:
        raise OverrideError("capture authorization is valid only for incomplete capture")
    record = {
        "schema": 1,
        "kind": "kandelo-abi-staging-override-receipt",
        "common": _common(
            request_sha256=request_sha256,
            subject={"kind": "candidate", "identity": candidate_record_sha256},
            source=source,
            run=_run(run),
            guard_codes=accepted_guard_codes,
            artifact=layer,
            promotion_state="accepted-with-override",
        ),
        "override_receipt": {
            "accepted_guard_codes": list(accepted_guard_codes),
            "maintainer": _maintainer(maintainer),
            "justification": _justification(justification),
            "policy": policy,
            "candidate_record_sha256": candidate_record_sha256,
            "bottle_layer": _plain(layer),
        },
    }
    if capture_authorization_sha256 is not None:
        record["override_receipt"][
            "capture_authorization_sha256"
        ] = capture_authorization_sha256
    return record


def _record_plan(
    record: Mapping[str, Any],
    *,
    repository: str,
    media_type: str,
    role: str,
    title: str,
    annotations: Mapping[str, str],
) -> OciRecordPlanV1:
    body = canonical_bytes(record)
    return OciRecordPlanV1(
        repository=repository,
        artifact_type=media_type,
        config=OciBlobV1(
            role=role,
            media_type=media_type,
            body=body,
            title=title,
        ),
        layers=(
            OciBlobV1(
                role="immutable-record-bytes",
                media_type=media_type,
                body=body,
                title=title,
            ),
        ),
        annotations=dict(annotations),
    )


def build_capture_authorization_oci_plan(
    record: Mapping[str, Any], *, policy: TapStagingPolicyV1
) -> OciRecordPlanV1:
    payload = _mapping(record.get("capture_authorization"), "capture authorization payload")
    formula = _mapping(payload.get("formula"), "capture authorization Formula")
    name = _stable_id(formula.get("formula"), "capture authorization Formula name")
    target_abi = _positive(formula.get("target_abi"), "capture authorization target ABI")
    base = candidate_repository(policy, target_abi, formula=name)
    return _record_plan(
        record,
        repository=f"{base}/authorizations/capture",
        media_type=CAPTURE_AUTHORIZATION_MEDIA_TYPE,
        role="capture-authorization",
        title="capture-override-authorization.json",
        annotations={
            "dev.kandelo.abi-staging.classification": "maintainer-risk-authorization",
            "dev.kandelo.abi-staging.kind": "capture-override-authorization",
            "dev.kandelo.abi-staging.request-sha256": str(
                _mapping(record.get("common"), "capture authorization common").get(
                    "request_sha256"
                )
            ),
            "org.opencontainers.image.source": "https://github.com/" + policy.tap_repository,
        },
    )


def build_override_receipt_oci_plan(
    record: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    policy: TapStagingPolicyV1,
) -> OciRecordPlanV1:
    formula = _mapping(
        _mapping(candidate.get("candidate"), "exact candidate payload").get("formula"),
        "exact candidate Formula",
    )
    name = _stable_id(formula.get("formula"), "exact candidate Formula name")
    target_abi = _positive(formula.get("target_abi"), "exact candidate target ABI")
    base = candidate_repository(policy, target_abi, formula=name)
    payload = _mapping(record.get("override_receipt"), "override receipt payload")
    candidate_digest = _digest(
        payload.get("candidate_record_sha256"), "override candidate record digest"
    )
    return _record_plan(
        record,
        repository=f"{base}/receipts/overrides",
        media_type=OVERRIDE_RECEIPT_MEDIA_TYPE,
        role="override-receipt",
        title="override-receipt.json",
        annotations={
            "dev.kandelo.abi-staging.candidate-record-sha256": candidate_digest,
            "dev.kandelo.abi-staging.classification": "maintainer-risk-acceptance",
            "dev.kandelo.abi-staging.kind": "override-receipt",
            "org.opencontainers.image.source": "https://github.com/" + policy.tap_repository,
        },
    )


def retry_exhausted(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    attempts: Sequence[AttemptFactV1],
    guard_registry: GuardRegistryV1,
    maintainer: Mapping[str, Any],
    justification: str,
    tap_repository: str,
) -> dict[str, Any]:
    """Authorize one later ordinal without changing any prior record or eligibility."""

    _, target_abi, policy = _request_context(request, request_sha256)
    _check_registry_policy(guard_registry, policy)
    transient = guard_registry.guards.get("transient_infrastructure_failure")
    if (
        transient is None
        or transient.override_policy != "never"
        or transient.recovery_policy != "manual-retry-after-exhaustion"
    ):
        raise OverrideError("transient retry maintenance policy is unavailable")
    if len(attempts) != 4 or [item.retry_ordinal for item in attempts] != [0, 1, 2, 3]:
        raise OverrideError("exhausted retry history requires exact ordinals 0 through 3")
    first = attempts[0]
    if any(
        item.request_sha256 != request_sha256
        or item.request_sha256 != first.request_sha256
        or item.subject != first.subject
        or item.contract_sha256 != first.contract_sha256
        or item.guard_code != "transient_infrastructure_failure"
        or item.outcome == "success"
        for item in attempts
    ):
        raise OverrideError("retry request does not bind one exact exhausted transient history")
    record_digests = [item.record_sha256 for item in attempts]
    if len(record_digests) != len(set(record_digests)):
        raise OverrideError("exhausted retry history repeats an attempt record")
    parsed_subject = _mapping_from_subject(first.subject)
    subject = exact_formula_subject(
        parsed_subject["identity"], parsed_subject["architecture"]
    )
    if subject != first.subject:
        raise OverrideError("exhausted retry subject is not canonical")
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-manual-retry",
        "request_sha256": request_sha256,
        "subject": _mapping_from_subject(subject),
        "formula": {
            "tap": _text(tap_repository, "manual retry tap repository", 255),
            "formula": parsed_subject["identity"],
            "architecture": parsed_subject["architecture"],
            "target_abi": target_abi,
            "bottle_contract_sha256": first.contract_sha256,
        },
        "bottle_contract_sha256": first.contract_sha256,
        "prior_attempt_record_sha256s": record_digests,
        "previous_ordinal": 3,
        "next_ordinal": 4,
        "maintainer": _maintainer(maintainer),
        "justification": _justification(justification),
        "policy": policy,
    }


def _mapping_from_subject(value: str) -> dict[str, str]:
    try:
        parsed = _plain(parse_canonical_bytes((value + "\n").encode(), maximum_bytes=512))
    except CanonicalJsonError as error:
        raise OverrideError(f"exact Formula subject is invalid: {error}") from error
    subject = _exact(
        parsed,
        frozenset({"kind", "identity", "architecture"}),
        "exact Formula subject",
    )
    if subject["kind"] != "formula":
        raise OverrideError("exact retry subject is not a Formula")
    return {
        "kind": "formula",
        "identity": _stable_id(subject["identity"], "exact Formula subject identity"),
        "architecture": _text(subject["architecture"], "exact Formula architecture", 32),
    }


@dataclass(frozen=True)
class MaintenanceEvidenceV1:
    operation: str
    request: Mapping[str, Any]
    request_sha256: str
    guard_registry: GuardRegistryV1
    payload: Mapping[str, Any]


def load_maintenance_evidence(body: bytes, *, operation: str) -> MaintenanceEvidenceV1:
    """Parse one protected, canonical, operation-specific evidence document."""

    if operation not in {
        "authorize-capture",
        "accept-artifact-risk",
        "retry-exhausted",
    }:
        raise OverrideError("maintenance operation is unsupported")
    try:
        value = _plain(parse_canonical_bytes(body, maximum_bytes=MAX_EVIDENCE_BYTES))
    except CanonicalJsonError as error:
        raise OverrideError(f"maintenance evidence is not canonical: {error}") from error
    base = {
        "schema",
        "kind",
        "operation",
        "request",
        "request_sha256",
        "guard_registry",
    }
    additions = {
        "authorize-capture": {"formula", "guard_code"},
        "accept-artifact-risk": {
            "candidate",
            "candidate_record_sha256",
            "accepted_guard_codes",
            "capture_authorization",
            "capture_authorization_sha256",
        },
        "retry-exhausted": {"attempts"},
    }
    evidence = _exact(value, frozenset(base | additions[operation]), "maintenance evidence")
    if (
        evidence["schema"] != 1
        or evidence["kind"] != "kandelo-abi-staging-maintenance-evidence"
        or evidence["operation"] != operation
    ):
        raise OverrideError("maintenance evidence identity differs from the command")
    request = _mapping(evidence["request"], "maintenance evidence request")
    request_sha256 = _digest(evidence["request_sha256"], "maintenance evidence request")
    _, _, policy = _request_context(request, request_sha256)
    guard_body = canonical_bytes(
        _mapping(evidence["guard_registry"], "maintenance evidence guard registry")
    )
    registry = load_guard_registry(
        guard_body,
        expected_version=policy["guard_registry_version"],
        expected_sha256=policy["guard_registry_sha256"],
    )
    payload = {key: evidence[key] for key in additions[operation]}
    if operation == "authorize-capture":
        if payload["guard_code"] != "build_input_capture_incomplete":
            raise OverrideError("capture evidence names the wrong guard")
        _mapping(payload["formula"], "capture evidence Formula")
    elif operation == "accept-artifact-risk":
        _mapping(payload["candidate"], "artifact-risk exact candidate")
        _digest(payload["candidate_record_sha256"], "artifact-risk candidate record")
        guards = _sequence(payload["accepted_guard_codes"], "artifact-risk guards")
        if not guards:
            raise OverrideError("artifact-risk evidence has no accepted guard")
        for guard in guards:
            _stable_id(guard, "artifact-risk guard")
        authorization = payload["capture_authorization"]
        authorization_sha256 = payload["capture_authorization_sha256"]
        if (authorization is None) != (authorization_sha256 is None):
            raise OverrideError("artifact-risk capture authorization identity is incomplete")
        if authorization is not None:
            _mapping(authorization, "artifact-risk capture authorization")
            _digest(authorization_sha256, "artifact-risk capture authorization")
    else:
        attempts = _sequence(payload["attempts"], "manual retry attempts")
        if len(attempts) != 4:
            raise OverrideError("manual retry evidence must contain exact exhausted history")
    return MaintenanceEvidenceV1(
        operation=operation,
        request=_plain(request),
        request_sha256=request_sha256,
        guard_registry=registry,
        payload=MappingProxyType(_plain(payload)),
    )


def load_maintenance_evidence_archive(
    archive: bytes, *, expected_sha256: str, operation: str
) -> MaintenanceEvidenceV1:
    """Validate one immutable Actions ZIP without extracting untrusted paths."""

    expected = _digest(expected_sha256, "maintenance evidence artifact digest")
    if not 1 <= len(archive) <= MAX_EVIDENCE_ARCHIVE_BYTES:
        raise OverrideError("maintenance evidence artifact is outside its byte bound")
    if hashlib.sha256(archive).hexdigest() != expected:
        raise OverrideError("maintenance evidence artifact differs from its exact digest")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            if len(entries) != 1:
                raise OverrideError("maintenance evidence artifact must contain one file")
            entry = entries[0]
            if (
                entry.filename != "maintenance-evidence.json"
                or entry.is_dir()
                or entry.file_size < 1
                or entry.file_size > MAX_EVIDENCE_BYTES
                or entry.compress_size > MAX_EVIDENCE_ARCHIVE_BYTES
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise OverrideError("maintenance evidence artifact inventory is unsafe")
            body = bundle.read(entry)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        if isinstance(error, OverrideError):
            raise
        raise OverrideError(f"maintenance evidence artifact is invalid: {error}") from error
    if len(body) != entry.file_size:
        raise OverrideError("maintenance evidence changed while reading")
    return load_maintenance_evidence(body, operation=operation)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        return None


class GitHubMaintenanceClientV1:
    """Bounded authenticated reader for permission and protected-run artifacts."""

    def __init__(self, repository: str, token: str, *, opener=None) -> None:
        if REPOSITORY.fullmatch(repository) is None:
            raise OverrideError("maintenance GitHub repository is invalid")
        if not token or any(character.isspace() for character in token):
            raise OverrideError("maintenance GitHub token is missing or malformed")
        self.repository = repository
        self._token = token
        if opener is None:
            built = urllib.request.build_opener(_NoRedirect())
            self._opener = lambda request: built.open(request, timeout=30)
        else:
            self._opener = opener

    def _open(
        self,
        url: str,
        *,
        authenticated: bool,
        maximum: int,
        accept: str,
    ) -> tuple[int, Mapping[str, str], bytes]:
        headers = {
            "Accept": accept,
            "User-Agent": "kandelo-abi-staging-maintenance/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if authenticated:
            headers["Authorization"] = "Bearer " + self._token
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self._opener(request)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError) as error:
            raise OverrideError(f"maintenance GitHub request failed: {error}") from error
        try:
            status = int(response.status)
            body = response.read(maximum + 1)
            if len(body) > maximum:
                raise OverrideError("maintenance GitHub response exceeded its byte bound")
            response_headers = {
                str(key).lower(): str(value) for key, value in response.headers.items()
            }
            length = response_headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) != len(body)):
                raise OverrideError("maintenance GitHub response length is contradictory")
            return status, response_headers, body
        finally:
            response.close()

    def _api_json(self, path: str) -> Mapping[str, Any]:
        url = "https://api.github.com" + path
        status, _, body = self._open(
            url,
            authenticated=True,
            maximum=MAX_EVIDENCE_BYTES,
            accept="application/vnd.github+json",
        )
        if status != 200:
            raise OverrideError(f"maintenance GitHub API returned HTTP {status}")
        try:
            value = json.loads(body.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OverrideError(f"maintenance GitHub API returned invalid JSON: {error}") from error
        return _mapping(value, "maintenance GitHub API response")

    def maintainer(self, actor: str, authorization_reference: str) -> dict[str, str]:
        normalized_actor = _stable_id(actor.lower(), "maintenance actor")
        owner, repository = self.repository.split("/", 1)
        path = (
            f"/repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(repository, safe='')}/collaborators/"
            f"{urllib.parse.quote(actor, safe='')}/permission"
        )
        value = self._api_json(path)
        user = _mapping(value.get("user"), "collaborator permission user")
        login = _text(user.get("login"), "collaborator permission login", 128)
        if login.lower() != normalized_actor:
            raise OverrideError("collaborator permission response names another actor")
        return _maintainer(
            {
                "login": normalized_actor,
                "permission": value.get("permission"),
                "authorization_reference": authorization_reference,
            }
        )

    def evidence_artifact(
        self,
        artifact_id: int,
        *,
        expected_sha256: str,
    ) -> bytes:
        artifact_id = _positive(artifact_id, "maintenance evidence artifact ID")
        expected = _digest(expected_sha256, "maintenance evidence artifact digest")
        metadata = self._api_json(
            f"/repos/{self.repository}/actions/artifacts/{artifact_id}"
        )
        if (
            metadata.get("id") != artifact_id
            or metadata.get("expired") is not False
            or metadata.get("digest") != "sha256:" + expected
            or not isinstance(metadata.get("name"), str)
            or re.fullmatch(
                r"abi-staging-maintenance-evidence-sha256-[0-9a-f]{64}",
                metadata["name"],
            )
            is None
        ):
            raise OverrideError("maintenance artifact metadata is not exact and immutable")
        size = metadata.get("size_in_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_EVIDENCE_ARCHIVE_BYTES:
            raise OverrideError("maintenance artifact metadata size is outside its bound")
        workflow = _mapping(metadata.get("workflow_run"), "maintenance artifact workflow")
        run_id = _positive(workflow.get("id"), "maintenance evidence run ID")
        run = self._api_json(f"/repos/{self.repository}/actions/runs/{run_id}")
        head_repository = _mapping(
            run.get("head_repository"), "maintenance evidence head repository"
        )
        if (
            run.get("id") != run_id
            or run.get("event") not in {"schedule", "workflow_dispatch"}
            or run.get("head_branch") != "main"
            or run.get("head_sha") != workflow.get("head_sha")
            or run.get("path") != ".github/workflows/abi-staging-reconcile.yml"
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or head_repository.get("full_name") != self.repository
        ):
            raise OverrideError("maintenance evidence did not come from protected reconciliation")
        download = (
            f"https://api.github.com/repos/{self.repository}/actions/artifacts/"
            f"{artifact_id}/zip"
        )
        status, headers, body = self._open(
            download,
            authenticated=True,
            maximum=1024,
            accept="application/vnd.github+json",
        )
        if status not in {301, 302, 303, 307, 308} or body:
            raise OverrideError("maintenance artifact download did not return one redirect")
        location = headers.get("location")
        if location is None:
            raise OverrideError("maintenance artifact redirect omitted its location")
        try:
            parsed = urllib.parse.urlsplit(location)
            port = parsed.port
        except ValueError as error:
            raise OverrideError(f"maintenance artifact redirect is invalid: {error}") from error
        host = (parsed.hostname or "").lower()
        allowed_host = (
            host == "objects.githubusercontent.com"
            or host == "pipelines.actions.githubusercontent.com"
            or host.endswith(".actions.githubusercontent.com")
            or host.endswith(".blob.core.windows.net")
        )
        if (
            parsed.scheme != "https"
            or not allowed_host
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.fragment
        ):
            raise OverrideError("maintenance artifact redirect escaped its host boundary")
        status, _, archive = self._open(
            location,
            authenticated=False,
            maximum=size,
            accept="application/octet-stream",
        )
        if status != 200 or len(archive) != size:
            raise OverrideError("maintenance artifact download is incomplete")
        if hashlib.sha256(archive).hexdigest() != expected:
            raise OverrideError("maintenance artifact bytes differ from GitHub digest")
        return archive


def build_manual_retry_oci_plan(
    intent: Mapping[str, Any], *, policy: TapStagingPolicyV1
) -> OciRecordPlanV1:
    formula = _mapping(intent.get("formula"), "manual retry Formula")
    name = _stable_id(formula.get("formula"), "manual retry Formula name")
    target_abi = _positive(formula.get("target_abi"), "manual retry target ABI")
    base = candidate_repository(policy, target_abi, formula=name)
    return _record_plan(
        intent,
        repository=f"{base}/maintenance/retries",
        media_type=MANUAL_RETRY_MEDIA_TYPE,
        role="manual-retry",
        title="manual-retry.json",
        annotations={
            "dev.kandelo.abi-staging.classification": "maintainer-retry-after-exhaustion",
            "dev.kandelo.abi-staging.kind": "manual-retry",
            "dev.kandelo.abi-staging.request-sha256": str(intent.get("request_sha256")),
            "org.opencontainers.image.source": "https://github.com/" + policy.tap_repository,
        },
    )


def _attempt_facts(value: Any) -> tuple[AttemptFactV1, ...]:
    result = []
    fields = frozenset(
        {
            "request_sha256",
            "subject",
            "contract_sha256",
            "retry_ordinal",
            "outcome",
            "guard_code",
            "completed_at",
            "record_sha256",
        }
    )
    for index, candidate in enumerate(_sequence(value, "manual retry attempts")):
        item = _exact(candidate, fields, f"manual retry attempt {index}")
        try:
            result.append(AttemptFactV1(**item))
        except (TypeError, ValueError) as error:
            raise OverrideError(f"manual retry attempt {index} is invalid: {error}") from error
    return tuple(result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.abi_staging.override")
    subcommands = parser.add_subparsers(dest="operation", required=True)
    for name in (
        "authorize-capture",
        "accept-artifact-risk",
        "retry-exhausted",
    ):
        command = subcommands.add_parser(name)
        command.add_argument("--evidence-artifact-id", required=True, type=int)
        command.add_argument("--evidence-sha256", required=True)
        command.add_argument("--justification", required=True)
        command.add_argument("--actor", required=True)
        command.add_argument("--authorization-reference", required=True)
        command.add_argument("--verify-actor-permission", action="store_true")
        command.add_argument("--immutable", action="store_true")
    return parser


def _run_cli(args: argparse.Namespace) -> dict[str, Any]:
    if not args.verify_actor_permission or not args.immutable:
        raise OverrideError("maintenance requires verified permission and immutable publication")
    tap_root = Path(__file__).resolve().parents[2]
    policy = load_tap_staging_policy(tap_root / "Kandelo/staging/tap-policy.toml")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if repository != policy.tap_repository:
        raise OverrideError("maintenance workflow repository differs from tap policy")
    token = os.environ.get("GITHUB_TOKEN", "")
    client = GitHubMaintenanceClientV1(repository, token)
    archive = client.evidence_artifact(
        args.evidence_artifact_id, expected_sha256=args.evidence_sha256
    )
    evidence = load_maintenance_evidence_archive(
        archive, expected_sha256=args.evidence_sha256, operation=args.operation
    )
    run_id = _positive(int(os.environ.get("GITHUB_RUN_ID", "0")), "maintenance run ID")
    run_attempt = _positive(
        int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")), "maintenance run attempt"
    )
    expected_reference = (
        f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{run_attempt}"
    )
    if args.authorization_reference != expected_reference:
        raise OverrideError("maintenance authorization reference differs from current run")
    maintainer = client.maintainer(args.actor, args.authorization_reference)
    run = {
        "repository": repository,
        "workflow_ref": (
            ".github/workflows/abi-staging-maintenance.yml@refs/heads/main"
        ),
        "run_id": run_id,
        "run_attempt": run_attempt,
        "job": "maintain",
    }
    payload = evidence.payload
    if args.operation == "authorize-capture":
        record = authorize_capture(
            request=evidence.request,
            request_sha256=evidence.request_sha256,
            formula=payload["formula"],
            expected_formula=payload["formula"],
            guard_code=payload["guard_code"],
            guard_registry=evidence.guard_registry,
            maintainer=maintainer,
            justification=args.justification,
            run=run,
            tap_repository=policy.tap_repository,
        )
        plan = build_capture_authorization_oci_plan(record, policy=policy)
    elif args.operation == "accept-artifact-risk":
        record = accept_artifact_risk(
            request=evidence.request,
            request_sha256=evidence.request_sha256,
            candidate=payload["candidate"],
            candidate_record_sha256=payload["candidate_record_sha256"],
            accepted_guard_codes=tuple(payload["accepted_guard_codes"]),
            guard_registry=evidence.guard_registry,
            maintainer=maintainer,
            justification=args.justification,
            run=run,
            tap_repository=policy.tap_repository,
            capture_authorization=payload["capture_authorization"],
            capture_authorization_sha256=payload["capture_authorization_sha256"],
        )
        plan = build_override_receipt_oci_plan(
            record, candidate=payload["candidate"], policy=policy
        )
    else:
        record = retry_exhausted(
            request=evidence.request,
            request_sha256=evidence.request_sha256,
            attempts=_attempt_facts(payload["attempts"]),
            guard_registry=evidence.guard_registry,
            maintainer=maintainer,
            justification=args.justification,
            tap_repository=policy.tap_repository,
        )
        plan = build_manual_retry_oci_plan(record, policy=policy)
    username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
    registry_token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
    with isolated_oras_transport(username=username, token=registry_token) as transport:
        locator = publish_record(
            plan,
            transport=transport,
            expected_source_repository=policy.tap_repository,
        )
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-maintenance-result",
        "operation": args.operation,
        "record_sha256": canonical_sha256(record),
        "published": {
            "repository": locator.repository,
            "digest": locator.digest,
            "immutable_reference": locator.immutable_reference,
            "anonymous_readback_sha256": locator.anonymous_readback_sha256,
        },
    }


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        result = _run_cli(_parser().parse_args(arguments))
        sys.stdout.buffer.write(canonical_bytes(result))
        return 0
    except (OverrideError, ValueError, OSError) as error:
        print(f"abi-staging maintenance: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
