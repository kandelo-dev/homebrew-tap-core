#!/usr/bin/env python3
"""Safely initialize, inspect, recover, or dispatch Kandelo bottle campaigns.

The default command is read-only with respect to GitHub. It fetches tap `main`,
checks finalized sidecars and production runs, and prints what is ready. The
only GitHub write path is the explicit `--dispatch` flag, which journals a
capacity-bounded batch and creates one fresh `repository_dispatch` per Formula.
Fresh campaign initialization and recovery commands write only the locked
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
import secrets
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
LEGACY_PUBLISHER_WORKFLOW_SHA = "3545bfd34509a52b68a4620c92e4aae24c60adb0"
LEGACY_ABI42_CONSUMER_SHA = "d3805721b887a19382ef1c96b576fc27badc0951"
LEGACY_PREPUBLICATION_STAGING_TAG = "pr-1079-staging"
LEGACY_PREPUBLICATION_GENERATION_SHA = "437fde2524ea6ad9c44933f8abbf995a46841009"
# The next campaign deliberately keeps its authority separate from the legacy
# d380 controller. These values are replaced only after the exact Kandelo main
# merge and its selected rootfs generation have both been admitted.
# WHY: retain the failed M3 caller as historical authority for its original
# private ledger and committed failure report. A new dispatch still requires
# protected tap main to equal the separately pinned current caller below.
FAILED_M3_MAIN_SHA = "5f448e68ec031108de42e965f5284944861b6ea2"
FAILED_M3_ROOTFS_GENERATION_TAG = "package-generation-rootfs-wasm32-abi-v42-sha256-d66825c03af08133538018dca0bad5732d8eaf5add3dfd513b3c1bce9210256e"
FAILED_M3_CALLER_SHA256 = "3c6028607ad3bdbba8a814e065d602d9c1cc45c64ccd6e8e859a336d58acfeac"
CURRENT_MAIN_SHA = "00cc12970ecaa474cb25350390bb270d38630e0c"
CURRENT_ROOTFS_GENERATION_TAG = "package-generation-rootfs-wasm32-abi-v42-sha256-817caab87fc9dfc7a87ce20bb5c43368ce83b43224538b5595a24c7ae45439a7"
CURRENT_CALLER_SHA256 = "8cdfa74f34f03f8a6af653e38dae4a917c087db0bb2911be529cfc1def9e41ae"
# WHY: the current write caller executes the publisher and consumes packages
# from the same exact main commit. The selected-input admission record, rather
# than a distinct source commit, vouches for the preserved rootfs bytes.
PUBLISHER_WORKFLOW_SHA = CURRENT_MAIN_SHA
ABI42_CONSUMER_SHA = CURRENT_MAIN_SHA
PREPUBLICATION_STAGING_TAG = CURRENT_ROOTFS_GENERATION_TAG
PREPUBLICATION_GENERATION_SHA = CURRENT_MAIN_SHA
# These hashes bind the complete protected caller, including permissions and
# the absence of caller-provided secrets or extra executable jobs. The
# transitional caller selected an incompatible consumer and is therefore
# approved only as evidence for runs proven to have stopped before all writes.
APPROVED_PUBLICATION_WORKFLOWS = {
    "3207ecd35a5cca77fc5bb0e26bee8ab9d354efcb7fef2c1d7aa8b65a8b2bade3": (
        LEGACY_ABI42_CONSUMER_SHA,
        LEGACY_ABI42_CONSUMER_SHA,
        "main",
    ),
    "0bf3328ac4d5c0f3497b071943d875e5d43ef4c37f81b941377d7cefdbde97d8": (
        LEGACY_PUBLISHER_WORKFLOW_SHA,
        LEGACY_ABI42_CONSUMER_SHA,
        "exact",
    ),
    "0e526ce02463ee83ec77952eb0cbdaf427541b0c8549fa9cd70e9e58f9fe4376": (
        LEGACY_PUBLISHER_WORKFLOW_SHA,
        LEGACY_ABI42_CONSUMER_SHA,
        "exact",
    ),
    FAILED_M3_CALLER_SHA256: (
        FAILED_M3_MAIN_SHA,
        FAILED_M3_MAIN_SHA,
        "exact",
    ),
    CURRENT_CALLER_SHA256: (
        CURRENT_MAIN_SHA,
        CURRENT_MAIN_SHA,
        "exact",
    ),
}
APPROVED_NO_WRITE_ONLY_WORKFLOWS = {
    "6e425bbaa04a1c0127db59a0cab8365eebfe5f67946b44de935b76b0ec745ada": (
        LEGACY_PUBLISHER_WORKFLOW_SHA,
        LEGACY_PUBLISHER_WORKFLOW_SHA,
        "exact",
    ),
}
EXPECTED_ABI = 42
EXPECTED_RELEASE_TAG = "bottles-abi-v42"
# A fresh campaign may not turn arbitrary command-line SHAs into publication
# authority. Each complete caller hash must be reviewed with the exact reusable
# publisher, package consumer, and sealed package generation it selects.
APPROVED_CAMPAIGN_CONTRACTS = {
    "0e526ce02463ee83ec77952eb0cbdaf427541b0c8549fa9cd70e9e58f9fe4376": (
        LEGACY_PUBLISHER_WORKFLOW_SHA,
        LEGACY_ABI42_CONSUMER_SHA,
        LEGACY_PREPUBLICATION_GENERATION_SHA,
        LEGACY_PREPUBLICATION_STAGING_TAG,
    ),
    FAILED_M3_CALLER_SHA256: (
        FAILED_M3_MAIN_SHA,
        FAILED_M3_MAIN_SHA,
        FAILED_M3_MAIN_SHA,
        FAILED_M3_ROOTFS_GENERATION_TAG,
    ),
    CURRENT_CALLER_SHA256: (
        CURRENT_MAIN_SHA,
        CURRENT_MAIN_SHA,
        CURRENT_MAIN_SHA,
        CURRENT_ROOTFS_GENERATION_TAG,
    ),
}
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
WORKFLOW_RUN_SNAPSHOT_ATTEMPTS = 3
WORKFLOW_RUN_SNAPSHOT_BACKOFF_SECONDS = 1.0
WORKFLOW_RUN_PAGE_SIZE = 100
MAX_WORKFLOW_RUN_PAGES = 100
DISPATCH_RUN_CLOCK_SKEW = dt.timedelta(minutes=5)
BOTTLE_ROOT = "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
REGISTRY_TOKEN_ROOT = "https://ghcr.io/token"
MAX_REGISTRY_RESPONSE_BYTES = 4 * 1024 * 1024
REGISTRY_NAMESPACE = "kandelo-dev/homebrew-tap-core"
CAMPAIGN_MANIFEST_PATH = (
    "Kandelo/campaigns/mostly-lazy-shell-abi42-rootfs-wasm32.json"
)
CAMPAIGN_MANIFEST_ID = "mostly-lazy-shell-abi42-rootfs-wasm32"
CAMPAIGN_BASE_TAP_SHA = "a0a3afe4ad63e1efd64c8c63d97edc5587a8d755"
CAMPAIGN_MANIFEST_SHA256 = (
    "c2d1b741a3b2c378c9b9287f4c0f775129e7433e3eed68020007f437b107678d"
)
CAMPAIGN_REUSE_COUNT = 23
MAX_BOTTLE_BYTES = 1024 * 1024 * 1024
MAX_JOB_LOG_BYTES = 4 * 1024 * 1024
ACCEPTED_REGISTRY_MEDIA_TYPES = frozenset(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
LEGACY_SINGLE_INTENT_WORKFLOW_SHA256 = (
    "3207ecd35a5cca77fc5bb0e26bee8ab9d354efcb7fef2c1d7aa8b65a8b2bade3"
)
DISPATCH_TOKEN_RE = re.compile(r"abi42-[0-9a-f]{32}")
WORKFLOW_RUN_NAME_SOURCE = (
    "Publish Kandelo bottles / ${{ github.event.client_payload.formulae }} / "
    "${{ github.event.client_payload.dispatch_token || 'untracked' }}"
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
ARCHITECTURE_IDENTITY_COUNT = sum(
    2 if name in DUAL_ARCH_FORMULAE else 1 for name in FORMULA_ORDER
)
CAMPAIGN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
PACKAGE_GENERATION_TAG_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}"
)

# Python's publication-time VFS acceptance uses Dash as its guest shell even
# though Python's Formula runtime dependency list itself contains only zlib.
EXTRA_DEPENDENCIES = {"python": frozenset(("dash",))}

if len(FORMULA_ORDER) != 63 or len(set(FORMULA_ORDER)) != 63:
    raise RuntimeError("the ABI 42 rollout must contain exactly 63 unique Formulae")
if ARCHITECTURE_IDENTITY_COUNT != 70:
    raise RuntimeError("the ABI 42 rollout must contain exactly 70 architecture identities")


class RolloutError(RuntimeError):
    """A condition that makes continuing the rollout unsafe."""


class WorkflowRunSnapshotError(RolloutError):
    """GitHub changed a workflow-run listing while it was being paginated."""


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
class CampaignContract:
    """Exact reviewed code and package generation used by one fresh campaign."""

    publisher_sha: str
    consumer_sha: str
    package_generation_sha: str
    package_generation_tag: str
    workflow_sha256: str

    def state_value(self) -> dict[str, str]:
        return {
            "expected_consumer_sha": self.consumer_sha,
            "expected_package_generation_sha": self.package_generation_sha,
            "expected_package_generation_tag": self.package_generation_tag,
            "expected_publisher_sha": self.publisher_sha,
            "expected_workflow_sha256": self.workflow_sha256,
        }


@dataclasses.dataclass(frozen=True)
class CampaignSelection:
    """One exact partition of the campaign-owned Formula catalog."""

    rebuild: tuple[str, ...]
    reuse: tuple[str, ...]
    deferred: tuple[str, ...]

    @staticmethod
    def _ordered(values: Iterable[str], label: str) -> tuple[str, ...]:
        selected = tuple(values)
        if (
            len(selected) != len(set(selected))
            or any(value not in FORMULA_ORDER for value in selected)
        ):
            raise RolloutError(
                f"campaign {label} must contain distinct known Formulae"
            )
        wanted = set(selected)
        ordered = tuple(formula for formula in FORMULA_ORDER if formula in wanted)
        if selected != ordered:
            raise RolloutError(
                f"campaign {label} must follow the canonical Formula order"
            )
        return ordered

    @classmethod
    def create(
        cls,
        *,
        rebuild: Iterable[str],
        reuse: Iterable[str],
        deferred: Iterable[str],
    ) -> "CampaignSelection":
        selection = cls(
            rebuild=cls._ordered(rebuild, "rebuild set"),
            reuse=cls._ordered(reuse, "reuse set"),
            deferred=cls._ordered(deferred, "deferred set"),
        )
        sets = tuple(map(set, dataclasses.astuple(selection)))
        if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
            raise RolloutError("campaign Formula partitions overlap")
        if set().union(*sets) != set(FORMULA_ORDER):
            raise RolloutError(
                "campaign rebuild, reuse, and deferred sets must partition "
                "the complete Formula catalog"
            )
        if not selection.rebuild:
            raise RolloutError("campaign rebuild set must not be empty")
        return selection

    @classmethod
    def all_rebuild(cls) -> "CampaignSelection":
        return cls.create(rebuild=FORMULA_ORDER, reuse=(), deferred=())

    def state_value(self) -> dict[str, list[str]]:
        return {
            "rebuild_formulae": list(self.rebuild),
            "reuse_formulae": list(self.reuse),
            "deferred_formulae": list(self.deferred),
        }


@dataclasses.dataclass(frozen=True)
class CampaignReuse:
    formula: str
    version: str
    formula_revision: int
    bottle_rebuild: int
    blob_sha256: str
    blob_bytes: int
    sidecar_path: str
    sidecar_sha256: str
    link_manifest_path: str
    link_manifest_sha256: str


@dataclasses.dataclass(frozen=True)
class CampaignManifest:
    campaign: str
    rootfs_arch: str
    base_tap_sha: str
    reservation_tap_sha: str
    rebuild_formula: str
    rebuild_version: str
    rebuild_formula_revision: int
    old_bottle_rebuild: int
    reserved_bottle_rebuild: int
    reuse: tuple[CampaignReuse, ...]
    deferred: tuple[str, ...]
    sha256: str

    @property
    def selection(self) -> CampaignSelection:
        reuse_names = {entry.formula for entry in self.reuse}
        return CampaignSelection.create(
            rebuild=(self.rebuild_formula,),
            reuse=tuple(
                formula for formula in FORMULA_ORDER if formula in reuse_names
            ),
            deferred=self.deferred,
        )


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
    formula_sidecar_tree: str = ""


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


@dataclasses.dataclass(frozen=True)
class PendingDispatch:
    formula: str
    arches: tuple[str, ...]
    tap_sha: str
    dispatch_token: str
    status: str
    recorded_at: str
    request_started_at: str | None
    submitted_at: str | None


@dataclasses.dataclass(frozen=True)
class CorrelatedRun:
    run_id: int
    caller_tap_sha: str


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

    def show_bytes(self, revision: str, path: str) -> bytes:
        # WHY: campaign authority binds the exact Git blob bytes. Text-mode
        # subprocess decoding and newline conversion must not be allowed to
        # turn a different sidecar, link manifest, or campaign manifest into
        # equivalent parsed JSON.
        result = subprocess.run(
            ("git", "cat-file", "blob", f"{revision}:{path}"),
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
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

    def ensure_commit(self, revision: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RolloutError(f"tap commit is not an exact SHA: {revision!r}")
        present = self.git("cat-file", "-e", f"{revision}^{{commit}}", check=False)
        if present.returncode == 0:
            return
        # A repository_dispatch run can expose a new protected-main commit
        # before this controller's remote-tracking ref is refreshed. Fetch only
        # that immutable run source so validation never substitutes newer main.
        self.git("fetch", "--quiet", "--no-tags", "origin", revision)
        resolved = self.git("rev-parse", f"{revision}^{{commit}}").stdout.strip()
        if resolved != revision:
            raise RolloutError(f"GitHub run source {revision} was not fetched exactly")

    def changed_entries(
        self, ancestor: str, descendant: str
    ) -> tuple[tuple[str, str], ...]:
        output = self.git(
            "diff",
            "--name-status",
            "--no-renames",
            "-z",
            ancestor,
            descendant,
        ).stdout
        fields = output.split("\0")
        if fields[-1:] == [""]:
            fields.pop()
        if len(fields) % 2:
            raise RolloutError("tap source comparison returned malformed entries")
        entries = tuple(zip(fields[0::2], fields[1::2], strict=True))
        if any(
            not re.fullmatch(r"[ACDMTUXB]", status) or not path
            for status, path in entries
        ):
            raise RolloutError("tap source comparison returned an unsafe change")
        if len(entries) != len({path for _status, path in entries}):
            raise RolloutError("tap source comparison returned duplicate paths")
        return entries

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

    def runs(
        self,
        *,
        per_page: int = WORKFLOW_RUN_PAGE_SIZE,
        page: int = 1,
        created: str | None = None,
    ) -> Mapping[str, Any]:
        query_values: dict[str, str | int] = {
            "per_page": per_page,
            "page": page,
        }
        if created is not None:
            query_values["created"] = created
        query = "?" + urllib.parse.urlencode(query_values)
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
        self,
        formula: str,
        arches: Sequence[str],
        tap_sha: str,
        dispatch_token: str,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", tap_sha):
            raise RolloutError(
                f"dispatch requires an exact lowercase tap commit SHA, got {tap_sha!r}"
            )
        if not DISPATCH_TOKEN_RE.fullmatch(dispatch_token):
            raise RolloutError("repository dispatch token is malformed")
        payload: dict[str, Any] = {
            "event_type": "publish-kandelo-bottles",
            "client_payload": {
                "formulae": formula,
                "arches": ",".join(arches),
                # WHY: repository_dispatch loads the caller from the default
                # branch, but bottle source must stay bound to the exact tap
                # snapshot that the controller validated and recorded.
                "tap_sha": tap_sha,
                "dispatch_token": dispatch_token,
            },
        }
        if formula == "python":
            # WHY: the protected caller maps this one reviewed bit to both
            # required acceptance and its temporary postpublication deferral.
            # No other Formula can independently request either exception.
            payload["client_payload"]["require_vfs_acceptance"] = True
        # repository_dispatch intentionally returns 204 with no run ID. The
        # token is recorded durably before this request and becomes part of the
        # outer workflow run name, so several requests can be submitted before
        # their runs appear without confusing one request for another.
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


class _NoRegistryRedirects(urllib.request.HTTPRedirectHandler):
    """Return every registry redirect to the verifier instead of following it."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del request, fp, code, msg, headers, newurl
        return None


