from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cli import main as cli_main
from scripts.abi_staging.oci import build_oci_manifest, fetch_public_record
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
        "browser/dist/bundle.js": b"browser bundle\n",
        "browser/dist/service-worker.js": b"service worker\n",
        "host/dist/bundle.js": b"host runtime bundle\n",
        "host/generated-abi.ts": b"generated ABI\n",
        "host/worker-protocol.ts": b"worker protocol\n",
        "kernel.wasm": b"\x00asm miniature kernel\n",
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
            {"vfs_image", "builder_report", "resolved_inputs", "runtime_bundle"},
        )
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
                    vfs_body=self.vfs,
                    builder_report_body=canonical_bytes(report),
                    resolved_inputs_body=canonical_bytes(self.inputs),
                    runtime_bundle_body=canonical_bytes(runtime),
                    runtime_files=files,
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
                vfs_body=self.vfs,
                builder_report_body=canonical_bytes(self.report),
                resolved_inputs_body=canonical_bytes(hostile),
                runtime_bundle_body=canonical_bytes(self.runtime),
                runtime_files=self.runtime_files,
            )


class ProductEvidenceRecordTests(ProductEvidenceFixture, unittest.TestCase):
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

    def test_checked_in_fixtures_are_accepted_by_cli(self) -> None:
        self.assertEqual(cli_main(["fixture-check", "--fixture", str(REPORT_FIXTURE)]), 0)
        self.assertEqual(cli_main(["fixture-check", "--fixture", str(EVIDENCE_FIXTURE)]), 0)


if __name__ == "__main__":
    unittest.main()
