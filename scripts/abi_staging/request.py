"""Strict validation for public, nonendorsed ABI staging requests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import tomllib
from types import MappingProxyType
from typing import Any

from .canonical import CanonicalJsonError, canonical_sha256, parse_canonical_bytes


POLICY_KEYS = frozenset(
    {
        "schema",
        "kind",
        "issuer_repository",
        "issuer_workflow_path",
        "request_release_tag_prefix",
        "addressed_tap",
        "accepted_request_schema",
        "accepted_request_kind",
        "max_request_bytes",
        "max_api_response_bytes",
        "max_string_bytes",
        "max_bindings",
        "max_json_depth",
        "max_release_pages",
        "max_release_assets",
        "max_redirects",
        "allowed_release_hosts",
    }
)
REQUEST_KEYS = frozenset(
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
)
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
HOST = re.compile(r"^[a-z0-9.-]+$")
ASSET_NAME = re.compile(
    r"^candidate-request-(?P<head>[0-9a-f]{40})-sha256-"
    r"(?P<digest>[0-9a-f]{64})\.json$"
)
CHANGE_CLASS_ORDER = {"abi": 0, "kernel": 1, "host": 2}
REGISTRY_KIND_ORDER = {"pages": 0, "tests": 1}
APPLICABILITY = frozenset({"not-applicable", "informational", "required"})


class RequestValidationError(ValueError):
    """Raised when a request, filename, or protected issuer policy is invalid."""


@dataclass(frozen=True)
class RequestIssuerPolicyV1:
    issuer_repository: str
    issuer_workflow_path: str
    request_release_tag_prefix: str
    addressed_tap: str
    accepted_request_schema: int
    accepted_request_kind: str
    max_request_bytes: int
    max_api_response_bytes: int
    max_string_bytes: int
    max_bindings: int
    max_json_depth: int
    max_release_pages: int
    max_release_assets: int
    max_redirects: int
    allowed_release_hosts: tuple[str, ...]


@dataclass(frozen=True)
class RequestAssetNameV1:
    head: str
    digest: str


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value.keys())
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RequestValidationError(
            f"{field} fields differ: missing={missing!r} extra={extra!r}"
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RequestValidationError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise RequestValidationError(f"{field} must be an array")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0, maximum: int = 2**64 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestValidationError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise RequestValidationError(f"{field} is outside its accepted range")
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise RequestValidationError(f"{field} must be a string")
    try:
        length = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise RequestValidationError(f"{field} is not valid UTF-8") from error
    if length < 1 or length > maximum or "\0" in value:
        raise RequestValidationError(f"{field} is outside its UTF-8 byte bound")
    return value


def _repository(value: Any, field: str) -> str:
    text = _text(value, field, 256)
    if REPOSITORY.fullmatch(text) is None:
        raise RequestValidationError(f"{field} is not an exact owner/repository identity")
    return text


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise RequestValidationError(f"{field} must be a full lowercase Git SHA")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise RequestValidationError(f"{field} must be a lowercase SHA-256")
    return value


def _stable_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or STABLE_ID.fullmatch(value) is None:
        raise RequestValidationError(f"{field} is not a stable identifier")
    return value


def _relative_path(value: Any, field: str) -> str:
    path = _text(value, field, 4096)
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise RequestValidationError(f"{field} is not a normalized repository-relative path")
    return path


def _sorted_unique_strings(
    value: Any,
    field: str,
    *,
    allow_empty: bool,
    validator: Any,
) -> tuple[str, ...]:
    values = tuple(_sequence(value, field))
    if not allow_empty and not values:
        raise RequestValidationError(f"{field} must not be empty")
    checked = tuple(validator(item, field) for item in values)
    if any(left >= right for left, right in zip(checked, checked[1:])):
        raise RequestValidationError(f"{field} must be sorted and duplicate-free")
    return checked


def load_request_issuer_policy(
    path: Path,
    *,
    expected_tap: str,
) -> RequestIssuerPolicyV1:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RequestValidationError(f"cannot read request issuer policy: {error}") from error
    if not raw or len(raw) > 1024 * 1024:
        raise RequestValidationError("request issuer policy size is invalid")
    try:
        value = tomllib.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise RequestValidationError(f"request issuer policy is invalid: {error}") from error
    _exact_keys(value, POLICY_KEYS, "request issuer policy")
    if _integer(value["schema"], "policy schema") != 1:
        raise RequestValidationError("request issuer policy schema is unsupported")
    if value["kind"] != "kandelo-abi-staging-request-issuers":
        raise RequestValidationError("request issuer policy kind is unsupported")
    issuer_repository = _repository(value["issuer_repository"], "issuer repository")
    issuer_workflow_path = _relative_path(value["issuer_workflow_path"], "issuer workflow path")
    prefix = _text(value["request_release_tag_prefix"], "request tag prefix", 128)
    if prefix != "abi-staging-pr-":
        raise RequestValidationError("request tag prefix is unsupported")
    addressed_tap = _repository(value["addressed_tap"], "addressed tap")
    if addressed_tap != expected_tap:
        raise RequestValidationError("protected registry does not address this tap")
    accepted_schema = _integer(value["accepted_request_schema"], "request schema", minimum=1)
    accepted_kind = _text(value["accepted_request_kind"], "request kind", 128)
    bounds = {
        key: _integer(value[key], key, minimum=1, maximum=16 * 1024 * 1024)
        for key in (
            "max_request_bytes",
            "max_api_response_bytes",
            "max_string_bytes",
            "max_bindings",
            "max_json_depth",
            "max_release_pages",
            "max_release_assets",
            "max_redirects",
        )
    }
    hosts_value = tuple(_sequence(value["allowed_release_hosts"], "allowed release hosts"))
    hosts = tuple(_text(host, "allowed release host", 253) for host in hosts_value)
    if not hosts or tuple(sorted(set(hosts))) != hosts:
        raise RequestValidationError("allowed release hosts must be sorted and duplicate-free")
    if any(HOST.fullmatch(host) is None for host in hosts):
        raise RequestValidationError("allowed release host is invalid")
    required_hosts = {"api.github.com", "github.com", "release-assets.githubusercontent.com"}
    if not required_hosts.issubset(hosts):
        raise RequestValidationError("allowed release hosts omit a required GitHub boundary")
    return RequestIssuerPolicyV1(
        issuer_repository=issuer_repository,
        issuer_workflow_path=issuer_workflow_path,
        request_release_tag_prefix=prefix,
        addressed_tap=addressed_tap,
        accepted_request_schema=accepted_schema,
        accepted_request_kind=accepted_kind,
        allowed_release_hosts=hosts,
        **bounds,
    )


def parse_request_asset_name(filename: str) -> RequestAssetNameV1:
    match = ASSET_NAME.fullmatch(filename)
    if match is None:
        raise RequestValidationError("request asset name does not have the exact grammar")
    return RequestAssetNameV1(head=match.group("head"), digest=match.group("digest"))


def _validate_products(value: Any, policy: RequestIssuerPolicyV1) -> None:
    products = tuple(_sequence(value, "request products"))
    if not products or len(products) > policy.max_bindings:
        raise RequestValidationError("request products must be bounded and nonempty")
    previous: str | None = None
    for item in products:
        product = _mapping(item, "request product")
        _exact_keys(product, frozenset({"id", "path", "manifest_sha256"}), "request product")
        product_id = _stable_id(product["id"], "request product id")
        _relative_path(product["path"], "request product path")
        _sha256(product["manifest_sha256"], "request product manifest digest")
        if previous is not None and previous >= product_id:
            raise RequestValidationError("request products must be sorted and duplicate-free")
        previous = product_id


def _validate_registries(value: Any, policy: RequestIssuerPolicyV1) -> None:
    registries = tuple(_sequence(value, "request registries"))
    if not registries or len(registries) > policy.max_bindings:
        raise RequestValidationError("request registries must be bounded and nonempty")
    previous: tuple[int, str] | None = None
    for item in registries:
        registry = _mapping(item, "request registry")
        _exact_keys(registry, frozenset({"kind", "path", "sha256"}), "request registry")
        kind = registry["kind"]
        if not isinstance(kind, str) or kind not in REGISTRY_KIND_ORDER:
            raise RequestValidationError("request registry kind is unsupported")
        path = _relative_path(registry["path"], "request registry path")
        _sha256(registry["sha256"], "request registry digest")
        key = (REGISTRY_KIND_ORDER[kind], path)
        if previous is not None and previous >= key:
            raise RequestValidationError("request registries must be sorted and duplicate-free")
        previous = key


def _validate_evidence(value: Any, policy: RequestIssuerPolicyV1) -> None:
    evidence = tuple(_sequence(value, "request evidence"))
    if len(evidence) > policy.max_bindings:
        raise RequestValidationError("request evidence exceeds its bound")
    previous: str | None = None
    for item in evidence:
        binding = _mapping(item, "request evidence binding")
        _exact_keys(
            binding,
            frozenset({"product_id", "applicability", "node", "browser"}),
            "request evidence binding",
        )
        product_id = _stable_id(binding["product_id"], "request evidence product id")
        if previous is not None and previous >= product_id:
            raise RequestValidationError("request evidence must be sorted and duplicate-free")
        previous = product_id
        if binding["applicability"] not in APPLICABILITY:
            raise RequestValidationError("request evidence applicability is unsupported")
        node = _sorted_unique_strings(
            binding["node"],
            "Node evidence IDs",
            allow_empty=True,
            validator=_stable_id,
        )
        browser = _sorted_unique_strings(
            binding["browser"],
            "browser evidence IDs",
            allow_empty=True,
            validator=_stable_id,
        )
        if not node and not browser:
            raise RequestValidationError("request evidence must name Node or browser evidence")


def _validate_requirements(value: Any, policy: RequestIssuerPolicyV1) -> None:
    requirements = _mapping(value, "request requirements")
    _exact_keys(
        requirements,
        frozenset({"digest", "change_classes", "products", "registries", "evidence"}),
        "request requirements",
    )
    digest = _sha256(requirements["digest"], "request requirements digest")
    classes = tuple(_sequence(requirements["change_classes"], "request change classes"))
    if not classes or any(not isinstance(item, str) or item not in CHANGE_CLASS_ORDER for item in classes):
        raise RequestValidationError("request change classes are unsupported or empty")
    keys = tuple(CHANGE_CLASS_ORDER[item] for item in classes)
    if any(left >= right for left, right in zip(keys, keys[1:])):
        raise RequestValidationError("request change classes must be sorted and duplicate-free")
    _validate_products(requirements["products"], policy)
    _validate_registries(requirements["registries"], policy)
    _validate_evidence(requirements["evidence"], policy)
    identity = {
        "change_classes": requirements["change_classes"],
        "products": requirements["products"],
        "registries": requirements["registries"],
        "evidence": requirements["evidence"],
    }
    if canonical_sha256(identity) != digest:
        raise RequestValidationError("request requirements digest does not match its contents")


def validate_request(
    body: bytes,
    filename: str,
    policy: RequestIssuerPolicyV1,
) -> MappingProxyType[str, Any]:
    try:
        request = parse_canonical_bytes(
            body,
            maximum_bytes=policy.max_request_bytes,
            maximum_depth=policy.max_json_depth,
            maximum_items=policy.max_bindings * 32,
            maximum_string_bytes=policy.max_string_bytes,
        )
    except CanonicalJsonError as error:
        raise RequestValidationError(f"request body is not canonical: {error}") from error
    _exact_keys(request, REQUEST_KEYS, "request")
    if _integer(request["schema"], "request schema") != policy.accepted_request_schema:
        raise RequestValidationError("request schema is unsupported")
    if request["kind"] != policy.accepted_request_kind:
        raise RequestValidationError("request kind is unsupported")

    pull_request = _mapping(request["pull_request"], "pull request")
    _exact_keys(pull_request, frozenset({"repository", "number"}), "pull request")
    repository = _repository(pull_request["repository"], "pull-request repository")
    if repository != policy.issuer_repository:
        raise RequestValidationError("request does not name the protected issuer repository")
    _integer(pull_request["number"], "pull-request number", minimum=1)

    source = _mapping(request["build_source"], "build source")
    _exact_keys(source, frozenset({"repository", "commit", "tree"}), "build source")
    source_repository = _repository(source["repository"], "build-source repository")
    head = _git_sha(source["commit"], "build-source commit")
    _git_sha(source["tree"], "build-source tree")

    target = _mapping(request["target_abi"], "target ABI")
    _exact_keys(target, frozenset({"version", "snapshot_sha256"}), "target ABI")
    _integer(target["version"], "target ABI version", maximum=2**32 - 1)
    _sha256(target["snapshot_sha256"], "target ABI snapshot digest")
    _validate_requirements(request["requirements"], policy)

    issuance = _mapping(request["issuance"], "request issuance")
    _exact_keys(
        issuance,
        frozenset(
            {
                "issuer_repository",
                "issuer_workflow_ref",
                "policy_version",
                "policy_sha256",
                "guard_registry_version",
                "guard_registry_sha256",
                "authorization",
            }
        ),
        "request issuance",
    )
    if _repository(issuance["issuer_repository"], "issuer repository") != policy.issuer_repository:
        raise RequestValidationError("request issuer is not authorized by the tap")
    workflow_ref = _text(issuance["issuer_workflow_ref"], "issuer workflow ref", 1024)
    expected_workflow_prefix = f"{policy.issuer_repository}/{policy.issuer_workflow_path}@"
    if not workflow_ref.startswith(expected_workflow_prefix) or not GIT_SHA.fullmatch(
        workflow_ref[len(expected_workflow_prefix) :]
    ):
        raise RequestValidationError("issuer workflow ref is not protected and immutable")
    _integer(issuance["policy_version"], "policy version", minimum=1)
    _sha256(issuance["policy_sha256"], "policy digest")
    _integer(issuance["guard_registry_version"], "guard-registry version", minimum=1)
    _sha256(issuance["guard_registry_sha256"], "guard-registry digest")
    authorization = _mapping(issuance["authorization"], "request authorization")
    _exact_keys(authorization, frozenset({"mode", "head"}), "request authorization")
    if authorization["mode"] != "same-repository":
        raise RequestValidationError("automatic fork request authorization is disabled")
    authorization_head = _git_sha(authorization["head"], "authorization head")
    if authorization_head != head or source_repository != repository:
        raise RequestValidationError("request authorization does not bind its exact source")

    context = _mapping(request["informational_context"], "informational context")
    allowed_context = frozenset({"base_commit", "base_tree", "previous_abi", "ref_hint"})
    if not frozenset(context).issubset(allowed_context):
        raise RequestValidationError("informational context attempts to add authority")
    base_commit = context.get("base_commit")
    base_tree = context.get("base_tree")
    if (base_commit is None) != (base_tree is None):
        raise RequestValidationError("informational base commit and tree must appear together")
    if base_commit is not None:
        _git_sha(base_commit, "informational base commit")
        _git_sha(base_tree, "informational base tree")
    if "previous_abi" in context:
        _integer(context["previous_abi"], "informational previous ABI")
    if "ref_hint" in context:
        _text(context["ref_hint"], "informational ref hint", 1024)

    parsed_name = parse_request_asset_name(filename)
    digest = hashlib.sha256(body).hexdigest()
    if parsed_name.head != head or parsed_name.digest != digest:
        raise RequestValidationError("request filename does not bind canonical head and bytes")
    return request
