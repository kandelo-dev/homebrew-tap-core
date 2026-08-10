"""Pure pull-request lifecycle decisions for validated staging requests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Mapping, Sequence
from pathlib import Path
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal
import tomllib

from .canonical import CanonicalJsonError, canonical_sha256, parse_canonical_bytes
from .github_public import DiscoveredRequestV1
from .plan import PlanError, exact_formula_subject, parse_formula_subject

if TYPE_CHECKING:
    from .promotion import PromotionDecisionV1


GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
MAX_PROMOTION_WAVE = 16
MAX_PROMOTION_PLAN_BYTES = 256 * 1024 * 1024
PROMOTION_PLAN_STAGES = ("activation", "canonical", "metadata", "admission")


class ReconciliationError(ValueError):
    """Raised when lifecycle facts or activation policy are contradictory."""


@dataclass(frozen=True)
class PullRequestLifecycleV1:
    state: Literal["open", "merged", "closed"]
    current_head: str | None
    merged_commit: str | None


@dataclass(frozen=True)
class ReconciliationDecisionV1:
    request_digest: str
    claim_key: str
    lifecycle: PullRequestLifecycleV1
    current_for_pull_request: bool
    action: Literal[
        "observe-open",
        "observe-historical",
        "observe-merged",
        "stop-new-work",
        "resume-same-head",
    ]
    permitted_work: tuple[str, ...]
    blockers: tuple[MappingProxyType[str, Any], ...]


@dataclass(frozen=True)
class ReconciliationWorkScopeV1:
    allow_required: bool
    allow_background: bool


@dataclass(frozen=True)
class ProductEvidenceWorkScopeV1:
    allow_required: bool
    allow_background: bool
    authoritative: bool


@dataclass(frozen=True)
class ProductSelectionV1:
    product_id: str
    manifest_sha256: str
    applicability: Literal["required", "informational"]
    dependency_product_ids: tuple[str, ...]
    node_definition_ids: tuple[str, ...]
    browser_definition_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if STABLE_ID.fullmatch(self.product_id) is None:
            raise ReconciliationError("product selection ID is invalid")
        if SHA256.fullmatch(self.manifest_sha256) is None:
            raise ReconciliationError("product selection manifest digest is invalid")
        if self.applicability not in {"required", "informational"}:
            raise ReconciliationError("product selection applicability is unsupported")
        for field, values in (
            ("dependency", self.dependency_product_ids),
            ("Node definition", self.node_definition_ids),
            ("browser definition", self.browser_definition_ids),
        ):
            if values != tuple(sorted(set(values))) or any(
                STABLE_ID.fullmatch(value) is None for value in values
            ):
                raise ReconciliationError(
                    f"product selection {field} IDs are not sorted and unique"
                )
        if self.product_id in self.dependency_product_ids:
            raise ReconciliationError("product selection depends on itself")
        if not self.node_definition_ids and not self.browser_definition_ids:
            raise ReconciliationError("product selection has no evidence definitions")


@dataclass(frozen=True)
class ProductProgressV1:
    formulae_ready: bool
    candidate_runtime_sha256: str | None
    terminal_results: tuple[
        tuple[Literal["node", "browser"], str, Literal["success", "failure", "timeout"]],
        ...,
    ]
    evidence_record_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.formulae_ready, bool):
            raise ReconciliationError("product Formula readiness is not boolean")
        for value, field in (
            (self.candidate_runtime_sha256, "candidate runtime"),
            (self.evidence_record_sha256, "evidence record"),
        ):
            if value is not None and SHA256.fullmatch(value) is None:
                raise ReconciliationError(f"product {field} digest is invalid")
        if self.terminal_results != tuple(sorted(set(self.terminal_results))):
            raise ReconciliationError(
                "product terminal results are not sorted and duplicate-free"
            )
        for host, definition_id, outcome in self.terminal_results:
            if (
                host not in {"node", "browser"}
                or STABLE_ID.fullmatch(definition_id) is None
                or outcome not in {"success", "failure", "timeout"}
            ):
                raise ReconciliationError("product terminal result is invalid")


@dataclass(frozen=True)
class ProductReconciliationPlanV1:
    request_digest: str
    runtime_bundle_sha256: str | None
    prepare_runtime: bool
    authoritative: bool
    composition_work: tuple[MappingProxyType[str, Any], ...]
    node_work: tuple[MappingProxyType[str, Any], ...]
    browser_work: tuple[MappingProxyType[str, Any], ...]
    evidence_publication_work: tuple[MappingProxyType[str, Any], ...]
    blocked: tuple[MappingProxyType[str, Any], ...]
    complete: tuple[str, ...]


@dataclass(frozen=True)
class PromotionEpochV1:
    request_digest: str
    history_record_sha256: str | None
    activation_patch_sha256: str
    activation_record_sha256: str | None
    current_tap_commit: str
    current_tap_tree: str

    def __post_init__(self) -> None:
        _optional_digest(self.history_record_sha256, "promotion history record")
        _digest(self.request_digest, "promotion epoch request")
        _digest(self.activation_patch_sha256, "promotion activation patch")
        _optional_digest(self.activation_record_sha256, "promotion activation record")
        _git_sha(self.current_tap_commit, "promotion current tap commit")
        _git_sha(self.current_tap_tree, "promotion current tap tree")
        if self.activation_record_sha256 is not None and self.history_record_sha256 is None:
            raise ReconciliationError(
                "promotion activation cannot exist without exact ABI history"
            )


@dataclass(frozen=True)
class PromotionSubjectV1:
    decision: "PromotionDecisionV1"
    work_class: Literal["required", "background"]
    dependency_subjects: tuple[str, ...]

    def __post_init__(self) -> None:
        from .promotion import (
            PromotionDecisionV1,
            PromotionError,
            validate_promotion_decision,
        )

        if not isinstance(self.decision, PromotionDecisionV1):
            raise ReconciliationError("promotion subject decision is untyped")
        try:
            validate_promotion_decision(asdict(self.decision))
        except PromotionError as error:
            raise ReconciliationError(
                f"promotion subject decision is invalid: {error}"
            ) from error
        if self.work_class not in {"required", "background"}:
            raise ReconciliationError("promotion subject work class is unsupported")
        checked = tuple(
            _formula_subject(value, "promotion dependency subject")
            for value in self.dependency_subjects
        )
        if checked != tuple(sorted(set(checked))):
            raise ReconciliationError(
                "promotion dependency subjects are not sorted and unique"
            )
        if self.decision.formula_subject in checked:
            raise ReconciliationError("promotion subject depends on itself")


@dataclass(frozen=True)
class PromotionProgressV1:
    canonical_manifest_sha256: str | None = None
    canonical_readback_sha256: str | None = None
    metadata_commit: str | None = None
    metadata_tree: str | None = None
    metadata_update_sha256: str | None = None
    metadata_readback_sha256: str | None = None
    admission_record_sha256: str | None = None
    stale_admission_record_sha256: str | None = None

    def __post_init__(self) -> None:
        canonical = (
            self.canonical_manifest_sha256,
            self.canonical_readback_sha256,
        )
        metadata = (
            self.metadata_commit,
            self.metadata_tree,
            self.metadata_update_sha256,
            self.metadata_readback_sha256,
        )
        if any(value is not None for value in canonical):
            if not all(value is not None for value in canonical):
                raise ReconciliationError(
                    "promotion canonical progress is only partially authenticated"
                )
            _digest(canonical[0], "promotion canonical manifest")
            _digest(canonical[1], "promotion canonical readback")
        if any(value is not None for value in metadata):
            if not all(value is not None for value in metadata):
                raise ReconciliationError(
                    "promotion metadata progress is only partially authenticated"
                )
            if canonical[0] is None:
                raise ReconciliationError(
                    "promotion metadata cannot precede canonical readback"
                )
            _git_sha(metadata[0], "promotion metadata commit")
            _git_sha(metadata[1], "promotion metadata tree")
            _digest(metadata[2], "promotion metadata update")
            _digest(metadata[3], "promotion metadata readback")
        _optional_digest(self.admission_record_sha256, "promotion admission record")
        _optional_digest(
            self.stale_admission_record_sha256,
            "stale promotion admission record",
        )
        if self.admission_record_sha256 is not None and metadata[0] is None:
            raise ReconciliationError(
                "promotion admission cannot precede metadata readback"
            )
        if (
            self.stale_admission_record_sha256 is not None
            and (canonical[0] is None or self.admission_record_sha256 is not None)
        ):
            raise ReconciliationError(
                "stale promotion admission requires canonical progress only"
            )


@dataclass(frozen=True)
class PromotionReconciliationPlanV1:
    mode: Literal["disabled", "observe", "active"]
    authoritative: bool
    request_digest: str
    history_record_sha256: str | None
    activation_work: tuple[MappingProxyType[str, Any], ...]
    canonical_work: tuple[MappingProxyType[str, Any], ...]
    metadata_work: tuple[MappingProxyType[str, Any], ...]
    admission_work: tuple[MappingProxyType[str, Any], ...]
    blocked: tuple[MappingProxyType[str, Any], ...]
    complete: tuple[str, ...]
    canonical_matrix: Mapping[str, list[dict[str, Any]]]
    metadata_matrix: Mapping[str, list[dict[str, Any]]]
    admission_matrix: Mapping[str, list[dict[str, Any]]]


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ReconciliationError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _optional_digest(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _digest(value, field)


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise ReconciliationError(f"{field} is not a full lowercase Git SHA")
    return value


def _formula_subject(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ReconciliationError(f"{field} is not a string")
    try:
        identity, architecture = parse_formula_subject(value, field)
        expected = exact_formula_subject(identity, architecture)
    except (PlanError, TypeError, ValueError) as error:
        raise ReconciliationError(f"{field} is invalid: {error}") from error
    if value != expected:
        raise ReconciliationError(f"{field} is not canonical")
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_plain(child) for child in value]
    return value


def _exact_mapping(
    value: Any, keys: frozenset[str], field: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != keys:
        raise ReconciliationError(f"{field} fields changed")
    return value


def _promotion_tap_source(value: Any) -> dict[str, str]:
    source = _exact_mapping(
        value,
        frozenset({"repository", "commit", "tree"}),
        "promotion plan tap source",
    )
    repository = source["repository"]
    if not isinstance(repository, str) or REPOSITORY.fullmatch(repository) is None:
        raise ReconciliationError("promotion plan tap repository is invalid")
    return {
        "repository": repository,
        "commit": _git_sha(source["commit"], "promotion plan tap commit"),
        "tree": _git_sha(source["tree"], "promotion plan tap tree"),
    }


def _validate_promotion_plan_document(value: Any) -> dict[str, Any]:
    document = _exact_mapping(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "mode",
                "authoritative",
                "request_sha256",
                "tap_source",
                "history_record_sha256",
                "work",
                "blocked",
                "complete",
                "matrices",
            }
        ),
        "promotion plan document",
    )
    mode = document["mode"]
    if (
        document["schema"] != 1
        or document["kind"] != "kandelo-abi-staging-promotion-plan"
        or mode not in {"disabled", "observe", "active"}
        or not isinstance(document["authoritative"], bool)
        or document["authoritative"] is not (mode == "active")
    ):
        raise ReconciliationError("promotion plan document protocol is unsupported")
    request_sha256 = _digest(
        document["request_sha256"], "promotion plan request"
    )
    tap_source = _promotion_tap_source(document["tap_source"])
    history_record_sha256 = _optional_digest(
        document["history_record_sha256"], "promotion plan history record"
    )
    work = _exact_mapping(
        document["work"], frozenset(PROMOTION_PLAN_STAGES), "promotion plan work"
    )
    checked_work: dict[str, list[dict[str, Any]]] = {}
    all_work_ids: set[str] = set()
    for stage in PROMOTION_PLAN_STAGES:
        entries = work[stage]
        if (
            not isinstance(entries, Sequence)
            or isinstance(entries, (str, bytes, bytearray))
            or len(entries) > MAX_PROMOTION_WAVE
        ):
            raise ReconciliationError(f"promotion {stage} work exceeds its bound")
        checked_entries: list[dict[str, Any]] = []
        for raw_entry in entries:
            entry = _exact_mapping(
                raw_entry,
                frozenset({"summary", "detail_sha256", "detail"}),
                f"promotion {stage} work entry",
            )
            summary = entry["summary"]
            detail = entry["detail"]
            if not isinstance(summary, Mapping) or not isinstance(detail, Mapping):
                raise ReconciliationError(
                    f"promotion {stage} work entry is not an object"
                )
            work_id = _digest(
                summary.get("work_id"), f"promotion {stage} work ID"
            )
            if work_id in all_work_ids:
                raise ReconciliationError("promotion work ID is duplicated")
            all_work_ids.add(work_id)
            detail_sha256 = _digest(
                entry["detail_sha256"], f"promotion {stage} detail"
            )
            if canonical_sha256(detail) != detail_sha256:
                raise ReconciliationError(
                    f"promotion {stage} work detail identity changed"
                )
            checked_entries.append(
                {
                    "summary": _plain(summary),
                    "detail_sha256": detail_sha256,
                    "detail": _plain(detail),
                }
            )
        checked_work[stage] = checked_entries

    blocked = document["blocked"]
    if (
        not isinstance(blocked, Sequence)
        or isinstance(blocked, (str, bytes, bytearray))
        or len(blocked) > 65_536
        or any(not isinstance(item, Mapping) for item in blocked)
    ):
        raise ReconciliationError("promotion plan blockers exceed their bound")
    complete = document["complete"]
    complete_values = list(complete) if isinstance(complete, Sequence) else []
    if (
        not isinstance(complete, Sequence)
        or isinstance(complete, (str, bytes, bytearray))
        or len(complete) > 65_536
        or complete_values != sorted(set(complete_values))
    ):
        raise ReconciliationError("promotion plan completion set is invalid")
    for subject in complete_values:
        _formula_subject(subject, "promotion completed subject")

    matrices = _exact_mapping(
        document["matrices"],
        frozenset({"canonical", "metadata", "admission"}),
        "promotion plan matrices",
    )
    active = mode == "active"
    expected_matrices = {
        "canonical": {
            "include": [
                _plain(item["summary"]) for item in checked_work["canonical"]
            ]
            if active
            else []
        },
        "metadata": {
            "include": [
                _plain(item["summary"])
                for stage in ("activation", "metadata")
                for item in checked_work[stage]
            ]
            if active
            else []
        },
        "admission": {
            "include": [
                _plain(item["summary"]) for item in checked_work["admission"]
            ]
            if active
            else []
        },
    }
    if _plain(matrices) != expected_matrices:
        raise ReconciliationError("promotion plan matrices differ from exact work")
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-promotion-plan",
        "mode": mode,
        "authoritative": active,
        "request_sha256": request_sha256,
        "tap_source": tap_source,
        "history_record_sha256": history_record_sha256,
        "work": checked_work,
        "blocked": [_plain(item) for item in blocked],
        "complete": complete_values,
        "matrices": expected_matrices,
    }


def build_promotion_plan_document(
    plan: PromotionReconciliationPlanV1,
    *,
    tap_source: Mapping[str, Any],
    work_details: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Bind every scheduled item to one canonical protected work detail."""

    if not isinstance(plan, PromotionReconciliationPlanV1):
        raise ReconciliationError("promotion workflow plan is untyped")
    details = _exact_mapping(
        work_details,
        frozenset(PROMOTION_PLAN_STAGES),
        "promotion plan work details",
    )
    stage_work = {
        "activation": plan.activation_work,
        "canonical": plan.canonical_work,
        "metadata": plan.metadata_work,
        "admission": plan.admission_work,
    }
    work: dict[str, list[dict[str, Any]]] = {}
    for stage in PROMOTION_PLAN_STAGES:
        stage_details = details[stage]
        if not isinstance(stage_details, Mapping):
            raise ReconciliationError(
                f"promotion {stage} work details are not an object"
            )
        summaries = [dict(item) for item in stage_work[stage]]
        work_ids = {item["work_id"] for item in summaries}
        if set(stage_details) != work_ids:
            raise ReconciliationError(
                f"promotion {stage} work details do not cover exact work"
            )
        work[stage] = []
        for summary in summaries:
            detail = stage_details[summary["work_id"]]
            if not isinstance(detail, Mapping):
                raise ReconciliationError(
                    f"promotion {stage} work detail is not an object"
                )
            plain_detail = _plain(detail)
            work[stage].append(
                {
                    "summary": summary,
                    "detail_sha256": canonical_sha256(plain_detail),
                    "detail": plain_detail,
                }
            )
    return _validate_promotion_plan_document(
        {
            "schema": 1,
            "kind": "kandelo-abi-staging-promotion-plan",
            "mode": plan.mode,
            "authoritative": plan.authoritative,
            "request_sha256": plan.request_digest,
            "tap_source": _plain(tap_source),
            "history_record_sha256": plan.history_record_sha256,
            "work": work,
            "blocked": [_plain(item) for item in plan.blocked],
            "complete": list(plan.complete),
            "matrices": {
                "canonical": _plain(plan.canonical_matrix),
                "metadata": _plain(plan.metadata_matrix),
                "admission": _plain(plan.admission_matrix),
            },
        }
    )


