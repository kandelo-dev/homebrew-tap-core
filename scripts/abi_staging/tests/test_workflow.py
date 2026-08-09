from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.plan import exact_formula_subject
from scripts.abi_staging.scheduler import ReadyWorkV1, SchedulingDecisionV1
from scripts.abi_staging.workflow import (
    WorkflowError,
    build_workflow_manifest,
    validate_workflow_manifest,
)


TAP_ROOT = Path(__file__).resolve().parents[3]
REQUEST = json.loads(
    (TAP_ROOT / "Kandelo/staging/fixtures/request/current-request.json").read_bytes()
)
PLAN = json.loads((TAP_ROOT / "Kandelo/staging/fixtures/tap-plan.json").read_bytes())


def _digest(character: str) -> str:
    return character * 64


def _ready() -> SchedulingDecisionV1:
    required = exact_formula_subject("libcxx", "wasm32")
    background = exact_formula_subject("asa", "wasm32")
    verified = exact_formula_subject("openssl", "wasm32")
    reused = exact_formula_subject("zlib", "wasm32")
    return SchedulingDecisionV1(
        request_sha256=PLAN["request_digest"],
        ready=(
            ReadyWorkV1(
                required,
                "required",
                "build-candidate",
                0,
                _digest("1"),
            ),
            ReadyWorkV1(
                verified,
                "required",
                "verify-candidate",
                1,
                _digest("2"),
                candidate_record_sha256=_digest("3"),
                test_definition_sha256=_digest("4"),
                host="node",
            ),
            ReadyWorkV1(
                reused,
                "required",
                "reuse-candidate",
                0,
                _digest("6"),
                candidate_record_sha256=_digest("7"),
            ),
            ReadyWorkV1(
                background,
                "background",
                "build-candidate",
                0,
                _digest("5"),
            ),
        ),
        blocked=(),
        complete=(),
        pending=(),
    )


def _planned() -> dict[str, object]:
    plan = copy.deepcopy(PLAN)
    contracts = {
        exact_formula_subject("libcxx", "wasm32"): _digest("1"),
        exact_formula_subject("openssl", "wasm32"): _digest("2"),
        exact_formula_subject("zlib", "wasm32"): _digest("6"),
        exact_formula_subject("asa", "wasm32"): _digest("5"),
    }
    for formula in plan["formulae"]:
        subject = exact_formula_subject(
            formula["identity"]["name"], formula["identity"]["architecture"]
        )
        if subject in contracts:
            formula["contract_sha256"] = contracts[subject]
    return plan


def _locator(digest: str = _digest("3"), formula: str = "openssl") -> dict[str, str]:
    return {
        "repository": f"ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/{formula}",
        "digest": "sha256:" + digest,
        "immutable_reference": (
            f"ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/{formula}@sha256:"
            + digest
        ),
    }


class WorkflowManifestTests(unittest.TestCase):
    def manifest(self, mode: str = "active") -> dict[str, object]:
        return build_workflow_manifest(
            mode=mode,
            request=REQUEST,
            request_sha256=PLAN["request_digest"],
            request_asset_url=PLAN["request_asset_url"],
            lifecycle={"state": "open", "current_head": REQUEST["build_source"]["commit"], "merged_commit": None},
            tap_plan=_planned(),
            scheduling=_ready(),
            candidate_locators={
                _digest("3"): _locator(),
                _digest("7"): _locator(_digest("7"), "zlib"),
            },
            max_ready_subjects=16,
        )

    def test_active_manifest_has_bounded_required_first_matrices(self) -> None:
        manifest = self.manifest()
        validate_workflow_manifest(manifest, max_ready_subjects=16)
        self.assertEqual(
            [item["work_class"] for item in manifest["build_work"]],
            ["required", "background"],
        )
        self.assertEqual(len(manifest["verify_work"]), 1)
        self.assertEqual(len(manifest["reuse_work"]), 1)
        self.assertEqual(
            manifest["build_matrix"],
            {"include": [{"work_id": item["work_id"]} for item in manifest["build_work"]]},
        )
        self.assertEqual(
            manifest["verify_matrix"],
            {"include": [{"work_id": manifest["verify_work"][0]["work_id"]}]},
        )
        self.assertEqual(
            manifest["reuse_matrix"],
            {"include": [{"work_id": manifest["reuse_work"][0]["work_id"]}]},
        )
        for item in [
            *manifest["build_work"],
            *manifest["verify_work"],
            *manifest["reuse_work"],
        ]:
            self.assertRegex(item["work_id"], r"^[0-9a-f]{64}$")
            self.assertNotIn("abi-8", item["artifact_name"])

    def test_observe_mode_retains_intents_but_starts_no_candidate_jobs(self) -> None:
        manifest = self.manifest("observe")
        self.assertEqual(len(manifest["build_work"]), 2)
        self.assertEqual(len(manifest["verify_work"]), 1)
        self.assertEqual(len(manifest["reuse_work"]), 1)
        self.assertEqual(manifest["build_matrix"], {"include": []})
        self.assertEqual(manifest["verify_matrix"], {"include": []})
        self.assertEqual(manifest["reuse_matrix"], {"include": []})

    def test_duplicate_scheduled_runs_converge_to_identical_work_identity(self) -> None:
        first = self.manifest()
        second = self.manifest()
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))

    def test_wrong_candidate_and_unbounded_or_duplicate_work_are_rejected(self) -> None:
        with self.assertRaises(WorkflowError):
            build_workflow_manifest(
                mode="active",
                request=REQUEST,
                request_sha256=PLAN["request_digest"],
                request_asset_url=PLAN["request_asset_url"],
                lifecycle={"state": "open", "current_head": REQUEST["build_source"]["commit"], "merged_commit": None},
                tap_plan=_planned(),
                scheduling=_ready(),
                candidate_locators={
                    _digest("3"): {**_locator(), "digest": "sha256:" + _digest("9")},
                    _digest("7"): _locator(_digest("7"), "zlib"),
                },
                max_ready_subjects=16,
            )
        duplicate = self.manifest()
        duplicate["build_work"].append(copy.deepcopy(duplicate["build_work"][0]))
        with self.assertRaises(WorkflowError):
            validate_workflow_manifest(duplicate, max_ready_subjects=16)
        bounded = self.manifest()
        bounded["build_work"] = bounded["build_work"] * 9
        with self.assertRaises(WorkflowError):
            validate_workflow_manifest(bounded, max_ready_subjects=16)


if __name__ == "__main__":
    unittest.main()
