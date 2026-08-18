from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
sys.path.insert(0, str(TAP_ROOT))

from scripts.abi_staging.github_public import DiscoveredRequestV1
from scripts.abi_staging import cli as cli_module
from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cli import main as cli_main
from scripts.abi_staging.reconcile import (
    build_product_workflow_wave,
    build_product_workflow_seed,
    build_promotion_workflow_plan,
    HistoricalMaintenanceWorkScopeV1,
    ProductProgressV1,
    ProductSelectionV1,
    PromotionEpochV1,
    PromotionProgressV1,
    PromotionSubjectV1,
    PullRequestLifecycleV1,
    ReconciliationError,
    build_promotion_plan_document,
    load_reconciliation_activation,
    load_promotion_plan_document,
    historical_maintenance_work_scope,
    plan_product_reconciliation,
    reconcile_request,
    select_promotion_plan_work,
    select_reconciliation_cycle,
)
from scripts.abi_staging.plan import build_miniature_tap_plan_fixture, exact_formula_subject
from scripts.abi_staging.oci import PublishedRecordLocatorV1
from scripts.abi_staging.promotion import PromotionDecisionV1
from scripts.abi_staging.request import load_request_issuer_policy, validate_request
from scripts.abi_staging.tap_metadata import (
    FormulaMetadataUpdateV1,
    TapMetadataPatchV1,
)


FIXTURES = TAP_ROOT / "Kandelo/staging/fixtures/request"
POLICY = load_request_issuer_policy(
    TAP_ROOT / "Kandelo/staging/request-issuers.toml",
    expected_tap="kandelo-dev/homebrew-tap-core",
)


def historical_repair_plan() -> dict[str, object]:
    abi = 7
    source = {
        "repository": "kandelo-dev/homebrew-tap-core",
        "commit": "1" * 40,
        "tree": "2" * 40,
    }
    identity = {
        "authorization_sha256": "a" * 64,
        "history_record_sha256": "b" * 64,
        "abi": abi,
        "branch": f"abi/{abi}",
        "source": source,
        "formula": {
            "tap": source["repository"],
            "formula": "bash",
            "architecture": "wasm32",
        },
        "reason": "failed-package-repair",
        "expected_contract_sha256": "c" * 64,
        "dependencies": [],
        "reuse_record_sha256": None,
    }
    return {
        "schema": 1,
        "kind": "kandelo-abi-historical-repair-plan",
        "work_id": canonical_sha256(identity),
        "identity": identity,
        "metadata_branch": f"abi/{abi}",
        "candidate_repository": (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7-candidates/bash"
        ),
        "canonical_repository": (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7/bash"
        ),
        "build_required": True,
        "stages": [
            "build-uncredentialed",
            "publish-candidate-protected",
            "verify-uncredentialed",
            "publish-receipt-protected",
            "publish-canonical-protected",
            "update-historical-metadata-protected",
            "publish-admission-protected",
        ],
        "override_receipts": [],
        "preserve_prior_records": True,
    }


def discovered(name: str = "current-request.json") -> DiscoveredRequestV1:
    body = (FIXTURES / name).read_bytes()
    raw = json.loads(body)
    head = raw["build_source"]["commit"]
    digest = hashlib.sha256(body).hexdigest()
    asset_name = f"candidate-request-{head}-sha256-{digest}.json"
    url = f"https://github.com/Automattic/kandelo/releases/download/abi-staging-pr-19/{asset_name}"
    request = validate_request(body, asset_name, POLICY)
    return DiscoveredRequestV1(digest, asset_name, url, "abi-staging-pr-19", request)


