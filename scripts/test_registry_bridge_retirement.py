#!/usr/bin/env python3
"""Keep active Formula builds owned by this tap, not Kandelo's registry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_TAP_RECIPES = (
    "bc",
    "fbdoom",
    "lsof",
    "mariadb",
    "modeset",
    "netcat",
    "nethack",
    "node",
    "php",
    "posix-utils-lite",
    "python",
)
DEFERRED_FORMULAE = ("erlang", "texlive")


class RegistryBridgeRetirementTest(unittest.TestCase):
    def test_active_formulae_use_complete_tap_owned_recipes(self) -> None:
        for name in ACTIVE_TAP_RECIPES:
            with self.subTest(formula=name):
                formula_path = ROOT / "Formula" / f"{name}.rb"
                source = formula_path.read_text(encoding="utf-8")
                self.assertIn("KANDELO_TAP_RECIPE = true", source)
                self.assertIn("kandelo_build_tap_recipe(", source)
                self.assertNotIn("KANDELO_REGISTRY_BRIDGE", source)
                self.assertNotIn("kandelo_build_package(", source)
                self.assertNotIn("packages/registry", source)

                recipe_root = ROOT / "Kandelo" / "recipes" / name
                manifest_path = recipe_root / "recipe.json"
                self.assertTrue(manifest_path.is_file())
                self.assertFalse(manifest_path.is_symlink())
                manifest = json.loads(manifest_path.read_bytes())
                self.assertEqual(
                    set(manifest),
                    {"schema", "dependencies", "entrypoint", "files"},
                )
                self.assertEqual(manifest["schema"], 1)
                records = manifest["files"]
                self.assertEqual(
                    [record["path"] for record in records],
                    sorted(record["path"] for record in records),
                )
                actual_files = {
                    str(path.relative_to(recipe_root))
                    for path in recipe_root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(
                    actual_files,
                    {"recipe.json", *(record["path"] for record in records)},
                )
                for record in records:
                    member = recipe_root / record["path"]
                    self.assertFalse(member.is_symlink())
                    self.assertEqual(member.stat().st_size, record["bytes"])
                    self.assertEqual(
                        hashlib.sha256(member.read_bytes()).hexdigest(),
                        record["sha256"],
                    )
                literal = re.search(
                    r'manifest_sha256: "([0-9a-f]{64})"', source
                )
                self.assertIsNotNone(literal)
                self.assertEqual(
                    literal.group(1),
                    hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                )

    def test_only_deferred_erlang_retains_the_registry_bridge(self) -> None:
        bridged = {
            path.stem
            for path in (ROOT / "Formula").glob("*.rb")
            if "KANDELO_REGISTRY_BRIDGE = true"
            in path.read_text(encoding="utf-8")
        }
        self.assertEqual(bridged, {"erlang"})

    def test_deferred_formulae_are_explicitly_disabled_from_staging(self) -> None:
        policy = (ROOT / "Kandelo/staging/formula-build-inputs.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('disabled_formulae = ["erlang", "texlive"]', policy)
        for name in DEFERRED_FORMULAE:
            self.assertTrue((ROOT / "Formula" / f"{name}.rb").is_file())


if __name__ == "__main__":
    unittest.main()
