#!/usr/bin/env python3
"""Seal and verify the inert source overlay for the prefix campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, NoReturn


sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_AUTHORITY = ROOT / "Kandelo/prefix-campaign-authority.json"
DEFAULT_MANIFEST = (
    ROOT / "Kandelo/campaigns/prefix-v1/manifest.json"
)
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_PATH = re.compile(r"^[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*$")
MAX_JSON_BYTES = 16 * 1024 * 1024


class SourceError(RuntimeError):
    """A fail-closed campaign-source error."""


def fail(message: str) -> NoReturn:
    raise SourceError(message)


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def reject_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"JSON repeats key {key!r}")
        result[key] = value
    return result


def load_json(path: pathlib.Path, label: str) -> tuple[Any, bytes]:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as error:
        fail(f"{label} is unavailable: {error}")
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        fail(f"{label} must be a regular non-symlink file")
    if len(payload) > MAX_JSON_BYTES:
        fail(f"{label} exceeds the size ceiling")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"{label} is not valid JSON: {error}")
    if payload != canonical_json(value):
        fail(f"{label} is not canonical JSON")
    return value, payload


def exact_keys(
    value: Any,
    expected: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        fail(f"{label} must contain exactly {sorted(expected)}")
    return value


def relative_path(value: Any, label: str) -> pathlib.PurePosixPath:
    if (
        not isinstance(value, str)
        or SAFE_PATH.fullmatch(value) is None
    ):
        fail(f"{label} is not a normalized repository path")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        fail(f"{label} escapes the repository")
    return path


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def git_object_id(kind: str, payload: bytes) -> str:
    header = f"{kind} {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def file_mode(metadata: os.stat_result) -> str:
    if not stat.S_ISREG(metadata.st_mode):
        fail("campaign source contains a non-regular file")
    return (
        "100755"
        if metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else "100644"
    )


def source_tree_oid(root: pathlib.Path) -> str:
    try:
        metadata = root.lstat()
    except OSError as error:
        fail(f"campaign source root is unavailable: {error}")
    if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
        fail("campaign source root must be a real directory")

    def visit(directory: pathlib.Path) -> bytes:
        entries: list[tuple[bytes, bytes]] = []
        for child in directory.iterdir():
            name = child.name.encode("utf-8")
            if b"\0" in name or b"/" in name:
                fail("campaign source has an unsafe entry name")
            child_metadata = child.lstat()
            if stat.S_ISDIR(child_metadata.st_mode) and not child.is_symlink():
                child_payload = visit(child)
                mode = b"40000"
                object_id = git_object_id("tree", child_payload)
                sort_key = name + b"/"
            elif stat.S_ISREG(child_metadata.st_mode) and not child.is_symlink():
                payload = child.read_bytes()
                mode = file_mode(child_metadata).encode("ascii")
                object_id = git_object_id("blob", payload)
                sort_key = name
            elif stat.S_ISLNK(child_metadata.st_mode):
                payload = os.readlink(child).encode("utf-8")
                mode = b"120000"
                object_id = git_object_id("blob", payload)
                sort_key = name
            else:
                fail("campaign source contains a special file")
            entry = mode + b" " + name + b"\0" + bytes.fromhex(object_id)
            entries.append((sort_key, entry))
        entries.sort(key=lambda item: item[0])
        return b"".join(entry for _key, entry in entries)

    payload = visit(root)
    return git_object_id("tree", payload)


def run_git(
    arguments: list[str],
    *,
    root: pathlib.Path,
    environment: dict[str, str] | None = None,
) -> bytes:
    command_environment = os.environ.copy()
    command_environment.update(environment or {})
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=command_environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        fail(f"git {' '.join(arguments)} failed: {error}")
    return result.stdout


def git_file(
    root: pathlib.Path,
    revision: str,
    path: pathlib.PurePosixPath,
) -> tuple[bytes, str, str]:
    record = run_git(
        ["ls-tree", revision, "--", path.as_posix()],
        root=root,
    ).decode("utf-8").rstrip("\n")
    if not record:
        fail(f"{path} is absent from {revision}")
    prefix, record_path = record.split("\t", 1)
    mode, kind, object_id = prefix.split(" ")
    if (
        record_path != path.as_posix()
        or kind != "blob"
        or mode not in ("100644", "100755")
        or SHA.fullmatch(object_id) is None
    ):
        fail(f"{path} has an unsupported Git entry")
    payload = run_git(
        ["show", f"{revision}:{path.as_posix()}"],
        root=root,
    )
    if git_object_id("blob", payload) != object_id:
        fail(f"{path} differs from its Git object identity")
    return payload, mode, object_id


def git_changed_paths(
    root: pathlib.Path,
    base: str,
    target: str,
) -> list[tuple[str, pathlib.PurePosixPath]]:
    output = run_git(
        [
            "diff",
            "--name-status",
            "--no-renames",
            "-z",
            base,
            target,
            "--",
        ],
        root=root,
    )
    fields = output.split(b"\0")
    if fields[-1] != b"":
        fail("Git produced a non-terminated changed-path list")
    fields.pop()
    if len(fields) % 2:
        fail("Git produced a malformed changed-path list")
    records: list[tuple[str, pathlib.PurePosixPath]] = []
    for index in range(0, len(fields), 2):
        status_value = fields[index].decode("ascii")
        path_value = fields[index + 1].decode("utf-8")
        if status_value not in ("A", "M"):
            fail(f"campaign source does not support status {status_value}")
        records.append(
            (
                status_value,
                relative_path(path_value, "changed path"),
            )
        )
    if not records:
        fail("campaign source has no changed paths")
    return records


def seal_manifest(
    *,
    root: pathlib.Path,
    source_root: pathlib.Path,
    base: str,
    target: str,
    output: pathlib.Path,
) -> None:
    if SHA.fullmatch(base) is None or SHA.fullmatch(target) is None:
        fail("base and target must be exact commits")
    root = root.resolve()
    source_root = source_root.resolve()
    try:
        source_relative = source_root.relative_to(root).as_posix()
    except ValueError:
        fail("campaign source root must be inside the repository")
    files: list[dict[str, Any]] = []
    expected_sources: set[pathlib.Path] = set()
    for status_value, path in git_changed_paths(root, base, target):
        source = source_root.joinpath(*path.parts)
        expected_sources.add(source)
        try:
            metadata = source.lstat()
            source_payload = source.read_bytes()
        except OSError as error:
            fail(f"campaign source {path} is unavailable: {error}")
        if source.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            fail(f"campaign source {path} is not a regular file")
        target_payload, target_mode, target_blob = git_file(
            root,
            target,
            path,
        )
        if (
            source_payload != target_payload
            or file_mode(metadata) != target_mode
        ):
            fail(f"campaign source {path} differs from the target tree")
        base_record: dict[str, Any] | None
        if status_value == "A":
            base_record = None
        else:
            base_payload, base_mode, base_blob = git_file(root, base, path)
            base_record = {
                "blob_git_oid": base_blob,
                "bytes": len(base_payload),
                "mode": base_mode,
                "sha256": digest(base_payload),
            }
        files.append(
            {
                "base": base_record,
                "path": path.as_posix(),
                "target": {
                    "blob_git_oid": target_blob,
                    "bytes": len(target_payload),
                    "mode": target_mode,
                    "sha256": digest(target_payload),
                },
            }
        )
    actual_sources = {
        entry
        for entry in source_root.rglob("*")
        if entry.is_file() or entry.is_symlink()
    }
    if actual_sources != expected_sources:
        fail("campaign source file set differs from the target delta")
    base_tree = run_git(
        ["rev-parse", f"{base}^{{tree}}"],
        root=root,
    ).decode("ascii").strip()
    target_tree = run_git(
        ["rev-parse", f"{target}^{{tree}}"],
        root=root,
    ).decode("ascii").strip()
    value = {
        "base": {
            "commit": base,
            "tree_git_oid": base_tree,
        },
        "campaign": "prefix-v1",
        "files": files,
        "kind": "kandelo-homebrew-prefix-campaign-source-overlay",
        "schema": 1,
        "source_root": source_relative,
        "target_tree_git_oid": target_tree,
    }
    if output.exists() or output.is_symlink():
        fail("manifest output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json(value))


def validate_file_record(value: Any, label: str) -> dict[str, Any]:
    record = exact_keys(
        value,
        {"blob_git_oid", "bytes", "mode", "sha256"},
        label,
    )
    if (
        SHA.fullmatch(str(record["blob_git_oid"])) is None
        or not isinstance(record["bytes"], int)
        or record["bytes"] < 0
        or record["mode"] not in ("100644", "100755")
        or SHA256.fullmatch(str(record["sha256"])) is None
    ):
        fail(f"{label} has an invalid identity")
    return record


def load_manifest(
    manifest_path: pathlib.Path,
) -> tuple[dict[str, Any], bytes]:
    value, payload = load_json(manifest_path, "campaign source manifest")
    value = exact_keys(
        value,
        {
            "base",
            "campaign",
            "files",
            "kind",
            "schema",
            "source_root",
            "target_tree_git_oid",
        },
        "campaign source manifest",
    )
    base = exact_keys(
        value["base"],
        {"commit", "tree_git_oid"},
        "campaign source base",
    )
    if (
        value["schema"] != 1
        or value["kind"]
        != "kandelo-homebrew-prefix-campaign-source-overlay"
        or value["campaign"] != "prefix-v1"
        or SHA.fullmatch(str(base["commit"])) is None
        or SHA.fullmatch(str(base["tree_git_oid"])) is None
        or SHA.fullmatch(str(value["target_tree_git_oid"])) is None
    ):
        fail("campaign source manifest has an unsupported contract")
    relative_path(value["source_root"], "campaign source root")
    files = value["files"]
    if not isinstance(files, list) or not files:
        fail("campaign source manifest has no files")
    prior = ""
    for index, file_value in enumerate(files):
        file_record = exact_keys(
            file_value,
            {"base", "path", "target"},
            f"campaign source file #{index}",
        )
        path = relative_path(
            file_record["path"],
            f"campaign source file #{index} path",
        )
        if path.as_posix() <= prior:
            fail("campaign source files must be unique and sorted")
        prior = path.as_posix()
        if file_record["base"] is not None:
            validate_file_record(
                file_record["base"],
                f"campaign source file #{index} base",
            )
        validate_file_record(
            file_record["target"],
            f"campaign source file #{index} target",
        )
    return value, payload


def verify_record(
    path: pathlib.Path,
    record: dict[str, Any],
    label: str,
) -> None:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as error:
        fail(f"{label} is unavailable: {error}")
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        fail(f"{label} is not a regular file")
    if (
        len(payload) != record["bytes"]
        or digest(payload) != record["sha256"]
        or file_mode(metadata) != record["mode"]
        or git_object_id("blob", payload) != record["blob_git_oid"]
    ):
        fail(f"{label} differs from its sealed identity")


def verify_target_tree(
    root: pathlib.Path,
    manifest: dict[str, Any],
    source_root: pathlib.Path,
) -> None:
    base = manifest["base"]
    actual_base_tree = run_git(
        ["rev-parse", f"{base['commit']}^{{tree}}"],
        root=root,
    ).decode("ascii").strip()
    if actual_base_tree != base["tree_git_oid"]:
        fail("campaign base commit differs from its sealed tree")
    with tempfile.TemporaryDirectory(
        prefix="kandelo-prefix-source-index-"
    ) as temporary:
        temporary_root = pathlib.Path(temporary)
        object_directory = temporary_root / "objects"
        object_directory.mkdir()
        environment = {
            "GIT_INDEX_FILE": str(temporary_root / "index"),
            "GIT_OBJECT_DIRECTORY": str(object_directory),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(
                run_git(
                    ["rev-parse", "--git-path", "objects"],
                    root=root,
                ).decode("utf-8").strip()
            ),
        }
        run_git(
            ["read-tree", base["tree_git_oid"]],
            root=root,
            environment=environment,
        )
        for file_record in manifest["files"]:
            path = pathlib.PurePosixPath(file_record["path"])
            source = source_root.joinpath(*path.parts)
            object_id = run_git(
                ["hash-object", "-w", "--", str(source)],
                root=root,
                environment=environment,
            ).decode("ascii").strip()
            if object_id != file_record["target"]["blob_git_oid"]:
                fail(f"campaign source {path} has the wrong Git identity")
            run_git(
                [
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    file_record["target"]["mode"],
                    object_id,
                    path.as_posix(),
                ],
                root=root,
                environment=environment,
            )
        target_tree = run_git(
            ["write-tree"],
            root=root,
            environment=environment,
        ).decode("ascii").strip()
    if target_tree != manifest["target_tree_git_oid"]:
        fail("campaign overlay does not reconstruct the sealed target tree")


def verify_source(
    *,
    root: pathlib.Path,
    authority_path: pathlib.Path,
    manifest_path: pathlib.Path,
    require_live_base: bool,
) -> dict[str, Any]:
    root = root.resolve()
    authority, _authority_payload = load_json(
        authority_path,
        "campaign caller authority",
    )
    if not isinstance(authority, dict):
        fail("campaign caller authority must be an object")
    target_source = exact_keys(
        authority.get("target_source"),
        {
            "manifest_path",
            "manifest_sha256",
            "source_root",
            "source_tree_git_oid",
            "target_tree_git_oid",
        },
        "campaign target-source authority",
    )
    manifest, manifest_payload = load_manifest(manifest_path)
    manifest_relative = manifest_path.resolve().relative_to(root).as_posix()
    if manifest_relative != target_source["manifest_path"]:
        fail("campaign authority selects a different source manifest")
    source_relative = relative_path(
        manifest["source_root"],
        "campaign manifest source root",
    )
    source_root = root.joinpath(*source_relative.parts)
    if (
        target_source["source_root"] != source_relative.as_posix()
        or target_source["manifest_sha256"] != digest(manifest_payload)
        or target_source["source_tree_git_oid"]
        != source_tree_oid(source_root)
        or target_source["target_tree_git_oid"]
        != manifest["target_tree_git_oid"]
    ):
        fail("campaign authority differs from the sealed target source")
    expected_sources: set[pathlib.Path] = set()
    for file_record in manifest["files"]:
        path = pathlib.PurePosixPath(file_record["path"])
        source = source_root.joinpath(*path.parts)
        expected_sources.add(source)
        verify_record(
            source,
            file_record["target"],
            f"campaign source {path}",
        )
        if require_live_base:
            live = root.joinpath(*path.parts)
            if file_record["base"] is None:
                if live.exists() or live.is_symlink():
                    fail(f"campaign-only path {path} is already live")
            else:
                verify_record(
                    live,
                    file_record["base"],
                    f"live pre-cutover path {path}",
                )
    actual_sources = {
        entry
        for entry in source_root.rglob("*")
        if entry.is_file() or entry.is_symlink()
    }
    if actual_sources != expected_sources:
        fail("campaign source contains unsealed files")
    verify_target_tree(root, manifest, source_root)
    return {
        "base_commit": manifest["base"]["commit"],
        "files": len(manifest["files"]),
        "manifest_sha256": digest(manifest_payload),
        "source_tree_git_oid": source_tree_oid(source_root),
        "target_tree_git_oid": manifest["target_tree_git_oid"],
    }


def materialize(
    *,
    root: pathlib.Path,
    authority_path: pathlib.Path,
    manifest_path: pathlib.Path,
    output: pathlib.Path,
    require_live_base: bool = True,
) -> dict[str, Any]:
    if type(require_live_base) is not bool:
        fail("require_live_base must be a boolean")
    summary = verify_source(
        root=root,
        authority_path=authority_path,
        manifest_path=manifest_path,
        require_live_base=require_live_base,
    )
    if output.exists() or output.is_symlink():
        fail("materialized output already exists")
    manifest, _payload = load_manifest(manifest_path)
    source_root = root / manifest["source_root"]
    temporary = pathlib.Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.",
            dir=output.parent,
        )
    )
    try:
        with subprocess.Popen(
            [
                "git",
                "archive",
                manifest["base"]["commit"],
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as archive:
            assert archive.stdout is not None
            extract = subprocess.run(
                ["tar", "-x", "-C", str(temporary)],
                stdin=archive.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=120,
            )
            archive.stdout.close()
            archive_stderr = (
                archive.stderr.read() if archive.stderr else b""
            )
            archive_result = archive.wait(timeout=120)
        if archive_result != 0 or extract.returncode != 0:
            fail(
                "failed to materialize campaign base: "
                f"{archive_stderr.decode(errors='replace')}"
                f"{extract.stderr.decode(errors='replace')}"
            )
        for file_record in manifest["files"]:
            path = pathlib.PurePosixPath(file_record["path"])
            source = source_root.joinpath(*path.parts)
            destination = temporary.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
            os.chmod(
                destination,
                0o755
                if file_record["target"]["mode"] == "100755"
                else 0o644,
            )
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    seal = commands.add_parser("seal")
    seal.add_argument("--root", type=pathlib.Path, default=ROOT)
    seal.add_argument("--source-root", type=pathlib.Path, required=True)
    seal.add_argument("--base", required=True)
    seal.add_argument("--target", required=True)
    seal.add_argument("--out", type=pathlib.Path, required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=pathlib.Path, default=ROOT)
    verify.add_argument(
        "--authority",
        type=pathlib.Path,
        default=DEFAULT_AUTHORITY,
    )
    verify.add_argument(
        "--manifest",
        type=pathlib.Path,
        default=DEFAULT_MANIFEST,
    )
    verify.add_argument(
        "--allow-live-target",
        action="store_true",
    )

    materialize_parser = commands.add_parser("materialize")
    materialize_parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=ROOT,
    )
    materialize_parser.add_argument(
        "--authority",
        type=pathlib.Path,
        default=DEFAULT_AUTHORITY,
    )
    materialize_parser.add_argument(
        "--manifest",
        type=pathlib.Path,
        default=DEFAULT_MANIFEST,
    )
    materialize_parser.add_argument(
        "--out",
        type=pathlib.Path,
        required=True,
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        if arguments.command == "seal":
            seal_manifest(
                root=arguments.root,
                source_root=arguments.source_root,
                base=arguments.base,
                target=arguments.target,
                output=arguments.out,
            )
            print("prefix-campaign-source: sealed manifest")
        elif arguments.command == "verify":
            summary = verify_source(
                root=arguments.root,
                authority_path=arguments.authority,
                manifest_path=arguments.manifest,
                require_live_base=not arguments.allow_live_target,
            )
            print(
                "prefix-campaign-source: verified "
                f"{summary['files']} inert target files"
            )
        elif arguments.command == "materialize":
            summary = materialize(
                root=arguments.root,
                authority_path=arguments.authority,
                manifest_path=arguments.manifest,
                output=arguments.out,
            )
            print(
                "prefix-campaign-source: materialized "
                f"{summary['target_tree_git_oid']}"
            )
        else:
            raise AssertionError(arguments.command)
        return 0
    except SourceError as error:
        print(f"prefix-campaign-source: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
