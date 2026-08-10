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

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.check_projection import (
    build_check_projection_input,
    collect_check_projection_input,
    inventory_claims_request,
    public_record_envelope,
    scheduling_blockers,
)
from scripts.abi_staging.github_public import DiscoveredRequestV1
from scripts.abi_staging.inventory import PublicSchedulingInventoryV1
from scripts.abi_staging.request import load_request_issuer_policy, validate_request
from scripts.abi_staging.scheduler import (
    AttemptFactV1,
    BlockedSubjectV1,
    SchedulingDecisionV1,
    SchedulingRecordsV1,
)


FIXTURES = TAP_ROOT / "Kandelo/staging/fixtures"
ISSUER_POLICY = load_request_issuer_policy(
    TAP_ROOT / "Kandelo/staging/request-issuers.toml",
    expected_tap="kandelo-dev/homebrew-tap-core",
)


def request_fixture(name: str = "current-request.json") -> tuple[dict, str, str]:
    body = (FIXTURES / "request" / name).read_bytes()
    raw = json.loads(body)
    digest = hashlib.sha256(body).hexdigest()
    head = raw["build_source"]["commit"]
    asset = f"candidate-request-{head}-sha256-{digest}.json"
    request = dict(validate_request(body, asset, ISSUER_POLICY))
    url = (
        "https://github.com/Automattic/kandelo/releases/download/"
        f"abi-staging-pr-19/{asset}"
    )
    return request, digest, url


def discovered(
    name: str = "current-request.json",
    *,
    created_at: str = "2026-08-09T10:00:00Z",
) -> DiscoveredRequestV1:
    request, digest, url = request_fixture(name)
    return DiscoveredRequestV1(
        digest,
        Path(url).name,
        url,
        "abi-staging-pr-19",
        request,
        created_at,
    )


def context(request: dict) -> dict:
    issuance = request["issuance"]
    return {
        "repository": request["pull_request"]["repository"],
        "pull_request_number": request["pull_request"]["number"],
        "exact_head": request["build_source"]["commit"],
        "current_requirements_sha256": request["requirements"]["digest"],
        "current_policy_version": issuance["policy_version"],
        "current_policy_sha256": issuance["policy_sha256"],
        "current_guard_registry_version": issuance["guard_registry_version"],
        "current_guard_registry_sha256": issuance["guard_registry_sha256"],
    }


def tap_plan(request_digest: str) -> dict:
    value = json.loads((FIXTURES / "tap-plan.json").read_bytes())
    value["request_digest"] = request_digest
    return value


