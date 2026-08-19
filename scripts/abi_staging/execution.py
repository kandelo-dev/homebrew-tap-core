"""Fail-closed preparation for uncredentialed ABI-staging work."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
from typing import Any

from .canonical import (
    MAX_VFS_COMPOSITION_JSON_ITEMS,
    CanonicalJsonError,
    canonical_bytes,
    canonical_sha256,
    parse_canonical_bytes,
)
from .coordination import (
    MAX_COORDINATION_BYTES,
    MAX_COORDINATION_JSON_ITEMS,
    CoordinationError,
    validate_coordination_bundle,
)
from .handoff import HandoffError, load_build_run
from .oci import UrllibOciTransportV1, fetch_public_record
from .plan import PlanError, bottle_metadata_formula_key, exact_formula_subject
from .plan import snapshot_tap_source
from .policy import TapStagingPolicyV1
from .records import CANDIDATE_RECORD_MEDIA_TYPE, validate_candidate_record


SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
TAP_NAME = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")
TAP_REPOSITORY = re.compile(r"^[a-z0-9_.-]+/homebrew-[a-z0-9_.-]+$")

_CANDIDATE_ROOT_ASSIGNMENT = 'KANDELO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"'
_CANDIDATE_VERIFIER_ROOT_ASSIGNMENT = (
    'KANDELO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"'
)
_VERIFICATION_ADAPTER_ROOT_ASSIGNMENT = (
    'REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"'
)
_VERIFICATION_ADAPTER_NORMAL_ASSIGNMENT = (
    'NORMAL_VERIFIER="$REPO_ROOT/scripts/homebrew-verify-poured-bottle.sh"'
)
_VERIFIER_FORMULA_INFO_CAPTURE = (
    '"$BREW_BIN" info --json=v2 "$FORMULA_REF" >"$FORMULA_INFO"'
)
_STAGING_VERIFIER_FORMULA_INFO_CAPTURE = r'''staging_formula_info="$FORMULA_INFO.staging"
staging_formula_info_raw="$FORMULA_INFO.raw"
"$BREW_BIN" info --json=v2 "$FORMULA_REF" >"$staging_formula_info_raw"
staging_rebuild="$(jq -er '
  to_entries |
  if length == 1 and (.[0].value.bottle.rebuild | type) == "number" then
    .[0].value.bottle.rebuild
  else
    error("candidate bottle metadata lacks one rebuild")
  end
' "$BOTTLE_JSON")"
jq -cer \
  --arg tag "$BOTTLE_TAG" \
  --arg sha256 "$BOTTLE_SHA256" \
  --arg url "$BOTTLE_URL" \
  --argjson rebuild "$staging_rebuild" '
  if (.formulae | type) == "array" and (.formulae | length) == 1 and .casks == [] then
    .formulae[0].bottle = {
      stable: {
        files: {($tag): {sha256: $sha256, url: $url}},
        rebuild: $rebuild
      }
    }
  else
    error("Homebrew Formula info has an unexpected shape")
  end
' "$staging_formula_info_raw" >"$staging_formula_info"
rm -f "$staging_formula_info_raw"
mv "$staging_formula_info" "$FORMULA_INFO"'''
_FORMULA_DEPENDENCY_INSTALL = """  run_brew_logged run_brew_for_kandelo_bottles "$BREW_BIN" install \\
    --force-bottle \\
    --as-dependency \\
    --ignore-dependencies \\
    --formula "$dependency"""
_VERIFIER_FORMULA_DEPENDENCY_INSTALL = """  run_brew_logged run_brew_for_kandelo_bottles "$BREW_BIN" install \\
    --force-bottle --as-dependency --ignore-dependencies --formula "$dependency"""
_LOCAL_ARCHIVE_DEPENDENCY_INSTALL = """  dependency_sha="$(jq -er --arg dependency "$dependency" \\
    '.[$dependency].sha256' "$LOCAL_DEPENDENCIES_JSON")"
  dependency_archive="$(HOMEBREW_KANDELO_BOTTLE_TAG="$BOTTLE_TAG" \\
    KANDELO_HOMEBREW_BOTTLE_TAG="$BOTTLE_TAG" \\
    "$BREW_BIN" --cache --bottle-tag="$BOTTLE_TAG" --formula "$dependency")"
  case "$dependency_archive" in
    *$'\\n'*|*$'\\t'*|"$HOMEBREW_CACHE")
      echo "homebrew-bottle-build.sh: local dependency archive path is invalid" >&2
      exit 1
      ;;
    "$HOMEBREW_CACHE"/*) ;;
    *)
      echo "homebrew-bottle-build.sh: local dependency archive escapes its private cache" >&2
      exit 1
      ;;
  esac
  if [ ! -f "$dependency_archive" ] || [ -L "$dependency_archive" ] || \\
     [ "$(sha256sum "$dependency_archive" | awk '{print $1}')" != "$dependency_sha" ]; then
    echo "homebrew-bottle-build.sh: exact local dependency archive changed before install: $dependency" >&2
    exit 1
  fi
  run_brew_logged run_brew_for_kandelo_bottles "$BREW_BIN" install \\
    --force-bottle \\
    --as-dependency \\
    --ignore-dependencies \\
    "$dependency_archive"""
