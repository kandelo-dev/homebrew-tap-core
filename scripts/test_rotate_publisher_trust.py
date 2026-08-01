#!/usr/bin/env python3
"""Regression tests for the fail-closed publisher trust rotation helper."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
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
    def scalar_value(
        self,
        relative: pathlib.Path,
        pattern,
    ) -> str:
        source = (self.root / relative).read_text()
        matches = tuple(pattern.finditer(source))
        self.assertEqual(1, len(matches), relative)
        return matches[0].group("value").removesuffix('"')

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

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        for relative in rotation.ROTATION_PATHS:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)

        self.predecessor_sha = self.scalar_value(
            rotation.TRUST_PATH,
            rotation.TRUST_KANDELO_SHA,
        )
        self.predecessor_dry_run_sha = self.scalar_value(
            rotation.TRUST_PATH,
            rotation.TRUST_DRY_RUN_KANDELO_SHA,
        )
        self.predecessor_first_publication_sha = self.scalar_value(
            rotation.TRUST_PATH,
            rotation.TRUST_FIRST_PUBLICATION_KANDELO_SHA,
        )
        self.predecessor_generation = self.scalar_value(
            rotation.TRUST_PATH,
            rotation.TRUST_GENERATION,
        )
        self.predecessor_caller = hashlib.sha256(
            (self.root / rotation.PUBLISH_PATH).read_bytes()
        ).hexdigest()

        # WHY: derive the test predecessor from checked-in protected-main
        # inputs, but independently require the controller to name those exact
        # raw caller bytes. A stale controller must not become test authority.
        self.assertEqual(
            self.predecessor_caller,
            self.scalar_value(
                rotation.CONTROLLER_PATH,
                rotation.CONTROLLER_CALLER_SHA,
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, **overrides):
        arguments = {
            "predecessor_kandelo_sha": self.predecessor_sha,
            "predecessor_dry_run_kandelo_sha": (
                self.predecessor_dry_run_sha
            ),
            "predecessor_first_publication_kandelo_sha": (
                self.predecessor_first_publication_sha
            ),
            "predecessor_generation_tag": self.predecessor_generation,
            "predecessor_caller_sha256": self.predecessor_caller,
            "kandelo_sha": NEW_SHA,
            "generation_tag": NEW_TAG,
        }
        arguments.update(overrides)
        return rotation.build_rotation(self.root, **arguments)

    def cli_arguments(self) -> list[str]:
        return [
            "--root",
            str(self.root),
            "--predecessor-kandelo-sha",
            self.predecessor_sha,
            "--predecessor-dry-run-kandelo-sha",
            self.predecessor_dry_run_sha,
            "--predecessor-first-publication-kandelo-sha",
            self.predecessor_first_publication_sha,
            "--predecessor-generation-tag",
            self.predecessor_generation,
            "--predecessor-caller-sha256",
            self.predecessor_caller,
            "--kandelo-sha",
            NEW_SHA,
            "--generation-tag",
            NEW_TAG,
        ]

    def unknown_kandelo_sha(self) -> str:
        for digit in "456789":
            candidate = digit * 39 + "a"
            if candidate not in (
                self.predecessor_sha,
                self.predecessor_dry_run_sha,
                self.predecessor_first_publication_sha,
                NEW_SHA,
            ):
                return candidate
        raise AssertionError("test SHA candidates unexpectedly exhausted")

    def unknown_generation_tag(self) -> str:
        prefix = "package-generation-rootfs-wasm32-abi-v42-sha256-"
        for digit in "456789":
            candidate = prefix + digit * 64
            if candidate not in (self.predecessor_generation, NEW_TAG):
                return candidate
        raise AssertionError("test generation candidates unexpectedly exhausted")

    def unknown_caller_sha256(self) -> str:
        for digit in "456789":
            candidate = digit * 64
            if candidate != self.predecessor_caller:
                return candidate
        raise AssertionError("test caller candidates unexpectedly exhausted")

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

    def test_complete_tuple_and_raw_caller_hash_are_derived_together(self) -> None:
        candidate = self.build()

        dry_run = candidate.contents[rotation.DRY_RUN_PATH].decode()
        self.assertIn(f"publish.yml@{NEW_SHA}", dry_run)
        self.assertNotIn("package-generation-", dry_run)
        self.assertIn(
            "${{ github.event.client_payload.kandelo_ref || 'main' }}",
            dry_run,
        )

        for relative in (rotation.MAINTENANCE_PATH, rotation.PUBLISH_PATH):
            source = candidate.contents[relative].decode()
            self.assertIn(f"@{NEW_SHA}", source)
            self.assertIn(f"kandelo-ref: {NEW_SHA}", source)
            self.assertIn(f"package-generation-wasm32: {NEW_TAG}", source)

        first_publication = candidate.contents[
            rotation.FIRST_PUBLICATION_PATH
        ].decode()
        self.assertIn(
            "reusable-homebrew-repository-namespace-canary.yml"
            f"@{NEW_SHA}",
            first_publication,
        )
        self.assertIn(f"kandelo-ref: {NEW_SHA}", first_publication)
        self.assertNotIn("package-generation-", first_publication)

        trust = candidate.contents[rotation.TRUST_PATH].decode()
        self.assertIn(f'CURRENT_KANDELO_WORKFLOW_SHA = "{NEW_SHA}"', trust)
        self.assertIn(
            f'DRY_RUN_KANDELO_WORKFLOW_SHA = "{NEW_SHA}"',
            trust,
        )
        self.assertIn(
            f'FIRST_PUBLICATION_KANDELO_SHA = "{NEW_SHA}"',
            trust,
        )
        self.assertIn(
            f'PACKAGE_GENERATION_WASM32_TAG = "{NEW_TAG}"',
            trust,
        )

        publish = candidate.contents[rotation.PUBLISH_PATH]
        caller_hash = hashlib.sha256(publish).hexdigest()
        self.assertEqual(candidate.caller_sha256, caller_hash)
        controller = candidate.contents[rotation.CONTROLLER_PATH].decode()
        self.assertIn(f'CURRENT_MAIN_SHA = "{NEW_SHA}"', controller)
        self.assertIn(
            f'CURRENT_ROOTFS_GENERATION_TAG = "{NEW_TAG}"',
            controller,
        )
        self.assertIn(
            f'CURRENT_CALLER_SHA256 = "{caller_hash}"',
            controller,
        )

        for relative in (
            rotation.DRY_RUN_PATH,
            rotation.MAINTENANCE_PATH,
            rotation.PUBLISH_PATH,
            rotation.FIRST_PUBLICATION_PATH,
            rotation.TRUST_PATH,
        ):
            source = candidate.contents[relative].decode()
            self.assertNotIn(self.predecessor_sha, source)
            self.assertNotIn(
                self.predecessor_first_publication_sha,
                source,
            )
            self.assertNotIn(self.predecessor_generation, source)
            self.assertNotIn(self.predecessor_caller, source)

    def test_separately_named_historical_controller_authority_is_preserved(
        self,
    ) -> None:
        path = self.root / rotation.CONTROLLER_PATH
        source = path.read_text()
        marker = f'CURRENT_MAIN_SHA = "{self.predecessor_sha}"'
        self.assertEqual(1, source.count(marker))
        historical = (
            f'HISTORICAL_TEST_MAIN_SHA = "{self.predecessor_sha}"\n'
            "HISTORICAL_TEST_ROOTFS_GENERATION_TAG = "
            f'"{self.predecessor_generation}"\n'
            "HISTORICAL_TEST_CALLER_SHA256 = "
            f'"{self.predecessor_caller}"\n'
        )
        path.write_text(source.replace(marker, historical + marker))

        controller = self.build().contents[rotation.CONTROLLER_PATH].decode()
        self.assertIn(historical, controller)
        self.assertIn(f'CURRENT_MAIN_SHA = "{NEW_SHA}"', controller)

    def test_partial_prior_application_converges_across_files(self) -> None:
        candidate = self.build()
        for relative in (
            rotation.DRY_RUN_PATH,
            rotation.MAINTENANCE_PATH,
            rotation.FIRST_PUBLICATION_PATH,
            rotation.TRUST_PATH,
        ):
            (self.root / relative).write_bytes(candidate.contents[relative])

        resumed = self.build()
        self.assertNotIn(rotation.DRY_RUN_PATH, resumed.changed)
        self.assertNotIn(rotation.MAINTENANCE_PATH, resumed.changed)
        self.assertNotIn(
            rotation.FIRST_PUBLICATION_PATH,
            resumed.changed,
        )
        self.assertNotIn(rotation.TRUST_PATH, resumed.changed)
        self.assertIn(rotation.PUBLISH_PATH, resumed.changed)
        self.assertIn(rotation.CONTROLLER_PATH, resumed.changed)
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

    def test_unknown_predecessor_slot_blocks_every_write(self) -> None:
        self.force_scalar(
            rotation.MAINTENANCE_PATH,
            rotation.MAINTENANCE_USES,
            self.unknown_kandelo_sha(),
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

    def test_wrong_predecessor_dry_run_sha_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            rotation.RotationError,
            "dry-run reusable workflow SHA has unexpected value",
        ):
            self.build(
                predecessor_dry_run_kandelo_sha=self.unknown_kandelo_sha()
            )

    def test_wrong_predecessor_first_publication_sha_is_rejected(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            rotation.RotationError,
            "first-publication reusable workflow SHA has "
            "unexpected value",
        ):
            self.build(
                predecessor_first_publication_kandelo_sha=(
                    self.unknown_kandelo_sha()
                )
            )

    def test_unknown_first_publication_ref_is_rejected(self) -> None:
        self.force_scalar(
            rotation.FIRST_PUBLICATION_PATH,
            rotation.EXACT_KANDELO_REF,
            self.unknown_kandelo_sha(),
        )
        with self.assertRaisesRegex(
            rotation.RotationError,
            "first-publication kandelo-ref has unexpected value",
        ):
            self.build()

    def test_unknown_first_publication_trust_root_is_rejected(
        self,
    ) -> None:
        self.force_scalar(
            rotation.TRUST_PATH,
            rotation.TRUST_FIRST_PUBLICATION_KANDELO_SHA,
            self.unknown_kandelo_sha(),
        )
        with self.assertRaisesRegex(
            rotation.RotationError,
            "trust-test first-publication Kandelo SHA has "
            "unexpected value",
        ):
            self.build()

    def test_unknown_generation_slot_is_rejected(self) -> None:
        self.force_scalar(
            rotation.MAINTENANCE_PATH,
            rotation.PACKAGE_GENERATION,
            self.unknown_generation_tag(),
        )
        with self.assertRaisesRegex(
            rotation.RotationError,
            "maintenance package generation has unexpected value",
        ):
            self.build()

    def test_wrong_predecessor_caller_digest_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            rotation.RotationError,
            "production caller bytes have SHA-256",
        ):
            self.build(
                predecessor_caller_sha256=self.unknown_caller_sha256()
            )

    def test_unreviewed_production_caller_bytes_are_rejected(self) -> None:
        path = self.root / rotation.PUBLISH_PATH
        path.write_text(path.read_text() + "\n# unreviewed caller bytes\n")
        with self.assertRaisesRegex(
            rotation.RotationError,
            "production caller bytes have SHA-256",
        ):
            self.build()

    def test_mixed_production_caller_tuple_is_rejected(self) -> None:
        self.force_scalar(
            rotation.PUBLISH_PATH,
            rotation.PUBLISH_USES,
            NEW_SHA,
        )
        with self.assertRaisesRegex(
            rotation.RotationError,
            "production caller bytes have SHA-256",
        ):
            self.build()

    def test_unknown_controller_caller_digest_is_rejected(self) -> None:
        self.force_scalar(
            rotation.CONTROLLER_PATH,
            rotation.CONTROLLER_CALLER_SHA,
            self.unknown_caller_sha256(),
        )
        with self.assertRaisesRegex(
            rotation.RotationError,
            "rollout-controller caller SHA-256 has unexpected value",
        ):
            self.build()

    def test_dry_run_generation_pin_is_rejected(self) -> None:
        path = self.root / rotation.DRY_RUN_PATH
        path.write_text(
            path.read_text()
            + "\n      package-generation-wasm32: "
            + self.predecessor_generation
            + "\n"
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

    def test_invalid_or_unchanged_arguments_are_rejected(self) -> None:
        invalid = (
            (
                {"predecessor_kandelo_sha": "main"},
                "predecessor Kandelo SHA must be exactly 40 lowercase hex",
            ),
            (
                {"predecessor_kandelo_sha": "2" * 40},
                "predecessor Kandelo SHA must contain at least one hex letter",
            ),
            (
                {"predecessor_first_publication_kandelo_sha": "main"},
                "predecessor first-publication Kandelo SHA must be exactly",
            ),
            (
                {"predecessor_generation_tag": "generation"},
                "predecessor generation tag must be an exact ABI 42",
            ),
            (
                {"predecessor_caller_sha256": "digest"},
                "predecessor caller SHA-256 must be exactly 64 lowercase hex",
            ),
            (
                {"kandelo_sha": "main"},
                "successor Kandelo SHA must be exactly 40 lowercase hex",
            ),
            (
                {"kandelo_sha": "2" * 40},
                "successor Kandelo SHA must contain at least one hex letter",
            ),
            (
                {"kandelo_sha": self.predecessor_sha},
                "successor Kandelo SHA still names the predecessor",
            ),
            (
                {"generation_tag": "generation"},
                "successor generation tag must be an exact ABI 42",
            ),
            (
                {"generation_tag": self.predecessor_generation},
                "successor generation tag still names the predecessor",
            ),
        )
        for overrides, message in invalid:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                rotation.RotationError, message
            ):
                self.build(**overrides)

    def test_rotation_input_symlink_is_rejected(self) -> None:
        target = self.root / rotation.DRY_RUN_PATH
        replacement = target.with_name("dry-run-target.yml")
        target.rename(replacement)
        target.symlink_to(replacement.name)
        with self.assertRaisesRegex(
            rotation.RotationError,
            "rotation input is not a regular file",
        ):
            self.build()

    def test_cli_requires_and_preserves_the_complete_input_tuple(self) -> None:
        arguments = rotation.parse_args(self.cli_arguments())
        self.assertEqual(self.root, arguments.root)
        self.assertEqual(
            self.predecessor_sha, arguments.predecessor_kandelo_sha
        )
        self.assertEqual(
            self.predecessor_dry_run_sha,
            arguments.predecessor_dry_run_kandelo_sha,
        )
        self.assertEqual(
            self.predecessor_first_publication_sha,
            arguments.predecessor_first_publication_kandelo_sha,
        )
        self.assertEqual(
            self.predecessor_generation,
            arguments.predecessor_generation_tag,
        )
        self.assertEqual(
            self.predecessor_caller,
            arguments.predecessor_caller_sha256,
        )
        self.assertEqual(NEW_SHA, arguments.kandelo_sha)
        self.assertEqual(NEW_TAG, arguments.generation_tag)
        self.assertFalse(arguments.apply)

        for option in (
            "--predecessor-kandelo-sha",
            "--predecessor-dry-run-kandelo-sha",
            "--predecessor-first-publication-kandelo-sha",
            "--predecessor-generation-tag",
            "--predecessor-caller-sha256",
        ):
            incomplete = self.cli_arguments()
            index = incomplete.index(option)
            del incomplete[index : index + 2]
            with (
                self.subTest(missing=option),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                rotation.parse_args(incomplete)
            self.assertEqual(2, raised.exception.code)

    def test_main_preview_is_read_only_and_apply_uses_explicit_tuple(self) -> None:
        before = {
            relative: (self.root / relative).read_bytes()
            for relative in rotation.ROTATION_PATHS
        }
        preview_output = io.StringIO()
        with contextlib.redirect_stdout(preview_output):
            self.assertEqual(0, rotation.main(self.cli_arguments()))
        self.assertIn(
            "would update 6 file(s)",
            preview_output.getvalue(),
        )
        self.assertIn("caller-sha256=", preview_output.getvalue())
        self.assertEqual(
            before,
            {
                relative: (self.root / relative).read_bytes()
                for relative in rotation.ROTATION_PATHS
            },
        )

        apply_output = io.StringIO()
        with contextlib.redirect_stdout(apply_output):
            self.assertEqual(
                0,
                rotation.main(self.cli_arguments() + ["--apply"]),
            )
        self.assertIn("updated 6 file(s)", apply_output.getvalue())
        self.assertEqual((), self.build().changed)

    def test_helper_does_not_embed_the_checked_in_authority(self) -> None:
        source = SCRIPT.read_text()
        self.assertNotIn(self.predecessor_sha, source)
        self.assertNotIn(self.predecessor_dry_run_sha, source)
        self.assertNotIn(self.predecessor_first_publication_sha, source)
        self.assertNotIn(self.predecessor_generation, source)
        self.assertNotIn(self.predecessor_caller, source)


if __name__ == "__main__":
    unittest.main()
