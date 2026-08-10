"""Publish immutable candidate VFS identity and separate factual evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any

from .canonical import (
    CanonicalJsonError,
    canonical_bytes,
    canonical_sha256,
    parse_canonical_bytes,
)
from .oci import (
    OciPublicationError,
    OciTransportV1,
    PublishedRecordLocatorV1,
    fetch_public_record,
    list_public_record_locators,
    publish_record,
)
from .plan import PlanError, parse_formula_subject
from .product import (
    CandidateProductArtifactV1,
    ProductInputPlanV1,
    ProductInputResolutionError,
    load_resolved_product_inputs,
    select_product_execution_scope,
)
from .records import OciBlobV1, OciRecordPlanV1


PRODUCT_CANDIDATE_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.product.candidate.v1+json"
)
PRODUCT_EVIDENCE_RECEIPT_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.product.evidence-receipt.v1+json"
)
PRODUCT_EVIDENCE_RECORD_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.product.evidence-record.v1+json"
)
PRODUCT_EVIDENCE_RESULT_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.product.evidence-result.v1+json"
)
VFS_IMAGE_MEDIA_TYPE = "application/vnd.kandelo.vfs.image.v1"
BUILDER_REPORT_MEDIA_TYPE = "application/vnd.kandelo.vfs.builder-report.v1+json"
RESOLVED_INPUTS_MEDIA_TYPE = (
    "application/vnd.kandelo.vfs.resolved-inputs.v1+json"
)
RUNTIME_BUNDLE_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.runtime-bundle.v1+json"
)
LAZY_INPUT_MEDIA_TYPE = "application/vnd.kandelo.vfs.lazy-input.v1"

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_RUNTIME_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_FILES = 32_768
MAX_RUNTIME_BYTES = 8 * 1024 * 1024 * 1024
MAX_RESULT_DIAGNOSTICS = 64
MAX_DIAGNOSTIC_BYTES = 64 * 1024
BROWSER_RUNTIME_KEYS = frozenset(
    {
        "bundle_sha256",
        "bytes",
        "harness_entry_bytes",
        "harness_entry_path",
        "harness_entry_sha256",
        "host_entry_bytes",
        "host_entry_path",
        "host_entry_sha256",
        "kernel_asset_path",
        "kernel_asset_sha256",
        "service_worker_sha256",
    }
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9@+._-]{0,255}$")
REPOSITORY = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)
PRODUCT_REPOSITORY = re.compile(
    r"^(?P<base>[a-z0-9]+(?:[._-][a-z0-9]+)*/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*-abi-(?P<abi>[0-9]+)-candidates)"
    r"/products/(?P<product>[a-z0-9][a-z0-9._-]{0,127})$"
)


class ProductEvidenceError(ValueError):
    """Raised when product identity or evidence is incomplete or contradictory."""


def select_product_evidence_execution_scope(
    request: Mapping[str, Any],
    *,
    request_sha256: str,
    product_id: str,
    product_work_id: str,
    host: str,
    definition_id: str,
    work_id: str,
) -> dict[str, Any]:
    """Bind one host evidence job to its exact request-selected parent work."""

    if host not in {"node", "browser"}:
        raise ProductEvidenceError("product evidence work host is unsupported")
    checked_definition_id = _stable_id(
        definition_id, "product evidence work definition ID"
    )
    checked_product_work_id = _digest(
        product_work_id, "product evidence parent work ID"
    )
    checked_work_id = _digest(work_id, "product evidence work ID")
    try:
        product_scope = select_product_execution_scope(
            request,
            request_sha256=request_sha256,
            product_id=product_id,
            work_id=checked_product_work_id,
        )
    except ProductInputResolutionError as error:
        raise ProductEvidenceError(
            f"product evidence parent work is invalid: {error}"
        ) from error

    checked_request = _plain(request)
    requirements = checked_request.get("requirements")
    if not isinstance(requirements, Mapping):
        raise ProductEvidenceError("product evidence work requirements are missing")
    selected_bindings = []
    for index, candidate in enumerate(
        _sequence(requirements.get("evidence"), "product evidence work bindings")
    ):
        binding = _exact(
            candidate,
            frozenset({"applicability", "browser", "node", "product_id"}),
            f"product evidence work binding {index}",
        )
        binding_product_id = _stable_id(
            binding["product_id"],
            f"product evidence work binding {index} product ID",
        )
        if binding_product_id == product_scope["id"]:
            selected_bindings.append(binding)
    if len(selected_bindings) != 1:
        raise ProductEvidenceError(
            "product evidence work lacks one exact product binding"
        )
    selected_ids = [
        _stable_id(value, f"product evidence work {host} definition")
        for value in _sequence(
            selected_bindings[0][host],
            f"product evidence work {host} definitions",
        )
    ]
    if selected_ids != sorted(set(selected_ids)):
        raise ProductEvidenceError(
            f"product evidence work {host} definitions are not sorted and unique"
        )
    if checked_definition_id not in selected_ids:
        raise ProductEvidenceError(
            "product evidence definition is not selected by the exact request"
        )

    expected_work_id = canonical_sha256(
        {
            "applicability": product_scope["applicability"],
            "definition_id": checked_definition_id,
            "manifest_sha256": product_scope["manifest_sha256"],
            "product_id": product_scope["id"],
            "request_digest": request_sha256,
            "stage": f"{host}-product-evidence",
        }
    )
    if checked_work_id != expected_work_id:
        raise ProductEvidenceError(
            "product evidence work ID differs from its selected host scope"
        )
    return {
        **product_scope,
        "definition_id": checked_definition_id,
        "host": host,
        "product_work_id": checked_product_work_id,
        "work_id": checked_work_id,
    }


def select_product_evidence_publication_scope(
    request: Mapping[str, Any],
    *,
    request_sha256: str,
    product_id: str,
    product_work_id: str,
    work_id: str,
    definitions: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one publisher to the complete protected terminal-result set."""

    checked_request = _plain(request)
    checked_request_sha256 = _digest(
        request_sha256, "product evidence publication request"
    )
    if canonical_sha256(checked_request) != checked_request_sha256:
        raise ProductEvidenceError(
            "product evidence publication request digest is not canonical"
        )
    checked_product_work_id = _digest(
        product_work_id, "product evidence publication parent work"
    )
    try:
        product_scope = select_product_execution_scope(
            checked_request,
            request_sha256=checked_request_sha256,
            product_id=product_id,
            work_id=checked_product_work_id,
        )
    except ProductInputResolutionError as error:
        raise ProductEvidenceError(
            f"product evidence publication parent work is invalid: {error}"
        ) from error

    requirements_value = checked_request.get("requirements")
    if not isinstance(requirements_value, Mapping):
        raise ProductEvidenceError(
            "product evidence publication requirements are missing"
        )
    bindings = []
    for index, candidate in enumerate(
        _sequence(
            requirements_value.get("evidence"),
            "product evidence publication bindings",
        )
    ):
        binding = _exact(
            candidate,
            frozenset({"applicability", "browser", "node", "product_id"}),
            f"product evidence publication binding {index}",
        )
        if binding["product_id"] == product_scope["id"]:
            bindings.append(binding)
    if len(bindings) != 1 or bindings[0]["applicability"] != product_scope["applicability"]:
        raise ProductEvidenceError(
            "product evidence publication lacks one exact product binding"
        )
    binding = bindings[0]

    registry = _exact(
        definitions,
        frozenset({"definitions", "kind", "schema", "version"}),
        "protected evidence definition registry",
    )
    if (
        registry["schema"] != 1
        or registry["kind"] != "kandelo-vfs-evidence-definitions"
        or _integer(
            registry["version"],
            "protected evidence definition registry version",
            positive=True,
        )
        < 1
    ):
        raise ProductEvidenceError(
            "protected evidence definition registry is unsupported"
        )
    by_id: dict[str, dict[str, Any]] = {}
    previous = ""
    for index, candidate in enumerate(
        _sequence(registry["definitions"], "protected evidence definitions")
    ):
        definition = _validate_context_definition(
            candidate, f"protected evidence definition {index}"
        )
        if definition["id"] <= previous:
            raise ProductEvidenceError(
                "protected evidence definitions must be sorted and duplicate-free"
            )
        previous = definition["id"]
        by_id[definition["id"]] = definition

    base_identity = {
        "applicability": product_scope["applicability"],
        "manifest_sha256": product_scope["manifest_sha256"],
        "product_id": product_scope["id"],
        "request_digest": checked_request_sha256,
    }
    expected_work_id = canonical_sha256(
        {**base_identity, "stage": "publish-product-evidence"}
    )
    if _digest(work_id, "product evidence publication work") != expected_work_id:
        raise ProductEvidenceError(
            "product evidence publication work ID differs from its selected scope"
        )

    selected_requirements = []
    evidence_work = []
    for host in ("browser", "node"):
        selected_ids = [
            _stable_id(value, f"product evidence publication {host} definition")
            for value in _sequence(
                binding[host], f"product evidence publication {host} definitions"
            )
        ]
        if selected_ids != sorted(set(selected_ids)):
            raise ProductEvidenceError(
                f"product evidence publication {host} definitions are not sorted and unique"
            )
        for definition_id in selected_ids:
            definition = by_id.get(definition_id)
            if definition is None or definition["host"] != host:
                raise ProductEvidenceError(
                    "selected product evidence definition differs from protected policy"
                )
            selected_requirements.append(
                {
                    "applicability": product_scope["applicability"],
                    "definition_sha256": definition["definition_sha256"],
                    "host": host,
                    "id": definition_id,
                }
            )
            evidence_work.append(
                {
                    "definition_id": definition_id,
                    "host": host,
                    "work_id": canonical_sha256(
                        {
                            **base_identity,
                            "definition_id": definition_id,
                            "stage": f"{host}-product-evidence",
                        }
                    ),
                }
            )
    if not selected_requirements:
        raise ProductEvidenceError(
            "product evidence publication has no selected terminal definitions"
        )
    return {
        **product_scope,
        "product_work_id": checked_product_work_id,
        "work_id": expected_work_id,
        "requirements": selected_requirements,
        "evidence_work": evidence_work,
        "selecting_registries": _registries(
            requirements_value.get("registries")
        ),
    }


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _require_blob_descriptor(
    blob: OciBlobV1,
    *,
    role: str,
    media_type: str,
    title: str,
    field: str,
) -> None:
    if (
        blob.role != role
        or blob.media_type != media_type
        or blob.title != title
        or blob.mount_from is not None
    ):
        raise ProductEvidenceError(f"{field} OCI descriptor metadata changed")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def _load_canonical(
    body: bytes, field: str, *, maximum_bytes: int = MAX_DOCUMENT_BYTES
) -> dict[str, Any]:
    try:
        value = _plain(
            parse_canonical_bytes(
                body,
                maximum_bytes=maximum_bytes,
                maximum_items=MAX_RUNTIME_FILES * 16,
                maximum_string_bytes=16 * 1024,
            )
        )
    except CanonicalJsonError as error:
        raise ProductEvidenceError(f"{field} is not canonical: {error}") from error
    if not isinstance(value, dict):
        raise ProductEvidenceError(f"{field} must be an object")
    return value


def _exact(value: Any, expected: frozenset[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != expected:
        raise ProductEvidenceError(f"{field} fields changed")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ProductEvidenceError(f"{field} must be an array")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise ProductEvidenceError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ProductEvidenceError(f"{field} is not UTF-8") from error
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise ProductEvidenceError(f"{field} is outside its string bound")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ProductEvidenceError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _oci_digest(value: Any, field: str) -> str:
    result = _text(value, field, 71)
    if not result.startswith("sha256:") or SHA256.fullmatch(result[7:]) is None:
        raise ProductEvidenceError(f"{field} is not an OCI SHA-256 digest")
    return result


def _git_sha(value: Any, field: str) -> str:
    result = _text(value, field, 40)
    if GIT_SHA.fullmatch(result) is None:
        raise ProductEvidenceError(f"{field} is not a full lowercase Git SHA")
    return result


def _stable_id(value: Any, field: str) -> str:
    result = _text(value, field, 256)
    if STABLE_ID.fullmatch(result) is None:
        raise ProductEvidenceError(f"{field} is not a stable identity")
    return result


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 2**64 - 1
    ):
        raise ProductEvidenceError(f"{field} is not a bounded integer")
    return value


def _relative_path(value: Any, field: str) -> str:
    path = _text(value, field)
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ProductEvidenceError(f"{field} is not a normalized relative path")
    return path


def _repository(value: Any, field: str) -> str:
    result = _text(value, field, 512)
    if REPOSITORY.fullmatch(result) is None:
        raise ProductEvidenceError(f"{field} is not a lowercase OCI repository")
    return result


def _artifact(value: Any, field: str) -> dict[str, Any]:
    artifact = _exact(
        value, frozenset({"sha256", "bytes", "immutable_reference"}), field
    )
    digest = _digest(artifact["sha256"], f"{field} digest")
    size = _integer(artifact["bytes"], f"{field} bytes", positive=True)
    reference = _text(artifact["immutable_reference"], f"{field} reference")
    if any(character.isspace() for character in reference) or f"sha256:{digest}" not in reference:
        raise ProductEvidenceError(f"{field} reference does not bind its digest")
    return {"sha256": digest, "bytes": size, "immutable_reference": reference}


def _artifact_for_blob(repository: str, digest: str, size: int) -> dict[str, Any]:
    return {
        "sha256": _digest(digest, "artifact digest"),
        "bytes": _integer(size, "artifact bytes", positive=True),
        "immutable_reference": f"ghcr.io/{repository}@sha256:{digest}",
    }


def _validate_source(value: Any, field: str) -> dict[str, str]:
    source = _exact(value, frozenset({"repository", "commit", "tree"}), field)
    repository = _text(source["repository"], f"{field} repository", 255)
    if re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository) is None:
        raise ProductEvidenceError(f"{field} repository is not owner/name")
    return {
        "repository": repository,
        "commit": _git_sha(source["commit"], f"{field} commit"),
        "tree": _git_sha(source["tree"], f"{field} tree"),
    }


def _validate_product(value: Any, field: str, *, full: bool) -> dict[str, Any]:
    expected = (
        frozenset({"id", "manifest_path", "manifest_sha256", "architecture", "output"})
        if full
        else frozenset({"id", "manifest_sha256"})
    )
    product = _exact(value, expected, field)
    result: dict[str, Any] = {
        "id": _stable_id(product["id"], f"{field} ID"),
        "manifest_sha256": _digest(
            product["manifest_sha256"], f"{field} manifest digest"
        ),
    }
    if full:
        result.update(
            {
                "manifest_path": _relative_path(
                    product["manifest_path"], f"{field} manifest path"
                ),
                "architecture": product["architecture"],
                "output": _text(product["output"], f"{field} output", 255),
            }
        )
        if result["architecture"] not in {"wasm32", "wasm64"}:
            raise ProductEvidenceError(f"{field} architecture is unsupported")
        if (
            result["output"].startswith(".")
            or "/" in result["output"]
            or "\\" in result["output"]
            or not result["output"].endswith((".vfs", ".vfs.zst"))
        ):
            raise ProductEvidenceError(f"{field} output is not a VFS filename")
    return result


def candidate_product_repository(
    *,
    owner: str,
    repository_prefix: str,
    candidate_suffix: str,
    target_abi: int,
    product_id: str,
) -> str:
    """Derive the one reserved product repository from protected policy."""

    checked_owner = _text(owner, "candidate owner", 255)
    checked_prefix = _text(repository_prefix, "candidate repository prefix", 255)
    checked_suffix = _text(candidate_suffix, "candidate suffix", 64)
    product = _stable_id(product_id, "candidate product ID")
    abi = _integer(target_abi, "candidate target ABI", positive=True)
    if abi > 2**32 - 1:
        raise ProductEvidenceError("candidate target ABI exceeds its unsigned 32-bit bound")
    repository = f"{checked_owner}/{checked_prefix}{abi}{checked_suffix}/products/{product}"
    _repository(repository, "candidate product repository")
    _validate_product_repository(repository, product, abi)
    return repository


def _validate_product_repository(repository: str, product_id: str, target_abi: int) -> None:
    checked = _repository(repository, "candidate product repository")
    match = PRODUCT_REPOSITORY.fullmatch(checked)
    if (
        match is None
        or match.group("product") != product_id
        or int(match.group("abi")) != target_abi
    ):
        raise ProductEvidenceError(
            "candidate product repository differs from its exact product or ABI"
        )


