from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Abi43FirstBottleShippingTests(unittest.TestCase):
    def formula(self, name: str) -> str:
        return (ROOT / f"Formula/{name}.rb").read_text(encoding="utf-8")

    def test_leaf_formulae_have_one_runtime_dependency_for_brew_deps(self) -> None:
        for formula in ("ed", "findutils"):
            with self.subTest(formula=formula):
                source = self.formula(formula)
                self.assertIn(
                    'depends_on "kandelo-dev/tap-core/dash"',
                    source,
                )
                self.assertNotIn(
                    'depends_on "kandelo-dev/tap-core/dash" => :test',
                    source,
                )

    def test_libcurl_instruments_the_dynamic_loader_for_abi_43(self) -> None:
        source = self.formula("libcurl")
        compile_loader = source.index('"-ldl", "-pthread", "-o", loader')
        instrument_loader = source.index("kandelo_fork_instrument(loader)")
        execute_loader = source.index("kandelo_run_wasm(loader")
        self.assertLess(compile_loader, instrument_loader)
        self.assertLess(instrument_loader, execute_loader)

    def test_zip_does_not_require_the_broken_external_unzip_pipe(self) -> None:
        source = self.formula("zip")
        self.assertNotIn('bin/"zip", ["-T", "archive.zip"]', source)

    def test_wget_static_smoke_does_not_mount_configuration(self) -> None:
        source = self.formula("wget")
        self.assertIn('bin/"wget", ["--version"]\n    )', source)
        self.assertNotIn(
            'bin/"wget", ["--version"], guest_files: version_guest_files',
            source,
        )


if __name__ == "__main__":
    unittest.main()
