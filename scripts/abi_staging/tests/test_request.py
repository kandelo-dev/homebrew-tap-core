from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from types import MappingProxyType
import unittest

TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
sys.path.insert(0, str(TAP_ROOT))

from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.request import (
    RequestValidationError,
    load_request_issuer_policy,
    parse_request_asset_name,
    validate_request,
)


FIXTURES = TAP_ROOT / "Kandelo/staging/fixtures/request"
POLICY_PATH = TAP_ROOT / "Kandelo/staging/request-issuers.toml"


def mutable_fixture(name: str = "current-request.json") -> dict[str, object]:
    return json.loads((FIXTURES / name).read_bytes())


def asset_name(value: dict[str, object], body: bytes) -> str:
    source = value["build_source"]
    assert isinstance(source, dict)
    head = source["commit"]
    return f"candidate-request-{head}-sha256-{hashlib.sha256(body).hexdigest()}.json"


class RequestValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_request_issuer_policy(
            POLICY_PATH,
            expected_tap="kandelo-dev/homebrew-tap-core",
        )

    def test_all_rust_fixtures_validate_with_exact_filename_binding(self) -> None:
        for path in sorted(FIXTURES.glob("*-request.json")):
            with self.subTest(name=path.name):
                body = path.read_bytes()
                value = json.loads(body)
                name = asset_name(value, body)
                parsed = validate_request(body, name, self.policy)
                self.assertIsInstance(parsed, MappingProxyType)
                self.assertEqual(parsed["build_source"]["commit"], value["build_source"]["commit"])
                self.assertEqual(parse_request_asset_name(name).head, value["build_source"]["commit"])

    def assert_invalid(self, value: dict[str, object], name: str | None = None) -> None:
        body = canonical_bytes(value)
        with self.assertRaises(RequestValidationError):
            validate_request(body, name or asset_name(value, body), self.policy)

    def test_unknown_fields_and_informational_authority_are_rejected(self) -> None:
        value = mutable_fixture()
        value["latest"] = True
        self.assert_invalid(value)

        value = mutable_fixture()
        context = value["informational_context"]
        assert isinstance(context, dict)
        context["build_source"] = copy.deepcopy(value["build_source"])
        self.assert_invalid(value)

    def test_filename_head_digest_and_canonical_bytes_are_independent_checks(self) -> None:
        value = mutable_fixture()
        body = canonical_bytes(value)
        good = asset_name(value, body)
        with self.assertRaises(RequestValidationError):
            validate_request(body, good.replace("11111111", "99999999", 1), self.policy)
        with self.assertRaises(RequestValidationError):
            validate_request(body, good.replace(hashlib.sha256(body).hexdigest(), "0" * 64), self.policy)
        pretty = json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
        with self.assertRaises(RequestValidationError):
            validate_request(pretty, good, self.policy)

    def test_issuer_tap_and_git_identities_are_strict(self) -> None:
        value = mutable_fixture()
        value["pull_request"]["repository"] = "other/project"
        value["build_source"]["repository"] = "other/project"
        value["issuance"]["issuer_repository"] = "other/project"
        self.assert_invalid(value)

        with self.assertRaises(RequestValidationError):
            load_request_issuer_policy(POLICY_PATH, expected_tap="other/tap")

        value = mutable_fixture()
        value["build_source"]["commit"] = "A" * 40
        value["issuance"]["authorization"]["head"] = "A" * 40
        self.assert_invalid(value)

        value = mutable_fixture()
        value["issuance"]["issuer_workflow_ref"] = (
            "Automattic/kandelo/.github/workflows/abi-staging-request-feed.yml@main"
        )
        self.assert_invalid(value)

    def test_requirements_digest_shape_and_sorted_bindings_are_enforced(self) -> None:
        value = mutable_fixture()
        value["requirements"]["products"][0]["path"] = "../escape.toml"
        self.assert_invalid(value)

        value = mutable_fixture()
        value["requirements"]["digest"] = "0" * 64
        self.assert_invalid(value)

        value = mutable_fixture()
        value["requirements"]["change_classes"] = ["kernel", "abi"]
        self.assert_invalid(value)

    def test_request_field_and_binding_bounds_are_enforced(self) -> None:
        value = mutable_fixture()
        value["informational_context"]["ref_hint"] = "x" * 4097
        self.assert_invalid(value)

        value = mutable_fixture()
        product = value["requirements"]["products"][0]
        value["requirements"]["products"] = [copy.deepcopy(product) for _ in range(4097)]
        self.assert_invalid(value)


if __name__ == "__main__":
    unittest.main()