class CheckProjectionCollectionTests(unittest.TestCase):
    def test_selects_only_the_exact_protected_request_and_never_uses_time_for_selection(self) -> None:
        expected, expected_digest, _ = request_fixture()
        stale = discovered("same-head-reissued-request.json", created_at="2026-08-10T11:00:00Z")
        current = discovered(created_at="2026-08-09T10:00:00Z")

        result = build_check_projection_input(
            context=context(expected),
            applicable=True,
            expected_request=expected,
            discovered_requests=(stale, current),
            tap_plan=tap_plan(expected_digest),
            blockers=(),
            public_records=(),
            request_claimed=False,
            now="2026-08-09T10:14:59Z",
        )

        self.assertEqual(result["expected_request_digest"], expected_digest)
        self.assertEqual(result["expected_request"], json.loads(canonical_bytes(expected)))
        self.assertEqual(result["request"]["digest"], expected_digest)
        self.assertEqual(result["request"]["immutable_reference"], current.asset_url)
        self.assertFalse(result["discovery_delayed"])

    def test_missing_request_and_not_applicable_inputs_drop_all_downstream_facts(self) -> None:
        expected, expected_digest, _ = request_fixture()
        missing = build_check_projection_input(
            context=context(expected),
            applicable=True,
            expected_request=expected,
            discovered_requests=(),
            tap_plan=tap_plan(expected_digest),
            blockers=(),
            public_records=(),
            request_claimed=False,
            now="2026-08-09T10:30:00Z",
        )
        self.assertEqual(missing["expected_request_digest"], "0" * 64)
        self.assertNotIn("expected_request", missing)
        self.assertNotIn("request", missing)
        self.assertNotIn("tap_plan", missing)
        self.assertEqual(missing["public_records"], [])

        not_applicable = build_check_projection_input(
            context=context(expected),
            applicable=False,
            expected_request=None,
            discovered_requests=(discovered(),),
            tap_plan=None,
            blockers=(),
            public_records=(),
            request_claimed=False,
            now="2026-08-09T10:30:00Z",
        )
        self.assertEqual(not_applicable["expected_request_digest"], "0" * 64)
        self.assertFalse(not_applicable["discovery_delayed"])

    def test_not_applicable_collection_does_not_scan_unrelated_public_state(self) -> None:
        expected, _, _ = request_fixture()

        class FailingClient:
            def scan(self):
                raise AssertionError("not-applicable collection scanned GitHub")

        result = collect_check_projection_input(
            tap_root=TAP_ROOT,
            exact_head_root=None,
            context=context(expected),
            applicable=False,
            expected_requirements=None,
            formula_requirements=None,
            now="2026-08-09T10:30:00Z",
            client=FailingClient(),
        )

        self.assertFalse(result["applicable"])
        self.assertEqual(result["public_records"], [])

    def test_discovery_delay_is_only_a_fifteen_minute_unclaimed_audit_diagnostic(self) -> None:
        expected, expected_digest, _ = request_fixture()
        current = discovered()
        base = dict(
            context=context(expected),
            applicable=True,
            expected_request=expected,
            discovered_requests=(current,),
            tap_plan=tap_plan(expected_digest),
            blockers=(),
        )
        before = build_check_projection_input(
            **base,
            public_records=(),
            request_claimed=False,
            now="2026-08-09T10:14:59Z",
        )
        delayed = build_check_projection_input(
            **base,
            public_records=(),
            request_claimed=False,
            now="2026-08-09T10:15:00Z",
        )
        self.assertFalse(before["discovery_delayed"])
        self.assertTrue(delayed["discovery_delayed"])

        claimed = build_check_projection_input(
            **base,
            public_records=(),
            request_claimed=True,
            now="2026-08-09T12:00:00Z",
        )
        self.assertFalse(claimed["discovery_delayed"])

    def test_envelopes_keep_manifest_and_record_digests_distinct_and_sort_deterministically(self) -> None:
        expected, expected_digest, _ = request_fixture()
        current = discovered()
        record = json.loads(
            (FIXTURES / "product/evidence-record.json").read_bytes()
        )
        envelopes = []
        for marker in ("c", "b"):
            envelopes.append(
                public_record_envelope(
                    kind="product-evidence",
                    locator={
                        "repository": "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/mini-tool/attempts",
                        "digest": "sha256:" + marker * 64,
                        "immutable_reference": (
                            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                            f"mini-tool/attempts@sha256:{marker * 64}"
                        ),
                    },
                    record=record,
                )
            )
        result = build_check_projection_input(
            context=context(expected),
            applicable=True,
            expected_request=expected,
            discovered_requests=(current,),
            tap_plan=tap_plan(expected_digest),
            blockers=(),
            public_records=tuple(reversed(envelopes)),
            request_claimed=True,
            now="2026-08-09T10:01:00Z",
        )
        self.assertEqual(
            [item["digest"] for item in result["public_records"]],
            ["b" * 64, "c" * 64],
        )
        self.assertTrue(
            all(
                item["record_sha256"] == canonical_sha256(record)
                and item["record_sha256"] != item["digest"]
                for item in result["public_records"]
            )
        )
        self.assertEqual(json.loads(canonical_bytes(result)), result)

    def test_scheduler_output_owns_terminal_required_blockers_without_rewriting_attempts(self) -> None:
        required = '{"architecture":"wasm32","identity":"mini-tool","kind":"formula"}'
        waiting = '{"architecture":"wasm32","identity":"waiting","kind":"formula"}'
        background = '{"architecture":"wasm32","identity":"background","kind":"formula"}'
        request_digest = "a" * 64
        attempt_digest = "b" * 64
        attempt = AttemptFactV1(
            request_sha256=request_digest,
            subject=required,
            contract_sha256="c" * 64,
            retry_ordinal=3,
            outcome="failure",
            guard_code="build_failed",
            completed_at="2026-08-09T10:00:00.000Z",
            record_sha256=attempt_digest,
        )
        locator = {
            "repository": "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/mini-tool/attempts",
            "digest": "sha256:" + attempt_digest,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                "mini-tool/attempts@sha256:" + attempt_digest
            ),
        }
        inventory = PublicSchedulingInventoryV1(
            records=SchedulingRecordsV1(
                attempts=(attempt,), candidates=(), verifications=()
            ),
            candidate_locators={},
            candidate_records={},
            attempt_locators={attempt_digest: locator},
            attempt_records={attempt_digest: {"kind": "kandelo-abi-staging-attempt-outcome"}},
        )
        scheduling = SchedulingDecisionV1(
            request_sha256=request_digest,
            ready=(),
            blocked=(
                BlockedSubjectV1(required, "build_failed", exhausted=True),
                BlockedSubjectV1(
                    waiting,
                    "transient_infrastructure_failure",
                    next_action="wait",
                    next_eligible_at="2026-08-09T10:01:00.000Z",
                ),
                BlockedSubjectV1(background, "build_failed"),
            ),
            complete=(),
            pending=(),
        )

        self.assertTrue(inventory_claims_request(inventory, request_digest))
        self.assertEqual(
            scheduling_blockers(
                scheduling,
                inventory=inventory,
                required_subjects=(required, waiting),
            ),
            [
                {
                    "guard_code": "build_failed",
                    "subject_kind": "formula",
                    "subject": required,
                    "record": {
                        "kind": "attempt-outcome",
                        "digest": attempt_digest,
                        "immutable_reference": locator["immutable_reference"],
                    },
                }
            ],
        )

    def test_collector_uses_anonymous_inventory_and_scheduler_facts_for_current_request(self) -> None:
        expected, expected_digest, _ = request_fixture()
        current = discovered()
        plan = tap_plan(expected_digest)
        required = plan["required_subjects"][0]
        attempt_digest = "b" * 64
        attempt = AttemptFactV1(
            request_sha256=expected_digest,
            subject=required,
            contract_sha256="c" * 64,
            retry_ordinal=3,
            outcome="failure",
            guard_code="build_failed",
            completed_at="2026-08-09T10:00:00.000Z",
            record_sha256=attempt_digest,
        )
        locator = {
            "repository": "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/mini-tool/attempts",
            "digest": "sha256:" + attempt_digest,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                "mini-tool/attempts@sha256:" + attempt_digest
            ),
        }
        inventory = PublicSchedulingInventoryV1(
            records=SchedulingRecordsV1(
                attempts=(attempt,), candidates=(), verifications=()
            ),
            candidate_locators={},
            candidate_records={},
            attempt_locators={attempt_digest: locator},
            attempt_records={attempt_digest: {"kind": "kandelo-abi-staging-attempt-outcome"}},
        )
        scheduling = SchedulingDecisionV1(
            request_sha256=expected_digest,
            ready=(),
            blocked=(BlockedSubjectV1(required, "build_failed", exhausted=True),),
            complete=(),
            pending=(),
        )

        class Client:
            def scan(self):
                return (discovered("same-head-reissued-request.json"), current)

        with tempfile.TemporaryDirectory() as temporary:
            exact_root = Path(temporary)
            catalog_path = exact_root / "images/vfs/products/generated/catalog.json"
            catalog_path.parent.mkdir(parents=True)
            catalog_path.write_bytes(b"{}\n")
            with (
                patch(
                    "scripts.abi_staging.check_projection._checked_exact_head_checkout",
                    return_value=exact_root,
                ),
                patch(
                    "scripts.abi_staging.check_projection.plan_exact_tap_request",
                    return_value=plan,
                ),
                patch(
                    "scripts.abi_staging.check_projection.load_verification_tests",
                    return_value=(),
                ),
                patch(
                    "scripts.abi_staging.check_projection.scan_scheduling_inventory",
                    return_value=inventory,
                ),
                patch(
                    "scripts.abi_staging.check_projection.prepare_tap_plan_contracts",
                    return_value=(plan, {}, {}),
                ),
                patch(
                    "scripts.abi_staging.check_projection.schedule_ready_batch",
                    return_value=scheduling,
                ),
                patch(
                    "scripts.abi_staging.check_projection._formula_record_envelopes",
                    return_value=[],
                ),
                patch(
                    "scripts.abi_staging.check_projection._maintenance_record_envelopes",
                    return_value=[],
                ),
                patch(
                    "scripts.abi_staging.check_projection._product_record_envelopes",
                    return_value=[],
                ),
                patch(
                    "scripts.abi_staging.check_projection.load_canonical_mapping",
                    return_value={},
                ),
            ):
                result = collect_check_projection_input(
                    tap_root=TAP_ROOT,
                    exact_head_root=exact_root,
                    context=context(expected),
                    applicable=True,
                    expected_requirements=expected["requirements"],
                    formula_requirements=(),
                    now="2026-08-09T12:00:00.000Z",
                    client=Client(),
                    transport=object(),
                )

        self.assertFalse(result["discovery_delayed"])
        self.assertEqual(result["public_records"], [])
        self.assertEqual(
            result["tap_plan"]["blockers"][0]["record"]["kind"],
            "attempt-outcome",
        )


if __name__ == "__main__":
    unittest.main()
