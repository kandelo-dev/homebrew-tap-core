#!/usr/bin/env python3
"""Admit and seal one protected Homebrew prefix-campaign task."""

from __future__ import annotations

import argparse
import dataclasses
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
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NoReturn


sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_AUTHORITY = ROOT / "Kandelo/prefix-campaign-authority.json"
CAMPAIGN_ASSET = "campaign.json"
EVENT_TYPE = "publish-prefix-campaign-bottle"
FORMULA = re.compile(r"^[a-z0-9][a-z0-9._-]{0,254}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)
REPOSITORY_PATH = re.compile(
    r"^[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*$"
)
CAMPAIGN_TAG = re.compile(
    r"^homebrew-prefix-campaign-sha256-([0-9a-f]{64})$"
)
HANDOFF_TAG = re.compile(
    r"^homebrew-prefix-handoff-sha256-([0-9a-f]{64})$"
)
ROOTFS_GENERATION = re.compile(
    r"^package-generation-rootfs-wasm32-abi-v42-"
    r"sha256-([0-9a-f]{64})$"
)
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_HANDOFF_ASSET_BYTES = 2 * 1024 * 1024 * 1024
MAX_HANDOFF_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_COMMAND_OUTPUT = 16 * 1024 * 1024
MAX_DEPENDENCIES = 256
MAX_FORMULAE = 256
COMMAND_TIMEOUT = 1800
INERT_EXIT = 78
UNAVAILABLE_EXIT = 69
HANDOFF_KIND = "kandelo-homebrew-prefix-formula-handoff"
BUILD_HANDOFF_PUBLICATION_FILES = (
    "build/bottle.json",
    "build/bottle.tar.gz",
    "build/dependency-provenance.json",
    "build/manifest.json",
    "composition/sidecars-input.json",
    "receipt.json",
)
REUSE_HANDOFF_PUBLICATION_FILES = (
    "composition/sidecars-input.json",
    "reuse/bottle.json",
    "reuse/bottle.tar.gz",
    "reuse/evidence.json",
)
TOKEN_ENV = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "HOMEBREW_GITHUB_API_TOKEN",
    "HOMEBREW_GITHUB_PACKAGES_TOKEN",
    "HOMEBREW_DOCKER_REGISTRY_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "ACTIONS_RUNTIME_TOKEN",
)