@dataclass(frozen=True)
class CandidateProductLocatorV1:
    product_id: str
    repository: str
    manifest_digest: str
    immutable_reference: str
    vfs_layer_sha256: str
    vfs_layer_bytes: int
    builder_report_sha256: str

    def __post_init__(self) -> None:
        product = _stable_id(self.product_id, "candidate product locator ID")
        repository = _text(self.repository, "candidate product locator repository", 520)
        if not repository.startswith("ghcr.io/"):
            raise ProductEvidenceError("candidate product locator is not in GHCR")
        match = PRODUCT_REPOSITORY.fullmatch(repository[len("ghcr.io/") :])
        if match is None or match.group("product") != product:
            raise ProductEvidenceError(
                "candidate product locator is outside its reserved candidate repository"
            )
        digest = _oci_digest(self.manifest_digest, "candidate product manifest digest")
        if self.immutable_reference != f"{repository}@{digest}":
            raise ProductEvidenceError("candidate product locator is not immutable")
        _digest(self.vfs_layer_sha256, "candidate product VFS layer")
        _integer(self.vfs_layer_bytes, "candidate product VFS bytes", positive=True)
        _digest(self.builder_report_sha256, "candidate product builder report")

    def as_public_locator(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "digest": self.manifest_digest,
            "immutable_reference": self.immutable_reference,
        }

    def evidence_identity(self) -> dict[str, Any]:
        return {
            "manifest_digest": self.manifest_digest,
            "vfs_layer_sha256": self.vfs_layer_sha256,
            "vfs_layer_bytes": self.vfs_layer_bytes,
            "builder_report_sha256": self.builder_report_sha256,
        }


@dataclass(frozen=True)
class CandidateProductInventoryEntryV1:
    """One current-request candidate reconstructed from public OCI identity."""

    artifact: CandidateProductArtifactV1
    locator: CandidateProductLocatorV1
    runtime_bundle_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, CandidateProductArtifactV1):
            raise ProductEvidenceError("candidate product inventory artifact is invalid")
        if not isinstance(self.locator, CandidateProductLocatorV1):
            raise ProductEvidenceError("candidate product inventory locator is invalid")
        _digest(
            self.runtime_bundle_sha256,
            "candidate product inventory runtime bundle",
        )
        if (
            self.artifact.product_id != self.locator.product_id
            or self.artifact.vfs_layer_sha256 != self.locator.vfs_layer_sha256
            or self.artifact.vfs_layer_bytes != self.locator.vfs_layer_bytes
            or self.artifact.builder_report_sha256
            != self.locator.builder_report_sha256
        ):
            raise ProductEvidenceError(
                "candidate product inventory artifact differs from its locator"
            )


@dataclass(frozen=True)
class ProductEvidenceInventoryEntryV1:
    """One complete aggregate reconstructed without downloading its layers."""

    record: Mapping[str, Any]
    record_sha256: str
    manifest_digest: str
    outcome: str
    immutable_reference: str

    def __post_init__(self) -> None:
        validate_product_evidence_record(self.record)
        _digest(self.record_sha256, "product evidence inventory record")
        if canonical_sha256(self.record) != self.record_sha256:
            raise ProductEvidenceError(
                "product evidence inventory record digest is not canonical"
            )
        if self.outcome != self.record["common"]["outcome"]:
            raise ProductEvidenceError(
                "product evidence inventory outcome differs from its record"
            )
        manifest_digest = _oci_digest(
            self.manifest_digest, "product evidence inventory manifest"
        )
        reference = _text(
            self.immutable_reference,
            "product evidence inventory immutable reference",
            1024,
        )
        if not reference.endswith("@" + manifest_digest):
            raise ProductEvidenceError(
                "product evidence inventory reference differs from its record"
            )


def load_candidate_product_locator(
    body: bytes, *, expected_repository: str
) -> CandidateProductLocatorV1:
    """Load one canonical locator and bind it to protected repository authority."""

    value = _exact(
        _load_canonical(body, "candidate product locator"),
        frozenset(
            {
                "builder_report_sha256",
                "immutable_reference",
                "manifest_digest",
                "product_id",
                "repository",
                "vfs_layer_bytes",
                "vfs_layer_sha256",
            }
        ),
        "candidate product locator",
    )
    expected = _text(
        expected_repository, "protected candidate product repository", 520
    )
    if value["repository"] != expected:
        raise ProductEvidenceError(
            "candidate product locator differs from its protected repository"
        )
    try:
        return CandidateProductLocatorV1(
            product_id=value["product_id"],
            repository=value["repository"],
            manifest_digest=value["manifest_digest"],
            immutable_reference=value["immutable_reference"],
            vfs_layer_sha256=value["vfs_layer_sha256"],
            vfs_layer_bytes=value["vfs_layer_bytes"],
            builder_report_sha256=value["builder_report_sha256"],
        )
    except (TypeError, ValueError) as error:
        if isinstance(error, ProductEvidenceError):
            raise
        raise ProductEvidenceError(
            f"candidate product locator is invalid: {error}"
        ) from error


def _validate_runtime_bundle(
    body: bytes,
    *,
    expected_inputs: Mapping[str, Any] | None = None,
    runtime_files: Mapping[str, bytes] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    runtime = _load_canonical(
        body, "exact runtime bundle", maximum_bytes=MAX_RUNTIME_BUNDLE_BYTES
    )
    _exact(
        runtime,
        frozenset(
            {
                "schema",
                "kind",
                "source",
                "target_abi",
                "kernel",
                "host",
                "browser",
                "build_policy_sha256",
                "inventory",
            }
        ),
        "exact runtime bundle",
    )
    if runtime["schema"] != 1 or runtime["kind"] != "kandelo-exact-runtime-bundle":
        raise ProductEvidenceError("exact runtime bundle protocol is unsupported")
    source = _validate_source(runtime["source"], "runtime source")
    target = _exact(
        runtime["target_abi"], frozenset({"version", "snapshot_sha256"}), "runtime ABI"
    )
    version = _integer(target["version"], "runtime ABI version")
    snapshot = _digest(target["snapshot_sha256"], "runtime ABI snapshot")
    kernel = _exact(
        runtime["kernel"],
        frozenset({"wasm_sha256", "bytes", "abi_version", "snapshot_sha256"}),
        "runtime kernel",
    )
    kernel_sha = _digest(kernel["wasm_sha256"], "runtime kernel Wasm")
    kernel_bytes = _integer(kernel["bytes"], "runtime kernel bytes", positive=True)
    if (
        _integer(kernel["abi_version"], "runtime kernel ABI") != version
        or _digest(kernel["snapshot_sha256"], "runtime kernel snapshot") != snapshot
    ):
        raise ProductEvidenceError("runtime kernel differs from the exact target ABI")
    host = _exact(
        runtime["host"],
        frozenset(
            {"bundle_sha256", "bytes", "generated_abi_sha256", "worker_protocol_sha256"}
        ),
        "runtime host",
    )
    host_sha = _digest(host["bundle_sha256"], "runtime host bundle")
    host_bytes = _integer(host["bytes"], "runtime host bytes", positive=True)
    generated_sha = _digest(host["generated_abi_sha256"], "runtime generated ABI")
    protocol_sha = _digest(host["worker_protocol_sha256"], "runtime worker protocol")
    browser = _exact(
        runtime["browser"],
        BROWSER_RUNTIME_KEYS,
        "runtime browser",
    )
    browser_sha = _digest(browser["bundle_sha256"], "runtime browser bundle")
    browser_bytes = _integer(browser["bytes"], "runtime browser bytes", positive=True)
    harness_path = _relative_path(
        browser["harness_entry_path"], "runtime browser harness entry path"
    )
    harness_sha = _digest(
        browser["harness_entry_sha256"], "runtime browser harness entry"
    )
    harness_bytes = _integer(
        browser["harness_entry_bytes"],
        "runtime browser harness entry bytes",
        positive=True,
    )
    host_entry_path = _relative_path(
        browser["host_entry_path"], "runtime browser host entry path"
    )
    host_entry_sha = _digest(
        browser["host_entry_sha256"], "runtime browser host entry"
    )
    host_entry_bytes = _integer(
        browser["host_entry_bytes"],
        "runtime browser host entry bytes",
        positive=True,
    )
    kernel_asset_path = _relative_path(
        browser["kernel_asset_path"], "runtime browser kernel asset path"
    )
    kernel_asset_sha = _digest(
        browser["kernel_asset_sha256"], "runtime browser kernel asset"
    )
    service_worker_sha = _digest(
        browser["service_worker_sha256"], "runtime service worker"
    )
    if (
        harness_path != "browser/dist/abi-staging-harness/index.html"
        or host_entry_path != "browser/dist/abi-staging/browser-host.js"
        or not kernel_asset_path.startswith("browser/dist/")
        or not kernel_asset_path.endswith(".wasm")
        or kernel_asset_sha != kernel_sha
    ):
        raise ProductEvidenceError("runtime browser entry identity differs")
    policy_sha = _digest(runtime["build_policy_sha256"], "runtime build policy")

    inventory = _sequence(runtime["inventory"], "runtime inventory")
    if not inventory or len(inventory) > MAX_RUNTIME_FILES:
        raise ProductEvidenceError("runtime inventory is empty or exceeds its bound")
    checked_inventory: dict[str, dict[str, Any]] = {}
    previous = ""
    for index, value in enumerate(inventory):
        item = _exact(
            value,
            frozenset({"path", "sha256", "bytes"}),
            f"runtime inventory {index}",
        )
        path = _relative_path(item["path"], f"runtime inventory {index} path")
        if path <= previous:
            raise ProductEvidenceError(
                "runtime inventory must be sorted and duplicate-free"
            )
        previous = path
        checked_inventory[path] = {
            "sha256": _digest(item["sha256"], f"runtime inventory {path} digest"),
            "bytes": _integer(
                item["bytes"],
                f"runtime inventory {path} bytes",
                positive=not path.startswith("toolchain/"),
            ),
        }
    total_bytes = sum(item["bytes"] for item in checked_inventory.values())
    if total_bytes > MAX_RUNTIME_BYTES:
        raise ProductEvidenceError("runtime inventory exceeds its byte bound")

    def exact_file(path: str, digest: str, size: int | None, field: str) -> None:
        item = checked_inventory.get(path)
        if item is None or item["sha256"] != digest:
            raise ProductEvidenceError(f"runtime inventory lacks exact {field}")
        if size is not None and item["bytes"] != size:
            raise ProductEvidenceError(f"runtime inventory lacks exact {field}")

    exact_file("kernel.wasm", kernel_sha, kernel_bytes, "kernel")
    exact_file("host/generated-abi.ts", generated_sha, None, "generated ABI")
    exact_file("host/worker-protocol.ts", protocol_sha, None, "worker protocol")
    exact_file(
        "browser/dist/service-worker.js",
        service_worker_sha,
        None,
        "service worker",
    )
    exact_file(harness_path, harness_sha, harness_bytes, "browser harness entry")
    exact_file(
        host_entry_path,
        host_entry_sha,
        host_entry_bytes,
        "browser host entry",
    )
    exact_file(kernel_asset_path, kernel_asset_sha, None, "browser kernel asset")

    def subset_identity(prefix: str, field: str) -> tuple[str, int]:
        selected = [
            {"bytes": item["bytes"], "path": path, "sha256": item["sha256"]}
            for path, item in checked_inventory.items()
            if path.startswith(prefix)
        ]
        if not selected:
            raise ProductEvidenceError(f"runtime inventory lacks {field} files")
        return _sha(canonical_bytes(selected)), sum(item["bytes"] for item in selected)

    actual_host = subset_identity("host/", "host bundle")
    actual_browser = subset_identity("browser/", "browser bundle")
    if actual_host != (host_sha, host_bytes):
        raise ProductEvidenceError("runtime host bundle identity differs from inventory")
    if actual_browser != (browser_sha, browser_bytes):
        raise ProductEvidenceError("runtime browser bundle identity differs from inventory")

    if expected_inputs is not None:
        if (
            source != expected_inputs["source"]
            or dict(target) != expected_inputs["target_abi"]
            or policy_sha != expected_inputs["build_environment"]["policy_sha256"]
        ):
            raise ProductEvidenceError(
                "runtime source, ABI, or build policy differs from resolved product inputs"
            )
    if runtime_files is not None and runtime_root is not None:
        raise ProductEvidenceError(
            "runtime validation must use either a file map or an artifact root"
        )
    checked_files: Mapping[str, Mapping[str, Any]] | None = None
    if runtime_root is not None:
        checked_files = _runtime_file_identities(runtime_root)
    if runtime_files is not None:
        checked_files = {
            path: {
                "bytes": len(file_body) if isinstance(file_body, bytes) else None,
                "sha256": (
                    hashlib.sha256(file_body).hexdigest()
                    if isinstance(file_body, bytes)
                    else None
                ),
            }
            for path, file_body in runtime_files.items()
        }
    if checked_files is not None:
        if set(checked_files) != set(checked_inventory):
            raise ProductEvidenceError(
                "runtime file handoff differs from the exact runtime inventory"
            )
        for path, expected in checked_inventory.items():
            actual = checked_files[path]
            if actual != expected:
                raise ProductEvidenceError(f"runtime file {path} differs from its inventory")
    return runtime


def _runtime_file_identities(root: Path) -> dict[str, dict[str, Any]]:
    """Hash one exact artifact tree without retaining its file bodies in memory."""

    try:
        supplied = Path(root)
        root_metadata = supplied.lstat()
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(
            root_metadata.st_mode
        ):
            raise ProductEvidenceError(
                "runtime artifact root must be a real directory"
            )
        exact_root = supplied.resolve(strict=True)
    except OSError as error:
        raise ProductEvidenceError(
            f"runtime artifact root is unavailable: {error}"
        ) from error

    identities: dict[str, dict[str, Any]] = {}
    entry_count = 0
    total_bytes = 0

    def visit(directory: Path, prefix: str, depth: int) -> None:
        nonlocal entry_count, total_bytes
        if depth > 128:
            raise ProductEvidenceError("runtime artifact directory depth exceeds 128")
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise ProductEvidenceError(
                f"cannot enumerate runtime artifact directory: {error}"
            ) from error
        if not children and prefix:
            raise ProductEvidenceError("runtime artifact contains an empty directory")
        for child in children:
            entry_count += 1
            if entry_count > MAX_RUNTIME_FILES * 2:
                raise ProductEvidenceError("runtime artifact entry count exceeds its bound")
            relative = child.name if not prefix else f"{prefix}/{child.name}"
            _relative_path(relative, "runtime artifact path")
            try:
                metadata = child.lstat()
            except OSError as error:
                raise ProductEvidenceError(
                    f"cannot inspect runtime artifact {relative}: {error}"
                ) from error
            if stat.S_ISLNK(metadata.st_mode):
                raise ProductEvidenceError(
                    f"runtime artifact {relative} is a symbolic link"
                )
            if stat.S_ISDIR(metadata.st_mode):
                visit(child, relative, depth + 1)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ProductEvidenceError(
                    f"runtime artifact {relative} is not one regular file"
                )
            total_bytes += metadata.st_size
            if total_bytes > MAX_RUNTIME_BYTES:
                raise ProductEvidenceError("runtime artifact exceeds its byte bound")
            digest = hashlib.sha256()
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(child, flags)
                with os.fdopen(descriptor, "rb", closefd=True) as stream:
                    opened = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_dev != metadata.st_dev
                        or opened.st_ino != metadata.st_ino
                        or opened.st_size != metadata.st_size
                    ):
                        raise ProductEvidenceError(
                            f"runtime artifact {relative} changed while opening"
                        )
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                    finished = os.fstat(stream.fileno())
                    if (
                        finished.st_size != opened.st_size
                        or finished.st_mtime_ns != opened.st_mtime_ns
                    ):
                        raise ProductEvidenceError(
                            f"runtime artifact {relative} changed while hashing"
                        )
            except OSError as error:
                raise ProductEvidenceError(
                    f"cannot hash runtime artifact {relative}: {error}"
                ) from error
            identities[relative] = {
                "sha256": digest.hexdigest(),
                "bytes": metadata.st_size,
            }

    visit(exact_root, "", 0)
    return identities


def runtime_evidence_identity(runtime_bundle_body: bytes) -> dict[str, Any]:
    runtime = _validate_runtime_bundle(runtime_bundle_body)
    return {
        "bundle_sha256": hashlib.sha256(runtime_bundle_body).hexdigest(),
        "source": _plain(runtime["source"]),
        "target_abi": _plain(runtime["target_abi"]),
        "kernel": _plain(runtime["kernel"]),
        "host_runtime": _plain(runtime["host"]),
        "browser": _plain(runtime["browser"]),
        "build_policy_sha256": runtime["build_policy_sha256"],
    }