_CANDIDATE_LAUNCHER_LOAD = '. "$KANDELO_ROOT/scripts/homebrew-patched-launcher.sh"'
_RECIPE_RUNNER_LIMITS = """EXPECTED_LIMITS = {
    "max_bytes": 2_147_483_648,
    "max_entries": 262_144,
    "max_file_bytes": 1_073_741_824,
    "max_path_bytes": 4_096,
}
"""
_STAGING_RECIPE_RUNNER_LIMITS = _RECIPE_RUNNER_LIMITS + """SOURCE_INPUT_LIMITS = {
    **EXPECTED_LIMITS,
    "max_bytes": 4_294_967_296,
    "max_entries": 524_288,
}
"""
_RECIPE_RUNNER_SOURCE_COPY = (
    'copy_input_tree(request["source_root"], source_root, request["limits"])'
)
_STAGING_RECIPE_RUNNER_SOURCE_COPY = (
    'copy_input_tree(request["source_root"], source_root, SOURCE_INPUT_LIMITS)'
)
_RECIPE_RUNNER_PLATFORM_ENVIRONMENT_SIGNATURE = """def add_runner_owned_platform_environment(
    environment: dict[str, str], platform_root: Path
) -> dict[str, str]:"""
_STAGING_RECIPE_RUNNER_PLATFORM_ENVIRONMENT_SIGNATURE = """def add_runner_owned_platform_environment(
    environment: dict[str, str], platform_root: Path, llvm_bin: Path
) -> dict[str, str]:"""
_RECIPE_RUNNER_CONFIG_SITE_ENVIRONMENT = """    child_environment[SDK_CONFIG_SITE_ENV_KEY] = str(
        platform_root / SDK_CONFIG_SITE_RELATIVE
    )"""
_STAGING_RECIPE_RUNNER_CONFIG_SITE_ENVIRONMENT = (
    _RECIPE_RUNNER_CONFIG_SITE_ENVIRONMENT
    + '\n    child_environment["LLVM_PREFIX"] = str(llvm_bin.parent)'
)
_RECIPE_RUNNER_PLATFORM_ENVIRONMENT_CALL = """        child_environment = add_runner_owned_platform_environment(
            request["environment"], request["platform_root"]
        )"""
_STAGING_RECIPE_RUNNER_PLATFORM_ENVIRONMENT_CALL = """        child_environment = add_runner_owned_platform_environment(
            request["environment"], request["platform_root"], config["llvm_bin"]
        )"""
_RECIPE_RUNNER_PROTECTED_ROOT = (
    'PROTECTED_PUBLISHER_ROOT = Path("/run/kandelo-homebrew-publisher")'
)
_RECIPE_RUNNER_PROTECTED_ROOT_CONFIG_CHECK = (
    'if config["protected_root"].parent != '
    'Path("/run/kandelo-homebrew-publisher"):'
)
_RECIPE_RUNNER_SOURCE_ASSIGNMENT = (
    '  local source="$platform_root/scripts/homebrew-tap-recipe-runner.py"'
)
_RECIPE_RUNNER_SOURCE_AUTHORITY_COMMENT = """  # WHY: this Python program runs as root before any tap recipe is admitted.
  # Select it only from the exact root-owned platform projection whose complete
  # manifest was sealed by the launcher; a checkout path supplied separately
  # would reintroduce mutable workflow state as privileged code authority."""
_STAGING_RECIPE_RUNNER_SOURCE_AUTHORITY_COMMENT = """  # WHY: this Python program runs as root before any tap recipe is admitted.
  # Staging replaces it with the tap executor's mode-0500, digest-literal
  # derivative while still revalidating the exact sealed platform projection."""
_RECIPE_RUNNER_SOURCE_STATE = (
    '       2>/dev/null || true)" != "0:0:444:1" ]; then'
)
_RECIPE_RUNNER_SOURCE_DIGEST_CHECK = (
    '  if ! [[ "$source_sha" =~ ^[0-9a-f]{64}$ ]]; then'
)
_TARGET_CELLAR_AUDIT = """  homebrew_patched_launcher_assert_target_cellar_links_safe \\
    "$build_user" "$cellar" || return"""
_STAGING_TARGET_CELLAR_AUDIT = """  # Reused local bottles are poured by the isolated build identity under
  # umask 077. Make only their directories traversable and non-writable before
  # the unchanged link/type audit inspects them; files and links are untouched.
  "$sudo_bin" -n -- /usr/bin/find "$cellar" -xdev -mindepth 1 -type d \\
    -exec /usr/bin/chmod 0555 {} + || return
  homebrew_patched_launcher_assert_target_cellar_links_safe \\
    "$build_user" "$cellar" || return"""


_PROTECTED_ROOT_PARENT_AUDIT = """  [ "$(/usr/bin/stat -c '%u:%g:%a' /run)" = "0:0:755" ] || {
    echo "homebrew-patched-launcher: /run does not provide a protected publisher anchor" >&2
    return 2
  }"""
_STAGING_PROTECTED_ROOT_PARENT_AUDIT = """  [ "$(/usr/bin/stat -c '%u:%g:%a' /run)" = "0:0:755" ] || {
    echo "homebrew-patched-launcher: /run does not provide a protected publisher anchor" >&2
    return 2
  }
  [ "$(/usr/bin/stat -c '%u:%g:%a' /var/lib)" = "0:0:755" ] || {
    echo "homebrew-patched-launcher: /var/lib does not provide a disk-backed protected publisher anchor" >&2
    return 2
  }
  # WHY: Formula contracts and both protected runners share the /run anchor,
  # but multi-gigabyte recipe copies cannot live on the runner's tmpfs. Keep
  # the contract path and bind it to a root-owned disk-backed directory before
  # any Formula identity or recipe process exists.
  local protected_backing="/var/lib/kandelo-abi-staging"
  "$sudo_bin" /usr/bin/install -d -o root -g root -m 0711 \\
    "$protected_backing" "$protected_anchor" || return
  [ "$(/usr/bin/stat -c '%u:%g:%a' "$protected_backing" 2>/dev/null || true)" = \\
    "0:0:711" ] || {
    echo "homebrew-patched-launcher: disk-backed publisher anchor has unsafe access" >&2
    return 2
  }
  if /usr/bin/mountpoint -q "$protected_anchor"; then
    echo "homebrew-patched-launcher: protected publisher anchor is already mounted" >&2
    return 2
  fi
  "$sudo_bin" /usr/bin/mount --bind \\
    "$protected_backing" "$protected_anchor" || return
  if [ "$(/usr/bin/stat -c '%d:%i' "$protected_backing" 2>/dev/null || true)" != \\
       "$(/usr/bin/stat -c '%d:%i' "$protected_anchor" 2>/dev/null || true)" ]; then
    echo "homebrew-patched-launcher: protected publisher anchor is not disk-backed" >&2
    return 2
  fi"""


