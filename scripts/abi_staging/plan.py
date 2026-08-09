"""Deterministic tap planning derived only from selected VFS product roots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any
from urllib.parse import unquote, urlsplit

from .canonical import CanonicalJsonError, canonical_bytes, canonical_sha256
from .formula_inventory import generate_formula_inventory, load_formula_inventory
from .request import RequestValidationError, parse_request_asset_name


MAX_REQUIREMENTS_BYTES = 4 * 1024 * 1024
MAX_GRAPH_NODES = 8_192
MAX_GRAPH_EDGES = 65_536
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
ARCHITECTURES = frozenset({"wasm32", "wasm64"})
MATERIALIZATIONS = frozenset({"embedded", "lazy"})
REQUEST_ASSET_HOSTS = frozenset(
    {"github.com", "release-assets.githubusercontent.com"}
)

PLAN_KEYS = frozenset(
    {
        "schema",
        "kind",
        "request_digest",
        "request_asset_url",
        "tap_source",
        "target_abi",
        "selected_products",
        "formulae",
        "graph_sha256",
        "required_subjects",
        "background_subjects",
    }
)
TAP_SOURCE_KEYS = frozenset({"repository", "commit", "tree"})
SELECTED_PRODUCT_KEYS = frozenset(
    {"id", "path", "manifest_sha256", "formula_roots"}
)
FORMULA_ROOT_KEYS = frozenset(
    {"tap", "formula", "architecture", "materialization"}
)
FORMULA_PLAN_KEYS = frozenset(
    {
        "identity",
        "direct_dependencies",
        "required_by_products",
        "work_class",
        "capture",
        "contract_sha256",
    }
)
FORMULA_IDENTITY_KEYS = frozenset(
    {
        "name",
        "version",
        "revision",
        "rebuild",
        "architecture",
        "formula_path",
        "normalized_formula_sha256",
    }
)
FORMULA_DEPENDENCY_KEYS = frozenset(
    {"formula", "architecture", "materialization_policy_sha256"}
)
CAPTURE_KEYS = frozenset(
    {
        "capture_policy_sha256",
        "normalized_source_sha256",
        "tap_input_components",
        "sources",
        "native_requirements",
    }
)
REQUIREMENT_KEYS = frozenset({"tap", "formula", "architecture", "uses"})
USE_KEYS = frozenset({"product_id", "materialization"})


class PlanError(ValueError):
    """Raised when selected products cannot produce one exact tap plan."""


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise PlanError(
            f"{field} fields changed: missing={sorted(expected - actual)!r} "
            f"extra={sorted(actual - expected)!r}"
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise PlanError(f"{field} must be an array")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise PlanError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise PlanError(f"{field} is not UTF-8") from error
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise PlanError(f"{field} is outside its string bound")
    return value


def _stable_id(value: Any, field: str) -> str:
    result = _text(value, field, 128)
    if STABLE_ID.fullmatch(result) is None:
        raise PlanError(f"{field} is not a stable identifier")
    return result


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise PlanError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise PlanError(f"{field} is not a full lowercase Git SHA")
    return value


def _architecture(value: Any, field: str) -> str:
    if not isinstance(value, str) or value not in ARCHITECTURES:
        raise PlanError(f"{field} is not a supported architecture")
    return value


def _materialization(value: Any, field: str) -> str:
    if not isinstance(value, str) or value not in MATERIALIZATIONS:
        raise PlanError(f"{field} is not embedded or lazy")
    return value


def _relative_path(value: Any, field: str) -> str:
    result = _text(value, field)
    if (
        result.startswith("/")
        or "\\" in result
        or any(part in {"", ".", "..", ".git"} for part in result.split("/"))
    ):
        raise PlanError(f"{field} is not an exact repository-relative path")
    return result


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**32 - 1:
        raise PlanError(f"{field} must be a bounded nonnegative integer")
    return value


def exact_formula_subject(name: str, architecture: str) -> str:
    _stable_id(name, "Formula subject identity")
    _architecture(architecture, "Formula subject architecture")
    return canonical_bytes(
        {"architecture": architecture, "identity": name, "kind": "formula"}
    ).decode("utf-8").strip()


def _parse_formula_subject(value: Any, field: str) -> tuple[str, str]:
    subject = _text(value, field, 512)
    try:
        parsed = json.loads(subject)
    except json.JSONDecodeError as error:
        raise PlanError(f"{field} is not canonical subject JSON: {error}") from error
    candidate = _mapping(parsed, field)
    _exact_keys(candidate, frozenset({"architecture", "identity", "kind"}), field)
    if candidate["kind"] != "formula":
        raise PlanError(f"{field} is outside the Formula subject namespace")
    name = _stable_id(candidate["identity"], f"{field} identity")
    architecture = _architecture(candidate["architecture"], f"{field} architecture")
    if subject != exact_formula_subject(name, architecture):
        raise PlanError(f"{field} is not canonical")
    return name, architecture


def _plain_json(body: bytes, *, maximum: int, field: str) -> Any:
    if not isinstance(body, bytes) or not body or len(body) > maximum:
        raise PlanError(f"{field} is outside its byte bound")

    def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PlanError(f"{field} repeats object key {key!r}")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=object_without_duplicates,
        )
        if canonical_bytes(parsed) != body:
            raise PlanError(f"{field} is not canonical JSON")
    except (UnicodeDecodeError, json.JSONDecodeError, CanonicalJsonError) as error:
        if isinstance(error, PlanError):
            raise
        raise PlanError(f"{field} is invalid: {error}") from error
    return parsed


def load_formula_requirements(body: bytes) -> list[dict[str, Any]]:
    parsed = _plain_json(body, maximum=MAX_REQUIREMENTS_BYTES, field="Formula requirements")
    requirements = _sequence(parsed, "Formula requirements")
    result = [dict(_mapping(item, f"Formula requirement {index}")) for index, item in enumerate(requirements)]
    _validate_formula_requirements_shape(result)
    return result


def _validate_formula_requirements_shape(
    requirements: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if len(requirements) > MAX_GRAPH_NODES:
        raise PlanError("Formula root requirements exceed their bound")
    result = []
    previous: tuple[str, str, str] | None = None
    for index, candidate in enumerate(requirements):
        requirement = _mapping(candidate, f"Formula requirement {index}")
        _exact_keys(requirement, REQUIREMENT_KEYS, f"Formula requirement {index}")
        tap = _text(requirement["tap"], f"Formula requirement tap {index}", 256)
        if REPOSITORY.fullmatch(tap) is None:
            raise PlanError("Formula requirement tap is not owner/repository")
        formula = _stable_id(requirement["formula"], f"Formula requirement name {index}")
        architecture = _architecture(
            requirement["architecture"], f"Formula requirement architecture {index}"
        )
        key = (tap, formula, architecture)
        if previous is not None and previous >= key:
            raise PlanError("Formula root requirements must be sorted and duplicate-free")
        previous = key
        uses = []
        previous_use: tuple[str, str] | None = None
        for use_index, candidate_use in enumerate(
            _sequence(requirement["uses"], f"Formula requirement uses for {formula}")
        ):
            use = _mapping(candidate_use, f"Formula use {use_index} for {formula}")
            _exact_keys(use, USE_KEYS, f"Formula use {use_index} for {formula}")
            product = _stable_id(use["product_id"], f"Formula use product for {formula}")
            materialization = _materialization(
                use["materialization"], f"Formula use materialization for {formula}"
            )
            use_key = (product, materialization)
            if previous_use is not None and previous_use >= use_key:
                raise PlanError(f"Formula uses for {formula} must be sorted and duplicate-free")
            previous_use = use_key
            uses.append({"product_id": product, "materialization": materialization})
        if not uses:
            raise PlanError(f"Formula root {formula} must name at least one selected product")
        result.append(
            {
                "tap": tap,
                "formula": formula,
                "architecture": architecture,
                "uses": uses,
            }
        )
    return result


def _inventory_entries(inventory: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if inventory.get("schema") != 1 or inventory.get("kind") != "kandelo-protected-formula-inventory":
        raise PlanError("Formula inventory protocol is unsupported")
    entries = [
        dict(_mapping(candidate, f"Formula inventory entry {index}"))
        for index, candidate in enumerate(_sequence(inventory.get("formulae"), "Formula inventory entries"))
    ]
    if not entries or len(entries) * len(ARCHITECTURES) > MAX_GRAPH_NODES:
        raise PlanError("Formula inventory size is invalid")
    names = []
    by_name: dict[str, dict[str, Any]] = {}
    graph_identity = []
    for entry in entries:
        name = _stable_id(entry.get("name"), "Formula inventory name")
        names.append(name)
        if name in by_name:
            raise PlanError(f"Formula inventory repeats {name}")
        architectures = list(_sequence(entry.get("architectures"), f"architectures for {name}"))
        if architectures != sorted(set(architectures)) or not architectures:
            raise PlanError(f"Formula {name} architectures are not sorted and unique")
        for architecture in architectures:
            _architecture(architecture, f"architecture for {name}")
        dependencies = []
        previous_dependency = ""
        for candidate in _sequence(entry.get("target_dependencies"), f"dependencies for {name}"):
            dependency = _mapping(candidate, f"dependency for {name}")
            if frozenset(dependency) != {"name", "scopes"}:
                raise PlanError(f"Formula {name} dependency fields changed")
            dependency_name = _stable_id(dependency["name"], f"dependency for {name}")
            if dependency_name <= previous_dependency:
                raise PlanError(f"Formula {name} dependencies are not sorted and unique")
            previous_dependency = dependency_name
            dependencies.append(dependency_name)
        by_name[name] = entry
        graph_identity.append({"name": name, "target_dependencies": dependencies})
    if names != sorted(names):
        raise PlanError("Formula inventory entries must be sorted")
    unknown = sorted(
        {
            dependency
            for identity in graph_identity
            for dependency in identity["target_dependencies"]
            if dependency not in by_name
        }
    )
    if unknown:
        raise PlanError(f"Formula inventory names unknown first-party dependencies {unknown!r}")
    if canonical_sha256(graph_identity) != inventory.get("graph_sha256"):
        raise PlanError("Formula inventory graph digest is stale")
    return entries, by_name


def _subject_graph(
    entries: Sequence[Mapping[str, Any]], by_name: Mapping[str, Mapping[str, Any]]
) -> dict[tuple[str, str], list[tuple[str, str]]]:
    graph: dict[tuple[str, str], list[tuple[str, str]]] = {}
    edge_count = 0
    for entry in entries:
        name = entry["name"]
        for architecture in entry["architectures"]:
            dependencies = []
            for dependency in entry["target_dependencies"]:
                dependency_name = dependency["name"]
                if architecture not in by_name[dependency_name]["architectures"]:
                    raise PlanError(
                        f"Formula {name} architecture {architecture} requires "
                        f"{dependency_name}, which lacks that architecture"
                    )
                dependencies.append((dependency_name, architecture))
            dependencies.sort(key=lambda item: exact_formula_subject(*item))
            graph[(name, architecture)] = dependencies
            edge_count += len(dependencies)
    if edge_count > MAX_GRAPH_EDGES:
        raise PlanError("Formula dependency graph exceeds its edge bound")
    return graph


def _topological_order(
    graph: Mapping[tuple[str, str], Sequence[tuple[str, str]]]
) -> list[tuple[str, str]]:
    state: dict[tuple[str, str], int] = {}
    levels: dict[tuple[str, str], int] = {}

    def visit(subject: tuple[str, str]) -> int:
        status = state.get(subject, 0)
        if status == 1:
            raise PlanError(f"Formula graph contains a cycle through {subject[0]}")
        if status == 2:
            return levels[subject]
        state[subject] = 1
        dependencies = graph.get(subject)
        if dependencies is None:
            raise PlanError(f"Formula graph omits subject {subject!r}")
        level = 0
        if dependencies:
            level = max(visit(dependency) + 1 for dependency in dependencies)
        state[subject] = 2
        levels[subject] = level
        return level

    for subject in graph:
        visit(subject)
    return sorted(graph, key=lambda item: (levels[item], exact_formula_subject(*item)))


def _materialization_policy_sha256(
    subject: tuple[str, str], uses: set[tuple[str, str]], work_class: str
) -> str:
    return canonical_sha256(
        {
            "schema": 1,
            "kind": "kandelo-formula-materialization-policy",
            "subject": json.loads(exact_formula_subject(*subject)),
            "work_class": work_class,
            "uses": [
                {"product_id": product, "materialization": materialization}
                for product, materialization in sorted(uses)
            ],
        }
    )


def _request_products(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    requirements = _mapping(request.get("requirements"), "request requirements")
    products = []
    previous = ""
    for index, candidate in enumerate(_sequence(requirements.get("products"), "request products")):
        product = _mapping(candidate, f"request product {index}")
        _exact_keys(product, frozenset({"id", "path", "manifest_sha256"}), f"request product {index}")
        product_id = _stable_id(product["id"], f"request product ID {index}")
        if product_id <= previous:
            raise PlanError("request products must be sorted and duplicate-free")
        previous = product_id
        products.append(
            {
                "id": product_id,
                "path": _relative_path(product["path"], f"request product path {index}"),
                "manifest_sha256": _digest(
                    product["manifest_sha256"], f"request product digest {index}"
                ),
            }
        )
    if not products:
        raise PlanError("request must bind at least one selected product")
    identity = {
        "change_classes": requirements.get("change_classes"),
        "products": requirements.get("products"),
        "registries": requirements.get("registries"),
        "evidence": requirements.get("evidence"),
    }
    if canonical_sha256(identity) != requirements.get("digest"):
        raise PlanError("request requirements digest is stale")
    return products


def _validate_request_binding(
    request: Mapping[str, Any], request_digest: str, request_asset_url: str
) -> None:
    _digest(request_digest, "request digest")
    if canonical_sha256(request) != request_digest:
        raise PlanError("request digest does not match canonical request bytes")
    source = _mapping(request.get("build_source"), "request build source")
    head = _git_sha(source.get("commit"), "request exact head")
    url = _text(request_asset_url, "request asset URL", 8192)
    parsed_url = urlsplit(url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname not in REQUEST_ASSET_HOSTS
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.port is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise PlanError("request asset URL is not one exact HTTPS asset")
    try:
        asset = parse_request_asset_name(unquote(Path(parsed_url.path).name))
    except RequestValidationError as error:
        raise PlanError(f"request asset URL is invalid: {error}") from error
    if asset.head != head or asset.digest != request_digest:
        raise PlanError("request asset URL does not bind the exact head and request digest")


def _validate_tap_source(source: Mapping[str, Any]) -> dict[str, str]:
    _exact_keys(source, TAP_SOURCE_KEYS, "tap source")
    repository = _text(source["repository"], "tap repository", 256)
    if REPOSITORY.fullmatch(repository) is None:
        raise PlanError("tap repository is not owner/repository")
    return {
        "repository": repository,
        "commit": _git_sha(source["commit"], "tap commit"),
        "tree": _git_sha(source["tree"], "tap tree"),
    }


def plan_request(
    request: Mapping[str, Any],
    *,
    request_digest: str,
    request_asset_url: str,
    tap_source: Mapping[str, str],
    inventory: Mapping[str, Any],
    formula_requirements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build one immutable plan from request-bound VFS Formula roots."""

    request_value = _mapping(request, "ABI staging request")
    _validate_request_binding(request_value, request_digest, request_asset_url)
    source = _validate_tap_source(_mapping(tap_source, "tap source"))
    products = _request_products(request_value)
    product_ids = {product["id"] for product in products}
    requirements = _validate_formula_requirements_shape(formula_requirements)
    entries, by_name = _inventory_entries(_mapping(inventory, "Formula inventory"))
    graph = _subject_graph(entries, by_name)
    order = _topological_order(graph)

    roots_by_product: dict[str, list[dict[str, str]]] = {
        product_id: [] for product_id in product_ids
    }
    propagated_uses: dict[tuple[str, str], set[tuple[str, str]]] = {
        subject: set() for subject in graph
    }

    def propagate(subject: tuple[str, str], use: tuple[str, str]) -> None:
        if use in propagated_uses[subject]:
            return
        propagated_uses[subject].add(use)
        for dependency in graph[subject]:
            propagate(dependency, use)

    for requirement in requirements:
        if requirement["tap"] != source["repository"]:
            raise PlanError("selected Formula root addresses a third-party tap")
        subject = (requirement["formula"], requirement["architecture"])
        if subject not in graph:
            if requirement["formula"] not in by_name:
                raise PlanError(f"selected Formula root {requirement['formula']} is missing")
            raise PlanError(
                f"selected Formula root {requirement['formula']} lacks "
                f"architecture {requirement['architecture']}"
            )
        for use in requirement["uses"]:
            product_id = use["product_id"]
            if product_id not in product_ids:
                raise PlanError(f"Formula root names unselected product {product_id}")
            materialization = use["materialization"]
            root = {
                "tap": requirement["tap"],
                "formula": requirement["formula"],
                "architecture": requirement["architecture"],
                "materialization": materialization,
            }
            roots_by_product[product_id].append(root)
            propagate(subject, (product_id, materialization))

    selected_products = []
    for product in products:
        roots = sorted(
            roots_by_product[product["id"]],
            key=lambda item: (
                item["tap"],
                item["formula"],
                item["architecture"],
                item["materialization"],
            ),
        )
        if len({tuple(root.values()) for root in roots}) != len(roots):
            raise PlanError(f"selected product {product['id']} repeats a Formula root")
        selected_products.append({**product, "formula_roots": roots})

    required_set = {subject for subject, uses in propagated_uses.items() if uses}
    required = [subject for subject in order if subject in required_set]
    background = [subject for subject in order if subject not in required_set]
    graph_identity = [
        {
            "subject": json.loads(exact_formula_subject(*subject)),
            "dependencies": [
                json.loads(exact_formula_subject(*dependency))
                for dependency in graph[subject]
            ],
        }
        for subject in order
    ]
    plans = []
    for subject in required + background:
        name, architecture = subject
        inventory_entry = by_name[name]
        work_class = "required" if subject in required_set else "background"
        direct_dependencies = []
        for dependency in graph[subject]:
            dependency_class = "required" if dependency in required_set else "background"
            direct_dependencies.append(
                {
                    "formula": dependency[0],
                    "architecture": dependency[1],
                    "materialization_policy_sha256": _materialization_policy_sha256(
                        dependency, propagated_uses[dependency], dependency_class
                    ),
                }
            )
        identity = {
            "name": name,
            "version": inventory_entry["version"],
            "revision": inventory_entry["revision"],
            "rebuild": inventory_entry["rebuild"],
            "architecture": architecture,
            "formula_path": inventory_entry["formula_path"],
            "normalized_formula_sha256": inventory_entry["normalized_formula_sha256"],
        }
        capture = {
            "capture_policy_sha256": inventory_entry["capture_policy_sha256"],
            "normalized_source_sha256": inventory_entry["normalized_source_sha256"],
            "tap_input_components": copy.deepcopy(inventory_entry["tap_input_components"]),
            "sources": copy.deepcopy(inventory_entry["sources"]),
            "native_requirements": copy.deepcopy(inventory_entry["native_requirements"]),
        }
        plans.append(
            {
                "identity": identity,
                "direct_dependencies": direct_dependencies,
                "required_by_products": sorted(
                    {product for product, _ in propagated_uses[subject]}
                ),
                "work_class": work_class,
                "capture": capture,
                "contract_sha256": None,
            }
        )
    target = _mapping(request_value.get("target_abi"), "request target ABI")
    if frozenset(target) != {"version", "snapshot_sha256"}:
        raise PlanError("request target ABI fields changed")
    result = {
        "schema": 1,
        "kind": "kandelo-abi-staging-tap-plan",
        "request_digest": request_digest,
        "request_asset_url": request_asset_url,
        "tap_source": source,
        "target_abi": {
            "version": _integer(target["version"], "target ABI version"),
            "snapshot_sha256": _digest(target["snapshot_sha256"], "target ABI snapshot"),
        },
        "selected_products": selected_products,
        "formulae": plans,
        "graph_sha256": canonical_sha256(graph_identity),
        "required_subjects": [exact_formula_subject(*subject) for subject in required],
        "background_subjects": [exact_formula_subject(*subject) for subject in background],
    }
    validate_tap_plan(result)
    return result


