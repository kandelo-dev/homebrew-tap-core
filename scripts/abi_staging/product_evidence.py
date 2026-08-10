"""Publish immutable candidate VFS identity and separate factual evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re
from typing import Any

from .canonical import (
    CanonicalJsonError,
    canonical_bytes,
    canonical_sha256,
    parse_canonical_bytes,
)
from .oci import OciTransportV1, PublishedRecordLocatorV1, publish_record
from .plan import PlanError, parse_formula_subject
from .product import (
    ProductInputPlanV1,
    ProductInputResolutionError,
    load_resolved_product_inputs,
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

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_RUNTIME_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_FILES = 32_768
MAX_RUNTIME_BYTES = 8 * 1024 * 1024 * 1024
MAX_RESULT_DIAGNOSTICS = 64
MAX_DIAGNOSTIC_BYTES = 64 * 1024
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


def _validate_runtime_bundle(
    body: bytes,
    *,
    expected_inputs: Mapping[str, Any] | None = None,
    runtime_files: Mapping[str, bytes] | None = None,
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
        frozenset({"bundle_sha256", "bytes", "service_worker_sha256"}),
        "runtime browser",
    )
    browser_sha = _digest(browser["bundle_sha256"], "runtime browser bundle")
    browser_bytes = _integer(browser["bytes"], "runtime browser bytes", positive=True)
    service_worker_sha = _digest(
        browser["service_worker_sha256"], "runtime service worker"
    )
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
                item["bytes"], f"runtime inventory {path} bytes", positive=True
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
    if runtime_files is not None:
        if set(runtime_files) != set(checked_inventory):
            raise ProductEvidenceError(
                "runtime file handoff differs from the exact runtime inventory"
            )
        for path, expected in checked_inventory.items():
            body_at_path = runtime_files[path]
            if not isinstance(body_at_path, bytes) or not body_at_path:
                raise ProductEvidenceError(f"runtime file {path} is empty or not bytes")
            if (
                len(body_at_path) != expected["bytes"]
                or hashlib.sha256(body_at_path).hexdigest() != expected["sha256"]
            ):
                raise ProductEvidenceError(f"runtime file {path} differs from its inventory")
    return runtime


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
    runtime_files: Mapping[str, bytes],
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
    record = _load_canonical(plan.config.body, "candidate product record")
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
    _validate_source(record["source"], "candidate product source")
    _validate_product_repository(
        plan.repository, product["id"], target_abi
    )
    artifacts = _exact(
        record["artifacts"],
        frozenset(
            {
                "vfs_image",
                "builder_report",
                "resolved_inputs",
                "runtime_bundle",
            }
        ),
        "candidate product artifacts",
    )
    for key in ("vfs_image", "builder_report", "resolved_inputs", "runtime_bundle"):
        _artifact(artifacts[key], f"candidate product {key}")
    expected_roles = ["vfs-image", "builder-report", "resolved-inputs", "runtime-bundle"]
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
        frozenset({"bundle_sha256", "bytes", "service_worker_sha256"}),
        f"{field} browser",
    )
    _digest(browser["bundle_sha256"], f"{field} browser bundle")
    _integer(browser["bytes"], f"{field} browser bytes", positive=True)
    _digest(browser["service_worker_sha256"], f"{field} service worker")
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
