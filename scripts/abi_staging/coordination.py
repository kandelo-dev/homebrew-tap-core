"""Protected contract preparation for one exact reconciliation cycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .canonical import canonical_bytes, canonical_sha256
from .contract import (
    assess_capture,
    build_bottle_contract,
    validate_capture_assessment,
    validate_candidate_reuse_record,
)
from .inventory import PublicSchedulingInventoryV1
from .oci import OciPublicationError, parse_public_record_locator
from .plan import exact_formula_subject, validate_tap_plan
from .policy import (
    TapStagingPolicyV1,
    VerificationTestDefinitionV1,
    generate_formula_capture_catalog,
    load_formula_build_inputs,
)
from .reconcile import ReconciliationDecisionV1
from .records import validate_candidate_record
from .scheduler import CandidateFactV1, schedule_ready_batch
from .workflow import build_workflow_manifest, validate_workflow_manifest
from .verification import validate_verification_receipt_record


SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CoordinationError(ValueError):
    """Raised when exact build inputs cannot form one bottle contract."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CoordinationError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CoordinationError(f"{field} must be an array")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise CoordinationError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _capture_entry(tap_root: Path, name: str, architecture: str) -> Mapping[str, Any]:
    policy = load_formula_build_inputs(
        tap_root / "Kandelo/staging/formula-build-inputs.toml", tap_root=tap_root
    )
    catalog = generate_formula_capture_catalog(tap_root, policy)
    matches = [entry for entry in catalog["formulae"] if entry["name"] == name]
    if len(matches) != 1 or architecture not in matches[0]["architectures"]:
        raise CoordinationError("Formula capture policy lacks the exact subject")
    return matches[0]


def _repository_inputs(
    captured: Sequence[Mapping[str, Any]], repository: str
) -> list[dict[str, str]]:
    result = [
        {
            "id": entry["id"],
            "kind": entry["kind"],
            "path": entry["path"],
            "sha256": entry["sha256"],
        }
        for entry in captured
        if entry["repository"] == repository
    ]
    result.sort(key=lambda item: item["id"])
    return result


