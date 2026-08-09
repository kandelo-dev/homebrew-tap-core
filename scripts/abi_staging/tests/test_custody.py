from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from scripts.abi_staging.canonical import canonical_bytes, canonical_sha256
from scripts.abi_staging.custody import (
    CustodyError,
    build_miniature_source_custody_manifest_fixture,
    create_source_custody,
    load_source_custody_manifest,
    source_capsule_digest,
    validate_source_custody,
)
from scripts.abi_staging.plan import exact_formula_subject


TAP_ROOT = Path(__file__).resolve().parents[3]
REQUEST = "a" * 64
SUBJECT = exact_formula_subject("mini-tool", "wasm32")
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Custody Fixture",
    "GIT_AUTHOR_EMAIL": "custody@example.test",
    "GIT_COMMITTER_NAME": "Custody Fixture",
    "GIT_COMMITTER_EMAIL": "custody@example.test",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}


def _git(root: Path, *arguments: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-C", str(root), *arguments],
        check=True,
        env=GIT_ENV,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip() if capture else ""


def _repository(path: Path, filename: str, body: str) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "--initial-branch=main")
    (path / filename).write_text(body, encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "-m", "fixture")


def _source(root: Path, repository: str) -> dict[str, str]:
    return {
        "repository": repository,
        "commit": _git(root, "rev-parse", "HEAD", capture=True),
        "tree": _git(root, "rev-parse", "HEAD^{tree}", capture=True),
    }


class SourceCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.submodule = self.root / "submodule"
        self.kandelo = self.root / "kandelo"
        self.tap = self.root / "tap"
        _repository(self.submodule, "library.txt", "submodule bytes\n")
        _repository(self.kandelo, "kernel.txt", "kernel bytes\n")
        _git(
            self.kandelo,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(self.submodule),
            "deps/mini",
        )
        _git(self.kandelo, "commit", "-am", "pin submodule")
        _repository(self.tap, "Formula.rb", "class MiniTool; end\n")
        self.kandelo_source = _source(self.kandelo, "automattic/kandelo")
        self.tap_source = _source(self.tap, "kandelo-dev/homebrew-tap-core")

    def _create(
        self, destination: Path, kandelo: Path | None = None, tap: Path | None = None
    ) -> None:
        create_source_custody(
            kandelo_root=kandelo or self.kandelo,
            tap_root=tap or self.tap,
            kandelo_source=self.kandelo_source,
            tap_source=self.tap_source,
            request_sha256=REQUEST,
            subject=SUBJECT,
            output=destination,
        )

    def _validate(self, destination: Path, **changes: object) -> dict[str, object]:
        arguments = {
            "root": destination,
            "expected_request_sha256": REQUEST,
            "expected_subject": SUBJECT,
            "expected_kandelo_source": self.kandelo_source,
            "expected_tap_source": self.tap_source,
        }
        arguments.update(changes)
        return validate_source_custody(**arguments)

    def _rewrite_manifest(self, destination: Path, mutate) -> dict[str, object]:
        path = destination / "manifest.json"
        value = json.loads(path.read_bytes())
        mutate(value)
        value["capsule_sha256"] = source_capsule_digest(value)
        path.write_bytes(canonical_bytes(value))
        return value

    def _rehash_member(self, destination: Path, relative: str) -> None:
        body = (destination / relative).read_bytes()

        def mutate(value: dict[str, object]) -> None:
            members = []
            for source in value["sources"]:
                members.extend((source["bundle"], source["tree_archive"]))
            for submodule in value["submodules"]:
                members.extend((submodule["bundle"], submodule["tree_archive"]))
            member = next(item for item in members if item["path"] == relative)
            member["sha256"] = hashlib.sha256(body).hexdigest()
            member["bytes"] = len(body)

        self._rewrite_manifest(destination, mutate)

    def test_construction_is_path_independent_exact_and_repeatable(self) -> None:
        first = self.root / "custody-first"
        second = self.root / "custody-second"
        clone_root = self.root / "alternate-host-path"
        clone_root.mkdir()
        alternate_kandelo = clone_root / "kandelo"
        alternate_tap = clone_root / "tap"
        _git(clone_root, "clone", "--recurse-submodules", str(self.kandelo), str(alternate_kandelo))
        _git(clone_root, "clone", str(self.tap), str(alternate_tap))
        self._create(first)
        self._create(second, alternate_kandelo, alternate_tap)

        first_files = {
            path.relative_to(first).as_posix(): path.read_bytes()
            for path in first.rglob("*")
            if path.is_file()
        }
        second_files = {
            path.relative_to(second).as_posix(): path.read_bytes()
            for path in second.rglob("*")
            if path.is_file()
        }
        self.assertEqual(first_files, second_files)
        manifest = self._validate(first)
        self.assertEqual(manifest["sources"][0]["commit"], self.kandelo_source["commit"])
        self.assertEqual(manifest["sources"][0]["tree"], self.kandelo_source["tree"])
        self.assertEqual(len(manifest["submodules"]), 1)
        self.assertEqual(manifest["submodules"][0]["path"], "deps/mini")
        self.assertEqual(
            manifest["submodules"][0]["gitlink_commit"],
            _git(self.submodule, "rev-parse", "HEAD", capture=True),
        )

    def test_bundle_object_tree_and_replacement_ref_fail_closed(self) -> None:
        for mutation in ("bundle", "tree", "replacement"):
            with self.subTest(mutation=mutation):
                destination = self.root / f"custody-{mutation}"
                self._create(destination)
                if mutation == "bundle":
                    path = destination / "kandelo.bundle"
                    path.write_bytes(path.read_bytes()[:64])
                    self._rehash_member(destination, "kandelo.bundle")
                elif mutation == "tree":
                    self._rewrite_manifest(
                        destination,
                        lambda value: value["sources"][0].__setitem__("tree", "f" * 40),
                    )
                else:
                    path = destination / "kandelo.bundle"
                    body = path.read_bytes().replace(b" HEAD\n", b" refs/replace/custody\n", 1)
                    path.write_bytes(body)
                    self._rehash_member(destination, "kandelo.bundle")
                with self.assertRaises(CustodyError):
                    self._validate(destination)

    def test_submodule_omission_extra_member_and_gitlink_drift_fail_closed(self) -> None:
        for mutation in ("omitted", "extra", "gitlink"):
            with self.subTest(mutation=mutation):
                destination = self.root / f"custody-{mutation}"
                self._create(destination)
                if mutation == "omitted":
                    self._rewrite_manifest(destination, lambda value: value["submodules"].clear())
                elif mutation == "extra":
                    (destination / "extra").write_text("extra\n", encoding="utf-8")
                else:
                    self._rewrite_manifest(
                        destination,
                        lambda value: value["submodules"][0].__setitem__(
                            "gitlink_commit", "e" * 40
                        ),
                    )
                with self.assertRaises(CustodyError):
                    self._validate(destination)

    def test_unsafe_archive_link_hash_size_malformed_manifest_and_symlink_fail(self) -> None:
        for mutation in (
            "archive",
            "hash",
            "size",
            "manifest",
            "member-path",
            "directory",
            "symlink",
        ):
            with self.subTest(mutation=mutation):
                destination = self.root / f"custody-{mutation}"
                self._create(destination)
                if mutation == "archive":
                    stream = io.BytesIO()
                    with tarfile.open(fileobj=stream, mode="w") as archive:
                        body = b"escape\n"
                        member = tarfile.TarInfo("../escape")
                        member.size = len(body)
                        archive.addfile(member, io.BytesIO(body))
                    path = destination / "kandelo-tree.tar"
                    path.write_bytes(stream.getvalue())
                    self._rehash_member(destination, "kandelo-tree.tar")
                elif mutation in {"hash", "size"}:
                    def change(value: dict[str, object]) -> None:
                        field = "sha256" if mutation == "hash" else "bytes"
                        value["sources"][0]["bundle"][field] = (
                            "0" * 64 if mutation == "hash" else 1
                        )
                    self._rewrite_manifest(destination, change)
                elif mutation == "manifest":
                    (destination / "manifest.json").write_bytes(b'{"schema":1}\n')
                elif mutation == "member-path":
                    self._rewrite_manifest(
                        destination,
                        lambda value: value["sources"][0]["bundle"].__setitem__(
                            "path", "tap.bundle"
                        ),
                    )
                elif mutation == "directory":
                    (destination / "unexpected").mkdir()
                else:
                    path = destination / "tap.bundle"
                    path.unlink()
                    path.symlink_to("kandelo.bundle")
                with self.assertRaises(CustodyError):
                    self._validate(destination)

    def test_request_subject_and_tap_plan_identity_are_not_interchangeable(self) -> None:
        destination = self.root / "custody-context"
        self._create(destination)
        mutations = (
            {"expected_request_sha256": "f" * 64},
            {"expected_subject": exact_formula_subject("other", "wasm32")},
            {"expected_tap_source": {**self.tap_source, "commit": "f" * 40}},
        )
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(CustodyError):
                self._validate(destination, **changes)

        manifest = load_source_custody_manifest(
            (destination / "manifest.json").read_bytes()
        )
        other_context = copy.deepcopy(manifest)
        other_context["request_sha256"] = "f" * 64
        other_context["subject"] = exact_formula_subject("other", "wasm32")
        self.assertEqual(
            source_capsule_digest(other_context), manifest["capsule_sha256"]
        )

    def test_checked_manifest_fixture_is_canonical_and_repeatable(self) -> None:
        expected = build_miniature_source_custody_manifest_fixture()
        fixture = TAP_ROOT / "Kandelo/staging/fixtures/source-custody/manifest.json"
        self.assertEqual(fixture.read_bytes(), canonical_bytes(expected))
        self.assertEqual(load_source_custody_manifest(fixture.read_bytes()), expected)
        self.assertEqual(expected["capsule_sha256"], source_capsule_digest(expected))
        self.assertEqual(
            canonical_sha256(expected), hashlib.sha256(fixture.read_bytes()).hexdigest()
        )


if __name__ == "__main__":
    unittest.main()
