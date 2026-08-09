"""Resolve request-selected VFS products from exact, nonendorsed inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re
from typing import Any, Literal

from .canonical import CanonicalJsonError, canonical_bytes, canonical_sha256, parse_canonical_bytes
from .contract import validate_candidate_reuse_record
from .custody import CustodyError, load_source_custody_manifest
from .plan import exact_formula_subject, validate_tap_plan
from .policy import VerificationTestDefinitionV1
from .records import validate_candidate_record
from .scheduler import CandidateFactV1, SchedulingRecordsV1, VerificationFactV1
from .verification import validate_verification_receipt_record


SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
CANDIDATE_NAMESPACE = re.compile(r"homebrew-tap-core-abi-([0-9]+)-candidates/")
CANONICAL_NAMESPACE = re.compile(r"homebrew-tap-core-abi-([0-9]+)/")
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
    if kind in {"homebrew-bottle", "product-image"}:
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
        frozenset({"bundle_sha256", "bytes", "service_worker_sha256"}),
        "runtime browser",
    )
    for field in ("bundle_sha256", "generated_abi_sha256", "worker_protocol_sha256"):
        _digest(host[field], f"runtime host {field}")
    _integer(host["bytes"], "runtime host bytes", positive=True)
    for field in ("bundle_sha256", "service_worker_sha256"):
        _digest(browser[field], f"runtime browser {field}")
    _integer(browser["bytes"], "runtime browser bytes", positive=True)
    policy_sha256 = _digest(root["build_policy_sha256"], "runtime build policy")
    dev_shell = []
    previous = ""
    for value in _sequence(root["inventory"], "runtime inventory"):
        item = _exact(value, frozenset({"path", "sha256", "bytes"}), "runtime inventory item")
        path = _relative_path(item["path"], "runtime inventory path")
        if path <= previous:
            raise ProductInputResolutionError("runtime inventory is not sorted and unique")
        previous = path
        digest = _digest(item["sha256"], "runtime inventory digest")
        _integer(item["bytes"], "runtime inventory bytes", positive=True)
        if path == "flake.lock":
            dev_shell.append(digest)
    if len(dev_shell) != 1:
        raise ProductInputResolutionError("runtime inventory lacks one exact flake.lock")
    return policy_sha256, dev_shell[0]


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
) -> tuple[CandidateFactV1, Mapping[str, Any]]:
    contract_sha256 = formula_plan.get("contract_sha256")
    _digest(contract_sha256, "selected Formula contract")
    matches = [
        fact
        for fact in records.candidates
        if fact.request_sha256 == request_sha256
        and fact.subject == subject
        and fact.contract_sha256 == contract_sha256
    ]
    if not matches:
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
    )


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
    build_policy_sha256, dev_shell_lock_sha256 = _runtime_identity(runtime_bundle, request)
    runtime_bundle_sha256 = canonical_sha256(runtime_bundle)
    formulae, dependencies = _formula_graph(tap_plan)
    product_formula_uses, global_formula_uses = _formula_uses(selected, formulae, dependencies)
    if global_formula_uses and not verification_tests:
        raise ProductInputResolutionError("selected Formulae lack verification policy")

    selected_candidates = {}
    for subject in sorted(global_formula_uses):
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

    claims: list[_ClaimV1] = []
    claims_by_product: dict[str, list[_ClaimV1]] = {product_id: [] for product_id in selected}
    for product_id in order:
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
                    requests=_direct_request(
                        input_id, product_id, "package", root_id, declared
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
        if claim.role == "runtime" and claim.declared == "embedded"
    }
    object_requests: dict[tuple[str, str, int], set[ProductInputRequestV1]] = {}
    for claim in claims:
        object_requests.setdefault(claim.object_key, set()).update(claim.requests)

    results = []
    for product_id in order:
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
                ),
            }
            if effective != "lazy-reference":
                value["path"] = f"inputs/objects/{claim.input_id}-sha256-{claim.sha256}"
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
                "effective_materialization",
                "sha256",
                "bytes",
                "reference",
                "path",
            }
        )
        item = _mapping(value, f"resolved input {index}")
        required = permitted - frozenset({"reference", "path"})
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
        if reference is not None:
            if root["reference_class"] == "candidate":
                _immutable_reference(
                    reference,
                    sha256,
                    f"resolved input {input_id} reference",
                    kind=kind,
                    target_abi=target_abi,
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
