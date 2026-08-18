from __future__ import annotations

import copy
import base64
from dataclasses import replace
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

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
    build_product_handoff_inventory,
    RepositoryArtifactV1,
    ToolchainArtifactV1,
    load_product_build_result,
    load_product_input_object_inventory,
    load_resolved_product_inputs,
    materialize_resolved_product_input_objects,
    resolver_artifacts_from_input_inventory,
    resolve_product_from_checked_input_authority,
    resolve_product_inputs,
    selected_product_formula_readiness,
    select_product_execution_scope,
    select_product_input_build_spec,
    validate_product_input_object_authority,
    validate_private_product_authority_handoff,
    validate_product_build_handoff,
    write_product_build_handoff,
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
from scripts.abi_staging.tests.test_oci import FakeRegistryTransport


TAP_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = TAP_ROOT / "Kandelo/staging/fixtures/product/resolved-inputs.json"
ACTIVATION = TAP_ROOT / "Kandelo/staging/product-evidence-activation.toml"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _digest_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _artifact_reference(label: str, digest: str) -> str:
    return f"https://artifacts.example.test/{label}?sha256={digest}"


def _runtime_bundle(
    source: dict[str, str], target: dict[str, object], policy_sha256: str
) -> tuple[dict[str, object], dict[str, bytes]]:
    files = {
        "browser/dist/abi-staging/browser-host.js": b"exact browser host\n",
        "browser/dist/abi-staging-harness/index.html": b"protected harness\n",
        "browser/dist/assets/kernel-fixture.wasm": b"\x00asm miniature kernel\n",
        "browser/dist/bundle.js": b"browser bundle\n",
        "browser/dist/service-worker.js": b"service worker\n",
        "flake.lock": b"flake-lock",
        "host/dist/bundle.js": b"host runtime bundle\n",
        "host/generated-abi.ts": b"generated ABI\n",
        "host/worker-protocol.ts": b"worker protocol\n",
        "kernel.wasm": b"\x00asm miniature kernel\n",
        # musl installs intentional empty architecture headers whose presence
        # is part of the exact compiler sysroot contract.
        "toolchain/wasm32-sysroot/include/bits/ioctl_fix.h": b"",
    }
    inventory = [
        {"path": path, "sha256": _digest_bytes(body), "bytes": len(body)}
        for path, body in sorted(files.items())
    ]
    host_inventory = [
        item for item in inventory if item["path"].startswith("host/")
    ]
    browser_inventory = [
        item for item in inventory if item["path"].startswith("browser/")
    ]
    return (
        {
            "schema": 1,
            "kind": "kandelo-exact-runtime-bundle",
            "source": copy.deepcopy(source),
            "target_abi": copy.deepcopy(target),
            "kernel": {
                "wasm_sha256": _digest_bytes(files["kernel.wasm"]),
                "bytes": len(files["kernel.wasm"]),
                "abi_version": target["version"],
                "snapshot_sha256": target["snapshot_sha256"],
            },
            "host": {
                "bundle_sha256": _digest_bytes(canonical_bytes(host_inventory)),
                "bytes": sum(item["bytes"] for item in host_inventory),
                "generated_abi_sha256": _digest_bytes(
                    files["host/generated-abi.ts"]
                ),
                "worker_protocol_sha256": _digest_bytes(
                    files["host/worker-protocol.ts"]
                ),
            },
            "browser": {
                "bundle_sha256": _digest_bytes(canonical_bytes(browser_inventory)),
                "bytes": sum(item["bytes"] for item in browser_inventory),
                "harness_entry_bytes": len(
                    files["browser/dist/abi-staging-harness/index.html"]
                ),
                "harness_entry_path": (
                    "browser/dist/abi-staging-harness/index.html"
                ),
                "harness_entry_sha256": _digest_bytes(
                    files["browser/dist/abi-staging-harness/index.html"]
                ),
                "host_entry_bytes": len(
                    files["browser/dist/abi-staging/browser-host.js"]
                ),
                "host_entry_path": "browser/dist/abi-staging/browser-host.js",
                "host_entry_sha256": _digest_bytes(
                    files["browser/dist/abi-staging/browser-host.js"]
                ),
                "kernel_asset_path": "browser/dist/assets/kernel-fixture.wasm",
                "kernel_asset_sha256": _digest_bytes(
                    files["browser/dist/assets/kernel-fixture.wasm"]
                ),
                "service_worker_sha256": _digest_bytes(
                    files["browser/dist/service-worker.js"]
                ),
            },
            "build_policy_sha256": policy_sha256,
            "inventory": inventory,
        },
        files,
    )


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
            composition_descriptor_sha256 = _digest(
                f"vfs-composition-descriptor-{subject}"
            )
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
                        {
                            "id": "vfs-composition-descriptor",
                            "artifact": {
                                "sha256": composition_descriptor_sha256,
                                "bytes": 96 + len(name),
                                "immutable_reference": (
                                    f"{candidate_repository}@sha256:"
                                    f"{composition_descriptor_sha256}"
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
                    descriptor_capable=True,
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
        self.runtime_bundle, self.runtime_files = _runtime_bundle(
            self.source, target, policy_sha256
        )

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

    def test_formula_readiness_reuses_the_exact_product_resolver_authority(self) -> None:
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
        }
        self.assertEqual(
            selected_product_formula_readiness(**arguments),
            {"alpha-shell": True, "beta-tools": True},
        )

        curl = exact_formula_subject("curl", "wasm32")
        without_curl = replace(
            self.records,
            candidates=tuple(
                fact for fact in self.records.candidates if fact.subject != curl
            ),
        )
        self.assertEqual(
            selected_product_formula_readiness(
                **{**arguments, "records": without_curl}
            ),
            {"alpha-shell": False, "beta-tools": True},
        )

    def test_metadata_only_tap_movement_preserves_exact_candidates(self) -> None:
        moved_tap_plan = copy.deepcopy(self.tap_plan)
        moved_tap_plan["tap_source"]["commit"] = "e" * 40
        moved_tap_plan["tap_source"]["tree"] = "d" * 40

        resolved = self.resolve(tap_plan=moved_tap_plan)

        self.assertEqual(
            [item.plan.product_id for item in resolved],
            ["beta-tools", "alpha-shell"],
        )

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

    def test_checked_private_inventory_is_wired_into_the_protected_resolver(self) -> None:
        alpha_manifest = next(
            entry
            for entry in self.catalog["products"]
            if entry["manifest"]["id"] == "alpha-shell"
        )
        package = self.package_artifacts[0]
        archive = self.archive_artifacts[0]
        repository = self.repository_artifacts[0]
        checked_inventory = {
            "schema": 1,
            "kind": "kandelo-vfs-product-input-object-inventory",
            "product": {
                "id": "alpha-shell",
                "manifest_path": alpha_manifest["path"],
                "manifest_sha256": alpha_manifest["sha256"],
                "architecture": "wasm32",
            },
            "source": copy.deepcopy(self.source),
            "target_abi": copy.deepcopy(self.request["target_abi"]),
            "build_environment": {
                "policy_sha256": self.runtime_bundle["build_policy_sha256"],
                "dev_shell_lock_sha256": self.dev_shell_lock_sha256,
            },
            "objects": [
                {
                    "id": "archive-alpha-source",
                    "kind": "source-archive",
                    "role": "runtime",
                    "declared_materialization": "embedded",
                    "architecture": "wasm32",
                    "adapter": "source-archive-v1",
                    "archive_id": archive.id,
                    "url": archive.url,
                    "path": f"inputs/objects/archive-alpha-source-sha256-{archive.sha256}",
                    "sha256": archive.sha256,
                    "bytes": archive.bytes,
                },
                {
                    "id": "package-shared-runtime-output-runtime",
                    "kind": "package-output",
                    "role": "runtime",
                    "declared_materialization": "lazy",
                    "architecture": "wasm32",
                    "adapter": "package-output-file-v1",
                    "package": package.package,
                    "selector_kind": package.selector_kind,
                    "selector": package.selector,
                    "path": (
                        "inputs/objects/package-shared-runtime-output-runtime-"
                        f"sha256-{package.sha256}"
                    ),
                    "sha256": package.sha256,
                    "bytes": package.bytes,
                },
                {
                    "id": "repository-alpha-config",
                    "kind": "repository-path",
                    "role": "runtime",
                    "declared_materialization": "embedded",
                    "architecture": "wasm32",
                    "adapter": "repository-path-bundle-v1",
                    "repository_id": repository.id,
                    "paths": list(repository.paths),
                    "path": f"inputs/objects/repository-alpha-config-sha256-{repository.sha256}",
                    "sha256": repository.sha256,
                    "bytes": repository.bytes,
                },
            ],
        }
        actual = resolve_product_from_checked_input_authority(
            checked_inventory,
            request=self.request,
            request_sha256=self.request_sha256,
            catalog=self.catalog,
            tap_plan=self.tap_plan,
            records=self.records,
            candidate_records=self.candidate_records,
            candidate_locators=self.candidate_locators,
            source_custody_records=self.source_custody_records,
            reuse_records={},
            verification_records=self.verification_records,
            verification_locators=self.verification_locators,
            verification_tests=(self.definition,),
            runtime_bundle=self.runtime_bundle,
            product_artifacts=self.product_artifacts,
        )
        self.assertEqual(actual.plan.product_id, "alpha-shell")
        by_kind = {
            item["kind"]: item for item in actual.resolved_inputs["inputs"]
        }
        self.assertEqual(
            set(by_kind),
            {
                "homebrew-bottle",
                "package-output",
                "product-image",
                "repository-path",
                "source-archive",
            },
        )
        self.assertEqual(by_kind["package-output"]["sha256"], package.sha256)
        self.assertEqual(by_kind["source-archive"]["sha256"], archive.sha256)
        self.assertEqual(by_kind["repository-path"]["sha256"], repository.sha256)
        self.assertTrue(
            by_kind["package-output"]["reference"].startswith(
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                "products/alpha-shell@sha256:"
            )
        )

    def test_resolver_plan_is_accepted_by_candidate_product_publication(self) -> None:
        target = self.request["target_abi"]
        runtime_bundle, runtime_files = _runtime_bundle(
            self.source,
            target,
            self.runtime_bundle["build_policy_sha256"],
        )
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

    def test_targeted_resolution_does_not_require_sibling_build_artifacts(self) -> None:
        alpha = self.resolve(
            target_product_ids=("alpha-shell",),
            package_artifacts=(self.package_artifacts[0],),
            archive_artifacts=self.archive_artifacts,
            toolchain_artifacts=(),
            repository_artifacts=(self.repository_artifacts[0],),
        )
        self.assertEqual([item.plan.product_id for item in alpha], ["alpha-shell"])
        shared = next(
            item
            for item in alpha[0].resolved_inputs["inputs"]
            if item["kind"] == "package-output"
        )
        self.assertEqual(shared["declared_materialization"], "lazy")
        self.assertEqual(
            shared["effective_materialization"],
            "embedded",
            "the selected manifest graph, not a sibling staging list, owns sharing",
        )

        beta = self.resolve(
            target_product_ids=("beta-tools",),
            product_artifacts=(),
            package_artifacts=self.package_artifacts,
            archive_artifacts=(),
            toolchain_artifacts=self.toolchain_artifacts,
            repository_artifacts=(self.repository_artifacts[1],),
        )
        self.assertEqual([item.plan.product_id for item in beta], ["beta-tools"])

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
        missing_descriptor = copy.deepcopy(self.candidate_records)
        first_missing = next(iter(missing_descriptor.values()))
        first_missing["candidate"]["normalized_components"] = [
            item
            for item in first_missing["candidate"]["normalized_components"]
            if item["id"] != "vfs-composition-descriptor"
        ]
        cases["missing VFS composition descriptor"] = {
            "candidate_records": missing_descriptor
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

    def test_fixture_and_activation_are_strict_and_active(self) -> None:
        document = load_resolved_product_inputs(FIXTURE.read_bytes())
        self.assertEqual(document["kind"], "kandelo-resolved-vfs-product-inputs")
        self.assertEqual(load_product_evidence_activation(ACTIVATION), "active")
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


class ProductInputObjectAuthorityTests(unittest.TestCase):
    def _zip_directory(self, root: str, files: dict[str, bytes]) -> bytes:
        with tempfile.NamedTemporaryFile() as temporary:
            with zipfile.ZipFile(
                temporary.name, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                directory = zipfile.ZipInfo(f"{root}/", (1980, 1, 2, 0, 0, 0))
                directory.create_system = 3
                directory.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(directory, b"")
                for path, body in sorted(files.items()):
                    parts = path.split("/")
                    for index in range(1, len(parts)):
                        parent = f"{root}/{'/'.join(parts[:index])}/"
                        if parent in archive.namelist():
                            continue
                        entry = zipfile.ZipInfo(parent, (1980, 1, 2, 0, 0, 0))
                        entry.create_system = 3
                        entry.external_attr = (stat.S_IFDIR | 0o755) << 16
                        archive.writestr(entry, b"")
                    entry = zipfile.ZipInfo(
                        f"{root}/{path}", (1980, 1, 2, 0, 0, 0)
                    )
                    entry.create_system = 3
                    entry.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(entry, body)
            return Path(temporary.name).read_bytes()

    def _fixture(self, root: Path) -> dict[str, object]:
        source_root = root / "source"
        runtime_root = root / "runtime"
        object_root = root / "private"
        (source_root / "config").mkdir(parents=True)
        (source_root / "config/fixture.json").write_bytes(b'{"fixture":true}\n')
        (source_root / "config/fixture.json").chmod(0o644)
        archive_body = b"pinned source archive\n"
        manifest = _manifest(
            "authority-product",
            repositories=[
                {
                    "id": "config",
                    "paths": ["config/fixture.json"],
                    "role": "build",
                }
            ],
            packages=[
                {
                    "name": "mini",
                    "outputs": ["runtime"],
                    "source_roles": [],
                    "role": "runtime",
                    "materialization": "lazy",
                }
            ],
            archives=[
                {
                    "id": "source",
                    "url": "https://sources.example.test/mini.tar.gz",
                    "sha256": _digest_bytes(archive_body),
                    "role": "runtime",
                    "materialization": "embedded",
                }
            ],
            toolchains=[
                {
                    "id": "sdk",
                    "provider": "repository-dev-shell",
                    "component": "wasm32-sysroot",
                    "role": "runtime",
                    "materialization": "embedded",
                }
            ],
        )
        manifest_sha256 = canonical_sha256(manifest)
        catalog = {
            "schema": 1,
            "kind": "kandelo-vfs-product-catalog",
            "products": [
                {
                    "path": "images/vfs/products/authority-product.toml",
                    "sha256": manifest_sha256,
                    "manifest": manifest,
                }
            ],
        }
        request = _request()
        request["requirements"]["products"] = [
            {
                "id": "authority-product",
                "path": "images/vfs/products/authority-product.toml",
                "manifest_sha256": manifest_sha256,
            }
        ]
        request["requirements"]["digest"] = canonical_sha256(
            {
                key: request["requirements"][key]
                for key in (
                    "change_classes",
                    "products",
                    "registries",
                    "evidence",
                )
            }
        )
        request_sha256 = canonical_sha256(request)
        policy_sha256 = _digest("authority-policy")
        runtime, runtime_files = _runtime_bundle(
            request["build_source"], request["target_abi"], policy_sha256
        )
        for path, body in runtime_files.items():
            destination = runtime_root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            destination.chmod(0o644)

        repository_body = canonical_bytes(
            {
                "schema": 1,
                "kind": "kandelo-vfs-repository-path-bundle",
                "paths": ["config/fixture.json"],
                "source": request["build_source"],
                "entries": [
                    {
                        "path": "config/fixture.json",
                        "kind": "file",
                        "mode": 0o644,
                        "sha256": _digest_bytes(b'{"fixture":true}\n'),
                        "bytes": len(b'{"fixture":true}\n'),
                        "content_base64": base64.b64encode(
                            b'{"fixture":true}\n'
                        ).decode(),
                    }
                ],
            }
        )
        objects = [
            {
                "id": "archive-source",
                "kind": "source-archive",
                "role": "runtime",
                "declared_materialization": "embedded",
                "architecture": "wasm32",
                "adapter": "source-archive-v1",
                "archive_id": "source",
                "url": "https://sources.example.test/mini.tar.gz",
                "body": archive_body,
            },
            {
                "id": "package-mini-output-runtime",
                "kind": "package-output",
                "role": "runtime",
                "declared_materialization": "lazy",
                "architecture": "wasm32",
                "adapter": "package-output-file-v1",
                "package": "mini",
                "selector_kind": "output",
                "selector": "runtime",
                "body": b"exact candidate package output\n",
            },
            {
                "id": "repository-config",
                "kind": "repository-path",
                "role": "build",
                "declared_materialization": "build-only",
                "architecture": "wasm32",
                "adapter": "repository-path-bundle-v1",
                "repository_id": "config",
                "paths": ["config/fixture.json"],
                "body": repository_body,
            },
            {
                "id": "toolchain-sdk",
                "kind": "toolchain-output",
                "role": "runtime",
                "declared_materialization": "embedded",
                "architecture": "wasm32",
                "adapter": "toolchain-directory-zip-v1",
                "toolchain_id": "sdk",
                "provider": "repository-dev-shell",
                "component": "wasm32-sysroot",
                "body": self._zip_directory(
                    "sdk", {"include/bits/ioctl_fix.h": b""}
                ),
            },
        ]
        stored_objects = []
        for value in objects:
            item = copy.deepcopy(value)
            body = item.pop("body")
            digest = _digest_bytes(body)
            item["sha256"] = digest
            item["bytes"] = len(body)
            item["path"] = f"inputs/objects/{item['id']}-sha256-{digest}"
            destination = object_root / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            stored_objects.append(item)
        inventory = {
            "schema": 1,
            "kind": "kandelo-vfs-product-input-object-inventory",
            "product": {
                "id": "authority-product",
                "manifest_path": "images/vfs/products/authority-product.toml",
                "manifest_sha256": manifest_sha256,
                "architecture": "wasm32",
            },
            "source": request["build_source"],
            "target_abi": request["target_abi"],
            "build_environment": {
                "policy_sha256": policy_sha256,
                "dev_shell_lock_sha256": _digest_bytes(runtime_files["flake.lock"]),
            },
            "objects": stored_objects,
        }
        return {
            "catalog": catalog,
            "inventory": inventory,
            "object_root": object_root,
            "request": request,
            "request_sha256": request_sha256,
            "runtime": runtime,
            "runtime_root": runtime_root,
            "source_root": source_root,
        }

    def _validate(self, fixture: dict[str, object], inventory: object | None = None):
        return validate_product_input_object_authority(
            fixture["inventory"] if inventory is None else inventory,
            request=fixture["request"],
            request_sha256=fixture["request_sha256"],
            catalog=fixture["catalog"],
            runtime_bundle=fixture["runtime"],
            object_root=fixture["object_root"],
            source_root=fixture["source_root"],
            runtime_root=fixture["runtime_root"],
        )

    def test_protected_authority_derives_and_checks_every_private_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            build_spec = select_product_input_build_spec(
                fixture["request"], fixture["catalog"], "authority-product"
            )
            self.assertEqual(
                build_spec["packages"],
                [
                    {
                        "name": "mini",
                        "outputs": ["runtime"],
                        "source_roles": [],
                    }
                ],
            )
            self.assertEqual(
                build_spec["archives"],
                [
                    {
                        "id": "source",
                        "sha256": _digest_bytes(b"pinned source archive\n"),
                        "url": "https://sources.example.test/mini.tar.gz",
                    }
                ],
            )
            self.assertEqual(
                build_spec["builder"],
                "images/vfs/scripts/build-authority-product.sh",
            )
            checked = self._validate(fixture)
            self.assertEqual(
                [item["id"] for item in checked["objects"]],
                [
                    "archive-source",
                    "package-mini-output-runtime",
                    "repository-config",
                    "toolchain-sdk",
                ],
            )
            packages, archives, toolchains, repositories = (
                resolver_artifacts_from_input_inventory(checked)
            )
            self.assertEqual([item.selector for item in packages], ["runtime"])
            self.assertEqual([item.id for item in archives], ["source"])
            self.assertEqual([item.id for item in toolchains], ["sdk"])
            self.assertEqual([item.id for item in repositories], ["config"])

    def test_build_spec_groups_distinct_selectors_for_one_package_recipe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            product = fixture["catalog"]["products"][0]
            product["manifest"]["software"]["package"].append(
                {
                    "name": "mini",
                    "outputs": ["headers"],
                    "source_roles": ["source"],
                    "role": "build",
                }
            )
            manifest_sha256 = canonical_sha256(product["manifest"])
            product["sha256"] = manifest_sha256
            fixture["request"]["requirements"]["products"][0][
                "manifest_sha256"
            ] = manifest_sha256
            fixture["request"]["requirements"]["digest"] = canonical_sha256(
                {
                    key: fixture["request"]["requirements"][key]
                    for key in (
                        "change_classes",
                        "products",
                        "registries",
                        "evidence",
                    )
                }
            )

            build_spec = select_product_input_build_spec(
                fixture["request"], fixture["catalog"], "authority-product"
            )

            self.assertEqual(
                build_spec["packages"],
                [
                    {
                        "name": "mini",
                        "outputs": ["headers", "runtime"],
                        "source_roles": ["source"],
                    }
                ],
            )

    def test_protected_authority_rejects_self_consistent_untrusted_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            original = fixture["inventory"]
            assert isinstance(original, dict)
            mutations = {}
            missing = copy.deepcopy(original)
            missing["objects"].pop()
            mutations["missing"] = missing
            selector = copy.deepcopy(original)
            next(
                item
                for item in selector["objects"]
                if item["kind"] == "package-output"
            )["selector"] = "undeclared"
            mutations["selector"] = selector
            archive = copy.deepcopy(original)
            next(
                item
                for item in archive["objects"]
                if item["kind"] == "source-archive"
            )["url"] = "https://sources.example.test/substituted.tar.gz"
            mutations["archive"] = archive
            repository = copy.deepcopy(original)
            next(
                item
                for item in repository["objects"]
                if item["kind"] == "repository-path"
            )["paths"] = ["config/other.json"]
            mutations["repository"] = repository
            toolchain = copy.deepcopy(original)
            next(
                item
                for item in toolchain["objects"]
                if item["kind"] == "toolchain-output"
            )["component"] = "ambient-sysroot"
            mutations["toolchain"] = toolchain
            for name, value in mutations.items():
                with self.subTest(name=name), self.assertRaises(
                    ProductInputResolutionError
                ):
                    self._validate(fixture, value)

            package = next(
                item
                for item in original["objects"]
                if item["kind"] == "package-output"
            )
            package_path = fixture["object_root"] / package["path"]
            package_path.write_bytes(b"substituted after collection\n")
            with self.assertRaisesRegex(
                ProductInputResolutionError, "changed|digest|bytes"
            ):
                self._validate(fixture)


class ProductBuildHandoffTests(unittest.TestCase):
    def test_product_subprocess_capture_is_bounded_before_completion(self) -> None:
        from scripts.abi_staging.cli import _run_bounded_product_command

        result = _run_bounded_product_command(
            [
                sys.executable,
                "-c",
                "import sys; print('root'); print('diagnostic', file=sys.stderr)",
            ],
            cwd=Path.cwd(),
            env={"PATH": str(Path(sys.executable).parent)},
            timeout_seconds=30,
            stdout_limit=1024,
            stderr_limit=1024,
        )
        self.assertEqual(result.stdout, b"root\n")
        self.assertEqual(result.stderr, b"diagnostic\n")

        with self.assertRaisesRegex(
            ProductInputResolutionError, "output exceeded|capture"
        ):
            _run_bounded_product_command(
                [sys.executable, "-c", "import sys; sys.stderr.write('x' * 4096)"],
                cwd=Path.cwd(),
                env={"PATH": str(Path(sys.executable).parent)},
                timeout_seconds=30,
                stdout_limit=1024,
                stderr_limit=1024,
            )

    def test_materializes_only_exact_public_objects_beside_private_inputs(self) -> None:
        resolved = json.loads(FIXTURE.read_bytes())
        private_body = b"exact private package output\n"
        descriptor_body = b"exact public bottle descriptor\n"
        product_body = b"exact dependency product VFS\n"
        private_sha256 = _digest_bytes(private_body)
        descriptor_sha256 = _digest_bytes(descriptor_body)
        product_sha256 = _digest_bytes(product_body)
        candidate_root = "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates"
        resolved["inputs"] = [
            {
                "architecture": "wasm32",
                "bytes": len(descriptor_body),
                "declared_materialization": "lazy",
                "descriptor": {
                    "bytes": len(descriptor_body),
                    "path": (
                        "inputs/objects/homebrew-mini-metadata-sha256-"
                        + descriptor_sha256
                    ),
                    "reference": (
                        f"{candidate_root}/mini@sha256:{descriptor_sha256}"
                    ),
                    "sha256": descriptor_sha256,
                },
                "effective_materialization": "lazy-reference",
                "id": "homebrew-mini",
                "kind": "homebrew-bottle",
                "reference": f"{candidate_root}/mini@sha256:{'2' * 64}",
                "role": "runtime",
                "sha256": "2" * 64,
            },
            {
                "architecture": "wasm32",
                "bytes": len(private_body),
                "declared_materialization": "embedded",
                "effective_materialization": "embedded",
                "id": "package-mini-output-runtime",
                "kind": "package-output",
                "path": (
                    "inputs/objects/package-mini-output-runtime-sha256-"
                    + private_sha256
                ),
                "reference": (
                    "urn:kandelo:abi-staging:product-input:"
                    f"package-mini-output-runtime:sha256:{private_sha256}"
                ),
                "role": "runtime",
                "sha256": private_sha256,
            },
            {
                "architecture": "wasm32",
                "bytes": len(product_body),
                "declared_materialization": "embedded",
                "effective_materialization": "embedded",
                "id": "product-base",
                "kind": "product-image",
                "path": f"inputs/objects/product-base-sha256-{product_sha256}",
                "reference": (
                    f"{candidate_root}/products/base@sha256:{product_sha256}"
                ),
                "role": "runtime",
                "sha256": product_sha256,
            },
        ]
        resolved_body = canonical_bytes(resolved)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            object_root = root / "inputs/objects"
            object_root.mkdir(parents=True)
            private_path = object_root / (
                "package-mini-output-runtime-sha256-" + private_sha256
            )
            private_path.write_bytes(private_body)
            transport = FakeRegistryTransport()
            transport.blobs[(
                "kandelo-dev/homebrew-tap-core-abi-8-candidates/mini",
                "sha256:" + descriptor_sha256,
            )] = descriptor_body
            transport.blobs[(
                "kandelo-dev/homebrew-tap-core-abi-8-candidates/products/base",
                "sha256:" + product_sha256,
            )] = product_body

            result_path = materialize_resolved_product_input_objects(
                resolved_body,
                root=root,
                transport=transport,
            )

            self.assertEqual(result_path, (root / "resolved-inputs.json").resolve())
            self.assertEqual(result_path.read_bytes(), resolved_body)
            self.assertEqual(
                (object_root / (
                    "homebrew-mini-metadata-sha256-" + descriptor_sha256
                )).read_bytes(),
                descriptor_body,
            )
            self.assertEqual(
                (object_root / f"product-base-sha256-{product_sha256}").read_bytes(),
                product_body,
            )
            self.assertEqual(len(transport.calls), 2)
            self.assertTrue(all(not authenticated for _, _, authenticated in transport.calls))

            tampered_root = root / "tampered"
            tampered_objects = tampered_root / "inputs/objects"
            tampered_objects.mkdir(parents=True)
            (tampered_objects / private_path.name).write_bytes(
                b"substituted private bytes\n"
            )
            with self.assertRaisesRegex(
                ProductInputResolutionError, "private|existing|digest|bytes"
            ):
                materialize_resolved_product_input_objects(
                    resolved_body,
                    root=tampered_root,
                    transport=transport,
                )

    def test_selects_only_the_exact_manifest_owned_product_work(self) -> None:
        request = _request()
        request_sha256 = canonical_sha256(request)
        selected = request["requirements"]["products"][0]
        evidence = request["requirements"]["evidence"][0]
        work_id = canonical_sha256(
            {
                "applicability": evidence["applicability"],
                "manifest_sha256": selected["manifest_sha256"],
                "product_id": selected["id"],
                "request_digest": request_sha256,
                "stage": "compose-product",
            }
        )
        scope = select_product_execution_scope(
            request,
            request_sha256=request_sha256,
            product_id=selected["id"],
            work_id=work_id,
        )
        self.assertEqual(scope["manifest_path"], selected["path"])
        self.assertEqual(scope["applicability"], evidence["applicability"])

        with self.assertRaisesRegex(ProductInputResolutionError, "work|selected"):
            select_product_execution_scope(
                request,
                request_sha256=request_sha256,
                product_id=selected["id"],
                work_id="0" * 64,
            )

    def test_cli_routes_the_exact_product_work_scope(self) -> None:
        from scripts.abi_staging import cli

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(cli, "_execute_product_work", return_value=0) as execute:
                status = cli_main(
                    [
                        "execute-product-work",
                        "--coordination-root",
                        str(root / "inputs"),
                        "--runtime-artifact-id",
                        "717",
                        "--runtime-artifact-digest",
                        "a" * 64,
                        "--product-id",
                        "mini-product",
                        "--work-id",
                        "b" * 64,
                        "--kandelo-root",
                        str(root / "candidate"),
                        "--kandelo-policy-root",
                        str(root / "policy"),
                        "--tap-root",
                        str(root / "tap"),
                        "--validate-builder-report",
                        "--private-out",
                        str(root / "private-inputs"),
                        "--out",
                        str(root / "handoff"),
                    ]
                )
        self.assertEqual(status, 0)
        args = execute.call_args.args[0]
        self.assertEqual(args.product_id, "mini-product")
        self.assertEqual(args.work_id, "b" * 64)
        self.assertTrue(args.validate_builder_report)
        self.assertEqual(args.private_out, str(root / "private-inputs"))

    def test_cli_routes_protected_product_publication_scope(self) -> None:
        from scripts.abi_staging import cli

        with patch.object(
            cli, "_publish_workflow_product_candidate", return_value=None, create=True
        ) as publish:
            status = cli_main(
                [
                    "publish-workflow-product-candidate",
                    "--run-id",
                    "717",
                    "--run-attempt",
                    "2",
                    "--head-sha",
                    "a" * 40,
                    "--product-id",
                    "mini-product",
                    "--work-id",
                    "b" * 64,
                    "--handoff-artifact-name",
                    "abi-staging-product-build-mini-product",
                    "--private-artifact-name",
                    "abi-staging-product-private-mini-product",
                    "--kandelo-root",
                    "/tmp/exact-candidate",
                    "--kandelo-policy-root",
                    "/tmp/exact-policy",
                    "--validate-builder-report",
                    "--require-github-digest",
                    "--anonymous-readback",
                    "--immutable",
                    "--out",
                    "/tmp/product-candidate.json",
                ]
            )
        self.assertEqual(status, 0)
        publish.assert_called_once()
        args = publish.call_args.args[0]
        self.assertEqual(args.product_id, "mini-product")
        self.assertEqual(args.run_attempt, 2)
        self.assertTrue(args.validate_builder_report)

    def test_candidate_package_command_receives_only_manifest_projection(self) -> None:
        from scripts.abi_staging.cli import _candidate_package_resolve_arguments

        root = Path("/tmp/exact-product-work")
        command = _candidate_package_resolve_arguments(
            kandelo_root=root / "candidate",
            cache_root=root / "cache",
            cargo_target=root / "cargo-target",
            sysroot=root / "runtime/toolchain/wasm32-sysroot",
            architecture="wasm32",
            package={
                "name": "mini",
                "outputs": ["runtime", "tools"],
                "source_roles": ["tests"],
            },
        )
        self.assertEqual(command[:3], [root / "candidate/scripts/dev-shell.sh", "bash", "-c"])
        self.assertEqual(command[-3:], ["mini", "runtime,tools", "tests"])
        self.assertIn("--force-source-build", command[3])
        self.assertIn("WASM_POSIX_BINARY_CACHE_ROOT", command[3])
        self.assertIn("KANDELO_VFS_PRODUCT_OUTPUTS", command[3])
        self.assertNotIn("GITHUB_TOKEN", " ".join(str(value) for value in command))

    def test_candidate_package_command_projects_the_exact_runtime_sysroot(self) -> None:
        from scripts.abi_staging.cli import _candidate_package_resolve_arguments

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            scripts = candidate / "scripts"
            tools = root / "tools"
            cache = root / "cache"
            cargo_target = root / "cargo-target"
            sysroot = root / "runtime/toolchain/wasm32-sysroot"
            scripts.mkdir(parents=True)
            tools.mkdir()
            cargo_target.mkdir()
            sysroot.mkdir(parents=True)
            (sysroot / "libc.a").write_bytes(b"exact runtime sysroot\n")
            (scripts / "dev-shell.sh").write_text(
                "#!/usr/bin/env bash\nset -euo pipefail\nexec \"$@\"\n"
            )
            (scripts / "dev-shell.sh").chmod(0o755)
            (tools / "rustc").write_text(
                "#!/usr/bin/env bash\nprintf 'host: fixture-host\\n'\n"
            )
            (tools / "rustc").chmod(0o755)
            (tools / "cargo").write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "test -L \"$PWD/sysroot\"\n"
                "test \"$(readlink \"$PWD/sysroot\")\" = \"$WASM_POSIX_SYSROOT\"\n"
                "test -L \"$PWD/target\"\n"
                "test \"$(readlink \"$PWD/target\")\" = \"$CARGO_TARGET_DIR\"\n"
                "mkdir -p \"$WASM_POSIX_BINARY_CACHE_ROOT/result\"\n"
                "printf '%s\\n' \"$WASM_POSIX_BINARY_CACHE_ROOT/result\"\n"
            )
            (tools / "cargo").chmod(0o755)

            command = _candidate_package_resolve_arguments(
                kandelo_root=candidate,
                cache_root=cache,
                cargo_target=cargo_target,
                sysroot=sysroot,
                architecture="wasm32",
                package={"name": "mini", "outputs": ["runtime"], "source_roles": []},
            )
            completed = subprocess.run(
                [str(value) for value in command],
                cwd=candidate,
                env={**dict(os.environ), "PATH": f"{tools}:{os.environ['PATH']}"},
                check=False,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stdout.decode().strip(), str(cache / "result"))
            self.assertFalse((candidate / "sysroot").exists())
            self.assertFalse((candidate / "sysroot").is_symlink())
            self.assertFalse((candidate / "target").exists())
            self.assertFalse((candidate / "target").is_symlink())

    def test_product_collection_and_builder_commands_keep_protected_argv_authority(
        self,
    ) -> None:
        from scripts.abi_staging.cli import (
            _product_input_collector_arguments,
            _vfs_product_builder_arguments,
        )

        root = Path("/tmp/exact-product-work")
        candidate = root / "candidate"
        policy = root / "policy"
        runtime = root / "runtime"
        collector = _product_input_collector_arguments(
            kandelo_root=candidate,
            kandelo_policy_root=policy,
            catalog=candidate / "images/vfs/products/generated/catalog.json",
            product_id="mini-product",
            source={
                "repository": "kandelo-dev/kandelo",
                "commit": "a" * 40,
                "tree": "b" * 40,
            },
            target_abi={"version": 8, "snapshot_sha256": "c" * 64},
            policy_sha256="d" * 64,
            dev_shell_lock_sha256="e" * 64,
            package_roots=root / "package-roots.json",
            archive_files=root / "archive-files.json",
            runtime_root=runtime,
            out=root / "private",
        )
        self.assertEqual(
            collector[:5],
            [
                candidate / "scripts/dev-shell.sh",
                "npx",
                "--no-install",
                "tsx",
                policy / "scripts/abi-staging-collect-product-inputs.ts",
            ],
        )
        self.assertEqual(collector[collector.index("--product-id") + 1], "mini-product")
        self.assertEqual(collector[collector.index("--runtime-root") + 1], runtime)
        self.assertNotIn("GITHUB_TOKEN", " ".join(str(value) for value in collector))

        builder = _vfs_product_builder_arguments(
            kandelo_root=candidate,
            kandelo_policy_root=policy,
            manifest=candidate / "images/vfs/products/mini-product.toml",
            resolved_inputs=root / "private/resolved-inputs.json",
            work_dir=root / "builder",
            output=root / "builder/mini-product.vfs",
            report=root / "builder/builder-report.json",
        )
        self.assertEqual(
            builder[:5],
            [
                candidate / "scripts/dev-shell.sh",
                "npx",
                "--no-install",
                "tsx",
                policy / "scripts/run-vfs-product-builder.ts",
            ],
        )
        self.assertEqual(
            builder[builder.index("--manifest") + 1],
            candidate / "images/vfs/products/mini-product.toml",
        )
        self.assertEqual(
            builder[builder.index("--inputs") + 1],
            root / "private/resolved-inputs.json",
        )

    def test_product_subprocess_environment_is_a_positive_allowlist(self) -> None:
        from scripts.abi_staging.cli import _product_subprocess_environment

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ambient = {
                "ACTIONS_RUNTIME_TOKEN": "runtime-secret",
                "AWS_SESSION_TOKEN": "cloud-secret",
                "CI": "true",
                "GITHUB_TOKEN": "repository-secret",
                "KANDELO_NIX_BIN": "/nix/store/example/bin/nix",
                "LANG": "C.UTF-8",
                "PATH": "/declared/tools",
                "SUPER_SECRET": "arbitrary-secret",
            }
            environment = _product_subprocess_environment(root, ambient=ambient)

            exact_root = root.resolve(strict=True)
            self.assertEqual(environment["HOME"], str(exact_root / "home"))
            self.assertEqual(environment["TMPDIR"], str(exact_root / "tmp"))
            self.assertEqual(
                environment["XDG_CACHE_HOME"], str(exact_root / "cache")
            )
            self.assertEqual(environment["KANDELO_NIX_BIN"], ambient["KANDELO_NIX_BIN"])
            self.assertEqual(environment["LANG"], "C.UTF-8")
            self.assertEqual(environment["PATH"], "/declared/tools")
            self.assertNotIn("GITHUB_TOKEN", environment)
            self.assertNotIn("ACTIONS_RUNTIME_TOKEN", environment)
            self.assertNotIn("AWS_SESSION_TOKEN", environment)
            self.assertNotIn("SUPER_SECRET", environment)
            self.assertEqual((exact_root / "home").stat().st_mode & 0o777, 0o700)

    def test_runtime_validator_uses_only_protected_code_and_exact_identity(self) -> None:
        from scripts.abi_staging.cli import _runtime_validation_arguments

        root = Path("/tmp/exact-product-work")
        command = _runtime_validation_arguments(
            kandelo_policy_root=root / "policy",
            bundle=root / "runtime/runtime-bundle.json",
            artifact_root=root / "runtime/runtime",
            source_root=root / "candidate",
            source={
                "repository": "kandelo-dev/kandelo",
                "commit": "a" * 40,
                "tree": "b" * 40,
            },
            target_abi={"version": 8, "snapshot_sha256": "c" * 64},
            build_policy_sha256="d" * 64,
        )
        self.assertEqual(
            command[:3],
            [root / "policy/scripts/dev-shell.sh", "bash", "-c"],
        )
        self.assertIn("runtime-bundle validate", command[3])
        self.assertEqual(command[command.index("--source-root") + 1], root / "candidate")
        self.assertEqual(command[command.index("--abi") + 1], "8")
        self.assertEqual(command[command.index("--build-policy-sha256") + 1], "d" * 64)
        self.assertNotIn("GITHUB_TOKEN", " ".join(str(value) for value in command))

    def test_archive_download_is_bounded_https_without_embedded_credentials(self) -> None:
        from scripts.abi_staging.cli import _archive_download_arguments

        root = Path("/tmp/exact-product-work")
        command = _archive_download_arguments(
            kandelo_policy_root=root / "policy",
            archive={
                "id": "fixture",
                "url": "https://sources.example.test/fixture.tar.gz",
                "sha256": "a" * 64,
            },
            output=root / "fixture.tar.gz",
        )
        self.assertEqual(
            command[:2], [root / "policy/scripts/dev-shell.sh", "curl"]
        )
        self.assertEqual(command[command.index("--proto") + 1], "=https")
        self.assertEqual(command[command.index("--proto-redir") + 1], "=https")
        self.assertIn("--max-filesize", command)
        self.assertNotIn("Authorization", " ".join(str(value) for value in command))

        with self.assertRaisesRegex(ProductInputResolutionError, "credential-free"):
            _archive_download_arguments(
                kandelo_policy_root=root / "policy",
                archive={
                    "id": "fixture",
                    "url": "https://user:secret@sources.example.test/fixture.tar.gz",
                    "sha256": "a" * 64,
                },
                output=root / "fixture.tar.gz",
            )

    def test_product_executor_emits_terminal_integrity_handoff(self) -> None:
        from scripts.abi_staging import cli

        request = _request()
        catalog = _catalog()
        manifest_by_id = {
            entry["manifest"]["id"]: entry for entry in catalog["products"]
        }
        for binding in request["requirements"]["products"]:
            entry = manifest_by_id[binding["id"]]
            binding["manifest_sha256"] = entry["sha256"]
        requirements = request["requirements"]
        requirements["digest"] = canonical_sha256(
            {
                key: requirements[key]
                for key in ("change_classes", "products", "registries", "evidence")
            }
        )
        request_sha256 = canonical_sha256(request)
        selected = request["requirements"]["products"][0]
        work_id = canonical_sha256(
            {
                "request_digest": request_sha256,
                "product_id": selected["id"],
                "manifest_sha256": selected["manifest_sha256"],
                "applicability": "required",
                "stage": "compose-product",
            }
        )
        runtime, runtime_files = _runtime_bundle(
            request["build_source"],
            request["target_abi"],
            request["issuance"]["policy_sha256"],
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "inputs"
            (inputs / "coordination").mkdir(parents=True)
            runtime_root = inputs / "runtime/runtime"
            runtime_root.mkdir(parents=True)
            for relative, body in runtime_files.items():
                path = runtime_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
            (inputs / "runtime/runtime-bundle.json").write_bytes(
                canonical_bytes(runtime)
            )
            candidate = root / "candidate"
            policy_root = root / "policy"
            tap_root = root / "tap"
            catalog_path = candidate / "images/vfs/products/generated/catalog.json"
            catalog_path.parent.mkdir(parents=True)
            catalog_path.write_bytes(canonical_bytes(catalog))
            policy_root.mkdir()
            tap_root.mkdir()
            output = root / "public"
            private_output = root / "private"
            args = SimpleNamespace(
                coordination_root=str(inputs),
                runtime_artifact_id=717,
                runtime_artifact_digest="a" * 64,
                product_id=selected["id"],
                work_id=work_id,
                kandelo_root=str(candidate),
                kandelo_policy_root=str(policy_root),
                tap_root=str(tap_root),
                validate_builder_report=True,
                private_out=str(private_output),
                out=str(output),
            )
            protected_policy = SimpleNamespace(
                tap_repository="kandelo-dev/homebrew-tap-core",
                kandelo_repository=request["build_source"]["repository"],
            )
            bundle = {
                "request": request,
                "request_sha256": request_sha256,
                "tap_plan": {},
            }
            failed_runtime = subprocess.CompletedProcess(
                ["runtime-validator"], 1, b"", b"runtime inventory mismatch\n"
            )
            with (
                patch.object(cli, "_protected_tap_root", return_value=tap_root),
                patch.object(
                    cli, "load_tap_staging_policy", return_value=protected_policy
                ),
                patch.object(cli, "load_coordination_bundle", return_value=bundle),
                patch.object(
                    cli,
                    "_checked_checkout_source",
                    side_effect=[candidate, policy_root],
                ),
                patch.object(
                    cli,
                    "_run_bounded_product_command",
                    return_value=failed_runtime,
                ),
            ):
                status = cli._execute_product_work(args)

            self.assertEqual(status, 1)
            result = load_product_build_result(
                (output / "product-build-result.json").read_bytes()
            )
            self.assertEqual(result["outcome"], "failure")
            self.assertEqual(result["guard_code"], "product_integrity_mismatch")
            authority = json.loads((private_output / "authority.json").read_bytes())
            self.assertEqual(authority["runtime_artifact"]["id"], 717)
            self.assertEqual(authority["outcome"], "failure")

    def test_private_product_authority_is_an_exact_closed_handoff(self) -> None:
        from scripts.abi_staging.cli import _write_private_product_authority

        product = {
            "id": "mini-product",
            "manifest_sha256": "a" * 64,
            "output": "mini-product.vfs",
        }
        inventory = {
            "schema": 1,
            "kind": "kandelo-vfs-product-input-object-inventory",
            "product": {
                "id": product["id"],
                "manifest_path": "images/vfs/products/mini-product.toml",
                "manifest_sha256": product["manifest_sha256"],
                "architecture": "wasm32",
            },
            "source": {
                "repository": "kandelo-dev/kandelo",
                "commit": "b" * 40,
                "tree": "c" * 40,
            },
            "target_abi": {"version": 8, "snapshot_sha256": "d" * 64},
            "build_environment": {
                "policy_sha256": "e" * 64,
                "dev_shell_lock_sha256": "f" * 64,
            },
            "objects": [],
        }
        inventory_body = canonical_bytes(inventory)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            (root / "inputs/objects").mkdir(parents=True)
            (root / "inputs/artifacts.json").write_bytes(inventory_body)
            _write_private_product_authority(
                root,
                request_sha256="1" * 64,
                work_id="2" * 64,
                product=product,
                runtime_artifact_id=717,
                runtime_artifact_digest="3" * 64,
                runtime_bundle_sha256="4" * 64,
                outcome="success",
                guard_code=None,
                input_inventory_sha256=_digest_bytes(inventory_body),
                resolved_inputs_sha256="5" * 64,
            )
            loaded = validate_private_product_authority_handoff(
                root,
                expected_request_sha256="1" * 64,
                expected_work_id="2" * 64,
                expected_product=product,
                expected_runtime_artifact_id=717,
                expected_runtime_artifact_digest="3" * 64,
                expected_runtime_bundle_sha256="4" * 64,
                max_files=16,
                max_bytes=1024 * 1024,
            )
            self.assertEqual(loaded["authority"]["outcome"], "success")
            self.assertEqual(loaded["inventory"], inventory)

            (root / "untrusted-extra").write_text("extra\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ProductInputResolutionError, "file closure|unexpected"
            ):
                validate_private_product_authority_handoff(
                    root,
                    expected_request_sha256="1" * 64,
                    expected_work_id="2" * 64,
                    expected_product=product,
                    expected_runtime_artifact_id=717,
                    expected_runtime_artifact_digest="3" * 64,
                    expected_runtime_bundle_sha256="4" * 64,
                    max_files=16,
                    max_bytes=1024 * 1024,
                )

    def test_writer_keeps_private_inputs_out_of_the_exact_public_handoff(self) -> None:
        resolved = json.loads(FIXTURE.read_bytes())
        runtime, _ = _runtime_bundle(
            resolved["source"],
            resolved["target_abi"],
            resolved["build_environment"]["policy_sha256"],
        )
        runtime_body = canonical_bytes(runtime)
        resolved_body = canonical_bytes(resolved)
        report_body = canonical_bytes(
            {
                "schema": 1,
                "kind": "fixture-builder-report",
                "product_id": resolved["product"]["id"],
            }
        )
        vfs_body = b"exact candidate VFS\n"
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            private = parent / "private"
            private.mkdir()
            (private / "inputs").mkdir()
            (private / "inputs/artifacts.json").write_bytes(b"private\n")
            handoff = parent / "handoff"
            result = write_product_build_handoff(
                handoff,
                request_sha256=_digest("request"),
                work_id=_digest("work"),
                product=resolved["product"],
                runtime_bundle_body=runtime_body,
                outcome="success",
                guard_code=None,
                exit_code=0,
                diagnostic_summary=b"bounded composition summary\n",
                resolved_inputs_body=resolved_body,
                builder_report_body=report_body,
                vfs_body=vfs_body,
            )
            self.assertEqual(result["outcome"], "success")
            self.assertEqual(
                {
                    path.relative_to(handoff).as_posix()
                    for path in handoff.rglob("*")
                    if path.is_file()
                },
                {
                    "builder-report.json",
                    "diagnostics/summary.txt",
                    "inventory.json",
                    "product-build-result.json",
                    "resolved-inputs.json",
                    "runtime-bundle.json",
                    resolved["product"]["output"],
                },
            )
            self.assertFalse((handoff / "inputs").exists())
            validated = validate_product_build_handoff(
                handoff,
                expected_product_id=resolved["product"]["id"],
                expected_work_id=_digest("work"),
                expected_request_sha256=_digest("request"),
                expected_runtime_bundle_sha256=_digest_bytes(runtime_body),
                max_files=16,
                max_bytes=16 * 1024 * 1024,
            )
            self.assertEqual(validated, result)

    def test_writer_emits_a_terminal_blocked_handoff_without_partial_bytes(self) -> None:
        resolved = json.loads(FIXTURE.read_bytes())
        runtime, _ = _runtime_bundle(
            resolved["source"],
            resolved["target_abi"],
            resolved["build_environment"]["policy_sha256"],
        )
        runtime_body = canonical_bytes(runtime)
        with tempfile.TemporaryDirectory() as temporary:
            handoff = Path(temporary) / "blocked"
            result = write_product_build_handoff(
                handoff,
                request_sha256=_digest("request"),
                work_id=_digest("work"),
                product=resolved["product"],
                runtime_bundle_body=runtime_body,
                outcome="blocked",
                guard_code="product_dependency_unavailable",
                exit_code=78,
                diagnostic_summary=b"dependency is not public yet\n",
            )
            self.assertEqual(result["outcome"], "blocked")
            self.assertEqual(
                {
                    path.relative_to(handoff).as_posix()
                    for path in handoff.rglob("*")
                    if path.is_file()
                },
                {
                    "diagnostics/summary.txt",
                    "inventory.json",
                    "product-build-result.json",
                    "runtime-bundle.json",
                },
            )

    def _write_handoff(self, root: Path, *, outcome: str) -> dict[str, object]:
        resolved = json.loads(FIXTURE.read_bytes())
        product = resolved["product"]
        runtime, _ = _runtime_bundle(
            resolved["source"],
            resolved["target_abi"],
            resolved["build_environment"]["policy_sha256"],
        )
        runtime_body = canonical_bytes(runtime)
        diagnostic = b"bounded product composition summary\n"
        (root / "diagnostics").mkdir()
        (root / "diagnostics/summary.txt").write_bytes(diagnostic)
        (root / "runtime-bundle.json").write_bytes(runtime_body)
        work_id = _digest("miniature-product-work")
        result: dict[str, object] = {
            "schema": 1,
            "kind": "kandelo-abi-staging-product-build-result",
            "request_sha256": _digest("miniature-request"),
            "work_id": work_id,
            "product": {
                "id": product["id"],
                "manifest_sha256": product["manifest_sha256"],
                "output": product["output"],
            },
            "outcome": outcome,
            "guard_code": None,
            "exit_code": 0,
            "runtime_bundle_sha256": _digest_bytes(runtime_body),
            "resolved_inputs_sha256": None,
            "builder_report_sha256": None,
            "vfs": None,
            "diagnostic_summary_sha256": _digest_bytes(diagnostic),
        }
        if outcome == "success":
            resolved_body = canonical_bytes(resolved)
            vfs_body = b"miniature exact product VFS\n"
            report_body = canonical_bytes(
                {
                    "schema": 1,
                    "kind": "fixture-builder-report",
                    "product_id": product["id"],
                }
            )
            (root / "resolved-inputs.json").write_bytes(resolved_body)
            (root / "builder-report.json").write_bytes(report_body)
            (root / product["output"]).write_bytes(vfs_body)
            result.update(
                {
                    "resolved_inputs_sha256": _digest_bytes(resolved_body),
                    "builder_report_sha256": _digest_bytes(report_body),
                    "vfs": {
                        "sha256": _digest_bytes(vfs_body),
                        "bytes": len(vfs_body),
                    },
                }
            )
        else:
            result.update(
                {
                    "guard_code": "product_inputs_unavailable",
                    "exit_code": 78,
                }
            )
        result_body = canonical_bytes(result)
        (root / "product-build-result.json").write_bytes(result_body)
        inventory = build_product_handoff_inventory(root, result)
        (root / "inventory.json").write_bytes(canonical_bytes(inventory))
        return {
            "result": result,
            "runtime_sha256": _digest_bytes(runtime_body),
        }

    def _validate(self, root: Path, fixture: dict[str, object]) -> dict[str, object]:
        result = fixture["result"]
        assert isinstance(result, dict)
        product = result["product"]
        assert isinstance(product, dict)
        return validate_product_build_handoff(
            root,
            expected_product_id=product["id"],
            expected_work_id=result["work_id"],
            expected_request_sha256=result["request_sha256"],
            expected_runtime_bundle_sha256=fixture["runtime_sha256"],
            max_files=16,
            max_bytes=16 * 1024 * 1024,
        )

    def test_success_and_blocked_handoffs_are_exact_terminal_results(self) -> None:
        for outcome in ("success", "blocked"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture = self._write_handoff(root, outcome=outcome)
                validated = self._validate(root, fixture)
                self.assertEqual(validated["outcome"], outcome)
                self.assertEqual(validated["vfs"] is not None, outcome == "success")
                if outcome == "success":
                    self.assertEqual(
                        {
                            path.relative_to(root).as_posix()
                            for path in root.rglob("*")
                            if path.is_file()
                        },
                        {
                            "builder-report.json",
                            "diagnostics/summary.txt",
                            "inventory.json",
                            "product-build-result.json",
                            "resolved-inputs.json",
                            "runtime-bundle.json",
                            "mini-shell.vfs",
                        },
                    )
                self.assertEqual(
                    load_product_build_result(
                        (root / "product-build-result.json").read_bytes()
                    ),
                    fixture["result"],
                )

    def test_handoff_rejects_unlisted_links_digest_drift_and_wrong_scope(self) -> None:
        mutations = ("extra", "symlink", "digest", "scope")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture = self._write_handoff(root, outcome="success")
                if mutation == "extra":
                    (root / "undeclared.txt").write_text("ambient\n", encoding="utf-8")
                elif mutation == "symlink":
                    (root / "diagnostics/summary.txt").unlink()
                    (root / "diagnostics/summary.txt").symlink_to(
                        root / "runtime-bundle.json"
                    )
                elif mutation == "digest":
                    (root / "builder-report.json").write_bytes(b"mutated\n")
                else:
                    result = fixture["result"]
                    assert isinstance(result, dict)
                    result["work_id"] = "f" * 64
                    (root / "product-build-result.json").write_bytes(
                        canonical_bytes(result)
                    )
                with self.assertRaises(ProductInputResolutionError):
                    self._validate(root, fixture)

    def test_handoff_rejects_private_input_material_or_inventory(self) -> None:
        for relative in (
            "inputs/artifacts.json",
            "inputs/objects/package-mini-output-runtime-sha256-" + "a" * 64,
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture = self._write_handoff(root, outcome="success")
                leaked = root / relative
                leaked.parent.mkdir(parents=True, exist_ok=True)
                leaked.write_bytes(b"private composition material must not be published\n")
                with self.assertRaisesRegex(
                    ProductInputResolutionError, "unexpected product handoff"
                ):
                    self._validate(root, fixture)


if __name__ == "__main__":
    unittest.main()
