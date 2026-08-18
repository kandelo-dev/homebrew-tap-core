"""Pure merge admission and unchanged-layer canonical promotion contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import base64
import copy
import hashlib
import re
from types import MappingProxyType
from typing import Any, Literal

from .abi_history import (
    HISTORY_RECORD_MEDIA_TYPE,
    AbiHistoryError,
    history_record_repository,
    validate_protection_snapshot,
)
from .canonical import (
    MAX_VFS_COMPOSITION_JSON_ITEMS,
    CanonicalJsonError,
    canonical_bytes,
    canonical_sha256,
    parse_canonical_bytes,
)
from .bottle_link import (
    BottleLinkError,
    build_link_manifest,
    inspect_bottle_link_inventory,
    link_manifest_bytes,
    load_guest_layout,
    validate_bottle_link_inventory,
)
from .contract import (
    ContractError,
    captured_file_sha256,
    load_bottle_contract,
    validate_candidate_reuse_record,
)
from .custody import CustodyError, load_source_custody_manifest
from .execution import ExecutionError, normalize_candidate_bottle_metadata
from .github_public import GitHubPublicClient, PublicGitHubError
from .oci import (
    FetchedOciBlobV1,
    FetchedOciRecordV1,
    OciPublicationError,
    OciTransportV1,
    PublishedRecordLocatorV1,
    build_oci_manifest,
    fetch_public_record,
    publish_immutable_oci_plan,
)
from .override import OVERRIDE_RECEIPT_MEDIA_TYPE
from .plan import (
    PlanError,
    bottle_metadata_formula_key,
    exact_formula_subject,
    parse_formula_subject,
    validate_tap_plan,
)
from .records import (
    BOTTLE_LAYER_MEDIA_TYPE,
    BOTTLE_METADATA_MEDIA_TYPE,
    CANDIDATE_RECORD_MEDIA_TYPE,
    CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
    OCI_MANIFEST_MEDIA_TYPE,
    SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
    VFS_COMPOSITION_DESCRIPTOR_MEDIA_TYPE,
    OciBlobV1,
    OciRecordPlanV1,
    TapRecordError,
    build_admission_record,
    validate_admission_record,
    validate_abi_history_record,
    validate_candidate_record,
)
from .policy import VerificationTestDefinitionV1
from .tap_metadata import (
    FormulaMetadataUpdateV1,
    PromotedBottleMetadataV1,
    PromotionPolicyV1,
    TapMetadataError,
    TapMetadataPatchV1,
    formula_generated_metadata_sha256,
    load_abi_state,
    plan_formula_metadata_patch,
    plan_successor_activation_patch,
    require_current_abi_authority,
)
from .verification import (
    VERIFICATION_RECEIPT_MEDIA_TYPE,
    VerificationError,
    receipt_repository,
    validate_verification_receipt_record,
)
CANONICAL_BOTTLE_MANIFEST_MEDIA_TYPE = (
    "application/vnd.kandelo.homebrew.canonical-bottle.v1+json"
)
ADMISSION_RECORD_MEDIA_TYPE = "application/vnd.kandelo.homebrew.admission.v1+json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,127}$")


class PromotionError(ValueError):
    """Raised when merge admission or canonical promotion is not exact."""


@dataclass(frozen=True)
class PromotionDecisionV1:
    request_digest: str
    merged_pull_request: Mapping[str, object]
    formula_subject: str
    tap_plan_digest: str
    candidate_record_digest: str
    candidate_binding_digest: str
    bottle_layer_sha256: str
    bottle_layer_bytes: int
    source_custody_digest: str
    qualifying_receipts: tuple[str, ...]
    override_receipts: tuple[str, ...]
    tap_source_state: Literal["exact", "drift", "rebuild-required"]
    eligibility: Literal["eligible", "ineligible", "rebuild-required"]


@dataclass(frozen=True)
class CanonicalBottleIdentityV1:
    request_digest: str
    formula_subject: str
    candidate_record_digest: str
    bottle_layer_sha256: str
    bottle_layer_bytes: int
    classification: Literal["canonical-pending-admission", "canonical-direct"]
    merged_pull_request: Mapping[str, object] | None = None
    source: Mapping[str, object] | None = None


@dataclass(frozen=True)
class CanonicalBottlePublicationV1:
    locator: PublishedRecordLocatorV1
    artifact: Mapping[str, object]


@dataclass(frozen=True)
class PreparedAdmissionV1:
    decision: PromotionDecisionV1
    request_source: Mapping[str, object]
    candidate_source: Mapping[str, object]
    preactivation_tap_source: Mapping[str, object]
    abi_history_record_sha256: str
    canonical: Mapping[str, object]
    canonical_readback_evidence_sha256: str
    promoted_layer: Mapping[str, object]
    original_producer: Mapping[str, object]
    candidate_formula: Mapping[str, object]
    candidate_bottle_metadata: Mapping[str, object]
    candidate_bottle_contract: Mapping[str, object]
    candidate_bottle_inventory: Mapping[str, object]


@dataclass(frozen=True)
class PreparedFormulaMetadataUpdateV1:
    update: FormulaMetadataUpdateV1
    patch: TapMetadataPatchV1


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def _exact(value: Any, keys: frozenset[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != keys:
        raise PromotionError(f"{field} fields changed")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise PromotionError(f"{field} is not a lowercase SHA-256")
    return value


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise PromotionError(f"{field} is not a full lowercase Git SHA")
    return value


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**64 - 1:
        raise PromotionError(f"{field} is not a bounded positive integer")
    return value


def _nonnegative(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**32 - 1:
        raise PromotionError(f"{field} is not a bounded nonnegative integer")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise PromotionError(f"{field} is not a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise PromotionError(f"{field} is not UTF-8") from error
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise PromotionError(f"{field} is outside its string bound")
    return value


def _repository(value: Any, field: str) -> str:
    if not isinstance(value, str) or REPOSITORY.fullmatch(value) is None:
        raise PromotionError(f"{field} is not an owner/name repository")
    return value


def _stable_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or STABLE_ID.fullmatch(value) is None:
        raise PromotionError(f"{field} is not a stable identifier")
    return value


def _source(value: Any, field: str) -> dict[str, str]:
    source = _exact(value, frozenset({"repository", "commit", "tree"}), field)
    return {
        "repository": _repository(source["repository"], f"{field} repository"),
        "commit": _git_sha(source["commit"], f"{field} commit"),
        "tree": _git_sha(source["tree"], f"{field} tree"),
    }


def _artifact(value: Any, field: str) -> dict[str, object]:
    artifact = _exact(
        value,
        frozenset({"sha256", "bytes", "immutable_reference"}),
        field,
    )
    digest = _digest(artifact["sha256"], f"{field} digest")
    size = _positive(artifact["bytes"], f"{field} bytes")
    reference = artifact["immutable_reference"]
    if (
        not isinstance(reference, str)
        or any(character.isspace() for character in reference)
        or f"sha256:{digest}" not in reference
    ):
        raise PromotionError(f"{field} reference is not immutable")
    return {"sha256": digest, "bytes": size, "immutable_reference": reference}


def _canonical_mapping(
    body: bytes,
    field: str,
    *,
    maximum_items: int = 100_000,
) -> dict[str, Any]:
    try:
        value = parse_canonical_bytes(
            body,
            maximum_bytes=64 * 1024 * 1024,
            maximum_items=maximum_items,
        )
    except CanonicalJsonError as error:
        raise PromotionError(f"{field} is not canonical: {error}") from error
    if not isinstance(value, Mapping):
        raise PromotionError(f"{field} is not an object")
    return _plain(value)


def _descriptor_identity(blob: FetchedOciBlobV1) -> dict[str, object]:
    return {
        "mediaType": blob.media_type,
        "digest": blob.digest,
        "size": blob.size,
        "annotations": {
            "dev.kandelo.abi-staging.role": blob.role,
            "org.opencontainers.image.title": blob.title,
        },
    }


def _validated_fetched_record(
    value: FetchedOciRecordV1 | None,
    *,
    artifact_type: str,
    required_roles: tuple[str, ...],
    field: str,
) -> tuple[dict[str, Any], dict[str, FetchedOciBlobV1]]:
    if not isinstance(value, FetchedOciRecordV1):
        raise PromotionError(f"{field} public record is missing")
    if value.artifact_type != artifact_type:
        raise PromotionError(f"{field} artifact type changed")
    if not value.repository.startswith("ghcr.io/"):
        raise PromotionError(f"{field} repository is not public GHCR")
    digest = value.digest
    if (
        not digest.startswith("sha256:")
        or SHA256.fullmatch(digest.removeprefix("sha256:")) is None
        or hashlib.sha256(value.manifest).hexdigest() != digest.removeprefix("sha256:")
        or value.immutable_reference != f"{value.repository}@{digest}"
    ):
        raise PromotionError(f"{field} public manifest/readback identity drifted")
    manifest = _canonical_mapping(value.manifest, f"{field} manifest")
    manifest = dict(
        _exact(
            manifest,
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
            f"{field} manifest",
        )
    )
    if (
        manifest["schemaVersion"] != 2
        or manifest["mediaType"] != OCI_MANIFEST_MEDIA_TYPE
        or manifest["artifactType"] != artifact_type
        or manifest["config"] != _descriptor_identity(value.config)
        or hashlib.sha256(value.config.body).hexdigest()
        != value.config.digest.removeprefix("sha256:")
        or len(value.config.body) != value.config.size
    ):
        raise PromotionError(f"{field} manifest/config identity drifted")
    raw_layers = manifest["layers"]
    if not isinstance(raw_layers, list):
        raise PromotionError(f"{field} manifest layers changed shape")
    descriptors: dict[str, Mapping[str, Any]] = {}
    for descriptor in raw_layers:
        if not isinstance(descriptor, Mapping):
            raise PromotionError(f"{field} manifest layer descriptor is invalid")
        annotations = descriptor.get("annotations")
        role = (
            annotations.get("dev.kandelo.abi-staging.role")
            if isinstance(annotations, Mapping)
            else None
        )
        if not isinstance(role, str) or not role or role in descriptors:
            raise PromotionError(f"{field} manifest layer roles are invalid")
        descriptors[role] = descriptor
    layers: dict[str, FetchedOciBlobV1] = {}
    for layer in value.layers:
        if (
            layer.role in layers
            or descriptors.get(layer.role) != _descriptor_identity(layer)
            or hashlib.sha256(layer.body).hexdigest()
            != layer.digest.removeprefix("sha256:")
            or len(layer.body) != layer.size
        ):
            raise PromotionError(f"{field} layer {layer.role!r} drifted")
        layers[layer.role] = layer
    if any(role not in layers for role in required_roles):
        raise PromotionError(f"{field} lacks required public layer bytes")
    return _canonical_mapping(value.config.body, f"{field} config"), layers


def _request_context(
    request: Mapping[str, Any],
    request_digest: str,
    expected_policy: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str], int]:
    digest = _digest(request_digest, "promotion request")
    if canonical_sha256(request) != digest:
        raise PromotionError("promotion request digest differs from canonical request bytes")
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
        "promotion request",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-request":
        raise PromotionError("promotion request protocol is unsupported")
    pull = _exact(
        value["pull_request"], frozenset({"repository", "number"}), "request PR"
    )
    pull_checked = {
        "repository": _repository(pull["repository"], "request PR repository"),
        "number": _positive(pull["number"], "request PR number"),
    }
    source = _source(value["build_source"], "request build source")
    target = _exact(
        value["target_abi"], frozenset({"version", "snapshot_sha256"}), "request target ABI"
    )
    target_abi = _nonnegative(target["version"], "request target ABI")
    _digest(target["snapshot_sha256"], "request ABI snapshot")
    if _plain(value["issuance"]) != _plain(expected_policy):
        raise PromotionError("request policy/guard identity is not current")
    return pull_checked, source, target_abi


def _merged_pull_request(
    value: Any, *, request_pull: Mapping[str, Any], source: Mapping[str, str]
) -> dict[str, object]:
    fact = _exact(
        value,
        frozenset({"repository", "number", "state", "head", "merge_commit"}),
        "merged pull-request fact",
    )
    checked = {
        "repository": _repository(fact["repository"], "merged PR repository"),
        "number": _positive(fact["number"], "merged PR number"),
        "head": _git_sha(fact["head"], "merged PR head"),
        "merge_commit": _git_sha(fact["merge_commit"], "merged PR commit"),
    }
    if fact["state"] != "merged":
        raise PromotionError("only an exact merged pull request permits promotion")
    if (
        checked["repository"].lower() != str(request_pull["repository"]).lower()
        or checked["number"] != request_pull["number"]
        or checked["head"] != source["commit"]
    ):
        raise PromotionError("merged pull-request fact differs from the exact request")
    return checked


def fetch_exact_merge_fact(
    request: Mapping[str, Any], client: GitHubPublicClient
) -> dict[str, object]:
    """Fetch and validate the public merge fact for the request's exact PR head."""

    request_value = _exact(
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
        "promotion request",
    )
    pull = _exact(
        request_value["pull_request"],
        frozenset({"repository", "number"}),
        "request PR",
    )
    checked_pull = {
        "repository": _repository(pull["repository"], "request PR repository"),
        "number": _positive(pull["number"], "request PR number"),
    }
    source = _source(request_value["build_source"], "request build source")
    policy = getattr(client, "policy", None)
    issuer_repository = getattr(policy, "issuer_repository", None)
    if (
        not isinstance(issuer_repository, str)
        or issuer_repository.lower() != checked_pull["repository"].lower()
    ):
        raise PromotionError("public GitHub client is bound to another repository")
    try:
        lifecycle = client.pull_request_lifecycle(checked_pull["number"])
    except PublicGitHubError as error:
        raise PromotionError(f"cannot fetch exact merged pull request: {error}") from error
    fact = {
        "repository": checked_pull["repository"],
        "number": checked_pull["number"],
        "state": getattr(lifecycle, "state", None),
        "head": getattr(lifecycle, "current_head", None),
        "merge_commit": getattr(lifecycle, "merged_commit", None),
    }
    checked = _merged_pull_request(
        fact,
        request_pull=checked_pull,
        source=source,
    )
    return {**checked, "state": "merged"}


