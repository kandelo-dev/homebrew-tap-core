"""Resolve request-selected VFS products from exact, nonendorsed inputs."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Literal
import zipfile

from .canonical import CanonicalJsonError, canonical_bytes, canonical_sha256, parse_canonical_bytes
from .contract import validate_candidate_reuse_record
from .custody import CustodyError, load_source_custody_manifest
from .oci import OciTransportV1, fetch_public_blob
from .plan import exact_formula_subject, validate_tap_plan
from .policy import VerificationTestDefinitionV1
from .records import validate_candidate_record
from .scheduler import CandidateFactV1, SchedulingRecordsV1, VerificationFactV1
from .verification import validate_verification_receipt_record


SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
CANDIDATE_NAMESPACE = re.compile(
    r"^(?:https://)?ghcr\.io/kandelo-dev/homebrew-tap-core-abi-([0-9]+)-candidates/"
)
CANONICAL_NAMESPACE = re.compile(
    r"^(?:https://)?ghcr\.io/kandelo-dev/homebrew-tap-core-abi-([0-9]+)/"
)
ARCHITECTURES = frozenset({"wasm32", "wasm64"})
INPUT_KINDS = frozenset(
    {
        "product-image",
        "homebrew-bottle",
        "package-output",
        "source-archive",
        "toolchain-output",
        "repository-path",
    }
)
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_INPUTS = 4_096
MAX_PRODUCT_HANDOFF_FILES = MAX_INPUTS + 16
MAX_PRODUCT_HANDOFF_JSON_BYTES = 4 * 1024 * 1024
MAX_INPUT_OBJECT_BYTES = 2 * 1024 * 1024 * 1024
MAX_DIRECTORY_INPUT_BYTES = 512 * 1024 * 1024
MAX_DIRECTORY_INPUT_ENTRIES = 100_000
MAX_REPOSITORY_BUNDLE_BYTES = 256 * 1024 * 1024
NONPUBLIC_PRODUCT_INPUT_KINDS = frozenset(
    {"package-output", "source-archive", "toolchain-output", "repository-path"}
)
PRODUCT_BUILD_OUTCOMES = frozenset({"success", "blocked", "failure"})
PRODUCT_BUILD_GUARDS = frozenset(
    {
        "product_inputs_unavailable",
        "product_dependency_unavailable",
        "product_builder_failed",
        "product_builder_timeout",
        "product_integrity_mismatch",
    }
)
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


class ProductInputResolutionError(ValueError):
    """Raised when selected product inputs are absent, ambiguous, or stale."""


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ProductInputResolutionError(f"{field} must be a lowercase SHA-256")
    return value


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise ProductInputResolutionError(f"{field} must be a full lowercase Git SHA")
    return value


def _stable_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or STABLE_ID.fullmatch(value) is None:
        raise ProductInputResolutionError(f"{field} is not a stable identifier")
    return value


def _repository(value: Any, field: str) -> str:
    if not isinstance(value, str) or REPOSITORY.fullmatch(value) is None:
        raise ProductInputResolutionError(f"{field} is not owner/repository")
    return value


def _architecture(value: Any, field: str) -> str:
    if value not in ARCHITECTURES:
        raise ProductInputResolutionError(f"{field} is not a supported architecture")
    return value


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 2**53 - 1:
        raise ProductInputResolutionError(f"{field} is outside its integer bound")
    return value


def _text(value: Any, field: str, maximum: int = 4_096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8", errors="strict")) > maximum
        or "\0" in value
    ):
        raise ProductInputResolutionError(f"{field} is outside its UTF-8 bound")
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProductInputResolutionError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ProductInputResolutionError(f"{field} must be an array")
    return value


def _exact(value: Any, keys: frozenset[str], field: str) -> Mapping[str, Any]:
    result = _mapping(value, field)
    if frozenset(result) != keys:
        raise ProductInputResolutionError(f"{field} fields changed")
    return result


def _relative_path(value: Any, field: str) -> str:
    result = _text(value, field)
    if (
        result.startswith("/")
        or "\\" in result
        or any(part in {"", ".", ".."} for part in result.split("/"))
    ):
        raise ProductInputResolutionError(f"{field} is not a normalized relative path")
    return result


def _source(value: Any, field: str) -> dict[str, str]:
    source = _exact(value, frozenset({"repository", "commit", "tree"}), field)
    return {
        "repository": _repository(source["repository"], f"{field} repository"),
        "commit": _git_sha(source["commit"], f"{field} commit"),
        "tree": _git_sha(source["tree"], f"{field} tree"),
    }


def _immutable_reference(
    value: Any,
    sha256: str,
    field: str,
    *,
    kind: str | None = None,
    target_abi: int | None = None,
    require_candidate: bool = False,
) -> str:
    reference = _text(value, field)
    if any(character.isspace() for character in reference) or (
        f"sha256:{sha256}" not in reference and f"sha256={sha256}" not in reference
    ):
        raise ProductInputResolutionError(f"{field} does not bind its exact SHA-256")
    canonical = CANONICAL_NAMESPACE.search(reference)
    candidate = CANDIDATE_NAMESPACE.search(reference)
    if canonical is not None:
        raise ProductInputResolutionError(f"{field} enters the canonical namespace")
    if kind in {"homebrew-bottle", "product-image"} or require_candidate:
        if candidate is None:
            raise ProductInputResolutionError(f"{field} is not in the candidate namespace")
        if target_abi is None or int(candidate.group(1)) != target_abi:
            raise ProductInputResolutionError(f"{field} candidate namespace has the wrong ABI")
    return reference


def _artifact_fields(instance: Any, field: str) -> None:
    _digest(instance.sha256, f"{field} SHA-256")
    _integer(instance.bytes, f"{field} bytes", positive=True)
    _immutable_reference(instance.immutable_reference, instance.sha256, f"{field} reference")


@dataclass(frozen=True)
class CandidateProductArtifactV1:
    product_id: str
    manifest_sha256: str
    architecture: Literal["wasm32", "wasm64"]
    request_sha256: str
    source_repository: str
    source_commit: str
    source_tree: str
    target_abi: int
    snapshot_sha256: str
    vfs_layer_sha256: str
    vfs_layer_bytes: int
    immutable_reference: str
    builder_report_sha256: str

    def __post_init__(self) -> None:
        _stable_id(self.product_id, "candidate product ID")
        _digest(self.manifest_sha256, "candidate product manifest")
        _architecture(self.architecture, "candidate product architecture")
        _digest(self.request_sha256, "candidate product request")
        _repository(self.source_repository, "candidate product source repository")
        _git_sha(self.source_commit, "candidate product source commit")
        _git_sha(self.source_tree, "candidate product source tree")
        _integer(self.target_abi, "candidate product ABI")
        _digest(self.snapshot_sha256, "candidate product snapshot")
        _digest(self.vfs_layer_sha256, "candidate product layer")
        _integer(self.vfs_layer_bytes, "candidate product bytes", positive=True)
        _digest(self.builder_report_sha256, "candidate product builder report")
        _immutable_reference(
            self.immutable_reference,
            self.vfs_layer_sha256,
            "candidate product reference",
            kind="product-image",
            target_abi=self.target_abi,
        )


@dataclass(frozen=True)
class PackageArtifactV1:
    package: str
    selector_kind: Literal["output", "source-role"]
    selector: str
    architecture: Literal["wasm32", "wasm64"]
    target_abi: int
    snapshot_sha256: str
    source_repository: str
    source_commit: str
    source_tree: str
    build_policy_sha256: str
    sha256: str
    bytes: int
    immutable_reference: str

    def __post_init__(self) -> None:
        _stable_id(self.package, "package artifact package")
        if self.selector_kind not in {"output", "source-role"}:
            raise ProductInputResolutionError("package artifact selector kind is unsupported")
        _stable_id(self.selector, "package artifact selector")
        _architecture(self.architecture, "package artifact architecture")
        _integer(self.target_abi, "package artifact ABI")
        _digest(self.snapshot_sha256, "package artifact snapshot")
        _repository(self.source_repository, "package artifact source repository")
        _git_sha(self.source_commit, "package artifact source commit")
        _git_sha(self.source_tree, "package artifact source tree")
        _digest(self.build_policy_sha256, "package artifact build policy")
        _artifact_fields(self, "package artifact")


@dataclass(frozen=True)
class ArchiveArtifactV1:
    product_id: str
    id: str
    url: str
    sha256: str
    bytes: int
    immutable_reference: str

    def __post_init__(self) -> None:
        _stable_id(self.product_id, "archive product ID")
        _stable_id(self.id, "archive artifact ID")
        if not _text(self.url, "archive URL", 8_192).startswith("https://"):
            raise ProductInputResolutionError("archive URL must use HTTPS")
        _artifact_fields(self, "archive artifact")


@dataclass(frozen=True)
class ToolchainArtifactV1:
    product_id: str
    id: str
    provider: Literal["repository-dev-shell"]
    component: str
    architecture: Literal["wasm32", "wasm64"]
    source_repository: str
    source_commit: str
    source_tree: str
    dev_shell_lock_sha256: str
    build_policy_sha256: str
    sha256: str
    bytes: int
    immutable_reference: str

    def __post_init__(self) -> None:
        _stable_id(self.product_id, "toolchain product ID")
        _stable_id(self.id, "toolchain artifact ID")
        if self.provider != "repository-dev-shell":
            raise ProductInputResolutionError("toolchain provider is unsupported")
        _stable_id(self.component, "toolchain component")
        _architecture(self.architecture, "toolchain architecture")
        _repository(self.source_repository, "toolchain source repository")
        _git_sha(self.source_commit, "toolchain source commit")
        _git_sha(self.source_tree, "toolchain source tree")
        _digest(self.dev_shell_lock_sha256, "toolchain dev-shell lock")
        _digest(self.build_policy_sha256, "toolchain build policy")
        _artifact_fields(self, "toolchain artifact")


@dataclass(frozen=True)
class RepositoryArtifactV1:
    product_id: str
    id: str
    paths: tuple[str, ...]
    architecture: Literal["wasm32", "wasm64"]
    source_repository: str
    source_commit: str
    source_tree: str
    sha256: str
    bytes: int
    immutable_reference: str

    def __post_init__(self) -> None:
        _stable_id(self.product_id, "repository artifact product ID")
        _stable_id(self.id, "repository artifact ID")
        if not self.paths:
            raise ProductInputResolutionError("repository artifact paths are empty")
        checked = tuple(_relative_path(path, "repository artifact path") for path in self.paths)
        if checked != tuple(sorted(set(checked))):
            raise ProductInputResolutionError("repository artifact paths are not sorted and unique")
        _architecture(self.architecture, "repository artifact architecture")
        _repository(self.source_repository, "repository artifact source repository")
        _git_sha(self.source_commit, "repository artifact source commit")
        _git_sha(self.source_tree, "repository artifact source tree")
        _artifact_fields(self, "repository artifact")


@dataclass(frozen=True)
class ProductInputPlanV1:
    product_id: str
    manifest_path: str
    manifest_sha256: str
    architecture: Literal["wasm32", "wasm64"]
    reference_class: Literal["candidate"]
    resolved_inputs_sha256: str
    dependency_product_ids: tuple[str, ...]
    required_formula_subjects: tuple[str, ...]
    runtime_bundle_sha256: str


@dataclass(frozen=True, order=True)
class ProductInputRequestV1:
    input_id: str
    requesting_product_id: str
    root_kind: str
    root_id: str
    materialization: Literal["embedded", "lazy", "build-only"]


@dataclass(frozen=True)
class ResolvedProductPlanV1:
    plan: ProductInputPlanV1
    resolved_inputs: Mapping[str, Any]
    input_requests: tuple[ProductInputRequestV1, ...]


def select_product_execution_scope(
    request: Mapping[str, Any],
    *,
    request_sha256: str,
    product_id: str,
    work_id: str,
) -> dict[str, Any]:
    """Bind one compose job to the request-selected product and applicability."""

    checked_request = _mapping(request, "product execution request")
    checked_request_sha256 = _digest(
        request_sha256, "product execution request digest"
    )
    if canonical_sha256(checked_request) != checked_request_sha256:
        raise ProductInputResolutionError(
            "product execution request differs from its canonical digest"
        )
    selected_product_id = _stable_id(product_id, "product execution product ID")
    checked_work_id = _digest(work_id, "product execution work ID")
    requirements = _mapping(
        checked_request.get("requirements"), "product execution requirements"
    )
    products: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(
        _sequence(requirements.get("products"), "product execution products")
    ):
        product = _exact(
            value,
            frozenset({"id", "path", "manifest_sha256"}),
            f"product execution product {index}",
        )
        identity = _stable_id(product["id"], f"product execution product {index} ID")
        if identity in products:
            raise ProductInputResolutionError(
                "product execution request repeats a product"
            )
        path = _relative_path(
            product["path"], f"product execution product {identity} path"
        )
        if path != f"images/vfs/products/{identity}.toml":
            raise ProductInputResolutionError(
                "product execution request names a noncanonical manifest path"
            )
        products[identity] = {
            "id": identity,
            "manifest_path": path,
            "manifest_sha256": _digest(
                product["manifest_sha256"],
                f"product execution product {identity} manifest",
            ),
        }
    selected = products.get(selected_product_id)
    if selected is None:
        raise ProductInputResolutionError(
            "product execution product is not selected by the request"
        )
    evidence_matches = []
    for index, value in enumerate(
        _sequence(requirements.get("evidence"), "product execution evidence")
    ):
        evidence = _exact(
            value,
            frozenset({"product_id", "applicability", "node", "browser"}),
            f"product execution evidence {index}",
        )
        if evidence["product_id"] == selected_product_id:
            evidence_matches.append(evidence)
    if len(evidence_matches) != 1:
        raise ProductInputResolutionError(
            "product execution product lacks one selected evidence binding"
        )
    applicability = evidence_matches[0]["applicability"]
    if applicability not in {"required", "informational"}:
        raise ProductInputResolutionError(
            "product execution product is not applicable to this request"
        )
    expected_work_id = canonical_sha256(
        {
            "request_digest": checked_request_sha256,
            "product_id": selected_product_id,
            "manifest_sha256": selected["manifest_sha256"],
            "applicability": applicability,
            "stage": "compose-product",
        }
    )
    if checked_work_id != expected_work_id:
        raise ProductInputResolutionError(
            "product execution work ID differs from its selected product scope"
        )
    return {
        **selected,
        "applicability": applicability,
        "work_id": checked_work_id,
    }


@dataclass(frozen=True)
class _ClaimV1:
    product_id: str
    input_id: str
    kind: str
    role: str
    architecture: str
    declared: str
    sha256: str
    bytes: int
    reference: str
    requests: tuple[ProductInputRequestV1, ...]
    globally_embedded: bool = False
    descriptor_sha256: str | None = None
    descriptor_bytes: int | None = None
    descriptor_reference: str | None = None

    @property
    def object_key(self) -> tuple[str, str, int]:
        return (self.kind, self.sha256, self.bytes)


def _resolved_input_id(kind: str, *parts: str) -> str:
    prefixes = {
        "product-image": "product",
        "homebrew-bottle": "homebrew",
        "package-output": "package",
        "source-archive": "archive",
        "toolchain-output": "toolchain",
        "repository-path": "repository",
    }
    if kind not in prefixes or not parts:
        raise ProductInputResolutionError("resolved input ID kind is unsupported")
    for part in parts:
        _stable_id(part, "resolved input identity component")
    stem = "-".join((prefixes[kind], *parts))
    if len(stem.encode()) <= 128:
        return stem
    identity = canonical_bytes({"kind": kind, "parts": list(parts)})
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    maximum_prefix = 128 - len(suffix) - 1
    shortened = stem[:maximum_prefix].rstrip("-._")
    return f"{shortened}-{suffix}"


def _materialization(role: Any, value: Any, field: str) -> str:
    if role == "build":
        if value is not None:
            raise ProductInputResolutionError(f"{field} build input declares materialization")
        return "build-only"
    if role != "runtime" or value not in {"embedded", "lazy"}:
        raise ProductInputResolutionError(f"{field} role/materialization is invalid")
    return value


def _validate_manifest_shape(manifest: Mapping[str, Any], field: str) -> None:
    required = frozenset(
        {
            "schema",
            "id",
            "architecture",
            "output",
            "builder",
            "composition",
            "software",
            "mounts",
            "evidence",
        }
    )
    if not required.issubset(manifest) or not frozenset(manifest).issubset(
        required | frozenset({"boot"})
    ):
        raise ProductInputResolutionError(f"{field} fields changed")
    if manifest["schema"] != 1:
        raise ProductInputResolutionError(f"{field} schema is unsupported")
    _stable_id(manifest["id"], f"{field} ID")
    _architecture(manifest["architecture"], f"{field} architecture")
    _text(manifest["output"], f"{field} output", 255)
    _relative_path(manifest["builder"], f"{field} builder")
    composition = _exact(
        manifest["composition"], frozenset({"product", "repository"}), f"{field} composition"
    )
    software = _exact(
        manifest["software"],
        frozenset({"homebrew", "package", "archive", "toolchain"}),
        f"{field} software",
    )
    for key in ("product", "repository"):
        _sequence(composition[key], f"{field} composition {key}")
    for key in ("homebrew", "package", "archive", "toolchain"):
        _sequence(software[key], f"{field} software {key}")


def _catalog_entries(catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    root = _exact(catalog, frozenset({"schema", "kind", "products"}), "product catalog")
    if root["schema"] != 1 or root["kind"] != "kandelo-vfs-product-catalog":
        raise ProductInputResolutionError("product catalog protocol is unsupported")
    entries: dict[str, Mapping[str, Any]] = {}
    previous = ""
    for index, value in enumerate(_sequence(root["products"], "catalog products")):
        entry = _exact(
            value,
            frozenset({"path", "sha256", "manifest"}),
            f"catalog product {index}",
        )
        path = _relative_path(entry["path"], f"catalog product {index} path")
        digest = _digest(entry["sha256"], f"catalog product {index} digest")
        manifest = _mapping(entry["manifest"], f"catalog product {index} manifest")
        _validate_manifest_shape(manifest, f"catalog product {index} manifest")
        product_id = manifest["id"]
        if product_id <= previous:
            raise ProductInputResolutionError("catalog products must be sorted and duplicate-free")
        previous = product_id
        if path != f"images/vfs/products/{product_id}.toml":
            raise ProductInputResolutionError("catalog product path differs from its product ID")
        if canonical_sha256(manifest) != digest:
            raise ProductInputResolutionError("catalog product manifest digest is stale")
        entries[product_id] = entry
    if not entries:
        raise ProductInputResolutionError("product catalog is empty")
    return entries


def _selected_entries(
    request: Mapping[str, Any], entries: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    requirements = _mapping(request.get("requirements"), "request requirements")
    required_requirement_fields = frozenset(
        {"digest", "change_classes", "products", "registries", "evidence"}
    )
    if frozenset(requirements) != required_requirement_fields:
        raise ProductInputResolutionError("request requirements fields changed")
    requirements_digest = _digest(requirements["digest"], "request requirements digest")
    requirements_identity = {
        key: requirements[key]
        for key in ("change_classes", "products", "registries", "evidence")
    }
    if canonical_sha256(requirements_identity) != requirements_digest:
        raise ProductInputResolutionError("request requirements digest is stale")
    products = _sequence(requirements.get("products"), "request selected products")
    selected: dict[str, Mapping[str, Any]] = {}
    paths: dict[str, str] = {}
    previous = ""
    for index, value in enumerate(products):
        binding = _exact(
            value,
            frozenset({"id", "path", "manifest_sha256"}),
            f"request product {index}",
        )
        product_id = _stable_id(binding["id"], f"request product {index} ID")
        if product_id <= previous:
            raise ProductInputResolutionError("request products are not sorted and unique")
        previous = product_id
        entry = entries.get(product_id)
        if entry is None:
            raise ProductInputResolutionError("request names a product absent from the catalog")
        path = _relative_path(binding["path"], f"request product {product_id} path")
        manifest_sha256 = _digest(
            binding["manifest_sha256"], f"request product {product_id} manifest"
        )
        if path != entry["path"] or manifest_sha256 != entry["sha256"]:
            raise ProductInputResolutionError(
                f"request product {product_id} manifest binding differs from the catalog"
            )
        selected[product_id] = entry["manifest"]
        paths[product_id] = path
    if not selected:
        raise ProductInputResolutionError("request selects no VFS products")
    return selected, paths


def select_product_input_build_spec(
    request: Mapping[str, Any],
    catalog: Mapping[str, Any],
    product_id: str,
) -> dict[str, Any]:
    """Derive physical build-source needs from one selected canonical manifest."""

    selected_id = _stable_id(product_id, "product input build product ID")
    entries = _catalog_entries(catalog)
    selected, paths = _selected_entries(request, entries)
    manifest = selected.get(selected_id)
    if manifest is None:
        raise ProductInputResolutionError(
            "product input build product is not selected by the request"
        )
    # Reuse the protected private-authority derivation so a manifest with a
    # colliding selector or contradictory role cannot reach command planning.
    _manifest_private_input_authority(manifest)
    packages = []
    seen_packages: set[str] = set()
    for index, value in enumerate(
        _sequence(manifest["software"]["package"], "product build packages")
    ):
        candidate = _mapping(value, f"product build package {index}")
        keys = frozenset({"name", "outputs", "source_roles", "role"}) | (
            frozenset({"materialization"})
            if "materialization" in candidate
            else frozenset()
        )
        package = _exact(candidate, keys, f"product build package {index}")
        name = _stable_id(package["name"], f"product build package {index} name")
        if name in seen_packages:
            raise ProductInputResolutionError(
                "product build packages repeat a package recipe"
            )
        seen_packages.add(name)
        outputs = sorted(
            _stable_id(item, f"product build package {name} output")
            for item in _sequence(
                package["outputs"], f"product build package {name} outputs"
            )
        )
        source_roles = sorted(
            _stable_id(item, f"product build package {name} source role")
            for item in _sequence(
                package["source_roles"],
                f"product build package {name} source roles",
            )
        )
        if outputs != sorted(set(outputs)) or source_roles != sorted(
            set(source_roles)
        ):
            raise ProductInputResolutionError(
                "product build package selectors are not duplicate-free"
            )
        packages.append(
            {"name": name, "outputs": outputs, "source_roles": source_roles}
        )
    packages.sort(key=lambda item: item["name"])

    archives = []
    for index, value in enumerate(
        _sequence(manifest["software"]["archive"], "product build archives")
    ):
        archive = _mapping(value, f"product build archive {index}")
        archive_id = _stable_id(
            archive.get("id"), f"product build archive {index} ID"
        )
        url = _text(archive.get("url"), f"product build archive {archive_id} URL")
        if not url.startswith("https://"):
            raise ProductInputResolutionError(
                "product build archive URL must use HTTPS"
            )
        archives.append(
            {
                "id": archive_id,
                "sha256": _digest(
                    archive.get("sha256"),
                    f"product build archive {archive_id} digest",
                ),
                "url": url,
            }
        )
    archives.sort(key=lambda item: item["id"])
    if len({item["id"] for item in archives}) != len(archives):
        raise ProductInputResolutionError("product build archives repeat an ID")

    dependency_product_ids = sorted(
        _stable_id(value["id"], "product build dependency ID")
        for value in _sequence(
            manifest["composition"]["product"], "product build dependencies"
        )
    )
    if dependency_product_ids != sorted(set(dependency_product_ids)):
        raise ProductInputResolutionError(
            "product build dependencies are not duplicate-free"
        )
    return {
        "id": selected_id,
        "manifest_path": paths[selected_id],
        "manifest_sha256": canonical_sha256(manifest),
        "architecture": manifest["architecture"],
        "output": manifest["output"],
        "builder": manifest["builder"],
        "dependency_product_ids": dependency_product_ids,
        "formula_roots": _manifest_formula_roots(manifest),
        "packages": packages,
        "archives": archives,
    }


def _topological_products(selected: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
    state: dict[str, int] = {}
    order: list[str] = []

    def visit(product_id: str) -> None:
        if state.get(product_id) == 1:
            raise ProductInputResolutionError("selected product graph contains a cycle")
        if state.get(product_id) == 2:
            return
        state[product_id] = 1
        manifest = selected[product_id]
        dependencies = []
        for value in _sequence(
            manifest["composition"]["product"], f"product {product_id} dependencies"
        ):
            edge = _exact(value, frozenset({"id", "materialization"}), "product edge")
            dependency = _stable_id(edge["id"], "product dependency ID")
            if edge["materialization"] not in {"embedded", "lazy"}:
                raise ProductInputResolutionError("product edge materialization is invalid")
            if dependency not in selected:
                raise ProductInputResolutionError(
                    f"selected product {product_id} depends on unselected product {dependency}"
                )
            if selected[dependency]["architecture"] != manifest["architecture"]:
                raise ProductInputResolutionError("product dependency architecture differs")
            dependencies.append(dependency)
        if len(dependencies) != len(set(dependencies)):
            raise ProductInputResolutionError("product dependency is repeated")
        for dependency in sorted(dependencies):
            visit(dependency)
        state[product_id] = 2
        order.append(product_id)

    for product_id in sorted(selected):
        visit(product_id)
    return tuple(order)


def _manifest_formula_roots(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    roots = []
    seen = set()
    for value in _sequence(manifest["software"]["homebrew"], "Homebrew groups"):
        group = _exact(
            value, frozenset({"tap", "formulae", "materialization"}), "Homebrew group"
        )
        tap = _repository(group["tap"], "Homebrew tap")
        materialization = group["materialization"]
        if materialization not in {"embedded", "lazy"}:
            raise ProductInputResolutionError("Homebrew materialization is invalid")
        for formula_value in _sequence(group["formulae"], "Homebrew Formulae"):
            formula = _stable_id(formula_value, "Homebrew Formula")
            key = (tap, formula)
            if key in seen:
                raise ProductInputResolutionError("product repeats a Homebrew Formula root")
            seen.add(key)
            roots.append(
                {
                    "tap": tap,
                    "formula": formula,
                    "architecture": manifest["architecture"],
                    "materialization": materialization,
                }
            )
    roots.sort(key=lambda item: (item["tap"], item["formula"], item["architecture"], item["materialization"]))
    return roots


def _validate_tap_binding(
    tap_plan: Mapping[str, Any],
    request: Mapping[str, Any],
    request_sha256: str,
    selected: Mapping[str, Mapping[str, Any]],
    paths: Mapping[str, str],
) -> None:
    try:
        validate_tap_plan(tap_plan)
    except ValueError as error:
        raise ProductInputResolutionError(f"tap plan is invalid: {error}") from error
    if tap_plan["request_digest"] != request_sha256:
        raise ProductInputResolutionError("tap plan names another request")
    if tap_plan["target_abi"] != request["target_abi"]:
        raise ProductInputResolutionError("tap plan target ABI differs from the request")
    planned = {item["id"]: item for item in tap_plan["selected_products"]}
    if set(planned) != set(selected):
        raise ProductInputResolutionError("tap plan products differ from request selection")
    for product_id, manifest in selected.items():
        item = planned[product_id]
        if (
            item["path"] != paths[product_id]
            or item["manifest_sha256"] != canonical_sha256(manifest)
            or item["formula_roots"] != _manifest_formula_roots(manifest)
        ):
            raise ProductInputResolutionError(
                f"tap plan Formula roots differ from selected manifest {product_id}"
            )


def _runtime_identity(
    runtime: Mapping[str, Any], request: Mapping[str, Any]
) -> tuple[str, str]:
    root = _exact(
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
    if root["schema"] != 1 or root["kind"] != "kandelo-exact-runtime-bundle":
        raise ProductInputResolutionError("exact runtime bundle protocol is unsupported")
    if _source(root["source"], "runtime source") != _source(
        request["build_source"], "request build source"
    ):
        raise ProductInputResolutionError("runtime bundle source differs from exact request")
    target = _exact(root["target_abi"], frozenset({"version", "snapshot_sha256"}), "runtime ABI")
    if target != request["target_abi"]:
        raise ProductInputResolutionError("runtime bundle ABI differs from exact request")
    version = _integer(target["version"], "runtime ABI version")
    snapshot = _digest(target["snapshot_sha256"], "runtime ABI snapshot")
    kernel = _exact(
        root["kernel"],
        frozenset({"wasm_sha256", "bytes", "abi_version", "snapshot_sha256"}),
        "runtime kernel",
    )
    _digest(kernel["wasm_sha256"], "runtime kernel Wasm")
    _integer(kernel["bytes"], "runtime kernel bytes", positive=True)
    if kernel["abi_version"] != version or kernel["snapshot_sha256"] != snapshot:
        raise ProductInputResolutionError("runtime kernel ABI identity differs")
    host = _exact(
        root["host"],
        frozenset({"bundle_sha256", "bytes", "generated_abi_sha256", "worker_protocol_sha256"}),
        "runtime host",
    )
    browser = _exact(
        root["browser"],
        BROWSER_RUNTIME_KEYS,
        "runtime browser",
    )
    for field in ("bundle_sha256", "generated_abi_sha256", "worker_protocol_sha256"):
        _digest(host[field], f"runtime host {field}")
    _integer(host["bytes"], "runtime host bytes", positive=True)
    for field in (
        "bundle_sha256",
        "harness_entry_sha256",
        "host_entry_sha256",
        "kernel_asset_sha256",
        "service_worker_sha256",
    ):
        _digest(browser[field], f"runtime browser {field}")
    _integer(browser["bytes"], "runtime browser bytes", positive=True)
    for field in ("harness_entry_bytes", "host_entry_bytes"):
        _integer(browser[field], f"runtime browser {field}", positive=True)
    browser_paths = {
        field: _relative_path(browser[field], f"runtime browser {field}")
        for field in (
            "harness_entry_path",
            "host_entry_path",
            "kernel_asset_path",
        )
    }
    if (
        browser_paths["harness_entry_path"]
        != "browser/dist/abi-staging-harness/index.html"
        or browser_paths["host_entry_path"]
        != "browser/dist/abi-staging/browser-host.js"
        or not browser_paths["kernel_asset_path"].startswith("browser/dist/")
        or not browser_paths["kernel_asset_path"].endswith(".wasm")
        or browser["kernel_asset_sha256"] != kernel["wasm_sha256"]
    ):
        raise ProductInputResolutionError("runtime browser entry identity differs")
    policy_sha256 = _digest(root["build_policy_sha256"], "runtime build policy")
    dev_shell = []
    inventory: dict[str, dict[str, Any]] = {}
    previous = ""
    for value in _sequence(root["inventory"], "runtime inventory"):
        item = _exact(value, frozenset({"path", "sha256", "bytes"}), "runtime inventory item")
        path = _relative_path(item["path"], "runtime inventory path")
        if path <= previous:
            raise ProductInputResolutionError("runtime inventory is not sorted and unique")
        previous = path
        digest = _digest(item["sha256"], "runtime inventory digest")
        size = _integer(
            item["bytes"],
            "runtime inventory bytes",
            positive=not path.startswith("toolchain/"),
        )
        inventory[path] = {"bytes": size, "path": path, "sha256": digest}
        if path == "flake.lock":
            dev_shell.append(digest)
    if len(dev_shell) != 1:
        raise ProductInputResolutionError("runtime inventory lacks one exact flake.lock")

    def exact_file(path: str, digest: str, size: int | None, field: str) -> None:
        item = inventory.get(path)
        if item is None or item["sha256"] != digest:
            raise ProductInputResolutionError(f"runtime inventory lacks exact {field}")
        if size is not None and item["bytes"] != size:
            raise ProductInputResolutionError(f"runtime inventory lacks exact {field}")

    exact_file(
        browser_paths["harness_entry_path"],
        browser["harness_entry_sha256"],
        browser["harness_entry_bytes"],
        "browser harness entry",
    )
    exact_file(
        browser_paths["host_entry_path"],
        browser["host_entry_sha256"],
        browser["host_entry_bytes"],
        "browser host entry",
    )
    exact_file(
        browser_paths["kernel_asset_path"],
        browser["kernel_asset_sha256"],
        None,
        "browser kernel asset",
    )
    exact_file(
        "browser/dist/service-worker.js",
        browser["service_worker_sha256"],
        None,
        "browser service worker",
    )
    browser_inventory = [
        inventory[path] for path in sorted(inventory) if path.startswith("browser/")
    ]
    if (
        canonical_sha256(browser_inventory) != browser["bundle_sha256"]
        or sum(item["bytes"] for item in browser_inventory) != browser["bytes"]
    ):
        raise ProductInputResolutionError(
            "runtime browser bundle identity differs from inventory"
        )
    return policy_sha256, dev_shell[0]


def product_runtime_identity(
    runtime: Mapping[str, Any], request: Mapping[str, Any]
) -> dict[str, str]:
    """Expose the one validated build-policy/dev-shell identity to orchestration."""

    policy_sha256, dev_shell_lock_sha256 = _runtime_identity(runtime, request)
    return {
        "policy_sha256": policy_sha256,
        "dev_shell_lock_sha256": dev_shell_lock_sha256,
    }


def _unique_index(values: Sequence[Any], key: Any, field: str) -> dict[Any, Any]:
    result = {}
    for value in values:
        identity = key(value)
        if identity in result:
            raise ProductInputResolutionError(f"{field} repeats an exact identity")
        result[identity] = value
    return result


def _formula_graph(tap_plan: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, tuple[str, ...]]]:
    formulae = {}
    dependencies = {}
    for value in tap_plan["formulae"]:
        identity = value["identity"]
        subject = exact_formula_subject(identity["name"], identity["architecture"])
        formulae[subject] = value
    for subject, value in formulae.items():
        dependencies[subject] = tuple(
            exact_formula_subject(item["formula"], item["architecture"])
            for item in value["direct_dependencies"]
        )
        if any(dependency not in formulae for dependency in dependencies[subject]):
            raise ProductInputResolutionError("tap plan Formula dependency is absent")
    return formulae, dependencies


def _formula_uses(
    selected: Mapping[str, Mapping[str, Any]],
    formulae: Mapping[str, Mapping[str, Any]],
    dependencies: Mapping[str, tuple[str, ...]],
) -> tuple[
    dict[str, dict[str, list[ProductInputRequestV1]]],
    dict[str, tuple[ProductInputRequestV1, ...]],
]:
    per_product: dict[str, dict[str, list[ProductInputRequestV1]]] = {
        product_id: {} for product_id in selected
    }
    global_uses: dict[str, set[ProductInputRequestV1]] = {}

    def propagate(subject: str, request: ProductInputRequestV1) -> None:
        per_product[request.requesting_product_id].setdefault(subject, []).append(request)
        global_uses.setdefault(subject, set()).add(request)
        for dependency in dependencies[subject]:
            propagate(dependency, request)

    for product_id, manifest in selected.items():
        for root in _manifest_formula_roots(manifest):
            subject = exact_formula_subject(root["formula"], root["architecture"])
            if subject not in formulae:
                raise ProductInputResolutionError(
                    f"selected manifest Formula {root['formula']} is absent from tap plan"
                )
            input_id = _resolved_input_id("homebrew-bottle", root["formula"])
            propagate(
                subject,
                ProductInputRequestV1(
                    input_id=input_id,
                    requesting_product_id=product_id,
                    root_kind="formula",
                    root_id=root["formula"],
                    materialization=root["materialization"],
                ),
            )
    normalized = {
        product: {
            subject: sorted(set(requests)) for subject, requests in subjects.items()
        }
        for product, subjects in per_product.items()
    }
    return normalized, {subject: tuple(sorted(requests)) for subject, requests in global_uses.items()}


def _checked_candidate(
    *,
    subject: str,
    formula_plan: Mapping[str, Any],
    request: Mapping[str, Any],
    request_sha256: str,
    records: SchedulingRecordsV1,
    candidate_records: Mapping[str, Mapping[str, Any]],
    candidate_locators: Mapping[str, Mapping[str, str]],
    source_custody_records: Mapping[str, Mapping[str, Any]],
    reuse_records: Mapping[str, Mapping[str, Any]],
    verification_records: Mapping[str, Mapping[str, Any]],
    verification_locators: Mapping[str, Mapping[str, str]],
    verification_tests: tuple[VerificationTestDefinitionV1, ...],
    tap_plan_repository: str,
    tap_source: Mapping[str, str],
    allow_unready: bool = False,
) -> tuple[CandidateFactV1, Mapping[str, Any]] | None:
    contract_sha256 = formula_plan.get("contract_sha256")
    if allow_unready and contract_sha256 is None:
        return None
    _digest(contract_sha256, "selected Formula contract")
    matches = [
        fact
        for fact in records.candidates
        if fact.request_sha256 == request_sha256
        and fact.subject == subject
        and fact.contract_sha256 == contract_sha256
    ]
    if not matches:
        if allow_unready:
            return None
        raise ProductInputResolutionError(f"Formula {subject} has no current candidate")
    if len({fact.bottle_layer_sha256 for fact in matches}) != 1:
        raise ProductInputResolutionError(f"Formula {subject} has conflicting candidate layers")
    fact = min(matches, key=lambda item: (item.record_sha256, item.binding_record_sha256 or ""))
    record = candidate_records.get(fact.record_sha256)
    locator = candidate_locators.get(fact.record_sha256)
    if record is None or locator is None:
        raise ProductInputResolutionError("candidate fact lacks its exact public record")
    try:
        validate_candidate_record(record)
    except ValueError as error:
        raise ProductInputResolutionError(f"candidate record is invalid: {error}") from error
    checked_locator = _exact(
        locator,
        frozenset({"repository", "digest", "immutable_reference"}),
        "candidate locator",
    )
    if checked_locator["digest"] != "sha256:" + fact.record_sha256:
        raise ProductInputResolutionError("candidate locator differs from candidate fact")
    _immutable_reference(
        checked_locator["immutable_reference"],
        fact.record_sha256,
        "candidate record reference",
        kind="homebrew-bottle",
        target_abi=request["target_abi"]["version"],
    )
    payload = record["candidate"]
    formula = payload["formula"]
    planned_identity = formula_plan["identity"]
    expected_formula = {
        "tap": tap_plan_repository,
        "formula": planned_identity["name"],
        "version": planned_identity["version"],
        "revision": planned_identity["revision"],
        "bottle_rebuild": planned_identity["rebuild"],
        "architecture": planned_identity["architecture"],
        "target_abi": request["target_abi"]["version"],
        "bottle_contract_sha256": contract_sha256,
    }
    if formula != expected_formula or payload["bottle_layer"]["sha256"] != fact.bottle_layer_sha256:
        raise ProductInputResolutionError("candidate Formula identity differs from exact plan")
    source = _source(request["build_source"], "request build source")
    if fact.binding_record_sha256 is None:
        if record["common"]["request_sha256"] != request_sha256 or record["common"]["source"] != source:
            raise ProductInputResolutionError("candidate source custody is stale for this request")
    else:
        reuse = reuse_records.get(fact.binding_record_sha256)
        if reuse is None:
            raise ProductInputResolutionError("reused candidate lacks its exact binding record")
        try:
            validate_candidate_reuse_record(reuse)
        except ValueError as error:
            raise ProductInputResolutionError(f"candidate reuse record is invalid: {error}") from error
        binding = reuse["candidate_reuse"]
        if (
            reuse["common"]["request_sha256"] != request_sha256
            or reuse["common"]["source"] != source
            or binding["existing_candidate"]["record_sha256"] != fact.record_sha256
            or binding["bottle_layer"] != payload["bottle_layer"]
            or binding["formula"] != {
                key: expected_formula[key]
                for key in ("tap", "formula", "architecture", "target_abi", "bottle_contract_sha256")
            }
        ):
            raise ProductInputResolutionError("candidate reuse binding differs from current request")
    custody_components = [
        item["artifact"]
        for item in payload["normalized_components"]
        if item["id"] == "source-custody"
    ]
    if len(custody_components) != 1:
        raise ProductInputResolutionError("candidate lacks one source-custody record")
    custody_link = custody_components[0]
    custody = source_custody_records.get(custody_link["sha256"])
    if custody is None:
        raise ProductInputResolutionError("candidate source custody record is unavailable")
    try:
        checked_custody = load_source_custody_manifest(canonical_bytes(custody))
    except CustodyError as error:
        raise ProductInputResolutionError(f"candidate source custody is invalid: {error}") from error
    if checked_custody["subject"] != subject:
        raise ProductInputResolutionError("candidate source custody names another Formula")
    custody_sources = {item["role"]: item for item in checked_custody["sources"]}
    candidate_source = _source(record["common"]["source"], "candidate source")
    if (
        checked_custody["request_sha256"] != record["common"]["request_sha256"]
        or {
            key: custody_sources.get("kandelo", {}).get(key)
            for key in ("repository", "commit", "tree")
        }
        != candidate_source
    ):
        raise ProductInputResolutionError("candidate source custody differs from its producer")
    if fact.binding_record_sha256 is None:
        if (
            checked_custody["request_sha256"] != request_sha256
            or {
                key: custody_sources.get("kandelo", {}).get(key)
                for key in ("repository", "commit", "tree")
            }
            != source
            or {
                key: custody_sources.get("tap", {}).get(key)
                for key in ("repository", "commit", "tree")
            }
            != tap_source
        ):
            raise ProductInputResolutionError("candidate source custody is stale for this request")
    else:
        reuse_custody = reuse_records[fact.binding_record_sha256]["candidate_reuse"][
            "source_custody"
        ]
        if reuse_custody != {
            "record_sha256": custody_link["sha256"],
            "immutable_reference": custody_link["immutable_reference"],
        }:
            raise ProductInputResolutionError("candidate reuse names different source custody")
    layer = payload["bottle_layer"]
    _immutable_reference(
        layer["immutable_reference"],
        layer["sha256"],
        "candidate bottle reference",
        kind="homebrew-bottle",
        target_abi=request["target_abi"]["version"],
    )
    for definition in verification_tests:
        for host in definition.hosts:
            facts = [
                verification
                for verification in records.verifications
                if verification.request_sha256 == request_sha256
                and verification.subject == subject
                and verification.candidate_record_sha256 == fact.record_sha256
                and verification.test_definition_sha256 == definition.sha256
                and verification.host == host
                and verification.outcome == "success"
            ]
            if not facts:
                if allow_unready:
                    return None
                raise ProductInputResolutionError(
                    f"Formula {subject} lacks qualifying {definition.id}/{host} verification"
                )
            verification = min(facts, key=lambda item: item.record_sha256)
            receipt = verification_records.get(verification.record_sha256)
            receipt_locator = verification_locators.get(verification.record_sha256)
            if receipt is None or receipt_locator is None:
                raise ProductInputResolutionError("verification fact lacks its exact receipt")
            try:
                validate_verification_receipt_record(receipt)
            except ValueError as error:
                raise ProductInputResolutionError(f"verification receipt is invalid: {error}") from error
            checked_receipt_locator = _exact(
                receipt_locator,
                frozenset({"repository", "digest", "immutable_reference"}),
                "verification receipt locator",
            )
            if checked_receipt_locator["digest"] != "sha256:" + verification.record_sha256:
                raise ProductInputResolutionError("verification receipt locator differs")
            _immutable_reference(
                checked_receipt_locator["immutable_reference"],
                verification.record_sha256,
                "verification receipt reference",
            )
            verification_payload = receipt["verification"]
            if (
                receipt["common"]["request_sha256"] != request_sha256
                or receipt["common"]["source"] != source
                or receipt["common"]["outcome"] != "success"
                or verification_payload["candidate_record_sha256"] != fact.record_sha256
                or verification_payload["candidate_layer"] != layer
                or verification_payload["test_definition_sha256"] != definition.sha256
                or verification_payload["host"] != host
            ):
                raise ProductInputResolutionError("verification receipt does not qualify candidate")
    return fact, record


def selected_product_formula_readiness(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    catalog: Mapping[str, Any],
    tap_plan: Mapping[str, Any],
    records: SchedulingRecordsV1,
    candidate_records: Mapping[str, Mapping[str, Any]],
    candidate_locators: Mapping[str, Mapping[str, str]],
    source_custody_records: Mapping[str, Mapping[str, Any]],
    reuse_records: Mapping[str, Mapping[str, Any]],
    verification_records: Mapping[str, Mapping[str, Any]],
    verification_locators: Mapping[str, Mapping[str, str]],
    verification_tests: tuple[VerificationTestDefinitionV1, ...],
) -> dict[str, bool]:
    """Project exact verified Formula availability onto selected VFS products."""

    checked_request_sha256 = _digest(request_sha256, "request digest")
    if canonical_sha256(request) != checked_request_sha256:
        raise ProductInputResolutionError(
            "Formula readiness request digest differs from canonical request"
        )
    entries = _catalog_entries(catalog)
    selected, paths = _selected_entries(request, entries)
    _validate_tap_binding(
        tap_plan, request, checked_request_sha256, selected, paths
    )
    formulae, dependencies = _formula_graph(tap_plan)
    product_formula_uses, global_formula_uses = _formula_uses(
        selected, formulae, dependencies
    )
    if global_formula_uses and not verification_tests:
        raise ProductInputResolutionError(
            "selected Formulae lack verification policy"
        )
    subject_ready = {}
    for subject in sorted(global_formula_uses):
        subject_ready[subject] = (
            _checked_candidate(
                subject=subject,
                formula_plan=formulae[subject],
                request=request,
                request_sha256=checked_request_sha256,
                records=records,
                candidate_records=candidate_records,
                candidate_locators=candidate_locators,
                source_custody_records=source_custody_records,
                reuse_records=reuse_records,
                verification_records=verification_records,
                verification_locators=verification_locators,
                verification_tests=verification_tests,
                tap_plan_repository=tap_plan["tap_source"]["repository"],
                tap_source=tap_plan["tap_source"],
                allow_unready=True,
            )
            is not None
        )
    return {
        product_id: all(
            subject_ready[subject]
            for subject in sorted(product_formula_uses[product_id])
        )
        for product_id in sorted(selected)
    }


def _direct_request(
    input_id: str,
    product_id: str,
    root_kind: str,
    root_id: str,
    materialization: str,
) -> tuple[ProductInputRequestV1, ...]:
    return (
        ProductInputRequestV1(
            input_id=input_id,
            requesting_product_id=product_id,
            root_kind=root_kind,
            root_id=root_id,
            materialization=materialization,
        ),
    )


def _claim(
    *,
    product_id: str,
    input_id: str,
    kind: str,
    role: str,
    architecture: str,
    declared: str,
    sha256: str,
    bytes: int,
    reference: str,
    requests: tuple[ProductInputRequestV1, ...],
    globally_embedded: bool = False,
    descriptor_sha256: str | None = None,
    descriptor_bytes: int | None = None,
    descriptor_reference: str | None = None,
) -> _ClaimV1:
    return _ClaimV1(
        product_id=product_id,
        input_id=input_id,
        kind=kind,
        role=role,
        architecture=architecture,
        declared=declared,
        sha256=sha256,
        bytes=bytes,
        reference=reference,
        requests=requests,
        globally_embedded=globally_embedded,
        descriptor_sha256=descriptor_sha256,
        descriptor_bytes=descriptor_bytes,
        descriptor_reference=descriptor_reference,
    )


def _candidate_bottle_metadata(record: Mapping[str, Any]) -> Mapping[str, Any]:
    matches = [
        item["artifact"]
        for item in record["candidate"]["normalized_components"]
        if item["id"] == "bottle-metadata"
    ]
    if len(matches) != 1:
        raise ProductInputResolutionError(
            "candidate lacks one authenticated bottle metadata component"
        )
    metadata = _exact(
        matches[0],
        frozenset({"sha256", "bytes", "immutable_reference"}),
        "candidate bottle metadata",
    )
    _digest(metadata["sha256"], "candidate bottle metadata digest")
    _integer(metadata["bytes"], "candidate bottle metadata bytes", positive=True)
    _immutable_reference(
        metadata["immutable_reference"],
        metadata["sha256"],
        "candidate bottle metadata reference",
    )
    return metadata


def _check_exact_source_artifact(
    artifact: Any,
    source: Mapping[str, str],
    field: str,
) -> None:
    if (
        artifact.source_repository != source["repository"]
        or artifact.source_commit != source["commit"]
        or artifact.source_tree != source["tree"]
    ):
        raise ProductInputResolutionError(f"{field} comes from the wrong source tree")


def resolve_product_inputs(
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    catalog: Mapping[str, Any],
    tap_plan: Mapping[str, Any],
    records: SchedulingRecordsV1,
    candidate_records: Mapping[str, Mapping[str, Any]],
    candidate_locators: Mapping[str, Mapping[str, str]],
    source_custody_records: Mapping[str, Mapping[str, Any]],
    reuse_records: Mapping[str, Mapping[str, Any]],
    verification_records: Mapping[str, Mapping[str, Any]],
    verification_locators: Mapping[str, Mapping[str, str]],
    verification_tests: tuple[VerificationTestDefinitionV1, ...],
    runtime_bundle: Mapping[str, Any],
    product_artifacts: tuple[CandidateProductArtifactV1, ...],
    package_artifacts: tuple[PackageArtifactV1, ...],
    archive_artifacts: tuple[ArchiveArtifactV1, ...],
    toolchain_artifacts: tuple[ToolchainArtifactV1, ...],
    repository_artifacts: tuple[RepositoryArtifactV1, ...],
    target_product_ids: tuple[str, ...] | None = None,
) -> tuple[ResolvedProductPlanV1, ...]:
    """Resolve only selected canonical manifests; extra artifact inventory is inert."""

    checked_request_sha256 = _digest(request_sha256, "request digest")
    if canonical_sha256(request) != checked_request_sha256:
        raise ProductInputResolutionError("request digest differs from canonical request")
    source = _source(request.get("build_source"), "request build source")
    target = _exact(
        request.get("target_abi"), frozenset({"version", "snapshot_sha256"}), "request target ABI"
    )
    target_abi = _integer(target["version"], "request target ABI version")
    target_snapshot = _digest(target["snapshot_sha256"], "request target ABI snapshot")
    entries = _catalog_entries(catalog)
    selected, paths = _selected_entries(request, entries)
    _validate_tap_binding(tap_plan, request, checked_request_sha256, selected, paths)
    order = _topological_products(selected)
    if target_product_ids is None:
        target_order = order
    else:
        if (
            not isinstance(target_product_ids, tuple)
            or target_product_ids != tuple(sorted(set(target_product_ids)))
            or any(product_id not in selected for product_id in target_product_ids)
            or not target_product_ids
        ):
            raise ProductInputResolutionError(
                "target products are not a sorted nonempty selected subset"
            )
        targets = set(target_product_ids)
        target_order = tuple(product_id for product_id in order if product_id in targets)
    build_policy_sha256, dev_shell_lock_sha256 = _runtime_identity(runtime_bundle, request)
    runtime_bundle_sha256 = canonical_sha256(runtime_bundle)
    formulae, dependencies = _formula_graph(tap_plan)
    product_formula_uses, global_formula_uses = _formula_uses(selected, formulae, dependencies)
    if global_formula_uses and not verification_tests:
        raise ProductInputResolutionError("selected Formulae lack verification policy")

    target_formula_subjects = {
        subject
        for product_id in target_order
        for subject in product_formula_uses[product_id]
    }
    selected_candidates = {}
    for subject in sorted(target_formula_subjects):
        selected_candidates[subject] = _checked_candidate(
            subject=subject,
            formula_plan=formulae[subject],
            request=request,
            request_sha256=checked_request_sha256,
            records=records,
            candidate_records=candidate_records,
            candidate_locators=candidate_locators,
            source_custody_records=source_custody_records,
            reuse_records=reuse_records,
            verification_records=verification_records,
            verification_locators=verification_locators,
            verification_tests=verification_tests,
            tap_plan_repository=tap_plan["tap_source"]["repository"],
            tap_source=tap_plan["tap_source"],
        )

    product_index = _unique_index(product_artifacts, lambda item: item.product_id, "product artifacts")
    package_index = _unique_index(
        package_artifacts,
        lambda item: (item.package, item.selector_kind, item.selector, item.architecture),
        "package artifacts",
    )
    archive_index = _unique_index(
        archive_artifacts, lambda item: (item.product_id, item.id), "archive artifacts"
    )
    toolchain_index = _unique_index(
        toolchain_artifacts, lambda item: (item.product_id, item.id), "toolchain artifacts"
    )
    repository_index = _unique_index(
        repository_artifacts, lambda item: (item.product_id, item.id), "repository artifacts"
    )

    package_requests: dict[
        tuple[str, str, str, str], set[ProductInputRequestV1]
    ] = {}
    for selected_product_id, selected_manifest in selected.items():
        selected_architecture = selected_manifest["architecture"]
        for value in selected_manifest["software"]["package"]:
            package = _mapping(value, "package sharing claim")
            name = _stable_id(package.get("name"), "package sharing name")
            role = package.get("role")
            declared = _materialization(
                role,
                package.get("materialization"),
                "package sharing claim",
            )
            selectors = [
                ("output", _stable_id(item, "package sharing output"))
                for item in _sequence(package.get("outputs"), "package sharing outputs")
            ] + [
                ("source-role", _stable_id(item, "package sharing source role"))
                for item in _sequence(
                    package.get("source_roles"), "package sharing source roles"
                )
            ]
            for selector_kind, selector in selectors:
                input_id = _resolved_input_id(
                    "package-output", name, selector_kind, selector
                )
                key = (name, selector_kind, selector, selected_architecture)
                package_requests.setdefault(key, set()).add(
                    ProductInputRequestV1(
                        input_id=input_id,
                        requesting_product_id=selected_product_id,
                        root_kind="package",
                        root_id=f"{name}/{selector_kind}/{selector}",
                        materialization=declared,
                    )
                )

    claims: list[_ClaimV1] = []
    claims_by_product: dict[str, list[_ClaimV1]] = {
        product_id: [] for product_id in target_order
    }
    for product_id in target_order:
        manifest = selected[product_id]
        architecture = manifest["architecture"]

        for value in manifest["composition"]["product"]:
            dependency = value["id"]
            declared = value["materialization"]
            artifact = product_index.get(dependency)
            if artifact is None:
                raise ProductInputResolutionError(
                    f"product {product_id} dependency {dependency} has no candidate product"
                )
            expected = selected[dependency]
            if (
                artifact.manifest_sha256 != canonical_sha256(expected)
                or artifact.architecture != architecture
                or artifact.request_sha256 != checked_request_sha256
                or artifact.target_abi != target_abi
                or artifact.snapshot_sha256 != target_snapshot
            ):
                raise ProductInputResolutionError("candidate product identity differs from request")
            _check_exact_source_artifact(artifact, source, "candidate product")
            input_id = _resolved_input_id("product-image", dependency)
            claim = _claim(
                product_id=product_id,
                input_id=input_id,
                kind="product-image",
                role="runtime",
                architecture=architecture,
                declared=declared,
                sha256=artifact.vfs_layer_sha256,
                bytes=artifact.vfs_layer_bytes,
                reference=artifact.immutable_reference,
                requests=_direct_request(input_id, product_id, "product", dependency, declared),
            )
            claims.append(claim)
            claims_by_product[product_id].append(claim)

        for subject in sorted(product_formula_uses[product_id]):
            _fact, record = selected_candidates[subject]
            formula = record["candidate"]["formula"]["formula"]
            layer = record["candidate"]["bottle_layer"]
            metadata = _candidate_bottle_metadata(record)
            requests = global_formula_uses[subject]
            declared = (
                "embedded"
                if any(
                    request.materialization == "embedded"
                    for request in product_formula_uses[product_id][subject]
                )
                else "lazy"
            )
            input_id = _resolved_input_id("homebrew-bottle", formula)
            claim = _claim(
                product_id=product_id,
                input_id=input_id,
                kind="homebrew-bottle",
                role="runtime",
                architecture=architecture,
                declared=declared,
                sha256=layer["sha256"],
                bytes=layer["bytes"],
                reference=layer["immutable_reference"],
                requests=tuple(
                    ProductInputRequestV1(
                        input_id=input_id,
                        requesting_product_id=item.requesting_product_id,
                        root_kind=item.root_kind,
                        root_id=item.root_id,
                        materialization=item.materialization,
                    )
                    for item in requests
                ),
                globally_embedded=any(
                    item.materialization == "embedded" for item in requests
                ),
                descriptor_sha256=metadata["sha256"],
                descriptor_bytes=metadata["bytes"],
                descriptor_reference=metadata["immutable_reference"],
            )
            claims.append(claim)
            claims_by_product[product_id].append(claim)

        for value in manifest["software"]["package"]:
            package = _exact(
                value,
                frozenset({"name", "outputs", "source_roles", "role"})
                | (frozenset({"materialization"}) if "materialization" in value else frozenset()),
                "package claim",
            )
            name = _stable_id(package["name"], "package name")
            role = package["role"]
            declared = _materialization(role, package.get("materialization"), "package claim")
            selectors = [
                ("output", _stable_id(item, "package output"))
                for item in _sequence(package["outputs"], "package outputs")
            ] + [
                ("source-role", _stable_id(item, "package source role"))
                for item in _sequence(package["source_roles"], "package source roles")
            ]
            if not selectors:
                raise ProductInputResolutionError("package claim has no output or source role")
            for selector_kind, selector in selectors:
                artifact = package_index.get((name, selector_kind, selector, architecture))
                if artifact is None:
                    raise ProductInputResolutionError(
                        f"package {name} {selector_kind} {selector} is unavailable"
                    )
                if (
                    artifact.target_abi != target_abi
                    or artifact.snapshot_sha256 != target_snapshot
                    or artifact.build_policy_sha256 != build_policy_sha256
                ):
                    raise ProductInputResolutionError("package artifact policy or ABI differs")
                _check_exact_source_artifact(artifact, source, "package artifact")
                parts = (name, selector_kind, selector)
                input_id = _resolved_input_id("package-output", *parts)
                root_id = f"{name}/{selector_kind}/{selector}"
                claim = _claim(
                    product_id=product_id,
                    input_id=input_id,
                    kind="package-output",
                    role=role,
                    architecture=architecture,
                    declared=declared,
                    sha256=artifact.sha256,
                    bytes=artifact.bytes,
                    reference=artifact.immutable_reference,
                    requests=tuple(
                        sorted(
                            package_requests[
                                (name, selector_kind, selector, architecture)
                            ]
                        )
                    ),
                    globally_embedded=any(
                        item.materialization == "embedded"
                        for item in package_requests[
                            (name, selector_kind, selector, architecture)
                        ]
                    ),
                )
                claims.append(claim)
                claims_by_product[product_id].append(claim)

        for value in manifest["software"]["archive"]:
            archive = _mapping(value, "archive claim")
            archive_id = _stable_id(archive.get("id"), "archive ID")
            role = archive.get("role")
            declared = _materialization(role, archive.get("materialization"), "archive claim")
            artifact = archive_index.get((product_id, archive_id))
            if artifact is None:
                raise ProductInputResolutionError(f"archive {product_id}/{archive_id} is unavailable")
            if artifact.url != archive.get("url") or artifact.sha256 != archive.get("sha256"):
                raise ProductInputResolutionError("archive SHA or URL differs from manifest")
            input_id = _resolved_input_id("source-archive", archive_id)
            claim = _claim(
                product_id=product_id,
                input_id=input_id,
                kind="source-archive",
                role=role,
                architecture=architecture,
                declared=declared,
                sha256=artifact.sha256,
                bytes=artifact.bytes,
                reference=artifact.immutable_reference,
                requests=_direct_request(input_id, product_id, "archive", archive_id, declared),
            )
            claims.append(claim)
            claims_by_product[product_id].append(claim)

        for value in manifest["software"]["toolchain"]:
            toolchain = _mapping(value, "toolchain claim")
            toolchain_id = _stable_id(toolchain.get("id"), "toolchain ID")
            role = toolchain.get("role")
            declared = _materialization(role, toolchain.get("materialization"), "toolchain claim")
            artifact = toolchain_index.get((product_id, toolchain_id))
            if artifact is None:
                raise ProductInputResolutionError(
                    f"toolchain {product_id}/{toolchain_id} is unavailable"
                )
            if (
                artifact.provider != toolchain.get("provider")
                or artifact.component != toolchain.get("component")
                or artifact.architecture != architecture
                or artifact.dev_shell_lock_sha256 != dev_shell_lock_sha256
                or artifact.build_policy_sha256 != build_policy_sha256
            ):
                raise ProductInputResolutionError("toolchain artifact is ambient or policy-stale")
            _check_exact_source_artifact(artifact, source, "toolchain artifact")
            input_id = _resolved_input_id("toolchain-output", toolchain_id)
            claim = _claim(
                product_id=product_id,
                input_id=input_id,
                kind="toolchain-output",
                role=role,
                architecture=architecture,
                declared=declared,
                sha256=artifact.sha256,
                bytes=artifact.bytes,
                reference=artifact.immutable_reference,
                requests=_direct_request(
                    input_id, product_id, "toolchain", toolchain_id, declared
                ),
            )
            claims.append(claim)
            claims_by_product[product_id].append(claim)

        for value in manifest["composition"]["repository"]:
            repository_claim = _mapping(value, "repository claim")
            repository_id = _stable_id(repository_claim.get("id"), "repository input ID")
            role = repository_claim.get("role")
            declared = _materialization(
                role, repository_claim.get("materialization"), "repository claim"
            )
            declared_paths = tuple(
                _relative_path(path, "repository claim path")
                for path in _sequence(repository_claim.get("paths"), "repository claim paths")
            )
            artifact = repository_index.get((product_id, repository_id))
            if artifact is None:
                raise ProductInputResolutionError(
                    f"repository input {product_id}/{repository_id} is unavailable"
                )
            if artifact.paths != declared_paths or artifact.architecture != architecture:
                raise ProductInputResolutionError("repository artifact differs from manifest")
            _check_exact_source_artifact(artifact, source, "repository artifact")
            input_id = _resolved_input_id("repository-path", repository_id)
            claim = _claim(
                product_id=product_id,
                input_id=input_id,
                kind="repository-path",
                role=role,
                architecture=architecture,
                declared=declared,
                sha256=artifact.sha256,
                bytes=artifact.bytes,
                reference=artifact.immutable_reference,
                requests=_direct_request(
                    input_id, product_id, "repository", repository_id, declared
                ),
            )
            claims.append(claim)
            claims_by_product[product_id].append(claim)

    embedded_objects = {
        claim.object_key
        for claim in claims
        if claim.role == "runtime"
        and (claim.declared == "embedded" or claim.globally_embedded)
    }
    object_requests: dict[tuple[str, str, int], set[ProductInputRequestV1]] = {}
    for claim in claims:
        object_requests.setdefault(claim.object_key, set()).update(claim.requests)

    results = []
    for product_id in target_order:
        manifest = selected[product_id]
        input_values = []
        input_requests = []
        ids = set()
        for claim in sorted(claims_by_product[product_id], key=lambda item: item.input_id):
            if claim.input_id in ids:
                raise ProductInputResolutionError(
                    f"product {product_id} has colliding resolved input IDs"
                )
            ids.add(claim.input_id)
            if claim.role == "build":
                effective = "build-only"
            elif claim.declared == "embedded" or claim.object_key in embedded_objects:
                effective = "embedded"
            else:
                effective = "lazy-reference"
            value = {
                "id": claim.input_id,
                "kind": claim.kind,
                "role": claim.role,
                "architecture": claim.architecture,
                "declared_materialization": claim.declared,
                "effective_materialization": effective,
                "sha256": claim.sha256,
                "bytes": claim.bytes,
                "reference": _immutable_reference(
                    claim.reference,
                    claim.sha256,
                    f"resolved {claim.input_id} reference",
                    kind=claim.kind,
                    target_abi=target_abi,
                    require_candidate=effective == "lazy-reference",
                ),
            }
            if effective != "lazy-reference":
                value["path"] = f"inputs/objects/{claim.input_id}-sha256-{claim.sha256}"
            if claim.kind == "homebrew-bottle":
                if (
                    claim.descriptor_sha256 is None
                    or claim.descriptor_bytes is None
                    or claim.descriptor_reference is None
                ):
                    raise ProductInputResolutionError(
                        "Homebrew input lacks authenticated composition metadata"
                    )
                value["descriptor"] = {
                    "sha256": claim.descriptor_sha256,
                    "bytes": claim.descriptor_bytes,
                    "reference": _immutable_reference(
                        claim.descriptor_reference,
                        claim.descriptor_sha256,
                        f"resolved {claim.input_id} descriptor reference",
                        kind=claim.kind,
                        target_abi=target_abi,
                    ),
                    "path": (
                        f"inputs/objects/{claim.input_id}-metadata-sha256-"
                        f"{claim.descriptor_sha256}"
                    ),
                }
            input_values.append(value)
            input_requests.extend(
                ProductInputRequestV1(
                    input_id=claim.input_id,
                    requesting_product_id=item.requesting_product_id,
                    root_kind=item.root_kind,
                    root_id=item.root_id,
                    materialization=item.materialization,
                )
                for item in object_requests[claim.object_key]
            )
        document = {
            "schema": 1,
            "kind": "kandelo-resolved-vfs-product-inputs",
            "product": {
                "id": product_id,
                "manifest_path": paths[product_id],
                "manifest_sha256": canonical_sha256(manifest),
                "architecture": manifest["architecture"],
                "output": manifest["output"],
            },
            "target_abi": {"version": target_abi, "snapshot_sha256": target_snapshot},
            "build_environment": {
                "policy_sha256": build_policy_sha256,
                "dev_shell_lock_sha256": dev_shell_lock_sha256,
            },
            "reference_class": "candidate",
            "source": source,
            "inputs": input_values,
        }
        normalized = load_resolved_product_inputs(canonical_bytes(document))
        plain = _plain(normalized)
        dependencies_for_product = tuple(
            sorted(value["id"] for value in manifest["composition"]["product"])
        )
        formula_subjects = tuple(sorted(product_formula_uses[product_id]))
        plan = ProductInputPlanV1(
            product_id=product_id,
            manifest_path=paths[product_id],
            manifest_sha256=canonical_sha256(manifest),
            architecture=manifest["architecture"],
            reference_class="candidate",
            resolved_inputs_sha256=canonical_sha256(plain),
            dependency_product_ids=dependencies_for_product,
            required_formula_subjects=formula_subjects,
            runtime_bundle_sha256=runtime_bundle_sha256,
        )
        results.append(
            ResolvedProductPlanV1(
                plan=plan,
                resolved_inputs=plain,
                input_requests=tuple(sorted(set(input_requests))),
            )
        )
    return tuple(results)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain(child) for child in value]
    return value


def load_resolved_product_inputs(body: bytes) -> Mapping[str, Any]:
    """Validate canonical resolved inputs without opening caller-owned local files."""

    try:
        root = parse_canonical_bytes(
            body,
            maximum_bytes=MAX_DOCUMENT_BYTES,
            maximum_items=MAX_INPUTS * 16,
            maximum_string_bytes=8_192,
        )
    except CanonicalJsonError as error:
        raise ProductInputResolutionError(f"resolved input document is not canonical: {error}") from error
    _exact(
        root,
        frozenset(
            {
                "schema",
                "kind",
                "product",
                "target_abi",
                "build_environment",
                "reference_class",
                "source",
                "inputs",
            }
        ),
        "resolved input document",
    )
    if root["schema"] != 1 or root["kind"] != "kandelo-resolved-vfs-product-inputs":
        raise ProductInputResolutionError("resolved input document protocol is unsupported")
    product = _exact(
        root["product"],
        frozenset({"id", "manifest_path", "manifest_sha256", "architecture", "output"}),
        "resolved product",
    )
    _stable_id(product["id"], "resolved product ID")
    _relative_path(product["manifest_path"], "resolved product manifest path")
    _digest(product["manifest_sha256"], "resolved product manifest")
    architecture = _architecture(product["architecture"], "resolved product architecture")
    output = _text(product["output"], "resolved product output", 255)
    if output.startswith(".") or "/" in output or "\\" in output or not output.endswith((".vfs", ".vfs.zst")):
        raise ProductInputResolutionError("resolved product output filename is invalid")
    target = _exact(root["target_abi"], frozenset({"version", "snapshot_sha256"}), "resolved ABI")
    target_abi = _integer(target["version"], "resolved ABI version")
    _digest(target["snapshot_sha256"], "resolved ABI snapshot")
    environment = _exact(
        root["build_environment"],
        frozenset({"policy_sha256", "dev_shell_lock_sha256"}),
        "resolved build environment",
    )
    _digest(environment["policy_sha256"], "resolved build policy")
    _digest(environment["dev_shell_lock_sha256"], "resolved dev-shell lock")
    if root["reference_class"] not in {"candidate", "canonical"}:
        raise ProductInputResolutionError("resolved reference class is unsupported")
    _source(root["source"], "resolved source")
    inputs = _sequence(root["inputs"], "resolved inputs")
    if len(inputs) > MAX_INPUTS:
        raise ProductInputResolutionError("resolved inputs exceed their bound")
    previous = ""
    paths = set()
    for index, value in enumerate(inputs):
        permitted = frozenset(
            {
                "id",
                "kind",
                "role",
                "architecture",
                "declared_materialization",
                "descriptor",
                "effective_materialization",
                "sha256",
                "bytes",
                "reference",
                "path",
            }
        )
        item = _mapping(value, f"resolved input {index}")
        required = permitted - frozenset({"descriptor", "reference", "path"})
        if not required.issubset(item) or not frozenset(item).issubset(permitted):
            raise ProductInputResolutionError(f"resolved input {index} fields changed")
        input_id = _stable_id(item["id"], f"resolved input {index} ID")
        if input_id <= previous:
            raise ProductInputResolutionError("resolved inputs are not sorted and unique")
        previous = input_id
        kind = item["kind"]
        if kind not in INPUT_KINDS:
            raise ProductInputResolutionError("resolved input kind is unsupported")
        if item["role"] not in {"runtime", "build"} or item["architecture"] != architecture:
            raise ProductInputResolutionError("resolved input role or architecture differs")
        sha256 = _digest(item["sha256"], f"resolved input {input_id} digest")
        _integer(item["bytes"], f"resolved input {input_id} bytes")
        declared = item["declared_materialization"]
        effective = item["effective_materialization"]
        valid = (
            item["role"] == "runtime"
            and declared == "embedded"
            and effective == "embedded"
        ) or (
            item["role"] == "runtime"
            and declared == "lazy"
            and effective in {"lazy-reference", "embedded"}
        ) or (
            item["role"] == "build"
            and declared == "build-only"
            and effective == "build-only"
        )
        if not valid:
            raise ProductInputResolutionError("resolved input materialization is contradictory")
        reference = item.get("reference")
        path = item.get("path")
        descriptor = item.get("descriptor")
        if kind == "homebrew-bottle":
            checked_descriptor = _exact(
                descriptor,
                frozenset({"sha256", "bytes", "reference", "path"}),
                f"resolved input {input_id} descriptor",
            )
            descriptor_sha256 = _digest(
                checked_descriptor["sha256"],
                f"resolved input {input_id} descriptor digest",
            )
            _integer(
                checked_descriptor["bytes"],
                f"resolved input {input_id} descriptor bytes",
                positive=True,
            )
            descriptor_reference = checked_descriptor["reference"]
            if root["reference_class"] == "candidate":
                _immutable_reference(
                    descriptor_reference,
                    descriptor_sha256,
                    f"resolved input {input_id} descriptor reference",
                    kind=kind,
                    target_abi=target_abi,
                    require_candidate=effective == "lazy-reference",
                )
            else:
                _text(
                    descriptor_reference,
                    f"resolved input {input_id} descriptor reference",
                )
                if (
                    f"sha256:{descriptor_sha256}" not in descriptor_reference
                    and f"sha256={descriptor_sha256}" not in descriptor_reference
                ):
                    raise ProductInputResolutionError(
                        "canonical descriptor reference does not bind its digest"
                    )
                if CANDIDATE_NAMESPACE.search(descriptor_reference):
                    raise ProductInputResolutionError(
                        "canonical descriptor enters candidate namespace"
                    )
            descriptor_path = _relative_path(
                checked_descriptor["path"],
                f"resolved input {input_id} descriptor path",
            )
            if descriptor_path in paths:
                raise ProductInputResolutionError(
                    "resolved inputs repeat a local descriptor path"
                )
            paths.add(descriptor_path)
        elif descriptor is not None:
            raise ProductInputResolutionError(
                "resolved input descriptor is only valid for Homebrew bottles"
            )
        if reference is not None:
            if root["reference_class"] == "candidate":
                _immutable_reference(
                    reference,
                    sha256,
                    f"resolved input {input_id} reference",
                    kind=kind,
                    target_abi=target_abi,
                    require_candidate=effective == "lazy-reference",
                )
            else:
                _text(reference, f"resolved input {input_id} reference")
                if f"sha256:{sha256}" not in reference and f"sha256={sha256}" not in reference:
                    raise ProductInputResolutionError("canonical reference does not bind its digest")
                if CANDIDATE_NAMESPACE.search(reference):
                    raise ProductInputResolutionError("canonical input enters candidate namespace")
        if effective == "lazy-reference":
            if reference is None or path is not None:
                raise ProductInputResolutionError("lazy input requires only an immutable reference")
        else:
            checked_path = _relative_path(path, f"resolved input {input_id} path")
            if checked_path in paths:
                raise ProductInputResolutionError("resolved inputs repeat a local path")
            paths.add(checked_path)
    return root


def materialize_resolved_product_input_objects(
    resolved_inputs_body: bytes,
    *,
    root: Path,
    transport: OciTransportV1,
) -> Path:
    """Close one resolved document over exact private and anonymous public bytes."""

    resolved = load_resolved_product_inputs(resolved_inputs_body)
    input_root = _checked_authority_root(root, "resolved product input root")
    object_root = input_root / "inputs" / "objects"
    try:
        object_metadata = object_root.lstat()
    except OSError as error:
        raise ProductInputResolutionError(
            f"resolved product object root is unavailable: {error}"
        ) from error
    if stat.S_ISLNK(object_metadata.st_mode) or not stat.S_ISDIR(
        object_metadata.st_mode
    ):
        raise ProductInputResolutionError(
            "resolved product object root is not a real directory"
        )

    created: list[Path] = []

    def materialize(
        *,
        input_id: str,
        relative: Any,
        reference: Any,
        digest: Any,
        size: Any,
        field: str,
    ) -> None:
        checked_relative = _relative_path(relative, f"{field} path")
        if not checked_relative.startswith("inputs/objects/"):
            raise ProductInputResolutionError(
                f"{field} path is outside the closed product object namespace"
            )
        checked_digest = _digest(digest, f"{field} digest")
        checked_size = _integer(size, f"{field} bytes", positive=True)
        target = input_root.joinpath(*checked_relative.split("/"))
        try:
            target.relative_to(object_root)
            parent = target.parent
            parent_metadata = parent.lstat()
        except (OSError, ValueError) as error:
            raise ProductInputResolutionError(
                f"{field} parent is unavailable: {error}"
            ) from error
        if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
            parent_metadata.st_mode
        ):
            raise ProductInputResolutionError(f"{field} parent is not a real directory")

        if target.exists() or target.is_symlink():
            _checked_input_object_file(
                input_root,
                {
                    "id": input_id,
                    "path": checked_relative,
                    "sha256": checked_digest,
                    "bytes": checked_size,
                },
            )
            return
        checked_reference = _text(reference, f"{field} reference", 8_192)
        if not checked_reference.startswith("ghcr.io/"):
            raise ProductInputResolutionError(
                f"{field} private object is absent and cannot be fetched publicly"
            )
        try:
            body = fetch_public_blob(
                checked_reference,
                expected_sha256=checked_digest,
                expected_bytes=checked_size,
                transport=transport,
            )
            with target.open("xb") as output:
                output.write(body)
            target.chmod(0o600)
            created.append(target)
        except Exception:
            if target.exists() and target not in created:
                try:
                    target.unlink()
                except OSError:
                    pass
            raise

    document_path = input_root / "resolved-inputs.json"
    if document_path.exists() or document_path.is_symlink():
        raise ProductInputResolutionError(
            "resolved product input document already exists"
        )
    try:
        for item in resolved["inputs"]:
            if item.get("path") is not None:
                materialize(
                    input_id=item["id"],
                    relative=item["path"],
                    reference=item.get("reference"),
                    digest=item["sha256"],
                    size=item["bytes"],
                    field=f"resolved input {item['id']}",
                )
            descriptor = item.get("descriptor")
            if descriptor is not None:
                materialize(
                    input_id=f"{item['id']}-descriptor",
                    relative=descriptor["path"],
                    reference=descriptor["reference"],
                    digest=descriptor["sha256"],
                    size=descriptor["bytes"],
                    field=f"resolved input {item['id']} descriptor",
                )
        with document_path.open("xb") as output:
            output.write(resolved_inputs_body)
        document_path.chmod(0o600)
        return document_path
    except Exception:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _load_product_canonical(body: bytes, field: str) -> Mapping[str, Any]:
    try:
        value = parse_canonical_bytes(
            body,
            maximum_bytes=MAX_PRODUCT_HANDOFF_JSON_BYTES,
            maximum_items=1_024,
            maximum_string_bytes=8_192,
        )
    except CanonicalJsonError as error:
        raise ProductInputResolutionError(
            f"{field} is not canonical JSON: {error}"
        ) from error
    return _mapping(value, field)


def _optional_digest(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _digest(value, field)


def _product_output(value: Any, field: str) -> str:
    output = _text(value, field, 255)
    if (
        output.startswith(".")
        or "/" in output
        or "\\" in output
        or not output.endswith((".vfs", ".vfs.zst"))
    ):
        raise ProductInputResolutionError(f"{field} is not a VFS output filename")
    return output


def _validate_product_build_result(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "request_sha256",
                "work_id",
                "product",
                "outcome",
                "guard_code",
                "exit_code",
                "runtime_bundle_sha256",
                "resolved_inputs_sha256",
                "builder_report_sha256",
                "vfs",
                "diagnostic_summary_sha256",
            }
        ),
        "product build result",
    )
    if (
        result["schema"] != 1
        or result["kind"] != "kandelo-abi-staging-product-build-result"
    ):
        raise ProductInputResolutionError(
            "product build result protocol is unsupported"
        )
    _digest(result["request_sha256"], "product build request")
    _digest(result["work_id"], "product build work ID")
    product = _exact(
        result["product"],
        frozenset({"id", "manifest_sha256", "output"}),
        "product build product",
    )
    _stable_id(product["id"], "product build product ID")
    _digest(product["manifest_sha256"], "product build manifest")
    _product_output(product["output"], "product build output")
    outcome = result["outcome"]
    if outcome not in PRODUCT_BUILD_OUTCOMES:
        raise ProductInputResolutionError("product build outcome is unsupported")
    guard = result["guard_code"]
    if guard is not None and guard not in PRODUCT_BUILD_GUARDS:
        raise ProductInputResolutionError("product build guard code is unsupported")
    exit_code = _integer(result["exit_code"], "product build exit code")
    _digest(result["runtime_bundle_sha256"], "product build runtime bundle")
    resolved_sha256 = _optional_digest(
        result["resolved_inputs_sha256"], "product build resolved inputs"
    )
    report_sha256 = _optional_digest(
        result["builder_report_sha256"], "product build builder report"
    )
    _digest(result["diagnostic_summary_sha256"], "product build diagnostics")
    vfs = result["vfs"]
    if vfs is not None:
        checked_vfs = _exact(
            vfs, frozenset({"sha256", "bytes"}), "product build VFS"
        )
        _digest(checked_vfs["sha256"], "product build VFS digest")
        _integer(checked_vfs["bytes"], "product build VFS bytes", positive=True)
    if outcome == "success":
        if (
            guard is not None
            or exit_code != 0
            or resolved_sha256 is None
            or report_sha256 is None
            or vfs is None
        ):
            raise ProductInputResolutionError(
                "successful product build lacks its exact artifacts"
            )
    elif (
        guard is None
        or exit_code == 0
        or report_sha256 is not None
        or vfs is not None
    ):
        raise ProductInputResolutionError(
            "unsuccessful product build claims contradictory artifacts"
        )
    if outcome == "blocked" and resolved_sha256 is not None:
        raise ProductInputResolutionError(
            "blocked product build cannot claim resolved inputs"
        )
    return _plain(result)


def load_product_build_result(body: bytes) -> dict[str, Any]:
    """Load one canonical terminal result from an uncredentialed product job."""

    return _validate_product_build_result(
        _load_product_canonical(body, "product build result")
    )


def write_product_build_handoff(
    root: Path,
    *,
    request_sha256: str,
    work_id: str,
    product: Mapping[str, Any],
    runtime_bundle_body: bytes,
    outcome: Literal["success", "blocked", "failure"],
    guard_code: str | None,
    exit_code: int,
    diagnostic_summary: bytes,
    resolved_inputs_body: bytes | None = None,
    builder_report_body: bytes | None = None,
    vfs_body: bytes | None = None,
) -> dict[str, Any]:
    """Write one exact terminal product handoff without private input objects."""

    checked_request = _digest(request_sha256, "product handoff request")
    checked_work = _digest(work_id, "product handoff work ID")
    checked_product = _mapping(product, "product handoff product")
    product_id = _stable_id(checked_product.get("id"), "product handoff product ID")
    manifest_sha256 = _digest(
        checked_product.get("manifest_sha256"), "product handoff manifest"
    )
    output = _product_output(
        checked_product.get("output"), "product handoff output"
    )
    if not isinstance(runtime_bundle_body, bytes) or not (
        1 <= len(runtime_bundle_body) <= 16 * 1024 * 1024
    ):
        raise ProductInputResolutionError(
            "product handoff runtime bundle is outside its byte bound"
        )
    if (
        not isinstance(diagnostic_summary, bytes)
        or not diagnostic_summary
        or len(diagnostic_summary) > 64 * 1024
        or b"\0" in diagnostic_summary
    ):
        raise ProductInputResolutionError(
            "product handoff diagnostic summary is outside its byte bound"
        )
    try:
        diagnostic_summary.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ProductInputResolutionError(
            "product handoff diagnostic summary is not UTF-8"
        ) from error
    if outcome not in PRODUCT_BUILD_OUTCOMES:
        raise ProductInputResolutionError("product handoff outcome is unsupported")
    if (
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or not 0 <= exit_code <= 255
    ):
        raise ProductInputResolutionError("product handoff exit code is invalid")
    bodies = (resolved_inputs_body, builder_report_body, vfs_body)
    if outcome == "success":
        if guard_code is not None or exit_code != 0 or any(
            not isinstance(body, bytes) or not body for body in bodies
        ):
            raise ProductInputResolutionError(
                "successful product handoff lacks its exact artifacts"
            )
    elif (
        guard_code not in PRODUCT_BUILD_GUARDS
        or exit_code == 0
        or any(body is not None for body in bodies)
    ):
        raise ProductInputResolutionError(
            "unsuccessful product handoff has contradictory artifacts"
        )

    destination = Path(root)
    try:
        if destination.exists() or destination.is_symlink():
            raise ProductInputResolutionError(
                "product handoff destination must not already exist"
            )
        parent = destination.parent.resolve(strict=True)
        parent_metadata = parent.lstat()
    except OSError as error:
        raise ProductInputResolutionError(
            f"product handoff destination is unavailable: {error}"
        ) from error
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
        parent_metadata.st_mode
    ):
        raise ProductInputResolutionError(
            "product handoff parent must be a real directory"
        )
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=parent)
    )

    def write(relative: str, body: bytes) -> None:
        path = staging.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with path.open("xb") as output_file:
                output_file.write(body)
            path.chmod(0o600)
        except OSError as error:
            raise ProductInputResolutionError(
                f"cannot write product handoff member {relative}: {error}"
            ) from error

    try:
        write("diagnostics/summary.txt", diagnostic_summary)
        write("runtime-bundle.json", runtime_bundle_body)
        resolved_sha256 = None
        report_sha256 = None
        vfs_identity = None
        if outcome == "success":
            assert resolved_inputs_body is not None
            assert builder_report_body is not None
            assert vfs_body is not None
            write("resolved-inputs.json", resolved_inputs_body)
            write("builder-report.json", builder_report_body)
            write(output, vfs_body)
            resolved_sha256 = hashlib.sha256(resolved_inputs_body).hexdigest()
            report_sha256 = hashlib.sha256(builder_report_body).hexdigest()
            vfs_identity = {
                "sha256": hashlib.sha256(vfs_body).hexdigest(),
                "bytes": len(vfs_body),
            }
        result = _validate_product_build_result(
            {
                "schema": 1,
                "kind": "kandelo-abi-staging-product-build-result",
                "request_sha256": checked_request,
                "work_id": checked_work,
                "product": {
                    "id": product_id,
                    "manifest_sha256": manifest_sha256,
                    "output": output,
                },
                "outcome": outcome,
                "guard_code": guard_code,
                "exit_code": exit_code,
                "runtime_bundle_sha256": hashlib.sha256(
                    runtime_bundle_body
                ).hexdigest(),
                "resolved_inputs_sha256": resolved_sha256,
                "builder_report_sha256": report_sha256,
                "vfs": vfs_identity,
                "diagnostic_summary_sha256": hashlib.sha256(
                    diagnostic_summary
                ).hexdigest(),
            }
        )
        write("product-build-result.json", canonical_bytes(result))
        write(
            "inventory.json",
            canonical_bytes(build_product_handoff_inventory(staging, result)),
        )
        os.rename(staging, destination)
        return result
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _product_handoff_role(path: str, output: str) -> str:
    roles = {
        "builder-report.json": "builder-report",
        "diagnostics/summary.txt": "diagnostic-summary",
        "product-build-result.json": "product-build-result",
        "resolved-inputs.json": "resolved-inputs",
        "runtime-bundle.json": "runtime-bundle",
        output: "vfs-image",
    }
    role = roles.get(path)
    if role is None:
        raise ProductInputResolutionError(
            f"unexpected product handoff file: {path}"
        )
    return role


def _product_handoff_files(root: Path) -> list[tuple[str, Path, os.stat_result]]:
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise ProductInputResolutionError(
            f"cannot inspect product handoff root: {error}"
        ) from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ProductInputResolutionError(
            "product handoff root must be a real directory"
        )
    files = []
    try:
        candidates = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    except OSError as error:
        raise ProductInputResolutionError(
            f"cannot enumerate product handoff: {error}"
        ) from error
    for candidate in candidates:
        relative = candidate.relative_to(root).as_posix()
        _relative_path(relative, "product handoff path")
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise ProductInputResolutionError(
                f"cannot inspect product handoff member {relative}: {error}"
            ) from error
        if stat.S_ISDIR(metadata.st_mode):
            if relative != "diagnostics":
                raise ProductInputResolutionError(
                    f"unexpected product handoff directory: {relative}"
                )
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ProductInputResolutionError(
                f"product handoff member is a symlink: {relative}"
            )
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ProductInputResolutionError(
                f"product handoff member is not one regular file: {relative}"
            )
        if metadata.st_size < 1:
            raise ProductInputResolutionError(
                f"product handoff member is empty: {relative}"
            )
        files.append((relative, candidate, metadata))
    return files


def _product_file_identity(path: Path, metadata: os.stat_result) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ProductInputResolutionError(
            f"cannot open product handoff member {path.name}: {error}"
        ) from error
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            raise ProductInputResolutionError(
                "product handoff member changed before reading"
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        closed = os.fstat(descriptor)
        if (
            closed.st_size != size
            or closed.st_dev != opened.st_dev
            or closed.st_ino != opened.st_ino
        ):
            raise ProductInputResolutionError(
                "product handoff member changed while reading"
            )
    finally:
        os.close(descriptor)
    return {"sha256": digest.hexdigest(), "bytes": size}


def build_product_handoff_inventory(
    root: Path, result: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe every non-inventory file in one bounded product handoff."""

    checked_result = _validate_product_build_result(result)
    output = checked_result["product"]["output"]
    entries = []
    for relative, path, metadata in _product_handoff_files(root):
        if relative == "inventory.json":
            continue
        identity = _product_file_identity(path, metadata)
        entries.append(
            {
                "path": relative,
                "role": _product_handoff_role(relative, output),
                **identity,
            }
        )
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-product-build-handoff-inventory",
        "product_id": checked_result["product"]["id"],
        "work_id": checked_result["work_id"],
        "outcome": checked_result["outcome"],
        "files": entries,
    }


