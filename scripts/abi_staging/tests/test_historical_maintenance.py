from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.historical_maintenance import (
    HistoricalMaintenanceError,
    authorize_historical_maintenance,
    build_historical_authorization_oci_plan,
    build_historical_repair_plan,
    derive_abi_epoch_status,
    load_historical_maintenance_evidence,
    load_historical_maintenance_evidence_archive,
    main as historical_main,
    validate_historical_repair_completion,
    validate_historical_repair_plan,
)
from scripts.abi_staging.policy import load_tap_staging_policy
from scripts.abi_staging.records import (
    validate_abi_epoch_status,
    validate_historical_maintenance_authorization,
)


TAP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ABI = 7
SUCCESSOR_ABI = SOURCE_ABI + 1
HISTORY_DIGEST = "b4cfe0546450034e1922d8afbe8c90e0814ba7069f47078409b5e5283d0ead55"
CONTRACT_DIGEST = "c" * 64
KERNEL_COMMIT = "3" * 40
KERNEL_TREE = "4" * 40


def _history_record() -> dict[str, object]:
    return json.loads(
        (TAP_ROOT / "Kandelo/staging/fixtures/abi-history-record.json").read_bytes()
    )


def _source() -> dict[str, str]:
    plan = _history_record()["plan"]
    return {
        "repository": "kandelo-dev/homebrew-tap-core",
        "commit": plan["preactivation_tap_commit"],
        "tree": plan["preactivation_tap_tree"],
    }


def _protection_snapshot(
    *,
    covered: bool = True,
    commit: str | None = None,
    tree: str | None = None,
) -> dict[str, object]:
    plan = _history_record()["plan"]
    ref_commit = plan["preactivation_tap_commit"] if commit is None else commit
    ref_tree = plan["preactivation_tap_tree"] if tree is None else tree
    return {
        "schema": 1,
        "kind": "kandelo-abi-history-protection-snapshot",
        "repository": "kandelo-dev/homebrew-tap-core",
        "branch": plan["branch"],
        "phase": "postcreate",
        "ref": {
            "object": ref_commit,
            "tree": ref_tree,
        },
        "direct": (
            {
                "branch": plan["branch"],
                "allow_deletions": False,
                "allow_force_pushes": False,
                "enforce_admins": True,
            }
            if covered
            else None
        ),
        "rulesets": [],
    }


def _policy_identity() -> dict[str, object]:
    return {
        "policy_version": 4,
        "policy_sha256": "5" * 64,
        "guard_registry_version": 1,
        "guard_registry_sha256": "6" * 64,
    }


def _maintainer(*, permission: str = "maintain") -> dict[str, str]:
    return {
        "login": "maintainer",
        "permission": permission,
        "authorization_reference": (
            "https://github.com/kandelo-dev/homebrew-tap-core/"
            "actions/runs/91/attempts/2"
        ),
    }


def _run(job: str) -> dict[str, object]:
    return {
        "repository": "kandelo-dev/homebrew-tap-core",
        "workflow_ref": (
            ".github/workflows/abi-staging-maintenance.yml@refs/heads/main"
        ),
        "run_id": 91,
        "run_attempt": 2,
        "job": job,
    }


def _record_link(digest: str, kind: str) -> dict[str, str]:
    return {
        "record_sha256": digest,
        "immutable_reference": (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-7-records/"
            f"{kind}@sha256:{digest}"
        ),
    }


def _authorization(*, reason: str = "failed-package-repair") -> dict[str, object]:
    return authorize_historical_maintenance(
        target_abi=SOURCE_ABI,
        current_abi=SUCCESSOR_ABI,
        branch_source=_source(),
        branch_metadata={
            "kandelo_abi": SOURCE_ABI,
            "kandelo_repository": "Automattic/kandelo",
            "kandelo_commit": KERNEL_COMMIT,
            "tap_repository": "kandelo-dev/homebrew-tap-core",
            "tap_commit": _source()["commit"],
        },
        kandelo_source={
            "repository": "Automattic/kandelo",
            "commit": KERNEL_COMMIT,
            "tree": KERNEL_TREE,
        },
        formula={
            "tap": "kandelo-dev/homebrew-tap-core",
            "formula": "bash",
            "architecture": "wasm32",
        },
        reason=reason,
        maintainer=_maintainer(),
        policy=_policy_identity(),
        history_record=_history_record(),
        history_record_link=_record_link(HISTORY_DIGEST, "history"),
        protection_snapshot=_protection_snapshot(),
        run=_run("authorize-historical"),
    )


