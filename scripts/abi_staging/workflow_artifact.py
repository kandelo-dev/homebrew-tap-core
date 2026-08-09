"""Authenticated, bounded bridge from protected GitHub jobs to inert files."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import zipfile


REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REDIRECTS = frozenset({301, 302, 303, 307, 308})
API_MAXIMUM = 4 * 1024 * 1024
ALLOWED_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "startup_failure",
        "success",
        "timed_out",
    }
)


class WorkflowArtifactError(ValueError):
    """Raised when GitHub run metadata or inert artifact bytes are ambiguous."""


class WorkflowArtifactServiceError(WorkflowArtifactError):
    """Bounded protected fact for a retryable GitHub/artifact transport failure."""

    def __init__(
        self, message: str, *, kind: str, http_status: int | None
    ) -> None:
        if kind not in {
            "artifact-service-unavailable",
            "github-http",
            "transport-reset",
        }:
            raise ValueError("workflow service failure kind is unsupported")
        if http_status is not None and (
            isinstance(http_status, bool)
            or not isinstance(http_status, int)
            or not 100 <= http_status <= 599
        ):
            raise ValueError("workflow service HTTP status is invalid")
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status


@dataclass(frozen=True)
class WorkflowJobV1:
    id: int
    name: str
    conclusion: str
    completed_at: str


@dataclass(frozen=True)
class WorkflowArtifactV1:
    id: int
    name: str
    sha256: str
    size_in_bytes: int
    job_id: int
    job_conclusion: str
    job_completed_at: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        return None


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**64 - 1:
        raise WorkflowArtifactError(f"{field} must be a positive integer")
    return value


def _text(value: Any, field: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise WorkflowArtifactError(f"{field} must be bounded text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise WorkflowArtifactError(f"{field} is not UTF-8") from error
    if size > maximum:
        raise WorkflowArtifactError(f"{field} exceeds its byte bound")
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowArtifactError(f"{field} must be an object")
    return value


def _timestamp(value: Any, field: str) -> str:
    text = _text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise WorkflowArtifactError(f"{field} is not an RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise WorkflowArtifactError(f"{field} lacks a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WorkflowArtifactError(f"{field} must be an array")
    return value


class GitHubWorkflowArtifactClientV1:
    """Read only artifacts produced by an exact job in the current protected run."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        run_id: int,
        run_attempt: int,
        head_sha: str,
        workflow_ref: str,
        opener=None,
    ) -> None:
        if REPOSITORY.fullmatch(repository) is None:
            raise WorkflowArtifactError("workflow repository is invalid")
        if not token or any(character.isspace() for character in token):
            raise WorkflowArtifactError("workflow GitHub token is missing or malformed")
        self.repository = repository
        self.run_id = _positive(run_id, "workflow run ID")
        self.run_attempt = _positive(run_attempt, "workflow run attempt")
        if GIT_SHA.fullmatch(head_sha) is None:
            raise WorkflowArtifactError("workflow head is not a full lowercase SHA")
        self.head_sha = head_sha
        expected_ref = (
            f"{repository}/.github/workflows/abi-staging-reconcile.yml@refs/heads/main"
        )
        if workflow_ref != expected_ref:
            raise WorkflowArtifactError("workflow ref is not protected tap main")
        self.workflow_ref = workflow_ref
        self._token = token
        if opener is None:
            built = urllib.request.build_opener(_NoRedirect())
            self._opener = lambda request: built.open(request, timeout=30)
        else:
            self._opener = opener
        self._run_validated = False

    def _open(
        self,
        url: str,
        *,
        authenticated: bool,
        maximum: int,
        accept: str,
    ) -> tuple[int, dict[str, str], bytes]:
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise WorkflowArtifactError(f"workflow URL is invalid: {error}") from error
        api = parsed.hostname == "api.github.com"
        artifact_host = (
            parsed.hostname == "objects.githubusercontent.com"
            or parsed.hostname == "pipelines.actions.githubusercontent.com"
            or (parsed.hostname or "").endswith(".actions.githubusercontent.com")
            or (parsed.hostname or "").endswith(".blob.core.windows.net")
        )
        if (
            parsed.scheme != "https"
            or not (api if authenticated else artifact_host)
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.fragment
        ):
            raise WorkflowArtifactError("workflow URL escaped its HTTPS host boundary")
        headers = {
            "Accept": accept,
            "User-Agent": "kandelo-abi-staging-workflow/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if authenticated:
            headers["Authorization"] = "Bearer " + self._token
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self._opener(request)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError) as error:
            raise WorkflowArtifactServiceError(
                f"workflow GitHub request failed: {error}",
                kind="transport-reset",
                http_status=None,
            ) from error
        try:
            status = int(response.status)
            body = response.read(maximum + 1)
            if len(body) > maximum:
                raise WorkflowArtifactError("workflow GitHub response exceeded its byte bound")
            response_headers = {
                str(key).lower(): str(value) for key, value in response.headers.items()
            }
            length = response_headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) != len(body)):
                raise WorkflowArtifactError("workflow GitHub response length is contradictory")
            return status, response_headers, body
        finally:
            response.close()

    def _api(self, path: str) -> Mapping[str, Any]:
        status, _, body = self._open(
            "https://api.github.com" + path,
            authenticated=True,
            maximum=API_MAXIMUM,
            accept="application/vnd.github+json",
        )
        if status != 200:
            if status == 429 or 500 <= status <= 599:
                kind = (
                    "artifact-service-unavailable"
                    if "/artifacts" in path
                    else "github-http"
                )
                raise WorkflowArtifactServiceError(
                    f"workflow GitHub API returned HTTP {status}",
                    kind=kind,
                    http_status=status,
                )
            raise WorkflowArtifactError(f"workflow GitHub API returned HTTP {status}")
        try:
            value = json.loads(body.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkflowArtifactError(f"workflow GitHub API returned invalid JSON: {error}") from error
        return _mapping(value, "workflow GitHub API response")

    def _validate_current_run(self) -> None:
        if self._run_validated:
            return
        run = self._api(
            f"/repos/{self.repository}/actions/runs/{self.run_id}/attempts/{self.run_attempt}"
        )
        head_repository = _mapping(run.get("head_repository"), "workflow head repository")
        if (
            run.get("id") != self.run_id
            or run.get("run_attempt") != self.run_attempt
            or run.get("event") not in {"schedule", "workflow_dispatch"}
            or run.get("head_branch") != "main"
            or run.get("head_sha") != self.head_sha
            or run.get("path") != ".github/workflows/abi-staging-reconcile.yml"
            or run.get("status") not in {"queued", "in_progress", "completed"}
            or run.get("conclusion") not in {None, "success", "failure", "cancelled"}
            or head_repository.get("full_name") != self.repository
        ):
            raise WorkflowArtifactError("current run is not protected reconciliation")
        self._run_validated = True

    def artifact_for_job(
        self,
        *,
        name: str,
        job_name: str,
        allowed_conclusions: tuple[str, ...],
        required: bool = True,
    ) -> WorkflowArtifactV1 | None:
        """Resolve one immutable artifact after verifying its exact producing job."""

        expected_name = _text(name, "workflow artifact name", 512)
        job = self.job_for_name(
            job_name, allowed_conclusions=allowed_conclusions
        )
        conclusion = job.conclusion
        job_id = job.id
        completed_at = job.completed_at
        artifacts_value = self._api(
            f"/repos/{self.repository}/actions/runs/{self.run_id}/artifacts?"
            "per_page=100&name=" + urllib.parse.quote(expected_name, safe="")
        )
        artifacts = _sequence(artifacts_value.get("artifacts"), "workflow artifacts")
        if artifacts_value.get("total_count") != len(artifacts) or len(artifacts) > 100:
            raise WorkflowArtifactError("workflow artifact inventory is incomplete or unbounded")
        matches = [
            _mapping(artifact, "workflow artifact")
            for artifact in artifacts
            if artifact.get("name") == expected_name
        ]
        if not matches and not required:
            return None
        if len(matches) != 1:
            raise WorkflowArtifactError("workflow does not contain one exact artifact")
        artifact = matches[0]
        digest = artifact.get("digest")
        workflow_run = _mapping(artifact.get("workflow_run"), "artifact workflow run")
        if (
            artifact.get("expired") is not False
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or SHA256.fullmatch(digest[7:]) is None
            or workflow_run.get("id") != self.run_id
            or workflow_run.get("head_sha") != self.head_sha
        ):
            raise WorkflowArtifactError("workflow artifact metadata is not exact and immutable")
        size = _positive(artifact.get("size_in_bytes"), "workflow artifact size")
        return WorkflowArtifactV1(
            id=_positive(artifact.get("id"), "workflow artifact ID"),
            name=expected_name,
            sha256=digest[7:],
            size_in_bytes=size,
            job_id=job_id,
            job_conclusion=str(conclusion),
            job_completed_at=completed_at,
        )

    def job_for_name(
        self,
        job_name: str,
        *,
        allowed_conclusions: tuple[str, ...],
    ) -> WorkflowJobV1:
        """Resolve one exact completed job in the current protected attempt."""

        self._validate_current_run()
        expected_job = _text(job_name, "workflow job name", 1024)
        if (
            not allowed_conclusions
            or len(set(allowed_conclusions)) != len(allowed_conclusions)
            or not set(allowed_conclusions).issubset(ALLOWED_CONCLUSIONS)
        ):
            raise WorkflowArtifactError("allowed job conclusions are invalid")
        jobs_value = self._api(
            f"/repos/{self.repository}/actions/runs/{self.run_id}/attempts/"
            f"{self.run_attempt}/jobs?per_page=100"
        )
        jobs = _sequence(jobs_value.get("jobs"), "workflow jobs")
        if jobs_value.get("total_count") != len(jobs) or len(jobs) > 100:
            raise WorkflowArtifactError("workflow job inventory is incomplete or unbounded")
        matches = [
            _mapping(job, "workflow job") for job in jobs if job.get("name") == expected_job
        ]
        if len(matches) != 1:
            raise WorkflowArtifactError("workflow does not contain one exact producing job")
        job = matches[0]
        conclusion = job.get("conclusion")
        if (
            job.get("run_id") != self.run_id
            or job.get("run_attempt") != self.run_attempt
            or job.get("head_sha") != self.head_sha
            or job.get("status") != "completed"
            or conclusion not in allowed_conclusions
        ):
            raise WorkflowArtifactError("producing job identity or conclusion differs")
        job_id = _positive(job.get("id"), "workflow job ID")
        completed_at = _timestamp(job.get("completed_at"), "workflow job completion")
        return WorkflowJobV1(
            id=job_id,
            name=expected_job,
            conclusion=str(conclusion),
            completed_at=completed_at,
        )

    def extract_artifact(
        self,
        artifact: WorkflowArtifactV1,
        destination: Path,
        *,
        max_files: int,
        max_bytes: int,
    ) -> dict[str, dict[str, int | str]]:
        """Download one exact ZIP and extract regular inert files into a new root."""

        if not 1 <= max_files <= 65_536 or not 1 <= max_bytes <= 16 * 1024**3:
            raise WorkflowArtifactError("workflow extraction bounds are invalid")
        if artifact.size_in_bytes > max_bytes:
            raise WorkflowArtifactError("workflow artifact exceeds its protected byte bound")
        download_url = (
            f"https://api.github.com/repos/{self.repository}/actions/artifacts/"
            f"{artifact.id}/zip"
        )
        status, headers, body = self._open(
            download_url,
            authenticated=True,
            maximum=1024,
            accept="application/vnd.github+json",
        )
        if status == 429 or 500 <= status <= 599:
            raise WorkflowArtifactServiceError(
                f"workflow artifact service returned HTTP {status}",
                kind="artifact-service-unavailable",
                http_status=status,
            )
        if status not in REDIRECTS or body:
            raise WorkflowArtifactError("workflow artifact download did not return one redirect")
        location = headers.get("location")
        if location is None:
            raise WorkflowArtifactError("workflow artifact redirect omitted Location")
        status, _, archive = self._open(
            location,
            authenticated=False,
            maximum=artifact.size_in_bytes,
            accept="application/octet-stream",
        )
        if status == 429 or 500 <= status <= 599:
            raise WorkflowArtifactServiceError(
                f"workflow artifact download returned HTTP {status}",
                kind="artifact-service-unavailable",
                http_status=status,
            )
        if status != 200 or len(archive) != artifact.size_in_bytes:
            raise WorkflowArtifactError("workflow artifact download is incomplete")
        if hashlib.sha256(archive).hexdigest() != artifact.sha256:
            raise WorkflowArtifactError("workflow artifact bytes differ from GitHub digest")
        try:
            if destination.exists() or destination.is_symlink():
                metadata = destination.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise WorkflowArtifactError("artifact destination must be a real directory")
                if any(destination.iterdir()):
                    raise WorkflowArtifactError("artifact destination must be empty")
            else:
                destination.mkdir()
            inventory: dict[str, dict[str, int | str]] = {}
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                entries = bundle.infolist()
                if not entries or len(entries) > max_files:
                    raise WorkflowArtifactError("workflow artifact file count is outside its bound")
                total = 0
                seen: set[str] = set()
                for entry in entries:
                    name = entry.filename
                    path = PurePosixPath(name)
                    mode = (entry.external_attr >> 16) & 0o170000
                    if (
                        not name
                        or "\\" in name
                        or path.is_absolute()
                        or any(part in {"", ".", ".."} for part in path.parts)
                        or name in seen
                        or mode == stat.S_IFLNK
                        or (mode not in {0, stat.S_IFREG, stat.S_IFDIR})
                        or entry.file_size < 0
                        or entry.compress_size < 0
                    ):
                        raise WorkflowArtifactError("workflow artifact inventory is unsafe")
                    seen.add(name)
                    if entry.is_dir():
                        continue
                    total += entry.file_size
                    if total > max_bytes:
                        raise WorkflowArtifactError("workflow artifact expands beyond its byte bound")
                    target = destination.joinpath(*path.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    count = 0
                    with bundle.open(entry) as source, target.open("xb") as output:
                        while True:
                            chunk = source.read(min(1024 * 1024, max_bytes - count + 1))
                            if not chunk:
                                break
                            count += len(chunk)
                            if count > entry.file_size or count > max_bytes:
                                raise WorkflowArtifactError("workflow artifact entry exceeded its bound")
                            digest.update(chunk)
                            output.write(chunk)
                    if count != entry.file_size:
                        raise WorkflowArtifactError("workflow artifact entry is incomplete")
                    inventory[name] = {"sha256": digest.hexdigest(), "bytes": count}
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            if isinstance(error, WorkflowArtifactError):
                raise
            raise WorkflowArtifactError(f"workflow artifact ZIP is invalid: {error}") from error
        return {name: inventory[name] for name in sorted(inventory)}