class ExecutionError(ValueError):
    """Raised when protected work cannot be tied to exact coordinated inputs."""


def _prepare_staging_normal_builder(
    *,
    source: Path,
    destination: Path,
    root_assignment: str = _CANDIDATE_ROOT_ASSIGNMENT,
    dependency_install: str = _FORMULA_DEPENDENCY_INSTALL,
    formula_info_capture: str | None = None,
    protect_launcher: bool = False,
) -> Path:
    """Derive the staging-only builder without changing Formula contracts."""

    try:
        metadata = source.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ExecutionError("candidate normal builder must be a regular file")
        if metadata.st_size < 1 or metadata.st_size > 2 * 1024 * 1024:
            raise ExecutionError("candidate normal builder is outside its byte bound")
        body = source.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as error:
        raise ExecutionError(f"cannot read candidate normal builder: {error}") from error
    if "\r" in body or not body.endswith("\n"):
        raise ExecutionError("candidate normal builder is not canonical LF text")
    if body.count(root_assignment) != 1:
        raise ExecutionError("candidate normal builder root boundary changed")
    if body.count(dependency_install) != 1:
        raise ExecutionError("candidate dependency install boundary changed")
    if formula_info_capture is not None and body.count(formula_info_capture) != 1:
        raise ExecutionError("candidate Formula info capture boundary changed")
    if protect_launcher and body.count(_CANDIDATE_LAUNCHER_LOAD) != 1:
        raise ExecutionError("candidate patched launcher selection boundary changed")
    prepared = body.replace(
        root_assignment,
        'KANDELO_ROOT="${KANDELO_ABI_STAGING_CANDIDATE_ROOT:?}"',
    ).replace(
        dependency_install,
        _LOCAL_ARCHIVE_DEPENDENCY_INSTALL,
    )
    if formula_info_capture is not None:
        prepared = prepared.replace(
            formula_info_capture,
            _STAGING_VERIFIER_FORMULA_INFO_CAPTURE,
        )
    if protect_launcher:
        prepared = prepared.replace(
            _CANDIDATE_LAUNCHER_LOAD,
            '. "${KANDELO_ABI_STAGING_PROTECTED_LAUNCHER:?}"',
        )
    if destination.exists() or destination.is_symlink():
        raise ExecutionError("protected staging builder destination already exists")
    try:
        destination.write_text(prepared, encoding="utf-8", errors="strict")
        destination.chmod(0o500)
    except OSError as error:
        raise ExecutionError(f"cannot write protected staging builder: {error}") from error
    return destination.resolve(strict=True)


def _prepare_staging_recipe_runner(*, source: Path, destination: Path) -> Path:
    """Expand only the staging source-input envelope for large upstream trees."""

    try:
        metadata = source.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ExecutionError("candidate recipe runner must be a regular file")
        if metadata.st_size < 1 or metadata.st_size > 2 * 1024 * 1024:
            raise ExecutionError("candidate recipe runner is outside its byte bound")
        body = source.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as error:
        raise ExecutionError(f"cannot read candidate recipe runner: {error}") from error
    if "\r" in body or not body.endswith("\n"):
        raise ExecutionError("candidate recipe runner is not canonical LF text")
    if body.count(_RECIPE_RUNNER_LIMITS) != 1:
        raise ExecutionError("candidate recipe runner limit boundary changed")
    if body.count(_RECIPE_RUNNER_SOURCE_COPY) != 1:
        raise ExecutionError("candidate recipe runner source-copy boundary changed")
    for marker, label in (
        (_RECIPE_RUNNER_PLATFORM_ENVIRONMENT_SIGNATURE, "platform environment"),
        (_RECIPE_RUNNER_CONFIG_SITE_ENVIRONMENT, "config-site environment"),
        (_RECIPE_RUNNER_PLATFORM_ENVIRONMENT_CALL, "platform environment call"),
        (_RECIPE_RUNNER_PROTECTED_ROOT, "protected root"),
        (
            _RECIPE_RUNNER_PROTECTED_ROOT_CONFIG_CHECK,
            "protected-root config check",
        ),
    ):
        if body.count(marker) != 1:
            raise ExecutionError(f"candidate recipe runner {label} boundary changed")
    prepared = body.replace(
        _RECIPE_RUNNER_LIMITS,
        _STAGING_RECIPE_RUNNER_LIMITS,
    ).replace(
        _RECIPE_RUNNER_SOURCE_COPY,
        _STAGING_RECIPE_RUNNER_SOURCE_COPY,
    ).replace(
        _RECIPE_RUNNER_PLATFORM_ENVIRONMENT_SIGNATURE,
        _STAGING_RECIPE_RUNNER_PLATFORM_ENVIRONMENT_SIGNATURE,
    ).replace(
        _RECIPE_RUNNER_CONFIG_SITE_ENVIRONMENT,
        _STAGING_RECIPE_RUNNER_CONFIG_SITE_ENVIRONMENT,
    ).replace(
        _RECIPE_RUNNER_PLATFORM_ENVIRONMENT_CALL,
        _STAGING_RECIPE_RUNNER_PLATFORM_ENVIRONMENT_CALL,
    )
    if destination.exists() or destination.is_symlink():
        raise ExecutionError("protected recipe runner destination already exists")
    try:
        destination.write_text(prepared, encoding="utf-8", errors="strict")
        destination.chmod(0o500)
    except OSError as error:
        raise ExecutionError(f"cannot write protected recipe runner: {error}") from error
    return destination.resolve(strict=True)


