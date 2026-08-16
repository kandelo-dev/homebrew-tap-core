"""Command-line entrypoint for protected ABI-staging coordination."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from .abi_history import (
    HISTORY_RECORD_MEDIA_TYPE,
    AbiHistoryError,
    GitHubHistoryClient,
    build_history_oci_plan,
    build_history_plan,
    build_history_record,
    ensure_history_ref,
    history_record_repository,
    validate_history_creation_handoff,
    validate_history_plan,
    validate_protection_snapshot,
    verify_history_snapshot,
)
from .canonical import canonical_bytes, canonical_sha256
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
from .custody import CustodyError, load_source_custody_manifest
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
    FetchedOciRecordV1,
    OciPublicationError,
    PublishedRecordLocatorV1,
    build_oci_manifest,
    fetch_public_record,
    isolated_oras_transport,
    list_public_record_locators,
    publish_record,
    UrllibOciTransportV1,
)
from .inventory import InventoryError, scan_scheduling_inventory
from .formula_inventory import FormulaInventoryError, write_formula_inventory_fixture
from .plan import (
    PlanError,
    build_miniature_tap_plan_fixture,
    exact_formula_subject,
    load_formula_requirements,
    parse_formula_subject,
    plan_exact_tap_request,
    snapshot_tap_source,
    write_canonical_plan,
)
from .records import (
    CANDIDATE_RECORD_MEDIA_TYPE,
    CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
    SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
    OciRecordPlanV1,
    TapRecordError,
    build_attempt_outcome_oci_plan,
    build_candidate_oci_plan,
    build_source_custody_oci_plan,
    load_tap_plan_record,
    validate_admission_record,
    validate_abi_history_record,
    validate_candidate_record,
)
from .promotion import (
    ADMISSION_RECORD_MEDIA_TYPE,
    CanonicalBottlePublicationV1,
    PromotionDecisionV1,
    PromotionError,
    admission_repository,
    build_canonical_bottle_plan,
    evaluate_promotion,
    expected_canonical_publication,
    finalize_admission_record,
    metadata_patch_document,
    prepare_admission,
    prepare_formula_metadata_patch,
    prepare_successor_activation_patch,
    publish_admission_record,
    publish_canonical_bottle,
    promotion_override_identity,
    read_canonical_publication,
    validate_promotion_history_barrier,
    validate_promotion_candidate_binding,
    validate_promotion_decision,
)
from .tap_metadata import (
    FormulaMetadataUpdateV1,
    GitTapMetadataStore,
    TapMetadataError,
    TapMetadataWriteError,
    apply_metadata_patch,
    build_admission_projection_observation,
    check_tap_metadata,
    formula_generated_metadata_sha256,
    load_abi_state,
    load_promotion_activation,
    load_promotion_policy,
    recover_landed_formula_metadata_commit,
    validate_formula_admission_projection,
    validate_landed_formula_metadata_commit,
)
from .product import (
    MAX_INPUT_OBJECT_BYTES,
    NONPUBLIC_PRODUCT_INPUT_KINDS,
    ProductInputResolutionError,
    load_product_input_object_inventory,
    load_resolved_product_inputs,
    materialize_resolved_product_input_objects,
    product_runtime_identity,
    resolve_product_from_checked_input_authority,
    selected_product_formula_readiness,
    select_product_execution_scope,
    select_product_input_build_spec,
    validate_private_product_authority_handoff,
    validate_product_input_object_authority,
    validate_product_build_handoff,
    write_product_build_handoff,
)
from .product_evidence import (
    ProductEvidenceError,
    build_candidate_product_oci_plan,
    build_product_evidence_context,
    candidate_product_repository,
    inspect_candidate_product_repository,
    inspect_product_evidence_repository,
    load_candidate_product_locator,
    publish_candidate_product,
    publish_exact_product_evidence,
    select_product_evidence_execution_scope,
    select_product_evidence_publication_scope,
    validate_candidate_builder_report,
    validate_product_evidence_record,
    validate_product_evidence_result,
)
from .reconcile import (
    build_promotion_plan_document,
    build_promotion_workflow_plan,
    build_product_workflow_wave,
    build_product_workflow_seed,
    ProductProgressV1,
    ProductSelectionV1,
    PromotionEpochV1,
    PromotionProgressV1,
    PromotionSubjectV1,
    PullRequestLifecycleV1,
    ReconciliationDecisionV1,
    ReconciliationError,
    load_promotion_plan_document,
    load_product_evidence_activation,
    load_reconciliation_activation,
    reconcile_request,
    select_promotion_plan_work,
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
    VERIFICATION_RECEIPT_MEDIA_TYPE,
    VerificationError,
    VerificationPublicationError,
    load_verification_result,
    publish_protected_verification_outcome,
    publish_verification_receipt,
)
from .override import OVERRIDE_RECEIPT_MEDIA_TYPE


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
    prepare_workflow.add_argument("--retry-exhausted-builds", action="store_true")
    prepare_workflow.add_argument("--out", required=True)
    prepare_workflow.add_argument("--github-output", required=True)
    plan_products = subcommands.add_parser("plan-workflow-products")
    plan_products.add_argument("--coordination-root", required=True)
    plan_products.add_argument("--runtime-root", required=True)
    plan_products.add_argument("--kandelo-root", required=True)
    plan_products.add_argument("--tap-root", required=True)
    plan_products.add_argument("--out", required=True)
    plan_products.add_argument("--github-output", required=True)
    plan_promotion = subcommands.add_parser("plan-workflow-promotion")
    plan_promotion.add_argument("--coordination-root", required=True)
    plan_promotion.add_argument("--kandelo-root", default="")
    plan_promotion.add_argument("--tap-root", required=True)
    plan_promotion.add_argument("--require-merged", action="store_true")
    plan_promotion.add_argument("--require-history-record", action="store_true")
    plan_promotion.add_argument("--out", required=True)
    plan_promotion.add_argument("--github-output", required=True)
    publish_canonical = subcommands.add_parser("publish-workflow-canonical")
    publish_canonical.add_argument("--run-id", required=True, type=int)
    publish_canonical.add_argument("--run-attempt", required=True, type=int)
    publish_canonical.add_argument("--head-sha", required=True)
    publish_canonical.add_argument("--request-digest", required=True)
    publish_canonical.add_argument("--work-id", required=True)
    publish_canonical.add_argument("--plan-artifact-id", required=True)
    publish_canonical.add_argument("--plan-artifact-digest", required=True)
    publish_canonical.add_argument("--require-unchanged-layer", action="store_true")
    publish_canonical.add_argument("--require-history-barrier", action="store_true")
    publish_canonical.add_argument("--require-github-digest", action="store_true")
    publish_canonical.add_argument("--anonymous-readback", action="store_true")
    publish_canonical.add_argument("--immutable", action="store_true")
    publish_canonical.add_argument("--out", required=True)
    update_tap_metadata = subcommands.add_parser("update-workflow-tap-metadata")
    update_tap_metadata.add_argument("--run-id", required=True, type=int)
    update_tap_metadata.add_argument("--run-attempt", required=True, type=int)
    update_tap_metadata.add_argument("--head-sha", required=True)
    update_tap_metadata.add_argument("--request-digest", required=True)
    update_tap_metadata.add_argument("--work-id", required=True)
    update_tap_metadata.add_argument(
        "--operation",
        required=True,
        choices=("successor-activation", "formula-metadata"),
    )
    update_tap_metadata.add_argument("--plan-artifact-id", required=True)
    update_tap_metadata.add_argument("--plan-artifact-digest", required=True)
    update_tap_metadata.add_argument("--contents-only", action="store_true")
    update_tap_metadata.add_argument("--normal-push", action="store_true")
    update_tap_metadata.add_argument("--post-write-readback", action="store_true")
    update_tap_metadata.add_argument("--require-history-barrier", action="store_true")
    update_tap_metadata.add_argument("--out", required=True)
    publish_admission = subcommands.add_parser("publish-workflow-admission")
    publish_admission.add_argument("--run-id", required=True, type=int)
    publish_admission.add_argument("--run-attempt", required=True, type=int)
    publish_admission.add_argument("--head-sha", required=True)
    publish_admission.add_argument("--request-digest", required=True)
    publish_admission.add_argument("--work-id", required=True)
    publish_admission.add_argument("--plan-artifact-id", required=True)
    publish_admission.add_argument("--plan-artifact-digest", required=True)
    publish_admission.add_argument("--metadata-root", required=True)
    publish_admission.add_argument("--require-metadata-readback", action="store_true")
    publish_admission.add_argument("--require-history-barrier", action="store_true")
    publish_admission.add_argument("--require-github-digest", action="store_true")
    publish_admission.add_argument("--anonymous-readback", action="store_true")
    publish_admission.add_argument("--immutable", action="store_true")
    publish_admission.add_argument("--out", required=True)
    policy_check = subcommands.add_parser("policy-check")
    policy_check.add_argument("--tap-root", required=True)
    tap_metadata_check = subcommands.add_parser("tap-metadata-check")
    tap_metadata_check.add_argument("--tap-root", required=True)
    plan_history = subcommands.add_parser("plan-history")
    plan_history.add_argument("--tap-root", required=True)
    plan_history.add_argument("--repository", required=True)
    plan_history.add_argument("--out", required=True)
    plan_history.add_argument("--github-output", required=True)
    create_history = subcommands.add_parser("create-history-ref")
    create_history.add_argument("--tap-root", required=True)
    create_history.add_argument("--repository", required=True)
    create_history.add_argument("--plan", required=True)
    create_history.add_argument("--out", required=True)
    create_history.add_argument("--github-output", required=True)
    verify_history = subcommands.add_parser("verify-history")
    verify_history.add_argument("--tap-root", required=True)
    verify_history.add_argument("--history-root", required=True)
    verify_history.add_argument("--repository", required=True)
    verify_history.add_argument("--plan", required=True)
    verify_history.add_argument("--creation", required=True)
    verify_history.add_argument("--out", required=True)
    verify_history.add_argument("--github-output", required=True)
    verify_history.add_argument("--anonymous-readback", action="store_true")
    publish_history = subcommands.add_parser("publish-history-record")
    publish_history.add_argument("--tap-root", required=True)
    publish_history.add_argument("--repository", required=True)
    publish_history.add_argument("--record", required=True)
    publish_history.add_argument("--anonymous-readback", action="store_true")
    publish_history.add_argument("--immutable", action="store_true")
    publish_history.add_argument("--out", required=True)
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
    export_runtime_realm = subcommands.add_parser("export-runtime-realm")
    export_runtime_realm.add_argument("--coordination", required=True)
    export_runtime_realm.add_argument("--tap-root", required=True)
    export_runtime_realm.add_argument("--github-env", required=True)
    export_build_realm = subcommands.add_parser("export-build-realm")
    export_build_realm.add_argument("--coordination", required=True)
    export_build_realm.add_argument("--work-id", required=True)
    export_build_realm.add_argument("--tap-root", required=True)
    export_build_realm.add_argument("--github-env", required=True)
    export_verification_realm = subcommands.add_parser(
        "export-verification-realm"
    )
    export_verification_realm.add_argument("--coordination", required=True)
    export_verification_realm.add_argument("--work-id", required=True)
    export_verification_realm.add_argument("--tap-root", required=True)
    export_verification_realm.add_argument("--github-env", required=True)
    execute_verification = subcommands.add_parser("execute-verification-work")
    execute_verification.add_argument("--coordination", required=True)
    execute_verification.add_argument("--work-id", required=True)
    execute_verification.add_argument("--kandelo-root", required=True)
    execute_verification.add_argument("--tap-root", required=True)
    execute_verification.add_argument("--run-id", required=True, type=int)
    execute_verification.add_argument("--run-attempt", required=True, type=int)
    execute_verification.add_argument("--workflow-ref", required=True)
    execute_verification.add_argument("--out", required=True)
    execute_product = subcommands.add_parser("execute-product-work")
    execute_product.add_argument("--coordination-root", required=True)
    execute_product.add_argument("--runtime-artifact-id", required=True)
    execute_product.add_argument("--runtime-artifact-digest", required=True)
    execute_product.add_argument("--product-id", required=True)
    execute_product.add_argument("--work-id", required=True)
    execute_product.add_argument("--kandelo-root", required=True)
    execute_product.add_argument("--kandelo-policy-root", required=True)
    execute_product.add_argument("--tap-root", required=True)
    execute_product.add_argument("--validate-builder-report", action="store_true")
    execute_product.add_argument("--private-out", required=True)
    execute_product.add_argument("--out", required=True)
    execute_product_evidence = subcommands.add_parser(
        "execute-product-evidence-work"
    )
    execute_product_evidence.add_argument(
        "--host", required=True, choices=("node", "browser")
    )
    execute_product_evidence.add_argument("--definition-id", required=True)
    execute_product_evidence.add_argument("--product-id", required=True)
    execute_product_evidence.add_argument("--product-work-id", required=True)
    execute_product_evidence.add_argument("--work-id", required=True)
    execute_product_evidence.add_argument("--input-root", required=True)
    execute_product_evidence.add_argument("--kandelo-root", required=True)
    execute_product_evidence.add_argument("--kandelo-policy-root", required=True)
    execute_product_evidence.add_argument("--tap-root", required=True)
    execute_product_evidence.add_argument("--run-id", required=True, type=int)
    execute_product_evidence.add_argument(
        "--run-attempt", required=True, type=int
    )
    execute_product_evidence.add_argument("--workflow-ref", required=True)
    execute_product_evidence.add_argument("--out", required=True)
    publish_product = subcommands.add_parser(
        "publish-workflow-product-candidate"
    )
    publish_product.add_argument("--run-id", required=True, type=int)
    publish_product.add_argument("--run-attempt", required=True, type=int)
    publish_product.add_argument("--head-sha", required=True)
    publish_product.add_argument("--product-id", required=True)
    publish_product.add_argument("--work-id", required=True)
    publish_product.add_argument("--handoff-artifact-name", required=True)
    publish_product.add_argument("--private-artifact-name", required=True)
    publish_product.add_argument("--kandelo-root", required=True)
    publish_product.add_argument("--kandelo-policy-root", required=True)
    publish_product.add_argument("--validate-builder-report", action="store_true")
    publish_product.add_argument("--require-github-digest", action="store_true")
    publish_product.add_argument("--anonymous-readback", action="store_true")
    publish_product.add_argument("--immutable", action="store_true")
    publish_product.add_argument("--out", required=True)
    publish_product_evidence = subcommands.add_parser(
        "publish-workflow-product-evidence"
    )
    publish_product_evidence.add_argument("--run-id", required=True, type=int)
    publish_product_evidence.add_argument(
        "--run-attempt", required=True, type=int
    )
    publish_product_evidence.add_argument("--head-sha", required=True)
    publish_product_evidence.add_argument("--product-id", required=True)
    publish_product_evidence.add_argument("--product-work-id", required=True)
    publish_product_evidence.add_argument("--work-id", required=True)
    publish_product_evidence.add_argument("--kandelo-root", required=True)
    publish_product_evidence.add_argument(
        "--kandelo-policy-root", required=True
    )
    publish_product_evidence.add_argument(
        "--require-terminal-results", action="store_true"
    )
    publish_product_evidence.add_argument(
        "--require-github-digest", action="store_true"
    )
    publish_product_evidence.add_argument(
        "--anonymous-readback", action="store_true"
    )
    publish_product_evidence.add_argument("--immutable", action="store_true")
    publish_product_evidence.add_argument("--out", required=True)
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
    validate_projection = subcommands.add_parser("validate-admission-projection")
    validate_projection.add_argument("--tap-root", required=True)
    validate_projection.add_argument("--record", required=True)
    validate_projection.add_argument("--out", required=True)
    return parser


def _protected_tap_root(value: str) -> Path:
    root = Path(value).resolve(strict=True)
    if root != TAP_ROOT.resolve(strict=True):
        raise PolicyError("--tap-root must name this protected tap checkout")
    return root


def _policy_owned_remote(root: Path, repository: str) -> None:
    try:
        remote = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise TapMetadataError(f"cannot inspect tap origin repository: {error}") from error
    if remote.startswith("git@github.com:"):
        remote_repository = remote.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(remote)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise TapMetadataError("tap origin is not a policy-owned GitHub repository")
        remote_repository = parsed.path.removeprefix("/")
    remote_repository = remote_repository.removesuffix(".git").removesuffix("/")
    if remote_repository.lower() != repository.lower():
        raise TapMetadataError("tap origin names another repository")


def _require_clean_checkout(root: Path) -> None:
    try:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TapMetadataError(f"cannot inspect tap checkout changes: {error}") from error
    if status.stdout:
        raise TapMetadataError("protected tap checkout contains changes")


def _write_atomic_canonical(destination: Path, value: Mapping[str, Any]) -> None:
    parent = destination.parent.resolve(strict=True)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise TapMetadataError("admission projection output is not a direct file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_admission_projection(args: argparse.Namespace) -> None:
    tap_root = _protected_tap_root(args.tap_root)
    policy = load_promotion_policy(
        tap_root / "Kandelo/staging/promotion-policy.toml"
    )
    _require_clean_checkout(tap_root)
    _policy_owned_remote(tap_root, policy.tap_repository)
    try:
        record = load_canonical_mapping(
            Path(args.record).resolve(strict=True).read_bytes(),
            "promotion admission",
        )
    except OSError as error:
        raise TapMetadataError(f"cannot load promotion admission: {error}") from error
    validate_admission_record(record)
    tap_source = snapshot_tap_source(tap_root, policy.tap_repository)
    metadata_source = record["admission"]["formula_metadata_source"]
    try:
        source_tree = subprocess.run(
            [
                "git",
                "-C",
                str(tap_root),
                "rev-parse",
                f"{metadata_source['commit']}^{{tree}}",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "-C",
                str(tap_root),
                "merge-base",
                "--is-ancestor",
                str(metadata_source["commit"]),
                "HEAD",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TapMetadataError(
            "admission Formula metadata source is not an ancestor of current main"
        ) from error
    if source_tree != metadata_source["tree"]:
        raise TapMetadataError("admission Formula metadata source tree changed")
    observation = build_admission_projection_observation(
        tap_root,
        record,
        tap_source=tap_source,
    )
    _require_clean_checkout(tap_root)
    if snapshot_tap_source(tap_root, policy.tap_repository) != tap_source:
        raise TapMetadataError(
            "protected tap commit or tree changed during admission projection"
        )
    _write_atomic_canonical(Path(args.out), observation)


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
        "browser_evidence_matrix": '{"include":[]}',
        "build_matrix": '{"include":[]}',
        "mode": "observe",
        "node_evidence_matrix": '{"include":[]}',
        "product_matrix": '{"include":[]}',
        "product_mode": "observe",
        "promotion_eligible": "false",
        "request_digest": "",
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
                "request_digest": candidate.request_digest,
                "selected": "true",
                "promotion_eligible": (
                    "true" if decision.action == "observe-merged" else "false"
                ),
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


def _public_inventory_transport() -> UrllibOciTransportV1:
    username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
    token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
    return UrllibOciTransportV1(
        username=username,
        token=token,
        authenticated_public_reads=bool(token),
    )


def _scan_scheduling_inventory_with_retries(
    tap_plan: Mapping[str, Any],
    *,
    policy: Any,
    verification_tests: Any,
    scanner: Any = None,
    transport_factory: Any = None,
    sleeper: Any = None,
) -> Any:
    """Retry only explicitly transient immutable public-inventory reads."""

    scan = scan_scheduling_inventory if scanner is None else scanner
    make_transport = (
        _public_inventory_transport
        if transport_factory is None
        else transport_factory
    )
    wait = time.sleep if sleeper is None else sleeper
    for attempt in range(3):
        try:
            return scan(
                tap_plan,
                policy=policy,
                verification_tests=verification_tests,
                transport=make_transport(),
                worker_transport_factory=make_transport,
            )
        except InventoryError as error:
            cause: BaseException | None = error
            retryable = False
            while cause is not None:
                if isinstance(cause, OciPublicationError):
                    retryable = cause.retryable
                    break
                cause = cause.__cause__
            if not retryable or attempt == 2:
                raise
            wait(float(2 ** (attempt + 1)))
    raise AssertionError("public inventory retry loop did not terminate")


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
    inventory = _scan_scheduling_inventory_with_retries(
        tap_plan,
        policy=staging_policy,
        verification_tests=verification_tests,
        retry_exhausted_builds=args.retry_exhausted_builds,
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
    product_mode = load_product_evidence_activation(
        tap_root / "Kandelo/staging/product-evidence-activation.toml"
    )
    product_workflow = build_product_workflow_seed(
        request,
        reconciliation,
        activation_mode=product_mode,
    )
    (output / "coordination.json").write_bytes(canonical_bytes(bundle))
    (output / "tap-plan.json").write_bytes(canonical_bytes(bundle["tap_plan"]))
    (output / "workflow-plan.json").write_bytes(canonical_bytes(bundle["workflow"]))
    (output / "product-workflow-plan.json").write_bytes(
        canonical_bytes(product_workflow)
    )
    _write_github_outputs(
        Path(args.github_output),
        {
            "build_matrix": json.dumps(
                bundle["workflow"]["build_matrix"], separators=(",", ":"), sort_keys=True
            ),
            "mode": mode,
            "product_mode": product_mode,
            "request_digest": request_sha256,
            "product_matrix": json.dumps(
                product_workflow["product_matrix"],
                separators=(",", ":"),
                sort_keys=True,
            ),
            "node_evidence_matrix": json.dumps(
                product_workflow["node_matrix"],
                separators=(",", ":"),
                sort_keys=True,
            ),
            "browser_evidence_matrix": json.dumps(
                product_workflow["browser_matrix"],
                separators=(",", ":"),
                sort_keys=True,
            ),
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


def _plan_workflow_products(args: argparse.Namespace) -> None:
    """Scan durable public facts after Formula jobs and emit one product DAG wave."""

    tap_root = _protected_tap_root(args.tap_root)
    output = _output_directory(args.out)
    policy = load_tap_staging_policy(tap_root / "Kandelo/staging/tap-policy.toml")
    coordination_root = Path(args.coordination_root).resolve(strict=True)
    bundle = load_coordination_bundle(
        coordination_root / "coordination.json", policy=policy
    )
    request = bundle["request"]
    request_sha256 = bundle["request_sha256"]
    source = request["build_source"]
    kandelo_root = _checked_checkout_source(
        args.kandelo_root,
        repository=source["repository"],
        commit=source["commit"],
        tree=source["tree"],
    )
    if snapshot_tap_source(tap_root, policy.tap_repository) != bundle["tap_plan"][
        "tap_source"
    ]:
        raise ReconciliationError(
            "product workflow planner tap source differs from coordination"
        )

    runtime_root = Path(args.runtime_root).resolve(strict=True)
    runtime_path = runtime_root / "runtime-bundle.json"
    try:
        runtime_body = runtime_path.read_bytes()
        runtime = load_canonical_mapping(
            runtime_body, "product workflow exact runtime bundle"
        )
    except (OSError, ContractError) as error:
        raise ProductInputResolutionError(
            f"product workflow runtime bundle is invalid: {error}"
        ) from error
    product_runtime_identity(runtime, request)
    runtime_bundle_sha256 = hashlib.sha256(runtime_body).hexdigest()

    catalog_path = kandelo_root / "images/vfs/products/generated/catalog.json"
    try:
        catalog = load_canonical_mapping(
            catalog_path.read_bytes(), "product workflow catalog"
        )
    except (OSError, ContractError) as error:
        raise ProductInputResolutionError(
            f"product workflow catalog is invalid: {error}"
        ) from error
    verification_tests = load_verification_tests(
        tap_root / "Kandelo/staging/verification-tests.toml"
    )
    transport = UrllibOciTransportV1(username="", token="")
    public_inventory = scan_scheduling_inventory(
        bundle["tap_plan"],
        policy=policy,
        verification_tests=verification_tests,
        transport=transport,
    )
    formula_readiness = selected_product_formula_readiness(
        request=request,
        request_sha256=request_sha256,
        catalog=catalog,
        tap_plan=bundle["tap_plan"],
        records=public_inventory.records,
        candidate_records=public_inventory.candidate_records,
        candidate_locators=public_inventory.candidate_locators,
        source_custody_records=public_inventory.source_custody_records,
        reuse_records=public_inventory.reuse_records,
        verification_records=public_inventory.verification_records,
        verification_locators=public_inventory.verification_locators,
        verification_tests=verification_tests,
    )

    requirements = request.get("requirements")
    if not isinstance(requirements, Mapping):
        raise ReconciliationError("product workflow request lacks requirements")
    selections = []
    build_specs = {}
    for value in requirements.get("evidence", []):
        if not isinstance(value, Mapping):
            raise ReconciliationError("product workflow evidence binding is invalid")
        applicability = value.get("applicability")
        if applicability == "not-applicable":
            continue
        product_id = value.get("product_id")
        if not isinstance(product_id, str):
            raise ReconciliationError("product workflow evidence product ID is invalid")
        build_spec = select_product_input_build_spec(request, catalog, product_id)
        build_specs[product_id] = build_spec
        try:
            selection = ProductSelectionV1(
                product_id=product_id,
                manifest_sha256=build_spec["manifest_sha256"],
                applicability=applicability,
                dependency_product_ids=tuple(build_spec["dependency_product_ids"]),
                node_definition_ids=tuple(value.get("node", [])),
                browser_definition_ids=tuple(value.get("browser", [])),
            )
        except (TypeError, ValueError) as error:
            if isinstance(error, ReconciliationError):
                raise
            raise ReconciliationError(
                f"product workflow selection is invalid: {error}"
            ) from error
        selections.append(selection)
    selections.sort(
        key=lambda item: (
            0 if item.applicability == "required" else 1,
            item.product_id,
        )
    )

    lifecycle_value = bundle["lifecycle"]
    lifecycle = PullRequestLifecycleV1(
        lifecycle_value["state"],
        lifecycle_value["current_head"],
        lifecycle_value["merged_commit"],
    )
    discovered = DiscoveredRequestV1(
        request_sha256,
        Path(urlsplit(bundle["request_asset_url"]).path).name,
        bundle["request_asset_url"],
        "coordinated-request",
        request,
    )
    decision = reconcile_request(discovered, lifecycle)
    product_mode = load_product_evidence_activation(
        tap_root / "Kandelo/staging/product-evidence-activation.toml"
    )

    progress = {}
    for selection in selections:
        build_spec = build_specs[selection.product_id]
        repository = candidate_product_repository(
            owner=policy.candidate_owner,
            repository_prefix=policy.candidate_repository_prefix,
            candidate_suffix=policy.candidate_suffix,
            target_abi=request["target_abi"]["version"],
            product_id=selection.product_id,
        )
        candidates = inspect_candidate_product_repository(
            repository,
            request=request,
            request_sha256=request_sha256,
            expected_source_repository=policy.tap_repository,
            transport=transport,
        )
        current = [
            item
            for item in candidates
            if item.runtime_bundle_sha256 == runtime_bundle_sha256
        ]
        current_identities = {
            (
                item.artifact.manifest_sha256,
                item.artifact.architecture,
                item.artifact.vfs_layer_sha256,
                item.artifact.vfs_layer_bytes,
                item.artifact.builder_report_sha256,
            )
            for item in current
        }
        if len(current_identities) > 1:
            raise ProductEvidenceError(
                f"product {selection.product_id} has conflicting current candidates"
            )
        for item in current:
            if (
                item.artifact.manifest_sha256 != build_spec["manifest_sha256"]
                or item.artifact.architecture != build_spec["architecture"]
            ):
                raise ProductEvidenceError(
                    f"product {selection.product_id} candidate differs from its catalog"
                )

        aggregate_entries = []
        full_product = {
            "id": build_spec["id"],
            "manifest_path": build_spec["manifest_path"],
            "manifest_sha256": build_spec["manifest_sha256"],
            "architecture": build_spec["architecture"],
            "output": build_spec["output"],
        }
        for item in sorted(current, key=lambda value: value.locator.manifest_digest):
            aggregate_entries.extend(
                inspect_product_evidence_repository(
                    repository + "/evidence",
                    request=request,
                    request_sha256=request_sha256,
                    product=full_product,
                    candidate_product=item.locator,
                    runtime_bundle_sha256=runtime_bundle_sha256,
                    expected_source_repository=policy.tap_repository,
                    transport=transport,
                )
            )
        aggregate_identities = {
            (
                item.record["product_evidence"]["runtime_evidence_sha256"],
                item.outcome,
                item.record["common"]["promotion_state"],
            )
            for item in aggregate_entries
        }
        if len(aggregate_identities) > 1:
            raise ProductEvidenceError(
                f"product {selection.product_id} has conflicting current evidence"
            )
        progress[selection.product_id] = ProductProgressV1(
            formulae_ready=formula_readiness[selection.product_id],
            candidate_runtime_sha256=(runtime_bundle_sha256 if current else None),
            terminal_results=(),
            evidence_record_sha256=(
                min(item.record_sha256 for item in aggregate_entries)
                if aggregate_entries
                else None
            ),
        )

    wave = build_product_workflow_wave(
        request,
        decision,
        tuple(selections),
        runtime_bundle_sha256=runtime_bundle_sha256,
        progress=progress,
        activation_mode=product_mode,
    )
    (output / "product-workflow-wave.json").write_bytes(canonical_bytes(wave))
    _write_github_outputs(
        Path(args.github_output),
        {
            "browser_evidence_matrix": json.dumps(
                wave["browser_matrix"], separators=(",", ":"), sort_keys=True
            ),
            "node_evidence_matrix": json.dumps(
                wave["node_matrix"], separators=(",", ":"), sort_keys=True
            ),
            "product_matrix": json.dumps(
                wave["product_matrix"], separators=(",", ":"), sort_keys=True
            ),
        },
    )


def _plan_workflow_promotion(args: argparse.Namespace) -> None:
    """Emit one exact protected promotion artifact; disabled mode is empty."""

    if not args.require_merged or not args.require_history_record:
        raise ReconciliationError(
            "promotion planning requires merged-PR and history-record guards"
        )
    tap_root = _protected_tap_root(args.tap_root)
    output = _output_directory(args.out)
    policy = load_tap_staging_policy(tap_root / "Kandelo/staging/tap-policy.toml")
    coordination_root = Path(args.coordination_root).resolve(strict=True)
    bundle = load_coordination_bundle(
        coordination_root / "coordination.json", policy=policy
    )
    request = bundle["request"]
    request_sha256 = bundle["request_sha256"]
    tap_source = snapshot_tap_source(tap_root, policy.tap_repository)
    if bundle["tap_plan"]["tap_source"] != tap_source:
        raise ReconciliationError(
            "promotion planner tap source differs from exact coordination"
        )
    issuer_policy = load_request_issuer_policy(
        tap_root / "Kandelo/staging/request-issuers.toml",
        expected_tap=policy.tap_repository,
    )
    lifecycle = GitHubPublicClient(issuer_policy).pull_request_lifecycle(
        request["pull_request"]["number"]
    )
    asset_path = Path(urlsplit(bundle["request_asset_url"]).path)
    discovered = DiscoveredRequestV1(
        request_sha256,
        asset_path.name,
        bundle["request_asset_url"],
        asset_path.parts[-2],
        request,
    )
    reconciliation = reconcile_request(discovered, lifecycle)
    if (
        reconciliation.lifecycle.state != "merged"
        or reconciliation.action != "observe-merged"
        or not reconciliation.current_for_pull_request
    ):
        raise ReconciliationError(
            "promotion planning requires the exact merged pull-request head"
        )
    activation = load_promotion_activation(
        tap_root / "Kandelo/staging/promotion-activation.toml"
    )
    if activation.mode == "disabled":
        epoch = PromotionEpochV1(
            request_digest=request_sha256,
            history_record_sha256=None,
            activation_patch_sha256=canonical_sha256(
                {
                    "mode": activation.mode,
                    "request_sha256": request_sha256,
                    "tap_source": tap_source,
                }
            ),
            activation_record_sha256=None,
            current_tap_commit=tap_source["commit"],
            current_tap_tree=tap_source["tree"],
        )
        planned = build_promotion_workflow_plan(
            reconciliation,
            (),
            epoch=epoch,
            progress={},
            activation_mode=activation.mode,
        )
        work_details = {
            "activation": {},
            "canonical": {},
            "metadata": {},
            "admission": {},
        }
    else:
        planned, work_details = _collect_active_promotion_inputs(
            tap_root=tap_root,
            kandelo_root=args.kandelo_root,
            bundle=bundle,
            reconciliation=reconciliation,
            tap_source=tap_source,
            activation_mode=activation.mode,
        )
    document = build_promotion_plan_document(
        planned,
        tap_source=tap_source,
        work_details=work_details,
    )
    (output / "promotion-plan.json").write_bytes(canonical_bytes(document))
    _write_github_outputs(
        Path(args.github_output),
        {
            "admission_matrix": json.dumps(
                document["matrices"]["admission"],
                separators=(",", ":"),
                sort_keys=True,
            ),
            "canonical_matrix": json.dumps(
                document["matrices"]["canonical"],
                separators=(",", ":"),
                sort_keys=True,
            ),
            "metadata_matrix": json.dumps(
                document["matrices"]["metadata"],
                separators=(",", ":"),
                sort_keys=True,
            ),
            "mode": activation.mode,
            "request_digest": request_sha256,
        },
    )


def _formula_requirements_from_tap_plan(
    tap_plan: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Reconstruct selected Formula roots from the existing product authority."""

    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    products = tap_plan.get("selected_products")
    if not isinstance(products, (list, tuple)):
        raise ReconciliationError("promotion tap plan products are not an array")
    for product in products:
        if not isinstance(product, Mapping):
            raise ReconciliationError("promotion tap plan product is not an object")
        product_id = product.get("id")
        roots = product.get("formula_roots")
        if not isinstance(product_id, str) or not isinstance(roots, (list, tuple)):
            raise ReconciliationError("promotion tap plan product roots are invalid")
        for root in roots:
            if not isinstance(root, Mapping):
                raise ReconciliationError("promotion Formula root is not an object")
            key = (root.get("tap"), root.get("formula"), root.get("architecture"))
            if not all(isinstance(value, str) for value in key):
                raise ReconciliationError("promotion Formula root identity is invalid")
            grouped.setdefault(key, []).append(
                {
                    "product_id": product_id,
                    "materialization": root.get("materialization"),
                }
            )
    requirements = [
        {
            "tap": tap,
            "formula": formula,
            "architecture": architecture,
            "uses": sorted(
                uses,
                key=lambda item: (item["product_id"], item["materialization"]),
            ),
        }
        for (tap, formula, architecture), uses in sorted(grouped.items())
    ]
    try:
        return load_formula_requirements(canonical_bytes(requirements))
    except PlanError as error:
        raise ReconciliationError(
            f"promotion Formula roots cannot be reconstructed: {error}"
        ) from error


