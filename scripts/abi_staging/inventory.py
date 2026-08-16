"""Anonymous reconstruction of durable scheduling facts from immutable OCI records."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from .contract import load_canonical_mapping, validate_candidate_reuse_record
from .custody import load_source_custody_manifest
from .oci import (
    REUSE_TAG_PREFIX,
    OciPublicationError,
    OciTransportV1,
    fetch_public_record,
    list_public_record_locators,
)
from .plan import exact_formula_subject, validate_tap_plan
from .policy import (
    TapStagingPolicyV1,
    VerificationTestDefinitionV1,
    attempt_repository,
    candidate_repository,
    candidate_reuse_repository,
)
from .records import (
    ATTEMPT_OUTCOME_MEDIA_TYPE,
    CANDIDATE_RECORD_MEDIA_TYPE,
    CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
    SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
    validate_attempt_outcome_record,
    validate_candidate_record,
)
from .scheduler import (
    AttemptFactV1,
    CandidateFactV1,
    SchedulingRecordsV1,
    VerificationFactV1,
)
from .verification import (
    VERIFICATION_RECEIPT_MEDIA_TYPE,
    receipt_repository,
    receipt_tag_prefix,
    validate_verification_receipt_record,
)


class InventoryError(ValueError):
    """Raised when public immutable records cannot be reconstructed safely."""


MAX_INVENTORY_WORKERS = 8


def _parallel_formula_inventory(
    names: tuple[str, ...], operation: Callable[[str], Any]
) -> dict[str, Any]:
    """Run one independent immutable Formula read per bounded worker."""

    if not names:
        return {}
    futures: dict[Future[Any], str] = {}
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(
        max_workers=min(MAX_INVENTORY_WORKERS, len(names)),
        thread_name_prefix="abi-staging-inventory",
    ) as executor:
        for name in names:
            futures[executor.submit(operation, name)] = name
        try:
            for future in as_completed(futures):
                name = futures[future]
                results[name] = future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return {name: results[name] for name in names}


@dataclass(frozen=True)
class CandidateInventoryV1:
    facts: tuple[CandidateFactV1, ...]
    locators: Mapping[str, Mapping[str, str]]
    records: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class AttemptInventoryV1:
    facts: tuple[AttemptFactV1, ...]
    locators: Mapping[str, Mapping[str, str]]
    records: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class VerificationInventoryV1:
    facts: tuple[VerificationFactV1, ...]
    locators: Mapping[str, Mapping[str, str]]
    records: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class CandidateReuseInventoryV1:
    facts: tuple[CandidateFactV1, ...]
    locators: Mapping[str, Mapping[str, str]]
    records: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class PublicSchedulingInventoryV1:
    records: SchedulingRecordsV1
    candidate_locators: Mapping[str, Mapping[str, str]]
    candidate_records: Mapping[str, Mapping[str, Any]]
    attempt_locators: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    attempt_records: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    source_custody_records: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    verification_locators: Mapping[str, Mapping[str, str]] = field(
        default_factory=dict
    )
    verification_records: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    reuse_locators: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    reuse_records: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def inspect_source_custody_records(
    candidate_records: Mapping[str, Mapping[str, Any]],
    *,
    transport: OciTransportV1,
) -> dict[str, Mapping[str, Any]]:
    """Load exact custody configs named by candidates without source payloads."""

    records: dict[str, Mapping[str, Any]] = {}
    try:
        for candidate_digest in sorted(candidate_records):
            candidate = candidate_records[candidate_digest]
            validate_candidate_record(candidate)
            components = [
                item["artifact"]
                for item in candidate["candidate"]["normalized_components"]
                if item["id"] == "source-custody"
            ]
            if len(components) != 1:
                raise InventoryError(
                    "candidate inventory lacks one source-custody link"
                )
            component = components[0]
            reference = component["immutable_reference"]
            if (
                not isinstance(reference, str)
                or not reference.startswith("ghcr.io/")
                or "@sha256:" not in reference
            ):
                raise InventoryError(
                    "candidate source-custody reference is not immutable"
                )
            repository, digest = reference.rsplit("@", 1)
            if digest != "sha256:" + component["sha256"]:
                raise InventoryError(
                    "candidate source-custody reference differs from its digest"
                )
            locator = {
                "repository": repository,
                "digest": digest,
                "immutable_reference": reference,
            }
            fetched = fetch_public_record(
                locator,
                transport=transport,
                expected_artifact_type=SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
                required_layer_roles=(),
            )
            if (
                fetched.digest != digest
                or len(fetched.manifest) != component["bytes"]
                or fetched.config.role != "source-custody-manifest"
                or fetched.config.media_type
                != SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE
                or fetched.config.title != "source-custody-manifest.json"
            ):
                raise InventoryError(
                    "candidate source-custody OCI identity differs from its link"
                )
            custody = load_source_custody_manifest(fetched.config.body)
            manifest = load_canonical_mapping(
                fetched.manifest, "source-custody OCI manifest"
            )
            annotations = manifest.get("annotations")
            expected_annotations = {
                "dev.kandelo.abi-staging.capsule-sha256": custody[
                    "capsule_sha256"
                ],
                "dev.kandelo.abi-staging.classification": (
                    "factual-source-custody"
                ),
                "dev.kandelo.abi-staging.kind": "source-custody",
                "org.opencontainers.image.source": (
                    "https://github.com/" + custody["sources"][1]["repository"]
                ),
            }
            if not isinstance(annotations, Mapping) or dict(annotations) != (
                expected_annotations
            ):
                raise InventoryError(
                    "source-custody OCI annotations differ from protected facts"
                )
            existing = records.get(component["sha256"])
            if existing is not None and existing != custody:
                raise InventoryError(
                    "source-custody digest maps to conflicting records"
                )
            records[component["sha256"]] = custody
    except (OciPublicationError, ValueError) as error:
        if isinstance(error, InventoryError):
            raise
        raise InventoryError(f"source-custody inventory is invalid: {error}") from error
    return {key: records[key] for key in sorted(records)}


def inspect_verification_repository(
    repository: str,
    *,
    test_id: str,
    host: str,
    candidates: Mapping[str, Mapping[str, Any]],
    transport: OciTransportV1,
) -> VerificationInventoryV1:
    """Read factual receipts and bind their candidate subjects externally."""

    facts: list[VerificationFactV1] = []
    locators: dict[str, dict[str, str]] = {}
    records: dict[str, dict[str, Any]] = {}
    try:
        for locator in list_public_record_locators(
            repository,
            transport=transport,
            tag_prefix=receipt_tag_prefix(test_id, host),
            allow_verification_tags=True,
            allow_reuse_tags=True,
        ):
            fetched = fetch_public_record(
                locator,
                transport=transport,
                expected_artifact_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
                required_layer_roles=(),
            )
            record = load_canonical_mapping(
                fetched.config.body, "verification receipt"
            )
            validate_verification_receipt_record(record)
            manifest = load_canonical_mapping(
                fetched.manifest, "verification receipt OCI manifest"
            )
            annotations = manifest.get("annotations")
            if not isinstance(annotations, Mapping) or frozenset(annotations) != frozenset(
                {
                    "dev.kandelo.abi-staging.candidate-record-sha256",
                    "dev.kandelo.abi-staging.classification",
                    "dev.kandelo.abi-staging.completed-at",
                    "dev.kandelo.abi-staging.host",
                    "dev.kandelo.abi-staging.kind",
                    "dev.kandelo.abi-staging.outcome",
                    "dev.kandelo.abi-staging.test-definition-sha256",
                    "org.opencontainers.image.source",
                }
            ):
                raise InventoryError("verification receipt annotations changed")
            verification = record["verification"]
            common = record["common"]
            candidate_record = verification["candidate_record_sha256"]
            candidate = candidates.get(candidate_record)
            if candidate is None:
                raise InventoryError(
                    "verification receipt names an unknown candidate record"
                )
            if (
                annotations["dev.kandelo.abi-staging.candidate-record-sha256"]
                != candidate_record
                or verification["candidate_layer"]
                != candidate.get("bottle_layer")
                or annotations["dev.kandelo.abi-staging.classification"]
                != "factual-verification-receipt"
                or annotations["dev.kandelo.abi-staging.kind"]
                != "verification-receipt"
                or annotations["dev.kandelo.abi-staging.host"]
                != verification["host"]
                or annotations["dev.kandelo.abi-staging.outcome"]
                != common["outcome"]
                or annotations["dev.kandelo.abi-staging.test-definition-sha256"]
                != verification["test_definition_sha256"]
            ):
                raise InventoryError(
                    "verification receipt annotations contradict protected facts"
                )
            subject = candidate.get("subject")
            if not isinstance(subject, str):
                raise InventoryError("candidate inventory lacks an exact subject")
            record_sha256 = fetched.digest.removeprefix("sha256:")
            if record_sha256 in locators:
                raise InventoryError("verification inventory repeats a record digest")
            facts.append(
                VerificationFactV1(
                    request_sha256=common["request_sha256"],
                    subject=subject,
                    candidate_record_sha256=candidate_record,
                    test_definition_sha256=verification[
                        "test_definition_sha256"
                    ],
                    host=verification["host"],
                    outcome=common["outcome"],
                    guard_code=(
                        None
                        if not common["guard_codes"]
                        else common["guard_codes"][0]
                    ),
                    attempt_ordinal=verification["attempt_ordinal"],
                    completed_at=annotations[
                        "dev.kandelo.abi-staging.completed-at"
                    ],
                    record_sha256=record_sha256,
                )
            )
            records[record_sha256] = dict(record)
            locators[record_sha256] = {
                "repository": fetched.repository,
                "digest": fetched.digest,
                "immutable_reference": fetched.immutable_reference,
            }
    except (OciPublicationError, ValueError) as error:
        if isinstance(error, InventoryError):
            raise
        raise InventoryError(f"verification inventory is invalid: {error}") from error
    facts.sort(key=lambda item: item.record_sha256)
    return VerificationInventoryV1(
        tuple(facts),
        {key: locators[key] for key in sorted(locators)},
        {key: records[key] for key in sorted(records)},
    )


def scan_verification_repository(
    repository: str,
    *,
    test_id: str,
    host: str,
    candidates: Mapping[str, Mapping[str, Any]],
    transport: OciTransportV1,
) -> tuple[VerificationFactV1, ...]:
    return inspect_verification_repository(
        repository,
        test_id=test_id,
        host=host,
        candidates=candidates,
        transport=transport,
    ).facts


def inspect_attempt_repository(
    repository: str, *, transport: OciTransportV1
) -> AttemptInventoryV1:
    facts: list[AttemptFactV1] = []
    locators: dict[str, dict[str, str]] = {}
    records: dict[str, dict[str, Any]] = {}
    try:
        for locator in list_public_record_locators(repository, transport=transport):
            fetched = fetch_public_record(
                locator,
                transport=transport,
                expected_artifact_type=ATTEMPT_OUTCOME_MEDIA_TYPE,
                required_layer_roles=("immutable-record-bytes",),
            )
            record = load_canonical_mapping(fetched.config.body, "attempt outcome")
            validate_attempt_outcome_record(record)
            attempt = record["attempt"]
            record_sha256 = fetched.digest.removeprefix("sha256:")
            if record_sha256 in records:
                raise InventoryError("attempt inventory repeats a record digest")
            facts.append(
                AttemptFactV1(
                    request_sha256=attempt["request_sha256"],
                    subject=attempt["subject"],
                    contract_sha256=attempt["contract_sha256"],
                    retry_ordinal=attempt["retry_ordinal"],
                    outcome=attempt["outcome"],
                    guard_code=attempt["guard_code"],
                    completed_at=attempt["completed_at"],
                    record_sha256=record_sha256,
                )
            )
            records[record_sha256] = dict(record)
            locators[record_sha256] = {
                "repository": fetched.repository,
                "digest": fetched.digest,
                "immutable_reference": fetched.immutable_reference,
            }
    except (OciPublicationError, ValueError) as error:
        if isinstance(error, InventoryError):
            raise
        raise InventoryError(f"attempt inventory is invalid: {error}") from error
    facts.sort(key=lambda item: item.record_sha256)
    return AttemptInventoryV1(
        tuple(facts),
        {key: locators[key] for key in sorted(locators)},
        {key: records[key] for key in sorted(records)},
    )


def scan_attempt_repository(
    repository: str, *, transport: OciTransportV1
) -> tuple[AttemptFactV1, ...]:
    return inspect_attempt_repository(repository, transport=transport).facts


def scan_candidate_repository(
    repository: str, *, transport: OciTransportV1
) -> tuple[tuple[CandidateFactV1, ...], dict[str, dict[str, str]]]:
    """Read every candidate record in one dedicated Formula repository."""

    inventory = inspect_candidate_repository(repository, transport=transport)
    return inventory.facts, {
        key: dict(inventory.locators[key]) for key in sorted(inventory.locators)
    }


def inspect_candidate_repository(
    repository: str, *, transport: OciTransportV1
) -> CandidateInventoryV1:
    """Read candidate facts plus exact public records without bottle payloads."""

    facts: list[CandidateFactV1] = []
    locators: dict[str, dict[str, str]] = {}
    records: dict[str, dict[str, Any]] = {}
    try:
        inventory = list_public_record_locators(
            repository,
            transport=transport,
            allow_verification_tags=True,
            allow_reuse_tags=True,
        )
        for locator in inventory:
            fetched = fetch_public_record(
                locator,
                transport=transport,
                expected_artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
                required_layer_roles=(),
            )
            record = load_canonical_mapping(fetched.config.body, "candidate record")
            validate_candidate_record(record)
            payload = record["candidate"]
            formula = payload["formula"]
            layer = payload["bottle_layer"]
            record_sha256 = fetched.digest.removeprefix("sha256:")
            fact = CandidateFactV1(
                request_sha256=record["common"]["request_sha256"],
                subject=exact_formula_subject(
                    formula["formula"], formula["architecture"]
                ),
                contract_sha256=formula["bottle_contract_sha256"],
                record_sha256=record_sha256,
                bottle_layer_sha256=layer["sha256"],
                descriptor_capable=any(
                    component["id"] == "vfs-composition-descriptor"
                    for component in payload["normalized_components"]
                ),
            )
            if record_sha256 in locators:
                raise InventoryError("candidate inventory repeats a record digest")
            facts.append(fact)
            records[record_sha256] = dict(record)
            locators[record_sha256] = {
                "repository": fetched.repository,
                "digest": fetched.digest,
                "immutable_reference": fetched.immutable_reference,
            }
    except (OciPublicationError, ValueError) as error:
        if isinstance(error, InventoryError):
            raise
        raise InventoryError(f"candidate inventory is invalid: {error}") from error
    facts.sort(key=lambda item: item.record_sha256)
    return CandidateInventoryV1(
        tuple(facts),
        {key: locators[key] for key in sorted(locators)},
        {key: records[key] for key in sorted(records)},
    )


def inspect_candidate_reuse_repository(
    repository: str,
    *,
    candidates: Mapping[str, Mapping[str, Any]],
    candidate_locators: Mapping[str, Mapping[str, str]],
    verifications: Mapping[str, VerificationFactV1],
    verification_locators: Mapping[str, Mapping[str, str]],
    transport: OciTransportV1,
) -> CandidateReuseInventoryV1:
    """Validate explicit request bindings to unchanged factual candidates."""

    facts: list[CandidateFactV1] = []
    locators: dict[str, dict[str, str]] = {}
    records: dict[str, dict[str, Any]] = {}
    try:
        for locator in list_public_record_locators(
            repository,
            transport=transport,
            tag_prefix=REUSE_TAG_PREFIX,
            allow_verification_tags=True,
            allow_reuse_tags=True,
        ):
            fetched = fetch_public_record(
                locator,
                transport=transport,
                expected_artifact_type=CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
                required_layer_roles=("immutable-record-bytes",),
            )
            record = load_canonical_mapping(
                fetched.config.body, "candidate reuse record"
            )
            validate_candidate_reuse_record(record)
            if fetched.layers[0].body != fetched.config.body:
                raise InventoryError("candidate reuse immutable bytes differ")
            manifest = load_canonical_mapping(
                fetched.manifest, "candidate reuse OCI manifest"
            )
            annotations = manifest.get("annotations")
            expected_annotation_keys = frozenset(
                {
                    "dev.kandelo.abi-staging.bottle-contract-sha256",
                    "dev.kandelo.abi-staging.classification",
                    "dev.kandelo.abi-staging.kind",
                    "dev.kandelo.abi-staging.request-sha256",
                    "org.opencontainers.image.source",
                }
            )
            if (
                not isinstance(annotations, Mapping)
                or frozenset(annotations) != expected_annotation_keys
            ):
                raise InventoryError("candidate reuse annotations changed")

            common = record["common"]
            reuse = record["candidate_reuse"]
            formula = reuse["formula"]
            existing = reuse["existing_candidate"]
            candidate_digest = existing["record_sha256"]
            candidate = candidates.get(candidate_digest)
            candidate_locator = candidate_locators.get(candidate_digest)
            if candidate is None or candidate_locator is None:
                raise InventoryError("candidate reuse names an unknown candidate")
            payload = candidate["candidate"]
            candidate_formula = payload["formula"]
            source_components = [
                item["artifact"]
                for item in payload["normalized_components"]
                if item["id"] == "source-custody"
            ]
            if len(source_components) != 1:
                raise InventoryError("reused candidate lacks one source-custody record")
            source = source_components[0]
            expected_source = {
                "record_sha256": source["sha256"],
                "immutable_reference": source["immutable_reference"],
            }
            expected_existing = {
                "record_sha256": candidate_digest,
                "immutable_reference": candidate_locator["immutable_reference"],
            }
            expected_formula = {
                "tap": candidate_formula["tap"],
                "formula": candidate_formula["formula"],
                "architecture": candidate_formula["architecture"],
                "target_abi": candidate_formula["target_abi"],
                "bottle_contract_sha256": candidate_formula[
                    "bottle_contract_sha256"
                ],
            }
            receipt_digests = [
                item["record_sha256"] for item in reuse["qualifying_receipts"]
            ]
            for link in reuse["qualifying_receipts"]:
                receipt = verifications.get(link["record_sha256"])
                receipt_locator = verification_locators.get(link["record_sha256"])
                if receipt is None and receipt_locator is None:
                    immutable_reference = link["immutable_reference"]
                    marker = "@sha256:"
                    if marker not in immutable_reference:
                        raise InventoryError(
                            "candidate reuse receipt reference is not immutable"
                        )
                    repository_name, digest = immutable_reference.rsplit(marker, 1)
                    if digest != link["record_sha256"]:
                        raise InventoryError(
                            "candidate reuse receipt reference differs from its digest"
                        )
                    fetched_receipt = fetch_public_record(
                        {
                            "repository": repository_name,
                            "digest": "sha256:" + digest,
                            "immutable_reference": immutable_reference,
                        },
                        transport=transport,
                        expected_artifact_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
                        required_layer_roles=(),
                    )
                    historical = load_canonical_mapping(
                        fetched_receipt.config.body,
                        "historical candidate reuse receipt",
                    )
                    validate_verification_receipt_record(historical)
                    historical_verification = historical["verification"]
                    if (
                        historical_verification["candidate_record_sha256"]
                        != candidate_digest
                        or historical_verification["candidate_layer"]
                        != payload["bottle_layer"]
                        or historical["common"]["outcome"] != "success"
                    ):
                        raise InventoryError(
                            "candidate reuse names a nonqualifying historical receipt"
                        )
                    continue
                if (
                    receipt is None
                    or receipt_locator is None
                    or receipt.candidate_record_sha256 != candidate_digest
                    or receipt.outcome != "success"
                    or link["immutable_reference"]
                    != receipt_locator["immutable_reference"]
                ):
                    raise InventoryError(
                        "candidate reuse names a nonqualifying verification receipt"
                    )
            if (
                reuse["formula"] != expected_formula
                or reuse["existing_candidate"] != expected_existing
                or reuse["bottle_layer"] != payload["bottle_layer"]
                or reuse["source_custody"] != expected_source
                or reuse["original_producer"] != payload["producer"]
                or annotations[
                    "dev.kandelo.abi-staging.bottle-contract-sha256"
                ]
                != candidate_formula["bottle_contract_sha256"]
                or annotations["dev.kandelo.abi-staging.classification"]
                != "public-candidate-reuse-not-endorsement"
                or annotations["dev.kandelo.abi-staging.kind"]
                != "candidate-reuse"
                or annotations["dev.kandelo.abi-staging.request-sha256"]
                != common["request_sha256"]
                or annotations["org.opencontainers.image.source"]
                != "https://github.com/" + candidate_formula["tap"]
                or len(receipt_digests) != len(set(receipt_digests))
            ):
                raise InventoryError("candidate reuse contradicts protected facts")
            record_sha256 = fetched.digest.removeprefix("sha256:")
            if record_sha256 in locators:
                raise InventoryError("candidate reuse inventory repeats a digest")
            facts.append(
                CandidateFactV1(
                    request_sha256=common["request_sha256"],
                    subject=exact_formula_subject(
                        formula["formula"], formula["architecture"]
                    ),
                    contract_sha256=formula["bottle_contract_sha256"],
                    record_sha256=candidate_digest,
                    bottle_layer_sha256=reuse["bottle_layer"]["sha256"],
                    descriptor_capable=any(
                        component["id"] == "vfs-composition-descriptor"
                        for component in payload["normalized_components"]
                    ),
                    binding_record_sha256=record_sha256,
                )
            )
            records[record_sha256] = dict(record)
            locators[record_sha256] = {
                "repository": fetched.repository,
                "digest": fetched.digest,
                "immutable_reference": fetched.immutable_reference,
            }
    except (OciPublicationError, ValueError) as error:
        if isinstance(error, InventoryError):
            raise
        raise InventoryError(f"candidate reuse inventory is invalid: {error}") from error
    facts.sort(
        key=lambda item: item.binding_record_sha256 or item.record_sha256
    )
    return CandidateReuseInventoryV1(
        tuple(facts),
        {key: locators[key] for key in sorted(locators)},
        {key: records[key] for key in sorted(records)},
    )


def scan_scheduling_inventory(
    tap_plan: Mapping[str, Any],
    *,
    policy: TapStagingPolicyV1,
    verification_tests: tuple[VerificationTestDefinitionV1, ...],
    transport: OciTransportV1,
    worker_transport_factory: Callable[[], OciTransportV1] | None = None,
) -> PublicSchedulingInventoryV1:
    """Reconstruct one request's scheduler inputs from dedicated public repos."""

    validate_tap_plan(tap_plan)
    target_abi = tap_plan["target_abi"]["version"]
    names = tuple(
        sorted({item["identity"]["name"] for item in tap_plan["formulae"]})
    )
    if not names or len(names) > policy.max_formulae:
        raise InventoryError("tap plan Formula inventory exceeds protected policy")
    attempts: list[AttemptFactV1] = []
    attempt_locators: dict[str, Mapping[str, str]] = {}
    attempt_records: dict[str, Mapping[str, Any]] = {}
    candidates: list[CandidateFactV1] = []
    verifications: list[VerificationFactV1] = []
    locators: dict[str, Mapping[str, str]] = {}
    candidate_records: dict[str, Mapping[str, Any]] = {}
    candidate_subjects: dict[str, dict[str, str]] = {}
    verification_locators: dict[str, Mapping[str, str]] = {}
    verification_records: dict[str, Mapping[str, Any]] = {}
    reuse_locators: dict[str, Mapping[str, str]] = {}
    reuse_records: dict[str, Mapping[str, Any]] = {}

    make_transport = (
        (lambda: transport)
        if worker_transport_factory is None
        else worker_transport_factory
    )

    def inspect_formula_records(
        name: str,
    ) -> tuple[CandidateInventoryV1, AttemptInventoryV1]:
        worker_transport = make_transport()
        repository = candidate_repository(policy, target_abi, formula=name)
        return (
            inspect_candidate_repository(repository, transport=worker_transport),
            inspect_attempt_repository(
                attempt_repository(policy, target_abi, formula=name),
                transport=worker_transport,
            ),
        )

    formula_records = _parallel_formula_inventory(names, inspect_formula_records)
    for name in names:
        inspected, attempt_inspected = formula_records[name]
        candidates.extend(inspected.facts)
        attempts.extend(attempt_inspected.facts)
        for digest in attempt_inspected.locators:
            if digest in attempt_locators or digest in attempt_records:
                raise InventoryError(
                    "attempt digest appears in multiple repositories"
                )
            attempt_locators[digest] = attempt_inspected.locators[digest]
            attempt_records[digest] = attempt_inspected.records[digest]
        for fact in inspected.facts:
            if fact.record_sha256 in locators or fact.record_sha256 in candidate_records:
                raise InventoryError("candidate digest appears in multiple repositories")
            locators[fact.record_sha256] = inspected.locators[fact.record_sha256]
            candidate_records[fact.record_sha256] = inspected.records[
                fact.record_sha256
            ]
            candidate_subjects[fact.record_sha256] = {
                "request_sha256": fact.request_sha256,
                "subject": fact.subject,
                "bottle_layer": inspected.records[fact.record_sha256][
                    "candidate"
                ]["bottle_layer"],
            }

    source_custody_records = inspect_source_custody_records(
        candidate_records,
        transport=transport,
    )

    def inspect_formula_verifications(
        name: str,
    ) -> tuple[VerificationInventoryV1, ...]:
        worker_transport = make_transport()
        base = candidate_repository(policy, target_abi, formula=name)
        inspected_for_formula: list[VerificationInventoryV1] = []
        for definition in verification_tests:
            for host in definition.hosts:
                inspected_for_formula.append(
                    inspect_verification_repository(
                        receipt_repository(base, definition.id, host),
                        test_id=definition.id,
                        host=host,
                        candidates=candidate_subjects,
                        transport=worker_transport,
                    )
                )
        return tuple(inspected_for_formula)

    formula_verifications = _parallel_formula_inventory(
        names, inspect_formula_verifications
    )
    for name in names:
        for inspected in formula_verifications[name]:
            verifications.extend(inspected.facts)
            for digest in inspected.locators:
                if digest in verification_locators:
                    raise InventoryError(
                        "verification digest appears in multiple repositories"
                    )
                verification_locators[digest] = inspected.locators[digest]
                verification_records[digest] = inspected.records[digest]

    verification_facts = {
        fact.record_sha256: fact for fact in verifications
    }
    if len(verification_facts) != len(verifications):
        raise InventoryError("verification inventory repeats a record digest")

    def inspect_formula_reuse(name: str) -> CandidateReuseInventoryV1:
        return inspect_candidate_reuse_repository(
            candidate_reuse_repository(policy, target_abi, formula=name),
            candidates=candidate_records,
            candidate_locators=locators,
            verifications=verification_facts,
            verification_locators=verification_locators,
            transport=make_transport(),
        )

    formula_reuse = _parallel_formula_inventory(names, inspect_formula_reuse)
    for name in names:
        inspected = formula_reuse[name]
        candidates.extend(inspected.facts)
        for digest in inspected.locators:
            if digest in reuse_locators:
                raise InventoryError(
                    "candidate reuse digest appears in multiple repositories"
                )
            reuse_locators[digest] = inspected.locators[digest]
            reuse_records[digest] = inspected.records[digest]
    return PublicSchedulingInventoryV1(
        records=SchedulingRecordsV1(
            attempts=tuple(attempts),
            candidates=tuple(candidates),
            verifications=tuple(verifications),
        ),
        candidate_locators={key: locators[key] for key in sorted(locators)},
        candidate_records={
            key: candidate_records[key] for key in sorted(candidate_records)
        },
        attempt_locators={
            key: attempt_locators[key] for key in sorted(attempt_locators)
        },
        attempt_records={
            key: attempt_records[key] for key in sorted(attempt_records)
        },
        source_custody_records=source_custody_records,
        verification_locators={
            key: verification_locators[key]
            for key in sorted(verification_locators)
        },
        verification_records={
            key: verification_records[key]
            for key in sorted(verification_records)
        },
        reuse_locators={
            key: reuse_locators[key] for key in sorted(reuse_locators)
        },
        reuse_records={
            key: reuse_records[key] for key in sorted(reuse_records)
        },
    )
