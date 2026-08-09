"""Pure pull-request lifecycle decisions for validated staging requests."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal
import tomllib

from .github_public import DiscoveredRequestV1


GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


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
