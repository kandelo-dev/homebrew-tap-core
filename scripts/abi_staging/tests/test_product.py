from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import unittest

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cli import main as cli_main
from scripts.abi_staging.custody import (
    build_miniature_source_custody_manifest_fixture,
    source_capsule_digest,
)
from scripts.abi_staging.plan import exact_formula_subject
from scripts.abi_staging.policy import VerificationTestDefinitionV1
from scripts.abi_staging.product import (
    ArchiveArtifactV1,
    CandidateProductArtifactV1,
    PackageArtifactV1,
    ProductInputResolutionError,
    RepositoryArtifactV1,
    ToolchainArtifactV1,
    load_resolved_product_inputs,
    resolve_product_inputs,
)
from scripts.abi_staging.product_evidence import (
    PRODUCT_CANDIDATE_MEDIA_TYPE,
    build_candidate_product_oci_plan,
    candidate_product_repository,
)
from scripts.abi_staging.reconcile import load_product_evidence_activation
from scripts.abi_staging.scheduler import (
    CandidateFactV1,
    SchedulingRecordsV1,
    VerificationFactV1,
)
from scripts.abi_staging.tests.test_plan import _plan, _request


TAP_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = TAP_ROOT / "Kandelo/staging/fixtures/product/resolved-inputs.json"
ACTIVATION = TAP_ROOT / "Kandelo/staging/product-evidence-activation.toml"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _digest_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _artifact_reference(label: str, digest: str) -> str:
    return f"https://artifacts.example.test/{label}?sha256={digest}"


def _manifest(
    product_id: str,
    *,
    product_inputs: list[dict[str, object]] | None = None,
    repositories: list[dict[str, object]] | None = None,
    homebrew: list[dict[str, object]] | None = None,
    packages: list[dict[str, object]] | None = None,
    archives: list[dict[str, object]] | None = None,
    toolchains: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema": 1,
        "id": product_id,
        "architecture": "wasm32",
        "output": f"{product_id}.vfs",
        "builder": f"images/vfs/scripts/build-{product_id}.sh",
        "composition": {
            "product": product_inputs or [],
            "repository": repositories or [],
        },
        "software": {
            "homebrew": homebrew or [],
            "package": packages or [],
            "archive": archives or [],
            "toolchain": toolchains or [],
        },
        "mounts": [{"path": "/", "source": "built-image", "readonly": False}],
        "boot": {
            "argv": ["/bin/sh"],
            "cwd": "/",
            "uid": 0,
            "gid": 0,
            "env": {"PATH": "/usr/bin:/bin"},
        },
        "evidence": {"node": {"test": f"{product_id}-node"}},
    }


def _catalog() -> dict[str, object]:
    alpha = _manifest(
        "alpha-shell",
        product_inputs=[{"id": "beta-tools", "materialization": "lazy"}],
        repositories=[
            {
                "id": "alpha-config",
                "paths": ["config/alpha.json"],
                "role": "runtime",
                "materialization": "embedded",
            }
        ],
        homebrew=[
            {
                "tap": "kandelo-dev/homebrew-tap-core",
                "formulae": ["curl"],
                "materialization": "lazy",
            }
        ],
        packages=[
            {
                "name": "shared-runtime",
                "outputs": ["runtime"],
                "source_roles": [],
                "role": "runtime",
                "materialization": "lazy",
            }
        ],
        archives=[
            {
                "id": "alpha-source",
                "url": "https://sources.example.test/alpha.tar.gz",
                "sha256": _digest("alpha-source"),
                "role": "runtime",
                "materialization": "embedded",
            }
        ],
    )
    beta = _manifest(
        "beta-tools",
        repositories=[
            {
                "id": "beta-build-fixtures",
                "paths": ["tests/beta"],
                "role": "build",
            }
        ],
        homebrew=[
            {
                "tap": "kandelo-dev/homebrew-tap-core",
                "formulae": ["bash", "libcurl"],
                "materialization": "embedded",
            }
        ],
        packages=[
            {
                "name": "shared-runtime",
                "outputs": ["runtime"],
                "source_roles": ["metadata"],
                "role": "runtime",
                "materialization": "embedded",
            },
            {
                "name": "kernel",
                "outputs": ["kernel"],
                "source_roles": [],
                "role": "build",
            },
        ],
        toolchains=[
            {
                "id": "sdk-headers",
                "provider": "repository-dev-shell",
                "component": "sdk-headers",
                "role": "runtime",
                "materialization": "embedded",
            }
        ],
    )
    products = []
    for manifest in (alpha, beta):
        products.append(
            {
                "path": f"images/vfs/products/{manifest['id']}.toml",
                "sha256": canonical_sha256(manifest),
                "manifest": manifest,
            }
        )
    return {"schema": 1, "kind": "kandelo-vfs-product-catalog", "products": products}


