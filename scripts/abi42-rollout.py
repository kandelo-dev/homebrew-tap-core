#!/usr/bin/env python3
"""Safely plan or dispatch the one-time Kandelo ABI 42 bottle rollout.

The default command is read-only with respect to GitHub. It fetches tap `main`,
checks finalized sidecars and production runs, and prints what is ready. The
only GitHub write path is the explicit `--dispatch` flag, which always creates
a fresh `repository_dispatch`; ledger-recovery commands write only the locked
private state file, and this program has no workflow-rerun operation.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping, Sequence


REPOSITORY = "Kandelo-dev/homebrew-tap-core"
TAP_NAME = "kandelo-dev/tap-core"
KANDELO_REPOSITORY = "Automattic/kandelo"
WORKFLOW_ID = 315_324_894
WORKFLOW_PATH = ".github/workflows/publish-bottles.yml"
# WHY: this SHA selects trusted publisher code, not package identity. The
# controller separately receives the Kandelo package-consumer SHA so hardened
# workflow changes cannot silently force ABI 42 to consume a different cache.
PUBLISHER_WORKFLOW_SHA = "3545bfd34509a52b68a4620c92e4aae24c60adb0"
ABI42_CONSUMER_SHA = "d3805721b887a19382ef1c96b576fc27badc0951"
# These hashes bind the complete protected caller, including permissions and
# the absence of caller-provided secrets or extra executable jobs. The
# transitional caller selected an incompatible consumer and is therefore
# approved only as evidence for runs proven to have stopped before all writes.
APPROVED_PUBLICATION_WORKFLOWS = {
    "3207ecd35a5cca77fc5bb0e26bee8ab9d354efcb7fef2c1d7aa8b65a8b2bade3": (
        ABI42_CONSUMER_SHA,
        ABI42_CONSUMER_SHA,
        "main",
    ),
    "0bf3328ac4d5c0f3497b071943d875e5d43ef4c37f81b941377d7cefdbde97d8": (
        PUBLISHER_WORKFLOW_SHA,
        ABI42_CONSUMER_SHA,
        "exact",
    ),
}
APPROVED_NO_WRITE_ONLY_WORKFLOWS = {
    "6e425bbaa04a1c0127db59a0cab8365eebfe5f67946b44de935b76b0ec745ada": (
        PUBLISHER_WORKFLOW_SHA,
        PUBLISHER_WORKFLOW_SHA,
        "exact",
    ),
}
EXPECTED_ABI = 42
EXPECTED_RELEASE_TAG = "bottles-abi-v42"
PREPUBLICATION_STAGING_TAG = "pr-1079-staging"
PREPUBLICATION_GENERATION_SHA = "437fde2524ea6ad9c44933f8abbf995a46841009"
MAX_ACTIVE_RUNS = 8
ACTIVE_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")
ABANDONED_DISPATCH_REASON = "cancelled before any external-write job started"
FAILED_RECOVERY_KINDS = frozenset(
    (
        "next-rebuild-after-publication",
        "same-rebuild-without-publication",
        "same-rebuild-before-matrix",
    )
)
EXTERNAL_WRITE_JOB_STAGES = frozenset(
    (
        "upload-bottle",
        "publish-bottle-index",
        "finalize-tap",
        "publish-vfs-release",
    )
)
CREDENTIAL_WRITE_STEPS = {
    "upload-bottle": "Upload validated bottle in isolated ORAS auth state",
    "publish-bottle-index": (
        "Publish the complete Homebrew version index in isolated ORAS auth state"
    ),
    "finalize-tap": (
        "Atomically compose and publish all sidecars under one tap state lock"
    ),
    "publish-vfs-release": (
        "Publish and anonymously read back the immutable VFS release"
    ),
}
BOTTLE_ROOT = "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
REGISTRY_TOKEN_ROOT = "https://ghcr.io/token"
MAX_REGISTRY_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_JOB_LOG_BYTES = 4 * 1024 * 1024
ACCEPTED_REGISTRY_MEDIA_TYPES = frozenset(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

WAVES: tuple[tuple[str, ...], ...] = (
    (
        "asa", "bc", "binutils", "bzip2", "coreutils", "ctags", "dash", "ed",
        "fbdoom", "gawk", "gencat", "getconf", "grep", "gzip", "libcxx",
        "libiconv", "lsof", "modeset", "musl-fts", "ncompress", "netcat",
        "openssl", "pcre2", "perl", "posix-utils-lite", "procps", "sed",
        "sqlite", "unzip", "what", "xz", "zlib", "zstd",
    ),
    (
        "diffutils", "dinit", "erlang", "findutils", "icu", "libcurl",
        "libmagic", "libpng", "libxml2", "libzip", "m4", "make", "ncurses",
        "patch", "pax", "python", "ruby", "tar", "tcl", "wget", "zip",
    ),
    (
        "bash", "curl", "file-formula", "less", "nano", "nethack", "texlive",
        "vim",
    ),
    ("git",),
)

DUAL_ARCH_FORMULAE = frozenset(
    ("libcxx", "musl-fts", "openssl", "sqlite", "zlib", "libcurl", "curl")
)
DUAL_ARCH_ROOTS = frozenset(("libcxx", "musl-fts", "openssl", "sqlite", "zlib"))
DUAL_ARCH_SECOND = frozenset(("libcurl",))
DUAL_ARCH_THIRD = frozenset(("curl",))
FORMULA_ORDER = tuple(formula for wave in WAVES for formula in wave)
FORMULA_LEVEL = {
    formula: level for level, wave in enumerate(WAVES, start=1) for formula in wave
}

# Python's publication-time VFS acceptance uses Dash as its guest shell even
# though Python's Formula runtime dependency list itself contains only zlib.
EXTRA_DEPENDENCIES = {"python": frozenset(("dash",))}

if len(FORMULA_ORDER) != 63 or len(set(FORMULA_ORDER)) != 63:
    raise RuntimeError("the ABI 42 rollout must contain exactly 63 unique Formulae")
if sum(2 if name in DUAL_ARCH_FORMULAE else 1 for name in FORMULA_ORDER) != 70:
    raise RuntimeError("the ABI 42 rollout must contain exactly 70 architecture identities")


class RolloutError(RuntimeError):
    """A condition that makes continuing the rollout unsafe."""


@dataclasses.dataclass(frozen=True)
class FormulaIdentity:
    name: str
    pkg_version: str
    formula_revision: int
    bottle_rebuild: int
    arches: tuple[str, ...]
    bottle_sha256: Mapping[str, str]

    @property
    def top_reference(self) -> str:
        return homebrew_top_reference(self.pkg_version, self.bottle_rebuild)

    def state_value(self) -> dict[str, Any]:
        # Generated bottle hashes change when the finalizer commits. The
        # version/revision/rebuild/arch tuple is the immutable reserved identity.
        return {
            "version": self.pkg_version,
            "formula_revision": self.formula_revision,
            "bottle_rebuild": self.bottle_rebuild,
            "arches": list(self.arches),
        }


@dataclasses.dataclass(frozen=True)
class TapSnapshot:
    sha: str
    metadata: Mapping[str, Any]
    formula_sources: Mapping[str, str]
    formula_sidecars: Mapping[str, Mapping[str, Any] | None]
    identities: Mapping[str, FormulaIdentity]
    dependencies: Mapping[str, frozenset[str]]
    workflow_source: str
    formula_support_tree: str


@dataclasses.dataclass(frozen=True)
class RunInventory:
    count: int
    runs: tuple[Mapping[str, Any], ...]
    formulae: Mapping[int, frozenset[str]]
    unknown_run_ids: tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class FormulaStatus:
    name: str
    state: str
    arches: tuple[str, ...]
    dependencies: tuple[str, ...]
    detail: str


@dataclasses.dataclass(frozen=True)
class SubmittedDispatch:
    formula: str
    arches: tuple[str, ...]
    tap_sha: str
    before_run_ids: frozenset[int]
    recorded_at: str
    submitted_at: str


@dataclasses.dataclass(frozen=True)
class RegistryManifestEvidence:
    exists: bool
    digest: str | None


def _run(
    argv: Sequence[str],
    *,
    cwd: pathlib.Path | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        command = " ".join(argv)
        raise RolloutError(
            f"command failed ({result.returncode}): {command}\n{result.stderr.strip()}"
        )
    return result


class GitTap:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root.resolve()
        inside = self.git("rev-parse", "--is-inside-work-tree").stdout.strip()
        if inside != "true":
            raise RolloutError(f"{self.root} is not a Git worktree")
        remote = self.git("remote", "get-url", "origin").stdout.strip()
        normalized = remote.removesuffix(".git").lower()
        accepted = (
            normalized == "https://github.com/kandelo-dev/homebrew-tap-core"
            or normalized == "git@github.com:kandelo-dev/homebrew-tap-core"
            or normalized == "ssh://git@github.com/kandelo-dev/homebrew-tap-core"
        )
        if not accepted:
            raise RolloutError(f"origin is not {REPOSITORY}: {remote}")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return _run(("git", *args), cwd=self.root, check=check)

    def fetch_main(self) -> str:
        # Fetch changes only the local remote-tracking ref. It never pushes or
        # checks out files in the operator's worktree.
        self.git(
            "fetch",
            "--quiet",
            "--no-tags",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        return self.git("rev-parse", "refs/remotes/origin/main^{commit}").stdout.strip()

    def main_without_fetch(self) -> str:
        return self.git("rev-parse", "refs/remotes/origin/main^{commit}").stdout.strip()

    def show(self, revision: str, path: str) -> str:
        result = self.git("show", f"{revision}:{path}", check=False)
        if result.returncode != 0:
            raise RolloutError(f"{path} is unavailable at tap commit {revision}")
        return result.stdout

    def show_optional(self, revision: str, path: str) -> str | None:
        result = self.git("show", f"{revision}:{path}", check=False)
        return result.stdout if result.returncode == 0 else None

    def formula_names(self, revision: str) -> frozenset[str]:
        output = self.git(
            "ls-tree", "-r", "--name-only", revision, "--", "Formula"
        ).stdout
        names = {
            pathlib.PurePosixPath(line).stem
            for line in output.splitlines()
            if line.startswith("Formula/") and line.endswith(".rb")
        }
        return frozenset(names)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self.git(
            "merge-base", "--is-ancestor", ancestor, descendant, check=False
        )
        if result.returncode not in (0, 1):
            raise RolloutError(
                f"cannot check whether {ancestor} is an ancestor of {descendant}"
            )
        return result.returncode == 0

    def tree_oid(self, revision: str, path: str) -> str:
        result = self.git("rev-parse", f"{revision}:{path}", check=False)
        oid = result.stdout.strip()
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise RolloutError(f"{path} is not a Git tree at tap commit {revision}")
        return oid


class GitHub:
    def __init__(self, repository: str = REPOSITORY) -> None:
        self.repository = repository

    def api_json(self, endpoint: str) -> Any:
        result = _run(("gh", "api", endpoint))
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RolloutError(f"GitHub returned invalid JSON for {endpoint}") from error

    def workflow(self) -> Mapping[str, Any]:
        return self.api_json(
            f"repos/{self.repository}/actions/workflows/{WORKFLOW_ID}"
        )

    def runs(self, status: str | None = None, per_page: int = 100) -> Mapping[str, Any]:
        query = f"?per_page={per_page}"
        if status is not None:
            query += f"&status={status}"
        result = self.api_json(
            f"repos/{self.repository}/actions/workflows/{WORKFLOW_ID}/runs{query}"
        )
        if not isinstance(result, dict) or not isinstance(result.get("workflow_runs"), list):
            raise RolloutError("GitHub workflow-run response has an unexpected shape")
        return result

    def run(self, run_id: int) -> Mapping[str, Any]:
        result = self.api_json(
            f"repos/{self.repository}/actions/runs/{run_id}"
        )
        if not isinstance(result, dict):
            raise RolloutError(f"GitHub run {run_id} has an unexpected shape")
        return result

    def jobs(self, run_id: int) -> tuple[Mapping[str, Any], ...]:
        result = self.api_json(
            f"repos/{self.repository}/actions/runs/{run_id}/jobs?per_page=100"
        )
        if not isinstance(result, dict) or not isinstance(result.get("jobs"), list):
            raise RolloutError(f"GitHub jobs for run {run_id} have an unexpected shape")
        count = result.get("total_count")
        if not isinstance(count, int) or count != len(result["jobs"]):
            raise RolloutError(
                f"GitHub returned an incomplete job matrix for run {run_id}"
            )
        if any(not isinstance(job, dict) for job in result["jobs"]):
            raise RolloutError(f"GitHub returned a malformed job for run {run_id}")
        return tuple(result["jobs"])

    def job_log(self, job_id: int) -> str:
        result = _run(
            (
                "gh",
                "api",
                f"repos/{self.repository}/actions/jobs/{job_id}/logs",
            )
        )
        size = len(result.stdout.encode())
        if size > MAX_JOB_LOG_BYTES:
            raise RolloutError(
                f"GitHub job log {job_id} exceeds the response-size limit"
            )
        return result.stdout

    def dispatch(
        self, formula: str, arches: Sequence[str], tap_sha: str
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", tap_sha):
            raise RolloutError(
                f"dispatch requires an exact lowercase tap commit SHA, got {tap_sha!r}"
            )
        payload: dict[str, Any] = {
            "event_type": "publish-kandelo-bottles",
            "client_payload": {
                "formulae": formula,
                "arches": ",".join(arches),
                # WHY: repository_dispatch loads the caller from the default
                # branch, but bottle source must stay bound to the exact tap
                # snapshot that the controller validated and recorded.
                "tap_sha": tap_sha,
            },
        }
        if formula == "python":
            # WHY: the protected caller maps this one reviewed bit to both
            # required acceptance and its temporary postpublication deferral.
            # No other Formula can independently request either exception.
            payload["client_payload"]["require_vfs_acceptance"] = True
        # repository_dispatch intentionally returns 204 with no run ID. The
        # caller must retain its unresolved marker until acknowledge_dispatch()
        # correlates the new production run.
        _run(
            (
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{self.repository}/dispatches",
                "--input",
                "-",
            ),
            input_text=json.dumps(payload, separators=(",", ":")),
        )


class AnonymousRegistry:
    """Read one public GHCR identity without reusing operator credentials."""

    def __init__(self, opener: Any = urllib.request.urlopen) -> None:
        self.opener = opener

    @staticmethod
    def _read_bounded(response: Any, label: str) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                parsed_length = int(content_length)
            except ValueError as error:
                raise RolloutError(
                    f"{label} has an invalid Content-Length"
                ) from error
            if parsed_length < 0 or parsed_length > MAX_REGISTRY_RESPONSE_BYTES:
                raise RolloutError(f"{label} exceeds the response-size limit")
        try:
            body = response.read(MAX_REGISTRY_RESPONSE_BYTES + 1)
        except OSError as error:
            raise RolloutError(f"cannot read {label}: {error}") from error
        if len(body) > MAX_REGISTRY_RESPONSE_BYTES:
            raise RolloutError(f"{label} exceeds the response-size limit")
        return body

    def _open(self, request: urllib.request.Request, label: str) -> Any:
        try:
            return self.opener(request, timeout=30)
        except urllib.error.HTTPError:
            raise
        except (OSError, urllib.error.URLError) as error:
            raise RolloutError(f"cannot read {label}: {error}") from error

    def manifest(
        self, formula: str, reference: str
    ) -> RegistryManifestEvidence:
        if formula not in FORMULA_ORDER:
            raise RolloutError(f"cannot inspect an unknown Formula: {formula!r}")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}", reference):
            raise RolloutError(f"cannot inspect an invalid OCI reference: {reference!r}")

        scope = f"repository:kandelo-dev/homebrew-tap-core/{formula}:pull"
        token_url = (
            f"{REGISTRY_TOKEN_ROOT}?"
            + urllib.parse.urlencode({"service": "ghcr.io", "scope": scope})
        )
        token_request = urllib.request.Request(
            token_url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with self._open(token_request, "anonymous GHCR token") as response:
                if response.geturl() != token_url or response.getcode() != 200:
                    raise RolloutError(
                        "anonymous GHCR token request was redirected or unsuccessful"
                    )
                token_body = self._read_bounded(response, "anonymous GHCR token")
        except urllib.error.HTTPError as error:
            error.close()
            raise RolloutError(
                f"anonymous GHCR token request returned HTTP {error.code}"
            ) from error
        try:
            token_payload = json.loads(token_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RolloutError("anonymous GHCR token response is invalid JSON") from error
        token = token_payload.get("token") if isinstance(token_payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RolloutError("anonymous GHCR token response lacks a token")

        manifest_url = (
            f"{BOTTLE_ROOT}/{urllib.parse.quote(formula, safe='')}/manifests/"
            f"{urllib.parse.quote(reference, safe='')}"
        )
        manifest_request = urllib.request.Request(
            manifest_url,
            headers={
                "Accept": ", ".join(sorted(ACCEPTED_REGISTRY_MEDIA_TYPES)),
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )
        try:
            with self._open(manifest_request, "public GHCR manifest") as response:
                if response.geturl() != manifest_url or response.getcode() != 200:
                    raise RolloutError(
                        "public GHCR manifest request was redirected or unsuccessful"
                    )
                media_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if media_type not in ACCEPTED_REGISTRY_MEDIA_TYPES:
                    raise RolloutError(
                        f"public GHCR manifest has unexpected media type {media_type!r}"
                    )
                body = self._read_bounded(response, "public GHCR manifest")
                header_digest = response.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError as error:
            code = error.code
            response_url = error.geturl()
            error.close()
            if code == 404 and response_url == manifest_url:
                return RegistryManifestEvidence(exists=False, digest=None)
            raise RolloutError(
                f"public GHCR manifest request returned HTTP {code}"
            ) from error

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RolloutError("public GHCR manifest is invalid JSON") from error
        if (
            not isinstance(payload, dict)
            or payload.get("schemaVersion") != 2
            or payload.get("mediaType") not in ACCEPTED_REGISTRY_MEDIA_TYPES
        ):
            raise RolloutError("public GHCR manifest has an unexpected schema")
        computed_digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
        if header_digest != computed_digest:
            raise RolloutError(
                "public GHCR manifest digest header does not match its exact bytes"
            )
        return RegistryManifestEvidence(exists=True, digest=computed_digest)


def _json_object(text: str, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise RolloutError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise RolloutError(f"{label} is not a JSON object")
    return value


def _single_int(source: str, pattern: str, default: int, label: str) -> int:
    matches = re.findall(pattern, source, flags=re.MULTILINE)
    if not matches:
        return default
    values = {int(value) for value in matches}
    if len(values) != 1:
        raise RolloutError(f"{label} has ambiguous values: {sorted(values)}")
    return values.pop()


def bottle_block(source: str, formula: str) -> str:
    lines = source.splitlines()
    starts = [index for index, line in enumerate(lines) if re.fullmatch(r"  bottle do", line)]
    if len(starts) != 1:
        raise RolloutError(f"Formula/{formula}.rb must contain one bottle block")
    start = starts[0]
    for end in range(start + 1, len(lines)):
        if lines[end] == "  end":
            return "\n".join(lines[start : end + 1])
    raise RolloutError(f"Formula/{formula}.rb has an unterminated bottle block")


def homebrew_pkg_version(base_version: str, formula_revision: int) -> str:
    if not base_version or "\n" in base_version or "\r" in base_version:
        raise RolloutError("Homebrew base version is invalid")
    if (
        isinstance(formula_revision, bool)
        or not isinstance(formula_revision, int)
        or formula_revision < 0
    ):
        raise RolloutError("Homebrew Formula revision is invalid")
    # WHY: Homebrew sidecars and OCI references use PkgVersion, which appends
    # Formula revision to the upstream/base version; using the base alone makes
    # a successful revised Formula look permanently unfinalized.
    return (
        f"{base_version}_{formula_revision}"
        if formula_revision > 0
        else base_version
    )


def previous_formula_base_version(previous_package: Mapping[str, Any]) -> str:
    previous_version = previous_package.get("version")
    previous_revision = previous_package.get("formula_revision")
    if (
        not isinstance(previous_version, str)
        or not previous_version
        or isinstance(previous_revision, bool)
        or not isinstance(previous_revision, int)
        or previous_revision < 0
    ):
        raise RolloutError(
            "previous package cannot provide an inferred Homebrew base version"
        )
    if previous_revision == 0:
        return previous_version
    suffix = f"_{previous_revision}"
    if not previous_version.endswith(suffix) or len(previous_version) == len(suffix):
        raise RolloutError(
            "previous package version does not match its Formula revision"
        )
    return previous_version[: -len(suffix)]


def homebrew_top_reference(pkg_version: str, bottle_rebuild: int) -> str:
    if (
        isinstance(bottle_rebuild, bool)
        or not isinstance(bottle_rebuild, int)
        or bottle_rebuild < 0
    ):
        raise RolloutError("Homebrew bottle rebuild is invalid")
    reference = (
        f"{pkg_version}-{bottle_rebuild}"
        if bottle_rebuild > 0
        else pkg_version
    )
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}", reference):
        raise RolloutError(f"Homebrew top reference is invalid: {reference!r}")
    return reference


def parse_formula_identity(
    formula: str,
    source: str,
    previous_package: Mapping[str, Any] | None,
) -> FormulaIdentity:
    block = bottle_block(source, formula)
    roots = re.findall(r'^\s+root_url\s+"([^"]+)"\s*$', block, flags=re.MULTILINE)
    if roots != [BOTTLE_ROOT]:
        raise RolloutError(
            f"Formula/{formula}.rb bottle root must be exactly {BOTTLE_ROOT}"
        )
    rebuild = _single_int(block, r"^\s+rebuild\s+([0-9]+)\s*$", 0, formula)
    sha_rows = re.findall(
        r'^\s+sha256\s+cellar:\s+[^,]+,\s+'
        r'(wasm32|wasm64)_kandelo:\s+"([0-9a-f]{64})"\s*$',
        block,
        flags=re.MULTILINE,
    )
    hashes = {arch: sha for arch, sha in sha_rows}
    expected_arches = ("wasm32", "wasm64") if formula in DUAL_ARCH_FORMULAE else ("wasm32",)
    if set(hashes) != set(expected_arches) or len(sha_rows) != len(expected_arches):
        raise RolloutError(
            f"Formula/{formula}.rb bottle arches differ from {expected_arches}"
        )

    formula_revision = _single_int(
        source, r"^\s{2}revision\s+([0-9]+)\s*$", 0, f"{formula} revision"
    )
    source_versions = re.findall(
        r'^\s{2}version\s+"([^"]+)"\s*$', source, flags=re.MULTILINE
    )
    if source_versions:
        if len(set(source_versions)) != 1:
            raise RolloutError(f"Formula/{formula}.rb has ambiguous versions")
        base_version = source_versions[0]
    elif isinstance(previous_package, dict):
        base_version = previous_formula_base_version(previous_package)
    else:
        raise RolloutError(
            f"Formula/{formula}.rb needs an explicit version for rollout identity"
        )
    pkg_version = homebrew_pkg_version(base_version, formula_revision)

    if rebuild < 1:
        raise RolloutError(
            f"Formula/{formula}.rb has not reserved a positive ABI 42 rebuild"
        )
    identity = FormulaIdentity(
        name=formula,
        pkg_version=pkg_version,
        formula_revision=formula_revision,
        bottle_rebuild=rebuild,
        arches=expected_arches,
        bottle_sha256=hashes,
    )
    # WHY: Validate the derived OCI name before this identity can be frozen
    # into rollout state or selected for a production dispatch.
    _ = identity.top_reference
    return identity


def formula_contract_sha256(formula: str, source: str) -> str:
    """Hash every Formula byte except finalizer-owned bottle checksums."""
    block = bottle_block(source, formula)
    normalized, substitutions = re.subn(
        r'^(\s+sha256\s+cellar:\s+[^,]+,\s+'
        r'(?:wasm32|wasm64)_kandelo:\s+)"[0-9a-f]{64}"\s*$',
        r'\1"<finalized-sha256>"',
        block,
        flags=re.MULTILINE,
    )
    if substitutions != len(required_arches(formula)):
        raise RolloutError(
            f"Formula/{formula}.rb did not expose every finalizer-owned checksum"
        )
    frozen_source = source.replace(block, normalized, 1)
    return hashlib.sha256(frozen_source.encode()).hexdigest()


def same_tap_dependencies(formula: str, source: str) -> frozenset[str]:
    found = set(
        re.findall(
            r'["\']kandelo-dev/tap-core/([a-z0-9][a-z0-9._-]*)["\']',
            source,
        )
    )
    found.update(EXTRA_DEPENDENCIES.get(formula, ()))
    return frozenset(found)


def load_snapshot(tap: GitTap, sha: str) -> TapSnapshot:
    metadata = _json_object(
        tap.show(sha, "Kandelo/metadata.json"), "Kandelo/metadata.json"
    )
    actual_formulae = tap.formula_names(sha)
    expected_formulae = frozenset(FORMULA_ORDER)
    if actual_formulae != expected_formulae:
        missing = sorted(expected_formulae - actual_formulae)
        extra = sorted(actual_formulae - expected_formulae)
        raise RolloutError(
            f"tap catalog differs from the 63-Formula plan; missing={missing}, extra={extra}"
        )

    sources: dict[str, str] = {}
    sidecars: dict[str, Mapping[str, Any] | None] = {}
    identities: dict[str, FormulaIdentity] = {}
    dependencies: dict[str, frozenset[str]] = {}
    for formula in FORMULA_ORDER:
        source = tap.show(sha, f"Formula/{formula}.rb")
        sidecar_text = tap.show_optional(sha, f"Kandelo/formula/{formula}.json")
        sources[formula] = source
        sidecars[formula] = (
            _json_object(sidecar_text, f"Kandelo/formula/{formula}.json")
            if sidecar_text is not None
            else None
        )
        if sidecars[formula] is not None and sidecars[formula].get("name") != formula:
            raise RolloutError(
                f"Kandelo/formula/{formula}.json belongs to another Formula"
            )
        # WHY: aggregate metadata intentionally contains only packages finalized
        # for the current ABI and therefore shrinks at the first ABI rollover.
        # Each package-owned sidecar remains its last finalized identity, so it
        # is the stable version fallback until this Formula is finalized again.
        # Write-capable continuation still cross-checks this derived identity
        # against the frozen state catalog and cannot recreate state post-cutover.
        identities[formula] = parse_formula_identity(
            formula, source, sidecars[formula]
        )
        dependencies[formula] = same_tap_dependencies(formula, source)

    for formula, deps in dependencies.items():
        unknown = deps - expected_formulae
        if unknown:
            raise RolloutError(f"{formula} refers to unknown same-tap deps: {sorted(unknown)}")
        late = sorted(dep for dep in deps if FORMULA_LEVEL[dep] >= FORMULA_LEVEL[formula])
        if late:
            raise RolloutError(
                f"{formula} dependencies are not in earlier exact waves: {late}"
            )

    return TapSnapshot(
        sha=sha,
        metadata=metadata,
        formula_sources=sources,
        formula_sidecars=sidecars,
        identities=identities,
        dependencies=dependencies,
        workflow_source=tap.show(sha, WORKFLOW_PATH),
        formula_support_tree=tap.tree_oid(sha, "Kandelo/formula_support"),
    )


def validate_workflow_source(
    snapshot: TapSnapshot,
    expected_kandelo_sha: str,
    *,
    expected_publisher_sha: str | None = None,
    allow_legacy_tap_ref: bool = False,
) -> None:
    publisher_sha = expected_publisher_sha or PUBLISHER_WORKFLOW_SHA
    uses = re.findall(
        r"uses:\s+Automattic/kandelo/\.github/workflows/"
        r"reusable-homebrew-bottle-publish\.yml@([0-9a-f]{40})",
        snapshot.workflow_source,
    )
    refs = re.findall(
        r"^\s+kandelo-ref:\s+([0-9a-f]{40})\s*$",
        snapshot.workflow_source,
        flags=re.MULTILINE,
    )
    if uses != [publisher_sha]:
        raise RolloutError(
            "production workflow publisher implementation is not frozen to the "
            f"reviewed SHA (uses={uses}, expected={publisher_sha})"
        )
    if refs != [expected_kandelo_sha]:
        raise RolloutError(
            "production workflow package consumer is not frozen to the requested "
            f"ABI 42 Kandelo SHA (kandelo-ref={refs})"
        )

    # The controller owns only formula/architecture payloads. Freeze the
    # surrounding caller wiring so a tap-side workflow edit cannot redirect or
    # force a publication while retaining the reviewed Kandelo SHA.
    expected_scalars = {
        "kandelo-repository": KANDELO_REPOSITORY,
        "tap-repository": REPOSITORY.lower(),
        "tap-name": TAP_NAME,
        "formulae": "${{ github.event.client_payload.formulae }}",
        "arches": "${{ github.event.client_payload.arches || 'wasm32' }}",
        "force": "${{ github.event.client_payload.force || false }}",
        "dry-run": "false",
        "require-vfs-acceptance": (
            "${{ github.event.client_payload.require_vfs_acceptance || false }}"
        ),
        "prepublication-staging-tag": PREPUBLICATION_STAGING_TAG,
        "prepublication-staging-kandelo-sha": PREPUBLICATION_GENERATION_SHA,
        "defer-vfs-acceptance-until-postpublication": (
            "${{ github.event.client_payload.require_vfs_acceptance || false }}"
        ),
    }
    for key, expected in expected_scalars.items():
        values = re.findall(
            rf"^\s+{re.escape(key)}:\s*(.+?)\s*$",
            snapshot.workflow_source,
            flags=re.MULTILINE,
        )
        if values != [expected]:
            raise RolloutError(
                f"production workflow {key} differs from {expected!r}: {values}"
            )

    tap_refs = re.findall(
        r"^\s+tap-ref:\s*(.+?)\s*$",
        snapshot.workflow_source,
        flags=re.MULTILINE,
    )
    allowed_tap_refs = {"${{ github.event.client_payload.tap_sha }}"}
    if allow_legacy_tap_ref:
        # WHY: bottles finalized before the exact-tap-source migration retain
        # truthful provenance from the older reviewed caller. Trusting that
        # immutable historical caller does not permit a new mutable dispatch.
        allowed_tap_refs.add("main")
    if len(tap_refs) != 1 or tap_refs[0] not in allowed_tap_refs:
        raise RolloutError(
            "production workflow tap-ref is not an allowed immutable or "
            f"historical source selector: {tap_refs}"
        )


def workflow_publisher_sha(snapshot: TapSnapshot) -> str:
    publishers = re.findall(
        r"uses:\s+Automattic/kandelo/\.github/workflows/"
        r"reusable-homebrew-bottle-publish\.yml@([0-9a-f]{40})",
        snapshot.workflow_source,
    )
    if len(publishers) != 1:
        raise RolloutError("production workflow has no unique publisher SHA")
    return publishers[0]


def workflow_sha256(snapshot: TapSnapshot) -> str:
    return hashlib.sha256(snapshot.workflow_source.encode()).hexdigest()


def approved_workflow_authority(
    snapshot: TapSnapshot,
    *,
    allow_no_write_only: bool = False,
) -> tuple[str, str, str]:
    workflow_hash = workflow_sha256(snapshot)
    authority = APPROVED_PUBLICATION_WORKFLOWS.get(workflow_hash)
    if authority is None and allow_no_write_only:
        authority = APPROVED_NO_WRITE_ONLY_WORKFLOWS.get(workflow_hash)
    if authority is None:
        raise RolloutError(
            f"publication workflow hash {workflow_hash} is not approved"
        )
    return authority


def validate_workflow(
    github: GitHub, snapshot: TapSnapshot, expected_kandelo_sha: str
) -> None:
    workflow = github.workflow()
    expected_path = f"/{WORKFLOW_PATH}"
    if workflow.get("id") != WORKFLOW_ID:
        raise RolloutError(f"workflow ID {WORKFLOW_ID} resolved to a different workflow")
    if workflow.get("path") not in (WORKFLOW_PATH, expected_path):
        raise RolloutError(
            f"workflow {WORKFLOW_ID} path is {workflow.get('path')!r}, expected {WORKFLOW_PATH}"
        )
    if workflow.get("state") != "active":
        raise RolloutError(f"production workflow {WORKFLOW_ID} is not active")
    validate_workflow_source(
        snapshot,
        expected_kandelo_sha,
        expected_publisher_sha=PUBLISHER_WORKFLOW_SHA,
    )
    authority = approved_workflow_authority(snapshot)
    if authority != (
        PUBLISHER_WORKFLOW_SHA,
        expected_kandelo_sha,
        "exact",
    ):
        raise RolloutError(
            "active publication workflow authority differs from the requested "
            "publisher, consumer, or exact tap-source contract"
        )


def _packages_by_name(metadata: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    packages: dict[str, Mapping[str, Any]] = {}
    values = metadata.get("packages")
    if not isinstance(values, list):
        raise RolloutError("Kandelo/metadata.json packages is not an array")
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise RolloutError("Kandelo/metadata.json contains a malformed package")
        name = value["name"]
        if name in packages:
            raise RolloutError(f"Kandelo/metadata.json duplicates package {name}")
        packages[name] = value
    return packages


def _bottles_by_arch(
    value: Mapping[str, Any], label: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    bottles = value.get("bottles")
    if not isinstance(bottles, list):
        raise RolloutError(f"{label} bottles is not an array")
    for bottle in bottles:
        if not isinstance(bottle, dict) or not isinstance(bottle.get("arch"), str):
            raise RolloutError(f"{label} contains a malformed bottle")
        arch = bottle["arch"]
        if arch in result:
            raise RolloutError(f"{label} duplicates architecture {arch}")
        result[arch] = bottle
    return result


def finalization_reasons(
    tap: GitTap,
    snapshot: TapSnapshot,
    formula: str,
    arches: Sequence[str],
    expected_kandelo_sha: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    metadata = snapshot.metadata
    identity = snapshot.identities[formula]
    if metadata.get("kandelo_abi") != EXPECTED_ABI:
        reasons.append("aggregate ABI is not 42")
    if metadata.get("release_tag") != EXPECTED_RELEASE_TAG:
        reasons.append("aggregate release tag is not ABI 42")

    package = _packages_by_name(metadata).get(formula)
    sidecar = snapshot.formula_sidecars.get(formula)
    if package is None:
        return tuple(reasons + ["aggregate package is absent"])
    if sidecar is None:
        return tuple(reasons + ["Formula sidecar is absent"])

    expected_fields = {
        "name": formula,
        "version": identity.pkg_version,
        "formula_revision": identity.formula_revision,
        "bottle_rebuild": identity.bottle_rebuild,
    }
    for field, expected in expected_fields.items():
        if package.get(field) != expected:
            reasons.append(f"aggregate {field} differs from {expected!r}")
        if sidecar.get(field) != expected:
            reasons.append(f"sidecar {field} differs from {expected!r}")
    if sidecar.get("kandelo_abi") != EXPECTED_ABI:
        reasons.append("sidecar ABI is not 42")

    aggregate_bottles = _bottles_by_arch(package, f"aggregate {formula}")
    sidecar_bottles = _bottles_by_arch(sidecar, f"sidecar {formula}")
    expected_arches = set(identity.arches)
    for label, bottles in (
        ("aggregate", aggregate_bottles),
        ("sidecar", sidecar_bottles),
    ):
        unexpected = sorted(set(bottles) - expected_arches)
        if unexpected:
            reasons.append(f"{label} has unexpected architectures: {unexpected}")
    if package.get("dependencies") != sidecar.get("dependencies"):
        reasons.append("aggregate and sidecar dependencies differ")
    for arch in arches:
        aggregate = aggregate_bottles.get(arch)
        formula_bottle = sidecar_bottles.get(arch)
        if aggregate is None or formula_bottle is None:
            reasons.append(f"{arch} is missing from aggregate or sidecar")
            continue
        if aggregate != formula_bottle:
            reasons.append(f"aggregate and sidecar {arch} bottle records differ")
        sha = aggregate.get("sha256")
        expected_url = f"{BOTTLE_ROOT}/{formula}/blobs/sha256:{sha}"
        for label, bottle in (("aggregate", aggregate), ("sidecar", formula_bottle)):
            if bottle.get("status", "success") != "success":
                reasons.append(f"{label} {arch} status is not success")
            if bottle.get("kandelo_abi") != EXPECTED_ABI:
                reasons.append(f"{label} {arch} ABI is not 42")
            if bottle.get("bottle_tag") != f"{arch}_kandelo":
                reasons.append(f"{label} {arch} tag is wrong")
            if bottle.get("sha256") != sha or not isinstance(sha, str) or not re.fullmatch(
                r"[0-9a-f]{64}", sha
            ):
                reasons.append(f"{label} {arch} digest differs")
            if bottle.get("url") != expected_url:
                reasons.append(f"{label} {arch} URL is not repository-rooted")
            built_from = bottle.get("built_from")
            if not isinstance(built_from, dict):
                reasons.append(f"{label} {arch} lacks built_from")
                continue
            if (
                built_from.get("kandelo_repository", "").lower()
                != KANDELO_REPOSITORY.lower()
            ):
                reasons.append(
                    f"{label} {arch} was built from another Kandelo repository"
                )
            if built_from.get("tap_repository", "").lower() != REPOSITORY.lower():
                reasons.append(f"{label} {arch} was built from another tap")

        built_from = aggregate.get("built_from")
        if not isinstance(built_from, dict):
            continue
        source_sha = built_from.get("tap_commit")
        archived_formula_sha = built_from.get("formula_sha256")
        if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            reasons.append(f"{arch} source tap SHA is invalid")
            continue
        # WHY: Homebrew records the digest of `.brew/<formula>.rb` in the
        # bottle, and that receipt canonically omits the source bottle block.
        # Source integrity is checked independently below against the frozen
        # Formula contract; treating this receipt digest as the tap file digest
        # makes every valid finalized bottle appear stale.
        if not isinstance(archived_formula_sha, str) or not re.fullmatch(
            r"[0-9a-f]{64}", archived_formula_sha
        ):
            reasons.append(f"{arch} archived Formula digest is invalid")
        try:
            if not tap.is_ancestor(source_sha, snapshot.sha):
                reasons.append(f"{arch} source tap SHA is not on current main")
                continue
            source_formula = tap.show(source_sha, f"Formula/{formula}.rb")
            source_identity = parse_formula_identity(formula, source_formula, package)
            if source_identity.state_value() != identity.state_value():
                reasons.append(f"{arch} source Formula identity differs")
            if formula_contract_sha256(
                formula, source_formula
            ) != formula_contract_sha256(
                formula, snapshot.formula_sources[formula]
            ):
                reasons.append(f"{arch} source Formula recipe differs")
            if (
                tap.tree_oid(source_sha, "Kandelo/formula_support")
                != snapshot.formula_support_tree
            ):
                reasons.append(f"{arch} source Formula support differs")
            source_workflow = tap.show(source_sha, WORKFLOW_PATH)
            source_snapshot = dataclasses.replace(
                snapshot,
                sha=source_sha,
                workflow_source=source_workflow,
            )
            try:
                (
                    source_publisher,
                    source_consumer,
                    source_selector,
                ) = approved_workflow_authority(source_snapshot)
                if source_consumer != expected_kandelo_sha:
                    raise RolloutError(
                        "historical caller selected another package consumer"
                    )
                validate_workflow_source(
                    source_snapshot,
                    expected_kandelo_sha,
                    expected_publisher_sha=source_publisher,
                    allow_legacy_tap_ref=source_selector == "main",
                )
            except RolloutError as error:
                reasons.append(
                    f"{arch} source publication workflow is untrusted: {error}"
                )
            if built_from.get("kandelo_commit") != expected_kandelo_sha:
                reasons.append(f"{arch} was built from another Kandelo SHA")
        except RolloutError as error:
            reasons.append(f"{arch} source provenance cannot be read: {error}")
        if identity.bottle_sha256.get(arch) != sha:
            reasons.append(f"current Formula {arch} checksum differs from sidecars")
    return tuple(dict.fromkeys(reasons))


def run_formulae(jobs: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    formulae: set[str] = set()
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str):
            continue
        for formula in re.findall(
            r"\((" + "|".join(re.escape(value) for value in FORMULA_ORDER) + r")"
            r"(?:,\s+(?:wasm32|wasm64))?\)",
            name,
        ):
            formulae.add(formula)
    return frozenset(formulae)


def active_inventory(github: GitHub) -> RunInventory:
    runs_by_id: dict[int, Mapping[str, Any]] = {}
    total = 0
    for status in ACTIVE_STATUSES:
        response = github.runs(status)
        count = response.get("total_count")
        if not isinstance(count, int) or count < 0:
            raise RolloutError(f"GitHub {status} run count is invalid")
        listed = response["workflow_runs"]
        if count <= 100 and len(listed) != count:
            raise RolloutError(
                f"GitHub {status} reported {count} active runs but returned "
                f"{len(listed)}"
            )
        total += count
        for run in listed:
            if not isinstance(run, dict) or not isinstance(run.get("id"), int):
                raise RolloutError(f"GitHub {status} returned a malformed active run")
            runs_by_id[run["id"]] = run
    # Status filters are disjoint. Deduplicating details protects reporting,
    # while the exact total_count sum is the authoritative capacity count.
    formulae: dict[int, frozenset[str]] = {}
    unknown: list[int] = []
    for run_id in sorted(runs_by_id):
        found = run_formulae(github.jobs(run_id))
        formulae[run_id] = found
        if not found:
            unknown.append(run_id)
    return RunInventory(
        count=total,
        runs=tuple(runs_by_id.values()),
        formulae=formulae,
        unknown_run_ids=tuple(unknown),
    )


def reconcile_recorded_activity(
    github: GitHub,
    inventory: RunInventory,
    state: Mapping[str, Any],
) -> RunInventory:
    """Keep controller-owned runs counted across status-filter transitions."""
    runs_by_id = {
        run["id"]: run
        for run in inventory.runs
        if isinstance(run, dict) and isinstance(run.get("id"), int)
    }
    formulae = dict(inventory.formulae)
    count = inventory.count
    for entry in state.get("dispatches", ()):
        if not isinstance(entry, dict):
            continue
        run_id = entry.get("run_id")
        formula = entry.get("formula")
        if (
            not isinstance(run_id, int)
            or formula not in FORMULA_ORDER
            or run_id in runs_by_id
        ):
            continue
        run = github.run(run_id)
        # Sequential status-filter queries can miss a run while GitHub moves it
        # between requested/queued/waiting/in-progress. The durable ledger
        # closes that race for every run this sole dispatcher creates.
        if run.get("status") != "completed":
            runs_by_id[run_id] = run
            formulae[run_id] = frozenset((formula,))
            count += 1
    return RunInventory(
        count=count,
        runs=tuple(runs_by_id.values()),
        formulae=formulae,
        unknown_run_ids=inventory.unknown_run_ids,
    )


def required_arches(formula: str) -> tuple[str, ...]:
    return ("wasm32", "wasm64") if formula in DUAL_ARCH_FORMULAE else ("wasm32",)


def dependency_arch(dependency: str, target_arch: str) -> str:
    if target_arch == "wasm64" and dependency in DUAL_ARCH_FORMULAE:
        return "wasm64"
    return "wasm32"


def catalog_state(snapshot: TapSnapshot) -> dict[str, Any]:
    return {
        name: {
            **snapshot.identities[name].state_value(),
            # Bottle finalization may replace only the checksum literals.
            # Freezing the rest prevents recipe or dependency edits from
            # silently reusing the rollout's already-reserved identity.
            "formula_contract_sha256": formula_contract_sha256(
                name, snapshot.formula_sources[name]
            ),
            "dependencies": sorted(snapshot.dependencies[name]),
        }
        for name in FORMULA_ORDER
    }


def catalog_identity_value(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    expected_keys = {
        "version",
        "formula_revision",
        "bottle_rebuild",
        "arches",
        "formula_contract_sha256",
        "dependencies",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise RolloutError(f"{label} has an unexpected catalog shape")
    version = value.get("version")
    formula_revision = value.get("formula_revision")
    bottle_rebuild = value.get("bottle_rebuild")
    arches = value.get("arches")
    contract = value.get("formula_contract_sha256")
    dependencies = value.get("dependencies")
    if (
        not isinstance(version, str)
        or not version
        or isinstance(formula_revision, bool)
        or not isinstance(formula_revision, int)
        or formula_revision < 0
        or isinstance(bottle_rebuild, bool)
        or not isinstance(bottle_rebuild, int)
        or bottle_rebuild < 1
        or not isinstance(arches, list)
        or not arches
        or any(arch not in ("wasm32", "wasm64") for arch in arches)
        or len(arches) != len(set(arches))
        or not isinstance(contract, str)
        or not re.fullmatch(r"[0-9a-f]{64}", contract)
        or not isinstance(dependencies, list)
        or any(
            not isinstance(dependency, str) or dependency not in FORMULA_ORDER
            for dependency in dependencies
        )
        or dependencies != sorted(set(dependencies))
    ):
        raise RolloutError(f"{label} contains an invalid catalog identity")
    return {
        "version": version,
        "formula_revision": formula_revision,
        "bottle_rebuild": bottle_rebuild,
        "arches": list(arches),
        "formula_contract_sha256": contract,
        "dependencies": list(dependencies),
    }


def catalog_top_reference(value: Mapping[str, Any], label: str) -> str:
    identity = catalog_identity_value(value, label)
    return homebrew_top_reference(
        identity["version"], identity["bottle_rebuild"]
    )


def validate_credential_write_evidence(
    value: Any,
    *,
    formula: str,
    arches: Sequence[str],
    label: str,
    pre_matrix: bool = False,
) -> None:
    if not isinstance(value, list):
        raise RolloutError(f"{label} is not an array")
    expected_counts = {
        "upload-bottle": 1 if pre_matrix else len(arches),
        "publish-bottle-index": 1,
        "finalize-tap": 1,
        "publish-vfs-release": 1,
    }
    counts = {stage: 0 for stage in expected_counts}
    seen_job_ids: set[int] = set()
    upload_arches: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "arch",
            "job_conclusion",
            "job_id",
            "job_name",
            "stage",
            "step_conclusion",
            "step_name",
        }:
            raise RolloutError(f"{label} contains malformed write evidence")
        stage = item.get("stage")
        job_id = item.get("job_id")
        arch = item.get("arch")
        if (
            stage not in expected_counts
            or isinstance(job_id, bool)
            or not isinstance(job_id, int)
            or job_id <= 0
            or job_id in seen_job_ids
            or not isinstance(item.get("job_name"), str)
            or external_write_job_stage(item["job_name"]) != stage
            or item.get("step_name") != CREDENTIAL_WRITE_STEPS[stage]
            or item.get("step_conclusion") not in ("skipped", "job-skipped")
            or not isinstance(item.get("job_conclusion"), str)
            or not item["job_conclusion"]
        ):
            raise RolloutError(f"{label} contains malformed write evidence")
        if item["step_conclusion"] == "job-skipped":
            if item["job_conclusion"] != "skipped":
                raise RolloutError(f"{label} has inconsistent skipped-job evidence")
        elif item["job_conclusion"] == "skipped":
            raise RolloutError(f"{label} has inconsistent skipped-step evidence")
        if stage == "upload-bottle":
            if pre_matrix:
                if arch is not None:
                    raise RolloutError(
                        f"{label} has unexpected pre-matrix architecture evidence"
                    )
                expected_name = "publish / upload-bottle"
            else:
                if arch not in arches or arch in upload_arches:
                    raise RolloutError(
                        f"{label} has invalid upload architecture evidence"
                    )
                expected_name = f"publish / upload-bottle ({formula}, {arch})"
            if item["job_name"] != expected_name:
                raise RolloutError(f"{label} has the wrong upload job identity")
            if arch is not None:
                upload_arches.add(arch)
        elif arch is not None:
            raise RolloutError(f"{label} has an unexpected non-upload architecture")
        counts[stage] += 1
        seen_job_ids.add(job_id)
    expected_upload_arches = set() if pre_matrix else set(arches)
    if counts != expected_counts or upload_arches != expected_upload_arches:
        raise RolloutError(f"{label} does not cover every credential-bearing stage")


def validate_failed_attempt(
    entry: Any,
    seen_run_ids: set[int],
    *,
    expected_consumer_sha: str,
    trusted_publishers: Mapping[str, str],
) -> None:
    expected_keys = {
        "arches",
        "correlation_evidence",
        "credential_write_evidence",
        "formula",
        "identity_reference",
        "previous_catalog",
        "public_manifest_digest",
        "recorded_failed_at",
        "recovery_kind",
        "replacement_catalog",
        "replacement_tap_sha",
        "run_conclusion",
        "run_id",
        "submitted_at",
        "tap_sha",
    }
    if not isinstance(entry, dict) or set(entry) != expected_keys:
        raise RolloutError("rollout state contains a malformed failed attempt")
    formula = entry.get("formula")
    run_id = entry.get("run_id")
    recovery_kind = entry.get("recovery_kind")
    if (
        formula not in FORMULA_ORDER
        or entry.get("arches") != list(required_arches(formula))
        or isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        or run_id in seen_run_ids
        or recovery_kind not in FAILED_RECOVERY_KINDS
        or entry.get("run_conclusion") != "failure"
        or any(
            not isinstance(entry.get(field), str)
            or not re.fullmatch(r"[0-9a-f]{40}", entry[field])
            for field in ("tap_sha", "replacement_tap_sha")
        )
        or any(
            not isinstance(entry.get(field), str) or not entry[field]
            for field in ("submitted_at", "recorded_failed_at")
        )
    ):
        raise RolloutError("rollout state contains a malformed failed attempt")
    previous = catalog_identity_value(
        entry.get("previous_catalog"), "failed-attempt previous catalog"
    )
    replacement = catalog_identity_value(
        entry.get("replacement_catalog"), "failed-attempt replacement catalog"
    )
    if entry.get("identity_reference") != catalog_top_reference(
        previous, "failed-attempt previous catalog"
    ):
        raise RolloutError("failed-attempt OCI identity does not match its catalog")
    stable_fields = ("version", "formula_revision", "arches", "dependencies")
    if any(previous[field] != replacement[field] for field in stable_fields):
        raise RolloutError("failed-attempt replacement changes a stable identity field")
    digest = entry.get("public_manifest_digest")
    evidence = entry.get("credential_write_evidence")
    correlation = entry.get("correlation_evidence")
    if recovery_kind == "next-rebuild-after-publication":
        if (
            replacement["bottle_rebuild"] != previous["bottle_rebuild"] + 1
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or evidence != []
            or correlation is not None
        ):
            raise RolloutError("failed occupied-identity recovery is malformed")
    elif recovery_kind == "same-rebuild-without-publication":
        if (
            replacement["bottle_rebuild"] != previous["bottle_rebuild"]
            or digest is not None
            or correlation is not None
        ):
            raise RolloutError("failed unpublished-identity recovery is malformed")
        validate_credential_write_evidence(
            evidence,
            formula=formula,
            arches=entry["arches"],
            label="failed-attempt credential-write evidence",
        )
    else:
        expected_correlation_keys = {
            "before_run_ids",
            "intent_recorded_at",
            "logged_arches",
            "logged_formula",
            "logged_kandelo_ref",
            "logged_publisher_sha",
            "logged_tap_ref",
            "plan_job_conclusion",
            "plan_job_id",
            "plan_job_name",
            "plan_log_sha256",
            "plan_token_permissions",
            "recovery_source",
            "run_attempt",
            "run_created_at",
            "run_workflow_id",
            "source_workflow_sha256",
        }
        if (
            replacement["bottle_rebuild"] != previous["bottle_rebuild"]
            or digest is not None
            or not isinstance(correlation, dict)
            or set(correlation) != expected_correlation_keys
            or not isinstance(correlation.get("before_run_ids"), list)
            or correlation["before_run_ids"]
            != sorted(set(correlation["before_run_ids"]))
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in correlation["before_run_ids"]
            )
            or not isinstance(correlation.get("intent_recorded_at"), str)
            or (
                correlation.get("recovery_source") == "submitted-intent"
                and not correlation["intent_recorded_at"]
            )
            or (
                correlation.get("recovery_source") == "explicit-run"
                and (
                    correlation["intent_recorded_at"] != ""
                    or correlation["before_run_ids"] != []
                )
            )
            or correlation.get("recovery_source")
            not in ("submitted-intent", "explicit-run")
            or isinstance(correlation.get("plan_job_id"), bool)
            or not isinstance(correlation.get("plan_job_id"), int)
            or correlation["plan_job_id"] <= 0
            or correlation.get("plan_job_name") != "publish / plan"
            or correlation.get("plan_job_conclusion") != "failure"
            or not isinstance(correlation.get("plan_log_sha256"), str)
            or not re.fullmatch(
                r"[0-9a-f]{64}", correlation["plan_log_sha256"]
            )
            or correlation.get("plan_token_permissions")
            != {"contents": "read", "metadata": "read"}
            or correlation.get("logged_formula") != formula
            or correlation.get("logged_arches") != list(entry["arches"])
            or not isinstance(correlation.get("logged_tap_ref"), str)
            or not correlation["logged_tap_ref"]
            or correlation.get("run_workflow_id") != WORKFLOW_ID
            or not is_first_run_attempt(correlation.get("run_attempt"))
            or not isinstance(correlation.get("run_created_at"), str)
            or not correlation["run_created_at"]
            or not isinstance(
                correlation.get("source_workflow_sha256"), str
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                correlation["source_workflow_sha256"],
            )
            or any(
                not isinstance(correlation.get(field), str)
                or not re.fullmatch(r"[0-9a-f]{40}", correlation[field])
                for field in ("logged_kandelo_ref", "logged_publisher_sha")
            )
        ):
            raise RolloutError("failed pre-matrix recovery is malformed")
        run_created_at = parse_github_time(
            correlation["run_created_at"],
            "failed-attempt run_created_at",
        )
        recovery_source = correlation["recovery_source"]
        if recovery_source == "submitted-intent":
            intent_recorded_at = parse_github_time(
                correlation["intent_recorded_at"],
                "failed-attempt intent_recorded_at",
            )
            if run_created_at < intent_recorded_at:
                raise RolloutError(
                    "failed pre-matrix run predates its submitted intent"
                )
        elif correlation["run_created_at"] != entry["submitted_at"]:
            raise RolloutError(
                "explicit pre-matrix run timestamp differs from its dispatch record"
            )

        workflow_hash = correlation["source_workflow_sha256"]
        authority = APPROVED_PUBLICATION_WORKFLOWS.get(workflow_hash)
        if recovery_source == "submitted-intent":
            if (
                authority is None
                or trusted_publishers.get(workflow_hash) != authority[0]
                or authority[1] != expected_consumer_sha
            ):
                raise RolloutError(
                    "submitted pre-matrix recovery uses an untrusted caller authority"
                )
        else:
            if authority is not None:
                if trusted_publishers.get(workflow_hash) != authority[0]:
                    raise RolloutError(
                        "explicit pre-matrix recovery uses an untrusted caller authority"
                    )
            else:
                authority = APPROVED_NO_WRITE_ONLY_WORKFLOWS.get(workflow_hash)
            if authority is None or authority[2] != "exact":
                raise RolloutError(
                    "explicit pre-matrix recovery uses an unapproved no-write caller"
                )
        expected_tap_ref = (
            "main" if authority[2] == "main" else entry["tap_sha"]
        )
        if (
            correlation["logged_publisher_sha"] != authority[0]
            or correlation["logged_kandelo_ref"] != authority[1]
            or correlation["logged_tap_ref"] != expected_tap_ref
        ):
            raise RolloutError(
                "failed pre-matrix recovery log differs from caller authority"
            )
        validate_credential_write_evidence(
            evidence,
            formula=formula,
            arches=entry["arches"],
            label="failed-attempt pre-matrix write evidence",
            pre_matrix=True,
        )
    seen_run_ids.add(run_id)


def read_state(path: pathlib.Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RolloutError(f"cannot read rollout state {path}") from error
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise RolloutError(f"rollout state {path} has an unsupported schema")
    return state


def write_state(path: pathlib.Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def state_lock(path: pathlib.Path) -> Iterable[None]:
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RolloutError(
                f"another rollout controller holds {lock_path}"
            ) from error
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def initial_state(
    snapshot: TapSnapshot, expected_kandelo_sha: str
) -> dict[str, Any]:
    return {
        "schema": 1,
        "repository": REPOSITORY,
        "workflow_id": WORKFLOW_ID,
        "abi": EXPECTED_ABI,
        "expected_kandelo_sha": expected_kandelo_sha,
        "expected_publisher_sha": workflow_publisher_sha(snapshot),
        "cutover_tap_sha": snapshot.sha,
        "catalog": catalog_state(snapshot),
        "formula_support_tree": snapshot.formula_support_tree,
        "workflow_sha256": hashlib.sha256(snapshot.workflow_source.encode()).hexdigest(),
        "workflow_rotations": [],
        "waves": [list(wave) for wave in WAVES],
        "unresolved_dispatch": None,
        "abandoned_dispatches": [],
        "failed_attempts": [],
        "dispatches": [],
    }


def trusted_workflow_publishers(
    state: Mapping[str, Any],
) -> dict[str, str]:
    # Schema-1 ledgers created before publisher/consumer separation used the
    # consumer SHA for both roles. Recovery upgrades that legacy representation
    # atomically instead of requiring a manual private-ledger edit.
    current_publisher = state.get(
        "expected_publisher_sha",
        state.get("expected_kandelo_sha"),
    )
    current_workflow = state.get("workflow_sha256")
    if (
        not isinstance(current_publisher, str)
        or not re.fullmatch(r"[0-9a-f]{40}", current_publisher)
        or not isinstance(current_workflow, str)
        or not re.fullmatch(r"[0-9a-f]{64}", current_workflow)
    ):
        raise RolloutError("rollout state has a malformed active workflow trust root")

    rotations = state.get("workflow_rotations", [])
    if not isinstance(rotations, list):
        raise RolloutError("rollout state workflow_rotations is not an array")
    trusted: dict[str, str] = {}
    previous_new: tuple[str, str] | None = None
    for entry in rotations:
        expected_keys = {
            "new_publisher_sha",
            "new_workflow_sha256",
            "old_publisher_sha",
            "old_workflow_sha256",
            "recorded_at",
            "tap_sha",
        }
        if not isinstance(entry, dict) or set(entry) != expected_keys:
            raise RolloutError("rollout state contains a malformed workflow rotation")
        old_pair = (
            entry.get("old_workflow_sha256"),
            entry.get("old_publisher_sha"),
        )
        new_pair = (
            entry.get("new_workflow_sha256"),
            entry.get("new_publisher_sha"),
        )
        if (
            any(
                not isinstance(value, str)
                or not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in (old_pair[0], new_pair[0])
            )
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[0-9a-f]{40}", value)
                for value in (old_pair[1], new_pair[1], entry.get("tap_sha"))
            )
            or not isinstance(entry.get("recorded_at"), str)
            or not entry["recorded_at"]
            or old_pair == new_pair
            or (previous_new is not None and old_pair != previous_new)
        ):
            raise RolloutError("rollout state contains a malformed workflow rotation")
        for workflow_hash, publisher_sha in (old_pair, new_pair):
            previous = trusted.setdefault(workflow_hash, publisher_sha)
            if previous != publisher_sha:
                raise RolloutError(
                    "rollout state maps one workflow to multiple publishers"
                )
        previous_new = new_pair
    if previous_new is not None and previous_new != (
        current_workflow,
        current_publisher,
    ):
        raise RolloutError(
            "rollout state workflow rotation chain does not reach its active trust root"
        )
    trusted.setdefault(current_workflow, current_publisher)
    return trusted


def migrate_workflow_trust(
    state: Mapping[str, Any],
    snapshot: TapSnapshot,
    expected_kandelo_sha: str,
) -> dict[str, Any]:
    migrated = copy.deepcopy(state)
    old_publisher = migrated.get(
        "expected_publisher_sha",
        migrated.get("expected_kandelo_sha"),
    )
    old_workflow = migrated.get("workflow_sha256")
    trusted_workflow_publishers(migrated)
    new_workflow = hashlib.sha256(snapshot.workflow_source.encode()).hexdigest()
    new_publisher = workflow_publisher_sha(snapshot)
    if (old_workflow, old_publisher) == (new_workflow, new_publisher):
        migrated.setdefault("workflow_rotations", [])
        migrated["expected_publisher_sha"] = new_publisher
        return migrated

    # WHY: a reviewed caller rotation changes only future publication
    # authority. Preserve the old workflow-to-publisher binding so completed
    # runs and finalized bottle provenance remain auditable after the active
    # caller advances.
    migrated.setdefault("workflow_rotations", []).append(
        {
            "old_publisher_sha": old_publisher,
            "old_workflow_sha256": old_workflow,
            "new_publisher_sha": new_publisher,
            "new_workflow_sha256": new_workflow,
            "tap_sha": snapshot.sha,
            "recorded_at": _utc_now(),
        }
    )
    if migrated.get("expected_kandelo_sha") != expected_kandelo_sha:
        raise RolloutError(
            "workflow rotation cannot change the frozen ABI 42 consumer SHA"
        )
    migrated["expected_publisher_sha"] = new_publisher
    migrated["workflow_sha256"] = new_workflow
    trusted_workflow_publishers(migrated)
    return migrated


def validate_state(
    state: Mapping[str, Any],
    snapshot: TapSnapshot,
    expected_kandelo_sha: str,
) -> None:
    fixed = {
        "repository": REPOSITORY,
        "workflow_id": WORKFLOW_ID,
        "abi": EXPECTED_ABI,
        "expected_kandelo_sha": expected_kandelo_sha,
        "expected_publisher_sha": workflow_publisher_sha(snapshot),
        "catalog": catalog_state(snapshot),
        "formula_support_tree": snapshot.formula_support_tree,
        "workflow_sha256": hashlib.sha256(snapshot.workflow_source.encode()).hexdigest(),
        "waves": [list(wave) for wave in WAVES],
    }
    for field, expected in fixed.items():
        if state.get(field) != expected:
            raise RolloutError(
                f"rollout state {field} differs from current reviewed cutover"
            )
    trusted_publishers = trusted_workflow_publishers(state)
    dispatches = state.get("dispatches")
    if not isinstance(dispatches, list):
        raise RolloutError("rollout state dispatches is not an array")
    seen_formulae: set[str] = set()
    seen_run_ids: set[int] = set()
    for entry in dispatches:
        if not isinstance(entry, dict) or set(entry) != {
            "arches",
            "formula",
            "run_id",
            "submitted_at",
            "tap_sha",
        }:
            raise RolloutError("rollout state contains a malformed dispatch")
        formula = entry.get("formula")
        run_id = entry.get("run_id")
        tap_sha = entry.get("tap_sha")
        if (
            formula not in FORMULA_ORDER
            or entry.get("arches") != list(required_arches(formula))
            or type(run_id) is not int
            or run_id <= 0
            or not isinstance(tap_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", tap_sha)
            or not isinstance(entry.get("submitted_at"), str)
            or not entry["submitted_at"]
        ):
            raise RolloutError("rollout state contains a malformed dispatch")
        if formula in seen_formulae or run_id in seen_run_ids:
            raise RolloutError("rollout state contains a duplicate dispatch")
        seen_formulae.add(formula)
        seen_run_ids.add(run_id)
    abandoned_dispatches = state.get("abandoned_dispatches", [])
    if not isinstance(abandoned_dispatches, list):
        raise RolloutError("rollout state abandoned_dispatches is not an array")
    for entry in abandoned_dispatches:
        if not isinstance(entry, dict) or set(entry) != {
            "abandoned_at",
            "arches",
            "formula",
            "intent_tap_sha",
            "reason",
            "run_id",
            "run_tap_sha",
            "submitted_at",
        }:
            raise RolloutError("rollout state contains a malformed abandoned dispatch")
        formula = entry.get("formula")
        run_id = entry.get("run_id")
        if (
            formula not in FORMULA_ORDER
            or entry.get("arches") != list(required_arches(formula))
            or isinstance(run_id, bool)
            or not isinstance(run_id, int)
            or run_id <= 0
            or run_id in seen_run_ids
            or entry.get("reason") != ABANDONED_DISPATCH_REASON
            or any(
                not isinstance(entry.get(field), str)
                or not re.fullmatch(r"[0-9a-f]{40}", entry[field])
                for field in ("intent_tap_sha", "run_tap_sha")
            )
            or any(
                not isinstance(entry.get(field), str) or not entry[field]
                for field in ("submitted_at", "abandoned_at")
            )
        ):
            raise RolloutError("rollout state contains a malformed abandoned dispatch")
        seen_run_ids.add(run_id)
    failed_attempts = state.get("failed_attempts", [])
    if not isinstance(failed_attempts, list):
        raise RolloutError("rollout state failed_attempts is not an array")
    for entry in failed_attempts:
        validate_failed_attempt(
            entry,
            seen_run_ids,
            expected_consumer_sha=expected_kandelo_sha,
            trusted_publishers=trusted_publishers,
        )


def history_blocks_from_state(
    github: GitHub,
    state: Mapping[str, Any] | None,
    finalized: Mapping[str, bool],
) -> dict[str, tuple[str, str]]:
    if state is None:
        return {}
    blocked: dict[str, tuple[str, str]] = {}
    for entry in state.get("dispatches", ()):
        if not isinstance(entry, dict):
            continue
        formula = entry.get("formula")
        run_id = entry.get("run_id")
        if formula not in finalized or not isinstance(run_id, int) or finalized[formula]:
            continue
        run = github.run(run_id)
        if run.get("status") != "completed":
            blocked[formula] = (
                "active",
                f"controller-recorded run {run_id} has not completed",
            )
        elif run.get("conclusion") == "success":
            # A finalizer commit can become visible just after this invocation
            # fetched main. Never redispatch during that observation window.
            blocked[formula] = (
                "waiting-finalization",
                f"successful run {run_id} is not yet visible in the fetched tap main",
            )
        else:
            blocked[formula] = (
                "blocked-failed",
                f"run {run_id} failed; inspect public partials and reserve a new "
                "identity if required",
            )
    return blocked


def calculate_statuses(
    tap: GitTap,
    snapshot: TapSnapshot,
    expected_kandelo_sha: str,
    inventory: RunInventory,
    history_blocks: Mapping[str, tuple[str, str]],
) -> tuple[FormulaStatus, ...]:
    active_formulae = frozenset(
        formula
        for values in inventory.formulae.values()
        for formula in values
    )
    reasons: dict[str, tuple[str, ...]] = {}
    finalized: dict[str, bool] = {}
    for formula in FORMULA_ORDER:
        found = finalization_reasons(
            tap,
            snapshot,
            formula,
            required_arches(formula),
            expected_kandelo_sha,
        )
        reasons[formula] = found
        finalized[formula] = not found

    statuses: list[FormulaStatus] = []
    for formula in FORMULA_ORDER:
        deps = snapshot.dependencies[formula]
        arches = required_arches(formula)
        if finalized[formula]:
            state, detail = "finalized", "all required ABI 42 identities are on current main"
        elif formula in active_formulae:
            state, detail = "active", "a production publication run is active"
        elif formula in history_blocks:
            state, detail = history_blocks[formula]
        else:
            missing: list[str] = []
            for dep in sorted(deps):
                for arch in arches:
                    dep_arch = dependency_arch(dep, arch)
                    dep_reasons = finalization_reasons(
                        tap,
                        snapshot,
                        dep,
                        (dep_arch,),
                        expected_kandelo_sha,
                    )
                    if dep_reasons:
                        missing.append(f"{dep}/{dep_arch}")
            if missing:
                state = "blocked-dependencies"
                detail = "waiting for " + ", ".join(sorted(set(missing)))
            else:
                state = "ready"
                detail = "all same-tap dependencies are finalized"
        statuses.append(
            FormulaStatus(
                name=formula,
                state=state,
                arches=arches,
                dependencies=tuple(sorted(deps)),
                detail=detail if not reasons[formula] else detail,
            )
        )
    return tuple(statuses)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_github_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise RolloutError(f"{label} is not a timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise RolloutError(f"{label} is not an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RolloutError(f"{label} is not timezone-aware")
    return parsed.astimezone(dt.timezone.utc)


def submitted_dispatch(state: Mapping[str, Any]) -> SubmittedDispatch:
    value = state.get("unresolved_dispatch")
    if not isinstance(value, dict):
        raise RolloutError("rollout state has no unresolved dispatch to recover")
    expected_keys = {
        "arches",
        "before_run_ids",
        "formula",
        "recorded_at",
        "status",
        "submitted_at",
        "tap_sha",
    }
    if set(value) != expected_keys or value.get("status") != "submitted":
        raise RolloutError(
            "unresolved dispatch is not an exact submitted intent; refusing recovery"
        )
    formula = value.get("formula")
    arches = value.get("arches")
    tap_sha = value.get("tap_sha")
    before_run_ids = value.get("before_run_ids")
    if (
        formula not in FORMULA_ORDER
        or arches != list(required_arches(formula))
        or not isinstance(tap_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", tap_sha)
        or not isinstance(before_run_ids, list)
        or any(
            isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0
            for run_id in before_run_ids
        )
        or before_run_ids != sorted(set(before_run_ids))
        or not isinstance(value.get("recorded_at"), str)
        or not value["recorded_at"]
        or not isinstance(value.get("submitted_at"), str)
        or not value["submitted_at"]
    ):
        raise RolloutError("unresolved submitted dispatch is malformed")
    if any(entry.get("formula") == formula for entry in state["dispatches"]):
        raise RolloutError(
            f"unresolved dispatch Formula {formula} is already in the durable ledger"
        )
    return SubmittedDispatch(
        formula=formula,
        arches=tuple(arches),
        tap_sha=tap_sha,
        before_run_ids=frozenset(before_run_ids),
        recorded_at=value["recorded_at"],
        submitted_at=value["submitted_at"],
    )


def build_and_test_matrix(
    jobs: Iterable[Mapping[str, Any]],
) -> tuple[tuple[str, str], ...]:
    matrix: list[tuple[str, str]] = []
    pattern = re.compile(
        r"(?:^| / )build-and-test "
        r"\(([a-z0-9][a-z0-9._-]*),\s+(wasm32|wasm64)\)$"
    )
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str):
            continue
        match = pattern.search(name)
        if match:
            matrix.append((match.group(1), match.group(2)))
    return tuple(sorted(matrix))


def is_first_run_attempt(value: Any) -> bool:
    return type(value) is int and value == 1


def workflow_run_page(
    github: GitHub,
) -> tuple[int, tuple[Mapping[str, Any], ...]]:
    response = github.runs(per_page=100)
    total_count = response.get("total_count")
    runs = response.get("workflow_runs")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
        or not isinstance(runs, list)
        or len(runs) != min(total_count, 100)
    ):
        raise RolloutError("GitHub returned an incomplete workflow run page")
    seen_run_ids: set[int] = set()
    for run in runs:
        if (
            not isinstance(run, dict)
            or isinstance(run.get("id"), bool)
            or not isinstance(run.get("id"), int)
            or run["id"] <= 0
        ):
            raise RolloutError("GitHub returned a malformed workflow run")
        if run["id"] in seen_run_ids:
            raise RolloutError(f"GitHub returned duplicate workflow run {run['id']}")
        seen_run_ids.add(run["id"])
    return total_count, tuple(runs)


def matching_dispatch_run_ids(
    github: GitHub,
    *,
    before_ids: frozenset[int],
    formula: str,
    arches: Sequence[str],
    tap_sha: str,
) -> tuple[int, ...]:
    total_count, runs = workflow_run_page(github)
    returned_run_ids = frozenset(run["id"] for run in runs)
    # WHY: before_run_ids is the durable correlation boundary. If no recorded
    # run remains in the newest page, an older duplicate may be hidden beyond
    # that page; accepting one visible candidate could adopt the wrong request.
    if before_ids:
        if returned_run_ids.isdisjoint(before_ids):
            raise RolloutError(
                "dispatch correlation window exceeded the newest 100 workflow runs"
            )
    elif total_count != len(runs):
        raise RolloutError(
            "dispatch correlation window exceeded the complete workflow history"
        )

    expected_matrix = tuple(sorted((formula, arch) for arch in arches))
    candidates: list[int] = []
    for run in runs:
        run_id = run["id"]
        if run_id in before_ids:
            continue
        if run.get("event") != "repository_dispatch" or run.get("head_sha") != tap_sha:
            continue
        if not is_first_run_attempt(run.get("run_attempt")):
            raise RolloutError(
                f"dispatch run {run_id} is a rerun; only attempt 1 is eligible"
            )
        if build_and_test_matrix(github.jobs(run_id)) == expected_matrix:
            candidates.append(run_id)
    return tuple(candidates)


def external_write_job_stage(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    match = re.fullmatch(
        r"publish / ("
        + "|".join(map(re.escape, sorted(EXTERNAL_WRITE_JOB_STAGES)))
        + r")(?: \([^)]*\))?$",
        name,
    )
    return match.group(1) if match else None


def skipped_credential_write_evidence(
    *,
    formula: str,
    arches: Sequence[str],
    jobs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {
        stage: [] for stage in EXTERNAL_WRITE_JOB_STAGES
    }
    for job in jobs:
        stage = external_write_job_stage(job.get("name"))
        if stage is not None:
            grouped[stage].append(job)
    expected_counts = {
        "upload-bottle": len(arches),
        "publish-bottle-index": 1,
        "finalize-tap": 1,
        "publish-vfs-release": 1,
    }
    actual_counts = {stage: len(grouped[stage]) for stage in expected_counts}
    if actual_counts != expected_counts:
        raise RolloutError(
            "failed run does not contain the exact credential-bearing job set"
        )

    upload_jobs = {
        job.get("name"): job for job in grouped["upload-bottle"]
    }
    expected_upload_names = {
        f"publish / upload-bottle ({formula}, {arch})" for arch in arches
    }
    if set(upload_jobs) != expected_upload_names:
        raise RolloutError(
            "failed run upload jobs do not match the exact Formula architecture matrix"
        )

    ordered: list[tuple[str, str | None, Mapping[str, Any]]] = []
    for arch in arches:
        ordered.append(
            (
                "upload-bottle",
                arch,
                upload_jobs[f"publish / upload-bottle ({formula}, {arch})"],
            )
        )
    for stage in (
        "publish-bottle-index",
        "finalize-tap",
        "publish-vfs-release",
    ):
        ordered.append((stage, None, grouped[stage][0]))

    evidence: list[dict[str, Any]] = []
    seen_job_ids: set[int] = set()
    for stage, arch, job in ordered:
        job_id = job.get("id")
        job_name = job.get("name")
        job_conclusion = job.get("conclusion")
        steps = job.get("steps")
        expected_step = CREDENTIAL_WRITE_STEPS[stage]
        if (
            isinstance(job_id, bool)
            or not isinstance(job_id, int)
            or job_id <= 0
            or job_id in seen_job_ids
            or job.get("status") != "completed"
            or not isinstance(job_name, str)
            or not isinstance(job_conclusion, str)
            or not isinstance(steps, list)
        ):
            raise RolloutError(
                f"failed run {stage} job evidence is incomplete"
            )
        if job_conclusion == "skipped":
            if steps:
                raise RolloutError(
                    f"failed run {stage} skipped job unexpectedly exposes steps"
                )
            step_conclusion = "job-skipped"
        else:
            matched_steps = [
                step for step in steps
                if isinstance(step, dict) and step.get("name") == expected_step
            ]
            if (
                len(matched_steps) != 1
                or matched_steps[0].get("status") != "completed"
                or matched_steps[0].get("conclusion") != "skipped"
            ):
                raise RolloutError(
                    f"failed run {stage} credential-bearing step was not skipped"
                )
            step_conclusion = "skipped"
        evidence.append(
            {
                "stage": stage,
                "arch": arch,
                "job_id": job_id,
                "job_name": job_name,
                "job_conclusion": job_conclusion,
                "step_name": expected_step,
                "step_conclusion": step_conclusion,
            }
        )
        seen_job_ids.add(job_id)
    validate_credential_write_evidence(
        evidence,
        formula=formula,
        arches=arches,
        label="failed-run credential-write evidence",
    )
    return evidence


def skipped_pre_matrix_write_evidence(
    jobs: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
    expected_names = {
        "publish / plan",
        "publish / build-and-test",
        "publish / upload-bottle",
        "publish / publish-bottle-index",
        "publish / verify-bottle",
        "publish / finalize-tap",
        "publish / publish-vfs-release",
    }
    by_name: dict[str, Mapping[str, Any]] = {}
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str) or name not in expected_names or name in by_name:
            raise RolloutError(
                "pre-matrix failed run does not contain the exact unexpanded job set"
            )
        by_name[name] = job
    if set(by_name) != expected_names:
        raise RolloutError(
            "pre-matrix failed run does not contain the exact unexpanded job set"
        )

    plan = by_name["publish / plan"]
    plan_id = plan.get("id")
    if (
        isinstance(plan_id, bool)
        or not isinstance(plan_id, int)
        or plan_id <= 0
        or plan.get("status") != "completed"
        or plan.get("conclusion") != "failure"
        or not isinstance(plan.get("steps"), list)
        or not plan["steps"]
    ):
        raise RolloutError("pre-matrix run lacks an exact completed failed plan job")

    for name, job in by_name.items():
        if name == "publish / plan":
            continue
        if (
            job.get("status") != "completed"
            or job.get("conclusion") != "skipped"
            or job.get("steps") != []
        ):
            raise RolloutError(
                f"pre-matrix run job {name!r} may have executed"
            )

    evidence: list[dict[str, Any]] = []
    for stage in (
        "upload-bottle",
        "publish-bottle-index",
        "finalize-tap",
        "publish-vfs-release",
    ):
        job = by_name[f"publish / {stage}"]
        job_id = job.get("id")
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            raise RolloutError(
                f"pre-matrix run {stage} job lacks an exact job ID"
            )
        evidence.append(
            {
                "stage": stage,
                "arch": None,
                "job_id": job_id,
                "job_name": f"publish / {stage}",
                "job_conclusion": "skipped",
                "step_name": CREDENTIAL_WRITE_STEPS[stage],
                "step_conclusion": "job-skipped",
            }
        )
    return evidence, plan


def plan_log_dispatch_evidence(
    *,
    github: GitHub,
    plan: Mapping[str, Any],
    formula: str | None = None,
    arches: Sequence[str] | None = None,
    allowed_tap_refs: frozenset[str],
) -> dict[str, Any]:
    plan_id = plan.get("id")
    if isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id <= 0:
        raise RolloutError("pre-matrix plan job lacks an exact log identity")
    raw_log = github.job_log(plan_id)
    if not isinstance(raw_log, str):
        raise RolloutError("GitHub returned a malformed pre-matrix plan log")
    log_bytes = raw_log.encode()
    if len(log_bytes) > MAX_JOB_LOG_BYTES:
        raise RolloutError("pre-matrix plan log exceeds the response-size limit")

    messages: list[str] = []
    for raw_line in raw_log.removeprefix("\ufeff").splitlines():
        marker = raw_line.find("Z ")
        if marker < 0:
            continue
        messages.append(raw_line[marker + 2 :])
    uses = [
        match.group(1)
        for message in messages
        if (
            match := re.fullmatch(
                r"Uses: Automattic/kandelo/\.github/workflows/"
                r"reusable-homebrew-bottle-publish\.yml@([0-9a-f]{40})",
                message,
            )
        )
    ]
    starts = [
        index for index, message in enumerate(messages)
        if message == "##[group] Inputs"
    ]
    if len(uses) != 1 or len(starts) != 1:
        raise RolloutError("pre-matrix plan log lacks one exact caller input block")
    try:
        end = messages.index("##[endgroup]", starts[0] + 1)
    except ValueError as error:
        raise RolloutError(
            "pre-matrix plan log has an unterminated caller input block"
        ) from error
    inputs: dict[str, str] = {}
    for message in messages[starts[0] + 1 : end]:
        match = re.fullmatch(r"  ([a-z0-9-]+):(?: (.*))?", message)
        if match is None:
            raise RolloutError(
                "pre-matrix plan log contains a malformed caller input"
            )
        key, value = match.group(1), match.group(2) or ""
        if key in inputs:
            raise RolloutError(
                "pre-matrix plan log duplicates a caller input"
            )
        inputs[key] = value

    permission_starts = [
        index
        for index, message in enumerate(messages)
        if message == "##[group]GITHUB_TOKEN Permissions"
    ]
    if len(permission_starts) != 1:
        raise RolloutError(
            "pre-matrix plan log lacks one exact GITHUB_TOKEN permission block"
        )
    try:
        permission_end = messages.index(
            "##[endgroup]", permission_starts[0] + 1
        )
    except ValueError as error:
        raise RolloutError(
            "pre-matrix plan log has an unterminated GITHUB_TOKEN permission block"
        ) from error
    permissions: dict[str, str] = {}
    for message in messages[permission_starts[0] + 1 : permission_end]:
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*): ([a-z]+)", message)
        if match is None:
            raise RolloutError(
                "pre-matrix plan log contains a malformed GITHUB_TOKEN permission"
            )
        key, value = match.group(1).lower(), match.group(2)
        if key in permissions:
            raise RolloutError(
                "pre-matrix plan log duplicates a GITHUB_TOKEN permission"
            )
        permissions[key] = value
    # WHY: skipped downstream jobs prove their credential-bearing steps did not
    # run, while this independent plan-job proof closes the remaining write
    # surface. A caller with broader token authority is not eligible for the
    # exceptional same-identity pre-matrix recovery path.
    if permissions != {"contents": "read", "metadata": "read"}:
        raise RolloutError(
            "pre-matrix plan job did not have the exact read-only token permissions"
        )

    required = {
        "kandelo-repository": KANDELO_REPOSITORY,
        "tap-repository": REPOSITORY.lower(),
        "tap-name": TAP_NAME,
        "force": "false",
        "dry-run": "false",
    }
    for key, expected in required.items():
        if inputs.get(key) != expected:
            raise RolloutError(
                f"pre-matrix plan log {key} differs from {expected!r}"
            )
    logged_formula = inputs.get("formulae")
    if logged_formula not in FORMULA_ORDER:
        raise RolloutError("pre-matrix plan log has an unknown Formula")
    logged_arches = list(required_arches(logged_formula))
    if inputs.get("arches") != ",".join(logged_arches):
        raise RolloutError(
            "pre-matrix plan log arches differ from the Formula contract"
        )
    if formula is not None and logged_formula != formula:
        raise RolloutError(
            f"pre-matrix plan log formulae differs from {formula!r}"
        )
    if arches is not None and logged_arches != list(arches):
        raise RolloutError(
            f"pre-matrix plan log arches differ from {','.join(arches)!r}"
        )
    tap_ref = inputs.get("tap-ref")
    kandelo_ref = inputs.get("kandelo-ref")
    if (
        tap_ref not in allowed_tap_refs
        or not isinstance(kandelo_ref, str)
        or not re.fullmatch(r"[0-9a-f]{40}", kandelo_ref)
    ):
        raise RolloutError("pre-matrix plan log has an untrusted source reference")
    return {
        "plan_log_sha256": hashlib.sha256(log_bytes).hexdigest(),
        "plan_token_permissions": permissions,
        "logged_arches": logged_arches,
        "logged_formula": logged_formula,
        "logged_kandelo_ref": kandelo_ref,
        "logged_publisher_sha": uses[0],
        "logged_tap_ref": tap_ref,
    }


def require_last_green_formula_checksums(
    snapshot: TapSnapshot, formula: str
) -> None:
    identity = snapshot.identities[formula]
    sidecar = snapshot.formula_sidecars.get(formula)
    if not isinstance(sidecar, dict):
        raise RolloutError(
            f"{formula} has no last-green sidecar for a retry reservation"
        )
    sidecar_rebuild = sidecar.get("bottle_rebuild")
    if (
        sidecar.get("name") != formula
        or sidecar.get("version") != identity.pkg_version
        or sidecar.get("formula_revision") != identity.formula_revision
        or isinstance(sidecar_rebuild, bool)
        or not isinstance(sidecar_rebuild, int)
        or sidecar_rebuild < 0
        or sidecar_rebuild >= identity.bottle_rebuild
    ):
        raise RolloutError(
            f"{formula} last-green sidecar does not precede the retry identity"
        )
    bottles = _bottles_by_arch(sidecar, f"last-green {formula}")
    if set(bottles) != set(identity.arches):
        raise RolloutError(
            f"{formula} last-green sidecar does not cover every retry architecture"
        )
    for arch in identity.arches:
        bottle = bottles[arch]
        digest = bottle.get("sha256")
        if (
            bottle.get("status", "success") != "success"
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or identity.bottle_sha256.get(arch) != digest
        ):
            raise RolloutError(
                f"Formula/{formula}.rb no longer retains the last-green {arch} checksum"
            )


def source_with_rebuild(
    source: str, formula: str, replacement_rebuild: int
) -> str:
    block = bottle_block(source, formula)
    replaced, count = re.subn(
        r"^(\s+rebuild\s+)[0-9]+(\s*)$",
        rf"\g<1>{replacement_rebuild}\g<2>",
        block,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RolloutError(
            f"Formula/{formula}.rb does not have one explicit rebuild reservation"
        )
    return source.replace(block, replaced, 1)


def correlate_pre_matrix_failed_intent(
    *,
    github: GitHub,
    intent: SubmittedDispatch,
    run_id: int,
) -> tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...], dict[str, Any]]:
    total_count, runs = workflow_run_page(github)
    returned_run_ids = frozenset(run["id"] for run in runs)
    if intent.before_run_ids:
        if returned_run_ids.isdisjoint(intent.before_run_ids):
            raise RolloutError(
                "dispatch correlation window exceeded the newest 100 workflow runs"
            )
    elif total_count != len(runs):
        raise RolloutError(
            "dispatch correlation window exceeded the complete workflow history"
        )

    intent_recorded_at = parse_github_time(
        intent.recorded_at, "unresolved intent recorded_at"
    )
    expected_matrix = tuple(
        sorted((intent.formula, arch) for arch in intent.arches)
    )
    candidates: list[
        tuple[
            Mapping[str, Any],
            tuple[Mapping[str, Any], ...],
            Mapping[str, Any],
            dict[str, Any],
        ]
    ] = []
    competing_matrix_run_ids: list[int] = []
    for run in runs:
        if (
            run["id"] in intent.before_run_ids
            or run.get("event") != "repository_dispatch"
            or run.get("head_sha") != intent.tap_sha
        ):
            continue
        if run.get("workflow_id") != WORKFLOW_ID:
            raise RolloutError(
                f"post-intent run {run['id']} belongs to another workflow"
            )
        if not is_first_run_attempt(run.get("run_attempt")):
            raise RolloutError(
                f"post-intent run {run['id']} is a rerun; "
                "only attempt 1 is eligible"
            )
        created_at = parse_github_time(
            run.get("created_at"), f"run {run['id']} created_at"
        )
        if created_at < intent_recorded_at:
            continue
        jobs = github.jobs(run["id"])
        matrix = build_and_test_matrix(jobs)
        if matrix:
            if matrix == expected_matrix:
                competing_matrix_run_ids.append(run["id"])
                continue
            matrix_formulae = {formula for formula, _arch in matrix}
            if len(matrix_formulae) != 1:
                raise RolloutError(
                    f"post-intent run {run['id']} has an ambiguous Formula matrix"
                )
            matrix_formula = next(iter(matrix_formulae))
            if (
                matrix_formula not in FORMULA_ORDER
                or matrix
                != tuple(
                    sorted(
                        (matrix_formula, arch)
                        for arch in required_arches(matrix_formula)
                    )
                )
            ):
                raise RolloutError(
                    f"post-intent run {run['id']} has a partial Formula matrix"
                )
            continue
        # WHY: a same-head run whose matrix never expanded cannot be dismissed
        # as unrelated until its immutable caller log positively identifies a
        # different Formula. Missing jobs or unreadable logs therefore make the
        # correlation ambiguous and fail the whole recovery.
        _write_evidence, plan = skipped_pre_matrix_write_evidence(jobs)
        logged = plan_log_dispatch_evidence(
            github=github,
            plan=plan,
            allowed_tap_refs=frozenset(("main", intent.tap_sha)),
        )
        if (
            logged["logged_formula"] != intent.formula
            or logged["logged_arches"] != list(intent.arches)
        ):
            continue
        candidates.append((run, jobs, plan, logged))
    matching_ids = sorted(
        [
            *(candidate[0]["id"] for candidate in candidates),
            *competing_matrix_run_ids,
        ]
    )
    if (
        matching_ids != [run_id]
        or len(candidates) != 1
        or candidates[0][0]["id"] != run_id
    ):
        raise RolloutError(
            "pre-matrix recovery requires the explicit sole post-intent run "
            "with the recorded Formula inputs; found "
            f"{matching_ids}"
        )
    run, jobs, plan, logged = candidates[0]
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        raise RolloutError(
            f"run {run_id} is not an exact completed pre-matrix failure"
        )
    return (
        run,
        jobs,
        {
            "before_run_ids": sorted(intent.before_run_ids),
            "intent_recorded_at": intent.recorded_at,
            "plan_job_id": plan["id"],
            "plan_job_name": plan["name"],
            "plan_job_conclusion": plan["conclusion"],
            "recovery_source": "submitted-intent",
            "run_attempt": run["run_attempt"],
            "run_created_at": run["created_at"],
            "run_workflow_id": run["workflow_id"],
            **logged,
        },
    )


def correlate_explicit_pre_matrix_failed_run(
    *,
    tap: GitTap,
    github: GitHub,
    current: TapSnapshot,
    formula: str,
    run_id: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if formula not in FORMULA_ORDER:
        raise RolloutError(f"cannot adopt an unknown Formula {formula!r}")
    run = github.run(run_id)
    run_tap_sha = run.get("head_sha")
    submitted_at = run.get("created_at")
    if (
        run.get("id") != run_id
        or run.get("workflow_id") != WORKFLOW_ID
        or not is_first_run_attempt(run.get("run_attempt"))
        or run.get("event") != "repository_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
        or not isinstance(run_tap_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", run_tap_sha)
        or not tap.is_ancestor(run_tap_sha, current.sha)
        or not isinstance(submitted_at, str)
        or not submitted_at
    ):
        raise RolloutError(
            f"run {run_id} is not an exact completed failed publication on tap main"
        )
    parse_github_time(submitted_at, f"run {run_id} created_at")
    jobs = github.jobs(run_id)
    if build_and_test_matrix(jobs):
        raise RolloutError(
            f"explicit run {run_id} is not an unexpanded pre-matrix failure"
        )
    _write_evidence, plan = skipped_pre_matrix_write_evidence(jobs)
    logged = plan_log_dispatch_evidence(
        github=github,
        plan=plan,
        formula=formula,
        arches=required_arches(formula),
        allowed_tap_refs=frozenset((run_tap_sha,)),
    )
    return (
        {
            "formula": formula,
            "arches": list(required_arches(formula)),
            "tap_sha": run_tap_sha,
            "run_id": run_id,
            "submitted_at": submitted_at,
        },
        {
            "before_run_ids": [],
            "intent_recorded_at": "",
            "plan_job_id": plan["id"],
            "plan_job_name": plan["name"],
            "plan_job_conclusion": plan["conclusion"],
            "recovery_source": "explicit-run",
            "run_attempt": run["run_attempt"],
            "run_created_at": submitted_at,
            "run_workflow_id": run["workflow_id"],
            **logged,
        },
    )


def prepare_failed_dispatch_recovery(
    *,
    tap: GitTap,
    github: GitHub,
    registry: Any,
    state: Mapping[str, Any],
    current: TapSnapshot,
    expected_kandelo_sha: str,
    run_id: int,
    pre_matrix_correlation: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any], tuple[str, int, str, str]]:
    matches = [
        (index, entry)
        for index, entry in enumerate(state.get("dispatches", ()))
        if (
            isinstance(entry, dict)
            and type(entry.get("run_id")) is int
            and entry["run_id"] == run_id
        )
    ]
    if len(matches) != 1:
        raise RolloutError(
            f"failed recovery requires one controller-recorded run {run_id}"
        )
    dispatch_index, dispatch = matches[0]
    formula = dispatch.get("formula")
    if (
        set(dispatch) != {
            "arches",
            "formula",
            "run_id",
            "submitted_at",
            "tap_sha",
        }
        or dispatch["run_id"] != run_id
        or formula not in FORMULA_ORDER
        or dispatch.get("arches") != list(required_arches(formula))
        or not isinstance(dispatch.get("tap_sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", dispatch["tap_sha"])
        or not isinstance(dispatch.get("submitted_at"), str)
        or not dispatch["submitted_at"]
    ):
        raise RolloutError(
            f"controller-recorded run {run_id} has a malformed dispatch identity"
        )

    if not tap.is_ancestor(dispatch["tap_sha"], current.sha):
        raise RolloutError(
            f"controller-recorded run {run_id} is not on current protected main"
        )
    source = load_snapshot(tap, dispatch["tap_sha"])
    source_workflow_hash = workflow_sha256(source)
    explicit_recovery = (
        pre_matrix_correlation is not None
        and pre_matrix_correlation.get("recovery_source") == "explicit-run"
    )
    (
        source_publisher,
        source_consumer,
        source_selector,
    ) = approved_workflow_authority(
        source,
        allow_no_write_only=explicit_recovery,
    )
    trusted_publishers = trusted_workflow_publishers(state)
    if explicit_recovery:
        # WHY: an explicitly adopted run may document a caller configuration
        # that was corrected before this ledger migration. It can be retained
        # only because the exact plan log and skipped-job set prove that caller
        # never reached a write path; it is not added to trusted workflow roots.
        if (
            source_workflow_hash in APPROVED_PUBLICATION_WORKFLOWS
            and trusted_publishers.get(source_workflow_hash) != source_publisher
        ):
            raise RolloutError(
                f"explicit run {run_id} uses an untrusted publication workflow"
            )
        if source_selector != "exact":
            raise RolloutError(
                f"explicit run {run_id} does not use an exact tap source"
            )
    else:
        if (
            source_workflow_hash not in APPROVED_PUBLICATION_WORKFLOWS
            or trusted_publishers.get(source_workflow_hash) != source_publisher
            or source_consumer != state.get("expected_kandelo_sha")
        ):
            raise RolloutError(
                f"controller-recorded run {run_id} uses an untrusted historical workflow"
            )
    validate_workflow_source(
        source,
        source_consumer,
        expected_publisher_sha=source_publisher,
        allow_legacy_tap_ref=source_selector == "main",
    )
    correlation_evidence = (
        copy.deepcopy(pre_matrix_correlation)
        if pre_matrix_correlation is not None
        else None
    )
    if correlation_evidence is not None:
        expected_logged_tap_ref = (
            "main" if source_selector == "main" else dispatch["tap_sha"]
        )
        if (
            correlation_evidence.get("logged_publisher_sha")
            != source_publisher
            or correlation_evidence.get("logged_kandelo_ref")
            != source_consumer
            or correlation_evidence.get("logged_tap_ref")
            != expected_logged_tap_ref
        ):
            raise RolloutError(
                f"run {run_id} plan log differs from its approved caller authority"
            )
        correlation_evidence["source_workflow_sha256"] = source_workflow_hash
    previous_catalog = state.get("catalog", {}).get(formula)
    source_catalog = catalog_state(source)[formula]
    if previous_catalog != source_catalog:
        raise RolloutError(
            f"controller-recorded run {run_id} source differs from its frozen catalog"
        )
    if (
        state.get("formula_support_tree") != source.formula_support_tree
    ):
        raise RolloutError(
            f"controller-recorded run {run_id} source differs from its frozen support"
        )

    run = github.run(run_id)
    if (
        run.get("id") != run_id
        or run.get("workflow_id") != WORKFLOW_ID
        # WHY: GitHub's jobs endpoint defaults to only the latest rerun
        # attempt. Restricting identity recovery to attempt 1 ensures the jobs
        # being proved skipped cover the run's complete execution history.
        or not is_first_run_attempt(run.get("run_attempt"))
        or run.get("event") != "repository_dispatch"
        or run.get("head_sha") != dispatch["tap_sha"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
        or (
            correlation_evidence is not None
            and run.get("created_at")
            != correlation_evidence.get("run_created_at")
        )
    ):
        raise RolloutError(
            f"run {run_id} is not the exact completed failed publication"
        )
    jobs = github.jobs(run_id)
    expected_matrix = tuple(
        sorted((formula, arch) for arch in required_arches(formula))
    )
    if (
        pre_matrix_correlation is None
        and build_and_test_matrix(jobs) != expected_matrix
    ):
        raise RolloutError(
            f"run {run_id} does not contain the exact Formula architecture matrix"
        )
    if pre_matrix_correlation is not None:
        if build_and_test_matrix(jobs):
            raise RolloutError(
                f"run {run_id} unexpectedly expanded a Formula matrix"
            )
        skipped_pre_matrix_write_evidence(jobs)

    old_identity = source.identities[formula]
    identity_reference = old_identity.top_reference
    manifest = registry.manifest(formula, identity_reference)
    if not isinstance(manifest, RegistryManifestEvidence):
        raise RolloutError("anonymous registry returned malformed identity evidence")
    current_catalog = catalog_state(current)[formula]
    current_identity = current.identities[formula]
    require_last_green_formula_checksums(current, formula)

    previous = catalog_identity_value(
        previous_catalog, f"frozen {formula} catalog"
    )
    replacement = catalog_identity_value(
        current_catalog, f"current {formula} catalog"
    )
    stable_fields = ("version", "formula_revision", "arches", "dependencies")
    if any(previous[field] != replacement[field] for field in stable_fields):
        raise RolloutError(
            f"current {formula} reservation changes a stable identity field"
        )

    if pre_matrix_correlation is not None:
        if manifest.exists or manifest.digest is not None:
            raise RolloutError(
                f"pre-matrix failed {identity_reference} must be anonymously absent"
            )
        if current_identity.state_value() != old_identity.state_value():
            raise RolloutError(
                f"absent public {identity_reference} must retain its exact rebuild"
            )
        write_evidence, _plan = skipped_pre_matrix_write_evidence(jobs)
        recovery_kind = "same-rebuild-before-matrix"
        public_digest = None
    elif manifest.exists:
        if (
            not isinstance(manifest.digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.digest)
        ):
            raise RolloutError(
                f"occupied public {formula} identity lacks an exact manifest digest"
            )
        if current_identity.bottle_rebuild != old_identity.bottle_rebuild + 1:
            raise RolloutError(
                f"public {identity_reference} is occupied; reserve rebuild "
                f"{old_identity.bottle_rebuild + 1} before recovery"
            )
        # WHY: an occupied tag needs only a new immutable name. Any simultaneous
        # recipe edit would make the reason for the new bytes ambiguous, so the
        # reservation is deliberately restricted to its one rebuild line.
        if source_with_rebuild(
            current.formula_sources[formula],
            formula,
            old_identity.bottle_rebuild,
        ) != source.formula_sources[formula]:
            raise RolloutError(
                f"Formula/{formula}.rb changes more than the rebuild reservation"
            )
        recovery_kind = "next-rebuild-after-publication"
        write_evidence: list[dict[str, Any]] = []
        public_digest: str | None = manifest.digest
    else:
        if manifest.digest is not None:
            raise RolloutError(
                f"absent public {formula} identity unexpectedly has a digest"
            )
        if current_identity.state_value() != old_identity.state_value():
            raise RolloutError(
                f"absent public {identity_reference} must retain its exact rebuild"
            )
        # WHY: absence alone is not proof that no credential-bearing path ran.
        # Require the exact GitHub job and step evidence before allowing the
        # same immutable identity to be used by a fresh publication attempt.
        write_evidence = skipped_credential_write_evidence(
            formula=formula,
            arches=required_arches(formula),
            jobs=jobs,
        )
        recovery_kind = "same-rebuild-without-publication"
        public_digest = None

    failed_attempt = {
        "formula": formula,
        "arches": list(required_arches(formula)),
        "tap_sha": dispatch["tap_sha"],
        "run_id": run_id,
        "submitted_at": dispatch["submitted_at"],
        "recorded_failed_at": _utc_now(),
        "run_conclusion": "failure",
        "recovery_kind": recovery_kind,
        "identity_reference": identity_reference,
        "public_manifest_digest": public_digest,
        "replacement_tap_sha": current.sha,
        "previous_catalog": copy.deepcopy(previous_catalog),
        "replacement_catalog": copy.deepcopy(current_catalog),
        "credential_write_evidence": write_evidence,
        "correlation_evidence": correlation_evidence,
    }
    return (
        dispatch_index,
        failed_attempt,
        (formula, run_id, recovery_kind, identity_reference),
    )


def recover_failed_dispatches(
    *,
    tap: GitTap,
    github: GitHub,
    registry: Any,
    expected_kandelo_sha: str,
    state_path: pathlib.Path,
    run_ids: Sequence[int],
    adopt_failed_runs: Sequence[tuple[str, int]] = (),
    no_fetch: bool,
) -> tuple[tuple[str, int, str, str], ...]:
    adopted_run_ids = [run_id for _formula, run_id in adopt_failed_runs]
    all_run_ids = [*run_ids, *adopted_run_ids]
    if (
        not all_run_ids
        or any(
            isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0
            for run_id in all_run_ids
        )
        or len(all_run_ids) != len(set(all_run_ids))
        or any(formula not in FORMULA_ORDER for formula, _run_id in adopt_failed_runs)
    ):
        raise RolloutError(
            "failed recovery requires distinct positive run IDs and known adopted Formulae"
        )
    state = read_state(state_path)
    if state is None:
        raise RolloutError(f"rollout state {state_path} does not exist")
    if not isinstance(state.get("catalog"), dict):
        raise RolloutError("rollout state catalog is not an object")

    current_sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
    current = load_snapshot(tap, current_sha)
    validate_workflow(github, current, expected_kandelo_sha)
    working_state = copy.deepcopy(state)
    pre_matrix_correlations: dict[int, Mapping[str, Any]] = {}
    if working_state.get("unresolved_dispatch") is not None:
        intent = submitted_dispatch(working_state)
        matching_requested: list[int] = []
        for requested_run_id in run_ids:
            try:
                _run_value, _jobs, correlation = correlate_pre_matrix_failed_intent(
                    github=github,
                    intent=intent,
                    run_id=requested_run_id,
                )
            except RolloutError:
                continue
            matching_requested.append(requested_run_id)
            pre_matrix_correlations[requested_run_id] = correlation
        if len(matching_requested) != 1:
            raise RolloutError(
                "failed recovery with an unresolved intent requires its one exact "
                "pre-matrix failed run in the same batch"
            )
        intent_run_id = matching_requested[0]
        working_state["dispatches"].append(
            {
                "formula": intent.formula,
                "arches": list(intent.arches),
                "tap_sha": intent.tap_sha,
                "run_id": intent_run_id,
                "submitted_at": intent.submitted_at,
            }
        )
        working_state["unresolved_dispatch"] = None

    for formula, adopted_run_id in adopt_failed_runs:
        dispatch, correlation = correlate_explicit_pre_matrix_failed_run(
            tap=tap,
            github=github,
            current=current,
            formula=formula,
            run_id=adopted_run_id,
        )
        if any(
            entry.get("run_id") == adopted_run_id
            for collection in (
                working_state.get("dispatches", ()),
                working_state.get("failed_attempts", ()),
                working_state.get("abandoned_dispatches", ()),
            )
            for entry in collection
            if isinstance(entry, dict)
        ):
            raise RolloutError(
                f"explicit run {adopted_run_id} is already present in the ledger"
            )
        working_state["dispatches"].append(dispatch)
        pre_matrix_correlations[adopted_run_id] = correlation

    prepared = [
        prepare_failed_dispatch_recovery(
            tap=tap,
            github=github,
            registry=registry,
            state=working_state,
            current=current,
            expected_kandelo_sha=expected_kandelo_sha,
            run_id=run_id,
            pre_matrix_correlation=pre_matrix_correlations.get(run_id),
        )
        for run_id in all_run_ids
    ]
    formulas = [result[2][0] for result in prepared]
    for formula in set(formulas):
        attempts = [
            failed_attempt
            for _dispatch_index, failed_attempt, _result in prepared
            if failed_attempt["formula"] == formula
        ]
        if len(attempts) <= 1:
            continue
        if (
            any(
                attempt["recovery_kind"] == "next-rebuild-after-publication"
                or attempt["public_manifest_digest"] is not None
                for attempt in attempts
            )
            or len(
                {
                    json.dumps(attempt["previous_catalog"], sort_keys=True)
                    for attempt in attempts
                }
            )
            != 1
            or len(
                {
                    json.dumps(attempt["replacement_catalog"], sort_keys=True)
                    for attempt in attempts
                }
            )
            != 1
        ):
            raise RolloutError(
                f"failed recovery cannot safely retire multiple {formula} attempts"
            )

    recovered_state = migrate_workflow_trust(
        working_state,
        current,
        expected_kandelo_sha,
    )
    recovered_run_ids = set(all_run_ids)
    recovered_state["dispatches"] = [
        entry
        for entry in recovered_state["dispatches"]
        if entry.get("run_id") not in recovered_run_ids
    ]
    for _dispatch_index, failed_attempt, _result in prepared:
        recovered_state.setdefault("failed_attempts", []).append(failed_attempt)
        formula = failed_attempt["formula"]
        recovered_state["catalog"][formula] = copy.deepcopy(
            failed_attempt["replacement_catalog"]
        )

    # WHY: Formula reservation, frozen catalog, and attempt history become
    # authoritative together. A single private-file replacement means a crash
    # cannot expose any member of a batched reservation as retryable without
    # retaining every occupied or unpublished identity decision in that batch.
    validate_state(recovered_state, current, expected_kandelo_sha)
    write_state(state_path, recovered_state)
    return tuple(result for _index, _attempt, result in prepared)


def recover_failed_dispatch(
    *,
    tap: GitTap,
    github: GitHub,
    registry: Any,
    expected_kandelo_sha: str,
    state_path: pathlib.Path,
    run_id: int,
    no_fetch: bool,
) -> tuple[str, int, str, str]:
    """Compatibility wrapper for one atomic failed-attempt transition."""
    return recover_failed_dispatches(
        tap=tap,
        github=github,
        registry=registry,
        expected_kandelo_sha=expected_kandelo_sha,
        state_path=state_path,
        run_ids=(run_id,),
        no_fetch=no_fetch,
    )[0]


def abandon_submitted_dispatch(
    *,
    tap: GitTap,
    github: GitHub,
    expected_kandelo_sha: str,
    state_path: pathlib.Path,
    run_id: int,
    no_fetch: bool,
) -> tuple[str, int]:
    state = read_state(state_path)
    if state is None:
        raise RolloutError(f"rollout state {state_path} does not exist")
    sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
    snapshot = load_snapshot(tap, sha)
    validate_workflow(github, snapshot, expected_kandelo_sha)
    validate_state(state, snapshot, expected_kandelo_sha)
    intent = submitted_dispatch(state)
    if not tap.is_ancestor(intent.tap_sha, snapshot.sha):
        raise RolloutError(
            "unresolved dispatch tap SHA is not an ancestor of current tap main"
        )

    total_count, runs = workflow_run_page(github)
    returned_run_ids = frozenset(run["id"] for run in runs)
    if intent.before_run_ids:
        if returned_run_ids.isdisjoint(intent.before_run_ids):
            raise RolloutError(
                "dispatch correlation window exceeded the newest 100 workflow runs"
            )
    elif total_count != len(runs):
        raise RolloutError(
            "dispatch correlation window exceeded the complete workflow history"
        )

    expected_matrix = tuple(
        sorted((intent.formula, arch) for arch in intent.arches)
    )
    candidates: list[tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]] = []
    for run in runs:
        if run["id"] in intent.before_run_ids:
            continue
        jobs = github.jobs(run["id"])
        if (
            run.get("event") == "repository_dispatch"
            and build_and_test_matrix(jobs) == expected_matrix
        ):
            if not is_first_run_attempt(run.get("run_attempt")):
                raise RolloutError(
                    f"abandonment run {run['id']} is a rerun; "
                    "only attempt 1 is eligible"
                )
            candidates.append((run, jobs))
    candidate_ids = sorted(run["id"] for run, _jobs in candidates)
    if candidate_ids != [run_id]:
        raise RolloutError(
            "abandonment requires the explicit sole post-intent Formula run; "
            f"found {candidate_ids}"
        )
    run, jobs = candidates[0]
    if run.get("status") != "completed" or run.get("conclusion") != "cancelled":
        raise RolloutError(
            f"run {run_id} is not a completed cancelled publication"
        )
    run_tap_sha = run.get("head_sha")
    if (
        not isinstance(run_tap_sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", run_tap_sha)
        or not tap.is_ancestor(intent.tap_sha, run_tap_sha)
        or not tap.is_ancestor(run_tap_sha, snapshot.sha)
    ):
        raise RolloutError(
            f"run {run_id} is not on the protected-main history after the intent"
        )

    write_jobs: dict[str, list[Mapping[str, Any]]] = {
        stage: [] for stage in EXTERNAL_WRITE_JOB_STAGES
    }
    for job in jobs:
        stage = external_write_job_stage(job.get("name"))
        if stage is not None:
            write_jobs[stage].append(job)
    if any(not entries for entries in write_jobs.values()):
        missing = sorted(stage for stage, entries in write_jobs.items() if not entries)
        raise RolloutError(
            f"run {run_id} lacks expected external-write jobs: {', '.join(missing)}"
        )
    for stage, entries in write_jobs.items():
        for job in entries:
            if (
                job.get("status") != "completed"
                or job.get("conclusion") not in ("cancelled", "skipped")
                or job.get("steps") != []
            ):
                raise RolloutError(
                    f"run {run_id} {stage} may have started; refusing abandonment"
                )

    # WHY: a cancelled request whose external-write jobs never started is safe
    # to retry, but deleting its marker would erase the only durable evidence
    # that the original HTTP request was accepted. Preserve that evidence in
    # the same private ledger before releasing the Formula for a fresh request.
    abandoned_state = copy.deepcopy(state)
    abandoned_state.setdefault("abandoned_dispatches", []).append(
        {
            "formula": intent.formula,
            "arches": list(intent.arches),
            "intent_tap_sha": intent.tap_sha,
            "run_tap_sha": run_tap_sha,
            "run_id": run_id,
            "submitted_at": intent.submitted_at,
            "abandoned_at": _utc_now(),
            "reason": ABANDONED_DISPATCH_REASON,
        }
    )
    abandoned_state["unresolved_dispatch"] = None
    validate_state(abandoned_state, snapshot, expected_kandelo_sha)
    write_state(state_path, abandoned_state)
    return intent.formula, run_id


def acknowledge_dispatch(
    github: GitHub,
    *,
    before_ids: frozenset[int],
    formula: str,
    arches: Sequence[str],
    tap_sha: str,
    timeout_seconds: int,
    poll_seconds: float,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        candidates = matching_dispatch_run_ids(
            github,
            before_ids=before_ids,
            formula=formula,
            arches=arches,
            tap_sha=tap_sha,
        )
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RolloutError(
                f"dispatch for {formula} matched multiple new runs: {sorted(candidates)}"
            )
        time.sleep(poll_seconds)
    raise RolloutError(
        f"no unambiguous run ID appeared for {formula} within {timeout_seconds}s; "
        "the unresolved state marker was retained"
    )


def recover_submitted_dispatch(
    *,
    tap: GitTap,
    github: GitHub,
    expected_kandelo_sha: str,
    state_path: pathlib.Path,
    no_fetch: bool,
) -> tuple[str, int]:
    state = read_state(state_path)
    if state is None:
        raise RolloutError(f"rollout state {state_path} does not exist")
    sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
    snapshot = load_snapshot(tap, sha)
    validate_workflow(github, snapshot, expected_kandelo_sha)
    validate_state(state, snapshot, expected_kandelo_sha)
    intent = submitted_dispatch(state)
    if not tap.is_ancestor(intent.tap_sha, snapshot.sha):
        raise RolloutError(
            "unresolved dispatch tap SHA is not an ancestor of current tap main"
        )

    candidates = matching_dispatch_run_ids(
        github,
        before_ids=intent.before_run_ids,
        formula=intent.formula,
        arches=intent.arches,
        tap_sha=intent.tap_sha,
    )
    if len(candidates) != 1:
        raise RolloutError(
            f"recovery found {len(candidates)} exact new runs for "
            f"{intent.formula}; the unresolved marker was retained"
        )
    run_id = candidates[0]

    # WHY: The dispatch request may have succeeded before acknowledgement
    # timed out. Record the correlated run and clear the marker in one file
    # replacement; recovery never sends another repository_dispatch.
    recovered_state = copy.deepcopy(state)
    recovered_state["dispatches"].append(
        {
            "formula": intent.formula,
            "arches": list(intent.arches),
            "tap_sha": intent.tap_sha,
            "run_id": run_id,
            "submitted_at": intent.submitted_at,
        }
    )
    recovered_state["unresolved_dispatch"] = None
    validate_state(recovered_state, snapshot, expected_kandelo_sha)
    write_state(state_path, recovered_state)
    return intent.formula, run_id


def ready_dispatch_candidates(
    statuses: Iterable[FormulaStatus],
    allowed_formulae: frozenset[str] | None,
) -> tuple[FormulaStatus, ...]:
    return tuple(
        status
        for status in statuses
        if status.state == "ready"
        and (
            allowed_formulae is None
            or status.name in allowed_formulae
        )
    )


def dispatch_ready(
    *,
    tap: GitTap,
    github: GitHub,
    expected_kandelo_sha: str,
    state_path: pathlib.Path,
    no_fetch: bool,
    maximum: int,
    timeout_seconds: int,
    poll_seconds: float,
    allowed_formulae: frozenset[str] | None = None,
) -> int:
    state = read_state(state_path)
    dispatched = 0
    while dispatched < maximum:
        sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
        snapshot = load_snapshot(tap, sha)
        validate_workflow(github, snapshot, expected_kandelo_sha)
        if state is None:
            aggregate_abi = snapshot.metadata.get("kandelo_abi")
            # WHY: after the first ABI 42 finalization, the original ledger is
            # the only durable record of prior dispatches and failed partials.
            # Sidecars are sufficient for read-only inspection, but must never
            # be used to reconstruct and resume a write-capable rollout.
            if (
                isinstance(aggregate_abi, bool)
                or not isinstance(aggregate_abi, int)
                or aggregate_abi >= EXPECTED_ABI
            ):
                raise RolloutError(
                    "cannot initialize a replacement rollout state after the "
                    f"ABI {EXPECTED_ABI} cutover; restore the original ledger"
                )
            state = initial_state(snapshot, expected_kandelo_sha)
            write_state(state_path, state)
        validate_state(state, snapshot, expected_kandelo_sha)
        if state.get("unresolved_dispatch") is not None:
            raise RolloutError(
                f"{state_path} contains an unresolved dispatch; inspect it before continuing"
            )

        inventory = reconcile_recorded_activity(
            github, active_inventory(github), state
        )
        if inventory.count >= MAX_ACTIVE_RUNS:
            return dispatched
        if inventory.unknown_run_ids:
            raise RolloutError(
                "active production runs have not exposed their Formula matrix yet: "
                + ", ".join(map(str, inventory.unknown_run_ids))
            )

        finalized = {
            formula: not finalization_reasons(
                tap,
                snapshot,
                formula,
                required_arches(formula),
                expected_kandelo_sha,
            )
            for formula in FORMULA_ORDER
        }
        history_blocks = history_blocks_from_state(github, state, finalized)
        statuses = calculate_statuses(
            tap, snapshot, expected_kandelo_sha, inventory, history_blocks
        )
        ready = ready_dispatch_candidates(statuses, allowed_formulae)
        if not ready:
            return dispatched

        selected = ready[0]
        # Refresh both main and the active-run count immediately before the
        # write. A moving tap or newly queued run invalidates the plan instead
        # of consuming an unchecked ninth slot.
        latest_sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
        if latest_sha != snapshot.sha:
            continue
        latest_inventory = reconcile_recorded_activity(
            github, active_inventory(github), state
        )
        if latest_inventory.count >= MAX_ACTIVE_RUNS:
            return dispatched
        if latest_inventory.unknown_run_ids:
            raise RolloutError(
                "active production runs have not exposed their Formula matrix yet: "
                + ", ".join(map(str, latest_inventory.unknown_run_ids))
            )
        if selected.name in {
            formula
            for values in latest_inventory.formulae.values()
            for formula in values
        }:
            continue
        _recent_total, recent_runs = workflow_run_page(github)
        before_ids = frozenset(
            run["id"]
            for run in recent_runs
        )
        intent = {
            "formula": selected.name,
            "arches": list(selected.arches),
            "tap_sha": snapshot.sha,
            "recorded_at": _utc_now(),
            "before_run_ids": sorted(before_ids),
            "status": "intent-recorded",
        }
        state["unresolved_dispatch"] = intent
        write_state(state_path, state)
        try:
            github.dispatch(selected.name, selected.arches, snapshot.sha)
            intent["status"] = "submitted"
            intent["submitted_at"] = _utc_now()
            write_state(state_path, state)
            run_id = acknowledge_dispatch(
                github,
                before_ids=before_ids,
                formula=selected.name,
                arches=selected.arches,
                tap_sha=snapshot.sha,
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
            )
        except BaseException:
            # A failed HTTP response or interrupted process can still follow an
            # accepted GitHub request. Retaining the marker prevents a blind
            # duplicate publication on the next invocation.
            write_state(state_path, state)
            raise
        state["dispatches"].append(
            {
                "formula": selected.name,
                "arches": list(selected.arches),
                "tap_sha": snapshot.sha,
                "run_id": run_id,
                "submitted_at": intent["submitted_at"],
            }
        )
        state["unresolved_dispatch"] = None
        write_state(state_path, state)
        print(
            f"dispatched {selected.name} ({','.join(selected.arches)}) as run {run_id}",
            flush=True,
        )
        dispatched += 1
    return dispatched


def render_status(
    snapshot: TapSnapshot,
    inventory: RunInventory,
    statuses: Sequence[FormulaStatus],
    *,
    as_json: bool,
) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "tap_sha": snapshot.sha,
                    "workflow_id": WORKFLOW_ID,
                    "active_run_count": inventory.count,
                    "available_slots": max(0, MAX_ACTIVE_RUNS - inventory.count),
                    "unknown_active_run_ids": list(inventory.unknown_run_ids),
                    "formulae": [dataclasses.asdict(status) for status in statuses],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status.state] = counts.get(status.state, 0) + 1
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    print(f"tap main: {snapshot.sha}")
    print(
        f"production runs: {inventory.count}/{MAX_ACTIVE_RUNS} active; "
        f"{max(0, MAX_ACTIVE_RUNS - inventory.count)} slots available"
    )
    if inventory.unknown_run_ids:
        print(
            "dispatch blocked until Formula matrices appear for active runs: "
            + ", ".join(map(str, inventory.unknown_run_ids))
        )
    print(f"catalog: {summary}")
    for status in statuses:
        if status.state in (
            "ready",
            "active",
            "waiting-finalization",
            "blocked-failed",
        ):
            print(
                f"{status.state:20} {status.name:18} "
                f"{','.join(status.arches):13} {status.detail}"
            )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tap-root",
        type=pathlib.Path,
        required=True,
        help="a local Kandelo-dev/homebrew-tap-core Git worktree",
    )
    parser.add_argument(
        "--expected-kandelo-sha",
        required=True,
        help="the frozen 40-character ABI 42 Kandelo publication SHA",
    )
    parser.add_argument(
        "--state-file",
        type=pathlib.Path,
        help=(
            "durable local dispatch ledger "
            "(required with --dispatch or --recover-dispatch)"
        ),
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--dispatch",
        action="store_true",
        help="explicitly create fresh production repository_dispatch events",
    )
    action.add_argument(
        "--recover-dispatch",
        action="store_true",
        help="record one exact late run for the submitted unresolved intent; never dispatch",
    )
    action.add_argument(
        "--abandon-dispatch-run",
        type=int,
        metavar="RUN_ID",
        help=(
            "clear one submitted intent only after proving this sole cancelled "
            "run never started an external-write job"
        ),
    )
    action.add_argument(
        "--recover-failed-run",
        type=int,
        action="append",
        metavar="RUN_ID",
        help=(
            "retire a controller-recorded failed run after proving its identity "
            "state; repeat to migrate one reviewed reservation batch atomically"
        ),
    )
    parser.add_argument(
        "--adopt-failed-run",
        action="append",
        metavar="FORMULA=RUN_ID",
        help=(
            "retire an exact unrecorded pre-matrix failed run after validating "
            "its caller input log and proving every downstream job was skipped; "
            "may be combined with --recover-failed-run"
        ),
    )
    parser.add_argument(
        "--max-dispatches",
        type=int,
        default=MAX_ACTIVE_RUNS,
        help="maximum fresh dispatches in this invocation (default: 8)",
    )
    parser.add_argument(
        "--formulae",
        help=(
            "comma-separated exact Formula allowlist for --dispatch; omitted "
            "Formulae remain in the ledger but cannot be selected"
        ),
    )
    parser.add_argument(
        "--ack-timeout",
        type=int,
        default=600,
        help="seconds to wait for each unambiguous new run ID (default: 600)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="run-ID acknowledgement poll interval",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="inspect the existing origin/main ref without fetching (tests only)",
    )
    parser.add_argument("--json", action="store_true", help="emit status as JSON")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_kandelo_sha):
        parser.error("--expected-kandelo-sha must be exactly 40 lowercase hex characters")
    if (
        args.dispatch
        or args.recover_dispatch
        or args.abandon_dispatch_run is not None
        or args.recover_failed_run is not None
        or args.adopt_failed_run is not None
    ) and args.state_file is None:
        parser.error(
            "--state-file is required with --dispatch, --recover-dispatch, "
            "--abandon-dispatch-run, --recover-failed-run, or --adopt-failed-run"
        )
    if args.abandon_dispatch_run is not None and args.abandon_dispatch_run < 1:
        parser.error("--abandon-dispatch-run must be a positive run ID")
    if args.recover_failed_run is not None and (
        any(run_id < 1 for run_id in args.recover_failed_run)
        or len(args.recover_failed_run) != len(set(args.recover_failed_run))
    ):
        parser.error("--recover-failed-run values must be distinct positive run IDs")
    adopted: list[tuple[str, int]] = []
    for value in args.adopt_failed_run or ():
        if value.count("=") != 1:
            parser.error("--adopt-failed-run must use FORMULA=RUN_ID")
        formula, raw_run_id = value.split("=", 1)
        try:
            run_id = int(raw_run_id)
        except ValueError:
            parser.error("--adopt-failed-run RUN_ID must be a positive integer")
        if formula not in FORMULA_ORDER or run_id < 1:
            parser.error(
                "--adopt-failed-run requires a known Formula and positive run ID"
            )
        adopted.append((formula, run_id))
    if len({run_id for _formula, run_id in adopted}) != len(adopted):
        parser.error("--adopt-failed-run run IDs must be distinct")
    if adopted and (
        args.dispatch
        or args.recover_dispatch
        or args.abandon_dispatch_run is not None
    ):
        parser.error(
            "--adopt-failed-run may be combined only with --recover-failed-run"
        )
    if args.recover_failed_run is not None and (
        set(args.recover_failed_run) & {run_id for _formula, run_id in adopted}
    ):
        parser.error("recovered and adopted failed run IDs must be distinct")
    args.adopt_failed_run = adopted
    if args.max_dispatches < 1 or args.max_dispatches > MAX_ACTIVE_RUNS:
        parser.error(f"--max-dispatches must be between 1 and {MAX_ACTIVE_RUNS}")
    if args.ack_timeout < 1 or args.poll_seconds <= 0:
        parser.error("acknowledgement timeout and poll interval must be positive")
    if args.formulae is not None:
        values = args.formulae.split(",")
        if (
            not args.dispatch
            or not values
            or any(value not in FORMULA_ORDER for value in values)
            or len(values) != len(set(values))
        ):
            parser.error(
                "--formulae requires --dispatch and distinct exact Formula names"
            )
        args.formulae = frozenset(values)
    return args


def main(argv: Sequence[str] = sys.argv[1:]) -> int:
    args = parse_args(argv)
    try:
        tap = GitTap(args.tap_root)
        github = GitHub()
        if args.recover_failed_run is not None or args.adopt_failed_run:
            state_path = args.state_file.resolve()
            with state_lock(state_path):
                results = recover_failed_dispatches(
                    tap=tap,
                    github=github,
                    registry=AnonymousRegistry(),
                    expected_kandelo_sha=args.expected_kandelo_sha,
                    state_path=state_path,
                    run_ids=args.recover_failed_run or (),
                    adopt_failed_runs=args.adopt_failed_run,
                    no_fetch=args.no_fetch,
                )
            for formula, run_id, recovery_kind, reference in results:
                print(
                    f"recovered failed {formula} run {run_id} ({recovery_kind}, "
                    f"{reference})"
                )
            print(
                f"failed-recovery batch complete: {len(results)} run(s); "
                "no repository_dispatch was sent"
            )
            return 0
        if args.abandon_dispatch_run is not None:
            state_path = args.state_file.resolve()
            with state_lock(state_path):
                formula, run_id = abandon_submitted_dispatch(
                    tap=tap,
                    github=github,
                    expected_kandelo_sha=args.expected_kandelo_sha,
                    state_path=state_path,
                    run_id=args.abandon_dispatch_run,
                    no_fetch=args.no_fetch,
                )
            print(
                f"abandoned submitted {formula} dispatch run {run_id}; "
                "no repository_dispatch was sent"
            )
            return 0
        if args.recover_dispatch:
            state_path = args.state_file.resolve()
            with state_lock(state_path):
                formula, run_id = recover_submitted_dispatch(
                    tap=tap,
                    github=github,
                    expected_kandelo_sha=args.expected_kandelo_sha,
                    state_path=state_path,
                    no_fetch=args.no_fetch,
                )
            print(
                f"recovered submitted {formula} dispatch as run {run_id}; "
                "no repository_dispatch was sent"
            )
            return 0
        if args.dispatch:
            state_path = args.state_file.resolve()
            with state_lock(state_path):
                dispatched = dispatch_ready(
                    tap=tap,
                    github=github,
                    expected_kandelo_sha=args.expected_kandelo_sha,
                    state_path=state_path,
                    no_fetch=args.no_fetch,
                    maximum=args.max_dispatches,
                    timeout_seconds=args.ack_timeout,
                    poll_seconds=args.poll_seconds,
                    allowed_formulae=args.formulae,
                )
            print(f"dispatch pass complete: {dispatched} fresh run(s) submitted")
            return 0

        sha = tap.main_without_fetch() if args.no_fetch else tap.fetch_main()
        snapshot = load_snapshot(tap, sha)
        validate_workflow(github, snapshot, args.expected_kandelo_sha)
        state = read_state(args.state_file.resolve()) if args.state_file else None
        if state is not None:
            validate_state(state, snapshot, args.expected_kandelo_sha)
            if state.get("unresolved_dispatch") is not None:
                raise RolloutError(
                    f"{args.state_file} contains an unresolved dispatch"
                )
        inventory = active_inventory(github)
        if state is not None:
            inventory = reconcile_recorded_activity(github, inventory, state)
        finalized = {
            formula: not finalization_reasons(
                tap,
                snapshot,
                formula,
                required_arches(formula),
                args.expected_kandelo_sha,
            )
            for formula in FORMULA_ORDER
        }
        history_blocks = history_blocks_from_state(github, state, finalized)
        statuses = calculate_statuses(
            tap, snapshot, args.expected_kandelo_sha, inventory, history_blocks
        )
        render_status(snapshot, inventory, statuses, as_json=args.json)
        return 0
    except RolloutError as error:
        print(f"abi42-rollout: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