def _load_product_handoff_inventory(
    body: bytes, *, output: str
) -> dict[str, Any]:
    inventory = _exact(
        _load_product_canonical(body, "product handoff inventory"),
        frozenset(
            {"schema", "kind", "product_id", "work_id", "outcome", "files"}
        ),
        "product handoff inventory",
    )
    if (
        inventory["schema"] != 1
        or inventory["kind"]
        != "kandelo-abi-staging-product-build-handoff-inventory"
    ):
        raise ProductInputResolutionError(
            "product handoff inventory protocol is unsupported"
        )
    _stable_id(inventory["product_id"], "product handoff product ID")
    _digest(inventory["work_id"], "product handoff work ID")
    if inventory["outcome"] not in PRODUCT_BUILD_OUTCOMES:
        raise ProductInputResolutionError("product handoff outcome is unsupported")
    values = _sequence(inventory["files"], "product handoff inventory files")
    if not 1 <= len(values) <= MAX_PRODUCT_HANDOFF_FILES:
        raise ProductInputResolutionError(
            "product handoff inventory file count is outside its bound"
        )
    files = []
    previous = ""
    for index, value in enumerate(values):
        item = _exact(
            value,
            frozenset({"path", "role", "sha256", "bytes"}),
            f"product handoff inventory file {index}",
        )
        path = _relative_path(
            item["path"], f"product handoff inventory file {index} path"
        )
        if path == "inventory.json" or path <= previous:
            raise ProductInputResolutionError(
                "product handoff inventory files are not sorted and unique"
            )
        previous = path
        if item["role"] != _product_handoff_role(path, output):
            raise ProductInputResolutionError(
                f"product handoff inventory role differs for {path}"
            )
        files.append(
            {
                "path": path,
                "role": item["role"],
                "sha256": _digest(
                    item["sha256"], f"product handoff inventory {path} digest"
                ),
                "bytes": _integer(
                    item["bytes"],
                    f"product handoff inventory {path} bytes",
                    positive=True,
                ),
            }
        )
    return {**_plain(inventory), "files": files}


