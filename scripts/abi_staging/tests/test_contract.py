from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cli import main as cli_main
from scripts.abi_staging.contract import (
    ContractError,
    assess_capture,
    bottle_contract_digest,
    build_bottle_contract,
    build_miniature_bottle_contract_fixture,
    candidate_reuse_decision,
    changed_dependency_subjects,
    contract_from_build_context,
    load_bottle_contract,
    make_candidate_reuse_record,
    require_complete_capture,
    validate_candidate_reuse_record,
)
from scripts.abi_staging.plan import exact_formula_subject


TAP_ROOT = Path(__file__).resolve().parents[3]


def _component(character: str) -> str:
    return character * 64


def _inputs() -> dict[str, object]:
    return {
        "schema": 1,
        "kind": "kandelo-homebrew-bottle-contract",
        "target": {
            "abi": 8,
            "snapshot_sha256": _component("a"),
            "architecture": "wasm32",
        },
        "formula": {
            "name": "curl",
            "version": "8.11.1",
            "revision": 2,
            "rebuild": 3,
            "normalized_source_sha256": _component("b"),
            "source_components": [
                {"id": "formula", "sha256": _component("c")},
                {"id": "support", "sha256": _component("d")},
            ],
        },
        "kandelo_inputs": [
            {
                "id": "build-entrypoint",
                "kind": "file",
                "path": "scripts/homebrew-bottle-build.sh",
                "sha256": _component("e"),
            },
            {
                "id": "sdk",
                "kind": "tree",
                "path": "sdk",
                "sha256": _component("f"),
            },
        ],
        "tap_inputs": [
            {
                "id": "formula-support",
                "kind": "file",
                "path": "Kandelo/formula_support/kandelo_formula_support.rb",
                "sha256": _component("1"),
            }
        ],
        "sdk": {
            "policy_sha256": _component("2"),
            "component_sha256": _component("3"),
        },
        "libc": {
            "policy_sha256": _component("4"),
            "component_sha256": _component("5"),
        },
        "sysroot": {
            "policy_sha256": _component("6"),
            "component_sha256": _component("7"),
        },
        "toolchain": {
            "policy_sha256": _component("8"),
            "component_sha256": _component("9"),
        },
        "instrumentation": {
            "policy_sha256": _component("a"),
            "component_sha256": _component("b"),
        },
        "environment": {
            "policy_sha256": _component("c"),
            "variables_sha256": _component("d"),
        },
        "sources": [
            {
                "role": "patch:000",
                "url": "https://example.test/curl.patch",
                "sha256": _component("e"),
                "receipt_sha256": _component("f"),
            },
            {
                "role": "primary",
                "url": "https://example.test/curl.tar.xz",
                "sha256": _component("1"),
                "receipt_sha256": _component("2"),
            },
            {
                "role": "resource:manual",
                "url": "https://example.test/curl.1",
                "sha256": _component("3"),
                "receipt_sha256": _component("4"),
            },
        ],
        "native_inputs": [
            {
                "role": "pkgconf",
                "identity": "homebrew-core/pkgconf@2.3.0",
                "sha256": _component("5"),
                "receipt_sha256": _component("6"),
            }
        ],
        "direct_dependencies": [
            {
                "formula": "zlib",
                "architecture": "wasm32",
                "bottle_layer_sha256": _component("7"),
                "bottle_layer_bytes": 1234,
                "materialization_policy_sha256": _component("8"),
            }
        ],
        "build_policy_sha256": _component("9"),
    }


def _context() -> dict[str, object]:
    return {
        "contract_inputs": _inputs(),
        "provenance": {
            "pull_request": 19,
            "branch_hint": "refs/heads/feature",
            "exact_commit": "1" * 40,
            "exact_tree": "2" * 40,
            "request_digest": _component("a"),
            "run_id": 101,
            "job": "build",
            "producer_workflow": ".github/workflows/build.yml@refs/heads/main",
            "timestamp": "2026-08-09T00:00:00Z",
        },
    }


