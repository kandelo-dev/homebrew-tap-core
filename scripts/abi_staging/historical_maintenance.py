"""Protected historical ABI repair and monotonic epoch-retirement contracts.

Historical maintenance is deliberately separate from overrides.  It authorizes
one exact Formula/architecture on a freshly verified ``abi/N`` ref, then
selects the same uncredentialed build and verification stages used by ordinary
staging.  The only write target for generated Formula metadata is that exact
historical branch.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import copy
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import re
import sys
from typing import Any
import zipfile

from .abi_history import (
    AbiHistoryError,
    GitHubHistoryClient,
    validate_history_plan,
    validate_protection_snapshot,
)
from .canonical import (
    CanonicalJsonError,
    canonical_bytes,
    canonical_sha256,
    parse_canonical_bytes,
)
from .oci import isolated_oras_transport, publish_record
from .override import GitHubMaintenanceClientV1, OverrideError
from .policy import candidate_repository, load_tap_staging_policy
from .records import (
    OciBlobV1,
    OciRecordPlanV1,
    TapRecordError,
    validate_abi_epoch_status,
    validate_abi_history_record,
    validate_historical_maintenance_authorization,
)
from .tap_metadata import load_abi_state


GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
MAX_SUBJECTS = 4096
MAX_LINKS = 4096
MAX_EVIDENCE_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
HISTORICAL_AUTHORIZATION_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.historical-maintenance-authorization.v1+json"
)


class HistoricalMaintenanceError(ValueError):
    """Raised when historical repair broadens or loses protected authority."""


@dataclass(frozen=True)
class HistoricalMaintenanceEvidenceV1:
    target_abi: int
    branch_source: Mapping[str, Any]
    branch_metadata: Mapping[str, Any]
    branch_lineage: Mapping[str, Any] | None
    kandelo_source: Mapping[str, Any]
    formula: Mapping[str, Any]
    reason: str
    policy: Mapping[str, Any]
    history_record: Mapping[str, Any]
    history_record_link: Mapping[str, Any]
    expected_contract_sha256: str
    dependencies: tuple[Mapping[str, Any], ...]
    reuse: Mapping[str, Any] | None


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_plain(child) for child in value]
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalMaintenanceError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise HistoricalMaintenanceError(f"{field} must be an array")
    return value


def _exact(value: Any, fields: frozenset[str], field: str) -> Mapping[str, Any]:
    result = _mapping(value, field)
    if frozenset(result) != fields:
        raise HistoricalMaintenanceError(f"{field} fields changed")
    return result


def _nonnegative(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 2**31 - 1
    ):
        raise HistoricalMaintenanceError(f"{field} is not a bounded integer")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise HistoricalMaintenanceError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise HistoricalMaintenanceError(f"{field} is not a full lowercase Git SHA")
    return value


def _stable_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or STABLE_ID.fullmatch(value) is None:
        raise HistoricalMaintenanceError(f"{field} is not a stable identifier")
    return value


def _repository(value: Any, field: str) -> str:
    if not isinstance(value, str) or REPOSITORY.fullmatch(value) is None:
        raise HistoricalMaintenanceError(f"{field} is not an owner/repository")
    return value


def _architecture(value: Any, field: str) -> str:
    if value not in {"wasm32", "wasm64"}:
        raise HistoricalMaintenanceError(f"{field} is unsupported")
    return str(value)


def _source(value: Any, field: str) -> dict[str, str]:
    source = _exact(value, frozenset({"repository", "commit", "tree"}), field)
    return {
        "repository": _repository(source["repository"], f"{field} repository"),
        "commit": _git_sha(source["commit"], f"{field} commit"),
        "tree": _git_sha(source["tree"], f"{field} tree"),
    }


def _subject(value: Any, field: str) -> dict[str, str]:
    subject = _exact(value, frozenset({"formula", "architecture"}), field)
    return {
        "formula": _stable_id(subject["formula"], f"{field} Formula"),
        "architecture": _architecture(
            subject["architecture"], f"{field} architecture"
        ),
    }


def _formula(value: Any, field: str) -> dict[str, str]:
    formula = _exact(value, frozenset({"tap", "formula", "architecture"}), field)
    return {
        "tap": _repository(formula["tap"], f"{field} tap"),
        "formula": _stable_id(formula["formula"], f"{field} name"),
        "architecture": _architecture(
            formula["architecture"], f"{field} architecture"
        ),
    }


def _record_link(value: Any, field: str) -> dict[str, str]:
    link = _exact(
        value, frozenset({"record_sha256", "immutable_reference"}), field
    )
    digest = _digest(link["record_sha256"], f"{field} digest")
    reference = link["immutable_reference"]
    if (
        not isinstance(reference, str)
        or not 1 <= len(reference.encode("utf-8", errors="strict")) <= 4096
        or f"sha256:{digest}" not in reference
        or any(character.isspace() for character in reference)
    ):
        raise HistoricalMaintenanceError(f"{field} reference does not bind its digest")
    return {"record_sha256": digest, "immutable_reference": reference}


def _history_authority(
    history_record: Mapping[str, Any],
    protection_snapshot: Mapping[str, Any],
    *,
    expected_repository: str,
    current_source: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        validate_abi_history_record(history_record)
        plan = validate_history_plan(_mapping(history_record, "history record")["plan"])
        source = _source(current_source, "current historical branch source")
        if source["repository"].lower() != expected_repository.lower():
            raise HistoricalMaintenanceError(
                "current historical branch source names another repository"
            )
        current_plan = {
            **plan,
            "preactivation_tap_commit": source["commit"],
            "preactivation_tap_tree": source["tree"],
        }
        evidence = validate_protection_snapshot(
            current_plan,
            protection_snapshot,
            phase="postcreate",
            expected_repository=expected_repository,
        )
    except (AbiHistoryError, TapRecordError) as error:
        raise HistoricalMaintenanceError(
            f"historical branch authority is invalid: {error}"
        ) from error
    if (
        evidence["branch"] != plan["branch"]
        or evidence["protection_requirement_sha256"]
        != plan["protection_requirement_sha256"]
    ):
        raise HistoricalMaintenanceError(
            "fresh historical protection differs from the immutable branch authority"
        )
    return dict(plan), evidence


def _branch_lineage(
    value: Mapping[str, Any] | None,
    *,
    plan: Mapping[str, Any],
    current_source: Mapping[str, Any],
) -> None:
    """Bind an advanced abi/N tip to the immutable branch-creation commit.

    The hosted authorizer derives this document from a full-history protected
    checkout after ``git merge-base --is-ancestor`` succeeds.  The pure
    contract requires the exact endpoints so a later stage cannot substitute
    another branch tip or treat lexical SHA ordering as ancestry.
    """

    if current_source["commit"] == plan["preactivation_tap_commit"]:
        if value is not None:
            raise HistoricalMaintenanceError(
                "unchanged historical source cannot carry a lineage assertion"
            )
        return
    lineage = _exact(
        value,
        frozenset({"ancestor", "descendant", "descendant_tree", "relation"}),
        "historical branch lineage",
    )
    if (
        _git_sha(lineage["ancestor"], "historical lineage ancestor")
        != plan["preactivation_tap_commit"]
        or _git_sha(lineage["descendant"], "historical lineage descendant")
        != current_source["commit"]
        or _git_sha(lineage["descendant_tree"], "historical lineage tree")
        != current_source["tree"]
        or lineage["relation"] != "protected-first-parent-descendant"
    ):
        raise HistoricalMaintenanceError(
            "historical branch lineage differs from its immutable origin or current tip"
        )


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise HistoricalMaintenanceError(f"{field} must be bounded text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise HistoricalMaintenanceError(f"{field} is not UTF-8") from error
    if not 1 <= size <= maximum:
        raise HistoricalMaintenanceError(f"{field} is outside its byte bound")
    return value


def load_historical_maintenance_evidence(
    body: bytes,
) -> HistoricalMaintenanceEvidenceV1:
    """Load one protected reconciliation artifact as inert maintenance input."""

    try:
        raw = parse_canonical_bytes(body, maximum_bytes=MAX_EVIDENCE_BYTES)
    except CanonicalJsonError as error:
        raise HistoricalMaintenanceError(
            f"historical maintenance evidence is not canonical: {error}"
        ) from error
    value = _exact(
        raw,
        frozenset(
            {
                "schema",
                "kind",
                "operation",
                "target_abi",
                "branch_source",
                "branch_metadata",
                "branch_lineage",
                "kandelo_source",
                "formula",
                "reason",
                "policy",
                "history_record",
                "history_record_link",
                "expected_contract_sha256",
                "dependencies",
                "reuse",
            }
        ),
        "historical maintenance evidence",
    )
    if (
        value["schema"] != 1
        or value["kind"]
        != "kandelo-abi-staging-historical-maintenance-evidence"
        or value["operation"] != "historical-repair"
    ):
        raise HistoricalMaintenanceError(
            "historical maintenance evidence identity is unsupported"
        )
    target_abi = _nonnegative(value["target_abi"], "historical evidence ABI")
    branch_source = _source(value["branch_source"], "historical evidence source")
    branch_metadata = dict(
        _exact(
            value["branch_metadata"],
            frozenset(
                {
                    "kandelo_abi",
                    "kandelo_repository",
                    "kandelo_commit",
                    "tap_repository",
                    "tap_commit",
                }
            ),
            "historical evidence metadata",
        )
    )
    kandelo_source = _source(
        value["kandelo_source"], "historical evidence Kandelo source"
    )
    formula = _formula(value["formula"], "historical evidence Formula")
    if formula["tap"].lower() != branch_source["repository"].lower():
        raise HistoricalMaintenanceError(
            "historical evidence Formula differs from its tap source"
        )
    reason = value["reason"]
    if reason not in {"failed-package-repair", "security-rebuild"}:
        raise HistoricalMaintenanceError(
            "historical maintenance evidence reason is unsupported"
        )
    policy = dict(_mapping(value["policy"], "historical evidence policy"))
    history_record = dict(
        _mapping(value["history_record"], "historical evidence history record")
    )
    try:
        validate_abi_history_record(history_record)
    except TapRecordError as error:
        raise HistoricalMaintenanceError(
            f"historical evidence history record is invalid: {error}"
        ) from error
    history_record_link = _record_link(
        value["history_record_link"], "historical evidence history record"
    )
    if history_record_link["record_sha256"] != canonical_sha256(history_record):
        raise HistoricalMaintenanceError(
            "historical evidence history locator differs from its exact record"
        )
    contract = _digest(
        value["expected_contract_sha256"], "historical evidence contract"
    )
    dependencies = tuple(
        dict(_mapping(candidate, f"historical evidence dependency {index}"))
        for index, candidate in enumerate(
            _sequence(value["dependencies"], "historical evidence dependencies")
        )
    )
    reuse_value = value["reuse"]
    reuse = (
        None
        if reuse_value is None
        else dict(_mapping(reuse_value, "historical evidence reuse"))
    )
    lineage_value = value["branch_lineage"]
    lineage = (
        None
        if lineage_value is None
        else dict(_mapping(lineage_value, "historical evidence lineage"))
    )
    return HistoricalMaintenanceEvidenceV1(
        target_abi=target_abi,
        branch_source=branch_source,
        branch_metadata=branch_metadata,
        branch_lineage=lineage,
        kandelo_source=kandelo_source,
        formula=formula,
        reason=reason,
        policy=policy,
        history_record=history_record,
        history_record_link=history_record_link,
        expected_contract_sha256=contract,
        dependencies=dependencies,
        reuse=reuse,
    )


def load_historical_maintenance_evidence_archive(
    archive: bytes, *, expected_sha256: str
) -> HistoricalMaintenanceEvidenceV1:
    """Validate one exact Actions ZIP without extracting attacker-owned paths."""

    expected = _digest(expected_sha256, "historical evidence artifact")
    if not 1 <= len(archive) <= MAX_EVIDENCE_ARCHIVE_BYTES:
        raise HistoricalMaintenanceError(
            "historical evidence artifact is outside its byte bound"
        )
    if hashlib.sha256(archive).hexdigest() != expected:
        raise HistoricalMaintenanceError(
            "historical evidence artifact differs from its exact digest"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            if len(entries) != 1:
                raise HistoricalMaintenanceError(
                    "historical evidence artifact must contain one file"
                )
            entry = entries[0]
            if (
                entry.filename != "maintenance-evidence.json"
                or entry.is_dir()
                or not 1 <= entry.file_size <= MAX_EVIDENCE_BYTES
                or entry.compress_size > MAX_EVIDENCE_ARCHIVE_BYTES
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise HistoricalMaintenanceError(
                    "historical evidence artifact inventory is unsafe"
                )
            body = bundle.read(entry)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        if isinstance(error, HistoricalMaintenanceError):
            raise
        raise HistoricalMaintenanceError(
            f"historical evidence artifact is invalid: {error}"
        ) from error
    if len(body) != entry.file_size:
        raise HistoricalMaintenanceError(
            "historical evidence artifact changed while reading"
        )
    return load_historical_maintenance_evidence(body)


def build_historical_authorization_oci_plan(
    record: Mapping[str, Any], *, tap_policy: Any
) -> OciRecordPlanV1:
    """Place authorizations beside one Formula's exact candidate namespace."""

    try:
        validate_historical_maintenance_authorization(record)
    except TapRecordError as error:
        raise HistoricalMaintenanceError(
            f"historical authorization is invalid: {error}"
        ) from error
    formula = _mapping(record["formula"], "historical authorization Formula")
    body = canonical_bytes(record)
    repository = (
        candidate_repository(
            tap_policy,
            record["abi"],
            formula=formula["formula"],
        )
        + "/maintenance/historical-authorizations"
    )
    return OciRecordPlanV1(
        repository=repository,
        artifact_type=HISTORICAL_AUTHORIZATION_MEDIA_TYPE,
        config=OciBlobV1(
            role="historical-maintenance-authorization",
            media_type=HISTORICAL_AUTHORIZATION_MEDIA_TYPE,
            body=body,
            title="historical-maintenance-authorization.json",
        ),
        layers=(
            OciBlobV1(
                role="immutable-record-bytes",
                media_type=HISTORICAL_AUTHORIZATION_MEDIA_TYPE,
                body=body,
                title="historical-maintenance-authorization.json",
            ),
        ),
        annotations={
            "dev.kandelo.abi-staging.classification": "historical-maintenance",
            "dev.kandelo.abi-staging.kind": "historical-maintenance-authorization",
            "dev.kandelo.abi-staging.source-abi": str(record["abi"]),
            "org.opencontainers.image.source": (
                "https://github.com/" + tap_policy.tap_repository
            ),
        },
    )


