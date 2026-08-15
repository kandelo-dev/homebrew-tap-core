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

    def test_dynamic_loader_side_modules_are_fork_instrumented(self) -> None:
        for name, side_module, linked_output in (
            ("bzip2", "plugin", '"-o", plugin'),
            ("libcxx", "side_module", '"-o", side_module'),
        ):
            with self.subTest(name=name):
                formula = (ROOT / f"Formula/{name}.rb").read_text(encoding="utf-8")
                link = formula.index(linked_output)
                instrument = formula.index(
                    f"kandelo_fork_instrument({side_module})",
                    link,
                )
                execute = formula.index("kandelo_run_wasm(loader", instrument)

                self.assertLess(link, instrument)
                self.assertLess(instrument, execute)

    def test_ed_shell_escape_declares_the_exact_tap_dash_dependency(self) -> None:
        formula = (ROOT / "Formula/ed.rb").read_text(encoding="utf-8")

        self.assertIn('depends_on "kandelo-dev/tap-core/dash" => :test', formula)
        self.assertNotIn('depends_on "dash-shell" => :test', formula)
        self.assertIn(
            'exec_programs: { "/bin/sh" => '
            'formula_opt_bin("kandelo-dev/tap-core/dash")/"dash" }',
            formula,
        )


if __name__ == "__main__":
    unittest.main()
