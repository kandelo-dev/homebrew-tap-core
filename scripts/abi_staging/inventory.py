"""Anonymous reconstruction of durable scheduling facts from immutable OCI records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .contract import load_canonical_mapping
from .oci import (
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
)
from .records import (
    ATTEMPT_OUTCOME_MEDIA_TYPE,
    CANDIDATE_RECORD_MEDIA_TYPE,
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
    validate_verification_receipt_record,
)


class InventoryError(ValueError):
    """Raised when public immutable records cannot be reconstructed safely."""


@dataclass(frozen=True)
class CandidateInventoryV1:
    facts: tuple[CandidateFactV1, ...]
    locators: Mapping[str, Mapping[str, str]]
    records: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class PublicSchedulingInventoryV1:
    records: SchedulingRecordsV1
    candidate_locators: Mapping[str, Mapping[str, str]]
    candidate_records: Mapping[str, Mapping[str, Any]]


def scan_verification_repository(
    repository: str,
    *,
    candidates: Mapping[str, Mapping[str, Any]],
    transport: OciTransportV1,
) -> tuple[VerificationFactV1, ...]:
    """Read factual receipts and bind their candidate subjects externally."""

    facts: list[VerificationFactV1] = []
    try:
        for locator in list_public_record_locators(repository, transport=transport):
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
                candidate.get("request_sha256") != common["request_sha256"]
                or annotations["dev.kandelo.abi-staging.candidate-record-sha256"]
                != candidate_record
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
                    record_sha256=fetched.digest.removeprefix("sha256:"),
                )
            )
    except (OciPublicationError, ValueError) as error:
        if isinstance(error, InventoryError):
            raise
        raise InventoryError(f"verification inventory is invalid: {error}") from error
    facts.sort(key=lambda item: item.record_sha256)
    return tuple(facts)


def scan_attempt_repository(
    repository: str, *, transport: OciTransportV1
) -> tuple[AttemptFactV1, ...]:
    facts: list[AttemptFactV1] = []
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
            facts.append(
                AttemptFactV1(
                    request_sha256=attempt["request_sha256"],
                    subject=attempt["subject"],
                    contract_sha256=attempt["contract_sha256"],
                    retry_ordinal=attempt["retry_ordinal"],
                    outcome=attempt["outcome"],
                    guard_code=attempt["guard_code"],
                    completed_at=attempt["completed_at"],
                    record_sha256=fetched.digest.removeprefix("sha256:"),
                )
            )
    except (OciPublicationError, ValueError) as error:
        if isinstance(error, InventoryError):
            raise
        raise InventoryError(f"attempt inventory is invalid: {error}") from error
    facts.sort(key=lambda item: item.record_sha256)
    return tuple(facts)


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
        inventory = list_public_record_locators(repository, transport=transport)
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


def scan_scheduling_inventory(
    tap_plan: Mapping[str, Any],
    *,
    policy: TapStagingPolicyV1,
    verification_tests: tuple[VerificationTestDefinitionV1, ...],
    transport: OciTransportV1,
) -> PublicSchedulingInventoryV1:
    """Reconstruct one request's scheduler inputs from dedicated public repos."""

    validate_tap_plan(tap_plan)
    target_abi = tap_plan["target_abi"]["version"]
    names = sorted({item["identity"]["name"] for item in tap_plan["formulae"]})
    if not names or len(names) > policy.max_formulae:
        raise InventoryError("tap plan Formula inventory exceeds protected policy")
    attempts: list[AttemptFactV1] = []
    candidates: list[CandidateFactV1] = []
    verifications: list[VerificationFactV1] = []
    locators: dict[str, Mapping[str, str]] = {}
    candidate_records: dict[str, Mapping[str, Any]] = {}
    candidate_subjects: dict[str, dict[str, str]] = {}

    for name in names:
        repository = candidate_repository(policy, target_abi, formula=name)
        inspected = inspect_candidate_repository(repository, transport=transport)
        candidates.extend(inspected.facts)
        attempts.extend(
            scan_attempt_repository(
                attempt_repository(policy, target_abi, formula=name),
                transport=transport,
            )
        )
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
            }

    for name in names:
        base = candidate_repository(policy, target_abi, formula=name)
        for definition in verification_tests:
            for host in definition.hosts:
                verifications.extend(
                    scan_verification_repository(
                        receipt_repository(base, definition.id, host),
                        candidates=candidate_subjects,
                        transport=transport,
                    )
                )
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
    )
