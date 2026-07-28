#!/usr/bin/env python3
"""Rotate the protected Homebrew callers to one exact Kandelo generation.

The helper is deliberately scoped to the post-#1121 transition prepared in
this worktree. By default it validates and previews the complete transition.
It writes only when --apply is supplied.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import pathlib
import re
import sys
import tempfile
from collections.abc import Mapping


LIVE_KANDELO_SHA = "88d26f4c627a363e01e567574916aff4e00828ee"
B90_KANDELO_SHA = "b90eff73960207b59b7db55c7fb4ed46a4d075c0"
PREDECESSOR_KANDELO_SHAS = frozenset((LIVE_KANDELO_SHA, B90_KANDELO_SHA))
OLD_GENERATION_TAG = (
    "package-generation-rootfs-wasm32-abi-v42-sha256-"
    "adc14c9c0923787e260585b7ddc4517b5b9013f642212e039804f32bf892a5f9"
)
# The prepared worktree stopped before recomputing the caller hash, so its
# controller can contain either the last live caller hash or the mechanically
# derived b90 placeholder hash. No other predecessor is accepted.
OLD_CALLER_SHA256 = frozenset(
    (
        "1d36416c57ba168f0d4b310dfb98c1f1b9a9d17926cb491079e18eba299b1e19",
        "afdbfec7272726c4d987fef41eb129fd8dd1dbefabd6ffaa00de699353dc87ae",
    )
)

SHA = re.compile(r"[0-9a-f]{40}")
GENERATION_TAG = re.compile(
    r"package-generation-rootfs-wasm32-abi-v42-sha256-[0-9a-f]{64}"
)
CALLER_SHA256 = re.compile(r"[0-9a-f]{64}")

DRY_RUN_PATH = pathlib.Path(".github/workflows/dry-run-bottles.yml")
MAINTENANCE_PATH = pathlib.Path(".github/workflows/maintain-bottles.yml")
PUBLISH_PATH = pathlib.Path(".github/workflows/publish-bottles.yml")
TRUST_PATH = pathlib.Path("Kandelo/test-workflow-trust.rb")
CONTROLLER_PATH = pathlib.Path("scripts/abi42-rollout.py")
ROTATION_PATHS = (
    DRY_RUN_PATH,
    MAINTENANCE_PATH,
    PUBLISH_PATH,
    TRUST_PATH,
    CONTROLLER_PATH,
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
EXACT_KANDELO_REF = scalar_pattern(r"\s+kandelo-ref:\s+")
PACKAGE_GENERATION = scalar_pattern(r"\s+package-generation-wasm32:\s+")
TRUST_KANDELO_SHA = scalar_pattern(
    r'CURRENT_KANDELO_WORKFLOW_SHA\s*=\s*"'
)
TRUST_GENERATION = scalar_pattern(
    r'PACKAGE_GENERATION_WASM32_TAG\s*=\s*"'
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
            raise RotationError(f"cannot read UTF-8 rotation input {relative}") from error
    return contents


def rotate_workflow(
    source: str,
    *,
    uses_pattern: re.Pattern[str],
    new_sha: str,
    exact_ref: bool,
    new_generation: str | None,
    label: str,
) -> str:
    source = replace_scalar(
        source,
        uses_pattern,
        allowed=PREDECESSOR_KANDELO_SHAS | frozenset((new_sha,)),
        replacement=new_sha,
        label=f"{label} reusable workflow SHA",
    )
    if exact_ref:
        source = replace_scalar(
            source,
            EXACT_KANDELO_REF,
            allowed=PREDECESSOR_KANDELO_SHAS | frozenset((new_sha,)),
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
            raise RotationError("dry-run caller must not select a package generation")
    else:
        source = replace_scalar(
            source,
            PACKAGE_GENERATION,
            allowed=frozenset((OLD_GENERATION_TAG, new_generation)),
            replacement=new_generation,
            label=f"{label} package generation",
        )
    return source


def build_rotation(
    root: pathlib.Path,
    *,
    kandelo_sha: str,
    generation_tag: str,
) -> Rotation:
    if SHA.fullmatch(kandelo_sha) is None:
        raise RotationError("Kandelo SHA must be exactly 40 lowercase hex characters")
    if kandelo_sha.isdigit():
        # WHY: the existing callers use unquoted SHA scalars. An all-numeric
        # value would be decoded as a YAML integer and would not name the
        # reviewed reusable workflow/string input contract.
        raise RotationError("Kandelo SHA must contain at least one hex letter")
    if kandelo_sha in PREDECESSOR_KANDELO_SHAS:
        raise RotationError("Kandelo SHA still names a predecessor authority")
    if GENERATION_TAG.fullmatch(generation_tag) is None:
        raise RotationError(
            "generation tag must be an exact ABI 42 rootfs-wasm32 content tag"
        )
    if generation_tag == OLD_GENERATION_TAG:
        raise RotationError("generation tag still names the obsolete b90 generation")

    original = read_rotation_files(root)
    rendered: dict[pathlib.Path, bytes] = dict(original)

    dry_run = rotate_workflow(
        original[DRY_RUN_PATH].decode(),
        uses_pattern=DRY_RUN_USES,
        new_sha=kandelo_sha,
        exact_ref=False,
        new_generation=None,
        label="dry-run",
    )
    maintenance = rotate_workflow(
        original[MAINTENANCE_PATH].decode(),
        uses_pattern=MAINTENANCE_USES,
        new_sha=kandelo_sha,
        exact_ref=True,
        new_generation=generation_tag,
        label="maintenance",
    )
    publish = rotate_workflow(
        original[PUBLISH_PATH].decode(),
        uses_pattern=PUBLISH_USES,
        new_sha=kandelo_sha,
        exact_ref=True,
        new_generation=generation_tag,
        label="publish",
    )
    rendered[DRY_RUN_PATH] = dry_run.encode()
    rendered[MAINTENANCE_PATH] = maintenance.encode()
    rendered[PUBLISH_PATH] = publish.encode()

    trust = original[TRUST_PATH].decode()
    trust = replace_scalar(
        trust,
        TRUST_KANDELO_SHA,
        allowed=PREDECESSOR_KANDELO_SHAS | frozenset((kandelo_sha,)),
        replacement=kandelo_sha,
        label="trust-test Kandelo SHA",
    )
    trust = replace_scalar(
        trust,
        TRUST_GENERATION,
        allowed=frozenset((OLD_GENERATION_TAG, generation_tag)),
        replacement=generation_tag,
        label="trust-test package generation",
    )
    rendered[TRUST_PATH] = trust.encode()

    caller_sha256 = hashlib.sha256(rendered[PUBLISH_PATH]).hexdigest()
    controller = original[CONTROLLER_PATH].decode()
    controller = replace_scalar(
        controller,
        CONTROLLER_MAIN_SHA,
        allowed=PREDECESSOR_KANDELO_SHAS | frozenset((kandelo_sha,)),
        replacement=kandelo_sha,
        label="rollout-controller Kandelo SHA",
    )
    controller = replace_scalar(
        controller,
        CONTROLLER_GENERATION,
        allowed=frozenset((OLD_GENERATION_TAG, generation_tag)),
        replacement=generation_tag,
        label="rollout-controller package generation",
    )
    controller = replace_scalar(
        controller,
        CONTROLLER_CALLER_SHA,
        allowed=OLD_CALLER_SHA256 | frozenset((caller_sha256,)),
        replacement=caller_sha256,
        label="rollout-controller caller SHA-256",
    )
    rendered[CONTROLLER_PATH] = controller.encode()

    # WHY: this transition is one authority tuple. Leaving even one old token
    # would create a mixed publisher/consumer/generation contract.
    for relative, data in rendered.items():
        source = data.decode()
        if (
            any(sha in source for sha in PREDECESSOR_KANDELO_SHAS)
            or OLD_GENERATION_TAG in source
        ):
            raise RotationError(
                f"{relative} still contains predecessor publisher authority"
            )
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
    parser.add_argument("--kandelo-sha", required=True)
    parser.add_argument("--generation-tag", required=True)
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
            print("preview only; rerun with --apply after reviewing M, G, and hash")
        return 0
    except (OSError, RotationError) as error:
        print(f"rotate-publisher-trust: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
