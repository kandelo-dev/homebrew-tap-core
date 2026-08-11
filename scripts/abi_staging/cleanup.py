"""Exact retention, deletion, and tombstone contracts for ABI staging objects."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Protocol
from urllib.parse import quote

from .canonical import canonical_bytes, canonical_sha256
from .github_public import GitHubPublicClient
from .oci import (
    HttpResponseV1,
    OciTransportV1,
    UrllibOciTransportV1,
    isolated_oras_transport,
    publish_record,
)
from .plan import snapshot_tap_source
from .policy import TapStagingPolicyV1, load_tap_staging_policy
from .request import load_request_issuer_policy
from .records import OciBlobV1, OciRecordPlanV1


SHA256 = re.compile(r"^[0-9a-f]{64}$")
OCI_REPOSITORY = re.compile(
    r"^ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
MAX_TARGETS = 4096
MAX_REFERENCES = 65_536
MAX_JUSTIFICATION_BYTES = 4096
MAX_GITHUB_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PACKAGES = 4096
MAX_PACKAGE_VERSIONS = 4096
MAX_CLEANUP_BATCH = 256
PIN_KINDS = frozenset(
    {
        "open-request",
        "merged-admission",
        "active-verification",
        "active-product",
        "active-promotion",
        "active-repair",
        "candidate-reuse",
        "shared-custody",
        "canonical-layer",
    }
)
NONPIN_KINDS = frozenset({"historical-identity"})
IMMEDIATE_REASONS = frozenset(
    {"malicious-object", "legal-removal", "pathological-size"}
)
DELETION_RECORD_MEDIA_TYPE = (
    "application/vnd.kandelo.abi-staging.deletion-record.v1+json"
)


class CleanupError(ValueError):
    """Raised when retention evidence cannot authorize one exact deletion."""


@dataclass(frozen=True)
class RetentionAssessmentV1:
    target_digest: str
    artifact_class: str
    pins: tuple[Mapping[str, str], ...]
    unreferenced_since: str | None
    grace_complete: bool
    deletion_eligible: bool
    reason: str


class RegistryDeletionV1(Protocol):
    def probe_anonymous(self, target: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def resolve_exact_version(
        self, target: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def delete_exact_version(self, version: Mapping[str, Any]) -> None: ...


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CleanupError(f"cleanup API JSON repeats field {key!r}")
        result[key] = value
    return result


def _json(body: bytes, field: str) -> Any:
    def reject_float(value: str) -> None:
        raise CleanupError(f"{field} contains unsupported number {value}")

    def reject_constant(value: str) -> None:
        raise CleanupError(f"{field} contains unsupported constant {value}")

    try:
        return json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_pairs,
            parse_float=reject_float,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CleanupError(f"{field} is invalid JSON: {error}") from error


def verify_cleanup_plan_artifact(
    *,
    repository: str,
    artifact_id: int,
    artifact_digest: str,
    run_id: int,
    run_attempt: int,
    head_sha: str,
    workflow_ref: str,
    transport: OciTransportV1,
) -> dict[str, Any]:
    """Bind one downloaded plan to this protected cleanup run's exact upload."""

    if re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository) is None:
        raise CleanupError("cleanup workflow repository is invalid")
    checked_id = _positive(artifact_id, "cleanup plan artifact ID")
    checked_digest = _digest(artifact_digest, "cleanup plan artifact digest")
    checked_run = _positive(run_id, "cleanup workflow run ID")
    checked_attempt = _positive(run_attempt, "cleanup workflow run attempt")
    if re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
        raise CleanupError("cleanup workflow head is not a full lowercase SHA")
    expected_path = ".github/workflows/abi-staging-candidate-cleanup.yml"
    if workflow_ref != f"{repository}/{expected_path}@refs/heads/main":
        raise CleanupError("cleanup workflow ref is not protected tap main")

    def api(path: str, field: str) -> Mapping[str, Any]:
        url = "https://api.github.com" + path
        response = transport.request(
            "GET",
            url,
            headers={"accept": "application/vnd.github+json"},
            body=None,
            authenticated=True,
            maximum_bytes=MAX_GITHUB_RESPONSE_BYTES,
        )
        if response.url != url or response.status != 200:
            raise CleanupError(f"{field} is unavailable from its exact GitHub URL")
        return _mapping(_json(response.body, field), field)

    run = api(
        f"/repos/{repository}/actions/runs/{checked_run}/attempts/{checked_attempt}",
        "cleanup workflow run",
    )
    head_repository = _mapping(
        run.get("head_repository"), "cleanup workflow head repository"
    )
    if (
        run.get("id") != checked_run
        or run.get("run_attempt") != checked_attempt
        or run.get("event") not in {"schedule", "workflow_dispatch"}
        or run.get("head_branch") != "main"
        or run.get("head_sha") != head_sha
        or run.get("path") != expected_path
        or run.get("status") not in {"queued", "in_progress", "completed"}
        or run.get("conclusion")
        not in {None, "success", "failure", "cancelled"}
        or head_repository.get("full_name") != repository
    ):
        raise CleanupError("cleanup workflow run is not protected and exact")

    artifact = api(
        f"/repos/{repository}/actions/artifacts/{checked_id}",
        "cleanup plan artifact",
    )
    workflow_run = _mapping(
        artifact.get("workflow_run"), "cleanup artifact workflow run"
    )
    expected_name = f"abi-staging-cleanup-plan-{checked_run}-{checked_attempt}"
    if (
        artifact.get("id") != checked_id
        or artifact.get("name") != expected_name
        or artifact.get("expired") is not False
        or artifact.get("digest") != "sha256:" + checked_digest
        or workflow_run.get("id") != checked_run
        or workflow_run.get("head_sha") != head_sha
    ):
        raise CleanupError("cleanup plan artifact metadata is not exact and immutable")
    size = _positive(artifact.get("size_in_bytes"), "cleanup plan artifact size")
    return {
        "artifact_id": checked_id,
        "artifact_digest": checked_digest,
        "artifact_name": expected_name,
        "artifact_bytes": size,
        "run_id": checked_run,
        "run_attempt": checked_attempt,
        "head_sha": head_sha,
    }


