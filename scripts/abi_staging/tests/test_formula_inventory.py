from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import unittest

from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.formula_inventory import (
    FormulaInventoryError,
    build_static_formula_probe,
    combined_source_sha256,
    generate_formula_inventory,
    load_formula_inventory,
    normalize_formula_source,
    parse_formula_source,
    validate_formula_probe,
    validate_legacy_sidecar,
)
from scripts.abi_staging.policy import (
    generate_formula_capture_catalog,
    load_formula_build_inputs,
)


TAP_ROOT = Path(__file__).resolve().parents[3]


def _formula_source(*, bottle: str = "") -> bytes:
    return (
        'require "support"\n\n'
        "class Demo < Formula\n"
        '  desc "fixture"\n'
        '  homepage "https://example.test"\n'
        '  url "https://example.test/demo-1.2.3.tar.gz"\n'
        '  mirror "https://mirror.example.test/demo-1.2.3.tar.gz"\n'
        f'  sha256 "{"a" * 64}"\n'
        '  revision 2\n\n'
        '  depends_on "kandelo-dev/tap-core/zlib"\n'
        "  depends_on KandeloFormulaSupport::WabtRequirement => :build\n\n"
        '  resource "manual" do\n'
        '    url "https://example.test/demo.1"\n'
        f'    sha256 "{"b" * 64}"\n'
        "  end\n\n"
        "  patch :DATA\n\n"
        "  def install\n"
        '    kandelo_require_arch!("wasm32")\n'
        '    system "make"\n'
        "  end\n\n"
        "  test do\n"
        '    system "true"\n'
        "  end\n"
        f"{bottle}"
        "end\n"
        "__END__\n"
        "diff --git a/a b/a\n"
    ).encode()


def _bottle(root: str, rebuild: int, digest: str) -> str:
    return (
        "\n  bottle do\n"
        f'    root_url "{root}"\n'
        f"    rebuild {rebuild}\n"
        '    sha256 cellar: :any_skip_relocation, '
        f'wasm32_kandelo: "{digest}"\n'
        "  end\n\n"
    )


class FormulaInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capture_policy = load_formula_build_inputs(
            TAP_ROOT / "Kandelo/staging/formula-build-inputs.toml",
            tap_root=TAP_ROOT,
        )
        cls.capture_catalog = generate_formula_capture_catalog(
            TAP_ROOT, cls.capture_policy
        )
        cls.probe = build_static_formula_probe(
            TAP_ROOT, cls.capture_policy
        )

    def test_generated_bottle_metadata_is_the_only_normalized_exclusion(self) -> None:
        first = _formula_source(
            bottle=_bottle(
                "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core",
                1,
                "1" * 64,
            )
        )
        second = _formula_source(
            bottle=_bottle(
                "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core",
                9,
                "9" * 64,
            )
        )
        self.assertEqual(normalize_formula_source(first), normalize_formula_source(second))
        baseline = hashlib.sha256(normalize_formula_source(first)).hexdigest()
        for label, old, new in [
            ("install", b'system "make"', b'system "make", "all"'),
            ("test", b'system "true"', b'system "false"'),
            ("source", b"demo-1.2.3", b"demo-1.2.4"),
            ("resource", b"demo.1", b"demo.2"),
            ("dependency", b"tap-core/zlib", b"tap-core/xz"),
            ("patch", b"diff --git a/a b/a", b"diff --git a/b b/b"),
        ]:
            with self.subTest(label=label):
                mutated = first.replace(old, new, 1)
                self.assertNotEqual(
                    hashlib.sha256(normalize_formula_source(mutated)).hexdigest(),
                    baseline,
                )

    def test_normalizer_rejects_multiple_or_malformed_bottle_blocks(self) -> None:
        block = _bottle("https://ghcr.io/v2/example/repository", 1, "1" * 64)
        with self.assertRaises(FormulaInventoryError):
            normalize_formula_source(_formula_source(bottle=block + block))
        malformed = block.replace("    rebuild 1\n", "    rebuild latest\n")
        with self.assertRaises(FormulaInventoryError):
            normalize_formula_source(_formula_source(bottle=malformed))
        nested = block.replace("  bottle do\n", "    bottle do\n")
        with self.assertRaises(FormulaInventoryError):
            normalize_formula_source(_formula_source(bottle=nested))

    def test_parser_records_sources_dependencies_native_inputs_and_architectures(self) -> None:
        parsed = parse_formula_source(
            "demo", "Formula/demo.rb", _formula_source(), ("wasm32",)
        )
        self.assertEqual(parsed["version"], "1.2.3")
        self.assertEqual(parsed["revision"], 2)
        self.assertEqual(parsed["rebuild"], 0)
        self.assertEqual(
            parsed["target_dependencies"],
            [{"name": "zlib", "scopes": ["runtime"]}],
        )
        self.assertEqual(
            parsed["native_requirements"],
            [{"identity": "wabt", "scopes": ["build"]}],
        )
        roles = [source["role"] for source in parsed["sources"]]
        self.assertEqual(roles, ["inline-patch:000", "primary", "resource:manual"])
        primary = next(source for source in parsed["sources"] if source["role"] == "primary")
        self.assertEqual(primary["mirrors"], ["https://mirror.example.test/demo-1.2.3.tar.gz"])

    def test_support_and_local_recipe_components_affect_combined_source_identity(self) -> None:
        formula_sha = "1" * 64
        first = combined_source_sha256(
            formula_sha,
            [{"path": "Kandelo/formula_support/support.rb", "sha256": "2" * 64}],
        )
        second = combined_source_sha256(
            formula_sha,
            [{"path": "Kandelo/formula_support/support.rb", "sha256": "3" * 64}],
        )
        self.assertNotEqual(first, second)

    def test_current_probe_is_exact_bounded_and_matches_protected_source(self) -> None:
        inventory = validate_formula_probe(
            TAP_ROOT,
            self.probe,
            self.capture_policy,
            self.capture_catalog,
        )
        self.assertEqual(len(inventory["formulae"]), 68)
        self.assertRegex(inventory["formula_tree"], r"^[0-9a-f]{40,64}$")
        self.assertRegex(inventory["sidecar_tree"], r"^[0-9a-f]{40,64}$")
        self.assertRegex(inventory["graph_sha256"], r"^[0-9a-f]{64}$")
        by_name = {entry["name"]: entry for entry in inventory["formulae"]}
        self.assertEqual(
            [dependency["name"] for dependency in by_name["curl"]["target_dependencies"]],
            ["libcurl", "openssl", "zlib"],
        )
        self.assertEqual(by_name["curl"]["architectures"], ["wasm32", "wasm64"])
        self.assertEqual(by_name["sqlite"]["architectures"], ["wasm32", "wasm64"])
        self.assertIn(
            {"name": "dash", "scopes": ["build", "test"]},
            by_name["nginx"]["target_dependencies"],
        )
        self.assertEqual(
            sorted(source["role"] for source in by_name["fbdoom"]["sources"]),
            ["primary", "resource:chocolate-doom", "resource:doom-shareware"],
        )

    def test_checked_in_current_fixture_is_canonical_and_repeatable(self) -> None:
        first = generate_formula_inventory(TAP_ROOT)
        second = generate_formula_inventory(TAP_ROOT)
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))
        fixture = TAP_ROOT / "Kandelo/staging/fixtures/formula-inventory.json"
        self.assertEqual(canonical_bytes(first), fixture.read_bytes())
        self.assertEqual(load_formula_inventory(fixture.read_bytes()), first)

    def test_probe_path_graph_architecture_and_identity_drift_fail_closed(self) -> None:
        mutations = []
        duplicate = copy.deepcopy(self.probe)
        duplicate["formulae"].append(copy.deepcopy(duplicate["formulae"][0]))
        mutations.append(("duplicate", duplicate))
        missing = copy.deepcopy(self.probe)
        missing["formulae"].pop()
        mutations.append(("missing", missing))
        escape = copy.deepcopy(self.probe)
        escape["formulae"][0]["formula_path"] = "../Formula/asa.rb"
        mutations.append(("escape", escape))
        architecture = copy.deepcopy(self.probe)
        architecture["formulae"][0]["architectures"] = ["wasm64"]
        mutations.append(("architecture", architecture))
        unknown = copy.deepcopy(self.probe)
        unknown["formulae"][0]["target_dependencies"] = [
            {"name": "not-first-party", "scopes": ["runtime"]}
        ]
        mutations.append(("unknown dependency", unknown))
        cycle = copy.deepcopy(self.probe)
        by_name = {entry["name"]: entry for entry in cycle["formulae"]}
        by_name["zlib"]["target_dependencies"] = [
            {"name": "curl", "scopes": ["runtime"]}
        ]
        mutations.append(("cycle", cycle))
        source_drift = copy.deepcopy(self.probe)
        source_drift["formulae"][0]["revision"] += 1
        mutations.append(("source drift", source_drift))
        for label, probe in mutations:
            with self.subTest(label=label):
                with self.assertRaises(FormulaInventoryError):
                    validate_formula_probe(
                        TAP_ROOT,
                        probe,
                        self.capture_policy,
                        self.capture_catalog,
                    )

    def test_sidecar_drift_is_rejected_without_making_sidecars_authoritative(self) -> None:
        parsed = next(
            entry for entry in self.probe["formulae"] if entry["name"] == "curl"
        )
        sidecar = json.loads((TAP_ROOT / "Kandelo/formula/curl.json").read_bytes())
        validate_legacy_sidecar(parsed, sidecar)
        for field, value in [
            ("formula_path", "Formula/other.rb"),
            ("formula_revision", 9),
            ("bottle_rebuild", 9),
            ("name", "other"),
        ]:
            with self.subTest(field=field):
                mutated = copy.deepcopy(sidecar)
                mutated[field] = value
                with self.assertRaises(FormulaInventoryError):
                    validate_legacy_sidecar(parsed, mutated)

    def test_probe_rejects_unknown_fields_and_dynamic_dependency_fallbacks(self) -> None:
        probe = copy.deepcopy(self.probe)
        probe["formulae"][0]["fallback"] = "legacy"
        with self.assertRaises(FormulaInventoryError):
            validate_formula_probe(
                TAP_ROOT, probe, self.capture_policy, self.capture_catalog
            )
        dynamic = _formula_source().replace(
            b'  depends_on "kandelo-dev/tap-core/zlib"\n',
            b"  depends_on computed_dependency\n",
        )
        with self.assertRaises(FormulaInventoryError):
            parse_formula_source("demo", "Formula/demo.rb", dynamic, ("wasm32",))


if __name__ == "__main__":
    unittest.main()
