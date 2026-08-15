#!/usr/bin/env python3
"""Contract tests for the ABI 43 login Formula roots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "5669d27fa171ad1bccf50031914dc6d997666276"
SOURCE_SHA256 = "af0984c5312b6396e86e62910342a0e23cd4c8822353b3d58787d8f071a7b6f4"
FORMULAE = {
    "login": {
        "outputs": ("login.wasm",),
        "source": "programs/login.c",
        "version": "0.1.0",
    },
    "sudo-lite": {
        "outputs": ("sudo-lite.wasm",),
        "source": "programs/sudo-lite.c",
        "version": "0.1.0",
    },
    "sudo": {
        "outputs": (
            "cvtsudoers.wasm",
            "sudo.wasm",
            "sudoreplay.wasm",
            "visudo.wasm",
        ),
        "source": None,
        "version": "1.9.17p2",
    },
}


class Abi43LoginFormulaRootsTest(unittest.TestCase):
    def test_formulae_use_sealed_tap_recipes_and_leave_bottles_to_promotion(self) -> None:
        for name, expected in FORMULAE.items():
            with self.subTest(name=name):
                formula = (ROOT / f"Formula/{name}.rb").read_text(encoding="utf-8")
                manifest_path = ROOT / f"Kandelo/recipes/{name}/recipe.json"
                manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

                self.assertIn("  KANDELO_TAP_RECIPE = true\n", formula)
                self.assertNotIn("KANDELO_REGISTRY_BRIDGE", formula)
                self.assertNotIn("kandelo_build_package(", formula)
                self.assertIn("kandelo_build_tap_recipe(", formula)
                self.assertIn(
                    f'manifest_sha256: "{manifest_sha256}"',
                    formula,
                )
                self.assertNotIn("  bottle do\n", formula)
                self.assertNotRegex(formula, r"ghcr\.io/(?:v2/)?[Aa]utomattic")
                self.assertIn(f'  version "{expected["version"]}"\n', formula)

        tap_policy = (ROOT / "Kandelo/staging/tap-policy.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('candidate_owner = "kandelo-dev"', tap_policy)
        self.assertIn(
            'candidate_repository_prefix = "homebrew-tap-core-abi-"', tap_policy
        )

    def test_recipes_bind_their_complete_file_inventory(self) -> None:
        for name, expected in FORMULAE.items():
            with self.subTest(name=name):
                recipe_root = ROOT / f"Kandelo/recipes/{name}"
                manifest = json.loads((recipe_root / "recipe.json").read_bytes())
                self.assertEqual(1, manifest["schema"])
                self.assertEqual([], manifest["dependencies"])
                self.assertEqual("build.sh", manifest["entrypoint"])
                records = manifest["files"]
                self.assertEqual(
                    sorted(record["path"] for record in records),
                    [record["path"] for record in records],
                )
                expected_files = {
                    path.relative_to(recipe_root).as_posix()
                    for path in recipe_root.rglob("*")
                    if path.is_file() and path.name != "recipe.json"
                }
                self.assertEqual(expected_files, {record["path"] for record in records})
                for record in records:
                    body = (recipe_root / record["path"]).read_bytes()
                    self.assertEqual(len(body), record["bytes"])
                    self.assertEqual(hashlib.sha256(body).hexdigest(), record["sha256"])
                    self.assertEqual(
                        "0755" if record["path"] == "build.sh" else "0644",
                        record["mode"],
                    )

                build = (recipe_root / "build.sh").read_text(encoding="utf-8")
                for output in expected["outputs"]:
                    self.assertIn(output, build)
                self.assertNotRegex(build, r"\b(?:curl|wget)\b")
                self.assertNotIn("build-deps resolve", build)
                self.assertNotIn("install-local-binary", build)
                self.assertNotIn("HOMEBREW_KANDELO_ROOT", build)
                self.assertNotRegex(build, r"\bREPO_ROOT\b")

    def test_kandelo_owned_sources_are_exactly_frozen_to_the_request_commit(self) -> None:
        for name in ("login", "sudo-lite"):
            with self.subTest(name=name):
                formula = (ROOT / f"Formula/{name}.rb").read_text(encoding="utf-8")
                build = (ROOT / f"Kandelo/recipes/{name}/build.sh").read_text(
                    encoding="utf-8"
                )
                self.assertIn(
                    f'  url "https://github.com/Automattic/kandelo/archive/{SOURCE_COMMIT}.tar.gz"',
                    formula,
                )
                self.assertIn(f'  sha256 "{SOURCE_SHA256}"', formula)
                self.assertIn(f'SOURCE_COMMIT="{SOURCE_COMMIT}"', build)
                self.assertIn(f'SOURCE_PATH="{FORMULAE[name]["source"]}"', build)
                self.assertIn('SOURCE_ROOT="${WASM_POSIX_DEP_SOURCE_DIR:?}"', build)

    def test_sudo_uses_only_pinned_upstream_source_and_reviewed_patch(self) -> None:
        formula = (ROOT / "Formula/sudo.rb").read_text(encoding="utf-8")
        build = (ROOT / "Kandelo/recipes/sudo/build.sh").read_text(encoding="utf-8")
        patch = (ROOT / "Kandelo/recipes/sudo/patches/wasm-main-envp.patch").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '  url "https://github.com/sudo-project/sudo/archive/refs/tags/v1.9.17p2.tar.gz"',
            formula,
        )
        self.assertIn(
            '  sha256 "cabee23359afa698d147478c3a141437dbfecb510382e114eaf4b5087a1f8ca5"',
            formula,
        )
        self.assertIn('PATCH="${WASM_POSIX_DEP_PATCH:?}"', build)
        self.assertIn('MAKE="${WASM_POSIX_DEP_MAKE:?}"', build)
        self.assertIn('"$PATCH" -d "$SRC_DIR" -p1', build)
        self.assertIn(
            'SDK_ROOT="$(cd "$(dirname "$(command -v wasm32posix-configure)")/.." && pwd)"',
            build,
        )
        self.assertIn('CONFIG_SITE="$SDK_ROOT/config.site"', build)
        self.assertNotIn('wasm32posix-configure "$SRC_DIR/configure"', build)
        self.assertIn(
            'if [ ! -f "$MAIN_ENVP_PATCH" ] || [ -L "$MAIN_ENVP_PATCH" ]; then',
            build,
        )
        self.assertNotIn("grep -q 'kernel_fork'", build)
        self.assertIn("grep 'kernel_fork' >/dev/null", build)
        self.assertIn('FORMULA_ROOT="$(dirname "$SOURCE_ROOT")"', build)
        for option in ("file", "debug", "macro"):
            self.assertIn(
                f'-f{option}-prefix-map=${{FORMULA_ROOT}}=/usr/src/sudo-1.9.17p2',
                build,
            )
        self.assertIn('for tool in wasm32posix-configure wasm-objdump wasm-opt; do', build)
        self.assertIn('wasm-opt --strip-debug "$source_path" -o "$artifact"', build)
        self.assertLess(
            build.index('wasm-opt --strip-debug "$source_path" -o "$artifact"'),
            build.index('if wasm-objdump -x "$artifact"'),
        )
        self.assertIn("char **envp = environ;", patch)

    def test_recipes_expose_the_posix_types_required_by_shared_sdk_glue(self) -> None:
        for name in ("login", "sudo-lite"):
            with self.subTest(name=name):
                build = (ROOT / f"Kandelo/recipes/{name}/build.sh").read_text(
                    encoding="utf-8"
                )
                self.assertIn("    -D_GNU_SOURCE \\\n", build)
                self.assertIn(f'    -o "$BUILD_ROOT/{name}.o"', build)

    def test_capture_policy_binds_every_formula_and_recipe(self) -> None:
        policy = (ROOT / "Kandelo/staging/formula-build-inputs.toml").read_text(
            encoding="utf-8"
        )
        for name in FORMULAE:
            with self.subTest(name=name):
                pattern = re.compile(
                    rf'\[\[formulae\]\]\nname = "{re.escape(name)}"\n'
                    rf'architectures = \["wasm32"\]\n'
                    rf'profiles = \["kandelo-common"\]\n'
                    rf'kandelo_paths = \[\]\n'
                    rf'tap_paths = \["Kandelo/recipes/{re.escape(name)}"\]',
                    re.MULTILINE,
                )
                self.assertRegex(policy, pattern)


if __name__ == "__main__":
    unittest.main()