def _epoch_record(value: Any) -> Mapping[str, Any]:
    wrapper = _mapping(value, "previous ABI epoch status")
    record = wrapper.get("record", wrapper)
    checked = _mapping(record, "previous ABI epoch record")
    try:
        validate_abi_epoch_status(checked)
    except TapRecordError as error:
        raise HistoricalMaintenanceError(
            f"previous ABI epoch status is invalid: {error}"
        ) from error
    return checked


def derive_abi_epoch_status(
    *,
    abi: int,
    scheduled_subjects: Sequence[Mapping[str, Any]],
    terminal_outcomes: Sequence[Mapping[str, Any]],
    successor_activated: bool,
    repair_links: Sequence[Mapping[str, Any]],
    run: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive monotonic active/retiring/retired state from exact terminal facts."""

    version = _nonnegative(abi, "epoch ABI")
    if not isinstance(successor_activated, bool):
        raise HistoricalMaintenanceError("successor activation is not Boolean")
    scheduled = [
        _subject(candidate, f"scheduled subject {index}")
        for index, candidate in enumerate(
            _sequence(scheduled_subjects, "scheduled subjects")
        )
    ]
    scheduled_keys = [
        (candidate["formula"], candidate["architecture"]) for candidate in scheduled
    ]
    if (
        not 1 <= len(scheduled) <= MAX_SUBJECTS
        or scheduled_keys != sorted(set(scheduled_keys))
    ):
        raise HistoricalMaintenanceError(
            "scheduled epoch subjects must be sorted and duplicate-free"
        )

    outcomes = []
    terminal_keys = []
    for index, candidate in enumerate(
        _sequence(terminal_outcomes, "terminal outcomes")
    ):
        outcome = _exact(
            candidate,
            frozenset({"subject", "outcome", "record"}),
            f"terminal outcome {index}",
        )
        subject = _subject(outcome["subject"], f"terminal outcome {index} subject")
        disposition = outcome["outcome"]
        if disposition not in {"success", "failure", "timeout", "canceled"}:
            raise HistoricalMaintenanceError("epoch outcome is not terminal")
        key = (subject["formula"], subject["architecture"])
        terminal_keys.append(key)
        outcomes.append(
            {
                "subject": subject,
                "outcome": disposition,
                "record": _record_link(
                    outcome["record"], f"terminal outcome {index} record"
                ),
            }
        )
    if (
        terminal_keys != sorted(set(terminal_keys))
        or not set(terminal_keys).issubset(scheduled_keys)
    ):
        raise HistoricalMaintenanceError(
            "terminal epoch subjects differ from the fixed schedule"
        )

    checked_repairs = [
        _record_link(candidate, f"repair link {index}")
        for index, candidate in enumerate(_sequence(repair_links, "repair links"))
    ]
    repair_digests = [candidate["record_sha256"] for candidate in checked_repairs]
    if (
        len(checked_repairs) > MAX_LINKS
        or repair_digests != sorted(set(repair_digests))
    ):
        raise HistoricalMaintenanceError(
            "repair links must be sorted and duplicate-free"
        )

    if previous is not None:
        prior = _epoch_record(previous)
        if prior["abi"] != version or prior["scheduled_subjects"] != scheduled:
            raise HistoricalMaintenanceError("epoch schedule cannot change after issuance")
        prior_outcomes = {
            (
                candidate["subject"]["formula"],
                candidate["subject"]["architecture"],
            ): candidate
            for candidate in prior["terminal_outcomes"]
        }
        current_outcomes = {
            (candidate["subject"]["formula"], candidate["subject"]["architecture"]): candidate
            for candidate in outcomes
        }
        if any(current_outcomes.get(key) != value for key, value in prior_outcomes.items()):
            raise HistoricalMaintenanceError(
                "repair or reopen cannot erase terminal epoch history"
            )
        prior_repairs = {
            candidate["record_sha256"]: candidate for candidate in prior["repair_links"]
        }
        current_repairs = {
            candidate["record_sha256"]: candidate for candidate in checked_repairs
        }
        if any(current_repairs.get(key) != value for key, value in prior_repairs.items()):
            raise HistoricalMaintenanceError("repair history cannot be removed or changed")
        if prior["state"] != "active" and not successor_activated:
            raise HistoricalMaintenanceError("a historical epoch cannot become active again")

    if not successor_activated:
        state = "active"
    elif terminal_keys == scheduled_keys:
        state = "retired"
    else:
        state = "retiring"
    record = {
        "schema": 1,
        "kind": "kandelo-abi-epoch-status",
        "abi": version,
        "scheduled_subjects": scheduled,
        "terminal_outcomes": outcomes,
        "state": state,
        "repair_links": checked_repairs,
        "run": _plain(run),
    }
    try:
        validate_abi_epoch_status(record)
    except TapRecordError as error:
        raise HistoricalMaintenanceError(f"derived ABI epoch is invalid: {error}") from error
    return {"record": record, "state": state, "gates_successor": False}


def authorize_historical_maintenance(
    *,
    target_abi: int,
    current_abi: int,
    branch_source: Mapping[str, Any],
    branch_metadata: Mapping[str, Any],
    kandelo_source: Mapping[str, Any],
    formula: Mapping[str, Any],
    reason: str,
    maintainer: Mapping[str, Any],
    policy: Mapping[str, Any],
    history_record: Mapping[str, Any],
    history_record_link: Mapping[str, Any],
    protection_snapshot: Mapping[str, Any],
    run: Mapping[str, Any],
    branch_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Authorize one exact repair only after fresh historical ref/protection checks."""

    abi = _nonnegative(target_abi, "historical target ABI")
    current = _nonnegative(current_abi, "current ABI")
    if current <= abi:
        raise HistoricalMaintenanceError(
            "historical maintenance cannot target current or future ABI metadata"
        )
    source = _source(branch_source, "historical branch source")
    tap_repository = _repository(source["repository"], "historical tap")
    plan, _evidence = _history_authority(
        history_record,
        protection_snapshot,
        expected_repository=tap_repository,
        current_source=source,
    )
    if plan["source_abi"] != abi or plan["branch"] != f"abi/{abi}":
        raise HistoricalMaintenanceError(
            "historical maintenance source differs from protected abi/N"
        )
    _branch_lineage(branch_lineage, plan=plan, current_source=source)

    metadata = _exact(
        branch_metadata,
        frozenset(
            {
                "kandelo_abi",
                "kandelo_repository",
                "kandelo_commit",
                "tap_repository",
                "tap_commit",
            }
        ),
        "historical branch metadata",
    )
    exact_kandelo = _source(kandelo_source, "historical Kandelo source")
    if (
        _nonnegative(metadata["kandelo_abi"], "historical metadata ABI") != abi
        or _repository(metadata["tap_repository"], "historical metadata tap")
        != tap_repository
        or _git_sha(metadata["tap_commit"], "historical metadata tap commit")
        != plan["preactivation_tap_commit"]
        or _repository(
            metadata["kandelo_repository"], "historical metadata Kandelo repository"
        )
        != exact_kandelo["repository"]
        or _git_sha(
            metadata["kandelo_commit"], "historical metadata Kandelo commit"
        )
        != exact_kandelo["commit"]
    ):
        raise HistoricalMaintenanceError(
            "historical metadata differs from exact tap or Kandelo source"
        )
    checked_formula = _formula(formula, "historical repair Formula")
    if checked_formula["tap"] != tap_repository:
        raise HistoricalMaintenanceError(
            "historical repair Formula names another tap"
        )
    if reason not in {"failed-package-repair", "security-rebuild"}:
        raise HistoricalMaintenanceError("historical repair reason is unsupported")
    link = _record_link(history_record_link, "historical repair history record")
    expected_history_reference = (
        f"ghcr.io/{tap_repository.lower()}-abi-{abi}-records/history@sha256:"
        + link["record_sha256"]
    )
    if (
        link["record_sha256"] != canonical_sha256(history_record)
        or link["immutable_reference"] != expected_history_reference
    ):
        raise HistoricalMaintenanceError(
            "historical repair history locator differs from its exact record"
        )
    record = {
        "schema": 1,
        "kind": "kandelo-abi-historical-maintenance-authorization",
        "abi": abi,
        "branch": f"abi/{abi}",
        "source": source,
        "formula": checked_formula,
        "reason": reason,
        "maintainer": _plain(maintainer),
        "policy": _plain(policy),
        "history_record": link,
        "run": _plain(run),
    }
    try:
        validate_historical_maintenance_authorization(record)
    except TapRecordError as error:
        raise HistoricalMaintenanceError(
            f"historical maintenance authorization is invalid: {error}"
        ) from error
    return record


def _candidate_repository(tap_repository: str, abi: int, formula: str) -> str:
    owner, tap = tap_repository.lower().split("/", 1)
    return f"ghcr.io/{owner}/{tap}-abi-{abi}-candidates/{formula}"


def _canonical_repository(tap_repository: str, abi: int, formula: str) -> str:
    owner, tap = tap_repository.lower().split("/", 1)
    return f"ghcr.io/{owner}/{tap}-abi-{abi}/{formula}"


def _repair_stages(*, build_required: bool) -> list[str]:
    return [
        "build-uncredentialed" if build_required else "reuse-exact-candidate",
        (
            "publish-candidate-protected"
            if build_required
            else "publish-reuse-protected"
        ),
        "verify-uncredentialed",
        "publish-receipt-protected",
        "publish-canonical-protected",
        "update-historical-metadata-protected",
        "publish-admission-protected",
    ]


def validate_historical_repair_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the exact single-subject lane before any job uses it."""

    value = _exact(
        plan,
        frozenset(
            {
                "schema",
                "kind",
                "work_id",
                "identity",
                "metadata_branch",
                "candidate_repository",
                "canonical_repository",
                "build_required",
                "stages",
                "override_receipts",
                "preserve_prior_records",
            }
        ),
        "historical repair plan",
    )
    identity = _exact(
        value["identity"],
        frozenset(
            {
                "authorization_sha256",
                "history_record_sha256",
                "abi",
                "branch",
                "source",
                "formula",
                "reason",
                "expected_contract_sha256",
                "dependencies",
                "reuse_record_sha256",
            }
        ),
        "historical repair identity",
    )
    abi = _nonnegative(identity["abi"], "historical repair ABI")
    source = _source(identity["source"], "historical repair source")
    formula = _formula(identity["formula"], "historical repair Formula")
    if formula["tap"].lower() != source["repository"].lower():
        raise HistoricalMaintenanceError(
            "historical repair Formula differs from its tap source"
        )
    branch = identity["branch"]
    if branch != f"abi/{abi}" or value["metadata_branch"] != branch:
        raise HistoricalMaintenanceError(
            "historical repair metadata target is not exact abi/N"
        )
    _digest(identity["authorization_sha256"], "repair authorization")
    _digest(identity["history_record_sha256"], "repair history record")
    _digest(identity["expected_contract_sha256"], "repair contract")
    if identity["reason"] not in {"failed-package-repair", "security-rebuild"}:
        raise HistoricalMaintenanceError("historical repair reason is unsupported")
    previous = ""
    for index, candidate in enumerate(
        _sequence(identity["dependencies"], "repair dependencies")
    ):
        dependency = _exact(
            candidate,
            frozenset(
                {"formula", "architecture", "target_abi", "contract_sha256"}
            ),
            f"repair dependency {index}",
        )
        name = _stable_id(dependency["formula"], f"repair dependency {index}")
        architecture = _architecture(
            dependency["architecture"], f"repair dependency {index} architecture"
        )
        subject = f"{name}:{architecture}"
        if subject <= previous:
            raise HistoricalMaintenanceError(
                "historical dependencies are not sorted and unique"
            )
        previous = subject
        if _nonnegative(dependency["target_abi"], "repair dependency ABI") != abi:
            raise HistoricalMaintenanceError(
                "historical repair cannot consume a cross-ABI dependency"
            )
        _digest(
            dependency["contract_sha256"],
            f"repair dependency {index} contract",
        )
    reuse_digest = identity["reuse_record_sha256"]
    if reuse_digest is not None:
        _digest(reuse_digest, "historical candidate reuse")
    build_required = value["build_required"]
    if not isinstance(build_required, bool) or build_required != (reuse_digest is None):
        raise HistoricalMaintenanceError(
            "historical repair build choice differs from its reuse identity"
        )
    if identity["reason"] == "security-rebuild" and not build_required:
        raise HistoricalMaintenanceError(
            "security rebuild cannot reuse an existing candidate"
        )
    expected_candidate = _candidate_repository(
        source["repository"], abi, formula["formula"]
    )
    expected_canonical = _canonical_repository(
        source["repository"], abi, formula["formula"]
    )
    if (
        value["candidate_repository"] != expected_candidate
        or value["canonical_repository"] != expected_canonical
    ):
        raise HistoricalMaintenanceError(
            "historical repair repository escaped the exact ABI namespace"
        )
    if (
        value["schema"] != 1
        or value["kind"] != "kandelo-abi-historical-repair-plan"
        or value["work_id"] != canonical_sha256(identity)
        or value["stages"] != _repair_stages(build_required=build_required)
        or value["override_receipts"] != []
        or value["preserve_prior_records"] is not True
    ):
        raise HistoricalMaintenanceError("historical repair plan identity changed")
    return copy.deepcopy(dict(value))


def build_historical_repair_plan(
    *,
    authorization: Mapping[str, Any],
    authorization_sha256: str,
    history_record: Mapping[str, Any],
    protection_snapshot: Mapping[str, Any],
    expected_contract_sha256: str,
    dependencies: Sequence[Mapping[str, Any]],
    reuse: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Select the closed normal staging lane for one authorized historical repair."""

    try:
        validate_historical_maintenance_authorization(authorization)
    except TapRecordError as error:
        raise HistoricalMaintenanceError(
            f"historical authorization is invalid: {error}"
        ) from error
    auth = _mapping(authorization, "historical authorization")
    authorization_digest = _digest(
        authorization_sha256, "historical authorization"
    )
    if authorization_digest != canonical_sha256(auth):
        raise HistoricalMaintenanceError(
            "historical authorization digest differs from its exact bytes"
        )
    abi = _nonnegative(auth["abi"], "historical repair ABI")
    source = _source(auth["source"], "historical repair source")
    plan, _evidence = _history_authority(
        history_record,
        protection_snapshot,
        expected_repository=source["repository"],
        current_source=source,
    )
    history_link = _record_link(auth["history_record"], "authorization history")
    if (
        history_link["record_sha256"] != canonical_sha256(history_record)
        or plan["source_abi"] != abi
        or plan["branch"] != auth["branch"]
    ):
        raise HistoricalMaintenanceError(
            "repair planning history differs from its authorization"
        )
    checked_formula = _formula(auth["formula"], "authorized historical Formula")
    contract = _digest(expected_contract_sha256, "historical repair contract")

    checked_dependencies = []
    previous = ""
    for index, candidate in enumerate(_sequence(dependencies, "repair dependencies")):
        dependency = _exact(
            candidate,
            frozenset(
                {"formula", "architecture", "target_abi", "contract_sha256"}
            ),
            f"repair dependency {index}",
        )
        formula = _stable_id(dependency["formula"], f"repair dependency {index}")
        architecture = _architecture(
            dependency["architecture"], f"repair dependency {index} architecture"
        )
        subject = f"{formula}:{architecture}"
        if subject <= previous:
            raise HistoricalMaintenanceError(
                "historical dependencies are not sorted and unique"
            )
        previous = subject
        if _nonnegative(dependency["target_abi"], "repair dependency ABI") != abi:
            raise HistoricalMaintenanceError(
                "historical repair cannot consume a cross-ABI dependency"
            )
        checked_dependencies.append(
            {
                "formula": formula,
                "architecture": architecture,
                "target_abi": abi,
                "contract_sha256": _digest(
                    dependency["contract_sha256"],
                    f"repair dependency {index} contract",
                ),
            }
        )

    candidate_repository = _candidate_repository(
        source["repository"], abi, checked_formula["formula"]
    )
    canonical_repository = _canonical_repository(
        source["repository"], abi, checked_formula["formula"]
    )
    reuse_digest = None
    if reuse is not None:
        checked_reuse = _exact(
            reuse,
            frozenset(
                {
                    "record_sha256",
                    "immutable_reference",
                    "formula",
                    "architecture",
                    "target_abi",
                    "contract_sha256",
                    "candidate_repository",
                }
            ),
            "historical candidate reuse",
        )
        if (
            _stable_id(checked_reuse["formula"], "reuse Formula")
            != checked_formula["formula"]
            or _architecture(checked_reuse["architecture"], "reuse architecture")
            != checked_formula["architecture"]
            or _nonnegative(checked_reuse["target_abi"], "reuse ABI") != abi
            or _digest(checked_reuse["contract_sha256"], "reuse contract")
            != contract
            or checked_reuse["candidate_repository"] != candidate_repository
        ):
            raise HistoricalMaintenanceError(
                "historical candidate reuse differs from the exact ABI contract"
            )
        reuse_link = _record_link(
            {
                "record_sha256": checked_reuse["record_sha256"],
                "immutable_reference": checked_reuse["immutable_reference"],
            },
            "historical candidate reuse",
        )
        if reuse_link["immutable_reference"] != (
            candidate_repository + "@sha256:" + reuse_link["record_sha256"]
        ):
            raise HistoricalMaintenanceError(
                "historical reuse locator differs from the exact candidate namespace"
            )
        reuse_digest = reuse_link["record_sha256"]
        if auth["reason"] == "security-rebuild":
            raise HistoricalMaintenanceError(
                "security rebuild must create a new attempt and candidate"
            )

    identity = {
        "authorization_sha256": authorization_digest,
        "history_record_sha256": history_link["record_sha256"],
        "abi": abi,
        "branch": auth["branch"],
        "source": source,
        "formula": checked_formula,
        "reason": auth["reason"],
        "expected_contract_sha256": contract,
        "dependencies": checked_dependencies,
        "reuse_record_sha256": reuse_digest,
    }
    result = {
        "schema": 1,
        "kind": "kandelo-abi-historical-repair-plan",
        "work_id": canonical_sha256(identity),
        "identity": identity,
        "metadata_branch": auth["branch"],
        "candidate_repository": candidate_repository,
        "canonical_repository": canonical_repository,
        "build_required": reuse_digest is None,
        "stages": _repair_stages(build_required=reuse_digest is None),
        "override_receipts": [],
        "preserve_prior_records": True,
    }
    return validate_historical_repair_plan(result)


def validate_historical_repair_completion(
    plan: Mapping[str, Any], completion: Mapping[str, Any]
) -> dict[str, Any]:
    """Require new immutable records and exact abi/N metadata without erasure."""

    checked_plan = validate_historical_repair_plan(plan)
    result = _exact(
        completion,
        frozenset(
            {
                "authorization_sha256",
                "attempt_records",
                "candidate_record",
                "verification_receipts",
                "admission_record",
                "override_receipts",
                "deleted_record_sha256s",
                "prior_record_sha256s",
                "preserved_prior_record_sha256s",
            }
        ),
        "historical repair completion",
    )
    identity = _mapping(checked_plan["identity"], "historical repair identity")
    if (
        _digest(result["authorization_sha256"], "completion authorization")
        != identity["authorization_sha256"]
        or result["override_receipts"] != []
        or result["deleted_record_sha256s"] != []
    ):
        raise HistoricalMaintenanceError(
            "historical repair completion used override or deletion authority"
        )
    attempts = [
        _record_link(candidate, f"repair attempt {index}")
        for index, candidate in enumerate(
            _sequence(result["attempt_records"], "repair attempts")
        )
    ]
    if not attempts:
        raise HistoricalMaintenanceError("historical repair has no new attempt record")
    attempt_digests = [candidate["record_sha256"] for candidate in attempts]
    if attempt_digests != sorted(set(attempt_digests)):
        raise HistoricalMaintenanceError(
            "historical repair attempts must be sorted and duplicate-free"
        )
    expected_attempt_prefix = checked_plan["candidate_repository"] + "/attempts@sha256:"
    if any(
        candidate["immutable_reference"]
        != expected_attempt_prefix + candidate["record_sha256"]
        for candidate in attempts
    ):
        raise HistoricalMaintenanceError(
            "historical repair attempt escaped the exact candidate namespace"
        )
    receipts = [
        _record_link(candidate, f"repair receipt {index}")
        for index, candidate in enumerate(
            _sequence(result["verification_receipts"], "repair receipts")
        )
    ]
    if not receipts:
        raise HistoricalMaintenanceError(
            "historical repair has no new verification receipt"
        )
    receipt_digests = [candidate["record_sha256"] for candidate in receipts]
    if receipt_digests != sorted(set(receipt_digests)):
        raise HistoricalMaintenanceError(
            "historical repair receipts must be sorted and duplicate-free"
        )
    expected_receipt_prefix = checked_plan["candidate_repository"] + "/receipts/"
    if any(
        not candidate["immutable_reference"].startswith(expected_receipt_prefix)
        or "/overrides@" in candidate["immutable_reference"]
        for candidate in receipts
    ):
        raise HistoricalMaintenanceError(
            "historical repair receipt escaped verification or used override authority"
        )
    candidate = _exact(
        result["candidate_record"],
        frozenset(
            {
                "record_sha256",
                "immutable_reference",
                "repository",
                "formula",
                "architecture",
                "target_abi",
                "contract_sha256",
            }
        ),
        "historical candidate record",
    )
    candidate_link = _record_link(
        {
            "record_sha256": candidate["record_sha256"],
            "immutable_reference": candidate["immutable_reference"],
        },
        "historical candidate record",
    )
    formula = _mapping(identity["formula"], "historical repair Formula")
    abi = _nonnegative(identity["abi"], "historical repair ABI")
    if (
        candidate["repository"] != checked_plan["candidate_repository"]
        or candidate_link["immutable_reference"]
        != checked_plan["candidate_repository"]
        + "@sha256:"
        + candidate_link["record_sha256"]
        or candidate["formula"] != formula["formula"]
        or candidate["architecture"] != formula["architecture"]
        or candidate["target_abi"] != abi
        or candidate["contract_sha256"] != identity["expected_contract_sha256"]
    ):
        raise HistoricalMaintenanceError(
            "historical candidate differs from the exact repair plan"
        )
    admission = _exact(
        result["admission_record"],
        frozenset(
            {
                "record_sha256",
                "immutable_reference",
                "repository",
                "branch",
                "candidate_record_sha256",
                "target_abi",
            }
        ),
        "historical admission record",
    )
    admission_link = _record_link(
        {
            "record_sha256": admission["record_sha256"],
            "immutable_reference": admission["immutable_reference"],
        },
        "historical admission record",
    )
    if (
        admission["repository"] != checked_plan["canonical_repository"]
        or admission_link["immutable_reference"]
        != checked_plan["canonical_repository"]
        + "/admissions@sha256:"
        + admission_link["record_sha256"]
        or admission["branch"] != checked_plan["metadata_branch"]
        or admission["branch"] == "main"
        or admission["candidate_record_sha256"]
        != candidate_link["record_sha256"]
        or admission["target_abi"] != abi
    ):
        raise HistoricalMaintenanceError(
            "historical admission differs from exact abi/N metadata"
        )
    prior = [
        _digest(candidate, f"prior record {index}")
        for index, candidate in enumerate(
            _sequence(result["prior_record_sha256s"], "prior records")
        )
    ]
    preserved = [
        _digest(candidate, f"preserved record {index}")
        for index, candidate in enumerate(
            _sequence(result["preserved_prior_record_sha256s"], "preserved records")
        )
    ]
    if prior != sorted(set(prior)) or preserved != prior:
        raise HistoricalMaintenanceError(
            "historical repair removed or changed prior immutable records"
        )
    new_digests = {
        *attempt_digests,
        candidate_link["record_sha256"],
        *receipt_digests,
        admission_link["record_sha256"],
    }
    if new_digests.intersection(prior):
        raise HistoricalMaintenanceError(
            "historical repair reused a prior immutable record as new work"
        )
    return copy.deepcopy(dict(result))


def _positive_environment(name: str) -> int:
    raw = os.environ.get(name, "")
    if not raw.isdigit():
        raise HistoricalMaintenanceError(f"{name} is not a positive integer")
    value = int(raw)
    if not 1 <= value <= 2**63 - 1:
        raise HistoricalMaintenanceError(f"{name} is outside its bound")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.abi_staging.historical_maintenance"
    )
    subcommands = parser.add_subparsers(dest="operation", required=True)
    authorize = subcommands.add_parser("authorize")
    authorize.add_argument("--evidence-artifact-id", required=True, type=int)
    authorize.add_argument("--evidence-sha256", required=True)
    authorize.add_argument("--justification", required=True)
    authorize.add_argument("--actor", required=True)
    authorize.add_argument("--authorization-reference", required=True)
    authorize.add_argument("--verify-actor-permission", action="store_true")
    authorize.add_argument("--fresh-protection", action="store_true")
    authorize.add_argument("--require-history-record", action="store_true")
    authorize.add_argument("--preserve-prior-records", action="store_true")
    authorize.add_argument("--immutable", action="store_true")
    authorize.add_argument("--out", required=True)
    return parser