def load_product_input_object_inventory(body: bytes) -> dict[str, Any]:
    """Load the exact manifest-derived private object closure from a build job."""

    try:
        value = parse_canonical_bytes(
            body,
            maximum_bytes=MAX_PRODUCT_HANDOFF_JSON_BYTES,
            maximum_items=MAX_INPUTS * 20 + 128,
            maximum_string_bytes=8_192,
        )
    except CanonicalJsonError as error:
        raise ProductInputResolutionError(
            f"product input object inventory is not canonical JSON: {error}"
        ) from error
    inventory = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "product",
                "source",
                "target_abi",
                "build_environment",
                "objects",
            }
        ),
        "product input object inventory",
    )
    if (
        inventory["schema"] != 1
        or inventory["kind"] != "kandelo-vfs-product-input-object-inventory"
    ):
        raise ProductInputResolutionError(
            "product input object inventory protocol is unsupported"
        )
    product = _exact(
        inventory["product"],
        frozenset({"id", "manifest_path", "manifest_sha256", "architecture"}),
        "product input object inventory product",
    )
    product_id = _stable_id(product["id"], "product input object product ID")
    manifest_path = _relative_path(
        product["manifest_path"], "product input object manifest path"
    )
    if manifest_path != f"images/vfs/products/{product_id}.toml":
        raise ProductInputResolutionError(
            "product input object inventory names a noncanonical manifest path"
        )
    _digest(product["manifest_sha256"], "product input object manifest")
    _architecture(product["architecture"], "product input object architecture")
    source = _source(inventory["source"], "product input object source")
    target = _exact(
        inventory["target_abi"],
        frozenset({"version", "snapshot_sha256"}),
        "product input object target ABI",
    )
    _integer(target["version"], "product input object target ABI version")
    _digest(
        target["snapshot_sha256"],
        "product input object target ABI snapshot",
    )
    environment = _exact(
        inventory["build_environment"],
        frozenset({"policy_sha256", "dev_shell_lock_sha256"}),
        "product input object build environment",
    )
    _digest(environment["policy_sha256"], "product input object build policy")
    _digest(
        environment["dev_shell_lock_sha256"],
        "product input object dev-shell lock",
    )

    values = _sequence(inventory["objects"], "product input objects")
    if len(values) > MAX_INPUTS:
        raise ProductInputResolutionError(
            "product input object count is outside its bound"
        )
    objects = []
    previous = ""
    paths: set[str] = set()
    base_keys = frozenset(
        {
            "id",
            "kind",
            "role",
            "declared_materialization",
            "architecture",
            "adapter",
            "path",
            "sha256",
            "bytes",
        }
    )
    kind_keys = {
        "package-output": frozenset({"package", "selector_kind", "selector"}),
        "source-archive": frozenset({"archive_id", "url"}),
        "toolchain-output": frozenset(
            {"toolchain_id", "provider", "component"}
        ),
        "repository-path": frozenset({"repository_id", "paths"}),
    }
    for index, value in enumerate(values):
        candidate = _mapping(value, f"product input object {index}")
        kind = candidate.get("kind")
        if kind not in kind_keys:
            raise ProductInputResolutionError(
                f"product input object {index} kind is unsupported"
            )
        item = _exact(
            candidate,
            base_keys | kind_keys[kind],
            f"product input object {index}",
        )
        input_id = _stable_id(item["id"], f"product input object {index} ID")
        if input_id <= previous:
            raise ProductInputResolutionError(
                "product input objects are not sorted and unique"
            )
        previous = input_id
        role = item["role"]
        declared = item["declared_materialization"]
        if (
            (role == "build" and declared != "build-only")
            or (role == "runtime" and declared not in {"embedded", "lazy"})
            or role not in {"runtime", "build"}
        ):
            raise ProductInputResolutionError(
                f"product input object {input_id} role/materialization is invalid"
            )
        architecture = _architecture(
            item["architecture"], f"product input object {input_id} architecture"
        )
        if architecture != product["architecture"]:
            raise ProductInputResolutionError(
                f"product input object {input_id} architecture differs"
            )
        sha256 = _digest(
            item["sha256"], f"product input object {input_id} digest"
        )
        path = _relative_path(
            item["path"], f"product input object {input_id} path"
        )
        if path != f"inputs/objects/{input_id}-sha256-{sha256}" or path in paths:
            raise ProductInputResolutionError(
                f"product input object {input_id} path is not its exact object path"
            )
        paths.add(path)
        _integer(
            item["bytes"], f"product input object {input_id} bytes", positive=True
        )
        if kind == "package-output":
            package = _stable_id(
                item["package"], f"product input object {input_id} package"
            )
            selector_kind = item["selector_kind"]
            if selector_kind not in {"output", "source-role"}:
                raise ProductInputResolutionError(
                    f"product input object {input_id} selector kind is unsupported"
                )
            selector = _stable_id(
                item["selector"], f"product input object {input_id} selector"
            )
            if input_id != _resolved_input_id(
                kind, package, selector_kind, selector
            ):
                raise ProductInputResolutionError(
                    f"product input object {input_id} identity differs from its selector"
                )
            allowed_adapters = (
                {"package-source-role-zip-v1"}
                if selector_kind == "source-role"
                else {
                    "package-output-file-v1",
                    "package-output-directory-zip-v1",
                }
            )
            if item["adapter"] not in allowed_adapters:
                raise ProductInputResolutionError(
                    f"product input object {input_id} adapter is unsupported"
                )
        elif kind == "source-archive":
            archive_id = _stable_id(
                item["archive_id"], f"product input object {input_id} archive ID"
            )
            if (
                input_id != _resolved_input_id(kind, archive_id)
                or item["adapter"] != "source-archive-v1"
                or not _text(
                    item["url"], f"product input object {input_id} URL", 8_192
                ).startswith("https://")
            ):
                raise ProductInputResolutionError(
                    f"product input object {input_id} archive identity is unsupported"
                )
        elif kind == "toolchain-output":
            toolchain_id = _stable_id(
                item["toolchain_id"],
                f"product input object {input_id} toolchain ID",
            )
            if (
                input_id != _resolved_input_id(kind, toolchain_id)
                or item["adapter"] != "toolchain-directory-zip-v1"
                or item["provider"] != "repository-dev-shell"
            ):
                raise ProductInputResolutionError(
                    f"product input object {input_id} toolchain identity is unsupported"
                )
            _stable_id(
                item["component"], f"product input object {input_id} component"
            )
        else:
            repository_id = _stable_id(
                item["repository_id"],
                f"product input object {input_id} repository ID",
            )
            repository_paths = [
                _relative_path(path_value, f"product input object {input_id} repository path")
                for path_value in _sequence(
                    item["paths"],
                    f"product input object {input_id} repository paths",
                )
            ]
            if (
                input_id != _resolved_input_id(kind, repository_id)
                or item["adapter"] != "repository-path-bundle-v1"
                or not repository_paths
                or repository_paths != sorted(set(repository_paths))
            ):
                raise ProductInputResolutionError(
                    f"product input object {input_id} repository identity is unsupported"
                )
        objects.append(_plain(item))
    return {**_plain(inventory), "source": source, "objects": objects}


