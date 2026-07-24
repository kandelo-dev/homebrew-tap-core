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
import urllib.error
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
        self.logs_by_job: dict[int, str] = {}
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

    def job_log(self, job_id):
        return self.logs_by_job[job_id]

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


class FakeRegistry:
    def __init__(self, evidence: rollout.RegistryManifestEvidence) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, str]] = []

    def manifest(
        self, formula: str, reference: str
    ) -> rollout.RegistryManifestEvidence:
        self.calls.append((formula, reference))
        return self.evidence


class FakeHttpResponse:
    def __init__(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str] | None = None,
        status: int = 200,
    ) -> None:
        self.url = url
        self.body = body
        self.headers = headers or {}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


class RolloutControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = SCRIPT.parent.parent
        cls.tap = rollout.GitTap(cls.root)
        cls.head = cls.tap.git("rev-parse", "HEAD").stdout.strip()
        cls.snapshot = rollout.load_snapshot(cls.tap, cls.head)
        match = re.search(
            r"^\s+kandelo-ref:\s+([0-9a-f]{40})\s*$",
            cls.snapshot.workflow_source,
            flags=re.MULTILINE,
        )
        assert match is not None
        cls.consumer_sha = match.group(1)
        cls.legacy_workflow_source = cls.tap.show(
            "71b3004a43be103b315d8d298a89799c3895e98a",
            rollout.WORKFLOW_PATH,
        )
        cls.transitional_workflow_source = cls.tap.show(
            "8b0d41714a0ecce7ca2deb38f5aeecccf9add557",
            rollout.WORKFLOW_PATH,
        )
        assert (
            hashlib.sha256(cls.legacy_workflow_source.encode()).hexdigest()
            in rollout.APPROVED_PUBLICATION_WORKFLOWS
        )
        assert (
            hashlib.sha256(cls.transitional_workflow_source.encode()).hexdigest()
            in rollout.APPROVED_NO_WRITE_ONLY_WORKFLOWS
        )
        assert (
            rollout.workflow_sha256(cls.snapshot)
            in rollout.APPROVED_PUBLICATION_WORKFLOWS
        )

    def _submitted_state(
        self,
        *,
        formula: str = "asa",
        arches: tuple[str, ...] = ("wasm32",),
        before_run_ids: tuple[int, ...] = (),
    ) -> dict:
        state = rollout.initial_state(self.snapshot, self.consumer_sha)
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
                    expected_kandelo_sha=self.consumer_sha,
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
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    run_id=run_id,
                    no_fetch=True,
                )
            abandoned = rollout.read_state(state_path)
            assert abandoned is not None
            self.assertEqual(0o600, state_path.stat().st_mode & 0o777)
            return result, abandoned

    @staticmethod
    def _snapshot_with_formula_source(
        snapshot,
        formula: str,
        source: str,
        *,
        sha: str,
    ):
        sources = dict(snapshot.formula_sources)
        sources[formula] = source
        identities = dict(snapshot.identities)
        identities[formula] = rollout.parse_formula_identity(
            formula,
            source,
            snapshot.formula_sidecars[formula],
        )
        dependencies = dict(snapshot.dependencies)
        dependencies[formula] = rollout.same_tap_dependencies(formula, source)
        return dataclasses.replace(
            snapshot,
            sha=sha,
            formula_sources=sources,
            identities=identities,
            dependencies=dependencies,
        )

    @staticmethod
    def _failed_state(snapshot, formula: str, *, run_id: int = 123) -> dict:
        state = rollout.initial_state(snapshot, RolloutControllerTests.consumer_sha)
        state["dispatches"].append(
            {
                "formula": formula,
                "arches": list(rollout.required_arches(formula)),
                "tap_sha": snapshot.sha,
                "run_id": run_id,
                "submitted_at": "2026-07-24T16:00:00Z",
            }
        )
        return state

    @staticmethod
    def _skipped_credential_jobs(
        formula: str, *arches: str
    ) -> tuple[dict, ...]:
        jobs: list[dict] = []
        next_job_id = 1000
        for arch in arches:
            jobs.append(
                {
                    "id": next_job_id,
                    "name": f"publish / upload-bottle ({formula}, {arch})",
                    "status": "completed",
                    "conclusion": "failure",
                    "steps": [
                        {
                            "name": rollout.CREDENTIAL_WRITE_STEPS["upload-bottle"],
                            "status": "completed",
                            "conclusion": "skipped",
                        }
                    ],
                }
            )
            next_job_id += 1
        for stage in (
            "publish-bottle-index",
            "finalize-tap",
            "publish-vfs-release",
        ):
            if stage == "finalize-tap":
                jobs.append(
                    {
                        "id": next_job_id,
                        "name": f"publish / {stage}",
                        "status": "completed",
                        "conclusion": "failure",
                        "steps": [
                            {
                                "name": rollout.CREDENTIAL_WRITE_STEPS[stage],
                                "status": "completed",
                                "conclusion": "skipped",
                            }
                        ],
                    }
                )
            else:
                jobs.append(
                    {
                        "id": next_job_id,
                        "name": f"publish / {stage}",
                        "status": "completed",
                        "conclusion": "skipped",
                        "steps": [],
                    }
                )
            next_job_id += 1
        return tuple(jobs)

    def _recover_failed(
        self,
        *,
        github: FakeGitHub,
        registry: FakeRegistry,
        state: dict,
        source_snapshot,
        current_snapshot,
        run_id: int = 123,
        run_ids: tuple[int, ...] | None = None,
        adopt_failed_runs: tuple[tuple[str, int], ...] = (),
        additional_source_snapshots: tuple[rollout.TapSnapshot, ...] = (),
    ) -> tuple[object, dict]:
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)

            snapshots = {
                source_snapshot.sha: source_snapshot,
                current_snapshot.sha: current_snapshot,
                **{
                    snapshot.sha: snapshot
                    for snapshot in additional_source_snapshots
                },
            }

            def load_snapshot(_tap, sha):
                try:
                    return snapshots[sha]
                except KeyError as error:
                    raise AssertionError(f"unexpected snapshot {sha}") from error

            with (
                mock.patch.object(
                    self.tap,
                    "main_without_fetch",
                    return_value=current_snapshot.sha,
                ),
                mock.patch.object(self.tap, "is_ancestor", return_value=True),
                mock.patch.object(
                    rollout, "load_snapshot", side_effect=load_snapshot
                ),
                mock.patch.object(
                    rollout, "_utc_now", return_value="2026-07-24T20:00:00Z"
                ),
            ):
                if run_ids is None:
                    result = rollout.recover_failed_dispatch(
                        tap=self.tap,
                        github=github,
                        registry=registry,
                        expected_kandelo_sha=self.consumer_sha,
                        state_path=state_path,
                        run_id=run_id,
                        no_fetch=True,
                    )
                else:
                    result = rollout.recover_failed_dispatches(
                        tap=self.tap,
                        github=github,
                        registry=registry,
                        expected_kandelo_sha=self.consumer_sha,
                        state_path=state_path,
                        run_ids=run_ids,
                        adopt_failed_runs=adopt_failed_runs,
                        no_fetch=True,
                    )
            recovered = rollout.read_state(state_path)
            assert recovered is not None
            self.assertEqual(0o600, state_path.stat().st_mode & 0o777)
            return result, recovered

    def _assert_failed_recovery_unchanged(
        self,
        pattern: str,
        *,
        github: FakeGitHub,
        registry: FakeRegistry,
        state: dict,
        source_snapshot,
        current_snapshot,
        run_id: int = 123,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)
            original = state_path.read_bytes()

            def load_snapshot(_tap, sha):
                if sha == source_snapshot.sha:
                    return source_snapshot
                if sha == current_snapshot.sha:
                    return current_snapshot
                raise AssertionError(f"unexpected snapshot {sha}")

            with (
                mock.patch.object(
                    self.tap,
                    "main_without_fetch",
                    return_value=current_snapshot.sha,
                ),
                mock.patch.object(self.tap, "is_ancestor", return_value=True),
                mock.patch.object(
                    rollout, "load_snapshot", side_effect=load_snapshot
                ),
                self.assertRaisesRegex(rollout.RolloutError, pattern),
            ):
                rollout.recover_failed_dispatch(
                    tap=self.tap,
                    github=github,
                    registry=registry,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    run_id=run_id,
                    no_fetch=True,
                )
            self.assertEqual(original, state_path.read_bytes())

    def _assert_failed_batch_recovery_unchanged(
        self,
        pattern: str,
        *,
        github: FakeGitHub,
        registry: FakeRegistry,
        state: dict,
        current_snapshot: rollout.TapSnapshot,
        source_snapshots: tuple[rollout.TapSnapshot, ...],
        run_ids: tuple[int, ...],
        adopt_failed_runs: tuple[tuple[str, int], ...] = (),
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)
            original = state_path.read_bytes()
            snapshots = {
                snapshot.sha: snapshot
                for snapshot in (*source_snapshots, current_snapshot)
            }
            with (
                mock.patch.object(
                    self.tap,
                    "main_without_fetch",
                    return_value=current_snapshot.sha,
                ),
                mock.patch.object(self.tap, "is_ancestor", return_value=True),
                mock.patch.object(
                    rollout,
                    "load_snapshot",
                    side_effect=lambda _tap, sha: snapshots[sha],
                ),
                self.assertRaisesRegex(rollout.RolloutError, pattern),
            ):
                rollout.recover_failed_dispatches(
                    tap=self.tap,
                    github=github,
                    registry=registry,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    run_ids=run_ids,
                    adopt_failed_runs=adopt_failed_runs,
                    no_fetch=True,
                )
            self.assertEqual(original, state_path.read_bytes())
            self.assertEqual([], registry.calls)

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
                    expected_kandelo_sha=self.consumer_sha,
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
                    expected_kandelo_sha=self.consumer_sha,
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
        created_at: str = "2026-07-24T21:33:30Z",
        workflow_id: int = rollout.WORKFLOW_ID,
        run_attempt: int = 1,
    ) -> dict:
        return {
            "id": run_id,
            "event": event,
            "head_sha": head_sha,
            "status": status,
            "conclusion": conclusion,
            "created_at": created_at,
            "workflow_id": workflow_id,
            "run_attempt": run_attempt,
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
    def _pre_matrix_jobs(plan_id: int = 900) -> tuple[dict, ...]:
        names = (
            "publish / build-and-test",
            "publish / upload-bottle",
            "publish / publish-bottle-index",
            "publish / verify-bottle",
            "publish / finalize-tap",
            "publish / publish-vfs-release",
        )
        return (
            {
                "id": plan_id,
                "name": "publish / plan",
                "status": "completed",
                "conclusion": "failure",
                "steps": [
                    {
                        "name": "Freeze exact prepublication generation",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ],
            },
            *(
                {
                    "id": plan_id + offset,
                    "name": name,
                    "status": "completed",
                    "conclusion": "skipped",
                    "steps": [],
                }
                for offset, name in enumerate(names, start=1)
            ),
        )

    def _plan_log(
        self,
        *,
        formula: str,
        tap_ref: str,
        publisher_sha: str | None = None,
        consumer_sha: str | None = None,
        permissions: tuple[str, ...] = (
            "Contents: read",
            "Metadata: read",
        ),
    ) -> str:
        publisher = publisher_sha or rollout.PUBLISHER_WORKFLOW_SHA
        consumer = consumer_sha or self.consumer_sha
        messages = (
            "##[group]GITHUB_TOKEN Permissions",
            *permissions,
            "##[endgroup]",
            "Uses: Automattic/kandelo/.github/workflows/"
            f"reusable-homebrew-bottle-publish.yml@{publisher}",
            "##[group] Inputs",
            f"  kandelo-repository: {rollout.KANDELO_REPOSITORY}",
            f"  kandelo-ref: {consumer}",
            f"  tap-repository: {rollout.REPOSITORY.lower()}",
            f"  tap-name: {rollout.TAP_NAME}",
            f"  tap-ref: {tap_ref}",
            f"  formulae: {formula}",
            f"  arches: {','.join(rollout.required_arches(formula))}",
            "  force: false",
            "  dry-run: false",
            "##[endgroup]",
        )
        return "".join(
            f"2026-07-24T21:33:{index:02d}.0000000Z {message}\n"
            for index, message in enumerate(messages)
        )

    def _explicit_adoption_fixture(
        self,
        *,
        adopted_workflow_source: str | None = None,
        logged_formula: str = "make",
        logged_publisher_sha: str | None = None,
        logged_consumer_sha: str | None = None,
        permissions: tuple[str, ...] = (
            "Contents: read",
            "Metadata: read",
        ),
        workflow_id: int = rollout.WORKFLOW_ID,
        run_attempt: int = 1,
    ):
        source = dataclasses.replace(
            self.snapshot,
            sha="a" * 40,
            workflow_source=self.legacy_workflow_source,
        )
        adopted_source = dataclasses.replace(
            source,
            sha="b" * 40,
            workflow_source=(
                adopted_workflow_source
                if adopted_workflow_source is not None
                else self.transitional_workflow_source
            ),
        )
        current = dataclasses.replace(self.snapshot, sha="c" * 40)
        state = self._failed_state(source, "make", run_id=123)
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123,
            source.sha,
            status="completed",
            conclusion="failure",
        )
        github.jobs_by_run[123] = (
            *self._matrix_jobs("make", "wasm32"),
            *self._skipped_credential_jobs("make", "wasm32"),
        )
        github.runs_by_id[124] = self._run(
            124,
            adopted_source.sha,
            status="completed",
            conclusion="failure",
            workflow_id=workflow_id,
            run_attempt=run_attempt,
        )
        github.jobs_by_run[124] = self._pre_matrix_jobs(plan_id=950)
        github.logs_by_job[950] = self._plan_log(
            formula=logged_formula,
            tap_ref=adopted_source.sha,
            publisher_sha=(
                logged_publisher_sha or rollout.PUBLISHER_WORKFLOW_SHA
            ),
            consumer_sha=(
                logged_consumer_sha or rollout.PUBLISHER_WORKFLOW_SHA
            ),
            permissions=permissions,
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )
        return source, adopted_source, current, state, github, registry

    def _unresolved_pre_matrix_fixture(
        self,
        *,
        created_at: str = "2026-07-24T21:16:42.500000Z",
        logged_publisher_sha: str | None = None,
        logged_consumer_sha: str | None = None,
        run_attempt: int = 1,
    ):
        source = dataclasses.replace(self.snapshot, sha="a" * 40)
        current = dataclasses.replace(source, sha="c" * 40)
        state = rollout.initial_state(source, self.consumer_sha)
        state["unresolved_dispatch"] = {
            "formula": "make",
            "arches": ["wasm32"],
            "tap_sha": source.sha,
            "recorded_at": "2026-07-24T21:16:42Z",
            "before_run_ids": [100],
            "status": "submitted",
            "submitted_at": "2026-07-24T21:16:43Z",
        }
        old_run = self._run(
            100,
            source.sha,
            status="completed",
            conclusion="success",
            created_at="2026-07-24T21:00:00Z",
        )
        failed_run = self._run(
            123,
            source.sha,
            status="completed",
            conclusion="failure",
            created_at=created_at,
            run_attempt=run_attempt,
        )
        github = self._candidate_github(
            failed_run,
            old_run,
            jobs_by_run={123: self._pre_matrix_jobs()},
        )
        github.runs_by_id[123] = failed_run
        github.logs_by_job[900] = self._plan_log(
            formula="make",
            tap_ref=source.sha,
            publisher_sha=logged_publisher_sha,
            consumer_sha=logged_consumer_sha,
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )
        return source, current, state, github, registry, old_run, failed_run

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
        historical_consumer_sha = historical_built_from["kandelo_commit"]
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
        state = rollout.initial_state(cutover, historical_consumer_sha)

        self.assertEqual(["asa"], [
            package["name"] for package in current.metadata["packages"]
        ])
        self.assertEqual(
            previous_binutils["version"],
            current.identities["binutils"].pkg_version,
        )
        rollout.validate_state(state, current, historical_consumer_sha)

        statuses = {
            status.name: status
            for status in rollout.calculate_statuses(
                self.tap,
                current,
                historical_consumer_sha,
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
        state = rollout.initial_state(self.snapshot, self.consumer_sha)
        sidecar = copy.deepcopy(self.snapshot.formula_sidecars["binutils"])
        self.assertIsNotNone(sidecar)
        sidecar["version"] = "999.0"
        current = self._load_snapshot_view(
            formula_sidecars={"binutils": sidecar}
        )

        with self.assertRaisesRegex(rollout.RolloutError, "catalog differs"):
            rollout.validate_state(state, current, self.consumer_sha)

    def test_frozen_catalog_rejects_ledger_or_current_source_tampering(self):
        state = rollout.initial_state(self.snapshot, self.consumer_sha)
        tampered_state = copy.deepcopy(state)
        tampered_state["catalog"]["binutils"]["version"] = "999.0"
        with self.assertRaisesRegex(rollout.RolloutError, "catalog differs"):
            rollout.validate_state(
                tampered_state,
                self.snapshot,
                self.consumer_sha,
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
            rollout.validate_state(state, current, self.consumer_sha)

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
                    expected_kandelo_sha=self.consumer_sha,
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

    def test_workflow_pins_publisher_and_package_consumer_separately(self):
        expected = self.consumer_sha
        vfs_expression = (
            "${{ github.event.client_payload.require_vfs_acceptance || false }}"
        )
        source = self.snapshot.workflow_source
        snapshot = self.snapshot
        rollout.validate_workflow(FakeGitHub(), snapshot, expected)
        with self.assertRaisesRegex(
            rollout.RolloutError, "publisher implementation is not frozen"
        ):
            rollout.validate_workflow(
                FakeGitHub(),
                dataclasses.replace(
                    snapshot,
                    workflow_source=source.replace(
                        rollout.PUBLISHER_WORKFLOW_SHA, "b" * 40
                    ),
                ),
                expected,
            )
        with self.assertRaisesRegex(
            rollout.RolloutError, "package consumer is not frozen"
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
                        "kandelo_commit": self.consumer_sha,
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
                self.tap,
                snapshot,
                "zlib",
                ("wasm32", "wasm64"),
                self.consumer_sha,
            ),
        )

        wrong = copy.deepcopy(snapshot.metadata)
        wrong["packages"][0]["bottle_rebuild"] += 1
        reasons = rollout.finalization_reasons(
            self.tap,
            dataclasses.replace(snapshot, metadata=wrong),
            "zlib",
            ("wasm32", "wasm64"),
            self.consumer_sha,
        )
        self.assertTrue(any("bottle_rebuild" in reason for reason in reasons))

    def test_finalization_rejects_a_self_declared_unapproved_caller(self):
        snapshot = self._finalized_snapshot("zlib")
        original_show = self.tap.show

        def show(revision, path):
            if path == rollout.WORKFLOW_PATH:
                return snapshot.workflow_source + "\n# Unreviewed caller change.\n"
            return original_show(revision, path)

        with mock.patch.object(self.tap, "show", side_effect=show):
            reasons = rollout.finalization_reasons(
                self.tap,
                snapshot,
                "zlib",
                ("wasm32", "wasm64"),
                self.consumer_sha,
            )
        self.assertTrue(
            any(
                "source publication workflow is untrusted" in reason
                and "is not approved" in reason
                for reason in reasons
            )
        )

    def test_explicit_revision_finalizes_and_unblocks_dependents(self):
        python = self._finalized_snapshot("python")
        self.assertEqual("3.13.3_1", python.identities["python"].pkg_version)
        self.assertEqual(
            (),
            rollout.finalization_reasons(
                self.tap,
                python,
                "python",
                ("wasm32",),
                self.consumer_sha,
            ),
        )

        libcxx = self._finalized_snapshot("libcxx")
        statuses = {
            status.name: status
            for status in rollout.calculate_statuses(
                self.tap,
                libcxx,
                self.consumer_sha,
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
            self.consumer_sha,
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
            self.consumer_sha,
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
            self.consumer_sha,
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
            self.consumer_sha,
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

    def test_rollout_state_rejects_a_boolean_or_expanded_dispatch_record(self):
        valid = {
            "formula": "asa",
            "arches": ["wasm32"],
            "tap_sha": self.head,
            "run_id": 123,
            "submitted_at": "2026-07-24T16:00:00Z",
        }
        for dispatch in (
            {**valid, "run_id": True},
            {**valid, "unexpected": "field"},
        ):
            with self.subTest(dispatch=dispatch):
                state = rollout.initial_state(
                    self.snapshot,
                    self.consumer_sha,
                )
                state["dispatches"].append(dispatch)
                with self.assertRaisesRegex(
                    rollout.RolloutError, "malformed dispatch"
                ):
                    rollout.validate_state(
                        state,
                        self.snapshot,
                        self.consumer_sha,
                    )

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

    def test_anonymous_registry_records_the_exact_public_manifest_digest(self):
        media_type = "application/vnd.oci.image.index.v1+json"
        body = json.dumps(
            {"schemaVersion": 2, "mediaType": media_type, "manifests": []},
            separators=(",", ":"),
        ).encode()
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        requests = []

        def opener(request, timeout):
            self.assertEqual(30, timeout)
            requests.append(request)
            if len(requests) == 1:
                return FakeHttpResponse(
                    url=request.full_url,
                    body=b'{"token":"anonymous-read-token"}',
                    headers={"Content-Length": "32"},
                )
            return FakeHttpResponse(
                url=request.full_url,
                body=body,
                headers={
                    "Content-Length": str(len(body)),
                    "Content-Type": media_type,
                    "Docker-Content-Digest": digest,
                },
            )

        evidence = rollout.AnonymousRegistry(opener=opener).manifest(
            "dinit", "0.19.4-1"
        )

        self.assertEqual(
            rollout.RegistryManifestEvidence(exists=True, digest=digest),
            evidence,
        )
        self.assertEqual(2, len(requests))
        self.assertIn(
            "scope=repository%3Akandelo-dev%2Fhomebrew-tap-core%2Fdinit%3Apull",
            requests[0].full_url,
        )
        self.assertEqual(
            "Bearer anonymous-read-token",
            requests[1].get_header("Authorization"),
        )

    def test_anonymous_registry_treats_only_an_exact_404_as_absent(self):
        requests = []

        def opener(request, timeout):
            del timeout
            requests.append(request)
            if len(requests) == 1:
                return FakeHttpResponse(
                    url=request.full_url,
                    body=b'{"token":"anonymous-read-token"}',
                )
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, None
            )

        evidence = rollout.AnonymousRegistry(opener=opener).manifest(
            "erlang", "28.2_1-1"
        )

        self.assertEqual(
            rollout.RegistryManifestEvidence(exists=False, digest=None),
            evidence,
        )

    def test_anonymous_registry_rejects_manifest_digest_substitution(self):
        media_type = "application/vnd.oci.image.index.v1+json"
        body = json.dumps(
            {"schemaVersion": 2, "mediaType": media_type, "manifests": []},
            separators=(",", ":"),
        ).encode()
        calls = 0

        def opener(request, timeout):
            nonlocal calls
            del timeout
            calls += 1
            if calls == 1:
                return FakeHttpResponse(
                    url=request.full_url,
                    body=b'{"token":"anonymous-read-token"}',
                )
            return FakeHttpResponse(
                url=request.full_url,
                body=body,
                headers={
                    "Content-Type": media_type,
                    "Docker-Content-Digest": "sha256:" + "f" * 64,
                },
            )

        with self.assertRaisesRegex(
            rollout.RolloutError, "digest header does not match"
        ):
            rollout.AnonymousRegistry(opener=opener).manifest(
                "dinit", "0.19.4-1"
            )

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

    def test_failed_recovery_rejects_a_boolean_dispatch_run_id(self):
        source = dataclasses.replace(self.snapshot, sha="a" * 40)
        current = dataclasses.replace(source, sha="c" * 40)
        state = self._failed_state(source, "make", run_id=1)
        state["dispatches"][0]["run_id"] = True
        self._assert_failed_recovery_unchanged(
            "requires one controller-recorded run 1",
            github=FakeGitHub(),
            registry=FakeRegistry(
                rollout.RegistryManifestEvidence(exists=False, digest=None)
            ),
            state=state,
            source_snapshot=source,
            current_snapshot=current,
            run_id=1,
        )

    def test_failed_recovery_reserves_the_next_public_identity_atomically(self):
        old_source = rollout.source_with_rebuild(
            self.snapshot.formula_sources["sqlite"], "sqlite", 1
        )
        source = self._snapshot_with_formula_source(
            self.snapshot, "sqlite", old_source, sha="a" * 40
        )
        current_source = rollout.source_with_rebuild(
            old_source, "sqlite", 2
        )
        current = self._snapshot_with_formula_source(
            source, "sqlite", current_source, sha="c" * 40
        )
        state = self._failed_state(source, "sqlite")
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123,
            source.sha,
            status="completed",
            conclusion="failure",
        )
        github.jobs_by_run[123] = self._matrix_jobs(
            "sqlite", "wasm32", "wasm64"
        )
        digest = "sha256:" + "d" * 64
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=True, digest=digest)
        )

        result, recovered = self._recover_failed(
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )

        self.assertEqual(
            (
                "sqlite",
                123,
                "next-rebuild-after-publication",
                "3.49.1_1-1",
            ),
            result,
        )
        self.assertEqual([("sqlite", "3.49.1_1-1")], registry.calls)
        self.assertEqual([], recovered["dispatches"])
        self.assertEqual(2, recovered["catalog"]["sqlite"]["bottle_rebuild"])
        attempt = recovered["failed_attempts"][-1]
        self.assertEqual(digest, attempt["public_manifest_digest"])
        self.assertEqual([], attempt["credential_write_evidence"])
        self.assertEqual(
            state["catalog"]["sqlite"], attempt["previous_catalog"]
        )
        self.assertEqual(
            recovered["catalog"]["sqlite"], attempt["replacement_catalog"]
        )
        self.assertEqual(
            "2026-07-24T20:00:00Z", attempt["recorded_failed_at"]
        )
        rollout.validate_state(recovered, current, self.consumer_sha)
        self.assertNotIn(
            "sqlite",
            rollout.history_blocks_from_state(
                github, recovered, {"sqlite": False}
            ),
        )

    def test_failed_recovery_migrates_a_multi_formula_reservation_as_one_batch(self):
        formulae = ("sqlite", "unzip", "what")
        run_ids = (123, 124, 125)
        source = self.snapshot
        for formula in formulae:
            source = self._snapshot_with_formula_source(
                source,
                formula,
                rollout.source_with_rebuild(
                    source.formula_sources[formula], formula, 1
                ),
                sha="a" * 40,
            )
        current = source
        for formula in formulae:
            current = self._snapshot_with_formula_source(
                current,
                formula,
                rollout.source_with_rebuild(
                    current.formula_sources[formula], formula, 2
                ),
                sha="c" * 40,
            )
        state = rollout.initial_state(source, self.consumer_sha)
        github = FakeGitHub()
        for formula, run_id in zip(formulae, run_ids, strict=True):
            state["dispatches"].append(
                {
                    "formula": formula,
                    "arches": list(rollout.required_arches(formula)),
                    "tap_sha": source.sha,
                    "run_id": run_id,
                    "submitted_at": f"2026-07-24T16:00:{run_id - 123:02d}Z",
                }
            )
            github.runs_by_id[run_id] = self._run(
                run_id,
                source.sha,
                status="completed",
                conclusion="failure",
            )
            github.jobs_by_run[run_id] = self._matrix_jobs(
                formula, *rollout.required_arches(formula)
            )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(
                exists=True, digest="sha256:" + "d" * 64
            )
        )

        results, recovered = self._recover_failed(
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
            run_ids=run_ids,
        )

        self.assertEqual(
            list(zip(formulae, run_ids, strict=True)),
            [(formula, run_id) for formula, run_id, _kind, _ref in results],
        )
        self.assertEqual([], recovered["dispatches"])
        self.assertEqual(
            list(formulae),
            [attempt["formula"] for attempt in recovered["failed_attempts"]],
        )
        for formula in formulae:
            self.assertEqual(
                2, recovered["catalog"][formula]["bottle_rebuild"]
            )
        rollout.validate_state(recovered, current, self.consumer_sha)

    def test_failed_recovery_rejects_a_partial_multi_formula_catalog_migration(self):
        formulae = ("sqlite", "unzip")
        source = self.snapshot
        for formula in formulae:
            source = self._snapshot_with_formula_source(
                source,
                formula,
                rollout.source_with_rebuild(
                    source.formula_sources[formula], formula, 1
                ),
                sha="a" * 40,
            )
        current = source
        for formula in formulae:
            current = self._snapshot_with_formula_source(
                current,
                formula,
                rollout.source_with_rebuild(
                    current.formula_sources[formula], formula, 2
                ),
                sha="c" * 40,
            )
        state = rollout.initial_state(source, self.consumer_sha)
        for formula, run_id in (("sqlite", 123), ("unzip", 124)):
            state["dispatches"].append(
                {
                    "formula": formula,
                    "arches": list(rollout.required_arches(formula)),
                    "tap_sha": source.sha,
                    "run_id": run_id,
                    "submitted_at": "2026-07-24T16:00:00Z",
                }
            )
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123, source.sha, status="completed", conclusion="failure"
        )
        github.jobs_by_run[123] = self._matrix_jobs(
            "sqlite", "wasm32", "wasm64"
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(
                exists=True, digest="sha256:" + "d" * 64
            )
        )

        self._assert_failed_recovery_unchanged(
            "catalog differs from current reviewed cutover",
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )

    def test_failed_recovery_reuses_an_unpublished_identity_with_step_evidence(self):
        old_source = rollout.source_with_rebuild(
            self.snapshot.formula_sources["dinit"], "dinit", 1
        )
        source = self._snapshot_with_formula_source(
            self.snapshot, "dinit", old_source, sha="a" * 40
        )
        fixed_source = old_source.replace(
            "class Dinit < Formula",
            "class Dinit < Formula\n  # The reviewed validator fix lives here.",
            1,
        )
        current = self._snapshot_with_formula_source(
            source, "dinit", fixed_source, sha="c" * 40
        )
        state = self._failed_state(source, "dinit")
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123,
            source.sha,
            status="completed",
            conclusion="failure",
        )
        github.jobs_by_run[123] = (
            *self._matrix_jobs("dinit", "wasm32"),
            *self._skipped_credential_jobs("dinit", "wasm32"),
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )

        result, recovered = self._recover_failed(
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )

        self.assertEqual(
            (
                "dinit",
                123,
                "same-rebuild-without-publication",
                "0.19.4-1",
            ),
            result,
        )
        attempt = recovered["failed_attempts"][-1]
        self.assertIsNone(attempt["public_manifest_digest"])
        self.assertEqual(
            {
                "upload-bottle",
                "publish-bottle-index",
                "finalize-tap",
                "publish-vfs-release",
            },
            {
                evidence["stage"]
                for evidence in attempt["credential_write_evidence"]
            },
        )
        self.assertEqual(
            1, recovered["catalog"]["dinit"]["bottle_rebuild"]
        )
        self.assertNotEqual(
            attempt["previous_catalog"]["formula_contract_sha256"],
            attempt["replacement_catalog"]["formula_contract_sha256"],
        )
        rollout.validate_state(recovered, current, self.consumer_sha)

    def test_failed_recovery_retires_an_unresolved_pre_matrix_intent(self):
        source = dataclasses.replace(self.snapshot, sha="a" * 40)
        current = dataclasses.replace(source, sha="c" * 40)
        state = rollout.initial_state(source, self.consumer_sha)
        state["unresolved_dispatch"] = {
            "formula": "make",
            "arches": ["wasm32"],
            "tap_sha": source.sha,
            "recorded_at": "2026-07-24T21:16:42Z",
            "before_run_ids": [100],
            "status": "submitted",
            "submitted_at": "2026-07-24T21:16:43Z",
        }
        github = FakeGitHub()
        old_run = self._run(
            100,
            source.sha,
            status="completed",
            conclusion="success",
        )
        failed_run = self._run(
            123,
            source.sha,
            status="completed",
            conclusion="failure",
            created_at="2026-07-24T21:16:42.500000Z",
        )
        github.by_status[None] = {
            "total_count": 2,
            "workflow_runs": [failed_run, old_run],
        }
        github.runs_by_id[123] = failed_run
        github.jobs_by_run[123] = self._pre_matrix_jobs()
        github.logs_by_job[900] = self._plan_log(
            formula="make",
            tap_ref=source.sha,
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )

        results, recovered = self._recover_failed(
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
            run_ids=(123,),
        )

        self.assertEqual(
            (("make", 123, "same-rebuild-before-matrix", "4.4.1-1"),),
            results,
        )
        self.assertIsNone(recovered["unresolved_dispatch"])
        self.assertEqual([], recovered["dispatches"])
        attempt = recovered["failed_attempts"][-1]
        self.assertEqual(
            "submitted-intent",
            attempt["correlation_evidence"]["recovery_source"],
        )
        self.assertEqual(
            "make",
            attempt["correlation_evidence"]["logged_formula"],
        )
        self.assertEqual(
            rollout.WORKFLOW_ID,
            attempt["correlation_evidence"]["run_workflow_id"],
        )
        self.assertEqual(
            1,
            attempt["correlation_evidence"]["run_attempt"],
        )
        self.assertEqual(
            {"contents": "read", "metadata": "read"},
            attempt["correlation_evidence"]["plan_token_permissions"],
        )
        rollout.validate_state(recovered, current, self.consumer_sha)

    def test_unresolved_pre_matrix_binds_log_to_ledger_authority(self):
        for field, value in (
            ("logged_publisher_sha", "d" * 40),
            ("logged_consumer_sha", "e" * 40),
        ):
            with self.subTest(field=field):
                (
                    source,
                    current,
                    state,
                    github,
                    registry,
                    _old_run,
                    _failed_run,
                ) = self._unresolved_pre_matrix_fixture(**{field: value})
                self._assert_failed_batch_recovery_unchanged(
                    "plan log differs from its approved caller authority",
                    github=github,
                    registry=registry,
                    state=state,
                    current_snapshot=current,
                    source_snapshots=(source,),
                    run_ids=(123,),
                )

    def test_unresolved_pre_matrix_rejects_an_opaque_competing_run(self):
        (
            source,
            current,
            state,
            github,
            registry,
            old_run,
            failed_run,
        ) = self._unresolved_pre_matrix_fixture()
        opaque_run = self._run(
            124,
            source.sha,
            status="completed",
            conclusion="failure",
            created_at="2026-07-24T21:16:45Z",
        )
        github.by_status[None] = {
            "total_count": 3,
            "workflow_runs": [opaque_run, failed_run, old_run],
        }
        github.jobs_by_run[124] = self._pre_matrix_jobs(plan_id=950)
        github.logs_by_job[950] = ""
        intent = rollout.submitted_dispatch(state)
        with self.assertRaisesRegex(
            rollout.RolloutError, "lacks one exact caller input block"
        ):
            rollout.correlate_pre_matrix_failed_intent(
                github=github,
                intent=intent,
                run_id=123,
            )
        self._assert_failed_batch_recovery_unchanged(
            "requires its one exact pre-matrix failed run",
            github=github,
            registry=registry,
            state=state,
            current_snapshot=current,
            source_snapshots=(source,),
            run_ids=(123,),
        )

    def test_unresolved_pre_matrix_rejects_an_exact_matrix_competitor(self):
        (
            source,
            current,
            state,
            github,
            registry,
            old_run,
            failed_run,
        ) = self._unresolved_pre_matrix_fixture()
        competing_run = self._run(
            124,
            source.sha,
            status="completed",
            conclusion="failure",
            created_at="2026-07-24T21:16:45Z",
        )
        github.by_status[None] = {
            "total_count": 3,
            "workflow_runs": [competing_run, failed_run, old_run],
        }
        github.jobs_by_run[124] = self._matrix_jobs("make", "wasm32")
        with self.assertRaisesRegex(
            rollout.RolloutError, r"found \[123, 124\]"
        ):
            rollout.correlate_pre_matrix_failed_intent(
                github=github,
                intent=rollout.submitted_dispatch(state),
                run_id=123,
            )
        self._assert_failed_batch_recovery_unchanged(
            "requires its one exact pre-matrix failed run",
            github=github,
            registry=registry,
            state=state,
            current_snapshot=current,
            source_snapshots=(source,),
            run_ids=(123,),
        )

    def test_unresolved_pre_matrix_ignores_a_proven_different_formula(self):
        (
            source,
            current,
            state,
            github,
            registry,
            old_run,
            failed_run,
        ) = self._unresolved_pre_matrix_fixture()
        ncurses_run = self._run(
            124,
            source.sha,
            status="completed",
            conclusion="failure",
            created_at="2026-07-24T21:16:45Z",
        )
        github.by_status[None] = {
            "total_count": 3,
            "workflow_runs": [ncurses_run, failed_run, old_run],
        }
        github.jobs_by_run[124] = self._pre_matrix_jobs(plan_id=950)
        github.logs_by_job[950] = self._plan_log(
            formula="ncurses",
            tap_ref=source.sha,
        )

        results, recovered = self._recover_failed(
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
            run_ids=(123,),
        )

        self.assertEqual(123, results[0][1])
        self.assertIsNone(recovered["unresolved_dispatch"])

    def test_unresolved_pre_matrix_rejects_a_pre_intent_run(self):
        (
            source,
            current,
            state,
            github,
            registry,
            _old_run,
            _failed_run,
        ) = self._unresolved_pre_matrix_fixture(
            created_at="2026-07-24T21:16:41Z"
        )
        with self.assertRaisesRegex(rollout.RolloutError, r"found \[\]"):
            rollout.correlate_pre_matrix_failed_intent(
                github=github,
                intent=rollout.submitted_dispatch(state),
                run_id=123,
            )
        self._assert_failed_batch_recovery_unchanged(
            "requires its one exact pre-matrix failed run",
            github=github,
            registry=registry,
            state=state,
            current_snapshot=current,
            source_snapshots=(source,),
            run_ids=(123,),
        )

    def test_unresolved_pre_matrix_rejects_a_rerun_attempt(self):
        (
            source,
            current,
            state,
            github,
            registry,
            _old_run,
            _failed_run,
        ) = self._unresolved_pre_matrix_fixture(run_attempt=2)
        with self.assertRaisesRegex(
            rollout.RolloutError, "is a rerun; only attempt 1 is eligible"
        ):
            rollout.correlate_pre_matrix_failed_intent(
                github=github,
                intent=rollout.submitted_dispatch(state),
                run_id=123,
            )
        self._assert_failed_batch_recovery_unchanged(
            "requires its one exact pre-matrix failed run",
            github=github,
            registry=registry,
            state=state,
            current_snapshot=current,
            source_snapshots=(source,),
            run_ids=(123,),
        )

    def test_pre_matrix_ledger_validation_rejects_tampered_proof(self):
        (
            source,
            current,
            state,
            github,
            registry,
            _old_run,
            _failed_run,
        ) = self._unresolved_pre_matrix_fixture()
        _results, recovered = self._recover_failed(
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
            run_ids=(123,),
        )

        cases = (
            (
                "logged_tap_ref",
                "b" * 40,
                "log differs from caller authority",
            ),
            (
                "run_workflow_id",
                rollout.WORKFLOW_ID + 1,
                "failed pre-matrix recovery is malformed",
            ),
            (
                "run_attempt",
                2,
                "failed pre-matrix recovery is malformed",
            ),
            (
                "plan_token_permissions",
                {
                    "contents": "read",
                    "metadata": "read",
                    "packages": "write",
                },
                "failed pre-matrix recovery is malformed",
            ),
            (
                "run_created_at",
                "2026-07-24T21:16:41Z",
                "predates its submitted intent",
            ),
        )
        for field, value, pattern in cases:
            with self.subTest(field=field):
                tampered = copy.deepcopy(recovered)
                tampered["failed_attempts"][-1]["correlation_evidence"][
                    field
                ] = value
                with self.assertRaisesRegex(rollout.RolloutError, pattern):
                    rollout.validate_state(
                        tampered,
                        current,
                        self.consumer_sha,
                    )

    def test_failed_recovery_adopts_an_explicit_pre_matrix_attempt(self):
        source = dataclasses.replace(
            self.snapshot,
            sha="a" * 40,
            workflow_source=self.legacy_workflow_source,
        )
        adopted_source = dataclasses.replace(
            source,
            sha="b" * 40,
            workflow_source=self.transitional_workflow_source,
        )
        current = dataclasses.replace(self.snapshot, sha="c" * 40)
        state = self._failed_state(source, "make", run_id=123)
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123,
            source.sha,
            status="completed",
            conclusion="failure",
        )
        github.jobs_by_run[123] = (
            *self._matrix_jobs("make", "wasm32"),
            *self._skipped_credential_jobs("make", "wasm32"),
        )
        github.runs_by_id[124] = self._run(
            124,
            adopted_source.sha,
            status="completed",
            conclusion="failure",
        )
        github.jobs_by_run[124] = self._pre_matrix_jobs(plan_id=950)
        github.logs_by_job[950] = self._plan_log(
            formula="make",
            tap_ref=adopted_source.sha,
            publisher_sha=rollout.PUBLISHER_WORKFLOW_SHA,
            consumer_sha=rollout.PUBLISHER_WORKFLOW_SHA,
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )

        results, recovered = self._recover_failed(
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
            run_ids=(123,),
            adopt_failed_runs=(("make", 124),),
            additional_source_snapshots=(adopted_source,),
        )

        self.assertEqual([123, 124], [result[1] for result in results])
        self.assertEqual([], recovered["dispatches"])
        self.assertEqual(
            [
                "same-rebuild-without-publication",
                "same-rebuild-before-matrix",
            ],
            [attempt["recovery_kind"] for attempt in recovered["failed_attempts"]],
        )
        self.assertEqual(
            "explicit-run",
            recovered["failed_attempts"][-1]["correlation_evidence"][
                "recovery_source"
            ],
        )
        explicit = recovered["failed_attempts"][-1]["correlation_evidence"]
        self.assertEqual(
            rollout.PUBLISHER_WORKFLOW_SHA,
            explicit["logged_publisher_sha"],
        )
        self.assertEqual(
            rollout.PUBLISHER_WORKFLOW_SHA,
            explicit["logged_kandelo_ref"],
        )
        self.assertNotIn(
            explicit["source_workflow_sha256"],
            rollout.trusted_workflow_publishers(recovered),
        )
        rollout.validate_state(recovered, current, self.consumer_sha)

    def test_explicit_pre_matrix_adoption_rejects_log_formula_substitution(self):
        source = dataclasses.replace(
            self.snapshot,
            sha="a" * 40,
            workflow_source=self.legacy_workflow_source,
        )
        adopted_source = dataclasses.replace(
            source,
            sha="b" * 40,
            workflow_source=self.transitional_workflow_source,
        )
        current = dataclasses.replace(self.snapshot, sha="c" * 40)
        state = self._failed_state(source, "make", run_id=123)
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123,
            source.sha,
            status="completed",
            conclusion="failure",
        )
        github.jobs_by_run[123] = (
            *self._matrix_jobs("make", "wasm32"),
            *self._skipped_credential_jobs("make", "wasm32"),
        )
        github.runs_by_id[124] = self._run(
            124,
            adopted_source.sha,
            status="completed",
            conclusion="failure",
        )
        github.jobs_by_run[124] = self._pre_matrix_jobs(plan_id=950)
        github.logs_by_job[950] = self._plan_log(
            formula="ncurses",
            tap_ref=adopted_source.sha,
            publisher_sha=rollout.PUBLISHER_WORKFLOW_SHA,
            consumer_sha=rollout.PUBLISHER_WORKFLOW_SHA,
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )

        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)
            original = state_path.read_bytes()
            with (
                mock.patch.object(
                    self.tap,
                    "main_without_fetch",
                    return_value=current.sha,
                ),
                mock.patch.object(self.tap, "is_ancestor", return_value=True),
                mock.patch.object(
                    rollout,
                    "load_snapshot",
                    side_effect=lambda _tap, _sha: {
                        source.sha: source,
                        adopted_source.sha: adopted_source,
                        current.sha: current,
                    }[_sha],
                ),
                self.assertRaisesRegex(
                    rollout.RolloutError,
                    "formulae differs",
                ),
            ):
                rollout.recover_failed_dispatches(
                    tap=self.tap,
                    github=github,
                    registry=registry,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    run_ids=(123,),
                    adopt_failed_runs=(("make", 124),),
                    no_fetch=True,
                )
            self.assertEqual(original, state_path.read_bytes())

    def test_explicit_adoption_requires_the_production_workflow_id(self):
        (
            source,
            adopted_source,
            current,
            state,
            github,
            registry,
        ) = self._explicit_adoption_fixture(
            workflow_id=rollout.WORKFLOW_ID + 1
        )
        self._assert_failed_batch_recovery_unchanged(
            "not an exact completed failed publication",
            github=github,
            registry=registry,
            state=state,
            current_snapshot=current,
            source_snapshots=(source, adopted_source),
            run_ids=(),
            adopt_failed_runs=(("make", 124),),
        )

    def test_explicit_adoption_requires_the_exact_run_id_response(self):
        (
            source,
            adopted_source,
            current,
            state,
            github,
            registry,
        ) = self._explicit_adoption_fixture()
        github.runs_by_id[124].pop("id")
        self._assert_failed_batch_recovery_unchanged(
            "not an exact completed failed publication",
            github=github,
            registry=registry,
            state=state,
            current_snapshot=current,
            source_snapshots=(source, adopted_source),
            run_ids=(),
            adopt_failed_runs=(("make", 124),),
        )

    def test_explicit_adoption_rejects_a_rerun_attempt(self):
        for run_attempt in (2, True):
            with self.subTest(run_attempt=run_attempt):
                (
                    source,
                    adopted_source,
                    current,
                    state,
                    github,
                    registry,
                ) = self._explicit_adoption_fixture(
                    run_attempt=run_attempt
                )
                self._assert_failed_batch_recovery_unchanged(
                    "not an exact completed failed publication",
                    github=github,
                    registry=registry,
                    state=state,
                    current_snapshot=current,
                    source_snapshots=(source, adopted_source),
                    run_ids=(),
                    adopt_failed_runs=(("make", 124),),
                )

    def test_explicit_adoption_requires_an_approved_complete_caller(self):
        (
            source,
            adopted_source,
            current,
            state,
            github,
            registry,
        ) = self._explicit_adoption_fixture(
            adopted_workflow_source=(
                self.transitional_workflow_source
                + "\n# Unreviewed caller change.\n"
            )
        )
        self._assert_failed_batch_recovery_unchanged(
            "publication workflow hash .* is not approved",
            github=github,
            registry=registry,
            state=state,
            current_snapshot=current,
            source_snapshots=(source, adopted_source),
            run_ids=(),
            adopt_failed_runs=(("make", 124),),
        )

    def test_explicit_adoption_binds_log_authority_to_the_approved_caller(self):
        transitional = dataclasses.replace(
            self.snapshot,
            workflow_source=self.transitional_workflow_source,
        )
        with self.assertRaisesRegex(
            rollout.RolloutError, "is not approved"
        ):
            rollout.approved_workflow_authority(transitional)
        self.assertEqual(
            (
                rollout.PUBLISHER_WORKFLOW_SHA,
                rollout.PUBLISHER_WORKFLOW_SHA,
                "exact",
            ),
            rollout.approved_workflow_authority(
                transitional,
                allow_no_write_only=True,
            ),
        )

        for field, value in (
            ("logged_publisher_sha", "d" * 40),
            ("logged_consumer_sha", "e" * 40),
        ):
            with self.subTest(field=field):
                fixture = self._explicit_adoption_fixture(**{field: value})
                source, adopted_source, current, state, github, registry = fixture
                self._assert_failed_batch_recovery_unchanged(
                    "plan log differs from its approved caller authority",
                    github=github,
                    registry=registry,
                    state=state,
                    current_snapshot=current,
                    source_snapshots=(source, adopted_source),
                    run_ids=(),
                    adopt_failed_runs=(("make", 124),),
                )

    def test_explicit_adoption_requires_a_read_only_plan_token(self):
        (
            source,
            adopted_source,
            current,
            state,
            github,
            registry,
        ) = self._explicit_adoption_fixture(
            permissions=(
                "Contents: read",
                "Metadata: read",
                "Packages: write",
            )
        )
        self._assert_failed_batch_recovery_unchanged(
            "exact read-only token permissions",
            github=github,
            registry=registry,
            state=state,
            current_snapshot=current,
            source_snapshots=(source, adopted_source),
            run_ids=(),
            adopt_failed_runs=(("make", 124),),
        )

    def test_failed_recovery_rotates_publisher_without_changing_consumer(self):
        old_source = rollout.source_with_rebuild(
            self.snapshot.formula_sources["sqlite"], "sqlite", 1
        )
        source = self._snapshot_with_formula_source(
            dataclasses.replace(
                self.snapshot,
                workflow_source=self.legacy_workflow_source,
            ),
            "sqlite",
            old_source,
            sha="a" * 40,
        )
        current = self._snapshot_with_formula_source(
            self.snapshot,
            "sqlite",
            rollout.source_with_rebuild(old_source, "sqlite", 2),
            sha="c" * 40,
        )
        state = self._failed_state(source, "sqlite")
        state.pop("expected_publisher_sha")
        state.pop("workflow_rotations")
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123,
            source.sha,
            status="completed",
            conclusion="failure",
        )
        github.jobs_by_run[123] = self._matrix_jobs(
            "sqlite", "wasm32", "wasm64"
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(
                exists=True,
                digest="sha256:" + "d" * 64,
            )
        )

        _result, recovered = self._recover_failed(
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )

        self.assertEqual(self.consumer_sha, recovered["expected_kandelo_sha"])
        self.assertEqual(
            rollout.PUBLISHER_WORKFLOW_SHA,
            recovered["expected_publisher_sha"],
        )
        self.assertEqual(1, len(recovered["workflow_rotations"]))
        rotation = recovered["workflow_rotations"][0]
        self.assertEqual(self.consumer_sha, rotation["old_publisher_sha"])
        self.assertEqual(
            rollout.PUBLISHER_WORKFLOW_SHA,
            rotation["new_publisher_sha"],
        )
        rollout.validate_state(recovered, current, self.consumer_sha)

    def test_failed_recovery_requires_a_new_rebuild_for_an_occupied_identity(self):
        old_source = rollout.source_with_rebuild(
            self.snapshot.formula_sources["sqlite"], "sqlite", 1
        )
        source = self._snapshot_with_formula_source(
            self.snapshot, "sqlite", old_source, sha="a" * 40
        )
        current = dataclasses.replace(source, sha="c" * 40)
        state = self._failed_state(source, "sqlite")
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123, source.sha, status="completed", conclusion="failure"
        )
        github.jobs_by_run[123] = self._matrix_jobs(
            "sqlite", "wasm32", "wasm64"
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(
                exists=True, digest="sha256:" + "d" * 64
            )
        )

        self._assert_failed_recovery_unchanged(
            "is occupied; reserve rebuild 2",
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )

    def test_failed_recovery_rejects_recipe_edits_hidden_in_a_rebuild_bump(self):
        old_source = rollout.source_with_rebuild(
            self.snapshot.formula_sources["sqlite"], "sqlite", 1
        )
        source = self._snapshot_with_formula_source(
            self.snapshot, "sqlite", old_source, sha="a" * 40
        )
        changed_source = rollout.source_with_rebuild(
            old_source, "sqlite", 2
        ).replace(
            "class Sqlite < Formula",
            "class Sqlite < Formula\n  # Unrelated recipe edit.",
            1,
        )
        current = self._snapshot_with_formula_source(
            source, "sqlite", changed_source, sha="c" * 40
        )
        state = self._failed_state(source, "sqlite")
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123, source.sha, status="completed", conclusion="failure"
        )
        github.jobs_by_run[123] = self._matrix_jobs(
            "sqlite", "wasm32", "wasm64"
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(
                exists=True, digest="sha256:" + "d" * 64
            )
        )

        self._assert_failed_recovery_unchanged(
            "changes more than the rebuild reservation",
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )

    def test_failed_recovery_requires_the_same_rebuild_when_public_identity_is_absent(self):
        old_source = rollout.source_with_rebuild(
            self.snapshot.formula_sources["dinit"], "dinit", 1
        )
        source = self._snapshot_with_formula_source(
            self.snapshot, "dinit", old_source, sha="a" * 40
        )
        current = self._snapshot_with_formula_source(
            source,
            "dinit",
            rollout.source_with_rebuild(old_source, "dinit", 2),
            sha="c" * 40,
        )
        state = self._failed_state(source, "dinit")
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123, source.sha, status="completed", conclusion="failure"
        )
        github.jobs_by_run[123] = (
            *self._matrix_jobs("dinit", "wasm32"),
            *self._skipped_credential_jobs("dinit", "wasm32"),
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )

        self._assert_failed_recovery_unchanged(
            "must retain its exact rebuild",
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )

    def test_failed_recovery_rejects_any_credential_bearing_step_that_ran(self):
        source = self._snapshot_with_formula_source(
            self.snapshot,
            "dinit",
            rollout.source_with_rebuild(
                self.snapshot.formula_sources["dinit"], "dinit", 1
            ),
            sha="a" * 40,
        )
        current = dataclasses.replace(source, sha="c" * 40)
        state = self._failed_state(source, "dinit")
        jobs = list(self._skipped_credential_jobs("dinit", "wasm32"))
        jobs[0] = copy.deepcopy(jobs[0])
        jobs[0]["steps"][0]["conclusion"] = "success"
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123, source.sha, status="completed", conclusion="failure"
        )
        github.jobs_by_run[123] = (
            *self._matrix_jobs("dinit", "wasm32"),
            *jobs,
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )

        self._assert_failed_recovery_unchanged(
            "credential-bearing step was not skipped",
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )

    def test_failed_recovery_requires_every_credential_bearing_job(self):
        source = self._snapshot_with_formula_source(
            self.snapshot,
            "dinit",
            rollout.source_with_rebuild(
                self.snapshot.formula_sources["dinit"], "dinit", 1
            ),
            sha="a" * 40,
        )
        current = dataclasses.replace(source, sha="c" * 40)
        state = self._failed_state(source, "dinit")
        write_jobs = tuple(
            job
            for job in self._skipped_credential_jobs("dinit", "wasm32")
            if not job["name"].endswith("publish-vfs-release")
        )
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123, source.sha, status="completed", conclusion="failure"
        )
        github.jobs_by_run[123] = (
            *self._matrix_jobs("dinit", "wasm32"),
            *write_jobs,
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )

        self._assert_failed_recovery_unchanged(
            "exact credential-bearing job set",
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )

    def test_failed_recovery_retains_the_last_green_formula_checksums(self):
        old_source = rollout.source_with_rebuild(
            self.snapshot.formula_sources["sqlite"], "sqlite", 1
        )
        source = self._snapshot_with_formula_source(
            self.snapshot, "sqlite", old_source, sha="a" * 40
        )
        wrong_checksum_source = rollout.source_with_rebuild(
            old_source, "sqlite", 2
        ).replace(
            source.identities["sqlite"].bottle_sha256["wasm32"],
            "f" * 64,
            1,
        )
        current = self._snapshot_with_formula_source(
            source, "sqlite", wrong_checksum_source, sha="c" * 40
        )
        state = self._failed_state(source, "sqlite")
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123, source.sha, status="completed", conclusion="failure"
        )
        github.jobs_by_run[123] = self._matrix_jobs(
            "sqlite", "wasm32", "wasm64"
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(
                exists=True, digest="sha256:" + "d" * 64
            )
        )

        self._assert_failed_recovery_unchanged(
            "no longer retains the last-green wasm32 checksum",
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )

    def test_failed_recovery_rejects_the_wrong_run_or_formula_matrix(self):
        source = self._snapshot_with_formula_source(
            self.snapshot,
            "dinit",
            rollout.source_with_rebuild(
                self.snapshot.formula_sources["dinit"], "dinit", 1
            ),
            sha="a" * 40,
        )
        current = dataclasses.replace(source, sha="c" * 40)
        state = self._failed_state(source, "dinit")
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )
        cases = (
            (
                {
                    key: value
                    for key, value in self._run(
                        123,
                        source.sha,
                        status="completed",
                        conclusion="failure",
                    ).items()
                    if key != "id"
                },
                self._matrix_jobs("dinit", "wasm32"),
                "not the exact completed failed publication",
            ),
            (
                self._run(
                    123,
                    source.sha,
                    status="completed",
                    conclusion="success",
                ),
                self._matrix_jobs("dinit", "wasm32"),
                "not the exact completed failed publication",
            ),
            (
                self._run(
                    123,
                    source.sha,
                    status="completed",
                    conclusion="failure",
                    run_attempt=2,
                ),
                self._matrix_jobs("dinit", "wasm32"),
                "not the exact completed failed publication",
            ),
            (
                self._run(
                    123,
                    "b" * 40,
                    status="completed",
                    conclusion="failure",
                ),
                self._matrix_jobs("dinit", "wasm32"),
                "not the exact completed failed publication",
            ),
            (
                self._run(
                    123,
                    source.sha,
                    status="completed",
                    conclusion="failure",
                ),
                self._matrix_jobs("erlang", "wasm32"),
                "does not contain the exact Formula architecture matrix",
            ),
        )
        for run, jobs, pattern in cases:
            with self.subTest(pattern=pattern, head=run["head_sha"]):
                github = FakeGitHub()
                github.runs_by_id[123] = run
                github.jobs_by_run[123] = jobs
                self._assert_failed_recovery_unchanged(
                    pattern,
                    github=github,
                    registry=registry,
                    state=state,
                    source_snapshot=source,
                    current_snapshot=current,
                )

    def test_failed_attempt_validation_rejects_tampered_registry_evidence(self):
        old_source = rollout.source_with_rebuild(
            self.snapshot.formula_sources["sqlite"], "sqlite", 1
        )
        source = self._snapshot_with_formula_source(
            self.snapshot, "sqlite", old_source, sha="a" * 40
        )
        current = self._snapshot_with_formula_source(
            source,
            "sqlite",
            rollout.source_with_rebuild(old_source, "sqlite", 2),
            sha="c" * 40,
        )
        state = self._failed_state(source, "sqlite")
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123, source.sha, status="completed", conclusion="failure"
        )
        github.jobs_by_run[123] = self._matrix_jobs(
            "sqlite", "wasm32", "wasm64"
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(
                exists=True, digest="sha256:" + "d" * 64
            )
        )
        _result, recovered = self._recover_failed(
            github=github,
            registry=registry,
            state=state,
            source_snapshot=source,
            current_snapshot=current,
        )
        recovered["failed_attempts"][0]["public_manifest_digest"] = None

        with self.assertRaisesRegex(
            rollout.RolloutError, "occupied-identity recovery is malformed"
        ):
            rollout.validate_state(recovered, current, self.consumer_sha)

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

    def test_formula_allowlist_controls_the_actual_dispatch_selection(self):
        class RecordingGitHub(FakeGitHub):
            def __init__(self):
                super().__init__()
                self.dispatches = []

            def dispatch(self, formula, arches, tap_sha):
                self.dispatches.append((formula, tuple(arches), tap_sha))

        github = RecordingGitHub()
        inventory = rollout.RunInventory(
            count=0,
            runs=(),
            formulae={},
            unknown_run_ids=(),
        )
        statuses = (
            rollout.FormulaStatus(
                "asa",
                "ready",
                ("wasm32",),
                (),
                "ready first but omitted from the allowlist",
            ),
            rollout.FormulaStatus(
                "bc",
                "ready",
                ("wasm32",),
                (),
                "ready and explicitly allowed",
            ),
            rollout.FormulaStatus(
                "ncurses",
                "blocked-dependencies",
                ("wasm32",),
                ("make",),
                "allowed would still not make a blocked Formula ready",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(
                state_path,
                rollout.initial_state(self.snapshot, self.consumer_sha),
            )
            with (
                mock.patch.object(
                    self.tap, "main_without_fetch", return_value=self.head
                ),
                mock.patch.object(
                    rollout, "active_inventory", return_value=inventory
                ),
                mock.patch.object(
                    rollout,
                    "reconcile_recorded_activity",
                    return_value=inventory,
                ),
                mock.patch.object(
                    rollout,
                    "finalization_reasons",
                    return_value=("not finalized",),
                ),
                mock.patch.object(
                    rollout, "history_blocks_from_state", return_value={}
                ),
                mock.patch.object(
                    rollout, "calculate_statuses", return_value=statuses
                ),
                mock.patch.object(
                    rollout, "workflow_run_page", return_value=(0, ())
                ),
                mock.patch.object(
                    rollout, "acknowledge_dispatch", return_value=123
                ),
                mock.patch.object(
                    rollout,
                    "_utc_now",
                    side_effect=(
                        "2026-07-24T22:00:00Z",
                        "2026-07-24T22:00:01Z",
                    ),
                ),
                mock.patch("sys.stdout", new=io.StringIO()),
            ):
                count = rollout.dispatch_ready(
                    tap=self.tap,
                    github=github,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    no_fetch=True,
                    maximum=1,
                    timeout_seconds=1,
                    poll_seconds=0.001,
                    allowed_formulae=frozenset(("bc", "ncurses")),
                )
            state = rollout.read_state(state_path)

        self.assertEqual(1, count)
        self.assertEqual(
            [("bc", ("wasm32",), self.head)],
            github.dispatches,
        )
        self.assertIsNotNone(state)
        self.assertEqual(
            ["bc"],
            [entry["formula"] for entry in state["dispatches"]],
        )

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
                    "run_attempt": 1,
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

    def test_recovery_rejects_a_rerun_attempt(self):
        state = self._submitted_state()
        github = self._candidate_github(
            self._run(
                123,
                self.head,
                run_attempt=2,
            ),
            jobs_by_run={123: self._matrix_jobs("asa", "wasm32")},
        )
        self._assert_recovery_fails_unchanged(
            "is a rerun; only attempt 1 is eligible",
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
        rollout.validate_state(abandoned, self.snapshot, self.consumer_sha)

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

    def test_abandonment_rejects_a_rerun_attempt(self):
        state = self._submitted_state()
        github = self._candidate_github(
            self._run(
                123,
                self.head,
                status="completed",
                conclusion="cancelled",
                run_attempt=2,
            ),
            jobs_by_run={
                123: (
                    *self._matrix_jobs("asa", "wasm32"),
                    *self._never_started_write_jobs(),
                )
            },
        )

        self._assert_abandon_fails_unchanged(
            "is a rerun; only attempt 1 is eligible",
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

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rollout.parse_args((*base, "--recover-failed-run", "123"))
        failed = rollout.parse_args(
            (
                *base,
                "--state-file",
                "/tmp/rollout-state.json",
                "--recover-failed-run",
                "123",
                "--recover-failed-run",
                "124",
            )
        )
        self.assertEqual([123, 124], failed.recover_failed_run)
        adopted = rollout.parse_args(
            (
                *base,
                "--state-file",
                "/tmp/rollout-state.json",
                "--recover-failed-run",
                "123",
                "--adopt-failed-run",
                "make=124",
                "--adopt-failed-run",
                "ncurses=125",
            )
        )
        self.assertEqual(
            [("make", 124), ("ncurses", 125)],
            adopted.adopt_failed_run,
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rollout.parse_args(
                (
                    *base,
                    "--state-file",
                    "/tmp/rollout-state.json",
                    "--adopt-failed-run",
                    "unknown=124",
                )
            )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rollout.parse_args(
                (
                    *base,
                    "--state-file",
                    "/tmp/rollout-state.json",
                    "--recover-failed-run",
                    "123",
                    "--recover-failed-run",
                    "123",
                )
            )

        selected = rollout.parse_args(
            (
                *base,
                "--state-file",
                "/tmp/rollout-state.json",
                "--dispatch",
                "--formulae",
                "ncurses,bash,ruby,curl,tar,less,vim,git",
            )
        )
        self.assertEqual(
            frozenset(
                ("ncurses", "bash", "ruby", "curl", "tar", "less", "vim", "git")
            ),
            selected.formulae,
        )
        for invalid in (
            "ncurses,unknown",
            "ncurses,ncurses",
            "ncurses,",
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                rollout.parse_args(
                    (
                        *base,
                        "--state-file",
                        "/tmp/rollout-state.json",
                        "--dispatch",
                        "--formulae",
                        invalid,
                    )
                )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rollout.parse_args((*base, "--formulae", "ncurses"))


if __name__ == "__main__":
    unittest.main()
