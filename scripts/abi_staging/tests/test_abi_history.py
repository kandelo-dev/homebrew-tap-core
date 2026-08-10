from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.abi_staging.abi_history import (
    AbiHistoryError,
    GitHubHistoryClient,
    LocalGitRefStore,
    build_history_plan,
    build_history_record,
    ensure_history_ref,
    protection_requirement_sha256,
    validate_history_creation_handoff,
    validate_history_plan,
    validate_protection_snapshot,
    verify_history_snapshot,
)
from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.records import validate_abi_history_record


SOURCE_ABI = 7
SUCCESSOR_ABI = SOURCE_ABI + 1
SNAPSHOT_SHA = "a" * 64
TREE_SENTINEL = "b" * 40
BOTTLE_BYTES = b"miniature immutable bottle\n"
BOTTLE_SHA = hashlib.sha256(BOTTLE_BYTES).hexdigest()
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _run(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


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
            "formula_sha256": SNAPSHOT_SHA,
            "kandelo_commit": "1" * 40,
            "kandelo_repository": "Automattic/kandelo",
            "tap_commit": "2" * 40,
            "tap_repository": "kandelo-dev/homebrew-tap-core",
        },
        "bytes": len(BOTTLE_BYTES),
        "cache_key_sha": BOTTLE_SHA,
        "cellar": ":any_skip_relocation",
        "fork_instrumentation": "not-required",
        "kandelo_abi": SOURCE_ABI,
        "link_manifest": "Kandelo/link/bash-1.0-rebuild1-wasm32.json",
        "prefix": "/opt/kandelo/homebrew",
        "runtime_support": ["node"],
        "sha256": BOTTLE_SHA,
        "status": "success",
        "url": f"https://objects.example.invalid/{BOTTLE_SHA}",
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


def _sidecar() -> dict[str, object]:
    package = _package()
    package.pop("formula_metadata")
    return {
        **package,
        "kandelo_abi": SOURCE_ABI,
        "schema": 1,
        "source_metadata": "Kandelo/metadata.json",
        "tap_commit": "2" * 40,
        "tap_name": "kandelo-dev/tap-core",
        "tap_repository": "kandelo-dev/homebrew-tap-core",
    }


def _metadata() -> dict[str, object]:
    return {
        "generated_at": "2026-08-09T00:00:00Z",
        "generator": "history fixture",
        "kandelo_abi": SOURCE_ABI,
        "kandelo_commit": "1" * 40,
        "kandelo_repository": "Automattic/kandelo",
        "packages": [_package()],
        "release_tag": f"bottles-abi-v{SOURCE_ABI}",
        "schema": 1,
        "tap_commit": "2" * 40,
        "tap_name": "kandelo-dev/tap-core",
        "tap_repository": "kandelo-dev/homebrew-tap-core",
    }


def _write_tap(root: Path) -> None:
    (root / "Formula").mkdir(parents=True)
    (root / "Kandelo/formula").mkdir(parents=True)
    (root / "Kandelo/staging").mkdir(parents=True)
    (root / "Formula/bash.rb").write_text(
        "class Bash < Formula\n"
        "  bottle do\n"
        '    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"\n'
        "    rebuild 1\n"
        f'    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "{BOTTLE_SHA}"\n'
        "  end\n"
        "end\n"
    )
    (root / "Kandelo/formula/bash.json").write_text(
        json.dumps(_sidecar(), indent=2, sort_keys=True) + "\n"
    )
    (root / "Kandelo/metadata.json").write_text(
        json.dumps(_metadata(), indent=2, sort_keys=True) + "\n"
    )
    (root / "Kandelo/abi-state.json").write_bytes(
        canonical_bytes(
            {
                "activation": None,
                "current_abi": SOURCE_ABI,
                "current_snapshot_sha256": SNAPSHOT_SHA,
                "kind": "kandelo-homebrew-abi-state",
                "schema": 1,
            }
        )
    )
    (root / "Kandelo/staging/promotion-policy.toml").write_text(_policy())
    (root / "Kandelo/staging/promotion-activation.toml").write_text(
        'schema = 1\nkind = "kandelo-abi-staging-promotion-activation"\nmode = "observe"\n'
    )


