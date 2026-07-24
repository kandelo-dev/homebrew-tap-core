#!/usr/bin/env python3
"""Tests for the local ABI 42 rollout controller."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib.util
import io
import json
import pathlib
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock


SCRIPT = pathlib.Path(__file__).with_name("abi42-rollout.py")
SPEC = importlib.util.spec_from_file_location("abi42_rollout", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
rollout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rollout
SPEC.loader.exec_module(rollout)


class FakeGitHub:
    def __init__(self) -> None:
        self.by_status: dict[str, dict] = {}
        self.jobs_by_run: dict[int, tuple[dict, ...]] = {}
        self.runs_by_id: dict[int, dict] = {}

    def runs(self, status=None, per_page=100):
        del per_page
        return self.by_status.get(
            status,
            {"total_count": 0, "workflow_runs": []},
        )

    def jobs(self, run_id):
        return self.jobs_by_run.get(run_id, ())

    def run(self, run_id):
        return self.runs_by_id[run_id]

    def dispatch(self, formula, arches, tap_sha):
        raise AssertionError(
            "recovery must never dispatch "
            f"{formula} for {tuple(arches)} from {tap_sha}"
        )

    def workflow(self):
        return {
            "id": rollout.WORKFLOW_ID,
            "path": rollout.WORKFLOW_PATH,
            "state": "active",
        }


class RolloutControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = SCRIPT.parent.parent
        cls.tap = rollout.GitTap(cls.root)
        cls.head = cls.tap.git("rev-parse", "HEAD").stdout.strip()
        cls.snapshot = rollout.load_snapshot(cls.tap, cls.head)
        match = re.search(
            r"reusable-homebrew-bottle-publish\.yml@([0-9a-f]{40})",
            cls.snapshot.workflow_source,
        )
        assert match is not None
        cls.publisher_sha = match.group(1)

    def _submitted_state(
        self,
        *,
        formula: str = "asa",
        arches: tuple[str, ...] = ("wasm32",),
        before_run_ids: tuple[int, ...] = (),
    ) -> dict:
        state = rollout.initial_state(self.snapshot, self.publisher_sha)
        state["unresolved_dispatch"] = {
            "formula": formula,
            "arches": list(arches),
            "tap_sha": self.head,
            "recorded_at": "2026-07-24T06:46:34Z",
            "before_run_ids": list(before_run_ids),
            "status": "submitted",
            "submitted_at": "2026-07-24T06:46:35Z",
        }
        return state

    @staticmethod
    def _candidate_github(
        *runs: dict,
        jobs_by_run: dict[int, tuple[dict, ...]] | None = None,
        total_count: int | None = None,
    ) -> FakeGitHub:
        github = FakeGitHub()
        github.by_status[None] = {
            "total_count": len(runs) if total_count is None else total_count,
            "workflow_runs": list(runs),
        }
        github.jobs_by_run = jobs_by_run or {}
        return github

    def _recover(
        self,
        github: FakeGitHub,
        state: dict,
    ) -> tuple[tuple[str, int], dict]:
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)
            with mock.patch.object(
                self.tap, "main_without_fetch", return_value=self.head
            ):
                result = rollout.recover_submitted_dispatch(
                    tap=self.tap,
                    github=github,
                    expected_kandelo_sha=self.publisher_sha,
                    state_path=state_path,
                    no_fetch=True,
                )
            recovered = rollout.read_state(state_path)
            assert recovered is not None
            self.assertEqual(0o600, state_path.stat().st_mode & 0o777)
            return result, recovered

    def _abandon(
        self,
        github: FakeGitHub,
        state: dict,
        *,
        run_id: int = 123,
    ) -> tuple[tuple[str, int], dict]:
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)
            with mock.patch.object(
                self.tap, "main_without_fetch", return_value=self.head
            ):
                result = rollout.abandon_submitted_dispatch(
                    tap=self.tap,
                    github=github,
                    expected_kandelo_sha=self.publisher_sha,
                    state_path=state_path,
                    run_id=run_id,
                    no_fetch=True,
                )
            abandoned = rollout.read_state(state_path)
            assert abandoned is not None
            self.assertEqual(0o600, state_path.stat().st_mode & 0o777)
            return result, abandoned

    def _assert_recovery_fails_unchanged(
        self,
        pattern: str,
        github: FakeGitHub,
        state: dict,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)
            original = state_path.read_bytes()
            with (
                mock.patch.object(
                    self.tap, "main_without_fetch", return_value=self.head
                ),
                self.assertRaisesRegex(rollout.RolloutError, pattern),
            ):
                rollout.recover_submitted_dispatch(
                    tap=self.tap,
                    github=github,
                    expected_kandelo_sha=self.publisher_sha,
                    state_path=state_path,
                    no_fetch=True,
                )
            self.assertEqual(original, state_path.read_bytes())

    def _assert_abandon_fails_unchanged(
        self,
        pattern: str,
        github: FakeGitHub,
        state: dict,
        *,
        run_id: int = 123,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)
            original = state_path.read_bytes()
            with (
                mock.patch.object(
                    self.tap, "main_without_fetch", return_value=self.head
                ),
                self.assertRaisesRegex(rollout.RolloutError, pattern),
            ):
                rollout.abandon_submitted_dispatch(
                    tap=self.tap,
                    github=github,
                    expected_kandelo_sha=self.publisher_sha,
                    state_path=state_path,
                    run_id=run_id,
                    no_fetch=True,
                )
            self.assertEqual(original, state_path.read_bytes())

    @staticmethod
    def _run(
        run_id: int,
        head_sha: str,
        *,
        status: str = "in_progress",
        conclusion=None,
        event: str = "repository_dispatch",
    ) -> dict:
        return {
            "id": run_id,
            "event": event,
            "head_sha": head_sha,
            "status": status,
            "conclusion": conclusion,
        }

    @staticmethod
    def _matrix_jobs(
        formula: str,
        *arches: str,
    ) -> tuple[dict, ...]:
        return tuple(
            {"name": f"publish / build-and-test ({formula}, {arch})"}
            for arch in arches
        )

    @staticmethod
    def _never_started_write_jobs() -> tuple[dict, ...]:
        return tuple(
            {
                "name": f"publish / {stage}",
                "status": "completed",
                "conclusion": "cancelled",
                "steps": [],
            }
            for stage in sorted(rollout.EXTERNAL_WRITE_JOB_STAGES)
        )

    @staticmethod
    def _identity_source(
        *,
        version: str | None,
        revision: int,
        rebuild: int = 1,
    ) -> str:
        version_line = f'  version "{version}"\n' if version is not None else ""
        revision_line = f"  revision {revision}\n" if revision else ""
        return (
            "class Asa < Formula\n"
            f"{version_line}"
            f"{revision_line}"
            "  bottle do\n"
            f'    root_url "{rollout.BOTTLE_ROOT}"\n'
            f"    rebuild {rebuild}\n"
            '    sha256 cellar: :any_skip_relocation, '
            f'wasm32_kandelo: "{"0" * 64}"\n'
            "  end\n"
            "end\n"
        )

    def _load_snapshot_view(
        self,
        *,
        metadata: dict | None = None,
        formula_sources: dict[str, str] | None = None,
        formula_sidecars: dict[str, dict | None] | None = None,
    ):
        source_overrides = formula_sources or {}
        sidecar_overrides = formula_sidecars or {}
        tap = mock.Mock(wraps=self.tap)

        def show(revision, path):
            if path == "Kandelo/metadata.json" and metadata is not None:
                return json.dumps(metadata)
            if path.startswith("Formula/"):
                formula = pathlib.PurePosixPath(path).stem
                if formula in source_overrides:
                    return source_overrides[formula]
            return self.tap.show(revision, path)

        def show_optional(revision, path):
            if path.startswith("Kandelo/formula/"):
                formula = pathlib.PurePosixPath(path).stem
                if formula in sidecar_overrides:
                    sidecar = sidecar_overrides[formula]
                    return None if sidecar is None else json.dumps(sidecar)
            return self.tap.show_optional(revision, path)

        tap.show.side_effect = show
        tap.show_optional.side_effect = show_optional
        return rollout.load_snapshot(tap, self.head)

    def test_exact_plan_has_63_formulae_and_70_architecture_identities(self):
        self.assertEqual(63, len(rollout.FORMULA_ORDER))
        self.assertEqual(63, len(set(rollout.FORMULA_ORDER)))
        self.assertEqual(
            70,
            sum(
                len(rollout.required_arches(formula))
                for formula in rollout.FORMULA_ORDER
            ),
        )
        self.assertEqual(
            ("libcxx", "musl-fts", "openssl", "sqlite", "zlib"),
            tuple(
                formula
                for formula in rollout.WAVES[0]
                if formula in rollout.DUAL_ARCH_ROOTS
            ),
        )
        self.assertEqual(frozenset(("libcurl",)), rollout.DUAL_ARCH_SECOND)
        self.assertEqual(frozenset(("curl",)), rollout.DUAL_ARCH_THIRD)
        self.assertEqual(
            "3.13.3_1-1",
            self.snapshot.identities["python"].top_reference,
        )
        self.assertEqual(
            "1.3.1_4-2",
            self.snapshot.identities["zlib"].top_reference,
        )
        self.assertEqual("pr-1079-staging", rollout.PREPUBLICATION_STAGING_TAG)
        self.assertEqual(
            "437fde2524ea6ad9c44933f8abbf995a46841009",
            rollout.PREPUBLICATION_GENERATION_SHA,
        )

    def test_explicit_base_version_becomes_canonical_homebrew_pkg_version(self):
        unrevisioned = rollout.parse_formula_identity(
            "asa",
            self._identity_source(version="1.2.3", revision=0),
            None,
        )
        revised = rollout.parse_formula_identity(
            "asa",
            self._identity_source(version="1.2.3", revision=2),
            None,
        )

        self.assertEqual("1.2.3", unrevisioned.pkg_version)
        self.assertEqual("1.2.3-1", unrevisioned.top_reference)
        self.assertEqual("1.2.3_2", revised.pkg_version)
        self.assertEqual("1.2.3_2-1", revised.top_reference)

    def test_inferred_base_version_tracks_formula_revision_changes(self):
        unchanged = rollout.parse_formula_identity(
            "asa",
            self._identity_source(version=None, revision=2),
            {
                "version": "1.2.3_2",
                "formula_revision": 2,
            },
        )
        advanced = rollout.parse_formula_identity(
            "asa",
            self._identity_source(version=None, revision=3),
            {
                "version": "1.2.3_2",
                "formula_revision": 2,
            },
        )

        self.assertEqual("1.2.3_2", unchanged.pkg_version)
        self.assertEqual("1.2.3_3", advanced.pkg_version)
        self.assertEqual("1.2.3_3-1", advanced.top_reference)

    def test_inferred_base_version_rejects_noncanonical_previous_pkg_version(self):
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "does not match its Formula revision",
        ):
            rollout.parse_formula_identity(
                "asa",
                self._identity_source(version=None, revision=2),
                {
                    "version": "1.2.3",
                    "formula_revision": 2,
                },
            )

    def test_first_abi42_finalization_keeps_continuation_versions_in_sidecars(self):
        asa_package = copy.deepcopy(self.snapshot.formula_sidecars["asa"])
        self.assertIsNotNone(asa_package)
        collapsed_metadata = copy.deepcopy(self.snapshot.metadata)
        collapsed_metadata["kandelo_abi"] = rollout.EXPECTED_ABI
        collapsed_metadata["release_tag"] = rollout.EXPECTED_RELEASE_TAG
        collapsed_metadata["packages"] = [asa_package]

        previous_binutils = copy.deepcopy(
            self.snapshot.formula_sidecars["binutils"]
        )
        self.assertIsNotNone(previous_binutils)
        previous_binutils["kandelo_abi"] = rollout.EXPECTED_ABI - 1
        current = self._load_snapshot_view(
            metadata=collapsed_metadata,
            formula_sidecars={"binutils": previous_binutils},
        )
        asa_bottles = asa_package["bottles"]
        historical_built_from = asa_bottles[0]["built_from"]
        historical_publisher_sha = historical_built_from["kandelo_commit"]
        current = dataclasses.replace(
            current,
            workflow_source=self.tap.show(
                historical_built_from["tap_commit"], rollout.WORKFLOW_PATH
            ),
        )

        # Model the ledger frozen before the aggregate metadata rolled over.
        # WHY: rotating the protected caller after the rollout does not rewrite
        # the provenance of bottles already finalized by the earlier producer.
        cutover_metadata = copy.deepcopy(collapsed_metadata)
        cutover_metadata["kandelo_abi"] = rollout.EXPECTED_ABI - 1
        cutover_metadata["packages"] = [
            copy.deepcopy(sidecar)
            for sidecar in self.snapshot.formula_sidecars.values()
            if sidecar is not None
        ]
        cutover = dataclasses.replace(
            self.snapshot,
            metadata=cutover_metadata,
            workflow_source=current.workflow_source,
        )
        state = rollout.initial_state(cutover, historical_publisher_sha)

        self.assertEqual(["asa"], [
            package["name"] for package in current.metadata["packages"]
        ])
        self.assertEqual(
            previous_binutils["version"],
            current.identities["binutils"].pkg_version,
        )
        rollout.validate_state(state, current, historical_publisher_sha)

        statuses = {
            status.name: status
            for status in rollout.calculate_statuses(
                self.tap,
                current,
                historical_publisher_sha,
                rollout.RunInventory(
                    count=0,
                    runs=(),
                    formulae={},
                    unknown_run_ids=(),
                ),
                {},
            )
        }
        self.assertEqual("finalized", statuses["asa"].state)
        self.assertEqual("ready", statuses["binutils"].state)

    def test_implicit_version_fails_closed_without_its_formula_sidecar(self):
        self.assertNotRegex(
            self.snapshot.formula_sources["binutils"],
            r"(?m)^\s{2}version\s+",
        )
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "Formula/binutils.rb needs an explicit version",
        ):
            self._load_snapshot_view(formula_sidecars={"binutils": None})

    def test_formula_sidecar_cannot_supply_another_packages_version(self):
        sidecar = copy.deepcopy(self.snapshot.formula_sidecars["binutils"])
        self.assertIsNotNone(sidecar)
        sidecar["name"] = "bc"
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "Kandelo/formula/binutils.json belongs to another Formula",
        ):
            self._load_snapshot_view(formula_sidecars={"binutils": sidecar})

    def test_frozen_catalog_rejects_current_sidecar_version_tampering(self):
        state = rollout.initial_state(self.snapshot, self.publisher_sha)
        sidecar = copy.deepcopy(self.snapshot.formula_sidecars["binutils"])
        self.assertIsNotNone(sidecar)
        sidecar["version"] = "999.0"
        current = self._load_snapshot_view(
            formula_sidecars={"binutils": sidecar}
        )

        with self.assertRaisesRegex(rollout.RolloutError, "catalog differs"):
            rollout.validate_state(state, current, self.publisher_sha)

    def test_frozen_catalog_rejects_ledger_or_current_source_tampering(self):
        state = rollout.initial_state(self.snapshot, self.publisher_sha)
        tampered_state = copy.deepcopy(state)
        tampered_state["catalog"]["binutils"]["version"] = "999.0"
        with self.assertRaisesRegex(rollout.RolloutError, "catalog differs"):
            rollout.validate_state(
                tampered_state,
                self.snapshot,
                self.publisher_sha,
            )

        source = self.snapshot.formula_sources["binutils"].replace(
            "class Binutils < Formula",
            "class Binutils < Formula\n  # Unreviewed recipe change.",
            1,
        )
        current = self._load_snapshot_view(
            formula_sources={"binutils": source}
        )
        with self.assertRaisesRegex(rollout.RolloutError, "catalog differs"):
            rollout.validate_state(state, current, self.publisher_sha)

    def test_dispatch_cannot_recreate_a_missing_ledger_after_cutover(self):
        self.assertEqual(
            rollout.EXPECTED_ABI,
            self.snapshot.metadata["kandelo_abi"],
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "missing-rollout.json"
            with (
                mock.patch.object(
                    self.tap, "main_without_fetch", return_value=self.head
                ),
                self.assertRaisesRegex(
                    rollout.RolloutError,
                    "cannot initialize a replacement rollout state after the ABI 42 cutover",
                ),
            ):
                rollout.dispatch_ready(
                    tap=self.tap,
                    github=FakeGitHub(),
                    expected_kandelo_sha=self.publisher_sha,
                    state_path=state_path,
                    no_fetch=True,
                    maximum=1,
                    timeout_seconds=1,
                    poll_seconds=0.001,
                )
            self.assertFalse(state_path.exists())

    def test_source_scan_captures_runtime_build_and_test_edges(self):
        dependencies = self.snapshot.dependencies
        self.assertEqual(frozenset(("dash",)), dependencies["erlang"])
        self.assertEqual(frozenset(("dash",)), dependencies["findutils"])
        self.assertEqual(
            frozenset(("dash", "zlib")),
            dependencies["python"],
            "Python includes the VFS-acceptance Dash edge",
        )
        self.assertEqual(
            frozenset(("openssl", "zlib")), dependencies["libcurl"]
        )
        self.assertEqual(
            frozenset(("coreutils", "dash", "diffutils", "grep", "less",
                       "libcurl", "openssl", "sed", "vim", "zlib")),
            dependencies["git"],
        )
        for formula, deps in dependencies.items():
            for dependency in deps:
                self.assertLess(
                    rollout.FORMULA_LEVEL[dependency],
                    rollout.FORMULA_LEVEL[formula],
                )

    def test_every_reserved_identity_has_expected_arches_and_positive_rebuild(self):
        for formula, identity in self.snapshot.identities.items():
            self.assertGreaterEqual(identity.bottle_rebuild, 1, formula)
            self.assertEqual(
                set(rollout.required_arches(formula)),
                set(identity.bottle_sha256),
                formula,
            )

    def test_workflow_must_pin_both_call_and_source_to_frozen_sha(self):
        expected = "a" * 40
        vfs_expression = (
            "${{ github.event.client_payload.require_vfs_acceptance || false }}"
        )
        source = f"""\
