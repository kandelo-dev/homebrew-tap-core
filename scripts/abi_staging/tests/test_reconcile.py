from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
sys.path.insert(0, str(TAP_ROOT))

from scripts.abi_staging.github_public import DiscoveredRequestV1
from scripts.abi_staging.reconcile import (
    PullRequestLifecycleV1,
    ReconciliationError,
    load_reconciliation_activation,
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


if __name__ == "__main__":
    unittest.main()
