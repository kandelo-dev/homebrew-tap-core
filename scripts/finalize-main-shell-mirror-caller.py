#!/usr/bin/env python3
"""Bind the protected main-shell mirror caller to merged Kandelo PR #1144.

The checked-in caller template is intentionally inert. This helper previews
the final Kandelo-SHA replacement by default and writes only with --apply.
The bottle catalog and canary authorities are already final literal inputs.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import pathlib
import re
import sys
import tempfile
from collections.abc import Mapping


MPRE_PLACEHOLDER = "__FINAL_KANDELO_MPRE_SHA__"
EXPECTED_TAP_CATALOG_SHA = "6ad0e3dbc60e5572c4288c86919238f71c1bc110"
EXPECTED_CANARY_SHA = "d8bdda662f6d80cf3dcdbe8451edb12bb33bbafc"
SHA = re.compile(r"[0-9a-f]{40}")

CALLER_PATH = pathlib.Path(".github/workflows/publish-main-shell-mirror.yml")
TRUST_PATH = pathlib.Path("Kandelo/test-workflow-trust.rb")
FINALIZATION_PATHS = (CALLER_PATH, TRUST_PATH)


class FinalizationError(RuntimeError):
    """The candidate tree does not match the reviewed caller contract."""


@dataclasses.dataclass(frozen=True)
class Finalization:
    contents: Mapping[pathlib.Path, bytes]
    changed: tuple[pathlib.Path, ...]


def scalar_pattern(prefix: str, *, suffix: str = r"\s*") -> re.Pattern[str]:
    return re.compile(
        rf"^(?P<prefix>{prefix})(?P<value>[^\s\"']+)"
        rf"(?P<suffix>{suffix})$",
        flags=re.MULTILINE,
    )


CALLER_USES = scalar_pattern(
    r"\s+uses:\s+Automattic/kandelo/\.github/workflows/"
    r"reusable-homebrew-main-shell-mirror-publish\.yml@"
)
CALLER_KANDELO = scalar_pattern(r"\s+kandelo-ref:\s+")
CALLER_TAP = scalar_pattern(r"\s+tap-catalog-ref:\s+")
CALLER_CANARY = scalar_pattern(r"\s+canary-ref:\s+")
TRUST_KANDELO = scalar_pattern(
    r'MAIN_SHELL_MIRROR_KANDELO_SHA\s*=\s*"',
    suffix=r'"\s*',
)
TRUST_TAP = scalar_pattern(
    r'MAIN_SHELL_MIRROR_TAP_CATALOG_SHA\s*=\s*"',
    suffix=r'"\s*',
)
TRUST_CANARY = scalar_pattern(
    r'MAIN_SHELL_MIRROR_CANARY_SHA\s*=\s*"',
    suffix=r'"\s*',
)


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
        raise FinalizationError(
            f"{label} must occur exactly once; found {len(matches)}"
        )
    match = matches[0]
    value = match.group("value")
    if value not in allowed:
        raise FinalizationError(f"{label} has unexpected value {value!r}")
    return (
        source[: match.start("value")]
        + replacement
        + source[match.end("value") :]
    )


def scalar_value(
    source: str,
    pattern: re.Pattern[str],
    *,
    label: str,
) -> str:
    matches = tuple(pattern.finditer(source))
    if len(matches) != 1:
        raise FinalizationError(
            f"{label} must occur exactly once; found {len(matches)}"
        )
    return matches[0].group("value")


def read_inputs(root: pathlib.Path) -> dict[pathlib.Path, bytes]:
    contents: dict[pathlib.Path, bytes] = {}
    for relative in FINALIZATION_PATHS:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise FinalizationError(
                f"finalization input is not a regular file: {relative}"
            )
        try:
            data = path.read_bytes()
            data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise FinalizationError(
                f"cannot read UTF-8 finalization input {relative}"
            ) from error
        contents[relative] = data
    return contents


def build_finalization(
    root: pathlib.Path,
    *,
    kandelo_sha: str,
) -> Finalization:
    if SHA.fullmatch(kandelo_sha) is None:
        raise FinalizationError(
            "Kandelo Mpre must be exactly 40 lowercase hex characters"
        )

    original = read_inputs(root)
    rendered: dict[pathlib.Path, bytes] = dict(original)

    caller = original[CALLER_PATH].decode()
    for pattern, placeholder, replacement, label in (
        (CALLER_USES, MPRE_PLACEHOLDER, kandelo_sha, "caller reusable Mpre"),
        (CALLER_KANDELO, MPRE_PLACEHOLDER, kandelo_sha, "caller input Mpre"),
    ):
        caller = replace_scalar(
            caller,
            pattern,
            allowed=frozenset((placeholder, replacement)),
            replacement=replacement,
            label=label,
        )
    rendered[CALLER_PATH] = caller.encode()

    trust = original[TRUST_PATH].decode()
    for pattern, placeholder, replacement, label in (
        (TRUST_KANDELO, MPRE_PLACEHOLDER, kandelo_sha, "trust Mpre"),
    ):
        trust = replace_scalar(
            trust,
            pattern,
            allowed=frozenset((placeholder, replacement)),
            replacement=replacement,
            label=label,
        )
    rendered[TRUST_PATH] = trust.encode()

    # WHY: a partially prepared tuple must never become dispatchable. The
    # workflow is data-only, so literal refs are its complete source authority.
    if MPRE_PLACEHOLDER in caller:
        raise FinalizationError(
            f"{CALLER_PATH} still contains unresolved placeholder "
            f"{MPRE_PLACEHOLDER}"
        )

    if (
        scalar_value(caller, CALLER_USES, label="final caller reusable Mpre")
        != kandelo_sha
        or scalar_value(
            caller, CALLER_KANDELO, label="final caller input Mpre"
        )
        != kandelo_sha
    ):
        raise FinalizationError(
            "final caller must bind the same Mpre in uses and kandelo-ref"
        )
    if (
        scalar_value(caller, CALLER_TAP, label="final caller input TF")
        != EXPECTED_TAP_CATALOG_SHA
        or scalar_value(caller, CALLER_CANARY, label="final caller input C")
        != EXPECTED_CANARY_SHA
        or scalar_value(trust, TRUST_TAP, label="final trust TF")
        != EXPECTED_TAP_CATALOG_SHA
        or scalar_value(trust, TRUST_CANARY, label="final trust C")
        != EXPECTED_CANARY_SHA
    ):
        raise FinalizationError(
            "fixed catalog or canary authority differs from the reviewed tuple"
        )
    forbidden = (
        "github.event.client_payload",
        "secrets:",
        "\n    steps:",
        "\n    env:",
        "\n    run:",
    )
    for token in forbidden:
        if token in caller:
            raise FinalizationError(
                f"caller gained forbidden executable or selected data: {token!r}"
            )

    changed = tuple(
        relative
        for relative in FINALIZATION_PATHS
        if rendered[relative] != original[relative]
    )
    return Finalization(contents=rendered, changed=changed)


def atomic_replace(path: pathlib.Path, contents: bytes) -> None:
    mode = path.stat().st_mode
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_finalization(root: pathlib.Path, finalization: Finalization) -> None:
    # WHY: update the rejecting trust expectation before the executable
    # caller. A crash can therefore leave only an inert placeholder caller;
    # the dispatchable authority is always the last file made ready.
    # Re-running with the same tuple converges because requested values and
    # placeholders are the only accepted predecessors.
    for relative in (TRUST_PATH, CALLER_PATH):
        if relative in finalization.changed:
            atomic_replace(root / relative, finalization.contents[relative])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kandelo-sha", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parent.parent,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        finalization = build_finalization(
            args.root.resolve(),
            kandelo_sha=args.kandelo_sha,
        )
        if args.apply:
            apply_finalization(args.root.resolve(), finalization)
        action = "applied" if args.apply else "preview"
        if finalization.changed:
            paths = ", ".join(str(path) for path in finalization.changed)
            print(f"{action}: {paths}")
        else:
            print(f"{action}: caller already matches the requested Kandelo SHA")
        return 0
    except FinalizationError as error:
        print(f"finalize-main-shell-mirror-caller: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
