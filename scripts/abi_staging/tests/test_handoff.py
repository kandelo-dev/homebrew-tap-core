from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile
import tempfile
import unittest

from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.custody import create_source_custody
from scripts.abi_staging.handoff import (
    HandoffError,
    build_handoff_inventory,
    build_miniature_build_result_fixture,
    build_miniature_handoff_inventory_fixture,
    load_build_result,
    load_handoff_inventory,
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
