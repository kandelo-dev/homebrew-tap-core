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
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager, redirect_stderr
from unittest import mock


SCRIPT = pathlib.Path(__file__).with_name("abi42-rollout.py")
SPEC = importlib.util.spec_from_file_location("abi42_rollout", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
rollout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rollout
SPEC.loader.exec_module(rollout)


class FakeGitHub:
    def __init__(self) -> None:
        self.by_status: dict[str | None, dict] = {}
        self.jobs_by_run: dict[int, tuple[dict, ...]] = {}
        self.logs_by_job: dict[int, str] = {}
        self.runs_by_id: dict[int, dict] = {}
        self.run_queries: list[dict[str, object]] = []

    def runs(self, *, per_page=100, page=1, created=None):
        self.run_queries.append(
            {"per_page": per_page, "page": page, "created": created}
        )
        if None in self.by_status:
            response = self.by_status[None]
        else:
            listed: list[dict] = []
            total_count = 0
            for response in self.by_status.values():
                total_count += response["total_count"]
                listed.extend(response["workflow_runs"])
            response = {
                "total_count": total_count,
                "workflow_runs": listed,
            }
        start = (page - 1) * per_page
        end = start + per_page
        return {
            "total_count": response["total_count"],
            "workflow_runs": response["workflow_runs"][start:end],
        }

    def jobs(self, run_id):
        return self.jobs_by_run.get(run_id, ())

    def run(self, run_id):
        return self.runs_by_id[run_id]

    def job_log(self, job_id):
        return self.logs_by_job[job_id]

    def dispatch(self, formula, arches, tap_sha, dispatch_token):
        raise AssertionError(
            "recovery must never dispatch "
            f"{formula} for {tuple(arches)} from {tap_sha} "
            f"with {dispatch_token}"
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
        self.blob_calls: list[tuple[str, str, int]] = []

    def manifest(
        self, formula: str, reference: str
    ) -> rollout.RegistryManifestEvidence:
        self.calls.append((formula, reference))
        return self.evidence

    def verify_blob(self, formula: str, digest: str, expected_bytes: int):
        self.blob_calls.append((formula, digest, expected_bytes))


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
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status

    def read(self, limit=-1):
        if limit < 0:
            result = self.body[self.offset :]
            self.offset = len(self.body)
            return result
        result = self.body[self.offset : self.offset + limit]
        self.offset += len(result)
        return result


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
        publisher_match = re.search(
            r"reusable-homebrew-bottle-publish\.yml@([0-9a-f]{40})",
            cls.snapshot.workflow_source,
        )
        assert match is not None and publisher_match is not None
        assert publisher_match.group(1) == rollout.PUBLISHER_WORKFLOW_SHA
        cls.consumer_sha = match.group(1)
        cls.legacy_workflow_source = cls.tap.show(
            "71b3004a43be103b315d8d298a89799c3895e98a",
            rollout.WORKFLOW_PATH,
        )
        cls.transitional_workflow_source = cls.tap.show(
            "8b0d41714a0ecce7ca2deb38f5aeecccf9add557",
            rollout.WORKFLOW_PATH,
        )
        cls.precutover_workflow_source = cls.tap.show(
            "e747f724efc63c81af453eeada3b7f1453726058",
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
            hashlib.sha256(cls.precutover_workflow_source.encode()).hexdigest()
            in rollout.APPROVED_PUBLICATION_WORKFLOWS
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

    def _token_state(
        self,
        *intents: tuple[str, str, str],
    ) -> dict:
        state = rollout.initial_state(self.snapshot, self.consumer_sha)
        for formula, token, status in intents:
            value = {
                "formula": formula,
                "arches": list(rollout.required_arches(formula)),
                "tap_sha": self.head,
                "dispatch_token": token,
                "recorded_at": "2026-07-24T07:00:00Z",
                "status": status,
            }
            if status in ("request-started", "submitted"):
                value["request_started_at"] = "2026-07-24T07:00:01Z"
            if status == "submitted":
                value["submitted_at"] = "2026-07-24T07:00:02Z"
            state["pending_dispatches"].append(value)
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
    ) -> tuple[tuple[tuple[str, int], ...], dict]:
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

    def _campaign_contract(self) -> rollout.CampaignContract:
        return rollout.CampaignContract(
            publisher_sha=rollout.PUBLISHER_WORKFLOW_SHA,
            consumer_sha=self.consumer_sha,
            package_generation_sha=rollout.PREPUBLICATION_GENERATION_SHA,
            package_generation_tag=rollout.PREPUBLICATION_STAGING_TAG,
            workflow_sha256=rollout.workflow_sha256(self.snapshot),
        )

    def _campaign_snapshots(
        self,
    ) -> tuple[rollout.TapSnapshot, rollout.TapSnapshot]:
        """Build a complete finalized base and its rebuild-only reservation."""
        base_sources: dict[str, str] = {}
        reservation_sources: dict[str, str] = {}
        base_identities: dict[str, rollout.FormulaIdentity] = {}
        reservation_identities: dict[str, rollout.FormulaIdentity] = {}
        sidecars = copy.deepcopy(self.snapshot.formula_sidecars)
        metadata = copy.deepcopy(self.snapshot.metadata)
        metadata["kandelo_abi"] = rollout.EXPECTED_ABI
        metadata["release_tag"] = rollout.EXPECTED_RELEASE_TAG
        metadata["kandelo_commit"] = self.consumer_sha

        for formula in rollout.FORMULA_ORDER:
            sidecar = sidecars[formula]
            assert isinstance(sidecar, dict)
            # Some checked-in sidecars predate their first ABI 42 finalization
            # and therefore use rebuild zero. A fresh campaign starts only
            # after that rollout is finalized, so model its base as rebuild one
            # or later without changing the package-owned last-green hashes.
            base_rebuild = max(1, sidecar["bottle_rebuild"])
            sidecar["bottle_rebuild"] = base_rebuild
            base_source = rollout.source_with_rebuild(
                self.snapshot.formula_sources[formula],
                formula,
                base_rebuild,
            )
            reservation_source = rollout.source_with_rebuild(
                base_source,
                formula,
                base_rebuild + 1,
            )
            base_sources[formula] = base_source
            reservation_sources[formula] = reservation_source
            base_identities[formula] = rollout.parse_formula_identity(
                formula, base_source, sidecar
            )
            reservation_identities[formula] = rollout.parse_formula_identity(
                formula, reservation_source, sidecar
            )

        metadata["packages"] = [
            copy.deepcopy(sidecars[formula])
            for formula in rollout.FORMULA_ORDER
        ]
        base = dataclasses.replace(
            self.snapshot,
            sha="a" * 40,
            metadata=metadata,
            formula_sources=base_sources,
            formula_sidecars=sidecars,
            identities=base_identities,
        )
        reservation = dataclasses.replace(
            base,
            sha="b" * 40,
            formula_sources=reservation_sources,
            identities=reservation_identities,
        )
        return base, reservation

    @staticmethod
    def _product_first_selection() -> rollout.CampaignSelection:
        rebuild = ("bash",)
        reuse_names = {"libcxx", "ncurses"}
        reuse = tuple(
            formula
            for formula in rollout.FORMULA_ORDER
            if formula in reuse_names
        )
        deferred = tuple(
            formula
            for formula in rollout.FORMULA_ORDER
            if formula not in {*rebuild, *reuse}
        )
        return rollout.CampaignSelection.create(
            rebuild=rebuild,
            reuse=reuse,
            deferred=deferred,
        )

    def _product_first_campaign_snapshots(
        self,
    ) -> tuple[
        rollout.TapSnapshot,
        rollout.TapSnapshot,
        rollout.CampaignSelection,
    ]:
        base, all_reserved = self._campaign_snapshots()
        selection = self._product_first_selection()
        sources = dict(base.formula_sources)
        identities = dict(base.identities)
        for formula in selection.rebuild:
            sources[formula] = all_reserved.formula_sources[formula]
            identities[formula] = all_reserved.identities[formula]
        reservation = dataclasses.replace(
            base,
            sha=all_reserved.sha,
            formula_sources=sources,
            identities=identities,
        )
        return base, reservation, selection

    def _current_successor_snapshots(
        self,
    ) -> tuple[rollout.TapSnapshot, rollout.TapSnapshot]:
        sources: dict[str, str] = {}
        identities: dict[str, rollout.FormulaIdentity] = {}
        for formula in rollout.FORMULA_ORDER:
            successor = self.snapshot.identities[formula].bottle_rebuild + 1
            source = rollout.source_with_rebuild(
                self.snapshot.formula_sources[formula],
                formula,
                successor,
            )
            sources[formula] = source
            identities[formula] = rollout.parse_formula_identity(
                formula,
                source,
                self.snapshot.formula_sidecars[formula],
            )
        reservation = dataclasses.replace(
            self.snapshot,
            sha="c" * 40,
            formula_sources=sources,
            identities=identities,
        )
        return self.snapshot, reservation

    def _campaign_state(
        self,
        base: rollout.TapSnapshot,
        reservation: rollout.TapSnapshot,
    ) -> dict:
        return rollout.initial_campaign_state(
            reservation,
            campaign_id="shell-bottles-2026-07-25",
            base_snapshot=base,
            contract=self._campaign_contract(),
            absent_oci_references={
                formula: reservation.identities[formula].top_reference
                for formula in rollout.FORMULA_ORDER
            },
            checked_at="2026-07-25T12:00:00Z",
        )

    @staticmethod
    def _campaign_reservation_changes() -> tuple[tuple[str, str], ...]:
        return tuple(
            ("M", f"Formula/{formula}.rb")
            for formula in rollout.FORMULA_ORDER
        )

    def _initialize_campaign(
        self,
        *,
        base: rollout.TapSnapshot,
        reservation: rollout.TapSnapshot,
        registry,
        state_path: pathlib.Path,
        observed_main: tuple[str, ...] | None = None,
        selection: rollout.CampaignSelection | None = None,
    ) -> tuple[dict, mock.Mock, FakeGitHub]:
        tap = mock.Mock()
        tap.fetch_main.side_effect = observed_main or (
            reservation.sha,
            reservation.sha,
        )
        tap.is_ancestor.return_value = True
        tap.changed_entries.return_value = tuple(
            ("M", f"Formula/{formula}.rb")
            for formula in (
                selection.rebuild
                if selection is not None
                else rollout.FORMULA_ORDER
            )
        )
        github = FakeGitHub()
        snapshots = {base.sha: base, reservation.sha: reservation}
        with (
            mock.patch.object(
                rollout,
                "load_snapshot",
                side_effect=lambda _tap, sha: snapshots[sha],
            ),
            mock.patch.object(
                rollout, "finalization_reasons", return_value=()
            ),
        ):
            state = rollout.initialize_campaign(
                tap=tap,
                github=github,
                registry=registry,
                state_path=state_path,
                campaign_id="shell-bottles-2026-07-25",
                base_tap_sha=base.sha,
                reservation_tap_sha=reservation.sha,
                contract=self._campaign_contract(),
                no_fetch=False,
                selection=selection,
            )
        return state, tap, github

    @staticmethod
    def _failed_state(
        snapshot,
        formula: str,
        *,
        run_id: int = 123,
        consumer_sha: str | None = None,
    ) -> dict:
        state = rollout.initial_state(
            snapshot,
            consumer_sha or RolloutControllerTests.consumer_sha,
        )
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
                mock.patch.object(self.tap, "ensure_commit"),
                mock.patch.object(self.tap, "changed_entries", return_value=()),
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
                        expected_kandelo_sha=state["expected_kandelo_sha"],
                        state_path=state_path,
                        run_id=run_id,
                        no_fetch=True,
                    )
                else:
                    result = rollout.recover_failed_dispatches(
                        tap=self.tap,
                        github=github,
                        registry=registry,
                        expected_kandelo_sha=state["expected_kandelo_sha"],
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
                    expected_kandelo_sha=state["expected_kandelo_sha"],
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
                    expected_kandelo_sha=state["expected_kandelo_sha"],
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
        display_title: str | None = None,
    ) -> dict:
        run = {
            "id": run_id,
            "event": event,
            "head_sha": head_sha,
            "status": status,
            "conclusion": conclusion,
            "created_at": created_at,
            "workflow_id": workflow_id,
            "run_attempt": run_attempt,
        }
        if display_title is not None:
            run["display_title"] = display_title
        return run

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
        current = dataclasses.replace(
            self.snapshot,
            sha="c" * 40,
            workflow_source=self.precutover_workflow_source,
        )
        state = self._failed_state(
            source,
            "make",
            run_id=123,
            consumer_sha=rollout.LEGACY_ABI42_CONSUMER_SHA,
        )
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
                logged_publisher_sha
                or rollout.LEGACY_PUBLISHER_WORKFLOW_SHA
            ),
            consumer_sha=(
                logged_consumer_sha
                or rollout.LEGACY_PUBLISHER_WORKFLOW_SHA
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

    def test_snapshot_keeps_new_formulae_outside_the_frozen_campaign(self):
        self.assertIn("msmtpd", self.tap.formula_names(self.head))
        self.assertNotIn("msmtpd", self.snapshot.formula_sources)
        self.assertEqual(
            set(rollout.FORMULA_ORDER),
            set(self.snapshot.formula_sources),
        )

    def test_snapshot_still_rejects_a_missing_campaign_formula(self):
        tap = mock.Mock(wraps=self.tap)
        tap.formula_names.return_value = frozenset(rollout.FORMULA_ORDER[1:])

        with self.assertRaisesRegex(
            rollout.RolloutError,
            "tap is missing Formulae from the frozen 63-Formula campaign: "
            r"\['asa'\]",
        ):
            rollout.load_snapshot(tap, self.head)

    @contextmanager
    def _descendant_tap(self, changes):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "tap"
            subprocess.run(
                (
                    "git",
                    "clone",
                    "--quiet",
                    "--shared",
                    str(self.root),
                    str(root),
                ),
                check=True,
            )
            subprocess.run(
                ("git", "checkout", "--quiet", "--detach", self.head),
                cwd=root,
                check=True,
            )
            subprocess.run(
                ("git", "config", "user.name", "Kandelo rollout test"),
                cwd=root,
                check=True,
            )
            subprocess.run(
                ("git", "config", "user.email", "rollout-test@example.invalid"),
                cwd=root,
                check=True,
            )
            for relative, replacement in changes.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                previous = path.read_text() if path.exists() else ""
                path.write_text(
                    replacement(previous)
                    if callable(replacement)
                    else replacement
                )
            subprocess.run(("git", "add", "--all"), cwd=root, check=True)
            subprocess.run(
                ("git", "commit", "--quiet", "-m", "Homebrew: test finalizer output"),
                cwd=root,
                check=True,
            )
            descendant = (
                subprocess.run(
                    ("git", "rev-parse", "HEAD"),
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                .stdout.strip()
            )
            tap = object.__new__(rollout.GitTap)
            tap.root = root.resolve()
            yield tap, descendant

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
        self.assertRegex(
            rollout.PREPUBLICATION_STAGING_TAG,
            r"^package-generation-rootfs-wasm32-abi-v42-sha256-[0-9a-f]{64}$",
        )
        self.assertEqual(
            rollout.CURRENT_MAIN_SHA,
            rollout.PREPUBLICATION_GENERATION_SHA,
        )
        self.assertEqual(
            "pr-1079-staging",
            rollout.LEGACY_PREPUBLICATION_STAGING_TAG,
        )
        self.assertEqual(
            "437fde2524ea6ad9c44933f8abbf995a46841009",
            rollout.LEGACY_PREPUBLICATION_GENERATION_SHA,
        )

    def test_failed_m3_campaign_authority_remains_auditable(self):
        self.assertEqual(
            (
                rollout.FAILED_M3_MAIN_SHA,
                rollout.FAILED_M3_MAIN_SHA,
                "exact",
            ),
            rollout.APPROVED_PUBLICATION_WORKFLOWS[
                rollout.FAILED_M3_CALLER_SHA256
            ],
        )
        self.assertEqual(
            (
                rollout.FAILED_M3_MAIN_SHA,
                rollout.FAILED_M3_MAIN_SHA,
                rollout.FAILED_M3_MAIN_SHA,
                rollout.FAILED_M3_ROOTFS_GENERATION_TAG,
            ),
            rollout.APPROVED_CAMPAIGN_CONTRACTS[
                rollout.FAILED_M3_CALLER_SHA256
            ],
        )
        failed = dataclasses.replace(
            self._campaign_contract(),
            publisher_sha=rollout.FAILED_M3_MAIN_SHA,
            consumer_sha=rollout.FAILED_M3_MAIN_SHA,
            package_generation_sha=rollout.FAILED_M3_MAIN_SHA,
            package_generation_tag=rollout.FAILED_M3_ROOTFS_GENERATION_TAG,
            workflow_sha256=rollout.FAILED_M3_CALLER_SHA256,
        )
        rollout.validate_campaign_contract(failed)
        # WHY: map membership alone would not prove that the exact caller
        # committed in the failed-run ledger still satisfies workflow wiring.
        historical_snapshot = rollout.load_snapshot(
            self.tap,
            "44cac58162ee06c700226578663f34362770286f",
        )
        self.assertEqual(
            rollout.FAILED_M3_CALLER_SHA256,
            rollout.workflow_sha256(historical_snapshot),
        )
        rollout.validate_workflow(
            FakeGitHub(),
            historical_snapshot,
            rollout.FAILED_M3_MAIN_SHA,
            campaign_contract=failed,
        )

    def test_fresh_campaign_requires_one_exact_reviewed_publication_contract(self):
        contract = self._campaign_contract()
        rollout.validate_campaign_contract(contract)
        rollout.validate_workflow(
            FakeGitHub(),
            self.snapshot,
            self.consumer_sha,
            campaign_contract=contract,
        )
        workflow_mutations = {
            "publisher wiring": self.snapshot.workflow_source.replace(
                rollout.PUBLISHER_WORKFLOW_SHA, "c" * 40, 1
            ),
            "consumer wiring": self.snapshot.workflow_source.replace(
                f"kandelo-ref: {self.consumer_sha}",
                f"kandelo-ref: {'d' * 40}",
                1,
            ),
            "generation tag wiring": self.snapshot.workflow_source.replace(
                rollout.PREPUBLICATION_STAGING_TAG, "unreviewed-generation", 1
            ),
            "complete caller bytes": self.snapshot.workflow_source
            + "# unreviewed executable caller bytes\n",
        }
        for label, workflow_source in workflow_mutations.items():
            with (
                self.subTest(workflow=label),
                self.assertRaises(rollout.RolloutError),
            ):
                rollout.validate_workflow(
                    FakeGitHub(),
                    dataclasses.replace(
                        self.snapshot, workflow_source=workflow_source
                    ),
                    self.consumer_sha,
                    campaign_contract=contract,
                )

        substitutions = {
            "publisher": {"publisher_sha": "c" * 40},
            "consumer": {"consumer_sha": "d" * 40},
            "package generation": {"package_generation_sha": "e" * 40},
            "package tag": {"package_generation_tag": "other-generation"},
            "complete workflow": {"workflow_sha256": "a" * 64},
        }
        for label, replacement in substitutions.items():
            with (
                self.subTest(field=label),
                self.assertRaisesRegex(
                    rollout.RolloutError,
                    "not an exact reviewed authority",
                ),
            ):
                rollout.validate_campaign_contract(
                    dataclasses.replace(contract, **replacement)
                )

    def test_fresh_campaign_reserves_every_formula_and_architecture_exactly_once(self):
        base, reservation = self._campaign_snapshots()
        tap = mock.Mock()
        tap.is_ancestor.return_value = True
        tap.changed_entries.return_value = (
            self._campaign_reservation_changes()
        )
        with mock.patch.object(
            rollout, "finalization_reasons", return_value=()
        ):
            rollout.validate_fresh_campaign_reservations(
                tap=tap,
                base=base,
                reservation=reservation,
            )

        reservations = rollout.campaign_reservations(reservation)
        self.assertEqual(63, len(reservation.identities))
        self.assertEqual(70, len(reservations))
        self.assertEqual(70, len({
            (entry["formula"], entry["arch"], entry["reference"])
            for entry in reservations
        }))
        self.assertEqual(
            set(rollout.FORMULA_ORDER),
            {entry["formula"] for entry in reservations},
        )
        for formula in rollout.FORMULA_ORDER:
            self.assertEqual(
                base.identities[formula].bottle_rebuild + 1,
                reservation.identities[formula].bottle_rebuild,
            )

        formula = "asa"
        over_bumped_source = rollout.source_with_rebuild(
            reservation.formula_sources[formula],
            formula,
            reservation.identities[formula].bottle_rebuild + 1,
        )
        over_bumped = self._snapshot_with_formula_source(
            reservation,
            formula,
            over_bumped_source,
            sha=reservation.sha,
        )
        with (
            mock.patch.object(
                rollout, "finalization_reasons", return_value=()
            ),
            self.assertRaisesRegex(
                rollout.RolloutError, "must reserve exact base successor rebuild"
            ),
        ):
            rollout.validate_fresh_campaign_reservations(
                tap=tap,
                base=base,
                reservation=over_bumped,
            )

        edited_source = reservation.formula_sources[formula].replace(
            f"class {formula.capitalize()} < Formula",
            f"class {formula.capitalize()} < Formula\n  # Unreviewed recipe edit.",
            1,
        )
        # ASA's class spelling is not guaranteed to follow capitalize(), so
        # make the mutation independently of package-specific Ruby naming.
        if edited_source == reservation.formula_sources[formula]:
            edited_source = (
                "# Unreviewed recipe edit.\n"
                + reservation.formula_sources[formula]
            )
        edited = self._snapshot_with_formula_source(
            reservation,
            formula,
            edited_source,
            sha=reservation.sha,
        )
        with (
            mock.patch.object(
                rollout, "finalization_reasons", return_value=()
            ),
            self.assertRaisesRegex(
                rollout.RolloutError, "changes more than its rebuild reservation"
            ),
        ):
            rollout.validate_fresh_campaign_reservations(
                tap=tap,
                base=base,
                reservation=edited,
            )

        tap.changed_entries.return_value = (
            *self._campaign_reservation_changes(),
            ("M", "README.md"),
        )
        with (
            mock.patch.object(
                rollout, "finalization_reasons", return_value=()
            ),
            self.assertRaisesRegex(
                rollout.RolloutError,
                "changes beyond the 63 exact Formula rebuild reservations",
            ),
        ):
            rollout.validate_fresh_campaign_reservations(
                tap=tap,
                base=base,
                reservation=reservation,
            )

    def test_product_first_campaign_reserves_only_changed_payloads(self):
        base, reservation, selection = (
            self._product_first_campaign_snapshots()
        )
        tap = mock.Mock()
        tap.is_ancestor.return_value = True
        tap.changed_entries.return_value = (("M", "Formula/bash.rb"),)
        rollout.validate_fresh_campaign_reservations(
            tap=tap,
            base=base,
            reservation=reservation,
            selection=selection,
        )

        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "campaign.json"
            state, _tap, _github = self._initialize_campaign(
                base=base,
                reservation=reservation,
                registry=registry,
                state_path=state_path,
                selection=selection,
            )
            rollout.validate_state(state, reservation, self.consumer_sha)
            rollout.require_dependency_closed_allowlist(
                reservation,
                frozenset(("bash",)),
                campaign_rebuilds=frozenset(selection.rebuild),
            )

        self.assertEqual(3, state["schema"])
        self.assertEqual(["bash"], state["campaign"]["rebuild_formulae"])
        self.assertEqual(
            list(selection.reuse), state["campaign"]["reuse_formulae"]
        )
        self.assertEqual(
            list(selection.deferred), state["campaign"]["deferred_formulae"]
        )
        self.assertEqual(["bash"], state["campaign"]["formulae"])
        self.assertEqual(1, state["campaign"]["architecture_identity_count"])
        self.assertEqual(
            [
                {
                    "formula": "bash",
                    "arch": "wasm32",
                    "reference": reservation.identities["bash"].top_reference,
                }
            ],
            state["campaign"]["reservations"],
        )
        self.assertEqual(
            {"bash": reservation.identities["bash"].top_reference},
            state["campaign"]["absent_oci_references"],
        )
        self.assertEqual(
            [("bash", reservation.identities["bash"].top_reference)],
            registry.calls,
        )
        for formula in (*selection.reuse, *selection.deferred):
            self.assertEqual(
                base.formula_sources[formula],
                reservation.formula_sources[formula],
                formula,
            )

        invalid_dispatch = copy.deepcopy(state)
        invalid_dispatch["dispatches"] = [
            {
                "formula": "libcxx",
                "arches": ["wasm32", "wasm64"],
                "tap_sha": reservation.sha,
                "run_id": 123,
                "submitted_at": "2026-07-25T12:01:00Z",
            }
        ]
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "dispatches outside its campaign rebuild partition",
        ):
            rollout.validate_state(
                invalid_dispatch, reservation, self.consumer_sha
            )

    def test_committed_shell_manifest_is_the_exact_production_partition(self):
        manifest = rollout.load_campaign_manifest(self.tap, self.head)
        expected_reuse = (
            "bzip2",
            "coreutils",
            "curl",
            "dash",
            "diffutils",
            "ed",
            "findutils",
            "gawk",
            "git",
            "grep",
            "gzip",
            "less",
            "libcurl",
            "libcxx",
            "m4",
            "ncurses",
            "openssl",
            "posix-utils-lite",
            "ruby",
            "sed",
            "tar",
            "vim",
            "zlib",
        )
        self.assertEqual(rollout.CAMPAIGN_BASE_TAP_SHA, manifest.base_tap_sha)
        self.assertEqual(
            rollout.CAMPAIGN_MANIFEST_SHA256, manifest.sha256
        )
        self.assertEqual("bash", manifest.rebuild_formula)
        self.assertEqual("5.2.37_2", manifest.rebuild_version)
        self.assertEqual(2, manifest.rebuild_formula_revision)
        self.assertEqual(4, manifest.old_bottle_rebuild)
        self.assertEqual(5, manifest.reserved_bottle_rebuild)
        self.assertEqual(
            expected_reuse,
            tuple(entry.formula for entry in manifest.reuse),
        )
        self.assertEqual(
            set(rollout.FORMULA_ORDER),
            {
                *manifest.selection.rebuild,
                *manifest.selection.reuse,
                *manifest.selection.deferred,
            },
        )
        base = rollout.load_snapshot(self.tap, manifest.base_tap_sha)
        reservation = rollout.load_snapshot(
            self.tap, manifest.reservation_tap_sha
        )
        rollout.validate_campaign_manifest_sources(
            self.tap, manifest, base, reservation
        )
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )
        rollout.verify_campaign_reuse_blobs(registry, manifest)
        self.assertEqual(23, len(registry.blob_calls))

        authority_snapshot = dataclasses.replace(
            self.snapshot,
            workflow_source=self.snapshot.workflow_source,
        )
        state = rollout.initial_campaign_state(
            authority_snapshot,
            campaign_id=rollout.CAMPAIGN_MANIFEST_ID,
            base_snapshot=base,
            contract=self._campaign_contract(),
            absent_oci_references={
                "bash": reservation.identities["bash"].top_reference
            },
            checked_at="2026-07-25T12:00:00Z",
            manifest=manifest,
            manifest_authority_sha=self.head,
            reservation_snapshot=reservation,
        )
        self.assertEqual(4, state["schema"])
        self.assertEqual(
            self.head, state["campaign"]["manifest_tap_sha"]
        )
        self.assertEqual(
            manifest.sha256, state["campaign"]["manifest_sha256"]
        )
        self.assertEqual(
            rollout.CAMPAIGN_BASE_TAP_SHA,
            state["campaign"]["base_tap_sha"],
        )
        self.assertEqual(
            list(manifest.selection.reuse),
            state["campaign"]["reuse_formulae"],
        )
        self.assertEqual(
            list(manifest.selection.deferred),
            state["campaign"]["deferred_formulae"],
        )
        with self.assertRaisesRegex(
            rollout.RolloutError, "cannot override"
        ):
            rollout.initial_campaign_state(
                authority_snapshot,
                campaign_id=rollout.CAMPAIGN_MANIFEST_ID,
                base_snapshot=base,
                contract=self._campaign_contract(),
                absent_oci_references={
                    "bash": reservation.identities["bash"].top_reference
                },
                checked_at="2026-07-25T12:00:00Z",
                selection=manifest.selection,
                manifest=manifest,
                manifest_authority_sha=self.head,
                reservation_snapshot=reservation,
            )

    def test_shell_manifest_rejects_partition_and_t0_substitution(self):
        raw = (self.root / rollout.CAMPAIGN_MANIFEST_PATH).read_bytes()
        value = json.loads(raw)

        class BytesTap:
            def __init__(self, body):
                self.body = body
                self.calls = []

            def show_bytes(self, revision, path):
                self.calls.append((revision, path))
                return self.body

        def encoded(candidate):
            return (json.dumps(candidate, indent=2) + "\n").encode()

        for label, mutate, message in (
            (
                "remove",
                lambda candidate: candidate["reuse"].pop(),
                "canonical production authority",
            ),
            (
                "add arbitrary asa",
                lambda candidate: candidate["reuse"].append(
                    {
                        **copy.deepcopy(candidate["reuse"][0]),
                        "formula": "asa",
                        "sidecar": {
                            **candidate["reuse"][0]["sidecar"],
                            "path": "Kandelo/formula/asa.json",
                        },
                    }
                ),
                "canonical production authority",
            ),
            (
                "substitute",
                lambda candidate: candidate["reuse"][0].update(
                    {"formula": "asa"}
                ),
                "canonical production authority",
            ),
            (
                "different T0",
                lambda candidate: candidate.update(
                    {"base_tap_sha": "f" * 40}
                ),
                "canonical production authority",
            ),
        ):
            candidate = copy.deepcopy(value)
            mutate(candidate)
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(rollout.RolloutError, message),
            ):
                rollout.load_campaign_manifest(
                    BytesTap(encoded(candidate)),
                    "a" * 40,
                )

        exact = BytesTap(raw)
        manifest = rollout.load_campaign_manifest(exact, "b" * 40)
        self.assertEqual(
            [("b" * 40, rollout.CAMPAIGN_MANIFEST_PATH)],
            exact.calls,
        )
        with self.assertRaisesRegex(
            rollout.RolloutError, "bytes differ from the private ledger"
        ):
            rollout.load_campaign_manifest(
                exact,
                "b" * 40,
                expected_sha256="0" * 64,
            )
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(), manifest.sha256
        )

    def test_shell_manifest_rejects_stale_raw_sidecar_and_link_bytes(self):
        manifest = rollout.load_campaign_manifest(self.tap, self.head)
        base = rollout.load_snapshot(self.tap, manifest.base_tap_sha)
        reservation = rollout.load_snapshot(
            self.tap, manifest.reservation_tap_sha
        )
        first = manifest.reuse[0]

        class TamperedTap:
            def __init__(self, changed_path):
                self.changed_path = changed_path

            def show_bytes(inner_self, revision, path):
                raw = self.tap.show_bytes(revision, path)
                return raw + b" " if path == inner_self.changed_path else raw

        for path, message in (
            (first.sidecar_path, "sidecar bytes differ at T0"),
            (first.link_manifest_path, "link bytes differ at T0"),
        ):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(rollout.RolloutError, message),
            ):
                rollout.validate_campaign_manifest_sources(
                    TamperedTap(path),
                    manifest,
                    base,
                    reservation,
                )

    def test_manifest_campaign_hashes_all_reuse_blobs_again_before_dispatch(self):
        events: list[tuple[str, str]] = []

        class DispatchGitHub(FakeGitHub):
            def __init__(inner_self):
                super().__init__()
                inner_self.dispatches = []
                inner_self.created_runs = []

            def runs(
                inner_self, *, per_page=100, page=1, created=None
            ):
                del created
                start = (page - 1) * per_page
                return {
                    "total_count": len(inner_self.created_runs),
                    "workflow_runs": inner_self.created_runs[
                        start : start + per_page
                    ],
                }

            def dispatch(
                inner_self, formula, arches, tap_sha, dispatch_token
            ):
                events.append(("dispatch", formula))
                inner_self.dispatches.append(
                    (formula, tuple(arches), tap_sha, dispatch_token)
                )
                inner_self.created_runs.append(
                    self._run(
                        200 + len(inner_self.created_runs),
                        tap_sha,
                        status="queued",
                        created_at=rollout._utc_now(),
                        display_title=rollout.workflow_run_title(
                            formula, dispatch_token
                        ),
                    )
                )

        class RecordingRegistry(FakeRegistry):
            def manifest(inner_self, formula, reference):
                events.append(("absence", formula))
                return super().manifest(formula, reference)

            def verify_blob(
                inner_self, formula, digest, expected_bytes
            ):
                events.append(("blob", formula))
                return super().verify_blob(
                    formula, digest, expected_bytes
                )

        with tempfile.TemporaryDirectory() as directory:
            # WHY: status calculation must consume real committed T0, Tpre,
            # and Tmanifest snapshots. A local shared clone gives the test an
            # exact protected-main coordinate without mocking Git evidence.
            tap_root = pathlib.Path(directory) / "tap"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--shared",
                    str(self.root),
                    str(tap_root),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(tap_root),
                    "remote",
                    "set-url",
                    "origin",
                    "https://github.com/Kandelo-dev/homebrew-tap-core.git",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(tap_root),
                    "update-ref",
                    "refs/remotes/origin/main",
                    self.head,
                ],
                check=True,
            )
            tap = rollout.GitTap(tap_root)
            manifest = rollout.load_campaign_manifest(tap, self.head)
            authority = rollout.load_snapshot(tap, self.head)
            github = DispatchGitHub()
            registry = RecordingRegistry(
                rollout.RegistryManifestEvidence(exists=False, digest=None)
            )
            state_path = pathlib.Path(directory) / "manifest-campaign.json"
            state = rollout.initialize_campaign(
                tap=tap,
                github=github,
                registry=registry,
                state_path=state_path,
                campaign_id=rollout.CAMPAIGN_MANIFEST_ID,
                base_tap_sha=manifest.base_tap_sha,
                reservation_tap_sha=manifest.reservation_tap_sha,
                contract=self._campaign_contract(),
                no_fetch=True,
                manifest_authority_sha=authority.sha,
            )
            self.assertEqual(4, state["schema"])
            self.assertEqual(23, len(registry.blob_calls))
            self.assertEqual(
                manifest.sha256, state["campaign"]["manifest_sha256"]
            )
            self.assertEqual(0o600, state_path.stat().st_mode & 0o777)
            pristine_state = copy.deepcopy(state)

            validated_manifest = rollout.validate_campaign_main_descendant(
                tap, state, authority
            )
            self.assertIsNotNone(validated_manifest)
            statuses = {
                status.name: status
                for status in rollout.calculate_statuses(
                    tap,
                    authority,
                    self.consumer_sha,
                    rollout.RunInventory(
                        count=0,
                        runs=(),
                        formulae={},
                        unknown_run_ids=(),
                    ),
                    {},
                    campaign_manifest=validated_manifest,
                )
            }
            self.assertEqual("ready", statuses["bash"].state)
            self.assertEqual("reused", statuses["ncurses"].state)
            self.assertEqual("reused", statuses["libcxx"].state)
            self.assertTrue(
                any(
                    "another Kandelo SHA" in reason
                    for reason in rollout.finalization_reasons(
                        tap,
                        authority,
                        "ncurses",
                        ("wasm32",),
                        self.consumer_sha,
                    )
                ),
                "reuse must preserve ncurses' historical built_from provenance",
            )
            assert validated_manifest is not None
            reuse_without_ncurses = tuple(
                entry
                for entry in validated_manifest.reuse
                if entry.formula != "ncurses"
            )
            remaining_reuse = {
                entry.formula for entry in reuse_without_ncurses
            }
            deferred_ncurses = dataclasses.replace(
                validated_manifest,
                reuse=reuse_without_ncurses,
                deferred=tuple(
                    formula
                    for formula in rollout.FORMULA_ORDER
                    if formula != "bash" and formula not in remaining_reuse
                ),
            )
            deferred_statuses = {
                status.name: status
                for status in rollout.calculate_statuses(
                    tap,
                    authority,
                    self.consumer_sha,
                    rollout.RunInventory(
                        count=0,
                        runs=(),
                        formulae={},
                        unknown_run_ids=(),
                    ),
                    {},
                    campaign_manifest=deferred_ncurses,
                )
            }
            self.assertEqual(
                "blocked-dependencies", deferred_statuses["bash"].state
            )
            self.assertIn(
                "ncurses/wasm32", deferred_statuses["bash"].detail
            )

            events.clear()
            dispatched = rollout.dispatch_ready(
                tap=tap,
                github=github,
                expected_kandelo_sha=self.consumer_sha,
                state_path=state_path,
                no_fetch=True,
                maximum=8,
                timeout_seconds=1,
                poll_seconds=0.001,
                allowed_formulae=frozenset(("bash",)),
                registry=registry,
            )
            successful_dispatches = list(github.dispatches)
            successful_events = list(events)

            class FlipAfterReuseRegistry(RecordingRegistry):
                def manifest(inner_self, formula, reference):
                    inner_self.calls.append((formula, reference))
                    if len(inner_self.blob_calls) == 23:
                        return rollout.RegistryManifestEvidence(
                            exists=True,
                            digest="sha256:" + "f" * 64,
                        )
                    return rollout.RegistryManifestEvidence(
                        exists=False,
                        digest=None,
                    )

            flip_registry = FlipAfterReuseRegistry(
                rollout.RegistryManifestEvidence(
                    exists=False, digest=None
                )
            )
            flip_path = pathlib.Path(directory) / "flip-campaign.json"
            rollout.write_new_state(flip_path, pristine_state)
            flip_github = DispatchGitHub()
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "campaign OCI identity is already occupied",
            ):
                rollout.dispatch_ready(
                    tap=tap,
                    github=flip_github,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=flip_path,
                    no_fetch=True,
                    maximum=8,
                    timeout_seconds=1,
                    poll_seconds=0.001,
                    allowed_formulae=frozenset(("bash",)),
                    registry=flip_registry,
                )
            stalled = rollout.read_state(flip_path)

        self.assertEqual(1, dispatched)
        self.assertEqual(46, len(registry.blob_calls))
        self.assertEqual(1, len(successful_dispatches))
        self.assertEqual("bash", successful_dispatches[0][0])
        self.assertNotIn("ncurses", (entry[0] for entry in successful_dispatches))
        self.assertNotIn("libcxx", (entry[0] for entry in successful_dispatches))
        self.assertEqual(
            [
                *(("blob", entry.formula) for entry in manifest.reuse),
                ("absence", "bash"),
                ("dispatch", "bash"),
            ],
            successful_events[-25:],
        )
        self.assertEqual(23, len(flip_registry.blob_calls))
        self.assertEqual([], flip_github.dispatches)
        assert stalled is not None
        self.assertEqual(1, len(stalled["pending_dispatches"]))
        self.assertEqual(
            "planned", stalled["pending_dispatches"][0]["status"]
        )
        self.assertNotIn(
            "request_started_at", stalled["pending_dispatches"][0]
        )

    def test_product_first_campaign_rejects_reuse_sidecar_from_old_abi(self):
        base, reservation, selection = (
            self._product_first_campaign_snapshots()
        )
        sidecars = copy.deepcopy(base.formula_sidecars)
        sidecars["libcxx"]["kandelo_abi"] = 41
        base = dataclasses.replace(base, formula_sidecars=sidecars)
        reservation = dataclasses.replace(
            reservation,
            formula_sidecars=copy.deepcopy(sidecars),
        )
        tap = mock.Mock()
        tap.is_ancestor.return_value = True
        tap.changed_entries.return_value = (("M", "Formula/bash.rb"),)

        with self.assertRaisesRegex(
            rollout.RolloutError,
            "libcxx reuse sidecar ABI is not 42",
        ):
            rollout.validate_fresh_campaign_reservations(
                tap=tap,
                base=base,
                reservation=reservation,
                selection=selection,
            )

    def test_product_first_campaign_rejects_reuse_bottle_from_old_abi(self):
        base, reservation, selection = (
            self._product_first_campaign_snapshots()
        )
        sidecars = copy.deepcopy(base.formula_sidecars)
        wasm32 = next(
            bottle
            for bottle in sidecars["libcxx"]["bottles"]
            if bottle["arch"] == "wasm32"
        )
        wasm32["kandelo_abi"] = 41
        base = dataclasses.replace(base, formula_sidecars=sidecars)
        reservation = dataclasses.replace(
            reservation,
            formula_sidecars=copy.deepcopy(sidecars),
        )
        tap = mock.Mock()
        tap.is_ancestor.return_value = True
        tap.changed_entries.return_value = (("M", "Formula/bash.rb"),)

        with self.assertRaisesRegex(
            rollout.RolloutError,
            "libcxx reuse wasm32 bottle ABI is not 42",
        ):
            rollout.validate_fresh_campaign_reservations(
                tap=tap,
                base=base,
                reservation=reservation,
                selection=selection,
            )

    def test_product_first_campaign_rejects_partition_and_unselected_edits(self):
        base, reservation, selection = (
            self._product_first_campaign_snapshots()
        )
        with self.assertRaisesRegex(
            rollout.RolloutError, "Formula partitions overlap"
        ):
            rollout.CampaignSelection.create(
                rebuild=("bash",),
                reuse=("bash",),
                deferred=selection.deferred,
            )

        formula = "libcxx"
        changed_source = rollout.source_with_rebuild(
            reservation.formula_sources[formula],
            formula,
            reservation.identities[formula].bottle_rebuild + 1,
        )
        changed = self._snapshot_with_formula_source(
            reservation,
            formula,
            changed_source,
            sha=reservation.sha,
        )
        tap = mock.Mock()
        tap.is_ancestor.return_value = True
        tap.changed_entries.return_value = (
            ("M", "Formula/bash.rb"),
            ("M", "Formula/libcxx.rb"),
        )
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "libcxx changed outside the campaign rebuild partition",
        ):
            rollout.validate_fresh_campaign_reservations(
                tap=tap,
                base=base,
                reservation=changed,
                selection=selection,
            )

    def test_fresh_campaign_initialization_writes_one_complete_private_ledger(self):
        base, reservation = self._campaign_snapshots()
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "campaign.json"
            state, tap, _github = self._initialize_campaign(
                base=base,
                reservation=reservation,
                registry=registry,
                state_path=state_path,
            )
            persisted = rollout.read_state(state_path)
            self.assertEqual(state, persisted)
            self.assertEqual(0o600, state_path.stat().st_mode & 0o777)
            rollout.validate_state(state, reservation, self.consumer_sha)

        self.assertEqual(2, tap.fetch_main.call_count)
        tap.ensure_commit.assert_called_once_with(base.sha)
        self.assertEqual(63, len(registry.calls))
        self.assertEqual(63, len(state["campaign"]["formulae"]))
        self.assertEqual(70, len(state["campaign"]["reservations"]))
        self.assertEqual(63, len(state["campaign"]["absent_oci_references"]))

    def test_fresh_campaign_uses_sidecars_when_the_partial_base_is_ahead(self):
        base, reservation = self._campaign_snapshots()
        formula = "asa"
        last_green_rebuild = base.formula_sidecars[formula]["bottle_rebuild"]
        advanced_source = rollout.source_with_rebuild(
            base.formula_sources[formula],
            formula,
            last_green_rebuild + 2,
        )
        base = self._snapshot_with_formula_source(
            base,
            formula,
            advanced_source,
            sha=base.sha,
        )
        successor_source = rollout.source_with_rebuild(
            advanced_source,
            formula,
            last_green_rebuild + 3,
        )
        reservation = self._snapshot_with_formula_source(
            reservation,
            formula,
            successor_source,
            sha=reservation.sha,
        )
        partial_metadata = copy.deepcopy(base.metadata)
        partial_metadata["packages"] = partial_metadata["packages"][:1]
        base = dataclasses.replace(base, metadata=partial_metadata)
        reservation = dataclasses.replace(
            reservation, metadata=copy.deepcopy(partial_metadata)
        )
        self.assertEqual(
            last_green_rebuild + 2,
            base.identities[formula].bottle_rebuild,
        )
        self.assertEqual(
            last_green_rebuild + 3,
            reservation.identities[formula].bottle_rebuild,
        )

        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "campaign.json"
            state, _tap, _github = self._initialize_campaign(
                base=base,
                reservation=reservation,
                registry=registry,
                state_path=state_path,
            )
            rollout.validate_state(state, reservation, self.consumer_sha)

        self.assertEqual(
            last_green_rebuild,
            state["previous_catalog"][formula]["bottle_rebuild"],
        )
        self.assertEqual(
            last_green_rebuild + 2,
            state["base_catalog"][formula]["bottle_rebuild"],
        )
        self.assertEqual(
            last_green_rebuild + 3,
            state["initial_catalog"][formula]["bottle_rebuild"],
        )
        self.assertEqual(63, len(registry.calls))

    def test_current_successors_avoid_all_six_historical_collisions(self):
        base, reservation = self._current_successor_snapshots()
        # These are the exact collisions that the old sidecar+1 rule selected:
        # libxml2's Formula is one rebuild ahead of its sidecar and the other
        # five are two ahead. Reserving T0+1 skips every historical identity.
        collision_gaps = {
            "libmagic": 2,
            "libpng": 2,
            "libxml2": 1,
            "sqlite": 2,
            "unzip": 2,
            "what": 2,
        }
        collisions = tuple(collision_gaps)
        historical = {
            (
                formula,
                rollout.homebrew_top_reference(
                    base.formula_sidecars[formula]["version"],
                    base.formula_sidecars[formula]["bottle_rebuild"] + 1,
                ),
            )
            for formula in collisions
        }
        for formula in collisions:
            last_green = base.formula_sidecars[formula]["bottle_rebuild"]
            self.assertEqual(
                last_green + collision_gaps[formula],
                base.identities[formula].bottle_rebuild,
            )
            self.assertEqual(
                base.identities[formula].bottle_rebuild + 1,
                reservation.identities[formula].bottle_rebuild,
            )
            self.assertNotIn(
                (
                    formula,
                    reservation.identities[formula].top_reference,
                ),
                historical,
            )

        class HistoricalCollisionRegistry:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def manifest(self, formula, reference):
                self.calls.append((formula, reference))
                if (formula, reference) in historical:
                    return rollout.RegistryManifestEvidence(
                        exists=True,
                        digest="sha256:" + "1" * 64,
                    )
                return rollout.RegistryManifestEvidence(
                    exists=False,
                    digest=None,
                )

        registry = HistoricalCollisionRegistry()
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "campaign.json"
            state, _tap, _github = self._initialize_campaign(
                base=base,
                reservation=reservation,
                registry=registry,
                state_path=state_path,
            )
            rollout.validate_state(state, reservation, self.consumer_sha)

        self.assertEqual(63, len(registry.calls))
        self.assertTrue(historical.isdisjoint(registry.calls))
        for formula in collisions:
            self.assertEqual(
                base.identities[formula].bottle_rebuild,
                state["base_catalog"][formula]["bottle_rebuild"],
            )
            self.assertEqual(
                base.identities[formula].bottle_rebuild + 1,
                state["initial_catalog"][formula]["bottle_rebuild"],
            )

    def test_fresh_campaign_rejects_a_base_behind_its_last_green_sidecar(self):
        base, reservation = self._campaign_snapshots()
        formula = "asa"
        sidecars = copy.deepcopy(base.formula_sidecars)
        sidecars[formula]["bottle_rebuild"] = (
            base.identities[formula].bottle_rebuild + 1
        )
        behind = dataclasses.replace(
            base,
            formula_sidecars=sidecars,
        )
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "does not describe a finalized predecessor",
        ):
            rollout.validate_fresh_campaign_reservations(
                tap=mock.Mock(),
                base=behind,
                reservation=reservation,
            )

    def test_fresh_campaign_rejects_last_green_checksum_drift(self):
        base, reservation = self._campaign_snapshots()
        formula = "asa"
        old_digest = reservation.identities[formula].bottle_sha256["wasm32"]
        replacement = (
            ("0" if old_digest[0] != "0" else "1") + old_digest[1:]
        )
        changed = self._snapshot_with_formula_source(
            reservation,
            formula,
            reservation.formula_sources[formula].replace(
                old_digest,
                replacement,
                1,
            ),
            sha=reservation.sha,
        )
        tap = mock.Mock()
        tap.is_ancestor.return_value = True
        with self.assertRaisesRegex(
            rollout.RolloutError,
            "no longer retains the last-green wasm32 checksum",
        ):
            rollout.validate_fresh_campaign_reservations(
                tap=tap,
                base=base,
                reservation=changed,
            )

    def test_fresh_campaign_rejects_an_occupied_base_successor(self):
        base, reservation = self._campaign_snapshots()
        formula = rollout.FORMULA_ORDER[0]
        self.assertEqual(
            base.identities[formula].bottle_rebuild + 1,
            reservation.identities[formula].bottle_rebuild,
        )
        occupied = FakeRegistry(
            rollout.RegistryManifestEvidence(
                exists=True,
                digest="sha256:" + "1" * 64,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "campaign.json"
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "OCI identity is already occupied",
            ):
                self._initialize_campaign(
                    base=base,
                    reservation=reservation,
                    registry=occupied,
                    state_path=state_path,
                )
            self.assertFalse(state_path.exists())
        self.assertEqual(
            [(formula, reservation.identities[formula].top_reference)],
            occupied.calls,
        )

    def test_fresh_campaign_refuses_any_existing_ledger_before_observation(self):
        base, reservation = self._campaign_snapshots()
        for contents in (
            b"",
            b'{"schema":1}\n',
            b'{"schema":2,"campaign":{}}\n',
        ):
            with self.subTest(contents=contents):
                with tempfile.TemporaryDirectory() as directory:
                    state_path = pathlib.Path(directory) / "campaign.json"
                    state_path.write_bytes(contents)
                    tap = mock.Mock()
                    github = mock.Mock()
                    registry = mock.Mock()
                    with self.assertRaisesRegex(
                        rollout.RolloutError, "already exists"
                    ):
                        rollout.initialize_campaign(
                            tap=tap,
                            github=github,
                            registry=registry,
                            state_path=state_path,
                            campaign_id="shell-bottles-2026-07-25",
                            base_tap_sha=base.sha,
                            reservation_tap_sha=reservation.sha,
                            contract=self._campaign_contract(),
                            no_fetch=False,
                        )
                    self.assertEqual(contents, state_path.read_bytes())
                    self.assertEqual([], tap.mock_calls)
                    self.assertEqual([], github.mock_calls)
                    self.assertEqual([], registry.mock_calls)

    def test_fresh_campaign_registry_failure_never_creates_a_ledger(self):
        base, reservation = self._campaign_snapshots()
        cases = {
            "absence with digest": rollout.RegistryManifestEvidence(
                exists=False, digest="sha256:" + "1" * 64
            ),
            "malformed type": {
                "exists": False,
                "digest": None,
            },
        }
        for label, evidence in cases.items():
            with self.subTest(case=label):
                registry = FakeRegistry(evidence)
                with tempfile.TemporaryDirectory() as directory:
                    state_path = pathlib.Path(directory) / "campaign.json"
                    with self.assertRaisesRegex(
                        rollout.RolloutError, "OCI identity is already occupied"
                    ):
                        self._initialize_campaign(
                            base=base,
                            reservation=reservation,
                            registry=registry,
                            state_path=state_path,
                        )
                    self.assertFalse(state_path.exists())
                    self.assertEqual(1, len(registry.calls))

        registry = mock.Mock()
        registry.manifest.side_effect = rollout.RolloutError(
            "anonymous registry response is malformed"
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "campaign.json"
            with self.assertRaisesRegex(
                rollout.RolloutError, "registry response is malformed"
            ):
                self._initialize_campaign(
                    base=base,
                    reservation=reservation,
                    registry=registry,
                    state_path=state_path,
                )
            self.assertFalse(state_path.exists())

    def test_fresh_campaign_rechecks_protected_main_before_writing(self):
        base, reservation = self._campaign_snapshots()
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "campaign.json"
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "protected tap main moved during campaign initialization",
            ):
                self._initialize_campaign(
                    base=base,
                    reservation=reservation,
                    registry=registry,
                    state_path=state_path,
                    observed_main=(reservation.sha, "c" * 40),
                )
            self.assertFalse(state_path.exists())
        self.assertEqual(63, len(registry.calls))

    def test_schema_two_campaign_rejects_every_contract_surface_mutation(self):
        base, reservation = self._campaign_snapshots()
        original = self._campaign_state(base, reservation)
        rollout.validate_state(original, reservation, self.consumer_sha)

        absent = copy.deepcopy(
            original["campaign"]["absent_oci_references"]
        )
        absent.pop("asa")
        mutations = (
            ("schema downgrade", ("schema",), 1),
            ("campaign id", ("campaign", "id"), ""),
            ("base SHA", ("campaign", "base_tap_sha"), reservation.sha),
            ("reservation SHA", ("campaign", "reservation_tap_sha"), "c" * 40),
            ("initialized time", ("campaign", "initialized_at"), ""),
            (
                "publisher",
                ("campaign", "expected_publisher_sha"),
                "c" * 40,
            ),
            (
                "consumer",
                ("campaign", "expected_consumer_sha"),
                "c" * 40,
            ),
            (
                "package generation",
                ("campaign", "expected_package_generation_sha"),
                "c" * 40,
            ),
            (
                "package generation tag",
                ("campaign", "expected_package_generation_tag"),
                "unreviewed-generation",
            ),
            (
                "workflow",
                ("campaign", "expected_workflow_sha256"),
                "c" * 64,
            ),
            ("prior consumer", ("campaign", "prior_kandelo_sha"), "invalid"),
            (
                "formula set",
                ("campaign", "formulae"),
                list(rollout.FORMULA_ORDER[:-1]),
            ),
            (
                "architecture count",
                ("campaign", "architecture_identity_count"),
                69,
            ),
            (
                "reservation set",
                ("campaign", "reservations"),
                original["campaign"]["reservations"][:-1],
            ),
            (
                "absence set",
                ("campaign", "absent_oci_references"),
                absent,
            ),
            (
                "absence time",
                ("campaign", "absent_oci_checked_at"),
                "",
            ),
            (
                "previous catalog",
                ("previous_catalog",),
                {
                    formula: value
                    for formula, value in original["previous_catalog"].items()
                    if formula != "asa"
                },
            ),
            (
                "base catalog",
                ("base_catalog",),
                {
                    formula: value
                    for formula, value in original["base_catalog"].items()
                    if formula != "asa"
                },
            ),
            (
                "initial catalog",
                ("initial_catalog",),
                {
                    formula: value
                    for formula, value in original["initial_catalog"].items()
                    if formula != "asa"
                },
            ),
            (
                "previous support",
                ("previous_formula_support_tree",),
                "c" * 40,
            ),
            (
                "previous sidecars",
                ("previous_formula_sidecar_tree",),
                "not-a-tree",
            ),
            (
                "reserved catalog",
                ("catalog", "asa", "bottle_rebuild"),
                original["catalog"]["asa"]["bottle_rebuild"] + 1,
            ),
            (
                "top-level publisher",
                ("expected_publisher_sha",),
                "c" * 40,
            ),
            (
                "top-level workflow",
                ("workflow_sha256",),
                "c" * 64,
            ),
        )
        for label, path, value in mutations:
            changed = copy.deepcopy(original)
            target = changed
            for component in path[:-1]:
                target = target[component]
            target[path[-1]] = value
            with (
                self.subTest(field=label),
                self.assertRaises(rollout.RolloutError),
            ):
                rollout.validate_state(
                    changed, reservation, self.consumer_sha
                )

        extra = copy.deepcopy(original)
        extra["campaign"]["unreviewed"] = True
        with self.assertRaisesRegex(
            rollout.RolloutError, "unexpected shape"
        ):
            rollout.validate_state(extra, reservation, self.consumer_sha)

        stable_field_mutations = {
            "version": "999.0",
            "formula_revision": (
                original["base_catalog"]["asa"]["formula_revision"] + 1
            ),
            "arches": ["wasm32", "wasm64"],
            "dependencies": ["zlib"],
        }
        for field, value in stable_field_mutations.items():
            changed = copy.deepcopy(original)
            changed["base_catalog"]["asa"][field] = value
            with (
                self.subTest(stable_field=field),
                self.assertRaisesRegex(
                    rollout.RolloutError,
                    f"changes stable asa field {field}",
                ),
            ):
                rollout.validate_state(
                    changed,
                    reservation,
                    self.consumer_sha,
                )

    def test_fresh_campaign_rechecks_the_whole_selected_batch_before_dispatch(self):
        base, reservation = self._campaign_snapshots()
        state = self._campaign_state(base, reservation)
        statuses = tuple(
            rollout.FormulaStatus(
                name=formula,
                state="ready",
                arches=rollout.required_arches(formula),
                dependencies=(),
                detail="ready",
            )
            for formula in ("asa", "bc")
        )

        class OccupiedSecondRegistry:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def manifest(self, formula, reference):
                self.calls.append((formula, reference))
                if formula == "bc":
                    return rollout.RegistryManifestEvidence(
                        exists=True,
                        digest="sha256:" + "1" * 64,
                    )
                return rollout.RegistryManifestEvidence(
                    exists=False, digest=None
                )

        registry = OccupiedSecondRegistry()
        tap = mock.Mock()
        tap.main_without_fetch.return_value = reservation.sha
        tap.is_ancestor.return_value = True
        tap.changed_entries.return_value = (
            self._campaign_reservation_changes()
        )
        github = FakeGitHub()
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "campaign.json"
            rollout.write_state(state_path, state)
            original = state_path.read_bytes()
            with (
                mock.patch.object(
                    rollout,
                    "load_snapshot",
                    side_effect=lambda _tap, sha: (
                        base if sha == base.sha else reservation
                    ),
                ),
                mock.patch.object(
                    rollout, "finalization_reasons", return_value=("pending",)
                ),
                mock.patch.object(
                    rollout, "history_blocks_from_state", return_value={}
                ),
                mock.patch.object(
                    rollout, "calculate_statuses", return_value=statuses
                ),
                self.assertRaisesRegex(
                    rollout.RolloutError, "OCI identity is already occupied"
                ),
            ):
                rollout.dispatch_ready(
                    tap=tap,
                    github=github,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    no_fetch=True,
                    maximum=2,
                    timeout_seconds=1,
                    poll_seconds=0.001,
                    registry=registry,
                )
            self.assertEqual(original, state_path.read_bytes())

        self.assertEqual(
            ["asa", "bc"], [formula for formula, _reference in registry.calls]
        )

    def test_schema_two_campaign_recovers_an_accepted_request_after_client_crash(
        self,
    ):
        base, reservation = self._campaign_snapshots()
        state = self._campaign_state(base, reservation)
        first_token = "abi42-" + "a1" * 16
        second_token = "abi42-" + "b2" * 16
        for formula, token in (
            ("asa", first_token),
            ("bc", second_token),
        ):
            state["pending_dispatches"].append(
                {
                    "formula": formula,
                    "arches": list(rollout.required_arches(formula)),
                    "tap_sha": reservation.sha,
                    "dispatch_token": token,
                    "recorded_at": "2026-07-25T12:00:00Z",
                    "status": "planned",
                }
            )

        class CrashAfterAcceptGitHub(FakeGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.calls: list[tuple[str, str]] = []
                self.run_is_visible = False

            def runs(self, *, per_page=100, page=1, created=None):
                self.run_queries.append(
                    {
                        "per_page": per_page,
                        "page": page,
                        "created": created,
                    }
                )
                runs = (
                    [
                        RolloutControllerTests._run(
                            200,
                            reservation.sha,
                            display_title=rollout.workflow_run_title(
                                "asa",
                                first_token,
                            ),
                        )
                    ]
                    if self.run_is_visible
                    else []
                )
                start = (page - 1) * per_page
                return {
                    "total_count": len(runs),
                    "workflow_runs": runs[start : start + per_page],
                }

            def dispatch(self, formula, arches, tap_sha, dispatch_token):
                del arches
                self.calls.append((formula, dispatch_token))
                if tap_sha != reservation.sha:
                    raise AssertionError(f"unexpected tap SHA {tap_sha}")
                # Model GitHub accepting the request while the client receives
                # no trustworthy response. Recovery must correlate the durable
                # token instead of issuing this request again.
                raise rollout.RolloutError(
                    "ambiguous HTTP transport failure"
                )

        github = CrashAfterAcceptGitHub()
        registry = FakeRegistry(
            rollout.RegistryManifestEvidence(exists=False, digest=None)
        )
        tap = mock.Mock()
        tap.main_without_fetch.return_value = reservation.sha
        tap.is_ancestor.return_value = True
        tap.changed_entries.return_value = (
            self._campaign_reservation_changes()
        )
        snapshots = {
            base.sha: base,
            reservation.sha: reservation,
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "campaign.json"
            rollout.write_state(state_path, state)
            with (
                mock.patch.object(
                    rollout,
                    "load_snapshot",
                    side_effect=lambda _tap, sha: snapshots[sha],
                ),
                mock.patch.object(
                    rollout,
                    "finalization_reasons",
                    return_value=(),
                ),
                self.assertRaisesRegex(
                    rollout.RolloutError,
                    "ambiguous HTTP transport failure",
                ),
            ):
                rollout.dispatch_ready(
                    tap=tap,
                    github=github,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    no_fetch=True,
                    maximum=2,
                    timeout_seconds=1,
                    poll_seconds=0.001,
                    registry=registry,
                )

            retained = rollout.read_state(state_path)
            assert retained is not None
            self.assertEqual(
                ["request-started", "planned"],
                [
                    entry["status"]
                    for entry in retained["pending_dispatches"]
                ],
            )
            with (
                mock.patch.object(
                    rollout,
                    "load_snapshot",
                    side_effect=lambda _tap, sha: snapshots[sha],
                ),
                self.assertRaisesRegex(
                    rollout.RolloutError,
                    "recover them before continuing",
                ),
            ):
                rollout.dispatch_ready(
                    tap=tap,
                    github=github,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    no_fetch=True,
                    maximum=2,
                    timeout_seconds=1,
                    poll_seconds=0.001,
                    registry=registry,
                )

            github.run_is_visible = True
            with mock.patch.object(
                rollout,
                "load_snapshot",
                side_effect=lambda _tap, sha: snapshots[sha],
            ):
                recovered = rollout.recover_submitted_dispatch(
                    tap=tap,
                    github=github,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    no_fetch=True,
                )
            final = rollout.read_state(state_path)
            assert final is not None

        self.assertEqual((("asa", 200),), recovered)
        self.assertEqual([("asa", first_token)], github.calls)
        self.assertEqual(
            [
                ("asa", reservation.identities["asa"].top_reference),
                ("bc", reservation.identities["bc"].top_reference),
            ],
            registry.calls,
        )
        self.assertEqual(
            ["bc"],
            [entry["formula"] for entry in final["pending_dispatches"]],
        )
        self.assertEqual(
            ["asa"],
            [entry["formula"] for entry in final["dispatches"]],
        )
        self.assertEqual(
            first_token,
            final["dispatches"][0]["dispatch_token"],
        )

    def test_fresh_campaign_allowlist_must_be_dependency_closed(self):
        _base, reservation = self._campaign_snapshots()
        with self.assertRaisesRegex(
            rollout.RolloutError, "allowlist is not dependency-closed"
        ):
            rollout.require_dependency_closed_allowlist(
                reservation, frozenset(("python",))
            )
        rollout.require_dependency_closed_allowlist(
            reservation,
            frozenset(("python", "dash", "zlib")),
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

    def test_workflow_pins_one_main_and_one_admitted_rootfs_generation(self):
        expected = self.consumer_sha
        source = self.snapshot.workflow_source
        snapshot = self.snapshot
        rollout.validate_workflow(FakeGitHub(), snapshot, expected)
        mutations = (
            (
                "publisher implementation is not frozen",
                source.replace(rollout.PUBLISHER_WORKFLOW_SHA, "b" * 40),
            ),
            (
                "tap-ref is not an allowed",
                source.replace(
                    "${{ github.event.client_payload.tap_sha }}",
                    "main",
                ),
            ),
            (
                "run-name does not expose",
                source.replace(
                    rollout.WORKFLOW_RUN_NAME_SOURCE,
                    "Publish Kandelo bottles",
                ),
            ),
            (
                "package consumer is not frozen",
                source.replace(
                    f"kandelo-ref: {expected}", "kandelo-ref: main"
                ),
            ),
            (
                "workflow force differs",
                source.replace(
                    "github.event.client_payload.force || false", "true"
                ),
            ),
            (
                "package-generation-wasm32 differs",
                source.replace(
                    rollout.PREPUBLICATION_STAGING_TAG,
                    "package-generation-rootfs-wasm32-abi-v42-sha256-"
                    + "c" * 64,
                ),
            ),
            (
                "forbidden generation input prepublication-staging-tag",
                source
                + "\n      prepublication-staging-tag: pr-1079-staging\n",
            ),
        )
        for source_reason, changed_source in mutations:
            changed = dataclasses.replace(
                snapshot,
                workflow_source=changed_source,
            )
            with (
                self.subTest(source_reason=source_reason),
                self.assertRaisesRegex(rollout.RolloutError, source_reason),
            ):
                rollout.validate_workflow_source(
                    changed,
                    expected,
                    expected_publisher_sha=rollout.PUBLISHER_WORKFLOW_SHA,
                    expected_package_generation_sha=(
                        rollout.PREPUBLICATION_GENERATION_SHA
                    ),
                    expected_package_generation_tag=(
                        rollout.PREPUBLICATION_STAGING_TAG
                    ),
                )
            # WHY: source-shape diagnostics remain useful, but the production
            # gate must also reject every byte-level caller mutation before it
            # can become publication authority.
            with (
                self.subTest(approval_reason=source_reason),
                self.assertRaisesRegex(
                    rollout.RolloutError,
                    "publication workflow hash .* is not approved",
                ),
            ):
                rollout.validate_workflow(FakeGitHub(), changed, expected)

    def test_run_name_is_the_only_non_bottle_affecting_workflow_difference(self):
        current = self.snapshot.workflow_source
        legacy = current.replace(
            f"run-name: {rollout.WORKFLOW_RUN_NAME_SOURCE}\n",
            "",
            1,
        )
        self.assertEqual(
            rollout.publication_workflow_contract(legacy),
            rollout.publication_workflow_contract(current),
        )
        changed_payload = current.replace(
            "github.event.client_payload.formulae",
            "github.event.client_payload.other_formulae",
            1,
        )
        self.assertNotEqual(
            rollout.publication_workflow_contract(changed_payload),
            rollout.publication_workflow_contract(current),
        )
        changed_run_name = current.replace(
            rollout.WORKFLOW_RUN_NAME_SOURCE,
            "Publish Kandelo bottles",
            1,
        )
        self.assertNotEqual(
            rollout.publication_workflow_contract(changed_run_name),
            rollout.publication_workflow_contract(current),
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

    def test_legacy_single_intent_ledger_upgrades_only_from_exact_workflow(self):
        historical = dataclasses.replace(
            self.snapshot,
            workflow_source=self.precutover_workflow_source,
        )
        state = rollout.initial_state(
            historical,
            rollout.LEGACY_ABI42_CONSUMER_SHA,
        )
        state.pop("pending_dispatches")
        state.pop("expected_publisher_sha")
        state.pop("workflow_rotations")
        state["workflow_sha256"] = rollout.LEGACY_SINGLE_INTENT_WORKFLOW_SHA256

        upgraded = rollout.upgrade_state(
            state,
            historical,
            rollout.LEGACY_ABI42_CONSUMER_SHA,
        )

        self.assertEqual([], upgraded["pending_dispatches"])
        self.assertEqual(
            hashlib.sha256(historical.workflow_source.encode()).hexdigest(),
            upgraded["workflow_sha256"],
        )
        corrupted = copy.deepcopy(state)
        corrupted["workflow_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            rollout.RolloutError, "single-intent or token-correlated"
        ):
            rollout.upgrade_state(
                corrupted,
                historical,
                rollout.LEGACY_ABI42_CONSUMER_SHA,
            )

    def test_multi_intent_ledger_rejects_duplicate_formulae_and_tokens(self):
        first = "abi42-" + "1" * 32
        second = "abi42-" + "2" * 32
        state = self._token_state(
            ("asa", first, "planned"),
            ("bc", second, "submitted"),
        )
        rollout.validate_state(state, self.snapshot, self.consumer_sha)

        duplicate_formula = copy.deepcopy(state)
        duplicate_formula["pending_dispatches"][1]["formula"] = "asa"
        with self.assertRaisesRegex(
            rollout.RolloutError, "duplicate dispatch Formula"
        ):
            rollout.validate_state(
                duplicate_formula, self.snapshot, self.consumer_sha
            )

        duplicate_token = copy.deepcopy(state)
        duplicate_token["pending_dispatches"][1]["dispatch_token"] = first
        with self.assertRaisesRegex(
            rollout.RolloutError, "duplicate dispatch token"
        ):
            rollout.validate_state(
                duplicate_token, self.snapshot, self.consumer_sha
            )

    def test_pending_dispatch_status_shape_is_exact(self):
        token = "abi42-" + "3" * 32
        state = self._token_state(("asa", token, "request-started"))
        state["pending_dispatches"][0]["submitted_at"] = (
            "2026-07-24T07:00:02Z"
        )
        with self.assertRaisesRegex(
            rollout.RolloutError, "malformed pending dispatch"
        ):
            rollout.validate_state(state, self.snapshot, self.consumer_sha)

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
        self.assertEqual(2, len(github.run_queries))
        self.assertTrue(
            all(query["created"] is None for query in github.run_queries)
        )

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
        with (
            mock.patch.object(rollout.time, "sleep") as sleep,
            self.assertRaisesRegex(
                rollout.RolloutError, "incomplete workflow run page 1"
            ),
        ):
            rollout.active_inventory(github)
        self.assertEqual(
            [
                mock.call(rollout.WORKFLOW_RUN_SNAPSHOT_BACKOFF_SECONDS),
                mock.call(rollout.WORKFLOW_RUN_SNAPSHOT_BACKOFF_SECONDS * 2),
            ],
            sleep.call_args_list,
        )

    def test_active_inventory_retries_a_transient_count_page_mismatch(self):
        class EventuallyConsistentGitHub(FakeGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.queued_calls = 0

            def runs(self, *, per_page=100, page=1, created=None):
                del created
                self.queued_calls += 1
                runs = (
                    []
                    if self.queued_calls == 1
                    else [{"id": 123, "status": "queued"}]
                )
                start = (page - 1) * per_page
                return {
                    "total_count": 1,
                    "workflow_runs": runs[start : start + per_page],
                }

        github = EventuallyConsistentGitHub()
        github.jobs_by_run[123] = self._matrix_jobs("asa", "wasm32")
        with mock.patch.object(rollout.time, "sleep") as sleep:
            inventory = rollout.active_inventory(github)

        self.assertEqual(1, inventory.count)
        self.assertEqual(frozenset(("asa",)), inventory.formulae[123])
        self.assertEqual(3, github.queued_calls)
        sleep.assert_called_once_with(
            rollout.WORKFLOW_RUN_SNAPSHOT_BACKOFF_SECONDS
        )

    def test_active_inventory_does_not_retry_malformed_run_identity(self):
        github = FakeGitHub()
        github.by_status["queued"] = {
            "total_count": 1,
            "workflow_runs": [{"id": "123", "status": "queued"}],
        }
        with (
            mock.patch.object(rollout.time, "sleep") as sleep,
            self.assertRaisesRegex(
                rollout.RolloutError, "malformed workflow run"
            ),
        ):
            rollout.active_inventory(github)
        sleep.assert_not_called()

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

    def test_anonymous_registry_streams_exact_blob_without_leaking_bearer(self):
        body = b"public-bottle-bytes"
        digest = hashlib.sha256(body).hexdigest()
        blob_url = (
            f"{rollout.BOTTLE_ROOT}/bzip2/blobs/sha256:{digest}"
        )
        storage_url = (
            "https://pkg-containers.githubusercontent.com/ghcrblobs09/blobs/"
            f"sha256:{digest}?sig=short-lived"
        )
        requests = []

        def opener(request, timeout):
            self.assertEqual(30, timeout)
            requests.append(request)
            if len(requests) == 1:
                return FakeHttpResponse(
                    url=request.full_url,
                    body=b'{"token":"anonymous-read-token"}',
                )
            if len(requests) == 2:
                raise urllib.error.HTTPError(
                    blob_url,
                    307,
                    "Temporary Redirect",
                    {"Location": storage_url},
                    None,
                )
            return FakeHttpResponse(
                url=storage_url,
                body=body,
                headers={"Content-Length": str(len(body))},
            )

        rollout.AnonymousRegistry(opener=opener).verify_blob(
            "bzip2", digest, len(body)
        )

        self.assertEqual(3, len(requests))
        self.assertIsNone(requests[0].get_header("Authorization"))
        self.assertEqual(
            "Bearer anonymous-read-token",
            requests[1].get_header("Authorization"),
        )
        self.assertEqual(blob_url, requests[1].full_url)
        self.assertEqual(storage_url, requests[2].full_url)
        self.assertIsNone(requests[2].get_header("Authorization"))

    def test_anonymous_registry_rejects_blob_redirect_digest_and_size_drift(self):
        body = b"public-bottle-bytes"
        digest = hashlib.sha256(body).hexdigest()
        blob_url = (
            f"{rollout.BOTTLE_ROOT}/bzip2/blobs/sha256:{digest}"
        )

        def registry_for(*, host="pkg-containers.githubusercontent.com", payload=body):
            calls = 0
            storage_url = (
                f"https://{host}/ghcrblobs09/blobs/sha256:{digest}"
                "?sig=short-lived"
            )

            def opener(request, timeout):
                nonlocal calls
                del timeout
                calls += 1
                if calls == 1:
                    return FakeHttpResponse(
                        url=request.full_url,
                        body=b'{"token":"anonymous-read-token"}',
                    )
                if calls == 2:
                    raise urllib.error.HTTPError(
                        blob_url,
                        307,
                        "Temporary Redirect",
                        {"Location": storage_url},
                        None,
                    )
                return FakeHttpResponse(
                    url=storage_url,
                    body=payload,
                    headers={"Content-Length": str(len(payload))},
                )

            return rollout.AnonymousRegistry(opener=opener)

        with self.assertRaisesRegex(
            rollout.RolloutError, "outside approved storage"
        ):
            registry_for(host="example.com").verify_blob(
                "bzip2", digest, len(body)
            )
        with self.assertRaisesRegex(
            rollout.RolloutError, "size differs from authority"
        ):
            registry_for().verify_blob("bzip2", digest, len(body) + 1)
        with self.assertRaisesRegex(
            rollout.RolloutError, "digest differs from authority"
        ):
            registry_for(payload=b"changed-bottle-byte").verify_blob(
                "bzip2", digest, len(b"changed-bottle-byte")
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
                count=8,
                runs=({"id": 123, "status": "in_progress"},),
                formulae={123: frozenset()},
                unknown_run_ids=(123,),
            ),
            {
                "dispatches": [
                    {"formula": "asa", "run_id": 123},
                ]
            },
        )
        self.assertEqual(8, inventory.count)
        self.assertEqual(frozenset(("asa",)), inventory.formulae[123])
        self.assertEqual((), inventory.unknown_run_ids)

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

    def test_failed_token_run_uses_its_recorded_caller_not_bottle_source(self):
        old_source = rollout.source_with_rebuild(
            self.snapshot.formula_sources["sqlite"], "sqlite", 1
        )
        source = self._snapshot_with_formula_source(
            self.snapshot, "sqlite", old_source, sha="a" * 40
        )
        caller = dataclasses.replace(source, sha="b" * 40)
        current = self._snapshot_with_formula_source(
            source,
            "sqlite",
            rollout.source_with_rebuild(old_source, "sqlite", 2),
            sha="c" * 40,
        )
        state = self._failed_state(source, "sqlite")
        state["dispatches"][0].update(
            {
                "caller_tap_sha": caller.sha,
                "dispatch_token": "abi42-" + "1" * 32,
            }
        )
        github = FakeGitHub()
        github.runs_by_id[123] = self._run(
            123,
            caller.sha,
            status="completed",
            conclusion="failure",
        )
        github.jobs_by_run[123] = self._matrix_jobs(
            "sqlite", "wasm32", "wasm64"
        )

        result, recovered = self._recover_failed(
            github=github,
            registry=FakeRegistry(
                rollout.RegistryManifestEvidence(
                    exists=True,
                    digest="sha256:" + "d" * 64,
                )
            ),
            state=state,
            source_snapshot=source,
            current_snapshot=current,
            additional_source_snapshots=(caller,),
        )

        self.assertEqual("sqlite", result[0])
        self.assertEqual(123, result[1])
        self.assertEqual([], recovered["dispatches"])

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
        current = dataclasses.replace(
            self.snapshot,
            sha="c" * 40,
            workflow_source=self.precutover_workflow_source,
        )
        state = self._failed_state(
            source,
            "make",
            run_id=123,
            consumer_sha=rollout.LEGACY_ABI42_CONSUMER_SHA,
        )
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
            publisher_sha=rollout.LEGACY_PUBLISHER_WORKFLOW_SHA,
            consumer_sha=rollout.LEGACY_PUBLISHER_WORKFLOW_SHA,
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
            rollout.LEGACY_PUBLISHER_WORKFLOW_SHA,
            explicit["logged_publisher_sha"],
        )
        self.assertEqual(
            rollout.LEGACY_PUBLISHER_WORKFLOW_SHA,
            explicit["logged_kandelo_ref"],
        )
        self.assertNotIn(
            explicit["source_workflow_sha256"],
            rollout.trusted_workflow_publishers(recovered),
        )
        rollout.validate_state(
            recovered,
            current,
            rollout.LEGACY_ABI42_CONSUMER_SHA,
        )

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
                rollout.LEGACY_PUBLISHER_WORKFLOW_SHA,
                rollout.LEGACY_PUBLISHER_WORKFLOW_SHA,
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
            dataclasses.replace(
                self.snapshot,
                workflow_source=self.precutover_workflow_source,
            ),
            "sqlite",
            rollout.source_with_rebuild(old_source, "sqlite", 2),
            sha="c" * 40,
        )
        state = self._failed_state(
            source,
            "sqlite",
            consumer_sha=rollout.LEGACY_ABI42_CONSUMER_SHA,
        )
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

        self.assertEqual(
            rollout.LEGACY_ABI42_CONSUMER_SHA,
            recovered["expected_kandelo_sha"],
        )
        self.assertEqual(
            rollout.LEGACY_PUBLISHER_WORKFLOW_SHA,
            recovered["expected_publisher_sha"],
        )
        self.assertEqual(1, len(recovered["workflow_rotations"]))
        rotation = recovered["workflow_rotations"][0]
        self.assertEqual(
            rollout.LEGACY_ABI42_CONSUMER_SHA,
            rotation["old_publisher_sha"],
        )
        self.assertEqual(
            rollout.LEGACY_PUBLISHER_WORKFLOW_SHA,
            rotation["new_publisher_sha"],
        )
        rollout.validate_state(
            recovered,
            current,
            rollout.LEGACY_ABI42_CONSUMER_SHA,
        )

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
            for index, formula in enumerate(rollout.FORMULA_ORDER, start=1):
                rollout.GitHub().dispatch(
                    formula,
                    rollout.required_arches(formula),
                    self.head,
                    f"abi42-{index:032x}",
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
        self.assertRegex(
            zlib_payload["client_payload"]["dispatch_token"],
            rollout.DISPATCH_TOKEN_RE,
        )
        self.assertNotIn("rerun", json.dumps(calls).lower())

    def test_formula_allowlist_controls_the_actual_dispatch_selection(self):
        class RecordingGitHub(FakeGitHub):
            def __init__(self):
                super().__init__()
                self.dispatches = []

            def dispatch(self, formula, arches, tap_sha, dispatch_token):
                self.dispatches.append((formula, tuple(arches), tap_sha))
                self.by_status[None] = {
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 123,
                            "event": "repository_dispatch",
                            "head_sha": tap_sha,
                            "status": "queued",
                            "run_attempt": 1,
                            "display_title": rollout.workflow_run_title(
                                formula, dispatch_token
                            ),
                        }
                    ],
                }

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
                        "2026-07-24T22:00:02Z",
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
            rollout.GitHub().dispatch(
                "zlib",
                ("wasm32",),
                "main",
                "abi42-" + "0" * 32,
            )

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

    def test_token_correlation_does_not_wait_for_workflow_jobs(self):
        token = "abi42-" + "4" * 32
        state = self._token_state(("asa", token, "submitted"))
        github = self._candidate_github(
            self._run(
                123,
                self.head,
                display_title=rollout.workflow_run_title("asa", token),
            )
        )

        updated, recovered = rollout.correlate_pending_dispatches(
            self.tap, github, state
        )

        self.assertEqual((("asa", 123),), recovered)
        self.assertEqual([], updated["pending_dispatches"])
        self.assertEqual(token, updated["dispatches"][-1]["dispatch_token"])
        self.assertEqual(self.head, updated["dispatches"][-1]["caller_tap_sha"])
        self.assertEqual({}, github.jobs_by_run)

    def test_batch_accepts_a_finalizer_only_source_advance_between_posts(self):
        first_token = "abi42-" + "a1" * 16
        second_token = "abi42-" + "b2" * 16
        state = self._token_state(
            ("asa", first_token, "submitted"),
            ("bc", second_token, "submitted"),
        )
        with self._descendant_tap(
            {
                "Kandelo/reports/test-finalizer.provenance.json": (
                    '{"schema":1,"status":"success"}\n'
                ),
            }
        ) as (tap, descendant):
            github = self._candidate_github(
                self._run(
                    123,
                    self.head,
                    display_title=rollout.workflow_run_title("asa", first_token),
                ),
                self._run(
                    124,
                    descendant,
                    display_title=rollout.workflow_run_title("bc", second_token),
                ),
            )

            updated, recovered = rollout.correlate_pending_dispatches(
                tap, github, state
            )

        self.assertEqual((("asa", 123), ("bc", 124)), recovered)
        self.assertEqual(
            [self.head, descendant],
            [entry["caller_tap_sha"] for entry in updated["dispatches"]],
        )
        self.assertTrue(
            all(entry["tap_sha"] == self.head for entry in updated["dispatches"])
        )

    def test_batch_rejects_recipe_support_and_workflow_drift(self):
        token = "abi42-" + "c3" * 16
        state = self._token_state(("asa", token, "submitted"))
        changes = (
            (
                "recipe",
                {
                    "Formula/asa.rb": lambda source: source.replace(
                        "class Asa < Formula\n",
                        'class Asa < Formula\n  desc "drifted recipe"\n',
                        1,
                    )
                },
                "changes a Formula recipe",
            ),
            (
                "support",
                {
                    "Kandelo/formula_support/kandelo_formula_support.rb": (
                        lambda source: source + "\n# drifted support\n"
                    )
                },
                "changes Formula support",
            ),
            (
                "workflow",
                {
                    rollout.WORKFLOW_PATH: lambda source: source.replace(
                        "github.event.client_payload.force || false",
                        "true",
                        1,
                    )
                },
                "changes the normalized publication workflow",
            ),
            (
                "unrelated-path",
                {"README.md": lambda source: source + "\nunrelated drift\n"},
                "not a finalizer-only descendant",
            ),
        )
        for label, change, message in changes:
            with self.subTest(label=label), self._descendant_tap(change) as (
                tap,
                descendant,
            ):
                github = self._candidate_github(
                    self._run(
                        123,
                        descendant,
                        display_title=rollout.workflow_run_title("asa", token),
                    )
                )
                with self.assertRaisesRegex(rollout.RolloutError, message):
                    rollout.correlate_pending_dispatches(tap, github, state)

    def test_finalizer_source_path_policy_is_exact_and_auditable(self):
        accepted = (
            ("M", "Formula/asa.rb"),
            ("M", "Formula/git.rb"),
            ("M", "Kandelo/metadata.json"),
            ("A", "Kandelo/formula/asa.json"),
            ("M", "Kandelo/formula/git.json"),
            ("A", "Kandelo/link/asa-15.0.0-rebuild1-wasm32.json"),
            ("M", "Kandelo/reports/asa-15.0.0.provenance.json"),
            ("A", "Kandelo/reports/failures/run-123-attempt-1.json"),
            ("M", "Kandelo/reports/rollbacks/run-123.json"),
        )
        rejected = (
            ("D", "Formula/asa.rb"),
            ("R", "Kandelo/formula/asa.json"),
            ("A", "Formula/not-in-catalog.rb"),
            ("M", "Formula/asa.rb.bak"),
            ("A", "Kandelo/formula/not-in-catalog.json"),
            ("M", "Kandelo/formula/asa.json.bak"),
            ("A", "Kandelo/link/subdirectory/asa.json"),
            ("M", "Kandelo/link/asa.json.bak"),
            ("A", "Kandelo/reports/../escaped.json"),
            ("M", "Kandelo/reports/report.txt"),
            ("A", "Kandelo/formula_support/generated.json"),
            ("M", "README.md"),
        )
        for status, path in accepted:
            with self.subTest(status=status, path=path):
                self.assertTrue(rollout.finalizer_owned_change(status, path))
        for status, path in rejected:
            with self.subTest(status=status, path=path):
                self.assertFalse(rollout.finalizer_owned_change(status, path))

    def test_token_correlation_rejects_a_rerun_attempt(self):
        token = "abi42-" + "4a" * 16
        title = rollout.workflow_run_title("asa", token)
        for run_attempt in (None, True, 2):
            with self.subTest(run_attempt=run_attempt):
                state = self._token_state(("asa", token, "submitted"))
                github = self._candidate_github(
                    self._run(
                        123,
                        self.head,
                        run_attempt=run_attempt,
                        display_title=title,
                    )
                )
                with self.assertRaisesRegex(
                    rollout.RolloutError,
                    "is a rerun; only attempt 1 is eligible",
                ):
                    rollout.correlate_pending_dispatches(
                        self.tap,
                        github,
                        state,
                    )
                self.assertEqual(1, len(state["pending_dispatches"]))
                self.assertEqual([], state["dispatches"])

    def test_token_correlation_requires_exact_event_head_formula_and_token(self):
        token = "abi42-" + "5" * 32
        state = self._token_state(("asa", token, "submitted"))
        title = rollout.workflow_run_title("asa", token)
        github = self._candidate_github(
            self._run(120, self.head, event="push", display_title=title),
            self._run(
                122,
                self.head,
                display_title=rollout.workflow_run_title("bc", token),
            ),
            self._run(
                123,
                self.head,
                display_title=rollout.workflow_run_title(
                    "asa", "abi42-" + "6" * 32
                ),
            ),
        )

        updated, recovered = rollout.correlate_pending_dispatches(
            self.tap, github, state
        )

        self.assertEqual((), recovered)
        self.assertEqual(state, updated)

    def test_duplicate_token_runs_fail_without_mutating_the_ledger(self):
        token = "abi42-" + "7" * 32
        state = self._token_state(("asa", token, "submitted"))
        title = rollout.workflow_run_title("asa", token)
        github = self._candidate_github(
            self._run(123, self.head, display_title=title),
            self._run(124, self.head, display_title=title),
        )

        with self.assertRaisesRegex(
            rollout.RolloutError, "matched multiple runs"
        ):
            rollout.correlate_pending_dispatches(self.tap, github, state)

        self.assertEqual(1, len(state["pending_dispatches"]))
        self.assertEqual([], state["dispatches"])

    def test_token_recovery_paginates_only_its_request_time_range(self):
        token = "abi42-" + "7a" * 16
        state = self._token_state(("asa", token, "submitted"))
        irrelevant = tuple(
            self._run(run_id, self.head)
            for run_id in range(1, 101)
        )
        github = self._candidate_github(
            *irrelevant,
            self._run(
                101,
                self.head,
                display_title=rollout.workflow_run_title("asa", token),
            ),
        )

        updated, recovered = rollout.correlate_pending_dispatches(
            self.tap, github, state
        )

        self.assertEqual((("asa", 101),), recovered)
        self.assertEqual([], updated["pending_dispatches"])
        self.assertEqual(
            [1, 2, 1, 2],
            [query["page"] for query in github.run_queries],
        )
        self.assertEqual(
            {
                "2026-07-24T06:55:01Z..2026-07-24T07:05:02Z",
            },
            {query["created"] for query in github.run_queries},
        )

    def test_workflow_run_snapshot_retries_a_torn_whole_listing(self):
        class MovingGitHub(FakeGitHub):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def runs(self, *, per_page=100, page=1, created=None):
                del per_page, page, created
                self.calls += 1
                status = "queued" if self.calls == 1 else "in_progress"
                return {
                    "total_count": 1,
                    "workflow_runs": [{"id": 123, "status": status}],
                }

        github = MovingGitHub()
        with mock.patch.object(rollout.time, "sleep") as sleep:
            runs = rollout.workflow_run_snapshot(github)

        self.assertEqual("in_progress", runs[0]["status"])
        self.assertEqual(4, github.calls)
        sleep.assert_called_once_with(
            rollout.WORKFLOW_RUN_SNAPSHOT_BACKOFF_SECONDS
        )

    def test_workflow_run_snapshot_rejects_duplicates_across_pages(self):
        class DuplicateGitHub(FakeGitHub):
            def runs(self, *, per_page=100, page=1, created=None):
                del created
                if page == 1:
                    runs = [{"id": run_id} for run_id in range(1, 101)]
                else:
                    runs = [{"id": 100}]
                return {
                    "total_count": 101,
                    "workflow_runs": runs[:per_page],
                }

        with (
            mock.patch.object(rollout.time, "sleep") as sleep,
            self.assertRaisesRegex(
                rollout.RolloutError,
                "duplicate workflow runs across pages",
            ),
        ):
            rollout.workflow_run_snapshot(DuplicateGitHub())
        self.assertEqual(2, sleep.call_count)

    def test_recovery_records_visible_tokens_and_retains_late_independent_intents(self):
        asa_token = "abi42-" + "8" * 32
        bc_token = "abi42-" + "9" * 32
        state = self._token_state(
            ("asa", asa_token, "request-started"),
            ("bc", bc_token, "submitted"),
        )
        github = self._candidate_github(
            self._run(
                123,
                self.head,
                display_title=rollout.workflow_run_title("asa", asa_token),
            )
        )

        result, recovered = self._recover(github, state)

        self.assertEqual((("asa", 123),), result)
        self.assertEqual(
            ["bc"],
            [entry["formula"] for entry in recovered["pending_dispatches"]],
        )
        self.assertEqual(
            "2026-07-24T07:00:01Z",
            recovered["dispatches"][-1]["submitted_at"],
        )

    def test_dispatch_ready_submits_a_batch_before_one_shared_acknowledgement(self):
        tokens = tuple(f"abi42-{value:032x}" for value in (10, 11, 12))
        events: list[tuple[str, object]] = []

        class BatchGitHub(FakeGitHub):
            def __init__(self, head: str) -> None:
                super().__init__()
                self.head = head
                self.created_runs: list[dict] = []

            def runs(self, *, per_page=100, page=1, created=None):
                del created
                events.append(("runs-page", None))
                start = (page - 1) * per_page
                return {
                    "total_count": len(self.created_runs),
                    "workflow_runs": list(self.created_runs)[
                        start : start + per_page
                    ],
                }

            def dispatch(self, formula, arches, tap_sha, dispatch_token):
                if tap_sha != self.head:
                    raise AssertionError(f"unexpected tap SHA {tap_sha}")
                events.append(("dispatch", formula))
                run_id = 200 + len(self.created_runs)
                self.created_runs.append(
                    {
                        "id": run_id,
                        "event": "repository_dispatch",
                        "head_sha": self.head,
                        "status": "queued",
                        "run_attempt": 1,
                        "display_title": rollout.workflow_run_title(
                            formula, dispatch_token
                        ),
                    }
                )

            def jobs(self, run_id):
                raise AssertionError(
                    f"token acknowledgement must not inspect jobs for {run_id}"
                )

        ready = tuple(
            rollout.FormulaStatus(
                name=formula,
                state="ready",
                arches=rollout.required_arches(formula),
                dependencies=(),
                detail="ready",
            )
            for formula in ("bc", "binutils", "bzip2")
        )
        github = BatchGitHub(self.head)
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
                    rollout, "finalization_reasons", return_value=("pending",)
                ),
                mock.patch.object(
                    rollout, "calculate_statuses", return_value=ready
                ),
                mock.patch.object(
                    rollout, "new_dispatch_token", side_effect=tokens
                ),
            ):
                count = rollout.dispatch_ready(
                    tap=self.tap,
                    github=github,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    no_fetch=True,
                    maximum=3,
                    timeout_seconds=1,
                    poll_seconds=0.001,
                )
            state = rollout.read_state(state_path)
            assert state is not None

        self.assertEqual(3, count)
        self.assertEqual(
            [
                ("runs-page", None),
                ("runs-page", None),
                ("runs-page", None),
                ("runs-page", None),
                ("dispatch", "bc"),
                ("dispatch", "binutils"),
                ("dispatch", "bzip2"),
                ("runs-page", None),
                ("runs-page", None),
            ],
            events,
        )
        self.assertEqual([], state["pending_dispatches"])
        self.assertEqual(
            ["bc", "binutils", "bzip2"],
            [entry["formula"] for entry in state["dispatches"]],
        )

    def test_resumed_batch_drops_only_plans_superseded_by_an_active_run(self):
        bc_token = "abi42-" + "c" * 32
        binutils_token = "abi42-" + "d" * 32
        state = self._token_state(
            ("bc", bc_token, "planned"),
            ("binutils", binutils_token, "planned"),
        )

        class CollisionGitHub(FakeGitHub):
            def __init__(self, head: str) -> None:
                super().__init__()
                self.head = head
                self.dispatched: list[str] = []
                self.created_runs: list[dict] = []

            def runs(self, *, per_page=100, page=1, created=None):
                del created
                active = {
                    "id": 150,
                    "event": "repository_dispatch",
                    "head_sha": self.head,
                    "status": "queued",
                }
                runs = [active, *self.created_runs]
                start = (page - 1) * per_page
                return {
                    "total_count": len(runs),
                    "workflow_runs": runs[start : start + per_page],
                }

            def dispatch(self, formula, arches, tap_sha, dispatch_token):
                del arches
                if tap_sha != self.head:
                    raise AssertionError(f"unexpected tap SHA {tap_sha}")
                self.dispatched.append(formula)
                self.created_runs.append(
                    {
                        "id": 200,
                        "event": "repository_dispatch",
                        "head_sha": self.head,
                        "status": "queued",
                        "run_attempt": 1,
                        "display_title": rollout.workflow_run_title(
                            formula, dispatch_token
                        ),
                    }
                )

        github = CollisionGitHub(self.head)
        github.jobs_by_run[150] = self._matrix_jobs("bc", "wasm32")
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)
            with mock.patch.object(
                self.tap, "main_without_fetch", return_value=self.head
            ):
                count = rollout.dispatch_ready(
                    tap=self.tap,
                    github=github,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    no_fetch=True,
                    maximum=2,
                    timeout_seconds=1,
                    poll_seconds=0.001,
                )
            recovered = rollout.read_state(state_path)
            assert recovered is not None

        self.assertEqual(1, count)
        self.assertEqual(["binutils"], github.dispatched)
        self.assertEqual([], recovered["pending_dispatches"])
        self.assertEqual(
            ["binutils"],
            [entry["formula"] for entry in recovered["dispatches"]],
        )

    def test_uncertain_http_request_blocks_retry_but_preserves_later_plans(self):
        first_token = "abi42-" + "d" * 32
        second_token = "abi42-" + "e" * 32
        state = self._token_state(
            ("bc", first_token, "planned"),
            ("binutils", second_token, "planned"),
        )

        class FailingGitHub(FakeGitHub):
            def __init__(self) -> None:
                super().__init__()
                self.head = RolloutControllerTests.head
                self.calls: list[tuple[str, str]] = []

            def dispatch(self, formula, arches, tap_sha, dispatch_token):
                del arches
                if tap_sha != self.head:
                    raise AssertionError(f"unexpected tap SHA {tap_sha}")
                self.calls.append((formula, dispatch_token))
                raise rollout.RolloutError("ambiguous HTTP transport failure")

        github = FailingGitHub()
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)
            with (
                mock.patch.object(
                    self.tap, "main_without_fetch", return_value=self.head
                ),
                self.assertRaisesRegex(
                    rollout.RolloutError, "ambiguous HTTP transport failure"
                ),
            ):
                rollout.dispatch_ready(
                    tap=self.tap,
                    github=github,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    no_fetch=True,
                    maximum=2,
                    timeout_seconds=1,
                    poll_seconds=0.001,
                )
            retained = rollout.read_state(state_path)
            assert retained is not None
            self.assertEqual(
                ["request-started", "planned"],
                [
                    entry["status"]
                    for entry in retained["pending_dispatches"]
                ],
            )
            with (
                mock.patch.object(
                    self.tap, "main_without_fetch", return_value=self.head
                ),
                self.assertRaisesRegex(
                    rollout.RolloutError,
                    "recover them before continuing",
                ),
            ):
                rollout.dispatch_ready(
                    tap=self.tap,
                    github=github,
                    expected_kandelo_sha=self.consumer_sha,
                    state_path=state_path,
                    no_fetch=True,
                    maximum=2,
                    timeout_seconds=1,
                    poll_seconds=0.001,
                )
        self.assertEqual([("bc", first_token)], github.calls)

    def test_shared_ack_timeout_retains_every_submitted_token(self):
        first_token = "abi42-" + "a" * 32
        second_token = "abi42-" + "b" * 32
        state = self._token_state(
            ("bc", first_token, "submitted"),
            ("binutils", second_token, "submitted"),
        )
        github = self._candidate_github()
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "rollout.json"
            rollout.write_state(state_path, state)
            original = state_path.read_bytes()
            with self.assertRaisesRegex(
                rollout.RolloutError,
                "bc, binutils",
            ):
                rollout.acknowledge_pending_dispatches(
                    tap=self.tap,
                    github=github,
                    state=state,
                    state_path=state_path,
                    snapshot=self.snapshot,
                    expected_kandelo_sha=self.consumer_sha,
                    timeout_seconds=0,
                    poll_seconds=0.001,
                )
            self.assertEqual(original, state_path.read_bytes())

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

        self.assertEqual((("asa", 123),), result)
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

        self.assertEqual((("asa", 123),), result)
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

    def test_state_write_persists_file_then_rename_then_parent_directory(self):
        events: list[str] = []
        real_fsync = rollout.os.fsync
        real_replace = rollout.os.replace

        def fsync(descriptor):
            events.append("fsync")
            return real_fsync(descriptor)

        def replace(source, destination):
            events.append("replace")
            return real_replace(source, destination)

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(rollout.os, "fsync", side_effect=fsync),
            mock.patch.object(rollout.os, "replace", side_effect=replace),
        ):
            rollout.write_state(
                pathlib.Path(directory) / "rollout.json",
                {"schema": 1},
            )

        self.assertEqual(["fsync", "replace", "fsync"], events)

    def test_fresh_state_creation_never_replaces_a_racing_ledger(self):
        competing = b'{"schema":2,"owner":"another controller"}\n'
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "campaign.json"

            def race(_temporary, destination):
                pathlib.Path(destination).write_bytes(competing)
                raise FileExistsError(destination)

            with (
                mock.patch.object(rollout.os, "link", side_effect=race),
                self.assertRaisesRegex(
                    rollout.RolloutError, "appeared during initialization"
                ),
            ):
                rollout.write_new_state(
                    path,
                    {"schema": 2, "owner": "this controller"},
                )

            self.assertEqual(competing, path.read_bytes())
            self.assertEqual(
                [path],
                list(path.parent.iterdir()),
                "the losing controller must clean up only its temporary file",
            )

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

    def test_cli_requires_the_complete_campaign_contract_only_for_initialization(self):
        common = (
            "--tap-root",
            str(self.root),
            "--expected-kandelo-sha",
            self.consumer_sha,
        )
        options = (
            ("--campaign-id", rollout.CAMPAIGN_MANIFEST_ID),
            ("--campaign-base-tap-sha", "a" * 40),
            ("--campaign-reservation-tap-sha", "b" * 40),
            ("--campaign-manifest-tap-sha", "c" * 40),
            ("--expected-publisher-sha", rollout.PUBLISHER_WORKFLOW_SHA),
            (
                "--expected-package-generation-sha",
                rollout.PREPUBLICATION_GENERATION_SHA,
            ),
            (
                "--expected-package-generation-tag",
                rollout.PREPUBLICATION_STAGING_TAG,
            ),
            (
                "--expected-workflow-sha256",
                rollout.workflow_sha256(self.snapshot),
            ),
        )
        campaign_args = tuple(
            value for option in options for value in option
        )
        valid = rollout.parse_args(
            (
                *common,
                "--state-file",
                "/tmp/fresh-campaign.json",
                "--initialize-campaign",
                *campaign_args,
            )
        )
        self.assertTrue(valid.initialize_campaign)
        self.assertEqual(
            rollout.CAMPAIGN_MANIFEST_ID, valid.campaign_id
        )
        self.assertIsNone(valid.campaign_selection)

        selection = self._product_first_selection()
        selection_args = (
            "--campaign-rebuild-formulae",
            ",".join(selection.rebuild),
            "--campaign-reuse-formulae",
            ",".join(selection.reuse),
            "--campaign-deferred-formulae",
            ",".join(selection.deferred),
        )
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            rollout.parse_args(
                (
                    *common,
                    "--state-file",
                    "/tmp/fresh-campaign.json",
                    "--initialize-campaign",
                    *campaign_args,
                    *selection_args,
                )
            )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rollout.parse_args(
                (
                    *common,
                    "--campaign-rebuild-formulae",
                    "bash",
                    "--campaign-reuse-formulae",
                    "libcxx,ncurses",
                    "--campaign-deferred-formulae",
                    ",".join(selection.deferred),
                )
            )

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rollout.parse_args(
                (*common, "--initialize-campaign", *campaign_args)
            )

        for omitted_option, _omitted_value in options:
            retained = tuple(
                value
                for option in options
                if option[0] != omitted_option
                for value in option
            )
            with (
                self.subTest(missing=omitted_option),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                rollout.parse_args(
                    (
                        *common,
                        "--state-file",
                        "/tmp/fresh-campaign.json",
                        "--initialize-campaign",
                        *retained,
                    )
                )

        for option, value in options:
            with (
                self.subTest(without_action=option),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                rollout.parse_args((*common, option, value))

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rollout.parse_args(
                (
                    *common,
                    "--state-file",
                    "/tmp/fresh-campaign.json",
                    "--initialize-campaign",
                    *campaign_args,
                    "--dispatch",
                )
            )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rollout.parse_args(
                (
                    *common,
                    "--state-file",
                    "/tmp/fresh-campaign.json",
                    "--initialize-campaign",
                    *campaign_args,
                    "--formulae",
                    "asa",
                )
            )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rollout.parse_args(
                (
                    *common,
                    "--state-file",
                    "/tmp/fresh-campaign.json",
                    "--initialize-campaign",
                    *campaign_args,
                    "--adopt-failed-run",
                    "asa=123",
                )
            )

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