def _validate_selected_products(
    value: Any, tap_repository: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    products = []
    requirements_by_key: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    previous = ""
    for index, candidate in enumerate(_sequence(value, "selected products")):
        product = _mapping(candidate, f"selected product {index}")
        _exact_keys(product, SELECTED_PRODUCT_KEYS, f"selected product {index}")
        product_id = _stable_id(product["id"], f"selected product ID {index}")
        if product_id <= previous:
            raise PlanError("selected products must be sorted and duplicate-free")
        previous = product_id
        roots = []
        previous_root: tuple[str, str, str, str] | None = None
        for root_index, root_candidate in enumerate(
            _sequence(product["formula_roots"], f"Formula roots for {product_id}")
        ):
            root = _mapping(root_candidate, f"Formula root {root_index} for {product_id}")
            _exact_keys(root, FORMULA_ROOT_KEYS, f"Formula root {root_index} for {product_id}")
            tap = _text(root["tap"], f"Formula root tap for {product_id}", 256)
            if tap != tap_repository:
                raise PlanError("tap plan contains a root for another tap")
            formula = _stable_id(root["formula"], f"Formula root name for {product_id}")
            architecture = _architecture(
                root["architecture"], f"Formula root architecture for {product_id}"
            )
            materialization = _materialization(
                root["materialization"], f"Formula root materialization for {product_id}"
            )
            root_key = (tap, formula, architecture, materialization)
            if previous_root is not None and previous_root >= root_key:
                raise PlanError(f"Formula roots for {product_id} are not sorted and unique")
            previous_root = root_key
            root_value = {
                "tap": tap,
                "formula": formula,
                "architecture": architecture,
                "materialization": materialization,
            }
            roots.append(root_value)
            requirements_by_key.setdefault((tap, formula, architecture), []).append(
                {"product_id": product_id, "materialization": materialization}
            )
        products.append(
            {
                "id": product_id,
                "path": _relative_path(product["path"], f"selected product path {index}"),
                "manifest_sha256": _digest(
                    product["manifest_sha256"], f"selected product digest {index}"
                ),
                "formula_roots": roots,
            }
        )
    if not products:
        raise PlanError("tap plan must retain selected products")
    requirements = [
        {
            "tap": key[0],
            "formula": key[1],
            "architecture": key[2],
            "uses": sorted(uses, key=lambda item: (item["product_id"], item["materialization"])),
        }
        for key, uses in sorted(requirements_by_key.items())
    ]
    return products, requirements


def _validate_capture(value: Any, formula: str) -> None:
    capture = _mapping(value, f"capture for {formula}")
    _exact_keys(capture, CAPTURE_KEYS, f"capture for {formula}")
    _digest(capture["capture_policy_sha256"], f"capture policy for {formula}")
    _digest(capture["normalized_source_sha256"], f"normalized source for {formula}")
    previous_component = ""
    for candidate in _sequence(capture["tap_input_components"], f"tap components for {formula}"):
        component = _mapping(candidate, f"tap component for {formula}")
        _exact_keys(component, frozenset({"path", "sha256"}), f"tap component for {formula}")
        path = _relative_path(component["path"], f"tap component path for {formula}")
        if path <= previous_component:
            raise PlanError(f"tap components for {formula} are not sorted and unique")
        previous_component = path
        _digest(component["sha256"], f"tap component digest for {formula}")
    for field in ("sources", "native_requirements"):
        values = _sequence(capture[field], f"{field} for {formula}")
        if len(values) > MAX_GRAPH_NODES:
            raise PlanError(f"{field} for {formula} exceeds its bound")
        canonical_bytes(values)


def validate_tap_plan(plan: Mapping[str, Any]) -> None:
    value = _mapping(plan, "tap plan")
    _exact_keys(value, PLAN_KEYS, "tap plan")
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-tap-plan":
        raise PlanError("tap plan protocol is unsupported")
    _digest(value["request_digest"], "tap plan request digest")
    _text(value["request_asset_url"], "tap plan request asset URL", 8192)
    tap_source = _validate_tap_source(_mapping(value["tap_source"], "tap source"))
    target = _mapping(value["target_abi"], "tap plan target ABI")
    _exact_keys(target, frozenset({"version", "snapshot_sha256"}), "tap plan target ABI")
    _integer(target["version"], "tap plan target ABI version")
    _digest(target["snapshot_sha256"], "tap plan ABI snapshot")
    products, requirements = _validate_selected_products(
        value["selected_products"], tap_source["repository"]
    )
    product_ids = {product["id"] for product in products}

    formulae = list(_sequence(value["formulae"], "tap plan Formulae"))
    if not formulae or len(formulae) > MAX_GRAPH_NODES:
        raise PlanError("tap plan Formula inventory size is invalid")
    graph: dict[tuple[str, str], list[tuple[str, str]]] = {}
    formula_by_subject: dict[tuple[str, str], Mapping[str, Any]] = {}
    formula_sequence: list[tuple[str, str]] = []
    edge_count = 0
    for index, candidate in enumerate(formulae):
        formula = _mapping(candidate, f"tap plan Formula {index}")
        _exact_keys(formula, FORMULA_PLAN_KEYS, f"tap plan Formula {index}")
        identity = _mapping(formula["identity"], f"Formula identity {index}")
        _exact_keys(identity, FORMULA_IDENTITY_KEYS, f"Formula identity {index}")
        name = _stable_id(identity["name"], f"Formula name {index}")
        architecture = _architecture(identity["architecture"], f"Formula architecture {index}")
        subject = (name, architecture)
        if subject in formula_by_subject:
            raise PlanError(f"tap plan repeats Formula subject {subject!r}")
        if identity["formula_path"] != f"Formula/{name}.rb":
            raise PlanError(f"Formula path differs from identity for {name}")
        _text(identity["version"], f"Formula version for {name}", 128)
        _integer(identity["revision"], f"Formula revision for {name}")
        _integer(identity["rebuild"], f"Formula rebuild for {name}")
        _digest(identity["normalized_formula_sha256"], f"normalized Formula digest for {name}")
        work_class = formula["work_class"]
        if work_class not in {"required", "background"}:
            raise PlanError(f"Formula {name} has an unsupported work class")
        required_by = list(_sequence(formula["required_by_products"], f"required products for {name}"))
        checked_required_by = [_stable_id(item, f"required product for {name}") for item in required_by]
        if checked_required_by != sorted(set(checked_required_by)) or not set(checked_required_by).issubset(product_ids):
            raise PlanError(f"Formula {name} required-product reasons are invalid")
        if (work_class == "required") != bool(checked_required_by):
            raise PlanError(f"Formula {name} work class and product reasons disagree")
        dependencies = []
        previous_dependency = ""
        for dependency_candidate in _sequence(
            formula["direct_dependencies"], f"direct dependencies for {name}"
        ):
            dependency = _mapping(dependency_candidate, f"direct dependency for {name}")
            _exact_keys(dependency, FORMULA_DEPENDENCY_KEYS, f"direct dependency for {name}")
            dependency_subject = (
                _stable_id(dependency["formula"], f"dependency name for {name}"),
                _architecture(dependency["architecture"], f"dependency architecture for {name}"),
            )
            encoded = exact_formula_subject(*dependency_subject)
            if encoded <= previous_dependency:
                raise PlanError(f"direct dependencies for {name} are not sorted and unique")
            previous_dependency = encoded
            _digest(
                dependency["materialization_policy_sha256"],
                f"dependency materialization policy for {name}",
            )
            dependencies.append(dependency_subject)
        edge_count += len(dependencies)
        _validate_capture(formula["capture"], name)
        if formula["contract_sha256"] is not None:
            _digest(formula["contract_sha256"], f"Formula contract for {name}")
        graph[subject] = dependencies
        formula_by_subject[subject] = formula
        formula_sequence.append(subject)
    if edge_count > MAX_GRAPH_EDGES:
        raise PlanError("tap plan graph exceeds its edge bound")
    for subject, dependencies in graph.items():
        for dependency in dependencies:
            if dependency not in graph:
                raise PlanError(f"Formula {subject[0]} names a missing dependency")
            if dependency[1] != subject[1]:
                raise PlanError("Formula dependency changes architecture")
    order = _topological_order(graph)

    required_list = list(_sequence(value["required_subjects"], "required subjects"))
    background_list = list(_sequence(value["background_subjects"], "background subjects"))
    required_subjects = [_parse_formula_subject(item, "required subject") for item in required_list]
    background_subjects = [_parse_formula_subject(item, "background subject") for item in background_list]
    if len(set(required_subjects + background_subjects)) != len(required_subjects) + len(background_subjects):
        raise PlanError("tap plan subject lists overlap or contain duplicates")
    if set(required_subjects + background_subjects) != set(graph):
        raise PlanError("tap plan subject lists do not cover the Formula graph")

    root_requirements = _validate_formula_requirements_shape(requirements)
    propagated: dict[tuple[str, str], set[tuple[str, str]]] = {subject: set() for subject in graph}

    def propagate(subject: tuple[str, str], use: tuple[str, str]) -> None:
        if subject not in graph:
            raise PlanError(f"selected product roots missing Formula subject {subject!r}")
        if use in propagated[subject]:
            return
        propagated[subject].add(use)
        for dependency in graph[subject]:
            propagate(dependency, use)

    for requirement in root_requirements:
        for use in requirement["uses"]:
            propagate(
                (requirement["formula"], requirement["architecture"]),
                (use["product_id"], use["materialization"]),
            )
    expected_required_set = {subject for subject, uses in propagated.items() if uses}
    expected_required = [subject for subject in order if subject in expected_required_set]
    expected_background = [subject for subject in order if subject not in expected_required_set]
    if required_subjects != expected_required or background_subjects != expected_background:
        raise PlanError("tap plan required/background classification is not product-derived")
    if formula_sequence != expected_required + expected_background:
        raise PlanError("tap plan Formula records are not in deterministic work order")
    for subject, formula in formula_by_subject.items():
        expected_products = sorted({product for product, _ in propagated[subject]})
        if formula["required_by_products"] != expected_products:
            raise PlanError(f"Formula {subject[0]} lost a product requirement reason")
        for dependency_record, dependency_subject in zip(
            formula["direct_dependencies"], graph[subject], strict=True
        ):
            dependency_class = (
                "required" if dependency_subject in expected_required_set else "background"
            )
            expected_policy = _materialization_policy_sha256(
                dependency_subject, propagated[dependency_subject], dependency_class
            )
            if dependency_record["materialization_policy_sha256"] != expected_policy:
                raise PlanError(
                    f"Formula {subject[0]} dependency materialization policy is stale"
                )
    graph_identity = [
        {
            "subject": json.loads(exact_formula_subject(*subject)),
            "dependencies": [
                json.loads(exact_formula_subject(*dependency))
                for dependency in graph[subject]
            ],
        }
        for subject in order
    ]
    if canonical_sha256(graph_identity) != value["graph_sha256"]:
        raise PlanError("tap plan graph digest is stale")


def reverse_dependants(plan: Mapping[str, Any]) -> dict[str, list[str]]:
    validate_tap_plan(plan)
    result = {
        exact_formula_subject(
            formula["identity"]["name"], formula["identity"]["architecture"]
        ): []
        for formula in plan["formulae"]
    }
    for formula in plan["formulae"]:
        dependant = exact_formula_subject(
            formula["identity"]["name"], formula["identity"]["architecture"]
        )
        for dependency in formula["direct_dependencies"]:
            subject = exact_formula_subject(
                dependency["formula"], dependency["architecture"]
            )
            result[subject].append(dependant)
    for dependants in result.values():
        dependants.sort()
    return result


def _git(tap_root: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=tap_root,
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PlanError(f"cannot snapshot exact tap source: {error}") from error
    return completed.stdout.strip()


def snapshot_tap_source(tap_root: Path, repository: str) -> dict[str, str]:
    root = tap_root.resolve(strict=True)
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise PlanError("tap root is not the exact checkout root")
    source = {
        "repository": repository,
        "commit": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
    }
    return _validate_tap_source(source)


def plan_exact_tap_request(
    tap_root: Path,
    request: Mapping[str, Any],
    *,
    request_digest: str,
    request_asset_url: str,
    formula_requirements: Sequence[Mapping[str, Any]],
    tap_repository: str,
) -> dict[str, Any]:
    before = snapshot_tap_source(tap_root, tap_repository)
    inventory = generate_formula_inventory(tap_root)
    after = snapshot_tap_source(tap_root, tap_repository)
    if before != after:
        raise PlanError("tap commit or tree changed during Formula inventory evaluation")
    return plan_request(
        request,
        request_digest=request_digest,
        request_asset_url=request_asset_url,
        tap_source=before,
        inventory=inventory,
        formula_requirements=formula_requirements,
    )


def _miniature_inputs(tap_root: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    inventory = load_formula_inventory(
        (tap_root / "Kandelo/staging/fixtures/formula-inventory.json").read_bytes()
    )
    names = {"asa", "bash", "curl", "libcurl", "libcxx", "ncurses", "openssl", "zlib"}
    formulae = [entry for entry in inventory["formulae"] if entry["name"] in names]
    graph_identity = [
        {
            "name": entry["name"],
            "target_dependencies": [
                dependency["name"] for dependency in entry["target_dependencies"]
            ],
        }
        for entry in formulae
    ]
    inventory = {**inventory, "formulae": formulae, "graph_sha256": canonical_sha256(graph_identity)}
    request = json.loads(
        (tap_root / "Kandelo/staging/fixtures/request/current-request.json").read_bytes()
    )
    request["requirements"]["products"] = [
        {
            "id": "alpha-shell",
            "path": "images/vfs/products/alpha-shell.toml",
            "manifest_sha256": "a" * 64,
        },
        {
            "id": "beta-tools",
            "path": "images/vfs/products/beta-tools.toml",
            "manifest_sha256": "b" * 64,
        },
    ]
    request["requirements"]["evidence"] = [
        {
            "product_id": "alpha-shell",
            "applicability": "required",
            "node": ["alpha-node"],
            "browser": ["alpha-browser"],
        },
        {
            "product_id": "beta-tools",
            "applicability": "required",
            "node": ["beta-node"],
            "browser": [],
        },
    ]
    request["requirements"]["digest"] = canonical_sha256(
        {
            "change_classes": request["requirements"]["change_classes"],
            "products": request["requirements"]["products"],
            "registries": request["requirements"]["registries"],
            "evidence": request["requirements"]["evidence"],
        }
    )
    requirements = [
        {
            "tap": "kandelo-dev/homebrew-tap-core",
            "formula": "bash",
            "architecture": "wasm32",
            "uses": [{"product_id": "beta-tools", "materialization": "embedded"}],
        },
        {
            "tap": "kandelo-dev/homebrew-tap-core",
            "formula": "curl",
            "architecture": "wasm32",
            "uses": [{"product_id": "alpha-shell", "materialization": "lazy"}],
        },
        {
            "tap": "kandelo-dev/homebrew-tap-core",
            "formula": "libcurl",
            "architecture": "wasm32",
            "uses": [{"product_id": "beta-tools", "materialization": "embedded"}],
        },
    ]
    return inventory, request, requirements


def build_miniature_tap_plan_fixture(tap_root: Path) -> dict[str, Any]:
    root = tap_root.resolve(strict=True)
    inventory, request, requirements = _miniature_inputs(root)
    digest = canonical_sha256(request)
    head = request["build_source"]["commit"]
    asset = f"candidate-request-{head}-sha256-{digest}.json"
    return plan_request(
        request,
        request_digest=digest,
        request_asset_url=(
            "https://github.com/Automattic/kandelo/releases/download/"
            f"abi-staging-pr-19/{asset}"
        ),
        tap_source={
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": "7" * 40,
            "tree": "8" * 40,
        },
        inventory=inventory,
        formula_requirements=requirements,
    )


def write_canonical_plan(destination: Path, plan: Mapping[str, Any]) -> None:
    validate_tap_plan(plan)
    parent = destination.parent.resolve(strict=True)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise PlanError("tap plan destination is not a direct regular file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(plan))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
