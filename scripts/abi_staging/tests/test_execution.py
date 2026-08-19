from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shlex
import subprocess
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
    def test_staging_recipe_runner_expands_only_the_source_input_envelope(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "protected-recipe-runner.py"

            execution._prepare_staging_recipe_runner(
                source=KANDELO_ROOT / "scripts/homebrew-tap-recipe-runner.py",
                destination=destination,
            )

            prepared = destination.read_text(encoding="utf-8")
            ast.parse(prepared)
            self.assertIn(
                'SOURCE_INPUT_LIMITS = {\n'
                '    **EXPECTED_LIMITS,\n'
                '    "max_bytes": 4_294_967_296,\n'
                '    "max_entries": 524_288,\n'
                "}",
                prepared,
            )
            self.assertIn(
                'copy_input_tree(request["source_root"], source_root, SOURCE_INPUT_LIMITS)',
                prepared,
            )
            self.assertIn(
                "seal_output_tree(\n"
                "            output_root,\n"
                "            sealed,\n"
                '            request["limits"],',
                prepared,
            )
            self.assertEqual(destination.stat().st_mode & 0o777, 0o500)

    def test_staging_recipe_runner_derives_the_native_llvm_prefix(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "protected-recipe-runner.py"

            execution._prepare_staging_recipe_runner(
                source=KANDELO_ROOT / "scripts/homebrew-tap-recipe-runner.py",
                destination=destination,
            )

            prepared = destination.read_text(encoding="utf-8")
            self.assertIn(
                "def add_runner_owned_platform_environment(\n"
                "    environment: dict[str, str], platform_root: Path, llvm_bin: Path\n"
                ") -> dict[str, str]:",
                prepared,
            )
            self.assertIn(
                'child_environment["LLVM_PREFIX"] = str(llvm_bin.parent)',
                prepared,
            )
            self.assertIn(
                'request["environment"], request["platform_root"], config["llvm_bin"]',
                prepared,
            )

            namespace: dict[str, object] = {"__name__": "staging_recipe_runner_test"}
            exec(compile(prepared, str(destination), "exec"), namespace)
            self.assertEqual(
                namespace["PROTECTED_PUBLISHER_ROOT"],
                Path("/run/kandelo-homebrew-publisher"),
            )
            self.assertIn(
                'config["protected_root"].parent != '
                'Path("/run/kandelo-homebrew-publisher")',
                prepared,
            )
            self.assertNotIn("/var/lib/kandelo-abi-staging", prepared)

    def test_staging_launcher_places_large_recipe_copies_on_runner_disk(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "protected-recipe-runner.py"
            runner.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            runner.chmod(0o500)
            destination = root / "protected-launcher.sh"

            execution._prepare_staging_launcher(
                source=KANDELO_ROOT / "scripts/homebrew-patched-launcher.sh",
                destination=destination,
                protected_recipe_runner=runner,
            )

            prepared = destination.read_text(encoding="utf-8")
            self.assertIn(
                'protected_anchor="/run/kandelo-homebrew-publisher"', prepared
            )
            self.assertIn(
                'protected_parent" != "/run/kandelo-homebrew-publisher"', prepared
            )
            self.assertIn(
                'protected_backing="/var/lib/kandelo-abi-staging"', prepared
            )
            self.assertIn(
                '"$sudo_bin" /usr/bin/mount --bind \\', prepared
            )
            self.assertIn(
                '"$protected_backing" "$protected_anchor"', prepared
            )
            self.assertIn(
                'stat -c \'%d:%i\' "$protected_backing"', prepared
            )
            self.assertIn(
                'stat -c \'%d:%i\' "$protected_anchor"', prepared
            )
            support = (
                TAP_ROOT
                / "Kandelo/formula_support/kandelo_formula_support.rb"
            ).read_text(encoding="utf-8")
            self.assertIn(
                'KANDELO_TAP_RECIPE_PROTECTED_ANCHOR = '
                '"/run/kandelo-homebrew-publisher".freeze',
                support,
            )
            subprocess.run(["bash", "-n", str(destination)], check=True)

    def test_staging_launcher_makes_private_dependency_directories_auditable(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "protected-recipe-runner.py"
            runner.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            runner.chmod(0o500)
            destination = root / "protected-launcher.sh"
            execution._prepare_staging_launcher(
                source=KANDELO_ROOT / "scripts/homebrew-patched-launcher.sh",
                destination=destination,
                protected_recipe_runner=runner,
            )

            prepared = destination.read_text(encoding="utf-8")
            candidate = (
                KANDELO_ROOT / "scripts/homebrew-patched-launcher.sh"
            ).read_text(encoding="utf-8")
            audit_start = candidate.index(
                "homebrew_patched_launcher_assert_target_cellar_links_safe() {"
            )
            audit_end = candidate.index(
                "\nhomebrew_patched_launcher_seal_target_dependencies() {",
                audit_start,
            )
            self.assertIn(candidate[audit_start:audit_end], prepared)
            self.assertIn(str(runner.resolve()), prepared)
            subprocess.run(["bash", "-n", str(destination)], check=True)
            if not Path("/usr/bin/chmod").exists():
                destination.chmod(0o700)
                destination.write_text(
                    prepared.replace("/usr/bin/chmod", "/bin/chmod"),
                    encoding="utf-8",
                )
                destination.chmod(0o500)

            prefix = root / "prefix"
            private = prefix / "Cellar/libiconv/1.19/bin"
            private.mkdir(parents=True)
            for path in (prefix / "Cellar/libiconv", prefix / "Cellar/libiconv/1.19", private):
                path.chmod(0o700)
            sudo = root / "sudo"
            sudo.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "[ \"${1:-}\" != -n ] || shift\n"
                "[ \"${1:-}\" != -- ] || shift\n"
                "if [ \"${1:-}\" = /usr/bin/chmod ]; then\n"
                "  shift\n"
                "  exec chmod \"$@\"\n"
                "fi\n"
                "exec \"$@\"\n",
                encoding="utf-8",
            )
            sudo.chmod(0o500)
            probe = root / "probe.sh"
            probe.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f"source {shlex.quote(str(destination))}\n"
                f"HOMEBREW_PATCHED_PREFIX={shlex.quote(str(prefix))}\n"
                "homebrew_patched_launcher_assert_target_cellar_links_safe() {\n"
                "  [ \"$(python3 -c 'import os,stat,sys;"
                "print(format(stat.S_IMODE(os.stat(sys.argv[1]).st_mode),\"o\"))' "
                "\"$2/libiconv/1.19/bin\")\" = 555 ]\n"
                "  return 73\n"
                "}\n"
                "if homebrew_patched_launcher_seal_target_dependencies "
                f"\"$(id -un)\" {shlex.quote(str(sudo))}; then\n"
                "  exit 99\n"
                "else\n"
                "  [ \"$?\" -eq 73 ]\n"
                "fi\n",
                encoding="utf-8",
            )
            subprocess.run(["bash", str(probe)], check=True)

    def test_staging_builder_overlay_uses_exact_local_dependency_archives(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        source = """#!/usr/bin/env bash
set -euo pipefail
KANDELO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
while IFS= read -r dependency; do
  run_brew_logged run_brew_for_kandelo_bottles "$BREW_BIN" install \\
    --force-bottle \\
    --as-dependency \\
    --ignore-dependencies \\
    --formula "$dependency"
done <"$DEPENDENCY_POUR_LIST"
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate-builder.sh"
            destination = root / "protected-builder.sh"
            candidate.write_text(source, encoding="utf-8")

            execution._prepare_staging_normal_builder(
                source=candidate,
                destination=destination,
            )

            prepared = destination.read_text(encoding="utf-8")
            self.assertIn(
                'KANDELO_ROOT="${KANDELO_ABI_STAGING_CANDIDATE_ROOT:?}"',
                prepared,
            )
            self.assertIn('"$dependency_archive"', prepared)
            self.assertNotIn(execution._FORMULA_DEPENDENCY_INSTALL, prepared)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o500)

    def test_staging_verifier_augments_info_without_rewriting_the_formula(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        formula_info_capture = (
            '"$BREW_BIN" info --json=v2 "$FORMULA_REF" >"$FORMULA_INFO"'
        )
        source = f'''#!/usr/bin/env bash
set -euo pipefail
KANDELO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
while IFS= read -r dependency; do
  run_brew_logged run_brew_for_kandelo_bottles "$BREW_BIN" install \\
    --force-bottle --as-dependency --ignore-dependencies --formula "$dependency"
done <"$DEPENDENCY_POUR_LIST"
{formula_info_capture}
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate-verifier.sh"
            destination = root / "protected-verifier.sh"
            candidate.write_text(source, encoding="utf-8")

            execution._prepare_staging_normal_builder(
                source=candidate,
                destination=destination,
                root_assignment=execution._CANDIDATE_VERIFIER_ROOT_ASSIGNMENT,
                dependency_install=execution._VERIFIER_FORMULA_DEPENDENCY_INSTALL,
                formula_info_capture=formula_info_capture,
            )

            prepared = destination.read_text(encoding="utf-8")
            self.assertIn('staging_formula_info="$FORMULA_INFO.staging"', prepared)
            self.assertIn(".[0].value.bottle.rebuild", prepared)
            self.assertIn('sha256: $sha256, url: $url', prepared)
            self.assertIn('mv "$staging_formula_info" "$FORMULA_INFO"', prepared)
            subprocess.run(["bash", "-n", str(destination)], check=True)
            brew = root / "brew"
            brew.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' "
                "'{\"casks\":[],\"formulae\":[{\"bottle\":{},\"name\":\"mini-tool\"}]}'\n",
                encoding="utf-8",
            )
            brew.chmod(0o500)
            dependencies = root / "dependencies.txt"
            dependencies.write_text("", encoding="utf-8")
            bottle_json = root / "bottle.json"
            bottle_json.write_text(
                json.dumps(
                    {
                        "kandelo-dev/tap-core/mini-tool": {
                            "bottle": {"rebuild": 7}
                        }
                    }
                ),
                encoding="utf-8",
            )
            formula_info = root / "formula-info.json"
            subprocess.run(
                ["bash", str(destination)],
                check=True,
                env={
                    **os.environ,
                    "BOTTLE_JSON": str(bottle_json),
                    "BOTTLE_SHA256": "a" * 64,
                    "BOTTLE_TAG": "wasm32_kandelo",
                    "BOTTLE_URL": (
                        "https://example.invalid/mini-tool/blobs/sha256:" + "a" * 64
                    ),
                    "BREW_BIN": str(brew),
                    "DEPENDENCY_POUR_LIST": str(dependencies),
                    "FORMULA_INFO": str(formula_info),
                    "FORMULA_REF": "kandelo-dev/tap-core/mini-tool",
                    "KANDELO_ABI_STAGING_CANDIDATE_ROOT": str(root),
                    "LOCAL_DEPENDENCIES_JSON": str(root / "unused.json"),
                },
            )
            stable = json.loads(formula_info.read_bytes())["formulae"][0]["bottle"][
                "stable"
            ]
            self.assertEqual(
                stable,
                {
                    "files": {
                        "wasm32_kandelo": {
                            "sha256": "a" * 64,
                            "url": "https://example.invalid/mini-tool/blobs/sha256:"
                            + "a" * 64,
                        }
                    },
                    "rebuild": 7,
                },
            )
            self.assertEqual(destination.stat().st_mode & 0o777, 0o500)

    def test_coordination_loader_uses_the_declared_large_item_bound(self) -> None:
        from scripts.abi_staging import execution

        value = {
            "mode": "active",
            "records": [{"index": index} for index in range(50_001)],
        }
        body = canonical_bytes(value, maximum_items=200_010)
        policy = load_tap_staging_policy(
            TAP_ROOT / "Kandelo/staging/tap-policy.toml"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "coordination.json"
            path.write_bytes(body)
            with patch.object(execution, "validate_coordination_bundle"):
                self.assertEqual(
                    execution.load_coordination_bundle(path, policy=policy),
                    value,
                )

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

    def test_cli_exports_one_exact_candidate_build_realm(self) -> None:
        from scripts.abi_staging import cli

        bundle = _active_bundle()
        work = bundle["workflow"]["build_work"][0]
        formula = next(
            candidate
            for candidate in bundle["tap_plan"]["formulae"]
            if candidate["identity"]["name"] == "libcxx"
            and candidate["identity"]["architecture"] == "wasm32"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordination = root / "coordination.json"
            output = root / "github-env"
            coordination.write_bytes(canonical_bytes(bundle))
            output.write_text("", encoding="utf-8")
            status = cli.main(
                [
                    "export-build-realm",
                    "--coordination",
                    str(coordination),
                    "--work-id",
                    work["work_id"],
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
                "KANDELO_ABI_STAGING_ARCHITECTURE="
                f"{formula['identity']['architecture']}\n"
                "KANDELO_ABI_STAGING_FORMULA="
                f"{formula['identity']['name']}\n"
                "KANDELO_ABI_STAGING_TAP_COMMIT="
                f"{bundle['tap_plan']['tap_source']['commit']}\n"
                "KANDELO_ABI_STAGING_TAP_NAME=kandelo-dev/tap-core\n"
                "KANDELO_ABI_STAGING_TAP_REPOSITORY="
                f"{bundle['tap_plan']['tap_source']['repository']}\n"
            ),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordination = root / "coordination.json"
            output = root / "github-env"
            coordination.write_bytes(canonical_bytes(bundle))
            output.write_text("", encoding="utf-8")
            status = cli.main(
                [
                    "export-build-realm",
                    "--coordination",
                    str(coordination),
                    "--work-id",
                    "0" * 64,
                    "--tap-root",
                    str(TAP_ROOT),
                    "--github-env",
                    str(output),
                ]
            )
            self.assertEqual(output.read_text(encoding="utf-8"), "")
        self.assertEqual(status, 1)

    def test_cli_exports_one_exact_candidate_verification_realm(self) -> None:
        from scripts.abi_staging import cli

        bundle = _active_bundle()
        work = bundle["workflow"]["build_work"][0]
        subject = json.loads(work["subject"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            coordination = root / "coordination.json"
            output = root / "github-env"
            coordination.write_bytes(canonical_bytes(bundle))
            output.write_text("", encoding="utf-8")
            with patch.object(
                cli, "select_verification_work", return_value=work
            ):
                status = cli.main(
                    [
                        "export-verification-realm",
                        "--coordination",
                        str(coordination),
                        "--work-id",
                        work["work_id"],
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
                "KANDELO_ABI_STAGING_ARCHITECTURE="
                f"{subject['architecture']}\n"
                "KANDELO_ABI_STAGING_FORMULA="
                f"{subject['identity']}\n"
                "KANDELO_ABI_STAGING_TAP_COMMIT="
                f"{bundle['tap_plan']['tap_source']['commit']}\n"
                "KANDELO_ABI_STAGING_TAP_NAME=kandelo-dev/tap-core\n"
                "KANDELO_ABI_STAGING_TAP_REPOSITORY="
                f"{bundle['tap_plan']['tap_source']['repository']}\n"
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

    def test_build_dependency_closure_includes_transitive_candidate_layers(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        dash_layer = {
            "bytes": 17,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                "dash@sha256:" + "d" * 64
            ),
            "sha256": "d" * 64,
        }
        ed_layer = {
            "bytes": 19,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                "ed@sha256:" + "e" * 64
            ),
            "sha256": "e" * 64,
        }
        dash = {
            "candidate": {
                "bottle_layer": dash_layer,
                "direct_dependency_layers": [],
                "formula": {"architecture": "wasm32", "formula": "dash"},
            }
        }
        ed = {
            "candidate": {
                "bottle_layer": ed_layer,
                "direct_dependency_layers": [
                    {"artifact": dash_layer, "id": "dash-wasm32"}
                ],
                "formula": {"architecture": "wasm32", "formula": "ed"},
            }
        }
        contract = {
            "direct_dependencies": [
                {
                    "architecture": "wasm32",
                    "bottle_layer_bytes": ed_layer["bytes"],
                    "bottle_layer_sha256": ed_layer["sha256"],
                    "formula": "ed",
                    "materialization_policy_sha256": "f" * 64,
                }
            ]
        }
        bundle = {
            "candidates": {
                "locators": {
                    "1" * 64: {"digest": "sha256:" + "1" * 64},
                    "2" * 64: {"digest": "sha256:" + "2" * 64},
                },
                "records": {"1" * 64: ed, "2" * 64: dash},
            }
        }

        with patch.object(
            execution,
            "_matching_dependency",
            return_value=(ed, bundle["candidates"]["locators"]["1" * 64]),
        ), patch.object(
            execution,
            "_dependency_candidate",
            return_value=(
                "2" * 64,
                dash,
                bundle["candidates"]["locators"]["2" * 64],
            ),
        ):
            closure = execution._build_dependency_closure(bundle, contract)

        self.assertEqual(
            [entry["formula"] for entry, _record, _locator in closure],
            ["dash", "ed"],
        )
        self.assertEqual(
            [entry["bottle_layer_sha256"] for entry, _record, _locator in closure],
            [dash_layer["sha256"], ed_layer["sha256"]],
        )

    def test_reused_dependency_resolves_to_its_exact_original_candidate(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        bottle = b"reused dependency bottle\n"
        bottle_sha256 = hashlib.sha256(bottle).hexdigest()
        repository = "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/dash"
        layer = {
            "bytes": len(bottle),
            "immutable_reference": f"{repository}@sha256:{bottle_sha256}",
            "sha256": bottle_sha256,
        }
        original = {
            "candidate": {
                "bottle_layer": layer,
                "formula": {
                    "architecture": "wasm32",
                    "bottle_contract_sha256": "b" * 64,
                    "formula": "dash",
                    "tap": "kandelo-dev/homebrew-tap-core",
                    "target_abi": 8,
                },
            },
            "common": {"request_sha256": "1" * 64},
        }
        original_sha256 = "2" * 64
        locator = {
            "digest": f"sha256:{original_sha256}",
            "immutable_reference": f"{repository}@sha256:{original_sha256}",
            "repository": repository,
        }
        reuse = {
            "candidate_reuse": {
                "bottle_layer": layer,
                "existing_candidate": {
                    "immutable_reference": locator["immutable_reference"],
                    "record_sha256": original_sha256,
                },
                "formula": {
                    "architecture": "wasm32",
                    "bottle_contract_sha256": "b" * 64,
                    "formula": "dash",
                    "tap": "kandelo-dev/homebrew-tap-core",
                    "target_abi": 8,
                },
            },
            "common": {"request_sha256": "a" * 64},
        }
        bundle = {
            "request_sha256": "a" * 64,
            "tap_plan": {
                "formulae": [
                    {
                        "contract_sha256": "b" * 64,
                        "identity": {"architecture": "wasm32", "name": "dash"},
                    }
                ],
                "tap_source": {"repository": "kandelo-dev/homebrew-tap-core"},
                "target_abi": {"version": 8},
            },
            "candidates": {
                "locators": {original_sha256: locator},
                "records": {original_sha256: original},
            },
            "reuse_bindings": {
                "locators": {},
                "records": {canonical_sha256(reuse): reuse},
            },
        }
        dependency = {
            "architecture": "wasm32",
            "bottle_layer_bytes": len(bottle),
            "bottle_layer_sha256": bottle_sha256,
            "formula": "dash",
            "materialization_policy_sha256": "f" * 64,
        }

        record, selected_locator = execution._matching_dependency(bundle, dependency)

        self.assertEqual(record, original)
        self.assertEqual(selected_locator, locator)
        fetched = SimpleNamespace(
            artifact_type=execution.CANDIDATE_RECORD_MEDIA_TYPE,
            digest=locator["digest"],
            immutable_reference=locator["immutable_reference"],
            config=SimpleNamespace(body=canonical_bytes(original)),
            layers=(
                SimpleNamespace(
                    body=bottle,
                    digest=f"sha256:{bottle_sha256}",
                    role="bottle-layer",
                ),
            ),
        )

        self.assertEqual(
            execution._fetched_layer(
                fetched,
                record=record,
                locator=selected_locator,
                dependency=dependency,
            ),
            bottle,
        )

    def test_current_dependency_wins_over_stale_same_layer_reuse(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        bottle = b"shared dependency bottle\n"
        bottle_sha256 = hashlib.sha256(bottle).hexdigest()
        repository = "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/ed"
        layer = {
            "bytes": len(bottle),
            "immutable_reference": f"{repository}@sha256:{bottle_sha256}",
            "sha256": bottle_sha256,
        }
        historical_sha256 = "1" * 64
        current_sha256 = "f" * 64
        historical = {
            "candidate": {
                "bottle_layer": layer,
                "formula": {
                    "architecture": "wasm32",
                    "bottle_contract_sha256": "b" * 64,
                    "formula": "ed",
                    "tap": "kandelo-dev/homebrew-tap-core",
                    "target_abi": 8,
                },
            },
            "common": {"request_sha256": "0" * 64},
        }
        current = copy.deepcopy(historical)
        current["common"]["request_sha256"] = "a" * 64
        current["candidate"]["formula"]["bottle_contract_sha256"] = "c" * 64
        historical_locator = {
            "digest": f"sha256:{historical_sha256}",
            "immutable_reference": f"{repository}@sha256:{historical_sha256}",
            "repository": repository,
        }
        current_locator = {
            "digest": f"sha256:{current_sha256}",
            "immutable_reference": f"{repository}@sha256:{current_sha256}",
            "repository": repository,
        }
        stale_reuse = {
            "candidate_reuse": {
                "bottle_layer": layer,
                "existing_candidate": {
                    "immutable_reference": historical_locator["immutable_reference"],
                    "record_sha256": historical_sha256,
                },
                "formula": historical["candidate"]["formula"],
            },
            "common": {"request_sha256": "a" * 64},
        }
        bundle = {
            "request_sha256": "a" * 64,
            "tap_plan": {
                "formulae": [
                    {
                        "contract_sha256": "c" * 64,
                        "identity": {"architecture": "wasm32", "name": "ed"},
                    }
                ],
                "tap_source": {"repository": "kandelo-dev/homebrew-tap-core"},
                "target_abi": {"version": 8},
            },
            "candidates": {
                "locators": {
                    historical_sha256: historical_locator,
                    current_sha256: current_locator,
                },
                "records": {
                    historical_sha256: historical,
                    current_sha256: current,
                },
            },
            "reuse_bindings": {
                "locators": {},
                "records": {canonical_sha256(stale_reuse): stale_reuse},
            },
        }
        dependency = {
            "architecture": "wasm32",
            "bottle_layer_bytes": len(bottle),
            "bottle_layer_sha256": bottle_sha256,
            "formula": "ed",
        }

        record, locator = execution._matching_dependency(bundle, dependency)

        self.assertEqual(record, current)
        self.assertEqual(locator, current_locator)

    def test_transitive_reuse_ignores_same_layer_historical_candidate(self) -> None:
        execution = importlib.import_module("scripts.abi_staging.execution")
        bottle = b"reused dependency bottle\n"
        bottle_sha256 = hashlib.sha256(bottle).hexdigest()
        repository = "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/dash"
        layer = {
            "bytes": len(bottle),
            "immutable_reference": f"{repository}@sha256:{bottle_sha256}",
            "sha256": bottle_sha256,
        }
        current = {
            "candidate": {
                "bottle_layer": layer,
                "direct_dependency_layers": [],
                "formula": {
                    "architecture": "wasm32",
                    "bottle_contract_sha256": "b" * 64,
                    "formula": "dash",
                    "tap": "kandelo-dev/homebrew-tap-core",
                    "target_abi": 8,
                },
            },
            "common": {"request_sha256": "1" * 64},
        }
        historical = copy.deepcopy(current)
        historical["common"]["request_sha256"] = "0" * 64
        historical["candidate"]["formula"]["bottle_contract_sha256"] = "c" * 64
        historical_sha256 = "1" * 64
        current_sha256 = "2" * 64
        historical_locator = {
            "digest": f"sha256:{historical_sha256}",
            "immutable_reference": f"{repository}@sha256:{historical_sha256}",
            "repository": repository,
        }
        current_locator = {
            "digest": f"sha256:{current_sha256}",
            "immutable_reference": f"{repository}@sha256:{current_sha256}",
            "repository": repository,
        }
        reuse = {
            "candidate_reuse": {
                "bottle_layer": layer,
                "existing_candidate": {
                    "immutable_reference": current_locator["immutable_reference"],
                    "record_sha256": current_sha256,
                },
                "formula": current["candidate"]["formula"],
            },
            "common": {"request_sha256": "a" * 64},
        }
        bundle = {
            "request_sha256": "a" * 64,
            "tap_plan": {
                "formulae": [
                    {
                        "contract_sha256": "b" * 64,
                        "identity": {"architecture": "wasm32", "name": "dash"},
                    }
                ],
                "tap_source": {"repository": "kandelo-dev/homebrew-tap-core"},
                "target_abi": {"version": 8},
            },
            "candidates": {
                "locators": {
                    historical_sha256: historical_locator,
                    current_sha256: current_locator,
                },
                "records": {
                    historical_sha256: historical,
                    current_sha256: current,
                },
            },
            "reuse_bindings": {
                "locators": {},
                "records": {canonical_sha256(reuse): reuse},
            },
        }

        with patch.object(
            execution,
            "_candidate_entry",
            side_effect=lambda _bundle, digest: (
                bundle["candidates"]["records"][digest],
                bundle["candidates"]["locators"][digest],
            ),
        ):
            selected_sha256, record, locator = execution._dependency_candidate(
                bundle,
                {"artifact": layer, "id": "dash-wasm32"},
            )

        self.assertEqual(selected_sha256, current_sha256)
        self.assertEqual(record, current)
        self.assertEqual(locator, current_locator)

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
            self.assertEqual(
                kwargs["env"]["KANDELO_ABI_STAGING_CANDIDATE_ROOT"],
                str(KANDELO_ROOT.resolve()),
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_ABI_STAGING_PROTECTED_NORMAL_BUILDER"],
                "1",
            )
            self.assertEqual(
                kwargs["env"]["HOMEBREW_NO_REQUIRE_TAP_TRUST"],
                "1",
            )
            self.assertNotIn("KANDELO_ABI_STAGING_TESTING", kwargs["env"])
            protected_builder = Path(
                kwargs["env"]["KANDELO_ABI_STAGING_NORMAL_BUILDER"]
            )
            protected_launcher = Path(
                kwargs["env"]["KANDELO_ABI_STAGING_PROTECTED_LAUNCHER"]
            )
            protected_runner = Path(
                kwargs["env"]["KANDELO_ABI_STAGING_PROTECTED_RECIPE_RUNNER"]
            )
            self.assertTrue(protected_launcher.is_file())
            self.assertTrue(protected_runner.is_file())
            self.assertTrue(protected_builder.is_file())
            self.assertIn(
                '"$dependency_archive"',
                protected_builder.read_text(encoding="utf-8"),
            )
            self.assertIn(
                '. "${KANDELO_ABI_STAGING_PROTECTED_LAUNCHER:?}"',
                protected_builder.read_text(encoding="utf-8"),
            )
            self.assertIn(
                str(protected_runner),
                protected_launcher.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                kwargs["env"]["HOMEBREW_BREW_FILE"], "/reviewed/brew/bin/brew"
            )
            self.assertEqual(kwargs["env"]["HOMEBREW_BREW_COMMIT"], "a" * 40)
            self.assertEqual(kwargs["env"]["HOMEBREW_CACHE"], "/private/brew-cache")
            self.assertEqual(kwargs["env"]["HOMEBREW_TEMP"], "/private/brew-temp")
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_RESOLVED_TAPS_FILE"],
                "/protected/resolved-taps.json",
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_TAP_SOURCE_COMMIT"], "b" * 40
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_BUILD_USER"],
                "kandelo-homebrew-build",
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_RECIPE_USER"],
                "kandelo-homebrew-recipe",
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_SHARED_TEMP"],
                "/tmp/kandelo-homebrew.fixture",
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_SUDO_BIN"], "/usr/bin/sudo"
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_SYSTEMD_RUN_BIN"],
                "/usr/bin/systemd-run",
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_SYSTEMCTL_BIN"],
                "/usr/bin/systemctl",
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_GETENT_BIN"],
                "/usr/bin/getent",
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_PGREP_BIN"],
                "/usr/bin/pgrep",
            )
            self.assertEqual(
                kwargs["env"]["KANDELO_HOMEBREW_PKILL_BIN"],
                "/usr/bin/pkill",
            )
            self.assertEqual(
                kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"],
                "/private/playwright",
            )
            self.assertEqual(
                kwargs["env"]["WASM_POSIX_BINARY_CACHE_ROOT"],
                "/private/package-cache",
            )
            self.assertEqual(
                kwargs["env"]["WASM_POSIX_XTASK_BIN"],
                "/protected/candidate/target/host/release/xtask",
            )
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
                    "HOMEBREW_BREW_FILE": "/reviewed/brew/bin/brew",
                    "HOMEBREW_BREW_COMMIT": "a" * 40,
                    "HOMEBREW_CACHE": "/private/brew-cache",
                    "HOMEBREW_TEMP": "/private/brew-temp",
                    "HOMEBREW_NO_AUTO_UPDATE": "1",
                    "HOMEBREW_NO_INSTALL_CLEANUP": "1",
                    "HOMEBREW_NO_ANALYTICS": "1",
                    "HOMEBREW_DEVELOPER": "1",
                    "KANDELO_HOMEBREW_RESOLVED_TAPS_FILE": (
                        "/protected/resolved-taps.json"
                    ),
                    "KANDELO_HOMEBREW_TAP_SOURCE_COMMIT": "b" * 40,
                    "KANDELO_HOMEBREW_BUILD_USER": "kandelo-homebrew-build",
                    "KANDELO_HOMEBREW_RECIPE_USER": "kandelo-homebrew-recipe",
                    "KANDELO_HOMEBREW_SHARED_TEMP": (
                        "/tmp/kandelo-homebrew.fixture"
                    ),
                    "KANDELO_HOMEBREW_SUDO_BIN": "/usr/bin/sudo",
                    "KANDELO_HOMEBREW_SYSTEMD_RUN_BIN": "/usr/bin/systemd-run",
                    "KANDELO_HOMEBREW_SYSTEMCTL_BIN": "/usr/bin/systemctl",
                    "KANDELO_HOMEBREW_GETENT_BIN": "/usr/bin/getent",
                    "KANDELO_HOMEBREW_PGREP_BIN": "/usr/bin/pgrep",
                    "KANDELO_HOMEBREW_PKILL_BIN": "/usr/bin/pkill",
                    "PLAYWRIGHT_BROWSERS_PATH": "/private/playwright",
                    "WASM_POSIX_BINARY_CACHE_ROOT": "/private/package-cache",
                    "WASM_POSIX_XTASK_BIN": (
                        "/protected/candidate/target/host/release/xtask"
                    ),
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
