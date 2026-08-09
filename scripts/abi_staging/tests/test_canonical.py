from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
from types import MappingProxyType
import unittest

TAP_ROOT = Path(os.environ["KANDELO_TAP_ROOT"])
sys.path.insert(0, str(TAP_ROOT))

from scripts.abi_staging.canonical import (
    CanonicalJsonError,
    canonical_bytes,
    canonical_sha256,
    parse_canonical_bytes,
)


FIXTURES = TAP_ROOT / "Kandelo/staging/fixtures/request"


class CanonicalJsonTests(unittest.TestCase):
    def test_encoding_sorts_objects_retains_arrays_and_ends_with_line_feed(self) -> None:
        value = {"z": [3, 1], "a": {"later": True, "first": None}}
        expected = b'{"a":{"first":null,"later":true},"z":[3,1]}\n'
        self.assertEqual(canonical_bytes(value), expected)
        self.assertEqual(canonical_sha256(value), hashlib.sha256(expected).hexdigest())

    def test_rust_fixtures_are_byte_stable_and_parse_immutably(self) -> None:
        expected = {
            "current-request.json": "e11c9256eca9456bb3da7e9f8516012bff1829ba0043f003ad3ed0cc80f1acb1",
            "same-head-reissued-request.json": "e92fb095fa06aa70c201493c6b699a49991b217ca833dba24d1a313c04bddf33",
            "historical-request.json": "50f56b1bcb6745efda47e6a79727c9557991f7fad8ab668ded73e1b0dc1df32a",
        }
        for name, digest in expected.items():
            with self.subTest(name=name):
                body = (FIXTURES / name).read_bytes()
                parsed = parse_canonical_bytes(body, maximum_bytes=4 * 1024 * 1024)
                self.assertIsInstance(parsed, MappingProxyType)
                self.assertEqual(canonical_bytes(parsed), body)
                self.assertEqual(canonical_sha256(parsed), digest)

    def test_parser_rejects_noncanonical_and_ambiguous_json(self) -> None:
        invalid = [
            b'{"b":1,"a":2}\n',
            b'{"a": 1}\n',
            b'{"a":1}',
            b'{"a":1,"a":1}\n',
            b'{"a":1.0}\n',
            b'{"a":18446744073709551616}\n',
            b'\xff',
        ]
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(CanonicalJsonError):
                    parse_canonical_bytes(body, maximum_bytes=1024)

    def test_bounds_and_integer_only_encoding_fail_closed(self) -> None:
        with self.assertRaises(CanonicalJsonError):
            parse_canonical_bytes(b'{"a":1}\n', maximum_bytes=4)
        for value in [1.0, float("nan"), 2**64, -(2**63) - 1]:
            with self.subTest(value=value):
                with self.assertRaises(CanonicalJsonError):
                    canonical_bytes({"value": value})


if __name__ == "__main__":
    unittest.main()