def _context_absolute_path(value: Any, field: str) -> str:
    path = _text(value, field)
    if (
        not path.startswith("/")
        or "\\" in path
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise ProductEvidenceError(f"{field} is not a normalized absolute path")
    return path


def _validate_context_boot(value: Any) -> dict[str, Any]:
    boot = _exact(
        value,
        frozenset({"argv", "cwd", "env", "gid", "uid"}),
        "protected product boot",
    )
    argv = [
        _text(item, f"protected product boot argv {index}")
        for index, item in enumerate(
            _sequence(boot["argv"], "protected product boot argv")
        )
    ]
    if not argv or len(argv) > 256:
        raise ProductEvidenceError("protected product boot argv exceeds its bound")
    environment = boot["env"]
    if not isinstance(environment, Mapping) or len(environment) > 256:
        raise ProductEvidenceError("protected product boot environment is invalid")
    checked_environment: dict[str, str] = {}
    for key, candidate in environment.items():
        if not isinstance(key, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ProductEvidenceError("protected product boot environment name is invalid")
        checked_environment[key] = _text(
            candidate, f"protected product boot environment {key}", 16 * 1024
        )
    return {
        "argv": argv,
        "cwd": _context_absolute_path(boot["cwd"], "protected product boot cwd"),
        "env": checked_environment,
        "gid": _integer(boot["gid"], "protected product boot gid"),
        "uid": _integer(boot["uid"], "protected product boot uid"),
    }


def _validate_context_mounts(value: Any) -> list[dict[str, Any]]:
    mounts = _sequence(value, "protected product mounts")
    if not mounts or len(mounts) > 32:
        raise ProductEvidenceError("protected product mounts exceed their bound")
    checked: list[dict[str, Any]] = []
    previous = ""
    for index, candidate in enumerate(mounts):
        if not isinstance(candidate, Mapping):
            raise ProductEvidenceError(f"protected product mount {index} is not an object")
        source = candidate.get("source")
        if source == "built-image":
            mount = _exact(
                candidate,
                frozenset({"path", "readonly", "source"}),
                f"protected product mount {index}",
            )
            if not isinstance(mount["readonly"], bool):
                raise ProductEvidenceError(
                    f"protected product mount {index} readonly flag is invalid"
                )
            result = {
                "path": _context_absolute_path(
                    mount["path"], f"protected product mount {index} path"
                ),
                "readonly": mount["readonly"],
                "source": "built-image",
            }
        elif source == "scratch":
            mount = _exact(
                candidate,
                frozenset({"ephemeral", "gid", "mode", "path", "source", "uid"}),
                f"protected product mount {index}",
            )
            mode = _text(mount["mode"], f"protected product mount {index} mode", 4)
            if re.fullmatch(r"[0-7]{3,4}", mode) is None or mount["ephemeral"] is not True:
                raise ProductEvidenceError(
                    f"protected product mount {index} scratch contract is invalid"
                )
            result = {
                "ephemeral": True,
                "gid": _integer(mount["gid"], f"protected product mount {index} gid"),
                "mode": mode,
                "path": _context_absolute_path(
                    mount["path"], f"protected product mount {index} path"
                ),
                "source": "scratch",
                "uid": _integer(mount["uid"], f"protected product mount {index} uid"),
            }
        else:
            raise ProductEvidenceError(
                f"protected product mount {index} source is unsupported"
            )
        if result["path"] <= previous:
            raise ProductEvidenceError(
                "protected product mounts must be sorted and duplicate-free"
            )
        previous = result["path"]
        checked.append(result)
    if checked[0].get("source") != "built-image" or checked[0]["path"] != "/":
        raise ProductEvidenceError("protected product mounts lack the built image root")
    return checked


def _validate_context_definition(value: Any, field: str) -> dict[str, Any]:
    definition = _exact(
        value,
        frozenset(
            {
                "definition_sha256",
                "host",
                "id",
                "implementation",
                "probe",
                "runner",
                "timeout_seconds",
            }
        ),
        field,
    )
    definition_id = _stable_id(definition["id"], f"{field} ID")
    if definition["host"] not in {"node", "browser"}:
        raise ProductEvidenceError(f"{field} host is unsupported")
    if definition["runner"] not in {
        "compile",
        "exec",
        "http",
        "interactive-terminal",
        "repository-suite",
        "service-protocol",
        "sql",
    }:
        raise ProductEvidenceError(f"{field} runner is unsupported")
    timeout = _integer(definition["timeout_seconds"], f"{field} timeout", positive=True)
    if timeout > 3 * 60 * 60:
        raise ProductEvidenceError(f"{field} timeout exceeds three hours")
    if not isinstance(definition["probe"], Mapping):
        raise ProductEvidenceError(f"{field} probe is not an object")
    implementations = _sequence(definition["implementation"], f"{field} implementation")
    if not implementations:
        raise ProductEvidenceError(f"{field} implementation is empty")
    checked_implementations = []
    previous = ""
    for index, candidate in enumerate(implementations):
        item = _exact(
            candidate,
            frozenset({"path", "sha256"}),
            f"{field} implementation {index}",
        )
        path = _relative_path(item["path"], f"{field} implementation {index} path")
        if path <= previous:
            raise ProductEvidenceError(
                f"{field} implementations must be sorted and duplicate-free"
            )
        previous = path
        checked_implementations.append(
            {
                "path": path,
                "sha256": _digest(
                    item["sha256"], f"{field} implementation {index} digest"
                ),
            }
        )
    identity = {
        "host": definition["host"],
        "id": definition_id,
        "implementation": checked_implementations,
        "probe": _plain(definition["probe"]),
        "runner": definition["runner"],
        "timeout_seconds": timeout,
    }
    if _digest(definition["definition_sha256"], f"{field} digest") != canonical_sha256(
        identity
    ):
        raise ProductEvidenceError(f"{field} digest differs from its protected identity")
    return {**identity, "definition_sha256": definition["definition_sha256"]}


def build_product_evidence_context(
    *,
    request: Mapping[str, Any],
    request_digest: str,
    catalog: Mapping[str, Any],
    definitions: Mapping[str, Any],
    candidate_product: CandidateProductLocatorV1,
    runtime_bundle_body: bytes,
    host: str,
    definition_id: str,
    run: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive one host context exclusively from protected selected authority."""

    if not isinstance(request, Mapping):
        raise ProductEvidenceError("product evidence request must be an object")
    checked_request = _plain(request)
    checked_request_digest = _digest(request_digest, "product evidence request digest")
    try:
        actual_request_digest = canonical_sha256(checked_request)
    except CanonicalJsonError as error:
        raise ProductEvidenceError(f"product evidence request is invalid: {error}") from error
    if actual_request_digest != checked_request_digest:
        raise ProductEvidenceError("product evidence request digest differs from its bytes")
    if host not in {"node", "browser"}:
        raise ProductEvidenceError("product evidence host is unsupported")
    selected_definition_id = _stable_id(
        definition_id, "product evidence definition ID"
    )
    if not isinstance(candidate_product, CandidateProductLocatorV1):
        raise ProductEvidenceError("product evidence candidate locator is invalid")

    requirements = checked_request.get("requirements")
    if not isinstance(requirements, Mapping):
        raise ProductEvidenceError("product evidence request requirements are missing")
    requested_products: dict[str, dict[str, str]] = {}
    previous = ""
    for index, candidate in enumerate(
        _sequence(requirements.get("products"), "product evidence request products")
    ):
        item = _exact(
            candidate,
            frozenset({"id", "manifest_sha256", "path"}),
            f"product evidence request product {index}",
        )
        product_id = _stable_id(item["id"], f"product evidence request product {index} ID")
        if product_id <= previous:
            raise ProductEvidenceError(
                "product evidence request products must be sorted and duplicate-free"
            )
        previous = product_id
        path = _relative_path(item["path"], f"product evidence request product {product_id} path")
        if path != f"images/vfs/products/{product_id}.toml":
            raise ProductEvidenceError(
                "product evidence request names a noncanonical product manifest"
            )
        requested_products[product_id] = {
            "id": product_id,
            "path": path,
            "manifest_sha256": _digest(
                item["manifest_sha256"],
                f"product evidence request product {product_id} manifest",
            ),
        }
    requested_product = requested_products.get(candidate_product.product_id)
    if requested_product is None:
        raise ProductEvidenceError(
            "candidate product is not selected by the exact product evidence request"
        )

    selected_evidence = []
    for index, candidate in enumerate(
        _sequence(requirements.get("evidence"), "product evidence request bindings")
    ):
        binding = _exact(
            candidate,
            frozenset({"applicability", "browser", "node", "product_id"}),
            f"product evidence request binding {index}",
        )
        product_id = _stable_id(
            binding["product_id"], f"product evidence request binding {index} product"
        )
        if product_id != candidate_product.product_id:
            continue
        if binding["applicability"] not in {"required", "informational"}:
            raise ProductEvidenceError("product evidence request applicability is unsupported")
        selected_ids = [
            _stable_id(value, f"product evidence request {host} definition")
            for value in _sequence(
                binding[host], f"product evidence request {host} definitions"
            )
        ]
        if selected_ids != sorted(set(selected_ids)):
            raise ProductEvidenceError(
                f"product evidence request {host} definitions are not sorted and unique"
            )
        selected_evidence.append(selected_ids)
    if len(selected_evidence) != 1 or selected_definition_id not in selected_evidence[0]:
        raise ProductEvidenceError(
            "evidence definition is not selected for the exact product and host"
        )

    checked_catalog = _exact(
        catalog,
        frozenset({"kind", "products", "schema"}),
        "protected VFS product catalog",
    )
    if (
        checked_catalog["schema"] != 1
        or checked_catalog["kind"] != "kandelo-vfs-product-catalog"
    ):
        raise ProductEvidenceError("protected VFS product catalog identity is unsupported")
    selected_catalog_entry = None
    previous = ""
    for index, candidate in enumerate(
        _sequence(checked_catalog["products"], "protected VFS product catalog products")
    ):
        entry = _exact(
            candidate,
            frozenset({"manifest", "path", "sha256"}),
            f"protected VFS product catalog entry {index}",
        )
        if not isinstance(entry["manifest"], Mapping):
            raise ProductEvidenceError(
                f"protected VFS product catalog entry {index} manifest is invalid"
            )
        manifest = _plain(entry["manifest"])
        product_id = _stable_id(
            manifest.get("id"), f"protected VFS product catalog entry {index} ID"
        )
        if product_id <= previous:
            raise ProductEvidenceError(
                "protected VFS product catalog must be sorted and duplicate-free"
            )
        previous = product_id
        path = _relative_path(entry["path"], f"protected VFS product {product_id} path")
        digest = _digest(entry["sha256"], f"protected VFS product {product_id} manifest")
        if (
            path != f"images/vfs/products/{product_id}.toml"
            or digest != canonical_sha256(manifest)
        ):
            raise ProductEvidenceError(
                f"protected VFS product {product_id} catalog identity is invalid"
            )
        if product_id == candidate_product.product_id:
            selected_catalog_entry = (manifest, path, digest)
    if selected_catalog_entry is None:
        raise ProductEvidenceError("candidate product is absent from the protected catalog")
    manifest, manifest_path, manifest_sha256 = selected_catalog_entry
    if (
        manifest_path != requested_product["path"]
        or manifest_sha256 != requested_product["manifest_sha256"]
    ):
        raise ProductEvidenceError(
            "protected product manifest differs from the exact request selection"
        )
    evidence = manifest.get("evidence")
    if not isinstance(evidence, Mapping) or host not in evidence:
        raise ProductEvidenceError("protected product does not declare evidence for this host")
    declared = _exact(
        evidence[host], frozenset({"test"}), f"protected product {host} evidence"
    )
    _stable_id(declared["test"], f"protected product {host} basic evidence")
    boot = _validate_context_boot(manifest.get("boot"))
    mounts = _validate_context_mounts(manifest.get("mounts"))

    checked_definitions = _exact(
        definitions,
        frozenset({"definitions", "kind", "schema", "version"}),
        "protected evidence definition registry",
    )
    if (
        checked_definitions["schema"] != 1
        or checked_definitions["kind"] != "kandelo-vfs-evidence-definitions"
        or _integer(
            checked_definitions["version"],
            "protected evidence definition registry version",
            positive=True,
        )
        < 1
    ):
        raise ProductEvidenceError("protected evidence definition registry is unsupported")
    selected_definition = None
    previous = ""
    for index, candidate in enumerate(
        _sequence(
            checked_definitions["definitions"],
            "protected evidence definitions",
        )
    ):
        definition = _validate_context_definition(
            candidate, f"protected evidence definition {index}"
        )
        if definition["id"] <= previous:
            raise ProductEvidenceError(
                "protected evidence definitions must be sorted and duplicate-free"
            )
        previous = definition["id"]
        if definition["id"] == selected_definition_id:
            selected_definition = definition
    if selected_definition is None or selected_definition["host"] != host:
        raise ProductEvidenceError(
            "selected evidence definition differs from protected current policy"
        )

    runtime = _validate_runtime_bundle(runtime_bundle_body)
    request_source = _validate_source(
        checked_request.get("build_source"), "product evidence request source"
    )
    request_target = _exact(
        checked_request.get("target_abi"),
        frozenset({"snapshot_sha256", "version"}),
        "product evidence request ABI",
    )
    checked_target = {
        "snapshot_sha256": _digest(
            request_target["snapshot_sha256"], "product evidence request ABI snapshot"
        ),
        "version": _integer(
            request_target["version"], "product evidence request ABI version"
        ),
    }
    issuance = checked_request.get("issuance")
    if not isinstance(issuance, Mapping):
        raise ProductEvidenceError("product evidence request issuance is missing")
    request_policy = _digest(
        issuance.get("policy_sha256"), "product evidence request policy"
    )
    if (
        runtime["source"] != request_source
        or runtime["target_abi"] != checked_target
        or runtime["build_policy_sha256"] != request_policy
    ):
        raise ProductEvidenceError(
            "exact runtime differs from the request source, ABI, or policy"
        )
    repository_match = PRODUCT_REPOSITORY.fullmatch(
        candidate_product.repository[len("ghcr.io/") :]
    )
    if (
        repository_match is None
        or repository_match.group("product") != candidate_product.product_id
        or int(repository_match.group("abi")) != checked_target["version"]
    ):
        raise ProductEvidenceError(
            "candidate product repository differs from the exact request ABI"
        )

    return {
        "schema": 1,
        "kind": f"kandelo-vfs-product-{host}-evidence-context",
        "request_digest": checked_request_digest,
        "product": {
            "id": candidate_product.product_id,
            "manifest_sha256": manifest_sha256,
        },
        "candidate_product": candidate_product.evidence_identity(),
        "runtime": runtime_evidence_identity(runtime_bundle_body),
        "host": host,
        "definition": selected_definition,
        "boot": boot,
        "mounts": mounts,
        "run": _validate_run(run, "product evidence run", evidence=True),
    }


def _validate_builder_report_shape(body: bytes) -> dict[str, Any]:
    report = _load_canonical(body, "VFS builder report")
    _exact(
        report,
        frozenset(
            {
                "schema",
                "kind",
                "capture",
                "inputs",
                "output",
                "product",
                "resolved_inputs_sha256",
            }
        ),
        "VFS builder report",
    )
    if report["schema"] != 1 or report["kind"] != "kandelo-vfs-builder-report":
        raise ProductEvidenceError("VFS builder report protocol is unsupported")
    capture = _exact(
        report["capture"],
        frozenset({"complete", "unreported_reads"}),
        "VFS builder report capture",
    )
    if capture != {"complete": True, "unreported_reads": []}:
        raise ProductEvidenceError("VFS builder report capture is incomplete")
    _validate_product(report["product"], "VFS builder report product", full=True)
    _digest(report["resolved_inputs_sha256"], "VFS builder report inputs")
    output = _exact(
        report["output"],
        frozenset({"abi", "bytes", "name", "path", "sha256"}),
        "VFS builder report output",
    )
    abi = _exact(
        output["abi"], frozenset({"version", "snapshot_sha256"}), "VFS output ABI"
    )
    _integer(abi["version"], "VFS output ABI version")
    _digest(abi["snapshot_sha256"], "VFS output ABI snapshot")
    _integer(output["bytes"], "VFS output bytes", positive=True)
    _text(output["name"], "VFS output name", 255)
    _relative_path(output["path"], "VFS output path")
    _digest(output["sha256"], "VFS output digest")
    inputs = _sequence(report["inputs"], "VFS builder report inputs")
    previous = ""
    for index, item_value in enumerate(inputs):
        item = item_value if isinstance(item_value, Mapping) else {}
        required = {"bytes", "id", "kind", "placement", "role", "sha256"}
        permitted = required | {"descriptor"}
        if not required.issubset(item) or not set(item).issubset(permitted):
            raise ProductEvidenceError(f"VFS builder report input {index} fields changed")
        identity = _stable_id(item["id"], f"VFS builder report input {index} ID")
        if identity <= previous:
            raise ProductEvidenceError("VFS builder report inputs are not sorted and unique")
        previous = identity
        _integer(item["bytes"], f"VFS builder report input {identity} bytes")
        _digest(item["sha256"], f"VFS builder report input {identity} digest")
        if item["kind"] not in {
            "product-image",
            "homebrew-bottle",
            "package-output",
            "source-archive",
            "toolchain-output",
            "repository-path",
        }:
            raise ProductEvidenceError("VFS builder report input kind is unsupported")
        if item["placement"] not in {"embedded", "lazy-reference", "build-only"}:
            raise ProductEvidenceError("VFS builder report input placement is unsupported")
        if item["role"] not in {"runtime", "build"}:
            raise ProductEvidenceError("VFS builder report input role is unsupported")
        if "descriptor" in item:
            descriptor = _exact(
                item["descriptor"],
                frozenset({"sha256", "bytes"}),
                f"VFS builder report input {identity} descriptor",
            )
            _digest(descriptor["sha256"], "VFS builder report descriptor digest")
            _integer(
                descriptor["bytes"], "VFS builder report descriptor bytes", positive=True
            )
    return report


def validate_candidate_builder_report(body: bytes) -> None:
    _validate_builder_report_shape(body)


def _expected_report_inputs(resolved: Mapping[str, Any]) -> list[dict[str, Any]]:
    expected = []
    for item in resolved["inputs"]:
        value = {
            "bytes": item["bytes"],
            "id": item["id"],
            "kind": item["kind"],
            "placement": item["effective_materialization"],
            "role": item["role"],
            "sha256": item["sha256"],
        }
        if "descriptor" in item:
            value["descriptor"] = {
                "bytes": item["descriptor"]["bytes"],
                "sha256": item["descriptor"]["sha256"],
            }
        expected.append(value)
    return expected


def _validate_builder_report(
    body: bytes, *, resolved: Mapping[str, Any], resolved_body: bytes, vfs_body: bytes
) -> dict[str, Any]:
    report = _validate_builder_report_shape(body)
    output = report["output"]
    if (
        report["product"] != resolved["product"]
        or report["resolved_inputs_sha256"] != hashlib.sha256(resolved_body).hexdigest()
        or report["inputs"] != _expected_report_inputs(resolved)
        or output["abi"] != resolved["target_abi"]
        or output["name"] != resolved["product"]["output"]
        or output["path"] != resolved["product"]["output"]
        or output["bytes"] != len(vfs_body)
        or output["sha256"] != hashlib.sha256(vfs_body).hexdigest()
    ):
        raise ProductEvidenceError(
            "VFS builder report differs from exact inputs, product, ABI, or output"
        )
    return report


def _load_resolved(body: bytes) -> Mapping[str, Any]:
    try:
        value = load_resolved_product_inputs(body)
    except ProductInputResolutionError as error:
        raise ProductEvidenceError(f"resolved product inputs are invalid: {error}") from error
    if value["reference_class"] != "candidate":
        raise ProductEvidenceError("candidate product inputs are not candidate references")
    return value


def _validate_protected_input_plan(
    plan: ProductInputPlanV1,
    *,
    resolved: Mapping[str, Any],
    resolved_body: bytes,
    runtime_bundle_body: bytes,
) -> None:
    if not isinstance(plan, ProductInputPlanV1):
        raise ProductEvidenceError("candidate publication lacks a protected product input plan")
    product = resolved["product"]
    expected_product = (
        product["id"],
        product["manifest_path"],
        product["manifest_sha256"],
        product["architecture"],
        resolved["reference_class"],
    )
    actual_product = (
        plan.product_id,
        plan.manifest_path,
        plan.manifest_sha256,
        plan.architecture,
        plan.reference_class,
    )
    if actual_product != expected_product:
        raise ProductEvidenceError(
            "candidate publication product differs from its protected input plan"
        )
    if plan.resolved_inputs_sha256 != _sha(resolved_body):
        raise ProductEvidenceError(
            "candidate publication resolved inputs differ from their protected input plan"
        )
    if plan.runtime_bundle_sha256 != _sha(runtime_bundle_body):
        raise ProductEvidenceError(
            "candidate publication runtime bundle differs from its protected input plan"
        )
    for label, values in (
        ("dependency products", plan.dependency_product_ids),
        ("Formula subjects", plan.required_formula_subjects),
    ):
        if not isinstance(values, tuple) or values != tuple(sorted(set(values))):
            raise ProductEvidenceError(
                f"protected product input plan {label} are not sorted and unique"
            )
    for value in plan.dependency_product_ids:
        _stable_id(value, "protected product input plan dependency products")
    for value in plan.required_formula_subjects:
        try:
            parse_formula_subject(value, "protected product input plan Formula subject")
        except PlanError as error:
            raise ProductEvidenceError(str(error)) from error


def build_candidate_product_oci_plan(
    *,
    repository: str,
    publisher_repository: str,
    input_plan: ProductInputPlanV1,
    vfs_body: bytes,
    builder_report_body: bytes,
    resolved_inputs_body: bytes,
    runtime_bundle_body: bytes,
    runtime_files: Mapping[str, bytes] | None = None,
    runtime_root: Path | None = None,
    lazy_input_bodies: Mapping[str, bytes] | None = None,
) -> OciRecordPlanV1:
    """Validate an inert composition handoff before creating its OCI plan."""

    if not isinstance(vfs_body, bytes) or not vfs_body:
        raise ProductEvidenceError("candidate VFS image must contain bytes")
    checked_publisher = _text(
        publisher_repository, "candidate product publisher repository", 255
    )
    if re.fullmatch(
        r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", checked_publisher
    ) is None:
        raise ProductEvidenceError(
            "candidate product publisher repository is not owner/name"
        )
    resolved = _load_resolved(resolved_inputs_body)
    _validate_protected_input_plan(
        input_plan,
        resolved=resolved,
        resolved_body=resolved_inputs_body,
        runtime_bundle_body=runtime_bundle_body,
    )
    product = resolved["product"]
    _validate_product_repository(repository, product["id"], resolved["target_abi"]["version"])
    report = _validate_builder_report(
        builder_report_body,
        resolved=resolved,
        resolved_body=resolved_inputs_body,
        vfs_body=vfs_body,
    )
    _validate_runtime_bundle(
        runtime_bundle_body,
        expected_inputs=resolved,
        runtime_files=runtime_files,
        runtime_root=runtime_root,
    )
    if lazy_input_bodies is None:
        lazy_input_bodies = {}
    if not isinstance(lazy_input_bodies, Mapping):
        raise ProductEvidenceError("candidate lazy input bodies must be an object")
    expected_lazy = [
        item
        for item in resolved["inputs"]
        if item["effective_materialization"] == "lazy-reference"
        and item["kind"]
        in {
            "package-output",
            "source-archive",
            "toolchain-output",
            "repository-path",
        }
    ]
    expected_lazy.sort(key=lambda item: item["id"])
    if set(lazy_input_bodies) != {item["id"] for item in expected_lazy}:
        raise ProductEvidenceError(
            "candidate lazy input body closure differs from resolved product inputs"
        )
    lazy_artifacts = []
    lazy_layers = []
    for index, item in enumerate(expected_lazy):
        body = lazy_input_bodies[item["id"]]
        if (
            not isinstance(body, bytes)
            or not body
            or len(body) != item["bytes"]
            or _sha(body) != item["sha256"]
        ):
            raise ProductEvidenceError(
                f"candidate lazy input {item['id']} differs from its resolved identity"
            )
        expected_reference = (
            f"ghcr.io/{repository}@sha256:{item['sha256']}"
        )
        if item["reference"] != expected_reference:
            raise ProductEvidenceError(
                f"candidate lazy input {item['id']} enters another repository"
            )
        role = f"lazy-input-{index:04d}"
        lazy_artifacts.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
                "immutable_reference": expected_reference,
            }
        )
        lazy_layers.append(
            OciBlobV1(
                role=role,
                media_type=LAZY_INPUT_MEDIA_TYPE,
                body=body,
                title=f"lazy-input-{item['id']}",
            )
        )
    record = {
        "schema": 1,
        "kind": "kandelo-vfs-candidate-product",
        "product": _plain(product),
        "target_abi": _plain(resolved["target_abi"]),
        "source": _plain(resolved["source"]),
        "reference_class": "candidate",
        "artifacts": {
            "vfs_image": _artifact_for_blob(repository, _sha(vfs_body), len(vfs_body)),
            "builder_report": _artifact_for_blob(
                repository, _sha(builder_report_body), len(builder_report_body)
            ),
            "resolved_inputs": _artifact_for_blob(
                repository, _sha(resolved_inputs_body), len(resolved_inputs_body)
            ),
            "runtime_bundle": _artifact_for_blob(
                repository, _sha(runtime_bundle_body), len(runtime_bundle_body)
            ),
            "lazy_inputs": lazy_artifacts,
        },
        "nonendorsed": True,
    }
    return OciRecordPlanV1(
        repository=repository,
        artifact_type=PRODUCT_CANDIDATE_MEDIA_TYPE,
        config=OciBlobV1(
            role="candidate-product-record",
            media_type=PRODUCT_CANDIDATE_MEDIA_TYPE,
            body=canonical_bytes(record),
            title="candidate-product-record.json",
        ),
        layers=(
            OciBlobV1(
                role="vfs-image",
                media_type=VFS_IMAGE_MEDIA_TYPE,
                body=vfs_body,
                title=product["output"],
            ),
            OciBlobV1(
                role="builder-report",
                media_type=BUILDER_REPORT_MEDIA_TYPE,
                body=builder_report_body,
                title="builder-report.json",
            ),
            OciBlobV1(
                role="resolved-inputs",
                media_type=RESOLVED_INPUTS_MEDIA_TYPE,
                body=resolved_inputs_body,
                title="resolved-inputs.json",
            ),
            OciBlobV1(
                role="runtime-bundle",
                media_type=RUNTIME_BUNDLE_MEDIA_TYPE,
                body=runtime_bundle_body,
                title="runtime-bundle.json",
            ),
            *lazy_layers,
        ),
        annotations={
            "dev.kandelo.abi-staging.architecture": product["architecture"],
            "dev.kandelo.abi-staging.classification": "public-candidate-not-endorsed",
            "dev.kandelo.abi-staging.kind": "candidate-product",
            "dev.kandelo.abi-staging.nonendorsed": "true",
            "dev.kandelo.abi-staging.product": product["id"],
            "dev.kandelo.abi-staging.target-abi": str(resolved["target_abi"]["version"]),
            "org.opencontainers.image.source": "https://github.com/"
            + checked_publisher,
        },
    )


def _validate_candidate_record_body(
    body: bytes, *, repository: str
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    int,
    dict[str, str],
    Mapping[str, Any],
    list[tuple[str, dict[str, Any]]],
]:
    """Validate candidate config bytes independently from large OCI layers."""

    record = _load_canonical(body, "candidate product record")
    _exact(
        record,
        frozenset(
            {
                "schema",
                "kind",
                "product",
                "target_abi",
                "source",
                "reference_class",
                "artifacts",
                "nonendorsed",
            }
        ),
        "candidate product record",
    )
    if (
        record["schema"] != 1
        or record["kind"] != "kandelo-vfs-candidate-product"
        or record["reference_class"] != "candidate"
        or record["nonendorsed"] is not True
    ):
        raise ProductEvidenceError("candidate product record state is contradictory")
    product = _validate_product(record["product"], "candidate product", full=True)
    target = _exact(
        record["target_abi"],
        frozenset({"version", "snapshot_sha256"}),
        "candidate product ABI",
    )
    target_abi = _integer(target["version"], "candidate product ABI version")
    if target_abi > 2**32 - 1:
        raise ProductEvidenceError("candidate product ABI exceeds its unsigned 32-bit bound")
    _digest(target["snapshot_sha256"], "candidate product ABI snapshot")
    source = _validate_source(record["source"], "candidate product source")
    _validate_product_repository(repository, product["id"], target_abi)
    artifacts = _exact(
        record["artifacts"],
        frozenset(
            {
                "vfs_image",
                "builder_report",
                "resolved_inputs",
                "runtime_bundle",
                "lazy_inputs",
            }
        ),
        "candidate product artifacts",
    )
    for key in ("vfs_image", "builder_report", "resolved_inputs", "runtime_bundle"):
        artifact = _artifact(artifacts[key], f"candidate product {key}")
        if artifact["immutable_reference"] != (
            f"ghcr.io/{repository}@sha256:{artifact['sha256']}"
        ):
            raise ProductEvidenceError(
                f"candidate product {key} reference differs from its repository"
            )
    lazy_inputs = _sequence(
        artifacts["lazy_inputs"], "candidate product lazy inputs"
    )
    checked_lazy_inputs = []
    previous_id = ""
    for index, value in enumerate(lazy_inputs):
        item = _exact(
            value,
            frozenset(
                {"id", "kind", "sha256", "bytes", "immutable_reference"}
            ),
            f"candidate product lazy input {index}",
        )
        input_id = _stable_id(
            item["id"], f"candidate product lazy input {index} ID"
        )
        if input_id <= previous_id:
            raise ProductEvidenceError(
                "candidate product lazy inputs are not sorted and unique"
            )
        previous_id = input_id
        if item["kind"] not in {
            "package-output",
            "source-archive",
            "toolchain-output",
            "repository-path",
        }:
            raise ProductEvidenceError("candidate product lazy input kind is unsupported")
        artifact = _artifact(
            {
                "sha256": item["sha256"],
                "bytes": item["bytes"],
                "immutable_reference": item["immutable_reference"],
            },
            f"candidate product lazy input {input_id}",
        )
        expected_reference = (
            f"ghcr.io/{repository}@sha256:{artifact['sha256']}"
        )
        if artifact["immutable_reference"] != expected_reference:
            raise ProductEvidenceError(
                "candidate product lazy input reference differs from its repository"
            )
        checked_lazy_inputs.append((input_id, artifact))
    return record, product, target_abi, source, artifacts, checked_lazy_inputs


def _candidate_record(plan: OciRecordPlanV1) -> dict[str, Any]:
    if plan.artifact_type != PRODUCT_CANDIDATE_MEDIA_TYPE:
        raise ProductEvidenceError("OCI plan is not a candidate product")
    _require_blob_descriptor(
        plan.config,
        role="candidate-product-record",
        media_type=PRODUCT_CANDIDATE_MEDIA_TYPE,
        title="candidate-product-record.json",
        field="candidate product config",
    )
    (
        record,
        product,
        _target_abi,
        _source,
        artifacts,
        checked_lazy_inputs,
    ) = _validate_candidate_record_body(
        plan.config.body,
        repository=plan.repository,
    )
    expected_roles = [
        "vfs-image",
        "builder-report",
        "resolved-inputs",
        "runtime-bundle",
        *(f"lazy-input-{index:04d}" for index in range(len(checked_lazy_inputs))),
    ]
    if [layer.role for layer in plan.layers] != expected_roles:
        raise ProductEvidenceError("candidate product OCI descriptor roles changed")
    fixed = (
        ("vfs_image", plan.layers[0], "vfs-image", VFS_IMAGE_MEDIA_TYPE, product["output"]),
        (
            "builder_report",
            plan.layers[1],
            "builder-report",
            BUILDER_REPORT_MEDIA_TYPE,
            "builder-report.json",
        ),
        (
            "resolved_inputs",
            plan.layers[2],
            "resolved-inputs",
            RESOLVED_INPUTS_MEDIA_TYPE,
            "resolved-inputs.json",
        ),
        (
            "runtime_bundle",
            plan.layers[3],
            "runtime-bundle",
            RUNTIME_BUNDLE_MEDIA_TYPE,
            "runtime-bundle.json",
        ),
    )
    for key, layer, role, media_type, title in fixed:
        _require_blob_descriptor(
            layer,
            role=role,
            media_type=media_type,
            title=title,
            field=f"candidate product {key}",
        )
        artifact = _artifact(artifacts[key], f"candidate product {key}")
        if (
            layer.digest != "sha256:" + artifact["sha256"]
            or layer.size != artifact["bytes"]
        ):
            raise ProductEvidenceError(
                f"candidate product {key} layer differs from its record"
            )
    for index, ((input_id, artifact), layer) in enumerate(
        zip(checked_lazy_inputs, plan.layers[4:], strict=True)
    ):
        _require_blob_descriptor(
            layer,
            role=f"lazy-input-{index:04d}",
            media_type=LAZY_INPUT_MEDIA_TYPE,
            title=f"lazy-input-{input_id}",
            field=f"candidate product lazy input {input_id}",
        )
        if (
            layer.digest != "sha256:" + artifact["sha256"]
            or layer.size != artifact["bytes"]
        ):
            raise ProductEvidenceError(
                f"candidate product lazy input {input_id} layer differs from its record"
            )
    return record


def publish_candidate_product(
    plan: OciRecordPlanV1,
    *,
    transport: OciTransportV1,
    expected_source_repository: str,
) -> CandidateProductLocatorV1:
    """Publish identity first; verification never changes this locator."""

    record = _candidate_record(plan)
    expected_annotations = {
        "dev.kandelo.abi-staging.architecture": record["product"]["architecture"],
        "dev.kandelo.abi-staging.classification": "public-candidate-not-endorsed",
        "dev.kandelo.abi-staging.kind": "candidate-product",
        "dev.kandelo.abi-staging.nonendorsed": "true",
        "dev.kandelo.abi-staging.product": record["product"]["id"],
        "dev.kandelo.abi-staging.target-abi": str(record["target_abi"]["version"]),
        "org.opencontainers.image.source": "https://github.com/"
        + expected_source_repository,
    }
    if dict(plan.annotations) != expected_annotations:
        raise ProductEvidenceError(
            "candidate product OCI annotations differ from protected publisher policy"
        )
    published = publish_record(
        plan,
        transport=transport,
        expected_source_repository=expected_source_repository,
    )
    artifacts = record["artifacts"]
    return CandidateProductLocatorV1(
        product_id=record["product"]["id"],
        repository=published.repository,
        manifest_digest=published.digest,
        immutable_reference=published.immutable_reference,
        vfs_layer_sha256=artifacts["vfs_image"]["sha256"],
        vfs_layer_bytes=artifacts["vfs_image"]["bytes"],
        builder_report_sha256=artifacts["builder_report"]["sha256"],
    )


def _candidate_manifest_descriptor(value: Any, field: str) -> dict[str, Any]:
    descriptor = _exact(
        value,
        frozenset({"mediaType", "digest", "size", "annotations"}),
        field,
    )
    annotations = _exact(
        descriptor["annotations"],
        frozenset(
            {
                "dev.kandelo.abi-staging.role",
                "org.opencontainers.image.title",
            }
        ),
        f"{field} annotations",
    )
    return {
        "media_type": _text(descriptor["mediaType"], f"{field} media type", 255),
        "digest": _oci_digest(descriptor["digest"], f"{field} digest"),
        "size": _integer(descriptor["size"], f"{field} bytes", positive=True),
        "role": _text(
            annotations["dev.kandelo.abi-staging.role"], f"{field} role", 255
        ),
        "title": _text(
            annotations["org.opencontainers.image.title"], f"{field} title", 255
        ),
    }


def _require_public_candidate_descriptor(
    descriptor: Mapping[str, Any],
    *,
    artifact: Mapping[str, Any],
    role: str,
    media_type: str,
    title: str,
    field: str,
) -> None:
    if (
        descriptor["role"] != role
        or descriptor["media_type"] != media_type
        or descriptor["title"] != title
        or descriptor["digest"] != "sha256:" + artifact["sha256"]
        or descriptor["size"] != artifact["bytes"]
    ):
        raise ProductEvidenceError(f"{field} OCI descriptor metadata changed")


def inspect_candidate_product_repository(
    repository: str,
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    expected_source_repository: str,
    transport: OciTransportV1,
) -> tuple[CandidateProductInventoryEntryV1, ...]:
    """Recover current-request product facts without downloading VFS layers."""

    checked_repository = _repository(repository, "candidate product inventory repository")
    match = PRODUCT_REPOSITORY.fullmatch(checked_repository)
    if match is None:
        raise ProductEvidenceError(
            "candidate product inventory is outside the reserved repository"
        )
    checked_request = _plain(request)
    checked_request_sha256 = _digest(
        request_sha256, "candidate product inventory request"
    )
    if canonical_sha256(checked_request) != checked_request_sha256:
        raise ProductEvidenceError(
            "candidate product inventory request digest is not canonical"
        )
    source = _validate_source(
        checked_request.get("build_source"), "candidate product inventory source"
    )
    target = _exact(
        checked_request.get("target_abi"),
        frozenset({"version", "snapshot_sha256"}),
        "candidate product inventory ABI",
    )
    target_abi = _integer(target["version"], "candidate product inventory ABI version")
    snapshot_sha256 = _digest(
        target["snapshot_sha256"], "candidate product inventory ABI snapshot"
    )
    if target_abi != int(match.group("abi")):
        raise ProductEvidenceError(
            "candidate product inventory repository has the wrong ABI"
        )
    requirements = checked_request.get("requirements")
    if not isinstance(requirements, Mapping):
        raise ProductEvidenceError(
            "candidate product inventory request lacks requirements"
        )
    selected = []
    for index, value in enumerate(
        _sequence(requirements.get("products"), "candidate product requirements")
    ):
        product = _exact(
            value,
            frozenset({"id", "path", "manifest_sha256"}),
            f"candidate product requirement {index}",
        )
        product_id = _stable_id(
            product["id"], f"candidate product requirement {index} ID"
        )
        if product_id == match.group("product"):
            selected.append(
                {
                    "id": product_id,
                    "path": _relative_path(
                        product["path"],
                        f"candidate product requirement {index} path",
                    ),
                    "manifest_sha256": _digest(
                        product["manifest_sha256"],
                        f"candidate product requirement {index} manifest",
                    ),
                }
            )
    if len(selected) != 1:
        raise ProductEvidenceError(
            "candidate product repository lacks one exact request selection"
        )
    selection = selected[0]
    checked_publisher = _text(
        expected_source_repository,
        "candidate product inventory publisher repository",
        255,
    )
    if re.fullmatch(
        r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", checked_publisher
    ) is None:
        raise ProductEvidenceError(
            "candidate product inventory publisher is not owner/name"
        )

    results = []
    try:
        locators = list_public_record_locators(
            checked_repository, transport=transport
        )
        for public_locator in locators:
            fetched = fetch_public_record(
                public_locator,
                transport=transport,
                expected_artifact_type=PRODUCT_CANDIDATE_MEDIA_TYPE,
                required_layer_roles=(),
            )
            manifest = _exact(
                _load_canonical(fetched.manifest, "candidate product OCI manifest"),
                frozenset(
                    {
                        "schemaVersion",
                        "mediaType",
                        "artifactType",
                        "config",
                        "layers",
                        "annotations",
                    }
                ),
                "candidate product OCI manifest",
            )
            if manifest["schemaVersion"] != 2:
                raise ProductEvidenceError(
                    "candidate product OCI manifest schema changed"
                )
            record, product, record_abi, record_source, artifacts, lazy_inputs = (
                _validate_candidate_record_body(
                    fetched.config.body,
                    repository=checked_repository,
                )
            )
            expected_annotations = {
                "dev.kandelo.abi-staging.architecture": product["architecture"],
                "dev.kandelo.abi-staging.classification": (
                    "public-candidate-not-endorsed"
                ),
                "dev.kandelo.abi-staging.kind": "candidate-product",
                "dev.kandelo.abi-staging.nonendorsed": "true",
                "dev.kandelo.abi-staging.product": product["id"],
                "dev.kandelo.abi-staging.target-abi": str(record_abi),
                "org.opencontainers.image.source": (
                    "https://github.com/" + checked_publisher
                ),
            }
            if dict(manifest["annotations"]) != expected_annotations:
                raise ProductEvidenceError(
                    "candidate product OCI annotations differ from protected policy"
                )
            config = _candidate_manifest_descriptor(
                manifest["config"], "candidate product config"
            )
            config_artifact = {
                "sha256": _sha(fetched.config.body),
                "bytes": len(fetched.config.body),
            }
            _require_public_candidate_descriptor(
                config,
                artifact=config_artifact,
                role="candidate-product-record",
                media_type=PRODUCT_CANDIDATE_MEDIA_TYPE,
                title="candidate-product-record.json",
                field="candidate product config",
            )
            raw_layers = _sequence(
                manifest["layers"], "candidate product OCI layers"
            )
            descriptors = [
                _candidate_manifest_descriptor(
                    value, f"candidate product OCI layer {index}"
                )
                for index, value in enumerate(raw_layers)
            ]
            fixed = (
                (
                    "vfs_image",
                    "vfs-image",
                    VFS_IMAGE_MEDIA_TYPE,
                    product["output"],
                ),
                (
                    "builder_report",
                    "builder-report",
                    BUILDER_REPORT_MEDIA_TYPE,
                    "builder-report.json",
                ),
                (
                    "resolved_inputs",
                    "resolved-inputs",
                    RESOLVED_INPUTS_MEDIA_TYPE,
                    "resolved-inputs.json",
                ),
                (
                    "runtime_bundle",
                    "runtime-bundle",
                    RUNTIME_BUNDLE_MEDIA_TYPE,
                    "runtime-bundle.json",
                ),
            )
            if len(descriptors) != len(fixed) + len(lazy_inputs):
                raise ProductEvidenceError(
                    "candidate product OCI descriptor count changed"
                )
            for descriptor, (key, role, media_type, title) in zip(
                descriptors, fixed, strict=False
            ):
                _require_public_candidate_descriptor(
                    descriptor,
                    artifact=_artifact(
                        artifacts[key], f"candidate product {key}"
                    ),
                    role=role,
                    media_type=media_type,
                    title=title,
                    field=f"candidate product {key}",
                )
            for index, ((input_id, artifact), descriptor) in enumerate(
                zip(lazy_inputs, descriptors[len(fixed) :], strict=True)
            ):
                _require_public_candidate_descriptor(
                    descriptor,
                    artifact=artifact,
                    role=f"lazy-input-{index:04d}",
                    media_type=LAZY_INPUT_MEDIA_TYPE,
                    title=f"lazy-input-{input_id}",
                    field=f"candidate product lazy input {input_id}",
                )

            if (
                record_source != source
                or record_abi != target_abi
                or record["target_abi"]["snapshot_sha256"] != snapshot_sha256
                or product["id"] != selection["id"]
                or product["manifest_path"] != selection["path"]
                or product["manifest_sha256"] != selection["manifest_sha256"]
            ):
                continue
            candidate_locator = CandidateProductLocatorV1(
                product_id=product["id"],
                repository=fetched.repository,
                manifest_digest=fetched.digest,
                immutable_reference=fetched.immutable_reference,
                vfs_layer_sha256=artifacts["vfs_image"]["sha256"],
                vfs_layer_bytes=artifacts["vfs_image"]["bytes"],
                builder_report_sha256=artifacts["builder_report"]["sha256"],
            )
            artifact = CandidateProductArtifactV1(
                product_id=product["id"],
                manifest_sha256=product["manifest_sha256"],
                architecture=product["architecture"],
                request_sha256=checked_request_sha256,
                source_repository=record_source["repository"],
                source_commit=record_source["commit"],
                source_tree=record_source["tree"],
                target_abi=record_abi,
                snapshot_sha256=record["target_abi"]["snapshot_sha256"],
                vfs_layer_sha256=artifacts["vfs_image"]["sha256"],
                vfs_layer_bytes=artifacts["vfs_image"]["bytes"],
                immutable_reference=(
                    f"ghcr.io/{checked_repository}@sha256:"
                    + artifacts["vfs_image"]["sha256"]
                ),
                builder_report_sha256=artifacts["builder_report"]["sha256"],
            )
            results.append(
                CandidateProductInventoryEntryV1(
                    artifact=artifact,
                    locator=candidate_locator,
                    runtime_bundle_sha256=artifacts["runtime_bundle"]["sha256"],
                )
            )
    except (OciPublicationError, ValueError) as error:
        if isinstance(error, ProductEvidenceError):
            raise
        raise ProductEvidenceError(
            f"candidate product inventory is invalid: {error}"
        ) from error
    results.sort(key=lambda value: value.locator.manifest_digest)
    return tuple(results)


def _validate_candidate_identity(value: Any, field: str) -> dict[str, Any]:
    identity = _exact(
        value,
        frozenset(
            {
                "manifest_digest",
                "vfs_layer_sha256",
                "vfs_layer_bytes",
                "builder_report_sha256",
            }
        ),
        field,
    )
    return {
        "manifest_digest": _oci_digest(identity["manifest_digest"], f"{field} manifest"),
        "vfs_layer_sha256": _digest(
            identity["vfs_layer_sha256"], f"{field} VFS layer"
        ),
        "vfs_layer_bytes": _integer(
            identity["vfs_layer_bytes"], f"{field} VFS bytes", positive=True
        ),
        "builder_report_sha256": _digest(
            identity["builder_report_sha256"], f"{field} builder report"
        ),
    }


def _validate_runtime_identity(value: Any, field: str) -> dict[str, Any]:
    runtime = _exact(
        value,
        frozenset(
            {
                "bundle_sha256",
                "source",
                "target_abi",
                "kernel",
                "host_runtime",
                "browser",
                "build_policy_sha256",
            }
        ),
        field,
    )
    _digest(runtime["bundle_sha256"], f"{field} bundle")
    _validate_source(runtime["source"], f"{field} source")
    target = _exact(
        runtime["target_abi"], frozenset({"version", "snapshot_sha256"}), f"{field} ABI"
    )
    _integer(target["version"], f"{field} ABI version")
    _digest(target["snapshot_sha256"], f"{field} ABI snapshot")
    kernel = _exact(
        runtime["kernel"],
        frozenset({"wasm_sha256", "bytes", "abi_version", "snapshot_sha256"}),
        f"{field} kernel",
    )
    _digest(kernel["wasm_sha256"], f"{field} kernel")
    _integer(kernel["bytes"], f"{field} kernel bytes", positive=True)
    _integer(kernel["abi_version"], f"{field} kernel ABI")
    _digest(kernel["snapshot_sha256"], f"{field} kernel snapshot")
    host = _exact(
        runtime["host_runtime"],
        frozenset(
            {"bundle_sha256", "bytes", "generated_abi_sha256", "worker_protocol_sha256"}
        ),
        f"{field} host runtime",
    )
    for key in ("bundle_sha256", "generated_abi_sha256", "worker_protocol_sha256"):
        _digest(host[key], f"{field} host {key}")
    _integer(host["bytes"], f"{field} host bytes", positive=True)
    browser = _exact(
        runtime["browser"],
        BROWSER_RUNTIME_KEYS,
        f"{field} browser",
    )
    for key in (
        "bundle_sha256",
        "harness_entry_sha256",
        "host_entry_sha256",
        "kernel_asset_sha256",
        "service_worker_sha256",
    ):
        _digest(browser[key], f"{field} browser {key}")
    _integer(browser["bytes"], f"{field} browser bytes", positive=True)
    for key in ("harness_entry_bytes", "host_entry_bytes"):
        _integer(browser[key], f"{field} browser {key}", positive=True)
    harness_path = _relative_path(
        browser["harness_entry_path"], f"{field} browser harness path"
    )
    host_entry_path = _relative_path(
        browser["host_entry_path"], f"{field} browser host path"
    )
    kernel_asset_path = _relative_path(
        browser["kernel_asset_path"], f"{field} browser kernel path"
    )
    if (
        harness_path != "browser/dist/abi-staging-harness/index.html"
        or host_entry_path != "browser/dist/abi-staging/browser-host.js"
        or not kernel_asset_path.startswith("browser/dist/")
        or not kernel_asset_path.endswith(".wasm")
        or browser["kernel_asset_sha256"] != kernel["wasm_sha256"]
    ):
        raise ProductEvidenceError(f"{field} browser entry identity differs")
    _digest(runtime["build_policy_sha256"], f"{field} build policy")
    return _plain(runtime)


def _validate_run(value: Any, field: str, *, evidence: bool) -> dict[str, Any]:
    keys = (
        frozenset({"repository", "workflow_ref", "run_id", "job_id", "attempt"})
        if evidence
        else frozenset({"repository", "workflow_ref", "run_id", "run_attempt", "job"})
    )
    run = _exact(value, keys, field)
    repository = _text(run["repository"], f"{field} repository", 255)
    if re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository) is None:
        raise ProductEvidenceError(f"{field} repository is not owner/name")
    job_key = "job_id" if evidence else "job"
    attempt_key = "attempt" if evidence else "run_attempt"
    _stable_id(run[job_key], f"{field} job")
    _text(run["workflow_ref"], f"{field} workflow ref", 2048)
    _integer(run["run_id"], f"{field} run ID", positive=True)
    _integer(run[attempt_key], f"{field} attempt", positive=True)
    return _plain(run)


def _guard_codes(value: Any, field: str, *, empty_allowed: bool) -> list[str]:
    codes = [_stable_id(item, field) for item in _sequence(value, field)]
    if codes != sorted(set(codes)) or (not empty_allowed and not codes):
        raise ProductEvidenceError(f"{field} must be sorted and duplicate-free")
    return codes


def _diagnostics(value: Any, field: str) -> list[dict[str, Any]]:
    diagnostics = _sequence(value, field)
    if len(diagnostics) > MAX_RESULT_DIAGNOSTICS:
        raise ProductEvidenceError(f"{field} exceeds its item bound")
    result = []
    previous = ""
    for index, candidate in enumerate(diagnostics):
        item = _exact(
            candidate,
            frozenset({"id", "sha256", "bytes", "text"}),
            f"{field} {index}",
        )
        identity = _stable_id(item["id"], f"{field} {index} ID")
        if identity <= previous:
            raise ProductEvidenceError(f"{field} must be sorted and duplicate-free")
        previous = identity
        text = _text(item["text"], f"{field} {identity} text", MAX_DIAGNOSTIC_BYTES)
        body = text.encode()
        if (
            _integer(item["bytes"], f"{field} {identity} bytes", positive=True)
            != len(body)
            or _digest(item["sha256"], f"{field} {identity} digest")
            != hashlib.sha256(body).hexdigest()
        ):
            raise ProductEvidenceError(f"{field} {identity} differs from its bytes")
        result.append(_plain(item))
    return result


def validate_product_evidence_result(value: Mapping[str, Any]) -> None:
    result = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "request_digest",
                "product",
                "candidate_product",
                "runtime",
                "host",
                "definition",
                "outcome",
                "guard_codes",
                "bounded_diagnostics",
                "run",
            }
        ),
        "product evidence result",
    )
    if (
        result["schema"] != 1
        or result["kind"] != "kandelo-vfs-product-evidence-result"
    ):
        raise ProductEvidenceError("product evidence result protocol is unsupported")
    _digest(result["request_digest"], "product evidence request")
    _validate_product(result["product"], "product evidence product", full=False)
    _validate_candidate_identity(result["candidate_product"], "product evidence candidate")
    _validate_runtime_identity(result["runtime"], "product evidence runtime")
    if result["host"] not in {"node", "browser"}:
        raise ProductEvidenceError("product evidence host is unsupported")
    definition = _exact(
        result["definition"],
        frozenset({"id", "definition_sha256"}),
        "product evidence definition",
    )
    _stable_id(definition["id"], "product evidence definition ID")
    _digest(definition["definition_sha256"], "product evidence definition digest")
    if result["outcome"] not in {"success", "failure", "timeout"}:
        raise ProductEvidenceError("product evidence outcome is unsupported")
    codes = _guard_codes(
        result["guard_codes"],
        "product evidence guard codes",
        empty_allowed=result["outcome"] == "success",
    )
    expected_codes = {
        "success": [],
        "failure": ["verification_failed"],
        "timeout": ["verification_timeout"],
    }[result["outcome"]]
    if codes != expected_codes:
        raise ProductEvidenceError("product evidence outcome and guards contradict")
    _diagnostics(result["bounded_diagnostics"], "product evidence diagnostics")
    _validate_run(result["run"], "product evidence run", evidence=True)
    if len(canonical_bytes(result)) > MAX_DOCUMENT_BYTES:
        raise ProductEvidenceError("product evidence result exceeds its byte bound")


def _validate_requirement(value: Any, field: str) -> dict[str, Any]:
    requirement = _exact(
        value,
        frozenset({"host", "id", "definition_sha256", "applicability"}),
        field,
    )
    if requirement["host"] not in {"node", "browser"}:
        raise ProductEvidenceError(f"{field} host is unsupported")
    if requirement["applicability"] not in {"required", "informational"}:
        raise ProductEvidenceError(f"{field} applicability is unsupported")
    return {
        "host": requirement["host"],
        "id": _stable_id(requirement["id"], f"{field} ID"),
        "definition_sha256": _digest(
            requirement["definition_sha256"], f"{field} definition"
        ),
        "applicability": requirement["applicability"],
    }


def _validate_override(
    value: Any,
    *,
    result_sha256: str,
    request_digest: str,
    product: Mapping[str, Any],
    candidate_product: Mapping[str, Any],
    requirement: Mapping[str, Any],
    outcome: str,
    guard_codes: Sequence[str],
    expected_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    override = _exact(
        value,
        frozenset({"record_sha256", "immutable_reference", "record"}),
        "product evidence accepted override",
    )
    record_sha = _digest(override["record_sha256"], "override record digest")
    reference = _text(override["immutable_reference"], "override immutable reference")
    if f"sha256:{record_sha}" not in reference or any(
        character.isspace() for character in reference
    ):
        raise ProductEvidenceError("override reference does not bind its record digest")
    record = _exact(
        override["record"],
        frozenset(
            {
                "schema",
                "kind",
                "request_digest",
                "subject_result_sha256",
                "product",
                "candidate_product",
                "host",
                "definition",
                "outcome",
                "accepted_guard_codes",
                "maintainer",
                "justification",
                "policy",
                "run",
            }
        ),
        "product evidence override record",
    )
    if (
        hashlib.sha256(canonical_bytes(record)).hexdigest() != record_sha
        or record["schema"] != 1
        or record["kind"] != "kandelo-vfs-product-evidence-override"
    ):
        raise ProductEvidenceError("override record identity or digest is invalid")
    if _digest(record["subject_result_sha256"], "override result subject") != result_sha256:
        raise ProductEvidenceError("override names a different exact evidence result")
    accepted = _guard_codes(
        record["accepted_guard_codes"],
        "override accepted guard codes",
        empty_allowed=False,
    )
    if list(guard_codes) != accepted:
        raise ProductEvidenceError("override does not accept exactly the result guards")
    if (
        record["request_digest"] != request_digest
        or record["product"] != product
        or record["candidate_product"] != candidate_product
        or record["host"] != requirement["host"]
        or record["definition"]
        != {
            "id": requirement["id"],
            "definition_sha256": requirement["definition_sha256"],
        }
        or record["outcome"] != outcome
    ):
        raise ProductEvidenceError(
            "override differs from the exact request, product, host, definition, or outcome"
        )
    maintainer = _exact(
        record["maintainer"],
        frozenset({"login", "permission", "authorization_reference"}),
        "override maintainer",
    )
    _stable_id(maintainer["login"], "override maintainer login")
    if maintainer["permission"] not in {"maintain", "admin"}:
        raise ProductEvidenceError("override maintainer lacks maintain or admin permission")
    authorization = _text(
        maintainer["authorization_reference"],
        "override authorization reference",
        2048,
    )
    if not authorization.startswith("https://github.com/"):
        raise ProductEvidenceError("override authorization is not an exact GitHub reference")
    justification = _text(record["justification"], "override justification", 2048)
    if len(justification.strip()) < 16:
        raise ProductEvidenceError("override justification is too short")
    policy = _exact(
        record["policy"],
        frozenset(
            {
                "policy_version",
                "policy_sha256",
                "guard_registry_version",
                "guard_registry_sha256",
            }
        ),
        "override policy",
    )
    _integer(policy["policy_version"], "override policy version", positive=True)
    _digest(policy["policy_sha256"], "override policy digest")
    _integer(
        policy["guard_registry_version"],
        "override guard registry version",
        positive=True,
    )
    _digest(policy["guard_registry_sha256"], "override guard registry digest")
    if expected_policy is not None and _plain(policy) != _plain(expected_policy):
        raise ProductEvidenceError("override policy differs from the exact request authority")
    _validate_run(record["run"], "override publication run", evidence=False)
    return _plain(override)


def build_product_evidence_receipt(
    result: Mapping[str, Any],
    *,
    request_digest: str,
    product: Mapping[str, Any],
    candidate_product: CandidateProductLocatorV1,
    runtime_bundle_body: bytes,
    requirement: Mapping[str, Any],
    accepted_override: Mapping[str, Any] | None = None,
    expected_override_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Revalidate one inert host result and issue a separate factual receipt."""

    validate_product_evidence_result(result)
    expected_product = _validate_product(product, "expected product", full=True)
    expected_requirement = _validate_requirement(requirement, "expected evidence requirement")
    expected_runtime = runtime_evidence_identity(runtime_bundle_body)
    expected_result_product = {
        "id": expected_product["id"],
        "manifest_sha256": expected_product["manifest_sha256"],
    }
    expected_definition = {
        "id": expected_requirement["id"],
        "definition_sha256": expected_requirement["definition_sha256"],
    }
    if (
        result["request_digest"] != _digest(request_digest, "expected request digest")
        or result["product"] != expected_result_product
        or result["candidate_product"] != candidate_product.evidence_identity()
        or result["runtime"] != expected_runtime
        or result["host"] != expected_requirement["host"]
        or result["definition"] != expected_definition
    ):
        raise ProductEvidenceError(
            "product evidence result differs from its exact request, product, "
            "runtime, or definition"
        )
    result_sha256 = canonical_sha256(result)
    checked_override = None
    if accepted_override is not None:
        if result["outcome"] == "success":
            raise ProductEvidenceError("successful evidence cannot consume an override")
        if expected_override_policy is None:
            raise ProductEvidenceError(
                "product evidence override requires the exact request policy identity"
            )
        checked_override = _validate_override(
            accepted_override,
            result_sha256=result_sha256,
            request_digest=request_digest,
            product=expected_result_product,
            candidate_product=candidate_product.evidence_identity(),
            requirement=expected_requirement,
            outcome=result["outcome"],
            guard_codes=result["guard_codes"],
            expected_policy=expected_override_policy,
        )
    receipt = {
        "schema": 1,
        "kind": "kandelo-vfs-product-evidence-receipt",
        "request_digest": request_digest,
        "product": expected_result_product,
        "candidate_product": candidate_product.evidence_identity(),
        "runtime_bundle_sha256": expected_runtime["bundle_sha256"],
        "requirement": expected_requirement,
        "result_sha256": result_sha256,
        "outcome": result["outcome"],
        "guard_codes": _plain(result["guard_codes"]),
        "bounded_diagnostics": _plain(result["bounded_diagnostics"]),
        "run": _plain(result["run"]),
        "accepted_override": checked_override,
    }
    validate_product_evidence_receipt(receipt)
    return receipt


def validate_product_evidence_receipt(value: Mapping[str, Any]) -> None:
    receipt = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "request_digest",
                "product",
                "candidate_product",
                "runtime_bundle_sha256",
                "requirement",
                "result_sha256",
                "outcome",
                "guard_codes",
                "bounded_diagnostics",
                "run",
                "accepted_override",
            }
        ),
        "product evidence receipt",
    )
    if (
        receipt["schema"] != 1
        or receipt["kind"] != "kandelo-vfs-product-evidence-receipt"
    ):
        raise ProductEvidenceError("product evidence receipt protocol is unsupported")
    _digest(receipt["request_digest"], "product evidence receipt request")
    _validate_product(receipt["product"], "product evidence receipt product", full=False)
    _validate_candidate_identity(
        receipt["candidate_product"], "product evidence receipt candidate"
    )
    _digest(receipt["runtime_bundle_sha256"], "product evidence receipt runtime")
    _validate_requirement(receipt["requirement"], "product evidence receipt requirement")
    result_sha = _digest(receipt["result_sha256"], "product evidence receipt result")
    if receipt["outcome"] not in {"success", "failure", "timeout"}:
        raise ProductEvidenceError("product evidence receipt outcome is unsupported")
    codes = _guard_codes(
        receipt["guard_codes"],
        "product evidence receipt guard codes",
        empty_allowed=receipt["outcome"] == "success",
    )
    if (receipt["outcome"] == "success") != (codes == []):
        raise ProductEvidenceError("product evidence receipt outcome and guards contradict")
    _diagnostics(receipt["bounded_diagnostics"], "product evidence receipt diagnostics")
    _validate_run(receipt["run"], "product evidence receipt run", evidence=True)
    if receipt["accepted_override"] is not None:
        if receipt["outcome"] == "success":
            raise ProductEvidenceError("successful receipt cannot carry an override")
        _validate_override(
            receipt["accepted_override"],
            result_sha256=result_sha,
            request_digest=receipt["request_digest"],
            product=receipt["product"],
            candidate_product=receipt["candidate_product"],
            requirement=receipt["requirement"],
            outcome=receipt["outcome"],
            guard_codes=codes,
        )
    if len(canonical_bytes(receipt)) > MAX_DOCUMENT_BYTES:
        raise ProductEvidenceError("product evidence receipt exceeds its byte bound")