def _manifest_private_input_authority(
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    architecture = _architecture(
        manifest["architecture"], "private input product architecture"
    )
    expected: dict[str, dict[str, Any]] = {}

    def add(input_id: str, value: dict[str, Any]) -> None:
        if input_id in expected:
            raise ProductInputResolutionError(
                "product manifest has colliding private input identities"
            )
        expected[input_id] = {
            "id": input_id,
            "architecture": architecture,
            **value,
        }

    for index, value in enumerate(
        _sequence(manifest["software"]["package"], "private package claims")
    ):
        candidate = _mapping(value, f"private package claim {index}")
        keys = frozenset({"name", "outputs", "source_roles", "role"})
        if "materialization" in candidate:
            keys |= frozenset({"materialization"})
        package = _exact(candidate, keys, f"private package claim {index}")
        name = _stable_id(package["name"], f"private package claim {index} name")
        role = package["role"]
        declared = _materialization(
            role,
            package.get("materialization"),
            f"private package claim {name}",
        )
        selectors = [
            (
                "output",
                _stable_id(selector, f"private package {name} output"),
            )
            for selector in _sequence(
                package["outputs"], f"private package {name} outputs"
            )
        ] + [
            (
                "source-role",
                _stable_id(selector, f"private package {name} source role"),
            )
            for selector in _sequence(
                package["source_roles"], f"private package {name} source roles"
            )
        ]
        if not selectors:
            raise ProductInputResolutionError(
                f"private package {name} has no output or source role"
            )
        for selector_kind, selector in selectors:
            add(
                _resolved_input_id(
                    "package-output", name, selector_kind, selector
                ),
                {
                    "kind": "package-output",
                    "role": role,
                    "declared_materialization": declared,
                    "package": name,
                    "selector_kind": selector_kind,
                    "selector": selector,
                },
            )

    for index, value in enumerate(
        _sequence(manifest["software"]["archive"], "private archive claims")
    ):
        candidate = _mapping(value, f"private archive claim {index}")
        keys = frozenset({"id", "url", "sha256", "role"})
        if "materialization" in candidate:
            keys |= frozenset({"materialization"})
        archive = _exact(candidate, keys, f"private archive claim {index}")
        archive_id = _stable_id(
            archive["id"], f"private archive claim {index} ID"
        )
        role = archive["role"]
        declared = _materialization(
            role, archive.get("materialization"), f"private archive {archive_id}"
        )
        url = _text(archive["url"], f"private archive {archive_id} URL", 8_192)
        if not url.startswith("https://"):
            raise ProductInputResolutionError(
                f"private archive {archive_id} URL must use HTTPS"
            )
        add(
            _resolved_input_id("source-archive", archive_id),
            {
                "kind": "source-archive",
                "role": role,
                "declared_materialization": declared,
                "archive_id": archive_id,
                "url": url,
                "manifest_sha256": _digest(
                    archive["sha256"], f"private archive {archive_id} digest"
                ),
            },
        )

    for index, value in enumerate(
        _sequence(manifest["software"]["toolchain"], "private toolchain claims")
    ):
        candidate = _mapping(value, f"private toolchain claim {index}")
        keys = frozenset({"id", "provider", "component", "role"})
        if "materialization" in candidate:
            keys |= frozenset({"materialization"})
        toolchain = _exact(candidate, keys, f"private toolchain claim {index}")
        toolchain_id = _stable_id(
            toolchain["id"], f"private toolchain claim {index} ID"
        )
        if toolchain["provider"] != "repository-dev-shell":
            raise ProductInputResolutionError(
                f"private toolchain {toolchain_id} provider is unsupported"
            )
        role = toolchain["role"]
        declared = _materialization(
            role,
            toolchain.get("materialization"),
            f"private toolchain {toolchain_id}",
        )
        add(
            _resolved_input_id("toolchain-output", toolchain_id),
            {
                "kind": "toolchain-output",
                "role": role,
                "declared_materialization": declared,
                "toolchain_id": toolchain_id,
                "provider": "repository-dev-shell",
                "component": _stable_id(
                    toolchain["component"],
                    f"private toolchain {toolchain_id} component",
                ),
            },
        )

    for index, value in enumerate(
        _sequence(
            manifest["composition"]["repository"],
            "private repository claims",
        )
    ):
        candidate = _mapping(value, f"private repository claim {index}")
        keys = frozenset({"id", "paths", "role"})
        if "materialization" in candidate:
            keys |= frozenset({"materialization"})
        repository = _exact(candidate, keys, f"private repository claim {index}")
        repository_id = _stable_id(
            repository["id"], f"private repository claim {index} ID"
        )
        role = repository["role"]
        declared = _materialization(
            role,
            repository.get("materialization"),
            f"private repository {repository_id}",
        )
        paths = tuple(
            _relative_path(path, f"private repository {repository_id} path")
            for path in _sequence(
                repository["paths"], f"private repository {repository_id} paths"
            )
        )
        if not paths or paths != tuple(sorted(set(paths))):
            raise ProductInputResolutionError(
                f"private repository {repository_id} paths are not sorted and unique"
            )
        if any(
            path == other or path.startswith(f"{other}/")
            for index, path in enumerate(paths)
            for other in paths[:index]
        ):
            raise ProductInputResolutionError(
                f"private repository {repository_id} paths overlap"
            )
        add(
            _resolved_input_id("repository-path", repository_id),
            {
                "kind": "repository-path",
                "role": role,
                "declared_materialization": declared,
                "repository_id": repository_id,
                "paths": list(paths),
            },
        )
    return expected


def _checked_authority_root(value: Path, field: str) -> Path:
    path = Path(value)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ProductInputResolutionError(f"{field} is unavailable: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ProductInputResolutionError(f"{field} is not a real directory")
    return resolved


def _checked_input_object_file(
    root: Path, item: Mapping[str, Any]
) -> tuple[Path, os.stat_result]:
    relative = _relative_path(item["path"], f"private input {item['id']} path")
    candidate = root.joinpath(*relative.split("/"))
    current = root
    try:
        for part in relative.split("/")[:-1]:
            current /= part
            parent = current.lstat()
            if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
                raise ProductInputResolutionError(
                    f"private input {item['id']} has a linked or non-directory parent"
                )
        metadata = candidate.lstat()
    except OSError as error:
        raise ProductInputResolutionError(
            f"private input {item['id']} object is unavailable: {error}"
        ) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ProductInputResolutionError(
            f"private input {item['id']} object is not one regular file"
        )
    if metadata.st_size != item["bytes"] or not 1 <= metadata.st_size <= MAX_INPUT_OBJECT_BYTES:
        raise ProductInputResolutionError(
            f"private input {item['id']} object bytes changed or exceed the bound"
        )
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ProductInputResolutionError(
            f"private input {item['id']} object cannot be read: {error}"
        ) from error
    if digest.hexdigest() != item["sha256"]:
        raise ProductInputResolutionError(
            f"private input {item['id']} object digest changed"
        )
    return candidate, metadata


def _validate_package_directory_archive(path: Path, *, root: str, field: str) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (
                not 2 <= len(entries) <= MAX_DIRECTORY_INPUT_ENTRIES
                or names != sorted(set(names))
                or names[0] != f"{root}/"
            ):
                raise ProductInputResolutionError(
                    f"{field} directory archive inventory is invalid"
                )
            total = 0
            for entry in entries:
                name = entry.filename
                if (
                    "\\" in name
                    or "\0" in name
                    or name.startswith("/")
                    or not name.startswith(f"{root}/")
                    or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))
                    or entry.create_system != 3
                    or entry.date_time != (1980, 1, 2, 0, 0, 0)
                    or entry.flag_bits & 1
                ):
                    raise ProductInputResolutionError(
                        f"{field} directory archive entry is invalid"
                    )
                mode = entry.external_attr >> 16
                if entry.is_dir():
                    if not stat.S_ISDIR(mode) or entry.file_size != 0:
                        raise ProductInputResolutionError(
                            f"{field} directory archive directory is invalid"
                        )
                    continue
                if not stat.S_ISREG(mode):
                    raise ProductInputResolutionError(
                        f"{field} directory archive contains a non-file"
                    )
                total += entry.file_size
                if total > MAX_DIRECTORY_INPUT_BYTES:
                    raise ProductInputResolutionError(
                        f"{field} directory archive exceeds its byte bound"
                    )
                with archive.open(entry) as member:
                    while member.read(1024 * 1024):
                        pass
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ProductInputResolutionError(
            f"{field} directory archive is unreadable: {error}"
        ) from error


