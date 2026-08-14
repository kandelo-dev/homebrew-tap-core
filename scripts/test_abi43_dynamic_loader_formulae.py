#!/usr/bin/env python3
"""Contract tests for ABI 43 Formula test executables that use dlopen."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Abi43DynamicLoaderFormulaeTest(unittest.TestCase):
    def test_dynamic_loader_main_programs_are_fork_instrumented(self) -> None:
        for name in ("bzip2", "libcxx"):
            with self.subTest(name=name):
                formula = (ROOT / f"Formula/{name}.rb").read_text(encoding="utf-8")
                link = formula.index('"-ldl"')
                instrument = formula.index("kandelo_fork_instrument(loader)", link)
                validate = formula.index(
                    "kandelo_validate_wasm_artifact(loader, fork: :required)",
                    instrument,
                )
                execute = formula.index("kandelo_run_wasm(loader", validate)

                self.assertLess(link, instrument)
                self.assertLess(instrument, validate)
                self.assertLess(validate, execute)

    def test_ed_shell_escape_uses_the_current_native_dash_test_dependency(self) -> None:
        formula = (ROOT / "Formula/ed.rb").read_text(encoding="utf-8")

        self.assertIn('depends_on "dash-shell" => :test', formula)
        self.assertNotIn('depends_on "dash" => :test', formula)
        self.assertIn(
            'exec_programs: { "/bin/sh" => '
            'formula_opt_bin("kandelo-dev/tap-core/dash")/"dash" }',
            formula,
        )


if __name__ == "__main__":
    unittest.main()