def load_promotion_plan_document(body: bytes) -> dict[str, Any]:
    try:
        value = parse_canonical_bytes(body, maximum_bytes=MAX_PROMOTION_PLAN_BYTES)
    except CanonicalJsonError as error:
        raise ReconciliationError(f"promotion plan is not canonical: {error}") from error
    return _validate_promotion_plan_document(value)


def select_promotion_plan_work(
    document: Mapping[str, Any], *, stage: str, work_id: str
) -> dict[str, Any]:
    checked = _validate_promotion_plan_document(document)
    if stage not in PROMOTION_PLAN_STAGES:
        raise ReconciliationError("promotion work stage is unsupported")
    digest = _digest(work_id, "promotion selected work ID")
    matches = [
        item
        for item in checked["work"][stage]
        if item["summary"]["work_id"] == digest
    ]
    if len(matches) != 1:
        raise ReconciliationError("promotion work is absent or duplicated")
    return matches[0]


def _promotion_item(
    *,
    stage: str,
    subject: PromotionSubjectV1,
    epoch: PromotionEpochV1,
    extra: Mapping[str, Any] | None = None,
) -> MappingProxyType[str, Any]:
    decision = asdict(subject.decision)
    decision_sha256 = canonical_sha256(decision)
    identity = {
        "stage": stage,
        "decision_sha256": decision_sha256,
        "request_digest": epoch.request_digest,
        "formula_subject": subject.decision.formula_subject,
        "current_tap_commit": epoch.current_tap_commit,
        "current_tap_tree": epoch.current_tap_tree,
        **({} if extra is None else dict(extra)),
    }
    return MappingProxyType(
        {
            "work_id": canonical_sha256(identity),
            "work_class": subject.work_class,
            "formula_subject": subject.decision.formula_subject,
            "decision_sha256": decision_sha256,
            "candidate_record_sha256": subject.decision.candidate_record_digest,
            "bottle_layer_sha256": subject.decision.bottle_layer_sha256,
            **({} if extra is None else dict(extra)),
        }
    )


