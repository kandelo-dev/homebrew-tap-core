#!/usr/bin/env python3
"""Rotate the protected Homebrew callers to one exact Kandelo generation.

The predecessor and successor authorities are explicit operator inputs so the
same fail-closed helper can rotate a reviewed protected-main tuple without
predicting a future Kandelo merge or package generation. By default it
validates and previews the complete transition. It writes only when --apply is
supplied.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
from collections.abc import Mapping


SHA = re.compile(r"[0-9a-f]{40}")
GENERATION_TAG = re.compile(
    r"package-generation-rootfs-wasm32-abi-v42-sha256-[0-9a-f]{64}"
)
CALLER_SHA256 = re.compile(r"[0-9a-f]{64}")

DRY_RUN_PATH = pathlib.Path(".github/workflows/dry-run-bottles.yml")
MAINTENANCE_PATH = pathlib.Path(".github/workflows/maintain-bottles.yml")
PREFIX_CAMPAIGN_PATH = pathlib.Path(
    ".github/workflows/prefix-campaign-bottles.yml"
)
PUBLISH_PATH = pathlib.Path(".github/workflows/publish-bottles.yml")
CLOSED_SELECTION_PATH = pathlib.Path(
    ".github/workflows/publish-closed-selection.yml"
)
FIRST_PUBLICATION_PATH = pathlib.Path(
    ".github/workflows/repository-namespace-canary.yml"
)
PREFIX_AUTHORITY_PATH = pathlib.Path(
    "Kandelo/prefix-campaign-authority.json"
)
TRUST_PATH = pathlib.Path("Kandelo/test-workflow-trust.rb")
CONTROLLER_PATH = pathlib.Path("scripts/abi42-rollout.py")
ROTATION_PATHS = (
    DRY_RUN_PATH,
    MAINTENANCE_PATH,
    PREFIX_CAMPAIGN_PATH,
    PUBLISH_PATH,
    CLOSED_SELECTION_PATH,
    FIRST_PUBLICATION_PATH,
    PREFIX_AUTHORITY_PATH,
    TRUST_PATH,
    CONTROLLER_PATH,
)

ZERO_SHA = "0" * 40
ZERO_CAMPAIGN_TAG = "homebrew-prefix-campaign-sha256-" + "0" * 64
ZERO_GENERATION_TAG = (
    "package-generation-rootfs-wasm32-abi-v42-sha256-" + "0" * 64
)


class RotationError(RuntimeError):
    """The candidate tree does not match the reviewed rotation contract."""


@dataclasses.dataclass(frozen=True)
class Rotation:
    contents: Mapping[pathlib.Path, bytes]
    changed: tuple[pathlib.Path, ...]
    caller_sha256: str


def scalar_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(
        rf"^(?P<prefix>{prefix})(?P<value>[^\s]+)(?P<suffix>\s*)$",
        flags=re.MULTILINE,
    )


DRY_RUN_USES = scalar_pattern(
    r"\s+uses:\s+Automattic/kandelo/\.github/workflows/"
    r"reusable-homebrew-bottle-publish\.yml@"
)
MAINTENANCE_USES = scalar_pattern(
    r"\s+uses:\s+Automattic/kandelo/\.github/workflows/"
    r"reusable-homebrew-bottle-maintenance\.yml@"
)
PUBLISH_USES = DRY_RUN_USES
PREFIX_BOTTLE_USES = scalar_pattern(
    r"\s+uses:\s+Automattic/kandelo/\.github/workflows/"
    r"reusable-homebrew-bottle-publish\.yml@"
)
PREFIX_FIRST_CHILD_USES = scalar_pattern(
    r"\s+uses:\s+Automattic/kandelo/\.github/workflows/"
    r"reusable-homebrew-prefix-first-child-publish\.yml@"
)
CLOSED_SELECTION_USES = scalar_pattern(
    r"\s+uses:\s+Automattic/kandelo/\.github/workflows/"
    r"reusable-homebrew-closed-selection-publish\.yml@"
)
FIRST_PUBLICATION_USES = scalar_pattern(
    r"\s+uses:\s+Automattic/kandelo/\.github/workflows/"
    r"reusable-homebrew-repository-namespace-canary\.yml@"
)
EXACT_KANDELO_REF = scalar_pattern(r"\s+kandelo-ref:\s+")
PACKAGE_GENERATION = scalar_pattern(r"\s+package-generation-wasm32:\s+")
TRUST_KANDELO_SHA = scalar_pattern(
    r'CURRENT_KANDELO_WORKFLOW_SHA\s*=\s*"'
)
TRUST_DRY_RUN_KANDELO_SHA = scalar_pattern(
    r'DRY_RUN_KANDELO_WORKFLOW_SHA\s*=\s*"'
)
TRUST_FIRST_PUBLICATION_KANDELO_SHA = scalar_pattern(
    r'FIRST_PUBLICATION_KANDELO_SHA\s*=\s*"'
)
TRUST_CLOSED_SELECTION_KANDELO_SHA = re.compile(
    r"^(?P<prefix>CLOSED_SELECTION_KANDELO_SHA\s*=\s*\n\s*\")"
    r"(?P<value>[^\s\"]+\")(?P<suffix>\s*)$",
    flags=re.MULTILINE,
)
TRUST_PREFIX_CAMPAIGN_KANDELO_SHA = re.compile(
    r"^(?P<prefix>PREFIX_CAMPAIGN_KANDELO_SHA\s*=\s*\n\s*\")"
    r"(?P<value>[^\s\"]+\")(?P<suffix>\s*)$",
    flags=re.MULTILINE,
)
TRUST_GENERATION = scalar_pattern(
    r'PACKAGE_GENERATION_WASM32_TAG\s*=\s*"'
)
AUTHORITY_KANDELO_SHA = re.compile(
    r'^(?P<prefix>\s+"kandelo_commit":\s+")'
    r'(?P<value>[^"\s]+)(?P<suffix>",\s*)$',
    flags=re.MULTILINE,
)
AUTHORITY_WORKFLOW_SHA = re.compile(
    r'^(?P<prefix>\s+"reusable_workflow_commit":\s+")'
    r'(?P<value>[^"\s]+)(?P<suffix>",\s*)$',
    flags=re.MULTILINE,
)
CONTROLLER_MAIN_SHA = scalar_pattern(r'CURRENT_MAIN_SHA\s*=\s*"')
CONTROLLER_GENERATION = scalar_pattern(
    r'CURRENT_ROOTFS_GENERATION_TAG\s*=\s*"'
)
CONTROLLER_CALLER_SHA = scalar_pattern(r'CURRENT_CALLER_SHA256\s*=\s*"')


def replace_scalar(
    source: str,
    pattern: re.Pattern[str],
    *,
    allowed: frozenset[str],
    replacement: str,
    label: str,
) -> str:
    matches = tuple(pattern.finditer(source))
    if len(matches) != 1:
        raise RotationError(
            f"{label} must occur exactly once; found {len(matches)}"
        )
    match = matches[0]
    value = match.group("value").removesuffix('"')
    quoted = match.group("value").endswith('"')
    if value not in allowed:
        raise RotationError(f"{label} has unexpected value {value!r}")
    rendered = replacement + ('"' if quoted else "")
    return (
        source[: match.start("value")]
        + rendered
        + source[match.end("value") :]
    )


def replace_repeated_scalars(
    source: str,
    pattern: re.Pattern[str],
    *,
    expected_count: int,
    allowed: frozenset[str],
    replacement: str,
    label: str,
) -> str:
    matches = tuple(pattern.finditer(source))
    if len(matches) != expected_count:
        occurrence = "time" if expected_count == 1 else "times"
        raise RotationError(
            f"{label} must occur exactly {expected_count} {occurrence}; "
            f"found {len(matches)}"
        )
    rendered = source
    # WHY: replace from the end so each earlier match retains the offsets
    # captured from the original bytes. This keeps mixed partial rotations
    # recoverable without broad token replacement.
    for match in reversed(matches):
        value = match.group("value").removesuffix('"')
        quoted = match.group("value").endswith('"')
        if value not in allowed:
            raise RotationError(
                f"{label} has unexpected value {value!r}"
            )
        value_bytes = replacement + ('"' if quoted else "")
        rendered = (
            rendered[: match.start("value")]
            + value_bytes
            + rendered[match.end("value") :]
        )
    return rendered


def validate_kandelo_sha(value: str, label: str) -> None:
    if SHA.fullmatch(value) is None:
        raise RotationError(
            f"{label} must be exactly 40 lowercase hex characters"
        )
    if value.isdigit():
        # WHY: the callers use unquoted SHA scalars. An all-numeric value would
        # be decoded as a YAML integer and would not name the reviewed reusable
        # workflow/string input contract.
        raise RotationError(f"{label} must contain at least one hex letter")


def validate_generation_tag(value: str, label: str) -> None:
    if GENERATION_TAG.fullmatch(value) is None:
        raise RotationError(
            f"{label} must be an exact ABI 42 rootfs-wasm32 content tag"
        )


def validate_caller_sha256(value: str, label: str) -> None:
    if CALLER_SHA256.fullmatch(value) is None:
        raise RotationError(
            f"{label} must be exactly 64 lowercase hex characters"
        )


def reject_duplicate_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RotationError(
                f"prefix campaign authority duplicates {key!r}"
            )
        result[key] = value
    return result


def load_campaign_authority(source: str) -> dict[str, object]:
    try:
        authority = json.loads(
            source,
            object_pairs_hook=reject_duplicate_pairs,
        )
    except (json.JSONDecodeError, UnicodeError) as error:
        raise RotationError(
            "prefix campaign authority is not valid JSON"
        ) from error
    if not isinstance(authority, dict):
        raise RotationError("prefix campaign authority must be an object")
    if source != json.dumps(authority, indent=2) + "\n":
        raise RotationError(
            "prefix campaign authority is not canonical pretty JSON"
        )

    expected_keys = [
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
    ]
    if list(authority) != expected_keys:
        raise RotationError(
            "prefix campaign authority field set or order changed"
        )
    return authority


def validate_armed_authority(
    source: str,
    *,
    allowed_kandelo_shas: frozenset[str],
) -> None:
    authority = load_campaign_authority(source)

    # WHY: this helper rotates executable trust, not campaign data. Requiring
    # the complete armed non-executable record prevents a trust rotation from
    # silently activating a campaign or blessing a changed source contract.
    expected_unchanged = {
        "campaign_release": {
            "repository": "kandelo-dev/homebrew-tap-core",
            "tag": ZERO_CAMPAIGN_TAG,
        },
        "kandelo_repository": "Automattic/kandelo",
        "kind": "kandelo-homebrew-prefix-campaign-caller-authority",
        "package_generations": {
            "rootfs_wasm32": ZERO_GENERATION_TAG,
        },
        "release_tag": "bottles-abi-v42",
        "schema": 2,
        "source_tap_commit": ZERO_SHA,
        "source_tap_name": "kandelo-dev/tap-core",
        "source_tap_repository": "kandelo-dev/homebrew-tap-core",
        "state": "armed",
        "target_source": {
            "manifest_path": "Kandelo/campaigns/prefix-v1/manifest.json",
            "manifest_sha256": (
                "b430d1b934e3b5b07e8f7fcf1b3c1ab6737a82eb6722dad7"
                "b5fdaa81ea949243"
            ),
            "source_root": "Kandelo/campaigns/prefix-v1/source",
            "source_tree_git_oid": (
                "8e825398d9ce414d6148ed2f8eac4e5de4ffb16c"
            ),
            "target_tree_git_oid": (
                "7e314590d18936d0ad3bf8ab42e49d7b4f234892"
            ),
        },
    }
    observed_unchanged = {
        key: value
        for key, value in authority.items()
        if key not in {"kandelo_commit", "reusable_workflow_commit"}
    }
    if observed_unchanged != expected_unchanged:
        raise RotationError(
            "prefix campaign authority changed outside its two Kandelo pins"
        )

    kandelo_sha = authority.get("kandelo_commit")
    workflow_sha = authority.get("reusable_workflow_commit")
    if (
        kandelo_sha not in allowed_kandelo_shas
        or workflow_sha not in allowed_kandelo_shas
        or kandelo_sha != workflow_sha
    ):
        raise RotationError(
            "armed prefix campaign authority has a split or unexpected "
            "Kandelo pin"
        )


def rotate_armed_authority(
    source: str,
    *,
    allowed_kandelo_shas: frozenset[str],
    new_sha: str,
) -> str:
    validate_armed_authority(
        source,
        allowed_kandelo_shas=allowed_kandelo_shas,
    )
    source = replace_scalar(
        source,
        AUTHORITY_KANDELO_SHA,
        allowed=allowed_kandelo_shas,
        replacement=new_sha,
        label="prefix authority Kandelo SHA",
    )
    source = replace_scalar(
        source,
        AUTHORITY_WORKFLOW_SHA,
        allowed=allowed_kandelo_shas,
        replacement=new_sha,
        label="prefix authority reusable workflow SHA",
    )
    validate_armed_authority(
        source,
        allowed_kandelo_shas=frozenset((new_sha,)),
    )
    return source


def read_rotation_files(root: pathlib.Path) -> dict[pathlib.Path, bytes]:
    contents: dict[pathlib.Path, bytes] = {}
    for relative in ROTATION_PATHS:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise RotationError(f"rotation input is not a regular file: {relative}")
        try:
            contents[relative] = path.read_bytes()
            contents[relative].decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise RotationError(
                f"cannot read UTF-8 rotation input {relative}"
            ) from error
    return contents


def rotate_workflow(
    source: str,
    *,
    uses_pattern: re.Pattern[str],
    allowed_kandelo_shas: frozenset[str],
    new_sha: str,
    exact_ref: bool,
    allowed_generation_tags: frozenset[str] | None,
    new_generation: str | None,
    label: str,
) -> str:
    source = replace_scalar(
        source,
        uses_pattern,
        allowed=allowed_kandelo_shas,
        replacement=new_sha,
        label=f"{label} reusable workflow SHA",
    )
    if exact_ref:
        source = replace_scalar(
            source,
            EXACT_KANDELO_REF,
            allowed=allowed_kandelo_shas,
            replacement=new_sha,
            label=f"{label} kandelo-ref",
        )
    else:
        # The expression contains spaces and therefore is intentionally not
        # parsed as a scalar by replace_scalar. Check its exact source and
        # uniqueness instead.
        expected = (
            "      kandelo-ref: "
            "${{ github.event.client_payload.kandelo_ref || 'main' }}"
        )
        lines = source.splitlines()
        if (
            lines.count(expected) != 1
            or sum("kandelo-ref:" in line for line in lines) != 1
        ):
            raise RotationError(
                "dry-run kandelo-ref is not the reviewed event-selected expression"
            )

    generation_matches = tuple(PACKAGE_GENERATION.finditer(source))
    if new_generation is None:
        if generation_matches or "package-generation-" in source:
            raise RotationError(
                f"{label} caller must not select a package generation"
            )
    else:
        if allowed_generation_tags is None:
            raise AssertionError("write caller has no generation authority")
        source = replace_scalar(
            source,
            PACKAGE_GENERATION,
            allowed=allowed_generation_tags,
            replacement=new_generation,
            label=f"{label} package generation",
        )
    return source


def build_rotation(
    root: pathlib.Path,
    *,
    predecessor_kandelo_sha: str,
    predecessor_dry_run_kandelo_sha: str,
    predecessor_first_publication_kandelo_sha: str,
    predecessor_campaign_kandelo_sha: str,
    predecessor_closed_selection_kandelo_sha: str,
    predecessor_generation_tag: str,
    predecessor_caller_sha256: str,
    kandelo_sha: str,
    generation_tag: str,
) -> Rotation:
    validate_kandelo_sha(
        predecessor_kandelo_sha, "predecessor Kandelo SHA"
    )
    validate_kandelo_sha(
        predecessor_dry_run_kandelo_sha,
        "predecessor dry-run Kandelo SHA",
    )
    validate_kandelo_sha(
        predecessor_first_publication_kandelo_sha,
        "predecessor first-publication Kandelo SHA",
    )
    validate_kandelo_sha(
        predecessor_campaign_kandelo_sha,
        "predecessor campaign Kandelo SHA",
    )
    validate_kandelo_sha(
        predecessor_closed_selection_kandelo_sha,
        "predecessor closed-selection Kandelo SHA",
    )
    validate_generation_tag(
        predecessor_generation_tag, "predecessor generation tag"
    )
    validate_caller_sha256(
        predecessor_caller_sha256, "predecessor caller SHA-256"
    )
    validate_kandelo_sha(kandelo_sha, "successor Kandelo SHA")
    validate_generation_tag(generation_tag, "successor generation tag")
    if kandelo_sha == predecessor_kandelo_sha:
        raise RotationError("successor Kandelo SHA still names the predecessor")
    if generation_tag == predecessor_generation_tag:
        raise RotationError(
            "successor generation tag still names the predecessor"
        )

    allowed_write_kandelo_shas = frozenset(
        (predecessor_kandelo_sha, kandelo_sha)
    )
    allowed_dry_run_kandelo_shas = frozenset(
        (predecessor_dry_run_kandelo_sha, kandelo_sha)
    )
    allowed_first_publication_kandelo_shas = frozenset(
        (predecessor_first_publication_kandelo_sha, kandelo_sha)
    )
    allowed_campaign_kandelo_shas = frozenset(
        (predecessor_campaign_kandelo_sha, kandelo_sha)
    )
    allowed_closed_selection_kandelo_shas = frozenset(
        (predecessor_closed_selection_kandelo_sha, kandelo_sha)
    )
    allowed_generation_tags = frozenset(
        (predecessor_generation_tag, generation_tag)
    )

    original = read_rotation_files(root)
    rendered: dict[pathlib.Path, bytes] = dict(original)
    authority_source = original[PREFIX_AUTHORITY_PATH].decode()
    authority = rotate_armed_authority(
        authority_source,
        allowed_kandelo_shas=allowed_campaign_kandelo_shas,
        new_sha=kandelo_sha,
    )

    dry_run = rotate_workflow(
        original[DRY_RUN_PATH].decode(),
        uses_pattern=DRY_RUN_USES,
        allowed_kandelo_shas=allowed_dry_run_kandelo_shas,
        new_sha=kandelo_sha,
        exact_ref=False,
        allowed_generation_tags=None,
        new_generation=None,
        label="dry-run",
    )
    maintenance = rotate_workflow(
        original[MAINTENANCE_PATH].decode(),
        uses_pattern=MAINTENANCE_USES,
        allowed_kandelo_shas=allowed_write_kandelo_shas,
        new_sha=kandelo_sha,
        exact_ref=True,
        allowed_generation_tags=allowed_generation_tags,
        new_generation=generation_tag,
        label="maintenance",
    )
    publish = rotate_workflow(
        original[PUBLISH_PATH].decode(),
        uses_pattern=PUBLISH_USES,
        allowed_kandelo_shas=allowed_write_kandelo_shas,
        new_sha=kandelo_sha,
        exact_ref=True,
        allowed_generation_tags=allowed_generation_tags,
        new_generation=generation_tag,
        label="publish",
    )
    first_publication = rotate_workflow(
        original[FIRST_PUBLICATION_PATH].decode(),
        uses_pattern=FIRST_PUBLICATION_USES,
        allowed_kandelo_shas=(
            allowed_first_publication_kandelo_shas
        ),
        new_sha=kandelo_sha,
        exact_ref=True,
        allowed_generation_tags=None,
        new_generation=None,
        label="first-publication",
    )
    prefix_campaign = replace_repeated_scalars(
        original[PREFIX_CAMPAIGN_PATH].decode(),
        PREFIX_BOTTLE_USES,
        expected_count=3,
        allowed=allowed_campaign_kandelo_shas,
        replacement=kandelo_sha,
        label="prefix-campaign bottle reusable workflow SHA",
    )
    prefix_campaign = replace_repeated_scalars(
        prefix_campaign,
        PREFIX_FIRST_CHILD_USES,
        expected_count=1,
        allowed=allowed_campaign_kandelo_shas,
        replacement=kandelo_sha,
        label="prefix-campaign first-child reusable workflow SHA",
    )
    closed_selection = replace_scalar(
        original[CLOSED_SELECTION_PATH].decode(),
        CLOSED_SELECTION_USES,
        allowed=allowed_closed_selection_kandelo_shas,
        replacement=kandelo_sha,
        label="closed-selection reusable workflow SHA",
    )
    closed_selection = replace_scalar(
        closed_selection,
        EXACT_KANDELO_REF,
        allowed=allowed_closed_selection_kandelo_shas,
        replacement=kandelo_sha,
        label="closed-selection kandelo-ref",
    )
    rendered[DRY_RUN_PATH] = dry_run.encode()
    rendered[MAINTENANCE_PATH] = maintenance.encode()
    rendered[PREFIX_CAMPAIGN_PATH] = prefix_campaign.encode()
    rendered[PUBLISH_PATH] = publish.encode()
    rendered[CLOSED_SELECTION_PATH] = closed_selection.encode()
    rendered[FIRST_PUBLICATION_PATH] = first_publication.encode()
    rendered[PREFIX_AUTHORITY_PATH] = authority.encode()

    trust = original[TRUST_PATH].decode()
    trust = replace_scalar(
        trust,
        TRUST_KANDELO_SHA,
        allowed=allowed_write_kandelo_shas,
        replacement=kandelo_sha,
        label="trust-test Kandelo SHA",
    )
    trust = replace_scalar(
        trust,
        TRUST_DRY_RUN_KANDELO_SHA,
        allowed=allowed_dry_run_kandelo_shas,
        replacement=kandelo_sha,
        label="trust-test dry-run Kandelo SHA",
    )
    trust = replace_scalar(
        trust,
        TRUST_FIRST_PUBLICATION_KANDELO_SHA,
        allowed=allowed_first_publication_kandelo_shas,
        replacement=kandelo_sha,
        label="trust-test first-publication Kandelo SHA",
    )
    trust = replace_scalar(
        trust,
        TRUST_PREFIX_CAMPAIGN_KANDELO_SHA,
        allowed=allowed_campaign_kandelo_shas,
        replacement=kandelo_sha,
        label="trust-test prefix-campaign Kandelo SHA",
    )
    trust = replace_scalar(
        trust,
        TRUST_CLOSED_SELECTION_KANDELO_SHA,
        allowed=allowed_closed_selection_kandelo_shas,
        replacement=kandelo_sha,
        label="trust-test closed-selection Kandelo SHA",
    )
    trust = replace_scalar(
        trust,
        TRUST_GENERATION,
        allowed=allowed_generation_tags,
        replacement=generation_tag,
        label="trust-test package generation",
    )
    rendered[TRUST_PATH] = trust.encode()

    caller_sha256 = hashlib.sha256(rendered[PUBLISH_PATH]).hexdigest()
    original_caller_sha256 = hashlib.sha256(original[PUBLISH_PATH]).hexdigest()
    if original_caller_sha256 not in (
        predecessor_caller_sha256,
        caller_sha256,
    ):
        # WHY: the controller approves the hash of the complete credentialed
        # caller, not only the scalars this helper knows how to replace. Refuse
        # to bless extra jobs, permissions, secrets, or other unreviewed bytes.
        raise RotationError(
            "production caller bytes have SHA-256 "
            f"{original_caller_sha256}, expected predecessor "
            f"{predecessor_caller_sha256} or rendered successor "
            f"{caller_sha256}"
        )
    if caller_sha256 == predecessor_caller_sha256:
        raise RotationError(
            "rendered production caller still names the predecessor digest"
        )

    controller = original[CONTROLLER_PATH].decode()
    controller = replace_scalar(
        controller,
        CONTROLLER_MAIN_SHA,
        allowed=allowed_write_kandelo_shas,
        replacement=kandelo_sha,
        label="rollout-controller Kandelo SHA",
    )
    controller = replace_scalar(
        controller,
        CONTROLLER_GENERATION,
        allowed=allowed_generation_tags,
        replacement=generation_tag,
        label="rollout-controller package generation",
    )
    controller = replace_scalar(
        controller,
        CONTROLLER_CALLER_SHA,
        allowed=frozenset((predecessor_caller_sha256, caller_sha256)),
        replacement=caller_sha256,
        label="rollout-controller caller SHA-256",
    )
    rendered[CONTROLLER_PATH] = controller.encode()

    # WHY: every live slot above is matched uniquely and rewritten as one
    # tuple. Do not globally reject predecessor tokens: the controller may
    # retain a separately named historical authority for ledger recovery.
    if not CALLER_SHA256.fullmatch(caller_sha256):
        raise AssertionError("unreachable invalid SHA-256")

    changed = tuple(
        relative
        for relative in ROTATION_PATHS
        if original[relative] != rendered[relative]
    )
    return Rotation(rendered, changed, caller_sha256)


def atomic_write(path: pathlib.Path, data: bytes) -> None:
    stat = path.stat(follow_symlinks=False)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.rotation-",
        dir=path.parent,
    )
    temporary = pathlib.Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.st_mode & 0o777)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_rotation(root: pathlib.Path, rotation: Rotation) -> None:
    # All inputs and all output contracts are validated before the first write.
    # If the host stops between replacements, a rerun accepts old or new values
    # in each reviewed slot and converges the tuple without broad search/replace.
    for relative in rotation.changed:
        atomic_write(root / relative, rotation.contents[relative])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parent.parent,
        help="tap checkout root (default: repository containing this script)",
    )
    parser.add_argument(
        "--predecessor-kandelo-sha",
        required=True,
        help="exact Kandelo SHA selected by the protected callers now",
    )
    parser.add_argument(
        "--predecessor-dry-run-kandelo-sha",
        required=True,
        help="exact Kandelo SHA selected by the dry-run caller now",
    )
    parser.add_argument(
        "--predecessor-first-publication-kandelo-sha",
        required=True,
        help=(
            "exact Kandelo SHA selected by the first-publication "
            "caller now"
        ),
    )
    parser.add_argument(
        "--predecessor-campaign-kandelo-sha",
        required=True,
        help=(
            "exact Kandelo SHA selected by the campaign authority and "
            "prefix caller now"
        ),
    )
    parser.add_argument(
        "--predecessor-closed-selection-kandelo-sha",
        required=True,
        help=(
            "exact Kandelo SHA selected by the closed-selection "
            "caller now"
        ),
    )
    parser.add_argument(
        "--predecessor-generation-tag",
        required=True,
        help="exact rootfs generation selected by the write callers now",
    )
    parser.add_argument(
        "--predecessor-caller-sha256",
        required=True,
        help="SHA-256 of the current raw production caller bytes",
    )
    parser.add_argument(
        "--kandelo-sha",
        required=True,
        help="exact successor Kandelo main SHA",
    )
    parser.add_argument(
        "--generation-tag",
        required=True,
        help="exact successor rootfs generation tag",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically replace the validated slots; default is preview only",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        rotation = build_rotation(
            root,
            predecessor_kandelo_sha=args.predecessor_kandelo_sha,
            predecessor_dry_run_kandelo_sha=(
                args.predecessor_dry_run_kandelo_sha
            ),
            predecessor_first_publication_kandelo_sha=(
                args.predecessor_first_publication_kandelo_sha
            ),
            predecessor_campaign_kandelo_sha=(
                args.predecessor_campaign_kandelo_sha
            ),
            predecessor_closed_selection_kandelo_sha=(
                args.predecessor_closed_selection_kandelo_sha
            ),
            predecessor_generation_tag=args.predecessor_generation_tag,
            predecessor_caller_sha256=args.predecessor_caller_sha256,
            kandelo_sha=args.kandelo_sha,
            generation_tag=args.generation_tag,
        )
        if args.apply:
            apply_rotation(root, rotation)
            action = "updated"
        else:
            action = "would update"
        print(f"publisher trust rotation {action} {len(rotation.changed)} file(s)")
        for relative in rotation.changed:
            print(f"  {relative}")
        print(f"  caller-sha256={rotation.caller_sha256}")
        if not args.apply:
            print(
                "preview only; rerun with --apply after reviewing "
                "P_M/P_D/P_F/P_A/P_S/P_G/P_C -> M/G/C"
            )
        return 0
    except (OSError, RotationError) as error:
        print(f"rotate-publisher-trust: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