def _formula_entry(plan: Mapping[str, Any], name: str, architecture: str) -> Mapping[str, Any]:
    matches = [
        formula
        for formula in plan["formulae"]
        if formula["identity"]["name"] == name
        and formula["identity"]["architecture"] == architecture
    ]
    if len(matches) != 1:
        raise PromotionError("candidate Formula is absent or duplicated in the tap plan")
    return matches[0]


def _candidate_facts(
    candidate: FetchedOciRecordV1,
    source_custody: FetchedOciRecordV1,
    *,
    request_digest: str,
    request_source: Mapping[str, str],
    target_abi: int,
    tap_plan: Mapping[str, Any],
    policy: PromotionPolicyV1,
    allow_historical_request: bool,
) -> tuple[dict[str, Any], Mapping[str, Any], dict[str, object], str, str]:
    record, candidate_layers = _validated_fetched_record(
        candidate,
        artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
        required_roles=("bottle-layer", "bottle-contract"),
        field="candidate",
    )
    try:
        validate_candidate_record(record)
    except TapRecordError as error:
        raise PromotionError(f"candidate record is invalid: {error}") from error
    payload = record["candidate"]
    formula = payload["formula"]
    name = _stable_id(formula["formula"], "candidate Formula")
    architecture = formula["architecture"]
    if architecture not in {"wasm32", "wasm64"}:
        raise PromotionError("candidate architecture is unsupported")
    expected_candidate_repository = canonical_repository(
        policy, target_abi, name, candidate=True
    )
    if candidate.repository != "ghcr.io/" + expected_candidate_repository:
        raise PromotionError("candidate repository differs from its exact ABI namespace")
    bottle = _artifact(payload["bottle_layer"], "candidate bottle layer")
    candidate_request_digest = _digest(
        record["common"]["request_sha256"], "candidate request"
    )
    candidate_source = _source(record["common"]["source"], "candidate source")
    layer = candidate_layers["bottle-layer"]
    contract_layer = candidate_layers["bottle-contract"]
    if (
        bottle["sha256"] != layer.digest.removeprefix("sha256:")
        or bottle["bytes"] != layer.size
        or record["common"]["artifact"] != bottle
        or formula["target_abi"] != target_abi
        or formula["tap"].lower() != policy.tap_repository.lower()
    ):
        raise PromotionError("candidate differs from request, source, target, or bottle bytes")
    if not allow_historical_request and (
        candidate_request_digest != request_digest
        or candidate_source != request_source
    ):
        raise PromotionError("candidate differs from the exact current request")
    try:
        contract = load_bottle_contract(contract_layer.body)
    except ContractError as error:
        raise PromotionError(f"candidate bottle contract is invalid: {error}") from error
    contract_digest = hashlib.sha256(contract_layer.body).hexdigest()
    normalized = {
        item["id"]: item["artifact"] for item in payload["normalized_components"]
    }
    expected_contract_artifact = {
        "sha256": contract_digest,
        "bytes": contract_layer.size,
        "immutable_reference": f"{candidate.repository}@sha256:{contract_digest}",
    }
    if (
        formula["bottle_contract_sha256"] != contract_digest
        or normalized.get("bottle-contract") != expected_contract_artifact
        or contract["target"]["abi"] != target_abi
        or contract["target"]["architecture"] != architecture
        or contract["formula"]["name"] != name
        or contract["formula"]["version"] != formula["version"]
        or contract["formula"]["revision"] != formula["revision"]
        or contract["formula"]["rebuild"] != formula["bottle_rebuild"]
    ):
        raise PromotionError("candidate Formula and bottle contract differ")
    formula_plan = _formula_entry(tap_plan, name, architecture)
    planned_identity = formula_plan["identity"]
    planned_contract = formula_plan["contract_sha256"]
    expected_source_components = [
        {
            "id": "formula",
            "sha256": planned_identity["normalized_formula_sha256"],
        },
        *(
            {
                "id": f"tap-input-{index:04d}",
                "sha256": component["sha256"],
            }
            for index, component in enumerate(
                formula_plan["capture"]["tap_input_components"]
            )
        ),
    ]
    if (
        planned_identity["version"] != formula["version"]
        or planned_identity["revision"] != formula["revision"]
        or planned_identity["rebuild"] != formula["bottle_rebuild"]
        or contract["formula"]["normalized_source_sha256"]
        != formula_plan["capture"]["normalized_source_sha256"]
        or contract["formula"]["source_components"]
        != expected_source_components
        or planned_contract not in {None, contract_digest}
    ):
        raise PromotionError("candidate differs from the protected tap plan")
    candidate_dependencies = {
        item["id"]: _artifact(
            item["artifact"],
            f"candidate dependency {item['id']}",
        )
        for item in payload["direct_dependency_layers"]
    }
    expected_dependency_ids = {
        f"{dependency['formula']}-{dependency['architecture']}"
        for dependency in formula_plan["direct_dependencies"]
    }
    expected_contract_dependencies = []
    for dependency in formula_plan["direct_dependencies"]:
        identity = f"{dependency['formula']}-{dependency['architecture']}"
        dependency_artifact = candidate_dependencies.get(identity)
        if dependency_artifact is None:
            raise PromotionError("candidate dependency layer inventory is incomplete")
        expected_contract_dependencies.append(
            {
                "formula": dependency["formula"],
                "architecture": dependency["architecture"],
                "bottle_layer_sha256": dependency_artifact["sha256"],
                "bottle_layer_bytes": dependency_artifact["bytes"],
                "materialization_policy_sha256": dependency[
                    "materialization_policy_sha256"
                ],
            }
        )
    if (
        set(candidate_dependencies) != expected_dependency_ids
        or contract["direct_dependencies"] != expected_contract_dependencies
    ):
        raise PromotionError("candidate dependency contract differs from its exact layers")

    custody_record, custody_layers = _validated_fetched_record(
        source_custody,
        artifact_type=SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
        required_roles=(),
        field="source custody",
    )
    try:
        custody = load_source_custody_manifest(canonical_bytes(custody_record))
    except CustodyError as error:
        raise PromotionError(f"source custody record is invalid: {error}") from error
    custody_digest = source_custody.digest.removeprefix("sha256:")
    expected_custody_artifact = {
        "sha256": custody_digest,
        "bytes": len(source_custody.manifest),
        "immutable_reference": source_custody.immutable_reference,
    }
    expected_custody_roles = {
        f"{entry[identity]}-{suffix}"
        for collection, identity in ((custody["sources"], "role"), (custody["submodules"], "id"))
        for entry in collection
        for suffix in ("bundle", "tree")
    }
    if set(custody_layers) != expected_custody_roles:
        raise PromotionError("source custody public member closure is incomplete")
    expected_custody_repository = (
        policy.tap_repository.split("/", 1)[0].lower()
        + "/"
        + policy.canonical_repository_prefix
        + str(target_abi)
        + "-source-custody"
    )
    sources = {entry["role"]: entry for entry in custody["sources"]}
    if (
        source_custody.repository != "ghcr.io/" + expected_custody_repository
        or payload["source_custody_sha256"] != custody_digest
        or normalized.get("source-custody") != expected_custody_artifact
        or custody["request_sha256"] != candidate_request_digest
        or {
            key: sources.get("kandelo", {}).get(key)
            for key in ("repository", "commit", "tree")
        }
        != candidate_source
    ):
        raise PromotionError("source custody differs from its original candidate")
    custody_tap_source = {
        key: sources.get("tap", {}).get(key)
        for key in ("repository", "commit", "tree")
    }
    if custody_tap_source.get("repository", "").lower() != policy.tap_repository.lower():
        raise PromotionError("source custody names another protected tap")
    return record, formula_plan, bottle, contract_digest, custody_digest


def _candidate_reuse_binding(
    candidate_reuse: FetchedOciRecordV1,
    *,
    request_digest: str,
    request_source: Mapping[str, str],
    candidate: FetchedOciRecordV1,
    candidate_record: Mapping[str, Any],
    source_custody: FetchedOciRecordV1,
    bottle: Mapping[str, object],
    contract_digest: str,
    target_abi: int,
    policy: PromotionPolicyV1,
) -> tuple[str, dict[str, Any]]:
    record, layers = _validated_fetched_record(
        candidate_reuse,
        artifact_type=CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
        required_roles=("immutable-record-bytes",),
        field="candidate reuse",
    )
    try:
        validate_candidate_reuse_record(record)
    except ContractError as error:
        raise PromotionError(f"candidate reuse record is invalid: {error}") from error
    if layers["immutable-record-bytes"].body != candidate_reuse.config.body:
        raise PromotionError("candidate reuse immutable bytes differ from its record")
    candidate_digest = candidate.digest.removeprefix("sha256:")
    custody_digest = source_custody.digest.removeprefix("sha256:")
    payload = record["candidate_reuse"]
    formula = candidate_record["candidate"]["formula"]
    expected_formula = {
        "tap": formula["tap"],
        "formula": formula["formula"],
        "architecture": formula["architecture"],
        "target_abi": target_abi,
        "bottle_contract_sha256": contract_digest,
    }
    if (
        candidate_reuse.repository != candidate.repository
        or record["common"]["request_sha256"] != request_digest
        or record["common"]["source"] != request_source
        or payload["formula"] != expected_formula
        or payload["existing_candidate"]
        != {
            "record_sha256": candidate_digest,
            "immutable_reference": candidate.immutable_reference,
        }
        or payload["bottle_layer"] != bottle
        or payload["source_custody"]
        != {
            "record_sha256": custody_digest,
            "immutable_reference": source_custody.immutable_reference,
        }
        or payload["original_producer"]
        != candidate_record["candidate"]["producer"]
        or formula["tap"].lower() != policy.tap_repository.lower()
    ):
        raise PromotionError(
            "candidate reuse differs from the current request or original candidate"
        )
    return candidate_reuse.digest.removeprefix("sha256:"), record


