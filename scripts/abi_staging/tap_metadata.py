"""Strict current-ABI state and generated tap metadata projections.

The checked-in metadata index selects the active legacy Formula projection.
Unselected sidecars and detached Formula bottle blocks are inventoried so they
cannot disappear silently, but they are not promoted to current authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
from types import MappingProxyType
from typing import Any, Literal

from .canonical import CanonicalJsonError, canonical_bytes, canonical_sha256, parse_canonical_bytes
from .bottle_link import BottleLinkError, link_manifest_bytes, validate_link_manifest
from .formula_inventory import (
    FormulaInventoryError,
    _bottle_span,
    _decode_formula,
    normalize_formula_source,
)


MAX_POLICY_BYTES = 64 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_METADATA_BYTES = 32 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(
    r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*/[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$"
)
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,127}$")
ABI_SUFFIX = re.compile(r"(?:^|[._-])abi[0-9]+(?:$|[._-])")
TOP_METADATA_KEYS = frozenset(
    {
        "generated_at",
        "generator",
        "kandelo_abi",
        "kandelo_commit",
        "kandelo_repository",
        "packages",
        "release_tag",
        "schema",
        "tap_commit",
        "tap_name",
        "tap_repository",
    }
)
PACKAGE_KEYS = frozenset(
    {
        "bottle_rebuild",
        "bottles",
        "dependencies",
        "formula_metadata",
        "formula_path",
        "formula_revision",
        "full_name",
        "name",
        "version",
    }
)
SIDECAR_KEYS = frozenset(
    (PACKAGE_KEYS - {"formula_metadata"})
    | {
        "kandelo_abi",
        "schema",
        "source_metadata",
        "tap_commit",
        "tap_name",
        "tap_repository",
    }
)


class TapMetadataError(ValueError):
    """Raised when protected tap policy or metadata is ambiguous."""


class TapMetadataWriteError(TapMetadataError):
    """A protected contents-only write failed one registered CAS boundary."""

    def __init__(self, message: str, *, guard_code: str) -> None:
        super().__init__(message)
        if guard_code not in {"tap_source_drift", "metadata_cas_conflict"}:
            raise ValueError("tap metadata write guard is unsupported")
        self.guard_code = guard_code


@dataclass(frozen=True)
class PromotionPolicyV1:
    schema: int
    kind: Literal["kandelo-abi-staging-promotion-policy"]
    version: int
    tap_repository: str
    kandelo_repository: str
    historical_branch_prefix: str
    require_branch_protection: bool
    canonical_repository_prefix: str
    require_anonymous_readback: bool
    allow_independent_formula_promotion: bool
    allow_global_completion_gate: bool


@dataclass(frozen=True)
class PromotionActivationV1:
    schema: int
    kind: Literal["kandelo-abi-staging-promotion-activation"]
    mode: Literal["disabled", "observe", "active"]


@dataclass(frozen=True)
class ManagedAbiActivationV1:
    request_digest: str
    merged_pull_request: Mapping[str, object]
    merge_commit: str
    prior_abi: int
    prior_branch: str
    abi_history_record_digest: str


@dataclass(frozen=True)
class AbiStateV1:
    schema: int
    kind: Literal["kandelo-homebrew-abi-state"]
    current_abi: int
    current_snapshot_sha256: str
    activation: ManagedAbiActivationV1 | None


@dataclass(frozen=True)
class TapMetadataPatchV1:
    operation: Literal["successor-activation", "formula-metadata"]
    expected_main_commit: str
    expected_main_tree: str
    allowed_paths: tuple[str, ...]
    expected_files_sha256: Mapping[str, str | None]
    files: Mapping[str, bytes]


@dataclass(frozen=True)
class FormulaMetadataUpdateV1:
    formula: str
    architecture: str
    expected_main_commit: str
    expected_normalized_formula_sha256: str
    expected_generated_metadata_sha256: str
    allowed_paths: tuple[str, ...]
    link_manifest_path: str
    link_manifest_sha256: str
    canonical_manifest_digest: str
    bottle_layer_sha256: str
    bottle_layer_bytes: int
    target_abi: int


@dataclass(frozen=True)
class PromotedBottleMetadataV1:
    formula: str
    architecture: str
    version: str
    revision: int
    rebuild: int
    canonical_root_url: str
    cellar: str
    built_by: str
    built_from: Mapping[str, str]
    link_manifest: Mapping[str, object]


@dataclass(frozen=True)
class TapMetadataWriteResultV1:
    status: Literal["committed", "already-landed"]
    source: Mapping[str, str]
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class RecoveredFormulaMetadataCommitV1:
    base_source: Mapping[str, str]
    landed_source: Mapping[str, str]
    update: FormulaMetadataUpdateV1
    patch: TapMetadataPatchV1


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise TapMetadataError(
            f"{field} fields changed: missing={sorted(expected - actual)!r} "
            f"extra={sorted(actual - expected)!r}"
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TapMetadataError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TapMetadataError(f"{field} must be an array")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise TapMetadataError(f"{field} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise TapMetadataError(f"{field} is not UTF-8") from error
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise TapMetadataError(f"{field} is outside its string bound")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**32 - 1:
        raise TapMetadataError(f"{field} must be a bounded positive integer")
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**32 - 1:
        raise TapMetadataError(f"{field} must be a bounded nonnegative integer")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise TapMetadataError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _git_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or GIT_SHA.fullmatch(value) is None:
        raise TapMetadataError(f"{field} is not a full lowercase Git SHA")
    return value


def _repository(value: Any, field: str) -> str:
    checked = _text(value, field, 255)
    if REPOSITORY.fullmatch(checked) is None:
        raise TapMetadataError(f"{field} is not an owner/name repository")
    return checked


def _stable_id(value: Any, field: str) -> str:
    checked = _text(value, field, 128)
    if STABLE_ID.fullmatch(checked) is None:
        raise TapMetadataError(f"{field} is not a stable identifier")
    if ABI_SUFFIX.search(checked) is not None:
        raise TapMetadataError(f"{field} must remain ABI-neutral")
    return checked


def _read_regular(path: Path, field: str, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise TapMetadataError(f"cannot inspect {field}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TapMetadataError(f"{field} must be a regular non-symlink file")
    if not 1 <= metadata.st_size <= maximum:
        raise TapMetadataError(f"{field} is outside its byte bound")
    try:
        body = path.read_bytes()
    except OSError as error:
        raise TapMetadataError(f"cannot read {field}: {error}") from error
    if len(body) != metadata.st_size:
        raise TapMetadataError(f"{field} changed while reading")
    return body


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TapMetadataError(f"JSON contains duplicate field {key!r}")
        result[key] = value
    return result


def _load_json(path: Path, field: str) -> Mapping[str, Any]:
    body = _read_regular(path, field, MAX_METADATA_BYTES)
    try:
        value = json.loads(body, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, TapMetadataError) as error:
        raise TapMetadataError(f"{field} is invalid JSON: {error}") from error
    return _mapping(value, field)


def _load_toml(path: Path, field: str) -> Mapping[str, Any]:
    body = _read_regular(path, field, MAX_POLICY_BYTES)
    try:
        value = tomllib.loads(body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise TapMetadataError(f"{field} is invalid TOML: {error}") from error
    return _mapping(value, field)


def _git_bytes(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise TapMetadataError(f"cannot inspect tap Git source: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[:4096]
        raise TapMetadataError(f"cannot inspect tap Git source: {detail}")
    return result.stdout


def _git(root: Path, *arguments: str) -> str:
    try:
        return _git_bytes(root, *arguments).decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise TapMetadataError("tap Git output is not UTF-8") from error


def _checked_source(root: Path, source: Mapping[str, Any]) -> dict[str, str]:
    checked = _mapping(source, "tap main source")
    _exact_keys(
        checked,
        frozenset({"repository", "commit", "tree"}),
        "tap main source",
    )
    result = {
        "repository": _repository(checked["repository"], "tap main repository"),
        "commit": _git_sha(checked["commit"], "tap main commit"),
        "tree": _git_sha(checked["tree"], "tap main tree"),
    }
    actual_root = Path(_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if actual_root != root:
        raise TapMetadataError("tap metadata root differs from its Git root")
    if (
        _git(root, "rev-parse", "HEAD") != result["commit"]
        or _git(root, "rev-parse", "HEAD^{tree}") != result["tree"]
    ):
        raise TapMetadataError("tap main moved from the expected commit/tree")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TapMetadataError("tap metadata checkout has an unexpected file change")
    return result


def _pretty_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _without_bottle_block(source: bytes) -> bytes:
    try:
        lines = _decode_formula(source).splitlines(keepends=True)
        span = _bottle_span(lines)
    except FormulaInventoryError as error:
        raise TapMetadataError(f"Formula bottle removal is unsafe: {error}") from error
    if span is None:
        return source
    start, end = span
    removal_start = start - 1 if start > 0 and lines[start - 1] == "\n" else start
    del lines[removal_start : end + 1]
    return "".join(lines).encode("utf-8")


_BOTTLE_SELECTION_FIELDS = frozenset(
    {
        "url",
        "sha256",
        "bytes",
        "cache_key_sha",
        "link_manifest",
        "fallback_url",
        "fallback_sha256",
        "fallback_bytes",
        "fallback_cache_key_sha",
        "fallback_link_manifest",
        "fallback_built_at",
    }
)


def _pending_bottle(
    value: Any,
    *,
    target_abi: int,
    kandelo_commit: str,
    tap_commit: str,
    normalized_formula_sha256: str,
    field: str,
) -> dict[str, Any]:
    bottle = dict(_mapping(value, field))
    for key in _BOTTLE_SELECTION_FIELDS:
        bottle.pop(key, None)
    bottle["status"] = "pending"
    bottle["kandelo_abi"] = target_abi
    built_from = _mapping(bottle.get("built_from"), f"{field} build source")
    _exact_keys(
        built_from,
        frozenset(
            {
                "formula_sha256",
                "kandelo_commit",
                "kandelo_repository",
                "tap_commit",
                "tap_repository",
            }
        ),
        f"{field} build source",
    )
    bottle["built_from"] = {
        **dict(built_from),
        "formula_sha256": normalized_formula_sha256,
        "kandelo_commit": kandelo_commit,
        "tap_commit": tap_commit,
    }
    return bottle


def load_promotion_policy(path: Path) -> PromotionPolicyV1:
    value = _load_toml(path, "promotion policy")
    fields = frozenset(PromotionPolicyV1.__dataclass_fields__)
    _exact_keys(value, fields, "promotion policy")
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-promotion-policy":
        raise TapMetadataError("promotion policy protocol is unsupported")
    if value["version"] != 1:
        raise TapMetadataError("promotion policy version is unsupported")
    tap_repository = _repository(value["tap_repository"], "promotion tap repository")
    kandelo_repository = _repository(
        value["kandelo_repository"], "promotion Kandelo repository"
    )
    prefix = _text(value["canonical_repository_prefix"], "canonical repository prefix", 128)
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]*-abi-", prefix) is None:
        raise TapMetadataError("canonical repository prefix must be explicitly ABI-qualified")
    if value["historical_branch_prefix"] != "abi/":
        raise TapMetadataError("historical branch prefix must be exactly abi/")
    required = {
        "require_branch_protection": True,
        "require_anonymous_readback": True,
        "allow_independent_formula_promotion": True,
        "allow_global_completion_gate": False,
    }
    for field, expected in required.items():
        if value[field] is not expected:
            raise TapMetadataError(f"promotion policy weakens required {field}")
    return PromotionPolicyV1(
        schema=1,
        kind="kandelo-abi-staging-promotion-policy",
        version=1,
        tap_repository=tap_repository,
        kandelo_repository=kandelo_repository,
        historical_branch_prefix="abi/",
        require_branch_protection=True,
        canonical_repository_prefix=prefix,
        require_anonymous_readback=True,
        allow_independent_formula_promotion=True,
        allow_global_completion_gate=False,
    )


def load_promotion_activation(path: Path) -> PromotionActivationV1:
    value = _load_toml(path, "promotion activation")
    _exact_keys(value, frozenset({"schema", "kind", "mode"}), "promotion activation")
    if value["schema"] != 1 or value["kind"] != "kandelo-abi-staging-promotion-activation":
        raise TapMetadataError("promotion activation protocol is unsupported")
    mode = value["mode"]
    if mode not in {"disabled", "observe", "active"}:
        raise TapMetadataError("promotion activation mode is unsupported")
    return PromotionActivationV1(
        schema=1,
        kind="kandelo-abi-staging-promotion-activation",
        mode=mode,
    )


def _managed_activation(value: Any, current_abi: int) -> ManagedAbiActivationV1:
    activation = _mapping(value, "managed ABI activation")
    _exact_keys(
        activation,
        frozenset(
            {
                "request_digest",
                "merged_pull_request",
                "merge_commit",
                "prior_abi",
                "prior_branch",
                "abi_history_record_digest",
            }
        ),
        "managed ABI activation",
    )
    request_digest = _digest(activation["request_digest"], "activation request")
    history_digest = _digest(
        activation["abi_history_record_digest"], "activation history record"
    )
    merge_commit = _git_sha(activation["merge_commit"], "activation merge commit")
    prior_abi = _nonnegative_integer(activation["prior_abi"], "activation prior ABI")
    if current_abi != prior_abi + 1:
        raise TapMetadataError("managed activation ABI transition is not N to N+1")
    prior_branch = _text(activation["prior_branch"], "activation prior branch", 128)
    if prior_branch != f"abi/{prior_abi}":
        raise TapMetadataError("managed activation prior branch is not exact")
    merged = _mapping(activation["merged_pull_request"], "activation merged pull request")
    _exact_keys(
        merged,
        frozenset({"repository", "number", "head", "merge_commit"}),
        "activation merged pull request",
    )
    checked_merged = {
        "repository": _repository(merged["repository"], "activation PR repository"),
        "number": _positive_integer(merged["number"], "activation PR number"),
        "head": _git_sha(merged["head"], "activation PR head"),
        "merge_commit": _git_sha(merged["merge_commit"], "activation PR merge commit"),
    }
    if checked_merged["merge_commit"] != merge_commit:
        raise TapMetadataError("activation merge identities disagree")
    return ManagedAbiActivationV1(
        request_digest=request_digest,
        merged_pull_request=MappingProxyType(checked_merged),
        merge_commit=merge_commit,
        prior_abi=prior_abi,
        prior_branch=prior_branch,
        abi_history_record_digest=history_digest,
    )


def load_abi_state(path: Path) -> AbiStateV1:
    body = _read_regular(path, "ABI state", MAX_STATE_BYTES)
    try:
        value = parse_canonical_bytes(body, maximum_bytes=MAX_STATE_BYTES)
    except CanonicalJsonError as error:
        raise TapMetadataError(f"ABI state is invalid: {error}") from error
    _exact_keys(
        value,
        frozenset(
            {"schema", "kind", "current_abi", "current_snapshot_sha256", "activation"}
        ),
        "ABI state",
    )
    if value["schema"] != 1 or value["kind"] != "kandelo-homebrew-abi-state":
        raise TapMetadataError("ABI state protocol is unsupported")
    current_abi = _positive_integer(value["current_abi"], "current ABI")
    snapshot = _digest(value["current_snapshot_sha256"], "current ABI snapshot")
    activation = (
        None
        if value["activation"] is None
        else _managed_activation(value["activation"], current_abi)
    )
    return AbiStateV1(
        schema=1,
        kind="kandelo-homebrew-abi-state",
        current_abi=current_abi,
        current_snapshot_sha256=snapshot,
        activation=activation,
    )


def _formula_bottle_projection(path: Path) -> dict[str, Any]:
    source = _read_regular(path, f"Formula {path.name}", MAX_METADATA_BYTES)
    name = _stable_id(path.stem, f"Formula name {path.name}")
    try:
        lines = _decode_formula(source).splitlines(keepends=True)
        span = _bottle_span(lines)
        normalized = normalize_formula_source(source)
    except FormulaInventoryError as error:
        raise TapMetadataError(f"Formula projection is invalid for {name}: {error}") from error
    bottle: dict[str, Any] | None = None
    if span is not None:
        start, end = span
        root_url = ""
        rebuild = 0
        architectures: list[dict[str, Any]] = []
        for line in lines[start + 1 : end]:
            root = re.fullmatch(r'    root_url "([^"\n]+)"\n', line)
            if root is not None:
                root_url = root.group(1)
                continue
            rebuilt = re.fullmatch(r"    rebuild ([1-9][0-9]*)\n", line)
            if rebuilt is not None:
                rebuild = int(rebuilt.group(1))
                continue
            sha = re.fullmatch(
                r'    sha256 cellar: ("[^"\n]+"|:any(?:_skip_relocation)?), '
                r'(wasm(?:32|64))_kandelo: "([0-9a-f]{64})"\n',
                line,
            )
            if sha is not None:
                architectures.append(
                    {"architecture": sha.group(2), "cellar": sha.group(1), "sha256": sha.group(3)}
                )
        bottle = {
            "root_url": root_url,
            "rebuild": rebuild,
            "architectures": architectures,
        }
    return {
        "name": name,
        "path": f"Formula/{path.name}",
        "normalized_formula_sha256": hashlib.sha256(normalized).hexdigest(),
        "bottle": bottle,
    }


def _validate_active_bottle(value: Any, *, current_abi: int, field: str) -> dict[str, Any]:
    bottle = _mapping(value, field)
    arch = bottle.get("arch")
    if arch not in {"wasm32", "wasm64"}:
        raise TapMetadataError(f"{field} architecture is unsupported")
    if bottle.get("bottle_tag") != f"{arch}_kandelo":
        raise TapMetadataError(f"{field} platform tag is not ABI-neutral")
    if bottle.get("kandelo_abi") != current_abi:
        raise TapMetadataError(f"{field} silently serves a different ABI")
    status = bottle.get("status")
    if status == "success":
        _digest(bottle.get("sha256"), f"{field} SHA-256")
        _positive_integer(bottle.get("bytes"), f"{field} bytes")
        _text(bottle.get("url"), f"{field} URL", 4096)
    elif status == "pending":
        selected = sorted(_BOTTLE_SELECTION_FIELDS.intersection(bottle))
        if selected:
            raise TapMetadataError(
                f"{field} pending bottle retains selectable fields {selected!r}"
            )
    else:
        raise TapMetadataError(f"{field} status is not a current selection state")
    return dict(bottle)


def _validate_sidecar(
    value: Any,
    *,
    name: str,
    current_abi: int | None,
    field: str,
) -> Mapping[str, Any]:
    sidecar = _mapping(value, field)
    _exact_keys(sidecar, SIDECAR_KEYS, field)
    if sidecar["schema"] != 1 or sidecar["name"] != name:
        raise TapMetadataError(f"{field} identity drifted")
    _stable_id(name, f"{field} Formula name")
    if sidecar["formula_path"] != f"Formula/{name}.rb":
        raise TapMetadataError(f"{field} Formula path drifted")
    if sidecar["full_name"] != f"kandelo-dev/tap-core/{name}":
        raise TapMetadataError(f"{field} full name drifted")
    if current_abi is not None and sidecar["kandelo_abi"] != current_abi:
        raise TapMetadataError(f"{field} silently serves a different ABI")
    _nonnegative_integer(sidecar["formula_revision"], f"{field} revision")
    _nonnegative_integer(sidecar["bottle_rebuild"], f"{field} rebuild")
    bottles = list(_sequence(sidecar["bottles"], f"{field} bottles"))
    if not bottles:
        raise TapMetadataError(f"{field} has no bottle projection")
    if current_abi is not None:
        checked = [
            _validate_active_bottle(
                bottle,
                current_abi=current_abi,
                field=f"{field} bottle {index}",
            )
            for index, bottle in enumerate(bottles)
        ]
        architectures = [item["arch"] for item in checked]
        if architectures != sorted(set(architectures)):
            raise TapMetadataError(f"{field} bottle architectures are not sorted and unique")
    return sidecar


def check_tap_metadata(tap_root: Path) -> dict[str, Any]:
    root = tap_root.resolve(strict=True)
    policy = load_promotion_policy(root / "Kandelo/staging/promotion-policy.toml")
    activation = load_promotion_activation(
        root / "Kandelo/staging/promotion-activation.toml"
    )
    state = load_abi_state(root / "Kandelo/abi-state.json")
    metadata = _load_json(root / "Kandelo/metadata.json", "top-level Formula metadata")
    _exact_keys(metadata, TOP_METADATA_KEYS, "top-level Formula metadata")
    if metadata["schema"] != 1:
        raise TapMetadataError("top-level Formula metadata schema is unsupported")
    if metadata["tap_repository"] != policy.tap_repository:
        raise TapMetadataError("top-level Formula metadata tap repository drifted")
    if metadata["kandelo_repository"].lower() != policy.kandelo_repository.lower():
        raise TapMetadataError("top-level Formula metadata Kandelo repository drifted")
    if metadata["kandelo_abi"] != state.current_abi:
        raise TapMetadataError("ABI state and top-level Formula metadata disagree")

    packages = list(_sequence(metadata["packages"], "top-level Formula packages"))
    if not packages:
        raise TapMetadataError("top-level Formula metadata has no packages")
    active: list[str] = []
    active_projection: list[dict[str, Any]] = []
    for index, candidate in enumerate(packages):
        package = _mapping(candidate, f"top-level package {index}")
        _exact_keys(package, PACKAGE_KEYS, f"top-level package {index}")
        name = _stable_id(package["name"], f"top-level package {index} name")
        if active and name <= active[-1]:
            raise TapMetadataError("top-level packages must be sorted and duplicate-free")
        active.append(name)
        formula_path = package["formula_path"]
        sidecar_path = package["formula_metadata"]
        if formula_path != f"Formula/{name}.rb" or sidecar_path != f"Kandelo/formula/{name}.json":
            raise TapMetadataError(f"active Formula paths drifted for {name}")
        if not (root / formula_path).is_file() or (root / formula_path).is_symlink():
            raise TapMetadataError(f"active Formula source is unavailable for {name}")
        sidecar = _validate_sidecar(
            _load_json(root / sidecar_path, f"active sidecar for {name}"),
            name=name,
            current_abi=state.current_abi,
            field=f"active sidecar for {name}",
        )
        projected_sidecar = {
            key: sidecar[key]
            for key in PACKAGE_KEYS
            if key != "formula_metadata"
        }
        projected_package = {
            key: package[key]
            for key in PACKAGE_KEYS
            if key != "formula_metadata"
        }
        if canonical_bytes(projected_sidecar) != canonical_bytes(projected_package):
            raise TapMetadataError(f"top-level metadata and active sidecar disagree for {name}")
        active_projection.append(
            {
                "name": name,
                "formula_path": formula_path,
                "sidecar_path": sidecar_path,
                "sidecar_sha256": canonical_sha256(sidecar),
            }
        )

    formula_projection = [
        _formula_bottle_projection(path)
        for path in sorted((root / "Formula").glob("*.rb"), key=lambda item: item.name)
    ]
    formula_by_name = {item["name"]: item for item in formula_projection}
    if not set(active).issubset(formula_by_name):
        raise TapMetadataError("active metadata names a missing Formula source")

    sidecar_paths = sorted((root / "Kandelo/formula").glob("*.json"), key=lambda item: item.name)
    sidecar_names = [_stable_id(path.stem, f"sidecar name {path.name}") for path in sidecar_paths]
    if sidecar_names != sorted(set(sidecar_names)):
        raise TapMetadataError("sidecar names are not sorted and duplicate-free")
    legacy = sorted(set(sidecar_names) - set(active))
    for name in legacy:
        _validate_sidecar(
            _load_json(root / f"Kandelo/formula/{name}.json", f"legacy sidecar for {name}"),
            name=name,
            current_abi=None,
            field=f"legacy sidecar for {name}",
        )

    detached: list[str] = []
    for package in packages:
        name = package["name"]
        bottle = formula_by_name[name]["bottle"]
        sidecar_bottles = {
            item["arch"]: item["sha256"]
            for item in package["bottles"]
            if item.get("status") == "success"
        }
        formula_bottles = (
            {}
            if bottle is None
            else {
                item["architecture"]: item["sha256"]
                for item in bottle["architectures"]
            }
        )
        if (
            bottle is None
            or bottle["rebuild"] != package["bottle_rebuild"]
            or formula_bottles != sidecar_bottles
        ):
            detached.append(name)

    return {
        "schema": 1,
        "kind": "kandelo-tap-metadata-projection",
        "current_abi": state.current_abi,
        "current_snapshot_sha256": state.current_snapshot_sha256,
        "promotion_mode": activation.mode,
        "active_formulae": active,
        "active_projection_sha256": canonical_sha256(active_projection),
        "formula_projection_sha256": canonical_sha256(formula_projection),
        "legacy_unselected_sidecars": legacy,
        "detached_active_formula_blocks": detached,
        "metadata_sha256": canonical_sha256(metadata),
    }


def plan_successor_activation_patch(
    tap_root: Path,
    *,
    current_tap_source: Mapping[str, Any],
    target_abi: int,
    target_snapshot_sha256: str,
    activation: Mapping[str, Any],
) -> TapMetadataPatchV1:
    """Build one deterministic N -> N+1 generated-metadata transition."""

    root = tap_root.resolve(strict=True)
    source = _checked_source(root, current_tap_source)
    target = _positive_integer(target_abi, "activation target ABI")
    snapshot = _digest(target_snapshot_sha256, "activation target snapshot")
    state = load_abi_state(root / "Kandelo/abi-state.json")
    if state.activation is not None:
        raise TapMetadataError("successor ABI is already managed")
    if state.current_abi == 2**32 - 1 or target != state.current_abi + 1:
        raise TapMetadataError("activation target is not the exact ABI successor")
    managed = _managed_activation(activation, target)
    if managed.prior_abi != state.current_abi:
        raise TapMetadataError("activation prior ABI differs from current tap state")
    before = check_tap_metadata(root)
    metadata = dict(_load_json(root / "Kandelo/metadata.json", "top-level Formula metadata"))
    packages = [dict(_mapping(item, "activation package")) for item in metadata["packages"]]
    files: dict[str, bytes] = {}
    projected_packages: list[dict[str, Any]] = []
    merged_head = str(managed.merged_pull_request["head"])

    for package in packages:
        name = _stable_id(package["name"], "activation Formula")
        formula_path = root / f"Formula/{name}.rb"
        sidecar_path = root / f"Kandelo/formula/{name}.json"
        formula_source = _read_regular(
            formula_path, f"activation Formula {name}", MAX_METADATA_BYTES
        )
        try:
            normalized = normalize_formula_source(formula_source)
        except FormulaInventoryError as error:
            raise TapMetadataError(
                f"activation Formula normalization failed for {name}: {error}"
            ) from error
        normalized_digest = hashlib.sha256(normalized).hexdigest()
        sidecar = dict(_load_json(sidecar_path, f"activation sidecar for {name}"))
        pending = [
            _pending_bottle(
                bottle,
                target_abi=target,
                kandelo_commit=merged_head,
                tap_commit=source["commit"],
                normalized_formula_sha256=normalized_digest,
                field=f"activation bottle {name}/{index}",
            )
            for index, bottle in enumerate(sidecar["bottles"])
        ]
        sidecar.update(
            {
                "bottles": pending,
                "kandelo_abi": target,
                "tap_commit": source["commit"],
            }
        )
        files[f"Kandelo/formula/{name}.json"] = _pretty_json_bytes(sidecar)
        projected = {
            key: sidecar[key]
            for key in PACKAGE_KEYS
            if key != "formula_metadata"
        }
        projected["formula_metadata"] = f"Kandelo/formula/{name}.json"
        projected_packages.append(projected)

    metadata.update(
        {
            "generator": "kandelo-abi-staging 1",
            "kandelo_abi": target,
            "kandelo_commit": merged_head,
            "packages": projected_packages,
            "release_tag": f"bottles-abi-v{target}",
            "tap_commit": source["commit"],
        }
    )
    files["Kandelo/metadata.json"] = _pretty_json_bytes(metadata)
    files["Kandelo/abi-state.json"] = canonical_bytes(
        {
            "activation": {
                "abi_history_record_digest": managed.abi_history_record_digest,
                "merge_commit": managed.merge_commit,
                "merged_pull_request": dict(managed.merged_pull_request),
                "prior_abi": managed.prior_abi,
                "prior_branch": managed.prior_branch,
                "request_digest": managed.request_digest,
            },
            "current_abi": target,
            "current_snapshot_sha256": snapshot,
            "kind": "kandelo-homebrew-abi-state",
            "schema": 1,
        }
    )

    for formula_path in sorted((root / "Formula").glob("*.rb"), key=lambda item: item.name):
        source_bytes = _read_regular(
            formula_path, f"activation Formula {formula_path.name}", MAX_METADATA_BYTES
        )
        without_bottle = _without_bottle_block(source_bytes)
        if without_bottle != source_bytes:
            files[f"Formula/{formula_path.name}"] = without_bottle

    allowed = tuple(sorted(files))
    expected = MappingProxyType(
        {
            path: hashlib.sha256((root / path).read_bytes()).hexdigest()
            for path in allowed
        }
    )
    patch = TapMetadataPatchV1(
        operation="successor-activation",
        expected_main_commit=source["commit"],
        expected_main_tree=source["tree"],
        allowed_paths=allowed,
        expected_files_sha256=expected,
        files=MappingProxyType(files),
    )
    validate_successor_activation_patch(root, patch)
    if before["current_abi"] != state.current_abi:
        raise TapMetadataError("activation input metadata changed while planning")
    return patch


def _copy_metadata_projection(root: Path, destination: Path) -> None:
    shutil.copytree(root / "Formula", destination / "Formula")
    shutil.copytree(root / "Kandelo/formula", destination / "Kandelo/formula")
    shutil.copytree(root / "Kandelo/link", destination / "Kandelo/link")
    (destination / "Kandelo/staging").mkdir(parents=True)
    for name in ("promotion-policy.toml", "promotion-activation.toml"):
        shutil.copy2(
            root / f"Kandelo/staging/{name}",
            destination / f"Kandelo/staging/{name}",
        )
    shutil.copy2(root / "Kandelo/metadata.json", destination / "Kandelo/metadata.json")
    shutil.copy2(root / "Kandelo/abi-state.json", destination / "Kandelo/abi-state.json")


def validate_successor_activation_patch(
    tap_root: Path, patch: TapMetadataPatchV1
) -> dict[str, Any]:
    root = tap_root.resolve(strict=True)
    if not isinstance(patch, TapMetadataPatchV1) or patch.operation != "successor-activation":
        raise TapMetadataError("successor activation patch protocol is unsupported")
    source = _checked_source(
        root,
        {
            "repository": load_promotion_policy(
                root / "Kandelo/staging/promotion-policy.toml"
            ).tap_repository,
            "commit": patch.expected_main_commit,
            "tree": patch.expected_main_tree,
        },
    )
    paths = list(patch.allowed_paths)
    if paths != sorted(set(paths)) or set(paths) != set(patch.files) or set(paths) != set(
        patch.expected_files_sha256
    ):
        raise TapMetadataError("successor activation path set is not exact")
    if "Kandelo/abi-state.json" not in paths or "Kandelo/metadata.json" not in paths:
        raise TapMetadataError("successor activation omits global generated metadata")
    for path in paths:
        if not re.fullmatch(
            r"(?:Formula/[a-z0-9][a-z0-9+._-]*\.rb|"
            r"Kandelo/formula/[a-z0-9][a-z0-9+._-]*\.json|"
            r"Kandelo/(?:abi-state|metadata)\.json)",
            path,
        ):
            raise TapMetadataError("successor activation names an unexpected path")
        before = _read_regular(root / path, f"activation input {path}", MAX_METADATA_BYTES)
        if hashlib.sha256(before).hexdigest() != _digest(
            patch.expected_files_sha256[path], f"activation input digest {path}"
        ):
            raise TapMetadataError("successor activation generated metadata CAS changed")
        body = patch.files[path]
        if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_METADATA_BYTES:
            raise TapMetadataError("successor activation output bytes are outside their bound")
        if body == before:
            raise TapMetadataError("successor activation includes an unchanged path")

    with tempfile.TemporaryDirectory() as temporary:
        simulated = Path(temporary)
        _copy_metadata_projection(root, simulated)
        for path, body in patch.files.items():
            destination = simulated / path
            if not destination.is_file() or destination.is_symlink():
                raise TapMetadataError("successor activation output path is not an existing file")
            destination.write_bytes(body)
        projection = check_tap_metadata(simulated)
        state = load_abi_state(simulated / "Kandelo/abi-state.json")
        if state.activation is None or state.activation.prior_abi + 1 != state.current_abi:
            raise TapMetadataError("successor activation state is incomplete")
        metadata = _load_json(
            simulated / "Kandelo/metadata.json", "activated top-level metadata"
        )
        if (
            metadata["kandelo_abi"] != state.current_abi
            or metadata["release_tag"] != f"bottles-abi-v{state.current_abi}"
            or metadata["kandelo_commit"] != state.activation.merged_pull_request["head"]
            or metadata["tap_commit"] != source["commit"]
        ):
            raise TapMetadataError("successor activation global metadata drifted")
        for package in metadata["packages"]:
            bottles = _sequence(package["bottles"], "activated package bottles")
            if not bottles or any(
                _mapping(bottle, "activated bottle").get("status") != "pending"
                for bottle in bottles
            ):
                raise TapMetadataError("successor activation waits on or selects a bottle")
        for formula_path in sorted((simulated / "Formula").glob("*.rb")):
            formula_lines = _decode_formula(formula_path.read_bytes()).splitlines(
                keepends=True
            )
            if _bottle_span(formula_lines) is not None:
                raise TapMetadataError("successor activation retains a prior bottle block")
        before_projection = check_tap_metadata(root)
        if projection["active_formulae"] != before_projection["active_formulae"]:
            raise TapMetadataError("successor activation changed the active Formula inventory")
        return projection


def formula_generated_metadata_sha256(tap_root: Path, formula: str) -> str:
    root = tap_root.resolve(strict=True)
    name = _stable_id(formula, "generated metadata Formula")
    formula_path = root / f"Formula/{name}.rb"
    sidecar = _load_json(
        root / f"Kandelo/formula/{name}.json", f"generated sidecar for {name}"
    )
    metadata = _load_json(root / "Kandelo/metadata.json", "generated metadata index")
    matches = [
        candidate
        for candidate in _sequence(metadata.get("packages"), "generated packages")
        if _mapping(candidate, "generated package").get("name") == name
    ]
    if len(matches) != 1:
        raise TapMetadataError("generated metadata Formula is absent or duplicated")
    formula_body = _read_regular(
        formula_path, f"generated Formula {name}", MAX_METADATA_BYTES
    )
    return canonical_sha256(
        {
            "formula_sha256": hashlib.sha256(formula_body).hexdigest(),
            "sidecar_sha256": canonical_sha256(sidecar),
            "top_index_row_sha256": canonical_sha256(matches[0]),
        }
    )


def _git_regular_blob(
    root: Path,
    commit: str,
    path: str,
    field: str,
    *,
    allow_absent: bool = False,
) -> bytes | None:
    listing = [
        entry
        for entry in _git_bytes(root, "ls-tree", "-z", commit, "--", path).split(b"\0")
        if entry
    ]
    if not listing:
        if allow_absent:
            return None
        raise TapMetadataError(f"{field} is absent from its exact Git source")
    if len(listing) != 1:
        raise TapMetadataError(f"{field} is ambiguous in its exact Git source")
    match = re.fullmatch(
        rb"(100644|100755) blob [0-9a-f]{40}\t([^\0]+)", listing[0]
    )
    if match is None or match.group(2) != path.encode("utf-8"):
        raise TapMetadataError(f"{field} is not an exact regular Git blob")
    body = _git_bytes(root, "show", f"{commit}:{path}")
    if not 1 <= len(body) <= MAX_METADATA_BYTES:
        raise TapMetadataError(f"{field} is outside its byte bound")
    return body


def _json_mapping_bytes(body: bytes, field: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TapMetadataError) as error:
        raise TapMetadataError(f"{field} is invalid JSON: {error}") from error
    return _mapping(value, field)


def _formula_generated_metadata_sha256_at_commit(
    root: Path, commit: str, formula: str
) -> str:
    name = _stable_id(formula, "historical generated metadata Formula")
    formula_body = _git_regular_blob(
        root, commit, f"Formula/{name}.rb", f"historical Formula {name}"
    )
    sidecar_body = _git_regular_blob(
        root,
        commit,
        f"Kandelo/formula/{name}.json",
        f"historical Formula sidecar {name}",
    )
    metadata_body = _git_regular_blob(
        root,
        commit,
        "Kandelo/metadata.json",
        "historical generated metadata index",
    )
    assert formula_body is not None and sidecar_body is not None
    assert metadata_body is not None
    sidecar = _json_mapping_bytes(sidecar_body, f"historical sidecar for {name}")
    metadata = _json_mapping_bytes(metadata_body, "historical generated metadata index")
    matches = [
        candidate
        for candidate in _sequence(metadata.get("packages"), "historical packages")
        if _mapping(candidate, "historical package").get("name") == name
    ]
    if len(matches) != 1:
        raise TapMetadataError(
            "historical generated metadata Formula is absent or duplicated"
        )
    return canonical_sha256(
        {
            "formula_sha256": hashlib.sha256(formula_body).hexdigest(),
            "sidecar_sha256": canonical_sha256(sidecar),
            "top_index_row_sha256": canonical_sha256(matches[0]),
        }
    )


def recover_landed_formula_metadata_commit(
    tap_root: Path, *, current_update: FormulaMetadataUpdateV1
) -> RecoveredFormulaMetadataCommitV1:
    """Recover the one exact metadata CAS commit after admission publication failed."""

    root = tap_root.resolve(strict=True)
    checked = _checked_formula_update(current_update)
    policy = load_promotion_policy(root / "Kandelo/staging/promotion-policy.toml")
    current_source = _checked_source(
        root,
        {
            "repository": policy.tap_repository,
            "commit": _git(root, "rev-parse", "HEAD"),
            "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        },
    )
    if (
        checked.expected_main_commit != current_source["commit"]
        or formula_generated_metadata_sha256(root, checked.formula)
        != checked.expected_generated_metadata_sha256
    ):
        raise TapMetadataError(
            "current Formula metadata retry authority differs from tap main"
        )
    validate_formula_admission_projection(root, checked)

    history = _git(
        root,
        "log",
        "--first-parent",
        "--format=%H",
        "--max-count=4097",
        "--",
        checked.link_manifest_path,
    ).splitlines()
    if len(history) > 4096:
        raise TapMetadataError("Formula metadata Git history exceeds its scan bound")
    matches: list[RecoveredFormulaMetadataCommitV1] = []
    for candidate_commit in history:
        try:
            landed_commit = _git_sha(
                candidate_commit, "landed Formula metadata commit"
            )
            parents = _git(
                root, "rev-list", "--parents", "-n", "1", landed_commit
            ).split()
            if len(parents) != 2 or parents[0] != landed_commit:
                continue
            base_commit = _git_sha(parents[1], "Formula metadata CAS base")
            changed_paths = _git(
                root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                landed_commit,
            ).splitlines()
            if sorted(changed_paths) != sorted(checked.allowed_paths):
                continue
            base_source = {
                "repository": policy.tap_repository,
                "commit": base_commit,
                "tree": _git(root, "rev-parse", f"{base_commit}^{{tree}}"),
            }
            landed_source = {
                "repository": policy.tap_repository,
                "commit": landed_commit,
                "tree": _git(root, "rev-parse", f"{landed_commit}^{{tree}}"),
            }
            expected_files: dict[str, str | None] = {}
            files: dict[str, bytes] = {}
            for path in checked.allowed_paths:
                before = _git_regular_blob(
                    root,
                    base_commit,
                    path,
                    f"Formula metadata CAS input {path}",
                    allow_absent=path == checked.link_manifest_path,
                )
                expected_files[path] = (
                    None if before is None else hashlib.sha256(before).hexdigest()
                )
                after = _git_regular_blob(
                    root, landed_commit, path, f"landed Formula metadata {path}"
                )
                assert after is not None
                files[path] = after
            if hashlib.sha256(files[checked.link_manifest_path]).hexdigest() != (
                checked.link_manifest_sha256
            ):
                continue
            base_formula = _git_regular_blob(
                root,
                base_commit,
                f"Formula/{checked.formula}.rb",
                "Formula metadata base source",
            )
            assert base_formula is not None
            normalized = normalize_formula_source(base_formula)
            if hashlib.sha256(normalized).hexdigest() != (
                checked.expected_normalized_formula_sha256
            ):
                continue
            recovered_update = replace(
                checked,
                expected_main_commit=base_commit,
                expected_generated_metadata_sha256=(
                    _formula_generated_metadata_sha256_at_commit(
                        root, base_commit, checked.formula
                    )
                ),
            )
            recovered_patch = TapMetadataPatchV1(
                operation="formula-metadata",
                expected_main_commit=base_commit,
                expected_main_tree=base_source["tree"],
                allowed_paths=checked.allowed_paths,
                expected_files_sha256=MappingProxyType(expected_files),
                files=MappingProxyType(files),
            )
            validate_landed_formula_metadata_commit(
                root,
                base_source=base_source,
                landed_source=landed_source,
                patch=recovered_patch,
            )
            matches.append(
                RecoveredFormulaMetadataCommitV1(
                    base_source=MappingProxyType(base_source),
                    landed_source=MappingProxyType(landed_source),
                    update=recovered_update,
                    patch=recovered_patch,
                )
            )
        except (FormulaInventoryError, TapMetadataError):
            continue
    if len(matches) != 1:
        raise TapMetadataError(
            "Formula metadata has no unique exact landed CAS commit"
        )
    validate_formula_admission_projection(root, matches[0].update)
    return matches[0]


def _checked_formula_update(
    update: FormulaMetadataUpdateV1,
) -> FormulaMetadataUpdateV1:
    if not isinstance(update, FormulaMetadataUpdateV1):
        raise TapMetadataError("Formula metadata update protocol is unsupported")
    formula = _stable_id(update.formula, "metadata Formula")
    if update.architecture not in {"wasm32", "wasm64"}:
        raise TapMetadataError("metadata Formula architecture is unsupported")
    link_manifest_path = _text(
        update.link_manifest_path, "metadata Formula link manifest", 4096
    )
    if re.fullmatch(
        rf"Kandelo/link/{re.escape(formula)}-"
        rf"[A-Za-z0-9][A-Za-z0-9._+,-]{{0,255}}-"
        rf"rebuild(?:0|[1-9][0-9]{{0,9}})-{update.architecture}\.json",
        link_manifest_path,
    ) is None:
        raise TapMetadataError("metadata Formula link manifest path is not exact")
    expected_paths = (
        f"Formula/{formula}.rb",
        f"Kandelo/formula/{formula}.json",
        "Kandelo/metadata.json",
        link_manifest_path,
    )
    if update.allowed_paths != expected_paths:
        raise TapMetadataError("Formula metadata update path set is not exact")
    _git_sha(update.expected_main_commit, "metadata expected main")
    _digest(update.expected_normalized_formula_sha256, "metadata normalized Formula")
    _digest(update.expected_generated_metadata_sha256, "metadata generated projection")
    _digest(update.link_manifest_sha256, "metadata link manifest")
    _digest(update.canonical_manifest_digest, "metadata canonical manifest")
    _digest(update.bottle_layer_sha256, "metadata bottle layer")
    _positive_integer(update.bottle_layer_bytes, "metadata bottle bytes")
    _positive_integer(update.target_abi, "metadata target ABI")
    return update


def _checked_promoted_bottle(
    value: PromotedBottleMetadataV1,
    update: FormulaMetadataUpdateV1,
) -> PromotedBottleMetadataV1:
    if not isinstance(value, PromotedBottleMetadataV1):
        raise TapMetadataError("promoted bottle metadata protocol is unsupported")
    if (
        value.formula != update.formula
        or value.architecture != update.architecture
    ):
        raise TapMetadataError("promoted bottle identity differs from its update")
    _text(value.version, "promoted bottle version", 128)
    _nonnegative_integer(value.revision, "promoted bottle revision")
    _nonnegative_integer(value.rebuild, "promoted bottle rebuild")
    root = _text(value.canonical_root_url, "promoted bottle root", 4096)
    if re.fullmatch(
        r"https://ghcr\.io/v2/[a-z0-9][a-z0-9._-]*/"
        r"[a-z0-9][a-z0-9._/-]*",
        root,
    ) is None:
        raise TapMetadataError("promoted bottle root is not exact HTTPS GHCR")
    cellar = _text(value.cellar, "promoted bottle cellar", 4096)
    if (
        cellar
        not in {"any", "any_skip_relocation", ":any", ":any_skip_relocation"}
        and re.fullmatch(
            r"/[A-Za-z0-9._/+:-]+(?:/[A-Za-z0-9._+:-]+)*", cellar
        )
        is None
    ):
        raise TapMetadataError("promoted bottle cellar is unsupported")
    built_by = _text(value.built_by, "promoted bottle producer", 4096)
    if re.fullmatch(
        r"https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/actions/runs/[1-9][0-9]*",
        built_by,
    ) is None:
        raise TapMetadataError("promoted bottle producer is not an exact workflow run")
    built_from = _mapping(value.built_from, "promoted bottle build source")
    _exact_keys(
        built_from,
        frozenset(
            {
                "formula_sha256",
                "kandelo_commit",
                "kandelo_repository",
                "tap_commit",
                "tap_repository",
            }
        ),
        "promoted bottle build source",
    )
    _digest(built_from["formula_sha256"], "promoted bottle Formula source")
    _git_sha(built_from["kandelo_commit"], "promoted bottle Kandelo commit")
    _repository(built_from["kandelo_repository"], "promoted bottle Kandelo repository")
    _git_sha(built_from["tap_commit"], "promoted bottle tap commit")
    _repository(built_from["tap_repository"], "promoted bottle tap repository")
    pkg_version = value.version if value.revision == 0 else f"{value.version}_{value.revision}"
    link_value = _mapping(value.link_manifest, "promoted bottle link manifest")
    try:
        checked_link = validate_link_manifest(
            link_value,
            formula=value.formula,
            version=pkg_version,
            architecture=value.architecture,
            target_abi=update.target_abi,
            prefix=link_value.get("prefix"),
            cellar=cellar,
            bottle_url=(
                root + "/blobs/sha256:" + update.bottle_layer_sha256
            ),
            bottle_sha256=update.bottle_layer_sha256,
            bottle_bytes=update.bottle_layer_bytes,
        )
        link_sha256 = hashlib.sha256(link_manifest_bytes(checked_link)).hexdigest()
    except BottleLinkError as error:
        raise TapMetadataError(f"promoted bottle link manifest is invalid: {error}") from error
    if link_sha256 != update.link_manifest_sha256:
        raise TapMetadataError("promoted bottle link manifest identity changed")
    return value


def _formula_cellar(value: str) -> str:
    if value in {"any", ":any"}:
        return ":any"
    if value in {"any_skip_relocation", ":any_skip_relocation"}:
        return ":any_skip_relocation"
    return json.dumps(value)


def _compose_formula_bottles(
    normalized: bytes,
    *,
    root_url: str,
    rebuild: int,
    bottles: Sequence[Mapping[str, Any]],
) -> bytes:
    lines = _decode_formula(normalized).splitlines(keepends=True)
    inline_patch_markers = [
        index for index, line in enumerate(lines) if line == "__END__\n"
    ]
    if len(inline_patch_markers) > 1:
        raise TapMetadataError("Formula has multiple inline patch boundaries")
    boundary = inline_patch_markers[0] if inline_patch_markers else len(lines)
    final = boundary - 1
    while final >= 0 and lines[final] == "\n":
        final -= 1
    if final < 0 or lines[final] != "end\n":
        raise TapMetadataError("Formula has no canonical final class end")
    successful = [bottle for bottle in bottles if bottle.get("status") == "success"]
    architectures = [str(bottle.get("arch")) for bottle in successful]
    if architectures != sorted(set(architectures)) or not successful:
        raise TapMetadataError("Formula success bottle set is not sorted and unique")
    body = ["\n", "  bottle do\n", f'    root_url "{root_url}"\n']
    if rebuild:
        body.append(f"    rebuild {rebuild}\n")
    for bottle in successful:
        body.append(
            "    sha256 cellar: "
            + _formula_cellar(str(bottle["cellar"]))
            + f', {bottle["bottle_tag"]}: "{bottle["sha256"]}"\n'
        )
    body.append("  end\n")
    lines[final:final] = body
    result = "".join(lines).encode("utf-8")
    try:
        if normalize_formula_source(result) != normalized:
            raise TapMetadataError("generated bottle block changed Formula intent")
    except FormulaInventoryError as error:
        raise TapMetadataError(f"generated bottle block is invalid: {error}") from error
    return result


def plan_formula_metadata_patch(
    tap_root: Path,
    *,
    current_tap_source: Mapping[str, Any],
    update: FormulaMetadataUpdateV1,
    promoted: PromotedBottleMetadataV1,
) -> TapMetadataPatchV1:
    root = tap_root.resolve(strict=True)
    checked_update = _checked_formula_update(update)
    checked_promoted = _checked_promoted_bottle(promoted, checked_update)
    policy = load_promotion_policy(root / "Kandelo/staging/promotion-policy.toml")
    source = _checked_source(root, current_tap_source)
    if source["repository"].lower() != policy.tap_repository.lower():
        raise TapMetadataError("Formula metadata source names another tap")
    if source["commit"] != checked_update.expected_main_commit:
        raise TapMetadataError("Formula metadata expected main CAS changed")
    state = load_abi_state(root / "Kandelo/abi-state.json")
    if state.activation is None or state.current_abi != checked_update.target_abi:
        raise TapMetadataError("Formula metadata target is not the active managed ABI")
    check_tap_metadata(root)
    name = checked_update.formula
    formula_path = root / f"Formula/{name}.rb"
    formula_source = _read_regular(
        formula_path, f"metadata Formula {name}", MAX_METADATA_BYTES
    )
    try:
        normalized = normalize_formula_source(formula_source)
    except FormulaInventoryError as error:
        raise TapMetadataError(f"metadata Formula is invalid: {error}") from error
    if hashlib.sha256(normalized).hexdigest() != checked_update.expected_normalized_formula_sha256:
        raise TapMetadataError("Formula source CAS changed")
    if (
        formula_generated_metadata_sha256(root, name)
        != checked_update.expected_generated_metadata_sha256
    ):
        raise TapMetadataError("Formula generated metadata CAS changed")

    sidecar_path = root / f"Kandelo/formula/{name}.json"
    sidecar = dict(_load_json(sidecar_path, f"metadata sidecar for {name}"))
    bottles = [dict(_mapping(item, "metadata bottle")) for item in sidecar["bottles"]]
    pkg_version = (
        checked_promoted.version
        if checked_promoted.revision == 0
        else f"{checked_promoted.version}_{checked_promoted.revision}"
    )
    current_versioning = (
        sidecar["version"],
        sidecar["formula_revision"],
        sidecar["bottle_rebuild"],
    )
    promoted_versioning = (
        pkg_version,
        checked_promoted.revision,
        checked_promoted.rebuild,
    )
    if any(bottle.get("status") == "success" for bottle in bottles):
        if current_versioning != promoted_versioning:
            raise TapMetadataError(
                "promoted bottle versioning differs from an already selected architecture"
            )
    else:
        sidecar.update(
            {
                "version": pkg_version,
                "formula_revision": checked_promoted.revision,
                "bottle_rebuild": checked_promoted.rebuild,
            }
        )
    matches = [
        index
        for index, bottle in enumerate(bottles)
        if bottle.get("arch") == checked_update.architecture
    ]
    if len(matches) != 1:
        raise TapMetadataError("metadata architecture is absent or duplicated")
    index = matches[0]
    existing = bottles[index]
    link_manifest = checked_update.link_manifest_path
    for key in (
        "error",
        "last_attempt",
        "last_attempt_by",
        "queued_at",
        "built_at",
        *_BOTTLE_SELECTION_FIELDS,
    ):
        existing.pop(key, None)
    existing.update(
        {
            "arch": checked_update.architecture,
            "bottle_tag": f"{checked_update.architecture}_kandelo",
            "built_by": checked_promoted.built_by,
            "built_from": dict(checked_promoted.built_from),
            "bytes": checked_update.bottle_layer_bytes,
            "cache_key_sha": checked_update.bottle_layer_sha256,
            "cellar": checked_promoted.cellar,
            "kandelo_abi": checked_update.target_abi,
            "link_manifest": link_manifest,
            "prefix": checked_promoted.link_manifest["prefix"],
            "sha256": checked_update.bottle_layer_sha256,
            "status": "success",
            "url": (
                checked_promoted.canonical_root_url
                + "/blobs/sha256:"
                + checked_update.bottle_layer_sha256
            ),
        }
    )
    bottles[index] = existing
    bottles.sort(key=lambda item: str(item["arch"]))
    sidecar["bottles"] = bottles
    formula_output = _compose_formula_bottles(
        normalized,
        root_url=checked_promoted.canonical_root_url,
        rebuild=checked_promoted.rebuild,
        bottles=bottles,
    )
    sidecar_output = _pretty_json_bytes(sidecar)
    metadata = dict(_load_json(root / "Kandelo/metadata.json", "metadata top index"))
    packages = [dict(_mapping(item, "metadata package")) for item in metadata["packages"]]
    package_matches = [index for index, item in enumerate(packages) if item["name"] == name]
    if len(package_matches) != 1:
        raise TapMetadataError("metadata top-index Formula is absent or duplicated")
    projected = {
        key: sidecar[key] for key in PACKAGE_KEYS if key != "formula_metadata"
    }
    projected["formula_metadata"] = f"Kandelo/formula/{name}.json"
    packages[package_matches[0]] = projected
    metadata["packages"] = packages
    metadata_output = _pretty_json_bytes(metadata)
    outputs = {
        f"Formula/{name}.rb": formula_output,
        f"Kandelo/formula/{name}.json": sidecar_output,
        "Kandelo/metadata.json": metadata_output,
        link_manifest: link_manifest_bytes(checked_promoted.link_manifest),
    }
    changed = {
        path: body
        for path, body in outputs.items()
        if not (root / path).is_file() or (root / path).read_bytes() != body
    }
    expected_files: dict[str, str | None] = {}
    for path in checked_update.allowed_paths:
        candidate = root / path
        if candidate.is_symlink():
            raise TapMetadataError("Formula generated metadata CAS path is a symlink")
        if candidate.exists():
            if not candidate.is_file():
                raise TapMetadataError("Formula generated metadata CAS path is not a file")
            expected_files[path] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        elif path == link_manifest:
            expected_files[path] = None
        else:
            raise TapMetadataError("Formula generated metadata CAS path is absent")
    patch = TapMetadataPatchV1(
        operation="formula-metadata",
        expected_main_commit=source["commit"],
        expected_main_tree=source["tree"],
        allowed_paths=checked_update.allowed_paths,
        expected_files_sha256=MappingProxyType(expected_files),
        files=MappingProxyType(changed),
    )
    validate_formula_metadata_patch(root, patch, checked_update)
    return patch


def validate_formula_metadata_patch(
    tap_root: Path,
    patch: TapMetadataPatchV1,
    update: FormulaMetadataUpdateV1,
) -> dict[str, Any]:
    root = tap_root.resolve(strict=True)
    checked = _checked_formula_update(update)
    if not isinstance(patch, TapMetadataPatchV1) or patch.operation != "formula-metadata":
        raise TapMetadataError("Formula metadata patch protocol is unsupported")
    if (
        patch.expected_main_commit != checked.expected_main_commit
        or patch.allowed_paths != checked.allowed_paths
        or set(patch.expected_files_sha256) != set(checked.allowed_paths)
        or not set(patch.files).issubset(checked.allowed_paths)
    ):
        raise TapMetadataError("Formula metadata patch CAS or path set changed")
    source = _checked_source(
        root,
        {
            "repository": load_promotion_policy(
                root / "Kandelo/staging/promotion-policy.toml"
            ).tap_repository,
            "commit": patch.expected_main_commit,
            "tree": patch.expected_main_tree,
        },
    )
    for path in checked.allowed_paths:
        expected_digest = patch.expected_files_sha256[path]
        candidate = root / path
        if expected_digest is None:
            if path != checked.link_manifest_path or candidate.exists() or candidate.is_symlink():
                raise TapMetadataError("Formula generated metadata creation CAS changed")
        else:
            before = _read_regular(
                candidate, f"Formula metadata input {path}", MAX_METADATA_BYTES
            )
            if hashlib.sha256(before).hexdigest() != _digest(
                expected_digest, f"Formula metadata input digest {path}"
            ):
                raise TapMetadataError("Formula generated metadata CAS changed")
    for path, body in patch.files.items():
        if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_METADATA_BYTES:
            raise TapMetadataError("Formula metadata output bytes are outside their bound")
        if (root / path).is_file() and body == (root / path).read_bytes():
            raise TapMetadataError("Formula metadata patch includes an unchanged path")

    if patch.files and set(patch.files) != set(checked.allowed_paths):
        raise TapMetadataError("Formula metadata patch does not update its exact generated set")
    with tempfile.TemporaryDirectory() as temporary:
        simulated = Path(temporary)
        _copy_metadata_projection(root, simulated)
        for path, body in patch.files.items():
            (simulated / path).parent.mkdir(parents=True, exist_ok=True)
            (simulated / path).write_bytes(body)
        projection = check_tap_metadata(simulated)
        state = load_abi_state(simulated / "Kandelo/abi-state.json")
        if state.current_abi != checked.target_abi:
            raise TapMetadataError("Formula metadata patch changed current ABI binding")
        sidecar = _load_json(
            simulated / f"Kandelo/formula/{checked.formula}.json",
            "updated Formula sidecar",
        )
        matches = [
            bottle
            for bottle in sidecar["bottles"]
            if bottle.get("arch") == checked.architecture
        ]
        if len(matches) != 1 or matches[0].get("status") != "success" or (
            matches[0].get("sha256") != checked.bottle_layer_sha256
            or matches[0].get("bytes") != checked.bottle_layer_bytes
        ):
            raise TapMetadataError("Formula metadata patch does not select the exact layer")
        selected = matches[0]
        if selected.get("link_manifest") != checked.link_manifest_path:
            raise TapMetadataError("Formula metadata patch selects another link manifest")
        link_body = _read_regular(
            simulated / checked.link_manifest_path,
            "updated Formula link manifest",
            MAX_METADATA_BYTES,
        )
        if hashlib.sha256(link_body).hexdigest() != checked.link_manifest_sha256:
            raise TapMetadataError("Formula metadata patch changed link manifest identity")
        link = _load_json(
            simulated / checked.link_manifest_path,
            "updated Formula link manifest",
        )
        try:
            validate_link_manifest(
                link,
                formula=checked.formula,
                version=sidecar["version"],
                architecture=checked.architecture,
                target_abi=checked.target_abi,
                prefix=selected.get("prefix"),
                cellar=selected.get("cellar"),
                bottle_url=selected.get("url"),
                bottle_sha256=checked.bottle_layer_sha256,
                bottle_bytes=checked.bottle_layer_bytes,
            )
        except BottleLinkError as error:
            raise TapMetadataError(
                f"Formula metadata patch link manifest is invalid: {error}"
            ) from error
        formula_projection = _formula_bottle_projection(
            simulated / f"Formula/{checked.formula}.rb"
        )
        bottle = formula_projection["bottle"]
        policy = load_promotion_policy(
            simulated / "Kandelo/staging/promotion-policy.toml"
        )
        owner = policy.tap_repository.split("/", 1)[0]
        expected_root = (
            "https://ghcr.io/v2/"
            + owner
            + "/"
            + policy.canonical_repository_prefix
            + str(checked.target_abi)
            + "/"
            + checked.formula
        )
        expected_architectures = [
            {
                "architecture": str(item["arch"]),
                "cellar": _formula_cellar(str(item["cellar"])),
                "sha256": str(item["sha256"]),
            }
            for item in sidecar["bottles"]
            if item.get("status") == "success"
        ]
        if (
            bottle is None
            or bottle["root_url"] != expected_root
            or bottle["rebuild"] != sidecar["bottle_rebuild"]
            or bottle["architectures"] != expected_architectures
            or checked.formula in projection["detached_active_formula_blocks"]
        ):
            raise TapMetadataError(
                "Formula bottle block differs from exact canonical metadata"
            )
        if source["commit"] != patch.expected_main_commit:
            raise TapMetadataError("Formula metadata source moved during validation")
        return projection


def validate_formula_admission_projection(
    tap_root: Path,
    update: FormulaMetadataUpdateV1,
) -> dict[str, Any]:
    """Require current main to retain the exact admitted four-path projection."""

    root = tap_root.resolve(strict=True)
    checked = _checked_formula_update(update)
    projection = check_tap_metadata(root)
    state = load_abi_state(root / "Kandelo/abi-state.json")
    if (
        state.current_abi != checked.target_abi
        or checked.formula in projection["detached_active_formula_blocks"]
    ):
        raise TapMetadataError("admitted Formula is detached from current ABI metadata")
    sidecar = _load_json(
        root / f"Kandelo/formula/{checked.formula}.json",
        "admitted Formula sidecar",
    )
    matches = [
        bottle
        for bottle in sidecar["bottles"]
        if bottle.get("arch") == checked.architecture
    ]
    if len(matches) != 1 or matches[0].get("status") != "success":
        raise TapMetadataError("admitted Formula selection is absent or duplicated")
    selected = matches[0]
    if (
        selected.get("sha256") != checked.bottle_layer_sha256
        or selected.get("bytes") != checked.bottle_layer_bytes
        or selected.get("link_manifest") != checked.link_manifest_path
    ):
        raise TapMetadataError("admitted Formula selection differs from its exact layer")
    link_body = _read_regular(
        root / checked.link_manifest_path,
        "admitted Formula link manifest",
        MAX_METADATA_BYTES,
    )
    if hashlib.sha256(link_body).hexdigest() != checked.link_manifest_sha256:
        raise TapMetadataError("admitted Formula link manifest identity changed")
    link = _load_json(
        root / checked.link_manifest_path,
        "admitted Formula link manifest",
    )
    try:
        validate_link_manifest(
            link,
            formula=checked.formula,
            version=sidecar["version"],
            architecture=checked.architecture,
            target_abi=checked.target_abi,
            prefix=selected.get("prefix"),
            cellar=selected.get("cellar"),
            bottle_url=selected.get("url"),
            bottle_sha256=checked.bottle_layer_sha256,
            bottle_bytes=checked.bottle_layer_bytes,
        )
    except BottleLinkError as error:
        raise TapMetadataError(
            f"admitted Formula link manifest is invalid: {error}"
        ) from error
    formula_projection = _formula_bottle_projection(
        root / f"Formula/{checked.formula}.rb"
    )
    if (
        formula_projection["normalized_formula_sha256"]
        != checked.expected_normalized_formula_sha256
    ):
        raise TapMetadataError("admitted Formula source identity changed")
    bottle = formula_projection["bottle"]
    policy = load_promotion_policy(
        root / "Kandelo/staging/promotion-policy.toml"
    )
    expected_root = (
        "https://ghcr.io/v2/"
        + policy.tap_repository.split("/", 1)[0]
        + "/"
        + policy.canonical_repository_prefix
        + str(checked.target_abi)
        + "/"
        + checked.formula
    )
    expected_architectures = [
        {
            "architecture": str(item["arch"]),
            "cellar": _formula_cellar(str(item["cellar"])),
            "sha256": str(item["sha256"]),
        }
        for item in sidecar["bottles"]
        if item.get("status") == "success"
    ]
    if (
        bottle is None
        or bottle["root_url"] != expected_root
        or bottle["rebuild"] != sidecar["bottle_rebuild"]
        or bottle["architectures"] != expected_architectures
    ):
        raise TapMetadataError("admitted Formula bottle projection changed")
    return projection


def build_admission_projection_observation(
    tap_root: Path,
    record: Mapping[str, Any],
    *,
    tap_source: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one canonical admission to the exact current four-path projection."""

    # Imported here because durable records depend on planning modules which also
    # consume tap metadata; keeping the boundary local avoids a module cycle.
    from .records import TapRecordError, validate_admission_record

    try:
        validate_admission_record(record)
    except TapRecordError as error:
        raise TapMetadataError(f"admission record is invalid: {error}") from error
    source = _mapping(tap_source, "current tap source")
    _exact_keys(
        source,
        frozenset({"repository", "commit", "tree"}),
        "current tap source",
    )
    repository = _text(source["repository"], "current tap repository", 255)
    if REPOSITORY.fullmatch(repository) is None:
        raise TapMetadataError("current tap repository is not owner/name")
    commit = _git_sha(source["commit"], "current tap commit")
    tree = _git_sha(source["tree"], "current tap tree")
    policy = load_promotion_policy(
        tap_root.resolve(strict=True) / "Kandelo/staging/promotion-policy.toml"
    )
    if repository.lower() != policy.tap_repository.lower():
        raise TapMetadataError("current tap source names another repository")

    admission = _mapping(record["admission"], "admission payload")
    update_value = _mapping(
        admission["formula_metadata_update"], "Formula metadata update"
    )
    try:
        update = FormulaMetadataUpdateV1(
            **{
                **update_value,
                "allowed_paths": tuple(update_value["allowed_paths"]),
            }
        )
    except (KeyError, TypeError) as error:
        raise TapMetadataError(
            f"Formula metadata update protocol is unsupported: {error}"
        ) from error
    projection = validate_formula_admission_projection(tap_root, update)
    return {
        "schema": 1,
        "kind": "kandelo-pages-admission-projection",
        "admission_record_sha256": canonical_sha256(record),
        "formula": update.formula,
        "architecture": update.architecture,
        "target_abi": update.target_abi,
        "formula_metadata_update_sha256": canonical_sha256(update_value),
        "projection_sha256": canonical_sha256(projection),
        "tap_source": {
            "repository": repository,
            "commit": commit,
            "tree": tree,
        },
    }


