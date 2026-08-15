from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.oci import FetchedOciBlobV1, FetchedOciRecordV1
from scripts.abi_staging.plan import exact_formula_subject
from scripts.abi_staging.policy import load_tap_staging_policy
from scripts.abi_staging.records import (
    CANDIDATE_RECORD_MEDIA_TYPE,
    validate_candidate_record,
)


TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
KANDELO_ROOT = Path(os.environ["KANDELO_ROOT"])
REQUEST_SHA256 = "a" * 64
SOURCE = {
    "repository": "Automattic/kandelo",
    "commit": "1" * 40,
    "tree": "2" * 40,
}
TAP_SOURCE = {
    "repository": "kandelo-dev/homebrew-tap-core",
    "commit": "3" * 40,
    "tree": "4" * 40,
}
ARCHITECTURE = "wasm32"
TARGET_ABI = 8
TEST_DEFINITION = {
    "hosts": ["build"],
    "id": "bottle-structure",
    "kandelo_paths": [
        "scripts/homebrew-inspect-bottle.py",
        "scripts/test-homebrew-inspect-bottle.sh",
    ],
    "policy": "kandelo-bottle-structure-v1",
}
TEST_SHA256 = canonical_sha256(TEST_DEFINITION)


def _artifact(repository: str, body: bytes) -> dict[str, object]:
    digest = hashlib.sha256(body).hexdigest()
    return {
        "bytes": len(body),
        "immutable_reference": f"ghcr.io/{repository}@sha256:{digest}",
        "sha256": digest,
    }


def _metadata(name: str, bottle: bytes) -> bytes:
    repository = (
        f"kandelo-dev/homebrew-tap-core-abi-{TARGET_ABI}-candidates/{name}"
    )
    return canonical_bytes(
        {
            f"kandelo-dev/tap-core/{name}": {
                "bottle": {
                    "cellar": "any",
                    "rebuild": 0,
                    "root_url": f"https://ghcr.io/v2/{repository}",
                    "tags": {
                        "wasm32_kandelo": {
                            "sha256": hashlib.sha256(bottle).hexdigest()
                        }
                    },
                },
                "formula": {
                    "name": name,
                    "path": (
                        "Library/Taps/kandelo-dev/homebrew-tap-core/"
                        f"Formula/{name}.rb"
                    ),
                    "pkg_version": "1.0",
                },
            }
        }
    )


def _composition(name: str, bottle: bytes) -> bytes:
    repository = (
        f"kandelo-dev/homebrew-tap-core-abi-{TARGET_ABI}-candidates/{name}"
    )
    bottle_sha256 = hashlib.sha256(bottle).hexdigest()
    return canonical_bytes(
        {
            "architecture": ARCHITECTURE,
            "formula": name,
            "kind": "kandelo-homebrew-original-bottle-tree",
            "required_by": [name],
            "schema": 1,
            "tap": TAP_SOURCE["repository"],
            "tree": {
                "activation": {
                    "capabilities": [f"homebrew-bottle:{name}"],
                    "mode": "first-use",
                    "roots": [f"/opt/kandelo/homebrew/Cellar/{name}/1.0"],
                },
                "content": {
                    "bytes": len(bottle),
                    "decoder": "homebrew-bottle-tar-gzip-v1",
                    "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "sha256": bottle_sha256,
                },
                "id": name,
                "inventory": {},
                "package": f"kandelo-dev/tap-core/{name}",
                "transports": [
                    {
                        "kind": "external-https",
                        "url": (
                            f"https://ghcr.io/v2/{repository}/blobs/"
                            f"sha256:{bottle_sha256}"
                        ),
                    }
                ],
            },
        }
    )