def _promotion_plan_result(
    *,
    mode: str,
    request_digest: str,
    history_record_sha256: str | None,
    activation_work: list[MappingProxyType[str, Any]],
    canonical_work: list[MappingProxyType[str, Any]],
    metadata_work: list[MappingProxyType[str, Any]],
    admission_work: list[MappingProxyType[str, Any]],
    blocked: list[MappingProxyType[str, Any]],
    complete: list[str],
) -> PromotionReconciliationPlanV1:
    active = mode == "active"
    metadata_jobs = [*activation_work, *metadata_work]
    return PromotionReconciliationPlanV1(
        mode=mode,
        authoritative=active,
        request_digest=request_digest,
        history_record_sha256=history_record_sha256,
        activation_work=tuple(activation_work),
        canonical_work=tuple(canonical_work),
        metadata_work=tuple(metadata_work),
        admission_work=tuple(admission_work),
        blocked=tuple(blocked),
        complete=tuple(complete),
        canonical_matrix={
            "include": [dict(item) for item in canonical_work] if active else []
        },
        metadata_matrix={
            "include": [dict(item) for item in metadata_jobs] if active else []
        },
        admission_matrix={
            "include": [dict(item) for item in admission_work] if active else []
        },
    )


def build_promotion_workflow_plan(
    reconciliation: ReconciliationDecisionV1,
    subjects: Sequence[PromotionSubjectV1],
    *,
    epoch: PromotionEpochV1,
    progress: Mapping[str, PromotionProgressV1],
    activation_mode: str,
) -> PromotionReconciliationPlanV1:
    """Plan one bounded, independently convergent merged-PR promotion wave."""

    if activation_mode not in {"disabled", "observe", "active"}:
        raise ReconciliationError("promotion activation mode is unsupported")
    if not isinstance(reconciliation, ReconciliationDecisionV1):
        raise ReconciliationError("promotion reconciliation decision is untyped")
    _digest(reconciliation.request_digest, "promotion reconciliation request")
    if not isinstance(epoch, PromotionEpochV1):
        raise ReconciliationError("promotion epoch is untyped")
    if epoch.request_digest != reconciliation.request_digest:
        raise ReconciliationError("promotion epoch names another request")
    checked_subjects = tuple(subjects)
    by_subject: dict[str, PromotionSubjectV1] = {}
    for subject in checked_subjects:
        if not isinstance(subject, PromotionSubjectV1):
            raise ReconciliationError("promotion subject is untyped")
        name = subject.decision.formula_subject
        if name in by_subject:
            raise ReconciliationError("promotion subject repeats")
        if subject.decision.request_digest != reconciliation.request_digest:
            raise ReconciliationError("promotion subject names another request")
        by_subject[name] = subject
    if set(progress) != set(by_subject) or any(
        not isinstance(item, PromotionProgressV1) for item in progress.values()
    ):
        raise ReconciliationError("promotion progress does not cover exact subjects")
    for subject in by_subject.values():
        if any(dependency not in by_subject for dependency in subject.dependency_subjects):
            raise ReconciliationError("promotion dependency is outside the exact plan")

    ordered = sorted(
        by_subject.values(),
        key=lambda item: (
            0 if item.work_class == "required" else 1,
            item.decision.formula_subject,
        ),
    )
    empty: list[MappingProxyType[str, Any]] = []
    if activation_mode == "disabled":
        return _promotion_plan_result(
            mode=activation_mode,
            request_digest=reconciliation.request_digest,
            history_record_sha256=epoch.history_record_sha256,
            activation_work=empty,
            canonical_work=[],
            metadata_work=[],
            admission_work=[],
            blocked=[],
            complete=[],
        )

    blocked: list[MappingProxyType[str, Any]] = []
    if (
        reconciliation.lifecycle.state != "merged"
        or reconciliation.action != "observe-merged"
        or not reconciliation.current_for_pull_request
    ):
        blocked.extend(
            MappingProxyType(
                {
                    "formula_subject": item.decision.formula_subject,
                    "guard_code": "pull_request_not_merged",
                    "blocked_by": None,
                }
            )
            for item in ordered
        )
        return _promotion_plan_result(
            mode=activation_mode,
            request_digest=reconciliation.request_digest,
            history_record_sha256=epoch.history_record_sha256,
            activation_work=[],
            canonical_work=[],
            metadata_work=[],
            admission_work=[],
            blocked=blocked,
            complete=[],
        )

    merge = {
        "repository": ordered[0].decision.merged_pull_request["repository"],
        "number": ordered[0].decision.merged_pull_request["number"],
        "head": ordered[0].decision.merged_pull_request["head"],
        "merge_commit": ordered[0].decision.merged_pull_request["merge_commit"],
    } if ordered else None
    for item in ordered:
        current_merge = dict(item.decision.merged_pull_request)
        if (
            merge is None
            or current_merge != merge
            or current_merge["head"] != reconciliation.lifecycle.current_head
            or current_merge["merge_commit"] != reconciliation.lifecycle.merged_commit
        ):
            raise ReconciliationError(
                "promotion subjects do not share the exact merged pull request"
            )

    if epoch.history_record_sha256 is None:
        blocked.extend(
            MappingProxyType(
                {
                    "formula_subject": item.decision.formula_subject,
                    "guard_code": "history_unavailable",
                    "blocked_by": None,
                }
            )
            for item in ordered
        )
        return _promotion_plan_result(
            mode=activation_mode,
            request_digest=reconciliation.request_digest,
            history_record_sha256=None,
            activation_work=[],
            canonical_work=[],
            metadata_work=[],
            admission_work=[],
            blocked=blocked,
            complete=[],
        )

    if epoch.activation_record_sha256 is None:
        activation_identity = {
            "stage": "successor-activation",
            "request_digest": epoch.request_digest,
            "history_record_sha256": epoch.history_record_sha256,
            "activation_patch_sha256": epoch.activation_patch_sha256,
            "current_tap_commit": epoch.current_tap_commit,
            "current_tap_tree": epoch.current_tap_tree,
        }
        activation = MappingProxyType(
            {
                "operation": "successor-activation",
                "work_id": canonical_sha256(activation_identity),
                **activation_identity,
            }
        )
        return _promotion_plan_result(
            mode=activation_mode,
            request_digest=reconciliation.request_digest,
            history_record_sha256=epoch.history_record_sha256,
            activation_work=[activation],
            canonical_work=[],
            metadata_work=[],
            admission_work=[],
            blocked=[
                MappingProxyType(
                    {
                        "formula_subject": item.decision.formula_subject,
                        "guard_code": "activation_pending",
                        "blocked_by": None,
                    }
                )
                for item in ordered
            ],
            complete=[],
        )

    canonical_work: list[MappingProxyType[str, Any]] = []
    metadata_work: list[MappingProxyType[str, Any]] = []
    admission_work: list[MappingProxyType[str, Any]] = []
    complete: list[str] = []
    scheduled = 0
    metadata_owner: str | None = None
    completed_subjects = {
        name
        for name, state in progress.items()
        if state.admission_record_sha256 is not None
    }
    for subject in ordered:
        name = subject.decision.formula_subject
        state = progress[name]
        if state.admission_record_sha256 is not None:
            complete.append(name)
            continue
        unavailable = next(
            (
                dependency
                for dependency in subject.dependency_subjects
                if dependency not in completed_subjects
            ),
            None,
        )
        if unavailable is not None:
            blocked.append(
                MappingProxyType(
                    {
                        "formula_subject": name,
                        "guard_code": "dependency_unavailable",
                        "blocked_by": unavailable,
                    }
                )
            )
            continue
        if subject.decision.eligibility == "rebuild-required":
            blocked.append(
                MappingProxyType(
                    {
                        "formula_subject": name,
                        "guard_code": "tap_source_drift",
                        "blocked_by": None,
                    }
                )
            )
            continue
        if subject.decision.eligibility != "eligible":
            blocked.append(
                MappingProxyType(
                    {
                        "formula_subject": name,
                        "guard_code": "promotion_ineligible",
                        "blocked_by": None,
                    }
                )
            )
            continue
        if scheduled >= MAX_PROMOTION_WAVE:
            blocked.append(
                MappingProxyType(
                    {
                        "formula_subject": name,
                        "guard_code": "promotion_wave_deferred",
                        "blocked_by": None,
                    }
                )
            )
            continue
        if state.canonical_manifest_sha256 is None:
            canonical_item = _promotion_item(
                stage="publish-canonical",
                subject=subject,
                epoch=epoch,
                extra={
                    "history_record_sha256": epoch.history_record_sha256,
                    "activation_record_sha256": epoch.activation_record_sha256,
                },
            )
            canonical_work.append(canonical_item)
            if metadata_owner is None:
                metadata_owner = name
                metadata_item = MappingProxyType(
                    {
                        **dict(
                            _promotion_item(
                                stage="update-tap-metadata",
                                subject=subject,
                                epoch=epoch,
                                extra={
                                    "canonical_work_id": canonical_item["work_id"],
                                    "activation_record_sha256": (
                                        epoch.activation_record_sha256
                                    ),
                                },
                            )
                        ),
                        "operation": "formula-metadata",
                    }
                )
                metadata_work.append(metadata_item)
                admission_work.append(
                    _promotion_item(
                        stage="publish-admission",
                        subject=subject,
                        epoch=epoch,
                        extra={
                            "canonical_work_id": canonical_item["work_id"],
                            "metadata_work_id": metadata_item["work_id"],
                        },
                    )
                )
            else:
                blocked.append(
                    MappingProxyType(
                        {
                            "formula_subject": name,
                            "guard_code": "dependency_unavailable",
                            "blocked_by": metadata_owner,
                        }
                    )
                )
        elif state.metadata_commit is None:
            if metadata_owner is None:
                metadata_owner = name
                metadata_item = MappingProxyType(
                    {
                        **dict(
                            _promotion_item(
                                stage="update-tap-metadata",
                                subject=subject,
                                epoch=epoch,
                                extra={
                                    "canonical_manifest_sha256": state.canonical_manifest_sha256,
                                    "canonical_readback_sha256": state.canonical_readback_sha256,
                                    "activation_record_sha256": epoch.activation_record_sha256,
                                },
                            )
                        ),
                        "operation": "formula-metadata",
                    }
                )
                metadata_work.append(metadata_item)
                if state.stale_admission_record_sha256 is None:
                    admission_work.append(
                        _promotion_item(
                            stage="publish-admission",
                            subject=subject,
                            epoch=epoch,
                            extra={
                                "canonical_manifest_sha256": state.canonical_manifest_sha256,
                                "canonical_readback_sha256": state.canonical_readback_sha256,
                                "metadata_work_id": metadata_item["work_id"],
                            },
                        )
                    )
            else:
                blocked.append(
                    MappingProxyType(
                        {
                            "formula_subject": name,
                            "guard_code": "dependency_unavailable",
                            "blocked_by": metadata_owner,
                        }
                    )
                )
        else:
            admission_work.append(
                _promotion_item(
                    stage="publish-admission",
                    subject=subject,
                    epoch=epoch,
                    extra={
                        "canonical_manifest_sha256": state.canonical_manifest_sha256,
                        "canonical_readback_sha256": state.canonical_readback_sha256,
                        "metadata_commit": state.metadata_commit,
                        "metadata_tree": state.metadata_tree,
                        "metadata_update_sha256": state.metadata_update_sha256,
                        "metadata_readback_sha256": state.metadata_readback_sha256,
                    },
                )
            )
        scheduled += 1

    return _promotion_plan_result(
        mode=activation_mode,
        request_digest=reconciliation.request_digest,
        history_record_sha256=epoch.history_record_sha256,
        activation_work=[],
        canonical_work=canonical_work,
        metadata_work=metadata_work,
        admission_work=admission_work,
        blocked=blocked,
        complete=complete,
    )


