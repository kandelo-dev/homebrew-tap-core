"""Canonical tap-side durable record readers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any

from .canonical import CanonicalJsonError, canonical_bytes, parse_canonical_bytes
from .contract import (
    load_bottle_contract,
    load_canonical_mapping,
    validate_candidate_reuse_record,
)
from .custody import load_source_custody_manifest
from .plan import PlanError, exact_formula_subject, validate_tap_plan


MAX_TAP_PLAN_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_BLOB_BYTES = 8 * 1024 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9@+._-]{0,255}$")
MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")

OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
CANDIDATE_RECORD_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.candidate.record.v1+json"
)
CANDIDATE_REUSE_RECORD_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.candidate-reuse.record.v1+json"
)
SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.source-custody.manifest.v1+json"
)
BOTTLE_LAYER_MEDIA_TYPE = "application/vnd.kandelo.homebrew.bottle.layer.v1+tar+gzip"
BOTTLE_METADATA_MEDIA_TYPE = (
    "application/vnd.kandelo.homebrew.bottle.metadata.v1+json"
)
VFS_COMPOSITION_DESCRIPTOR_MEDIA_TYPE = (
    "application/vnd.kandelo.homebrew.vfs-composition-descriptor.v2+json"
)
BOTTLE_CONTRACT_MEDIA_TYPE = (
    "application/vnd.kandelo.homebrew.bottle.contract.v1+json"
)
ATTEMPT_RECORD_MEDIA_TYPE = "application/vnd.kandelo.abi-staging.attempt.v1+json"
ATTEMPT_OUTCOME_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.attempt.outcome.v1+json"
)
GIT_BUNDLE_MEDIA_TYPE = "application/vnd.kandelo.git.bundle.v1"
GIT_TREE_MEDIA_TYPE = "application/vnd.kandelo.git.tree.v1+tar"


class TapRecordError(ValueError):
    """Raised when a tap-owned record is malformed or semantically invalid."""


@dataclass(frozen=True)
class OciBlobV1:
    """One exact local blob that will become an OCI descriptor."""

    role: str
    media_type: str
    body: bytes
    title: str
    mount_from: str | None = None

    def __post_init__(self) -> None:
        _stable_id(self.role, "OCI blob role")
        _media_type(self.media_type, "OCI blob media type")
        if not isinstance(self.body, bytes) or not 1 <= len(self.body) <= MAX_BLOB_BYTES:
            raise TapRecordError("OCI blob bytes are outside their bound")
        _title(self.title, "OCI blob title")
        if self.mount_from is not None:
            _repository(self.mount_from, "OCI blob mount source")

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.body).hexdigest()

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass(frozen=True)
class OciRecordPlanV1:
    """Local, deterministic inputs for one immutable OCI record."""

    repository: str
    artifact_type: str
    config: OciBlobV1
    layers: tuple[OciBlobV1, ...]
    annotations: Mapping[str, str]

    def __post_init__(self) -> None:
        _repository(self.repository, "OCI record repository")
        _media_type(self.artifact_type, "OCI artifact type")
        if self.config.media_type != self.artifact_type:
            raise TapRecordError("OCI record config media type must equal its artifact type")
        if not self.layers or len(self.layers) > 65_536:
            raise TapRecordError("OCI record requires bounded nonempty layers")
        roles = [layer.role for layer in self.layers]
        if self.config.role in roles or len(roles) != len(set(roles)):
            raise TapRecordError("OCI record layer roles must be unique")
        _annotations(self.annotations, "OCI record annotations")


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise TapRecordError(
            f"{field} fields changed: missing={sorted(expected - actual)!r} "
            f"extra={sorted(actual - expected)!r}"
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TapRecordError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TapRecordError(f"{field} must be an array")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise TapRecordError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise TapRecordError(f"{field} is not UTF-8") from error
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise TapRecordError(f"{field} is outside its string bound")
    return value


def _stable_id(value: Any, field: str) -> str:
    result = _text(value, field, 256)
    if STABLE_ID.fullmatch(result) is None:
        raise TapRecordError(f"{field} is not a stable identity")
    return result


def _title(value: Any, field: str) -> str:
    result = _text(value, field, 512)
    if result.startswith("/") or "\\" in result or any(
        part in {"", ".", ".."} for part in result.split("/")
    ):
        raise TapRecordError(f"{field} is not a normalized relative title")
    return result


def _repository(value: Any, field: str) -> str:
    result = _text(value, field, 512)
    if REPOSITORY.fullmatch(result) is None:
        raise TapRecordError(f"{field} is not a lowercase OCI repository")
    return result


def validate_formula_candidate_repository(repository: str) -> str:
    """Keep Formula records out of the reserved VFS product subtree."""

    checked = _repository(repository, "Formula candidate repository")
    if checked.endswith("/products") or "/products/" in checked:
        raise TapRecordError(
            "Formula candidate repository enters the reserved products subtree"
        )
    return checked


def _media_type(value: Any, field: str) -> str:
    result = _text(value, field, 256)
    if MEDIA_TYPE.fullmatch(result) is None:
        raise TapRecordError(f"{field} is not a media type")
    return result


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise TapRecordError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise TapRecordError(f"{field} is not a full lowercase Git SHA")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**64 - 1:
        raise TapRecordError(f"{field} is not a bounded positive integer")
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**64 - 1:
        raise TapRecordError(f"{field} is not a bounded nonnegative integer")
    return value


def _annotations(value: Mapping[str, str], field: str) -> dict[str, str]:
    annotations = _mapping(value, field)
    if not annotations or len(annotations) > 256:
        raise TapRecordError(f"{field} must be bounded and nonempty")
    checked: dict[str, str] = {}
    for key, candidate in annotations.items():
        checked[_text(key, f"{field} key", 256)] = _text(
            candidate, f"{field} value", 4096
        )
    return checked


def _read_regular(path: Path, field: str, maximum: int = MAX_BLOB_BYTES) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise TapRecordError(f"cannot inspect {field}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TapRecordError(f"{field} must be a regular non-symlink file")
    if not 1 <= metadata.st_size <= maximum:
        raise TapRecordError(f"{field} is outside its byte bound")
    try:
        body = path.read_bytes()
    except OSError as error:
        raise TapRecordError(f"cannot read {field}: {error}") from error
    if len(body) != metadata.st_size:
        raise TapRecordError(f"{field} changed while reading")
    return body


def _artifact(body: bytes, immutable_reference: str) -> dict[str, Any]:
    digest = hashlib.sha256(body).hexdigest()
    if f"sha256:{digest}" not in immutable_reference:
        raise TapRecordError("artifact immutable reference does not bind its bytes")
    return {
        "sha256": digest,
        "bytes": len(body),
        "immutable_reference": immutable_reference,
    }


def _artifact_from_identity(
    sha256: str, size: int, immutable_reference: str
) -> dict[str, Any]:
    digest = _digest(sha256, "artifact digest")
    count = _positive_integer(size, "artifact bytes")
    reference = _text(immutable_reference, "artifact immutable reference")
    if any(character.isspace() for character in reference) or f"sha256:{digest}" not in reference:
        raise TapRecordError("artifact immutable reference does not bind its digest")
    return {"sha256": digest, "bytes": count, "immutable_reference": reference}


def build_source_custody_oci_plan(
    custody_root: Path, *, repository: str
) -> OciRecordPlanV1:
    checked_repository = _repository(repository, "source custody repository")
    manifest_body = _read_regular(
        custody_root / "manifest.json", "source custody manifest", MAX_RECORD_BYTES
    )
    try:
        manifest = load_source_custody_manifest(manifest_body)
    except ValueError as error:
        raise TapRecordError(f"source custody manifest is invalid: {error}") from error
    layers: list[OciBlobV1] = []
    for source in manifest["sources"]:
        for suffix, key, media_type in (
            ("bundle", "bundle", GIT_BUNDLE_MEDIA_TYPE),
            ("tree", "tree_archive", GIT_TREE_MEDIA_TYPE),
        ):
            member = source[key]
            body = _read_regular(custody_root / member["path"], member["path"])
            if hashlib.sha256(body).hexdigest() != member["sha256"] or len(body) != member["bytes"]:
                raise TapRecordError(f"source custody member {member['path']!r} changed")
            layers.append(
                OciBlobV1(
                    role=f"{source['role']}-{suffix}",
                    media_type=media_type,
                    body=body,
                    title=member["path"],
                )
            )
    for submodule in manifest["submodules"]:
        for suffix, key, media_type in (
            ("bundle", "bundle", GIT_BUNDLE_MEDIA_TYPE),
            ("tree", "tree_archive", GIT_TREE_MEDIA_TYPE),
        ):
            member = submodule[key]
            body = _read_regular(custody_root / member["path"], member["path"])
            if hashlib.sha256(body).hexdigest() != member["sha256"] or len(body) != member["bytes"]:
                raise TapRecordError(f"source custody member {member['path']!r} changed")
            layers.append(
                OciBlobV1(
                    role=f"{submodule['id']}-{suffix}",
                    media_type=media_type,
                    body=body,
                    title=member["path"],
                )
            )
    return OciRecordPlanV1(
        repository=checked_repository,
        artifact_type=SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
        config=OciBlobV1(
            role="source-custody-manifest",
            media_type=SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
            body=manifest_body,
            title="source-custody-manifest.json",
        ),
        layers=tuple(layers),
        annotations={
            "dev.kandelo.abi-staging.capsule-sha256": manifest["capsule_sha256"],
            "dev.kandelo.abi-staging.classification": "factual-source-custody",
            "dev.kandelo.abi-staging.kind": "source-custody",
            "org.opencontainers.image.source": (
                "https://github.com/" + manifest["sources"][1]["repository"]
            ),
        },
    )


def _run(value: Any, field: str) -> dict[str, Any]:
    run = _mapping(value, field)
    _exact_keys(
        run,
        frozenset({"repository", "workflow_ref", "run_id", "run_attempt", "job"}),
        field,
    )
    repository = _text(run["repository"], f"{field} repository", 255)
    if not re.fullmatch(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", repository):
        raise TapRecordError(f"{field} repository is not owner/name")
    return {
        "repository": repository,
        "workflow_ref": _text(run["workflow_ref"], f"{field} workflow ref", 2048),
        "run_id": _positive_integer(run["run_id"], f"{field} run ID"),
        "run_attempt": _positive_integer(run["run_attempt"], f"{field} run attempt"),
        "job": _stable_id(run["job"], f"{field} job"),
    }


def _record_source(value: Any, field: str) -> dict[str, str]:
    source = _mapping(value, field)
    _exact_keys(source, frozenset({"repository", "commit", "tree"}), field)
    repository = _text(source["repository"], f"{field} repository", 255)
    if not re.fullmatch(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", repository):
        raise TapRecordError(f"{field} repository is not owner/name")
    return {
        "repository": repository,
        "commit": _git_sha(source["commit"], f"{field} commit"),
        "tree": _git_sha(source["tree"], f"{field} tree"),
    }


def _source_record(value: Any) -> dict[str, str]:
    locator = _mapping(value, "source custody record locator")
    _exact_keys(
        locator,
        frozenset({"repository", "digest", "immutable_reference"}),
        "source custody record locator",
    )
    repository = _text(locator["repository"], "source custody record repository", 512)
    if not repository.startswith("ghcr.io/"):
        raise TapRecordError("source custody record is not in GHCR")
    _repository(repository[len("ghcr.io/") :], "source custody record repository")
    digest = _text(locator["digest"], "source custody record digest", 71)
    if not digest.startswith("sha256:"):
        raise TapRecordError("source custody record digest is not SHA-256")
    digest_hex = _digest(digest[len("sha256:") :], "source custody record digest")
    reference = _text(
        locator["immutable_reference"], "source custody immutable reference", 4096
    )
    if reference != f"{repository}@sha256:{digest_hex}":
        raise TapRecordError("source custody immutable reference is not exact")
    return {
        "repository": repository,
        "digest": digest,
        "immutable_reference": reference,
    }


def build_candidate_oci_plan(
    handoff_root: Path,
    *,
    repository: str,
    source_record: Mapping[str, Any],
    source_manifest_bytes: bytes,
    publication_run: Mapping[str, Any],
) -> OciRecordPlanV1:
    checked_repository = validate_formula_candidate_repository(repository)
    source_locator = _source_record(source_record)
    source_digest = hashlib.sha256(source_manifest_bytes).hexdigest()
    if source_locator["digest"] != "sha256:" + source_digest:
        raise TapRecordError("source custody locator differs from exact manifest bytes")
    contract_body = _read_regular(
        handoff_root / "bottle-contract.json", "bottle contract", 16 * 1024 * 1024
    )
    try:
        contract = load_bottle_contract(contract_body)
        attempt = load_canonical_mapping(
            _read_regular(
                handoff_root / "attempt-record.json", "attempt record", MAX_RECORD_BYTES
            ),
            "attempt record",
        )
    except ValueError as error:
        raise TapRecordError(f"candidate input record is invalid: {error}") from error
    attempt_body = canonical_bytes(attempt)
    if attempt.get("schema") != 1 or attempt.get("kind") != "kandelo-abi-staging-attempt":
        raise TapRecordError("attempt record protocol is unsupported")
    common = _mapping(attempt.get("common"), "attempt common")
    attempt_payload = _mapping(attempt.get("attempt"), "attempt payload")
    formula_attempt = _mapping(attempt_payload.get("formula"), "attempt Formula")
    source = _record_source(common.get("source"), "attempt source")
    producer_run = _run(common.get("run"), "attempt run")
    formula = contract["formula"]
    target = contract["target"]
    contract_sha256 = hashlib.sha256(contract_body).hexdigest()
    if (
        formula_attempt.get("tap") != producer_run["repository"]
        or formula_attempt.get("formula") != formula["name"]
        or formula_attempt.get("architecture") != target["architecture"]
        or formula_attempt.get("target_abi") != target["abi"]
        or formula_attempt.get("bottle_contract_sha256") != contract_sha256
    ):
        raise TapRecordError("attempt Formula differs from the exact bottle contract")
    if common.get("outcome") != "success" or attempt_payload.get("candidate") is None:
        raise TapRecordError("only a successful exact build can produce a candidate")

    bottle_body = _read_regular(handoff_root / "bottle.tar.gz", "candidate bottle layer")
    metadata_body = _read_regular(
        handoff_root / "bottle-metadata.json", "candidate bottle metadata", MAX_RECORD_BYTES
    )
    composition_body = _read_regular(
        handoff_root / "vfs-composition-descriptor.json",
        "candidate VFS composition descriptor",
        MAX_RECORD_BYTES,
    )
    bottle_sha256 = hashlib.sha256(bottle_body).hexdigest()
    candidate_blob_reference = f"ghcr.io/{checked_repository}@sha256:{bottle_sha256}"
    bottle_artifact = _artifact(bottle_body, candidate_blob_reference)
    if attempt_payload["candidate"] != {
        **{key: bottle_artifact[key] for key in ("sha256", "bytes")},
        "immutable_reference": (
            "handoff:bottle.tar.gz@sha256:" + bottle_sha256
        ),
    }:
        raise TapRecordError("attempt candidate differs from the exact bottle bytes")

    tap = _text(formula_attempt["tap"], "candidate Formula tap", 255)
    candidate_base = checked_repository.rsplit("/", 1)[0]
    normalized_components = [
        {
            "id": "bottle-contract",
            "artifact": _artifact(
                contract_body,
                f"ghcr.io/{checked_repository}@sha256:{contract_sha256}",
            ),
        },
        {
            "id": "bottle-metadata",
            "artifact": _artifact(
                metadata_body,
                "ghcr.io/"
                + checked_repository
                + "@sha256:"
                + hashlib.sha256(metadata_body).hexdigest(),
            ),
        },
        {
            "id": "vfs-composition-descriptor",
            "artifact": _artifact(
                composition_body,
                "ghcr.io/"
                + checked_repository
                + "@sha256:"
                + hashlib.sha256(composition_body).hexdigest(),
            ),
        },
        {
            "id": "source-custody",
            "artifact": _artifact(
                source_manifest_bytes, source_locator["immutable_reference"]
            ),
        },
    ]
    normalized_components.sort(key=lambda candidate: candidate["id"])
    direct_dependencies = []
    for dependency in contract["direct_dependencies"]:
        dependency_digest = dependency["bottle_layer_sha256"]
        dependency_reference = (
            f"ghcr.io/{candidate_base}/{dependency['formula']}@sha256:{dependency_digest}"
        )
        direct_dependencies.append(
            {
                "id": f"{dependency['formula']}-{dependency['architecture']}",
                "artifact": _artifact_from_identity(
                    dependency_digest,
                    dependency["bottle_layer_bytes"],
                    dependency_reference,
                ),
            }
        )
    direct_dependencies.sort(key=lambda candidate: candidate["id"])
    publisher_run = _run(publication_run, "candidate publication run")
    record = {
        "schema": 1,
        "kind": "kandelo-abi-staging-candidate",
        "common": {
            "request_sha256": _digest(common.get("request_sha256"), "candidate request"),
            "subject": {
                "kind": "candidate",
                "identity": f"{tap}/{formula['name']}@sha256:{bottle_sha256}",
            },
            "source": source,
            "run": publisher_run,
            "guard_codes": [],
            "work_state": "complete",
            "outcome": "success",
            "artifact_class": "candidate",
            "artifact": bottle_artifact,
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
                "tap": tap,
                "formula": formula["name"],
                "version": formula["version"],
                "revision": formula["revision"],
                "bottle_rebuild": formula["rebuild"],
                "architecture": target["architecture"],
                "target_abi": target["abi"],
                "bottle_contract_sha256": contract_sha256,
            },
            "bottle_layer": bottle_artifact,
            "normalized_components": normalized_components,
            "direct_dependency_layers": direct_dependencies,
            "source_custody_sha256": source_digest,
            "producer": {
                "request_sha256": common["request_sha256"],
                "head": source["commit"],
                "run_id": producer_run["run_id"],
            },
            "nonendorsed": True,
        },
    }
    validate_candidate_record(record)
    candidate_body = canonical_bytes(record)
    source_record_blob = OciBlobV1(
        role="source-custody-record",
        media_type=OCI_MANIFEST_MEDIA_TYPE,
        body=source_manifest_bytes,
        title="source-custody-record.json",
        mount_from=source_locator["repository"][len("ghcr.io/") :],
    )
    return OciRecordPlanV1(
        repository=checked_repository,
        artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
        config=OciBlobV1(
            role="candidate-record",
            media_type=CANDIDATE_RECORD_MEDIA_TYPE,
            body=candidate_body,
            title="candidate-record.json",
        ),
        layers=(
            OciBlobV1(
                role="bottle-layer",
                media_type=BOTTLE_LAYER_MEDIA_TYPE,
                body=bottle_body,
                title="bottle.tar.gz",
            ),
            OciBlobV1(
                role="bottle-metadata",
                media_type=BOTTLE_METADATA_MEDIA_TYPE,
                body=metadata_body,
                title="bottle-metadata.json",
            ),
            OciBlobV1(
                role="vfs-composition-descriptor",
                media_type=VFS_COMPOSITION_DESCRIPTOR_MEDIA_TYPE,
                body=composition_body,
                title="vfs-composition-descriptor.json",
            ),
            OciBlobV1(
                role="bottle-contract",
                media_type=BOTTLE_CONTRACT_MEDIA_TYPE,
                body=contract_body,
                title="bottle-contract.json",
            ),
            OciBlobV1(
                role="attempt-record",
                media_type=ATTEMPT_RECORD_MEDIA_TYPE,
                body=attempt_body,
                title="attempt-record.json",
            ),
            source_record_blob,
        ),
        annotations={
            "dev.kandelo.abi-staging.architecture": target["architecture"],
            "dev.kandelo.abi-staging.bottle-contract-sha256": contract_sha256,
            "dev.kandelo.abi-staging.classification": "public-candidate-not-endorsed",
            "dev.kandelo.abi-staging.formula": formula["name"],
            "dev.kandelo.abi-staging.kind": "candidate",
            "dev.kandelo.abi-staging.nonendorsed": "true",
            "dev.kandelo.abi-staging.source-custody-record-sha256": source_digest,
            "dev.kandelo.abi-staging.target-abi": str(target["abi"]),
            "org.opencontainers.image.source": "https://github.com/" + tap,
        },
    )


def _validated_artifact(value: Any, field: str) -> dict[str, Any]:
    artifact = _mapping(value, field)
    _exact_keys(
        artifact, frozenset({"sha256", "bytes", "immutable_reference"}), field
    )
    digest = _digest(artifact["sha256"], f"{field} digest")
    size = _positive_integer(artifact["bytes"], f"{field} bytes")
    reference = _text(artifact["immutable_reference"], f"{field} reference")
    if any(character.isspace() for character in reference) or f"sha256:{digest}" not in reference:
        raise TapRecordError(f"{field} reference does not bind its digest")
    return {"sha256": digest, "bytes": size, "immutable_reference": reference}


def _validate_named_artifacts(value: Any, field: str) -> list[dict[str, Any]]:
    result = []
    previous = ""
    for index, candidate in enumerate(_sequence(value, field)):
        item = _mapping(candidate, f"{field} {index}")
        _exact_keys(item, frozenset({"id", "artifact"}), f"{field} {index}")
        identity = _stable_id(item["id"], f"{field} {index} id")
        if identity <= previous:
            raise TapRecordError(f"{field} must be sorted and duplicate-free")
        previous = identity
        result.append(
            {
                "id": identity,
                "artifact": _validated_artifact(
                    item["artifact"], f"{field} {identity} artifact"
                ),
            }
        )
    return result


def validate_candidate_record(record: Mapping[str, Any]) -> None:
    value = _mapping(record, "candidate record")
    _exact_keys(
        value,
        frozenset({"schema", "kind", "common", "candidate"}),
        "candidate record",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-candidate":
        raise TapRecordError("candidate record protocol is unsupported")
    common = _mapping(value["common"], "candidate common")
    _exact_keys(
        common,
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
                "artifact",
                "promotion_state",
                "retry_state",
                "blockers",
            }
        ),
        "candidate common",
    )
    request_sha256 = _digest(common["request_sha256"], "candidate request")
    subject = _mapping(common["subject"], "candidate subject")
    _exact_keys(subject, frozenset({"kind", "identity"}), "candidate subject")
    if subject["kind"] != "candidate":
        raise TapRecordError("candidate record subject kind changed")
    _text(subject["identity"], "candidate subject identity", 512)
    source = _record_source(common["source"], "candidate source")
    _run(common["run"], "candidate run")
    artifact = _validated_artifact(common["artifact"], "candidate common artifact")
    if (
        common["guard_codes"] != []
        or common["work_state"] != "complete"
        or common["outcome"] != "success"
        or common["artifact_class"] != "candidate"
        or common["promotion_state"] != "unknown"
        or common["blockers"] != []
        or common["retry_state"]
        != {
            "attempts": 1,
            "eligible": False,
            "exhausted": False,
            "next_action": "none",
        }
    ):
        raise TapRecordError("candidate common state is contradictory")
    payload = _mapping(value["candidate"], "candidate payload")
    _exact_keys(
        payload,
        frozenset(
            {
                "formula",
                "bottle_layer",
                "normalized_components",
                "direct_dependency_layers",
                "source_custody_sha256",
                "producer",
                "nonendorsed",
            }
        ),
        "candidate payload",
    )
    formula = _mapping(payload["formula"], "candidate Formula")
    _exact_keys(
        formula,
        frozenset(
            {
                "tap",
                "formula",
                "version",
                "revision",
                "bottle_rebuild",
                "architecture",
                "target_abi",
                "bottle_contract_sha256",
            }
        ),
        "candidate Formula",
    )
    tap = _text(formula["tap"], "candidate Formula tap", 255)
    if not re.fullmatch(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", tap):
        raise TapRecordError("candidate Formula tap is not owner/name")
    name = _stable_id(formula["formula"], "candidate Formula name")
    _text(formula["version"], "candidate Formula version", 256)
    _nonnegative_integer(formula["revision"], "candidate Formula revision")
    _nonnegative_integer(formula["bottle_rebuild"], "candidate Formula rebuild")
    if formula["architecture"] not in {"wasm32", "wasm64"}:
        raise TapRecordError("candidate Formula architecture is unsupported")
    _nonnegative_integer(formula["target_abi"], "candidate Formula target ABI")
    _digest(formula["bottle_contract_sha256"], "candidate bottle contract")
    bottle = _validated_artifact(payload["bottle_layer"], "candidate bottle layer")
    normalized = _validate_named_artifacts(
        payload["normalized_components"], "candidate normalized components"
    )
    _validate_named_artifacts(
        payload["direct_dependency_layers"], "candidate dependency layers"
    )
    custody_sha256 = _digest(
        payload["source_custody_sha256"], "candidate source custody"
    )
    producer = _mapping(payload["producer"], "candidate producer")
    _exact_keys(
        producer,
        frozenset({"request_sha256", "head", "run_id"}),
        "candidate producer",
    )
    if (
        _digest(producer["request_sha256"], "candidate producer request")
        != request_sha256
        or _git_sha(producer["head"], "candidate producer head") != source["commit"]
    ):
        raise TapRecordError("candidate producer differs from exact request source")
    _positive_integer(producer["run_id"], "candidate producer run ID")
    if payload["nonendorsed"] is not True:
        raise TapRecordError("public candidate must remain visibly nonendorsed")
    if artifact != bottle:
        raise TapRecordError("candidate common artifact differs from its bottle layer")
    if not subject["identity"].startswith(f"{tap}/{name}@sha256:") or not subject[
        "identity"
    ].endswith(bottle["sha256"]):
        raise TapRecordError("candidate subject differs from its exact bottle layer")
    normalized_by_id = {item["id"]: item["artifact"] for item in normalized}
    legacy_components = frozenset(
        {
            "bottle-contract",
            "bottle-metadata",
            "source-custody",
        }
    )
    descriptor_components = legacy_components | {
        "vfs-composition-descriptor",
    }
    if frozenset(normalized_by_id) not in {
        legacy_components,
        descriptor_components,
    }:
        raise TapRecordError("candidate normalized component inventory changed")
    if normalized_by_id.get("bottle-contract", {}).get("sha256") != formula[
        "bottle_contract_sha256"
    ]:
        raise TapRecordError("candidate normalized contract differs from Formula")
    if normalized_by_id.get("source-custody", {}).get("sha256") != custody_sha256:
        raise TapRecordError("candidate normalized custody differs from source record")
    if len(canonical_bytes(value)) > MAX_RECORD_BYTES:
        raise TapRecordError("candidate record exceeds its byte bound")


def _attempt_subject(value: Any) -> str:
    subject = _text(value, "attempt outcome subject", 512)
    try:
        parsed = json.loads(subject)
    except json.JSONDecodeError as error:
        raise TapRecordError("attempt outcome subject is not JSON") from error
    if not isinstance(parsed, Mapping) or set(parsed) != {
        "architecture",
        "identity",
        "kind",
    }:
        raise TapRecordError("attempt outcome subject fields changed")
    if parsed["kind"] != "formula":
        raise TapRecordError("attempt outcome subject is not a Formula")
    try:
        expected = exact_formula_subject(parsed["identity"], parsed["architecture"])
    except ValueError as error:
        raise TapRecordError(f"attempt outcome subject is invalid: {error}") from error
    if subject != expected:
        raise TapRecordError("attempt outcome subject is not canonical")
    return subject


def _timestamp(value: Any, field: str) -> str:
    result = _text(value, field, 64)
    if re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z",
        result,
    ) is None:
        raise TapRecordError(f"{field} is not millisecond UTC RFC 3339")
    return result


def validate_publication_failure(
    value: Any, *, field: str = "protected publication failure"
) -> dict[str, Any]:
    """Validate bounded transport facts recorded by protected publisher code."""

    failure = _mapping(value, field)
    _exact_keys(
        failure,
        frozenset(
            {"phase", "kind", "http_status", "retryable", "guard_code"}
        ),
        field,
    )
    phase = _stable_id(failure["phase"], f"{field} phase")
    kind = failure["kind"]
    if kind not in {
        "github-http",
        "registry-contract",
        "registry-http",
        "transport-reset",
    }:
        raise TapRecordError(f"{field} kind is unsupported")
    status = failure["http_status"]
    if status is not None and (
        isinstance(status, bool)
        or not isinstance(status, int)
        or not 100 <= status <= 599
    ):
        raise TapRecordError(f"{field} HTTP status is invalid")
    retryable = failure["retryable"]
    if not isinstance(retryable, bool):
        raise TapRecordError(f"{field} retryable fact is not Boolean")
    guard = _stable_id(failure["guard_code"], f"{field} guard")
    if kind in {"github-http", "registry-http"}:
        if status is None:
            raise TapRecordError(f"{field} HTTP failure lacks a status")
        expected_retryable = status == 429 or status >= 500
        if retryable != expected_retryable:
            raise TapRecordError(f"{field} HTTP retry classification is contradictory")
    elif kind == "transport-reset":
        if status is not None or not retryable:
            raise TapRecordError(f"{field} transport reset facts are contradictory")
    elif status is not None or retryable:
        raise TapRecordError(f"{field} registry contract facts are contradictory")
    return {
        "phase": phase,
        "kind": kind,
        "http_status": status,
        "retryable": retryable,
        "guard_code": guard,
    }


def validate_attempt_outcome_record(record: Mapping[str, Any]) -> None:
    value = _mapping(record, "attempt outcome record")
    _exact_keys(
        value,
        frozenset({"schema", "kind", "attempt"}),
        "attempt outcome record",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-attempt-outcome":
        raise TapRecordError("attempt outcome protocol is unsupported")
    attempt = _mapping(value["attempt"], "attempt outcome")
    expected_fields = {
        "request_sha256",
        "subject",
        "contract_sha256",
        "retry_ordinal",
        "outcome",
        "guard_code",
        "completed_at",
        "run",
        "handoff",
        "candidate_record_sha256",
    }
    if "publication_failure" in attempt:
        expected_fields.add("publication_failure")
    _exact_keys(attempt, frozenset(expected_fields), "attempt outcome")
    _digest(attempt["request_sha256"], "attempt outcome request")
    _attempt_subject(attempt["subject"])
    _digest(attempt["contract_sha256"], "attempt outcome contract")
    ordinal = _nonnegative_integer(attempt["retry_ordinal"], "attempt outcome ordinal")
    if ordinal > 3:
        raise TapRecordError("automatic attempt outcome ordinal exceeds three retries")
    outcome = attempt["outcome"]
    if outcome not in {"success", "failure", "timeout", "canceled"}:
        raise TapRecordError("attempt outcome is unsupported")
    guard = attempt["guard_code"]
    candidate = attempt["candidate_record_sha256"]
    if outcome == "success":
        if guard is not None or candidate is None:
            raise TapRecordError("successful attempt outcome is contradictory")
        _digest(candidate, "successful attempt candidate record")
    else:
        if candidate is not None or guard is None:
            raise TapRecordError("unsuccessful attempt outcome is contradictory")
        _stable_id(guard, "attempt outcome guard")
    publication_failure = attempt.get("publication_failure")
    if publication_failure is not None:
        checked_failure = validate_publication_failure(publication_failure)
        expected_guard = (
            "transient_infrastructure_failure"
            if checked_failure["retryable"]
            else checked_failure["guard_code"]
        )
        if outcome != "failure" or candidate is not None or guard != expected_guard:
            raise TapRecordError(
                "attempt publication failure and terminal outcome contradict"
            )
    _timestamp(attempt["completed_at"], "attempt outcome completion")
    _run(attempt["run"], "attempt outcome run")
    handoff = attempt["handoff"]
    if handoff is not None:
        handoff = _mapping(handoff, "attempt outcome handoff")
        _exact_keys(
            handoff,
            frozenset({"sha256", "bytes"}),
            "attempt outcome handoff",
        )
        _digest(handoff["sha256"], "attempt outcome handoff digest")
        _positive_integer(handoff["bytes"], "attempt outcome handoff bytes")
    if len(canonical_bytes(value)) > MAX_RECORD_BYTES:
        raise TapRecordError("attempt outcome record exceeds its byte bound")


def build_attempt_outcome_record(
    *,
    request_sha256: str,
    subject: str,
    contract_sha256: str,
    retry_ordinal: int,
    outcome: str,
    guard_code: str | None,
    completed_at: str,
    run: Mapping[str, Any],
    handoff: Mapping[str, Any] | None,
    candidate_record_sha256: str | None,
    publication_failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = {
        "schema": 1,
        "kind": "kandelo-abi-staging-attempt-outcome",
        "attempt": {
            "request_sha256": request_sha256,
            "subject": subject,
            "contract_sha256": contract_sha256,
            "retry_ordinal": retry_ordinal,
            "outcome": outcome,
            "guard_code": guard_code,
            "completed_at": completed_at,
            "run": dict(run),
            "handoff": None if handoff is None else dict(handoff),
            "candidate_record_sha256": candidate_record_sha256,
        },
    }
    if publication_failure is not None:
        record["attempt"]["publication_failure"] = dict(publication_failure)
    validate_attempt_outcome_record(record)
    return json.loads(canonical_bytes(record))


def build_attempt_outcome_oci_plan(
    record: Mapping[str, Any], *, repository: str
) -> OciRecordPlanV1:
    validate_attempt_outcome_record(record)
    checked_repository = _repository(repository, "attempt outcome repository")
    body = canonical_bytes(record)
    attempt = _mapping(record["attempt"], "attempt outcome")
    return OciRecordPlanV1(
        repository=checked_repository,
        artifact_type=ATTEMPT_OUTCOME_MEDIA_TYPE,
        config=OciBlobV1(
            role="attempt-outcome",
            media_type=ATTEMPT_OUTCOME_MEDIA_TYPE,
            body=body,
            title="attempt-outcome.json",
        ),
        layers=(
            OciBlobV1(
                role="immutable-record-bytes",
                media_type=ATTEMPT_OUTCOME_MEDIA_TYPE,
                body=body,
                title="attempt-outcome.json",
            ),
        ),
        annotations={
            "dev.kandelo.abi-staging.classification": "protected-attempt-outcome",
            "dev.kandelo.abi-staging.completed-at": str(attempt["completed_at"]),
            "dev.kandelo.abi-staging.kind": "attempt-outcome",
            "dev.kandelo.abi-staging.request-sha256": str(
                attempt["request_sha256"]
            ),
            "org.opencontainers.image.source": "https://github.com/"
            + str(_mapping(attempt["run"], "attempt outcome run")["repository"]),
        },
    )


def build_candidate_reuse_oci_plan(
    record: Mapping[str, Any], *, repository: str
) -> OciRecordPlanV1:
    """Wrap one factual cross-request binding without inventing a build."""

    try:
        validate_candidate_reuse_record(record)
    except ValueError as error:
        raise TapRecordError(f"candidate reuse record is invalid: {error}") from error
    checked_repository = _repository(repository, "candidate reuse repository")
    body = canonical_bytes(record)
    common = _mapping(record["common"], "candidate reuse common")
    payload = _mapping(record["candidate_reuse"], "candidate reuse payload")
    formula = _mapping(payload["formula"], "candidate reuse Formula")
    return OciRecordPlanV1(
        repository=checked_repository,
        artifact_type=CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
        config=OciBlobV1(
            role="candidate-reuse-record",
            media_type=CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
            body=body,
            title="candidate-reuse-record.json",
        ),
        layers=(
            OciBlobV1(
                role="immutable-record-bytes",
                media_type=CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
                body=body,
                title="candidate-reuse-record.json",
            ),
        ),
        annotations={
            "dev.kandelo.abi-staging.bottle-contract-sha256": str(
                formula["bottle_contract_sha256"]
            ),
            "dev.kandelo.abi-staging.classification": (
                "public-candidate-reuse-not-endorsement"
            ),
            "dev.kandelo.abi-staging.kind": "candidate-reuse",
            "dev.kandelo.abi-staging.request-sha256": str(
                common["request_sha256"]
            ),
            "org.opencontainers.image.source": "https://github.com/"
            + str(formula["tap"]),
        },
    )


def _record_link(value: Any, field: str) -> dict[str, str]:
    link = _mapping(value, field)
    _exact_keys(link, frozenset({"record_sha256", "immutable_reference"}), field)
    digest = _digest(link["record_sha256"], f"{field} digest")
    reference = _text(link["immutable_reference"], f"{field} reference", 4096)
    if f"sha256:{digest}" not in reference or any(
        character.isspace() for character in reference
    ):
        raise TapRecordError(f"{field} reference does not bind its digest")
    return {"record_sha256": digest, "immutable_reference": reference}


def _architecture(value: Any, field: str) -> str:
    if value not in {"wasm32", "wasm64"}:
        raise TapRecordError(f"{field} is unsupported")
    return value


def _maintainer(value: Any, field: str) -> dict[str, str]:
    maintainer = _mapping(value, field)
    _exact_keys(
        maintainer,
        frozenset({"login", "permission", "authorization_reference"}),
        field,
    )
    permission = _text(maintainer["permission"], f"{field} permission", 128)
    if permission not in {"maintain", "admin"}:
        raise TapRecordError(f"{field} lacks maintain permission")
    return {
        "login": _stable_id(maintainer["login"], f"{field} login"),
        "permission": permission,
        "authorization_reference": _text(
            maintainer["authorization_reference"], f"{field} reference", 2048
        ),
    }


def _policy_identity(value: Any, field: str) -> dict[str, Any]:
    policy = _mapping(value, field)
    _exact_keys(
        policy,
        frozenset(
            {
                "policy_version",
                "policy_sha256",
                "guard_registry_version",
                "guard_registry_sha256",
            }
        ),
        field,
    )
    return {
        "policy_version": _positive_integer(
            policy["policy_version"], f"{field} version"
        ),
        "policy_sha256": _digest(policy["policy_sha256"], f"{field} digest"),
        "guard_registry_version": _positive_integer(
            policy["guard_registry_version"], f"{field} guard registry version"
        ),
        "guard_registry_sha256": _digest(
            policy["guard_registry_sha256"], f"{field} guard registry digest"
        ),
    }


def validate_abi_history_record(record: Mapping[str, Any]) -> None:
    value = _mapping(record, "ABI history record")
    _exact_keys(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "plan",
                "created_ref_object",
                "protection_evidence",
                "metadata_verification_sha256",
                "public_readback_sha256",
                "run",
            }
        ),
        "ABI history record",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-history-record":
        raise TapRecordError("ABI history record protocol is unsupported")
    plan = _mapping(value["plan"], "ABI history plan")
    _exact_keys(
        plan,
        frozenset(
            {
                "source_abi",
                "successor_abi",
                "preactivation_tap_commit",
                "preactivation_tap_tree",
                "branch",
                "expected_current_metadata_sha256",
                "protection_requirement_sha256",
            }
        ),
        "ABI history plan",
    )
    source_abi = _nonnegative_integer(plan["source_abi"], "history source ABI")
    successor_abi = _nonnegative_integer(
        plan["successor_abi"], "history successor ABI"
    )
    if successor_abi != source_abi + 1:
        raise TapRecordError("ABI history transition must be exactly N to N+1")
    commit = _git_sha(plan["preactivation_tap_commit"], "history tap commit")
    tree = _git_sha(plan["preactivation_tap_tree"], "history tap tree")
    branch = _text(plan["branch"], "history branch", 128)
    if branch != f"abi/{source_abi}":
        raise TapRecordError("ABI history branch is not exact")
    _digest(
        plan["expected_current_metadata_sha256"], "history expected metadata"
    )
    requirement = _digest(
        plan["protection_requirement_sha256"], "history protection requirement"
    )
    if _git_sha(value["created_ref_object"], "created history ref") != commit:
        raise TapRecordError("created history ref differs from preactivation commit")
    evidence = _mapping(value["protection_evidence"], "history protection evidence")
    _exact_keys(
        evidence,
        frozenset(
            {
                "branch",
                "covered",
                "observed_protection_sha256",
                "protection_requirement_sha256",
                "ref_object",
                "ref_tree",
                "source",
            }
        ),
        "history protection evidence",
    )
    if evidence["covered"] is not True:
        raise TapRecordError("ABI history branch is not protected")
    if evidence["source"] not in {"branch-protection", "ruleset"}:
        raise TapRecordError("ABI history protection source is unsupported")
    if (
        evidence["branch"] != branch
        or _git_sha(evidence["ref_object"], "protected history ref") != commit
        or _git_sha(evidence["ref_tree"], "protected history tree") != tree
        or _digest(
            evidence["protection_requirement_sha256"],
            "observed history protection requirement",
        )
        != requirement
    ):
        raise TapRecordError("ABI history ref/tree/protection evidence differs from its plan")
    _digest(evidence["observed_protection_sha256"], "observed protection snapshot")
    _digest(value["metadata_verification_sha256"], "history metadata verification")
    _digest(value["public_readback_sha256"], "history public readback")
    _run(value["run"], "history run")
    if len(canonical_bytes(value)) > MAX_RECORD_BYTES:
        raise TapRecordError("ABI history record exceeds its byte bound")


def validate_historical_maintenance_authorization(
    record: Mapping[str, Any],
) -> None:
    value = _mapping(record, "historical maintenance authorization")
    _exact_keys(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "abi",
                "branch",
                "source",
                "formula",
                "reason",
                "maintainer",
                "policy",
                "history_record",
                "run",
            }
        ),
        "historical maintenance authorization",
    )
    if (
        value["schema"] != 1
        or value["kind"]
        != "kandelo-abi-historical-maintenance-authorization"
    ):
        raise TapRecordError("historical maintenance protocol is unsupported")
    abi = _nonnegative_integer(value["abi"], "historical maintenance ABI")
    branch = _text(value["branch"], "historical maintenance branch", 128)
    if branch != f"abi/{abi}":
        raise TapRecordError("historical maintenance must target exact abi/N")
    source = _record_source(value["source"], "historical maintenance source")
    formula = _mapping(value["formula"], "historical maintenance Formula")
    _exact_keys(
        formula,
        frozenset({"tap", "formula", "architecture"}),
        "historical maintenance Formula",
    )
    if _repository(formula["tap"], "historical Formula tap") != source["repository"]:
        raise TapRecordError("historical Formula tap differs from protected source")
    _stable_id(formula["formula"], "historical Formula name")
    _architecture(formula["architecture"], "historical Formula architecture")
    if value["reason"] not in {"failed-package-repair", "security-rebuild"}:
        raise TapRecordError("historical maintenance reason is not a repair reason")
    _maintainer(value["maintainer"], "historical maintenance maintainer")
    _policy_identity(value["policy"], "historical maintenance policy")
    _record_link(value["history_record"], "historical maintenance history record")
    _run(value["run"], "historical maintenance run")
    if len(canonical_bytes(value)) > MAX_RECORD_BYTES:
        raise TapRecordError("historical maintenance authorization exceeds its byte bound")


def _epoch_subject(value: Any, field: str) -> tuple[str, str]:
    subject = _mapping(value, field)
    _exact_keys(subject, frozenset({"formula", "architecture"}), field)
    return (
        _stable_id(subject["formula"], f"{field} Formula"),
        _architecture(subject["architecture"], f"{field} architecture"),
    )


def validate_abi_epoch_status(record: Mapping[str, Any]) -> None:
    value = _mapping(record, "ABI epoch status")
    _exact_keys(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "abi",
                "scheduled_subjects",
                "terminal_outcomes",
                "state",
                "repair_links",
                "run",
            }
        ),
        "ABI epoch status",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-epoch-status":
        raise TapRecordError("ABI epoch status protocol is unsupported")
    _nonnegative_integer(value["abi"], "epoch ABI")
    scheduled = [
        _epoch_subject(candidate, f"scheduled subject {index}")
        for index, candidate in enumerate(
            _sequence(value["scheduled_subjects"], "scheduled subjects")
        )
    ]
    if not scheduled or scheduled != sorted(set(scheduled)):
        raise TapRecordError("scheduled epoch subjects must be sorted and duplicate-free")
    terminal: list[tuple[str, str]] = []
    for index, candidate in enumerate(
        _sequence(value["terminal_outcomes"], "terminal outcomes")
    ):
        outcome = _mapping(candidate, f"terminal outcome {index}")
        _exact_keys(
            outcome,
            frozenset({"subject", "outcome", "record"}),
            f"terminal outcome {index}",
        )
        subject = _epoch_subject(outcome["subject"], f"terminal outcome {index} subject")
        if outcome["outcome"] not in {"success", "failure", "timeout", "canceled"}:
            raise TapRecordError("epoch terminal outcome is not terminal for retirement")
        _record_link(outcome["record"], f"terminal outcome {index} record")
        terminal.append(subject)
    if terminal != sorted(set(terminal)) or not set(terminal).issubset(scheduled):
        raise TapRecordError("epoch terminal subjects differ from its schedule")
    state = value["state"]
    if state not in {"active", "retiring", "retired"}:
        raise TapRecordError("ABI epoch state is unsupported")
    if state == "retired" and terminal != scheduled:
        raise TapRecordError("retired ABI epoch still has nonterminal scheduled subjects")
    if state == "retiring" and terminal == scheduled:
        raise TapRecordError("fully terminal ABI epoch must be retired")
    repair_links = [
        _record_link(candidate, f"repair link {index}")
        for index, candidate in enumerate(_sequence(value["repair_links"], "repair links"))
    ]
    repair_digests = [link["record_sha256"] for link in repair_links]
    if repair_digests != sorted(set(repair_digests)):
        raise TapRecordError("repair links must be sorted and duplicate-free")
    _run(value["run"], "ABI epoch run")
    if len(canonical_bytes(value)) > MAX_RECORD_BYTES:
        raise TapRecordError("ABI epoch status exceeds its byte bound")


def _formula_metadata_update(
    value: Any,
    *,
    tap_source: Mapping[str, str],
    canonical: Mapping[str, Any],
    layer: Mapping[str, Any],
) -> dict[str, Any]:
    update = _mapping(value, "Formula metadata update")
    historical_fields = frozenset(
        {
            "formula",
            "architecture",
            "expected_main_commit",
            "expected_normalized_formula_sha256",
            "expected_generated_metadata_sha256",
            "allowed_paths",
            "link_manifest_path",
            "link_manifest_sha256",
            "canonical_manifest_digest",
            "bottle_layer_sha256",
            "bottle_layer_bytes",
            "target_abi",
        }
    )
    current_fields = historical_fields | {"runtime_claim"}
    if frozenset(update) not in {historical_fields, current_fields}:
        _exact_keys(update, current_fields, "Formula metadata update")
    formula = _stable_id(update["formula"], "metadata Formula")
    architecture = _architecture(update["architecture"], "metadata architecture")
    if _git_sha(update["expected_main_commit"], "metadata expected main") != tap_source[
        "commit"
    ]:
        raise TapRecordError("Formula metadata update expected main differs from tap source")
    _digest(
        update["expected_normalized_formula_sha256"],
        "metadata normalized Formula",
    )
    _digest(
        update["expected_generated_metadata_sha256"],
        "metadata generated projection",
    )
    link_manifest_path = _title(
        update["link_manifest_path"], "metadata link manifest path"
    )
    if re.fullmatch(
        rf"Kandelo/link/{re.escape(formula)}-"
        rf"[A-Za-z0-9][A-Za-z0-9._+,-]{{0,255}}-"
        rf"rebuild(?:0|[1-9][0-9]{{0,9}})-{architecture}\.json",
        link_manifest_path,
    ) is None:
        raise TapRecordError("Formula metadata link manifest path is not exact")
    _digest(update["link_manifest_sha256"], "metadata link manifest")
    allowed = list(_sequence(update["allowed_paths"], "metadata allowed paths"))
    expected_paths = [
        f"Formula/{formula}.rb",
        f"Kandelo/formula/{formula}.json",
        "Kandelo/metadata.json",
        link_manifest_path,
    ]
    if allowed != expected_paths:
        raise TapRecordError("Formula metadata update path set is not exact")
    if (
        _digest(update["canonical_manifest_digest"], "metadata canonical manifest")
        != canonical["sha256"]
        or _digest(update["bottle_layer_sha256"], "metadata bottle layer")
        != layer["sha256"]
        or _positive_integer(update["bottle_layer_bytes"], "metadata bottle bytes")
        != layer["bytes"]
    ):
        raise TapRecordError("Formula metadata update differs from promoted layer/readback")
    _nonnegative_integer(update["target_abi"], "metadata target ABI")
    runtime_claim = update.get("runtime_claim")
    if runtime_claim is not None:
        # Local import avoids making the durable-record module part of the
        # tap-metadata module's import cycle.
        from .tap_metadata import TapMetadataError, validate_runtime_claim

        try:
            validate_runtime_claim(_mapping(runtime_claim, "metadata runtime claim"))
        except TapMetadataError as error:
            raise TapRecordError(f"Formula metadata runtime claim is invalid: {error}") from error
    return {"formula": formula, "architecture": architecture}


def build_admission_record(
    *,
    request_sha256: str,
    request_source: Mapping[str, Any],
    run: Mapping[str, Any],
    candidate_record_sha256: str,
    candidate_binding_sha256: str,
    promoted_layer: Mapping[str, Any],
    qualifying_receipt_sha256s: Sequence[str],
    merged_pull_request: Mapping[str, Any],
    abi_history_record_sha256: str,
    preactivation_tap_source: Mapping[str, Any],
    tap_source: Mapping[str, Any],
    canonical: Mapping[str, Any],
    canonical_public_readback_sha256: str,
    formula_metadata_source: Mapping[str, Any],
    formula_metadata_update: Mapping[str, Any],
    original_producer: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one admission only after canonical and tap metadata readback."""

    candidate_digest = _digest(candidate_record_sha256, "admission candidate record")
    record = {
        "schema": 1,
        "kind": "kandelo-abi-staging-admission",
        "common": {
            "request_sha256": _digest(request_sha256, "admission request"),
            "subject": {"kind": "candidate", "identity": candidate_digest},
            "source": _plain(request_source),
            "run": _plain(run),
            "guard_codes": [],
            "work_state": "complete",
            "outcome": "success",
            "artifact_class": "canonical",
            "artifact": _plain(canonical),
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
            "candidate_record_sha256": candidate_digest,
            "candidate_binding_sha256": _digest(
                candidate_binding_sha256, "admission candidate binding"
            ),
            "promoted_layer": _plain(promoted_layer),
            "qualifying_receipt_sha256s": list(qualifying_receipt_sha256s),
            "merged_pull_request": _plain(merged_pull_request),
            "abi_history_record_sha256": _digest(
                abi_history_record_sha256, "admission ABI history record"
            ),
            "preactivation_tap_source": _plain(preactivation_tap_source),
            "tap_source": _plain(tap_source),
            "canonical": _plain(canonical),
            "canonical_public_readback_sha256": _digest(
                canonical_public_readback_sha256, "canonical public readback"
            ),
            "formula_metadata_source": _plain(formula_metadata_source),
            "formula_metadata_update": _plain(formula_metadata_update),
            "original_producer": _plain(original_producer),
        },
    }
    validate_admission_record(record)
    return record