class GitHubRetentionInventoryClientV1:
    """Read the tap's exact public record graph without executing any record."""

    def __init__(
        self,
        *,
        expected_source_repository: str,
        package_prefix: str,
        transport: OciTransportV1,
    ) -> None:
        if (
            re.fullmatch(
                r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+",
                expected_source_repository,
            )
            is None
        ):
            raise CleanupError("retention source repository is invalid")
        if (
            not isinstance(package_prefix, str)
            or re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*-", package_prefix)
            is None
        ):
            raise CleanupError("retention package prefix is invalid")
        self.expected_source_repository = expected_source_repository
        self.package_prefix = package_prefix
        self.transport = transport

    def _request(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool,
        maximum_bytes: int = MAX_GITHUB_RESPONSE_BYTES,
        headers: dict[str, str] | None = None,
    ) -> HttpResponseV1:
        response = self.transport.request(
            method,
            url,
            headers=headers,
            body=None,
            authenticated=authenticated,
            maximum_bytes=maximum_bytes,
        )
        if response.url != url:
            raise CleanupError("retention inventory request changed its exact URL")
        return response

    def _packages(self) -> tuple[str, ...]:
        owner, _name = self.expected_source_repository.split("/", 1)
        packages: list[str] = []
        seen: set[str] = set()
        for page in range(1, MAX_PACKAGES // 100 + 2):
            url = (
                "https://api.github.com/orgs/"
                + quote(owner, safe="")
                + f"/packages?package_type=container&per_page=100&page={page}"
            )
            response = self._request("GET", url, authenticated=True)
            if response.status != 200:
                raise CleanupError(
                    f"retention package inventory returned HTTP {response.status}"
                )
            values = _sequence(_json(response.body, "retention packages"), "retention packages")
            if len(values) > 100:
                raise CleanupError("retention package page exceeds its bound")
            for index, candidate in enumerate(values):
                package = _mapping(candidate, f"retention package {index}")
                name = _text(package.get("name"), "retention package name", 512)
                if name in seen:
                    raise CleanupError("retention package inventory repeated a name")
                seen.add(name)
                if not name.startswith(self.package_prefix):
                    continue
                association = _mapping(
                    package.get("repository"), "retention package association"
                )
                if (
                    package.get("package_type") != "container"
                    or package.get("visibility") != "public"
                    or association.get("full_name") != self.expected_source_repository
                    or re.fullmatch(
                        r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*",
                        name,
                    )
                    is None
                ):
                    raise CleanupError(
                        "retention package is not an exact public tap package"
                    )
                packages.append(name)
                if len(packages) > MAX_PACKAGES:
                    raise CleanupError("retention package inventory exceeds its bound")
            if len(values) < 100:
                break
        else:
            raise CleanupError("retention package pagination did not terminate")
        return tuple(sorted(packages))

    def _record_locators(self, package: str) -> tuple[dict[str, str], ...]:
        owner, _name = self.expected_source_repository.split("/", 1)
        base = (
            "https://api.github.com/orgs/"
            + quote(owner, safe="")
            + "/packages/container/"
            + quote(package, safe="")
        )
        locators: list[dict[str, str]] = []
        seen_ids: set[int] = set()
        for page in range(1, MAX_PACKAGE_VERSIONS // 100 + 2):
            url = base + f"/versions?per_page=100&page={page}"
            response = self._request("GET", url, authenticated=True)
            if response.status != 200:
                raise CleanupError(
                    f"retention package versions returned HTTP {response.status}"
                )
            versions = _sequence(
                _json(response.body, "retention package versions"),
                "retention package versions",
            )
            if len(versions) > 100:
                raise CleanupError("retention package version page exceeds its bound")
            for index, candidate in enumerate(versions):
                version = _mapping(candidate, f"retention package version {index}")
                identifier = _positive(version.get("id"), "retention package version ID")
                if identifier in seen_ids:
                    raise CleanupError("retention package versions repeated an ID")
                seen_ids.add(identifier)
                digest_value = _text(
                    version.get("name"), "retention package version digest", 71
                )
                if not digest_value.startswith("sha256:"):
                    raise CleanupError("retention package version is not digest-named")
                digest = _digest(
                    digest_value.removeprefix("sha256:"),
                    "retention package version digest",
                )
                metadata = _mapping(
                    version.get("metadata"), "retention package version metadata"
                )
                container = _mapping(
                    metadata.get("container"), "retention container metadata"
                )
                tags = [
                    _text(tag, "retention version tag", 255)
                    for tag in _sequence(
                        container.get("tags"), "retention version tags"
                    )
                ]
                if len(tags) != len(set(tags)):
                    raise CleanupError("retention version tags are not unique")
                tags.sort()
                allowed = {
                    "record-sha256-" + digest,
                    "canonical-sha256-" + digest,
                }
                if any(tag not in allowed for tag in tags):
                    raise CleanupError("retention package contains a mutable or unknown tag")
                record_tag = "record-sha256-" + digest
                if record_tag in tags:
                    repository = f"ghcr.io/{owner}/{package}"
                    locators.append(
                        {
                            "repository": repository,
                            "digest": "sha256:" + digest,
                            "immutable_reference": repository + "@sha256:" + digest,
                        }
                    )
            if len(seen_ids) > MAX_PACKAGE_VERSIONS:
                raise CleanupError("retention package versions exceed their bound")
            if len(versions) < 100:
                break
        else:
            raise CleanupError("retention package version pagination did not terminate")
        return tuple(locators)

    def _record(self, locator: Mapping[str, str]) -> dict[str, Any]:
        repository = locator["repository"].removeprefix("ghcr.io/")
        digest = locator["digest"]
        manifest_url = (
            "https://ghcr.io/v2/"
            + quote(repository, safe="/")
            + "/manifests/"
            + digest
        )
        response = self._request(
            "GET",
            manifest_url,
            authenticated=False,
            headers={"accept": "application/vnd.oci.image.manifest.v1+json"},
        )
        if (
            response.status != 200
            or hashlib.sha256(response.body).hexdigest()
            != digest.removeprefix("sha256:")
        ):
            raise CleanupError("public retention record manifest is absent or changed")
        observed = {str(key).lower(): str(value) for key, value in response.headers.items()}.get(
            "docker-content-digest"
        )
        if observed not in {None, digest}:
            raise CleanupError("public retention record reported another digest")
        manifest = _exact(
            _json(response.body, "retention OCI manifest"),
            frozenset(
                {
                    "schemaVersion",
                    "mediaType",
                    "artifactType",
                    "config",
                    "layers",
                    "annotations",
                }
            ),
            "retention OCI manifest",
        )
        if (
            manifest["schemaVersion"] != 2
            or manifest["mediaType"] != "application/vnd.oci.image.manifest.v1+json"
        ):
            raise CleanupError("retention OCI manifest protocol changed")
        config = _exact(
            manifest["config"],
            frozenset({"mediaType", "digest", "size", "annotations"}),
            "retention config descriptor",
        )
        config_media_type = _text(
            config["mediaType"], "retention config media type", 256
        )
        if manifest["artifactType"] != config_media_type:
            raise CleanupError("retention artifact and config media types differ")
        config_digest_value = _text(
            config["digest"], "retention config digest", 71
        )
        if not config_digest_value.startswith("sha256:"):
            raise CleanupError("retention config digest is invalid")
        config_digest = _digest(
            config_digest_value.removeprefix("sha256:"), "retention config digest"
        )
        config_size = _positive(config["size"], "retention config size")
        if config_size > MAX_GITHUB_RESPONSE_BYTES:
            raise CleanupError("retention config exceeds its byte bound")
        _mapping(config["annotations"], "retention config annotations")
        _sequence(manifest["layers"], "retention OCI layers")
        annotations = _mapping(manifest["annotations"], "retention OCI annotations")
        if annotations.get("org.opencontainers.image.source") != (
            "https://github.com/" + self.expected_source_repository
        ):
            raise CleanupError("retention record source association changed")
        config_url = (
            "https://ghcr.io/v2/"
            + quote(repository, safe="/")
            + "/blobs/sha256:"
            + config_digest
        )
        config_response = self._request(
            "GET",
            config_url,
            authenticated=False,
            maximum_bytes=config_size,
        )
        if (
            config_response.status != 200
            or len(config_response.body) != config_size
            or hashlib.sha256(config_response.body).hexdigest() != config_digest
        ):
            raise CleanupError("public retention config is absent or byte-drifted")
        record = dict(
            _mapping(_json(config_response.body, "public retention record"), "public retention record")
        )
        if canonical_bytes(record) != config_response.body:
            raise CleanupError("public retention record is not canonical JSON")
        return record

    def scan_records(self) -> tuple[dict[str, Any], ...]:
        records: list[dict[str, Any]] = []
        for package in self._packages():
            for locator in self._record_locators(package):
                records.append({"locator": locator, "record": self._record(locator)})
                if len(records) > MAX_REFERENCES:
                    raise CleanupError("public retention record inventory exceeds its bound")
        records.sort(key=lambda candidate: candidate["locator"]["immutable_reference"])
        return tuple(records)

    def pull_request_lifecycle(
        self,
        *,
        repository: str,
        number: int,
        request_reference: str,
    ) -> dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository) is None:
            raise CleanupError("retention pull-request repository is invalid")
        _positive(number, "retention pull-request number")
        url = (
            "https://api.github.com/repos/"
            + repository
            + "/pulls/"
            + str(number)
        )
        response = self._request("GET", url, authenticated=True)
        if response.status != 200:
            raise CleanupError(
                f"retention pull-request lifecycle returned HTTP {response.status}"
            )
        value = _mapping(
            _json(response.body, "retention pull-request lifecycle"),
            "retention pull-request lifecycle",
        )
        state = value.get("state")
        merged_at = value.get("merged_at")
        closed_at = value.get("closed_at")
        if state == "open":
            if merged_at is not None or closed_at is not None:
                raise CleanupError("open retention request has a close or merge time")
            lifecycle_state = "open"
            lifecycle_closed_at = None
        elif state == "closed" and merged_at is not None:
            _timestamp(merged_at, "retention merge time")
            if closed_at is None:
                raise CleanupError("merged retention request lacks a close time")
            _timestamp(closed_at, "retention merged close time")
            lifecycle_state = "merged"
            lifecycle_closed_at = None
        elif state == "closed" and merged_at is None:
            _timestamp(closed_at, "retention unmerged close time")
            lifecycle_state = "closed-unmerged"
            lifecycle_closed_at = closed_at
        else:
            raise CleanupError("retention pull-request lifecycle is contradictory")
        return _lifecycle(
            {
                "state": lifecycle_state,
                "closed_at": lifecycle_closed_at,
                "request_reference": request_reference,
            },
            "retention pull-request lifecycle",
        )


class GitHubPackageDeletionClientV1:
    """Delete one GHCR package version only after exact API/digest resolution."""

    def __init__(
        self,
        *,
        expected_source_repository: str,
        transport: OciTransportV1,
    ) -> None:
        if (
            not isinstance(expected_source_repository, str)
            or re.fullmatch(
                r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+",
                expected_source_repository,
            )
            is None
        ):
            raise CleanupError("cleanup source repository is invalid")
        self.expected_source_repository = expected_source_repository
        self.transport = transport
        self._resolved: dict[int, tuple[str, str]] = {}

    @staticmethod
    def _parts(target: Mapping[str, Any]) -> tuple[str, str, str]:
        checked = _target(target)
        without_host = checked["repository"][len("ghcr.io/") :]
        owner, package = without_host.split("/", 1)
        return owner, package, without_host

    def _request(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool,
        maximum_bytes: int = MAX_GITHUB_RESPONSE_BYTES,
        headers: dict[str, str] | None = None,
    ) -> HttpResponseV1:
        response = self.transport.request(
            method,
            url,
            headers=headers,
            body=None,
            authenticated=authenticated,
            maximum_bytes=maximum_bytes,
        )
        if response.url != url:
            raise CleanupError("cleanup HTTP request followed or changed its exact target")
        return response

    def probe_anonymous(self, target: Mapping[str, Any]) -> dict[str, Any]:
        checked = _target(target)
        _owner, _package, repository = self._parts(checked)
        digest = "sha256:" + checked["target_digest"]
        url = (
            "https://ghcr.io/v2/"
            + quote(repository, safe="/")
            + "/manifests/"
            + digest
        )
        response = self._request(
            "GET",
            url,
            authenticated=False,
            headers={"accept": "application/vnd.oci.image.manifest.v1+json"},
        )
        if response.status == 404:
            return {
                "status": 404,
                "url": checked["immutable_reference"],
                "digest": None,
            }
        if response.status != 200 or hashlib.sha256(response.body).hexdigest() != checked[
            "target_digest"
        ]:
            raise CleanupError("anonymous GHCR manifest probe is absent or byte-drifted")
        headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
        observed = headers.get("docker-content-digest")
        if observed not in {None, digest}:
            raise CleanupError("anonymous GHCR manifest reported another digest")
        return {
            "status": 200,
            "url": checked["immutable_reference"],
            "digest": digest,
        }

    def _package_base(self, target: Mapping[str, Any]) -> tuple[str, str, str]:
        checked = _target(target)
        owner, package, _repository = self._parts(checked)
        expected_owner, _expected_name = self.expected_source_repository.split("/", 1)
        if owner.lower() != expected_owner.lower():
            raise CleanupError("cleanup target owner differs from protected tap")
        base = (
            "https://api.github.com/orgs/"
            + quote(owner, safe="")
            + "/packages/container/"
            + quote(package, safe="")
        )
        return base, package, checked["repository"]

    def resolve_exact_version(self, target: Mapping[str, Any]) -> dict[str, Any]:
        checked = _target(target)
        base, package, repository = self._package_base(checked)
        metadata_response = self._request(
            "GET",
            base,
            authenticated=True,
            headers={"accept": "application/vnd.github+json"},
        )
        if metadata_response.status != 200:
            raise CleanupError(
                f"cleanup package metadata returned HTTP {metadata_response.status}"
            )
        metadata = _mapping(_json(metadata_response.body, "package metadata"), "package metadata")
        associated = _mapping(metadata.get("repository"), "package association")
        if (
            metadata.get("name") != package
            or metadata.get("package_type") != "container"
            or metadata.get("visibility") != "public"
            or associated.get("full_name") != self.expected_source_repository
        ):
            raise CleanupError("cleanup package is not the exact public tap package")

        expected_digest = "sha256:" + checked["target_digest"]
        matches: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for page in range(1, MAX_PACKAGE_VERSIONS // 100 + 2):
            url = base + f"/versions?per_page=100&page={page}"
            response = self._request(
                "GET",
                url,
                authenticated=True,
                headers={"accept": "application/vnd.github+json"},
            )
            if response.status != 200:
                raise CleanupError(
                    f"cleanup package versions returned HTTP {response.status}"
                )
            raw = _json(response.body, "package versions")
            versions = _sequence(raw, "package versions")
            if len(versions) > 100:
                raise CleanupError("package version page exceeds its bound")
            for index, candidate in enumerate(versions):
                version = _mapping(candidate, f"package version {index}")
                identifier = _positive(version.get("id"), "package version ID")
                name = _text(version.get("name"), "package version digest", 128)
                if identifier in seen_ids:
                    raise CleanupError("package version inventory repeated an ID")
                seen_ids.add(identifier)
                if name == expected_digest:
                    matches.append(
                        {
                            "id": identifier,
                            "repository": repository,
                            "digest": name,
                        }
                    )
            if len(seen_ids) > MAX_PACKAGE_VERSIONS:
                raise CleanupError("package version inventory exceeds its bound")
            if len(versions) < 100:
                break
        else:
            raise CleanupError("package version pagination did not terminate")
        if len(matches) != 1:
            raise CleanupError("exact manifest digest did not resolve to one package version")
        version = matches[0]
        prior = self._resolved.get(version["id"])
        identity = (version["repository"], version["digest"])
        if prior not in {None, identity}:
            raise CleanupError("package version ID was reused for another target")
        self._resolved[version["id"]] = identity
        return version

    def delete_exact_version(self, version: Mapping[str, Any]) -> None:
        checked = _exact(
            version,
            frozenset({"id", "repository", "digest"}),
            "resolved package version",
        )
        identifier = _positive(checked["id"], "resolved package version ID")
        repository = _text(checked["repository"], "resolved package repository")
        digest_value = _text(
            checked["digest"], "resolved package digest", 71
        )
        if not digest_value.startswith("sha256:"):
            raise CleanupError("resolved package digest is not SHA-256 qualified")
        digest = "sha256:" + _digest(
            digest_value.removeprefix("sha256:"), "resolved package digest"
        )
        if self._resolved.get(identifier) != (repository, digest):
            raise CleanupError("package version was not resolved by this protected client")
        without_host = repository[len("ghcr.io/") :]
        owner, package = without_host.split("/", 1)
        base = (
            "https://api.github.com/orgs/"
            + quote(owner, safe="")
            + "/packages/container/"
            + quote(package, safe="")
        )
        url = base + "/versions/" + str(identifier)
        response = self._request("DELETE", url, authenticated=True, maximum_bytes=0)
        if response.status != 204 or response.body:
            raise CleanupError(
                f"exact package version deletion returned HTTP {response.status}"
            )
        del self._resolved[identifier]


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CleanupError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CleanupError(f"{field} must be an array")
    return value


def _exact(value: Any, fields: frozenset[str], field: str) -> Mapping[str, Any]:
    result = _mapping(value, field)
    if frozenset(result) != fields:
        raise CleanupError(f"{field} fields changed")
    return result


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise CleanupError(f"{field} is not a lowercase SHA-256 digest")
    return value


def _text(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise CleanupError(f"{field} must be bounded text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise CleanupError(f"{field} is not UTF-8") from error
    if not 1 <= size <= maximum:
        raise CleanupError(f"{field} is outside its byte bound")
    return value


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise CleanupError(f"{field} is not a bounded positive integer")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    text = _text(value, field, 64)
    if RFC3339_UTC.fullmatch(text) is None:
        raise CleanupError(f"{field} is not canonical UTC RFC3339")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise CleanupError(f"{field} is not a real timestamp") from error
    return parsed


def _canonical_timestamp(value: datetime, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CleanupError(f"{field} must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    if normalized.microsecond != 0:
        raise CleanupError(f"{field} must have whole-second precision")
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def _target(value: Any) -> dict[str, Any]:
    target = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "artifact_class",
                "target_digest",
                "repository",
                "immutable_reference",
                "record_kind",
                "record_sha256",
                "request_sha256",
                "source_custody_digest",
            }
        ),
        "retention target",
    )
    if (
        target["schema"] != 1
        or target["kind"] != "kandelo-abi-staging-retention-target"
    ):
        raise CleanupError("retention target protocol is unsupported")
    artifact_class = target["artifact_class"]
    if artifact_class not in {"candidate", "source"}:
        raise CleanupError("retention target artifact class is unsupported")
    digest = _digest(target["target_digest"], "retention target digest")
    repository = target["repository"]
    if not isinstance(repository, str) or OCI_REPOSITORY.fullmatch(repository) is None:
        raise CleanupError("retention target repository is invalid")
    if artifact_class == "candidate":
        if re.fullmatch(
            r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/"
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*-abi-[0-9]+-candidates/"
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*",
            repository,
        ) is None:
            raise CleanupError("candidate cleanup target is not visibly nonendorsed")
        expected_record_kind = "kandelo-abi-staging-candidate"
        source_digest: str | None = _digest(
            target["source_custody_digest"], "candidate source custody target"
        )
    else:
        if re.fullmatch(
            r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/"
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*-abi-[0-9]+-source-custody",
            repository,
        ) is None:
            raise CleanupError("source cleanup target escaped source custody")
        expected_record_kind = "kandelo-source-custody-manifest"
        if target["source_custody_digest"] is not None:
            raise CleanupError("source target cannot name another source target")
        source_digest = None
    if target["record_kind"] != expected_record_kind:
        raise CleanupError("retention target record kind is unknown or contradictory")
    immutable = target["immutable_reference"]
    if immutable != f"{repository}@sha256:{digest}":
        raise CleanupError("retention target is not one immutable digest reference")
    record_sha256 = _digest(target["record_sha256"], "target record")
    if record_sha256 != digest:
        raise CleanupError("retention target record locator differs from its manifest")
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-retention-target",
        "artifact_class": artifact_class,
        "target_digest": digest,
        "repository": repository,
        "immutable_reference": immutable,
        "record_kind": expected_record_kind,
        "record_sha256": record_sha256,
        "request_sha256": _digest(target["request_sha256"], "target request"),
        "source_custody_digest": source_digest,
    }


def _lifecycle(value: Any, field: str) -> dict[str, Any]:
    lifecycle = _exact(
        value, frozenset({"state", "closed_at", "request_reference"}), field
    )
    state = lifecycle["state"]
    if state not in {"open", "merged", "closed-unmerged"}:
        raise CleanupError(f"{field} state is unsupported")
    closed_at = lifecycle["closed_at"]
    if state == "closed-unmerged":
        _timestamp(closed_at, f"{field} closed_at")
    elif closed_at is not None:
        raise CleanupError(f"{field} nonclosed request has a close timestamp")
    request_reference = _text(
        lifecycle["request_reference"], f"{field} request reference"
    )
    if (
        not request_reference.startswith("https://github.com/")
        or f"-sha256-" not in request_reference
        or not request_reference.endswith(".json")
        or any(character.isspace() for character in request_reference)
    ):
        raise CleanupError(f"{field} request reference is not one exact public asset")
    return {
        "state": state,
        "closed_at": closed_at,
        "request_reference": request_reference,
    }


def _reference(value: Any, field: str) -> dict[str, str]:
    reference = _exact(
        value,
        frozenset(
            {"kind", "target_digest", "record_sha256", "immutable_reference"}
        ),
        field,
    )
    kind = reference["kind"]
    if kind not in PIN_KINDS | NONPIN_KINDS:
        raise CleanupError(f"{field} kind is unsupported")
    digest = _digest(reference["record_sha256"], f"{field} record")
    immutable = _text(reference["immutable_reference"], f"{field} reference")
    digest_bound = immutable.endswith("@sha256:" + digest) or (
        f"-sha256-{digest}.json" in immutable
        and immutable.startswith("https://github.com/")
    )
    if any(character.isspace() for character in immutable) or not digest_bound:
        raise CleanupError(f"{field} reference does not bind its exact record")
    return {
        "kind": kind,
        "target_digest": _digest(reference["target_digest"], f"{field} target"),
        "record_sha256": digest,
        "immutable_reference": immutable,
    }


def _assessment(
    *,
    target: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    pins: Sequence[Mapping[str, str]],
    now: datetime,
    grace_days: int,
) -> dict[str, Any]:
    _canonical_timestamp(now, "retention clock")
    if isinstance(grace_days, bool) or not isinstance(grace_days, int) or not 1 <= grace_days <= 3650:
        raise CleanupError("retention grace is outside its bound")
    checked_pins = [dict(candidate) for candidate in pins]
    checked_pins.sort(
        key=lambda candidate: (
            candidate["kind"],
            candidate["record_sha256"],
            candidate["immutable_reference"],
        )
    )
    pin_identities = [
        (
            candidate["kind"],
            candidate["record_sha256"],
            candidate["immutable_reference"],
        )
        for candidate in checked_pins
    ]
    if pin_identities != sorted(set(pin_identities)):
        raise CleanupError("retention pins are not unique")
    state = lifecycle["state"]
    if checked_pins:
        unreferenced_since = None
        grace_complete = False
        deletion_eligible = False
        reason = "pinned"
    elif state == "open":
        unreferenced_since = None
        grace_complete = False
        deletion_eligible = False
        reason = "request-open"
    elif state == "merged":
        unreferenced_since = None
        grace_complete = False
        deletion_eligible = False
        reason = "request-merged"
    else:
        unreferenced_since = lifecycle["closed_at"]
        closed = _timestamp(unreferenced_since, "retention unreferenced_since")
        normalized_now = now.astimezone(timezone.utc)
        if closed > normalized_now:
            raise CleanupError("retention close time is in the future")
        grace_complete = normalized_now - closed >= timedelta(days=grace_days)
        deletion_eligible = grace_complete
        reason = (
            "unreferenced-grace-complete" if grace_complete else "grace-incomplete"
        )
    result = {
        "schema": 1,
        "kind": "kandelo-abi-staging-retention-assessment",
        "target_digest": target["target_digest"],
        "artifact_class": target["artifact_class"],
        "pins": checked_pins,
        "unreferenced_since": unreferenced_since,
        "grace_complete": grace_complete,
        "deletion_eligible": deletion_eligible,
        "reason": reason,
    }
    validate_retention_assessment(result)
    return result


def validate_retention_assessment(value: Mapping[str, Any]) -> dict[str, Any]:
    assessment = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "target_digest",
                "artifact_class",
                "pins",
                "unreferenced_since",
                "grace_complete",
                "deletion_eligible",
                "reason",
            }
        ),
        "retention assessment",
    )
    if (
        assessment["schema"] != 1
        or assessment["kind"] != "kandelo-abi-staging-retention-assessment"
    ):
        raise CleanupError("retention assessment protocol is unsupported")
    digest = _digest(assessment["target_digest"], "assessment target")
    artifact_class = assessment["artifact_class"]
    if artifact_class not in {"candidate", "source"}:
        raise CleanupError("assessment artifact class is unsupported")
    pins = [
        _reference(candidate, f"assessment pin {index}")
        for index, candidate in enumerate(
            _sequence(assessment["pins"], "assessment pins")
        )
    ]
    if any(candidate["kind"] not in PIN_KINDS for candidate in pins):
        raise CleanupError("assessment contains a nonpin reference")
    identities = [
        (candidate["kind"], candidate["record_sha256"], candidate["immutable_reference"])
        for candidate in pins
    ]
    if identities != sorted(set(identities)):
        raise CleanupError("assessment pins are not sorted and unique")
    for pin in pins:
        if pin["target_digest"] != digest:
            raise CleanupError("assessment pin names another target")
    unreferenced = assessment["unreferenced_since"]
    if unreferenced is not None:
        _timestamp(unreferenced, "assessment unreferenced_since")
    grace = assessment["grace_complete"]
    eligible = assessment["deletion_eligible"]
    if not isinstance(grace, bool) or not isinstance(eligible, bool):
        raise CleanupError("assessment decisions are not Boolean")
    reason = assessment["reason"]
    expected = {
        "pinned": (True, None, False, False),
        "request-merged": (False, None, False, False),
        "grace-incomplete": (False, "timestamp", False, False),
        "unreferenced-grace-complete": (False, "timestamp", True, True),
    }.get(reason)
    if expected is None:
        raise CleanupError("assessment reason is unsupported")
    expected_pins, expected_time, expected_grace, expected_eligible = expected
    if (
        bool(pins) != expected_pins
        or ("timestamp" if unreferenced is not None else None) != expected_time
        or grace != expected_grace
        or eligible != expected_eligible
    ):
        raise CleanupError("retention assessment decision is contradictory")
    return copy.deepcopy(dict(assessment))


