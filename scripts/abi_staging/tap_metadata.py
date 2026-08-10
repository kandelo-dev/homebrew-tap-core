"""Strict current-ABI state and generated tap metadata projections.

The checked-in metadata index selects the active legacy Formula projection.
Unselected sidecars and detached Formula bottle blocks are inventoried so they
cannot disappear silently, but they are not promoted to current authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import stat
import tomllib
from types import MappingProxyType
from typing import Any, Literal

from .canonical import CanonicalJsonError, canonical_bytes, canonical_sha256, parse_canonical_bytes
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
                r'    sha256 cellar: ("[^"\n]+"|:any_skip_relocation), '
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
    _digest(bottle.get("sha256"), f"{field} SHA-256")
    _positive_integer(bottle.get("bytes"), f"{field} bytes")
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
    if activation.mode == "active" and state.activation is None:
        raise TapMetadataError("active promotion requires exact managed ABI activation")
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
        sidecar_bottles = {item["arch"]: item["sha256"] for item in package["bottles"]}
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