def _subject(name: str) -> dict[str, str]:
    return {"formula": name, "architecture": "wasm32"}


def _outcome(name: str, outcome: str, digest: str) -> dict[str, object]:
    return {
        "subject": _subject(name),
        "outcome": outcome,
        "record": _record_link(digest, "attempts"),
    }


def _historical_evidence() -> dict[str, object]:
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-historical-maintenance-evidence",
        "operation": "historical-repair",
        "target_abi": SOURCE_ABI,
        "branch_source": _source(),
        "branch_metadata": {
            "kandelo_abi": SOURCE_ABI,
            "kandelo_repository": "Automattic/kandelo",
            "kandelo_commit": KERNEL_COMMIT,
            "tap_repository": "kandelo-dev/homebrew-tap-core",
            "tap_commit": _source()["commit"],
        },
        "branch_lineage": None,
        "kandelo_source": {
            "repository": "Automattic/kandelo",
            "commit": KERNEL_COMMIT,
            "tree": KERNEL_TREE,
        },
        "formula": {
            "tap": "kandelo-dev/homebrew-tap-core",
            "formula": "bash",
            "architecture": "wasm32",
        },
        "reason": "failed-package-repair",
        "policy": _policy_identity(),
        "history_record": _history_record(),
        "history_record_link": _record_link(HISTORY_DIGEST, "history"),
        "expected_contract_sha256": CONTRACT_DIGEST,
        "dependencies": [],
        "reuse": None,
    }


