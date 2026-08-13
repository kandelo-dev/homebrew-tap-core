from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.coordination import coordinate_planned_request
from scripts.abi_staging.inventory import PublicSchedulingInventoryV1
from scripts.abi_staging.policy import (
    load_tap_staging_policy,
    load_verification_tests,
)
from scripts.abi_staging.reconcile import (
    PullRequestLifecycleV1,
    ReconciliationDecisionV1,
)
from scripts.abi_staging.scheduler import SchedulingRecordsV1


TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
KANDELO_ROOT = Path(os.environ["KANDELO_ROOT"])
PLAN = json.loads((TAP_ROOT / "Kandelo/staging/fixtures/tap-plan.json").read_bytes())


def _active_bundle() -> dict[str, object]:
    request = json.loads(
        (TAP_ROOT / "Kandelo/staging/fixtures/request/current-request.json").read_bytes()
    )
    request["requirements"]["products"] = [
        {key: product[key] for key in ("id", "path", "manifest_sha256")}
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
    return coordinate_planned_request(
        mode="active",
        tap_root=TAP_ROOT,
        kandelo_root=KANDELO_ROOT,
        request=request,
        request_asset_url=PLAN["request_asset_url"],
        tap_plan=PLAN,
        reconciliation=reconciliation,
        inventory=PublicSchedulingInventoryV1(
            records=SchedulingRecordsV1((), (), ()),
            candidate_locators={},
            candidate_records={},
        ),
        now="2026-08-09T10:00:00.000Z",
        policy=load_tap_staging_policy(
            TAP_ROOT / "Kandelo/staging/tap-policy.toml"
        ),
        verification_tests=load_verification_tests(
            TAP_ROOT / "Kandelo/staging/verification-tests.toml"
        ),
    )


class WorkflowExecutionTests(unittest.TestCase):
    def test_cli_binds_protected_github_run_to_build_execution(self) -> None:
        from scripts.abi_staging import cli

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "handoff"
            with patch.object(cli, "execute_build_work", return_value=0) as execute:
                status = cli.main(
                    [
                        "execute-build-work",
                        "--coordination",
                        str(Path(temporary) / "coordination"),
                        "--work-id",
                        "a" * 64,
                        "--kandelo-root",
                        str(KANDELO_ROOT),
                        "--tap-root",
                        str(TAP_ROOT),
                        "--run-id",
                        "808",
                        "--run-attempt",
                        "2",
                        "--workflow-ref",
                        (
                            "kandelo-dev/homebrew-tap-core/"
                            ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
                        ),
                        "--out",
                        str(output),
                    ]
                )
        self.assertEqual(status, 0)
        self.assertEqual(
            execute.call_args.kwargs["run"],
            {
                "repository": "kandelo-dev/homebrew-tap-core",
                "workflow_ref": (
                    "kandelo-dev/homebrew-tap-core/"
                    ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
                ),
                "run_id": 808,
                "run_attempt": 2,
                "job": "build-candidate",
            },
        )

    def test_cli_exports_exact_runtime_identity_without_parsing_shell_output(self) -> None:
        from scripts.abi_staging import cli

        bundle = _active_bundle()
        request = bundle["request"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordination = root / "coordination.json"
            output = root / "github-env"
            coordination.write_bytes(canonical_bytes(bundle))
            output.write_text("", encoding="utf-8")
            status = cli.main(
                [
                    "export-runtime-realm",
                    "--coordination",
                    str(coordination),
                    "--tap-root",
                    str(TAP_ROOT),
                    "--github-env",
                    str(output),
                ]
            )
            observed = output.read_text(encoding="utf-8")
        self.assertEqual(status, 0)
        self.assertEqual(
            observed,
            (
                f"KANDELO_ABI_STAGING_BUILD_POLICY_SHA256="
                f"{request['issuance']['policy_sha256']}\n"
                f"KANDELO_ABI_STAGING_SNAPSHOT_SHA256="
                f"{request['target_abi']['snapshot_sha256']}\n"
                f"KANDELO_ABI_STAGING_SOURCE_TREE="
                f"{request['build_source']['tree']}\n"
                f"KANDELO_ABI_STAGING_TARGET_ABI="
                f"{request['target_abi']['version']}\n"
            ),
        )

    def test_build_work_materializes_only_exact_declared_inputs(self) -> None:
        try:
            execution = importlib.import_module("scripts.abi_staging.execution")
        except ModuleNotFoundError:
            execution = None
        self.assertIsNotNone(execution, "workflow execution support is absent")
        assert execution is not None
        bundle = _active_bundle()
        work = bundle["workflow"]["build_work"][0]
        run = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "workflow_ref": (
                "kandelo-dev/homebrew-tap-core/"
                ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
            ),
            "run_id": 808,
            "run_attempt": 2,
            "job": "build-candidate",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordination = root / "coordination.json"
            coordination.write_bytes(canonical_bytes(bundle))
            loaded = execution.load_coordination_bundle(
                coordination,
                policy=load_tap_staging_policy(
                    TAP_ROOT / "Kandelo/staging/tap-policy.toml"
                ),
            )
            selected = execution.select_build_work(loaded, work["work_id"])
            prepared = execution.prepare_build_inputs(
                loaded,
                selected,
                destination=root / "inputs",
                run=run,
                fetch_candidate=lambda _locator: self.fail(
                    "root Formula unexpectedly fetched a dependency candidate"
                ),
            )
            subject = work["subject"]
            contract = bundle["contracts"][subject]
            contract_sha256 = canonical_sha256(contract)
            self.assertEqual(prepared["formula_plan"]["contract_sha256"], contract_sha256)
            self.assertEqual(
                (root / "inputs/contracts" / f"sha256-{contract_sha256}.json").read_bytes(),
                canonical_bytes(contract),
            )
            self.assertEqual(
                json.loads((root / "inputs/run.json").read_bytes()), run
            )
            self.assertEqual(list((root / "inputs/layers").iterdir()), [])

    def test_unknown_or_mutated_work_id_fails_closed(self) -> None:
        try:
            execution = importlib.import_module("scripts.abi_staging.execution")
        except ModuleNotFoundError:
            execution = None
        self.assertIsNotNone(execution, "workflow execution support is absent")
        assert execution is not None
        bundle = _active_bundle()
        with self.assertRaises(execution.ExecutionError):
            execution.select_build_work(bundle, "0" * 64)
        changed = copy.deepcopy(bundle)
        changed["workflow"]["build_work"][0]["contract_sha256"] = "0" * 64
        with self.assertRaises(Exception):
            execution.select_build_work(
                changed, changed["workflow"]["build_work"][0]["work_id"]
            )

    def test_build_executor_rechecks_sources_and_invokes_only_the_kandelo_adapter(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        bundle = _active_bundle()
        work = bundle["workflow"]["build_work"][0]
        run = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "workflow_ref": (
                "kandelo-dev/homebrew-tap-core/"
                ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
            ),
            "run_id": 808,
            "run_attempt": 2,
            "job": "build-candidate",
        }
        calls = []

        def snapshot(root: Path, repository: str) -> dict[str, str]:
            expected = (
                bundle["tap_plan"]["tap_source"]
                if repository == "kandelo-dev/homebrew-tap-core"
                else bundle["request"]["build_source"]
            )
            return dict(expected)

        def run_process(command, **kwargs):
            calls.append((command, kwargs))
            self.assertEqual(command[0], str(KANDELO_ROOT / "scripts/abi-staging-build-bottle.sh"))
            self.assertEqual(kwargs["cwd"], TAP_ROOT)
            self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
            self.assertNotIn("HOMEBREW_GITHUB_PACKAGES_TOKEN", kwargs["env"])
            self.assertNotIn("ACTIONS_RUNTIME_TOKEN", kwargs["env"])
            self.assertNotIn("GITHUB_ENV", kwargs["env"])
            self.assertNotIn("RENAMED_WRITE_TOKEN", kwargs["env"])
            self.assertNotIn("NIX_CONFIG", kwargs["env"])
            self.assertEqual(kwargs["env"]["CC"], "/declared/cc")
            self.assertEqual(kwargs["env"]["GITHUB_ACTIONS"], "true")
            self.assertNotEqual(kwargs["env"]["HOME"], "/credentialed/home")
            self.assertEqual(
                kwargs["env"]["XDG_CONFIG_HOME"],
                str(Path(kwargs["env"]["HOME"]) / ".config"),
            )
            for flag in (
                "--request",
                "--tap-plan",
                "--formula-plan",
                "--dependency-root",
                "--run",
                "--retry-ordinal",
                "--handoff",
            ):
                self.assertIn(flag, command)
            self.assertEqual(
                command[command.index("--retry-ordinal") + 1],
                str(work["attempt_ordinal"]),
            )
            return SimpleNamespace(returncode=7)

        with tempfile.TemporaryDirectory() as temporary:
            coordination = Path(temporary) / "coordination.json"
            coordination.write_bytes(canonical_bytes(bundle))
            status = execution.execute_build_work(
                coordination_path=coordination,
                work_id=work["work_id"],
                kandelo_root=KANDELO_ROOT,
                tap_root=TAP_ROOT,
                run=run,
                handoff=Path(temporary) / "handoff",
                snapshot_source=snapshot,
                run_process=run_process,
                environment={
                    "PATH": os.environ["PATH"],
                    "CC": "/declared/cc",
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_TOKEN": "must-not-survive",
                    "HOMEBREW_GITHUB_PACKAGES_TOKEN": "must-not-survive",
                    "ACTIONS_RUNTIME_TOKEN": "must-not-survive",
                    "GITHUB_ENV": "/credentialed/github-env",
                    "RENAMED_WRITE_TOKEN": "must-not-survive",
                    "NIX_CONFIG": "access-tokens = github.com=must-not-survive",
                    "HOME": "/credentialed/home",
                },
            )
        self.assertEqual(status, 7)
        self.assertEqual(len(calls), 1)

        with tempfile.TemporaryDirectory() as temporary:
            coordination = Path(temporary) / "coordination.json"
            coordination.write_bytes(canonical_bytes(bundle))
            with self.assertRaises(execution.ExecutionError):
                execution.execute_build_work(
                    coordination_path=coordination,
                    work_id=work["work_id"],
                    kandelo_root=KANDELO_ROOT,
                    tap_root=TAP_ROOT,
                    run=run,
                    handoff=Path(temporary) / "handoff",
                    snapshot_source=lambda _root, repository: {
                        **snapshot(_root, repository),
                        "tree": "0" * 40,
                    },
                    run_process=lambda *_args, **_kwargs: self.fail(
                        "source mismatch reached candidate execution"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
