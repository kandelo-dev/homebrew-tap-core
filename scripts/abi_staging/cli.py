"""Command-line entrypoint for protected ABI-staging coordination."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit

from .canonical import canonical_bytes
from .contract import (
    ContractError,
    build_miniature_bottle_contract_fixture,
    candidate_reuse_decision,
    contract_from_build_context,
    load_bottle_contract,
    load_canonical_mapping,
    make_candidate_reuse_record,
)
from .coordination import (
    CoordinationError,
    coordinate_planned_request,
)
from .execution import (
    ExecutionError,
    execute_build_work,
    execute_verification_work,
    load_coordination_bundle,
    select_build_work,
    select_reuse_work,
    select_verification_work,
)
from .github_public import DiscoveredRequestV1, GitHubPublicClient, PublicGitHubError
from .handoff import (
    HandoffError,
    load_build_result,
    load_handoff_validation_expectations,
    validate_handoff,
)
from .oci import (
    OciPublicationError,
    build_oci_manifest,
    isolated_oras_transport,
    publish_record,
    UrllibOciTransportV1,
)
from .inventory import InventoryError, scan_scheduling_inventory
from .formula_inventory import FormulaInventoryError, write_formula_inventory_fixture
from .plan import (
    PlanError,
    build_miniature_tap_plan_fixture,
    load_formula_requirements,
    plan_exact_tap_request,
    snapshot_tap_source,
    write_canonical_plan,
)
from .records import (
    OciRecordPlanV1,
    TapRecordError,
    build_attempt_outcome_oci_plan,
    build_candidate_oci_plan,
    build_source_custody_oci_plan,
    load_tap_plan_record,
)
from .reconcile import (
    PullRequestLifecycleV1,
    ReconciliationDecisionV1,
    ReconciliationError,
    load_reconciliation_activation,
    reconcile_request,
    select_reconciliation_cycle,
)
from .request import RequestValidationError, load_request_issuer_policy, validate_request
from .reuse import CandidateReuseError, publish_candidate_reuse
from .policy import (
    PolicyError,
    attempt_repository,
    candidate_repository,
    check_policy_files,
    load_candidate_publication_activation,
    load_tap_staging_policy,
    load_verification_tests,
    source_custody_repository,
    write_formula_capture_catalog,
)
from .workflow_artifact import (
    GitHubWorkflowArtifactClientV1,
    WorkflowArtifactError,
    WorkflowArtifactServiceError,
    WorkflowJobV1,
)
from .workflow_publication import (
    WorkflowPublicationError,
    build_protected_attempt_outcome,
    build_protected_verification_outcome,
)
from .verification import (
    VerificationError,
    VerificationPublicationError,
    load_verification_result,
    publish_protected_verification_outcome,
    publish_verification_receipt,
)


TAP_ROOT = Path(__file__).resolve().parents[2]


def _decision_mapping(
    discovered: DiscoveredRequestV1,
    decision: ReconciliationDecisionV1,
) -> dict[str, Any]:
    return {
        "action": decision.action,
        "asset_url": discovered.asset_url,
        "blockers": list(decision.blockers),
        "claim_key": decision.claim_key,
        "current_for_pull_request": decision.current_for_pull_request,
        "exact_head": discovered.request["build_source"]["commit"],
        "lifecycle": {
            "current_head": decision.lifecycle.current_head,
            "merged_commit": decision.lifecycle.merged_commit,
            "state": decision.lifecycle.state,
        },
        "permitted_work": list(decision.permitted_work),
        "request_digest": decision.request_digest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.abi_staging.cli")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("scan")
    reconcile = subcommands.add_parser("reconcile")
    reconcile.add_argument("--request-asset-url", required=True)
    discover_workflow = subcommands.add_parser("discover-workflow-request")
    discover_workflow.add_argument("--tap-root", required=True)
    discover_workflow.add_argument("--request-asset-url", default="")
    discover_workflow.add_argument("--cycle-index", required=True, type=int)
    discover_workflow.add_argument("--out", required=True)
    discover_workflow.add_argument("--github-output", required=True)
    prepare_workflow = subcommands.add_parser("prepare-workflow")
    prepare_workflow.add_argument("--tap-root", required=True)
    prepare_workflow.add_argument("--kandelo-root", required=True)
    prepare_workflow.add_argument("--discovery", required=True)
    prepare_workflow.add_argument("--request", required=True)
    prepare_workflow.add_argument("--formula-requirements", required=True)
    prepare_workflow.add_argument("--now", required=True)
    prepare_workflow.add_argument("--out", required=True)
    prepare_workflow.add_argument("--github-output", required=True)
    policy_check = subcommands.add_parser("policy-check")
    policy_check.add_argument("--tap-root", required=True)
    policy_generate = subcommands.add_parser("policy-generate")
    policy_generate.add_argument("--tap-root", required=True)
    policy_generate.add_argument("--out", required=True)
    formula_fixture = subcommands.add_parser("formula-inventory-fixture")
    formula_fixture.add_argument("--tap-root", required=True)
    formula_fixture.add_argument("--out", required=True)
    plan = subcommands.add_parser("plan-request")
    plan.add_argument("--tap-root", required=True)
    plan.add_argument("--request", required=True)
    plan.add_argument("--request-asset-url", required=True)
    plan.add_argument("--formula-requirements", required=True)
    plan.add_argument("--out", required=True)
    plan_fixture = subcommands.add_parser("tap-plan-fixture")
    plan_fixture.add_argument("--tap-root", required=True)
    plan_fixture.add_argument("--out", required=True)
    contract = subcommands.add_parser("contract")
    contract.add_argument("--input", required=True)
    contract.add_argument("--out", required=True)
    reuse = subcommands.add_parser("reuse")
    reuse.add_argument("--contract", required=True)
    reuse.add_argument("--candidate", required=True)
    reuse.add_argument("--expected-source-custody-sha256", required=True)
    reuse.add_argument("--subject", required=True)
    reuse.add_argument("--new-request", required=True)
    reuse.add_argument("--out", required=True)
    contract_fixture = subcommands.add_parser("bottle-contract-fixture")
    contract_fixture.add_argument("--tap-root", required=True)
    contract_fixture.add_argument("--out", required=True)
    fixture_check = subcommands.add_parser("fixture-check")
    fixture_check.add_argument("--fixture", required=True)
    execute_build = subcommands.add_parser("execute-build-work")
    execute_build.add_argument("--coordination", required=True)
    execute_build.add_argument("--work-id", required=True)
    execute_build.add_argument("--kandelo-root", required=True)
    execute_build.add_argument("--tap-root", required=True)
    execute_build.add_argument("--run-id", required=True, type=int)
    execute_build.add_argument("--run-attempt", required=True, type=int)
    execute_build.add_argument("--workflow-ref", required=True)
    execute_build.add_argument("--out", required=True)
    execute_verification = subcommands.add_parser("execute-verification-work")
    execute_verification.add_argument("--coordination", required=True)
    execute_verification.add_argument("--work-id", required=True)
    execute_verification.add_argument("--kandelo-root", required=True)
    execute_verification.add_argument("--tap-root", required=True)
    execute_verification.add_argument("--run-id", required=True, type=int)
    execute_verification.add_argument("--run-attempt", required=True, type=int)
    execute_verification.add_argument("--workflow-ref", required=True)
    execute_verification.add_argument("--out", required=True)
    publish = subcommands.add_parser("publish-candidate")
    publish.add_argument("--tap-root", required=True)
    publish.add_argument("--handoff", required=True)
    publish.add_argument("--request", required=True)
    publish.add_argument("--tap-plan", required=True)
    publish.add_argument("--formula-plan", required=True)
    publish.add_argument("--publication-run", required=True)
    publish.add_argument("--out", required=True)
    publish_workflow = subcommands.add_parser("publish-workflow-candidate")
    publish_workflow.add_argument("--run-id", required=True, type=int)
    publish_workflow.add_argument("--run-attempt", required=True, type=int)
    publish_workflow.add_argument("--head-sha", required=True)
    publish_workflow.add_argument("--work-id", required=True)
    publish_workflow.add_argument("--coordination-artifact-id", required=True)
    publish_workflow.add_argument("--coordination-artifact-digest", required=True)
    publish_workflow.add_argument("--producer-conclusion", required=True)
    publish_workflow.add_argument("--handoff-artifact-id", required=True)
    publish_workflow.add_argument("--handoff-artifact-digest", required=True)
    publish_workflow.add_argument("--require-github-digest", action="store_true")
    publish_workflow.add_argument("--anonymous-readback", action="store_true")
    publish_workflow.add_argument("--immutable", action="store_true")
    publish_workflow.add_argument("--out", required=True)
    publish_receipt = subcommands.add_parser("publish-workflow-receipt")
    publish_receipt.add_argument("--run-id", required=True, type=int)
    publish_receipt.add_argument("--run-attempt", required=True, type=int)
    publish_receipt.add_argument("--head-sha", required=True)
    publish_receipt.add_argument("--work-id", required=True)
    publish_receipt.add_argument("--coordination-artifact-id", required=True)
    publish_receipt.add_argument("--coordination-artifact-digest", required=True)
    publish_receipt.add_argument("--producer-conclusion", required=True)
    publish_receipt.add_argument("--handoff-artifact-id", required=True)
    publish_receipt.add_argument("--handoff-artifact-digest", required=True)
    publish_receipt.add_argument("--require-github-digest", action="store_true")
    publish_receipt.add_argument("--anonymous-readback", action="store_true")
    publish_receipt.add_argument("--immutable", action="store_true")
    publish_receipt.add_argument("--out", required=True)
    publish_reuse = subcommands.add_parser("publish-workflow-reuse")
    publish_reuse.add_argument("--run-id", required=True, type=int)
    publish_reuse.add_argument("--run-attempt", required=True, type=int)
    publish_reuse.add_argument("--head-sha", required=True)
    publish_reuse.add_argument("--work-id", required=True)
    publish_reuse.add_argument("--coordination-artifact-id", required=True)
    publish_reuse.add_argument("--coordination-artifact-digest", required=True)
    publish_reuse.add_argument("--require-github-digest", action="store_true")
    publish_reuse.add_argument("--anonymous-readback", action="store_true")
    publish_reuse.add_argument("--immutable", action="store_true")
    publish_reuse.add_argument("--out", required=True)
    return parser


def _protected_tap_root(value: str) -> Path:
    root = Path(value).resolve(strict=True)
    if root != TAP_ROOT.resolve(strict=True):
        raise PolicyError("--tap-root must name this protected tap checkout")
    return root


def _output_directory(value: str) -> Path:
    path = Path(value)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise PolicyError("workflow output must be a real directory")
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        allowed = {"discovery.json", "request.json"}
        actual = {item.name for item in path.iterdir()}
        if not actual.issubset(allowed) or any(item.is_symlink() for item in path.iterdir()):
            raise PolicyError("workflow output directory contains unknown entries")
    return path.resolve(strict=True)


def _write_github_outputs(path: Path, values: dict[str, str]) -> None:
    if path.is_symlink() or not path.is_file():
        raise PolicyError("GitHub output path must be an existing regular file")
    lines = []
    for key in sorted(values):
        value = values[key]
        if (
            not key.replace("_", "").isalnum()
            or not isinstance(value, str)
            or "\n" in value
            or "\r" in value
            or "\0" in value
            or len(value.encode()) > 1024 * 1024
        ):
            raise PolicyError("GitHub output contains an unsafe field")
        lines.append(f"{key}={value}\n")
    with path.open("a", encoding="utf-8", newline="") as output:
        output.writelines(lines)


def _discover_workflow_request(args: argparse.Namespace) -> None:
    tap_root = _protected_tap_root(args.tap_root)
    output = _output_directory(args.out)
    staging_policy = load_tap_staging_policy(
        tap_root / "Kandelo/staging/tap-policy.toml"
    )
    issuer_policy = load_request_issuer_policy(
        tap_root / "Kandelo/staging/request-issuers.toml",
        expected_tap=staging_policy.tap_repository,
    )
    load_reconciliation_activation(
        tap_root / "Kandelo/staging/reconciliation-activation.toml"
    )
    client = GitHubPublicClient(issuer_policy)
    discovered = (
        (client.discover_url(args.request_asset_url),)
        if args.request_asset_url
        else client.scan()
    )
    pairs = []
    decisions = []
    for candidate in discovered:
        lifecycle = client.pull_request_lifecycle(
            candidate.request["pull_request"]["number"]
        )
        decision = reconcile_request(candidate, lifecycle)
        pairs.append((candidate, decision))
        decisions.append(_decision_mapping(candidate, decision))
    selected = pairs[0] if args.request_asset_url and pairs else select_reconciliation_cycle(
        tuple(pairs), cycle_index=args.cycle_index
    )
    tap_source = snapshot_tap_source(tap_root, staging_policy.tap_repository)
    selection = None
    outputs = {
        "build_matrix": '{"include":[]}',
        "mode": "observe",
        "reuse_matrix": '{"include":[]}',
        "selected": "false",
        "tap_commit": tap_source["commit"],
        "verify_matrix": '{"include":[]}',
    }
    if selected is not None:
        candidate, decision = selected
        request_body = canonical_bytes(candidate.request)
        if hashlib.sha256(request_body).hexdigest() != candidate.request_digest:
            raise ReconciliationError(
                "selected public request differs from its canonical digest"
            )
        (output / "request.json").write_bytes(request_body)
        selection = _decision_mapping(candidate, decision)
        selection.update(
            {
                "asset_name": candidate.asset_name,
                "release_tag": candidate.release_tag,
            }
        )
        outputs.update(
            {
                "kandelo_head": candidate.request["build_source"]["commit"],
                "kandelo_policy_commit": candidate.request["issuance"][
                    "issuer_workflow_ref"
                ].rsplit("@", 1)[1],
                "kandelo_repository": candidate.request["build_source"]["repository"],
                "selected": "true",
            }
        )
    discovery = {
        "schema": 1,
        "kind": "kandelo-abi-staging-workflow-discovery",
        "cycle_index": args.cycle_index,
        "tap_source": tap_source,
        "decisions": decisions,
        "selection": selection,
    }
    (output / "discovery.json").write_bytes(canonical_bytes(discovery))
    _write_github_outputs(Path(args.github_output), outputs)


def _load_workflow_discovery(path: Path) -> dict[str, Any]:
    value = load_canonical_mapping(path.resolve(strict=True).read_bytes(), "workflow discovery")
    if frozenset(value) != frozenset(
        {"schema", "kind", "cycle_index", "tap_source", "decisions", "selection"}
    ) or value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-workflow-discovery":
        raise ReconciliationError("workflow discovery protocol is unsupported")
    if value["selection"] is None or not isinstance(value["selection"], dict):
        raise ReconciliationError("workflow discovery lacks one selected request")
    return dict(value)


def _prepare_workflow(args: argparse.Namespace) -> None:
    tap_root = _protected_tap_root(args.tap_root)
    kandelo_root = Path(args.kandelo_root).resolve(strict=True)
    output = _output_directory(args.out)
    discovery = _load_workflow_discovery(Path(args.discovery))
    request_path = Path(args.request).resolve(strict=True)
    request_body = request_path.read_bytes()
    selection = discovery["selection"]
    asset_url = selection["asset_url"]
    asset_name = Path(urlsplit(asset_url).path).name
    staging_policy = load_tap_staging_policy(
        tap_root / "Kandelo/staging/tap-policy.toml"
    )
    issuer_policy = load_request_issuer_policy(
        tap_root / "Kandelo/staging/request-issuers.toml",
        expected_tap=staging_policy.tap_repository,
    )
    request = validate_request(request_body, asset_name, issuer_policy)
    request_sha256 = hashlib.sha256(request_body).hexdigest()
    if (
        request_sha256 != selection["request_digest"]
        or request["build_source"]["commit"] != selection["exact_head"]
    ):
        raise ReconciliationError("workflow discovery and request bytes differ")
    kandelo_source = snapshot_tap_source(
        kandelo_root, staging_policy.kandelo_repository
    )
    if kandelo_source != dict(request["build_source"]):
        raise ReconciliationError(
            "Kandelo checkout is not the exact requested PR head and tree"
        )
    lifecycle_value = selection["lifecycle"]
    lifecycle = PullRequestLifecycleV1(
        lifecycle_value["state"],
        lifecycle_value["current_head"],
        lifecycle_value["merged_commit"],
    )
    discovered = DiscoveredRequestV1(
        request_sha256,
        selection["asset_name"],
        asset_url,
        selection["release_tag"],
        request,
    )
    reconciliation = reconcile_request(discovered, lifecycle)
    if (
        reconciliation.action != selection["action"]
        or reconciliation.current_for_pull_request
        != selection["current_for_pull_request"]
    ):
        raise ReconciliationError("workflow discovery decision is not reproducible")
    formula_requirements = load_formula_requirements(
        Path(args.formula_requirements).resolve(strict=True).read_bytes()
    )
    tap_plan = plan_exact_tap_request(
        tap_root,
        request,
        request_digest=request_sha256,
        request_asset_url=asset_url,
        formula_requirements=formula_requirements,
        tap_repository=staging_policy.tap_repository,
    )
    if tap_plan["tap_source"] != discovery["tap_source"]:
        raise ReconciliationError(
            "protected tap checkout changed after workflow discovery"
        )
    verification_tests = load_verification_tests(
        tap_root / "Kandelo/staging/verification-tests.toml"
    )
    transport = UrllibOciTransportV1(username="", token="")
    inventory = scan_scheduling_inventory(
        tap_plan,
        policy=staging_policy,
        verification_tests=verification_tests,
        transport=transport,
    )
    reconciliation_mode = load_reconciliation_activation(
        tap_root / "Kandelo/staging/reconciliation-activation.toml"
    )
    publication_mode = load_candidate_publication_activation(
        tap_root / "Kandelo/staging/candidate-publication-activation.toml"
    )
    mode = (
        "active"
        if reconciliation_mode == "active" and publication_mode == "active"
        else "observe"
    )
    bundle = coordinate_planned_request(
        mode=mode,
        tap_root=tap_root,
        kandelo_root=kandelo_root,
        request=request,
        request_asset_url=asset_url,
        tap_plan=tap_plan,
        reconciliation=reconciliation,
        inventory=inventory,
        now=args.now,
        policy=staging_policy,
        verification_tests=verification_tests,
    )
    (output / "coordination.json").write_bytes(canonical_bytes(bundle))
    (output / "tap-plan.json").write_bytes(canonical_bytes(bundle["tap_plan"]))
    (output / "workflow-plan.json").write_bytes(canonical_bytes(bundle["workflow"]))
    _write_github_outputs(
        Path(args.github_output),
        {
            "build_matrix": json.dumps(
                bundle["workflow"]["build_matrix"], separators=(",", ":"), sort_keys=True
            ),
            "mode": mode,
            "reuse_matrix": json.dumps(
                bundle["workflow"]["reuse_matrix"],
                separators=(",", ":"),
                sort_keys=True,
            ),
            "verify_matrix": json.dumps(
                bundle["workflow"]["verify_matrix"], separators=(",", ":"), sort_keys=True
            ),
        },
    )


def _local_locator(repository: str, manifest: bytes) -> dict[str, str]:
    digest = hashlib.sha256(manifest).hexdigest()
    return {
        "repository": "ghcr.io/" + repository,
        "digest": "sha256:" + digest,
        "immutable_reference": f"ghcr.io/{repository}@sha256:{digest}",
    }


def _protected_oci_failure(
    error: OciPublicationError | VerificationPublicationError, *, phase: str
) -> dict[str, Any]:
    """Reduce an OCI exception to bounded facts safe for a durable record."""

    phased = error.with_phase(phase)
    return {
        "phase": phased.phase,
        "kind": phased.kind,
        "http_status": phased.http_status,
        "retryable": phased.retryable,
        "guard_code": phased.guard_code,
    }


def _publication_result(
    *,
    mode: str,
    candidate_plan: OciRecordPlanV1,
    planned_source_locator: dict[str, str],
    planned_candidate_locator: dict[str, str],
    published_source_locator: dict[str, Any] | None,
    published_candidate_locator: dict[str, Any] | None,
) -> dict[str, Any]:
    has_published_source = published_source_locator is not None
    has_published_candidate = published_candidate_locator is not None
    if has_published_source != has_published_candidate:
        raise TapRecordError("candidate publication locators must appear together")
    if mode == "observe" and has_published_source:
        raise TapRecordError("observe mode cannot claim published candidate bytes")
    if mode == "active" and not has_published_source:
        raise TapRecordError("active mode requires exact anonymous publication evidence")
    if mode not in {"observe", "active"}:
        raise TapRecordError("candidate publication mode is unsupported")
    for label, planned, published in (
        ("source custody", planned_source_locator, published_source_locator),
        ("candidate record", planned_candidate_locator, published_candidate_locator),
    ):
        if published is None:
            continue
        if any(published.get(field) != planned[field] for field in planned):
            raise TapRecordError(f"published {label} differs from its local identity")
        readback = published.get("anonymous_readback_sha256")
        if (
            not isinstance(readback, str)
            or len(readback) != 64
            or any(character not in "0123456789abcdef" for character in readback)
        ):
            raise TapRecordError(f"published {label} lacks anonymous readback evidence")
    candidate_record = load_canonical_mapping(
        candidate_plan.config.body, "candidate record"
    )
    candidate_payload = candidate_record["candidate"]
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-candidate-publication",
        "mode": mode,
        "planned": {
            "source_custody": planned_source_locator,
            "candidate_record": planned_candidate_locator,
        },
        "published": (
            None
            if published_source_locator is None
            else {
                "source_custody": published_source_locator,
                "candidate_record": published_candidate_locator,
            }
        ),
        "candidate_config_sha256": hashlib.sha256(
            candidate_plan.config.body
        ).hexdigest(),
        "bottle_layer": candidate_payload["bottle_layer"],
        "formula": candidate_payload["formula"],
        "original_producer": candidate_payload["producer"],
        "nonendorsed": True,
    }


def _publish_candidate_paths(
    *,
    tap_root_value: Path,
    handoff_value: Path,
    request_value: Path,
    tap_plan_value: Path,
    formula_plan_value: Path,
    publication_run_value: Path,
) -> dict[str, Any]:
    tap_root = tap_root_value.resolve(strict=True)
    if tap_root != TAP_ROOT.resolve(strict=True):
        raise PolicyError("--tap-root must name this protected tap checkout")
    expectations = load_handoff_validation_expectations(
        request_path=request_value.resolve(strict=True),
        tap_plan_path=tap_plan_value.resolve(strict=True),
        formula_plan_path=formula_plan_value.resolve(strict=True),
    )
    policy = load_tap_staging_policy(tap_root / "Kandelo/staging/tap-policy.toml")
    handoff_root = handoff_value.resolve(strict=True)
    validate_handoff(
        handoff_root,
        max_files=policy.max_handoff_files,
        max_bytes=policy.max_handoff_bytes,
        expected_request_sha256=expectations["request_sha256"],
        expected_subject=expectations["subject"],
        expected_kandelo_source=expectations["kandelo_source"],
        expected_tap_source=expectations["tap_source"],
    )
    publication_run = load_canonical_mapping(
        publication_run_value.resolve(strict=True).read_bytes(),
        "candidate publication run",
    )
    contract = load_bottle_contract((handoff_root / "bottle-contract.json").read_bytes())
    formula = contract["formula"]["name"]
    target_abi = contract["target"]["abi"]
    source_repository = source_custody_repository(policy, target_abi)
    candidate_record_repository = candidate_repository(
        policy, target_abi, formula=formula
    )
    source_plan = build_source_custody_oci_plan(
        handoff_root / "source-custody", repository=source_repository
    )
    source_manifest = build_oci_manifest(source_plan)
    planned_source_locator = _local_locator(source_repository, source_manifest)
    candidate_plan = build_candidate_oci_plan(
        handoff_root,
        repository=candidate_record_repository,
        source_record=planned_source_locator,
        source_manifest_bytes=source_manifest,
        publication_run=publication_run,
    )
    candidate_manifest = build_oci_manifest(candidate_plan)
    planned_candidate_locator = _local_locator(
        candidate_record_repository, candidate_manifest
    )
    published_source_locator = None
    published_candidate_locator = None
    mode = load_candidate_publication_activation(
        tap_root / "Kandelo/staging/candidate-publication-activation.toml"
    )
    if mode == "active":
        username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
        token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
        with isolated_oras_transport(username=username, token=token) as transport:
            try:
                published_source = publish_record(
                    source_plan,
                    transport=transport,
                    expected_source_repository=policy.tap_repository,
                )
            except OciPublicationError as error:
                raise error.with_phase("source-custody-publication") from error
            if (
                published_source.repository != planned_source_locator["repository"]
                or published_source.digest != planned_source_locator["digest"]
                or published_source.immutable_reference
                != planned_source_locator["immutable_reference"]
            ):
                raise OciPublicationError(
                    "published source locator differs from its local identity",
                    guard_code="candidate_public_readback_failed",
                    phase="source-custody-publication",
                )
            try:
                published_candidate = publish_record(
                    candidate_plan,
                    transport=transport,
                    expected_source_repository=policy.tap_repository,
                )
            except OciPublicationError as error:
                raise error.with_phase("candidate-record-publication") from error
        published_source_locator = asdict(published_source)
        published_candidate_locator = asdict(published_candidate)
    result = _publication_result(
        mode=mode,
        candidate_plan=candidate_plan,
        planned_source_locator=planned_source_locator,
        planned_candidate_locator=planned_candidate_locator,
        published_source_locator=published_source_locator,
        published_candidate_locator=published_candidate_locator,
    )
    return result


def _publish_candidate(args: argparse.Namespace) -> None:
    result = _publish_candidate_paths(
        tap_root_value=Path(args.tap_root),
        handoff_value=Path(args.handoff),
        request_value=Path(args.request),
        tap_plan_value=Path(args.tap_plan),
        formula_plan_value=Path(args.formula_plan),
        publication_run_value=Path(args.publication_run),
    )
    Path(args.out).write_bytes(canonical_bytes(result))


def _execute_build(args: argparse.Namespace) -> int:
    tap_root = _protected_tap_root(args.tap_root)
    policy = load_tap_staging_policy(
        tap_root / "Kandelo/staging/tap-policy.toml"
    )
    run = {
        "repository": policy.tap_repository,
        "workflow_ref": args.workflow_ref,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "job": "build-candidate",
    }
    return execute_build_work(
        coordination_path=Path(args.coordination),
        work_id=args.work_id,
        kandelo_root=Path(args.kandelo_root),
        tap_root=tap_root,
        run=run,
        handoff=Path(args.out),
    )


def _execute_verification(args: argparse.Namespace) -> int:
    tap_root = _protected_tap_root(args.tap_root)
    policy = load_tap_staging_policy(
        tap_root / "Kandelo/staging/tap-policy.toml"
    )
    run = {
        "repository": policy.tap_repository,
        "workflow_ref": args.workflow_ref,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "job": "verify-candidate",
    }
    return execute_verification_work(
        coordination_path=Path(args.coordination),
        work_id=args.work_id,
        kandelo_root=Path(args.kandelo_root),
        tap_root=tap_root,
        run=run,
        output=Path(args.out),
    )


def _require_workflow_publication_guards(args: argparse.Namespace) -> None:
    if not (
        args.require_github_digest and args.anonymous_readback and args.immutable
    ):
        raise WorkflowPublicationError(
            "workflow publication requires GitHub digest, anonymous readback, and immutability"
        )


def _workflow_artifact_output(
    client: GitHubWorkflowArtifactClientV1,
    *,
    artifact_id: str,
    artifact_digest: str,
    name: str,
    required: bool,
) -> Any:
    present = (bool(artifact_id), bool(artifact_digest))
    if present == (False, False) and not required:
        return None
    if present != (True, True) or not artifact_id.isdigit():
        raise WorkflowPublicationError(
            "protected upload outputs are incomplete or malformed"
        )
    try:
        return client.artifact_by_id(
            artifact_id=int(artifact_id),
            name=name,
            sha256=artifact_digest,
        )
    except WorkflowArtifactError:
        raise


def _workflow_job_from_needs(*, name: str, conclusion: str) -> WorkflowJobV1:
    if conclusion not in {"cancelled", "failure", "skipped", "success"}:
        raise WorkflowPublicationError("protected producer result is unsupported")
    completed_at = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    return WorkflowJobV1(name, conclusion, completed_at)


def _recheck_workflow_activation(tap_root: Path) -> None:
    if (
        load_reconciliation_activation(
            tap_root / "Kandelo/staging/reconciliation-activation.toml"
        )
        != "active"
        or load_candidate_publication_activation(
            tap_root / "Kandelo/staging/candidate-publication-activation.toml"
        )
        != "active"
    ):
        raise WorkflowPublicationError("candidate workflow publication is not active")


def _publish_workflow_candidate(args: argparse.Namespace) -> None:
    """Publish only protected reconstructions from the current workflow run."""

    _require_workflow_publication_guards(args)
    tap_root = TAP_ROOT.resolve(strict=True)
    policy = load_tap_staging_policy(tap_root / "Kandelo/staging/tap-policy.toml")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if repository != policy.tap_repository:
        raise WorkflowPublicationError("current workflow repository differs from tap policy")
    tap_source = snapshot_tap_source(tap_root, policy.tap_repository)
    if tap_source["commit"] != args.head_sha:
        raise WorkflowPublicationError("protected publisher checkout differs from workflow head")
    _recheck_workflow_activation(tap_root)
    client = GitHubWorkflowArtifactClientV1(
        repository,
        token,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        head_sha=args.head_sha,
        workflow_ref=workflow_ref,
    )
    publication_run = {
        "repository": repository,
        "workflow_ref": workflow_ref,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "job": "publish-candidate",
    }
    with tempfile.TemporaryDirectory(prefix="kandelo-abi-staging-publisher-") as temporary:
        root = Path(temporary)
        coordination_artifact = _workflow_artifact_output(
            client,
            artifact_id=args.coordination_artifact_id,
            artifact_digest=args.coordination_artifact_digest,
            name=f"abi-staging-coordination-{args.run_id}-{args.run_attempt}",
            required=True,
        )
        if coordination_artifact is None:
            raise WorkflowPublicationError("protected coordination artifact is absent")
        coordination_root = root / "coordination"
        client.extract_artifact(
            coordination_artifact,
            coordination_root,
            max_files=32,
            max_bytes=64 * 1024 * 1024,
        )
        bundle = load_coordination_bundle(
            coordination_root / "coordination.json", policy=policy
        )
        work = select_build_work(bundle, args.work_id)
        formula_matches = [
            item
            for item in bundle["tap_plan"]["formulae"]
            if hashlib.sha256(canonical_bytes(item)).hexdigest()
            == work["formula_plan_sha256"]
        ]
        if len(formula_matches) != 1:
            raise WorkflowPublicationError("build work lacks one exact Formula plan")
        formula_plan = formula_matches[0]
        if bundle["tap_plan"]["tap_source"] != tap_source:
            raise WorkflowPublicationError(
                "protected publisher source differs from coordinated tap source"
            )

        # Existing exact work may finish after a close or head advance. Query
        # lifecycle again so publication never relies on stale discovery, but
        # do not turn historical validity into current applicability.
        issuer_policy = load_request_issuer_policy(
            tap_root / "Kandelo/staging/request-issuers.toml",
            expected_tap=policy.tap_repository,
        )
        lifecycle = GitHubPublicClient(issuer_policy).pull_request_lifecycle(
            bundle["request"]["pull_request"]["number"]
        )
        discovered = DiscoveredRequestV1(
            bundle["request_sha256"],
            Path(urlsplit(bundle["request_asset_url"]).path).name,
            bundle["request_asset_url"],
            Path(urlsplit(bundle["request_asset_url"]).path).parts[-2],
            bundle["request"],
        )
        reconcile_request(discovered, lifecycle)

        job = _workflow_job_from_needs(
            name="build-candidate " + args.work_id,
            conclusion=args.producer_conclusion,
        )
        service_failure = None
        try:
            artifact = _workflow_artifact_output(
                client,
                artifact_id=args.handoff_artifact_id,
                artifact_digest=args.handoff_artifact_digest,
                name=(
                    f"{work['artifact_name']}-{args.run_id}-{args.run_attempt}"
                ),
                required=False,
            )
        except WorkflowArtifactServiceError as error:
            artifact = None
            service_failure = error
        application_outcome = None
        application_guard = None
        candidate_record_sha256 = None
        candidate_publication = None
        publication_failure = None
        if artifact is not None:
            handoff_root = root / "handoff"
            try:
                client.extract_artifact(
                    artifact,
                    handoff_root,
                    max_files=policy.max_handoff_files,
                    max_bytes=policy.max_handoff_bytes,
                )
            except WorkflowArtifactServiceError as error:
                service_failure = error
            except WorkflowArtifactError:
                application_outcome = "failure"
                application_guard = "candidate_integrity_mismatch"
            else:
                request_path = root / "request.json"
                tap_plan_path = root / "tap-plan.json"
                formula_plan_path = root / "formula-plan.json"
                publication_run_path = root / "publication-run.json"
                request_path.write_bytes(canonical_bytes(bundle["request"]))
                tap_plan_path.write_bytes(canonical_bytes(bundle["tap_plan"]))
                formula_plan_path.write_bytes(canonical_bytes(formula_plan))
                publication_run_path.write_bytes(canonical_bytes(publication_run))
                try:
                    expectations = load_handoff_validation_expectations(
                        request_path=request_path,
                        tap_plan_path=tap_plan_path,
                        formula_plan_path=formula_plan_path,
                    )
                    validate_handoff(
                        handoff_root,
                        max_files=policy.max_handoff_files,
                        max_bytes=policy.max_handoff_bytes,
                        expected_request_sha256=expectations["request_sha256"],
                        expected_subject=expectations["subject"],
                        expected_kandelo_source=expectations["kandelo_source"],
                        expected_tap_source=expectations["tap_source"],
                    )
                    build_result = load_build_result(
                        (handoff_root / "build-result.json").read_bytes()
                    )
                except (HandoffError, OSError, ValueError):
                    application_outcome = "failure"
                    application_guard = "candidate_integrity_mismatch"
                else:
                    if build_result["outcome"] == "success":
                        _recheck_workflow_activation(tap_root)
                        application_outcome = "success"
                        try:
                            candidate_publication = _publish_candidate_paths(
                                tap_root_value=tap_root,
                                handoff_value=handoff_root,
                                request_value=request_path,
                                tap_plan_value=tap_plan_path,
                                formula_plan_value=formula_plan_path,
                                publication_run_value=publication_run_path,
                            )
                        except OciPublicationError as error:
                            if error.phase is None:
                                raise WorkflowPublicationError(
                                    "candidate publication failure lacks its phase"
                                ) from error
                            publication_failure = _protected_oci_failure(
                                error, phase=error.phase
                            )
                        else:
                            candidate_record_sha256 = candidate_publication[
                                "planned"
                            ]["candidate_record"]["digest"].removeprefix("sha256:")
                    else:
                        application_outcome = (
                            "timeout"
                            if build_result["exit_code"] == 124
                            else "failure"
                        )
                        application_guard = (
                            "build_timeout"
                            if build_result["exit_code"] == 124
                            else "build_failed"
                        )

        attempt_record = build_protected_attempt_outcome(
            bundle=bundle,
            work=work,
            job=job,
            artifact=None if service_failure is not None else artifact,
            application_outcome=application_outcome,
            application_guard=application_guard,
            candidate_record_sha256=candidate_record_sha256,
            publication_run=publication_run,
            infrastructure_kind=(
                None if service_failure is None else service_failure.kind
            ),
            infrastructure_http_status=(
                None if service_failure is None else service_failure.http_status
            ),
            publication_failure=publication_failure,
        )
        formula_name = formula_plan["identity"]["name"]
        target_abi = bundle["tap_plan"]["target_abi"]["version"]
        attempt_plan = build_attempt_outcome_oci_plan(
            attempt_record,
            repository=attempt_repository(
                policy, target_abi, formula=formula_name
            ),
        )
        _recheck_workflow_activation(tap_root)
        username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
        package_token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
        with isolated_oras_transport(
            username=username, token=package_token
        ) as transport:
            published_attempt = publish_record(
                attempt_plan,
                transport=transport,
                expected_source_repository=policy.tap_repository,
            )
        result = {
            "schema": 1,
            "kind": "kandelo-abi-staging-workflow-candidate-publication",
            "work_id": work["work_id"],
            "job": {
                "name": job.name,
                "conclusion": job.conclusion,
                "completed_at": job.completed_at,
            },
            "handoff": (
                None
                if artifact is None
                else {
                    "id": artifact.id,
                    "sha256": artifact.sha256,
                    "bytes": artifact.size_in_bytes,
                }
            ),
            "candidate": candidate_publication,
            "attempt_record": attempt_record,
            "attempt_locator": asdict(published_attempt),
        }
        Path(args.out).write_bytes(canonical_bytes(result))


def _recheck_coordinated_lifecycle(
    tap_root: Path,
    policy: Any,
    bundle: Mapping[str, Any],
) -> None:
    issuer_policy = load_request_issuer_policy(
        tap_root / "Kandelo/staging/request-issuers.toml",
        expected_tap=policy.tap_repository,
    )
    lifecycle = GitHubPublicClient(issuer_policy).pull_request_lifecycle(
        bundle["request"]["pull_request"]["number"]
    )
    asset_path = Path(urlsplit(bundle["request_asset_url"]).path)
    discovered = DiscoveredRequestV1(
        bundle["request_sha256"],
        asset_path.name,
        bundle["request_asset_url"],
        asset_path.parts[-2],
        bundle["request"],
    )
    reconcile_request(discovered, lifecycle)


def _publish_workflow_reuse(args: argparse.Namespace) -> None:
    """Publish an explicit binding without executing or rewriting a candidate."""

    _require_workflow_publication_guards(args)
    tap_root = TAP_ROOT.resolve(strict=True)
    policy = load_tap_staging_policy(tap_root / "Kandelo/staging/tap-policy.toml")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if repository != policy.tap_repository:
        raise WorkflowPublicationError(
            "current workflow repository differs from tap policy"
        )
    tap_source = snapshot_tap_source(tap_root, policy.tap_repository)
    if tap_source["commit"] != args.head_sha:
        raise WorkflowPublicationError(
            "protected reuse checkout differs from workflow head"
        )
    _recheck_workflow_activation(tap_root)
    client = GitHubWorkflowArtifactClientV1(
        repository,
        token,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        head_sha=args.head_sha,
        workflow_ref=workflow_ref,
    )
    publication_run = {
        "repository": repository,
        "workflow_ref": workflow_ref,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "job": "publish-reuse",
    }
    with tempfile.TemporaryDirectory(
        prefix="kandelo-abi-staging-reuse-publisher-"
    ) as temporary:
        root = Path(temporary)
        coordination_artifact = _workflow_artifact_output(
            client,
            artifact_id=args.coordination_artifact_id,
            artifact_digest=args.coordination_artifact_digest,
            name=f"abi-staging-coordination-{args.run_id}-{args.run_attempt}",
            required=True,
        )
        if coordination_artifact is None:
            raise WorkflowPublicationError(
                "protected coordination artifact is absent"
            )
        coordination_root = root / "coordination"
        client.extract_artifact(
            coordination_artifact,
            coordination_root,
            max_files=32,
            max_bytes=64 * 1024 * 1024,
        )
        bundle = load_coordination_bundle(
            coordination_root / "coordination.json", policy=policy
        )
        work = select_reuse_work(bundle, args.work_id)
        if bundle["tap_plan"]["tap_source"] != tap_source:
            raise WorkflowPublicationError(
                "protected reuse source differs from coordinated tap source"
            )
        _recheck_coordinated_lifecycle(tap_root, policy, bundle)
        _recheck_workflow_activation(tap_root)
        username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
        package_token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
        with isolated_oras_transport(
            username=username, token=package_token
        ) as transport:
            record, published = publish_candidate_reuse(
                bundle,
                work["work_id"],
                publication_run=publication_run,
                policy=policy,
                transport=transport,
            )
        Path(args.out).write_bytes(
            canonical_bytes(
                {
                    "schema": 1,
                    "kind": "kandelo-abi-staging-workflow-reuse-publication",
                    "work_id": work["work_id"],
                    "reuse_record": record,
                    "reuse_locator": asdict(published),
                }
            )
        )


def _verification_result_matches_work(
    result: Mapping[str, Any],
    *,
    bundle: Mapping[str, Any],
    work: Mapping[str, Any],
    run: Mapping[str, Any],
    definition: Any,
    job_conclusion: str,
) -> bool:
    candidate = bundle["candidates"]["records"].get(
        work["candidate_record_sha256"]
    )
    if not isinstance(candidate, Mapping):
        return False
    payload = candidate.get("candidate")
    common = candidate.get("common")
    if not isinstance(payload, Mapping) or not isinstance(common, Mapping):
        return False
    selected_definition = result.get("test_definition")
    return (
        result.get("candidate_record") == work["candidate_locator"]
        and result.get("candidate_layer") == payload.get("bottle_layer")
        and result.get("request_sha256") == bundle["request_sha256"]
        and result.get("source") == bundle["request"]["build_source"]
        and result.get("run") == run
        and result.get("attempt_ordinal") == work["attempt_ordinal"]
        and result.get("runtime_artifacts")
        == {"kernel": None, "host_runtime": None, "vfs": None}
        and isinstance(selected_definition, Mapping)
        and selected_definition.get("id") == definition.id
        and selected_definition.get("sha256") == definition.sha256
        and selected_definition.get("host") == work["host"]
        and (
            (result.get("outcome") == "success" and job_conclusion == "success")
            or (
                result.get("outcome") in {"failure", "timeout"}
                and job_conclusion == "failure"
            )
        )
    )


def _publish_workflow_receipt(args: argparse.Namespace) -> None:
    """Publish one receipt reconstructed by protected code from exact job facts."""

    _require_workflow_publication_guards(args)
    tap_root = TAP_ROOT.resolve(strict=True)
    policy = load_tap_staging_policy(tap_root / "Kandelo/staging/tap-policy.toml")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if repository != policy.tap_repository:
        raise WorkflowPublicationError(
            "current workflow repository differs from tap policy"
        )
    tap_source = snapshot_tap_source(tap_root, policy.tap_repository)
    if tap_source["commit"] != args.head_sha:
        raise WorkflowPublicationError(
            "protected receipt checkout differs from workflow head"
        )
    _recheck_workflow_activation(tap_root)
    client = GitHubWorkflowArtifactClientV1(
        repository,
        token,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        head_sha=args.head_sha,
        workflow_ref=workflow_ref,
    )
    verification_run = {
        "repository": repository,
        "workflow_ref": workflow_ref,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "job": "verify-candidate",
    }
    with tempfile.TemporaryDirectory(
        prefix="kandelo-abi-staging-receipt-publisher-"
    ) as temporary:
        root = Path(temporary)
        coordination_artifact = _workflow_artifact_output(
            client,
            artifact_id=args.coordination_artifact_id,
            artifact_digest=args.coordination_artifact_digest,
            name=f"abi-staging-coordination-{args.run_id}-{args.run_attempt}",
            required=True,
        )
        if coordination_artifact is None:
            raise WorkflowPublicationError("protected coordination artifact is absent")
        coordination_root = root / "coordination"
        client.extract_artifact(
            coordination_artifact,
            coordination_root,
            max_files=32,
            max_bytes=64 * 1024 * 1024,
        )
        bundle = load_coordination_bundle(
            coordination_root / "coordination.json", policy=policy
        )
        work = select_verification_work(bundle, args.work_id)
        if bundle["tap_plan"]["tap_source"] != tap_source:
            raise WorkflowPublicationError(
                "protected receipt source differs from coordinated tap source"
            )
        definitions = [
            definition
            for definition in load_verification_tests(
                tap_root / "Kandelo/staging/verification-tests.toml"
            )
            if definition.sha256 == work["test_definition_sha256"]
            and work["host"] in definition.hosts
        ]
        if len(definitions) != 1:
            raise WorkflowPublicationError(
                "verification work lacks one current protected test definition"
            )
        definition = definitions[0]
        _recheck_coordinated_lifecycle(tap_root, policy, bundle)
        job = _workflow_job_from_needs(
            name="verify-candidate " + args.work_id,
            conclusion=args.producer_conclusion,
        )
        service_failure = None
        try:
            artifact = _workflow_artifact_output(
                client,
                artifact_id=args.handoff_artifact_id,
                artifact_digest=args.handoff_artifact_digest,
                name=(
                    f"{work['artifact_name']}-{args.run_id}-{args.run_attempt}"
                ),
                required=False,
            )
        except WorkflowArtifactServiceError as error:
            artifact = None
            service_failure = error
        result_root = root / "verification"
        loaded_result = None
        if artifact is not None:
            try:
                client.extract_artifact(
                    artifact,
                    result_root,
                    max_files=64,
                    max_bytes=64 * 1024 * 1024,
                )
            except WorkflowArtifactServiceError as error:
                service_failure = error
                candidate_result = None
            except WorkflowArtifactError:
                candidate_result = None
            else:
                try:
                    candidate_result = load_verification_result(result_root)
                except VerificationError:
                    candidate_result = None
            if candidate_result is not None and _verification_result_matches_work(
                candidate_result,
                bundle=bundle,
                work=work,
                run=verification_run,
                definition=definition,
                job_conclusion=job.conclusion,
            ):
                loaded_result = candidate_result
        if service_failure is not None:
            application_outcome = None
            application_guard = None
        elif loaded_result is None and artifact is not None:
            application_outcome = "failure"
            application_guard = "candidate_integrity_mismatch"
        elif loaded_result is None:
            application_outcome = None
            application_guard = None
        else:
            application_outcome = loaded_result["outcome"]
            application_guard = {
                "success": None,
                "failure": "verification_failed",
                "timeout": "verification_timeout",
            }[application_outcome]
        outcome = build_protected_verification_outcome(
            bundle=bundle,
            work=work,
            job=job,
            artifact=None if service_failure is not None else artifact,
            application_outcome=application_outcome,
            application_guard=application_guard,
            verification_run=verification_run,
            infrastructure_kind=(
                None if service_failure is None else service_failure.kind
            ),
            infrastructure_http_status=(
                None if service_failure is None else service_failure.http_status
            ),
        )
        _recheck_workflow_activation(tap_root)
        username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
        package_token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
        with isolated_oras_transport(
            username=username, token=package_token
        ) as transport:
            try:
                if loaded_result is None:
                    published = publish_protected_verification_outcome(
                        candidate_locator=work["candidate_locator"],
                        test_definition=definition,
                        host=work["host"],
                        tap_policy=policy,
                        expected_run=verification_run,
                        expected_runtime_artifacts={
                            "kernel": None,
                            "host_runtime": None,
                            "vfs": None,
                        },
                        expected_request_sha256=bundle["request_sha256"],
                        expected_source=bundle["request"]["build_source"],
                        completed_at=job.completed_at,
                        attempt_ordinal=work["attempt_ordinal"],
                        outcome=outcome["outcome"],
                        guard_code=outcome["guard_code"],
                        transport=transport,
                        expected_source_repository=policy.tap_repository,
                    )
                else:
                    published = publish_verification_receipt(
                        result_root,
                        candidate_locator=work["candidate_locator"],
                        test_definition=definition,
                        tap_policy=policy,
                        expected_run=verification_run,
                        expected_runtime_artifacts={
                            "kernel": None,
                            "host_runtime": None,
                            "vfs": None,
                        },
                        expected_request_sha256=bundle["request_sha256"],
                        expected_source=bundle["request"]["build_source"],
                        completed_at=job.completed_at,
                        transport=transport,
                        expected_source_repository=policy.tap_repository,
                    )
            except VerificationPublicationError as error:
                if error.phase is None:
                    raise WorkflowPublicationError(
                        "verification publication failure lacks its phase"
                    ) from error
                publication_failure = _protected_oci_failure(
                    error, phase=error.phase
                )
                outcome = build_protected_verification_outcome(
                    bundle=bundle,
                    work=work,
                    job=job,
                    artifact=artifact,
                    application_outcome=application_outcome,
                    application_guard=application_guard,
                    verification_run=verification_run,
                    infrastructure_kind=(
                        None if service_failure is None else service_failure.kind
                    ),
                    infrastructure_http_status=(
                        None
                        if service_failure is None
                        else service_failure.http_status
                    ),
                    publication_failure=publication_failure,
                )
                candidate_record = bundle["candidates"]["records"].get(
                    work["candidate_record_sha256"]
                )
                if not isinstance(candidate_record, Mapping):
                    raise WorkflowPublicationError(
                        "verification publication recovery lacks its coordinated candidate"
                    )
                published = publish_protected_verification_outcome(
                    candidate_locator=work["candidate_locator"],
                    test_definition=definition,
                    host=work["host"],
                    tap_policy=policy,
                    expected_run=verification_run,
                    expected_runtime_artifacts={
                        "kernel": None,
                        "host_runtime": None,
                        "vfs": None,
                    },
                    expected_request_sha256=bundle["request_sha256"],
                    expected_source=bundle["request"]["build_source"],
                    completed_at=job.completed_at,
                    attempt_ordinal=work["attempt_ordinal"],
                    outcome=outcome["outcome"],
                    guard_code=outcome["guard_code"],
                    publication_failure=publication_failure,
                    coordinated_candidate=candidate_record,
                    transport=transport,
                    expected_source_repository=policy.tap_repository,
                )
        result = {
            "schema": 1,
            "kind": "kandelo-abi-staging-workflow-receipt-publication",
            "work_id": work["work_id"],
            "job": {
                "name": job.name,
                "conclusion": job.conclusion,
                "completed_at": job.completed_at,
            },
            "handoff": (
                None
                if artifact is None
                else {
                    "id": artifact.id,
                    "sha256": artifact.sha256,
                    "bytes": artifact.size_in_bytes,
                }
            ),
            "outcome": outcome,
            "receipt_locator": asdict(published),
        }
        Path(args.out).write_bytes(canonical_bytes(result))


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        if args.command == "discover-workflow-request":
            _discover_workflow_request(args)
            return 0
        if args.command == "prepare-workflow":
            _prepare_workflow(args)
            return 0
        if args.command == "execute-build-work":
            return _execute_build(args)
        if args.command == "execute-verification-work":
            return _execute_verification(args)
        if args.command == "publish-workflow-candidate":
            _publish_workflow_candidate(args)
            return 0
        if args.command == "publish-workflow-receipt":
            _publish_workflow_receipt(args)
            return 0
        if args.command == "publish-workflow-reuse":
            _publish_workflow_reuse(args)
            return 0
        if args.command == "publish-candidate":
            _publish_candidate(args)
            return 0
        if args.command in {
            "policy-check",
            "policy-generate",
            "formula-inventory-fixture",
            "plan-request",
            "tap-plan-fixture",
            "bottle-contract-fixture",
        }:
            tap_root = Path(args.tap_root).resolve(strict=True)
            if tap_root != TAP_ROOT.resolve(strict=True):
                raise PolicyError("--tap-root must name this protected tap checkout")
            if args.command == "policy-check":
                check_policy_files(tap_root)
            elif args.command == "policy-generate":
                write_formula_capture_catalog(tap_root, Path(args.out))
            elif args.command == "formula-inventory-fixture":
                write_formula_inventory_fixture(tap_root, Path(args.out))
            elif args.command == "tap-plan-fixture":
                destination = Path(args.out)
                expected = tap_root / "Kandelo/staging/fixtures/tap-plan.json"
                if destination.resolve(strict=False) != expected.resolve(strict=False):
                    raise PlanError("tap plan fixture must use its protected path")
                write_canonical_plan(destination, build_miniature_tap_plan_fixture(tap_root))
            elif args.command == "bottle-contract-fixture":
                destination = Path(args.out)
                expected = tap_root / "Kandelo/staging/fixtures/bottle-contract.json"
                if destination.resolve(strict=False) != expected.resolve(strict=False):
                    raise ContractError(
                        "bottle contract fixture must use its protected path"
                    )
                destination.write_bytes(
                    canonical_bytes(build_miniature_bottle_contract_fixture())
                )
            else:
                staging_policy = load_tap_staging_policy(
                    tap_root / "Kandelo/staging/tap-policy.toml"
                )
                issuer_policy = load_request_issuer_policy(
                    tap_root / "Kandelo/staging/request-issuers.toml",
                    expected_tap=staging_policy.tap_repository,
                )
                request_path = Path(args.request).resolve(strict=True)
                request_body = request_path.read_bytes()
                asset_name = Path(args.request_asset_url).name
                request = validate_request(request_body, asset_name, issuer_policy)
                formula_requirements = load_formula_requirements(
                    Path(args.formula_requirements).resolve(strict=True).read_bytes()
                )
                planned = plan_exact_tap_request(
                    tap_root,
                    request,
                    request_digest=hashlib.sha256(request_body).hexdigest(),
                    request_asset_url=args.request_asset_url,
                    formula_requirements=formula_requirements,
                    tap_repository=staging_policy.tap_repository,
                )
                write_canonical_plan(Path(args.out), planned)
            return 0
        if args.command == "contract":
            context = load_canonical_mapping(
                Path(args.input).resolve(strict=True).read_bytes(),
                "contract build context",
            )
            Path(args.out).write_bytes(canonical_bytes(contract_from_build_context(context)))
            return 0
        if args.command == "reuse":
            contract = load_bottle_contract(
                Path(args.contract).resolve(strict=True).read_bytes()
            )
            candidate = load_canonical_mapping(
                Path(args.candidate).resolve(strict=True).read_bytes(),
                "existing candidate",
            )
            decision = candidate_reuse_decision(
                contract,
                candidate,
                expected_source_custody_sha256=args.expected_source_custody_sha256,
            )
            if decision["action"] != "reuse":
                raise ContractError(
                    f"candidate is not reusable: {decision['reason']}"
                )
            new_request = load_canonical_mapping(
                Path(args.new_request).resolve(strict=True).read_bytes(),
                "new request context",
            )
            record = make_candidate_reuse_record(
                contract, args.subject, candidate, new_request
            )
            Path(args.out).write_bytes(canonical_bytes(record))
            return 0
        if args.command == "fixture-check":
            fixture = Path(args.fixture).resolve(strict=True)
            tap_plan = TAP_ROOT / "Kandelo/staging/fixtures/tap-plan.json"
            bottle_contract = TAP_ROOT / "Kandelo/staging/fixtures/bottle-contract.json"
            if fixture == tap_plan.resolve(strict=True):
                load_tap_plan_record(fixture.read_bytes())
            elif fixture == bottle_contract.resolve(strict=True):
                load_bottle_contract(fixture.read_bytes())
            else:
                raise TapRecordError(
                    "fixture-check accepts only protected ABI staging fixtures"
                )
            return 0
        policy = load_request_issuer_policy(
            TAP_ROOT / "Kandelo/staging/request-issuers.toml",
            expected_tap="kandelo-dev/homebrew-tap-core",
        )
        load_reconciliation_activation(
            TAP_ROOT / "Kandelo/staging/reconciliation-activation.toml"
        )
        client = GitHubPublicClient(policy)
        if args.command == "scan":
            discovered = client.scan()
        else:
            discovered = (client.discover_url(args.request_asset_url),)
        decisions = []
        for request in discovered:
            number = request.request["pull_request"]["number"]
            lifecycle = client.pull_request_lifecycle(number)
            decisions.append(_decision_mapping(request, reconcile_request(request, lifecycle)))
        sys.stdout.buffer.write(canonical_bytes({"decisions": decisions, "mode": "observe"}))
        return 0
    except (
        PolicyError,
        FormulaInventoryError,
        PlanError,
        ContractError,
        TapRecordError,
        PublicGitHubError,
        ReconciliationError,
        RequestValidationError,
        HandoffError,
        OciPublicationError,
        CoordinationError,
        InventoryError,
        ExecutionError,
        WorkflowArtifactError,
        WorkflowPublicationError,
        VerificationError,
        CandidateReuseError,
    ) as error:
        print(f"abi-staging {args.command}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
