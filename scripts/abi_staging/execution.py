"""Fail-closed preparation for uncredentialed ABI-staging work."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any

from .canonical import CanonicalJsonError, canonical_bytes, canonical_sha256, parse_canonical_bytes
from .coordination import CoordinationError, validate_coordination_bundle
from .handoff import HandoffError, load_build_run
from .oci import UrllibOciTransportV1, fetch_public_record
from .plan import exact_formula_subject
from .plan import snapshot_tap_source
from .policy import TapStagingPolicyV1
from .records import CANDIDATE_RECORD_MEDIA_TYPE, validate_candidate_record


MAX_COORDINATION_BYTES = 64 * 1024 * 1024
MAX_VFS_COMPOSITION_JSON_ITEMS = 4_000_000
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExecutionError(ValueError):
    """Raised when protected work cannot be tied to exact coordinated inputs."""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(child) for child in value]
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionError(f"{field} must be an object")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ExecutionError(f"{field} is not a lowercase SHA-256 digest")
    return value


def load_coordination_bundle(
    path: Path, *, policy: TapStagingPolicyV1
) -> dict[str, Any]:
    """Load one canonical active coordination bundle within protected bounds."""

    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ExecutionError("coordination bundle must be a regular non-symlink file")
        body = path.read_bytes()
        parsed = parse_canonical_bytes(body, maximum_bytes=MAX_COORDINATION_BYTES)
        bundle = dict(_mapping(_plain(parsed), "coordination bundle"))
        validate_coordination_bundle(
            bundle, max_ready_subjects=policy.max_ready_subjects_per_cycle
        )
    except (OSError, CanonicalJsonError, CoordinationError) as error:
        if isinstance(error, ExecutionError):
            raise
        raise ExecutionError(f"coordination bundle is invalid: {error}") from error
    if bundle["mode"] != "active":
        raise ExecutionError("observe-only coordination cannot execute work")
    return bundle


def _formula_for_subject(
    bundle: Mapping[str, Any], subject: str
) -> Mapping[str, Any]:
    matches = []
    for candidate in bundle["tap_plan"]["formulae"]:
        identity = candidate["identity"]
        if exact_formula_subject(identity["name"], identity["architecture"]) == subject:
            matches.append(candidate)
    if len(matches) != 1:
        raise ExecutionError("build work does not name one exact Formula plan")
    return matches[0]


def _expected_build_work_id(bundle: Mapping[str, Any], work: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {
            "action": "build-candidate",
            "attempt_ordinal": work["attempt_ordinal"],
            "candidate_record_sha256": None,
            "contract_sha256": work["contract_sha256"],
            "host": None,
            "request_sha256": bundle["request_sha256"],
            "subject": json.loads(work["subject"]),
            "test_definition_sha256": None,
        }
    )


def select_build_work(
    bundle: Mapping[str, Any], work_id: str
) -> dict[str, Any]:
    """Select one content-addressed build item and rebind all of its inputs."""

    _digest(work_id, "build work ID")
    workflow = _mapping(bundle.get("workflow"), "coordination workflow")
    matches = [item for item in workflow.get("build_work", ()) if item.get("work_id") == work_id]
    if len(matches) != 1:
        raise ExecutionError("coordination bundle does not contain the exact build work ID")
    work = dict(_mapping(matches[0], "build work"))
    if work.get("action") != "build-candidate" or _expected_build_work_id(bundle, work) != work_id:
        raise ExecutionError("build work identity differs from its coordinated inputs")
    formula = _formula_for_subject(bundle, work["subject"])
    if (
        canonical_sha256(formula) != work["formula_plan_sha256"]
        or formula.get("contract_sha256") != work["contract_sha256"]
    ):
        raise ExecutionError("build work differs from its exact Formula plan")
    contracts = _mapping(bundle.get("contracts"), "coordination contracts")
    assessments = _mapping(
        bundle.get("capture_assessments"), "coordination capture assessments"
    )
    contract = _mapping(contracts.get(work["subject"]), "build contract")
    assessment = _mapping(assessments.get(work["subject"]), "capture assessment")
    if canonical_sha256(contract) != work["contract_sha256"] or not assessment.get(
        "complete"
    ):
        raise ExecutionError("build work lacks one complete exact bottle contract")
    return work


def _create_output_directory(path: Path) -> Path:
    try:
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ExecutionError("build input destination must be a real directory")
            if any(path.iterdir()):
                raise ExecutionError("build input destination must be empty")
        else:
            path.mkdir(parents=True)
        (path / "contracts").mkdir()
        (path / "layers").mkdir()
    except OSError as error:
        raise ExecutionError(f"cannot prepare build input destination: {error}") from error
    return path


def _matching_dependency(
    bundle: Mapping[str, Any], dependency: Mapping[str, Any]
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    candidates = _mapping(bundle["candidates"], "coordination candidates")
    records = _mapping(candidates["records"], "coordination candidate records")
    locators = _mapping(candidates["locators"], "coordination candidate locators")
    matches: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    for record_sha256, candidate_value in records.items():
        candidate = _mapping(candidate_value, "dependency candidate")
        payload = _mapping(candidate.get("candidate"), "dependency candidate payload")
        formula = _mapping(payload.get("formula"), "dependency candidate Formula")
        layer = _mapping(payload.get("bottle_layer"), "dependency candidate layer")
        common = _mapping(candidate.get("common"), "dependency candidate common")
        if (
            common.get("request_sha256") == bundle["request_sha256"]
            and formula.get("tap") == bundle["tap_plan"]["tap_source"]["repository"]
            and formula.get("formula") == dependency["formula"]
            and formula.get("architecture") == dependency["architecture"]
            and formula.get("target_abi") == bundle["tap_plan"]["target_abi"]["version"]
            and layer.get("sha256") == dependency["bottle_layer_sha256"]
            and layer.get("bytes") == dependency["bottle_layer_bytes"]
        ):
            locator = _mapping(locators.get(record_sha256), "dependency candidate locator")
            matches.append((record_sha256, candidate, locator))
    reuse_bindings = _mapping(
        bundle.get("reuse_bindings", {}), "coordination reuse bindings"
    )
    reuse_records = _mapping(
        reuse_bindings.get("records", {}), "coordination reuse binding records"
    )
    for reuse_value in reuse_records.values():
        reuse = _mapping(reuse_value, "dependency reuse binding")
        common = _mapping(reuse.get("common"), "dependency reuse common")
        payload = _mapping(
            reuse.get("candidate_reuse"), "dependency reuse payload"
        )
        formula = _mapping(payload.get("formula"), "dependency reuse Formula")
        layer = _mapping(payload.get("bottle_layer"), "dependency reuse layer")
        if (
            common.get("request_sha256") != bundle["request_sha256"]
            or formula.get("tap") != bundle["tap_plan"]["tap_source"]["repository"]
            or formula.get("formula") != dependency["formula"]
            or formula.get("architecture") != dependency["architecture"]
            or formula.get("target_abi") != bundle["tap_plan"]["target_abi"]["version"]
            or layer.get("sha256") != dependency["bottle_layer_sha256"]
            or layer.get("bytes") != dependency["bottle_layer_bytes"]
        ):
            continue
        existing = _mapping(
            payload.get("existing_candidate"), "dependency reused candidate"
        )
        record_sha256 = existing.get("record_sha256")
        candidate = _mapping(records.get(record_sha256), "dependency candidate")
        locator = _mapping(locators.get(record_sha256), "dependency candidate locator")
        candidate_payload = _mapping(
            candidate.get("candidate"), "dependency candidate payload"
        )
        candidate_formula = _mapping(
            candidate_payload.get("formula"), "dependency candidate Formula"
        )
        candidate_layer = _mapping(
            candidate_payload.get("bottle_layer"), "dependency candidate layer"
        )
        if (
            locator.get("digest") != f"sha256:{record_sha256}"
            or locator.get("immutable_reference")
            != existing.get("immutable_reference")
            or candidate_formula.get("tap") != formula.get("tap")
            or candidate_formula.get("formula") != formula.get("formula")
            or candidate_formula.get("architecture") != formula.get("architecture")
            or candidate_formula.get("target_abi") != formula.get("target_abi")
            or candidate_formula.get("bottle_contract_sha256")
            != formula.get("bottle_contract_sha256")
            or candidate_layer.get("sha256") != layer.get("sha256")
            or candidate_layer.get("bytes") != layer.get("bytes")
        ):
            raise ExecutionError(
                "dependency reuse binding differs from its exact public candidate"
            )
        matches.append((record_sha256, candidate, locator))
    if not matches:
        raise ExecutionError("contract dependency lacks its exact public candidate")
    matches.sort(key=lambda item: item[0])
    _, record, locator = matches[0]
    return record, locator


def _fetched_layer(
    fetched: Any,
    *,
    record: Mapping[str, Any],
    locator: Mapping[str, Any],
    dependency: Mapping[str, Any],
) -> bytes:
    if (
        getattr(fetched, "artifact_type", None) != CANDIDATE_RECORD_MEDIA_TYPE
        or getattr(fetched, "digest", None) != locator.get("digest")
        or getattr(fetched, "immutable_reference", None)
        != locator.get("immutable_reference")
    ):
        raise ExecutionError("fetched dependency record differs from its exact locator")
    config = getattr(fetched, "config", None)
    if config is None or getattr(config, "body", None) != canonical_bytes(record):
        raise ExecutionError("fetched dependency config differs from its public record")
    layers = tuple(getattr(fetched, "layers", ()))
    if len(layers) != 1 or getattr(layers[0], "role", None) != "bottle-layer":
        raise ExecutionError("fetched dependency lacks one exact bottle layer")
    layer = layers[0]
    body = getattr(layer, "body", None)
    if (
        not isinstance(body, bytes)
        or hashlib.sha256(body).hexdigest() != dependency["bottle_layer_sha256"]
        or len(body) != dependency["bottle_layer_bytes"]
        or getattr(layer, "digest", None)
        != "sha256:" + dependency["bottle_layer_sha256"]
    ):
        raise ExecutionError("fetched dependency layer differs from its contract")
    return body


def prepare_build_inputs(
    bundle: Mapping[str, Any],
    work: Mapping[str, Any],
    *,
    destination: Path,
    run: Mapping[str, Any],
    fetch_candidate: Callable[[Mapping[str, Any]], Any],
) -> dict[str, Any]:
    """Materialize only the contract-declared inputs for one build subprocess."""

    selected = select_build_work(bundle, str(work.get("work_id", "")))
    if canonical_bytes(selected) != canonical_bytes(work):
        raise ExecutionError("caller-supplied build work differs from coordination")
    try:
        checked_run = load_build_run(
            canonical_bytes(run),
            expected_repository=bundle["tap_plan"]["tap_source"]["repository"],
        )
    except HandoffError as error:
        raise ExecutionError(f"protected build run is invalid: {error}") from error
    root = _create_output_directory(destination)
    formula = dict(_formula_for_subject(bundle, selected["subject"]))
    contract = dict(bundle["contracts"][selected["subject"]])
    assessment = dict(bundle["capture_assessments"][selected["subject"]])
    contract_sha256 = selected["contract_sha256"]

    documents = {
        root / "request.json": bundle["request"],
        root / "tap-plan.json": bundle["tap_plan"],
        root / "formula-plan.json": formula,
        root / "run.json": checked_run,
        root / "contracts" / f"sha256-{contract_sha256}.json": contract,
        root / "contracts" / f"sha256-{contract_sha256}.capture.json": assessment,
    }
    try:
        for path, value in documents.items():
            path.write_bytes(canonical_bytes(value))
        for dependency in contract["direct_dependencies"]:
            record, locator = _matching_dependency(bundle, dependency)
            validate_candidate_record(record)
            body = _fetched_layer(
                fetch_candidate(locator),
                record=record,
                locator=locator,
                dependency=dependency,
            )
            layer_path = root / "layers" / (
                f"sha256-{dependency['bottle_layer_sha256']}.tar.gz"
            )
            if layer_path.exists() and layer_path.read_bytes() != body:
                raise ExecutionError("dependency layer digest collision changed bytes")
            layer_path.write_bytes(body)
    except OSError as error:
        raise ExecutionError(f"cannot materialize exact build inputs: {error}") from error
    return {
        "root": root,
        "formula_plan": formula,
        "contract_sha256": contract_sha256,
        "request": root / "request.json",
        "tap_plan": root / "tap-plan.json",
        "formula_plan_path": root / "formula-plan.json",
        "run": root / "run.json",
        "dependency_root": root,
    }


_DECLARED_TOOL_ENVIRONMENT = frozenset(
    {
        "ACLOCAL_PATH",
        "AR",
        "AR_FOR_BUILD",
        "AS",
        "AS_FOR_BUILD",
        "CC",
        "CC_FOR_BUILD",
        "CI",
        "CMAKE_INCLUDE_PATH",
        "CMAKE_LIBRARY_PATH",
        "CONFIG_SHELL",
        "CURL_CA_BUNDLE",
        "CXX",
        "CXX_FOR_BUILD",
        "DETERMINISTIC_BUILD",
        "DEVELOPER_DIR",
        "GEM_PATH",
        "GITHUB_ACTIONS",
        "GIT_SSL_CAINFO",
        "HOMEBREW_BREW_COMMIT",
        "HOMEBREW_BREW_FILE",
        "HOMEBREW_CACHE",
        "HOMEBREW_DEVELOPER",
        "HOMEBREW_NO_ANALYTICS",
        "HOMEBREW_NO_AUTO_UPDATE",
        "HOMEBREW_NO_INSTALL_CLEANUP",
        "HOMEBREW_PREFIX",
        "HOMEBREW_REPOSITORY",
        "HOMEBREW_TEMP",
        "HOST_PATH",
        "IN_NIX_SHELL",
        "KANDELO_DEV_SHELL_TOOL_PATH",
        "KANDELO_HOMEBREW_BUILD_USER",
        "KANDELO_HOMEBREW_GETENT_BIN",
        "KANDELO_HOMEBREW_PGREP_BIN",
        "KANDELO_HOMEBREW_PKILL_BIN",
        "KANDELO_HOMEBREW_RECIPE_USER",
        "KANDELO_HOMEBREW_RESOLVED_TAPS_FILE",
        "KANDELO_HOMEBREW_SHARED_TEMP",
        "KANDELO_HOMEBREW_SUDO_BIN",
        "KANDELO_HOMEBREW_SYSTEMCTL_BIN",
        "KANDELO_HOMEBREW_SYSTEMD_RUN_BIN",
        "KANDELO_HOMEBREW_TAP_SOURCE_COMMIT",
        "LD",
        "LD_DYLD_PATH",
        "LD_FOR_BUILD",
        "LD_LIBRARY_PATH",
        "LLVM_BIN",
        "LLVM_PREFIX",
        "LLVM_VERSION",
        "LOGNAME",
        "MACOSX_DEPLOYMENT_TARGET",
        "NIXPKGS_CMAKE_PREFIX_PATH",
        "NIX_APPLE_SDK_VERSION",
        "NIX_BINTOOLS",
        "NIX_BINTOOLS_FOR_BUILD",
        "NIX_BUILD_CORES",
        "NIX_BUILD_TOP",
        "NIX_CC",
        "NIX_CC_FOR_BUILD",
        "NIX_CFLAGS_COMPILE",
        "NIX_CFLAGS_COMPILE_FOR_BUILD",
        "NIX_DONT_SET_RPATH",
        "NIX_DONT_SET_RPATH_FOR_BUILD",
        "NIX_ENFORCE_NO_NATIVE",
        "NIX_GCROOT",
        "NIX_HARDENING_ENABLE",
        "NIX_IGNORE_LD_THROUGH_GCC",
        "NIX_LDFLAGS",
        "NIX_LDFLAGS_FOR_BUILD",
        "NIX_NO_SELF_RPATH",
        "NIX_SSL_CERT_FILE",
        "NIX_STORE",
        "NM",
        "NM_FOR_BUILD",
        "NODE_PATH",
        "OBJCOPY",
        "OBJCOPY_FOR_BUILD",
        "OBJDUMP",
        "OBJDUMP_FOR_BUILD",
        "PATH",
        "PATH_LOCALE",
        "PERL5LIB",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PKG_CONFIG",
        "PKG_CONFIG_PATH",
        "PYTHONHASHSEED",
        "PYTHONNOUSERSITE",
        "RANLIB",
        "RANLIB_FOR_BUILD",
        "REQUESTS_CA_BUNDLE",
        "RUBYLIB",
        "SDKROOT",
        "SHELL",
        "SIZE",
        "SIZE_FOR_BUILD",
        "SOURCE_DATE_EPOCH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "STRINGS",
        "STRINGS_FOR_BUILD",
        "STRIP",
        "STRIP_FOR_BUILD",
        "SYSTEM_CERTIFICATE_PATH",
        "TERM",
        "USER",
        "WASM_POSIX_BINARY_CACHE_ROOT",
        "WASM_POSIX_XTASK_BIN",
        "WASM_POSIX_LLVM_LIBCXX_SOURCE",
        "WASM_POSIX_LLVM_LIBUNWIND_SOURCE",
        "XDG_DATA_DIRS",
        "ZERO_AR_DATE",
        "__darwinAllowLocalNetworking",
        "__impureHostDeps",
        "__propagatedImpureHostDeps",
        "__propagatedSandboxProfile",
        "__sandboxProfile",
        "__structuredAttrs",
    }
)
_NIX_TARGET_ENVIRONMENT = re.compile(
    r"^NIX_(?:BINTOOLS|CC|PKG_CONFIG)_WRAPPER_TARGET_(?:BUILD|HOST)_"
    r"[A-Za-z0-9_]+$"
)


def _uncredentialed_environment(
    source: Mapping[str, str], *, sandbox_root: Path
) -> dict[str, str]:
    """Construct a capability allowlist and isolated user state for candidate code."""

    selected = {
        name: value
        for name, value in source.items()
        if (
            name in _DECLARED_TOOL_ENVIRONMENT
            or _NIX_TARGET_ENVIRONMENT.fullmatch(name) is not None
        )
        and isinstance(value, str)
    }
    if not selected.get("PATH"):
        raise ExecutionError("candidate environment lacks the declared tool PATH")
    if selected.get("GITHUB_ACTIONS") not in {None, "true"}:
        raise ExecutionError("candidate GitHub Actions marker is invalid")
    home = sandbox_root / "home"
    temporary = sandbox_root / "tmp"
    for path in (home, temporary, home / ".config", home / ".cache", home / ".local/share"):
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        except OSError as error:
            raise ExecutionError(
                f"cannot isolate candidate environment state: {error}"
            ) from error
    selected.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(home),
            "NPM_CONFIG_USERCONFIG": "/dev/null",
            "TEMP": str(temporary),
            "TEMPDIR": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
        }
    )
    return selected


def execute_build_work(
    *,
    coordination_path: Path,
    work_id: str,
    kandelo_root: Path,
    tap_root: Path,
    run: Mapping[str, Any],
    handoff: Path,
    snapshot_source: Callable[[Path, str], Mapping[str, Any]] = snapshot_tap_source,
    run_process: Callable[..., Any] = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Execute one exact build through the Kandelo adapter without credentials."""

    try:
        tap = tap_root.resolve(strict=True)
        kandelo = kandelo_root.resolve(strict=True)
    except OSError as error:
        raise ExecutionError(f"candidate source checkout is unavailable: {error}") from error
    policy_path = tap / "Kandelo/staging/tap-policy.toml"
    from .policy import load_tap_staging_policy

    policy = load_tap_staging_policy(policy_path)
    source_path = coordination_path
    if source_path.is_dir():
        source_path = source_path / "coordination.json"
    bundle = load_coordination_bundle(source_path, policy=policy)
    work = select_build_work(bundle, work_id)
    expected_tap = bundle["tap_plan"]["tap_source"]
    expected_kandelo = bundle["request"]["build_source"]
    if dict(snapshot_source(tap, policy.tap_repository)) != expected_tap:
        raise ExecutionError("tap checkout differs from protected coordinated source")
    if dict(snapshot_source(kandelo, policy.kandelo_repository)) != expected_kandelo:
        raise ExecutionError("Kandelo checkout differs from exact requested source")
    adapter = kandelo / "scripts/abi-staging-build-bottle.sh"
    if adapter.is_symlink() or not adapter.is_file():
        raise ExecutionError("exact-head Kandelo build adapter is unavailable")
    transport = UrllibOciTransportV1(username="", token="")

    def fetch_candidate(locator: Mapping[str, Any]) -> Any:
        return fetch_public_record(
            locator,
            transport=transport,
            expected_artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
            required_layer_roles=("bottle-layer",),
        )

    with tempfile.TemporaryDirectory(prefix="kandelo-abi-staging-inputs-") as temporary:
        prepared = prepare_build_inputs(
            bundle,
            work,
            destination=Path(temporary) / "inputs",
            run=run,
            fetch_candidate=fetch_candidate,
        )
        command = [
            str(adapter),
            "--request",
            str(prepared["request"]),
            "--tap-plan",
            str(prepared["tap_plan"]),
            "--formula-plan",
            str(prepared["formula_plan_path"]),
            "--dependency-root",
            str(prepared["dependency_root"]),
            "--run",
            str(prepared["run"]),
            "--retry-ordinal",
            str(work["attempt_ordinal"]),
            "--handoff",
            str(handoff),
        ]
        child_environment = _uncredentialed_environment(
            os.environ if environment is None else environment,
            sandbox_root=Path(temporary) / "environment",
        )
        result = run_process(command, cwd=tap, env=child_environment, check=False)
    returncode = getattr(result, "returncode", None)
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise ExecutionError("candidate build process returned no exact status")
    return returncode if 0 <= returncode <= 255 else 1