def assess_retention_inventory(
    *,
    targets: Sequence[Mapping[str, Any]],
    lifecycles: Mapping[str, Mapping[str, Any]],
    references: Sequence[Mapping[str, Any]],
    now: datetime,
    grace_days: int,
) -> dict[str, dict[str, Any]]:
    """Assess a bounded complete inventory and derive shared-custody pins."""

    checked_targets = [_target(candidate) for candidate in _sequence(targets, "targets")]
    if not 0 <= len(checked_targets) <= MAX_TARGETS:
        raise CleanupError("retention target inventory is outside its bound")
    target_by_digest = {candidate["target_digest"]: candidate for candidate in checked_targets}
    if len(target_by_digest) != len(checked_targets):
        raise CleanupError("retention targets are not digest-unique")
    checked_lifecycles = {
        _digest(request, "lifecycle request"): _lifecycle(
            candidate, f"request {request} lifecycle"
        )
        for request, candidate in _mapping(lifecycles, "lifecycles").items()
    }
    required_requests = {candidate["request_sha256"] for candidate in checked_targets}
    if set(checked_lifecycles) != required_requests:
        raise CleanupError("retention lifecycle inventory is incomplete or extra")
    checked_references = [
        _reference(candidate, f"reference {index}")
        for index, candidate in enumerate(_sequence(references, "references"))
    ]
    if len(checked_references) > MAX_REFERENCES:
        raise CleanupError("retention reference inventory is outside its bound")
    if any(candidate["target_digest"] not in target_by_digest for candidate in checked_references):
        raise CleanupError("retention reference names an unknown target")
    direct_by_target: dict[str, list[dict[str, str]]] = {
        digest: [] for digest in target_by_digest
    }
    for reference in checked_references:
        if reference["kind"] in PIN_KINDS:
            direct_by_target[reference["target_digest"]].append(reference)
    results: dict[str, dict[str, Any]] = {}
    for digest in sorted(target_by_digest):
        target = target_by_digest[digest]
        target_pins = list(direct_by_target[digest])
        lifecycle = checked_lifecycles[target["request_sha256"]]
        if lifecycle["state"] == "open":
            target_pins.append(
                {
                    "kind": "open-request",
                    "target_digest": digest,
                    "record_sha256": target["request_sha256"],
                    "immutable_reference": lifecycle["request_reference"],
                }
            )
        results[digest] = _assessment(
            target=target,
            lifecycle=lifecycle,
            pins=target_pins,
            now=now,
            grace_days=grace_days,
        )
    candidates = [
        candidate for candidate in checked_targets if candidate["artifact_class"] == "candidate"
    ]
    for digest in sorted(target_by_digest):
        source = target_by_digest[digest]
        if source["artifact_class"] != "source":
            continue
        derived = []
        for candidate in candidates:
            if candidate["source_custody_digest"] == digest:
                derived.append(
                    {
                        "kind": "shared-custody",
                        "target_digest": digest,
                        "record_sha256": candidate["record_sha256"],
                        "immutable_reference": candidate["immutable_reference"],
                    }
                )
        if derived:
            results[digest] = _assessment(
                target=source,
                lifecycle=checked_lifecycles[source["request_sha256"]],
                pins=[*direct_by_target[digest], *derived],
                now=now,
                grace_days=grace_days,
            )
    return results