def _prepare_staging_launcher(
    *, source: Path, destination: Path, protected_recipe_runner: Path
) -> Path:
    """Derive a staging launcher that admits protected runner and poured inputs."""

    try:
        runner_metadata = protected_recipe_runner.lstat()
        if (
            stat.S_ISLNK(runner_metadata.st_mode)
            or not stat.S_ISREG(runner_metadata.st_mode)
            or stat.S_IMODE(runner_metadata.st_mode) != 0o500
            or runner_metadata.st_nlink != 1
            or runner_metadata.st_size < 1
            or runner_metadata.st_size > 2 * 1024 * 1024
        ):
            raise ExecutionError("protected recipe runner has an invalid state")
        runner = protected_recipe_runner.resolve(strict=True)
        runner_sha256 = hashlib.sha256(runner.read_bytes()).hexdigest()
        metadata = source.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ExecutionError("candidate patched launcher must be a regular file")
        if metadata.st_size < 1 or metadata.st_size > 2 * 1024 * 1024:
            raise ExecutionError("candidate patched launcher is outside its byte bound")
        body = source.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as error:
        raise ExecutionError(f"cannot read staging launcher input: {error}") from error
    if "\r" in body or not body.endswith("\n"):
        raise ExecutionError("candidate patched launcher is not canonical LF text")
    for marker, label in (
        (_RECIPE_RUNNER_SOURCE_ASSIGNMENT, "recipe runner source"),
        (_RECIPE_RUNNER_SOURCE_AUTHORITY_COMMENT, "recipe runner authority"),
        (_RECIPE_RUNNER_SOURCE_STATE, "recipe runner state"),
        (_RECIPE_RUNNER_SOURCE_DIGEST_CHECK, "recipe runner digest"),
        (_TARGET_CELLAR_AUDIT, "target Cellar audit"),
        (_PROTECTED_ROOT_PARENT_AUDIT, "protected-root parent audit"),
    ):
        if body.count(marker) != 1:
            raise ExecutionError(f"candidate patched launcher {label} boundary changed")
    if body.count("/run/kandelo-homebrew-publisher") != 2:
        raise ExecutionError("candidate patched launcher protected-root boundary changed")
    prepared = body.replace(
        _RECIPE_RUNNER_SOURCE_ASSIGNMENT,
        f"  local source={shlex.quote(str(runner))}",
    ).replace(
        _RECIPE_RUNNER_SOURCE_AUTHORITY_COMMENT,
        _STAGING_RECIPE_RUNNER_SOURCE_AUTHORITY_COMMENT,
    ).replace(
        _RECIPE_RUNNER_SOURCE_STATE,
        (
            '       2>/dev/null || true)" != '
            f'"{os.getuid()}:{os.getgid()}:500:1" ]; then'
        ),
    ).replace(
        _RECIPE_RUNNER_SOURCE_DIGEST_CHECK,
        f'  if [ "$source_sha" != "{runner_sha256}" ]; then',
    ).replace(
        _TARGET_CELLAR_AUDIT,
        _STAGING_TARGET_CELLAR_AUDIT,
    ).replace(
        _PROTECTED_ROOT_PARENT_AUDIT,
        _STAGING_PROTECTED_ROOT_PARENT_AUDIT,
    )
    if destination.exists() or destination.is_symlink():
        raise ExecutionError("protected staging launcher destination already exists")
    try:
        destination.write_text(prepared, encoding="utf-8", errors="strict")
        destination.chmod(0o500)
    except OSError as error:
        raise ExecutionError(f"cannot write protected staging launcher: {error}") from error
    return destination.resolve(strict=True)


def _prepare_staging_verification_adapter(
    *, source: Path, destination: Path
) -> Path:
    """Bind the exact verifier to the protected staging-only dependency path."""

    try:
        metadata = source.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ExecutionError("candidate verification adapter must be a regular file")
        if metadata.st_size < 1 or metadata.st_size > 2 * 1024 * 1024:
            raise ExecutionError("candidate verification adapter is outside its byte bound")
        body = source.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as error:
        raise ExecutionError(
            f"cannot read candidate verification adapter: {error}"
        ) from error
    if "\r" in body or not body.endswith("\n"):
        raise ExecutionError("candidate verification adapter is not canonical LF text")
    if body.count(_VERIFICATION_ADAPTER_ROOT_ASSIGNMENT) != 1:
        raise ExecutionError("candidate verification adapter root boundary changed")
    if body.count(_VERIFICATION_ADAPTER_NORMAL_ASSIGNMENT) != 1:
        raise ExecutionError("candidate normal verifier selection boundary changed")
    prepared = body.replace(
        _VERIFICATION_ADAPTER_ROOT_ASSIGNMENT,
        'REPO_ROOT="${KANDELO_ABI_STAGING_CANDIDATE_ROOT:?}"',
    ).replace(
        _VERIFICATION_ADAPTER_NORMAL_ASSIGNMENT,
        'NORMAL_VERIFIER="${KANDELO_ABI_STAGING_PROTECTED_NORMAL_VERIFIER:?}"',
    )
    if destination.exists() or destination.is_symlink():
        raise ExecutionError(
            "protected staging verification adapter destination already exists"
        )
    try:
        destination.write_text(prepared, encoding="utf-8", errors="strict")
        destination.chmod(0o500)
    except OSError as error:
        raise ExecutionError(
            f"cannot write protected staging verification adapter: {error}"
        ) from error
    return destination.resolve(strict=True)


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
        parsed = parse_canonical_bytes(
            body,
            maximum_bytes=MAX_COORDINATION_BYTES,
            maximum_items=MAX_COORDINATION_JSON_ITEMS,
        )
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
    subject = exact_formula_subject(
        str(dependency.get("formula")), str(dependency.get("architecture"))
    )
    dependency_plan = _formula_for_subject(bundle, subject)
    expected_contract_sha256 = _digest(
        dependency_plan.get("contract_sha256"),
        "dependency Formula contract",
    )
    # A request can retain an older same-layer reuse record after the exact
    # current candidate is published. Only the current Formula contract may
    # supply build inputs for this coordinated plan.
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
            and formula.get("bottle_contract_sha256")
            == expected_contract_sha256
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
            or formula.get("bottle_contract_sha256")
            != expected_contract_sha256
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