class HistoricalMaintenanceTests(unittest.TestCase):
    def test_loads_one_canonical_protected_historical_evidence_archive(self) -> None:
        body = canonical_bytes(_historical_evidence())
        loaded = load_historical_maintenance_evidence(body)
        self.assertEqual(loaded.target_abi, SOURCE_ABI)
        self.assertEqual(loaded.branch_source, _source())
        self.assertEqual(loaded.expected_contract_sha256, CONTRACT_DIGEST)

        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("maintenance-evidence.json", body)
        archive_body = archive_buffer.getvalue()
        loaded_archive = load_historical_maintenance_evidence_archive(
            archive_body,
            expected_sha256=hashlib.sha256(archive_body).hexdigest(),
        )
        self.assertEqual(loaded_archive, loaded)

        changed = copy.deepcopy(_historical_evidence())
        changed["formula"]["formula"] = "foreign"
        changed["formula"]["tap"] = "another/tap"
        with self.assertRaises(HistoricalMaintenanceError):
            load_historical_maintenance_evidence(canonical_bytes(changed))

    def test_authorization_oci_plan_is_exact_abi_scoped_and_not_an_override(self) -> None:
        policy = load_tap_staging_policy(
            TAP_ROOT / "Kandelo/staging/tap-policy.toml"
        )
        authorization = _authorization()
        plan = build_historical_authorization_oci_plan(
            authorization, tap_policy=policy
        )
        self.assertEqual(
            plan.repository,
            (
                "kandelo-dev/homebrew-tap-core-abi-7-candidates/bash/"
                "maintenance/historical-authorizations"
            ),
        )
        self.assertEqual(
            plan.annotations["dev.kandelo.abi-staging.classification"],
            "historical-maintenance",
        )
        self.assertNotIn("override", plan.artifact_type)

    def test_cli_requires_permission_fresh_protection_and_immutable_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = historical_main(
                [
                    "authorize",
                    "--evidence-artifact-id",
                    "1",
                    "--evidence-sha256",
                    "a" * 64,
                    "--justification",
                    "repair one exact historical package",
                    "--actor",
                    "maintainer",
                    "--authorization-reference",
                    "https://github.com/kandelo-dev/homebrew-tap-core/actions/runs/1/attempts/1",
                    "--out",
                    str(Path(temporary) / "result.json"),
                ]
            )
        self.assertEqual(result, 1)

    def test_epoch_becomes_retiring_then_retired_without_gating_successor(self) -> None:
        scheduled = tuple(
            _subject(name) for name in ("bash", "curl", "dash", "zlib")
        )
        active = derive_abi_epoch_status(
            abi=SOURCE_ABI,
            scheduled_subjects=scheduled,
            terminal_outcomes=(),
            successor_activated=False,
            repair_links=(),
            run=_run("derive-epoch"),
        )
        retiring = derive_abi_epoch_status(
            abi=SOURCE_ABI,
            scheduled_subjects=scheduled,
            terminal_outcomes=(_outcome("bash", "failure", "1" * 64),),
            successor_activated=True,
            repair_links=(),
            run=_run("derive-epoch"),
            previous=active,
        )
        retired = derive_abi_epoch_status(
            abi=SOURCE_ABI,
            scheduled_subjects=scheduled,
            terminal_outcomes=(
                _outcome("bash", "failure", "1" * 64),
                _outcome("curl", "success", "2" * 64),
                _outcome("dash", "timeout", "3" * 64),
                _outcome("zlib", "canceled", "4" * 64),
            ),
            successor_activated=True,
            repair_links=(),
            run=_run("derive-epoch"),
            previous=retiring,
        )

        self.assertEqual(active["state"], "active")
        self.assertEqual(retiring["state"], "retiring")
        self.assertEqual(retired["state"], "retired")
        self.assertFalse(retiring["gates_successor"])
        self.assertFalse(retired["gates_successor"])
        validate_abi_epoch_status(retiring["record"])
        validate_abi_epoch_status(retired["record"])

    def test_repair_and_reopen_preserve_terminal_history(self) -> None:
        scheduled = (_subject("bash"), _subject("dash"))
        previous = derive_abi_epoch_status(
            abi=SOURCE_ABI,
            scheduled_subjects=scheduled,
            terminal_outcomes=(_outcome("bash", "failure", "1" * 64),),
            successor_activated=True,
            repair_links=(),
            run=_run("derive-epoch"),
        )
        with self.assertRaises(HistoricalMaintenanceError):
            derive_abi_epoch_status(
                abi=SOURCE_ABI,
                scheduled_subjects=scheduled,
                terminal_outcomes=(),
                successor_activated=True,
                repair_links=(_record_link("9" * 64, "repairs"),),
                run=_run("derive-epoch"),
                previous=previous,
            )

        repaired = derive_abi_epoch_status(
            abi=SOURCE_ABI,
            scheduled_subjects=scheduled,
            terminal_outcomes=(_outcome("bash", "failure", "1" * 64),),
            successor_activated=True,
            repair_links=(_record_link("9" * 64, "repairs"),),
            run=_run("derive-epoch"),
            previous=previous,
        )
        self.assertEqual(repaired["state"], "retiring")
        self.assertEqual(
            repaired["record"]["terminal_outcomes"],
            previous["record"]["terminal_outcomes"],
        )

    def test_authorizes_failed_repair_and_security_rebuild_on_exact_history(self) -> None:
        for reason in ("failed-package-repair", "security-rebuild"):
            with self.subTest(reason=reason):
                record = _authorization(reason=reason)
                validate_historical_maintenance_authorization(record)
                self.assertEqual(record["abi"], SOURCE_ABI)
                self.assertEqual(record["branch"], f"abi/{SOURCE_ABI}")
                self.assertEqual(record["source"], _source())
                self.assertEqual(record["reason"], reason)

    def test_authorizes_only_a_proven_descendant_after_prior_historical_repairs(self) -> None:
        advanced_source = {
            **_source(),
            "commit": "a" * 40,
            "tree": "b" * 40,
        }
        common = {
            "target_abi": SOURCE_ABI,
            "current_abi": SUCCESSOR_ABI,
            "branch_source": advanced_source,
            "branch_metadata": {
                "kandelo_abi": SOURCE_ABI,
                "kandelo_repository": "Automattic/kandelo",
                "kandelo_commit": KERNEL_COMMIT,
                "tap_repository": "kandelo-dev/homebrew-tap-core",
                "tap_commit": _source()["commit"],
            },
            "kandelo_source": {
                "repository": "Automattic/kandelo",
                "commit": KERNEL_COMMIT,
                "tree": KERNEL_TREE,
            },
            "formula": {
                "tap": "kandelo-dev/homebrew-tap-core",
                "formula": "bash",
                "architecture": "wasm32",
            },
            "reason": "failed-package-repair",
            "maintainer": _maintainer(),
            "policy": _policy_identity(),
            "history_record": _history_record(),
            "history_record_link": _record_link(HISTORY_DIGEST, "history"),
            "protection_snapshot": _protection_snapshot(
                commit=advanced_source["commit"], tree=advanced_source["tree"]
            ),
            "run": _run("authorize-historical"),
        }
        with self.assertRaises(HistoricalMaintenanceError):
            authorize_historical_maintenance(**common)

        record = authorize_historical_maintenance(
            **common,
            branch_lineage={
                "ancestor": _source()["commit"],
                "descendant": advanced_source["commit"],
                "descendant_tree": advanced_source["tree"],
                "relation": "protected-first-parent-descendant",
            },
        )
        self.assertEqual(record["source"], advanced_source)

    def test_rejects_unauthorized_moved_unprotected_or_main_authority(self) -> None:
        base = {
            "target_abi": SOURCE_ABI,
            "current_abi": SUCCESSOR_ABI,
            "branch_source": _source(),
            "branch_metadata": {
                "kandelo_abi": SOURCE_ABI,
                "kandelo_repository": "Automattic/kandelo",
                "kandelo_commit": KERNEL_COMMIT,
                "tap_repository": "kandelo-dev/homebrew-tap-core",
                "tap_commit": _source()["commit"],
            },
            "kandelo_source": {
                "repository": "Automattic/kandelo",
                "commit": KERNEL_COMMIT,
                "tree": KERNEL_TREE,
            },
            "formula": {
                "tap": "kandelo-dev/homebrew-tap-core",
                "formula": "bash",
                "architecture": "wasm32",
            },
            "reason": "failed-package-repair",
            "maintainer": _maintainer(),
            "policy": _policy_identity(),
            "history_record": _history_record(),
            "history_record_link": _record_link(HISTORY_DIGEST, "history"),
            "protection_snapshot": _protection_snapshot(),
            "run": _run("authorize-historical"),
        }
        mutations = []
        unauthorized = copy.deepcopy(base)
        unauthorized["maintainer"] = _maintainer(permission="read")
        mutations.append(unauthorized)
        moved = copy.deepcopy(base)
        moved["branch_source"]["commit"] = "f" * 40
        mutations.append(moved)
        unprotected = copy.deepcopy(base)
        unprotected["protection_snapshot"] = _protection_snapshot(covered=False)
        mutations.append(unprotected)
        current = copy.deepcopy(base)
        current["target_abi"] = SUCCESSOR_ABI
        current["branch_metadata"]["kandelo_abi"] = SUCCESSOR_ABI
        mutations.append(current)
        main = copy.deepcopy(base)
        main["history_record"]["plan"]["branch"] = "main"
        mutations.append(main)
        foreign_history = copy.deepcopy(base)
        foreign_history["history_record_link"]["immutable_reference"] = (
            "ghcr.io/attacker/foreign/history@sha256:" + HISTORY_DIGEST
        )
        mutations.append(foreign_history)

        for candidate in mutations:
            with self.subTest(candidate=candidate):
                with self.assertRaises(HistoricalMaintenanceError):
                    authorize_historical_maintenance(**candidate)

    def test_plan_reuses_normal_uncredentialed_lane_under_exact_abi(self) -> None:
        authorization = _authorization()
        plan = build_historical_repair_plan(
            authorization=authorization,
            authorization_sha256=canonical_sha256(authorization),
            history_record=_history_record(),
            protection_snapshot=_protection_snapshot(),
            expected_contract_sha256=CONTRACT_DIGEST,
            dependencies=(
                {
                    "formula": "zlib",
                    "architecture": "wasm32",
                    "target_abi": SOURCE_ABI,
                    "contract_sha256": "d" * 64,
                },
            ),
            reuse=None,
        )

        self.assertEqual(plan["metadata_branch"], f"abi/{SOURCE_ABI}")
        self.assertIn(f"abi-{SOURCE_ABI}-candidates", plan["candidate_repository"])
        self.assertIn(f"abi-{SOURCE_ABI}/bash", plan["canonical_repository"])
        self.assertTrue(plan["build_required"])
        self.assertEqual(
            plan["stages"],
            [
                "build-uncredentialed",
                "publish-candidate-protected",
                "verify-uncredentialed",
                "publish-receipt-protected",
                "publish-canonical-protected",
                "update-historical-metadata-protected",
                "publish-admission-protected",
            ],
        )
        self.assertEqual(plan["override_receipts"], [])
        self.assertTrue(plan["preserve_prior_records"])

    def test_rejects_cross_abi_dependencies_wrong_namespaces_and_reuse_contract(self) -> None:
        authorization = _authorization()
        common = {
            "authorization": authorization,
            "authorization_sha256": canonical_sha256(authorization),
            "history_record": _history_record(),
            "protection_snapshot": _protection_snapshot(),
            "expected_contract_sha256": CONTRACT_DIGEST,
        }
        with self.assertRaises(HistoricalMaintenanceError):
            build_historical_repair_plan(
                **common,
                dependencies=(
                    {
                        "formula": "zlib",
                        "architecture": "wasm32",
                        "target_abi": SUCCESSOR_ABI,
                        "contract_sha256": "d" * 64,
                    },
                ),
                reuse=None,
            )
        with self.assertRaises(HistoricalMaintenanceError):
            build_historical_repair_plan(
                **common,
                dependencies=(),
                reuse={
                    "record_sha256": "e" * 64,
                    "immutable_reference": (
                        "ghcr.io/kandelo-dev/"
                        f"homebrew-tap-core-abi-{SOURCE_ABI}-candidates/bash"
                        "@sha256:"
                        + "e" * 64
                    ),
                    "formula": "bash",
                    "architecture": "wasm32",
                    "target_abi": SOURCE_ABI,
                    "contract_sha256": "f" * 64,
                    "candidate_repository": (
                        "ghcr.io/kandelo-dev/"
                        f"homebrew-tap-core-abi-{SOURCE_ABI}-candidates/bash"
                    ),
                },
            )

    def test_exact_reuse_is_bounded_and_security_rebuild_always_builds(self) -> None:
        authorization = _authorization()
        exact_reuse = {
            "record_sha256": "e" * 64,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/"
                f"homebrew-tap-core-abi-{SOURCE_ABI}-candidates/bash"
                "@sha256:"
                + "e" * 64
            ),
            "formula": "bash",
            "architecture": "wasm32",
            "target_abi": SOURCE_ABI,
            "contract_sha256": CONTRACT_DIGEST,
            "candidate_repository": (
                "ghcr.io/kandelo-dev/"
                f"homebrew-tap-core-abi-{SOURCE_ABI}-candidates/bash"
            ),
        }
        plan = build_historical_repair_plan(
            authorization=authorization,
            authorization_sha256=canonical_sha256(authorization),
            history_record=_history_record(),
            protection_snapshot=_protection_snapshot(),
            expected_contract_sha256=CONTRACT_DIGEST,
            dependencies=(),
            reuse=exact_reuse,
        )
        self.assertFalse(plan["build_required"])
        self.assertEqual(plan["stages"][0], "reuse-exact-candidate")
        validate_historical_repair_plan(plan)

        security = _authorization(reason="security-rebuild")
        with self.assertRaises(HistoricalMaintenanceError):
            build_historical_repair_plan(
                authorization=security,
                authorization_sha256=canonical_sha256(security),
                history_record=_history_record(),
                protection_snapshot=_protection_snapshot(),
                expected_contract_sha256=CONTRACT_DIGEST,
                dependencies=(),
                reuse=exact_reuse,
            )

    def test_plan_validation_rejects_namespace_and_metadata_target_mutation(self) -> None:
        authorization = _authorization()
        plan = build_historical_repair_plan(
            authorization=authorization,
            authorization_sha256=canonical_sha256(authorization),
            history_record=_history_record(),
            protection_snapshot=_protection_snapshot(),
            expected_contract_sha256=CONTRACT_DIGEST,
            dependencies=(),
            reuse=None,
        )
        for mutation in (
            {"metadata_branch": "main"},
            {
                "candidate_repository": plan["candidate_repository"].replace(
                    f"abi-{SOURCE_ABI}-candidates",
                    f"abi-{SUCCESSOR_ABI}-candidates",
                )
            },
            {"override_receipts": [_record_link("7" * 64, "overrides")]},
        ):
            changed = copy.deepcopy(plan)
            changed.update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(HistoricalMaintenanceError):
                    validate_historical_repair_plan(changed)

    def test_completion_requires_new_records_branch_metadata_and_no_override_or_deletion(self) -> None:
        authorization = _authorization()
        authorization_digest = canonical_sha256(authorization)
        plan = build_historical_repair_plan(
            authorization=authorization,
            authorization_sha256=authorization_digest,
            history_record=_history_record(),
            protection_snapshot=_protection_snapshot(),
            expected_contract_sha256=CONTRACT_DIGEST,
            dependencies=(),
            reuse=None,
        )
        completion = {
            "authorization_sha256": authorization_digest,
            "attempt_records": [
                {
                    "record_sha256": "1" * 64,
                    "immutable_reference": (
                        plan["candidate_repository"]
                        + "/attempts@sha256:"
                        + "1" * 64
                    ),
                }
            ],
            "candidate_record": {
                "record_sha256": "2" * 64,
                "immutable_reference": (
                    plan["candidate_repository"] + "@sha256:" + "2" * 64
                ),
                "repository": plan["candidate_repository"],
                "formula": "bash",
                "architecture": "wasm32",
                "target_abi": SOURCE_ABI,
                "contract_sha256": CONTRACT_DIGEST,
            },
            "verification_receipts": [
                {
                    "record_sha256": "3" * 64,
                    "immutable_reference": (
                        plan["candidate_repository"]
                        + "/receipts/bottle-structure/build@sha256:"
                        + "3" * 64
                    ),
                }
            ],
            "admission_record": {
                "record_sha256": "4" * 64,
                "immutable_reference": (
                    plan["canonical_repository"]
                    + "/admissions@sha256:"
                    + "4" * 64
                ),
                "repository": plan["canonical_repository"],
                "branch": f"abi/{SOURCE_ABI}",
                "candidate_record_sha256": "2" * 64,
                "target_abi": SOURCE_ABI,
            },
            "override_receipts": [],
            "deleted_record_sha256s": [],
            "prior_record_sha256s": ["8" * 64, "9" * 64],
            "preserved_prior_record_sha256s": ["8" * 64, "9" * 64],
        }
        validated = validate_historical_repair_completion(plan, completion)
        self.assertEqual(validated["admission_record"]["record_sha256"], "4" * 64)

        for mutation in (
            {"override_receipts": [_record_link("5" * 64, "overrides")]},
            {"deleted_record_sha256s": ["8" * 64]},
            {"preserved_prior_record_sha256s": ["9" * 64]},
            {
                "admission_record": {
                    **completion["admission_record"],
                    "branch": "main",
                }
            },
            {
                "candidate_record": {
                    **completion["candidate_record"],
                    "repository": completion["candidate_record"]["repository"].replace(
                        f"abi-{SOURCE_ABI}-candidates", f"abi-{SUCCESSOR_ABI}-candidates"
                    ),
                }
            },
            {
                "prior_record_sha256s": ["1" * 64, "8" * 64, "9" * 64],
                "preserved_prior_record_sha256s": [
                    "1" * 64,
                    "8" * 64,
                    "9" * 64,
                ],
            },
        ):
            changed = copy.deepcopy(completion)
            changed.update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaises(HistoricalMaintenanceError):
                    validate_historical_repair_completion(plan, changed)

    def test_authorization_and_plan_digests_are_generic_not_timestamp_ordered(self) -> None:
        authorization = _authorization()
        first = build_historical_repair_plan(
            authorization=authorization,
            authorization_sha256=canonical_sha256(authorization),
            history_record=_history_record(),
            protection_snapshot=_protection_snapshot(),
            expected_contract_sha256=CONTRACT_DIGEST,
            dependencies=(),
            reuse=None,
        )
        second = build_historical_repair_plan(
            authorization=authorization,
            authorization_sha256=canonical_sha256(authorization),
            history_record=_history_record(),
            protection_snapshot=_protection_snapshot(),
            expected_contract_sha256=CONTRACT_DIGEST,
            dependencies=(),
            reuse=None,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["work_id"], canonical_sha256(first["identity"]))
        implementation = (
            TAP_ROOT / "scripts/abi_staging/historical_maintenance.py"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(implementation, r"\b(?:42|43)\b")


if __name__ == "__main__":
    unittest.main()
