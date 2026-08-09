"""Deterministic OCI record construction and fail-closed publication."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Protocol
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .canonical import canonical_bytes, canonical_sha256
from .records import (
    MAX_RECORD_BYTES,
    OCI_MANIFEST_MEDIA_TYPE,
    OciBlobV1,
    OciRecordPlanV1,
)


MANIFEST_ACCEPT = OCI_MANIFEST_MEDIA_TYPE
REGISTRY_HOST = "ghcr.io"
GITHUB_API_HOST = "api.github.com"


@dataclass(frozen=True)
class HttpResponseV1:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


class OciTransportV1(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        authenticated: bool,
        maximum_bytes: int,
    ) -> HttpResponseV1: ...


@dataclass(frozen=True)
class PublishedRecordLocatorV1:
    repository: str
    digest: str
    immutable_reference: str
    anonymous_readback_sha256: str


class OciPublicationError(ValueError):
    """Publication failure carrying the registered staging guard and retry fact."""

    def __init__(self, message: str, *, guard_code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.guard_code = guard_code
        self.retryable = retryable


def _descriptor(blob: OciBlobV1) -> dict[str, object]:
    return {
        "mediaType": blob.media_type,
        "digest": blob.digest,
        "size": blob.size,
        "annotations": {
            "dev.kandelo.abi-staging.role": blob.role,
            "org.opencontainers.image.title": blob.title,
        },
    }


def build_oci_manifest(plan: OciRecordPlanV1) -> bytes:
    """Build the one canonical OCI manifest encoding for a local record plan."""

    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "artifactType": plan.artifact_type,
        "config": _descriptor(plan.config),
        "layers": [_descriptor(layer) for layer in plan.layers],
        "annotations": dict(plan.annotations),
    }
    body = canonical_bytes(manifest)
    if len(body) > MAX_RECORD_BYTES:
        raise OciPublicationError(
            "OCI manifest exceeds its record byte bound",
            guard_code="namespace_bootstrap_failed",
        )
    return body


def _header(response: HttpResponseV1, name: str) -> str | None:
    lowered = name.lower()
    for key, value in response.headers.items():
        if key.lower() == lowered:
            return value
    return None


def _registry_url(repository: str, kind: str, identity: str = "") -> str:
    base = f"https://{REGISTRY_HOST}/v2/{repository}/{kind}"
    return base + ("/" + quote(identity, safe=":") if identity else "")


def _request(
    transport: OciTransportV1,
    method: str,
    url: str,
    *,
    authenticated: bool,
    guard_code: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    maximum_bytes: int = 4 * 1024 * 1024,
) -> HttpResponseV1:
    try:
        response = transport.request(
            method,
            url,
            headers=headers,
            body=body,
            authenticated=authenticated,
            maximum_bytes=maximum_bytes,
        )
    except OciPublicationError:
        raise
    except Exception as error:
        raise OciPublicationError(
            f"OCI transport failed before a response: {error}",
            guard_code=guard_code,
            retryable=True,
        ) from error
    if response.url != url:
        raise OciPublicationError(
            f"OCI transport attempted a redirect from {url!r} to {response.url!r}",
            guard_code=guard_code,
        )
    if response.status >= 500 or response.status == 429:
        raise OciPublicationError(
            f"OCI endpoint returned retryable HTTP {response.status}",
            guard_code=guard_code,
            retryable=True,
        )
    if len(response.body) > maximum_bytes:
        raise OciPublicationError(
            "OCI endpoint exceeded its response byte bound",
            guard_code=guard_code,
        )
    return response


def _validate_digest_size_headers(
    response: HttpResponseV1,
    blob: OciBlobV1,
    *,
    guard_code: str,
) -> None:
    digest = _header(response, "docker-content-digest")
    if digest is not None and digest != blob.digest:
        raise OciPublicationError(
            f"registry reported the wrong digest for {blob.role}",
            guard_code=guard_code,
        )
    length = _header(response, "content-length")
    if length is not None:
        try:
            parsed = int(length, 10)
        except ValueError as error:
            raise OciPublicationError(
                f"registry reported malformed size for {blob.role}",
                guard_code=guard_code,
            ) from error
        if parsed != blob.size:
            raise OciPublicationError(
                f"registry reported the wrong size for {blob.role}",
                guard_code=guard_code,
            )


def _validate_blob_response(
    response: HttpResponseV1,
    blob: OciBlobV1,
    *,
    guard_code: str,
) -> None:
    if response.status != 200:
        raise OciPublicationError(
            f"registry could not read {blob.role}: HTTP {response.status}",
            guard_code=guard_code,
        )
    _validate_digest_size_headers(response, blob, guard_code=guard_code)
    if len(response.body) != blob.size or hashlib.sha256(response.body).hexdigest() != blob.digest[
        len("sha256:") :
    ]:
        raise OciPublicationError(
            f"registry readback bytes drifted for {blob.role}",
            guard_code=guard_code,
        )


def _safe_upload_location(location: str, repository: str, field: str) -> str:
    resolved = urljoin(f"https://{REGISTRY_HOST}/", location)
    parsed = urlsplit(resolved)
    expected_prefix = f"/v2/{repository}/blobs/uploads/"
    if (
        parsed.scheme != "https"
        or parsed.netloc != REGISTRY_HOST
        or not parsed.path.startswith(expected_prefix)
        or parsed.fragment
    ):
        raise OciPublicationError(
            f"registry returned a hostile {field} location",
            guard_code="namespace_bootstrap_failed",
        )
    return resolved


def _upload_blob(
    plan: OciRecordPlanV1,
    blob: OciBlobV1,
    transport: OciTransportV1,
) -> None:
    blob_url = _registry_url(plan.repository, "blobs", blob.digest)
    probe = _request(
        transport,
        "HEAD",
        blob_url,
        authenticated=True,
        guard_code="namespace_bootstrap_failed",
        maximum_bytes=0,
    )
    if probe.status == 200:
        _validate_digest_size_headers(
            probe, blob, guard_code="candidate_public_readback_failed"
        )
    elif probe.status != 404:
        raise OciPublicationError(
            f"registry blob probe returned HTTP {probe.status}",
            guard_code="namespace_bootstrap_failed",
        )
    else:
        upload_url: str | None = None
        mounted_successfully = False
        if blob.mount_from is not None:
            mount_query = urlencode({"mount": blob.digest, "from": blob.mount_from})
            mount_url = _registry_url(plan.repository, "blobs/uploads/") + "?" + mount_query
            mounted = _request(
                transport,
                "POST",
                mount_url,
                authenticated=True,
                guard_code="namespace_bootstrap_failed",
                maximum_bytes=0,
            )
            if mounted.status == 201:
                mounted_successfully = True
                _validate_digest_size_headers(
                    mounted, blob, guard_code="namespace_bootstrap_failed"
                )
            elif mounted.status == 202:
                location = _header(mounted, "location")
                if location is None:
                    raise OciPublicationError(
                        "registry mount fallback omitted its upload location",
                        guard_code="namespace_bootstrap_failed",
                    )
                upload_url = _safe_upload_location(
                    location, plan.repository, "mount fallback"
                )
            elif mounted.status not in {404, 405}:
                raise OciPublicationError(
                    f"registry blob mount returned HTTP {mounted.status}",
                    guard_code="namespace_bootstrap_failed",
                )
        if upload_url is None and not mounted_successfully:
            # A successful mount already made the blob readable. Otherwise open
            # one fresh upload session in the exact destination repository.
            begin_url = _registry_url(plan.repository, "blobs/uploads/")
            begun = _request(
                transport,
                "POST",
                begin_url,
                authenticated=True,
                guard_code="namespace_bootstrap_failed",
                maximum_bytes=0,
            )
            if begun.status != 202 or _header(begun, "location") is None:
                raise OciPublicationError(
                    f"registry blob upload start returned HTTP {begun.status}",
                    guard_code="namespace_bootstrap_failed",
                )
            upload_url = _safe_upload_location(
                _header(begun, "location") or "",
                plan.repository,
                "blob upload",
            )
        if upload_url is not None:
            separator = "&" if "?" in upload_url else "?"
            completion_url = upload_url + separator + urlencode({"digest": blob.digest})
            completed = _request(
                transport,
                "PUT",
                completion_url,
                authenticated=True,
                guard_code="namespace_bootstrap_failed",
                headers={"content-type": blob.media_type},
                body=blob.body,
                maximum_bytes=0,
            )
            if completed.status != 201:
                raise OciPublicationError(
                    f"registry blob upload returned HTTP {completed.status}",
                    guard_code="namespace_bootstrap_failed",
                )
            _validate_digest_size_headers(
                completed, blob, guard_code="namespace_bootstrap_failed"
            )
    authenticated_read = _request(
        transport,
        "GET",
        blob_url,
        authenticated=True,
        guard_code="candidate_public_readback_failed",
        maximum_bytes=blob.size,
    )
    _validate_blob_response(
        authenticated_read, blob, guard_code="candidate_public_readback_failed"
    )


def _probe_manifest(
    plan: OciRecordPlanV1,
    reference: str,
    expected: bytes,
    transport: OciTransportV1,
    *,
    collision: str,
) -> bool:
    url = _registry_url(plan.repository, "manifests", reference)
    response = _request(
        transport,
        "GET",
        url,
        authenticated=True,
        guard_code="namespace_bootstrap_failed",
        headers={"accept": MANIFEST_ACCEPT},
        maximum_bytes=max(len(expected), 4 * 1024 * 1024),
    )
    if response.status == 404:
        return False
    if response.status != 200:
        raise OciPublicationError(
            f"registry manifest probe returned HTTP {response.status}",
            guard_code="namespace_bootstrap_failed",
        )
    if response.body != expected:
        raise OciPublicationError(
            f"immutable {collision} collision contains different manifest bytes",
            guard_code="namespace_bootstrap_failed",
        )
    expected_digest = "sha256:" + hashlib.sha256(expected).hexdigest()
    observed = _header(response, "docker-content-digest")
    if observed is not None and observed != expected_digest:
        raise OciPublicationError(
            f"immutable {collision} collision reports a different digest",
            guard_code="namespace_bootstrap_failed",
        )
    return True


def _verify_package_association(
    repository: str,
    expected_source_repository: str,
    transport: OciTransportV1,
) -> None:
    owner, package = repository.split("/", 1)
    url = (
        f"https://{GITHUB_API_HOST}/orgs/{quote(owner, safe='')}/packages/container/"
        + quote(package, safe="")
    )
    response = _request(
        transport,
        "GET",
        url,
        authenticated=True,
        guard_code="namespace_bootstrap_failed",
        headers={"accept": "application/vnd.github+json"},
        maximum_bytes=4 * 1024 * 1024,
    )
    if response.status != 200:
        raise OciPublicationError(
            f"GitHub package metadata returned HTTP {response.status}",
            guard_code="namespace_bootstrap_failed",
        )
    try:
        metadata = json.loads(response.body)
        association = metadata["repository"]["full_name"]
        visibility = metadata["visibility"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise OciPublicationError(
            "GitHub package metadata is malformed",
            guard_code="namespace_bootstrap_failed",
        ) from error
    if association != expected_source_repository or visibility != "public":
        raise OciPublicationError(
            "OCI package is not public and associated with the protected tap",
            guard_code="namespace_bootstrap_failed",
        )


def publish_record(
    plan: OciRecordPlanV1,
    *,
    transport: OciTransportV1,
    expected_source_repository: str,
) -> PublishedRecordLocatorV1:
    """Publish exact bytes, then independently read the manifest and every blob."""

    manifest = build_oci_manifest(plan)
    digest_hex = hashlib.sha256(manifest).hexdigest()
    digest = "sha256:" + digest_hex
    tag = "record-sha256-" + digest_hex
    tag_exists = _probe_manifest(
        plan, tag, manifest, transport, collision="record tag"
    )
    digest_exists = _probe_manifest(
        plan, digest, manifest, transport, collision="manifest digest"
    )
    blobs = (plan.config, *plan.layers)
    for blob in blobs:
        _upload_blob(plan, blob, transport)
    if not tag_exists:
        manifest_url = _registry_url(plan.repository, "manifests", tag)
        response = _request(
            transport,
            "PUT",
            manifest_url,
            authenticated=True,
            guard_code="namespace_bootstrap_failed",
            headers={"content-type": OCI_MANIFEST_MEDIA_TYPE},
            body=manifest,
            maximum_bytes=0,
        )
        if response.status != 201 or _header(response, "docker-content-digest") != digest:
            raise OciPublicationError(
                f"registry manifest upload returned contradictory HTTP {response.status}",
                guard_code="namespace_bootstrap_failed",
            )
    elif not digest_exists:
        raise OciPublicationError(
            "record tag exists but its immutable digest cannot be resolved",
            guard_code="namespace_bootstrap_failed",
        )
    if not _probe_manifest(
        plan, digest, manifest, transport, collision="manifest digest"
    ):
        raise OciPublicationError(
            "new OCI manifest cannot be resolved by digest",
            guard_code="namespace_bootstrap_failed",
        )
    _verify_package_association(
        plan.repository, expected_source_repository, transport
    )

    anonymous_manifest = _request(
        transport,
        "GET",
        _registry_url(plan.repository, "manifests", digest),
        authenticated=False,
        guard_code="candidate_public_readback_failed",
        headers={"accept": MANIFEST_ACCEPT},
        maximum_bytes=len(manifest),
    )
    if anonymous_manifest.status != 200 or anonymous_manifest.body != manifest:
        raise OciPublicationError(
            "anonymous manifest readback is private, missing, or byte-drifted",
            guard_code="candidate_public_readback_failed",
        )
    if _header(anonymous_manifest, "docker-content-digest") not in {None, digest}:
        raise OciPublicationError(
            "anonymous manifest readback reported a different digest",
            guard_code="candidate_public_readback_failed",
        )
    readback_blobs = []
    for blob in blobs:
        response = _request(
            transport,
            "GET",
            _registry_url(plan.repository, "blobs", blob.digest),
            authenticated=False,
            guard_code="candidate_public_readback_failed",
            maximum_bytes=blob.size,
        )
        _validate_blob_response(
            response, blob, guard_code="candidate_public_readback_failed"
        )
        readback_blobs.append(
            {"digest": blob.digest, "role": blob.role, "size": blob.size}
        )
    evidence_sha256 = canonical_sha256(
        {
            "manifest": {"digest": digest, "size": len(manifest)},
            "blobs": readback_blobs,
        }
    )
    return PublishedRecordLocatorV1(
        repository="ghcr.io/" + plan.repository,
        digest=digest,
        immutable_reference=f"ghcr.io/{plan.repository}@{digest}",
        anonymous_readback_sha256=evidence_sha256,
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        return None


class UrllibOciTransportV1:
    """Bounded OCI/GitHub HTTP transport with explicit bearer challenges."""

    def __init__(self, *, username: str, token: str) -> None:
        if (
            not username
            or not token
            or any(character.isspace() for character in username)
            or any(character.isspace() for character in token)
        ):
            raise OciPublicationError(
                "registry credentials are missing or malformed",
                guard_code="namespace_bootstrap_failed",
            )
        self._username = username
        self._token = token
        self._opener = build_opener(_NoRedirect())
        self._bearer_tokens: dict[tuple[bool, str], str] = {}

    def _basic(self) -> str:
        encoded = base64.b64encode(
            f"{self._username}:{self._token}".encode("utf-8")
        ).decode("ascii")
        return "Basic " + encoded

    def _read_response(self, response, maximum_bytes: int) -> bytes:
        limit = max(maximum_bytes, 64 * 1024)
        body = response.read(limit + 1)
        if len(body) > limit:
            raise OciPublicationError(
                "HTTP response exceeded its transport byte bound",
                guard_code="candidate_public_readback_failed",
            )
        if maximum_bytes == 0:
            return b""
        if len(body) > maximum_bytes:
            raise OciPublicationError(
                "HTTP response exceeded its requested byte bound",
                guard_code="candidate_public_readback_failed",
            )
        return body

    def _perform(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        maximum_bytes: int,
    ) -> HttpResponseV1:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=120) as response:
                return HttpResponseV1(
                    response.status,
                    {key.lower(): value for key, value in response.headers.items()},
                    self._read_response(response, maximum_bytes),
                    response.geturl(),
                )
        except HTTPError as error:
            final_url = error.geturl()
            if 300 <= error.code < 400 and error.headers.get("location"):
                final_url = urljoin(url, error.headers["location"])
            return HttpResponseV1(
                error.code,
                {key.lower(): value for key, value in error.headers.items()},
                self._read_response(error, maximum_bytes),
                final_url,
            )
        except URLError as error:
            raise OSError(f"HTTP transport unavailable: {error.reason}") from error

    @staticmethod
    def _challenge(value: str) -> dict[str, str]:
        if not value.startswith("Bearer "):
            raise OciPublicationError(
                "registry authentication challenge is not Bearer",
                guard_code="namespace_bootstrap_failed",
            )
        parameters: dict[str, str] = {}
        for match in re.finditer(r'([a-z]+)="([^"]*)"(?:,|$)', value[len("Bearer ") :]):
            parameters[match.group(1)] = match.group(2)
        if not {"realm", "service", "scope"}.issubset(parameters):
            raise OciPublicationError(
                "registry authentication challenge is incomplete",
                guard_code="namespace_bootstrap_failed",
            )
        realm = urlsplit(parameters["realm"])
        if realm.scheme != "https" or realm.netloc != REGISTRY_HOST:
            raise OciPublicationError(
                "registry authentication challenge has a hostile realm",
                guard_code="namespace_bootstrap_failed",
            )
        return parameters

    def _bearer(self, challenge: str, *, authenticated: bool) -> str:
        parameters = self._challenge(challenge)
        key = (authenticated, parameters["scope"])
        if key in self._bearer_tokens:
            return self._bearer_tokens[key]
        separator = "&" if "?" in parameters["realm"] else "?"
        token_url = parameters["realm"] + separator + urlencode(
            {"service": parameters["service"], "scope": parameters["scope"]}
        )
        headers = {"accept": "application/json"}
        if authenticated:
            headers["authorization"] = self._basic()
        response = self._perform(
            "GET", token_url, headers=headers, body=None, maximum_bytes=1024 * 1024
        )
        if response.url != token_url or response.status != 200:
            raise OciPublicationError(
                "registry bearer-token exchange failed",
                guard_code="namespace_bootstrap_failed",
                retryable=response.status >= 500 or response.status == 429,
            )
        try:
            payload = json.loads(response.body)
            bearer = payload.get("token") or payload["access_token"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise OciPublicationError(
                "registry bearer-token response is malformed",
                guard_code="namespace_bootstrap_failed",
            ) from error
        if not isinstance(bearer, str) or not bearer or any(
            character.isspace() for character in bearer
        ):
            raise OciPublicationError(
                "registry bearer token is malformed",
                guard_code="namespace_bootstrap_failed",
            )
        self._bearer_tokens[key] = bearer
        return bearer

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        authenticated: bool,
        maximum_bytes: int,
    ) -> HttpResponseV1:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in {
            REGISTRY_HOST,
            GITHUB_API_HOST,
        }:
            raise OciPublicationError(
                "HTTP transport target is outside the GitHub allowlist",
                guard_code="namespace_bootstrap_failed",
            )
        request_headers = {key.lower(): value for key, value in (headers or {}).items()}
        if {"authorization", "cookie", "proxy-authorization"} & set(request_headers):
            raise OciPublicationError(
                "callers cannot inject HTTP credentials",
                guard_code="namespace_bootstrap_failed",
            )
        request_headers.setdefault("user-agent", "kandelo-abi-staging/1")
        if parsed.netloc == GITHUB_API_HOST:
            if not authenticated:
                raise OciPublicationError(
                    "GitHub package metadata requires protected authentication",
                    guard_code="namespace_bootstrap_failed",
                )
            request_headers["authorization"] = "Bearer " + self._token
            request_headers.setdefault("x-github-api-version", "2022-11-28")
            return self._perform(
                method,
                url,
                headers=request_headers,
                body=body,
                maximum_bytes=maximum_bytes,
            )
        if authenticated:
            request_headers["authorization"] = self._basic()
        response = self._perform(
            method,
            url,
            headers=request_headers,
            body=body,
            maximum_bytes=maximum_bytes,
        )
        if response.status != 401:
            return response
        challenge = _header(response, "www-authenticate")
        if challenge is None:
            return response
        bearer = self._bearer(challenge, authenticated=authenticated)
        retried_headers = dict(request_headers)
        retried_headers["authorization"] = "Bearer " + bearer
        return self._perform(
            method,
            url,
            headers=retried_headers,
            body=body,
            maximum_bytes=maximum_bytes,
        )


@contextmanager
def isolated_oras_transport(
    *, username: str, token: str, oras: str = "oras"
) -> Iterator[UrllibOciTransportV1]:
    """Bootstrap credentials with ORAS in an ephemeral config, then remove it."""

    transport = UrllibOciTransportV1(username=username, token=token)
    with tempfile.TemporaryDirectory(prefix="kandelo-oras-auth-") as temporary:
        config = Path(temporary) / "config.json"
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "HOME",
                "PATH",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "NIX_SSL_CERT_FILE",
            }
        }
        try:
            result = subprocess.run(
                [
                    oras,
                    "login",
                    "--registry-config",
                    str(config),
                    "--username",
                    username,
                    "--password-stdin",
                    REGISTRY_HOST,
                ],
                input=token.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OciPublicationError(
                f"isolated ORAS login could not run: {error}",
                guard_code="namespace_bootstrap_failed",
                retryable=True,
            ) from error
        if result.returncode != 0 or not config.is_file() or config.is_symlink():
            raise OciPublicationError(
                "isolated ORAS login rejected protected registry credentials",
                guard_code="namespace_bootstrap_failed",
                retryable=False,
            )
        yield transport