def _verification_receipt(
    receipt: FetchedOciRecordV1,
    *,
    request_digest: str,
    request_source: Mapping[str, str],
    candidate_digest: str,
    bottle: Mapping[str, object],
    expected_authority: Mapping[tuple[str, str], str],
) -> tuple[str, str | None, tuple[str, str]]:
    record, _layers = _validated_fetched_record(
        receipt,
        artifact_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
        required_roles=(),
        field="verification receipt",
    )
    try:
        validate_verification_receipt_record(record)
    except VerificationError as error:
        raise PromotionError(f"verification receipt is invalid: {error}") from error
    common = record["common"]
    payload = record["verification"]
    identity = (payload["test_definition_sha256"], payload["host"])
    if (
        common["request_sha256"] != request_digest
        or common["source"] != request_source
        or payload["candidate_record_sha256"] != candidate_digest
        or payload["candidate_layer"] != bottle
        or receipt.repository != expected_authority.get(identity)
    ):
        raise PromotionError("verification receipt differs from the exact candidate")
    guard = common["guard_codes"][0] if common["guard_codes"] else None
    eligible = (
        common["outcome"] == "success"
        and common["promotion_state"] == "eligible"
        and guard is None
    )
    return receipt.digest.removeprefix("sha256:"), None if eligible else guard, identity