def _retention_inventory(value: Mapping[str, Any]) -> dict[str, Any]:
    inventory = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "targets",
                "lifecycles",
                "references",
                "tombstones",
            }
        ),
        "retention inventory",
    )
    if (
        inventory["schema"] != 1
        or inventory["kind"] != "kandelo-abi-staging-retention-inventory"
    ):
        raise CleanupError("retention inventory protocol is unsupported")
    targets = [
        _target(candidate)
        for candidate in _sequence(inventory["targets"], "retention targets")
    ]
    if not 0 <= len(targets) <= MAX_TARGETS:
        raise CleanupError("retention target inventory is outside its bound")
    targets.sort(key=lambda candidate: candidate["immutable_reference"])
    if len({candidate["target_digest"] for candidate in targets}) != len(targets):
        raise CleanupError("retention target inventory repeats a digest")
    lifecycles = {
        _digest(key, "retention lifecycle request"): _lifecycle(
            candidate, f"request {key} lifecycle"
        )
        for key, candidate in _mapping(
            inventory["lifecycles"], "retention lifecycles"
        ).items()
    }
    references = [
        _reference(candidate, f"retention reference {index}")
        for index, candidate in enumerate(
            _sequence(inventory["references"], "retention references")
        )
    ]
    if len(references) > MAX_REFERENCES:
        raise CleanupError("retention reference inventory is outside its bound")
    references.sort(
        key=lambda candidate: (
            candidate["target_digest"],
            candidate["kind"],
            candidate["record_sha256"],
            candidate["immutable_reference"],
        )
    )
    identities = [
        (
            candidate["target_digest"],
            candidate["kind"],
            candidate["record_sha256"],
            candidate["immutable_reference"],
        )
        for candidate in references
    ]
    if identities != sorted(set(identities)):
        raise CleanupError("retention references are not unique")
    target_digests = {candidate["target_digest"] for candidate in targets}
    if any(candidate["target_digest"] not in target_digests for candidate in references):
        raise CleanupError("retention reference names an unknown target")
    tombstones = [
        validate_deletion_record(
            _mapping(candidate, f"retention tombstone {index}")
        )
        for index, candidate in enumerate(
            _sequence(inventory["tombstones"], "retention tombstones")
        )
    ]
    tombstones.sort(key=lambda candidate: candidate["target"]["immutable_reference"])
    tombstone_identities = [
        candidate["target"]["immutable_reference"] for candidate in tombstones
    ]
    if tombstone_identities != sorted(set(tombstone_identities)):
        raise CleanupError("retention tombstones repeat a target")
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-retention-inventory",
        "targets": targets,
        "lifecycles": {key: lifecycles[key] for key in sorted(lifecycles)},
        "references": references,
        "tombstones": tombstones,
    }


def _tap_source(value: Any) -> dict[str, str]:
    source = _exact(
        value,
        frozenset({"repository", "commit", "tree"}),
        "cleanup tap source",
    )
    repository = _text(source["repository"], "cleanup tap repository", 255)
    if re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository) is None:
        raise CleanupError("cleanup tap repository is invalid")
    commit = source["commit"]
    tree = source["tree"]
    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        or not isinstance(tree, str)
        or re.fullmatch(r"[0-9a-f]{40}", tree) is None
    ):
        raise CleanupError("cleanup tap source is not an exact Git identity")
    return {"repository": repository, "commit": commit, "tree": tree}


def build_cleanup_batch(
    *,
    inventory: Mapping[str, Any],
    tap_source: Mapping[str, Any],
    now: datetime,
    grace_days: int,
    batch_size: int,
    mode: str,
    target_reference: str,
    reason_category: str,
    justification: str,
    maintainer: Mapping[str, Any] | None,
) -> dict[str, Any]:
    checked_inventory = _retention_inventory(inventory)
    checked_source = _tap_source(tap_source)
    planned_at = _canonical_timestamp(now, "cleanup plan time")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_CLEANUP_BATCH
    ):
        raise CleanupError("cleanup batch size is outside its bound")
    assessments = assess_retention_inventory(
        targets=checked_inventory["targets"],
        lifecycles=checked_inventory["lifecycles"],
        references=checked_inventory["references"],
        now=now,
        grace_days=grace_days,
    )
    tombstoned = {
        candidate["target"]["immutable_reference"]
        for candidate in checked_inventory["tombstones"]
    }
    by_reference = {
        candidate["immutable_reference"]: candidate
        for candidate in checked_inventory["targets"]
    }
    if target_reference:
        if (
            any(character.isspace() for character in target_reference)
            or target_reference not in by_reference
        ):
            raise CleanupError("cleanup target is not one discovered immutable reference")
        selected_targets = [by_reference[target_reference]]
    else:
        selected_targets = list(checked_inventory["targets"])
    selected_targets.sort(key=lambda candidate: candidate["immutable_reference"])
    plans: list[dict[str, Any]] = []
    if mode == "ordinary":
        if reason_category != "retention-expired" or justification or maintainer is not None:
            raise CleanupError("ordinary cleanup gained maintenance authority")
        for target in selected_targets:
            if target["immutable_reference"] in tombstoned:
                raise CleanupError("a tombstoned cleanup target is present again")
            assessment = assessments[target["target_digest"]]
            if not assessment["deletion_eligible"]:
                continue
            plans.append(
                build_deletion_plan(
                    target=target,
                    assessment=assessment,
                    mode=mode,
                    reason_category=reason_category,
                    authorization=None,
                    decision_time=planned_at,
                )
            )
            if len(plans) == batch_size:
                break
    elif mode == "immediate-purge":
        if not target_reference or len(selected_targets) != 1 or maintainer is None:
            raise CleanupError("immediate purge requires one exact target and maintainer")
        target = selected_targets[0]
        if target["immutable_reference"] in tombstoned:
            raise CleanupError("a tombstoned cleanup target is present again")
        assessment = assessments[target["target_digest"]]
        authorization = authorize_immediate_purge(
            target=target,
            assessment=assessment,
            reason_category=reason_category,
            justification=justification,
            maintainer=maintainer,
            authorized_at=planned_at,
        )
        plans.append(
            build_deletion_plan(
                target=target,
                assessment=assessment,
                mode=mode,
                reason_category=reason_category,
                authorization=authorization,
                decision_time=planned_at,
            )
        )
    else:
        raise CleanupError("cleanup mode is unsupported")
    result = {
        "schema": 1,
        "kind": "kandelo-abi-staging-cleanup-batch",
        "tap_source": checked_source,
        "planned_at": planned_at,
        "grace_days": grace_days,
        "batch_size": batch_size,
        "inventory": checked_inventory,
        "inventory_sha256": canonical_sha256(checked_inventory),
        "plans": plans,
    }
    validate_cleanup_batch(result)
    return result