def _validate_toolchain_directory_archive(
    path: Path,
    *,
    item: Mapping[str, Any],
    runtime_bundle: Mapping[str, Any],
    runtime_root: Path,
) -> None:
    component = item["component"]
    component_root = runtime_root / "toolchain" / component
    try:
        component_metadata = component_root.lstat()
    except OSError as error:
        raise ProductInputResolutionError(
            f"private toolchain {item['id']} component is unavailable: {error}"
        ) from error
    if stat.S_ISLNK(component_metadata.st_mode) or not stat.S_ISDIR(
        component_metadata.st_mode
    ):
        raise ProductInputResolutionError(
            f"private toolchain {item['id']} component is not a real directory"
        )
    inventory = {
        value["path"]: value
        for value in runtime_bundle["inventory"]
        if value["path"].startswith(f"toolchain/{component}/")
    }
    expected: dict[str, tuple[str, int, str | None, int]] = {
        f"{item['toolchain_id']}/": ("directory", 0o755, None, 0)
    }
    actual_inventory_paths: set[str] = set()

    def visit(directory: Path, prefix: str, depth: int) -> None:
        if depth > 128:
            raise ProductInputResolutionError(
                f"private toolchain {item['id']} component depth exceeds its bound"
            )
        try:
            children = sorted(directory.iterdir(), key=lambda child: child.name)
        except OSError as error:
            raise ProductInputResolutionError(
                f"private toolchain {item['id']} component cannot be read: {error}"
            ) from error
        for child in children:
            try:
                metadata = child.lstat()
            except OSError as error:
                raise ProductInputResolutionError(
                    f"private toolchain {item['id']} entry cannot be read: {error}"
                ) from error
            relative = child.name if not prefix else f"{prefix}/{child.name}"
            archive_path = f"{item['toolchain_id']}/{relative}"
            if stat.S_ISLNK(metadata.st_mode):
                raise ProductInputResolutionError(
                    f"private toolchain {item['id']} contains a symlink"
                )
            if stat.S_ISDIR(metadata.st_mode):
                expected[f"{archive_path}/"] = (
                    "directory",
                    stat.S_IMODE(metadata.st_mode),
                    None,
                    0,
                )
                visit(child, relative, depth + 1)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                runtime_path = f"toolchain/{component}/{relative}"
                runtime_item = inventory.get(runtime_path)
                digest = hashlib.sha256(child.read_bytes()).hexdigest()
                if (
                    runtime_item is None
                    or runtime_item["bytes"] != metadata.st_size
                    or runtime_item["sha256"] != digest
                ):
                    raise ProductInputResolutionError(
                        f"private toolchain {item['id']} differs from exact runtime inventory"
                    )
                actual_inventory_paths.add(runtime_path)
                expected[archive_path] = (
                    "file",
                    stat.S_IMODE(metadata.st_mode),
                    digest,
                    metadata.st_size,
                )
            else:
                raise ProductInputResolutionError(
                    f"private toolchain {item['id']} contains an unsupported entry"
                )
            if len(expected) > MAX_DIRECTORY_INPUT_ENTRIES:
                raise ProductInputResolutionError(
                    f"private toolchain {item['id']} exceeds its entry bound"
                )

    visit(component_root, "", 0)
    if len(expected) == 1 or actual_inventory_paths != set(inventory):
        raise ProductInputResolutionError(
            f"private toolchain {item['id']} closure differs from exact runtime"
        )
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if names != sorted(expected) or len(names) != len(set(names)):
                raise ProductInputResolutionError(
                    f"private toolchain {item['id']} archive closure differs"
                )
            for entry in entries:
                kind, mode, digest, size = expected[entry.filename]
                archive_mode = entry.external_attr >> 16
                if (
                    entry.create_system != 3
                    or entry.date_time != (1980, 1, 2, 0, 0, 0)
                    or entry.flag_bits & 1
                    or stat.S_IMODE(archive_mode) != mode
                    or (kind == "directory") != entry.is_dir()
                    or entry.file_size != size
                    or (kind == "directory" and not stat.S_ISDIR(archive_mode))
                    or (kind == "file" and not stat.S_ISREG(archive_mode))
                ):
                    raise ProductInputResolutionError(
                        f"private toolchain {item['id']} archive entry differs"
                    )
                if kind == "file":
                    member_digest = hashlib.sha256()
                    with archive.open(entry) as member:
                        while chunk := member.read(1024 * 1024):
                            member_digest.update(chunk)
                    if member_digest.hexdigest() != digest:
                        raise ProductInputResolutionError(
                            f"private toolchain {item['id']} archive bytes differ"
                        )
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ProductInputResolutionError(
            f"private toolchain {item['id']} archive is unreadable: {error}"
        ) from error