def _select_exact_history_record(
    records: tuple[FetchedOciRecordV1, ...],
    *,
    target_abi: int,
    planned_tap_source: Mapping[str, Any],
) -> FetchedOciRecordV1:
    """Select one durable history record for the exact planned N -> N+1 epoch."""

    matches: list[FetchedOciRecordV1] = []
    for fetched in records:
        try:
            record = load_canonical_mapping(fetched.config.body, "ABI history record")
            validate_abi_history_record(record)
        except (ContractError, TapRecordError, ValueError) as error:
            raise ReconciliationError(
                f"public ABI history record is invalid: {error}"
            ) from error
        plan = record["plan"]
        if (
            plan["source_abi"] + 1 == target_abi
            and plan["successor_abi"] == target_abi
            and plan["preactivation_tap_commit"] == planned_tap_source.get("commit")
            and plan["preactivation_tap_tree"] == planned_tap_source.get("tree")
        ):
            matches.append(fetched)
    if len(matches) != 1:
        raise ReconciliationError(
            "promotion requires one exact protected ABI history record"
        )
    return matches[0]


def _fetch_exact_history_record(
    *,
    policy: Any,
    target_abi: int,
    planned_tap_source: Mapping[str, Any],
    expected_digest: str | None = None,
    transport: UrllibOciTransportV1,
) -> FetchedOciRecordV1:
    source_abi = target_abi - 1
    if source_abi < 0:
        raise ReconciliationError("promotion target ABI has no predecessor")
    repository = history_record_repository(policy.tap_repository, source_abi)
    if expected_digest is not None:
        if (
            not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or any(character not in "0123456789abcdef" for character in expected_digest)
        ):
            raise ReconciliationError(
                "activated promotion history digest is invalid"
            )
        reference = "ghcr.io/" + repository + "@sha256:" + expected_digest
        fetched = fetch_public_record(
            {
                "repository": "ghcr.io/" + repository,
                "digest": "sha256:" + expected_digest,
                "immutable_reference": reference,
            },
            transport=transport,
            expected_artifact_type=HISTORY_RECORD_MEDIA_TYPE,
            required_layer_roles=("immutable-record-bytes",),
        )
        if fetched.digest != "sha256:" + expected_digest:
            raise ReconciliationError(
                "activated promotion history record identity changed"
            )
        return fetched
    fetched = tuple(
        fetch_public_record(
            locator,
            transport=transport,
            expected_artifact_type=HISTORY_RECORD_MEDIA_TYPE,
            required_layer_roles=("immutable-record-bytes",),
        )
        for locator in list_public_record_locators(
            repository,
            transport=transport,
            max_records=256,
        )
    )
    return _select_exact_history_record(
        fetched,
        target_abi=target_abi,
        planned_tap_source=planned_tap_source,
    )


def _history_epoch_authority(
    history: FetchedOciRecordV1,
    *,
    policy: Any,
    target_abi: int,
) -> tuple[dict[str, str], str]:
    """Recover the immutable preactivation source independently of current main."""

    try:
        record = load_canonical_mapping(history.config.body, "ABI history record")
        validate_abi_history_record(record)
    except (ContractError, TapRecordError, ValueError) as error:
        raise ReconciliationError(
            f"public ABI history record is invalid: {error}"
        ) from error
    plan = record["plan"]
    source_abi = plan["source_abi"]
    branch = policy.historical_branch_prefix + str(source_abi)
    expected_repository = (
        "ghcr.io/"
        + history_record_repository(policy.tap_repository, source_abi)
    )
    if (
        plan["successor_abi"] != target_abi
        or source_abi + 1 != target_abi
        or plan["branch"] != branch
        or history.repository != expected_repository
    ):
        raise ReconciliationError(
            "public ABI history record names another successor epoch"
        )
    return (
        {
            "repository": policy.tap_repository,
            "commit": plan["preactivation_tap_commit"],
            "tree": plan["preactivation_tap_tree"],
        },
        branch,
    )


def _public_locator(fetched: FetchedOciRecordV1) -> dict[str, str]:
    return {
        "repository": fetched.repository,
        "digest": fetched.digest,
        "immutable_reference": fetched.immutable_reference,
    }


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"JSON object repeats field {key!r}")
        value[key] = child
    return value


def _fetch_candidate_record(
    locator: Mapping[str, Any], *, transport: UrllibOciTransportV1
) -> FetchedOciRecordV1:
    return fetch_public_record(
        locator,
        transport=transport,
        expected_artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
        required_layer_roles=(
            "bottle-layer",
            "bottle-metadata",
            "vfs-composition-descriptor",
            "bottle-contract",
        ),
    )


def _fetch_candidate_reuse(
    locator: Mapping[str, Any], *, transport: UrllibOciTransportV1
) -> FetchedOciRecordV1:
    return fetch_public_record(
        locator,
        transport=transport,
        expected_artifact_type=CANDIDATE_REUSE_RECORD_MEDIA_TYPE,
        required_layer_roles=("immutable-record-bytes",),
    )


def _fetch_candidate_custody(
    candidate: FetchedOciRecordV1, *, transport: UrllibOciTransportV1
) -> FetchedOciRecordV1:
    record = load_canonical_mapping(candidate.config.body, "promotion candidate")
    matches = [
        item["artifact"]
        for item in record["candidate"]["normalized_components"]
        if item["id"] == "source-custody"
    ]
    if len(matches) != 1:
        raise ReconciliationError("promotion candidate has no exact custody record")
    artifact = matches[0]
    reference = artifact["immutable_reference"]
    if not isinstance(reference, str) or "@sha256:" not in reference:
        raise ReconciliationError("promotion custody reference is not immutable")
    repository, digest = reference.rsplit("@", 1)
    locator = {
        "repository": repository,
        "digest": digest,
        "immutable_reference": reference,
    }
    first = fetch_public_record(
        locator,
        transport=transport,
        expected_artifact_type=SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
        required_layer_roles=(),
    )
    try:
        custody = load_source_custody_manifest(first.config.body)
    except CustodyError as error:
        raise ReconciliationError(
            f"promotion source custody is invalid: {error}"
        ) from error
    roles = tuple(
        sorted(
            {
                f"{item[identity]}-{suffix}"
                for collection, identity in (
                    (custody["sources"], "role"),
                    (custody["submodules"], "id"),
                )
                for item in collection
                for suffix in ("bundle", "tree")
            }
        )
    )
    fetched = fetch_public_record(
        locator,
        transport=transport,
        expected_artifact_type=SOURCE_CUSTODY_MANIFEST_MEDIA_TYPE,
        required_layer_roles=roles,
    )
    if (
        fetched.digest.removeprefix("sha256:") != artifact["sha256"]
        or len(fetched.manifest) != artifact["bytes"]
    ):
        raise ReconciliationError("promotion custody differs from candidate link")
    return fetched


