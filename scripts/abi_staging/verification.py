"""Validate inert verifier handoffs and publish factual immutable receipts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any

from .canonical import CanonicalJsonError, canonical_bytes, canonical_sha256, parse_canonical_bytes
from .contract import load_canonical_mapping
from .oci import (
    OciPublicationError,
    OciTransportV1,
    PublishedRecordLocatorV1,
    fetch_public_record,
    parse_public_record_locator,
    publish_record,
)
from .policy import (
    TapStagingPolicyV1,
    VerificationTestDefinitionV1,
    candidate_repository,
)
from .records import (
    CANDIDATE_RECORD_MEDIA_TYPE,
    OciBlobV1,
    OciRecordPlanV1,
    validate_candidate_record,
)


MAX_RESULT_FILES = 64
MAX_RESULT_BYTES = 64 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 16 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9@+._-]{0,255}$")
VERIFICATION_RECEIPT_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.verification.receipt.v1+json"
)
VERIFICATION_RESULT_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.verification.result.v1+json"
)
PROTECTED_VERIFICATION_OUTCOME_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.verification.protected-outcome.v1+json"
)
SECRET_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"AKIA[A-Z0-9]{16}"),
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
)


class VerificationError(ValueError):
    """Raised when verification facts are incomplete or contradictory."""


def _exact(value: Any, expected: frozenset[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != expected:
        raise VerificationError(f"{field} fields changed")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise VerificationError(f"{field} must be an array")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise VerificationError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise VerificationError(f"{field} is not UTF-8") from error
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise VerificationError(f"{field} is outside its string bound")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise VerificationError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 2**64 - 1
    ):
        raise VerificationError(f"{field} is not a bounded integer")
    return value


def _timestamp(value: Any, field: str) -> str:
    result = _text(value, field, 64)
    if re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z",
        result,
    ) is None:
        raise VerificationError(f"{field} is not millisecond UTC RFC 3339")
    try:
        datetime.strptime(result, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise VerificationError(f"{field} is not a real UTC timestamp") from error
    return result


def _relative_path(value: Any, field: str) -> str:
    path = _text(value, field)
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise VerificationError(f"{field} is not a normalized relative path")
    return path


def _artifact(value: Any, field: str) -> dict[str, Any]:
    artifact = _exact(
        value, frozenset({"sha256", "bytes", "immutable_reference"}), field
    )
    digest = _digest(artifact["sha256"], f"{field} digest")
    size = _integer(artifact["bytes"], f"{field} bytes", positive=True)
    reference = _text(artifact["immutable_reference"], f"{field} reference")
    if any(character.isspace() for character in reference) or f"sha256:{digest}" not in reference:
        raise VerificationError(f"{field} reference does not bind its digest")
    return {"sha256": digest, "bytes": size, "immutable_reference": reference}


def _source(value: Any, field: str) -> dict[str, str]:
    source = _exact(value, frozenset({"repository", "commit", "tree"}), field)
    repository = _text(source["repository"], f"{field} repository", 255)
    commit = _text(source["commit"], f"{field} commit", 40)
    tree = _text(source["tree"], f"{field} tree", 40)
    if (
        re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository) is None
        or GIT_SHA.fullmatch(commit) is None
        or GIT_SHA.fullmatch(tree) is None
    ):
        raise VerificationError(f"{field} identity is invalid")
    return {"repository": repository, "commit": commit, "tree": tree}


def _run(value: Any, field: str) -> dict[str, Any]:
    run = _exact(
        value,
        frozenset({"repository", "workflow_ref", "run_id", "run_attempt", "job"}),
        field,
    )
    repository = _text(run["repository"], f"{field} repository", 255)
    job = _text(run["job"], f"{field} job", 256)
    if (
        re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository) is None
        or STABLE_ID.fullmatch(job) is None
    ):
        raise VerificationError(f"{field} identity is invalid")
    return {
        "repository": repository,
        "workflow_ref": _text(run["workflow_ref"], f"{field} workflow ref", 2048),
        "run_id": _integer(run["run_id"], f"{field} run ID", positive=True),
        "run_attempt": _integer(
            run["run_attempt"], f"{field} run attempt", positive=True
        ),
        "job": job,
    }


def _read_regular(path: Path, field: str, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise VerificationError(f"cannot inspect {field}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise VerificationError(f"{field} must be a regular non-symlink file")
    if not 1 <= metadata.st_size <= maximum:
        raise VerificationError(f"{field} is outside its byte bound")
    try:
        body = path.read_bytes()
    except OSError as error:
        raise VerificationError(f"cannot read {field}: {error}") from error
    if len(body) != metadata.st_size:
        raise VerificationError(f"{field} changed while reading")
    return body


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def _load_canonical(body: bytes, field: str) -> dict[str, Any]:
    try:
        value = _plain(parse_canonical_bytes(body, maximum_bytes=MAX_RESULT_BYTES))
    except CanonicalJsonError as error:
        raise VerificationError(f"{field} is not canonical: {error}") from error
    if not isinstance(value, dict):
        raise VerificationError(f"{field} must be an object")
    return value


def _scan_result_files(root: Path) -> dict[str, Path]:
    try:
        metadata = root.lstat()
    except OSError as error:
        raise VerificationError(f"cannot inspect verification handoff: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise VerificationError("verification handoff must be a real directory")
    files: dict[str, Path] = {}
    total = 0
    for directory, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        relative_directory = current.relative_to(root).as_posix()
        for name in directories:
            candidate = current / name
            if candidate.is_symlink() or not candidate.is_dir():
                raise VerificationError("verification handoff contains an unsafe directory")
        for name in filenames:
            candidate = current / name
            relative = name if relative_directory == "." else f"{relative_directory}/{name}"
            relative = _relative_path(relative, "verification handoff path")
            member = candidate.lstat()
            if stat.S_ISLNK(member.st_mode) or not stat.S_ISREG(member.st_mode):
                raise VerificationError("verification handoff contains a non-regular file")
            total += member.st_size
            files[relative] = candidate
    if len(files) > MAX_RESULT_FILES or total > MAX_RESULT_BYTES:
        raise VerificationError("verification handoff exceeds its inventory bounds")
    return files


def load_verification_result(root: Path) -> dict[str, Any]:
    files = _scan_result_files(root)
    if "inventory.json" not in files or "result.json" not in files:
        raise VerificationError("verification handoff lacks inventory or result")
    inventory = _load_canonical(
        _read_regular(files["inventory.json"], "verification inventory", MAX_RESULT_BYTES),
        "verification inventory",
    )
    inventory = _exact(
        inventory,
        frozenset({"schema", "kind", "files"}),
        "verification inventory",
    )
    if (
        inventory["schema"] != 1
        or inventory["kind"] != "kandelo-abi-staging-verification-inventory"
    ):
        raise VerificationError("verification inventory protocol is unsupported")
    entries: dict[str, dict[str, Any]] = {}
    previous = ""
    for index, candidate in enumerate(_sequence(inventory["files"], "inventory files")):
        entry = _exact(
            candidate,
            frozenset({"path", "role", "sha256", "bytes"}),
            f"inventory file {index}",
        )
        path = _relative_path(entry["path"], f"inventory file {index} path")
        role = _text(entry["role"], f"inventory file {index} role", 64)
        if path <= previous or role not in {"diagnostic", "result"}:
            raise VerificationError("verification inventory must be sorted and typed")
        previous = path
        entries[path] = {
            "sha256": _digest(entry["sha256"], f"inventory file {path} digest"),
            "bytes": _integer(entry["bytes"], f"inventory file {path} bytes", positive=True),
            "role": role,
        }
    if set(entries) != set(files) - {"inventory.json"}:
        raise VerificationError("verification inventory does not list exactly every payload file")
    bodies: dict[str, bytes] = {}
    for path, entry in entries.items():
        maximum = MAX_DIAGNOSTIC_BYTES if entry["role"] == "diagnostic" else MAX_RESULT_BYTES
        body = _read_regular(files[path], f"verification file {path}", maximum)
        if len(body) != entry["bytes"] or hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise VerificationError(f"verification file {path} differs from its inventory")
        if entry["role"] == "diagnostic" and any(
            pattern.search(body) is not None for pattern in SECRET_PATTERNS
        ):
            raise VerificationError(
                f"verification diagnostic contains a secret-shaped value: {path}"
            )
        bodies[path] = body
    if entries.get("result.json", {}).get("role") != "result" or sum(
        entry["role"] == "result" for entry in entries.values()
    ) != 1:
        raise VerificationError("verification inventory requires one exact result")
    result = _load_canonical(bodies["result.json"], "verification result")
    result = _exact(
        result,
        frozenset(
            {
                "schema",
                "kind",
                "request_sha256",
                "candidate_record",
                "candidate_layer",
                "test_definition",
                "source",
                "run",
                "attempt_ordinal",
                "outcome",
                "exit_code",
                "runtime_artifacts",
                "diagnostics",
            }
        ),
        "verification result",
    )
    if (
        result["schema"] != 1
        or result["kind"] != "kandelo-abi-staging-verification-result"
    ):
        raise VerificationError("verification result protocol is unsupported")
    _digest(result["request_sha256"], "verification request")
    try:
        result["candidate_record"] = parse_public_record_locator(
            result["candidate_record"]
        )
    except OciPublicationError as error:
        raise VerificationError(f"verification candidate locator is invalid: {error}") from error
    result["candidate_layer"] = _artifact(
        result["candidate_layer"], "verification candidate layer"
    )
    definition = _exact(
        result["test_definition"],
        frozenset({"id", "sha256", "host"}),
        "verification test definition",
    )
    test_id = _text(definition["id"], "verification test ID", 256)
    host = _text(definition["host"], "verification host", 32)
    if STABLE_ID.fullmatch(test_id) is None or host not in {"build", "node", "browser"}:
        raise VerificationError("verification test identity is unsupported")
    result["test_definition"] = {
        "id": test_id,
        "sha256": _digest(definition["sha256"], "verification test definition"),
        "host": host,
    }
    result["source"] = _source(result["source"], "verification source")
    result["run"] = _run(result["run"], "verification run")
    result["attempt_ordinal"] = _integer(
        result["attempt_ordinal"], "verification attempt ordinal"
    )
    outcome = result["outcome"]
    exit_code = result["exit_code"]
    if (
        outcome not in {"success", "failure", "timeout"}
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or not 0 <= exit_code <= 255
        or (outcome == "success") != (exit_code == 0)
        or (outcome == "timeout") != (exit_code == 124)
    ):
        raise VerificationError("verification outcome and exit code are contradictory")
    runtime = _exact(
        result["runtime_artifacts"],
        frozenset({"kernel", "host_runtime", "vfs"}),
        "verification runtime artifacts",
    )
    result["runtime_artifacts"] = {
        key: None if runtime[key] is None else _artifact(runtime[key], f"verification {key}")
        for key in ("kernel", "host_runtime", "vfs")
    }
    diagnostics = []
    previous_path = ""
    for index, candidate in enumerate(
        _sequence(result["diagnostics"], "verification diagnostics")
    ):
        diagnostic = _exact(
            candidate,
            frozenset({"path", "sha256", "bytes"}),
            f"verification diagnostic {index}",
        )
        path = _relative_path(diagnostic["path"], f"verification diagnostic {index} path")
        if path <= previous_path or entries.get(path, {}).get("role") != "diagnostic":
            raise VerificationError("verification diagnostics must exactly name diagnostic files")
        previous_path = path
        identity = {
            "path": path,
            "sha256": _digest(diagnostic["sha256"], f"diagnostic {path} digest"),
            "bytes": _integer(diagnostic["bytes"], f"diagnostic {path} bytes", positive=True),
        }
        if (
            identity["sha256"] != entries[path]["sha256"]
            or identity["bytes"] != entries[path]["bytes"]
        ):
            raise VerificationError(f"diagnostic {path} differs from its inventory")
        diagnostics.append(identity)
    if {item["path"] for item in diagnostics} != {
        path for path, entry in entries.items() if entry["role"] == "diagnostic"
    }:
        raise VerificationError("verification result omits an inventoried diagnostic")
    result["diagnostics"] = diagnostics
    result["_bodies"] = bodies
    return dict(result)


def _definition_identity(definition: VerificationTestDefinitionV1) -> dict[str, Any]:
    identity = {
        "hosts": list(definition.hosts),
        "id": definition.id,
        "kandelo_paths": list(definition.kandelo_paths),
        "policy": definition.policy,
    }
    if canonical_sha256(identity) != definition.sha256:
        raise VerificationError("protected verification test definition digest drifted")
    return identity


def receipt_repository(candidate_repository: str, test_id: str, host: str) -> str:
    if (
        re.fullmatch(
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+",
            candidate_repository,
        )
        is None
        or STABLE_ID.fullmatch(test_id) is None
        or host not in {"build", "node", "browser"}
    ):
        raise VerificationError("verification receipt repository inputs are invalid")
    return f"{candidate_repository}/receipts/{test_id}/{host}"


def validate_verification_receipt_record(value: Mapping[str, Any]) -> None:
    """Validate the durable facts required to reconstruct scheduler state."""

    receipt = _exact(
        value,
        frozenset({"schema", "kind", "common", "verification"}),
        "verification receipt",
    )
    if receipt["schema"] != 1 or receipt["kind"] != "kandelo-abi-staging-verification":
        raise VerificationError("verification receipt protocol is unsupported")
    common = _exact(
        receipt["common"],
        frozenset(
            {
                "request_sha256",
                "subject",
                "source",
                "run",
                "guard_codes",
                "work_state",
                "outcome",
                "artifact_class",
                "promotion_state",
                "retry_state",
                "blockers",
            }
        ),
        "verification receipt common",
    )
    _digest(common["request_sha256"], "verification receipt request")
    subject = _exact(
        common["subject"],
        frozenset({"kind", "identity"}),
        "verification receipt subject",
    )
    if subject["kind"] != "candidate":
        raise VerificationError("verification receipt subject is not a candidate")
    subject_identity = _digest(
        subject["identity"], "verification receipt candidate subject"
    )
    _source(common["source"], "verification receipt source")
    _run(common["run"], "verification receipt run")

    verification = _exact(
        receipt["verification"],
        frozenset(
            {
                "candidate_record_sha256",
                "candidate_layer",
                "test_definition_sha256",
                "host",
                "attempt_ordinal",
                "diagnostics",
            }
        )
        | frozenset(
            key
            for key in ("kernel", "host_runtime", "vfs")
            if key in _exact_or_mapping(receipt["verification"], "verification receipt payload")
        ),
        "verification receipt payload",
    )
    candidate_record = _digest(
        verification["candidate_record_sha256"],
        "verification receipt candidate record",
    )
    if subject_identity != candidate_record:
        raise VerificationError("verification receipt subject differs from its candidate")
    _artifact(verification["candidate_layer"], "verification receipt candidate layer")
    _digest(
        verification["test_definition_sha256"],
        "verification receipt test definition",
    )
    if verification["host"] not in {"build", "node", "browser"}:
        raise VerificationError("verification receipt host is unsupported")
    attempt_ordinal = _integer(
        verification["attempt_ordinal"], "verification receipt attempt ordinal"
    )
    if attempt_ordinal > 3:
        raise VerificationError("verification receipt attempt ordinal exceeds retry policy")

    diagnostics = _sequence(
        verification["diagnostics"], "verification receipt diagnostics"
    )
    previous = ("", "")
    for index, candidate in enumerate(diagnostics):
        diagnostic = _exact(
            candidate,
            frozenset({"record_sha256", "immutable_reference"}),
            f"verification receipt diagnostic {index}",
        )
        digest = _digest(
            diagnostic["record_sha256"],
            f"verification receipt diagnostic {index} digest",
        )
        reference = _text(
            diagnostic["immutable_reference"],
            f"verification receipt diagnostic {index} reference",
        )
        if any(character.isspace() for character in reference) or not reference.endswith(
            "@sha256:" + digest
        ):
            raise VerificationError("verification receipt diagnostic reference is not exact")
        identity = (digest, reference)
        if identity <= previous:
            raise VerificationError(
                "verification receipt diagnostics must be sorted and duplicate-free"
            )
        previous = identity
    for key in ("kernel", "host_runtime", "vfs"):
        if key in verification:
            _artifact(verification[key], f"verification receipt {key}")

    outcome = common["outcome"]
    allowed_guards = {
        "success": {None},
        "failure": {
            "verification_failed",
            "transient_infrastructure_failure",
            "candidate_integrity_mismatch",
        },
        "timeout": {
            "verification_timeout",
            "transient_infrastructure_failure",
        },
        "canceled": {"transient_infrastructure_failure"},
    }
    if outcome not in allowed_guards:
        raise VerificationError("verification receipt outcome is unsupported")
    guards = list(
        _sequence(common["guard_codes"], "verification receipt guards")
    )
    if len(guards) > 1 or any(
        not isinstance(guard, str) for guard in guards
    ):
        raise VerificationError("verification receipt guards are invalid")
    guard = None if not guards else guards[0]
    if guard not in allowed_guards[outcome]:
        raise VerificationError("verification receipt outcome and guard contradict")
    blockers = _sequence(common["blockers"], "verification receipt blockers")
    expected_blockers = (
        []
        if guard is None
        else [
            {
                "guard_code": guard,
                "subject_kind": "candidate",
                "subject": candidate_record,
            }
        ]
    )
    retry = _exact(
        common["retry_state"],
        frozenset({"attempts", "eligible", "exhausted", "next_action"}),
        "verification receipt retry state",
    )
    if (
        list(blockers) != expected_blockers
        or common["work_state"] != "complete"
        or common["artifact_class"] != "none"
        or common["promotion_state"]
        != ("eligible" if guard is None else "ineligible")
        or retry
        != {
            "attempts": attempt_ordinal + 1,
            "eligible": False,
            "exhausted": False,
            "next_action": "none",
        }
    ):
        raise VerificationError("verification receipt state is contradictory")
    if len(canonical_bytes(receipt)) > MAX_RESULT_BYTES:
        raise VerificationError("verification receipt exceeds its byte bound")


def _exact_or_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VerificationError(f"{field} must be an object")
    return value


def _receipt_record(
    *,
    candidate: Mapping[str, Any],
    candidate_record_sha256: str,
    result: Mapping[str, Any],
    repository: str,
    guard_code: str | None,
    include_result: bool,
) -> tuple[dict[str, Any], tuple[OciBlobV1, ...]]:
    outcome = result["outcome"]
    guard = guard_code
    diagnostics = []
    layers = []
    if include_result:
        layers.append(
            OciBlobV1(
                role="verification-result",
                media_type=VERIFICATION_RESULT_MEDIA_TYPE,
                body=result["_bodies"]["result.json"],
                title="verification-result.json",
            )
        )
    else:
        layers.append(
            OciBlobV1(
                role="protected-verification-outcome",
                media_type=PROTECTED_VERIFICATION_OUTCOME_MEDIA_TYPE,
                body=canonical_bytes(
                    {
                        "schema": 1,
                        "kind": "kandelo-abi-staging-protected-verification-outcome",
                        "request_sha256": result["request_sha256"],
                        "candidate_record_sha256": candidate_record_sha256,
                        "test_definition": result["test_definition"],
                        "source": result["source"],
                        "run": result["run"],
                        "attempt_ordinal": result["attempt_ordinal"],
                        "outcome": outcome,
                        "guard_code": guard,
                        "runtime_artifacts": result["runtime_artifacts"],
                    }
                ),
                title="protected-verification-outcome.json",
            )
        )
    for index, diagnostic in enumerate(result["diagnostics"]):
        body = result["_bodies"][diagnostic["path"]]
        reference = f"ghcr.io/{repository}@sha256:{diagnostic['sha256']}"
        diagnostics.append(
            {
                "record_sha256": diagnostic["sha256"],
                "immutable_reference": reference,
            }
        )
        layers.append(
            OciBlobV1(
                role=f"diagnostic-{index:04d}",
                media_type="text/plain",
                body=body,
                title=diagnostic["path"],
            )
        )
    diagnostics.sort(key=lambda item: (item["record_sha256"], item["immutable_reference"]))
    candidate_layer = candidate["candidate"]["bottle_layer"]
    blockers = []
    if guard is not None:
        blockers.append(
            {
                "guard_code": guard,
                "subject_kind": "candidate",
                "subject": candidate_record_sha256,
            }
        )
    receipt = {
        "schema": 1,
        "kind": "kandelo-abi-staging-verification",
        "common": {
            "request_sha256": result["request_sha256"],
            "subject": {"kind": "candidate", "identity": candidate_record_sha256},
            "source": result["source"],
            "run": result["run"],
            "guard_codes": [] if guard is None else [guard],
            "work_state": "complete",
            "outcome": outcome,
            "artifact_class": "none",
            "promotion_state": "eligible" if guard is None else "ineligible",
            "retry_state": {
                "attempts": result["attempt_ordinal"] + 1,
                "eligible": False,
                "exhausted": False,
                "next_action": "none",
            },
            "blockers": blockers,
        },
        "verification": {
            "candidate_record_sha256": candidate_record_sha256,
            "candidate_layer": candidate_layer,
            "test_definition_sha256": result["test_definition"]["sha256"],
            "host": result["test_definition"]["host"],
            "attempt_ordinal": result["attempt_ordinal"],
            "diagnostics": diagnostics,
        },
    }
    for key in ("kernel", "host_runtime", "vfs"):
        if result["runtime_artifacts"][key] is not None:
            receipt["verification"][key] = result["runtime_artifacts"][key]
    return receipt, tuple(layers)


def _checked_runtime_artifacts(
    value: Mapping[str, object | None],
) -> dict[str, object | None]:
    protected_runtime = _exact(
        value,
        frozenset({"kernel", "host_runtime", "vfs"}),
        "protected verification runtime artifacts",
    )
    return {
        key: None
        if protected_runtime[key] is None
        else _artifact(
            protected_runtime[key], f"protected verification {key}"
        )
        for key in ("kernel", "host_runtime", "vfs")
    }


def _public_candidate(
    candidate_locator: Mapping[str, object],
    *,
    tap_policy: TapStagingPolicyV1,
    transport: OciTransportV1,
    expected_source_repository: str,
) -> tuple[
    dict[str, str],
    Mapping[str, Any],
    str,
    str,
    Mapping[str, Any],
]:
    try:
        checked_locator = parse_public_record_locator(candidate_locator)
        fetched = fetch_public_record(
            checked_locator,
            transport=transport,
            expected_artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
            required_layer_roles=("bottle-layer",),
        )
    except OciPublicationError as error:
        raise VerificationError(
            f"cannot re-fetch exact public candidate: {error}"
        ) from error
    try:
        candidate = load_canonical_mapping(fetched.config.body, "candidate record")
        validate_candidate_record(candidate)
    except ValueError as error:
        raise VerificationError(f"public candidate record is invalid: {error}") from error
    formula = candidate["candidate"]["formula"]
    expected_candidate_repository = candidate_repository(
        tap_policy,
        formula["target_abi"],
        formula=formula["formula"],
    )
    actual_candidate_repository = checked_locator["repository"].removeprefix(
        "ghcr.io/"
    )
    if (
        tap_policy.tap_repository != expected_source_repository
        or formula["tap"] != tap_policy.tap_repository
        or actual_candidate_repository != expected_candidate_repository
    ):
        raise VerificationError(
            "public candidate namespace differs from protected tap policy"
        )
    record_sha256 = checked_locator["digest"].removeprefix("sha256:")
    candidate_layer = candidate["candidate"]["bottle_layer"]
    if len(fetched.layers) != 1:
        raise VerificationError("public candidate lacks one exact bottle layer")
    fetched_layer = fetched.layers[0]
    if (
        fetched_layer.role != "bottle-layer"
        or fetched_layer.digest != "sha256:" + candidate_layer["sha256"]
        or fetched_layer.size != candidate_layer["bytes"]
    ):
        raise VerificationError("public candidate bottle layer differs from its record")
    return (
        checked_locator,
        candidate,
        actual_candidate_repository,
        record_sha256,
        candidate_layer,
    )


def _publish_receipt_plan(
    *,
    repository: str,
    receipt: Mapping[str, Any],
    layers: tuple[OciBlobV1, ...],
    completed_at: str,
    host: str,
    outcome: str,
    candidate_record_sha256: str,
    test_definition_sha256: str,
    transport: OciTransportV1,
    expected_source_repository: str,
) -> PublishedRecordLocatorV1:
    plan = OciRecordPlanV1(
        repository=repository,
        artifact_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
        config=OciBlobV1(
            role="verification-receipt",
            media_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
            body=canonical_bytes(receipt),
            title="verification-receipt.json",
        ),
        layers=layers,
        annotations={
            "dev.kandelo.abi-staging.candidate-record-sha256": candidate_record_sha256,
            "dev.kandelo.abi-staging.classification": "factual-verification-receipt",
            "dev.kandelo.abi-staging.completed-at": completed_at,
            "dev.kandelo.abi-staging.host": host,
            "dev.kandelo.abi-staging.kind": "verification-receipt",
            "dev.kandelo.abi-staging.outcome": outcome,
            "dev.kandelo.abi-staging.test-definition-sha256": test_definition_sha256,
            "org.opencontainers.image.source": "https://github.com/"
            + expected_source_repository,
        },
    )
    try:
        return publish_record(
            plan,
            transport=transport,
            expected_source_repository=expected_source_repository,
        )
    except OciPublicationError as error:
        raise VerificationError(f"cannot publish verification receipt: {error}") from error


def publish_verification_receipt(
    result_root: Path,
    *,
    candidate_locator: Mapping[str, object],
    test_definition: VerificationTestDefinitionV1,
    tap_policy: TapStagingPolicyV1,
    expected_run: Mapping[str, Any],
    expected_runtime_artifacts: Mapping[str, object | None],
    expected_request_sha256: str,
    expected_source: Mapping[str, Any],
    completed_at: str,
    transport: OciTransportV1,
    expected_source_repository: str,
) -> PublishedRecordLocatorV1:
    _definition_identity(test_definition)
    completed = _timestamp(completed_at, "verification completion")
    result = load_verification_result(result_root)
    expected_run_value = _run(expected_run, "expected verification run")
    if result["run"] != expected_run_value:
        raise VerificationError("verification result run differs from protected job facts")
    checked_runtime = _checked_runtime_artifacts(expected_runtime_artifacts)
    bound_request = _digest(
        expected_request_sha256, "protected verification request"
    )
    bound_source = _source(expected_source, "protected verification source")
    if result["runtime_artifacts"] != checked_runtime:
        raise VerificationError(
            "verification runtime artifacts differ from protected job inputs"
        )
    (
        checked_locator,
        candidate,
        actual_candidate_repository,
        record_sha256,
        candidate_layer,
    ) = _public_candidate(
        candidate_locator,
        tap_policy=tap_policy,
        transport=transport,
        expected_source_repository=expected_source_repository,
    )
    if (
        result["candidate_record"] != checked_locator
        or result["candidate_layer"] != candidate_layer
        or result["request_sha256"] != bound_request
        or result["source"] != bound_source
    ):
        raise VerificationError("verification result differs from the exact public candidate")
    selected = result["test_definition"]
    if (
        selected["id"] != test_definition.id
        or selected["sha256"] != test_definition.sha256
        or selected["host"] not in test_definition.hosts
    ):
        raise VerificationError("verification result differs from its protected test definition")
    repository = receipt_repository(
        actual_candidate_repository, test_definition.id, selected["host"]
    )
    receipt, layers = _receipt_record(
        candidate=candidate,
        candidate_record_sha256=record_sha256,
        result=result,
        repository=repository,
        guard_code={
            "success": None,
            "failure": "verification_failed",
            "timeout": "verification_timeout",
        }[result["outcome"]],
        include_result=True,
    )
    validate_verification_receipt_record(receipt)
    return _publish_receipt_plan(
        repository=repository,
        receipt=receipt,
        layers=layers,
        completed_at=completed,
        host=selected["host"],
        outcome=result["outcome"],
        candidate_record_sha256=record_sha256,
        test_definition_sha256=test_definition.sha256,
        transport=transport,
        expected_source_repository=expected_source_repository,
    )


def publish_protected_verification_outcome(
    *,
    candidate_locator: Mapping[str, object],
    test_definition: VerificationTestDefinitionV1,
    host: str,
    tap_policy: TapStagingPolicyV1,
    expected_run: Mapping[str, Any],
    expected_runtime_artifacts: Mapping[str, object | None],
    expected_request_sha256: str,
    expected_source: Mapping[str, Any],
    completed_at: str,
    attempt_ordinal: int,
    outcome: str,
    guard_code: str,
    transport: OciTransportV1,
    expected_source_repository: str,
) -> PublishedRecordLocatorV1:
    """Publish an exact protected job outcome when no verifier output exists."""

    _definition_identity(test_definition)
    if host not in test_definition.hosts:
        raise VerificationError("protected verification host is not in its definition")
    completed = _timestamp(completed_at, "verification completion")
    run = _run(expected_run, "expected verification run")
    runtime = _checked_runtime_artifacts(expected_runtime_artifacts)
    bound_request = _digest(
        expected_request_sha256, "protected verification request"
    )
    bound_source = _source(expected_source, "protected verification source")
    ordinal = _integer(attempt_ordinal, "verification attempt ordinal")
    if ordinal > 3:
        raise VerificationError("verification attempt ordinal exceeds retry policy")
    allowed = {
        "failure": {
            "transient_infrastructure_failure",
            "candidate_integrity_mismatch",
        },
        "timeout": {
            "verification_timeout",
            "transient_infrastructure_failure",
        },
        "canceled": {"transient_infrastructure_failure"},
    }
    if outcome not in allowed or guard_code not in allowed[outcome]:
        raise VerificationError("protected verification outcome and guard contradict")
    (
        _checked_locator,
        candidate,
        actual_candidate_repository,
        record_sha256,
        _candidate_layer,
    ) = _public_candidate(
        candidate_locator,
        tap_policy=tap_policy,
        transport=transport,
        expected_source_repository=expected_source_repository,
    )
    repository = receipt_repository(
        actual_candidate_repository, test_definition.id, host
    )
    synthetic_result = {
        "request_sha256": bound_request,
        "source": bound_source,
        "run": run,
        "attempt_ordinal": ordinal,
        "outcome": outcome,
        "runtime_artifacts": runtime,
        "test_definition": {
            "id": test_definition.id,
            "sha256": test_definition.sha256,
            "host": host,
        },
        "diagnostics": [],
    }
    receipt, layers = _receipt_record(
        candidate=candidate,
        candidate_record_sha256=record_sha256,
        result=synthetic_result,
        repository=repository,
        guard_code=guard_code,
        include_result=False,
    )
    validate_verification_receipt_record(receipt)
    return _publish_receipt_plan(
        repository=repository,
        receipt=receipt,
        layers=layers,
        completed_at=completed,
        host=host,
        outcome=outcome,
        candidate_record_sha256=record_sha256,
        test_definition_sha256=test_definition.sha256,
        transport=transport,
        expected_source_repository=expected_source_repository,
    )
