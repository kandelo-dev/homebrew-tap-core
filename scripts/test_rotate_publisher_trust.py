#!/usr/bin/env python3
"""Regression tests for the fail-closed publisher trust rotation helper."""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import shutil
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).with_name("rotate-publisher-trust.py")
SPEC = importlib.util.spec_from_file_location("rotate_publisher_trust", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
rotation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rotation
SPEC.loader.exec_module(rotation)

ROOT = SCRIPT.parent.parent
NEW_SHA = "2" * 39 + "a"
NEW_TAG = "package-generation-rootfs-wasm32-abi-v42-sha256-" + "3" * 64


class PublisherTrustRotationTests(unittest.TestCase):
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
        quoted = match.group("value").endswith('"')
        rendered = replacement + ('"' if quoted else "")
        path.write_text(
            source[: match.start("value")]
            + rendered
            + source[match.end("value") :]
        )

    def force_kandelo_slots(self, predecessor: str) -> None:
        self.force_scalar(
            rotation.DRY_RUN_PATH,
            rotation.DRY_RUN_USES,
            predecessor,
        )
        for relative, uses in (
            (rotation.MAINTENANCE_PATH, rotation.MAINTENANCE_USES),
            (rotation.PUBLISH_PATH, rotation.PUBLISH_USES),
        ):
            self.force_scalar(relative, uses, predecessor)
            self.force_scalar(
                relative,
                rotation.EXACT_KANDELO_REF,
                predecessor,
            )
        self.force_scalar(
            rotation.TRUST_PATH,
            rotation.TRUST_KANDELO_SHA,
            predecessor,
        )
        self.force_scalar(
            rotation.CONTROLLER_PATH,
            rotation.CONTROLLER_MAIN_SHA,
            predecessor,
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        for relative in rotation.ROTATION_PATHS:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)

        # Keep these tests stable after the real M/G rotation lands by
        # reconstructing the exact protected-main predecessor in the fixture.
        self.force_kandelo_slots(rotation.LIVE_KANDELO_SHA)
        for relative in (rotation.MAINTENANCE_PATH, rotation.PUBLISH_PATH):
            self.force_scalar(
                relative,
                rotation.PACKAGE_GENERATION,
                rotation.OLD_GENERATION_TAG,
            )
        self.force_scalar(
            rotation.TRUST_PATH,
            rotation.TRUST_GENERATION,
            rotation.OLD_GENERATION_TAG,
        )
        self.force_scalar(
            rotation.CONTROLLER_PATH,
            rotation.CONTROLLER_GENERATION,
            rotation.OLD_GENERATION_TAG,
        )
        self.force_scalar(
            rotation.CONTROLLER_PATH,
            rotation.CONTROLLER_CALLER_SHA,
            sorted(rotation.OLD_CALLER_SHA256)[0],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self):
        return rotation.build_rotation(
            self.root,
            kandelo_sha=NEW_SHA,
            generation_tag=NEW_TAG,
        )

    def test_live_predecessor_preview_does_not_write_and_apply_is_idempotent(
        self,
    ) -> None:
        before = {
            relative: (self.root / relative).read_bytes()
            for relative in rotation.ROTATION_PATHS
        }
        preview = self.build()
        self.assertEqual(set(rotation.ROTATION_PATHS), set(preview.changed))
        self.assertEqual(
            before,
            {
                relative: (self.root / relative).read_bytes()
                for relative in rotation.ROTATION_PATHS
            },
        )

        rotation.apply_rotation(self.root, preview)
        after = self.build()
        self.assertEqual((), after.changed)
        self.assertEqual(preview.caller_sha256, after.caller_sha256)

    def test_b90_predecessor_converges_without_live_worktree_dirt(self) -> None:
        self.force_kandelo_slots(rotation.B90_KANDELO_SHA)
        candidate = self.build()
        self.assertEqual(set(rotation.ROTATION_PATHS), set(candidate.changed))
        rotation.apply_rotation(self.root, candidate)
        self.assertEqual((), self.build().changed)

    def test_mixed_live_and_b90_predecessors_converge(self) -> None:
        self.force_scalar(
            rotation.DRY_RUN_PATH,
            rotation.DRY_RUN_USES,
            rotation.B90_KANDELO_SHA,
        )
        self.force_scalar(
            rotation.MAINTENANCE_PATH,
            rotation.MAINTENANCE_USES,
            rotation.B90_KANDELO_SHA,
        )
        self.force_scalar(
            rotation.CONTROLLER_PATH,
            rotation.CONTROLLER_MAIN_SHA,
            rotation.B90_KANDELO_SHA,
        )

        candidate = self.build()
        rotation.apply_rotation(self.root, candidate)
        self.assertEqual((), self.build().changed)

    def test_complete_tuple_and_raw_caller_hash_are_derived_together(self) -> None:
        candidate = self.build()
        rotation.apply_rotation(self.root, candidate)

        dry_run = (self.root / rotation.DRY_RUN_PATH).read_text()
        self.assertIn(f"publish.yml@{NEW_SHA}", dry_run)
        self.assertNotIn("package-generation-", dry_run)
        self.assertIn(
            "${{ github.event.client_payload.kandelo_ref || 'main' }}",
            dry_run,
        )

        for relative in (rotation.MAINTENANCE_PATH, rotation.PUBLISH_PATH):
            source = (self.root / relative).read_text()
            self.assertIn(f"@{NEW_SHA}", source)
            self.assertIn(f"kandelo-ref: {NEW_SHA}", source)
            self.assertIn(f"package-generation-wasm32: {NEW_TAG}", source)

        publish = (self.root / rotation.PUBLISH_PATH).read_bytes()
        caller_hash = hashlib.sha256(publish).hexdigest()
        self.assertEqual(candidate.caller_sha256, caller_hash)
        controller = (self.root / rotation.CONTROLLER_PATH).read_text()
        self.assertIn(f'CURRENT_MAIN_SHA = "{NEW_SHA}"', controller)
        self.assertIn(
            f'CURRENT_ROOTFS_GENERATION_TAG = "{NEW_TAG}"',
            controller,
        )
        self.assertIn(
            f'CURRENT_CALLER_SHA256 = "{caller_hash}"',
            controller,
        )

    def test_partial_prior_application_converges(self) -> None:
        candidate = self.build()
        (self.root / rotation.DRY_RUN_PATH).write_bytes(
            candidate.contents[rotation.DRY_RUN_PATH]
        )
        resumed = self.build()
        self.assertNotIn(rotation.DRY_RUN_PATH, resumed.changed)
        rotation.apply_rotation(self.root, resumed)
        self.assertEqual((), self.build().changed)

    def test_new_publish_with_old_controller_converges_derived_hash(self) -> None:
        candidate = self.build()
        (self.root / rotation.PUBLISH_PATH).write_bytes(
            candidate.contents[rotation.PUBLISH_PATH]
        )

        resumed = self.build()
        self.assertNotIn(rotation.PUBLISH_PATH, resumed.changed)
        self.assertIn(rotation.CONTROLLER_PATH, resumed.changed)
        self.assertEqual(candidate.caller_sha256, resumed.caller_sha256)
        rotation.apply_rotation(self.root, resumed)
        self.assertEqual((), self.build().changed)

    def test_new_controller_with_old_publish_converges_derived_hash(self) -> None:
        candidate = self.build()
        (self.root / rotation.CONTROLLER_PATH).write_bytes(
            candidate.contents[rotation.CONTROLLER_PATH]
        )

        resumed = self.build()
        self.assertIn(rotation.PUBLISH_PATH, resumed.changed)
        self.assertNotIn(rotation.CONTROLLER_PATH, resumed.changed)
        self.assertEqual(candidate.caller_sha256, resumed.caller_sha256)
        rotation.apply_rotation(self.root, resumed)
        self.assertEqual((), self.build().changed)

    def test_unexpected_authority_blocks_every_write(self) -> None:
        path = self.root / rotation.MAINTENANCE_PATH
        path.write_text(
            path.read_text().replace(rotation.LIVE_KANDELO_SHA, "4" * 40)
        )
        before = {
            relative: (self.root / relative).read_bytes()
            for relative in rotation.ROTATION_PATHS
        }
        with self.assertRaisesRegex(
            rotation.RotationError,
            "maintenance reusable workflow SHA has unexpected value",
        ):
            self.build()
        self.assertEqual(
            before,
            {
                relative: (self.root / relative).read_bytes()
                for relative in rotation.ROTATION_PATHS
            },
        )

    def test_dry_run_generation_pin_is_rejected(self) -> None:
        path = self.root / rotation.DRY_RUN_PATH
        path.write_text(
            path.read_text()
            + f"\n      package-generation-wasm32: {rotation.OLD_GENERATION_TAG}\n"
        )
        with self.assertRaisesRegex(
            rotation.RotationError,
            "dry-run caller must not select a package generation",
        ):
            self.build()

    def test_changed_dry_run_ref_expression_is_rejected(self) -> None:
        path = self.root / rotation.DRY_RUN_PATH
        path.write_text(
            path.read_text().replace(
                "${{ github.event.client_payload.kandelo_ref || 'main' }}",
                "${{ github.event.client_payload.other_ref || 'main' }}",
            )
        )
        with self.assertRaisesRegex(
            rotation.RotationError,
            "dry-run kandelo-ref is not the reviewed event-selected expression",
        ):
            self.build()

    def test_invalid_or_obsolete_arguments_are_rejected(self) -> None:
        invalid = (
            ("main", NEW_TAG, "40 lowercase hex"),
            ("2" * 40, NEW_TAG, "at least one hex letter"),
            (rotation.LIVE_KANDELO_SHA, NEW_TAG, "predecessor authority"),
            (rotation.B90_KANDELO_SHA, NEW_TAG, "predecessor authority"),
            (NEW_SHA, "generation", "exact ABI 42"),
            (NEW_SHA, rotation.OLD_GENERATION_TAG, "obsolete b90 generation"),
        )
        for kandelo_sha, generation_tag, message in invalid:
            with self.subTest(
                kandelo_sha=kandelo_sha,
                generation_tag=generation_tag,
            ), self.assertRaisesRegex(rotation.RotationError, message):
                rotation.build_rotation(
                    self.root,
                    kandelo_sha=kandelo_sha,
                    generation_tag=generation_tag,
                )


if __name__ == "__main__":
    unittest.main()