def _snapshot(
    *,
    branch: str,
    phase: str,
    reference: dict[str, str] | None,
    direct: dict[str, object] | None = None,
    rulesets: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema": 1,
        "kind": "kandelo-abi-history-protection-snapshot",
        "repository": "kandelo-dev/homebrew-tap-core",
        "branch": branch,
        "phase": phase,
        "ref": reference,
        "direct": direct,
        "rulesets": [] if rulesets is None else rulesets,
    }


def _direct(branch: str) -> dict[str, object]:
    return {
        "branch": branch,
        "allow_deletions": False,
        "allow_force_pushes": False,
        "enforce_admins": True,
    }


def _ruleset(*, enforcement: str = "active") -> dict[str, object]:
    return {
        "id": 9,
        "name": "Protect ABI history",
        "target": "branch",
        "enforcement": enforcement,
        "include": ["refs/heads/abi/*"],
        "exclude": [],
        "rules": ["deletion", "non_fast_forward"],
        "bypass_actors": [],
    }


class _Response:
    def __init__(self, status: int, value: object) -> None:
        self.status = status
        self.body = canonical_bytes(value)
        self.headers = {"Content-Length": str(len(self.body))}

    def read(self, amount: int = -1) -> bytes:
        return self.body if amount < 0 else self.body[:amount]

    def close(self) -> None:
        return None


class AbiHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        _write_tap(self.root)
        _run(self.root, "init", "-b", "main")
        _run(self.root, "config", "user.name", "ABI History Test")
        _run(self.root, "config", "user.email", "abi-history@example.invalid")
        _run(self.root, "add", ".")
        _run(self.root, "commit", "-m", "fixture")
        self.commit = _run(self.root, "rev-parse", "HEAD")
        self.tree = _run(self.root, "rev-parse", "HEAD^{tree}")
        self.plan = build_history_plan(
            self.root,
            preactivation_tap_commit=self.commit,
            preactivation_tap_tree=self.tree,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_plan_is_generic_adjacent_and_binds_exact_metadata(self) -> None:
        self.assertEqual(self.plan["source_abi"], SOURCE_ABI)
        self.assertEqual(self.plan["successor_abi"], SUCCESSOR_ABI)
        self.assertEqual(self.plan["branch"], f"abi/{SOURCE_ABI}")
        self.assertEqual(self.plan["preactivation_tap_commit"], self.commit)
        self.assertEqual(self.plan["preactivation_tap_tree"], self.tree)
        self.assertEqual(
            self.plan["protection_requirement_sha256"],
            protection_requirement_sha256(),
        )

        changed = copy.deepcopy(self.plan)
        changed["successor_abi"] += 1
        with self.assertRaises(AbiHistoryError):
            ensure_history_ref(
                changed,
                LocalGitRefStore(self.root),
                _snapshot(
                    branch=self.plan["branch"],
                    phase="precreate",
                    reference=None,
                    rulesets=[_ruleset()],
                ),
                mode="observe",
            )

    def test_checked_history_fixture_is_canonical_and_uses_current_requirement(self) -> None:
        path = REPOSITORY_ROOT / "Kandelo/staging/fixtures/abi-history-record.json"
        body = path.read_bytes()
        record = json.loads(body)
        self.assertEqual(body, canonical_bytes(record))
        validate_abi_history_record(record)
        validate_history_plan(record["plan"])
        self.assertEqual(
            canonical_sha256(record),
            "b4cfe0546450034e1922d8afbe8c90e0814ba7069f47078409b5e5283d0ead55",
        )

    def test_creation_handoff_is_exact_and_rejects_inert_fields(self) -> None:
        evidence = validate_protection_snapshot(
            self.plan,
            _snapshot(
                branch=self.plan["branch"],
                phase="precreate",
                reference=None,
                rulesets=[_ruleset()],
            ),
            phase="precreate",
        )
        handoff = {
            "schema": 1,
            "kind": "kandelo-abi-history-ref-creation",
            "plan_sha256": canonical_sha256(self.plan),
            "action": "created",
            "ref": {"object": self.commit, "tree": self.tree},
            "protection_evidence": evidence,
        }
        self.assertEqual(
            validate_history_creation_handoff(self.plan, handoff), handoff
        )
        changed = copy.deepcopy(handoff)
        changed["ignored"] = True
        with self.assertRaisesRegex(AbiHistoryError, "fields changed"):
            validate_history_creation_handoff(self.plan, changed)

    def test_ref_creation_is_no_force_exact_and_idempotent(self) -> None:
        store = LocalGitRefStore(self.root)
        preflight = _snapshot(
            branch=self.plan["branch"],
            phase="precreate",
            reference=None,
            rulesets=[_ruleset()],
        )
        observed = ensure_history_ref(self.plan, store, preflight, mode="observe")
        self.assertEqual(observed["action"], "would-create")
        self.assertIsNone(store.read(self.plan["branch"]))

        created = ensure_history_ref(self.plan, store, preflight, mode="active")
        self.assertEqual(created["action"], "created")
        self.assertEqual(store.read(self.plan["branch"]).object_sha, self.commit)
        existing = ensure_history_ref(
            self.plan,
            store,
            _snapshot(
                branch=self.plan["branch"],
                phase="precreate",
                reference={"object": self.commit, "tree": self.tree},
                direct=_direct(self.plan["branch"]),
            ),
            mode="active",
        )
        self.assertEqual(existing["action"], "already-exact")

        _run(self.root, "branch", "-D", self.plan["branch"])
        other = self.root / "new.txt"
        other.write_text("main moved\n")
        _run(self.root, "add", "new.txt")
        _run(self.root, "commit", "-m", "move main")
        with self.assertRaisesRegex(AbiHistoryError, "main moved"):
            ensure_history_ref(self.plan, store, preflight, mode="active")

    def test_existing_wrong_history_ref_is_never_forced(self) -> None:
        store = LocalGitRefStore(self.root)
        wrong = _run(
            self.root,
            "commit-tree",
            f"{self.commit}^{{tree}}",
            "-p",
            self.commit,
            "-m",
            "wrong",
        )
        _run(self.root, "branch", self.plan["branch"], wrong)
        with self.assertRaisesRegex(AbiHistoryError, "already exists"):
            ensure_history_ref(
                self.plan,
                store,
                _snapshot(
                    branch=self.plan["branch"],
                    phase="precreate",
                    reference={"object": wrong, "tree": _run(self.root, "rev-parse", f"{wrong}^{{tree}}")},
                    direct=_direct(self.plan["branch"]),
                ),
                mode="active",
            )
        self.assertEqual(store.read(self.plan["branch"]).object_sha, wrong)

    def test_protection_requires_exact_active_nonbypass_coverage(self) -> None:
        branch = self.plan["branch"]
        reference = {"object": self.commit, "tree": self.tree}
        ruleset = _snapshot(
            branch=branch,
            phase="precreate",
            reference=None,
            rulesets=[_ruleset()],
        )
        self.assertEqual(
            validate_protection_snapshot(self.plan, ruleset, phase="precreate")["source"],
            "ruleset",
        )
        direct = _snapshot(
            branch=branch,
            phase="postcreate",
            reference=reference,
            direct=_direct(branch),
        )
        self.assertEqual(
            validate_protection_snapshot(self.plan, direct, phase="postcreate")["source"],
            "branch-protection",
        )

        invalid = []
        invalid.append(_snapshot(branch=branch, phase="precreate", reference=None))
        invalid.append(
            _snapshot(
                branch=branch,
                phase="precreate",
                reference=None,
                rulesets=[_ruleset(enforcement="disabled")],
            )
        )
        bypass = _ruleset()
        bypass["bypass_actors"] = [{"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}]
        invalid.append(
            _snapshot(
                branch=branch,
                phase="precreate",
                reference=None,
                rulesets=[bypass],
            )
        )
        invalid.append(
            _snapshot(
                branch="abi/999",
                phase="precreate",
                reference=None,
                rulesets=[_ruleset()],
            )
        )
        invalid.append(
            _snapshot(
                branch=branch,
                phase="precreate",
                reference=None,
                direct=_direct("abi/999"),
            )
        )
        invalid.append(
            _snapshot(
                branch=branch,
                phase="precreate",
                reference=None,
                rulesets=[{**_ruleset(), "rules": []}],
            )
        )
        invalid.append(
            _snapshot(
                branch=branch,
                phase="precreate",
                reference=None,
                rulesets=[
                    {**_ruleset(), "exclude": [f"refs/heads/{branch}*"]}
                ],
            )
        )
        for snapshot in invalid:
            with self.subTest(snapshot=snapshot), self.assertRaises(AbiHistoryError):
                validate_protection_snapshot(self.plan, snapshot, phase="precreate")

        with self.assertRaisesRegex(AbiHistoryError, "phase"):
            validate_protection_snapshot(self.plan, ruleset, phase="postcreate")
        moved = copy.deepcopy(direct)
        moved["ref"]["tree"] = TREE_SENTINEL
        with self.assertRaisesRegex(AbiHistoryError, "ref"):
            validate_protection_snapshot(self.plan, moved, phase="postcreate")

    def test_github_adapter_derives_ruleset_facts_without_boolean_authority(self) -> None:
        expected = [
            (
                "GET",
                "/repos/kandelo-dev/homebrew-tap-core/git/ref/heads/abi%2F7",
                404,
                {"message": "Not Found"},
            ),
            (
                "GET",
                "/repos/kandelo-dev/homebrew-tap-core/rulesets?includes_parents=true&per_page=100",
                200,
                [{"id": 9}],
            ),
            (
                "GET",
                "/repos/kandelo-dev/homebrew-tap-core/rulesets/9",
                200,
                {
                    "id": 9,
                    "name": "Protect ABI history",
                    "target": "branch",
                    "enforcement": "active",
                    "conditions": {
                        "ref_name": {
                            "include": ["refs/heads/abi/*"],
                            "exclude": [],
                        }
                    },
                    "rules": [
                        {"type": "non_fast_forward"},
                        {"type": "deletion"},
                    ],
                    "bypass_actors": [],
                },
            ),
        ]

        def opener(request: object) -> _Response:
            method, path, status, value = expected.pop(0)
            self.assertEqual(request.get_method(), method)
            parsed = request.full_url.split("api.github.com", 1)[1]
            self.assertEqual(parsed, path)
            return _Response(status, value)

        client = GitHubHistoryClient(
            "kandelo-dev/homebrew-tap-core", "token", opener=opener
        )
        snapshot = client.protection_snapshot(self.plan["branch"], phase="precreate")
        evidence = validate_protection_snapshot(
            self.plan,
            snapshot,
            phase="precreate",
            expected_repository="kandelo-dev/homebrew-tap-core",
        )
        self.assertEqual(evidence["source"], "ruleset")
        self.assertEqual(expected, [])

    def test_github_adapter_never_treats_forbidden_ref_as_absent(self) -> None:
        client = GitHubHistoryClient(
            "kandelo-dev/homebrew-tap-core",
            "token",
            opener=lambda request: _Response(403, {"message": "Forbidden"}),
        )
        with self.assertRaisesRegex(AbiHistoryError, "ref read returned HTTP 403"):
            client.read(self.plan["branch"])

    def test_github_adapter_supports_anonymous_exact_ref_readback(self) -> None:
        expected = [
            (200, {"ref": f"refs/heads/{self.plan['branch']}", "object": {"type": "commit", "sha": self.commit}}),
            (200, {"sha": self.commit, "tree": {"sha": self.tree}}),
        ]

        def opener(request: object) -> _Response:
            self.assertIsNone(request.get_header("Authorization"))
            status, value = expected.pop(0)
            return _Response(status, value)

        client = GitHubHistoryClient(
            "kandelo-dev/homebrew-tap-core", "", opener=opener
        )
        self.assertEqual(
            client.read(self.plan["branch"]),
            LocalGitRefStore(self.root).read("main"),
        )
        self.assertEqual(expected, [])

    def test_github_adapter_falls_back_to_rulesets_when_direct_protection_is_forbidden(
        self,
    ) -> None:
        expected = [
            (200, {"ref": f"refs/heads/{self.plan['branch']}", "object": {"type": "commit", "sha": self.commit}}),
            (200, {"sha": self.commit, "tree": {"sha": self.tree}}),
            (403, {"message": "Forbidden"}),
            (200, [{"id": 9}]),
            (
                200,
                {
                    "id": 9,
                    "name": "Protect ABI history",
                    "target": "branch",
                    "enforcement": "active",
                    "conditions": {
                        "ref_name": {
                            "include": ["refs/heads/abi/*"],
                            "exclude": [],
                        }
                    },
                    "rules": [
                        {"type": "non_fast_forward"},
                        {"type": "deletion"},
                    ],
                    "bypass_actors": [],
                },
            ),
        ]

        def opener(request: object) -> _Response:
            status, value = expected.pop(0)
            return _Response(status, value)

        client = GitHubHistoryClient(
            "kandelo-dev/homebrew-tap-core", "token", opener=opener
        )
        snapshot = client.protection_snapshot(
            self.plan["branch"], phase="postcreate"
        )
        evidence = validate_protection_snapshot(
            self.plan, snapshot, phase="postcreate"
        )
        self.assertEqual(evidence["source"], "ruleset")
        self.assertEqual(expected, [])

    def test_postcreate_verification_binds_metadata_bottles_and_record(self) -> None:
        store = LocalGitRefStore(self.root)
        _run(self.root, "branch", self.plan["branch"], self.commit)
        snapshot = _snapshot(
            branch=self.plan["branch"],
            phase="postcreate",
            reference={"object": self.commit, "tree": self.tree},
            direct=_direct(self.plan["branch"]),
        )
        verified = verify_history_snapshot(
            self.root,
            self.plan,
            store,
            snapshot,
            anonymous_reader=lambda url, maximum: BOTTLE_BYTES,
        )
        record = build_history_record(
            self.plan,
            created_ref_object=self.commit,
            protection_evidence=verified["protection_evidence"],
            metadata_verification_sha256=verified["metadata_verification_sha256"],
            public_readback_sha256=verified["public_readback_sha256"],
            run={
                "repository": "kandelo-dev/homebrew-tap-core",
                "workflow_ref": ".github/workflows/abi-staging-abi-history.yml@refs/heads/main",
                "run_id": 9,
                "run_attempt": 1,
                "job": "verify-and-publish-history",
            },
        )
        validate_abi_history_record(record)
        self.assertEqual(
            canonical_bytes(record),
            canonical_bytes(json.loads(canonical_bytes(record))),
        )

        with self.assertRaisesRegex(AbiHistoryError, "bottle"):
            verify_history_snapshot(
                self.root,
                self.plan,
                store,
                snapshot,
                anonymous_reader=lambda url, maximum: b"different bytes",
            )
        (self.root / "Kandelo/formula/bash.json").unlink()
        with self.assertRaises(AbiHistoryError):
            verify_history_snapshot(
                self.root,
                self.plan,
                store,
                snapshot,
                anonymous_reader=lambda url, maximum: BOTTLE_BYTES,
            )


if __name__ == "__main__":
    unittest.main()