def reconciliation_work_scope(
    decision: ReconciliationDecisionV1,
) -> ReconciliationWorkScopeV1:
    """Translate exact-head lifecycle into new-work permission only."""

    if decision.action in {
        "observe-open",
        "observe-historical",
        "observe-merged",
        "resume-same-head",
    }:
        return ReconciliationWorkScopeV1(True, True)
    return ReconciliationWorkScopeV1(False, False)


def product_evidence_work_scope(
    decision: ReconciliationDecisionV1, activation_mode: str
) -> ProductEvidenceWorkScopeV1:
    """Allow observe-mode proof while keeping it non-authoritative."""

    if activation_mode not in {"observe", "active"}:
        raise ReconciliationError("product evidence activation mode is unsupported")
    lifecycle = reconciliation_work_scope(decision)
    return ProductEvidenceWorkScopeV1(
        allow_required=lifecycle.allow_required,
        allow_background=lifecycle.allow_background,
        authoritative=activation_mode == "active",
    )


def _product_work(
    *,
    decision: ReconciliationDecisionV1,
    selection: ProductSelectionV1,
    stage: str,
    runtime_bundle_sha256: str,
    host: str | None = None,
    definition_id: str | None = None,
) -> MappingProxyType[str, Any]:
    identity = {
        "request_digest": decision.request_digest,
        "product_id": selection.product_id,
        "manifest_sha256": selection.manifest_sha256,
        "runtime_bundle_sha256": runtime_bundle_sha256,
        "stage": stage,
        "host": host,
        "definition_id": definition_id,
    }
    return MappingProxyType(
        {
            "work_id": canonical_sha256(identity),
            "product_id": selection.product_id,
            "manifest_sha256": selection.manifest_sha256,
            "applicability": selection.applicability,
            "stage": stage,
            **({"host": host} if host is not None else {}),
            **(
                {"definition_id": definition_id}
                if definition_id is not None
                else {}
            ),
        }
    )


