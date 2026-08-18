from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile
import tempfile
from unittest import mock
import unittest

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.custody import create_source_custody
from scripts.abi_staging import handoff as handoff_module
from scripts.abi_staging.formula_inventory import normalize_formula_source
from scripts.abi_staging.handoff import (
    HandoffError,
    assemble_handoff,
    build_handoff_inventory,
    build_miniature_build_result_fixture,
    build_miniature_handoff_inventory_fixture,
    load_build_result,
    load_handoff_inventory,
    prepare_composition_input,
    validate_handoff,
    write_handoff_inventory,
)
from scripts.abi_staging.plan import exact_formula_subject


TAP_ROOT = Path(__file__).resolve().parents[3]
SUBJECT = exact_formula_subject("mini-tool", "wasm32")
REQUEST = "a" * 64
CUSTODY_TEMPLATE: Path | None = None
CUSTODY_SOURCES: dict[str, dict[str, str]] | None = None


def _git(root: Path, *arguments: str, capture: bool = False) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Handoff Fixture",
        "GIT_AUTHOR_EMAIL": "handoff@example.test",
        "GIT_COMMITTER_NAME": "Handoff Fixture",
        "GIT_COMMITTER_EMAIL": "handoff@example.test",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        env=environment,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip() if capture else ""


def _fixture_repository(root: Path, filename: str) -> dict[str, str]:
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    (root / filename).write_text(f"exact {filename}\n", encoding="utf-8")
    _git(root, "add", filename)
    _git(root, "commit", "-m", "fixture")
    return {
        "repository": f"example/{root.name}",
        "commit": _git(root, "rev-parse", "HEAD", capture=True),
        "tree": _git(root, "rev-parse", "HEAD^{tree}", capture=True),
    }


def _tar_bytes(*, unsafe: bool = False) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        body = b"payload\n"
        member = tarfile.TarInfo("../escape" if unsafe else "mini/bin/tool")
        member.size = len(body)
        member.mtime = 0
        archive.addfile(member, io.BytesIO(body))
    return stream.getvalue()


def _composition_bottle_bytes() -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        # Homebrew archives the requested Cellar keg and does not emit a
        # separate tar member for the Formula-name parent directory.
        for path in (
            "mini-tool/1.0.0_1",
            "mini-tool/1.0.0_1/bin",
            "mini-tool/1.0.0_1/.brew",
        ):
            member = tarfile.TarInfo(path)
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            member.mtime = 0
            archive.addfile(member)
        for path, body, mode in (
            ("mini-tool/1.0.0_1/bin/mini-tool", b"#!/bin/sh\necho miniature\n", 0o755),
            ("mini-tool/1.0.0_1/.brew/mini-tool.rb", b"class MiniTool < Formula\nend\n", 0o644),
            ("mini-tool/1.0.0_1/INSTALL_RECEIPT.json", b"{}\n", 0o644),
        ):
            member = tarfile.TarInfo(path)
            member.size = len(body)
            member.mode = mode
            member.mtime = 0
            archive.addfile(member, io.BytesIO(body))
    return stream.getvalue()


def _write_handoff(root: Path, *, outcome: str = "success") -> None:
    if CUSTODY_TEMPLATE is None:
        raise AssertionError("source custody test fixture is not initialized")
    shutil.copytree(CUSTODY_TEMPLATE, root / "source-custody")
    (root / "diagnostics").mkdir()
    contract = TAP_ROOT / "Kandelo/staging/fixtures/bottle-contract.json"
    (root / "bottle-contract.json").write_bytes(contract.read_bytes())
    (root / "attempt-record.json").write_bytes(
        canonical_bytes(
            {
                "kind": "kandelo-abi-staging-attempt",
                "schema": 1,
                "request_sha256": REQUEST,
                "subject": SUBJECT,
                "outcome": outcome,
            }
        )
    )
    (root / "diagnostics/summary.txt").write_text("bounded summary\n", encoding="utf-8")

    if outcome == "success":
        (root / "bottle.tar.gz").write_bytes(_tar_bytes())
        (root / "bottle-metadata.json").write_bytes(
            canonical_bytes({"formula": "mini-tool", "architecture": "wasm32"})
        )
        (root / "vfs-composition-descriptor.json").write_bytes(
            canonical_bytes(
                {
                    "schema": 1,
                    "kind": "kandelo-homebrew-original-bottle-tree",
                    "formula": "mini-tool",
                }
            )
        )
        result = build_miniature_build_result_fixture(
            request_sha256=REQUEST,
            subject=SUBJECT,
            outcome="success",
            root=root,
        )
    else:
        result = build_miniature_build_result_fixture(
            request_sha256=REQUEST,
            subject=SUBJECT,
            outcome="failure",
            root=root,
        )
    (root / "build-result.json").write_bytes(canonical_bytes(result))
    write_handoff_inventory(root, subject=SUBJECT, outcome=outcome)