def _registries(value: Any) -> list[dict[str, str]]:
    result = []
    previous: tuple[str, str] | None = None
    for index, candidate in enumerate(_sequence(value, "selecting registries")):
        registry = _exact(
            candidate,
            frozenset({"kind", "path", "sha256"}),
            f"selecting registry {index}",
        )
        if registry["kind"] not in {"pages", "tests"}:
            raise ProductEvidenceError("selecting registry kind is unsupported")
        checked = {
            "kind": registry["kind"],
            "path": _relative_path(registry["path"], "selecting registry path"),
            "sha256": _digest(registry["sha256"], "selecting registry digest"),
        }
        key = (checked["kind"], checked["path"])
        if previous is not None and previous >= key:
            raise ProductEvidenceError(
                "selecting registries must be sorted and duplicate-free"
            )
        previous = key
        result.append(checked)
    if not result:
        raise ProductEvidenceError("product evidence requires a selecting registry")
    return result


def _formula_layers(resolved: Mapping[str, Any]) -> list[dict[str, Any]]:
    layers = []
    for item in resolved["inputs"]:
        if item["kind"] != "homebrew-bottle":
            continue
        if "reference" not in item:
            raise ProductEvidenceError("resolved Formula layer lacks an immutable reference")
        layers.append(
            {
                "id": item["id"],
                "artifact": {
                    "sha256": item["sha256"],
                    "bytes": item["bytes"],
                    "immutable_reference": item["reference"],
                },
            }
        )
    layers.sort(key=lambda item: item["id"])
    return layers