class ReconciliationTests(unittest.TestCase):
    def test_canonical_progress_requests_the_canonical_tag_inventory(self) -> None:
        digest = "a" * 64
        repository = "ghcr.io/kandelo-dev/homebrew-tap-core-abi-43/mini"
        expected = SimpleNamespace(
            locator=SimpleNamespace(
                repository=repository,
                digest="sha256:" + digest,
            )
        )
        readback = SimpleNamespace(
            artifact={"sha256": digest},
            locator=SimpleNamespace(anonymous_readback_sha256="b" * 64),
        )
        transport = object()
        with (
            patch.object(
                cli_module,
                "expected_canonical_publication",
                return_value=expected,
            ),
            patch.object(
                cli_module,
                "list_public_record_locators",
                return_value=(
                    {
                        "repository": repository,
                        "digest": "sha256:" + digest,
                        "immutable_reference": repository + "@sha256:" + digest,
                    },
                ),
            ) as inventory,
            patch.object(
                cli_module,
                "read_canonical_publication",
                return_value=readback,
            ),
        ):
            canonical, progress = cli_module._canonical_progress(
                object(),
                candidate=object(),
                policy=object(),
                transport=transport,
            )

        self.assertIs(canonical, readback)
        self.assertEqual(progress.canonical_manifest_sha256, digest)
        inventory.assert_called_once_with(
            repository.removeprefix("ghcr.io/"),
            transport=transport,
            tag_prefix="canonical-sha256-",
            max_records=4096,
        )

    def test_historical_maintenance_reuses_normal_lane_without_gating_successor(self) -> None:
        plan = historical_repair_plan()
        scope = historical_maintenance_work_scope(plan)

        self.assertEqual(scope.work_id, plan["work_id"])
        self.assertEqual(scope.target_abi, 7)
        self.assertEqual(scope.metadata_branch, "abi/7")
        self.assertEqual(scope.build_action, "build-uncredentialed")
        self.assertEqual(scope.permitted_stages, tuple(plan["stages"]))
        self.assertEqual(
            scope.formula_subject,
            exact_formula_subject("bash", "wasm32"),
        )
        self.assertFalse(scope.gates_successor)

        changed = copy.deepcopy(plan)
        changed["metadata_branch"] = "main"
        with self.assertRaises(ReconciliationError):
            historical_maintenance_work_scope(changed)

        with self.assertRaises(ReconciliationError):
            HistoricalMaintenanceWorkScopeV1(
                work_id=scope.work_id,
                authorization_sha256=scope.authorization_sha256,
                formula_subject=scope.formula_subject,
                target_abi=scope.target_abi,
                tap_source=MappingProxyType(dict(scope.tap_source)),
                metadata_branch=scope.metadata_branch,
                build_action=scope.build_action,
                permitted_stages=("build-uncredentialed",),
            )

    def test_promotion_reconstructs_formula_roots_without_parallel_authority(self) -> None:
        tap_plan = build_miniature_tap_plan_fixture(TAP_ROOT)
        requirements = cli_module._formula_requirements_from_tap_plan(tap_plan)
        self.assertEqual(
            requirements,
            [
                {
                    "tap": "kandelo-dev/homebrew-tap-core",
                    "formula": "bash",
                    "architecture": "wasm32",
                    "uses": [
                        {
                            "product_id": "beta-tools",
                            "materialization": "embedded",
                        }
                    ],
                },
                {
                    "tap": "kandelo-dev/homebrew-tap-core",
                    "formula": "curl",
                    "architecture": "wasm32",
                    "uses": [
                        {
                            "product_id": "alpha-shell",
                            "materialization": "lazy",
                        }
                    ],
                },
                {
                    "tap": "kandelo-dev/homebrew-tap-core",
                    "formula": "libcurl",
                    "architecture": "wasm32",
                    "uses": [
                        {
                            "product_id": "beta-tools",
                            "materialization": "embedded",
                        }
                    ],
                },
            ],
        )

    def test_promotion_history_selection_recovers_the_unique_adjacent_epoch(self) -> None:
        record = json.loads(
            (TAP_ROOT / "Kandelo/staging/fixtures/abi-history-record.json").read_bytes()
        )
        fetched = SimpleNamespace(
            digest="sha256:" + "d" * 64,
            config=SimpleNamespace(body=canonical_bytes(record)),
        )
        selected = cli_module._select_exact_history_record(
            (fetched,),
            target_abi=8,
        )
        self.assertIs(selected, fetched)

        with self.assertRaises(ReconciliationError):
            cli_module._select_exact_history_record(
                (fetched, fetched),
                target_abi=8,
            )

    def test_activated_epoch_fetches_the_record_bound_by_abi_state(self) -> None:
        digest = "d" * 64
        fetched = SimpleNamespace(digest="sha256:" + digest)
        policy = SimpleNamespace(tap_repository="kandelo-dev/homebrew-tap-core")
        with patch.object(
            cli_module, "fetch_public_record", return_value=fetched
        ) as fetch, patch.object(
            cli_module, "list_public_record_locators"
        ) as listing:
            selected = cli_module._fetch_exact_history_record(
                policy=policy,
                target_abi=8,
                expected_digest=digest,
                transport=SimpleNamespace(),
            )

        self.assertIs(selected, fetched)
        listing.assert_not_called()
        locator = fetch.call_args.args[0]
        self.assertEqual(
            locator,
            {
                "repository": (
                    "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7-records/history"
                ),
                "digest": "sha256:" + digest,
                "immutable_reference": (
                    "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7-records/history"
                    "@sha256:"
                    + digest
                ),
            },
        )

    def test_history_epoch_authority_comes_from_the_immutable_record(self) -> None:
        record = json.loads(
            (TAP_ROOT / "Kandelo/staging/fixtures/abi-history-record.json").read_bytes()
        )
        fetched = SimpleNamespace(
            repository=(
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7-records/history"
            ),
            config=SimpleNamespace(body=canonical_bytes(record)),
        )

        source, branch = cli_module._history_epoch_authority(
            fetched,
            policy=SimpleNamespace(
                tap_repository="kandelo-dev/homebrew-tap-core",
                historical_branch_prefix="abi/",
            ),
            target_abi=8,
        )

        self.assertEqual(
            source,
            {
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": "1" * 40,
                "tree": "2" * 40,
            },
        )
        self.assertEqual(branch, "abi/7")

    def test_admission_progress_does_not_hide_current_metadata_drift(self) -> None:
        record = json.loads(
            (TAP_ROOT / "Kandelo/staging/fixtures/admission-record.json").read_bytes()
        )
        admission = record["admission"]
        current = PromotionProgressV1(
            canonical_manifest_sha256=admission["canonical"]["sha256"],
            canonical_readback_sha256=admission["canonical"]["sha256"],
        )
        fetched = SimpleNamespace(
            digest="sha256:" + "e" * 64,
            config=SimpleNamespace(body=canonical_bytes(record)),
        )
        decision = SimpleNamespace(
            request_digest=record["common"]["request_sha256"],
            candidate_record_digest=admission["candidate_record_sha256"],
            candidate_binding_digest=admission["candidate_binding_sha256"],
            merged_pull_request=admission["merged_pull_request"],
        )
        with patch.object(
            cli_module, "list_public_record_locators", return_value=({},)
        ), patch.object(
            cli_module, "fetch_public_record", return_value=fetched
        ), patch.object(
            cli_module,
            "validate_formula_admission_projection",
            side_effect=cli_module.TapMetadataError("metadata drift"),
            create=True,
        ):
            progress = cli_module._admission_progress(
                current,
                tap_root=TAP_ROOT,
                decision=decision,
                canonical=SimpleNamespace(artifact=admission["canonical"]),
                policy=cli_module.load_promotion_policy(
                    TAP_ROOT / "Kandelo/staging/promotion-policy.toml"
                ),
                history_record_sha256=admission["abi_history_record_sha256"],
                preactivation_tap_source=admission[
                    "preactivation_tap_source"
                ],
                target_abi=admission["formula_metadata_update"]["target_abi"],
                formula=admission["formula_metadata_update"]["formula"],
                transport=SimpleNamespace(),
            )

        self.assertEqual(
            progress.canonical_manifest_sha256,
            current.canonical_manifest_sha256,
        )
        self.assertEqual(
            progress.stale_admission_record_sha256,
            "e" * 64,
        )

    def test_admission_progress_requires_the_exact_history_epoch(self) -> None:
        record = json.loads(
            (TAP_ROOT / "Kandelo/staging/fixtures/admission-record.json").read_bytes()
        )
        admission = record["admission"]
        current = PromotionProgressV1(
            canonical_manifest_sha256=admission["canonical"]["sha256"],
            canonical_readback_sha256=admission["canonical"]["sha256"],
        )
        fetched = SimpleNamespace(
            digest="sha256:" + "e" * 64,
            config=SimpleNamespace(body=canonical_bytes(record)),
        )
        decision = SimpleNamespace(
            request_digest=record["common"]["request_sha256"],
            candidate_record_digest=admission["candidate_record_sha256"],
            candidate_binding_digest=admission["candidate_binding_sha256"],
            merged_pull_request=admission["merged_pull_request"],
        )
        with patch.object(
            cli_module, "list_public_record_locators", return_value=({},)
        ), patch.object(
            cli_module, "fetch_public_record", return_value=fetched
        ), patch.object(
            cli_module, "validate_formula_admission_projection"
        ) as projection:
            progress = cli_module._admission_progress(
                current,
                tap_root=TAP_ROOT,
                decision=decision,
                canonical=SimpleNamespace(artifact=admission["canonical"]),
                policy=cli_module.load_promotion_policy(
                    TAP_ROOT / "Kandelo/staging/promotion-policy.toml"
                ),
                history_record_sha256="f" * 64,
                preactivation_tap_source=admission[
                    "preactivation_tap_source"
                ],
                target_abi=admission["formula_metadata_update"]["target_abi"],
                formula=admission["formula_metadata_update"]["formula"],
                transport=SimpleNamespace(),
            )

        self.assertEqual(progress, current)
        projection.assert_not_called()

    def promotion_decision(
        self,
        name: str,
        *,
        architecture: str = "wasm32",
        eligibility: str = "eligible",
        tap_source_state: str = "exact",
        marker: str = "a",
    ) -> PromotionDecisionV1:
        request = discovered()
        digest = lambda suffix: hashlib.sha256(
            f"{marker}-{suffix}".encode("utf-8")
        ).hexdigest()
        return PromotionDecisionV1(
            request_digest=request.request_digest,
            merged_pull_request={
                "repository": request.request["pull_request"]["repository"],
                "number": request.request["pull_request"]["number"],
                "head": request.request["build_source"]["commit"],
                "merge_commit": "8" * 40,
            },
            formula_subject=exact_formula_subject(name, architecture),
            tap_plan_digest=digest("tap-plan"),
            candidate_record_digest=digest("candidate"),
            candidate_binding_digest=digest("candidate"),
            bottle_layer_sha256=digest("bottle"),
            bottle_layer_bytes=4096,
            source_custody_digest=digest("custody"),
            qualifying_receipts=(digest("receipt"),),
            override_receipts=(),
            tap_source_state=tap_source_state,
            eligibility=eligibility,
        )

    def promotion_epoch(self, *, activated: bool) -> PromotionEpochV1:
        request = discovered()
        return PromotionEpochV1(
            request_digest=request.request_digest,
            history_record_sha256="1" * 64,
            activation_patch_sha256="2" * 64,
            activation_record_sha256="3" * 64 if activated else None,
            current_tap_commit="4" * 40,
            current_tap_tree="5" * 40,
        )

    def merged_promotion_reconciliation(self) -> ReconciliationDecisionV1:
        request = discovered()
        return reconcile_request(
            request,
            PullRequestLifecycleV1(
                "merged", request.request["build_source"]["commit"], "8" * 40
            ),
        )

    def test_promotion_modes_require_exact_history_then_one_activation(self) -> None:
        reconciliation = self.merged_promotion_reconciliation()
        subject = PromotionSubjectV1(
            self.promotion_decision("alpha"), "required", ()
        )
        progress = {subject.decision.formula_subject: PromotionProgressV1()}

        disabled = build_promotion_workflow_plan(
            reconciliation,
            (subject,),
            epoch=self.promotion_epoch(activated=False),
            progress=progress,
            activation_mode="disabled",
        )
        self.assertEqual(disabled.activation_work, ())
        self.assertEqual(disabled.canonical_matrix, {"include": []})

        observe = build_promotion_workflow_plan(
            reconciliation,
            (subject,),
            epoch=self.promotion_epoch(activated=False),
            progress=progress,
            activation_mode="observe",
        )
        self.assertEqual(len(observe.activation_work), 1)
        self.assertEqual(observe.metadata_matrix, {"include": []})
        self.assertFalse(observe.authoritative)

        active = build_promotion_workflow_plan(
            reconciliation,
            (subject,),
            epoch=self.promotion_epoch(activated=False),
            progress=progress,
            activation_mode="active",
        )
        self.assertEqual(
            [item["operation"] for item in active.metadata_matrix["include"]],
            ["successor-activation"],
        )
        self.assertEqual(active.canonical_matrix, {"include": []})

        activated = build_promotion_workflow_plan(
            reconciliation,
            (subject,),
            epoch=self.promotion_epoch(activated=True),
            progress=progress,
            activation_mode="active",
        )
        self.assertEqual(activated.activation_work, ())
        self.assertEqual(
            [item["formula_subject"] for item in activated.canonical_matrix["include"]],
            [subject.decision.formula_subject],
        )

        with self.assertRaises(ReconciliationError):
            PromotionEpochV1(
                request_digest=reconciliation.request_digest,
                history_record_sha256=True,
                activation_patch_sha256="2" * 64,
                activation_record_sha256=None,
                current_tap_commit="4" * 40,
                current_tap_tree="5" * 40,
            )

    def test_open_pr_never_produces_promotion_work(self) -> None:
        request = discovered()
        open_reconciliation = reconcile_request(
            request,
            PullRequestLifecycleV1(
                "open", request.request["build_source"]["commit"], None
            ),
        )
        subject = PromotionSubjectV1(
            self.promotion_decision("alpha"), "required", ()
        )
        plan = build_promotion_workflow_plan(
            open_reconciliation,
            (subject,),
            epoch=self.promotion_epoch(activated=True),
            progress={subject.decision.formula_subject: PromotionProgressV1()},
            activation_mode="active",
        )
        self.assertEqual(plan.canonical_matrix, {"include": []})
        self.assertEqual(plan.metadata_matrix, {"include": []})
        self.assertEqual(plan.admission_matrix, {"include": []})
        self.assertEqual(
            [item["guard_code"] for item in plan.blocked],
            ["pull_request_not_merged"],
        )

    def test_promotion_plan_artifact_is_canonical_exact_and_stage_bound(self) -> None:
        reconciliation = self.merged_promotion_reconciliation()
        subject = PromotionSubjectV1(
            self.promotion_decision("alpha"), "required", ()
        )
        plan = build_promotion_workflow_plan(
            reconciliation,
            (subject,),
            epoch=self.promotion_epoch(activated=True),
            progress={subject.decision.formula_subject: PromotionProgressV1()},
            activation_mode="active",
        )
        work = plan.canonical_work[0]
        detail = {
            "decision": asdict(subject.decision),
            "candidate_locator": {
                "repository": "ghcr.io/kandelo-dev/fixture",
                "digest": "sha256:" + subject.decision.candidate_record_digest,
                "immutable_reference": (
                    "ghcr.io/kandelo-dev/fixture@sha256:"
                    + subject.decision.candidate_record_digest
                ),
            },
        }
        document = build_promotion_plan_document(
            plan,
            tap_source={
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": "4" * 40,
                "tree": "5" * 40,
            },
            work_details={
                "activation": {},
                "canonical": {work["work_id"]: detail},
                "metadata": {
                    plan.metadata_work[0]["work_id"]: {
                        "decision": asdict(subject.decision)
                    }
                },
                "admission": {
                    plan.admission_work[0]["work_id"]: {
                        "decision": asdict(subject.decision)
                    }
                },
            },
        )
        body = canonical_bytes(document)
        loaded = load_promotion_plan_document(body)
        self.assertEqual(canonical_bytes(loaded), body)
        selected = select_promotion_plan_work(
            loaded, stage="canonical", work_id=work["work_id"]
        )
        self.assertEqual(
            canonical_bytes(selected["detail"]), canonical_bytes(detail)
        )
        with self.assertRaises(ReconciliationError):
            select_promotion_plan_work(
                loaded, stage="metadata", work_id=work["work_id"]
            )

        changed = copy.deepcopy(document)
        changed["work"]["canonical"][0]["detail"]["candidate_locator"][
            "digest"
        ] = "sha256:" + "f" * 64
        with self.assertRaises(ReconciliationError):
            load_promotion_plan_document(canonical_bytes(changed))

        changed = copy.deepcopy(document)
        changed["matrices"]["canonical"]["include"][0]["work_id"] = "f" * 64
        with self.assertRaises(ReconciliationError):
            load_promotion_plan_document(canonical_bytes(changed))

        with self.assertRaises(ReconciliationError):
            build_promotion_plan_document(
                plan,
                tap_source={
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "commit": "4" * 40,
                    "tree": "5" * 40,
                },
                work_details={
                    "activation": {},
                    "canonical": {},
                    "metadata": {},
                    "admission": {},
                },
            )

    def test_batch_metadata_detail_carries_each_member_authority(self) -> None:
        reconciliation = self.merged_promotion_reconciliation()
        root = PromotionSubjectV1(
            self.promotion_decision("root", marker="a"), "required", ()
        )
        dependant = PromotionSubjectV1(
            self.promotion_decision("dependant", marker="f"),
            "required",
            (root.decision.formula_subject,),
        )
        planned = build_promotion_workflow_plan(
            reconciliation,
            (dependant, root),
            epoch=self.promotion_epoch(activated=True),
            progress={
                root.decision.formula_subject: PromotionProgressV1(),
                dependant.decision.formula_subject: PromotionProgressV1(),
            },
            activation_mode="active",
        )

        def context(subject: PromotionSubjectV1, digit: str) -> dict[str, object]:
            return {
                "subject": subject,
                "candidate_locator": {"digest": "sha256:" + digit * 64},
                "candidate_reuse_locator": None,
                "canonical": SimpleNamespace(
                    locator=PublishedRecordLocatorV1(
                        repository="ghcr.io/kandelo-dev/homebrew-tap-core-abi-43",
                        digest="sha256:" + digit * 64,
                        immutable_reference=(
                            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-43@sha256:"
                            + digit * 64
                        ),
                        anonymous_readback_sha256=digit * 64,
                    ),
                    artifact={"formula_subject": subject.decision.formula_subject},
                ),
                "history_locator": {"digest": "sha256:" + "1" * 64},
                "history_snapshot": {"ref": "abi/43"},
                "formula_metadata_base_source": {
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "commit": "2" * 40,
                    "tree": "3" * 40,
                },
                "metadata_patch": {
                    "operation": "formula-metadata",
                    "formula_update": {
                        "formula_subject": subject.decision.formula_subject
                    },
                },
            }

        contexts = {
            root.decision.formula_subject: context(root, "4"),
            dependant.decision.formula_subject: context(dependant, "5"),
        }
        details = cli_module._promotion_work_details(planned, contexts)
        batch = details["metadata"][planned.metadata_work[0]["work_id"]]

        self.assertEqual(
            [member["formula_subject"] for member in batch["members"]],
            planned.metadata_work[0]["member_formula_subjects"],
        )
        self.assertEqual(
            [member["canonical_work_id"] for member in batch["members"]],
            [item["work_id"] for item in planned.canonical_work],
        )
        self.assertEqual(
            {
                item["metadata_work_id"]
                for item in details["admission"].values()
            },
            {planned.metadata_work[0]["work_id"]},
        )

    def test_writer_loads_only_the_exact_current_run_promotion_plan(self) -> None:
        reconciliation = self.merged_promotion_reconciliation()
        subject = PromotionSubjectV1(
            self.promotion_decision("alpha"), "required", ()
        )
        plan = build_promotion_workflow_plan(
            reconciliation,
            (subject,),
            epoch=self.promotion_epoch(activated=True),
            progress={subject.decision.formula_subject: PromotionProgressV1()},
            activation_mode="active",
        )
        work = plan.canonical_work[0]
        detail = {"decision": asdict(subject.decision)}
        document = build_promotion_plan_document(
            plan,
            tap_source={
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": "4" * 40,
                "tree": "5" * 40,
            },
            work_details={
                "activation": {},
                "canonical": {work["work_id"]: detail},
                "metadata": {
                    plan.metadata_work[0]["work_id"]: {
                        "decision": asdict(subject.decision)
                    }
                },
                "admission": {
                    plan.admission_work[0]["work_id"]: {
                        "decision": asdict(subject.decision)
                    }
                },
            },
        )

        class ArtifactClient:
            def __init__(self) -> None:
                self.requested = None

            def artifact_by_id(self, **kwargs):
                self.requested = kwargs
                return object()

            def extract_artifact(self, _artifact, destination, **_kwargs):
                destination.mkdir(parents=True)
                (destination / "promotion-plan.json").write_bytes(
                    canonical_bytes(document)
                )

        with tempfile.TemporaryDirectory() as temporary:
            client = ArtifactClient()
            selected = cli_module._load_workflow_promotion_work(
                client,
                root=Path(temporary),
                run_id=91,
                run_attempt=2,
                request_digest=reconciliation.request_digest,
                artifact_id="17",
                artifact_digest="a" * 64,
                expected_tap_source={
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "commit": "4" * 40,
                    "tree": "5" * 40,
                },
                stage="canonical",
                work_id=work["work_id"],
            )
            self.assertEqual(
                canonical_bytes(selected["detail"]), canonical_bytes(detail)
            )
            self.assertEqual(
                client.requested["name"],
                "abi-staging-promotion-plan-"
                + reconciliation.request_digest
                + "-91-2",
            )

            with self.assertRaises(ReconciliationError):
                cli_module._load_workflow_promotion_work(
                    ArtifactClient(),
                    root=Path(temporary) / "second",
                    run_id=91,
                    run_attempt=2,
                    request_digest="f" * 64,
                    artifact_id="17",
                    artifact_digest="a" * 64,
                    expected_tap_source={
                        "repository": "kandelo-dev/homebrew-tap-core",
                        "commit": "4" * 40,
                        "tree": "5" * 40,
                    },
                    stage="canonical",
                    work_id=work["work_id"],
                )

    def test_metadata_readback_handoff_binds_exact_landed_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Kandelo").mkdir()
            (root / "Kandelo/abi-state.json").write_bytes(b"state\n")
            source = {
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": "a" * 40,
                "tree": "b" * 40,
            }
            result = SimpleNamespace(
                status="committed",
                source=source,
                changed_paths=("Kandelo/abi-state.json",),
            )
            patch = {
                "schema": 1,
                "kind": "kandelo-tap-metadata-patch",
                "operation": "successor-activation",
                "expected_main_commit": "c" * 40,
                "expected_main_tree": "d" * 40,
                "allowed_paths": ["Kandelo/abi-state.json"],
                "expected_files_sha256": {
                    "Kandelo/abi-state.json": "e" * 64
                },
                "files": [],
                "formula_update": None,
            }
            document = cli_module._metadata_readback_document(
                work_id="1" * 64,
                request_digest="f" * 64,
                result=result,
                patch_document=patch,
                formula_update=None,
                tap_root=root,
            )
            self.assertEqual(
                document["post_write_readback_sha256"],
                canonical_sha256(document["post_write_readback"]),
            )
            self.assertEqual(
                document["changed_files"],
                [
                    {
                        "path": "Kandelo/abi-state.json",
                        "sha256": hashlib.sha256(b"state\n").hexdigest(),
                        "bytes": 6,
                    }
                ],
            )

    def test_batch_metadata_readback_selects_one_exact_formula_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Kandelo").mkdir()
            (root / "Kandelo/metadata.json").write_bytes(b"batch\n")
            source = {
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": "a" * 40,
                "tree": "b" * 40,
            }
            result = SimpleNamespace(
                status="committed",
                source=source,
                changed_paths=("Kandelo/metadata.json",),
            )
            members = tuple(
                (
                    exact_formula_subject(formula, "wasm32"),
                    {"operation": "formula-metadata", "marker": marker},
                    FormulaMetadataUpdateV1(
                        formula=formula,
                        architecture="wasm32",
                        expected_main_commit="5" * 40,
                        expected_normalized_formula_sha256="6" * 64,
                        expected_generated_metadata_sha256="7" * 64,
                        allowed_paths=(f"Formula/{formula}.rb",),
                        link_manifest_path=(
                            f"Kandelo/link/{formula}-1.0-wasm32.json"
                        ),
                        link_manifest_sha256="8" * 64,
                        canonical_manifest_digest="9" * 64,
                        bottle_layer_sha256=marker * 64,
                        bottle_layer_bytes=4096,
                        target_abi=43,
                    ),
                )
                for formula, marker in (("dash", "2"), ("bash", "1"))
            )
            document = cli_module._metadata_batch_readback_document(
                work_id="3" * 64,
                request_digest="4" * 64,
                result=result,
                members=members,
                tap_root=root,
            )

            class ArtifactClient:
                def artifact_by_name(self, **_kwargs):
                    return object()

                def extract_artifact(self, _artifact, destination, **_kwargs):
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / "metadata-readback.json").write_bytes(
                        canonical_bytes(document)
                    )

            selected = cli_module._load_metadata_handoff(
                ArtifactClient(),
                root=root / "artifact",
                work_id="3" * 64,
                run_id=91,
                run_attempt=2,
                request_digest="4" * 64,
                formula_subject=exact_formula_subject("dash", "wasm32"),
            )

            self.assertEqual(selected["source"], source)
            self.assertEqual(selected["formula_metadata_update"]["formula"], "dash")
            self.assertEqual(
                selected["metadata_patch_sha256"],
                canonical_sha256(members[0][1]),
            )
            with self.assertRaises(cli_module.WorkflowPublicationError):
                cli_module._load_metadata_handoff(
                    ArtifactClient(),
                    root=root / "missing",
                    work_id="3" * 64,
                    run_id=91,
                    run_attempt=2,
                    request_digest="4" * 64,
                    formula_subject=exact_formula_subject("zlib", "wasm32"),
                )

    def test_batch_metadata_writer_parses_each_exact_member_patch(self) -> None:
        members = []
        for formula, marker in (("dash", "2"), ("bash", "1")):
            update = FormulaMetadataUpdateV1(
                formula=formula,
                architecture="wasm32",
                expected_main_commit="3" * 40,
                expected_normalized_formula_sha256="4" * 64,
                expected_generated_metadata_sha256="5" * 64,
                allowed_paths=(f"Formula/{formula}.rb",),
                link_manifest_path=f"Kandelo/link/{formula}-1.0-wasm32.json",
                link_manifest_sha256="6" * 64,
                canonical_manifest_digest="7" * 64,
                bottle_layer_sha256=marker * 64,
                bottle_layer_bytes=4096,
                target_abi=43,
            )
            patch_value = cli_module.metadata_patch_document(
                TapMetadataPatchV1(
                    operation="formula-metadata",
                    expected_main_commit="3" * 40,
                    expected_main_tree="8" * 40,
                    allowed_paths=update.allowed_paths,
                    expected_files_sha256={
                        f"Formula/{formula}.rb": "9" * 64
                    },
                    files={f"Formula/{formula}.rb": formula.encode("utf-8")},
                ),
                formula_update=update,
            )
            members.append(
                {
                    "formula_subject": exact_formula_subject(formula, "wasm32"),
                    "metadata_patch": patch_value,
                }
            )

        parsed = cli_module._load_metadata_batch_members({"members": members})

        self.assertEqual(
            [subject for subject, _patch_value, _patch, _update in parsed],
            [member["formula_subject"] for member in members],
        )
        duplicate = {"members": [members[0], members[0]]}
        with self.assertRaises(cli_module.WorkflowPublicationError):
            cli_module._load_metadata_batch_members(duplicate)

    def test_batch_metadata_writer_makes_one_atomic_commit(self) -> None:
        formula = "bash"
        subject = exact_formula_subject(formula, "wasm32")
        update = FormulaMetadataUpdateV1(
            formula=formula,
            architecture="wasm32",
            expected_main_commit="1" * 40,
            expected_normalized_formula_sha256="2" * 64,
            expected_generated_metadata_sha256="3" * 64,
            allowed_paths=(f"Formula/{formula}.rb",),
            link_manifest_path=f"Kandelo/link/{formula}-1.0-wasm32.json",
            link_manifest_sha256="4" * 64,
            canonical_manifest_digest="5" * 64,
            bottle_layer_sha256="6" * 64,
            bottle_layer_bytes=4096,
            target_abi=43,
        )
        member_patch = TapMetadataPatchV1(
            operation="formula-metadata",
            expected_main_commit="1" * 40,
            expected_main_tree="7" * 40,
            allowed_paths=update.allowed_paths,
            expected_files_sha256={f"Formula/{formula}.rb": "8" * 64},
            files={f"Formula/{formula}.rb": b"bash"},
        )
        patch_value = cli_module.metadata_patch_document(
            member_patch,
            formula_update=update,
        )
        detail = {
            "members": [
                {
                    "formula_subject": subject,
                    "metadata_patch": patch_value,
                }
            ]
        }
        batch_patch = SimpleNamespace(operation="formula-metadata-batch")
        batch = SimpleNamespace(patch=batch_patch, updates=(update,))
        source = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": "9" * 40,
            "tree": "a" * 40,
        }
        result = SimpleNamespace(
            status="committed",
            source=source,
            changed_paths=(),
        )
        args = SimpleNamespace(
            contents_only=True,
            normal_push=True,
            post_write_readback=True,
            require_history_barrier=True,
            operation="formula-metadata-batch",
            run_id=91,
            run_attempt=2,
            request_digest="b" * 64,
            work_id="c" * 64,
            plan_artifact_id="17",
            plan_artifact_digest="d" * 64,
        )
        with tempfile.TemporaryDirectory() as temporary:
            tap_root = Path(temporary)
            args.out = str(tap_root / "metadata-readback.json")
            policy = SimpleNamespace(
                tap_repository="kandelo-dev/homebrew-tap-core"
            )
            decision = SimpleNamespace(formula_subject=subject)
            expected = object()
            with (
                patch.object(
                    cli_module,
                    "_promotion_writer_authority",
                    return_value=(tap_root, policy, source, object()),
                ),
                patch.object(
                    cli_module,
                    "_load_workflow_promotion_work",
                    return_value={"detail": detail},
                ),
                patch.object(
                    cli_module,
                    "_promotion_candidate_and_canonical",
                    return_value=(decision, object(), None, expected),
                ),
                patch.object(
                    cli_module,
                    "read_canonical_publication",
                    return_value=expected,
                ),
                patch.object(cli_module, "_require_promotion_history_barrier"),
                patch.object(
                    cli_module,
                    "plan_formula_metadata_batch",
                    return_value=batch,
                ) as planner,
                patch.object(cli_module, "_configure_metadata_committer"),
                patch.object(
                    cli_module,
                    "apply_metadata_patch",
                    return_value=result,
                ) as apply,
                patch.object(cli_module, "check_tap_metadata"),
                patch.object(cli_module, "GitTapMetadataStore", return_value=object()),
            ):
                cli_module._update_workflow_tap_metadata(args)

            planner.assert_called_once()
            apply.assert_called_once()
            self.assertIs(apply.call_args.kwargs["formula_batch"], batch)
            readback = json.loads(Path(args.out).read_bytes())
            self.assertEqual(readback["operation"], "formula-metadata-batch")
            self.assertEqual(
                [member["formula_subject"] for member in readback["members"]],
                [subject],
            )

    def test_disabled_workflow_planner_emits_exact_empty_artifact(self) -> None:
        selected = discovered()
        tap_source = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": "4" * 40,
            "tree": "5" * 40,
        }
        bundle = {
            "request": selected.request,
            "request_sha256": selected.request_digest,
            "request_asset_url": selected.asset_url,
            "tap_plan": {"tap_source": tap_source},
        }
        client = unittest.mock.Mock()
        client.pull_request_lifecycle.return_value = PullRequestLifecycleV1(
            "merged", selected.request["build_source"]["commit"], "8" * 40
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordination = root / "coordination"
            coordination.mkdir()
            output = root / "promotion-plan"
            github_output = root / "github-output"
            github_output.write_text("", encoding="utf-8")
            with (
                patch(
                    "scripts.abi_staging.cli.load_coordination_bundle",
                    return_value=bundle,
                ),
                patch(
                    "scripts.abi_staging.cli.snapshot_tap_source",
                    return_value=tap_source,
                ),
                patch(
                    "scripts.abi_staging.cli.load_promotion_activation",
                    return_value=SimpleNamespace(mode="disabled"),
                ),
                patch(
                    "scripts.abi_staging.cli.GitHubPublicClient",
                    return_value=client,
                ),
            ):
                status = cli_main(
                    [
                        "plan-workflow-promotion",
                        "--coordination-root",
                        str(coordination),
                        "--tap-root",
                        str(TAP_ROOT),
                        "--require-merged",
                        "--require-history-record",
                        "--out",
                        str(output),
                        "--github-output",
                        str(github_output),
                    ]
                )

            self.assertEqual(status, 0)
            plan = load_promotion_plan_document(
                (output / "promotion-plan.json").read_bytes()
            )
            self.assertEqual(plan["mode"], "disabled")
            self.assertEqual(
                plan["matrices"],
                {
                    "canonical": {"include": []},
                    "metadata": {"include": []},
                    "admission": {"include": []},
                },
            )
            self.assertIn("canonical_matrix={\"include\":[]}", github_output.read_text())

    def test_observe_workflow_planner_keeps_work_non_authoritative(self) -> None:
        selected = discovered()
        tap_source = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": "4" * 40,
            "tree": "5" * 40,
        }
        bundle = {
            "request": selected.request,
            "request_sha256": selected.request_digest,
            "request_asset_url": selected.asset_url,
            "tap_plan": {"tap_source": tap_source},
        }
        client = unittest.mock.Mock()
        client.pull_request_lifecycle.return_value = PullRequestLifecycleV1(
            "merged", selected.request["build_source"]["commit"], "8" * 40
        )
        subject = PromotionSubjectV1(
            self.promotion_decision("alpha"), "required", ()
        )
        planned = build_promotion_workflow_plan(
            self.merged_promotion_reconciliation(),
            (subject,),
            epoch=self.promotion_epoch(activated=True),
            progress={subject.decision.formula_subject: PromotionProgressV1()},
            activation_mode="observe",
        )
        details = {
            "activation": {},
            "canonical": {
                planned.canonical_work[0]["work_id"]: {
                    "decision": asdict(subject.decision)
                }
            },
            "metadata": {
                planned.metadata_work[0]["work_id"]: {
                    "decision": asdict(subject.decision)
                }
            },
            "admission": {
                planned.admission_work[0]["work_id"]: {
                    "decision": asdict(subject.decision)
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordination = root / "coordination"
            coordination.mkdir()
            output = root / "promotion-plan"
            github_output = root / "github-output"
            github_output.write_text("", encoding="utf-8")
            with (
                patch(
                    "scripts.abi_staging.cli.load_coordination_bundle",
                    return_value=bundle,
                ),
                patch(
                    "scripts.abi_staging.cli.snapshot_tap_source",
                    return_value=tap_source,
                ),
                patch(
                    "scripts.abi_staging.cli.load_promotion_activation",
                    return_value=SimpleNamespace(mode="observe"),
                ),
                patch(
                    "scripts.abi_staging.cli.GitHubPublicClient",
                    return_value=client,
                ),
                patch(
                    "scripts.abi_staging.cli._collect_active_promotion_inputs",
                    return_value=(planned, details),
                    create=True,
                ) as collector,
            ):
                status = cli_main(
                    [
                        "plan-workflow-promotion",
                        "--coordination-root",
                        str(coordination),
                        "--tap-root",
                        str(TAP_ROOT),
                        "--require-merged",
                        "--require-history-record",
                        "--out",
                        str(output),
                        "--github-output",
                        str(github_output),
                    ]
                )

            self.assertEqual(status, 0)
            collector.assert_called_once()
            plan = load_promotion_plan_document(
                (output / "promotion-plan.json").read_bytes()
            )
            self.assertEqual(len(plan["work"]["canonical"]), 1)
            self.assertEqual(plan["matrices"]["canonical"], {"include": []})
            self.assertFalse(plan["authoritative"])

    def test_verified_dependant_does_not_wait_for_dependency_admission(self) -> None:
        reconciliation = self.merged_promotion_reconciliation()
        root = PromotionSubjectV1(
            self.promotion_decision("root", marker="a"), "required", ()
        )
        dependant = PromotionSubjectV1(
            self.promotion_decision("dependant", marker="f"),
            "required",
            (root.decision.formula_subject,),
        )
        plan = build_promotion_workflow_plan(
            reconciliation,
            (dependant, root),
            epoch=self.promotion_epoch(activated=True),
            progress={
                root.decision.formula_subject: PromotionProgressV1(),
                dependant.decision.formula_subject: PromotionProgressV1(),
            },
            activation_mode="active",
        )

        self.assertEqual(
            [item["formula_subject"] for item in plan.canonical_work],
            [dependant.decision.formula_subject, root.decision.formula_subject],
        )
        self.assertEqual(len(plan.metadata_work), 1)
        batch = plan.metadata_work[0]
        self.assertEqual(batch["operation"], "formula-metadata-batch")
        self.assertEqual(
            batch["member_formula_subjects"],
            [dependant.decision.formula_subject, root.decision.formula_subject],
        )
        self.assertEqual(
            [item["formula_subject"] for item in plan.admission_work],
            [dependant.decision.formula_subject, root.decision.formula_subject],
        )
        self.assertEqual(
            {item["metadata_work_id"] for item in plan.admission_work},
            {batch["work_id"]},
        )

    def test_complete_required_graph_fits_one_promotion_wave(self) -> None:
        reconciliation = self.merged_promotion_reconciliation()
        subjects = tuple(
            PromotionSubjectV1(
                self.promotion_decision(
                    f"formula-{index:02d}", marker=f"formula-{index:02d}"
                ),
                "required",
                (),
            )
            for index in range(43)
        )
        plan = build_promotion_workflow_plan(
            reconciliation,
            subjects,
            epoch=self.promotion_epoch(activated=True),
            progress={
                subject.decision.formula_subject: PromotionProgressV1()
                for subject in subjects
            },
            activation_mode="active",
        )

        self.assertEqual(len(plan.canonical_work), 43)
        self.assertNotIn(
            "promotion_wave_deferred",
            {item["guard_code"] for item in plan.blocked},
        )

    def test_missing_generated_metadata_is_formula_local_for_background(self) -> None:
        with patch.object(
            cli_module,
            "formula_generated_metadata_sha256",
            side_effect=cli_module.TapMetadataError("missing generated row"),
        ):
            self.assertIsNone(
                cli_module._formula_generated_metadata_identity(
                    Path("/tmp/tap"),
                    formula="nginx",
                    work_class="background",
                )
            )
            with self.assertRaises(ReconciliationError):
                cli_module._formula_generated_metadata_identity(
                    Path("/tmp/tap"),
                    formula="bash",
                    work_class="required",
                )

    def test_promotion_converges_independently_and_retries_exact_stage(self) -> None:
        reconciliation = self.merged_promotion_reconciliation()
        root = PromotionSubjectV1(
            self.promotion_decision("root", marker="a"), "required", ()
        )
        background = PromotionSubjectV1(
            self.promotion_decision("background", marker="k"), "background", ()
        )
        drifted = PromotionSubjectV1(
            self.promotion_decision(
                "drifted",
                marker="p",
                eligibility="rebuild-required",
                tap_source_state="rebuild-required",
            ),
            "background",
            (),
        )
        subjects = (background, drifted, root)
        empty = {
            item.decision.formula_subject: PromotionProgressV1()
            for item in subjects
        }

        first = build_promotion_workflow_plan(
            reconciliation,
            subjects,
            epoch=self.promotion_epoch(activated=True),
            progress=empty,
            activation_mode="active",
        )
        self.assertEqual(
            [item["formula_subject"] for item in first.canonical_work],
            [root.decision.formula_subject, background.decision.formula_subject],
        )
        self.assertEqual(len(first.metadata_work), 1)
        first_batch = first.metadata_work[0]
        self.assertEqual(first_batch["operation"], "formula-metadata-batch")
        self.assertEqual(
            first_batch["member_formula_subjects"],
            [root.decision.formula_subject, background.decision.formula_subject],
        )
        self.assertEqual(
            [item["formula_subject"] for item in first.admission_work],
            [root.decision.formula_subject, background.decision.formula_subject],
        )
        self.assertEqual(
            {item["metadata_work_id"] for item in first.admission_work},
            {first_batch["work_id"]},
        )
        self.assertEqual(
            [item["canonical_work_id"] for item in first.admission_work],
            [item["work_id"] for item in first.canonical_work],
        )
        self.assertEqual(
            [(item["formula_subject"], item["guard_code"]) for item in first.blocked],
            [(drifted.decision.formula_subject, "tap_source_drift")],
        )
        self.assertEqual(
            first,
            build_promotion_workflow_plan(
                reconciliation,
                subjects,
                epoch=self.promotion_epoch(activated=True),
                progress=empty,
                activation_mode="active",
            ),
        )

        canonical_progress = dict(empty)
        canonical_progress[root.decision.formula_subject] = PromotionProgressV1(
            canonical_manifest_sha256="6" * 64,
            canonical_readback_sha256="7" * 64,
        )
        canonical_progress[background.decision.formula_subject] = (
            PromotionProgressV1(
                canonical_manifest_sha256="8" * 64,
                canonical_readback_sha256="9" * 64,
            )
        )
        metadata = build_promotion_workflow_plan(
            reconciliation,
            subjects,
            epoch=self.promotion_epoch(activated=True),
            progress=canonical_progress,
            activation_mode="active",
        )
        self.assertEqual(
            metadata.metadata_work[0]["member_formula_subjects"],
            [root.decision.formula_subject, background.decision.formula_subject],
        )
        stale_admission = build_promotion_workflow_plan(
            reconciliation,
            (root,),
            epoch=self.promotion_epoch(activated=True),
            progress={
                root.decision.formula_subject: PromotionProgressV1(
                    canonical_manifest_sha256="6" * 64,
                    canonical_readback_sha256="7" * 64,
                    stale_admission_record_sha256="e" * 64,
                )
            },
            activation_mode="active",
        )
        self.assertEqual(
            stale_admission.metadata_work[0]["member_formula_subjects"],
            [root.decision.formula_subject],
        )
        self.assertEqual(stale_admission.admission_work, ())
        retry = build_promotion_workflow_plan(
            reconciliation,
            subjects,
            epoch=PromotionEpochV1(
                request_digest=reconciliation.request_digest,
                history_record_sha256="1" * 64,
                activation_patch_sha256="2" * 64,
                activation_record_sha256="3" * 64,
                current_tap_commit="a" * 40,
                current_tap_tree="b" * 40,
            ),
            progress=canonical_progress,
            activation_mode="active",
        )
        self.assertNotEqual(
            metadata.metadata_work[0]["work_id"], retry.metadata_work[0]["work_id"]
        )

        landed = dict(canonical_progress)
        landed[root.decision.formula_subject] = PromotionProgressV1(
            canonical_manifest_sha256="6" * 64,
            canonical_readback_sha256="7" * 64,
            metadata_commit="a" * 40,
            metadata_tree="b" * 40,
            metadata_update_sha256="c" * 64,
            metadata_readback_sha256="d" * 64,
        )
        admission = build_promotion_workflow_plan(
            reconciliation,
            subjects,
            epoch=self.promotion_epoch(activated=True),
            progress=landed,
            activation_mode="active",
        )
        self.assertEqual(
            [item["formula_subject"] for item in admission.admission_work],
            [root.decision.formula_subject, background.decision.formula_subject],
        )
        self.assertEqual(
            admission.metadata_work[0]["member_formula_subjects"],
            [background.decision.formula_subject],
        )
        self.assertEqual(
            admission.admission_work,
            build_promotion_workflow_plan(
                reconciliation,
                subjects,
                epoch=self.promotion_epoch(activated=True),
                progress=landed,
                activation_mode="active",
            ).admission_work,
        )

        complete = dict(landed)
        complete[root.decision.formula_subject] = PromotionProgressV1(
            canonical_manifest_sha256="6" * 64,
            canonical_readback_sha256="7" * 64,
            metadata_commit="a" * 40,
            metadata_tree="b" * 40,
            metadata_update_sha256="c" * 64,
            metadata_readback_sha256="d" * 64,
            admission_record_sha256="e" * 64,
        )
        resumed = build_promotion_workflow_plan(
            reconciliation,
            subjects,
            epoch=self.promotion_epoch(activated=True),
            progress=complete,
            activation_mode="active",
        )
        self.assertIn(root.decision.formula_subject, resumed.complete)

    def test_cli_routes_post_formula_product_wave_planning(self) -> None:
        from scripts.abi_staging import cli

        with patch.object(
            cli, "_plan_workflow_products", return_value=None, create=True
        ) as planner:
            status = cli_main(
                [
                    "plan-workflow-products",
                    "--coordination-root",
                    "/tmp/coordination",
                    "--runtime-root",
                    "/tmp/runtime",
                    "--kandelo-root",
                    "/tmp/kandelo",
                    "--tap-root",
                    "/tmp/tap",
                    "--github-output",
                    "/tmp/github-output",
                    "--out",
                    "/tmp/product-wave",
                ]
            )

        self.assertEqual(status, 0)
        planner.assert_called_once()
        args = planner.call_args.args[0]
        self.assertEqual(args.coordination_root, "/tmp/coordination")
        self.assertEqual(args.runtime_root, "/tmp/runtime")

    def test_cli_accepts_formula_metadata_batch_writer_operation(self) -> None:
        with patch.object(
            cli_module,
            "_update_workflow_tap_metadata",
            return_value=None,
        ) as writer:
            status = cli_main(
                [
                    "update-workflow-tap-metadata",
                    "--run-id",
                    "91",
                    "--run-attempt",
                    "2",
                    "--head-sha",
                    "a" * 40,
                    "--request-digest",
                    "b" * 64,
                    "--work-id",
                    "c" * 64,
                    "--operation",
                    "formula-metadata-batch",
                    "--plan-artifact-id",
                    "17",
                    "--plan-artifact-digest",
                    "d" * 64,
                    "--contents-only",
                    "--normal-push",
                    "--post-write-readback",
                    "--require-history-barrier",
                    "--out",
                    "/tmp/metadata-readback.json",
                ]
            )

        self.assertEqual(status, 0)
        writer.assert_called_once()
        self.assertEqual(writer.call_args.args[0].operation, "formula-metadata-batch")

    def test_product_workflow_wave_schedules_only_ready_whole_products(self) -> None:
        selected = discovered()
        decision = reconcile_request(
            selected,
            PullRequestLifecycleV1(
                "open", selected.request["build_source"]["commit"], None
            ),
        )
        request = {
            "requirements": {
                "products": [
                    {
                        "id": "base",
                        "path": "images/vfs/products/base.toml",
                        "manifest_sha256": "a" * 64,
                    },
                    {
                        "id": "dependent",
                        "path": "images/vfs/products/dependent.toml",
                        "manifest_sha256": "b" * 64,
                    },
                    {
                        "id": "informational",
                        "path": "images/vfs/products/informational.toml",
                        "manifest_sha256": "c" * 64,
                    },
                ],
                "evidence": [
                    {
                        "product_id": "base",
                        "applicability": "required",
                        "node": ["base-node"],
                        "browser": ["base-browser"],
                    },
                    {
                        "product_id": "dependent",
                        "applicability": "required",
                        "node": ["dependent-node"],
                        "browser": [],
                    },
                    {
                        "product_id": "informational",
                        "applicability": "informational",
                        "node": ["informational-node"],
                        "browser": [],
                    },
                ],
            }
        }
        decision = decision.__class__(
            request_digest=canonical_sha256(request),
            claim_key="sha256:" + canonical_sha256(request),
            lifecycle=decision.lifecycle,
            current_for_pull_request=decision.current_for_pull_request,
            action=decision.action,
            permitted_work=decision.permitted_work,
            blockers=decision.blockers,
        )
        selections = (
            ProductSelectionV1(
                "base", "a" * 64, "required", (), ("base-node",), ("base-browser",)
            ),
            ProductSelectionV1(
                "dependent",
                "b" * 64,
                "required",
                ("base",),
                ("dependent-node",),
                (),
            ),
            ProductSelectionV1(
                "informational",
                "c" * 64,
                "informational",
                (),
                ("informational-node",),
                (),
            ),
        )
        runtime = "d" * 64

        wave = build_product_workflow_wave(
            request,
            decision,
            selections,
            runtime_bundle_sha256=runtime,
            progress={
                "base": ProductProgressV1(True, None, (), None),
                "dependent": ProductProgressV1(True, None, (), None),
                "informational": ProductProgressV1(True, None, (), None),
            },
            activation_mode="observe",
        )

        self.assertEqual(
            [item["product_id"] for item in wave["product_matrix"]["include"]],
            ["base", "informational"],
        )
        self.assertEqual(
            [item["product_id"] for item in wave["node_matrix"]["include"]],
            ["base", "informational"],
        )
        self.assertEqual(
            [item["product_id"] for item in wave["browser_matrix"]["include"]],
            ["base"],
        )
        self.assertEqual(
            [item["product_id"] for item in wave["blocked"]], ["dependent"]
        )

        partial_current_run = build_product_workflow_wave(
            request,
            decision,
            selections,
            runtime_bundle_sha256=runtime,
            progress={
                "base": ProductProgressV1(
                    True,
                    runtime,
                    (("node", "base-node", "failure"),),
                    None,
                ),
                "dependent": ProductProgressV1(False, None, (), None),
                "informational": ProductProgressV1(True, runtime, (), "f" * 64),
            },
            activation_mode="observe",
        )
        self.assertEqual(
            [item["product_id"] for item in partial_current_run["product_matrix"]["include"]],
            ["base"],
        )
        self.assertEqual(
            [item["definition_id"] for item in partial_current_run["node_matrix"]["include"]],
            ["base-node"],
        )
        self.assertEqual(
            [item["definition_id"] for item in partial_current_run["browser_matrix"]["include"]],
            ["base-browser"],
        )

        resumed = build_product_workflow_wave(
            request,
            decision,
            selections,
            runtime_bundle_sha256=runtime,
            progress={
                "base": ProductProgressV1(True, runtime, (), "e" * 64),
                "dependent": ProductProgressV1(True, None, (), None),
                "informational": ProductProgressV1(True, runtime, (), "f" * 64),
            },
            activation_mode="observe",
        )
        self.assertEqual(
            [item["product_id"] for item in resumed["product_matrix"]["include"]],
            ["dependent"],
        )
        self.assertEqual(resumed["complete"], ["base", "informational"])

    def test_product_seed_binds_a_distinct_evidence_publication_work_id(self) -> None:
        selected = discovered()
        decision = reconcile_request(
            selected,
            PullRequestLifecycleV1(
                "open", selected.request["build_source"]["commit"], None
            ),
        )

        seed = build_product_workflow_seed(
            selected.request, decision, activation_mode="observe"
        )

        for work in seed["product_work"]:
            binding = next(
                item
                for item in selected.request["requirements"]["evidence"]
                if item["product_id"] == work["product_id"]
            )
            product = next(
                item
                for item in selected.request["requirements"]["products"]
                if item["id"] == work["product_id"]
            )
            base = {
                "applicability": binding["applicability"],
                "manifest_sha256": product["manifest_sha256"],
                "product_id": product["id"],
                "request_digest": decision.request_digest,
            }
            self.assertEqual(
                work["publication_work_id"],
                canonical_sha256(
                    {**base, "stage": "publish-product-evidence"}
                ),
            )
            self.assertNotEqual(work["publication_work_id"], work["work_id"])

    def test_open_advance_close_reopen_and_merge_table(self) -> None:
        request = discovered()
        head = request.request["build_source"]["commit"]
        other = "9" * 40
        cases = [
            (PullRequestLifecycleV1("open", head, None), None, True, "observe-open"),
            (PullRequestLifecycleV1("open", other, None), None, False, "observe-historical"),
            (PullRequestLifecycleV1("closed", head, None), None, True, "stop-new-work"),
            (
                PullRequestLifecycleV1("open", head, None),
                PullRequestLifecycleV1("closed", head, None),
                True,
                "resume-same-head",
            ),
            (
                PullRequestLifecycleV1("open", other, None),
                PullRequestLifecycleV1("closed", head, None),
                False,
                "observe-historical",
            ),
            (PullRequestLifecycleV1("merged", head, "8" * 40), None, True, "observe-merged"),
            (
                PullRequestLifecycleV1("merged", other, "8" * 40),
                None,
                False,
                "observe-historical",
            ),
        ]
        for lifecycle, previous, current, action in cases:
            with self.subTest(lifecycle=lifecycle, previous=previous):
                decision = reconcile_request(request, lifecycle, previous_lifecycle=previous)
                self.assertEqual(decision.claim_key, f"sha256:{request.request_digest}")
                self.assertEqual(decision.current_for_pull_request, current)
                self.assertEqual(decision.action, action)
                self.assertEqual(decision.permitted_work, ())

    def test_historical_exact_heads_remain_valid_without_commit_ordering(self) -> None:
        old = discovered("historical-request.json")
        current = discovered()
        lifecycle = PullRequestLifecycleV1(
            "open", current.request["build_source"]["commit"], None
        )
        decisions = [reconcile_request(item, lifecycle) for item in [old, current]]
        self.assertEqual(
            [decision.action for decision in decisions],
            ["observe-historical", "observe-open"],
        )
        self.assertNotEqual(old.request_digest, current.request_digest)

    def test_historical_and_merged_requests_remain_buildable_but_closed_stops_new_work(self) -> None:
        from scripts.abi_staging.reconcile import reconciliation_work_scope

        request = discovered()
        historical = reconcile_request(
            request, PullRequestLifecycleV1("open", "9" * 40, None)
        )
        merged = reconcile_request(
            request,
            PullRequestLifecycleV1(
                "merged", request.request["build_source"]["commit"], "8" * 40
            ),
        )
        closed = reconcile_request(
            request,
            PullRequestLifecycleV1(
                "closed", request.request["build_source"]["commit"], None
            ),
        )
        self.assertEqual(
            reconciliation_work_scope(historical),
            reconciliation_work_scope(merged),
        )
        self.assertTrue(reconciliation_work_scope(historical).allow_required)
        self.assertTrue(reconciliation_work_scope(historical).allow_background)
        self.assertFalse(reconciliation_work_scope(closed).allow_required)

    def test_invalid_lifecycle_and_duplicate_requests_are_rejected(self) -> None:
        request = discovered()
        with self.assertRaises(ReconciliationError):
            reconcile_request(request, PullRequestLifecycleV1("merged", None, None))
        with self.assertRaises(ReconciliationError):
            reconcile_request(request, PullRequestLifecycleV1("open", "A" * 40, None))

    def test_cycle_selection_is_bounded_fair_and_independent_of_discovery_order(self) -> None:
        old = discovered("historical-request.json")
        current = discovered()
        lifecycle = PullRequestLifecycleV1(
            "open", current.request["build_source"]["commit"], None
        )
        pairs = tuple(
            (item, reconcile_request(item, lifecycle)) for item in (old, current)
        )
        first = select_reconciliation_cycle(pairs, cycle_index=0)
        second = select_reconciliation_cycle(tuple(reversed(pairs)), cycle_index=1)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first[0].request_digest, second[0].request_digest)
        self.assertEqual(
            select_reconciliation_cycle(pairs, cycle_index=2)[0].request_digest,
            first[0].request_digest,
        )
        closed = tuple(
            (
                item,
                reconcile_request(
                    item,
                    PullRequestLifecycleV1(
                        "closed", current.request["build_source"]["commit"], None
                    ),
                ),
            )
            for item in (old, current)
        )
        self.assertIsNone(select_reconciliation_cycle(closed, cycle_index=0))

    def test_checked_in_activation_is_active_after_observe_canary(self) -> None:
        activation = load_reconciliation_activation(
            TAP_ROOT / "Kandelo/staging/reconciliation-activation.toml"
        )
        self.assertEqual(activation, "active")

    def test_activation_accepts_only_explicit_observe_or_active_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "activation.toml"
            path.write_text(
                'schema = 1\n'
                'kind = "kandelo-abi-staging-reconciliation-activation"\n'
                'mode = "active"\n',
                encoding="utf-8",
            )
            self.assertEqual(load_reconciliation_activation(path), "active")
            path.write_text(
                'schema = 1\n'
                'kind = "kandelo-abi-staging-reconciliation-activation"\n'
                'mode = "paused"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ReconciliationError):
                load_reconciliation_activation(path)

    def test_product_work_is_required_first_dependency_scoped_and_idempotent(self) -> None:
        request = discovered()
        decision = reconcile_request(
            request,
            PullRequestLifecycleV1(
                "open", request.request["build_source"]["commit"], None
            ),
        )
        selections = (
            ProductSelectionV1(
                "base",
                "a" * 64,
                "required",
                (),
                ("base-node",),
                ("base-browser",),
            ),
            ProductSelectionV1(
                "dependent",
                "b" * 64,
                "required",
                ("base",),
                ("dependent-node",),
                (),
            ),
            ProductSelectionV1(
                "informational",
                "c" * 64,
                "informational",
                (),
                ("informational-node",),
                (),
            ),
        )
        runtime = "d" * 64
        first = plan_product_reconciliation(
            decision,
            selections,
            runtime_bundle_sha256=runtime,
            progress={
                "base": ProductProgressV1(True, None, (), None),
                "dependent": ProductProgressV1(True, None, (), None),
                "informational": ProductProgressV1(True, None, (), None),
            },
            activation_mode="observe",
        )
        self.assertEqual(
            [item["product_id"] for item in first.composition_work],
            ["base", "informational"],
        )
        self.assertEqual(
            [(item["product_id"], item["guard_code"]) for item in first.blocked],
            [("dependent", "dependency_unavailable")],
        )
        self.assertFalse(first.authoritative)
        self.assertEqual(
            first,
            plan_product_reconciliation(
                decision,
                selections,
                runtime_bundle_sha256=runtime,
                progress={
                    "base": ProductProgressV1(True, None, (), None),
                    "dependent": ProductProgressV1(True, None, (), None),
                    "informational": ProductProgressV1(True, None, (), None),
                },
                activation_mode="observe",
            ),
        )

    def test_product_stages_resume_independently_and_runtime_change_recomposes(self) -> None:
        request = discovered()
        head = request.request["build_source"]["commit"]
        decision = reconcile_request(
            request, PullRequestLifecycleV1("open", head, None)
        )
        selections = (
            ProductSelectionV1(
                "alpha", "a" * 64, "required", (), ("alpha-node",), ()
            ),
            ProductSelectionV1(
                "beta", "b" * 64, "required", (), ("beta-node",), ()
            ),
        )
        runtime = "d" * 64
        plan = plan_product_reconciliation(
            decision,
            selections,
            runtime_bundle_sha256=runtime,
            progress={
                "alpha": ProductProgressV1(
                    True,
                    runtime,
                    (("node", "alpha-node", "failure"),),
                    None,
                ),
                "beta": ProductProgressV1(True, runtime, (), None),
            },
            activation_mode="active",
        )
        self.assertEqual(
            [item["product_id"] for item in plan.evidence_publication_work],
            ["alpha"],
        )
        self.assertEqual(
            [(item["product_id"], item["definition_id"]) for item in plan.node_work],
            [("beta", "beta-node")],
        )
        self.assertTrue(plan.authoritative)

        changed = plan_product_reconciliation(
            decision,
            selections,
            runtime_bundle_sha256="e" * 64,
            progress={
                "alpha": ProductProgressV1(True, runtime, (), "f" * 64),
                "beta": ProductProgressV1(True, runtime, (), "1" * 64),
            },
            activation_mode="active",
        )
        self.assertEqual(
            [item["product_id"] for item in changed.composition_work],
            ["alpha", "beta"],
        )
        self.assertEqual(changed.complete, ())

    def test_product_formula_blockers_closed_and_reopened_lifecycle_are_local(self) -> None:
        request = discovered()
        head = request.request["build_source"]["commit"]
        selection = ProductSelectionV1(
            "alpha", "a" * 64, "required", (), ("alpha-node",), ()
        )
        closed = reconcile_request(
            request, PullRequestLifecycleV1("closed", head, None)
        )
        stopped = plan_product_reconciliation(
            closed,
            (selection,),
            runtime_bundle_sha256="d" * 64,
            progress={"alpha": ProductProgressV1(True, None, (), None)},
            activation_mode="active",
        )
        self.assertFalse(stopped.prepare_runtime)
        self.assertEqual(stopped.composition_work, ())

        reopened = reconcile_request(
            request,
            PullRequestLifecycleV1("open", head, None),
            previous_lifecycle=PullRequestLifecycleV1("closed", head, None),
        )
        blocked = plan_product_reconciliation(
            reopened,
            (selection,),
            runtime_bundle_sha256="d" * 64,
            progress={"alpha": ProductProgressV1(False, None, (), None)},
            activation_mode="active",
        )
        self.assertEqual(blocked.composition_work, ())
        self.assertEqual(blocked.blocked[0]["guard_code"], "dependency_unavailable")


if __name__ == "__main__":
    unittest.main()
