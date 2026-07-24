#!/usr/bin/env python3
"""Safely plan or dispatch the one-time Kandelo ABI 42 bottle rollout.

The default command is read-only with respect to GitHub. It fetches tap `main`,
checks finalized sidecars and production runs, and prints what is ready. The
only write path is the explicit `--dispatch` flag, which always creates a fresh
`repository_dispatch`; this program has no workflow-rerun operation.
"""

from __future__ import annotations

import argparse
import contextlib
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
from typing import Any, Iterable, Mapping, Sequence


REPOSITORY = "Kandelo-dev/homebrew-tap-core"
TAP_NAME = "kandelo-dev/tap-core"
WORKFLOW_ID = 315_324_894
WORKFLOW_PATH = ".github/workflows/publish-bottles.yml"
EXPECTED_ABI = 42
EXPECTED_RELEASE_TAG = "bottles-abi-v42"
MAX_ACTIVE_RUNS = 8
ACTIVE_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")
BOTTLE_ROOT = "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"

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
    version: str
    formula_revision: int
    bottle_rebuild: int
    arches: tuple[str, ...]
    bottle_sha256: Mapping[str, str]

    def state_value(self) -> dict[str, Any]:
        # Generated bottle hashes change when the finalizer commits. The
        # version/revision/rebuild/arch tuple is the immutable reserved identity.
        return {
            "version": self.version,
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
        return tuple(result["jobs"])

    def dispatch(self, formula: str, arches: Sequence[str]) -> None:
        payload: dict[str, Any] = {
            "event_type": "publish-kandelo-bottles",
            "client_payload": {
                "formulae": formula,
                "arches": ",".join(arches),
            },
        }
        if formula == "python":
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

    source_versions = re.findall(
        r'^\s{2}version\s+"([^"]+)"\s*$', source, flags=re.MULTILINE
    )
    previous_version = (
        previous_package.get("version")
        if isinstance(previous_package, dict)
        and isinstance(previous_package.get("version"), str)
        else None
    )
    if source_versions:
        if len(set(source_versions)) != 1:
            raise RolloutError(f"Formula/{formula}.rb has ambiguous versions")
        version = source_versions[0]
    elif previous_version:
        version = previous_version
    else:
        raise RolloutError(
            f"Formula/{formula}.rb needs an explicit version for rollout identity"
        )

    formula_revision = _single_int(
        source, r"^\s{2}revision\s+([0-9]+)\s*$", 0, f"{formula} revision"
    )
    if rebuild < 1:
        raise RolloutError(
            f"Formula/{formula}.rb has not reserved a positive ABI 42 rebuild"
        )
    return FormulaIdentity(
        name=formula,
        version=version,
        formula_revision=formula_revision,
        bottle_rebuild=rebuild,
        arches=expected_arches,
        bottle_sha256=hashes,
    )


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
    previous_packages = {
        package.get("name"): package
        for package in metadata.get("packages", ())
        if isinstance(package, dict) and isinstance(package.get("name"), str)
    }
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
        identities[formula] = parse_formula_identity(
            formula, source, previous_packages.get(formula)
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
    )


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
    if uses != [expected_kandelo_sha] or refs != [expected_kandelo_sha]:
        raise RolloutError(
            "production workflow is not frozen to the requested ABI 42 Kandelo SHA "
            f"(uses={uses}, kandelo-ref={refs})"
        )


def _packages_by_name(metadata: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    packages: dict[str, Mapping[str, Any]] = {}
    for value in metadata.get("packages", ()):
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            continue
        name = value["name"]
        if name in packages:
            raise RolloutError(f"Kandelo/metadata.json duplicates package {name}")
        packages[name] = value
    return packages


def _bottles_by_arch(
    value: Mapping[str, Any], label: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for bottle in value.get("bottles", ()):
        if not isinstance(bottle, dict) or not isinstance(bottle.get("arch"), str):
            continue
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
        "version": identity.version,
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
    for arch in arches:
        aggregate = aggregate_bottles.get(arch)
        formula_bottle = sidecar_bottles.get(arch)
        if aggregate is None or formula_bottle is None:
            reasons.append(f"{arch} is missing from aggregate or sidecar")
            continue
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
            if built_from.get("kandelo_commit") != expected_kandelo_sha:
                reasons.append(f"{label} {arch} was built from another Kandelo SHA")
            if built_from.get("tap_repository", "").lower() != REPOSITORY.lower():
                reasons.append(f"{label} {arch} was built from another tap")

        built_from = aggregate.get("built_from")
        if not isinstance(built_from, dict):
            continue
        source_sha = built_from.get("tap_commit")
        source_formula_sha = built_from.get("formula_sha256")
        if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            reasons.append(f"{arch} source tap SHA is invalid")
            continue
        try:
            if not tap.is_ancestor(source_sha, snapshot.sha):
                reasons.append(f"{arch} source tap SHA is not on current main")
                continue
            source_formula = tap.show(source_sha, f"Formula/{formula}.rb")
            actual_formula_sha = hashlib.sha256(source_formula.encode()).hexdigest()
            if source_formula_sha != actual_formula_sha:
                reasons.append(f"{arch} source Formula digest is wrong")
            source_identity = parse_formula_identity(formula, source_formula, package)
            if source_identity.state_value() != identity.state_value():
                reasons.append(f"{arch} source Formula identity differs")
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
        total += count
        for run in response["workflow_runs"]:
            if isinstance(run, dict) and isinstance(run.get("id"), int):
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


def required_arches(formula: str) -> tuple[str, ...]:
    return ("wasm32", "wasm64") if formula in DUAL_ARCH_FORMULAE else ("wasm32",)


def dependency_arch(dependency: str, target_arch: str) -> str:
    if target_arch == "wasm64" and dependency in DUAL_ARCH_FORMULAE:
        return "wasm64"
    return "wasm32"


def catalog_state(snapshot: TapSnapshot) -> dict[str, Any]:
    return {
        name: snapshot.identities[name].state_value()
        for name in FORMULA_ORDER
    }


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
        "cutover_tap_sha": snapshot.sha,
        "catalog": catalog_state(snapshot),
        "unresolved_dispatch": None,
        "dispatches": [],
    }


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
        "catalog": catalog_state(snapshot),
    }
    for field, expected in fixed.items():
        if state.get(field) != expected:
            raise RolloutError(
                f"rollout state {field} differs from current reviewed cutover"
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
    expected_arches = set(arches)
    while time.monotonic() < deadline:
        response = github.runs(per_page=100)
        candidates: list[int] = []
        for run in response["workflow_runs"]:
            if not isinstance(run, dict) or not isinstance(run.get("id"), int):
                continue
            if run["id"] in before_ids:
                continue
            if run.get("event") != "repository_dispatch" or run.get("head_sha") != tap_sha:
                continue
            jobs = github.jobs(run["id"])
            found_formulae = run_formulae(jobs)
            found_arches = {
                arch
                for job in jobs
                for arch in re.findall(
                    rf"\({re.escape(formula)},\s+(wasm32|wasm64)\)",
                    str(job.get("name", "")),
                )
            }
            if found_formulae == frozenset((formula,)) and expected_arches <= found_arches:
                candidates.append(run["id"])
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
) -> int:
    state = read_state(state_path)
    dispatched = 0
    while dispatched < maximum:
        sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
        snapshot = load_snapshot(tap, sha)
        validate_workflow(github, snapshot, expected_kandelo_sha)
        if state is None:
            state = initial_state(snapshot, expected_kandelo_sha)
            write_state(state_path, state)
        validate_state(state, snapshot, expected_kandelo_sha)
        if state.get("unresolved_dispatch") is not None:
            raise RolloutError(
                f"{state_path} contains an unresolved dispatch; inspect it before continuing"
            )

        inventory = active_inventory(github)
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
        ready = [status for status in statuses if status.state == "ready"]
        if not ready:
            return dispatched

        selected = ready[0]
        # Refresh both main and the active-run count immediately before the
        # write. A moving tap or newly queued run invalidates the plan instead
        # of consuming an unchecked ninth slot.
        latest_sha = tap.main_without_fetch() if no_fetch else tap.fetch_main()
        if latest_sha != snapshot.sha:
            continue
        latest_inventory = active_inventory(github)
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
        recent = github.runs(per_page=100)
        before_ids = frozenset(
            run["id"]
            for run in recent["workflow_runs"]
            if isinstance(run, dict) and isinstance(run.get("id"), int)
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
            github.dispatch(selected.name, selected.arches)
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
        help="durable local dispatch ledger (required with --dispatch)",
    )
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="explicitly create fresh production repository_dispatch events",
    )
    parser.add_argument(
        "--max-dispatches",
        type=int,
        default=MAX_ACTIVE_RUNS,
        help="maximum fresh dispatches in this invocation (default: 8)",
    )
    parser.add_argument(
        "--ack-timeout",
        type=int,
        default=120,
        help="seconds to wait for each unambiguous new run ID",
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
    if args.dispatch and args.state_file is None:
        parser.error("--state-file is required with --dispatch")
    if args.max_dispatches < 1 or args.max_dispatches > MAX_ACTIVE_RUNS:
        parser.error(f"--max-dispatches must be between 1 and {MAX_ACTIVE_RUNS}")
    if args.ack_timeout < 1 or args.poll_seconds <= 0:
        parser.error("acknowledgement timeout and poll interval must be positive")
    return args


def main(argv: Sequence[str] = sys.argv[1:]) -> int:
    args = parse_args(argv)
    try:
        tap = GitTap(args.tap_root)
        github = GitHub()
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
