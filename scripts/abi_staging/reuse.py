"""Protected publication of explicit cross-request candidate reuse bindings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .canonical import canonical_sha256
from .contract import make_candidate_reuse_record
from .execution import ExecutionError, select_reuse_work
from .oci import (
    REUSE_TAG_PREFIX,
    OciTransportV1,
    PublishedRecordLocatorV1,
    publish_immutable_oci_plan,
)
from .policy import TapStagingPolicyV1, candidate_reuse_repository
from .records import build_candidate_reuse_oci_plan, validate_candidate_record
from .verification import validate_verification_receipt_record


class CandidateReuseError(ValueError):
    """Raised when prior public facts cannot authorize exact reuse."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CandidateReuseError(f"{field} must be an object")
    return value


def _record_link(
    digest: str, locator: Mapping[str, Any], *, field: str
) -> dict[str, str]:
    if (
        locator.get("digest") != "sha256:" + digest
        or not isinstance(locator.get("immutable_reference"), str)
        or not locator["immutable_reference"].endswith("@sha256:" + digest)
    ):
        raise CandidateReuseError(f"{field} locator differs from its record")
    return {
        "record_sha256": digest,
        "immutable_reference": locator["immutable_reference"],
    }


def build_candidate_reuse_from_bundle(
    bundle: Mapping[str, Any],
    work_id: str,
    *,
    publication_run: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconstruct one reuse record solely from coordinated immutable facts."""

    try:
        work = select_reuse_work(bundle, work_id)
    except ExecutionError as error:
        raise CandidateReuseError(f"reuse work identity is invalid: {error}") from error
    candidate_digest = work["candidate_record_sha256"]
    candidates = _mapping(bundle.get("candidates"), "coordination candidates")
    candidate_records = _mapping(
        candidates.get("records"), "coordination candidate records"
    )
    candidate_locators = _mapping(
        candidates.get("locators"), "coordination candidate locators"
    )
    candidate = _mapping(
        candidate_records.get(candidate_digest), "reused candidate record"
    )
    try:
        validate_candidate_record(candidate)
    except ValueError as error:
        raise CandidateReuseError(f"reused candidate is invalid: {error}") from error
    candidate_locator = _mapping(
        candidate_locators.get(candidate_digest), "reused candidate locator"
    )
    payload = _mapping(candidate["candidate"], "reused candidate payload")
    formula = _mapping(payload["formula"], "reused candidate Formula")
    source_matches = [
        item["artifact"]
        for item in payload["normalized_components"]
        if item["id"] == "source-custody"
    ]
    if len(source_matches) != 1:
        raise CandidateReuseError("reused candidate lacks one source-custody record")
    source = source_matches[0]

    receipts = _mapping(
        bundle.get("verification_receipts"),
        "coordination verification receipts",
    )
    receipt_records = _mapping(
        receipts.get("records"), "coordination verification receipt records"
    )
    receipt_locators = _mapping(
        receipts.get("locators"), "coordination verification receipt locators"
    )
    qualifying: list[dict[str, str]] = []
    for definition in bundle.get("verification_tests", ()):
        checked_definition = _mapping(definition, "verification definition")
        for host in checked_definition.get("hosts", ()):
            matches: list[tuple[str, Mapping[str, Any]]] = []
            for digest, value in receipt_records.items():
                receipt = _mapping(value, "verification receipt")
                try:
                    validate_verification_receipt_record(receipt)
                except ValueError as error:
                    raise CandidateReuseError(
                        f"verification receipt is invalid: {error}"
                    ) from error
                verification = receipt["verification"]
                common = receipt["common"]
                if (
                    verification["candidate_record_sha256"] == candidate_digest
                    and verification["test_definition_sha256"]
                    == checked_definition["sha256"]
                    and verification["host"] == host
                    and common["outcome"] == "success"
                ):
                    matches.append((str(digest), receipt))
            if not matches:
                raise CandidateReuseError(
                    "reuse work lacks one qualifying receipt for every protected test"
                )
            digest, _receipt = min(matches, key=lambda item: item[0])
            qualifying.append(
                _record_link(
                    digest,
                    _mapping(
                        receipt_locators.get(digest),
                        "verification receipt locator",
                    ),
                    field="verification receipt",
                )
            )
    qualifying.sort(key=lambda item: item["record_sha256"])
    if len(qualifying) != len(
        {item["record_sha256"] for item in qualifying}
    ):
        raise CandidateReuseError(
            "one receipt cannot stand for multiple protected verification identities"
        )

    existing = {
        "schema": 1,
        "kind": "kandelo-existing-candidate",
        "contract_sha256": formula["bottle_contract_sha256"],
        "formula": {
            key: formula[key]
            for key in ("tap", "formula", "architecture", "target_abi")
        },
        "candidate_record": _record_link(
            candidate_digest,
            candidate_locator,
            field="candidate record",
        ),
        "source_custody": {
            "record_sha256": source["sha256"],
            "immutable_reference": source["immutable_reference"],
        },
        "bottle_layer": payload["bottle_layer"],
        "qualifying_receipts": qualifying,
        "original_producer": payload["producer"],
        "nonendorsed": True,
    }
    contract = _mapping(
        _mapping(bundle.get("contracts"), "coordination contracts").get(
            work["subject"]
        ),
        "reuse bottle contract",
    )
    if canonical_sha256(contract) != work["contract_sha256"]:
        raise CandidateReuseError("reuse contract differs from coordinated work")
    try:
        return make_candidate_reuse_record(
            contract,
            work["subject"],
            existing,
            {
                "request_sha256": bundle["request_sha256"],
                "source": bundle["request"]["build_source"],
                "run": dict(publication_run),
            },
        )
    except ValueError as error:
        raise CandidateReuseError(f"cannot construct candidate reuse: {error}") from error


def publish_candidate_reuse(
    bundle: Mapping[str, Any],
    work_id: str,
    *,
    publication_run: Mapping[str, Any],
    policy: TapStagingPolicyV1,
    transport: OciTransportV1,
) -> tuple[dict[str, Any], PublishedRecordLocatorV1]:
    record = build_candidate_reuse_from_bundle(
        bundle, work_id, publication_run=publication_run
    )
    formula = record["candidate_reuse"]["formula"]
    plan = build_candidate_reuse_oci_plan(
        record,
        repository=candidate_reuse_repository(
            policy,
            formula["target_abi"],
            formula=formula["formula"],
        ),
    )
    try:
        locator = publish_immutable_oci_plan(
            plan,
            transport=transport,
            expected_source_repository=policy.tap_repository,
            tag_prefix=REUSE_TAG_PREFIX,
        )
    except ValueError as error:
        raise CandidateReuseError(f"cannot publish candidate reuse: {error}") from error
    return record, locator