def _select_current_candidate_fact(
    inventory: Any,
    *,
    request_sha256: str,
    subject: str,
    contract_sha256: str,
) -> Any | None:
    matches = [
        fact
        for fact in inventory.records.candidates
        if fact.request_sha256 == request_sha256
        and fact.subject == subject
        and fact.contract_sha256 == contract_sha256
    ]
    descriptor_matches = []
    for fact in matches:
        record = inventory.candidate_records.get(fact.record_sha256)
        if record is None:
            raise ReconciliationError(
                "promotion candidate record is missing from the public inventory"
            )
        component_ids = {
            component["id"]
            for component in record["candidate"]["normalized_components"]
        }
        descriptor_capable = "vfs-composition-descriptor" in component_ids
        if fact.descriptor_capable != descriptor_capable:
            raise ReconciliationError(
                "promotion candidate descriptor capability changed across inventory"
            )
        if fact.descriptor_capable:
            descriptor_matches.append(fact)
    matches = descriptor_matches
    if len({fact.bottle_layer_sha256 for fact in matches}) > 1:
        raise ReconciliationError(
            "promotion candidates conflict for one exact contract"
        )
    return min(
        matches,
        key=lambda fact: (
            fact.record_sha256,
            fact.binding_record_sha256 or fact.record_sha256,
        ),
        default=None,
    )


def _selected_verification_receipts(
    inventory: Any,
    *,
    subject: str,
    candidate_record_sha256: str,
    verification_tests: tuple[Any, ...],
    transport: UrllibOciTransportV1,
) -> tuple[FetchedOciRecordV1, ...] | None:
    selected = []
    for definition in verification_tests:
        for host in definition.hosts:
            matches = [
                fact
                for fact in inventory.records.verifications
                if fact.subject == subject
                and fact.candidate_record_sha256 == candidate_record_sha256
                and fact.test_definition_sha256 == definition.sha256
                and fact.host == host
            ]
            successes = [fact for fact in matches if fact.outcome == "success"]
            if successes:
                fact = min(successes, key=lambda item: item.record_sha256)
            elif matches:
                ordinal = max(item.attempt_ordinal for item in matches)
                fact = min(
                    (item for item in matches if item.attempt_ordinal == ordinal),
                    key=lambda item: (item.completed_at, item.record_sha256),
                )
            else:
                return None
            locator = inventory.verification_locators.get(fact.record_sha256)
            if not isinstance(locator, Mapping):
                raise ReconciliationError(
                    "promotion verification locator is missing"
                )
            selected.append(
                fetch_public_record(
                    locator,
                    transport=transport,
                    expected_artifact_type=VERIFICATION_RECEIPT_MEDIA_TYPE,
                    required_layer_roles=(),
                )
            )
    return tuple(selected)


def _fetch_candidate_overrides(
    candidate: FetchedOciRecordV1, *, transport: UrllibOciTransportV1
) -> tuple[FetchedOciRecordV1, ...]:
    repository = candidate.repository.removeprefix("ghcr.io/") + "/receipts/overrides"
    fetched = tuple(
        fetch_public_record(
            locator,
            transport=transport,
            expected_artifact_type=OVERRIDE_RECEIPT_MEDIA_TYPE,
            required_layer_roles=(),
        )
        for locator in list_public_record_locators(
            repository,
            transport=transport,
            max_records=1024,
        )
    )
    try:
        candidate_record = load_canonical_mapping(
            candidate.config.body, "override candidate record"
        )
        validate_candidate_record(candidate_record)
    except (ContractError, TapRecordError, ValueError) as error:
        raise ReconciliationError(
            f"override candidate record is invalid: {error}"
        ) from error
    candidate_digest = candidate.digest.removeprefix("sha256:")
    common = candidate_record["common"]
    bottle = candidate_record["candidate"]["bottle_layer"]
    selected: list[FetchedOciRecordV1] = []
    for receipt in fetched:
        try:
            identity = promotion_override_identity(receipt)
        except PromotionError as error:
            raise ReconciliationError(
                f"public override receipt is invalid: {error}"
            ) from error
        if receipt.repository != "ghcr.io/" + repository:
            raise ReconciliationError("public override escaped its exact repository")
        if identity == {
            "request_digest": common["request_sha256"],
            "request_source": common["source"],
            "candidate_digest": candidate_digest,
            "bottle": bottle,
        }:
            selected.append(receipt)
    return tuple(selected)