def plan_product_reconciliation(
    decision: ReconciliationDecisionV1,
    selections: Sequence[ProductSelectionV1],
    *,
    runtime_bundle_sha256: str | None,
    progress: Mapping[str, ProductProgressV1],
    activation_mode: str,
) -> ProductReconciliationPlanV1:
    """Schedule one bounded product DAG wave from exact immutable facts."""

    if decision.request_digest == "" or SHA256.fullmatch(decision.request_digest) is None:
        raise ReconciliationError("product plan request digest is invalid")
    scope = product_evidence_work_scope(decision, activation_mode)
    if runtime_bundle_sha256 is not None and SHA256.fullmatch(
        runtime_bundle_sha256
    ) is None:
        raise ReconciliationError("product runtime bundle digest is invalid")
    checked = tuple(selections)
    if len(checked) > 4_096:
        raise ReconciliationError("product selection exceeds its bound")
    ordered = tuple(
        sorted(
            checked,
            key=lambda item: (
                0 if item.applicability == "required" else 1,
                item.product_id,
            ),
        )
    )
    if checked != ordered or len({item.product_id for item in checked}) != len(checked):
        raise ReconciliationError(
            "product selections are not required-first and duplicate-free"
        )
    product_ids = {item.product_id for item in checked}
    for item in checked:
        if not set(item.dependency_product_ids).issubset(product_ids):
            raise ReconciliationError("product selection names an unselected dependency")
    if set(progress) != product_ids or any(
        not isinstance(item, ProductProgressV1) for item in progress.values()
    ):
        raise ReconciliationError("product progress differs from selected products")

    empty = ProductReconciliationPlanV1(
        request_digest=decision.request_digest,
        runtime_bundle_sha256=runtime_bundle_sha256,
        prepare_runtime=False,
        authoritative=scope.authoritative,
        composition_work=(),
        node_work=(),
        browser_work=(),
        evidence_publication_work=(),
        blocked=(),
        complete=(),
    )
    if not scope.allow_required:
        return empty
    if runtime_bundle_sha256 is None:
        return ProductReconciliationPlanV1(
            **{
                **empty.__dict__,
                "prepare_runtime": True,
            }
        )

    composition = []
    node = []
    browser = []
    publication = []
    blockers = []
    complete = []
    for selection in checked:
        state = progress[selection.product_id]
        current_candidate = state.candidate_runtime_sha256 == runtime_bundle_sha256
        if not current_candidate:
            missing_dependencies = [
                dependency
                for dependency in selection.dependency_product_ids
                if progress[dependency].candidate_runtime_sha256
                != runtime_bundle_sha256
            ]
            if not state.formulae_ready or missing_dependencies:
                blockers.append(
                    MappingProxyType(
                        {
                            "product_id": selection.product_id,
                            "applicability": selection.applicability,
                            "guard_code": "dependency_unavailable",
                            "blocked_by": tuple(
                                (["formula-inputs"] if not state.formulae_ready else [])
                                + missing_dependencies
                            ),
                        }
                    )
                )
                continue
            composition.append(
                _product_work(
                    decision=decision,
                    selection=selection,
                    stage="compose-product",
                    runtime_bundle_sha256=runtime_bundle_sha256,
                )
            )
            continue

        # A protected aggregate is the durable terminal fact.  Its inspector
        # revalidates the complete receipt set, so a later workflow run does
        # not need ephemeral host-result artifacts to rediscover completion.
        if state.evidence_record_sha256 is not None:
            complete.append(selection.product_id)
            continue

        expected = tuple(
            [("browser", value) for value in selection.browser_definition_ids]
            + [("node", value) for value in selection.node_definition_ids]
        )
        terminal = {
            (host, definition): outcome
            for host, definition, outcome in state.terminal_results
        }
        if not set(terminal).issubset(set(expected)):
            raise ReconciliationError(
                "product progress contains an undeclared evidence result"
            )
        for host, definition_id in expected:
            if (host, definition_id) in terminal:
                continue
            work = _product_work(
                decision=decision,
                selection=selection,
                stage=f"{host}-product-evidence",
                runtime_bundle_sha256=runtime_bundle_sha256,
                host=host,
                definition_id=definition_id,
            )
            (node if host == "node" else browser).append(work)
        if len(terminal) != len(expected):
            continue
        if state.evidence_record_sha256 is None:
            publication.append(
                _product_work(
                    decision=decision,
                    selection=selection,
                    stage="publish-product-evidence",
                    runtime_bundle_sha256=runtime_bundle_sha256,
                )
            )
        else:
            complete.append(selection.product_id)

    return ProductReconciliationPlanV1(
        request_digest=decision.request_digest,
        runtime_bundle_sha256=runtime_bundle_sha256,
        prepare_runtime=False,
        authoritative=scope.authoritative,
        composition_work=tuple(composition),
        node_work=tuple(node),
        browser_work=tuple(browser),
        evidence_publication_work=tuple(publication),
        blocked=tuple(blockers),
        complete=tuple(complete),
    )