def _override_receipt(
    receipt: FetchedOciRecordV1,
    *,
    request_digest: str,
    request_source: Mapping[str, str],
    candidate_digest: str,
    bottle: Mapping[str, object],
    request_policy: Mapping[str, Any],
    failed_guards: set[str],
    expected_repository: str,
) -> tuple[str, tuple[str, ...]]:
    record, _layers = _validated_fetched_record(
        receipt,
        artifact_type=OVERRIDE_RECEIPT_MEDIA_TYPE,
        required_roles=(),
        field="override receipt",
    )
    value = _exact(
        record,
        frozenset({"schema", "kind", "common", "override_receipt"}),
        "override receipt",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-override-receipt":
        raise PromotionError("override receipt protocol is unsupported")
    common = _exact(
        value["common"],
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
        "override receipt common",
    )
    payload = value["override_receipt"]
    allowed = {
        "accepted_guard_codes",
        "maintainer",
        "justification",
        "policy",
        "candidate_record_sha256",
        "bottle_layer",
    }
    if "capture_authorization_sha256" in payload:
        allowed.add("capture_authorization_sha256")
    payload = _exact(payload, frozenset(allowed), "override receipt payload")
    guards = payload["accepted_guard_codes"]
    if (
        not isinstance(guards, list)
        or guards != sorted(set(guards))
        or not guards
        or not set(guards).issubset(failed_guards)
    ):
        raise PromotionError("override receipt does not accept the exact failed guards")
    maintainer = _exact(
        payload["maintainer"],
        frozenset({"login", "permission", "authorization_reference"}),
        "override maintainer",
    )
    justification = payload["justification"]
    subject = common["subject"]
    if (
        common["request_sha256"] != request_digest
        or common["source"] != request_source
        or subject != {"kind": "candidate", "identity": candidate_digest}
        or common["guard_codes"] != guards
        or common["work_state"] != "complete"
        or common["outcome"] != "success"
        or common["artifact_class"] != "candidate"
        or common["artifact"] != bottle
        or common["promotion_state"] != "accepted-with-override"
        or common["blockers"] != []
        or payload["candidate_record_sha256"] != candidate_digest
        or payload["bottle_layer"] != bottle
        or receipt.repository != expected_repository
        or _plain(payload["policy"])
        != {
            key: request_policy[key]
            for key in (
                "policy_version",
                "policy_sha256",
                "guard_registry_version",
                "guard_registry_sha256",
            )
        }
        or maintainer["permission"] not in {"maintain", "admin"}
        or not isinstance(maintainer["authorization_reference"], str)
        or not maintainer["authorization_reference"].startswith("https://github.com/")
        or not isinstance(justification, str)
        or len(justification.strip()) < 16
    ):
        raise PromotionError("override receipt differs from exact candidate authority")
    if "capture_authorization_sha256" in payload:
        _digest(payload["capture_authorization_sha256"], "capture authorization")
    return receipt.digest.removeprefix("sha256:"), tuple(guards)


def promotion_override_identity(
    receipt: FetchedOciRecordV1,
) -> dict[str, Any]:
    """Validate one public override structurally before exact-candidate selection."""

    record, _layers = _validated_fetched_record(
        receipt,
        artifact_type=OVERRIDE_RECEIPT_MEDIA_TYPE,
        required_roles=(),
        field="override receipt",
    )
    value = _exact(
        record,
        frozenset({"schema", "kind", "common", "override_receipt"}),
        "override receipt",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-override-receipt":
        raise PromotionError("override receipt protocol is unsupported")
    common = _exact(
        value["common"],
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
        "override receipt common",
    )
    payload = value["override_receipt"]
    fields = {
        "accepted_guard_codes",
        "maintainer",
        "justification",
        "policy",
        "candidate_record_sha256",
        "bottle_layer",
    }
    if isinstance(payload, Mapping) and "capture_authorization_sha256" in payload:
        fields.add("capture_authorization_sha256")
    payload = _exact(payload, frozenset(fields), "override receipt payload")
    subject = _exact(
        common["subject"], frozenset({"kind", "identity"}), "override subject"
    )
    request_digest = _digest(common["request_sha256"], "override request")
    request_source = _source(common["source"], "override request source")
    candidate_digest = _digest(
        payload["candidate_record_sha256"], "override candidate"
    )
    bottle = _artifact(payload["bottle_layer"], "override bottle layer")
    guards = payload["accepted_guard_codes"]
    maintainer = _exact(
        payload["maintainer"],
        frozenset({"login", "permission", "authorization_reference"}),
        "override maintainer",
    )
    policy = _exact(
        payload["policy"],
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
    if (
        subject != {"kind": "candidate", "identity": candidate_digest}
        or not isinstance(guards, list)
        or guards != sorted(set(guards))
        or not guards
        or common["guard_codes"] != guards
        or common["work_state"] != "complete"
        or common["outcome"] != "success"
        or common["artifact_class"] != "candidate"
        or _artifact(common["artifact"], "override common artifact") != bottle
        or common["promotion_state"] != "accepted-with-override"
        or common["blockers"] != []
        or maintainer["permission"] not in {"maintain", "admin"}
        or not isinstance(maintainer["authorization_reference"], str)
        or not maintainer["authorization_reference"].startswith("https://github.com/")
        or not isinstance(payload["justification"], str)
        or len(payload["justification"].strip()) < 16
    ):
        raise PromotionError("override receipt authority is internally contradictory")
    _positive(policy["policy_version"], "override policy version")
    _digest(policy["policy_sha256"], "override policy")
    _positive(policy["guard_registry_version"], "override guard registry version")
    _digest(policy["guard_registry_sha256"], "override guard registry")
    if "capture_authorization_sha256" in payload:
        _digest(payload["capture_authorization_sha256"], "capture authorization")
    return {
        "request_digest": request_digest,
        "request_source": request_source,
        "candidate_digest": candidate_digest,
        "bottle": bottle,
    }


def _verification_authority(
    definitions: Sequence[VerificationTestDefinitionV1],
    *,
    candidate_repository: str,
) -> dict[tuple[str, str], str]:
    if not definitions:
        raise PromotionError("protected verification registry is empty")
    result: dict[tuple[str, str], str] = {}
    previous_id = ""
    base = candidate_repository.removeprefix("ghcr.io/")
    for definition in definitions:
        if not isinstance(definition, VerificationTestDefinitionV1):
            raise PromotionError("protected verification definition is invalid")
        identity = {
            "hosts": list(definition.hosts),
            "id": definition.id,
            "kandelo_paths": list(definition.kandelo_paths),
            "policy": definition.policy,
        }
        if (
            definition.id <= previous_id
            or canonical_sha256(identity) != definition.sha256
            or not definition.hosts
        ):
            raise PromotionError("protected verification registry identity drifted")
        previous_id = definition.id
        for host in definition.hosts:
            key = (definition.sha256, host)
            if key in result:
                raise PromotionError("protected verification identity is duplicated")
            try:
                repository = receipt_repository(base, definition.id, host)
            except VerificationError as error:
                raise PromotionError(
                    f"protected verification namespace is invalid: {error}"
                ) from error
            result[key] = "ghcr.io/" + repository
    return result


def _require_same_history_protection_authority(
    recorded: Mapping[str, Any],
    fresh: Mapping[str, Any],
) -> None:
    stable_fields = (
        "branch",
        "covered",
        "protection_requirement_sha256",
        "ref_object",
        "ref_tree",
    )
    if any(recorded.get(field) != fresh.get(field) for field in stable_fields):
        raise PromotionError("ABI history protection authority moved after publication")


def _history(
    history: FetchedOciRecordV1 | None,
    *,
    protection_snapshot: Mapping[str, Any],
    target_abi: int,
    tap_source: Mapping[str, str],
    policy: PromotionPolicyV1,
) -> None:
    record, _layers = _validated_fetched_record(
        history,
        artifact_type=HISTORY_RECORD_MEDIA_TYPE,
        required_roles=(),
        field="ABI history",
    )
    try:
        validate_abi_history_record(record)
    except TapRecordError as error:
        raise PromotionError(f"ABI history record is invalid: {error}") from error
    plan = record["plan"]
    expected_repository = (
        "ghcr.io/"
        + history_record_repository(policy.tap_repository, plan["source_abi"])
    )
    if (
        plan["successor_abi"] != target_abi
        or plan["source_abi"] + 1 != target_abi
        or plan["branch"] != policy.historical_branch_prefix + str(plan["source_abi"])
        or plan["preactivation_tap_commit"] != tap_source["commit"]
        or plan["preactivation_tap_tree"] != tap_source["tree"]
        or history is None
        or history.repository != expected_repository
    ):
        raise PromotionError("ABI history does not guard this exact successor transition")
    try:
        evidence = validate_protection_snapshot(
            plan,
            protection_snapshot,
            phase="postcreate",
            expected_repository=policy.tap_repository,
        )
    except AbiHistoryError as error:
        raise PromotionError(f"ABI history protection is invalid: {error}") from error
    _require_same_history_protection_authority(
        record["protection_evidence"],
        evidence,
    )


def validate_promotion_history_barrier(
    history: FetchedOciRecordV1 | None,
    *,
    protection_snapshot: Mapping[str, Any],
    target_abi: int,
    tap_source: Mapping[str, str],
    policy: PromotionPolicyV1,
) -> None:
    """Recheck the immutable ABI-history and protected-ref mutation barrier."""

    _history(
        history,
        protection_snapshot=protection_snapshot,
        target_abi=target_abi,
        tap_source=tap_source,
        policy=policy,
    )


def prepare_successor_activation_patch(
    *,
    tap_root: Any,
    history: FetchedOciRecordV1 | None,
    history_protection_snapshot: Mapping[str, Any],
    history_tap_source: Mapping[str, Any],
    current_tap_source: Mapping[str, Any],
    request_digest: str,
    merged_pull_request: Mapping[str, Any],
    target_abi: int,
    target_snapshot_sha256: str,
    policy: PromotionPolicyV1,
) -> TapMetadataPatchV1:
    """Require exact protected history before planning the one-time ABI switch."""

    if not isinstance(policy, PromotionPolicyV1):
        raise PromotionError("successor activation policy is not protected schema 1")
    source = _source(current_tap_source, "current activation tap source")
    if source["repository"].lower() != policy.tap_repository.lower():
        raise PromotionError("activation tap source names another repository")
    history_source = _source(history_tap_source, "activation history tap source")
    if history_source["repository"].lower() != policy.tap_repository.lower():
        raise PromotionError("activation history names another repository")
    target = _nonnegative(target_abi, "activation target ABI")
    snapshot = _digest(target_snapshot_sha256, "activation target snapshot")
    request = _digest(request_digest, "activation request")
    merged = _exact(
        merged_pull_request,
        frozenset({"repository", "number", "head", "merge_commit"}),
        "activation merged pull request",
    )
    checked_merged = {
        "repository": _repository(merged["repository"], "activation PR repository"),
        "number": _positive(merged["number"], "activation PR number"),
        "head": _git_sha(merged["head"], "activation PR head"),
        "merge_commit": _git_sha(
            merged["merge_commit"], "activation PR merge commit"
        ),
    }
    if checked_merged["repository"].lower() != policy.kandelo_repository.lower():
        raise PromotionError("activation PR names another Kandelo repository")
    _history(
        history,
        protection_snapshot=history_protection_snapshot,
        target_abi=target,
        tap_source=history_source,
        policy=policy,
    )
    assert history is not None
    record = _canonical_mapping(history.config.body, "activation ABI history")
    history_plan = _exact(
        record["plan"],
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
        "activation history plan",
    )
    activation = {
        "request_digest": request,
        "merged_pull_request": checked_merged,
        "merge_commit": checked_merged["merge_commit"],
        "prior_abi": history_plan["source_abi"],
        "prior_branch": history_plan["branch"],
        "abi_history_record_digest": history.digest.removeprefix("sha256:"),
    }
    try:
        return plan_successor_activation_patch(
            tap_root,
            current_tap_source=source,
            target_abi=target,
            target_snapshot_sha256=snapshot,
            activation=activation,
        )
    except TapMetadataError as error:
        raise PromotionError(f"successor activation metadata is invalid: {error}") from error


def _formula_contract_changed(
    planned: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    current = _exact(
        current,
        frozenset(
            {
                "identity",
                "capture",
                "contract_sha256",
                "direct_dependencies",
                "required_by_products",
                "work_class",
            }
        ),
        "current Formula plan",
    )
    for field in ("identity", "capture", "direct_dependencies"):
        if canonical_bytes(current[field]) != canonical_bytes(planned[field]):
            return True
    return False


def _dependency_layers_changed(
    candidate_record: Mapping[str, Any],
    current_dependency_layers: Mapping[str, Mapping[str, Any]] | None,
    formula_plan: Mapping[str, Any],
) -> bool:
    if not isinstance(current_dependency_layers, Mapping):
        raise PromotionError("current dependency layer inventory is not an object")
    candidate_layers = {
        item["id"]: _artifact(item["artifact"], f"candidate dependency {item['id']}")
        for item in candidate_record["candidate"]["direct_dependency_layers"]
    }
    for dependency in formula_plan["direct_dependencies"]:
        name = dependency["formula"]
        architecture = dependency["architecture"]
        subject = exact_formula_subject(name, architecture)
        current = current_dependency_layers.get(subject)
        if current is None:
            raise PromotionError("current dependency layer inventory is incomplete")
        expected = candidate_layers.get(f"{name}-{architecture}")
        if expected is None:
            raise PromotionError("candidate dependency layer inventory is incomplete")
        checked_current = _artifact(current, f"current dependency {subject}")
        if (
            checked_current["sha256"] != expected["sha256"]
            or checked_current["bytes"] != expected["bytes"]
        ):
            return True
    return False


def evaluate_promotion(
    *,
    request: Mapping[str, Any],
    request_digest: str,
    merge_fact: Mapping[str, Any],
    tap_plan: Mapping[str, Any],
    tap_plan_digest: str,
    candidate: FetchedOciRecordV1,
    candidate_reuse: FetchedOciRecordV1 | None = None,
    source_custody: FetchedOciRecordV1,
    verification_receipts: Sequence[FetchedOciRecordV1],
    override_receipts: Sequence[FetchedOciRecordV1],
    history: FetchedOciRecordV1 | None,
    history_protection_snapshot: Mapping[str, Any],
    current_tap_source: Mapping[str, Any],
    current_formula: Mapping[str, Any],
    current_dependency_layers: Mapping[str, Mapping[str, Any]],
    policy: PromotionPolicyV1,
    expected_request_policy: Mapping[str, Any],
    verification_tests: Sequence[VerificationTestDefinitionV1],
    history_tap_source: Mapping[str, Any] | None = None,
) -> PromotionDecisionV1:
    """Re-evaluate one Formula against exact merge, record, and tap authority."""

    if not isinstance(policy, PromotionPolicyV1):
        raise PromotionError("promotion policy is not protected schema 1")
    pull, request_source, target_abi = _request_context(
        request, request_digest, expected_request_policy
    )
    if pull["repository"].lower() != policy.kandelo_repository.lower():
        raise PromotionError("request repository differs from promotion policy")
    merged = _merged_pull_request(merge_fact, request_pull=pull, source=request_source)
    try:
        validate_tap_plan(tap_plan)
    except PlanError as error:
        raise PromotionError(f"tap plan is invalid: {error}") from error
    checked_plan_digest = _digest(tap_plan_digest, "tap plan")
    if (
        canonical_sha256(tap_plan) != checked_plan_digest
        or tap_plan["request_digest"] != request_digest
        or tap_plan["target_abi"] != request["target_abi"]
        or tap_plan["tap_source"]["repository"].lower()
        != policy.tap_repository.lower()
    ):
        raise PromotionError("tap plan differs from request, target, or protected tap")
    planned_tap_source = _source(tap_plan["tap_source"], "planned tap source")
    protected_history_source = (
        planned_tap_source
        if history_tap_source is None
        else _source(history_tap_source, "protected history tap source")
    )
    candidate_record, formula_plan, bottle, _contract_digest, custody_digest = (
        _candidate_facts(
            candidate,
            source_custody,
            request_digest=request_digest,
            request_source=request_source,
            target_abi=target_abi,
            tap_plan=tap_plan,
            policy=policy,
            allow_historical_request=candidate_reuse is not None,
        )
    )
    candidate_digest = candidate.digest.removeprefix("sha256:")
    binding_digest = candidate_digest
    reuse_record = None
    if candidate_reuse is not None:
        binding_digest, reuse_record = _candidate_reuse_binding(
            candidate_reuse,
            request_digest=request_digest,
            request_source=request_source,
            candidate=candidate,
            candidate_record=candidate_record,
            source_custody=source_custody,
            bottle=bottle,
            contract_digest=_contract_digest,
            target_abi=target_abi,
            policy=policy,
        )
    expected_verification = _verification_authority(
        verification_tests,
        candidate_repository=candidate.repository,
    )
    receipt_digests: list[str] = []
    failed_guards: set[str] = set()
    seen_verification: set[tuple[str, str]] = set()
    for receipt in verification_receipts:
        digest, guard, identity = _verification_receipt(
            receipt,
            request_digest=request_digest,
            request_source=request_source,
            candidate_digest=candidate_digest,
            bottle=bottle,
            expected_authority=expected_verification,
        )
        if identity in seen_verification:
            raise PromotionError("verification identity repeats in promotion input")
        seen_verification.add(identity)
        if guard is None:
            receipt_digests.append(digest)
        else:
            failed_guards.add(guard)
    override_results = [
        _override_receipt(
            receipt,
            request_digest=request_digest,
            request_source=request_source,
            candidate_digest=candidate_digest,
            bottle=bottle,
            request_policy=request["issuance"],
            failed_guards=failed_guards,
            expected_repository=(
                candidate.repository + "/receipts/overrides"
            ),
        )
        for receipt in override_receipts
    ]
    override_digests = [result[0] for result in override_results]
    accepted_guards = {
        guard for _digest_value, guards in override_results for guard in guards
    }
    if len(receipt_digests) != len(set(receipt_digests)):
        raise PromotionError("qualifying verification receipts repeat")
    if len(override_digests) != len(set(override_digests)):
        raise PromotionError("override receipts repeat")
    receipt_digests.sort()
    override_digests.sort()
    if reuse_record is not None:
        if override_receipts:
            raise PromotionError(
                "candidate reuse cannot acquire a new override for old bytes"
            )
        # The reuse record preserves the receipts that qualified the original
        # candidate. Promotion is qualified independently by receipts issued
        # for the current request, so the two immutable receipt sets must not
        # be conflated.
        if failed_guards:
            raise PromotionError(
                "candidate reuse differs from its exact qualifying receipts"
            )
    verification_complete = set(expected_verification) == seen_verification
    verification_accepted = failed_guards.issubset(accepted_guards)

    _history(
        history,
        protection_snapshot=history_protection_snapshot,
        target_abi=target_abi,
        tap_source=protected_history_source,
        policy=policy,
    )
    current_source = _source(current_tap_source, "current tap source")
    if current_source["repository"].lower() != policy.tap_repository.lower():
        raise PromotionError("current tap source names another repository")
    formula_changed = _formula_contract_changed(formula_plan, current_formula)
    dependency_changed = (
        False
        if current_dependency_layers is None
        else _dependency_layers_changed(
            candidate_record, current_dependency_layers, formula_plan
        )
    )
    if formula_changed or dependency_changed:
        tap_source_state = "rebuild-required"
        eligibility = "rebuild-required"
    else:
        tap_source_state = "exact" if current_source == planned_tap_source else "drift"
        eligibility = (
            "eligible"
            if verification_complete and verification_accepted
            else "ineligible"
        )

    decision = PromotionDecisionV1(
        request_digest=request_digest,
        merged_pull_request=merged,
        formula_subject=exact_formula_subject(
            candidate_record["candidate"]["formula"]["formula"],
            candidate_record["candidate"]["formula"]["architecture"],
        ),
        tap_plan_digest=checked_plan_digest,
        candidate_record_digest=candidate_digest,
        candidate_binding_digest=binding_digest,
        bottle_layer_sha256=str(bottle["sha256"]),
        bottle_layer_bytes=int(bottle["bytes"]),
        source_custody_digest=custody_digest,
        qualifying_receipts=tuple(receipt_digests),
        override_receipts=tuple(override_digests),
        tap_source_state=tap_source_state,
        eligibility=eligibility,
    )
    validate_promotion_decision(asdict(decision))
    return decision


def validate_promotion_decision(value: Mapping[str, Any]) -> None:
    decision = _exact(
        value,
        frozenset(PromotionDecisionV1.__dataclass_fields__),
        "promotion decision",
    )
    _digest(decision["request_digest"], "promotion request")
    merged = _exact(
        decision["merged_pull_request"],
        frozenset({"repository", "number", "head", "merge_commit"}),
        "promotion merged PR",
    )
    _repository(merged["repository"], "promotion merged PR repository")
    _positive(merged["number"], "promotion merged PR number")
    _git_sha(merged["head"], "promotion merged PR head")
    _git_sha(merged["merge_commit"], "promotion merged PR commit")
    subject = decision["formula_subject"]
    try:
        parse_formula_subject(subject, "promotion Formula subject")
    except PlanError as error:
        raise PromotionError(f"promotion Formula subject is invalid: {error}") from error
    _digest(decision["tap_plan_digest"], "promotion tap plan")
    _digest(decision["candidate_record_digest"], "promotion candidate")
    _digest(decision["candidate_binding_digest"], "promotion candidate binding")
    _digest(decision["bottle_layer_sha256"], "promotion bottle layer")
    _positive(decision["bottle_layer_bytes"], "promotion bottle bytes")
    _digest(decision["source_custody_digest"], "promotion source custody")
    for field in ("qualifying_receipts", "override_receipts"):
        values = decision[field]
        if (
            not isinstance(values, (list, tuple))
            or list(values) != sorted(set(values))
        ):
            raise PromotionError(f"promotion {field} is not sorted and unique")
        for item in values:
            _digest(item, f"promotion {field}")
    if decision["tap_source_state"] not in {"exact", "drift", "rebuild-required"}:
        raise PromotionError("promotion tap source state is unsupported")
    if decision["eligibility"] not in {"eligible", "ineligible", "rebuild-required"}:
        raise PromotionError("promotion eligibility is unsupported")
    if (
        decision["eligibility"] == "eligible"
        and not decision["qualifying_receipts"]
        and not decision["override_receipts"]
    ):
        raise PromotionError("eligible promotion has no qualifying trust receipt")
    if (decision["tap_source_state"] == "rebuild-required") != (
        decision["eligibility"] == "rebuild-required"
    ):
        raise PromotionError("promotion source and eligibility states contradict")


def canonical_repository(
    policy: PromotionPolicyV1, target_abi: int, formula: str, *, candidate: bool = False
) -> str:
    if not isinstance(policy, PromotionPolicyV1):
        raise PromotionError("promotion policy is invalid")
    owner, tap_name = policy.tap_repository.lower().split("/", 1)
    expected_prefix = tap_name + "-abi-"
    if policy.canonical_repository_prefix != expected_prefix:
        raise PromotionError("canonical repository policy does not derive from the tap")
    abi = _nonnegative(target_abi, "canonical target ABI")
    name = _stable_id(formula, "canonical Formula")
    suffix = "-candidates" if candidate else ""
    return f"{owner}/{policy.canonical_repository_prefix}{abi}{suffix}/{name}"


def _candidate_layer(candidate: FetchedOciRecordV1) -> tuple[dict[str, Any], FetchedOciBlobV1]:
    record, layers = _validated_fetched_record(
        candidate,
        artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
        required_roles=("bottle-layer",),
        field="candidate",
    )
    try:
        validate_candidate_record(record)
    except TapRecordError as error:
        raise PromotionError(f"candidate record is invalid: {error}") from error
    return record, layers["bottle-layer"]


def _candidate_bottle_metadata(
    candidate: FetchedOciRecordV1,
) -> tuple[dict[str, Any], dict[str, Any], FetchedOciBlobV1]:
    record, layers = _validated_fetched_record(
        candidate,
        artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
        required_roles=("bottle-layer", "bottle-metadata", "bottle-contract"),
        field="candidate bottle metadata",
    )
    try:
        validate_candidate_record(record)
    except TapRecordError as error:
        raise PromotionError(f"candidate record is invalid: {error}") from error
    components = [
        item["artifact"]
        for item in record["candidate"]["normalized_components"]
        if item["id"] == "bottle-metadata"
    ]
    if len(components) != 1:
        raise PromotionError("candidate has no exact bottle metadata identity")
    component = _artifact(components[0], "candidate bottle metadata")
    layer = layers["bottle-metadata"]
    if (
        component["sha256"] != layer.digest.removeprefix("sha256:")
        or component["bytes"] != layer.size
        or component["immutable_reference"]
        != f"{candidate.repository}@{layer.digest}"
    ):
        raise PromotionError("candidate bottle metadata differs from its record")
    return record, _canonical_mapping(layer.body, "candidate bottle metadata"), layer


def _candidate_bottle_contract(
    candidate: FetchedOciRecordV1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    record, layers = _validated_fetched_record(
        candidate,
        artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
        required_roles=("bottle-layer", "bottle-metadata", "bottle-contract"),
        field="candidate bottle contract",
    )
    try:
        validate_candidate_record(record)
    except TapRecordError as error:
        raise PromotionError(f"candidate record is invalid: {error}") from error
    components = [
        item["artifact"]
        for item in record["candidate"]["normalized_components"]
        if item["id"] == "bottle-contract"
    ]
    if len(components) != 1:
        raise PromotionError("candidate has no exact bottle contract identity")
    component = _artifact(components[0], "candidate bottle contract")
    layer = layers["bottle-contract"]
    if (
        component["sha256"] != layer.digest.removeprefix("sha256:")
        or component["bytes"] != layer.size
        or component["immutable_reference"]
        != f"{candidate.repository}@{layer.digest}"
    ):
        raise PromotionError("candidate bottle contract differs from its record")
    try:
        contract = load_bottle_contract(layer.body)
    except ContractError as error:
        raise PromotionError(f"candidate bottle contract is invalid: {error}") from error
    return record, contract


def _candidate_vfs_composition_descriptor(
    candidate: FetchedOciRecordV1,
) -> tuple[dict[str, Any], dict[str, Any], FetchedOciBlobV1]:
    record, layers = _validated_fetched_record(
        candidate,
        artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
        required_roles=(
            "bottle-layer",
            "vfs-composition-descriptor",
        ),
        field="candidate VFS composition descriptor",
    )
    try:
        validate_candidate_record(record)
    except TapRecordError as error:
        raise PromotionError(f"candidate record is invalid: {error}") from error
    components = [
        item["artifact"]
        for item in record["candidate"]["normalized_components"]
        if item["id"] == "vfs-composition-descriptor"
    ]
    if len(components) != 1:
        raise PromotionError("candidate has no exact VFS composition descriptor identity")
    component = _artifact(components[0], "candidate VFS composition descriptor")
    layer = layers["vfs-composition-descriptor"]
    if (
        component["sha256"] != layer.digest.removeprefix("sha256:")
        or component["bytes"] != layer.size
        or component["immutable_reference"]
        != f"{candidate.repository}@{layer.digest}"
        or layer.media_type != VFS_COMPOSITION_DESCRIPTOR_MEDIA_TYPE
    ):
        raise PromotionError(
            "candidate VFS composition descriptor differs from its record"
        )
    return (
        record,
        _canonical_mapping(
            layer.body,
            "candidate VFS composition descriptor",
            maximum_items=MAX_VFS_COMPOSITION_JSON_ITEMS,
        ),
        layer,
    )


def _canonical_vfs_composition_descriptor(
    descriptor: Mapping[str, Any],
    *,
    record: Mapping[str, Any],
    candidate_repository: str,
    canonical_repository_name: str,
) -> bytes:
    payload = _exact(
        record["candidate"],
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
    formula = _exact(
        payload["formula"],
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
    bottle = _artifact(payload["bottle_layer"], "candidate bottle layer")
    value = _exact(
        descriptor,
        frozenset(
            {
                "schema",
                "kind",
                "architecture",
                "tap",
                "formula",
                "required_by",
                "tree",
            }
        ),
        "candidate VFS composition descriptor",
    )
    roots = value["required_by"]
    if (
        value["schema"] != 1
        or value["kind"] != "kandelo-homebrew-original-bottle-tree"
        or value["architecture"] != formula["architecture"]
        or value["tap"] != formula["tap"]
        or value["formula"] != formula["formula"]
        or not isinstance(roots, list)
        or not roots
        or len(roots) > 256
        or any(not isinstance(root, str) or STABLE_ID.fullmatch(root) is None for root in roots)
        or roots != sorted(set(roots))
    ):
        raise PromotionError("candidate VFS composition descriptor identity is invalid")
    tree = _exact(
        value["tree"],
        frozenset(
            {
                "id",
                "package",
                "activation",
                "content",
                "transports",
                "inventory",
            }
        ),
        "candidate VFS composition tree",
    )
    content = _exact(
        tree["content"],
        frozenset({"media_type", "decoder", "sha256", "bytes"}),
        "candidate VFS composition content",
    )
    candidate_repository_name = candidate_repository.removeprefix("ghcr.io/")
    candidate_url = (
        "https://ghcr.io/v2/"
        + candidate_repository_name
        + "/blobs/sha256:"
        + str(bottle["sha256"])
    )
    transports = tree["transports"]
    if (
        tree["id"] != formula["formula"]
        or not isinstance(tree["package"], str)
        or not isinstance(tree["activation"], Mapping)
        or not isinstance(tree["inventory"], Mapping)
        or content
        != {
            "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
            "decoder": "homebrew-bottle-tar-gzip-v1",
            "sha256": bottle["sha256"],
            "bytes": bottle["bytes"],
        }
        or transports != [{"kind": "external-https", "url": candidate_url}]
    ):
        raise PromotionError(
            "candidate VFS composition tree differs from its exact bottle"
        )
    canonical_url = (
        "https://ghcr.io/v2/"
        + canonical_repository_name
        + "/blobs/sha256:"
        + str(bottle["sha256"])
    )
    canonical = copy.deepcopy(_plain(value))
    canonical["tree"]["transports"] = [
        {"kind": "external-https", "url": canonical_url}
    ]
    body = canonical_bytes(
        canonical,
        maximum_items=MAX_VFS_COMPOSITION_JSON_ITEMS,
    )
    if b"-candidates/" in body:
        raise PromotionError(
            "canonical VFS composition descriptor retains candidate authority"
        )
    return body


def build_canonical_bottle_plan_from_identity(
    identity: CanonicalBottleIdentityV1,
    *,
    candidate: FetchedOciRecordV1,
    policy: PromotionPolicyV1,
    destination_repository: str | None = None,
) -> OciRecordPlanV1:
    if not isinstance(identity, CanonicalBottleIdentityV1):
        raise PromotionError("canonical bottle identity is missing")
    _digest(identity.request_digest, "canonical request")
    if identity.classification == "canonical-direct":
        if identity.merged_pull_request is not None:
            raise PromotionError("direct canonical identity retains a merged PR")
        source = _source(identity.source, "direct canonical source")
        merged = None
    else:
        if identity.source is not None:
            raise PromotionError("admission canonical identity carries direct source")
        merged = _exact(
            identity.merged_pull_request,
            frozenset({"repository", "number", "head", "merge_commit"}),
            "canonical merged PR",
        )
        _repository(merged["repository"], "canonical merged PR repository")
        _positive(merged["number"], "canonical merged PR number")
        _git_sha(merged["head"], "canonical merged PR head")
        _git_sha(merged["merge_commit"], "canonical merged PR commit")
        source = None
    try:
        parse_formula_subject(identity.formula_subject, "canonical Formula subject")
    except PlanError as error:
        raise PromotionError(f"canonical Formula subject is invalid: {error}") from error
    _digest(identity.candidate_record_digest, "canonical candidate")
    _digest(identity.bottle_layer_sha256, "canonical bottle layer")
    _positive(identity.bottle_layer_bytes, "canonical bottle bytes")
    if identity.classification not in {
        "canonical-pending-admission",
        "canonical-direct",
    }:
        raise PromotionError("canonical bottle classification is unsupported")
    record, layer = _candidate_layer(candidate)
    metadata_record, _bottle_metadata, metadata_layer = _candidate_bottle_metadata(candidate)
    (
        descriptor_record,
        candidate_descriptor,
        _candidate_descriptor_layer,
    ) = _candidate_vfs_composition_descriptor(candidate)
    if (
        canonical_bytes(metadata_record) != canonical_bytes(record)
        or canonical_bytes(descriptor_record) != canonical_bytes(record)
    ):
        raise PromotionError("candidate metadata changed its record identity")
    formula = record["candidate"]["formula"]
    if identity.formula_subject != exact_formula_subject(
        formula["formula"], formula["architecture"]
    ):
        raise PromotionError("canonical plan Formula differs from its exact identity")
    expected = canonical_repository(policy, formula["target_abi"], formula["formula"])
    destination = expected if destination_repository is None else destination_repository
    if destination != expected or destination == candidate.repository.removeprefix("ghcr.io/"):
        raise PromotionError("canonical destination is not the exact endorsed ABI namespace")
    if (
        candidate.digest.removeprefix("sha256:") != identity.candidate_record_digest
        or layer.digest.removeprefix("sha256:") != identity.bottle_layer_sha256
        or layer.size != identity.bottle_layer_bytes
    ):
        raise PromotionError("canonical plan candidate/layer differs from promotion decision")
    canonical_descriptor_body = _canonical_vfs_composition_descriptor(
        candidate_descriptor,
        record=record,
        candidate_repository=candidate.repository,
        canonical_repository_name=destination,
    )
    canonical_descriptor_sha256 = hashlib.sha256(canonical_descriptor_body).hexdigest()
    metadata = {
        "schema": 1,
        "kind": "kandelo-homebrew-canonical-bottle",
        "classification": identity.classification,
        "request_sha256": identity.request_digest,
        "candidate_record_sha256": identity.candidate_record_digest,
        "formula": {
            "tap": formula["tap"],
            "name": formula["formula"],
            "architecture": formula["architecture"],
            "target_abi": formula["target_abi"],
        },
        "bottle_layer": {
            "sha256": identity.bottle_layer_sha256,
            "bytes": identity.bottle_layer_bytes,
        },
        "bottle_metadata": {
            "sha256": metadata_layer.digest.removeprefix("sha256:"),
            "bytes": metadata_layer.size,
        },
        "vfs_composition_descriptor": {
            "sha256": canonical_descriptor_sha256,
            "bytes": len(canonical_descriptor_body),
        },
    }
    if merged is None:
        metadata["source"] = _plain(source)
    else:
        metadata["merged_pull_request"] = _plain(merged)
    return OciRecordPlanV1(
        repository=destination,
        artifact_type=CANONICAL_BOTTLE_MANIFEST_MEDIA_TYPE,
        config=OciBlobV1(
            role="canonical-bottle-metadata",
            media_type=CANONICAL_BOTTLE_MANIFEST_MEDIA_TYPE,
            body=canonical_bytes(metadata),
            title="canonical-bottle.json",
        ),
        layers=(
            OciBlobV1(
                role="bottle-layer",
                media_type=BOTTLE_LAYER_MEDIA_TYPE,
                body=layer.body,
                title=layer.title,
                mount_from=candidate.repository.removeprefix("ghcr.io/"),
            ),
            OciBlobV1(
                role="bottle-metadata",
                media_type=BOTTLE_METADATA_MEDIA_TYPE,
                body=metadata_layer.body,
                title=metadata_layer.title,
                mount_from=candidate.repository.removeprefix("ghcr.io/"),
            ),
            OciBlobV1(
                role="vfs-composition-descriptor",
                media_type=VFS_COMPOSITION_DESCRIPTOR_MEDIA_TYPE,
                body=canonical_descriptor_body,
                title="vfs-composition-descriptor.json",
            ),
        ),
        annotations={
            "dev.kandelo.abi-staging.candidate-record-sha256": (
                identity.candidate_record_digest
            ),
            "dev.kandelo.abi-staging.classification": identity.classification,
            "dev.kandelo.abi-staging.formula": formula["formula"],
            "dev.kandelo.abi-staging.kind": "canonical-bottle",
            "dev.kandelo.abi-staging.target-abi": str(formula["target_abi"]),
            "org.opencontainers.image.source": "https://github.com/" + policy.tap_repository,
        },
    )


def build_canonical_bottle_plan(
    decision: PromotionDecisionV1,
    *,
    candidate: FetchedOciRecordV1,
    policy: PromotionPolicyV1,
    destination_repository: str | None = None,
) -> OciRecordPlanV1:
    validate_promotion_decision(asdict(decision))
    if decision.eligibility != "eligible":
        raise PromotionError("ineligible candidate cannot receive a canonical manifest")
    return build_canonical_bottle_plan_from_identity(
        CanonicalBottleIdentityV1(
            request_digest=decision.request_digest,
            formula_subject=decision.formula_subject,
            candidate_record_digest=decision.candidate_record_digest,
            bottle_layer_sha256=decision.bottle_layer_sha256,
            bottle_layer_bytes=decision.bottle_layer_bytes,
            classification="canonical-pending-admission",
            merged_pull_request=decision.merged_pull_request,
        ),
        candidate=candidate,
        policy=policy,
        destination_repository=destination_repository,
    )


def validate_canonical_bottle_plan(
    plan: OciRecordPlanV1,
    *,
    decision: PromotionDecisionV1,
    candidate: FetchedOciRecordV1,
    policy: PromotionPolicyV1,
) -> None:
    expected = build_canonical_bottle_plan(
        decision, candidate=candidate, policy=policy
    )
    if plan != expected:
        raise PromotionError("canonical bottle plan changed bytes, destination, or authority")


def _canonical_publication_identity(plan: OciRecordPlanV1) -> dict[str, Any]:
    manifest = build_oci_manifest(plan)
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    repository = "ghcr.io/" + plan.repository
    reference = f"{repository}@{digest}"
    return {
        "repository": repository,
        "digest": digest,
        "immutable_reference": reference,
        "anonymous_readback_sha256": canonical_sha256(
            {
                "manifest": {"digest": digest, "size": len(manifest)},
                "blobs": [
                    {"digest": blob.digest, "role": blob.role, "size": blob.size}
                    for blob in (plan.config, *plan.layers)
                ],
            }
        ),
        "artifact": {
            "sha256": digest.removeprefix("sha256:"),
            "bytes": len(manifest),
            "immutable_reference": reference,
        },
    }


def publish_canonical_bottle(
    plan: OciRecordPlanV1,
    *,
    decision: PromotionDecisionV1,
    candidate: FetchedOciRecordV1,
    policy: PromotionPolicyV1,
    transport: OciTransportV1,
) -> CanonicalBottlePublicationV1:
    validate_canonical_bottle_plan(
        plan, decision=decision, candidate=candidate, policy=policy
    )
    locator = publish_immutable_oci_plan(
        plan,
        transport=transport,
        expected_source_repository=policy.tap_repository,
        tag_prefix="canonical-sha256-",
    )
    expected = _canonical_publication_identity(plan)
    artifact = expected["artifact"]
    if (
        locator.repository != expected["repository"]
        or locator.digest != expected["digest"]
        or locator.immutable_reference != expected["immutable_reference"]
        or locator.anonymous_readback_sha256
        != expected["anonymous_readback_sha256"]
    ):
        raise PromotionError("canonical manifest lacks exact anonymous readback")
    return CanonicalBottlePublicationV1(
        locator=locator,
        artifact=MappingProxyType(artifact),
    )


def expected_canonical_publication(
    decision: PromotionDecisionV1,
    *,
    candidate: FetchedOciRecordV1,
    policy: PromotionPolicyV1,
) -> CanonicalBottlePublicationV1:
    """Derive the one canonical locator before publication without claiming I/O."""

    plan = build_canonical_bottle_plan(decision, candidate=candidate, policy=policy)
    expected = _canonical_publication_identity(plan)
    return CanonicalBottlePublicationV1(
        locator=PublishedRecordLocatorV1(
            repository=expected["repository"],
            digest=expected["digest"],
            immutable_reference=expected["immutable_reference"],
            anonymous_readback_sha256=expected["anonymous_readback_sha256"],
        ),
        artifact=MappingProxyType(expected["artifact"]),
    )


def read_canonical_publication(
    decision: PromotionDecisionV1,
    *,
    candidate: FetchedOciRecordV1,
    policy: PromotionPolicyV1,
    transport: OciTransportV1,
) -> CanonicalBottlePublicationV1:
    """Require the exact expected manifest and unchanged layer by anonymous readback."""

    expected = expected_canonical_publication(
        decision, candidate=candidate, policy=policy
    )
    fetched = fetch_public_record(
        {
            "repository": expected.locator.repository,
            "digest": expected.locator.digest,
            "immutable_reference": expected.locator.immutable_reference,
        },
        transport=transport,
        expected_artifact_type=CANONICAL_BOTTLE_MANIFEST_MEDIA_TYPE,
        required_layer_roles=(
            "bottle-layer",
            "bottle-metadata",
            "vfs-composition-descriptor",
        ),
    )
    plan = build_canonical_bottle_plan(decision, candidate=candidate, policy=policy)
    if (
        fetched.manifest != build_oci_manifest(plan)
        or fetched.config.body != plan.config.body
        or len(fetched.layers) != len(plan.layers)
        or any(
            fetched_layer.body != planned_layer.body
            or _descriptor_identity(fetched_layer)
            != _descriptor_identity(planned_layer)
            for fetched_layer, planned_layer in zip(
                fetched.layers, plan.layers, strict=True
            )
        )
    ):
        raise PromotionError("canonical readback differs from the exact unchanged-layer plan")
    return expected


def admission_repository(
    policy: PromotionPolicyV1, target_abi: int, formula: str
) -> str:
    return canonical_repository(policy, target_abi, formula) + "/admissions"


def build_admission_oci_plan(
    record: Mapping[str, Any],
    *,
    policy: PromotionPolicyV1,
) -> OciRecordPlanV1:
    """Wrap one final admission as an immutable factual record."""

    try:
        validate_admission_record(record)
    except TapRecordError as error:
        raise PromotionError(f"admission record is invalid: {error}") from error
    admission = _exact(
        record["admission"],
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
    update = _exact(
        admission["formula_metadata_update"],
        frozenset(
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
        ),
        "admission Formula metadata update",
    )
    formula = _stable_id(update["formula"], "admission Formula")
    target_abi = _nonnegative(update["target_abi"], "admission target ABI")
    body = canonical_bytes(_plain(record))
    repository = admission_repository(policy, target_abi, formula)
    return OciRecordPlanV1(
        repository=repository,
        artifact_type=ADMISSION_RECORD_MEDIA_TYPE,
        config=OciBlobV1(
            role="admission-record",
            media_type=ADMISSION_RECORD_MEDIA_TYPE,
            body=body,
            title="admission-record.json",
        ),
        layers=(
            OciBlobV1(
                role="immutable-record-bytes",
                media_type=ADMISSION_RECORD_MEDIA_TYPE,
                body=body,
                title="admission-record.json",
            ),
        ),
        annotations={
            "dev.kandelo.abi-staging.candidate-record-sha256": str(
                admission["candidate_record_sha256"]
            ),
            "dev.kandelo.abi-staging.classification": "admitted-canonical",
            "dev.kandelo.abi-staging.formula": formula,
            "dev.kandelo.abi-staging.kind": "admission",
            "dev.kandelo.abi-staging.target-abi": str(target_abi),
            "org.opencontainers.image.source": "https://github.com/"
            + policy.tap_repository,
        },
    )


def publish_admission_record(
    record: Mapping[str, Any],
    *,
    policy: PromotionPolicyV1,
    transport: OciTransportV1,
) -> PublishedRecordLocatorV1:
    plan = build_admission_oci_plan(record, policy=policy)
    return publish_immutable_oci_plan(
        plan,
        transport=transport,
        expected_source_repository=policy.tap_repository,
        tag_prefix="record-sha256-",
    )


def metadata_patch_document(
    patch: TapMetadataPatchV1,
    *,
    formula_update: FormulaMetadataUpdateV1 | None,
) -> dict[str, Any]:
    """Encode one bounded contents-only patch without executable handoff files."""

    if not isinstance(patch, TapMetadataPatchV1):
        raise PromotionError("metadata patch is untyped")
    if (patch.operation == "formula-metadata") != (formula_update is not None):
        raise PromotionError("metadata patch and Formula update are contradictory")
    files = []
    total = 0
    for path in sorted(patch.files):
        body = patch.files[path]
        if not isinstance(body, bytes):
            raise PromotionError("metadata patch file body is not bytes")
        total += len(body)
        if total > 64 * 1024 * 1024:
            raise PromotionError("metadata patch files exceed their aggregate bound")
        files.append(
            {
                "path": path,
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "base64": base64.b64encode(body).decode("ascii"),
            }
        )
    update = None if formula_update is None else asdict(formula_update)
    document = {
        "schema": 1,
        "kind": "kandelo-abi-staging-metadata-patch",
        "operation": patch.operation,
        "expected_main_commit": patch.expected_main_commit,
        "expected_main_tree": patch.expected_main_tree,
        "allowed_paths": list(patch.allowed_paths),
        "expected_files_sha256": dict(patch.expected_files_sha256),
        "files": files,
        "formula_update": update,
    }
    load_metadata_patch_document(canonical_bytes(document))
    return document


def load_metadata_patch_document(
    body: bytes,
) -> tuple[TapMetadataPatchV1, FormulaMetadataUpdateV1 | None]:
    try:
        value = parse_canonical_bytes(body, maximum_bytes=80 * 1024 * 1024)
    except CanonicalJsonError as error:
        raise PromotionError(f"metadata patch document is invalid: {error}") from error
    value = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "operation",
                "expected_main_commit",
                "expected_main_tree",
                "allowed_paths",
                "expected_files_sha256",
                "files",
                "formula_update",
            }
        ),
        "metadata patch document",
    )
    if (
        value["schema"] != 1
        or value["kind"] != "kandelo-abi-staging-metadata-patch"
        or value["operation"] not in {"successor-activation", "formula-metadata"}
    ):
        raise PromotionError("metadata patch document protocol is unsupported")
    allowed = tuple(value["allowed_paths"])
    if (
        not allowed
        or allowed != tuple(dict.fromkeys(allowed))
        or any(not isinstance(path, str) for path in allowed)
    ):
        raise PromotionError("metadata patch allowed paths are invalid")
    expected = value["expected_files_sha256"]
    if not isinstance(expected, Mapping) or set(expected) != set(allowed):
        raise PromotionError("metadata patch expected path set changed")
    checked_expected = {}
    for path in allowed:
        digest = expected[path]
        checked_expected[path] = None if digest is None else _digest(
            digest, f"metadata expected file {path}"
        )
    decoded: dict[str, bytes] = {}
    total = 0
    for item_value in value["files"]:
        item = _exact(
            item_value,
            frozenset({"path", "sha256", "bytes", "base64"}),
            "metadata patch file",
        )
        path = _text(item["path"], "metadata patch file path", 4096)
        if path not in allowed or path in decoded:
            raise PromotionError("metadata patch file path is unexpected or repeated")
        try:
            decoded_body = base64.b64decode(item["base64"], validate=True)
        except (TypeError, ValueError) as error:
            raise PromotionError("metadata patch file is not canonical base64") from error
        size = _nonnegative(item["bytes"], "metadata patch file bytes")
        if (
            base64.b64encode(decoded_body).decode("ascii") != item["base64"]
            or len(decoded_body) != size
            or hashlib.sha256(decoded_body).hexdigest()
            != _digest(item["sha256"], "metadata patch file digest")
        ):
            raise PromotionError("metadata patch file identity changed")
        total += size
        if total > 64 * 1024 * 1024:
            raise PromotionError("metadata patch files exceed their aggregate bound")
        decoded[path] = decoded_body
    patch = TapMetadataPatchV1(
        operation=value["operation"],
        expected_main_commit=_git_sha(
            value["expected_main_commit"], "metadata patch expected commit"
        ),
        expected_main_tree=_git_sha(
            value["expected_main_tree"], "metadata patch expected tree"
        ),
        allowed_paths=allowed,
        expected_files_sha256=MappingProxyType(checked_expected),
        files=MappingProxyType(decoded),
    )
    raw_update = value["formula_update"]
    if raw_update is None:
        update = None
    else:
        update_value = _exact(
            raw_update,
            frozenset(FormulaMetadataUpdateV1.__dataclass_fields__),
            "metadata Formula update",
        )
        update = FormulaMetadataUpdateV1(
            formula=update_value["formula"],
            architecture=update_value["architecture"],
            expected_main_commit=update_value["expected_main_commit"],
            expected_normalized_formula_sha256=update_value[
                "expected_normalized_formula_sha256"
            ],
            expected_generated_metadata_sha256=update_value[
                "expected_generated_metadata_sha256"
            ],
            allowed_paths=tuple(update_value["allowed_paths"]),
            link_manifest_path=update_value["link_manifest_path"],
            link_manifest_sha256=update_value["link_manifest_sha256"],
            canonical_manifest_digest=update_value["canonical_manifest_digest"],
            bottle_layer_sha256=update_value["bottle_layer_sha256"],
            bottle_layer_bytes=update_value["bottle_layer_bytes"],
            target_abi=update_value["target_abi"],
        )
    if (patch.operation == "formula-metadata") != (update is not None):
        raise PromotionError("metadata patch Formula update is contradictory")
    return patch, update


def validate_promotion_candidate_binding(
    decision: PromotionDecisionV1,
    *,
    candidate: FetchedOciRecordV1,
    candidate_reuse: FetchedOciRecordV1 | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return current-request and original-candidate sources after exact binding."""

    validate_promotion_decision(asdict(decision))
    record, layer = _candidate_layer(candidate)
    candidate_digest = candidate.digest.removeprefix("sha256:")
    candidate_source = _source(record["common"]["source"], "candidate source")
    candidate_request = _digest(
        record["common"]["request_sha256"], "candidate request"
    )
    payload = record["candidate"]
    custody = [
        item["artifact"]
        for item in payload["normalized_components"]
        if item["id"] == "source-custody"
    ]
    if (
        candidate_digest != decision.candidate_record_digest
        or layer.digest.removeprefix("sha256:") != decision.bottle_layer_sha256
        or layer.size != decision.bottle_layer_bytes
        or len(custody) != 1
        or custody[0]["sha256"] != decision.source_custody_digest
    ):
        raise PromotionError("candidate differs from its protected promotion decision")
    if candidate_reuse is None:
        if (
            decision.candidate_binding_digest != candidate_digest
            or candidate_request != decision.request_digest
            or candidate_source["commit"]
            != decision.merged_pull_request["head"]
        ):
            raise PromotionError("direct candidate binding differs from merged request")
        return candidate_source, candidate_source

    reuse_record, layers = _validated_fetched_record(
        candidate_reuse,
        artifact_type=CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
        required_roles=("immutable-record-bytes",),
        field="candidate reuse",
    )
    try:
        validate_candidate_reuse_record(reuse_record)
    except ContractError as error:
        raise PromotionError(f"candidate reuse record is invalid: {error}") from error
    if layers["immutable-record-bytes"].body != candidate_reuse.config.body:
        raise PromotionError("candidate reuse immutable bytes differ from its record")
    reuse = reuse_record["candidate_reuse"]
    formula = payload["formula"]
    request_source = _source(reuse_record["common"]["source"], "reuse source")
    if (
        candidate_reuse.repository != candidate.repository
        or candidate_reuse.digest.removeprefix("sha256:")
        != decision.candidate_binding_digest
        or reuse_record["common"]["request_sha256"] != decision.request_digest
        or request_source["commit"] != decision.merged_pull_request["head"]
        or reuse["existing_candidate"]
        != {
            "record_sha256": candidate_digest,
            "immutable_reference": candidate.immutable_reference,
        }
        or reuse["bottle_layer"] != payload["bottle_layer"]
        or reuse["source_custody"]
        != {
            "record_sha256": custody[0]["sha256"],
            "immutable_reference": custody[0]["immutable_reference"],
        }
        or reuse["formula"]
        != {
            "tap": formula["tap"],
            "formula": formula["formula"],
            "architecture": formula["architecture"],
            "target_abi": formula["target_abi"],
            "bottle_contract_sha256": formula["bottle_contract_sha256"],
        }
        or reuse["original_producer"] != payload["producer"]
        or decision.override_receipts
    ):
        raise PromotionError("candidate reuse binding differs from promotion decision")
    return request_source, candidate_source


def prepare_admission(
    decision: PromotionDecisionV1,
    *,
    candidate: FetchedOciRecordV1,
    candidate_reuse: FetchedOciRecordV1 | None = None,
    canonical_publication: CanonicalBottlePublicationV1,
    preactivation_tap_source: Mapping[str, Any],
    abi_history_record_sha256: str,
    policy: PromotionPolicyV1,
) -> PreparedAdmissionV1:
    validate_promotion_decision(asdict(decision))
    if decision.eligibility != "eligible":
        raise PromotionError("admission cannot be prepared for an ineligible candidate")
    if not isinstance(canonical_publication, CanonicalBottlePublicationV1):
        raise PromotionError("canonical publication handoff is missing")
    request_source, candidate_source = validate_promotion_candidate_binding(
        decision,
        candidate=candidate,
        candidate_reuse=candidate_reuse,
    )
    expected_plan = build_canonical_bottle_plan(
        decision,
        candidate=candidate,
        policy=policy,
    )
    expected = _canonical_publication_identity(expected_plan)
    record, layer = _candidate_layer(candidate)
    metadata_record, bottle_metadata, _metadata_layer = _candidate_bottle_metadata(candidate)
    contract_record, bottle_contract = _candidate_bottle_contract(candidate)
    if (
        canonical_bytes(metadata_record) != canonical_bytes(record)
        or canonical_bytes(contract_record) != canonical_bytes(record)
    ):
        raise PromotionError("candidate bottle inputs changed their record identity")
    candidate_formula = record["candidate"]["formula"]
    pkg_version = (
        candidate_formula["version"]
        if candidate_formula["revision"] == 0
        else f"{candidate_formula['version']}_{candidate_formula['revision']}"
    )
    try:
        bottle_inventory = inspect_bottle_link_inventory(
            layer.body,
            formula=candidate_formula["formula"],
            version=pkg_version,
        )
    except BottleLinkError as error:
        raise PromotionError(f"candidate bottle link inventory is invalid: {error}") from error
    canonical = _artifact(canonical_publication.artifact, "canonical artifact")
    if (
        candidate.digest.removeprefix("sha256:") != decision.candidate_record_digest
        or layer.digest.removeprefix("sha256:") != decision.bottle_layer_sha256
        or layer.size != decision.bottle_layer_bytes
        or canonical != expected["artifact"]
        or canonical_publication.locator.repository != expected["repository"]
        or canonical_publication.locator.digest != expected["digest"]
        or canonical_publication.locator.immutable_reference
        != expected["immutable_reference"]
        or canonical_publication.locator.anonymous_readback_sha256
        != expected["anonymous_readback_sha256"]
    ):
        raise PromotionError("admission preparation differs from canonical readback")
    return PreparedAdmissionV1(
        decision=decision,
        request_source=MappingProxyType(request_source),
        candidate_source=MappingProxyType(candidate_source),
        preactivation_tap_source=MappingProxyType(
            _source(preactivation_tap_source, "admission preactivation tap source")
        ),
        abi_history_record_sha256=_digest(
            abi_history_record_sha256, "admission ABI history record"
        ),
        canonical=MappingProxyType(canonical),
        canonical_readback_evidence_sha256=(
            canonical_publication.locator.anonymous_readback_sha256
        ),
        promoted_layer=MappingProxyType(
            _artifact(record["candidate"]["bottle_layer"], "promoted layer")
        ),
        original_producer=MappingProxyType(
            _plain(record["candidate"]["producer"])
        ),
        candidate_formula=MappingProxyType(
            _plain(candidate_formula)
        ),
        candidate_bottle_metadata=MappingProxyType(_plain(bottle_metadata)),
        candidate_bottle_contract=MappingProxyType(_plain(bottle_contract)),
        candidate_bottle_inventory=MappingProxyType(_plain(bottle_inventory)),
    )


def prepare_formula_metadata_patch(
    *,
    tap_root: Any,
    prepared: PreparedAdmissionV1,
    history: FetchedOciRecordV1 | None,
    history_protection_snapshot: Mapping[str, Any],
    current_tap_source: Mapping[str, Any],
    expected_generated_metadata_sha256: str,
    guest_layout_bytes: bytes,
    policy: PromotionPolicyV1,
) -> PreparedFormulaMetadataUpdateV1:
    """Plan one path-bounded Formula selection from authenticated promotion facts."""

    if not isinstance(prepared, PreparedAdmissionV1):
        raise PromotionError("prepared Formula admission facts are missing")
    if not isinstance(policy, PromotionPolicyV1):
        raise PromotionError("Formula metadata policy is not protected schema 1")
    validate_promotion_decision(asdict(prepared.decision))
    if prepared.decision.eligibility != "eligible":
        raise PromotionError("ineligible promotion cannot update Formula metadata")
    current = _source(current_tap_source, "current Formula metadata source")
    if current["repository"].lower() != policy.tap_repository.lower():
        raise PromotionError("Formula metadata source names another tap")
    planned_tap = _source(
        prepared.preactivation_tap_source,
        "prepared Formula preactivation tap source",
    )
    _history(
        history,
        protection_snapshot=history_protection_snapshot,
        target_abi=_nonnegative(
            prepared.candidate_formula.get("target_abi"), "metadata target ABI"
        ),
        tap_source=planned_tap,
        policy=policy,
    )
    assert history is not None
    history_digest = history.digest.removeprefix("sha256:")
    try:
        state = load_abi_state(tap_root / "Kandelo/abi-state.json")
    except (OSError, TapMetadataError) as error:
        raise PromotionError(f"current Formula ABI state is invalid: {error}") from error
    formula_value = _exact(
        prepared.candidate_formula,
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
        "prepared candidate Formula",
    )
    name = _stable_id(formula_value["formula"], "metadata Formula")
    architecture = formula_value["architecture"]
    if architecture not in {"wasm32", "wasm64"}:
        raise PromotionError("metadata architecture is unsupported")
    target_abi = _nonnegative(formula_value["target_abi"], "metadata target ABI")
    if (
        state.current_abi != target_abi
        or formula_value["tap"].lower() != policy.tap_repository.lower()
        or prepared.decision.formula_subject
        != exact_formula_subject(name, architecture)
    ):
        raise PromotionError("Formula metadata subject differs from current ABI authority")
    version = _text(formula_value["version"], "metadata Formula version", 256)
    revision = _nonnegative(formula_value["revision"], "metadata Formula revision")
    rebuild = _nonnegative(
        formula_value["bottle_rebuild"], "metadata Formula rebuild"
    )
    contract_digest = _digest(
        formula_value["bottle_contract_sha256"], "metadata bottle contract"
    )
    try:
        contract = load_bottle_contract(
            canonical_bytes(_plain(prepared.candidate_bottle_contract))
        )
    except (CanonicalJsonError, ContractError) as error:
        raise PromotionError(f"prepared bottle contract is invalid: {error}") from error
    if (
        hashlib.sha256(canonical_bytes(contract)).hexdigest() != contract_digest
        or contract["target"]["abi"] != target_abi
        or contract["target"]["architecture"] != architecture
        or contract["formula"]["name"] != name
        or contract["formula"]["version"] != version
        or contract["formula"]["revision"] != revision
        or contract["formula"]["rebuild"] != rebuild
    ):
        raise PromotionError("prepared bottle contract differs from the candidate Formula")
    try:
        require_current_abi_authority(
            state,
            target_abi=target_abi,
            target_snapshot_sha256=contract["target"]["snapshot_sha256"],
            abi_history_record_digest=history_digest,
        )
    except TapMetadataError as error:
        raise PromotionError(
            "Formula metadata update differs from current ABI authority"
        ) from error
    formula_components = [
        component
        for component in contract["formula"]["source_components"]
        if component["id"] == "formula"
    ]
    if len(formula_components) != 1:
        raise PromotionError("prepared bottle contract has no exact Formula source identity")
    normalized = _digest(
        formula_components[0]["sha256"], "authenticated normalized Formula"
    )
    guest_layout_inputs = [
        component
        for component in contract["kandelo_inputs"]
        if component["path"] == "homebrew/kandelo-guest-layout.json"
    ]
    if (
        len(guest_layout_inputs) != 1
        or guest_layout_inputs[0]["kind"] != "file"
        or not isinstance(guest_layout_bytes, bytes)
        or captured_file_sha256(guest_layout_bytes, executable=False)
        != guest_layout_inputs[0]["sha256"]
    ):
        raise PromotionError("guest layout differs from the captured build input")
    try:
        guest_layout = load_guest_layout(guest_layout_bytes)
    except BottleLinkError as error:
        raise PromotionError(f"captured guest layout is invalid: {error}") from error
    generated = _digest(
        expected_generated_metadata_sha256, "expected generated Formula metadata"
    )

    canonical = _artifact(prepared.canonical, "prepared canonical bottle")
    layer = _artifact(prepared.promoted_layer, "prepared promoted layer")
    if (
        layer["sha256"] != prepared.decision.bottle_layer_sha256
        or layer["bytes"] != prepared.decision.bottle_layer_bytes
    ):
        raise PromotionError("Formula metadata layer differs from the promotion decision")
    canonical_repository_name = canonical_repository(policy, target_abi, name)
    canonical_repository_reference = "ghcr.io/" + canonical_repository_name
    if canonical["immutable_reference"] != (
        canonical_repository_reference + "@sha256:" + canonical["sha256"]
    ):
        raise PromotionError("Formula metadata canonical reference escaped its ABI namespace")
    candidate_repository_name = canonical_repository(
        policy, target_abi, name, candidate=True
    )
    if layer["immutable_reference"] != (
        "ghcr.io/"
        + candidate_repository_name
        + "@sha256:"
        + layer["sha256"]
    ):
        raise PromotionError("Formula metadata layer escaped its candidate namespace")

    try:
        normalized_metadata, _, _ = normalize_candidate_bottle_metadata(
            {
                "formula": name,
                "tap_repository": policy.tap_repository,
                "target_abi": target_abi,
                "architecture": architecture,
                "bottle_layer": layer,
            },
            prepared.candidate_bottle_metadata,
        )
    except ExecutionError as error:
        raise PromotionError(
            f"candidate Homebrew bottle metadata is invalid: {error}"
        ) from error
    metadata_key = bottle_metadata_formula_key(policy.tap_repository, name)
    metadata = _exact(
        normalized_metadata,
        frozenset({metadata_key}),
        "candidate Homebrew bottle metadata",
    )
    entry = _exact(
        metadata[metadata_key],
        frozenset({"formula", "bottle"}),
        "candidate Homebrew bottle entry",
    )
    formula_metadata = _exact(
        entry["formula"],
        frozenset({"name", "path", "pkg_version"}),
        "candidate Homebrew Formula metadata",
    )
    bottle_metadata = _exact(
        entry["bottle"],
        frozenset({"root_url", "cellar", "rebuild", "tags"}),
        "candidate Homebrew bottle projection",
    )
    expected_formula_path = (
        "Library/Taps/"
        + policy.tap_repository.split("/", 1)[0]
        + "/"
        + policy.tap_repository.split("/", 1)[1]
        + f"/Formula/{name}.rb"
    )
    expected_pkg_version = version if revision == 0 else f"{version}_{revision}"
    tags = _exact(
        bottle_metadata["tags"],
        frozenset({f"{architecture}_kandelo"}),
        "candidate Homebrew bottle tags",
    )
    tag = _exact(
        tags[f"{architecture}_kandelo"],
        frozenset({"sha256"}),
        "candidate Homebrew bottle tag",
    )
    candidate_root = "https://ghcr.io/v2/" + candidate_repository_name
    if (
        formula_metadata["name"] != name
        or formula_metadata["path"] != expected_formula_path
        or formula_metadata["pkg_version"] != expected_pkg_version
        or bottle_metadata["root_url"] != candidate_root
        or _nonnegative(bottle_metadata["rebuild"], "candidate bottle rebuild")
        != rebuild
        or _digest(tag["sha256"], "candidate bottle layer") != layer["sha256"]
    ):
        raise PromotionError("candidate Homebrew metadata differs from exact Formula/layer")
    candidate_cellar = _text(
        bottle_metadata["cellar"], "candidate bottle cellar", 4096
    )
    if candidate_cellar not in {
        "any",
        "any_skip_relocation",
        guest_layout["cellar"],
    }:
        raise PromotionError("candidate bottle cellar differs from the captured guest layout")
    cellar = str(guest_layout["cellar"])
    try:
        inventory = validate_bottle_link_inventory(
            prepared.candidate_bottle_inventory,
            formula=name,
            version=expected_pkg_version,
        )
        link_manifest = build_link_manifest(
            inventory=inventory,
            guest_layout=guest_layout,
            formula=name,
            version=expected_pkg_version,
            architecture=architecture,
            target_abi=target_abi,
            bottle_url=(
                "https://ghcr.io/v2/"
                + canonical_repository_name
                + "/blobs/sha256:"
                + layer["sha256"]
            ),
            bottle_sha256=layer["sha256"],
            bottle_bytes=layer["bytes"],
        )
    except BottleLinkError as error:
        raise PromotionError(f"candidate link manifest cannot be derived: {error}") from error
    link_manifest_path = (
        f"Kandelo/link/{name}-{expected_pkg_version}-"
        f"rebuild{rebuild}-{architecture}.json"
    )
    producer = _exact(
        prepared.original_producer,
        frozenset({"request_sha256", "head", "run_id"}),
        "original candidate producer",
    )
    if (
        _digest(producer["request_sha256"], "candidate producer request")
        == prepared.decision.request_digest
    ) != (
        prepared.decision.candidate_binding_digest
        == prepared.decision.candidate_record_digest
    ):
        raise PromotionError("candidate producer/reuse relationship is contradictory")
    if (
        _git_sha(producer["head"], "candidate producer head")
        != prepared.candidate_source["commit"]
        or prepared.request_source["commit"]
        != prepared.decision.merged_pull_request["head"]
    ):
        raise PromotionError("candidate producer differs from prepared source authority")
    if (
        prepared.decision.candidate_binding_digest
        == prepared.decision.candidate_record_digest
        and prepared.candidate_source != prepared.request_source
    ):
        raise PromotionError("direct candidate source differs from current request")
    run_id = _positive(producer["run_id"], "candidate producer run")
    candidate_source = _source(
        prepared.candidate_source, "prepared candidate source"
    )
    update = FormulaMetadataUpdateV1(
        formula=name,
        architecture=architecture,
        expected_main_commit=current["commit"],
        expected_normalized_formula_sha256=normalized,
        expected_generated_metadata_sha256=generated,
        allowed_paths=(
            f"Formula/{name}.rb",
            f"Kandelo/formula/{name}.json",
            "Kandelo/metadata.json",
            link_manifest_path,
        ),
        link_manifest_path=link_manifest_path,
        link_manifest_sha256=hashlib.sha256(
            link_manifest_bytes(link_manifest)
        ).hexdigest(),
        canonical_manifest_digest=canonical["sha256"],
        bottle_layer_sha256=layer["sha256"],
        bottle_layer_bytes=layer["bytes"],
        target_abi=target_abi,
    )
    promoted = PromotedBottleMetadataV1(
        formula=name,
        architecture=architecture,
        version=version,
        revision=revision,
        rebuild=rebuild,
        canonical_root_url=(
            "https://ghcr.io/v2/" + canonical_repository_name
        ),
        cellar=cellar,
        built_by=(
            "https://github.com/"
            + policy.tap_repository
            + "/actions/runs/"
            + str(run_id)
        ),
        built_from=MappingProxyType(
            {
                "formula_sha256": normalized,
                "kandelo_commit": candidate_source["commit"],
                "kandelo_repository": candidate_source["repository"],
                "tap_commit": planned_tap["commit"],
                "tap_repository": planned_tap["repository"],
            }
        ),
        link_manifest=MappingProxyType(_plain(link_manifest)),
    )
    try:
        patch = plan_formula_metadata_patch(
            tap_root,
            current_tap_source=current,
            update=update,
            promoted=promoted,
        )
    except TapMetadataError as error:
        raise PromotionError(f"Formula metadata patch is invalid: {error}") from error
    return PreparedFormulaMetadataUpdateV1(update=update, patch=patch)


def finalize_admission_record(
    prepared: PreparedAdmissionV1,
    *,
    formula_metadata_base_source: Mapping[str, Any] | None,
    formula_metadata_source: Mapping[str, Any] | None,
    formula_metadata_update: Mapping[str, Any] | None,
    post_write_readback: Mapping[str, Any] | None,
    run: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(prepared, PreparedAdmissionV1):
        raise PromotionError("prepared admission facts are missing")
    if (
        formula_metadata_base_source is None
        or formula_metadata_source is None
        or formula_metadata_update is None
        or post_write_readback is None
    ):
        raise PromotionError("admission requires exact metadata commit and post-write readback")
    metadata_base_source = _source(
        formula_metadata_base_source, "Formula metadata base source"
    )
    metadata_source = _source(formula_metadata_source, "Formula metadata source")
    readback = _exact(
        post_write_readback,
        frozenset({"source", "formula_metadata_update"}),
        "Formula metadata post-write readback",
    )
    if (
        _plain(readback["source"]) != metadata_source
        or canonical_bytes(readback["formula_metadata_update"])
        != canonical_bytes(formula_metadata_update)
    ):
        raise PromotionError("Formula metadata post-write readback differs from committed facts")
    try:
        return build_admission_record(
            request_sha256=prepared.decision.request_digest,
            request_source=prepared.request_source,
            run=run,
            candidate_record_sha256=prepared.decision.candidate_record_digest,
            candidate_binding_sha256=prepared.decision.candidate_binding_digest,
            promoted_layer=prepared.promoted_layer,
            qualifying_receipt_sha256s=sorted(
                set(prepared.decision.qualifying_receipts)
                | set(prepared.decision.override_receipts)
            ),
            merged_pull_request=prepared.decision.merged_pull_request,
            abi_history_record_sha256=prepared.abi_history_record_sha256,
            preactivation_tap_source=prepared.preactivation_tap_source,
            tap_source=metadata_base_source,
            canonical=prepared.canonical,
            canonical_public_readback_sha256=str(prepared.canonical["sha256"]),
            formula_metadata_source=metadata_source,
            formula_metadata_update=formula_metadata_update,
            original_producer=prepared.original_producer,
        )
    except TapRecordError as error:
        raise PromotionError(f"admission record is invalid: {error}") from error