def _candidate(
    name: str,
    *,
    bottle: bytes,
    metadata: bytes,
    contract_sha256: str,
    dependencies: list[dict[str, object]],
) -> dict[str, object]:
    repository = (
        f"kandelo-dev/homebrew-tap-core-abi-{TARGET_ABI}-candidates/{name}"
    )
    bottle_artifact = _artifact(repository, bottle)
    metadata_artifact = _artifact(repository, metadata)
    composition_artifact = _artifact(repository, _composition(name, bottle))
    custody_sha256 = "e" * 64
    record = {
        "candidate": {
            "bottle_layer": bottle_artifact,
            "direct_dependency_layers": dependencies,
            "formula": {
                "architecture": ARCHITECTURE,
                "bottle_contract_sha256": contract_sha256,
                "bottle_rebuild": 0,
                "formula": name,
                "revision": 0,
                "tap": TAP_SOURCE["repository"],
                "target_abi": TARGET_ABI,
                "version": "1.0",
            },
            "nonendorsed": True,
            "normalized_components": [
                {
                    "artifact": {
                        "bytes": 1,
                        "immutable_reference": (
                            f"ghcr.io/{repository}@sha256:{contract_sha256}"
                        ),
                        "sha256": contract_sha256,
                    },
                    "id": "bottle-contract",
                },
                {"artifact": metadata_artifact, "id": "bottle-metadata"},
                {
                    "artifact": {
                        "bytes": 1,
                        "immutable_reference": (
                            f"ghcr.io/{repository}@sha256:{custody_sha256}"
                        ),
                        "sha256": custody_sha256,
                    },
                    "id": "source-custody",
                },
                {
                    "artifact": composition_artifact,
                    "id": "vfs-composition-descriptor",
                },
            ],
            "producer": {
                "head": SOURCE["commit"],
                "request_sha256": REQUEST_SHA256,
                "run_id": 707,
            },
            "source_custody_sha256": custody_sha256,
        },
        "common": {
            "artifact": bottle_artifact,
            "artifact_class": "candidate",
            "blockers": [],
            "guard_codes": [],
            "outcome": "success",
            "promotion_state": "unknown",
            "request_sha256": REQUEST_SHA256,
            "retry_state": {
                "attempts": 1,
                "eligible": False,
                "exhausted": False,
                "next_action": "none",
            },
            "run": {
                "job": "publish-candidate",
                "repository": TAP_SOURCE["repository"],
                "run_attempt": 1,
                "run_id": 707,
                "workflow_ref": (
                    ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
                ),
            },
            "source": SOURCE,
            "subject": {
                "identity": (
                    f"{TAP_SOURCE['repository']}/{name}@sha256:"
                    f"{bottle_artifact['sha256']}"
                ),
                "kind": "candidate",
            },
            "work_state": "complete",
        },
        "kind": "kandelo-abi-staging-candidate",
        "schema": 1,
    }
    validate_candidate_record(record)
    return record


def _locator(name: str, character: str) -> dict[str, str]:
    repository = (
        f"ghcr.io/kandelo-dev/homebrew-tap-core-abi-{TARGET_ABI}-candidates/{name}"
    )
    digest = "sha256:" + character * 64
    return {
        "repository": repository,
        "digest": digest,
        "immutable_reference": f"{repository}@{digest}",
    }


