"""Pure pull-request lifecycle decisions for validated staging requests."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal
import tomllib

from .canonical import canonical_sha256
from .github_public import DiscoveredRequestV1


GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


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