def _expected_repository_bundle(
    *,
    source_root: Path,
    source: Mapping[str, Any],
    paths: Sequence[str],
) -> bytes:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(relative: str) -> None:
        if relative in seen:
            return
        candidate = source_root.joinpath(*relative.split("/"))
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise ProductInputResolutionError(
                f"private repository path {relative} is unavailable: {error}"
            ) from error
        seen.add(relative)
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": relative, "kind": "directory", "mode": mode})
            for child in sorted(candidate.iterdir(), key=lambda item: item.name):
                visit(f"{relative}/{child.name}")
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(candidate)
            if (
                not target
                or len(target.encode()) > 4_096
                or "\0" in target
                or "\\" in target
                or target.startswith("/")
            ):
                raise ProductInputResolutionError(
                    f"private repository symlink {relative} has an unsafe target"
                )
            resolved = (candidate.parent / target).resolve(strict=False)
            try:
                resolved.relative_to(source_root)
            except ValueError as error:
                raise ProductInputResolutionError(
                    f"private repository symlink {relative} escapes exact source"
                ) from error
            if resolved == source_root:
                raise ProductInputResolutionError(
                    f"private repository symlink {relative} targets source root"
                )
            entries.append(
                {"path": relative, "kind": "symlink", "mode": mode, "target": target}
            )
        elif stat.S_ISREG(metadata.st_mode):
            body = candidate.read_bytes()
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": mode,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body),
                    "content_base64": base64.b64encode(body).decode("ascii"),
                }
            )
        else:
            raise ProductInputResolutionError(
                f"private repository path {relative} has an unsupported type"
            )
        if len(entries) > MAX_DIRECTORY_INPUT_ENTRIES:
            raise ProductInputResolutionError(
                "private repository bundle exceeds its entry bound"
            )

    for relative in paths:
        visit(relative)
    entries.sort(key=lambda item: item["path"])
    body = canonical_bytes(
        {
            "schema": 1,
            "kind": "kandelo-vfs-repository-path-bundle",
            "paths": list(paths),
            "source": _plain(source),
            "entries": entries,
        }
    )
    if len(body) > MAX_REPOSITORY_BUNDLE_BYTES:
        raise ProductInputResolutionError(
            "private repository bundle exceeds its byte bound"
        )
    return body


