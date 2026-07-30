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
BROWSER_WASM32_GENERATION = re.compile(
    r"^package-generation-browser-inputs-wasm32-abi-v42-"
    r"sha256-([0-9a-f]{64})$"
)
BROWSER_WASM64_GENERATION = re.compile(
    r"^package-generation-browser-inputs-wasm64-abi-v42-"
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
HANDOFF_PUBLICATION_FILES = (
    "build/bottle.json",
    "build/bottle.tar.gz",
    "build/dependency-provenance.json",
    "build/manifest.json",
    "composition/sidecars-input.json",
    "receipt.json",
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
    browser_inputs_wasm32: str
    browser_inputs_wasm64: str
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
    disposition: str
    generation_kind: str
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


def nonzero_match(
    value: str,
    pattern: re.Pattern[str],
    label: str,
) -> None:
    match = pattern.fullmatch(value)
    if match is None or set(match.group(1)) == {"0"}:
        fail(
            "campaign-authority-inert",
            f"{label} is still an inert placeholder",
            INERT_EXIT,
        )


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
        {
            "browser_inputs_wasm32",
            "browser_inputs_wasm64",
            "rootfs_wasm32",
        },
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
        value["schema"] != 1
        or value["kind"]
        != "kandelo-homebrew-prefix-campaign-caller-authority"
        or value["state"] not in ("inert", "active")
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
        browser_inputs_wasm32=require_string(
            generations["browser_inputs_wasm32"],
            "browser-inputs wasm32 generation",
            BROWSER_WASM32_GENERATION,
        ),
        browser_inputs_wasm64=require_string(
            generations["browser_inputs_wasm64"],
            "browser-inputs wasm64 generation",
            BROWSER_WASM64_GENERATION,
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
    if require_active:
        if authority.state != "active":
            fail(
                "campaign-authority-inert",
                "campaign caller authority is explicitly inert",
                INERT_EXIT,
            )
        for value, pattern, label in (
            (
                authority.campaign_tag,
                CAMPAIGN_TAG,
                "campaign release tag",
            ),
            (
                authority.rootfs_wasm32,
                ROOTFS_GENERATION,
                "rootfs wasm32 generation",
            ),
            (
                authority.browser_inputs_wasm32,
                BROWSER_WASM32_GENERATION,
                "browser-inputs wasm32 generation",
            ),
            (
                authority.browser_inputs_wasm64,
                BROWSER_WASM64_GENERATION,
                "browser-inputs wasm64 generation",
            ),
        ):
            nonzero_match(value, pattern, label)
        if (
            set(authority.kandelo_commit) == {"0"}
            or set(authority.source_tap_commit) == {"0"}
            or set(authority.reusable_workflow_commit) == {"0"}
        ):
            fail(
                "campaign-authority-inert",
                "campaign commit authority is still an inert placeholder",
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
    if arches not in (("wasm32",), ("wasm32", "wasm64")):
        fail(
            "invalid-task-selection",
            "architectures must be exactly wasm32 or wasm32,wasm64",
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


def run_command(
    arguments: Sequence[str],
    *,
    cwd: pathlib.Path,
    anonymous: bool = True,
    timeout: int = COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess[bytes]:
    environment = (
        anonymous_environment() if anonymous else os.environ.copy()
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
        detail = result.stderr.decode(
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
    formulae = formula_index(campaign)
    formula = formulae.get(request.formula)
    if formula is None:
        fail(
            "invalid-task-selection",
            f"campaign does not contain Formula {request.formula}",
            2,
        )
    variants = formula.get("variants")
    if not isinstance(variants, list) or not variants:
        fail(
            "invalid-campaign",
            f"{request.formula} has no campaign variants",
        )
    arches: list[str] = []
    kinds: set[str] = set()
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
        arches.append(arch)
        kinds.add(disposition["kind"])
    if tuple(arches) != request.arches:
        fail(
            "invalid-task-selection",
            "requested architectures differ from the campaign task",
            2,
        )
    if kinds == {"byte-clean-reuse-candidate"}:
        disposition = "reuse"
    elif kinds and kinds <= {"required-build", "required-rebuild"}:
        disposition = "build"
    else:
        fail(
            "invalid-campaign",
            f"{request.formula} mixes build and reuse dispositions",
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
    generation_kind = (
        "browser-inputs"
        if "wasm64" in request.arches
        else "rootfs-wasm32"
    )
    return TaskPlan(
        request=request,
        disposition=disposition,
        generation_kind=generation_kind,
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
        "schema": 1,
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
) -> tuple[Authority, TaskPlan]:
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
    require_target_source_checkout(authority, source_tap_root)
    campaign_path = fetch_campaign(
        authority,
        kandelo_root,
        working / "campaign",
    )
    plan = validate_campaign(campaign_path, authority, request)
    if plan.disposition == "reuse":
        # WHY: reuse must be proved by one reviewed Kandelo generator.
        # A tap-side substitute creates a second admission authority.
        fail(
            "reuse-admission-api-unavailable",
            "Kandelo has no reviewed reuse evidence generator",
            UNAVAILABLE_EXIT,
        )
    return authority, plan


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
        run_command(arguments, cwd=kandelo_root)
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
        authority, plan = prepare_task(
            authority_path=authority_path,
            kandelo_root=kandelo_root,
            source_tap_root=source_tap_root,
            event_path=event_path,
            working=working,
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
        summary = {
            "arches": list(plan.request.arches),
            "authority_sha256": authority.sha256,
            "dependencies": plan.request.dependency_document(),
            "formula": plan.request.formula,
            "kind": "kandelo-homebrew-prefix-build-release-preparation",
            "release_tag": tag,
            "schema": 1,
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
        manifest["schema"] != 1
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
    total_bytes = len(payload)
    for index, arch in enumerate(plan.request.arches):
        publication = exact_keys(
            publications[index],
            {"arch", "files"},
            f"read-back {arch} publication",
        )
        files = publication["files"]
        if (
            publication["arch"] != arch
            or not isinstance(files, list)
            or len(files) != len(HANDOFF_PUBLICATION_FILES)
        ):
            fail(
                "invalid-release",
                f"anonymous {arch} handoff inventory is invalid",
            )
        for file_index, relative in enumerate(
            HANDOFF_PUBLICATION_FILES
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
        authority, plan = prepare_task(
            authority_path=authority_path,
            kandelo_root=kandelo_root,
            source_tap_root=source_tap_root,
            event_path=event_path,
            working=working,
        )
        dependencies = fetch_dependency_handoffs(
            kandelo_root=kandelo_root,
            plan=plan,
            root=working / "dependencies",
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
        run_command(arguments, cwd=kandelo_root)
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
        # WHY: fetch-release already validates the downloaded bytes, but this
        # tap-owned boundary must also reject an executor whose output contract
        # or selected task no longer matches the reviewed campaign caller.
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
                "browser-wasm32-generation":
                authority.browser_inputs_wasm32,
                "browser-wasm64-generation":
                authority.browser_inputs_wasm64,
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
        authority, plan = prepare_task(
            authority_path=authority_path,
            kandelo_root=kandelo_root,
            source_tap_root=source_tap_root,
            event_path=event_path,
            working=working,
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
                    "generation-wasm32":
                    authority.browser_inputs_wasm32,
                    "generation-wasm64":
                    authority.browser_inputs_wasm64,
                }
            )
            append_github_output(
                github_output,
                {
                    "arches": ",".join(plan.request.arches),
                    "dependencies": compact_json(
                        plan.request.dependency_document()
                    ),
                    "disposition": plan.disposition,
                    "formula": plan.request.formula,
                    "generation-kind": plan.generation_kind,
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