def _component(
    name: str,
    *,
    capture_policy_sha256: str,
    kandelo_inputs: Sequence[Mapping[str, Any]],
    tap_inputs: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    policy = {
        "schema": 1,
        "kind": "kandelo-bottle-contract-component-policy",
        "component": name,
        "capture_policy_sha256": capture_policy_sha256,
    }
    identity = {
        "schema": 1,
        "kind": "kandelo-bottle-contract-component",
        "component": name,
        "kandelo_inputs": list(kandelo_inputs),
        "tap_inputs": list(tap_inputs),
    }
    return {
        "policy_sha256": canonical_sha256(policy),
        "component_sha256": canonical_sha256(identity),
    }


def _candidate_dependency(
    candidate: Mapping[str, Any],
    *,
    expected_request: str,
    expected_subject: str,
    expected_formula: str,
    expected_architecture: str,
    materialization_policy_sha256: str,
) -> dict[str, Any]:
    value = _mapping(candidate, f"candidate dependency {expected_formula}")
    if value.get("request_sha256") != expected_request:
        raise CoordinationError("dependency candidate names a different request")
    if value.get("subject") != expected_subject:
        raise CoordinationError("dependency candidate names a different exact subject")
    _digest(value.get("record_sha256"), "dependency candidate record")
    _digest(value.get("contract_sha256"), "dependency candidate contract")
    layer = _mapping(value.get("bottle_layer"), "dependency candidate layer")
    layer_digest = _digest(layer.get("sha256"), "dependency candidate layer")
    size = layer.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 2**32:
        raise CoordinationError("dependency candidate layer size is invalid")
    return {
        "formula": expected_formula,
        "architecture": expected_architecture,
        "bottle_layer_sha256": layer_digest,
        "bottle_layer_bytes": size,
        "materialization_policy_sha256": _digest(
            materialization_policy_sha256,
            "dependency materialization policy",
        ),
    }


def build_formula_contract(
    *,
    tap_root: Path,
    kandelo_root: Path,
    tap_plan: Mapping[str, Any],
    formula_plan: Mapping[str, Any],
    dependency_candidates: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive a contract only from protected policy and exact checked-out bytes."""

    validate_tap_plan(tap_plan)
    root = tap_root.resolve(strict=True)
    kandelo = kandelo_root.resolve(strict=True)
    if root != tap_root.resolve() or kandelo != kandelo_root.resolve():
        raise CoordinationError("contract roots must be exact real paths")
    identity = _mapping(formula_plan.get("identity"), "Formula identity")
    name = identity.get("name")
    architecture = identity.get("architecture")
    if not isinstance(name, str) or not isinstance(architecture, str):
        raise CoordinationError("Formula identity is incomplete")
    subject = exact_formula_subject(name, architecture)
    matching = [
        item
        for item in tap_plan["formulae"]
        if exact_formula_subject(
            item["identity"]["name"], item["identity"]["architecture"]
        )
        == subject
    ]
    if len(matching) != 1 or canonical_bytes(matching[0]) != canonical_bytes(formula_plan):
        raise CoordinationError("Formula plan is not one exact tap-plan member")

    capture = _mapping(formula_plan.get("capture"), "Formula capture")
    policy_entry = _capture_entry(root, name, architecture)
    if (
        policy_entry["capture_policy_sha256"] != capture.get("capture_policy_sha256")
        or policy_entry["tap_paths"]
        != sorted(
            {
                identity["formula_path"],
                *(item["path"] for item in capture.get("tap_input_components", [])),
            }
        )
    ):
        raise CoordinationError("Formula plan capture differs from protected capture policy")
    declared_kandelo = list(policy_entry["kandelo_paths"])
    declared_tap = list(policy_entry["tap_paths"])
    assessment = assess_capture(
        subject=subject,
        affected_products=formula_plan["required_by_products"],
        kandelo_root=kandelo,
        tap_root=root,
        kandelo_paths=declared_kandelo,
        tap_paths=declared_tap,
        # Formula policy generation statically audits observed Formula paths;
        # the full declared sets remain the build sandbox allowlist.
        observed_kandelo_paths=declared_kandelo,
        observed_tap_paths=declared_tap,
    )
    kandelo_inputs = _repository_inputs(assessment["captured"], "kandelo")
    tap_inputs = _repository_inputs(assessment["captured"], "tap")

    formula_components = [
        {
            "id": "formula",
            "sha256": _digest(
                identity.get("normalized_formula_sha256"), "normalized Formula"
            ),
        }
    ]
    for index, component in enumerate(capture.get("tap_input_components", [])):
        formula_components.append(
            {
                "id": f"tap-input-{index:04d}",
                "sha256": _digest(component.get("sha256"), "Formula tap component"),
            }
        )

    sources = []
    for source in _sequence(capture.get("sources"), "Formula sources"):
        checked = _mapping(source, "Formula source")
        receipt = canonical_sha256(
            {
                "kind": checked.get("kind"),
                "mirrors": checked.get("mirrors"),
                "role": checked.get("role"),
                "sha256": checked.get("sha256"),
                "url": checked.get("url"),
            }
        )
        sources.append(
            {
                "role": checked.get("role"),
                "url": checked.get("url"),
                "sha256": _digest(checked.get("sha256"), "Formula source"),
                "receipt_sha256": receipt,
            }
        )
    sources.sort(key=lambda item: item["role"])

    environment_policy = policy_entry["environment_policy"]
    toolchain_identity = _component(
        "toolchain",
        capture_policy_sha256=capture["capture_policy_sha256"],
        kandelo_inputs=kandelo_inputs,
        tap_inputs=tap_inputs,
    )
    native_inputs = []
    for native in _sequence(capture.get("native_requirements"), "native requirements"):
        checked = _mapping(native, "native requirement")
        native_identity = {
            "environment_policy": environment_policy,
            "identity": checked.get("identity"),
            "scopes": checked.get("scopes"),
            "toolchain_sha256": toolchain_identity["component_sha256"],
        }
        native_inputs.append(
            {
                "role": checked.get("identity"),
                "identity": checked.get("identity"),
                "sha256": canonical_sha256(native_identity),
                "receipt_sha256": canonical_sha256(
                    {"kind": "kandelo-native-input-receipt", **native_identity}
                ),
            }
        )
    native_inputs.sort(key=lambda item: (item["role"], item["identity"]))

    direct_dependencies = []
    for dependency in formula_plan["direct_dependencies"]:
        dependency_subject = exact_formula_subject(
            dependency["formula"], dependency["architecture"]
        )
        if dependency_subject not in dependency_candidates:
            raise CoordinationError(
                f"Formula {name} awaits exact dependency {dependency_subject}"
            )
        direct_dependencies.append(
            _candidate_dependency(
                dependency_candidates[dependency_subject],
                expected_request=tap_plan["request_digest"],
                expected_subject=dependency_subject,
                expected_formula=dependency["formula"],
                expected_architecture=dependency["architecture"],
                materialization_policy_sha256=dependency[
                    "materialization_policy_sha256"
                ],
            )
        )
    direct_dependencies.sort(
        key=lambda item: exact_formula_subject(item["formula"], item["architecture"])
    )

    components = {
        field: _component(
            field,
            capture_policy_sha256=capture["capture_policy_sha256"],
            kandelo_inputs=kandelo_inputs,
            tap_inputs=tap_inputs,
        )
        for field in ("sdk", "libc", "sysroot", "toolchain", "instrumentation")
    }
    environment = {
        "policy_sha256": canonical_sha256(
            {
                "kind": "kandelo-homebrew-build-environment-policy",
                "name": environment_policy,
                "capture_policy_sha256": capture["capture_policy_sha256"],
            }
        ),
        "variables_sha256": canonical_sha256(
            {
                "architecture": architecture,
                "credential_policy": "stripped-before-formula-execution",
                "environment_policy": environment_policy,
                "sdk": "worktree-local",
            }
        ),
    }
    staging_policy_body = (root / "Kandelo/staging/tap-policy.toml").read_bytes()
    contract = build_bottle_contract(
        {
            "schema": 1,
            "kind": "kandelo-homebrew-bottle-contract",
            "target": {
                "abi": tap_plan["target_abi"]["version"],
                "snapshot_sha256": tap_plan["target_abi"]["snapshot_sha256"],
                "architecture": architecture,
            },
            "formula": {
                "name": name,
                "version": identity["version"],
                "revision": identity["revision"],
                "rebuild": identity["rebuild"],
                "normalized_source_sha256": capture["normalized_source_sha256"],
                "source_components": formula_components,
            },
            "kandelo_inputs": kandelo_inputs,
            "tap_inputs": tap_inputs,
            **components,
            "environment": environment,
            "sources": sources,
            "native_inputs": native_inputs,
            "direct_dependencies": direct_dependencies,
            "build_policy_sha256": canonical_sha256(
                {
                    "capture_policy_sha256": capture["capture_policy_sha256"],
                    "kind": "kandelo-protected-bottle-build-policy",
                    "staging_policy_sha256": hashlib.sha256(
                        staging_policy_body
                    ).hexdigest(),
                    "version": 1,
                }
            ),
        }
    )
    return contract, assessment


def prepare_tap_plan_contracts(
    *,
    tap_root: Path,
    kandelo_root: Path,
    tap_plan: Mapping[str, Any],
    candidate_facts: Sequence[CandidateFactV1],
    candidate_records: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Advance contracts in graph order using only exact dependency layers."""

    validate_tap_plan(tap_plan)
    planned = copy.deepcopy(dict(tap_plan))
    request_sha256 = planned["request_digest"]
    formulae: dict[str, Mapping[str, Any]] = {}
    for item in planned["formulae"]:
        identity = item["identity"]
        formulae[
            exact_formula_subject(identity["name"], identity["architecture"])
        ] = item

    candidates: dict[str, list[CandidateFactV1]] = {}
    for fact in candidate_facts:
        if fact.request_sha256 != request_sha256:
            continue
        if fact.record_sha256 not in candidate_records:
            raise CoordinationError("candidate fact lacks its exact public record")
        candidates.setdefault(fact.subject, []).append(fact)

    contracts: dict[str, dict[str, Any]] = {}
    assessments: dict[str, dict[str, Any]] = {}
    for item in planned["formulae"]:
        identity = item["identity"]
        subject = exact_formula_subject(identity["name"], identity["architecture"])
        item["contract_sha256"] = None
        dependency_candidates: dict[str, Mapping[str, Any]] = {}
        dependencies_ready = True
        for dependency in item["direct_dependencies"]:
            dependency_subject = exact_formula_subject(
                dependency["formula"], dependency["architecture"]
            )
            dependency_plan = formulae.get(dependency_subject)
            if dependency_plan is None:
                raise CoordinationError("Formula dependency is outside the exact tap plan")
            dependency_contract = dependency_plan.get("contract_sha256")
            if dependency_contract is None:
                dependencies_ready = False
                break
            matching = [
                fact
                for fact in candidates.get(dependency_subject, [])
                if fact.contract_sha256 == dependency_contract
            ]
            if not matching:
                dependencies_ready = False
                break
            if len({fact.bottle_layer_sha256 for fact in matching}) != 1:
                raise CoordinationError(
                    "dependency contract has conflicting candidate bottle layers"
                )
            selected = min(matching, key=lambda fact: fact.record_sha256)
            record = _mapping(
                candidate_records[selected.record_sha256],
                "dependency candidate record",
            )
            payload = _mapping(record.get("candidate"), "dependency candidate payload")
            layer = _mapping(
                payload.get("bottle_layer"), "dependency candidate bottle layer"
            )
            if layer.get("sha256") != selected.bottle_layer_sha256:
                raise CoordinationError(
                    "dependency candidate fact differs from its public record"
                )
            dependency_candidates[dependency_subject] = {
                "request_sha256": selected.request_sha256,
                "subject": selected.subject,
                "record_sha256": selected.record_sha256,
                "contract_sha256": selected.contract_sha256,
                "bottle_layer": {
                    "sha256": layer.get("sha256"),
                    "bytes": layer.get("bytes"),
                },
            }
        if not dependencies_ready:
            continue
        contract, assessment = build_formula_contract(
            tap_root=tap_root,
            kandelo_root=kandelo_root,
            tap_plan=planned,
            formula_plan=item,
            dependency_candidates=dependency_candidates,
        )
        contracts[subject] = contract
        assessments[subject] = assessment
        if assessment["complete"]:
            item["contract_sha256"] = canonical_sha256(contract)

    validate_tap_plan(planned)
    return (
        dict(json.loads(canonical_bytes(planned))),
        {key: contracts[key] for key in sorted(contracts)},
        {key: assessments[key] for key in sorted(assessments)},
    )


def coordinate_planned_request(
    *,
    mode: str,
    tap_root: Path,
    kandelo_root: Path,
    request: Mapping[str, Any],
    request_asset_url: str,
    tap_plan: Mapping[str, Any],
    reconciliation: ReconciliationDecisionV1,
    inventory: PublicSchedulingInventoryV1,
    now: str,
    policy: TapStagingPolicyV1,
    verification_tests: Sequence[VerificationTestDefinitionV1],
) -> dict[str, Any]:
    """Build one canonical coordination bundle from protected and public facts."""

    planned, contracts, assessments = prepare_tap_plan_contracts(
        tap_root=tap_root,
        kandelo_root=kandelo_root,
        tap_plan=tap_plan,
        candidate_facts=inventory.records.candidates,
        candidate_records=inventory.candidate_records,
    )
    scheduling = schedule_ready_batch(
        planned,
        inventory.records,
        reconciliation,
        now=now,
        policy=policy,
        verification_tests=verification_tests,
    )
    lifecycle = {
        "state": reconciliation.lifecycle.state,
        "current_head": reconciliation.lifecycle.current_head,
        "merged_commit": reconciliation.lifecycle.merged_commit,
    }
    workflow = build_workflow_manifest(
        mode=mode,
        request=request,
        request_sha256=planned["request_digest"],
        request_asset_url=request_asset_url,
        lifecycle=lifecycle,
        tap_plan=planned,
        scheduling=scheduling,
        candidate_locators=inventory.candidate_locators,
        max_ready_subjects=policy.max_ready_subjects_per_cycle,
    )
    definitions = [
        {
            "id": definition.id,
            "hosts": list(definition.hosts),
            "kandelo_paths": list(definition.kandelo_paths),
            "policy": definition.policy,
            "sha256": definition.sha256,
        }
        for definition in verification_tests
    ]
    bundle = {
        "schema": 1,
        "kind": "kandelo-abi-staging-coordination-bundle",
        "mode": mode,
        "request_sha256": planned["request_digest"],
        "request_asset_url": request_asset_url,
        "request": copy.deepcopy(dict(request)),
        "lifecycle": lifecycle,
        "tap_plan": planned,
        "contracts": contracts,
        "capture_assessments": assessments,
        "candidates": {
            "locators": copy.deepcopy(dict(inventory.candidate_locators)),
            "records": copy.deepcopy(dict(inventory.candidate_records)),
        },
        "verification_receipts": {
            "locators": copy.deepcopy(dict(inventory.verification_locators)),
            "records": copy.deepcopy(dict(inventory.verification_records)),
        },
        "reuse_bindings": {
            "locators": copy.deepcopy(dict(inventory.reuse_locators)),
            "records": copy.deepcopy(dict(inventory.reuse_records)),
        },
        "verification_tests": definitions,
        "workflow": workflow,
    }
    normalized = json.loads(canonical_bytes(bundle))
    validate_coordination_bundle(
        normalized, max_ready_subjects=policy.max_ready_subjects_per_cycle
    )
    return normalized


def validate_coordination_bundle(
    value: Mapping[str, Any], *, max_ready_subjects: int
) -> None:
    bundle = _mapping(value, "coordination bundle")
    if frozenset(bundle) != frozenset(
        {
            "schema",
            "kind",
            "mode",
            "request_sha256",
            "request_asset_url",
            "request",
            "lifecycle",
            "tap_plan",
            "contracts",
            "capture_assessments",
            "candidates",
            "verification_receipts",
            "reuse_bindings",
            "verification_tests",
            "workflow",
        }
    ):
        raise CoordinationError("coordination bundle fields changed")
    if (
        bundle["schema"] != 1
        or bundle["kind"] != "kandelo-abi-staging-coordination-bundle"
        or bundle["mode"] not in {"observe", "active"}
    ):
        raise CoordinationError("coordination bundle protocol is unsupported")
    request = _mapping(bundle["request"], "coordination request")
    request_sha256 = _digest(bundle["request_sha256"], "coordination request")
    if canonical_sha256(request) != request_sha256:
        raise CoordinationError("coordination request digest differs from its bytes")
    if not isinstance(bundle["request_asset_url"], str) or not bundle[
        "request_asset_url"
    ]:
        raise CoordinationError("coordination request asset URL is invalid")
    plan = _mapping(bundle["tap_plan"], "coordination tap plan")
    validate_tap_plan(plan)
    if (
        plan["request_digest"] != request_sha256
        or plan["request_asset_url"] != bundle["request_asset_url"]
    ):
        raise CoordinationError("coordination tap plan names another request")
    workflow = _mapping(bundle["workflow"], "coordination workflow")
    validate_workflow_manifest(workflow, max_ready_subjects=max_ready_subjects)
    if (
        workflow["mode"] != bundle["mode"]
        or workflow["tap_plan_sha256"] != canonical_sha256(plan)
        or workflow["request"]["sha256"] != request_sha256
        or workflow["request"]["lifecycle"] != bundle["lifecycle"]
    ):
        raise CoordinationError("coordination workflow differs from its inputs")

    formulae = {
        exact_formula_subject(
            item["identity"]["name"], item["identity"]["architecture"]
        ): item
        for item in plan["formulae"]
    }
    contracts = _mapping(bundle["contracts"], "coordination contracts")
    assessments = _mapping(
        bundle["capture_assessments"], "coordination capture assessments"
    )
    if set(contracts) != set(assessments) or not set(contracts).issubset(formulae):
        raise CoordinationError("coordination contract subjects are incomplete")
    for subject in sorted(contracts):
        contract = build_bottle_contract(
            _mapping(contracts[subject], f"coordination contract {subject}")
        )
        assessment = _mapping(
            assessments[subject], f"coordination capture assessment {subject}"
        )
        validate_capture_assessment(assessment)
        if assessment["subject"] != subject:
            raise CoordinationError("coordination assessment names another subject")
        planned_digest = formulae[subject]["contract_sha256"]
        if assessment["complete"]:
            if planned_digest != canonical_sha256(contract):
                raise CoordinationError("coordination contract differs from tap plan")
        elif planned_digest is not None:
            raise CoordinationError("incomplete capture entered ordinary scheduling")

    candidates = _mapping(bundle["candidates"], "coordination candidates")
    if frozenset(candidates) != frozenset({"locators", "records"}):
        raise CoordinationError("coordination candidate fields changed")
    locators = _mapping(candidates["locators"], "coordination candidate locators")
    records = _mapping(candidates["records"], "coordination candidate records")
    if set(locators) != set(records):
        raise CoordinationError("coordination candidates lack records or locators")
    for digest in sorted(locators):
        _digest(digest, "coordination candidate record")
        try:
            locator = parse_public_record_locator(
                _mapping(locators[digest], "coordination candidate locator")
            )
        except OciPublicationError as error:
            raise CoordinationError(
                f"coordination candidate locator is invalid: {error}"
            ) from error
        if locator["digest"] != "sha256:" + digest:
            raise CoordinationError("coordination candidate locator digest differs")
        validate_candidate_record(
            _mapping(records[digest], "coordination candidate record")
        )

    for field, validator in (
        ("verification_receipts", validate_verification_receipt_record),
        ("reuse_bindings", validate_candidate_reuse_record),
    ):
        collection = _mapping(bundle[field], f"coordination {field}")
        if frozenset(collection) != frozenset({"locators", "records"}):
            raise CoordinationError(f"coordination {field} fields changed")
        collection_locators = _mapping(
            collection["locators"], f"coordination {field} locators"
        )
        collection_records = _mapping(
            collection["records"], f"coordination {field} records"
        )
        if set(collection_locators) != set(collection_records):
            raise CoordinationError(f"coordination {field} lacks records or locators")
        for digest in sorted(collection_locators):
            _digest(digest, f"coordination {field} record")
            try:
                locator = parse_public_record_locator(
                    _mapping(
                        collection_locators[digest],
                        f"coordination {field} locator",
                    )
                )
            except OciPublicationError as error:
                raise CoordinationError(
                    f"coordination {field} locator is invalid: {error}"
                ) from error
            if locator["digest"] != "sha256:" + digest:
                raise CoordinationError(f"coordination {field} locator digest differs")
            validator(
                _mapping(
                    collection_records[digest],
                    f"coordination {field} record",
                )
            )

    definitions = _sequence(
        bundle["verification_tests"], "coordination verification tests"
    )
    previous = ""
    for definition in definitions:
        checked = _mapping(definition, "coordination verification test")
        if frozenset(checked) != frozenset(
            {"id", "hosts", "kandelo_paths", "policy", "sha256"}
        ):
            raise CoordinationError("coordination verification test fields changed")
        identity = {
            "hosts": list(_sequence(checked["hosts"], "verification hosts")),
            "id": checked["id"],
            "kandelo_paths": list(
                _sequence(checked["kandelo_paths"], "verification paths")
            ),
            "policy": checked["policy"],
        }
        if (
            not isinstance(checked["id"], str)
            or checked["id"] <= previous
            or canonical_sha256(identity) != checked["sha256"]
        ):
            raise CoordinationError("coordination verification test identity drifted")
        previous = checked["id"]
    if not definitions:
        raise CoordinationError("coordination bundle lacks verification tests")
    if len(canonical_bytes(bundle)) > 64 * 1024 * 1024:
        raise CoordinationError("coordination bundle exceeds its byte bound")
