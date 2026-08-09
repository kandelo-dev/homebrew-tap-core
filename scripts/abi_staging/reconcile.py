"""Pure pull-request lifecycle decisions for validated staging requests."""

from __future__ import annotations

from dataclasses import dataclass
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
        "observe-merged",
        "stop-new-work",
        "resume-same-head",
        "await-new-request",
    ]
    permitted_work: tuple[str, ...]
    blockers: tuple[MappingProxyType[str, Any], ...]


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
        or value["mode"] != "observe"
    ):
        raise ReconciliationError("reconciliation activation is not observe schema 1")
    return "observe"


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
        action = "observe-merged" if current else "stop-new-work"
    elif not current:
        action = "await-new-request"
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