def _successful_receipt(
    *,
    request_sha256: str,
    source: dict[str, str],
    candidate_digest: str,
    layer: dict[str, object],
    definition: VerificationTestDefinitionV1,
) -> dict[str, object]:
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-verification",
        "common": {
            "request_sha256": request_sha256,
            "subject": {"kind": "candidate", "identity": candidate_digest},
            "source": source,
            "run": {
                "repository": "kandelo-dev/homebrew-tap-core",
                "workflow_ref": ".github/workflows/abi-staging-reconcile.yml@refs/heads/main",
                "run_id": 300,
                "run_attempt": 1,
                "job": "verify-candidate",
            },
            "guard_codes": [],
            "work_state": "complete",
            "outcome": "success",
            "artifact_class": "none",
            "promotion_state": "eligible",
            "retry_state": {
                "attempts": 1,
                "eligible": False,
                "exhausted": False,
                "next_action": "none",
            },
            "blockers": [],
        },
        "verification": {
            "candidate_record_sha256": candidate_digest,
            "candidate_layer": layer,
            "test_definition_sha256": definition.sha256,
            "host": "build",
            "attempt_ordinal": 0,
            "diagnostics": [],
        },
    }


class ProductInputResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = _catalog()
        self.request = _request()
        products = [
            {
                "id": entry["manifest"]["id"],
                "path": entry["path"],
                "manifest_sha256": entry["sha256"],
            }
            for entry in self.catalog["products"]
        ]
        self.request["requirements"]["products"] = products
        self.request["requirements"]["digest"] = canonical_sha256(
            {
                key: self.request["requirements"][key]
                for key in ("change_classes", "products", "registries", "evidence")
            }
        )
        self.request_sha256 = canonical_sha256(self.request)
        self.tap_plan = _plan(request=self.request)
        for formula in self.tap_plan["formulae"]:
            if formula["work_class"] == "required":
                formula["contract_sha256"] = _digest(
                    "contract-"
                    + exact_formula_subject(
                        formula["identity"]["name"],
                        formula["identity"]["architecture"],
                    )
                )

        self.definition = VerificationTestDefinitionV1(
            id="bottle-structure",
            hosts=("build",),
            kandelo_paths=("scripts/homebrew-inspect-bottle.py",),
            policy="kandelo-bottle-structure-v1",
            sha256=_digest("bottle-structure-definition"),
        )
        self.source = copy.deepcopy(self.request["build_source"])
        self.candidate_records: dict[str, dict[str, object]] = {}
        self.candidate_locators: dict[str, dict[str, str]] = {}
        self.source_custody_records: dict[str, dict[str, object]] = {}
        candidate_facts = []
        verification_facts = []
        self.verification_records: dict[str, dict[str, object]] = {}
        self.verification_locators: dict[str, dict[str, str]] = {}
        for formula in self.tap_plan["formulae"]:
            if formula["work_class"] != "required":
                continue
            identity = formula["identity"]
            name = identity["name"]
            subject = exact_formula_subject(name, identity["architecture"])
            layer_sha256 = _digest(f"layer-{subject}")
            contract_sha256 = formula["contract_sha256"]
            record_sha256 = _digest(f"candidate-record-{subject}")
            source_sha256 = _digest(f"source-custody-{subject}")
            custody = build_miniature_source_custody_manifest_fixture()
            custody["request_sha256"] = self.request_sha256
            custody["subject"] = subject
            for role, exact_source in (
                ("kandelo", self.source),
                ("tap", self.tap_plan["tap_source"]),
            ):
                custody_source = next(
                    item for item in custody["sources"] if item["role"] == role
                )
                for key in ("repository", "commit", "tree"):
                    custody_source[key] = exact_source[key]
            custody["capsule_sha256"] = source_capsule_digest(custody)
            self.source_custody_records[source_sha256] = custody
            candidate_repository = (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/" + name
            )
            layer = {
                "sha256": layer_sha256,
                "bytes": 100 + len(name),
                "immutable_reference": f"{candidate_repository}@sha256:{layer_sha256}",
            }
            bottle_metadata_sha256 = _digest(f"bottle-metadata-{subject}")
            candidate = {
                "schema": 1,
                "kind": "kandelo-abi-staging-candidate",
                "common": {
                    "request_sha256": self.request_sha256,
                    "subject": {
                        "kind": "candidate",
                        "identity": (
                            f"kandelo-dev/homebrew-tap-core/{name}@sha256:{layer_sha256}"
                        ),
                    },
                    "source": copy.deepcopy(self.source),
                    "run": {
                        "repository": "kandelo-dev/homebrew-tap-core",
                        "workflow_ref": ".github/workflows/abi-staging-reconcile.yml@refs/heads/main",
                        "run_id": 200,
                        "run_attempt": 1,
                        "job": "publish-candidate",
                    },
                    "guard_codes": [],
                    "work_state": "complete",
                    "outcome": "success",
                    "artifact_class": "candidate",
                    "artifact": layer,
                    "promotion_state": "unknown",
                    "retry_state": {
                        "attempts": 1,
                        "eligible": False,
                        "exhausted": False,
                        "next_action": "none",
                    },
                    "blockers": [],
                },
                "candidate": {
                    "formula": {
                        "tap": "kandelo-dev/homebrew-tap-core",
                        "formula": name,
                        "version": identity["version"],
                        "revision": identity["revision"],
                        "bottle_rebuild": identity["rebuild"],
                        "architecture": identity["architecture"],
                        "target_abi": self.request["target_abi"]["version"],
                        "bottle_contract_sha256": contract_sha256,
                    },
                    "bottle_layer": layer,
                    "normalized_components": [
                        {
                            "id": "bottle-contract",
                            "artifact": {
                                "sha256": contract_sha256,
                                "bytes": 64,
                                "immutable_reference": (
                                    f"{candidate_repository}@sha256:{contract_sha256}"
                                ),
                            },
                        },
                        {
                            "id": "bottle-metadata",
                            "artifact": {
                                "sha256": bottle_metadata_sha256,
                                "bytes": 80 + len(name),
                                "immutable_reference": (
                                    f"{candidate_repository}@sha256:{bottle_metadata_sha256}"
                                ),
                            },
                        },
                        {
                            "id": "source-custody",
                            "artifact": {
                                "sha256": source_sha256,
                                "bytes": 64,
                                "immutable_reference": (
                                    "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-sources/"
                                    f"{name}@sha256:{source_sha256}"
                                ),
                            },
                        },
                    ],
                    "direct_dependency_layers": [],
                    "source_custody_sha256": source_sha256,
                    "producer": {
                        "request_sha256": self.request_sha256,
                        "head": self.source["commit"],
                        "run_id": 200,
                    },
                    "nonendorsed": True,
                },
            }
            self.candidate_records[record_sha256] = candidate
            self.candidate_locators[record_sha256] = {
                "repository": candidate_repository,
                "digest": f"sha256:{record_sha256}",
                "immutable_reference": f"{candidate_repository}@sha256:{record_sha256}",
            }
            candidate_facts.append(
                CandidateFactV1(
                    request_sha256=self.request_sha256,
                    subject=subject,
                    contract_sha256=contract_sha256,
                    record_sha256=record_sha256,
                    bottle_layer_sha256=layer_sha256,
                )
            )
            receipt_sha256 = _digest(f"receipt-{subject}")
            receipt = _successful_receipt(
                request_sha256=self.request_sha256,
                source=self.source,
                candidate_digest=record_sha256,
                layer=layer,
                definition=self.definition,
            )
            self.verification_records[receipt_sha256] = receipt
            receipt_repository = candidate_repository + "/receipts/bottle-structure/build"
            self.verification_locators[receipt_sha256] = {
                "repository": receipt_repository,
                "digest": f"sha256:{receipt_sha256}",
                "immutable_reference": f"{receipt_repository}@sha256:{receipt_sha256}",
            }
            verification_facts.append(
                VerificationFactV1(
                    request_sha256=self.request_sha256,
                    subject=subject,
                    candidate_record_sha256=record_sha256,
                    test_definition_sha256=self.definition.sha256,
                    host="build",
                    outcome="success",
                    guard_code=None,
                    attempt_ordinal=0,
                    completed_at="2026-08-09T10:00:00.000Z",
                    record_sha256=receipt_sha256,
                )
            )
        self.records = SchedulingRecordsV1(
            attempts=(),
            candidates=tuple(candidate_facts),
            verifications=tuple(verification_facts),
        )

        target = self.request["target_abi"]
        policy_sha256 = _digest("product-build-policy")
        self.dev_shell_lock_sha256 = _digest("flake-lock")
        self.runtime_bundle = {
            "schema": 1,
            "kind": "kandelo-exact-runtime-bundle",
            "source": copy.deepcopy(self.source),
            "target_abi": copy.deepcopy(target),
            "kernel": {
                "wasm_sha256": _digest("kernel-wasm"),
                "bytes": 1024,
                "abi_version": target["version"],
                "snapshot_sha256": target["snapshot_sha256"],
            },
            "host": {
                "bundle_sha256": _digest("host-bundle"),
                "bytes": 2048,
                "generated_abi_sha256": _digest("generated-abi"),
                "worker_protocol_sha256": _digest("worker-protocol"),
            },
            "browser": {
                "bundle_sha256": _digest("browser-bundle"),
                "bytes": 4096,
                "service_worker_sha256": _digest("service-worker"),
            },
            "build_policy_sha256": policy_sha256,
            "inventory": [
                {
                    "path": "flake.lock",
                    "sha256": self.dev_shell_lock_sha256,
                    "bytes": 512,
                }
            ],
        }

        common_package = {
            "architecture": "wasm32",
            "target_abi": target["version"],
            "snapshot_sha256": target["snapshot_sha256"],
            "source_repository": self.source["repository"],
            "source_commit": self.source["commit"],
            "source_tree": self.source["tree"],
            "build_policy_sha256": policy_sha256,
        }
        self.package_artifacts = (
            PackageArtifactV1(
                package="shared-runtime",
                selector_kind="output",
                selector="runtime",
                sha256=_digest("shared-runtime-output"),
                bytes=501,
                immutable_reference=_artifact_reference(
                    "shared-runtime-output", _digest("shared-runtime-output")
                ),
                **common_package,
            ),
            PackageArtifactV1(
                package="shared-runtime",
                selector_kind="source-role",
                selector="metadata",
                sha256=_digest("shared-runtime-metadata"),
                bytes=502,
                immutable_reference=_artifact_reference(
                    "shared-runtime-metadata", _digest("shared-runtime-metadata")
                ),
                **common_package,
            ),
            PackageArtifactV1(
                package="kernel",
                selector_kind="output",
                selector="kernel",
                sha256=_digest("kernel-package-output"),
                bytes=503,
                immutable_reference=_artifact_reference(
                    "kernel-package-output", _digest("kernel-package-output")
                ),
                **common_package,
            ),
        )
        self.archive_artifacts = (
            ArchiveArtifactV1(
                product_id="alpha-shell",
                id="alpha-source",
                url="https://sources.example.test/alpha.tar.gz",
                sha256=_digest("alpha-source"),
                bytes=601,
                immutable_reference=_artifact_reference(
                    "alpha-source", _digest("alpha-source")
                ),
            ),
        )
        self.toolchain_artifacts = (
            ToolchainArtifactV1(
                product_id="beta-tools",
                id="sdk-headers",
                provider="repository-dev-shell",
                component="sdk-headers",
                architecture="wasm32",
                source_repository=self.source["repository"],
                source_commit=self.source["commit"],
                source_tree=self.source["tree"],
                dev_shell_lock_sha256=self.dev_shell_lock_sha256,
                build_policy_sha256=policy_sha256,
                sha256=_digest("sdk-headers"),
                bytes=701,
                immutable_reference=_artifact_reference(
                    "sdk-headers", _digest("sdk-headers")
                ),
            ),
        )
        self.repository_artifacts = (
            RepositoryArtifactV1(
                product_id="alpha-shell",
                id="alpha-config",
                paths=("config/alpha.json",),
                architecture="wasm32",
                source_repository=self.source["repository"],
                source_commit=self.source["commit"],
                source_tree=self.source["tree"],
                sha256=_digest("alpha-config"),
                bytes=801,
                immutable_reference=_artifact_reference(
                    "alpha-config", _digest("alpha-config")
                ),
            ),
            RepositoryArtifactV1(
                product_id="beta-tools",
                id="beta-build-fixtures",
                paths=("tests/beta",),
                architecture="wasm32",
                source_repository=self.source["repository"],
                source_commit=self.source["commit"],
                source_tree=self.source["tree"],
                sha256=_digest("beta-build-fixtures"),
                bytes=802,
                immutable_reference=_artifact_reference(
                    "beta-build-fixtures", _digest("beta-build-fixtures")
                ),
            ),
        )
        beta_entry = next(
            entry
            for entry in self.catalog["products"]
            if entry["manifest"]["id"] == "beta-tools"
        )
        product_sha256 = _digest("beta-product-layer")
        product_repository = (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/products/beta-tools"
        )
        self.product_artifacts = (
            CandidateProductArtifactV1(
                product_id="beta-tools",
                manifest_sha256=beta_entry["sha256"],
                architecture="wasm32",
                request_sha256=self.request_sha256,
                source_repository=self.source["repository"],
                source_commit=self.source["commit"],
                source_tree=self.source["tree"],
                target_abi=target["version"],
                snapshot_sha256=target["snapshot_sha256"],
                vfs_layer_sha256=product_sha256,
                vfs_layer_bytes=901,
                immutable_reference=f"{product_repository}@sha256:{product_sha256}",
                builder_report_sha256=_digest("beta-builder-report"),
            ),
        )

    def resolve(self, **changes):
        arguments = {
            "request": self.request,
            "request_sha256": self.request_sha256,
            "catalog": self.catalog,
            "tap_plan": self.tap_plan,
            "records": self.records,
            "candidate_records": self.candidate_records,
            "candidate_locators": self.candidate_locators,
            "source_custody_records": self.source_custody_records,
            "reuse_records": {},
            "verification_records": self.verification_records,
            "verification_locators": self.verification_locators,
            "verification_tests": (self.definition,),
            "runtime_bundle": self.runtime_bundle,
            "product_artifacts": self.product_artifacts,
            "package_artifacts": self.package_artifacts,
            "archive_artifacts": self.archive_artifacts,
            "toolchain_artifacts": self.toolchain_artifacts,
            "repository_artifacts": self.repository_artifacts,
        }
        arguments.update(changes)
        return resolve_product_inputs(**arguments)

    def test_resolves_every_input_kind_from_exact_selected_authority(self) -> None:
        resolutions = self.resolve()
        self.assertEqual([item.plan.product_id for item in resolutions], ["beta-tools", "alpha-shell"])
        all_kinds = {
            entry["kind"]
            for resolution in resolutions
            for entry in resolution.resolved_inputs["inputs"]
        }
        self.assertEqual(
            all_kinds,
            {
                "product-image",
                "homebrew-bottle",
                "package-output",
                "source-archive",
                "toolchain-output",
                "repository-path",
            },
        )
        for resolution in resolutions:
            self.assertEqual(
                resolution.plan.resolved_inputs_sha256,
                canonical_sha256(resolution.resolved_inputs),
            )
            self.assertEqual(resolution.resolved_inputs["reference_class"], "candidate")
            self.assertEqual(resolution.resolved_inputs["source"], self.source)
            self.assertEqual(
                resolution.plan.runtime_bundle_sha256,
                canonical_sha256(self.runtime_bundle),
            )
            self.assertEqual(
                list(resolution.plan.required_formula_subjects),
                sorted(resolution.plan.required_formula_subjects),
            )

    def test_resolver_plan_is_accepted_by_candidate_product_publication(self) -> None:
        runtime_files = {
            "browser/dist/bundle.js": b"browser bundle\n",
            "browser/dist/service-worker.js": b"service worker\n",
            "flake.lock": b"flake-lock",
            "host/dist/bundle.js": b"host runtime bundle\n",
            "host/generated-abi.ts": b"generated ABI\n",
            "host/worker-protocol.ts": b"worker protocol\n",
            "kernel.wasm": b"\x00asm miniature kernel\n",
        }
        inventory = [
            {"path": path, "sha256": _digest_bytes(body), "bytes": len(body)}
            for path, body in sorted(runtime_files.items())
        ]
        host_inventory = [
            item for item in inventory if item["path"].startswith("host/")
        ]
        browser_inventory = [
            item for item in inventory if item["path"].startswith("browser/")
        ]
        target = self.request["target_abi"]
        runtime_bundle = {
            "schema": 1,
            "kind": "kandelo-exact-runtime-bundle",
            "source": copy.deepcopy(self.source),
            "target_abi": copy.deepcopy(target),
            "kernel": {
                "wasm_sha256": _digest_bytes(runtime_files["kernel.wasm"]),
                "bytes": len(runtime_files["kernel.wasm"]),
                "abi_version": target["version"],
                "snapshot_sha256": target["snapshot_sha256"],
            },
            "host": {
                "bundle_sha256": _digest_bytes(canonical_bytes(host_inventory)),
                "bytes": sum(item["bytes"] for item in host_inventory),
                "generated_abi_sha256": _digest_bytes(
                    runtime_files["host/generated-abi.ts"]
                ),
                "worker_protocol_sha256": _digest_bytes(
                    runtime_files["host/worker-protocol.ts"]
                ),
            },
            "browser": {
                "bundle_sha256": _digest_bytes(canonical_bytes(browser_inventory)),
                "bytes": sum(item["bytes"] for item in browser_inventory),
                "service_worker_sha256": _digest_bytes(
                    runtime_files["browser/dist/service-worker.js"]
                ),
            },
            "build_policy_sha256": self.runtime_bundle["build_policy_sha256"],
            "inventory": inventory,
        }
        self.assertEqual(
            _digest_bytes(runtime_files["flake.lock"]),
            self.dev_shell_lock_sha256,
        )
        resolution = next(
            item
            for item in self.resolve(runtime_bundle=runtime_bundle)
            if item.plan.product_id == "alpha-shell"
        )
        resolved = resolution.resolved_inputs
        resolved_body = canonical_bytes(resolved)
        vfs_body = b"resolved product miniature VFS\n"
        report_inputs = []
        for item in resolved["inputs"]:
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
        builder_report = {
            "schema": 1,
            "kind": "kandelo-vfs-builder-report",
            "product": copy.deepcopy(resolved["product"]),
            "resolved_inputs_sha256": canonical_sha256(resolved),
            "inputs": report_inputs,
            "capture": {"complete": True, "unreported_reads": []},
            "output": {
                "abi": copy.deepcopy(resolved["target_abi"]),
                "bytes": len(vfs_body),
                "name": resolved["product"]["output"],
                "path": resolved["product"]["output"],
                "sha256": _digest_bytes(vfs_body),
            },
        }
        repository = candidate_product_repository(
            owner="kandelo-dev",
            repository_prefix="homebrew-tap-core-abi-",
            candidate_suffix="-candidates",
            target_abi=target["version"],
            product_id=resolution.plan.product_id,
        )

        publication = build_candidate_product_oci_plan(
            repository=repository,
            publisher_repository="kandelo-dev/homebrew-tap-core",
            input_plan=resolution.plan,
            vfs_body=vfs_body,
            builder_report_body=canonical_bytes(builder_report),
            resolved_inputs_body=resolved_body,
            runtime_bundle_body=canonical_bytes(runtime_bundle),
            runtime_files=runtime_files,
        )

        self.assertEqual(publication.artifact_type, PRODUCT_CANDIDATE_MEDIA_TYPE)
        self.assertTrue(resolution.plan.required_formula_subjects)

    def test_preserves_lazy_embedded_build_only_and_records_shared_roots(self) -> None:
        by_product = {item.plan.product_id: item for item in self.resolve()}
        alpha = by_product["alpha-shell"]
        beta = by_product["beta-tools"]
        alpha_inputs = {item["id"]: item for item in alpha.resolved_inputs["inputs"]}
        beta_inputs = {item["id"]: item for item in beta.resolved_inputs["inputs"]}

        product_input = next(item for item in alpha_inputs.values() if item["kind"] == "product-image")
        self.assertEqual(product_input["effective_materialization"], "lazy-reference")
        self.assertNotIn("path", product_input)
        curl = next(
            item
            for item in alpha_inputs.values()
            if item["kind"] == "homebrew-bottle" and "curl" in item["id"] and "libcurl" not in item["id"]
        )
        self.assertEqual(curl["effective_materialization"], "lazy-reference")
        self.assertNotIn("path", curl)
        self.assertEqual(
            set(curl["descriptor"]),
            {"bytes", "path", "reference", "sha256"},
        )
        self.assertIn("-metadata-sha256-", curl["descriptor"]["path"])
        libcurl = next(
            item
            for item in alpha_inputs.values()
            if item["kind"] == "homebrew-bottle" and "libcurl" in item["id"]
        )
        self.assertEqual(libcurl["declared_materialization"], "lazy")
        self.assertEqual(libcurl["effective_materialization"], "embedded")
        self.assertIn("path", libcurl)
        roots = {
            (root.requesting_product_id, root.root_id, root.materialization)
            for root in alpha.input_requests
            if root.input_id == libcurl["id"]
        }
        self.assertEqual(
            roots,
            {("alpha-shell", "curl", "lazy"), ("beta-tools", "libcurl", "embedded")},
        )
        shared = next(
            item
            for item in alpha_inputs.values()
            if item["kind"] == "package-output" and "shared-runtime" in item["id"]
        )
        self.assertEqual(shared["declared_materialization"], "lazy")
        self.assertEqual(shared["effective_materialization"], "embedded")
        build_only = next(
            item
            for item in beta_inputs.values()
            if item["kind"] == "package-output" and "kernel" in item["id"]
        )
        self.assertEqual(build_only["effective_materialization"], "build-only")

    def test_rejects_unverified_or_identity_drifting_inputs(self) -> None:
        cases = {}
        cases["missing verification policy"] = {"verification_tests": ()}
        cases["unverified bottle"] = {
            "records": replace(self.records, verifications=self.records.verifications[1:])
        }
        wrong_arch = copy.deepcopy(self.candidate_records)
        first_record = next(iter(wrong_arch.values()))
        first_record["candidate"]["formula"]["architecture"] = "wasm64"
        cases["wrong bottle architecture"] = {"candidate_records": wrong_arch}
        wrong_abi = copy.deepcopy(self.candidate_records)
        next(iter(wrong_abi.values()))["candidate"]["formula"]["target_abi"] += 1
        cases["wrong bottle ABI"] = {"candidate_records": wrong_abi}
        stale = copy.deepcopy(self.candidate_records)
        next(iter(stale.values()))["common"]["source"]["tree"] = "f" * 40
        cases["stale custody"] = {"candidate_records": stale}
        missing_metadata = copy.deepcopy(self.candidate_records)
        first_missing = next(iter(missing_metadata.values()))
        first_missing["candidate"]["normalized_components"] = [
            item
            for item in first_missing["candidate"]["normalized_components"]
            if item["id"] != "bottle-metadata"
        ]
        cases["missing bottle metadata"] = {
            "candidate_records": missing_metadata
        }
        stale_capsule = copy.deepcopy(self.source_custody_records)
        first_capsule = next(iter(stale_capsule.values()))
        next(
            item for item in first_capsule["sources"] if item["role"] == "kandelo"
        )["tree"] = "e" * 40
        first_capsule["capsule_sha256"] = source_capsule_digest(first_capsule)
        cases["stale source custody capsule"] = {
            "source_custody_records": stale_capsule
        }
        missing_capsule = dict(self.source_custody_records)
        missing_capsule.pop(next(iter(missing_capsule)))
        cases["missing source custody capsule"] = {
            "source_custody_records": missing_capsule
        }
        cases["missing package output"] = {"package_artifacts": self.package_artifacts[1:]}
        cases["archive SHA drift"] = {
            "archive_artifacts": (
                replace(
                    self.archive_artifacts[0],
                    sha256="f" * 64,
                    immutable_reference=_artifact_reference("alpha-source", "f" * 64),
                ),
            )
        }
        cases["ambient toolchain"] = {
            "toolchain_artifacts": (
                replace(self.toolchain_artifacts[0], dev_shell_lock_sha256="f" * 64),
            )
        }
        cases["wrong repository tree"] = {
            "repository_artifacts": (
                replace(self.repository_artifacts[0], source_tree="f" * 40),
                self.repository_artifacts[1],
            )
        }
        canonical = copy.deepcopy(self.candidate_records)
        record = next(iter(canonical.values()))
        canonical_reference = record["candidate"]["bottle_layer"]["immutable_reference"].replace(
            "-candidates/", "/"
        )
        record["candidate"]["bottle_layer"]["immutable_reference"] = canonical_reference
        record["common"]["artifact"]["immutable_reference"] = canonical_reference
        cases["canonical bottle reference"] = {"candidate_records": canonical}

        for name, changes in cases.items():
            with self.subTest(name=name), self.assertRaises(ProductInputResolutionError):
                self.resolve(**changes)

    def test_current_reuse_binding_accepts_historical_producer_without_rewriting_it(self) -> None:
        candidate_facts = list(self.records.candidates)
        original_fact = candidate_facts[0]
        candidate_records = copy.deepcopy(self.candidate_records)
        candidate = candidate_records[original_fact.record_sha256]
        historical_request = _digest("historical-request")
        historical_source = {
            "repository": self.source["repository"],
            "commit": "8" * 40,
            "tree": "9" * 40,
        }
        candidate["common"]["request_sha256"] = historical_request
        candidate["common"]["source"] = historical_source
        candidate["candidate"]["producer"]["request_sha256"] = historical_request
        candidate["candidate"]["producer"]["head"] = historical_source["commit"]

        custody_link = next(
            item["artifact"]
            for item in candidate["candidate"]["normalized_components"]
            if item["id"] == "source-custody"
        )
        custody_records = copy.deepcopy(self.source_custody_records)
        custody = custody_records[custody_link["sha256"]]
        custody["request_sha256"] = historical_request
        custody_source = next(
            item for item in custody["sources"] if item["role"] == "kandelo"
        )
        for key in ("repository", "commit", "tree"):
            custody_source[key] = historical_source[key]
        custody["capsule_sha256"] = source_capsule_digest(custody)

        binding_sha256 = _digest("current-reuse-binding")
        candidate_facts[0] = replace(
            original_fact,
            request_sha256=self.request_sha256,
            binding_record_sha256=binding_sha256,
        )
        formula = candidate["candidate"]["formula"]
        layer = candidate["candidate"]["bottle_layer"]
        candidate_locator = self.candidate_locators[original_fact.record_sha256]
        receipt_sha256 = next(
            fact.record_sha256
            for fact in self.records.verifications
            if fact.candidate_record_sha256 == original_fact.record_sha256
        )
        reuse = {
            "schema": 1,
            "kind": "kandelo-abi-staging-candidate-reuse",
            "common": {
                "request_sha256": self.request_sha256,
                "subject": {
                    "kind": "formula",
                    "identity": f"{formula['tap']}/{formula['formula']}",
                    "architecture": formula["architecture"],
                },
                "source": copy.deepcopy(self.source),
                "run": {
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "workflow_ref": ".github/workflows/abi-staging-reconcile.yml@refs/heads/main",
                    "run_id": 400,
                    "run_attempt": 1,
                    "job": "publish-reuse",
                },
                "guard_codes": [],
                "work_state": "complete",
                "outcome": "success",
                "artifact_class": "candidate",
                "artifact": copy.deepcopy(layer),
                "promotion_state": "eligible",
                "retry_state": {
                    "attempts": 0,
                    "eligible": False,
                    "exhausted": False,
                    "next_action": "none",
                },
                "blockers": [],
            },
            "candidate_reuse": {
                "formula": {
                    key: formula[key]
                    for key in (
                        "tap",
                        "formula",
                        "architecture",
                        "target_abi",
                        "bottle_contract_sha256",
                    )
                },
                "existing_candidate": {
                    "record_sha256": original_fact.record_sha256,
                    "immutable_reference": candidate_locator["immutable_reference"],
                },
                "bottle_layer": copy.deepcopy(layer),
                "source_custody": {
                    "record_sha256": custody_link["sha256"],
                    "immutable_reference": custody_link["immutable_reference"],
                },
                "qualifying_receipts": [
                    {
                        "record_sha256": receipt_sha256,
                        "immutable_reference": self.verification_locators[receipt_sha256][
                            "immutable_reference"
                        ],
                    }
                ],
                "original_producer": copy.deepcopy(candidate["candidate"]["producer"]),
                "nonendorsed": True,
            },
        }
        records = replace(self.records, candidates=tuple(candidate_facts))
        self.resolve(
            records=records,
            candidate_records=candidate_records,
            source_custody_records=custody_records,
            reuse_records={binding_sha256: reuse},
        )
        self.assertEqual(
            candidate["candidate"]["producer"]["request_sha256"], historical_request
        )

        hostile = copy.deepcopy(reuse)
        hostile["common"]["source"]["tree"] = "f" * 40
        with self.assertRaises(ProductInputResolutionError):
            self.resolve(
                records=records,
                candidate_records=candidate_records,
                source_custody_records=custody_records,
                reuse_records={binding_sha256: hostile},
            )

    def test_only_manifest_roots_and_request_products_can_add_inputs(self) -> None:
        baseline = self.resolve()
        signature = inspect.signature(resolve_product_inputs)
        forbidden = {
            "brewfiles",
            "package_dependencies",
            "legacy_arrays",
            "evidence_definitions",
            "workflow_matrix",
            "pages_registry",
            "test_registry",
            "background_inventory",
        }
        self.assertTrue(forbidden.isdisjoint(signature.parameters))

        extra_catalog = copy.deepcopy(self.catalog)
        unselected = _manifest("unselected-product")
        extra_catalog["products"].append(
            {
                "path": "images/vfs/products/unselected-product.toml",
                "sha256": canonical_sha256(unselected),
                "manifest": unselected,
            }
        )
        self.assertEqual(
            [item.resolved_inputs for item in self.resolve(catalog=extra_catalog)],
            [item.resolved_inputs for item in baseline],
        )

        tampered_plan = copy.deepcopy(self.tap_plan)
        alpha = next(item for item in tampered_plan["selected_products"] if item["id"] == "alpha-shell")
        alpha["formula_roots"].append(
            {
                "tap": "kandelo-dev/homebrew-tap-core",
                "formula": "asa",
                "architecture": "wasm32",
                "materialization": "embedded",
            }
        )
        alpha["formula_roots"].sort(key=lambda item: tuple(item.values()))
        with self.assertRaises(ProductInputResolutionError):
            self.resolve(tap_plan=tampered_plan)

    def test_selected_manifest_change_changes_inputs_and_stale_binding_fails(self) -> None:
        changed = copy.deepcopy(self.catalog)
        alpha = next(item for item in changed["products"] if item["manifest"]["id"] == "alpha-shell")
        alpha["manifest"]["software"]["archive"].append(
            {
                "id": "second-source",
                "url": "https://sources.example.test/second.tar.gz",
                "sha256": _digest("second-source"),
                "role": "runtime",
                "materialization": "lazy",
            }
        )
        alpha["sha256"] = canonical_sha256(alpha["manifest"])
        with self.assertRaisesRegex(ProductInputResolutionError, "request.*manifest"):
            self.resolve(catalog=changed)

    def test_generic_miniature_successor_uses_request_target(self) -> None:
        previous = self.request["informational_context"]["previous_abi"]
        target = self.request["target_abi"]["version"]
        self.assertEqual(target, previous + 1)
        for resolution in self.resolve():
            self.assertEqual(resolution.resolved_inputs["target_abi"]["version"], target)

    def test_fixture_and_activation_are_strict_and_observe_only(self) -> None:
        document = load_resolved_product_inputs(FIXTURE.read_bytes())
        self.assertEqual(document["kind"], "kandelo-resolved-vfs-product-inputs")
        self.assertEqual(load_product_evidence_activation(ACTIVATION), "observe")
        self.assertEqual(cli_main(["fixture-check", "--fixture", str(FIXTURE)]), 0)

        hostile = json.loads(canonical_bytes(document))
        hostile["unexpected"] = True
        with self.assertRaises(ProductInputResolutionError):
            load_resolved_product_inputs(canonical_bytes(hostile))

    def test_every_lazy_reference_uses_the_candidate_namespace(self) -> None:
        embedded = json.loads(FIXTURE.read_bytes())
        archive = next(
            item for item in embedded["inputs"] if item["kind"] == "source-archive"
        )
        self.assertEqual(archive["effective_materialization"], "embedded")
        self.assertTrue(archive["reference"].startswith("https://"))
        load_resolved_product_inputs(canonical_bytes(embedded))

        lazy = copy.deepcopy(embedded)
        lazy_archive = next(
            item for item in lazy["inputs"] if item["kind"] == "source-archive"
        )
        lazy_archive["declared_materialization"] = "lazy"
        lazy_archive["effective_materialization"] = "lazy-reference"
        lazy_archive.pop("path")
        lazy_archive["reference"] = (
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
            f"lazy/archive@sha256:{lazy_archive['sha256']}"
        )
        load_resolved_product_inputs(canonical_bytes(lazy))

        lazy_archive["reference"] = archive["reference"]
        with self.assertRaisesRegex(
            ProductInputResolutionError,
            "candidate namespace",
        ):
            load_resolved_product_inputs(canonical_bytes(lazy))


if __name__ == "__main__":
    unittest.main()
