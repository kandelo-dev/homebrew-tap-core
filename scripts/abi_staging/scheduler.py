"""Pure dependency scheduling and deterministic no-sleep retry decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Literal

from .canonical import canonical_sha256
from .plan import exact_formula_subject, validate_tap_plan
from .policy import TapStagingPolicyV1, VerificationTestDefinitionV1
from .reconcile import ReconciliationDecisionV1, reconciliation_work_scope


SHA256 = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
MAX_U64 = 2**64 - 1
RETRYABLE_GUARDS = frozenset(
    {
        "build_timeout",
        "transient_infrastructure_failure",
        "verification_timeout",
    }
)
# A Formula nonzero exit is not evidence that infrastructure failed, but the
# reconciler still gives it the same bounded attempt budget. This lets a fixed
# protected execution realm make progress without rewriting immutable failure
# history or manufacturing a new Formula identity.
RECONCILIATION_RETRY_GUARDS = RETRYABLE_GUARDS | {"build_failed"}
KNOWN_GUARDS = frozenset(
    {
        "request_invalid",
        "request_unauthorized",
        "abi_structure_changed_without_bump",
        "source_identity_mismatch",
        "source_custody_mismatch",
        "build_input_capture_incomplete",
        "build_failed",
        "build_timeout",
        "transient_infrastructure_failure",
        "candidate_integrity_mismatch",
        "candidate_public_readback_failed",
        "verification_failed",
        "verification_timeout",
        "dependency_unavailable",
        "tap_source_drift",
        "namespace_bootstrap_failed",
        "policy_version_unknown",
        "pages_product_incomplete",
    }
)


class SchedulingError(ValueError):
    """Raised when scheduling facts are incomplete, ambiguous, or unsafe."""


def _digest(value: str, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise SchedulingError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _subject(value: str, field: str = "Formula subject") -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 512:
        raise SchedulingError(f"{field} is outside its bound")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise SchedulingError(f"{field} is not canonical JSON") from error
    if not isinstance(parsed, dict) or set(parsed) != {
        "architecture",
        "identity",
        "kind",
    }:
        raise SchedulingError(f"{field} identity changed")
    if parsed["kind"] != "formula":
        raise SchedulingError(f"{field} is outside the Formula namespace")
    try:
        expected = exact_formula_subject(parsed["identity"], parsed["architecture"])
    except ValueError as error:
        raise SchedulingError(f"{field} is invalid: {error}") from error
    if value != expected:
        raise SchedulingError(f"{field} is not canonical")
    return value


def _timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        raise SchedulingError(f"{field} is not millisecond UTC RFC 3339")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise SchedulingError(f"{field} is not a real UTC timestamp") from error


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _ordinal(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
        raise SchedulingError(f"{field} must be an automatic attempt ordinal 0 through 3")
    return value


@dataclass(frozen=True)
class AttemptFactV1:
    request_sha256: str
    subject: str
    contract_sha256: str
    retry_ordinal: int
    outcome: Literal["success", "failure", "timeout", "canceled"]
    guard_code: str | None
    completed_at: str
    record_sha256: str

    def __post_init__(self) -> None:
        _digest(self.request_sha256, "attempt request")
        _subject(self.subject, "attempt subject")
        _digest(self.contract_sha256, "attempt contract")
        _ordinal(self.retry_ordinal, "attempt retry ordinal")
        _digest(self.record_sha256, "attempt record")
        _timestamp(self.completed_at, "attempt completion")
        if self.outcome not in {"success", "failure", "timeout", "canceled"}:
            raise SchedulingError("attempt outcome is unsupported")
        if self.outcome == "success":
            if self.guard_code is not None:
                raise SchedulingError("successful attempt cannot carry a guard")
        elif self.guard_code not in KNOWN_GUARDS:
            raise SchedulingError("failed attempt requires one registered guard")


@dataclass(frozen=True)
class CandidateFactV1:
    request_sha256: str
    subject: str
    contract_sha256: str
    record_sha256: str
    bottle_layer_sha256: str
    descriptor_capable: bool
    binding_record_sha256: str | None = None

    def __post_init__(self) -> None:
        _digest(self.request_sha256, "candidate request")
        _subject(self.subject, "candidate subject")
        _digest(self.contract_sha256, "candidate contract")
        _digest(self.record_sha256, "candidate record")
        _digest(self.bottle_layer_sha256, "candidate bottle layer")
        if not isinstance(self.descriptor_capable, bool):
            raise SchedulingError("candidate descriptor capability is not Boolean")
        if self.binding_record_sha256 is not None:
            _digest(self.binding_record_sha256, "candidate binding record")


@dataclass(frozen=True)
class VerificationFactV1:
    request_sha256: str
    subject: str
    candidate_record_sha256: str
    test_definition_sha256: str
    host: Literal["build", "node", "browser"]
    outcome: Literal["success", "failure", "timeout", "canceled"]
    guard_code: str | None
    attempt_ordinal: int
    completed_at: str
    record_sha256: str

    def __post_init__(self) -> None:
        _digest(self.request_sha256, "verification request")
        _subject(self.subject, "verification subject")
        _digest(self.candidate_record_sha256, "verification candidate record")
        _digest(self.test_definition_sha256, "verification test definition")
        _ordinal(self.attempt_ordinal, "verification attempt ordinal")
        _timestamp(self.completed_at, "verification completion")
        _digest(self.record_sha256, "verification record")
        if self.host not in {"build", "node", "browser"}:
            raise SchedulingError("verification host is unsupported")
        if self.outcome not in {"success", "failure", "timeout", "canceled"}:
            raise SchedulingError("verification outcome is unsupported")
        allowed_guards = {
            "success": {None},
            "failure": {
                "verification_failed",
                "transient_infrastructure_failure",
                "candidate_integrity_mismatch",
                "candidate_public_readback_failed",
                "namespace_bootstrap_failed",
            },
            "timeout": {
                "verification_timeout",
                "transient_infrastructure_failure",
            },
            "canceled": {
                "transient_infrastructure_failure",
                "verification_failed",
            },
        }
        if self.guard_code not in allowed_guards[self.outcome]:
            raise SchedulingError("verification outcome and guard are contradictory")


@dataclass(frozen=True)
class SchedulingRecordsV1:
    attempts: tuple[AttemptFactV1, ...]
    candidates: tuple[CandidateFactV1, ...]
    verifications: tuple[VerificationFactV1, ...]


@dataclass(frozen=True)
class ProtectedFailureFactsV1:
    authority: str
    kind: str
    job_conclusion: str
    http_status: int | None
    application_started: bool
    application_outcome: str | None


@dataclass(frozen=True)
class FailureClassificationV1:
    guard_code: str
    automatic_retry: bool


@dataclass(frozen=True)
class RetryDecisionV1:
    action: Literal["not-retryable", "wait", "retry", "maintainer-action"]
    current_ordinal: int
    next_ordinal: int | None
    delay_ms: int | None
    next_eligible_at: str | None
    exhausted: bool


@dataclass(frozen=True)
class ReadyWorkV1:
    subject: str
    work_class: Literal["required", "background"]
    action: Literal["build-candidate", "verify-candidate", "reuse-candidate"]
    attempt_ordinal: int
    contract_sha256: str
    candidate_record_sha256: str | None = None
    test_definition_sha256: str | None = None
    host: str | None = None


@dataclass(frozen=True)
class BlockedSubjectV1:
    subject: str
    guard_code: str
    dependency: str | None = None
    next_action: str = "none"
    next_eligible_at: str | None = None
    exhausted: bool = False


@dataclass(frozen=True)
class SchedulingDecisionV1:
    request_sha256: str
    ready: tuple[ReadyWorkV1, ...]
    blocked: tuple[BlockedSubjectV1, ...]
    complete: tuple[str, ...]
    pending: tuple[str, ...]


def deterministic_retry_delay_ms(
    request_digest: str,
    exact_subject: str,
    retry_number: int,
    base_ms: int,
    cap_ms: int,
) -> int:
    _digest(request_digest, "retry request")
    if (
        not isinstance(exact_subject, str)
        or not exact_subject
        or len(exact_subject.encode()) > 512
        or "\0" in exact_subject
        or any(character.isspace() for character in exact_subject)
    ):
        raise SchedulingError("retry subject must be a bounded exact identity")
    if (
        isinstance(retry_number, bool)
        or not isinstance(retry_number, int)
        or not 1 <= retry_number <= 3
    ):
        raise SchedulingError("automatic retry number must be 1 through 3")
    if (
        isinstance(base_ms, bool)
        or isinstance(cap_ms, bool)
        or not isinstance(base_ms, int)
        or not isinstance(cap_ms, int)
        or base_ms <= 0
        or cap_ms <= 0
        or base_ms > MAX_U64
        or cap_ms >= MAX_U64
    ):
        raise SchedulingError(
            "retry base and cap must be positive bounded milliseconds"
        )
    multiplier = 1 << (retry_number - 1)
    if base_ms > MAX_U64 // multiplier:
        raise SchedulingError("retry window arithmetic overflow")
    window_ms = min(cap_ms, base_ms * multiplier)
    seed = hashlib.sha256(
        request_digest.encode()
        + b"\0"
        + exact_subject.encode()
        + b"\0"
        + str(retry_number).encode()
    ).digest()
    return int.from_bytes(seed[:8], "big") % (window_ms + 1)


def classify_protected_failure(
    facts: ProtectedFailureFactsV1,
) -> FailureClassificationV1:
    if facts.authority != "protected-workflow":
        raise SchedulingError("candidate output cannot classify infrastructure failure")
    if not isinstance(facts.application_started, bool):
        raise SchedulingError("failure application-started fact is not Boolean")
    if facts.job_conclusion not in {
        "action_required",
        "cancelled",
        "failure",
        "stale",
        "startup_failure",
        "success",
        "timed_out",
    }:
        raise SchedulingError("failure job conclusion is not a protected failure")
    if facts.job_conclusion == "success" and facts.kind not in {
        "artifact-service-unavailable",
        "github-http",
        "registry-http",
        "transport-reset",
    }:
        raise SchedulingError(
            "successful application job cannot assert a non-transport failure"
        )
    if facts.application_outcome not in {None, "success", "failure", "timeout"}:
        raise SchedulingError("failure application outcome is unsupported")
    if facts.http_status is not None and (
        isinstance(facts.http_status, bool)
        or not isinstance(facts.http_status, int)
        or not 100 <= facts.http_status <= 599
    ):
        raise SchedulingError("failure HTTP status is invalid")
    transient = facts.application_outcome not in {"failure", "timeout"} and (
        facts.kind == "runner-lost"
        or (
            facts.kind == "artifact-service-unavailable"
            and (
                facts.http_status is None
                or facts.http_status == 429
                or 500 <= facts.http_status <= 599
            )
        )
        or (
            facts.kind in {"registry-http", "github-http"}
            and facts.http_status is not None
            and (facts.http_status == 429 or 500 <= facts.http_status <= 599)
        )
        or (facts.kind == "transport-reset" and not facts.application_started)
    )
    if transient:
        return FailureClassificationV1(
            "transient_infrastructure_failure", True
        )
    guards = {
        "formula-nonzero": "build_failed",
        "test-assertion": "verification_failed",
        "contract-failure": "build_input_capture_incomplete",
        "capture-failure": "build_input_capture_incomplete",
        "source-mismatch": "source_identity_mismatch",
        "archive-hazard": "candidate_integrity_mismatch",
        "digest-mismatch": "candidate_integrity_mismatch",
        "public-readback-failure": "candidate_public_readback_failed",
        "build-timeout": "build_timeout",
        "verification-timeout": "verification_timeout",
    }
    guard = guards.get(facts.kind, "build_failed")
    return FailureClassificationV1(guard, guard in RETRYABLE_GUARDS)


def retry_decision(
    request_digest: str,
    exact_subject: str,
    *,
    current_ordinal: int,
    guard_code: str,
    completed_at: str,
    now: str,
    policy: TapStagingPolicyV1,
) -> RetryDecisionV1:
    _digest(request_digest, "retry request")
    if not isinstance(exact_subject, str):
        raise SchedulingError("retry subject must be a bounded exact identity")
    _ordinal(current_ordinal, "current attempt ordinal")
    completed = _timestamp(completed_at, "retry completion")
    current_time = _timestamp(now, "retry clock")
    if guard_code not in KNOWN_GUARDS:
        raise SchedulingError("retry guard is not registered")
    if guard_code not in RECONCILIATION_RETRY_GUARDS:
        return RetryDecisionV1(
            "not-retryable", current_ordinal, None, None, None, False
        )
    if policy.automatic_retry_count != 3:
        raise SchedulingError("protected policy changed automatic retry count")
    if current_ordinal >= policy.automatic_retry_count:
        return RetryDecisionV1(
            "maintainer-action", current_ordinal, None, None, None, True
        )
    next_ordinal = current_ordinal + 1
    delay = deterministic_retry_delay_ms(
        request_digest,
        exact_subject,
        next_ordinal,
        policy.retry_base_ms,
        policy.retry_cap_ms,
    )
    if current_time < completed:
        raise SchedulingError("retry clock predates protected failure completion")
    try:
        eligible = completed + timedelta(milliseconds=delay)
    except OverflowError as error:
        raise SchedulingError("retry timestamp arithmetic overflow") from error
    return RetryDecisionV1(
        "retry" if current_time >= eligible else "wait",
        current_ordinal,
        next_ordinal,
        delay,
        _format_timestamp(eligible),
        False,
    )


def _deduplicate_records(records: SchedulingRecordsV1) -> SchedulingRecordsV1:
    def unique(values: Sequence[object], field: str) -> tuple[object, ...]:
        seen: dict[str, object] = {}
        for value in values:
            digest = (
                getattr(value, "binding_record_sha256", None)
                if isinstance(value, CandidateFactV1)
                else None
            ) or getattr(value, "record_sha256", None)
            if not isinstance(digest, str):
                raise SchedulingError(f"{field} contains an untyped record")
            if digest in seen and seen[digest] != value:
                raise SchedulingError(f"{field} record digest has conflicting facts")
            seen[digest] = value
        return tuple(seen[digest] for digest in sorted(seen))

    return SchedulingRecordsV1(
        attempts=unique(records.attempts, "attempt"),
        candidates=unique(records.candidates, "candidate"),
        verifications=unique(records.verifications, "verification"),
    )


def _definition_pairs(
    definitions: Sequence[VerificationTestDefinitionV1],
) -> tuple[tuple[str, str, str], ...]:
    pairs = []
    previous = ""
    for definition in definitions:
        identity = {
            "hosts": list(definition.hosts),
            "id": definition.id,
            "kandelo_paths": list(definition.kandelo_paths),
            "policy": definition.policy,
        }
        if canonical_sha256(identity) != definition.sha256:
            raise SchedulingError("verification test definition digest drifted")
        if definition.id <= previous:
            raise SchedulingError(
                "verification test definitions must be sorted and duplicate-free"
            )
        previous = definition.id
        for host in definition.hosts:
            if host not in {"build", "node", "browser"}:
                raise SchedulingError("verification test host is unsupported")
            pairs.append((definition.sha256, host, definition.id))
    if not pairs:
        raise SchedulingError("scheduler requires protected verification tests")
    return tuple(pairs)


def _dominant_terminal_failure(records: Sequence[object]) -> object:
    """Converge at-least-once failures without hiding deterministic failures."""

    if not records:
        raise SchedulingError("terminal failure selection requires one record")
    nonretryable = [
        item
        for item in records
        if getattr(item, "guard_code", None) not in RETRYABLE_GUARDS
    ]
    pool = nonretryable or list(records)
    guards = sorted({getattr(item, "guard_code", None) for item in pool})
    if not guards or guards[0] is None:
        raise SchedulingError("terminal failure lacks one registered guard")
    guarded = [item for item in pool if getattr(item, "guard_code", None) == guards[0]]
    latest_completion = max(getattr(item, "completed_at", "") for item in guarded)
    latest = [
        item
        for item in guarded
        if getattr(item, "completed_at", "") == latest_completion
    ]
    return min(latest, key=lambda item: getattr(item, "record_sha256", ""))


def schedule_ready_batch(
    plan: Mapping[str, object],
    records: SchedulingRecordsV1,
    reconciliation: ReconciliationDecisionV1,
    *,
    now: str,
    policy: TapStagingPolicyV1,
    verification_tests: Sequence[VerificationTestDefinitionV1],
) -> SchedulingDecisionV1:
    validate_tap_plan(plan)
    _timestamp(now, "scheduling clock")
    request_sha256 = plan["request_digest"]
    if reconciliation.request_digest != request_sha256:
        raise SchedulingError("reconciliation and tap plan name different requests")
    scope = reconciliation_work_scope(reconciliation)
    checked_records = _deduplicate_records(records)
    definitions = _definition_pairs(verification_tests)
    formulae: dict[str, Mapping[str, object]] = {}
    dependencies: dict[str, tuple[str, ...]] = {}
    work_classes: dict[str, str] = {}
    order: list[str] = []
    for formula in plan["formulae"]:
        identity = formula["identity"]
        subject = exact_formula_subject(identity["name"], identity["architecture"])
        formulae[subject] = formula
        dependencies[subject] = tuple(
            exact_formula_subject(item["formula"], item["architecture"])
            for item in formula["direct_dependencies"]
        )
        work_classes[subject] = formula["work_class"]
        order.append(subject)
    if tuple(order) != tuple(plan["required_subjects"] + plan["background_subjects"]):
        raise SchedulingError("tap plan subject order differs from Formula plans")

    for collection in (
        checked_records.attempts,
        checked_records.candidates,
        checked_records.verifications,
    ):
        for record in collection:
            if record.request_sha256 == request_sha256 and record.subject not in formulae:
                raise SchedulingError("current request record names an unknown Formula subject")

    ready: list[ReadyWorkV1] = []
    blocked: list[BlockedSubjectV1] = []
    complete: list[str] = []
    pending: list[str] = []
    state: dict[str, str] = {}

    for subject in order:
        work_class = work_classes[subject]
        allowed = (
            scope.allow_required if work_class == "required" else scope.allow_background
        )
        unavailable = next(
            (
                dependency
                for dependency in dependencies[subject]
                if state.get(dependency) != "complete"
            ),
            None,
        )
        if unavailable is not None:
            state[subject] = "blocked"
            blocked.append(
                BlockedSubjectV1(
                    subject,
                    "dependency_unavailable",
                    dependency=unavailable,
                    next_action="wait",
                )
            )
            continue
        formula = formulae[subject]
        contract = formula["contract_sha256"]
        if contract is None:
            state[subject] = "blocked"
            blocked.append(
                BlockedSubjectV1(subject, "build_input_capture_incomplete")
            )
            continue
        _digest(contract, f"contract for {subject}")
        candidates = [
            item
            for item in checked_records.candidates
            # WHY: historical records lack the descriptor consumed by the
            # product path, so they cannot participate in layer equivalence.
            if item.subject == subject
            and item.contract_sha256 == contract
            and item.descriptor_capable
        ]
        if len({item.bottle_layer_sha256 for item in candidates}) > 1:
            raise SchedulingError(
                "current contract has conflicting candidate bottle layers"
            )
        # At-least-once runs may publish more than one factual producer record
        # for identical contract/layer bytes. Select one immutable record
        # deterministically; never treat a differing layer as equivalent.
        current_candidates = [
            item for item in candidates if item.request_sha256 == request_sha256
        ]
        candidate = min(
            current_candidates or candidates,
            key=lambda item: (
                item.record_sha256,
                item.binding_record_sha256 or item.record_sha256,
            ),
            default=None,
        )
        candidate_is_bound = bool(current_candidates)
        if candidate is not None:
            next_verification: tuple[str, str, str, int] | None = None
            verification_blocker: BlockedSubjectV1 | None = None
            for definition_sha256, host, _definition_id in definitions:
                receipts = [
                    item
                    for item in checked_records.verifications
                    if item.subject == subject
                    and item.candidate_record_sha256 == candidate.record_sha256
                    and item.test_definition_sha256 == definition_sha256
                    and item.host == host
                ]
                if any(item.outcome == "success" for item in receipts):
                    continue
                if not receipts:
                    next_verification = (definition_sha256, host, _definition_id, 0)
                    break
                latest_ordinal = max(item.attempt_ordinal for item in receipts)
                latest = [
                    item for item in receipts if item.attempt_ordinal == latest_ordinal
                ]
                selected = _dominant_terminal_failure(latest)
                if not isinstance(selected, VerificationFactV1):
                    raise SchedulingError("verification failure selection changed type")
                guard = selected.guard_code
                if guard is None:
                    raise SchedulingError(
                        "unsuccessful verification receipt lacks a guard"
                    )
                retry = retry_decision(
                    request_sha256,
                    subject,
                    current_ordinal=selected.attempt_ordinal,
                    guard_code=guard,
                    completed_at=selected.completed_at,
                    now=now,
                    policy=policy,
                )
                if retry.action == "retry":
                    next_verification = (
                        definition_sha256,
                        host,
                        _definition_id,
                        retry.next_ordinal,
                    )
                else:
                    verification_blocker = BlockedSubjectV1(
                        subject,
                        guard,
                        next_action=retry.action,
                        next_eligible_at=retry.next_eligible_at,
                        exhausted=retry.exhausted,
                    )
                break
            if next_verification is not None:
                state[subject] = "pending"
                if allowed:
                    ready.append(
                        ReadyWorkV1(
                            subject,
                            work_class,
                            "verify-candidate",
                            next_verification[3],
                            contract,
                            candidate_record_sha256=candidate.record_sha256,
                            test_definition_sha256=next_verification[0],
                            host=next_verification[1],
                        )
                    )
                else:
                    pending.append(subject)
            elif verification_blocker is not None:
                state[subject] = "blocked"
                blocked.append(verification_blocker)
            elif candidate_is_bound:
                state[subject] = "complete"
                complete.append(subject)
            else:
                state[subject] = "pending"
                if allowed:
                    ready.append(
                        ReadyWorkV1(
                            subject,
                            work_class,
                            "reuse-candidate",
                            0,
                            contract,
                            candidate_record_sha256=candidate.record_sha256,
                        )
                    )
                else:
                    pending.append(subject)
            continue

        attempts = [
            item
            for item in checked_records.attempts
            if item.request_sha256 == request_sha256
            and item.subject == subject
            and item.contract_sha256 == contract
        ]
        if not attempts:
            state[subject] = "pending"
            if allowed:
                ready.append(
                    ReadyWorkV1(subject, work_class, "build-candidate", 0, contract)
                )
            else:
                pending.append(subject)
            continue
        latest_ordinal = max(item.retry_ordinal for item in attempts)
        latest = [item for item in attempts if item.retry_ordinal == latest_ordinal]
        successful = [item for item in latest if item.outcome == "success"]
        selected = (
            min(successful, key=lambda item: item.record_sha256)
            if successful
            else _dominant_terminal_failure(latest)
        )
        if not isinstance(selected, AttemptFactV1):
            raise SchedulingError("build failure selection changed type")
        if selected.outcome == "success":
            state[subject] = "blocked"
            blocked.append(BlockedSubjectV1(subject, "candidate_integrity_mismatch"))
            continue
        retry = retry_decision(
            request_sha256,
            subject,
            current_ordinal=selected.retry_ordinal,
            guard_code=selected.guard_code,
            completed_at=selected.completed_at,
            now=now,
            policy=policy,
        )
        if retry.action == "retry":
            state[subject] = "pending"
            if allowed:
                ready.append(
                    ReadyWorkV1(
                        subject,
                        work_class,
                        "build-candidate",
                        retry.next_ordinal,
                        contract,
                    )
                )
            else:
                pending.append(subject)
        else:
            state[subject] = "blocked"
            blocked.append(
                BlockedSubjectV1(
                    subject,
                    selected.guard_code,
                    next_action=retry.action,
                    next_eligible_at=retry.next_eligible_at,
                    exhausted=retry.exhausted,
                )
            )

    bounded_ready = tuple(ready[: policy.max_ready_subjects_per_cycle])
    pending.extend(item.subject for item in ready[policy.max_ready_subjects_per_cycle :])
    return SchedulingDecisionV1(
        request_sha256=request_sha256,
        ready=bounded_ready,
        blocked=tuple(blocked),
        complete=tuple(complete),
        pending=tuple(pending),
    )
