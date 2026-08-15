#!/usr/bin/env python3
"""Contract tests for ABI 43 Formula test executables that use dlopen."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Abi43DynamicLoaderFormulaeTest(unittest.TestCase):
    def test_dynamic_loader_main_programs_are_fork_instrumented(self) -> None:
        for name, loader, execute_call in (
            ("bzip2", "loader", "kandelo_run_wasm(loader"),
            ("libcxx", "loader", "kandelo_run_wasm(loader"),
            ("xz", "loader", "kandelo_run_wasm(loader"),
            ("zlib", "loader_wasm", "kandelo_run_wasm(loader_wasm"),
        ):
            with self.subTest(name=name):
                formula = (ROOT / f"Formula/{name}.rb").read_text(encoding="utf-8")
                link = formula.index('"-ldl"')
                instrument = formula.index(
                    f"kandelo_fork_instrument({loader})",
                    link,
                )
                validate = formula.index(
                    f"kandelo_validate_wasm_artifact({loader}, fork: :required)",
                    instrument,
                )
                execute = formula.index(execute_call, validate)

                self.assertLess(link, instrument)
                self.assertLess(instrument, validate)
                self.assertLess(validate, execute)

    def test_dynamic_loader_side_modules_are_fork_instrumented(self) -> None:
        for name, side_module, linked_output in (
            ("bzip2", "plugin", '"-o", plugin'),
            ("libcxx", "side_module", '"-o", side_module'),
            ("xz", "plugin", '"-o", plugin'),
            ("zlib", "plugin_so", '"-o", plugin_so'),
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

    def test_dynamic_loader_side_modules_export_the_exact_abi_identity(self) -> None:
        for name, linked_output in (
            ("bzip2", '"-o", plugin'),
            ("libcxx", '"-o", side_module'),
            ("xz", '"-o", plugin'),
            ("zlib", '"-o", plugin_so'),
        ):
            with self.subTest(name=name):
                formula = (ROOT / f"Formula/{name}.rb").read_text(encoding="utf-8")
                declaration = formula.index('#include "abi_constants.h"')
                export = formula.index('export_name("__abi_version")', declaration)
                value = formula.index("return WASM_POSIX_ABI_VERSION;", export)
                link = formula.index(linked_output, value)
                glue_root = "kandelo_require_root!" if name == "xz" else "root"
                glue_include = formula.index(
                    f'"-I#{{{glue_root}}}/libc/glue"', value, link
                )

                self.assertLess(declaration, export)
                self.assertLess(export, value)
                self.assertLess(value, glue_include)
                self.assertLess(glue_include, link)

    def test_xz_test_resolves_the_attested_kandelo_root(self) -> None:
        formula = (ROOT / "Formula/xz.rb").read_text(encoding="utf-8")

        self.assertIn('"-I#{kandelo_require_root!}/libc/glue"', formula)
        self.assertNotIn('"-I#{root}/libc/glue"', formula)

    def test_libcxx_nonpic_probe_can_compile_the_abi_identity(self) -> None:
        formula = (ROOT / "Formula/libcxx.rb").read_text(encoding="utf-8")
        command = formula.index("nonpic_command =")
        command_end = formula.index("].shelljoin", command)

        self.assertIn(
            '"-I#{root}/libc/glue"',
            formula[command:command_end],
        )

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
