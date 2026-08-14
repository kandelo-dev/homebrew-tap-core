"""Construct and verify deterministic custody for exact Git build sources."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import configparser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile
from typing import Any

from .canonical import CanonicalJsonError, canonical_bytes, canonical_sha256, parse_canonical_bytes
from .git_policy import protected_git_arguments
from .plan import exact_formula_subject


MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200_000
MAX_SUBMODULES = 256
MAX_SUBMODULE_DEPTH = 32
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

MANIFEST_KEYS = frozenset(
    {"schema", "kind", "request_sha256", "subject", "sources", "submodules", "capsule_sha256"}
)
SOURCE_KEYS = frozenset(
    {"role", "repository", "commit", "tree", "bundle", "tree_archive"}
)
SUBMODULE_KEYS = frozenset(
    {
        "id",
        "parent_role",
        "path",
        "declared_url",
        "gitlink_commit",
        "tree",
        "bundle",
        "tree_archive",
    }
)
MEMBER_KEYS = frozenset({"path", "sha256", "bytes"})
SOURCE_ROLES = ("kandelo", "tap")


class CustodyError(ValueError):
    """Raised when exact source custody is incomplete or contradictory."""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CustodyError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CustodyError(f"{field} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise CustodyError(
            f"{field} fields changed: missing={sorted(expected - actual)!r} "
            f"extra={sorted(actual - expected)!r}"
        )


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise CustodyError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CustodyError(f"{field} is not UTF-8") from error
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise CustodyError(f"{field} is outside its string bound")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise CustodyError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise CustodyError(f"{field} is not a full lowercase Git SHA")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_MEMBER_BYTES:
        raise CustodyError(f"{field} is not a bounded positive integer")
    return value


def _relative_path(value: Any, field: str) -> str:
    path = _text(value, field)
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise CustodyError(f"{field} is not a normalized relative path")
    return path


def _source_context(value: Mapping[str, Any], field: str) -> dict[str, str]:
    source = _mapping(value, field)
    _exact_keys(source, frozenset({"repository", "commit", "tree"}), field)
    repository = _text(source["repository"], f"{field} repository", 255)
    if REPOSITORY.fullmatch(repository) is None:
        raise CustodyError(f"{field} repository is not owner/name")
    return {
        "repository": repository,
        "commit": _git_sha(source["commit"], f"{field} commit"),
        "tree": _git_sha(source["tree"], f"{field} tree"),
    }


def _subject(value: Any) -> str:
    subject = _text(value, "custody subject", 512)
    try:
        parsed = json.loads(subject)
        if not isinstance(parsed, dict):
            raise TypeError("subject root is not an object")
        identity = parsed["identity"]
        architecture = parsed["architecture"]
        if set(parsed) != {"kind", "identity", "architecture"} or parsed["kind"] != "formula":
            raise TypeError("subject fields changed")
        if not isinstance(identity, str) or STABLE_ID.fullmatch(identity) is None:
            raise TypeError("subject identity is invalid")
        if subject != exact_formula_subject(identity, architecture):
            raise TypeError("subject is not canonical")
    except (KeyError, TypeError, ValueError) as error:
        raise CustodyError("custody subject is not canonical Formula subject JSON") from error
    return subject


def _member(value: Any, field: str) -> dict[str, Any]:
    member = _mapping(value, field)
    _exact_keys(member, MEMBER_KEYS, field)
    return {
        "path": _relative_path(member["path"], f"{field} path"),
        "sha256": _digest(member["sha256"], f"{field} digest"),
        "bytes": _positive_integer(member["bytes"], f"{field} bytes"),
    }


def _validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _mapping(value, "source custody manifest")
    _exact_keys(manifest, MANIFEST_KEYS, "source custody manifest")
    if manifest["schema"] != 1 or manifest["kind"] != "kandelo-source-custody-manifest":
        raise CustodyError("source custody manifest protocol is unsupported")
    request_sha256 = _digest(manifest["request_sha256"], "custody request")
    subject = _subject(manifest["subject"])

    sources: list[dict[str, Any]] = []
    for index, candidate in enumerate(_sequence(manifest["sources"], "custody sources")):
        source = _mapping(candidate, f"custody source {index}")
        _exact_keys(source, SOURCE_KEYS, f"custody source {index}")
        role = _text(source["role"], f"custody source {index} role", 32)
        if index >= len(SOURCE_ROLES) or role != SOURCE_ROLES[index]:
            raise CustodyError("custody sources must be exactly kandelo then tap")
        context = _source_context(
            {
                "repository": source["repository"],
                "commit": source["commit"],
                "tree": source["tree"],
            },
            f"custody source {role}",
        )
        sources.append(
            {
                "role": role,
                **context,
                "bundle": _member(source["bundle"], f"custody source {role} bundle"),
                "tree_archive": _member(
                    source["tree_archive"], f"custody source {role} tree archive"
                ),
            }
        )
        if sources[-1]["bundle"]["path"] != f"{role}.bundle":
            raise CustodyError(f"custody source {role} bundle path is not canonical")
        if sources[-1]["tree_archive"]["path"] != f"{role}-tree.tar":
            raise CustodyError(
                f"custody source {role} tree archive path is not canonical"
            )
    if len(sources) != len(SOURCE_ROLES):
        raise CustodyError("custody sources must be exactly kandelo and tap")

    submodules: list[dict[str, Any]] = []
    previous: tuple[str, str] | None = None
    for index, candidate in enumerate(_sequence(manifest["submodules"], "custody submodules")):
        if index >= MAX_SUBMODULES:
            raise CustodyError("source custody contains too many submodules")
        submodule = _mapping(candidate, f"custody submodule {index}")
        _exact_keys(submodule, SUBMODULE_KEYS, f"custody submodule {index}")
        identifier = _text(submodule["id"], f"custody submodule {index} id", 128)
        if STABLE_ID.fullmatch(identifier) is None:
            raise CustodyError(f"custody submodule {index} id is invalid")
        parent_role = _text(
            submodule["parent_role"], f"custody submodule {index} parent role", 32
        )
        if parent_role not in SOURCE_ROLES:
            raise CustodyError(f"custody submodule {index} parent role is unsupported")
        path = _relative_path(submodule["path"], f"custody submodule {index} path")
        if ".git" in path.split("/"):
            raise CustodyError(f"custody submodule {index} path contains .git")
        ordering = (parent_role, path)
        if previous is not None and ordering <= previous:
            raise CustodyError("custody submodules must be sorted and duplicate-free")
        previous = ordering
        expected_id = _submodule_id(parent_role, path)
        if identifier != expected_id:
            raise CustodyError(f"custody submodule {index} id does not match its path")
        submodules.append(
            {
                "id": identifier,
                "parent_role": parent_role,
                "path": path,
                "declared_url": _text(
                    submodule["declared_url"], f"custody submodule {index} declared URL", 8192
                ),
                "gitlink_commit": _git_sha(
                    submodule["gitlink_commit"], f"custody submodule {index} gitlink"
                ),
                "tree": _git_sha(submodule["tree"], f"custody submodule {index} tree"),
                "bundle": _member(
                    submodule["bundle"], f"custody submodule {index} bundle"
                ),
                "tree_archive": _member(
                    submodule["tree_archive"], f"custody submodule {index} tree archive"
                ),
            }
        )
        if submodules[-1]["bundle"]["path"] != f"submodules/{identifier}.bundle":
            raise CustodyError(f"custody submodule {index} bundle path is not canonical")
        if (
            submodules[-1]["tree_archive"]["path"]
            != f"submodules/{identifier}-tree.tar"
        ):
            raise CustodyError(
                f"custody submodule {index} tree archive path is not canonical"
            )

    member_paths = [
        member["path"]
        for owner in (*sources, *submodules)
        for member in (owner["bundle"], owner["tree_archive"])
    ]
    if len(member_paths) != len(set(member_paths)) or "manifest.json" in member_paths:
        raise CustodyError("source custody member paths must be unique and canonical")

    normalized = {
        "schema": 1,
        "kind": "kandelo-source-custody-manifest",
        "request_sha256": request_sha256,
        "subject": subject,
        "sources": sources,
        "submodules": submodules,
        "capsule_sha256": _digest(manifest["capsule_sha256"], "source capsule"),
    }
    if normalized["capsule_sha256"] != source_capsule_digest(normalized):
        raise CustodyError("source capsule digest does not match the custody members")
    return normalized


def load_source_custody_manifest(body: bytes) -> dict[str, Any]:
    try:
        parsed = parse_canonical_bytes(body, maximum_bytes=MAX_MANIFEST_BYTES)
    except CanonicalJsonError as error:
        raise CustodyError(f"source custody manifest is not canonical JSON: {error}") from error
    return _validate_manifest(_plain(parsed))


def source_capsule_digest(manifest: Mapping[str, Any]) -> str:
    """Return content identity without request/run provenance."""

    return canonical_sha256(
        {
            "schema": 1,
            "kind": "kandelo-source-custody-capsule",
            "sources": _plain(manifest.get("sources")),
            "submodules": _plain(manifest.get("submodules")),
        }
    )


def _git_environment() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _git_arguments(root: Path, *arguments: str) -> list[str]:
    return protected_git_arguments(root, *arguments, file_protocol="always")


def _git_bytes(root: Path, *arguments: str, field: str) -> bytes:
    try:
        result = subprocess.run(
            _git_arguments(root, *arguments),
            check=True,
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError):
            detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise CustodyError(f"cannot inspect {field}: {detail or error}") from error
    return result.stdout


def _git_text(root: Path, *arguments: str, field: str) -> str:
    try:
        return _git_bytes(root, *arguments, field=field).decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise CustodyError(f"{field} is not UTF-8") from error


def _ensure_exact_checkout(root: Path, expected: Mapping[str, str], field: str) -> None:
    if not root.is_dir() or root.is_symlink():
        raise CustodyError(f"{field} checkout is not a real directory")
    replacements = _git_text(
        root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace",
        field=f"{field} replacement refs",
    )
    if replacements:
        raise CustodyError(f"{field} checkout contains replacement refs")
    head = _git_text(root, "rev-parse", "--verify", "HEAD^{commit}", field=f"{field} HEAD")
    tree = _git_text(root, "rev-parse", "--verify", "HEAD^{tree}", field=f"{field} tree")
    if head != expected["commit"] or tree != expected["tree"]:
        raise CustodyError(f"{field} checkout is not the exact expected commit/tree")


def _tree_object_ids(root: Path, commit: str, field: str) -> list[str]:
    tree = _git_text(
        root, "rev-parse", "--verify", f"{commit}^{{tree}}", field=f"{field} tree"
    )
    body = _git_text(
        root,
        "rev-list",
        "--objects",
        "--no-object-names",
        tree,
        field=f"{field} tree objects",
    )
    object_ids = {commit, *body.splitlines()}
    for object_id in object_ids:
        _git_sha(object_id, f"{field} object")
    return sorted(object_ids)


def _create_bundle(root: Path, destination: Path, commit: str, field: str) -> None:
    object_ids = _tree_object_ids(root, commit, field)
    try:
        with destination.open("wb") as stream:
            stream.write(b"# v2 git bundle\n")
            stream.write(f"{commit} HEAD\n\n".encode("ascii"))
            stream.flush()
            result = subprocess.run(
                _git_arguments(
                    root,
                    "-c",
                    "pack.threads=1",
                    "-c",
                    "pack.window=0",
                    "-c",
                    "pack.depth=0",
                    "pack-objects",
                    "--stdout",
                ),
                check=False,
                env=_git_environment(),
                input=("\n".join(object_ids) + "\n").encode("ascii"),
                stdout=stream,
                stderr=subprocess.PIPE,
            )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise CustodyError(f"cannot create {field} bundle: {detail}")
    except OSError as error:
        raise CustodyError(f"cannot create {field} bundle: {error}") from error


def _parse_tree(root: Path, commit: str, field: str) -> list[tuple[str, str, str, str]]:
    body = _git_bytes(root, "ls-tree", "-rz", "-r", commit, field=f"{field} tree")
    entries: list[tuple[str, str, str, str]] = []
    for raw in body.split(b"\0"):
        if not raw:
            continue
        header, separator, raw_path = raw.partition(b"\t")
        if not separator:
            raise CustodyError(f"{field} tree entry is malformed")
        try:
            mode, object_type, object_id = header.decode("ascii").split(" ")
            path = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise CustodyError(f"{field} tree entry is malformed") from error
        _relative_path(path, f"{field} tree path")
        if object_type not in {"blob", "commit"}:
            raise CustodyError(f"{field} tree entry has unsupported object type")
        if mode not in {"100644", "100755", "120000", "160000"}:
            raise CustodyError(f"{field} tree entry has unsupported mode {mode}")
        if (mode == "160000") != (object_type == "commit"):
            raise CustodyError(f"{field} tree entry has contradictory Git type")
        _git_sha(object_id, f"{field} tree object")
        entries.append((path, mode, object_type, object_id))
    return entries


def _parent_directories(paths: Sequence[str]) -> set[str]:
    directories: set[str] = set()
    for path in paths:
        parent = PurePosixPath(path).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _safe_link_target(path: str, target: str, field: str) -> None:
    if not target or "\0" in target or target.startswith("/") or "\\" in target:
        raise CustodyError(f"{field} has an unsafe symbolic-link target")
    combined = PurePosixPath(path).parent.joinpath(target)
    depth = 0
    for part in combined.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            depth -= 1
        else:
            depth += 1
        if depth < 0:
            raise CustodyError(f"{field} symbolic link escapes the tree")


def _tar_info(name: str, *, mode: int, entry_type: bytes, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.type = entry_type
    info.size = size
    return info


def _write_tree_archive(root: Path, commit: str, destination: Path, field: str) -> None:
    entries = _parse_tree(root, commit, field)
    directories = _parent_directories([path for path, _, _, _ in entries])
    directories.update(path for path, mode, _, _ in entries if mode == "160000")
    by_path = {
        path: (mode, object_type, object_id)
        for path, mode, object_type, object_id in entries
    }
    ordered = sorted(set(by_path) | directories, key=lambda value: value.encode("utf-8"))

    try:
        process = subprocess.Popen(
            _git_arguments(root, "cat-file", "--batch"),
            env=_git_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise CustodyError(f"cannot read {field} objects: {error}") from error
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        with tarfile.open(destination, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for path in ordered:
                if path in directories:
                    archive.addfile(_tar_info(path, mode=0o755, entry_type=tarfile.DIRTYPE))
                    if path not in by_path or by_path[path][0] == "160000":
                        continue
                mode, object_type, object_id = by_path[path]
                if object_type != "blob":
                    continue
                process.stdin.write(object_id.encode("ascii") + b"\n")
                process.stdin.flush()
                response = process.stdout.readline()
                parts = response.rstrip(b"\n").split(b" ")
                if len(parts) != 3 or parts[0] != object_id.encode("ascii") or parts[1] != b"blob":
                    raise CustodyError(f"cannot read exact blob for {field} path {path!r}")
                try:
                    size = int(parts[2], 10)
                except ValueError as error:
                    raise CustodyError(
                        f"cannot read exact blob size for {field} path {path!r}"
                    ) from error
                body = process.stdout.read(size)
                trailer = process.stdout.read(1)
                if len(body) != size or trailer != b"\n":
                    raise CustodyError(f"exact blob changed while reading {field} path {path!r}")
                if mode == "120000":
                    try:
                        target = body.decode("utf-8", errors="strict")
                    except UnicodeDecodeError as error:
                        raise CustodyError(f"{field} symbolic-link target is not UTF-8") from error
                    _safe_link_target(path, target, f"{field} path {path!r}")
                    info = _tar_info(path, mode=0o777, entry_type=tarfile.SYMTYPE)
                    info.linkname = target
                    archive.addfile(info)
                else:
                    archive.addfile(
                        _tar_info(
                            path,
                            mode=0o755 if mode == "100755" else 0o644,
                            entry_type=tarfile.REGTYPE,
                            size=size,
                        ),
                        io.BytesIO(body),
                    )
        process.stdin.close()
        return_code = process.wait()
        if return_code != 0:
            detail = process.stderr.read().decode("utf-8", errors="replace").strip()
            raise CustodyError(f"cannot read {field} objects: {detail}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


def _artifact(path: Path, relative: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CustodyError(f"source custody member {relative!r} is not a regular file")
        if not 1 <= metadata.st_size <= MAX_MEMBER_BYTES:
            raise CustodyError(f"source custody member {relative!r} is outside its byte bound")
        body = path.read_bytes()
    except OSError as error:
        raise CustodyError(f"cannot inspect source custody member {relative!r}: {error}") from error
    if len(body) != metadata.st_size:
        raise CustodyError(f"source custody member {relative!r} changed while reading")
    return {"path": relative, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def _submodule_id(role: str, path: str) -> str:
    digest = hashlib.sha256(f"{role}\0{path}".encode("utf-8")).hexdigest()[:24]
    return f"submodule-{digest}"


def _gitmodules(root: Path, commit: str, field: str) -> dict[str, str]:
    try:
        body = _git_bytes(root, "show", f"{commit}:.gitmodules", field=f"{field} .gitmodules")
    except CustodyError:
        return {}
    try:
        text = body.decode("utf-8", errors="strict")
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read_string(text)
    except (UnicodeDecodeError, configparser.Error) as error:
        raise CustodyError(f"{field} .gitmodules is malformed") from error
    result: dict[str, str] = {}
    for section in parser.sections():
        if not section.startswith('submodule "') or not section.endswith('"'):
            raise CustodyError(f"{field} .gitmodules contains an unsupported section")
        if not parser.has_option(section, "path") or not parser.has_option(section, "url"):
            raise CustodyError(f"{field} .gitmodules entry is incomplete")
        path = _relative_path(parser.get(section, "path"), f"{field} submodule path")
        if path in result:
            raise CustodyError(f"{field} .gitmodules repeats submodule path {path!r}")
        result[path] = _text(parser.get(section, "url"), f"{field} submodule URL", 8192)
    return result


def _direct_gitlinks(root: Path, commit: str, field: str) -> list[tuple[str, str]]:
    return [
        (path, object_id)
        for path, mode, object_type, object_id in _parse_tree(root, commit, field)
        if mode == "160000" and object_type == "commit"
    ]


def _collect_submodules(
    *,
    role: str,
    root: Path,
    commit: str,
    output: Path,
    logical_prefix: str = "",
    depth: int = 0,
) -> list[dict[str, Any]]:
    if depth > MAX_SUBMODULE_DEPTH:
        raise CustodyError("source custody exceeds its submodule depth bound")
    gitlinks = _direct_gitlinks(root, commit, f"{role} source")
    declared = _gitmodules(root, commit, f"{role} source") if gitlinks else {}
    if set(declared) != {path for path, _ in gitlinks}:
        raise CustodyError(f"{role} source .gitmodules does not exactly describe its gitlinks")
    records: list[dict[str, Any]] = []
    for direct_path, gitlink_commit in sorted(gitlinks, key=lambda item: item[0].encode("utf-8")):
        logical_path = f"{logical_prefix}/{direct_path}" if logical_prefix else direct_path
        identifier = _submodule_id(role, logical_path)
        checkout = root.joinpath(*direct_path.split("/"))
        tree = _git_text(
            checkout,
            "rev-parse",
            "--verify",
            "HEAD^{tree}",
            field=f"{role} submodule {logical_path} tree",
        )
        expected = {"commit": gitlink_commit, "tree": tree}
        _ensure_exact_checkout(checkout, expected, f"{role} submodule {logical_path}")
        bundle_relative = f"submodules/{identifier}.bundle"
        archive_relative = f"submodules/{identifier}-tree.tar"
        bundle_path = output / bundle_relative
        archive_path = output / archive_relative
        _create_bundle(
            checkout,
            bundle_path,
            gitlink_commit,
            f"{role} submodule {logical_path}",
        )
        _write_tree_archive(
            checkout, gitlink_commit, archive_path, f"{role} submodule {logical_path}"
        )
        records.append(
            {
                "id": identifier,
                "parent_role": role,
                "path": logical_path,
                "declared_url": declared[direct_path],
                "gitlink_commit": gitlink_commit,
                "tree": tree,
                "bundle": _artifact(bundle_path, bundle_relative),
                "tree_archive": _artifact(archive_path, archive_relative),
            }
        )
        records.extend(
            _collect_submodules(
                role=role,
                root=checkout,
                commit=gitlink_commit,
                output=output,
                logical_prefix=logical_path,
                depth=depth + 1,
            )
        )
        if len(records) > MAX_SUBMODULES:
            raise CustodyError("source custody contains too many submodules")
    return records


def create_source_custody(
    *,
    kandelo_root: Path,
    tap_root: Path,
    kandelo_source: Mapping[str, Any],
    tap_source: Mapping[str, Any],
    request_sha256: str,
    subject: str,
    output: Path,
) -> dict[str, Any]:
    """Create path-independent bundles, tree archives, and their exact manifest."""

    contexts = {
        "kandelo": _source_context(kandelo_source, "expected Kandelo source"),
        "tap": _source_context(tap_source, "expected tap source"),
    }
    _digest(request_sha256, "custody request")
    _subject(subject)
    roots = {"kandelo": Path(kandelo_root), "tap": Path(tap_root)}
    try:
        if output.is_symlink():
            raise CustodyError("source custody output cannot be a symbolic link")
        if output.exists():
            if not output.is_dir() or any(output.iterdir()):
                raise CustodyError("source custody output must be absent or empty")
        output.mkdir(parents=True, exist_ok=True)
        (output / "submodules").mkdir()
    except OSError as error:
        raise CustodyError(f"cannot prepare source custody output: {error}") from error

    sources: list[dict[str, Any]] = []
    submodules: list[dict[str, Any]] = []
    for role in SOURCE_ROLES:
        root = roots[role]
        context = contexts[role]
        _ensure_exact_checkout(root, context, f"{role} source")
        bundle_relative = f"{role}.bundle"
        archive_relative = f"{role}-tree.tar"
        _create_bundle(root, output / bundle_relative, context["commit"], f"{role} source")
        _write_tree_archive(root, context["commit"], output / archive_relative, f"{role} source")
        sources.append(
            {
                "role": role,
                **context,
                "bundle": _artifact(output / bundle_relative, bundle_relative),
                "tree_archive": _artifact(output / archive_relative, archive_relative),
            }
        )
        submodules.extend(
            _collect_submodules(
                role=role,
                root=root,
                commit=context["commit"],
                output=output,
            )
        )
    submodules.sort(key=lambda item: (item["parent_role"], item["path"]))
    manifest: dict[str, Any] = {
        "schema": 1,
        "kind": "kandelo-source-custody-manifest",
        "request_sha256": request_sha256,
        "subject": subject,
        "sources": sources,
        "submodules": submodules,
        "capsule_sha256": "0" * 64,
    }
    manifest["capsule_sha256"] = source_capsule_digest(manifest)
    body = canonical_bytes(manifest)
    (output / "manifest.json").write_bytes(body)
    return load_source_custody_manifest(body)


def _scan_members(root: Path) -> dict[str, Path]:
    try:
        metadata = root.lstat()
    except OSError as error:
        raise CustodyError(f"cannot inspect source custody root: {error}") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise CustodyError("source custody root must be a real directory")
    files: dict[str, Path] = {}
    for directory, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in list(directories):
            candidate = directory_path / name
            candidate_metadata = candidate.lstat()
            if (
                not stat.S_ISDIR(candidate_metadata.st_mode)
                or stat.S_ISLNK(candidate_metadata.st_mode)
            ):
                raise CustodyError("source custody cannot contain linked or special directories")
            relative_directory = candidate.relative_to(root).as_posix()
            if relative_directory != "submodules":
                raise CustodyError(
                    f"source custody contains unexpected directory {relative_directory!r}"
                )
        for name in filenames:
            candidate = directory_path / name
            relative = candidate.relative_to(root).as_posix()
            _relative_path(relative, "source custody member path")
            candidate_metadata = candidate.lstat()
            if (
                not stat.S_ISREG(candidate_metadata.st_mode)
                or stat.S_ISLNK(candidate_metadata.st_mode)
                or candidate_metadata.st_nlink != 1
            ):
                raise CustodyError(f"source custody member {relative!r} is linked or special")
            if relative in files:
                raise CustodyError(f"source custody repeats member {relative!r}")
            files[relative] = candidate
    return files


def _list_safe_archive(path: Path, field: str) -> None:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            seen: set[str] = set()
            for index, member in enumerate(archive):
                if index >= MAX_ARCHIVE_MEMBERS:
                    raise CustodyError(f"{field} contains too many entries")
                name = _relative_path(member.name.rstrip("/"), f"{field} entry")
                if name in seen:
                    raise CustodyError(f"{field} repeats archive entry {name!r}")
                seen.add(name)
                if not (member.isfile() or member.isdir() or member.issym()):
                    raise CustodyError(f"{field} contains a linked or special entry")
                if member.uid != 0 or member.gid != 0 or member.mtime != 0:
                    raise CustodyError(f"{field} contains non-normalized metadata")
                if member.isfile() and member.mode not in {0o644, 0o755}:
                    raise CustodyError(f"{field} contains a non-normalized file mode")
                if member.isdir() and member.mode != 0o755:
                    raise CustodyError(f"{field} contains a non-normalized directory mode")
                if member.issym():
                    if member.mode != 0o777:
                        raise CustodyError(f"{field} contains a non-normalized symlink mode")
                    _safe_link_target(name, member.linkname, f"{field} entry {name!r}")
    except (OSError, tarfile.TarError) as error:
        raise CustodyError(f"cannot safely list {field}: {error}") from error


def _bundle_header(path: Path, field: str) -> tuple[str, str]:
    try:
        with path.open("rb") as stream:
            signature = stream.readline(256)
            if signature != b"# v2 git bundle\n":
                raise CustodyError(f"{field} is not a supported Git bundle")
            advertised: list[tuple[str, str]] = []
            for _ in range(4096):
                line = stream.readline(8192)
                if not line:
                    raise CustodyError(f"{field} has an incomplete header")
                if line == b"\n":
                    break
                if line.startswith(b"-") or line.startswith(b"@"):  # prerequisites/capabilities
                    raise CustodyError(f"{field} has unexpected bundle metadata")
                commit, separator, reference = line.rstrip(b"\n").partition(b" ")
                try:
                    commit_text = commit.decode("ascii")
                    reference_text = reference.decode("ascii")
                except UnicodeDecodeError as error:
                    raise CustodyError(f"{field} header is not ASCII") from error
                if not separator:
                    raise CustodyError(f"{field} advertised ref is malformed")
                advertised.append((commit_text, reference_text))
            else:
                raise CustodyError(f"{field} header is too large")
    except OSError as error:
        raise CustodyError(f"cannot read {field}: {error}") from error
    if len(advertised) != 1 or advertised[0][1] != "HEAD":
        raise CustodyError(f"{field} must advertise exactly one neutral HEAD")
    if advertised[0][1].startswith("refs/replace/"):
        raise CustodyError(f"{field} advertises a replacement ref")
    return advertised[0]


def _bundle_object_database(
    bundle: Path,
    archive: Path,
    *,
    expected_commit: str,
    expected_tree: str,
    field: str,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    advertised_commit, _ = _bundle_header(bundle, f"{field} bundle")
    if advertised_commit != expected_commit:
        raise CustodyError(f"{field} bundle advertises the wrong commit")
    _list_safe_archive(archive, f"{field} tree archive")
    temporary = tempfile.TemporaryDirectory()
    bare = Path(temporary.name) / "objects.git"
    try:
        bare.mkdir()
        _git_bytes(bare, "init", "--bare", field=f"{field} isolated repository")
        _git_bytes(bare, "bundle", "verify", str(bundle), field=f"{field} bundle")
        (bare / "shallow").write_text(expected_commit + "\n", encoding="ascii")
        _git_bytes(
            bare,
            "fetch",
            "--no-tags",
            "--force",
            str(bundle),
            "HEAD:refs/custody/source",
            field=f"{field} bundle objects",
        )
        commit = _git_text(
            bare,
            "rev-parse",
            "--verify",
            f"{expected_commit}^{{commit}}",
            field=f"{field} commit object",
        )
        tree = _git_text(
            bare,
            "rev-parse",
            "--verify",
            f"{expected_commit}^{{tree}}",
            field=f"{field} tree object",
        )
        if commit != expected_commit or tree != expected_tree:
            raise CustodyError(f"{field} bundle has the wrong commit/tree relationship")
        _git_bytes(
            bare,
            "fsck",
            "--full",
            "--strict",
            "--no-reflogs",
            field=f"{field} bundle object graph",
        )
        actual_objects = set(
            _git_text(
                bare,
                "cat-file",
                "--batch-check=%(objectname)",
                "--batch-all-objects",
                field=f"{field} bundle object inventory",
            ).splitlines()
        )
        expected_objects = set(_tree_object_ids(bare, expected_commit, field))
        if actual_objects != expected_objects:
            raise CustodyError(f"{field} bundle contains missing or extra Git objects")
        rebuilt = Path(temporary.name) / "rebuilt.tar"
        _write_tree_archive(bare, expected_commit, rebuilt, field)
        if rebuilt.read_bytes() != archive.read_bytes():
            raise CustodyError(f"{field} tree archive does not match the exact Git tree")
        return temporary, bare
    except Exception:
        temporary.cleanup()
        raise


def _read_member(path: Path, expected: Mapping[str, Any], field: str) -> None:
    actual = _artifact(path, expected["path"])
    if actual != expected:
        raise CustodyError(f"{field} digest or size does not match its manifest")


def _direct_parent(
    submodule: Mapping[str, Any], prior: Sequence[Mapping[str, Any]]
) -> tuple[str | None, str]:
    role = submodule["parent_role"]
    path = submodule["path"]
    candidates = [
        candidate
        for candidate in prior
        if candidate["parent_role"] == role and path.startswith(candidate["path"] + "/")
    ]
    if not candidates:
        return None, path
    parent = max(candidates, key=lambda candidate: len(candidate["path"]))
    return parent["path"], path[len(parent["path"]) + 1 :]


def _verify_gitlink(
    parent_database: Path,
    parent_commit: str,
    direct_path: str,
    expected_commit: str,
    declared_url: str,
    field: str,
) -> None:
    body = _git_bytes(
        parent_database,
        "ls-tree",
        "-z",
        parent_commit,
        "--",
        direct_path,
        field=f"{field} gitlink",
    )
    records = [entry for entry in body.split(b"\0") if entry]
    if len(records) != 1:
        raise CustodyError(f"{field} is not an exact parent gitlink")
    header, separator, raw_path = records[0].partition(b"\t")
    expected_header = f"160000 commit {expected_commit}".encode("ascii")
    if (
        not separator
        or header != expected_header
        or raw_path.decode("utf-8", errors="strict") != direct_path
    ):
        raise CustodyError(f"{field} has the wrong parent gitlink")
    modules = _gitmodules(parent_database, parent_commit, field)
    if modules.get(direct_path) != declared_url:
        raise CustodyError(f"{field} has the wrong declared submodule URL")


def validate_source_custody(
    *,
    root: Path,
    expected_request_sha256: str,
    expected_subject: str,
    expected_kandelo_source: Mapping[str, Any],
    expected_tap_source: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate source bytes as inert data in isolated Git object stores."""

    expected_sources = {
        "kandelo": _source_context(expected_kandelo_source, "expected Kandelo source"),
        "tap": _source_context(expected_tap_source, "expected tap source"),
    }
    _digest(expected_request_sha256, "expected custody request")
    _subject(expected_subject)
    files = _scan_members(root)
    if "manifest.json" not in files:
        raise CustodyError("source custody manifest is missing")
    manifest = load_source_custody_manifest(files["manifest.json"].read_bytes())
    if manifest["request_sha256"] != expected_request_sha256:
        raise CustodyError("source custody refers to a different request")
    if manifest["subject"] != expected_subject:
        raise CustodyError("source custody refers to a different Formula subject")

    declared_paths = {"manifest.json"}
    for source in manifest["sources"]:
        context = {key: source[key] for key in ("repository", "commit", "tree")}
        if context != expected_sources[source["role"]]:
            raise CustodyError(f"source custody refers to a different {source['role']} plan")
        declared_paths.update((source["bundle"]["path"], source["tree_archive"]["path"]))
    for submodule in manifest["submodules"]:
        declared_paths.update((submodule["bundle"]["path"], submodule["tree_archive"]["path"]))
    if set(files) != declared_paths:
        raise CustodyError(
            f"source custody members changed: missing={sorted(declared_paths - set(files))!r} "
            f"extra={sorted(set(files) - declared_paths)!r}"
        )

    databases: dict[tuple[str, str | None], tuple[tempfile.TemporaryDirectory[str], Path, str]] = {}
    try:
        for source in manifest["sources"]:
            bundle_member = source["bundle"]
            archive_member = source["tree_archive"]
            _read_member(root / bundle_member["path"], bundle_member, f"{source['role']} bundle")
            _read_member(
                root / archive_member["path"], archive_member, f"{source['role']} tree archive"
            )
            temporary, database = _bundle_object_database(
                root / bundle_member["path"],
                root / archive_member["path"],
                expected_commit=source["commit"],
                expected_tree=source["tree"],
                field=f"{source['role']} source",
            )
            databases[(source["role"], None)] = (temporary, database, source["commit"])

        prior: list[Mapping[str, Any]] = []
        for submodule in manifest["submodules"]:
            bundle_member = submodule["bundle"]
            archive_member = submodule["tree_archive"]
            _read_member(
                root / bundle_member["path"],
                bundle_member,
                f"submodule {submodule['path']} bundle",
            )
            _read_member(
                root / archive_member["path"],
                archive_member,
                f"submodule {submodule['path']} tree archive",
            )
            parent_path, direct_path = _direct_parent(submodule, prior)
            parent_key = (submodule["parent_role"], parent_path)
            if parent_key not in databases:
                raise CustodyError(f"submodule {submodule['path']!r} has no preserved parent")
            _, parent_database, parent_commit = databases[parent_key]
            _verify_gitlink(
                parent_database,
                parent_commit,
                direct_path,
                submodule["gitlink_commit"],
                submodule["declared_url"],
                f"submodule {submodule['path']}",
            )
            temporary, database = _bundle_object_database(
                root / bundle_member["path"],
                root / archive_member["path"],
                expected_commit=submodule["gitlink_commit"],
                expected_tree=submodule["tree"],
                field=f"submodule {submodule['path']}",
            )
            databases[(submodule["parent_role"], submodule["path"])] = (
                temporary,
                database,
                submodule["gitlink_commit"],
            )
            prior.append(submodule)
    finally:
        for temporary, _, _ in databases.values():
            temporary.cleanup()
    return manifest