def _expected_verification_work_id(
    bundle: Mapping[str, Any], work: Mapping[str, Any]
) -> str:
    return canonical_sha256(
        {
            "action": "verify-candidate",
            "attempt_ordinal": work["attempt_ordinal"],
            "candidate_record_sha256": work["candidate_record_sha256"],
            "contract_sha256": work["contract_sha256"],
            "host": work["host"],
            "request_sha256": bundle["request_sha256"],
            "subject": json.loads(work["subject"]),
            "test_definition_sha256": work["test_definition_sha256"],
        }
    )


def _candidate_repository_name(
    tap_repository: str, target_abi: int, formula: str
) -> str:
    if (
        not isinstance(tap_repository, str)
        or tap_repository != tap_repository.lower()
        or not re.fullmatch(r"[a-z0-9._-]+/[a-z0-9._-]+", tap_repository)
        or isinstance(target_abi, bool)
        or not isinstance(target_abi, int)
        or not 1 <= target_abi <= 2**32 - 1
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", formula)
    ):
        raise ExecutionError("candidate namespace inputs are invalid")
    return f"ghcr.io/{tap_repository}-abi-{target_abi}-candidates/{formula}"


def _bottle_metadata_formula_key(tap_repository: Any, formula: Any) -> str:
    if (
        not isinstance(tap_repository, str)
        or tap_repository != tap_repository.lower()
        or re.fullmatch(r"[a-z0-9._-]+/homebrew-[a-z0-9._-]+", tap_repository)
        is None
        or not isinstance(formula, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9._-]*", formula) is None
    ):
        raise ExecutionError("candidate bottle metadata identity is invalid")
    owner, repository = tap_repository.split("/", 1)
    return f"{owner}/{repository.removeprefix('homebrew-')}/{formula}"