def _candidate(contract: dict[str, object]) -> dict[str, object]:
    digest = bottle_contract_digest(contract)
    return {
        "schema": 1,
        "kind": "kandelo-existing-candidate",
        "contract_sha256": digest,
        "formula": {
            "tap": "kandelo-dev/homebrew-tap-core",
            "formula": "curl",
            "architecture": "wasm32",
            "target_abi": 8,
        },
        "candidate_record": {
            "record_sha256": _component("a"),
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                f"record@sha256:{_component('a')}"
            ),
        },
        "source_custody": {
            "record_sha256": _component("b"),
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-source-custody/"
                f"record@sha256:{_component('b')}"
            ),
        },
        "bottle_layer": {
            "sha256": _component("c"),
            "bytes": 4567,
            "immutable_reference": (
                "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                f"curl@sha256:{_component('c')}"
            ),
        },
        "qualifying_receipts": [
            {
                "record_sha256": _component("d"),
                "immutable_reference": (
                    "ghcr.io/kandelo-dev/homebrew-tap-core-abi-8-candidates/"
                    f"receipt@sha256:{_component('d')}"
                ),
            }
        ],
        "original_producer": {
            "request_sha256": _component("e"),
            "head": "3" * 40,
            "run_id": 77,
        },
        "nonendorsed": True,
    }


def _new_request_context() -> dict[str, object]:
    return {
        "request_sha256": _component("f"),
        "source": {
            "repository": "automattic/kandelo",
            "commit": "4" * 40,
            "tree": "5" * 40,
        },
        "run": {
            "repository": "kandelo-dev/homebrew-tap-core",
            "workflow_ref": ".github/workflows/staging.yml@refs/heads/main",
            "run_id": 88,
            "run_attempt": 1,
            "job": "reuse",
        },
    }


