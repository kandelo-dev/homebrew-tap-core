from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
from unittest import mock
import unittest

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.cli import main as cli_main
from scripts.abi_staging.formula_inventory import load_formula_inventory
from scripts.abi_staging.plan import (
    PlanError,
    build_miniature_tap_plan_fixture,
    exact_formula_subject,
    load_formula_requirements,
    plan_request,
    reverse_dependants,
    snapshot_tap_source,
)
from scripts.abi_staging.records import TapRecordError, load_tap_plan_record


TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
REQUEST_FIXTURE = TAP_ROOT / "Kandelo/staging/fixtures/request/current-request.json"
INVENTORY_FIXTURE = TAP_ROOT / "Kandelo/staging/fixtures/formula-inventory.json"


def _miniature_inventory() -> dict[str, object]:
    current = load_formula_inventory(INVENTORY_FIXTURE.read_bytes())
    names = {
        "asa",
        "bash",
        "curl",
        "libcurl",
        "libcxx",
        "ncurses",
        "openssl",
        "zlib",
    }
    formulae = [entry for entry in current["formulae"] if entry["name"] in names]
    graph = [
        {
            "name": entry["name"],
            "target_dependencies": [
                dependency["name"] for dependency in entry["target_dependencies"]
            ],
        }
        for entry in formulae
    ]
    return {
        **current,
        "disabled_formulae": [],
        "formulae": formulae,
        "graph_sha256": canonical_sha256(graph),
    }


def _request() -> dict[str, object]:
    request = json.loads(REQUEST_FIXTURE.read_bytes())
    requirements = request["requirements"]
    requirements["products"] = [
        {
            "id": "alpha-shell",
            "path": "images/vfs/products/alpha-shell.toml",
            "manifest_sha256": "a" * 64,
        },
        {
            "id": "beta-tools",
            "path": "images/vfs/products/beta-tools.toml",
            "manifest_sha256": "b" * 64,
        },
    ]
    requirements["evidence"] = [
        {
            "product_id": "alpha-shell",
            "applicability": "required",
            "node": ["alpha-node"],
            "browser": ["alpha-browser"],
        },
        {
            "product_id": "beta-tools",
            "applicability": "required",
            "node": ["beta-node"],
            "browser": [],
        },
    ]
    requirements["digest"] = canonical_sha256(
        {
            "change_classes": requirements["change_classes"],
            "products": requirements["products"],
            "registries": requirements["registries"],
            "evidence": requirements["evidence"],
        }
    )
    return request


def _refresh_inventory_graph(inventory: dict[str, object]) -> None:
    inventory["graph_sha256"] = canonical_sha256(
        [
            {
                "name": entry["name"],
                "target_dependencies": [
                    dependency["name"] for dependency in entry["target_dependencies"]
                ],
            }
            for entry in inventory["formulae"]
        ]
    )


def _requirements() -> list[dict[str, object]]:
    return [
        {
            "tap": "kandelo-dev/homebrew-tap-core",
            "formula": "bash",
            "architecture": "wasm32",
            "uses": [
                {"product_id": "beta-tools", "materialization": "embedded"}
            ],
        },
        {
            "tap": "kandelo-dev/homebrew-tap-core",
            "formula": "curl",
            "architecture": "wasm32",
            "uses": [
                {"product_id": "alpha-shell", "materialization": "lazy"}
            ],
        },
        {
            "tap": "kandelo-dev/homebrew-tap-core",
            "formula": "libcurl",
            "architecture": "wasm32",
            "uses": [
                {"product_id": "beta-tools", "materialization": "embedded"}
            ],
        },
    ]


def _tap_source() -> dict[str, str]:
    return {
        "repository": "kandelo-dev/homebrew-tap-core",
        "commit": "7" * 40,
        "tree": "8" * 40,
    }