def build_miniature_source_custody_manifest_fixture() -> dict[str, Any]:
    """Return the checked generic N -> N+1 source-custody protocol fixture."""

    manifest: dict[str, Any] = {
        "schema": 1,
        "kind": "kandelo-source-custody-manifest",
        "request_sha256": "1" * 64,
        "subject": exact_formula_subject("mini-tool", "wasm32"),
        "sources": [
            {
                "role": "kandelo",
                "repository": "example/kandelo",
                "commit": "2" * 40,
                "tree": "3" * 40,
                "bundle": {"path": "kandelo.bundle", "sha256": "4" * 64, "bytes": 101},
                "tree_archive": {
                    "path": "kandelo-tree.tar",
                    "sha256": "5" * 64,
                    "bytes": 10240,
                },
            },
            {
                "role": "tap",
                "repository": "example/homebrew-tap-core",
                "commit": "6" * 40,
                "tree": "7" * 40,
                "bundle": {"path": "tap.bundle", "sha256": "8" * 64, "bytes": 103},
                "tree_archive": {
                    "path": "tap-tree.tar",
                    "sha256": "9" * 64,
                    "bytes": 10240,
                },
            },
        ],
        "submodules": [
            {
                "id": _submodule_id("kandelo", "deps/mini"),
                "parent_role": "kandelo",
                "path": "deps/mini",
                "declared_url": "https://example.test/mini.git",
                "gitlink_commit": "a" * 40,
                "tree": "b" * 40,
                "bundle": {
                    "path": f"submodules/{_submodule_id('kandelo', 'deps/mini')}.bundle",
                    "sha256": "c" * 64,
                    "bytes": 104,
                },
                "tree_archive": {
                    "path": f"submodules/{_submodule_id('kandelo', 'deps/mini')}-tree.tar",
                    "sha256": "d" * 64,
                    "bytes": 10240,
                },
            }
        ],
        "capsule_sha256": "0" * 64,
    }
    manifest["capsule_sha256"] = source_capsule_digest(manifest)
    return manifest