def validate_product_input_object_authority(
    inventory: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    catalog: Mapping[str, Any],
    runtime_bundle: Mapping[str, Any],
    object_root: Path,
    source_root: Path,
    runtime_root: Path,
) -> dict[str, Any]:
    """Derive private input authority without trusting the collector's labels."""

    checked_request_sha256 = _digest(request_sha256, "private input request digest")
    if canonical_sha256(request) != checked_request_sha256:
        raise ProductInputResolutionError(
            "private input request differs from its canonical digest"
        )
    entries = _catalog_entries(catalog)
    selected, paths = _selected_entries(request, entries)
    checked = load_product_input_object_inventory(canonical_bytes(inventory))
    product_id = checked["product"]["id"]
    manifest = selected.get(product_id)
    if manifest is None:
        raise ProductInputResolutionError(
            "private input inventory names an unselected product"
        )
    expected_product = {
        "id": product_id,
        "manifest_path": paths[product_id],
        "manifest_sha256": canonical_sha256(manifest),
        "architecture": manifest["architecture"],
    }
    if checked["product"] != expected_product:
        raise ProductInputResolutionError(
            "private input product authority differs from selected manifest"
        )
    source = _source(request.get("build_source"), "private input request source")
    target = _exact(
        request.get("target_abi"),
        frozenset({"version", "snapshot_sha256"}),
        "private input request ABI",
    )
    policy_sha256, dev_shell_lock_sha256 = _runtime_identity(
        runtime_bundle, request
    )
    if (
        checked["source"] != source
        or checked["target_abi"] != target
        or checked["build_environment"]
        != {
            "policy_sha256": policy_sha256,
            "dev_shell_lock_sha256": dev_shell_lock_sha256,
        }
    ):
        raise ProductInputResolutionError(
            "private input inventory differs from exact request/runtime authority"
        )
    private_root = _checked_authority_root(object_root, "private input object root")
    exact_source_root = _checked_authority_root(source_root, "exact source root")
    exact_runtime_root = _checked_authority_root(runtime_root, "exact runtime root")
    expected = _manifest_private_input_authority(manifest)
    actual = {item["id"]: item for item in checked["objects"]}
    if set(actual) != set(expected):
        raise ProductInputResolutionError(
            "private input object closure differs from selected manifest"
        )
    for input_id in sorted(expected):
        item = actual[input_id]
        authority = expected[input_id]
        compared_fields = set(authority) - {"manifest_sha256"}
        if any(item.get(field) != authority[field] for field in compared_fields):
            raise ProductInputResolutionError(
                f"private input {input_id} labels differ from selected manifest"
            )
        path, metadata = _checked_input_object_file(private_root, item)
        if item["kind"] == "source-archive":
            if item["sha256"] != authority["manifest_sha256"]:
                raise ProductInputResolutionError(
                    f"private input {input_id} differs from pinned archive digest"
                )
        elif item["kind"] == "toolchain-output":
            if item["adapter"] != "toolchain-directory-zip-v1":
                raise ProductInputResolutionError(
                    f"private input {input_id} toolchain adapter differs"
                )
            _validate_toolchain_directory_archive(
                path,
                item=item,
                runtime_bundle=runtime_bundle,
                runtime_root=exact_runtime_root,
            )
        elif item["kind"] == "repository-path":
            if item["adapter"] != "repository-path-bundle-v1":
                raise ProductInputResolutionError(
                    f"private input {input_id} repository adapter differs"
                )
            if metadata.st_size > MAX_REPOSITORY_BUNDLE_BYTES or path.read_bytes() != _expected_repository_bundle(
                source_root=exact_source_root,
                source=source,
                paths=item["paths"],
            ):
                raise ProductInputResolutionError(
                    f"private input {input_id} repository bundle differs from exact source"
                )
        elif item["selector_kind"] == "source-role":
            if item["adapter"] != "package-source-role-zip-v1":
                raise ProductInputResolutionError(
                    f"private input {input_id} source-role adapter differs"
                )
            _validate_package_directory_archive(
                path, root=item["selector"], field=f"private input {input_id}"
            )
        elif item["adapter"] == "package-output-directory-zip-v1":
            _validate_package_directory_archive(
                path, root=item["selector"], field=f"private input {input_id}"
            )
        elif item["adapter"] != "package-output-file-v1":
            raise ProductInputResolutionError(
                f"private input {input_id} package adapter differs"
            )
    return checked


