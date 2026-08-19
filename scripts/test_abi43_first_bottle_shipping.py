from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Abi43FirstBottleShippingTests(unittest.TestCase):
    def formula(self, name: str) -> str:
        return (ROOT / f"Formula/{name}.rb").read_text(encoding="utf-8")

    def recipe(self, name: str) -> str:
        return (ROOT / f"Kandelo/recipes/{name}/build.sh").read_text(
            encoding="utf-8"
        )

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

    def test_tcl_instruments_its_test_side_module_before_loading_it(self) -> None:
        source = self.formula("tcl")
        compile_extension = source.index('"-ltclstub", "-o", extension')
        instrument_extension = source.index("kandelo_fork_instrument(extension)")
        mount_extension = source.index('"/work/kandelo-extension.so" => extension')
        self.assertLess(compile_extension, instrument_extension)
        self.assertLess(instrument_extension, mount_extension)

    def test_node_declares_the_native_rust_recipe_toolchain(self) -> None:
        source = self.formula("node")
        self.assertIn('depends_on "cbindgen" => :build', source)
        self.assertIn('depends_on "rust" => :build', source)

    def test_mariadb_maps_private_build_paths_out_of_target_artifacts(self) -> None:
        source = self.recipe("mariadb")
        self.assertIn('prefix_map_flags() {', source)
        self.assertIn(
            'REPRODUCIBLE_PREFIX_MAPS="$(prefix_map_flags "$WORK_DIR" '
            '/usr/src/mariadb-build)"',
            source,
        )
        self.assertIn(
            '-DCMAKE_C_FLAGS_RELEASE="${MARIADB_OPT_LEVEL:--O2} '
            '-DNDEBUG $REPRODUCIBLE_PREFIX_MAPS"',
            source,
        )
        self.assertIn(
            '-DCMAKE_CXX_FLAGS_RELEASE="${MARIADB_OPT_LEVEL:--O2} '
            '-DNDEBUG $REPRODUCIBLE_PREFIX_MAPS"',
            source,
        )

    def test_php_links_curl_side_module_with_the_pic_libcurl_archive(self) -> None:
        source = self.recipe("php")
        self.assertIn(
            '[ -f "$LIBCURL_PREFIX/lib/libcurl-pic.a" ] || '
            '{ echo "ERROR: libcurl resolve missing libcurl-pic.a"; exit 1; }',
            source,
        )
        curl_start = source.index('echo "==> Building curl.so (extension)..."')
        curl_link = source[
            curl_start : source.index('echo "==> curl.so:', curl_start)
        ]
        self.assertIn('"$LIBCURL_PREFIX/lib/libcurl-pic.a"', curl_link)
        self.assertNotIn('"$LIBCURL_PREFIX/lib/libcurl.a"', curl_link)

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

    def test_nginx_guest_can_read_its_mounted_http_fixture(self) -> None:
        source = self.formula("nginx")
        test_block = source[source.index("  test do\n") :]

        fixture_write = test_block.index('(testpath/"nginx.conf").write')
        expected_permissions = (
            'chmod 0644, [testpath/"nginx.conf", '
            'testpath/"html/new/message.txt"]'
        )
        self.assertIn(expected_permissions, test_block)
        guest_permissions = test_block.index(expected_permissions)
        service_start = test_block.index("responses = kandelo_run_http_service(")
        self.assertLess(fixture_write, guest_permissions)
        self.assertLess(guest_permissions, service_start)

    def test_nginx_browser_guest_has_the_configured_runtime_accounts(self) -> None:
        source = self.formula("nginx")
        test_block = source[source.index("  test do\n") :]

        self.assertIn('browser_passwd.write "nobody:x:65534:65534:', test_block)
        self.assertIn('browser_group.write "nobody:x:65534:', test_block)
        self.assertRegex(test_block, r'"/etc/passwd"\s+=> browser_passwd')
        self.assertRegex(test_block, r'"/etc/group"\s+=> browser_group')

    def test_redis_accepts_the_current_abi_dynamic_loader_contract(self) -> None:
        source = self.formula("redis")

        for loader_import in (
            "__wasm_dlopen_main",
            "__wasm_dlopen_prepare",
            "__wasm_dlopen_next",
        ):
            with self.subTest(loader_import=loader_import):
                self.assertIn(loader_import, source)

    def test_nginx_installs_a_guest_readable_runtime_tree(self) -> None:
        source = self.formula("nginx")
        install_block = source[source.index("  def install\n") : source.index("  test do\n")]
        test_block = source[source.index("  test do\n") :]

        self.assertIn('[prefix/"conf", prefix/"html"].each do |tree|', install_block)
        self.assertIn(
            "path.chmod(path.directory? ? 0755 : 0644)",
            install_block,
        )
        self.assertIn(
            'assert_equal 0755, (prefix/"conf").stat.mode & 0777',
            test_block,
        )
        self.assertIn(
            'assert_equal 0644, (prefix/"conf/mime.types").stat.mode & 0777',
            test_block,
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