def _fixture() -> tuple[dict[str, object], dict[str, FetchedOciRecordV1]]:
    dependency_bottle = b"dependency candidate bottle\n"
    dependency_metadata = _metadata("mini-base", dependency_bottle)
    dependency_record = _candidate(
        "mini-base",
        bottle=dependency_bottle,
        metadata=dependency_metadata,
        contract_sha256="b" * 64,
        dependencies=[],
    )
    dependency_artifact = dependency_record["candidate"]["bottle_layer"]
    target_contract = {
        "direct_dependencies": [
            {
                "architecture": ARCHITECTURE,
                "bottle_layer_bytes": dependency_artifact["bytes"],
                "bottle_layer_sha256": dependency_artifact["sha256"],
                "formula": "mini-base",
                "materialization_policy_sha256": "f" * 64,
            }
        ],
        "target": {"abi": TARGET_ABI},
    }
    target_contract_sha256 = canonical_sha256(target_contract)
    target_bottle = b"target candidate bottle\n"
    target_metadata = _metadata("mini-tool", target_bottle)
    target_record = _candidate(
        "mini-tool",
        bottle=target_bottle,
        metadata=target_metadata,
        contract_sha256=target_contract_sha256,
        dependencies=[
            {
                "artifact": dependency_artifact,
                "id": "mini-base-wasm32",
            }
        ],
    )
    target_locator = _locator("mini-tool", "1")
    dependency_locator = _locator("mini-base", "2")
    subject = exact_formula_subject("mini-tool", ARCHITECTURE)
    formula_plan = {
        "capture": {},
        "contract_sha256": target_contract_sha256,
        "direct_dependencies": [],
        "identity": {
            "architecture": ARCHITECTURE,
            "formula_path": "Formula/mini-tool.rb",
            "name": "mini-tool",
            "normalized_formula_sha256": "d" * 64,
            "rebuild": 0,
            "revision": 0,
            "version": "1.0",
        },
        "required_by_products": ["mini-product"],
        "work_class": "required",
    }
    work = {
        "action": "verify-candidate",
        "artifact_name": "",
        "attempt_ordinal": 0,
        "candidate_locator": target_locator,
        "candidate_record_sha256": "1" * 64,
        "contract_sha256": target_contract_sha256,
        "formula_plan_sha256": canonical_sha256(formula_plan),
        "host": "build",
        "subject": subject,
        "subject_sha256": hashlib.sha256(subject.encode()).hexdigest(),
        "test_definition_sha256": TEST_SHA256,
        "work_class": "required",
        "work_id": "",
    }
    work["work_id"] = canonical_sha256(
        {
            "action": "verify-candidate",
            "attempt_ordinal": 0,
            "candidate_record_sha256": "1" * 64,
            "contract_sha256": target_contract_sha256,
            "host": "build",
            "request_sha256": REQUEST_SHA256,
            "subject": json.loads(subject),
            "test_definition_sha256": TEST_SHA256,
        }
    )
    work["artifact_name"] = f"abi-staging-verification-{work['work_id']}"
    bundle = {
        "mode": "active",
        "request_sha256": REQUEST_SHA256,
        "request": {"build_source": SOURCE},
        "tap_plan": {
            "tap_source": TAP_SOURCE,
            "target_abi": {"version": TARGET_ABI},
            "formulae": [formula_plan],
        },
        "contracts": {subject: target_contract},
        "candidates": {
            "locators": {
                "1" * 64: target_locator,
                "2" * 64: dependency_locator,
            },
            "records": {
                "1" * 64: target_record,
                "2" * 64: dependency_record,
            },
        },
        "verification_tests": [{**TEST_DEFINITION, "sha256": TEST_SHA256}],
        "workflow": {"verify_work": [work]},
    }

    def fetched(
        locator: dict[str, str],
        record: dict[str, object],
        bottle: bytes,
        metadata: bytes,
    ) -> FetchedOciRecordV1:
        repository = locator["repository"]
        bottle_identity = record["candidate"]["bottle_layer"]
        metadata_identity = next(
            item["artifact"]
            for item in record["candidate"]["normalized_components"]
            if item["id"] == "bottle-metadata"
        )
        composition = _composition(record["candidate"]["formula"]["formula"], bottle)
        composition_identity = next(
            item["artifact"]
            for item in record["candidate"]["normalized_components"]
            if item["id"] == "vfs-composition-descriptor"
        )
        return FetchedOciRecordV1(
            repository=repository,
            digest=locator["digest"],
            immutable_reference=locator["immutable_reference"],
            artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
            manifest=b"fixture manifest",
            config=FetchedOciBlobV1(
                role="candidate-record",
                media_type=CANDIDATE_RECORD_MEDIA_TYPE,
                digest="sha256:" + canonical_sha256(record),
                size=len(canonical_bytes(record)),
                title="candidate-record.json",
                body=canonical_bytes(record),
            ),
            layers=(
                FetchedOciBlobV1(
                    role="bottle-layer",
                    media_type="application/vnd.oci.image.layer.v1.tar+gzip",
                    digest="sha256:" + bottle_identity["sha256"],
                    size=bottle_identity["bytes"],
                    title=f"{record['candidate']['formula']['formula']}.tar.gz",
                    body=bottle,
                ),
                FetchedOciBlobV1(
                    role="bottle-metadata",
                    media_type="application/json",
                    digest="sha256:" + metadata_identity["sha256"],
                    size=metadata_identity["bytes"],
                    title="bottle-metadata.json",
                    body=metadata,
                ),
                FetchedOciBlobV1(
                    role="vfs-composition-descriptor",
                    media_type=(
                        "application/vnd.kandelo.homebrew."
                        "vfs-composition-descriptor.v1+json"
                    ),
                    digest="sha256:" + composition_identity["sha256"],
                    size=composition_identity["bytes"],
                    title="vfs-composition-descriptor.json",
                    body=composition,
                ),
            ),
        )

    fetched_records = {
        target_locator["immutable_reference"]: fetched(
            target_locator, target_record, target_bottle, target_metadata
        ),
        dependency_locator["immutable_reference"]: fetched(
            dependency_locator,
            dependency_record,
            dependency_bottle,
            dependency_metadata,
        ),
    }
    return bundle, fetched_records


