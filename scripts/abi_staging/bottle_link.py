"""Derive transitional Homebrew link metadata from exact bottle bytes.

The link manifest is a mechanical projection for legacy Homebrew consumers. It
is never accepted from candidate output: protected promotion code inventories
the immutable bottle layer and combines that inventory with the exact captured
Kandelo guest-layout contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import io
import json
import posixpath
import re
import tarfile
from typing import Any


MAX_COMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200_000
MAX_PATH_BYTES = 4096
MAX_LINK_DEPTH = 256
MAX_LAYOUT_BYTES = 64 * 1024
MAX_LINK_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_INVENTORY_FILES = 200_000
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,127}$")
PKG_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+,-]{0,255}$")


class BottleLinkError(ValueError):
    """Raised when bottle-derived link metadata is unsafe or contradictory."""


def link_manifest_bytes(value: Any) -> bytes:
    """Encode the exact readable link-manifest bytes used by tap metadata."""

    manifest = _mapping(value, "link manifest encoding")
    try:
        body = (
            json.dumps(dict(manifest), indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise BottleLinkError(f"link manifest is not encodable JSON: {error}") from error
    if not 1 <= len(body) <= MAX_LINK_MANIFEST_BYTES:
        raise BottleLinkError("link manifest bytes are outside their bound")
    return body


@dataclass(frozen=True)
class _ArchiveEntry:
    path: str
    kind: str
    mode: int
    size: int
    target: str | None = None


def _text(value: Any, field: str, maximum: int = MAX_PATH_BYTES) -> str:
    if not isinstance(value, str):
        raise BottleLinkError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise BottleLinkError(f"{field} is not UTF-8") from error
    if (
        not encoded
        or len(encoded) > maximum
        or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise BottleLinkError(f"{field} is outside its string bound")
    return value


def _stable_id(value: Any, field: str) -> str:
    checked = _text(value, field, 128)
    if STABLE_ID.fullmatch(checked) is None:
        raise BottleLinkError(f"{field} is not a stable identity")
    return checked


def _version(value: Any, field: str) -> str:
    checked = _text(value, field, 256)
    if PKG_VERSION.fullmatch(checked) is None:
        raise BottleLinkError(f"{field} is not a package version")
    return checked


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise BottleLinkError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _positive(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_COMPRESSED_BYTES
    ):
        raise BottleLinkError(f"{field} is not a bounded positive integer")
    return value


def _relative_path(value: Any, field: str) -> str:
    checked = _text(value, field)
    if (
        checked.startswith("/")
        or "\\" in checked
        or any(part in {"", ".", ".."} for part in checked.split("/"))
    ):
        raise BottleLinkError(f"{field} is not a safe relative POSIX path")
    return checked


def _absolute_path(value: Any, field: str) -> str:
    checked = _text(value, field)
    if (
        not checked.startswith("/")
        or checked.startswith("//")
        or checked == "/"
        or "\\" in checked
        or checked.endswith("/")
        or posixpath.normpath(checked) != checked
    ):
        raise BottleLinkError(f"{field} is not a normalized guest absolute path")
    return checked


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BottleLinkError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BottleLinkError(f"{field} must be an array")
    return value


def _exact(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise BottleLinkError(
            f"{field} fields changed: missing={sorted(expected - actual)!r} "
            f"extra={sorted(actual - expected)!r}"
        )


def _normalize_member_path(value: str, field: str) -> str:
    checked = _text(value, field)
    while checked.startswith("./"):
        checked = checked[2:]
    checked = checked.rstrip("/")
    return _relative_path(checked, field)


def _under_payload(path: str, payload_root: str, field: str) -> str:
    if path != payload_root and not path.startswith(payload_root + "/"):
        raise BottleLinkError(f"{field} escapes bottle payload root")
    return path


def _link_target(
    value: str, source: str, payload_root: str, *, hardlink: bool
) -> str:
    checked = _text(value, f"archive link {source!r}")
    if checked.startswith("/") or "\\" in checked:
        raise BottleLinkError(f"archive link {source!r} has an unsafe target")
    if hardlink:
        while checked.startswith("./"):
            checked = checked[2:]
        resolved = posixpath.normpath(checked)
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), checked))
    if resolved in {"", ".", ".."} or resolved.startswith("../") or resolved.startswith("/"):
        raise BottleLinkError(f"archive link {source!r} escapes the archive")
    _relative_path(resolved, f"archive link {source!r} target")
    return _under_payload(resolved, payload_root, f"archive link {source!r}")


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "directory"
    if member.isfile():
        return "regular"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    raise BottleLinkError(f"archive entry {member.name!r} has an unsupported type")


def inspect_bottle_link_inventory(
    archive_bytes: bytes, *, formula: str, version: str
) -> dict[str, object]:
    """Return the bounded file/link inventory needed by the legacy manifest."""

    name = _stable_id(formula, "bottle Formula")
    pkg_version = _version(version, "bottle version")
    if not isinstance(archive_bytes, bytes) or not 1 <= len(archive_bytes) <= MAX_COMPRESSED_BYTES:
        raise BottleLinkError("bottle archive bytes are outside their bound")
    payload_root = f"{name}/{pkg_version}"
    entries: dict[str, _ArchiveEntry] = {}
    expanded = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_ARCHIVE_ENTRIES:
                    raise BottleLinkError("bottle archive exceeds its entry bound")
                path = _normalize_member_path(member.name, f"archive entry {index}")
                if path in entries:
                    raise BottleLinkError(f"bottle archive duplicates {path!r}")
                kind = _member_kind(member)
                if path not in {name, payload_root}:
                    _under_payload(path, payload_root, f"archive entry {index}")
                elif kind != "directory":
                    raise BottleLinkError("bottle archive roots must be directories")
                if isinstance(member.size, bool) or not 0 <= member.size <= MAX_EXPANDED_BYTES:
                    raise BottleLinkError(f"archive entry {path!r} has an invalid size")
                if kind == "regular":
                    expanded += member.size
                    if expanded > MAX_EXPANDED_BYTES:
                        raise BottleLinkError("bottle archive exceeds its expanded-byte bound")
                target = None
                if kind in {"symlink", "hardlink"}:
                    target = _link_target(
                        member.linkname,
                        path,
                        payload_root,
                        hardlink=kind == "hardlink",
                    )
                entries[path] = _ArchiveEntry(
                    path=path,
                    kind=kind,
                    mode=member.mode & 0o7777,
                    size=member.size,
                    target=target,
                )
    except (OSError, tarfile.TarError, UnicodeError) as error:
        raise BottleLinkError(f"bottle archive is not a valid bounded gzip tar: {error}") from error

    if entries.get(name, _ArchiveEntry("", "", 0, 0)).kind != "directory" or entries.get(
        payload_root, _ArchiveEntry("", "", 0, 0)
    ).kind != "directory":
        raise BottleLinkError("bottle archive lacks its exact directory roots")

    def resolve(path: str, trail: tuple[str, ...] = ()) -> _ArchiveEntry:
        if path in trail or len(trail) >= MAX_LINK_DEPTH:
            raise BottleLinkError(f"archive link cycle reaches {path!r}")
        entry = entries.get(path)
        if entry is None:
            raise BottleLinkError(f"archive link target {path!r} is absent")
        if entry.kind not in {"symlink", "hardlink"}:
            return entry
        assert entry.target is not None
        return resolve(entry.target, (*trail, path))

    all_files = sorted(
        path[len(payload_root) + 1 :]
        for path, entry in entries.items()
        if path.startswith(payload_root + "/")
        and entry.kind in {"regular", "symlink", "hardlink"}
    )
    if len(all_files) > MAX_INVENTORY_FILES:
        raise BottleLinkError("bottle link inventory exceeds its file bound")
    required_receipts = (f".brew/{name}.rb", "INSTALL_RECEIPT.json")
    for receipt in required_receipts:
        if resolve(f"{payload_root}/{receipt}").kind != "regular":
            raise BottleLinkError(f"bottle receipt {receipt!r} is not a regular file")
    path_exec_files = []
    for relative in all_files:
        if relative.split("/", 1)[0] not in {"bin", "sbin"}:
            continue
        resolved = resolve(f"{payload_root}/{relative}")
        if resolved.kind == "regular" and resolved.mode & 0o111:
            path_exec_files.append(relative)
    return {
        "schema": 1,
        "kind": "kandelo-homebrew-bottle-link-inventory",
        "payload_root": payload_root,
        "all_files": all_files,
        "path_exec_files": sorted(path_exec_files),
    }


def validate_bottle_link_inventory(
    value: Any, *, formula: str, version: str
) -> dict[str, object]:
    name = _stable_id(formula, "link inventory Formula")
    pkg_version = _version(version, "link inventory version")
    inventory = _mapping(value, "bottle link inventory")
    _exact(
        inventory,
        frozenset({"schema", "kind", "payload_root", "all_files", "path_exec_files"}),
        "bottle link inventory",
    )
    if (
        inventory["schema"] != 1
        or inventory["kind"] != "kandelo-homebrew-bottle-link-inventory"
        or inventory["payload_root"] != f"{name}/{pkg_version}"
    ):
        raise BottleLinkError("bottle link inventory identity changed")
    all_files = [
        _relative_path(item, f"bottle link file {index}")
        for index, item in enumerate(_sequence(inventory["all_files"], "bottle link files"))
    ]
    path_exec_files = [
        _relative_path(item, f"bottle executable {index}")
        for index, item in enumerate(
            _sequence(inventory["path_exec_files"], "bottle executables")
        )
    ]
    if (
        not all_files
        or len(all_files) > MAX_INVENTORY_FILES
        or all_files != sorted(set(all_files))
        or path_exec_files != sorted(set(path_exec_files))
        or not set(path_exec_files).issubset(all_files)
        or f".brew/{name}.rb" not in all_files
        or "INSTALL_RECEIPT.json" not in all_files
    ):
        raise BottleLinkError("bottle link inventory is incomplete or noncanonical")
    return {
        "schema": 1,
        "kind": "kandelo-homebrew-bottle-link-inventory",
        "payload_root": inventory["payload_root"],
        "all_files": all_files,
        "path_exec_files": path_exec_files,
    }


def load_guest_layout(body: bytes) -> dict[str, object]:
    if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_LAYOUT_BYTES:
        raise BottleLinkError("guest layout bytes are outside their bound")
    try:
        value = json.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BottleLinkError(f"guest layout is not UTF-8 JSON: {error}") from error
    layout = _mapping(value, "guest layout")
    _exact(
        layout,
        frozenset(
            {
                "schema",
                "kind",
                "prefix",
                "cellar",
                "repository",
                "stable_entrypoint",
                "retired_prefixes",
            }
        ),
        "guest layout",
    )
    if layout["schema"] != 1 or layout["kind"] != "kandelo-homebrew-guest-layout":
        raise BottleLinkError("guest layout protocol is unsupported")
    prefix = _absolute_path(layout["prefix"], "guest prefix")
    cellar = _absolute_path(layout["cellar"], "guest cellar")
    repository = _absolute_path(layout["repository"], "guest repository")
    stable_entrypoint = _absolute_path(layout["stable_entrypoint"], "guest entrypoint")
    retired = [
        _absolute_path(item, f"retired guest prefix {index}")
        for index, item in enumerate(_sequence(layout["retired_prefixes"], "retired prefixes"))
    ]
    if (
        cellar != prefix + "/Cellar"
        or repository != prefix
        or retired != sorted(set(retired))
        or prefix in retired
    ):
        raise BottleLinkError("guest layout paths are contradictory")
    return {
        "schema": 1,
        "kind": "kandelo-homebrew-guest-layout",
        "prefix": prefix,
        "cellar": cellar,
        "repository": repository,
        "stable_entrypoint": stable_entrypoint,
        "retired_prefixes": retired,
    }


def build_link_manifest(
    *,
    inventory: Any,
    guest_layout: Any,
    formula: str,
    version: str,
    architecture: str,
    target_abi: int,
    bottle_url: str,
    bottle_sha256: str,
    bottle_bytes: int,
) -> dict[str, object]:
    name = _stable_id(formula, "link manifest Formula")
    pkg_version = _version(version, "link manifest version")
    checked_inventory = validate_bottle_link_inventory(
        inventory, formula=name, version=pkg_version
    )
    layout = _mapping(guest_layout, "link manifest guest layout")
    # Re-encode only to reuse the strict shape/path validator for caller mappings.
    checked_layout = load_guest_layout(json.dumps(layout).encode())
    if architecture not in {"wasm32", "wasm64"}:
        raise BottleLinkError("link manifest architecture is unsupported")
    if isinstance(target_abi, bool) or not isinstance(target_abi, int) or not 1 <= target_abi <= 2**32 - 1:
        raise BottleLinkError("link manifest ABI is not a bounded positive integer")
    digest = _digest(bottle_sha256, "link manifest bottle")
    size = _positive(bottle_bytes, "link manifest bottle bytes")
    url = _text(bottle_url, "link manifest bottle URL", 8192)
    if not url.endswith("/blobs/sha256:" + digest):
        raise BottleLinkError("link manifest bottle URL does not bind its digest")

    def linkable(relative: str) -> bool:
        parts = relative.split("/")
        if not parts or parts[0] not in {"bin", "etc", "include", "lib", "sbin", "share", "var"}:
            return False
        if relative in {"lib/charset.alias", "share/locale/locale.alias", "share/info/dir"}:
            return False
        if relative.endswith("/.DS_Store"):
            return False
        if re.fullmatch(r"share/icons/.+/icon-theme\.cache", relative):
            return False
        if "/site-packages/" in relative and relative.endswith((".pyc", ".pyo")):
            return False
        return True

    links = [
        {"type": "symlink", "source": relative, "target": relative}
        for relative in checked_inventory["all_files"]
        if linkable(str(relative))
    ]
    linked = {str(item["source"]) for item in links}
    if not set(checked_inventory["path_exec_files"]).issubset(linked):
        raise BottleLinkError("bottle executable inventory contains an unlinked path")
    path_prepend = [
        directory
        for directory in ("bin", "sbin")
        if any(
            str(relative).startswith(directory + "/")
            for relative in checked_inventory["path_exec_files"]
        )
    ]
    result = {
        "schema": 1,
        "package": name,
        "version": pkg_version,
        "arch": architecture,
        "kandelo_abi": target_abi,
        "prefix": checked_layout["prefix"],
        "cellar": checked_layout["cellar"],
        "keg": f"{checked_layout['cellar']}/{name}/{pkg_version}",
        "bottle": {
            "url": url,
            "sha256": digest,
            "bytes": size,
            "cache_key_sha": digest,
            "payload_root": checked_inventory["payload_root"],
        },
        "links": links,
        "receipts": [f".brew/{name}.rb", "INSTALL_RECEIPT.json"],
        "env": {"PATH_prepend": path_prepend} if path_prepend else {},
    }
    return validate_link_manifest(
        result,
        formula=name,
        version=pkg_version,
        architecture=architecture,
        target_abi=target_abi,
        prefix=str(checked_layout["prefix"]),
        cellar=str(checked_layout["cellar"]),
        bottle_url=url,
        bottle_sha256=digest,
        bottle_bytes=size,
    )


def validate_link_manifest(
    value: Any,
    *,
    formula: str,
    version: str,
    architecture: str,
    target_abi: int,
    prefix: str,
    cellar: str,
    bottle_url: str,
    bottle_sha256: str,
    bottle_bytes: int,
) -> dict[str, object]:
    name = _stable_id(formula, "link manifest Formula")
    pkg_version = _version(version, "link manifest version")
    checked_prefix = _absolute_path(prefix, "link manifest expected prefix")
    checked_cellar = _absolute_path(cellar, "link manifest expected cellar")
    digest = _digest(bottle_sha256, "link manifest expected bottle")
    size = _positive(bottle_bytes, "link manifest expected bottle bytes")
    url = _text(bottle_url, "link manifest expected URL", 8192)
    manifest = _mapping(value, "link manifest")
    _exact(
        manifest,
        frozenset(
            {
                "schema",
                "package",
                "version",
                "arch",
                "kandelo_abi",
                "prefix",
                "cellar",
                "keg",
                "bottle",
                "links",
                "receipts",
                "env",
            }
        ),
        "link manifest",
    )
    if (
        manifest["schema"] != 1
        or manifest["package"] != name
        or manifest["version"] != pkg_version
        or manifest["arch"] != architecture
        or manifest["kandelo_abi"] != target_abi
        or manifest["prefix"] != checked_prefix
        or manifest["cellar"] != checked_cellar
        or manifest["keg"] != f"{checked_cellar}/{name}/{pkg_version}"
    ):
        raise BottleLinkError("link manifest identity differs from promotion facts")
    bottle = _mapping(manifest["bottle"], "link manifest bottle")
    _exact(
        bottle,
        frozenset({"url", "sha256", "bytes", "cache_key_sha", "payload_root"}),
        "link manifest bottle",
    )
    if bottle != {
        "url": url,
        "sha256": digest,
        "bytes": size,
        "cache_key_sha": digest,
        "payload_root": f"{name}/{pkg_version}",
    }:
        raise BottleLinkError("link manifest bottle differs from promoted bytes")
    links = []
    for index, candidate in enumerate(_sequence(manifest["links"], "link manifest links")):
        entry = _mapping(candidate, f"link manifest link {index}")
        _exact(entry, frozenset({"type", "source", "target"}), f"link manifest link {index}")
        source = _relative_path(entry["source"], f"link manifest source {index}")
        target = _relative_path(entry["target"], f"link manifest target {index}")
        if entry["type"] != "symlink" or source != target:
            raise BottleLinkError("link manifest must be a mechanical symlink projection")
        links.append({"type": "symlink", "source": source, "target": target})
    sources = [str(entry["source"]) for entry in links]
    if sources != sorted(set(sources)):
        raise BottleLinkError("link manifest links must be sorted and duplicate-free")
    if manifest["receipts"] != [f".brew/{name}.rb", "INSTALL_RECEIPT.json"]:
        raise BottleLinkError("link manifest receipt set changed")
    env = _mapping(manifest["env"], "link manifest environment")
    if set(env) not in {frozenset(), frozenset({"PATH_prepend"})}:
        raise BottleLinkError("link manifest environment fields changed")
    path_prepend = []
    if "PATH_prepend" in env:
        path_prepend = list(_sequence(env["PATH_prepend"], "link manifest PATH"))
        if any(item not in {"bin", "sbin"} for item in path_prepend) or path_prepend != [
            item for item in ("bin", "sbin") if item in set(path_prepend)
        ]:
            raise BottleLinkError("link manifest PATH projection is invalid")
    return {
        "schema": 1,
        "package": name,
        "version": pkg_version,
        "arch": architecture,
        "kandelo_abi": target_abi,
        "prefix": checked_prefix,
        "cellar": checked_cellar,
        "keg": f"{checked_cellar}/{name}/{pkg_version}",
        "bottle": dict(bottle),
        "links": links,
        "receipts": [f".brew/{name}.rb", "INSTALL_RECEIPT.json"],
        "env": {"PATH_prepend": path_prepend} if path_prepend else {},
    }
