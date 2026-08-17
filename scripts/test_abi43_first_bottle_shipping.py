from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Abi43FirstBottleShippingTests(unittest.TestCase):
    def formula(self, name: str) -> str:
        return (ROOT / f"Formula/{name}.rb").read_text(encoding="utf-8")

    def test_staged_formulae_expose_dash_to_the_runtime_dependency_cache(self) -> None:
        for formula in ("ed", "findutils", "less", "nginx", "redis", "vim"):
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
                self.assertNotIn(
                    'depends_on "kandelo-dev/tap-core/dash" => [:build, :test]',
                    source,
                )

    def test_nginx_service_mounts_are_readable_by_the_non_root_guest(self) -> None:
        source = self.formula("nginx")
        self.assertIn(
            '[testpath/"nginx.conf", testpath/"html/new/message.txt"].each do |path|',
            source,
        )
        self.assertIn("chmod 0644, path", source)

    def test_redis_uses_the_shared_abi_import_validator(self) -> None:
        source = self.formula("redis")
        self.assertNotIn("unexpected_env_imports", source)
        self.assertIn(
            "kandelo_validate_wasm_artifact(server, fork: :required)",
            source,
        )
        self.assertIn(
            "kandelo_validate_wasm_artifact(cli, fork: :forbidden)",
            source,
        )

    def test_tcl_instruments_its_runtime_side_module(self) -> None:
        source = self.formula("tcl")
        compile_side_module = source.index(
            'system kandelo_cc, "-shared", "-fPIC", "-O2", "-I#{include}/tcl"'
        )
        instrument_side_module = source.index(
            "kandelo_fork_instrument(extension)", compile_side_module
        )
        load_side_module = source.index(
            "load /work/kandelo-extension.so Kandelo", instrument_side_module
        )
        self.assertLess(compile_side_module, instrument_side_module)
        self.assertLess(instrument_side_module, load_side_module)

    def test_texlive_uses_the_responsive_immutable_archive_origin(self) -> None:
        source = self.formula("texlive")
        self.assertNotIn("https://pi.kwarc.info/", source)
        self.assertEqual(
            source.count("https://texlive.info/historic/systems/texlive/2025/"),
            4,
        )

    def test_bash_bottle_does_not_gate_on_the_known_append_redirection_gap(self) -> None:
        source = self.formula("bash")
        self.assertIn("append redirection currently returns ENOTSUP", source)
        self.assertNotIn("printf 'fc-replay\\n' >> fc.log", source)

    def test_bash_bottle_does_not_gate_on_writable_fixture_self_execution(self) -> None:
        source = self.formula("bash")
        self.assertIn("do not currently preserve an", source)
        self.assertNotIn('child_output=$("$0" -c', source)

    def test_vim_bottle_keeps_editor_checks_separate_from_platform_edges(self) -> None:
        source = self.formula("vim")
        self.assertIn("%substitute/alpha/ALPHA/", source)
        self.assertIn('assert_equal "ALPHA\\nbeta\\n", source.read', source)
        self.assertNotIn("libcallnr('/work/vim-libcall.so'", source)
        self.assertNotIn("silent 0read !printf child-line", source)
        self.assertNotIn("expected_fork_descendants: 1", source)

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

    def test_git_bottle_does_not_enumerate_every_mergetool_helper(self) -> None:
        source = self.formula("git")
        self.assertIn('["clone", "file:///work/repo", "clone"]', source)
        self.assertIn('["-C", "clone", "submodule", "status"]', source)
        self.assertIn('assert_path_exists git_core/"mergetools/vimdiff"', source)
        self.assertNotIn('["-C", "clone", "mergetool", "--tool-help"]', source)
        self.assertNotIn("mergetool_help_descendant_statuses", source)

    def test_git_bottle_does_not_block_on_the_interactive_vim_editor(self) -> None:
        source = self.formula("git")
        self.assertIn('["-C", "repo", "commit", "-m", "initial"]', source)
        self.assertNotIn('bin/"git", ["-C", "clone", "commit"]', source)
        self.assertNotIn('input_ready_text:           "COMMIT_EDITMSG"', source)
        self.assertNotIn("editor_runtime_files", source)


if __name__ == "__main__":
    unittest.main()
