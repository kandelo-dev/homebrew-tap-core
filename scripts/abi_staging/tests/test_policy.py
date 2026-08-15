from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import tempfile
import unittest

from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.policy import (
    PolicyError,
    attempt_repository,
    candidate_repository,
    check_policy_files,
    generate_formula_capture_catalog,
    load_candidate_publication_activation,
    load_formula_build_inputs,
    load_tap_staging_policy,
    load_verification_tests,
    source_custody_repository,
)


TAP_ROOT = Path(__file__).resolve().parents[3]


class PolicyTests(unittest.TestCase):
    def test_attempt_repository_is_nested_under_generic_candidate_subject(self) -> None:
        policy = load_tap_staging_policy(TAP_ROOT / "Kandelo/staging/tap-policy.toml")
        self.assertEqual(
            attempt_repository(policy, 8, formula="mini-tool"),
            "kandelo-dev/homebrew-tap-core-abi-8-candidates/mini-tool/attempts",
        )

    def setUp(self) -> None:
        self.staging = TAP_ROOT / "Kandelo/staging"

    def test_tap_policy_has_exact_limits_and_generic_namespaces(self) -> None:
        policy = load_tap_staging_policy(self.staging / "tap-policy.toml")
        self.assertEqual(policy.version, 1)
        self.assertEqual(policy.tap_repository, "kandelo-dev/homebrew-tap-core")
        self.assertEqual(policy.kandelo_repository, "Automattic/kandelo")
        self.assertEqual(policy.max_ready_subjects_per_cycle, 64)
        self.assertEqual(policy.max_formulae, 256)
        self.assertEqual(policy.max_edges, 4096)
        self.assertEqual(policy.max_handoff_files, 256)
        self.assertEqual(policy.max_handoff_bytes, 4_294_967_296)
        self.assertEqual(policy.max_record_bytes, 4_194_304)
        self.assertEqual(policy.build_timeout_minutes, 360)
        self.assertEqual(policy.verification_timeout_minutes, 360)
        self.assertEqual(policy.automatic_retry_count, 3)
        self.assertEqual(policy.retry_base_ms, 60_000)
        self.assertEqual(policy.retry_cap_ms, 900_000)
        self.assertEqual(policy.candidate_retention_days_after_unmerged_close, 30)
        self.assertEqual(
            candidate_repository(policy, 7, formula="bash"),
            "kandelo-dev/homebrew-tap-core-abi-7-candidates/bash",
        )
        self.assertEqual(
            candidate_repository(policy, 8, formula="zlib"),
            "kandelo-dev/homebrew-tap-core-abi-8-candidates/zlib",
        )
        self.assertEqual(
            source_custody_repository(policy, 8),
            "kandelo-dev/homebrew-tap-core-abi-8-source-custody",
        )
        policy_text = (self.staging / "tap-policy.toml").read_text()
        self.assertIsNone(re.search(r"(?i)abi[-_ ]?4[23]", policy_text))

    def test_candidate_owner_must_match_tap_owner(self) -> None:
        source = self.staging / "tap-policy.toml"
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "tap-policy.toml"
            candidate.write_text(
                source.read_text().replace(
                    'candidate_owner = "kandelo-dev"',
                    'candidate_owner = "Automattic"',
                )
            )
            with self.assertRaisesRegex(PolicyError, "tap owner"):
                load_tap_staging_policy(candidate)

    def test_checked_in_candidate_publication_is_active_after_reconciliation_canary(self) -> None:
        activation_path = self.staging / "candidate-publication-activation.toml"
        self.assertEqual(load_candidate_publication_activation(activation_path), "active")
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "activation.toml"
            candidate.write_text(
                'schema = 1\nkind = "kandelo-candidate-publication-activation"\n'
                'mode = "active"\n'
            )
            self.assertEqual(load_candidate_publication_activation(candidate), "active")
            candidate.write_text(candidate.read_text() + 'fallback = "legacy"\n')
            with self.assertRaisesRegex(PolicyError, "fields changed"):
                load_candidate_publication_activation(candidate)

    def test_formula_policy_covers_every_direct_formula_exactly_once(self) -> None:
        policy = load_formula_build_inputs(
            self.staging / "formula-build-inputs.toml", tap_root=TAP_ROOT
        )
        formula_files = sorted(path.stem for path in (TAP_ROOT / "Formula").glob("*.rb"))
        self.assertEqual([entry.name for entry in policy.formulae], formula_files)
        self.assertEqual(len(set(formula_files)), len(policy.formulae))
        dual_architecture = {
            "curl",
            "libcurl",
            "libcxx",
            "musl-fts",
            "openssl",
            "sqlite",
            "zlib",
        }
        for entry in policy.formulae:
            expected = (
                ("wasm32", "wasm64")
                if entry.name in dual_architecture
                else ("wasm32",)
            )
            self.assertEqual(entry.architectures, expected)
            self.assertTrue(entry.profiles)
            self.assertEqual(tuple(sorted(entry.profiles)), entry.profiles)

    def test_musl_fts_binds_both_relocated_automake_macro_roots(self) -> None:
        source = (TAP_ROOT / "Formula/musl-fts.rb").read_text(encoding="utf-8")
        self.assertIn('ENV["AUTOMAKE_LIBDIR"] = automake_modules.to_s', source)
        self.assertNotIn('ENV.prepend_path "PERL5LIB", automake_modules', source)
        self.assertIn(
            'ENV.prepend_path "ACLOCAL_PATH", Formula["libtool"].opt_share/"aclocal"',
            source,
        )
        self.assertIn("--automake-acdir=#{automake_macros}", source)
        self.assertIn("--system-acdir=#{automake_macros}", source)

    def test_sudo_configure_owns_flags_without_overriding_submake_cppflags(self) -> None:
        source = (TAP_ROOT / "Kandelo/recipes/sudo/build.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('export CFLAGS="-O2 -D_GNU_SOURCE $PREFIX_MAPS"', source)
        self.assertIn(
            'PREFIX_MAPS+=" -fdebug-compilation-dir=/usr/src/sudo-1.9.17p2"',
            source,
        )
        self.assertNotIn('        CFLAGS="-O2 -D_GNU_SOURCE $PREFIX_MAPS"', source)
        self.assertIn('    "$MAKE" -j2\n', source)
        self.assertNotIn('"$MAKE" -j2 CFLAGS=', source)
        self.assertNotIn('        CPPFLAGS="$PREFIX_MAPS"', source)

    def test_expanded_capture_matches_observed_build_entrypoints(self) -> None:
        policy = load_formula_build_inputs(
            self.staging / "formula-build-inputs.toml", tap_root=TAP_ROOT
        )
        catalog = generate_formula_capture_catalog(TAP_ROOT, policy)
        by_name = {entry["name"]: entry for entry in catalog["formulae"]}
        for entry in catalog["formulae"]:
            self.assertIn(
                "Kandelo/formula_support/kandelo_formula_support.rb",
                entry["tap_paths"],
            )
            self.assertNotIn("Kandelo/formula_support", entry["tap_paths"])
        self.assertIn(
            "Kandelo/formula_support/run-network-wasm.ts",
            by_name["nginx"]["tap_paths"],
        )
        for name in [
            "bc",
            "erlang",
            "fbdoom",
            "lsof",
            "modeset",
            "netcat",
            "nethack",
            "posix-utils-lite",
        ]:
            self.assertIn(f"packages/registry/{name}", by_name[name]["kandelo_paths"])
        self.assertIn("packages/registry/cpython", by_name["python"]["kandelo_paths"])
        self.assertIn(
            "Kandelo/recipes/homebrew-bootstrap",
            by_name["homebrew-bootstrap"]["tap_paths"],
        )
        self.assertIn("Kandelo/recipes/ruby", by_name["ruby"]["tap_paths"])
        self.assertIn("Kandelo/patches/perl", by_name["perl"]["tap_paths"])
        self.assertIn(
            "images/rootfs/etc/ssl/cert.pem", by_name["git"]["kandelo_paths"]
        )
        self.assertIn(
            "images/rootfs/etc/ssl/cert.pem", by_name["wget"]["kandelo_paths"]
        )

    def test_generated_catalog_is_canonical_fresh_and_contains_path_strings_only(self) -> None:
        policy = load_formula_build_inputs(
            self.staging / "formula-build-inputs.toml", tap_root=TAP_ROOT
        )
        generated = generate_formula_capture_catalog(TAP_ROOT, policy)
        generated_path = self.staging / "generated/formula-build-inputs.json"
        self.assertEqual(canonical_bytes(generated), generated_path.read_bytes())
        for entry in generated["formulae"]:
            self.assertEqual(entry["architectures"], sorted(entry["architectures"]))
            self.assertEqual(entry["kandelo_paths"], sorted(entry["kandelo_paths"]))
            self.assertEqual(entry["tap_paths"], sorted(entry["tap_paths"]))
            self.assertNotIn("content_sha256", entry)
            self.assertRegex(entry["capture_policy_sha256"], r"^[0-9a-f]{64}$")
        check_policy_files(TAP_ROOT)

    def test_verification_definitions_are_bounded_and_digest_stable(self) -> None:
        definitions = load_verification_tests(self.staging / "verification-tests.toml")
        self.assertEqual(
            [definition.id for definition in definitions],
            ["bottle-structure", "public-candidate-browser", "public-candidate-node"],
        )
        for definition in definitions:
            self.assertRegex(definition.sha256, r"^[0-9a-f]{64}$")
            self.assertTrue(definition.kandelo_paths)

    def test_unknown_intent_cycles_unsafe_paths_and_inventory_drift_fail_closed(self) -> None:
        source_path = self.staging / "formula-build-inputs.toml"
        source = source_path.read_text()
        mutations = {
            "runtime dependency intent": source.replace(
                'name = "asa"\n', 'name = "asa"\ndependencies = ["zlib"]\n', 1
            ),
            "unsafe path": source.replace('"sdk",', '"../sdk",', 1),
            "profile cycle": source.replace(
                '[profiles.kandelo-common]\n',
                '[profiles.kandelo-common]\nprofiles = ["kandelo-common"]\n',
                1,
            ),
            "missing formula": source.replace(
                '[[formulae]]\nname = "asa"\n', '[[formulae]]\nname = "absent"\n', 1
            ),
            "unsupported architecture": source.replace(
                'architectures = ["wasm32"]', 'architectures = ["native"]', 1
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            for label, body in mutations.items():
                with self.subTest(label=label):
                    candidate = Path(directory) / f"{label.replace(' ', '-')}.toml"
                    candidate.write_text(body)
                    with self.assertRaises(PolicyError):
                        load_formula_build_inputs(candidate, tap_root=TAP_ROOT)

    def test_missing_observed_input_reports_exact_formula_architecture_and_subject(self) -> None:
        source_path = self.staging / "formula-build-inputs.toml"
        source = source_path.read_text().replace(
            'kandelo_paths = ["packages/registry/cpython"]',
            "kandelo_paths = []",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "formula-build-inputs.toml"
            candidate.write_text(source)
            policy = load_formula_build_inputs(candidate, tap_root=TAP_ROOT)
            with self.assertRaises(PolicyError) as raised:
                generate_formula_capture_catalog(TAP_ROOT, policy)
        message = str(raised.exception)
        self.assertIn("python", message)
        self.assertIn("wasm32", message)
        self.assertIn("packages/registry/cpython", message)
        self.assertIn('"kind":"formula"', message)


if __name__ == "__main__":
    unittest.main()
    attempt_repository,