def build_product_workflow_seed(
    request: Mapping[str, Any],
    decision: ReconciliationDecisionV1,
    *,
    activation_mode: str,
) -> dict[str, Any]:
    """Derive bounded matrices only from a validated request's product evidence."""

    scope = product_evidence_work_scope(decision, activation_mode)
    requirements = request.get("requirements")
    if not isinstance(requirements, Mapping):
        raise ReconciliationError("product workflow request lacks requirements")
    products_value = requirements.get("products")
    evidence_value = requirements.get("evidence")
    if (
        isinstance(products_value, (str, bytes, bytearray))
        or not isinstance(products_value, Sequence)
        or isinstance(evidence_value, (str, bytes, bytearray))
        or not isinstance(evidence_value, Sequence)
    ):
        raise ReconciliationError("product workflow request bindings are invalid")
    products: dict[str, Mapping[str, Any]] = {}
    for value in products_value:
        if not isinstance(value, Mapping) or frozenset(value) != frozenset(
            {"id", "path", "manifest_sha256"}
        ):
            raise ReconciliationError("product workflow product fields changed")
        product_id = value["id"]
        if (
            not isinstance(product_id, str)
            or STABLE_ID.fullmatch(product_id) is None
            or product_id in products
            or not isinstance(value["manifest_sha256"], str)
            or SHA256.fullmatch(value["manifest_sha256"]) is None
        ):
            raise ReconciliationError("product workflow product identity is invalid")
        products[product_id] = value
    entries = []
    previous = ""
    for value in evidence_value:
        if not isinstance(value, Mapping) or frozenset(value) != frozenset(
            {"product_id", "applicability", "node", "browser"}
        ):
            raise ReconciliationError("product workflow evidence fields changed")
        product_id = value["product_id"]
        if (
            not isinstance(product_id, str)
            or product_id <= previous
            or product_id not in products
        ):
            raise ReconciliationError("product workflow evidence identity is invalid")
        previous = product_id
        applicability = value["applicability"]
        if applicability == "not-applicable":
            continue
        selection = ProductSelectionV1(
            product_id=product_id,
            manifest_sha256=products[product_id]["manifest_sha256"],
            applicability=applicability,
            dependency_product_ids=(),
            node_definition_ids=tuple(value["node"]),
            browser_definition_ids=tuple(value["browser"]),
        )
        entries.append(selection)
    entries.sort(
        key=lambda item: (
            0 if item.applicability == "required" else 1,
            item.product_id,
        )
    )
    enabled = scope.allow_required
    product_work = []
    node_work = []
    browser_work = []
    for selection in entries:
        base_identity = {
            "request_digest": decision.request_digest,
            "product_id": selection.product_id,
            "manifest_sha256": selection.manifest_sha256,
            "applicability": selection.applicability,
        }
        product_work_id = canonical_sha256(
            {**base_identity, "stage": "compose-product"}
        )
        product_work.append(
            {
                "applicability": selection.applicability,
                "product_id": selection.product_id,
                "publication_work_id": canonical_sha256(
                    {**base_identity, "stage": "publish-product-evidence"}
                ),
                "work_id": product_work_id,
            }
        )
        for host, definitions, target in (
            ("node", selection.node_definition_ids, node_work),
            ("browser", selection.browser_definition_ids, browser_work),
        ):
            for definition_id in definitions:
                target.append(
                    {
                        "applicability": selection.applicability,
                        "definition_id": definition_id,
                        "product_id": selection.product_id,
                        "product_work_id": product_work_id,
                        "work_id": canonical_sha256(
                            {
                                **base_identity,
                                "stage": f"{host}-product-evidence",
                                "definition_id": definition_id,
                            }
                        ),
                    }
                )
    result = {
        "schema": 1,
        "kind": "kandelo-abi-staging-product-workflow-seed",
        "mode": activation_mode,
        "authoritative": scope.authoritative,
        "request_digest": decision.request_digest,
        "product_work": product_work,
        "node_work": node_work,
        "browser_work": browser_work,
        "product_matrix": {"include": product_work if enabled else []},
        "node_matrix": {"include": node_work if enabled else []},
        "browser_matrix": {"include": browser_work if enabled else []},
    }
    if len(canonical_sha256(result)) != 64:
        raise AssertionError("canonical product workflow seed digest changed")
    return result


