from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
sys.path.insert(0, str(TAP_ROOT))

from scripts.abi_staging.github_public import DiscoveredRequestV1
from scripts.abi_staging.canonical import canonical_sha256
from scripts.abi_staging.cli import main as cli_main
from scripts.abi_staging.reconcile import (
    build_product_workflow_wave,
    build_product_workflow_seed,
    ProductProgressV1,
    ProductSelectionV1,
    PullRequestLifecycleV1,
    ReconciliationError,
    load_reconciliation_activation,
    plan_product_reconciliation,
    reconcile_request,
    select_reconciliation_cycle,
)
from scripts.abi_staging.request import load_request_issuer_policy, validate_request


FIXTURES = TAP_ROOT / "Kandelo/staging/fixtures/request"
POLICY = load_request_issuer_policy(
    TAP_ROOT / "Kandelo/staging/request-issuers.toml",
    expected_tap="kandelo-dev/homebrew-tap-core",
)


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

    def test_activation_is_strict_observe_only(self) -> None:
        activation = load_reconciliation_activation(
            TAP_ROOT / "Kandelo/staging/reconciliation-activation.toml"
        )
        self.assertEqual(activation, "observe")

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