def validate_cleanup_batch(value: Mapping[str, Any]) -> dict[str, Any]:
    batch = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "tap_source",
                "planned_at",
                "grace_days",
                "batch_size",
                "inventory",
                "inventory_sha256",
                "plans",
            }
        ),
        "cleanup batch",
    )
    if batch["schema"] != 1 or batch["kind"] != "kandelo-abi-staging-cleanup-batch":
        raise CleanupError("cleanup batch protocol is unsupported")
    source = _tap_source(batch["tap_source"])
    planned_at = _canonical_timestamp(
        _timestamp(batch["planned_at"], "cleanup plan time"),
        "cleanup plan time",
    )
    grace_days = batch["grace_days"]
    batch_size = batch["batch_size"]
    if (
        isinstance(grace_days, bool)
        or not isinstance(grace_days, int)
        or not 1 <= grace_days <= 3650
        or isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_CLEANUP_BATCH
    ):
        raise CleanupError("cleanup batch bounds changed")
    inventory = _retention_inventory(batch["inventory"])
    inventory_sha256 = _digest(batch["inventory_sha256"], "cleanup inventory")
    if canonical_sha256(inventory) != inventory_sha256:
        raise CleanupError("cleanup inventory digest changed")
    plans = [
        _validate_deletion_plan(
            _mapping(candidate, f"cleanup deletion plan {index}")
        )
        for index, candidate in enumerate(
            _sequence(batch["plans"], "cleanup deletion plans")
        )
    ]
    if len(plans) > batch_size:
        raise CleanupError("cleanup batch exceeds its bound")
    identities = [candidate["target"]["immutable_reference"] for candidate in plans]
    if identities != sorted(set(identities)):
        raise CleanupError("cleanup plans are not sorted and unique")
    assessments = assess_retention_inventory(
        targets=inventory["targets"],
        lifecycles=inventory["lifecycles"],
        references=inventory["references"],
        now=_timestamp(planned_at, "cleanup plan time"),
        grace_days=grace_days,
    )
    targets = {
        candidate["immutable_reference"]: candidate
        for candidate in inventory["targets"]
    }
    for plan in plans:
        target = plan["target"]
        if targets.get(target["immutable_reference"]) != target:
            raise CleanupError("cleanup plan target is absent from its inventory")
        assessment = assessments[target["target_digest"]]
        if canonical_sha256(assessment) != plan["assessment_sha256"]:
            raise CleanupError("cleanup plan assessment differs from its inventory")
        if plan["decision_time"] != planned_at:
            raise CleanupError("cleanup plan time differs from its batch")
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-cleanup-batch",
        "tap_source": source,
        "planned_at": planned_at,
        "grace_days": grace_days,
        "batch_size": batch_size,
        "inventory": inventory,
        "inventory_sha256": inventory_sha256,
        "plans": plans,
    }


def _contains_digest(value: Any, digest: str) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_digest(child, digest) for child in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_digest(child, digest) for child in value)
    return isinstance(value, str) and (
        value == digest
        or value == "sha256:" + digest
        or value.endswith("@sha256:" + digest)
    )


def classify_retention_reference(
    *,
    record: Mapping[str, Any],
    locator: Mapping[str, Any],
    target_digest: str,
    lifecycle_state: str | None,
) -> dict[str, str]:
    digest = _digest(target_digest, "retention reference target")
    checked_locator = _exact(
        locator,
        frozenset({"repository", "digest", "immutable_reference"}),
        "retention record locator",
    )
    repository = _text(checked_locator["repository"], "retention record repository")
    record_digest_value = _text(
        checked_locator["digest"], "retention record digest", 71
    )
    if not record_digest_value.startswith("sha256:"):
        raise CleanupError("retention record locator digest is invalid")
    record_digest = _digest(
        record_digest_value.removeprefix("sha256:"), "retention record digest"
    )
    immutable = checked_locator["immutable_reference"]
    if immutable != f"{repository}@sha256:{record_digest}":
        raise CleanupError("retention record locator is not immutable")
    if not _contains_digest(record, digest):
        raise CleanupError("retention record does not reference the target")
    kind = record.get("kind")
    durable = {
        "kandelo-abi-staging-admission": "merged-admission",
        "kandelo-abi-staging-candidate-reuse": "candidate-reuse",
        "kandelo-homebrew-canonical-bottle": "canonical-layer",
    }
    lifecycle_bound = {
        "kandelo-abi-staging-verification": "active-verification",
        "kandelo-abi-staging-override-receipt": "active-verification",
        "kandelo-vfs-candidate-product": "active-product",
        "kandelo-vfs-product-evidence-override": "active-product",
        "kandelo-vfs-product-evidence-receipt": "active-product",
        "kandelo-abi-staging-product-evidence": "active-product",
        "kandelo-abi-staging-promotion-plan": "active-promotion",
        "kandelo-abi-staging-metadata-patch": "active-promotion",
    }
    historical = {
        "kandelo-abi-history-record",
        "kandelo-abi-staging-attempt",
        "kandelo-abi-staging-attempt-outcome",
        "kandelo-abi-staging-candidate",
        "kandelo-abi-staging-deletion-record",
        "kandelo-source-custody-manifest",
    }
    repair = {
        "kandelo-abi-historical-maintenance-authorization",
        "kandelo-abi-historical-repair-plan",
    }
    if kind in durable:
        reference_kind = durable[str(kind)]
    elif kind in lifecycle_bound:
        reference_kind = (
            lifecycle_bound[str(kind)]
            if lifecycle_state in {"open", "merged"}
            else "historical-identity"
        )
    elif kind in repair:
        reference_kind = "active-repair"
    elif kind == "kandelo-abi-epoch-status":
        state = record.get("state")
        reference_kind = (
            "active-repair" if state in {"active", "retiring"} else "historical-identity"
        )
    elif kind in historical:
        reference_kind = "historical-identity"
    else:
        raise CleanupError(f"referencing record kind {kind!r} has no retention policy")
    return {
        "kind": reference_kind,
        "target_digest": digest,
        "record_sha256": record_digest,
        "immutable_reference": immutable,
    }


def _public_record_entry(value: Any, field: str) -> dict[str, Any]:
    entry = _exact(value, frozenset({"locator", "record"}), field)
    locator = _exact(
        entry["locator"],
        frozenset({"repository", "digest", "immutable_reference"}),
        f"{field} locator",
    )
    repository = _text(locator["repository"], f"{field} repository")
    digest_value = _text(locator["digest"], f"{field} digest", 71)
    if not digest_value.startswith("sha256:"):
        raise CleanupError(f"{field} digest is invalid")
    digest = _digest(digest_value.removeprefix("sha256:"), f"{field} digest")
    immutable = locator["immutable_reference"]
    if immutable != f"{repository}@sha256:{digest}":
        raise CleanupError(f"{field} locator is not immutable")
    record = dict(_mapping(entry["record"], f"{field} record"))
    if record.get("schema") != 1 or not isinstance(record.get("kind"), str):
        raise CleanupError(f"{field} record protocol is unsupported")
    return {
        "locator": {
            "repository": repository,
            "digest": "sha256:" + digest,
            "immutable_reference": immutable,
        },
        "record": copy.deepcopy(record),
    }


def _record_request_digest(record: Mapping[str, Any]) -> str | None:
    common = record.get("common")
    if not isinstance(common, Mapping) or "request_sha256" not in common:
        return None
    return _digest(common["request_sha256"], "public record request")


