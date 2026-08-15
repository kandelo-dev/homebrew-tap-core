"""Protected policy for generic ABI candidate planning and capture."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
import tomllib
from typing import Any

from .canonical import canonical_bytes, canonical_sha256


ARCHITECTURES = ("wasm32", "wasm64")
PROFILE_KEYS = frozenset(
    {"profiles", "kandelo_paths", "tap_paths", "environment_policy"}
)
FORMULA_KEYS = frozenset(
    {"name", "architectures", "profiles", "kandelo_paths", "tap_paths"}
)
STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


class PolicyError(ValueError):
    """Raised when protected staging policy is incomplete or ambiguous."""


@dataclass(frozen=True)
class SourceCustodyPolicyV1:
    required_git_roles: tuple[str, ...]
    external_source_bytes: str


@dataclass(frozen=True)
class TapStagingPolicyV1:
    schema: int
    kind: str
    version: int
    tap_repository: str
    kandelo_repository: str
    candidate_owner: str
    candidate_repository_prefix: str
    candidate_suffix: str
    source_custody_suffix: str
    max_ready_subjects_per_cycle: int
    max_formulae: int
    max_edges: int
    max_handoff_files: int
    max_handoff_bytes: int
    max_record_bytes: int
    build_timeout_minutes: int
    verification_timeout_minutes: int
    automatic_retry_count: int
    retry_base_ms: int
    retry_cap_ms: int
    candidate_retention_days_after_unmerged_close: int
    source_custody: SourceCustodyPolicyV1


@dataclass(frozen=True)
class FormulaCaptureProfileV1:
    name: str
    profiles: tuple[str, ...]
    kandelo_paths: tuple[str, ...]
    tap_paths: tuple[str, ...]
    environment_policy: str


@dataclass(frozen=True)
class FormulaCaptureEntryV1:
    name: str
    architectures: tuple[str, ...]
    profiles: tuple[str, ...]
    kandelo_paths: tuple[str, ...]
    tap_paths: tuple[str, ...]


@dataclass(frozen=True)
class FormulaBuildInputPolicyV1:
    schema: int
    kind: str
    version: int
    profiles: Mapping[str, FormulaCaptureProfileV1]
    formulae: tuple[FormulaCaptureEntryV1, ...]


@dataclass(frozen=True)
class VerificationTestDefinitionV1:
    id: str
    hosts: tuple[str, ...]
    kandelo_paths: tuple[str, ...]
    policy: str
    sha256: str


def _read_toml(path: Path, maximum: int = 1024 * 1024) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise PolicyError(f"cannot read {path}: {error}") from error
    if not raw or len(raw) > maximum:
        raise PolicyError(f"{path} has an invalid byte size")
    try:
        value = tomllib.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PolicyError(f"{path} is invalid TOML: {error}") from error
    if not isinstance(value, Mapping):
        raise PolicyError(f"{path} must contain a TOML table")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise PolicyError(
            f"{field} fields changed: missing={sorted(expected - actual)!r} "
            f"extra={sorted(actual - expected)!r}"
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyError(f"{field} must be a table")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise PolicyError(f"{field} must be an array")
    return value


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise PolicyError(f"{field} is outside its accepted range")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise PolicyError(f"{field} must be a string")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise PolicyError(f"{field} is not UTF-8") from error
    if size < 1 or size > maximum or "\0" in value:
        raise PolicyError(f"{field} is outside its string bound")
    return value


def _stable_id(value: Any, field: str) -> str:
    text = _text(value, field, 128)
    if STABLE_ID.fullmatch(text) is None:
        raise PolicyError(f"{field} is not a stable identifier")
    return text


def _repository(value: Any, field: str) -> str:
    text = _text(value, field, 256)
    if REPOSITORY.fullmatch(text) is None:
        raise PolicyError(f"{field} is not owner/repository")
    return text


def _safe_path(value: Any, field: str) -> str:
    text = _text(value, field)
    parts = text.split("/")
    if (
        text.startswith("/")
        or "\\" in text
        or any(character in text for character in "*?[")
        or any(part in {"", ".", "..", ".git"} for part in parts)
    ):
        raise PolicyError(f"{field} is not an exact repository-relative path")
    return text


def _sorted_unique(
    value: Any,
    field: str,
    validator: Any,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    checked = tuple(validator(item, field) for item in _sequence(value, field))
    if not allow_empty and not checked:
        raise PolicyError(f"{field} must not be empty")
    if any(left >= right for left, right in zip(checked, checked[1:])):
        raise PolicyError(f"{field} must be sorted and duplicate-free")
    return checked


def load_tap_staging_policy(path: Path) -> TapStagingPolicyV1:
    value = _read_toml(path)
    expected = frozenset(
        {
            "schema",
            "kind",
            "version",
            "tap_repository",
            "kandelo_repository",
            "candidate_owner",
            "candidate_repository_prefix",
            "candidate_suffix",
            "source_custody_suffix",
            "max_ready_subjects_per_cycle",
            "max_formulae",
            "max_edges",
            "max_handoff_files",
            "max_handoff_bytes",
            "max_record_bytes",
            "build_timeout_minutes",
            "verification_timeout_minutes",
            "automatic_retry_count",
            "retry_base_ms",
            "retry_cap_ms",
            "candidate_retention_days_after_unmerged_close",
            "source_custody",
        }
    )
    _exact_keys(value, expected, "tap staging policy")
    if _integer(value["schema"], "tap policy schema", 1, 1) != 1:
        raise PolicyError("tap staging policy schema is unsupported")
    if value["kind"] != "kandelo-tap-staging-policy":
        raise PolicyError("tap staging policy kind is unsupported")
    version = _integer(value["version"], "tap policy version", 1, 2**32 - 1)
    tap_repository = _repository(value["tap_repository"], "tap repository")
    kandelo_repository = _repository(value["kandelo_repository"], "Kandelo repository")
    candidate_owner_value = _text(value["candidate_owner"], "candidate owner", 128)
    if tap_repository.split("/", 1)[0] != candidate_owner_value:
        raise PolicyError("candidate owner differs from the tap owner")
    candidate_owner = _stable_id(candidate_owner_value, "candidate owner")
    prefix = _text(value["candidate_repository_prefix"], "candidate repository prefix", 128)
    candidate_suffix = _text(value["candidate_suffix"], "candidate suffix", 64)
    custody_suffix = _text(value["source_custody_suffix"], "custody suffix", 64)
    if prefix != "homebrew-tap-core-abi-" or candidate_suffix != "-candidates":
        raise PolicyError("candidate namespace grammar changed")
    if custody_suffix != "-source-custody":
        raise PolicyError("source-custody namespace grammar changed")
    numeric_bounds = {
        "max_ready_subjects_per_cycle": (1, 256),
        "max_formulae": (1, 4096),
        "max_edges": (1, 65_536),
        "max_handoff_files": (1, 4096),
        "max_handoff_bytes": (1, 16 * 1024**3),
        "max_record_bytes": (1, 16 * 1024**2),
        "build_timeout_minutes": (1, 360),
        "verification_timeout_minutes": (1, 360),
        "automatic_retry_count": (3, 3),
        "retry_base_ms": (1, 86_400_000),
        "retry_cap_ms": (1, 86_400_000),
        "candidate_retention_days_after_unmerged_close": (1, 3650),
    }
    numbers = {
        key: _integer(value[key], key, minimum, maximum)
        for key, (minimum, maximum) in numeric_bounds.items()
    }
    if numbers["build_timeout_minutes"] != 360 or numbers["verification_timeout_minutes"] != 360:
        raise PolicyError("candidate execution timeouts must be exactly six hours")
    if numbers["retry_cap_ms"] < numbers["retry_base_ms"]:
        raise PolicyError("retry cap cannot be below retry base")
    custody_value = _mapping(value["source_custody"], "source custody policy")
    _exact_keys(
        custody_value,
        frozenset({"required_git_roles", "external_source_bytes"}),
        "source custody policy",
    )
    roles = tuple(
        _stable_id(item, "source-custody Git role")
        for item in _sequence(custody_value["required_git_roles"], "source-custody Git roles")
    )
    if roles != ("kandelo", "tap", "pinned-submodule"):
        raise PolicyError("source-custody Git roles changed")
    if custody_value["external_source_bytes"] != "deferred":
        raise PolicyError("source custody cannot claim complete external-source bytes")
    return TapStagingPolicyV1(
        schema=1,
        kind="kandelo-tap-staging-policy",
        version=version,
        tap_repository=tap_repository,
        kandelo_repository=kandelo_repository,
        candidate_owner=candidate_owner,
        candidate_repository_prefix=prefix,
        candidate_suffix=candidate_suffix,
        source_custody_suffix=custody_suffix,
        source_custody=SourceCustodyPolicyV1(roles, "deferred"),
        **numbers,
    )


def candidate_repository(
    policy: TapStagingPolicyV1, target_abi: int, *, formula: str
) -> str:
    abi = _integer(target_abi, "target ABI", 1, 2**32 - 1)
    name = _stable_id(formula, "Formula name")
    return (
        f"{policy.candidate_owner}/{policy.candidate_repository_prefix}{abi}"
        f"{policy.candidate_suffix}/{name}"
    )


def attempt_repository(
    policy: TapStagingPolicyV1, target_abi: int, *, formula: str
) -> str:
    return candidate_repository(policy, target_abi, formula=formula) + "/attempts"


def candidate_reuse_repository(
    policy: TapStagingPolicyV1, target_abi: int, *, formula: str
) -> str:
    return candidate_repository(policy, target_abi, formula=formula)


def source_custody_repository(policy: TapStagingPolicyV1, target_abi: int) -> str:
    abi = _integer(target_abi, "target ABI", 1, 2**32 - 1)
    return (
        f"{policy.candidate_owner}/{policy.candidate_repository_prefix}{abi}"
        f"{policy.source_custody_suffix}"
    )


def _parse_profile(name: str, value: Any) -> FormulaCaptureProfileV1:
    profile = _mapping(value, f"capture profile {name}")
    _exact_keys(profile, PROFILE_KEYS, f"capture profile {name}")
    return FormulaCaptureProfileV1(
        name=_stable_id(name, "capture profile name"),
        profiles=_sorted_unique(profile["profiles"], f"profile parents for {name}", _stable_id),
        kandelo_paths=_sorted_unique(
            profile["kandelo_paths"], f"Kandelo paths for profile {name}", _safe_path
        ),
        tap_paths=_sorted_unique(profile["tap_paths"], f"tap paths for profile {name}", _safe_path),
        environment_policy=_stable_id(
            profile["environment_policy"], f"environment policy for profile {name}"
        ),
    )


def _check_profile_graph(profiles: Mapping[str, FormulaCaptureProfileV1]) -> None:
    state: dict[str, int] = {}

    def visit(name: str, trail: tuple[str, ...]) -> None:
        if name not in profiles:
            raise PolicyError(f"capture profile {trail[-1]} names missing parent {name}")
        if state.get(name) == 1:
            raise PolicyError(f"capture profile cycle: {' -> '.join(trail + (name,))}")
        if state.get(name) == 2:
            return
        state[name] = 1
        for parent in profiles[name].profiles:
            visit(parent, trail + (name,))
        state[name] = 2

    for name in profiles:
        visit(name, ())


def load_formula_build_inputs(
    path: Path, *, tap_root: Path | None = None
) -> FormulaBuildInputPolicyV1:
    value = _read_toml(path, 4 * 1024 * 1024)
    _exact_keys(
        value,
        frozenset({"schema", "kind", "version", "profiles", "formulae"}),
        "Formula build-input policy",
    )
    if _integer(value["schema"], "Formula policy schema", 1, 1) != 1:
        raise PolicyError("Formula build-input policy schema is unsupported")
    if value["kind"] != "kandelo-formula-build-inputs":
        raise PolicyError("Formula build-input policy kind is unsupported")
    version = _integer(value["version"], "Formula policy version", 1, 2**32 - 1)
    profile_values = _mapping(value["profiles"], "capture profiles")
    if not profile_values:
        raise PolicyError("Formula capture profiles must not be empty")
    profiles = {
        name: _parse_profile(name, profile_value)
        for name, profile_value in profile_values.items()
    }
    if tuple(profiles) != tuple(sorted(profiles)):
        raise PolicyError("capture profiles must be sorted by name")
    _check_profile_graph(profiles)

    formulae: list[FormulaCaptureEntryV1] = []
    for index, item in enumerate(_sequence(value["formulae"], "Formula capture entries")):
        entry = _mapping(item, f"Formula capture entry {index}")
        _exact_keys(entry, FORMULA_KEYS, f"Formula capture entry {index}")
        name = _stable_id(entry["name"], "Formula name")
        architectures = _sorted_unique(
            entry["architectures"], f"architectures for {name}", _stable_id, allow_empty=False
        )
        if any(architecture not in ARCHITECTURES for architecture in architectures):
            raise PolicyError(f"Formula {name} names an unsupported architecture")
        parents = _sorted_unique(
            entry["profiles"], f"profiles for {name}", _stable_id, allow_empty=False
        )
        for parent in parents:
            if parent not in profiles:
                raise PolicyError(f"Formula {name} names missing profile {parent}")
        formulae.append(
            FormulaCaptureEntryV1(
                name=name,
                architectures=architectures,
                profiles=parents,
                kandelo_paths=_sorted_unique(
                    entry["kandelo_paths"], f"Kandelo paths for {name}", _safe_path
                ),
                tap_paths=_sorted_unique(entry["tap_paths"], f"tap paths for {name}", _safe_path),
            )
        )
    names = tuple(entry.name for entry in formulae)
    if not names or any(left >= right for left, right in zip(names, names[1:])):
        raise PolicyError("Formula capture entries must be sorted and duplicate-free")
    if tap_root is not None:
        root = tap_root.resolve(strict=True)
        actual = tuple(sorted(candidate.stem for candidate in (root / "Formula").glob("*.rb")))
        if names != actual:
            raise PolicyError(
                f"Formula capture inventory differs from direct Formula files: "
                f"missing={sorted(set(actual) - set(names))!r} "
                f"extra={sorted(set(names) - set(actual))!r}"
            )
    return FormulaBuildInputPolicyV1(1, "kandelo-formula-build-inputs", version, profiles, tuple(formulae))


def _expand_profile(
    name: str,
    profiles: Mapping[str, FormulaCaptureProfileV1],
    memo: dict[str, tuple[set[str], set[str], set[str]]],
) -> tuple[set[str], set[str], set[str]]:
    if name in memo:
        kandelo, tap, environments = memo[name]
        return set(kandelo), set(tap), set(environments)
    profile = profiles[name]
    kandelo = set(profile.kandelo_paths)
    tap = set(profile.tap_paths)
    environments = {profile.environment_policy}
    for parent in profile.profiles:
        parent_kandelo, parent_tap, parent_environments = _expand_profile(parent, profiles, memo)
        kandelo.update(parent_kandelo)
        tap.update(parent_tap)
        environments.update(parent_environments)
    memo[name] = (set(kandelo), set(tap), set(environments))
    return kandelo, tap, environments


def _path_covers(captured: set[str], observed: str) -> bool:
    return any(observed == candidate or observed.startswith(f"{candidate}/") for candidate in captured)


def _path_uses_symlink(root: Path, relative: str) -> bool:
    current = root
    for component in relative.split("/"):
        current /= component
        if current.is_symlink():
            return True
    return False


def _observed_paths(name: str, formula_source: str) -> tuple[set[str], set[str]]:
    kandelo: set[str] = set()
    tap = {"Kandelo/formula_support/kandelo_formula_support.rb"}
    package_match = re.search(
        r"kandelo_build_package\s*\(\s*(?:package:\s*\"([^\"]+)\")?",
        formula_source,
        flags=re.MULTILINE,
    )
    if package_match is not None:
        package = package_match.group(1) or name
        kandelo.add(f"packages/registry/{package}")
    if "kandelo_build_tap_recipe" in formula_source:
        tap.add(f"Kandelo/recipes/{name}")
    for observed in re.findall(
        r"Kandelo/(?:formula_support|patches)/[A-Za-z0-9_./-]+",
        formula_source,
    ):
        if observed != "Kandelo/formula_support/kandelo_formula_support":
            tap.add(observed)
    kandelo.update(
        match
        for match in re.findall(r'root/\"([^\"]+)\"', formula_source)
        if match.startswith(("images/", "scripts/"))
    )
    kandelo.update(
        match
        for match in re.findall(r"#\{root\}/([A-Za-z0-9_./-]+)", formula_source)
        if match.startswith(("images/", "scripts/"))
    )
    return kandelo, tap


def _observed_architectures(name: str, formula_source: str) -> tuple[str, ...]:
    calls = re.findall(r"kandelo_require_arch!\(([^)]*)\)", formula_source)
    if len(calls) > 1:
        raise PolicyError(f"Formula {name} has ambiguous architecture declarations")
    if calls:
        architectures = tuple(sorted(set(re.findall(r'\"(wasm(?:32|64))\"', calls[0]))))
        if not architectures:
            raise PolicyError(f"Formula {name} has an unreadable architecture declaration")
        return architectures
    bottle_architectures = tuple(
        sorted(set(re.findall(r"\b(wasm(?:32|64))_kandelo\b", formula_source)))
    )
    if not bottle_architectures:
        raise PolicyError(f"Formula {name} has no explicit supported architecture evidence")
    return bottle_architectures


def generate_formula_capture_catalog(
    tap_root: Path, policy: FormulaBuildInputPolicyV1
) -> dict[str, Any]:
    root = tap_root.resolve(strict=True)
    memo: dict[str, tuple[set[str], set[str], set[str]]] = {}
    generated: list[dict[str, Any]] = []
    for entry in policy.formulae:
        kandelo = set(entry.kandelo_paths)
        tap = set(entry.tap_paths)
        environments: set[str] = set()
        for profile_name in entry.profiles:
            profile_kandelo, profile_tap, profile_environments = _expand_profile(
                profile_name, policy.profiles, memo
            )
            kandelo.update(profile_kandelo)
            tap.update(profile_tap)
            environments.update(profile_environments)
        if len(environments) != 1:
            raise PolicyError(f"Formula {entry.name} does not resolve one environment policy")
        formula_path = f"Formula/{entry.name}.rb"
        formula_file = root / formula_path
        if not formula_file.is_file() or formula_file.is_symlink():
            raise PolicyError(f"Formula {entry.name} is not one direct regular Formula file")
        tap.add(formula_path)
        try:
            source = formula_file.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as error:
            raise PolicyError(f"cannot audit Formula {entry.name}: {error}") from error
        observed_architectures = _observed_architectures(entry.name, source)
        if entry.architectures != observed_architectures:
            raise PolicyError(
                f"Formula {entry.name} capture architectures {entry.architectures!r} "
                f"differ from source evidence {observed_architectures!r}"
            )
        observed_kandelo, observed_tap = _observed_paths(entry.name, source)
        for architecture in entry.architectures:
            for repository, observed, captured in (
                ("Kandelo", observed_kandelo, kandelo),
                ("tap", observed_tap, tap),
            ):
                missing = sorted(path for path in observed if not _path_covers(captured, path))
                if missing:
                    subject = canonical_bytes(
                        {
                            "architecture": architecture,
                            "identity": entry.name,
                            "kind": "formula",
                        }
                    ).decode("utf-8").strip()
                    raise PolicyError(
                        f"Formula {entry.name} architecture {architecture} has incomplete "
                        f"{repository} input capture: missing={missing!r}; "
                        f"override_subject={subject}"
                    )
        for captured in sorted(tap):
            candidate = root / captured
            if not candidate.exists() or _path_uses_symlink(root, captured):
                raise PolicyError(f"Formula {entry.name} tap input is unavailable: {captured}")
        identity = {
            "architectures": list(entry.architectures),
            "environment_policy": next(iter(environments)),
            "kandelo_paths": sorted(kandelo),
            "name": entry.name,
            "tap_paths": sorted(tap),
        }
        generated.append({**identity, "capture_policy_sha256": canonical_sha256(identity)})
    return {
        "formulae": generated,
        "kind": "kandelo-formula-build-input-catalog",
        "schema": 1,
        "source_policy_version": policy.version,
    }


def load_candidate_publication_activation(path: Path) -> str:
    value = _read_toml(path, 4096)
    _exact_keys(
        value,
        frozenset({"schema", "kind", "mode"}),
        "candidate publication activation",
    )
    if (
        _integer(value["schema"], "activation schema", 1, 1) != 1
        or value["kind"] != "kandelo-candidate-publication-activation"
        or value["mode"] not in {"observe", "active"}
    ):
        raise PolicyError("candidate publication activation is unsupported")
    return value["mode"]


def load_verification_tests(path: Path) -> tuple[VerificationTestDefinitionV1, ...]:
    value = _read_toml(path)
    _exact_keys(
        value,
        frozenset({"schema", "kind", "version", "tests"}),
        "verification-test registry",
    )
    if (
        _integer(value["schema"], "verification registry schema", 1, 1) != 1
        or value["kind"] != "kandelo-candidate-verification-tests"
        or _integer(value["version"], "verification registry version", 1, 2**32 - 1) < 1
    ):
        raise PolicyError("verification-test registry is unsupported")
    result: list[VerificationTestDefinitionV1] = []
    for index, item in enumerate(_sequence(value["tests"], "verification tests")):
        definition = _mapping(item, f"verification test {index}")
        _exact_keys(
            definition,
            frozenset({"id", "hosts", "kandelo_paths", "policy"}),
            f"verification test {index}",
        )
        test_id = _stable_id(definition["id"], "verification test ID")
        hosts = _sorted_unique(
            definition["hosts"], f"verification hosts for {test_id}", _stable_id, allow_empty=False
        )
        if any(host not in {"browser", "build", "node"} for host in hosts):
            raise PolicyError(f"verification test {test_id} names an unsupported host")
        paths = _sorted_unique(
            definition["kandelo_paths"], f"verification paths for {test_id}", _safe_path, allow_empty=False
        )
        policy = _stable_id(definition["policy"], f"verification policy for {test_id}")
        identity = {
            "hosts": list(hosts),
            "id": test_id,
            "kandelo_paths": list(paths),
            "policy": policy,
        }
        result.append(
            VerificationTestDefinitionV1(test_id, hosts, paths, policy, canonical_sha256(identity))
        )
    ids = tuple(item.id for item in result)
    if not ids or any(left >= right for left, right in zip(ids, ids[1:])):
        raise PolicyError("verification tests must be sorted and duplicate-free")
    return tuple(result)


def check_policy_files(tap_root: Path) -> None:
    staging = tap_root / "Kandelo/staging"
    load_tap_staging_policy(staging / "tap-policy.toml")
    policy = load_formula_build_inputs(
        staging / "formula-build-inputs.toml", tap_root=tap_root
    )
    expected = canonical_bytes(generate_formula_capture_catalog(tap_root, policy))
    try:
        actual = (staging / "generated/formula-build-inputs.json").read_bytes()
    except OSError as error:
        raise PolicyError(f"cannot read generated Formula capture catalog: {error}") from error
    if actual != expected:
        raise PolicyError("generated Formula capture catalog is stale")
    load_candidate_publication_activation(
        staging / "candidate-publication-activation.toml"
    )
    load_verification_tests(staging / "verification-tests.toml")


def write_formula_capture_catalog(tap_root: Path, destination: Path) -> None:
    expected_destination = tap_root / "Kandelo/staging/generated/formula-build-inputs.json"
    if destination.resolve(strict=False) != expected_destination.resolve(strict=False):
        raise PolicyError("generated Formula capture output must use its protected path")
    policy = load_formula_build_inputs(
        tap_root / "Kandelo/staging/formula-build-inputs.toml", tap_root=tap_root
    )
    body = canonical_bytes(generate_formula_capture_catalog(tap_root, policy))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _path_uses_symlink(tap_root, "Kandelo/staging/generated"):
        raise PolicyError("generated Formula capture parent traverses a symlink")
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise PolicyError("generated Formula capture destination is not a regular file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