def _plan(
    *,
    request: dict[str, object] | None = None,
    requirements: list[dict[str, object]] | None = None,
    inventory: dict[str, object] | None = None,
    request_asset_url: str | None = None,
) -> dict[str, object]:
    selected_request = request or _request()
    digest = canonical_sha256(selected_request)
    head = selected_request["build_source"]["commit"]
    asset = f"candidate-request-{head}-sha256-{digest}.json"
    return plan_request(
        selected_request,
        request_digest=digest,
        request_asset_url=request_asset_url
        or (
            "https://github.com/Automattic/kandelo/releases/download/"
            f"abi-staging-pr-19/{asset}"
        ),
        tap_source=_tap_source(),
        inventory=inventory or _miniature_inventory(),
        formula_requirements=requirements or _requirements(),
    )


def _formula_by_name(plan: dict[str, object], name: str) -> dict[str, object]:
    return next(
        formula
        for formula in plan["formulae"]
        if formula["identity"]["name"] == name
        and formula["identity"]["architecture"] == "wasm32"
    )


class TapPlanTests(unittest.TestCase):
    def test_disabled_background_formula_is_not_planned(self) -> None:
        inventory = _miniature_inventory()
        inventory["disabled_formulae"] = ["asa"]
        plan = _plan(inventory=inventory)

        self.assertNotIn(
            exact_formula_subject("asa", "wasm32"),
            plan["background_subjects"],
        )
        self.assertNotIn(
            "asa",
            {formula["identity"]["name"] for formula in plan["formulae"]},
        )

    def test_disabled_required_formula_or_dependency_fails_closed(self) -> None:
        inventory = _miniature_inventory()
        inventory["disabled_formulae"] = ["bash"]
        with self.assertRaisesRegex(PlanError, "selected Formula root bash is disabled"):
            _plan(inventory=inventory)

        inventory = _miniature_inventory()
        inventory["disabled_formulae"] = ["zlib"]
        with self.assertRaisesRegex(PlanError, "active Formula .* depends on disabled zlib"):
            _plan(inventory=inventory)

    def test_snapshots_an_exact_checkout_with_protected_ownership(self) -> None:
        expected = snapshot_tap_source(
            TAP_ROOT, "kandelo-dev/homebrew-tap-core"
        )
        with mock.patch.dict(
            os.environ, {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
        ):
            source = snapshot_tap_source(
                TAP_ROOT, "kandelo-dev/homebrew-tap-core"
            )

        self.assertEqual(source, expected)

    def test_request_url_names_the_exact_repository_pr_and_release(self) -> None:
        request = _request()
        digest = canonical_sha256(request)
        head = request["build_source"]["commit"]
        asset = f"candidate-request-{head}-sha256-{digest}.json"
        content_addressed = (
            "https://github.com/Automattic/kandelo/releases/download/"
            f"abi-staging-pr-19-sha256-{digest}/{asset}"
        )
        self.assertEqual(_plan(request_asset_url=content_addressed)["schema"], 1)

        hostile_urls = (
            content_addressed.replace("Automattic/kandelo", "other/project"),
            content_addressed.replace("abi-staging-pr-19-", "abi-staging-pr-20-"),
            content_addressed.replace(
                f"sha256-{digest}/", f"sha256-{'f' * 64}/"
            ),
        )
        for url in hostile_urls:
            with self.subTest(url=url), self.assertRaises(PlanError):
                _plan(request_asset_url=url)

    def test_required_closure_reasons_background_and_order_come_from_products(self) -> None:
        plan = _plan()
        required = {
            json.loads(subject)["identity"] for subject in plan["required_subjects"]
        }
        self.assertEqual(
            required,
            {"bash", "curl", "libcurl", "libcxx", "ncurses", "openssl", "zlib"},
        )
        self.assertEqual(
            set(plan["background_subjects"]),
            {
                exact_formula_subject("asa", "wasm32"),
                exact_formula_subject("curl", "wasm64"),
                exact_formula_subject("libcurl", "wasm64"),
                exact_formula_subject("libcxx", "wasm64"),
                exact_formula_subject("openssl", "wasm64"),
                exact_formula_subject("zlib", "wasm64"),
            },
        )
        self.assertEqual(
            _formula_by_name(plan, "libcurl")["required_by_products"],
            ["alpha-shell", "beta-tools"],
        )
        self.assertEqual(
            _formula_by_name(plan, "zlib")["required_by_products"],
            ["alpha-shell", "beta-tools"],
        )
        positions = {subject: index for index, subject in enumerate(plan["required_subjects"])}
        for formula in plan["formulae"]:
            subject = exact_formula_subject(
                formula["identity"]["name"], formula["identity"]["architecture"]
            )
            if subject not in positions:
                continue
            for dependency in formula["direct_dependencies"]:
                dependency_subject = exact_formula_subject(
                    dependency["formula"], dependency["architecture"]
                )
                self.assertLess(positions[dependency_subject], positions[subject])

        roots = {
            product["id"]: product["formula_roots"]
            for product in plan["selected_products"]
        }
        self.assertIn(
            {
                "tap": "kandelo-dev/homebrew-tap-core",
                "formula": "curl",
                "architecture": "wasm32",
                "materialization": "lazy",
            },
            roots["alpha-shell"],
        )
        self.assertIn(
            {
                "tap": "kandelo-dev/homebrew-tap-core",
                "formula": "libcurl",
                "architecture": "wasm32",
                "materialization": "embedded",
            },
            roots["beta-tools"],
        )

    def test_materialization_changes_policy_not_membership(self) -> None:
        first = _plan()
        requirements = _requirements()
        requirements[1]["uses"][0]["materialization"] = "embedded"
        second = _plan(requirements=requirements)
        self.assertEqual(first["required_subjects"], second["required_subjects"])
        first_dependency = _formula_by_name(first, "curl")["direct_dependencies"][0]
        second_dependency = _formula_by_name(second, "curl")["direct_dependencies"][0]
        self.assertNotEqual(
            first_dependency["materialization_policy_sha256"],
            second_dependency["materialization_policy_sha256"],
        )

    def test_reverse_dependants_are_derived_from_direct_edges(self) -> None:
        reverse = reverse_dependants(_plan())
        zlib = exact_formula_subject("zlib", "wasm32")
        self.assertEqual(
            set(reverse[zlib]),
            {
                exact_formula_subject("curl", "wasm32"),
                exact_formula_subject("libcurl", "wasm32"),
            },
        )

    def test_unrelated_lists_or_background_records_cannot_change_membership(self) -> None:
        baseline = _plan()
        unrelated_brewfile = ["not-selected"]
        unrelated_staging_list = ["also-not-selected"]
        legacy_wave = {"wave": ["never-authoritative"]}
        background_records = {exact_formula_subject("asa", "wasm32"): "failed"}
        self.assertTrue(unrelated_brewfile and unrelated_staging_list and legacy_wave)
        self.assertTrue(background_records)
        self.assertNotIn("records", inspect.signature(plan_request).parameters)
        self.assertEqual(_plan()["required_subjects"], baseline["required_subjects"])

    def test_roots_reject_missing_formula_third_party_tap_and_extra_authority(self) -> None:
        for label, field, value in [
            ("missing", "formula", "missing"),
            ("third-party", "tap", "other/tap"),
        ]:
            requirements = _requirements()
            requirements[0][field] = value
            with self.subTest(label=label), self.assertRaises(PlanError):
                _plan(requirements=requirements)

        for field, value in [
            ("transitive_dependencies", ["zlib"]),
            ("build_order", 1),
        ]:
            requirements = _requirements()
            requirements[0][field] = value
            with self.subTest(field=field), self.assertRaises(PlanError):
                _plan(requirements=requirements)

    def test_graph_rejects_cycles_architecture_gaps_duplicates_and_overflow(self) -> None:
        cycle = _miniature_inventory()
        zlib = next(item for item in cycle["formulae"] if item["name"] == "zlib")
        zlib["target_dependencies"] = [{"name": "curl", "scopes": ["runtime"]}]
        _refresh_inventory_graph(cycle)
        with self.assertRaises(PlanError):
            _plan(inventory=cycle)

        gap = _miniature_inventory()
        libcurl = next(item for item in gap["formulae"] if item["name"] == "libcurl")
        libcurl["architectures"] = ["wasm32"]
        requirements = _requirements()
        for requirement in requirements:
            if requirement["formula"] in {"curl", "libcurl"}:
                requirement["architecture"] = "wasm64"
        requirements = [item for item in requirements if item["formula"] != "bash"]
        with self.assertRaises(PlanError):
            _plan(requirements=requirements, inventory=gap)

        duplicate = _miniature_inventory()
        duplicate["formulae"].append(copy.deepcopy(duplicate["formulae"][0]))
        with self.assertRaises(PlanError):
            _plan(inventory=duplicate)

        with mock.patch("scripts.abi_staging.plan.MAX_GRAPH_EDGES", 1):
            with self.assertRaises(PlanError):
                _plan()

    def test_root_products_must_be_request_bound(self) -> None:
        requirements = _requirements()
        requirements[0]["uses"][0]["product_id"] = "not-selected"
        with self.assertRaises(PlanError):
            _plan(requirements=requirements)

    def test_record_loader_rejects_duplicate_nodes_and_product_subject_namespace(self) -> None:
        plan = _plan()
        duplicate = copy.deepcopy(plan)
        duplicate["formulae"].append(copy.deepcopy(duplicate["formulae"][0]))
        with self.assertRaises(TapRecordError):
            load_tap_plan_record(canonical_bytes(duplicate))

        wrong_subject = copy.deepcopy(plan)
        wrong_subject["required_subjects"][0] = canonical_bytes(
            {"architecture": "wasm32", "identity": "alpha-shell", "kind": "product"}
        ).decode().strip()
        with self.assertRaises(TapRecordError):
            load_tap_plan_record(canonical_bytes(wrong_subject))
        with self.assertRaises(TapRecordError):
            load_tap_plan_record(b'{"schema":1, "kind":"not-canonical"}\n')

    def test_formula_requirements_are_canonical_bounded_and_exact(self) -> None:
        body = canonical_bytes(_requirements())
        self.assertEqual(load_formula_requirements(body), _requirements())
        pretty = json.dumps(_requirements(), indent=2).encode() + b"\n"
        with self.assertRaises(PlanError):
            load_formula_requirements(pretty)

    def test_checked_miniature_plan_fixture_is_canonical_and_repeatable(self) -> None:
        expected = build_miniature_tap_plan_fixture(TAP_ROOT)
        fixture = TAP_ROOT / "Kandelo/staging/fixtures/tap-plan.json"
        self.assertEqual(canonical_bytes(expected), fixture.read_bytes())
        self.assertEqual(load_tap_plan_record(fixture.read_bytes()), expected)
        self.assertEqual(
            hashlib.sha256(canonical_bytes(expected)).hexdigest(),
            hashlib.sha256(fixture.read_bytes()).hexdigest(),
        )

    def test_plan_request_cli_snapshots_the_exact_tap_and_writes_a_record(self) -> None:
        request_body = REQUEST_FIXTURE.read_bytes()
        request = json.loads(request_body)
        digest = hashlib.sha256(request_body).hexdigest()
        head = request["build_source"]["commit"]
        asset = f"candidate-request-{head}-sha256-{digest}.json"
        requirements = [
            {
                "tap": "kandelo-dev/homebrew-tap-core",
                "formula": "curl",
                "architecture": "wasm32",
                "uses": [
                    {"product_id": "fixture-shell", "materialization": "lazy"}
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requirements_path = root / "formula-requirements.json"
            output = root / "tap-plan.json"
            requirements_path.write_bytes(canonical_bytes(requirements))
            result = cli_main(
                [
                    "plan-request",
                    "--tap-root",
                    str(TAP_ROOT),
                    "--request",
                    str(REQUEST_FIXTURE),
                    "--request-asset-url",
                    (
                        "https://github.com/Automattic/kandelo/releases/download/"
                        f"abi-staging-pr-19/{asset}"
                    ),
                    "--formula-requirements",
                    str(requirements_path),
                    "--out",
                    str(output),
                ]
            )
            self.assertEqual(result, 0)
            planned = load_tap_plan_record(output.read_bytes())
            self.assertEqual(
                planned["tap_source"],
                snapshot_tap_source(TAP_ROOT, "kandelo-dev/homebrew-tap-core"),
            )
            self.assertIn(exact_formula_subject("curl", "wasm32"), planned["required_subjects"])


if __name__ == "__main__":
    unittest.main()