def _current_dependency_layers(
    tap_root: Path, formula_plan: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    layers: dict[str, dict[str, Any]] = {}
    for dependency in formula_plan["direct_dependencies"]:
        subject = exact_formula_subject(
            dependency["formula"], dependency["architecture"]
        )
        sidecar_path = tap_root / f"Kandelo/formula/{dependency['formula']}.json"
        try:
            body = sidecar_path.read_bytes()
            if not 1 <= len(body) <= 32 * 1024 * 1024:
                raise ValueError("sidecar size is outside its bound")
            sidecar = json.loads(
                body.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_json_object,
            )
            if not isinstance(sidecar, Mapping):
                raise ValueError("sidecar is not an object")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ReconciliationError(
                f"promotion dependency metadata is invalid: {error}"
            ) from error
        matches = [
            item
            for item in sidecar["bottles"]
            if item.get("arch") == dependency["architecture"]
            and item.get("status") == "success"
        ]
        if len(matches) != 1:
            raise ReconciliationError(
                "promotion dependency has no exact selected bottle"
            )
        bottle = matches[0]
        layers[subject] = {
            "sha256": bottle["sha256"],
            "bytes": bottle["bytes"],
            "immutable_reference": bottle["url"],
        }
    return layers


def _canonical_progress(
    decision: Any,
    *,
    candidate: FetchedOciRecordV1,
    policy: Any,
    transport: UrllibOciTransportV1,
) -> tuple[Any, PromotionProgressV1]:
    expected = expected_canonical_publication(
        decision,
        candidate=candidate,
        policy=policy,
    )
    repository = expected.locator.repository.removeprefix("ghcr.io/")
    locators = list_public_record_locators(
        repository,
        transport=transport,
        max_records=4096,
    )
    if not any(locator["digest"] == expected.locator.digest for locator in locators):
        return expected, PromotionProgressV1()
    readback = read_canonical_publication(
        decision,
        candidate=candidate,
        policy=policy,
        transport=transport,
    )
    return readback, PromotionProgressV1(
        canonical_manifest_sha256=readback.artifact["sha256"],
        canonical_readback_sha256=(
            readback.locator.anonymous_readback_sha256
        ),
    )


def _admission_progress(
    current: PromotionProgressV1,
    *,
    tap_root: Path,
    decision: Any,
    canonical: Any,
    policy: Any,
    history_record_sha256: str,
    preactivation_tap_source: Mapping[str, Any],
    target_abi: int,
    formula: str,
    transport: UrllibOciTransportV1,
) -> PromotionProgressV1:
    if current.canonical_manifest_sha256 is None:
        return current
    matches: list[tuple[str, Mapping[str, Any]]] = []
    stale_matches: list[tuple[str, Mapping[str, Any]]] = []
    repository = admission_repository(policy, target_abi, formula)
    for locator in list_public_record_locators(
        repository,
        transport=transport,
        max_records=4096,
    ):
        fetched = fetch_public_record(
            locator,
            transport=transport,
            expected_artifact_type=ADMISSION_RECORD_MEDIA_TYPE,
            required_layer_roles=("immutable-record-bytes",),
        )
        record = load_canonical_mapping(fetched.config.body, "promotion admission")
        try:
            validate_admission_record(record)
        except TapRecordError as error:
            raise ReconciliationError(
                f"public promotion admission is invalid: {error}"
            ) from error
        admission = record["admission"]
        if (
            record["common"]["request_sha256"] == decision.request_digest
            and admission["candidate_record_sha256"]
            == decision.candidate_record_digest
            and admission["candidate_binding_sha256"]
            == decision.candidate_binding_digest
            and admission["abi_history_record_sha256"]
            == history_record_sha256
            and canonical_bytes(admission["preactivation_tap_source"])
            == canonical_bytes(preactivation_tap_source)
            and admission["merged_pull_request"]
            == dict(decision.merged_pull_request)
            and admission["canonical"] == dict(canonical.artifact)
        ):
            update_value = admission["formula_metadata_update"]
            update = FormulaMetadataUpdateV1(
                formula=update_value["formula"],
                architecture=update_value["architecture"],
                expected_main_commit=update_value["expected_main_commit"],
                expected_normalized_formula_sha256=update_value[
                    "expected_normalized_formula_sha256"
                ],
                expected_generated_metadata_sha256=update_value[
                    "expected_generated_metadata_sha256"
                ],
                allowed_paths=tuple(update_value["allowed_paths"]),
                link_manifest_path=update_value["link_manifest_path"],
                link_manifest_sha256=update_value["link_manifest_sha256"],
                canonical_manifest_digest=update_value[
                    "canonical_manifest_digest"
                ],
                bottle_layer_sha256=update_value["bottle_layer_sha256"],
                bottle_layer_bytes=update_value["bottle_layer_bytes"],
                target_abi=update_value["target_abi"],
            )
            try:
                validate_formula_admission_projection(tap_root, update)
            except TapMetadataError:
                stale_matches.append(
                    (fetched.digest.removeprefix("sha256:"), record)
                )
                continue
            matches.append((fetched.digest.removeprefix("sha256:"), record))
    if not matches:
        if not stale_matches:
            return current
        stale_identities = {
            canonical_sha256(
                {
                    "source": record["admission"]["formula_metadata_source"],
                    "formula_metadata_update": record["admission"][
                        "formula_metadata_update"
                    ],
                }
            )
            for _digest_value, record in stale_matches
        }
        if len(stale_identities) != 1:
            raise ReconciliationError(
                "stale promotion admissions conflict on landed metadata"
            )
        return PromotionProgressV1(
            canonical_manifest_sha256=current.canonical_manifest_sha256,
            canonical_readback_sha256=current.canonical_readback_sha256,
            stale_admission_record_sha256=min(
                digest for digest, _record in stale_matches
            ),
        )
    identities = {
        canonical_sha256(
            {
                "source": record["admission"]["formula_metadata_source"],
                "formula_metadata_update": record["admission"][
                    "formula_metadata_update"
                ],
            }
        )
        for _digest_value, record in matches
    }
    if len(identities) != 1:
        raise ReconciliationError("promotion admissions conflict on landed metadata")
    digest, record = min(matches, key=lambda item: item[0])
    admission = record["admission"]
    source = admission["formula_metadata_source"]
    update = admission["formula_metadata_update"]
    return PromotionProgressV1(
        canonical_manifest_sha256=current.canonical_manifest_sha256,
        canonical_readback_sha256=current.canonical_readback_sha256,
        metadata_commit=source["commit"],
        metadata_tree=source["tree"],
        metadata_update_sha256=canonical_sha256(update),
        metadata_readback_sha256=canonical_sha256(
            {"source": source, "formula_metadata_update": update}
        ),
        admission_record_sha256=digest,
    )


def _collect_active_promotion_inputs(
    *,
    tap_root: Path,
    kandelo_root: str,
    bundle: Mapping[str, Any],
    reconciliation: ReconciliationDecisionV1,
    tap_source: Mapping[str, Any],
    activation_mode: str,
) -> tuple[Any, dict[str, dict[str, Mapping[str, Any]]]]:
    """Reconstruct one promotion wave from exact public facts and tap state."""

    request = bundle["request"]
    request_sha256 = bundle["request_sha256"]
    tap_plan = bundle["tap_plan"]
    target_abi = request["target_abi"]["version"]
    target_snapshot = request["target_abi"]["snapshot_sha256"]
    promotion_policy = load_promotion_policy(
        tap_root / "Kandelo/staging/promotion-policy.toml"
    )
    staging_policy = load_tap_staging_policy(
        tap_root / "Kandelo/staging/tap-policy.toml"
    )
    verification_tests = load_verification_tests(
        tap_root / "Kandelo/staging/verification-tests.toml"
    )
    transport = UrllibOciTransportV1(username="", token="")
    state = load_abi_state(tap_root / "Kandelo/abi-state.json")
    history = _fetch_exact_history_record(
        policy=promotion_policy,
        target_abi=target_abi,
        planned_tap_source=tap_plan["tap_source"],
        expected_digest=(
            None
            if state.activation is None
            else state.activation.abi_history_record_digest
        ),
        transport=transport,
    )
    history_tap_source, branch = _history_epoch_authority(
        history,
        policy=promotion_policy,
        target_abi=target_abi,
    )
    history_snapshot = GitHubHistoryClient(
        promotion_policy.tap_repository,
        os.environ.get("GITHUB_TOKEN", ""),
    ).protection_snapshot(branch, phase="postcreate")
    merge = {
        "repository": request["pull_request"]["repository"],
        "number": request["pull_request"]["number"],
        "head": request["build_source"]["commit"],
        "merge_commit": reconciliation.lifecycle.merged_commit,
    }
    if merge["merge_commit"] is None:
        raise ReconciliationError("promotion merge fact lost its commit")
    history_digest = history.digest.removeprefix("sha256:")
    history_locator = _public_locator(history)

    if state.current_abi == target_abi - 1 and state.activation is None:
        patch = prepare_successor_activation_patch(
            tap_root=tap_root,
            history=history,
            history_protection_snapshot=history_snapshot,
            current_tap_source=tap_source,
            request_digest=request_sha256,
            merged_pull_request=merge,
            target_abi=target_abi,
            target_snapshot_sha256=target_snapshot,
            policy=promotion_policy,
        )
        patch_document = metadata_patch_document(patch, formula_update=None)
        epoch = PromotionEpochV1(
            request_digest=request_sha256,
            history_record_sha256=history_digest,
            activation_patch_sha256=canonical_sha256(patch_document),
            activation_record_sha256=None,
            current_tap_commit=tap_source["commit"],
            current_tap_tree=tap_source["tree"],
        )
        planned = build_promotion_workflow_plan(
            reconciliation,
            (),
            epoch=epoch,
            progress={},
            activation_mode=activation_mode,
        )
        details: dict[str, dict[str, Mapping[str, Any]]] = {
            "activation": {},
            "canonical": {},
            "metadata": {},
            "admission": {},
        }
        for work in planned.activation_work:
            details["activation"][work["work_id"]] = {
                "operation": "successor-activation",
                "request_sha256": request_sha256,
                "history_locator": history_locator,
                "history_protection_snapshot": history_snapshot,
                "metadata_patch": patch_document,
            }
        return planned, details

    activation = state.activation
    if (
        state.current_abi != target_abi
        or state.current_snapshot_sha256 != target_snapshot
        or activation is None
        or activation.request_digest != request_sha256
        or dict(activation.merged_pull_request) != merge
        or activation.merge_commit != merge["merge_commit"]
        or activation.prior_abi != target_abi - 1
        or activation.prior_branch != branch
        or activation.abi_history_record_digest != history_digest
    ):
        raise ReconciliationError(
            "current tap ABI state differs from exact successor activation"
        )
    activation_identity = asdict(activation)
    epoch = PromotionEpochV1(
        request_digest=request_sha256,
        history_record_sha256=history_digest,
        activation_patch_sha256=canonical_sha256(
            {
                "target_abi": request["target_abi"],
                "activation": activation_identity,
            }
        ),
        activation_record_sha256=canonical_sha256(activation_identity),
        current_tap_commit=tap_source["commit"],
        current_tap_tree=tap_source["tree"],
    )

    requirements = _formula_requirements_from_tap_plan(tap_plan)
    current_plan = plan_exact_tap_request(
        tap_root,
        request,
        request_digest=request_sha256,
        request_asset_url=bundle["request_asset_url"],
        formula_requirements=requirements,
        tap_repository=promotion_policy.tap_repository,
    )
    original_formulae = {
        exact_formula_subject(
            item["identity"]["name"], item["identity"]["architecture"]
        ): item
        for item in tap_plan["formulae"]
    }
    current_formulae = {
        exact_formula_subject(
            item["identity"]["name"], item["identity"]["architecture"]
        ): item
        for item in current_plan["formulae"]
    }
    if set(original_formulae) != set(current_formulae):
        raise ReconciliationError("current promotion Formula graph changed subjects")
    inventory = scan_scheduling_inventory(
        tap_plan,
        policy=staging_policy,
        verification_tests=verification_tests,
        transport=transport,
    )
    exact_kandelo_root = _checked_checkout_source(
        kandelo_root,
        repository=request["build_source"]["repository"],
        commit=request["build_source"]["commit"],
        tree=request["build_source"]["tree"],
    )

    contexts: dict[str, dict[str, Any]] = {}
    for subject in tap_plan["required_subjects"] + tap_plan["background_subjects"]:
        formula_plan = original_formulae[subject]
        contract_sha256 = formula_plan["contract_sha256"]
        if not isinstance(contract_sha256, str):
            continue
        fact = _select_current_candidate_fact(
            inventory,
            request_sha256=request_sha256,
            subject=subject,
            contract_sha256=contract_sha256,
        )
        if fact is None:
            continue
        locator = inventory.candidate_locators.get(fact.record_sha256)
        if not isinstance(locator, Mapping):
            raise ReconciliationError("promotion candidate locator is missing")
        candidate = _fetch_candidate_record(locator, transport=transport)
        reuse_locator = None
        candidate_reuse = None
        if fact.binding_record_sha256 is not None:
            reuse_locator = inventory.reuse_locators.get(
                fact.binding_record_sha256
            )
            if not isinstance(reuse_locator, Mapping):
                raise ReconciliationError(
                    "promotion candidate reuse locator is missing"
                )
            candidate_reuse = _fetch_candidate_reuse(
                reuse_locator, transport=transport
            )
        receipts = _selected_verification_receipts(
            inventory,
            subject=subject,
            candidate_record_sha256=fact.record_sha256,
            verification_tests=verification_tests,
            transport=transport,
        )
        if receipts is None:
            continue
        custody = _fetch_candidate_custody(candidate, transport=transport)
        decision = evaluate_promotion(
            request=request,
            request_digest=request_sha256,
            merge_fact={**merge, "state": "merged"},
            tap_plan=tap_plan,
            tap_plan_digest=canonical_sha256(tap_plan),
            candidate=candidate,
            candidate_reuse=candidate_reuse,
            source_custody=custody,
            verification_receipts=receipts,
            override_receipts=(
                ()
                if candidate_reuse is not None
                else _fetch_candidate_overrides(candidate, transport=transport)
            ),
            history=history,
            history_protection_snapshot=history_snapshot,
            current_tap_source=tap_source,
            current_formula=current_formulae[subject],
            current_dependency_layers=_current_dependency_layers(
                tap_root, current_formulae[subject]
            ),
            policy=promotion_policy,
            expected_request_policy=request["issuance"],
            verification_tests=verification_tests,
            history_tap_source=history_tap_source,
        )
        formula, architecture = parse_formula_subject(subject, "promotion subject")
        dependencies = tuple(
            sorted(
                exact_formula_subject(item["formula"], item["architecture"])
                for item in formula_plan["direct_dependencies"]
            )
        )
        canonical, progress = _canonical_progress(
            decision,
            candidate=candidate,
            policy=promotion_policy,
            transport=transport,
        )
        progress = _admission_progress(
            progress,
            tap_root=tap_root,
            decision=decision,
            canonical=canonical,
            policy=promotion_policy,
            history_record_sha256=history_digest,
            preactivation_tap_source=history_tap_source,
            target_abi=target_abi,
            formula=formula,
            transport=transport,
        )
        prepared = prepare_admission(
            decision,
            candidate=candidate,
            candidate_reuse=candidate_reuse,
            canonical_publication=canonical,
            preactivation_tap_source=history_tap_source,
            abi_history_record_sha256=history_digest,
            policy=promotion_policy,
        )
        metadata = prepare_formula_metadata_patch(
            tap_root=tap_root,
            prepared=prepared,
            history=history,
            history_protection_snapshot=history_snapshot,
            current_tap_source=tap_source,
            expected_generated_metadata_sha256=(
                formula_generated_metadata_sha256(tap_root, formula)
            ),
            guest_layout_bytes=(
                exact_kandelo_root / "homebrew/kandelo-guest-layout.json"
            ).read_bytes(),
            policy=promotion_policy,
        )
        metadata_patch = metadata.patch
        metadata_update = metadata.update
        formula_metadata_base_source = dict(tap_source)
        if (
            not metadata_patch.files
            and progress.admission_record_sha256 is None
        ):
            if progress.canonical_manifest_sha256 is None:
                raise ReconciliationError(
                    "landed Formula metadata has no canonical public readback"
                )
            try:
                recovered = recover_landed_formula_metadata_commit(
                    tap_root, current_update=metadata_update
                )
            except TapMetadataError as error:
                raise ReconciliationError(
                    f"landed Formula metadata cannot resume admission: {error}"
                ) from error
            metadata_patch = recovered.patch
            metadata_update = recovered.update
            formula_metadata_base_source = dict(recovered.base_source)
            landed_source = dict(recovered.landed_source)
            progress = PromotionProgressV1(
                canonical_manifest_sha256=progress.canonical_manifest_sha256,
                canonical_readback_sha256=progress.canonical_readback_sha256,
                metadata_commit=landed_source["commit"],
                metadata_tree=landed_source["tree"],
                metadata_update_sha256=canonical_sha256(
                    asdict(metadata_update)
                ),
                metadata_readback_sha256=canonical_sha256(
                    {
                        "source": landed_source,
                        "formula_metadata_update": asdict(metadata_update),
                    }
                ),
            )
        contexts[subject] = {
            "subject": PromotionSubjectV1(
                decision,
                formula_plan["work_class"],
                dependencies,
            ),
            "progress": progress,
            "candidate": candidate,
            "candidate_locator": dict(locator),
            "candidate_reuse_locator": (
                None if reuse_locator is None else dict(reuse_locator)
            ),
            "canonical": canonical,
            "metadata_patch": metadata_patch_document(
                metadata_patch,
                formula_update=metadata_update,
            ),
            "history_locator": history_locator,
            "history_snapshot": history_snapshot,
            "formula_metadata_base_source": formula_metadata_base_source,
        }

    # A subject is promotable only when its complete dependency closure has
    # exact current-request authority. Missing background facts stay buildable
    # by the ordinary reconciler and enter a later promotion wave.
    changed = True
    while changed:
        changed = False
        for subject, context in list(contexts.items()):
            if any(
                dependency not in contexts
                for dependency in context["subject"].dependency_subjects
            ):
                del contexts[subject]
                changed = True

    subjects = tuple(contexts[key]["subject"] for key in sorted(contexts))
    progress = {key: contexts[key]["progress"] for key in sorted(contexts)}
    planned = build_promotion_workflow_plan(
        reconciliation,
        subjects,
        epoch=epoch,
        progress=progress,
        activation_mode=activation_mode,
    )
    details = {
        "activation": {},
        "canonical": {},
        "metadata": {},
        "admission": {},
    }
    for stage, work_items in (
        ("canonical", planned.canonical_work),
        ("metadata", planned.metadata_work),
        ("admission", planned.admission_work),
    ):
        for work in work_items:
            context = contexts[work["formula_subject"]]
            detail = {
                "decision": asdict(context["subject"].decision),
                "candidate_locator": context["candidate_locator"],
                "candidate_reuse_locator": context[
                    "candidate_reuse_locator"
                ],
                "canonical": {
                    "locator": asdict(context["canonical"].locator),
                    "artifact": dict(context["canonical"].artifact),
                },
                "history_locator": context["history_locator"],
                "history_protection_snapshot": context["history_snapshot"],
                "formula_metadata_base_source": context[
                    "formula_metadata_base_source"
                ],
            }
            if stage == "metadata":
                detail["metadata_patch"] = context["metadata_patch"]
                if "canonical_work_id" in work:
                    detail["canonical_work_id"] = work["canonical_work_id"]
            if stage == "admission":
                detail["metadata_patch"] = context["metadata_patch"]
                if "canonical_work_id" in work:
                    detail["canonical_work_id"] = work["canonical_work_id"]
                if "metadata_work_id" in work:
                    detail["metadata_work_id"] = work["metadata_work_id"]
            details[stage][work["work_id"]] = detail
    return planned, details


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
    registry_username: str | None = None,
    registry_token: str | None = None,
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
        if (registry_username is None) != (registry_token is None):
            raise PolicyError("candidate registry credentials are incomplete")
        username = (
            os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
            if registry_username is None
            else registry_username
        )
        token = (
            os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
            if registry_token is None
            else registry_token
        )
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


def _export_runtime_realm(args: argparse.Namespace) -> None:
    """Export the protected exact-source/runtime identity without shell parsing."""

    tap_root = _protected_tap_root(args.tap_root)
    policy = load_tap_staging_policy(
        tap_root / "Kandelo/staging/tap-policy.toml"
    )
    coordination = Path(args.coordination)
    if coordination.is_dir():
        coordination = coordination / "coordination.json"
    bundle = load_coordination_bundle(coordination, policy=policy)
    request = bundle["request"]
    _write_github_outputs(
        Path(args.github_env),
        {
            "KANDELO_ABI_STAGING_BUILD_POLICY_SHA256": request["issuance"][
                "policy_sha256"
            ],
            "KANDELO_ABI_STAGING_SNAPSHOT_SHA256": request["target_abi"][
                "snapshot_sha256"
            ],
            "KANDELO_ABI_STAGING_SOURCE_TREE": request["build_source"]["tree"],
            "KANDELO_ABI_STAGING_TARGET_ABI": str(request["target_abi"]["version"]),
        },
    )


def _export_formula_realm(
    args: argparse.Namespace,
    *,
    select_work: Any,
    subject_label: str,
) -> None:
    """Export one protected Formula identity without shell parsing."""

    tap_root = _protected_tap_root(args.tap_root)
    policy = load_tap_staging_policy(
        tap_root / "Kandelo/staging/tap-policy.toml"
    )
    coordination = Path(args.coordination)
    if coordination.is_dir():
        coordination = coordination / "coordination.json"
    bundle = load_coordination_bundle(coordination, policy=policy)
    work = select_work(bundle, args.work_id)
    formula, architecture = parse_formula_subject(
        work["subject"], subject_label
    )
    tap_source = bundle["tap_plan"]["tap_source"]
    repository = tap_source["repository"]
    owner, repository_name = repository.split("/", 1)
    if not repository_name.startswith("homebrew-"):
        raise ExecutionError("build tap repository is not conventional Homebrew")
    _write_github_outputs(
        Path(args.github_env),
        {
            "KANDELO_ABI_STAGING_ARCHITECTURE": architecture,
            "KANDELO_ABI_STAGING_FORMULA": formula,
            "KANDELO_ABI_STAGING_TAP_COMMIT": tap_source["commit"],
            "KANDELO_ABI_STAGING_TAP_NAME": (
                f"{owner}/{repository_name.removeprefix('homebrew-')}"
            ),
            "KANDELO_ABI_STAGING_TAP_REPOSITORY": repository,
        },
    )


def _export_build_realm(args: argparse.Namespace) -> None:
    """Export one protected Formula build identity without shell parsing."""

    _export_formula_realm(
        args,
        select_work=select_build_work,
        subject_label="build work subject",
    )


def _export_verification_realm(args: argparse.Namespace) -> None:
    """Export one protected Formula verification identity."""

    _export_formula_realm(
        args,
        select_work=select_verification_work,
        subject_label="verification work subject",
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


def _product_subprocess_environment(
    root: Path, *, ambient: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Create one private positive-allowlist environment for candidate work."""

    source = os.environ if ambient is None else ambient
    environment_root = Path(root)
    try:
        if environment_root.is_symlink():
            raise ProductInputResolutionError(
                "product subprocess environment root must not be a symlink"
            )
        environment_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = environment_root.lstat()
    except OSError as error:
        raise ProductInputResolutionError(
            f"product subprocess environment root is unavailable: {error}"
        ) from error
    if not environment_root.is_dir() or stat.S_ISLNK(metadata.st_mode):
        raise ProductInputResolutionError(
            "product subprocess environment root must be a real directory"
        )
    environment_root = environment_root.resolve(strict=True)
    private_paths = {
        "HOME": environment_root / "home",
        "TMPDIR": environment_root / "tmp",
        "XDG_CACHE_HOME": environment_root / "cache",
        "XDG_CONFIG_HOME": environment_root / "config",
        "CARGO_HOME": environment_root / "cargo-home",
        "NPM_CONFIG_CACHE": environment_root / "npm-cache",
    }
    for path in private_paths.values():
        try:
            path.mkdir(exist_ok=True, mode=0o700)
            path.chmod(0o700)
            path_metadata = path.lstat()
        except OSError as error:
            raise ProductInputResolutionError(
                f"product subprocess private directory is unavailable: {error}"
            ) from error
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISDIR(
            path_metadata.st_mode
        ):
            raise ProductInputResolutionError(
                "product subprocess private path must be a real directory"
            )

    allowed = (
        "CI",
        "GIT_SSL_CAINFO",
        "KANDELO_DEV_SHELL_TOOL_PATH",
        "KANDELO_NIX_BIN",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NIX_SSL_CERT_FILE",
        "NO_COLOR",
        "PATH",
        "SOURCE_DATE_EPOCH",
        "SSL_CERT_FILE",
        "TERM",
        "TZ",
        "USER",
    )
    environment = {
        name: source[name]
        for name in allowed
        if isinstance(source.get(name), str) and source[name]
    }
    environment.update({name: str(path) for name, path in private_paths.items()})
    environment.update(
        {
            "CI": "true",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _runtime_validation_arguments(
    *,
    kandelo_policy_root: Path,
    bundle: Path,
    artifact_root: Path,
    source_root: Path,
    source: Mapping[str, Any],
    target_abi: Mapping[str, Any],
    build_policy_sha256: str,
) -> list[Path | str]:
    """Build the protected exact-runtime validator argv without shell interpolation."""

    command = r'''set -euo pipefail
host_target="$(rustc -vV | awk '/^host/ {print $2}')"
exec cargo run -p xtask --target "$host_target" --quiet -- \
  abi-staging runtime-bundle validate "$@"
'''
    return [
        kandelo_policy_root / "scripts/dev-shell.sh",
        "bash",
        "-c",
        command,
        "abi-staging-runtime",
        "--bundle",
        bundle,
        "--artifact-root",
        artifact_root,
        "--source-root",
        source_root,
        "--repository",
        str(source["repository"]),
        "--commit",
        str(source["commit"]),
        "--tree",
        str(source["tree"]),
        "--abi",
        str(target_abi["version"]),
        "--snapshot-sha256",
        str(target_abi["snapshot_sha256"]),
        "--build-policy-sha256",
        build_policy_sha256,
    ]


def _candidate_package_resolve_arguments(
    *,
    kandelo_root: Path,
    cache_root: Path,
    cargo_target: Path,
    sysroot: Path,
    architecture: str,
    package: Mapping[str, Any],
) -> list[Path | str]:
    """Build one exact-head package through its normal resolver path."""

    name = str(package["name"])
    outputs = package["outputs"]
    source_roles = package["source_roles"]
    if (
        architecture not in {"wasm32", "wasm64"}
        or not isinstance(outputs, list)
        or not isinstance(source_roles, list)
        or any(not isinstance(value, str) for value in [name, *outputs, *source_roles])
    ):
        raise ProductInputResolutionError(
            "candidate package projection is malformed"
        )
    command = r'''set -euo pipefail
cache_root="$1"
cargo_target="$2"
sysroot="$3"
private_root="$4"
architecture="$5"
package="$6"
outputs="$7"
source_roles="$8"
export CARGO_TARGET_DIR="$cargo_target"
export WASM_POSIX_BINARY_CACHE_ROOT="$cache_root"
export WASM_POSIX_SYSROOT="$sysroot"
export HOME="$private_root/home"
export TMPDIR="$private_root/tmp"
export KANDELO_VFS_PRODUCT_PACKAGE="$package"
export KANDELO_VFS_PRODUCT_OUTPUTS="$outputs"
export KANDELO_VFS_PRODUCT_SOURCE_ROLES="$source_roles"
host_target="$(rustc -vV | awk '/^host/ {print $2}')"
exec cargo run -p xtask --target "$host_target" --quiet -- \
  build-deps "--arch=$architecture" --force-source-build resolve "$package"
'''
    return [
        kandelo_root / "scripts/dev-shell.sh",
        "bash",
        "-c",
        command,
        "abi-staging-package",
        cache_root,
        cargo_target,
        sysroot,
        cache_root.parent,
        architecture,
        name,
        ",".join(outputs),
        ",".join(source_roles),
    ]


def _run_bounded_product_command(
    command: list[Path | str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    stdout_limit: int,
    stderr_limit: int,
) -> subprocess.CompletedProcess[bytes]:
    """Run one uncredentialed build command with killable bounded capture."""

    if (
        not command
        or timeout_seconds < 1
        or stdout_limit < 1
        or stderr_limit < 1
    ):
        raise ProductInputResolutionError(
            "product subprocess bounds are invalid"
        )
    arguments = [str(value) for value in command]
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise ProductInputResolutionError(
            f"cannot launch product subprocess: {error}"
        ) from error
    assert process.stdout is not None and process.stderr is not None
    capture = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout_seconds

    def terminate() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate()
                raise ProductInputResolutionError("product subprocess timed out")
            for key, _mask in selector.select(min(remaining, 0.25)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                stream = key.data
                capture[stream].extend(chunk)
                if len(capture[stream]) > limits[stream]:
                    terminate()
                    raise ProductInputResolutionError(
                        f"product subprocess {stream} output exceeded its capture bound"
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate()
            raise ProductInputResolutionError("product subprocess timed out")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            terminate()
            raise ProductInputResolutionError("product subprocess timed out") from error
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
    return subprocess.CompletedProcess(
        arguments,
        return_code,
        bytes(capture["stdout"]),
        bytes(capture["stderr"]),
    )


def _product_input_collector_arguments(
    *,
    kandelo_root: Path,
    kandelo_policy_root: Path,
    catalog: Path,
    product_id: str,
    source: Mapping[str, Any],
    target_abi: Mapping[str, Any],
    policy_sha256: str,
    dev_shell_lock_sha256: str,
    package_roots: Path,
    archive_files: Path,
    runtime_root: Path,
    out: Path,
) -> list[Path | str]:
    """Build the closed protected collector argv inside the exact-head shell."""

    return [
        kandelo_root / "scripts/dev-shell.sh",
        "npx",
        "--no-install",
        "tsx",
        kandelo_policy_root / "scripts/abi-staging-collect-product-inputs.ts",
        "--archive-files",
        archive_files,
        "--catalog",
        catalog,
        "--dev-shell-lock-sha256",
        dev_shell_lock_sha256,
        "--out",
        out,
        "--package-roots",
        package_roots,
        "--policy-sha256",
        policy_sha256,
        "--product-id",
        product_id,
        "--program-index",
        kandelo_root / "packages/registry/program-packages.json",
        "--runtime-root",
        runtime_root,
        "--snapshot-sha256",
        target_abi["snapshot_sha256"],
        "--source-commit",
        source["commit"],
        "--source-repository",
        source["repository"],
        "--source-root",
        kandelo_root,
        "--source-tree",
        source["tree"],
        "--target-abi",
        str(target_abi["version"]),
    ]


def _vfs_product_builder_arguments(
    *,
    kandelo_root: Path,
    kandelo_policy_root: Path,
    manifest: Path,
    resolved_inputs: Path,
    work_dir: Path,
    output: Path,
    report: Path,
) -> list[Path | str]:
    """Build the closed protected VFS runner argv for one candidate builder."""

    return [
        kandelo_root / "scripts/dev-shell.sh",
        "npx",
        "--no-install",
        "tsx",
        kandelo_policy_root / "scripts/run-vfs-product-builder.ts",
        "--inputs",
        resolved_inputs,
        "--manifest",
        manifest,
        "--output",
        output,
        "--report",
        report,
        "--work-dir",
        work_dir,
    ]


class _ProductWorkTerminal(Exception):
    def __init__(
        self,
        *,
        outcome: str,
        guard_code: str,
        exit_code: int,
        summary: bytes,
    ) -> None:
        super().__init__(guard_code)
        self.outcome = outcome
        self.guard_code = guard_code
        self.exit_code = exit_code
        self.summary = summary


def _product_diagnostic_summary(label: str, detail: bytes | str = b"") -> bytes:
    """Produce one bounded, non-secret terminal diagnostic summary."""

    if isinstance(detail, bytes):
        text = detail.decode("utf-8", errors="replace")
    else:
        text = str(detail)
    normalized = text.replace("\0", "").strip()
    combined = label if not normalized else f"{label}: {normalized}"
    body = (combined + "\n").encode("utf-8")
    if len(body) > 64 * 1024:
        body = body[: 64 * 1024 - len("\n".encode())].decode(
            "utf-8", errors="ignore"
        ).encode("utf-8") + b"\n"
    return body


def _archive_download_arguments(
    *, kandelo_policy_root: Path, archive: Mapping[str, Any], output: Path
) -> list[Path | str]:
    """Build a credential-free bounded HTTPS download command."""

    url = str(archive["url"])
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProductInputResolutionError(
            "product source archive URL is not one credential-free HTTPS URL"
        )
    return [
        kandelo_policy_root / "scripts/dev-shell.sh",
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--max-time",
        "900",
        "--max-filesize",
        str(MAX_INPUT_OBJECT_BYTES),
        "--output",
        output,
        "--",
        url,
    ]


def _resolve_candidate_package_roots(
    *,
    build_spec: Mapping[str, Any],
    kandelo_root: Path,
    runtime_root: Path,
    work_root: Path,
    environment: Mapping[str, str],
) -> dict[str, str]:
    cache_root = (work_root / "package-cache").resolve(strict=False)
    cargo_target = (work_root / "cargo-target").resolve(strict=False)
    cache_root.mkdir(mode=0o700)
    cargo_target.mkdir(mode=0o700)
    package_roots: dict[str, str] = {}
    for package in build_spec["packages"]:
        command = _candidate_package_resolve_arguments(
            kandelo_root=kandelo_root,
            cache_root=cache_root,
            cargo_target=cargo_target,
            sysroot=runtime_root
            / "toolchain"
            / f"{build_spec['architecture']}-sysroot",
            architecture=build_spec["architecture"],
            package=package,
        )
        try:
            completed = _run_bounded_product_command(
                command,
                cwd=kandelo_root,
                env=environment,
                timeout_seconds=3 * 60 * 60,
                stdout_limit=64 * 1024,
                stderr_limit=1024 * 1024,
            )
        except ProductInputResolutionError as error:
            raise _ProductWorkTerminal(
                outcome="blocked",
                guard_code="product_inputs_unavailable",
                exit_code=78,
                summary=_product_diagnostic_summary(
                    f"package {package['name']} could not be resolved", error
                ),
            ) from error
        if completed.returncode != 0:
            raise _ProductWorkTerminal(
                outcome="blocked",
                guard_code="product_inputs_unavailable",
                exit_code=78,
                summary=_product_diagnostic_summary(
                    f"package {package['name']} build failed", completed.stderr
                ),
            )
        try:
            lines = completed.stdout.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError as error:
            raise ProductInputResolutionError(
                f"package {package['name']} resolver output is not UTF-8"
            ) from error
        if len(lines) != 1 or not lines[0]:
            raise ProductInputResolutionError(
                f"package {package['name']} resolver did not emit one path"
            )
        candidate_path = Path(lines[0])
        try:
            metadata = candidate_path.lstat()
            resolved = candidate_path.resolve(strict=True)
            resolved.relative_to(cache_root.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise ProductInputResolutionError(
                f"package {package['name']} resolver path escapes its private cache"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ProductInputResolutionError(
                f"package {package['name']} resolver output is not a real directory"
            )
        package_roots[package["name"]] = str(resolved)
    return package_roots


def _download_product_archives(
    *,
    build_spec: Mapping[str, Any],
    kandelo_policy_root: Path,
    work_root: Path,
    environment: Mapping[str, str],
) -> dict[str, str]:
    archive_root = (work_root / "archives").resolve(strict=False)
    archive_root.mkdir(mode=0o700)
    archive_files: dict[str, str] = {}
    for archive in build_spec["archives"]:
        output = archive_root / archive["id"]
        command = _archive_download_arguments(
            kandelo_policy_root=kandelo_policy_root,
            archive=archive,
            output=output,
        )
        try:
            completed = _run_bounded_product_command(
                command,
                cwd=kandelo_policy_root,
                env=environment,
                timeout_seconds=20 * 60,
                stdout_limit=64 * 1024,
                stderr_limit=1024 * 1024,
            )
        except ProductInputResolutionError as error:
            raise _ProductWorkTerminal(
                outcome="blocked",
                guard_code="product_inputs_unavailable",
                exit_code=78,
                summary=_product_diagnostic_summary(
                    f"archive {archive['id']} is unavailable", error
                ),
            ) from error
        if completed.returncode != 0:
            raise _ProductWorkTerminal(
                outcome="blocked",
                guard_code="product_inputs_unavailable",
                exit_code=78,
                summary=_product_diagnostic_summary(
                    f"archive {archive['id']} download failed", completed.stderr
                ),
            )
        try:
            metadata = output.lstat()
            body = output.read_bytes()
        except OSError as error:
            raise ProductInputResolutionError(
                f"archive {archive['id']} output is unavailable: {error}"
            ) from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not body
            or len(body) > MAX_INPUT_OBJECT_BYTES
            or hashlib.sha256(body).hexdigest() != archive["sha256"]
        ):
            raise ProductInputResolutionError(
                f"archive {archive['id']} differs from its pinned identity"
            )
        archive_files[archive["id"]] = str(output.resolve(strict=True))
    return archive_files


def _dependency_product_artifacts(
    *,
    dependency_product_ids: list[str],
    request: Mapping[str, Any],
    request_sha256: str,
    runtime_bundle_sha256: str,
    policy: Any,
    transport: UrllibOciTransportV1,
) -> tuple[Any, ...]:
    artifacts = []
    for product_id in dependency_product_ids:
        repository = candidate_product_repository(
            owner=policy.candidate_owner,
            repository_prefix=policy.candidate_repository_prefix,
            candidate_suffix=policy.candidate_suffix,
            target_abi=request["target_abi"]["version"],
            product_id=product_id,
        )
        entries = inspect_candidate_product_repository(
            repository,
            request=request,
            request_sha256=request_sha256,
            expected_source_repository=policy.tap_repository,
            transport=transport,
        )
        current = [
            entry
            for entry in entries
            if entry.runtime_bundle_sha256 == runtime_bundle_sha256
        ]
        if not current:
            raise _ProductWorkTerminal(
                outcome="blocked",
                guard_code="product_dependency_unavailable",
                exit_code=78,
                summary=_product_diagnostic_summary(
                    f"dependency product {product_id} has no current candidate"
                ),
            )
        if len(current) != 1:
            raise ProductInputResolutionError(
                f"dependency product {product_id} has ambiguous current candidates"
            )
        artifacts.append(current[0].artifact)
    return tuple(artifacts)


def _write_private_product_authority(
    root: Path,
    *,
    request_sha256: str,
    work_id: str,
    product: Mapping[str, Any],
    runtime_artifact_id: int,
    runtime_artifact_digest: str,
    runtime_bundle_sha256: str,
    outcome: str,
    guard_code: str | None,
    input_inventory_sha256: str | None,
    resolved_inputs_sha256: str | None,
) -> None:
    destination = Path(root)
    try:
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = destination.lstat()
    except OSError as error:
        raise ProductInputResolutionError(
            f"private product authority root is unavailable: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ProductInputResolutionError(
            "private product authority root is not a real directory"
        )
    body = canonical_bytes(
        {
            "schema": 1,
            "kind": "kandelo-abi-staging-private-product-authority",
            "request_sha256": request_sha256,
            "work_id": work_id,
            "product": dict(product),
            "runtime_artifact": {
                "id": runtime_artifact_id,
                "digest": runtime_artifact_digest,
            },
            "runtime_bundle_sha256": runtime_bundle_sha256,
            "outcome": outcome,
            "guard_code": guard_code,
            "input_inventory_sha256": input_inventory_sha256,
            "resolved_inputs_sha256": resolved_inputs_sha256,
        }
    )
    path = destination / "authority.json"
    try:
        with path.open("xb") as output:
            output.write(body)
        path.chmod(0o600)
    except OSError as error:
        raise ProductInputResolutionError(
            f"cannot write private product authority: {error}"
        ) from error


def _execute_product_work(args: argparse.Namespace) -> int:
    """Compose one exact candidate product from protected and public authority."""

    tap_root = _protected_tap_root(args.tap_root)
    policy = load_tap_staging_policy(tap_root / "Kandelo/staging/tap-policy.toml")
    input_root = Path(args.coordination_root).resolve(strict=True)
    bundle = load_coordination_bundle(
        input_root / "coordination/coordination.json", policy=policy
    )
    request = bundle["request"]
    request_sha256 = bundle["request_sha256"]
    scope = select_product_execution_scope(
        request,
        request_sha256=request_sha256,
        product_id=args.product_id,
        work_id=args.work_id,
    )
    source = request["build_source"]
    kandelo_root = _checked_checkout_source(
        args.kandelo_root,
        repository=source["repository"],
        commit=source["commit"],
        tree=source["tree"],
    )
    policy_commit = request["issuance"]["issuer_workflow_ref"].rsplit("@", 1)[-1]
    kandelo_policy_root = _checked_checkout_source(
        args.kandelo_policy_root,
        repository=source["repository"],
        commit=policy_commit,
        tree=None,
    )
    if (
        not str(args.runtime_artifact_id).isdigit()
        or int(args.runtime_artifact_id) < 1
        or len(args.runtime_artifact_digest) != 64
        or any(character not in "0123456789abcdef" for character in args.runtime_artifact_digest)
    ):
        raise ProductInputResolutionError(
            "runtime workflow artifact identity is malformed"
        )
    runtime_artifact_id = int(args.runtime_artifact_id)
    runtime_bundle_path = input_root / "runtime/runtime-bundle.json"
    runtime_root = input_root / "runtime/runtime"
    try:
        runtime_body = runtime_bundle_path.read_bytes()
        runtime = load_canonical_mapping(runtime_body, "exact product runtime bundle")
    except (OSError, ContractError) as error:
        raise ProductInputResolutionError(
            f"exact product runtime bundle is invalid: {error}"
        ) from error
    runtime_bundle_sha256 = hashlib.sha256(runtime_body).hexdigest()
    catalog_path = kandelo_root / "images/vfs/products/generated/catalog.json"
    try:
        catalog = load_canonical_mapping(
            catalog_path.read_bytes(), "exact candidate product catalog"
        )
    except (OSError, ContractError) as error:
        raise ProductInputResolutionError(
            f"exact candidate product catalog is invalid: {error}"
        ) from error
    build_spec = select_product_input_build_spec(
        request, catalog, args.product_id
    )
    product = {
        "id": build_spec["id"],
        "manifest_sha256": build_spec["manifest_sha256"],
        "output": build_spec["output"],
    }
    output = Path(args.out)
    private_output = Path(args.private_out)
    if (
        output.exists()
        or output.is_symlink()
        or private_output.exists()
        or private_output.is_symlink()
    ):
        raise ProductInputResolutionError(
            "product public and private outputs must both be new"
        )
    output.parent.resolve(strict=True)
    private_parent = private_output.parent.resolve(strict=True)
    identity = product_runtime_identity(runtime, request)

    def terminal(
        *, outcome: str, guard_code: str, exit_code: int, summary: bytes
    ) -> int:
        write_product_build_handoff(
            output,
            request_sha256=request_sha256,
            work_id=scope["work_id"],
            product=product,
            runtime_bundle_body=runtime_body,
            outcome=outcome,
            guard_code=guard_code,
            exit_code=exit_code,
            diagnostic_summary=summary,
        )
        _write_private_product_authority(
            private_output,
            request_sha256=request_sha256,
            work_id=scope["work_id"],
            product=product,
            runtime_artifact_id=runtime_artifact_id,
            runtime_artifact_digest=args.runtime_artifact_digest,
            runtime_bundle_sha256=runtime_bundle_sha256,
            outcome=outcome,
            guard_code=guard_code,
            input_inventory_sha256=None,
            resolved_inputs_sha256=None,
        )
        return 0 if outcome == "blocked" else exit_code

    try:
        with tempfile.TemporaryDirectory(
            prefix="abi-staging-product-work-", dir=private_parent
        ) as temporary:
            work_root = Path(temporary).resolve(strict=True)
            environment = _product_subprocess_environment(
                work_root, ambient=os.environ
            )
            runtime_validation = _run_bounded_product_command(
                _runtime_validation_arguments(
                    kandelo_policy_root=kandelo_policy_root,
                    bundle=runtime_bundle_path,
                    artifact_root=runtime_root,
                    source_root=kandelo_root,
                    source=source,
                    target_abi=request["target_abi"],
                    build_policy_sha256=identity["policy_sha256"],
                ),
                cwd=kandelo_policy_root,
                env=environment,
                timeout_seconds=30 * 60,
                stdout_limit=64 * 1024,
                stderr_limit=1024 * 1024,
            )
            if runtime_validation.returncode != 0:
                raise ProductInputResolutionError(
                    "exact runtime validation failed: "
                    + runtime_validation.stderr.decode("utf-8", errors="replace")
                )

            package_roots = _resolve_candidate_package_roots(
                build_spec=build_spec,
                kandelo_root=kandelo_root,
                runtime_root=runtime_root,
                work_root=work_root,
                environment=environment,
            )
            archive_files = _download_product_archives(
                build_spec=build_spec,
                kandelo_policy_root=kandelo_policy_root,
                work_root=work_root,
                environment=environment,
            )
            package_map = work_root / "package-roots.json"
            archive_map = work_root / "archive-files.json"
            package_map.write_bytes(canonical_bytes(package_roots))
            archive_map.write_bytes(canonical_bytes(archive_files))
            collector = _run_bounded_product_command(
                _product_input_collector_arguments(
                    kandelo_root=kandelo_root,
                    kandelo_policy_root=kandelo_policy_root,
                    catalog=catalog_path,
                    product_id=args.product_id,
                    source=source,
                    target_abi=request["target_abi"],
                    policy_sha256=identity["policy_sha256"],
                    dev_shell_lock_sha256=identity["dev_shell_lock_sha256"],
                    package_roots=package_map,
                    archive_files=archive_map,
                    runtime_root=runtime_root,
                    out=private_output,
                ),
                cwd=kandelo_root,
                env=environment,
                timeout_seconds=60 * 60,
                stdout_limit=4 * 1024 * 1024,
                stderr_limit=1024 * 1024,
            )
            if collector.returncode != 0:
                raise _ProductWorkTerminal(
                    outcome="blocked",
                    guard_code="product_inputs_unavailable",
                    exit_code=78,
                    summary=_product_diagnostic_summary(
                        "protected product input collection failed", collector.stderr
                    ),
                )
            inventory_path = private_output / "inputs/artifacts.json"
            inventory_body = inventory_path.read_bytes()
            inventory = load_product_input_object_inventory(inventory_body)
            checked_inventory = validate_product_input_object_authority(
                inventory,
                request=request,
                request_sha256=request_sha256,
                catalog=catalog,
                runtime_bundle=runtime,
                object_root=private_output,
                source_root=kandelo_root,
                runtime_root=runtime_root,
            )

            transport = UrllibOciTransportV1(username="", token="")
            verification_tests = load_verification_tests(
                tap_root / "Kandelo/staging/verification-tests.toml"
            )
            public_inventory = scan_scheduling_inventory(
                bundle["tap_plan"],
                policy=policy,
                verification_tests=verification_tests,
                transport=transport,
            )
            product_artifacts = _dependency_product_artifacts(
                dependency_product_ids=build_spec["dependency_product_ids"],
                request=request,
                request_sha256=request_sha256,
                runtime_bundle_sha256=runtime_bundle_sha256,
                policy=policy,
                transport=transport,
            )
            resolution = resolve_product_from_checked_input_authority(
                checked_inventory,
                request=request,
                request_sha256=request_sha256,
                catalog=catalog,
                tap_plan=bundle["tap_plan"],
                records=public_inventory.records,
                candidate_records=public_inventory.candidate_records,
                candidate_locators=public_inventory.candidate_locators,
                source_custody_records=public_inventory.source_custody_records,
                reuse_records=public_inventory.reuse_records,
                verification_records=public_inventory.verification_records,
                verification_locators=public_inventory.verification_locators,
                verification_tests=verification_tests,
                runtime_bundle=runtime,
                product_artifacts=product_artifacts,
            )
            resolved_body = canonical_bytes(resolution.resolved_inputs)
            closed_input_root = work_root / "closed-inputs"
            closed_object_root = closed_input_root / "inputs/objects"
            closed_object_root.mkdir(parents=True, mode=0o700)
            for item in checked_inventory["objects"]:
                source_path = private_output.joinpath(*item["path"].split("/"))
                destination_path = closed_input_root.joinpath(
                    *item["path"].split("/")
                )
                destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    with source_path.open("rb") as source_file, destination_path.open(
                        "xb"
                    ) as destination_file:
                        shutil.copyfileobj(
                            source_file, destination_file, length=1024 * 1024
                        )
                    destination_path.chmod(0o600)
                except OSError as error:
                    raise ProductInputResolutionError(
                        f"cannot close private product input {item['id']}: {error}"
                    ) from error
            resolved_path = materialize_resolved_product_input_objects(
                resolved_body,
                root=closed_input_root,
                transport=transport,
            )

            builder_root = work_root / "builder"
            builder_root.mkdir(mode=0o700)
            vfs_path = builder_root / build_spec["output"]
            report_path = builder_root / "builder-report.json"
            try:
                builder = _run_bounded_product_command(
                    _vfs_product_builder_arguments(
                        kandelo_root=kandelo_root,
                        kandelo_policy_root=kandelo_policy_root,
                        manifest=kandelo_root / build_spec["manifest_path"],
                        resolved_inputs=resolved_path,
                        work_dir=builder_root,
                        output=vfs_path,
                        report=report_path,
                    ),
                    cwd=kandelo_root,
                    env=environment,
                    timeout_seconds=3 * 60 * 60,
                    stdout_limit=1024 * 1024,
                    stderr_limit=4 * 1024 * 1024,
                )
            except ProductInputResolutionError as error:
                guard = (
                    "product_builder_timeout"
                    if "timed out" in str(error)
                    else "product_builder_failed"
                )
                raise _ProductWorkTerminal(
                    outcome="failure",
                    guard_code=guard,
                    exit_code=1,
                    summary=_product_diagnostic_summary(
                        "candidate VFS product builder did not complete", error
                    ),
                ) from error
            if builder.returncode != 0:
                raise _ProductWorkTerminal(
                    outcome="failure",
                    guard_code="product_builder_failed",
                    exit_code=1,
                    summary=_product_diagnostic_summary(
                        "candidate VFS product builder failed", builder.stderr
                    ),
                )
            report_body = report_path.read_bytes()
            vfs_body = vfs_path.read_bytes()
            if not args.validate_builder_report:
                raise ProductInputResolutionError(
                    "protected builder-report validation is required"
                )
            validate_candidate_builder_report(report_body)
            result = write_product_build_handoff(
                output,
                request_sha256=request_sha256,
                work_id=scope["work_id"],
                product=product,
                runtime_bundle_body=runtime_body,
                outcome="success",
                guard_code=None,
                exit_code=0,
                diagnostic_summary=b"exact product composition completed\n",
                resolved_inputs_body=resolved_body,
                builder_report_body=report_body,
                vfs_body=vfs_body,
            )
            _write_private_product_authority(
                private_output,
                request_sha256=request_sha256,
                work_id=scope["work_id"],
                product=product,
                runtime_artifact_id=runtime_artifact_id,
                runtime_artifact_digest=args.runtime_artifact_digest,
                runtime_bundle_sha256=runtime_bundle_sha256,
                outcome=result["outcome"],
                guard_code=None,
                input_inventory_sha256=hashlib.sha256(inventory_body).hexdigest(),
                resolved_inputs_sha256=hashlib.sha256(resolved_body).hexdigest(),
            )
            return 0
    except _ProductWorkTerminal as terminal_state:
        return terminal(
            outcome=terminal_state.outcome,
            guard_code=terminal_state.guard_code,
            exit_code=terminal_state.exit_code,
            summary=terminal_state.summary,
        )
    except (InventoryError, ProductEvidenceError) as error:
        return terminal(
            outcome="failure",
            guard_code="product_integrity_mismatch",
            exit_code=1,
            summary=_product_diagnostic_summary(
                "protected product authority validation failed", error
            ),
        )
    except ProductInputResolutionError as error:
        return terminal(
            outcome="failure",
            guard_code="product_integrity_mismatch",
            exit_code=1,
            summary=_product_diagnostic_summary(
                "exact product composition was rejected", error
            ),
        )


def _private_lazy_input_bodies(
    *,
    resolved_inputs: Mapping[str, Any],
    checked_inventory: Mapping[str, Any],
    private_root: Path,
) -> dict[str, bytes]:
    """Recover only exact nonpublic lazy bytes admitted by the manifest."""

    objects = {item["id"]: item for item in checked_inventory["objects"]}
    expected = {
        item["id"]: item
        for item in resolved_inputs["inputs"]
        if item["effective_materialization"] == "lazy-reference"
        and item["kind"] in NONPUBLIC_PRODUCT_INPUT_KINDS
    }
    if set(expected) != set(objects).intersection(expected):
        raise ProductInputResolutionError(
            "private lazy product object closure is incomplete"
        )
    bodies: dict[str, bytes] = {}
    for input_id in sorted(expected):
        item = objects[input_id]
        resolved = expected[input_id]
        if (
            item["sha256"] != resolved["sha256"]
            or item["bytes"] != resolved["bytes"]
            or item["kind"] != resolved["kind"]
        ):
            raise ProductInputResolutionError(
                f"private lazy product object {input_id} differs from resolution"
            )
        path = private_root.joinpath(*item["path"].split("/"))
        try:
            metadata = path.lstat()
            body = path.read_bytes()
        except OSError as error:
            raise ProductInputResolutionError(
                f"private lazy product object {input_id} is unavailable: {error}"
            ) from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or len(body) != resolved["bytes"]
            or hashlib.sha256(body).hexdigest() != resolved["sha256"]
        ):
            raise ProductInputResolutionError(
                f"private lazy product object {input_id} changed"
            )
        bodies[input_id] = body
    return bodies


def _publish_workflow_product_candidate(args: argparse.Namespace) -> None:
    """Publish one candidate only after a fresh protected reconstruction."""

    _require_workflow_publication_guards(args)
    if not args.validate_builder_report:
        raise WorkflowPublicationError(
            "product candidate publication requires builder-report validation"
        )
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
            "protected product publisher checkout differs from workflow head"
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
    expected_handoff_name = (
        f"abi-staging-product-build-{args.product_id}-{args.work_id}-"
        f"{args.run_id}-{args.run_attempt}"
    )
    expected_private_name = (
        f"abi-staging-product-private-{args.product_id}-{args.work_id}-"
        f"{args.run_id}-{args.run_attempt}"
    )
    if (
        args.handoff_artifact_name != expected_handoff_name
        or args.private_artifact_name != expected_private_name
    ):
        raise WorkflowPublicationError(
            "product publisher artifact names differ from protected scope"
        )

    with tempfile.TemporaryDirectory(
        prefix="abi-staging-product-publication-"
    ) as temporary:
        root = Path(temporary)
        coordination_artifact = client.artifact_by_name(
            name=f"abi-staging-coordination-{args.run_id}-{args.run_attempt}"
        )
        coordination_root = root / "coordination"
        client.extract_artifact(
            coordination_artifact,
            coordination_root,
            max_files=policy.max_handoff_files,
            max_bytes=policy.max_handoff_bytes,
        )
        bundle = load_coordination_bundle(
            coordination_root / "coordination.json", policy=policy
        )
        request = bundle["request"]
        request_sha256 = bundle["request_sha256"]
        scope = select_product_execution_scope(
            request,
            request_sha256=request_sha256,
            product_id=args.product_id,
            work_id=args.work_id,
        )
        source = request["build_source"]
        kandelo_root = _checked_checkout_source(
            args.kandelo_root,
            repository=source["repository"],
            commit=source["commit"],
            tree=source["tree"],
        )
        policy_commit = request["issuance"]["issuer_workflow_ref"].rsplit(
            "@", 1
        )[-1]
        _checked_checkout_source(
            args.kandelo_policy_root,
            repository=source["repository"],
            commit=policy_commit,
            tree=None,
        )

        runtime_artifact = client.artifact_by_name(
            name=(
                f"abi-staging-runtime-{request_sha256}-"
                f"{args.run_id}-{args.run_attempt}"
            )
        )
        runtime_artifact_root = root / "runtime"
        client.extract_artifact(
            runtime_artifact,
            runtime_artifact_root,
            max_files=65_536,
            max_bytes=8 * 1024**3,
        )
        runtime_bundle_path = runtime_artifact_root / "runtime-bundle.json"
        runtime_root = runtime_artifact_root / "runtime"
        try:
            runtime_body = runtime_bundle_path.read_bytes()
            runtime = load_canonical_mapping(
                runtime_body, "protected product publication runtime"
            )
        except (OSError, ContractError) as error:
            raise WorkflowPublicationError(
                f"product publication runtime is invalid: {error}"
            ) from error
        runtime_sha256 = hashlib.sha256(runtime_body).hexdigest()
        product_runtime_identity(runtime, request)

        handoff_artifact = client.artifact_by_name(name=expected_handoff_name)
        handoff_root = root / "handoff"
        client.extract_artifact(
            handoff_artifact,
            handoff_root,
            max_files=policy.max_handoff_files,
            max_bytes=policy.max_handoff_bytes,
        )
        product_result = validate_product_build_handoff(
            handoff_root,
            expected_product_id=args.product_id,
            expected_work_id=scope["work_id"],
            expected_request_sha256=request_sha256,
            expected_runtime_bundle_sha256=runtime_sha256,
            max_files=policy.max_handoff_files,
            max_bytes=policy.max_handoff_bytes,
        )
        if product_result["outcome"] != "success":
            raise WorkflowPublicationError(
                "protected publisher cannot publish an unsuccessful product handoff"
            )
        product = product_result["product"]

        private_artifact = client.artifact_by_name(name=expected_private_name)
        private_root = root / "private"
        client.extract_artifact(
            private_artifact,
            private_root,
            max_files=policy.max_handoff_files,
            max_bytes=policy.max_handoff_bytes,
        )
        private = validate_private_product_authority_handoff(
            private_root,
            expected_request_sha256=request_sha256,
            expected_work_id=scope["work_id"],
            expected_product=product,
            expected_runtime_artifact_id=runtime_artifact.id,
            expected_runtime_artifact_digest=runtime_artifact.sha256,
            expected_runtime_bundle_sha256=runtime_sha256,
            max_files=policy.max_handoff_files,
            max_bytes=policy.max_handoff_bytes,
        )

        catalog_path = kandelo_root / "images/vfs/products/generated/catalog.json"
        try:
            catalog = load_canonical_mapping(
                catalog_path.read_bytes(), "protected candidate product catalog"
            )
        except (OSError, ContractError) as error:
            raise WorkflowPublicationError(
                f"product publication catalog is invalid: {error}"
            ) from error
        build_spec = select_product_input_build_spec(
            request, catalog, args.product_id
        )
        if (
            build_spec["manifest_sha256"] != product["manifest_sha256"]
            or build_spec["output"] != product["output"]
        ):
            raise WorkflowPublicationError(
                "product publication scope differs from exact catalog"
            )
        checked_inventory = validate_product_input_object_authority(
            private["inventory"],
            request=request,
            request_sha256=request_sha256,
            catalog=catalog,
            runtime_bundle=runtime,
            object_root=private_root,
            source_root=kandelo_root,
            runtime_root=runtime_root,
        )
        transport = UrllibOciTransportV1(username="", token="")
        verification_tests = load_verification_tests(
            tap_root / "Kandelo/staging/verification-tests.toml"
        )
        public_inventory = scan_scheduling_inventory(
            bundle["tap_plan"],
            policy=policy,
            verification_tests=verification_tests,
            transport=transport,
        )
        product_artifacts = _dependency_product_artifacts(
            dependency_product_ids=build_spec["dependency_product_ids"],
            request=request,
            request_sha256=request_sha256,
            runtime_bundle_sha256=runtime_sha256,
            policy=policy,
            transport=transport,
        )
        resolution = resolve_product_from_checked_input_authority(
            checked_inventory,
            request=request,
            request_sha256=request_sha256,
            catalog=catalog,
            tap_plan=bundle["tap_plan"],
            records=public_inventory.records,
            candidate_records=public_inventory.candidate_records,
            candidate_locators=public_inventory.candidate_locators,
            source_custody_records=public_inventory.source_custody_records,
            reuse_records=public_inventory.reuse_records,
            verification_records=public_inventory.verification_records,
            verification_locators=public_inventory.verification_locators,
            verification_tests=verification_tests,
            runtime_bundle=runtime,
            product_artifacts=product_artifacts,
        )
        resolved_body = canonical_bytes(resolution.resolved_inputs)
        if (
            hashlib.sha256(resolved_body).hexdigest()
            != private["resolved_inputs_sha256"]
            or (handoff_root / "resolved-inputs.json").read_bytes()
            != resolved_body
        ):
            raise WorkflowPublicationError(
                "product handoff resolved inputs differ from protected reconstruction"
            )
        lazy_input_bodies = _private_lazy_input_bodies(
            resolved_inputs=resolution.resolved_inputs,
            checked_inventory=checked_inventory,
            private_root=private_root,
        )
        candidate_repository_name = candidate_product_repository(
            owner=policy.candidate_owner,
            repository_prefix=policy.candidate_repository_prefix,
            candidate_suffix=policy.candidate_suffix,
            target_abi=request["target_abi"]["version"],
            product_id=args.product_id,
        )
        candidate_plan = build_candidate_product_oci_plan(
            repository=candidate_repository_name,
            publisher_repository=policy.tap_repository,
            input_plan=resolution.plan,
            vfs_body=(handoff_root / product["output"]).read_bytes(),
            builder_report_body=(handoff_root / "builder-report.json").read_bytes(),
            resolved_inputs_body=resolved_body,
            runtime_bundle_body=runtime_body,
            runtime_root=runtime_root,
            lazy_input_bodies=lazy_input_bodies,
        )
        username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
        package_token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
        _recheck_workflow_activation(tap_root)
        with isolated_oras_transport(
            username=username, token=package_token
        ) as publication_transport:
            locator = publish_candidate_product(
                candidate_plan,
                transport=publication_transport,
                expected_source_repository=policy.tap_repository,
            )
        output = Path(args.out)
        if output.exists() or output.is_symlink():
            raise WorkflowPublicationError(
                "product candidate locator output must be new"
            )
        output.write_bytes(canonical_bytes(asdict(locator)))


def _product_evidence_runner_arguments(
    *,
    host: str,
    builder_report: Path,
    candidate_locator: Path,
    context: Path,
    definitions: Path,
    output: Path,
    products: Path,
    resolved_inputs: Path,
    runtime_bundle: Path,
    runtime_root: Path,
    source_root: Path,
    vfs: Path,
) -> list[Path | str]:
    """Build the closed protected runner argv for exactly one host."""

    if host not in {"node", "browser"}:
        raise ProductEvidenceError("product evidence runner host is unsupported")
    try:
        policy_root = definitions.parents[2]
    except IndexError as error:
        raise ProductEvidenceError(
            "protected evidence definition path is outside its checkout"
        ) from error
    script = policy_root / "scripts" / (
        "abi-staging-product-node-evidence.ts"
        if host == "node"
        else "abi-staging-product-browser-evidence.ts"
    )
    arguments: list[Path | str] = [
        script,
        "--builder-report",
        builder_report,
        "--candidate-locator",
        candidate_locator,
        "--context",
        context,
        "--definitions",
        definitions,
        "--output",
        output,
        "--products",
        products,
        "--resolved-inputs",
        resolved_inputs,
        "--runtime-bundle",
        runtime_bundle,
        "--runtime-root",
        runtime_root,
        "--vfs",
        vfs,
    ]
    if host == "node":
        arguments.extend(("--source-root", source_root))
    else:
        arguments.extend(
            (
                "--pages",
                source_root
                / "apps/browser-demos/pages/kandelo/kernel-host/"
                "pages-vfs-products.generated.json",
                "--tests",
                source_root / "tests/vfs-products.generated.json",
            )
        )
    return arguments


def _checked_checkout_source(
    root_value: str, *, repository: str, commit: str, tree: str | None
) -> Path:
    root = Path(root_value).resolve(strict=True)
    observed = snapshot_tap_source(root, repository)
    if observed["commit"] != commit or (
        tree is not None and observed["tree"] != tree
    ):
        raise ProductEvidenceError(
            "evidence checkout differs from its protected exact source identity"
        )
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProductEvidenceError(
            f"cannot inspect exact evidence checkout: {error}"
        ) from error
    if status.stdout:
        raise ProductEvidenceError("exact evidence checkout contains changes")
    return root


def _execute_product_evidence_work(args: argparse.Namespace) -> int:
    """Validate inert handoffs, derive context, and run one protected host probe."""

    tap_root = _protected_tap_root(args.tap_root)
    policy = load_tap_staging_policy(
        tap_root / "Kandelo/staging/tap-policy.toml"
    )
    input_root = Path(args.input_root).resolve(strict=True)
    coordination_path = input_root / "coordination/coordination.json"
    bundle = load_coordination_bundle(coordination_path, policy=policy)
    request = bundle["request"]
    request_digest = bundle["request_sha256"]
    scope = select_product_evidence_execution_scope(
        request,
        request_sha256=request_digest,
        product_id=args.product_id,
        product_work_id=args.product_work_id,
        host=args.host,
        definition_id=args.definition_id,
        work_id=args.work_id,
    )
    source = request["build_source"]
    kandelo_root = _checked_checkout_source(
        args.kandelo_root,
        repository=source["repository"],
        commit=source["commit"],
        tree=source["tree"],
    )
    workflow_ref = request["issuance"]["issuer_workflow_ref"]
    policy_commit = workflow_ref.rsplit("@", 1)[-1]
    kandelo_policy_root = _checked_checkout_source(
        args.kandelo_policy_root,
        repository=source["repository"],
        commit=policy_commit,
        tree=None,
    )

    runtime_bundle = input_root / "runtime/runtime-bundle.json"
    runtime_root = input_root / "runtime/runtime"
    runtime_body = runtime_bundle.read_bytes()
    runtime_sha256 = hashlib.sha256(runtime_body).hexdigest()
    product_root = input_root / "product"
    product_result = validate_product_build_handoff(
        product_root,
        expected_product_id=args.product_id,
        expected_work_id=args.product_work_id,
        expected_request_sha256=request_digest,
        expected_runtime_bundle_sha256=runtime_sha256,
        max_files=16,
        max_bytes=3 * 1024 * 1024 * 1024,
    )
    if product_result["outcome"] != "success":
        raise ProductEvidenceError(
            "product evidence cannot run without one successful exact composition"
        )
    expected_repository = "ghcr.io/" + candidate_product_repository(
        owner=policy.tap_repository.split("/", 1)[0],
        repository_prefix=policy.tap_repository.split("/", 1)[1] + "-abi-",
        candidate_suffix="-candidates",
        target_abi=request["target_abi"]["version"],
        product_id=args.product_id,
    )
    candidate_locator_path = input_root / "candidate/product-candidate.json"
    candidate = load_candidate_product_locator(
        candidate_locator_path.read_bytes(), expected_repository=expected_repository
    )
    if (
        candidate.vfs_layer_sha256 != product_result["vfs"]["sha256"]
        or candidate.vfs_layer_bytes != product_result["vfs"]["bytes"]
        or candidate.builder_report_sha256
        != product_result["builder_report_sha256"]
    ):
        raise ProductEvidenceError(
            "candidate locator differs from the exact composition handoff"
        )

    products_path = kandelo_root / "images/vfs/products/generated/catalog.json"
    definitions_path = (
        kandelo_policy_root / "abi/staging/evidence-definitions.generated.json"
    )
    catalog = load_canonical_mapping(
        products_path.read_bytes(), "exact candidate product catalog"
    )
    definitions = load_canonical_mapping(
        definitions_path.read_bytes(), "protected evidence definitions"
    )
    run = {
        "repository": policy.tap_repository,
        "workflow_ref": args.workflow_ref,
        "run_id": args.run_id,
        "job_id": f"{args.host}-product-evidence",
        "attempt": args.run_attempt,
    }
    context = build_product_evidence_context(
        request=request,
        request_digest=request_digest,
        catalog=catalog,
        definitions=definitions,
        candidate_product=candidate,
        runtime_bundle_body=runtime_body,
        host=args.host,
        definition_id=args.definition_id,
        run=run,
    )
    output = Path(args.out)
    if output.exists() or output.is_symlink():
        raise ProductEvidenceError("product evidence result path must be new")
    output_parent = output.parent.resolve(strict=True)
    with tempfile.TemporaryDirectory(
        prefix="abi-staging-product-evidence-", dir=output_parent
    ) as temporary:
        context_path = Path(temporary) / "context.json"
        context_path.write_bytes(canonical_bytes(context))
        runner_arguments = _product_evidence_runner_arguments(
            host=args.host,
            builder_report=product_root / "builder-report.json",
            candidate_locator=candidate_locator_path,
            context=context_path,
            definitions=definitions_path,
            output=output,
            products=products_path,
            resolved_inputs=product_root / "resolved-inputs.json",
            runtime_bundle=runtime_bundle,
            runtime_root=runtime_root,
            source_root=kandelo_root,
            vfs=product_root / product_result["product"]["output"],
        )
        try:
            completed = subprocess.run(
                ["npx", "tsx", *(str(value) for value in runner_arguments)],
                cwd=kandelo_policy_root,
                env=dict(os.environ),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
                timeout=3 * 60 * 60 + 120,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ProductEvidenceError(
                f"protected product evidence runner failed to complete: {error}"
            ) from error
        if completed.returncode != 0 or not output.is_file():
            raise ProductEvidenceError(
                "protected product evidence runner emitted no terminal result"
            )
    result = load_canonical_mapping(
        output.read_bytes(), "protected product evidence result"
    )
    validate_product_evidence_result(result)
    if (
        result["request_digest"] != request_digest
        or result["product"]["id"] != scope["id"]
        or result["candidate_product"] != candidate.evidence_identity()
        or result["host"] != args.host
        or result["definition"]["id"] != args.definition_id
        or result["run"] != run
    ):
        raise ProductEvidenceError(
            "terminal product evidence result differs from protected work scope"
        )
    return 0


def _publish_workflow_product_evidence(args: argparse.Namespace) -> None:
    """Publish exact receipts and one aggregate from current-run inert results."""

    _require_workflow_publication_guards(args)
    if not args.require_terminal_results:
        raise WorkflowPublicationError(
            "product evidence publication requires every terminal host result"
        )
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
            "protected product evidence publisher differs from workflow head"
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

    with tempfile.TemporaryDirectory(
        prefix="abi-staging-product-evidence-publication-"
    ) as temporary:
        root = Path(temporary)
        coordination_name = (
            f"abi-staging-coordination-{args.run_id}-{args.run_attempt}"
        )
        coordination_artifact = client.artifact_by_name(name=coordination_name)
        coordination_root = root / "coordination"
        client.extract_artifact(
            coordination_artifact,
            coordination_root,
            max_files=policy.max_handoff_files,
            max_bytes=policy.max_handoff_bytes,
        )
        bundle = load_coordination_bundle(
            coordination_root / "coordination.json", policy=policy
        )
        request = bundle["request"]
        request_digest = bundle["request_sha256"]
        source = request["build_source"]
        kandelo_root = _checked_checkout_source(
            args.kandelo_root,
            repository=source["repository"],
            commit=source["commit"],
            tree=source["tree"],
        )
        policy_commit = request["issuance"]["issuer_workflow_ref"].rsplit(
            "@", 1
        )[-1]
        kandelo_policy_root = _checked_checkout_source(
            args.kandelo_policy_root,
            repository=source["repository"],
            commit=policy_commit,
            tree=None,
        )
        definitions_path = (
            kandelo_policy_root
            / "abi/staging/evidence-definitions.generated.json"
        )
        try:
            definitions = load_canonical_mapping(
                definitions_path.read_bytes(),
                "protected product evidence definitions",
            )
        except (OSError, ContractError) as error:
            raise WorkflowPublicationError(
                f"protected product evidence definitions are invalid: {error}"
            ) from error
        scope = select_product_evidence_publication_scope(
            request,
            request_sha256=request_digest,
            product_id=args.product_id,
            product_work_id=args.product_work_id,
            work_id=args.work_id,
            definitions=definitions,
        )

        runtime_name = (
            f"abi-staging-runtime-{request_digest}-"
            f"{args.run_id}-{args.run_attempt}"
        )
        runtime_artifact = client.artifact_by_name(name=runtime_name)
        runtime_root = root / "runtime"
        client.extract_artifact(
            runtime_artifact,
            runtime_root,
            max_files=65_536,
            max_bytes=8 * 1024**3,
        )
        try:
            runtime_body = (runtime_root / "runtime-bundle.json").read_bytes()
            runtime = load_canonical_mapping(
                runtime_body, "protected product evidence runtime"
            )
        except (OSError, ContractError) as error:
            raise WorkflowPublicationError(
                f"protected product evidence runtime is invalid: {error}"
            ) from error
        product_runtime_identity(runtime, request)
        runtime_sha256 = hashlib.sha256(runtime_body).hexdigest()

        handoff_name = (
            f"abi-staging-product-build-{args.product_id}-"
            f"{args.product_work_id}-{args.run_id}-{args.run_attempt}"
        )
        handoff_artifact = client.artifact_by_name(name=handoff_name)
        handoff_root = root / "product"
        client.extract_artifact(
            handoff_artifact,
            handoff_root,
            max_files=policy.max_handoff_files,
            max_bytes=policy.max_handoff_bytes,
        )
        product_result = validate_product_build_handoff(
            handoff_root,
            expected_product_id=args.product_id,
            expected_work_id=args.product_work_id,
            expected_request_sha256=request_digest,
            expected_runtime_bundle_sha256=runtime_sha256,
            max_files=policy.max_handoff_files,
            max_bytes=policy.max_handoff_bytes,
        )
        if product_result["outcome"] != "success":
            raise WorkflowPublicationError(
                "product evidence requires one successful exact composition"
            )

        candidate_name = (
            f"abi-staging-product-candidate-{args.product_id}-"
            f"{args.product_work_id}-{args.run_id}-{args.run_attempt}"
        )
        candidate_artifact = client.artifact_by_name(name=candidate_name)
        candidate_root = root / "candidate"
        client.extract_artifact(
            candidate_artifact,
            candidate_root,
            max_files=8,
            max_bytes=4 * 1024 * 1024,
        )
        expected_candidate_repository = "ghcr.io/" + candidate_product_repository(
            owner=policy.tap_repository.split("/", 1)[0],
            repository_prefix=(
                policy.tap_repository.split("/", 1)[1] + "-abi-"
            ),
            candidate_suffix="-candidates",
            target_abi=request["target_abi"]["version"],
            product_id=args.product_id,
        )
        candidate_path = candidate_root / "product-candidate.json"
        candidate = load_candidate_product_locator(
            candidate_path.read_bytes(),
            expected_repository=expected_candidate_repository,
        )
        if (
            candidate.product_id != product_result["product"]["id"]
            or candidate.builder_report_sha256
            != product_result["builder_report_sha256"]
            or candidate.vfs_layer_sha256 != product_result["vfs"]["sha256"]
            or candidate.vfs_layer_bytes != product_result["vfs"]["bytes"]
        ):
            raise WorkflowPublicationError(
                "candidate locator differs from exact product composition"
            )

        results = []
        for work in scope["evidence_work"]:
            host = work["host"]
            result_name = (
                f"abi-staging-product-{host}-{args.product_id}-"
                f"{work['work_id']}-{args.run_id}-{args.run_attempt}"
            )
            artifact = client.artifact_by_name(name=result_name)
            result_root = root / f"result-{host}-{work['definition_id']}"
            client.extract_artifact(
                artifact,
                result_root,
                max_files=4,
                max_bytes=8 * 1024 * 1024,
            )
            result_path = result_root / f"{host}-result"
            try:
                result = load_canonical_mapping(
                    result_path.read_bytes(),
                    f"terminal {host} product evidence result",
                )
            except (OSError, ContractError) as error:
                raise WorkflowPublicationError(
                    f"terminal {host} product evidence result is invalid: {error}"
                ) from error
            validate_product_evidence_result(result)
            expected_run = {
                "repository": repository,
                "workflow_ref": workflow_ref,
                "run_id": args.run_id,
                "job_id": f"{host}-product-evidence",
                "attempt": args.run_attempt,
            }
            if result["run"] != expected_run:
                raise WorkflowPublicationError(
                    "terminal product evidence result run identity changed"
                )
            results.append(result)

        publication_run = {
            "repository": repository,
            "workflow_ref": workflow_ref,
            "run_id": args.run_id,
            "run_attempt": args.run_attempt,
            "job": "publish-product-evidence",
        }
        try:
            resolved_body = (handoff_root / "resolved-inputs.json").read_bytes()
            resolved_inputs = load_resolved_product_inputs(resolved_body)
            builder_report_body = (
                handoff_root / "builder-report.json"
            ).read_bytes()
        except (OSError, ProductInputResolutionError) as error:
            raise WorkflowPublicationError(
                f"exact product evidence inputs are invalid: {error}"
            ) from error
        resolved_product = resolved_inputs["product"]
        if {
            "id": resolved_product["id"],
            "manifest_sha256": resolved_product["manifest_sha256"],
            "output": resolved_product["output"],
        } != product_result["product"]:
            raise WorkflowPublicationError(
                "resolved product differs from exact composition result"
            )
        username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
        package_token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
        _recheck_workflow_activation(tap_root)
        with isolated_oras_transport(
            username=username, token=package_token
        ) as publication_transport:
            published = publish_exact_product_evidence(
                request_digest=request_digest,
                product=resolved_product,
                candidate_product=candidate,
                runtime_bundle_body=runtime_body,
                resolved_inputs_body=resolved_body,
                builder_report_body=builder_report_body,
                selecting_registries=scope["selecting_registries"],
                requirements=scope["requirements"],
                results=results,
                run=publication_run,
                transport=publication_transport,
                expected_source_repository=policy.tap_repository,
            )

        output = Path(args.out)
        if output.exists() or output.is_symlink():
            raise WorkflowPublicationError(
                "product evidence publication output must be new"
            )
        output.write_bytes(
            canonical_bytes(
                {
                    "schema": 1,
                    "kind": "kandelo-vfs-product-evidence-publication",
                    "request_sha256": request_digest,
                    "product_id": args.product_id,
                    "product_work_id": args.product_work_id,
                    "work_id": args.work_id,
                    "record_sha256": canonical_sha256(published["record"]),
                    "record_locator": asdict(published["record_locator"]),
                    "receipt_locators": [
                        {
                            "requirement": receipt["requirement"],
                            "locator": asdict(locator),
                        }
                        for receipt, locator in zip(
                            published["receipts"],
                            published["receipt_locators"],
                            strict=True,
                        )
                    ],
                }
            )
        )


def _promotion_decision_from_detail(
    detail: Mapping[str, Any],
) -> PromotionDecisionV1:
    value = detail.get("decision")
    if not isinstance(value, Mapping):
        raise WorkflowPublicationError("promotion work has no decision")
    try:
        validate_promotion_decision(value)
        return PromotionDecisionV1(
            request_digest=value["request_digest"],
            merged_pull_request=dict(value["merged_pull_request"]),
            formula_subject=value["formula_subject"],
            tap_plan_digest=value["tap_plan_digest"],
            candidate_record_digest=value["candidate_record_digest"],
            candidate_binding_digest=value["candidate_binding_digest"],
            bottle_layer_sha256=value["bottle_layer_sha256"],
            bottle_layer_bytes=value["bottle_layer_bytes"],
            source_custody_digest=value["source_custody_digest"],
            qualifying_receipts=tuple(value["qualifying_receipts"]),
            override_receipts=tuple(value["override_receipts"]),
            tap_source_state=value["tap_source_state"],
            eligibility=value["eligibility"],
        )
    except (KeyError, TypeError, PromotionError) as error:
        raise WorkflowPublicationError(
            f"promotion work decision is invalid: {error}"
        ) from error


def _promotion_candidate_and_canonical(
    detail: Mapping[str, Any],
    *,
    policy: Any,
    transport: Any,
) -> tuple[
    PromotionDecisionV1,
    FetchedOciRecordV1,
    FetchedOciRecordV1 | None,
    CanonicalBottlePublicationV1,
]:
    decision = _promotion_decision_from_detail(detail)
    locator = detail.get("candidate_locator")
    if not isinstance(locator, Mapping):
        raise WorkflowPublicationError("promotion work has no candidate locator")
    candidate = _fetch_candidate_record(locator, transport=transport)
    if candidate.digest.removeprefix("sha256:") != decision.candidate_record_digest:
        raise WorkflowPublicationError(
            "promotion candidate differs from protected decision"
        )
    reuse_locator = detail.get("candidate_reuse_locator")
    candidate_reuse = None
    if reuse_locator is not None:
        if not isinstance(reuse_locator, Mapping):
            raise WorkflowPublicationError(
                "promotion candidate reuse locator is invalid"
            )
        candidate_reuse = _fetch_candidate_reuse(
            reuse_locator, transport=transport
        )
    try:
        validate_promotion_candidate_binding(
            decision,
            candidate=candidate,
            candidate_reuse=candidate_reuse,
        )
    except PromotionError as error:
        raise WorkflowPublicationError(
            f"promotion candidate binding is invalid: {error}"
        ) from error
    expected = expected_canonical_publication(
        decision,
        candidate=candidate,
        policy=policy,
    )
    declared = detail.get("canonical")
    expected_value = {
        "locator": asdict(expected.locator),
        "artifact": dict(expected.artifact),
    }
    if not isinstance(declared, Mapping) or canonical_bytes(declared) != canonical_bytes(
        expected_value
    ):
        raise WorkflowPublicationError(
            "promotion canonical identity differs from unchanged-layer plan"
        )
    return decision, candidate, candidate_reuse, expected


def _promotion_writer_authority(
    args: argparse.Namespace,
) -> tuple[Path, Any, dict[str, str], GitHubWorkflowArtifactClientV1]:
    tap_root = TAP_ROOT.resolve(strict=True)
    staging_policy = load_tap_staging_policy(
        tap_root / "Kandelo/staging/tap-policy.toml"
    )
    promotion_policy = load_promotion_policy(
        tap_root / "Kandelo/staging/promotion-policy.toml"
    )
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if (
        repository != staging_policy.tap_repository
        or repository != promotion_policy.tap_repository
    ):
        raise WorkflowPublicationError(
            "promotion writer repository differs from protected policy"
        )
    tap_source = snapshot_tap_source(tap_root, promotion_policy.tap_repository)
    if tap_source["commit"] != args.head_sha:
        raise WorkflowPublicationError(
            "promotion writer checkout differs from protected workflow head"
        )
    if (
        load_promotion_activation(
            tap_root / "Kandelo/staging/promotion-activation.toml"
        ).mode
        != "active"
    ):
        raise WorkflowPublicationError("promotion writer is not active")
    return (
        tap_root,
        promotion_policy,
        tap_source,
        GitHubWorkflowArtifactClientV1(
            repository,
            token,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            head_sha=args.head_sha,
            workflow_ref=workflow_ref,
        ),
    )


def _require_promotion_history_barrier(
    detail: Mapping[str, Any],
    *,
    policy: Any,
    expected_target_abi: int | None,
    transport: Any,
) -> tuple[FetchedOciRecordV1, dict[str, str]]:
    """Fetch and recheck the protected ABI history immediately before a write."""

    locator = detail.get("history_locator")
    if not isinstance(locator, Mapping):
        raise WorkflowPublicationError(
            "promotion work has no exact history locator"
        )
    history = fetch_public_record(
        locator,
        transport=transport,
        expected_artifact_type=HISTORY_RECORD_MEDIA_TYPE,
        required_layer_roles=("immutable-record-bytes",),
    )
    try:
        history_record = load_canonical_mapping(
            history.config.body, "writer ABI history"
        )
    except (ContractError, ValueError) as error:
        raise WorkflowPublicationError(
            f"writer ABI history is invalid: {error}"
        ) from error
    target_abi = history_record.get("plan", {}).get("successor_abi")
    if (
        not isinstance(target_abi, int)
        or isinstance(target_abi, bool)
        or (expected_target_abi is not None and target_abi != expected_target_abi)
    ):
        raise WorkflowPublicationError(
            "writer ABI history names another target ABI"
        )
    source, branch = _history_epoch_authority(
        history,
        policy=policy,
        target_abi=target_abi,
    )
    snapshot = GitHubHistoryClient(
        policy.tap_repository,
        os.environ.get("GITHUB_TOKEN", ""),
    ).protection_snapshot(branch, phase="postcreate")
    validate_promotion_history_barrier(
        history,
        protection_snapshot=snapshot,
        target_abi=target_abi,
        tap_source=source,
        policy=policy,
    )
    return history, source


def _candidate_target_abi(candidate: FetchedOciRecordV1) -> int:
    try:
        record = load_canonical_mapping(candidate.config.body, "candidate record")
        validate_candidate_record(record)
        target_abi = record["candidate"]["formula"]["target_abi"]
    except (ContractError, KeyError, TapRecordError, TypeError) as error:
        raise WorkflowPublicationError(
            f"promotion candidate target ABI is invalid: {error}"
        ) from error
    if not isinstance(target_abi, int) or isinstance(target_abi, bool) or target_abi < 0:
        raise WorkflowPublicationError("promotion candidate target ABI is invalid")
    return target_abi


def _load_named_workflow_document(
    client: GitHubWorkflowArtifactClientV1,
    *,
    root: Path,
    name: str,
    filename: str,
    field: str,
) -> dict[str, Any]:
    artifact = client.artifact_by_name(name=name)
    destination = root / canonical_sha256({"name": name, "filename": filename})
    client.extract_artifact(
        artifact,
        destination,
        max_files=4,
        max_bytes=64 * 1024 * 1024,
    )
    try:
        return load_canonical_mapping(
            (destination / filename).read_bytes(), field
        )
    except (OSError, ContractError) as error:
        raise WorkflowPublicationError(f"{field} is invalid: {error}") from error


def _published_canonical_from_document(
    document: Mapping[str, Any],
    *,
    work_id: str,
    request_digest: str,
    expected: CanonicalBottlePublicationV1,
) -> CanonicalBottlePublicationV1:
    if set(document) != {
        "schema",
        "kind",
        "request_sha256",
        "work_id",
        "candidate_record_sha256",
        "canonical",
    }:
        raise WorkflowPublicationError("canonical publication fields changed")
    canonical = document["canonical"]
    expected_value = {
        "locator": asdict(expected.locator),
        "artifact": dict(expected.artifact),
    }
    if (
        document["schema"] != 1
        or document["kind"]
        != "kandelo-abi-staging-canonical-publication"
        or document["request_sha256"] != request_digest
        or document["work_id"] != work_id
        or canonical_bytes(canonical) != canonical_bytes(expected_value)
    ):
        raise WorkflowPublicationError(
            "canonical publication differs from exact promotion work"
        )
    return expected


def _publish_workflow_canonical(args: argparse.Namespace) -> None:
    if not args.require_unchanged_layer or not args.require_history_barrier:
        raise WorkflowPublicationError(
            "canonical publication requires unchanged bytes and protected history"
        )
    _require_workflow_publication_guards(args)
    tap_root, policy, tap_source, client = _promotion_writer_authority(args)
    with tempfile.TemporaryDirectory(
        prefix="kandelo-abi-staging-canonical-writer-"
    ) as temporary:
        root = Path(temporary)
        work = _load_workflow_promotion_work(
            client,
            root=root,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            request_digest=args.request_digest,
            artifact_id=args.plan_artifact_id,
            artifact_digest=args.plan_artifact_digest,
            expected_tap_source=tap_source,
            stage="canonical",
            work_id=args.work_id,
        )
        detail = work["detail"]
        username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
        token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
        with isolated_oras_transport(username=username, token=token) as transport:
            decision, candidate, _candidate_reuse, expected = (
                _promotion_candidate_and_canonical(
                    detail,
                    policy=policy,
                    transport=transport,
                )
            )
            _require_promotion_history_barrier(
                detail,
                policy=policy,
                expected_target_abi=_candidate_target_abi(candidate),
                transport=UrllibOciTransportV1(username="", token=""),
            )
            plan = build_canonical_bottle_plan(
                decision,
                candidate=candidate,
                policy=policy,
            )
            published = publish_canonical_bottle(
                plan,
                decision=decision,
                candidate=candidate,
                policy=policy,
                transport=transport,
            )
        if published != expected:
            raise WorkflowPublicationError(
                "canonical publication differs from protected expected readback"
            )
        Path(args.out).write_bytes(
            canonical_bytes(
                {
                    "schema": 1,
                    "kind": "kandelo-abi-staging-canonical-publication",
                    "request_sha256": args.request_digest,
                    "work_id": args.work_id,
                    "candidate_record_sha256": decision.candidate_record_digest,
                    "canonical": {
                        "locator": asdict(published.locator),
                        "artifact": dict(published.artifact),
                    },
                }
            )
        )


def _metadata_readback_document(
    *,
    work_id: str,
    request_digest: str,
    result: Any,
    patch_document: Mapping[str, Any],
    formula_update: Any,
    tap_root: Path,
) -> dict[str, Any]:
    files = []
    for relative in result.changed_paths:
        body = (tap_root / relative).read_bytes()
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
        )
    update = None if formula_update is None else asdict(formula_update)
    post_write = {
        "source": dict(result.source),
        "formula_metadata_update": update,
    }
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-metadata-readback",
        "request_sha256": request_digest,
        "work_id": work_id,
        "operation": patch_document["operation"],
        "status": result.status,
        "source": dict(result.source),
        "metadata_patch_sha256": canonical_sha256(patch_document),
        "formula_metadata_update": update,
        "post_write_readback": post_write,
        "post_write_readback_sha256": canonical_sha256(post_write),
        "changed_files": files,
    }


def _configure_metadata_committer(tap_root: Path) -> None:
    actor = os.environ.get("GITHUB_ACTOR", "github-actions[bot]")
    if not actor or any(character in actor for character in "\n\r\0"):
        raise WorkflowPublicationError("metadata writer actor is invalid")
    for key, value in (
        ("user.name", actor),
        ("user.email", actor + "@users.noreply.github.com"),
    ):
        completed = subprocess.run(
            ["git", "-C", str(tap_root), "config", "--local", key, value],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode != 0:
            raise WorkflowPublicationError(
                "metadata writer cannot configure its protected committer"
            )


def _update_workflow_tap_metadata(args: argparse.Namespace) -> None:
    if not (
        args.contents_only
        and args.normal_push
        and args.post_write_readback
        and args.require_history_barrier
    ):
        raise WorkflowPublicationError(
            "metadata writer requires contents-only push, readback, and history"
        )
    tap_root, policy, tap_source, client = _promotion_writer_authority(args)
    stage = "activation" if args.operation == "successor-activation" else "metadata"
    with tempfile.TemporaryDirectory(
        prefix="kandelo-abi-staging-metadata-writer-"
    ) as temporary:
        root = Path(temporary)
        work = _load_workflow_promotion_work(
            client,
            root=root,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            request_digest=args.request_digest,
            artifact_id=args.plan_artifact_id,
            artifact_digest=args.plan_artifact_digest,
            expected_tap_source=tap_source,
            stage=stage,
            work_id=args.work_id,
        )
        detail = work["detail"]
        patch_value = detail.get("metadata_patch")
        if not isinstance(patch_value, Mapping):
            raise WorkflowPublicationError("metadata work has no exact patch")
        patch, formula_update = load_metadata_patch_document(
            canonical_bytes(patch_value)
        )
        if (formula_update is None) != (args.operation == "successor-activation"):
            raise WorkflowPublicationError(
                "metadata work operation differs from protected patch"
            )
        if formula_update is not None:
            anonymous = UrllibOciTransportV1(username="", token="")
            decision, candidate, _candidate_reuse, expected = (
                _promotion_candidate_and_canonical(
                    detail,
                    policy=policy,
                    transport=anonymous,
                )
            )
            readback = read_canonical_publication(
                decision,
                candidate=candidate,
                policy=policy,
                transport=anonymous,
            )
            if readback != expected:
                raise WorkflowPublicationError(
                    "metadata writer lacks exact canonical public readback"
                )
            canonical_work_id = detail.get("canonical_work_id")
            if canonical_work_id is not None:
                publication = _load_named_workflow_document(
                    client,
                    root=root,
                    name=(
                        f"abi-staging-canonical-{canonical_work_id}-"
                        f"{args.run_id}-{args.run_attempt}"
                    ),
                    filename="canonical-publication.json",
                    field="canonical publication handoff",
                )
                _published_canonical_from_document(
                    publication,
                    work_id=canonical_work_id,
                    request_digest=args.request_digest,
                    expected=expected,
                )
        _require_promotion_history_barrier(
            detail,
            policy=policy,
            expected_target_abi=(
                None if formula_update is None else formula_update.target_abi
            ),
            transport=UrllibOciTransportV1(username="", token=""),
        )
        _configure_metadata_committer(tap_root)
        result = apply_metadata_patch(
            tap_root,
            patch,
            formula_update=formula_update,
            commit_message=(
                "[ABI] Activate protected successor metadata"
                if formula_update is None
                else "[Homebrew] Admit canonical " + formula_update.formula
            ),
            store=GitTapMetadataStore(tap_root, remote="origin"),
        )
        check_tap_metadata(tap_root)
        output = _metadata_readback_document(
            work_id=args.work_id,
            request_digest=args.request_digest,
            result=result,
            patch_document=patch_value,
            formula_update=formula_update,
            tap_root=tap_root,
        )
        Path(args.out).write_bytes(canonical_bytes(output))


def _load_metadata_handoff(
    client: GitHubWorkflowArtifactClientV1,
    *,
    root: Path,
    work_id: str,
    run_id: int,
    run_attempt: int,
    request_digest: str,
) -> dict[str, Any]:
    document = _load_named_workflow_document(
        client,
        root=root,
        name=f"abi-staging-metadata-{work_id}-{run_id}-{run_attempt}",
        filename="metadata-readback.json",
        field="metadata readback handoff",
    )
    expected_keys = {
        "schema",
        "kind",
        "request_sha256",
        "work_id",
        "operation",
        "status",
        "source",
        "metadata_patch_sha256",
        "formula_metadata_update",
        "post_write_readback",
        "post_write_readback_sha256",
        "changed_files",
    }
    if (
        set(document) != expected_keys
        or document["schema"] != 1
        or document["kind"] != "kandelo-abi-staging-metadata-readback"
        or document["request_sha256"] != request_digest
        or document["work_id"] != work_id
        or document["operation"] != "formula-metadata"
        or document["status"] not in {"committed", "already-landed"}
        or canonical_sha256(document["post_write_readback"])
        != document["post_write_readback_sha256"]
    ):
        raise WorkflowPublicationError(
            "metadata readback handoff differs from exact promotion work"
        )
    return document


def _require_remote_metadata_source(
    metadata_root: Path, source: Mapping[str, Any], *, repository: str
) -> dict[str, str]:
    current = snapshot_tap_source(metadata_root, repository)
    store = GitTapMetadataStore(metadata_root, remote="origin")
    if store.remote_main() != current["commit"]:
        raise WorkflowPublicationError(
            "metadata checkout is not current public main"
        )
    client = GitHubHistoryClient(
        repository, os.environ.get("GITHUB_TOKEN", "")
    )
    reference = client.read("main")
    if (
        reference is None
        or reference.object_sha != current["commit"]
        or reference.tree_sha != current["tree"]
    ):
        raise WorkflowPublicationError(
            "metadata checkout lacks exact public main commit/tree"
        )
    if source.get("repository", "").lower() != repository.lower():
        raise WorkflowPublicationError("metadata readback names another tap")
    try:
        source_tree = subprocess.run(
            [
                "git",
                "-C",
                str(metadata_root),
                "rev-parse",
                f"{source.get('commit')}^{{tree}}",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "-C",
                str(metadata_root),
                "merge-base",
                "--is-ancestor",
                str(source.get("commit")),
                "HEAD",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise WorkflowPublicationError(
            "metadata readback commit is not on current public main"
        ) from error
    if source_tree != source.get("tree"):
        raise WorkflowPublicationError(
            "metadata readback commit tree identity changed"
        )
    return current


def _publish_workflow_admission(args: argparse.Namespace) -> None:
    if not args.require_metadata_readback or not args.require_history_barrier:
        raise WorkflowPublicationError(
            "admission publication requires exact metadata and protected history"
        )
    _require_workflow_publication_guards(args)
    tap_root, policy, tap_source, client = _promotion_writer_authority(args)
    metadata_root = Path(args.metadata_root).resolve(strict=True)
    with tempfile.TemporaryDirectory(
        prefix="kandelo-abi-staging-admission-writer-"
    ) as temporary:
        root = Path(temporary)
        work = _load_workflow_promotion_work(
            client,
            root=root,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            request_digest=args.request_digest,
            artifact_id=args.plan_artifact_id,
            artifact_digest=args.plan_artifact_digest,
            expected_tap_source=tap_source,
            stage="admission",
            work_id=args.work_id,
        )
        detail = work["detail"]
        username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
        token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
        with isolated_oras_transport(username=username, token=token) as transport:
            decision, candidate, candidate_reuse, expected = (
                _promotion_candidate_and_canonical(
                    detail,
                    policy=policy,
                    transport=transport,
                )
            )
            canonical = read_canonical_publication(
                decision,
                candidate=candidate,
                policy=policy,
                transport=transport,
            )
            if canonical != expected:
                raise WorkflowPublicationError(
                    "admission lacks exact canonical public readback"
                )
            canonical_work_id = detail.get("canonical_work_id")
            if canonical_work_id is not None:
                publication = _load_named_workflow_document(
                    client,
                    root=root,
                    name=(
                        f"abi-staging-canonical-{canonical_work_id}-"
                        f"{args.run_id}-{args.run_attempt}"
                    ),
                    filename="canonical-publication.json",
                    field="canonical publication handoff",
                )
                _published_canonical_from_document(
                    publication,
                    work_id=canonical_work_id,
                    request_digest=args.request_digest,
                    expected=expected,
                )
            patch_value = detail.get("metadata_patch")
            if not isinstance(patch_value, Mapping):
                raise WorkflowPublicationError(
                    "admission work has no Formula metadata authority"
                )
            _patch, formula_update = load_metadata_patch_document(
                canonical_bytes(patch_value)
            )
            if formula_update is None:
                raise WorkflowPublicationError(
                    "admission work carries an activation patch"
                )
            metadata_work_id = detail.get("metadata_work_id")
            if metadata_work_id is not None:
                metadata = _load_metadata_handoff(
                    client,
                    root=root,
                    work_id=metadata_work_id,
                    run_id=args.run_id,
                    run_attempt=args.run_attempt,
                    request_digest=args.request_digest,
                )
                if (
                    metadata["metadata_patch_sha256"]
                    != canonical_sha256(patch_value)
                    or canonical_bytes(metadata["formula_metadata_update"])
                    != canonical_bytes(asdict(formula_update))
                ):
                    raise WorkflowPublicationError(
                        "metadata handoff differs from admission authority"
                    )
                metadata_source = metadata["source"]
                post_write = metadata["post_write_readback"]
            else:
                summary = work["summary"]
                metadata_source = {
                    "repository": policy.tap_repository,
                    "commit": summary["metadata_commit"],
                    "tree": summary["metadata_tree"],
                }
                post_write = {
                    "source": metadata_source,
                    "formula_metadata_update": asdict(formula_update),
                }
                if (
                    canonical_sha256(asdict(formula_update))
                    != summary["metadata_update_sha256"]
                    or canonical_sha256(post_write)
                    != summary["metadata_readback_sha256"]
                ):
                    raise WorkflowPublicationError(
                        "resumed admission metadata identity changed"
                    )
            _require_remote_metadata_source(
                metadata_root,
                metadata_source,
                repository=policy.tap_repository,
            )
            metadata_base_source = detail.get("formula_metadata_base_source")
            if not isinstance(metadata_base_source, Mapping):
                raise WorkflowPublicationError(
                    "admission work has no exact Formula metadata base"
                )
            try:
                validate_landed_formula_metadata_commit(
                    metadata_root,
                    base_source=metadata_base_source,
                    landed_source=metadata_source,
                    patch=_patch,
                )
                validate_formula_admission_projection(
                    metadata_root, formula_update
                )
            except TapMetadataError as error:
                raise WorkflowPublicationError(
                    f"landed Formula metadata is invalid: {error}"
                ) from error
            _history, history_tap_source = _require_promotion_history_barrier(
                detail,
                policy=policy,
                expected_target_abi=formula_update.target_abi,
                transport=UrllibOciTransportV1(username="", token=""),
            )
            prepared = prepare_admission(
                decision,
                candidate=candidate,
                candidate_reuse=candidate_reuse,
                canonical_publication=canonical,
                preactivation_tap_source=history_tap_source,
                abi_history_record_sha256=_history.digest.removeprefix("sha256:"),
                policy=policy,
            )
            record = finalize_admission_record(
                prepared,
                formula_metadata_base_source=metadata_base_source,
                formula_metadata_source=metadata_source,
                formula_metadata_update=asdict(formula_update),
                post_write_readback=post_write,
                run={
                    "repository": policy.tap_repository,
                    "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF", ""),
                    "run_id": args.run_id,
                    "run_attempt": args.run_attempt,
                    "job": "publish-admission",
                },
            )
            published = publish_admission_record(
                record,
                policy=policy,
                transport=transport,
            )
        Path(args.out).write_bytes(
            canonical_bytes(
                {
                    "schema": 1,
                    "kind": "kandelo-abi-staging-admission-publication",
                    "request_sha256": args.request_digest,
                    "work_id": args.work_id,
                    "record_sha256": canonical_sha256(record),
                    "locator": asdict(published),
                }
            )
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


def _load_workflow_promotion_work(
    client: GitHubWorkflowArtifactClientV1,
    *,
    root: Path,
    run_id: int,
    run_attempt: int,
    request_digest: str,
    artifact_id: str,
    artifact_digest: str,
    expected_tap_source: Mapping[str, Any],
    stage: str,
    work_id: str,
) -> dict[str, Any]:
    artifact = _workflow_artifact_output(
        client,
        artifact_id=artifact_id,
        artifact_digest=artifact_digest,
        name=(
            f"abi-staging-promotion-plan-{request_digest}-"
            f"{run_id}-{run_attempt}"
        ),
        required=True,
    )
    if artifact is None:
        raise WorkflowPublicationError("protected promotion plan is absent")
    destination = root / "promotion-plan"
    client.extract_artifact(
        artifact,
        destination,
        max_files=4,
        max_bytes=256 * 1024 * 1024,
    )
    try:
        document = load_promotion_plan_document(
            (destination / "promotion-plan.json").read_bytes()
        )
    except (OSError, ReconciliationError) as error:
        raise ReconciliationError(
            f"protected promotion plan cannot be loaded: {error}"
        ) from error
    if (
        document["mode"] != "active"
        or document["authoritative"] is not True
        or document["request_sha256"] != request_digest
        or document["tap_source"] != dict(expected_tap_source)
    ):
        raise ReconciliationError(
            "protected promotion plan differs from current workflow authority"
        )
    return select_promotion_plan_work(document, stage=stage, work_id=work_id)


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
    actor = os.environ.get("GITHUB_ACTOR", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if repository != policy.tap_repository:
        raise WorkflowPublicationError("current workflow repository differs from tap policy")
    if not actor or not token:
        raise WorkflowPublicationError("candidate publisher lacks workflow package authority")
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
        lifecycle = GitHubPublicClient(
            issuer_policy, api_token=token
        ).pull_request_lifecycle(bundle["request"]["pull_request"]["number"])
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
                            # A personal package token creates a new granular
                            # GHCR package as private. The repository-scoped
                            # workflow token creates the source-linked package
                            # with this public tap's visibility instead.
                            candidate_publication = _publish_candidate_paths(
                                tap_root_value=tap_root,
                                handoff_value=handoff_root,
                                request_value=request_path,
                                tap_plan_value=tap_plan_path,
                                formula_plan_value=formula_plan_path,
                                publication_run_value=publication_run_path,
                                registry_username=actor,
                                registry_token=token,
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
        with isolated_oras_transport(
            username=actor, token=token
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
    *,
    github_api_token: str,
) -> None:
    issuer_policy = load_request_issuer_policy(
        tap_root / "Kandelo/staging/request-issuers.toml",
        expected_tap=policy.tap_repository,
    )
    lifecycle = GitHubPublicClient(
        issuer_policy, api_token=github_api_token
    ).pull_request_lifecycle(bundle["request"]["pull_request"]["number"])
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
        _recheck_coordinated_lifecycle(
            tap_root, policy, bundle, github_api_token=token
        )
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
        _recheck_coordinated_lifecycle(
            tap_root, policy, bundle, github_api_token=token
        )
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


def _history_output_directory(value: str) -> Path:
    path = Path(value)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise AbiHistoryError("history output must be a real directory")
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise AbiHistoryError("history output directory must begin empty")
    return path.resolve(strict=True)


def _history_repository(tap_root: Path, repository: str) -> tuple[Any, Any]:
    policy = load_promotion_policy(
        tap_root / "Kandelo/staging/promotion-policy.toml"
    )
    activation = load_promotion_activation(
        tap_root / "Kandelo/staging/promotion-activation.toml"
    )
    if repository.lower() != policy.tap_repository.lower():
        raise AbiHistoryError("workflow repository differs from promotion policy")
    return policy, activation


def _history_client(repository: str) -> GitHubHistoryClient:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise AbiHistoryError("protected history command requires GITHUB_TOKEN")
    return GitHubHistoryClient(repository, token)


def _git_identity(root: Path) -> tuple[str, str]:
    def read(revision: str) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", revision],
                cwd=root,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise AbiHistoryError(f"cannot read exact tap Git identity: {error}") from error
        value = result.stdout.strip()
        if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
            raise AbiHistoryError("tap Git identity is not a full lowercase SHA")
        return value

    return read("HEAD"), read("HEAD^{tree}")


def _load_history_plan(path: str) -> dict[str, Any]:
    try:
        value = load_canonical_mapping(
            Path(path).resolve(strict=True).read_bytes(), "ABI history plan"
        )
    except (OSError, ValueError) as error:
        raise AbiHistoryError(f"cannot load ABI history plan: {error}") from error
    return validate_history_plan(value)


def _plan_history(args: argparse.Namespace) -> None:
    tap_root = _protected_tap_root(args.tap_root)
    policy, activation = _history_repository(tap_root, args.repository)
    output = _history_output_directory(args.out)
    commit, tree = _git_identity(tap_root)
    plan = build_history_plan(
        tap_root,
        preactivation_tap_commit=commit,
        preactivation_tap_tree=tree,
    )
    client = _history_client(policy.tap_repository)
    snapshot = client.protection_snapshot(plan["branch"], phase="precreate")
    decision = ensure_history_ref(
        plan,
        client,
        snapshot,
        mode="observe",
        expected_repository=policy.tap_repository,
    )
    (output / "plan.json").write_bytes(canonical_bytes(plan))
    (output / "precreate-protection.json").write_bytes(canonical_bytes(snapshot))
    (output / "decision.json").write_bytes(
        canonical_bytes(
            {
                "schema": 1,
                "kind": "kandelo-abi-history-plan-decision",
                "mode": activation.mode,
                "write_enabled": activation.mode == "active",
                "decision": decision,
                "plan_sha256": canonical_sha256(plan),
            }
        )
    )
    _write_github_outputs(
        Path(args.github_output),
        {
            "branch": plan["branch"],
            "mode": activation.mode,
            "tap_commit": commit,
            "tap_tree": tree,
            "write_enabled": "true" if activation.mode == "active" else "false",
        },
    )


def _create_history_ref(args: argparse.Namespace) -> None:
    tap_root = _protected_tap_root(args.tap_root)
    policy, activation = _history_repository(tap_root, args.repository)
    if activation.mode != "active":
        raise AbiHistoryError("history ref creation requires active promotion mode")
    plan = _load_history_plan(args.plan)
    commit, tree = _git_identity(tap_root)
    current = build_history_plan(
        tap_root,
        preactivation_tap_commit=commit,
        preactivation_tap_tree=tree,
    )
    if canonical_bytes(current) != canonical_bytes(plan):
        raise AbiHistoryError("history plan differs from the exact protected checkout")
    output = _history_output_directory(args.out)
    client = _history_client(policy.tap_repository)
    snapshot = client.protection_snapshot(plan["branch"], phase="precreate")
    creation = ensure_history_ref(
        plan,
        client,
        snapshot,
        mode="active",
        expected_repository=policy.tap_repository,
    )
    result = {
        "schema": 1,
        "kind": "kandelo-abi-history-ref-creation",
        "plan_sha256": canonical_sha256(plan),
        **creation,
    }
    (output / "creation.json").write_bytes(canonical_bytes(result))
    (output / "precreate-protection.json").write_bytes(canonical_bytes(snapshot))
    _write_github_outputs(
        Path(args.github_output),
        {
            "branch": plan["branch"],
            "ref_object": plan["preactivation_tap_commit"],
            "ref_tree": plan["preactivation_tap_tree"],
        },
    )


def _verify_history(args: argparse.Namespace) -> None:
    if not args.anonymous_readback:
        raise AbiHistoryError("history verification requires anonymous readback")
    tap_root = _protected_tap_root(args.tap_root)
    policy, activation = _history_repository(tap_root, args.repository)
    if activation.mode != "active":
        raise AbiHistoryError("history publication requires active promotion mode")
    plan = _load_history_plan(args.plan)
    try:
        creation = load_canonical_mapping(
            Path(args.creation).resolve(strict=True).read_bytes(),
            "history ref creation",
        )
    except (OSError, ValueError) as error:
        raise AbiHistoryError(f"cannot load history ref creation: {error}") from error
    validate_history_creation_handoff(plan, creation)
    history_root = Path(args.history_root).resolve(strict=True)
    output = _history_output_directory(args.out)
    client = _history_client(policy.tap_repository)
    snapshot = client.protection_snapshot(plan["branch"], phase="postcreate")
    public_client = GitHubHistoryClient(policy.tap_repository, "")
    verification = verify_history_snapshot(
        history_root,
        plan,
        public_client,
        snapshot,
        expected_repository=policy.tap_repository,
    )
    try:
        run_id = int(os.environ.get("GITHUB_RUN_ID", ""), 10)
        run_attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", ""), 10)
    except ValueError as error:
        raise AbiHistoryError("history workflow run identity is invalid") from error
    record = build_history_record(
        plan,
        created_ref_object=plan["preactivation_tap_commit"],
        protection_evidence=verification["protection_evidence"],
        metadata_verification_sha256=verification["metadata_verification_sha256"],
        public_readback_sha256=verification["public_readback_sha256"],
        run={
            "repository": policy.tap_repository,
            "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF", ""),
            "run_id": run_id,
            "run_attempt": run_attempt,
            "job": "verify-and-publish-history",
        },
    )
    (output / "record.json").write_bytes(canonical_bytes(record))
    (output / "postcreate-protection.json").write_bytes(canonical_bytes(snapshot))
    (output / "metadata-verification.json").write_bytes(
        canonical_bytes(verification["metadata_verification"])
    )
    (output / "public-readback.json").write_bytes(
        canonical_bytes(verification["public_readback"])
    )
    _write_github_outputs(
        Path(args.github_output),
        {"record_sha256": canonical_sha256(record)},
    )


def _publish_history_record(args: argparse.Namespace) -> None:
    if not args.anonymous_readback or not args.immutable:
        raise AbiHistoryError("history publication requires immutable anonymous readback")
    tap_root = _protected_tap_root(args.tap_root)
    policy, activation = _history_repository(tap_root, args.repository)
    if activation.mode != "active":
        raise AbiHistoryError("history publication requires active promotion mode")
    try:
        record = load_canonical_mapping(
            Path(args.record).resolve(strict=True).read_bytes(), "ABI history record"
        )
    except (OSError, ValueError) as error:
        raise AbiHistoryError(f"cannot load ABI history record: {error}") from error
    validate_abi_history_record(record)
    source_abi = record["plan"]["source_abi"]
    plan = build_history_oci_plan(
        record,
        repository=history_record_repository(policy.tap_repository, source_abi),
    )
    username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
    token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
    with isolated_oras_transport(username=username, token=token) as transport:
        published = publish_record(
            plan,
            transport=transport,
            expected_source_repository=policy.tap_repository,
        )
    Path(args.out).write_bytes(
        canonical_bytes(
            {
                "schema": 1,
                "kind": "kandelo-abi-history-publication",
                "record_sha256": canonical_sha256(record),
                "locator": asdict(published),
            }
        )
    )


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        if args.command == "plan-history":
            _plan_history(args)
            return 0
        if args.command == "create-history-ref":
            _create_history_ref(args)
            return 0
        if args.command == "verify-history":
            _verify_history(args)
            return 0
        if args.command == "publish-history-record":
            _publish_history_record(args)
            return 0
        if args.command == "validate-admission-projection":
            _validate_admission_projection(args)
            return 0
        if args.command == "discover-workflow-request":
            _discover_workflow_request(args)
            return 0
        if args.command == "prepare-workflow":
            _prepare_workflow(args)
            return 0
        if args.command == "plan-workflow-products":
            _plan_workflow_products(args)
            return 0
        if args.command == "plan-workflow-promotion":
            _plan_workflow_promotion(args)
            return 0
        if args.command == "publish-workflow-canonical":
            _publish_workflow_canonical(args)
            return 0
        if args.command == "update-workflow-tap-metadata":
            _update_workflow_tap_metadata(args)
            return 0
        if args.command == "publish-workflow-admission":
            _publish_workflow_admission(args)
            return 0
        if args.command == "execute-build-work":
            return _execute_build(args)
        if args.command == "export-runtime-realm":
            _export_runtime_realm(args)
            return 0
        if args.command == "export-build-realm":
            _export_build_realm(args)
            return 0
        if args.command == "export-verification-realm":
            _export_verification_realm(args)
            return 0
        if args.command == "execute-verification-work":
            return _execute_verification(args)
        if args.command == "execute-product-work":
            return _execute_product_work(args)
        if args.command == "execute-product-evidence-work":
            return _execute_product_evidence_work(args)
        if args.command == "publish-workflow-product-candidate":
            _publish_workflow_product_candidate(args)
            return 0
        if args.command == "publish-workflow-product-evidence":
            _publish_workflow_product_evidence(args)
            return 0
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
            "tap-metadata-check",
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
            elif args.command == "tap-metadata-check":
                sys.stdout.buffer.write(canonical_bytes(check_tap_metadata(tap_root)))
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
            product_inputs = (
                TAP_ROOT / "Kandelo/staging/fixtures/product/resolved-inputs.json"
            )
            product_report = (
                TAP_ROOT / "Kandelo/staging/fixtures/product/builder-report.json"
            )
            product_evidence = (
                TAP_ROOT / "Kandelo/staging/fixtures/product/evidence-record.json"
            )
            history_record = (
                TAP_ROOT / "Kandelo/staging/fixtures/abi-history-record.json"
            )
            promotion_decision = (
                TAP_ROOT / "Kandelo/staging/fixtures/promotion-decision.json"
            )
            admission_record = (
                TAP_ROOT / "Kandelo/staging/fixtures/admission-record.json"
            )
            if fixture == tap_plan.resolve(strict=True):
                load_tap_plan_record(fixture.read_bytes())
            elif fixture == bottle_contract.resolve(strict=True):
                load_bottle_contract(fixture.read_bytes())
            elif fixture == product_inputs.resolve(strict=True):
                load_resolved_product_inputs(fixture.read_bytes())
            elif fixture == product_report.resolve(strict=True):
                validate_candidate_builder_report(fixture.read_bytes())
            elif fixture == product_evidence.resolve(strict=True):
                validate_product_evidence_record(
                    load_canonical_mapping(fixture.read_bytes(), "product evidence fixture")
                )
            elif fixture == history_record.resolve(strict=True):
                validate_abi_history_record(
                    load_canonical_mapping(fixture.read_bytes(), "ABI history fixture")
                )
            elif fixture == promotion_decision.resolve(strict=True):
                validate_promotion_decision(
                    load_canonical_mapping(fixture.read_bytes(), "promotion fixture")
                )
            elif fixture == admission_record.resolve(strict=True):
                validate_admission_record(
                    load_canonical_mapping(fixture.read_bytes(), "admission fixture")
                )
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
        load_product_evidence_activation(
            TAP_ROOT / "Kandelo/staging/product-evidence-activation.toml"
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
        TapMetadataError,
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
        ProductInputResolutionError,
        ProductEvidenceError,
        AbiHistoryError,
        PromotionError,
    ) as error:
        print(f"abi-staging {args.command}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