class VerificationExecutionTests(unittest.TestCase):
    def test_candidate_tap_composition_uses_the_exact_generic_namespace(self) -> None:
        from scripts.abi_staging import execution

        digest = "a" * 64
        root_url = (
            "https://ghcr.io/v2/kandelo-dev/"
            "homebrew-tap-core-abi-8-candidates/asa"
        )
        metadata_root_url = root_url.rsplit("/", 1)[0]
        metadata = canonical_bytes(
            {
                "kandelo-dev/tap-core/asa": {
                    "bottle": {
                        "cellar": "any_skip_relocation",
                        "rebuild": 1,
                        "root_url": metadata_root_url,
                        "tags": {
                            "wasm32_kandelo": {
                                "all_files": ["bin/asa", "INSTALL_RECEIPT.json"],
                                "filename": "asa-15.0.0.wasm32_kandelo.bottle.1.tar.gz",
                                "installed_size": 1234,
                                "local_filename": "asa--15.0.0.wasm32_kandelo.bottle.1.tar.gz",
                                "path_exec_files": ["bin/asa"],
                                "sbom": {},
                                "sha256": digest,
                                "tab": {},
                            }
                        },
                    },
                    "formula": {
                        "desc": "Miniature asa fixture",
                        "homepage": "https://example.test/asa",
                        "license": "MIT",
                        "name": "asa",
                        "path": (
                            "Library/Taps/kandelo-dev/homebrew-tap-core/"
                            "Formula/asa.rb"
                        ),
                        "pkg_version": "15.0.0",
                    },
                }
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata_path = root / "bottle.json"
            metadata_path.write_bytes(metadata)
            composed = execution.compose_candidate_tap(
                tap_root=TAP_ROOT,
                kandelo_root=KANDELO_ROOT,
                destination=root / "tap",
                candidates=[
                    {
                        "architecture": "wasm32",
                        "bottle_layer": {
                            "immutable_reference": (
                                "ghcr.io/kandelo-dev/"
                                "homebrew-tap-core-abi-8-candidates/asa@sha256:"
                                + digest
                            ),
                            "sha256": digest,
                        },
                        "formula": "asa",
                        "metadata": metadata_path,
                        "tap_repository": TAP_SOURCE["repository"],
                        "target_abi": TARGET_ABI,
                    }
                ],
            )
            formula = (composed / "Formula/asa.rb").read_text(encoding="utf-8")
        self.assertIn(f'root_url "{root_url}"', formula)
        self.assertIn(f'wasm32_kandelo: "{digest}"', formula)

    def test_candidate_tap_composition_rejects_foreign_raw_bottle_root(self) -> None:
        from scripts.abi_staging import execution

        digest = "a" * 64
        metadata = canonical_bytes(
            {
                "kandelo-dev/tap-core/asa": {
                    "bottle": {
                        "cellar": "any_skip_relocation",
                        "rebuild": 1,
                        "root_url": "https://ghcr.io/v2/attacker/foreign",
                        "tags": {"wasm32_kandelo": {"sha256": digest}},
                    },
                    "formula": {
                        "name": "asa",
                        "path": (
                            "Library/Taps/kandelo-dev/homebrew-tap-core/"
                            "Formula/asa.rb"
                        ),
                        "pkg_version": "15.0.0",
                    },
                }
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata_path = root / "bottle.json"
            metadata_path.write_bytes(metadata)
            with self.assertRaisesRegex(
                execution.ExecutionError,
                "publication namespace",
            ):
                execution.compose_candidate_tap(
                    tap_root=TAP_ROOT,
                    kandelo_root=KANDELO_ROOT,
                    destination=root / "tap",
                    candidates=[
                        {
                            "architecture": "wasm32",
                            "bottle_layer": {
                                "immutable_reference": (
                                    "ghcr.io/kandelo-dev/"
                                    "homebrew-tap-core-abi-8-candidates/asa@sha256:"
                                    + digest
                                ),
                                "sha256": digest,
                            },
                            "formula": "asa",
                            "metadata": metadata_path,
                            "tap_repository": TAP_SOURCE["repository"],
                            "target_abi": TARGET_ABI,
                        }
                    ],
                )

    def test_candidate_tap_composition_rejects_ambiguous_formula_identity(self) -> None:
        from scripts.abi_staging import execution

        digest = "a" * 64
        entry = {
            "bottle": {
                "cellar": "any_skip_relocation",
                "rebuild": 1,
                "root_url": (
                    "https://ghcr.io/v2/kandelo-dev/"
                    "homebrew-tap-core-abi-8-candidates/asa"
                ),
                "tags": {"wasm32_kandelo": {"sha256": digest}},
            },
            "formula": {
                "name": "asa",
                "path": (
                    "Library/Taps/kandelo-dev/homebrew-tap-core/Formula/asa.rb"
                ),
                "pkg_version": "15.0.0",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata_path = root / "bottle.json"
            metadata_path.write_bytes(
                canonical_bytes(
                    {
                        "asa": entry,
                        "kandelo-dev/tap-core/asa": entry,
                    }
                )
            )
            with self.assertRaisesRegex(
                execution.ExecutionError,
                "exactly one fully qualified Formula",
            ):
                execution.compose_candidate_tap(
                    tap_root=TAP_ROOT,
                    kandelo_root=KANDELO_ROOT,
                    destination=root / "tap",
                    candidates=[
                        {
                            "architecture": "wasm32",
                            "bottle_layer": {
                                "immutable_reference": (
                                    "ghcr.io/kandelo-dev/"
                                    "homebrew-tap-core-abi-8-candidates/asa@sha256:"
                                    + digest
                                ),
                                "sha256": digest,
                            },
                            "formula": "asa",
                            "metadata": metadata_path,
                            "tap_repository": TAP_SOURCE["repository"],
                            "target_abi": TARGET_ABI,
                        }
                    ],
                )

    def test_verification_materializes_the_full_exact_candidate_closure(self) -> None:
        from scripts.abi_staging import execution

        bundle, fetched = _fixture()
        work = bundle["workflow"]["verify_work"][0]
        calls: list[str] = []

        def fetch(locator):
            calls.append(locator["immutable_reference"])
            return fetched[locator["immutable_reference"]]

        with tempfile.TemporaryDirectory() as temporary:
            prepared = execution.prepare_verification_inputs(
                bundle,
                work,
                destination=Path(temporary) / "inputs",
                run={
                    "repository": TAP_SOURCE["repository"],
                    "workflow_ref": (
                        ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
                    ),
                    "run_id": 808,
                    "run_attempt": 2,
                    "job": "verify-candidate",
                },
                fetch_candidate=fetch,
            )
            dependency_contract = json.loads(
                prepared["dependency_provenance"].read_bytes()
            )
            self.assertEqual(
                dependency_contract,
                {
                    "architecture": ARCHITECTURE,
                    "dependency_layers": [
                        {
                            "artifact": bundle["candidates"]["records"]["2" * 64][
                                "candidate"
                            ]["bottle_layer"],
                            "formula": "mini-base",
                        }
                    ],
                    "kind": "kandelo-abi-staging-dependency-layers",
                    "schema": 1,
                    "tap_repository": TAP_SOURCE["repository"],
                    "target_abi": TARGET_ABI,
                },
            )
            self.assertEqual(
                [candidate["formula"] for candidate in prepared["candidates"]],
                ["mini-base", "mini-tool"],
            )
            self.assertEqual(len(calls), 2)
            for candidate in prepared["candidates"]:
                self.assertEqual(
                    candidate["vfs_composition_descriptor"].read_bytes(),
                    _composition(candidate["formula"], candidate["bottle"].read_bytes()),
                )

    def test_missing_transitive_candidate_fails_before_verification(self) -> None:
        from scripts.abi_staging import execution

        bundle, fetched = _fixture()
        del bundle["candidates"]["locators"]["2" * 64]
        del bundle["candidates"]["records"]["2" * 64]
        work = bundle["workflow"]["verify_work"][0]
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            execution.ExecutionError, "dependency"
        ):
            execution.prepare_verification_inputs(
                bundle,
                work,
                destination=Path(temporary) / "inputs",
                run={
                    "repository": TAP_SOURCE["repository"],
                    "workflow_ref": "workflow@refs/heads/main",
                    "run_id": 808,
                    "run_attempt": 2,
                    "job": "verify-candidate",
                },
                fetch_candidate=lambda locator: fetched[locator["immutable_reference"]],
            )

    def test_executor_rechecks_sources_strips_credentials_and_invokes_only_verifier(self) -> None:
        from scripts.abi_staging import execution

        bundle, fetched = _fixture()
        work = bundle["workflow"]["verify_work"][0]
        calls = []
        expected_formula = b"class MiniTool\nend\n"
        support_relative = Path(
            "Kandelo/formula_support/kandelo_formula_support.rb"
        )
        protected_support = (TAP_ROOT / support_relative).read_bytes()

        def snapshot(_root: Path, repository: str) -> dict[str, str]:
            return dict(TAP_SOURCE if repository == TAP_SOURCE["repository"] else SOURCE)

        def compose(**kwargs):
            self.assertEqual(
                [candidate["formula"] for candidate in kwargs["candidates"]],
                ["mini-base", "mini-tool"],
            )
            composed = kwargs["destination"]
            support = composed / support_relative
            support.parent.mkdir(parents=True)
            support.write_bytes(protected_support)
            formula = composed / "Formula" / "mini-tool.rb"
            formula.parent.mkdir(parents=True)
            formula.write_bytes(expected_formula)
            return composed

        def run_process(command, **kwargs):
            calls.append((command, kwargs))
            self.assertEqual(
                command[0], str(KANDELO_ROOT / "scripts/abi-staging-verify-bottle.sh")
            )
            self.assertEqual(kwargs["cwd"], KANDELO_ROOT)
            self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
            self.assertNotIn("HOMEBREW_GITHUB_PACKAGES_TOKEN", kwargs["env"])
            self.assertNotIn("ACTIONS_RUNTIME_TOKEN", kwargs["env"])
            self.assertNotIn("GITHUB_ENV", kwargs["env"])
            self.assertNotIn("RENAMED_WRITE_TOKEN", kwargs["env"])
            self.assertNotIn("NIX_CONFIG", kwargs["env"])
            self.assertEqual(kwargs["env"]["CC"], "/declared/cc")
            self.assertNotIn("GITHUB_ACTIONS", kwargs["env"])
            self.assertNotIn("KANDELO_HOMEBREW_BUILD_USER", kwargs["env"])
            self.assertNotIn("KANDELO_HOMEBREW_RECIPE_USER", kwargs["env"])
            self.assertNotIn("KANDELO_HOMEBREW_SHARED_TEMP", kwargs["env"])
            self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", kwargs["env"])
            self.assertNotIn(
                "HOMEBREW_KANDELO_PLAYWRIGHT_BROWSERS_PATH", kwargs["env"]
            )
            self.assertEqual(
                Path(command[command.index("--playwright-browsers-path") + 1]),
                browsers.resolve(),
            )
            composed_tap = Path(command[command.index("--tap-root") + 1])
            self.assertEqual(
                (composed_tap / "Formula" / "mini-tool.rb").read_bytes(),
                expected_formula,
            )
            self.assertEqual((TAP_ROOT / support_relative).read_bytes(), protected_support)
            self.assertNotEqual(kwargs["env"]["HOME"], "/credentialed/home")
            self.assertEqual(
                kwargs["env"]["XDG_CONFIG_HOME"],
                str(Path(kwargs["env"]["HOME"]) / ".config"),
            )
            for flag in (
                "--candidate-locator",
                "--test-definition",
                "--test-definition-sha256",
                "--host",
                "--attempt-ordinal",
                "--run",
                "--request-binding",
                "--tap-root",
                "--tap-commit",
                "--dependency-provenance",
                "--sysroot-build-root",
                "--forbidden-root",
                "--out",
            ):
                self.assertIn(flag, command)
            request_binding = Path(command[command.index("--request-binding") + 1])
            self.assertEqual(
                json.loads(request_binding.read_bytes()),
                {
                    "request_sha256": bundle["request_sha256"],
                    "source": bundle["request"]["build_source"],
                },
            )
            self.assertEqual(
                Path(command[command.index("--sysroot-build-root") + 1]),
                KANDELO_ROOT,
            )
            return SimpleNamespace(returncode=7)

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            execution, "load_coordination_bundle", return_value=bundle
        ):
            browsers = Path(temporary) / "ms-playwright"
            browsers.mkdir()
            environment = {
                "PATH": os.environ["PATH"],
                "CC": "/declared/cc",
                "GITHUB_ACTIONS": "true",
                "GITHUB_TOKEN": "must-not-survive",
                "HOMEBREW_GITHUB_PACKAGES_TOKEN": "must-not-survive",
                "ACTIONS_RUNTIME_TOKEN": "must-not-survive",
                "GITHUB_ENV": "/credentialed/github-env",
                "KANDELO_HOMEBREW_BUILD_USER": "kandelo-homebrew-build",
                "KANDELO_HOMEBREW_RECIPE_USER": "kandelo-homebrew-recipe",
                "KANDELO_HOMEBREW_SHARED_TEMP": "/tmp/kandelo-homebrew",
                "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
                "RENAMED_WRITE_TOKEN": "must-not-survive",
                "NIX_CONFIG": "access-tokens = github.com=must-not-survive",
                "HOME": "/credentialed/home",
            }
            status = execution.execute_verification_work(
                coordination_path=Path(temporary) / "coordination.json",
                work_id=work["work_id"],
                kandelo_root=KANDELO_ROOT,
                tap_root=TAP_ROOT,
                run={
                    "repository": TAP_SOURCE["repository"],
                    "workflow_ref": (
                        ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
                    ),
                    "run_id": 808,
                    "run_attempt": 2,
                    "job": "verify-candidate",
                },
                output=Path(temporary) / "result",
                snapshot_source=snapshot,
                fetch_candidate=lambda locator: fetched[locator["immutable_reference"]],
                compose_tap=compose,
                run_process=run_process,
                environment=environment,
            )
            invalid_environments = [
                {
                    key: value
                    for key, value in environment.items()
                    if key != "PLAYWRIGHT_BROWSERS_PATH"
                },
            ]
            browser_link = Path(temporary) / "playwright-link"
            browser_link.symlink_to(browsers, target_is_directory=True)
            invalid_environments.append(
                {**environment, "PLAYWRIGHT_BROWSERS_PATH": str(browser_link)}
            )
            for invalid_environment in invalid_environments:
                with self.assertRaisesRegex(
                    execution.ExecutionError,
                    "prepared Playwright browser root is unavailable",
                ):
                    execution.execute_verification_work(
                        coordination_path=Path(temporary) / "coordination.json",
                        work_id=work["work_id"],
                        kandelo_root=KANDELO_ROOT,
                        tap_root=TAP_ROOT,
                        run={
                            "repository": TAP_SOURCE["repository"],
                            "workflow_ref": (
                                ".github/workflows/abi-staging-reconcile.yml"
                                "@refs/heads/main"
                            ),
                            "run_id": 808,
                            "run_attempt": 2,
                            "job": "verify-candidate",
                        },
                        output=Path(temporary) / "invalid-result",
                        snapshot_source=snapshot,
                        fetch_candidate=lambda locator: fetched[
                            locator["immutable_reference"]
                        ],
                        compose_tap=compose,
                        run_process=run_process,
                        environment=invalid_environment,
                    )
        self.assertEqual(status, 7)
        self.assertEqual(len(calls), 1)

    def test_cli_binds_protected_github_run_to_verification(self) -> None:
        from scripts.abi_staging import cli

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            cli, "execute_verification_work", return_value=0
        ) as execute:
            status = cli.main(
                [
                    "execute-verification-work",
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
                    str(Path(temporary) / "result"),
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            execute.call_args.kwargs["run"],
            {
                "repository": TAP_SOURCE["repository"],
                "workflow_ref": (
                    "kandelo-dev/homebrew-tap-core/"
                    ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
                ),
                "run_id": 808,
                "run_attempt": 2,
                "job": "verify-candidate",
            },
        )


if __name__ == "__main__":
    unittest.main()
