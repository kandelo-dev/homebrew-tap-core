#!/usr/bin/env python3
"""Archive a campaign, then activate its published successor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import Any


AUTHORITY_PATH = pathlib.Path("Kandelo/prefix-campaign-authority.json")
ARCHIVE_ROOT = pathlib.Path(
    "Kandelo/campaigns/prefix-v1/aborted-campaigns"
)
ZERO_SHA = "0" * 40
ZERO_CAMPAIGN = "homebrew-prefix-campaign-sha256-" + "0" * 64
ZERO_GENERATION = (
    "package-generation-rootfs-wasm32-abi-v42-sha256-" + "0" * 64
)
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
CAMPAIGN = re.compile(r"homebrew-prefix-campaign-sha256-([0-9a-f]{64})")
GENERATION = re.compile(
    r"package-generation-rootfs-wasm32-abi-v42-sha256-[0-9a-f]{64}"
)
HANDOFF = re.compile(r"homebrew-prefix-handoff-sha256-[0-9a-f]{64}")
FORMULA = re.compile(r"[a-z0-9][a-z0-9+._-]*")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_DISPATCHES = 256
MAX_TASKS = 256
MAX_RECOVERY_ARCHIVES = 256
SCOPE_KIND = "kandelo-homebrew-prefix-successor-scope"
GRAPH_KIND = "kandelo-prefix-campaign-task-graph"


class TransitionError(RuntimeError):
    """The candidate does not satisfy the campaign transition contract."""


def duplicate_rejecting_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise TransitionError(f"JSON document duplicates {key!r}")
        value[key] = item
    return value


def canonical(value: object) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


def parse_json(payload: bytes, label: str) -> dict[str, Any]:
    if not payload or len(payload) > MAX_JSON_BYTES:
        raise TransitionError(f"{label} is empty or too large")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=duplicate_rejecting_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                TransitionError(f"{label} contains {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TransitionError(f"{label} is not strict UTF-8 JSON") from error
    if not isinstance(value, dict) or payload != canonical(value):
        raise TransitionError(f"{label} is not canonical pretty JSON")
    return value


def load_json(path: pathlib.Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise TransitionError(f"{label} is not a regular file")
    payload = path.read_bytes()
    value = parse_json(payload, label)
    return value, payload


def exact_keys(value: object, keys: list[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or list(value) != keys:
        raise TransitionError(f"{label} field set or order changed")
    return value


def require_string(
    value: object,
    label: str,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not value:
        raise TransitionError(f"{label} is not a nonempty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise TransitionError(f"{label} has an invalid identity")
    return value


def validate_authority(
    authority: dict[str, Any],
    *,
    state: str,
) -> None:
    exact_keys(
        authority,
        [
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
        ],
        "campaign authority",
    )
    if (
        authority["schema"] != 2
        or authority["kind"]
        != "kandelo-homebrew-prefix-campaign-caller-authority"
        or authority["kandelo_repository"] != "Automattic/kandelo"
        or authority["source_tap_repository"]
        != "kandelo-dev/homebrew-tap-core"
        or authority["source_tap_name"] != "kandelo-dev/tap-core"
        or authority["release_tag"] != "bottles-abi-v42"
        or authority["state"] != state
    ):
        raise TransitionError("campaign authority has an invalid contract")
    kandelo = require_string(
        authority["kandelo_commit"], "Kandelo commit", SHA
    )
    if authority["reusable_workflow_commit"] != kandelo:
        raise TransitionError("campaign authority splits Kandelo executors")
    release = exact_keys(
        authority["campaign_release"],
        ["repository", "tag"],
        "campaign release",
    )
    if release["repository"] != "kandelo-dev/homebrew-tap-core":
        raise TransitionError("campaign authority names another repository")
    generations = exact_keys(
        authority["package_generations"],
        ["rootfs_wasm32"],
        "campaign package generations",
    )
    target = exact_keys(
        authority["target_source"],
        [
            "manifest_path",
            "manifest_sha256",
            "source_root",
            "source_tree_git_oid",
            "target_tree_git_oid",
        ],
        "campaign target source",
    )
    if (
        target["manifest_path"]
        != "Kandelo/campaigns/prefix-v1/manifest.json"
        or target["source_root"] != "Kandelo/campaigns/prefix-v1/source"
    ):
        raise TransitionError("campaign target source paths changed")
    require_string(target["manifest_sha256"], "target manifest", SHA256)
    require_string(target["source_tree_git_oid"], "source tree", SHA)
    require_string(target["target_tree_git_oid"], "target tree", SHA)
    if state == "active":
        tag = require_string(release["tag"], "campaign tag", CAMPAIGN)
        generation = require_string(
            generations["rootfs_wasm32"], "rootfs generation", GENERATION
        )
        source = require_string(
            authority["source_tap_commit"], "source tap commit", SHA
        )
        if (
            tag == ZERO_CAMPAIGN
            or generation == ZERO_GENERATION
            or source == ZERO_SHA
        ):
            raise TransitionError("active campaign contains zero authority")
    else:
        if (
            release["tag"] != ZERO_CAMPAIGN
            or generations["rootfs_wasm32"] != ZERO_GENERATION
            or authority["source_tap_commit"] != ZERO_SHA
        ):
            raise TransitionError("armed campaign retains executable data")


def git_output(root: pathlib.Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise TransitionError(
            f"Git command failed: {' '.join(arguments)}"
        )
    return result.stdout


def validate_activation_worktree(
    root: pathlib.Path,
    *,
    allow_authority_change: bool,
) -> None:
    status = git_output(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if not status:
        return
    expected_path = AUTHORITY_PATH.as_posix().encode()
    entries = [entry for entry in status.split(b"\0") if entry]
    if (
        not allow_authority_change
        or len(entries) != 1
        or len(entries[0]) < 4
        or entries[0][:2] not in (b" M", b"M ", b"MM")
        or entries[0][3:] != expected_path
    ):
        raise TransitionError(
            "successor activation worktree changes more than authority"
        )


def load_head_json(
    root: pathlib.Path,
    value: object,
    label: str,
) -> tuple[dict[str, Any], bytes, str]:
    raw = require_string(value, f"{label} path")
    relative = pathlib.PurePosixPath(raw)
    if (
        relative.is_absolute()
        or raw != relative.as_posix()
        or not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise TransitionError(f"{label} path escapes the repository")
    candidate = root.joinpath(*relative.parts)
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise TransitionError(f"{label} path contains a symlink")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise TransitionError(
            f"{label} path escapes the repository"
        ) from error
    document, payload = load_json(resolved, label)
    if git_output(root, "show", f"HEAD:{relative.as_posix()}") != payload:
        raise TransitionError(f"{label} bytes differ from protected HEAD")
    return document, payload, relative.as_posix()


def parse_task_list(value: object, label: str) -> list[tuple[str, str]]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_TASKS
    ):
        raise TransitionError(f"{label} is not a bounded task array")
    tasks: list[tuple[str, str]] = []
    for position, item in enumerate(value):
        task = exact_keys(
            item,
            ["arch", "formula"],
            f"{label} task #{position}",
        )
        arch = require_string(task["arch"], f"{label} architecture")
        formula = require_string(
            task["formula"], f"{label} Formula", FORMULA
        )
        if arch not in ("wasm32", "wasm64"):
            raise TransitionError(f"{label} architecture is unsupported")
        tasks.append((formula, arch))
    if tasks != sorted(set(tasks)):
        raise TransitionError(f"{label} tasks are not unique and sorted")
    return tasks


def parse_archive(
    archive_path: pathlib.Path,
) -> tuple[
    dict[str, Any],
    bytes,
    dict[tuple[str, str], str],
]:
    archive, payload = load_json(
        archive_path, "abandoned campaign archive"
    )
    exact_keys(
        archive,
        [
            "abandoned_at",
            "authority",
            "cause",
            "dispatches",
            "kind",
            "recovery",
            "schema",
        ],
        "abandoned campaign archive",
    )
    if (
        archive["schema"] != 1
        or archive["kind"]
        != "kandelo-homebrew-prefix-abandoned-campaign"
    ):
        raise TransitionError("abandoned archive has an invalid contract")
    require_string(archive["abandoned_at"], "abandoned timestamp")
    cause = exact_keys(
        archive["cause"],
        ["corrective_workstream", "kind", "summary"],
        "abandoned campaign cause",
    )
    for key in cause:
        require_string(cause[key], f"abandoned campaign cause {key}")
    if archive["recovery"] != {
        "authority_state": "armed",
        "fresh_builds_require_successor_campaign": True,
        "partial_publications_require_successor_revalidation": True,
        "predecessor_handoffs_are_not_successor_authority": True,
        "published_handoffs_remain_independently_usable": True,
    }:
        raise TransitionError("abandoned archive recovery policy changed")
    archived = exact_keys(
        archive["authority"],
        [
            "activation_commit",
            "campaign_release",
            "kandelo_commit",
            "payload_sha256",
            "rootfs_wasm32",
            "source_tap_commit",
            "target_source",
        ],
        "abandoned campaign authority",
    )
    release = exact_keys(
        archived["campaign_release"],
        ["id", "repository", "tag"],
        "abandoned campaign release",
    )
    if (
        not isinstance(release["id"], int)
        or isinstance(release["id"], bool)
        or release["id"] < 1
        or release["repository"] != "kandelo-dev/homebrew-tap-core"
    ):
        raise TransitionError("abandoned campaign release is invalid")
    tag = require_string(release["tag"], "abandoned campaign tag", CAMPAIGN)
    tag_match = CAMPAIGN.fullmatch(tag)
    assert tag_match is not None
    if archive_path.stem != tag_match.group(1):
        raise TransitionError("abandoned archive is not content-addressed")
    require_string(archived["activation_commit"], "activation commit", SHA)
    require_string(archived["kandelo_commit"], "Kandelo commit", SHA)
    require_string(archived["payload_sha256"], "authority digest", SHA256)
    require_string(archived["rootfs_wasm32"], "rootfs generation", GENERATION)
    require_string(archived["source_tap_commit"], "source tap commit", SHA)
    target = exact_keys(
        archived["target_source"],
        [
            "manifest_path",
            "manifest_sha256",
            "source_root",
            "source_tree_git_oid",
            "target_tree_git_oid",
        ],
        "abandoned target source",
    )
    if (
        target["manifest_path"]
        != "Kandelo/campaigns/prefix-v1/manifest.json"
        or target["source_root"]
        != "Kandelo/campaigns/prefix-v1/source"
    ):
        raise TransitionError("abandoned target source paths changed")
    require_string(target["manifest_sha256"], "target manifest", SHA256)
    require_string(target["source_tree_git_oid"], "source tree", SHA)
    require_string(target["target_tree_git_oid"], "target tree", SHA)
    dispatches = archive["dispatches"]
    if (
        not isinstance(dispatches, list)
        or not dispatches
        or len(dispatches) > MAX_DISPATCHES
    ):
        raise TransitionError("abandoned archive dispatch set is invalid")
    run_ids: set[int] = set()
    completed: dict[tuple[str, str], str] = {}
    handoffs: set[str] = set()
    for position, dispatch in enumerate(dispatches):
        label = f"abandoned dispatch #{position}"
        if not isinstance(dispatch, dict):
            raise TransitionError(f"{label} is not an object")
        required = {"arch", "formula", "result", "run_id"}
        allowed = required | {
            "failure",
            "handoff_release",
            "partial_publication",
        }
        if not required <= set(dispatch) or not set(dispatch) <= allowed:
            raise TransitionError(f"{label} has an invalid field set")
        formula = require_string(
            dispatch["formula"], f"{label} Formula", FORMULA
        )
        arch = require_string(dispatch["arch"], f"{label} architecture")
        if arch not in ("wasm32", "wasm64"):
            raise TransitionError(f"{label} architecture is invalid")
        result = require_string(dispatch["result"], f"{label} result")
        run_id = dispatch["run_id"]
        if (
            not isinstance(run_id, int)
            or isinstance(run_id, bool)
            or run_id < 1
            or run_id in run_ids
        ):
            raise TransitionError(f"{label} run id is invalid or repeated")
        run_ids.add(run_id)
        if result != "handoff-published-and-publicly-verified":
            if "handoff_release" in dispatch:
                raise TransitionError(f"{label} names an unverified handoff")
            continue
        if set(dispatch) != required | {"handoff_release"}:
            raise TransitionError(f"{label} verified handoff shape changed")
        handoff = exact_keys(
            dispatch["handoff_release"],
            ["id", "tag"],
            f"{label} handoff",
        )
        identity = require_string(
            handoff["tag"], f"{label} handoff tag", HANDOFF
        )
        key = (formula, arch)
        if (
            not isinstance(handoff["id"], int)
            or isinstance(handoff["id"], bool)
            or handoff["id"] < 1
            or key in completed
            or identity in handoffs
        ):
            raise TransitionError(f"{label} handoff identity is repeated")
        completed[key] = identity
        handoffs.add(identity)
    return archive, payload, completed


def archive_recovery_record(
    archive: dict[str, Any],
    archive_path: str,
    archive_payload: bytes,
) -> dict[str, Any]:
    authority = archive["authority"]
    tag = authority["campaign_release"]["tag"]
    match = CAMPAIGN.fullmatch(tag)
    assert match is not None
    return {
        "activation_commit": authority["activation_commit"],
        "archive": {
            "path": archive_path,
            "sha256": hashlib.sha256(archive_payload).hexdigest(),
        },
        "campaign": {"sha256": match.group(1), "tag": tag},
        "kandelo_commit": authority["kandelo_commit"],
        "source_tap_commit": authority["source_tap_commit"],
        "target_tree_git_oid": authority["target_source"][
            "target_tree_git_oid"
        ],
    }


def validate_campaign_recovery(
    *,
    root: pathlib.Path,
    value: object,
    required_record: dict[str, Any],
) -> tuple[
    set[str],
    dict[tuple[str, tuple[str, str]], str],
]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_RECOVERY_ARCHIVES
    ):
        raise TransitionError(
            "successor recovery records are not a bounded array"
        )
    paths: list[str] = []
    records: list[dict[str, Any]] = []
    tags: set[str] = set()
    routes: dict[tuple[str, tuple[str, str]], str] = {}
    for position, item in enumerate(value):
        record = exact_keys(
            item,
            [
                "activation_commit",
                "archive",
                "campaign",
                "kandelo_commit",
                "source_tap_commit",
                "target_tree_git_oid",
            ],
            f"successor recovery record #{position}",
        )
        archive_reference = exact_keys(
            record["archive"],
            ["path", "sha256"],
            f"successor recovery record #{position} archive",
        )
        archive, payload, relative = load_head_json(
            root,
            archive_reference["path"],
            f"successor recovery archive #{position}",
        )
        digest = require_string(
            archive_reference["sha256"],
            f"successor recovery archive #{position} digest",
            SHA256,
        )
        if hashlib.sha256(payload).hexdigest() != digest:
            raise TransitionError(
                f"successor recovery archive #{position} digest changed"
            )
        parsed, parsed_payload, handoffs = parse_archive(
            root.joinpath(*pathlib.PurePosixPath(relative).parts)
        )
        if parsed != archive or parsed_payload != payload:
            raise AssertionError("recovery parser changed protected bytes")
        expected = archive_recovery_record(parsed, relative, payload)
        if record != expected:
            raise TransitionError(
                f"successor recovery record #{position} changed"
            )
        tag = expected["campaign"]["tag"]
        if tag in tags:
            raise TransitionError("successor recovery repeats a campaign")
        tags.add(tag)
        for task, handoff in handoffs.items():
            routes[(tag, task)] = handoff
        paths.append(relative)
        records.append(record)
    if paths != sorted(set(paths)):
        raise TransitionError(
            "successor recovery records are not unique and sorted"
        )
    if required_record not in records:
        raise TransitionError(
            "successor recovery omits the selected predecessor"
        )
    return tags, routes


def validate_archive(
    *,
    root: pathlib.Path,
    archive_path: pathlib.Path,
    active: dict[str, Any],
    active_payload: bytes,
    activation_commit: str,
) -> None:
    require_string(activation_commit, "activation commit", SHA)
    # WHY: current working bytes alone cannot prove what was executable.
    # Require the exact authority bytes from their protected ancestor.
    if git_output(
        root,
        "merge-base",
        "--is-ancestor",
        activation_commit,
        "HEAD",
    ):
        raise AssertionError("merge-base unexpectedly wrote output")
    historical = git_output(
        root,
        "show",
        f"{activation_commit}:{AUTHORITY_PATH.as_posix()}",
    )
    if historical != active_payload:
        raise TransitionError(
            "activation commit does not contain the active authority"
        )
    tag = active["campaign_release"]["tag"]
    match = CAMPAIGN.fullmatch(tag)
    assert match is not None
    expected_path = root / ARCHIVE_ROOT / f"{match.group(1)}.json"
    if archive_path.resolve() != expected_path.resolve():
        raise TransitionError("abandoned archive path is not content-addressed")
    archive, _archive_payload, _completed = parse_archive(archive_path)
    archived = archive["authority"]
    release = archived["campaign_release"]
    tag = active["campaign_release"]["tag"]
    if (
        release["repository"] != active["campaign_release"]["repository"]
        or release["tag"] != tag
        or archived["activation_commit"] != activation_commit
        or archived["kandelo_commit"] != active["kandelo_commit"]
        or archived["payload_sha256"]
        != hashlib.sha256(active_payload).hexdigest()
        or archived["rootfs_wasm32"]
        != active["package_generations"]["rootfs_wasm32"]
        or archived["source_tap_commit"] != active["source_tap_commit"]
        or archived["target_source"] != active["target_source"]
    ):
        raise TransitionError("abandoned archive differs from active authority")


def armed_authority(active: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(active))
    value["campaign_release"]["tag"] = ZERO_CAMPAIGN
    value["package_generations"]["rootfs_wasm32"] = ZERO_GENERATION
    value["source_tap_commit"] = ZERO_SHA
    value["state"] = "armed"
    validate_authority(value, state="armed")
    return value


def validate_successor_scope(
    *,
    root: pathlib.Path,
    scope_path: str,
) -> tuple[
    list[tuple[str, str]],
    set[tuple[str, str]],
    set[tuple[str, str]],
    dict[str, str],
    dict[str, Any],
    bytes,
    str,
    dict[tuple[str, str], str],
]:
    scope, scope_payload, scope_relative = load_head_json(
        root, scope_path, "successor scope"
    )
    exact_keys(
        scope,
        [
            "build_tasks",
            "graph",
            "kind",
            "predecessor_archive",
            "reuse_tasks",
            "schema",
        ],
        "successor scope",
    )
    if scope["schema"] != 1 or scope["kind"] != SCOPE_KIND:
        raise TransitionError("successor scope has an invalid contract")
    graph_reference = exact_keys(
        scope["graph"], ["path", "sha256"], "successor graph reference"
    )
    graph, graph_payload, _graph_relative = load_head_json(
        root, graph_reference["path"], "successor task graph"
    )
    graph_sha = require_string(
        graph_reference["sha256"], "successor graph digest", SHA256
    )
    if hashlib.sha256(graph_payload).hexdigest() != graph_sha:
        raise TransitionError("successor task graph digest changed")
    exact_keys(
        graph,
        ["kind", "max_active", "repository", "schema", "tasks", "workflow"],
        "successor task graph",
    )
    max_active = graph["max_active"]
    if (
        graph["schema"] != 1
        or graph["kind"] != GRAPH_KIND
        or graph["repository"] != "Kandelo-dev/homebrew-tap-core"
        or graph["workflow"]
        != ".github/workflows/prefix-campaign-bottles.yml"
        or not isinstance(max_active, int)
        or isinstance(max_active, bool)
        or not 1 <= max_active <= 32
    ):
        raise TransitionError("successor task graph has an invalid contract")
    graph_tasks = parse_task_list(graph["tasks"], "successor graph")
    reuse_tasks = set(
        parse_task_list(scope["reuse_tasks"], "successor reuse scope")
    )
    build_tasks = set(
        parse_task_list(scope["build_tasks"], "successor build scope")
    )
    if reuse_tasks & build_tasks or reuse_tasks | build_tasks != set(
        graph_tasks
    ):
        raise TransitionError("successor scope does not partition its graph")
    archive_reference = exact_keys(
        scope["predecessor_archive"],
        ["path", "sha256"],
        "successor predecessor archive reference",
    )
    archive_document, archive_payload, archive_relative = load_head_json(
        root,
        archive_reference["path"],
        "successor predecessor archive",
    )
    archive_sha = require_string(
        archive_reference["sha256"],
        "successor predecessor archive digest",
        SHA256,
    )
    if hashlib.sha256(archive_payload).hexdigest() != archive_sha:
        raise TransitionError("successor predecessor archive digest changed")
    archive, parsed_payload, handoffs = parse_archive(
        root.joinpath(*pathlib.PurePosixPath(archive_relative).parts)
    )
    if archive != archive_document or parsed_payload != archive_payload:
        raise AssertionError("archive parser changed protected bytes")
    if set(handoffs) != reuse_tasks:
        raise TransitionError(
            "successor reuse scope differs from verified handoffs"
        )
    scope_reference = {
        "path": scope_relative,
        "sha256": hashlib.sha256(scope_payload).hexdigest(),
    }
    return (
        graph_tasks,
        reuse_tasks,
        build_tasks,
        scope_reference,
        archive,
        archive_payload,
        archive_relative,
        handoffs,
    )


# WHY: the full campaign schema belongs to the exact K-pinned Kandelo
# executor. Importing it here would require a second repository checkout,
# its companion modules, and its declared build environment. This helper
# validates only the cross-repository activation boundary: exact source,
# recovery, graph, and routes. Every task later passes the complete campaign
# through Kandelo's full validator before any publication write.
def validate_campaign_for_activation(
    *,
    root: pathlib.Path,
    campaign_path: pathlib.Path,
    scope_path: str,
    armed: dict[str, Any],
    source_tap_commit: str,
) -> str:
    (
        graph_tasks,
        expected_reuse,
        expected_builds,
        expected_scope,
        archive,
        archive_payload,
        archive_path,
        handoffs,
    ) = validate_successor_scope(root=root, scope_path=scope_path)
    campaign, payload = load_json(campaign_path, "successor campaign")
    if (
        campaign.get("schema") != 3
        or campaign.get("kind")
        != "kandelo-homebrew-guest-prefix-campaign"
        or not isinstance(campaign.get("formulae"), list)
        or not campaign["formulae"]
    ):
        raise TransitionError("successor campaign has an unsupported schema")
    authority = campaign.get("authority")
    if not isinstance(authority, dict):
        raise TransitionError("successor campaign lacks authority")
    successor_scope = exact_keys(
        authority.get("successor_scope"),
        ["path", "sha256"],
        "successor campaign scope authority",
    )
    if successor_scope != expected_scope:
        raise TransitionError(
            "successor campaign scope authority differs from T_ARM"
        )
    recovery_source = exact_keys(
        authority.get("predecessor_recovery_source"),
        ["commit", "repository"],
        "successor predecessor recovery source",
    )
    if recovery_source != {
        "commit": source_tap_commit,
        "repository": "kandelo-dev/homebrew-tap-core",
    }:
        raise TransitionError("successor recovery source differs from T_ARM")
    recovery = authority.get("predecessor_recovery")
    expected_recovery = archive_recovery_record(
        archive, archive_path, archive_payload
    )
    recovery_tags, recovery_routes = validate_campaign_recovery(
        root=root,
        value=recovery,
        required_record=expected_recovery,
    )
    if (
        authority.get("kandelo_commit") != armed["kandelo_commit"]
        or authority.get("source_tap_commit") != source_tap_commit
        or authority.get("tap_repository")
        != "kandelo-dev/homebrew-tap-core"
        or authority.get("tap_name") != "kandelo-dev/tap-core"
        or authority.get("current_kandelo_abi") != 42
    ):
        raise TransitionError("successor campaign authority differs from armed tap")
    materialization = authority.get("source_materialization")
    if not isinstance(materialization, dict):
        raise TransitionError("successor campaign lacks source materialization")
    manifest = materialization.get("manifest")
    if not isinstance(manifest, dict):
        raise TransitionError("successor campaign lacks source manifest")
    expected_target = armed["target_source"]
    if (
        materialization.get("kind") != "sealed-target-overlay-v1"
        or manifest.get("path") != expected_target["manifest_path"]
        or manifest.get("sha256") != expected_target["manifest_sha256"]
        or materialization.get("source_root") != expected_target["source_root"]
        or materialization.get("source_tree_git_oid")
        != expected_target["source_tree_git_oid"]
        or materialization.get("target_tree_git_oid")
        != expected_target["target_tree_git_oid"]
    ):
        raise TransitionError("successor campaign target source changed")
    campaign_tasks: list[tuple[str, str]] = []
    formula_names: list[str] = []
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    used_recovery_tags: set[str] = set()
    for formula_position, formula in enumerate(campaign["formulae"]):
        if not isinstance(formula, dict):
            raise TransitionError("successor Formula record is not an object")
        name = require_string(
            formula.get("name"),
            f"successor Formula #{formula_position} name",
            FORMULA,
        )
        formula_names.append(name)
        variants = formula.get("variants")
        if not isinstance(variants, list) or not variants:
            raise TransitionError(f"successor Formula {name} has no variants")
        arches: list[str] = []
        for variant_position, variant in enumerate(variants):
            if not isinstance(variant, dict):
                raise TransitionError(
                    f"successor Formula {name} variant "
                    f"#{variant_position} is not an object"
                )
            arch = require_string(
                variant.get("arch"),
                f"successor Formula {name} variant architecture",
            )
            if arch not in ("wasm32", "wasm64"):
                raise TransitionError(
                    f"successor Formula {name} architecture is unsupported"
                )
            arches.append(arch)
            task = (name, arch)
            campaign_tasks.append(task)
            if "reuse_source" in variant:
                source = exact_keys(
                    variant["reuse_source"],
                    ["arch", "campaign_tag", "handoff_tag", "kind"],
                    f"successor Formula {name} reuse source",
                )
                require_string(
                    source["campaign_tag"],
                    f"successor Formula {name} reuse campaign",
                    CAMPAIGN,
                )
                require_string(
                    source["handoff_tag"],
                    f"successor Formula {name} reuse handoff",
                    HANDOFF,
                )
                source_tag = source["campaign_tag"]
                if source != {
                    "arch": arch,
                    "campaign_tag": source_tag,
                    "handoff_tag": recovery_routes.get((source_tag, task)),
                    "kind": "predecessor-handoff",
                }:
                    raise TransitionError(
                        f"successor Formula {name} reuse route changed"
                    )
                used_recovery_tags.add(source_tag)
            else:
                disposition = variant.get("disposition")
                if (
                    not isinstance(disposition, dict)
                    or disposition.get("kind")
                    not in (
                        "byte-clean-reuse-candidate",
                        "required-build",
                        "required-rebuild",
                    )
                ):
                    raise TransitionError(
                        f"successor Formula {name} disposition is invalid"
                    )
            if task in routes:
                raise TransitionError("successor campaign repeats a task")
            routes[task] = variant
        if arches != sorted(set(arches)):
            raise TransitionError(
                f"successor Formula {name} variants are not unique and sorted"
            )
    if formula_names != sorted(set(formula_names)):
        raise TransitionError(
            "successor Formula records are not unique and sorted"
        )
    if campaign_tasks != sorted(set(campaign_tasks)):
        raise TransitionError(
            "successor campaign tasks are not unique and sorted"
        )
    if used_recovery_tags != recovery_tags:
        raise TransitionError(
            "successor recovery records differ from used routes"
        )
    for task in graph_tasks:
        variant = routes.get(task)
        if variant is None:
            raise TransitionError("successor campaign lacks a selected task")
        if task in expected_reuse:
            selected_source = variant.get("reuse_source")
            if selected_source != {
                "arch": task[1],
                "campaign_tag": archive["authority"][
                    "campaign_release"
                ]["tag"],
                "handoff_tag": handoffs[task],
                "kind": "predecessor-handoff",
            }:
                raise TransitionError(
                    "successor selected reuse route changed"
                )
        elif task in expected_builds:
            disposition = variant.get("disposition")
            if (
                "reuse_source" in variant
                or not isinstance(disposition, dict)
                or disposition.get("kind")
                not in ("required-build", "required-rebuild")
            ):
                raise TransitionError(
                    "successor selected build route became reusable"
                )
        else:
            raise AssertionError("scope graph contains an unknown route")
    digest = hashlib.sha256(payload).hexdigest()
    return f"homebrew-prefix-campaign-sha256-{digest}"


def activated_authority(
    *,
    armed: dict[str, Any],
    campaign_tag: str,
    generation: str,
    source_tap_commit: str,
) -> dict[str, Any]:
    require_string(campaign_tag, "successor campaign tag", CAMPAIGN)
    require_string(generation, "successor rootfs generation", GENERATION)
    require_string(source_tap_commit, "successor source tap commit", SHA)
    value = json.loads(json.dumps(armed))
    value["campaign_release"]["tag"] = campaign_tag
    value["package_generations"]["rootfs_wasm32"] = generation
    value["source_tap_commit"] = source_tap_commit
    value["state"] = "active"
    validate_authority(value, state="active")
    return value


def atomic_write(path: pathlib.Path, payload: bytes) -> None:
    mode = path.stat().st_mode & 0o777
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # WHY: syncing the file before rename is not enough after a host
        # crash. Sync the directory entry that now names the new bytes.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("archive-active")
    archive.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd())
    archive.add_argument("--archive", type=pathlib.Path, required=True)
    archive.add_argument("--activation-commit", required=True)
    archive.add_argument("--apply", action="store_true")
    activate = commands.add_parser("activate-successor")
    activate.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd())
    activate.add_argument("--campaign", type=pathlib.Path, required=True)
    activate.add_argument("--scope", required=True)
    activate.add_argument("--rootfs-generation", required=True)
    activate.add_argument("--source-tap-commit", required=True)
    activate.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    arguments = parse_args(argv)
    try:
        root = arguments.root.resolve(strict=True)
        authority_path = root / AUTHORITY_PATH
        authority, payload = load_json(authority_path, "campaign authority")
        if arguments.command == "archive-active":
            if authority.get("state") == "active":
                validate_authority(authority, state="active")
                active = authority
                active_payload = payload
            elif authority.get("state") == "armed":
                validate_authority(authority, state="armed")
                active_payload = git_output(
                    root,
                    "show",
                    (
                        f"{arguments.activation_commit}:"
                        f"{AUTHORITY_PATH.as_posix()}"
                    ),
                )
                active = parse_json(
                    active_payload, "historical active authority"
                )
                validate_authority(active, state="active")
                if authority != armed_authority(active):
                    raise TransitionError(
                        "armed authority is not the exact archive result"
                    )
            else:
                raise TransitionError(
                    "campaign authority is neither active nor armed"
                )
            validate_archive(
                root=root,
                archive_path=arguments.archive.resolve(strict=True),
                active=active,
                active_payload=active_payload,
                activation_commit=arguments.activation_commit,
            )
            result = armed_authority(active)
        else:
            # WHY: the immutable successor release selects workflows
            # from T_ARM. Requiring current HEAD prevents activation on
            # another tree with equivalent-looking authority data.
            head = git_output(root, "rev-parse", "HEAD").decode().strip()
            if head != arguments.source_tap_commit:
                raise TransitionError(
                    "successor source tap commit is not current HEAD"
                )
            head_payload = git_output(
                root, "show", f"HEAD:{AUTHORITY_PATH.as_posix()}"
            )
            head_authority = parse_json(
                head_payload, "protected armed authority"
            )
            validate_authority(head_authority, state="armed")
            if authority.get("state") == "armed":
                validate_authority(authority, state="armed")
                if payload != head_payload:
                    raise TransitionError(
                        "armed authority bytes differ from protected HEAD"
                    )
                validate_activation_worktree(
                    root, allow_authority_change=False
                )
                armed = authority
            elif authority.get("state") == "active":
                validate_authority(authority, state="active")
                validate_activation_worktree(
                    root, allow_authority_change=True
                )
                armed = head_authority
            else:
                raise TransitionError(
                    "campaign authority is neither armed nor active"
                )
            campaign_tag = validate_campaign_for_activation(
                root=root,
                campaign_path=arguments.campaign.resolve(strict=True),
                scope_path=arguments.scope,
                armed=armed,
                source_tap_commit=arguments.source_tap_commit,
            )
            result = activated_authority(
                armed=armed,
                campaign_tag=campaign_tag,
                generation=arguments.rootfs_generation,
                source_tap_commit=arguments.source_tap_commit,
            )
            if authority.get("state") == "active" and authority != result:
                raise TransitionError(
                    "active authority is not the exact successor result"
                )
        result_payload = canonical(result)
        if arguments.apply:
            # WHY: successor activation is a data-only second commit.
            # This helper owns only the authority file and cannot hide
            # executable changes in the activation step.
            if payload == result_payload:
                action = "already exact"
            else:
                atomic_write(authority_path, result_payload)
                action = "updated"
        else:
            action = "would update"
        print(
            f"prefix campaign authority {action}: "
            f"state={result['state']} "
            f"tag={result['campaign_release']['tag']}"
        )
        return 0
    except (OSError, TransitionError) as error:
        print(
            f"transition-prefix-campaign-authority: {error}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
