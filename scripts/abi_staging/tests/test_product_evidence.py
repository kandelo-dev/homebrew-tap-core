from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.abi_staging import product_evidence as product_evidence_module
from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cli import main as cli_main
from scripts.abi_staging.oci import build_oci_manifest, fetch_public_record
from scripts.abi_staging.plan import exact_formula_subject
from scripts.abi_staging.product import CandidateProductArtifactV1, ProductInputPlanV1
from scripts.abi_staging.product_evidence import (
    PRODUCT_CANDIDATE_MEDIA_TYPE,
    CandidateProductLocatorV1,
    ProductEvidenceError,
    build_candidate_product_oci_plan,
    build_product_evidence_oci_plan,
    build_product_evidence_record,
    build_product_evidence_receipt,
    build_product_evidence_receipt_oci_plan,
    candidate_product_repository,
    inspect_product_evidence_repository,
    load_candidate_product_locator,
    publish_candidate_product,
    publish_product_evidence_record,
    publish_product_evidence_receipt,
    runtime_evidence_identity,
    validate_product_evidence_record,
    validate_product_evidence_receipt,
    validate_product_evidence_result,
)
from scripts.abi_staging.records import (
    TapRecordError,
    validate_formula_candidate_repository,
)
from scripts.abi_staging.reconcile import (
    PullRequestLifecycleV1,
    ReconciliationDecisionV1,
    product_evidence_work_scope,
)
from scripts.abi_staging.tests.test_oci import FakeRegistryTransport


TAP_ROOT = Path(__file__).resolve().parents[3]
INPUT_FIXTURE = TAP_ROOT / "Kandelo/staging/fixtures/product/resolved-inputs.json"
REPORT_FIXTURE = TAP_ROOT / "Kandelo/staging/fixtures/product/builder-report.json"
EVIDENCE_FIXTURE = TAP_ROOT / "Kandelo/staging/fixtures/product/evidence-record.json"
SOURCE_ASSOCIATION = "kandelo-dev/homebrew-tap-core"


def _sha(value: bytes | str) -> str:
    body = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(body).hexdigest()


def _artifact(reference_root: str, identity: str, size: int = 71) -> dict[str, object]:
    digest = _sha(identity)
    return {
        "sha256": digest,
        "bytes": size,
        "immutable_reference": f"{reference_root}@sha256:{digest}",
    }


def _report(inputs: dict[str, object], vfs: bytes) -> dict[str, object]:
    report_inputs = []
    for item in inputs["inputs"]:
        report_item = {
            "bytes": item["bytes"],
            "id": item["id"],
            "kind": item["kind"],
            "placement": item["effective_materialization"],
            "role": item["role"],
            "sha256": item["sha256"],
        }
        if "descriptor" in item:
            report_item["descriptor"] = {
                "bytes": item["descriptor"]["bytes"],
                "sha256": item["descriptor"]["sha256"],
            }
        report_inputs.append(report_item)
    return {
        "capture": {"complete": True, "unreported_reads": []},
        "inputs": report_inputs,
        "kind": "kandelo-vfs-builder-report",
        "output": {
            "abi": copy.deepcopy(inputs["target_abi"]),
            "bytes": len(vfs),
            "name": inputs["product"]["output"],
            "path": inputs["product"]["output"],
            "sha256": _sha(vfs),
        },
        "product": copy.deepcopy(inputs["product"]),
        "resolved_inputs_sha256": canonical_sha256(inputs),
        "schema": 1,
    }


def _runtime(inputs: dict[str, object]) -> tuple[dict[str, object], dict[str, bytes]]:
    files = {
        "browser/dist/abi-staging/browser-host.js": b"exact browser host\n",
        "browser/dist/abi-staging-harness/index.html": b"protected harness\n",
        "browser/dist/assets/kernel-fixture.wasm": b"\x00asm miniature kernel\n",
        "browser/dist/bundle.js": b"browser bundle\n",
        "browser/dist/service-worker.js": b"service worker\n",
        "host/dist/bundle.js": b"host runtime bundle\n",
        "host/generated-abi.ts": b"generated ABI\n",
        "host/worker-protocol.ts": b"worker protocol\n",
        "kernel.wasm": b"\x00asm miniature kernel\n",
        # An exact musl sysroot contains intentional empty architecture
        # headers. Their path and digest remain inventory-bound.
        "toolchain/wasm32-sysroot/include/bits/ioctl_fix.h": b"",
    }
    inventory = [
        {"path": path, "sha256": _sha(body), "bytes": len(body)}
        for path, body in sorted(files.items())
    ]
    host_inventory = [item for item in inventory if item["path"].startswith("host/")]
    browser_inventory = [
        item for item in inventory if item["path"].startswith("browser/")
    ]
    runtime = {
        "schema": 1,
        "kind": "kandelo-exact-runtime-bundle",
        "source": copy.deepcopy(inputs["source"]),
        "target_abi": copy.deepcopy(inputs["target_abi"]),
        "kernel": {
            "wasm_sha256": _sha(files["kernel.wasm"]),
            "bytes": len(files["kernel.wasm"]),
            "abi_version": inputs["target_abi"]["version"],
            "snapshot_sha256": inputs["target_abi"]["snapshot_sha256"],
        },
        "host": {
            "bundle_sha256": _sha(canonical_bytes(host_inventory)),
            "bytes": sum(item["bytes"] for item in host_inventory),
            "generated_abi_sha256": _sha(files["host/generated-abi.ts"]),
            "worker_protocol_sha256": _sha(files["host/worker-protocol.ts"]),
        },
        "browser": {
            "bundle_sha256": _sha(canonical_bytes(browser_inventory)),
            "bytes": sum(item["bytes"] for item in browser_inventory),
            "harness_entry_bytes": len(
                files["browser/dist/abi-staging-harness/index.html"]
            ),
            "harness_entry_path": "browser/dist/abi-staging-harness/index.html",
            "harness_entry_sha256": _sha(
                files["browser/dist/abi-staging-harness/index.html"]
            ),
            "host_entry_bytes": len(
                files["browser/dist/abi-staging/browser-host.js"]
            ),
            "host_entry_path": "browser/dist/abi-staging/browser-host.js",
            "host_entry_sha256": _sha(
                files["browser/dist/abi-staging/browser-host.js"]
            ),
            "kernel_asset_path": "browser/dist/assets/kernel-fixture.wasm",
            "kernel_asset_sha256": _sha(
                files["browser/dist/assets/kernel-fixture.wasm"]
            ),
            "service_worker_sha256": _sha(files["browser/dist/service-worker.js"]),
        },
        "build_policy_sha256": inputs["build_environment"]["policy_sha256"],
        "inventory": inventory,
    }
    return runtime, files


def _run(job_id: str = "node-product-evidence") -> dict[str, object]:
    return {
        "repository": SOURCE_ASSOCIATION,
        "workflow_ref": (
            "kandelo-dev/homebrew-tap-core/.github/workflows/"
            "abi-staging-reconcile.yml@" + "9" * 40
        ),
        "run_id": 717,
        "job_id": job_id,
        "attempt": 1,
    }


def _record_run() -> dict[str, object]:
    run = _run("publish-product-evidence")
    return {
        "repository": run["repository"],
        "workflow_ref": run["workflow_ref"],
        "run_id": run["run_id"],
        "run_attempt": run["attempt"],
        "job": run["job_id"],
    }


