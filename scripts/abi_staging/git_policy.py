"""Shared protected Git command policy for ABI staging checkouts."""

from __future__ import annotations

from pathlib import Path


def protected_git_arguments(
    root: Path,
    *arguments: str,
    file_protocol: str,
) -> list[str]:
    """Build a noninteractive Git command for one exact checkout root."""

    if file_protocol not in {"always", "never"}:
        raise ValueError("protected Git file protocol must be always or never")
    exact_root = root.resolve(strict=True)
    return [
        "git",
        "-c",
        f"safe.directory={exact_root}",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        f"protocol.file.allow={file_protocol}",
        "-C",
        str(exact_root),
        *arguments,
    ]