def validate_landed_formula_metadata_commit(
    tap_root: Path,
    *,
    base_source: Mapping[str, Any],
    landed_source: Mapping[str, Any],
    patch: TapMetadataPatchV1,
) -> None:
    """Prove the landed Formula commit is the exact CAS patch from its base."""

    root = tap_root.resolve(strict=True)
    if not isinstance(patch, TapMetadataPatchV1) or patch.operation != "formula-metadata":
        raise TapMetadataError("landed Formula metadata patch protocol changed")
    base = _mapping(base_source, "Formula metadata base source")
    landed = _mapping(landed_source, "Formula metadata landed source")
    for value, field in ((base, "base"), (landed, "landed")):
        _exact_keys(
            value,
            frozenset({"repository", "commit", "tree"}),
            f"Formula metadata {field} source",
        )
        _git_sha(value["commit"], f"Formula metadata {field} commit")
        _git_sha(value["tree"], f"Formula metadata {field} tree")
    policy = load_promotion_policy(root / "Kandelo/staging/promotion-policy.toml")
    if (
        base["repository"].lower() != policy.tap_repository.lower()
        or landed["repository"].lower() != policy.tap_repository.lower()
        or patch.expected_main_commit != base["commit"]
        or patch.expected_main_tree != base["tree"]
    ):
        raise TapMetadataError("landed Formula metadata names another CAS base")
    if (
        _git(root, "rev-parse", f"{base['commit']}^{{tree}}") != base["tree"]
        or _git(root, "rev-parse", f"{landed['commit']}^{{tree}}")
        != landed["tree"]
    ):
        raise TapMetadataError("landed Formula metadata Git tree identity changed")
    parents = _git(root, "rev-list", "--parents", "-n", "1", landed["commit"]).split()
    if len(parents) != 2 or parents[0] != landed["commit"] or parents[1] != base["commit"]:
        raise TapMetadataError("landed Formula metadata is not the exact CAS child")
    _git(root, "merge-base", "--is-ancestor", landed["commit"], "HEAD")
    changed_paths = _git(
        root,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        landed["commit"],
    ).splitlines()
    if sorted(changed_paths) != sorted(patch.files):
        raise TapMetadataError("landed Formula metadata changed another path set")
    for path, expected in patch.files.items():
        if _git_bytes(root, "show", f"{landed['commit']}:{path}") != expected:
            raise TapMetadataError("landed Formula metadata bytes differ from the patch")


