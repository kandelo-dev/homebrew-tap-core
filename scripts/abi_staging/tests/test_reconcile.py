from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
sys.path.insert(0, str(TAP_ROOT))

from scripts.abi_staging.github_public import DiscoveredRequestV1
from scripts.abi_staging.reconcile import (
    PullRequestLifecycleV1,
    ReconciliationError,
    load_reconciliation_activation,
    reconcile_request,
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
            (PullRequestLifecycleV1("open", other, None), None, False, "await-new-request"),
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
                "await-new-request",
            ),
            (PullRequestLifecycleV1("merged", head, "8" * 40), None, True, "observe-merged"),
            (PullRequestLifecycleV1("merged", other, "8" * 40), None, False, "stop-new-work"),
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
            ["await-new-request", "observe-open"],
        )
        self.assertNotEqual(old.request_digest, current.request_digest)

    def test_invalid_lifecycle_and_duplicate_requests_are_rejected(self) -> None:
        request = discovered()
        with self.assertRaises(ReconciliationError):
            reconcile_request(request, PullRequestLifecycleV1("merged", None, None))
        with self.assertRaises(ReconciliationError):
            reconcile_request(request, PullRequestLifecycleV1("open", "A" * 40, None))

    def test_activation_is_strict_observe_only(self) -> None:
        activation = load_reconciliation_activation(
            TAP_ROOT / "Kandelo/staging/reconciliation-activation.toml"
        )
        self.assertEqual(activation, "observe")


if __name__ == "__main__":
    unittest.main()
