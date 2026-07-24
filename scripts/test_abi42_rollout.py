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
        source = f"""\
jobs:
  publish:
    uses: Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@{expected}
    with:
      kandelo-ref: {expected}
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
            "version": identity.version,
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

    def test_unknown_active_formula_is_reported_conservatively(self):
        github = FakeGitHub()
        github.by_status["queued"] = {
            "total_count": 1,
            "workflow_runs": [{"id": 123, "status": "queued"}],
        }
        github.jobs_by_run[123] = ({"name": "publish / plan"},)
        inventory = rollout.active_inventory(github)
        self.assertEqual((123,), inventory.unknown_run_ids)

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

    def test_python_dispatch_is_single_formula_and_requires_vfs_acceptance(self):
        calls = []

        def capture(argv, **kwargs):
            calls.append((argv, kwargs))
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(rollout, "_run", side_effect=capture):
            rollout.GitHub().dispatch("python", ("wasm32",))
            rollout.GitHub().dispatch("zlib", ("wasm32", "wasm64"))
        python_payload = json.loads(calls[0][1]["input_text"])
        zlib_payload = json.loads(calls[1][1]["input_text"])
        self.assertEqual("python", python_payload["client_payload"]["formulae"])
        self.assertIs(True, python_payload["client_payload"]["require_vfs_acceptance"])
        self.assertEqual(
            "wasm32,wasm64", zlib_payload["client_payload"]["arches"]
        )
        self.assertNotIn(
            "require_vfs_acceptance", zlib_payload["client_payload"]
        )
        self.assertNotIn("rerun", json.dumps((calls[0][0], calls[1][0])).lower())

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


if __name__ == "__main__":
    unittest.main()
