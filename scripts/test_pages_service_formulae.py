#!/usr/bin/env python3
"""Contract tests for the Pages service-runtime Formulae."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.abi_staging.formula_inventory import parse_formula_source


FORMULAE = {
    "node": {
        "bridge": "spidermonkey",
        "dependencies": {"libcxx", "openssl", "zlib"},
        "source": {
            "url": "https://ftp.mozilla.org/pub/firefox/releases/140.11.0esr/source/firefox-140.11.0esr.source.tar.xz",
            "sha256": "1b034d2117356fda24807a151055132315c6ba58ad2bdf7ec71ee707fac5e028",
        },
        "outputs": ("bin/node",),
        "smoke": "node-ok",
    },
    "php": {
        "bridge": "php",
        "dependencies": {
            "icu",
            "libcurl",
            "libcxx",
            "libiconv",
            "libxml2",
            "libzip",
            "openssl",
            "sqlite",
            "zlib",
        },
        "source": {
            "url": "https://www.php.net/distributions/php-8.3.15.tar.gz",
            "sha256": "67073c3c9c56c86461e0715d9e1806af5ddffe8e6e2eb9781f7923bbb5bd67fa",
        },
        "outputs": (
            "bin/php",
            "sbin/php-fpm",
            "lib/php/extensions/opcache.so",
            "lib/php/extensions/curl.so",
            "lib/php/extensions/phar.so",
            "lib/php/extensions/zip.so",
            "lib/php/extensions/intl.so",
            "share/php/icu.dat",
        ),
        "smoke": "php-ok",
    },
    "mariadb": {
        "bridge": "mariadb",
        "dependencies": {"libcxx", "pcre2"},
        "source": {
            "url": "https://archive.mariadb.org/mariadb-10.5.28/source/mariadb-10.5.28.tar.gz",
            "sha256": "0b5070208da0116640f20bd085f1136527f998cc23268715bcbf352e7b7f3cc1",
        },
        "outputs": (
            "bin/mariadbd",
            "bin/mysqltest",
            "share/mariadb/system-tables",
            "share/mariadb/test-suite",
        ),
        "smoke": "mariadb-ok",
    },
}


class PagesServiceFormulaTests(unittest.TestCase):
    def formula_source(self, name: str) -> str:
        path = ROOT / "Formula" / f"{name}.rb"
        self.assertTrue(path.is_file(), f"missing tap-owned Formula: {path}")
        return path.read_text(encoding="utf-8")

    def parsed_formula(self, name: str) -> dict[str, object]:
        path = ROOT / "Formula" / f"{name}.rb"
        return parse_formula_source(
            name,
            f"Formula/{name}.rb",
            path.read_bytes(),
            ["wasm32"],
        )

    def test_formulae_use_only_the_tap_owned_source_build_path(self) -> None:
        for name, contract in FORMULAE.items():
            with self.subTest(formula=name):
                source = self.formula_source(name)
                self.assertIn("include KandeloFormulaSupport", source)
                self.assertIn("KANDELO_REGISTRY_BRIDGE = true", source)
                self.assertIn('kandelo_require_arch!("wasm32")', source)
                self.assertRegex(
                    source,
                    rf'kandelo_build_package\(\s*package:\s*"{contract["bridge"]}"',
                )
                self.assertNotIn("binaries-abi-v", source)
                self.assertNotIn("WASM_POSIX_BINARY_CACHE_ROOT", source)
                self.assertNotIn("ghcr.io/Automattic", source)
                self.assertNotIn("github.com/Automattic/kandelo/releases", source)

    def test_formulae_declare_the_exact_target_dependencies(self) -> None:
        for name, contract in FORMULAE.items():
            with self.subTest(formula=name):
                parsed = self.parsed_formula(name)
                self.assertEqual(
                    {item["name"] for item in parsed["target_dependencies"]},
                    contract["dependencies"],
                )

    def test_formulae_bind_the_exact_upstream_source(self) -> None:
        for name, contract in FORMULAE.items():
            with self.subTest(formula=name):
                parsed = self.parsed_formula(name)
                self.assertEqual(
                    parsed["sources"],
                    [{"role": "primary", "kind": "remote", "mirrors": [], **contract["source"]}],
                )

    def test_formulae_install_the_complete_product_runtime(self) -> None:
        for name, contract in FORMULAE.items():
            with self.subTest(formula=name):
                source = self.formula_source(name)
                for path in contract["outputs"]:
                    self.assertIn(path, source)
                self.assertIn(contract["smoke"], source)

    def test_formulae_bind_dependencies_to_the_registry_helpers(self) -> None:
        node = self.formula_source("node")
        self.assertIn('"WASM_POSIX_DEP_LIBCXX_DIR"', node)
        self.assertIn('"WASM_POSIX_DEP_OPENSSL_DIR"', node)
        self.assertIn('"WASM_POSIX_DEP_ZLIB_DIR"', node)

        php = self.formula_source("php")
        for dependency in FORMULAE["php"]["dependencies"]:
            key = dependency.upper().replace("-", "_")
            self.assertIn(f'"WASM_POSIX_DEP_{key}_DIR"', php)

        mariadb = self.formula_source("mariadb")
        self.assertIn('"WASM_POSIX_DEP_LIBCXX_DIR"', mariadb)
        self.assertIn('"WASM_POSIX_DEP_PCRE2_DIR"', mariadb)
        self.assertNotIn("WASM_POSIX_DEP_PCRE2_SOURCE_SRC_DIR", mariadb)


if __name__ == "__main__":
    unittest.main()