def validate_admission_record(record: Mapping[str, Any]) -> None:
    value = _mapping(record, "admission record")
    _exact_keys(
        value,
        frozenset({"schema", "kind", "common", "admission"}),
        "admission record",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-admission":
        raise TapRecordError("admission record protocol is unsupported")
    common = _mapping(value["common"], "admission common")
    _exact_keys(
        common,
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
                "artifact",
                "promotion_state",
                "retry_state",
                "blockers",
            }
        ),
        "admission common",
    )
    _digest(common["request_sha256"], "admission request")
    source = _record_source(common["source"], "admission source")
    _run(common["run"], "admission run")
    canonical_common = _validated_artifact(common["artifact"], "admission artifact")
    if (
        common["guard_codes"] != []
        or common["work_state"] != "complete"
        or common["outcome"] != "success"
        or common["artifact_class"] != "canonical"
        or common["promotion_state"] != "promoted"
        or common["blockers"] != []
        or common["retry_state"]
        != {
            "attempts": 0,
            "eligible": False,
            "exhausted": False,
            "next_action": "none",
        }
    ):
        raise TapRecordError("admission common state is contradictory")
    subject = _mapping(common["subject"], "admission subject")
    _exact_keys(subject, frozenset({"kind", "identity"}), "admission subject")
    if subject["kind"] != "candidate":
        raise TapRecordError("admission subject must be the candidate record")
    payload = _mapping(value["admission"], "admission payload")
    _exact_keys(
        payload,
        frozenset(
            {
                "candidate_record_sha256",
                "candidate_binding_sha256",
                "promoted_layer",
                "qualifying_receipt_sha256s",
                "merged_pull_request",
                "abi_history_record_sha256",
                "preactivation_tap_source",
                "tap_source",
                "canonical",
                "canonical_public_readback_sha256",
                "formula_metadata_source",
                "formula_metadata_update",
                "original_producer",
            }
        ),
        "admission payload",
    )
    candidate_digest = _digest(
        payload["candidate_record_sha256"], "admission candidate record"
    )
    _digest(payload["candidate_binding_sha256"], "admission candidate binding")
    if subject["identity"] != candidate_digest:
        raise TapRecordError("admission subject differs from candidate record")
    layer = _validated_artifact(payload["promoted_layer"], "promoted bottle layer")
    receipts = list(
        _sequence(payload["qualifying_receipt_sha256s"], "admission receipts")
    )
    checked_receipts = [_digest(item, "admission receipt") for item in receipts]
    if not checked_receipts or checked_receipts != sorted(set(checked_receipts)):
        raise TapRecordError("admission receipts must be sorted and duplicate-free")
    merged = _mapping(payload["merged_pull_request"], "admission merged PR")
    _exact_keys(
        merged,
        frozenset({"repository", "number", "head", "merge_commit"}),
        "admission merged PR",
    )
    merged_repository = _text(
        merged["repository"], "admission PR repository", 255
    )
    if not re.fullmatch(
        r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", merged_repository
    ):
        raise TapRecordError("admission PR repository is not owner/name")
    _positive_integer(merged["number"], "admission PR number")
    merged_head = _git_sha(merged["head"], "admission PR head")
    merge_commit = _git_sha(merged["merge_commit"], "admission merge commit")
    if source["repository"].lower() != merged_repository.lower() or source[
        "commit"
    ] != merged_head:
        raise TapRecordError("admission source differs from exact merged PR head")
    _digest(
        payload["abi_history_record_sha256"], "admission ABI history record"
    )
    preactivation_tap_source = _record_source(
        payload["preactivation_tap_source"],
        "admission preactivation tap source",
    )
    tap_source = _record_source(payload["tap_source"], "admission tap source")
    canonical = _validated_artifact(payload["canonical"], "admission canonical artifact")
    if canonical != canonical_common:
        raise TapRecordError("admission common artifact differs from canonical artifact")
    if (
        _digest(payload["canonical_public_readback_sha256"], "canonical readback")
        != canonical["sha256"]
    ):
        raise TapRecordError("canonical readback differs from admitted manifest")
    metadata_source = _record_source(
        payload["formula_metadata_source"], "Formula metadata source"
    )
    if (
        preactivation_tap_source["repository"].lower()
        != tap_source["repository"].lower()
        or preactivation_tap_source["commit"] == tap_source["commit"]
        or metadata_source["repository"].lower()
        != tap_source["repository"].lower()
        or metadata_source["commit"] == tap_source["commit"]
        or tap_source["repository"].lower() == merged_repository.lower()
    ):
        raise TapRecordError("Formula metadata update source is absent or wrong")
    _formula_metadata_update(
        payload["formula_metadata_update"],
        tap_source=tap_source,
        canonical=canonical,
        layer=layer,
    )
    producer = _mapping(payload["original_producer"], "admission original producer")
    _exact_keys(
        producer,
        frozenset({"request_sha256", "head", "run_id"}),
        "admission original producer",
    )
    _digest(producer["request_sha256"], "original producer request")
    producer_head = _git_sha(producer["head"], "original producer head")
    _positive_integer(producer["run_id"], "original producer run ID")
    if producer_head == merge_commit:
        raise TapRecordError("admission rewrote original producer to the merge commit")
    if len(canonical_bytes(value)) > MAX_RECORD_BYTES:
        raise TapRecordError("admission record exceeds its byte bound")


def validate_durable_record(record: Mapping[str, Any]) -> None:
    value = _mapping(record, "durable record")
    kind = value.get("kind")
    validators = {
        "kandelo-abi-history-record": validate_abi_history_record,
        "kandelo-abi-historical-maintenance-authorization": (
            validate_historical_maintenance_authorization
        ),
        "kandelo-abi-epoch-status": validate_abi_epoch_status,
        "kandelo-abi-staging-admission": validate_admission_record,
        "kandelo-abi-staging-candidate": validate_candidate_record,
        "kandelo-abi-staging-attempt-outcome": validate_attempt_outcome_record,
    }
    validator = validators.get(kind)
    if validator is None:
        raise TapRecordError(f"unknown durable record kind {kind!r}")
    validator(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def load_tap_plan_record(body: bytes) -> dict[str, Any]:
    try:
        value = _plain(parse_canonical_bytes(body, maximum_bytes=MAX_TAP_PLAN_BYTES))
        validate_tap_plan(value)
    except (CanonicalJsonError, PlanError) as error:
        raise TapRecordError(f"tap plan record is invalid: {error}") from error
    return value