def _normalized_candidate_bottle_metadata(
    candidate: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[dict[str, Any], str, str]:
    """Reduce authenticated Homebrew output to the strict Formula composer input."""

    formula = candidate.get("formula")
    tap_repository = candidate.get("tap_repository")
    target_abi = candidate.get("target_abi")
    architecture = candidate.get("architecture")
    formula_key = _bottle_metadata_formula_key(tap_repository, formula)
    candidate_repository = _candidate_repository_name(
        tap_repository, target_abi, formula
    )
    expected_root_url = (
        "https://ghcr.io/v2/" + candidate_repository.removeprefix("ghcr.io/")
    )
    metadata_root_url = expected_root_url.rsplit("/", 1)[0]
    bottle_layer = _mapping(candidate.get("bottle_layer"), "candidate bottle layer")
    bottle_sha256 = _digest(
        bottle_layer.get("sha256"), "candidate bottle layer digest"
    )
    if bottle_layer.get("immutable_reference") != (
        candidate_repository + "@sha256:" + bottle_sha256
    ):
        raise ExecutionError(
            "candidate bottle layer differs from its exact publication namespace"
        )
    if architecture not in {"wasm32", "wasm64"}:
        raise ExecutionError("candidate bottle architecture is invalid")
    if list(metadata) != [formula_key]:
        raise ExecutionError(
            "candidate bottle metadata must contain exactly one fully qualified Formula"
        )
    entry = _mapping(metadata.get(formula_key), "candidate bottle metadata entry")
    formula_metadata = _mapping(
        entry.get("formula"), "candidate bottle Formula metadata"
    )
    bottle = _mapping(entry.get("bottle"), "candidate bottle metadata bottle")
    tags = _mapping(bottle.get("tags"), "candidate bottle metadata tags")
    tag_name = f"{architecture}_kandelo"
    tag = _mapping(tags.get(tag_name), "candidate bottle metadata tag")
    owner, repository = tap_repository.split("/", 1)
    formula_path = (
        f"Library/Taps/{owner}/homebrew-{repository.removeprefix('homebrew-')}/"
        f"Formula/{formula}.rb"
    )
    pkg_version = formula_metadata.get("pkg_version")
    cellar = bottle.get("cellar")
    rebuild = bottle.get("rebuild")
    if (
        formula_metadata.get("name") != formula
        or formula_metadata.get("path") != formula_path
        or not isinstance(pkg_version, str)
        or not pkg_version
        or len(pkg_version.encode("utf-8")) > 255
        or bottle.get("root_url") != metadata_root_url
        or cellar
        not in {"any", "any_skip_relocation", "/opt/kandelo/homebrew/Cellar"}
        or isinstance(rebuild, bool)
        or not isinstance(rebuild, int)
        or rebuild < 0
        or list(tags) != [tag_name]
        or tag.get("sha256") != bottle_sha256
    ):
        raise ExecutionError(
            "candidate bottle metadata differs from its exact publication namespace"
        )
    normalized = {
        formula_key: {
            "bottle": {
                "cellar": cellar,
                "rebuild": rebuild,
                "root_url": expected_root_url,
                "tags": {tag_name: {"sha256": bottle_sha256}},
            },
            "formula": {
                "name": formula,
                "path": formula_path,
                "pkg_version": pkg_version,
            },
        }
    }
    return normalized, expected_root_url, cellar


def _candidate_entry(
    bundle: Mapping[str, Any], record_sha256: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    digest = _digest(record_sha256, "candidate record")
    candidates = _mapping(bundle.get("candidates"), "coordination candidates")
    records = _mapping(candidates.get("records"), "coordination candidate records")
    locators = _mapping(candidates.get("locators"), "coordination candidate locators")
    record = dict(_mapping(records.get(digest), "candidate record"))
    locator = dict(_mapping(locators.get(digest), "candidate locator"))
    try:
        validate_candidate_record(record)
    except ValueError as error:
        raise ExecutionError(f"candidate record is invalid: {error}") from error
    expected_repository = _candidate_repository_name(
        str(bundle["tap_plan"]["tap_source"]["repository"]),
        int(record["candidate"]["formula"]["target_abi"]),
        str(record["candidate"]["formula"]["formula"]),
    )
    if (
        locator.get("repository") != expected_repository
        or locator.get("digest") != "sha256:" + digest
        or locator.get("immutable_reference")
        != f"{expected_repository}@sha256:{digest}"
    ):
        raise ExecutionError("candidate locator differs from protected namespace identity")
    formula = _mapping(record["candidate"].get("formula"), "candidate Formula")
    if (
        formula.get("tap") != bundle["tap_plan"]["tap_source"]["repository"]
        or formula.get("target_abi") != bundle["tap_plan"]["target_abi"]["version"]
    ):
        raise ExecutionError("candidate differs from exact coordinated authority")
    return record, locator


def _expected_reuse_work_id(
    bundle: Mapping[str, Any], work: Mapping[str, Any]
) -> str:
    return canonical_sha256(
        {
            "action": "reuse-candidate",
            "attempt_ordinal": work["attempt_ordinal"],
            "candidate_record_sha256": work["candidate_record_sha256"],
            "contract_sha256": work["contract_sha256"],
            "host": None,
            "request_sha256": bundle["request_sha256"],
            "subject": json.loads(work["subject"]),
            "test_definition_sha256": None,
        }
    )


def select_reuse_work(
    bundle: Mapping[str, Any], work_id: str
) -> dict[str, Any]:
    """Bind an explicit new-request record to one unchanged prior candidate."""

    _digest(work_id, "reuse work ID")
    workflow = _mapping(bundle.get("workflow"), "coordination workflow")
    matches = [
        item
        for item in workflow.get("reuse_work", ())
        if item.get("work_id") == work_id
    ]
    if len(matches) != 1:
        raise ExecutionError(
            "coordination bundle does not contain the exact reuse work ID"
        )
    work = dict(_mapping(matches[0], "reuse work"))
    if (
        work.get("action") != "reuse-candidate"
        or work.get("attempt_ordinal") != 0
        or _expected_reuse_work_id(bundle, work) != work_id
    ):
        raise ExecutionError("reuse work identity differs from coordinated inputs")
    formula_plan = _formula_for_subject(bundle, work["subject"])
    contract = _mapping(
        _mapping(bundle.get("contracts"), "coordination contracts").get(
            work["subject"]
        ),
        "reuse bottle contract",
    )
    if (
        canonical_sha256(formula_plan) != work["formula_plan_sha256"]
        or formula_plan.get("contract_sha256") != work["contract_sha256"]
        or canonical_sha256(contract) != work["contract_sha256"]
    ):
        raise ExecutionError("reuse work differs from its Formula contract")
    candidate, locator = _candidate_entry(
        bundle, work["candidate_record_sha256"]
    )
    formula = candidate["candidate"]["formula"]
    if (
        locator != work["candidate_locator"]
        or exact_formula_subject(formula["formula"], formula["architecture"])
        != work["subject"]
        or formula["bottle_contract_sha256"] != work["contract_sha256"]
    ):
        raise ExecutionError("reuse work differs from its exact candidate")
    return work


def _verification_definition(
    bundle: Mapping[str, Any], work: Mapping[str, Any]
) -> dict[str, Any]:
    matches = []
    for candidate in bundle.get("verification_tests", ()):
        definition = _mapping(candidate, "verification test definition")
        if (
            definition.get("sha256") == work["test_definition_sha256"]
            and work["host"] in definition.get("hosts", ())
        ):
            matches.append(dict(definition))
    if len(matches) != 1:
        raise ExecutionError("verification work lacks one exact protected test definition")
    definition = matches[0]
    identity = {key: definition[key] for key in ("hosts", "id", "kandelo_paths", "policy")}
    if canonical_sha256(identity) != definition["sha256"]:
        raise ExecutionError("verification test definition digest drifted")
    return identity


def select_verification_work(
    bundle: Mapping[str, Any], work_id: str
) -> dict[str, Any]:
    """Select one exact verification item and bind candidate, contract, and test."""

    _digest(work_id, "verification work ID")
    workflow = _mapping(bundle.get("workflow"), "coordination workflow")
    matches = [
        item
        for item in workflow.get("verify_work", ())
        if item.get("work_id") == work_id
    ]
    if len(matches) != 1:
        raise ExecutionError(
            "coordination bundle does not contain the exact verification work ID"
        )
    work = dict(_mapping(matches[0], "verification work"))
    if (
        work.get("action") != "verify-candidate"
        or _expected_verification_work_id(bundle, work) != work_id
        or work.get("host") not in {"build", "node", "browser"}
    ):
        raise ExecutionError("verification work identity differs from coordinated inputs")
    formula_plan = _formula_for_subject(bundle, work["subject"])
    contract = _mapping(
        _mapping(bundle.get("contracts"), "coordination contracts").get(work["subject"]),
        "verification bottle contract",
    )
    if (
        canonical_sha256(formula_plan) != work["formula_plan_sha256"]
        or formula_plan.get("contract_sha256") != work["contract_sha256"]
        or canonical_sha256(contract) != work["contract_sha256"]
    ):
        raise ExecutionError("verification work differs from its Formula contract")
    candidate, locator = _candidate_entry(bundle, work["candidate_record_sha256"])
    formula = candidate["candidate"]["formula"]
    expected_dependencies = []
    for dependency in contract.get("direct_dependencies", ()):
        dependency_formula = dependency["formula"]
        dependency_digest = dependency["bottle_layer_sha256"]
        dependency_repository = _candidate_repository_name(
            str(formula["tap"]), int(formula["target_abi"]), dependency_formula
        )
        expected_dependencies.append(
            {
                "artifact": {
                    "bytes": dependency["bottle_layer_bytes"],
                    "immutable_reference": (
                        f"{dependency_repository}@sha256:{dependency_digest}"
                    ),
                    "sha256": dependency_digest,
                },
                "id": f"{dependency_formula}-{dependency['architecture']}",
            }
        )
    expected_dependencies.sort(key=lambda item: item["id"])
    if (
        locator != work["candidate_locator"]
        or exact_formula_subject(formula["formula"], formula["architecture"])
        != work["subject"]
        or formula["bottle_contract_sha256"] != work["contract_sha256"]
        or candidate["candidate"]["direct_dependency_layers"]
        != expected_dependencies
    ):
        raise ExecutionError("verification work differs from its exact candidate")
    _verification_definition(bundle, work)
    return work


def _dependency_candidate(
    bundle: Mapping[str, Any], direct: Mapping[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    artifact = _mapping(direct.get("artifact"), "candidate dependency artifact")
    direct_id = direct.get("id")
    matches: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    records = _mapping(bundle["candidates"]["records"], "candidate records")
    for record_sha256 in sorted(records):
        record, locator = _candidate_entry(bundle, record_sha256)
        payload = record["candidate"]
        formula = payload["formula"]
        if (
            direct_id == f"{formula['formula']}-{formula['architecture']}"
            and payload["bottle_layer"] == artifact
        ):
            matches.append((record_sha256, record, locator))
    if not matches:
        raise ExecutionError("candidate dependency lacks its exact public record")
    return matches[0]


def _candidate_closure(
    bundle: Mapping[str, Any], work: Mapping[str, Any]
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    root_digest = work["candidate_record_sha256"]
    root_record, root_locator = _candidate_entry(bundle, root_digest)
    by_formula: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    visiting: set[str] = set()

    def visit(
        record_sha256: str,
        record: dict[str, Any],
        locator: dict[str, Any],
    ) -> None:
        if record_sha256 in visiting:
            raise ExecutionError("candidate dependency closure contains a cycle")
        formula = record["candidate"]["formula"]["formula"]
        prior = by_formula.get(formula)
        if prior is not None:
            if prior[1]["candidate"]["bottle_layer"] != record["candidate"]["bottle_layer"]:
                raise ExecutionError("candidate dependency closure conflicts by Formula")
            return
        visiting.add(record_sha256)
        for direct in record["candidate"]["direct_dependency_layers"]:
            visit(*_dependency_candidate(bundle, direct))
        visiting.remove(record_sha256)
        by_formula[formula] = (record_sha256, record, locator)

    visit(root_digest, root_record, root_locator)
    if len(by_formula) > 256:
        raise ExecutionError("candidate dependency closure exceeds its bound")
    return [by_formula[name] for name in sorted(by_formula)]


def _verification_run(value: Mapping[str, Any], repository: str) -> dict[str, Any]:
    run = dict(_mapping(value, "verification run"))
    if frozenset(run) != frozenset(
        {"repository", "workflow_ref", "run_id", "run_attempt", "job"}
    ):
        raise ExecutionError("verification run fields changed")
    if (
        run.get("repository") != repository
        or run.get("job") != "verify-candidate"
        or not isinstance(run.get("workflow_ref"), str)
        or not run["workflow_ref"]
        or len(run["workflow_ref"].encode()) > 2048
        or isinstance(run.get("run_id"), bool)
        or not isinstance(run.get("run_id"), int)
        or run["run_id"] <= 0
        or isinstance(run.get("run_attempt"), bool)
        or not isinstance(run.get("run_attempt"), int)
        or run["run_attempt"] <= 0
    ):
        raise ExecutionError("verification run identity is invalid")
    return run


def _fetched_candidate_material(
    fetched: Any,
    *,
    record_sha256: str,
    record: Mapping[str, Any],
    locator: Mapping[str, Any],
) -> tuple[bytes, bytes, bytes]:
    if (
        getattr(fetched, "artifact_type", None) != CANDIDATE_RECORD_MEDIA_TYPE
        or getattr(fetched, "digest", None) != "sha256:" + record_sha256
        or getattr(fetched, "immutable_reference", None)
        != locator["immutable_reference"]
        or getattr(getattr(fetched, "config", None), "body", None)
        != canonical_bytes(record)
    ):
        raise ExecutionError("fetched candidate differs from its exact public record")
    layers = tuple(getattr(fetched, "layers", ()))
    if [getattr(layer, "role", None) for layer in layers] != [
        "bottle-layer",
        "bottle-metadata",
        "vfs-composition-descriptor",
    ]:
        raise ExecutionError(
            "fetched candidate lacks exact bottle, metadata, and VFS composition layers"
        )
    bottle = record["candidate"]["bottle_layer"]
    metadata_matches = [
        item["artifact"]
        for item in record["candidate"]["normalized_components"]
        if item["id"] == "bottle-metadata"
    ]
    if len(metadata_matches) != 1:
        raise ExecutionError("candidate record lacks one bottle metadata identity")
    metadata = metadata_matches[0]
    composition_matches = [
        item["artifact"]
        for item in record["candidate"]["normalized_components"]
        if item["id"] == "vfs-composition-descriptor"
    ]
    if len(composition_matches) != 1:
        raise ExecutionError(
            "candidate record lacks one VFS composition descriptor identity"
        )
    composition = composition_matches[0]
    identities = (
        (layers[0], bottle),
        (layers[1], metadata),
        (layers[2], composition),
    )
    for layer, identity in identities:
        body = getattr(layer, "body", None)
        if (
            not isinstance(body, bytes)
            or len(body) != identity["bytes"]
            or hashlib.sha256(body).hexdigest() != identity["sha256"]
            or getattr(layer, "digest", None) != "sha256:" + identity["sha256"]
            or getattr(layer, "size", None) != identity["bytes"]
        ):
            raise ExecutionError("fetched candidate layer differs from its record")
    try:
        parse_canonical_bytes(layers[1].body, maximum_bytes=4 * 1024 * 1024)
        parse_canonical_bytes(
            layers[2].body,
            maximum_bytes=16 * 1024 * 1024,
            maximum_items=MAX_VFS_COMPOSITION_JSON_ITEMS,
        )
    except CanonicalJsonError as error:
        raise ExecutionError(
            f"candidate metadata or VFS composition descriptor is not canonical: {error}"
        ) from error
    return layers[0].body, layers[1].body, layers[2].body


def prepare_verification_inputs(
    bundle: Mapping[str, Any],
    work: Mapping[str, Any],
    *,
    destination: Path,
    run: Mapping[str, Any],
    fetch_candidate: Callable[[Mapping[str, Any]], Any],
) -> dict[str, Any]:
    """Materialize an exact target plus its full public dependency closure."""

    selected = select_verification_work(bundle, str(work.get("work_id", "")))
    if canonical_bytes(selected) != canonical_bytes(work):
        raise ExecutionError("caller-supplied verification work differs from coordination")
    checked_run = _verification_run(
        run, str(bundle["tap_plan"]["tap_source"]["repository"])
    )
    root = _create_output_directory(destination)
    candidate_root = root / "candidates"
    candidate_root.mkdir()
    sysroot = root / "sysroot"
    sysroot.mkdir()
    closure = _candidate_closure(bundle, selected)
    prepared_candidates: list[dict[str, Any]] = []
    for record_sha256, record, locator in closure:
        bottle, metadata, composition = _fetched_candidate_material(
            fetch_candidate(locator),
            record_sha256=record_sha256,
            record=record,
            locator=locator,
        )
        formula = record["candidate"]["formula"]
        formula_root = candidate_root / formula["formula"]
        formula_root.mkdir()
        bottle_path = formula_root / "bottle.tar.gz"
        metadata_path = formula_root / "bottle-metadata.json"
        composition_path = formula_root / "vfs-composition-descriptor.json"
        bottle_path.write_bytes(bottle)
        metadata_path.write_bytes(metadata)
        composition_path.write_bytes(composition)
        prepared_candidates.append(
            {
                "formula": formula["formula"],
                "architecture": formula["architecture"],
                "target_abi": formula["target_abi"],
                "tap_repository": formula["tap"],
                "record_sha256": record_sha256,
                "locator": locator,
                "bottle_layer": record["candidate"]["bottle_layer"],
                "bottle": bottle_path,
                "metadata": metadata_path,
                "vfs_composition_descriptor": composition_path,
            }
        )
    target = next(
        (
            candidate
            for candidate in prepared_candidates
            if candidate["record_sha256"] == selected["candidate_record_sha256"]
        ),
        None,
    )
    if target is None:
        raise ExecutionError("verification closure omitted its target candidate")
    dependencies = [
        {
            "artifact": candidate["bottle_layer"],
            "formula": candidate["formula"],
        }
        for candidate in prepared_candidates
        if candidate is not target
    ]
    dependency_contract = {
        "architecture": target["architecture"],
        "dependency_layers": dependencies,
        "kind": "kandelo-abi-staging-dependency-layers",
        "schema": 1,
        "tap_repository": bundle["tap_plan"]["tap_source"]["repository"],
        "target_abi": target["target_abi"],
    }
    definition = _verification_definition(bundle, selected)
    documents = {
        root / "candidate-locator.json": target["locator"],
        root / "request-binding.json": {
            "request_sha256": bundle["request_sha256"],
            "source": bundle["request"]["build_source"],
        },
        root / "test-definition.json": definition,
        root / "run.json": checked_run,
        root / "dependency-provenance.json": dependency_contract,
    }
    try:
        for path, value in documents.items():
            path.write_bytes(canonical_bytes(value))
    except OSError as error:
        raise ExecutionError(f"cannot write verification inputs: {error}") from error
    return {
        "root": root,
        "candidates": prepared_candidates,
        "target": target,
        "candidate_locator": root / "candidate-locator.json",
        "request_binding": root / "request-binding.json",
        "test_definition": root / "test-definition.json",
        "test_definition_sha256": selected["test_definition_sha256"],
        "run": root / "run.json",
        "dependency_provenance": root / "dependency-provenance.json",
        "sysroot": sysroot,
    }


def compose_candidate_tap(
    *,
    tap_root: Path,
    kandelo_root: Path,
    destination: Path,
    candidates: Sequence[Mapping[str, Any]],
) -> Path:
    """Clone the exact tap commit and compose only exact candidate bottle blocks."""

    if destination.exists() or destination.is_symlink():
        raise ExecutionError("candidate tap destination must not already exist")
    try:
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(tap_root), str(destination)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ExecutionError(f"cannot clone exact candidate tap: {error}") from error
    if clone.returncode != 0:
        detail = clone.stderr.decode("utf-8", errors="replace")[:4096]
        raise ExecutionError(f"cannot clone exact candidate tap: {detail}")
    expected_head = subprocess.run(
        ["git", "-C", str(tap_root), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.decode("ascii").strip()
    actual_head = subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.decode("ascii").strip()
    if actual_head != expected_head:
        raise ExecutionError("candidate tap clone differs from exact protected commit")
    merge = kandelo_root / "scripts/homebrew-merge-bottle-json.sh"
    if merge.is_symlink() or not merge.is_file():
        raise ExecutionError("exact-head candidate Formula composer is unavailable")
    for candidate in candidates:
        try:
            metadata = parse_canonical_bytes(
                Path(candidate["metadata"]).read_bytes(), maximum_bytes=4 * 1024 * 1024
            )
            metadata_entries = _mapping(metadata, "candidate bottle metadata")
        except (OSError, CanonicalJsonError) as error:
            raise ExecutionError(f"cannot read candidate bottle metadata: {error}") from error
        normalized, expected_root_url, expected_cellar = (
            _normalized_candidate_bottle_metadata(candidate, metadata_entries)
        )
        try:
            with tempfile.NamedTemporaryFile(
                prefix="kandelo-candidate-bottle-", suffix=".json"
            ) as normalized_file:
                normalized_file.write(canonical_bytes(normalized))
                normalized_file.flush()
                command = [
                    str(merge),
                    "--tap-root",
                    str(destination),
                    "--tap-repository",
                    str(candidate["tap_repository"]),
                    "--formula",
                    str(candidate["formula"]),
                    "--arch",
                    str(candidate["architecture"]),
                    "--release-tag",
                    f"bottles-abi-v{candidate['target_abi']}",
                    "--bottle-json",
                    normalized_file.name,
                    "--expected-sha256",
                    str(candidate["bottle_layer"]["sha256"]),
                    "--expected-root-url",
                    expected_root_url,
                    "--expected-cellar",
                    expected_cellar,
                    "--staging-candidate-abi",
                    str(candidate["target_abi"]),
                ]
                result = subprocess.run(
                    command,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
        except OSError as error:
            raise ExecutionError(
                f"cannot write normalized candidate bottle metadata: {error}"
            ) from error
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[:4096]
            raise ExecutionError(
                f"cannot compose exact candidate Formula {candidate['formula']}: {detail}"
            )
        # Homebrew appends the Formula name to a bottle root when it derives
        # the blob URL. Candidate OCI packages already use one repository per
        # Formula, so the composed dependency Formula must retain the common
        # ABI candidate parent here; otherwise Homebrew resolves
        # `.../<formula>/<formula>/blobs/...`. The exact-head composer still
        # validates the per-Formula publication namespace before this
        # protected, deterministic runtime normalization.
        formula_path = destination / "Formula" / f"{candidate['formula']}.rb"
        candidate_root_line = f'root_url "{expected_root_url}"'
        bottle_root_url = expected_root_url.rsplit("/", 1)[0]
        bottle_root_line = f'root_url "{bottle_root_url}"'
        try:
            formula_source = formula_path.read_text(
                encoding="utf-8", errors="strict"
            )
            if formula_source.count(candidate_root_line) != 1:
                raise ExecutionError(
                    "composed candidate Formula lacks one exact package root"
                )
            formula_path.write_text(
                formula_source.replace(candidate_root_line, bottle_root_line),
                encoding="utf-8",
            )
        except OSError as error:
            raise ExecutionError(
                f"cannot normalize candidate Formula bottle root: {error}"
            ) from error
    allowed = {f"Formula/{candidate['formula']}.rb" for candidate in candidates}
    status = subprocess.run(
        ["git", "-C", str(destination), "status", "--short", "--untracked-files=all"],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.decode("utf-8", errors="strict").splitlines()
    for line in status:
        if not line.startswith(" M ") or line[3:] not in allowed:
            raise ExecutionError("candidate composition changed an undeclared tap path")
    return destination.resolve(strict=True)


def execute_verification_work(
    *,
    coordination_path: Path,
    work_id: str,
    kandelo_root: Path,
    tap_root: Path,
    run: Mapping[str, Any],
    output: Path,
    snapshot_source: Callable[[Path, str], Mapping[str, Any]] = snapshot_tap_source,
    fetch_candidate: Callable[[Mapping[str, Any]], Any] | None = None,
    compose_tap: Callable[..., Path] = compose_candidate_tap,
    run_process: Callable[..., Any] = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Verify one exact public candidate without credentials or rebuilding."""

    try:
        tap = tap_root.resolve(strict=True)
        kandelo = kandelo_root.resolve(strict=True)
    except OSError as error:
        raise ExecutionError(f"verification source checkout is unavailable: {error}") from error
    from .policy import load_tap_staging_policy

    policy = load_tap_staging_policy(tap / "Kandelo/staging/tap-policy.toml")
    source_path = coordination_path
    if source_path.is_dir():
        source_path = source_path / "coordination.json"
    bundle = load_coordination_bundle(source_path, policy=policy)
    work = select_verification_work(bundle, work_id)
    if dict(snapshot_source(tap, policy.tap_repository)) != bundle["tap_plan"]["tap_source"]:
        raise ExecutionError("tap checkout differs from protected coordinated source")
    if dict(snapshot_source(kandelo, policy.kandelo_repository)) != bundle["request"]["build_source"]:
        raise ExecutionError("Kandelo checkout differs from exact requested source")
    adapter = kandelo / "scripts/abi-staging-verify-bottle.sh"
    if adapter.is_symlink() or not adapter.is_file():
        raise ExecutionError("exact-head Kandelo verification adapter is unavailable")
    if fetch_candidate is None:
        transport = UrllibOciTransportV1(username="", token="")

        def fetch_candidate(locator: Mapping[str, Any]) -> Any:
            return fetch_public_record(
                locator,
                transport=transport,
                expected_artifact_type=CANDIDATE_RECORD_MEDIA_TYPE,
                required_layer_roles=(
                    "bottle-layer",
                    "bottle-metadata",
                    "vfs-composition-descriptor",
                ),
            )

    with tempfile.TemporaryDirectory(prefix="kandelo-abi-staging-verification-") as temporary:
        temporary_root = Path(temporary)
        prepared = prepare_verification_inputs(
            bundle,
            work,
            destination=temporary_root / "inputs",
            run=run,
            fetch_candidate=fetch_candidate,
        )
        composed_tap = compose_tap(
            tap_root=tap,
            kandelo_root=kandelo,
            destination=temporary_root / "tap",
            candidates=prepared["candidates"],
        )
        command = [
            str(adapter),
            "--candidate-locator",
            str(prepared["candidate_locator"]),
            "--test-definition",
            str(prepared["test_definition"]),
            "--test-definition-sha256",
            str(prepared["test_definition_sha256"]),
            "--host",
            str(work["host"]),
            "--attempt-ordinal",
            str(work["attempt_ordinal"]),
            "--run",
            str(prepared["run"]),
            "--request-binding",
            str(prepared["request_binding"]),
            "--tap-root",
            str(composed_tap),
            "--tap-commit",
            str(bundle["tap_plan"]["tap_source"]["commit"]),
            "--tap-checkout-commit",
            str(bundle["tap_plan"]["tap_source"]["commit"]),
            "--dependency-provenance",
            str(prepared["dependency_provenance"]),
            "--sysroot-build-root",
            str(kandelo),
        ]
        for forbidden in sorted({str(kandelo), str(tap), str(prepared["root"])}):
            command.extend(["--forbidden-root", forbidden])
        command.extend(["--out", str(output)])
        child_environment = _uncredentialed_environment(
            os.environ if environment is None else environment,
            sandbox_root=temporary_root / "environment",
        )
        # Candidate verification is a credential-free consumer, not the
        # production Homebrew publisher.  Keep the real isolated native-tool
        # installs, but do not require the publisher-only signed API preflight
        # that is intentionally absent from this reusable verification job.
        child_environment.pop("GITHUB_ACTIONS", None)
        # Verification is already a credential-free consumer job. Reusing the
        # publisher's reserved build identities here makes the exact adapter's
        # private TemporaryDirectory inaccessible to the child Homebrew
        # process. Candidate construction and publication retain the isolated
        # identities; read-only verification runs as this uncredentialed job.
        child_environment.pop("KANDELO_HOMEBREW_BUILD_USER", None)
        child_environment.pop("KANDELO_HOMEBREW_RECIPE_USER", None)
        child_environment.pop("KANDELO_HOMEBREW_SHARED_TEMP", None)
        playwright_browsers = child_environment.pop(
            "PLAYWRIGHT_BROWSERS_PATH", None
        )
        if not isinstance(playwright_browsers, str) or not playwright_browsers:
            raise ExecutionError("prepared Playwright browser root is unavailable")
        browser_root = Path(playwright_browsers)
        try:
            resolved_browser_root = browser_root.resolve(strict=True)
        except OSError as error:
            raise ExecutionError(
                f"prepared Playwright browser root is unavailable: {error}"
            ) from error
        if (
            not browser_root.is_absolute()
            or browser_root.is_symlink()
            or not resolved_browser_root.is_dir()
        ):
            raise ExecutionError("prepared Playwright browser root is unavailable")
        command.extend(
            ["--playwright-browsers-path", str(resolved_browser_root)]
        )
        result = run_process(command, cwd=kandelo, env=child_environment, check=False)
    returncode = getattr(result, "returncode", None)
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise ExecutionError("candidate verification process returned no exact status")
    return returncode if 0 <= returncode <= 255 else 1