def build_live_retention_inventory(
    *,
    records: Sequence[Mapping[str, Any]],
    lifecycles: Mapping[str, Mapping[str, Any]],
    required_targets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive candidate/source targets and explicit pins from public records."""

    checked_records = [
        _public_record_entry(candidate, f"public record {index}")
        for index, candidate in enumerate(_sequence(records, "public records"))
    ]
    checked_records.sort(
        key=lambda candidate: candidate["locator"]["immutable_reference"]
    )
    locator_identities = [
        candidate["locator"]["immutable_reference"] for candidate in checked_records
    ]
    if locator_identities != sorted(set(locator_identities)):
        raise CleanupError("public record inventory repeats an immutable locator")
    checked_lifecycles = {
        _digest(key, "public request lifecycle"): _lifecycle(
            candidate, f"request {key} lifecycle"
        )
        for key, candidate in _mapping(lifecycles, "public lifecycles").items()
    }
    targets: dict[str, dict[str, Any]] = {}
    candidate_sources: dict[str, str] = {}
    candidate_requests: dict[str, str] = {}
    source_records: dict[str, dict[str, Any]] = {}
    deleted_source_requests: dict[str, set[str]] = {}
    tombstones: list[dict[str, Any]] = []

    def remember_candidate_identity(
        candidate_digest: str, source_digest: str, request_digest: str
    ) -> None:
        prior = (
            candidate_sources.get(candidate_digest),
            candidate_requests.get(candidate_digest),
        )
        identity = (source_digest, request_digest)
        if prior not in {(None, None), identity}:
            raise CleanupError("candidate retention identity changed across records")
        candidate_sources[candidate_digest] = source_digest
        candidate_requests[candidate_digest] = request_digest

    for entry in checked_records:
        locator = entry["locator"]
        record = entry["record"]
        digest = locator["digest"].removeprefix("sha256:")
        if record["kind"] == "kandelo-abi-staging-candidate":
            common = _mapping(record.get("common"), "candidate retention common")
            payload = _mapping(record.get("candidate"), "candidate retention payload")
            request_digest = _digest(
                common.get("request_sha256"), "candidate retention request"
            )
            source_digest = _digest(
                payload.get("source_custody_sha256"),
                "candidate retention source custody",
            )
            target = _target(
                {
                    "schema": 1,
                    "kind": "kandelo-abi-staging-retention-target",
                    "artifact_class": "candidate",
                    "target_digest": digest,
                    "repository": locator["repository"],
                    "immutable_reference": locator["immutable_reference"],
                    "record_kind": record["kind"],
                    "record_sha256": digest,
                    "request_sha256": request_digest,
                    "source_custody_digest": source_digest,
                }
            )
            targets[digest] = target
            remember_candidate_identity(digest, source_digest, request_digest)
        elif record["kind"] == "kandelo-source-custody-manifest":
            source_records[digest] = entry
        elif record["kind"] == "kandelo-abi-staging-deletion-record":
            tombstone = validate_deletion_record(record)
            tombstones.append(tombstone)
            deleted = tombstone["target"]
            if deleted["artifact_class"] == "candidate":
                remember_candidate_identity(
                    deleted["target_digest"],
                    deleted["source_custody_digest"],
                    deleted["request_sha256"],
                )
            else:
                deleted_source_requests.setdefault(
                    deleted["target_digest"], set()
                ).add(deleted["request_sha256"])
    for candidate in required_targets:
        target = _target(candidate)
        prior = targets.get(target["target_digest"])
        if prior is not None and prior != target:
            raise CleanupError("required cleanup target differs from public inventory")
        targets[target["target_digest"]] = target
        if target["artifact_class"] == "candidate":
            remember_candidate_identity(
                target["target_digest"],
                target["source_custody_digest"],
                target["request_sha256"],
            )
    tombstoned_digests = {
        candidate["target"]["target_digest"] for candidate in tombstones
    }
    source_requests = {
        digest: set(requests)
        for digest, requests in deleted_source_requests.items()
    }
    for candidate_digest, source_digest in candidate_sources.items():
        if source_digest not in source_records and source_digest not in targets:
            if source_digest in tombstoned_digests:
                continue
            raise CleanupError("candidate references an absent source-custody record")
        source_requests.setdefault(source_digest, set()).add(
            candidate_requests[candidate_digest]
        )
    for source_digest, entry in source_records.items():
        requests = sorted(source_requests.get(source_digest, set()))
        if not requests:
            # An interrupted source-only publication has no request identity from
            # which complete candidate deletion coverage can be proven.  A
            # required target from an older plan is not factual absence evidence.
            targets.pop(source_digest, None)
            continue
        def request_retention_order(request_digest: str) -> tuple[int, datetime, str]:
            lifecycle = checked_lifecycles.get(request_digest)
            if lifecycle is None:
                raise CleanupError(
                    "source-custody request lacks a public lifecycle"
                )
            state = lifecycle["state"]
            if state == "open":
                return (2, datetime.min.replace(tzinfo=timezone.utc), request_digest)
            if state == "merged":
                return (1, datetime.min.replace(tzinfo=timezone.utc), request_digest)
            return (
                0,
                _timestamp(
                    lifecycle["closed_at"], "source-custody request close time"
                ),
                request_digest,
            )

        retention_request = max(requests, key=request_retention_order)
        locator = entry["locator"]
        target = _target(
            {
                "schema": 1,
                "kind": "kandelo-abi-staging-retention-target",
                "artifact_class": "source",
                "target_digest": source_digest,
                "repository": locator["repository"],
                "immutable_reference": locator["immutable_reference"],
                "record_kind": "kandelo-source-custody-manifest",
                "record_sha256": source_digest,
                "request_sha256": retention_request,
                "source_custody_digest": None,
            }
        )
        prior = targets.get(source_digest)
        if prior is not None and prior != target:
            raise CleanupError("required source target differs from public inventory")
        targets[source_digest] = target
    required_requests = {target["request_sha256"] for target in targets.values()}
    if not required_requests.issubset(checked_lifecycles):
        raise CleanupError("public lifecycle inventory is incomplete for cleanup targets")
    references: list[dict[str, str]] = []
    for digest in sorted(targets):
        target = targets[digest]
        for entry in checked_records:
            locator = entry["locator"]
            record = entry["record"]
            if locator["digest"] == "sha256:" + digest:
                continue
            if not _contains_digest(record, digest):
                continue
            request_digest = _record_request_digest(record)
            state = (
                checked_lifecycles[request_digest]["state"]
                if request_digest in checked_lifecycles
                else None
            )
            references.append(
                classify_retention_reference(
                    record=record,
                    locator=locator,
                    target_digest=digest,
                    lifecycle_state=state,
                )
            )
    inventory = {
        "schema": 1,
        "kind": "kandelo-abi-staging-retention-inventory",
        "targets": list(targets.values()),
        "lifecycles": {
            key: checked_lifecycles[key] for key in sorted(required_requests)
        },
        "references": references,
        "tombstones": tombstones,
    }
    return _retention_inventory(inventory)


def execute_cleanup_batch(
    batch: Mapping[str, Any],
    *,
    current_tap_source: Mapping[str, Any],
    collect_inventory: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    registry: RegistryDeletionV1,
    publish_tombstone: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    checked = validate_cleanup_batch(batch)
    if _tap_source(current_tap_source) != checked["tap_source"]:
        raise CleanupError("cleanup writer source differs from the protected plan")
    records: list[dict[str, Any]] = []
    for plan in checked["plans"]:
        target = plan["target"]
        refreshed_inventory = _retention_inventory(collect_inventory(target))
        assessments = assess_retention_inventory(
            targets=refreshed_inventory["targets"],
            lifecycles=refreshed_inventory["lifecycles"],
            references=refreshed_inventory["references"],
            now=_timestamp(checked["planned_at"], "cleanup plan time"),
            grace_days=checked["grace_days"],
        )
        if target["target_digest"] not in assessments:
            raise CleanupError("fresh cleanup inventory lost its exact target")
        existing = [
            tombstone
            for tombstone in refreshed_inventory["tombstones"]
            if tombstone["target"]["target_digest"] == target["target_digest"]
            or tombstone["target"]["immutable_reference"]
            == target["immutable_reference"]
        ]
        record = execute_exact_deletion(
            plan,
            recheck=lambda assessment=assessments[target["target_digest"]]: assessment,
            registry=registry,
            existing_tombstones=existing,
            publish_tombstone=publish_tombstone,
        )
        records.append(record)
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-cleanup-result",
        "tap_source": checked["tap_source"],
        "plan_sha256": canonical_sha256(checked),
        "records": records,
    }


def _maintainer(value: Any) -> dict[str, str]:
    maintainer = _exact(
        value,
        frozenset({"login", "permission", "authorization_reference"}),
        "cleanup maintainer",
    )
    login = _text(maintainer["login"], "cleanup maintainer login", 128)
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login) is None:
        raise CleanupError("cleanup maintainer login is invalid")
    if maintainer["permission"] not in {"admin", "maintain", "write"}:
        raise CleanupError("cleanup maintainer lacks write authority")
    reference = _text(
        maintainer["authorization_reference"], "cleanup authorization reference"
    )
    if not reference.startswith("https://github.com/") or any(
        character.isspace() for character in reference
    ):
        raise CleanupError("cleanup authorization reference is not protected GitHub evidence")
    return {
        "login": login.lower(),
        "permission": str(maintainer["permission"]),
        "authorization_reference": reference,
    }


def authorize_immediate_purge(
    *,
    target: Mapping[str, Any],
    assessment: Mapping[str, Any],
    reason_category: str,
    justification: str,
    maintainer: Mapping[str, Any],
    authorized_at: str,
) -> dict[str, Any]:
    checked_target = _target(target)
    checked_assessment = validate_retention_assessment(assessment)
    if (
        checked_assessment["target_digest"] != checked_target["target_digest"]
        or checked_assessment["artifact_class"] != checked_target["artifact_class"]
        or checked_assessment["pins"]
    ):
        raise CleanupError("immediate purge target is mismatched or still pinned")
    if reason_category not in IMMEDIATE_REASONS:
        raise CleanupError("immediate purge reason category is unsupported")
    checked_justification = _text(
        justification, "immediate purge justification", MAX_JUSTIFICATION_BYTES
    )
    if not checked_justification.strip():
        raise CleanupError("immediate purge justification cannot be blank")
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-immediate-purge-authorization",
        "target": checked_target,
        "assessment_sha256": canonical_sha256(checked_assessment),
        "reason_category": reason_category,
        "justification": checked_justification,
        "maintainer": _maintainer(maintainer),
        "authorized_at": _canonical_timestamp(
            _timestamp(authorized_at, "immediate purge authorization time"),
            "immediate purge authorization time",
        ),
    }


def _validate_immediate_authorization(
    value: Any,
    *,
    target: Mapping[str, Any],
    assessment_sha256: str,
    reason_category: str,
) -> dict[str, Any]:
    authorization = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "target",
                "assessment_sha256",
                "reason_category",
                "justification",
                "maintainer",
                "authorized_at",
            }
        ),
        "immediate purge authorization",
    )
    if (
        authorization["schema"] != 1
        or authorization["kind"]
        != "kandelo-abi-staging-immediate-purge-authorization"
        or _target(authorization["target"]) != dict(target)
        or _digest(authorization["assessment_sha256"], "authorization assessment")
        != assessment_sha256
        or authorization["reason_category"] != reason_category
        or reason_category not in IMMEDIATE_REASONS
    ):
        raise CleanupError("immediate purge authorization identity changed")
    justification = _text(
        authorization["justification"],
        "immediate purge justification",
        MAX_JUSTIFICATION_BYTES,
    )
    if not justification.strip():
        raise CleanupError("immediate purge justification cannot be blank")
    _maintainer(authorization["maintainer"])
    _timestamp(authorization["authorized_at"], "immediate purge authorization time")
    return copy.deepcopy(dict(authorization))


def build_deletion_plan(
    *,
    target: Mapping[str, Any],
    assessment: Mapping[str, Any],
    mode: str,
    reason_category: str,
    authorization: Mapping[str, Any] | None,
    decision_time: str,
) -> dict[str, Any]:
    checked_target = _target(target)
    checked_assessment = validate_retention_assessment(assessment)
    if (
        checked_assessment["target_digest"] != checked_target["target_digest"]
        or checked_assessment["artifact_class"] != checked_target["artifact_class"]
    ):
        raise CleanupError("deletion assessment names another target")
    assessment_sha256 = canonical_sha256(checked_assessment)
    checked_decision_time = _canonical_timestamp(
        _timestamp(decision_time, "deletion decision time"),
        "deletion decision time",
    )
    if mode == "ordinary":
        if (
            reason_category != "retention-expired"
            or authorization is not None
            or not checked_assessment["deletion_eligible"]
        ):
            raise CleanupError("ordinary deletion lacks elapsed unreferenced retention")
        checked_authorization = None
        authorization_sha256 = None
    elif mode == "immediate-purge":
        if authorization is None or checked_assessment["pins"]:
            raise CleanupError("immediate purge lacks exact unpinned authorization")
        checked_authorization = _validate_immediate_authorization(
            authorization,
            target=checked_target,
            assessment_sha256=assessment_sha256,
            reason_category=reason_category,
        )
        authorization_sha256 = canonical_sha256(checked_authorization)
        if checked_authorization["authorized_at"] != checked_decision_time:
            raise CleanupError("immediate deletion time differs from its authorization")
    else:
        raise CleanupError("deletion mode is unsupported")
    identity = {
        "target": checked_target,
        "assessment_sha256": assessment_sha256,
        "mode": mode,
        "reason_category": reason_category,
        "authorization_sha256": authorization_sha256,
        "decision_time": checked_decision_time,
    }
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-deletion-plan",
        "plan_sha256": canonical_sha256(identity),
        **identity,
        "authorization": checked_authorization,
    }


def _validate_deletion_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    plan = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "plan_sha256",
                "target",
                "assessment_sha256",
                "mode",
                "reason_category",
                "authorization_sha256",
                "authorization",
                "decision_time",
            }
        ),
        "deletion plan",
    )
    checked_target = _target(plan["target"])
    assessment_sha256 = _digest(plan["assessment_sha256"], "plan assessment")
    mode = plan["mode"]
    reason = plan["reason_category"]
    authorization_sha256 = plan["authorization_sha256"]
    decision_time = _canonical_timestamp(
        _timestamp(plan["decision_time"], "deletion decision time"),
        "deletion decision time",
    )
    if mode == "ordinary":
        if reason != "retention-expired" or authorization_sha256 is not None or plan["authorization"] is not None:
            raise CleanupError("ordinary deletion plan gained maintenance authority")
    elif mode == "immediate-purge":
        digest = _digest(authorization_sha256, "plan authorization")
        authorization = _validate_immediate_authorization(
            plan["authorization"],
            target=checked_target,
            assessment_sha256=assessment_sha256,
            reason_category=reason,
        )
        if canonical_sha256(authorization) != digest:
            raise CleanupError("deletion authorization digest changed")
        if authorization["authorized_at"] != decision_time:
            raise CleanupError("immediate deletion time differs from its authorization")
    else:
        raise CleanupError("deletion plan mode is unsupported")
    identity = {
        "target": checked_target,
        "assessment_sha256": assessment_sha256,
        "mode": mode,
        "reason_category": reason,
        "authorization_sha256": authorization_sha256,
        "decision_time": decision_time,
    }
    if (
        plan["schema"] != 1
        or plan["kind"] != "kandelo-abi-staging-deletion-plan"
        or _digest(plan["plan_sha256"], "deletion plan") != canonical_sha256(identity)
    ):
        raise CleanupError("deletion plan identity changed")
    return copy.deepcopy(dict(plan))


def _deletion_record(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "kind": "kandelo-abi-staging-deletion-record",
        "target": copy.deepcopy(plan["target"]),
        "assessment_sha256": plan["assessment_sha256"],
        "mode": plan["mode"],
        "reason_category": plan["reason_category"],
        "authorization": copy.deepcopy(plan["authorization"]),
        "deleted_at": plan["decision_time"],
        "prior_records": [
            {
                "record_sha256": plan["target"]["record_sha256"],
                "immutable_reference": plan["target"]["immutable_reference"],
            }
        ],
        "absence": {
            "method": "anonymous-immutable-manifest-get",
            "status": 404,
        },
    }


def validate_deletion_record(value: Mapping[str, Any]) -> dict[str, Any]:
    record = _exact(
        value,
        frozenset(
            {
                "schema",
                "kind",
                "target",
                "assessment_sha256",
                "mode",
                "reason_category",
                "authorization",
                "deleted_at",
                "prior_records",
                "absence",
            }
        ),
        "deletion record",
    )
    target = _target(record["target"])
    assessment_sha256 = _digest(record["assessment_sha256"], "deletion assessment")
    mode = record["mode"]
    reason = record["reason_category"]
    _timestamp(record["deleted_at"], "deletion time")
    prior_records = _sequence(record["prior_records"], "deletion prior records")
    expected_prior = [
        {
            "record_sha256": target["record_sha256"],
            "immutable_reference": target["immutable_reference"],
        }
    ]
    if list(prior_records) != expected_prior:
        raise CleanupError("deletion record lost or changed its prior record")
    if mode == "ordinary":
        if reason != "retention-expired" or record["authorization"] is not None:
            raise CleanupError("ordinary tombstone gained maintenance authority")
    elif mode == "immediate-purge":
        _validate_immediate_authorization(
            record["authorization"],
            target=target,
            assessment_sha256=assessment_sha256,
            reason_category=reason,
        )
        if record["authorization"]["authorized_at"] != record["deleted_at"]:
            raise CleanupError("immediate tombstone time differs from authorization")
    else:
        raise CleanupError("deletion record mode is unsupported")
    absence = _exact(
        record["absence"], frozenset({"method", "status"}), "deletion absence"
    )
    if (
        absence["method"] != "anonymous-immutable-manifest-get"
        or absence["status"] != 404
    ):
        raise CleanupError("deletion record lacks exact anonymous absence")
    if record["schema"] != 1 or record["kind"] != "kandelo-abi-staging-deletion-record":
        raise CleanupError("deletion record protocol is unsupported")
    return copy.deepcopy(dict(record))


def _probe(
    registry: RegistryDeletionV1,
    target: Mapping[str, Any],
    *,
    allow_present: bool,
) -> bool:
    probe = _exact(
        registry.probe_anonymous(target),
        frozenset({"status", "url", "digest"}),
        "anonymous deletion probe",
    )
    if probe["url"] != target["immutable_reference"]:
        raise CleanupError("anonymous deletion probe followed or changed its target")
    if probe["status"] == 404 and probe["digest"] is None:
        return False
    if (
        allow_present
        and probe["status"] == 200
        and probe["digest"] == "sha256:" + target["target_digest"]
    ):
        return True
    raise CleanupError("anonymous deletion state is unconfirmed or contradictory")


def execute_exact_deletion(
    plan: Mapping[str, Any],
    *,
    recheck: Callable[[], Mapping[str, Any]],
    registry: RegistryDeletionV1,
    existing_tombstones: Sequence[Mapping[str, Any]],
    publish_tombstone: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Delete one digest only after fresh pins, then prove absence and tombstone."""

    checked_plan = _validate_deletion_plan(plan)
    target = checked_plan["target"]
    refreshed = validate_retention_assessment(recheck())
    if (
        refreshed["target_digest"] != target["target_digest"]
        or refreshed["artifact_class"] != target["artifact_class"]
        or canonical_sha256(refreshed) != checked_plan["assessment_sha256"]
        or refreshed["pins"]
        or (
            checked_plan["mode"] == "ordinary"
            and not refreshed["deletion_eligible"]
        )
    ):
        raise CleanupError(
            "pre-delete retention assessment changed or no longer authorizes deletion"
        )

    expected_record = _deletion_record(checked_plan)
    validate_deletion_record(expected_record)
    matches = []
    for index, candidate in enumerate(
        _sequence(existing_tombstones, "existing deletion tombstones")
    ):
        tombstone = validate_deletion_record(
            _mapping(candidate, f"existing deletion tombstone {index}")
        )
        if (
            tombstone["target"]["target_digest"] == target["target_digest"]
            or tombstone["target"]["immutable_reference"]
            == target["immutable_reference"]
        ):
            matches.append(tombstone)
    if len(matches) > 1 or (matches and matches[0] != expected_record):
        raise CleanupError("existing deletion tombstone conflicts with exact reason or target")

    present = _probe(registry, target, allow_present=True)
    if matches:
        if present:
            raise CleanupError("tombstoned immutable target became present again")
        return matches[0]
    if present:
        version = _exact(
            registry.resolve_exact_version(target),
            frozenset({"id", "repository", "digest"}),
            "registry package version",
        )
        _positive(version["id"], "registry package version ID")
        if (
            version["repository"] != target["repository"]
            or version["digest"] != "sha256:" + target["target_digest"]
        ):
            raise CleanupError("registry package version differs from exact target")
        registry.delete_exact_version(dict(version))
        if _probe(registry, target, allow_present=False):
            raise CleanupError("registry deletion did not remove the exact target")
    publish_tombstone(expected_record)
    return expected_record


def build_deletion_oci_plan(record: Mapping[str, Any]) -> OciRecordPlanV1:
    checked = validate_deletion_record(record)
    target = checked["target"]
    body = canonical_bytes(checked)
    repository = target["repository"][len("ghcr.io/") :] + "/deletions"
    package = repository.split("/", 1)[1]
    source_name = package.split("-abi-", 1)[0]
    owner = repository.split("/", 1)[0]
    return OciRecordPlanV1(
        repository=repository,
        artifact_type=DELETION_RECORD_MEDIA_TYPE,
        config=OciBlobV1(
            role="deletion-record",
            media_type=DELETION_RECORD_MEDIA_TYPE,
            body=body,
            title="deletion-record.json",
        ),
        layers=(
            OciBlobV1(
                role="immutable-record-bytes",
                media_type=DELETION_RECORD_MEDIA_TYPE,
                body=body,
                title="deletion-record.json",
            ),
        ),
        annotations={
            "dev.kandelo.abi-staging.classification": "immutable-deletion-tombstone",
            "dev.kandelo.abi-staging.kind": "deletion-record",
            "dev.kandelo.abi-staging.target-sha256": target["target_digest"],
            "org.opencontainers.image.source": f"https://github.com/{owner}/{source_name}",
        },
    )


def collect_live_retention_inventory(
    *,
    tap_root: Path,
    policy: TapStagingPolicyV1,
    repository: str,
    username: str,
    token: str,
    required_targets: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Collect the complete public retention graph without executing records."""

    if repository != policy.tap_repository:
        raise CleanupError("live retention repository differs from protected policy")
    transport = UrllibOciTransportV1(username=username, token=token)
    client = GitHubRetentionInventoryClientV1(
        expected_source_repository=policy.tap_repository,
        package_prefix=policy.candidate_repository_prefix,
        transport=transport,
    )
    records = client.scan_records()
    requests_needed: set[str] = set()
    for entry in records:
        record = entry["record"]
        if record.get("kind") == "kandelo-abi-staging-candidate":
            common = _mapping(record.get("common"), "live candidate common")
            requests_needed.add(
                _digest(common.get("request_sha256"), "live candidate request")
            )
        elif record.get("kind") == "kandelo-abi-staging-deletion-record":
            deleted = validate_deletion_record(record)["target"]
            requests_needed.add(deleted["request_sha256"])
    checked_required = [_target(candidate) for candidate in required_targets]
    requests_needed.update(
        candidate["request_sha256"] for candidate in checked_required
    )
    lifecycles: dict[str, dict[str, Any]] = {}
    if requests_needed:
        issuer_policy = load_request_issuer_policy(
            tap_root / "Kandelo/staging/request-issuers.toml",
            expected_tap=policy.tap_repository,
        )
        discovered = GitHubPublicClient(issuer_policy).scan()
        by_digest = {candidate.request_digest: candidate for candidate in discovered}
        if len(by_digest) != len(discovered):
            raise CleanupError("public request feed repeats a digest")
        if not requests_needed.issubset(by_digest):
            raise CleanupError("cleanup target request is absent from the public feed")
        for digest in sorted(requests_needed):
            request = by_digest[digest]
            pull = _mapping(request.request.get("pull_request"), "cleanup request PR")
            lifecycles[digest] = client.pull_request_lifecycle(
                repository=_text(
                    pull.get("repository"), "cleanup request repository", 255
                ),
                number=_positive(pull.get("number"), "cleanup request PR number"),
                request_reference=request.asset_url,
            )
    return build_live_retention_inventory(
        records=records,
        lifecycles=lifecycles,
        required_targets=checked_required,
    )


def resolve_cleanup_maintainer(
    *,
    repository: str,
    actor: str,
    authorization_reference: str,
    token: str,
) -> dict[str, str]:
    from .override import GitHubMaintenanceClientV1

    if not token:
        raise CleanupError("cleanup maintainer verification lacks GitHub authority")
    try:
        return GitHubMaintenanceClientV1(repository, token).maintainer(
            actor, authorization_reference
        )
    except ValueError as error:
        raise CleanupError(f"cleanup maintainer verification failed: {error}") from error


def _protected_tap_root(value: str) -> Path:
    try:
        root = Path(value).resolve(strict=True)
        module_root = Path(__file__).resolve(strict=True).parents[2]
    except OSError as error:
        raise CleanupError(f"cannot resolve protected tap checkout: {error}") from error
    if root != module_root:
        raise CleanupError("--tap-root must name this protected tap checkout")
    return root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.abi_staging.cleanup")
    commands = parser.add_subparsers(dest="operation", required=True)
    plan = commands.add_parser("plan-live")
    plan.add_argument("--tap-root", required=True)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--mode", choices=("ordinary", "immediate-purge"), required=True)
    plan.add_argument("--target-reference", required=True)
    plan.add_argument("--reason-category", required=True)
    plan.add_argument("--justification", required=True)
    plan.add_argument("--actor", required=True)
    plan.add_argument("--authorization-reference", required=True)
    plan.add_argument("--verify-actor-permission", action="store_true")
    plan.add_argument("--enumerate-public-records", action="store_true")
    plan.add_argument("--recheck-lifecycle", action="store_true")
    plan.add_argument("--grace-days", required=True, type=int)
    plan.add_argument("--batch-size", required=True, type=int)
    plan.add_argument("--out", required=True)
    plan.add_argument("--github-output", required=True)
    execute = commands.add_parser("execute-live")
    execute.add_argument("--tap-root", required=True)
    execute.add_argument("--repository", required=True)
    execute.add_argument("--plan", required=True)
    execute.add_argument("--plan-artifact-id", required=True, type=int)
    execute.add_argument("--plan-artifact-digest", required=True)
    execute.add_argument("--recheck-live", action="store_true")
    execute.add_argument("--one-exact-version", action="store_true")
    execute.add_argument("--anonymous-absence", action="store_true")
    execute.add_argument("--immutable-tombstone", action="store_true")
    execute.add_argument("--batch-size", required=True, type=int)
    execute.add_argument("--out", required=True)
    return parser


def _output_directory(value: str) -> Path:
    output = Path(value)
    if output.exists():
        raise CleanupError("cleanup output already exists")
    try:
        output.mkdir(parents=True, mode=0o700)
    except OSError as error:
        raise CleanupError(f"cannot create cleanup output: {error}") from error
    return output.resolve(strict=True)


def _write_github_outputs(path: Path, values: Mapping[str, str]) -> None:
    for key, value in values.items():
        if (
            re.fullmatch(r"[a-z_][a-z0-9_]*", key) is None
            or "\n" in value
            or "\r" in value
        ):
            raise CleanupError("cleanup GitHub output is malformed")
    try:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for key in sorted(values):
                handle.write(f"{key}={values[key]}\n")
    except OSError as error:
        raise CleanupError(f"cannot write cleanup GitHub outputs: {error}") from error


def _plan_live(args: argparse.Namespace) -> None:
    if (
        not args.verify_actor_permission
        or not args.enumerate_public_records
        or not args.recheck_lifecycle
    ):
        raise CleanupError("live cleanup planning lacks protected discovery guards")
    root = _protected_tap_root(args.tap_root)
    policy = load_tap_staging_policy(root / "Kandelo/staging/tap-policy.toml")
    if args.repository != policy.tap_repository:
        raise CleanupError("cleanup repository differs from protected policy")
    if args.grace_days != policy.candidate_retention_days_after_unmerged_close:
        raise CleanupError("cleanup grace differs from protected policy")
    username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
    token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
    inventory = collect_live_retention_inventory(
        tap_root=root,
        policy=policy,
        repository=args.repository,
        username=username,
        token=token,
    )
    maintainer = None
    if args.mode == "immediate-purge":
        maintainer = resolve_cleanup_maintainer(
            repository=args.repository,
            actor=args.actor,
            authorization_reference=args.authorization_reference,
            token=os.environ.get("GITHUB_TOKEN", ""),
        )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    source = snapshot_tap_source(root, policy.tap_repository)
    batch = build_cleanup_batch(
        inventory=inventory,
        tap_source=source,
        now=now,
        grace_days=args.grace_days,
        batch_size=args.batch_size,
        mode=args.mode,
        target_reference=args.target_reference,
        reason_category=args.reason_category,
        justification=args.justification,
        maintainer=maintainer,
    )
    output = _output_directory(args.out)
    (output / "plan.json").write_bytes(canonical_bytes(batch))
    _write_github_outputs(
        Path(args.github_output),
        {
            "has_work": "true" if batch["plans"] else "false",
            "tap_commit": source["commit"],
        },
    )


def _execute_live(args: argparse.Namespace) -> None:
    if (
        not args.recheck_live
        or not args.one_exact_version
        or not args.anonymous_absence
        or not args.immutable_tombstone
    ):
        raise CleanupError("live cleanup execution lacks exact deletion guards")
    root = _protected_tap_root(args.tap_root)
    policy = load_tap_staging_policy(root / "Kandelo/staging/tap-policy.toml")
    if args.repository != policy.tap_repository:
        raise CleanupError("cleanup repository differs from protected policy")
    plan_path = Path(args.plan)
    try:
        metadata = plan_path.lstat()
        body = plan_path.read_bytes()
    except OSError as error:
        raise CleanupError(f"cannot read cleanup plan: {error}") from error
    if not plan_path.is_file() or plan_path.is_symlink() or not 1 <= metadata.st_size <= 64 * 1024 * 1024:
        raise CleanupError("cleanup plan is not one bounded regular file")
    if len(body) != metadata.st_size:
        raise CleanupError("cleanup plan changed while reading")
    batch = validate_cleanup_batch(
        _mapping(_json(body, "cleanup plan"), "cleanup plan")
    )
    if canonical_bytes(batch) != body:
        raise CleanupError("cleanup plan is not canonical JSON")
    if args.batch_size != batch["batch_size"]:
        raise CleanupError("cleanup execution batch bound differs from its plan")
    artifact_id = _positive(args.plan_artifact_id, "cleanup plan artifact ID")
    artifact_digest = _digest(
        args.plan_artifact_digest, "cleanup plan artifact digest"
    )
    actor = _text(os.environ.get("GITHUB_ACTOR"), "cleanup workflow actor", 255)
    github_token = _text(
        os.environ.get("GITHUB_TOKEN"), "cleanup workflow token", 4096
    )
    if any(character.isspace() for character in github_token):
        raise CleanupError("cleanup workflow token is malformed")
    run_id_text = _text(
        os.environ.get("GITHUB_RUN_ID"), "cleanup workflow run ID", 20
    )
    run_attempt_text = _text(
        os.environ.get("GITHUB_RUN_ATTEMPT"),
        "cleanup workflow run attempt",
        20,
    )
    if not run_id_text.isdigit() or not run_attempt_text.isdigit():
        raise CleanupError("cleanup workflow run identity is malformed")
    handoff = verify_cleanup_plan_artifact(
        repository=policy.tap_repository,
        artifact_id=artifact_id,
        artifact_digest=artifact_digest,
        run_id=int(run_id_text),
        run_attempt=int(run_attempt_text),
        head_sha=_text(
            os.environ.get("GITHUB_SHA"), "cleanup workflow head", 40
        ),
        workflow_ref=_text(
            os.environ.get("GITHUB_WORKFLOW_REF"),
            "cleanup workflow ref",
            512,
        ),
        transport=UrllibOciTransportV1(username=actor, token=github_token),
    )
    current_source = snapshot_tap_source(root, policy.tap_repository)
    username = os.environ.get("HOMEBREW_GITHUB_PACKAGES_USER", "")
    token = os.environ.get("HOMEBREW_GITHUB_PACKAGES_TOKEN", "")
    published: list[dict[str, Any]] = []
    with isolated_oras_transport(username=username, token=token) as transport:
        registry = GitHubPackageDeletionClientV1(
            expected_source_repository=policy.tap_repository,
            transport=transport,
        )

        def collect(target: Mapping[str, Any]) -> dict[str, Any]:
            return collect_live_retention_inventory(
                tap_root=root,
                policy=policy,
                repository=policy.tap_repository,
                username=username,
                token=token,
                required_targets=(target,),
            )

        def publish_tombstone(record: dict[str, Any]) -> None:
            locator = publish_record(
                build_deletion_oci_plan(record),
                transport=transport,
                expected_source_repository=policy.tap_repository,
            )
            if locator is not None:
                published.append(
                    {
                        "repository": locator.repository,
                        "digest": locator.digest,
                        "immutable_reference": locator.immutable_reference,
                    }
                )

        result = execute_cleanup_batch(
            batch,
            current_tap_source=current_source,
            collect_inventory=collect,
            registry=registry,
            publish_tombstone=publish_tombstone,
        )
    result["handoff"] = handoff
    result["published_tombstones"] = published
    output = _output_directory(args.out)
    (output / "result.json").write_bytes(canonical_bytes(result))


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(arguments)
        if args.operation == "plan-live":
            _plan_live(args)
        else:
            _execute_live(args)
        return 0
    except (CleanupError, OSError, ValueError) as error:
        print(f"abi-staging cleanup: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