class BottleContractTests(unittest.TestCase):
    def test_every_build_input_component_changes_contract_identity(self) -> None:
        baseline = bottle_contract_digest(build_bottle_contract(_inputs()))

        def architecture(value: dict[str, object]) -> None:
            value["target"]["architecture"] = "wasm64"
            value["direct_dependencies"][0]["architecture"] = "wasm64"

        mutations = [
            ("target ABI", lambda value: value["target"].__setitem__("abi", 9)),
            ("snapshot", lambda value: value["target"].__setitem__("snapshot_sha256", _component("0"))),
            ("architecture", architecture),
            ("Formula name", lambda value: value["formula"].__setitem__("name", "curl-next")),
            ("Formula version", lambda value: value["formula"].__setitem__("version", "8.11.2")),
            ("Formula revision", lambda value: value["formula"].__setitem__("revision", 4)),
            ("Formula rebuild", lambda value: value["formula"].__setitem__("rebuild", 5)),
            ("Formula source", lambda value: value["formula"].__setitem__("normalized_source_sha256", _component("0"))),
            ("Formula component", lambda value: value["formula"]["source_components"][0].__setitem__("sha256", _component("0"))),
            ("selected build path", lambda value: value["kandelo_inputs"][0].__setitem__("sha256", _component("0"))),
            ("tap support", lambda value: value["tap_inputs"][0].__setitem__("sha256", _component("0"))),
            ("SDK", lambda value: value["sdk"].__setitem__("component_sha256", _component("0"))),
            ("libc", lambda value: value["libc"].__setitem__("component_sha256", _component("0"))),
            ("sysroot", lambda value: value["sysroot"].__setitem__("component_sha256", _component("0"))),
            ("toolchain", lambda value: value["toolchain"].__setitem__("component_sha256", _component("0"))),
            ("instrumentation", lambda value: value["instrumentation"].__setitem__("component_sha256", _component("0"))),
            ("environment", lambda value: value["environment"].__setitem__("variables_sha256", _component("0"))),
            ("primary receipt", lambda value: value["sources"][1].__setitem__("receipt_sha256", _component("0"))),
            ("resource", lambda value: value["sources"][2].__setitem__("sha256", _component("0"))),
            ("patch", lambda value: value["sources"][0].__setitem__("sha256", _component("0"))),
            ("native receipt", lambda value: value["native_inputs"][0].__setitem__("receipt_sha256", _component("0"))),
            ("dependency layer", lambda value: value["direct_dependencies"][0].__setitem__("bottle_layer_sha256", _component("0"))),
            ("materialization peer", lambda value: value["direct_dependencies"][0].__setitem__("materialization_policy_sha256", _component("0"))),
            ("build policy", lambda value: value.__setitem__("build_policy_sha256", _component("0"))),
        ]
        for label, mutate in mutations:
            value = _inputs()
            mutate(value)
            with self.subTest(label=label):
                self.assertNotEqual(
                    bottle_contract_digest(build_bottle_contract(value)), baseline
                )

    def test_provenance_is_excluded_from_contract_and_layer_reuse_identity(self) -> None:
        first = contract_from_build_context(_context())
        second_context = _context()
        second_context["provenance"] = {
            "pull_request": 200,
            "branch_hint": "refs/heads/advanced",
            "exact_commit": "6" * 40,
            "exact_tree": "2" * 40,
            "request_digest": _component("0"),
            "run_id": 909,
            "job": "different-job",
            "producer_workflow": ".github/workflows/other.yml@refs/heads/main",
            "timestamp": "2030-01-01T00:00:00Z",
        }
        second = contract_from_build_context(second_context)
        self.assertEqual(bottle_contract_digest(first), bottle_contract_digest(second))

        candidate = _candidate(first)
        decision = candidate_reuse_decision(
            second,
            candidate,
            expected_source_custody_sha256=_component("b"),
        )
        self.assertEqual(decision["action"], "reuse")
        record = make_candidate_reuse_record(
            second,
            exact_formula_subject("curl", "wasm32"),
            candidate,
            _new_request_context(),
        )
        validate_candidate_reuse_record(record)
        self.assertEqual(
            canonical_sha256(record),
            "db70ec2851481d96c4fd88a4a659de77537afc3afd146bda2a44f93b9fb23b6e",
        )
        self.assertEqual(record["common"]["request_sha256"], _component("f"))
        self.assertEqual(
            record["candidate_reuse"]["original_producer"],
            candidate["original_producer"],
        )

    def test_capture_is_content_based_complete_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kandelo = root / "kandelo"
            tap = root / "tap"
            (kandelo / "sdk").mkdir(parents=True)
            (tap / "Kandelo").mkdir(parents=True)
            (kandelo / "sdk/tool.txt").write_text("tool\n", encoding="utf-8")
            (kandelo / "build.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (kandelo / "build.sh").chmod(0o755)
            (tap / "Kandelo/support.rb").write_text("support\n", encoding="utf-8")
            subject = exact_formula_subject("curl", "wasm32")
            assessment = assess_capture(
                subject=subject,
                affected_products=["alpha-shell"],
                kandelo_root=kandelo,
                tap_root=tap,
                kandelo_paths=["build.sh", "sdk"],
                tap_paths=["Kandelo/support.rb"],
                observed_kandelo_paths=["build.sh", "sdk/tool.txt"],
                observed_tap_paths=["Kandelo/support.rb"],
            )
            self.assertTrue(assessment["complete"])
            require_complete_capture(assessment, subject)
            first = {
                (entry["repository"], entry["path"]): entry["sha256"]
                for entry in assessment["captured"]
            }
            (kandelo / "sdk/tool.txt").write_text("changed\n", encoding="utf-8")
            changed = assess_capture(
                subject=subject,
                affected_products=["alpha-shell"],
                kandelo_root=kandelo,
                tap_root=tap,
                kandelo_paths=["build.sh", "sdk"],
                tap_paths=["Kandelo/support.rb"],
                observed_kandelo_paths=["build.sh", "sdk/tool.txt"],
                observed_tap_paths=["Kandelo/support.rb"],
            )
            second = {
                (entry["repository"], entry["path"]): entry["sha256"]
                for entry in changed["captured"]
            }
            self.assertNotEqual(first[("kandelo", "sdk")], second[("kandelo", "sdk")])

            incomplete = assess_capture(
                subject=subject,
                affected_products=["alpha-shell"],
                kandelo_root=kandelo,
                tap_root=tap,
                kandelo_paths=["missing"],
                tap_paths=["Kandelo/support.rb"],
                observed_kandelo_paths=["missing", "undeclared.txt"],
                observed_tap_paths=["Kandelo/support.rb"],
            )
            self.assertFalse(incomplete["complete"])
            self.assertEqual(incomplete["override_subject"], subject)
            self.assertEqual(incomplete["missing"][0]["path"], "missing")
            self.assertEqual(incomplete["ambiguous"][0]["path"], "undeclared.txt")
            with self.assertRaises(ContractError):
                require_complete_capture(incomplete, subject)

            forged = copy.deepcopy(assessment)
            forged["captured"] = [{"unexpected": "trusted"}]
            with self.assertRaises(ContractError):
                require_complete_capture(forged, subject)

            contradictory = copy.deepcopy(assessment)
            contradictory["missing"] = [
                {"repository": "kandelo", "path": "sdk", "reason": "missing"}
            ]
            with self.assertRaises(ContractError):
                require_complete_capture(contradictory, subject)

    def test_capture_rejects_symlinks_in_the_absence_of_explicit_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kandelo = root / "kandelo"
            tap = root / "tap"
            kandelo.mkdir()
            tap.mkdir()
            (kandelo / "real").write_text("bytes\n", encoding="utf-8")
            (kandelo / "link").symlink_to("real")
            assessment = assess_capture(
                subject=exact_formula_subject("curl", "wasm32"),
                affected_products=["alpha-shell"],
                kandelo_root=kandelo,
                tap_root=tap,
                kandelo_paths=["link"],
                tap_paths=[],
                observed_kandelo_paths=["link"],
                observed_tap_paths=[],
            )
            self.assertFalse(assessment["complete"])
            self.assertEqual(assessment["ambiguous"][0]["reason"], "symlink-not-authorized")

    def test_reuse_decisions_rebuild_on_change_and_reject_false_identity(self) -> None:
        contract = build_bottle_contract(_inputs())
        candidate = _candidate(contract)
        self.assertEqual(
            candidate_reuse_decision(
                contract,
                candidate,
                expected_source_custody_sha256=_component("b"),
            )["action"],
            "reuse",
        )
        changed = _inputs()
        changed["formula"]["rebuild"] += 1
        self.assertEqual(
            candidate_reuse_decision(
                build_bottle_contract(changed),
                candidate,
                expected_source_custody_sha256=_component("b"),
            )["action"],
            "rebuild",
        )

        mutations = [
            ("custody", lambda value: value["source_custody"].__setitem__("record_sha256", _component("0"))),
            ("layer", lambda value: value["bottle_layer"].__setitem__("sha256", _component("0"))),
            ("ABI", lambda value: value["formula"].__setitem__("target_abi", 9)),
            ("architecture", lambda value: value["formula"].__setitem__("architecture", "wasm64")),
            ("endorsement", lambda value: value.__setitem__("nonendorsed", False)),
        ]
        for label, mutate in mutations:
            invalid = copy.deepcopy(candidate)
            mutate(invalid)
            with self.subTest(label=label), self.assertRaises(ContractError):
                candidate_reuse_decision(
                    contract,
                    invalid,
                    expected_source_custody_sha256=_component("b"),
                )

    def test_dependency_layer_changes_are_exact_and_unchanged_rebuilds_are_ignored(self) -> None:
        before = build_bottle_contract(_inputs())
        changed = _inputs()
        changed["direct_dependencies"][0]["bottle_layer_sha256"] = _component("0")
        after = build_bottle_contract(changed)
        self.assertEqual(
            changed_dependency_subjects(before, after),
            [exact_formula_subject("zlib", "wasm32")],
        )
        rebuilt_same_layer = build_bottle_contract(_inputs())
        self.assertEqual(changed_dependency_subjects(before, rebuilt_same_layer), [])

    def test_checked_contract_fixture_is_canonical_and_repeatable(self) -> None:
        expected = build_miniature_bottle_contract_fixture()
        fixture = TAP_ROOT / "Kandelo/staging/fixtures/bottle-contract.json"
        self.assertEqual(canonical_bytes(expected), fixture.read_bytes())
        self.assertEqual(load_bottle_contract(fixture.read_bytes()), expected)
        self.assertEqual(
            hashlib.sha256(canonical_bytes(expected)).hexdigest(),
            bottle_contract_digest(expected),
        )

    def test_contract_and_reuse_cli_write_only_valid_canonical_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context_path = root / "context.json"
            contract_path = root / "contract.json"
            candidate_path = root / "candidate.json"
            request_path = root / "new-request.json"
            reuse_path = root / "reuse.json"
            context_path.write_bytes(canonical_bytes(_context()))

            self.assertEqual(
                cli_main(
                    [
                        "contract",
                        "--input",
                        str(context_path),
                        "--out",
                        str(contract_path),
                    ]
                ),
                0,
            )
            contract = load_bottle_contract(contract_path.read_bytes())
            candidate_path.write_bytes(canonical_bytes(_candidate(contract)))
            request_path.write_bytes(canonical_bytes(_new_request_context()))
            self.assertEqual(
                cli_main(
                    [
                        "reuse",
                        "--contract",
                        str(contract_path),
                        "--candidate",
                        str(candidate_path),
                        "--expected-source-custody-sha256",
                        _component("b"),
                        "--subject",
                        exact_formula_subject("curl", "wasm32"),
                        "--new-request",
                        str(request_path),
                        "--out",
                        str(reuse_path),
                    ]
                ),
                0,
            )
            reuse = json.loads(reuse_path.read_bytes())
            validate_candidate_reuse_record(reuse)
            self.assertEqual(canonical_bytes(reuse), reuse_path.read_bytes())

            fixture = TAP_ROOT / "Kandelo/staging/fixtures/bottle-contract.json"
            self.assertEqual(
                cli_main(["fixture-check", "--fixture", str(fixture)]),
                0,
            )


if __name__ == "__main__":
    unittest.main()
