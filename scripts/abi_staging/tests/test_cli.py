from __future__ import annotations

import copy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.abi_staging import cli as cli_module
from scripts.abi_staging.tests import test_tap_metadata as tap_metadata_tests
from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cli import main as cli_main
from scripts.abi_staging.inventory import InventoryError
from scripts.abi_staging.oci import OciPublicationError
from scripts.abi_staging.plan import snapshot_tap_source
from scripts.abi_staging.tap_metadata import (
    load_abi_state,
    load_promotion_policy,
    validate_formula_admission_projection,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _source(root: Path, revision: str) -> dict[str, str]:
    policy = load_promotion_policy(root / "Kandelo/staging/promotion-policy.toml")
    return {
        "repository": policy.tap_repository,
        "commit": _git(root, "rev-parse", revision),
        "tree": _git(root, "rev-parse", f"{revision}^{{tree}}"),
    }


class PublicInventoryRetryTests(unittest.TestCase):
    def test_protected_inventory_transport_uses_explicit_read_credentials(self) -> None:
        with patch.dict(
            cli_module.os.environ,
            {
                "HOMEBREW_GITHUB_PACKAGES_USER": "protected-reader",
                "HOMEBREW_GITHUB_PACKAGES_TOKEN": "read-token",
            },
            clear=False,
        ):
            transport = cli_module._public_inventory_transport()
        self.assertTrue(transport._authenticated)
        self.assertTrue(transport._authenticated_public_reads)

    def test_retries_only_retryable_oci_failures_with_fresh_transports(self) -> None:
        transports: list[object] = []
        sleeps: list[float] = []
        calls = 0

        def transport_factory() -> object:
            transport = object()
            transports.append(transport)
            return transport

        def scanner(*_args, transport: object, **_kwargs):
            nonlocal calls
            self.assertIs(transport, transports[calls])
            calls += 1
            if calls < 3:
                cause = OciPublicationError(
                    "temporary public inventory failure",
                    guard_code="candidate_public_readback_failed",
                    retryable=True,
                )
                raise InventoryError("inventory failed") from cause
            return "inventory"

        self.assertEqual(
            cli_module._scan_scheduling_inventory_with_retries(
                {},
                policy=object(),
                verification_tests=(),
                scanner=scanner,
                transport_factory=transport_factory,
                sleeper=sleeps.append,
            ),
            "inventory",
        )
        self.assertEqual(calls, 3)
        self.assertEqual(len(transports), 3)
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_parallel_inventory_workers_receive_fresh_transports(self) -> None:
        transports: list[object] = []

        def transport_factory() -> object:
            transport = object()
            transports.append(transport)
            return transport

        def scanner(
            *_args,
            transport: object,
            worker_transport_factory,
            **_kwargs,
        ):
            self.assertIs(transport, transports[0])
            first = worker_transport_factory()
            second = worker_transport_factory()
            self.assertIsNot(first, second)
            return "inventory"

        self.assertEqual(
            cli_module._scan_scheduling_inventory_with_retries(
                {},
                policy=object(),
                verification_tests=(),
                scanner=scanner,
                transport_factory=transport_factory,
                sleeper=lambda _seconds: None,
            ),
            "inventory",
        )
        self.assertEqual(len(transports), 3)

    def test_does_not_retry_nonretryable_inventory_failure(self) -> None:
        calls = 0

        def scanner(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            cause = OciPublicationError(
                "invalid public inventory",
                guard_code="candidate_integrity_mismatch",
            )
            raise InventoryError("inventory failed") from cause

        with self.assertRaisesRegex(InventoryError, "inventory failed"):
            cli_module._scan_scheduling_inventory_with_retries(
                {},
                policy=object(),
                verification_tests=(),
                scanner=scanner,
                transport_factory=lambda: object(),
                sleeper=lambda _seconds: self.fail("nonretryable failure slept"),
            )
        self.assertEqual(calls, 1)


class AdmissionProjectionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = tap_metadata_tests.TapMetadataTests()
        self.fixture.setUp()
        self.root = self.fixture.root
        history, snapshot, preactivation, current = self.fixture._activate_fixture()
        prepared = self.fixture._prepared_admission(preactivation=preactivation)
        planned = self.fixture._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        landed = self.fixture._materialize_patch(
            planned.patch, "promote bash wasm32"
        )
        update = asdict(planned.update)
        self.record = tap_metadata_tests.promotion_module.finalize_admission_record(
            prepared,
            formula_metadata_base_source=current,
            formula_metadata_source=landed,
            formula_metadata_update=update,
            post_write_readback={
                "source": landed,
                "formula_metadata_update": update,
            },
            run={
                "repository": "kandelo-dev/homebrew-tap-core",
                "workflow_ref": (
                    ".github/workflows/abi-staging-promote.yml@refs/heads/main"
                ),
                "run_id": 91,
                "run_attempt": 1,
                "job": "publish-admission",
            },
        )
        _git(
            self.root,
            "remote",
            "add",
            "origin",
            "https://github.com/kandelo-dev/homebrew-tap-core.git",
        )
        self.record_path = Path(self.temporary.name) / "admission.json"
        self.record_path.write_bytes(canonical_bytes(self.record))
        self.output = Path(self.temporary.name) / "observation.json"

    def tearDown(self) -> None:
        self.fixture.tearDown()
        self.temporary.cleanup()

    def _run(self, record: Path | None = None) -> int:
        with patch.object(cli_module, "TAP_ROOT", self.root):
            return cli_main(
                [
                    "validate-admission-projection",
                    "--tap-root",
                    str(self.root),
                    "--record",
                    str(self.record_path if record is None else record),
                    "--out",
                    str(self.output),
                ]
            )

    def test_writes_the_exact_current_admission_projection(self) -> None:
        self.assertEqual(self._run(), 0)

        observation_bytes = self.output.read_bytes()
        observation = json.loads(observation_bytes)
        self.assertEqual(observation_bytes, canonical_bytes(observation))
        update = self.record["admission"]["formula_metadata_update"]
        projection = validate_formula_admission_projection(
            self.root,
            cli_module.FormulaMetadataUpdateV1(
                **{
                    **update,
                    "allowed_paths": tuple(update["allowed_paths"]),
                }
            ),
        )
        self.assertEqual(
            observation,
            {
                "schema": 1,
                "kind": "kandelo-pages-admission-projection",
                "admission_record_sha256": canonical_sha256(self.record),
                "formula": "bash",
                "architecture": "wasm32",
                "target_abi": load_abi_state(
                    self.root / "Kandelo/abi-state.json"
                ).current_abi,
                "formula_metadata_update_sha256": canonical_sha256(update),
                "projection_sha256": canonical_sha256(projection),
                "tap_source": snapshot_tap_source(
                    self.root, "kandelo-dev/homebrew-tap-core"
                ),
            },
        )

    def test_rejects_dirty_checkout_and_wrong_remote_repository(self) -> None:
        (self.root / "Formula/bash.rb").write_bytes(
            (self.root / "Formula/bash.rb").read_bytes() + b"\n"
        )
        self.assertEqual(self._run(), 1)
        self.assertFalse(self.output.exists())
        _git(self.root, "checkout", "--", "Formula/bash.rb")
        _git(
            self.root,
            "remote",
            "set-url",
            "origin",
            "https://github.com/example/homebrew-tap-core.git",
        )
        self.assertEqual(self._run(), 1)
        self.assertFalse(self.output.exists())

    def test_rejects_non_ancestor_formula_metadata_source(self) -> None:
        _git(self.root, "config", "user.name", "Admission projection test")
        _git(self.root, "config", "user.email", "test@example.invalid")
        empty_tree = _git(self.root, "mktree")
        unrelated_commit = _git(
            self.root,
            "commit-tree",
            empty_tree,
            "-m",
            "unrelated metadata source",
        )
        unrelated = _source(self.root, unrelated_commit)
        changed = copy.deepcopy(self.record)
        changed["admission"]["formula_metadata_source"] = unrelated
        changed_path = Path(self.temporary.name) / "unrelated-admission.json"
        changed_path.write_bytes(canonical_bytes(changed))

        self.assertEqual(self._run(changed_path), 1)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
