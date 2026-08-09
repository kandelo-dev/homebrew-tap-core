from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import unittest


TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
KANDELO_ROOT = Path(os.environ["KANDELO_ROOT"])
sys.path.insert(0, str(TAP_ROOT))

from scripts.abi_staging.canonical import canonical_sha256
from scripts.abi_staging.coordination import (
    CoordinationError,
    coordinate_planned_request,
    build_formula_contract,
    prepare_tap_plan_contracts,
    validate_coordination_bundle,
)
from scripts.abi_staging.inventory import PublicSchedulingInventoryV1
from scripts.abi_staging.policy import (
    load_tap_staging_policy,
    load_verification_tests,
)
from scripts.abi_staging.reconcile import (
    PullRequestLifecycleV1,
    ReconciliationDecisionV1,
)
from scripts.abi_staging.scheduler import CandidateFactV1, SchedulingRecordsV1
from scripts.abi_staging.plan import exact_formula_subject


PLAN = json.loads((TAP_ROOT / "Kandelo/staging/fixtures/tap-plan.json").read_bytes())


def formula(name: str, architecture: str = "wasm32") -> dict[str, object]:
    return copy.deepcopy(
        next(
            item
            for item in PLAN["formulae"]
            if item["identity"]["name"] == name
            and item["identity"]["architecture"] == architecture
        )
    )