def _common_artifact(repository: str, sha256: str, size: int) -> dict[str, Any]:
    return _artifact_for_blob(repository[len("ghcr.io/") :], sha256, size)


def _runtime_evidence_sha256(
    *,
    candidate_product: CandidateProductLocatorV1,
    runtime_bundle_body: bytes,
    resolved_inputs_body: bytes,
    requirements: Mapping[tuple[str, str], Mapping[str, Any]],
    receipts: Mapping[tuple[str, str], Mapping[str, Any]],
) -> str:
    receipt_outcomes = []
    for key in sorted(requirements):
        requirement = requirements[key]
        receipt = receipts[key]
        receipt_outcomes.append(
            {
                "accepted_with_override": receipt["accepted_override"] is not None,
                "applicability": requirement["applicability"],
                "definition_sha256": requirement["definition_sha256"],
                "guard_codes": _plain(receipt["guard_codes"]),
                "host": requirement["host"],
                "id": requirement["id"],
                "outcome": receipt["outcome"],
            }
        )
    identity = {
        "schema": 1,
        "kind": "kandelo-vfs-runtime-evidence-identity",
        "candidate_product": {
            "product_id": candidate_product.product_id,
            "repository": candidate_product.repository,
            "manifest_digest": candidate_product.manifest_digest,
            "immutable_reference": candidate_product.immutable_reference,
            "vfs_layer_sha256": candidate_product.vfs_layer_sha256,
            "vfs_layer_bytes": candidate_product.vfs_layer_bytes,
            "builder_report_sha256": candidate_product.builder_report_sha256,
        },
        "runtime_bundle_sha256": hashlib.sha256(runtime_bundle_body).hexdigest(),
        "resolved_inputs_sha256": hashlib.sha256(resolved_inputs_body).hexdigest(),
        "evidence_definition_sha256s": sorted(
            {item["definition_sha256"] for item in requirements.values()}
        ),
        "receipt_outcomes": receipt_outcomes,
    }
    return canonical_sha256(identity)