class ProductEvidenceFixture:
    def setUp(self) -> None:
        self.inputs = json.loads(INPUT_FIXTURE.read_bytes())
        self.vfs = b"miniature ABI-bound VFS image\n"
        self.report = _report(self.inputs, self.vfs)
        self.runtime, self.runtime_files = _runtime(self.inputs)
        product = self.inputs["product"]
        self.input_plan = ProductInputPlanV1(
            product_id=product["id"],
            manifest_path=product["manifest_path"],
            manifest_sha256=product["manifest_sha256"],
            architecture=product["architecture"],
            reference_class="candidate",
            resolved_inputs_sha256=canonical_sha256(self.inputs),
            dependency_product_ids=("base",),
            required_formula_subjects=(exact_formula_subject("mini", "wasm32"),),
            runtime_bundle_sha256=canonical_sha256(self.runtime),
        )
        self.repository = candidate_product_repository(
            owner="kandelo-dev",
            repository_prefix="homebrew-tap-core-abi-",
            candidate_suffix="-candidates",
            target_abi=self.inputs["target_abi"]["version"],
            product_id=self.inputs["product"]["id"],
        )
        self.plan = build_candidate_product_oci_plan(
            repository=self.repository,
            publisher_repository=SOURCE_ASSOCIATION,
            input_plan=self.input_plan,
            vfs_body=self.vfs,
            builder_report_body=canonical_bytes(self.report),
            resolved_inputs_body=canonical_bytes(self.inputs),
            runtime_bundle_body=canonical_bytes(self.runtime),
            runtime_files=self.runtime_files,
        )
        manifest = build_oci_manifest(self.plan)
        digest = "sha256:" + _sha(manifest)
        self.locator = CandidateProductLocatorV1(
            product_id=self.inputs["product"]["id"],
            repository="ghcr.io/" + self.repository,
            manifest_digest=digest,
            immutable_reference=f"ghcr.io/{self.repository}@{digest}",
            vfs_layer_sha256=_sha(self.vfs),
            vfs_layer_bytes=len(self.vfs),
            builder_report_sha256=canonical_sha256(self.report),
        )
        self.request_digest = "a" * 64
        self.registries = [
            {
                "kind": "pages",
                "path": "apps/browser-demos/vfs-products.toml",
                "sha256": "b" * 64,
            },
            {
                "kind": "tests",
                "path": "abi/staging/test-products.toml",
                "sha256": "c" * 64,
            },
        ]

    def requirement(
        self,
        host: str,
        definition_id: str,
        applicability: str = "required",
    ) -> dict[str, object]:
        return {
            "host": host,
            "id": definition_id,
            "definition_sha256": _sha(f"definition:{definition_id}"),
            "applicability": applicability,
        }

    def result(
        self,
        requirement: dict[str, object],
        *,
        outcome: str = "success",
    ) -> dict[str, object]:
        guard_codes = {
            "success": [],
            "failure": ["verification_failed"],
            "timeout": ["verification_timeout"],
        }[outcome]
        return {
            "schema": 1,
            "kind": "kandelo-vfs-product-evidence-result",
            "request_digest": self.request_digest,
            "product": {
                "id": self.inputs["product"]["id"],
                "manifest_sha256": self.inputs["product"]["manifest_sha256"],
            },
            "candidate_product": self.locator.evidence_identity(),
            "runtime": runtime_evidence_identity(canonical_bytes(self.runtime)),
            "host": requirement["host"],
            "definition": {
                "id": requirement["id"],
                "definition_sha256": requirement["definition_sha256"],
            },
            "outcome": outcome,
            "guard_codes": guard_codes,
            "bounded_diagnostics": [],
            "run": _run(f"{requirement['host']}-product-evidence"),
        }

    def receipt(
        self,
        requirement: dict[str, object],
        *,
        outcome: str = "success",
        override: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return build_product_evidence_receipt(
            self.result(requirement, outcome=outcome),
            request_digest=self.request_digest,
            product=self.inputs["product"],
            candidate_product=self.locator,
            runtime_bundle_body=canonical_bytes(self.runtime),
            requirement=requirement,
            accepted_override=override,
            expected_override_policy=(
                None if override is None else override["record"]["policy"]
            ),
        )

    def override_for(self, result: dict[str, object]) -> dict[str, object]:
        result_sha256 = canonical_sha256(result)
        record = {
            "schema": 1,
            "kind": "kandelo-vfs-product-evidence-override",
            "request_digest": result["request_digest"],
            "subject_result_sha256": result_sha256,
            "product": copy.deepcopy(result["product"]),
            "candidate_product": copy.deepcopy(result["candidate_product"]),
            "host": result["host"],
            "definition": copy.deepcopy(result["definition"]),
            "outcome": result["outcome"],
            "accepted_guard_codes": list(result["guard_codes"]),
            "maintainer": {
                "login": "fixture-maintainer",
                "permission": "maintain",
                "authorization_reference": (
                    "https://github.com/kandelo-dev/homebrew-tap-core/"
                    "issues/1#issuecomment-1"
                ),
            },
            "justification": "Accept this exact failed miniature evidence result.",
            "policy": {
                "policy_version": 1,
                "policy_sha256": "d" * 64,
                "guard_registry_version": 1,
                "guard_registry_sha256": "e" * 64,
            },
            "run": _record_run(),
        }
        record_sha256 = canonical_sha256(record)
        return {
            "record_sha256": record_sha256,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                f"products/mini-shell/overrides@sha256:{record_sha256}"
            ),
            "record": record,
        }

    def record(
        self,
        requirements: list[dict[str, object]],
        receipts: list[dict[str, object]],
    ) -> dict[str, object]:
        return build_product_evidence_record(
            request_digest=self.request_digest,
            candidate_product=self.locator,
            resolved_inputs_body=canonical_bytes(self.inputs),
            builder_report_body=canonical_bytes(self.report),
            runtime_bundle_body=canonical_bytes(self.runtime),
            selecting_registries=self.registries,
            requirements=requirements,
            receipts=receipts,
            run=_record_run(),
        )