class AnonymousRegistry:
    """Read one public GHCR identity without reusing operator credentials."""

    def __init__(self, opener: Any | None = None) -> None:
        # WHY: urllib's default handler follows redirects and can copy request
        # headers. Bottle redirects are inspected and followed explicitly so
        # the GHCR bearer can never escape to object storage.
        self.opener = opener or urllib.request.build_opener(
            _NoRegistryRedirects()
        ).open

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

    def _anonymous_token(self, formula: str) -> str:
        if formula not in FORMULA_ORDER:
            raise RolloutError(f"cannot inspect an unknown Formula: {formula!r}")
        scope = f"repository:{REGISTRY_NAMESPACE}/{formula}:pull"
        token_url = (
            f"{REGISTRY_TOKEN_ROOT}?"
            + urllib.parse.urlencode({"service": "ghcr.io", "scope": scope})
        )
        # WHY: never accept an operator token here. The campaign is proving
        # that a guest can retrieve these public bottle bytes, so its only
        # credential is a pull token obtained from GHCR without Authorization.
        token_request = urllib.request.Request(
            token_url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        if token_request.has_header("Authorization"):
            raise RolloutError("anonymous GHCR token request is authenticated")
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
        return token

    def manifest(
        self, formula: str, reference: str
    ) -> RegistryManifestEvidence:
        if formula not in FORMULA_ORDER:
            raise RolloutError(f"cannot inspect an unknown Formula: {formula!r}")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}", reference):
            raise RolloutError(f"cannot inspect an invalid OCI reference: {reference!r}")

        token = self._anonymous_token(formula)

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

    def verify_blob(
        self,
        formula: str,
        digest: str,
        expected_bytes: int,
    ) -> None:
        if formula not in FORMULA_ORDER:
            raise RolloutError(f"cannot inspect an unknown Formula: {formula!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RolloutError("public GHCR blob digest is invalid")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 1
            or expected_bytes > MAX_BOTTLE_BYTES
        ):
            raise RolloutError("public GHCR blob size is invalid")

        token = self._anonymous_token(formula)
        # WHY: the endpoint is derived from the fixed public namespace and
        # immutable digest. No stored URL, package ID, run ID, or redirect can
        # select different bytes.
        blob_url = (
            f"{BOTTLE_ROOT}/{urllib.parse.quote(formula, safe='')}/blobs/"
            f"sha256:{digest}"
        )
        request = urllib.request.Request(
            blob_url,
            headers={
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )
        try:
            with self._open(request, f"public GHCR {formula} blob"):
                raise RolloutError(
                    f"public GHCR {formula} blob did not use its required redirect"
                )
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location")
            code = error.code
            response_url = error.geturl()
            error.close()
            if (
                code != 307
                or response_url != blob_url
                or not isinstance(location, str)
            ):
                raise RolloutError(
                    f"public GHCR {formula} blob returned HTTP {code}"
                ) from error

        redirect = urllib.parse.urlsplit(location)
        if (
            redirect.scheme != "https"
            or redirect.hostname != "pkg-containers.githubusercontent.com"
            or redirect.port not in (None, 443)
            or redirect.username is not None
            or redirect.password is not None
            or redirect.fragment
            or not re.fullmatch(
                rf"/ghcrblobs[0-9]+/blobs/sha256:{digest}",
                redirect.path,
            )
            or not redirect.query
        ):
            raise RolloutError(
                f"public GHCR {formula} blob redirect is outside approved storage"
            )

        # WHY: Location is a short-lived signed object URL. Construct a new
        # request rather than reusing the GHCR request, and deliberately omit
        # Authorization so only the signed URL reaches GitHub object storage.
        storage_request = urllib.request.Request(
            location,
            headers={"Accept": "application/octet-stream"},
            method="GET",
        )
        if storage_request.has_header("Authorization"):
            raise RolloutError("public GHCR storage request is authenticated")
        try:
            with self._open(
                storage_request, f"public GHCR {formula} storage blob"
            ) as response:
                if response.geturl() != location or response.getcode() != 200:
                    raise RolloutError(
                        f"public GHCR {formula} storage blob redirected or failed"
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        parsed_length = int(content_length)
                    except ValueError as error:
                        raise RolloutError(
                            f"public GHCR {formula} blob has an invalid Content-Length"
                        ) from error
                    if parsed_length != expected_bytes:
                        raise RolloutError(
                            f"public GHCR {formula} blob size differs from authority"
                        )
                observed = 0
                hasher = hashlib.sha256()
                while True:
                    try:
                        chunk = response.read(
                            min(1024 * 1024, expected_bytes - observed + 1)
                        )
                    except OSError as error:
                        raise RolloutError(
                            f"cannot read public GHCR {formula} blob: {error}"
                        ) from error
                    if not chunk:
                        break
                    observed += len(chunk)
                    if observed > expected_bytes:
                        raise RolloutError(
                            f"public GHCR {formula} blob exceeds authority size"
                        )
                    hasher.update(chunk)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise RolloutError(
                f"public GHCR {formula} storage blob returned HTTP {code}"
            ) from error
        if observed != expected_bytes:
            raise RolloutError(
                f"public GHCR {formula} blob size differs from authority"
            )
        if hasher.hexdigest() != digest:
            raise RolloutError(
                f"public GHCR {formula} blob digest differs from authority"
            )


def _json_object(text: str, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise RolloutError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise RolloutError(f"{label} is not a JSON object")
    return value


def _json_object_bytes(raw: bytes, label: str) -> dict[str, Any]:
    def reject_duplicate_keys(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RolloutError(f"{label} duplicates JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RolloutError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise RolloutError(f"{label} is not a JSON object")
    return value


def _exact_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RolloutError(f"{label} must be exactly 64 lowercase hex")
    return value


def _manifest_file_reference(
    value: Any,
    *,
    label: str,
    expected_path: str,
) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise RolloutError(f"{label} has an unexpected shape")
    path = value.get("path")
    digest = _exact_sha256(value.get("sha256"), f"{label} SHA-256")
    if path != expected_path:
        raise RolloutError(f"{label} does not name its canonical T0 path")
    return path, digest


def load_campaign_manifest(
    tap: GitTap,
    manifest_authority_sha: str,
    *,
    expected_sha256: str | None = None,
) -> CampaignManifest:
    if not re.fullmatch(r"[0-9a-f]{40}", manifest_authority_sha):
        raise RolloutError("campaign manifest authority is not an exact tap SHA")
    raw = tap.show_bytes(manifest_authority_sha, CAMPAIGN_MANIFEST_PATH)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != CAMPAIGN_MANIFEST_SHA256:
        # WHY: an exact commit is necessary but insufficient if an operator
        # can nominate an arbitrary new protected-main commit. This reviewed
        # raw digest makes the one committed manifest—not a replacement with
        # a different 23-entry selection—the production authority.
        raise RolloutError(
            "campaign manifest bytes are not the canonical production authority"
        )
    if expected_sha256 is not None and digest != _exact_sha256(
        expected_sha256, "campaign manifest ledger SHA-256"
    ):
        raise RolloutError("campaign manifest bytes differ from the private ledger")
    value = _json_object_bytes(raw, CAMPAIGN_MANIFEST_PATH)
    # WHY: the ledger hashes raw bytes. Requiring the repository's canonical
    # encoding makes review and byte-for-byte test fixtures unambiguous.
    if raw != (json.dumps(value, indent=2) + "\n").encode():
        raise RolloutError("campaign manifest is not canonical JSON")
    expected_top_keys = {
        "base_tap_sha",
        "campaign",
        "kandelo_abi",
        "rebuild",
        "registry_namespace",
        "reservation_tap_sha",
        "reuse",
        "rootfs_arch",
        "schema",
    }
    if set(value) != expected_top_keys:
        raise RolloutError("campaign manifest has an unexpected shape")
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or value.get("campaign") != CAMPAIGN_MANIFEST_ID
        or type(value.get("kandelo_abi")) is not int
        or value["kandelo_abi"] != EXPECTED_ABI
        or value.get("rootfs_arch") != "wasm32"
        or value.get("registry_namespace") != REGISTRY_NAMESPACE
        or value.get("base_tap_sha") != CAMPAIGN_BASE_TAP_SHA
        or not isinstance(value.get("reservation_tap_sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["reservation_tap_sha"])
    ):
        raise RolloutError("campaign manifest selects an unapproved campaign identity")

    rebuild = value.get("rebuild")
    expected_rebuild_keys = {
        "formula",
        "formula_revision",
        "old_bottle_rebuild",
        "reserved_bottle_rebuild",
        "version",
    }
    if not isinstance(rebuild, dict) or set(rebuild) != expected_rebuild_keys:
        raise RolloutError("campaign manifest rebuild identity has an unexpected shape")
    if (
        rebuild.get("formula") != "bash"
        or not isinstance(rebuild.get("version"), str)
        or not rebuild["version"]
        or type(rebuild.get("formula_revision")) is not int
        or rebuild["formula_revision"] < 0
        or type(rebuild.get("old_bottle_rebuild")) is not int
        or rebuild["old_bottle_rebuild"] < 0
        or type(rebuild.get("reserved_bottle_rebuild")) is not int
        or rebuild["reserved_bottle_rebuild"]
        != rebuild["old_bottle_rebuild"] + 1
    ):
        raise RolloutError("campaign manifest Bash identity is invalid")

    raw_reuse = value.get("reuse")
    if not isinstance(raw_reuse, list) or len(raw_reuse) != CAMPAIGN_REUSE_COUNT:
        raise RolloutError(
            f"campaign manifest must contain exactly {CAMPAIGN_REUSE_COUNT} reuse entries"
        )
    reuse: list[CampaignReuse] = []
    expected_entry_keys = {
        "blob",
        "bottle_rebuild",
        "formula",
        "formula_revision",
        "link_manifest",
        "sidecar",
        "version",
    }
    for index, entry in enumerate(raw_reuse):
        if not isinstance(entry, dict) or set(entry) != expected_entry_keys:
            raise RolloutError(
                f"campaign manifest reuse entry {index} has an unexpected shape"
            )
        formula = entry.get("formula")
        version = entry.get("version")
        formula_revision = entry.get("formula_revision")
        bottle_rebuild = entry.get("bottle_rebuild")
        blob = entry.get("blob")
        if (
            formula not in FORMULA_ORDER
            or formula == "bash"
            or not isinstance(version, str)
            or not version
            or type(formula_revision) is not int
            or formula_revision < 0
            or type(bottle_rebuild) is not int
            or bottle_rebuild < 0
            or not isinstance(blob, dict)
            or set(blob) != {"bytes", "sha256"}
            or type(blob.get("bytes")) is not int
            or blob["bytes"] < 1
            or blob["bytes"] > MAX_BOTTLE_BYTES
        ):
            raise RolloutError(
                f"campaign manifest reuse entry {index} has an invalid identity"
            )
        blob_sha256 = _exact_sha256(
            blob.get("sha256"),
            f"campaign manifest {formula} blob SHA-256",
        )
        sidecar_path, sidecar_sha256 = _manifest_file_reference(
            entry.get("sidecar"),
            label=f"campaign manifest {formula} sidecar",
            expected_path=f"Kandelo/formula/{formula}.json",
        )
        link_path, link_sha256 = _manifest_file_reference(
            entry.get("link_manifest"),
            label=f"campaign manifest {formula} link manifest",
            expected_path=(
                f"Kandelo/link/{formula}-{version}-"
                f"rebuild{bottle_rebuild}-wasm32.json"
            ),
        )
        reuse.append(
            CampaignReuse(
                formula=formula,
                version=version,
                formula_revision=formula_revision,
                bottle_rebuild=bottle_rebuild,
                blob_sha256=blob_sha256,
                blob_bytes=blob["bytes"],
                sidecar_path=sidecar_path,
                sidecar_sha256=sidecar_sha256,
                link_manifest_path=link_path,
                link_manifest_sha256=link_sha256,
            )
        )

    reuse_formulae = tuple(entry.formula for entry in reuse)
    if len(reuse_formulae) != len(set(reuse_formulae)):
        raise RolloutError("campaign manifest duplicates a reuse Formula")
    wanted = set(reuse_formulae)
    if reuse_formulae != tuple(sorted(wanted)):
        raise RolloutError(
            "campaign manifest reuse entries are not in canonical name order"
        )
    deferred = tuple(
        formula
        for formula in FORMULA_ORDER
        if formula != "bash" and formula not in wanted
    )
    manifest = CampaignManifest(
        campaign=value["campaign"],
        rootfs_arch=value["rootfs_arch"],
        base_tap_sha=value["base_tap_sha"],
        reservation_tap_sha=value["reservation_tap_sha"],
        rebuild_formula=rebuild["formula"],
        rebuild_version=rebuild["version"],
        rebuild_formula_revision=rebuild["formula_revision"],
        old_bottle_rebuild=rebuild["old_bottle_rebuild"],
        reserved_bottle_rebuild=rebuild["reserved_bottle_rebuild"],
        reuse=tuple(reuse),
        deferred=deferred,
        sha256=digest,
    )
    # This also proves that the one rebuild, 23 reuse entries, and every
    # derived deferred T0 Formula form an exact, non-overlapping catalog.
    _ = manifest.selection
    return manifest


def validate_campaign_manifest_sources(
    tap: GitTap,
    manifest: CampaignManifest,
    base: TapSnapshot,
    reservation: TapSnapshot,
) -> None:
    if (
        base.sha != manifest.base_tap_sha
        or reservation.sha != manifest.reservation_tap_sha
    ):
        raise RolloutError("campaign manifest tap identities differ from loaded sources")
    base_bash = base.identities.get(manifest.rebuild_formula)
    reserved_bash = reservation.identities.get(manifest.rebuild_formula)
    stable_bash = (
        manifest.rebuild_version,
        manifest.rebuild_formula_revision,
    )
    if (
        base_bash is None
        or reserved_bash is None
        or (base_bash.pkg_version, base_bash.formula_revision) != stable_bash
        or (reserved_bash.pkg_version, reserved_bash.formula_revision) != stable_bash
        or base_bash.bottle_rebuild != manifest.old_bottle_rebuild
        or reserved_bash.bottle_rebuild != manifest.reserved_bottle_rebuild
    ):
        raise RolloutError("campaign manifest Bash identity differs from T0 or Tpre")

    for entry in manifest.reuse:
        raw_sidecar = tap.show_bytes(manifest.base_tap_sha, entry.sidecar_path)
        if hashlib.sha256(raw_sidecar).hexdigest() != entry.sidecar_sha256:
            raise RolloutError(
                f"campaign manifest {entry.formula} sidecar bytes differ at T0"
            )
        raw_link = tap.show_bytes(manifest.base_tap_sha, entry.link_manifest_path)
        if hashlib.sha256(raw_link).hexdigest() != entry.link_manifest_sha256:
            raise RolloutError(
                f"campaign manifest {entry.formula} link bytes differ at T0"
            )
        sidecar = _json_object_bytes(raw_sidecar, entry.sidecar_path)
        link = _json_object_bytes(raw_link, entry.link_manifest_path)
        base_identity = base.identities.get(entry.formula)
        sidecar_bottles = _bottles_by_arch(
            sidecar, f"campaign manifest {entry.formula} sidecar"
        )
        bottle = sidecar_bottles.get("wasm32")
        expected_url = (
            f"{BOTTLE_ROOT}/{entry.formula}/blobs/sha256:{entry.blob_sha256}"
        )
        if (
            base_identity is None
            or base_identity.pkg_version != entry.version
            or base_identity.formula_revision != entry.formula_revision
            or base_identity.bottle_rebuild != entry.bottle_rebuild
            or base_identity.bottle_sha256.get("wasm32") != entry.blob_sha256
            or sidecar.get("schema") != 1
            or sidecar.get("name") != entry.formula
            or sidecar.get("version") != entry.version
            or sidecar.get("formula_revision") != entry.formula_revision
            or sidecar.get("bottle_rebuild") != entry.bottle_rebuild
            or sidecar.get("kandelo_abi") != EXPECTED_ABI
            or not isinstance(bottle, dict)
            or bottle.get("arch") != "wasm32"
            or bottle.get("bottle_tag") != "wasm32_kandelo"
            or bottle.get("kandelo_abi") != EXPECTED_ABI
            or bottle.get("status", "success") != "success"
            or bottle.get("sha256") != entry.blob_sha256
            or bottle.get("bytes") != entry.blob_bytes
            or bottle.get("link_manifest") != entry.link_manifest_path
            or bottle.get("url") != expected_url
        ):
            raise RolloutError(
                f"campaign manifest {entry.formula} sidecar identity differs at T0"
            )
        link_bottle = link.get("bottle")
        if (
            link.get("schema") != 1
            or link.get("package") != entry.formula
            or link.get("version") != entry.version
            or link.get("arch") != "wasm32"
            or link.get("kandelo_abi") != EXPECTED_ABI
            or not isinstance(link_bottle, dict)
            or link_bottle.get("sha256") != entry.blob_sha256
            or link_bottle.get("bytes") != entry.blob_bytes
            or link_bottle.get("url") != expected_url
        ):
            raise RolloutError(
                f"campaign manifest {entry.formula} link identity differs at T0"
            )


def verify_campaign_reuse_blobs(
    registry: Any,
    manifest: CampaignManifest,
) -> None:
    if not hasattr(registry, "verify_blob"):
        raise RolloutError("campaign registry cannot verify exact public blob bytes")
    for entry in manifest.reuse:
        registry.verify_blob(
            entry.formula,
            entry.blob_sha256,
            entry.blob_bytes,
        )


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
    if not expected_formulae.issubset(actual_formulae):
        missing = sorted(expected_formulae - actual_formulae)
        raise RolloutError(
            f"tap is missing Formulae from the frozen 63-Formula campaign: {missing}"
        )
    # WHY: FORMULA_ORDER is the immutable ABI-42 shell campaign, not a
    # repository-wide prohibition on future packages. Unrelated Formulae stay
    # outside this snapshot, its dependency waves, and its dispatch authority.
    # Source-pair validation still rejects adding one between a reserved source
    # and a finalizer caller, so this does not widen an in-flight campaign.

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
        formula_sidecar_tree=tap.tree_oid(sha, "Kandelo/formula"),
    )


def validate_workflow_source(
    snapshot: TapSnapshot,
    expected_kandelo_sha: str,
    *,
    expected_publisher_sha: str | None = None,
    expected_package_generation_sha: str = PREPUBLICATION_GENERATION_SHA,
    expected_package_generation_tag: str = PREPUBLICATION_STAGING_TAG,
    allow_legacy_tap_ref: bool = False,
    allow_legacy_run_name: bool = False,
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

    run_names = re.findall(
        r"^run-name:\s*(.+?)\s*$",
        snapshot.workflow_source,
        flags=re.MULTILINE,
    )
    allowed_run_names = (
        ([], [WORKFLOW_RUN_NAME_SOURCE])
        if allow_legacy_run_name
        else ([WORKFLOW_RUN_NAME_SOURCE],)
    )
    if run_names not in allowed_run_names:
        raise RolloutError(
            "production workflow run-name does not expose the exact Formula "
            f"and dispatch-token identity: {run_names}"
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
    }
    if expected_package_generation_tag.startswith(
        "package-generation-rootfs-wasm32-"
    ):
        # WHY: selected-input admission moves authority to the immutable
        # generation tag and the exact current-main consumer. The legacy source
        # SHA and VFS deferral inputs would reopen the retired staging bridge.
        if expected_package_generation_sha != expected_kandelo_sha:
            raise RolloutError(
                "selected rootfs generation authority must equal the exact "
                "Kandelo package consumer"
            )
        expected_scalars["package-generation-wasm32"] = (
            expected_package_generation_tag
        )
        forbidden_generation_keys = (
            "package-generation-wasm64",
            "prepublication-staging-tag",
            "prepublication-staging-kandelo-sha",
            "defer-vfs-acceptance-until-postpublication",
        )
    else:
        expected_scalars.update(
            {
                "prepublication-staging-tag": expected_package_generation_tag,
                "prepublication-staging-kandelo-sha": (
                    expected_package_generation_sha
                ),
                "defer-vfs-acceptance-until-postpublication": (
                    "${{ github.event.client_payload."
                    "require_vfs_acceptance || false }}"
                ),
            }
        )
        forbidden_generation_keys = (
            "package-generation-wasm32",
            "package-generation-wasm64",
        )
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
    for key in forbidden_generation_keys:
        values = re.findall(
            rf"^\s+{re.escape(key)}:\s*(.+?)\s*$",
            snapshot.workflow_source,
            flags=re.MULTILINE,
        )
        if values:
            raise RolloutError(
                f"production workflow retains forbidden generation input {key}: "
                f"{values}"
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
    if authority is None and workflow_hash in APPROVED_CAMPAIGN_CONTRACTS:
        publisher, consumer, _generation_sha, _generation_tag = (
            APPROVED_CAMPAIGN_CONTRACTS[workflow_hash]
        )
        authority = (publisher, consumer, "exact")
    if authority is None and allow_no_write_only:
        authority = APPROVED_NO_WRITE_ONLY_WORKFLOWS.get(workflow_hash)
    if authority is None:
        raise RolloutError(
            f"publication workflow hash {workflow_hash} is not approved"
        )
    return authority


def approved_package_generation(
    workflow_hash: str,
) -> tuple[str, str]:
    campaign = APPROVED_CAMPAIGN_CONTRACTS.get(workflow_hash)
    if campaign is not None:
        return campaign[2], campaign[3]
    # WHY: recovery ledgers bind the caller that actually ran. Historical
    # callers selected the admitted pre-publication generation, even after the
    # controller's current campaign constants advance to final main.
    if (
        workflow_hash in APPROVED_PUBLICATION_WORKFLOWS
        or workflow_hash in APPROVED_NO_WRITE_ONLY_WORKFLOWS
    ):
        return (
            LEGACY_PREPUBLICATION_GENERATION_SHA,
            LEGACY_PREPUBLICATION_STAGING_TAG,
        )
    raise RolloutError(
        f"publication workflow hash {workflow_hash} has no approved package generation"
    )


def approved_publication_workflow_hash(workflow_hash: str) -> bool:
    return (
        workflow_hash in APPROVED_PUBLICATION_WORKFLOWS
        or workflow_hash in APPROVED_CAMPAIGN_CONTRACTS
    )


def validate_workflow(
    github: GitHub,
    snapshot: TapSnapshot,
    expected_kandelo_sha: str,
    *,
    campaign_contract: CampaignContract | None = None,
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
    if campaign_contract is not None:
        publisher_sha = campaign_contract.publisher_sha
        generation_sha = campaign_contract.package_generation_sha
        generation_tag = campaign_contract.package_generation_tag
        if campaign_contract.consumer_sha != expected_kandelo_sha:
            raise RolloutError(
                "campaign workflow contract selects another package consumer"
            )
    else:
        # WHY: recovery may inspect a retired but explicitly approved caller.
        # Derive every pin from that caller's exact hash; using today's globals
        # would either strand old ledgers or let a caller mix generations.
        publisher_sha, approved_consumer, source_selector = (
            approved_workflow_authority(snapshot)
        )
        if (
            approved_consumer != expected_kandelo_sha
            or source_selector != "exact"
        ):
            raise RolloutError(
                "active publication workflow authority differs from the "
                "requested consumer or exact tap-source contract"
            )
        generation_sha, generation_tag = approved_package_generation(
            workflow_sha256(snapshot)
        )
    validate_workflow_source(
        snapshot,
        expected_kandelo_sha,
        expected_publisher_sha=publisher_sha,
        expected_package_generation_sha=generation_sha,
        expected_package_generation_tag=generation_tag,
    )
    if campaign_contract is not None:
        if workflow_sha256(snapshot) != campaign_contract.workflow_sha256:
            raise RolloutError(
                "active publication workflow differs from the campaign's exact "
                "reviewed workflow"
            )
        return


def publication_workflow_contract(source: str) -> str:
    """Return bottle-affecting workflow bytes, excluding exact run identity."""
    lines = source.splitlines(keepends=True)
    run_name_lines = [
        line
        for line in lines
        if re.match(r"^run-name:", line)
    ]
    expected = f"run-name: {WORKFLOW_RUN_NAME_SOURCE}\n"
    if run_name_lines not in ([], [expected]):
        # Do not normalize an unknown run-name shape. It remains a provenance
        # mismatch instead of turning this display-only exception into a broad
        # workflow compatibility rule.
        return source
    return "".join(line for line in lines if line not in run_name_lines)


def finalizer_owned_path(path: str) -> bool:
    if path == "Kandelo/metadata.json":
        return True
    if re.fullmatch(
        r"Formula/(" + "|".join(map(re.escape, FORMULA_ORDER)) + r")\.rb",
        path,
    ):
        return True
    if re.fullmatch(
        r"Kandelo/formula/("
        + "|".join(map(re.escape, FORMULA_ORDER))
        + r")\.json",
        path,
    ):
        return True
    generated_name = r"[A-Za-z0-9][A-Za-z0-9._+-]*"
    return bool(
        re.fullmatch(rf"Kandelo/link/{generated_name}\.json", path)
        or re.fullmatch(
            rf"Kandelo/reports/(?:{generated_name}/)*{generated_name}\.json",
            path,
        )
    )


def finalizer_owned_change(status: str, path: str) -> bool:
    # Finalization composes files and replaces earlier generated summaries. It
    # never deletes or renames tracked state; treating D/R as ordinary output
    # could hide removal of provenance during a parallel source advance.
    return status in ("A", "M") and finalizer_owned_path(path)


def validate_caller_source_pair(
    tap: GitTap,
    formula: str,
    reserved_tap_sha: str,
    caller_tap_sha: str,
    *,
    snapshots: dict[str, TapSnapshot] | None = None,
    validated_pairs: set[tuple[str, str]] | None = None,
) -> tuple[TapSnapshot, TapSnapshot]:
    """Validate that a run caller advanced only generated finalizer state."""
    if not re.fullmatch(r"[0-9a-f]{40}", caller_tap_sha):
        raise RolloutError(
            f"token-correlated run for {formula} has an invalid caller SHA"
        )
    tap.ensure_commit(caller_tap_sha)
    if not tap.is_ancestor(reserved_tap_sha, caller_tap_sha):
        raise RolloutError(
            f"token-correlated caller for {formula} is not a descendant "
            "of its reserved tap commit"
        )

    pair = (reserved_tap_sha, caller_tap_sha)
    snapshot_cache = snapshots if snapshots is not None else {}
    if reserved_tap_sha not in snapshot_cache:
        snapshot_cache[reserved_tap_sha] = load_snapshot(tap, reserved_tap_sha)
    if caller_tap_sha not in snapshot_cache:
        snapshot_cache[caller_tap_sha] = load_snapshot(tap, caller_tap_sha)
    reserved = snapshot_cache[reserved_tap_sha]
    caller = snapshot_cache[caller_tap_sha]
    if validated_pairs is not None and pair in validated_pairs:
        return reserved, caller
    if catalog_state(caller) != catalog_state(reserved):
        raise RolloutError(
            f"token-correlated caller for {formula} changes a Formula "
            "recipe, identity, or dependency"
        )
    if caller.formula_support_tree != reserved.formula_support_tree:
        raise RolloutError(
            f"token-correlated caller for {formula} changes Formula support"
        )
    if publication_workflow_contract(
        caller.workflow_source
    ) != publication_workflow_contract(reserved.workflow_source):
        raise RolloutError(
            f"token-correlated caller for {formula} changes the normalized "
            "publication workflow"
        )

    changes = tap.changed_entries(reserved_tap_sha, caller_tap_sha)
    unsafe = [
        f"{status} {path}"
        for status, path in changes
        if not finalizer_owned_change(status, path)
    ]
    if unsafe:
        # WHY: equivalent recipes and workflow bytes are necessary but do not
        # make an unrelated README, controller, or policy edit part of a bottle
        # build. Only generated finalizer outputs may advance the event source.
        raise RolloutError(
            f"token-correlated caller for {formula} is not a "
            f"finalizer-only descendant: {', '.join(unsafe)}"
        )
    if validated_pairs is not None:
        validated_pairs.add(pair)
    return reserved, caller


def validate_correlated_caller(
    tap: GitTap,
    state: Mapping[str, Any],
    intent: PendingDispatch,
    caller_tap_sha: str,
    *,
    snapshots: dict[str, TapSnapshot] | None = None,
    validated_pairs: set[tuple[str, str]] | None = None,
) -> None:
    reserved, _caller = validate_caller_source_pair(
        tap,
        intent.formula,
        intent.tap_sha,
        caller_tap_sha,
        snapshots=snapshots,
        validated_pairs=validated_pairs,
    )
    # WHY: the pair check proves what changed after dispatch; this independent
    # check proves the controller originally reserved the campaign-wide catalog.
    if catalog_state(reserved) != state.get("catalog"):
        raise RolloutError(
            f"reserved source catalog for {intent.formula} differs from the ledger"
        )
    if reserved.formula_support_tree != state.get("formula_support_tree"):
        raise RolloutError(
            f"reserved source support for {intent.formula} differs from the ledger"
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
                generation_sha, generation_tag = approved_package_generation(
                    workflow_sha256(source_snapshot)
                )
                if source_consumer != expected_kandelo_sha:
                    raise RolloutError(
                        "historical caller selected another package consumer"
                    )
                validate_workflow_source(
                    source_snapshot,
                    expected_kandelo_sha,
                    expected_publisher_sha=source_publisher,
                    expected_package_generation_sha=generation_sha,
                    expected_package_generation_tag=generation_tag,
                    allow_legacy_tap_ref=source_selector == "main",
                    allow_legacy_run_name=True,
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


def _workflow_run_page(
    github: GitHub,
    *,
    page: int,
    created: str | None,
) -> tuple[int, tuple[Mapping[str, Any], ...]]:
    response = github.runs(
        per_page=WORKFLOW_RUN_PAGE_SIZE,
        page=page,
        created=created,
    )
    total_count = response.get("total_count")
    runs = response.get("workflow_runs")
    expected_count = (
        max(
            0,
            min(
                WORKFLOW_RUN_PAGE_SIZE,
                total_count - ((page - 1) * WORKFLOW_RUN_PAGE_SIZE),
            ),
        )
        if isinstance(total_count, int) and not isinstance(total_count, bool)
        else -1
    )
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
        or not isinstance(runs, list)
        or len(runs) != expected_count
    ):
        raise WorkflowRunSnapshotError(
            f"GitHub returned an incomplete workflow run page {page}"
        )
    for run in runs:
        if (
            not isinstance(run, dict)
            or isinstance(run.get("id"), bool)
            or not isinstance(run.get("id"), int)
            or run["id"] <= 0
        ):
            raise RolloutError("GitHub returned a malformed workflow run")
    return total_count, tuple(runs)


def _collect_workflow_run_snapshot(
    github: GitHub,
    *,
    created: str | None,
) -> tuple[Mapping[str, Any], ...]:
    total_count, first_page = _workflow_run_page(
        github,
        page=1,
        created=created,
    )
    page_count = max(
        1,
        (total_count + WORKFLOW_RUN_PAGE_SIZE - 1) // WORKFLOW_RUN_PAGE_SIZE,
    )
    if page_count > MAX_WORKFLOW_RUN_PAGES:
        raise RolloutError(
            f"workflow run snapshot requires {page_count} pages; "
            f"the safety limit is {MAX_WORKFLOW_RUN_PAGES}"
        )
    runs = list(first_page)
    for page in range(2, page_count + 1):
        page_total, page_runs = _workflow_run_page(
            github,
            page=page,
            created=created,
        )
        if page_total != total_count:
            raise WorkflowRunSnapshotError(
                "GitHub changed workflow run total_count during pagination"
            )
        runs.extend(page_runs)
    if len(runs) != total_count:
        raise WorkflowRunSnapshotError(
            f"GitHub returned {len(runs)} of {total_count} workflow runs"
        )
    run_ids = [run["id"] for run in runs]
    if len(run_ids) != len(set(run_ids)):
        raise WorkflowRunSnapshotError(
            "GitHub returned duplicate workflow runs across pages"
        )
    return tuple(runs)


def workflow_run_snapshot(
    github: GitHub,
    *,
    created: str | None = None,
) -> tuple[Mapping[str, Any], ...]:
    for attempt in range(WORKFLOW_RUN_SNAPSHOT_ATTEMPTS):
        try:
            first = _collect_workflow_run_snapshot(github, created=created)
            second = _collect_workflow_run_snapshot(github, created=created)
            # WHY: page insertion can preserve total_count while shifting an
            # item across the pagination boundary. Two identical complete
            # listings prove the controller did not correlate against a torn
            # view, and duplicate IDs are rejected in each collection.
            if first != second:
                raise WorkflowRunSnapshotError(
                    "GitHub changed the workflow run snapshot during collection"
                )
            return second
        except WorkflowRunSnapshotError:
            if attempt + 1 == WORKFLOW_RUN_SNAPSHOT_ATTEMPTS:
                raise
            time.sleep(WORKFLOW_RUN_SNAPSHOT_BACKOFF_SECONDS * (2**attempt))
    raise AssertionError("workflow run snapshot retry loop did not return or raise")


def active_inventory(github: GitHub) -> RunInventory:
    # One unfiltered, stable listing avoids losing a run while GitHub moves it
    # between requested, queued, waiting, pending, and in-progress states.
    all_runs = workflow_run_snapshot(github)
    runs_by_id: dict[int, Mapping[str, Any]] = {}
    for run in all_runs:
        status = run.get("status")
        if not isinstance(status, str):
            raise RolloutError("GitHub returned a workflow run without a status")
        if status in ACTIVE_STATUSES:
            runs_by_id[run["id"]] = run
    formulae: dict[int, frozenset[str]] = {}
    unknown: list[int] = []
    for run_id in sorted(runs_by_id):
        found = run_formulae(github.jobs(run_id))
        formulae[run_id] = found
        if not found:
            unknown.append(run_id)
    return RunInventory(
        count=len(runs_by_id),
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
    recorded_run_ids: set[int] = set()
    for entry in state.get("dispatches", ()):
        if not isinstance(entry, dict):
            continue
        run_id = entry.get("run_id")
        formula = entry.get("formula")
        if (
            not isinstance(run_id, int)
            or formula not in FORMULA_ORDER
        ):
            continue
        recorded_run_ids.add(run_id)
        if run_id in runs_by_id:
            # The run name token is visible before the reusable workflow emits
            # its matrix jobs. The ledger already binds this exact run ID to its
            # Formula, so treating it as unknown would serialize the next pass.
            formulae[run_id] = frozenset((formula,))
            continue
        run = github.run(run_id)
        # A run can complete or change status immediately after the stable
        # listing. The durable ledger remains authoritative for every run this
        # sole dispatcher created until its direct status says completed.
        if run.get("status") != "completed":
            runs_by_id[run_id] = run
            formulae[run_id] = frozenset((formula,))
            count += 1
    return RunInventory(
        count=count,
        runs=tuple(runs_by_id.values()),
        formulae=formulae,
        unknown_run_ids=tuple(
            run_id
            for run_id in inventory.unknown_run_ids
            if run_id not in recorded_run_ids
        ),
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


def last_green_catalog_state(snapshot: TapSnapshot) -> dict[str, Any]:
    """Freeze the finalized sidecar identity behind each current Formula."""
    result: dict[str, Any] = {}
    for formula in FORMULA_ORDER:
        identity = snapshot.identities[formula]
        sidecar = snapshot.formula_sidecars.get(formula)
        if not isinstance(sidecar, dict):
            raise RolloutError(
                f"{formula} has no package-owned last-green sidecar"
            )
        rebuild = sidecar.get("bottle_rebuild")
        if (
            sidecar.get("name") != formula
            or sidecar.get("version") != identity.pkg_version
            or sidecar.get("formula_revision") != identity.formula_revision
            or isinstance(rebuild, bool)
            or not isinstance(rebuild, int)
            or rebuild < 0
            or rebuild > identity.bottle_rebuild
        ):
            raise RolloutError(
                f"{formula} sidecar does not describe a finalized predecessor"
            )
        bottles = _bottles_by_arch(sidecar, f"last-green {formula}")
        if set(bottles) != set(identity.arches):
            raise RolloutError(
                f"{formula} last-green sidecar does not cover every architecture"
            )
        for arch in identity.arches:
            digest = bottles[arch].get("sha256")
            if (
                bottles[arch].get("status", "success") != "success"
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or identity.bottle_sha256.get(arch) != digest
            ):
                raise RolloutError(
                    f"Formula/{formula}.rb does not retain the last-green "
                    f"{arch} checksum"
                )
        finalized_source = source_with_rebuild(
            snapshot.formula_sources[formula],
            formula,
            rebuild,
        )
        result[formula] = {
            "version": identity.pkg_version,
            "formula_revision": identity.formula_revision,
            "bottle_rebuild": rebuild,
            "arches": list(identity.arches),
            "formula_contract_sha256": formula_contract_sha256(
                formula, finalized_source
            ),
            "dependencies": sorted(snapshot.dependencies[formula]),
        }
    return result


def require_reuse_target_abi(
    snapshot: TapSnapshot, formulae: Sequence[str]
) -> None:
    """Require every retained bottle to already satisfy this campaign ABI."""
    for formula in formulae:
        sidecar = snapshot.formula_sidecars.get(formula)
        if not isinstance(sidecar, dict):
            raise RolloutError(f"{formula} reuse Formula has no finalized sidecar")
        if sidecar.get("kandelo_abi") != EXPECTED_ABI:
            raise RolloutError(
                f"{formula} reuse sidecar ABI is not {EXPECTED_ABI}"
            )
        bottles = _bottles_by_arch(sidecar, f"reuse {formula}")
        for arch in required_arches(formula):
            bottle = bottles.get(arch)
            if not isinstance(bottle, dict):
                raise RolloutError(
                    f"{formula} reuse sidecar omits required architecture {arch}"
                )
            if bottle.get("kandelo_abi") != EXPECTED_ABI:
                raise RolloutError(
                    f"{formula} reuse {arch} bottle ABI is not {EXPECTED_ABI}"
                )


def catalog_identity_value(
    value: Mapping[str, Any],
    label: str,
    *,
    allow_zero_rebuild: bool = False,
) -> dict[str, Any]:
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
        or bottle_rebuild < (0 if allow_zero_rebuild else 1)
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
        if authority is None and workflow_hash in APPROVED_CAMPAIGN_CONTRACTS:
            publisher, consumer, _generation_sha, _generation_tag = (
                APPROVED_CAMPAIGN_CONTRACTS[workflow_hash]
            )
            authority = (publisher, consumer, "exact")
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
    if not isinstance(state, dict) or state.get("schema") not in (1, 2, 3, 4):
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
        # WHY: fsyncing the file before rename preserves its bytes, but not the
        # directory entry that makes the replacement discoverable after a host
        # crash. Persist the parent after os.replace so a durable request marker
        # cannot silently roll back and authorize a duplicate publication.
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_new_state(path: pathlib.Path, state: Mapping[str, Any]) -> None:
    """Atomically create a ledger without ever replacing an existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    linked = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            # WHY: os.replace would silently destroy a campaign ledger that
            # appeared while the lengthy registry preflight was running. A
            # same-directory hard link provides atomic create-if-absent.
            os.link(temporary, path)
            linked = True
        except FileExistsError as error:
            raise RolloutError(
                f"fresh campaign state {path} appeared during initialization"
            ) from error
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
        if linked:
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)


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
        "pending_dispatches": [],
        "dispatches": [],
    }


def validate_campaign_contract(contract: CampaignContract) -> None:
    for label, value in (
        ("publisher SHA", contract.publisher_sha),
        ("consumer SHA", contract.consumer_sha),
        ("package-generation SHA", contract.package_generation_sha),
    ):
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9a-f]{40}", value
        ):
            raise RolloutError(f"campaign {label} must be exactly 40 lowercase hex")
    if not isinstance(contract.workflow_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", contract.workflow_sha256
    ):
        raise RolloutError(
            "campaign workflow SHA-256 must be exactly 64 lowercase hex"
        )
    if (
        not isinstance(contract.package_generation_tag, str)
        or not PACKAGE_GENERATION_TAG_RE.fullmatch(
            contract.package_generation_tag
        )
    ):
        raise RolloutError("campaign package-generation tag is invalid")
    approved = APPROVED_CAMPAIGN_CONTRACTS.get(contract.workflow_sha256)
    if approved != (
        contract.publisher_sha,
        contract.consumer_sha,
        contract.package_generation_sha,
        contract.package_generation_tag,
    ):
        # WHY: validating individual YAML scalars does not reject an extra
        # credential-bearing job. Only a reviewed hash of the complete caller
        # is allowed to create a write-capable campaign trust root.
        raise RolloutError(
            "campaign publication contract is not an exact reviewed authority"
        )


def campaign_contract_from_state(
    state: Mapping[str, Any],
    expected_kandelo_sha: str,
) -> CampaignContract | None:
    if state.get("schema") == 1:
        campaign_only = {
            "base_catalog",
            "campaign",
            "initial_catalog",
            "previous_catalog",
            "previous_formula_sidecar_tree",
            "previous_formula_support_tree",
        }
        if campaign_only & set(state):
            # WHY: changing only `schema: 2` to `schema: 1` must not suppress
            # campaign authority checks or the anonymous pre-dispatch recheck.
            raise RolloutError(
                "schema-1 rollout state contains schema-2 campaign fields"
            )
        return None
    if state.get("schema") not in (2, 3, 4):
        raise RolloutError("rollout state has an unsupported schema")
    campaign = state.get("campaign")
    if not isinstance(campaign, dict):
        raise RolloutError("campaign rollout state has no campaign contract")
    try:
        contract = CampaignContract(
            publisher_sha=campaign["expected_publisher_sha"],
            consumer_sha=campaign["expected_consumer_sha"],
            package_generation_sha=campaign[
                "expected_package_generation_sha"
            ],
            package_generation_tag=campaign[
                "expected_package_generation_tag"
            ],
            workflow_sha256=campaign["expected_workflow_sha256"],
        )
    except (KeyError, TypeError) as error:
        raise RolloutError(
            "campaign rollout state has a malformed campaign contract"
        ) from error
    if any(not isinstance(value, str) for value in dataclasses.astuple(contract)):
        raise RolloutError(
            "campaign rollout state has a malformed campaign contract"
        )
    validate_campaign_contract(contract)
    if contract.consumer_sha != expected_kandelo_sha:
        raise RolloutError(
            "campaign rollout state selects another package consumer"
        )
    return contract


def campaign_selection_from_state(
    state: Mapping[str, Any],
) -> CampaignSelection | None:
    schema = state.get("schema")
    if schema == 1:
        return None
    if schema == 2:
        return CampaignSelection.all_rebuild()
    if schema not in (3, 4):
        raise RolloutError("rollout state has an unsupported schema")
    campaign = state.get("campaign")
    if not isinstance(campaign, dict):
        raise RolloutError("campaign rollout state has no campaign selection")
    try:
        return CampaignSelection.create(
            rebuild=campaign["rebuild_formulae"],
            reuse=campaign["reuse_formulae"],
            deferred=campaign["deferred_formulae"],
        )
    except (KeyError, TypeError) as error:
        raise RolloutError(
            "campaign rollout state has a malformed Formula partition"
        ) from error


def campaign_reservations(
    snapshot: TapSnapshot,
    formulae: Sequence[str] = FORMULA_ORDER,
) -> list[dict[str, str]]:
    reservations = [
        {
            "formula": formula,
            "arch": arch,
            "reference": snapshot.identities[formula].top_reference,
        }
        for formula in formulae
        for arch in required_arches(formula)
    ]
    expected_count = sum(len(required_arches(formula)) for formula in formulae)
    if len(reservations) != expected_count:
        raise RolloutError(
            "campaign reservation set does not contain every selected "
            "architecture identity"
        )
    return reservations


def campaign_reservations_from_catalog(
    catalog: Mapping[str, Any],
    *,
    rebuild_increment: int = 0,
    formulae: Sequence[str] = FORMULA_ORDER,
) -> list[dict[str, str]]:
    reservations: list[dict[str, str]] = []
    for formula in formulae:
        identity = catalog_identity_value(
            catalog.get(formula),
            f"campaign {formula} catalog",
            allow_zero_rebuild=rebuild_increment > 0,
        )
        reference = homebrew_top_reference(
            identity["version"],
            identity["bottle_rebuild"] + rebuild_increment,
        )
        for arch in identity["arches"]:
            reservations.append(
                {"formula": formula, "arch": arch, "reference": reference}
            )
    expected_count = sum(
        len(
            catalog_identity_value(
                catalog.get(formula),
                f"campaign {formula} catalog",
                allow_zero_rebuild=rebuild_increment > 0,
            )["arches"]
        )
        for formula in formulae
    )
    if len(reservations) != expected_count:
        raise RolloutError(
            "campaign catalog does not contain every selected architecture identity"
        )
    return reservations


def initial_campaign_state(
    snapshot: TapSnapshot,
    *,
    campaign_id: str,
    base_snapshot: TapSnapshot,
    contract: CampaignContract,
    absent_oci_references: Mapping[str, str],
    checked_at: str,
    selection: CampaignSelection | None = None,
    manifest: CampaignManifest | None = None,
    manifest_authority_sha: str | None = None,
    reservation_snapshot: TapSnapshot | None = None,
) -> dict[str, Any]:
    if not isinstance(campaign_id, str) or not CAMPAIGN_ID_RE.fullmatch(
        campaign_id
    ):
        raise RolloutError("campaign ID is invalid")
    validate_campaign_contract(contract)
    selected_reservation = reservation_snapshot or snapshot
    if selected_reservation.sha == base_snapshot.sha:
        raise RolloutError(
            "campaign reservation commit must differ from its base commit"
        )
    if manifest is not None:
        if selection is not None:
            raise RolloutError(
                "operator Formula partitions cannot override a manifest-backed campaign"
            )
        if (
            manifest_authority_sha != snapshot.sha
            or selected_reservation.sha != manifest.reservation_tap_sha
            or base_snapshot.sha != manifest.base_tap_sha
        ):
            raise RolloutError("campaign manifest authority differs from campaign sources")
        selected_campaign = manifest.selection
    else:
        if manifest_authority_sha is not None or reservation_snapshot is not None:
            raise RolloutError("campaign manifest inputs must be supplied together")
        selected_campaign = selection or CampaignSelection.all_rebuild()
    state = initial_state(snapshot, contract.consumer_sha)
    state["schema"] = 4 if manifest is not None else (3 if selection is not None else 2)
    state["expected_publisher_sha"] = contract.publisher_sha
    state["workflow_sha256"] = contract.workflow_sha256
    state["previous_catalog"] = last_green_catalog_state(base_snapshot)
    state["base_catalog"] = catalog_state(base_snapshot)
    state["initial_catalog"] = catalog_state(selected_reservation)
    state["previous_formula_support_tree"] = base_snapshot.formula_support_tree
    state["previous_formula_sidecar_tree"] = base_snapshot.formula_sidecar_tree
    state["campaign"] = {
        "id": campaign_id,
        "base_tap_sha": base_snapshot.sha,
        "reservation_tap_sha": selected_reservation.sha,
        "initialized_at": _utc_now(),
        **contract.state_value(),
        "prior_kandelo_sha": base_snapshot.metadata.get("kandelo_commit"),
        "formulae": list(selected_campaign.rebuild),
        "architecture_identity_count": sum(
            len(required_arches(formula))
            for formula in selected_campaign.rebuild
        ),
        # WHY: only payloads whose build closure changed acquire a new OCI
        # identity. Reused and deferred Formulae remain explicit in schema 3,
        # but reserving them would turn validation into an accidental rebuild.
        "reservations": campaign_reservations(
            selected_reservation, selected_campaign.rebuild
        ),
        "absent_oci_references": dict(absent_oci_references),
        "absent_oci_checked_at": checked_at,
    }
    if selection is not None or manifest is not None:
        state["campaign"].update(selected_campaign.state_value())
    if manifest is not None:
        state["campaign"].update(
            {
                "manifest_path": CAMPAIGN_MANIFEST_PATH,
                "manifest_sha256": manifest.sha256,
                "manifest_tap_sha": manifest_authority_sha,
            }
        )
    return state


def validate_campaign_state(
    state: Mapping[str, Any],
    snapshot: TapSnapshot,
    expected_kandelo_sha: str,
) -> CampaignContract | None:
    contract = campaign_contract_from_state(state, expected_kandelo_sha)
    if contract is None:
        return None
    campaign = state.get("campaign")
    assert isinstance(campaign, dict)
    selection = campaign_selection_from_state(state)
    assert selection is not None
    expected_keys = {
        "absent_oci_checked_at",
        "absent_oci_references",
        "architecture_identity_count",
        "base_tap_sha",
        "expected_consumer_sha",
        "expected_package_generation_sha",
        "expected_package_generation_tag",
        "expected_publisher_sha",
        "expected_workflow_sha256",
        "formulae",
        "id",
        "initialized_at",
        "prior_kandelo_sha",
        "reservation_tap_sha",
        "reservations",
    }
    if state.get("schema") in (3, 4):
        expected_keys.update(
            ("rebuild_formulae", "reuse_formulae", "deferred_formulae")
        )
    if state.get("schema") == 4:
        expected_keys.update(
            ("manifest_path", "manifest_sha256", "manifest_tap_sha")
        )
    if set(campaign) != expected_keys:
        raise RolloutError("campaign rollout state has an unexpected shape")
    previous_catalog = state.get("previous_catalog")
    base_catalog = state.get("base_catalog")
    initial_catalog = state.get("initial_catalog")
    if (
        not isinstance(previous_catalog, dict)
        or not isinstance(base_catalog, dict)
        or not isinstance(initial_catalog, dict)
    ):
        raise RolloutError(
            "campaign rollout state does not retain its last-green, base, "
            "and initial catalogs"
        )
    initial_reservations = campaign_reservations_from_catalog(
        initial_catalog,
        formulae=selection.rebuild,
    )
    initial_references = {
        entry["formula"]: entry["reference"]
        for entry in initial_reservations
    }
    if (
        not isinstance(campaign.get("id"), str)
        or not CAMPAIGN_ID_RE.fullmatch(campaign["id"])
        or not isinstance(campaign.get("base_tap_sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", campaign["base_tap_sha"])
        or not isinstance(campaign.get("reservation_tap_sha"), str)
        or not re.fullmatch(
            r"[0-9a-f]{40}", campaign["reservation_tap_sha"]
        )
        or campaign.get("base_tap_sha")
        == campaign.get("reservation_tap_sha")
        or campaign.get("formulae") != list(selection.rebuild)
        or campaign.get("architecture_identity_count")
        != sum(
            len(required_arches(formula))
            for formula in selection.rebuild
        )
        or campaign.get("reservations") != initial_reservations
        or campaign.get("absent_oci_references")
        != initial_references
        or not isinstance(campaign.get("absent_oci_checked_at"), str)
        or not campaign["absent_oci_checked_at"]
        or not isinstance(campaign.get("initialized_at"), str)
        or not campaign["initialized_at"]
        or state.get("cutover_tap_sha")
        != (
            campaign.get("manifest_tap_sha")
            if state.get("schema") == 4
            else campaign["reservation_tap_sha"]
        )
        or state.get("expected_publisher_sha") != contract.publisher_sha
        or state.get("workflow_sha256") != contract.workflow_sha256
    ):
        raise RolloutError(
            "campaign rollout state differs from its complete reservation contract"
        )
    if state.get("schema") == 4 and (
        campaign.get("manifest_path") != CAMPAIGN_MANIFEST_PATH
        or not isinstance(campaign.get("manifest_tap_sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", campaign["manifest_tap_sha"])
        or not isinstance(campaign.get("manifest_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", campaign["manifest_sha256"])
    ):
        raise RolloutError(
            "campaign rollout state has a malformed manifest authority"
        )
    parse_github_time(campaign["initialized_at"], "campaign initialized_at")
    parse_github_time(
        campaign["absent_oci_checked_at"], "campaign absent_oci_checked_at"
    )
    if (
        not isinstance(campaign.get("prior_kandelo_sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", campaign["prior_kandelo_sha"])
    ):
        raise RolloutError("campaign prior Kandelo SHA is invalid")

    if (
        set(previous_catalog) != set(FORMULA_ORDER)
        or set(base_catalog) != set(FORMULA_ORDER)
        or set(initial_catalog) != set(FORMULA_ORDER)
        or state.get("previous_formula_support_tree")
        != snapshot.formula_support_tree
        or not isinstance(state.get("previous_formula_sidecar_tree"), str)
        or not re.fullmatch(
            r"[0-9a-f]{40}", state["previous_formula_sidecar_tree"]
        )
    ):
        raise RolloutError(
            "campaign rollout state does not retain its complete last-green base"
        )
    current_catalog = catalog_state(snapshot)
    for formula in FORMULA_ORDER:
        previous = catalog_identity_value(
            previous_catalog.get(formula),
            f"campaign previous {formula} catalog",
            allow_zero_rebuild=True,
        )
        base = catalog_identity_value(
            base_catalog.get(formula),
            f"campaign base {formula} catalog",
        )
        initial = catalog_identity_value(
            initial_catalog.get(formula),
            f"campaign initial {formula} catalog",
        )
        current = catalog_identity_value(
            current_catalog[formula],
            f"campaign reserved {formula} catalog",
        )
        for field in ("version", "formula_revision", "arches", "dependencies"):
            if (
                previous[field] != base[field]
                or base[field] != initial[field]
                or initial[field] != current[field]
            ):
                raise RolloutError(
                    f"campaign reservation changes stable {formula} field {field}"
                )
        if previous["bottle_rebuild"] > base["bottle_rebuild"]:
            raise RolloutError(
                f"campaign {formula} base predates its last-green sidecar"
            )
        if formula in selection.rebuild:
            if initial["bottle_rebuild"] != base["bottle_rebuild"] + 1:
                raise RolloutError(
                    f"campaign {formula} initial reservation is not the base successor"
                )
            if current["bottle_rebuild"] < initial["bottle_rebuild"]:
                raise RolloutError(
                    f"campaign {formula} reservation predates its fresh identity"
                )
        elif initial != base or current != base:
            # WHY: a reuse or deferred payload owns no fresh OCI identity in
            # this campaign. Any Formula identity movement must be reviewed as
            # a later campaign rather than hidden beside a selected rebuild.
            raise RolloutError(
                f"campaign {formula} changed outside the rebuild partition"
            )
    return contract


def validate_campaign_main_descendant(
    tap: GitTap,
    state: Mapping[str, Any],
    snapshot: TapSnapshot,
) -> CampaignManifest | None:
    if state.get("schema") not in (2, 3, 4):
        return None
    campaign = state.get("campaign")
    if not isinstance(campaign, dict):
        raise RolloutError("campaign rollout state has no campaign contract")
    reservation_sha = campaign.get("reservation_tap_sha")
    base_sha = campaign.get("base_tap_sha")
    if not isinstance(reservation_sha, str) or not isinstance(base_sha, str):
        raise RolloutError("campaign base or reservation tap SHA is malformed")
    base = load_snapshot(tap, base_sha)
    reservation = load_snapshot(tap, reservation_sha)
    selection = campaign_selection_from_state(state)
    assert selection is not None
    validate_fresh_campaign_reservations(
        tap=tap,
        base=base,
        reservation=reservation,
        selection=selection,
    )
    if (
        last_green_catalog_state(base) != state.get("previous_catalog")
        or catalog_state(base) != state.get("base_catalog")
        or catalog_state(reservation) != state.get("initial_catalog")
        or reservation.formula_support_tree != state.get("formula_support_tree")
        or reservation.formula_sidecar_tree
        != state.get("previous_formula_sidecar_tree")
    ):
        raise RolloutError(
            "campaign reservation commit differs from the private ledger"
        )

    manifest_anchor_sha: str | None = None
    manifest: CampaignManifest | None = None
    if state.get("schema") == 4:
        manifest_anchor_sha = campaign.get("manifest_tap_sha")
        manifest_digest = campaign.get("manifest_sha256")
        if (
            not isinstance(manifest_anchor_sha, str)
            or not isinstance(manifest_digest, str)
        ):
            raise RolloutError("campaign ledger lacks its manifest authority")
        manifest = load_campaign_manifest(
            tap,
            manifest_anchor_sha,
            expected_sha256=manifest_digest,
        )
        if (
            manifest.base_tap_sha != base_sha
            or manifest.reservation_tap_sha != reservation_sha
            or manifest.selection != selection
        ):
            raise RolloutError(
                "campaign manifest authority differs from the private ledger"
            )
        validate_campaign_manifest_sources(
            tap,
            manifest,
            base,
            reservation,
        )
        if (
            not tap.is_ancestor(reservation_sha, manifest_anchor_sha)
            or not tap.is_ancestor(manifest_anchor_sha, snapshot.sha)
        ):
            raise RolloutError(
                "campaign manifest authority is not on protected main history"
            )
        manifest_snapshot = load_snapshot(tap, manifest_anchor_sha)
        if (
            catalog_state(manifest_snapshot) != state.get("catalog")
            or manifest_snapshot.formula_support_tree
            != state.get("formula_support_tree")
            or manifest_snapshot.formula_sidecar_tree
            != state.get("previous_formula_sidecar_tree")
        ):
            raise RolloutError(
                "campaign manifest commit changes its reserved publication sources"
            )

    # Failed-run recovery may legitimately reserve a later rebuild for one or
    # more occupied identities. Those reviewed replacement commits are part of
    # the ledger; all later movement must again be finalizer-only.
    anchor_shas = [
        entry.get("replacement_tap_sha")
        for entry in reversed(state.get("failed_attempts", []))
        if isinstance(entry, dict)
    ]
    anchor_shas.append(manifest_anchor_sha or reservation_sha)
    anchor: TapSnapshot | None = None
    for candidate_sha in anchor_shas:
        if (
            not isinstance(candidate_sha, str)
            or not tap.is_ancestor(
                manifest_anchor_sha or reservation_sha, candidate_sha
            )
            or not tap.is_ancestor(candidate_sha, snapshot.sha)
        ):
            continue
        candidate = load_snapshot(tap, candidate_sha)
        if (
            catalog_state(candidate) == state.get("catalog")
            and candidate.formula_support_tree
            == state.get("formula_support_tree")
        ):
            anchor = candidate
            break
    if anchor is None:
        raise RolloutError(
            "current campaign catalog has no reviewed reservation anchor"
        )
    if snapshot.sha != anchor.sha:
        validate_caller_source_pair(
            tap,
            "campaign",
            anchor.sha,
            snapshot.sha,
        )
    return manifest


def verify_manifest_backed_campaign(
    tap: GitTap,
    registry: Any,
    state: Mapping[str, Any],
) -> CampaignManifest | None:
    if state.get("schema") != 4:
        return None
    campaign = state.get("campaign")
    if not isinstance(campaign, dict):
        raise RolloutError("manifest-backed campaign has no campaign contract")
    manifest_tap_sha = campaign.get("manifest_tap_sha")
    manifest_sha256 = campaign.get("manifest_sha256")
    base_tap_sha = campaign.get("base_tap_sha")
    reservation_tap_sha = campaign.get("reservation_tap_sha")
    if any(
        not isinstance(value, str)
        for value in (
            manifest_tap_sha,
            manifest_sha256,
            base_tap_sha,
            reservation_tap_sha,
        )
    ):
        raise RolloutError("manifest-backed campaign authority is malformed")
    manifest = load_campaign_manifest(
        tap,
        manifest_tap_sha,
        expected_sha256=manifest_sha256,
    )
    if (
        manifest.base_tap_sha != base_tap_sha
        or manifest.reservation_tap_sha != reservation_tap_sha
        or manifest.selection != campaign_selection_from_state(state)
    ):
        raise RolloutError(
            "manifest-backed campaign selection differs from the private ledger"
        )
    base = load_snapshot(tap, base_tap_sha)
    reservation = load_snapshot(tap, reservation_tap_sha)
    validate_campaign_manifest_sources(tap, manifest, base, reservation)
    verify_campaign_reuse_blobs(registry, manifest)
    return manifest


def validate_campaign_recovery_transition(
    tap: GitTap,
    state: Mapping[str, Any],
    current: TapSnapshot,
) -> None:
    if state.get("schema") != 2:
        return
    campaign = state.get("campaign")
    if not isinstance(campaign, dict):
        raise RolloutError("campaign rollout state has no campaign contract")
    candidates = [
        entry.get("replacement_tap_sha")
        for entry in reversed(state.get("failed_attempts", []))
        if isinstance(entry, dict)
    ]
    candidates.extend(
        entry.get("tap_sha")
        for entry in reversed(state.get("dispatches", []))
        if isinstance(entry, dict)
    )
    candidates.append(campaign.get("reservation_tap_sha"))
    anchor: TapSnapshot | None = None
    for candidate_sha in candidates:
        if (
            not isinstance(candidate_sha, str)
            or not tap.is_ancestor(candidate_sha, current.sha)
        ):
            continue
        candidate = load_snapshot(tap, candidate_sha)
        if (
            catalog_state(candidate) == state.get("catalog")
            and candidate.formula_support_tree
            == state.get("formula_support_tree")
        ):
            anchor = candidate
            break
    if anchor is None:
        raise RolloutError(
            "campaign recovery has no reviewed pre-recovery catalog anchor"
        )
    unsafe = [
        f"{status} {path}"
        for status, path in tap.changed_entries(anchor.sha, current.sha)
        if not finalizer_owned_change(status, path)
    ]
    if unsafe:
        # Formula source and generated finalizer paths receive their own exact
        # catalog/rebuild checks. A recovery commit is not authority to carry
        # unrelated documentation, controller, policy, or workflow drift.
        raise RolloutError(
            "campaign recovery includes unrelated tap changes: "
            + ", ".join(unsafe)
        )


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

    old_authority = APPROVED_PUBLICATION_WORKFLOWS.get(old_workflow)
    new_authority = APPROVED_PUBLICATION_WORKFLOWS.get(new_workflow)
    if (
        old_authority is None
        or new_authority is None
        or old_authority[:2] != (old_publisher, expected_kandelo_sha)
        or new_authority[:2] != (new_publisher, expected_kandelo_sha)
    ):
        # WHY: the private ledger cannot bless an arbitrary digest by naming it
        # as its previous caller. Every link must already be an exact reviewed
        # publication authority with the same frozen package consumer.
        raise RolloutError(
            "rollout state workflow_sha256 differs from the reviewed "
            "single-intent or token-correlated workflow"
        )

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


def upgrade_state(
    state: Mapping[str, Any],
    snapshot: TapSnapshot,
    expected_kandelo_sha: str,
) -> dict[str, Any]:
    """Add batching state while preserving the reviewed workflow trust chain."""
    upgraded = copy.deepcopy(state)
    if "pending_dispatches" not in upgraded:
        upgraded["pending_dispatches"] = []
    if upgraded.get("schema") in (2, 3, 4):
        # A fresh campaign starts with batching and an exact complete caller
        # authority. Rotating it implicitly would make the private ledger bless
        # code outside the initialization review.
        validate_state(upgraded, snapshot, expected_kandelo_sha)
        return upgraded
    upgraded = migrate_workflow_trust(
        upgraded,
        snapshot,
        expected_kandelo_sha,
    )
    validate_state(upgraded, snapshot, expected_kandelo_sha)
    return upgraded


def validate_state(
    state: Mapping[str, Any],
    snapshot: TapSnapshot,
    expected_kandelo_sha: str,
) -> None:
    if state.get("schema") not in (1, 2, 3, 4):
        raise RolloutError("rollout state has an unsupported schema")
    campaign_contract = validate_campaign_state(
        state, snapshot, expected_kandelo_sha
    )
    campaign_selection = campaign_selection_from_state(state)
    campaign_rebuilds = (
        set(campaign_selection.rebuild)
        if campaign_selection is not None
        else set(FORMULA_ORDER)
    )
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
    if campaign_contract is not None:
        if (
            campaign_contract.publisher_sha
            != state.get("expected_publisher_sha")
            or campaign_contract.workflow_sha256
            != state.get("workflow_sha256")
        ):
            raise RolloutError(
                "campaign authority differs from active rollout trust"
            )
    trusted_publishers = trusted_workflow_publishers(state)
    dispatches = state.get("dispatches")
    if not isinstance(dispatches, list):
        raise RolloutError("rollout state dispatches is not an array")
    seen_formulae: set[str] = set()
    seen_run_ids: set[int] = set()
    seen_tokens: set[str] = set()
    for entry in dispatches:
        if not isinstance(entry, dict):
            raise RolloutError("rollout state contains a malformed dispatch")
        dispatch_token = entry.get("dispatch_token")
        expected_entry_keys = {
            "arches",
            "formula",
            "run_id",
            "submitted_at",
            "tap_sha",
        }
        if dispatch_token is not None:
            expected_entry_keys.update(("caller_tap_sha", "dispatch_token"))
        formula = entry.get("formula")
        run_id = entry.get("run_id")
        tap_sha = entry.get("tap_sha")
        caller_tap_sha = entry.get("caller_tap_sha")
        submitted_at = entry.get("submitted_at")
        if formula in FORMULA_ORDER and formula not in campaign_rebuilds:
            raise RolloutError(
                "rollout state dispatches outside its campaign rebuild partition"
            )
        if (
            set(entry) != expected_entry_keys
            or formula not in campaign_rebuilds
            or entry.get("arches") != list(required_arches(formula))
            or type(run_id) is not int
            or run_id <= 0
            or not isinstance(tap_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", tap_sha)
            or not isinstance(submitted_at, str)
            or not submitted_at
            or (
                dispatch_token is not None
                and (
                    not isinstance(dispatch_token, str)
                    or not DISPATCH_TOKEN_RE.fullmatch(dispatch_token)
                )
            )
            or (
                dispatch_token is not None
                and (
                    not isinstance(caller_tap_sha, str)
                    or not re.fullmatch(r"[0-9a-f]{40}", caller_tap_sha)
                )
            )
        ):
            raise RolloutError("rollout state contains a malformed dispatch")
        if formula in seen_formulae or run_id in seen_run_ids:
            raise RolloutError("rollout state contains a duplicate dispatch")
        if dispatch_token is not None and dispatch_token in seen_tokens:
            raise RolloutError("rollout state contains a duplicate dispatch token")
        seen_formulae.add(formula)
        seen_run_ids.add(run_id)
        if dispatch_token is not None:
            seen_tokens.add(dispatch_token)

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
        if formula in FORMULA_ORDER and formula not in campaign_rebuilds:
            raise RolloutError(
                "rollout state abandons a Formula outside its campaign "
                "rebuild partition"
            )
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
        if (
            isinstance(entry, dict)
            and entry.get("formula") in FORMULA_ORDER
            and entry["formula"] not in campaign_rebuilds
        ):
            raise RolloutError(
                "rollout state recovers a Formula outside its campaign "
                "rebuild partition"
            )
        validate_failed_attempt(
            entry,
            seen_run_ids,
            expected_consumer_sha=expected_kandelo_sha,
            trusted_publishers=trusted_publishers,
        )

    pending = pending_dispatches(state)
    for intent in pending:
        if intent.formula not in campaign_rebuilds:
            raise RolloutError(
                "rollout state dispatches outside its campaign rebuild partition"
            )
        if intent.formula in seen_formulae:
            raise RolloutError("rollout state contains a duplicate dispatch Formula")
        if intent.dispatch_token in seen_tokens:
            raise RolloutError("rollout state contains a duplicate dispatch token")
        seen_formulae.add(intent.formula)
        seen_tokens.add(intent.dispatch_token)

    if state.get("unresolved_dispatch") is not None:
        legacy = submitted_dispatch(state)
        if legacy.formula not in campaign_rebuilds:
            raise RolloutError(
                "rollout state has an unresolved Formula outside its campaign "
                "rebuild partition"
            )
        if legacy.formula in seen_formulae:
            raise RolloutError("rollout state contains a duplicate dispatch Formula")


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
    *,
    campaign_manifest: CampaignManifest | None = None,
) -> tuple[FormulaStatus, ...]:
    active_formulae = frozenset(
        formula
        for values in inventory.formulae.values()
        for formula in values
    )
    selection = (
        campaign_manifest.selection
        if campaign_manifest is not None
        else None
    )
    reuse_formulae = (
        frozenset(selection.reuse) if selection is not None else frozenset()
    )
    deferred_formulae = (
        frozenset(selection.deferred) if selection is not None else frozenset()
    )
    reasons: dict[str, tuple[str, ...]] = {}
    finalized: dict[str, bool] = {}
    for formula in FORMULA_ORDER:
        if formula in reuse_formulae or formula in deferred_formulae:
            # WHY: schema-4 reuse is an explicit frozen-authority disposition,
            # not a claim that historical bottles were rebuilt by the new
            # consumer. Deferred Formulae likewise own no identity in this
            # campaign. Only rebuilds use ordinary new-consumer finalization.
            reasons[formula] = ()
            finalized[formula] = False
            continue
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
        if formula in reuse_formulae:
            state, detail = (
                "reused",
                "historical bottle is bound by exact campaign authority",
            )
        elif formula in deferred_formulae:
            state, detail = (
                "deferred",
                "Formula is outside this campaign's publication set",
            )
        elif finalized[formula]:
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
                    if dep in deferred_formulae:
                        # WHY: a deferred dependency has neither a fresh
                        # campaign build nor frozen reuse authority.
                        missing.append(f"{dep}/{dep_arch}")
                        continue
                    if dep in reuse_formulae:
                        # WHY: validate_campaign_main_descendant returned this
                        # exact manifest only after checking current catalog,
                        # frozen T0 sidecars/link manifests, ABI, architecture,
                        # and protected-main lineage. Preserve built_from as
                        # historical truth; the fresh anonymous blob proof
                        # still gates dispatch immediately before any write.
                        if (
                            campaign_manifest is None
                            or dep_arch != campaign_manifest.rootfs_arch
                        ):
                            missing.append(f"{dep}/{dep_arch}")
                        continue
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
                detail = (
                    "all same-tap dependencies satisfy campaign disposition"
                    if campaign_manifest is not None
                    else "all same-tap dependencies are finalized"
                )
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


def workflow_run_title(formula: str, dispatch_token: str) -> str:
    if formula not in FORMULA_ORDER or not DISPATCH_TOKEN_RE.fullmatch(dispatch_token):
        raise RolloutError("workflow run identity is malformed")
    return f"Publish Kandelo bottles / {formula} / {dispatch_token}"


def new_dispatch_token(used_tokens: set[str]) -> str:
    while True:
        token = f"abi42-{secrets.token_hex(16)}"
        if token not in used_tokens:
            return token


def pending_dispatches(state: Mapping[str, Any]) -> tuple[PendingDispatch, ...]:
    values = state.get("pending_dispatches")
    if not isinstance(values, list):
        raise RolloutError("rollout state pending_dispatches is not an array")
    parsed: list[PendingDispatch] = []
    for value in values:
        if not isinstance(value, dict):
            raise RolloutError("rollout state contains a malformed pending dispatch")
        formula = value.get("formula")
        arches = value.get("arches")
        tap_sha = value.get("tap_sha")
        token = value.get("dispatch_token")
        status = value.get("status")
        recorded_at = value.get("recorded_at")
        request_started_at = value.get("request_started_at")
        submitted_at = value.get("submitted_at")
        expected_keys = {
            "formula",
            "arches",
            "tap_sha",
            "dispatch_token",
            "recorded_at",
            "status",
        }
        if status in ("request-started", "submitted"):
            expected_keys.add("request_started_at")
        if status == "submitted":
            expected_keys.add("submitted_at")
        if (
            set(value) != expected_keys
            or formula not in FORMULA_ORDER
            or arches != list(required_arches(formula))
            or not isinstance(tap_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", tap_sha)
            or not isinstance(token, str)
            or not DISPATCH_TOKEN_RE.fullmatch(token)
            or status not in ("planned", "request-started", "submitted")
            or not isinstance(recorded_at, str)
            or not recorded_at
            or (
                status in ("request-started", "submitted")
                and (
                    not isinstance(request_started_at, str)
                    or not request_started_at
                )
            )
            or (
                status == "submitted"
                and (not isinstance(submitted_at, str) or not submitted_at)
            )
        ):
            raise RolloutError("rollout state contains a malformed pending dispatch")
        parsed.append(
            PendingDispatch(
                formula=formula,
                arches=tuple(arches),
                tap_sha=tap_sha,
                dispatch_token=token,
                status=status,
                recorded_at=recorded_at,
                request_started_at=request_started_at,
                submitted_at=submitted_at,
            )
        )
    return tuple(parsed)


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
    response = github.runs(per_page=100, page=1, created=None)
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


def campaign_base_consumer(snapshot: TapSnapshot) -> str:
    consumer = snapshot.metadata.get("kandelo_commit")
    packages = _packages_by_name(snapshot.metadata)
    if (
        snapshot.metadata.get("kandelo_abi") != EXPECTED_ABI
        or snapshot.metadata.get("release_tag") != EXPECTED_RELEASE_TAG
        or not set(packages).issubset(FORMULA_ORDER)
        or not isinstance(consumer, str)
        or not re.fullmatch(r"[0-9a-f]{40}", consumer)
    ):
        raise RolloutError(
            "campaign base is not a valid ABI 42 last-green catalog"
        )
    return consumer


def validate_fresh_campaign_reservations(
    *,
    tap: GitTap,
    base: TapSnapshot,
    reservation: TapSnapshot,
    selection: CampaignSelection | None = None,
) -> None:
    if base.sha == reservation.sha or not tap.is_ancestor(base.sha, reservation.sha):
        raise RolloutError(
            "campaign base must be a strict ancestor of its reservation commit"
        )
    _ = campaign_base_consumer(base)
    # This deliberately uses package-owned sidecars rather than aggregate
    # membership. A previous campaign may have finalized only a product subset;
    # each sidecar still names the immutable last-green checksum source. The
    # Formula committed at T0 separately owns the next identity, because it may
    # already be ahead of that sidecar after an earlier reservation attempt.
    _ = last_green_catalog_state(base)
    selected_campaign = selection or CampaignSelection.all_rebuild()
    # WHY: an unchanged Formula is reusable only if its retained bytes already
    # implement the target ABI. Without this check an ABI-41 sidecar could be
    # placed in an ABI-42 reuse partition and never receive a successor build.
    require_reuse_target_abi(base, selected_campaign.reuse)

    if reservation.metadata != base.metadata:
        raise RolloutError(
            "campaign reservation commit changes finalized aggregate metadata"
        )
    if reservation.formula_sidecars != base.formula_sidecars:
        raise RolloutError(
            "campaign reservation commit changes package-owned sidecars"
        )
    if (
        not base.formula_sidecar_tree
        or reservation.formula_sidecar_tree != base.formula_sidecar_tree
    ):
        raise RolloutError(
            "campaign reservation commit changes the sidecar tree"
        )
    if reservation.formula_support_tree != base.formula_support_tree:
        raise RolloutError(
            "campaign reservation commit changes Formula support"
        )

    for formula in FORMULA_ORDER:
        sidecar = base.formula_sidecars.get(formula)
        sidecar_rebuild = (
            sidecar.get("bottle_rebuild") if isinstance(sidecar, dict) else None
        )
        if (
            isinstance(sidecar_rebuild, bool)
            or not isinstance(sidecar_rebuild, int)
            or sidecar_rebuild < 0
        ):
            raise RolloutError(
                f"{formula} finalized sidecar has an invalid bottle rebuild"
            )
        base_rebuild = base.identities[formula].bottle_rebuild
        if sidecar_rebuild > base_rebuild:
            raise RolloutError(
                f"{formula} base Formula predates its last-green sidecar"
            )
        if formula in selected_campaign.rebuild:
            expected_rebuild = base_rebuild + 1
            if reservation.identities[formula].bottle_rebuild != expected_rebuild:
                raise RolloutError(
                    f"{formula} must reserve exact base successor rebuild "
                    f"{expected_rebuild}"
                )
            require_last_green_formula_checksums(reservation, formula)
            # WHY: a normalized recipe hash could conceal two offsetting edits.
            # Reconstructing Tpre from T0 proves the rebuild line is the only
            # Formula byte changed. Advancing T0—not the older last-green
            # sidecar—prevents a new campaign from reusing an identity reserved
            # or occupied by an earlier campaign.
            expected_source = source_with_rebuild(
                base.formula_sources[formula],
                formula,
                expected_rebuild,
            )
        else:
            expected_source = base.formula_sources[formula]
            if (
                reservation.identities[formula].state_value()
                != base.identities[formula].state_value()
            ):
                raise RolloutError(
                    f"{formula} changed outside the campaign rebuild partition"
                )
        if reservation.formula_sources[formula] != expected_source:
            if formula in selected_campaign.rebuild:
                raise RolloutError(
                    f"Formula/{formula}.rb changes more than its rebuild reservation"
                )
            raise RolloutError(
                f"Formula/{formula}.rb changes beyond its campaign disposition"
            )

    expected_changes = {
        ("M", f"Formula/{formula}.rb")
        for formula in selected_campaign.rebuild
    }
    changes = tuple(tap.changed_entries(base.sha, reservation.sha))
    if len(changes) != len(expected_changes) or set(changes) != expected_changes:
        # WHY: TapSnapshot covers the publication contract, but not every
        # repository path. Checking the Git diff prevents an unrelated script,
        # policy, or documentation edit from riding along in mechanical Tpre.
        if selection is None:
            raise RolloutError(
                "campaign reservation contains changes beyond the 63 exact "
                "Formula rebuild reservations"
            )
        raise RolloutError(
            "campaign reservation contains changes beyond its exact Formula "
            "rebuild reservations"
        )

    expected_identity_count = sum(
        len(required_arches(formula))
        for formula in selected_campaign.rebuild
    )
    if (
        len(campaign_reservations(reservation, selected_campaign.rebuild))
        != expected_identity_count
    ):
        raise RolloutError(
            "campaign reservation does not contain every selected architecture identity"
        )


def require_absent_campaign_references(
    registry: Any,
    snapshot: TapSnapshot,
    formulae: Sequence[str] = FORMULA_ORDER,
) -> dict[str, str]:
    absent: dict[str, str] = {}
    for formula in formulae:
        if formula not in FORMULA_ORDER:
            raise RolloutError(f"cannot reserve unknown Formula {formula!r}")
        reference = snapshot.identities[formula].top_reference
        evidence = registry.manifest(formula, reference)
        if (
            not isinstance(evidence, RegistryManifestEvidence)
            or evidence.exists
            or evidence.digest is not None
        ):
            raise RolloutError(
                f"campaign OCI identity is already occupied: {formula}:{reference}"
            )
        absent[formula] = reference
    return absent


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
    dispatch_token = dispatch.get("dispatch_token")
    expected_dispatch_keys = {
        "arches",
        "formula",
        "run_id",
        "submitted_at",
        "tap_sha",
    }
    if dispatch_token is not None:
        expected_dispatch_keys.update(("caller_tap_sha", "dispatch_token"))
    if (
        set(dispatch) != expected_dispatch_keys
        or dispatch["run_id"] != run_id
        or formula not in FORMULA_ORDER
        or dispatch.get("arches") != list(required_arches(formula))
        or not isinstance(dispatch.get("tap_sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", dispatch["tap_sha"])
        or not isinstance(dispatch.get("submitted_at"), str)
        or not dispatch["submitted_at"]
        or (
            dispatch_token is not None
            and (
                not isinstance(dispatch_token, str)
                or not DISPATCH_TOKEN_RE.fullmatch(dispatch_token)
                or not isinstance(dispatch.get("caller_tap_sha"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", dispatch["caller_tap_sha"])
            )
        )
    ):
        raise RolloutError(
            f"controller-recorded run {run_id} has a malformed dispatch identity"
        )

    if not tap.is_ancestor(dispatch["tap_sha"], current.sha):
        raise RolloutError(
            f"controller-recorded run {run_id} is not on current protected main"
        )
    caller_tap_sha = dispatch.get("caller_tap_sha", dispatch["tap_sha"])
    if not tap.is_ancestor(caller_tap_sha, current.sha):
        raise RolloutError(
            f"controller-recorded run {run_id} caller is not on current protected main"
        )
    if dispatch.get("dispatch_token") is not None:
        # WHY: token batches deliberately keep bottle input on the reserved
        # tap_sha while repository_dispatch may load a later finalizer-only
        # caller. Revalidate that exact relationship before trusting failed-run
        # evidence; legacy single-intent runs used one SHA for both identities.
        validate_caller_source_pair(
            tap,
            formula,
            dispatch["tap_sha"],
            caller_tap_sha,
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
            approved_publication_workflow_hash(source_workflow_hash)
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
            not approved_publication_workflow_hash(source_workflow_hash)
            or trusted_publishers.get(source_workflow_hash) != source_publisher
            or source_consumer != state.get("expected_kandelo_sha")
        ):
            raise RolloutError(
                f"controller-recorded run {run_id} uses an untrusted historical workflow"
            )
    generation_sha, generation_tag = approved_package_generation(
        source_workflow_hash
    )
    validate_workflow_source(
        source,
        source_consumer,
        expected_publisher_sha=source_publisher,
        expected_package_generation_sha=generation_sha,
        expected_package_generation_tag=generation_tag,
        allow_legacy_tap_ref=source_selector == "main",
        allow_legacy_run_name=True,
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
        or run.get("head_sha") != caller_tap_sha
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
    validate_workflow(
        github,
        current,
        expected_kandelo_sha,
        campaign_contract=campaign_contract_from_state(
            state, expected_kandelo_sha
        ),
    )
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
    validate_campaign_recovery_transition(tap, state, current)
    validate_state(recovered_state, current, expected_kandelo_sha)
    validate_campaign_main_descendant(tap, recovered_state, current)
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
    validate_workflow(
        github,
        snapshot,
        expected_kandelo_sha,
        campaign_contract=campaign_contract_from_state(
            state, expected_kandelo_sha
        ),
    )
    validate_state(state, snapshot, expected_kandelo_sha)
    validate_campaign_main_descendant(tap, state, snapshot)
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


def _parse_utc_timestamp(value: str, label: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RolloutError(f"{label} is not an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise RolloutError(f"{label} is not a UTC timestamp")
    return parsed


def _format_utc_timestamp(value: dt.datetime) -> str:
    return (
        value.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def dispatch_run_created_range(
    intents: Sequence[PendingDispatch],
    *,
    now: dt.datetime | None = None,
) -> str:
    if not intents:
        raise RolloutError("dispatch run range requires at least one intent")
    current = now or dt.datetime.now(dt.timezone.utc)
    starts: list[dt.datetime] = []
    ends: list[dt.datetime] = []
    for intent in intents:
        if intent.request_started_at is None:
            raise RolloutError(
                f"dispatch intent for {intent.formula} has no request start"
            )
        started = _parse_utc_timestamp(
            intent.request_started_at,
            f"{intent.formula} request_started_at",
        )
        ended = (
            _parse_utc_timestamp(
                intent.submitted_at,
                f"{intent.formula} submitted_at",
            )
            if intent.submitted_at is not None
            else current
        )
        if ended < started:
            raise RolloutError(
                f"{intent.formula} submitted_at precedes request_started_at"
            )
        starts.append(started)
        ends.append(ended)
    lower = min(starts) - DISPATCH_RUN_CLOCK_SKEW
    upper = max(ends)
    if any(intent.submitted_at is None for intent in intents):
        upper = max(upper, current)
    upper += DISPATCH_RUN_CLOCK_SKEW
    return f"{_format_utc_timestamp(lower)}..{_format_utc_timestamp(upper)}"


def matching_token_runs_in_runs(
    tap: GitTap,
    state: Mapping[str, Any],
    runs: Iterable[Mapping[str, Any]],
    intent: PendingDispatch,
    *,
    snapshots: dict[str, TapSnapshot],
    validated_pairs: set[tuple[str, str]],
) -> tuple[CorrelatedRun, ...]:
    expected_title = workflow_run_title(intent.formula, intent.dispatch_token)
    matches: list[CorrelatedRun] = []
    for run in runs:
        if (
            run.get("event") != "repository_dispatch"
            or run.get("display_title") != expected_title
        ):
            continue
        if not is_first_run_attempt(run.get("run_attempt")):
            # WHY: a manual workflow rerun keeps the original run ID, title,
            # token, and caller SHA. Only attempt 1 represents the HTTP request
            # journaled by this campaign; later attempts are not new intents.
            raise RolloutError(
                f"token-correlated run {run['id']} is a rerun; "
                "only attempt 1 is eligible"
            )
        caller_tap_sha = run.get("head_sha")
        if not isinstance(caller_tap_sha, str):
            raise RolloutError(
                f"token-correlated run {run['id']} has no caller tap SHA"
            )
        validate_correlated_caller(
            tap,
            state,
            intent,
            caller_tap_sha,
            snapshots=snapshots,
            validated_pairs=validated_pairs,
        )
        matches.append(
            CorrelatedRun(
                run_id=run["id"],
                caller_tap_sha=caller_tap_sha,
            )
        )
    return tuple(matches)


def correlate_pending_dispatches(
    tap: GitTap,
    github: GitHub,
    state: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[tuple[str, int], ...]]:
    intents = pending_dispatches(state)
    uncertain = tuple(intent for intent in intents if intent.status != "planned")
    if not uncertain:
        return copy.deepcopy(state), ()
    created = dispatch_run_created_range(uncertain)
    runs = workflow_run_snapshot(github, created=created)
    snapshots: dict[str, TapSnapshot] = {}
    validated_pairs: set[tuple[str, str]] = set()
    matches: dict[str, CorrelatedRun] = {}
    for intent in uncertain:
        candidates = matching_token_runs_in_runs(
            tap,
            state,
            runs,
            intent,
            snapshots=snapshots,
            validated_pairs=validated_pairs,
        )
        if len(candidates) > 1:
            raise RolloutError(
                f"dispatch token for {intent.formula} matched multiple runs: "
                f"{sorted(candidate.run_id for candidate in candidates)}"
            )
        if candidates:
            matches[intent.dispatch_token] = candidates[0]
    if not matches:
        return copy.deepcopy(state), ()

    updated = copy.deepcopy(state)
    retained: list[Mapping[str, Any]] = []
    recovered: list[tuple[str, int]] = []
    for value, intent in zip(updated["pending_dispatches"], intents, strict=True):
        match = matches.get(intent.dispatch_token)
        if match is None:
            retained.append(value)
            continue
        # WHY: request-started is written before the HTTP request. A crash can
        # therefore hide the later submitted timestamp even when GitHub accepted
        # the request. The unique run-name token is the authority in that case;
        # request_started_at remains the earliest durable submission boundary.
        submitted_at = intent.submitted_at or intent.request_started_at
        assert submitted_at is not None
        updated["dispatches"].append(
            {
                "formula": intent.formula,
                "arches": list(intent.arches),
                "tap_sha": intent.tap_sha,
                # WHY: tap_sha remains the bottle input. The caller SHA proves
                # which protected-main workflow received the dispatch after
                # parallel finalizers may have advanced the default branch.
                "caller_tap_sha": match.caller_tap_sha,
                "run_id": match.run_id,
                "dispatch_token": intent.dispatch_token,
                "submitted_at": submitted_at,
            }
        )
        recovered.append((intent.formula, match.run_id))
    updated["pending_dispatches"] = retained
    return updated, tuple(recovered)


def acknowledge_pending_dispatches(
    *,
    tap: GitTap,
    github: GitHub,
    state: Mapping[str, Any],
    state_path: pathlib.Path,
    snapshot: TapSnapshot,
    expected_kandelo_sha: str,
    timeout_seconds: int,
    poll_seconds: float,
) -> tuple[dict[str, Any], tuple[tuple[str, int], ...]]:
    current = copy.deepcopy(state)
    acknowledged: list[tuple[str, int]] = []
    deadline = time.monotonic() + timeout_seconds
    while True:
        updated, recovered = correlate_pending_dispatches(tap, github, current)
        if recovered:
            validate_state(updated, snapshot, expected_kandelo_sha)
            write_state(state_path, updated)
            current = updated
            acknowledged.extend(recovered)
        uncertain = tuple(
            intent
            for intent in pending_dispatches(current)
            if intent.status != "planned"
        )
        if not uncertain:
            return current, tuple(acknowledged)
        if time.monotonic() >= deadline:
            names = ", ".join(intent.formula for intent in uncertain)
            raise RolloutError(
                "no token-correlated run ID appeared within "
                f"{timeout_seconds}s for: {names}; pending markers were retained"
            )
        time.sleep(poll_seconds)


def recover_submitted_dispatch(
    *,
    tap: GitTap,
    github: GitHub,
    expected_kandelo_sha: str,
    state_path: pathlib.Path,
    no_fetch: bool,
) -> tuple[tuple[str, int], ...]:
    state = read_state(state_path)
    if state is None:
        raise RolloutError(f"rollout state {state_path} does not exist")
    sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
    snapshot = load_snapshot(tap, sha)
    validate_workflow(
        github,
        snapshot,
        expected_kandelo_sha,
        campaign_contract=campaign_contract_from_state(
            state, expected_kandelo_sha
        ),
    )
    upgraded = upgrade_state(state, snapshot, expected_kandelo_sha)
    validate_campaign_main_descendant(tap, upgraded, snapshot)
    recovered_state = copy.deepcopy(upgraded)
    recovered: list[tuple[str, int]] = []

    if recovered_state.get("unresolved_dispatch") is not None:
        intent = submitted_dispatch(recovered_state)
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
        recovered.append((intent.formula, run_id))

    token_state, token_recovered = correlate_pending_dispatches(
        tap, github, recovered_state
    )
    recovered.extend(token_recovered)
    if not recovered:
        raise RolloutError(
            "recovery found no exact token-correlated runs; pending markers "
            "were retained"
        )

    # WHY: every recovered token was durable before its HTTP request. Move all
    # currently visible exact matches to the completed ledger in one replacement
    # while retaining every still-ambiguous intent; recovery never dispatches.
    validate_state(token_state, snapshot, expected_kandelo_sha)
    write_state(state_path, token_state)
    return tuple(recovered)


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


def require_dependency_closed_allowlist(
    snapshot: TapSnapshot,
    allowed_formulae: frozenset[str] | None,
    campaign_rebuilds: frozenset[str] | None = None,
) -> None:
    if allowed_formulae is None:
        return
    closure = set(allowed_formulae)
    pending = list(allowed_formulae)
    while pending:
        formula = pending.pop()
        for dependency in snapshot.dependencies[formula]:
            if (
                campaign_rebuilds is not None
                and dependency not in campaign_rebuilds
            ):
                # WHY: a reuse dependency is validated in place and has no
                # dispatchable successor. Requiring it in a build allowlist
                # would contradict the campaign partition.
                continue
            if dependency not in closure:
                closure.add(dependency)
                pending.append(dependency)
    missing = sorted(closure - set(allowed_formulae))
    if missing:
        raise RolloutError(
            "fresh-campaign Formula allowlist is not dependency-closed; "
            "also include " + ", ".join(missing)
        )


def initialize_campaign(
    *,
    tap: GitTap,
    github: GitHub,
    registry: Any,
    state_path: pathlib.Path,
    campaign_id: str,
    base_tap_sha: str,
    reservation_tap_sha: str,
    contract: CampaignContract,
    no_fetch: bool,
    selection: CampaignSelection | None = None,
    manifest_authority_sha: str | None = None,
) -> dict[str, Any]:
    # WHY: even a valid old ledger contains dispatch history that cannot be
    # inferred from Git or GHCR. Refuse before any network observation rather
    # than overwriting, upgrading, or reconstructing another campaign.
    if state_path.exists():
        raise RolloutError(
            f"fresh campaign state {state_path} already exists; choose a new path"
        )
    if (
        not isinstance(campaign_id, str)
        or not CAMPAIGN_ID_RE.fullmatch(campaign_id)
        or not re.fullmatch(r"[0-9a-f]{40}", base_tap_sha)
        or not re.fullmatch(r"[0-9a-f]{40}", reservation_tap_sha)
    ):
        raise RolloutError("fresh campaign identity or tap SHA is invalid")
    validate_campaign_contract(contract)

    manifest: CampaignManifest | None = None
    if manifest_authority_sha is not None:
        if selection is not None:
            raise RolloutError(
                "operator Formula partitions cannot override a manifest-backed campaign"
            )
        tap.ensure_commit(manifest_authority_sha)
        manifest = load_campaign_manifest(tap, manifest_authority_sha)
        if (
            campaign_id != manifest.campaign
            or base_tap_sha != manifest.base_tap_sha
            or reservation_tap_sha != manifest.reservation_tap_sha
        ):
            raise RolloutError(
                "campaign inputs differ from the exact manifest authority"
            )
    tap.ensure_commit(base_tap_sha)
    if manifest is not None:
        tap.ensure_commit(reservation_tap_sha)
    observed_main = (
        tap.main_without_fetch() if no_fetch else tap.fetch_main()
    )
    expected_main = manifest_authority_sha or reservation_tap_sha
    if observed_main != expected_main:
        raise RolloutError(
            "protected tap main does not equal the requested campaign authority"
        )
    base = load_snapshot(tap, base_tap_sha)
    reservation = load_snapshot(tap, reservation_tap_sha)
    authority = (
        load_snapshot(tap, manifest_authority_sha)
        if manifest_authority_sha is not None
        else reservation
    )
    validate_workflow(
        github,
        authority,
        contract.consumer_sha,
        campaign_contract=contract,
    )
    selected_campaign = (
        manifest.selection
        if manifest is not None
        else (selection or CampaignSelection.all_rebuild())
    )
    validate_fresh_campaign_reservations(
        tap=tap,
        base=base,
        reservation=reservation,
        selection=selected_campaign,
    )
    if manifest is not None:
        assert manifest_authority_sha is not None
        if (
            not tap.is_ancestor(reservation_tap_sha, manifest_authority_sha)
            or authority.metadata != reservation.metadata
            or authority.formula_sources != reservation.formula_sources
            or authority.formula_sidecars != reservation.formula_sidecars
            or authority.formula_support_tree != reservation.formula_support_tree
            or authority.formula_sidecar_tree != reservation.formula_sidecar_tree
        ):
            # WHY: Tmanifest may commit the controller, tests, docs, and the
            # authority itself, but it cannot quietly change any package input
            # after the separately reviewed Tpre reservation.
            raise RolloutError(
                "campaign manifest commit changes Tpre package publication sources"
            )
        validate_campaign_manifest_sources(tap, manifest, base, reservation)

    inventory = active_inventory(github)
    if inventory.count or inventory.unknown_run_ids:
        raise RolloutError(
            "cannot initialize a fresh campaign while publication runs are active"
        )
    if manifest is not None:
        verify_campaign_reuse_blobs(registry, manifest)
    absent = require_absent_campaign_references(
        registry, reservation, selected_campaign.rebuild
    )
    checked_at = _utc_now()

    # Re-observe both mutable coordination surfaces immediately before the
    # single durable write. No HTTP dispatch is attempted by initialization.
    latest_main = (
        tap.main_without_fetch() if no_fetch else tap.fetch_main()
    )
    if latest_main != expected_main:
        raise RolloutError(
            "protected tap main moved during campaign initialization"
        )
    latest_inventory = active_inventory(github)
    if latest_inventory.count or latest_inventory.unknown_run_ids:
        raise RolloutError(
            "a publication run started during campaign initialization"
        )

    state = initial_campaign_state(
        authority,
        campaign_id=campaign_id,
        base_snapshot=base,
        contract=contract,
        absent_oci_references=absent,
        checked_at=checked_at,
        selection=selection,
        manifest=manifest,
        manifest_authority_sha=manifest_authority_sha,
        reservation_snapshot=reservation if manifest is not None else None,
    )
    validate_state(state, authority, contract.consumer_sha)
    if manifest is not None:
        validate_campaign_main_descendant(tap, state, authority)
    write_new_state(state_path, state)
    return state


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
    registry: Any | None = None,
) -> int:
    state = read_state(state_path)
    if state is None:
        raise RolloutError(
            "cannot initialize a replacement rollout state after the ABI 42 "
            f"cutover; {state_path} does not exist. Initialize a fresh "
            "campaign explicitly before dispatch"
        )
    while True:
        sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
        snapshot = load_snapshot(tap, sha)
        campaign_contract = campaign_contract_from_state(
            state, expected_kandelo_sha
        )
        campaign_selection = campaign_selection_from_state(state)
        campaign_rebuilds = (
            frozenset(campaign_selection.rebuild)
            if campaign_selection is not None
            else None
        )
        if campaign_contract is not None:
            if (
                allowed_formulae is not None
                and campaign_rebuilds is not None
                and not allowed_formulae.issubset(campaign_rebuilds)
            ):
                raise RolloutError(
                    "Formula allowlist contains a reuse or deferred Formula"
                )
            require_dependency_closed_allowlist(
                snapshot,
                allowed_formulae,
                campaign_rebuilds=campaign_rebuilds,
            )
        validate_workflow(
            github,
            snapshot,
            expected_kandelo_sha,
            campaign_contract=campaign_contract,
        )
        state = upgrade_state(state, snapshot, expected_kandelo_sha)
        campaign_manifest = validate_campaign_main_descendant(
            tap, state, snapshot
        )
        if state.get("unresolved_dispatch") is not None:
            raise RolloutError(
                f"{state_path} contains an unresolved dispatch; inspect it before continuing"
            )
        pending = pending_dispatches(state)
        uncertain = tuple(intent for intent in pending if intent.status != "planned")
        if uncertain:
            raise RolloutError(
                f"{state_path} contains submitted token-correlated dispatches; "
                "recover them before continuing"
            )
        if pending and any(intent.tap_sha != snapshot.sha for intent in pending):
            # WHY: planned means no HTTP request was attempted, so these entries
            # can be discarded safely when main moves. Once request-started is
            # durable, this path is forbidden and recovery owns the decision.
            state["pending_dispatches"] = []
            write_state(state_path, state)
            continue

        preflighted_formulae: set[str] = set()
        inventory = reconcile_recorded_activity(
            github, active_inventory(github), state
        )
        if inventory.count >= MAX_ACTIVE_RUNS:
            return 0
        if inventory.unknown_run_ids:
            raise RolloutError(
                "active production runs have not exposed their Formula matrix yet: "
                + ", ".join(map(str, inventory.unknown_run_ids))
            )
        active_formulae = {
            formula
            for values in inventory.formulae.values()
            for formula in values
        }
        if pending:
            superseded_tokens = {
                intent.dispatch_token
                for intent in pending
                if intent.formula in active_formulae
            }
            if superseded_tokens:
                # WHY: a planned marker proves no HTTP request was attempted.
                # If another actor started that Formula while this controller
                # was down, dropping only the colliding plans avoids a duplicate
                # publication while preserving unrelated reserved work.
                state["pending_dispatches"] = [
                    entry
                    for entry in state["pending_dispatches"]
                    if entry.get("dispatch_token") not in superseded_tokens
                ]
                validate_state(state, snapshot, expected_kandelo_sha)
                write_state(state_path, state)
                pending = pending_dispatches(state)
                if not pending:
                    continue
        available = min(maximum, MAX_ACTIVE_RUNS - inventory.count)

        if not pending:
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
                tap,
                snapshot,
                expected_kandelo_sha,
                inventory,
                history_blocks,
                campaign_manifest=campaign_manifest,
            )
            selected_allowlist = allowed_formulae
            if campaign_rebuilds is not None:
                selected_allowlist = (
                    campaign_rebuilds
                    if selected_allowlist is None
                    else selected_allowlist & campaign_rebuilds
                )
            ready = ready_dispatch_candidates(statuses, selected_allowlist)
            if not ready:
                return 0

            # Refresh both main and capacity once immediately before reserving
            # the whole batch. Every intent is then durable before any request,
            # so the reserved count—not slow run discovery—protects the limit.
            latest_sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
            if latest_sha != snapshot.sha:
                continue
            latest_inventory = reconcile_recorded_activity(
                github, active_inventory(github), state
            )
            if latest_inventory.unknown_run_ids:
                raise RolloutError(
                    "active production runs have not exposed their Formula matrix yet: "
                    + ", ".join(map(str, latest_inventory.unknown_run_ids))
                )
            available = min(maximum, MAX_ACTIVE_RUNS - latest_inventory.count)
            if available <= 0:
                return 0
            active_formulae = {
                formula
                for values in latest_inventory.formulae.values()
                for formula in values
            }
            selected = [
                status for status in ready if status.name not in active_formulae
            ][:available]
            if not selected:
                return 0
            if campaign_contract is not None:
                if registry is None:
                    registry = AnonymousRegistry()
                # WHY: initialization proves the complete campaign namespace,
                # but builds may start hours later. Recheck the entire selected
                # batch before journaling any HTTP attempt so one occupied
                # identity aborts the batch without a partial submission.
                require_absent_campaign_references(
                    registry,
                    snapshot,
                    tuple(status.name for status in selected),
                )
                preflighted_formulae.update(status.name for status in selected)
            used_tokens = {
                entry["dispatch_token"]
                for entry in state["dispatches"]
                if isinstance(entry, dict)
                and isinstance(entry.get("dispatch_token"), str)
            }
            recorded_at = _utc_now()
            for status in selected:
                token = new_dispatch_token(used_tokens)
                used_tokens.add(token)
                state["pending_dispatches"].append(
                    {
                        "formula": status.name,
                        "arches": list(status.arches),
                        "tap_sha": snapshot.sha,
                        "dispatch_token": token,
                        "recorded_at": recorded_at,
                        "status": "planned",
                    }
                )
            validate_state(state, snapshot, expected_kandelo_sha)
            write_state(state_path, state)
            pending = pending_dispatches(state)

        planned = [intent for intent in pending if intent.status == "planned"][:available]
        if not planned:
            return 0
        if campaign_contract is not None:
            if registry is None:
                registry = AnonymousRegistry()
            unchecked = tuple(
                intent.formula
                for intent in planned
                if intent.formula not in preflighted_formulae
            )
            if unchecked:
                require_absent_campaign_references(
                    registry,
                    snapshot,
                    unchecked,
                )
        if state.get("schema") == 4:
            if registry is None:
                registry = AnonymousRegistry()
            # WHY: T0's sidecars and link manifests plus all 23 public blobs
            # may have been verified hours before dispatch. Repeat the entire
            # immutable proof immediately before this invocation's first
            # external write, while still bound to the ledger's Tmanifest hash.
            verify_manifest_backed_campaign(tap, registry, state)
            latest_sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
            if latest_sha != snapshot.sha:
                raise RolloutError(
                    "protected tap main moved during manifest pre-dispatch verification"
                )
            latest_inventory = reconcile_recorded_activity(
                github, active_inventory(github), state
            )
            if (
                latest_inventory.unknown_run_ids
                or latest_inventory.count + len(planned) > MAX_ACTIVE_RUNS
            ):
                raise RolloutError(
                    "publication capacity changed during manifest verification"
                )
            active_formulae = {
                formula
                for values in latest_inventory.formulae.values()
                for formula in values
            }
            if any(intent.formula in active_formulae for intent in planned):
                raise RolloutError(
                    "a selected Formula started during manifest verification"
                )
            # WHY: hashing all reused bytes is intentionally expensive. A
            # competing publisher can occupy Bash after the earlier planning
            # check, so recheck the complete planned batch only after every
            # long read and mutable coordination check, immediately before any
            # intent is marked request-started.
            require_absent_campaign_references(
                registry,
                snapshot,
                tuple(intent.formula for intent in planned),
            )
        submitted = 0
        for intent in planned:
            value = next(
                entry
                for entry in state["pending_dispatches"]
                if entry.get("dispatch_token") == intent.dispatch_token
            )
            value["status"] = "request-started"
            value["request_started_at"] = _utc_now()
            validate_state(state, snapshot, expected_kandelo_sha)
            write_state(state_path, state)
            try:
                github.dispatch(
                    intent.formula,
                    intent.arches,
                    intent.tap_sha,
                    intent.dispatch_token,
                )
            except BaseException:
                # An HTTP error can still follow an accepted request. The token
                # and request-started marker make later recovery unambiguous;
                # this controller never retries the same Formula blindly.
                write_state(state_path, state)
                raise
            value["status"] = "submitted"
            value["submitted_at"] = _utc_now()
            validate_state(state, snapshot, expected_kandelo_sha)
            write_state(state_path, state)
            print(
                f"submitted {intent.formula} ({','.join(intent.arches)}) "
                f"with token {intent.dispatch_token}",
                flush=True,
            )
            submitted += 1

        state, acknowledged = acknowledge_pending_dispatches(
            tap=tap,
            github=github,
            state=state,
            state_path=state_path,
            snapshot=snapshot,
            expected_kandelo_sha=expected_kandelo_sha,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
        for formula, run_id in acknowledged:
            print(f"acknowledged {formula} as run {run_id}", flush=True)
        return submitted


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
        "--initialize-campaign",
        action="store_true",
        help=(
            "validate and atomically record one complete fresh reservation; "
            "never dispatch"
        ),
    )
    action.add_argument(
        "--dispatch",
        action="store_true",
        help="explicitly create fresh production repository_dispatch events",
    )
    action.add_argument(
        "--recover-dispatch",
        action="store_true",
        help=(
            "record exact late runs for submitted unresolved intents; "
            "never dispatch"
        ),
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
        "--campaign-id",
        help="stable operator-chosen identity for --initialize-campaign",
    )
    parser.add_argument(
        "--campaign-base-tap-sha",
        help="exact reviewed last-green tap commit preceding all reservations",
    )
    parser.add_argument(
        "--campaign-reservation-tap-sha",
        help="exact reviewed Tpre commit containing selected fresh reservations",
    )
    parser.add_argument(
        "--campaign-manifest-tap-sha",
        help=(
            "exact protected-main Tmanifest commit containing the canonical "
            "campaign manifest"
        ),
    )
    parser.add_argument(
        "--expected-publisher-sha",
        help="reviewed reusable publisher SHA for --initialize-campaign",
    )
    parser.add_argument(
        "--expected-package-generation-sha",
        help="reviewed sealed package-generation SHA for --initialize-campaign",
    )
    parser.add_argument(
        "--expected-package-generation-tag",
        help="reviewed sealed package-generation tag for --initialize-campaign",
    )
    parser.add_argument(
        "--expected-workflow-sha256",
        help="reviewed complete caller SHA-256 for --initialize-campaign",
    )
    parser.add_argument(
        "--campaign-rebuild-formulae",
        help=(
            "canonical comma-separated Formulae whose payload closures changed "
            "and therefore reserve successor identities"
        ),
    )
    parser.add_argument(
        "--campaign-reuse-formulae",
        help=(
            "canonical comma-separated Formulae retained with new validation "
            "evidence; use an empty value when none"
        ),
    )
    parser.add_argument(
        "--campaign-deferred-formulae",
        help=(
            "canonical comma-separated Formulae intentionally outside this "
            "campaign; use an empty value when none"
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
        help="seconds to wait for the batch's unambiguous run IDs (default: 600)",
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
        args.initialize_campaign
        or args.dispatch
        or args.recover_dispatch
        or args.abandon_dispatch_run is not None
        or args.recover_failed_run is not None
        or args.adopt_failed_run is not None
    ) and args.state_file is None:
        parser.error(
            "--state-file is required with --dispatch, --initialize-campaign, "
            "--recover-dispatch, "
            "--abandon-dispatch-run, --recover-failed-run, or --adopt-failed-run"
        )
    campaign_values = (
        args.campaign_id,
        args.campaign_base_tap_sha,
        args.campaign_reservation_tap_sha,
        args.campaign_manifest_tap_sha,
        args.expected_publisher_sha,
        args.expected_package_generation_sha,
        args.expected_package_generation_tag,
        args.expected_workflow_sha256,
    )
    selection_values = (
        args.campaign_rebuild_formulae,
        args.campaign_reuse_formulae,
        args.campaign_deferred_formulae,
    )
    if args.initialize_campaign:
        if any(value is None for value in campaign_values):
            parser.error(
                "--initialize-campaign requires --campaign-id, "
                "--campaign-base-tap-sha, --campaign-reservation-tap-sha, "
                "--campaign-manifest-tap-sha, "
                "--expected-publisher-sha, --expected-package-generation-sha, "
                "--expected-package-generation-tag, and "
                "--expected-workflow-sha256"
            )
        if (
            not CAMPAIGN_ID_RE.fullmatch(args.campaign_id)
            or not re.fullmatch(
                r"[0-9a-f]{40}", args.campaign_base_tap_sha
            )
            or not re.fullmatch(
                r"[0-9a-f]{40}", args.campaign_reservation_tap_sha
            )
            or not re.fullmatch(
                r"[0-9a-f]{40}", args.campaign_manifest_tap_sha
            )
            or not re.fullmatch(r"[0-9a-f]{40}", args.expected_publisher_sha)
            or not re.fullmatch(
                r"[0-9a-f]{40}", args.expected_package_generation_sha
            )
            or not PACKAGE_GENERATION_TAG_RE.fullmatch(
                args.expected_package_generation_tag
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}", args.expected_workflow_sha256
            )
        ):
            parser.error("--initialize-campaign contains an invalid identity")
        if any(value is not None for value in selection_values):
            parser.error(
                "manifest-backed campaigns reject operator Formula partitions"
            )
    elif any(value is not None for value in campaign_values):
        parser.error("campaign contract flags require --initialize-campaign")
    elif any(value is not None for value in selection_values):
        parser.error("campaign Formula partitions require --initialize-campaign")
    if all(value is not None for value in selection_values):
        def parse_campaign_partition(value: str) -> tuple[str, ...]:
            if value == "":
                return ()
            parts = tuple(value.split(","))
            if any(not part for part in parts):
                raise RolloutError(
                    "campaign Formula partitions contain an empty entry"
                )
            return parts

        try:
            args.campaign_selection = CampaignSelection.create(
                rebuild=parse_campaign_partition(args.campaign_rebuild_formulae),
                reuse=parse_campaign_partition(args.campaign_reuse_formulae),
                deferred=parse_campaign_partition(args.campaign_deferred_formulae),
            )
        except RolloutError as error:
            parser.error(str(error))
    else:
        args.campaign_selection = None
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
        args.initialize_campaign
        or args.dispatch
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
        if args.initialize_campaign:
            state_path = args.state_file.resolve()
            contract = CampaignContract(
                publisher_sha=args.expected_publisher_sha,
                consumer_sha=args.expected_kandelo_sha,
                package_generation_sha=args.expected_package_generation_sha,
                package_generation_tag=args.expected_package_generation_tag,
                workflow_sha256=args.expected_workflow_sha256,
            )
            with state_lock(state_path):
                state = initialize_campaign(
                    tap=tap,
                    github=github,
                    registry=AnonymousRegistry(),
                    state_path=state_path,
                    campaign_id=args.campaign_id,
                    base_tap_sha=args.campaign_base_tap_sha,
                    reservation_tap_sha=args.campaign_reservation_tap_sha,
                    contract=contract,
                    no_fetch=args.no_fetch,
                    selection=args.campaign_selection,
                    manifest_authority_sha=args.campaign_manifest_tap_sha,
                )
            selection = campaign_selection_from_state(state)
            assert selection is not None
            print(
                f"initialized campaign {state['campaign']['id']} with "
                f"{len(selection.rebuild)} rebuild, {len(selection.reuse)} reuse, "
                f"and {len(selection.deferred)} deferred Formulae; "
                f"{sum(len(required_arches(formula)) for formula in selection.rebuild)} "
                "new architecture identities; "
                "no repository_dispatch was sent"
            )
            return 0
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
                recovered = recover_submitted_dispatch(
                    tap=tap,
                    github=github,
                    expected_kandelo_sha=args.expected_kandelo_sha,
                    state_path=state_path,
                    no_fetch=args.no_fetch,
                )
            details = ", ".join(
                f"{formula} as run {run_id}" for formula, run_id in recovered
            )
            print(f"recovered submitted {details}; no repository_dispatch was sent")
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
                    registry=AnonymousRegistry(),
                )
            print(f"dispatch pass complete: {dispatched} fresh run(s) submitted")
            return 0

        sha = tap.main_without_fetch() if args.no_fetch else tap.fetch_main()
        snapshot = load_snapshot(tap, sha)
        state = read_state(args.state_file.resolve()) if args.state_file else None
        validate_workflow(
            github,
            snapshot,
            args.expected_kandelo_sha,
            campaign_contract=(
                campaign_contract_from_state(
                    state, args.expected_kandelo_sha
                )
                if state is not None
                else None
            ),
        )
        if state is not None:
            state = upgrade_state(state, snapshot, args.expected_kandelo_sha)
            campaign_manifest = validate_campaign_main_descendant(
                tap, state, snapshot
            )
            if (
                state.get("unresolved_dispatch") is not None
                or state.get("pending_dispatches")
            ):
                raise RolloutError(
                    f"{args.state_file} contains unresolved dispatch intents"
                )
        else:
            campaign_manifest = None
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
            tap,
            snapshot,
            args.expected_kandelo_sha,
            inventory,
            history_blocks,
            campaign_manifest=campaign_manifest,
        )
        render_status(snapshot, inventory, statuses, as_json=args.json)
        return 0
    except RolloutError as error:
        print(f"abi42-rollout: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