def _build_dependency_closure(
    bundle: Mapping[str, Any], contract: Mapping[str, Any]
) -> list[tuple[dict[str, Any], Mapping[str, Any], Mapping[str, Any]]]:
    """Resolve every exact candidate reachable from a build contract."""

    resolved: dict[
        tuple[str, str],
        tuple[dict[str, Any], Mapping[str, Any], Mapping[str, Any]],
    ] = {}
    visiting: set[str] = set()

    def visit(record: Mapping[str, Any], locator: Mapping[str, Any]) -> None:
        payload = _mapping(record.get("candidate"), "dependency candidate payload")
        formula = _mapping(payload.get("formula"), "dependency candidate Formula")
        layer = _mapping(
            payload.get("bottle_layer"), "dependency candidate bottle layer"
        )
        name = formula.get("formula")
        architecture = formula.get("architecture")
        if (
            not isinstance(name, str)
            or not name
            or architecture not in {"wasm32", "wasm64"}
        ):
            raise ExecutionError("dependency candidate Formula identity is invalid")
        entry = {
            "architecture": architecture,
            "bottle_layer_bytes": layer.get("bytes"),
            "bottle_layer_sha256": layer.get("sha256"),
            "formula": name,
        }
        _digest(entry["bottle_layer_sha256"], "dependency candidate layer")
        if (
            isinstance(entry["bottle_layer_bytes"], bool)
            or not isinstance(entry["bottle_layer_bytes"], int)
            or entry["bottle_layer_bytes"] <= 0
        ):
            raise ExecutionError("dependency candidate layer size is invalid")
        key = (name, architecture)
        prior = resolved.get(key)
        if prior is not None:
            if prior != (entry, record, locator):
                raise ExecutionError(
                    "build dependency closure conflicts by Formula architecture"
                )
            return
        locator_digest = locator.get("digest")
        if (
            not isinstance(locator_digest, str)
            or not locator_digest.startswith("sha256:")
            or SHA256.fullmatch(locator_digest.removeprefix("sha256:")) is None
        ):
            raise ExecutionError("dependency candidate locator digest is invalid")
        record_sha256 = locator_digest.removeprefix("sha256:")
        if record_sha256 in visiting:
            raise ExecutionError("build dependency closure contains a cycle")
        if len(resolved) + len(visiting) >= 256:
            raise ExecutionError("build dependency closure exceeds its bound")
        visiting.add(record_sha256)
        direct_layers = payload.get("direct_dependency_layers", ())
        if not isinstance(direct_layers, Sequence) or isinstance(
            direct_layers, (str, bytes, bytearray)
        ):
            raise ExecutionError("dependency candidate direct layers are invalid")
        for direct in direct_layers:
            _record_sha256, child, child_locator = _dependency_candidate(
                bundle, _mapping(direct, "candidate dependency")
            )
            visit(child, child_locator)
        visiting.remove(record_sha256)
        resolved[key] = (entry, record, locator)

    direct_dependencies = contract.get("direct_dependencies", ())
    if not isinstance(direct_dependencies, Sequence) or isinstance(
        direct_dependencies, (str, bytes, bytearray)
    ):
        raise ExecutionError("build contract direct dependencies are invalid")
    for raw_dependency in direct_dependencies:
        dependency = _mapping(raw_dependency, "build contract dependency")
        record, locator = _matching_dependency(bundle, dependency)
        payload = _mapping(record.get("candidate"), "dependency candidate payload")
        formula = _mapping(payload.get("formula"), "dependency candidate Formula")
        layer = _mapping(payload.get("bottle_layer"), "dependency candidate layer")
        if (
            formula.get("formula") != dependency.get("formula")
            or formula.get("architecture") != dependency.get("architecture")
            or layer.get("sha256") != dependency.get("bottle_layer_sha256")
            or layer.get("bytes") != dependency.get("bottle_layer_bytes")
        ):
            raise ExecutionError(
                "build contract dependency differs from its exact candidate"
            )
        visit(record, locator)
    return [resolved[key] for key in sorted(resolved)]


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
    closure = _build_dependency_closure(bundle, contract)
    for dependency, record, _locator in closure:
        formula = _mapping(record.get("candidate"), "dependency candidate payload")
        formula_identity = _mapping(
            formula.get("formula"), "dependency candidate Formula"
        )
        subject = exact_formula_subject(
            str(formula_identity.get("formula")),
            str(formula_identity.get("architecture")),
        )
        dependency_plan = _formula_for_subject(bundle, subject)
        dependency_contract_sha256 = dependency_plan.get("contract_sha256")
        dependency_contract = _mapping(
            _mapping(bundle.get("contracts"), "coordination contracts").get(subject),
            "dependency bottle contract",
        )
        if (
            canonical_sha256(dependency_contract) != dependency_contract_sha256
            or formula_identity.get("bottle_contract_sha256")
            != dependency_contract_sha256
        ):
            raise ExecutionError(
                "dependency candidate differs from its exact bottle contract"
            )
        documents[
            root
            / "contracts"
            / f"sha256-{dependency_contract_sha256}.json"
        ] = dependency_contract
    try:
        for path, value in documents.items():
            path.write_bytes(canonical_bytes(value))
        for dependency, record, locator in closure:
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
        staging_recipe_runner = _prepare_staging_recipe_runner(
            source=kandelo / "scripts/homebrew-tap-recipe-runner.py",
            destination=Path(temporary) / "protected-recipe-runner.py",
        )
        staging_launcher = _prepare_staging_launcher(
            source=kandelo / "scripts/homebrew-patched-launcher.sh",
            destination=Path(temporary) / "protected-launcher.sh",
            protected_recipe_runner=staging_recipe_runner,
        )
        staging_builder = _prepare_staging_normal_builder(
            source=kandelo / "scripts/homebrew-bottle-build.sh",
            destination=Path(temporary) / "protected-normal-builder.sh",
            protect_launcher=True,
        )
        child_environment.update(
            {
                # The exact tap checkout is already selected, revalidated, and
                # sealed by this protected executor. Homebrew canonicalizes its
                # temporary file:// clone as a custom remote, so its mutable
                # user trust store cannot identify the same reviewed tap.
                "HOMEBREW_NO_REQUIRE_TAP_TRUST": "1",
                "KANDELO_ABI_STAGING_CANDIDATE_ROOT": str(kandelo),
                "KANDELO_ABI_STAGING_NORMAL_BUILDER": str(staging_builder),
                "KANDELO_ABI_STAGING_PROTECTED_NORMAL_BUILDER": "1",
                "KANDELO_ABI_STAGING_PROTECTED_LAUNCHER": str(staging_launcher),
                "KANDELO_ABI_STAGING_PROTECTED_RECIPE_RUNNER": str(
                    staging_recipe_runner
                ),
            }
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


def normalize_candidate_bottle_metadata(
    candidate: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[dict[str, Any], str, str]:
    """Reduce authenticated Homebrew output to the strict Formula composer input."""

    formula = candidate.get("formula")
    tap_repository = candidate.get("tap_repository")
    target_abi = candidate.get("target_abi")
    architecture = candidate.get("architecture")
    try:
        formula_key = bottle_metadata_formula_key(tap_repository, formula)
    except PlanError as error:
        raise ExecutionError("candidate bottle metadata identity is invalid") from error
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
    architecture = next(
        (
            candidate
            for candidate in ("wasm32", "wasm64")
            if isinstance(direct_id, str) and direct_id.endswith(f"-{candidate}")
        ),
        None,
    )
    if architecture is None:
        raise ExecutionError("candidate dependency identity is invalid")
    formula = direct_id.removesuffix(f"-{architecture}")
    if not formula:
        raise ExecutionError("candidate dependency identity is invalid")
    dependency = {
        "architecture": architecture,
        "bottle_layer_bytes": artifact.get("bytes"),
        "bottle_layer_sha256": artifact.get("sha256"),
        "formula": formula,
    }
    record, locator = _matching_dependency(bundle, dependency)
    payload = _mapping(record.get("candidate"), "dependency candidate payload")
    if payload.get("bottle_layer") != artifact:
        raise ExecutionError("candidate dependency differs from its exact binding")
    locator_digest = locator.get("digest")
    if (
        not isinstance(locator_digest, str)
        or not locator_digest.startswith("sha256:")
        or SHA256.fullmatch(locator_digest.removeprefix("sha256:")) is None
    ):
        raise ExecutionError("candidate dependency locator digest is invalid")
    return locator_digest.removeprefix("sha256:"), dict(record), dict(locator)


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
    dependency_cache = root / "dependency-cache"
    try:
        dependency_cache.mkdir()
        for candidate in prepared_candidates:
            if candidate is target:
                continue
            dependency_sha256 = candidate["bottle_layer"]["sha256"]
            dependency_archive = dependency_cache / f"{dependency_sha256}.tar.gz"
            body = candidate["bottle"].read_bytes()
            if dependency_archive.exists():
                if dependency_archive.read_bytes() != body:
                    raise ExecutionError(
                        "dependency layer digest collision changed bytes"
                    )
                continue
            dependency_archive.write_bytes(body)
            dependency_archive.chmod(0o400)
        dependency_cache.chmod(0o500)
    except OSError as error:
        raise ExecutionError(
            f"cannot materialize local dependency cache: {error}"
        ) from error
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
        "dependency_cache": dependency_cache,
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
            normalize_candidate_bottle_metadata(candidate, metadata_entries)
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
    try:
        subprocess.run(
            ["git", "-C", str(destination), "add", "--", *sorted(allowed)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        tree = subprocess.run(
            ["git", "-C", str(destination), "write-tree"],
            check=True,
            stdin=subprocess.DEVNULL,
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
                str(destination),
                "commit-tree",
                tree,
                "-p",
                expected_head,
                "-m",
                "Prepare exact ABI candidate verification closure",
            ],
            check=True,
            env=commit_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        ).stdout.decode("ascii").strip()
        subprocess.run(
            ["git", "-C", str(destination), "reset", "--hard", prepared_commit],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        prepared_status = subprocess.run(
            [
                "git",
                "-C",
                str(destination),
                "status",
                "--short",
                "--untracked-files=all",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ExecutionError(f"cannot seal candidate verification tap: {error}") from error
    if prepared_status:
        raise ExecutionError("candidate verification tap is not clean after sealing")
    return destination.resolve(strict=True)


def _prepare_verification_resolved_taps(
    *,
    source_path: Path,
    destination: Path,
    tap_root: Path,
    composed_tap: Path,
    tap_source: Mapping[str, Any],
) -> str:
    """Bind dependency parsing to the clean prepared verification checkout."""

    try:
        metadata = source_path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ExecutionError(
                "resolved tap map must be a regular non-symlink file"
            )
        if metadata.st_size > 65_536:
            raise ExecutionError("resolved tap map exceeds its byte limit")
        document = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if isinstance(error, ExecutionError):
            raise
        raise ExecutionError(f"cannot read resolved tap map: {error}") from error
    root = dict(_mapping(document, "resolved tap map"))
    if frozenset(root) != frozenset({"dependencies", "primary", "schema"}):
        raise ExecutionError("resolved tap map fields changed")
    schema = root.get("schema")
    if schema not in {1, 2}:
        raise ExecutionError("resolved tap map schema is unsupported")
    primary = dict(_mapping(root.get("primary"), "resolved tap map primary"))
    primary_fields = {
        "root",
        "tap_commit",
        "tap_name",
        "tap_repository",
    }
    if schema == 2:
        primary_fields.add("checkout_commit")
    primary_repository = primary.get("tap_repository")
    primary_name = primary.get("tap_name")
    if (
        not isinstance(root.get("dependencies"), list)
        or frozenset(primary) != frozenset(primary_fields)
        or primary_repository != tap_source.get("repository")
        or primary.get("tap_commit") != tap_source.get("commit")
        or (schema == 2 and primary.get("checkout_commit") != tap_source.get("commit"))
        or not isinstance(primary_repository, str)
        or TAP_REPOSITORY.fullmatch(primary_repository) is None
        or not isinstance(primary_name, str)
        or TAP_NAME.fullmatch(primary_name) is None
        or primary_name
        != (
            f"{primary_repository.split('/', 1)[0]}/"
            f"{primary_repository.split('/', 1)[1].removeprefix('homebrew-')}"
        )
    ):
        raise ExecutionError("resolved tap map primary differs from coordinated source")
    try:
        source_root = Path(str(primary.get("root"))).resolve(strict=True)
    except OSError as error:
        raise ExecutionError(f"resolved primary tap is unavailable: {error}") from error
    if source_root != tap_root:
        raise ExecutionError("resolved tap map primary root differs from protected tap")

    lock_path = tap_root / "Kandelo" / "dependency-taps.json"
    try:
        if lock_path.exists() or lock_path.is_symlink():
            lock_metadata = lock_path.lstat()
            if stat.S_ISLNK(lock_metadata.st_mode) or not stat.S_ISREG(
                lock_metadata.st_mode
            ):
                raise ExecutionError(
                    "protected dependency tap lock must be a regular non-symlink file"
                )
            if lock_metadata.st_size > 65_536:
                raise ExecutionError("protected dependency tap lock exceeds its byte limit")
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        else:
            lock = {"schema": 1, "taps": []}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if isinstance(error, ExecutionError):
            raise
        raise ExecutionError(f"cannot read protected dependency tap lock: {error}") from error
    lock_root = dict(_mapping(lock, "protected dependency tap lock"))
    if frozenset(lock_root) != frozenset({"schema", "taps"}):
        raise ExecutionError("protected dependency tap lock fields changed")
    locked_taps = lock_root.get("taps")
    if (
        lock_root.get("schema") != 1
        or not isinstance(locked_taps, list)
        or len(locked_taps) > 8
    ):
        raise ExecutionError("protected dependency tap lock is invalid")

    expected_dependencies: list[dict[str, str]] = []
    prior_name = ""
    seen_repositories: set[str] = set()
    for index, value in enumerate(locked_taps):
        entry = dict(_mapping(value, f"protected dependency tap lock taps[{index}]"))
        if frozenset(entry) != frozenset(
            {"tap_commit", "tap_name", "tap_repository"}
        ):
            raise ExecutionError("protected dependency tap lock entry fields changed")
        name = entry.get("tap_name")
        repository = entry.get("tap_repository")
        commit = entry.get("tap_commit")
        if (
            not isinstance(name, str)
            or TAP_NAME.fullmatch(name) is None
            or name <= prior_name
            or not isinstance(repository, str)
            or TAP_REPOSITORY.fullmatch(repository) is None
            or repository in seen_repositories
            or name
            != (
                f"{repository.split('/', 1)[0]}/"
                f"{repository.split('/', 1)[1].removeprefix('homebrew-')}"
            )
            or not isinstance(commit, str)
            or COMMIT.fullmatch(commit) is None
        ):
            raise ExecutionError("protected dependency tap lock entry is invalid")
        prior_name = name
        seen_repositories.add(repository)
        expected_dependencies.append(entry)

    dependencies = root["dependencies"]
    if len(dependencies) != len(expected_dependencies):
        raise ExecutionError("dependency tap map differs from the protected lock")
    prepared_dependencies: list[dict[str, str]] = []
    for index, (value, expected) in enumerate(
        zip(dependencies, expected_dependencies, strict=True)
    ):
        entry = dict(_mapping(value, f"resolved dependency tap[{index}]"))
        entry_fields = {
            "root",
            "tap_commit",
            "tap_name",
            "tap_repository",
        }
        if schema == 2:
            entry_fields.add("checkout_commit")
        if (
            frozenset(entry) != frozenset(entry_fields)
            or {key: entry.get(key) for key in expected} != expected
            or (schema == 2 and entry.get("checkout_commit") != expected["tap_commit"])
        ):
            raise ExecutionError("dependency tap map differs from the protected lock")
        try:
            dependency_root = Path(str(entry.get("root")))
            if dependency_root.is_symlink() or not dependency_root.is_dir():
                raise ExecutionError("resolved dependency tap root is unavailable")
            dependency_root = dependency_root.resolve(strict=True)
            formula_root = dependency_root / "Formula"
            if formula_root.is_symlink() or not formula_root.is_dir():
                raise ExecutionError("resolved dependency tap Formula root is unavailable")
            dependency_head = subprocess.run(
                ["git", "-C", str(dependency_root), "rev-parse", "HEAD"],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            ).stdout.decode("ascii").strip()
            dependency_status = subprocess.run(
                [
                    "git",
                    "-C",
                    str(dependency_root),
                    "status",
                    "--short",
                    "--untracked-files=all",
                ],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            ).stdout
        except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as error:
            if isinstance(error, ExecutionError):
                raise
            raise ExecutionError(f"cannot inspect resolved dependency tap: {error}") from error
        if dependency_head != expected["tap_commit"] or dependency_status:
            raise ExecutionError("resolved dependency tap differs from the protected lock")
        prepared_dependencies.append(
            {**entry, "checkout_commit": expected["tap_commit"], "root": str(dependency_root)}
        )
    try:
        composed_head = subprocess.run(
            ["git", "-C", str(composed_tap), "rev-parse", "HEAD"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        ).stdout.decode("ascii").strip()
        status = subprocess.run(
            [
                "git",
                "-C",
                str(composed_tap),
                "status",
                "--short",
                "--untracked-files=all",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        ).stdout
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(composed_tap),
                "merge-base",
                "--is-ancestor",
                str(tap_source.get("commit")),
                composed_head,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as error:
        raise ExecutionError(f"cannot inspect prepared verification tap: {error}") from error
    if (
        re.fullmatch(r"[0-9a-f]{40}", composed_head) is None
        or composed_head == tap_source.get("commit")
        or status
        or ancestry.returncode != 0
    ):
        raise ExecutionError("prepared verification tap is not one clean descendant")
    root["schema"] = 2
    root["dependencies"] = prepared_dependencies
    root["primary"] = {
        **primary,
        "checkout_commit": composed_head,
        "root": str(composed_tap),
    }
    payload = (json.dumps(root, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > 65_536:
        raise ExecutionError("prepared resolved tap map exceeds its byte limit")
    try:
        destination.write_bytes(payload)
        destination.chmod(0o600)
    except OSError as error:
        raise ExecutionError(f"cannot write prepared resolved tap map: {error}") from error
    return composed_head


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
    candidate_adapter = kandelo / "scripts/abi-staging-verify-bottle.sh"
    if candidate_adapter.is_symlink() or not candidate_adapter.is_file():
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
        protected_normal_verifier = _prepare_staging_normal_builder(
            source=kandelo / "scripts/homebrew-verify-poured-bottle.sh",
            destination=temporary_root / "protected-normal-verifier.sh",
            root_assignment=_CANDIDATE_VERIFIER_ROOT_ASSIGNMENT,
            dependency_install=_VERIFIER_FORMULA_DEPENDENCY_INSTALL,
            formula_info_capture=_VERIFIER_FORMULA_INFO_CAPTURE,
        )
        adapter = _prepare_staging_verification_adapter(
            source=candidate_adapter,
            destination=temporary_root / "protected-verification-adapter.sh",
        )
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
            # The target archive is already selected, fetched, and poured by
            # exact digest.  Composing its candidate bottle block would replace
            # the protected Formula's existing bottle block before the archived
            # Formula receipt is compared, manufacturing a source mismatch.
            # Only dependency Formulae need candidate bottle blocks so Homebrew
            # can resolve the locally authenticated closure.
            candidates=[
                candidate
                for candidate in prepared["candidates"]
                if candidate is not prepared["target"]
            ],
        )
        child_environment = _uncredentialed_environment(
            os.environ if environment is None else environment,
            sandbox_root=temporary_root / "environment",
        )
        resolved_taps_value = child_environment.get(
            "KANDELO_HOMEBREW_RESOLVED_TAPS_FILE"
        )
        if not isinstance(resolved_taps_value, str) or not resolved_taps_value:
            raise ExecutionError("resolved tap map is unavailable")
        composed_head = _prepare_verification_resolved_taps(
            source_path=Path(resolved_taps_value),
            destination=temporary_root / "resolved-taps.json",
            tap_root=tap,
            composed_tap=composed_tap,
            tap_source=bundle["tap_plan"]["tap_source"],
        )
        child_environment["KANDELO_HOMEBREW_RESOLVED_TAPS_FILE"] = str(
            temporary_root / "resolved-taps.json"
        )
        # Reused candidates deliberately need no mutable Homebrew version tag.
        # The exact verifier already validates and installs local dependency
        # archives; expose only the public, digest-authenticated closure that
        # preparation just materialized.
        child_environment["KANDELO_HOMEBREW_LOCAL_DEPENDENCY_CACHE"] = str(
            prepared["dependency_cache"].resolve(strict=True)
        )
        child_environment["KANDELO_ABI_STAGING_CANDIDATE_ROOT"] = str(kandelo)
        child_environment["KANDELO_ABI_STAGING_PROTECTED_NORMAL_VERIFIER"] = str(
            protected_normal_verifier
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
            composed_head,
            "--dependency-provenance",
            str(prepared["dependency_provenance"]),
            "--sysroot-build-root",
            str(kandelo),
        ]
        for forbidden in sorted({str(kandelo), str(tap), str(prepared["root"])}):
            command.extend(["--forbidden-root", forbidden])
        command.extend(["--out", str(output)])
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