class ContractCoordinationTests(unittest.TestCase):
    def test_coordination_bundle_is_deterministic_and_observe_mode_starts_no_jobs(self) -> None:
        request = json.loads(
            (TAP_ROOT / "Kandelo/staging/fixtures/request/current-request.json").read_bytes()
        )
        request["requirements"]["products"] = [
            {
                key: product[key]
                for key in ("id", "path", "manifest_sha256")
            }
            for product in PLAN["selected_products"]
        ]
        request["requirements"]["evidence"] = [
            {
                "product_id": "alpha-shell",
                "applicability": "required",
                "node": ["alpha-node"],
                "browser": ["alpha-browser"],
            },
            {
                "product_id": "beta-tools",
                "applicability": "required",
                "node": ["beta-node"],
                "browser": [],
            },
        ]
        request["requirements"]["digest"] = canonical_sha256(
            {
                key: request["requirements"][key]
                for key in ("change_classes", "products", "registries", "evidence")
            }
        )
        self.assertEqual(canonical_sha256(request), PLAN["request_digest"])
        lifecycle = PullRequestLifecycleV1(
            "open", request["build_source"]["commit"], None
        )
        reconciliation = ReconciliationDecisionV1(
            request_digest=PLAN["request_digest"],
            claim_key="sha256:" + PLAN["request_digest"],
            lifecycle=lifecycle,
            current_for_pull_request=True,
            action="observe-open",
            permitted_work=(),
            blockers=(),
        )
        inventory = PublicSchedulingInventoryV1(
            records=SchedulingRecordsV1((), (), ()),
            candidate_locators={},
            candidate_records={},
        )
        arguments = {
            "mode": "observe",
            "tap_root": TAP_ROOT,
            "kandelo_root": KANDELO_ROOT,
            "request": request,
            "request_asset_url": PLAN["request_asset_url"],
            "tap_plan": PLAN,
            "reconciliation": reconciliation,
            "inventory": inventory,
            "now": "2026-08-09T10:00:00.000Z",
            "policy": load_tap_staging_policy(
                TAP_ROOT / "Kandelo/staging/tap-policy.toml"
            ),
            "verification_tests": load_verification_tests(
                TAP_ROOT / "Kandelo/staging/verification-tests.toml"
            ),
        }
        first = coordinate_planned_request(**arguments)
        second = coordinate_planned_request(**arguments)
        validate_coordination_bundle(first, max_ready_subjects=16)
        self.assertEqual(first, second)
        self.assertTrue(first["workflow"]["build_work"])
        self.assertEqual(first["workflow"]["build_matrix"], {"include": []})
        self.assertEqual(first["workflow"]["verify_matrix"], {"include": []})

    def test_contract_planning_advances_only_after_exact_dependency_layers_exist(self) -> None:
        first, contracts, assessments = prepare_tap_plan_contracts(
            tap_root=TAP_ROOT,
            kandelo_root=KANDELO_ROOT,
            tap_plan=PLAN,
            candidate_facts=(),
            candidate_records={},
        )
        by_subject = {
            exact_formula_subject(
                item["identity"]["name"], item["identity"]["architecture"]
            ): item
            for item in first["formulae"]
        }
        openssl = exact_formula_subject("openssl", "wasm32")
        zlib = exact_formula_subject("zlib", "wasm32")
        libcurl = exact_formula_subject("libcurl", "wasm32")
        self.assertIsNotNone(by_subject[openssl]["contract_sha256"])
        self.assertIsNotNone(by_subject[zlib]["contract_sha256"])
        self.assertIsNone(by_subject[libcurl]["contract_sha256"])
        self.assertIn(openssl, contracts)
        self.assertIn(openssl, assessments)

        facts = []
        records = {}
        for index, subject in enumerate((openssl, zlib), start=1):
            record_sha256 = str(index) * 64
            contract_sha256 = by_subject[subject]["contract_sha256"]
            layer_sha256 = str(index + 2) * 64
            facts.append(
                CandidateFactV1(
                    request_sha256=PLAN["request_digest"],
                    subject=subject,
                    contract_sha256=contract_sha256,
                    record_sha256=record_sha256,
                    bottle_layer_sha256=layer_sha256,
                )
            )
            records[record_sha256] = {
                "candidate": {
                    "bottle_layer": {
                        "sha256": layer_sha256,
                        "bytes": 100 + index,
                        "immutable_reference": (
                            "ghcr.io/kandelo-dev/fixture@sha256:" + layer_sha256
                        ),
                    }
                }
            }
        second, _contracts, _assessments = prepare_tap_plan_contracts(
            tap_root=TAP_ROOT,
            kandelo_root=KANDELO_ROOT,
            tap_plan=PLAN,
            candidate_facts=tuple(facts),
            candidate_records=records,
        )
        advanced = {
            exact_formula_subject(
                item["identity"]["name"], item["identity"]["architecture"]
            ): item
            for item in second["formulae"]
        }
        self.assertIsNotNone(advanced[libcurl]["contract_sha256"])

    def test_root_contract_is_derived_from_exact_declared_capture(self) -> None:
        contract, assessment = build_formula_contract(
            tap_root=TAP_ROOT,
            kandelo_root=KANDELO_ROOT,
            tap_plan=PLAN,
            formula_plan=formula("asa"),
            dependency_candidates={},
        )
        self.assertTrue(assessment["complete"])
        self.assertEqual(contract["formula"]["name"], "asa")
        self.assertEqual(contract["target"]["abi"], PLAN["target_abi"]["version"])
        self.assertTrue(contract["kandelo_inputs"])
        self.assertTrue(contract["tap_inputs"])
        self.assertEqual(contract["direct_dependencies"], [])
        self.assertEqual(
            assessment["subject"], exact_formula_subject("asa", "wasm32")
        )

    def test_inline_patch_formula_has_a_valid_content_complete_contract(self) -> None:
        contract, assessment = build_formula_contract(
            tap_root=TAP_ROOT,
            kandelo_root=KANDELO_ROOT,
            tap_plan=PLAN,
            formula_plan=formula("libcxx"),
            dependency_candidates={},
        )
        self.assertTrue(assessment["complete"])
        self.assertIn("inline:__END__", [item["url"] for item in contract["sources"]])

    def test_dependency_contract_requires_one_exact_candidate_layer(self) -> None:
        with self.assertRaises(CoordinationError):
            build_formula_contract(
                tap_root=TAP_ROOT,
                kandelo_root=KANDELO_ROOT,
                tap_plan=PLAN,
                formula_plan=formula("libcurl"),
                dependency_candidates={},
            )
        dependency_candidates = {
            exact_formula_subject("openssl", "wasm32"): {
                "request_sha256": PLAN["request_digest"],
                "subject": exact_formula_subject("openssl", "wasm32"),
                "record_sha256": "1" * 64,
                "contract_sha256": "2" * 64,
                "bottle_layer": {"sha256": "3" * 64, "bytes": 123},
            },
            exact_formula_subject("zlib", "wasm32"): {
                "request_sha256": PLAN["request_digest"],
                "subject": exact_formula_subject("zlib", "wasm32"),
                "record_sha256": "4" * 64,
                "contract_sha256": "5" * 64,
                "bottle_layer": {"sha256": "6" * 64, "bytes": 456},
            },
        }
        contract, _ = build_formula_contract(
            tap_root=TAP_ROOT,
            kandelo_root=KANDELO_ROOT,
            tap_plan=PLAN,
            formula_plan=formula("libcurl"),
            dependency_candidates=dependency_candidates,
        )
        self.assertEqual(
            [item["formula"] for item in contract["direct_dependencies"]],
            ["openssl", "zlib"],
        )
        changed = copy.deepcopy(dependency_candidates)
        changed[exact_formula_subject("zlib", "wasm32")]["request_sha256"] = "9" * 64
        with self.assertRaises(CoordinationError):
            build_formula_contract(
                tap_root=TAP_ROOT,
                kandelo_root=KANDELO_ROOT,
                tap_plan=PLAN,
                formula_plan=formula("libcurl"),
                dependency_candidates=changed,
            )


if __name__ == "__main__":
    unittest.main()
