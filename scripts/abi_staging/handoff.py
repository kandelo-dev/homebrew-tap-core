"""Create and validate bounded uncredentialed bottle-build handoffs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import subprocess
import tarfile
from typing import Any
from urllib.parse import unquote, urlsplit

from .canonical import (
    CanonicalJsonError,
    canonical_bytes,
    parse_canonical_bytes,
    parse_json_bytes,
)
from .bottle_link import (
    BottleLinkError,
    build_link_manifest,
    inspect_bottle_link_inventory,
    load_guest_layout,
)
from .contract import (
    validate_capture_assessment,
    load_bottle_contract,
)
from .custody import (
    CustodyError,
    create_source_custody,
    load_source_custody_manifest,
    validate_source_custody,
)
from .git_policy import protected_git_arguments
from .formula_inventory import FormulaInventoryError, normalize_formula_source
from .plan import exact_formula_subject
from .policy import candidate_repository, load_tap_staging_policy
from .records import load_tap_plan_record


MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_ARCHIVE_ENTRIES = 200_000
MAX_DIAGNOSTIC_BYTES = 4 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
HTTPS_URL = re.compile(r"^https?://[^\s]+$")
RFC3339_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
RAW_FORMULA_KEYS = frozenset(
    {
        "desc",
        "homepage",
        "license",
        "name",
        "path",
        "pkg_version",
        "tap_git_path",
        "tap_git_remote",
        "tap_git_revision",
    }
)
RAW_BOTTLE_KEYS = frozenset({"cellar", "date", "rebuild", "root_url", "tags"})
RAW_TAG_KEYS = frozenset(
    {
        "all_files",
        "filename",
        "installed_size",
        "local_filename",
        "path_exec_files",
        "sbom",
        "sha256",
        "tab",
    }
)
INVENTORY_KEYS = frozenset({"schema", "kind", "subject", "outcome", "files"})
FILE_KEYS = frozenset({"path", "role", "sha256", "bytes"})
RESULT_KEYS = frozenset(
    {
        "schema",
        "kind",
        "request_sha256",
        "subject",
        "outcome",
        "exit_code",
        "candidate",
        "diagnostic_summary_sha256",
    }
)
CANDIDATE_KEYS = frozenset(
    {
        "bottle_contract_sha256",
        "bottle_layer",
        "bottle_metadata",
        "vfs_composition_descriptor",
    }
)
LOCAL_ARTIFACT_KEYS = frozenset({"sha256", "bytes"})
ALLOWED_DIRECTORIES = frozenset(
    {"diagnostics", "source-custody", "source-custody/submodules"}
)
REQUIRED_COMMON_PATHS = frozenset(
    {
        "attempt-record.json",
        "bottle-contract.json",
        "build-result.json",
        "diagnostics/summary.txt",
        "source-custody/kandelo.bundle",
        "source-custody/kandelo-tree.tar",
        "source-custody/manifest.json",
        "source-custody/tap.bundle",
        "source-custody/tap-tree.tar",
    }
)
SUCCESS_PATHS = frozenset(
    {"bottle.tar.gz", "bottle-metadata.json", "vfs-composition-descriptor.json"}
)
RUN_KEYS = frozenset({"repository", "workflow_ref", "run_id", "run_attempt", "job"})
SECRET_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"AKIA[A-Z0-9]{16}"),
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
)


class HandoffError(ValueError):
    """Raised when a candidate-produced handoff is unsafe or contradictory."""


def _read_regular(path: Path, field: str, maximum: int = MAX_JSON_BYTES) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise HandoffError(f"cannot inspect {field}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HandoffError(f"{field} must be a regular non-symlink file")
    if metadata.st_size < 1 or metadata.st_size > maximum:
        raise HandoffError(f"{field} is outside its byte bound")
    try:
        body = path.read_bytes()
    except OSError as error:
        raise HandoffError(f"cannot read {field}: {error}") from error
    if len(body) != metadata.st_size:
        raise HandoffError(f"{field} changed while reading")
    return body


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HandoffError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise HandoffError(f"{field} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise HandoffError(
            f"{field} fields changed: missing={sorted(expected - actual)!r} "
            f"extra={sorted(actual - expected)!r}"
        )


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise HandoffError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise HandoffError(f"{field} is not UTF-8") from error
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise HandoffError(f"{field} is outside its string bound")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise HandoffError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 2**64 - 1:
        raise HandoffError(f"{field} is not a bounded integer")
    return value


def _subject(value: Any, field: str) -> str:
    subject = _text(value, field, 512)
    try:
        parsed = _mapping(json.loads(subject), field)
    except (json.JSONDecodeError, HandoffError) as error:
        raise HandoffError(f"{field} is not exact subject JSON: {error}") from error
    _exact_keys(parsed, frozenset({"architecture", "identity", "kind"}), field)
    if parsed["kind"] != "formula":
        raise HandoffError(f"{field} is not a Formula subject")
    identity = _text(parsed["identity"], f"{field} identity", 128)
    if STABLE_ID.fullmatch(identity) is None:
        raise HandoffError(f"{field} identity is invalid")
    architecture = parsed["architecture"]
    if architecture not in {"wasm32", "wasm64"}:
        raise HandoffError(f"{field} architecture is unsupported")
    if subject != exact_formula_subject(identity, architecture):
        raise HandoffError(f"{field} is not canonical")
    return subject


def _relative_path(value: Any, field: str) -> str:
    path = _text(value, field, MAX_PATH_BYTES)
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise HandoffError(f"{field} is not a normalized relative path")
    return path


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def _canonical_mapping(body: bytes, field: str) -> dict[str, Any]:
    try:
        parsed = parse_canonical_bytes(body, maximum_bytes=MAX_JSON_BYTES)
    except CanonicalJsonError as error:
        raise HandoffError(f"{field} is not canonical JSON: {error}") from error
    return dict(_mapping(_plain(parsed), field))


def _source_identity(value: Any, field: str) -> dict[str, str]:
    source = _mapping(value, field)
    _exact_keys(source, frozenset({"repository", "commit", "tree"}), field)
    repository = _text(source["repository"], f"{field} repository", 255)
    commit = source["commit"]
    tree = source["tree"]
    if (
        REPOSITORY.fullmatch(repository) is None
        or not isinstance(commit, str)
        or GIT_SHA.fullmatch(commit) is None
        or not isinstance(tree, str)
        or GIT_SHA.fullmatch(tree) is None
    ):
        raise HandoffError(f"{field} identity is invalid")
    return {"repository": repository, "commit": commit, "tree": tree}


def _tap_name(repository: str) -> str:
    owner, separator, name = repository.partition("/")
    if not separator or not name.startswith("homebrew-") or name == "homebrew-":
        raise HandoffError("tap repository cannot derive its Homebrew tap name")
    return f"{owner}/{name.removeprefix('homebrew-')}"


def _validate_raw_formula_remote(
    remote_value: Any, *, tap_source: Mapping[str, str]
) -> None:
    remote = _text(remote_value, "composition Formula tap Git remote")
    repository = tap_source["repository"].lower()
    if remote in {
        f"https://github.com/{repository}",
        f"https://github.com/{repository}.git",
    }:
        return
    parsed = urlsplit(remote)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise HandoffError(
            "composition Formula tap Git remote does not match the exact tap"
        )
    # Homebrew records the build job's exact local tap checkout. Composition
    # runs in a later protected job, so that runner-local path no longer
    # exists. The separately validated source-custody bundle authenticates the
    # tap bytes; this field binds their recorded source identity without
    # pretending the former runner filesystem is durable authority.
    decoded_path = unquote(parsed.path)
    path = Path(decoded_path)
    if (
        not path.is_absolute()
        or decoded_path in {"", "/"}
        or "\\" in decoded_path
        or "//" in decoded_path
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise HandoffError(
            "composition Formula tap Git remote is not one normalized absolute path"
        )


def _package_version(version: Any, revision: Any, field: str) -> str:
    base = _text(version, f"{field} version", 256)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+,-]{0,255}", base) is None:
        raise HandoffError(f"{field} version is invalid")
    checked_revision = _integer(revision, f"{field} revision")
    return base if checked_revision == 0 else f"{base}_{checked_revision}"


def prepare_composition_input(
    *,
    context: Mapping[str, Any],
    bottle_body: bytes,
    metadata_body: bytes,
    guest_layout_body: bytes,
) -> dict[str, Any]:
    """Bind exact bottle bytes to inert VFS composition input for Kandelo."""

    value = _mapping(context, "build context")
    if value.get("schema") != 1 or value.get("kind") != "kandelo-abi-staging-build-context":
        raise HandoffError("build context protocol is unsupported")
    source = _source_identity(value.get("request_source"), "composition source")
    tap_source = _source_identity(value.get("tap_source"), "composition tap source")
    formula = _text(value.get("formula"), "composition Formula", 128)
    if STABLE_ID.fullmatch(formula) is None:
        raise HandoffError("composition Formula is invalid")
    architecture = value.get("architecture")
    if architecture not in {"wasm32", "wasm64"}:
        raise HandoffError("composition architecture is unsupported")
    target_abi = _integer(value.get("target_abi"), "composition target ABI", positive=True)
    identity = _mapping(value.get("formula_identity"), "composition Formula identity")
    _exact_keys(
        identity,
        frozenset(
            {
                "name",
                "version",
                "revision",
                "rebuild",
                "architecture",
                "formula_path",
                "normalized_formula_sha256",
            }
        ),
        "composition Formula identity",
    )
    if (
        identity["name"] != formula
        or identity["architecture"] != architecture
        or identity["formula_path"] != f"Formula/{formula}.rb"
    ):
        raise HandoffError("composition Formula identity differs from the build context")
    version = _text(identity["version"], "composition Formula version", 256)
    revision = _integer(identity["revision"], "composition Formula revision")
    pkg_version = _package_version(version, revision, "composition Formula")
    rebuild = _integer(identity["rebuild"], "composition Formula rebuild")
    normalized_formula_sha256 = _digest(
        identity["normalized_formula_sha256"], "composition normalized Formula"
    )
    roots = [
        _text(item, f"composition root {index}", 128)
        for index, item in enumerate(
            _sequence(value.get("composition_roots"), "composition roots")
        )
    ]
    if (
        not roots
        or len(roots) > 256
        or roots != sorted(set(roots))
        or any(STABLE_ID.fullmatch(root) is None for root in roots)
    ):
        raise HandoffError("composition roots are invalid")
    if not isinstance(bottle_body, bytes) or not 1 <= len(bottle_body) <= 2 * 1024**3:
        raise HandoffError("composition bottle bytes are outside their bound")
    bottle_sha256 = hashlib.sha256(bottle_body).hexdigest()
    if not isinstance(metadata_body, bytes) or not 1 <= len(metadata_body) <= MAX_JSON_BYTES:
        raise HandoffError("composition bottle metadata bytes are outside their bound")
    try:
        metadata_value = json.loads(metadata_body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HandoffError(f"composition bottle metadata is not UTF-8 JSON: {error}") from error
    metadata = _mapping(metadata_value, "composition bottle metadata")
    tap_name = _tap_name(tap_source["repository"])
    full_name = f"{tap_name}/{formula}"
    if frozenset(metadata) != {full_name}:
        raise HandoffError("composition bottle metadata names another Formula")
    entry = _mapping(metadata[full_name], "composition bottle metadata entry")
    _exact_keys(entry, frozenset({"formula", "bottle"}), "composition bottle metadata entry")
    formula_metadata = _mapping(entry["formula"], "composition bottle Formula metadata")
    raw_builder_json = frozenset(formula_metadata) == RAW_FORMULA_KEYS
    _exact_keys(
        formula_metadata,
        RAW_FORMULA_KEYS
        if raw_builder_json
        else frozenset({"name", "path", "pkg_version"}),
        "composition bottle Formula metadata",
    )
    owner, tap = tap_name.split("/", 1)
    expected_formula_path = f"Library/Taps/{owner}/homebrew-{tap}/Formula/{formula}.rb"
    if any(
        formula_metadata.get(field) != expected
        for field, expected in {
            "name": formula,
            "path": expected_formula_path,
            "pkg_version": pkg_version,
        }.items()
    ):
        raise HandoffError("composition bottle Formula metadata differs from the plan")
    if raw_builder_json:
        if formula_metadata["tap_git_path"] != f"Formula/{formula}.rb":
            raise HandoffError("composition bottle Formula path differs from the exact tap")
        prepared_revision = _text(
            formula_metadata["tap_git_revision"],
            "composition bottle prepared tap revision",
            40,
        )
        if GIT_SHA.fullmatch(prepared_revision) is None:
            raise HandoffError("composition bottle prepared tap revision is invalid")
        for field in ("desc", "license"):
            _text(
                formula_metadata[field],
                f"composition bottle Formula {field}",
            )
        homepage = _text(
            formula_metadata["homepage"],
            "composition bottle Formula homepage",
        )
        if HTTPS_URL.fullmatch(homepage) is None:
            raise HandoffError("composition bottle Formula homepage is invalid")
        _validate_raw_formula_remote(
            formula_metadata["tap_git_remote"], tap_source=tap_source
        )
    bottle = _mapping(entry["bottle"], "composition bottle metadata payload")
    _exact_keys(
        bottle,
        RAW_BOTTLE_KEYS
        if raw_builder_json
        else frozenset({"root_url", "cellar", "rebuild", "tags"}),
        "composition bottle metadata payload",
    )
    root_url = _text(value.get("bottle_root_url"), "composition bottle root", 8192)
    formula_suffix = f"/{formula}"
    metadata_root_url = root_url.removesuffix(formula_suffix)
    if (
        not root_url.startswith("https://ghcr.io/v2/")
        or root_url.endswith("/")
        or metadata_root_url == root_url
        or bottle["root_url"] != metadata_root_url
        or bottle["rebuild"] != rebuild
    ):
        raise HandoffError("composition bottle metadata uses another publication identity")
    tag_name = f"{architecture}_kandelo"
    tags = _mapping(bottle["tags"], "composition bottle tags")
    if frozenset(tags) != {tag_name}:
        raise HandoffError("composition bottle metadata has another architecture tag")
    tag = _mapping(tags[tag_name], "composition bottle tag")
    accepted_tag_keys = (
        {RAW_TAG_KEYS}
        if raw_builder_json
        else {
            frozenset({"local_filename", "sha256"}),
            frozenset({"cellar", "local_filename", "sha256"}),
        }
    )
    if frozenset(tag) not in accepted_tag_keys:
        raise HandoffError("composition bottle tag fields changed")
    suffix = "" if rebuild == 0 else f".{rebuild}"
    expected_filename = (
        f"{formula}--{pkg_version}.{tag_name}.bottle{suffix}.tar.gz"
    )
    if (
        tag.get("sha256") != bottle_sha256
        or tag.get("local_filename") != expected_filename
        or ("cellar" in tag and tag["cellar"] != bottle["cellar"])
    ):
        raise HandoffError("composition bottle tag differs from the exact bottle")
    raw_all_files: list[str] | None = None
    raw_path_exec_files: list[str] | None = None
    if raw_builder_json:
        date = _text(bottle["date"], "composition bottle date", 64)
        if RFC3339_UTC.fullmatch(date) is None:
            raise HandoffError("composition bottle date is invalid")
        _text(bottle["cellar"], "composition bottle cellar")
        if tag["filename"] != (
            f"{formula}-{pkg_version}.{tag_name}.bottle{suffix}.tar.gz"
        ):
            raise HandoffError("composition bottle URL filename differs from the plan")
        _integer(
            tag["installed_size"],
            "composition bottle installed size",
            positive=True,
        )
        raw_all_files = [
            _relative_path(item, f"composition bottle file {index}")
            for index, item in enumerate(
                _sequence(tag["all_files"], "composition bottle files")
            )
        ]
        raw_path_exec_files = [
            _relative_path(item, f"composition bottle executable {index}")
            for index, item in enumerate(
                _sequence(tag["path_exec_files"], "composition bottle executables")
            )
        ]
        if (
            len(raw_all_files) > 200_000
            or len(raw_all_files) != len(set(raw_all_files))
            or len(raw_path_exec_files) != len(set(raw_path_exec_files))
            or not set(raw_path_exec_files).issubset(raw_all_files)
        ):
            raise HandoffError("composition bottle file metadata is noncanonical")
        raw_all_files.sort()
        raw_path_exec_files.sort()
        _mapping(tag["tab"], "composition bottle tab")
        _mapping(tag["sbom"], "composition bottle SBOM")
    transport_url = f"{root_url}/blobs/sha256:{bottle_sha256}"
    immutable_reference = (
        "ghcr.io/" + root_url.removeprefix("https://ghcr.io/v2/")
        + f"@sha256:{bottle_sha256}"
    )
    try:
        inventory = inspect_bottle_link_inventory(
            bottle_body, formula=formula, version=pkg_version
        )
        guest_layout = load_guest_layout(guest_layout_body)
        link_manifest = build_link_manifest(
            inventory=inventory,
            guest_layout=guest_layout,
            formula=formula,
            version=pkg_version,
            architecture=architecture,
            target_abi=target_abi,
            bottle_url=transport_url,
            bottle_sha256=bottle_sha256,
            bottle_bytes=len(bottle_body),
        )
    except BottleLinkError as error:
        raise HandoffError(f"cannot derive composition link manifest: {error}") from error
    result = {
        "schema": 1,
        "kind": "kandelo-abi-staging-homebrew-composition-input",
        "source": source,
        "tap_source": tap_source,
        "formula": {
            "name": formula,
            "full_name": full_name,
            "version": version,
            "pkg_version": pkg_version,
            "revision": revision,
            "rebuild": rebuild,
            "architecture": architecture,
            "target_abi": target_abi,
            "normalized_formula_sha256": normalized_formula_sha256,
        },
        "bottle": {
            "sha256": bottle_sha256,
            "bytes": len(bottle_body),
            "immutable_reference": immutable_reference,
            "transport_url": transport_url,
        },
        "required_by": roots,
        "link_manifest": link_manifest,
    }
    return json.loads(canonical_bytes(result))


def load_build_run(body: bytes, *, expected_repository: str) -> dict[str, Any]:
    run = _canonical_mapping(body, "build run")
    _exact_keys(run, RUN_KEYS, "build run")
    repository = _text(run["repository"], "build run repository", 255)
    if REPOSITORY.fullmatch(repository) is None:
        raise HandoffError("build run repository is not owner/name")
    if repository != expected_repository:
        raise HandoffError("build run names a different tap repository")
    job = _text(run["job"], "build run job", 128)
    if STABLE_ID.fullmatch(job) is None or job != "build-candidate":
        raise HandoffError("build run does not name the candidate build job")
    return {
        "repository": repository,
        "workflow_ref": _text(run["workflow_ref"], "build run workflow ref", 1024),
        "run_id": _integer(run["run_id"], "build run ID", positive=True),
        "run_attempt": _integer(
            run["run_attempt"], "build run attempt", positive=True
        ),
        "job": job,
    }


def _local_artifact(value: Any, field: str) -> dict[str, Any]:
    artifact = _mapping(value, field)
    _exact_keys(artifact, LOCAL_ARTIFACT_KEYS, field)
    return {
        "sha256": _digest(artifact["sha256"], f"{field} digest"),
        "bytes": _integer(artifact["bytes"], f"{field} bytes", positive=True),
    }


def _file_identity(path: Path) -> dict[str, Any]:
    body = path.read_bytes()
    return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def validate_build_result(result: Mapping[str, Any]) -> None:
    value = _mapping(result, "build result")
    _exact_keys(value, RESULT_KEYS, "build result")
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-build-result":
        raise HandoffError("build result protocol is unsupported")
    _digest(value["request_sha256"], "build result request")
    _subject(value["subject"], "build result subject")
    outcome = value["outcome"]
    if outcome not in {"success", "failure"}:
        raise HandoffError("build result outcome is unsupported")
    exit_code = _integer(value["exit_code"], "build result exit code")
    _digest(value["diagnostic_summary_sha256"], "diagnostic summary")
    if outcome == "failure":
        if exit_code == 0 or value["candidate"] is not None:
            raise HandoffError("failed build result cannot claim a candidate")
        return
    if exit_code != 0 or value["candidate"] is None:
        raise HandoffError("successful build result requires candidate identity")
    candidate = _mapping(value["candidate"], "build result candidate")
    _exact_keys(candidate, CANDIDATE_KEYS, "build result candidate")
    _digest(candidate["bottle_contract_sha256"], "candidate bottle contract")
    _local_artifact(candidate["bottle_layer"], "candidate bottle layer")
    _local_artifact(candidate["bottle_metadata"], "candidate bottle metadata")
    _local_artifact(
        candidate["vfs_composition_descriptor"],
        "candidate VFS composition descriptor",
    )


def load_build_result(body: bytes) -> dict[str, Any]:
    result = _canonical_mapping(body, "build result")
    validate_build_result(result)
    return result


def validate_handoff_inventory(inventory: Mapping[str, Any]) -> None:
    value = _mapping(inventory, "handoff inventory")
    _exact_keys(value, INVENTORY_KEYS, "handoff inventory")
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-build-handoff-inventory":
        raise HandoffError("handoff inventory protocol is unsupported")
    _subject(value["subject"], "handoff inventory subject")
    if value["outcome"] not in {"success", "failure"}:
        raise HandoffError("handoff inventory outcome is unsupported")
    previous = ""
    for index, candidate in enumerate(_sequence(value["files"], "handoff files")):
        entry = _mapping(candidate, f"handoff file {index}")
        _exact_keys(entry, FILE_KEYS, f"handoff file {index}")
        path = _relative_path(entry["path"], f"handoff file path {index}")
        if path == "inventory.json":
            raise HandoffError("handoff inventory cannot include itself")
        if path <= previous:
            raise HandoffError("handoff files must be sorted and duplicate-free")
        previous = path
        expected_role = _role_for_path(path)
        if entry["role"] != expected_role:
            raise HandoffError(f"handoff file {path!r} has the wrong role")
        _digest(entry["sha256"], f"handoff file digest {path}")
        _integer(entry["bytes"], f"handoff file bytes {path}", positive=True)


def load_handoff_inventory(body: bytes) -> dict[str, Any]:
    inventory = _canonical_mapping(body, "handoff inventory")
    validate_handoff_inventory(inventory)
    return inventory


def _role_for_path(path: str) -> str:
    exact = {
        "attempt-record.json": "attempt-record",
        "bottle-contract.json": "bottle-contract",
        "bottle-metadata.json": "bottle-metadata",
        "bottle.tar.gz": "bottle-layer",
        "vfs-composition-descriptor.json": "vfs-composition-descriptor",
        "build-result.json": "build-result",
        "diagnostics/summary.txt": "diagnostic-summary",
        "source-custody/manifest.json": "source-custody-manifest",
        "source-custody/kandelo.bundle": "source-custody-bundle",
        "source-custody/kandelo-tree.tar": "source-custody-tree",
        "source-custody/tap.bundle": "source-custody-bundle",
        "source-custody/tap-tree.tar": "source-custody-tree",
    }
    if path in exact:
        return exact[path]
    if path.startswith("diagnostics/"):
        return "diagnostic"
    match = re.fullmatch(
        r"source-custody/submodules/([a-z0-9][a-z0-9._-]{0,127})(\.bundle|-tree\.tar)",
        path,
    )
    if match is not None:
        return "source-custody-bundle" if match.group(2) == ".bundle" else "source-custody-tree"
    raise HandoffError(f"unexpected handoff file: {path}")


def _scan_regular_files(root: Path) -> list[tuple[str, Path, os.stat_result]]:
    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise HandoffError(f"cannot inspect handoff root: {error}") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise HandoffError("handoff root must be a real directory")
    entries: list[tuple[str, Path, os.stat_result]] = []
    for candidate in sorted(root.rglob("*")):
        relative = candidate.relative_to(root).as_posix()
        _relative_path(relative, "handoff member path")
        metadata = candidate.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            if relative not in ALLOWED_DIRECTORIES:
                raise HandoffError(f"unexpected handoff directory: {relative}")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise HandoffError(f"handoff member is a symlink: {relative}")
        if not stat.S_ISREG(metadata.st_mode):
            raise HandoffError(f"handoff member is not a regular file: {relative}")
        if metadata.st_nlink != 1:
            raise HandoffError(f"handoff member is hard-linked: {relative}")
        entries.append((relative, candidate, metadata))
    return entries


@contextmanager
def _open_member_nofollow(
    root: Path, relative: str, expected: os.stat_result
):
    parts = relative.split("/")
    root_flags = os.O_RDONLY | os.O_DIRECTORY
    file_flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        root_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        root_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current = os.open(root, root_flags)
        descriptors.append(current)
        for component in parts[:-1]:
            current = os.open(component, root_flags, dir_fd=current)
            descriptors.append(current)
        member = os.open(parts[-1], file_flags, dir_fd=current)
        descriptors.append(member)
        actual = os.fstat(member)
        if (
            not stat.S_ISREG(actual.st_mode)
            or actual.st_nlink != 1
            or actual.st_dev != expected.st_dev
            or actual.st_ino != expected.st_ino
            or actual.st_size != expected.st_size
        ):
            raise HandoffError(f"handoff member changed while opening: {relative}")
        with os.fdopen(os.dup(member), "rb", closefd=True) as stream:
            yield stream
    except OSError as error:
        raise HandoffError(f"cannot open handoff member {relative!r}: {error}") from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_member_nofollow(
    root: Path, relative: str, expected: os.stat_result, maximum: int
) -> bytes:
    if expected.st_size < 1 or expected.st_size > maximum:
        raise HandoffError(f"handoff member {relative!r} is outside its byte bound")
    with _open_member_nofollow(root, relative, expected) as stream:
        body = stream.read(maximum + 1)
    if len(body) != expected.st_size:
        raise HandoffError(f"handoff member changed while reading: {relative}")
    return body


def _hash_member_nofollow(
    root: Path, relative: str, expected: os.stat_result
) -> tuple[str, int]:
    digest = hashlib.sha256()
    observed = 0
    with _open_member_nofollow(root, relative, expected) as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            digest.update(chunk)
    if observed != expected.st_size:
        raise HandoffError(f"handoff member changed while hashing: {relative}")
    return digest.hexdigest(), observed


def _validate_path_set(paths: set[str], outcome: str) -> None:
    expected = set(REQUIRED_COMMON_PATHS)
    if outcome == "success":
        expected.update(SUCCESS_PATHS)
    elif paths.intersection(SUCCESS_PATHS):
        raise HandoffError("failed handoff contains candidate bottle files")
    missing = expected - paths
    if missing:
        raise HandoffError(f"handoff is missing required files: {sorted(missing)!r}")
    submodule_members: dict[str, set[str]] = {}
    for path in paths:
        match = re.fullmatch(
            r"source-custody/submodules/([a-z0-9][a-z0-9._-]{0,127})(\.bundle|-tree\.tar)",
            path,
        )
        if match is not None:
            submodule_members.setdefault(match.group(1), set()).add(match.group(2))
    for identity, suffixes in submodule_members.items():
        if suffixes != {".bundle", "-tree.tar"}:
            raise HandoffError(f"source-custody submodule {identity!r} is incomplete")


def build_handoff_inventory(
    root: Path, *, subject: str, outcome: str
) -> dict[str, Any]:
    checked_subject = _subject(subject, "handoff inventory subject")
    if outcome not in {"success", "failure"}:
        raise HandoffError("handoff inventory outcome is unsupported")
    files = []
    for relative, path, metadata in _scan_regular_files(root):
        if relative == "inventory.json":
            continue
        role = _role_for_path(relative)
        body = path.read_bytes()
        if len(body) != metadata.st_size or not body:
            raise HandoffError(f"handoff member changed while hashing: {relative}")
        files.append(
            {
                "path": relative,
                "role": role,
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
        )
    _validate_path_set({entry["path"] for entry in files}, outcome)
    inventory = {
        "schema": 1,
        "kind": "kandelo-abi-staging-build-handoff-inventory",
        "subject": checked_subject,
        "outcome": outcome,
        "files": files,
    }
    validate_handoff_inventory(inventory)
    return inventory


def write_handoff_inventory(root: Path, *, subject: str, outcome: str) -> None:
    inventory = build_handoff_inventory(root, subject=subject, outcome=outcome)
    destination = root / "inventory.json"
    temporary = root / ".inventory.json.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise HandoffError("temporary inventory path already exists")
    temporary.write_bytes(canonical_bytes(inventory))
    os.replace(temporary, destination)


def _normalize_archive_path(value: str, field: str) -> str:
    path = _text(value.rstrip("/"), field, MAX_PATH_BYTES)
    while path.startswith("./"):
        path = path[2:]
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise HandoffError(f"archive member is unsafe: {value!r}")
    return path


def _inspect_archive(root: Path, relative: str, expected: os.stat_result) -> None:
    try:
        with _open_member_nofollow(root, relative, expected) as stream:
            with tarfile.open(fileobj=stream, mode="r|*") as archive:
                for index, member in enumerate(archive, start=1):
                    if index > MAX_ARCHIVE_ENTRIES:
                        raise HandoffError("archive member count exceeds its bound")
                    normalized = _normalize_archive_path(member.name, "archive member")
                    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                        raise HandoffError(f"archive member has unsupported type: {normalized}")
                    if member.issym() or member.islnk():
                        target = _text(member.linkname, "archive link target", MAX_PATH_BYTES)
                        if target.startswith("/") or "\\" in target:
                            raise HandoffError("archive link target is unsafe")
                        resolved = posixpath.normpath(
                            target
                            if member.islnk()
                            else posixpath.join(posixpath.dirname(normalized), target)
                        )
                        if (
                            resolved == ".."
                            or resolved.startswith("../")
                            or resolved.startswith("/")
                        ):
                            raise HandoffError("archive link target escapes its archive")
    except (tarfile.TarError, OSError) as error:
        raise HandoffError(f"cannot inspect archive {relative!r}: {error}") from error


def _validate_diagnostics(
    root: Path,
    inventory: Mapping[str, Any],
    scanned_by_path: Mapping[str, tuple[Path, os.stat_result]],
) -> None:
    for entry in inventory["files"]:
        if not entry["path"].startswith("diagnostics/"):
            continue
        body = _read_member_nofollow(
            root,
            entry["path"],
            scanned_by_path[entry["path"]][1],
            MAX_DIAGNOSTIC_BYTES,
        )
        if any(pattern.search(body) is not None for pattern in SECRET_PATTERNS):
            raise HandoffError(f"diagnostic contains a secret-shaped value: {entry['path']}")


def validate_handoff(
    root: Path,
    *,
    max_files: int,
    max_bytes: int,
    expected_request_sha256: str,
    expected_subject: str,
    expected_kandelo_source: Mapping[str, Any],
    expected_tap_source: Mapping[str, Any],
) -> dict[str, Any]:
    if max_files < 1 or max_files > 65_536 or max_bytes < 1 or max_bytes > 2**63 - 1:
        raise HandoffError("handoff limits are invalid")
    scanned = _scan_regular_files(root)
    scanned_by_path = {relative: (path, metadata) for relative, path, metadata in scanned}
    if "inventory.json" not in scanned_by_path:
        raise HandoffError("handoff inventory is missing")
    inventory = load_handoff_inventory(
        _read_member_nofollow(
            root,
            "inventory.json",
            scanned_by_path["inventory.json"][1],
            MAX_JSON_BYTES,
        )
    )
    if len(scanned) > max_files:
        raise HandoffError("handoff file count exceeds its bound")
    total_bytes = sum(metadata.st_size for _, metadata in scanned_by_path.values())
    if total_bytes > max_bytes:
        raise HandoffError("handoff byte count exceeds its bound")
    expected_entries = {entry["path"]: entry for entry in inventory["files"]}
    actual_paths = set(scanned_by_path) - {"inventory.json"}
    if actual_paths != set(expected_entries):
        raise HandoffError(
            "handoff contains unlisted or missing files: "
            f"unlisted={sorted(actual_paths - set(expected_entries))!r} "
            f"missing={sorted(set(expected_entries) - actual_paths)!r}"
        )
    _validate_path_set(actual_paths, inventory["outcome"])
    for relative, entry in expected_entries.items():
        _, metadata = scanned_by_path[relative]
        digest, observed = _hash_member_nofollow(root, relative, metadata)
        if observed != entry["bytes"]:
            raise HandoffError(f"handoff byte count differs from inventory: {relative}")
        if digest != entry["sha256"]:
            raise HandoffError(f"handoff digest differs from inventory: {relative}")

    _validate_diagnostics(root, inventory, scanned_by_path)
    result = load_build_result(
        _read_member_nofollow(
            root,
            "build-result.json",
            scanned_by_path["build-result.json"][1],
            MAX_JSON_BYTES,
        )
    )
    if result["outcome"] != inventory["outcome"] or result["subject"] != inventory["subject"]:
        raise HandoffError("build result differs from inventory identity")
    if result["request_sha256"] != _digest(
        expected_request_sha256, "expected handoff request"
    ):
        raise HandoffError("build result refers to a different exact request")
    if result["subject"] != _subject(expected_subject, "expected handoff subject"):
        raise HandoffError("build result refers to a different exact Formula subject")
    custody_manifest_body = _read_member_nofollow(
        root,
        "source-custody/manifest.json",
        scanned_by_path["source-custody/manifest.json"][1],
        MAX_JSON_BYTES,
    )
    try:
        custody_manifest = load_source_custody_manifest(custody_manifest_body)
        validate_source_custody(
            root=root / "source-custody",
            expected_request_sha256=expected_request_sha256,
            expected_subject=expected_subject,
            expected_kandelo_source=expected_kandelo_source,
            expected_tap_source=expected_tap_source,
        )
    except CustodyError as error:
        raise HandoffError(f"source custody is invalid: {error}") from error
    diagnostic_digest = expected_entries["diagnostics/summary.txt"]["sha256"]
    if result["diagnostic_summary_sha256"] != diagnostic_digest:
        raise HandoffError("build result diagnostic digest differs from exact bytes")
    contract_body = _read_member_nofollow(
        root,
        "bottle-contract.json",
        scanned_by_path["bottle-contract.json"][1],
        16 * 1024 * 1024,
    )
    contract = load_bottle_contract(contract_body)
    contract_digest = hashlib.sha256(contract_body).hexdigest()
    parsed_subject = json.loads(inventory["subject"])
    if (
        contract["formula"]["name"] != parsed_subject["identity"]
        or contract["target"]["architecture"] != parsed_subject["architecture"]
    ):
        raise HandoffError("bottle contract differs from exact handoff subject")
    if result["candidate"] is not None:
        candidate = result["candidate"]
        if candidate["bottle_contract_sha256"] != contract_digest:
            raise HandoffError("candidate contract digest differs from exact bytes")
        for field, filename in (
            ("bottle_layer", "bottle.tar.gz"),
            ("bottle_metadata", "bottle-metadata.json"),
        ):
            if candidate[field] != _file_identity(root / filename):
                raise HandoffError(f"candidate {field} differs from exact bytes")
    for entry in inventory["files"]:
        if entry["role"] in {"bottle-layer", "source-custody-tree"}:
            _inspect_archive(root, entry["path"], scanned_by_path[entry["path"]][1])
    return {
        "schema": 1,
        "kind": "kandelo-validated-build-handoff",
        "request_sha256": result["request_sha256"],
        "subject": inventory["subject"],
        "outcome": inventory["outcome"],
        "candidate": result["candidate"],
        "source_capsule_sha256": custody_manifest["capsule_sha256"],
        "inventory_sha256": hashlib.sha256(
            _read_member_nofollow(
                root,
                "inventory.json",
                scanned_by_path["inventory.json"][1],
                MAX_JSON_BYTES,
            )
        ).hexdigest(),
    }


def _git_identity(root: Path, field: str) -> tuple[str, str]:
    try:
        metadata = root.lstat()
    except OSError as error:
        raise HandoffError(f"cannot inspect {field} checkout: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise HandoffError(f"{field} checkout must be a real directory")
    command = protected_git_arguments(
        root,
        "rev-parse",
        "HEAD",
        "HEAD^{tree}",
        file_protocol="never",
    )
    try:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env={
                "PATH": os.environ.get("PATH", ""),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HandoffError(f"cannot inspect {field} Git identity: {error}") from error
    values = result.stdout.decode("ascii", errors="strict").splitlines()
    if result.returncode != 0 or len(values) != 2:
        detail = result.stderr.decode("utf-8", errors="replace")[:1024]
        raise HandoffError(f"cannot resolve {field} Git identity: {detail}")
    if any(re.fullmatch(r"[0-9a-f]{40}", value) is None for value in values):
        raise HandoffError(f"{field} Git identity is not exact")
    return values[0], values[1]


def _validate_capture_authorization(
    authorization: Mapping[str, Any],
    *,
    request_sha256: str,
    subject: str,
    tap_repository: str,
    contract: Mapping[str, Any],
) -> None:
    value = _mapping(authorization, "capture authorization")
    _exact_keys(
        value,
        frozenset({"schema", "kind", "common", "capture_authorization"}),
        "capture authorization",
    )
    if (
        value["schema"] != 1
        or value["kind"] != "kandelo-abi-staging-capture-override-authorization"
    ):
        raise HandoffError("capture authorization protocol is unsupported")
    common = _mapping(value["common"], "capture authorization common")
    if common.get("request_sha256") != request_sha256:
        raise HandoffError("capture authorization names a different request")
    common_subject = _mapping(common.get("subject"), "capture authorization subject")
    parsed_subject = json.loads(subject)
    expected_common_subject = {
        "kind": "formula",
        "identity": f"{tap_repository}/{parsed_subject['identity']}",
        "architecture": parsed_subject["architecture"],
    }
    if common_subject != expected_common_subject:
        raise HandoffError("capture authorization names a different exact subject")
    if common.get("guard_codes") != ["build_input_capture_incomplete"]:
        raise HandoffError("capture authorization names a different guard")
    if common.get("artifact_class") != "none" or common.get("artifact") is not None:
        raise HandoffError("pre-build capture authorization cannot guess an artifact")
    payload = _mapping(value["capture_authorization"], "capture authorization payload")
    formula = _mapping(payload.get("formula"), "capture authorization Formula")
    if formula != {
        "tap": tap_repository,
        "formula": parsed_subject["identity"],
        "architecture": parsed_subject["architecture"],
        "target_abi": contract["target"]["abi"],
        "bottle_contract_sha256": hashlib.sha256(canonical_bytes(contract)).hexdigest(),
    }:
        raise HandoffError("capture authorization Formula identity differs")
    if payload.get("guard_code") != "build_input_capture_incomplete":
        raise HandoffError("capture authorization payload guard differs")
    maintainer = _mapping(payload.get("maintainer"), "capture authorization maintainer")
    if maintainer.get("permission") not in {"maintain", "admin"}:
        raise HandoffError("capture authorization lacks maintainer authority")
    _text(maintainer.get("login"), "capture authorization maintainer login", 128)
    _text(
        maintainer.get("authorization_reference"),
        "capture authorization reference",
        4096,
    )
    _text(payload.get("justification"), "capture authorization justification", 2048)
    policy = _mapping(payload.get("policy"), "capture authorization policy")
    _integer(policy.get("policy_version"), "capture authorization policy version", positive=True)
    _digest(policy.get("policy_sha256"), "capture authorization policy")
    _integer(
        policy.get("guard_registry_version"),
        "capture authorization guard version",
        positive=True,
    )
    _digest(policy.get("guard_registry_sha256"), "capture authorization guard registry")


def _composition_roots(
    plan: Mapping[str, Any], formula_plan: Mapping[str, Any]
) -> list[str]:
    """Return every selected direct Formula root whose closure reaches this subject."""

    target_identity = _mapping(
        formula_plan.get("identity"), "composition target Formula identity"
    )
    target = (
        _text(target_identity.get("name"), "composition target Formula", 128),
        target_identity.get("architecture"),
    )
    formulae = [
        _mapping(candidate, f"composition Formula {index}")
        for index, candidate in enumerate(_sequence(plan.get("formulae"), "tap plan Formulae"))
    ]
    graph: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
    for index, candidate in enumerate(formulae):
        identity = _mapping(candidate.get("identity"), f"composition Formula identity {index}")
        subject = (
            _text(identity.get("name"), f"composition Formula name {index}", 128),
            identity.get("architecture"),
        )
        if subject[1] not in {"wasm32", "wasm64"} or subject in graph:
            raise HandoffError("tap plan composition graph identity is invalid")
        graph[subject] = tuple(
            (
                _text(dependency.get("formula"), "composition dependency", 128),
                dependency.get("architecture"),
            )
            for dependency in (
                _mapping(value, "composition dependency")
                for value in _sequence(
                    candidate.get("direct_dependencies"),
                    "composition dependencies",
                )
            )
        )
    if target not in graph:
        raise HandoffError("composition target Formula is absent from the tap graph")

    memo: dict[tuple[str, str], frozenset[tuple[str, str]]] = {}

    def closure(subject: tuple[str, str], trail: frozenset[tuple[str, str]]) -> frozenset[tuple[str, str]]:
        if subject in memo:
            return memo[subject]
        if subject in trail or subject not in graph:
            raise HandoffError("tap plan composition graph is cyclic or incomplete")
        reached = {subject}
        for dependency in graph[subject]:
            reached.update(closure(dependency, trail | {subject}))
        result = frozenset(reached)
        memo[subject] = result
        return result

    tap_repository = _mapping(plan.get("tap_source"), "tap plan source").get("repository")
    roots = set()
    for product in (
        _mapping(value, "selected composition product")
        for value in _sequence(plan.get("selected_products"), "selected products")
    ):
        for root in (
            _mapping(value, "selected composition root")
            for value in _sequence(product.get("formula_roots"), "selected Formula roots")
        ):
            if root.get("tap") != tap_repository or root.get("architecture") != target[1]:
                continue
            root_subject = (
                _text(root.get("formula"), "selected composition root Formula", 128),
                root.get("architecture"),
            )
            if target in closure(root_subject, frozenset()):
                roots.add(root_subject[0])
    if roots:
        return sorted(roots)
    if formula_plan.get("work_class") != "background":
        raise HandoffError("required Formula has no selected composition root")
    return [target[0]]


def _dependency_layer_closure(
    *,
    formulae: Sequence[Mapping[str, Any]],
    root_formula_plan: Mapping[str, Any],
    root_contract: Mapping[str, Any],
    dependency_root: Path,
) -> list[dict[str, Any]]:
    """Reconstruct every reachable dependency layer from exact tap contracts."""

    plans: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, candidate in enumerate(formulae):
        plan = _mapping(candidate, f"dependency Formula plan {index}")
        identity = _mapping(
            plan.get("identity"), f"dependency Formula identity {index}"
        )
        name = _text(identity.get("name"), f"dependency Formula name {index}", 128)
        architecture = identity.get("architecture")
        key = (name, str(architecture))
        if architecture not in {"wasm32", "wasm64"} or key in plans:
            raise HandoffError("tap plan dependency Formula identity is invalid")
        plans[key] = plan

    resolved: dict[tuple[str, str], dict[str, Any]] = {}
    visiting: set[tuple[str, str]] = set()

    def dependencies(
        plan: Mapping[str, Any], contract: Mapping[str, Any]
    ) -> list[tuple[tuple[str, str], Mapping[str, Any]]]:
        planned: dict[tuple[str, str], Mapping[str, Any]] = {}
        for raw in _sequence(
            plan.get("direct_dependencies"), "Formula plan direct dependencies"
        ):
            dependency = _mapping(raw, "Formula plan direct dependency")
            name = _text(
                dependency.get("formula"), "Formula plan dependency name", 128
            )
            architecture = dependency.get("architecture")
            key = (name, str(architecture))
            if architecture not in {"wasm32", "wasm64"} or key in planned:
                raise HandoffError(
                    "Formula plan direct dependencies are invalid or duplicated"
                )
            _digest(
                dependency.get("materialization_policy_sha256"),
                "Formula plan dependency materialization policy",
            )
            planned[key] = dependency

        contracted: dict[tuple[str, str], Mapping[str, Any]] = {}
        for raw in _sequence(
            contract.get("direct_dependencies"), "bottle contract direct dependencies"
        ):
            dependency = _mapping(raw, "bottle contract direct dependency")
            name = _text(
                dependency.get("formula"), "bottle contract dependency name", 128
            )
            architecture = dependency.get("architecture")
            key = (name, str(architecture))
            if architecture not in {"wasm32", "wasm64"} or key in contracted:
                raise HandoffError(
                    "bottle contract direct dependencies are invalid or duplicated"
                )
            contracted[key] = dependency
        if set(planned) != set(contracted):
            raise HandoffError("bottle contract dependency set differs from Formula plan")
        for key in sorted(contracted):
            dependency = contracted[key]
            if dependency.get("materialization_policy_sha256") != planned[key].get(
                "materialization_policy_sha256"
            ):
                raise HandoffError(
                    "dependency materialization policy differs from tap plan"
                )
        return [(key, contracted[key]) for key in sorted(contracted)]

    def load_child(
        key: tuple[str, str], plan: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        digest = _digest(
            plan.get("contract_sha256"),
            f"dependency bottle contract {key[0]}",
        )
        path = dependency_root / "contracts" / f"sha256-{digest}.json"
        body = _read_regular(path, f"dependency bottle contract {key[0]}", 16 * 1024 * 1024)
        if hashlib.sha256(body).hexdigest() != digest:
            raise HandoffError(
                f"content-addressed dependency contract differs for {key[0]}"
            )
        contract = load_bottle_contract(body)
        formula = _mapping(contract.get("formula"), "dependency contract Formula")
        target = _mapping(contract.get("target"), "dependency contract target")
        if formula.get("name") != key[0] or target.get("architecture") != key[1]:
            raise HandoffError(
                "dependency contract differs from its exact Formula plan identity"
            )
        return contract

    def visit(plan: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
        for key, dependency in dependencies(plan, contract):
            digest = _digest(
                dependency.get("bottle_layer_sha256"),
                f"dependency layer {key[0]}",
            )
            size = _integer(
                dependency.get("bottle_layer_bytes"),
                f"dependency layer bytes {key[0]}",
                positive=True,
            )
            path = dependency_root / "layers" / f"sha256-{digest}.tar.gz"
            body = _read_regular(path, f"dependency layer {key[0]}", 2**32)
            if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
                raise HandoffError(
                    f"dependency layer identity differs for {key[0]}"
                )
            layer = {
                "formula": key[0],
                "architecture": key[1],
                "sha256": digest,
                "bytes": size,
                "source_path": str(path.resolve(strict=True)),
            }
            prior = resolved.get(key)
            if prior is not None:
                if prior != layer:
                    raise HandoffError(
                        "dependency closure conflicts by Formula architecture"
                    )
                continue
            if key in visiting:
                raise HandoffError("dependency closure contains a cycle")
            child_plan = plans.get(key)
            if child_plan is None:
                raise HandoffError("dependency closure is absent from the tap plan")
            if len(resolved) + len(visiting) >= 256:
                raise HandoffError("dependency closure exceeds its bound")
            visiting.add(key)
            child_contract = load_child(key, child_plan)
            visit(child_plan, child_contract)
            visiting.remove(key)
            resolved[key] = layer

    visit(root_formula_plan, root_contract)
    return [resolved[key] for key in sorted(resolved)]


def prepare_build_context(
    *,
    kandelo_root: Path,
    tap_root: Path,
    request_path: Path,
    tap_plan_path: Path,
    formula_plan_path: Path,
    dependency_root: Path,
    run_path: Path,
    retry_ordinal: int,
) -> dict[str, Any]:
    request_body = _read_regular(request_path, "ABI staging request")
    request = _canonical_mapping(request_body, "ABI staging request")
    _exact_keys(
        request,
        frozenset(
            {
                "schema",
                "kind",
                "pull_request",
                "build_source",
                "target_abi",
                "requirements",
                "issuance",
                "informational_context",
            }
        ),
        "ABI staging request",
    )
    if request["schema"] != 1 or request["kind"] != "kandelo-abi-staging-request":
        raise HandoffError("ABI staging request protocol is unsupported")
    request_digest = hashlib.sha256(request_body).hexdigest()
    plan = load_tap_plan_record(_read_regular(tap_plan_path, "tap plan"))
    if plan["request_digest"] != request_digest:
        raise HandoffError("tap plan names a different exact request")
    formula_plan = _canonical_mapping(
        _read_regular(formula_plan_path, "Formula plan"), "Formula plan"
    )
    if sum(candidate == formula_plan for candidate in plan["formulae"]) != 1:
        raise HandoffError("Formula plan is not one exact member of the tap plan")
    identity = _mapping(formula_plan.get("identity"), "Formula plan identity")
    formula = _text(identity.get("name"), "Formula plan name", 128)
    architecture = identity.get("architecture")
    subject = exact_formula_subject(formula, architecture)
    contract_digest = formula_plan.get("contract_sha256")
    _digest(contract_digest, "Formula plan bottle contract")

    build_source = _mapping(request["build_source"], "request build source")
    kandelo_commit, kandelo_tree = _git_identity(kandelo_root, "Kandelo")
    if (
        build_source.get("commit") != kandelo_commit
        or build_source.get("tree") != kandelo_tree
    ):
        raise HandoffError("Kandelo checkout differs from the exact PR head")
    tap_source = _mapping(plan["tap_source"], "tap plan source")
    tap_commit, tap_tree = _git_identity(tap_root, "tap")
    if tap_source.get("commit") != tap_commit or tap_source.get("tree") != tap_tree:
        raise HandoffError("tap checkout differs from the exact tap plan")

    try:
        dependency_metadata = dependency_root.lstat()
    except OSError as error:
        raise HandoffError(f"cannot inspect dependency root: {error}") from error
    if stat.S_ISLNK(dependency_metadata.st_mode) or not stat.S_ISDIR(dependency_metadata.st_mode):
        raise HandoffError("dependency root must be a real directory")
    contract_path = dependency_root / "contracts" / f"sha256-{contract_digest}.json"
    contract_body = _read_regular(contract_path, "bottle contract", 16 * 1024 * 1024)
    if hashlib.sha256(contract_body).hexdigest() != contract_digest:
        raise HandoffError("content-addressed bottle contract digest differs")
    contract = load_bottle_contract(contract_body)
    target = _mapping(plan["target_abi"], "tap plan target ABI")
    if (
        contract["formula"]["name"] != formula
        or contract["formula"]["version"] != identity["version"]
        or contract["formula"]["revision"] != identity["revision"]
        or contract["formula"]["rebuild"] != identity["rebuild"]
        or contract["target"]["architecture"] != architecture
        or contract["target"]["abi"] != target["version"]
        or contract["target"]["snapshot_sha256"] != target["snapshot_sha256"]
    ):
        raise HandoffError("bottle contract differs from exact Formula/tap plan")

    assessment_path = dependency_root / "contracts" / f"sha256-{contract_digest}.capture.json"
    assessment = _canonical_mapping(
        _read_regular(assessment_path, "capture assessment"), "capture assessment"
    )
    try:
        validate_capture_assessment(assessment)
    except ValueError as error:
        raise HandoffError(f"capture assessment is invalid: {error}") from error
    if assessment["subject"] != subject:
        raise HandoffError("capture assessment names a different exact subject")
    authorization_digest = None
    if not assessment["complete"]:
        authorization_path = (
            dependency_root
            / "contracts"
            / f"sha256-{contract_digest}.authorization.json"
        )
        authorization_body = _read_regular(
            authorization_path, "capture authorization"
        )
        authorization = _canonical_mapping(
            authorization_body, "capture authorization"
        )
        _validate_capture_authorization(
            authorization,
            request_sha256=request_digest,
            subject=subject,
            tap_repository=tap_source["repository"],
            contract=contract,
        )
        authorization_digest = hashlib.sha256(authorization_body).hexdigest()

    layers = _dependency_layer_closure(
        formulae=plan["formulae"],
        root_formula_plan=formula_plan,
        root_contract=contract,
        dependency_root=dependency_root,
    )

    repository = _text(tap_source["repository"], "tap repository", 256)
    run = load_build_run(
        _read_regular(run_path, "build run"), expected_repository=repository
    )
    checked_retry_ordinal = _integer(retry_ordinal, "retry ordinal")
    if checked_retry_ordinal == 2**64 - 1:
        raise HandoffError("retry ordinal cannot overflow the attempt count")
    try:
        staging_policy = load_tap_staging_policy(
            tap_root / "Kandelo/staging/tap-policy.toml"
        )
        if staging_policy.tap_repository != repository:
            raise HandoffError("tap staging policy names a different repository")
        candidate_root = candidate_repository(
            staging_policy, contract["target"]["abi"], formula=formula
        )
    except ValueError as error:
        raise HandoffError(f"tap staging policy is invalid: {error}") from error
    context = {
        "schema": 1,
        "kind": "kandelo-abi-staging-build-context",
        "request_sha256": request_digest,
        "request_source": dict(build_source),
        "tap_source": dict(tap_source),
        "subject": subject,
        "formula": formula,
        "formula_identity": dict(identity),
        "architecture": architecture,
        "target_abi": contract["target"]["abi"],
        "run": run,
        "retry_ordinal": checked_retry_ordinal,
        "bottle_contract_sha256": contract_digest,
        "bottle_contract_path": str(contract_path.resolve(strict=True)),
        "capture_assessment_sha256": hashlib.sha256(
            _read_regular(assessment_path, "capture assessment")
        ).hexdigest(),
        "capture_authorization_sha256": authorization_digest,
        "dependency_layers": layers,
        "composition_roots": _composition_roots(plan, formula_plan),
        "bottle_root_url": f"https://ghcr.io/v2/{candidate_root}",
    }
    return json.loads(canonical_bytes(context))


def _copy_regular(source: Path, destination: Path, field: str) -> None:
    body = _read_regular(source, field, 2**32)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)


def _copy_canonical_json(source: Path, destination: Path, field: str) -> None:
    body = _read_regular(source, field, MAX_JSON_BYTES)
    try:
        parsed = parse_json_bytes(body, maximum_bytes=MAX_JSON_BYTES)
        canonical = canonical_bytes(parsed)
    except CanonicalJsonError as error:
        raise HandoffError(f"{field} is not unambiguous JSON: {error}") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical)


def create_context_source_custody(
    *,
    context_path: Path,
    kandelo_root: Path,
    tap_root: Path,
    output: Path,
) -> dict[str, Any]:
    context = _canonical_mapping(
        _read_regular(context_path, "build context"), "build context"
    )
    if context.get("schema") != 1 or context.get("kind") != "kandelo-abi-staging-build-context":
        raise HandoffError("build context protocol is unsupported")
    try:
        return create_source_custody(
            kandelo_root=kandelo_root,
            tap_root=tap_root,
            kandelo_source=_mapping(context.get("request_source"), "request source"),
            tap_source=_mapping(context.get("tap_source"), "tap source"),
            request_sha256=_digest(context.get("request_sha256"), "request digest"),
            subject=_subject(context.get("subject"), "build context subject"),
            output=output,
        )
    except CustodyError as error:
        raise HandoffError(f"cannot preserve exact source custody: {error}") from error


def _attempt_record(
    context: Mapping[str, Any],
    *,
    outcome: str,
    exit_code: int,
    source_capsule_sha256: str,
    source_capsule_bytes: int,
    candidate: Mapping[str, Any] | None,
    diagnostic_sha256: str,
) -> dict[str, Any]:
    formula = {
        "tap": context["tap_source"]["repository"],
        "formula": context["formula"],
        "architecture": context["architecture"],
        "target_abi": context["target_abi"],
        "bottle_contract_sha256": context["bottle_contract_sha256"],
    }
    bottle_artifact = None
    if candidate is not None:
        layer = candidate["bottle_layer"]
        bottle_artifact = {
            **layer,
            "immutable_reference": (
                "handoff:bottle.tar.gz@sha256:" + layer["sha256"]
            ),
        }
    guard_codes = [] if outcome == "success" else ["build_failed"]
    blockers = (
        []
        if outcome == "success"
        else [
            {
                "guard_code": "build_failed",
                "subject_kind": "formula",
                "subject": context["subject"],
            }
        ]
    )
    common = {
        "request_sha256": context["request_sha256"],
        "subject": {
            "kind": "formula",
            "identity": f"{context['tap_source']['repository']}/{context['formula']}",
            "architecture": context["architecture"],
        },
        "source": context["request_source"],
        "run": context["run"],
        "guard_codes": guard_codes,
        "work_state": "complete",
        "outcome": outcome,
        "artifact_class": "candidate" if candidate is not None else "none",
        "promotion_state": "eligible" if candidate is not None else "ineligible",
        "retry_state": {
            "attempts": context["retry_ordinal"] + 1,
            "eligible": False,
            "exhausted": False,
            "next_action": "none",
        },
        "blockers": blockers,
    }
    if bottle_artifact is not None:
        common["artifact"] = bottle_artifact
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-attempt",
        "common": common,
        "attempt": {
            "formula": formula,
            "source_capsule": {
                "sha256": source_capsule_sha256,
                "bytes": source_capsule_bytes,
                "immutable_reference": (
                    "handoff:source-custody@sha256:" + source_capsule_sha256
                ),
            },
            "build": {
                "runner_image": "uncredentialed-candidate",
                "command_sha256": hashlib.sha256(canonical_bytes(context)).hexdigest(),
                "result_sha256": diagnostic_sha256,
                "diagnostics": [],
            },
            "retry_ordinal": context["retry_ordinal"],
            **({"candidate": bottle_artifact} if bottle_artifact is not None else {}),
        },
    }


def assemble_handoff(
    *,
    context_path: Path,
    raw_output: Path,
    source_custody: Path,
    handoff: Path,
    exit_code: int,
) -> dict[str, Any]:
    context = _canonical_mapping(
        _read_regular(context_path, "build context"), "build context"
    )
    if context.get("schema") != 1 or context.get("kind") != "kandelo-abi-staging-build-context":
        raise HandoffError("build context protocol is unsupported")
    if exit_code < 0 or exit_code > 255:
        raise HandoffError("builder exit code is invalid")
    if handoff.exists() or handoff.is_symlink():
        if handoff.is_symlink() or not handoff.is_dir() or any(handoff.iterdir()):
            raise HandoffError("handoff output must be a new or empty real directory")
    else:
        handoff.mkdir(parents=True)
    _copy_regular(
        Path(context["bottle_contract_path"]),
        handoff / "bottle-contract.json",
        "bottle contract",
    )
    try:
        custody_manifest = validate_source_custody(
            root=source_custody,
            expected_request_sha256=context["request_sha256"],
            expected_subject=context["subject"],
            expected_kandelo_source=context["request_source"],
            expected_tap_source=context["tap_source"],
        )
    except CustodyError as error:
        raise HandoffError(
            f"source custody is invalid after candidate execution: {error}"
        ) from error
    custody_members = ["manifest.json"]
    for source in custody_manifest["sources"]:
        custody_members.extend((source["bundle"]["path"], source["tree_archive"]["path"]))
    for submodule in custody_manifest["submodules"]:
        custody_members.extend((submodule["bundle"]["path"], submodule["tree_archive"]["path"]))
    for relative in sorted(custody_members):
        _copy_regular(
            source_custody / relative,
            handoff / "source-custody" / relative,
            f"source custody {relative}",
        )
    summary_source = raw_output / "diagnostics/summary.txt"
    _copy_regular(summary_source, handoff / "diagnostics/summary.txt", "build summary")

    outcome = "success" if exit_code == 0 else "failure"
    candidate_identity = None
    if outcome == "success":
        bottles = raw_output / "bottles"
        archives = sorted(bottles.glob("*.tar.gz"))
        metadata_files = sorted(bottles.glob("*.bottle.json"))
        composition_files = sorted(bottles.glob("*.vfs-composition.json"))
        if len(archives) != 1 or len(metadata_files) != 1 or len(composition_files) != 1:
            raise HandoffError(
                "successful normal build must emit one bottle, metadata file, and VFS composition descriptor"
            )
        _copy_regular(archives[0], handoff / "bottle.tar.gz", "bottle archive")
        _copy_canonical_json(
            metadata_files[0],
            handoff / "bottle-metadata.json",
            "bottle metadata",
        )
        _copy_regular(
            composition_files[0],
            handoff / "vfs-composition-descriptor.json",
            "VFS composition descriptor",
        )
        candidate_identity = {
            "bottle_contract_sha256": context["bottle_contract_sha256"],
            "bottle_layer": _file_identity(handoff / "bottle.tar.gz"),
            "bottle_metadata": _file_identity(handoff / "bottle-metadata.json"),
            "vfs_composition_descriptor": _file_identity(
                handoff / "vfs-composition-descriptor.json"
            ),
        }
    diagnostic_sha256 = hashlib.sha256(
        (handoff / "diagnostics/summary.txt").read_bytes()
    ).hexdigest()
    result = {
        "schema": 1,
        "kind": "kandelo-abi-staging-build-result",
        "request_sha256": context["request_sha256"],
        "subject": context["subject"],
        "outcome": outcome,
        "exit_code": exit_code,
        "candidate": candidate_identity,
        "diagnostic_summary_sha256": diagnostic_sha256,
    }
    validate_build_result(result)
    (handoff / "build-result.json").write_bytes(canonical_bytes(result))
    custody_manifest_body = _read_regular(
        handoff / "source-custody/manifest.json", "source custody manifest"
    )
    source_capsule_bytes = len(custody_manifest_body) + sum(
        member["bytes"]
        for owner in (*custody_manifest["sources"], *custody_manifest["submodules"])
        for member in (owner["bundle"], owner["tree_archive"])
    )
    attempt = _attempt_record(
        context,
        outcome=outcome,
        exit_code=exit_code,
        source_capsule_sha256=custody_manifest["capsule_sha256"],
        source_capsule_bytes=source_capsule_bytes,
        candidate=candidate_identity,
        diagnostic_sha256=diagnostic_sha256,
    )
    (handoff / "attempt-record.json").write_bytes(canonical_bytes(attempt))
    write_handoff_inventory(handoff, subject=context["subject"], outcome=outcome)
    return validate_handoff(
        handoff,
        max_files=256,
        max_bytes=4_294_967_296,
        expected_request_sha256=context["request_sha256"],
        expected_subject=context["subject"],
        expected_kandelo_source=context["request_source"],
        expected_tap_source=context["tap_source"],
    )


def materialize_dependency_layers(*, context_path: Path, output: Path) -> None:
    context = _canonical_mapping(
        _read_regular(context_path, "build context"), "build context"
    )
    if context.get("schema") != 1 or context.get("kind") != "kandelo-abi-staging-build-context":
        raise HandoffError("build context protocol is unsupported")
    if output.exists() or output.is_symlink():
        raise HandoffError("declared dependency output must not already exist")
    output.mkdir(parents=True)
    for candidate in _sequence(context.get("dependency_layers"), "dependency layers"):
        layer = _mapping(candidate, "dependency layer")
        digest = _digest(layer.get("sha256"), "dependency layer digest")
        size = _integer(layer.get("bytes"), "dependency layer bytes", positive=True)
        source = Path(_text(layer.get("source_path"), "dependency layer source"))
        body = _read_regular(source, "dependency layer", 2**32)
        if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
            raise HandoffError("dependency layer changed after build planning")
        destination = output / f"{digest}.tar.gz"
        destination.write_bytes(body)


def _candidate_dependency_formula_source(
    source: bytes, *, root_url: str, architecture: str, digest: str
) -> bytes:
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HandoffError("dependency Formula is not UTF-8") from error
    if not source or b"\r" in source or not source.endswith(b"\n"):
        raise HandoffError("dependency Formula is not canonical LF text")
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line == "  bottle do\n"]
    if len(starts) != 1:
        raise HandoffError("dependency Formula must contain one canonical bottle block")
    start = starts[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index] == "  end\n"),
        None,
    )
    if end is None:
        raise HandoffError("dependency Formula bottle block is unterminated")
    root_pattern = re.compile(r'    root_url "https?://[^"\n]+"\n')
    root_lines = [
        index for index in range(start + 1, end) if root_pattern.fullmatch(lines[index])
    ]
    if root_lines != [start + 1]:
        raise HandoffError("dependency Formula lacks one canonical bottle root")
    selected_pattern = re.compile(
        r'(    sha256 cellar: (?:"[^"\n]+"|:any(?:_skip_relocation)?), '
        + re.escape(architecture)
        + r'_kandelo: ")([0-9a-f]{64})("\n)'
    )
    selected = [
        (index, selected_pattern.fullmatch(lines[index]))
        for index in range(start + 1, end)
        if selected_pattern.fullmatch(lines[index]) is not None
    ]
    if len(selected) != 1:
        raise HandoffError(
            "dependency Formula lacks one bottle digest for the selected architecture"
        )
    lines[root_lines[0]] = f'    root_url "{root_url}"\n'
    selected_index, selected_match = selected[0]
    if selected_match is None:
        raise HandoffError("dependency Formula bottle digest match disappeared")
    lines[selected_index] = (
        selected_match.group(1) + digest + selected_match.group(3)
    )
    prepared = "".join(lines).encode("utf-8")
    try:
        if normalize_formula_source(prepared) != normalize_formula_source(source):
            raise HandoffError(
                "candidate dependency preparation changed Formula recipe bytes"
            )
    except FormulaInventoryError as error:
        raise HandoffError(f"dependency Formula bottle block is invalid: {error}") from error
    return prepared


def prepare_dependency_tap(
    *, context_path: Path, tap_root: Path, output: Path
) -> Path:
    """Create one clean deterministic checkout with exact dependency bottles."""

    context = _canonical_mapping(
        _read_regular(context_path, "build context"), "build context"
    )
    if (
        context.get("schema") != 1
        or context.get("kind") != "kandelo-abi-staging-build-context"
    ):
        raise HandoffError("build context protocol is unsupported")
    source = _mapping(context.get("tap_source"), "tap source")
    repository = _text(source.get("repository"), "tap repository", 256)
    if REPOSITORY.fullmatch(repository) is None or repository != repository.lower():
        raise HandoffError("tap repository is not a normalized repository identity")
    source_commit = _text(source.get("commit"), "tap source commit", 40)
    source_tree = _text(source.get("tree"), "tap source tree", 64)
    if GIT_SHA.fullmatch(source_commit) is None:
        raise HandoffError("tap source commit is invalid")
    target_abi = _integer(context.get("target_abi"), "target ABI")
    if target_abi <= 0:
        raise HandoffError("target ABI must be positive")
    root_url = f"https://ghcr.io/v2/{repository}-abi-{target_abi}-candidates"
    tap_commit, tap_tree = _git_identity(tap_root, "tap")
    if tap_commit != source_commit or tap_tree != source_tree:
        raise HandoffError("tap checkout differs from the exact build context")
    if output.exists() or output.is_symlink():
        raise HandoffError("prepared dependency tap output must not already exist")
    clone = subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--quiet",
            "--no-hardlinks",
            str(tap_root),
            str(output),
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if clone.returncode != 0:
        detail = clone.stderr.decode("utf-8", errors="replace")[:4096]
        raise HandoffError(f"cannot clone exact dependency tap: {detail}")
    try:
        dependencies = _sequence(
            context.get("dependency_layers"), "dependency layers"
        )
        changed: list[str] = []
        prior: tuple[str, str] | None = None
        for raw in dependencies:
            layer = _mapping(raw, "dependency layer")
            formula = _text(layer.get("formula"), "dependency Formula", 128)
            architecture = layer.get("architecture")
            digest = _digest(layer.get("sha256"), "dependency layer digest")
            key = (formula, str(architecture))
            if (
                STABLE_ID.fullmatch(formula) is None
                or architecture not in {"wasm32", "wasm64"}
                or (prior is not None and key <= prior)
            ):
                raise HandoffError(
                    "dependency layers must be normalized, sorted, and unique"
                )
            prior = key
            relative = f"Formula/{formula}.rb"
            path = output / relative
            try:
                metadata = path.lstat()
            except OSError as error:
                raise HandoffError(
                    f"cannot inspect dependency Formula {formula}: {error}"
                ) from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise HandoffError(
                    f"dependency Formula {formula} must be a regular non-symlink file"
                )
            prepared = _candidate_dependency_formula_source(
                path.read_bytes(),
                root_url=root_url,
                architecture=str(architecture),
                digest=digest,
            )
            path.write_bytes(prepared)
            changed.append(relative)
        if changed:
            subprocess.run(
                ["git", "-C", str(output), "add", "--", *changed], check=True
            )
            tree = subprocess.run(
                ["git", "-C", str(output), "write-tree"],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.decode("ascii").strip()
            commit_environment = {
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Kandelo ABI staging",
                "GIT_AUTHOR_EMAIL": "abi-staging@kandelo.invalid",
                "GIT_COMMITTER_NAME": "Kandelo ABI staging",
                "GIT_COMMITTER_EMAIL": "abi-staging@kandelo.invalid",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
            }
            prepared_commit = subprocess.run(
                [
                    "git",
                    "-C",
                    str(output),
                    "commit-tree",
                    tree,
                    "-p",
                    source_commit,
                    "-m",
                    "Prepare exact ABI candidate dependencies",
                ],
                check=True,
                env=commit_environment,
                stdout=subprocess.PIPE,
            ).stdout.decode("ascii").strip()
            subprocess.run(
                ["git", "-C", str(output), "reset", "--hard", prepared_commit],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        status = subprocess.run(
            ["git", "-C", str(output), "status", "--short", "--untracked-files=all"],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        if status:
            raise HandoffError("prepared dependency tap is not clean")
        checkout_commit, _checkout_tree = _git_identity(output, "prepared dependency tap")
        if changed and checkout_commit == source_commit:
            raise HandoffError("prepared dependency tap commit did not change")
        if not changed and checkout_commit != source_commit:
            raise HandoffError("dependency-free tap checkout changed")
        return output.resolve(strict=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise HandoffError(f"cannot prepare exact dependency tap: {error}") from error


def load_handoff_validation_expectations(
    *, request_path: Path, tap_plan_path: Path, formula_plan_path: Path
) -> dict[str, Any]:
    request_body = _read_regular(request_path, "ABI staging request")
    request = _canonical_mapping(request_body, "ABI staging request")
    if request.get("schema") != 1 or request.get("kind") != "kandelo-abi-staging-request":
        raise HandoffError("ABI staging request protocol is unsupported")
    request_sha256 = hashlib.sha256(request_body).hexdigest()
    tap_plan = load_tap_plan_record(_read_regular(tap_plan_path, "tap plan"))
    if tap_plan["request_digest"] != request_sha256:
        raise HandoffError("tap plan names a different exact request")
    formula_plan = _canonical_mapping(
        _read_regular(formula_plan_path, "Formula plan"), "Formula plan"
    )
    if sum(candidate == formula_plan for candidate in tap_plan["formulae"]) != 1:
        raise HandoffError("Formula plan is not one exact member of the tap plan")
    identity = _mapping(formula_plan.get("identity"), "Formula plan identity")
    return {
        "request_sha256": request_sha256,
        "subject": exact_formula_subject(identity.get("name"), identity.get("architecture")),
        "kandelo_source": _mapping(request.get("build_source"), "request build source"),
        "tap_source": _mapping(tap_plan.get("tap_source"), "tap plan source"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.abi_staging.handoff")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-build")
    prepare.add_argument("--kandelo-root", required=True)
    prepare.add_argument("--tap-root", required=True)
    prepare.add_argument("--request", required=True)
    prepare.add_argument("--tap-plan", required=True)
    prepare.add_argument("--formula-plan", required=True)
    prepare.add_argument("--dependency-root", required=True)
    prepare.add_argument("--run", required=True)
    prepare.add_argument("--retry-ordinal", required=True, type=int)
    prepare.add_argument("--out", required=True)
    composition = commands.add_parser("prepare-composition")
    composition.add_argument("--context", required=True)
    composition.add_argument("--bottle", required=True)
    composition.add_argument("--metadata", required=True)
    composition.add_argument("--guest-layout", required=True)
    composition.add_argument("--out", required=True)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("--context", required=True)
    assemble.add_argument("--raw-output", required=True)
    assemble.add_argument("--source-custody", required=True)
    assemble.add_argument("--handoff", required=True)
    assemble.add_argument("--exit-code", required=True, type=int)
    custody = commands.add_parser("create-custody")
    custody.add_argument("--context", required=True)
    custody.add_argument("--kandelo-root", required=True)
    custody.add_argument("--tap-root", required=True)
    custody.add_argument("--out", required=True)
    materialize = commands.add_parser("materialize-dependencies")
    materialize.add_argument("--context", required=True)
    materialize.add_argument("--out", required=True)
    prepare_tap = commands.add_parser("prepare-dependency-tap")
    prepare_tap.add_argument("--context", required=True)
    prepare_tap.add_argument("--tap-root", required=True)
    prepare_tap.add_argument("--out", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--handoff", required=True)
    validate.add_argument("--request", required=True)
    validate.add_argument("--tap-plan", required=True)
    validate.add_argument("--formula-plan", required=True)
    validate.add_argument("--max-files", required=True, type=int)
    validate.add_argument("--max-bytes", required=True, type=int)
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        if args.command == "prepare-build":
            context = prepare_build_context(
                kandelo_root=Path(args.kandelo_root).resolve(strict=True),
                tap_root=Path(args.tap_root).resolve(strict=True),
                request_path=Path(args.request).resolve(strict=True),
                tap_plan_path=Path(args.tap_plan).resolve(strict=True),
                formula_plan_path=Path(args.formula_plan).resolve(strict=True),
                dependency_root=Path(args.dependency_root).resolve(strict=True),
                run_path=Path(args.run).resolve(strict=True),
                retry_ordinal=args.retry_ordinal,
            )
            Path(args.out).write_bytes(canonical_bytes(context))
        elif args.command == "prepare-composition":
            context = _canonical_mapping(
                _read_regular(Path(args.context).resolve(strict=True), "build context"),
                "build context",
            )
            prepared = prepare_composition_input(
                context=context,
                bottle_body=_read_regular(
                    Path(args.bottle).resolve(strict=True),
                    "composition bottle",
                    2 * 1024**3,
                ),
                metadata_body=_read_regular(
                    Path(args.metadata).resolve(strict=True),
                    "composition bottle metadata",
                ),
                guest_layout_body=_read_regular(
                    Path(args.guest_layout).resolve(strict=True),
                    "composition guest layout",
                    64 * 1024,
                ),
            )
            Path(args.out).write_bytes(canonical_bytes(prepared))
        elif args.command == "assemble":
            assemble_handoff(
                context_path=Path(args.context).resolve(strict=True),
                raw_output=Path(args.raw_output).resolve(strict=True),
                source_custody=Path(args.source_custody).resolve(strict=True),
                handoff=Path(args.handoff).resolve(strict=False),
                exit_code=args.exit_code,
            )
        elif args.command == "create-custody":
            create_context_source_custody(
                context_path=Path(args.context).resolve(strict=True),
                kandelo_root=Path(args.kandelo_root).resolve(strict=True),
                tap_root=Path(args.tap_root).resolve(strict=True),
                output=Path(args.out).resolve(strict=False),
            )
        elif args.command == "materialize-dependencies":
            materialize_dependency_layers(
                context_path=Path(args.context).resolve(strict=True),
                output=Path(args.out).resolve(strict=False),
            )
        elif args.command == "prepare-dependency-tap":
            prepare_dependency_tap(
                context_path=Path(args.context).resolve(strict=True),
                tap_root=Path(args.tap_root).resolve(strict=True),
                output=Path(args.out).resolve(strict=False),
            )
        else:
            expectations = load_handoff_validation_expectations(
                request_path=Path(args.request).resolve(strict=True),
                tap_plan_path=Path(args.tap_plan).resolve(strict=True),
                formula_plan_path=Path(args.formula_plan).resolve(strict=True),
            )
            validated = validate_handoff(
                Path(args.handoff).resolve(strict=True),
                max_files=args.max_files,
                max_bytes=args.max_bytes,
                expected_request_sha256=expectations["request_sha256"],
                expected_subject=expectations["subject"],
                expected_kandelo_source=expectations["kandelo_source"],
                expected_tap_source=expectations["tap_source"],
            )
            print(canonical_bytes(validated).decode("utf-8"), end="")
        return 0
    except (HandoffError, OSError, ValueError) as error:
        print(f"abi-staging handoff {args.command}: {error}", file=os.sys.stderr)
        return 1


def build_miniature_build_result_fixture(
    *,
    request_sha256: str = "a" * 64,
    subject: str | None = None,
    outcome: str = "success",
    root: Path | None = None,
) -> dict[str, Any]:
    exact_subject = subject or exact_formula_subject("mini-tool", "wasm32")
    if root is None:
        contract = {"sha256": "b" * 64, "bytes": 128}
        bottle = {"sha256": "c" * 64, "bytes": 256}
        metadata = {"sha256": "d" * 64, "bytes": 64}
        composition = {"sha256": "e" * 64, "bytes": 128}
        diagnostic_digest = "f" * 64
    else:
        contract = _file_identity(root / "bottle-contract.json")
        bottle = _file_identity(root / "bottle.tar.gz") if outcome == "success" else None
        metadata = _file_identity(root / "bottle-metadata.json") if outcome == "success" else None
        composition = (
            _file_identity(root / "vfs-composition-descriptor.json")
            if outcome == "success"
            else None
        )
        diagnostic_digest = hashlib.sha256(
            (root / "diagnostics/summary.txt").read_bytes()
        ).hexdigest()
    result = {
        "schema": 1,
        "kind": "kandelo-abi-staging-build-result",
        "request_sha256": request_sha256,
        "subject": exact_subject,
        "outcome": outcome,
        "exit_code": 0 if outcome == "success" else 1,
        "candidate": (
            {
                "bottle_contract_sha256": contract["sha256"],
                "bottle_layer": bottle,
                "bottle_metadata": metadata,
                "vfs_composition_descriptor": composition,
            }
            if outcome == "success"
            else None
        ),
        "diagnostic_summary_sha256": diagnostic_digest,
    }
    validate_build_result(result)
    return result


def build_miniature_handoff_inventory_fixture() -> dict[str, Any]:
    paths = [
        ("attempt-record.json", "attempt-record", "1", 64),
        ("bottle-contract.json", "bottle-contract", "2", 128),
        ("bottle-metadata.json", "bottle-metadata", "3", 64),
        ("bottle.tar.gz", "bottle-layer", "4", 256),
        ("build-result.json", "build-result", "5", 128),
        ("diagnostics/summary.txt", "diagnostic-summary", "6", 32),
        ("source-custody/kandelo-tree.tar", "source-custody-tree", "7", 256),
        ("source-custody/kandelo.bundle", "source-custody-bundle", "8", 256),
        ("source-custody/manifest.json", "source-custody-manifest", "9", 128),
        ("source-custody/tap-tree.tar", "source-custody-tree", "a", 256),
        ("source-custody/tap.bundle", "source-custody-bundle", "b", 256),
        (
            "vfs-composition-descriptor.json",
            "vfs-composition-descriptor",
            "c",
            128,
        ),
    ]
    inventory = {
        "schema": 1,
        "kind": "kandelo-abi-staging-build-handoff-inventory",
        "subject": exact_formula_subject("mini-tool", "wasm32"),
        "outcome": "success",
        "files": [
            {"path": path, "role": role, "sha256": character * 64, "bytes": size}
            for path, role, character, size in sorted(paths)
        ],
    }
    validate_handoff_inventory(inventory)
    return inventory


if __name__ == "__main__":
    raise SystemExit(main())
