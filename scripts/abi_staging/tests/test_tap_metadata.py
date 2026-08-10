from __future__ import annotations

import copy
from dataclasses import replace
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from scripts.abi_staging import promotion as promotion_module
from scripts.abi_staging import tap_metadata as tap_metadata_module
from scripts.abi_staging.bottle_link import (
    BottleLinkError,
    inspect_bottle_link_inventory,
)
from scripts.abi_staging.abi_history import (
    build_history_oci_plan,
    build_history_record,
    history_record_repository,
    protection_requirement_sha256,
    validate_protection_snapshot,
)
from scripts.abi_staging.canonical import canonical_bytes
from scripts.abi_staging.oci import (
    FetchedOciBlobV1,
    FetchedOciRecordV1,
    build_oci_manifest,
)
from scripts.abi_staging.plan import exact_formula_subject
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
SUCCESSOR_ABI = SOURCE_ABI + 1
SUCCESSOR_SNAPSHOT = "e" * 64
REQUEST_DIGEST = "d" * 64
MERGED_HEAD = "3" * 40
MERGE_COMMIT = "4" * 40


def _guest_layout_bytes() -> bytes:
    return (
        json.dumps(
            {
                "schema": 1,
                "kind": "kandelo-homebrew-guest-layout",
                "prefix": "/opt/kandelo/homebrew",
                "cellar": "/opt/kandelo/homebrew/Cellar",
                "repository": "/opt/kandelo/homebrew",
                "stable_entrypoint": "/usr/bin/brew",
                "retired_prefixes": ["/home/linuxbrew/.linuxbrew"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _bottle_archive(formula: str, version: str) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            payload_root = f"{formula}/{version}"
            for path in (
                formula,
                payload_root,
                f"{payload_root}/.brew",
                f"{payload_root}/bin",
                f"{payload_root}/share",
                f"{payload_root}/share/doc",
            ):
                member = tarfile.TarInfo(path)
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
                member.mtime = 0
                archive.addfile(member)
            files = {
                f"{payload_root}/.brew/{formula}.rb": (b"class Fixture < Formula\nend\n", 0o644),
                f"{payload_root}/INSTALL_RECEIPT.json": (b"{}\n", 0o644),
                f"{payload_root}/bin/{formula}": (b"fixture executable\n", 0o755),
                f"{payload_root}/share/doc/README": (b"fixture documentation\n", 0o644),
            }
            for path, (body, mode) in files.items():
                member = tarfile.TarInfo(path)
                member.size = len(body)
                member.mode = mode
                member.mtime = 0
                archive.addfile(member, io.BytesIO(body))
    return output.getvalue()


def _unsafe_bottle_archive(member: tarfile.TarInfo) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path in ("bash", "bash/1.0", "bash/1.0/.brew", "bash/1.0/bin"):
                directory = tarfile.TarInfo(path)
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o755
                directory.mtime = 0
                archive.addfile(directory)
            for path in ("bash/1.0/.brew/bash.rb", "bash/1.0/INSTALL_RECEIPT.json"):
                body = b"{}\n"
                required = tarfile.TarInfo(path)
                required.size = len(body)
                required.mode = 0o644
                required.mtime = 0
                archive.addfile(required, io.BytesIO(body))
            member.mtime = 0
            archive.addfile(member)
    return output.getvalue()


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


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.decode("utf-8", errors="strict").strip()


def _fetched_history(record: dict[str, object]) -> FetchedOciRecordV1:
    repository = history_record_repository(
        "kandelo-dev/homebrew-tap-core", SOURCE_ABI
    )
    plan = build_history_oci_plan(record, repository=repository)
    manifest = build_oci_manifest(plan)
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()

    def fetched_blob(blob: object) -> FetchedOciBlobV1:
        return FetchedOciBlobV1(
            role=blob.role,
            media_type=blob.media_type,
            digest=blob.digest,
            size=blob.size,
            title=blob.title,
            body=blob.body,
        )

    return FetchedOciRecordV1(
        repository="ghcr.io/" + repository,
        digest=digest,
        immutable_reference=f"ghcr.io/{repository}@{digest}",
        artifact_type=plan.artifact_type,
        manifest=manifest,
        config=fetched_blob(plan.config),
        layers=tuple(fetched_blob(layer) for layer in plan.layers),
    )


class TapMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "Formula").mkdir()
        (self.root / "Kandelo/formula").mkdir(parents=True)
        (self.root / "Kandelo/link").mkdir()
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
        (self.root / "Kandelo/link/bash-1.0-rebuild1-wasm32.json").write_bytes(
            canonical_bytes({"architecture": "wasm32", "formula": "bash"})
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

    def _commit_fixture(self) -> dict[str, str]:
        _git(self.root, "init", "--initial-branch=main")
        _git(self.root, "config", "user.name", "ABI metadata test")
        _git(self.root, "config", "user.email", "abi-metadata@example.invalid")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-m", "current ABI fixture")
        return {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": _git(self.root, "rev-parse", "HEAD"),
            "tree": _git(self.root, "rev-parse", "HEAD^{tree}"),
        }

    def _history_authority(
        self, source: dict[str, str]
    ) -> tuple[FetchedOciRecordV1, dict[str, object]]:
        plan = {
            "source_abi": SOURCE_ABI,
            "successor_abi": SUCCESSOR_ABI,
            "preactivation_tap_commit": source["commit"],
            "preactivation_tap_tree": source["tree"],
            "branch": f"abi/{SOURCE_ABI}",
            "expected_current_metadata_sha256": check_tap_metadata(self.root)[
                "active_projection_sha256"
            ],
            "protection_requirement_sha256": protection_requirement_sha256(),
        }
        snapshot = {
            "schema": 1,
            "kind": "kandelo-abi-history-protection-snapshot",
            "repository": "kandelo-dev/homebrew-tap-core",
            "branch": f"abi/{SOURCE_ABI}",
            "phase": "postcreate",
            "ref": {"object": source["commit"], "tree": source["tree"]},
            "direct": {
                "branch": f"abi/{SOURCE_ABI}",
                "allow_deletions": False,
                "allow_force_pushes": False,
                "enforce_admins": True,
            },
            "rulesets": [],
        }
        evidence = validate_protection_snapshot(
            plan,
            snapshot,
            phase="postcreate",
            expected_repository="kandelo-dev/homebrew-tap-core",
        )
        record = build_history_record(
            plan,
            created_ref_object=source["commit"],
            protection_evidence=evidence,
            metadata_verification_sha256="7" * 64,
            public_readback_sha256="8" * 64,
            run={
                "repository": "kandelo-dev/homebrew-tap-core",
                "workflow_ref": (
                    ".github/workflows/abi-staging-abi-history.yml@refs/heads/main"
                ),
                "run_id": 9,
                "run_attempt": 1,
                "job": "verify-and-publish-history",
            },
        )
        return _fetched_history(record), snapshot

    def _prepare_activation(
        self,
        *,
        history: FetchedOciRecordV1 | None,
        snapshot: dict[str, object],
        source: dict[str, str],
        target_abi: int = SUCCESSOR_ABI,
    ) -> object:
        prepare = getattr(
            promotion_module, "prepare_successor_activation_patch", None
        )
        if prepare is None:
            self.fail("successor activation planning is absent")
        return prepare(
            tap_root=self.root,
            history=history,
            history_protection_snapshot=snapshot,
            current_tap_source=source,
            request_digest=REQUEST_DIGEST,
            merged_pull_request={
                "repository": "Automattic/kandelo",
                "number": 19,
                "head": MERGED_HEAD,
                "merge_commit": MERGE_COMMIT,
            },
            target_abi=target_abi,
            target_snapshot_sha256=SUCCESSOR_SNAPSHOT,
            policy=load_promotion_policy(
                self.root / "Kandelo/staging/promotion-policy.toml"
            ),
        )

    def test_plans_successor_activation_without_a_complete_tap_gate(self) -> None:
        source = self._commit_fixture()
        history, snapshot = self._history_authority(source)

        patch = self._prepare_activation(
            history=history,
            snapshot=snapshot,
            source=source,
        )

        self.assertEqual(patch.operation, "successor-activation")
        self.assertEqual(patch.expected_main_commit, source["commit"])
        self.assertIn("Kandelo/abi-state.json", patch.allowed_paths)
        state = json.loads(patch.files["Kandelo/abi-state.json"])
        metadata = json.loads(patch.files["Kandelo/metadata.json"])
        sidecar = json.loads(patch.files["Kandelo/formula/bash.json"])
        self.assertEqual(state["current_abi"], SUCCESSOR_ABI)
        self.assertEqual(state["current_snapshot_sha256"], SUCCESSOR_SNAPSHOT)
        self.assertEqual(state["activation"]["prior_abi"], SOURCE_ABI)
        self.assertEqual(state["activation"]["prior_branch"], f"abi/{SOURCE_ABI}")
        self.assertEqual(state["activation"]["request_digest"], REQUEST_DIGEST)
        self.assertEqual(metadata["kandelo_abi"], SUCCESSOR_ABI)
        self.assertEqual(sidecar["kandelo_abi"], SUCCESSOR_ABI)
        self.assertEqual(metadata["packages"][0]["bottles"][0]["status"], "pending")
        self.assertEqual(sidecar["bottles"][0]["status"], "pending")
        for field in ("url", "sha256", "bytes", "cache_key_sha", "link_manifest"):
            self.assertNotIn(field, sidecar["bottles"][0])
        self.assertNotIn(b"bottle do", patch.files["Formula/bash.rb"])
        validate = getattr(
            tap_metadata_module, "validate_successor_activation_patch", None
        )
        if validate is None:
            self.fail("successor activation patch validation is absent")
        validate(self.root, patch)

    def test_activation_rejects_missing_wrong_or_unprotected_history(self) -> None:
        source = self._commit_fixture()
        history, snapshot = self._history_authority(source)

        with self.assertRaises(promotion_module.PromotionError):
            self._prepare_activation(
                history=None,
                snapshot=snapshot,
                source=source,
            )

        moved_source = copy.deepcopy(source)
        moved_source["commit"] = "9" * 40
        with self.assertRaises(promotion_module.PromotionError):
            self._prepare_activation(
                history=history,
                snapshot=snapshot,
                source=moved_source,
            )

        unprotected = copy.deepcopy(snapshot)
        unprotected["direct"]["allow_force_pushes"] = True
        with self.assertRaises(promotion_module.PromotionError):
            self._prepare_activation(
                history=history,
                snapshot=unprotected,
                source=source,
            )

    def test_activation_rejects_non_successor_or_changed_current_abi(self) -> None:
        source = self._commit_fixture()
        history, snapshot = self._history_authority(source)
        with self.assertRaises(promotion_module.PromotionError):
            self._prepare_activation(
                history=history,
                snapshot=snapshot,
                source=source,
                target_abi=SUCCESSOR_ABI + 1,
            )

        state = {
            "activation": None,
            "current_abi": SUCCESSOR_ABI,
            "current_snapshot_sha256": SUCCESSOR_SNAPSHOT,
            "kind": "kandelo-homebrew-abi-state",
            "schema": 1,
        }
        (self.root / "Kandelo/abi-state.json").write_bytes(canonical_bytes(state))
        with self.assertRaises(promotion_module.PromotionError):
            self._prepare_activation(
                history=history,
                snapshot=snapshot,
                source=source,
            )

    def test_activation_patch_rejects_old_bottle_or_unexpected_path(self) -> None:
        source = self._commit_fixture()
        history, snapshot = self._history_authority(source)
        patch = self._prepare_activation(
            history=history,
            snapshot=snapshot,
            source=source,
        )
        validate = getattr(
            tap_metadata_module, "validate_successor_activation_patch", None
        )
        if validate is None:
            self.fail("successor activation patch validation is absent")

        old_bottle = dict(patch.files)
        old_bottle["Formula/bash.rb"] = (self.root / "Formula/bash.rb").read_bytes()
        with self.assertRaises(TapMetadataError):
            validate(self.root, replace(patch, files=old_bottle))

        unexpected = dict(patch.files)
        unexpected["README.md"] = b"unexpected\n"
        with self.assertRaises(TapMetadataError):
            validate(self.root, replace(patch, files=unexpected))

    def _materialize_patch(self, patch: object, message: str) -> dict[str, str]:
        for path, body in patch.files.items():
            (self.root / path).write_bytes(body)
        _git(self.root, "add", "--", *patch.allowed_paths)
        _git(self.root, "commit", "-m", message)
        return {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": _git(self.root, "rev-parse", "HEAD"),
            "tree": _git(self.root, "rev-parse", "HEAD^{tree}"),
        }

    def _activate_fixture(
        self,
    ) -> tuple[FetchedOciRecordV1, dict[str, object], dict[str, str], dict[str, str]]:
        preactivation = self._commit_fixture()
        history, snapshot = self._history_authority(preactivation)
        activation = self._prepare_activation(
            history=history,
            snapshot=snapshot,
            source=preactivation,
        )
        current = self._materialize_patch(activation, "activate successor ABI")
        return history, snapshot, preactivation, current

    def _enable_wasm64(self) -> None:
        digest = "c" * 64
        sidecar_path = self.root / "Kandelo/formula/bash.json"
        sidecar = json.loads(sidecar_path.read_bytes())
        bottle = copy.deepcopy(sidecar["bottles"][0])
        bottle.update(
            {
                "arch": "wasm64",
                "bottle_tag": "wasm64_kandelo",
                "bytes": 13,
                "cache_key_sha": digest,
                "link_manifest": "Kandelo/link/bash-1.0-rebuild1-wasm64.json",
                "sha256": digest,
                "url": (
                    "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core/bash/"
                    "blobs/sha256:"
                    + digest
                ),
            }
        )
        sidecar["bottles"].append(bottle)
        sidecar_path.write_bytes(
            json.dumps(sidecar, indent=2, sort_keys=True).encode() + b"\n"
        )
        metadata_path = self.root / "Kandelo/metadata.json"
        metadata = json.loads(metadata_path.read_bytes())
        metadata["packages"][0]["bottles"] = copy.deepcopy(sidecar["bottles"])
        metadata_path.write_bytes(
            json.dumps(metadata, indent=2, sort_keys=True).encode() + b"\n"
        )
        formula_path = self.root / "Formula/bash.rb"
        formula = formula_path.read_text()
        formula = formula.replace(
            "  end\nend\n",
            f'    sha256 cellar: :any_skip_relocation, wasm64_kandelo: "{digest}"\n'
            "  end\nend\n",
        )
        formula_path.write_text(formula)
        (self.root / "Kandelo/link/bash-1.0-rebuild1-wasm64.json").write_bytes(
            canonical_bytes({"architecture": "wasm64", "formula": "bash"})
        )

    def _add_dash(self) -> None:
        digest = "c" * 64
        (self.root / "Formula/dash.rb").write_text(
            "class Dash < Formula\n"
            "  bottle do\n"
            '    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"\n'
            "    rebuild 1\n"
            f'    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "{digest}"\n'
            "  end\n"
            "end\n"
        )
        sidecar = _sidecar(name="dash")
        bottle = sidecar["bottles"][0]
        bottle.update(
            {
                "bytes": 13,
                "cache_key_sha": digest,
                "link_manifest": "Kandelo/link/dash-1.0-rebuild1-wasm32.json",
                "sha256": digest,
                "url": (
                    "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core/dash/"
                    "blobs/sha256:"
                    + digest
                ),
            }
        )
        (self.root / "Kandelo/formula/dash.json").write_bytes(
            json.dumps(sidecar, indent=2, sort_keys=True).encode() + b"\n"
        )
        metadata_path = self.root / "Kandelo/metadata.json"
        metadata = json.loads(metadata_path.read_bytes())
        package = {
            key: value
            for key, value in sidecar.items()
            if key
            not in {
                "kandelo_abi",
                "schema",
                "source_metadata",
                "tap_commit",
                "tap_name",
                "tap_repository",
            }
        }
        package["formula_metadata"] = "Kandelo/formula/dash.json"
        metadata["packages"].append(package)
        metadata["packages"].sort(key=lambda item: item["name"])
        metadata_path.write_bytes(
            json.dumps(metadata, indent=2, sort_keys=True).encode() + b"\n"
        )
        (self.root / "Kandelo/link/dash-1.0-rebuild1-wasm32.json").write_bytes(
            canonical_bytes({"architecture": "wasm32", "formula": "dash"})
        )

    def _generated_digest(self, formula: str = "bash") -> str:
        calculate = getattr(
            tap_metadata_module, "formula_generated_metadata_sha256", None
        )
        if calculate is None:
            self.fail("per-Formula generated metadata identity is absent")
        return calculate(self.root, formula)

    def _prepared_admission(
        self,
        *,
        preactivation: dict[str, str],
        architecture: str = "wasm32",
        canonical_digest: str = "5" * 64,
        formula: str = "bash",
        metadata_layer_sha256: str | None = None,
        version: str = "1.0",
        revision: int = 0,
        rebuild: int = 1,
    ) -> object:
        pkg_version = version if revision == 0 else f"{version}_{revision}"
        bottle_body = _bottle_archive(formula, pkg_version)
        layer_sha256 = hashlib.sha256(bottle_body).hexdigest()
        formula_source = (self.root / f"Formula/{formula}.rb").read_bytes()
        normalized_formula_sha256 = hashlib.sha256(
            tap_metadata_module.normalize_formula_source(formula_source)
        ).hexdigest()
        guest_layout = _guest_layout_bytes()
        contract = json.loads(
            (
                Path(promotion_module.__file__).parent.parent.parent
                / "Kandelo/staging/fixtures/bottle-contract.json"
            ).read_bytes()
        )
        contract["target"] = {
            "abi": SUCCESSOR_ABI,
            "architecture": architecture,
            "snapshot_sha256": SUCCESSOR_SNAPSHOT,
        }
        contract["formula"] = {
            "name": formula,
            "version": version,
            "revision": revision,
            "rebuild": rebuild,
            "normalized_source_sha256": "b" * 64,
            "source_components": [
                {"id": "formula", "sha256": normalized_formula_sha256},
                {"id": "tap-input-0000", "sha256": "d" * 64},
            ],
        }
        for dependency in contract["direct_dependencies"]:
            dependency["architecture"] = architecture
        contract["kandelo_inputs"] = [
            {
                "id": "kandelo-0000",
                "kind": "file",
                "path": "homebrew/kandelo-guest-layout.json",
                "sha256": hashlib.sha256(guest_layout).hexdigest(),
            }
        ]
        contract_sha256 = hashlib.sha256(canonical_bytes(contract)).hexdigest()
        decision = promotion_module.PromotionDecisionV1(
            request_digest=REQUEST_DIGEST,
            merged_pull_request={
                "repository": "Automattic/kandelo",
                "number": 19,
                "head": MERGED_HEAD,
                "merge_commit": MERGE_COMMIT,
            },
            formula_subject=exact_formula_subject(formula, architecture),
            tap_plan_digest="1" * 64,
            candidate_record_digest="2" * 64,
            candidate_binding_digest="2" * 64,
            bottle_layer_sha256=layer_sha256,
            bottle_layer_bytes=len(bottle_body),
            source_custody_digest="3" * 64,
            qualifying_receipts=("4" * 64,),
            override_receipts=(),
            tap_source_state="exact",
            eligibility="eligible",
        )
        candidate_root = (
            "https://ghcr.io/v2/kandelo-dev/"
            f"homebrew-tap-core-abi-{SUCCESSOR_ABI}-candidates/{formula}"
        )
        candidate_metadata = {
            formula: {
                "formula": {
                    "name": formula,
                    "path": (
                        "Library/Taps/kandelo-dev/homebrew-tap-core/Formula/"
                        f"{formula}.rb"
                    ),
                    "pkg_version": pkg_version,
                },
                "bottle": {
                    "root_url": candidate_root,
                    "cellar": "any_skip_relocation",
                    "rebuild": rebuild,
                    "tags": {
                        f"{architecture}_kandelo": {
                            "sha256": (
                                layer_sha256
                                if metadata_layer_sha256 is None
                                else metadata_layer_sha256
                            )
                        }
                    },
                },
            }
        }
        prepared_type = promotion_module.PreparedAdmissionV1
        try:
            return prepared_type(
                decision=decision,
                request_source={
                    "repository": "Automattic/kandelo",
                    "commit": MERGED_HEAD,
                    "tree": "6" * 40,
                },
                candidate_source={
                    "repository": "Automattic/kandelo",
                    "commit": MERGED_HEAD,
                    "tree": "6" * 40,
                },
                preactivation_tap_source=preactivation,
                abi_history_record_sha256="9" * 64,
                canonical={
                    "sha256": canonical_digest,
                    "bytes": 99,
                    "immutable_reference": (
                        "ghcr.io/kandelo-dev/"
                        f"homebrew-tap-core-abi-{SUCCESSOR_ABI}/{formula}"
                        f"@sha256:{canonical_digest}"
                    ),
                },
                canonical_readback_evidence_sha256="7" * 64,
                promoted_layer={
                    "sha256": layer_sha256,
                    "bytes": len(bottle_body),
                    "immutable_reference": (
                        "ghcr.io/kandelo-dev/"
                        f"homebrew-tap-core-abi-{SUCCESSOR_ABI}-candidates/{formula}"
                        f"@sha256:{layer_sha256}"
                    ),
                },
                original_producer={
                    "request_sha256": REQUEST_DIGEST,
                    "head": MERGED_HEAD,
                    "run_id": 77,
                },
                candidate_formula={
                    "tap": "kandelo-dev/homebrew-tap-core",
                    "formula": formula,
                    "version": version,
                    "revision": revision,
                    "bottle_rebuild": rebuild,
                    "architecture": architecture,
                    "target_abi": SUCCESSOR_ABI,
                    "bottle_contract_sha256": contract_sha256,
                },
                candidate_bottle_metadata=candidate_metadata,
                candidate_bottle_contract=contract,
                candidate_bottle_inventory={
                    "schema": 1,
                    "kind": "kandelo-homebrew-bottle-link-inventory",
                    "payload_root": f"{formula}/{pkg_version}",
                    "all_files": [
                        f".brew/{formula}.rb",
                        "INSTALL_RECEIPT.json",
                        f"bin/{formula}",
                        "share/doc/README",
                    ],
                    "path_exec_files": [f"bin/{formula}"],
                },
            )
        except TypeError as error:
            self.fail(f"prepared admission omits authenticated bottle metadata: {error}")

    def _prepare_formula(
        self,
        *,
        prepared: object,
        history: FetchedOciRecordV1,
        snapshot: dict[str, object],
        current: dict[str, str],
        generated: str | None = None,
    ) -> object:
        prepare = getattr(promotion_module, "prepare_formula_metadata_patch", None)
        if prepare is None:
            self.fail("per-Formula metadata planning is absent")
        formula = prepared.candidate_formula["formula"]
        return prepare(
            tap_root=self.root,
            prepared=prepared,
            history=history,
            history_protection_snapshot=snapshot,
            current_tap_source=current,
            expected_generated_metadata_sha256=(
                self._generated_digest(formula) if generated is None else generated
            ),
            guest_layout_bytes=_guest_layout_bytes(),
            policy=load_promotion_policy(
                self.root / "Kandelo/staging/promotion-policy.toml"
            ),
        )

    def test_plans_one_formula_update_with_exact_canonical_root_and_layer(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)

        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )

        update = result.update
        patch = result.patch
        layer_sha256 = prepared.promoted_layer["sha256"]
        self.assertEqual(update.formula, "bash")
        self.assertEqual(update.architecture, "wasm32")
        self.assertEqual(update.expected_main_commit, current["commit"])
        self.assertEqual(
            update.allowed_paths,
            (
                "Formula/bash.rb",
                "Kandelo/formula/bash.json",
                "Kandelo/metadata.json",
                "Kandelo/link/bash-1.0-rebuild1-wasm32.json",
            ),
        )
        self.assertEqual(update.canonical_manifest_digest, "5" * 64)
        self.assertEqual(update.bottle_layer_sha256, layer_sha256)
        self.assertEqual(
            update.link_manifest_path,
            "Kandelo/link/bash-1.0-rebuild1-wasm32.json",
        )
        formula = patch.files["Formula/bash.rb"].decode()
        canonical_root = (
            "https://ghcr.io/v2/kandelo-dev/"
            f"homebrew-tap-core-abi-{SUCCESSOR_ABI}/bash"
        )
        self.assertIn(f'root_url "{canonical_root}"', formula)
        self.assertIn(f'wasm32_kandelo: "{layer_sha256}"', formula)
        sidecar = json.loads(patch.files["Kandelo/formula/bash.json"])
        bottle = sidecar["bottles"][0]
        self.assertEqual(bottle["status"], "success")
        self.assertEqual(bottle["kandelo_abi"], SUCCESSOR_ABI)
        self.assertEqual(bottle["sha256"], layer_sha256)
        self.assertEqual(
            bottle["url"], canonical_root + "/blobs/sha256:" + layer_sha256
        )
        self.assertEqual(bottle["prefix"], "/opt/kandelo/homebrew")
        self.assertEqual(bottle["cellar"], "/opt/kandelo/homebrew/Cellar")
        link = json.loads(patch.files[update.link_manifest_path])
        self.assertEqual(link["package"], "bash")
        self.assertEqual(link["version"], "1.0")
        self.assertEqual(link["kandelo_abi"], SUCCESSOR_ABI)
        self.assertEqual(link["bottle"]["sha256"], layer_sha256)
        self.assertEqual(link["bottle"]["url"], bottle["url"])
        self.assertEqual(link["links"][0]["source"], "bin/bash")
        self.assertEqual(link["env"], {"PATH_prepend": ["bin"]})
        self.assertEqual(
            hashlib.sha256(patch.files[update.link_manifest_path]).hexdigest(),
            update.link_manifest_sha256,
        )
        self.assertEqual(
            bottle["built_by"],
            "https://github.com/kandelo-dev/homebrew-tap-core/actions/runs/77",
        )
        self.assertEqual(bottle["built_from"]["kandelo_commit"], MERGED_HEAD)
        self.assertEqual(bottle["built_from"]["tap_commit"], preactivation["commit"])
        metadata = json.loads(patch.files["Kandelo/metadata.json"])
        projected = dict(sidecar)
        for key in (
            "kandelo_abi",
            "schema",
            "source_metadata",
            "tap_commit",
            "tap_name",
            "tap_repository",
        ):
            projected.pop(key)
        projected["formula_metadata"] = "Kandelo/formula/bash.json"
        self.assertEqual(metadata["packages"][0], projected)
        validate = getattr(tap_metadata_module, "validate_formula_metadata_patch", None)
        if validate is None:
            self.fail("per-Formula metadata patch validation is absent")
        validate(self.root, patch, update)

    def test_formula_update_rejects_canonical_or_candidate_layer_drift(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        wrong_canonical = replace(
            prepared,
            canonical={
                **dict(prepared.canonical),
                "immutable_reference": (
                    "ghcr.io/attacker/homebrew-tap-core-abi-"
                    f"{SUCCESSOR_ABI}/bash@sha256:{"5" * 64}"
                ),
            },
        )
        with self.assertRaises(promotion_module.PromotionError):
            self._prepare_formula(
                prepared=wrong_canonical,
                history=history,
                snapshot=snapshot,
                current=current,
            )

        wrong_candidate_reference = replace(
            prepared,
            promoted_layer={
                **dict(prepared.promoted_layer),
                "immutable_reference": (
                    prepared.promoted_layer["immutable_reference"] + "/forged"
                ),
            },
        )
        with self.assertRaises(promotion_module.PromotionError):
            self._prepare_formula(
                prepared=wrong_candidate_reference,
                history=history,
                snapshot=snapshot,
                current=current,
            )

        wrong_layer = self._prepared_admission(
            preactivation=preactivation,
            metadata_layer_sha256="9" * 64,
        )
        with self.assertRaises(promotion_module.PromotionError):
            self._prepare_formula(
                prepared=wrong_layer,
                history=history,
                snapshot=snapshot,
                current=current,
            )

    def test_formula_update_rejects_source_generated_or_current_abi_drift(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        forged_contract = copy.deepcopy(dict(prepared.candidate_bottle_contract))
        forged_contract["formula"]["source_components"][0]["sha256"] = "9" * 64
        with self.subTest(field="normalized"), self.assertRaises(
            promotion_module.PromotionError
        ):
            self._prepare_formula(
                prepared=replace(
                    prepared,
                    candidate_bottle_contract=forged_contract,
                ),
                history=history,
                snapshot=snapshot,
                current=current,
            )

        with self.subTest(field="generated"), self.assertRaises(
            promotion_module.PromotionError
        ):
            self._prepare_formula(
                prepared=prepared,
                history=history,
                snapshot=snapshot,
                current=current,
                generated="9" * 64,
            )

        state = json.loads((self.root / "Kandelo/abi-state.json").read_bytes())
        state["current_abi"] = SUCCESSOR_ABI + 1
        (self.root / "Kandelo/abi-state.json").write_bytes(canonical_bytes(state))
        with self.assertRaises(promotion_module.PromotionError):
            self._prepare_formula(
                prepared=prepared,
                history=history,
                snapshot=snapshot,
                current=current,
            )

    def test_formula_update_rejects_an_unexpected_patch_path(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        validate = getattr(tap_metadata_module, "validate_formula_metadata_patch")
        unexpected = dict(result.patch.files)
        unexpected["README.md"] = b"unexpected\n"

        with self.assertRaises(TapMetadataError):
            validate(
                self.root,
                replace(result.patch, files=unexpected),
                result.update,
            )

    def test_formula_update_rejects_uncaptured_guest_layout_bytes(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        layout = json.loads(_guest_layout_bytes())
        layout["prefix"] = "/attacker/prefix"
        changed = (json.dumps(layout, indent=2, sort_keys=True) + "\n").encode()

        prepare = getattr(promotion_module, "prepare_formula_metadata_patch")
        with self.assertRaises(promotion_module.PromotionError):
            prepare(
                tap_root=self.root,
                prepared=prepared,
                history=history,
                history_protection_snapshot=snapshot,
                current_tap_source=current,
                expected_generated_metadata_sha256=self._generated_digest(),
                guest_layout_bytes=changed,
                policy=load_promotion_policy(
                    self.root / "Kandelo/staging/promotion-policy.toml"
                ),
            )

    def test_formula_update_transitions_revision_and_rebuild_before_first_architecture(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(
            preactivation=preactivation,
            version="1.0",
            revision=2,
            rebuild=3,
        )

        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )

        sidecar = json.loads(result.patch.files["Kandelo/formula/bash.json"])
        self.assertEqual(sidecar["version"], "1.0_2")
        self.assertEqual(sidecar["formula_revision"], 2)
        self.assertEqual(sidecar["bottle_rebuild"], 3)
        self.assertEqual(
            sidecar["bottles"][0]["link_manifest"],
            "Kandelo/link/bash-1.0_2-rebuild3-wasm32.json",
        )
        self.assertIn(
            "Kandelo/link/bash-1.0_2-rebuild3-wasm32.json",
            result.patch.files,
        )
        apply, _write_error, _store = self._metadata_writer()
        landed = apply(
            self.root,
            result.patch,
            formula_update=result.update,
            commit_message="promote revised bash",
        )
        self.assertEqual(landed.status, "committed")
        self.assertTrue(
            (self.root / "Kandelo/link/bash-1.0_2-rebuild3-wasm32.json").is_file()
        )

    def test_formula_update_validator_rejects_a_noncanonical_root(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        formula_path = "Formula/bash.rb"
        files = dict(result.patch.files)
        files[formula_path] = files[formula_path].replace(
            f"homebrew-tap-core-abi-{SUCCESSOR_ABI}/bash".encode(),
            f"homebrew-tap-core-abi-{SUCCESSOR_ABI}-candidates/bash".encode(),
        )

        with self.assertRaises(TapMetadataError):
            tap_metadata_module.validate_formula_metadata_patch(
                self.root,
                replace(result.patch, files=files),
                result.update,
            )

    def test_formula_update_preserves_a_schema_valid_mixed_case_version(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(
            preactivation=preactivation,
            version="R14B04",
        )

        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )

        sidecar = json.loads(result.patch.files["Kandelo/formula/bash.json"])
        self.assertEqual(sidecar["version"], "R14B04")
        self.assertEqual(
            sidecar["bottles"][0]["link_manifest"],
            "Kandelo/link/bash-R14B04-rebuild1-wasm32.json",
        )

    def test_formula_update_validator_rejects_false_idempotence(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )

        with self.assertRaises(TapMetadataError):
            tap_metadata_module.validate_formula_metadata_patch(
                self.root,
                replace(result.patch, files={}),
                result.update,
            )

    def test_formula_architectures_promote_independently(self) -> None:
        self._enable_wasm64()
        history, snapshot, preactivation, current = self._activate_fixture()
        wasm32 = self._prepared_admission(preactivation=preactivation)
        first = self._prepare_formula(
            prepared=wasm32,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        first_sidecar = json.loads(first.patch.files["Kandelo/formula/bash.json"])
        self.assertEqual(
            [(item["arch"], item["status"]) for item in first_sidecar["bottles"]],
            [("wasm32", "success"), ("wasm64", "pending")],
        )
        current = self._materialize_patch(first.patch, "promote bash wasm32")

        wasm64 = self._prepared_admission(
            preactivation=preactivation,
            architecture="wasm64",
            canonical_digest="8" * 64,
        )
        second = self._prepare_formula(
            prepared=wasm64,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        sidecar = json.loads(second.patch.files["Kandelo/formula/bash.json"])
        self.assertEqual(
            [(item["arch"], item["status"]) for item in sidecar["bottles"]],
            [("wasm32", "success"), ("wasm64", "success")],
        )
        formula = second.patch.files["Formula/bash.rb"].decode()
        self.assertIn(
            f'wasm32_kandelo: "{wasm32.promoted_layer["sha256"]}"', formula
        )
        self.assertIn(
            f'wasm64_kandelo: "{wasm64.promoted_layer["sha256"]}"', formula
        )

    def test_formula_update_uses_captured_guest_layout_for_relocatable_bottle(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        metadata = copy.deepcopy(dict(prepared.candidate_bottle_metadata))
        metadata["bash"]["bottle"]["cellar"] = "any"
        prepared = replace(prepared, candidate_bottle_metadata=metadata)

        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )

        self.assertIn(
            'sha256 cellar: "/opt/kandelo/homebrew/Cellar", wasm32_kandelo:',
            result.patch.files["Formula/bash.rb"].decode(),
        )

    def test_formula_update_preserves_inline_patch_payload(self) -> None:
        formula_path = self.root / "Formula/bash.rb"
        formula_path.write_text(
            formula_path.read_text()
            + "__END__\n"
            + "diff --git a/source.c b/source.c\n"
            + "--- a/source.c\n"
            + "+++ b/source.c\n"
        )
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)

        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )

        output = result.patch.files["Formula/bash.rb"]
        self.assertEqual(
            tap_metadata_module.normalize_formula_source(output),
            tap_metadata_module.normalize_formula_source(formula_path.read_bytes()),
        )
        self.assertTrue(output.endswith(b"+++ b/source.c\n"))

    def test_formula_update_is_idempotent_after_exact_landing(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        first = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        current = self._materialize_patch(first.patch, "promote bash wasm32")

        repeated = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )

        self.assertEqual(dict(repeated.patch.files), {})
        self.assertEqual(repeated.update.expected_main_commit, current["commit"])

    def test_admission_progress_requires_the_exact_current_metadata_projection(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        planned = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        self._materialize_patch(planned.patch, "promote bash wasm32")
        validate = getattr(
            tap_metadata_module, "validate_formula_admission_projection", None
        )
        if validate is None:
            self.fail("admission progress does not revalidate current metadata")

        validate(self.root, planned.update)
        sidecar_path = self.root / "Kandelo/formula/bash.json"
        sidecar = json.loads(sidecar_path.read_bytes())
        sidecar["bottles"][0]["sha256"] = "f" * 64
        sidecar_path.write_bytes(canonical_bytes(sidecar))
        with self.assertRaises(tap_metadata_module.TapMetadataError):
            validate(self.root, planned.update)

    def test_landed_metadata_commit_has_the_exact_base_and_changed_paths(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        planned = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        landed = self._materialize_patch(planned.patch, "promote bash wasm32")

        tap_metadata_module.validate_landed_formula_metadata_commit(
            self.root,
            base_source=current,
            landed_source=landed,
            patch=planned.patch,
        )
        with self.assertRaises(tap_metadata_module.TapMetadataError):
            tap_metadata_module.validate_landed_formula_metadata_commit(
                self.root,
                base_source={**current, "commit": "f" * 40},
                landed_source=landed,
                patch=planned.patch,
            )

    def test_landed_metadata_without_admission_recovers_its_original_cas(self) -> None:
        history, snapshot, preactivation, base = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        planned = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=base,
        )
        landed = self._materialize_patch(planned.patch, "promote bash wasm32")
        (self.root / "later-change.txt").write_text("later metadata wave\n")
        _git(self.root, "add", "--", "later-change.txt")
        _git(self.root, "commit", "-m", "later metadata wave")
        later = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": _git(self.root, "rev-parse", "HEAD"),
            "tree": _git(self.root, "rev-parse", "HEAD^{tree}"),
        }
        repeated = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=later,
        )
        self.assertEqual(dict(repeated.patch.files), {})

        recover = getattr(
            tap_metadata_module, "recover_landed_formula_metadata_commit", None
        )
        if recover is None:
            self.fail("landed Formula metadata has no admission retry recovery")
        recovered = recover(self.root, current_update=repeated.update)

        self.assertEqual(dict(recovered.base_source), base)
        self.assertEqual(dict(recovered.landed_source), landed)
        self.assertEqual(recovered.update, planned.update)
        self.assertEqual(recovered.patch, planned.patch)
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), later["commit"])

    def _metadata_writer(self) -> tuple[object, type[Exception], type[object]]:
        apply = getattr(tap_metadata_module, "apply_metadata_patch", None)
        error = getattr(tap_metadata_module, "TapMetadataWriteError", None)
        store = getattr(tap_metadata_module, "GitTapMetadataStore", None)
        if apply is None or error is None or store is None:
            self.fail("contents-only metadata CAS writer is absent")
        return apply, error, store

    def test_contents_writer_applies_the_validated_activation(self) -> None:
        source = self._commit_fixture()
        history, snapshot = self._history_authority(source)
        patch = self._prepare_activation(
            history=history,
            snapshot=snapshot,
            source=source,
        )
        apply, _write_error, _store = self._metadata_writer()

        landed = apply(
            self.root,
            patch,
            commit_message="activate successor ABI",
        )

        self.assertEqual(landed.status, "committed")
        self.assertEqual(
            load_abi_state(self.root / "Kandelo/abi-state.json").current_abi,
            SUCCESSOR_ABI,
        )

    def test_formula_cas_replans_after_another_formula_lands(self) -> None:
        self._add_dash()
        history, snapshot, preactivation, current = self._activate_fixture()
        bash_prepared = self._prepared_admission(preactivation=preactivation)
        bash_old = self._prepare_formula(
            prepared=bash_prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        dash_prepared = self._prepared_admission(
            preactivation=preactivation,
            formula="dash",
            canonical_digest="b" * 64,
        )
        dash = self._prepare_formula(
            prepared=dash_prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        apply, write_error, _store = self._metadata_writer()
        landed = apply(
            self.root,
            dash.patch,
            formula_update=dash.update,
            commit_message="promote dash wasm32",
        )
        self.assertEqual(landed.status, "committed")

        with self.assertRaises(write_error) as conflict:
            apply(
                self.root,
                bash_old.patch,
                formula_update=bash_old.update,
                commit_message="stale bash promotion",
            )
        self.assertEqual(conflict.exception.guard_code, "tap_source_drift")

        current = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": _git(self.root, "rev-parse", "HEAD"),
            "tree": _git(self.root, "rev-parse", "HEAD^{tree}"),
        }
        bash_replanned = self._prepare_formula(
            prepared=bash_prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        apply(
            self.root,
            bash_replanned.patch,
            formula_update=bash_replanned.update,
            commit_message="promote bash wasm32",
        )
        dash_sidecar = json.loads(
            (self.root / "Kandelo/formula/dash.json").read_bytes()
        )
        self.assertEqual(
            dash_sidecar["bottles"][0]["sha256"],
            dash_prepared.promoted_layer["sha256"],
        )

    def test_formula_cas_rejects_unexpected_worktree_change(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        (self.root / "unexpected.txt").write_text("not generated\n")
        apply, write_error, _store = self._metadata_writer()
        with self.assertRaises(write_error) as conflict:
            apply(
                self.root,
                result.patch,
                formula_update=result.update,
                commit_message="must not land",
            )
        self.assertEqual(conflict.exception.guard_code, "tap_source_drift")

    def test_contents_writer_revalidates_formula_semantics(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        files = dict(result.patch.files)
        files["Formula/bash.rb"] = files["Formula/bash.rb"].replace(
            f"homebrew-tap-core-abi-{SUCCESSOR_ABI}/bash".encode(),
            f"homebrew-tap-core-abi-{SUCCESSOR_ABI}-candidates/bash".encode(),
        )
        apply, write_error, _store = self._metadata_writer()

        with self.assertRaises(write_error) as rejected:
            apply(
                self.root,
                replace(result.patch, files=files),
                formula_update=result.update,
                commit_message="must revalidate",
            )
        self.assertEqual(rejected.exception.guard_code, "tap_source_drift")

    def test_contents_writer_rejects_traversal_before_touching_the_path(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        outside = self.root.parent / "outside-metadata.txt"
        outside.write_bytes(b"must remain unchanged\n")
        outside_digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        allowed_paths = (*result.update.allowed_paths, "../outside-metadata.txt")
        forged_update = replace(result.update, allowed_paths=allowed_paths)
        forged_patch = replace(
            result.patch,
            allowed_paths=allowed_paths,
            expected_files_sha256={
                **dict(result.patch.expected_files_sha256),
                "../outside-metadata.txt": outside_digest,
            },
            files={
                **dict(result.patch.files),
                "../outside-metadata.txt": b"escaped the checkout\n",
            },
        )
        apply, write_error, _store = self._metadata_writer()

        with self.assertRaises(write_error) as rejected:
            apply(
                self.root,
                forged_patch,
                formula_update=forged_update,
                commit_message="must not escape",
            )

        self.assertEqual(rejected.exception.guard_code, "tap_source_drift")
        self.assertEqual(outside.read_bytes(), b"must remain unchanged\n")

    def test_bottle_link_inventory_rejects_traversal_and_escape_links(self) -> None:
        traversal = tarfile.TarInfo("bash/1.0/../../outside")
        traversal.size = 0
        with self.assertRaises(BottleLinkError):
            inspect_bottle_link_inventory(
                _unsafe_bottle_archive(traversal), formula="bash", version="1.0"
            )

        escape = tarfile.TarInfo("bash/1.0/bin/escape")
        escape.type = tarfile.SYMTYPE
        escape.linkname = "../../../outside"
        with self.assertRaises(BottleLinkError):
            inspect_bottle_link_inventory(
                _unsafe_bottle_archive(escape), formula="bash", version="1.0"
            )

        control = tarfile.TarInfo("bash/1.0/share/bad\nname")
        control.size = 0
        with self.assertRaises(BottleLinkError):
            inspect_bottle_link_inventory(
                _unsafe_bottle_archive(control), formula="bash", version="1.0"
            )

    def test_formula_cas_uses_non_force_push_and_reports_race(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        result = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        remote = self.root.parent / "metadata-remote.git"
        other = self.root.parent / "metadata-racer"
        _git(remote.parent, "init", "--bare", str(remote))
        _git(self.root, "remote", "add", "origin", str(remote))
        _git(self.root, "push", "-u", "origin", "main")
        _git(remote.parent, "clone", str(remote), str(other))
        _git(other, "config", "user.name", "Concurrent metadata writer")
        _git(other, "config", "user.email", "racer@example.invalid")
        apply, write_error, store_type = self._metadata_writer()

        class RacingStore(store_type):
            def push(self, expected_main: str, new_commit: str) -> None:
                (other / "Kandelo/race.json").write_bytes(
                    canonical_bytes({"writer": "concurrent"})
                )
                _git(other, "add", "Kandelo/race.json")
                _git(other, "commit", "-m", "concurrent main update")
                _git(other, "push", "origin", "main")
                super().push(expected_main, new_commit)

        with self.assertRaises(write_error) as conflict:
            apply(
                self.root,
                result.patch,
                formula_update=result.update,
                commit_message="promote bash wasm32",
                store=RacingStore(self.root, remote="origin", branch="main"),
            )
        self.assertEqual(conflict.exception.guard_code, "metadata_cas_conflict")

    def test_contents_writer_returns_exact_idempotent_landing(self) -> None:
        history, snapshot, preactivation, current = self._activate_fixture()
        prepared = self._prepared_admission(preactivation=preactivation)
        first = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        apply, _write_error, _store = self._metadata_writer()
        apply(
            self.root,
            first.patch,
            formula_update=first.update,
            commit_message="promote bash wasm32",
        )
        current = {
            "repository": "kandelo-dev/homebrew-tap-core",
            "commit": _git(self.root, "rev-parse", "HEAD"),
            "tree": _git(self.root, "rev-parse", "HEAD^{tree}"),
        }
        repeated = self._prepare_formula(
            prepared=prepared,
            history=history,
            snapshot=snapshot,
            current=current,
        )
        before = _git(self.root, "rev-parse", "HEAD")
        landed = apply(
            self.root,
            repeated.patch,
            formula_update=repeated.update,
            commit_message="idempotent bash promotion",
        )
        self.assertEqual(landed.status, "already-landed")
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), before)


if __name__ == "__main__":
    unittest.main()