class GitTapMetadataStore:
    """One disposable protected checkout with an optional normal Git remote."""

    def __init__(
        self,
        root: Path,
        *,
        remote: str | None = None,
        branch: str = "main",
    ) -> None:
        self.root = root.resolve(strict=True)
        actual = Path(_git(self.root, "rev-parse", "--show-toplevel")).resolve(
            strict=True
        )
        if actual != self.root:
            raise TapMetadataWriteError(
                "metadata writer root differs from its Git checkout",
                guard_code="tap_source_drift",
            )
        if branch != "main" and re.fullmatch(r"abi/(0|[1-9][0-9]{0,9})", branch) is None:
            raise TapMetadataWriteError(
                "metadata writer branch is unsupported",
                guard_code="tap_source_drift",
            )
        if remote is not None and (
            not isinstance(remote, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", remote) is None
        ):
            raise TapMetadataWriteError(
                "metadata writer remote is unsupported",
                guard_code="tap_source_drift",
            )
        self.remote = remote
        self.branch = branch

    def local_source(self) -> dict[str, str]:
        policy = load_promotion_policy(
            self.root / "Kandelo/staging/promotion-policy.toml"
        )
        return {
            "repository": policy.tap_repository,
            "commit": _git(self.root, "rev-parse", "HEAD"),
            "tree": _git(self.root, "rev-parse", "HEAD^{tree}"),
        }

    def require_clean(self) -> None:
        if _git(self.root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise TapMetadataWriteError(
                "metadata writer checkout has an unexpected file change",
                guard_code="tap_source_drift",
            )

    def remote_main(self) -> str | None:
        if self.remote is None:
            return None
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "ls-remote",
                    "--refs",
                    self.remote,
                    f"refs/heads/{self.branch}",
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise TapMetadataWriteError(
                f"cannot read metadata remote: {error}",
                guard_code="tap_source_drift",
            ) from error
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[:4096]
            raise TapMetadataWriteError(
                f"cannot read metadata remote: {detail}",
                guard_code="tap_source_drift",
            )
        try:
            output = result.stdout.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError as error:
            raise TapMetadataWriteError(
                "metadata remote response is not ASCII",
                guard_code="tap_source_drift",
            ) from error
        lines = output.splitlines() if output else []
        if len(lines) != 1:
            raise TapMetadataWriteError(
                "metadata remote main ref is absent or ambiguous",
                guard_code="tap_source_drift",
            )
        match = re.fullmatch(
            rf"([0-9a-f]{{40}})\trefs/heads/{re.escape(self.branch)}", lines[0]
        )
        if match is None:
            raise TapMetadataWriteError(
                "metadata remote main ref is malformed",
                guard_code="tap_source_drift",
            )
        return match.group(1)

    def commit(
        self, patch: TapMetadataPatchV1, *, commit_message: str
    ) -> dict[str, str]:
        message = _text(commit_message, "metadata commit message", 4096)
        self.require_clean()
        if not patch.files:
            raise TapMetadataWriteError(
                "metadata writer cannot commit an empty patch",
                guard_code="tap_source_drift",
        )
        for path, body in patch.files.items():
            destination = self.root / path
            expected_digest = patch.expected_files_sha256[path]
            if expected_digest is None:
                if destination.exists() or destination.is_symlink():
                    raise TapMetadataWriteError(
                        "metadata generated file creation raced another writer",
                        guard_code="tap_source_drift",
                    )
                try:
                    parent = destination.parent.resolve(strict=True)
                except OSError as error:
                    raise TapMetadataWriteError(
                        f"metadata output parent is unavailable: {error}",
                        guard_code="tap_source_drift",
                    ) from error
                if parent != self.root / "Kandelo/link":
                    raise TapMetadataWriteError(
                        "metadata created path escaped the exact link directory",
                        guard_code="tap_source_drift",
                    )
            else:
                try:
                    metadata = destination.lstat()
                except OSError as error:
                    raise TapMetadataWriteError(
                        f"metadata output path is unavailable: {error}",
                        guard_code="tap_source_drift",
                    ) from error
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise TapMetadataWriteError(
                        "metadata output path is not a regular file",
                        guard_code="tap_source_drift",
                    )
                if hashlib.sha256(destination.read_bytes()).hexdigest() != expected_digest:
                    raise TapMetadataWriteError(
                        "metadata generated file changed before commit",
                        guard_code="tap_source_drift",
                    )
            destination.write_bytes(body)
        try:
            changed_bytes = _git_bytes(
                self.root, "diff", "--name-only", "-z", "--"
            ) + _git_bytes(
                self.root,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            )
            changed = {
                path.decode("utf-8", errors="strict")
                for path in changed_bytes.split(b"\0")
                if path
            }
        except (TapMetadataError, UnicodeDecodeError) as error:
            raise TapMetadataWriteError(
                f"cannot enumerate metadata writer changes: {error}",
                guard_code="tap_source_drift",
            ) from error
        if changed != set(patch.files):
            raise TapMetadataWriteError(
                "metadata writer changed an unexpected path: "
                f"expected {sorted(patch.files)!r}, observed {sorted(changed)!r}",
                guard_code="tap_source_drift",
            )
        _git(self.root, "add", "--", *patch.files)
        staged = set(
            filter(
                None,
                _git(self.root, "diff", "--cached", "--name-only", "--").splitlines(),
            )
        )
        if staged != set(patch.files):
            raise TapMetadataWriteError(
                "metadata writer staged an unexpected path",
                guard_code="tap_source_drift",
            )
        _git(self.root, "commit", "-m", message)
        if _git(self.root, "rev-parse", "HEAD^") != patch.expected_main_commit:
            raise TapMetadataWriteError(
                "metadata commit parent differs from expected main",
                guard_code="tap_source_drift",
            )
        committed = set(
            filter(
                None,
                _git(
                    self.root,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "HEAD",
                ).splitlines(),
            )
        )
        if committed != set(patch.files):
            raise TapMetadataWriteError(
                "metadata commit contains an unexpected path",
                guard_code="tap_source_drift",
            )
        self.require_clean()
        return self.local_source()

    def push(self, expected_main: str, new_commit: str) -> None:
        _git_sha(expected_main, "metadata push expected main")
        _git_sha(new_commit, "metadata push commit")
        if self.remote is None:
            return
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "push",
                    self.remote,
                    f"{new_commit}:refs/heads/{self.branch}",
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise TapMetadataWriteError(
                f"metadata CAS push failed: {error}",
                guard_code="metadata_cas_conflict",
            ) from error
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[:4096]
            raise TapMetadataWriteError(
                f"metadata CAS push was rejected: {detail}",
                guard_code="metadata_cas_conflict",
            )


def apply_metadata_patch(
    tap_root: Path,
    patch: TapMetadataPatchV1,
    *,
    formula_update: FormulaMetadataUpdateV1 | None = None,
    commit_message: str,
    store: GitTapMetadataStore | None = None,
) -> TapMetadataWriteResultV1:
    root = tap_root.resolve(strict=True)
    if not isinstance(patch, TapMetadataPatchV1):
        raise TapMetadataWriteError(
            "metadata patch protocol is unsupported",
            guard_code="tap_source_drift",
        )
    try:
        if patch.operation == "successor-activation":
            if formula_update is not None:
                raise TapMetadataError(
                    "successor activation cannot carry a Formula update"
                )
            validate_successor_activation_patch(root, patch)
        elif patch.operation == "formula-metadata":
            if formula_update is None:
                raise TapMetadataError(
                    "Formula metadata writer requires its exact update"
                )
            validate_formula_metadata_patch(root, patch, formula_update)
        else:
            raise TapMetadataError("metadata patch operation is unsupported")
    except TapMetadataError as error:
        raise TapMetadataWriteError(
            f"metadata patch failed semantic revalidation: {error}",
            guard_code="tap_source_drift",
        ) from error
    writer = GitTapMetadataStore(root) if store is None else store
    if not isinstance(writer, GitTapMetadataStore) or writer.root != root:
        raise TapMetadataWriteError(
            "metadata writer does not own this exact checkout",
            guard_code="tap_source_drift",
        )
    try:
        writer.require_clean()
        current = writer.local_source()
    except TapMetadataWriteError:
        raise
    except TapMetadataError as error:
        raise TapMetadataWriteError(
            f"cannot verify metadata source: {error}",
            guard_code="tap_source_drift",
        ) from error
    if (
        current["commit"] != patch.expected_main_commit
        or current["tree"] != patch.expected_main_tree
    ):
        raise TapMetadataWriteError(
            "tap main moved before metadata commit",
            guard_code="tap_source_drift",
        )
    if (
        list(patch.allowed_paths) != list(dict.fromkeys(patch.allowed_paths))
        or set(patch.expected_files_sha256) != set(patch.allowed_paths)
        or not set(patch.files).issubset(patch.allowed_paths)
    ):
        raise TapMetadataWriteError(
            "metadata patch path set is not exact",
            guard_code="tap_source_drift",
        )
    for path in patch.allowed_paths:
        destination = root / path
        expected_digest = patch.expected_files_sha256[path]
        if expected_digest is None:
            if (
                formula_update is None
                or path != formula_update.link_manifest_path
                or destination.exists()
                or destination.is_symlink()
            ):
                raise TapMetadataWriteError(
                    "metadata generated file creation changed before CAS",
                    guard_code="tap_source_drift",
                )
            continue
        if (
            not destination.is_file()
            or destination.is_symlink()
            or hashlib.sha256(destination.read_bytes()).hexdigest() != expected_digest
        ):
            raise TapMetadataWriteError(
                "metadata generated file changed before CAS",
                guard_code="tap_source_drift",
            )
    remote_before = writer.remote_main()
    if remote_before is not None and remote_before != patch.expected_main_commit:
        raise TapMetadataWriteError(
            "tap main moved before metadata commit",
            guard_code="tap_source_drift",
        )
    if not patch.files:
        return TapMetadataWriteResultV1(
            status="already-landed",
            source=MappingProxyType(current),
            changed_paths=(),
        )
    committed = writer.commit(patch, commit_message=commit_message)
    remote_recheck = writer.remote_main()
    if remote_recheck is not None and remote_recheck != patch.expected_main_commit:
        raise TapMetadataWriteError(
            "tap main moved immediately before metadata push",
            guard_code="tap_source_drift",
        )
    writer.push(patch.expected_main_commit, committed["commit"])
    remote_after = writer.remote_main()
    if remote_after is not None and remote_after != committed["commit"]:
        raise TapMetadataWriteError(
            "metadata push lacks exact public ref readback",
            guard_code="metadata_cas_conflict",
        )
    return TapMetadataWriteResultV1(
        status="committed",
        source=MappingProxyType(committed),
        changed_paths=tuple(sorted(patch.files)),
    )
