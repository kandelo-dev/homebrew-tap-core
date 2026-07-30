#!/usr/bin/env python3
"""Regression tests for fail-closed main-shell mirror caller finalization."""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).with_name(
    "finalize-main-shell-mirror-caller.py"
)
SPEC = importlib.util.spec_from_file_location(
    "finalize_main_shell_mirror_caller", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
finalizer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = finalizer
SPEC.loader.exec_module(finalizer)

ROOT = SCRIPT.parent.parent
KANDELO = "a123456789012345678901234567890123456789"
TA0 = finalizer.EXPECTED_MIRROR_AUTHORITY_SHA
TF = finalizer.EXPECTED_TAP_CATALOG_SHA
C = finalizer.EXPECTED_CANARY_SHA


class MainShellMirrorCallerFinalizationTests(unittest.TestCase):
    def force_scalar(
        self,
        relative: pathlib.Path,
        pattern,
        replacement: str,
    ) -> None:
        path = self.root / relative
        source = path.read_text()
        matches = tuple(pattern.finditer(source))
        self.assertEqual(1, len(matches), relative)
        match = matches[0]
        path.write_text(
            source[: match.start("value")]
            + replacement
            + source[match.end("value") :]
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        for relative in finalizer.FINALIZATION_PATHS:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        # Keep the unit fixture stable after the real tuple is finalized.
        for pattern in (finalizer.CALLER_USES, finalizer.CALLER_KANDELO):
            self.force_scalar(
                finalizer.CALLER_PATH,
                pattern,
                finalizer.KANDELO_PLACEHOLDER,
            )
        self.force_scalar(
            finalizer.TRUST_PATH,
            finalizer.TRUST_KANDELO,
            finalizer.KANDELO_PLACEHOLDER,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self):
        return finalizer.build_finalization(
            self.root,
            kandelo_sha=KANDELO,
        )

    def snapshots(self) -> dict[pathlib.Path, bytes]:
        return {
            relative: (self.root / relative).read_bytes()
            for relative in finalizer.FINALIZATION_PATHS
        }

    def test_preview_does_not_write_and_apply_is_idempotent(self) -> None:
        before = self.snapshots()
        preview = self.build()
        self.assertEqual(set(finalizer.FINALIZATION_PATHS), set(preview.changed))
        self.assertEqual(before, self.snapshots())

        finalizer.apply_finalization(self.root, preview)
        self.assertEqual((), self.build().changed)
        caller = (self.root / finalizer.CALLER_PATH).read_text()
        self.assertEqual(2, caller.count(KANDELO))
        self.assertEqual(1, caller.count(TA0))
        self.assertEqual(1, caller.count(TF))
        self.assertEqual(1, caller.count(C))

    def test_partial_application_converges(self) -> None:
        candidate = self.build()
        (self.root / finalizer.CALLER_PATH).write_bytes(
            candidate.contents[finalizer.CALLER_PATH]
        )
        resumed = self.build()
        self.assertNotIn(finalizer.CALLER_PATH, resumed.changed)
        self.assertIn(finalizer.TRUST_PATH, resumed.changed)
        finalizer.apply_finalization(self.root, resumed)
        self.assertEqual((), self.build().changed)

    def test_trust_first_partial_application_keeps_caller_inert(self) -> None:
        candidate = self.build()
        (self.root / finalizer.TRUST_PATH).write_bytes(
            candidate.contents[finalizer.TRUST_PATH]
        )
        caller = (self.root / finalizer.CALLER_PATH).read_text()
        self.assertIn(finalizer.KANDELO_PLACEHOLDER, caller)

        resumed = self.build()
        self.assertIn(finalizer.CALLER_PATH, resumed.changed)
        self.assertNotIn(finalizer.TRUST_PATH, resumed.changed)
        finalizer.apply_finalization(self.root, resumed)
        self.assertEqual((), self.build().changed)

    def test_unknown_existing_ref_rejects_every_write(self) -> None:
        caller = self.root / finalizer.CALLER_PATH
        caller.write_text(
            caller.read_text().replace(
                finalizer.KANDELO_PLACEHOLDER,
                "d" * 40,
                1,
            )
        )
        before = self.snapshots()
        with self.assertRaisesRegex(
            finalizer.FinalizationError,
            "caller reusable Kandelo SHA has unexpected value",
        ):
            self.build()
        self.assertEqual(before, self.snapshots())

    def test_mismatched_final_kandelo_sha_is_rejected(self) -> None:
        caller = self.root / finalizer.CALLER_PATH
        caller.write_text(
            caller.read_text().replace(
                "kandelo-ref: " + finalizer.KANDELO_PLACEHOLDER,
                "kandelo-ref: " + "e" * 40,
            )
        )
        with self.assertRaisesRegex(
            finalizer.FinalizationError,
            "caller input Kandelo SHA has unexpected value",
        ):
            self.build()

    def test_event_selected_mirror_authority_is_rejected(self) -> None:
        caller = self.root / finalizer.CALLER_PATH
        caller.write_text(
            caller.read_text().replace(
                TA0,
                "${{ github.event.client_payload.mirror_sha }}",
                1,
            )
        )
        with self.assertRaisesRegex(
            finalizer.FinalizationError,
            "caller mirror authority TA0",
        ):
            self.build()

    def test_event_selected_catalog_is_rejected(self) -> None:
        caller = self.root / finalizer.CALLER_PATH
        caller.write_text(
            caller.read_text().replace(
                TF,
                "${{ github.event.client_payload.tap_sha }}",
                1,
            )
        )
        with self.assertRaisesRegex(
            finalizer.FinalizationError,
            "final caller input TF",
        ):
            self.build()

    def test_executable_or_secret_capability_is_rejected(self) -> None:
        caller = self.root / finalizer.CALLER_PATH
        caller.write_text(caller.read_text() + "\n    secrets: inherit\n")
        with self.assertRaisesRegex(
            finalizer.FinalizationError,
            "forbidden executable or selected data",
        ):
            self.build()

    def test_malformed_requested_refs_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            finalizer.FinalizationError,
            "40 lowercase hex",
        ):
            finalizer.build_finalization(
                self.root,
                kandelo_sha="A" * 40,
            )


if __name__ == "__main__":
    unittest.main()