on:
  repository_dispatch:
    types: [publish-kandelo-bottles]
jobs:
  publish:
    uses: Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@{expected}
    with:
      kandelo-repository: Automattic/kandelo
      kandelo-ref: {expected}
      tap-repository: kandelo-dev/homebrew-tap-core
      tap-name: kandelo-dev/tap-core
      tap-ref: ${{{{ github.event.client_payload.tap_sha }}}}
      formulae: ${{{{ github.event.client_payload.formulae }}}}
      arches: ${{{{ github.event.client_payload.arches || 'wasm32' }}}}
      force: ${{{{ github.event.client_payload.force || false }}}}
      dry-run: false
      require-vfs-acceptance: {vfs_expression}
      prepublication-staging-tag: {rollout.PREPUBLICATION_STAGING_TAG}
      prepublication-staging-kandelo-sha: {rollout.PREPUBLICATION_GENERATION_SHA}
      defer-vfs-acceptance-until-postpublication: {vfs_expression}
"""
        snapshot = dataclasses.replace(self.snapshot, workflow_source=source)
        rollout.validate_workflow(FakeGitHub(), snapshot, expected)
        with self.assertRaisesRegex(
            rollout.RolloutError, "not frozen to the requested"
        ):
            rollout.validate_workflow(
                FakeGitHub(),
                dataclasses.replace(
                    snapshot,
                    workflow_source=source.replace(
                        f"kandelo-ref: {expected}", "kandelo-ref: main"
                    ),
                ),
                expected,
            )
        with self.assertRaisesRegex(
            rollout.RolloutError, "workflow force differs"
        ):
            rollout.validate_workflow(
                FakeGitHub(),
                dataclasses.replace(
                    snapshot,
                    workflow_source=source.replace(
                        "github.event.client_payload.force || false", "true"
                    ),
                ),
                expected,
            )
        with self.assertRaisesRegex(
            rollout.RolloutError, "prepublication-staging-kandelo-sha differs"
        ):
            rollout.validate_workflow(
                FakeGitHub(),
                dataclasses.replace(
                    snapshot,
                    workflow_source=source.replace(
                        rollout.PREPUBLICATION_GENERATION_SHA,
                        "b" * 40,
                    ),
                ),
                expected,
            )
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "defer-vfs-acceptance-until-postpublication differs",
        ):
            rollout.validate_workflow(
                FakeGitHub(),
                dataclasses.replace(
                    snapshot,
                    workflow_source=source.replace(
                        "defer-vfs-acceptance-until-postpublication: "
                        f"{vfs_expression}",
                        "defer-vfs-acceptance-until-postpublication: true",
                    ),
                ),
                expected,
            )

    def _finalized_snapshot(self, formula: str):
        identity = self.snapshot.identities[formula]
        source = self.snapshot.formula_sources[formula]
        formula_sha = hashlib.sha256(source.encode()).hexdigest()
        bottles = []
        for arch in identity.arches:
            digest = identity.bottle_sha256[arch]
            bottles.append(
                {
                    "arch": arch,
                    "bottle_tag": f"{arch}_kandelo",
                    "built_from": {
                        "formula_sha256": formula_sha,
                        "kandelo_commit": "a" * 40,
                        "kandelo_repository": rollout.KANDELO_REPOSITORY,
                        "tap_commit": self.head,
                        "tap_repository": rollout.REPOSITORY,
                    },
                    "kandelo_abi": 42,
                    "sha256": digest,
                    "status": "success",
                    "url": (
                        f"{rollout.BOTTLE_ROOT}/{formula}/blobs/sha256:{digest}"
                    ),
                }
            )
        package = {
            "name": formula,
            "version": identity.pkg_version,
            "formula_revision": identity.formula_revision,
            "bottle_rebuild": identity.bottle_rebuild,
            "bottles": copy.deepcopy(bottles),
        }
        sidecar = {
            **copy.deepcopy(package),
            "kandelo_abi": 42,
        }
        metadata = {
            "kandelo_abi": 42,
            "release_tag": "bottles-abi-v42",
            "packages": [package],
        }
        sidecars = dict(self.snapshot.formula_sidecars)
        sidecars[formula] = sidecar
        return dataclasses.replace(
            self.snapshot,
            metadata=metadata,
            formula_sidecars=sidecars,
        )

    def test_finalization_requires_matching_current_main_sidecars_and_provenance(self):
        snapshot = self._finalized_snapshot("zlib")
        self.assertEqual(
            (),
            rollout.finalization_reasons(
                self.tap, snapshot, "zlib", ("wasm32", "wasm64"), "a" * 40
            ),
        )

        wrong = copy.deepcopy(snapshot.metadata)
        wrong["packages"][0]["bottle_rebuild"] += 1
        reasons = rollout.finalization_reasons(
            self.tap,
            dataclasses.replace(snapshot, metadata=wrong),
            "zlib",
            ("wasm32", "wasm64"),
            "a" * 40,
        )
        self.assertTrue(any("bottle_rebuild" in reason for reason in reasons))

    def test_explicit_revision_finalizes_and_unblocks_dependents(self):
        python = self._finalized_snapshot("python")
        self.assertEqual("3.13.3_1", python.identities["python"].pkg_version)
        self.assertEqual(
            (),
            rollout.finalization_reasons(
                self.tap, python, "python", ("wasm32",), "a" * 40
            ),
        )

        libcxx = self._finalized_snapshot("libcxx")
        statuses = {
            status.name: status
            for status in rollout.calculate_statuses(
                self.tap,
                libcxx,
                "a" * 40,
                rollout.RunInventory(
                    count=0,
                    runs=(),
                    formulae={},
                    unknown_run_ids=(),
                ),
                {},
            )
        }
        self.assertEqual("finalized", statuses["libcxx"].state)
        self.assertEqual(
            "ready",
            statuses["dinit"].state,
            "a finalized revised dependency must not stall the next wave",
        )

    def test_finalization_rejects_wrong_kandelo_sha_and_missing_arch(self):
        snapshot = self._finalized_snapshot("zlib")
        reasons = rollout.finalization_reasons(
            self.tap, snapshot, "zlib", ("wasm32", "wasm64"), "b" * 40
        )
        self.assertTrue(any("another Kandelo SHA" in reason for reason in reasons))

        metadata = copy.deepcopy(snapshot.metadata)
        metadata["packages"][0]["bottles"] = [
            bottle
            for bottle in metadata["packages"][0]["bottles"]
            if bottle["arch"] == "wasm32"
        ]
        reasons = rollout.finalization_reasons(
            self.tap,
            dataclasses.replace(snapshot, metadata=metadata),
            "zlib",
            ("wasm32", "wasm64"),
            "a" * 40,
        )
        self.assertIn(
            "wasm64 is missing from aggregate or sidecar",
            reasons,
        )

    def test_finalization_rejects_sidecar_provenance_different_from_aggregate(self):
        snapshot = self._finalized_snapshot("zlib")
        sidecars = copy.deepcopy(snapshot.formula_sidecars)
        sidecars["zlib"]["bottles"][0]["built_from"]["formula_sha256"] = "f" * 64
        reasons = rollout.finalization_reasons(
            self.tap,
            dataclasses.replace(snapshot, formula_sidecars=sidecars),
            "zlib",
            ("wasm32", "wasm64"),
            "a" * 40,
        )
        self.assertIn(
            "aggregate and sidecar wasm32 bottle records differ",
            reasons,
        )

    def test_finalization_validates_archived_formula_digest_as_a_receipt(self):
        snapshot = self._finalized_snapshot("zlib")
        metadata = copy.deepcopy(snapshot.metadata)
        sidecars = copy.deepcopy(snapshot.formula_sidecars)
        metadata["packages"][0]["bottles"][0]["built_from"][
            "formula_sha256"
        ] = "not-a-sha"
        sidecars["zlib"]["bottles"][0]["built_from"][
            "formula_sha256"
        ] = "not-a-sha"

        reasons = rollout.finalization_reasons(
            self.tap,
            dataclasses.replace(
                snapshot,
                metadata=metadata,
                formula_sidecars=sidecars,
            ),
            "zlib",
            ("wasm32",),
            "a" * 40,
        )

        self.assertIn("wasm32 archived Formula digest is invalid", reasons)

    def test_finalization_rejects_a_different_source_recipe_with_same_identity(self):
        snapshot = self._finalized_snapshot("zlib")
        source = snapshot.formula_sources["zlib"].replace(
            "class Zlib < Formula",
            "class Zlib < Formula\n  # Semantically different build input.",
            1,
        )
        source_digest = hashlib.sha256(source.encode()).hexdigest()
        metadata = copy.deepcopy(snapshot.metadata)
        sidecars = copy.deepcopy(snapshot.formula_sidecars)
        for bottle in metadata["packages"][0]["bottles"]:
            bottle["built_from"]["formula_sha256"] = source_digest
        for bottle in sidecars["zlib"]["bottles"]:
            bottle["built_from"]["formula_sha256"] = source_digest

        class SourceTap:
            def is_ancestor(self, ancestor, descendant):
                return ancestor == self_head and descendant == self_head

            def show(self, revision, path):
                self.assert_revision(revision)
                if path == "Formula/zlib.rb":
                    return source
                if path == rollout.WORKFLOW_PATH:
                    return snapshot.workflow_source
                raise AssertionError(path)

            def tree_oid(self, revision, path):
                self.assert_revision(revision)
                self.assert_path(path)
                return snapshot.formula_support_tree

            @staticmethod
            def assert_revision(revision):
                if revision != self_head:
                    raise AssertionError(revision)

            @staticmethod
            def assert_path(path):
                if path != "Kandelo/formula_support":
                    raise AssertionError(path)

        self_head = self.head
        reasons = rollout.finalization_reasons(
            SourceTap(),
            dataclasses.replace(
                snapshot,
                metadata=metadata,
                formula_sidecars=sidecars,
            ),
            "zlib",
            ("wasm32", "wasm64"),
            "a" * 40,
        )
        self.assertIn("wasm32 source Formula recipe differs", reasons)
        self.assertIn("wasm64 source Formula recipe differs", reasons)

    def test_rollout_state_freezes_recipe_support_and_wave_contracts(self):
        state = rollout.initial_state(self.snapshot, "a" * 40)
        rollout.validate_state(state, self.snapshot, "a" * 40)

        sources = dict(self.snapshot.formula_sources)
        sources["asa"] = sources["asa"].replace(
            'desc "', 'desc "changed ', 1
        )
        with self.assertRaisesRegex(rollout.RolloutError, "catalog differs"):
            rollout.validate_state(
                state,
                dataclasses.replace(self.snapshot, formula_sources=sources),
                "a" * 40,
            )

        with self.assertRaisesRegex(
            rollout.RolloutError, "formula_support_tree differs"
        ):
            rollout.validate_state(
                state,
                dataclasses.replace(self.snapshot, formula_support_tree="f" * 40),
                "a" * 40,
            )

        changed_waves = copy.deepcopy(state)
        changed_waves["waves"][0].reverse()
        with self.assertRaisesRegex(rollout.RolloutError, "waves differs"):
            rollout.validate_state(changed_waves, self.snapshot, "a" * 40)

    def test_rollout_state_allows_only_finalizer_checksum_formula_edits(self):
        state = rollout.initial_state(self.snapshot, "a" * 40)
        sources = dict(self.snapshot.formula_sources)
        old_digest = self.snapshot.identities["asa"].bottle_sha256["wasm32"]
        sources["asa"] = sources["asa"].replace(old_digest, "f" * 64, 1)
        rollout.validate_state(
            state,
            dataclasses.replace(self.snapshot, formula_sources=sources),
            "a" * 40,
        )

    def test_rollout_state_rejects_a_malformed_dispatch_ledger(self):
        state = rollout.initial_state(self.snapshot, "a" * 40)
        state["dispatches"].append(
            {
                "formula": "asa",
                "run_id": 123,
            }
        )
        with self.assertRaisesRegex(
            rollout.RolloutError, "malformed dispatch"
        ):
            rollout.validate_state(state, self.snapshot, "a" * 40)

    def test_active_inventory_counts_every_production_wait_state(self):
        github = FakeGitHub()
        for index, status in enumerate(rollout.ACTIVE_STATUSES, start=1):
            run_id = 100 + index
            github.by_status[status] = {
                "total_count": 1,
                "workflow_runs": [{"id": run_id, "status": status}],
            }
            github.jobs_by_run[run_id] = (
                {"name": f"publish / build-and-test (asa, wasm32)"},
            )
        inventory = rollout.active_inventory(github)
        self.assertEqual(len(rollout.ACTIVE_STATUSES), inventory.count)
        self.assertEqual((), inventory.unknown_run_ids)

    def test_dual_arch_dependencies_follow_the_consumer_architecture(self):
        self.assertEqual("wasm32", rollout.dependency_arch("zlib", "wasm32"))
        self.assertEqual("wasm64", rollout.dependency_arch("zlib", "wasm64"))
        self.assertEqual(
            "wasm32",
            rollout.dependency_arch("dash", "wasm64"),
            "single-architecture dependencies cannot be requested as wasm64",
        )

    def test_unknown_active_formula_is_reported_conservatively(self):
        github = FakeGitHub()
        github.by_status["queued"] = {
            "total_count": 1,
            "workflow_runs": [{"id": 123, "status": "queued"}],
        }
        github.jobs_by_run[123] = ({"name": "publish / plan"},)
        inventory = rollout.active_inventory(github)
        self.assertEqual((123,), inventory.unknown_run_ids)

    def test_active_inventory_rejects_count_without_complete_run_details(self):
        github = FakeGitHub()
        github.by_status["queued"] = {
            "total_count": 1,
            "workflow_runs": [],
        }
        with self.assertRaisesRegex(
            rollout.RolloutError, "reported 1 active runs but returned 0"
        ):
            rollout.active_inventory(github)

    def test_run_correlation_rejects_an_incomplete_job_page(self):
        github = rollout.GitHub()
        with (
            mock.patch.object(
                github,
                "api_json",
                return_value={
                    "total_count": 2,
                    "jobs": [{"name": "publish / plan"}],
                },
            ),
            self.assertRaisesRegex(
                rollout.RolloutError, "incomplete job matrix"
            ),
        ):
            github.jobs(123)

    def test_recorded_active_run_cannot_disappear_during_status_transitions(self):
        github = FakeGitHub()
        github.runs_by_id[123] = {
            "id": 123,
            "status": "in_progress",
        }
        inventory = rollout.reconcile_recorded_activity(
            github,
            rollout.RunInventory(
                count=7,
                runs=(),
                formulae={},
                unknown_run_ids=(),
            ),
            {
                "dispatches": [
                    {"formula": "asa", "run_id": 123},
                ]
            },
        )
        self.assertEqual(8, inventory.count)
        self.assertEqual(frozenset(("asa",)), inventory.formulae[123])

    def test_successful_recorded_run_waits_for_finalizer_visibility(self):
        github = FakeGitHub()
        github.runs_by_id[123] = {
            "id": 123,
            "status": "completed",
            "conclusion": "success",
        }
        blocks = rollout.history_blocks_from_state(
            github,
            {
                "dispatches": [
                    {"formula": "asa", "run_id": 123},
                ]
            },
            {"asa": False},
        )
        self.assertEqual("waiting-finalization", blocks["asa"][0])

    def test_only_python_dispatch_requests_the_deferred_vfs_acceptance(self):
        calls = []

        def capture(argv, **kwargs):
            calls.append((argv, kwargs))
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(rollout, "_run", side_effect=capture):
            for formula in rollout.FORMULA_ORDER:
                rollout.GitHub().dispatch(
                    formula,
                    rollout.required_arches(formula),
                    self.head,
                )
        payloads = {
            payload["client_payload"]["formulae"]: payload
            for _, kwargs in calls
            for payload in (json.loads(kwargs["input_text"]),)
        }
        self.assertEqual(set(rollout.FORMULA_ORDER), set(payloads))
        self.assertEqual(
            ["python"],
            sorted(
                formula
                for formula, payload in payloads.items()
                if "require_vfs_acceptance" in payload["client_payload"]
            ),
        )
        python_payload = payloads["python"]
        zlib_payload = payloads["zlib"]
        self.assertEqual("python", python_payload["client_payload"]["formulae"])
        self.assertIs(True, python_payload["client_payload"]["require_vfs_acceptance"])
        self.assertEqual(
            {self.head},
            {
                payload["client_payload"]["tap_sha"]
                for payload in payloads.values()
            },
        )
        self.assertEqual(
            "wasm32,wasm64", zlib_payload["client_payload"]["arches"]
        )
        self.assertNotIn(
            "require_vfs_acceptance", zlib_payload["client_payload"]
        )
        self.assertNotIn("rerun", json.dumps(calls).lower())

    def test_dispatch_rejects_a_mutable_or_malformed_tap_ref(self):
        with self.assertRaisesRegex(
            rollout.RolloutError, "exact lowercase tap commit SHA"
        ):
            rollout.GitHub().dispatch("zlib", ("wasm32",), "main")

    def test_absent_dispatch_run_id_fails_closed(self):
        github = FakeGitHub()
        with self.assertRaisesRegex(
            rollout.RolloutError, "no unambiguous run ID appeared"
        ):
            rollout.acknowledge_dispatch(
                github,
                before_ids=frozenset(),
                formula="asa",
                arches=("wasm32",),
                tap_sha=self.head,
                timeout_seconds=0,
                poll_seconds=0.001,
            )

    def test_dispatch_acknowledgement_requires_the_exact_arch_matrix(self):
        github = FakeGitHub()
        github.by_status[None] = {
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": 124,
                    "event": "repository_dispatch",
                    "head_sha": self.head,
                }
            ],
        }
        github.jobs_by_run[124] = (
            {"name": "publish / build-and-test (asa, wasm32)"},
            {"name": "publish / build-and-test (asa, wasm64)"},
        )
        with (
            mock.patch.object(rollout.time, "monotonic", side_effect=(0, 0, 2)),
            mock.patch.object(rollout.time, "sleep"),
            self.assertRaisesRegex(
                rollout.RolloutError, "no unambiguous run ID appeared"
            ),
        ):
            rollout.acknowledge_dispatch(
                github,
                before_ids=frozenset(),
                formula="asa",
                arches=("wasm32",),
                tap_sha=self.head,
                timeout_seconds=1,
                poll_seconds=0.001,
            )

    def test_recovery_atomically_records_one_late_exact_match(self):
        state = self._submitted_state(before_run_ids=(100,))
        github = self._candidate_github(
            self._run(100, self.head),
            self._run(123, self.head),
            jobs_by_run={
                123: (
                    {"name": "publish / plan"},
                    *self._matrix_jobs("asa", "wasm32"),
                    {"name": "publish / upload-bottle (asa, wasm32)"},
                ),
            },
        )

        result, recovered = self._recover(github, state)

        self.assertEqual(("asa", 123), result)
        self.assertIsNone(recovered["unresolved_dispatch"])
        self.assertEqual(
            {
                "formula": "asa",
                "arches": ["wasm32"],
                "tap_sha": self.head,
                "run_id": 123,
                "submitted_at": "2026-07-24T06:46:35Z",
            },
            recovered["dispatches"][-1],
        )

    def test_recovery_accepts_an_exact_run_that_already_completed(self):
        state = self._submitted_state()
        github = self._candidate_github(
            self._run(
                123,
                self.head,
                status="completed",
                conclusion="failure",
            ),
            jobs_by_run={123: self._matrix_jobs("asa", "wasm32")},
        )

        result, recovered = self._recover(github, state)

        self.assertEqual(("asa", 123), result)
        self.assertEqual(123, recovered["dispatches"][-1]["run_id"])

    def test_recovery_with_no_new_run_fails_without_rewriting_state(self):
        state = self._submitted_state(before_run_ids=(100,))
        github = self._candidate_github(
            self._run(100, self.head),
            jobs_by_run={100: self._matrix_jobs("asa", "wasm32")},
        )
        self._assert_recovery_fails_unchanged(
            "recovery found 0 exact new runs",
            github,
            state,
        )

    def test_recovery_with_multiple_exact_runs_fails_without_rewriting_state(self):
        state = self._submitted_state()
        github = self._candidate_github(
            self._run(123, self.head),
            self._run(124, self.head),
            jobs_by_run={
                123: self._matrix_jobs("asa", "wasm32"),
                124: self._matrix_jobs("asa", "wasm32"),
            },
        )
        self._assert_recovery_fails_unchanged(
            "recovery found 2 exact new runs",
            github,
            state,
        )

    def test_recovery_rejects_a_run_from_the_wrong_tap_head(self):
        state = self._submitted_state()
        github = self._candidate_github(
            self._run(123, "f" * 40),
            jobs_by_run={123: self._matrix_jobs("asa", "wasm32")},
        )
        self._assert_recovery_fails_unchanged(
            "recovery found 0 exact new runs",
            github,
            state,
        )

    def test_recovery_rejects_the_wrong_formula_matrix(self):
        state = self._submitted_state()
        github = self._candidate_github(
            self._run(123, self.head),
            jobs_by_run={123: self._matrix_jobs("bc", "wasm32")},
        )
        self._assert_recovery_fails_unchanged(
            "recovery found 0 exact new runs",
            github,
            state,
        )

    def test_recovery_rejects_the_wrong_architecture_matrix(self):
        state = self._submitted_state()
        github = self._candidate_github(
            self._run(123, self.head),
            jobs_by_run={123: self._matrix_jobs("asa", "wasm64")},
        )
        self._assert_recovery_fails_unchanged(
            "recovery found 0 exact new runs",
            github,
            state,
        )

    def test_recovery_fails_closed_when_the_job_page_is_incomplete(self):
        state = self._submitted_state()
        github = self._candidate_github(self._run(123, self.head))
        github.jobs = mock.Mock(
            side_effect=rollout.RolloutError(
                "GitHub returned an incomplete job matrix for run 123"
            )
        )
        self._assert_recovery_fails_unchanged(
            "incomplete job matrix",
            github,
            state,
        )

    def test_recovery_rejects_a_truncated_workflow_run_page(self):
        state = self._submitted_state(before_run_ids=(100,))
        github = self._candidate_github(
            self._run(123, self.head),
            jobs_by_run={123: self._matrix_jobs("asa", "wasm32")},
            total_count=2,
        )
        self._assert_recovery_fails_unchanged(
            "incomplete workflow run page",
            github,
            state,
        )

    def test_recovery_rejects_one_visible_match_after_boundary_loss(self):
        state = self._submitted_state(before_run_ids=(100,))
        runs = tuple(
            self._run(run_id, self.head)
            for run_id in range(200, 300)
        )
        github = self._candidate_github(
            *runs,
            jobs_by_run={299: self._matrix_jobs("asa", "wasm32")},
            total_count=200,
        )
        self._assert_recovery_fails_unchanged(
            "correlation window exceeded the newest 100",
            github,
            state,
        )

    def test_recovery_requires_complete_history_for_an_empty_boundary(self):
        state = self._submitted_state(before_run_ids=())
        runs = tuple(
            self._run(run_id, self.head)
            for run_id in range(200, 300)
        )
        github = self._candidate_github(
            *runs,
            jobs_by_run={299: self._matrix_jobs("asa", "wasm32")},
            total_count=101,
        )
        self._assert_recovery_fails_unchanged(
            "correlation window exceeded the complete workflow history",
            github,
            state,
        )

    def test_recovery_rejects_an_intent_not_known_to_be_submitted(self):
        state = self._submitted_state()
        state["unresolved_dispatch"]["status"] = "intent-recorded"
        state["unresolved_dispatch"].pop("submitted_at")
        github = self._candidate_github(
            self._run(123, self.head),
            jobs_by_run={123: self._matrix_jobs("asa", "wasm32")},
        )
        self._assert_recovery_fails_unchanged(
            "not an exact submitted intent",
            github,
            state,
        )

    def test_abandonment_preserves_a_cancelled_never_started_request(self):
        state = self._submitted_state(before_run_ids=(100,))
        github = self._candidate_github(
            self._run(100, self.head),
            self._run(
                123,
                self.head,
                status="completed",
                conclusion="cancelled",
            ),
            jobs_by_run={
                100: self._matrix_jobs("asa", "wasm32"),
                123: (
                    *self._matrix_jobs("asa", "wasm32"),
                    *self._never_started_write_jobs(),
                ),
            },
        )

        with mock.patch.object(
            rollout, "_utc_now", return_value="2026-07-24T17:40:00Z"
        ):
            result, abandoned = self._abandon(github, state)

        self.assertEqual(("asa", 123), result)
        self.assertIsNone(abandoned["unresolved_dispatch"])
        self.assertEqual([], abandoned["dispatches"])
        self.assertEqual(
            [
                {
                    "formula": "asa",
                    "arches": ["wasm32"],
                    "intent_tap_sha": self.head,
                    "run_tap_sha": self.head,
                    "run_id": 123,
                    "submitted_at": "2026-07-24T06:46:35Z",
                    "abandoned_at": "2026-07-24T17:40:00Z",
                    "reason": rollout.ABANDONED_DISPATCH_REASON,
                }
            ],
            abandoned["abandoned_dispatches"],
        )
        rollout.validate_state(abandoned, self.snapshot, self.publisher_sha)

    def test_abandonment_rejects_any_external_write_job_step(self):
        state = self._submitted_state()
        write_jobs = list(self._never_started_write_jobs())
        write_jobs[0] = {
            **write_jobs[0],
            "steps": [
                {
                    "name": "Authenticate to GHCR",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        }
        github = self._candidate_github(
            self._run(
                123,
                self.head,
                status="completed",
                conclusion="cancelled",
            ),
            jobs_by_run={
                123: (
                    *self._matrix_jobs("asa", "wasm32"),
                    *write_jobs,
                )
            },
        )

        self._assert_abandon_fails_unchanged(
            "may have started; refusing abandonment",
            github,
            state,
        )

    def test_abandonment_requires_the_sole_explicit_post_intent_run(self):
        state = self._submitted_state()
        jobs = (
            *self._matrix_jobs("asa", "wasm32"),
            *self._never_started_write_jobs(),
        )
        github = self._candidate_github(
            self._run(
                123,
                self.head,
                status="completed",
                conclusion="cancelled",
            ),
            self._run(
                124,
                self.head,
                status="completed",
                conclusion="cancelled",
            ),
            jobs_by_run={123: jobs, 124: jobs},
        )

        self._assert_abandon_fails_unchanged(
            "explicit sole post-intent Formula run",
            github,
            state,
        )

    def test_abandonment_rejects_a_non_cancelled_run(self):
        state = self._submitted_state()
        github = self._candidate_github(
            self._run(
                123,
                self.head,
                status="completed",
                conclusion="failure",
            ),
            jobs_by_run={
                123: (
                    *self._matrix_jobs("asa", "wasm32"),
                    *self._never_started_write_jobs(),
                )
            },
        )

        self._assert_abandon_fails_unchanged(
            "not a completed cancelled publication",
            github,
            state,
        )

    def test_abandonment_requires_every_external_write_job(self):
        state = self._submitted_state()
        write_jobs = tuple(
            job
            for job in self._never_started_write_jobs()
            if not job["name"].endswith("publish-vfs-release")
        )
        github = self._candidate_github(
            self._run(
                123,
                self.head,
                status="completed",
                conclusion="cancelled",
            ),
            jobs_by_run={
                123: (
                    *self._matrix_jobs("asa", "wasm32"),
                    *write_jobs,
                )
            },
        )

        self._assert_abandon_fails_unchanged(
            "lacks expected external-write jobs: publish-vfs-release",
            github,
            state,
        )

    def test_state_write_is_private_and_preserves_unresolved_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "rollout.json"
            state = {
                "schema": 1,
                "unresolved_dispatch": {"formula": "asa"},
            }
            rollout.write_state(path, state)
            self.assertEqual(state, rollout.read_state(path))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_state_lock_rejects_a_second_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "rollout.json"
            with rollout.state_lock(path):
                with self.assertRaisesRegex(
                    rollout.RolloutError, "another rollout controller"
                ):
                    with rollout.state_lock(path):
                        self.fail("second state lock should not be acquired")

    def test_cli_requires_explicit_state_file_for_dispatch(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            rollout.parse_args(
                (
                    "--tap-root",
                    str(self.root),
                    "--expected-kandelo-sha",
                    "a" * 40,
                    "--dispatch",
                )
            )
        self.assertIn("--state-file is required with --dispatch", stderr.getvalue())

    def test_cli_requires_state_for_recovery_and_preserves_timeout_override(self):
        base = (
            "--tap-root",
            str(self.root),
            "--expected-kandelo-sha",
            "a" * 40,
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            rollout.parse_args((*base, "--recover-dispatch"))
        self.assertIn("--recover-dispatch", stderr.getvalue())

        defaults = rollout.parse_args(base)
        override = rollout.parse_args((*base, "--ack-timeout", "17"))
        self.assertEqual(600, defaults.ack_timeout)
        self.assertEqual(17, override.ack_timeout)

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rollout.parse_args((*base, "--abandon-dispatch-run", "123"))
        abandon = rollout.parse_args(
            (
                *base,
                "--state-file",
                "/tmp/rollout-state.json",
                "--abandon-dispatch-run",
                "123",
            )
        )
        self.assertEqual(123, abandon.abandon_dispatch_run)


if __name__ == "__main__":
    unittest.main()