def _validate(
    root: Path,
    *,
    max_files: int = 256,
    max_bytes: int = 4_294_967_296,
) -> dict[str, object]:
    if CUSTODY_SOURCES is None:
        raise AssertionError("source custody test identities are not initialized")
    return validate_handoff(
        root,
        max_files=max_files,
        max_bytes=max_bytes,
        expected_request_sha256=REQUEST,
        expected_subject=SUBJECT,
        expected_kandelo_source=CUSTODY_SOURCES["kandelo"],
        expected_tap_source=CUSTODY_SOURCES["tap"],
    )


class BuildHandoffTests(unittest.TestCase):
    def test_reconstructs_transitive_dependency_layers_from_exact_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contracts = root / "contracts"
            layers = root / "layers"
            contracts.mkdir()
            layers.mkdir()
            dash_body = b"dash candidate bottle\n"
            ed_body = b"ed candidate bottle\n"
            dash_digest = hashlib.sha256(dash_body).hexdigest()
            ed_digest = hashlib.sha256(ed_body).hexdigest()
            (layers / f"sha256-{dash_digest}.tar.gz").write_bytes(dash_body)
            (layers / f"sha256-{ed_digest}.tar.gz").write_bytes(ed_body)
            dash_contract = {
                "direct_dependencies": [],
                "formula": {"name": "dash"},
                "target": {"architecture": "wasm32"},
            }
            ed_contract = {
                "direct_dependencies": [
                    {
                        "architecture": "wasm32",
                        "bottle_layer_bytes": len(dash_body),
                        "bottle_layer_sha256": dash_digest,
                        "formula": "dash",
                        "materialization_policy_sha256": "d" * 64,
                    }
                ],
                "formula": {"name": "ed"},
                "target": {"architecture": "wasm32"},
            }
            dash_contract_sha256 = canonical_sha256(dash_contract)
            ed_contract_sha256 = canonical_sha256(ed_contract)
            (contracts / f"sha256-{dash_contract_sha256}.json").write_bytes(
                canonical_bytes(dash_contract)
            )
            (contracts / f"sha256-{ed_contract_sha256}.json").write_bytes(
                canonical_bytes(ed_contract)
            )
            formulae = [
                {
                    "contract_sha256": ed_contract_sha256,
                    "direct_dependencies": [
                        {
                            "architecture": "wasm32",
                            "formula": "dash",
                            "materialization_policy_sha256": "d" * 64,
                        }
                    ],
                    "identity": {"architecture": "wasm32", "name": "ed"},
                },
                {
                    "contract_sha256": dash_contract_sha256,
                    "direct_dependencies": [],
                    "identity": {"architecture": "wasm32", "name": "dash"},
                },
            ]
            root_plan = {
                "direct_dependencies": [
                    {
                        "architecture": "wasm32",
                        "formula": "ed",
                        "materialization_policy_sha256": "e" * 64,
                    }
                ],
                "identity": {"architecture": "wasm32", "name": "diffutils"},
            }
            root_contract = {
                "direct_dependencies": [
                    {
                        "architecture": "wasm32",
                        "bottle_layer_bytes": len(ed_body),
                        "bottle_layer_sha256": ed_digest,
                        "formula": "ed",
                        "materialization_policy_sha256": "e" * 64,
                    }
                ],
                "formula": {"name": "diffutils"},
                "target": {"architecture": "wasm32"},
            }

            with mock.patch.object(
                handoff_module,
                "load_bottle_contract",
                side_effect=lambda body: json.loads(body),
            ):
                observed = handoff_module._dependency_layer_closure(
                    formulae=formulae,
                    root_formula_plan=root_plan,
                    root_contract=root_contract,
                    dependency_root=root,
                )

        self.assertEqual(
            [entry["formula"] for entry in observed], ["dash", "ed"]
        )
        self.assertEqual(
            [entry["sha256"] for entry in observed],
            [dash_digest, ed_digest],
        )

    def test_materializes_dependency_layers_for_the_normal_builder_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bottle = root / "dependency.tar.gz"
            body = b"exact candidate dependency bottle\n"
            bottle.write_bytes(body)
            digest = hashlib.sha256(body).hexdigest()
            context = root / "context.json"
            context.write_bytes(
                canonical_bytes(
                    {
                        "schema": 1,
                        "kind": "kandelo-abi-staging-build-context",
                        "dependency_layers": [
                            {
                                "formula": "bzip2",
                                "architecture": "wasm32",
                                "sha256": digest,
                                "bytes": len(body),
                                "source_path": str(bottle),
                            }
                        ],
                    }
                )
            )

            output = root / "cache"
            handoff_module.materialize_dependency_layers(
                context_path=context,
                output=output,
            )

            self.assertEqual(
                [path.name for path in output.iterdir()],
                [f"{digest}.tar.gz"],
            )
            self.assertEqual((output / f"{digest}.tar.gz").read_bytes(), body)

    def test_prepares_exact_candidate_dependency_bottle_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tap = root / "tap"
            tap.mkdir()
            _git(tap, "init", "--initial-branch=main")
            (tap / "Formula").mkdir()
            formula_path = tap / "Formula/libcxx.rb"
            formula_path.write_text(
                'class Libcxx < Formula\n'
                '  desc "fixture"\n'
                '  url "https://example.test/libcxx.tar.gz"\n'
                f'  sha256 "{"1" * 64}"\n'
                "\n"
                "  bottle do\n"
                '    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"\n'
                "    rebuild 2\n"
                f'    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "{"2" * 64}"\n'
                "  end\n"
                "end\n",
                encoding="utf-8",
            )
            _git(tap, "add", "Formula/libcxx.rb")
            _git(tap, "commit", "-m", "fixture")
            source_commit = _git(tap, "rev-parse", "HEAD", capture=True)
            source_normalized = normalize_formula_source(formula_path.read_bytes())
            candidate_digest = "3" * 64
            context_path = root / "context.json"
            context = {
                "schema": 1,
                "kind": "kandelo-abi-staging-build-context",
                "tap_source": {
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "commit": source_commit,
                    "tree": _git(tap, "rev-parse", "HEAD^{tree}", capture=True),
                },
                "target_abi": 43,
                "dependency_layers": [
                    {
                        "formula": "libcxx",
                        "architecture": "wasm32",
                        "sha256": candidate_digest,
                        "bytes": 12,
                        "source_path": str(root / "unused.tar.gz"),
                    }
                ],
            }
            context_path.write_bytes(canonical_bytes(context))

            changed_context = dict(context)
            changed_context["tap_source"] = {
                **context["tap_source"],
                "tree": "4" * 40,
            }
            changed_context_path = root / "changed-context.json"
            changed_context_path.write_bytes(canonical_bytes(changed_context))
            with self.assertRaisesRegex(
                HandoffError, "tap checkout differs from the exact build context"
            ):
                handoff_module.prepare_dependency_tap(
                    context_path=changed_context_path,
                    tap_root=tap,
                    output=root / "rejected",
                )

            prepared = handoff_module.prepare_dependency_tap(
                context_path=context_path,
                tap_root=tap,
                output=root / "prepared",
            )

            prepared_formula = prepared / "Formula/libcxx.rb"
            prepared_source = prepared_formula.read_bytes()
            self.assertEqual(
                normalize_formula_source(prepared_source),
                source_normalized,
            )
            self.assertIn(
                b'root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core-abi-43-candidates"',
                prepared_source,
            )
            self.assertIn(candidate_digest.encode("ascii"), prepared_source)
            self.assertNotEqual(
                _git(prepared, "rev-parse", "HEAD", capture=True), source_commit
            )
            self.assertEqual(
                _git(prepared, "status", "--short", capture=True), ""
            )

            wasm64_digest = "4" * 64
            wasm64_context = {
                **context,
                "dependency_layers": [
                    {
                        **context["dependency_layers"][0],
                        "architecture": "wasm64",
                        "sha256": wasm64_digest,
                    }
                ],
            }
            wasm64_context_path = root / "wasm64-context.json"
            wasm64_context_path.write_bytes(canonical_bytes(wasm64_context))

            prepared_wasm64 = handoff_module.prepare_dependency_tap(
                context_path=wasm64_context_path,
                tap_root=tap,
                output=root / "prepared-wasm64",
            )

            prepared_wasm64_source = (
                prepared_wasm64 / "Formula/libcxx.rb"
            ).read_bytes()
            self.assertEqual(
                normalize_formula_source(prepared_wasm64_source),
                source_normalized,
            )
            self.assertIn(
                f'wasm32_kandelo: "{"2" * 64}"'.encode("ascii"),
                prepared_wasm64_source,
            )
            self.assertIn(
                f'wasm64_kandelo: "{wasm64_digest}"'.encode("ascii"),
                prepared_wasm64_source,
            )
            self.assertLess(
                prepared_wasm64_source.index(b"wasm32_kandelo"),
                prepared_wasm64_source.index(b"wasm64_kandelo"),
            )
            self.assertEqual(
                _git(prepared_wasm64, "status", "--short", capture=True), ""
            )

    def test_prepares_first_candidate_dependency_before_formula_promotion(self) -> None:
        source = (
            'class Dash < Formula\n'
            '  desc "fixture"\n'
            '  url "https://example.test/dash.tar.gz"\n'
            f'  sha256 "{"1" * 64}"\n'
            'end\n'
        ).encode("utf-8")
        digest = "2" * 64

        prepared = handoff_module._candidate_dependency_formula_source(
            source,
            root_url=(
                "https://ghcr.io/v2/"
                "kandelo-dev/homebrew-tap-core-abi-43-candidates"
            ),
            architecture="wasm32",
            digest=digest,
        )

        self.assertEqual(normalize_formula_source(prepared), source)
        self.assertIn(b"  bottle do\n", prepared)
        self.assertIn(digest.encode("ascii"), prepared)

    def test_git_identity_accepts_an_exact_protected_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            expected = _fixture_repository(root, "source.txt")
            run = subprocess.run

            def run_as_different_owner(*arguments: object, **keywords: object) -> object:
                keywords["env"] = {
                    **keywords["env"],
                    "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
                }
                return run(*arguments, **keywords)

            with mock.patch.object(
                handoff_module.subprocess,
                "run",
                side_effect=run_as_different_owner,
            ):
                identity = handoff_module._git_identity(root, "Kandelo")

        self.assertEqual(identity, (expected["commit"], expected["tree"]))

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.custody_temporary = tempfile.TemporaryDirectory()
        fixture_root = Path(cls.custody_temporary.name)
        kandelo = fixture_root / "kandelo"
        tap = fixture_root / "homebrew-tap-core"
        kandelo_source = _fixture_repository(kandelo, "kernel.txt")
        tap_source = _fixture_repository(tap, "Formula.rb")
        global CUSTODY_SOURCES, CUSTODY_TEMPLATE
        CUSTODY_TEMPLATE = fixture_root / "custody"
        CUSTODY_SOURCES = {"kandelo": kandelo_source, "tap": tap_source}
        create_source_custody(
            kandelo_root=kandelo,
            tap_root=tap,
            kandelo_source=kandelo_source,
            tap_source=tap_source,
            request_sha256=REQUEST,
            subject=SUBJECT,
            output=CUSTODY_TEMPLATE,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        global CUSTODY_SOURCES, CUSTODY_TEMPLATE
        CUSTODY_TEMPLATE = None
        CUSTODY_SOURCES = None
        cls.custody_temporary.cleanup()
        super().tearDownClass()

    def test_success_and_failure_handoffs_are_exact_and_self_consistent(self) -> None:
        for outcome in ("success", "failure"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_handoff(root, outcome=outcome)
                validated = _validate(root)
                self.assertEqual(validated["outcome"], outcome)
                self.assertEqual(validated["subject"], SUBJECT)
                self.assertEqual(validated["request_sha256"], REQUEST)
                self.assertEqual(validated["candidate"] is not None, outcome == "success")

    def test_assembly_canonicalizes_homebrew_bottle_metadata(self) -> None:
        if CUSTODY_SOURCES is None or CUSTODY_TEMPLATE is None:
            raise AssertionError("source custody fixtures are not initialized")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw_output = root / "raw-output"
            bottles = raw_output / "bottles"
            bottles.mkdir(parents=True)
            (raw_output / "diagnostics").mkdir()
            (raw_output / "diagnostics/summary.txt").write_text(
                "bounded summary\n", encoding="utf-8"
            )
            (bottles / "mini-tool.tar.gz").write_bytes(_tar_bytes())
            metadata = {
                "kandelo-dev/tap-core/mini-tool": {
                    "formula": {"name": "mini-tool"},
                    "bottle": {"rebuild": 0},
                }
            }
            (bottles / "mini-tool.bottle.json").write_text(
                json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
            )
            (bottles / "mini-tool.vfs-composition.json").write_bytes(
                canonical_bytes(
                    {
                        "schema": 1,
                        "kind": "kandelo-homebrew-original-bottle-tree",
                        "formula": "mini-tool",
                    }
                )
            )
            contract_path = TAP_ROOT / "Kandelo/staging/fixtures/bottle-contract.json"
            context = {
                "schema": 1,
                "kind": "kandelo-abi-staging-build-context",
                "request_sha256": REQUEST,
                "subject": SUBJECT,
                "request_source": CUSTODY_SOURCES["kandelo"],
                "tap_source": CUSTODY_SOURCES["tap"],
                "formula": "mini-tool",
                "architecture": "wasm32",
                "target_abi": 9,
                "run": {
                    "repository": "kandelo-dev/homebrew-tap-core",
                    "workflow_ref": (
                        ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
                    ),
                    "run_id": 1,
                    "run_attempt": 1,
                    "job": "build-candidate",
                },
                "retry_ordinal": 0,
                "bottle_contract_sha256": hashlib.sha256(
                    contract_path.read_bytes()
                ).hexdigest(),
                "bottle_contract_path": str(contract_path),
            }
            context_path = root / "context.json"
            context_path.write_bytes(canonical_bytes(context))
            handoff = root / "handoff"

            assemble_handoff(
                context_path=context_path,
                raw_output=raw_output,
                source_custody=CUSTODY_TEMPLATE,
                handoff=handoff,
                exit_code=0,
            )

            self.assertEqual(
                (handoff / "bottle-metadata.json").read_bytes(),
                canonical_bytes(metadata),
            )

    def test_composition_input_is_derived_from_exact_plan_bottle_and_guest_layout(self) -> None:
        bottle = _composition_bottle_bytes()
        bottle_sha256 = hashlib.sha256(bottle).hexdigest()
        candidate_root_url = (
            "https://ghcr.io/v2/kandelo-dev/"
            "homebrew-tap-core-abi-9-candidates/mini-tool"
        )
        metadata_root_url = candidate_root_url.rsplit("/", 1)[0]
        metadata = {
            "kandelo-dev/tap-core/mini-tool": {
                "formula": {
                    "name": "mini-tool",
                    "path": (
                        "Library/Taps/kandelo-dev/homebrew-tap-core/"
                        "Formula/mini-tool.rb"
                    ),
                    "pkg_version": "1.0.0_1",
                },
                "bottle": {
                    "root_url": metadata_root_url,
                    "cellar": "any_skip_relocation",
                    "rebuild": 2,
                    "tags": {
                        "wasm32_kandelo": {
                            "local_filename": (
                                "mini-tool--1.0.0_1.wasm32_kandelo.bottle.2.tar.gz"
                            ),
                            "sha256": bottle_sha256,
                        }
                    },
                },
            }
        }
        context = {
            "schema": 1,
            "kind": "kandelo-abi-staging-build-context",
            "request_source": {
                "repository": "Automattic/kandelo",
                "commit": "1" * 40,
                "tree": "2" * 40,
            },
            "tap_source": {
                "repository": "kandelo-dev/homebrew-tap-core",
                "commit": "3" * 40,
                "tree": "4" * 40,
            },
            "formula": "mini-tool",
            "architecture": "wasm32",
            "target_abi": 9,
            "bottle_root_url": candidate_root_url,
            "formula_identity": {
                "name": "mini-tool",
                "version": "1.0.0",
                "revision": 1,
                "rebuild": 2,
                "architecture": "wasm32",
                "formula_path": "Formula/mini-tool.rb",
                "normalized_formula_sha256": "5" * 64,
            },
            "composition_roots": ["mini-shell"],
        }
        guest_layout = {
            "schema": 1,
            "kind": "kandelo-homebrew-guest-layout",
            "prefix": "/opt/kandelo/homebrew",
            "cellar": "/opt/kandelo/homebrew/Cellar",
            "repository": "/opt/kandelo/homebrew",
            "stable_entrypoint": "/opt/kandelo/homebrew/bin/brew",
            "retired_prefixes": [],
        }

        prepared = prepare_composition_input(
            context=context,
            bottle_body=bottle,
            metadata_body=json.dumps(metadata).encode(),
            guest_layout_body=canonical_bytes(guest_layout),
        )

        self.assertEqual(prepared["formula"]["pkg_version"], "1.0.0_1")
        self.assertEqual(prepared["required_by"], ["mini-shell"])
        self.assertEqual(
            prepared["bottle"]["immutable_reference"],
            "ghcr.io/kandelo-dev/homebrew-tap-core-abi-9-candidates/"
            f"mini-tool@sha256:{bottle_sha256}",
        )
        self.assertEqual(
            prepared["bottle"]["transport_url"],
            f"{candidate_root_url}/blobs/sha256:{bottle_sha256}",
        )
        self.assertEqual(prepared["link_manifest"]["version"], "1.0.0_1")

        rich_metadata = copy.deepcopy(metadata)
        rich_entry = rich_metadata["kandelo-dev/tap-core/mini-tool"]
        rich_entry["formula"] = {
            "desc": "Miniature exact bottle fixture",
            "homepage": "https://example.test/mini-tool",
            "license": "MIT",
            **rich_entry["formula"],
            "tap_git_path": "Formula/mini-tool.rb",
            "tap_git_remote": (
                "file:///home/runner/work/homebrew-tap-core/"
                "homebrew-tap-core/tap-authority"
            ),
            "tap_git_revision": context["tap_source"]["commit"],
        }
        rich_entry["bottle"] = {
            **rich_entry["bottle"],
            "date": "2026-08-14T12:00:00Z",
        }
        rich_entry["bottle"]["tags"]["wasm32_kandelo"] = {
            "all_files": [
                "bin/mini-tool",
                "INSTALL_RECEIPT.json",
                ".brew/mini-tool.rb",
            ],
            "filename": (
                "mini-tool-1.0.0_1.wasm32_kandelo.bottle.2.tar.gz"
            ),
            "installed_size": len(bottle),
            "local_filename": (
                "mini-tool--1.0.0_1.wasm32_kandelo.bottle.2.tar.gz"
            ),
            "path_exec_files": ["bin/mini-tool"],
            "sbom": {},
            "sha256": bottle_sha256,
            "tab": {},
        }
        rich_prepared = prepare_composition_input(
            context=context,
            bottle_body=bottle,
            metadata_body=json.dumps(rich_metadata).encode(),
            guest_layout_body=canonical_bytes(guest_layout),
        )
        self.assertEqual(rich_prepared, prepared)

        rich_mutations = []
        prepared_revision = copy.deepcopy(rich_metadata)
        prepared_revision["kandelo-dev/tap-core/mini-tool"]["formula"][
            "tap_git_revision"
        ] = "f" * 40
        self.assertEqual(
            prepare_composition_input(
                context=context,
                bottle_body=bottle,
                metadata_body=json.dumps(prepared_revision).encode(),
                guest_layout_body=canonical_bytes(guest_layout),
            ),
            prepared,
        )
        foreign_inventory = copy.deepcopy(rich_metadata)
        foreign_tag = foreign_inventory["kandelo-dev/tap-core/mini-tool"]["bottle"][
            "tags"
        ]["wasm32_kandelo"]
        foreign_tag["all_files"].append("share/foreign")
        foreign_tag["path_exec_files"] = []
        self.assertEqual(
            prepare_composition_input(
                context=context,
                bottle_body=bottle,
                metadata_body=json.dumps(foreign_inventory).encode(),
                guest_layout_body=canonical_bytes(guest_layout),
            ),
            prepared,
        )
        extra_field = copy.deepcopy(rich_metadata)
        extra_field["kandelo-dev/tap-core/mini-tool"]["bottle"]["tags"][
            "wasm32_kandelo"
        ]["trusted"] = True
        rich_mutations.append(extra_field)
        duplicate_inventory = copy.deepcopy(rich_metadata)
        duplicate_inventory["kandelo-dev/tap-core/mini-tool"]["bottle"]["tags"][
            "wasm32_kandelo"
        ]["all_files"].append("bin/mini-tool")
        rich_mutations.append(duplicate_inventory)
        for mutation in rich_mutations:
            with self.subTest(rich_mutation=mutation), self.assertRaises(HandoffError):
                prepare_composition_input(
                    context=context,
                    bottle_body=bottle,
                    metadata_body=json.dumps(mutation).encode(),
                    guest_layout_body=canonical_bytes(guest_layout),
                )

        hostile = copy.deepcopy(metadata)
        hostile["kandelo-dev/tap-core/mini-tool"]["bottle"]["root_url"] = (
            "https://ghcr.io/v2/attacker/foreign"
        )
        with self.assertRaises(HandoffError):
            prepare_composition_input(
                context=context,
                bottle_body=bottle,
                metadata_body=json.dumps(hostile).encode(),
                guest_layout_body=canonical_bytes(guest_layout),
            )

    def test_build_run_is_canonical_and_bound_to_the_tap_build_job(self) -> None:
        loader = getattr(handoff_module, "load_build_run", None)
        self.assertTrue(callable(loader), "build run loading is absent")
        assert loader is not None
        run = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "workflow_ref": (
                ".github/workflows/abi-staging-reconcile.yml@refs/heads/main"
            ),
            "run_id": 808,
            "run_attempt": 2,
            "job": "build-candidate",
        }
        body = canonical_bytes(run)
        self.assertEqual(
            loader(
                body,
                expected_repository="kandelo-dev/homebrew-tap-core",
            ),
            run,
        )
        for mutation in (
            {**run, "run_id": 0},
            {**run, "job": "verify-candidate"},
            {**run, "repository": "example/homebrew-other"},
            {**run, "trusted": True},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(HandoffError):
                loader(
                    canonical_bytes(mutation),
                    expected_repository="kandelo-dev/homebrew-tap-core",
                )

    def test_protected_validation_requires_external_request_subject_and_tap_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_handoff(root)
            assert CUSTODY_SOURCES is not None
            cases = (
                {"expected_request_sha256": "f" * 64},
                {"expected_subject": exact_formula_subject("other", "wasm32")},
                {
                    "expected_tap_source": {
                        **CUSTODY_SOURCES["tap"],
                        "commit": "f" * 40,
                    }
                },
            )
            defaults = {
                "expected_request_sha256": REQUEST,
                "expected_subject": SUBJECT,
                "expected_kandelo_source": CUSTODY_SOURCES["kandelo"],
                "expected_tap_source": CUSTODY_SOURCES["tap"],
            }
            for changes in cases:
                arguments = {**defaults, **changes}
                with self.subTest(changes=changes), self.assertRaises(HandoffError):
                    validate_handoff(
                        root,
                        max_files=256,
                        max_bytes=4_294_967_296,
                        **arguments,
                    )

    def test_unknown_unlisted_count_and_size_overflow_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_handoff(root)
            (root / "unknown.txt").write_text("not listed\n", encoding="utf-8")
            with self.assertRaisesRegex(HandoffError, "unlisted|unexpected"):
                _validate(root)
            (root / "unknown.txt").unlink()
            with self.assertRaisesRegex(HandoffError, "file count"):
                _validate(root, max_files=2)
            with self.assertRaisesRegex(HandoffError, "byte"):
                _validate(root, max_bytes=10)

    def test_symlink_hardlink_fifo_socket_and_path_escape_are_rejected(self) -> None:
        hazards = ("symlink", "hardlink", "fifo", "socket")
        for hazard in hazards:
            with self.subTest(hazard=hazard), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_handoff(root)
                target = root / "diagnostics/hazard"
                if hazard == "symlink":
                    target.symlink_to("summary.txt")
                elif hazard == "hardlink":
                    os.link(root / "diagnostics/summary.txt", target)
                elif hazard == "fifo":
                    os.mkfifo(target)
                else:
                    connection = socket.socket(socket.AF_UNIX)
                    self.addCleanup(connection.close)
                    connection.bind(str(target))
                with self.assertRaises(HandoffError):
                    _validate(root)

        inventory = build_miniature_handoff_inventory_fixture()
        escaped = copy.deepcopy(inventory)
        escaped["files"][0]["path"] = "../escape"
        with self.assertRaises(HandoffError):
            load_handoff_inventory(canonical_bytes(escaped))
        duplicate = copy.deepcopy(inventory)
        duplicate["files"].append(copy.deepcopy(duplicate["files"][0]))
        with self.assertRaises(HandoffError):
            load_handoff_inventory(canonical_bytes(duplicate))

    def test_digest_size_and_inventory_mutation_are_rejected(self) -> None:
        for mutation in ("digest", "size", "unknown-field"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_handoff(root)
                inventory_path = root / "inventory.json"
                inventory = json.loads(inventory_path.read_bytes())
                if mutation == "digest":
                    inventory["files"][0]["sha256"] = "0" * 64
                elif mutation == "size":
                    inventory["files"][0]["bytes"] += 1
                else:
                    inventory["files"][0]["trusted"] = True
                inventory_path.write_bytes(canonical_bytes(inventory))
                with self.assertRaises(HandoffError):
                    _validate(root)

    def test_result_cannot_claim_a_candidate_on_failure_or_omit_one_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_handoff(root)
            result_path = root / "build-result.json"
            result = json.loads(result_path.read_bytes())
            candidate = copy.deepcopy(result["candidate"])
            for outcome, value in (("failure", candidate), ("success", None)):
                invalid = copy.deepcopy(result)
                invalid["outcome"] = outcome
                invalid["exit_code"] = 1 if outcome == "failure" else 0
                invalid["candidate"] = value
                with self.subTest(outcome=outcome), self.assertRaises(HandoffError):
                    load_build_result(canonical_bytes(invalid))

    def test_diagnostics_cannot_contain_secret_shaped_values(self) -> None:
        for secret in (
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
            "AKIAABCDEFGHIJKLMNOP",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
        ):
            with self.subTest(secret=secret[:8]), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_handoff(root)
                (root / "diagnostics/summary.txt").write_text(secret, encoding="utf-8")
                write_handoff_inventory(root, subject=SUBJECT, outcome="success")
                with self.assertRaisesRegex(HandoffError, "secret"):
                    _validate(root)

    def test_archives_are_listed_without_extraction_and_unsafe_members_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_handoff(root)
            (root / "bottle.tar.gz").write_bytes(_tar_bytes(unsafe=True))
            result = build_miniature_build_result_fixture(
                request_sha256=REQUEST,
                subject=SUBJECT,
                outcome="success",
                root=root,
            )
            (root / "build-result.json").write_bytes(canonical_bytes(result))
            write_handoff_inventory(root, subject=SUBJECT, outcome="success")
            with self.assertRaisesRegex(HandoffError, "archive"):
                _validate(root)
            self.assertFalse((root.parent / "escape").exists())

    def test_checked_fixtures_are_canonical_and_repeatable(self) -> None:
        fixtures = TAP_ROOT / "Kandelo/staging/fixtures/build-handoff"
        inventory = build_miniature_handoff_inventory_fixture()
        result = build_miniature_build_result_fixture()
        self.assertEqual(
            (fixtures / "inventory.json").read_bytes(), canonical_bytes(inventory)
        )
        self.assertEqual(
            (fixtures / "build-result.json").read_bytes(), canonical_bytes(result)
        )
        self.assertEqual(load_handoff_inventory(canonical_bytes(inventory)), inventory)
        self.assertEqual(load_build_result(canonical_bytes(result)), result)


if __name__ == "__main__":
    unittest.main()