def build_product_workflow_wave(
    request: Mapping[str, Any],
    decision: ReconciliationDecisionV1,
    selections: Sequence[ProductSelectionV1],
    *,
    runtime_bundle_sha256: str,
    progress: Mapping[str, ProductProgressV1],
    activation_mode: str,
) -> dict[str, Any]:
    """Materialize one complete, dependency-ready product wave for a workflow run.

    Host results are current-run artifacts, so an incomplete product is scheduled as
    one bounded compose/publish/evidence transaction.  Durable public candidates and
    aggregate records still decide dependency readiness and completion across runs.
    """

    seed = build_product_workflow_seed(
        request, decision, activation_mode=activation_mode
    )
    checked_selections = tuple(selections)
    seed_products = {item["product_id"]: item for item in seed["product_work"]}
    selected_products = {item.product_id: item for item in checked_selections}
    if set(seed_products) != set(selected_products):
        raise ReconciliationError(
            "product workflow selections differ from the exact request seed"
        )
    for product_id in sorted(selected_products):
        selected = selected_products[product_id]
        seeded = seed_products[product_id]
        seeded_node = tuple(
            item["definition_id"]
            for item in seed["node_work"]
            if item["product_id"] == product_id
        )
        seeded_browser = tuple(
            item["definition_id"]
            for item in seed["browser_work"]
            if item["product_id"] == product_id
        )
        if (
            seeded["applicability"] != selected.applicability
            or seeded_node != selected.node_definition_ids
            or seeded_browser != selected.browser_definition_ids
        ):
            raise ReconciliationError(
                "product workflow selections differ from request evidence authority"
            )

    plan = plan_product_reconciliation(
        decision,
        checked_selections,
        runtime_bundle_sha256=runtime_bundle_sha256,
        progress=progress,
        activation_mode=activation_mode,
    )
    ready_product_ids = {
        item["product_id"]
        for work in (
            plan.composition_work,
            plan.node_work,
            plan.browser_work,
            plan.evidence_publication_work,
        )
        for item in work
    }
    product_work = [
        item for item in seed["product_work"] if item["product_id"] in ready_product_ids
    ]
    node_work = [
        item for item in seed["node_work"] if item["product_id"] in ready_product_ids
    ]
    browser_work = [
        item for item in seed["browser_work"] if item["product_id"] in ready_product_ids
    ]
    blockers = []
    for item in plan.blocked:
        blocker = dict(item)
        blocker["blocked_by"] = list(blocker["blocked_by"])
        blockers.append(blocker)
    result = {
        "schema": 1,
        "kind": "kandelo-abi-staging-product-workflow-wave",
        "mode": activation_mode,
        "authoritative": plan.authoritative,
        "request_digest": decision.request_digest,
        "runtime_bundle_sha256": runtime_bundle_sha256,
        "product_work": product_work,
        "node_work": node_work,
        "browser_work": browser_work,
        "blocked": blockers,
        "complete": list(plan.complete),
        "product_matrix": {"include": product_work},
        "node_matrix": {"include": node_work},
        "browser_matrix": {"include": browser_work},
    }
    if len(canonical_sha256(result)) != 64:
        raise AssertionError("canonical product workflow wave digest changed")
    return result