def resolver_artifacts_from_input_inventory(
    inventory: Mapping[str, Any],
) -> tuple[
    tuple[PackageArtifactV1, ...],
    tuple[ArchiveArtifactV1, ...],
    tuple[ToolchainArtifactV1, ...],
    tuple[RepositoryArtifactV1, ...],
]:
    """Reconstruct resolver facts only from one checked object inventory."""

    checked = load_product_input_object_inventory(canonical_bytes(inventory))
    product = checked["product"]
    source = checked["source"]
    target = checked["target_abi"]
    environment = checked["build_environment"]
    candidate_repository = (
        "ghcr.io/kandelo-dev/homebrew-tap-core-abi-"
        f"{target['version']}-candidates/products/{product['id']}"
    )

    def reference(item: Mapping[str, Any]) -> str:
        if item["declared_materialization"] == "lazy":
            return f"{candidate_repository}@sha256:{item['sha256']}"
        return (
            "urn:kandelo:abi-staging:product-input:"
            f"{item['id']}:sha256:{item['sha256']}"
        )

    packages = []
    archives = []
    toolchains = []
    repositories = []
    for item in checked["objects"]:
        common = {
            "sha256": item["sha256"],
            "bytes": item["bytes"],
            "immutable_reference": reference(item),
        }
        if item["kind"] == "package-output":
            packages.append(
                PackageArtifactV1(
                    package=item["package"],
                    selector_kind=item["selector_kind"],
                    selector=item["selector"],
                    architecture=item["architecture"],
                    target_abi=target["version"],
                    snapshot_sha256=target["snapshot_sha256"],
                    source_repository=source["repository"],
                    source_commit=source["commit"],
                    source_tree=source["tree"],
                    build_policy_sha256=environment["policy_sha256"],
                    **common,
                )
            )
        elif item["kind"] == "source-archive":
            archives.append(
                ArchiveArtifactV1(
                    product_id=product["id"],
                    id=item["archive_id"],
                    url=item["url"],
                    **common,
                )
            )
        elif item["kind"] == "toolchain-output":
            toolchains.append(
                ToolchainArtifactV1(
                    product_id=product["id"],
                    id=item["toolchain_id"],
                    provider=item["provider"],
                    component=item["component"],
                    architecture=item["architecture"],
                    source_repository=source["repository"],
                    source_commit=source["commit"],
                    source_tree=source["tree"],
                    dev_shell_lock_sha256=environment["dev_shell_lock_sha256"],
                    build_policy_sha256=environment["policy_sha256"],
                    **common,
                )
            )
        else:
            repositories.append(
                RepositoryArtifactV1(
                    product_id=product["id"],
                    id=item["repository_id"],
                    paths=tuple(item["paths"]),
                    architecture=item["architecture"],
                    source_repository=source["repository"],
                    source_commit=source["commit"],
                    source_tree=source["tree"],
                    **common,
                )
            )
    return (
        tuple(packages),
        tuple(archives),
        tuple(toolchains),
        tuple(repositories),
    )


def validate_private_product_authority_handoff(
    root: Path,
    *,
    expected_request_sha256: str,
    expected_work_id: str,
    expected_product: Mapping[str, Any],
    expected_runtime_artifact_id: int,
    expected_runtime_artifact_digest: str,
    expected_runtime_bundle_sha256: str,
    max_files: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Validate the exact private-only object closure from an untrusted job."""

    expected_request = _digest(
        expected_request_sha256, "private authority expected request"
    )
    expected_work = _digest(
        expected_work_id, "private authority expected work ID"
    )
    product = _exact(
        expected_product,
        frozenset({"id", "manifest_sha256", "output"}),
        "private authority expected product",
    )
    _stable_id(product["id"], "private authority expected product ID")
    _digest(product["manifest_sha256"], "private authority expected manifest")
    _product_output(product["output"], "private authority expected output")
    artifact_id = _integer(
        expected_runtime_artifact_id,
        "private authority expected runtime artifact ID",
        positive=True,
    )
    artifact_digest = _digest(
        expected_runtime_artifact_digest,
        "private authority expected runtime artifact digest",
    )
    runtime_sha256 = _digest(
        expected_runtime_bundle_sha256,
        "private authority expected runtime bundle",
    )
    if (
        isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or not 3 <= max_files <= MAX_PRODUCT_HANDOFF_FILES
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 1
    ):
        raise ProductInputResolutionError(
            "private product authority bounds are invalid"
        )
    authority_root = _checked_authority_root(root, "private product authority root")
    try:
        authority_body = (authority_root / "authority.json").read_bytes()
        inventory_body = (authority_root / "inputs/artifacts.json").read_bytes()
    except OSError as error:
        raise ProductInputResolutionError(
            f"private product authority control file is unavailable: {error}"
        ) from error
    authority = _exact(
        _load_product_canonical(authority_body, "private product authority"),
        frozenset(
            {
                "schema",
                "kind",
                "request_sha256",
                "work_id",
                "product",
                "runtime_artifact",
                "runtime_bundle_sha256",
                "outcome",
                "guard_code",
                "input_inventory_sha256",
                "resolved_inputs_sha256",
            }
        ),
        "private product authority",
    )
    authority_product = _exact(
        authority["product"],
        frozenset({"id", "manifest_sha256", "output"}),
        "private product authority product",
    )
    runtime_artifact = _exact(
        authority["runtime_artifact"],
        frozenset({"id", "digest"}),
        "private product authority runtime artifact",
    )
    input_inventory_sha256 = _digest(
        authority["input_inventory_sha256"],
        "private product authority input inventory",
    )
    resolved_inputs_sha256 = _digest(
        authority["resolved_inputs_sha256"],
        "private product authority resolved inputs",
    )
    if (
        authority["schema"] != 1
        or authority["kind"]
        != "kandelo-abi-staging-private-product-authority"
        or authority["request_sha256"] != expected_request
        or authority["work_id"] != expected_work
        or dict(authority_product) != dict(product)
        or runtime_artifact["id"] != artifact_id
        or runtime_artifact["digest"] != artifact_digest
        or authority["runtime_bundle_sha256"] != runtime_sha256
        or authority["outcome"] != "success"
        or authority["guard_code"] is not None
        or input_inventory_sha256 != hashlib.sha256(inventory_body).hexdigest()
    ):
        raise ProductInputResolutionError(
            "private product authority differs from protected workflow scope"
        )
    inventory = load_product_input_object_inventory(inventory_body)
    if (
        inventory["product"]["id"] != product["id"]
        or inventory["product"]["manifest_sha256"]
        != product["manifest_sha256"]
    ):
        raise ProductInputResolutionError(
            "private product inventory differs from protected product scope"
        )

    expected_files = {
        "authority.json",
        "inputs/artifacts.json",
        *(item["path"] for item in inventory["objects"]),
    }
    expected_directories = {"inputs", "inputs/objects"}
    for relative in expected_files:
        path = PurePosixPath(relative)
        for parent in path.parents:
            if parent.as_posix() not in {".", ""}:
                expected_directories.add(parent.as_posix())
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    total = 0
    try:
        candidates = sorted(
            authority_root.rglob("*"),
            key=lambda value: value.relative_to(authority_root).as_posix(),
        )
    except OSError as error:
        raise ProductInputResolutionError(
            f"cannot enumerate private product authority: {error}"
        ) from error
    for candidate in candidates:
        relative = candidate.relative_to(authority_root).as_posix()
        _relative_path(relative, "private product authority path")
        try:
            metadata = candidate.lstat()
        except OSError as error:
            raise ProductInputResolutionError(
                f"cannot inspect private product authority member {relative}: {error}"
            ) from error
        if stat.S_ISDIR(metadata.st_mode):
            observed_directories.add(relative)
            continue
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 1
        ):
            raise ProductInputResolutionError(
                f"private product authority member {relative} is not one file"
            )
        observed_files.add(relative)
        total += metadata.st_size
    if (
        observed_files != expected_files
        or observed_directories != expected_directories
        or len(observed_files) > max_files
        or total > max_bytes
    ):
        raise ProductInputResolutionError(
            "private product authority file closure is unexpected"
        )
    return {
        "authority": dict(authority),
        "inventory": inventory,
        "inventory_body": inventory_body,
        "resolved_inputs_sha256": resolved_inputs_sha256,
    }


def resolve_product_from_checked_input_authority(
    checked_inventory: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    request_sha256: str,
    catalog: Mapping[str, Any],
    tap_plan: Mapping[str, Any],
    records: SchedulingRecordsV1,
    candidate_records: Mapping[str, Mapping[str, Any]],
    candidate_locators: Mapping[str, Mapping[str, str]],
    source_custody_records: Mapping[str, Mapping[str, Any]],
    reuse_records: Mapping[str, Mapping[str, Any]],
    verification_records: Mapping[str, Mapping[str, Any]],
    verification_locators: Mapping[str, Mapping[str, str]],
    verification_tests: tuple[VerificationTestDefinitionV1, ...],
    runtime_bundle: Mapping[str, Any],
    product_artifacts: tuple[CandidateProductArtifactV1, ...],
) -> ResolvedProductPlanV1:
    """Wire independently checked private bytes into the canonical resolver."""

    inventory = load_product_input_object_inventory(
        canonical_bytes(checked_inventory)
    )
    packages, archives, toolchains, repositories = (
        resolver_artifacts_from_input_inventory(inventory)
    )
    resolutions = resolve_product_inputs(
        request=request,
        request_sha256=request_sha256,
        catalog=catalog,
        tap_plan=tap_plan,
        records=records,
        candidate_records=candidate_records,
        candidate_locators=candidate_locators,
        source_custody_records=source_custody_records,
        reuse_records=reuse_records,
        verification_records=verification_records,
        verification_locators=verification_locators,
        verification_tests=verification_tests,
        runtime_bundle=runtime_bundle,
        product_artifacts=product_artifacts,
        package_artifacts=packages,
        archive_artifacts=archives,
        toolchain_artifacts=toolchains,
        repository_artifacts=repositories,
        target_product_ids=(inventory["product"]["id"],),
    )
    if len(resolutions) != 1:
        raise ProductInputResolutionError(
            "checked private authority did not resolve one exact product"
        )
    return resolutions[0]


def validate_product_build_handoff(
    root: Path,
    *,
    expected_product_id: str,
    expected_work_id: str,
    expected_request_sha256: str,
    expected_runtime_bundle_sha256: str,
    max_files: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Validate one inert composition result against protected external scope."""

    _stable_id(expected_product_id, "expected product ID")
    _digest(expected_work_id, "expected product work ID")
    _digest(expected_request_sha256, "expected product request")
    _digest(expected_runtime_bundle_sha256, "expected product runtime")
    if (
        isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or not 1 <= max_files <= MAX_PRODUCT_HANDOFF_FILES
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 1
    ):
        raise ProductInputResolutionError(
            "product handoff validation bounds are invalid"
        )
    files = _product_handoff_files(root)
    if not 1 <= len(files) <= max_files:
        raise ProductInputResolutionError(
            "product handoff file count is outside its bound"
        )
    if sum(metadata.st_size for _, _, metadata in files) > max_bytes:
        raise ProductInputResolutionError(
            "product handoff byte count exceeds its bound"
        )
    by_path = {relative: (path, metadata) for relative, path, metadata in files}
    required_control = {
        "diagnostics/summary.txt",
        "inventory.json",
        "product-build-result.json",
        "runtime-bundle.json",
    }
    if not required_control.issubset(by_path):
        raise ProductInputResolutionError(
            "product handoff lacks its required control files"
        )

    def read_body(relative: str, maximum: int | None = None) -> bytes:
        path, metadata = by_path[relative]
        if maximum is not None and metadata.st_size > maximum:
            raise ProductInputResolutionError(
                f"product handoff file {relative} exceeds its byte bound"
            )
        try:
            body = path.read_bytes()
        except OSError as error:
            raise ProductInputResolutionError(
                f"cannot read product handoff file {relative}: {error}"
            ) from error
        if len(body) != metadata.st_size:
            raise ProductInputResolutionError(
                f"product handoff file {relative} changed while reading"
            )
        return body

    result = load_product_build_result(
        read_body("product-build-result.json", MAX_PRODUCT_HANDOFF_JSON_BYTES)
    )
    product = result["product"]
    if (
        product["id"] != expected_product_id
        or result["work_id"] != expected_work_id
        or result["request_sha256"] != expected_request_sha256
        or result["runtime_bundle_sha256"] != expected_runtime_bundle_sha256
    ):
        raise ProductInputResolutionError(
            "product handoff differs from its protected work scope"
        )
    output = product["output"]
    inventory = _load_product_handoff_inventory(
        read_body("inventory.json", MAX_PRODUCT_HANDOFF_JSON_BYTES), output=output
    )
    if (
        inventory["product_id"] != expected_product_id
        or inventory["work_id"] != expected_work_id
        or inventory["outcome"] != result["outcome"]
    ):
        raise ProductInputResolutionError(
            "product handoff inventory differs from its terminal result"
        )
    inventory_paths = {item["path"] for item in inventory["files"]}
    actual_paths = set(by_path) - {"inventory.json"}
    if inventory_paths != actual_paths:
        raise ProductInputResolutionError(
            "product handoff inventory differs from its files"
        )
    inventory_by_path = {item["path"]: item for item in inventory["files"]}
    for relative in sorted(actual_paths):
        path, metadata = by_path[relative]
        if _product_file_identity(path, metadata) != {
            "sha256": inventory_by_path[relative]["sha256"],
            "bytes": inventory_by_path[relative]["bytes"],
        }:
            raise ProductInputResolutionError(
                f"product handoff file {relative} differs from inventory"
            )
    runtime_identity = _product_file_identity(*by_path["runtime-bundle.json"])
    if runtime_identity["sha256"] != expected_runtime_bundle_sha256:
        raise ProductInputResolutionError(
            "product handoff runtime bundle differs from protected identity"
        )
    diagnostics_identity = _product_file_identity(*by_path["diagnostics/summary.txt"])
    if diagnostics_identity["sha256"] != result["diagnostic_summary_sha256"]:
        raise ProductInputResolutionError(
            "product handoff diagnostics differ from terminal result"
        )
    expected_paths = set(required_control)
    if result["resolved_inputs_sha256"] is not None:
        expected_paths.add("resolved-inputs.json")
    if result["outcome"] == "success":
        expected_paths.update({"builder-report.json", output})
    if result["resolved_inputs_sha256"] is not None:
        resolved_body = read_body("resolved-inputs.json", MAX_DOCUMENT_BYTES)
        if hashlib.sha256(resolved_body).hexdigest() != result["resolved_inputs_sha256"]:
            raise ProductInputResolutionError(
                "product handoff resolved inputs differ from terminal result"
            )
        resolved = load_resolved_product_inputs(resolved_body)
        if (
            resolved["product"]["id"] != expected_product_id
            or resolved["product"]["manifest_sha256"]
            != product["manifest_sha256"]
            or resolved["product"]["output"] != output
        ):
            raise ProductInputResolutionError(
                "product handoff resolved product differs from terminal result"
            )
    if set(by_path) != expected_paths:
        raise ProductInputResolutionError(
            "product handoff files contradict its terminal outcome"
        )
    if result["outcome"] == "success":
        report_identity = _product_file_identity(*by_path["builder-report.json"])
        vfs_identity = _product_file_identity(*by_path[output])
        if (
            report_identity["sha256"] != result["builder_report_sha256"]
            or vfs_identity != result["vfs"]
        ):
            raise ProductInputResolutionError(
                "product handoff success artifacts differ from terminal result"
            )
    return result
