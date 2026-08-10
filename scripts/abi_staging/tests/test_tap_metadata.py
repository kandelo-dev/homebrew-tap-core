from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.tap_metadata import (
    TapMetadataError,
    check_tap_metadata,
    load_abi_state,
    load_promotion_activation,
    load_promotion_policy,
)


DIGEST = "a" * 64
COMMIT = "1" * 40
TREE = "2" * 40
BOTTLE_SHA = "b" * 64
SOURCE_ABI = 7
PRIOR_ABI = SOURCE_ABI - 1


def _policy() -> str:
    return """\
schema = 1
kind = "kandelo-abi-staging-promotion-policy"
version = 1
tap_repository = "kandelo-dev/homebrew-tap-core"
kandelo_repository = "Automattic/kandelo"
historical_branch_prefix = "abi/"
require_branch_protection = true
canonical_repository_prefix = "homebrew-tap-core-abi-"
require_anonymous_readback = true
allow_independent_formula_promotion = true
allow_global_completion_gate = false
"""


def _bottle() -> dict[str, object]:
    return {
        "arch": "wasm32",
        "bottle_tag": "wasm32_kandelo",
        "browser_compatible": False,
        "built_at": "2026-08-09T00:00:00Z",
        "built_by": "https://github.com/kandelo-dev/homebrew-tap-core/actions/runs/9",
        "built_from": {
            "formula_sha256": DIGEST,
            "kandelo_commit": COMMIT,
            "kandelo_repository": "Automattic/kandelo",
            "tap_commit": COMMIT,
            "tap_repository": "kandelo-dev/homebrew-tap-core",
        },
        "bytes": 12,
        "cache_key_sha": BOTTLE_SHA,
        "cellar": ":any_skip_relocation",
        "fork_instrumentation": "not-required",
        "kandelo_abi": SOURCE_ABI,
        "link_manifest": "Kandelo/link/bash-1.0-rebuild1-wasm32.json",
        "prefix": "/opt/kandelo/homebrew",
        "runtime_support": ["node"],
        "sha256": BOTTLE_SHA,
        "status": "success",
        "url": (
            "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core/bash/blobs/sha256:"
            + BOTTLE_SHA
        ),
    }


def _package() -> dict[str, object]:
    return {
        "bottle_rebuild": 1,
        "bottles": [_bottle()],
        "dependencies": [],
        "formula_metadata": "Kandelo/formula/bash.json",
        "formula_path": "Formula/bash.rb",
        "formula_revision": 0,
        "full_name": "kandelo-dev/tap-core/bash",
        "name": "bash",
        "version": "1.0",
    }


def _sidecar(*, name: str = "bash", abi: int = SOURCE_ABI) -> dict[str, object]:
    package = _package()
    package.pop("formula_metadata")
    package["name"] = name
    package["formula_path"] = f"Formula/{name}.rb"
    package["full_name"] = f"kandelo-dev/tap-core/{name}"
    package["kandelo_abi"] = abi
    package["schema"] = 1
    package["source_metadata"] = "Kandelo/metadata.json"
    package["tap_commit"] = COMMIT
    package["tap_name"] = "kandelo-dev/tap-core"
    package["tap_repository"] = "kandelo-dev/homebrew-tap-core"
    bottles = copy.deepcopy(package["bottles"])
    assert isinstance(bottles, list)
    bottles[0]["kandelo_abi"] = abi
    package["bottles"] = bottles
    return package


def _metadata() -> dict[str, object]:
    return {
        "generated_at": "2026-08-09T00:00:00Z",
        "generator": "fixture",
        "kandelo_abi": SOURCE_ABI,
        "kandelo_commit": COMMIT,
        "kandelo_repository": "Automattic/kandelo",
        "packages": [_package()],
        "release_tag": f"bottles-abi-v{SOURCE_ABI}",
        "schema": 1,
        "tap_commit": COMMIT,
        "tap_name": "kandelo-dev/tap-core",
        "tap_repository": "kandelo-dev/homebrew-tap-core",
    }


class TapMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "Formula").mkdir()
        (self.root / "Kandelo/formula").mkdir(parents=True)
        (self.root / "Kandelo/staging").mkdir()
        (self.root / "Formula/bash.rb").write_text(
            "class Bash < Formula\n"
            "  bottle do\n"
            '    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"\n'
            "    rebuild 1\n"
            f'    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "{BOTTLE_SHA}"\n'
            "  end\n"
            "end\n"
        )
        (self.root / "Kandelo/formula/bash.json").write_bytes(
            json.dumps(_sidecar(), indent=2, sort_keys=True).encode() + b"\n"
        )
        (self.root / "Kandelo/metadata.json").write_bytes(
            json.dumps(_metadata(), indent=2, sort_keys=True).encode() + b"\n"
        )
        (self.root / "Kandelo/abi-state.json").write_bytes(
            canonical_bytes(
                {
                    "activation": None,
                    "current_abi": SOURCE_ABI,
                    "current_snapshot_sha256": DIGEST,
                    "kind": "kandelo-homebrew-abi-state",
                    "schema": 1,
                }
            )
        )
        (self.root / "Kandelo/staging/promotion-policy.toml").write_text(_policy())
        (self.root / "Kandelo/staging/promotion-activation.toml").write_text(
            'schema = 1\nkind = "kandelo-abi-staging-promotion-activation"\nmode = "disabled"\n'
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_loads_strict_disabled_policy_and_current_state(self) -> None:
        policy = load_promotion_policy(
            self.root / "Kandelo/staging/promotion-policy.toml"
        )
        activation = load_promotion_activation(
            self.root / "Kandelo/staging/promotion-activation.toml"
        )
        state = load_abi_state(self.root / "Kandelo/abi-state.json")
        projection = check_tap_metadata(self.root)

        self.assertEqual(policy.canonical_repository_prefix, "homebrew-tap-core-abi-")
        self.assertEqual(activation.mode, "disabled")
        self.assertIsNone(state.activation)
        self.assertEqual(projection["current_abi"], SOURCE_ABI)
        self.assertEqual(projection["active_formulae"], ["bash"])
        self.assertEqual(projection["promotion_mode"], "disabled")

    def test_rejects_weakened_or_ambiguous_policy(self) -> None:
        path = self.root / "Kandelo/staging/promotion-policy.toml"
        invalid = (
            _policy().replace("require_branch_protection = true", "require_branch_protection = false"),
            _policy().replace("allow_global_completion_gate = false", "allow_global_completion_gate = true"),
            _policy().replace(
                'canonical_repository_prefix = "homebrew-tap-core-abi-"',
                'canonical_repository_prefix = "homebrew-tap-core"',
            ),
            _policy() + "unknown = true\n",
        )
        for body in invalid:
            with self.subTest(body=body[-60:]):
                path.write_text(body)
                with self.assertRaises(TapMetadataError):
                    load_promotion_policy(path)

    def test_rejects_unknown_activation_mode_or_field(self) -> None:
        path = self.root / "Kandelo/staging/promotion-activation.toml"
        for body in (
            'schema = 1\nkind = "kandelo-abi-staging-promotion-activation"\nmode = "write"\n',
            'schema = 1\nkind = "kandelo-abi-staging-promotion-activation"\nmode = "disabled"\nextra = 1\n',
        ):
            path.write_text(body)
            with self.assertRaises(TapMetadataError):
                load_promotion_activation(path)

    def test_rejects_incomplete_or_inconsistent_managed_activation(self) -> None:
        path = self.root / "Kandelo/abi-state.json"
        activation = {
            "abi_history_record_digest": "c" * 64,
            "merge_commit": "4" * 40,
            "merged_pull_request": {
                "head": "3" * 40,
                "merge_commit": "4" * 40,
                "number": 19,
                "repository": "Automattic/kandelo",
            },
            "prior_abi": PRIOR_ABI,
            "prior_branch": f"abi/{PRIOR_ABI}",
            "request_digest": "d" * 64,
        }
        valid = {
            "activation": activation,
            "current_abi": SOURCE_ABI,
            "current_snapshot_sha256": DIGEST,
            "kind": "kandelo-homebrew-abi-state",
            "schema": 1,
        }
        for mutate in ("missing-history", "wrong-branch", "wrong-merge", "unknown"):
            changed = copy.deepcopy(valid)
            if mutate == "missing-history":
                del changed["activation"]["abi_history_record_digest"]
            elif mutate == "wrong-branch":
                changed["activation"]["prior_branch"] = f"abi/{PRIOR_ABI - 1}"
            elif mutate == "wrong-merge":
                changed["activation"]["merge_commit"] = "5" * 40
            else:
                changed["extra"] = True
            path.write_bytes(canonical_bytes(changed))
            with self.subTest(mutate=mutate), self.assertRaises(TapMetadataError):
                load_abi_state(path)

    def test_rejects_current_abi_or_active_sidecar_drift(self) -> None:
        metadata_path = self.root / "Kandelo/metadata.json"
        metadata = _metadata()
        metadata["kandelo_abi"] = PRIOR_ABI
        metadata_path.write_text(json.dumps(metadata))
        with self.assertRaises(TapMetadataError):
            check_tap_metadata(self.root)

        metadata_path.write_text(json.dumps(_metadata()))
        sidecar = _sidecar(abi=PRIOR_ABI)
        (self.root / "Kandelo/formula/bash.json").write_text(json.dumps(sidecar))
        with self.assertRaises(TapMetadataError):
            check_tap_metadata(self.root)

    def test_rejects_abi_suffixes_in_formula_or_platform_identity(self) -> None:
        metadata = _metadata()
        package = metadata["packages"][0]
        package["name"] = f"bash-abi{SOURCE_ABI}"
        (self.root / "Kandelo/metadata.json").write_text(json.dumps(metadata))
        with self.assertRaises(TapMetadataError):
            check_tap_metadata(self.root)

        (self.root / "Kandelo/metadata.json").write_text(json.dumps(_metadata()))
        sidecar = _sidecar()
        sidecar["bottles"][0]["bottle_tag"] = f"wasm32_abi{SOURCE_ABI}_kandelo"
        (self.root / "Kandelo/formula/bash.json").write_text(json.dumps(sidecar))
        with self.assertRaises(TapMetadataError):
            check_tap_metadata(self.root)

    def test_unselected_legacy_sidecar_is_visible_but_not_current_authority(self) -> None:
        legacy = _sidecar(name="legacy", abi=PRIOR_ABI)
        (self.root / "Kandelo/formula/legacy.json").write_text(json.dumps(legacy))
        (self.root / "Formula/legacy.rb").write_text(
            "class Legacy < Formula\n"
            "  bottle do\n"
            '    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"\n'
            f'    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "{"c" * 64}"\n'
            "  end\n"
            "end\n"
        )

        projection = check_tap_metadata(self.root)

        self.assertEqual(projection["legacy_unselected_sidecars"], ["legacy"])
        self.assertEqual(
            len(projection["formula_projection_sha256"]), hashlib.sha256().digest_size * 2
        )


if __name__ == "__main__":
    unittest.main()