def _validate_lifecycle(lifecycle: PullRequestLifecycleV1) -> None:
    if lifecycle.state not in {"open", "merged", "closed"}:
        raise ReconciliationError("pull-request lifecycle state is unsupported")
    if lifecycle.current_head is not None and GIT_SHA.fullmatch(lifecycle.current_head) is None:
        raise ReconciliationError("pull-request lifecycle head is not a full lowercase SHA")
    if lifecycle.merged_commit is not None and GIT_SHA.fullmatch(lifecycle.merged_commit) is None:
        raise ReconciliationError("pull-request merge commit is not a full lowercase SHA")
    if lifecycle.state in {"open", "merged"} and lifecycle.current_head is None:
        raise ReconciliationError("open and merged pull requests require a current head")
    if lifecycle.state == "merged" and lifecycle.merged_commit is None:
        raise ReconciliationError("merged pull request requires a merge commit")
    if lifecycle.state != "merged" and lifecycle.merged_commit is not None:
        raise ReconciliationError("only merged pull requests may carry a merge commit")


def load_reconciliation_activation(path: Path) -> str:
    try:
        raw = path.read_bytes()
        value = tomllib.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ReconciliationError(f"reconciliation activation is invalid: {error}") from error
    if not raw or len(raw) > 4096:
        raise ReconciliationError("reconciliation activation size is invalid")
    if set(value) != {"schema", "kind", "mode"}:
        raise ReconciliationError("reconciliation activation fields changed")
    if (
        isinstance(value["schema"], bool)
        or value["schema"] != 1
        or value["kind"] != "kandelo-abi-staging-reconciliation-activation"
        or value["mode"] not in {"observe", "active"}
    ):
        raise ReconciliationError("reconciliation activation is not schema 1")
    return value["mode"]


def load_product_evidence_activation(path: Path) -> str:
    """Load the independent product-evidence rollout switch fail closed."""

    try:
        raw = path.read_bytes()
        value = tomllib.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ReconciliationError(f"product evidence activation is invalid: {error}") from error
    if not raw or len(raw) > 4096:
        raise ReconciliationError("product evidence activation size is invalid")
    if set(value) != {"schema", "kind", "mode"}:
        raise ReconciliationError("product evidence activation fields changed")
    if (
        isinstance(value["schema"], bool)
        or value["schema"] != 1
        or value["kind"] != "kandelo-vfs-product-evidence-activation"
        or value["mode"] not in {"observe", "active"}
    ):
        raise ReconciliationError("product evidence activation is not schema 1")
    return value["mode"]


def reconcile_request(
    discovered: DiscoveredRequestV1,
    lifecycle: PullRequestLifecycleV1,
    *,
    previous_lifecycle: PullRequestLifecycleV1 | None = None,
) -> ReconciliationDecisionV1:
    _validate_lifecycle(lifecycle)
    if previous_lifecycle is not None:
        _validate_lifecycle(previous_lifecycle)
    request_head = discovered.request["build_source"]["commit"]
    if not isinstance(request_head, str) or GIT_SHA.fullmatch(request_head) is None:
        raise ReconciliationError("validated request lost its exact source head")
    current = lifecycle.current_head == request_head
    if lifecycle.state == "closed":
        action = "stop-new-work"
    elif lifecycle.state == "merged":
        action = "observe-merged" if current else "observe-historical"
    elif not current:
        action = "observe-historical"
    elif previous_lifecycle is not None and previous_lifecycle.state == "closed":
        action = "resume-same-head"
    else:
        action = "observe-open"
    return ReconciliationDecisionV1(
        request_digest=discovered.request_digest,
        claim_key=f"sha256:{discovered.request_digest}",
        lifecycle=lifecycle,
        current_for_pull_request=current,
        action=action,
        permitted_work=(),
        blockers=(),
    )


def select_reconciliation_cycle(
    decisions: Sequence[tuple[DiscoveredRequestV1, ReconciliationDecisionV1]],
    *,
    cycle_index: int,
) -> tuple[DiscoveredRequestV1, ReconciliationDecisionV1] | None:
    """Choose one authorized request fairly without timestamp or SHA ordering."""

    if (
        isinstance(cycle_index, bool)
        or not isinstance(cycle_index, int)
        or not 0 <= cycle_index <= 2**63 - 1
    ):
        raise ReconciliationError("reconciliation cycle index is invalid")
    eligible = []
    seen: set[str] = set()
    for discovered, decision in decisions:
        if decision.request_digest != discovered.request_digest:
            raise ReconciliationError(
                "reconciliation decision names a different public request"
            )
        if discovered.request_digest in seen:
            raise ReconciliationError("reconciliation cycle repeats a request digest")
        seen.add(discovered.request_digest)
        if decision.action == "stop-new-work":
            continue
        number = discovered.request["pull_request"]["number"]
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ReconciliationError("reconciliation request lost its pull-request number")
        eligible.append((number, discovered.request_digest, discovered, decision))
    if not eligible:
        return None
    eligible.sort(key=lambda item: (item[0], item[1]))
    selected = eligible[cycle_index % len(eligible)]
    return selected[2], selected[3]