class ProductEvidenceContextTests(ProductEvidenceFixture, unittest.TestCase):
    def test_cli_routes_protected_product_evidence_publication_scope(self) -> None:
        from scripts.abi_staging import cli

        with patch.object(
            cli,
            "_publish_workflow_product_evidence",
            return_value=None,
            create=True,
        ) as publish:
            status = cli_main(
                [
                    "publish-workflow-product-evidence",
                    "--run-id",
                    "717",
                    "--run-attempt",
                    "2",
                    "--head-sha",
                    "a" * 40,
                    "--product-id",
                    "mini-shell",
                    "--product-work-id",
                    "c" * 64,
                    "--work-id",
                    "b" * 64,
                    "--kandelo-root",
                    "/tmp/exact-candidate",
                    "--kandelo-policy-root",
                    "/tmp/exact-policy",
                    "--require-terminal-results",
                    "--require-github-digest",
                    "--anonymous-readback",
                    "--immutable",
                    "--out",
                    "/tmp/product-evidence.json",
                ]
            )

        self.assertEqual(status, 0)
        publish.assert_called_once()
        args = publish.call_args.args[0]
        self.assertEqual(args.product_id, "mini-shell")
        self.assertEqual(args.product_work_id, "c" * 64)
        self.assertEqual(args.work_id, "b" * 64)
        self.assertEqual(args.run_attempt, 2)
        self.assertTrue(args.require_terminal_results)
        self.assertTrue(args.require_github_digest)
        self.assertTrue(args.anonymous_readback)
        self.assertTrue(args.immutable)

    def test_candidate_locator_file_is_canonical_and_exact(self) -> None:
        body = canonical_bytes(
            {
                "builder_report_sha256": self.locator.builder_report_sha256,
                "immutable_reference": self.locator.immutable_reference,
                "manifest_digest": self.locator.manifest_digest,
                "product_id": self.locator.product_id,
                "repository": self.locator.repository,
                "vfs_layer_bytes": self.locator.vfs_layer_bytes,
                "vfs_layer_sha256": self.locator.vfs_layer_sha256,
            }
        )
        self.assertEqual(
            load_candidate_product_locator(
                body, expected_repository=self.locator.repository
            ),
            self.locator,
        )
        with self.assertRaises(ProductEvidenceError):
            load_candidate_product_locator(
                body.rstrip(b"\n"), expected_repository=self.locator.repository
            )
        substituted = json.loads(body)
        substituted["repository"] = substituted["repository"].replace(
            "kandelo-dev", "attacker"
        )
        with self.assertRaises(ProductEvidenceError):
            load_candidate_product_locator(
                canonical_bytes(substituted),
                expected_repository=self.locator.repository,
            )

    def test_cli_routes_one_exact_product_evidence_work_item(self) -> None:
        from scripts.abi_staging import cli

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                cli,
                "_execute_product_evidence_work",
                return_value=0,
                create=True,
            ) as execute:
                status = cli_main(
                    [
                        "execute-product-evidence-work",
                        "--host",
                        "node",
                        "--definition-id",
                        "mini-node",
                        "--product-id",
                        "mini-shell",
                        "--product-work-id",
                        "a" * 64,
                        "--work-id",
                        "b" * 64,
                        "--input-root",
                        str(root / "inputs"),
                        "--kandelo-root",
                        str(root / "candidate"),
                        "--kandelo-policy-root",
                        str(root / "policy"),
                        "--tap-root",
                        str(root / "tap"),
                        "--run-id",
                        "717",
                        "--run-attempt",
                        "2",
                        "--workflow-ref",
                        (
                            "kandelo-dev/homebrew-tap-core/.github/workflows/"
                            "abi-staging-reconcile.yml@" + "9" * 40
                        ),
                        "--out",
                        str(root / "result"),
                    ]
                )
        self.assertEqual(status, 0)
        args = execute.call_args.args[0]
        self.assertEqual(args.host, "node")
        self.assertEqual(args.definition_id, "mini-node")
        self.assertEqual(args.product_work_id, "a" * 64)
        self.assertEqual(args.work_id, "b" * 64)
        self.assertEqual(args.run_id, 717)
        self.assertEqual(args.run_attempt, 2)

    def test_protected_runner_arguments_are_host_exact_and_complete(self) -> None:
        from scripts.abi_staging.cli import _product_evidence_runner_arguments

        root = Path("/tmp/exact-product-evidence")
        common = {
            "builder_report": root / "product/builder-report.json",
            "candidate_locator": root / "candidate/product-candidate.json",
            "context": root / "context.json",
            "definitions": root / "policy/abi/staging/evidence-definitions.generated.json",
            "output": root / "result.json",
            "products": root / "candidate/images/vfs/products/generated/catalog.json",
            "resolved_inputs": root / "product/resolved-inputs.json",
            "runtime_bundle": root / "runtime/runtime-bundle.json",
            "runtime_root": root / "runtime/runtime",
            "source_root": root / "candidate",
            "vfs": root / "product/mini-shell.vfs.zst",
        }
        node = _product_evidence_runner_arguments(host="node", **common)
        self.assertEqual(
            node[0],
            root / "policy/scripts/abi-staging-product-node-evidence.ts",
        )
        self.assertIn("--source-root", node)
        self.assertNotIn("--pages", node)
        self.assertNotIn("--tests", node)

        browser = _product_evidence_runner_arguments(host="browser", **common)
        self.assertEqual(
            browser[0],
            root / "policy/scripts/abi-staging-product-browser-evidence.ts",
        )
        self.assertNotIn("--source-root", browser)
        self.assertEqual(
            browser[browser.index("--pages") + 1],
            root
            / "candidate/apps/browser-demos/pages/kandelo/kernel-host/"
            "pages-vfs-products.generated.json",
        )
        self.assertEqual(
            browser[browser.index("--tests") + 1],
            root / "candidate/tests/vfs-products.generated.json",
        )

    def test_selects_only_the_exact_request_owned_evidence_work(self) -> None:
        selector = getattr(
            product_evidence_module,
            "select_product_evidence_execution_scope",
            None,
        )
        self.assertIsNotNone(
            selector, "protected product evidence work selector is absent"
        )
        request = {
            "requirements": {
                "evidence": [
                    {
                        "applicability": "required",
                        "browser": ["mini-browser"],
                        "node": ["mini-node"],
                        "product_id": "mini-shell",
                    }
                ],
                "products": [
                    {
                        "id": "mini-shell",
                        "manifest_sha256": "b" * 64,
                        "path": "images/vfs/products/mini-shell.toml",
                    }
                ],
            }
        }
        request_digest = canonical_sha256(request)
        base_identity = {
            "applicability": "required",
            "manifest_sha256": "b" * 64,
            "product_id": "mini-shell",
            "request_digest": request_digest,
        }
        product_work_id = canonical_sha256(
            {**base_identity, "stage": "compose-product"}
        )
        evidence_work_id = canonical_sha256(
            {
                **base_identity,
                "definition_id": "mini-node",
                "stage": "node-product-evidence",
            }
        )

        scope = selector(
            request,
            request_sha256=request_digest,
            product_id="mini-shell",
            product_work_id=product_work_id,
            host="node",
            definition_id="mini-node",
            work_id=evidence_work_id,
        )

        self.assertEqual(scope["manifest_path"], "images/vfs/products/mini-shell.toml")
        self.assertEqual(scope["applicability"], "required")
        self.assertEqual(scope["host"], "node")
        self.assertEqual(scope["definition_id"], "mini-node")
        self.assertEqual(scope["product_work_id"], product_work_id)
        self.assertEqual(scope["work_id"], evidence_work_id)

        for label, mutation in {
            "parent work": {"product_work_id": "0" * 64},
            "evidence work": {"work_id": "0" * 64},
            "definition": {"definition_id": "unselected-node"},
            "host": {"host": "browser"},
        }.items():
            arguments = {
                "request_sha256": request_digest,
                "product_id": "mini-shell",
                "product_work_id": product_work_id,
                "host": "node",
                "definition_id": "mini-node",
                "work_id": evidence_work_id,
                **mutation,
            }
            with self.subTest(label=label), self.assertRaises(ProductEvidenceError):
                selector(request, **arguments)

    def test_publication_scope_requires_every_selected_terminal_definition(self) -> None:
        selector = getattr(
            product_evidence_module,
            "select_product_evidence_publication_scope",
            None,
        )
        self.assertIsNotNone(
            selector, "protected product evidence publication selector is absent"
        )
        request = {
            "requirements": {
                "evidence": [
                    {
                        "applicability": "required",
                        "browser": ["mini-browser"],
                        "node": ["mini-node"],
                        "product_id": "mini-shell",
                    }
                ],
                "products": [
                    {
                        "id": "mini-shell",
                        "manifest_sha256": "b" * 64,
                        "path": "images/vfs/products/mini-shell.toml",
                    }
                ],
                "registries": [
                    {
                        "kind": "pages",
                        "path": (
                            "apps/browser-demos/pages/kandelo/kernel-host/"
                            "pages-vfs-products.toml"
                        ),
                        "sha256": "c" * 64,
                    },
                    {
                        "kind": "tests",
                        "path": "tests/vfs-products.toml",
                        "sha256": "d" * 64,
                    },
                ],
            }
        }
        definitions = []
        for host, definition_id in (
            ("browser", "mini-browser"),
            ("node", "mini-node"),
        ):
            identity = {
                "host": host,
                "id": definition_id,
                "implementation": [
                    {
                        "path": f"scripts/{definition_id}.ts",
                        "sha256": _sha(definition_id),
                    }
                ],
                "probe": {"argv": ["/usr/bin/mini"]},
                "runner": "exec",
                "timeout_seconds": 30,
            }
            definitions.append(
                {**identity, "definition_sha256": canonical_sha256(identity)}
            )
        registry = {
            "schema": 1,
            "kind": "kandelo-vfs-evidence-definitions",
            "version": 1,
            "definitions": definitions,
        }
        request_digest = canonical_sha256(request)
        base_identity = {
            "applicability": "required",
            "manifest_sha256": "b" * 64,
            "product_id": "mini-shell",
            "request_digest": request_digest,
        }
        product_work_id = canonical_sha256(
            {**base_identity, "stage": "compose-product"}
        )
        publication_work_id = canonical_sha256(
            {**base_identity, "stage": "publish-product-evidence"}
        )

        scope = selector(
            request,
            request_sha256=request_digest,
            product_id="mini-shell",
            product_work_id=product_work_id,
            work_id=publication_work_id,
            definitions=registry,
        )

        self.assertEqual(scope["work_id"], publication_work_id)
        self.assertEqual(scope["product_work_id"], product_work_id)
        self.assertEqual(scope["selecting_registries"], request["requirements"]["registries"])
        self.assertEqual(
            scope["requirements"],
            [
                {
                    "applicability": "required",
                    "definition_sha256": definitions[0]["definition_sha256"],
                    "host": "browser",
                    "id": "mini-browser",
                },
                {
                    "applicability": "required",
                    "definition_sha256": definitions[1]["definition_sha256"],
                    "host": "node",
                    "id": "mini-node",
                },
            ],
        )
        self.assertEqual(
            [item["work_id"] for item in scope["evidence_work"]],
            [
                canonical_sha256(
                    {
                        **base_identity,
                        "definition_id": "mini-browser",
                        "stage": "browser-product-evidence",
                    }
                ),
                canonical_sha256(
                    {
                        **base_identity,
                        "definition_id": "mini-node",
                        "stage": "node-product-evidence",
                    }
                ),
            ],
        )

        missing = copy.deepcopy(registry)
        missing["definitions"].pop()
        with self.assertRaisesRegex(ProductEvidenceError, "definition"):
            selector(
                request,
                request_sha256=request_digest,
                product_id="mini-shell",
                product_work_id=product_work_id,
                work_id=publication_work_id,
                definitions=missing,
            )

    def test_protected_context_uses_only_selected_manifest_and_definition_authority(
        self,
    ) -> None:
        builder = getattr(
            product_evidence_module, "build_product_evidence_context", None
        )
        self.assertIsNotNone(builder, "protected evidence context builder is absent")
        manifest = {
            "architecture": "wasm32",
            "boot": {
                "argv": ["/usr/bin/mini", "--ready"],
                "cwd": "/home",
                "env": {"HOME": "/home", "PATH": "/usr/bin:/bin"},
                "gid": 1000,
                "uid": 1000,
            },
            "evidence": {
                "browser": {"test": "mini-browser"},
                "node": {"test": "mini-node"},
            },
            "id": "mini-shell",
            "mounts": [
                {"path": "/", "readonly": False, "source": "built-image"},
                {
                    "ephemeral": True,
                    "gid": 0,
                    "mode": "1777",
                    "path": "/tmp",
                    "source": "scratch",
                    "uid": 0,
                },
            ],
            "output": "mini-shell.vfs",
            "schema": 1,
        }
        manifest_sha256 = canonical_sha256(manifest)
        definition_identity = {
            "host": "node",
            "id": "mini-node",
            "implementation": [
                {"path": "scripts/mini-node-runner.ts", "sha256": "e" * 64}
            ],
            "probe": {
                "argv": ["/usr/bin/mini", "--ready"],
                "stdout_exact": "ready\n",
            },
            "runner": "exec",
            "timeout_seconds": 30,
        }
        definition = {
            **definition_identity,
            "definition_sha256": canonical_sha256(definition_identity),
        }
        catalog = {
            "schema": 1,
            "kind": "kandelo-vfs-product-catalog",
            "products": [
                {
                    "manifest": manifest,
                    "path": "images/vfs/products/mini-shell.toml",
                    "sha256": manifest_sha256,
                }
            ],
        }
        definitions = {
            "schema": 1,
            "kind": "kandelo-vfs-evidence-definitions",
            "version": 1,
            "definitions": [definition],
        }
        request = {
            "build_source": copy.deepcopy(self.inputs["source"]),
            "issuance": {
                "policy_sha256": self.inputs["build_environment"]["policy_sha256"]
            },
            "requirements": {
                "evidence": [
                    {
                        "applicability": "required",
                        "browser": ["mini-browser"],
                        "node": ["mini-node"],
                        "product_id": "mini-shell",
                    }
                ],
                "products": [
                    {
                        "id": "mini-shell",
                        "manifest_sha256": manifest_sha256,
                        "path": "images/vfs/products/mini-shell.toml",
                    }
                ],
            },
            "target_abi": copy.deepcopy(self.inputs["target_abi"]),
        }
        request_digest = canonical_sha256(request)

        context = builder(
            request=request,
            request_digest=request_digest,
            catalog=catalog,
            definitions=definitions,
            candidate_product=self.locator,
            runtime_bundle_body=canonical_bytes(self.runtime),
            host="node",
            definition_id="mini-node",
            run=_run(),
        )

        self.assertEqual(
            context,
            {
                "schema": 1,
                "kind": "kandelo-vfs-product-node-evidence-context",
                "request_digest": request_digest,
                "product": {
                    "id": "mini-shell",
                    "manifest_sha256": manifest_sha256,
                },
                "candidate_product": self.locator.evidence_identity(),
                "runtime": runtime_evidence_identity(canonical_bytes(self.runtime)),
                "host": "node",
                "definition": definition,
                "boot": manifest["boot"],
                "mounts": manifest["mounts"],
                "run": _run(),
            },
        )