def build_product_evidence_record(
    *,
    request_digest: str,
    candidate_product: CandidateProductLocatorV1,
    resolved_inputs_body: bytes,
    builder_report_body: bytes,
    runtime_bundle_body: bytes,
    selecting_registries: Sequence[Mapping[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
) -> dict[str, Any]:
    """Aggregate terminal host receipts without mutating candidate identity."""

    request_sha = _digest(request_digest, "product evidence request")
    resolved = _load_resolved(resolved_inputs_body)
    product = _validate_product(resolved["product"], "resolved product", full=True)
    if product["id"] != candidate_product.product_id:
        raise ProductEvidenceError("candidate product ID differs from resolved inputs")
    report = _validate_builder_report_shape(builder_report_body)
    if (
        report["product"] != resolved["product"]
        or report["resolved_inputs_sha256"] != hashlib.sha256(resolved_inputs_body).hexdigest()
        or report["inputs"] != _expected_report_inputs(resolved)
        or report["output"]["sha256"] != candidate_product.vfs_layer_sha256
        or report["output"]["bytes"] != candidate_product.vfs_layer_bytes
        or hashlib.sha256(builder_report_body).hexdigest()
        != candidate_product.builder_report_sha256
    ):
        raise ProductEvidenceError("candidate locator differs from its exact builder report")
    runtime = _validate_runtime_bundle(runtime_bundle_body, expected_inputs=resolved)
    runtime_identity = runtime_evidence_identity(runtime_bundle_body)
    registries = _registries(selecting_registries)
    expected_requirements = [
        _validate_requirement(value, f"product evidence requirement {index}")
        for index, value in enumerate(requirements)
    ]
    requirement_keys = [(item["host"], item["id"]) for item in expected_requirements]
    if not expected_requirements or requirement_keys != sorted(set(requirement_keys)):
        raise ProductEvidenceError(
            "product evidence requirements must be sorted and duplicate-free"
        )
    expected_by_key = {
        (item["host"], item["id"]): item for item in expected_requirements
    }
    receipts_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for receipt in receipts:
        validate_product_evidence_receipt(receipt)
        requirement = receipt["requirement"]
        key = (requirement["host"], requirement["id"])
        if key in receipts_by_key:
            raise ProductEvidenceError("product evidence repeats a host definition receipt")
        if (
            receipt["request_digest"] != request_sha
            or receipt["product"]
            != {"id": product["id"], "manifest_sha256": product["manifest_sha256"]}
            or receipt["candidate_product"] != candidate_product.evidence_identity()
            or receipt["runtime_bundle_sha256"] != runtime_identity["bundle_sha256"]
            or requirement != expected_by_key.get(key)
        ):
            raise ProductEvidenceError(
                "product evidence receipt differs from exact aggregate inputs"
            )
        receipts_by_key[key] = receipt
    if set(receipts_by_key) != set(expected_by_key):
        raise ProductEvidenceError("product evidence lacks one or more sibling receipts")

    runtime_evidence_sha256 = _runtime_evidence_sha256(
        candidate_product=candidate_product,
        runtime_bundle_body=runtime_bundle_body,
        resolved_inputs_body=resolved_inputs_body,
        requirements=expected_by_key,
        receipts=receipts_by_key,
    )

    blocking = []
    accepted_required_override = False
    for key in sorted(expected_by_key):
        requirement = expected_by_key[key]
        receipt = receipts_by_key[key]
        if requirement["applicability"] != "required" or receipt["outcome"] == "success":
            continue
        if receipt["accepted_override"] is not None:
            accepted_required_override = True
            continue
        blocking.append(receipt)
    if blocking:
        outcome = (
            "failure"
            if any(receipt["outcome"] == "failure" for receipt in blocking)
            else "timeout"
        )
        guards = sorted(
            {
                code
                for receipt in blocking
                for code in receipt["guard_codes"]
            }
        )
        blockers = sorted(
            (
                {
                    "guard_code": code,
                    "subject_kind": "product",
                    "subject": (
                        f"{receipt['requirement']['host']}/"
                        f"{receipt['requirement']['id']}"
                    ),
                }
                for receipt in blocking
                for code in receipt["guard_codes"]
            ),
            key=lambda item: (item["guard_code"], item["subject_kind"], item["subject"]),
        )
        promotion_state = "ineligible"
    else:
        outcome = "success"
        guards = []
        blockers = []
        promotion_state = (
            "accepted-with-override" if accepted_required_override else "eligible"
        )

    vfs_artifact = _common_artifact(
        candidate_product.repository,
        candidate_product.vfs_layer_sha256,
        candidate_product.vfs_layer_bytes,
    )
    repository = candidate_product.repository[len("ghcr.io/") :]
    record = {
        "schema": 1,
        "kind": "kandelo-abi-staging-product-evidence",
        "common": {
            "request_sha256": request_sha,
            "subject": {"kind": "product", "identity": product["id"]},
            "source": _plain(resolved["source"]),
            "run": _validate_run(run, "product evidence publication run", evidence=False),
            "guard_codes": guards,
            "work_state": "complete",
            "outcome": outcome,
            "artifact_class": "candidate",
            "artifact": vfs_artifact,
            "promotion_state": promotion_state,
            "retry_state": {
                "attempts": 1,
                "eligible": False,
                "exhausted": False,
                "next_action": "none",
            },
            "blockers": blockers,
        },
        "product_evidence": {
            "product": _plain(resolved["product"]),
            "selecting_registries": registries,
            "resolved_formula_layers": _formula_layers(resolved),
            "resolved_inputs_sha256": hashlib.sha256(resolved_inputs_body).hexdigest(),
            "runtime_evidence_sha256": runtime_evidence_sha256,
            "vfs_image": vfs_artifact,
            "builder_report": _artifact_for_blob(
                repository,
                hashlib.sha256(builder_report_body).hexdigest(),
                len(builder_report_body),
            ),
            "kernel": _artifact_for_blob(
                repository, runtime["kernel"]["wasm_sha256"], runtime["kernel"]["bytes"]
            ),
            "host_runtime": _artifact_for_blob(
                repository, runtime["host"]["bundle_sha256"], runtime["host"]["bytes"]
            ),
            "evidence_definition_sha256s": sorted(
                {item["definition_sha256"] for item in expected_requirements}
            ),
            "verification_receipt_sha256s": sorted(
                canonical_sha256(receipt) for receipt in receipts_by_key.values()
            ),
        },
    }
    validate_product_evidence_record(record)
    return record


def _validate_record_shape(value: Mapping[str, Any]) -> None:
    record = _exact(
        value,
        frozenset({"schema", "kind", "common", "product_evidence"}),
        "product evidence record",
    )
    if (
        record["schema"] != 1
        or record["kind"] != "kandelo-abi-staging-product-evidence"
    ):
        raise ProductEvidenceError("product evidence record protocol is unsupported")
    common = _exact(
        record["common"],
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
        "product evidence common",
    )
    _digest(common["request_sha256"], "product evidence request")
    subject = _exact(
        common["subject"], frozenset({"kind", "identity"}), "product evidence subject"
    )
    if subject["kind"] != "product":
        raise ProductEvidenceError("product evidence subject is not a product")
    _stable_id(subject["identity"], "product evidence subject identity")
    _validate_source(common["source"], "product evidence source")
    _validate_run(common["run"], "product evidence run", evidence=False)
    guards = _guard_codes(
        common["guard_codes"],
        "product evidence common guards",
        empty_allowed=common["outcome"] == "success",
    )
    if common["work_state"] != "complete" or common["outcome"] not in {
        "success",
        "failure",
        "timeout",
    }:
        raise ProductEvidenceError("product evidence common terminal state is invalid")
    if common["artifact_class"] != "candidate":
        raise ProductEvidenceError("product evidence artifact is not a candidate")
    common_artifact = _artifact(common["artifact"], "product evidence artifact")
    if common["promotion_state"] not in {
        "eligible",
        "ineligible",
        "accepted-with-override",
    }:
        raise ProductEvidenceError("product evidence promotion state is unsupported")
    if (common["outcome"] == "success") != (guards == []):
        raise ProductEvidenceError("product evidence outcome and guards contradict")
    if common["outcome"] == "success" and common["promotion_state"] == "ineligible":
        raise ProductEvidenceError("successful product evidence cannot be ineligible")
    if common["outcome"] != "success" and common["promotion_state"] != "ineligible":
        raise ProductEvidenceError("failed product evidence must be ineligible")
    retry = _exact(
        common["retry_state"],
        frozenset({"attempts", "eligible", "exhausted", "next_action"}),
        "product evidence retry state",
    )
    if retry != {
        "attempts": 1,
        "eligible": False,
        "exhausted": False,
        "next_action": "none",
    }:
        raise ProductEvidenceError("product evidence retry state is contradictory")
    blockers = _sequence(common["blockers"], "product evidence blockers")
    if common["outcome"] == "success" and blockers:
        raise ProductEvidenceError("successful product evidence cannot contain blockers")
    previous = None
    for index, candidate in enumerate(blockers):
        blocker = _exact(
            candidate,
            frozenset({"guard_code", "subject_kind", "subject"}),
            f"product evidence blocker {index}",
        )
        if blocker["guard_code"] not in guards or blocker["subject_kind"] != "product":
            raise ProductEvidenceError("product evidence blocker contradicts common guards")
        key = (blocker["guard_code"], blocker["subject_kind"], blocker["subject"])
        if previous is not None and previous >= key:
            raise ProductEvidenceError("product evidence blockers are not sorted and unique")
        previous = key

    payload = _exact(
        record["product_evidence"],
        frozenset(
            {
                "product",
                "selecting_registries",
                "resolved_formula_layers",
                "resolved_inputs_sha256",
                "runtime_evidence_sha256",
                "vfs_image",
                "builder_report",
                "kernel",
                "host_runtime",
                "evidence_definition_sha256s",
                "verification_receipt_sha256s",
            }
        ),
        "product evidence payload",
    )
    product = _validate_product(payload["product"], "product evidence product", full=True)
    if product["id"] != subject["identity"]:
        raise ProductEvidenceError("product evidence subject differs from its product")
    _registries(payload["selecting_registries"])
    previous_id = ""
    for index, candidate in enumerate(
        _sequence(payload["resolved_formula_layers"], "resolved Formula layers")
    ):
        item = _exact(
            candidate,
            frozenset({"id", "artifact"}),
            f"resolved Formula layer {index}",
        )
        identity = _stable_id(item["id"], f"resolved Formula layer {index} ID")
        if identity <= previous_id:
            raise ProductEvidenceError("resolved Formula layers are not sorted and unique")
        previous_id = identity
        _artifact(item["artifact"], f"resolved Formula layer {identity}")
    _digest(payload["resolved_inputs_sha256"], "product evidence resolved inputs")
    _digest(payload["runtime_evidence_sha256"], "product runtime evidence")
    vfs = _artifact(payload["vfs_image"], "product evidence VFS")
    for key in ("builder_report", "kernel", "host_runtime"):
        _artifact(payload[key], f"product evidence {key}")
    if vfs != common_artifact:
        raise ProductEvidenceError("product evidence common artifact differs from its VFS")
    for field in ("evidence_definition_sha256s", "verification_receipt_sha256s"):
        values = [_digest(item, field) for item in _sequence(payload[field], field)]
        if not values or values != sorted(set(values)):
            raise ProductEvidenceError(f"{field} must be sorted and duplicate-free")
    if len(canonical_bytes(record)) > MAX_DOCUMENT_BYTES:
        raise ProductEvidenceError("product evidence record exceeds its byte bound")


def validate_product_evidence_record(
    value: Mapping[str, Any],
    *,
    request_digest: str | None = None,
    candidate_product: CandidateProductLocatorV1 | None = None,
    resolved_inputs_body: bytes | None = None,
    builder_report_body: bytes | None = None,
    runtime_bundle_body: bytes | None = None,
    selecting_registries: Sequence[Mapping[str, Any]] | None = None,
    requirements: Sequence[Mapping[str, Any]] | None = None,
    receipts: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    """Validate shape alone, or rederive the complete record from exact inputs."""

    _validate_record_shape(value)
    expected_arguments = (
        request_digest,
        candidate_product,
        resolved_inputs_body,
        builder_report_body,
        runtime_bundle_body,
        selecting_registries,
        requirements,
        receipts,
    )
    if all(item is None for item in expected_arguments):
        return
    if any(item is None for item in expected_arguments):
        raise ProductEvidenceError(
            "complete expected inputs are required for contextual product evidence validation"
        )
    expected = build_product_evidence_record(
        request_digest=request_digest,
        candidate_product=candidate_product,
        resolved_inputs_body=resolved_inputs_body,
        builder_report_body=builder_report_body,
        runtime_bundle_body=runtime_bundle_body,
        selecting_registries=selecting_registries,
        requirements=requirements,
        receipts=receipts,
        run=value["common"]["run"],
    )
    if _plain(value) != expected:
        raise ProductEvidenceError("product evidence record differs from exact rederivation")


def inspect_product_evidence_repository(
    repository: str,
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    product: Mapping[str, Any],
    candidate_product: CandidateProductLocatorV1,
    runtime_bundle_sha256: str,
    expected_source_repository: str,
    transport: OciTransportV1,
) -> tuple[ProductEvidenceInventoryEntryV1, ...]:
    """Recover complete aggregate facts through anonymous manifest/config reads."""

    checked_repository = _repository(
        repository, "product evidence inventory repository"
    )
    suffix = "/evidence"
    if not checked_repository.endswith(suffix):
        raise ProductEvidenceError(
            "product evidence inventory repository is not an aggregate repository"
        )
    product_repository = checked_repository[: -len(suffix)]
    checked_product = _validate_product(
        product, "product evidence inventory product", full=True
    )
    match = PRODUCT_REPOSITORY.fullmatch(product_repository)
    if match is None or match.group("product") != checked_product["id"]:
        raise ProductEvidenceError(
            "product evidence inventory is outside its reserved product repository"
        )
    checked_request = _plain(request)
    checked_request_sha256 = _digest(
        request_sha256, "product evidence inventory request"
    )
    if canonical_sha256(checked_request) != checked_request_sha256:
        raise ProductEvidenceError(
            "product evidence inventory request digest is not canonical"
        )
    source = _validate_source(
        checked_request.get("build_source"), "product evidence inventory source"
    )
    target = _exact(
        checked_request.get("target_abi"),
        frozenset({"version", "snapshot_sha256"}),
        "product evidence inventory ABI",
    )
    target_abi = _integer(target["version"], "product evidence inventory ABI version")
    _digest(target["snapshot_sha256"], "product evidence inventory ABI snapshot")
    if int(match.group("abi")) != target_abi:
        raise ProductEvidenceError(
            "product evidence inventory repository has the wrong ABI"
        )
    if (
        not isinstance(candidate_product, CandidateProductLocatorV1)
        or candidate_product.product_id != checked_product["id"]
        or candidate_product.repository != "ghcr.io/" + product_repository
    ):
        raise ProductEvidenceError(
            "product evidence inventory candidate differs from its repository"
        )
    checked_runtime_sha256 = _digest(
        runtime_bundle_sha256, "product evidence inventory runtime bundle"
    )
    checked_publisher = _text(
        expected_source_repository,
        "product evidence inventory publisher repository",
        255,
    )
    if re.fullmatch(
        r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", checked_publisher
    ) is None:
        raise ProductEvidenceError(
            "product evidence inventory publisher is not owner/name"
        )

    results = []
    try:
        for locator in list_public_record_locators(
            checked_repository, transport=transport
        ):
            fetched = fetch_public_record(
                locator,
                transport=transport,
                expected_artifact_type=PRODUCT_EVIDENCE_RECORD_MEDIA_TYPE,
                required_layer_roles=(),
            )
            manifest = _exact(
                _load_canonical(fetched.manifest, "product evidence OCI manifest"),
                frozenset(
                    {
                        "schemaVersion",
                        "mediaType",
                        "artifactType",
                        "config",
                        "layers",
                        "annotations",
                    }
                ),
                "product evidence OCI manifest",
            )
            record = _load_canonical(
                fetched.config.body, "product evidence inventory record"
            )
            validate_product_evidence_record(record)
            payload = record["product_evidence"]
            common = record["common"]
            expected_annotations = {
                "dev.kandelo.abi-staging.candidate-product-manifest": (
                    candidate_product.manifest_digest
                ),
                "dev.kandelo.abi-staging.classification": (
                    "public-candidate-not-endorsed"
                ),
                "dev.kandelo.abi-staging.kind": "product-evidence-record",
                "dev.kandelo.abi-staging.nonendorsed": "true",
                "dev.kandelo.abi-staging.product": checked_product["id"],
                "org.opencontainers.image.source": (
                    "https://github.com/" + checked_publisher
                ),
            }
            if (
                dict(manifest["annotations"]) != expected_annotations
                or common["request_sha256"] != checked_request_sha256
                or common["source"] != source
                or payload["product"] != checked_product
                or payload["vfs_image"]
                != _common_artifact(
                    candidate_product.repository,
                    candidate_product.vfs_layer_sha256,
                    candidate_product.vfs_layer_bytes,
                )
                or payload["builder_report"]["sha256"]
                != candidate_product.builder_report_sha256
            ):
                continue

            config = _candidate_manifest_descriptor(
                manifest["config"], "product evidence config"
            )
            config_artifact = {
                "sha256": canonical_sha256(record),
                "bytes": len(fetched.config.body),
            }
            _require_public_candidate_descriptor(
                config,
                artifact=config_artifact,
                role="product-evidence-record",
                media_type=PRODUCT_EVIDENCE_RECORD_MEDIA_TYPE,
                title="product-evidence-record.json",
                field="product evidence config",
            )
            descriptors = [
                _candidate_manifest_descriptor(
                    value, f"product evidence layer {index}"
                )
                for index, value in enumerate(
                    _sequence(manifest["layers"], "product evidence layers")
                )
            ]
            receipt_sha256s = payload["verification_receipt_sha256s"]
            expected_roles = ["runtime-bundle", "resolved-inputs"] + [
                f"receipt-{index:04d}" for index in range(len(receipt_sha256s))
            ]
            if [item["role"] for item in descriptors] != expected_roles:
                raise ProductEvidenceError(
                    "product evidence inventory layer roles changed"
                )
            if len(descriptors) != len(receipt_sha256s) + 2:
                raise ProductEvidenceError(
                    "product evidence inventory layer count changed"
                )
            if (
                descriptors[0]["media_type"] != RUNTIME_BUNDLE_MEDIA_TYPE
                or descriptors[0]["title"] != "runtime-bundle.json"
                or descriptors[0]["digest"]
                != "sha256:" + checked_runtime_sha256
                or descriptors[1]["media_type"] != RESOLVED_INPUTS_MEDIA_TYPE
                or descriptors[1]["title"] != "resolved-inputs.json"
                or descriptors[1]["digest"]
                != "sha256:" + payload["resolved_inputs_sha256"]
            ):
                continue
            for index, (descriptor, receipt_sha256) in enumerate(
                zip(descriptors[2:], receipt_sha256s, strict=True)
            ):
                if (
                    descriptor["media_type"]
                    != PRODUCT_EVIDENCE_RECEIPT_MEDIA_TYPE
                    or descriptor["title"] != f"receipts/{index:04d}.json"
                    or descriptor["digest"] != "sha256:" + receipt_sha256
                ):
                    raise ProductEvidenceError(
                        "product evidence inventory receipt descriptor changed"
                    )
            results.append(
                ProductEvidenceInventoryEntryV1(
                    record=_plain(record),
                    record_sha256=canonical_sha256(record),
                    manifest_digest=fetched.digest,
                    outcome=common["outcome"],
                    immutable_reference=fetched.immutable_reference,
                )
            )
    except (OciPublicationError, ValueError) as error:
        if isinstance(error, ProductEvidenceError):
            raise
        raise ProductEvidenceError(
            f"product evidence inventory is invalid: {error}"
        ) from error
    semantic_identities = {
        (
            item.record["product_evidence"]["runtime_evidence_sha256"],
            item.record["common"]["outcome"],
            item.record["common"]["promotion_state"],
        )
        for item in results
    }
    if len(semantic_identities) > 1:
        raise ProductEvidenceError(
            "product evidence inventory contains conflicting current aggregates"
        )
    results.sort(key=lambda item: item.record_sha256)
    return tuple(results)


def build_product_evidence_receipt_oci_plan(
    receipt: Mapping[str, Any],
    *,
    result: Mapping[str, Any],
    candidate_product: CandidateProductLocatorV1,
) -> OciRecordPlanV1:
    """Bind one protected receipt to the exact inert result it revalidated."""

    validate_product_evidence_receipt(receipt)
    validate_product_evidence_result(result)
    result_body = canonical_bytes(result)
    if (
        hashlib.sha256(result_body).hexdigest() != receipt["result_sha256"]
        or receipt["candidate_product"] != candidate_product.evidence_identity()
        or result["candidate_product"] != candidate_product.evidence_identity()
        or result["request_digest"] != receipt["request_digest"]
        or result["product"] != receipt["product"]
        or result["host"] != receipt["requirement"]["host"]
        or result["definition"]
        != {
            "id": receipt["requirement"]["id"],
            "definition_sha256": receipt["requirement"]["definition_sha256"],
        }
        or result["outcome"] != receipt["outcome"]
        or result["guard_codes"] != receipt["guard_codes"]
        or result["bounded_diagnostics"] != receipt["bounded_diagnostics"]
        or result["run"] != receipt["run"]
    ):
        raise ProductEvidenceError(
            "product evidence receipt differs from the exact inert result"
        )
    requirement = receipt["requirement"]
    repository = (
        candidate_product.repository[len("ghcr.io/") :]
        + f"/receipts/{requirement['id']}/{requirement['host']}"
    )
    body = canonical_bytes(receipt)
    return OciRecordPlanV1(
        repository=repository,
        artifact_type=PRODUCT_EVIDENCE_RECEIPT_MEDIA_TYPE,
        config=OciBlobV1(
            role="product-evidence-receipt",
            media_type=PRODUCT_EVIDENCE_RECEIPT_MEDIA_TYPE,
            body=body,
            title="product-evidence-receipt.json",
        ),
        layers=(
            OciBlobV1(
                role="product-evidence-result",
                media_type=PRODUCT_EVIDENCE_RESULT_MEDIA_TYPE,
                body=result_body,
                title="product-evidence-result.json",
            ),
        ),
        annotations={
            "dev.kandelo.abi-staging.candidate-product-manifest": candidate_product.manifest_digest,
            "dev.kandelo.abi-staging.classification": "public-candidate-not-endorsed",
            "dev.kandelo.abi-staging.definition": requirement["id"],
            "dev.kandelo.abi-staging.host": requirement["host"],
            "dev.kandelo.abi-staging.kind": "product-evidence-receipt",
            "dev.kandelo.abi-staging.nonendorsed": "true",
            "dev.kandelo.abi-staging.product": candidate_product.product_id,
            "org.opencontainers.image.source": "https://github.com/"
            + receipt["run"]["repository"],
        },
    )


def publish_product_evidence_receipt(
    plan: OciRecordPlanV1,
    *,
    transport: OciTransportV1,
    expected_source_repository: str,
) -> PublishedRecordLocatorV1:
    if plan.artifact_type != PRODUCT_EVIDENCE_RECEIPT_MEDIA_TYPE:
        raise ProductEvidenceError("OCI plan is not a product evidence receipt")
    _require_blob_descriptor(
        plan.config,
        role="product-evidence-receipt",
        media_type=PRODUCT_EVIDENCE_RECEIPT_MEDIA_TYPE,
        title="product-evidence-receipt.json",
        field="product evidence receipt config",
    )
    receipt = _load_canonical(plan.config.body, "product evidence receipt")
    validate_product_evidence_receipt(receipt)
    requirement = receipt["requirement"]
    suffix = f"/receipts/{requirement['id']}/{requirement['host']}"
    if not plan.repository.endswith(suffix):
        raise ProductEvidenceError("product evidence receipt repository is not exact")
    product_repository = plan.repository[: -len(suffix)]
    match = PRODUCT_REPOSITORY.fullmatch(product_repository)
    if match is None or match.group("product") != receipt["product"]["id"]:
        raise ProductEvidenceError(
            "product evidence receipt is outside its reserved product repository"
        )
    if len(plan.layers) != 1 or plan.layers[0].role != "product-evidence-result":
        raise ProductEvidenceError("product evidence receipt result layer changed")
    _require_blob_descriptor(
        plan.layers[0],
        role="product-evidence-result",
        media_type=PRODUCT_EVIDENCE_RESULT_MEDIA_TYPE,
        title="product-evidence-result.json",
        field="product evidence receipt result",
    )
    result = _load_canonical(plan.layers[0].body, "product evidence receipt result")
    validate_product_evidence_result(result)
    if (
        hashlib.sha256(plan.layers[0].body).hexdigest() != receipt["result_sha256"]
        or result["request_digest"] != receipt["request_digest"]
        or result["product"] != receipt["product"]
        or result["candidate_product"] != receipt["candidate_product"]
        or result["host"] != requirement["host"]
        or result["definition"]
        != {
            "id": requirement["id"],
            "definition_sha256": requirement["definition_sha256"],
        }
        or result["outcome"] != receipt["outcome"]
        or result["guard_codes"] != receipt["guard_codes"]
        or result["bounded_diagnostics"] != receipt["bounded_diagnostics"]
        or result["run"] != receipt["run"]
    ):
        raise ProductEvidenceError(
            "product evidence receipt layer differs from its exact record"
        )
    expected_annotations = {
        "dev.kandelo.abi-staging.candidate-product-manifest": receipt[
            "candidate_product"
        ]["manifest_digest"],
        "dev.kandelo.abi-staging.classification": "public-candidate-not-endorsed",
        "dev.kandelo.abi-staging.definition": requirement["id"],
        "dev.kandelo.abi-staging.host": requirement["host"],
        "dev.kandelo.abi-staging.kind": "product-evidence-receipt",
        "dev.kandelo.abi-staging.nonendorsed": "true",
        "dev.kandelo.abi-staging.product": receipt["product"]["id"],
        "org.opencontainers.image.source": "https://github.com/"
        + expected_source_repository,
    }
    if dict(plan.annotations) != expected_annotations:
        raise ProductEvidenceError(
            "product evidence receipt annotations differ from protected policy"
        )
    return publish_record(
        plan,
        transport=transport,
        expected_source_repository=expected_source_repository,
    )


def build_product_evidence_oci_plan(
    record: Mapping[str, Any],
    *,
    candidate_product: CandidateProductLocatorV1,
    receipts: Sequence[Mapping[str, Any]],
    runtime_bundle_body: bytes,
    resolved_inputs_body: bytes,
) -> OciRecordPlanV1:
    """Make one anonymously readable aggregate without changing VFS identity."""

    validate_product_evidence_record(record)
    resolved = _load_resolved(resolved_inputs_body)
    _validate_runtime_bundle(runtime_bundle_body, expected_inputs=resolved)
    payload = record["product_evidence"]
    if (
        resolved["product"] != payload["product"]
        or hashlib.sha256(resolved_inputs_body).hexdigest()
        != payload["resolved_inputs_sha256"]
        or record["common"]["request_sha256"]
        != next(iter(receipts), {}).get("request_digest")
        or payload["vfs_image"]["sha256"] != candidate_product.vfs_layer_sha256
        or payload["vfs_image"]["bytes"] != candidate_product.vfs_layer_bytes
        or payload["builder_report"]["sha256"]
        != candidate_product.builder_report_sha256
        or payload["product"]["id"] != candidate_product.product_id
    ):
        raise ProductEvidenceError(
            "aggregate product evidence differs from candidate or resolved inputs"
        )
    runtime_sha = hashlib.sha256(runtime_bundle_body).hexdigest()
    ordered_receipts = sorted(
        (_plain(receipt) for receipt in receipts), key=canonical_sha256
    )
    receipt_sha256s = []
    receipt_layers = []
    for index, receipt in enumerate(ordered_receipts):
        validate_product_evidence_receipt(receipt)
        if (
            receipt["request_digest"] != record["common"]["request_sha256"]
            or receipt["product"]
            != {
                "id": payload["product"]["id"],
                "manifest_sha256": payload["product"]["manifest_sha256"],
            }
            or receipt["candidate_product"] != candidate_product.evidence_identity()
            or receipt["runtime_bundle_sha256"] != runtime_sha
        ):
            raise ProductEvidenceError(
                "aggregate contains a receipt for a different exact product"
            )
        body = canonical_bytes(receipt)
        receipt_sha256s.append(hashlib.sha256(body).hexdigest())
        receipt_layers.append(
            OciBlobV1(
                role=f"receipt-{index:04d}",
                media_type=PRODUCT_EVIDENCE_RECEIPT_MEDIA_TYPE,
                body=body,
                title=f"receipts/{index:04d}.json",
            )
        )
    if sorted(receipt_sha256s) != payload["verification_receipt_sha256s"]:
        raise ProductEvidenceError(
            "aggregate receipt layers differ from the product evidence record"
        )
    repository = (
        candidate_product.repository[len("ghcr.io/") :] + "/evidence"
    )
    return OciRecordPlanV1(
        repository=repository,
        artifact_type=PRODUCT_EVIDENCE_RECORD_MEDIA_TYPE,
        config=OciBlobV1(
            role="product-evidence-record",
            media_type=PRODUCT_EVIDENCE_RECORD_MEDIA_TYPE,
            body=canonical_bytes(record),
            title="product-evidence-record.json",
        ),
        layers=(
            OciBlobV1(
                role="runtime-bundle",
                media_type=RUNTIME_BUNDLE_MEDIA_TYPE,
                body=runtime_bundle_body,
                title="runtime-bundle.json",
            ),
            OciBlobV1(
                role="resolved-inputs",
                media_type=RESOLVED_INPUTS_MEDIA_TYPE,
                body=resolved_inputs_body,
                title="resolved-inputs.json",
            ),
            *receipt_layers,
        ),
        annotations={
            "dev.kandelo.abi-staging.candidate-product-manifest": candidate_product.manifest_digest,
            "dev.kandelo.abi-staging.classification": "public-candidate-not-endorsed",
            "dev.kandelo.abi-staging.kind": "product-evidence-record",
            "dev.kandelo.abi-staging.nonendorsed": "true",
            "dev.kandelo.abi-staging.product": candidate_product.product_id,
            "org.opencontainers.image.source": "https://github.com/"
            + record["common"]["run"]["repository"],
        },
    )


def publish_product_evidence_record(
    plan: OciRecordPlanV1,
    *,
    transport: OciTransportV1,
    expected_source_repository: str,
) -> PublishedRecordLocatorV1:
    if plan.artifact_type != PRODUCT_EVIDENCE_RECORD_MEDIA_TYPE:
        raise ProductEvidenceError("OCI plan is not an aggregate product evidence record")
    _require_blob_descriptor(
        plan.config,
        role="product-evidence-record",
        media_type=PRODUCT_EVIDENCE_RECORD_MEDIA_TYPE,
        title="product-evidence-record.json",
        field="aggregate product evidence config",
    )
    record = _load_canonical(plan.config.body, "product evidence record")
    validate_product_evidence_record(record)
    product = record["product_evidence"]["product"]
    suffix = "/evidence"
    if not plan.repository.endswith(suffix):
        raise ProductEvidenceError("aggregate product evidence repository is not exact")
    product_repository = plan.repository[: -len(suffix)]
    match = PRODUCT_REPOSITORY.fullmatch(product_repository)
    if match is None or match.group("product") != product["id"]:
        raise ProductEvidenceError(
            "aggregate evidence is outside its reserved product repository"
        )
    if len(plan.layers) < 3:
        raise ProductEvidenceError("aggregate product evidence layers are incomplete")
    expected_roles = ["runtime-bundle", "resolved-inputs"] + [
        f"receipt-{index:04d}" for index in range(len(plan.layers) - 2)
    ]
    if [layer.role for layer in plan.layers] != expected_roles:
        raise ProductEvidenceError("aggregate product evidence layer roles changed")
    _require_blob_descriptor(
        plan.layers[0],
        role="runtime-bundle",
        media_type=RUNTIME_BUNDLE_MEDIA_TYPE,
        title="runtime-bundle.json",
        field="aggregate runtime bundle",
    )
    _require_blob_descriptor(
        plan.layers[1],
        role="resolved-inputs",
        media_type=RESOLVED_INPUTS_MEDIA_TYPE,
        title="resolved-inputs.json",
        field="aggregate resolved inputs",
    )
    for index, layer in enumerate(plan.layers[2:]):
        _require_blob_descriptor(
            layer,
            role=f"receipt-{index:04d}",
            media_type=PRODUCT_EVIDENCE_RECEIPT_MEDIA_TYPE,
            title=f"receipts/{index:04d}.json",
            field=f"aggregate receipt {index}",
        )
    runtime_body = plan.layers[0].body
    resolved_body = plan.layers[1].body
    resolved = _load_resolved(resolved_body)
    _validate_runtime_bundle(runtime_body, expected_inputs=resolved)
    payload = record["product_evidence"]
    if (
        resolved["product"] != product
        or resolved["source"] != record["common"]["source"]
        or hashlib.sha256(resolved_body).hexdigest()
        != payload["resolved_inputs_sha256"]
    ):
        raise ProductEvidenceError(
            "aggregate runtime or resolved-input layer differs from its record"
        )
    receipt_sha256s = []
    candidate_identity = None
    for layer in plan.layers[2:]:
        receipt = _load_canonical(layer.body, "aggregate product evidence receipt")
        validate_product_evidence_receipt(receipt)
        if (
            receipt["request_digest"] != record["common"]["request_sha256"]
            or receipt["product"]
            != {
                "id": product["id"],
                "manifest_sha256": product["manifest_sha256"],
            }
            or receipt["runtime_bundle_sha256"]
            != hashlib.sha256(runtime_body).hexdigest()
        ):
            raise ProductEvidenceError(
                "aggregate receipt layer differs from the exact product"
            )
        if candidate_identity is None:
            candidate_identity = receipt["candidate_product"]
        elif candidate_identity != receipt["candidate_product"]:
            raise ProductEvidenceError("aggregate receipts name different candidates")
        receipt_sha256s.append(hashlib.sha256(layer.body).hexdigest())
    if sorted(receipt_sha256s) != payload["verification_receipt_sha256s"]:
        raise ProductEvidenceError(
            "aggregate receipt layer digests differ from its record"
        )
    if candidate_identity is None:
        raise ProductEvidenceError("aggregate product evidence lacks a candidate receipt")
    expected_annotations = {
        "dev.kandelo.abi-staging.candidate-product-manifest": candidate_identity[
            "manifest_digest"
        ],
        "dev.kandelo.abi-staging.classification": "public-candidate-not-endorsed",
        "dev.kandelo.abi-staging.kind": "product-evidence-record",
        "dev.kandelo.abi-staging.nonendorsed": "true",
        "dev.kandelo.abi-staging.product": product["id"],
        "org.opencontainers.image.source": "https://github.com/"
        + expected_source_repository,
    }
    if dict(plan.annotations) != expected_annotations:
        raise ProductEvidenceError(
            "aggregate product evidence annotations differ from protected policy"
        )
    return publish_record(
        plan,
        transport=transport,
        expected_source_repository=expected_source_repository,
    )


def publish_exact_product_evidence(
    *,
    request_digest: str,
    product: Mapping[str, Any],
    candidate_product: CandidateProductLocatorV1,
    runtime_bundle_body: bytes,
    resolved_inputs_body: bytes,
    builder_report_body: bytes,
    selecting_registries: Sequence[Mapping[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
    transport: OciTransportV1,
    expected_source_repository: str,
) -> dict[str, Any]:
    """Issue every factual receipt, then one exact aggregate evidence record."""

    checked_requirements = [
        _validate_requirement(value, f"product evidence requirement {index}")
        for index, value in enumerate(requirements)
    ]
    requirement_keys = [
        (value["host"], value["id"]) for value in checked_requirements
    ]
    if not checked_requirements or requirement_keys != sorted(set(requirement_keys)):
        raise ProductEvidenceError(
            "product evidence requirements must be sorted and duplicate-free"
        )

    results_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for result in results:
        validate_product_evidence_result(result)
        key = (result["host"], result["definition"]["id"])
        if key in results_by_key:
            raise ProductEvidenceError(
                "product evidence repeats a terminal host result"
            )
        results_by_key[key] = result
    if set(results_by_key) != set(requirement_keys):
        raise ProductEvidenceError(
            "product evidence terminal result set is incomplete or unexpected"
        )

    receipts = []
    receipt_plans = []
    for requirement in checked_requirements:
        key = (requirement["host"], requirement["id"])
        result = results_by_key[key]
        receipt = build_product_evidence_receipt(
            result,
            request_digest=request_digest,
            product=product,
            candidate_product=candidate_product,
            runtime_bundle_body=runtime_bundle_body,
            requirement=requirement,
        )
        receipts.append(receipt)
        receipt_plans.append(
            build_product_evidence_receipt_oci_plan(
                receipt,
                result=result,
                candidate_product=candidate_product,
            )
        )

    record = build_product_evidence_record(
        request_digest=request_digest,
        candidate_product=candidate_product,
        resolved_inputs_body=resolved_inputs_body,
        builder_report_body=builder_report_body,
        runtime_bundle_body=runtime_bundle_body,
        selecting_registries=selecting_registries,
        requirements=checked_requirements,
        receipts=receipts,
        run=run,
    )
    aggregate_plan = build_product_evidence_oci_plan(
        record,
        candidate_product=candidate_product,
        receipts=receipts,
        runtime_bundle_body=runtime_bundle_body,
        resolved_inputs_body=resolved_inputs_body,
    )

    receipt_locators = [
        publish_product_evidence_receipt(
            plan,
            transport=transport,
            expected_source_repository=expected_source_repository,
        )
        for plan in receipt_plans
    ]
    record_locator = publish_product_evidence_record(
        aggregate_plan,
        transport=transport,
        expected_source_repository=expected_source_repository,
    )
    return {
        "receipts": receipts,
        "receipt_locators": receipt_locators,
        "record": record,
        "record_locator": record_locator,
    }