class ControllerError(RuntimeError):
    """A fail-closed campaign controller error."""

    def __init__(
        self,
        status: str,
        message: str,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.exit_code = exit_code


@dataclasses.dataclass(frozen=True)
class Authority:
    kandelo_repository: str
    kandelo_commit: str
    source_tap_repository: str
    source_tap_name: str
    source_tap_commit: str
    campaign_repository: str
    campaign_tag: str
    rootfs_wasm32: str
    release_tag: str
    reusable_workflow_commit: str
    target_manifest_path: str
    target_manifest_sha256: str
    target_source_root: str
    target_source_tree: str
    target_tree: str
    state: str
    payload: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclasses.dataclass(frozen=True)
class TaskRequest:
    formula: str
    arches: tuple[str, ...]
    dependency_tags: tuple[tuple[str, str], ...]

    def dependency_document(self) -> dict[str, Any]:
        return {
            "dependencies": [
                {"formula": formula, "tag": tag}
                for formula, tag in self.dependency_tags
            ],
            "schema": 1,
        }


@dataclasses.dataclass(frozen=True)
class TaskPlan:
    request: TaskRequest
    admission_kind: str
    disposition: str
    generation_kind: str
    old_tap_commit: str
    campaign_path: pathlib.Path
    campaign_payload: bytes
    campaign: dict[str, Any]
    formula: dict[str, Any]


def fail(
    status: str,
    message: str,
    exit_code: int = 1,
) -> NoReturn:
    raise ControllerError(status, message, exit_code)


def duplicate_rejecting_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("invalid-json", f"JSON object repeats key {key!r}", 2)
        result[key] = value
    return result


def pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def load_json_bytes(
    path: pathlib.Path,
    label: str,
    *,
    canonical: bool = False,
) -> tuple[Any, bytes]:
    try:
        metadata = path.lstat()
    except OSError as error:
        fail("invalid-input", f"{label} is unavailable: {error}", 2)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_size < 1
        or metadata.st_size > MAX_JSON_BYTES
    ):
        fail(
            "invalid-input",
            f"{label} must be one bounded regular file",
            2,
        )
    payload = path.read_bytes()
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=duplicate_rejecting_object,
            parse_constant=lambda item: fail(
                "invalid-json",
                f"{label} contains {item}",
                2,
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(
            "invalid-json",
            f"{label} is not strict UTF-8 JSON: {error}",
            2,
        )
    if canonical and payload != pretty_json(value):
        fail(
            "noncanonical-json",
            f"{label} is not canonical pretty JSON",
            2,
        )
    return value, payload


def exact_keys(
    value: Any,
    expected: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        fail(
            "invalid-contract",
            f"{label} must contain exactly {sorted(expected)}",
            2,
        )
    return value


def require_string(
    value: Any,
    label: str,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        fail("invalid-contract", f"{label} is invalid", 2)
    return value


def require_integer(
    value: Any,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        fail("invalid-contract", f"{label} is invalid", 2)
    return value


def load_authority(
    path: pathlib.Path,
    *,
    require_active: bool,
) -> Authority:
    value, payload = load_json_bytes(
        path,
        "campaign caller authority",
        canonical=True,
    )
    value = exact_keys(
        value,
        {
            "campaign_release",
            "kandelo_commit",
            "kandelo_repository",
            "kind",
            "package_generations",
            "release_tag",
            "reusable_workflow_commit",
            "schema",
            "source_tap_commit",
            "source_tap_name",
            "source_tap_repository",
            "state",
            "target_source",
        },
        "campaign caller authority",
    )
    campaign = exact_keys(
        value["campaign_release"],
        {"repository", "tag"},
        "campaign release authority",
    )
    generations = exact_keys(
        value["package_generations"],
        {"rootfs_wasm32"},
        "package generation authority",
    )
    target_source = exact_keys(
        value["target_source"],
        {
            "manifest_path",
            "manifest_sha256",
            "source_root",
            "source_tree_git_oid",
            "target_tree_git_oid",
        },
        "campaign target-source authority",
    )
    if (
        value["schema"] != 2
        or value["kind"]
        != "kandelo-homebrew-prefix-campaign-caller-authority"
        or value["state"] not in ("inert", "armed", "active")
    ):
        fail(
            "invalid-contract",
            "campaign caller authority has an unsupported contract",
            2,
        )
    authority = Authority(
        kandelo_repository=require_string(
            value["kandelo_repository"],
            "Kandelo repository",
            REPOSITORY,
        ),
        kandelo_commit=require_string(
            value["kandelo_commit"],
            "Kandelo commit",
            SHA,
        ),
        source_tap_repository=require_string(
            value["source_tap_repository"],
            "source tap repository",
            REPOSITORY,
        ).lower(),
        source_tap_name=require_string(
            value["source_tap_name"],
            "source tap name",
            REPOSITORY,
        ).lower(),
        source_tap_commit=require_string(
            value["source_tap_commit"],
            "source tap commit",
            SHA,
        ),
        campaign_repository=require_string(
            campaign["repository"],
            "campaign release repository",
            REPOSITORY,
        ).lower(),
        campaign_tag=require_string(
            campaign["tag"],
            "campaign release tag",
            CAMPAIGN_TAG,
        ),
        rootfs_wasm32=require_string(
            generations["rootfs_wasm32"],
            "rootfs wasm32 generation",
            ROOTFS_GENERATION,
        ),
        release_tag=require_string(
            value["release_tag"],
            "bottle release tag",
        ),
        reusable_workflow_commit=require_string(
            value["reusable_workflow_commit"],
            "reusable workflow commit",
            SHA,
        ),
        target_manifest_path=require_string(
            target_source["manifest_path"],
            "target source manifest path",
            REPOSITORY_PATH,
        ),
        target_manifest_sha256=require_string(
            target_source["manifest_sha256"],
            "target source manifest SHA-256",
            SHA256,
        ),
        target_source_root=require_string(
            target_source["source_root"],
            "target source root",
            REPOSITORY_PATH,
        ),
        target_source_tree=require_string(
            target_source["source_tree_git_oid"],
            "target source tree",
            SHA,
        ),
        target_tree=require_string(
            target_source["target_tree_git_oid"],
            "target reconstructed tree",
            SHA,
        ),
        state=value["state"],
        payload=payload,
    )
    if (
        authority.kandelo_repository != "Automattic/kandelo"
        or authority.source_tap_repository
        != "kandelo-dev/homebrew-tap-core"
        or authority.source_tap_name != "kandelo-dev/tap-core"
        or authority.campaign_repository
        != authority.source_tap_repository
        or authority.release_tag != "bottles-abi-v42"
        or authority.reusable_workflow_commit
        != authority.kandelo_commit
        or authority.target_manifest_path
        != "Kandelo/campaigns/prefix-v1/manifest.json"
        or authority.target_source_root
        != "Kandelo/campaigns/prefix-v1/source"
        or set(authority.target_manifest_sha256) == {"0"}
        or set(authority.target_source_tree) == {"0"}
        or set(authority.target_tree) == {"0"}
    ):
        fail(
            "invalid-contract",
            "campaign caller authority changes a fixed repository identity",
            2,
        )
    campaign_match = CAMPAIGN_TAG.fullmatch(authority.campaign_tag)
    generation_match = ROOTFS_GENERATION.fullmatch(
        authority.rootfs_wasm32
    )
    assert campaign_match is not None
    assert generation_match is not None
    zero_identities = {
        "campaign": set(campaign_match.group(1)) == {"0"},
        "kandelo": set(authority.kandelo_commit) == {"0"},
        "rootfs": set(generation_match.group(1)) == {"0"},
        "source": set(authority.source_tap_commit) == {"0"},
        "workflow": set(authority.reusable_workflow_commit) == {"0"},
    }
    expected_zero_identities = {
        "inert": set(zero_identities),
        # WHY: an armed authority places the final workflow bytes on
        # protected main before a campaign is sealed. Dispatch stays disabled
        # until a later data-only activation fills the remaining identities.
        "armed": {"campaign", "rootfs", "source"},
        "active": set(),
    }[authority.state]
    actual_zero_identities = {
        name for name, is_zero in zero_identities.items() if is_zero
    }
    if actual_zero_identities != expected_zero_identities:
        fail(
            "invalid-contract",
            f"campaign {authority.state} authority mixes identity states",
            2,
        )
    if require_active and authority.state != "active":
        fail(
            "campaign-authority-inert",
            "campaign caller authority is not active",
            INERT_EXIT,
        )
    return authority


def parse_arches(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        fail(
            "invalid-task-selection",
            "client_payload.arches must be an array",
            2,
        )
    arches = tuple(value)
    if arches not in (("wasm32",), ("wasm64",)):
        fail(
            "invalid-task-selection",
            "architectures must select exactly one of wasm32 or wasm64",
            2,
        )
    return arches


def parse_dependency_tags(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) > MAX_DEPENDENCIES:
        fail(
            "invalid-task-selection",
            "dependency_handoffs must be a bounded array",
            2,
        )
    output: list[tuple[str, str]] = []
    prior = ""
    for index, item in enumerate(value):
        item = exact_keys(
            item,
            {"formula", "tag"},
            f"dependency_handoffs[{index}]",
        )
        formula = require_string(
            item["formula"],
            f"dependency_handoffs[{index}].formula",
            FORMULA,
        )
        tag = require_string(
            item["tag"],
            f"dependency_handoffs[{index}].tag",
            HANDOFF_TAG,
        )
        match = HANDOFF_TAG.fullmatch(tag)
        assert match is not None
        if set(match.group(1)) == {"0"} or formula <= prior:
            fail(
                "invalid-task-selection",
                "dependency handoffs must be unique, sorted, and non-inert",
                2,
            )
        prior = formula
        output.append((formula, tag))
    return tuple(output)


def load_task_request(event_path: pathlib.Path) -> TaskRequest:
    value, _payload = load_json_bytes(event_path, "GitHub event")
    if not isinstance(value, dict):
        fail("invalid-task-selection", "GitHub event must be an object", 2)
    action = value.get("action")
    if action != EVENT_TYPE:
        fail(
            "invalid-task-selection",
            "GitHub event action differs from the campaign event",
            2,
        )
    payload = exact_keys(
        value.get("client_payload"),
        {"arches", "dependency_handoffs", "formula"},
        "GitHub client_payload",
    )
    return TaskRequest(
        formula=require_string(
            payload["formula"],
            "client_payload.formula",
            FORMULA,
        ),
        arches=parse_arches(payload["arches"]),
        dependency_tags=parse_dependency_tags(
            payload["dependency_handoffs"]
        ),
    )


def anonymous_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in TOKEN_ENV:
        environment.pop(name, None)
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def internal_release_environment() -> dict[str, str]:
    environment = anonymous_environment()
    token = os.environ.get("GH_TOKEN")
    if not token:
        fail(
            "credential-unavailable",
            "internal GitHub release reads require GH_TOKEN",
        )
    # WHY: controller reads need GitHub API quota, not package publication or
    # Actions runtime authority. Start from the anonymous environment and add
    # back only the repository-scoped token instead of inheriting every secret.
    environment["GH_TOKEN"] = token
    return environment


def redact_environment_secrets(payload: bytes) -> bytes:
    redacted = payload
    for name in TOKEN_ENV:
        value = os.environ.get(name)
        if value:
            redacted = redacted.replace(
                value.encode("utf-8"),
                b"<redacted>",
            )
    return redacted


def run_command(
    arguments: Sequence[str],
    *,
    cwd: pathlib.Path,
    inherit_github_token: bool = False,
    timeout: int = COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess[bytes]:
    environment = (
        internal_release_environment()
        if inherit_github_token
        else anonymous_environment()
    )
    try:
        result = subprocess.run(
            list(arguments),
            cwd=cwd,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        fail(
            "command-failed",
            f"reviewed command could not run: {arguments[0]}: {error}",
        )
    if (
        len(result.stdout) > MAX_COMMAND_OUTPUT
        or len(result.stderr) > MAX_COMMAND_OUTPUT
    ):
        fail(
            "command-failed",
            f"reviewed command exceeded bounded output: {arguments[0]}",
        )
    if result.returncode != 0:
        detail = redact_environment_secrets(result.stderr).decode(
            "utf-8",
            errors="replace",
        )[:16_384].replace("\r", "\\r").replace("\n", "\\n")
        fail(
            "command-failed",
            f"reviewed command failed: {arguments[0]}: {detail}",
        )
    return result


def git_output(root: pathlib.Path, *arguments: str) -> str:
    result = run_command(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(root),
            *arguments,
        ],
        cwd=root,
    )
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        fail("invalid-checkout", f"Git output is not UTF-8: {error}")


def require_exact_checkout(
    root: pathlib.Path,
    expected_commit: str,
    label: str,
) -> pathlib.Path:
    try:
        metadata = root.lstat()
    except OSError as error:
        fail("invalid-checkout", f"{label} is unavailable: {error}")
    if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
        fail(
            "invalid-checkout",
            f"{label} must be a real non-symlink directory",
        )
    root = root.resolve()
    if (
        git_output(root, "rev-parse", "--show-toplevel")
        != str(root)
        or git_output(root, "rev-parse", "HEAD") != expected_commit
        or git_output(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
    ):
        fail(
            "invalid-checkout",
            f"{label} differs from its exact clean commit",
        )
    flags = run_command(
        ["git", "-C", str(root), "ls-files", "-v", "-z"],
        cwd=root,
    ).stdout
    if any(entry and not entry.startswith(b"H ") for entry in flags.split(b"\0")):
        fail(
            "invalid-checkout",
            f"{label} uses unsafe Git index flags",
        )
    return root


def require_matching_workflow_tree(
    caller_root: pathlib.Path,
    source_tap_root: pathlib.Path,
) -> None:
    """Require the sealed source and active caller to share workflow bytes."""
    path = ".github/workflows"
    caller_tree = git_output(caller_root, "rev-parse", f"HEAD:{path}")
    source_tree = git_output(
        source_tap_root,
        "rev-parse",
        f"HEAD:{path}",
    )
    if (
        re.fullmatch(r"[0-9a-f]{40}", caller_tree) is None
        or re.fullmatch(r"[0-9a-f]{40}", source_tree) is None
        or caller_tree != source_tree
    ):
        # WHY: GitHub denies GITHUB_TOKEN release creation when the release
        # target has historical workflow bytes. Reject the campaign before it
        # builds or reuses a bottle that its handoff job cannot seal.
        fail(
            "invalid-checkout",
            "active workflow tree differs from the sealed campaign source",
        )


def target_source_identity(authority: Authority) -> tuple[str, ...]:
    return (
        authority.target_manifest_path,
        authority.target_manifest_sha256,
        authority.target_source_root,
        authority.target_source_tree,
        authority.target_tree,
    )


def require_target_source_checkout(
    authority: Authority,
    source_tap_root: pathlib.Path,
) -> pathlib.Path:
    source_authority = load_authority(
        source_tap_root / "Kandelo/prefix-campaign-authority.json",
        require_active=False,
    )
    if target_source_identity(source_authority) != target_source_identity(
        authority
    ):
        fail(
            "invalid-checkout",
            "source tap differs from the protected target-source authority",
        )
    verifier = source_tap_root / "scripts/prefix-campaign-source.py"
    run_command(
        [
            "python3",
            str(verifier),
            "verify",
            "--root",
            str(source_tap_root),
            "--authority",
            str(
                source_tap_root
                / "Kandelo/prefix-campaign-authority.json"
            ),
            "--manifest",
            str(source_tap_root / authority.target_manifest_path),
        ],
        cwd=source_tap_root,
    )
    return source_tap_root / authority.target_source_root


def materialize_target_source(
    authority: Authority,
    source_tap_root: pathlib.Path,
    output: pathlib.Path,
) -> pathlib.Path:
    verifier = source_tap_root / "scripts/prefix-campaign-source.py"
    run_command(
        [
            "python3",
            str(verifier),
            "materialize",
            "--root",
            str(source_tap_root),
            "--authority",
            str(
                source_tap_root
                / "Kandelo/prefix-campaign-authority.json"
            ),
            "--manifest",
            str(source_tap_root / authority.target_manifest_path),
            "--out",
            str(output),
        ],
        cwd=source_tap_root,
    )
    return output


def fetch_campaign(
    authority: Authority,
    kandelo_root: pathlib.Path,
    output: pathlib.Path,
    *,
    authenticated: bool,
) -> pathlib.Path:
    output.mkdir(mode=0o700)
    campaign = output / CAMPAIGN_ASSET
    receipt = output / "campaign-receipt.json"
    tool = (
        kandelo_root
        / "scripts/homebrew-prefix-campaign-executor.py"
    )
    run_command(
        [
            "python3",
            str(tool),
            "fetch-campaign-release",
            "--repository",
            authority.campaign_repository,
            "--tag",
            authority.campaign_tag,
            "--out",
            str(campaign),
            "--receipt-out",
            str(receipt),
        ],
        cwd=kandelo_root,
        inherit_github_token=authenticated,
    )
    return campaign


def formula_index(campaign: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = campaign.get("formulae")
    if (
        not isinstance(values, list)
        or not values
        or len(values) > MAX_FORMULAE
    ):
        fail(
            "invalid-campaign",
            "campaign formulae must be a bounded non-empty array",
        )
    result: dict[str, dict[str, Any]] = {}
    prior = ""
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            fail(
                "invalid-campaign",
                f"campaign formulae[{index}] must be an object",
            )
        name = require_string(
            value.get("name"),
            f"campaign formulae[{index}].name",
            FORMULA,
        )
        if name <= prior:
            fail(
                "invalid-campaign",
                "campaign Formulae must be unique and sorted",
            )
        prior = name
        result[name] = value
    return result


def direct_dependencies(
    campaign: dict[str, Any],
    formula: dict[str, Any],
) -> tuple[str, ...]:
    authority = campaign["authority"]
    prefix = f"{authority['tap_name']}/"
    values = formula.get("dependencies")
    if not isinstance(values, list):
        fail(
            "invalid-campaign",
            f"{formula.get('name')} dependencies must be an array",
        )
    result: list[str] = []
    prior = ""
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            fail(
                "invalid-campaign",
                f"dependency #{index} must be an object",
            )
        full_name = value.get("full_name")
        if (
            not isinstance(full_name, str)
            or not full_name.startswith(prefix)
        ):
            fail(
                "invalid-campaign",
                "campaign dependency is not a same-tap identity",
            )
        name = full_name.removeprefix(prefix)
        if FORMULA.fullmatch(name) is None or name <= prior:
            fail(
                "invalid-campaign",
                "campaign dependencies must be unique and sorted",
            )
        prior = name
        result.append(name)
    return tuple(result)


def dependency_closure(
    campaign: dict[str, Any],
    formula: dict[str, Any],
) -> tuple[str, ...]:
    formulae = formula_index(campaign)
    reached: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            fail(
                "invalid-campaign",
                f"campaign dependency graph cycles at {name}",
            )
        if name in reached:
            return
        if name not in formulae:
            fail(
                "invalid-campaign",
                f"campaign dependency {name} is outside the campaign",
            )
        visiting.add(name)
        for dependency in direct_dependencies(campaign, formulae[name]):
            visit(dependency)
        visiting.remove(name)
        reached.add(name)

    for dependency in direct_dependencies(campaign, formula):
        visit(dependency)
    return tuple(sorted(reached))


def destination_admission_kind(formula: dict[str, Any]) -> str:
    name = formula.get("name", "Formula")
    destination = exact_keys(
        formula.get("destination"),
        {"admission", "bottle_rebuild", "reference", "remote"},
        f"{name} destination",
    )
    require_integer(
        destination["bottle_rebuild"],
        f"{name} destination bottle rebuild",
        0,
        2**31 - 1,
    )
    require_string(
        destination["reference"],
        f"{name} destination reference",
    )
    require_string(
        destination["remote"],
        f"{name} destination remote",
    )
    admission = exact_keys(
        destination.get("admission"),
        {"kind", "method", "probe", "schema"},
        f"{name} destination admission",
    )
    probe = exact_keys(
        admission["probe"],
        {"digest", "kind", "schema", "status"},
        f"{name} destination admission probe",
    )
    kind = admission["kind"]
    expected_status = {
        "anonymous-absence": "missing",
        "first-package-namespace-bootstrap-required": "auth-required",
    }.get(kind)
    if (
        admission["schema"] != 1
        or admission["method"] != "anonymous-oras-manifest-probe"
        or expected_status is None
        or probe["schema"] != 1
        or probe["kind"] != "manifest"
        or probe["status"] != expected_status
        or probe["digest"] is not None
    ):
        fail(
            "invalid-campaign",
            f"{name} destination admission is invalid",
        )
    return kind


def validate_campaign(
    path: pathlib.Path,
    authority: Authority,
    request: TaskRequest,
) -> TaskPlan:
    campaign, payload = load_json_bytes(
        path,
        "campaign manifest",
        canonical=True,
    )
    if not isinstance(campaign, dict):
        fail("invalid-campaign", "campaign manifest must be an object")
    if (
        campaign.get("schema") != 2
        or campaign.get("kind")
        != "kandelo-homebrew-guest-prefix-campaign"
    ):
        fail(
            "invalid-campaign",
            "campaign manifest has an unsupported schema",
        )
    tag_match = CAMPAIGN_TAG.fullmatch(authority.campaign_tag)
    assert tag_match is not None
    if hashlib.sha256(payload).hexdigest() != tag_match.group(1):
        fail(
            "invalid-campaign",
            "campaign bytes differ from their release tag",
        )
    campaign_authority = campaign.get("authority")
    if not isinstance(campaign_authority, dict):
        fail("invalid-campaign", "campaign lacks authority")
    if (
        campaign_authority.get("kandelo_commit")
        != authority.kandelo_commit
        or campaign_authority.get("source_tap_commit")
        != authority.source_tap_commit
        or str(campaign_authority.get("tap_repository", "")).lower()
        != authority.source_tap_repository
        or str(campaign_authority.get("tap_name", "")).lower()
        != authority.source_tap_name
    ):
        fail(
            "invalid-campaign",
            "campaign differs from the protected caller authority",
        )
    old_tap_commit = require_string(
        campaign_authority.get("old_tap_commit"),
        "campaign old tap commit",
        SHA,
    )
    formulae = formula_index(campaign)
    formula = formulae.get(request.formula)
    if formula is None:
        fail(
            "invalid-task-selection",
            f"campaign does not contain Formula {request.formula}",
            2,
        )
    admission_kind = destination_admission_kind(formula)
    variants = formula.get("variants")
    if not isinstance(variants, list) or not variants:
        fail(
            "invalid-campaign",
            f"{request.formula} has no campaign variants",
        )
    declared_arches: list[str] = []
    selected_kind: str | None = None
    for variant in variants:
        if not isinstance(variant, dict):
            fail(
                "invalid-campaign",
                f"{request.formula} variant is not an object",
            )
        arch = variant.get("arch")
        disposition = variant.get("disposition")
        if (
            arch not in ("wasm32", "wasm64")
            or not isinstance(disposition, dict)
            or disposition.get("kind")
            not in (
                "byte-clean-reuse-candidate",
                "required-build",
                "required-rebuild",
            )
        ):
            fail(
                "invalid-campaign",
                f"{request.formula} variant identity is invalid",
            )
        declared_arches.append(arch)
        if arch == request.arches[0]:
            selected_kind = disposition["kind"]
    if tuple(declared_arches) != tuple(sorted(set(declared_arches))):
        fail(
            "invalid-campaign",
            f"{request.formula} variants must be unique and sorted",
        )
    if admission_kind == "first-package-namespace-bootstrap-required":
        if formula.get("source_kind") != "reviewed-new-entrant":
            fail(
                "invalid-campaign",
                "package namespace bootstrap requires a reviewed new entrant",
            )
        for variant in variants:
            if (
                variant.get("selected_by")
                != "reviewed-campaign-input"
                or not isinstance(variant.get("build_input"), dict)
                or not isinstance(variant.get("disposition"), dict)
                or variant["disposition"].get("kind")
                != "required-build"
                or variant["disposition"].get("reasons")
                != ["new-campaign-entrant"]
                or "old_record" in variant
            ):
                # WHY: an anonymous auth challenge is ambiguous: it can mean
                # either an absent package or an existing private package.
                # Only the separately reviewed new-entrant contract may defer
                # that distinction to the credentialed canary's whole-package
                # absence check immediately before its one allowed write.
                fail(
                    "invalid-campaign",
                    "package namespace bootstrap Formula is not an exact "
                    "reviewed new entrant",
                )
    if selected_kind is None:
        fail(
            "invalid-task-selection",
            "requested architecture is not declared by the campaign task",
            2,
        )
    # WHY: sibling architectures are independent publication units. One may
    # already have reusable bytes while the other still needs a build, so the
    # selected architecture alone determines this task's disposition.
    if selected_kind == "byte-clean-reuse-candidate":
        disposition = "reuse"
    else:
        assert selected_kind in ("required-build", "required-rebuild")
        disposition = "build"
    if (
        admission_kind
        == "first-package-namespace-bootstrap-required"
        and disposition != "build"
    ):
        # WHY: the namespace bootstrap is a one-time child write for a new
        # package. Reuse can only select an already public package and must
        # therefore remain on the ordinary anonymous-absence path.
        fail(
            "invalid-campaign",
            "package namespace bootstrap requires a build task",
        )
    expected_dependencies = dependency_closure(campaign, formula)
    actual_dependencies = tuple(
        name for name, _tag in request.dependency_tags
    )
    if actual_dependencies != expected_dependencies:
        fail(
            "invalid-task-selection",
            "dependency handoff tags differ from the exact transitive closure",
            2,
        )
    if disposition == "reuse":
        # Reuse reads and verifies already-built public bytes. It does
        # not execute a Formula and therefore needs no package runtime.
        generation_kind = "none"
    elif request.arches == ("wasm32",):
        generation_kind = "rootfs-wasm32"
    else:
        # WHY: the previous broad browser-input generation cannot be
        # made for this campaign. It includes browser images outside the
        # package build runtime, and some of those images have no wasm64
        # closure. Fail here instead of dispatching a task whose inputs
        # cannot exist.
        fail(
            "package-generation-unavailable",
            "new wasm64 builds need a dedicated package runtime generation",
            UNAVAILABLE_EXIT,
        )
    return TaskPlan(
        request=request,
        admission_kind=admission_kind,
        disposition=disposition,
        generation_kind=generation_kind,
        old_tap_commit=old_tap_commit,
        campaign_path=path,
        campaign_payload=payload,
        campaign=campaign,
        formula=formula,
    )


def build_plan_document(
    authority: Authority,
    plan: TaskPlan,
) -> dict[str, Any]:
    return {
        "admission": {
            "kind": plan.admission_kind,
            "schema": 1,
        },
        "arches": list(plan.request.arches),
        "authority_sha256": authority.sha256,
        "campaign": {
            "bytes": len(plan.campaign_payload),
            "sha256": hashlib.sha256(
                plan.campaign_payload
            ).hexdigest(),
            "tag": authority.campaign_tag,
        },
        "dependencies": plan.request.dependency_document(),
        "disposition": plan.disposition,
        "formula": plan.request.formula,
        "generation_kind": plan.generation_kind,
        "kind": "kandelo-homebrew-prefix-campaign-task-plan",
        "schema": 2,
        "old_tap_commit": plan.old_tap_commit,
        "source_tap_commit": authority.source_tap_commit,
        "target_source": {
            "manifest_path": authority.target_manifest_path,
            "manifest_sha256": authority.target_manifest_sha256,
            "source_root": authority.target_source_root,
            "source_tree_git_oid": authority.target_source_tree,
            "target_tree_git_oid": authority.target_tree,
        },
    }


def append_github_output(path: pathlib.Path, values: Mapping[str, str]) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        fail(
            "invalid-output",
            f"GitHub output file is unavailable: {error}",
        )
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        fail(
            "invalid-output",
            "GitHub output path must be a regular file",
        )
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            if (
                not re.fullmatch(r"[a-z][a-z0-9-]*", key)
                or "\n" in value
                or "\r" in value
            ):
                fail(
                    "invalid-output",
                    f"unsafe GitHub output {key!r}",
                )
            handle.write(f"{key}={value}\n")


def prepare_task(
    *,
    authority_path: pathlib.Path,
    kandelo_root: pathlib.Path,
    source_tap_root: pathlib.Path,
    event_path: pathlib.Path,
    working: pathlib.Path,
    authenticated_release_reads: bool,
) -> tuple[Authority, TaskPlan, pathlib.Path, pathlib.Path]:
    authority = load_authority(authority_path, require_active=True)
    request = load_task_request(event_path)
    kandelo_root = require_exact_checkout(
        kandelo_root,
        authority.kandelo_commit,
        "Kandelo checkout",
    )
    source_tap_root = require_exact_checkout(
        source_tap_root,
        authority.source_tap_commit,
        "source tap checkout",
    )
    require_matching_workflow_tree(ROOT, source_tap_root)
    require_target_source_checkout(authority, source_tap_root)
    campaign_path = fetch_campaign(
        authority,
        kandelo_root,
        working / "campaign",
        authenticated=authenticated_release_reads,
    )
    plan = validate_campaign(campaign_path, authority, request)
    # WHY: later commands run with a checkout as their working directory. A
    # relative input such as `kandelo` would otherwise be interpreted again as
    # `kandelo/kandelo` after validation. Return the exact canonical roots that
    # require_exact_checkout already validated instead of reusing raw inputs.
    return authority, plan, kandelo_root, source_tap_root


def dependency_order(
    plan: TaskPlan,
) -> tuple[str, ...]:
    formulae = formula_index(plan.campaign)
    wanted = set(
        name for name, _tag in plan.request.dependency_tags
    )
    output: list[str] = []
    reached: set[str] = set()

    def visit(name: str) -> None:
        if name in reached:
            return
        for dependency in direct_dependencies(
            plan.campaign,
            formulae[name],
        ):
            if dependency in wanted:
                visit(dependency)
        reached.add(name)
        output.append(name)

    for name in sorted(wanted):
        visit(name)
    return tuple(output)


def dependency_roots_for(
    plan: TaskPlan,
    name: str,
    materialized: Mapping[str, pathlib.Path],
) -> list[pathlib.Path]:
    formulae = formula_index(plan.campaign)
    closure = set(dependency_closure(plan.campaign, formulae[name]))
    return [
        materialized[dependency]
        for dependency in sorted(closure)
    ]


def fetch_dependency_handoffs(
    *,
    kandelo_root: pathlib.Path,
    plan: TaskPlan,
    root: pathlib.Path,
    authenticated: bool,
) -> dict[str, pathlib.Path]:
    if root.exists() or root.is_symlink():
        fail(
            "invalid-output",
            "dependency readback root already exists",
            2,
        )
    root.mkdir(mode=0o700)
    executor = (
        kandelo_root
        / "scripts/homebrew-prefix-campaign-executor.py"
    )
    tags = dict(plan.request.dependency_tags)
    materialized: dict[str, pathlib.Path] = {}
    receipts = root / "receipts"
    receipts.mkdir(mode=0o700)
    handoffs = root / "handoffs"
    handoffs.mkdir(mode=0o700)
    for name in dependency_order(plan):
        output = handoffs / name
        arguments = [
            "python3",
            str(executor),
            "fetch-release",
            "--campaign",
            str(plan.campaign_path),
            "--tag",
            tags[name],
            "--out",
            str(output),
            "--receipt-out",
            str(receipts / f"{name}.json"),
        ]
        for dependency_root in dependency_roots_for(
            plan,
            name,
            materialized,
        ):
            arguments.extend(
                ["--dependency-handoff", str(dependency_root)]
            )
        run_command(
            arguments,
            cwd=kandelo_root,
            inherit_github_token=authenticated,
        )
        materialized[name] = output
    return materialized


def require_publication_roots(
    root: pathlib.Path,
    arches: Iterable[str],
) -> list[tuple[str, pathlib.Path]]:
    try:
        metadata = root.lstat()
    except OSError as error:
        fail(
            "invalid-publication",
            f"publication root is unavailable: {error}",
        )
    if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
        fail(
            "invalid-publication",
            "publication root must be a real directory",
        )
    output: list[tuple[str, pathlib.Path]] = []
    expected = set(arches)
    actual = {path.name for path in root.iterdir()}
    if actual != expected:
        fail(
            "invalid-publication",
            "publication directories differ from the exact task arches",
        )
    for arch in arches:
        path = root / arch
        if path.is_symlink() or not path.is_dir():
            fail(
                "invalid-publication",
                f"{arch} publication is not a real directory",
            )
        output.append((arch, path.resolve()))
    return output


def require_reuse_oci_tree(root: pathlib.Path) -> pathlib.Path:
    try:
        root_metadata = root.lstat()
    except OSError as error:
        fail(
            "invalid-release",
            f"cannot inspect prepared reuse OCI child: {error}",
        )
    if not stat.S_ISDIR(root_metadata.st_mode) or root.is_symlink():
        fail(
            "invalid-release",
            "prepared reuse OCI child must be a real directory",
        )
    layout = root / "layout"
    try:
        root_names = {path.name for path in root.iterdir()}
        layout_metadata = layout.lstat()
        layout_names = {path.name for path in layout.iterdir()}
        blobs_metadata = (layout / "blobs").lstat()
        blobs_names = {
            path.name for path in (layout / "blobs").iterdir()
        }
        sha_root = layout / "blobs/sha256"
        sha_metadata = sha_root.lstat()
        blob_paths = list(sha_root.iterdir())
    except OSError as error:
        fail(
            "invalid-release",
            f"cannot walk prepared reuse OCI child: {error}",
        )
    if (
        root_names != {"layout", "receipt.json"}
        or layout_names != {"blobs", "index.json", "oci-layout"}
        or blobs_names != {"sha256"}
        or not stat.S_ISDIR(layout_metadata.st_mode)
        or not stat.S_ISDIR(blobs_metadata.st_mode)
        or not stat.S_ISDIR(sha_metadata.st_mode)
        or layout.is_symlink()
        or (layout / "blobs").is_symlink()
        or sha_root.is_symlink()
        or len(blob_paths) != 3
        or any(SHA256.fullmatch(path.name) is None for path in blob_paths)
    ):
        fail(
            "invalid-release",
            "prepared reuse OCI child has an unexpected layout shape",
        )
    files = [
        root / "receipt.json",
        layout / "index.json",
        layout / "oci-layout",
        *blob_paths,
    ]
    total_bytes = 0
    for path in files:
        try:
            metadata = path.lstat()
        except OSError as error:
            fail(
                "invalid-release",
                f"cannot inspect prepared reuse OCI file: {error}",
            )
        limit = (
            MAX_HANDOFF_ASSET_BYTES
            if path.parent == sha_root
            else MAX_JSON_BYTES
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_nlink != 1
            or metadata.st_size < 1
            or metadata.st_size > limit
        ):
            fail(
                "invalid-release",
                "prepared reuse OCI child contains an unsafe file",
            )
        total_bytes += metadata.st_size
        if total_bytes > MAX_HANDOFF_TOTAL_BYTES:
            fail(
                "invalid-release",
                "prepared reuse OCI child exceeds its total byte limit",
            )
    return root


def prepare_handoff_release(
    *,
    authority: Authority,
    plan: TaskPlan,
    kandelo_root: pathlib.Path,
    dependencies: Mapping[str, pathlib.Path],
    handoff: pathlib.Path,
    working: pathlib.Path,
    output: pathlib.Path,
    github_output: pathlib.Path | None,
    reuse_oci: pathlib.Path | None = None,
) -> dict[str, Any]:
    executor = (
        kandelo_root
        / "scripts/homebrew-prefix-campaign-executor.py"
    )
    prepared = working / "prepared-release"
    prepare = [
        "python3",
        str(executor),
        "prepare-release",
        "--campaign",
        str(plan.campaign_path),
        "--handoff",
        str(handoff),
        "--out",
        str(prepared),
    ]
    for name in sorted(dependencies):
        prepare.extend(
            ["--dependency-handoff", str(dependencies[name])]
        )
    run_command(prepare, cwd=kandelo_root)
    manifest, _payload = load_json_bytes(
        prepared / "release-manifest.json",
        "prepared release manifest",
        canonical=True,
    )
    if (
        not isinstance(manifest, dict)
        or manifest.get("repository")
        != authority.source_tap_repository
        or manifest.get("target_commitish")
        != authority.source_tap_commit
    ):
        fail(
            "invalid-release",
            "prepared release differs from caller authority",
        )
    tag = require_string(
        manifest.get("tag"),
        "prepared handoff release tag",
        HANDOFF_TAG,
    )
    if reuse_oci is not None:
        reuse_oci = require_reuse_oci_tree(reuse_oci)
        reuse_destination = prepared / "reuse-oci"
        if reuse_destination.exists() or reuse_destination.is_symlink():
            fail(
                "invalid-release",
                "prepared release already contains a reuse OCI child",
            )
        # WHY: keep the immutable child and Formula handoff in one atomic
        # controller output. The workflow must publish and publicly read back
        # this exact child before it is allowed to seal the handoff release.
        os.rename(reuse_oci, reuse_destination)
    summary = {
        "arches": list(plan.request.arches),
        "authority_sha256": authority.sha256,
        "dependencies": plan.request.dependency_document(),
        "disposition": plan.disposition,
        "formula": plan.request.formula,
        "kind": "kandelo-homebrew-prefix-release-preparation",
        "release_tag": tag,
        "schema": 2,
        "target_commitish": authority.source_tap_commit,
    }
    summary_path = output.parent / "controller-summary.json"
    if summary_path.exists() or summary_path.is_symlink():
        fail(
            "invalid-output",
            "controller summary output already exists",
            2,
        )
    os.rename(prepared, output)
    summary_path.write_bytes(pretty_json(summary))
    if github_output is not None:
        append_github_output(
            github_output,
            {"handoff-tag": tag},
        )
    return summary


def prepare_build_release(
    *,
    authority_path: pathlib.Path,
    kandelo_root: pathlib.Path,
    source_tap_root: pathlib.Path,
    event_path: pathlib.Path,
    publications_root: pathlib.Path,
    output: pathlib.Path,
    github_output: pathlib.Path | None,
) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        fail(
            "invalid-output",
            "build release output must not already exist",
            2,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    working = pathlib.Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.",
            dir=output.parent,
        )
    )
    try:
        authority, plan, kandelo_root, source_tap_root = prepare_task(
            authority_path=authority_path,
            kandelo_root=kandelo_root,
            source_tap_root=source_tap_root,
            event_path=event_path,
            working=working,
            authenticated_release_reads=True,
        )
        if plan.disposition != "build":
            fail(
                "invalid-task-selection",
                "build preparation requires a campaign build task",
                2,
            )
        target_source_root = materialize_target_source(
            authority,
            source_tap_root.resolve(),
            working / "target-source",
        )
        dependencies = fetch_dependency_handoffs(
            kandelo_root=kandelo_root,
            plan=plan,
            root=working / "dependencies",
            authenticated=True,
        )
        publications = require_publication_roots(
            publications_root,
            plan.request.arches,
        )
        executor = (
            kandelo_root
            / "scripts/homebrew-prefix-campaign-executor.py"
        )
        handoff = working / "formula-handoff"
        derive = [
            "python3",
            str(executor),
            "derive-build",
            "--campaign",
            str(plan.campaign_path),
            "--source-tap-root",
            str(target_source_root),
            "--formula",
            plan.request.formula,
            "--out",
            str(handoff),
        ]
        for arch, publication in publications:
            derive.extend(
                ["--publication", f"{arch}={publication}"]
            )
        for name in sorted(dependencies):
            derive.extend(
                ["--dependency-handoff", str(dependencies[name])]
            )
        run_command(derive, cwd=kandelo_root)

        return prepare_handoff_release(
            authority=authority,
            plan=plan,
            kandelo_root=kandelo_root,
            dependencies=dependencies,
            handoff=handoff,
            working=working,
            output=output,
            github_output=github_output,
        )
    finally:
        shutil.rmtree(working, ignore_errors=True)


def prepare_reuse_release(
    *,
    authority_path: pathlib.Path,
    kandelo_root: pathlib.Path,
    source_tap_root: pathlib.Path,
    old_tap_root: pathlib.Path,
    event_path: pathlib.Path,
    output: pathlib.Path,
    github_output: pathlib.Path | None,
) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        fail(
            "invalid-output",
            "reuse release output must not already exist",
            2,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    working = pathlib.Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.",
            dir=output.parent,
        )
    )
    try:
        authority, plan, kandelo_root, source_tap_root = prepare_task(
            authority_path=authority_path,
            kandelo_root=kandelo_root,
            source_tap_root=source_tap_root,
            event_path=event_path,
            working=working,
            authenticated_release_reads=True,
        )
        if plan.disposition != "reuse":
            fail(
                "invalid-task-selection",
                "reuse preparation requires a campaign reuse task",
                2,
            )
        old_tap_root = require_exact_checkout(
            old_tap_root,
            plan.old_tap_commit,
            "historical tap checkout",
        )
        target_source_root = materialize_target_source(
            authority,
            source_tap_root.resolve(),
            working / "target-source",
        )
        dependencies = fetch_dependency_handoffs(
            kandelo_root=kandelo_root,
            plan=plan,
            root=working / "dependencies",
            authenticated=True,
        )
        executor = (
            kandelo_root
            / "scripts/homebrew-prefix-campaign-executor.py"
        )
        handoff = working / "formula-handoff"
        derive = [
            "python3",
            str(executor),
            "derive-reuse",
            "--campaign",
            str(plan.campaign_path),
            "--source-tap-root",
            str(target_source_root),
            "--old-tap-root",
            str(old_tap_root),
            "--formula",
            plan.request.formula,
            "--arch",
            plan.request.arches[0],
            "--out",
            str(handoff),
        ]
        for name in sorted(dependencies):
            derive.extend(
                ["--dependency-handoff", str(dependencies[name])]
            )
        # WHY: the tap only selects inputs and publishes the result.
        # Kandelo's reviewed executor re-fetches the public bottle and
        # owns every reuse provenance, layout, dependency, and archive
        # validation rule.
        run_command(derive, cwd=kandelo_root)
        reuse_oci = working / "reuse-oci"
        compose = [
            "python3",
            str(executor),
            "compose-reuse-child",
            "--campaign",
            str(plan.campaign_path),
            "--source-tap-root",
            str(target_source_root),
            "--handoff",
            str(handoff),
            "--formula",
            plan.request.formula,
            "--arch",
            plan.request.arches[0],
            "--out",
            str(reuse_oci),
        ]
        run_command(compose, cwd=kandelo_root)
        run_command(
            [
                "python3",
                str(kandelo_root / "scripts/homebrew-oci-layout.py"),
                "validate-child",
                "--layout",
                str(reuse_oci / "layout"),
                "--receipt",
                str(reuse_oci / "receipt.json"),
            ],
            cwd=kandelo_root,
        )
        return prepare_handoff_release(
            authority=authority,
            plan=plan,
            kandelo_root=kandelo_root,
            dependencies=dependencies,
            handoff=handoff,
            working=working,
            output=output,
            github_output=github_output,
            reuse_oci=reuse_oci,
        )
    finally:
        shutil.rmtree(working, ignore_errors=True)


def validate_readback_handoff(
    path: pathlib.Path,
    *,
    authority: Authority,
    plan: TaskPlan,
    tag: str,
) -> dict[str, Any]:
    manifest, payload = load_json_bytes(
        path,
        "read-back Formula handoff",
        canonical=True,
    )
    manifest = exact_keys(
        manifest,
        {
            "campaign",
            "dependency_handoffs",
            "formula",
            "kind",
            "publications",
            "schema",
            "source",
        },
        "read-back Formula handoff",
    )
    tag_match = HANDOFF_TAG.fullmatch(tag)
    assert tag_match is not None
    if (
        manifest["schema"] != 2
        or manifest["kind"] != HANDOFF_KIND
        or hashlib.sha256(payload).hexdigest() != tag_match.group(1)
    ):
        fail(
            "invalid-release",
            "anonymous handoff readback has the wrong contract or tag",
        )

    campaign = exact_keys(
        manifest["campaign"],
        {"sha256"},
        "read-back handoff campaign",
    )
    if campaign["sha256"] != hashlib.sha256(
        plan.campaign_payload
    ).hexdigest():
        fail(
            "invalid-release",
            "anonymous handoff readback belongs to another campaign",
        )

    formula_source = plan.formula.get("formula_source")
    destination = plan.formula.get("destination")
    dependencies = plan.formula.get("dependencies")
    if (
        not isinstance(formula_source, dict)
        or not isinstance(destination, dict)
        or not isinstance(dependencies, list)
    ):
        fail(
            "invalid-campaign",
            "campaign Formula lacks exact handoff evidence",
        )
    expected_formula = {
        "bottle_rebuild": require_integer(
            destination.get("bottle_rebuild"),
            "campaign Formula bottle rebuild",
            0,
            2**31 - 1,
        ),
        "dependencies": dependencies,
        "formula_sha256": require_string(
            formula_source.get("sha256"),
            "campaign Formula SHA-256",
            SHA256,
        ),
        "name": plan.request.formula,
        "version": require_string(
            plan.formula.get("version"),
            "campaign Formula version",
        ),
    }
    if manifest["formula"] != expected_formula:
        fail(
            "invalid-release",
            "anonymous handoff readback has the wrong Formula identity",
        )

    expected_dependencies = [
        {
            "formula": name,
            "manifest_sha256": dependency_tag.removeprefix(
                "homebrew-prefix-handoff-sha256-"
            ),
            "tag": dependency_tag,
        }
        for name, dependency_tag in plan.request.dependency_tags
    ]
    if manifest["dependency_handoffs"] != expected_dependencies:
        fail(
            "invalid-release",
            "anonymous handoff readback has the wrong dependencies",
        )

    expected_source = {
        "kandelo_commit": authority.kandelo_commit,
        "source_tap_commit": authority.source_tap_commit,
        "target_tree_git_oid": authority.target_tree,
        "tap_name": authority.source_tap_name,
        "tap_repository": authority.source_tap_repository,
    }
    if manifest["source"] != expected_source:
        fail(
            "invalid-release",
            "anonymous handoff readback has the wrong source authority",
        )

    publications = manifest["publications"]
    if (
        not isinstance(publications, list)
        or len(publications) != len(plan.request.arches)
    ):
        fail(
            "invalid-release",
            "anonymous handoff readback has the wrong publications",
        )
    expected_files = (
        BUILD_HANDOFF_PUBLICATION_FILES
        if plan.disposition == "build"
        else REUSE_HANDOFF_PUBLICATION_FILES
    )
    total_bytes = len(payload)
    for index, arch in enumerate(plan.request.arches):
        publication = exact_keys(
            publications[index],
            {"arch", "files", "kind"},
            f"read-back {arch} publication",
        )
        files = publication["files"]
        if (
            publication["arch"] != arch
            or publication["kind"] != plan.disposition
            or not isinstance(files, list)
            or len(files) != len(expected_files)
        ):
            fail(
                "invalid-release",
                f"anonymous {arch} handoff inventory is invalid",
            )
        for file_index, relative in enumerate(
            expected_files
        ):
            record = exact_keys(
                files[file_index],
                {"asset_name", "bytes", "path", "sha256"},
                f"read-back {arch} file #{file_index}",
            )
            expected_path = f"payload/{arch}/{relative}"
            expected_asset = (
                f"{arch}.{relative.replace('/', '.')}"
            )
            if (
                record["path"] != expected_path
                or record["asset_name"] != expected_asset
            ):
                fail(
                    "invalid-release",
                    f"anonymous {arch} handoff path is not canonical",
                )
            total_bytes += require_integer(
                record["bytes"],
                f"read-back {arch} file #{file_index} bytes",
                1,
                MAX_HANDOFF_ASSET_BYTES,
            )
            require_string(
                record["sha256"],
                f"read-back {arch} file #{file_index} SHA-256",
                SHA256,
            )
            if total_bytes > MAX_HANDOFF_TOTAL_BYTES:
                fail(
                    "invalid-release",
                    "anonymous handoff readback exceeds its size bound",
                )
    return manifest


def verify_published_release(
    *,
    authority_path: pathlib.Path,
    kandelo_root: pathlib.Path,
    source_tap_root: pathlib.Path,
    event_path: pathlib.Path,
    tag: str,
    output: pathlib.Path,
) -> dict[str, Any]:
    require_string(tag, "published handoff release tag", HANDOFF_TAG)
    if output.exists() or output.is_symlink():
        fail(
            "invalid-output",
            "readback output must not already exist",
            2,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    working = pathlib.Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.",
            dir=output.parent,
        )
    )
    try:
        authority, plan, kandelo_root, source_tap_root = prepare_task(
            authority_path=authority_path,
            kandelo_root=kandelo_root,
            source_tap_root=source_tap_root,
            event_path=event_path,
            working=working,
            authenticated_release_reads=True,
        )
        dependencies = fetch_dependency_handoffs(
            kandelo_root=kandelo_root,
            plan=plan,
            root=working / "dependencies",
            authenticated=True,
        )
        executor = (
            kandelo_root
            / "scripts/homebrew-prefix-campaign-executor.py"
        )
        arguments = [
            "python3",
            str(executor),
            "fetch-release",
            "--campaign",
            str(plan.campaign_path),
            "--tag",
            tag,
            "--out",
            str(output),
            "--receipt-out",
            str(working / "readback-receipt.json"),
        ]
        for name in sorted(dependencies):
            arguments.extend(
                ["--dependency-handoff", str(dependencies[name])]
            )
        run_command(
            arguments,
            cwd=kandelo_root,
            # WHY: the frozen executor uses this token only for exact GitHub
            # release-metadata requests. It deliberately omits credentials
            # from every asset download, preserving the public-byte proof.
            inherit_github_token=True,
        )
        receipt_output = output.parent / "readback-receipt.json"
        if receipt_output.exists() or receipt_output.is_symlink():
            fail(
                "invalid-output",
                "read-back receipt output already exists",
                2,
            )
        shutil.copyfile(
            working / "readback-receipt.json",
            receipt_output,
        )
        # WHY: fetch-release already validates the downloaded bytes.
        # This tap-owned boundary must also reject an executor whose
        # output contract or selected task no longer matches the
        # reviewed campaign caller.
        validate_readback_handoff(
            output / "handoff.json",
            authority=authority,
            plan=plan,
            tag=tag,
        )
        summary = {
            "formula": plan.request.formula,
            "kind": "kandelo-homebrew-prefix-build-release-readback",
            "release_tag": tag,
            "schema": 1,
            "status": "verified",
        }
        summary_output = output.parent / "readback-summary.json"
        if summary_output.exists() or summary_output.is_symlink():
            fail(
                "invalid-output",
                "read-back summary output already exists",
                2,
            )
        summary_output.write_bytes(pretty_json(summary))
        return summary
    finally:
        shutil.rmtree(working, ignore_errors=True)


def preflight(
    authority_path: pathlib.Path,
    event_path: pathlib.Path,
    github_output: pathlib.Path | None,
) -> None:
    authority = load_authority(authority_path, require_active=True)
    load_task_request(event_path)
    if github_output is not None:
        append_github_output(
            github_output,
            {
                "campaign-tag": authority.campaign_tag,
                "kandelo-commit": authority.kandelo_commit,
                "release-tag": authority.release_tag,
                "rootfs-wasm32-generation":
                authority.rootfs_wasm32,
                "source-tap-commit": authority.source_tap_commit,
            },
        )


def admit(
    *,
    authority_path: pathlib.Path,
    kandelo_root: pathlib.Path,
    source_tap_root: pathlib.Path,
    event_path: pathlib.Path,
    output: pathlib.Path,
    github_output: pathlib.Path | None,
) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        fail(
            "invalid-output",
            "task plan output must not already exist",
            2,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    working = pathlib.Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.",
            dir=output.parent,
        )
    )
    try:
        authority, plan, _kandelo_root, _source_tap_root = prepare_task(
            authority_path=authority_path,
            kandelo_root=kandelo_root,
            source_tap_root=source_tap_root,
            event_path=event_path,
            working=working,
            authenticated_release_reads=True,
        )
        document = build_plan_document(authority, plan)
        output.write_bytes(pretty_json(document))
        if github_output is not None:
            generations = (
                {
                    "generation-wasm32": authority.rootfs_wasm32,
                    "generation-wasm64": "",
                }
                if plan.generation_kind == "rootfs-wasm32"
                else {
                    "generation-wasm32": "",
                    "generation-wasm64": "",
                }
            )
            append_github_output(
                github_output,
                {
                    "admission-kind": plan.admission_kind,
                    "arch": plan.request.arches[0],
                    "arches": ",".join(plan.request.arches),
                    "dependencies": compact_json(
                        plan.request.dependency_document()
                    ),
                    "disposition": plan.disposition,
                    "formula": plan.request.formula,
                    "generation-kind": plan.generation_kind,
                    "old-tap-commit": plan.old_tap_commit,
                    **generations,
                },
            )
        return document
    finally:
        shutil.rmtree(working, ignore_errors=True)


def add_common_task_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--authority",
        type=pathlib.Path,
        default=DEFAULT_AUTHORITY,
    )
    parser.add_argument(
        "--kandelo-root",
        type=pathlib.Path,
        required=True,
    )
    parser.add_argument(
        "--source-tap-root",
        type=pathlib.Path,
        required=True,
    )
    parser.add_argument(
        "--event",
        type=pathlib.Path,
        required=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    preflight_parser = commands.add_parser("preflight")
    preflight_parser.add_argument(
        "--authority",
        type=pathlib.Path,
        default=DEFAULT_AUTHORITY,
    )
    preflight_parser.add_argument(
        "--event",
        type=pathlib.Path,
        required=True,
    )
    preflight_parser.add_argument(
        "--github-output",
        type=pathlib.Path,
    )

    admit_parser = commands.add_parser("admit")
    add_common_task_arguments(admit_parser)
    admit_parser.add_argument(
        "--out",
        type=pathlib.Path,
        required=True,
    )
    admit_parser.add_argument(
        "--github-output",
        type=pathlib.Path,
    )

    prepare_parser = commands.add_parser("prepare-build")
    add_common_task_arguments(prepare_parser)
    prepare_parser.add_argument(
        "--publications-root",
        type=pathlib.Path,
        required=True,
    )
    prepare_parser.add_argument(
        "--out",
        type=pathlib.Path,
        required=True,
    )
    prepare_parser.add_argument(
        "--github-output",
        type=pathlib.Path,
    )

    reuse_parser = commands.add_parser("prepare-reuse")
    add_common_task_arguments(reuse_parser)
    reuse_parser.add_argument(
        "--old-tap-root",
        type=pathlib.Path,
        required=True,
    )
    reuse_parser.add_argument(
        "--out",
        type=pathlib.Path,
        required=True,
    )
    reuse_parser.add_argument(
        "--github-output",
        type=pathlib.Path,
    )

    verify_parser = commands.add_parser("verify-release")
    add_common_task_arguments(verify_parser)
    verify_parser.add_argument("--tag", required=True)
    verify_parser.add_argument(
        "--out",
        type=pathlib.Path,
        required=True,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "preflight":
            preflight(
                args.authority,
                args.event,
                args.github_output,
            )
            print("prefix-campaign-controller: authority is active")
        elif args.command == "admit":
            document = admit(
                authority_path=args.authority,
                kandelo_root=args.kandelo_root,
                source_tap_root=args.source_tap_root,
                event_path=args.event,
                output=args.out,
                github_output=args.github_output,
            )
            print(
                "prefix-campaign-controller: admitted "
                f"{document['formula']}/{','.join(document['arches'])}"
            )
        elif args.command == "prepare-build":
            document = prepare_build_release(
                authority_path=args.authority,
                kandelo_root=args.kandelo_root,
                source_tap_root=args.source_tap_root,
                event_path=args.event,
                publications_root=args.publications_root,
                output=args.out,
                github_output=args.github_output,
            )
            print(
                "prefix-campaign-controller: prepared immutable handoff "
                f"{document['release_tag']}"
            )
        elif args.command == "prepare-reuse":
            document = prepare_reuse_release(
                authority_path=args.authority,
                kandelo_root=args.kandelo_root,
                source_tap_root=args.source_tap_root,
                old_tap_root=args.old_tap_root,
                event_path=args.event,
                output=args.out,
                github_output=args.github_output,
            )
            print(
                "prefix-campaign-controller: prepared immutable handoff "
                f"{document['release_tag']}"
            )
        elif args.command == "verify-release":
            document = verify_published_release(
                authority_path=args.authority,
                kandelo_root=args.kandelo_root,
                source_tap_root=args.source_tap_root,
                event_path=args.event,
                tag=args.tag,
                output=args.out,
            )
            print(
                "prefix-campaign-controller: anonymously verified "
                f"{document['release_tag']}"
            )
        else:
            raise AssertionError(args.command)
        return 0
    except ControllerError as error:
        print(
            "prefix-campaign-controller: "
            f"status={error.status}: {error}",
            file=sys.stderr,
        )
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