class CandidateProductPublicationTests(ProductEvidenceFixture, unittest.TestCase):
    def test_product_repository_is_reserved_and_abi_derived(self) -> None:
        self.assertEqual(
            self.repository,
            "kandelo-dev/homebrew-tap-core-abi-8-candidates/products/mini-shell",
        )
        self.assertEqual(self.plan.artifact_type, PRODUCT_CANDIDATE_MEDIA_TYPE)
        self.assertEqual(
            [layer.role for layer in self.plan.layers],
            ["vfs-image", "builder-report", "resolved-inputs", "runtime-bundle"],
        )
        candidate_record = json.loads(self.plan.config.body)
        self.assertEqual(
            set(candidate_record["artifacts"]),
            {
                "vfs_image",
                "builder_report",
                "resolved_inputs",
                "runtime_bundle",
                "lazy_inputs",
            },
        )
        self.assertEqual(candidate_record["artifacts"]["lazy_inputs"], [])
        self.assertEqual(
            self.plan.annotations["dev.kandelo.abi-staging.classification"],
            "public-candidate-not-endorsed",
        )
        self.assertEqual(self.plan.annotations["dev.kandelo.abi-staging.nonendorsed"], "true")
        self.assertEqual(
            self.plan.annotations["org.opencontainers.image.source"],
            "https://github.com/" + SOURCE_ASSOCIATION,
        )

    def test_publication_returns_immutable_product_locator_and_anonymous_readback(self) -> None:
        transport = FakeRegistryTransport()
        locator = publish_candidate_product(
            self.plan,
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.assertIsInstance(locator, CandidateProductLocatorV1)
        self.assertEqual(locator.product_id, "mini-shell")
        self.assertEqual(locator.repository, "ghcr.io/" + self.repository)
        self.assertEqual(locator.vfs_layer_sha256, _sha(self.vfs))
        fetched = fetch_public_record(
            locator.as_public_locator(),
            transport=transport,
            expected_artifact_type=PRODUCT_CANDIDATE_MEDIA_TYPE,
            required_layer_roles=("vfs-image",),
        )
        self.assertEqual(fetched.artifact_type, PRODUCT_CANDIDATE_MEDIA_TYPE)
        self.assertEqual(fetched.layers[0].body, self.vfs)
        self.assertTrue(any(not authenticated for _, _, authenticated in transport.calls))

    def test_anonymous_inventory_recovers_current_product_without_vfs_download(self) -> None:
        inspector = getattr(
            product_evidence_module, "inspect_candidate_product_repository", None
        )
        self.assertIsNotNone(
            inspector, "anonymous candidate product inventory is absent"
        )
        transport = FakeRegistryTransport()
        published = publish_candidate_product(
            self.plan,
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        request = {
            "build_source": copy.deepcopy(self.inputs["source"]),
            "requirements": {
                "products": [
                    {
                        "id": self.inputs["product"]["id"],
                        "manifest_sha256": self.inputs["product"][
                            "manifest_sha256"
                        ],
                        "path": self.inputs["product"]["manifest_path"],
                    }
                ]
            },
            "target_abi": copy.deepcopy(self.inputs["target_abi"]),
        }
        request_sha256 = canonical_sha256(request)
        transport.calls.clear()

        inventory = inspector(
            self.repository,
            request=request,
            request_sha256=request_sha256,
            expected_source_repository=SOURCE_ASSOCIATION,
            transport=transport,
        )

        self.assertEqual(len(inventory), 1)
        entry = inventory[0]
        self.assertEqual(entry.locator, published)
        self.assertEqual(
            entry.runtime_bundle_sha256, canonical_sha256(self.runtime)
        )
        self.assertEqual(
            entry.artifact,
            CandidateProductArtifactV1(
                product_id=self.inputs["product"]["id"],
                manifest_sha256=self.inputs["product"]["manifest_sha256"],
                architecture=self.inputs["product"]["architecture"],
                request_sha256=request_sha256,
                source_repository=self.inputs["source"]["repository"],
                source_commit=self.inputs["source"]["commit"],
                source_tree=self.inputs["source"]["tree"],
                target_abi=self.inputs["target_abi"]["version"],
                snapshot_sha256=self.inputs["target_abi"]["snapshot_sha256"],
                vfs_layer_sha256=_sha(self.vfs),
                vfs_layer_bytes=len(self.vfs),
                immutable_reference=(
                    f"ghcr.io/{self.repository}@sha256:{_sha(self.vfs)}"
                ),
                builder_report_sha256=canonical_sha256(self.report),
            ),
        )
        vfs_blob_path = "/blobs/sha256:" + _sha(self.vfs)
        self.assertFalse(
            any(
                method == "GET" and vfs_blob_path in url
                for method, url, _authenticated in transport.calls
            )
        )
        self.assertTrue(all(not authenticated for _, _, authenticated in transport.calls))

    def test_candidate_plan_streams_the_exact_runtime_root_without_loading_a_file_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_root = Path(temporary) / "runtime"
            for relative, body in self.runtime_files.items():
                destination = runtime_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(body)

            plan = build_candidate_product_oci_plan(
                repository=self.repository,
                publisher_repository=SOURCE_ASSOCIATION,
                input_plan=self.input_plan,
                vfs_body=self.vfs,
                builder_report_body=canonical_bytes(self.report),
                resolved_inputs_body=canonical_bytes(self.inputs),
                runtime_bundle_body=canonical_bytes(self.runtime),
                runtime_root=runtime_root,
            )
            self.assertEqual(plan.config.body, self.plan.config.body)

            (runtime_root / "undeclared.bin").write_bytes(b"ambient runtime byte\n")
            with self.assertRaisesRegex(
                ProductEvidenceError,
                "runtime file handoff differs",
            ):
                build_candidate_product_oci_plan(
                    repository=self.repository,
                    publisher_repository=SOURCE_ASSOCIATION,
                    input_plan=self.input_plan,
                    vfs_body=self.vfs,
                    builder_report_body=canonical_bytes(self.report),
                    resolved_inputs_body=canonical_bytes(self.inputs),
                    runtime_bundle_body=canonical_bytes(self.runtime),
                    runtime_root=runtime_root,
                )

    def test_candidate_product_validation_rejects_every_identity_drift(self) -> None:
        mutations = {
            "Formula repository": self.repository.replace("/products", ""),
            "wrong product": self.repository.replace("mini-shell", "other-shell"),
            "wrong ABI": self.repository.replace("abi-8", "abi-9"),
            "canonical namespace": self.repository.replace("-candidates", ""),
        }
        for label, repository in mutations.items():
            with self.subTest(label=label), self.assertRaises(ProductEvidenceError):
                build_candidate_product_oci_plan(
                    repository=repository,
                    publisher_repository=SOURCE_ASSOCIATION,
                    input_plan=self.input_plan,
                    vfs_body=self.vfs,
                    builder_report_body=canonical_bytes(self.report),
                    resolved_inputs_body=canonical_bytes(self.inputs),
                    runtime_bundle_body=canonical_bytes(self.runtime),
                    runtime_files=self.runtime_files,
                )

        for label, mutation in {
            "VFS": lambda report, runtime, files: report["output"].update(sha256="f" * 64),
            "report input": lambda report, runtime, files: report["inputs"][0].update(bytes=999),
            "runtime source": lambda report, runtime, files: runtime[
                "source"
            ].update(commit="f" * 40),
            "runtime ABI": lambda report, runtime, files: runtime["target_abi"].update(version=9),
            "runtime host bundle": lambda report, runtime, files: runtime["host"].update(
                bundle_sha256="f" * 64
            ),
            "runtime inventory": lambda report, runtime, files: files.update(
                {"kernel/kernel.wasm": b"drift"}
            ),
        }.items():
            report = copy.deepcopy(self.report)
            runtime = copy.deepcopy(self.runtime)
            files = dict(self.runtime_files)
            mutation(report, runtime, files)
            with self.subTest(label=label), self.assertRaises(ProductEvidenceError):
                build_candidate_product_oci_plan(
                    repository=self.repository,
                    publisher_repository=SOURCE_ASSOCIATION,
                    input_plan=self.input_plan,
                    vfs_body=self.vfs,
                    builder_report_body=canonical_bytes(report),
                    resolved_inputs_body=canonical_bytes(self.inputs),
                    runtime_bundle_body=canonical_bytes(runtime),
                    runtime_files=files,
                )

    def test_candidate_publication_is_bound_to_the_protected_resolver_plan(self) -> None:
        mutations = {
            "product": replace(self.input_plan, product_id="other-shell"),
            "resolved inputs": replace(
                self.input_plan,
                resolved_inputs_sha256="f" * 64,
            ),
            "runtime bundle": replace(
                self.input_plan,
                runtime_bundle_sha256="f" * 64,
            ),
        }
        for label, input_plan in mutations.items():
            with self.subTest(label=label), self.assertRaises(ProductEvidenceError):
                build_candidate_product_oci_plan(
                    repository=self.repository,
                    publisher_repository=SOURCE_ASSOCIATION,
                    input_plan=input_plan,
                    vfs_body=self.vfs,
                    builder_report_body=canonical_bytes(self.report),
                    resolved_inputs_body=canonical_bytes(self.inputs),
                    runtime_bundle_body=canonical_bytes(self.runtime),
                    runtime_files=self.runtime_files,
                )

    def test_formula_and_product_repositories_cannot_cross(self) -> None:
        validate_formula_candidate_repository(
            "kandelo-dev/homebrew-tap-core-abi-8-candidates/mini-shell"
        )
        with self.assertRaises(TapRecordError):
            validate_formula_candidate_repository(self.repository)
        with self.assertRaises(ProductEvidenceError):
            build_candidate_product_oci_plan(
                repository="kandelo-dev/homebrew-tap-core-abi-8-candidates/mini-shell",
                publisher_repository=SOURCE_ASSOCIATION,
                input_plan=self.input_plan,
                vfs_body=self.vfs,
                builder_report_body=canonical_bytes(self.report),
                resolved_inputs_body=canonical_bytes(self.inputs),
                runtime_bundle_body=canonical_bytes(self.runtime),
                runtime_files=self.runtime_files,
            )

    def test_candidate_references_remain_noncanonical(self) -> None:
        hostile = copy.deepcopy(self.inputs)
        bottle = next(item for item in hostile["inputs"] if item["kind"] == "homebrew-bottle")
        bottle["reference"] = bottle["reference"].replace("-candidates", "")
        with self.assertRaises(ProductEvidenceError):
            build_candidate_product_oci_plan(
                repository=self.repository,
                publisher_repository=SOURCE_ASSOCIATION,
                input_plan=self.input_plan,
                vfs_body=self.vfs,
                builder_report_body=canonical_bytes(self.report),
                resolved_inputs_body=canonical_bytes(hostile),
                runtime_bundle_body=canonical_bytes(self.runtime),
                runtime_files=self.runtime_files,
            )

    def test_private_lazy_inputs_publish_only_as_exact_candidate_blob_layers(self) -> None:
        inputs = copy.deepcopy(self.inputs)
        body = b"exact lazy package bytes\n"
        digest = _sha(body)
        package = next(
            item for item in inputs["inputs"] if item["kind"] == "package-output"
        )
        package["declared_materialization"] = "lazy"
        package["effective_materialization"] = "lazy-reference"
        package.pop("path")
        package["sha256"] = digest
        package["bytes"] = len(body)
        package["reference"] = f"ghcr.io/{self.repository}@sha256:{digest}"
        report = _report(inputs, self.vfs)
        input_plan = replace(
            self.input_plan,
            resolved_inputs_sha256=canonical_sha256(inputs),
        )
        plan = build_candidate_product_oci_plan(
            repository=self.repository,
            publisher_repository=SOURCE_ASSOCIATION,
            input_plan=input_plan,
            vfs_body=self.vfs,
            builder_report_body=canonical_bytes(report),
            resolved_inputs_body=canonical_bytes(inputs),
            runtime_bundle_body=canonical_bytes(self.runtime),
            runtime_files=self.runtime_files,
            lazy_input_bodies={package["id"]: body},
        )
        record = json.loads(plan.config.body)
        self.assertEqual(
            record["artifacts"]["lazy_inputs"],
            [
                {
                    "bytes": len(body),
                    "id": package["id"],
                    "immutable_reference": package["reference"],
                    "kind": "package-output",
                    "sha256": digest,
                }
            ],
        )
        self.assertEqual(plan.layers[-1].role, "lazy-input-0000")
        self.assertEqual(plan.layers[-1].body, body)

        for lazy_input_bodies in ({}, {package["id"]: body, "extra": b"extra"}):
            with self.subTest(lazy_input_bodies=lazy_input_bodies), self.assertRaises(
                ProductEvidenceError
            ):
                build_candidate_product_oci_plan(
                    repository=self.repository,
                    publisher_repository=SOURCE_ASSOCIATION,
                    input_plan=input_plan,
                    vfs_body=self.vfs,
                    builder_report_body=canonical_bytes(report),
                    resolved_inputs_body=canonical_bytes(inputs),
                    runtime_bundle_body=canonical_bytes(self.runtime),
                    runtime_files=self.runtime_files,
                    lazy_input_bodies=lazy_input_bodies,
                )


class ProductEvidenceRecordTests(ProductEvidenceFixture, unittest.TestCase):
    def test_workflow_publisher_selects_only_current_run_terminal_artifacts(self) -> None:
        from scripts.abi_staging import cli

        publisher = getattr(cli, "_publish_workflow_product_evidence", None)
        self.assertIsNotNone(
            publisher, "protected workflow product evidence publisher is absent"
        )
        definitions = []
        for host, definition_id in (
            ("browser", "mini-browser"),
            ("node", "mini-node"),
        ):
            identity = {
                "host": host,
                "id": definition_id,
                "implementation": [
                    {
                        "path": f"scripts/{definition_id}.ts",
                        "sha256": _sha(definition_id),
                    }
                ],
                "probe": {"argv": ["/usr/bin/mini"]},
                "runner": "exec",
                "timeout_seconds": 30,
            }
            definitions.append(
                {**identity, "definition_sha256": canonical_sha256(identity)}
            )
        request = {
            "build_source": copy.deepcopy(self.inputs["source"]),
            "issuance": {
                "issuer_workflow_ref": (
                    "kandelo-dev/kandelo/.github/workflows/"
                    "abi-staging-request.yml@" + "9" * 40
                ),
                "policy_sha256": self.inputs["build_environment"]["policy_sha256"],
            },
            "requirements": {
                "evidence": [
                    {
                        "applicability": "required",
                        "browser": ["mini-browser"],
                        "node": ["mini-node"],
                        "product_id": "mini-shell",
                    }
                ],
                "products": [
                    {
                        "id": "mini-shell",
                        "manifest_sha256": self.inputs["product"]["manifest_sha256"],
                        "path": self.inputs["product"]["manifest_path"],
                    }
                ],
                "registries": copy.deepcopy(self.registries),
            },
            "target_abi": copy.deepcopy(self.inputs["target_abi"]),
        }
        self.request_digest = canonical_sha256(request)
        base_identity = {
            "applicability": "required",
            "manifest_sha256": self.inputs["product"]["manifest_sha256"],
            "product_id": "mini-shell",
            "request_digest": self.request_digest,
        }
        product_work_id = canonical_sha256(
            {**base_identity, "stage": "compose-product"}
        )
        publication_work_id = canonical_sha256(
            {**base_identity, "stage": "publish-product-evidence"}
        )
        requirements = [
            {
                "applicability": "required",
                "definition_sha256": definitions[0]["definition_sha256"],
                "host": "browser",
                "id": "mini-browser",
            },
            {
                "applicability": "required",
                "definition_sha256": definitions[1]["definition_sha256"],
                "host": "node",
                "id": "mini-node",
            },
        ]
        results = [self.result(requirement) for requirement in requirements]
        workflow_ref = (
            SOURCE_ASSOCIATION
            + "/.github/workflows/abi-staging-reconcile.yml@refs/heads/main"
        )
        for result in results:
            result["run"] = {
                "repository": SOURCE_ASSOCIATION,
                "workflow_ref": workflow_ref,
                "run_id": 717,
                "job_id": f"{result['host']}-product-evidence",
                "attempt": 2,
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate_root = root / "candidate"
            policy_root = root / "policy"
            definitions_path = (
                policy_root / "abi/staging/evidence-definitions.generated.json"
            )
            definitions_path.parent.mkdir(parents=True)
            definitions_path.write_bytes(
                canonical_bytes(
                    {
                        "schema": 1,
                        "kind": "kandelo-vfs-evidence-definitions",
                        "version": 1,
                        "definitions": definitions,
                    }
                )
            )
            candidate_root.mkdir()
            sources: dict[str, Path] = {}

            def artifact_source(name: str) -> Path:
                path = root / "artifacts" / name
                path.mkdir(parents=True)
                sources[name] = path
                return path

            coordination_name = "abi-staging-coordination-717-2"
            (artifact_source(coordination_name) / "coordination.json").write_bytes(
                b"inert coordination bytes\n"
            )
            runtime_name = f"abi-staging-runtime-{self.request_digest}-717-2"
            (artifact_source(runtime_name) / "runtime-bundle.json").write_bytes(
                canonical_bytes(self.runtime)
            )
            handoff_name = (
                f"abi-staging-product-build-mini-shell-{product_work_id}-717-2"
            )
            handoff = artifact_source(handoff_name)
            (handoff / "resolved-inputs.json").write_bytes(canonical_bytes(self.inputs))
            (handoff / "builder-report.json").write_bytes(canonical_bytes(self.report))
            candidate_name = (
                f"abi-staging-product-candidate-mini-shell-{product_work_id}-717-2"
            )
            (artifact_source(candidate_name) / "product-candidate.json").write_bytes(
                canonical_bytes(
                    {
                        "builder_report_sha256": self.locator.builder_report_sha256,
                        "immutable_reference": self.locator.immutable_reference,
                        "manifest_digest": self.locator.manifest_digest,
                        "product_id": self.locator.product_id,
                        "repository": self.locator.repository,
                        "vfs_layer_bytes": self.locator.vfs_layer_bytes,
                        "vfs_layer_sha256": self.locator.vfs_layer_sha256,
                    }
                )
            )
            expected_result_names = []
            for result in results:
                evidence_work_id = canonical_sha256(
                    {
                        **base_identity,
                        "definition_id": result["definition"]["id"],
                        "stage": f"{result['host']}-product-evidence",
                    }
                )
                name = (
                    f"abi-staging-product-{result['host']}-mini-shell-"
                    f"{evidence_work_id}-717-2"
                )
                expected_result_names.append(name)
                filename = f"{result['host']}-result"
                (artifact_source(name) / filename).write_bytes(canonical_bytes(result))

            class FakeArtifactClient:
                def __init__(self) -> None:
                    self.requested: list[str] = []

                def artifact_by_name(self, *, name: str) -> SimpleNamespace:
                    self.requested.append(name)
                    source = sources[name]
                    return SimpleNamespace(
                        id=len(self.requested),
                        name=name,
                        sha256=_sha(name),
                        size_in_bytes=sum(
                            path.stat().st_size for path in source.rglob("*") if path.is_file()
                        ),
                    )

                def extract_artifact(
                    self,
                    artifact: SimpleNamespace,
                    destination: Path,
                    *,
                    max_files: int,
                    max_bytes: int,
                ) -> dict[str, object]:
                    del max_files, max_bytes
                    shutil.copytree(sources[artifact.name], destination)
                    return {}

            client = FakeArtifactClient()
            policy = SimpleNamespace(
                max_handoff_bytes=4 * 1024 * 1024,
                max_handoff_files=32,
                tap_repository=SOURCE_ASSOCIATION,
            )
            args = SimpleNamespace(
                anonymous_readback=True,
                head_sha="a" * 40,
                immutable=True,
                kandelo_policy_root=str(policy_root),
                kandelo_root=str(candidate_root),
                out=str(root / "published.json"),
                product_id="mini-shell",
                product_work_id=product_work_id,
                require_github_digest=True,
                require_terminal_results=True,
                run_attempt=2,
                run_id=717,
                work_id=publication_work_id,
            )
            transport = FakeRegistryTransport()
            bundle = {
                "request": request,
                "request_sha256": self.request_digest,
                "tap_plan": {},
            }
            with (
                patch.object(cli, "load_tap_staging_policy", return_value=policy),
                patch.object(
                    cli,
                    "snapshot_tap_source",
                    return_value={"commit": "a" * 40},
                ),
                patch.object(cli, "_recheck_workflow_activation"),
                patch.object(
                    cli, "GitHubWorkflowArtifactClientV1", return_value=client
                ),
                patch.object(cli, "load_coordination_bundle", return_value=bundle),
                patch.object(
                    cli,
                    "product_runtime_identity",
                    return_value={
                        "policy_sha256": self.inputs["build_environment"][
                            "policy_sha256"
                        ],
                        "dev_shell_lock_sha256": self.inputs["build_environment"][
                            "dev_shell_lock_sha256"
                        ],
                    },
                ),
                patch.object(
                    cli,
                    "_checked_checkout_source",
                    side_effect=[candidate_root, policy_root],
                ),
                patch.object(
                    cli,
                    "validate_product_build_handoff",
                    return_value={
                        "outcome": "success",
                        "builder_report_sha256": self.locator.builder_report_sha256,
                        "product": {
                            "id": "mini-shell",
                            "manifest_sha256": self.inputs["product"]["manifest_sha256"],
                            "output": self.inputs["product"]["output"],
                        },
                        "vfs": {
                            "bytes": self.locator.vfs_layer_bytes,
                            "sha256": self.locator.vfs_layer_sha256,
                        },
                    },
                ),
                patch.object(cli, "isolated_oras_transport") as isolated,
                patch.dict(
                    "os.environ",
                    {
                        "GITHUB_REPOSITORY": SOURCE_ASSOCIATION,
                        "GITHUB_TOKEN": "token",
                        "GITHUB_WORKFLOW_REF": workflow_ref,
                        "HOMEBREW_GITHUB_PACKAGES_TOKEN": "token",
                        "HOMEBREW_GITHUB_PACKAGES_USER": "publisher",
                    },
                    clear=True,
                ),
            ):
                isolated.return_value.__enter__.return_value = transport
                publisher(args)

            output = json.loads((root / "published.json").read_bytes())
            self.assertEqual(output["kind"], "kandelo-vfs-product-evidence-publication")
            self.assertEqual(output["work_id"], publication_work_id)
            self.assertEqual(len(output["receipt_locators"]), 2)
            self.assertTrue(
                output["record_locator"]["repository"].endswith(
                    "/products/mini-shell/evidence"
                )
            )
            self.assertEqual(
                client.requested,
                [
                    coordination_name,
                    runtime_name,
                    handoff_name,
                    candidate_name,
                    *expected_result_names,
                ],
            )

    def test_exact_publisher_requires_and_publishes_every_terminal_result(self) -> None:
        publisher = getattr(
            product_evidence_module, "publish_exact_product_evidence", None
        )
        self.assertIsNotNone(
            publisher, "protected exact product evidence publisher is absent"
        )
        requirements = [
            self.requirement("browser", "mini-browser"),
            self.requirement("node", "mini-node"),
        ]
        results = [self.result(requirement) for requirement in requirements]
        transport = FakeRegistryTransport()

        published = publisher(
            request_digest=self.request_digest,
            product=self.inputs["product"],
            candidate_product=self.locator,
            runtime_bundle_body=canonical_bytes(self.runtime),
            resolved_inputs_body=canonical_bytes(self.inputs),
            builder_report_body=canonical_bytes(self.report),
            selecting_registries=self.registries,
            requirements=requirements,
            results=results,
            run=_record_run(),
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )

        self.assertEqual(len(published["receipts"]), 2)
        self.assertEqual(len(published["receipt_locators"]), 2)
        self.assertEqual(
            [
                (receipt["requirement"]["host"], receipt["requirement"]["id"])
                for receipt in published["receipts"]
            ],
            [("browser", "mini-browser"), ("node", "mini-node")],
        )
        self.assertTrue(
            published["record_locator"].repository.endswith(
                "/products/mini-shell/evidence"
            )
        )
        self.assertEqual(
            published["record"]["common"]["promotion_state"], "eligible"
        )

        for label, changed in {
            "missing": results[:-1],
            "duplicate": [results[0], results[0]],
        }.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                ProductEvidenceError, "terminal|result"
            ):
                publisher(
                    request_digest=self.request_digest,
                    product=self.inputs["product"],
                    candidate_product=self.locator,
                    runtime_bundle_body=canonical_bytes(self.runtime),
                    resolved_inputs_body=canonical_bytes(self.inputs),
                    builder_report_body=canonical_bytes(self.report),
                    selecting_registries=self.registries,
                    requirements=requirements,
                    results=changed,
                    run=_record_run(),
                    transport=transport,
                    expected_source_repository=SOURCE_ASSOCIATION,
                )

    def test_anonymous_inventory_recovers_current_aggregate_without_layers(self) -> None:
        request = {
            "build_source": copy.deepcopy(self.inputs["source"]),
            "target_abi": copy.deepcopy(self.inputs["target_abi"]),
        }
        self.request_digest = canonical_sha256(request)
        requirements = [
            self.requirement("browser", "mini-browser"),
            self.requirement("node", "mini-node"),
        ]
        transport = FakeRegistryTransport()
        published = product_evidence_module.publish_exact_product_evidence(
            request_digest=self.request_digest,
            product=self.inputs["product"],
            candidate_product=self.locator,
            runtime_bundle_body=canonical_bytes(self.runtime),
            resolved_inputs_body=canonical_bytes(self.inputs),
            builder_report_body=canonical_bytes(self.report),
            selecting_registries=self.registries,
            requirements=requirements,
            results=[self.result(requirement) for requirement in requirements],
            run=_record_run(),
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )

        entries = inspect_product_evidence_repository(
            self.repository + "/evidence",
            request=request,
            request_sha256=self.request_digest,
            product=self.inputs["product"],
            candidate_product=self.locator,
            runtime_bundle_sha256=canonical_sha256(self.runtime),
            expected_source_repository=SOURCE_ASSOCIATION,
            transport=transport,
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].record, published["record"])
        self.assertEqual(entries[0].outcome, "success")
        self.assertEqual(
            entries[0].record_sha256, canonical_sha256(published["record"])
        )

    def test_product_work_respects_lifecycle_and_marks_active_authority(self) -> None:
        decision = ReconciliationDecisionV1(
            request_digest=self.request_digest,
            claim_key="sha256:" + self.request_digest,
            lifecycle=PullRequestLifecycleV1("open", "d" * 40, None),
            current_for_pull_request=True,
            action="observe-open",
            permitted_work=(),
            blockers=(),
        )
        observed = product_evidence_work_scope(decision, "observe")
        self.assertTrue(observed.allow_required)
        self.assertFalse(observed.authoritative)
        active = product_evidence_work_scope(decision, "active")
        self.assertTrue(active.allow_required)
        self.assertTrue(active.authoritative)
        closed = replace(decision, action="stop-new-work")
        self.assertFalse(product_evidence_work_scope(closed, "active").allow_required)

    def test_complete_node_and_browser_success_is_eligible(self) -> None:
        requirements = [
            self.requirement("browser", "mini-browser"),
            self.requirement("node", "mini-node"),
        ]
        receipts = [self.receipt(requirement) for requirement in requirements]
        record = self.record(requirements, receipts)
        self.assertEqual(record["common"]["outcome"], "success")
        self.assertEqual(record["common"]["promotion_state"], "eligible")
        self.assertEqual(
            record["product_evidence"]["verification_receipt_sha256s"],
            sorted(canonical_sha256(receipt) for receipt in receipts),
        )

    def test_receipt_and_aggregate_publish_as_separate_immutable_records(self) -> None:
        transport = FakeRegistryTransport()
        published_candidate = publish_candidate_product(
            self.plan,
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        requirement = self.requirement("node", "mini-node")
        result = self.result(requirement)
        receipt = self.receipt(requirement)
        receipt_plan = build_product_evidence_receipt_oci_plan(
            receipt,
            result=result,
            candidate_product=published_candidate,
        )
        receipt_locator = publish_product_evidence_receipt(
            receipt_plan,
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.assertIn("/products/mini-shell/receipts/mini-node/node", receipt_locator.repository)
        fetched_receipt = fetch_public_record(
            {
                "repository": receipt_locator.repository,
                "digest": receipt_locator.digest,
                "immutable_reference": receipt_locator.immutable_reference,
            },
            transport=transport,
            expected_artifact_type=receipt_plan.artifact_type,
            required_layer_roles=("product-evidence-result",),
        )
        self.assertEqual(fetched_receipt.layers[0].body, canonical_bytes(result))

        record = self.record([requirement], [receipt])
        aggregate_plan = build_product_evidence_oci_plan(
            record,
            candidate_product=published_candidate,
            receipts=[receipt],
            runtime_bundle_body=canonical_bytes(self.runtime),
            resolved_inputs_body=canonical_bytes(self.inputs),
        )
        aggregate_locator = publish_product_evidence_record(
            aggregate_plan,
            transport=transport,
            expected_source_repository=SOURCE_ASSOCIATION,
        )
        self.assertTrue(aggregate_locator.repository.endswith("/products/mini-shell/evidence"))
        fetched = fetch_public_record(
            {
                "repository": aggregate_locator.repository,
                "digest": aggregate_locator.digest,
                "immutable_reference": aggregate_locator.immutable_reference,
            },
            transport=transport,
            expected_artifact_type=aggregate_plan.artifact_type,
            required_layer_roles=("runtime-bundle", "resolved-inputs", "receipt-0000"),
        )
        self.assertEqual(fetched.config.body, canonical_bytes(record))
        self.assertEqual(published_candidate, self.locator)

    def test_receipt_publication_rejects_a_different_inert_result(self) -> None:
        requirement = self.requirement("node", "mini-node")
        receipt = self.receipt(requirement)
        wrong = self.result(requirement, outcome="failure")
        with self.assertRaises(ProductEvidenceError):
            build_product_evidence_receipt_oci_plan(
                receipt,
                result=wrong,
                candidate_product=self.locator,
            )

    def test_publishers_revalidate_every_layer_at_the_write_boundary(self) -> None:
        transport = FakeRegistryTransport()
        hostile_candidate = replace(
            self.plan,
            layers=(
                replace(self.plan.layers[0], body=b"different VFS"),
                *self.plan.layers[1:],
            ),
        )
        with self.assertRaises(ProductEvidenceError):
            publish_candidate_product(
                hostile_candidate,
                transport=transport,
                expected_source_repository=SOURCE_ASSOCIATION,
            )

        requirement = self.requirement("node", "mini-node")
        result = self.result(requirement)
        receipt = self.receipt(requirement)
        receipt_plan = build_product_evidence_receipt_oci_plan(
            receipt, result=result, candidate_product=self.locator
        )
        hostile_receipt = replace(
            receipt_plan,
            layers=(replace(receipt_plan.layers[0], body=b"different result"),),
        )
        with self.assertRaises(ProductEvidenceError):
            publish_product_evidence_receipt(
                hostile_receipt,
                transport=transport,
                expected_source_repository=SOURCE_ASSOCIATION,
            )

        record = self.record([requirement], [receipt])
        aggregate = build_product_evidence_oci_plan(
            record,
            candidate_product=self.locator,
            receipts=[receipt],
            runtime_bundle_body=canonical_bytes(self.runtime),
            resolved_inputs_body=canonical_bytes(self.inputs),
        )
        hostile_aggregate = replace(
            aggregate,
            layers=(
                replace(aggregate.layers[0], body=b"different runtime"),
                *aggregate.layers[1:],
            ),
        )
        with self.assertRaises(ProductEvidenceError):
            publish_product_evidence_record(
                hostile_aggregate,
                transport=transport,
                expected_source_repository=SOURCE_ASSOCIATION,
            )
        self.assertFalse(transport.calls)

    def test_publishers_reject_descriptor_metadata_drift_before_writes(self) -> None:
        hostile_candidate = replace(
            self.plan,
            layers=(
                replace(self.plan.layers[0], media_type="application/octet-stream"),
                *self.plan.layers[1:],
            ),
        )
        with self.assertRaises(ProductEvidenceError):
            publish_candidate_product(
                hostile_candidate,
                transport=FakeRegistryTransport(),
                expected_source_repository=SOURCE_ASSOCIATION,
            )

        requirement = self.requirement("node", "mini-node")
        result = self.result(requirement)
        receipt = self.receipt(requirement)
        receipt_plan = build_product_evidence_receipt_oci_plan(
            receipt, result=result, candidate_product=self.locator
        )
        hostile_receipt = replace(
            receipt_plan,
            config=replace(receipt_plan.config, title="other-receipt.json"),
        )
        with self.assertRaises(ProductEvidenceError):
            publish_product_evidence_receipt(
                hostile_receipt,
                transport=FakeRegistryTransport(),
                expected_source_repository=SOURCE_ASSOCIATION,
            )

        record = self.record([requirement], [receipt])
        aggregate = build_product_evidence_oci_plan(
            record,
            candidate_product=self.locator,
            receipts=[receipt],
            runtime_bundle_body=canonical_bytes(self.runtime),
            resolved_inputs_body=canonical_bytes(self.inputs),
        )
        hostile_aggregate = replace(
            aggregate,
            layers=(
                replace(aggregate.layers[0], title="other-runtime.json"),
                *aggregate.layers[1:],
            ),
        )
        with self.assertRaises(ProductEvidenceError):
            publish_product_evidence_record(
                hostile_aggregate,
                transport=FakeRegistryTransport(),
                expected_source_repository=SOURCE_ASSOCIATION,
            )

    def test_node_only_product_is_complete_without_browser_receipt(self) -> None:
        requirement = self.requirement("node", "sdk-compile")
        record = self.record([requirement], [self.receipt(requirement)])
        self.assertEqual(record["common"]["outcome"], "success")
        self.assertRegex(
            record["product_evidence"]["runtime_evidence_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_runtime_evidence_identity_uses_outcomes_but_not_run_provenance(self) -> None:
        requirement = self.requirement("node", "mini-node")
        receipt = self.receipt(requirement)
        first = self.record([requirement], [receipt])

        later_run = copy.deepcopy(receipt)
        later_run["run"]["run_id"] += 1
        later_run["run"]["attempt"] += 1
        second = self.record([requirement], [later_run])
        self.assertEqual(
            first["product_evidence"]["runtime_evidence_sha256"],
            second["product_evidence"]["runtime_evidence_sha256"],
        )
        self.assertNotEqual(
            first["product_evidence"]["verification_receipt_sha256s"],
            second["product_evidence"]["verification_receipt_sha256s"],
        )

        failure = self.record(
            [requirement], [self.receipt(requirement, outcome="failure")]
        )
        self.assertNotEqual(
            first["product_evidence"]["runtime_evidence_sha256"],
            failure["product_evidence"]["runtime_evidence_sha256"],
        )

    def test_required_failure_and_timeout_are_visible_and_ineligible(self) -> None:
        for outcome in ("failure", "timeout"):
            requirement = self.requirement("node", "mini-node")
            record = self.record(
                [requirement], [self.receipt(requirement, outcome=outcome)]
            )
            with self.subTest(outcome=outcome):
                self.assertEqual(record["common"]["outcome"], outcome)
                self.assertEqual(record["common"]["promotion_state"], "ineligible")
                self.assertEqual(record["common"]["guard_codes"], [
                    "verification_failed" if outcome == "failure" else "verification_timeout"
                ])

    def test_informational_failure_does_not_block_required_success(self) -> None:
        required = self.requirement("node", "mini-node")
        informational = self.requirement(
            "browser", "mini-browser-extra", "informational"
        )
        record = self.record(
            [informational, required],
            [
                self.receipt(informational, outcome="failure"),
                self.receipt(required),
            ],
        )
        self.assertEqual(record["common"]["outcome"], "success")
        self.assertEqual(record["common"]["promotion_state"], "eligible")
        self.assertEqual(len(record["product_evidence"]["verification_receipt_sha256s"]), 2)

    def test_exact_maintainer_override_accepts_only_its_failed_result(self) -> None:
        requirement = self.requirement("browser", "mini-browser")
        result = self.result(requirement, outcome="failure")
        accepted_override = self.override_for(result)
        receipt = build_product_evidence_receipt(
            result,
            request_digest=self.request_digest,
            product=self.inputs["product"],
            candidate_product=self.locator,
            runtime_bundle_body=canonical_bytes(self.runtime),
            requirement=requirement,
            accepted_override=accepted_override,
            expected_override_policy=accepted_override["record"]["policy"],
        )
        record = self.record([requirement], [receipt])
        self.assertEqual(record["common"]["outcome"], "success")
        self.assertEqual(
            record["common"]["promotion_state"], "accepted-with-override"
        )

        wrong = self.override_for(result)
        wrong["record"]["subject_result_sha256"] = "f" * 64
        with self.assertRaises(ProductEvidenceError):
            build_product_evidence_receipt(
                result,
                request_digest=self.request_digest,
                product=self.inputs["product"],
                candidate_product=self.locator,
                runtime_bundle_body=canonical_bytes(self.runtime),
                requirement=requirement,
                accepted_override=wrong,
                expected_override_policy=accepted_override["record"]["policy"],
            )

    def test_missing_sibling_and_duplicate_receipts_fail_closed(self) -> None:
        node = self.requirement("node", "mini-node")
        browser = self.requirement("browser", "mini-browser")
        with self.assertRaises(ProductEvidenceError):
            self.record([browser, node], [self.receipt(node)])
        duplicate = self.receipt(node)
        with self.assertRaises(ProductEvidenceError):
            self.record([node], [duplicate, copy.deepcopy(duplicate)])

    def test_receipt_rejects_wrong_definition_runtime_vfs_manifest_and_layer(self) -> None:
        requirement = self.requirement("node", "mini-node")
        for label, mutate in {
            "definition": lambda result: result["definition"].update(definition_sha256="f" * 64),
            "runtime": lambda result: result["runtime"].update(bundle_sha256="f" * 64),
            "VFS": lambda result: result["candidate_product"].update(vfs_layer_sha256="f" * 64),
            "manifest": lambda result: result["product"].update(manifest_sha256="f" * 64),
            "builder report": lambda result: result["candidate_product"].update(
                builder_report_sha256="f" * 64
            ),
        }.items():
            result = self.result(requirement)
            mutate(result)
            with self.subTest(label=label), self.assertRaises(ProductEvidenceError):
                build_product_evidence_receipt(
                    result,
                    request_digest=self.request_digest,
                    product=self.inputs["product"],
                    candidate_product=self.locator,
                    runtime_bundle_body=canonical_bytes(self.runtime),
                    requirement=requirement,
                )

    def test_record_rejects_registry_and_formula_layer_drift(self) -> None:
        requirement = self.requirement("node", "mini-node")
        receipt = self.receipt(requirement)
        record = self.record([requirement], [receipt])
        for label, mutate in {
            "registry": lambda value: value["product_evidence"][
                "selecting_registries"
            ][0].update(sha256="f" * 64),
            "Formula layer": lambda value: value["product_evidence"][
                "resolved_formula_layers"
            ][0]["artifact"].update(sha256="f" * 64),
            "VFS": lambda value: value["product_evidence"]["vfs_image"].update(sha256="f" * 64),
        }.items():
            hostile = copy.deepcopy(record)
            mutate(hostile)
            with self.subTest(label=label), self.assertRaises(ProductEvidenceError):
                validate_product_evidence_record(
                    hostile,
                    request_digest=self.request_digest,
                    candidate_product=self.locator,
                    resolved_inputs_body=canonical_bytes(self.inputs),
                    builder_report_body=canonical_bytes(self.report),
                    runtime_bundle_body=canonical_bytes(self.runtime),
                    selecting_registries=self.registries,
                    requirements=[requirement],
                    receipts=[receipt],
                )

    def test_candidate_and_canonical_evidence_cannot_cross(self) -> None:
        requirement = self.requirement("node", "mini-node")
        with self.assertRaises(ProductEvidenceError):
            replace(
                self.locator,
                repository=self.locator.repository.replace("-candidates", ""),
                immutable_reference=self.locator.immutable_reference.replace(
                    "-candidates", ""
                ),
            )

    def test_result_and_receipt_protocols_are_canonical_and_bounded(self) -> None:
        requirement = self.requirement("node", "mini-node")
        result = self.result(requirement)
        validate_product_evidence_result(result)
        receipt = self.receipt(requirement)
        validate_product_evidence_receipt(receipt)
        hostile = copy.deepcopy(result)
        hostile["bounded_diagnostics"] = [
            {
                "id": "stdout",
                "sha256": _sha("oversized"),
                "bytes": 70_000,
                "text": "x" * 70_000,
            }
        ]
        with self.assertRaises(ProductEvidenceError):
            validate_product_evidence_result(hostile)

    def test_result_outcomes_require_their_exact_guard_code(self) -> None:
        requirement = self.requirement("node", "mini-node")
        for outcome, wrong_code in (
            ("failure", "verification_timeout"),
            ("timeout", "verification_failed"),
        ):
            hostile = self.result(requirement, outcome=outcome)
            hostile["guard_codes"] = [wrong_code]
            with self.subTest(outcome=outcome), self.assertRaises(
                ProductEvidenceError
            ):
                validate_product_evidence_result(hostile)

    def test_checked_in_fixtures_are_accepted_by_cli(self) -> None:
        self.assertEqual(cli_main(["fixture-check", "--fixture", str(REPORT_FIXTURE)]), 0)
        self.assertEqual(cli_main(["fixture-check", "--fixture", str(EVIDENCE_FIXTURE)]), 0)


if __name__ == "__main__":
    unittest.main()