def _run_cli(args: argparse.Namespace) -> dict[str, Any]:
    if not (
        args.verify_actor_permission
        and args.fresh_protection
        and args.require_history_record
        and args.preserve_prior_records
        and args.immutable
    ):
        raise HistoricalMaintenanceError(
            "historical maintenance requires permission, history, fresh protection, "
            "record preservation, and immutability"
        )
    justification = _text(args.justification, "historical justification", 2048)
    if not justification.strip():
        raise HistoricalMaintenanceError(
            "historical maintenance justification cannot be blank"
        )
    tap_root = Path(__file__).resolve().parents[2]
    tap_policy = load_tap_staging_policy(
        tap_root / "Kandelo/staging/tap-policy.toml"
    )
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if repository != tap_policy.tap_repository:
        raise HistoricalMaintenanceError(
            "historical maintenance repository differs from tap policy"
        )
    token = os.environ.get("GITHUB_TOKEN", "")
    client = GitHubMaintenanceClientV1(repository, token)
    archive = client.evidence_artifact(
        args.evidence_artifact_id,
        expected_sha256=args.evidence_sha256,
    )
    evidence = load_historical_maintenance_evidence_archive(
        archive, expected_sha256=args.evidence_sha256
    )
    run_id = _positive_environment("GITHUB_RUN_ID")
    run_attempt = _positive_environment("GITHUB_RUN_ATTEMPT")
    expected_reference = (
        f"https://github.com/{repository}/actions/runs/{run_id}/"
        f"attempts/{run_attempt}"
    )
    if args.authorization_reference != expected_reference:
        raise HistoricalMaintenanceError(
            "historical authorization reference differs from current run"
        )
    maintainer = client.maintainer(args.actor, args.authorization_reference)
    current_abi = load_abi_state(tap_root / "Kandelo/abi-state.json").current_abi
    branch = f"abi/{evidence.target_abi}"
    snapshot = GitHubHistoryClient(repository, token).protection_snapshot(
        branch, phase="postcreate"
    )
    run = {
        "repository": repository,
        "workflow_ref": (
            ".github/workflows/abi-staging-maintenance.yml@refs/heads/main"
        ),
        "run_id": run_id,
        "run_attempt": run_attempt,
        "job": "authorize-historical-repair",
    }
    authorization = authorize_historical_maintenance(
        target_abi=evidence.target_abi,
        current_abi=current_abi,
        branch_source=evidence.branch_source,
        branch_metadata=evidence.branch_metadata,
        kandelo_source=evidence.kandelo_source,
        formula=evidence.formula,
        reason=evidence.reason,
        maintainer=maintainer,
        policy=evidence.policy,
        history_record=evidence.history_record,
        history_record_link=evidence.history_record_link,
        protection_snapshot=snapshot,
        run=run,
        branch_lineage=evidence.branch_lineage,
    )
    authorization_sha256 = canonical_sha256(authorization)
    repair_plan = build_historical_repair_plan(
        authorization=authorization,
        authorization_sha256=authorization_sha256,
        history_record=evidence.history_record,
        protection_snapshot=snapshot,
        expected_contract_sha256=evidence.expected_contract_sha256,
        dependencies=evidence.dependencies,
        reuse=evidence.reuse,
    )
    from .reconcile import historical_maintenance_work_scope

    scope = historical_maintenance_work_scope(repair_plan)
    publication = build_historical_authorization_oci_plan(
        authorization, tap_policy=tap_policy
    )
    username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
    package_token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
    with isolated_oras_transport(
        username=username, token=package_token
    ) as transport:
        locator = publish_record(
            publication,
            transport=transport,
            expected_source_repository=tap_policy.tap_repository,
        )
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-historical-maintenance-result",
        "operation": "historical-repair",
        "justification_sha256": hashlib.sha256(
            justification.encode("utf-8")
        ).hexdigest(),
        "authorization": authorization,
        "authorization_sha256": authorization_sha256,
        "repair_plan": repair_plan,
        "work_scope": {
            "work_id": scope.work_id,
            "authorization_sha256": scope.authorization_sha256,
            "formula_subject": scope.formula_subject,
            "target_abi": scope.target_abi,
            "tap_source": dict(scope.tap_source),
            "metadata_branch": scope.metadata_branch,
            "build_action": scope.build_action,
            "permitted_stages": list(scope.permitted_stages),
            "gates_successor": scope.gates_successor,
        },
        "published": {
            "repository": locator.repository,
            "digest": locator.digest,
            "immutable_reference": locator.immutable_reference,
            "anonymous_readback_sha256": locator.anonymous_readback_sha256,
        },
    }


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(arguments)
        result = _run_cli(args)
        destination = Path(args.out)
        if destination.exists() or destination.is_symlink():
            raise HistoricalMaintenanceError(
                "historical maintenance output already exists"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(canonical_bytes(result))
        return 0
    except (HistoricalMaintenanceError, OverrideError, ValueError, OSError) as error:
        print(f"abi-staging historical maintenance: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
